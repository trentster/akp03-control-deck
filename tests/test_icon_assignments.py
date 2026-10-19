"""Icons assigned to keys: staged in the draft, materialised only when pushed.

Mark's requirement is the shape of these tests: the library in the editor and the
artwork on the deck are separate things, an assignment is a *reference*, and no
file or configuration byte changes until the draft is pushed.
"""
from __future__ import annotations

import io
import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from akp03 import artwork, iconpacks
from akp03.config import load_config, managed_image_path
from akp03.images import to_jpeg
from akp03.webui import ControlPlaneEditorError, DraftStore, make_server, merge_draft_into_config, parse_draft

COPY = "pack:general/copy"
PASTE = "pack:general/paste"
NEW_WINDOW = "pack:macos/new-window"

CONFIG = '''[actions.undo]
action = "keys"
keys = ["cmd", "z"]
[actions.copy]
action = "keys"
keys = ["cmd", "c"]
[labels]
key0 = "Undo"
key1 = "Copy"
[bindings.key0]
action_id = "undo"
[bindings.key1]
action_id = "copy"
'''

WITH_ART = CONFIG + '''
[images]
key0 = "key0.jpg"
'''


def write_config(tmp_path: Path, text: str = CONFIG, *, art_for: tuple[int, ...] = ()) -> Path:
    directory = tmp_path
    path = directory / "config.toml"
    path.write_text(text)
    path.chmod(0o600)
    for key in art_for:
        images = directory / "images"
        images.mkdir(mode=0o700, exist_ok=True)
        asset = managed_image_path(path, key)
        asset.write_bytes(artwork.find_icon(COPY).jpeg())
        asset.chmod(0o600)
    return path


def draft_document(icons: list[Any] | None = None) -> dict:
    document = {
        "actions": [{"id": "undo", "kind": "keys", "keys": ["cmd", "z"]}],
        "bindings": ["undo", None, None, None, None, None],
        "dials": [None] * 8,
        "menus": [],
    }
    if icons is not None:
        document["icons"] = icons
    return document


# --- references ---------------------------------------------------------------

def test_assignments_accept_library_and_managed_references() -> None:
    parsed = artwork.parse_assignments([COPY, "file:key1.jpg", None, PASTE, None, NEW_WINDOW])
    assert parsed == (COPY, "file:key1.jpg", None, PASTE, None, NEW_WINDOW)


@pytest.mark.parametrize("bad, why", [
    (["pack:general/nope"] * 6, "no such icon"),
    (["pack:nosuchpack/copy"] * 6, "no such pack"),
    (["pack:general/copy"] * 5, "too few entries"),
    (["pack:general/copy"] * 7, "too many entries"),
    (["copy"] * 6, "unprefixed reference"),
    (["file:key0.jpg", "file:key0.jpg", None, None, None, None], "a key claiming another key's file"),
    (["file:../../../../etc/passwd", None, None, None, None, None], "path traversal"),
    (["file:/etc/passwd", None, None, None, None, None], "absolute path"),
    (["pack:general/copy.png", None, None, None, None, None], "an image URL rather than a reference"),
    ([42, None, None, None, None, None], "a non-string entry"),
])
def test_assignments_refuse_anything_but_a_real_reference(bad: list, why: str) -> None:
    with pytest.raises(artwork.ArtworkError):
        artwork.parse_assignments(bad)


def test_assignments_note_a_missing_library_icon_by_name() -> None:
    with pytest.raises(artwork.ArtworkError, match="no icon named"):
        artwork.parse_assignments([COPY, None, None, None, None, "pack:general/not-a-thing"])


def test_the_library_offers_both_packs_with_previews() -> None:
    payload = artwork.library_payload()
    packs = {pack["id"]: pack for pack in payload["packs"]}
    assert set(packs) == {"general", "macos"}
    assert len(packs["general"]["icons"]) >= 20
    assert packs["general"]["licence"].startswith("MIT")
    copy = next(icon for icon in packs["general"]["icons"] if icon["name"] == "copy")
    assert copy["reference"] == COPY
    assert copy["preview"] == "/api/icons/general/copy.png"
    assert copy["hue"].startswith("#")


@pytest.fixture(autouse=True)
def _no_user_packs(tmp_path_factory, monkeypatch):
    """Keep the machine's own icon folder out of these tests.

    Without this they describe the library that ships with the app only until the
    person running them has an icon of their own: the extra set changes the answer.
    """
    monkeypatch.setenv("AKP03_ICONS_DIR", str(tmp_path_factory.mktemp("icons")))


def test_every_picture_files_under_its_own_set_named_as_the_set_is_named() -> None:
    """The picture picker heads its pictures by set, under the set's own name.

    It used to head them by purpose alone — Editing, Windows & apps, Sound & media,
    System — which left the base sets unnamed.  Nothing in the picker ever said
    "General" or "macOS", so a person could not find the pictures that ship with the
    app by set at all, and reported them as unselectable.
    """
    payload = artwork.library_payload()
    names = {section["id"]: section["name"] for section in payload["sections"]}
    assert names, "the picker has no headings at all"

    shipped = [pack for pack in payload["packs"] if pack["id"] in payload["builtin"]]
    assert {pack["id"] for pack in shipped} == {"general", "macos"}
    for pack in shipped:
        assert names[pack["id"]] == pack["name"], "a shipped set is not named in the picker"
    for pack in payload["packs"]:
        assert names[pack["id"]] == pack["name"], f"{pack['id']} is headed differently"
        for icon in pack["icons"]:
            assert icon["section"] == pack["id"], f"{icon['reference']} does not file under its set"

    # Every picture is filed exactly once, under a heading that exists.
    filed = {}
    for pack in payload["packs"]:
        for icon in pack["icons"]:
            filed[icon["section"]] = filed.get(icon["section"], 0) + 1
    assert set(filed) <= set(names), "a picture files under a heading the picker never shows"
    assert sum(filed.values()) == 48


def test_each_picture_group_is_a_block_with_one_rule_between_it_and_the_next() -> None:
    """The separator between picture groups is drawn on the *following* block.

    A quiet rule between groups is what lets the eye see where one set of pictures
    ends.  Drawing it between siblings - rather than closing off every group - keeps
    a line from trailing the last group, which would read as an unfinished edge.
    """
    assets = Path(__file__).resolve().parents[1] / "akp03" / "webui_assets"
    picker = (assets / "library.js").read_text()
    css = (assets / "library.css").read_text()

    # A group is one block: its heading and its pictures together.
    assert 'class="lib-block"' in picker
    assert ".lib-block + .lib-block" in css

    # The rule is on the top edge of the following block, never the bottom of a
    # group, and it is the same quiet grey the panel separates things with - now
    # named, so it cannot drift away from the token the panel uses.
    between = css.split(".lib-block + .lib-block")[1].split("}")[0]
    assert "border-top: 1px solid var(--soft)" in between
    assert "border-bottom" not in between

    # The earlier half-measure, which only closed off the "No picture" row, is gone.
    assert ".lib-heading.first" not in css and "lib-heading first" not in picker


def test_an_icon_of_your_own_can_be_copied_on_into_another_of_your_sets(tmp_path, monkeypatch) -> None:
    """A picture gathered out of an imported set is worth keeping in a set of your own.

    Copying used to be offered only for the built-in sets, so the icons someone curated
    from a library could not be gathered anywhere.  The copy works between your own sets
    now, and it is a copy: the set it came from keeps its icon.
    """
    monkeypatch.setenv(iconpacks.USER_PACKS_ENV, str(tmp_path / "icons"))
    tiles = {60: artwork.tile_png(COPY, size=60), 180: artwork.tile_png(COPY, size=180)}
    iconpacks.add_user_icon("workshop", name="marker", label="Marker", tiles=tiles, set_name="Workshop")
    iconpacks.add_user_icon("bench", name="clamp", label="Clamp", tiles=tiles, set_name="Bench")

    copied = iconpacks.copy_icon_into_user("workshop", "marker", "bench")
    assert copied.label == "Marker"

    packs = {pack.id: pack for pack in iconpacks.load_packs()}
    assert [icon.name for icon in packs["workshop"].icons] == ["marker"], "the copy removed the original"
    assert sorted(icon.name for icon in packs["bench"].icons) == ["clamp", "marker"]


def test_a_library_tile_is_served_as_a_transparent_png() -> None:
    data = artwork.tile_png(COPY)
    with Image.open(io.BytesIO(data)) as opened:
        assert opened.size == (60, 60) and opened.mode == "RGBA"


# --- what is already on the deck ---------------------------------------------

def test_a_configs_artwork_comes_back_as_the_icon_it_really_is(tmp_path: Path) -> None:
    path = write_config(tmp_path, WITH_ART, art_for=(0,))
    # The file holds exactly the general/cut bytes, so the editor can say which
    # icon it is - not merely "keep whatever this file is".
    (tmp_path / "images" / "key0.jpg").write_bytes(artwork.find_icon("pack:general/cut").jpeg())
    assignments = artwork.assignments_from_config(load_config(path).images, path)
    assert assignments[0] == "pack:general/cut"
    assert assignments[1:] == (None,) * 5


def test_unrecognised_artwork_is_kept_rather_than_guessed(tmp_path: Path) -> None:
    path = write_config(tmp_path, WITH_ART, art_for=(0,))
    (tmp_path / "images" / "key0.jpg").write_bytes(
        to_jpeg(Image.new("RGB", (60, 60), "#ab12cd"))
    )
    assignments = artwork.assignments_from_config(load_config(path).images, path)
    assert assignments[0] == "file:key0.jpg", "a picture from anywhere else must not be renamed"
    assert artwork.known_icon(managed_image_path(path, 0)) is None


def test_images_rows_are_written_only_for_keys_with_artwork() -> None:
    assert artwork.render_images_table([None] * 6) == ""
    assert artwork.render_images_table([COPY, None, "file:key2.jpg", None, None, None]) == (
        '[images]\nkey0 = "key0.jpg"\nkey2 = "key2.jpg"\n'
    )


def test_the_differ_reports_what_a_push_would_do(tmp_path: Path) -> None:
    path = write_config(tmp_path, WITH_ART, art_for=(0,))
    states = artwork.diff_assignments([COPY, PASTE, None, None, None, NEW_WINDOW], path)
    # key0 already holds exactly these bytes; key1 has nothing yet; keys 3-5 empty.
    assert states["key0"] == "same"
    assert states["key1"] == "adds"
    assert states["key5"] == "adds"
    assert states["key2"] == "empty"

    assert artwork.diff_assignments([None, None, None, None, None, None], path)["key0"] == "removes"
    # A keep reference for a key with no managed file is what a push must refuse.
    assert artwork.diff_assignments([None, None, "file:key2.jpg", None, None, None], path)["key2"] == "missing"

    # A different icon where one is already installed reads as a change.
    (tmp_path / "images" / "key1.jpg").write_bytes(artwork.find_icon(PASTE).jpeg())
    assert artwork.diff_assignments([COPY, COPY, None, None, None, None], path)["key1"] == "changes"
    described = artwork.describe_diff(states)
    assert described["key1"] == "new icon, not on the deck"


# --- pushing writes files, staging never does ---------------------------------

def test_materialise_writes_a_device_ready_image_once(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    assignments = [COPY, None, NEW_WINDOW, None, None, None]
    written = artwork.materialise(assignments, path)

    assert [asset.name for asset in written] == ["key0.jpg", "key2.jpg"]
    asset = managed_image_path(path, 0)
    assert asset.is_file()
    assert asset.stat().st_mode & 0o777 == 0o600
    assert asset.parent.stat().st_mode & 0o777 == 0o700
    assert asset.read_bytes() == artwork.find_icon(COPY).jpeg()
    assert len(asset.read_bytes()) <= 0xFFFF
    with Image.open(asset) as opened:
        assert opened.size == (60, 60) and opened.mode == "RGB"

    # Idempotent: the second push of the same assignment writes nothing at all.
    before = asset.stat().st_mtime_ns
    assert artwork.materialise(assignments, path) == ()
    assert asset.stat().st_mtime_ns == before


def test_a_keep_reference_with_no_file_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    with pytest.raises(artwork.ArtworkError, match="no such image on the deck"):
        artwork.verify_pusheable(["file:key0.jpg", None, None, None, None, None], path)
    assert not (tmp_path / "images").exists()
    # A library assignment needs no file to exist: it brings its own.
    artwork.verify_pusheable([COPY, None, None, None, None, None], path)


# --- the draft document -------------------------------------------------------

def test_the_draft_carries_assignments_and_keeps_them_out_of_the_config(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    draft = parse_draft(draft_document([COPY, None, None, None, None, None]))
    assert draft.icons == (COPY, None, None, None, None, None)

    merged = merge_draft_into_config(path.read_text(), draft)
    assert '[images]\nkey0 = "key0.jpg"' in merged
    # Staging alone still touched nothing on disk.
    assert not (tmp_path / "images").exists()
    assert path.read_text() == CONFIG


def test_a_draft_that_says_nothing_about_icons_cannot_blank_artwork(tmp_path: Path) -> None:
    path = write_config(tmp_path, WITH_ART, art_for=(0,))
    draft = parse_draft(draft_document())
    assert draft.icons is None
    # The editor rewrites its own tables, but artwork it never modelled survives.
    merged = merge_draft_into_config(path.read_text(), draft)
    assert "[images]" in merged
    assert 'key0 = "key0.jpg"' in merged
    assert managed_image_path(path, 0).is_file()


def test_a_draft_with_every_icon_cleared_drops_the_images_table(tmp_path: Path) -> None:
    path = write_config(tmp_path, WITH_ART, art_for=(0,))
    draft = parse_draft(draft_document([None] * 6))
    merged = merge_draft_into_config(path.read_text(), draft)
    assert "[images]" not in merged
    assert "key0.jpg" not in merged


def test_the_editor_refuses_a_draft_with_a_bad_assignment() -> None:
    with pytest.raises(ControlPlaneEditorError, match="icon"):
        parse_draft(draft_document([COPY, "pack:general/nope", None, None, None, None]))
    with pytest.raises(ControlPlaneEditorError, match="exactly 6"):
        parse_draft(draft_document([COPY]))


# --- the editor's routes ------------------------------------------------------

class Server:
    def __init__(self, path: Path):
        self.server = make_server(0, config=path, feed=None)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def __enter__(self) -> "Server":
        return self

    def __exit__(self, *_) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, Any, str]:
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw, content_type = response.read(), response.getheader("Content-Type") or ""
        connection.close()
        if content_type.startswith("application/json"):
            return response.status, json.loads(raw), content_type
        return response.status, raw, content_type

    def json(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        status, body, _ = self.request(method, path, payload)
        assert isinstance(body, dict), f"{path} did not answer with JSON"
        return status, body


def test_library_and_diff_routes_report_the_real_library(tmp_path: Path) -> None:
    path = write_config(tmp_path, WITH_ART, art_for=(0,))
    with Server(path) as server:
        status, body = server.json("GET", "/api/icons")
        assert status == 200
        assert {pack["id"] for pack in body["packs"]} == {"general", "macos"}

        status, raw, content_type = server.request("GET", "/api/icons/general/copy.png")
        assert status == 200 and content_type == "image/png"
        with Image.open(io.BytesIO(raw)) as opened:
            assert opened.size == (60, 60)

        for bad in ("/api/icons/general/nope.png", "/api/icons/nope/copy.png", "/api/icons/general/copy"):
            assert server.request("GET", bad)[0] == 404, bad

        # The draft loaded from the config knows which icon each key wears, and
        # that it matches - recognising it must not look like a change.
        status, body = server.json("GET", "/api/draft/diff")
        assert status == 200
        assert body["staged"][0] == "pack:general/copy"
        assert body["pending"] == []
        assert body["detail"]["key0"] == "already on the deck"


def test_staging_an_icon_changes_nothing_and_pushing_writes_it(tmp_path: Path) -> None:
    path = write_config(tmp_path, WITH_ART, art_for=(0,))
    before = path.read_text()
    pushed = artwork.find_icon(PASTE).jpeg()
    staged = ["file:key0.jpg", PASTE, None, None, None, None]

    with Server(path) as server:
        # Stage: the library icon is chosen and validated, and nothing is written.
        status, body = server.json("POST", "/api/draft", draft_document(staged))
        assert status == 200
        assert body["draft"]["icons"] == staged
        assert "not saved" in body["persistence"]
        assert path.read_text() == before
        assert managed_image_path(path, 1).exists() is False

        # The differ says exactly what is pending, and the deck's own key0 is untouched.
        status, body = server.json("GET", "/api/draft/diff")
        assert body["artwork"]["key1"] == "adds"
        assert body["artwork"]["key0"] == "same"
        assert body["pending"] == ["key1"]

        # Push: now the icon reaches the deck, with the config row to match.
        status, body = server.json("POST", "/api/save", draft_document(staged))
        assert status == 200, body
        assert body["icons_pushed"] == ["key1.jpg"]
        assert managed_image_path(path, 1).read_bytes() == pushed
        written = path.read_text()
        assert '[images]\nkey0 = "key0.jpg"\nkey1 = "key1.jpg"' in written
        assert load_config(path).images[1] == managed_image_path(path, 1)

        # And now nothing is pending any more.
        status, body = server.json("GET", "/api/draft/diff")
        assert body["pending"] == []


def test_a_rejected_push_leaves_the_deck_alone(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    before = path.read_text()
    with Server(path) as server:
        # key0 claims a file that is not there: the push must refuse, not blank it.
        status, body = server.json("POST", "/api/save", draft_document(["file:key0.jpg"] + [None] * 5))
        assert status == 400 and "artwork was not pushed" in body["error"]
        assert path.read_text() == before
        assert not (tmp_path / "images").exists()

        status, body = server.json("POST", "/api/save", draft_document([COPY.replace("copy", "nope")] + [None] * 5))
        assert status == 400 and "no icon named" in body["error"]
        assert path.read_text() == before


def test_discard_returns_to_the_file_not_to_the_staged_draft(tmp_path: Path) -> None:
    """Discard must re-read the config: /api/state hands back what is staged."""
    path = write_config(tmp_path, WITH_ART, art_for=(0,))
    before = path.read_text()
    staged = ["file:key0.jpg", PASTE, None, None, None, None]
    with Server(path) as server:
        status, body = server.json("POST", "/api/draft", draft_document(staged))
        assert body["draft"]["icons"][1] == PASTE

        status, body = server.json("GET", "/api/state")
        assert body["draft"]["icons"][1] == PASTE, "the store is the staged draft, by design"

        status, body = server.json("POST", "/api/draft/discard")
        assert status == 200
        assert body["draft"]["icons"] == ["pack:general/copy", None, None, None, None, None]
        assert "dropped" in body["persistence"]
        assert path.read_text() == before

        # The store and the disc now agree, so nothing is pending.
        status, body = server.json("GET", "/api/draft/diff")
        assert body["pending"] == []


def test_discard_is_refused_when_the_config_cannot_be_read(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    with Server(path) as server:
        path.chmod(0o644)  # no longer the daemon's own 0600 config
        status, body = server.json("POST", "/api/draft/discard")
        assert status == 409 and "could not re-read" in body["error"]


def test_unknown_icon_requests_never_reach_the_library(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    with Server(path) as server:
        for attempt in ("/api/icons/../../config.toml.png", "/api/icons/general/../config.toml.png"):
            assert server.request("GET", attempt)[0] == 404, attempt


def test_the_draft_store_keeps_the_last_staged_document(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    store = DraftStore(parse_draft(draft_document()))
    staged = store.replace(draft_document([COPY, None, None, None, None, None]))
    assert store.get().icons == staged.icons
    assert store.get().icons[0] == COPY
