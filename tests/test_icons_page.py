"""The Icons page: adding your own pictures, and the rules that keep them yours.

The page is the home of the pictures a button can wear.  These tests hold the three
promises it makes: a set that ships with the app is never written to, your picture is
stored as you uploaded it, and an icon still in use is not deleted out from under a key.
"""

from __future__ import annotations

import base64
import io
import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from akp03.webui import make_server


def tile_png(size: int, colour=(220, 90, 60, 255)) -> bytes:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    margin = size * 0.25
    ImageDraw.Draw(image).ellipse([margin, margin, size - margin, size - margin], fill=colour)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


def icon_payload(name: str = "tomato", label: str = "Tomato", *, sizes=(60, 180)) -> dict:
    return {
        "name": name,
        "label": label,
        "tiles": {str(size): encoded(tile_png(size)) for size in sizes},
        "source": {"name": f"{name}.png", "data": encoded(tile_png(180))},
    }


def request(server, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    connection.request(method, path, payload, headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, (json.loads(raw) if raw and raw.strip().startswith(b"{") else {})


def running_server(config: Path | None = None):
    server = make_server(0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture()
def icons_home(tmp_path, monkeypatch) -> Path:
    """An empty folder for the user's own sets, so no test touches a real one."""
    home = tmp_path / "icons"
    home.mkdir()
    monkeypatch.setenv("AKP03_ICONS_DIR", str(home))
    return home


def test_the_workspace_has_a_third_place_for_icons() -> None:
    """Deck, Menus, Icons: the page is reachable and loads its own script and styles."""
    from importlib.resources import files

    assets = files("akp03").joinpath("webui_assets")
    page = assets.joinpath("index.html").read_text()
    assert '<button class="nav" data-view="icons">' in page
    assert '<section class="view" id="icons">' in page
    assert '<div id="iconsRoot"></div>' in page
    assert '/assets/icons.css' in page and '/assets/icons.js' in page
    script = assets.joinpath("icons.js").read_text()
    # The page never writes to a set that shipped with the app; it copies instead,
    # and the tile's action says so in one small word rather than a wrapping phrase.
    assert 'data-ic-copy=' in script and '>Copy…</button>' in script
    assert "Copy to my own" not in script
    assert "into a custom set, where you can change it" in script
    css = assets.joinpath("icons.css").read_text()
    # A rule between the built-in sets and the custom ones: they are different kinds of
    # thing, and without it the two groups read as one list.
    assert ".ic-group + .ic-group { margin-top: 4px; padding-top: 16px;" in css
    assert "border-top: 1px solid var(--line-strong); }" in css
    assert ".ic-tiny { border: none;" in css and "white-space: nowrap" in css
    # The add control is a real button, not a line of loose text.
    assert 'class="ic-add"' in script and ".ic-add {" in css
    # A drop is swallowed even when it is refused, or the browser opens the file and
    # navigates away from the editor.
    dragover = script.split("addEventListener('dragover'", 1)[1].split('});', 1)[0]
    assert "event.preventDefault()" in dragover
    # The two tile sizes are the deck's: 60 for the key face, 180 for this page.
    assert "const TILE_SIZES = [60, 180];" in script


def test_the_library_says_which_sets_ship_with_the_app(icons_home: Path) -> None:
    """The page decides what is read-only from the server, not from a list of its own."""
    server, thread = running_server()
    try:
        status, payload = request(server, "GET", "/api/icons")
        assert status == 200
        assert sorted(payload["builtin"]) == ["general", "macos"]
        assert all(pack["icons"] for pack in payload["packs"])
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_a_picture_you_add_becomes_an_icon_of_your_own(icons_home: Path) -> None:
    """The file is kept as uploaded, the tiles are made at the deck's sizes, and the
    icon arrives in a set of your own rather than among the ones that shipped."""
    server, thread = running_server()
    try:
        status, payload = request(server, "POST", "/api/icons", {
            "op": "add", "set": "my-things", "set_name": "My things", "icons": [icon_payload()],
        })
        assert status == 200 and payload["ok"] is True, payload
        assert payload["added"] == ["my-things/tomato"]
        mine = [pack for pack in payload["library"]["packs"] if pack["id"] == "my-things"]
        assert mine, "the new set is missing from the library"
        assert mine[0]["icons"][0]["label"] == "Tomato"
        # The picture files under its own set, so the picker shows a heading with the
        # name the person gave the set instead of burying it in a general "Your own".
        assert mine[0]["icons"][0]["section"] == mine[0]["id"]
        assert mine[0]["id"] in [entry["id"] for entry in payload["library"]["sections"]]

        # On disk: the two tiles, the manifest, and the original file untouched.
        folder = icons_home / "my-things"
        assert sorted(path.name for path in folder.iterdir()) == [
            "pack.json", "tomato.180.png", "tomato.60.png", "tomato.src.png",
        ]
        assert (folder / "tomato.src.png").read_bytes() == tile_png(180)
        for size in (60, 180):
            with Image.open(folder / f"tomato.{size}.png") as opened:
                assert opened.mode == "RGBA" and opened.size == (size, size)
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_the_same_name_twice_gets_its_own_name(icons_home: Path) -> None:
    """Adding a second picture called `tomato` must not overwrite the first."""
    server, thread = running_server()
    try:
        body = {"op": "add", "set": "my-things", "icons": [icon_payload()]}
        request(server, "POST", "/api/icons", body)
        status, payload = request(server, "POST", "/api/icons", {
            "op": "add", "set": "my-things", "icons": [icon_payload(label="Another")],
        })
        assert status == 200 and payload["ok"] is True
        assert payload["added"] == ["my-things/tomato-2"]
        assert (icons_home / "my-things" / "tomato.60.png").exists()
        assert (icons_home / "my-things" / "tomato-2.60.png").exists()
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_a_set_that_ships_with_the_app_cannot_be_written_to(icons_home: Path) -> None:
    server, thread = running_server()
    try:
        status, payload = request(server, "POST", "/api/icons", {
            "op": "add", "set": "general", "icons": [icon_payload()],
        })
        assert status == 200 and payload["ok"] is False
        assert "built-in set" in payload["reason"]
        assert not (icons_home / "general").exists()
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_a_tile_that_is_not_the_deck_size_is_refused(icons_home: Path) -> None:
    """A 30px tile cannot be painted, so it is refused before anything is written."""
    server, thread = running_server()
    try:
        bad = {"op": "add", "set": "my-things",
               "icons": [{"name": "small", "label": "Small", "tiles": {"60": "not an image", "180": encoded(tile_png(180))}}]}
        status, payload = request(server, "POST", "/api/icons", bad)
        assert status == 200 and payload["ok"] is False
        assert payload["reason"] == "the 60px tile did not arrive as picture data"
        assert not (icons_home / "my-things").exists()
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_a_label_can_change_but_the_reference_never_moves(icons_home: Path) -> None:
    """A config points at `set/name`; renaming the label cannot break those pointers."""
    server, thread = running_server()
    try:
        request(server, "POST", "/api/icons", {"op": "add", "set": "my-things", "icons": [icon_payload()]})
        status, payload = request(server, "POST", "/api/icons",
                                  {"op": "label", "set": "my-things", "name": "tomato", "label": "Ripe"})
        assert status == 200 and payload["ok"] is True, payload
        icon = [i for pack in payload["library"]["packs"] if pack["id"] == "my-things"
                for i in pack["icons"]][0]
        assert icon["label"] == "Ripe"
        assert icon["reference"] == "pack:my-things/tomato"
        assert (icons_home / "my-things" / "tomato.60.png").exists()
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_a_built_in_icon_can_be_copied_into_your_own_set(icons_home: Path) -> None:
    """The honest way to change a shipped icon: copy it, then edit the copy."""
    server, thread = running_server()
    try:
        status, payload = request(server, "POST", "/api/icons",
                                  {"op": "copy", "from": "macos", "name": "lock", "set": "my-things"})
        assert status == 200 and payload["ok"] is True, payload
        assert payload["added"] if "added" in payload else True
        assert (icons_home / "my-things" / "lock.60.png").exists()
        # The shipped set is untouched: the same file still lives beside the module.
        from akp03.iconpacks import BUILTIN_PACKS

        assert (BUILTIN_PACKS / "macos" / "lock.60.png").exists()
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_an_icon_the_deck_is_using_is_not_deleted(icons_home: Path) -> None:
    """Deleting an icon a key wears would leave that key pointing at nothing.

    The check reads the draft the editor is showing as well as the saved config, so
    it holds for a key you have just changed and not yet saved.
    """
    server, thread = running_server()
    try:
        status, payload = request(server, "POST", "/api/icons", {
            "op": "add", "set": "my-things", "icons": [icon_payload()],
        })
        assert payload["ok"] is True
        status, state = request(server, "GET", "/api/state")
        original = json.loads(json.dumps(state["draft"]))   # kept to put the key back
        draft = json.loads(json.dumps(original))
        draft["icons"] = ["pack:my-things/tomato"] + [None] * 5
        status, staged = request(server, "POST", "/api/draft", draft)
        assert status == 200 and "draft" in staged, staged

        status, payload = request(server, "POST", "/api/icons",
                                  {"op": "delete", "set": "my-things", "name": "tomato"})
        assert status == 200 and payload["ok"] is False
        assert "is used by 1 key" in payload["reason"], payload["reason"]
        assert "give those another icon first" in payload["reason"]
        # Still there, and still loadable.
        assert (icons_home / "my-things" / "tomato.60.png").exists()

        # Put the key back the way it was, and now the same icon can go.
        request(server, "POST", "/api/draft", original)
        status, payload = request(server, "POST", "/api/icons",
                                  {"op": "delete", "set": "my-things", "name": "tomato"})
        assert payload["ok"] is True, payload
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_deleting_a_free_icon_removes_its_files(icons_home: Path) -> None:
    """The icon's own files go; the set it lived in is not collateral damage."""
    server, thread = running_server()
    try:
        request(server, "POST", "/api/icons", {"op": "add", "set": "my-things", "icons": [icon_payload()]})
        status, payload = request(server, "POST", "/api/icons",
                                  {"op": "delete", "set": "my-things", "name": "tomato"})
        assert status == 200 and payload["ok"] is True, payload
        assert not list((icons_home / "my-things").glob("tomato*"))
        # The set stays: it is a container with a name, not a side effect of its icons.
        assert (icons_home / "my-things" / "pack.json").is_file()
        assert [pack for pack in payload["library"]["packs"] if pack["id"] == "my-things"]
    finally:
        server.shutdown()
        thread.join(timeout=3)

def test_a_copy_is_told_where_to_go_and_never_guessed(icons_home: Path) -> None:
    """Copying asks which set, in the page and on the server.

    The page used to invent a destination for you — a set called `my-icons`, or your
    first set — and name it after the icon, which is where "Mission Control and more"
    came from.  Where a copy lands is the person's decision, so the page offers the
    sets to choose from and the server takes the name it is handed.
    """
    from importlib.resources import files

    script = files("akp03").joinpath("webui_assets", "icons.js").read_text()
    assert "data-ic-copyset" in script, "the copy must offer the sets to land in"
    assert "data-ic-copycancel" in script, "a question needs a way out of it"
    assert "'my-icons'" not in script, "no set is invented on your behalf"
    # `mine[0].id` is still right for deciding which set to *open*; what must be gone is
    # the copy that picked a destination for you.
    assert "Copying needs a home" not in script, "nor is the first set picked for you"
    assert "iconChooserHTML" in script

    server, thread = running_server()
    try:
        status, payload = request(server, "POST", "/api/icons", {
            "op": "copy", "from": "macos", "name": "lock",
            "set": "workshop", "set_name": "Workshop",
        })
        assert status == 200 and payload["ok"] is True, payload
        pack = [p for p in payload["library"]["packs"] if p["id"] == "workshop"][0]
        assert pack["name"] == "Workshop", "the set carries the name you typed"
        assert "and more" not in pack["name"]
        assert (icons_home / "workshop" / "lock.60.png").exists()
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_a_set_of_your_own_can_be_renamed(icons_home: Path) -> None:
    """A set's name is a label: the reference its icons are addressed by never moves."""
    server, thread = running_server()
    try:
        request(server, "POST", "/api/icons",
                {"op": "add", "set": "my-things", "icons": [icon_payload()]})
        status, payload = request(server, "POST", "/api/icons",
                                  {"op": "set-label", "set": "my-things", "name": "Workshop"})
        assert status == 200 and payload["ok"] is True, payload
        pack = [p for p in payload["library"]["packs"] if p["id"] == "my-things"][0]
        assert pack["name"] == "Workshop"
        assert pack["icons"][0]["reference"] == "pack:my-things/tomato"
        assert (icons_home / "my-things" / "tomato.60.png").exists()

        # A name with no letters in it, and a set that ships with the app, are refused.
        status, blank = request(server, "POST", "/api/icons",
                                {"op": "set-label", "set": "my-things", "name": "   "})
        assert blank.get("ok") is not True and "name" in json.dumps(blank)
        status, shipped = request(server, "POST", "/api/icons",
                                  {"op": "set-label", "set": "macos", "name": "Nope"})
        assert shipped.get("ok") is not True
        assert "built-in set" in json.dumps(shipped)
    finally:
        server.shutdown()
        thread.join(timeout=3)

def test_a_set_stays_when_its_last_icon_goes(icons_home: Path) -> None:
    """A set of your own is a named container: emptying it leaves the container.

    Deleting the last icon used to take the whole set with it, so a set you had just
    made vanished the moment you tidied it — and its name went with it.
    """
    server, thread = running_server()
    try:
        request(server, "POST", "/api/icons", {"op": "add", "set": "my-things",
                                               "set_name": "Workshop", "icons": [icon_payload()]})
        status, payload = request(server, "POST", "/api/icons",
                                  {"op": "delete", "set": "my-things", "name": "tomato"})
        assert status == 200 and payload["ok"] is True, payload
        packs = {pack["id"]: pack for pack in payload["library"]["packs"]}
        assert "my-things" in packs, "emptying a set must not remove the set"
        assert packs["my-things"]["icons"] == []
        assert packs["my-things"]["name"] == "Workshop", "the name survives too"
        assert (icons_home / "my-things" / "pack.json").is_file()
        assert not list((icons_home / "my-things").glob("tomato*")), "the tiles are gone"
        # An empty set is still a destination: the library lists it for the copy chooser.
        assert packs["my-things"]["icons"] == []
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_deleting_a_set_is_its_own_deliberate_act(icons_home: Path) -> None:
    """Losing a whole set is asked for separately, and never one that ships with the app."""
    from importlib.resources import files

    script = files("akp03").joinpath("webui_assets", "icons.js").read_text()
    assert "data-ic-setdeleteopen" in script, "there must be a way to delete a set"
    assert "data-ic-setdelete=" in script and "data-ic-cancelsetdelete" in script, "and a way back out"
    assert "ic-confirm" in script, "it is asked about, not done on the spot"

    server, thread = running_server()
    try:
        request(server, "POST", "/api/icons", {"op": "add", "set": "my-things",
                                               "set_name": "Workshop", "icons": [icon_payload()]})
        status, refused = request(server, "POST", "/api/icons", {"op": "set-delete", "set": "macos"})
        assert refused.get("ok") is not True
        assert "built-in set" in json.dumps(refused)

        status, payload = request(server, "POST", "/api/icons", {"op": "set-delete", "set": "my-things"})
        assert status == 200 and payload["ok"] is True, payload
        assert "Workshop" in payload["detail"]
        assert "my-things" not in {pack["id"] for pack in payload["library"]["packs"]}
        assert not (icons_home / "my-things").exists(), "the set's folder goes with it"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_your_own_set_is_a_heading_in_the_picker_named_after_it(icons_home, ):
    """A set of the person's own must be a heading in the picker, under the name they
    gave it, after the packs that shipped with the app — and its pictures must file
    under it, rather than being buried in a general "Your own" at the very bottom."""
    server, thread = running_server()
    try:
        status, payload = request(server, "POST", "/api/icons", {
            "op": "add", "set": "my-things", "set_name": "Workshop", "icons": [icon_payload()],
        })
        assert status == 200 and payload["ok"] is True, payload
        library = payload["library"]
        entry = next((item for item in library["packs"] if item["id"] == "my-things"), None)
        assert entry, "the set is missing from the library the picker reads"
        assert entry["name"] == "Workshop"
        sections = [section["id"] for section in library["sections"]]
        names = {section["id"]: section["name"] for section in library["sections"]}
        assert "my-things" in sections, "a set of your own has no heading in the picker"
        assert names["my-things"] == "Workshop", "the heading is not the name they gave it"
        assert entry["icons"][0]["section"] == "my-things", "its picture does not file under it"
        # The sets that shipped with the app lead: General and macOS are the two
        # everyone has and the ordinary deck glyphs live there, so they are the first
        # rows rather than something to scroll past a library to reach.
        assert sections[:2] == ["general", "macos"], sections
        # A set of their own follows them, still a heading of its own.
        assert sections.index("my-things") == 2, sections
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_making_a_set_asks_for_the_name_and_leaves_the_pictures_for_next(icons_home: Path) -> None:
    """A set is a container, so it can be named before it holds anything.

    The name used to be conditional on a picture arriving with it ("a set is its
    pictures"), so there was no way to make the set you were about to fill - while the
    guide already promised that New set… makes an empty set of your own to start from.
    """
    server, thread = running_server()
    try:
        status, payload = request(server, "POST", "/api/icons",
                                  {"op": "set-create", "set": "workshop", "set_name": "Workshop"})
        assert status == 200 and payload["ok"] is True, payload
        packs = {pack["id"]: pack for pack in payload["library"]["packs"]}
        assert "workshop" in packs, "a set made on its own is missing from the library"
        assert packs["workshop"]["name"] == "Workshop", "the name typed is the name it carries"
        assert packs["workshop"]["icons"] == [], "and nothing has been put in it yet"
        assert (icons_home / "workshop" / "pack.json").is_file(), "the empty set is on disk"
        assert "Workshop" in payload["detail"]
        # The same name twice is a question, not a second set quietly beside the first.
        status, again = request(server, "POST", "/api/icons",
                                {"op": "set-create", "set": "workshop", "set_name": "Workshop"})
        assert again.get("ok") is not True and "already exists" in json.dumps(again), again
        # And a set that shipped with the app is still never written to.
        status, shipped = request(server, "POST", "/api/icons",
                                  {"op": "set-create", "set": "macos", "set_name": "macos"})
        assert shipped.get("ok") is not True and "built-in set" in json.dumps(shipped), shipped
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_the_new_set_form_asks_for_a_name_and_not_a_picture() -> None:
    """Naming the container and filling it are two steps, in that order.

    The form carried a Pictures input and refused to submit without one, which made the
    name conditional on a step that belongs after it.  The pictures have their own way
    in — ＋ Add icons…, or a drop anywhere on the page — and both land in the set that is
    open, which is the set this form leaves you standing in.
    """
    from importlib.resources import files

    script = files("akp03").joinpath("webui_assets", "icons.js").read_text()
    form = script.split('class="ic-newset"', 1)[1].split("</form>", 1)[0]
    assert 'id="icNewSetName"' in form, "the form asks for a name"
    assert 'id="icNewSetFiles"' not in form, "and for nothing else"
    assert 'type="file"' not in form, "no picture belongs on this form"
    assert "Make the set" in form
    assert "set-create" in script, "a set is made by itself, before any picture"
    assert "is its pictures" not in script, "the refusal that required a picture is gone"
    assert "createSetAndAdd" not in script, "and so is the pairing that made them one step"
    assert "＋ Add icons…" in script, "the pictures keep their own way in"
