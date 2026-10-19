from __future__ import annotations

import pytest

from akp03 import protocol


def assert_packet(packet: bytes, beginning: bytes) -> None:
    assert len(packet) == 1024
    assert packet.startswith(beginning)
    assert packet[len(beginning) :] == b"\0" * (1024 - len(beginning))


def test_command_builders() -> None:
    assert_packet(protocol.wake(), b"CRT\0\0DIS")
    assert_packet(protocol.brightness(1), b"CRT\0\0LIG\0\0\x01")
    assert_packet(protocol.brightness(100), b"CRT\0\0LIG\0\0d")
    assert_packet(protocol.keepalive(), b"CRT\0\0CONNECT")
    assert_packet(protocol.clear_key(0), b"CRT\0\0CLE\0\0\0\x01")
    assert_packet(protocol.clear_key(5), b"CRT\0\0CLE\0\0\0\x06")
    assert_packet(protocol.clear_all(), b"CRT\0\0CLE\0\0\0\xff")
    assert_packet(protocol.flush(), b"CRT\0\0STP")
    assert_packet(protocol.image_header(3, 0x1234), b"CRT\0\0BAT\0\0\x124\x04")


@pytest.mark.parametrize("value", [-1, 101, 1.5])
def test_invalid_brightness(value: object) -> None:
    with pytest.raises(ValueError, match="1 through 100"):
        protocol.brightness(value)  # type: ignore[arg-type]


def test_brightness_zero_is_rejected_without_a_packet() -> None:
    with pytest.raises(ValueError, match="ignored by AKP03E firmware"):
        protocol.brightness(0)


@pytest.mark.parametrize("key", [-1, 6, "1"])
def test_invalid_key(key: object) -> None:
    with pytest.raises(ValueError):
        protocol.clear_key(key)  # type: ignore[arg-type]


def test_jpeg_chunks_and_limits() -> None:
    data = bytes(range(256)) * 5
    chunks = protocol.jpeg_chunks(data)
    assert len(chunks) == 2
    assert chunks[0] == data[:1024]
    assert chunks[1].startswith(data[1024:])
    assert len(chunks[1]) == 1024
    assert protocol.jpeg_chunks(b"") == ()
    with pytest.raises(ValueError):
        protocol.jpeg_chunks(b"x" * 65536)


def test_parse_event() -> None:
    report = b"ACK" + b"\0" * 6 + bytes([51, 1])
    parsed = protocol.parse_event(report)
    assert parsed == protocol.RawEvent(51, 1, report)
    assert protocol.parse_event(b"ACK" + b"\0" * 7) is None
    assert protocol.parse_event(b"NOPE" + b"\0" * 20) is None
