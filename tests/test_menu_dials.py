"""A menu may take the big dial and the right dial while it is open.

A menu that claims nothing keeps the dials it had: the big one turns to move its
highlight and presses to select, and the right one stays the deck's.  A menu that
claims part of the big dial takes that dial: the parts it leaves empty then do nothing,
because moving a highlight the dial no longer owns is a change nobody asked for.

The left dial is not anybody's to claim.  Its press is always the way back out of a
menu, and its turns are nothing while one is open, so a menu that names one is refused
rather than quietly ignored.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from akp03.config import ConfigError, load_config
from akp03.custom_menu import MENU_DIAL_CONTROLS, MENU_DIAL_SLOTS
from akp03.daemon import Daemon
from akp03.device import Event
from akp03.model import ActionDefinition, ControlPlaneEditorError, MenuDefinition, validate_menu_definitions
from akp03.webui import draft_from_config, draft_payload, merge_draft_into_config, parse_draft

# A deck with a YouTube menu on key 1, two volume keys, and the right dial's turn
# assigned on the deck itself.
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

[bindings.bottom_right_turn_right]
action_id = "quieter"

[custom_menus.youtube]
root = "home"

[[custom_menus.youtube.pages]]
id = "home"
title = "YouTube"
items = [{ key = 0, label = "Rewind", action = "spotlight" }, { key = 1, label = "Forward", action = "louder" }]
'''

# The same deck, with the menu taking both ways the big dial turns.
CLAIMED_TURNS = '''
[custom_menus.youtube.dials]
top_turn_right = "louder"
top_turn_left = "quieter"
'''


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


def _daemon(tmp_path: Path, text: str) -> tuple[Daemon, Deck, Backend]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(text)
    path.chmod(0o600)
    deck, backend = Deck("/dev/fake"), Backend()
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck, backend=backend)
    assert daemon.reload() and daemon.connect()
    return daemon, deck, backend


def _open_menu(daemon: Daemon) -> None:
    daemon.event(Event("key", "key0", True, None, b""))
    assert daemon.custom_menu is not None


def _written(tmp_path: Path, text: str) -> Path:
    """A config file the editor can read, because the editor reads files."""
    path = tmp_path / f"config-{abs(hash(text)) % 100000}.toml"
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_only_the_big_dial_and_the_right_dial_are_a_menus_to_claim() -> None:
    # Everything a menu can take belongs to one of those two dials, and the left dial
    # - whose press is the way back - is in neither list.
    assert MENU_DIAL_CONTROLS == ("top", "bottom_right")
    assert set(MENU_DIAL_SLOTS) == {
        "top", "top_turn_left", "top_turn_right",
        "bottom_right", "bottom_right_turn_left", "bottom_right_turn_right",
    }
    assert not [slot for slot in MENU_DIAL_SLOTS if slot.startswith("bottom_left")]


def test_a_menu_that_claims_nothing_leaves_the_big_dial_its_own_job(tmp_path: Path) -> None:
    daemon, _deck, backend = _daemon(tmp_path, DECK)
    _open_menu(daemon)
    before = daemon.custom_menu.state.selected_key

    # Nothing claimed, so the turn moves the highlight and runs no key at all.
    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert backend.calls == []
    assert daemon.custom_menu.state.selected_key != before

    # And the press is the menu's own select, which runs whatever the highlight sits on:
    # the turn above moved it from the first item to the second.
    daemon.event(Event("knob_press", "top", True, None, b""))
    assert backend.calls == [("tap", ("f12",))]


def test_a_menu_that_takes_the_big_dial_runs_its_own_action_on_a_turn(tmp_path: Path) -> None:
    daemon, _deck, backend = _daemon(tmp_path, DECK + CLAIMED_TURNS)
    _open_menu(daemon)
    backend.calls.clear()
    before = daemon.custom_menu.state.selected_key

    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert backend.calls == [("tap", ("f12",))], "the menu's own action, not a moved highlight"
    assert daemon.custom_menu.state.selected_key == before

    backend.calls.clear()
    daemon.event(Event("knob_turn", "top", None, -1, b""))
    assert backend.calls == [("tap", ("f11",))]


def test_a_part_of_the_big_dial_left_empty_does_nothing_rather_than_the_old_job(tmp_path: Path) -> None:
    daemon, _deck, backend = _daemon(tmp_path, DECK + CLAIMED_TURNS)
    _open_menu(daemon)
    backend.calls.clear()
    before = daemon.custom_menu.state.selected_key

    # The menu took the dial, so its press is not the menu's select any more: the
    # highlight is where it was left, and pressing it must not run what it sits on.
    daemon.event(Event("knob_press", "top", True, None, b""))
    assert backend.calls == []
    assert daemon.custom_menu.state.selected_key == before


def test_the_right_dial_stays_the_decks_unless_the_menu_takes_it(tmp_path: Path) -> None:
    daemon, _deck, backend = _daemon(tmp_path, DECK)
    _open_menu(daemon)
    backend.calls.clear()

    # Not claimed: the deck's own binding on that turn still fires inside the menu.
    daemon.event(Event("knob_turn", "bottom_right", None, 1, b""))
    assert backend.calls == [("tap", ("f11",))]

    # Claimed: the menu's action runs instead, and the deck's stays out of the way.
    daemon, _deck, backend = _daemon(tmp_path / "claimed", DECK + """
[custom_menus.youtube.dials]
bottom_right_turn_right = "louder"
""")
    _open_menu(daemon)
    backend.calls.clear()
    daemon.event(Event("knob_turn", "bottom_right", None, 1, b""))
    assert backend.calls == [("tap", ("f12",))]


def test_the_left_dial_is_refused_rather_than_quietly_ignored(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(DECK + '''
[custom_menus.youtube.dials]
bottom_left = "louder"
''')
    with pytest.raises(ConfigError):
        load_config(path)

    # The editor refuses the same slot, and says which are its to claim.
    payload = draft_payload(draft_from_config(_written(tmp_path, DECK)))
    payload["menus"][0]["dials"] = {"bottom_left_turn_left": "louder"}
    with pytest.raises(ControlPlaneEditorError):
        parse_draft(payload)


def test_a_claim_survives_the_round_trip_through_the_editor(tmp_path: Path) -> None:
    text = DECK + CLAIMED_TURNS
    draft = draft_from_config(_written(tmp_path, text))
    assert draft.menus[0].dials == {"top_turn_left": "quieter", "top_turn_right": "louder"}
    assert draft_payload(draft)["menus"][0]["dials"] == {"top_turn_left": "quieter", "top_turn_right": "louder"}

    written = merge_draft_into_config(text, parse_draft(draft_payload(draft)))
    assert "[custom_menus.youtube.dials]" in written
    assert 'top_turn_right = "louder"' in written
    # Written in the order the panel reads them, so the file matches what a person saw.
    assert written.index('top_turn_left = "quieter"') < written.index('top_turn_right = "louder"')
    assert tomllib.loads(written)["custom_menus"]["youtube"]["dials"]["top_turn_right"] == "louder"
    # And the daemon loads what the editor wrote, with the claim intact.
    path = tmp_path / "config.toml"
    path.write_text(written)
    path.chmod(0o600)
    loaded = load_config(path)
    assert loaded.custom_menus["youtube"].dials == {"top_turn_left": "quieter", "top_turn_right": "louder"}


def test_a_menu_can_only_claim_an_action_a_menu_may_run() -> None:
    menus = [MenuDefinition("youtube", "YouTube", (), (None, None, None), {"top_turn_right": "louder"})]
    validate_menu_definitions(menus, [ActionDefinition("louder", "keys", ("f12",))])

    unsafe = [MenuDefinition("youtube", "YouTube", (), (None, None, None), {"top_turn_right": "shell"})]
    with pytest.raises(ControlPlaneEditorError):
        validate_menu_definitions(unsafe, [ActionDefinition("shell", "shell", ("ls",))])

    outside = [MenuDefinition("youtube", "YouTube", (), (None, None, None), {"key0": "louder"})]
    with pytest.raises(ControlPlaneEditorError):
        validate_menu_definitions(outside, [ActionDefinition("louder", "keys", ("f12",))])


# A deck whose own menu - the [menu] tree, not a custom one - holds a YouTube page.
OWN_MENU = '''[actions.louder]
action = "keys"
keys = ["f12"]

[actions.quieter]
action = "keys"
keys = ["f11"]

[actions.open_the_menu]
action = "omarchy_menu"
route = "root"

[bindings.key0]
action_id = "open_the_menu"

[bindings.bottom_right_turn_right]
action_id = "quieter"

[menu]
enabled = true
root = "home"

[[menu.pages]]
id = "home"
title = "YouTube"
items = [
  { label = "Rewind", action = "quieter" },
  { label = "Forward", action = "louder" },
]
'''

OWN_MENU_CLAIMED = OWN_MENU + '''
[menu.dials]
top_turn_right = "louder"
top_turn_left = "quieter"
'''


def _open_own_menu(daemon: Daemon) -> None:
    daemon.event(Event("key", "key0", True, None, b""))
    assert daemon.menu is not None, "the deck's own menu should be open"


def test_the_decks_own_menu_can_take_the_big_dial_turns(tmp_path: Path) -> None:
    """A menu opened to work in should still turn the volume down.

    The deck's own menu used to hold the big dial unconditionally, so a volume binding
    on the deck went dead the moment a menu opened.  It can now name the dial parts it
    wants, exactly as a custom menu can.
    """
    daemon, _deck, backend = _daemon(tmp_path, OWN_MENU_CLAIMED)
    _open_own_menu(daemon)
    backend.calls.clear()
    before = daemon.menu.state.selected_index

    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert backend.calls == [("tap", ("f12",))], "the menu's claimed action, not a moved highlight"
    assert daemon.menu.state.selected_index == before

    backend.calls.clear()
    daemon.event(Event("knob_turn", "top", None, -1, b""))
    assert backend.calls == [("tap", ("f11",))]


def test_the_decks_own_menu_still_walks_its_highlight_when_it_claims_nothing(tmp_path: Path) -> None:
    daemon, _deck, backend = _daemon(tmp_path, OWN_MENU)
    _open_own_menu(daemon)
    backend.calls.clear()
    before = daemon.menu.state.selected_index

    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert backend.calls == [], "unclaimed, so the turn runs nothing"
    assert daemon.menu.state.selected_index != before

    daemon.event(Event("knob_press", "top", True, None, b""))
    assert backend.calls == [("tap", ("f12",))], "the press selects what the highlight sits on"


def test_the_right_dial_stays_the_decks_inside_its_own_menu(tmp_path: Path) -> None:
    daemon, _deck, backend = _daemon(tmp_path, OWN_MENU)
    _open_own_menu(daemon)
    backend.calls.clear()

    # The deck's own menu never touches the right dial, so its binding still fires.
    daemon.event(Event("knob_turn", "bottom_right", None, 1, b""))
    assert backend.calls == [("tap", ("f11",))]


def test_a_claim_on_the_decks_own_menu_survives_a_save_from_the_editor(tmp_path: Path) -> None:
    """The editor owns the [menu] table, so a save must not silently drop a claim.

    The deck's own menu is written back from the loaded config rather than from the
    draft page, which does not show it: without that, setting a dial on the deck's own
    menu and then saving anything at all would quietly undo it.
    """
    text = OWN_MENU_CLAIMED
    written = merge_draft_into_config(text, parse_draft(draft_payload(draft_from_config(_written(tmp_path, text)))))
    assert "[menu.dials]" in written, written
    assert 'top_turn_right = "louder"' in written
    assert tomllib.loads(written)["menu"]["dials"] == {"top_turn_left": "quieter", "top_turn_right": "louder"}

    path = tmp_path / "after-editor.toml"
    path.write_text(written)
    path.chmod(0o600)
    loaded = load_config(path)
    assert loaded.menu_dials == {"top_turn_left": "quieter", "top_turn_right": "louder"}


# The deck's own big dial, set to volume, and nothing about any menu.
DECK_BIG_DIAL = '''
[bindings.top_turn_right]
action_id = "louder"

[bindings.top_turn_left]
action_id = "quieter"
'''


def test_an_unclaimed_menu_leaves_the_decks_own_big_dial_turn_working(tmp_path: Path) -> None:
    """Cycling a highlight is the fallback, not the default.

    A menu that claims nothing must not suspend the deck's own job on the big dial: the
    keys are there to be pushed, so walking a menu by dial only happens when the deck has
    nothing bound to that turn.
    """
    daemon, _deck, backend = _daemon(tmp_path, DECK + DECK_BIG_DIAL)
    _open_menu(daemon)
    backend.calls.clear()
    before = daemon.custom_menu.state.selected_key

    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert backend.calls == [("tap", ("f12",))], "the deck's own volume, not a moved highlight"
    assert daemon.custom_menu.state.selected_key == before

    backend.calls.clear()
    daemon.event(Event("knob_turn", "top", None, -1, b""))
    assert backend.calls == [("tap", ("f11",))]


def test_an_unclaimed_own_menu_leaves_the_decks_own_big_dial_turn_working(tmp_path: Path) -> None:
    daemon, _deck, backend = _daemon(tmp_path, OWN_MENU + DECK_BIG_DIAL)
    _open_own_menu(daemon)
    backend.calls.clear()
    before = daemon.menu.state.selected_index

    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert backend.calls == [("tap", ("f12",))]
    assert daemon.menu.state.selected_index == before


def test_a_claimed_turn_still_beats_the_decks_own_binding(tmp_path: Path) -> None:
    """The menu's claim wins where it has one, and only there."""
    # The deck's own big-dial right turn is Spotlight here, so the two actions are
    # distinguishable in the recording: the claim must be the one that runs.
    deck_dial = """
[bindings.top_turn_right]
action_id = "spotlight"
"""
    daemon, _deck, backend = _daemon(tmp_path / "no-claim", DECK + deck_dial)
    _open_menu(daemon)
    backend.calls.clear()
    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert backend.calls == [("tap", ("cmd", "space"))], "the deck's own turn"

    claimed = _daemon(tmp_path / "claimed", DECK + deck_dial + CLAIMED_TURNS)
    daemon, _deck, backend = claimed
    _open_menu(daemon)
    backend.calls.clear()
    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert backend.calls == [("tap", ("f12",))], "the menu's claim, not the deck's own turn"
