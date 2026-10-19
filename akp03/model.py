"""The editor's data model, with no GUI toolkit attached.

The action and menu shapes, their validation, and the TOML they render live here
rather than inside :mod:`akp03.gui`, because the shipping Mac app runs the
**web** editor as its only configuration plane.  When these definitions lived in
the tkinter module, a machine without tkinter made the daemon report
``editor unavailable`` and quietly serve no editor at all - the whole
configuration surface disappearing behind an optional GUI dependency.

A menu is **one screen of up to six buttons**.  A button either runs an action or
opens another menu, so menus nest to any depth and the same menu can be reached
from several keys.  There is no page concept: the ``pages`` array survives in the
file format only because it shipped first, and everything written here has
exactly one page (id ``main``).

Nothing in this module may import tkinter (or anything that does): it is the
shared floor under the daemon and the web editor alike.  The Tk control panel
that used to live in ``akp03.gui`` is gone - the browser editor is the only
configuration plane - but that module still re-exports every name here, so
callers written against it keep working.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .config import MENU_ICON_REFERENCE, ConfigError, load_config
from .custom_menu import CONTENT_KEYS, MENU_DIAL_SLOTS, CustomMenu, CustomMenuItem
from .events import MEDIA_KEYS, turn_binding_name
from .omarchy_menu_catalogue import ALLOWED_OMARCHY_MENU_ROUTES


MENU_ITEM_COUNT = 6
LCD_CONTROLS = tuple(f"key{number}" for number in range(MENU_ITEM_COUNT))
# The three caps under the LCD keys, in the order they sit on the deck: left,
# middle, right.  The firmware's own names are not in that order, so this list
# is the one place the two orders are reconciled.
BUTTON_CONTROLS = ("button6", "button8", "button7")
# The three dials, in the order they sit under the keys: the big one, then the two
# small ones.  A dial presses and turns two ways, and a turn is not a press - the
# deck reports one event per notch and which way it went - so each way a dial turns
# is a slot of its own.  A turn carries no release, so nothing can be held on one.
DIAL_CONTROLS = ("top", "bottom_left", "bottom_right")
DIAL_TURN_WAYS = ("left", "right")
# The left dial's press is the one control that is not anybody's to set: while a menu
# is open it is always the way back a level, and a menu you cannot leave is a brick.
# It is left out of the slots below, so the editor neither offers it nor writes it -
# and a hand-written binding on it stays exactly as it is written.
RESERVED_CONTROLS = ("bottom_left",)
DIAL_SLOTS = tuple(
    slot
    for dial in DIAL_CONTROLS
    for slot in (
        ([] if dial in RESERVED_CONTROLS else [dial])
        + [turn_binding_name(dial, delta) for delta in (-1, 1)]
    )
)
# Everything a person can give an action to: the six LCD keys, the three caps, and
# the three dials with each way they turn.
ASSIGNABLE_CONTROLS = LCD_CONTROLS + BUTTON_CONTROLS + DIAL_SLOTS
SAFE_ACTION_TYPES = ("keys", "brightness", "omarchy_menu", "menu")
MENU_LEAF_ACTION_TYPES = {"keys", "brightness", "omarchy_menu"}
# The page id every editor-written menu uses.  Nothing in the interface shows it.
MAIN_PAGE_ID = "main"
_ACTION_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_TABLE_HEADER = re.compile(r"(?m)^\[[^\n]+\][ \t]*(?:#.*)?$")


class ControlPlaneEditorError(ValueError):
    """An invalid editor model or safe configuration-write failure."""


# Kept as an import-compatible name for callers of the old editor.
MenuEditorError = ControlPlaneEditorError


@dataclass(frozen=True)
class ActionDefinition:
    """One GUI-editable named action.

    ``keys`` is used only with ``action == 'keys'``, ``brightness`` only with
    ``action == 'brightness'``, and ``omarchy_route`` only with
    ``action == 'omarchy_menu'``. No shell, HTTP, or hold action is
    representable by this model.
    """

    action_id: str
    action: str
    keys: tuple[str, ...] = ()
    brightness: int | None = None
    omarchy_route: str | None = None
    menu_name: str | None = None


@dataclass(frozen=True)
class MenuItemDefinition:
    """One button on a menu's screen.

    Exactly one of ``action_id`` and ``menu`` is set: the button either runs a
    safe action or opens another menu.  ``icon`` is an optional library reference
    (``pack:general/copy``) that the deck draws instead of the label, so a menu
    can be all pictures.  It is carried here rather than derived so a round trip
    through the editor can never quietly delete an icon the user assigned.
    """

    key: int
    label: str
    action_id: str | None = None
    menu: str | None = None
    icon: str | None = None


@dataclass(frozen=True)
class MenuControlDefinition:
    """One of the three buttons under the LCD keys, for as long as a menu is open.

    No label and no icon: a button under the keys has no screen to put either on.
    ``mode`` is the same choice the deck page offers - a tap, or a chord held down
    until the button is let go.
    """

    action_id: str
    mode: str = "tap"


@dataclass(frozen=True)
class MenuDefinition:
    """One menu: a title and up to six buttons, and what the caps do while it is open."""

    name: str
    title: str
    items: tuple[MenuItemDefinition, ...]
    # The three buttons under the LCD keys, in deck order (left, middle, right).
    # None leaves that button its ordinary deck job while the menu is open, which is
    # what all three keep until a menu claims them.
    controls: tuple[MenuControlDefinition | None, ...] = (None, None, None)
    # The parts of the big dial and the right dial this menu takes over while it is
    # open, by slot name, each naming an action.  A part left out keeps its ordinary
    # job, so a menu that claims none of the big dial still turns to move its
    # highlight and presses to select.
    dials: dict[str, str] = field(default_factory=dict)


def validate_action_definitions(actions: list[ActionDefinition]) -> None:
    """Validate the small action language the GUI is permitted to write."""
    seen: set[str] = set()
    for definition in actions:
        if not isinstance(definition, ActionDefinition):
            raise ControlPlaneEditorError("actions must be ActionDefinition values")
        if not _ACTION_ID.fullmatch(definition.action_id):
            raise ControlPlaneEditorError("action IDs must match [a-z][a-z0-9_-]{0,63}")
        if definition.action_id in seen:
            raise ControlPlaneEditorError(f"action ID {definition.action_id!r} is duplicated")
        seen.add(definition.action_id)
        if definition.action not in SAFE_ACTION_TYPES:
            raise ControlPlaneEditorError("only keys and brightness, plus reviewed Omarchy menu actions, can be edited")
        if definition.action == "keys":
            if not definition.keys or not all(isinstance(key, str) and key for key in definition.keys):
                raise ControlPlaneEditorError(f"action {definition.action_id!r} keys must be a non-empty array of strings")
            if definition.brightness is not None or definition.omarchy_route is not None:
                raise ControlPlaneEditorError(f"action {definition.action_id!r} keys action cannot set brightness or an Omarchy route")
        elif definition.action == "brightness":
            if isinstance(definition.brightness, bool) or not isinstance(definition.brightness, int) or not 1 <= definition.brightness <= 100:
                raise ControlPlaneEditorError(f"action {definition.action_id!r} brightness must be 1-100")
            if definition.keys or definition.omarchy_route is not None:
                raise ControlPlaneEditorError(f"action {definition.action_id!r} brightness action cannot set keys or an Omarchy route")
        elif definition.action == "omarchy_menu":
            if definition.omarchy_route not in ALLOWED_OMARCHY_MENU_ROUTES:
                raise ControlPlaneEditorError(f"action {definition.action_id!r} must select a reviewed Omarchy menu route")
            if definition.keys or definition.brightness is not None:
                raise ControlPlaneEditorError(f"action {definition.action_id!r} Omarchy menu action cannot set keys or brightness")
        else:
            if not isinstance(definition.menu_name, str) or not _ACTION_ID.fullmatch(definition.menu_name):
                raise ControlPlaneEditorError(f"action {definition.action_id!r} must name a custom menu")
            if definition.keys or definition.brightness is not None or definition.omarchy_route is not None:
                raise ControlPlaneEditorError(f"action {definition.action_id!r} menu action cannot set keys, brightness, or an Omarchy route")


def validate_menu_definitions(menus: list[MenuDefinition], actions: list[ActionDefinition]) -> None:
    """Validate menus as screens of buttons; every LCD key is content, nothing reserved."""
    action_types = {action.action_id: action.action for action in actions}
    menu_names: set[str] = set()
    for menu in menus:
        if not _ACTION_ID.fullmatch(menu.name) or menu.name in menu_names:
            raise ControlPlaneEditorError("menu names must be unique IDs matching [a-z][a-z0-9_-]{0,63}")
        menu_names.add(menu.name)
    for menu in menus:
        if not isinstance(menu.title, str) or not 1 <= len(menu.title) <= 32 or not menu.title.strip():
            raise ControlPlaneEditorError(f"menu {menu.name!r} needs a title of 1 through 32 characters")
        # A menu may be created empty and filled in afterwards; the deck shows
        # blank keys until a button is given something to do.
        if len(menu.items) > len(CONTENT_KEYS):
            raise ControlPlaneEditorError(f"menus hold at most {len(CONTENT_KEYS)} buttons")
        controls: set[int] = set()
        for item in menu.items:
            if item.key not in CONTENT_KEYS or item.key in controls:
                raise ControlPlaneEditorError(f"menu {menu.name!r} cannot have duplicate or invalid buttons")
            controls.add(item.key)
            if not isinstance(item.label, str) or not 1 <= len(item.label) <= 32 or not item.label.strip():
                raise ControlPlaneEditorError("menu button labels must be 1 through 32 characters")
            if item.icon is not None and not MENU_ICON_REFERENCE.fullmatch(str(item.icon)):
                raise ControlPlaneEditorError("menu button icons must be library references such as pack:general/copy")
            if (item.action_id is None) == (item.menu is None):
                raise ControlPlaneEditorError("each menu button must either run an action or open one menu")
            if item.action_id is not None:
                # Two different troubles wear the same shape here: a button pointing at an
                # action the deck no longer holds, and a button pointing at one of a kind a
                # menu cannot use.  Said as one sentence, the first read as the second -
                # "can't run 'copy'" - which blamed a perfectly ordinary shortcut and sent
                # the reader looking at a button that was not the trouble at all.
                kind = action_types.get(item.action_id)
                if kind is None:
                    raise ControlPlaneEditorError(
                        f"“{menu.title}” button {item.key + 1} runs {item.action_id!r}, which "
                        "your deck no longer has — give that button something else, or put "
                        "the action back"
                    )
                if kind not in MENU_LEAF_ACTION_TYPES:
                    raise ControlPlaneEditorError(
                        f"“{menu.title}” button {item.key + 1} can't run {item.action_id!r}: "
                        "a menu button can only send a key combination, change the brightness, "
                        "or open another menu"
                    )
            if item.menu is not None and item.menu not in menu_names:
                raise ControlPlaneEditorError(f"menu button opens {item.menu!r}, which is not a menu")
        # What the menu does with the three buttons under the keys while it is open.
        # Only a keys action can be held: there is nothing else to keep down.
        if len(menu.controls) != len(BUTTON_CONTROLS):
            raise ControlPlaneEditorError("a menu must carry one slot for each button under the LCD keys")
        for control, claim in zip(BUTTON_CONTROLS, menu.controls):
            if claim is None:
                continue
            if action_types.get(claim.action_id) not in MENU_LEAF_ACTION_TYPES:
                raise ControlPlaneEditorError(
                    f"“{menu.title}” can't put {claim.action_id!r} on {control} while it is open: "
                    "that button can only send a key combination, change the brightness, or open another menu"
                )
            if claim.mode not in ("tap", "hold"):
                raise ControlPlaneEditorError(f"menu {menu.name!r} control {control} mode must be tap or hold")
            if claim.mode == "hold" and action_types.get(claim.action_id) != "keys":
                raise ControlPlaneEditorError(f"menu {menu.name!r} control {control} can only hold a keys action")
        # What the menu does with the big dial and the right dial while it is open.  A
        # turn cannot be held, so a claim is nothing but the action it runs.
        for slot, action_id in menu.dials.items():
            if slot not in MENU_DIAL_SLOTS:
                raise ControlPlaneEditorError(
                    f"menu {menu.name!r} cannot take over {slot!r}: only the big dial and the right dial are a menu's to claim"
                )
            if action_types.get(action_id) not in MENU_LEAF_ACTION_TYPES:
                raise ControlPlaneEditorError(
                    f"“{menu.title}” can't put {action_id!r} on its {slot} dial: a dial can only "
                    "send a key combination, change the brightness, or open another menu"
                )
    # Menus may nest, but a menu that contains itself can never be navigated out
    # of: the cycle is rejected wherever it starts.
    graph = {menu.name: [item.menu for item in menu.items if item.menu is not None] for menu in menus}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ControlPlaneEditorError("menus must not contain a cycle")
        if name not in visited:
            visiting.add(name)
            for destination in graph.get(name, ()):
                visit(destination)
            visiting.remove(name)
            visited.add(name)

    for name in graph:
        visit(name)
    for action in actions:
        if action.action == "menu" and action.menu_name not in menu_names:
            raise ControlPlaneEditorError(f"menu action {action.action_id!r} must reference an editor menu")


def menu_slots(menu: MenuDefinition) -> tuple[MenuItemDefinition | None, ...]:
    """Return the editable LCD content slots for a menu, Tk-free.

    All six keys appear here: a menu may fill any of them, and Back lives on the
    bottom-left knob rather than on a reserved LCD.
    """
    slots: list[MenuItemDefinition | None] = [None] * len(CONTENT_KEYS)
    for item in menu.items:
        if item.key not in CONTENT_KEYS or slots[item.key] is not None:
            raise ControlPlaneEditorError("menus cannot have duplicate or invalid buttons")
        slots[item.key] = item
    return tuple(slots)


def _control_slots(custom_menu: CustomMenu) -> tuple[MenuControlDefinition | None, ...]:
    """A menu's claims on the caps, in deck order, ready for the editor model."""
    claims = custom_menu.controls or {}
    slots: list[MenuControlDefinition | None] = []
    for control in BUTTON_CONTROLS:
        claim = claims.get(control)
        slots.append(None if claim is None else MenuControlDefinition(claim.action, claim.mode))
    return tuple(slots)


def menu_definitions_from_custom_menus(custom_menus: Mapping[str, CustomMenu]) -> list[MenuDefinition]:
    """Flatten loaded custom menus into the editor's one-screen model.

    A menu written by this editor has a single page and passes through untouched.
    A menu written when a menu *had* pages keeps every page: each becomes its own
    menu, the menu's own name going to its root page, and links between pages are
    rewritten to point at the new menus.  Nothing is dropped, and the next save
    writes the flat shape.  A menu's claims on the caps follow its own name, so a
    menu split this way keeps them on the screen a person lands on.
    """
    definitions: list[MenuDefinition] = []
    for name, custom_menu in custom_menus.items():
        pages = custom_menu.pages
        root_page = pages[custom_menu.root]
        claims = _control_slots(custom_menu)
        if len(pages) == 1:
            definitions.append(MenuDefinition(
                name, root_page.title,
                tuple(MenuItemDefinition(item.key, item.label, item.action, item.submenu, item.icon) for item in root_page.items),
                claims, dict(custom_menu.dials or {}),
            ))
            continue

        def flat_name(page_id: str) -> str:
            return name if page_id == custom_menu.root else f"{name}-{page_id}"

        def target(item: CustomMenuItem) -> str | None:
            if item.submenu is None:
                return None
            return flat_name(item.submenu) if item.submenu in pages else item.submenu

        definitions.extend(
            MenuDefinition(
                flat_name(page_id), page.title,
                tuple(MenuItemDefinition(item.key, item.label, item.action, target(item), item.icon) for item in page.items),
                claims if page_id == custom_menu.root else (None, None, None),
                dict(custom_menu.dials or {}) if page_id == custom_menu.root else {},
            )
            for page_id, page in pages.items()
        )
    return definitions


def custom_menu_definitions(path: Path) -> list[MenuDefinition]:
    """Load validated custom menus into the editor model without Tk."""
    try:
        loaded = load_config(path)
    except ConfigError as error:
        raise ControlPlaneEditorError(f"cannot load config: {error}") from error
    return menu_definitions_from_custom_menus(loaded.custom_menus or {})


def _validate_assignments(controls: tuple[str, ...], assignments: list[str | None], actions: list[ActionDefinition], count_message: str) -> None:
    if len(assignments) != len(controls):
        raise ControlPlaneEditorError(count_message)
    action_ids = {definition.action_id for definition in actions}
    for control, action_id in zip(controls, assignments):
        if action_id is None:
            continue
        if not isinstance(action_id, str) or action_id not in action_ids:
            raise ControlPlaneEditorError(f"binding {control} must name an editable safe action")
        if len(action_id) > 32:
            raise ControlPlaneEditorError(f"binding {control} action ID must contain at most 32 characters for its LCD label")


def validate_tile_bindings(bindings: list[str | None], actions: list[ActionDefinition]) -> None:
    """Require six optional bindings, each pointing at an editable safe action."""
    _validate_assignments(LCD_CONTROLS, bindings, actions, "deck must contain exactly six LCD tiles")






def validate_held_controls(held: list[str], assignments: Mapping[str, str | None], actions: list[ActionDefinition]) -> None:
    """A held control needs a keys action: there is nothing else to hold down."""
    by_id = {definition.action_id: definition for definition in actions}
    for control in held:
        if control not in ASSIGNABLE_CONTROLS:
            raise ControlPlaneEditorError(f"held control {control!r} is not an assignable control")
        if control not in DIAL_CONTROLS and control.endswith(tuple(f"_turn_{way}" for way in DIAL_TURN_WAYS)):
            # A notch is over the moment it happens: there is no release to wait for.
            raise ControlPlaneEditorError(f"held control {control} is a dial turn, and a turn cannot be held")
        action_id = assignments.get(control)
        if action_id is None:
            raise ControlPlaneEditorError(f"held control {control} has nothing assigned to hold")
        if by_id[action_id].action != "keys":
            raise ControlPlaneEditorError(f"held control {control} needs a keys action")
        if set(by_id[action_id].keys) & set(MEDIA_KEYS):
            # A media function is one event pair with nothing to hold: offering a
            # hold the deck cannot perform is a promise it breaks.
            raise ControlPlaneEditorError(f"held control {control} needs keys that can be held, not a media key")


def validate_assignments(assignments: Mapping[str, str | None], actions: list[ActionDefinition], held: list[str] | None = None) -> None:
    """Validate every assignable control at once, keys first, then caps, then dials."""
    for controls, message in (
        (LCD_CONTROLS, "deck must contain exactly six LCD tiles"),
        (BUTTON_CONTROLS, "deck must contain exactly three buttons under the LCD keys"),
        (DIAL_SLOTS, "each dial needs a slot for every part of it that can be set"),
    ):
        _validate_assignments(controls, [assignments.get(control) for control in controls], actions, message)
    validate_held_controls(list(held or []), assignments, actions)


def action_registry_toml(actions: list[ActionDefinition], bindings: list[str | None], menus: list[MenuDefinition] | None = None, buttons: tuple[str | None, ...] = (None, None, None), held: tuple[str, ...] = (), dials: Mapping[str, str | None] | None = None) -> str:
    """Render the GUI-owned TOML tables without enabling menu or feedback."""
    validate_action_definitions(actions)
    dial_assignments = {slot: (dials or {}).get(slot) for slot in DIAL_SLOTS}
    assignable = {**dict(zip(LCD_CONTROLS, bindings)), **dict(zip(BUTTON_CONTROLS, buttons)), **dial_assignments}
    validate_assignments(assignable, actions, list(held))
    validate_menu_definitions(menus or [], actions)
    lines: list[str] = []
    for definition in actions:
        lines.extend((f"[actions.{definition.action_id}]", f'action = "{definition.action}"'))
        if definition.action == "keys":
            lines.append("keys = " + json.dumps(list(definition.keys), ensure_ascii=False))
        elif definition.action == "brightness":
            lines.append(f"brightness = {definition.brightness}")
        elif definition.action == "omarchy_menu":
            lines.append(f"route = {json.dumps(definition.omarchy_route)}")
        else:
            lines.append(f"menu = {json.dumps(definition.menu_name)}")
        lines.append("")
    for control, action_id in (*zip(LCD_CONTROLS, bindings), *zip(BUTTON_CONTROLS, buttons), *dial_assignments.items()):
        if action_id is not None:
            lines.extend((f"[bindings.{control}]", f'action_id = "{action_id}"'))
            if control in held:
                # The assignment keeps the chord down while the control is held.
                lines.append('mode = "hold"')
            lines.append("")
    for menu in menus or []:
        # One screen per menu, written as the single page the file format expects.
        # A key that opens another menu writes that menu's name as its submenu.
        lines.extend((f"[custom_menus.{menu.name}]", f'root = "{MAIN_PAGE_ID}"', ""))
        items = []
        for item in menu.items:
            row = {"key": item.key, "label": item.label}
            row["action" if item.action_id is not None else "submenu"] = item.action_id if item.action_id is not None else item.menu
            if item.icon is not None:
                row["icon"] = item.icon
            items.append(row)
        item_toml = "[" + ", ".join("{ " + ", ".join(f"{key} = {json.dumps(value)}" for key, value in row.items()) + " }" for row in items) + "]"
        lines.extend((f"[[custom_menus.{menu.name}.pages]]", f'id = "{MAIN_PAGE_ID}"', f"title = {json.dumps(menu.title)}", "items = " + item_toml, ""))
        # What this menu claims of the three buttons under the keys, in deck order.
        # A claim left empty is not written at all, so the button keeps its own job.
        claims = [(control, claim) for control, claim in zip(BUTTON_CONTROLS, menu.controls) if claim is not None]
        if claims:
            lines.append(f"[custom_menus.{menu.name}.controls]")
            for control, claim in claims:
                if claim.mode == "hold":
                    lines.append(f'{control} = {{ action_id = "{claim.action_id}", mode = "hold" }}')
                else:
                    lines.append(f'{control} = "{claim.action_id}"')
            lines.append("")
        # What this menu takes of the big dial and the right dial while it is open, in
        # the order the panel reads them.  A part it does not name keeps its own job,
        # so a menu that claims none of the big dial still turns to move the highlight.
        dial_claims = [slot for slot in MENU_DIAL_SLOTS if menu.dials.get(slot) is not None]
        if dial_claims:
            lines.append(f"[custom_menus.{menu.name}.dials]")
            for slot in dial_claims:
                lines.append(f'{slot} = "{menu.dials[slot]}"')
            lines.append("")
    result = "\n".join(lines)
    if result:
        result += "\n"
    # This is a syntax check only. Full semantic validation happens after this
    # is merged with the real configuration, before the atomic replacement.
    tomllib.loads(result or "# no editor-owned tables\n")
    return result


def _table_name(header: re.Match[str]) -> str:
    return header.group(0).split("#", 1)[0].strip()


def replace_control_plane_sections(config_text: str, rendered: str, action_ids: set[str], *, menu_names: set[str] | None = None, replace_labels: bool = False, replace_images: bool = False, dials: bool = False, screen: bool = False) -> str:
    """Replace editor-owned tables, byte-preserving unrelated configuration.

    ``dials`` says whether this caller manages the dials.  The browser editor does,
    because it shows a slot for every press and every way a dial turns; a caller
    that has no dials in its model leaves every dial's table exactly as it is.

    ``screen`` says the same for ``[screen]``, which holds how long the deck may sit
    untouched before its keys go dark.  It is a table of its own rather than a line
    in ``[deck]`` so that the editor can own it whole: the deck's own settings stay
    out of the editor's hands and keep their comments.
    """
    targets = {f"[actions.{action_id}]" for action_id in action_ids}
    # Every control a person can assign: the six LCD keys, the three caps, and -
    # only for a caller that manages them - the dials.
    managed = ASSIGNABLE_CONTROLS if dials else tuple(control for control in ASSIGNABLE_CONTROLS if control not in DIAL_SLOTS)
    targets.update(f"[bindings.{control}]" for control in managed)
    for menu_name in menu_names or set():
        targets.update({f"[custom_menus.{menu_name}]", f"[[custom_menus.{menu_name}.pages]]",
                        f"[custom_menus.{menu_name}.controls]", f"[custom_menus.{menu_name}.dials]"})
    if replace_labels:
        targets.add("[labels]")
    if replace_images:
        targets.add("[images]")
    if screen:
        targets.add("[screen]")
    headers = list(_TABLE_HEADER.finditer(config_text))
    chunks: list[str] = []
    cursor = 0
    for index, header in enumerate(headers):
        if _table_name(header) not in targets:
            continue
        end = headers[index + 1].start() if index + 1 < len(headers) else len(config_text)
        chunks.append(config_text[cursor:header.start()])
        cursor = end
    chunks.append(config_text[cursor:])
    retained = "".join(chunks)
    if not rendered:
        return retained
    # Exactly one blank line between the configuration this editor keeps and the
    # tables it owns - however many times the file is saved.  The blank line that
    # already separated them comes back inside ``retained``, so appending another
    # one each time made the file grow a blank line per save (and made two saves of
    # identical content produce different bytes).  Collapsing them first is what
    # makes a save idempotent, and it tidies any file that has already drifted.
    kept = retained.rstrip("\n")
    return (kept + "\n\n" if kept else "") + rendered


def _action_ids(config_text: str) -> tuple[set[str], set[str]]:
    """Return all named IDs and the subset that this editor owns."""
    try:
        raw = tomllib.loads(config_text) if config_text else {}
    except tomllib.TOMLDecodeError as error:
        raise ControlPlaneEditorError(f"invalid config: {error}") from error
    actions = raw.get("actions", {})
    if not isinstance(actions, dict):
        return set(), set()
    all_ids = {action_id for action_id in actions if isinstance(action_id, str)}
    editable = {
        action_id for action_id, definition in actions.items()
        if isinstance(action_id, str) and isinstance(definition, dict)
        and definition.get("action") in SAFE_ACTION_TYPES
    }
    return all_ids, editable


def _editable_action_ids(config_text: str) -> set[str]:
    """Return existing action tables the GUI owns and may replace."""
    return _action_ids(config_text)[1]
