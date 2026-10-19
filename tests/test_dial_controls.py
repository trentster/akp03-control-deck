"""The three dials: a press, and each way they turn.

A dial is not one control.  The deck reports a press like any other button, and a
turn as one event per notch that carries which way it went, so each way a dial turns
is a slot of its own: assigning something to a dial's press can never grab its turns.
A turn also has no release, so nothing can be held on one - there is nothing to wait
for, the notch is over the moment it happens.

While a menu is open the menu owns the big dial and the left dial (its highlight and
its way back), and the right dial stays the deck's.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from akp03.daemon import Daemon
from akp03.device import Event
from akp03.events import binding_name, turn_binding_name
from akp03.model import (
    DIAL_CONTROLS,
    DIAL_SLOTS,
    DIAL_TURN_WAYS,
    RESERVED_CONTROLS,
    ActionDefinition,
    ControlPlaneEditorError,
    validate_assignments,
)
from akp03.webui import draft_from_config, draft_payload, merge_draft_into_config, parse_draft


# A deck whose big dial presses to a shortcut and turns to two volume keys, and
# whose two small dials are left as they came out of the box.
DECK = '''[actions.louder]
action = "keys"
keys = ["f12"]

[actions.quieter]
action = "keys"
keys = ["f11"]

[actions.spotlight]
action = "keys"
keys = ["cmd", "space"]

[actions.open_menu]
action = "menu"
menu = "youtube"

[bindings.key0]
action_id = "open_menu"

[bindings.top]
action_id = "spotlight"

[bindings.top_turn_right]
action_id = "louder"

[bindings.top_turn_left]
action_id = "quieter"

[custom_menus.youtube]
root = "home"

[[custom_menus.youtube.pages]]
id = "home"
title = "YouTube"
items = [{ key = 0, label = "Rewind", action = "spotlight" }]
'''

# The same deck, with the two small dials turned as well.
SMALL_DIALS = '''
[bindings.bottom_left_turn_right]
action_id = "louder"

[bindings.bottom_right_turn_right]
action_id = "quieter"
'''

ACTIONS = [
    ActionDefinition("louder", "keys", ("f12",)),
    ActionDefinition("quieter", "keys", ("f11",)),
    ActionDefinition("spotlight", "keys", ("cmd", "space")),
]


class Backend:
    """A keyboard that records instead of typing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def tap(self, keys): self.calls.append(("tap", tuple(keys)))
    def press(self, keys): self.calls.append(("press", tuple(keys)))
    def release(self, keys): self.calls.append(("release", tuple(keys)))


class Deck:
    def __init__(self, _path: str) -> None:
        self.calls: list[tuple] = []

    def on_event(self, callback): self.event = callback
    def on_disconnect(self, callback): self.disconnect = callback
    def open(self): return self
    def close(self): pass
    def clear(self, key): self.calls.append(("clear", key))
    def set_key_image(self, key, image): self.calls.append(("image", key))
    def set_brightness(self, level): self.calls.append(("brightness", level))


def _config(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def _daemon(tmp_path: Path, text: str) -> tuple[Daemon, Deck, Backend]:
    path = tmp_path / "config.toml"; _config(path, text)
    deck, backend = Deck("/dev/fake"), Backend()
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck, backend=backend)
    assert daemon.reload() and daemon.connect()
    return daemon, deck, backend


def test_every_dial_carries_a_press_and_a_slot_for_each_way_it_turns() -> None:
    assert DIAL_CONTROLS == ("top", "bottom_left", "bottom_right")
    assert DIAL_TURN_WAYS == ("left", "right")
    # The press first, then how it turns, for each dial in turn.
    assert DIAL_SLOTS[:3] == ("top", "top_turn_left", "top_turn_right")
    # The left dial's press is nobody's to set: it is always the way back out of a menu.
    assert RESERVED_CONTROLS == ("bottom_left",)
    assert "bottom_left" not in DIAL_SLOTS
    assert DIAL_SLOTS[3:5] == ("bottom_left_turn_left", "bottom_left_turn_right")
    assert len(DIAL_SLOTS) == 8 and len(set(DIAL_SLOTS)) == 8
    assert turn_binding_name("bottom_right", 1) == "bottom_right_turn_right"
    assert turn_binding_name("bottom_right", -1) == "bottom_right_turn_left"
    # A press is filed under the control; a turn under the way it went.
    assert binding_name(Event("knob_press", "top", True, None, b"")) == "top"
    assert binding_name(Event("knob_turn", "top", None, 1, b"")) == "top_turn_right"
    assert binding_name(Event("knob_turn", "top", None, -1, b"")) == "top_turn_left"


def test_a_dial_runs_its_press_and_each_way_it_turns_separately(tmp_path: Path) -> None:
    daemon, _deck, backend = _daemon(tmp_path, DECK)

    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert backend.calls[-1] == ("tap", ("f12",)), "turning right runs the right-hand action"

    daemon.event(Event("knob_turn", "top", None, 1, b""))
    daemon.event(Event("knob_turn", "top", None, -1, b""))
    assert backend.calls[-2:] == [("tap", ("f12",)), ("tap", ("f11",))]

    # The press is its own slot: it never fires on a turn, and a turn never fires on it.
    daemon.event(Event("knob_press", "top", True, None, b""))
    assert backend.calls[-1] == ("tap", ("cmd", "space"))
    assert backend.calls.count(("tap", ("cmd", "space"))) == 1

    # A turn nobody assigned does nothing at all, quietly.
    before = len(backend.calls)
    daemon.event(Event("knob_turn", "bottom_right", None, 1, b""))
    assert len(backend.calls) == before


def test_a_turn_cannot_be_held(tmp_path: Path) -> None:
    assignments = {"top": "louder", "top_turn_right": "louder"}
    # A keys action may be held on a press...
    validate_assignments(assignments, ACTIONS, ["top"])
    # ...but a notch is over the moment it happens: there is no release to wait for.
    with pytest.raises(ControlPlaneEditorError, match="cannot be held"):
        validate_assignments(assignments, ACTIONS, ["top_turn_right"])


def test_an_unknown_control_cannot_be_held() -> None:
    with pytest.raises(ControlPlaneEditorError, match="not an assignable control"):
        validate_assignments({"top": "louder"}, ACTIONS, ["bottom_middle"])


def test_a_hand_written_dial_binding_survives_a_save(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, DECK)

    draft = draft_from_config(path)
    # The dials ride along in the browser's document, in deck order.
    assert len(draft.dials) == len(DIAL_SLOTS)
    assert draft.dials[0] == "spotlight"
    assert draft.dials[2] == "louder"

    round_tripped = parse_draft(draft_payload(draft))
    assert round_tripped.dials == draft.dials

    text = merge_draft_into_config(DECK, round_tripped)
    written = tomllib.loads(text)
    # The three dial slots are still bound, still to the same keys.
    assert written["bindings"]["top_turn_right"]["action_id"] == "louder"
    assert written["bindings"]["top"]["action_id"] == "spotlight"
    for slot in ("top", "top_turn_left", "top_turn_right"):
        action_id = written["bindings"][slot]["action_id"]
        assert written["actions"][action_id]["action"] == "keys"


def test_a_client_that_says_nothing_about_the_dials_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, DECK)
    payload = draft_payload(draft_from_config(path))
    del payload["dials"]

    # Refused rather than treated as "no dials": the dials' own tables are the
    # editor's to write now, so silence must not blank what is written there.
    with pytest.raises(ControlPlaneEditorError, match="unsupported fields"):
        parse_draft(payload)


def test_the_left_dials_press_is_always_the_way_back(tmp_path: Path) -> None:
    daemon, _deck, backend = _daemon(tmp_path, DECK + SMALL_DIALS + '\n[bindings.bottom_left]\naction_id = "louder"\n')

    # With no menu open the left dial is the deck's like any other control.
    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    assert backend.calls[-1] == ("tap", ("f12",))

    daemon.event(Event("key", "key0", True, None, b""))
    assert daemon.custom_menu is not None
    before = len(backend.calls)

    # Inside a menu the press is the way back, whatever the deck has on that dial: the
    # menu owns it and the deck's own action stays out of the way.  The menu in this
    # deck is one page deep, so Back leaves it.
    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    assert len(backend.calls) == before, "the deck's own action on the left dial stayed out of the way"
    assert daemon.custom_menu is None, "the press went back out of the menu"


def test_a_menu_owns_the_left_dial_and_leaves_the_right_one_to_the_deck(tmp_path: Path) -> None:
    daemon, _deck, backend = _daemon(tmp_path, DECK + SMALL_DIALS)

    # With no menu open the small dials are the deck's.
    daemon.event(Event("knob_turn", "bottom_left", None, 1, b""))
    assert backend.calls[-1] == ("tap", ("f12",))
    daemon.event(Event("knob_turn", "bottom_right", None, 1, b""))
    assert backend.calls[-1] == ("tap", ("f11",))

    daemon.event(Event("key", "key0", True, None, b""))
    assert daemon.custom_menu is not None

    # Inside the menu the left dial is the menu's: it turns to nothing, and the
    # deck's own action on it stays out of the way.
    before = len(backend.calls)
    daemon.event(Event("knob_turn", "bottom_left", None, 1, b""))
    assert len(backend.calls) == before

    # The right dial is still the deck's.
    daemon.event(Event("knob_turn", "bottom_right", None, 1, b""))
    assert backend.calls[-1] == ("tap", ("f11",))

    # The big dial's turn is unclaimed here, so the deck's own job on it still runs:
    # cycling the highlight only happens when nothing at all is bound to that turn.
    before = len(backend.calls)
    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert len(backend.calls) == before + 1, "the deck's own big-dial turn still ran"
    assert backend.calls[-1] == ("tap", ("f12",))
