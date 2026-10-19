from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from akp03.menu import Activation, Menu, MenuItem, MenuPage, MenuValidationError


def build_menu() -> Menu:
    settings = MenuPage(
        title="Settings",
        items=(
            MenuItem("Brightness", action="brightness"),
            MenuItem("About", action="about"),
        ),
    )
    return Menu(
        MenuPage(
            title="Home",
            items=(
                MenuItem("Launch", action="launch"),
                MenuItem("Settings", submenu=settings),
                MenuItem("Sleep", action="sleep"),
            ),
        )
    )


def test_state_is_renderable_immutable_and_pads_to_six_labels() -> None:
    menu = build_menu()
    assert menu.state.labels == ("Launch", "Settings", "Sleep", "", "", "")
    assert menu.state.selected_index == 0
    assert menu.state.title == "Home"
    with pytest.raises(FrozenInstanceError):
        menu.state.selected_index = 1  # type: ignore[misc]


def test_top_knob_wraps_selection_in_both_directions() -> None:
    menu = build_menu()
    assert menu.top_counterclockwise().selected_index == 2
    assert menu.top_clockwise().selected_index == 0
    assert menu.rotate(4).selected_index == 1
    assert menu.rotate(-5).selected_index == 2


def test_top_press_enters_submenu_and_returns_action_token() -> None:
    menu = build_menu()
    menu.top_clockwise()
    entered = menu.top_press()
    assert isinstance(entered, Activation)
    assert entered.action is None
    assert entered.state.title == "Settings"
    assert entered.state.labels == ("Brightness", "About", "", "", "", "")

    fired = menu.top_press()
    assert fired.action == "brightness"
    assert fired.state == menu.state


def test_bottom_left_back_returns_to_parent_and_is_noop_at_root() -> None:
    menu = build_menu()
    menu.lcd_press(1)
    assert menu.state.title == "Settings"
    assert menu.bottom_left_press().state.title == "Home"
    root = menu.state
    assert menu.bottom_left_press().state == root


def test_lcd_keys_activate_visible_item_without_changing_selection() -> None:
    menu = build_menu()
    fired = menu.lcd_press(2)
    assert fired.action == "sleep"
    assert menu.state.selected_index == 0
    entered = menu.lcd_press(1)
    assert entered.action is None
    assert entered.state.title == "Settings"


def test_validation_rejects_invalid_trees_and_inputs() -> None:
    with pytest.raises(MenuValidationError, match="one to six"):
        MenuPage(items=())
    with pytest.raises(MenuValidationError, match="at most six"):
        MenuPage(items=tuple(MenuItem(str(index), action=str(index)) for index in range(7)))
    with pytest.raises(MenuValidationError, match="non-empty"):
        MenuItem("", action="run")
    with pytest.raises(MenuValidationError, match="exactly one"):
        MenuItem("bad")
    with pytest.raises(MenuValidationError, match="exactly one"):
        MenuItem("bad", action="run", submenu=MenuPage(items=(MenuItem("child", action="x"),)))

    menu = build_menu()
    with pytest.raises(MenuValidationError, match="integer"):
        menu.rotate(True)
    with pytest.raises(MenuValidationError, match="0 through 5"):
        menu.lcd_press(6)
    with pytest.raises(MenuValidationError, match="visible item"):
        menu.lcd_press(5)
