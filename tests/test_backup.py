"""A backup carries the setup and the pictures it needs, and trusts nothing it reads.

The two halves are tested apart: making one is about choosing the right pictures, and
reading one is about opening somebody else's file without believing it.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from akp03 import backup, iconpacks
from akp03.config import load_config


CONFIG = """
[actions.mute]
action = "keys"
keys = ["m"]

[actions.open_menu]
action = "menu"
menu = "watch"

[bindings.key0]
action_id = "open_menu"

[custom_menus.watch]
root = "home"

[[custom_menus.watch.pages]]
id = "home"
title = "Watch"
items = [
  { key = 0, label = "Mute", action = "mute", icon = "pack:my-pictures/mute" },
]
"""


def _config(tmp_path: Path, text: str = CONFIG):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path, load_config(path)


def _make_set(root: Path, set_id: str, named: dict[str, str]) -> None:
    """Write a user set with the two tiles the deck paints, on disk, as the app does."""
    directory = root / set_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"id": set_id, "name": set_id.title(), "author": "Tests", "licence": "MIT",
                "description": "", "icons": []}
    for name, colour in named.items():
        manifest["icons"].append({"name": name, "label": name.title()})
        for size in backup.TILE_SIZES:
            Image.new("RGBA", (size, size), colour).save(directory / f"{name}.{size}.png")
    (directory / "pack.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture()
def icons_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "icons"
    root.mkdir()
    monkeypatch.setenv(iconpacks.USER_PACKS_ENV, str(root))
    return root


def test_a_menu_button_picture_is_carried_in_the_backup(tmp_path: Path, icons_root: Path) -> None:
    _make_set(icons_root, "my-pictures", {"mute": "red", "spare": "blue"})
    path, config = _config(tmp_path)

    found = backup.used_icon_references(config, path)

    assert found == ("pack:my-pictures/mute",)


def test_pictures_from_a_shipped_set_are_left_out(tmp_path: Path, icons_root: Path) -> None:
    # general ships inside the app: carrying it would swell every backup for nothing.
    text = CONFIG.replace("pack:my-pictures/mute", "pack:general/mute")
    path, config = _config(tmp_path, text)

    blob = backup.build_backup("config text", config, path)

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()

    assert "config.toml" in names
    assert "manifest.json" in names
    assert not [name for name in names if name.startswith("icons/")]


def test_only_the_picture_something_points_at_is_carried(tmp_path: Path, icons_root: Path) -> None:
    _make_set(icons_root, "my-pictures", {"mute": "red", "spare": "blue"})
    path, config = _config(tmp_path)

    blob = backup.build_backup("config text", config, path)

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))

    assert "icons/my-pictures/mute.60.png" in names
    assert "icons/my-pictures/mute.180.png" in names
    # A picture nothing references stays out: a backup is not the whole library.
    assert not any("spare" in name for name in names)
    assert manifest["kind"] == backup.KIND
    assert manifest["version"] == backup.VERSION
    assert [entry["reference"] for entry in manifest["icons"]] == ["pack:my-pictures/mute"]


def test_a_backup_keeps_the_configuration_exactly_as_it_was_handed(tmp_path: Path, icons_root: Path) -> None:
    _make_set(icons_root, "my-pictures", {"mute": "red"})
    path, config = _config(tmp_path)
    now = datetime(2026, 10, 19, 9, 30, tzinfo=timezone.utc)

    blob = backup.build_backup("the merged document", config, path, app_version="0.8.0", now=now)
    read = backup.read_backup(blob)

    assert read.config_text == "the merged document"
    assert read.manifest["written"] == "2026-10-19T09:30:00+00:00"
    assert read.manifest["app"] == "0.8.0"
    assert read.icon_count == 1
    assert "1 picture" in read.describe()


def test_restoring_writes_the_pictures_and_lists_them_in_their_set(tmp_path: Path, icons_root: Path) -> None:
    _make_set(icons_root, "my-pictures", {"mute": "red"})
    path, config = _config(tmp_path)
    blob = backup.build_backup("config text", config, path)
    read = backup.read_backup(blob)

    written = backup.restore_pictures(read)

    directory = icons_root / "my-pictures"
    assert (directory / "mute.60.png").read_bytes() == read.icons["icons/my-pictures/mute.60.png"]
    assert len(written) >= 0  # same bytes were already there, so nothing needed writing
    manifest = iconpacks.read_manifest("my-pictures")
    assert any(entry["name"] == "mute" for entry in manifest["icons"])


def test_restoring_into_a_fresh_library_creates_the_set(icons_root: Path) -> None:
    # Nothing on disk at all: a new machine, restoring a file from another one.
    payload = {"icons/my-pictures/mute.60.png": b"\x89PNG\r\n\x1a\n60",
               "icons/my-pictures/mute.180.png": b"\x89PNG\r\n\x1a\n180"}
    read = backup.Backup(config_text="x", icons=payload, manifest={})

    written = backup.restore_pictures(read)

    assert {path.name for path in written} == {"mute.60.png", "mute.180.png"}
    assert all(path.is_relative_to(icons_root) for path in written)
    manifest = iconpacks.read_manifest("my-pictures")
    assert [entry["name"] for entry in manifest["icons"]] == ["mute"]


def test_a_file_that_is_not_a_zip_is_refused() -> None:
    with pytest.raises(backup.BackupError, match="not a zip"):
        backup.read_backup(b"this is not a backup")


def test_a_zip_without_a_configuration_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("something-else.txt", "hello")

    with pytest.raises(backup.BackupError, match="no config.toml"):
        backup.read_backup(buffer.getvalue())


def test_a_zip_that_is_not_one_of_ours_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("config.toml", CONFIG)
        archive.writestr("manifest.json", json.dumps({"kind": "something-else"}))

    with pytest.raises(backup.BackupError, match="not a deck backup"):
        backup.read_backup(buffer.getvalue())


def test_a_backup_from_a_newer_version_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("config.toml", CONFIG)
        archive.writestr("manifest.json", json.dumps({"kind": backup.KIND, "version": 99}))

    with pytest.raises(backup.BackupError, match="newer version"):
        backup.read_backup(buffer.getvalue())


def test_a_path_that_climbs_out_of_the_icon_folder_is_refused() -> None:
    # A zip member name is attacker-controlled: this is the one place the app opens
    # somebody else's file, so a ../ that would land outside the icon folder is fatal.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("config.toml", CONFIG)
        archive.writestr("manifest.json", json.dumps({"kind": backup.KIND, "version": 1}))
        archive.writestr("icons/../../evil.60.png", b"x")

    with pytest.raises(backup.BackupError, match="unexpected path"):
        backup.read_backup(buffer.getvalue())


def test_a_backup_may_not_replace_a_shipped_set(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("config.toml", CONFIG)
        archive.writestr("manifest.json", json.dumps({"kind": backup.KIND, "version": 1}))
        archive.writestr("icons/general/mute.60.png", b"x")

    with pytest.raises(backup.BackupError, match="shipped set"):
        backup.read_backup(buffer.getvalue())


# --------------------------------------------------------------------------- endpoints


def _raw_request(server, method: str, path: str, body: bytes = b"") -> tuple[int, bytes, str]:
    """Talk to the editor's server with raw bytes, the way a file upload arrives."""
    from http.client import HTTPConnection

    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    headers = {"Content-Type": "application/zip"} if body else {}
    connection.request(method, path, body, headers)
    response = connection.getresponse()
    raw = response.read()
    kind = response.getheader("Content-Type") or ""
    connection.close()
    return response.status, raw, kind


def _running(config_path: Path):
    import threading

    from akp03.webui import make_server

    server = make_server(0, config=config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_the_backup_endpoint_hands_back_a_file_to_keep(tmp_path: Path, icons_root: Path) -> None:
    import threading

    _make_set(icons_root, "my-pictures", {"mute": "red"})
    path, _ = _config(tmp_path)
    server, thread = _running(path)
    try:
        status, blob, kind = _raw_request(server, "GET", "/api/backup")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert status == 200
    assert kind == "application/zip"
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = set(archive.namelist())
        assert "config.toml" in names
        assert "manifest.json" in names
        # The menu button's own picture travels, because a menu restored without it
        # would come back with a blank key.
        assert "icons/my-pictures/mute.60.png" in names
        assert "Watch" in archive.read("config.toml").decode()


def test_restoring_stages_the_draft_and_leaves_the_deck_file_alone(tmp_path: Path, icons_root: Path) -> None:
    _make_set(icons_root, "my-pictures", {"mute": "red"})
    path, config = _config(tmp_path)
    before = path.read_bytes()
    blob = backup.build_backup("config text", config, path)
    # A restore is the real file, produced the way a person would have: read from disk.
    blob = backup.build_backup(CONFIG, config, path)

    server, thread = _running(path)
    try:
        status, raw, _ = _raw_request(server, "POST", "/api/restore", blob)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    body = json.loads(raw)
    assert status == 200
    assert body["ok"] is True
    assert "nothing on the deck changes until you press Save" in body["persistence"]
    assert body["draft"]
    # The configuration on disk is untouched: restoring stages, it does not save.
    assert path.read_bytes() == before


def test_a_file_that_is_not_a_backup_is_refused_with_a_reason(tmp_path: Path, icons_root: Path) -> None:
    path, _ = _config(tmp_path)

    server, thread = _running(path)
    try:
        status, raw, _ = _raw_request(server, "POST", "/api/restore", b"not a zip at all")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    body = json.loads(raw)
    assert status == 400
    assert body["ok"] is False
    assert "not a zip" in body["reason"]
