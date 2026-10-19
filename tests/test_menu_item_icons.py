"""Menu items drawn as pictures instead of words.

A menu key can carry a library icon, and when it does the deck shows that picture
and none of the item's text: the words stay in the model so the editor and the
mirror can still name the item, but nothing of them reaches the glass.  These
tests hold that rule at the three layers that can each quietly break it - the
config, the daemon's painter, and the editor's round trip.
"""
from __future__ import annotations

import io
import tomllib
from pathlib import Path

import pytest
from PIL import Image

from akp03 import artwork
from akp03.config import ConfigError, config_document, load_config
from akp03.daemon import Daemon, Paint, _label_image
from akp03.device import Event

WITH_ICON = '''[actions.undo]
action = "keys"
keys = ["cmd", "z"]
[actions.copy]
action = "keys"
keys = ["cmd", "c"]
[actions.youtube]
action = "menu"
menu = "youtube"
[bindings.key1]
action_id = "youtube"
[custom_menus.youtube]
root = "home"
[[custom_menus.youtube.pages]]
id = "home"
title = "YouTube"
items = [
  { key = 0, label = "Rewind", action = "undo", icon = "pack:general/undo" },
  { key = 1, label = "Copy", action = "copy" },
]
'''

UNKNOWN_ICON = WITH_ICON.replace("pack:general/undo", "pack:general/no-such-icon")

WITH_ART = WITH_ICON + '''
[images]
key1 = "key1.jpg"
'''


class FakeDeck:
    def __init__(self, _path: str):
        self.calls: list[tuple] = []

    def on_event(self, callback): pass
    def on_disconnect(self, callback): pass
    def open(self): return self
    def close(self): return None
    def clear(self, key=None): self.calls.append(("clear", key))
    def set_key_image(self, key, source): self.calls.append(("image", key))
    def set_brightness(self, level): self.calls.append(("brightness", level))


def write_config(tmp_path: Path, text: str = WITH_ICON, *, art_for: tuple[int, ...] = ()) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    path.chmod(0o600)
    if art_for:
        from akp03.images import to_jpeg
        images = tmp_path / "images"
        images.mkdir(mode=0o700, exist_ok=True)
        for key in art_for:
            target = images / f"key{key}.jpg"
            target.write_bytes(to_jpeg(Image.new("RGB", (60, 60), "#123456")))
            target.chmod(0o600)
    return path


def running_daemon(tmp_path: Path, text: str = WITH_ICON, *, art_for: tuple[int, ...] = ()) -> Daemon:
    daemon = Daemon(write_config(tmp_path, text, art_for=art_for), state_path=tmp_path / "state",
                    deck_factory=lambda path: FakeDeck(path))
    assert daemon.reload() and daemon.connect()
    return daemon


def open_menu(daemon: Daemon) -> None:
    daemon.event(Event("key", "key1", True, None, b""))


def icon_bytes(reference: str) -> bytes:
    """Exactly the bytes the deck would receive for one library icon."""
    from PIL import Image as _Image
    with _Image.open(io.BytesIO(artwork.find_icon(reference).jpeg())) as opened:
        return opened.convert("RGB").tobytes()


# --- the config -----------------------------------------------------------------

def test_an_item_icon_survives_a_config_round_trip(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    item = config.custom_menus["youtube"].pages["home"].items[0]

    assert item.icon == "pack:general/undo"
    document = config_document(config)
    assert document["custom_menus"]["youtube"]["pages"][0]["items"][0]["icon"] == "pack:general/undo"
    # An item with no icon does not grow a null field.
    assert "icon" not in document["custom_menus"]["youtube"]["pages"][0]["items"][1]


@pytest.mark.parametrize("bad", [
    '"file:key0.jpg"',   # a managed face, not a library icon
    '"/etc/passwd"',     # never a path
    '"general/undo"',    # missing the pack: prefix
    '"pack:general/Undo"',  # upper case is not a library reference
    "12",                # not a string
])
def test_a_menu_icon_must_be_a_library_reference(tmp_path: Path, bad: str) -> None:
    text = WITH_ICON.replace('"pack:general/undo"', bad)
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, text))


# --- the deck -------------------------------------------------------------------

def test_a_menu_key_shows_its_icon_and_none_of_its_word(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    open_menu(daemon)

    face = daemon._base_image(0)
    assert face is not None
    # What reaches the glass is the icon's own pixels...
    assert face.tobytes() == icon_bytes("pack:general/undo")
    # ...and not the word "Rewind" drawn as a label.
    assert face.tobytes() != _label_image(Paint(0, "Rewind", "#173f5f")).tobytes()


def test_an_item_without_an_icon_keeps_its_word(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    open_menu(daemon)

    face = daemon._base_image(1)
    assert face is not None
    assert face.tobytes() == _label_image(Paint(1, "Copy", "black")).tobytes()
    # The key with the icon is a picture; the key without one is still text.
    assert daemon.menu_faces.get(0) is not None and daemon.menu_faces.get(1) is None


def test_the_mirror_reports_the_menu_icon_as_an_image(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    open_menu(daemon)
    keys = daemon.screen_description()["keys"]

    assert keys[0]["kind"] == "image" and keys[0]["revision"]
    # The words stay in the model even when the glass shows a picture: the editor
    # needs the item's name to label the key it is assigning.
    assert keys[0]["text"] == "Rewind"
    assert keys[1]["kind"] == "text" and keys[1]["text"] == "Copy"


def test_an_unresolvable_icon_falls_back_to_the_word_instead_of_failing(tmp_path: Path) -> None:
    """A pack edited on disk must not leave the deck unable to draw a menu."""
    daemon = running_daemon(tmp_path, UNKNOWN_ICON)
    open_menu(daemon)

    assert daemon.custom_menu is not None, "the menu still opens"
    assert daemon.menu_faces == {}
    face = daemon._base_image(0)
    assert face is not None
    assert face.tobytes() == _label_image(Paint(0, "Rewind", "#173f5f")).tobytes()


def test_leaving_the_menu_puts_the_keys_own_artwork_back(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path, WITH_ART, art_for=(1,))
    open_menu(daemon)
    assert daemon.screen_description()["keys"][0]["kind"] == "image"

    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    assert daemon.custom_menu is None
    assert daemon.menu_faces == {}
    # Key 1 wears configured artwork again; the menu's own icon is gone, and a key
    # with nothing configured is blank rather than left showing the menu item.
    assert daemon.screen_description()["keys"][1]["kind"] == "image"
    assert daemon.screen_description()["keys"][0]["kind"] == "blank"


# --- the editor -----------------------------------------------------------------

def test_the_editor_carries_an_item_icon_in_both_directions(tmp_path: Path) -> None:
    from akp03.model import action_registry_toml
    from akp03.webui import ControlPlaneEditorError, draft_from_config, draft_payload, parse_draft

    path = write_config(tmp_path)
    draft = draft_from_config(path)
    item = draft_payload(draft)["menus"][0]["items"][0]
    assert item["icon"] == "pack:general/undo"

    # Saving is what could silently delete it: the TOML the editor writes must
    # still carry the icon, and the browser's own document must be accepted back.
    # (On disk a menu is still written as its single page.)
    written = action_registry_toml(list(draft.actions), list(draft.bindings), list(draft.menus))
    items = tomllib.loads(written)["custom_menus"]["youtube"]["pages"][0]["items"]
    assert items[0]["icon"] == "pack:general/undo" and "icon" not in items[1]

    round_tripped = parse_draft({"actions": draft_payload(draft)["actions"],
                                 "bindings": list(draft.bindings),
                                 "dials": [None] * 8,
                                 "menus": draft_payload(draft)["menus"]})
    assert round_tripped.menus[0].items[0].icon == "pack:general/undo"

    with pytest.raises(ControlPlaneEditorError):
        parse_draft({"actions": draft_payload(draft)["actions"],
                     "bindings": list(draft.bindings),
                     "dials": [None] * 8,
                     "menus": [{"name": "youtube", "title": "YouTube", "items": [
                         {"key": 0, "label": "Rewind", "action": "undo", "icon": "file:key0.jpg"}]}]})
