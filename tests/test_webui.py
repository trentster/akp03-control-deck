from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

from akp03.webui import make_server, parse_draft

import support


def request(server, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    return response.status, data


def request_text(server, path: str) -> tuple[int, str, str]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read().decode()
    content_type = response.getheader("Content-Type") or ""
    connection.close()
    return response.status, content_type, body


def running_server():
    server = make_server(0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    return server, thread


def test_state_endpoint_returns_in_memory_draft() -> None:
    server, thread = running_server()
    try:
        status, body = request(server, "GET", "/api/state")
        assert status == 200
        assert body["draft"]["bindings"] == ["record", "marker", "dim", "studio", None, None]
        # Each menu is one screen of buttons, and a button that opens a menu
        # names it rather than pointing at a page.
        assert [menu["name"] for menu in body["draft"]["menus"]] == ["studio", "display"]
        studio_items = body["draft"]["menus"][0]["items"]
        assert studio_items[0]["key"] == 0
        assert {"key": 2, "label": "Display", "menu": "display"} in studio_items
    finally:
        server.shutdown(); thread.join(); server.server_close()


def test_deck_page_serves_the_physical_akp03_hardware_layout() -> None:
    server, thread = running_server()
    try:
        status, content_type, page = request_text(server, "/")
        assert status == 200
        assert "text/html" in content_type
        # The page holds the drawing's place; the drawing holds the hardware layout.
        assert 'id="deckDevice"' in page
        _, _, drawing = request_text(server, "/assets/deckdraw.js")
        assert 'aria-label="Six editable LCD keys"' in drawing
        # The three caps under the LCD keys and the three dials are all assignable.
        assert drawing.count("control: 'button") == 3
        assert drawing.count("{ control: '") >= 6
        _, _, script = request_text(server, "/assets/app.js")
        assert '<span class="button-cap"' not in page
        assert 'data-control="${cap.control}" data-cap="${index}"' in drawing
        assert "The three buttons under the LCD keys. Choose one to assign what it does." in script
        assert 'data-dial="' not in page
        assert '<span class="dial' not in drawing
        assert 'data-control="${entry.control}" data-dial="${index}"' in drawing
        for control in ("top", "bottom_left", "bottom_right"):
            assert f"control: '{control}'" in drawing, control
        assert "Choose one to assign what its press and each way it turns does." in script
        # Nothing is offered that cannot be given something: every control on this
        # page now says what it is for, so the old "not assignable yet" notes are gone.
        assert "not yet assignable" not in page
        assert "cannot be assigned yet" not in page
        # The panel below the deck is the button panel component, mounted where the
        # deck's own panel goes.  The page holds the two places; the component holds
        # the markup it paints into them, so neither page draws a row of its own.
        assert 'id="deckPanel" class="key-panel"' in page
        assert 'id="fxButtonPanel" class="key-panel" hidden' in page
        assert "Editing selected control" not in page
        assert 'aria-label="Selected control"' in page
        for gone in ('id="selectedControl"', 'id="keyPanel"', 'id="keyNumber"',
                     'id="summaryRow"', 'id="editJob"', 'id="editArea"',
                     'id="pickAction"', 'id="actionChoices"', 'id="iconChoices"'):
            assert gone not in page, gone
        assert "What should this key do?" not in page
        assert 'aria-describedby="deckActionHelp"' not in page
        # The retired page-per-picker controls are gone, not merely hidden.
        for gone in ('id="assignmentCard"', 'id="artworkCard"', 'id="chooseDeckAction"', 'id="chooseArtwork"'):
            assert gone not in page, gone

        # The panel's own module carries the markup and the behaviour, and is served.
        status, content_type, script = request_text(server, "/assets/keypanel.js")
        assert status == 200 and "javascript" in content_type
        # One panel per button, in two states: at rest it says what the button has -
        # the kind of thing assigned, then its label, its shortcut and its picture -
        # and Edit opens the editors that change them.  Nothing here navigates away
        # to another page: a button is one place you look.
        assert 'aria-live="polite"' in script
        for hook in ('data-panel-row="summary"',
                     'data-panel="summary-label"', 'data-panel="summary-shortcut"',
                     'data-panel="summary-thumb"', 'data-panel="summary-image"',
                     'data-panel-edit>Edit…</button>',
                     'data-panel="editors" hidden', 'data-panel-open="action"',
                     'data-panel-open="shortcut"', 'data-panel-open="icon"',
                     'data-panel="action-choices"', 'data-panel="shortcut-editor" hidden',
                     'data-panel="icon-choices"'):
            assert hook in script, hook
        # Each value is named for what it is, because the value itself is only
        # ever a name and no name describes its own behaviour.
        for name in ('Label', 'Shortcut'):
            assert f'<dt>{name}</dt>' in script, name
        # The picture line is the one a control with no screen goes without, so its name
        # is on the row instead of in the row: that is what lets the row be taken away.
        assert '<dt data-panel="summary-icon-label">Icon</dt>' in script
        assert 'data-panel="summary-icon-cell"' in script
        # The wording this replaced is gone, not merely reworded in one spot.
        for gone_words in ('What it does', 'What it looks like', 'Its shortcut', 'Try it now'):
            assert gone_words not in script and gone_words not in page, gone_words
        for behaviour in ("mountButtonPanel", "paintEditorRows", "fillActionChoices", "fillIconChoices",
                          "stageShortcut", "pickJob", "pickIcon", "renderShortcutEditor"):
            assert behaviour in script, behaviour
        # One panel for both targets: a menu key is answered by the same three rows.
        assert "targetIsSlot()" in script
        # The card's own sentence is built where the card is built - "Editing LCD Key
        # 1: ..." on the deck, "Editing Menu button 3: ..." on a menu button - so the
        # two mounts cannot describe the same thing differently.
        assert "Editing ${type}: ${title}." in script
        assert "const type = kind === 'key' ? `LCD key ${key + 1}` : `Menu button ${key + 1}`;" in script

        # The panel's own toggles need explicit `[hidden]` rules: a `display: grid`
        # or `inline-flex` on the element silently beats the UA's rule, which once
        # left a stale shortcut showing for a key with no action at all.
        status, content_type, panel_css = request_text(server, "/assets/keypanel.css")
        assert status == 200 and "text/css" in content_type
        for guard in (".panel-row[hidden]", ".row-choices[hidden]", ".row-thumb[hidden]",
                      ".panel-context[hidden]", ".row-new-form[hidden]",
                      '[data-panel="editors"][hidden]'):
            assert guard in panel_css, guard
        assert "display: none" in panel_css

        # Every stylesheet the page loads, not one named file: a rule's address is not
        # its identity, so a rule that moved to the file that owns the thing it styles
        # must still be found here.
        css = ""
        for name in support.stylesheet_names():
            status, content_type, served = request_text(server, f"/assets/{name}")
            assert status == 200, f"{name} did not serve"
            assert "text/css" in content_type, name
            css += served
        _, _, drawing_css = request_text(server, "/assets/deckdraw.css")
        assert ".deck-body {" in drawing_css
        # One knob size, and one larger: the two small dials are the pair below the big one.
        assert ".dial {" in drawing_css and ".dial-large {" in drawing_css
        assert ".deck-well-cap" in drawing_css
        assert "overflow-y: auto" in css
        assert "gap: 14px" in css
        assert "overflow-wrap: anywhere" in css
        assert ".deck-num" in drawing_css
        assert ".selected-control" in css
        assert ".tone-gold" in css
        assert ".control-number" in css
        assert "prefers-reduced-motion" in css

        status, content_type, script = request_text(server, "/assets/app.js")
        assert status == 200
        assert "javascript" in content_type
        assert "renderDeck" in script
        assert "keyTone" in script
        assert "aria-pressed" in script
        # The page says what the selected control does.  The wording lives in the panel now,
        # and this reading is the page's scripts rather than app.js alone: the sentence it
        # used to find belonged to the older slot editor, which is gone.
        assert "What this dial does" in support.scripts()
        # A content key can still be pointed at another page of its own menu,
        # now offered as one of the chooser's routes.
        assert "Go to a page" in script
        assert "pageTargets" in script
        assert "submenu: targetPage.id" in script
    finally:
        server.shutdown(); thread.join(); server.server_close()


def test_draft_endpoint_replaces_validated_state() -> None:
    server, thread = running_server()
    try:
        _, current = request(server, "GET", "/api/state")
        draft = current["draft"]
        draft["bindings"][0] = "marker"
        status, updated = request(server, "POST", "/api/draft", draft)
        assert status == 200
        assert updated["draft"]["bindings"][0] == "marker"
        _, observed = request(server, "GET", "/api/state")
        assert observed["draft"]["bindings"][0] == "marker"
    finally:
        server.shutdown(); thread.join(); server.server_close()


def test_draft_endpoint_accepts_a_menu_target_and_rejects_a_menu_cycle() -> None:
    server, thread = running_server()
    try:
        _, current = request(server, "GET", "/api/state")
        draft = current["draft"]
        studio = draft["menus"][0]
        studio["items"][0] = {"key": 0, "label": "Display", "menu": "display"}
        status, updated = request(server, "POST", "/api/draft", draft)
        assert status == 200
        assert updated["draft"]["menus"][0]["items"][0] == {
            "key": 0, "label": "Display", "menu": "display",
        }

        # A menu that opens itself can never be navigated out of: refused.
        malformed = json.loads(json.dumps(updated["draft"]))
        malformed["menus"][0]["items"][0] = {
            "key": 0, "label": "Studio Home", "menu": "studio",
        }
        status, body = request(server, "POST", "/api/draft", malformed)
        assert status == 400
        assert "cycle" in body["error"]
    finally:
        server.shutdown(); thread.join(); server.server_close()


def test_draft_endpoint_rejects_unsafe_or_malformed_actions() -> None:
    server, thread = running_server()
    try:
        unsafe = {"actions": [{"id": "bad", "kind": "shell", "shell": ["/bin/sh"]}], "bindings": [None] * 6, "dials": [None] * 8, "menus": []}
        status, body = request(server, "POST", "/api/draft", unsafe)
        assert status == 400
        assert "safe" in body["error"]

        malformed = {"actions": [{"id": "bad", "kind": "keys", "keys": ["x"], "url": "https://example.invalid"}], "bindings": [None] * 6, "dials": [None] * 8, "menus": []}
        status, body = request(server, "POST", "/api/draft", malformed)
        assert status == 400
        assert "safe" in body["error"]
    finally:
        server.shutdown(); thread.join(); server.server_close()


def test_menu_validation_accepts_all_six_content_keys_and_rejects_out_of_range() -> None:
    valid = {"actions": [{"id": "copy", "kind": "keys", "keys": ["ctrl", "c"]}], "bindings": [None] * 6, "dials": [None] * 8, "menus": [{"name": "tools", "title": "Tools", "items": [{"key": 0, "label": "Copy", "action": "copy"}, {"key": 3, "label": "Again", "action": "copy"}]}]}
    draft = parse_draft(valid)
    assert [item.key for item in draft.menus[0].items] == [0, 3]

    # The sixth key is content like any other: nothing is reserved.
    top_key = json.loads(json.dumps(valid))
    top_key["menus"][0]["items"][0]["key"] = 5
    assert sorted(item.key for item in parse_draft(top_key).menus[0].items) == [3, 5]

    out_of_range = json.loads(json.dumps(valid))
    out_of_range["menus"][0]["items"][0]["key"] = 6
    try:
        parse_draft(out_of_range)
    except ValueError as error:
        assert "invalid buttons" in str(error)
    else:
        raise AssertionError("an out-of-range content key was accepted")


def test_an_empty_menu_is_accepted_and_more_than_six_buttons_is_refused() -> None:
    # A menu may be created before it is filled in: six blank keys, no buttons.
    empty = {"actions": [], "bindings": [None] * 6, "dials": [None] * 8, "menus": [{"name": "blank", "title": "Blank", "items": []}]}
    draft = parse_draft(empty)
    assert draft.menus[0].items == ()

    # More than six buttons is still impossible: a menu is one screen of six keys.
    overloaded = {"actions": [{"id": "copy", "kind": "keys", "keys": ["ctrl", "c"]}], "bindings": [None] * 6,
                  "dials": [None] * 8,
                  "menus": [{"name": "big", "title": "Big", "items": [{"key": key, "label": f"Item {key}", "action": "copy"} for key in range(6)] + [{"key": 0, "label": "Seventh", "action": "copy"}]}]}
    try:
        parse_draft(overloaded)
    except ValueError as error:
        assert "at most 6" in str(error)
    else:
        raise AssertionError("a menu with more than six buttons was accepted")


def test_a_status_message_looks_like_one_and_a_success_goes_away() -> None:
    """The icons page's notice was unstyled and permanent.

    Its look was scoped to `.save-bar`, which only the deck page's notice sits inside, so
    on the icons page the same class rendered as plain body text at full size - "it doesn't
    look right" - and nothing ever cleared it, so "Renamed to ..." stayed on screen for the
    rest of the session instead of fading like every other success.

    The page now reports in the page bar's own line, which every other page uses, so the
    fade is written once and a re-render of the page cannot take the words with it.
    """
    assets = Path(__file__).resolve().parent.parent / "akp03" / "webui_assets"
    css = support.stylesheets(assets)
    icons = (assets / "icons.js").read_text(encoding="utf-8")
    app = (assets / "app.js").read_text(encoding="utf-8")

    assert "\n.save-state {" in css, "the status look must not be scoped to the save bar"
    assert ".save-state.tone-error" in css and ".save-state.tone-warn" in css
    assert ".save-bar .save-state { max-width: 30ch; }" in css, "only the bar needs the cap"

    assert "setStatus(tone === 'ok' ? 'saved' : (tone || ''), String(text || ''));" in icons
    assert "iconNotice" not in icons, "the page's own notice is gone: the bar's line is the one"
    assert "2600" in app, "a success message has to leave on its own"


def test_the_icons_page_keeps_its_actions_in_one_place() -> None:
    """Mark asked for the icon section's actions to stop repeating on every picture.

    The tiles are selections now.  What can be done to the picked one is stated once, in the
    bar above the grid, beside the set's own actions - where the old arrangement put a
    Rename and a Delete under every tile, which is 48 links across a set of 24.  The page
    also said "Icons" twice, once in the top bar and once under it.
    """
    assets = Path(__file__).resolve().parent.parent / "akp03" / "webui_assets"
    js = (assets / "icons.js").read_text(encoding="utf-8")
    css = (assets / "icons.css").read_text(encoding="utf-8")

    # A tile is a selection, and carries no actions of its own.
    assert 'data-ic-pick="${iconEsc(key)}"' in js
    assert 'role="button"' in js and "aria-pressed" in js, "a tile answers the keyboard"
    assert "ic-tools" not in js, "the per-tile action row is gone"

    # One bar, above the grid, holding the picked icon's actions and the set's own.
    assert "iconPickedToolsHTML" in js and 'class="ic-bar"' in js
    assert "${bar}\n    ${pack.icons.length" in js, "the bar is drawn even when the set is empty"
    assert ".ic-bar {" in css and ".ic-bar-set {" in css

    # The page says Icons once: the top bar already names it.
    assert "<h1>Icons</h1>" not in js

    # Deleting does not look like renaming.
    assert ".ic-tile.is-picked" in css and ".quiet.is-danger" in css
