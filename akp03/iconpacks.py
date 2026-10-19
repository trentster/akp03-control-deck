"""Icon pack library: the deck's key artwork, browsable and assignable.

A pack is a directory holding a ``pack.json`` manifest plus transparent glyph
tiles at each size the deck needs.  Tiles are deliberately transparent: the same
glyph composites onto a black idle key or onto the blue selected tile at paint
time, so selection styling stays the daemon's business and a pack never has to
ship twice the artwork.

Built-in packs live beside this module.  User packs live in the config directory
so they sit next to the config that references them, and a user pack with the
same id replaces the built-in one.
"""
from __future__ import annotations

import io
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import shutil
import tempfile
import zipfile

from PIL import Image

from .images import to_jpeg

PACK_MANIFEST = "pack.json"
TILE_SIZES = (60, 180)
BUILTIN_PACKS = Path(__file__).resolve().parent / "icons"
USER_PACKS_ENV = "AKP03_ICONS_DIR"
DEFAULT_BACKGROUND = "black"
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
HUE = re.compile(r"^#[0-9a-f]{6}$")


class IconLibraryError(ValueError):
    """Raised when a pack on disk is missing, malformed, or unsafe to load."""


@dataclass(frozen=True)
class Icon:
    """One glyph, with the palette hue and the action it is meant to carry."""

    pack: str
    name: str
    label: str
    hue: str
    action: str | None
    tiles: dict[int, Path]
    # Which group the picture list files this under.  The four built-in groups are
    # the action picker's own headings; anything else - a pack that predates this,
    # or one someone made themselves - is their own art and belongs under "Your own".
    section: str = "own"

    @property
    def reference(self) -> str:
        """The ``pack/name`` string a config uses to point at this icon."""
        return f"{self.pack}/{self.name}"

    def tile(self, size: int = 60) -> Image.Image:
        """The transparent glyph tile at ``size`` pixels square."""
        path = self.tiles.get(size)
        if path is None:
            raise IconLibraryError(f"{self.reference} has no {size}px tile")
        with Image.open(path) as opened:
            return opened.convert("RGBA").copy()

    def face(self, size: int = 60, background: str = DEFAULT_BACKGROUND) -> Image.Image:
        """The glyph composited onto a key face of ``background``."""
        tile = self.tile(size)
        face = Image.new("RGB", tile.size, background)
        face.paste(tile, (0, 0), tile)
        return face

    def jpeg(self, background: str = DEFAULT_BACKGROUND, *, quality: int = 90) -> bytes:
        """Deck-ready JPEG bytes for this icon on a key face."""
        return to_jpeg(self.face(background=background), quality=quality)


@dataclass(frozen=True)
class Pack:
    """A loaded pack: its manifest facts plus its icons."""

    id: str
    name: str
    description: str
    licence: str
    author: str
    directory: Path
    icons: tuple[Icon, ...]

    def find(self, name: str) -> Icon:
        for icon in self.icons:
            if icon.name == name:
                return icon
        raise IconLibraryError(f"pack {self.id!r} has no icon named {name!r}")

    def hues(self) -> tuple[str, ...]:
        return tuple(icon.hue for icon in self.icons)


def user_packs_dir() -> Path:
    """Where user-supplied packs live: beside the config, if it is in the default place."""
    override = os.environ.get(USER_PACKS_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "akp03" / "icons"


def _string(raw: dict, field: str, where: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise IconLibraryError(f"{where}: {field} must be a non-empty string")
    return value


def _icon_entry(raw: object, pack_id: str, directory: Path, index: int) -> Icon:
    where = f"pack {pack_id!r} icon {index}"
    if not isinstance(raw, dict):
        raise IconLibraryError(f"{where}: entry must be a table")
    name = _string(raw, "name", where)
    if not SLUG.match(name):
        raise IconLibraryError(f"{where}: name {name!r} must be lowercase letters, digits and hyphens")
    label = _string(raw, "label", where)
    # hue is a hint, not a fact: the sets that ship with the app do not all carry one,
    # so a pack that leaves it out is fine.  A hue that *is* there must be a colour.
    raw_hue = raw.get("hue")
    hue = None if raw_hue in (None, "") else _string(raw, "hue", where)
    if hue is not None and not HUE.match(hue):
        raise IconLibraryError(f"{where}: hue {hue!r} must look like #rrggbb")
    action = raw.get("action")
    if action is not None and (not isinstance(action, str) or not action.strip()):
        raise IconLibraryError(f"{where}: action must be a non-empty string when present")
    section = raw.get("section", "own")
    if not isinstance(section, str) or not SLUG.match(section):
        raise IconLibraryError(f"{where}: section {section!r} must be lowercase letters, digits and hyphens")
    tiles: dict[int, Path] = {}
    for size in TILE_SIZES:
        path = directory / f"{name}.{size}.png"
        try:
            details = path.lstat()
        except OSError as error:
            raise IconLibraryError(f"{where}: {path.name} is missing: {error}") from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise IconLibraryError(f"{where}: {path.name} must be a regular file")
        try:
            with Image.open(path) as opened:
                mode, actual = opened.mode, opened.size
        except OSError as error:
            raise IconLibraryError(f"{where}: {path.name} cannot be read: {error}") from error
        if actual != (size, size):
            raise IconLibraryError(f"{where}: {path.name} is {actual[0]}x{actual[1]}, expected {size}x{size}")
        if mode != "RGBA":
            raise IconLibraryError(f"{where}: {path.name} is {mode}, expected RGBA")
        tiles[size] = path
    return Icon(pack=pack_id, name=name, label=label, hue=hue, action=action, tiles=tiles, section=section)


def load_pack(directory: Path) -> Pack:
    """Load and validate one pack directory."""
    directory = Path(directory)
    manifest_path = directory / PACK_MANIFEST
    if not manifest_path.is_file():
        raise IconLibraryError(f"{directory}: no {PACK_MANIFEST}")
    try:
        raw = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise IconLibraryError(f"{directory}: {PACK_MANIFEST} unreadable: {error}") from error
    if not isinstance(raw, dict):
        raise IconLibraryError(f"{directory}: {PACK_MANIFEST} must contain a table")
    pack_id = _string(raw, "id", str(directory))
    if pack_id != directory.name:
        raise IconLibraryError(f"{directory}: id {pack_id!r} must match the directory name")
    if not SLUG.match(pack_id):
        raise IconLibraryError(f"{directory}: id {pack_id!r} must be lowercase letters, digits and hyphens")
    entries = raw.get("icons")
    if not isinstance(entries, list):
        raise IconLibraryError(f"pack {pack_id!r}: icons must be an array")
    # An empty set is allowed: a set of your own is a named container, and taking the
    # last icon out of it leaves the container standing.  The shipped packs each carry
    # their icons, and the packaging tests check what they contain.
    icons = tuple(_icon_entry(entry, pack_id, directory, index) for index, entry in enumerate(entries))
    names = [icon.name for icon in icons]
    if len(set(names)) != len(names):
        raise IconLibraryError(f"pack {pack_id!r}: icon names must be unique")
    return Pack(
        id=pack_id,
        name=_string(raw, "name", f"pack {pack_id!r}"),
        description=str(raw.get("description", "")),
        licence=_string(raw, "licence", f"pack {pack_id!r}"),
        author=_string(raw, "author", f"pack {pack_id!r}"),
        directory=directory,
        icons=icons,
    )


def pack_directories(*, include_user: bool = True, extra_dirs: Sequence[Path] = ()) -> tuple[Path, ...]:
    """Pack directories in load order: built-ins, then user and extra packs."""
    directories: list[Path] = []
    if BUILTIN_PACKS.is_dir():
        directories.extend(sorted(entry for entry in BUILTIN_PACKS.iterdir() if entry.is_dir()))
    if include_user:
        user = user_packs_dir()
        if user.is_dir():
            directories.extend(sorted(entry for entry in user.iterdir() if entry.is_dir()))
    for extra in extra_dirs:
        extra = Path(extra).expanduser()
        if extra.is_dir():
            directories.extend(sorted(entry for entry in extra.iterdir() if entry.is_dir()))
    return tuple(directories)


_PACKS: dict[tuple, tuple[Pack, ...]] = {}


def _manifest_stamp(directory: Path) -> int:
    try:
        return directory.joinpath(PACK_MANIFEST).stat().st_mtime_ns
    except OSError:
        return 0


def load_packs(*, include_user: bool = True, extra_dirs: Sequence[Path] = ()) -> tuple[Pack, ...]:
    """Load every available pack. A later pack with a known id replaces the earlier one.

    The result is cached against what is on disk rather than against a clock: one stat
    per set decides whether the library has to be built again.  It matters because a
    library-sized set holds a thousand icons and reading it per request made every icon
    tile in the picker take seconds.  Anything that rewrites a set's manifest - icons
    added, a set made, renamed or deleted - moves the stamp, so a cached read can never
    outlive the disk it came from.
    """
    directories = pack_directories(include_user=include_user, extra_dirs=extra_dirs)
    signature = tuple((str(directory), _manifest_stamp(directory)) for directory in directories)
    cached = _PACKS.get(signature)
    if cached is not None:
        return cached
    ordered: dict[str, Pack] = {}
    for directory in directories:
        pack = load_pack(directory)
        ordered[pack.id] = pack
    packs = tuple(ordered.values())
    _PACKS.clear()  # one signature is the whole truth; a stale entry would only hold memory
    _PACKS[signature] = packs
    return packs


def iter_icons(packs: Sequence[Pack]) -> Iterator[tuple[str, Icon]]:
    """Every icon as ``(pack/name, icon)``, the form configs reference."""
    for pack in packs:
        for icon in pack.icons:
            yield icon.reference, icon


def resolve(reference: str, packs: Sequence[Pack] | None = None) -> Icon:
    """Look up one ``pack/name`` reference across the library."""
    library = tuple(packs) if packs is not None else load_packs()
    pack_id, _, name = reference.partition("/")
    if not pack_id or not name:
        raise IconLibraryError(f"{reference!r} must be written as pack/name")
    for pack in library:
        if pack.id == pack_id:
            return pack.find(name)
    raise IconLibraryError(f"no pack with id {pack_id!r}")


# ── writing the user's own sets ───────────────────────────────────────────────
#
# Everything below edits the user's directory only.  Built-in packs live beside this
# module and are never written to, so a shipped icon cannot be damaged or replaced;
# a set whose id matches a built-in is refused outright, because the loader lets a
# user pack shadow a built-in of the same name and that would silently swap artwork.

MAX_TILE_BYTES = 512 * 1024        # one 180px tile is a few KB; this is a wall
MAX_SOURCE_BYTES = 8 * 1024 * 1024  # the file as it was uploaded
MAX_ICONS_PER_SET = 2000            # a set you make by hand is small; an imported one is a library
MAX_SET_NAME_LENGTH = 40           # what fits the sets column without wrapping
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024   # a zip of icons is small; this is a wall

# A set arrives base64-encoded inside a JSON body, which is a third larger again than the
# archive itself.  The request bound therefore has to hold the archive's own limit with
# room to spare, or a set that import_pack_zip would happily accept is thrown out before
# it is ever looked at.
IMPORT_BODY_BYTES = MAX_ARCHIVE_BYTES * 4 // 3 + 64 * 1024
MAX_USER_SETS = 20
LABEL_LIMIT = 32


def _slug(value: str, what: str) -> str:
    if not isinstance(value, str) or not SLUG.match(value):
        raise IconLibraryError(f"{what} {value!r} must be lowercase letters, digits and hyphens")
    return value


def builtin_ids() -> tuple[str, ...]:
    if not BUILTIN_PACKS.is_dir():
        return ()
    return tuple(sorted(entry.name for entry in BUILTIN_PACKS.iterdir() if entry.is_dir()))


def user_set_directory(set_id: str) -> Path:
    """The directory for a user set, refusing built-in ids and anything outside it."""
    set_id = _slug(set_id, "a set name")
    if set_id in builtin_ids():
        raise IconLibraryError(f"“{set_id}” is a built-in set, so it cannot be written to")
    root = user_packs_dir()
    directory = root / set_id
    # The slug has no dots or slashes, but the check costs nothing and keeps a later
    # change to SLUG from turning into a path escape.
    if directory.parent != root:
        raise IconLibraryError(f"{set_id!r} does not name a directory in the icon folder")
    return directory


def read_manifest(set_id: str) -> dict:
    """A user set's manifest as plain data, or a fresh one if the set is new."""
    directory = user_set_directory(set_id)
    path = directory / PACK_MANIFEST
    if not path.is_file():
        return {"id": set_id, "name": set_id.replace("-", " ").title(), "licence": "my own",
                "author": "my own", "description": "", "icons": []}
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise IconLibraryError(f"{set_id}: {PACK_MANIFEST} must contain a table")
    raw.setdefault("icons", [])
    return raw


def write_manifest(set_id: str, manifest: dict) -> None:
    directory = user_set_directory(set_id)
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    path = directory / PACK_MANIFEST
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _check_tile(name: str, size: int, data: bytes) -> None:
    if len(data) > MAX_TILE_BYTES:
        raise IconLibraryError(f"{name}.{size}.png is larger than {MAX_TILE_BYTES // 1024} KB")
    try:
        with Image.open(io.BytesIO(data)) as opened:
            mode, actual = opened.mode, opened.size
    except OSError as error:
        raise IconLibraryError(f"{name}.{size}.png cannot be read as an image: {error}") from error
    if mode != "RGBA":
        raise IconLibraryError(f"{name}.{size}.png is {mode}, expected RGBA (transparent)")
    if actual != (size, size):
        raise IconLibraryError(f"{name}.{size}.png is {actual[0]}x{actual[1]}, expected {size}x{size}")


def unique_icon_name(manifest: dict, wanted: str) -> str:
    """A free name in this set: `star`, then `star-2`, and so on."""
    taken = {entry.get("name") for entry in manifest.get("icons", []) if isinstance(entry, dict)}
    if wanted not in taken:
        return wanted
    for suffix in range(2, 100):
        candidate = f"{wanted}-{suffix}"
        if candidate not in taken:
            return candidate
    raise IconLibraryError(f"{wanted} is used too many times in this set")


def add_user_icon(set_id: str, *, name: str, label: str, tiles: dict[int, bytes], hue: str | None = None,
                  set_name: str | None = None, source: tuple[str, bytes] | None = None) -> Icon:
    """Write one icon into a user set, creating the set when it is new."""
    set_id = _slug(set_id, "a set name")
    directory = user_set_directory(set_id)
    if not directory.is_dir() and len(user_set_directories()) >= MAX_USER_SETS:
        raise IconLibraryError(f"that would be more than {MAX_USER_SETS} sets of your own")
    manifest = read_manifest(set_id)
    if len(manifest.get("icons", [])) >= MAX_ICONS_PER_SET:
        raise IconLibraryError(f"“{set_id}” already holds {MAX_ICONS_PER_SET} icons")
    for size in TILE_SIZES:
        if size not in tiles:
            raise IconLibraryError(f"a {size}px tile is required")
        _check_tile(name, size, tiles[size])
    if source is not None:
        filename, data = source
        if len(data) > MAX_SOURCE_BYTES:
            raise IconLibraryError(f"the file as uploaded is larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MB")
        extension = Path(filename).suffix.lower()
        if extension not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"):
            raise IconLibraryError(f"{extension or 'that file'} is not a picture the deck can use")
    final_name = unique_icon_name(manifest, name)
    label = (label or final_name).strip()[:LABEL_LIMIT] or final_name
    if set_name and not manifest.get("icons"):
        manifest["name"] = str(set_name).strip()[:LABEL_LIMIT] or manifest.get("name") or set_id
    directory.mkdir(parents=True, exist_ok=True)
    for size in TILE_SIZES:
        tile = directory / f"{final_name}.{size}.png"
        tile.write_bytes(tiles[size])
        os.chmod(tile, 0o600)
    if source is not None:
        original = directory / f"{final_name}.src{Path(source[0]).suffix.lower()}"
        original.write_bytes(source[1])
        os.chmod(original, 0o600)
    entry = {"name": final_name, "label": label, "hue": hue or dominant_hue(tiles[60]), "section": "own"}
    manifest["icons"] = [*manifest.get("icons", []), entry]
    write_manifest(set_id, manifest)
    return load_pack(directory).find(final_name)


def create_user_set(set_id: str, name: str = "") -> Pack:
    """Make an empty set of your own, named and waiting for pictures.

    A set is a container, so its name is worth having before its contents: someone
    who is about to gather pictures wants the set to put them in.  The loader already
    allows an empty set - taking the last icon out of one leaves the container
    standing - so nothing new is needed to hold it; this is the way to arrive at one
    directly instead of by emptying another.
    """
    set_id = _slug(set_id, "a set name")
    directory = user_set_directory(set_id)  # refuses a built-in id
    if (directory / PACK_MANIFEST).is_file():
        raise IconLibraryError(f"“{set_id}” already exists — open it, or pick another name")
    if len(user_set_directories()) >= MAX_USER_SETS:
        raise IconLibraryError(f"that would be more than {MAX_USER_SETS} sets of your own")
    manifest = read_manifest(set_id)
    manifest["name"] = (name or "").strip()[:LABEL_LIMIT] or manifest["name"]
    write_manifest(set_id, manifest)
    return load_pack(directory)


def dominant_hue(tile_png: bytes) -> str:
    """A colour for the tile's background: the average of what is not transparent."""
    with Image.open(io.BytesIO(tile_png)) as opened:
        small = opened.convert("RGBA").resize((1, 1), Image.BOX)
        red, green, blue, alpha = small.getpixel((0, 0))
    if alpha < 24:
        return "#e8ddcf"          # nothing visible to sample: a warm neutral is honest
    return f"#{red:02x}{green:02x}{blue:02x}"


def user_set_directories() -> tuple[Path, ...]:
    root = user_packs_dir()
    if not root.is_dir():
        return ()
    return tuple(sorted(entry for entry in root.iterdir() if entry.is_dir()))


def set_user_icon_label(set_id: str, name: str, label: str) -> Icon:
    """Change an icon's label.  The name is what a config points at, so it never moves."""
    manifest = read_manifest(set_id)
    wanted = (label or "").strip()[:LABEL_LIMIT]
    if not wanted:
        raise IconLibraryError("a label cannot be empty")
    found = False
    for entry in manifest.get("icons", []):
        if isinstance(entry, dict) and entry.get("name") == name:
            entry["label"] = wanted
            found = True
    if not found:
        raise IconLibraryError(f"“{set_id}” has no icon called {name!r}")
    write_manifest(set_id, manifest)
    return load_pack(user_set_directory(set_id)).find(name)


def remove_user_icon(set_id: str, name: str) -> None:
    """Delete one icon: its tiles, its original upload and its manifest entry."""
    directory = user_set_directory(set_id)
    manifest = read_manifest(set_id)
    kept = [entry for entry in manifest.get("icons", [])
            if not (isinstance(entry, dict) and entry.get("name") == name)]
    if len(kept) == len(manifest.get("icons", [])):
        raise IconLibraryError(f"“{set_id}” has no icon called {name!r}")
    for path in list(directory.glob(f"{name}.*.png")) + list(directory.glob(f"{name}.src.*")):
        if path.is_file():
            path.unlink()
    # The set stays, empty or not: it is yours, it has a name, and an empty one is a
    # perfectly good thing to add to later.  Deleting a set is its own deliberate act.
    manifest["icons"] = kept
    write_manifest(set_id, manifest)


def delete_user_pack(set_id: str) -> str:
    """Delete one of your own sets and everything in it, returning the name it had.

    Deleting a *set* is deliberately separate from deleting an icon: emptying a set
    leaves it standing, and losing a set has to be asked for.  The caller checks
    first that no key or menu button is wearing anything inside it.
    """
    set_id = _slug(set_id, "a set name")
    if set_id in builtin_ids():
        raise IconLibraryError("a built-in set cannot be deleted")
    directory = user_set_directory(set_id)
    if not (directory / PACK_MANIFEST).is_file():
        raise IconLibraryError(f"there is no set of yours called {set_id!r}")
    name = read_manifest(set_id).get("name") or set_id
    for path in sorted(directory.iterdir()):
        if path.is_file():
            path.unlink()
    try:
        directory.rmdir()
    except OSError:
        pass
    return name


def copy_icon_into_user(from_pack_id: str, name: str, to_set_id: str, *,
                        set_name: str | None = None) -> Icon:
    """Copy one icon from any set into one of your own, so the copy is yours to change.

    The source may be a set that shipped with the app or one you imported: an icon you
    curated out of a library is exactly the thing worth gathering into a set of your own.
    The destination is the caller's decision, never guessed here: ``set_name`` is what
    names the set when it does not exist yet, and an existing set keeps the name it has.
    """
    source = None
    for pack in load_packs():
        if pack.id == from_pack_id:
            source = pack
            break
    if source is None:
        raise IconLibraryError(f"there is no set called {from_pack_id!r}")
    icon = source.find(name)
    tiles = {size: path.read_bytes() for size, path in icon.tiles.items()}
    if set_name is None:
        fresh = not (user_set_directory(to_set_id) / PACK_MANIFEST).is_file()
        set_name = f"{icon.label} and more" if fresh else read_manifest(to_set_id).get("name")
    return add_user_icon(to_set_id, name=name, label=icon.label, tiles=tiles, hue=icon.hue,
                         set_name=set_name)


def rename_user_pack(set_id: str, name: str) -> Pack:
    """Give one of your own sets a name.  Its folder, and every reference into it, stays.

    The name is a label, not an address: keys and menu buttons point at
    ``pack:<set>/<icon>``, so renaming a set never breaks what is already using it.
    """
    set_id = _slug(set_id, "a set name")
    if set_id in builtin_ids():
        raise IconLibraryError("a built-in set cannot be renamed")
    if not (user_set_directory(set_id) / PACK_MANIFEST).is_file():
        raise IconLibraryError(f"there is no set of yours called {set_id!r}")
    name = " ".join(str(name).split())
    if not name:
        raise IconLibraryError("a set wants a name")
    if len(name) > MAX_SET_NAME_LENGTH:
        raise IconLibraryError(f"a set name is at most {MAX_SET_NAME_LENGTH} characters")
    manifest = read_manifest(set_id)
    manifest["name"] = name
    write_manifest(set_id, manifest)
    for pack in load_packs():
        if pack.id == set_id:
            return pack
    raise IconLibraryError(f"the set {set_id!r} could not be read back")

# ── packing a set up, and taking one in ──────────────────────────────────────

# What a zip picked up on its way here tends to carry: resource forks, and the finder's
# notes.  None of it is an icon, so none of it counts toward the archive's shape.
ZIP_JUNK = ("__MACOSX/", "._", ".DS_Store")

def _set_directory(set_id: str) -> Path:
    """Where a set lives, whether it ships with the app or belongs to you."""
    return BUILTIN_PACKS / set_id if set_id in builtin_ids() else user_set_directory(set_id)


def export_pack_zip(set_id: str) -> bytes:
    """One set as a zip someone else can import: the manifest and both tiles per icon.

    The originals you uploaded are left out on purpose — a pack carries what the deck
    paints, not the working files behind it.
    """
    set_id = _slug(set_id, "a set name")
    directory = _set_directory(set_id)
    if not (directory / PACK_MANIFEST).is_file():
        raise IconLibraryError(f"there is no set called {set_id!r}")
    pack = load_pack(directory)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{pack.id}/{PACK_MANIFEST}", (directory / PACK_MANIFEST).read_text())
        for icon in pack.icons:
            for size, path in sorted(icon.tiles.items()):
                archive.writestr(f"{pack.id}/{icon.name}.{size}.png", path.read_bytes())
    return buffer.getvalue()


def tiles_from_picture(data: bytes, sizes: Sequence[int] = TILE_SIZES) -> dict[int, bytes]:
    """Make the deck's tiles from one picture: square, transparent, never stretched.

    A pack that ships only a single square PNG is easier to build than one that ships
    two of every icon, so the same promise the Add button makes holds here.
    """
    try:
        with Image.open(io.BytesIO(data)) as opened:
            picture = opened.convert("RGBA")
    except OSError as error:
        raise IconLibraryError(f"that picture cannot be read: {error}") from error
    if picture.width != picture.height:
        raise IconLibraryError(f"that picture is {picture.width}x{picture.height}: icons are square")
    if picture.width < max(sizes):
        raise IconLibraryError(f"that picture is {picture.width}px: {max(sizes)}px or larger, please")
    made: dict[int, bytes] = {}
    for size in sizes:
        tile = picture.resize((size, size), Image.LANCZOS)
        out = io.BytesIO()
        tile.save(out, format="PNG")
        made[size] = out.getvalue()
    return made


def import_pack_zip(data: bytes, *, wanted_id: str | None = None) -> tuple[Pack, list[str]]:
    """Take a zip in as a set of your own.  Returns the set, and anything worth saying.

    Everything is checked in a staging folder first: a pack that is missing a file is
    refused whole, so a half-imported set is never left behind.  The id of a set that
    ships with the app — or one you already have — is never taken from under it: the
    arrival is renamed `-2` instead, and the notes say so.
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        raise IconLibraryError(f"that archive is larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)}MB")
    notes: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise IconLibraryError(f"that file is not a zip archive: {error}") from error
    with archive:
        names = [name for name in archive.namelist()
                 if not name.startswith(ZIP_JUNK) and not any(part in name for part in (".DS_Store", "__MACOSX/"))]
        if sum(info.file_size for info in archive.infolist()) > MAX_ARCHIVE_BYTES:
            raise IconLibraryError("that archive unpacks to far more than it should")
        roots = {name.split("/")[0] for name in names if "/" in name}
        if len(roots) != 1:
            raise IconLibraryError(
                "the archive must hold one folder with pack.json inside it, and nothing else")
        folder = roots.pop()
        for name in names:
            parts = Path(name).parts
            if ".." in parts or name.startswith("/"):
                raise IconLibraryError(f"{name!r} reaches outside the archive")
        manifest_name = f"{folder}/{PACK_MANIFEST}"
        if manifest_name not in names:
            raise IconLibraryError(f"the folder {folder!r} has no {PACK_MANIFEST}")
        try:
            manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise IconLibraryError(f"{PACK_MANIFEST} could not be read: {error}") from error
        if not isinstance(manifest, dict):
            raise IconLibraryError(f"{PACK_MANIFEST} must contain a table")
        manifest["id"] = str(manifest.get("id") or folder)
        if manifest["id"] != folder:
            raise IconLibraryError(
                f"the id inside {PACK_MANIFEST} is {manifest['id']!r} but the folder is {folder!r}")
        for field in ("name", "author", "licence"):
            value = " ".join(str(manifest.get(field) or "").split())
            if not value:
                raise IconLibraryError(f"the pack has no {field} — the app shows it on the set's card")
            manifest[field] = value[:MAX_SET_NAME_LENGTH * 3]
        entries = manifest.get("icons")
        if not isinstance(entries, list):
            raise IconLibraryError("icons must be a list")
        if len(entries) > MAX_ICONS_PER_SET:
            raise IconLibraryError(f"that pack has more than {MAX_ICONS_PER_SET} icons")

        wanted = _slug(wanted_id, "a set name") if wanted_id else manifest["id"]
        taken = set(builtin_ids()) | {entry.name for entry in user_set_directories()}
        target = wanted
        suffix = 2
        while target in taken:
            target = f"{wanted}-{suffix}"
            suffix += 1
        if target != wanted:
            notes.append(f"“{wanted}” is already a set, so this one came in as “{target}”.")

        # The set arrived under one id and lands under another: the manifest has to say
        # the name it is actually filed under, or the loader refuses the lot.
        manifest["id"] = target
        staging = Path(tempfile.mkdtemp(prefix="akp03-pack-"))
        try:
            made = staging / target
            made.mkdir()
            (made / PACK_MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise IconLibraryError("each icon must be a table")
                icon_name = _slug(str(entry.get("name") or ""), "an icon name")
                entry["name"] = icon_name
                entry.setdefault("label", icon_name.replace("-", " ").title())
                tiles: dict[int, bytes] = {}
                for size in TILE_SIZES:
                    member = f"{folder}/{icon_name}.{size}.png"
                    if member in names:
                        tiles[size] = archive.read(member)
                if not tiles:
                    # A single square picture is enough; the tiles are made here.
                    for candidate in (f"{icon_name}.png", f"{icon_name}.src.png", f"{icon_name}.src.jpeg"):
                        member = f"{folder}/{candidate}"
                        if member in names:
                            tiles = tiles_from_picture(archive.read(member))
                            notes.append(f"“{entry['label']}” arrived as one picture; its tiles were made here.")
                            break
                    else:
                        raise IconLibraryError(f"{icon_name!r} has no PNG in the archive")
                elif set(tiles) != set(TILE_SIZES):
                    raise IconLibraryError(f"{icon_name!r} is missing one of its two tiles")
                for size, blob in tiles.items():
                    _check_tile(icon_name, size, blob)
                    (made / f"{icon_name}.{size}.png").write_bytes(blob)
            (made / PACK_MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            checked = load_pack(made)          # the same validation every other set gets
            destination = user_set_directory(target)
            if destination.exists():
                raise IconLibraryError(f"“{target}” is already there")
            if len(user_set_directories()) >= MAX_USER_SETS:
                raise IconLibraryError(f"that would be more than {MAX_USER_SETS} sets of your own")
            shutil.move(str(made), str(destination))
            for path in destination.iterdir():
                os.chmod(path, 0o600)
            return checked, notes
        finally:
            shutil.rmtree(staging, ignore_errors=True)

