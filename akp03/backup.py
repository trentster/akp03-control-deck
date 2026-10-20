"""One file that carries a whole deck setup, and the pictures it needs, home again.

A backup is a zip laid out like this::

    manifest.json                 what wrote it, when, and what is inside
    config.toml                   the merged configuration, as saving would write it
    icons/<set>/<name>.60.png     the small tile the deck's menu keys are painted from
    icons/<set>/<name>.180.png    the large tile the LCD keys are painted from

Why only *some* pictures travel: a set that ships with the app is already inside the
app, so carrying it again would swell every backup for nothing.  A set you made or
imported is not - and a menu button pointing at one of those restores as a blank key
without its picture.  That is the whole reason these files are in here, and the reason
a backup carries only the pictures something actually points at.

The configuration is written as a *merged* document, not the raw file on disk, so a
backup taken before saving still contains what the person can see in the editor.  That
merge is the same one ``Save to deck`` uses; nothing here writes to the deck.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import artwork, iconpacks
from .config import Config

KIND = "akp03-backup"
VERSION = 1
CONFIG_NAME = "config.toml"
MANIFEST_NAME = "manifest.json"
ICON_PREFIX = "icons/"
# A backup is a person's own settings and pictures, not a data dump: anything larger
# than this is not one, and reading it would be the wrong thing to do.
MAX_BACKUP_BYTES = 64 * 1024 * 1024
TILE_SIZES = (60, 180)


class BackupError(Exception):
    """A backup that cannot be made, or cannot be trusted enough to restore."""


@dataclass(frozen=True)
class Backup:
    """What was read out of a backup file, before anything is written."""

    config_text: str
    icons: dict[str, bytes] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def icon_count(self) -> int:
        return len({Path(name).stem.split(".")[0] for name in self.icons})

    def describe(self) -> str:
        """One line a person can read, for the editor to show."""
        written = str(self.manifest.get("written") or "").replace("T", " ")[:16]
        pictures = self.icon_count
        tail = f", written {written}" if written else ""
        return (
            f"Read a backup with {pictures} picture{'' if pictures == 1 else 's'}{tail}."
        )


def _menu_items(config: Config) -> Iterable[Any]:
    for menu in (config.custom_menus or {}).values():
        for page in (menu.pages or {}).values():
            items = getattr(page, "items", None) or {}
            for item in (items.values() if isinstance(items, dict) else items):
                yield item


def used_icon_references(config: Config, config_path: Path) -> tuple[str, ...]:
    """Every picture this configuration points at, key faces and menu buttons alike.

    Key faces are not stored as references - they are painted files - so the reference
    is recovered from the bytes on the deck, by the same fingerprinting the diff uses.
    A face that no longer matches any library picture is skipped rather than guessed at.
    """
    names: set[str] = set()
    for reference in artwork.assignments_from_config(config.images, config_path):
        if reference:
            names.add(reference)
    for item in _menu_items(config):
        icon = getattr(item, "icon", None)
        if isinstance(icon, str) and icon.strip():
            names.add(icon.strip())
    return tuple(sorted(names))


def _user_set_for(reference: str) -> tuple[str, str] | None:
    """Split a reference into (set id, icon name), or None when the app already has it.

    ``pack:<set>/<name>`` is the only shape a configuration may hold; anything else is
    not a reference at all and is left out rather than written somewhere odd.
    """
    if not reference.startswith("pack:"):
        return None
    body = reference[len("pack:"):]
    set_id, _, name = body.partition("/")
    if not set_id or not name:
        return None
    if set_id in iconpacks.builtin_ids():
        # Ships inside the app: no need to carry the bytes.
        return None
    return set_id, name


def build_backup(
    config_text: str,
    config: Config,
    config_path: Path,
    *,
    app_version: str = "",
    now: datetime | None = None,
) -> bytes:
    """Return the zip bytes for this configuration and the pictures it needs."""
    stamps: list[dict[str, str]] = []
    pictures: dict[str, bytes] = {}
    for reference in used_icon_references(config, config_path):
        split = _user_set_for(reference)
        if split is None:
            continue
        set_id, name = split
        written = False
        for size in TILE_SIZES:
            try:
                data = artwork.tile_png(reference, size=size)
            except (iconpacks.IconLibraryError, artwork.ArtworkError):
                continue
            pictures[f"{ICON_PREFIX}{set_id}/{name}.{size}.png"] = data
            written = True
        if written:
            stamps.append({"reference": reference, "set": set_id, "name": name})

    written_at = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    manifest = {
        "kind": KIND,
        "version": VERSION,
        "written": written_at,
        "app": app_version,
        "icons": stamps,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n")
        archive.writestr(CONFIG_NAME, config_text)
        for name, data in sorted(pictures.items()):
            archive.writestr(name, data)
    return buffer.getvalue()


def read_backup(blob: bytes) -> Backup:
    """Read a backup file, trusting nothing in it that has not been checked.

    Every name is inspected before anything is written: a zip can hold ``../`` in a
    member name, and this is the one place in the app where somebody else's file is
    opened.  Anything that is not this app's own layout is refused with a reason.
    """
    if len(blob) > MAX_BACKUP_BYTES:
        raise BackupError("that file is too large to be a deck backup")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as error:
        raise BackupError("that is not a zip file, so it is not a backup") from error

    with archive:
        names = set(archive.namelist())
        if CONFIG_NAME not in names:
            raise BackupError(f"the backup has no {CONFIG_NAME} in it")
        try:
            config_text = archive.read(CONFIG_NAME).decode("utf-8")
        except UnicodeDecodeError as error:
            raise BackupError(f"the {CONFIG_NAME} in that backup is not readable text") from error
        manifest: dict[str, Any] = {}
        if MANIFEST_NAME in names:
            try:
                manifest = json.loads(archive.read(MANIFEST_NAME))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise BackupError("the backup's manifest is not readable") from error
            if not isinstance(manifest, dict) or manifest.get("kind") != KIND:
                raise BackupError("that file is a zip, but not a deck backup")
            version = manifest.get("version")
            if not isinstance(version, int) or version > VERSION:
                raise BackupError(
                    "that backup was written by a newer version of the app than this one"
                )

        icons: dict[str, bytes] = {}
        for name in sorted(names):
            if not name.startswith(ICON_PREFIX):
                continue
            relative = Path(name[len(ICON_PREFIX):])
            parts = relative.parts
            if len(parts) != 2 or relative.is_absolute() or ".." in parts:
                raise BackupError(f"the backup holds an unexpected path: {name}")
            set_id, filename = parts
            iconpacks._slug(set_id, "set")  # noqa: SLF001 - the library's own naming rule
            stem, _, suffix = filename.partition(".")
            if not stem or suffix.split(".")[0] not in {str(size) for size in TILE_SIZES}:
                continue
            if set_id in iconpacks.builtin_ids():
                raise BackupError(
                    f"the backup tries to replace the shipped set {set_id!r}, which is read-only"
                )
            icons[name] = archive.read(name)

    return Backup(config_text=config_text, icons=icons, manifest=manifest)


def restore_pictures(backup: Backup) -> tuple[Path, ...]:
    """Write the backup's pictures into the sets they came from, and say which.

    A picture that is already there with the same bytes is left alone, so restoring the
    same backup twice is quiet rather than a flurry of writes.
    """
    by_set: dict[str, dict[str, bytes]] = {}
    for name, data in backup.icons.items():
        set_id, _, filename = name[len(ICON_PREFIX):].partition("/")
        by_set.setdefault(set_id, {})[filename] = data

    written: list[Path] = []
    for set_id, files in sorted(by_set.items()):
        directory = iconpacks.user_set_directory(set_id)
        directory.mkdir(parents=True, exist_ok=True)
        for filename, data in sorted(files.items()):
            target = directory / filename
            if target.exists() and target.read_bytes() == data:
                continue
            target.write_bytes(data)
            written.append(target)
        _note_in_manifest(directory, set_id, files)
    return tuple(written)


def _note_in_manifest(directory: Path, set_id: str, files: dict[str, bytes]) -> None:
    """Make sure a restored set is listed, so the library offers it.

    The manifest is the library's own record of a set - its name, author and which
    pictures belong to it.  A restore that wrote tiles without touching it would leave
    pictures on disk that nothing ever shows.
    """
    names = {filename.split(".")[0] for filename in files if filename}
    try:
        manifest = iconpacks.read_manifest(set_id)
    except iconpacks.IconLibraryError:
        manifest = {}
    known = manifest.get("icons")
    if not isinstance(known, list):
        known = []
    existing = {str(entry.get("name")) for entry in known if isinstance(entry, dict)}
    for name in sorted(names - existing):
        known.append({"name": name, "label": name.replace("-", " ").title()})
    manifest.setdefault("id", set_id)
    manifest.setdefault("name", set_id.replace("-", " ").title())
    manifest.setdefault("author", "Restored from a backup")
    # The library insists on a licence; a set whose licence the backup did not carry is
    # honestly labelled rather than given somebody else's terms.
    manifest.setdefault("licence", "Unstated")
    manifest.setdefault("description", "")
    for field in ("id", "name", "author", "licence"):
        if not str(manifest.get(field) or "").strip():
            manifest[field] = {
                "id": set_id,
                "name": set_id.replace("-", " ").title(),
                "author": "Restored from a backup",
                "licence": "Unstated",
            }[field]
    manifest["icons"] = known
    iconpacks.write_manifest(set_id, manifest)
