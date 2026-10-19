"""The three caps under the LCD keys, and the two ways an assignment behaves.

The editor assigns a cap exactly as it assigns an LCD key: by naming an action.
Two things are new here.  A binding can be *held* rather than tapped, on any
control.  And a chord written straight into the file is *adopted* under a name
rather than erased, because a save must never lose behaviour it did not set.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from akp03.config import ConfigError, load_config
from akp03.daemon import Daemon
from akp03.device import Event
from akp03.model import (
    BUTTON_CONTROLS,
    ActionDefinition,
    ControlPlaneEditorError,
    action_registry_toml,
)
from akp03.webui import draft_from_config, draft_payload, merge_draft_into_config, parse_draft

HAND_WRITTEN = '''[daemon]
device = "/dev/hidraw0"

[actions.undo]
action = "keys"
keys = ["cmd", "z"]

[bindings.button6]
action = "keys_hold"
keys_hold = ["ctrl", "shift", "space"]
[bindings.button7]
action = "keys"
keys = ["enter"]
[bindings.button8]
action = "keys"
keys = ["cmd", "z"]
'''


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o600)
    return path


class Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def tap(self, keys: tuple[str, ...]) -> None:
        self.calls.append(("tap", tuple(keys)))

    def press(self, keys: tuple[str, ...]) -> None:
        self.calls.append(("press", tuple(keys)))

    def release(self, keys: tuple[str, ...]) -> None:
        self.calls.append(("release", tuple(keys)))


class Deck:
    def __init__(self, _path: str) -> None:
        self.calls: list[tuple[object, ...]] = []

    def on_event(self, callback) -> None:
        self.event = callback

    def on_disconnect(self, callback) -> None:
        self.disconnect = callback

    def open(self):
        return self

    def close(self) -> None:
        pass

    def clear(self, key=None) -> None:
        self.calls.append(("clear", key))

    def set_key_image(self, key, image) -> None:
        self.calls.append(("image", key))

    def set_brightness(self, level: int) -> None:
        self.calls.append(("brightness", level))


def test_the_three_caps_are_read_in_deck_order_and_named(tmp_path: Path) -> None:
    # Deck order is left, middle, right.  The firmware's own names are not in
    # that order, so the editor's list is where the two orders meet.
    assert BUTTON_CONTROLS == ("button6", "button8", "button7")

    draft = draft_from_config(write(tmp_path / "config.toml", HAND_WRITTEN))

    assert draft.buttons == ("hold-ctrl-shift-space", "undo", "keys-enter")
    assert draft.held == ("button6",)
    action_ids = {definition.action_id for definition in draft.actions}
    # The middle cap's chord already had a name, so that name was reused rather
    # than a second identical action invented beside it.
    assert "undo" in action_ids
    assert "keys-cmd-z" not in action_ids
    assert "hold-ctrl-shift-space" in action_ids


def test_an_adopted_chord_still_holds_after_a_save(tmp_path: Path) -> None:
    path = write(tmp_path / "config.toml", HAND_WRITTEN)
    merged = merge_draft_into_config(path.read_text(), draft_from_config(path))
    write(path, merged)

    config = load_config(path)
    assert config.bindings["button6"].mode == "hold"
    assert config.bindings["button6"].keys == ("ctrl", "shift", "space")
    assert config.bindings["button7"].keys == ("enter",)

    backend = Backend()
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=Deck, backend=backend)
    assert daemon.reload() and daemon.connect()
    daemon.event(Event("button", "button6", True, None, b""))
    daemon.event(Event("button", "button6", False, None, b""))
    daemon.event(Event("button", "button7", True, None, b""))
    assert backend.calls == [
        ("press", ("ctrl", "shift", "space")),
        ("release", ("ctrl", "shift", "space")),
        ("tap", ("enter",)),
    ]


def test_a_held_assignment_works_on_an_lcd_key_too(tmp_path: Path) -> None:
    path = write(tmp_path / "config.toml", (
        '[daemon]\ndevice = "/dev/hidraw0"\n'
        '[actions.copy]\naction = "keys"\nkeys = ["ctrl", "c"]\n'
        '[bindings.key0]\naction_id = "copy"\n'
    ))
    payload = draft_payload(draft_from_config(path))
    payload["held"] = ["key0"]

    staged = parse_draft(payload)
    assert staged.held == ("key0",)
    merged = merge_draft_into_config(path.read_text(), staged)
    write(path, merged)

    assert '[bindings.key0]\naction_id = "copy"\nmode = "hold"' in merged
    assert load_config(path).bindings["key0"].mode == "hold"


def test_the_payload_carries_the_caps_and_what_is_held(tmp_path: Path) -> None:
    payload = draft_payload(draft_from_config(write(tmp_path / "config.toml", HAND_WRITTEN)))

    assert payload["buttons"] == ["hold-ctrl-shift-space", "undo", "keys-enter"]
    assert payload["held"] == ["button6"]
    # A payload that has been through the browser and back is the same payload.
    assert draft_payload(parse_draft(payload)) == payload


def test_a_held_assignment_refuses_anything_but_a_keys_action() -> None:
    payload = {
        "actions": [{"id": "dim", "kind": "brightness", "brightness": 40}],
        "bindings": [None] * 6,
        "buttons": ["dim", None, None],
        "dials": [None] * 8,
        "held": ["button6"],
        "menus": [],
    }
    with pytest.raises(ControlPlaneEditorError, match="needs a keys action"):
        parse_draft(payload)

    payload["buttons"] = [None, None, None]
    with pytest.raises(ControlPlaneEditorError, match="nothing assigned to hold"):
        parse_draft(payload)


def test_a_button_binding_the_editor_cannot_show_is_refused_not_erased(tmp_path: Path) -> None:
    text = (
        '[daemon]\ndevice = "/dev/hidraw0"\n'
        '[bindings.button6]\naction = "http"\nmethod = "GET"\nurl = "https://example.invalid/hook"\n'
    )
    path = write(tmp_path / "config.toml", text)
    draft = draft_from_config(path)
    assert draft.buttons == (None, None, None)

    with pytest.raises(ControlPlaneEditorError, match="refusing to overwrite"):
        merge_draft_into_config(path.read_text(), draft)
    assert '[bindings.button6]\naction = "http"' in path.read_text()


# -- media keys: a real key, and nothing that can be held you cannot hold --------


def test_a_binding_cannot_hold_a_media_key(tmp_path: Path) -> None:
    """A media function is one event pair; there is no held state to ask for."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[daemon]\n"
        'device = "/dev/hidraw0"\n\n'
        "[actions.volume-up]\n"
        'action = "keys"\n'
        'keys = ["sound-up"]\n\n'
        "[bindings.key0]\n"
        'action_id = "volume-up"\n'
        'mode = "hold"\n'
    )
    path.chmod(0o600)
    with pytest.raises(ConfigError, match="cannot hold a media key"):
        load_config(path)


def test_the_editor_refuses_to_hold_a_media_key(tmp_path: Path) -> None:
    """The same rule where the editor writes it, so the panel cannot offer it."""
    with pytest.raises(ControlPlaneEditorError, match="not a media key"):
        action_registry_toml(
            [ActionDefinition("volume-up", "keys", ("sound-up",))],
            ["volume-up", None, None, None, None, None],
            held=("key0",),
        )


def test_a_media_key_token_has_no_function_key_standing_in(tmp_path: Path) -> None:
    """F12 is not volume up, and the file that says so is the one that sends it."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[daemon]\n"
        'device = "/dev/hidraw0"\n\n'
        "[actions.volume-up]\n"
        'action = "keys"\n'
        'keys = ["sound-up"]\n\n'
        "[bindings.key0]\n"
        'action_id = "volume-up"\n'
    )
    path.chmod(0o600)
    config = load_config(path)
    assert config.bindings["key0"].keys == ("sound-up",)
    assert config.bindings["key0"].mode == "tap"


def test_the_editor_writes_a_media_action_the_daemon_accepts(tmp_path: Path) -> None:
    """The editor's own path: draft in, file out, and the daemon reads it back."""
    path = tmp_path / "config.toml"
    path.write_text('[daemon]\ndevice = "/dev/hidraw0"\n')
    path.chmod(0o600)
    draft = replace(
        draft_from_config(path),
        actions=[ActionDefinition("volume-up", "keys", ("sound-up",))],
        bindings=["volume-up", None, None, None, None, None],
    )
    merged = merge_draft_into_config(path.read_text(), draft)
    assert '"sound-up"' in merged
    path.write_text(merged)
    reloaded = load_config(path)
    assert reloaded.bindings["key0"].keys == ("sound-up",)
