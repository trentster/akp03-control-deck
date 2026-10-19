"""The three buttons under the LCD keys, while a menu is open.

The six LCD keys are the menu's own content and the dials are how it is navigated,
so the buttons beneath the keys are the only controls a menu may claim.  A button a
menu does not claim keeps its ordinary deck job, which is what all three did before
a menu could claim one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w

from akp03 import config as config_module
from akp03.config import ConfigError, config_document, load_config
from akp03.daemon import Daemon
from akp03.device import Event
from akp03.model import (
    ActionDefinition, ControlPlaneEditorError, MenuControlDefinition, MenuDefinition,
    MenuItemDefinition, action_registry_toml, custom_menu_definitions, validate_menu_definitions,
)


# A deck whose first key opens a menu, and whose three buttons carry ordinary deck
# jobs: quiet, a chord, and a spoken chord.  The menu claims two of them - the left
# one as a tap, the middle held down - and leaves the right one alone.
DECK = '''[actions.open_menu]
action = "menu"
menu = "youtube"

[actions.rewind]
action = "keys"
keys = ["left"]

[actions.quiet]
action = "brightness"
brightness = 20

[actions.loud]
action = "brightness"
brightness = 90

[actions.dictate]
action = "keys"
keys = ["ctrl", "shift", "space"]

[actions.hang_up]
action = "shell"
shell = ["true"]

[bindings.key0]
action_id = "open_menu"

[bindings.button6]
action_id = "quiet"

[bindings.button7]
action_id = "rewind"

[bindings.button8]
action_id = "dictate"

[custom_menus.youtube]
root = "home"

[[custom_menus.youtube.pages]]
id = "home"
title = "YouTube"
items = [{ key = 0, label = "Rewind", action = "rewind" }]
'''

CLAIMS = '''
[custom_menus.youtube.controls]
button6 = "loud"
button8 = { action_id = "dictate", mode = "hold" }
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


def _config(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def _menu(path: Path, text: str):
    """The one menu in a config written to ``path``."""
    menus = load_config(path).custom_menus or {}
    return menus["youtube"]


def test_a_menu_claims_the_buttons_under_the_keys_and_nothing_else(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, DECK + CLAIMS)
    claims = _menu(path, DECK + CLAIMS).controls or {}

    assert set(claims) == {"button6", "button8"}
    assert claims["button6"].action == "loud"
    assert claims["button6"].mode == "tap", "an unnamed mode is a tap"
    assert claims["button8"].action == "dictate" and claims["button8"].mode == "hold"
    # The right button is left out, so it keeps its ordinary deck job.
    assert "button7" not in claims
    # The claimed buttons are named in deck order, left to right.
    assert config_module.MENU_CONTROL_NAMES == ("button6", "button8", "button7")


def test_a_menu_with_no_claims_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, DECK)
    claims = _menu(path, DECK).controls
    assert not claims, "a menu that claims nothing leaves every cap its own job"


@pytest.mark.parametrize("claims", [
    # Only the three buttons under the keys: the LCD keys are the menu's content.
    '\n[custom_menus.youtube.controls]\nkey0 = "loud"\n',
    # And the dials are its navigation, so a menu cannot quietly take them over.
    '\n[custom_menus.youtube.controls]\ntop = "loud"\n',
    '\n[custom_menus.youtube.controls]\nbottom_left = "loud"\n',
    '\n[custom_menus.youtube.controls]\nbottom_right = "loud"\n',
    # A claim must name an action that exists and is safe to run inside a menu.
    '\n[custom_menus.youtube.controls]\nbutton6 = "nobody"\n',
    '\n[custom_menus.youtube.controls]\nbutton6 = "hang_up"\n',
    # A mode that means nothing, and a hold on something there is nothing to hold.
    '\n[custom_menus.youtube.controls]\nbutton6 = { action_id = "dictate", mode = "latch" }\n',
    '\n[custom_menus.youtube.controls]\nbutton6 = { action_id = "loud", mode = "hold" }\n',
    # A claim that is not an action name at all.
    '\n[custom_menus.youtube.controls]\nbutton6 = 5\n',
])
def test_a_claim_outside_the_rules_is_refused(tmp_path: Path, claims: str) -> None:
    path = tmp_path / "config.toml"; _config(path, DECK + claims)
    with pytest.raises(ConfigError):
        load_config(path)


def test_a_save_writes_a_tap_as_one_line_and_a_hold_as_a_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, DECK + CLAIMS)
    loaded = load_config(path)
    text = tomli_w.dumps(config_document(loaded))

    # The short spelling keeps an ordinary claim as readable as the rest of the file.
    assert 'button6 = "loud"' in text
    assert 'action_id = "dictate"' in text and 'mode = "hold"' in text

    again = tmp_path / "again.toml"; _config(again, text)
    assert (_menu(again, text).controls or {}) == (loaded.custom_menus["youtube"].controls or {})


def test_the_open_menu_runs_its_own_buttons_and_leaves_the_others_alone(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, DECK + CLAIMS)
    deck, backend = Deck("/dev/fake"), Backend()
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck, backend=backend)
    assert daemon.reload() and daemon.connect()

    # The deck's own job for the left button, with no menu open: quiet.
    daemon.event(Event("button", "button6", True, None, b""))
    assert deck.calls[-1] == ("brightness", 20)

    daemon.event(Event("key", "key0", True, None, b""))
    assert daemon.custom_menu is not None

    # While the menu is open, the left button does what the menu says: loud.
    daemon.event(Event("button", "button6", True, None, b""))
    assert deck.calls[-1] == ("brightness", 90)

    # The button the menu left alone keeps its ordinary job.
    daemon.event(Event("button", "button7", True, None, b""))
    assert backend.calls[-1] == ("tap", ("left",))

    # A claimed hold presses on the way down and lets go on the way up.
    daemon.event(Event("button", "button8", True, None, b""))
    daemon.event(Event("button", "button8", False, None, b""))
    assert backend.calls[-2:] == [
        ("press", ("ctrl", "shift", "space")),
        ("release", ("ctrl", "shift", "space")),
    ]
    assert daemon.held == {}

    # Backing out of the menu hands the buttons their own jobs again.
    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    assert daemon.custom_menu is None
    daemon.event(Event("button", "button6", True, None, b""))
    assert deck.calls[-1] == ("brightness", 20)


# ── What the editor shows and writes ───────────────────────────────────────────

def test_a_menu_carries_its_claims_into_the_editor_in_deck_order(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, DECK + CLAIMS)
    menus = custom_menu_definitions(path)

    assert [menu.name for menu in menus] == ["youtube"]
    # Left is button6, middle is button8, right is button7: the order they sit in.
    assert menus[0].controls == (
        MenuControlDefinition("loud"),
        MenuControlDefinition("dictate", "hold"),
        None,
    ), "left and middle claimed, the right one left alone"


def test_the_editor_writes_the_claims_it_shows(tmp_path: Path) -> None:
    actions = [
        ActionDefinition("rewind", "keys", keys=("left",)),
        ActionDefinition("loud", "brightness", brightness=90),
        ActionDefinition("dictate", "keys", keys=("ctrl", "shift", "space")),
    ]
    menus = [MenuDefinition(
        "youtube", "YouTube",
        (MenuItemDefinition(0, "Rewind", action_id="rewind"),),
        (MenuControlDefinition("loud"), MenuControlDefinition("dictate", "hold"), None),
    )]
    text = action_registry_toml(actions, [None] * 6, menus, (None, None, None), ())

    assert "[custom_menus.youtube.controls]" in text
    assert 'button6 = "loud"' in text
    assert 'button8 = { action_id = "dictate", mode = "hold" }' in text
    assert "button7" not in text, "an empty slot is not written at all"

    # And those claims load back as the same ones, so a save cannot lose them.
    path = tmp_path / "config.toml"; _config(path, text)
    claims = _menu(path, text).controls or {}
    assert claims["button6"].action == "loud"
    assert claims["button8"].mode == "hold"


def test_the_editor_refuses_a_claim_the_deck_could_not_run() -> None:
    actions = [ActionDefinition("loud", "brightness", brightness=90)]
    # Nothing to hold down: a brightness action is not a chord.
    held = MenuDefinition("youtube", "YouTube", (), (None, MenuControlDefinition("loud", "hold"), None))
    with pytest.raises(ControlPlaneEditorError):
        validate_menu_definitions([held], actions)
    # And a claim has to name an action the editor can write.
    stranger = MenuDefinition("youtube", "YouTube", (), (MenuControlDefinition("nobody"), None, None))
    with pytest.raises(ControlPlaneEditorError):
        validate_menu_definitions([stranger], actions)
