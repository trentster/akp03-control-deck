"""Closed, application-owned catalogue of non-destructive Omarchy menu routes.

This is intentionally data rather than a pass-through to Omarchy configuration:
the daemon accepts only these reviewed submenu routes and never an arbitrary route
or underlying Omarchy action.
"""
from __future__ import annotations

from dataclasses import dataclass


OMARCHY_MENU_EXECUTABLE = "/usr/bin/omarchy-menu"


@dataclass(frozen=True)
class OmarchyMenuRoute:
    """One reviewed route exposed by the AKP03 Action Library."""

    route: str
    group: str
    name: str
    path: str
    description: str


# The route IDs are from Omarchy's installed JSONC root catalogue.  Keep this
# list deliberately small: it exposes navigation menus, not individual leaf
# operations such as suspend or shutdown.
OMARCHY_MENU_ROUTES = (
    OmarchyMenuRoute("root", "Start", "Go…", "Go…", "Open Omarchy’s top-level Go… menu."),
    OmarchyMenuRoute("apps", "Primary menus", "Apps", "Go… → Apps", "Browse application launch categories."),
    OmarchyMenuRoute("learn", "Primary menus", "Learn", "Go… → Learn", "Browse Omarchy learning resources."),
    OmarchyMenuRoute("trigger", "Primary menus", "Trigger", "Go… → Trigger", "Browse reviewed trigger-menu navigation."),
    OmarchyMenuRoute("style", "Primary menus", "Style", "Go… → Style", "Browse appearance and style menus."),
    OmarchyMenuRoute("setup", "Primary menus", "Setup", "Go… → Setup", "Browse setup menus without exposing leaf commands."),
    OmarchyMenuRoute("install", "Primary menus", "Install", "Go… → Install", "Browse installation menus without exposing leaf commands."),
    OmarchyMenuRoute("remove", "Primary menus", "Remove", "Go… → Remove", "Browse removal menus without exposing leaf commands."),
    OmarchyMenuRoute("update", "Primary menus", "Update", "Go… → Update", "Browse update menus without exposing leaf commands."),
    OmarchyMenuRoute("system", "Primary menus", "System", "Go… → System", "Browse system menus without exposing shutdown or suspend leaves."),
)

OMARCHY_MENU_ROUTES_BY_ID = {entry.route: entry for entry in OMARCHY_MENU_ROUTES}
ALLOWED_OMARCHY_MENU_ROUTES = frozenset(OMARCHY_MENU_ROUTES_BY_ID)


def omarchy_menu_route(route: str) -> OmarchyMenuRoute | None:
    """Return a reviewed route, or ``None`` for any unrecognised input."""
    return OMARCHY_MENU_ROUTES_BY_ID.get(route)
