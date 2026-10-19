"""Validated six-key custom-menu definitions and visible navigation.

A custom menu is one screen of up to six buttons.  A button either runs an
action or opens another menu; menus may therefore nest as deeply as the person
building them likes, and the same menu can be reached from several places.

The on-disk shape still carries a ``pages`` array because that is what shipped
first, but a menu written by the editor has exactly one page (id ``main``) and
nothing in the interface talks about pages any more.  ``submenu`` on an item is
the name of the menu it opens; a value naming a page of the *same* menu is still
honoured so configurations written before this model still navigate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping

KEY_COUNT: Final = 6
# Every LCD key carries content.  Nothing is reserved for navigation: Back is the
# bottom-left knob press (at the outermost menu it leaves the menu and puts the
# deck's ordinary key labels back), and the top knob turns to move the highlight
# and presses to select, exactly as the older brightness menu behaved.
CONTENT_KEYS: Final = tuple(range(KEY_COUNT))


class CustomMenuError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CustomMenuItem:
    """One content key of a menu.

    ``submenu`` names the menu this key opens, or - for configurations written
    before menus could nest - a page of the same menu.  ``icon`` is an optional
    library reference (``pack:general/copy``): when it is None the key still
    shows its label text, and when it is set the daemon paints that picture
    instead, so a menu can be all pictures with no words on it.
    """
    key: int
    label: str
    action: str | None = None
    submenu: str | None = None
    icon: str | None = None


@dataclass(frozen=True, slots=True)
class CustomMenuPage:
    page_id: str
    title: str
    items: tuple[CustomMenuItem, ...]


@dataclass(frozen=True, slots=True)
class CustomMenuControl:
    """A control a menu takes over while it is open.

    The action is named rather than inlined, for the same reason a menu item's is:
    a menu points at actions, so one action can serve several menus and a menu
    copied to another deck reads the same.  ``mode`` says whether the deck taps the
    action or holds it down until the control is let go.
    """

    action: str
    mode: str = "tap"


# The parts of a dial a menu may take over for as long as it is open: the big dial and
# the right dial, press and both turns of each.  The left dial is not here - its press is
# always the way back out of a menu and its turns are nothing while one is open.  A menu
# that claims no part of the big dial leaves it its ordinary job of moving the highlight.
MENU_DIAL_SLOTS = (
    "top", "top_turn_left", "top_turn_right",
    "bottom_right", "bottom_right_turn_left", "bottom_right_turn_right",
)
# The two dials those slots belong to, as the deck names them.
MENU_DIAL_CONTROLS = ("top", "bottom_right")


@dataclass(frozen=True, slots=True)
class CustomMenu:
    name: str
    root: str
    pages: dict[str, CustomMenuPage]
    # The three buttons under the LCD keys, by control name, for as long as this
    # menu is open.  A button left out of here keeps its ordinary binding.
    controls: dict[str, CustomMenuControl] | None = None
    # The parts of the big dial and the right dial this menu takes over while it is
    # open, by slot name - ``top``, ``top_turn_left`` and so on - each naming an
    # action.  A menu that claims nothing here leaves the big dial its ordinary job of
    # moving the highlight, and leaves the right dial to the deck.
    dials: dict[str, str] | None = None

    @property
    def title(self) -> str:
        return self.pages[self.root].title

    @property
    def items(self) -> tuple[CustomMenuItem, ...]:
        """The buttons on this menu's screen, in key order."""
        return tuple(sorted(self.pages[self.root].items, key=lambda item: item.key))


@dataclass(frozen=True, slots=True)
class CustomMenuState:
    title: str
    labels: tuple[str, str, str, str, str, str]
    page_id: str
    # The item the top knob has highlighted, so the deck can show the selection.
    selected_key: int | None = None
    # Parallel to ``labels``: the library reference each key is to be drawn with,
    # or None to keep its text.  The daemon paints from this, so the menu's own
    # icons never have to be re-derived from the page in a second place.
    icons: tuple[str | None, ...] = (None,) * 6
    # Which menu is on the glass, and how deep it is nested, so a caller can say
    # where the person is without re-deriving the trail.
    menu_name: str = ""
    depth: int = 0


@dataclass(frozen=True, slots=True)
class CustomMenuActivation:
    state: CustomMenuState
    action: str | None = None


class CustomMenuNavigator:
    """Six visible content keys, with highlight, select and Back on the knobs.

    ``menus`` is every custom menu by name and ``name`` is the one being entered.
    A key whose item names another menu pushes that menu; Back pops one level and
    returns ``None`` only when the outermost menu has been left.
    """

    def __init__(self, menus: CustomMenu | Mapping[str, CustomMenu], name: str | None = None) -> None:
        if isinstance(menus, CustomMenu):
            # A single menu, entered directly: the shape callers used before
            # nesting existed.
            self._menus: dict[str, CustomMenu] = {menus.name: menus}
            self._name = menus.name
        else:
            self._menus = dict(menus)
            if name is None or name not in self._menus:
                raise CustomMenuError(f"no custom menu named {name!r}")
            self._name = name
        self._trail: list[tuple[str, str]] = [(self._name, self._menus[self._name].root)]
        self._selected = 0
        self._state = self._render()

    @property
    def state(self) -> CustomMenuState:
        return self._state

    @property
    def menu_name(self) -> str:
        """The menu currently on the glass."""
        return self._trail[-1][0]

    @property
    def depth(self) -> int:
        """0 for the menu that was entered, 1 for its first sub-menu, and so on."""
        return len(self._trail) - 1

    @property
    def controls(self) -> dict[str, CustomMenuControl]:
        """The buttons this menu has taken over while it is open, by control name.

        Empty for a menu that sets none, which leaves every cap doing its ordinary
        deck job - what they all did before a menu could claim one.
        """
        return self._menu.controls or {}

    @property
    def dials(self) -> dict[str, str]:
        """The parts of the dials this menu has taken over while it is open, by slot.

        Empty for a menu that claims none, which leaves the big dial turning to move
        its highlight and pressing to select, and leaves the right dial to the deck.
        """
        return self._menu.dials or {}

    @property
    def _menu(self) -> CustomMenu:
        return self._menus[self._trail[-1][0]]

    @property
    def _page(self) -> CustomMenuPage:
        return self._menu.pages[self._trail[-1][1]]

    @property
    def _ordered(self) -> tuple[CustomMenuItem, ...]:
        """The page's items in key order: what the top knob cycles through."""
        return tuple(sorted(self._page.items, key=lambda item: item.key))

    def press(self, key: int) -> CustomMenuActivation:
        if isinstance(key, bool) or not isinstance(key, int) or not 0 <= key < KEY_COUNT:
            raise CustomMenuError("LCD key must be an integer from 0 through 5")
        for position, item in enumerate(self._ordered):
            if item.key == key:
                self._selected = position
                return self._activate(item)
        # Empty content keys intentionally have no hidden behavior.
        return CustomMenuActivation(self._state)

    def rotate(self, steps: int) -> CustomMenuState:
        """Move the highlight over the page's items; positive is clockwise."""
        if isinstance(steps, bool) or not isinstance(steps, int):
            raise CustomMenuError("steps must be an integer")
        items = self._ordered
        if items:
            self._selected = (self._selected + steps) % len(items)
        return self._refresh()

    def select(self) -> CustomMenuActivation:
        """Activate the highlighted item."""
        items = self._ordered
        if not items:
            return CustomMenuActivation(self._state)
        return self._activate(items[self._selected % len(items)])

    def back(self) -> CustomMenuState | None:
        """Go back one level; at the outermost menu, leave the menu.

        ``None`` means there is nothing above: the deck is at the menu it was
        entered from, so backing out closes the menu and puts the ordinary key
        labels back.  A key that visibly does nothing is a dead end -- the only
        way back to the deck's assigned keys is to leave the menu, so the
        outermost menu has to be able to.
        """
        if len(self._trail) <= 1:
            return None
        self._trail.pop()
        self._selected = 0
        return self._refresh()

    def _activate(self, item: CustomMenuItem) -> CustomMenuActivation:
        if item.submenu is not None:
            self._trail.append(self._resolve(item.submenu))
            self._selected = 0
            return CustomMenuActivation(self._refresh())
        return CustomMenuActivation(self._state, item.action)

    def _resolve(self, target: str) -> tuple[str, str]:
        """Where a key that opens something goes.

        A page of the menu being shown wins, so configurations from the page
        model keep working; otherwise the target must be another menu.
        """
        if target in self._menu.pages:
            return (self._trail[-1][0], target)
        menu = self._menus.get(target)
        if menu is None:
            raise CustomMenuError(f"menu key opens {target!r}, which is not a menu")
        return (target, menu.root)

    def _refresh(self) -> CustomMenuState:
        self._state = self._render()
        return self._state

    def _render(self) -> CustomMenuState:
        labels = [""] * KEY_COUNT
        icons: list[str | None] = [None] * KEY_COUNT
        for item in self._ordered:
            labels[item.key] = item.label
            icons[item.key] = item.icon
        items = self._ordered
        selected_key = items[self._selected % len(items)].key if items else None
        return CustomMenuState(
            self._page.title,
            tuple(labels),  # type: ignore[arg-type]
            self._page.page_id,
            selected_key,
            tuple(icons),
            self._trail[-1][0],
            len(self._trail) - 1,
        )
