from __future__ import annotations

from pathlib import Path

import pytest

from akp03.config import ConfigError, load_config
from akp03.daemon import Daemon
from akp03.device import Event


def config(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


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


REGISTRY = '''[actions.copy]
action = "keys"
keys = ["ctrl", "c"]
[actions.dim]
action = "brightness"
brightness = 25
[actions.ptt]
action = "keys_hold"
keys_hold = ["ctrl", "shift", "space"]

[bindings.top]
action_id = "copy"
[bindings.button6]
action_id = "ptt"

[menu]
enabled = true
root = "home"
[[menu.pages]]
id = "home"
items = [{ label = "Copy", action = "copy" }, { label = "Dim", action = "dim" }]
'''

HELD_AND_TAPPED = '''[actions.dictation]
action = "keys"
keys = ["ctrl", "shift", "space"]

[bindings.key0]
action_id = "dictation"
[bindings.button6]
action_id = "dictation"
mode = "hold"
'''


def test_one_action_can_be_tapped_on_one_control_and_held_on_another(tmp_path: Path) -> None:
    """The mode belongs to the assignment, so a shared action serves both."""
    path = tmp_path / "config.toml"
    config(path, HELD_AND_TAPPED)
    loaded = load_config(path)

    # The tapped key still points at the action itself: nothing was copied.
    assert loaded.bindings["key0"] is loaded.actions["dictation"]
    assert loaded.bindings["key0"].mode == "tap"
    assert loaded.bindings["button6"].action == "keys"
    assert loaded.bindings["button6"].keys == ("ctrl", "shift", "space")
    assert loaded.bindings["button6"].mode == "hold"
    assert loaded.actions["dictation"].mode == "tap"

    deck = Deck("/dev/fake")
    backend = Backend()
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck, backend=backend)
    assert daemon.reload() and daemon.connect()
    daemon.event(Event("button", "button6", True, None, b""))
    daemon.event(Event("button", "button6", False, None, b""))
    daemon.event(Event("key", "key0", True, None, b""))

    assert backend.calls == [
        ("press", ("ctrl", "shift", "space")),
        ("release", ("ctrl", "shift", "space")),
        ("tap", ("ctrl", "shift", "space")),
    ]


def test_registry_reuses_actions_for_physical_bindings_and_menu_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config(path, REGISTRY)
    loaded = load_config(path)
    assert loaded.bindings["top"] is loaded.actions["copy"]
    assert loaded.bindings["button6"] is loaded.actions["ptt"]

    deck = Deck("/dev/fake")
    backend = Backend()
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck, backend=backend)
    assert daemon.reload() and daemon.connect()
    daemon.event(Event("knob_press", "top", True, None, b""))
    daemon.event(Event("key", "key1", True, None, b""))
    daemon.event(Event("button", "button6", True, None, b""))
    daemon.event(Event("button", "button6", False, None, b""))
    assert backend.calls == [("tap", ("ctrl", "c")), ("press", ("ctrl", "shift", "space")), ("release", ("ctrl", "shift", "space"))]
    assert ("brightness", 25) in deck.calls


@pytest.mark.parametrize("text, message", [
    ("[actions.Bad]\naction=\"keys\"\nkeys=[\"x\"]\n", "action IDs"),
    ("[actions.copy]\naction=\"keys\"\nkeys=[\"x\"]\nshell=[\"/bin/true\"]\n", "unsupported fields"),
    ("[bindings.top]\naction_id=\"missing\"\n", "declared action"),
    ("[actions.copy]\naction=\"keys\"\nkeys=[\"x\"]\n[bindings.top]\naction_id=\"copy\"\nkeys=[\"y\"]\n", "cannot be combined"),
    ("[actions.hold]\naction=\"keys_hold\"\nkeys_hold=[\"x\"]\n[menu]\nenabled=true\nroot=\"home\"\n[[menu.pages]]\nid=\"home\"\nitems=[{label=\"Hold\",action=\"hold\"}]\n", "keys_hold"),
    ("[menu]\nenabled=true\nroot=\"home\"\n[[menu.pages]]\nid=\"home\"\nitems=[{label=\"Unsafe\",action=\"shell:/bin/sh\"}]\n", "declared action"),
    ("[bindings.key0]\naction=\"keys\"\nkeys=[\"x\"]\nmode=\"latch\"\n", "mode must be tap or hold"),
    ("[actions.dim]\naction=\"brightness\"\nbrightness=50\n[bindings.key1]\naction_id=\"dim\"\nmode=\"hold\"\n", "mode is only meaningful"),
    ("[bindings.key0]\naction=\"none\"\nmode=\"hold\"\n", "unsupported fields"),
])
def test_registry_rejects_malformed_or_unsafe_definitions(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "config.toml"
    config(path, text)
    with pytest.raises(ConfigError, match=message):
        load_config(path)


DERIVED_LABELS = '''[actions.new-window]
action = "keys"
keys = ["cmd", "n"]
[actions.dim]
action = "brightness"
brightness = 50
[bindings.key0]
action_id = "new-window"
[bindings.key1]
action_id = "dim"
[bindings.key3]
action = "keys"
keys = ["cmd", "z"]
[labels]
key0 = "One"
key1 = "Two"
key2 = "Three"
key3 = "Four"
'''


def test_a_bound_key_shows_the_assigned_action_name_and_an_unbound_key_keeps_its_label(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config(path, DERIVED_LABELS)
    loaded = load_config(path)

    # The [labels] section is never rewritten: it stays the round-tripping
    # source of truth for every key.
    assert loaded.labels == {0: "One", 1: "Two", 2: "Three", 3: "Four"}
    # What the LCD keys actually show, derived from the bindings.
    assert loaded.key_labels == {
        0: "New Window",      # bound to the named action "new-window"
        1: "Brightness 50%",  # bound to a brightness action
        2: "Three",           # unbound, so the configured [labels] text
        3: "Four",            # an unnamed inline binding the editor cannot present
    }


def test_a_bound_brightness_key_shows_the_editor_percentage_text(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config(path, '[bindings.key4]\naction = "brightness"\nbrightness = 100\n[labels]\nkey4 = "Bright"\n')
    assert load_config(path).key_labels == {4: "Brightness 100%"}


def test_derived_key_names_survive_a_save_without_touching_the_labels_section(tmp_path: Path) -> None:
    from akp03.config import save_config

    source = tmp_path / "config.toml"
    config(source, DERIVED_LABELS)
    loaded = load_config(source)

    target = tmp_path / "copy.toml"
    save_config(loaded, target)
    written = target.read_text()
    reloaded = load_config(target)

    assert '[labels]' in written
    assert 'key0 = "One"' in written
    assert 'key1 = "Two"' in written
    # No derived text was written into the config...
    assert "New Window" not in written
    assert "Brightness 50%" not in written
    # ...and the derivation is identical after the round trip.
    assert reloaded.labels == loaded.labels
    assert reloaded.key_labels == loaded.key_labels


def test_a_save_repaints_the_derived_key_names_on_the_deck(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config(path, '''[actions.copy]
action = "keys"
keys = ["ctrl", "c"]
[bindings.key0]
action_id = "copy"
[labels]
key0 = "One"
key1 = "Two"
''')
    deck = Deck("/dev/fake")
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck)  # type: ignore[arg-type]
    assert daemon.reload()
    # Bound key: the assigned action's human name.  Unbound key: the label.
    assert daemon.labels[0].text == "Copy"
    assert daemon.labels[1].text == "Two"

    # Rebind key0 the way the editor does, then apply the save.  The screen text
    # must follow the new assignment for the next paint.
    config(path, '''[actions.undo]
action = "keys"
keys = ["cmd", "z"]
[bindings.key0]
action_id = "undo"
[labels]
key0 = "One"
key1 = "Two"
''')
    assert daemon._apply_editor_save()
    assert daemon.labels[0].text == "Undo"
    assert daemon.labels[1].text == "Two"
    assert daemon.config is not None
    assert daemon.config.labels == {0: "One", 1: "Two"}


def test_the_daemon_reports_a_paint_failure_instead_of_claiming_the_screens(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config(path, '[bindings.key0]\naction = "keys"\nkeys = ["ctrl", "c"]\n')

    # A hidraw-style deck cannot report painting separately, so there is nothing
    # to claim either way and the editor makes no statement about the screens.
    plain = Deck("/dev/fake")
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: plain)  # type: ignore[arg-type]
    assert daemon.reload() and daemon.connect()
    assert daemon.paint_status() is None

    class FailingDeck(Deck):
        paint_error = "device write failed"

    failing = FailingDeck("/dev/fake")
    reporting = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: failing)  # type: ignore[arg-type]
    assert reporting.reload() and reporting.connect()
    assert reporting.paint_status() == "device write failed"

    # With no deck attached the screens are certainly not what the config says.
    def unavailable(_path: str) -> Deck:
        raise OSError("no device")

    detached = Daemon(path, state_path=tmp_path / "state", deck_factory=unavailable)  # type: ignore[arg-type]
    assert detached.reload()
    assert detached.connect() is False
    assert detached.paint_status() == "device unavailable: no device"
