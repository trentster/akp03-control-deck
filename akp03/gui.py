"""Small local Tk control panel for the AKP03 daemon.

The editor in this module is deliberately a control-plane editor, not a generic
command runner.  It can create only shortcut, brightness, and reviewed Omarchy
menu actions and can
bind those actions to the six LCD keys.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path


from .config import MENU_ICON_REFERENCE, ConfigError, default_config_path, load_config, managed_image_path
from .custom_menu import CONTENT_KEYS
from .images import to_jpeg
from .omarchy_menu_catalogue import ALLOWED_OMARCHY_MENU_ROUTES, OMARCHY_MENU_ROUTES, omarchy_menu_route
from .session import send_command
from .starter_sets import STARTER_SETS, ArtworkGalleryCategory, StarterTile, artwork_gallery_categories, starter_tile_path


# The control-plane config API.  The Tk panel that used to live here is gone: the
# browser editor is the configuration plane that ships.  What remains is the shared
# action and menu shapes, their validation, and the TOML they render - now defined in
# :mod:`akp03.model`, free of any GUI toolkit, and re-exported here so callers written
# against this module keep working unchanged.
from .model import (
    LCD_CONTROLS,
    MENU_ITEM_COUNT,
    MENU_LEAF_ACTION_TYPES,
    SAFE_ACTION_TYPES,
    ActionDefinition,
    ControlPlaneEditorError,
    MenuDefinition,
    MenuEditorError,
    MenuItemDefinition,
    _ACTION_ID,
    _action_ids,
    _editable_action_ids,
    _table_name,
    action_registry_toml,
    custom_menu_definitions,
    menu_slots,
    replace_control_plane_sections,
    validate_action_definitions,
    validate_menu_definitions,
    validate_tile_bindings,
)

# Tk exposes the platform Command/Super family through different keysyms, but
# the daemon configuration already uses ``cmd`` as its platform-neutral token.
_CHORD_MODIFIERS = ("ctrl", "alt", "shift", "cmd")
_MODIFIER_KEYSYMS = {
    "ctrl": "ctrl", "control": "ctrl",
    "control_l": "ctrl", "control_r": "ctrl",
    "alt": "alt", "option": "alt",
    "alt_l": "alt", "alt_r": "alt", "option_l": "alt", "option_r": "alt",
    "shift": "shift",
    "shift_l": "shift", "shift_r": "shift",
    "cmd": "cmd", "meta": "cmd", "super": "cmd", "hyper": "cmd",
    "meta_l": "cmd", "meta_r": "cmd", "super_l": "cmd", "super_r": "cmd",
    "hyper_l": "cmd", "hyper_r": "cmd", "command": "cmd",
}
_KEY_ALIASES = {
    "return": "enter", "kp_enter": "enter", "escape": "escape",
    "backspace": "backspace", "delete": "delete", "prior": "pageup",
    "next": "pagedown", "space": "space", "tab": "tab",
}


def label_table_toml(previous: str, bindings: list[str | None]) -> str:
    """Render labels for assigned tiles while retaining labels on unassigned tiles."""
    try:
        raw = tomllib.loads(previous) if previous else {}
    except tomllib.TOMLDecodeError as error:
        raise ControlPlaneEditorError(f"invalid config: {error}") from error
    existing = raw.get("labels", {})
    if not isinstance(existing, dict):
        raise ControlPlaneEditorError("labels must be a table")
    labels: dict[int, str] = {}
    for name, text in existing.items():
        if (
            not isinstance(name, str)
            or not name.startswith("key")
            or not name[3:].isdigit()
            or not 0 <= int(name[3:]) < MENU_ITEM_COUNT
            or not isinstance(text, str)
            or not 1 <= len(text) <= 32
        ):
            raise ControlPlaneEditorError("labels must use key0 through key5 string values of 1 through 32 characters")
        labels[int(name[3:])] = text
    for key, action_id in enumerate(bindings):
        if action_id is not None:
            labels[key] = action_id
    if not labels:
        return ""
    lines = ["[labels]"]
    lines.extend(f"key{key} = {json.dumps(text, ensure_ascii=False)}" for key, text in sorted(labels.items()))
    return "\n".join(lines) + "\n"


def image_table_toml(image_keys: set[int]) -> str:
    """Render the deliberately closed managed-image table."""
    if not image_keys:
        return ""
    if not all(isinstance(key, int) and 0 <= key < MENU_ITEM_COUNT for key in image_keys):
        raise ControlPlaneEditorError("images must target LCD keys 0 through 5")
    return "[images]\n" + "".join(f'key{key} = "key{key}.jpg"\n' for key in sorted(image_keys))


def _secure_assets_dir(path: Path) -> Path:
    directory = path.parent / "images"
    if directory.exists():
        details = directory.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise ControlPlaneEditorError("managed images directory must be a directory, not a link or special file")
        if stat.S_IMODE(details.st_mode) != 0o700:
            raise ControlPlaneEditorError("managed images directory must have mode 0700")
    else:
        directory.mkdir(mode=0o700, parents=True)
    return directory


def _atomic_write_config(path: Path, candidate: str) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(candidate)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        # Asset updates are staged separately, so validate configuration syntax
        # and schema here without requiring a not-yet-installed new JPEG.
        try:
            load_config(temporary, _require_image_files=False)
        except ConfigError as error:
            raise ControlPlaneEditorError(f"configuration rejected: {error}") from error
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_control_plane_config(path: Path, actions: list[ActionDefinition], bindings: list[str | None], *, menus: list[MenuDefinition] | None = None, image_updates: dict[int, Path | None] | None = None) -> None:
    """Atomically install validated safe editor tables using 0600 permissions."""
    menus = menus or []
    rendered = action_registry_toml(actions, bindings, menus)
    try:
        details = path.lstat()
    except FileNotFoundError:
        details, previous = None, ""
    except OSError as error:
        raise ControlPlaneEditorError(f"cannot inspect config: {error}") from error
    else:
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ControlPlaneEditorError("config must be a regular file, not a link or special file")
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise ControlPlaneEditorError("config must have mode 0600")
        try:
            previous = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ControlPlaneEditorError(f"cannot read config: {error}") from error
    all_old_ids, old_ids = _action_ids(previous)
    new_ids = {definition.action_id for definition in actions}
    opaque_collisions = new_ids & (all_old_ids - old_ids)
    if opaque_collisions:
        raise ControlPlaneEditorError(f"action IDs already belong to non-editable actions: {', '.join(sorted(opaque_collisions))}")
    # The editor must never silently erase a pre-existing inline or unsafe LCD
    # binding that it cannot faithfully display and edit.  Refuse the save until
    # that binding is handled by a future, explicitly safe editor feature.
    raw = tomllib.loads(previous) if previous else {}
    raw_menus = raw.get("custom_menus", {})
    old_menu_names = set(raw_menus) if isinstance(raw_menus, dict) else set()
    raw_bindings = raw.get("bindings", {})
    if isinstance(raw_bindings, dict):
        for control in LCD_CONTROLS:
            binding = raw_bindings.get(control)
            if binding is None:
                continue
            action_id = binding.get("action_id") if isinstance(binding, dict) else None
            if action_id not in old_ids:
                raise ControlPlaneEditorError(f"binding {control} is not a GUI-editable safe action and will not be overwritten")
    labels = label_table_toml(previous, bindings)
    existing_images = load_config(path).images if details is not None else {}
    image_keys = set(existing_images)
    updates = image_updates or {}
    if not all(isinstance(key, int) and 0 <= key < MENU_ITEM_COUNT and (value is None or isinstance(value, Path)) for key, value in updates.items()):
        raise ControlPlaneEditorError("image updates must target LCD keys 0 through 5")
    staged: dict[int, bytes] = {}
    for key, source in updates.items():
        if source is None:
            image_keys.discard(key)
        else:
            try:
                staged[key] = to_jpeg(source)
            except (OSError, ValueError) as error:
                raise ControlPlaneEditorError(f"cannot normalize image for key{key}: {error}") from error
            image_keys.add(key)
    images = image_table_toml(image_keys)
    old_ids.update(new_ids)
    candidate = replace_control_plane_sections(
        previous, rendered + labels + images, old_ids, menu_names=old_menu_names | {menu.name for menu in menus},
        replace_labels=bool(labels), replace_images=True,
    )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    assets = _secure_assets_dir(path) if staged else None
    replacements: list[tuple[int, Path, Path | None]] = []
    committed = False
    try:
        for key, data in staged.items():
            target = managed_image_path(path, key)
            try:
                target_details = target.lstat()
            except FileNotFoundError:
                target_details = None
            if target_details is not None and (stat.S_ISLNK(target_details.st_mode) or not stat.S_ISREG(target_details.st_mode)):
                raise ControlPlaneEditorError(f"managed image key{key} must be a regular file, not a link or special file")
            fd, name = tempfile.mkstemp(prefix=f".key{key}.", dir=assets)
            staged_path = Path(name)
            with os.fdopen(fd, "wb") as output:
                output.write(data); output.flush(); os.fsync(output.fileno())
            os.chmod(staged_path, 0o600)
            backup = None
            if target.exists():
                fd, name = tempfile.mkstemp(prefix=f".key{key}.backup.", dir=assets)
                backup = Path(name)
                with os.fdopen(fd, "wb") as output: output.write(target.read_bytes())
                os.chmod(backup, 0o600)
            replacements.append((key, staged_path, backup))
            os.replace(staged_path, target)
        _atomic_write_config(path, candidate)
        committed = True
    finally:
        if not committed:
            for key, _staged_path, backup in replacements:
                target = managed_image_path(path, key)
                if backup is None:
                    if target.exists() and target.is_file() and not target.is_symlink():
                        target.unlink()
                elif backup.exists():
                    os.replace(backup, target)
        for _key, staged_path, backup in replacements:
            if staged_path.exists(): staged_path.unlink()
            if backup is not None and backup.exists(): backup.unlink()
    for key, source in updates.items():
        if source is None:
            target = managed_image_path(path, key)
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise ControlPlaneEditorError(f"managed image key{key} must be a regular file, not a link or special file")
                target.unlink()


def load_control_plane(path: Path) -> tuple[list[ActionDefinition], list[str | None]]:
    """Load GUI-safe actions and six LCD bindings; leave unsafe actions opaque."""
    try:
        config = load_config(path)
    except ConfigError as error:
        raise ControlPlaneEditorError(f"cannot load config: {error}") from error
    actions = [
        ActionDefinition(action_id, binding.action, binding.keys, binding.brightness, binding.omarchy_route, binding.menu_name)
        for action_id, binding in config.actions.items()
        if binding.action in SAFE_ACTION_TYPES
    ]
    safe_ids = {definition.action_id for definition in actions}
    # Read raw action_id references so a key binding is shown only when it is a
    # safe named action. Inline, shell, HTTP, and hold bindings remain intact.
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_bindings = raw.get("bindings", {})
    bindings = [
        raw_bindings.get(control, {}).get("action_id") if isinstance(raw_bindings.get(control), dict) else None
        for control in LCD_CONTROLS
    ]
    bindings = [action_id if action_id in safe_ids else None for action_id in bindings]
    return actions, bindings


if __name__ == "__main__":
    raise SystemExit(main())
