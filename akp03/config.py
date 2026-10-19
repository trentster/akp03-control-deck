"""Validated, permission-checked configuration for akp03d."""
from __future__ import annotations

import contextlib
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import tomli_w

from .menu import MenuItem, MenuPage, MenuValidationError
from .events import MEDIA_KEYS
from .custom_menu import CONTENT_KEYS, MENU_DIAL_SLOTS, CustomMenu, CustomMenuControl, CustomMenuItem, CustomMenuPage
from .omarchy_menu_catalogue import ALLOWED_OMARCHY_MENU_ROUTES


class ConfigError(ValueError):
    pass


class ConfigNotFound(ConfigError):
    """The configured path does not exist at all — a true first run.

    Deliberately a distinct subclass of :class:`ConfigError` so a caller can
    bootstrap a *missing* config while still refusing to touch one that exists
    but is unreadable, malformed, or invalid.  Because it subclasses
    ``ConfigError``, every existing ``except ConfigError`` (and every
    ``pytest.raises(ConfigError)``) keeps working unchanged.
    """

    pass


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
_ACTION_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
# A menu item's icon is a library reference, never a path: the device-bound name
# (key<N>.jpg) belongs to [images], and a menu key is not one of the six faces.
MENU_ICON_REFERENCE = re.compile(r"pack:[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_.-]*\Z")
# How long a key label may be.  The config validates it, the daemon draws it and
# the editor previews it, so the number lives in one place.
LABEL_MAX_LENGTH = 32
_LABEL_TEXT = re.compile(rf".{{1,{LABEL_MAX_LENGTH}}}\Z", re.DOTALL)


@dataclass(frozen=True)
class Binding:
    action: str = "none"
    keys: tuple[str, ...] = ()
    keys_hold: tuple[str, ...] = ()
    shell: tuple[str, ...] = ()
    http_method: str = "GET"
    http_url: str | None = None
    http_json: Any = None
    http_headers: tuple[tuple[str, str], ...] = ()
    brightness: int | None = None
    omarchy_route: str | None = None
    menu_name: str | None = None
    # How a keys assignment behaves: ``tap`` presses and lets go at once, while
    # ``hold`` keeps the chord down until the control is released.  The mode
    # belongs to the binding, not the action, so one action can be tapped on one
    # control and held on another.
    mode: str = "tap"


ALLOWED_MODES = ("tap", "hold")


def _checked_mode(name: str, raw: Any) -> str:
    """Validate an assignment's optional ``mode``.

    ``keys_hold`` is the older way of saying the same thing and stays supported
    for hand-written configs; a ``keys`` action with ``mode = "hold"`` says it
    per assignment instead.
    """
    if raw is None:
        return "tap"
    if raw not in ALLOWED_MODES:
        raise ConfigError(f"binding {name} mode must be {' or '.join(ALLOWED_MODES)}")
    return raw


@dataclass(frozen=True)
class Feedback:
    enabled: bool = False
    duration_ms: int = 180


# How long the deck may sit untouched before its keys go dark.  Fifteen minutes
# never blanks a deck someone is thinking about, and spares the panel overnight.
DEFAULT_BLANK_AFTER_S = 900


@dataclass(frozen=True)
class Screen:
    """What the deck does when nobody is using it.

    The panel's own keys are the part that suffers from a fixed bright picture,
    and the answer is the one the deck's own protocol documents: a brightness of
    0 is ignored by the firmware, and dark means cleared keys.  Any key, button
    or dial restarts the countdown, and the deck comes back exactly as it was.
    Zero means the keys are never blanked.
    """

    blank_after_s: int = DEFAULT_BLANK_AFTER_S


@dataclass(frozen=True)
class Config:
    device: str
    poll_seconds: float
    max_hold_s: float
    bindings: dict[str, Binding]
    actions: dict[str, Binding]
    labels: dict[int, str]
    images: dict[int, Path]
    feedback: Feedback = Feedback()
    menu_enabled: bool = False
    menu_root: MenuPage | None = None
    custom_menus: dict[str, CustomMenu] | None = None
    # The parts of the dials the deck's own menu takes over while it is open.  A menu
    # that names the big knob's turns no longer moves its highlight by dial, which is
    # the point: a menu opened to work in should still turn the volume down.  Declared
    # after ``custom_menus`` so every existing positional Config(...) keeps its meaning.
    menu_dials: dict[str, str] = field(default_factory=dict)
    screen: Screen = Screen()
    # The text each of the six LCD keys shows: derived from the key's binding
    # (the assigned action's human name) and falling back to the static
    # ``[labels]`` text for an unbound key. Derived at load time and never
    # written back -- ``labels`` is the round-tripping source of truth.
    key_labels: dict[int, str] = field(default_factory=dict)


def editor_action_title(action_id: str) -> str:
    """Render an action id exactly as the browser editor does.

    Mirrors ``title()`` in ``webui_assets/app.js``: ``-`` and ``_`` become
    spaces and each word is capitalised, so ``new-window`` reads ``New Window``
    and ``undo`` reads ``Undo``.
    """
    return re.sub(r"[-_]", " ", action_id).title()


def lcd_key_labels(
    bindings: Mapping[str, Binding],
    binding_names: Mapping[str, str],
    labels: Mapping[int, str],
) -> dict[int, str]:
    """Derive the text each LCD key shows, from its binding.

    A key bound to a named action shows that action's human name exactly as the
    editor presents it: a brightness action shows ``Brightness N%``; any other
    named action shows its id rendered by :func:`editor_action_title` (for
    example ``new-window`` -> ``New Window``). An unbound key -- or one bound to
    an *unnamed* inline action the editor cannot present -- keeps the configured
    ``[labels]`` text. A key with neither is left blank (not painted).
    """
    derived: dict[int, str] = {}
    for key in range(6):
        name = f"key{key}"
        binding = bindings.get(name)
        text: str | None = None
        if binding is not None and binding.action != "none":
            if binding.action == "brightness" and binding.brightness is not None:
                text = f"Brightness {binding.brightness}%"
            elif name in binding_names:
                text = editor_action_title(binding_names[name])
        if text is None:
            text = labels.get(key)
        if text is not None:
            derived[key] = text
    return derived


def default_config_path() -> Path:
    return Path.home() / ".config" / "akp03" / "config.toml"


def managed_image_path(config_path: Path, key: int) -> Path:
    """Return the sole daemon-owned asset name permitted for an LCD key."""
    return config_path.parent / "images" / f"key{key}.jpg"


def _images(raw: Any, config_path: Path, *, require_files: bool = True) -> dict[int, Path]:
    if not isinstance(raw, dict):
        raise ConfigError("images must be a table")
    images: dict[int, Path] = {}
    for name, value in raw.items():
        if (not isinstance(name, str) or not name.startswith("key") or not name[3:].isdigit()
                or not 0 <= int(name[3:]) <= 5 or value != f"{name}.jpg"):
            raise ConfigError("images must use key0 through key5 values exactly matching keyN.jpg")
        key = int(name[3:])
        asset = managed_image_path(config_path, key)
        if require_files:
            try:
                details = asset.lstat()
            except OSError as error:
                raise ConfigError(f"configured image {name} is unavailable: {error}") from error
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise ConfigError(f"configured image {name} must be a regular file, not a link or special file")
        images[key] = asset
    return images


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{name} must be a non-empty array of strings")
    return tuple(value)


def _headers(value: Any, name: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ConfigError(f"binding {name} headers must be a table")
    result: list[tuple[str, str]] = []
    for header, item in value.items():
        if not isinstance(header, str) or not header or "\r" in header or "\n" in header:
            raise ConfigError(f"binding {name} headers must use valid string names")
        if not isinstance(item, str) or "\r" in item or "\n" in item:
            raise ConfigError(f"binding {name} header values must be strings without newlines")
        if item.startswith("env:") and not _ENV_NAME.fullmatch(item[4:]):
            raise ConfigError(f"binding {name} has an invalid environment variable reference")
        result.append((header, item))
    return tuple(result)


def _binding(name: str, raw: Any) -> Binding:
    if not isinstance(raw, dict):
        raise ConfigError(f"binding {name} must be a table")
    action = raw.get("action", "none")
    allowed_fields = {
        "none": {"action"},
        "keys": {"action", "keys", "mode"},
        "keys_hold": {"action", "keys_hold"},
        "shell": {"action", "shell"},
        "http": {"action", "method", "url", "json", "headers"},
        "brightness": {"action", "brightness"},
        "omarchy_menu": {"action", "route"},
        "menu": {"action", "menu"},
    }
    if action not in allowed_fields:
        raise ConfigError(f"binding {name} has unsupported action")
    unknown = set(raw) - allowed_fields[action]
    if unknown:
        raise ConfigError(f"binding {name} has unsupported fields: {', '.join(sorted(unknown))}")
    keys = _strings(raw.get("keys"), f"binding {name} keys") if action == "keys" else ()
    mode = _checked_mode(name, raw.get("mode")) if action == "keys" else "tap"
    if mode != "tap" and set(keys) & set(MEDIA_KEYS):
        # A media function is a single event pair: there is no held state to ask
        # for, and accepting the mode would only fail on the first press.
        raise ConfigError(f"binding {name} cannot hold a media key")
    held = _strings(raw.get("keys_hold"), f"binding {name} keys_hold") if action == "keys_hold" else ()
    shell = _strings(raw.get("shell"), f"binding {name} shell") if action == "shell" else ()
    brightness = raw.get("brightness")
    if action == "brightness" and (isinstance(brightness, bool) or not isinstance(brightness, int) or not 1 <= brightness <= 100):
        raise ConfigError(f"binding {name} brightness must be 1-100")
    if action == "omarchy_menu":
        route = raw.get("route")
        if not isinstance(route, str) or route not in ALLOWED_OMARCHY_MENU_ROUTES:
            raise ConfigError(f"binding {name} route must name a reviewed Omarchy menu route")
        return Binding(action, omarchy_route=route)
    if action == "menu":
        menu_name = raw.get("menu")
        if not isinstance(menu_name, str) or not _ACTION_ID.fullmatch(menu_name):
            raise ConfigError(f"binding {name} menu must name a custom menu")
        return Binding(action, menu_name=menu_name)
    if action != "http":
        return Binding(action, keys, held, shell, brightness=brightness, mode=mode)
    method = raw.get("method", "GET")
    url = raw.get("url")
    body = raw.get("json")
    if not isinstance(method, str) or method.upper() not in _HTTP_METHODS:
        raise ConfigError(f"binding {name} method must be one of {', '.join(sorted(_HTTP_METHODS))}")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ConfigError(f"binding {name} url must be an http(s) URL")
    if body is not None:
        try:
            import json
            json.dumps(body)
        except (TypeError, ValueError) as error:
            raise ConfigError(f"binding {name} json must be JSON serializable") from error
    return Binding(action, http_method=method.upper(), http_url=url, http_json=body, http_headers=_headers(raw.get("headers"), name))


def _actions(raw: Any) -> dict[str, Binding]:
    if not isinstance(raw, dict):
        raise ConfigError("actions must be a table")
    actions: dict[str, Binding] = {}
    for action_id, definition in raw.items():
        if not isinstance(action_id, str) or not _ACTION_ID.fullmatch(action_id):
            raise ConfigError("action IDs must match [a-z][a-z0-9_-]{0,63}")
        actions[action_id] = _binding(f"action {action_id}", definition)
    return actions


def _bindings(raw: Any, actions: dict[str, Binding]) -> dict[str, Binding]:
    if not isinstance(raw, dict):
        raise ConfigError("bindings must be a table")
    bindings: dict[str, Binding] = {}
    for name, definition in raw.items():
        if not isinstance(definition, dict):
            raise ConfigError(f"binding {name} must be a table")
        if "action_id" in definition:
            if set(definition) - {"action_id", "mode"}:
                raise ConfigError(f"binding {name} action_id cannot be combined with inline fields")
            action_id = definition["action_id"]
            if not isinstance(action_id, str) or action_id not in actions:
                raise ConfigError(f"binding {name} action_id must name a declared action")
            mode = _checked_mode(name, definition.get("mode"))
            if mode == "tap":
                # No mode: the binding *is* the action, so one reference serves
                # every control that shares it.
                bindings[name] = actions[action_id]
            elif actions[action_id].action != "keys":
                raise ConfigError(f"binding {name} mode is only meaningful for a keys action")
            elif set(actions[action_id].keys) & set(MEDIA_KEYS):
                # Referred to by id the same rule holds: a media function has no
                # held state, and accepting the mode only fails on the first press.
                raise ConfigError(f"binding {name} cannot hold a media key")
            else:
                # A held assignment is the shared action with a mode of its own.
                bindings[name] = replace(actions[action_id], mode=mode)
        else:
            bindings[name] = _binding(name, definition)
            if bindings[name].action in {"omarchy_menu", "menu"}:
                raise ConfigError(f"binding {name} {bindings[name].action} must use a named action_id")
    return bindings


def _checked_item_icon(icon: Any, name: str, page_id: str, item_number: int) -> str | None:
    """Validate one menu item's optional icon reference, or return None.

    Only ``pack:<pack>/<name>`` is accepted.  Whether that icon exists is the
    library's business (it can be added to a pack later), but its *shape* is
    checked here so a config can never smuggle a file path onto a key face.
    """
    if icon is None:
        return None
    if not isinstance(icon, str) or not MENU_ICON_REFERENCE.fullmatch(icon):
        raise ConfigError(
            f"custom menu {name!r} page {page_id!r} item {item_number} icon must be a "
            f"library reference such as pack:general/copy"
        )
    return icon


# The three buttons under the LCD keys, in the order they sit on the deck.  A menu
# may take these over while it is open.  Nothing else can be claimed: the six LCD
# keys are the menu's own content, and the dials are how a menu is navigated.
MENU_CONTROL_NAMES = ("button6", "button8", "button7")

# What a menu may put on one of them: the GUI-safe set a menu item may name, plus a
# held chord, because a button under the keys can be held down.
MENU_CONTROL_ACTIONS = frozenset({"keys", "keys_hold", "brightness", "omarchy_menu"})


def _custom_menu_controls(name: str, raw: Any, actions: dict[str, Binding]) -> dict[str, CustomMenuControl]:
    """Parse the buttons a menu takes over for as long as it is open.

    Two spellings mean one thing: a bare action name, which the deck taps, and a
    table naming the action with a mode - what the editor writes when the button is
    held down until it is let go.  A button a menu does not name keeps its ordinary
    binding, so nothing that works on the deck stops working inside a menu.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"custom menu {name!r} controls must be a table")
    controls: dict[str, CustomMenuControl] = {}
    for control, value in raw.items():
        if control not in MENU_CONTROL_NAMES:
            raise ConfigError(
                f"custom menu {name!r} can only take over {', '.join(MENU_CONTROL_NAMES)}: "
                "the six LCD keys are the menu's content and the dials are its navigation"
            )
        if isinstance(value, str):
            action_id, mode = value, "tap"
        elif isinstance(value, dict) and isinstance(value.get("action_id"), str) and not set(value) - {"action_id", "mode"}:
            action_id, mode = value["action_id"], value.get("mode", "tap")
        else:
            raise ConfigError(f"custom menu {name!r} control {control!r} must name an action")
        if mode not in ALLOWED_MODES:
            raise ConfigError(f'custom menu {name!r} control {control!r} mode must be "tap" or "hold"')
        target = actions.get(action_id)
        if target is None or target.action not in MENU_CONTROL_ACTIONS:
            raise ConfigError(f"custom menu {name!r} control {control!r} must name a GUI-safe action")
        if mode == "hold" and target.action not in {"keys", "keys_hold"}:
            raise ConfigError(f"custom menu {name!r} control {control!r} can only hold a keys action")
        controls[control] = CustomMenuControl(action_id, mode)
    return controls


def _custom_menu_dials(raw: Any, actions: dict[str, Binding], *, label: str) -> dict[str, str]:
    """Parse the parts of a dial a menu takes over for as long as it is open.

    One spelling only: a part of a dial is either claimed by an action or it is not.
    There is no mode, because a turn has no release to hold and a press is over the
    moment it happens.  A menu names only the parts of the big and right dials it
    wants: whatever it leaves out keeps its ordinary job.

    ``label`` names the menu in a refusal, so the message says which menu asked for
    something impossible rather than which function noticed.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} dials must be a table")
    dials: dict[str, str] = {}
    for slot, value in raw.items():
        if slot not in MENU_DIAL_SLOTS:
            raise ConfigError(
                f"{label} can only take over {', '.join(MENU_DIAL_SLOTS)}: "
                "the left dial's press is always the way back and its turns are nothing while a menu is open"
            )
        if not isinstance(value, str):
            raise ConfigError(f"{label} dial {slot!r} must name an action")
        target = actions.get(value)
        if target is None or target.action not in MENU_CONTROL_ACTIONS:
            raise ConfigError(f"{label} dial {slot!r} must name a GUI-safe action")
        dials[slot] = value
    return dials


def _custom_menus(raw: Any, actions: dict[str, Binding]) -> dict[str, CustomMenu]:
    """Parse editor-owned menu profiles without changing the legacy [menu] demo."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("custom_menus must be a table")
    result: dict[str, CustomMenu] = {}
    # Every (menu, page) -> target link seen while parsing.  Targets are resolved
    # after all menus are known, because a key may open any menu, not only a page
    # of its own.
    pending: list[tuple[str, str, str]] = []
    for name, definition in raw.items():
        if not isinstance(name, str) or not _ACTION_ID.fullmatch(name) or not isinstance(definition, dict):
            raise ConfigError("custom menu names must match [a-z][a-z0-9_-]{0,63} and use tables")
        if set(definition) - {"root", "pages", "controls", "dials"}:
            raise ConfigError(f"custom menu {name!r} has unsupported fields")
        root, pages = definition.get("root"), definition.get("pages")
        if not isinstance(root, str) or not _ACTION_ID.fullmatch(root):
            raise ConfigError(f"custom menu {name!r} root must name a page")
        if not isinstance(pages, list) or not pages:
            raise ConfigError(f"custom menu {name!r} pages must be a non-empty array")
        parsed: dict[str, CustomMenuPage] = {}
        links: list[tuple[str, str]] = []
        for number, page in enumerate(pages, 1):
            if not isinstance(page, dict) or set(page) - {"id", "title", "items"}:
                raise ConfigError(f"custom menu {name!r} page {number} is malformed")
            page_id, title, items = page.get("id"), page.get("title"), page.get("items")
            if not isinstance(page_id, str) or not _ACTION_ID.fullmatch(page_id) or page_id in parsed:
                raise ConfigError(f"custom menu {name!r} page IDs must be unique valid names")
            if not isinstance(title, str) or not title.strip() or len(title) > 32:
                raise ConfigError(f"custom menu {name!r} page {page_id!r} title must be 1 through 32 characters")
            # Zero items is legal: a menu can be created before it is filled in,
            # and the deck then shows blank keys until a button is given something.
            if not isinstance(items, list) or len(items) > len(CONTENT_KEYS):
                raise ConfigError(f"custom menu {name!r} page {page_id!r} cannot hold more than {len(CONTENT_KEYS)} items")
            controls: set[int] = set(); built: list[CustomMenuItem] = []
            for item_number, item in enumerate(items, 1):
                if not isinstance(item, dict) or set(item) - {"key", "label", "action", "submenu", "icon"}:
                    raise ConfigError(f"custom menu {name!r} page {page_id!r} item {item_number} is malformed")
                key, label, action, submenu = item.get("key"), item.get("label"), item.get("action"), item.get("submenu")
                icon = _checked_item_icon(item.get("icon"), name, page_id, item_number)
                if isinstance(key, bool) or not isinstance(key, int) or key not in CONTENT_KEYS or key in controls:
                    raise ConfigError(f"custom menu {name!r} page {page_id!r} items must use unique content keys 0 through {len(CONTENT_KEYS) - 1}")
                if not isinstance(label, str) or not label.strip() or len(label) > 32:
                    raise ConfigError(f"custom menu {name!r} page {page_id!r} item {item_number} label must be 1 through 32 characters")
                if (action is None) == (submenu is None):
                    raise ConfigError(f"custom menu {name!r} page {page_id!r} item {item_number} must define exactly one of action or submenu")
                if action is not None:
                    target = actions.get(action) if isinstance(action, str) else None
                    if target is None or target.action not in {"keys", "brightness", "omarchy_menu"}:
                        raise ConfigError(f"custom menu {name!r} page {page_id!r} item {item_number} must name a GUI-safe action")
                else:
                    if not isinstance(submenu, str) or not _ACTION_ID.fullmatch(submenu):
                        raise ConfigError(f"custom menu {name!r} page {page_id!r} item {item_number} submenu must name a page")
                    links.append((page_id, submenu))
                controls.add(key); built.append(CustomMenuItem(key, label, action=action, submenu=submenu, icon=icon))
            parsed[page_id] = CustomMenuPage(page_id, title, tuple(built))
        if root not in parsed:
            raise ConfigError(f"custom menu {name!r} root must name a declared page")
        pending.extend((name, source, destination) for source, destination in links)
        result[name] = CustomMenu(name, root, parsed, _custom_menu_controls(name, definition.get("controls"), actions),
                                  _custom_menu_dials(definition.get("dials"), actions,
                                                     label=f"custom menu {name!r}"))

    # A key's target is a page of the menu being shown (configurations written
    # before menus could nest) or another menu by name.
    graph: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for menu_name, page_id, target in pending:
        menu = result[menu_name]
        if target in menu.pages:
            graph.setdefault((menu_name, page_id), []).append((menu_name, target))
        elif target in result:
            graph.setdefault((menu_name, page_id), []).append((target, result[target].root))
        else:
            raise ConfigError(f"custom menu {menu_name!r} submenu must name a page of that menu or another menu")
    visiting: set[tuple[str, str]] = set()
    visited: set[tuple[str, str]] = set()

    def visit(node: tuple[str, str]) -> None:
        if node in visiting:
            raise ConfigError("custom menus must not contain cycles")
        if node not in visited:
            visiting.add(node)
            for destination in graph.get(node, ()):
                visit(destination)
            visiting.remove(node)
            visited.add(node)

    for node in [(name, page_id) for name, menu in result.items() for page_id in menu.pages]:
        visit(node)
    for action_id, binding in actions.items():
        if binding.action == "menu" and binding.menu_name not in result:
            raise ConfigError(f"action {action_id!r} menu must name a declared custom menu")
    return result


_MENU_BRIGHTNESS_ACTION = re.compile(r"brightness:(?:[1-9]|[1-9][0-9]|100)\Z")


def _menu_root(menu: dict[str, Any], actions: dict[str, Binding]) -> MenuPage | None:
    # Parse a deliberately small declarative menu tree from TOML data.
    unknown = set(menu) - {"enabled", "root", "pages", "dials"}
    if unknown:
        raise ConfigError(f"menu has unsupported fields: {', '.join(sorted(unknown))}")
    pages = menu.get("pages")
    root = menu.get("root")
    if pages is None:
        if root is not None:
            raise ConfigError("menu root requires menu pages")
        return None
    if not isinstance(pages, list) or not pages:
        raise ConfigError("menu pages must be a non-empty array of tables")
    if not isinstance(root, str) or not root.strip():
        raise ConfigError("menu root must name a page")

    definitions: dict[str, dict[str, Any]] = {}
    for number, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            raise ConfigError(f"menu page {number} must be a table")
        unknown = set(page) - {"id", "title", "items"}
        if unknown:
            raise ConfigError(f"menu page {number} has unsupported fields: {', '.join(sorted(unknown))}")
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id.strip():
            raise ConfigError(f"menu page {number} id must be a non-empty string")
        if page_id in definitions:
            raise ConfigError(f"menu page id {page_id!r} is duplicated")
        definitions[page_id] = page
    if root not in definitions:
        raise ConfigError("menu root must name a declared page")

    built: dict[str, MenuPage] = {}
    building: set[str] = set()

    def build(page_id: str) -> MenuPage:
        if page_id in built:
            return built[page_id]
        if page_id in building:
            raise ConfigError("menu submenus must not contain cycles")
        page = definitions[page_id]
        title = page.get("title", "")
        items = page.get("items")
        if not isinstance(items, list):
            raise ConfigError(f"menu page {page_id!r} items must be an array")
        building.add(page_id)
        try:
            parsed: list[MenuItem] = []
            for number, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    raise ConfigError(f"menu page {page_id!r} item {number} must be a table")
                unknown = set(item) - {"label", "action", "submenu"}
                if unknown:
                    raise ConfigError(f"menu page {page_id!r} item {number} has unsupported fields: {', '.join(sorted(unknown))}")
                label, action, submenu = item.get("label"), item.get("action"), item.get("submenu")
                if (action is None) == (submenu is None):
                    raise ConfigError(f"menu page {page_id!r} item {number} must define exactly one of action or submenu")
                if action is not None:
                    if not isinstance(action, str):
                        raise ConfigError(f"menu page {page_id!r} item {number} action must be a string")
                    if _MENU_BRIGHTNESS_ACTION.fullmatch(action) is None:
                        target = actions.get(action)
                        if target is None:
                            raise ConfigError(f"menu page {page_id!r} item {number} action must name a declared action or be brightness:1 through brightness:100")
                        if target.action not in {"keys", "brightness", "omarchy_menu"}:
                            raise ConfigError(f"menu page {page_id!r} item {number} cannot use unsafe action {target.action!r}; only GUI-safe actions are allowed")
                    parsed.append(MenuItem(label, action=action))
                else:
                    if not isinstance(submenu, str) or submenu not in definitions:
                        raise ConfigError(f"menu page {page_id!r} item {number} submenu must name a declared page")
                    parsed.append(MenuItem(label, submenu=build(submenu)))
            try:
                result = MenuPage(title=title, items=tuple(parsed))
            except MenuValidationError as error:
                raise ConfigError(f"invalid menu page {page_id!r}: {error}") from error
            built[page_id] = result
            return result
        finally:
            building.remove(page_id)

    return build(root)

def load_config(path: Path | None = None, *, _require_image_files: bool = True) -> Config:
    path = default_config_path() if path is None else path
    try:
        details = path.stat()
    except FileNotFoundError as error:
        # The file is simply not there. This is the one, clearly distinguished
        # case a caller may bootstrap; an existing-but-unreadable config below
        # stays a plain ConfigError and is never overwritten.
        raise ConfigNotFound(f"cannot read config: {error}") from error
    except OSError as error:
        raise ConfigError(f"cannot read config: {error}") from error
    if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600:
        raise ConfigError("config must be a regular file with mode 0600")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"invalid config: {error}") from error
    return _config_from_mapping(raw, path, require_image_files=_require_image_files)


def _config_from_mapping(raw: dict[str, Any], path: Path, *, require_image_files: bool) -> Config:
    daemon = raw.get("daemon", {})
    if not isinstance(daemon, dict):
        raise ConfigError("daemon must be a table")
    device, poll = daemon.get("device", "/dev/hidraw0"), daemon.get("poll_seconds", 1.0)
    if not isinstance(device, str) or not device.startswith("/dev/"):
        raise ConfigError("daemon device must be an absolute /dev path")
    if not isinstance(poll, (int, float)) or not 0.1 <= poll <= 60:
        raise ConfigError("daemon poll_seconds must be 0.1-60")
    deck = raw.get("deck", {})
    if not isinstance(deck, dict):
        raise ConfigError("deck must be a table")
    max_hold_s = deck.get("max_hold_s", 120)
    if isinstance(max_hold_s, bool) or not isinstance(max_hold_s, (int, float)) or not 1 <= max_hold_s <= 3600:
        raise ConfigError("deck max_hold_s must be 1-3600")
    screen_raw = raw.get("screen", {})
    if not isinstance(screen_raw, dict):
        raise ConfigError("screen must be a table")
    blank_after_s = screen_raw.get("blank_after_s", DEFAULT_BLANK_AFTER_S)
    if isinstance(blank_after_s, bool) or not isinstance(blank_after_s, int) or not 0 <= blank_after_s <= 86400:
        raise ConfigError("screen blank_after_s must be 0 (never) or an integer from 1 through 86400")
    feedback_raw = raw.get("feedback", {})
    if not isinstance(feedback_raw, dict):
        raise ConfigError("feedback must be a table")
    enabled = feedback_raw.get("enabled", False)
    duration_ms = feedback_raw.get("duration_ms", 180)
    if not isinstance(enabled, bool):
        raise ConfigError("feedback enabled must be true or false")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 150 <= duration_ms <= 2000:
        raise ConfigError("feedback duration_ms must be an integer from 150 through 2000")
    feedback = Feedback(enabled, duration_ms)
    menu_raw = raw.get("menu", {})
    if not isinstance(menu_raw, dict):
        raise ConfigError("menu must be a table")
    menu_enabled = menu_raw.get("enabled", False)
    if not isinstance(menu_enabled, bool):
        raise ConfigError("menu enabled must be true or false")
    actions = _actions(raw.get("actions", {}))
    custom_menus = _custom_menus(raw.get("custom_menus"), actions)
    menu_root = _menu_root(menu_raw, actions)
    menu_dials = _custom_menu_dials(menu_raw.get("dials"), actions, label="the deck's menu")
    bindings = _bindings(raw.get("bindings", {}), actions)
    # The action id a binding references survives by identity -- the same way
    # save_config reconstructs it -- so the editor-presented name can be derived
    # without changing the resolved Binding.
    identity = {id(candidate): action_id for action_id, candidate in actions.items()}
    binding_names = {name: identity[id(binding)] for name, binding in bindings.items() if id(binding) in identity}
    labels_raw = raw.get("labels", {})
    if not isinstance(labels_raw, dict):
        raise ConfigError("labels must be a table")
    labels: dict[int, str] = {}
    for name, text in labels_raw.items():
        if (
            not isinstance(name, str)
            or not name.startswith("key")
            or not name[3:].isdigit()
            or not 0 <= int(name[3:]) <= 5
            or not isinstance(text, str)
            or _LABEL_TEXT.fullmatch(text) is None
        ):
            raise ConfigError(
                f"labels must use key0 through key5 string values of 1 through {LABEL_MAX_LENGTH} characters"
            )
        labels[int(name[3:])] = text
    images = _images(raw.get("images", {}), path, require_files=require_image_files)
    key_labels = lcd_key_labels(bindings, binding_names, labels)
    return Config(
        device, float(poll), float(max_hold_s), bindings, actions, labels, images,
        feedback, menu_enabled, menu_root, custom_menus, key_labels=key_labels,
        screen=Screen(blank_after_s), menu_dials=menu_dials,
    )


def _binding_document(binding: Binding) -> dict[str, Any]:
    """Serialise one fully-resolved binding back to its declarative TOML shape."""
    action = binding.action
    if action == "none":
        return {"action": "none"}
    if action == "keys":
        document: dict[str, Any] = {"action": "keys", "keys": list(binding.keys)}
        if binding.mode != "tap":
            # Only the non-default is written, so an ordinary assignment stays
            # as short as it has always been.
            document["mode"] = binding.mode
        return document
    if action == "keys_hold":
        return {"action": "keys_hold", "keys_hold": list(binding.keys_hold)}
    if action == "shell":
        return {"action": "shell", "shell": list(binding.shell)}
    if action == "brightness":
        return {"action": "brightness", "brightness": binding.brightness}
    if action == "http":
        document: dict[str, Any] = {"action": "http", "method": binding.http_method, "url": binding.http_url}
        if binding.http_json is not None:
            document["json"] = binding.http_json
        if binding.http_headers:
            document["headers"] = {name: value for name, value in binding.http_headers}
        return document
    if action == "omarchy_menu":
        return {"action": "omarchy_menu", "route": binding.omarchy_route}
    if action == "menu":
        return {"action": "menu", "menu": binding.menu_name}
    raise ConfigError(f"cannot serialise unsupported action: {action!r}")


def _menu_document(config: Config) -> dict[str, Any]:
    """Serialise the legacy demo menu tree, assigning stable page IDs on the fly."""
    document: dict[str, Any] = {"enabled": config.menu_enabled}
    root = config.menu_root
    if root is None:
        return document
    assigned: dict[int, str] = {}
    order: list[MenuPage] = []

    def assign(page: MenuPage) -> str:
        identifier = assigned.get(id(page))
        if identifier is None:
            identifier = f"page{len(assigned)}"
            assigned[id(page)] = identifier
        return identifier

    def collect(page: MenuPage) -> None:
        if id(page) in assigned:
            return
        assign(page)
        order.append(page)
        for item in page.items:
            if item.submenu is not None:
                collect(item.submenu)

    collect(root)
    document["root"] = assign(root)
    document["pages"] = [
        {
            "id": assigned[id(page)],
            "title": page.title,
            "items": [
                {"label": item.label, "submenu": assign(item.submenu)}
                if item.submenu is not None
                else {"label": item.label, "action": item.action}
                for item in page.items
            ],
        }
        for page in order
    ]
    if config.menu_dials:
        # The parts of the dials this menu takes over, in the order the panel reads them.
        # Written back or a save would quietly drop what the person had just set.
        document["dials"] = {
            slot: config.menu_dials[slot]
            for slot in MENU_DIAL_SLOTS
            if slot in config.menu_dials
        }
    return document


def _custom_menus_document(config: Config) -> dict[str, Any]:
    menus = config.custom_menus or {}
    document: dict[str, Any] = {}
    for name, menu in menus.items():
        pages: list[dict[str, Any]] = []
        for page in menu.pages.values():
            items: list[dict[str, Any]] = []
            for item in page.items:
                entry: dict[str, Any] = {"key": item.key, "label": item.label}
                if item.submenu is not None:
                    entry["submenu"] = item.submenu
                else:
                    entry["action"] = item.action
                # An icon is what makes a menu key a picture instead of a word;
                # dropping it here would delete it on the next save.
                if item.icon is not None:
                    entry["icon"] = item.icon
                items.append(entry)
            pages.append({"id": page.page_id, "title": page.title, "items": items})
        entry: dict[str, Any] = {"root": menu.root, "pages": pages}
        if menu.controls:
            # Written in deck order, and in the short spelling for an ordinary tap:
            # one line per button is what a person reads in the file.
            entry["controls"] = {
                control: (assignment.action if assignment.mode == "tap"
                          else {"action_id": assignment.action, "mode": assignment.mode})
                for control, assignment in sorted(
                    menu.controls.items(), key=lambda pair: MENU_CONTROL_NAMES.index(pair[0])
                )
            }
        if menu.dials:
            # The parts of the dials this menu takes over, in the order the panel reads
            # them.  One line each: a part of a dial has nothing to say beyond the
            # action it runs, so there is no mode to write.
            entry["dials"] = {
                slot: menu.dials[slot]
                for slot in MENU_DIAL_SLOTS
                if slot in menu.dials
            }
        document[name] = entry
    return document


def _binding_reference(binding: Binding, config: Config) -> dict[str, Any]:
    """Emit an action reference when one exists, so identity survives a round trip."""
    identity = {id(candidate): action_id for action_id, candidate in config.actions.items()}
    action_id = identity.get(id(binding))
    mode = "tap"
    if action_id is None and binding.action == "keys" and binding.mode != "tap":
        # A held assignment is a copy of its action rather than the action
        # itself, so it is found by value: the reference and mode are written
        # out together.
        tapped = replace(binding, mode="tap")
        for candidate_id, candidate in config.actions.items():
            if candidate == tapped:
                action_id, mode = candidate_id, binding.mode
                break
    if action_id is None and binding.action in {"menu", "omarchy_menu"}:
        for candidate_id, candidate in config.actions.items():
            if candidate == binding:
                action_id = candidate_id
                break
    if action_id is not None:
        if mode != "tap":
            return {"action_id": action_id, "mode": mode}
        return {"action_id": action_id}
    if binding.action in {"menu", "omarchy_menu"}:
        raise ConfigError(f"binding with action {binding.action!r} must reference a declared action")
    return _binding_document(binding)


def config_document(config: Config) -> dict[str, Any]:
    """Build the declarative TOML document that reloads into ``config`` unchanged."""
    document: dict[str, Any] = {
        "daemon": {"device": config.device, "poll_seconds": config.poll_seconds},
        "deck": {"max_hold_s": config.max_hold_s},
        "screen": {"blank_after_s": config.screen.blank_after_s},
        "feedback": {"enabled": config.feedback.enabled, "duration_ms": config.feedback.duration_ms},
        "menu": _menu_document(config),
    }
    if config.actions:
        document["actions"] = {name: _binding_document(binding) for name, binding in config.actions.items()}
    if config.bindings:
        document["bindings"] = {name: _binding_reference(binding, config) for name, binding in config.bindings.items()}
    if config.labels:
        document["labels"] = {f"key{key}": text for key, text in config.labels.items()}
    if config.images:
        document["images"] = {f"key{key}": f"key{key}.jpg" for key in config.images}
    if config.custom_menus:
        document["custom_menus"] = _custom_menus_document(config)
    return document


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, never leaving a partial file behind."""
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    fd, temp = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    # Persist the directory entry so the rename itself survives a crash.
    with contextlib.suppress(OSError):
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def save_config(config: Config, path: Path | None = None) -> None:
    """Persist a validated Config atomically, refusing to write anything invalid.

    The candidate document is round-tripped through the same validators used by
    :func:`load_config` *before* any file is created, so a rejected save leaves
    the filesystem exactly as it found it.  The write itself uses a same-directory
    temporary file that is fsynced, chmodded to 0600, and atomically renamed.
    """
    path = default_config_path() if path is None else path
    document = config_document(config)
    text = tomli_w.dumps(document)
    try:
        reparsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"generated config is not valid TOML: {error}") from error
    _config_from_mapping(reparsed, path, require_image_files=True)
    _atomic_write(path, text)


def write_example(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write('''[daemon]
device = "/dev/hidraw0"
poll_seconds = 1

# The three caps under the glass start unbound, and so do the LCD keys: a fresh
# install should not arrive wearing somebody else's shortcuts.  A binding is the
# action plus the control it answers to - a chord to hold, a key to tap, an
# action from the library, or a menu.
#[bindings.button6]
#action = "keys_hold"
#keys_hold = ["ctrl", "shift", "space"]
#[bindings.button7]
#action = "keys"
#keys = ["enter"]
#[bindings.button8]
#action = "keys"
#keys = ["cmd", "z"]

# Shell actions are strict executable argv arrays: no shell parsing is ever performed.
#[bindings.top]
#action = "shell"
#shell = ["/usr/bin/notify-send", "AKP03 top knob"]

# HTTP header values may use env:NAME. Put those values only in
# ~/.config/akp03/env (mode 0600); akp03d.service loads that file.
#[bindings.bottom_left]
#action = "http"
#method = "POST"
#url = "https://example.invalid/hook"
#json = { source = "akp03" }
#headers = { Authorization = "env:AKP03_WEBHOOK_TOKEN" }

# LCD press feedback is intentionally disabled by default.
[feedback]
enabled = false
duration_ms = 180

# The LCD keys go dark after blank_after_s seconds with no key, button or dial
# touched, which is what spares the panel a fixed bright picture; the next touch
# brings the deck back exactly as it was.  0 keeps them lit.
[screen]
blank_after_s = 900

# Safe physical menu; disabled by default. With enabled=true and no pages,
# akp03d renders its built-in demo. A configured menu accepts only brightness:1
# through brightness:100 action tokens; it cannot run shell, HTTP, or key actions.
[menu]
enabled = false
#root = "home"
#[[menu.pages]]
#id = "home"
#title = "Home"
#items = [{ label = "Dim", action = "brightness:25" }, { label = "Display", submenu = "display" }]
#[[menu.pages]]
#id = "display"
#title = "Display"
#items = [{ label = "Full", action = "brightness:100" }]

[labels]
key0 = "One"
key1 = "Two"
key2 = "Three"
key3 = "Four"
key4 = "Five"
key5 = "Six"
''')
