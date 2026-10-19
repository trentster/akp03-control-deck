from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from akp03.iconpacks import (
    BUILTIN_PACKS, IconLibraryError, Pack, iter_icons, load_pack, load_packs, resolve, user_packs_dir,
)
from akp03.images import KEY_SIZE, MAX_JPEG_BYTES

SELECTED = "#173f5f"


def make_pack(directory: Path, *, pack_id: str = "testpack", icons: int = 1, hue: str = "#123456",
              size: int = 60, mode: str = "RGBA", name: str = "test-icon", licence: str = "MIT",
              author: str = "Someone") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for index in range(icons):
        icon_name = f"{name}-{index}" if icons > 1 else name
        entries.append({"name": icon_name, "label": f"Test {index}", "hue": hue, "action": None})
        for tile_size in (60, 180):
            tile = Image.new(mode, (size if tile_size == 60 else 180, size if tile_size == 60 else 180),
                             (0, 0, 0, 0) if mode == "RGBA" else "black")
            draw = ImageDraw.Draw(tile)
            draw.ellipse((4, 4, (size if tile_size == 60 else 180) - 5, (size if tile_size == 60 else 180) - 5),
                         fill=(255, 255, 255, 255) if mode == "RGBA" else "white")
            tile.save(directory / f"{icon_name}.{tile_size}.png")
    manifest = {"id": directory.name, "name": pack_id, "description": "test", "version": 1,
                "licence": licence, "author": author, "icons": entries}
    (directory / "pack.json").write_text(json.dumps(manifest))
    return directory


def shipped() -> tuple[Pack, ...]:
    return load_packs(include_user=False)


# --- the shipped library -----------------------------------------------------

def test_both_shipped_packs_load():
    packs = shipped()
    assert [pack.id for pack in packs] == ["general", "macos"]
    assert all(len(pack.icons) == 24 for pack in packs)
    assert all(pack.licence for pack in packs) and all(pack.author for pack in packs)


def test_shipped_icons_are_deck_ready():
    for reference, icon in iter_icons(shipped()):
        assert set(icon.tiles) == {60, 180}, reference
        for size in (60, 180):
            with Image.open(icon.tiles[size]) as opened:
                assert opened.size == (size, size), reference
                assert opened.mode == "RGBA", reference
        encoded = icon.jpeg()
        assert len(encoded) <= MAX_JPEG_BYTES, reference
        with Image.open(__import__("io").BytesIO(encoded)) as opened:
            assert opened.size == KEY_SIZE and opened.mode == "RGB"


def test_shipped_glyphs_leave_a_bezel_margin():
    """House rule: art-safe area, nothing jammed against the edge of the key."""
    for reference, icon in iter_icons(shipped()):
        bounding = icon.tile(60).getchannel("A").getbbox()
        assert bounding is not None, reference
        left, top, right, bottom = bounding
        assert min(left, top, 60 - right, 60 - bottom) >= 4, f"{reference} touches the bezel: {bounding}"


def test_shipped_hues_are_distinct_within_a_pack():
    for pack in shipped():
        assert len(set(pack.hues())) == len(pack.icons), f"{pack.id} reuses a hue"


def test_glyph_composites_onto_a_selected_tile():
    icon = resolve("general/copy", shipped())
    idle, selected = icon.face(), icon.face(background=SELECTED)
    assert idle.size == selected.size == KEY_SIZE
    assert idle != selected
    glyph = icon.tile(60).getchannel("A").getbbox()
    assert glyph is not None
    assert idle.getpixel((glyph[0], glyph[1] + 2)) != selected.getpixel((glyph[0], glyph[1] + 2))


def test_resolve_finds_icons_and_reports_unknown_ones():
    packs = shipped()
    assert resolve("general/copy", packs).label == "Copy"
    assert resolve("macos/spotlight", packs).action == "cmd + space"
    for bad in ("general/nope", "nope/copy", "copy", "/copy", "general/"):
        with pytest.raises(IconLibraryError):
            resolve(bad, packs)


# --- validation --------------------------------------------------------------

def test_pack_without_a_manifest_is_rejected(tmp_path):
    with pytest.raises(IconLibraryError, match="no pack.json"):
        load_pack(tmp_path)


def test_id_must_match_the_directory_name(tmp_path):
    pack = make_pack(tmp_path / "realname", pack_id="other")
    manifest = json.loads((pack / "pack.json").read_text())
    manifest["id"] = "mismatched"
    (pack / "pack.json").write_text(json.dumps(manifest))
    with pytest.raises(IconLibraryError, match="must match the directory name"):
        load_pack(pack)


@pytest.mark.parametrize("field, value, message", [
    ("licence", "", "licence must be a non-empty string"),
    ("author", "", "author must be a non-empty string"),
    ("icons", {"not": "a list"}, "icons must be an array"),
])
def test_missing_manifest_facts_are_rejected(tmp_path, field, value, message):
    pack = make_pack(tmp_path / "testpack")
    manifest = json.loads((pack / "pack.json").read_text())
    manifest[field] = value
    (pack / "pack.json").write_text(json.dumps(manifest))
    with pytest.raises(IconLibraryError, match=message.replace(" ", r"\s+")):
        load_pack(pack)


def test_an_empty_set_is_still_a_set(tmp_path):
    """A set of your own is a named container: taking the last icon out leaves it there."""
    pack = make_pack(tmp_path / "testpack")
    manifest = json.loads((pack / "pack.json").read_text())
    manifest["icons"] = []
    (pack / "pack.json").write_text(json.dumps(manifest))
    loaded = load_pack(pack)
    assert loaded.id == "testpack" and loaded.icons == ()
    assert loaded.name, "the set keeps its name while it is empty"


def test_bad_hue_and_escaping_names_are_rejected(tmp_path):
    pack = make_pack(tmp_path / "testpack")
    manifest = json.loads((pack / "pack.json").read_text())
    manifest["icons"][0]["hue"] = "blue"
    (pack / "pack.json").write_text(json.dumps(manifest))
    with pytest.raises(IconLibraryError, match="must look like #rrggbb"):
        load_pack(pack)

    manifest["icons"][0]["hue"] = "#123456"
    manifest["icons"][0]["name"] = "../escape"
    (pack / "pack.json").write_text(json.dumps(manifest))
    with pytest.raises(IconLibraryError, match="lowercase letters"):
        load_pack(pack)


def test_duplicate_icon_names_are_rejected(tmp_path):
    pack = make_pack(tmp_path / "testpack", name="twice")
    manifest = json.loads((pack / "pack.json").read_text())
    manifest["icons"].append(dict(manifest["icons"][0]))
    manifest["icons"][1]["name"] = manifest["icons"][0]["name"]
    (pack / "pack.json").write_text(json.dumps(manifest))
    with pytest.raises(IconLibraryError, match="names must be unique"):
        load_pack(pack)


def test_wrong_tile_size_and_mode_are_rejected(tmp_path):
    pack = make_pack(tmp_path / "testpack", size=48)
    with pytest.raises(IconLibraryError, match="expected 60x60"):
        load_pack(pack)

    pack = make_pack(tmp_path / "withrgb", name="rgb-icon", mode="RGB")
    with pytest.raises(IconLibraryError, match="expected RGBA"):
        load_pack(pack)


def test_missing_tile_is_rejected(tmp_path):
    pack = make_pack(tmp_path / "testpack")
    (pack / "test-icon.180.png").unlink()
    with pytest.raises(IconLibraryError, match="is missing"):
        load_pack(pack)


# --- user packs --------------------------------------------------------------

def test_user_packs_are_discovered_and_can_replace_builtins(tmp_path, monkeypatch):
    monkeypatch.setenv("AKP03_ICONS_DIR", str(tmp_path))
    assert user_packs_dir() == tmp_path
    make_pack(tmp_path / "mine")

    ids = [pack.id for pack in load_packs()]
    assert ids == ["general", "macos", "mine"]

    # A user pack reusing a built-in id replaces it rather than duplicating it.
    replacement = make_pack(tmp_path / "general", pack_id="general", icons=2)
    packs = load_packs()
    assert [pack.id for pack in packs] == ["general", "macos", "mine"]
    assert load_pack(replacement).directory == replacement
    assert len([pack for pack in packs if pack.id == "general"][0].icons) == 2

    # And the built-in pack itself is untouched on disk.
    assert len(load_pack(BUILTIN_PACKS / "general").icons) == 24


def test_include_user_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AKP03_ICONS_DIR", str(tmp_path))
    make_pack(tmp_path / "mine")
    assert [pack.id for pack in load_packs(include_user=False)] == ["general", "macos"]


def test_extra_directories_are_loaded(tmp_path):
    extra = tmp_path / "elsewhere"
    make_pack(extra / "contrib")
    ids = [pack.id for pack in load_packs(include_user=False, extra_dirs=[extra])]
    assert ids == ["general", "macos", "contrib"]
