"""Read-only AKP03 vendor-interface discovery for macOS readiness checks.

This module deliberately performs enumeration only.  It never opens a HID path,
reads a report, writes a report, changes configuration, or runs a shell command.
The Linux hidraw transport remains the only driver implementation.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TextIO, cast

AKP03_VENDOR_ID: Final = 0x0300
AKP03_PRODUCT_ID: Final = 0x3002
AKP03_VENDOR_USAGE_PAGE: Final = 0xFFA0

HIDEnumerator = Callable[[], Iterable[object]]


@dataclass(frozen=True)
class PreflightResult:
    """The bounded result of one enumeration-only readiness check."""

    status: str
    candidates: tuple[Mapping[str, object], ...] = ()
    detail: str | None = None


def _integer(value: object) -> int | None:
    """Accept normal hidapi integer fields and conservative numeric strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def is_akp03_vendor_interface(device: object) -> bool:
    """Return whether one hidapi enumeration entry is the AKP03 vendor HID interface.

    ``usage_page`` is intentionally mandatory.  A missing value is not evidence
    that a generic HID collection is safe to select.
    """
    if not isinstance(device, Mapping):
        return False
    return (
        _integer(device.get("vendor_id")) == AKP03_VENDOR_ID
        and _integer(device.get("product_id")) == AKP03_PRODUCT_ID
        and "usage_page" in device
        and _integer(device.get("usage_page")) == AKP03_VENDOR_USAGE_PAGE
    )


def matching_candidates(devices: Iterable[object]) -> tuple[Mapping[str, object], ...]:
    """Filter hidapi entries without opening or otherwise selecting a device."""
    return tuple(device for device in devices if is_akp03_vendor_interface(device))  # type: ignore[misc]


def _candidate_identity(candidate: Mapping[str, object], index: int) -> object:
    """Group hidapi collections belonging to the same physical interface by path.

    macOS commonly reports separate usage collections for one HID interface with
    the same opaque path. A missing path stays distinct rather than being guessed.
    """
    path = candidate.get("path")
    if isinstance(path, (str, bytes)) and path:
        return ("path", path)
    return ("unidentified", index)


def classify_candidates(candidates: Iterable[Mapping[str, object]]) -> PreflightResult:
    """Classify candidate collections by distinct physical HID interface."""
    matched = tuple(candidates)
    identities = {_candidate_identity(candidate, index) for index, candidate in enumerate(matched)}
    if not matched:
        return PreflightResult("no matching AKP03 vendor interface found")
    if len(identities) == 1:
        return PreflightResult("exactly one matching candidate found", matched)
    return PreflightResult("ambiguous candidates", matched)


def preflight_from_enumeration(devices: Iterable[object]) -> PreflightResult:
    """Perform the pure, unit-testable part of preflight classification."""
    return classify_candidates(matching_candidates(devices))


def _hidapi_enumerator(import_module: Callable[[str], Any] = importlib.import_module) -> HIDEnumerator | None:
    """Load optional hidapi lazily; importing this module never requires hidapi."""
    try:
        hid = import_module("hid")
        enumerate_devices = hid.enumerate
    except (ImportError, AttributeError):
        return None
    if not callable(enumerate_devices):
        return None
    return cast(HIDEnumerator, enumerate_devices)


def preflight(
    *,
    enumerate_devices: HIDEnumerator | None = None,
    import_module: Callable[[str], Any] = importlib.import_module,
) -> PreflightResult:
    """Enumerate only, returning a bounded readiness result without HID I/O."""
    enumerator = enumerate_devices or _hidapi_enumerator(import_module)
    if enumerator is None:
        return PreflightResult("hidapi unavailable")
    try:
        devices = enumerator()
    except Exception as error:
        return PreflightResult("hidapi unavailable", detail=f"enumeration failed: {error}")
    return preflight_from_enumeration(devices)


def _candidate_path(candidate: Mapping[str, object]) -> str:
    path = candidate.get("path")
    if isinstance(path, bytes):
        return path.decode("utf-8", errors="backslashreplace")
    return str(path) if path is not None else "<no path reported>"


def write_report(result: PreflightResult, output: TextIO) -> None:
    """Write human guidance without exposing an operation that could use a device."""
    print(f"AKP03 macOS preflight: {result.status}.", file=output)
    if result.detail:
        print(f"Detail: {result.detail}", file=output)
    for index, candidate in enumerate(result.candidates, start=1):
        print(f"Candidate {index}: path={_candidate_path(candidate)}", file=output)
    print(
        "Discovery only: no HID device was opened, read, written, configured, or sent any data.",
        file=output,
    )
    print(
        "A plugged-in physical test is allowed only after exactly one matching candidate is reported; "
        "the next test must be explicit and non-destructive.",
        file=output,
    )
    print(
        "macOS hardware event/read/write support is unported and unverified. The Linux hidraw driver "
        "does not run on macOS, and this command never uploads, injects keys, or opens a device.",
        file=output,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="akp03-preflight",
        description="Read-only AKP03 macOS HID discovery; does not open or write a device.",
    )
    parser.parse_args(argv)
    result = preflight()
    write_report(result, sys.stdout)
    return 0 if result.status == "exactly one matching candidate found" else 1


if __name__ == "__main__":
    raise SystemExit(main())
