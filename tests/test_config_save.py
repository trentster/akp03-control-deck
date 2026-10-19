"""Config persistence: validated atomic save and exact round-trip equality."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from akp03 import config as config_module
from akp03.config import Binding, Config, ConfigError, load_config, save_config

FULL = '''[daemon]
device = "/dev/hidraw3"
poll_seconds = 0.5

[deck]
max_hold_s = 30

[feedback]
enabled = true
duration_ms = 250

[actions.copy]
action = "keys"
keys = ["ctrl", "c"]
[actions.dim]
action = "brightness"
brightness = 40
[actions.ptt]
action = "keys_hold"
keys_hold = ["ctrl", "shift", "space"]
[actions.gomenu]
action = "omarchy_menu"
route = "apps"
[actions.studio]
action = "menu"
menu = "studio"

[bindings.key0]
action_id = "copy"
[bindings.key1]
action = "keys"
keys = ["enter"]
[bindings.bottom_left]
action = "http"
method = "POST"
url = "https://example.invalid/hook"
json = { source = "akp03", level = 2 }
headers = { Authorization = "env:AKP03_WEBHOOK_TOKEN" }
[bindings.top]
action_id = "gomenu"

[labels]
key0 = "Copy"
key1 = "Enter"

[images]
key0 = "key0.jpg"

[menu]
enabled = true
root = "home"
[[menu.pages]]
id = "home"
title = "Home"
items = [
  { label = "Copy", action = "copy" },
  { label = "Display", submenu = "display" },
]
[[menu.pages]]
id = "display"
title = "Display"
items = [ { label = "Dim", action = "brightness:40" } ]

[custom_menus.studio]
root = "home"
[[custom_menus.studio.pages]]
id = "home"
title = "Studio"
items = [
  { key = 0, label = "Copy", action = "copy" },
  { key = 1, label = "Record", submenu = "rec" },
]
[[custom_menus.studio.pages]]
id = "rec"
title = "Record"
items = [ { key = 0, label = "Dim", action = "dim" } ]
'''


def write_config(directory: Path, text: str, name: str = "config.toml") -> Path:
    path = directory / name
    path.write_text(text)
    path.chmod(0o600)
    return path


def make_images(directory: Path) -> None:
    (directory / "images").mkdir(exist_ok=True)
    (directory / "images" / "key0.jpg").write_bytes(b"\xff\xd8\xff")


def temp_files(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.*.tmp"))


HELD_ROUND_TRIP = '''[daemon]
device = "/dev/hidraw0"
[actions.dictation]
action = "keys"
keys = ["ctrl", "shift", "space"]

[bindings.button6]
action_id = "dictation"
mode = "hold"

[bindings.key0]
action = "keys"
keys = ["cmd", "n"]
mode = "hold"
'''


def test_a_held_assignment_round_trips_as_an_assignment_not_an_action(tmp_path: Path) -> None:
    original = load_config(write_config(tmp_path, HELD_ROUND_TRIP))
    target = tmp_path / "saved.toml"

    save_config(original, target)
    reloaded = load_config(target)

    assert reloaded == original
    assert reloaded.bindings["button6"].mode == "hold"
    assert reloaded.bindings["button6"].keys == ("ctrl", "shift", "space")
    assert reloaded.bindings["key0"].mode == "hold"

    written = target.read_text()
    # Still a reference to the shared action, with the mode beside it.
    assert 'action_id = "dictation"' in written
    assert written.count('mode = "hold"') == 2


def test_load_save_load_is_equal_and_atomic_mode(tmp_path: Path) -> None:
    make_images(tmp_path)
    original = load_config(write_config(tmp_path, FULL))
    target = tmp_path / "saved.toml"

    save_config(original, target)
    reloaded = load_config(target)

    assert reloaded == original
    assert temp_files(target) == []
    assert (target.stat().st_mode & 0o777) == 0o600
    assert (tmp_path.stat().st_mode & 0o777) == 0o700


def test_action_identity_survives_a_round_trip(tmp_path: Path) -> None:
    make_images(tmp_path)
    original = load_config(write_config(tmp_path, FULL))
    target = tmp_path / "saved.toml"

    save_config(original, target)
    reloaded = load_config(target)

    # The webui editor pairs bindings to actions by object identity, so a
    # reference must stay a reference rather than becoming an inline copy.
    assert reloaded.bindings["key0"] is reloaded.actions["copy"]
    assert reloaded.bindings["top"] is reloaded.actions["gomenu"]


def test_minimal_config_round_trips(tmp_path: Path) -> None:
    original = load_config(write_config(tmp_path, '[daemon]\ndevice = "/dev/hidraw0"\n'))
    target = tmp_path / "saved.toml"
    save_config(original, target)
    assert load_config(target) == original


def test_saving_creates_a_private_directory(tmp_path: Path) -> None:
    original = load_config(write_config(tmp_path, '[daemon]\ndevice = "/dev/hidraw0"\n'))
    nested = tmp_path / "fresh" / "akp03" / "config.toml"
    save_config(original, nested)
    assert load_config(nested) == original
    assert (nested.parent.stat().st_mode & 0o777) == 0o700
    assert (nested.stat().st_mode & 0o777) == 0o600


def test_invalid_config_is_rejected_without_writing_anything(tmp_path: Path) -> None:
    make_images(tmp_path)
    original = load_config(write_config(tmp_path, FULL))
    broken = dataclasses.replace(original, poll_seconds=999.0)
    target = tmp_path / "never.toml"

    with pytest.raises(ConfigError, match="poll_seconds"):
        save_config(broken, target)

    assert not target.exists()
    assert temp_files(target) == []


def test_unsafe_binding_without_a_named_action_is_rejected(tmp_path: Path) -> None:
    unsafe = Config(
        device="/dev/hidraw0",
        poll_seconds=1.0,
        max_hold_s=120.0,
        bindings={"top": Binding("omarchy_menu", omarchy_route="apps")},
        actions={},
        labels={},
        images={},
    )
    target = tmp_path / "never.toml"

    with pytest.raises(ConfigError, match="declared action"):
        save_config(unsafe, target)

    assert not target.exists()
    assert temp_files(target) == []


def test_failed_write_leaves_no_partial_file_and_keeps_the_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = load_config(write_config(tmp_path, '[daemon]\ndevice = "/dev/hidraw0"\n'))
    target = write_config(tmp_path, "# ORIGINAL\n")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(config_module.os, "replace", explode)

    with pytest.raises(OSError, match="disk on fire"):
        save_config(original, target)

    assert target.read_text() == "# ORIGINAL\n"
    assert temp_files(target) == []
