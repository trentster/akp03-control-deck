"""Building a nested menu system by hand in the editor, from an empty config.

These cover the Menus tab's whole flow through the real service boundary: a menu
created from nothing, content keys added (an action or a link to another menu),
a deck key bound to open the menu, the draft saved and reloaded through the
service, and the refusals that keep a save from silently erasing or corrupting
the configuration.  The front-end assertions at the end keep a future edit from
quietly dropping the controls this flow depends on.

The model is flat: one menu is one screen of up to six buttons, and a button
that opens another screen names that menu with ``menu``.  Tests are written
against that shape; a former second *page* of one menu is now a second menu.
"""
from __future__ import annotations
from support import wait_for_port as _wait_for_port, wait_until as _wait_until

import json
import logging
import socket
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest

from akp03.config import load_config
from akp03.gui import ControlPlaneEditorError
from akp03.webui import make_server, merge_draft_into_config, parse_draft, save_draft_config

import support

# A starting config with NO [custom_menus] at all, plus two sections the editor
# does not model and must never touch.
EMPTY = '''[daemon]
device = "/dev/null"
poll_seconds = 1

[labels]
key0 = "One"

# a comment the editor must preserve
[feedback]
enabled = false
duration_ms = 180
'''


def write_config(directory: Path, text: str, name: str = "config.toml") -> Path:
    path = directory / name
    path.write_text(text)
    path.chmod(0o600)
    return path


def request(server, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    return response.status, data


def running_server(*, config: Path | None = None):
    server = make_server(0, config=config)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    return server, thread


def stop_server(server, thread) -> None:
    server.shutdown()
    thread.join()
    server.server_close()


def built_draft() -> dict:
    """The document the Menus tab sends after building a two-menu 'youtube'
    system and binding Key 1 to open it — exactly the shape in ``draft_payload``.

    The former second *page* of one menu is now its own menu (``youtube-more``),
    and the 'More' key opens it by name with ``menu``.
    """
    return {
        "actions": [
            {"id": "copy", "kind": "keys", "keys": ["cmd", "c"]},
            {"id": "paste", "kind": "keys", "keys": ["cmd", "v"]},
            {"id": "youtube-menu", "kind": "menu", "menu": "youtube"},
        ],
        "bindings": ["youtube-menu", None, None, None, None, None],
        "dials": [None] * 8,
        "menus": [
            {"name": "youtube", "title": "Home", "items": [
                {"key": 0, "label": "Copy", "action": "copy"},
                {"key": 1, "label": "More", "menu": "youtube-more"},
            ]},
            {"name": "youtube-more", "title": "More", "items": [
                {"key": 0, "label": "Paste", "action": "paste"},
            ]},
        ],
    }


# -- Creating a menu from a config that has none -------------------------------


def test_a_menu_can_be_created_from_a_config_that_has_no_menus(tmp_path: Path) -> None:
    path = write_config(tmp_path, EMPTY)
    draft = parse_draft({
        "actions": [{"id": "copy", "kind": "keys", "keys": ["cmd", "c"]}],
        "bindings": [None] * 6,
        "dials": [None] * 8,
        "menus": [{"name": "youtube", "title": "Home", "items": [
            {"key": 0, "label": "Copy", "action": "copy"}]}],
    })
    assert [menu.name for menu in draft.menus] == ["youtube"]
    merged = merge_draft_into_config(path.read_text(), draft)
    assert "[custom_menus.youtube]" in merged
    # One menu is one screen, written as the single page the file format keeps.
    assert 'root = "main"' in merged
    # Everything the editor does not model survives the merge.
    assert "# a comment the editor must preserve" in merged
    assert "[feedback]\nenabled = false\nduration_ms = 180" in merged
    assert '[labels]\nkey0 = "One"' in merged


# -- Saving and reloading menus and content keys -------------------------------


def test_menus_and_content_keys_save_and_reload_through_the_service(tmp_path: Path) -> None:
    path = write_config(tmp_path, EMPTY)
    server, thread = running_server(config=path)
    try:
        status, saved = request(server, "POST", "/api/save", built_draft())
        assert status == 200, saved
        # No daemon hook is attached to a bare editor, so it says so honestly.
        assert saved["applied"] is None
        assert "Saved to" in saved["persistence"]
    finally:
        stop_server(server, thread)

    reloaded = load_config(path)
    home = reloaded.custom_menus["youtube"]
    # The file format keeps one page per menu; the interface shows one screen.
    assert set(home.pages) == {"main"}
    items = {item.key: item for item in home.pages["main"].items}
    assert items[0].action == "copy"
    assert items[1].submenu == "youtube-more"  # a content key that opens another menu
    assert reloaded.custom_menus["youtube-more"].pages["main"].items[0].action == "paste"


def test_menu_order_round_trips_through_a_save(tmp_path: Path) -> None:
    path = write_config(tmp_path, EMPTY)
    payload = built_draft()
    payload["menus"] = list(reversed(payload["menus"]))
    save_draft_config(path, parse_draft(payload))
    reloaded = load_config(path)
    assert list(reloaded.custom_menus) == ["youtube-more", "youtube"]


def test_renaming_a_menu_rewrites_the_action_that_opens_it(tmp_path: Path) -> None:
    path = write_config(tmp_path, EMPTY)
    save_draft_config(path, parse_draft(built_draft()))
    renamed = built_draft()
    renamed["menus"][0]["name"] = "video"
    renamed["actions"][2]["menu"] = "video"  # the editor rewrites the opener in step
    save_draft_config(path, parse_draft(renamed))
    reloaded = load_config(path)
    assert set(reloaded.custom_menus) == {"video", "youtube-more"}
    assert reloaded.actions["youtube-menu"].menu_name == "video"
    assert reloaded.bindings["key0"] is reloaded.actions["youtube-menu"]


# -- Binding a deck key to open a menu ----------------------------------------


def test_binding_a_deck_key_to_open_a_menu_survives_a_save(tmp_path: Path) -> None:
    path = write_config(tmp_path, EMPTY)
    server, thread = running_server(config=path)
    try:
        status, saved = request(server, "POST", "/api/save", built_draft())
        assert status == 200, saved
    finally:
        stop_server(server, thread)
    reloaded = load_config(path)
    binding = reloaded.bindings["key0"]
    assert binding.action == "menu"
    assert binding.menu_name == "youtube"
    assert reloaded.actions["youtube-menu"] is binding


# -- Refusing a delete of a menu that is still opened --------------------------


def test_deleting_a_referenced_menu_is_refused_by_the_parser(tmp_path: Path) -> None:
    path = write_config(tmp_path, EMPTY)
    save_draft_config(path, parse_draft(built_draft()))
    before = path.read_text()
    stripped = built_draft()
    stripped["menus"] = []  # remove the menu but leave the opener action behind
    with pytest.raises(ControlPlaneEditorError) as info:
        parse_draft(stripped)
    assert "must reference an editor menu" in str(info.value)
    assert path.read_text() == before  # nothing was written


def test_the_save_route_refuses_a_referenced_menu_delete_with_a_reason(tmp_path: Path) -> None:
    path = write_config(tmp_path, EMPTY)
    server, thread = running_server(config=path)
    try:
        assert request(server, "POST", "/api/save", built_draft())[0] == 200
        before = path.read_text()
        stripped = built_draft()
        stripped["menus"] = []
        status, body = request(server, "POST", "/api/save", stripped)
        assert status == 400
        assert "must reference an editor menu" in body["error"]
        assert path.read_text() == before
    finally:
        stop_server(server, thread)


# -- Refusing an invalid draft -------------------------------------------------


def test_an_invalid_menu_draft_is_refused_with_a_reason() -> None:
    # An empty menu is deliberately allowed now (it can be filled in later), so
    # the boundary the parser still enforces is the six-button ceiling.
    too_many = built_draft()
    too_many["menus"][1]["items"] = [{"key": key, "label": f"Extra {key}", "action": "paste"} for key in range(7)]
    with pytest.raises(ControlPlaneEditorError) as info:
        parse_draft(too_many)
    assert "at most 6 buttons" in str(info.value)

    missing_menu = built_draft()
    missing_menu["menus"][0]["items"][1] = {"key": 1, "label": "Missing", "menu": "nope"}
    with pytest.raises(ControlPlaneEditorError) as info:
        parse_draft(missing_menu)
    assert "which is not a menu" in str(info.value)

    cyclic = built_draft()
    cyclic["menus"][1]["items"] = [{"key": 0, "label": "Back home", "menu": "youtube"}]
    with pytest.raises(ControlPlaneEditorError) as info:
        parse_draft(cyclic)
    assert "cycle" in str(info.value)

    duplicate_key = built_draft()
    duplicate_key["menus"][0]["items"][1] = {"key": 0, "label": "Clip", "action": "copy"}
    with pytest.raises(ControlPlaneEditorError) as info:
        parse_draft(duplicate_key)
    assert "duplicate or invalid buttons" in str(info.value)


# -- End to end: the RUNNING daemon opens the menu on a key press -------------


class _FakeDeck:
    publishes_feed_state = False

    def __init__(self) -> None:
        self.event = None
        self.clear_calls = 0

    def on_event(self, callback):
        self.event = callback
        return callback

    def on_disconnect(self, callback):
        self.disconnect = callback
        return callback

    def open(self):
        return self

    def close(self) -> None:
        pass

    def clear(self, key=None) -> None:
        self.clear_calls += 1

    def set_key_image(self, key, image) -> None:
        pass

    def set_brightness(self, level: int) -> None:
        pass


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _request_port(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    return response.status, data


def test_a_running_daemon_opens_the_saved_menu_on_a_key_press(tmp_path: Path) -> None:
    from akp03.backend import LogBackend
    from akp03.daemon import Daemon
    from akp03.device import Event
    from akp03.feed import ControlFeed

    path = write_config(tmp_path, EMPTY)
    created: list[_FakeDeck] = []
    feed = ControlFeed(connected=False, phase="waiting")
    daemon = Daemon(
        path,
        state_path=tmp_path / "state.json",
        deck_factory=lambda device: created.append(_FakeDeck()) or created[-1],  # type: ignore[arg-type,return-value]
        backend=LogBackend(logging.getLogger("akp03d.keys")),
        feed=feed,
    )
    port = _free_port()
    thread = threading.Thread(target=lambda: daemon.run(tmp_path / "d.sock", editor_port=port), name="menu-live-daemon")
    thread.start()
    try:
        assert _wait_for_port(port), "daemon never hosted the editor"
        assert _wait_until(lambda: daemon.deck is not None and bool(created)), "deck never connected"
        deck = created[-1]
        assert deck.event is not None

        status, saved = _request_port(port, "POST", "/api/save", built_draft())
        assert status == 200, saved
        assert saved["applied"] is True

        # Nothing opens a menu until the bound key is pressed.
        assert daemon.custom_menu is None
        deck.event(Event("key", "key0", True, None, b""))
        assert _wait_until(lambda: daemon.custom_menu is not None), "the key did not open the menu"
        assert daemon.custom_menu.state.title == "Home"
        # All six keys belong to the page: no key is spent on Back or Home.
        assert daemon.custom_menu.state.labels == ("Copy", "More", "", "", "", "")
        assert daemon.custom_menu.state.selected_key == 0
        assert 4 not in daemon.labels and 5 not in daemon.labels

        # The top knob turns to move the highlight, then presses to select it.
        deck.event(Event("knob_turn", "top", None, 1, b""))
        assert _wait_until(lambda: daemon.custom_menu.state.selected_key == 1)
        assert daemon.labels[1].text == "More"
        assert daemon.labels[1].background == "#173f5f"
        deck.event(Event("knob_press", "top", True, None, b""))
        assert _wait_until(lambda: daemon.custom_menu.state.title == "More")

        # The bottom-left knob goes back a level.
        deck.event(Event("knob_press", "bottom_left", True, None, b""))
        assert _wait_until(lambda: daemon.custom_menu.state.title == "Home")

        # A content key carrying a submenu link still navigates directly.
        deck.event(Event("key", "key1", True, None, b""))
        assert _wait_until(lambda: daemon.custom_menu.state.title == "More")
    finally:
        daemon.stop.set()
        thread.join(timeout=6)
    assert not thread.is_alive()


# -- The shipped page carries every control the flow needs ---------------------


def test_the_shipped_page_carries_the_menu_builder_controls() -> None:
    """The browser is the only configuration plane, so the menu-building flow
    must not silently lose a control in a refactor.

    The flat model has no page grid: the Menus view (``flow.js``) is a list of
    menus, each a title and up to six buttons, and a button either runs an action
    or opens another menu.  This keeps every control that flow depends on.
    """
    from importlib.resources import files

    assets = files("akp03").joinpath("webui_assets")
    page = assets.joinpath("index.html").read_text()
    script = assets.joinpath("app.js").read_text()
    flow = assets.joinpath("flow.js").read_text()

    # The Menus view is where a menu is created and designed.
    assert 'id="fxMenusRoot"' in page and 'data-view="menus"' in page

    # Create a menu from scratch (named) even with no menus present.
    assert "data-fx-newmenu" in flow and 'id="fxNewMenuTitle"' in flow and "Add menu" in flow
    assert "data-fx-newmenuform" in flow and "fxCreateMenu" in flow

    # A menu is a title and up to six buttons; a button opens a menu or runs an
    # action, and both routes live in the same one panel.
    assert "const FX_KEYS = [0, 1, 2, 3, 4, 5]" in flow
    assert "function fxMountDeck(menu)" in flow and "mountDeckDrawing(mount, fxDeckView(menu))" in flow
    # The panel itself is the deck's own component, mounted here rather than written
    # again: this page supplies only what a button *is* and what a change means.
    panel = assets.joinpath("keypanel.js").read_text()
    # The Menus page mounts one target for whichever control its panel is answering for:
    # a tile, a button under the keys, or one part of one dial.
    assert "mountButtonPanel(root, fxSlotKey !== null && fxCapKey === null && fxDialControl === null" in flow
    assert "menusCapTarget(fxMenuName, fxCapKey)" in flow
    assert "menusDialTarget(fxMenuName, fxDialControl)" in flow

    # The dials are drawn as dials: three knobs in the frame, and a press of one asks the
    # panel for that whole dial.  Six chips of prose were what they replaced.
    assert "const FX_DIALS = [" in flow
    # The knobs themselves are drawn by the shared drawing, which every page mounts.
    drawing = assets.joinpath("deckdraw.js").read_text()
    assert 'data-dial="${index}"' in drawing and 'data-control="${entry.control}"' in drawing
    assert "function fxDialsSaid(control, claims)" in flow
    assert "function fxDialParts(control)" in flow
    # The left dial is drawn but not settable: it is the way back out of a menu.
    assert "{control: 'bottom_left', name: 'Left dial', short: 'Left', big: false}," in flow
    assert "value: 'Back one level', note: 'Always, while a menu is open.'" in flow
    assert "fixed: true" in flow
    assert "function menusButtonTarget(menuName, key)" in flow
    assert "fxSetButtonMenu" in flow and "fxSetButtonAction" in flow
    # Both routes are rows of the one shared chooser, so "opens a menu" and "runs an
    # action" read the same on either page.
    assert "data-value=" in panel and "Open a menu" in panel and "Opens “" in panel
    assert "[data-menus-toggle]" in panel and "actionChoiceList()" in panel
    # Clearing a button is a row in that same list - the deck's own "Nothing yet" -
    # and it is offered wherever a button may be left with nothing at all.
    assert 'data-label="Nothing yet"' in panel and "target.clearable" in panel
    assert "iconPickerHTML" in panel and "iconPickerHTML" not in flow
    assert "fxClearButton" in flow
    assert "fxDeleteMenu" in flow and "data-fx-renamemenu" in flow

    # The deck's own picker offers the declared menus (bind a key to open one).
    assert "__menu__:" in script and "assignMenuOpen" in script and "Open: " in script
    assert "renderRoutes" in script and "routeSection" in script
    assert "openMenuInChooser" in script and "assignToTarget" in script
    # Assigning a dial slot is a row of the one shared panel: each slot is a row that reads
    # "＋ Assign…" until it holds something.  This used to assert a button of its own that
    # only the older slot editor had.
    assert 'data-slot="${esc(slot.slot)}"' in panel and "＋ Assign…" in panel

    # Six content keys, and no reserved Back/Home tile on a menu screen.
    assert "[0, 1, 2, 3, 4, 5].map" in script
    assert 'class="slot reserved"' not in script
    # A key labelled "Copy" that runs the Copy action must not read "Copy Copy":
    # the tile's second line only appears when it says something new.
    assert "sameWords" in script
    assert "<small>${esc(itemSummary(item))}</small>" not in script
    # The deck's own dials are explained in the live vocabulary: Back is the bottom-left
    # knob, and the big one is the Big dial.  "top knob" was the older page's name for it,
    # and it went with that page.
    assert "bottom-left knob" in script
    assert "Big dial" in flow and "Big dial" in panel


def test_the_picture_library_and_its_picker_have_one_definition():
    """One library, one picker, used by both pages.

    A picture added to a pack — or drawn by hand and dropped in later — has to
    appear on the Deck page's key panel and the Menus page's button panel with no
    second change, so the fetch, the search, the "no picture" option and the tile
    markup all live in one file.  Only what a pick *means* differs, and that is
    the callback each page hands over.  This guards against the drift that
    produced three near-copies of the same grid.
    """
    assets = Path(__file__).resolve().parents[1] / "akp03" / "webui_assets"
    library = (assets / "library.js").read_text()
    shell = (assets / "index.html").read_text()
    pages = {
        "app.js": (assets / "app.js").read_text(),
        "keypanel.js": (assets / "keypanel.js").read_text(),
        "flow.js": (assets / "flow.js").read_text(),
    }

    # The library is read in exactly one place.
    assert library.count("fetch('/api/icons')") == 1
    for name, source in pages.items():
        assert "/api/icons'" not in source, f"{name} fetches the library itself"

    # The picker is rendered in exactly one place now - the panel component - and the
    # pages only mount it.  "One definition" is therefore a fact about the source
    # rather than a convention both pages have to keep in step.
    assert "iconPickerHTML(" in pages["keypanel.js"]
    assert "wireIconPicker(" in pages["keypanel.js"]
    for name in ("app.js", "flow.js"):
        assert "iconPickerHTML(" not in pages[name], f"{name} renders its own picker"
        assert "wireIconPicker(" not in pages[name], f"{name} wires its own picker"
    for name, source in pages.items():
        assert "data-lib-ref" not in source, f"{name} draws its own picture tiles"

    # The picker offers the whole library, not a slice of it.
    assert "slice(0, 11)" not in library and ".slice(0, 11)" not in library
    assert "libraryIcons().filter" in library

    # It groups the pictures under headings, and it has no search box: a short
    # list under four headings is quicker to scan than to type at.  Neither page
    # may grow its own finder or its own headings.
    assert "lib-heading" in library and "librarySections()" in library
    assert "libFilter" not in library and "lib-search" not in library
    for name in ("keypanel.js", "flow.js"):
        assert "onRerender" not in pages[name], f"{name} still filters the shared picker"
        assert "lib-heading" not in pages[name], f"{name} draws its own headings"

    # And the shell loads it before the pages that use it.
    assert shell.count("library.js") == 1 and shell.count("library.css") == 1
    assert shell.index("library.js") < shell.index("app.js") < shell.index("keypanel.js")


def test_the_button_panel_separates_its_parts_in_whichever_container_it_lands_in():
    """The panel's parts are cards, and the spacing belongs to the panel itself.

    The panel was once built once (``fxButtonPanelHTML``) and shown in two
    containers: the Menus tab's ``.fx-insp``, and the deck's ``#fxButtonPanel``,
    which is a bare div with no layout of its own.  Relying on the container's gap
    left the deck's cards flush against each other - so the spacing was moved onto a
    selector covering both.  Both are gone now: the panel *is* the component, it
    spaces its own cards, and there is no container that can fail to provide a gap.
    """
    assets = Path(__file__).resolve().parents[1] / "akp03" / "webui_assets"
    css = (assets / "flow.css").read_text()
    keypanel_css = (assets / "keypanel.css").read_text()
    panel = (assets / "keypanel.js").read_text()

    # Every part of the panel is a card, built in the one skeleton: the three value
    # rows' '.panel-row', the summary row a button is read on, and the row a dial's
    # three slots live in.
    assert panel.count('class="panel-row"') == 6
    assert panel.count('class="row-value') == 4
    assert "border-radius: 15px" in css

    # The card is defined once, for the one panel that exists.
    assert ".key-panel .panel-row {" in css
    # One label style for every small heading in it.
    assert "/* One label style for every small heading in the panel." in css
    assert ".summary-list dt,\n.key-panel .row-label {" in css
    # Neither panel's own class survives as a second copy of a rule this component
    # already has: the Menus page draws none of this markup now.
    for gone in (".fx-sec", ".fx-cardhead", ".fx-thumb", ".fx-picks", ".fx-rlabel"):
        assert f"\n{gone} " not in css and f"\n{gone}," not in css, gone
    # The deck panel may only set the spacing under its label, never the type itself.
    assert ".key-panel .row-label { margin: 0; }" in keypanel_css
    assert "text-transform" not in keypanel_css.split(".key-panel .row-label")[1].split("}")[0]

    # The panel spaces its own cards, and no container has to know about it.
    assert ".key-panel {\n  display: grid;\n  gap: 12px;\n}" in keypanel_css
    assert ".fx-insp, #fxButtonPanel" not in css

    # The shortcut row is a value row with the deck's own editor behind it, not a
    # note saying shortcuts live elsewhere: "shortcut is connected to a button, and
    # here we are configuring a button... they should be the same component."
    assert 'data-panel-open="shortcut"' in panel
    assert 'class="shortcut-editor" data-panel="shortcut-editor" hidden' in panel
    assert "Shortcuts are edited on the Deck page" not in panel
    assert ".fx-hint {" in css and "border-top" in css

    # The rule that never matched any markup (.fx-group) is gone rather than left
    # beside the class that replaced it.  The name may still appear in the comment
    # explaining its removal, so look for the rule itself.
    assert ".fx-group {" not in css


def test_the_menu_list_is_folded_away_until_it_is_asked_for() -> None:
    """The menus are a disclosure, not a wall.

    Listed above the actions they push the list people came for off the bottom of
    the panel - fine with two menus, useless with forty.  The row names the menu the
    button already opens, so nothing that matters is hidden while it is closed.  The
    disclosure is the shared chooser's own row now, so the deck's panel gets the same
    treatment as the Menus page's: one list, one builder, one fold.
    """
    from importlib.resources import files

    panel = files("akp03").joinpath("webui_assets").joinpath("keypanel.js").read_text()
    assert "data-menus-toggle" in panel, "no row to unfold the menus"
    assert "const menusOpen = box.dataset.menusOpen === '1';" in panel
    assert "box.dataset.menusOpen = menusOpen ? '0' : '1';" in panel
    # Closed means closed: the rows are not in the markup until it is opened.
    assert "menusOpen ? menuChoiceRows().map" in panel
    assert 'aria-expanded="${menusOpen}"' in panel
    # And an open list is re-filled rather than rebuilt, so a filter being typed into
    # it is not thrown away by the repaint that follows.
    assert "if (toggle) {" in panel


def test_a_pick_belongs_to_the_panel_it_came_from() -> None:
    """A picture or an action picked in one panel must not land on another's button.

    Both panels are on screen at once - the Menus inspector and the deck chain's
    panel - and a pick was applied to the Menus page's own selection instead, so
    choosing a picture for a button inside a sub-menu changed the first button of
    the menu above it.  A mount now holds the *target* it was mounted for: the
    reading and the three writes close over that one button, so a pick cannot reach
    a selection the panel was not mounted for.
    """
    from importlib.resources import files

    assets = files("akp03").joinpath("webui_assets")
    flow = assets.joinpath("flow.js").read_text()
    panel = assets.joinpath("keypanel.js").read_text()
    # The component asks the mount's own target, on every click and every paint...
    assert "state.target = target;" in panel
    assert "const target = state && state.target;" in panel
    assert "state.target.pick && state.target.pick(event, root)" in panel
    # ...and the Menus page mounts one target per button, named when it is mounted.
    # The Menus page mounts one target for whichever control its panel is answering for:
    # a tile, a button under the keys, or one part of one dial.
    assert "mountButtonPanel(root, fxSlotKey !== null && fxCapKey === null && fxDialControl === null" in flow
    assert "menusCapTarget(fxMenuName, fxCapKey)" in flow
    assert "menusDialTarget(fxMenuName, fxDialControl)" in flow
    assert "mountButtonPanel($('#fxButtonPanel'), menusButtonTarget(menuName, key));" in flow
    assert "function menusButtonTarget(menuName, key)" in flow
    # Its three writes are the button's own, never the page's selection.
    assert "setIcon: reference => fxSetIcon(menuName, key, reference)," in flow
    assert "await fxSetButtonAction(menuName, key, value);" in flow
    # Choosing an action also brings the picture the list drew beside it, so the button
    # does not sit blank until the icon is found by hand.  Changing it afterwards is
    # still the icon row's job.
    assert "if (icon) await fxSetIcon(menuName, key, icon);" in flow
    assert "return fxSetButtonMenu(menuName, key, value.slice('__menu__:'.length));" in flow
    for gone in (
        "fxSetButtonMenu(fxMenuName, fxSlotKey",
        "fxSetButtonAction(fxMenuName, fxSlotKey",
        "fxSetIcon(fxMenuName, fxSlotKey",
    ):
        assert gone not in flow, gone
    # The one shared picker hands the button on, so a caller can tell the two apart.
    library = assets.joinpath("library.js").read_text()
    assert "onPick(button.dataset.libRef || null, button)" in library


def test_an_empty_button_says_what_to_do_instead_of_nothing() -> None:
    """An unassigned button's row is a control, not a description of emptiness.

    "Nothing yet / Nothing yet - press it and nothing happens" told you what was
    missing without telling you that the row was the thing to press.  It is now one
    Assign row, and the parts that cannot be used yet say why.
    """
    from importlib.resources import files

    assets = files("akp03").joinpath("webui_assets")
    panel = assets.joinpath("keypanel.js").read_text()
    flow = assets.joinpath("flow.js").read_text()
    shared = support.stylesheets(assets)
    # The empty state is the deck's own Assign row: the value row takes it as its
    # label and the shared dashed-edge class marks it as nothing-here-yet.
    assert 'class="row-value is-assign"' in panel
    assert "＋ Assign…" in panel
    # The action row is painted by a function of its own; the name inside it is short
    # because the row is all that function is about.
    assert "function paintActionRow(root, view) {" in panel
    assert "pick.classList.toggle('is-assign', !view.assigned);" in panel
    assert "Nothing yet — press it and nothing happens" not in panel + flow
    # The row says what it will take, and the two parts that need an assignment
    # explain themselves rather than sitting there dead: there is no keys to send,
    # and on a menu button there is nowhere for a picture to live.
    assert "Menu or action" in panel
    assert "Nothing to send yet" in panel
    assert "An icon goes on a button that does something. Assign one first." in panel
    # How the panel knows: the target says whether this button can hold a picture
    # at all, and the Menus page says no for a button with nothing assigned.
    assert "iconUsable: job.iconUsable !== false," in panel
    assert "iconUsable: Boolean(button)," in flow
    # And it reads as a control: the app's dashed edge for "nothing here yet", from
    # one shared rule, so the panel cannot drift from the rest of the app.
    assert ".is-assign { border: 1px dashed #7fb3ad !important;" in shared


def test_the_chain_highlight_names_its_menu_not_just_the_button_number() -> None:
    """A level's highlighted tile must belong to that level.

    Selecting the second button of a sub-menu lit up the second button of the menu
    above it, because the highlight compared the button number and never asked which
    menu the selection was in.
    """
    from importlib.resources import files

    flow = files("akp03").joinpath("webui_assets").joinpath("flow.js").read_text()
    # The selection is matched with the menu as well as the number...
    assert "const selected = Boolean(fxChainButton && fxChainButton.menu === menu.name && fxChainButton.key === key);" in flow
    # ...the level above marks the item that leads into the open one...
    assert "const on = selected ? ' on' : (leadsDeeper ? ' on via' : '');" in flow
    # ...and nothing highlights on a bare button-number match any more.
    assert "fxDeep.some(step => step.key === key)" not in flow
    # The trail mark is quieter than the selected button, so two tiles lit at once
    # cannot read as two selections.
    css = files("akp03").joinpath("webui_assets").joinpath("flow.css").read_text()
    assert ".fx-slot.on.via { border-color: #edc3a6;" in css


def test_hiding_the_deck_panel_does_not_close_the_menus_editors() -> None:
    """Each mount keeps its own state, so hiding one cannot disturb another.

    fxHideButtonPanel() runs on every render with no deck chain to draw - which is
    always, on the Menus page - and it used to clear the edit flag for the whole
    page, dropping the Menus panel out of its editors as soon as any chooser asked
    for a render: opening the icon list threw the panel back to its summary.  The
    state lives with the mount now, and hiding a mount only hides it.
    """
    from importlib.resources import files

    assets = files("akp03").joinpath("webui_assets")
    flow = assets.joinpath("flow.js").read_text()
    panel = assets.joinpath("keypanel.js").read_text()
    body = flow[flow.index("function fxHideButtonPanel()"):]
    body = body[:body.index("\n}")]
    assert "editing" not in body, "hiding the deck panel touches the editors"
    assert "unmountButtonPanel($('#fxButtonPanel'));" in body
    # One state per mount, keyed by the DOM it was mounted in and by the target it
    # answers for, so no page can clear another's editors.
    assert "const panelMounts = new Map();" in panel
    assert "function panelState(root) {" in panel
    assert "if (state.token !== target.token) {" in panel
    # And Edit… is a toggle, so a panel can always be closed from where it opened.
    assert "state.editing = !state.editing;" in panel


def test_the_chooser_lists_only_the_actions_that_are_genuinely_your_own() -> None:
    from pathlib import Path

    panel = (Path(__file__).resolve().parent.parent / "akp03" / "webui_assets" / "keypanel.js").read_text()

    # The ready-made sections above already answer for their own ids, so a saved copy of
    # one of them was listed a second time underneath and the group grew with every action
    # ever touched.  What is left is a menu you built or an action you named yourself.
    assert "function readyActionsById()" in panel
    assert "const mine = (state.actions || []).filter(entry => !ready.has(entry.id));" in panel
    # The heading says where those actions live rather than claiming them as yours: every
    # one of them is in your deck file, and "Your own" read as "yours rather than the
    # app's", which is not what the group is.
    assert "heading: `From your deck file \u00b7 ${mine.length}`" in panel
    assert "groups.push({heading: 'Your own', rows: state.actions.map" not in panel


def test_a_changed_saved_copy_says_so_and_offers_the_way_back() -> None:
    from pathlib import Path

    panel = (Path(__file__).resolve().parent.parent / "akp03" / "webui_assets" / "keypanel.js").read_text()

    # A ready-made action you have changed keeps your version, but it has to say that it
    # is yours and offer the way back: without this the row showed a saved copy's words
    # with no hint that the app ships another one, which is how the volume rows came to
    # look like the app's own keys while the deck sent something else.
    assert "function readyVersionDiffers(entry)" in panel
    assert "function savedCopyDiffersFromReady(actionId)" in panel
    assert "function putReadyVersionBack(id)" in panel
    assert 'data-tool="reset-action"' in panel
    assert "const changed = view.assigned ? savedCopyDiffersFromReady(view.actionId) : null;" in panel
    # The ready-made reading is copied, not referenced, so writing the draft back out can
    # never edit the catalogue itself.
    assert "const fresh = JSON.parse(JSON.stringify(ready));" in panel
