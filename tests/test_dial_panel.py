"""Choosing a dial on the deck page.

A dial is the one control whose panel answers for three slots at once, so it is also
the one control whose panel shows three rows.  The panel itself learns nothing new: it
asks a target for its slots and paints a row for each, and the row that opened the
chooser is the slot the choice is written to.
"""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "akp03" / "webui_assets"


import support

def asset(name: str) -> str:
    return (ASSETS / name).read_text()


def test_the_panel_offers_a_row_for_each_of_a_dials_slots() -> None:
    panel = asset("keypanel.js")

    # The vocabulary is the deck's own: the names a binding is filed under, so what the
    # page writes is what the daemon looks up.  The left dial's press is missing on
    # purpose - it is always the way back out of a menu - and the panel says so instead
    # of offering a row that would be overruled.
    assert "const DIAL_ORDER = ['top', 'bottom_left', 'bottom_right'];" in panel
    assert "const DIAL_FIXED = {" in panel
    assert "const DIAL_SLOTS = DIAL_ORDER.flatMap(dial => dialSettableKinds(dial).map(kind => (kind === 'press' ? dial : `${dial}_${kind}`)));" in panel
    assert "bottom_left: {press: {value: 'Back one level', note: 'Always, while a menu is open.'}}," in panel
    # And the panel draws that one as a statement in its place in the list.
    assert "if (slot.fixed) continue;" in panel
    assert "class=\"panel-row dial-slot is-fixed\"" in panel
    # Three rows, built from the target's own reading rather than a second list here.
    assert "function paintDialRows(root, target, state)" in panel
    assert "function dialSlots()" in panel
    assert 'data-panel-slot="${esc(slot.slot)}"' in panel
    # One chooser serves both shapes of panel, and the row that opened it is the slot.
    assert "function actionChoiceBox(root)" in panel
    assert "function pickDialSlot(slot, value, label)" in panel
    assert "target.setAction(button.dataset.value, button.dataset.label, openState ? openState.openSlot : null, button.dataset.icon || '');" in panel
    # A choice carries its picture with it, so picking "Undo" also wears Undo's icon
    # instead of leaving the row blank until someone hunts the same icon down by hand.
    assert 'data-icon="${esc(reference)}"' in panel
    assert "if (icon) await pickIcon(icon, true);" in panel
    # A dial has no single job to summarise, so its rows are showing as soon as the panel
    # lands on it rather than waiting behind an Edit… button.  It stays out of the editor
    # once Done or Cancel has been pressed, which is why the flag is read here and set by
    # both of those - otherwise the next paint walked straight back in.
    assert "if (typeof target.slots === 'function' && !state.dialLeft) {" in panel
    assert "if (targetIsDial()) return dialPanelTarget();" in panel
    # The single-job rows step aside for a dial's rows, so nothing offers a response
    # switch for a turn that is over the moment it happens.
    assert "for (const name of ['action', 'shortcut', 'mode', 'icon'])" in panel


def test_the_deck_page_selects_a_dial_the_same_way_it_selects_a_cap() -> None:
    app = asset("app.js")
    page = asset("index.html")
    css = support.stylesheets()

    # The dials are real buttons now, named where they sit, exactly like the caps.
    # The dials are drawn by the shared drawing now, so that is where they are named.
    drawing = asset("deckdraw.js")
    assert "{ control: 'top', big: true, short: 'Big' }," in drawing
    assert "{ control: 'bottom_left', big: false, short: 'Left' }," in drawing
    assert "{ control: 'bottom_right', big: false, short: 'Right' }," in drawing
    assert 'data-control="${entry.control}" data-dial="${index}"' in drawing
    assert '<span class="dial' not in drawing

    # One selection at a time: choosing a key or a cap lets the dial go, and the other
    # way round, because the panel answers for exactly one control.
    assert "let selectedDial = null;" in app
    assert "const chosen = selectedCap === null && selectedDial === null && index === selectedKey;" in app
    assert "document.querySelectorAll('#deckDevice .dial').forEach(dial => {" in app

    # Nothing overlays the dials, so they take the pointer themselves, and the chosen one
    # is drawn as chosen.
    assert ".dial {" in asset("deckdraw.css") and "cursor: pointer;" in asset("deckdraw.css")
    assert ".dial.selected {" in asset("deckdraw.css")


def test_a_dial_panel_has_a_way_out_and_a_way_to_take_it_back() -> None:
    panel = asset("keypanel.js")

    # A dial opens straight into the editor view rather than waiting behind an Edit…
    # button, so for a long time neither Done nor Cancel was drawn for it: the only way
    # out was to select a different control and come back to this one.  Mark hit exactly
    # that, the same way he hit it for the buttons.  Both are the dial's now.
    assert "if (done) done.hidden = false;" in panel
    assert "if (cancel) cancel.hidden = false;" in panel
    assert "if (done) done.hidden = dial;" not in panel
    assert "if (cancel) cancel.hidden = dial;" not in panel

    # A dial never presses Edit…, so it takes here the snapshot a button takes when Edit…
    # is pressed.  Without it Cancel would have nothing to return to and would quietly do
    # nothing at all, which is worse than not offering it.
    assert "if (!state.editing) state.snapshot = snapshotDraft();" in panel

    # And leaving has to mean leaving.  Nothing else remembers that the dial left, so the
    # next paint put it straight back into the editor it had just left and Done was
    # decoration; Edit… in the summary is the way back in.
    assert "if (typeof target.slots === 'function' && !state.dialLeft) {" in panel
    assert "state.dialLeft = true;" in panel
    assert "state.dialLeft = false;" in panel

    # And the strip those two buttons live in has to be built for a dial.  It was not:
    # paintEditorRows returned as soon as it had drawn a dial's own rows, so Done and
    # Cancel sat in the markup and never on the screen - the strings were present and the
    # buttons were unreachable, which is exactly the trap a source-reading test walks into.
    assert "    paintDialRows(root, target, state);\n" in panel
    assert "    paintToolStrip(root, view, target);\n    return;" in panel
    # A dial is three actions, not one, so the verbs that act on a single action step
    # aside - from where those verbs are decided, not from the top of the function, or the
    # lines further down simply set them visible again.
    # `dial` has to be a name in that function for those two lines to mean anything: it
    # was deleted from the top of paintActionTools by an edit that only meant to stop the
    # hides, and every paint since threw a ReferenceError halfway through - which no
    # source-reading test here noticed, and the strip showing Done and Cancel while the
    # verbs beside them never updated is what gave it away on the page.
    assert "const dial = !!target && typeof target.slots === 'function';" in panel
    assert "rename.hidden = !view.actionId || dial;" in panel
    assert "del.hidden = dial ? true : (clears ? false : !(view.actionId && view.mine));" in panel
    # Delete means whatever the panel is answering for: a menu button or a cap is cleared,
    # a deck key's action is removed from the deck - and only while nothing else uses it.
    assert "const clears = !!target && typeof target.clearAssignment === 'function';" in panel
    assert "del.onclick = () => (clears ? target.clearAssignment() : removeAction(view.actionId));" in panel


def test_a_control_with_no_screen_does_not_read_out_a_picture_line() -> None:
    panel = asset("keypanel.js")

    # A button under the keys has no screen, so the summary's picture line could only ever
    # say "No screen on this button" - a line that exists to state that a thing cannot be
    # set, which the reader has to read and then throw away.  Mark asked for the label and
    # the shortcut, and those two are the whole truth about a control with no screen.
    assert 'data-panel="summary-icon-label"' in panel
    assert 'data-panel="summary-icon-cell"' in panel
    assert "if (iconLabel) iconLabel.hidden = view.screen === false;" in panel
    assert "if (iconCell) iconCell.hidden = view.screen === false;" in panel
    # No screen means no picture, so nothing hidden there is a picture the reader lost.
    assert "view.screen ? view.iconWords : 'No screen on this button'" in panel


def test_the_action_list_shows_where_it_goes_on() -> None:
    css = asset("keypanel.css")

    # macOS hides its overlay scrollbar until you scroll, so a list longer than its window
    # looks like a list that ended.  Styling the scrollbar at all keeps it on screen.
    assert ".key-panel .row-choices::-webkit-scrollbar { width: 10px; }" in css
    assert ".key-panel .row-choices::-webkit-scrollbar-thumb {" in css
    # A rule between sections, and a heading that stays at the top of the list as its rows
    # scroll past, so the section you are reading is always named.
    assert ".key-panel .row-choices .row-list + .row-heading {" in css
    assert "border-top: 1px solid var(--line);" in css
    assert ".key-panel .row-choices .row-heading {" in css
    assert "position: sticky; top: -8px; z-index: 1;" in css
