from __future__ import annotations

from collections.abc import Sequence
from io import StringIO

import pytest

from akp03 import macos_probe, preflight


PATH = b"one-physical-akp03"
CANDIDATE = {
    "vendor_id": 0x0300,
    "product_id": 0x3002,
    "usage_page": 0xFFA0,
    "path": PATH,
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += 1.0


class FakeDevice:
    """Only the read-only hidapi surface accepted by the probe."""

    def __init__(self, reports: list[Sequence[int] | bytes] | None = None) -> None:
        self.reports = list(reports or [])
        self.opened_paths: list[bytes | str] = []
        self.nonblocking: list[int] = []
        self.read_sizes: list[int] = []
        self.closed = False

    def open_path(self, path: bytes | str) -> None:
        self.opened_paths.append(path)

    def set_nonblocking(self, nonblocking: int) -> None:
        self.nonblocking.append(nonblocking)

    def read(self, size: int) -> Sequence[int] | bytes:
        self.read_sizes.append(size)
        return self.reports.pop(0) if self.reports else []

    def close(self) -> None:
        self.closed = True


def discovery(*candidates: object) -> preflight.PreflightResult:
    return preflight.preflight_from_enumeration(candidates)


def test_wrong_preflight_refuses_before_constructing_a_device() -> None:
    called = False

    def never_construct() -> FakeDevice:
        nonlocal called
        called = True
        return FakeDevice()

    result = macos_probe.probe(
        1,
        preflight_check=lambda: discovery({**CANDIDATE, "vendor_id": 0x1234}),
        device_factory=never_construct,
    )

    assert result.status == "preflight refused"
    assert result.detail == "no matching AKP03 vendor interface found"
    assert not called


def test_ambiguous_preflight_refuses_before_constructing_a_device() -> None:
    called = False

    def never_construct() -> FakeDevice:
        nonlocal called
        called = True
        return FakeDevice()

    result = macos_probe.probe(
        1,
        preflight_check=lambda: discovery(CANDIDATE, {**CANDIDATE, "path": b"other"}),
        device_factory=never_construct,
    )

    assert result.status == "preflight refused"
    assert result.detail == "ambiguous candidates"
    assert not called


def test_exactly_one_physical_path_opens_nonblocking_and_closes() -> None:
    device = FakeDevice()
    clock = FakeClock()

    result = macos_probe.probe(
        1,
        preflight_check=lambda: discovery(CANDIDATE, {**CANDIDATE, "usage": 2}),
        device_factory=lambda: device,
        clock=clock,
        sleep=clock.sleep,
    )

    assert result.succeeded
    assert device.opened_paths == [PATH]
    assert device.nonblocking == [1]
    assert device.read_sizes == [1024]
    assert device.closed
    assert result.attempts == 1
    assert not hasattr(device, "write")
    assert not hasattr(device, "send_feature_report")


def test_known_events_are_decoded_and_unknown_reports_are_counted_without_dumping() -> None:
    known = b"ACK" + b"\0" * 6 + bytes([0x51, 0])
    unknown = b"private-content-must-not-be-printed"
    device = FakeDevice([known, unknown])
    clock = FakeClock()

    result = macos_probe.probe(
        2,
        preflight_check=lambda: discovery(CANDIDATE),
        device_factory=lambda: device,
        clock=clock,
        sleep=clock.sleep,
    )
    output = StringIO()
    macos_probe.write_report(result, output)

    assert result.succeeded
    assert len(result.events) == 1
    assert result.events[0].control == "top"
    assert result.events[0].kind == "knob_turn"
    assert result.events[0].delta == 1
    assert result.unknown_reports == 1
    report = output.getvalue()
    assert "event control=top kind=knob_turn press=none delta=1 length=11" in report
    assert "Unknown reports: 1." in report
    assert unknown.decode() not in report
    assert "does not verify macOS event controls until a physical run is reviewed" in report
    assert device.closed


def test_read_errors_retry_with_fresh_devices_and_reenumeration() -> None:
    class FailingDevice(FakeDevice):
        def read(self, size: int) -> Sequence[int] | bytes:
            raise OSError("read unplugged")

    devices = [FailingDevice(), FailingDevice(), FailingDevice()]
    constructed: list[FailingDevice] = []
    preflight_calls = 0
    sleep_calls: list[float] = []

    def reenumerate() -> preflight.PreflightResult:
        nonlocal preflight_calls
        preflight_calls += 1
        return discovery(CANDIDATE)

    def construct() -> macos_probe.ReadOnlyHIDDevice:
        device = devices.pop(0)
        constructed.append(device)
        return device

    result = macos_probe.probe(
        1,
        preflight_check=reenumerate,
        device_factory=construct,
        clock=lambda: 0.0,
        sleep=sleep_calls.append,
    )

    assert result.status == "device error"
    assert result.attempts == 3
    assert result.detail == "after 3 attempts: read failed: OSError: read unplugged"
    assert preflight_calls == 3
    assert not devices
    assert len({id(device) for device in constructed}) == 3
    assert all(device.closed for device in constructed)
    assert sleep_calls == [macos_probe._RETRY_BACKOFF_SECONDS] * 2


def test_failed_preflight_never_constructs_a_device_even_after_a_retry() -> None:
    devices = [FakeDevice()]
    preflight_results = iter(
        [
            discovery(CANDIDATE),
            discovery({**CANDIDATE, "path": b"ambiguous"}, CANDIDATE),
        ]
    )

    def construct() -> macos_probe.ReadOnlyHIDDevice:
        return devices.pop(0)

    class OpenFailingDevice(FakeDevice):
        def open_path(self, path: bytes | str) -> None:
            super().open_path(path)
            raise OSError("open failed")

    first = OpenFailingDevice()
    factory_calls = 0

    def retry_factory() -> macos_probe.ReadOnlyHIDDevice:
        nonlocal factory_calls
        factory_calls += 1
        return first if factory_calls == 1 else construct()

    result = macos_probe.probe(
        1,
        preflight_check=lambda: next(preflight_results),
        device_factory=retry_factory,
        clock=lambda: 0.0,
        sleep=lambda _duration: None,
    )

    assert result.status == "preflight refused"
    assert result.detail == "ambiguous candidates"
    assert result.attempts == 2
    assert factory_calls == 1
    assert first.closed
    assert len(devices) == 1


def test_success_does_not_retry_after_one_preflight_and_device() -> None:
    device = FakeDevice()
    clock = FakeClock()
    preflight_calls = 0
    factory_calls = 0

    def check() -> preflight.PreflightResult:
        nonlocal preflight_calls
        preflight_calls += 1
        return discovery(CANDIDATE)

    def construct() -> macos_probe.ReadOnlyHIDDevice:
        nonlocal factory_calls
        factory_calls += 1
        return device

    result = macos_probe.probe(
        1,
        preflight_check=check,
        device_factory=construct,
        clock=clock,
        sleep=clock.sleep,
    )

    assert result.succeeded
    assert result.attempts == 1
    assert preflight_calls == 1
    assert factory_calls == 1


@pytest.mark.parametrize("seconds", [0, 31, -1, True, 1.5, "1"])
def test_duration_validation_is_finite_and_strict(seconds: object) -> None:
    with pytest.raises(ValueError, match="integer from 1 through 30"):
        macos_probe.validate_seconds(seconds)


def test_cli_requires_a_finite_duration_without_running_probe() -> None:
    with pytest.raises(SystemExit) as missing:
        macos_probe.main([])
    with pytest.raises(SystemExit) as invalid:
        macos_probe.main(["--seconds", "0"])

    assert missing.value.code == 2
    assert invalid.value.code == 2
