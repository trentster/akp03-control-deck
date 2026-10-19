"""Pack a set up, take a set in — and every way it has to refuse.

An export that will not import is worse than no export, and an import that lands under a
guessed name is worse than none: the round trip is checked, and so is each refusal.
"""
from __future__ import annotations

import base64
import io
import json
import threading
import zipfile
from http.client import HTTPConnection
from pathlib import Path

import pytest
from PIL import Image

from akp03.webui import make_server


def tile(size: int, colour=(220, 90, 60, 255)) -> bytes:
    image = Image.new("RGBA", (size, size), colour)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def picture(size: int = 200, colour=(90, 160, 220, 255)) -> bytes:
    image = Image.new("RGBA", (size, size), colour)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def icon_payload(name: str = "tomato", label: str = "Tomato", *, sizes=(60, 180)) -> dict:
    return {"name": name, "label": label, "hue": "#cc5522",
            "tiles": {str(size): base64.b64encode(tile(size)).decode() for size in sizes}}


def pack_zip(folder: str = "handmade", *, manifest: dict | None = None,
             files: dict[str, bytes] | None = None, extra: dict[str, bytes] | None = None) -> bytes:
    """A zip built by hand, so a test can be wrong in exactly one way."""
    manifest = manifest if manifest is not None else {
        "id": folder, "name": "Hand Made", "author": "A Person", "licence": "CC0",
        "description": "test", "icons": [{"name": "find", "label": "Find"}],
    }
    files = files if files is not None else {
        f"{folder}/find.60.png": tile(60), f"{folder}/find.180.png": tile(180),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{folder}/pack.json", json.dumps(manifest, indent=2))
        for name, blob in files.items():
            archive.writestr(name, blob)
        for name, blob in (extra or {}).items():
            archive.writestr(name, blob)
    return buffer.getvalue()


def request(server, method: str, path: str, body: dict | None = None):
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    connection.request(method, path, payload, headers)
    response = connection.getresponse()
    raw = response.read()
    kind = response.getheader("Content-Type") or ""
    connection.close()
    if kind.startswith("application/zip"):
        return response.status, raw
    return response.status, (json.loads(raw) if raw and raw.strip().startswith(b"{") else {})


def running_server():
    server = make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture()
def icons_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "icons"
    home.mkdir()
    monkeypatch.setenv("AKP03_ICONS_DIR", str(home))
    return home


@pytest.fixture()
def server(icons_home: Path):
    server, thread = running_server()
    yield server
    server.shutdown()
    thread.join(timeout=3)


# ── the round trip ───────────────────────────────────────────────────────────

def test_a_set_exported_and_imported_comes_back_the_same(server, icons_home: Path) -> None:
    """The whole point: what leaves as a zip returns as the set it came from."""
    request(server, "POST", "/api/icons", {"op": "add", "set": "my-things",
                                           "set_name": "Workshop", "icons": [icon_payload()]})
    status, blob = request(server, "GET", "/api/icons/export?set=my-things")
    assert status == 200 and isinstance(blob, bytes) and blob[:2] == b"PK", "a real zip comes back"

    status, gone = request(server, "POST", "/api/icons", {"op": "set-delete", "set": "my-things"})
    assert gone["ok"] is True
    assert not (icons_home / "my-things").exists()

    status, payload = request(server, "POST", "/api/icons",
                              {"op": "import", "data": base64.b64encode(blob).decode()})
    assert status == 200 and payload["ok"] is True, payload
    assert payload["set"] == "my-things"
    pack = [p for p in payload["library"]["packs"] if p["id"] == "my-things"][0]
    assert pack["name"] == "Workshop", "the set's name travels with it"
    assert [icon["label"] for icon in pack["icons"]] == ["Tomato"]
    assert pack["icons"][0]["reference"] == "pack:my-things/tomato"
    assert (icons_home / "my-things" / "tomato.60.png").is_file()
    assert (icons_home / "my-things" / "tomato.180.png").is_file()


def test_a_set_that_ships_with_the_app_can_be_exported_and_taken_in(server, icons_home: Path) -> None:
    """Taking General away with you is a reasonable thing to want."""
    status, blob = request(server, "GET", "/api/icons/export?set=general")
    assert status == 200 and blob[:2] == b"PK"
    status, payload = request(server, "POST", "/api/icons",
                              {"op": "import", "data": base64.b64encode(blob).decode()})
    assert status == 200 and payload["ok"] is True, payload
    assert payload["set"] == "general-2", "the shipped id is never taken from under it"
    assert any("already a set" in note for note in payload["notes"])
    arrived = [p for p in payload["library"]["packs"] if p["id"] == "general-2"][0]
    assert arrived["name"] == "General" and len(arrived["icons"]) == 24
    shipped = [p for p in payload["library"]["packs"] if p["id"] == "general"][0]
    assert len(shipped["icons"]) == 24, "and the shipped set is untouched"


def test_an_import_never_replaces_a_set_that_ships_with_the_app(server, icons_home: Path) -> None:
    shipped = Path(__file__).resolve().parents[1] / "akp03" / "icons" / "macos"
    before = sorted(p.name for p in shipped.iterdir())
    status, payload = request(server, "POST", "/api/icons", {
        "op": "import", "data": base64.b64encode(pack_zip("macos")).decode()})
    assert status == 200 and payload["ok"] is True, payload
    assert payload["set"] == "macos-2"
    assert sorted(p.name for p in shipped.iterdir()) == before, "the shipped files are untouched"


# ── the ways it must refuse, and leave nothing behind ────────────────────────

def test_a_pack_whose_folder_and_id_disagree_is_refused(server, icons_home: Path) -> None:
    blob = pack_zip("handmade", manifest={"id": "something-else", "name": "Hand Made",
                                          "author": "A Person", "licence": "CC0",
                                          "icons": [{"name": "find", "label": "Find"}]})
    status, payload = request(server, "POST", "/api/icons",
                              {"op": "import", "data": base64.b64encode(blob).decode()})
    assert payload.get("ok") is not True
    assert "something-else" in json.dumps(payload) and "handmade" in json.dumps(payload)
    assert list(icons_home.iterdir()) == [], "nothing is left behind"


def test_a_pack_missing_a_file_is_refused_whole(server, icons_home: Path) -> None:
    blob = pack_zip("handmade", files={"handmade/find.60.png": tile(60)})   # no 180 tile
    status, payload = request(server, "POST", "/api/icons",
                              {"op": "import", "data": base64.b64encode(blob).decode()})
    assert payload.get("ok") is not True
    assert "find" in json.dumps(payload)
    assert list(icons_home.iterdir()) == [], "a half-imported set is never left standing"


def test_a_pack_without_a_licence_is_refused(server, icons_home: Path) -> None:
    blob = pack_zip("handmade", manifest={"id": "handmade", "name": "Hand Made",
                                          "author": "A Person", "licence": "",
                                          "icons": [{"name": "find", "label": "Find"}]})
    status, payload = request(server, "POST", "/api/icons",
                              {"op": "import", "data": base64.b64encode(blob).decode()})
    assert payload.get("ok") is not True and "licence" in json.dumps(payload)
    assert list(icons_home.iterdir()) == []


def test_an_archive_that_reaches_outside_its_folder_is_refused(server, icons_home: Path) -> None:
    blob = pack_zip("handmade", extra={"handmade/../../escaped.png": tile(60)})
    status, payload = request(server, "POST", "/api/icons",
                              {"op": "import", "data": base64.b64encode(blob).decode()})
    assert payload.get("ok") is not True
    assert "outside" in json.dumps(payload)
    assert not (icons_home.parent / "escaped.png").exists()


def test_something_that_is_not_a_zip_is_refused(server, icons_home: Path) -> None:
    status, payload = request(server, "POST", "/api/icons",
                              {"op": "import", "data": base64.b64encode(b"not a zip at all").decode()})
    assert payload.get("ok") is not True
    assert "zip" in json.dumps(payload).lower()


# ── forgiving where it costs nothing ─────────────────────────────────────────

def test_a_pack_may_carry_one_square_picture_instead_of_two_tiles(server, icons_home: Path) -> None:
    """The same promise Add icons makes: one square picture, and the tiles are made here."""
    blob = pack_zip("handmade", files={"handmade/find.png": picture(200)})
    status, payload = request(server, "POST", "/api/icons",
                              {"op": "import", "data": base64.b64encode(blob).decode()})
    assert status == 200 and payload["ok"] is True, payload
    assert (icons_home / "handmade" / "find.60.png").is_file()
    assert (icons_home / "handmade" / "find.180.png").is_file()
    with Image.open(icons_home / "handmade" / "find.60.png") as made:
        assert made.size == (60, 60) and made.mode == "RGBA"
    assert any("one picture" in note for note in payload["notes"])


def test_macos_junk_in_the_archive_is_ignored(server, icons_home: Path) -> None:
    blob = pack_zip("handmade", extra={
        "__MACOSX/._find.60.png": b"junk",
        "handmade/.DS_Store": b"junk",
        "handmade/notes.txt": b"junk",
    })
    status, payload = request(server, "POST", "/api/icons",
                              {"op": "import", "data": base64.b64encode(blob).decode()})
    assert status == 200 and payload["ok"] is True, payload
    assert not (icons_home / "handmade" / "notes.txt").exists(), "notes are not icons"
    assert (icons_home / "handmade" / "find.60.png").is_file()


# ── one icon, into the set you name ──────────────────────────────────────────

def test_one_icon_lands_in_the_set_you_name_and_nowhere_else(server, icons_home: Path) -> None:
    """Adding a picture takes the set you are in, and never a set it guesses at."""
    request(server, "POST", "/api/icons", {"op": "add", "set": "my-things",
                                           "set_name": "My Things", "icons": [icon_payload("first")]})
    status, payload = request(server, "POST", "/api/icons", {
        "op": "add", "set": "workshop", "set_name": "Workshop",
        "icons": [icon_payload("spanner", "Spanner")]})
    assert status == 200 and payload["ok"] is True, payload
    packs = {pack["id"]: pack for pack in payload["library"]["packs"]}
    assert [icon["name"] for icon in packs["workshop"]["icons"]] == ["spanner"]
    assert packs["workshop"]["name"] == "Workshop"
    assert [icon["name"] for icon in packs["my-things"]["icons"]] == ["first"], "the other set is untouched"
    assert (icons_home / "workshop" / "spanner.60.png").is_file()
    assert not (icons_home / "my-things" / "spanner.60.png").exists()
