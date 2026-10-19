"""Bounded, read-only macOS AKP03 event probe.

This adapter intentionally has no relationship to the Linux hidraw transport or
Deck lifecycle.  After enumeration proves one physical vendor interface, it
uses only hidapi's ``open_path``, ``set_nonblocking``, ``read``, and ``close``
operations on a fresh device object.
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TextIO, cast

from .events import Event, decode_event
from . import preflight

_MIN_SECONDS = 1
_MAX_SECONDS = 30
_READ_SIZE = 1024
_POLL_INTERVAL_SECONDS = 0.01
_MAX_OPEN_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 0.05


class ReadOnlyHIDDevice(Protocol):
    """The deliberately minimal hidapi surface used by this probe."""

    def open_path(self, path: bytes | str) -> None: ...

    def set_nonblocking(self, nonblocking: int) -> None: ...

    def read(self, size: int) -> Sequence[int] | bytes: ...

    def close(self) -> None: ...


HIDDeviceFactory = Callable[[], ReadOnlyHIDDevice]
PreflightCheck = Callable[[], preflight.PreflightResult]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class ProbeResult:
    """The complete, bounded outcome of one read-only probe."""

    status: str
    events: tuple[Event, ...] = ()
    unknown_reports: int = 0
    detail: str | None = None
    attempts: int = 0

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"


def validate_seconds(value: object) -> int:
    """Validate an explicitly requested finite monitoring duration."""
    if isinstance(value, bool) or not isinstance(value, int) or not _MIN_SECONDS <= value <= _MAX_SECONDS:
        raise ValueError(f"seconds must be an integer from {_MIN_SECONDS} through {_MAX_SECONDS}")
    return value


def _parse_seconds(value: str) -> int:
    try:
        return validate_seconds(int(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _hidapi_device_factory(
    import_module: Callable[[str], Any] = importlib.import_module,
) -> HIDDeviceFactory | None:
    """Load the optional hidapi factory lazily, without opening a path."""
    try:
        factory = import_module("hid").device
    except (ImportError, AttributeError):
        return None
    return cast(HIDDeviceFactory, factory) if callable(factory) else None


def _one_exact_path(result: preflight.PreflightResult) -> bytes | str | None:
    """Return one opaque hidapi path only after physical-interface classification."""
    if result.status != "exactly one matching candidate found":
        return None
    paths = {
        candidate.get("path")
        for candidate in result.candidates
        if isinstance(candidate.get("path"), (bytes, str)) and candidate.get("path")
    }
    if len(paths) != 1:
        return None
    return cast(bytes | str, next(iter(paths)))


def _error_detail(operation: str, error: Exception) -> str:
    """Format one actionable error without exposing HID report contents."""
    return f"{operation}: {type(error).__name__}: {error}"


def _preflight_detail(result: preflight.PreflightResult) -> str:
    """Keep enumeration failure context while preserving strict refusal."""
    return result.status if result.detail is None else f"{result.status}: {result.detail}"


def probe(
    seconds: int,
    *,
    preflight_check: PreflightCheck = preflight.preflight,
    device_factory: HIDDeviceFactory | None = None,
    import_module: Callable[[str], Any] = importlib.import_module,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> ProbeResult:
    """Read events for an explicit finite interval, with no output HID operations.

    Discovery is completed before every open attempt, and a failed discovery is
    refused before constructing a hidapi device object. Transient open, setup,
    or read failures get at most two retries, each with fresh enumeration and a
    fresh device object. A device that was constructed is closed on every path.
    This function is intended to be exercised with fakes in tests; calling it
    against real hardware remains a separate physical verification.
    """
    validate_seconds(seconds)
    events: list[Event] = []
    unknown_reports = 0
    factory: HIDDeviceFactory | None = None
    attempts = 0
    last_error: str | None = None
    deadline = clock() + seconds

    while attempts < _MAX_OPEN_ATTEMPTS:
        attempts += 1
        discovery = preflight_check()
        path = _one_exact_path(discovery)
        if path is None:
            return ProbeResult(
                "preflight refused",
                tuple(events),
                unknown_reports,
                detail=_preflight_detail(discovery),
                attempts=attempts,
            )

        if factory is None:
            factory = device_factory or _hidapi_device_factory(import_module)
            if factory is None:
                return ProbeResult("hidapi unavailable", attempts=attempts)

        device: ReadOnlyHIDDevice | None = None
        attempt_error: str | None = None
        operation = "construct device"
        try:
            device = factory()
            operation = "open path"
            device.open_path(path)
            operation = "enable nonblocking reads"
            device.set_nonblocking(1)
            operation = "read"
            while clock() < deadline:
                report = device.read(_READ_SIZE)
                if report:
                    try:
                        raw = bytes(report)
                    except (TypeError, ValueError):
                        unknown_reports += 1
                    else:
                        event = decode_event(raw)
                        if event is None:
                            unknown_reports += 1
                        else:
                            events.append(event)
                sleep(_POLL_INTERVAL_SECONDS)
        except Exception as error:
            attempt_error = _error_detail(f"{operation} failed", error)
        finally:
            if device is not None:
                try:
                    device.close()
                except Exception as error:
                    close_error = _error_detail("close failed", error)
                    attempt_error = attempt_error or close_error

        if attempt_error is None:
            return ProbeResult("completed", tuple(events), unknown_reports, attempts=attempts)
        last_error = attempt_error
        if attempts < _MAX_OPEN_ATTEMPTS:
            sleep(_RETRY_BACKOFF_SECONDS)

    return ProbeResult(
        "device error",
        tuple(events),
        unknown_reports,
        detail=f"after {attempts} attempts: {last_error}",
        attempts=attempts,
    )


def _format_event(event: Event) -> str:
    pressed = "none" if event.pressed is None else str(event.pressed).lower()
    delta = "none" if event.delta is None else str(event.delta)
    return (
        f"event control={event.control} kind={event.kind} "
        f"press={pressed} delta={delta} length={len(event.raw)}"
    )


def write_report(result: ProbeResult, output: TextIO) -> None:
    """Write sanitized results; raw HID report content is never printed."""
    print(f"AKP03 macOS read-only probe: {result.status}.", file=output)
    if result.detail:
        print(f"Detail: {result.detail}", file=output)
    if result.attempts:
        print(f"Attempts: {result.attempts}.", file=output)
    for event in result.events:
        print(_format_event(event), file=output)
    print(f"Unknown reports: {result.unknown_reports}.", file=output)
    print(
        "Read-only probe only: it does not verify macOS event controls until a physical run is reviewed.",
        file=output,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="akp03-macos-probe",
        description="Bounded read-only AKP03 macOS HID event probe; never sends HID output.",
    )
    parser.add_argument(
        "--seconds",
        required=True,
        type=_parse_seconds,
        metavar="N",
        help=f"finite probe duration ({_MIN_SECONDS}-{_MAX_SECONDS} seconds)",
    )
    args = parser.parse_args(argv)
    result = probe(args.seconds)
    write_report(result, sys.stdout)
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
