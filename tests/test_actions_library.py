"""The ready-made action library: browsable, and honest about what a key sends."""

from __future__ import annotations

import pytest

from akp03 import actions_library
from akp03.gui import SAFE_ACTION_TYPES

MODIFIERS = {"ctrl", "alt", "shift", "cmd"}


def test_every_entry_is_a_safe_action_the_daemon_accepts() -> None:
    for entry in actions_library.CATALOGUE:
        spec = entry.spec
        assert spec["kind"] in SAFE_ACTION_TYPES, entry.id
        assert spec["id"] == entry.id


def test_every_shortcut_uses_a_key_the_deck_can_send() -> None:
    """An unsendable key would save fine and do nothing on the deck."""
    assert actions_library.bad_keys() == []


def test_each_shortcut_is_a_modifier_combo_or_one_named_key() -> None:
    for entry in actions_library.CATALOGUE:
        if not entry.keys:
            continue
        plain = [key for key in entry.keys if key not in MODIFIERS]
        assert len(plain) == 1, f"{entry.id} sends {entry.keys}"
        assert entry.keys[-1] not in MODIFIERS, f"{entry.id} ends on a modifier"


def test_no_entry_id_is_defined_twice() -> None:
    ids = [entry.id for entry in actions_library.CATALOGUE]
    assert len(ids) == len(set(ids))


def test_action_ids_match_the_registry_rules() -> None:
    """The id becomes the `[actions.ID]` table name, so it must be a legal one."""
    for entry in actions_library.CATALOGUE:
        assert entry.id[0].isalpha(), entry.id
        assert all(character.islower() or character.isdigit() or character in "-_" for character in entry.id), entry.id
        assert len(entry.id) <= 32, entry.id


def test_brightness_entry_carries_a_level_and_no_keys() -> None:
    brightness = [entry for entry in actions_library.CATALOGUE if entry.brightness is not None]
    assert len(brightness) == 1, "brightness is deliberately a single afterthought row"
    level = brightness[0].brightness
    assert level is not None and 1 <= level <= 100
    assert brightness[0].keys == ()


def test_every_entry_says_what_it_does() -> None:
    for entry in actions_library.CATALOGUE:
        assert entry.label and not entry.label.startswith(entry.id), entry.id
        assert len(entry.blurb) > 15, entry.id


def test_catalogue_groups_every_entry_into_a_named_section() -> None:
    payload = actions_library.catalogue()
    listed = [action["id"] for section in payload["sections"] for action in section["actions"]]
    assert sorted(listed) == sorted(entry.id for entry in actions_library.CATALOGUE)
    assert payload["count"] == len(listed)
    section_ids = {section["id"] for section in payload["sections"]}
    assert section_ids == {entry.section for entry in actions_library.CATALOGUE}
    for section in payload["sections"]:
        assert section["name"] and section["blurb"]
        assert section["actions"], f"empty section {section['id']}"


def test_shortcut_text_reads_like_a_mac_shortcut() -> None:
    assert actions_library.shortcut_text(("cmd", "c")) == "\u2318 C"
    assert actions_library.shortcut_text(("shift", "cmd", "z")) == "\u21e7 \u2318 Z"
    assert actions_library.shortcut_text(("ctrl", "up")) == "\u2303 \u2191"
    assert actions_library.shortcut_text(("f8",)) == "F8"
    assert actions_library.shortcut_text(("space",)) == "Space"


def test_payload_carries_a_shortcut_and_an_icon_name_for_each_entry() -> None:
    for section in actions_library.catalogue()["sections"]:
        for action in section["actions"]:
            if action["brightness"] is not None:
                assert action["shortcut"] == ""
            else:
                assert action["shortcut"], action["id"]
            assert action["icon"], action["id"]


def test_spec_lookup_returns_the_draft_shape() -> None:
    assert actions_library.spec("copy") == {"id": "copy", "kind": "keys", "keys": ["cmd", "c"]}
    assert actions_library.spec("brightness-25") == {"id": "brightness-25", "kind": "brightness", "brightness": 25}
    assert actions_library.spec("not-an-action") is None


@pytest.mark.parametrize("action_id", ["copy", "play-pause", "lock", "quit-app"])
def test_common_actions_are_offered(action_id: str) -> None:
    """A fresh config has no actions at all, so these must exist in the library."""
    assert actions_library.spec(action_id) is not None


def test_a_media_row_sends_a_media_key_and_never_a_function_key() -> None:
    """The bug this pins: F12 looks like volume up and is not.

    F7-F12 are the function keys a Mac keyboard prints the media symbols under.
    Pressing one with a finger sends the media command, but the app can only send
    "the function key", which moves nothing unless a volume-aware panel happens to
    be in front.  A row that promises a media key must name one of the six.
    """
    media_rows = [entry for entry in actions_library.CATALOGUE if "media key" in entry.blurb]
    assert len(media_rows) == 6, [entry.id for entry in media_rows]
    for entry in media_rows:
        assert len(entry.keys) == 1, f"{entry.id} sends {entry.keys}"
        assert entry.keys[0] in actions_library.MEDIA_KEYS, f"{entry.id} sends {entry.keys}"
    # And no row anywhere stands a function key in for a real key.
    function_keys = {f"f{n}" for n in range(7, 13)}
    for entry in actions_library.CATALOGUE:
        assert not function_keys & set(entry.keys), f"{entry.id} sends a function key where a real key belongs"
