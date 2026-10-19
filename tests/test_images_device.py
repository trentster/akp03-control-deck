from __future__ import annotations

from io import BytesIO

from PIL import Image

from akp03.device import decode_event
from akp03.images import to_jpeg


def report(code: int, state: int = 1) -> bytes:
    return b"ACK" + b"\0" * 6 + bytes([code, state])


def test_all_18_control_codes() -> None:
    expected = {
        **{code: ("key", f"key{code - 1}", True, None) for code in range(0x01, 0x07)},
        0x25: ("button", "button6", True, None),
        0x31: ("button", "button7", True, None),
        0x30: ("button", "button8", True, None),
        # Hardware map: top is 0x51/0x50, then bottom-left 0x91/0x90,
        # then bottom-right 0x61/0x60. Avoid ambiguous visual labels.
        0x51: ("knob_turn", "top", None, 1),
        0x50: ("knob_turn", "top", None, -1),
        0x91: ("knob_turn", "bottom_left", None, 1),
        0x90: ("knob_turn", "bottom_left", None, -1),
        0x61: ("knob_turn", "bottom_right", None, 1),
        0x60: ("knob_turn", "bottom_right", None, -1),
        0x35: ("knob_press", "top", True, None),
        0x33: ("knob_press", "bottom_left", True, None),
        0x34: ("knob_press", "bottom_right", True, None),
    }
    assert len(expected) == 18
    for code, fields in expected.items():
        event = decode_event(report(code))
        assert event is not None
        assert (event.kind, event.control, event.pressed, event.delta) == fields
    assert decode_event(report(99)) is None
    assert decode_event(b"bad") is None


def test_jpeg_dimensions_letterbox_preserves_orientation_and_input_forms(tmp_path) -> None:
    source = Image.new("RGB", (120, 60), "red")
    raw = to_jpeg(source)
    with Image.open(BytesIO(raw)) as encoded:
        assert encoded.size == (60, 60)
        assert encoded.getpixel((30, 30))[0] > 200  # Source remains horizontal.
        assert encoded.getpixel((30, 0))[0] < 20  # letterbox black edge.
    source.save(tmp_path / "input.png")
    assert Image.open(BytesIO(to_jpeg(tmp_path / "input.png"))).size == (60, 60)
    assert Image.open(BytesIO(to_jpeg(raw))).size == (60, 60)
