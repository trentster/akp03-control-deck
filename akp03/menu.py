"""Portable, UI-independent menu navigation for the AKP03."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

MAX_ITEMS: Final = 6


class MenuValidationError(ValueError):
    """Raised when a menu definition or navigation input is invalid."""


def _require_text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise MenuValidationError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise MenuValidationError(f"{name} must be non-empty")
    return value


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One selectable item that either emits an action or enters a submenu."""

    label: str
    action: str | None = None
    submenu: MenuPage | None = None

    def __post_init__(self) -> None:
        _require_text(self.label, "label")
        if self.action is not None:
            _require_text(self.action, "action")
        if self.submenu is not None and not isinstance(self.submenu, MenuPage):
            raise MenuValidationError("submenu must be a MenuPage")
        if (self.action is None) == (self.submenu is None):
            raise MenuValidationError("an item must define exactly one of action or submenu")


@dataclass(frozen=True, slots=True)
class MenuPage:
    """An immutable hierarchy page containing one through six menu items."""

    items: tuple[MenuItem, ...]
    title: str = ""

    def __post_init__(self) -> None:
        _require_text(self.title, "title", allow_empty=True)
        try:
            items = tuple(self.items)
        except TypeError as error:
            raise MenuValidationError("items must be an iterable of MenuItem values") from error
        if not 1 <= len(items) <= MAX_ITEMS:
            raise MenuValidationError("a page must contain one to six items (at most six)")
        if not all(isinstance(item, MenuItem) for item in items):
            raise MenuValidationError("items must contain only MenuItem values")
        object.__setattr__(self, "items", items)


@dataclass(frozen=True, slots=True)
class MenuState:
    """Immutable state suitable for rendering on the six LCD keys."""

    title: str
    labels: tuple[str, str, str, str, str, str]
    selected_index: int


@dataclass(frozen=True, slots=True)
class Activation:
    """Result of pressing an item; ``action`` is set only for leaf items."""

    state: MenuState
    action: str | None = None


class Menu:
    """Stateful navigator over an immutable :class:`MenuPage` hierarchy.

    The class has no device, GUI, daemon, or configuration dependencies.
    """

    def __init__(self, root: MenuPage, dials: dict[str, str] | None = None) -> None:
        if not isinstance(root, MenuPage):
            raise MenuValidationError("root must be a MenuPage")
        self._pages = [root]
        self._selected_index = 0
        self._state = self._render_state()
        # The parts of the dials this menu has taken over while it is open, by slot:
        # ``top``, ``top_turn_left``, ``bottom_right_turn_right`` and so on.  Whatever a
        # menu names runs that action instead of the menu's own navigation, so a menu can
        # leave the volume on the big knob and select with the keys.  Empty means the
        # dials do what they have always done here.
        self.dials: dict[str, str] = dict(dials or {})

    @property
    def state(self) -> MenuState:
        """Current immutable six-key render state."""
        return self._state

    def rotate(self, steps: int) -> MenuState:
        """Move the top-knob selection; positive is clockwise and wraps."""
        if isinstance(steps, bool) or not isinstance(steps, int):
            raise MenuValidationError("steps must be an integer")
        self._selected_index = (self._selected_index + steps) % len(self._page.items)
        return self._refresh()

    def top_clockwise(self) -> MenuState:
        return self.rotate(1)

    def top_counterclockwise(self) -> MenuState:
        return self.rotate(-1)

    def top_press(self) -> Activation:
        """Activate the currently selected item."""
        return self._activate(self._selected_index)

    def bottom_left_press(self) -> Activation:
        """Return to the parent page, or remain on the root page."""
        if len(self._pages) > 1:
            self._pages.pop()
            self._selected_index = 0
            self._refresh()
        return Activation(self._state)

    def lcd_press(self, index: int) -> Activation:
        """Activate visible LCD key ``index`` (zero through five)."""
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < MAX_ITEMS:
            raise MenuValidationError("LCD index must be an integer from 0 through 5")
        if index >= len(self._page.items):
            raise MenuValidationError("LCD index does not name a visible item")
        return self._activate(index)

    @property
    def _page(self) -> MenuPage:
        return self._pages[-1]

    def _activate(self, index: int) -> Activation:
        item = self._page.items[index]
        if item.submenu is not None:
            self._pages.append(item.submenu)
            self._selected_index = 0
            return Activation(self._refresh())
        return Activation(self._state, item.action)

    def _refresh(self) -> MenuState:
        self._state = self._render_state()
        return self._state

    def _render_state(self) -> MenuState:
        labels = tuple(item.label for item in self._page.items)
        padded = labels + ("",) * (MAX_ITEMS - len(labels))
        return MenuState(self._page.title, padded, self._selected_index)  # type: ignore[arg-type]


__all__ = [
    "Activation",
    "MAX_ITEMS",
    "Menu",
    "MenuItem",
    "MenuPage",
    "MenuState",
    "MenuValidationError",
]
