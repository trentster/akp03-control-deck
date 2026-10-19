from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from akp03.config import ConfigError, load_config
from akp03.custom_menu import CustomMenu, CustomMenuNavigator, CustomMenuItem, CustomMenuPage
from akp03.daemon import Daemon
from akp03.device import Event
from akp03.model import (
    ActionDefinition, BUTTON_CONTROLS, ControlPlaneEditorError, MenuControlDefinition,
    MenuDefinition, MenuItemDefinition,
    action_registry_toml, custom_menu_definitions, menu_definitions_from_custom_menus,
    menu_slots, validate_menu_definitions,
)


# Two menus, the root one holding a button that opens the other.  This is the
# shape the editor writes now: a menu is one screen, and its button names the
# menu it opens (``submenu`` on disk, because that key predates the flat model).
YOUTUBE = '''[actions.rewind]
action = "keys"
keys = ["left"]
[actions.play_pause]
action = "keys"
keys = ["space"]
[actions.forward]
action = "keys"
keys = ["right"]
[actions.youtube]
action = "menu"
menu = "youtube"
[bindings.key0]
action_id = "youtube"

[custom_menus.youtube]
root = "home"
[[custom_menus.youtube.pages]]
id = "home"
title = "YouTube"
items = [{ key = 0, label = "Playback", submenu = "playback" }]

[custom_menus.playback]
root = "home"
[[custom_menus.playback.pages]]
id = "home"
title = "Playback"
items = [
  { key = 0, label = "Rewind", action = "rewind" },
  { key = 1, label = "Play / Pause", action = "play_pause" },
  { key = 2, label = "Forward", action = "forward" },
]
'''

# A configuration written when a menu *had* pages: one menu, two pages, linked
# by a page-id submenu.  It must keep loading, and the editor model must lift it
# into two menus with the link rewritten.
LEGACY_TWO_PAGE = '''[actions.rewind]
action = "keys"
keys = ["left"]
[actions.play_pause]
action = "keys"
keys = ["space"]
[actions.forward]
action = "keys"
keys = ["right"]

[custom_menus.youtube]
root = "home"
[[custom_menus.youtube.pages]]
id = "home"
title = "YouTube"
items = [{ key = 0, label = "Playback", submenu = "playback" }]
[[custom_menus.youtube.pages]]
id = "playback"
title = "Playback"
items = [
  { key = 0, label = "Rewind", action = "rewind" },
  { key = 1, label = "Play / Pause", action = "play_pause" },
  { key = 2, label = "Forward", action = "forward" },
]
'''


# The tile colour the daemon paints the knob-selected item with.
SELECTED = "#173f5f"


def _config(path: Path, text: str) -> None:
    path.write_text(text); path.chmod(0o600)


def _menu(name: str, title: str, *items: CustomMenuItem) -> CustomMenu:
    """One editor-style menu: a single page, id ``main``, holding the buttons."""
    return CustomMenu(name, "main", {"main": CustomMenuPage("main", title, tuple(items))})


def test_custom_menu_uses_all_six_keys_with_knob_navigation(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, YOUTUBE)
    loaded = load_config(path)
    assert loaded.actions["youtube"].menu_name == "youtube"
    assert loaded.custom_menus and loaded.custom_menus["youtube"].root == "home"

    class Deck:
        def __init__(self, _path: str): self.calls = []
        def on_event(self, callback): self.event = callback
        def on_disconnect(self, callback): self.disconnect = callback
        def open(self): return self
        def close(self): pass
        def clear(self, key): self.calls.append(("clear", key))
        def set_key_image(self, key, image): self.calls.append(("image", key, image.copy()))
        def set_brightness(self, level): self.calls.append(("brightness", level))

    deck = Deck("/dev/fake")
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck)
    assert daemon.reload() and daemon.connect()
    idle = {key: paint.text for key, paint in daemon.labels.items()}
    assert idle, "the deck must show its ordinary labels before a menu opens"
    daemon.event(Event("key", "key0", True, None, b""))
    assert daemon.custom_menu is not None
    # Every painted key is menu content: no key is spent on reserved Back/Home.
    assert daemon.labels[0].text == "Playback"
    assert set(daemon.labels) == {0}
    # The selection is the tile colour alone: the knob needs a visible effect,
    # and the tile carries no heading and no arrow beside the text.
    assert daemon.labels[0].background == SELECTED

    # The top knob presses to select the highlighted item (the menu link).
    daemon.event(Event("knob_press", "top", True, None, b""))
    assert daemon.labels[0].text == "Rewind"
    # No heading on any tile: a key shows only the item it runs.
    assert daemon.labels[2].text == "Forward"
    assert daemon.labels[0].background == SELECTED and daemon.labels[2].background == "black"
    assert set(daemon.labels) == {0, 1, 2}

    # The top knob turns to move the highlight, and it wraps both ways.
    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert daemon.labels[1].text == "Play / Pause"
    # The highlight moved on; the key it left shows just its own item.
    assert daemon.labels[0].text == "Rewind"
    assert daemon.labels[1].background == SELECTED and daemon.labels[0].background == "black"
    daemon.event(Event("knob_turn", "top", None, -1, b""))
    assert daemon.labels[0].text == "Rewind"
    assert daemon.labels[0].background == SELECTED

    # The bottom-left knob goes back a level: out of the opened menu and back
    # onto the one that opened it.
    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    assert daemon.labels[0].text == "Playback"

    # Visible LCD keys still activate their item directly.
    daemon.event(Event("key", "key0", True, None, b""))
    assert daemon.labels[0].text == "Rewind"

    # Back returns to the menu that opened it...
    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    assert daemon.custom_menu is not None and daemon.custom_menu.state.title == "YouTube"
    # ...and Back again ON the outermost menu leaves the deck's menu altogether.
    # Without this the deck could not be taken out of a menu at all, which is a
    # dead end at the hardware: the person can only recover by restarting the daemon.
    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    assert daemon.custom_menu is None
    # The deck shows exactly the ordinary labels it had before the menu opened.
    assert {key: paint.text for key, paint in daemon.labels.items()} == idle

    # The bound key still opens the menu again afterwards.
    daemon.event(Event("key", "key0", True, None, b""))
    assert daemon.custom_menu is not None
    assert daemon.labels[0].text == "Playback"


@pytest.mark.parametrize("fragment", [
    'items=[{key=6,label="Bad",action="rewind"}]',
    'items=[{key=0,label="A",action="rewind"},{key=0,label="B",action="rewind"}]',
    'items=[{key=0,label="Bad",action="missing"}]',
])
def test_custom_menu_rejects_out_of_range_duplicate_and_unknown_controls(tmp_path: Path, fragment: str) -> None:
    text = YOUTUBE.replace('items = [{ key = 0, label = "Playback", submenu = "playback" }]', fragment)
    path = tmp_path / "config.toml"; _config(path, text)
    with pytest.raises(ConfigError): load_config(path)


def test_gui_menu_model_is_tk_free_safe_and_renders_template() -> None:
    actions = [
        ActionDefinition("rewind", "keys", ("left",)),
        ActionDefinition("play_pause", "keys", ("space",)),
        ActionDefinition("forward", "keys", ("right",)),
        ActionDefinition("youtube", "menu", menu_name="youtube"),
    ]
    # A menu whose button opens a second menu, each three buttons or fewer.
    youtube = MenuDefinition("youtube", "YouTube", (
        MenuItemDefinition(0, "Playback", menu="playback"),
    ))
    playback = MenuDefinition("playback", "Playback", (
        MenuItemDefinition(0, "Rewind", action_id="rewind"),
        MenuItemDefinition(1, "Play / Pause", action_id="play_pause"),
        MenuItemDefinition(2, "Forward", action_id="forward"),
    ))
    validate_menu_definitions([youtube, playback], actions)
    rendered = action_registry_toml(actions, ["youtube", None, None, None, None, None], [youtube, playback])
    assert '[custom_menus.youtube]' in rendered
    assert 'action = "menu"' in rendered
    assert 'submenu = "playback"' in rendered
    with pytest.raises(ControlPlaneEditorError, match="duplicate"):
        validate_menu_definitions([MenuDefinition("bad", "Home", (MenuItemDefinition(0, "A", action_id="rewind"), MenuItemDefinition(0, "B", action_id="rewind")))], actions)


def test_gui_menu_save_round_trips_without_replacing_opaque_configuration(tmp_path: Path) -> None:
    from akp03.gui import save_control_plane_config

    path = tmp_path / "config.toml"; _config(path, '[bindings.top]\naction="shell"\nshell=["/bin/true"]\n')
    actions = [ActionDefinition("copy", "keys", ("ctrl", "c")), ActionDefinition("deck", "menu", menu_name="deck")]
    menu = MenuDefinition("deck", "Deck", (MenuItemDefinition(0, "Copy", action_id="copy"),))
    save_control_plane_config(path, actions, ["deck", None, None, None, None, None], menus=[menu])
    assert load_config(path).custom_menus and "deck" in load_config(path).custom_menus
    assert 'action="shell"' in path.read_text()


def test_menu_editor_helpers_expose_six_content_slots_and_load_safe_models(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, YOUTUBE)
    menus = custom_menu_definitions(path)
    assert menus[0].name == "youtube"
    slots = menu_slots(menus[0])
    assert slots[0] == MenuItemDefinition(0, "Playback", menu="playback")
    assert slots[1:] == (None, None, None, None, None)
    with pytest.raises(ControlPlaneEditorError, match="duplicate"):
        menu_slots(MenuDefinition("home", "Home", (
            MenuItemDefinition(0, "One", action_id="rewind"),
            MenuItemDefinition(0, "Two", action_id="rewind"),
        )))


def test_menu_save_removes_a_deleted_editor_owned_menu(tmp_path: Path) -> None:
    from akp03.gui import save_control_plane_config

    path = tmp_path / "config.toml"; _config(path, YOUTUBE)
    save_control_plane_config(path, [ActionDefinition("copy", "keys", ("ctrl", "c"))], ["copy", None, None, None, None, None])
    assert "[custom_menus.youtube]" not in path.read_text()
    assert load_config(path).custom_menus == {}


# --- menus nest: a button opens another menu ---------------------------------


def test_a_button_opens_another_menu_and_back_returns_to_it() -> None:
    menu_a = _menu("a", "Menu A", CustomMenuItem(0, "Open B", submenu="b"))
    menu_b = _menu("b", "Menu B", CustomMenuItem(0, "In B", action="copy"))
    navigator = CustomMenuNavigator({"a": menu_a, "b": menu_b}, "a")

    assert navigator.state.title == "Menu A"
    activation = navigator.press(0)
    # Opening a menu runs nothing; it moves the deck onto that menu's screen.
    assert activation.action is None
    assert navigator.state.title == "Menu B"
    assert navigator.state.labels[0] == "In B"
    assert navigator.menu_name == "b" and navigator.depth == 1

    # Back unwinds one level, straight back onto the menu that opened it.
    assert navigator.back() is not None
    assert navigator.state.title == "Menu A"
    assert navigator.menu_name == "a" and navigator.depth == 0


def test_three_nested_menus_unwind_one_level_at_a_time() -> None:
    menu_a = _menu("a", "A", CustomMenuItem(0, "To B", submenu="b"))
    menu_b = _menu("b", "B", CustomMenuItem(0, "To C", submenu="c"))
    menu_c = _menu("c", "C", CustomMenuItem(0, "Leaf", action="copy"))
    navigator = CustomMenuNavigator({"a": menu_a, "b": menu_b, "c": menu_c}, "a")

    navigator.press(0)
    assert navigator.menu_name == "b" and navigator.depth == 1
    navigator.press(0)
    assert navigator.menu_name == "c" and navigator.depth == 2
    assert navigator.state.labels[0] == "Leaf"

    # Back pops exactly one level each time...
    assert navigator.back() is not None and navigator.menu_name == "b" and navigator.depth == 1
    assert navigator.back() is not None and navigator.menu_name == "a" and navigator.depth == 0
    # ...and only the outermost menu returns None, which is what closes the menu.
    assert navigator.back() is None
    # The outermost menu is left untouched by that final Back.
    assert navigator.menu_name == "a" and navigator.depth == 0


def test_state_tracks_the_menu_name_and_depth() -> None:
    menu_a = _menu("a", "A", CustomMenuItem(0, "To B", submenu="b"))
    menu_b = _menu("b", "B", CustomMenuItem(0, "To C", submenu="c"))
    menu_c = _menu("c", "C", CustomMenuItem(0, "Leaf", action="copy"))
    navigator = CustomMenuNavigator({"a": menu_a, "b": menu_b, "c": menu_c}, "a")

    assert (navigator.state.menu_name, navigator.state.depth) == ("a", 0)
    navigator.press(0)
    assert (navigator.state.menu_name, navigator.state.depth) == ("b", 1)
    navigator.press(0)
    assert (navigator.state.menu_name, navigator.state.depth) == ("c", 2)
    navigator.back()
    assert (navigator.state.menu_name, navigator.state.depth) == ("b", 1)
    navigator.back()
    assert (navigator.state.menu_name, navigator.state.depth) == ("a", 0)
    # The rendered state and the navigator's own account of where it is agree.
    assert navigator.state.menu_name == navigator.menu_name
    assert navigator.state.depth == navigator.depth


# --- menus nest: the config ---------------------------------------------------


def test_a_menu_item_may_name_another_menu(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, YOUTUBE)
    loaded = load_config(path)

    assert set(loaded.custom_menus or {}) == {"youtube", "playback"}
    # The button's submenu names the *other menu*, not a page of its own.
    assert loaded.custom_menus["youtube"].pages["home"].items[0].submenu == "playback"
    assert loaded.custom_menus["playback"].title == "Playback"


def test_a_cycle_across_menus_is_refused(tmp_path: Path) -> None:
    # A opens B, B opens A: a cycle that can never be navigated out of.
    cycle = YOUTUBE.replace(
        '{ key = 0, label = "Rewind", action = "rewind" }',
        '{ key = 0, label = "Back to A", submenu = "youtube" }',
    )
    path = tmp_path / "config.toml"; _config(path, cycle)
    with pytest.raises(ConfigError, match="cycles"):
        load_config(path)


# --- the editor writes one page per menu --------------------------------------


def test_the_editor_writes_one_page_per_menu_and_links_by_name() -> None:
    actions = [ActionDefinition("copy", "keys", ("cmd", "c"))]
    menu_a = MenuDefinition("a", "A", (MenuItemDefinition(0, "To B", menu="b"),))
    menu_b = MenuDefinition("b", "B", (MenuItemDefinition(0, "Copy", action_id="copy"),))
    rendered = action_registry_toml(actions, ["copy", None, None, None, None, None], [menu_a, menu_b])

    document = tomllib.loads(rendered)
    # Each menu is written as exactly one page, whose id is the fixed main page.
    assert document["custom_menus"]["a"]["root"] == "main"
    assert len(document["custom_menus"]["a"]["pages"]) == 1
    assert document["custom_menus"]["a"]["pages"][0]["id"] == "main"
    assert rendered.count("[[custom_menus.a.pages]]") == 1
    # A button that opens another menu renders as that menu's name.
    assert document["custom_menus"]["a"]["pages"][0]["items"][0]["submenu"] == "b"
    assert "submenu = \"b\"" in rendered


def test_a_legacy_two_page_menu_loads_and_flattens_into_two_linked_menus(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"; _config(path, LEGACY_TWO_PAGE)
    loaded = load_config(path)
    # On disk it is still one menu with two pages, and it still loads.
    assert set(loaded.custom_menus or {}) == {"youtube"}
    assert loaded.custom_menus["youtube"].root == "home"

    definitions = menu_definitions_from_custom_menus(loaded.custom_menus)
    # The editor model lifts it into two menus: the root page keeps the menu's
    # own name, the other page becomes ``<name>-<page_id>``.
    assert [menu.name for menu in definitions] == ["youtube", "youtube-playback"]
    root = next(menu for menu in definitions if menu.name == "youtube")
    other = next(menu for menu in definitions if menu.name == "youtube-playback")
    assert root.title == "YouTube" and other.title == "Playback"
    # The page-to-page link is rewritten to point at the new menu.
    assert root.items[0].menu == "youtube-playback"
    assert [item.action_id for item in other.items] == ["rewind", "play_pause", "forward"]


# --- an empty menu is legal ---------------------------------------------------


def test_an_empty_menu_loads_validates_and_renders_blank(tmp_path: Path) -> None:
    empty = '''[custom_menus.blank]
root = "main"
[[custom_menus.blank.pages]]
id = "main"
title = "Blank"
items = []
'''
    path = tmp_path / "config.toml"; _config(path, empty)
    loaded = load_config(path)
    assert loaded.custom_menus["blank"].items == ()

    # The editor model accepts it too, and it fills no key.
    definition = menu_definitions_from_custom_menus(loaded.custom_menus)[0]
    validate_menu_definitions([definition], [])
    assert menu_slots(definition) == (None,) * 6

    # On the deck it renders nothing: six blank keys and no highlight.
    navigator = CustomMenuNavigator(loaded.custom_menus, "blank")
    assert navigator.state.labels == ("",) * 6
    assert navigator.state.selected_key is None
    assert navigator.state.menu_name == "blank" and navigator.state.depth == 0
    # No button, so pressing or selecting does nothing and Back leaves at once.
    assert navigator.press(3).action is None
    assert navigator.select().action is None
    assert navigator.back() is None


def test_a_menu_may_hold_at_most_six_buttons(tmp_path: Path) -> None:
    seven = MenuDefinition("big", "Big", tuple(
        MenuItemDefinition(key, f"Item {key}", action_id="copy") for key in range(7)
    ))
    actions = [ActionDefinition("copy", "keys", ("cmd", "c"))]
    with pytest.raises(ControlPlaneEditorError, match="at most 6"):
        validate_menu_definitions([seven], actions)
    # And the config parser refuses it the same way.
    text = '''[actions.copy]
action = "keys"
keys = ["cmd", "c"]
[custom_menus.big]
root = "main"
[[custom_menus.big.pages]]
id = "main"
title = "Big"
items = [{ key = 0, label = "One", action = "copy" }, { key = 1, label = "Two", action = "copy" }, { key = 2, label = "Three", action = "copy" }, { key = 3, label = "Four", action = "copy" }, { key = 4, label = "Five", action = "copy" }, { key = 5, label = "Six", action = "copy" }, { key = 0, label = "Seven", action = "copy" }]
'''
    probe = tmp_path / "config.toml"; _config(probe, text)
    with pytest.raises(ConfigError, match="more than 6"):
        load_config(probe)


def test_the_editor_writes_an_empty_menu_as_an_empty_items_list() -> None:
    rendered = action_registry_toml([], [None] * 6, [MenuDefinition("blank", "Blank", ())])
    document = tomllib.loads(rendered)
    assert document["custom_menus"]["blank"]["root"] == "main"
    assert document["custom_menus"]["blank"]["pages"][0]["items"] == []
    assert "items = []" in rendered


def test_a_menu_refusal_says_which_button_and_never_names_the_host() -> None:
    """The refusal used to read "menu buttons may reference only GUI-safe keys, brightness,
    or Omarchy actions" and name nothing at all.

    So a delete that happened to trip it looked like the delete had failed, and the sentence
    told the reader about a host they had not heard of instead of about the button in front
    of them.  The three cases below are the three places a menu names an action.

    The count matters as much as the name: the message used to number buttons from zero, so
    a complaint about "button 2" pointed at the third button on the screen.  Number them the
    way the editor does - from one - or the reader goes looking at the wrong button.
    """
    actions = [ActionDefinition("find", "keys", ("cmd", "f")), ActionDefinition("youtube-menu", "menu")]
    cap = BUTTON_CONTROLS[0]
    cases = {
        "button 6": MenuDefinition("youtube", "Youtube", (MenuItemDefinition(5, "Copy", action_id="youtube-menu"),)),
        cap: MenuDefinition("youtube", "Youtube", (), (MenuControlDefinition("youtube-menu"), None, None)),
        "top dial": MenuDefinition("youtube", "Youtube", (), (None, None, None), {"top": "youtube-menu"}),
    }
    for where, menu in cases.items():
        with pytest.raises(ControlPlaneEditorError) as refused:
            validate_menu_definitions([menu], actions)
        said = str(refused.value)
        assert "Youtube" in said and "youtube-menu" in said, said
        assert where in said, said
        assert "Omarchy" not in said and "GUI-safe" not in said, said


def test_a_button_left_pointing_at_a_missing_action_says_so() -> None:
    """A button naming an action the deck no longer holds is not the same trouble as a
    button naming one a menu cannot use.

    Both used to raise the same sentence, so a button pointing at a vanished action read as
    "can't run 'copy'" - blaming an ordinary shortcut and sending the reader to a button that
    was never the trouble.  It says the real thing now, and says which button.
    """
    gone = MenuDefinition("youtube", "Youtube", (MenuItemDefinition(5, "Copy", action_id="copy"),))
    with pytest.raises(ControlPlaneEditorError) as refused:
        validate_menu_definitions([gone], [])
    said = str(refused.value)
    assert "button 6" in said and "copy" in said, said
    assert "no longer has" in said, said
    assert "can't run" not in said, "a missing action is not a forbidden one"
