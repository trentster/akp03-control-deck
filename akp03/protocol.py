"""Pure packet construction and vendor event decoding."""
from __future__ import annotations

from dataclasses import dataclass

PAYLOAD_SIZE = 1024
PREFIX = b"CRT\x00\x00"


@dataclass(frozen=True)
class RawEvent:
    """An ACK event emitted by the vendor HID interface."""

    code: int
    state: int
    raw: bytes


def _payload(command: bytes) -> bytes:
    if len(command) > PAYLOAD_SIZE:
        raise ValueError(f"command is {len(command)} bytes; maximum is {PAYLOAD_SIZE}")
    return command.ljust(PAYLOAD_SIZE, b"\x00")


def _key_byte(key: int) -> int:
    if not isinstance(key, int) or not 0 <= key <= 5:
        raise ValueError("key must be an integer from 0 through 5")
    return key + 1


def wake() -> bytes:
    return _payload(PREFIX + b"DIS")


def brightness(level: int) -> bytes:
    if not isinstance(level, int) or not 1 <= level <= 100:
        if level == 0:
            raise ValueError("brightness 0 is ignored by AKP03E firmware; use 1-100, or clear keys for dark")
        raise ValueError("brightness must be an integer from 1 through 100")
    return _payload(PREFIX + b"LIG\x00\x00" + bytes([level]))


def keepalive() -> bytes:
    return _payload(PREFIX + b"CONNECT")


def clear_key(key: int) -> bytes:
    return _payload(PREFIX + b"CLE\x00\x00\x00" + bytes([_key_byte(key)]))


def clear_all() -> bytes:
    return _payload(PREFIX + b"CLE\x00\x00\x00\xff")


def flush() -> bytes:
    return _payload(PREFIX + b"STP")


def image_header(key: int, jpeg_length: int) -> bytes:
    if not isinstance(jpeg_length, int) or not 0 <= jpeg_length <= 0xFFFF:
        raise ValueError("JPEG length must be an integer from 0 through 65535")
    return _payload(PREFIX + b"BAT\x00\x00" + jpeg_length.to_bytes(2, "big") + bytes([_key_byte(key)]))


def jpeg_chunks(jpeg: bytes) -> tuple[bytes, ...]:
    if not isinstance(jpeg, bytes):
        raise TypeError("jpeg must be bytes")
    if len(jpeg) > 0xFFFF:
        raise ValueError("JPEG data exceeds 65535 bytes")
    return tuple(_payload(jpeg[offset : offset + PAYLOAD_SIZE]) for offset in range(0, len(jpeg), PAYLOAD_SIZE))


def parse_event(report: bytes) -> RawEvent | None:
    """Parse an ACK report, returning None for malformed or unrelated reports."""
    if len(report) <= 10 or report[:3] != b"ACK":
        return None
    return RawEvent(code=report[9], state=report[10], raw=bytes(report))
