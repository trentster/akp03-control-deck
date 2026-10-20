"""Local-only web editor control plane for AKP03 drafts and configuration.

The daemon hosts this editor in-process on loopback.  It exposes a validated
draft over localhost, a read-only ``/api/events`` Server-Sent Events stream fed
by an injected :class:`akp03.feed.ControlFeed`, and - when it is attached to a
real config path - a ``/api/save`` route that persists the draft through the
service.  The browser itself never writes a file; the service does.

Saving is deliberately narrow and round-trip safe: only the tables this editor
models (``[actions.*]`` for its safe actions, ``[bindings.key0..5]``, and
``[custom_menus.*]``) are replaced.  Every other byte of the on-disk config -
``[labels]``, ``[images]``, ``[feedback]``, ``[menu]``, ``[daemon]``, button and
dial bindings, comments and ordering - is preserved untouched.  When the editor
is not attached to a config (the sample draft, or the standalone dev server) it
says so honestly instead of pretending a save happened.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
from datetime import datetime
import mimetypes
import os
import queue
import stat
import tempfile
import threading
import tomllib
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import artwork
from . import actions_library
from . import backup
from . import iconpacks
from .config import (
    DEFAULT_BLANK_AFTER_S,
    LABEL_MAX_LENGTH,
    MENU_ICON_REFERENCE,
    ConfigError,
    load_config,
)
from .faces import Paint, label_png
from .feed import ControlFeed
from .custom_menu import MENU_DIAL_SLOTS
from .model import (
    ASSIGNABLE_CONTROLS,
    BUTTON_CONTROLS,
    DIAL_SLOTS,
    LCD_CONTROLS,
    SAFE_ACTION_TYPES,
    ActionDefinition,
    ControlPlaneEditorError,
    MenuDefinition,
    MenuControlDefinition,
    MenuItemDefinition,
    action_registry_toml,
    menu_definitions_from_custom_menus,
    replace_control_plane_sections,
    validate_action_definitions,
    validate_assignments,
    validate_menu_definitions,
)

ASSET_PACKAGE = "akp03"
ASSET_DIRECTORY = "webui_assets"
# The hand-written bindings the editor can name for itself.  A chord written
# straight into ``[bindings.X]`` carries no name to point at, so the load path
# adopts these and the save path refuses to overwrite anything else it cannot
# represent.
ADOPTABLE_CHORD_ACTIONS = ("keys", "keys_hold")


@dataclass(frozen=True)
class Draft:
    """The editor's working document: what the user has staged, not what is live.

    ``icons`` is ``None`` when the client never mentioned artwork, which means
    "leave the deck's images exactly as they are".  A tuple of six entries instead
    stages artwork for every key, and is the only case that may rewrite
    ``[images]`` on push.
    """

    actions: tuple[ActionDefinition, ...]
    bindings: tuple[str | None, ...]
    menus: tuple[MenuDefinition, ...]
    icons: tuple[str | None, ...] | None = None
    # The three caps under the LCD keys, in deck order (left, middle, right),
    # and the controls whose assignment is held down rather than tapped.
    buttons: tuple[str | None, ...] = (None, None, None)
    held: tuple[str, ...] = ()
    # Every dial, in the same order: its press, then each way it turns.  A turn is
    # a slot of its own because the deck reports one event per notch and the
    # direction it went, and nothing can be held on a turn.
    dials: tuple[str | None, ...] = (None,) * len(DIAL_SLOTS)
    # How long the deck may sit untouched before its keys go dark, in seconds.
    # Zero keeps them lit; the countdown is what spares the panel a fixed bright
    # picture, and it lives in [screen] because the editor owns that table whole.
    blank_after_s: int = DEFAULT_BLANK_AFTER_S


def _sample_draft() -> Draft:
    actions = (
        ActionDefinition("record", "keys", ("ctrl", "shift", "r")),
        ActionDefinition("marker", "keys", ("ctrl", "m")),
        ActionDefinition("dim", "brightness", brightness=35),
        ActionDefinition("studio", "menu", menu_name="studio"),
    )
    studio = MenuDefinition("studio", "Studio", (
        MenuItemDefinition(0, "Record", action_id="record"),
        MenuItemDefinition(1, "Marker", action_id="marker"),
        MenuItemDefinition(2, "Display", menu="display"),
        MenuItemDefinition(3, "Dim", action_id="dim"),
    ))
    display = MenuDefinition("display", "Display", (
        MenuItemDefinition(0, "Dim", action_id="dim"),
    ))
    return Draft(actions, ("record", "marker", "dim", "studio", None, None), (studio, display))


def _action_payload(action: ActionDefinition) -> dict[str, Any]:
    result: dict[str, Any] = {"id": action.action_id, "kind": action.action}
    if action.action == "keys":
        result["keys"] = list(action.keys)
    elif action.action == "brightness":
        result["brightness"] = action.brightness
    elif action.action == "omarchy_menu":
        result["route"] = action.omarchy_route
    else:
        result["menu"] = action.menu_name
    return result


def _menu_payload(menu: MenuDefinition) -> dict[str, Any]:
    """One menu as the browser sees it: a title and its buttons.

    A button that opens a menu carries ``menu`` (the menu's name); one that runs
    something carries ``action``.  The page concept is not part of this document.
    """
    return {"name": menu.name, "title": menu.title, "items": [
        {"key": item.key, "label": item.label,
         **({"icon": item.icon} if item.icon else {}),
         **({"action": item.action_id} if item.action_id is not None else {"menu": item.menu})}
        for item in menu.items
    ], "controls": [
        # The three buttons under the keys, in deck order.  None leaves that button
        # its ordinary deck job while this menu is open.
        None if claim is None else {"action": claim.action_id, "mode": claim.mode}
        for claim in menu.controls
    ], "dials": {
        # Only the parts of the dials this menu takes over.  A slot left out of here
        # keeps its ordinary job, so an empty object is a menu that claims no dials.
        slot: menu.dials[slot] for slot in MENU_DIAL_SLOTS if slot in menu.dials
    }}


def draft_payload(draft: Draft) -> dict[str, Any]:
    """Return the small, display-neutral document consumed by the browser."""
    return {
        "actions": [_action_payload(action) for action in draft.actions],
        "bindings": list(draft.bindings),
        "buttons": list(draft.buttons),
        "held": list(draft.held),
        "dials": list(draft.dials),
        "screen": draft.blank_after_s,
        "menus": [_menu_payload(menu) for menu in draft.menus],
        "icons": list(draft.icons) if draft.icons is not None else None,
    }


def _expect_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlPlaneEditorError(f"{name} must be an object")
    return value


def _parse_actions(raw: Any) -> list[ActionDefinition]:
    if not isinstance(raw, list):
        raise ControlPlaneEditorError("actions must be an array")
    actions: list[ActionDefinition] = []
    for number, value in enumerate(raw, 1):
        item = _expect_object(value, f"action {number}")
        kind = item.get("kind")
        allowed = {
            "keys": {"id", "kind", "keys"},
            "brightness": {"id", "kind", "brightness"},
            "omarchy_menu": {"id", "kind", "route"},
            "menu": {"id", "kind", "menu"},
        }
        if kind not in allowed or set(item) != allowed[kind]:
            raise ControlPlaneEditorError("actions may contain only safe keys, brightness, reviewed menus, or custom menus")
        action_id = item.get("id")
        if not isinstance(action_id, str):
            raise ControlPlaneEditorError("action IDs must be strings")
        if kind == "keys":
            keys = item.get("keys")
            if not isinstance(keys, list):
                raise ControlPlaneEditorError("keys actions require a keys array")
            actions.append(ActionDefinition(action_id, kind, tuple(keys)))
        elif kind == "brightness":
            actions.append(ActionDefinition(action_id, kind, brightness=item.get("brightness")))
        elif kind == "omarchy_menu":
            actions.append(ActionDefinition(action_id, kind, omarchy_route=item.get("route")))
        else:
            actions.append(ActionDefinition(action_id, kind, menu_name=item.get("menu")))
    validate_action_definitions(actions)
    return actions


def _parse_menu_controls(raw: Any, number: int) -> tuple[MenuControlDefinition | None, ...]:
    """Read what a menu claims of the three buttons under the keys.

    Saying nothing at all - which is what a client from before menus could claim
    them sends - leaves every one of them its ordinary deck job.
    """
    if raw is None:
        return (None, None, None)
    if not isinstance(raw, list) or len(raw) != len(BUTTON_CONTROLS):
        raise ControlPlaneEditorError(f"menu {number} needs one slot for each button under the LCD keys")
    slots: list[MenuControlDefinition | None] = []
    for slot_number, value in enumerate(raw, 1):
        if value is None:
            slots.append(None)
            continue
        slot = _expect_object(value, f"menu {number} button {slot_number}")
        if set(slot) != {"action", "mode"} or not isinstance(slot.get("action"), str):
            raise ControlPlaneEditorError("a claimed button needs an action and a mode")
        slots.append(MenuControlDefinition(slot["action"], slot["mode"]))
    return tuple(slots)


def _parse_menu_dials(raw: Any, number: int) -> dict[str, str]:
    """Read the parts of the dials a menu takes over while it is open.

    Saying nothing at all - which is what a client from before menus could claim a
    dial sends - leaves the dials exactly as they were: the big dial turns to move the
    menu's highlight and presses to select, and the right dial stays the deck's.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ControlPlaneEditorError(f"menu {number} dials must be an object")
    dials: dict[str, str] = {}
    for slot, value in raw.items():
        if slot not in MENU_DIAL_SLOTS:
            raise ControlPlaneEditorError(
                f"menu {number} cannot take over {slot!r}: only the big dial and the right dial are a menu's to claim"
            )
        if value is None:
            continue
        if not isinstance(value, str):
            raise ControlPlaneEditorError(f"menu {number} dial {slot!r} must name an action")
        dials[slot] = value
    return dials


def _parse_menus(raw: Any) -> list[MenuDefinition]:
    if not isinstance(raw, list):
        raise ControlPlaneEditorError("menus must be an array")
    menus: list[MenuDefinition] = []
    for number, value in enumerate(raw, 1):
        menu = _expect_object(value, f"menu {number}")
        if not {"name", "title", "items"} <= set(menu) or set(menu) - {"name", "title", "items", "controls", "dials"} or not isinstance(menu.get("items"), list):
            raise ControlPlaneEditorError("menus must have name, title, and items")
        items: list[MenuItemDefinition] = []
        for item_number, item_value in enumerate(menu["items"], 1):
            item = _expect_object(item_value, f"menu button {item_number}")
            fields = set(item) - {"icon"}
            if fields not in ({"key", "label", "action"}, {"key", "label", "menu"}):
                raise ControlPlaneEditorError("each menu button must either run an action or open one menu")
            key, label = item.get("key"), item.get("label")
            if isinstance(key, bool) or not isinstance(key, int) or not isinstance(label, str):
                raise ControlPlaneEditorError("menu button key and label must be an integer and string")
            target = item.get("action") if "action" in item else item.get("menu")
            if not isinstance(target, str):
                raise ControlPlaneEditorError("a menu button's action or menu must be a string")
            icon = item.get("icon")
            if icon is not None and (not isinstance(icon, str) or not MENU_ICON_REFERENCE.fullmatch(icon)):
                raise ControlPlaneEditorError("menu button icons must be library references such as pack:general/copy")
            items.append(MenuItemDefinition(key, label, item.get("action"), item.get("menu"), icon))
        name, title = menu.get("name"), menu.get("title")
        if not isinstance(name, str) or not isinstance(title, str):
            raise ControlPlaneEditorError("menu name and title must be strings")
        menus.append(MenuDefinition(name, title, tuple(items), _parse_menu_controls(menu.get("controls"), number),
                                    _parse_menu_dials(menu.get("dials"), number)))
    return menus


def _parse_buttons(raw: Any) -> tuple[str | None, ...]:
    """The three caps under the LCD keys, in deck order."""
    if raw is None:
        # A client from before the caps existed said nothing about them, which
        # means "leave them alone", exactly like absent artwork.
        return (None, None, None)
    if not isinstance(raw, list) or len(raw) != len(BUTTON_CONTROLS):
        raise ControlPlaneEditorError("buttons must be an array of three assignments")
    return tuple(raw)


def _parse_dials(raw: Any) -> tuple[str | None, ...]:
    """Every way each dial can be set, in the order the deck has them."""
    if not isinstance(raw, list) or len(raw) != len(DIAL_SLOTS):
        raise ControlPlaneEditorError(
            f"dials must be an array of {len(DIAL_SLOTS)} assignments: a press and two turns for the big and right dials, "
            "and two turns for the left, whose press is always the way back"
        )
    return tuple(raw)


def _parse_screen(raw: Any) -> int:
    """How long the deck may sit untouched before its keys go dark, in seconds.

    Zero is a real answer - it means the keys stay lit - so it is allowed here and
    written back, rather than being read as "unset" and quietly reset to a default.
    """
    if raw is None:
        return DEFAULT_BLANK_AFTER_S
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 86400:
        raise ControlPlaneEditorError(
            "screen must be 0 (never blank) or the number of seconds from 1 through 86400"
        )
    return raw


def _parse_held(raw: Any) -> tuple[str, ...]:
    """The controls whose assignment is held down rather than tapped."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(control, str) for control in raw):
        raise ControlPlaneEditorError("held must be an array of control names")
    return tuple(raw)


def parse_draft(payload: Any) -> Draft:
    """Parse the browser document through the existing safe editor models."""
    raw = _expect_object(payload, "draft")
    allowed = {"actions", "bindings", "buttons", "held", "dials", "menus", "icons", "screen"}
    if not {"actions", "bindings", "dials", "menus"} <= set(raw) or not set(raw) <= allowed:
        raise ControlPlaneEditorError("draft contains unsupported fields")
    actions = _parse_actions(raw["actions"])
    if not isinstance(raw["bindings"], list):
        raise ControlPlaneEditorError("bindings must be an array")
    bindings = tuple(raw["bindings"])
    buttons = _parse_buttons(raw.get("buttons"))
    held = _parse_held(raw.get("held"))
    dials = _parse_dials(raw["dials"])
    menus = _parse_menus(raw["menus"])
    assignments = dict(zip(LCD_CONTROLS, bindings)) | dict(zip(BUTTON_CONTROLS, buttons)) | dict(zip(DIAL_SLOTS, dials))
    validate_assignments(assignments, actions, list(held))
    validate_menu_definitions(menus, actions)
    icons: tuple[str | None, ...] | None = None
    if "icons" in raw and raw["icons"] is not None:
        try:
            icons = artwork.parse_assignments(raw["icons"])
        except artwork.ArtworkError as error:
            raise ControlPlaneEditorError(str(error)) from error
    return Draft(tuple(actions), bindings, tuple(menus), icons, buttons, held, dials,
                 blank_after_s=_parse_screen(raw.get("screen")))


def _id_text(part: str) -> str:
    """Letters and digits stay; everything else becomes a dash."""
    return "".join(character if character.isalnum() else "-" for character in part.lower()).strip("-")


def _adopted_action_id(chord: tuple[str, ...], held: bool, taken: set[str]) -> str:
    """Name an adopted chord so the file says what it is.

    ``keys-enter`` and ``hold-ctrl-shift-space`` read as well in the Actions
    list as in the config file.
    """
    stem = ("hold-" if held else "keys-") + "-".join(_id_text(part) for part in chord)
    stem = stem[:32].rstrip("-") or "keys-chord"
    candidate, index = stem, 2
    while candidate in taken:
        suffix = f"-{index}"
        candidate = stem[: 32 - len(suffix)] + suffix
        index += 1
    return candidate


def _adopted_assignment(binding: Any, actions: list[ActionDefinition], taken: set[str]) -> tuple[str, bool] | None:
    """Name a binding the editor did not write, so a save cannot lose it.

    A hand-written chord has no name to point at.  Where an action already holds
    the same chord, that name is reused; otherwise a new one is added.  Anything
    this model cannot express - shell, HTTP - is left alone for the merge guard.
    """
    if binding.action not in {"keys", "keys_hold"}:
        return None
    held = binding.action == "keys_hold" or binding.mode == "hold"
    chord = tuple(binding.keys_hold if binding.action == "keys_hold" else binding.keys)
    for definition in actions:
        if definition.action == "keys" and definition.keys == chord:
            return definition.action_id, held
    action_id = _adopted_action_id(chord, held, taken)
    actions.append(ActionDefinition(action_id, "keys", chord))
    taken.add(action_id)
    return action_id, held


def draft_from_config(path: Path) -> Draft:
    """Read only an explicitly chosen config and retain editor-safe definitions."""
    try:
        config = load_config(path)
    except ConfigError as error:
        raise ControlPlaneEditorError(str(error)) from error
    actions: list[ActionDefinition] = []
    for action_id, binding in config.actions.items():
        if binding.action == "keys":
            actions.append(ActionDefinition(action_id, "keys", binding.keys))
        elif binding.action == "brightness":
            actions.append(ActionDefinition(action_id, "brightness", brightness=binding.brightness))
        elif binding.action == "omarchy_menu":
            actions.append(ActionDefinition(action_id, "omarchy_menu", omarchy_route=binding.omarchy_route))
        elif binding.action == "menu":
            actions.append(ActionDefinition(action_id, "menu", menu_name=binding.menu_name))
    menus = menu_definitions_from_custom_menus(config.custom_menus or {})
    by_binding = {id(binding): action_id for action_id, binding in config.actions.items()}
    taken = {definition.action_id for definition in actions}
    assignments: dict[str, str | None] = {}
    held: list[str] = []
    for control in ASSIGNABLE_CONTROLS:
        assignments[control] = None
        binding = config.bindings.get(control)
        if binding is None:
            continue
        action_id = by_binding.get(id(binding))
        if action_id is not None:
            assignments[control] = action_id
            if binding.mode == "hold":
                held.append(control)
            continue
        adopted = _adopted_assignment(binding, actions, taken)
        if adopted is None:
            # Something this model cannot show.  Its table stays outside the
            # editor's reach, which is what the merge guard enforces.
            continue
        assignments[control] = adopted[0]
        if adopted[1]:
            held.append(control)
    bindings = tuple(assignments[control] for control in LCD_CONTROLS)
    buttons = tuple(assignments[control] for control in BUTTON_CONTROLS)
    dials = tuple(assignments[control] for control in DIAL_SLOTS)
    draft = Draft(tuple(actions), bindings, tuple(menus), artwork.assignments_from_config(config.images, path), buttons, tuple(held), dials,
                  # The deck's own countdown, read from the file: a config that sets
                  # it by hand is what the editor shows, so a save cannot quietly
                  # reset it to the default.
                  blank_after_s=config.screen.blank_after_s)
    # Existing parser is authoritative, and the UI model adds editor restrictions.
    validate_action_definitions(actions)
    validate_assignments(assignments, actions, held)
    validate_menu_definitions(menus, actions)
    return draft


class DraftStore:
    def __init__(self, draft: Draft) -> None:
        self._draft = draft
        self._lock = threading.RLock()

    def get(self) -> Draft:
        with self._lock:
            return self._draft

    def set(self, draft: Draft) -> Draft:
        with self._lock:
            self._draft = draft
        return draft

    def replace(self, payload: Any) -> Draft:
        draft = parse_draft(payload)
        with self._lock:
            self._draft = draft
        return draft


def _read_config_text(path: Path) -> str:
    """Read an existing config byte-for-byte, enforcing the daemon's invariants.

    A symlink, special file, or a file whose mode is not 0600 is refused rather
    than read: saving over it could widen permissions or follow a link, so the
    write path must never even start.
    """
    try:
        details = path.lstat()
    except OSError as error:
        raise ControlPlaneEditorError(f"cannot read config: {error}") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ControlPlaneEditorError("config must be a regular file, not a link or special file")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise ControlPlaneEditorError("config must have mode 0600")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ControlPlaneEditorError(f"cannot read config: {error}") from error


def merge_draft_into_config(config_text: str, draft: Draft) -> str:
    """Replace exactly the tables this editor owns; preserve every other byte.

    The editor models named safe ``[actions.*]``, the bindings of the six LCD
    keys, of the three caps under them, and of every dial's press and each way it
    turns, and ``[custom_menus.*]``.  It does *not* model ``[labels]``,
    ``[images]``, ``[feedback]``, ``[menu]``, or ``[daemon]``, so those are copied
    through byte-for-byte (see :func:`akp03.model.replace_control_plane_sections`).

    Two guards keep a save from silently erasing something it cannot display:
    a pre-existing binding that is neither an editable safe action nor an
    adoptable hand-written chord is refused rather than overwritten, and a new
    action ID that collides with a non-editable action is refused rather than
    shadowed.
    """
    try:
        raw = tomllib.loads(config_text) if config_text.strip() else {}
    except tomllib.TOMLDecodeError as error:
        raise ControlPlaneEditorError(f"invalid config: {error}") from error
    if not isinstance(raw, dict):
        raise ControlPlaneEditorError("config must be a TOML table")
    raw_actions = raw.get("actions") or {}
    raw_menus = raw.get("custom_menus") or {}
    raw_bindings = raw.get("bindings") or {}
    if not isinstance(raw_actions, dict) or not isinstance(raw_menus, dict) or not isinstance(raw_bindings, dict):
        raise ControlPlaneEditorError("config tables are malformed")
    editable = {
        action_id for action_id, definition in raw_actions.items()
        if isinstance(action_id, str) and isinstance(definition, dict)
        and definition.get("action") in SAFE_ACTION_TYPES
    }
    for control in ASSIGNABLE_CONTROLS:
        binding = raw_bindings.get(control)
        if binding is None:
            continue
        if not isinstance(binding, dict):
            raise ControlPlaneEditorError(f"binding {control} must be a table")
        if binding.get("action_id") in editable:
            continue
        # A hand-written chord is adopted under a name rather than erased, so it
        # survives the save; anything else stays out of the editor's reach.
        if binding.get("action_id") is None and binding.get("action") in ADOPTABLE_CHORD_ACTIONS:
            continue
        raise ControlPlaneEditorError(
            f"binding {control} is not an editable safe action; refusing to overwrite it"
        )
    new_ids = {definition.action_id for definition in draft.actions}
    collisions = new_ids & (set(raw_actions) - editable)
    if collisions:
        raise ControlPlaneEditorError(
            f"action IDs already belong to non-editable actions: {', '.join(sorted(collisions))}"
        )
    rendered = action_registry_toml(
        list(draft.actions), list(draft.bindings), list(draft.menus), tuple(draft.buttons), tuple(draft.held),
        dict(zip(DIAL_SLOTS, draft.dials)),
    )
    # Artwork only joins the merge when the draft actually staged some: a client
    # that never mentioned icons must not be able to blank a key's image.
    staged_icons = draft.icons is not None
    if staged_icons:
        rendered += artwork.render_images_table(draft.icons or ())
    menu_names = set(raw_menus) | {menu.name for menu in draft.menus}
    # The countdown travels with its own comment, so a saved file explains itself
    # the same way a hand-written one does.
    rendered += (
        "[screen]\n"
        "# How long the deck may sit untouched before its keys go dark.  Any key, button\n"
        "# or dial wakes it, and the deck comes back exactly as it was.  0 keeps them lit.\n"
        f"blank_after_s = {draft.blank_after_s}\n"
    )
    return replace_control_plane_sections(
        config_text, rendered, editable | new_ids, menu_names=menu_names,
        replace_labels=False, replace_images=staged_icons, dials=True, screen=True,
    )


def _atomic_install_config(path: Path, text: str) -> None:
    """Validate then atomically replace ``path``; leave nothing partial behind.

    The candidate is written to a same-directory temporary file, fsynced, and
    re-parsed with the daemon's own :func:`load_config` *before* the rename.  A
    rejected candidate never touches the real file, and the temporary is removed
    on every failure path.
    """
    directory = path.parent
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        try:
            load_config(temporary, _require_image_files=False)
        except ConfigError as error:
            raise ControlPlaneEditorError(f"configuration rejected: {error}") from error
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    # Persist the directory entry so the rename itself survives a crash.
    with contextlib.suppress(OSError):
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def save_draft_config(path: Path, draft: Draft) -> Path:
    """Merge ``draft`` over the on-disk config and write it atomically.

    Returns the saved path.  Raises :class:`ControlPlaneEditorError` (leaving the
    file untouched) on any validation, merge, or permission failure; an
    :class:`OSError` from the final rename is likewise raised with the original
    file intact.
    """
    candidate = merge_draft_into_config(_read_config_text(path), draft)
    _atomic_install_config(path, candidate)
    return path



def make_server(
    port: int = 8765,
    *,
    config: Path | None = None,
    feed: ControlFeed | None = None,
    apply: Callable[[], bool] | None = None,
    paint_status: Callable[[], str | None] | None = None,
    injection_status: Callable[[], dict[str, Any] | None] | None = None,
    request_permission: Callable[[], dict[str, Any]] | None = None,
    try_action: Callable[[str], dict[str, Any]] | None = None,
    screens: Callable[[], dict[str, Any]] | None = None,
    face: Callable[[int], bytes | None] | None = None,
) -> ThreadingHTTPServer:
    """Build a localhost-only server.  Callers own serve_forever/shutdown.

    ``feed`` is the read-only source of live deck state for ``/api/events``.  It
    defaults to a detached feed that reports no deck attached.

    ``GET /api/label/<key>.png?text=...`` renders the face a button with no
    artwork shows - the label the deck draws on the tile - so the editor can
    preview buttons the same way it mirrors the deck.

    ``screens`` and ``face`` are the daemon's own account of what the six keys
    are showing: the former describes them for ``GET /api/screens``, the latter
    returns one key's picture for ``GET /api/screen/<n>.png``.  They exist so the
    editor *mirrors* the panel instead of working out for itself what it thinks
    the deck should look like - two implementations would drift.

    ``config`` is the path the daemon loaded.  When it is given, ``GET
    /api/state`` shows the daemon's *current* configuration as the draft and
    ``POST /api/save`` merges the draft over that file, writes it atomically,
    and honours the result honestly.

    ``apply`` is the daemon's in-place rebind hook.  The save route calls it
    *after* a successful write so the running deck uses the new bindings without
    a restart; its boolean return is reported as ``applied``.  When ``apply`` is
    ``None`` (the standalone dev server) a save still writes the file but says
    plainly that no running daemon applied it.

    ``paint_status`` is the daemon's report of why the deck's own LCD keys may
    not show what was asked of them, or ``None`` when they do.  Writing the file
    and rebinding the keys say nothing about the panel, so when this is given the
    editor says plainly that a save did *not* reach the screens rather than
    letting "applied" read as "the deck was repainted".
    """
    store = DraftStore(draft_from_config(config) if config is not None else _sample_draft())
    events = feed if feed is not None else ControlFeed()
    assets = files(ASSET_PACKAGE).joinpath(ASSET_DIRECTORY)
    can_save = config is not None
    # Rendered label faces, keyed by (key, text).  The canvas asks for the same
    # faces on every render, and each one is a fresh PIL image otherwise.
    label_faces: dict[tuple[int, str], bytes] = {}

    def persistence_note() -> str:
        if config is None:
            return "Sample draft — no configuration file is attached, so it cannot be saved."
        if apply is not None:
            return f"Editing {config} — Save writes it through the daemon and applies it live."
        return f"Editing {config} — Save writes it to disk; no running daemon is attached to apply it."

    

    class Handler(BaseHTTPRequestHandler):
        server_version = "AKP03WebUI/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _asset(self, name: str) -> None:
            if not name or "/" in name or "\\" in name or name.startswith("."):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            target = assets.joinpath(name)
            if not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            # The editor is served from the live source tree while it is being
            # worked on: never let a browser cache a stale page or script, or a fix
            # appears not to have landed.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _events(self) -> None:
            """Stream live deck state and control events to a loopback SSE client.

            Read-only: it forwards frames from the injected feed and never issues
            device, shell, or persistence work.
            """
            subscription = events.subscribe()
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                while True:
                    try:
                        message = subscription.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(message.encode().encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # A disconnected editor must never disturb the server.
                pass
            finally:
                subscription.close()

        def _screens(self) -> None:
            """Describe what the six keys are showing, for the editor's mirror.

            Read-only.  When the daemon supplied its own description we ask it for
            a fresh one; otherwise we replay the last frame the feed published, so
            a page load is never blind between frames.
            """
            described = screens() if screens is not None else events.screens_snapshot()
            self._json(HTTPStatus.OK, {"screens": described, "source": "daemon" if screens is not None else "feed"})

        def _screen(self, path: str) -> None:
            """Serve one key's current face as a PNG, straight from the daemon."""
            name = path.removeprefix("/api/screen/")
            digits = name.removesuffix(".png")
            if not digits.isdigit() or not 0 <= int(digits) <= 5 or name not in {digits, digits + ".png"}:
                self._json(HTTPStatus.NOT_FOUND, {"error": "unknown key"})
                return
            if face is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "no deck attached"})
                return
            data = face(int(digits))
            if data is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "that key is blank"})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _label(self, path: str) -> None:
            """Serve the face a button shows when it has no picture: its label.

            The editor previews what the deck will actually draw, so a button with
            no artwork still shows you its words instead of a blank tile.  The
            renderer is the device's own (`akp03.faces`), so the preview and the
            glass can never disagree - and it needs no daemon, so the canvas works
            before anything is connected.
            """
            name = path.removeprefix("/api/label/")
            digits = name.removesuffix(".png")
            if name != f"{digits}.png" or not digits.isdigit() or not 0 <= int(digits) <= 5:
                self._json(HTTPStatus.NOT_FOUND, {"error": "unknown key"})
                return
            text = (parse_qs(urlparse(self.path).query).get("text") or [""])[0]
            # The same limit the config enforces on a label: a preview must never
            # show a face the deck would refuse to accept.
            if not text or len(text) > LABEL_MAX_LENGTH or any(character < " " for character in text):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"text must be 1 to {LABEL_MAX_LENGTH} printable characters"},
                )
                return
            key = (int(digits), text)
            data = label_faces.get(key)
            if data is None:
                data = label_png(Paint(key[0], text))
                if len(label_faces) >= 64:  # bounded: a handful of menus' worth
                    label_faces.clear()
                label_faces[key] = data
            self._png(data)

        def _icon(self, path: str) -> None:
            """Serve one library icon's tile, so the picker shows the real glyph."""
            name = path.removeprefix("/api/icons/")
            if not name.endswith(".png"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "unknown icon"})
                return
            try:
                data = artwork.tile_png(f"{artwork.PACK_PREFIX}{name[:-len('.png')]}")
            except (artwork.ArtworkError, OSError) as error:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            self._png(data)

        def _draft_diff(self) -> None:
            """What the staged draft would change on the deck, per key.

            Read-only: it compares the staged assignments with the files the keys
            actually have, so "on the deck" is never guessed at.
            """
            staged = store.get().icons
            body: dict[str, Any] = {
                "staged": list(staged) if staged is not None else None,
                "source": "daemon" if config is not None else "sample",
            }
            if staged is not None and config is not None:
                try:
                    states = artwork.diff_assignments(staged, config)
                except artwork.ArtworkError as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                body["artwork"] = states
                body["pending"] = sorted(name for name, state in states.items() if state not in {"same", "empty"})
                body["detail"] = artwork.describe_diff(states)
            self._json(HTTPStatus.OK, body)

        def _png(self, data: bytes) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/events":
                self._events()
            elif path == "/api/screens":
                self._screens()
            elif path == "/api/icons":
                self._json(HTTPStatus.OK, artwork.library_payload())
            elif path == "/api/actions/ready":
                self._json(HTTPStatus.OK, actions_library.catalogue())
            elif path.startswith("/api/icons/export"):
                # Before the catch-all below: /api/icons/<pack>/<name>.png matches
                # that prefix too, and it would take this route with it.
                self._icons_export()
            elif path.startswith("/api/icons/"):
                self._icon(path)
            elif path == "/api/backup":
                self._backup()
            elif path == "/api/draft/diff":
                self._draft_diff()
            elif path.startswith("/api/label/"):
                self._label(path)
            elif path.startswith("/api/screen/"):
                self._screen(path)
            elif path == "/api/build":
                self._json(HTTPStatus.OK, build_stamp())
            elif path == "/api/state":
                body = {
                    "draft": draft_payload(store.get()),
                    "persistence": persistence_note(),
                    "source": "daemon" if config is not None else "sample",
                    "config_path": str(config) if config is not None else None,
                    "can_save": can_save,
                }
                if injection_status is not None:
                    # Whether the daemon can actually send a shortcut is part of
                    # the editor's answer, not a detail the user has to discover
                    # by pressing a key that silently does nothing.
                    try:
                        body["injection"] = injection_status()
                    except Exception as error:  # noqa: BLE001 - report, never 500
                        body["injection"] = {"permitted": False, "reason": str(error)}
                if paint_status is not None:
                    # The [labels]/bindings a save writes are not the same thing
                    # as what the deck's LCD keys show, so report the screens
                    # separately instead of implying they were repainted.
                    reason = paint_status()
                    body["screens_updated"] = reason is None
                    body["paint_error"] = reason
                self._json(HTTPStatus.OK, body)
            elif path in {"/", "/index.html"}:
                self._asset("index.html")
            elif path.startswith("/assets/"):
                self._asset(path.removeprefix("/assets/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _read_payload(self, *, limit: int = 1_000_000) -> Any:
            """Parse a bounded JSON request body, raising ValueError on abuse.

            The bound is per route: an imported set arrives base64-encoded inside the body
            and is legitimately megabytes, while everything else in the editor is a small
            payload and keeps the tight default.
            """
            length = self.headers.get("Content-Length")
            if length is None or not 0 <= int(length) <= limit:
                raise ValueError("invalid request body")
            return json.loads(self.rfile.read(int(length)).decode("utf-8"))

        def _discard(self) -> None:
            """Throw the staged draft away and load what is on disk again.

            The store holds whatever was last staged, so re-reading it (as a plain
            /api/state does) would return the very changes the person is trying to
            drop.  Discarding must go back to the configuration file.
            """
            try:
                draft = draft_from_config(config) if config is not None else _sample_draft()
            except (ControlPlaneEditorError, OSError) as error:
                self._json(HTTPStatus.CONFLICT, {"error": f"could not re-read {config}: {error}"})
                return
            store.set(draft)
            self._json(HTTPStatus.OK, {
                "draft": draft_payload(draft),
                "persistence": "Reloaded the saved configuration; staged changes were dropped.",
            })

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/action/test":
                # Runs an action the configuration already declares, once. Bounded
                # by design: the editor cannot ask for arbitrary keys this way.
                if try_action is None:
                    self._json(HTTPStatus.OK, {"ok": False, "reason": "this daemon cannot run actions"})
                    return
                try:
                    payload = self._read_payload()
                    action_id = str((payload or {}).get("action", ""))
                except (ValueError, AttributeError) as error:
                    self._json(HTTPStatus.OK, {"ok": False, "reason": f"bad request: {error}"})
                    return
                try:
                    self._json(HTTPStatus.OK, try_action(action_id))
                except Exception as error:  # noqa: BLE001 - the editor shows the reason
                    self._json(HTTPStatus.OK, {"ok": False, "reason": str(error)})
                return
            if path == "/api/permission/request":
                if request_permission is None:
                    self._json(HTTPStatus.OK, {"supported": False, "granted": False,
                                               "detail": "this backend cannot ask macOS for permission"})
                    return
                try:
                    self._json(HTTPStatus.OK, {"supported": True, **request_permission()})
                except Exception as error:  # noqa: BLE001 - the editor shows the reason
                    self._json(HTTPStatus.OK, {"supported": True, "granted": False, "detail": str(error)})
                return
            if path == "/api/restore":
                self._restore()
                return
            if path == "/api/icons":
                # Managing your own icons always answers, with the fresh library on
                # success and with a reason the page can show when it refuses.
                self._icons_edit()
                return
            if path == "/api/draft":
                self._draft()
            elif path == "/api/draft/discard":
                self._discard()
            elif path == "/api/save":
                self._save()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _backup(self) -> None:
            """Hand back the whole setup as a file, and the pictures it needs.

            The configuration inside is the *merged* document - what ``Save to deck``
            would write - so a backup taken with unsaved edits still carries what the
            person can see in the editor.  Nothing is written anywhere to make one.
            """
            if config is None:
                self._json(HTTPStatus.CONFLICT, {
                    "ok": False,
                    "reason": "This editor is not attached to a daemon configuration, so there is nothing to back up.",
                })
                return
            try:
                merged = merge_draft_into_config(_read_config_text(config), store.get())
                blob = backup.build_backup(merged, load_config(config), config)
            except (ControlPlaneEditorError, ConfigError, OSError, backup.BackupError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(error)})
                return
            filename = f"akp03-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.akp03"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def _restore(self) -> None:
            """Read a backup into the draft, and put its pictures back.

            Order is deliberate.  The file is read and its configuration validated
            *before* a single picture is written, because a backup that restored a set
            and then failed to load would leave litter behind for no reason.  The
            configuration itself only lands on the deck when the person presses Save.
            """
            if not can_save or config is None:
                self._json(HTTPStatus.CONFLICT, {
                    "ok": False,
                    "reason": "This editor is not attached to a daemon configuration, so there is nothing to restore into.",
                })
                return
            try:
                blob = self._read_body(limit=backup.MAX_BACKUP_BYTES)
                read = backup.read_backup(blob)
            except (ValueError, backup.BackupError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(error) or "that file could not be read"})
                return
            with tempfile.TemporaryDirectory() as directory:
                candidate = Path(directory) / "config.toml"
                candidate.write_text(read.config_text, encoding="utf-8")
                candidate.chmod(0o600)
                try:
                    load_config(candidate)
                    draft = draft_from_config(candidate)
                except (ConfigError, ControlPlaneEditorError, OSError) as error:
                    self._json(HTTPStatus.BAD_REQUEST, {
                        "ok": False,
                        "reason": f"the backup's configuration does not load: {error}",
                    })
                    return
            try:
                pictures = backup.restore_pictures(read)
            except (iconpacks.IconLibraryError, OSError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {
                    "ok": False,
                    "reason": f"the backup's pictures could not be put back: {error}",
                })
                return
            staged = store.replace(draft_payload(draft))
            replaced = " Its pictures were put back in your sets." if pictures else " It carried no pictures of its own."
            self._json(HTTPStatus.OK, {
                "ok": True,
                "draft": draft_payload(staged),
                "pictures": len(pictures),
                "persistence": (
                    f"{read.describe()} Loaded into the draft — nothing on the deck changes until you press Save."
                    f"{replaced}"
                ),
            })

        def _read_body(self, *, limit: int = 1_000_000) -> bytes:
            """Read a bounded request body as raw bytes, for files rather than JSON."""
            length = self.headers.get("Content-Length")
            if length is None or not 0 <= int(length) <= limit:
                raise ValueError("that file is larger than the app will accept")
            return self.rfile.read(int(length))

        def _icons_export(self) -> None:
            """Hand back one set as a zip, ready to import somewhere else."""
            parsed = urlparse(self.path)
            wanted = (parse_qs(parsed.query).get("set") or [""])[0]
            # Both shapes work: ?set=<id>, and the id on the end of the path.
            suffix = parsed.path[len("/api/icons/export/"):] if parsed.path.startswith("/api/icons/export/") else ""
            name = unquote(suffix or wanted)
            try:
                if not name:
                    raise iconpacks.IconLibraryError("name a set to export")
                blob = iconpacks.export_pack_zip(name)
            except iconpacks.IconLibraryError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(error)})
                return
            filename = f"{name}.zip"
            body = blob
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _icons_edit(self) -> None:
            """Add, relabel, copy or delete icons of your own.

            The browser validates too, but every write is checked again here so the
            rule holds however the request arrives: a set that ships with the app is
            never written to, tiles must be the two sizes the deck paints, and an
            icon still used by a key or a menu button is not deleted out from under it.
            """
            try:
                payload = self._read_payload(limit=iconpacks.IMPORT_BODY_BYTES) or {}
                if not isinstance(payload, dict):
                    raise iconpacks.IconLibraryError("the request must be a table")
                op = str(payload.get("op") or "")
                set_id = str(payload.get("set") or "")
                if op == "add":
                    added = [self._add_one(set_id, raw, payload) for raw in (payload.get("icons") or [])]
                    if not added:
                        raise iconpacks.IconLibraryError("no icons were sent")
                    detail = ", ".join(f"“{icon.label}”" for icon in added)
                    self._json(HTTPStatus.OK, {
                        "ok": True, "added": [icon.reference for icon in added],
                        "library": artwork.library_payload(),
                        "detail": f"Added {detail} to “{set_id}”.",
                    })
                    return
                if op == "set-create":
                    pack = iconpacks.create_user_set(set_id, str(payload.get("set_name") or ""))
                    self._json(HTTPStatus.OK, {
                        "ok": True, "library": artwork.library_payload(),
                        "detail": f"Made the set “{pack.name}” — add its pictures whenever you like.",
                    })
                    return
                if op == "label":
                    icon = iconpacks.set_user_icon_label(set_id, str(payload.get("name") or ""),
                                                         str(payload.get("label") or ""))
                    self._json(HTTPStatus.OK, {"ok": True, "library": artwork.library_payload(),
                                               "detail": f"Renamed to “{icon.label}”."})
                    return
                if op == "copy":
                    source = str(payload.get("from") or "")
                    name = str(payload.get("name") or "")
                    icon = iconpacks.copy_icon_into_user(
                        source, name, set_id, set_name=str(payload.get("set_name") or "") or None)
                    self._json(HTTPStatus.OK, {"ok": True, "library": artwork.library_payload(),
                                               "detail": f"Copied “{icon.label}” into “{set_id}”."})
                    return
                if op == "set-delete":
                    pack = next((p for p in iconpacks.load_packs() if p.id == set_id), None)
                    if pack is not None:
                        used = self._icon_uses()
                        worn = [icon.label for icon in pack.icons if used.get(icon.reference, 0)]
                        if worn:
                            raise iconpacks.IconLibraryError(
                                "a key or menu button is wearing "
                                + ", ".join(f"“{label}”" for label in worn[:3])
                                + (", so this set stays for now" if len(worn) <= 3 else " and more, so this set stays for now"))
                    name = iconpacks.delete_user_pack(set_id)
                    self._json(HTTPStatus.OK, {"ok": True, "library": artwork.library_payload(),
                                               "detail": f"Deleted the set “{name}”."})
                    return
                if op == "import":
                    blob = payload.get("data")
                    if not isinstance(blob, str):
                        raise iconpacks.IconLibraryError("the archive did not arrive")
                    try:
                        raw = base64.b64decode(blob, validate=True)
                    except (ValueError, TypeError) as error:
                        raise iconpacks.IconLibraryError(
                            "the archive did not arrive as a file") from error
                    pack, notes = iconpacks.import_pack_zip(
                        raw, wanted_id=str(payload.get("set") or "") or None)
                    said = f"Imported “{pack.name}” as a set of its own, with {len(pack.icons)} icon"
                    said += "s." if len(pack.icons) != 1 else "."
                    if notes:
                        said = said + " " + " ".join(notes)
                    self._json(HTTPStatus.OK, {"ok": True, "library": artwork.library_payload(),
                                               "set": pack.id, "made": pack.name, "notes": notes,
                                               "detail": said})
                    return
                if op == "set-label":
                    pack = iconpacks.rename_user_pack(set_id, str(payload.get("name") or ""))
                    self._json(HTTPStatus.OK, {"ok": True, "library": artwork.library_payload(),
                                               "detail": f"Renamed the set to “{pack.name}”."})
                    return
                if op == "delete":
                    name = str(payload.get("name") or "")
                    reference = f"{artwork.PACK_PREFIX}{set_id}/{name}"
                    places = self._icon_uses().get(reference, 0)
                    if places:
                        raise iconpacks.IconLibraryError(
                            f"“{name}” is used by {places} key{'s' if places != 1 else ''} or menu "
                            f"button{'s' if places != 1 else ''} — give those another icon first")
                    iconpacks.remove_user_icon(set_id, name)
                    self._json(HTTPStatus.OK, {"ok": True, "library": artwork.library_payload(),
                                               "detail": f"Deleted “{name}”."})
                    return
                raise iconpacks.IconLibraryError(f"unknown change {op!r}")
            except iconpacks.IconLibraryError as error:
                self._json(HTTPStatus.OK, {"ok": False, "reason": str(error)})
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.OK, {"ok": False, "reason": f"bad request: {error}"})

        def _add_one(self, set_id: str, raw: object, payload: dict) -> iconpacks.Icon:
            if not isinstance(raw, dict):
                raise iconpacks.IconLibraryError("each icon must be a table")
            tiles: dict[int, bytes] = {}
            sent = raw.get("tiles")
            if not isinstance(sent, dict):
                raise iconpacks.IconLibraryError("each icon needs its two tiles")
            for size in iconpacks.TILE_SIZES:
                encoded = sent.get(str(size))
                if not isinstance(encoded, str):
                    raise iconpacks.IconLibraryError(f"each icon needs a {size}px tile")
                if len(encoded) > iconpacks.MAX_TILE_BYTES * 2:
                    raise iconpacks.IconLibraryError(f"the {size}px tile is far too large")
                try:
                    tiles[size] = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as error:
                    raise iconpacks.IconLibraryError(
                        f"the {size}px tile did not arrive as picture data") from error
            source = None
            original = raw.get("source")
            if isinstance(original, dict) and isinstance(original.get("data"), str):
                encoded = original["data"]
                if len(encoded) > iconpacks.MAX_SOURCE_BYTES * 2:
                    raise iconpacks.IconLibraryError("that picture is larger than the limit")
                source = (str(original.get("name") or "picture.png"), base64.b64decode(encoded, validate=True))
            return iconpacks.add_user_icon(
                set_id, name=str(raw.get("name") or "icon"), label=str(raw.get("label") or ""),
                tiles=tiles, hue=raw.get("hue") if isinstance(raw.get("hue"), str) else None,
                set_name=str(payload.get("set_name") or "") or None, source=source)

        def _icon_uses(self) -> dict[str, int]:
            """Every icon reference the deck is currently using, and how often.

            Read from what the editor is showing (the draft) as well as the saved
            config, so deleting is refused for an icon either one still points at.
            """
            uses: dict[str, int] = {}
            states = [draft_payload(store.get())]
            if config is not None:
                states.append(draft_payload(draft_from_config(config)))
            for state in states:
                for reference in (state.get("icons") or []):
                    if isinstance(reference, str):
                        uses[reference] = uses.get(reference, 0) + 1
                for menu in (state.get("menus") or []):
                    for page in (menu.get("pages") or []):
                        for item in (page.get("items") or []):
                            reference = item.get("icon") if isinstance(item, dict) else None
                            if isinstance(reference, str):
                                uses[reference] = uses.get(reference, 0) + 1
            return uses

        def _draft(self) -> None:
            """Validate a draft in memory only.  This never writes a file."""
            try:
                draft = store.replace(self._read_payload())
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, ControlPlaneEditorError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error) or "invalid draft"})
                return
            self._json(HTTPStatus.OK, {
                "draft": draft_payload(draft),
                "persistence": "Draft validated in memory only — not saved. Use Save to persist it.",
            })

        def _save(self) -> None:
            """Persist the draft through the service, then apply it in place.

            Order is deliberate: the draft is validated, then merged over the
            on-disk config and written atomically (both of which leave the file
            byte-identical on failure), and only then is the running daemon asked
            to rebind.  Any failure replies with a clear error and never claims a
            save that did not happen.
            """
            if not can_save or config is None:
                self._json(HTTPStatus.CONFLICT, {
                    "error": "This editor is not attached to a daemon configuration; there is nothing to save.",
                })
                return
            try:
                payload = self._read_payload()
                draft = parse_draft(payload)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, ControlPlaneEditorError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error) or "invalid draft"})
                return
            if draft.icons is not None:
                # The one part of a push that writes files, and the reason this is
                # a push at all: until now the assignment lived only in the draft.
                # Keys already wearing exactly these bytes are left untouched.
                try:
                    artwork.verify_pusheable(draft.icons, config)
                    written = artwork.materialise(draft.icons, config)
                except (artwork.ArtworkError, OSError) as error:
                    self._json(HTTPStatus.BAD_REQUEST, {
                        "error": f"artwork was not pushed: {error}",
                    })
                    return
            else:
                written = ()
            try:
                saved = save_draft_config(config, draft)
            except ControlPlaneEditorError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            except OSError as error:
                # The atomic write left the previous file in place.
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                    "error": f"could not write {config}: {error}",
                })
                return
            store.set(draft)
            applied: bool | None = None
            note = f"Saved to {saved}."
            if apply is not None:
                try:
                    applied = bool(apply())
                except Exception as error:  # the file is saved; the rebind failed
                    applied = False
                    note = f"Saved to {saved}, but the running daemon could not apply it: {error}"
                else:
                    note = (
                        f"Saved to {saved} and applied to the running deck."
                        if applied
                        else f"Saved to {saved}, but the running daemon did not apply it."
                    )
                    if applied and paint_status is not None:
                        # "Applied" is about the bindings, never a claim that the
                        # deck's LCD keys were repainted.
                        reason = paint_status()
                        if reason is not None:
                            note += f" The deck's LCD keys were not updated: {reason}."
            else:
                note = f"Saved to {saved}; no running daemon is attached to apply it."
            if written:
                note += f" {len(written)} icon{'s' if len(written) != 1 else ''} written to the deck."
            self._json(HTTPStatus.OK, {
                "draft": draft_payload(draft),
                "saved": str(saved),
                "path": str(saved),
                "applied": applied,
                "persistence": note,
                "icons_pushed": [path.name for path in written],
            })

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.draft_store = store  # type: ignore[attr-defined]
    server.feed = events  # type: ignore[attr-defined]
    return server


class EditorServer:
    """Own an in-process editor server on its own thread.

    The daemon hosts the editor in-process so ``/api/events`` can stream the
    daemon's *own* :class:`akp03.feed.ControlFeed`.  This wrapper starts the
    serving thread and stops it deterministically: :meth:`stop` shuts the
    listener down, closes it, and joins the thread so no thread is leaked.

    ``start``/``stop`` are each idempotent, and the serving thread is a daemon
    thread, so an abrupt daemon exit cannot hang on it.  This is a local
    convenience only: it adds no new endpoint and does not widen the
    loopback-only posture.
    """

    def __init__(self, server: ThreadingHTTPServer) -> None:
        self.server = server
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def start(self) -> "EditorServer":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self.server.serve_forever, name="akp03-editor", daemon=True
            )
            self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self.server.shutdown()
        self.server.server_close()
        thread.join(timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local AKP03 web editor draft server.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, help="Read this explicit config into the draft and save back to it; saving has no running daemon to apply it.")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be 0 through 65535")
    try:
        server = make_server(args.port, config=args.config)
    except (OSError, ControlPlaneEditorError) as error:
        parser.error(str(error))
    host, port = server.server_address[:2]
    print(f"AKP03 web editor draft: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def build_stamp() -> dict[str, str]:
    """What the editor is being served, so a person can say which build they have.

    Every page reports it in its own title.  When a change is reported as missing, the
    first question is which copy is on screen, and the title answers it without a
    screenshot, a console or a guess.
    """
    newest = 0.0
    assets = Path(__file__).resolve().parent / "webui_assets"
    if assets.is_dir():
        for path in assets.rglob("*"):
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
    when = datetime.fromtimestamp(newest).strftime("%d %b %H:%M") if newest else "unknown"
    return {"build": when, "assets": str(assets)}
