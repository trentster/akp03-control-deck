"""The button panel: one component, wherever a button is edited.

The panel used to open straight onto the editors - a chooser row, a shortcut
field and a picture list - with the row labelled "What it does", which described
nothing: the value under it is only ever a *name* the user chose, so a label like
"Eat candy floss" made the sentence nonsense.  It now opens at rest on what the
key actually has (the kind of thing assigned, its label, its shortcut, its
picture) and only becomes the editors when Edit is pressed.

It is also *one* panel.  The deck's key panel and the Menus button panel were two
pieces of markup for one job, and they drifted every time either was touched - the
card was made one component first, then the rows, and the Menus panel still built
its own copy of everything around them: "I want the menus edit and the deck edit
to be identical... use the exact same component".  Now keypanel.js owns the card,
the rows, the two states, the choosers and the tools strip, and a *target* supplies
only what the panel answers for, what it reads, and where a change lands.

These tests hold that shape: the resting view, the two headings that name the kind
honestly, the shortcut row that exists only when the assignment sends keys, the
tools that change with the kind instead of pretending a menu is an action, and the
fact that neither page draws any of it for itself.
"""
from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "akp03" / "webui_assets"


import support

def asset(name: str) -> str:
    return (ASSETS / name).read_text()


def test_the_panel_opens_at_rest_and_only_edits_when_asked() -> None:
    """Two states, one panel: the summary at rest, the editors behind Edit."""
    panel = asset("keypanel.js")
    css = asset("keypanel.css")

    # The skeleton the component builds: the summary row, and the editors hidden.
    assert 'data-panel-row="summary"' in panel
    assert 'data-panel="editors" hidden' in panel
    assert "data-panel-edit>Edit…</button>" in panel
    # Done is the way out of the editor rows, and it is there because Edit… lives in the
    # at-rest summary while the summary is hidden when the editors are showing: without it
    # the only way back was to select another control and return to this one, which is the
    # report that put it there.  (This pin used to say the opposite, on the reasoning that
    # Edit again and selecting elsewhere already closed it - the report showed they did not.)
    assert 'data-tool="done">Done</button>' in panel
    assert "doneEditing" not in panel

    # One piece of state owns the swap, so the two views can never both show or both
    # hide, and it belongs to the mount and to the target it is answering for.
    assert "function panelState(root)" in panel
    assert "editing: false" in panel and "open: null" in panel
    assert "paintSummaryRow(root, view, target);" in panel
    assert "if (state.editing) paintEditorRows(root, view, target, state);" in panel
    # Moving to another button starts over at rest: the mode belongs to one button,
    # and the markup is never rebuilt, so an open editor keeps its focus.
    assert "if (state.token !== target.token) {" in panel
    assert "state.token = target.token;" in panel
    assert "state.editing = false;" in panel

    # The editors live in a plain wrapper: without its own gap the cards sit flush
    # against each other, and without its own [hidden] rule a display:grid beats
    # the browser's own hiding.
    assert '.key-panel [data-panel="editors"] { display: grid; gap: 12px; }' in css
    assert '.key-panel [data-panel="editors"][hidden] { display: none; }' in css


def test_each_value_is_named_for_what_it_is() -> None:
    """Label, shortcut, picture - because the value is only ever a name."""
    panel = asset("keypanel.js")
    for name in ("Label", "Shortcut"):
        assert f"<dt>{name}</dt>" in panel, name
    # The picture line is the one a control with no screen goes without, so its name sits
    # on the row rather than in the row: a button under the keys reads out its label and
    # its shortcut, and nothing else.
    assert '<dt data-panel="summary-icon-label">Icon</dt>' in panel
    # The heading names the kind of thing assigned.  An empty key is called
    # neither an action nor a menu, because it is neither.
    assert "heading: isMenu ? 'Menu' : (assigned ? 'Action' : 'Not assigned')," in panel
    # At rest the value is the name the button carries, never a description of what
    # it might do with it.  A *deck key* that opens a menu is named by that menu; a
    # menu key and a menu button are named by the words they were given.
    assert "const assignedName = isMenu && menuName && job.namedByMenu" in panel
    assert "label: assigned ? assignedName : 'Nothing yet'," in panel
    # A menu is an action of kind 'menu' on a deck key and a `__menu__:` value on a
    # menu key or button: both have to be recognised, or a menu reads as an action
    # and gets offered a Test button it cannot honour.
    assert "const prefixed = Boolean(job.value && job.value.startsWith(MENU_PREFIX));" in panel
    assert "const isMenu = spec ? spec.kind === 'menu' : prefixed;" in panel
    assert "actionId: isMenu ? null : (job.actionId || null)," in panel
    # And the deck's own reading sets that flag for the one case it is true of.
    assert "noun: 'key', namedByMenu: true" in panel
    # The chooser marks the row the button is already on, which needs the button's raw
    # value to travel with the reading: the list opened with nothing lit, and the
    # "in use" tag never appeared, because view.value was undefined on every mount.
    assert "value: job.value || null," in panel
    assert "entry.value === view.value" in panel


def test_a_shortcut_only_appears_when_the_assignment_sends_keys() -> None:
    """A menu sends no keys - and neither does a brightness or HTTP action."""
    panel = asset("keypanel.js")
    assert "shortcut: keys ? shortcut(keys) : ''," in panel
    assert "shortcutNote: keys ? '' : (isMenu" in panel
    # The empty cell is explained rather than left blank, or it reads as a fault.
    assert "'Opening a menu sends no keys'" in panel
    assert "'This one sends no keys'" in panel
    assert "'Nothing to send yet'" in panel
    # The shortcut card follows the same rule, so the two can never disagree.  It is a
    # row with its own painter, so a reader looking for the rule is sent to one place.
    assert "function paintShortcutRow(root, view) {" in panel
    assert "if (row) row.hidden = !view.shortcut;" in panel


def test_the_chooser_row_does_not_repeat_the_shortcut() -> None:
    """The shortcut is in the card above and in its own card: not twice more."""
    panel = asset("keypanel.js")
    assert "const repeatsShortcut = Boolean(view.shortcut && view.meta);" in panel
    assert "? (repeatsShortcut ? '' : view.meta)" in panel


def test_an_action_nothing_wears_can_be_removed_from_the_list_it_sits_in() -> None:
    """An action that stopped being used had no way out of the deck file.

    The only Delete is on a control's panel, and it acts on the action that control is
    wearing - so once nothing wore it there was nothing left to press: it stayed in the
    file, listed under "From your deck file", for good.  The row that can go now carries
    its own Delete, and the reading behind it counts every control a draft can wear, so a
    row still in use never offers one.
    """
    script = asset("keypanel.js")
    assert 'data-panel-remove=' in script, "a row nothing wears has no way out"
    assert "row-remove" in script and ".key-panel .row-remove" in asset("keypanel.css")
    # The reading that decides whether a row can go, and the guard that refuses it, are one
    # reading - and it counts every control the draft can wear, not only the six keys.
    uses = script.split("function actionUses(actionId)", 1)[1].split("\nasync function", 1)[0]
    for control in ("bindingKeys(actionId)", "state.buttons", "state.dials", "fxMenus()"):
        assert control in uses, f"the reading that decides this misses {control}"
    # Only the rows that can go get the button, and pressing it removes rather than picks:
    # a Delete on a choice row must not also choose the action for the control.
    assert "if (!entry.removable) return choice;" in script
    assert 'data-panel-remove="${esc(entry.value)}"' in script
    assert "button.onclick = () => removeAction(button.dataset.panelRemove);" in script


def test_the_tools_change_with_the_kind_of_assignment() -> None:
    """An action can be tested, renamed, deleted; a menu is designed elsewhere."""
    panel = asset("keypanel.js")
    # Test and Rename belong to an action only.
    # The strip is one shared function now, so the rules are asserted where the
    # buttons are made: test and rename follow the action, delete follows ownership.
    assert "test.hidden = !view.actionId;" in panel
    # A dial is three actions rather than one, so the verbs that act on a single action
    # step aside for it - and they step aside where they are decided, because the top of
    # the function would only be undone by these very lines.
    assert "rename.hidden = !view.actionId || dial;" in panel
    assert "del.hidden = dial ? true : (clears ? false : !(view.actionId && view.mine));" in panel
    # A menu button's Delete clears the button; the action it ran stays in the deck, because
    # an action is shared and removing it is a different job with its own guard.
    assert "const clears = !!target && typeof target.clearAssignment === 'function';" in panel
    assert "del.onclick = () => (clears ? target.clearAssignment() : removeAction(view.actionId));" in panel
    # A menu offers the only honest route: where its buttons are designed.
    assert 'data-tool="menu"' in panel
    assert "menu.hidden = !view.menuName;" in panel
    assert "showView('menus');" in panel
    # Nothing on the panel is a silent dead end.
    assert "menu.title = 'Opens the Menus page on this menu, where its buttons are designed.';" in panel


def test_the_key_card_reads_the_key_and_the_panel_changes_it() -> None:
    """The card states the key; the panel is where it is edited.

    The name and shortcut were once printed in both places, so they were hidden on
    the card.  The card is the place a key is read at a glance - number, name and
    shortcut together - while every edit happens in the panel below, so all three
    are shown again.  The shrink guard below matters either way.
    """
    css = support.stylesheets()
    # Nothing on the card is hidden any more: neither the name nor the shortcut.
    assert "#deck .selected-control #keyTitle," not in css
    assert "#deck .selected-control #keyDetail { display: none; }" not in css
    # The card is flex-shrunk to a 24px sliver in a short window without this,
    # which clipped its own contents rather than letting the column scroll.
    assert "#deck .deck-inspector > * { flex-shrink: 0; }" in css


def test_both_pages_describe_a_button_the_same_way() -> None:
    """One reading of a button, so the deck and the Menus page cannot disagree."""
    panel = asset("keypanel.js")
    flow = asset("flow.js")
    # The heading and the labels come from the one view function...
    assert "function panelView(job)" in panel
    assert "function paintEditorRows(root, view, target, state)" in panel
    # ...and the Menus page no longer has a reading, or any of the words, of its own.
    for gone in ("const kind = !item", "fxNoShortcutWords", "fxShortcut(", "fxWords("):
        assert gone not in flow, gone
    assert "view: () => panelView(job())" in flow
    # The labels are the component's own, in one place.
    # Less is better: the Label row already names the control, and the card above it names
    # it again, so the summary carries no heading of its own.
    assert "summary-heading" not in panel
    assert 'class="row-label">Shortcut</p>' in panel
    assert 'class="row-label">Icon</p>' in panel
    # Both pages offer the same verbs because both draw them from one function.
    strip = panel.split("function actionToolsHTML", 1)[1].split("function renameFormHTML", 1)[0]
    assert ">Done</button>" in strip, "the editor rows have lost their way out"
    assert 'data-tool="cancel">Cancel</button>' in strip, "an experiment can no longer be put back"
    assert ">Rename…</button>" in strip
    assert ">Delete</button>" in strip, "Delete is one word, not a sentence"
    assert ">Test it now</button>" not in strip, "Test belongs to the at-rest view, not here"
    assert "Delete this action" not in strip
    assert "actionToolsHTML() + renameFormHTML()" in panel
    # The words that described nothing are gone from every asset, not just the one.
    for name in ("index.html", "keypanel.js", "flow.js", "keypanel.css", "flow.css"):
        text = asset(name)
        for gone in ("What it does", "What it looks like", "Its shortcut", "Try it now"):
            assert gone not in text, f"{name}: {gone}"


def test_the_menus_panel_has_the_same_two_states() -> None:
    """A menu button is edited by the deck's own panel, not by a copy of it."""
    panel = asset("keypanel.js")
    flow = asset("flow.js")
    # The Menus page mounts the same component and tells it which button to answer
    # for; it draws no panel of its own.
    # The Menus page mounts one target for whichever control its panel is answering for:
    # a tile, a button under the keys, or one part of one dial.
    assert "mountButtonPanel(root, fxSlotKey !== null && fxCapKey === null && fxDialControl === null" in flow
    assert "menusCapTarget(fxMenuName, fxCapKey)" in flow
    assert "menusDialTarget(fxMenuName, fxDialControl)" in flow
    assert "mountButtonPanel($('#fxButtonPanel'), menusButtonTarget(menuName, key));" in flow
    for drawn_for_itself in ('class="panel-row"', 'class="row-value', "<dl class=\"summary-list\">",
                             "actionToolsHTML()", "renameFormHTML()", "controlCardHTML(",
                             'class="shortcut-editor"'):
        assert drawn_for_itself not in flow, f"the Menus page draws its own {drawn_for_itself}"
    # One place decides what a button has: the panel's own reading, fed by the target.
    assert "function menusButtonTarget(menuName, key)" in flow
    assert "card: () => controlCardFor('menu-button', key, menuName, item())," in flow
    assert "clearable: true," in flow
    # The Edit row is the way in AND the way out, and it is one control for both
    # pages - the shape that answered "how do I go back out from edit mode".
    assert "state.editing = !state.editing;" in panel
    # Choosing another button starts that panel over, without touching any other
    # mount's editors, because the state is keyed by the mount and the target.
    assert "if (state.token !== target.token) {" in panel
    assert "panelMounts" in panel
    # The summary and the heading row are shared CSS, so the pages cannot drift.
    css = asset("flow.css")
    assert ".summary-list dd.summary-note" in css
    assert ".summary-list dt,\n.key-panel .row-label {" in css
    # .fx-thumb went with the Menus panel's own markup: the component's own
    # .row-thumb is the one thumbnail rule now.
    assert ".key-panel .row-thumb { display: inline-flex;" in asset("keypanel.css")
    # The summary's dd is display: flex, which would beat the browser's [hidden] - so the
    # hidden rule must be guarded, or an empty cell slides the rows after it a column across.
    assert ".summary-list dd[hidden]" in asset("flow.css")


def test_the_two_panels_cannot_both_show_at_once() -> None:
    """`hidden` must beat the panel's own `display`, or both panels render.

    The chain hides the deck's panel while a menu button takes over the column.
    `.key-panel { display: grid }` outranked the browser's `[hidden] { display: none }`,
    so the key's own summary - its picture included - stayed on screen above the
    button's panel, and read as if it belonged to whichever button you clicked.
    """
    css = asset("keypanel.css")
    assert ".key-panel[hidden] {" in css
    # Every mount carries that class, so one rule covers the deck's column, the
    # chain's, and the Menus page's inspector.
    panel = asset("keypanel.js")
    assert 'root.dataset.panelRoot = \'\';' in panel
    assert "root.hidden = true;" in panel
    flow = asset("flow.js")
    assert "if (deckPanel) deckPanel.hidden = true;" in flow
    assert "unmountButtonPanel($('#fxButtonPanel'));" in flow
    # ...and the deck's panel only shows when the chain has not taken the column.
    assert "if (chain && !chain.hidden) { root.hidden = true; return; }" in panel


def test_the_shortcut_row_reports_before_it_edits() -> None:
    """Shortcut behaves like Action and Icon: one line, then a Change button.

    The chip bundle used to be permanently open, so the panel showed every key on
    the keyboard at once with only the current one highlighted - "a wild bundle",
    in the report.  Now the row states the shortcut the button has and opens the
    editor only when asked, and only one row is ever open.
    """
    panel = asset("keypanel.js")
    css = asset("keypanel.css")
    # A value row with its own opener, and a collapsed editor.
    assert 'data-panel-open="shortcut"' in panel
    assert 'data-panel="shortcut-editor" hidden' in panel
    # Opening it is one row at a time, decided by the panel's own state.
    # A dial's three rows share one list, so the row that opened it is part of what
    # is open: pressing the same row again closes the list.
    assert "const same = state.open === wanted && state.openSlot === slot;" in panel
    assert "state.open = same ? null : wanted;" in panel
    assert "const open = state.open === name;" in panel
    # The editor is filled from the same reading that fills the row above it.
    assert "fill('shortcut', 'shortcut-editor', box => renderShortcutEditor(" in panel
    assert "view.actionId, view.keys, box, target.askedBy()));" in panel
    # And display:flex must not win over the hidden attribute.
    assert ".key-panel .shortcut-editor[hidden] { display: none; }" in css


def test_the_action_buttons_sit_below_the_icon_card() -> None:
    """Done / Rename / Delete act on the panel's view and the action, under all three cards.

    Inside the Action card its own list opening pushed them around, and they are
    not the Icon card's business either - they are their own strip under all three
    cards.  The rename field travels with the Rename button, because otherwise
    clicking it unfolds a field up in a card the button has left.
    """
    panel = asset("keypanel.js")
    skeleton = panel.split("function panelSkeletonHTML()", 1)[1].split("`;", 1)[0]
    order = [skeleton.index(marker) for marker in ('data-panel-row="action"',
                                                   'data-panel-row="shortcut"',
                                                   'data-panel-row="icon"',
                                                   'data-panel="tools"')]
    assert order == sorted(order), "the tools are no longer below the three cards"
    assert skeleton.index('data-panel="icon-choices"') < skeleton.index('data-panel="tools"'), \
        "the tools sit inside the icon card"
    # The strip is the deck's own tools plus the rename form that travels with it.
    assert "box.innerHTML = actionToolsHTML() + renameFormHTML();" in panel


def test_the_icon_row_shows_the_picture_without_repeating_its_name() -> None:
    """With a picture assigned the row is the picture and Change..., nothing else.

    The name beside the thumbnail repeated what the picture already showed, and for
    a drawn icon it named it in words the picture does not use.  The words still
    appear when there is no picture to show, and the name moves onto the button as
    an aria-label so the row is still announced by name.
    """
    panel = asset("keypanel.js")
    # The row is painted by a function of its own, so the rule is one place to read.
    assert "function paintIconRow(root, view, target) {" in panel
    assert "paintIconRow(root, view, target);" in panel
    assert "words.hidden = !!view.icon;" in panel
    assert ".key-panel .row-value b[hidden] { display: none; }" in asset("keypanel.css")


def test_both_panels_use_the_same_row_markup() -> None:
    """One panel definition, three mounts, no page drawing its own copy.

    The Menus button panel was a hand-written copy of the deck key panel, so every
    change to one had to be made again in the other and the two drifted - "I don't
    know why you don't just reuse the same component."  The card was made one
    component, then the rows, and finally the panel itself: the markup is built in
    one place and both pages mount it.
    """
    panel, flow, page = asset("keypanel.js"), asset("flow.js"), asset("index.html")
    # The rows exist once, in the component's skeleton: the summary row a button is
    # read on, the three value rows its editors hang under, and the row a dial's
    # three slots live in.
    assert panel.count('class="panel-row"') == 6
    # One per value row, plus the template the dial's own rows are built from.
    assert panel.count('class="row-value') == 4
    assert "function panelSkeletonHTML()" in panel
    # No page builds a row, a summary or a card for itself any more.
    assert 'class="panel-row"' not in flow
    assert 'class="panel-row"' not in page


    assert "controlCardHTML(" not in flow and "controlCardHTML(" not in page
    assert 'id="keyPanel"' not in page and 'id="selectedControl"' not in page
    # Each page holds a place for a mount and nothing more.
    assert '<div id="deckPanel" class="key-panel"></div>' in page
    assert '<div id="fxButtonPanel" class="key-panel" hidden></div>' in page
    assert "<div class=\"key-panel\" data-fx-menupanel></div>" in flow
    # A mount is wired once and paints in place: the skeleton is never rebuilt.
    assert "root.innerHTML = panelSkeletonHTML();" in panel
    assert panel.count("panelSkeletonHTML()") == 2, "the skeleton is rebuilt somewhere else"


def test_the_three_caps_are_chosen_on_the_deck_and_read_by_the_same_panel() -> None:
    panel, app, page, css = asset("keypanel.js"), asset("app.js"), asset("index.html"), support.stylesheets()

    # Deck order, once: the firmware's own names are not in it.
    assert "const CAP_CONTROLS = ['button6', 'button8', 'button7'];" in panel
    drawing = asset("deckdraw.js")
    for control in ("button6", "button8", "button7"):
        assert f"control: '{control}'" in drawing, control
    assert 'data-cap="${index}"' in drawing, "each cap is named for the click that picks it"
    # A cap is a real button, drawn as the matte domed button it is.
    assert ".button-cap {" in asset("deckdraw.css") and "cursor: pointer;" in asset("deckdraw.css")
    assert "pointer-events: none;" in css

    # Choosing one hands the panel its own target, and only ever one control.
    assert "let selectedCap = null;" in app
    assert "if (targetIsCap()) return capJob();" in panel
    assert "if (targetIsCap()) return `cap:${capControl()}`;" in panel
    assert "selectedCap = null;" in app


def test_the_response_row_is_the_only_place_a_hold_is_set() -> None:
    panel = asset("keypanel.js")

    assert 'data-panel-row="mode"' in panel
    assert 'data-panel="mode"' in panel
    # Response is a flags line, not a card: no heading band, no hint row, and its own
    # guard so the `display: flex` that makes it a line cannot beat [hidden].
    mode_row = panel.split('data-panel-row="mode"', 1)[1].split("</div>", 1)[0]
    assert "row-label" not in mode_row, "the response line has grown a heading band again"
    assert "mode-hint" not in panel, "the explainer is a tooltip now, not a second line"
    assert '[data-panel-row="mode"][hidden]' in asset("keypanel.css")
    # And it keeps the panel's own card inset: less than that and the checkbox sits on the
    # card's edge with the words on its floor.
    mode_rule = asset("keypanel.css").split('[data-panel-row="mode"] {', 1)[1].split("}", 1)[0]
    assert "padding: 13px 14px;" in mode_rule
    # The row is painted by a function of its own, so a reader looking for what the
    # response row does is sent to one place rather than into the whole painter.
    assert "function paintModeRow(root, view, target) {" in panel
    assert "paintModeRow(root, view, target);" in panel
    assert "box.checked = view.held;" in panel
    # Offered only where the control can be held and the panel's target can write it:
    # a menu's own buttons cannot be held, so they are never asked.
    assert "if (row) row.hidden = !(view.canHold && typeof target.setMode === 'function');" in panel
    assert "if (target.setMode) target.setMode(box.checked);" in panel
    # The deck's own two writes: the hold is kept on the assignment.
    assert "setMode: async hold => {" in panel
    assert "if (hold) state.held = [...(state.held || []), control];" in panel
    assert 'if (row) row.hidden = !view.screen;' in panel
    assert "canHold: Boolean(keys && keys.length)," in panel
    assert "No screen on this button" in panel

    # The Menus page draws the same three buttons under the tiles, and the panel answers
    # for whichever one is chosen.  A claim lives on the menu, so a fresh one taps and the
    # Response row is how it is held instead.
    flow = asset("flow.js")
    assert "function fxMountDeck(menu)" in flow and "mountDeckDrawing(mount, fxDeckView(menu))" in flow
    assert 'data-cap="${index}"' in asset("deckdraw.js"), "the shared drawing is the one that draws them"
    assert "function menusCapTarget(menuName, index)" in flow
    assert "fxSetClaim(menuName, index, {action: actionId, mode: 'tap'})" in flow
    assert "fxSetClaim(menuName, index, {action: claim.action, mode: hold ? 'hold' : 'tap'})" in flow
    # One control at a time: choosing a button under the keys lets go of the tile.
    assert "fxCapKey = +cap.dataset.cap; fxSlotKey = null;" in flow
    assert "fxSlotKey = +face.dataset.key; fxCapKey = null;" in flow
