from __future__ import annotations

from pathlib import Path

import pytest

from akp03.config import ConfigError, load_config
from akp03.daemon import Daemon
from akp03.device import Event


def config(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


class Deck:
    def __init__(self, path: str) -> None:
        self.path = path
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


CUSTOM_MENU = '''[menu]
enabled = true
root = "home"

[[menu.pages]]
id = "home"
title = "Home"
items = [
  { label = "Display", submenu = "display" },
  { label = "Dim", action = "brightness:25" },
]

[[menu.pages]]
id = "display"
title = "Display"
items = [
  { label = "Full", action = "brightness:100" },
]
'''


def test_custom_menu_schema_builds_page_tree_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config(path, CUSTOM_MENU)

    loaded = load_config(path)

    assert loaded.menu_enabled is True
    assert loaded.menu_root is not None
    assert loaded.menu_root.title == "Home"
    assert loaded.menu_root.items[0].label == "Display"
    assert loaded.menu_root.items[0].submenu is not None
    assert loaded.menu_root.items[0].submenu.title == "Display"
    assert loaded.menu_root.items[1].action == "brightness:25"


@pytest.mark.parametrize(
    ("menu", "message"),
    [
        ("[menu]\nenabled=true\nroot=\"home\"\n", "menu pages"),
        ("[menu]\nenabled=true\nroot=\"missing\"\n[[menu.pages]]\nid=\"home\"\nitems=[{label=\"One\",action=\"brightness:1\"}]\n", "root"),
        ("[menu]\nenabled=true\nroot=\"home\"\n[[menu.pages]]\nid=\"home\"\nitems=[{label=\"One\",submenu=\"missing\"}]\n", "submenu"),
        ("[menu]\nenabled=true\nroot=\"home\"\n[[menu.pages]]\nid=\"home\"\nitems=[{label=\"One\",action=\"shell:/bin/sh\"}]\n", "brightness"),
        ("[menu]\nenabled=true\nroot=\"home\"\n[[menu.pages]]\nid=\"home\"\nitems=[{label=\"One\",action=\"http:https://example.invalid\"}]\n", "brightness"),
        ("[menu]\nenabled=true\nroot=\"home\"\n[[menu.pages]]\nid=\"home\"\nitems=[{label=\"One\",action=\"brightness:0\"}]\n", "brightness"),
        ("[menu]\nenabled=true\nroot=\"home\"\n[[menu.pages]]\nid=\"home\"\nitems=[{label=\"One\",action=\"brightness:101\"}]\n", "brightness"),
    ],
)
def test_custom_menu_schema_rejects_invalid_and_unsafe_definitions(tmp_path: Path, menu: str, message: str) -> None:
    path = tmp_path / "config.toml"
    config(path, menu)

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_enabled_custom_menu_renders_and_only_runs_brightness(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config(path, CUSTOM_MENU)
    deck = Deck("/dev/fake")
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck)

    assert daemon.reload() and daemon.connect()
    assert daemon.labels[0].text == "> Display"
    assert daemon.labels[1].text == "Dim"

    daemon.event(Event("key", "key1", True, None, b""))

    assert ("brightness", 25) in deck.calls


def test_enabled_menu_without_pages_keeps_demo_fallback(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config(path, "[menu]\nenabled=true\n")
    daemon = Daemon(path, state_path=tmp_path / "state")

    assert daemon.reload()
    assert daemon.menu is not None
    assert daemon.menu.state.title == "Deck"
