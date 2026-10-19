"""Linux hidraw transport for the AKP03 vendor HID interface."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Final

VID: Final = "00000300"
PID: Final = "00003002"
VENDOR_USAGE: Final = b"\x06\xa0\xff"
REPORT_ID: Final = b"\x00"


class TransportError(OSError):
    pass


class HidrawTransport:
    """A checked hidraw connection. Protocol payloads are always 1024 bytes."""

    def __init__(self, path: str = "/dev/hidraw0") -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise TransportError("transport is not open")
        return self._fd

    def _sys_device(self) -> Path:
        return Path("/sys/class/hidraw") / Path(self.path).name / "device"

    def _validate(self) -> None:
        sys_device = self._sys_device()
        try:
            descriptor = (sys_device / "report_descriptor").read_bytes()
            uevent = (sys_device / "uevent").read_text()
        except OSError as error:
            raise TransportError(f"cannot inspect {self.path}: {error}") from error
        if VENDOR_USAGE not in descriptor:
            raise TransportError(f"{self.path} is not the 0xffa0 vendor interface")
        if f"HID_ID=0003:{VID}:{PID}" not in uevent:
            raise TransportError(f"{self.path} is not AKP03 VID:PID 0300:3002")

    def open(self) -> "HidrawTransport":
        if self._fd is None:
            self._validate()
            self._fd = os.open(self.path, os.O_RDWR | os.O_CLOEXEC)
        return self

    def write(self, payload: bytes) -> None:
        if len(payload) != 1024:
            raise ValueError("AKP03 protocol payloads must be exactly 1024 bytes")
        packet = REPORT_ID + payload
        written = os.write(self.fd, packet)
        if written != len(packet):
            raise TransportError(f"short write to {self.path}: {written}/{len(packet)} bytes")

    def read(self, size: int = 512) -> bytes:
        return os.read(self.fd, size)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "HidrawTransport":
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()
