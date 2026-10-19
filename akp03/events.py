"""Pure AKP03 input-event decoding shared by hardware adapters."""
from __future__ import annotations

from dataclasses import dataclass

from . import protocol

# The media functions a keyboard sends.  They are canonical key tokens - the same
# vocabulary a shortcut uses - and it is the *backend* that decides which event
# family carries one: macOS delivers a media key as a system-defined event rather
# than a keycode, and Wayland as an X keysym.  Naming the function key printed
# beside them (F7-F12) looks right and does nothing, because a synthetic F12
# reaches macOS as the plain function key.
MEDIA_KEYS: tuple[str, ...] = ("mute", "sound-down", "sound-up", "play-pause", "next", "previous")


@dataclass(frozen=True)
class Event:
    """One recognized AKP03 input event."""

    kind: str
    control: str
    pressed: bool | None
    delta: int | None
    raw: bytes


_CONTROLS: dict[int, tuple[str, str, int | None]] = {
    **{code: ("key", f"key{code - 1}", None) for code in range(0x01, 0x07)},
    # Physical numbering and raw hexadecimal codes are from the Phase 1 map.
    0x25: ("button", "button6", None),
    0x31: ("button", "button7", None),
    0x30: ("button", "button8", None),
    0x51: ("knob_turn", "top", 1),
    0x50: ("knob_turn", "top", -1),
    0x91: ("knob_turn", "bottom_left", 1),
    0x90: ("knob_turn", "bottom_left", -1),
    0x61: ("knob_turn", "bottom_right", 1),
    0x60: ("knob_turn", "bottom_right", -1),
    0x35: ("knob_press", "top", None),
    0x33: ("knob_press", "bottom_left", None),
    0x34: ("knob_press", "bottom_right", None),
}


def decode_event(raw: bytes) -> Event | None:
    """Decode a recognized vendor input event without any HID I/O."""
    parsed = protocol.parse_event(raw)
    if parsed is None or parsed.code not in _CONTROLS:
        return None
    kind, control, delta = _CONTROLS[parsed.code]
    return Event(kind, control, None if delta is not None else bool(parsed.state), delta, parsed.raw)


def turn_binding_name(control: str, delta: int) -> str:
    """The name a dial turn is filed under.

    Each way a dial turns gets a slot of its own, because a turn is not a press:
    the deck reports one event per notch and the direction it went.  A turn also
    carries no release, so nothing can be held on one.
    """
    return f"{control}_turn_{'right' if delta > 0 else 'left'}"


def binding_name(event: Event) -> str:
    """The name a control event's binding is looked up by.

    A press is filed under the control itself; a turn is filed under the way it
    turned, so assigning something to a dial's press never grabs its turns.
    """
    if event.kind == "knob_turn" and event.delta is not None:
        return turn_binding_name(event.control, event.delta)
    return event.control
