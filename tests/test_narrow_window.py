"""A narrow window.

The app was built for a desktop window and defended itself with a 900px floor on the
body.  Nothing below that floor could ever happen: a phone, a split pane or a small
laptop window could only scroll sideways for ever, and the responsive rules the pages
already had - the deck folding its panel under the drawing, the menus folding its three
columns into two - were unreachable, because a media query below 900px can never match.

These tests hold the floor off and hold the folding in place, and they hold the order the
deck's two rules have to be written in: the wide-window pin first, the narrow override
after, or the override is dead the moment the pin exists.
"""

from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "akp03" / "webui_assets"


import support

def read(name):
    return (ASSETS / name).read_text(encoding="utf-8")


def test_the_page_can_be_narrower_than_a_desktop_window():
    app = read("app.css")
    assert "min-width:900px" not in app
    assert "min-width: 900px" not in app
    # The one fixed-width block left in the layout can still give way.
    assert ".hint{width:500px;max-width:100%;" in app


def test_the_shell_folds_rather_than_scrolling_sideways():
    css = support.stylesheets()
    narrow = css.split("@media (max-width: 900px) {")[1]
    # The rail becomes a strip along the top, and the three pages sit in it in a row.
    assert ".rail { flex-direction: row" in narrow
    assert ".rail nav { display: flex" in narrow
    # The shell stops being a fixed-height two-column grid.
    assert ".app { height: auto; grid-template-columns: minmax(0, 1fr); }" in narrow
    # The pages stop being fixed-height, which is what let a stacked pane be clipped.
    assert ".view, .view.active { height: auto; }" in narrow


def test_the_drawings_are_scaled_not_cut():
    css = support.stylesheets()
    # This test used to ask for two blocks of overrides.css that had nothing in them:
    # "@media (max-width: 560px) {" and 470px, each closed again with no rule between the
    # braces.  That is the shell of a retired view's narrow rules, and its presence was the
    # whole evidence - a wrapper with no rules scales nothing.
    assert "@media (max-width: 560px) {" not in css
    assert "@media (max-width: 470px) {" not in css
    # The page-level narrow step that is still in use is still here, the shell folds at
    # 900px (see test_the_shell_folds_rather_than_scrolling_sideways), and the drawing - the
    # one component both pages mount - carries its own scale.
    assert "@media (max-width: 410px) {" in css
    # The drawing's own sheet carries narrow-window steps, and the body's scale is one of
    # them.  This used to count those steps - three of them - as the evidence that the
    # drawing is not simply cut off.  The count was a proxy: the deck page's own tiles and
    # its narrow fold now live in this sheet too, so counting says nothing.  What matters
    # is that a step exists here and that it is a scale rather than a clip.
    drawing = read("deckdraw.css")
    assert "@media (max-width" in drawing
    assert ".deck-body { zoom:" in drawing


def test_the_decks_two_rules_are_in_the_order_that_works():
    # Both of the deck's rules are in deckdraw.css now - the sheet that draws the deck
    # rather than the one that happened to win - so the question is asked of the sheets the
    # page loads, in the order it loads them: the wide-window pin first, the narrow override
    # after, or the override is dead code the moment the pin exists.
    css = support.stylesheets()
    pin = css.index("#deck.active {")
    narrow = css.index("@media (max-width: 1170px) {")
    assert pin < narrow, (
        "the wide-window pin has to be written before the narrow override: same selector, "
        "same weight, so whichever comes last wins and the other is dead code"
    )
    # And the narrow one really does fold the panel under the drawing.
    folded = css[narrow:narrow + 520]
    assert "#deck.active { grid-template-columns: minmax(0, 1fr);" in folded
    assert "#deck .deck-inspector { border-left: 0; border-top: 1px solid var(--line);" in folded


def test_the_menus_page_still_folds_its_three_columns():
    css = read("flow.css")
    block = css.split("@media (max-width: 900px) {")[1].split("}")[0]
    assert "#fxMenusRoot { grid-template-columns: 170px minmax(0, 1fr);" in block
    assert ".fx-insp { grid-column: 2; grid-row: 2;" in css
