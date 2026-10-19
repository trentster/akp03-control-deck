"""Icons chosen for keys: staged in the draft, materialised only when pushed.

The library in the editor and the artwork on the deck are different things.
Choosing an icon stages a *reference* - ``pack:general/copy`` for a library icon,
or ``file:key3.jpg`` for art a key already has - and nothing reaches the device
until the draft is pushed.  Everything here is pure except :func:`materialise`,
the single function allowed to write into the managed image directory.

The device only ever accepts the fixed name ``key<N>.jpg`` inside the config's own
``images`` directory (see :func:`akp03.config.managed_image_path` and the schema
check in :func:`akp03.config._images`), so a reference can name a library icon or
a key's own managed file and nothing else.  That is what keeps an assignment from
being a file path someone could aim anywhere.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence

from . import actions_library
from .config import managed_image_path
from .iconpacks import BUILTIN_PACKS, USER_PACKS_ENV, Icon, IconLibraryError, Pack, load_packs, resolve

KEY_COUNT = 6
PACK_PREFIX = "pack:"
FILE_PREFIX = "file:"
MANAGED_NAME = re.compile(r"^key[0-5]\.jpg$")


class ArtworkError(ValueError):
    """Raised when an icon assignment cannot be understood, or cannot be pushed."""


def library(packs: Sequence[Pack] | None = None) -> tuple[Pack, ...]:
    """Every pack available for assignment, built-ins and the user's own."""
    return tuple(packs) if packs is not None else load_packs()


def find_icon(reference: str, packs: Sequence[Pack] | None = None) -> Icon:
    """Resolve one ``pack:<id>/<name>`` reference, or explain why it fails."""
    wanted = reference[len(PACK_PREFIX):] if reference.startswith(PACK_PREFIX) else reference
    try:
        return resolve(wanted, library(packs))
    except IconLibraryError as error:
        raise ArtworkError(str(error)) from error


# A picture's group is the same heading the action picker uses, taken from the
# same list so the two rows cannot drift apart, plus one the actions have no
# equivalent for: the pictures someone brings themselves.

def library_payload(packs: Sequence[Pack] | None = None) -> dict[str, Any]:
    """The whole library in the small shape the editor's picker consumes.

    ``preview`` is a URL rather than image data: the picker shows the real tiles
    the deck would receive, fetched from the running editor, instead of a copy
    embedded in the page.  ``sections`` travels with the packs so the picker shows
    the headings in one agreed order instead of working them out itself.
    """
    builtin_root = BUILTIN_PACKS.resolve()
    packs = library(packs)
    # The sets that shipped with the app lead: General and macOS are the two everyone
    # has, they hold the ordinary deck glyphs, and they should be the first rows in the
    # picker rather than something to scroll past a library to reach.  Sets of the
    # person's own follow, in their own order.  "Your own" stays last as the net for any
    # picture whose pack names a group we do not know.
    own = [pack for pack in packs if pack.directory.resolve().parent != builtin_root]
    shipped = [pack for pack in packs if pack.directory.resolve().parent == builtin_root]
    # One heading per set, named as the set is named.  The picker used to head its
    # pictures by purpose alone (Editing, Windows & apps, ...), which left the base sets
    # unnamed and unselectable as sets -- the words "General" and "macOS" never appeared
    # anywhere in it.  The purpose of a picture is still useful, so it stays on the tile
    # as its section.
    ordered = shipped + own
    return {
        "sections": [{"id": pack.id, "name": pack.name} for pack in ordered],
        # The sets that shipped with the app, so the page can say which are read-only
        # without guessing from a list it would have to keep in step.
        "builtin": [pack.id for pack in packs if pack.directory.resolve().parent == builtin_root],
        "packs": [
        {
            "id": pack.id,
            "name": pack.name,
            "description": pack.description,
            "licence": pack.licence,
            "author": pack.author,
            "icons": [
                {
                    "reference": f"{PACK_PREFIX}{icon.reference}",
                    "name": icon.name,
                    "label": icon.label,
                    "hue": icon.hue,
                    "action": icon.action,
                    "section": pack.id,
                    "preview": f"/api/icons/{icon.reference}.png",
                }
                for icon in pack.icons
            ],
        }
        for pack in library(packs)
    ]}


def tile_png(reference: str, *, size: int = 60, packs: Sequence[Pack] | None = None) -> bytes:
    """PNG bytes of one library icon's transparent tile, for the picker."""
    icon = find_icon(reference, packs)
    buffer = BytesIO()
    icon.tile(size).save(buffer, format="PNG")
    return buffer.getvalue()


def assignments_from_config(images: dict[int, Path], config_path: Path) -> tuple[str | None, ...]:
    """What each key already has on the deck, as references the editor can show.

    A configured image is always the key's own managed file, but that name says
    nothing about *which* icon it is.  The file's bytes are matched against the
    library, so an icon this editor put there comes back as ``pack:general/cut``
    - which is what lets the editor warn when a key wears an icon drawn for a
    different action.  An unrecognised picture stays ``file:key<N>.jpg``: keep
    exactly what is there, and never a guess at what it might be.
    """
    assignments: list[str | None] = []
    for key in range(KEY_COUNT):
        asset = managed_image_path(config_path, key)
        if asset not in set(images.values()):
            assignments.append(None)
            continue
        known = known_icon(asset)
        assignments.append(f"{PACK_PREFIX}{known}" if known else f"{FILE_PREFIX}key{key}.jpg")
    return tuple(assignments)


def known_icon(asset: Path) -> str | None:
    """The library reference whose artwork this file holds exactly, if any."""
    try:
        digest = hashlib.sha256(asset.read_bytes()).digest()
    except OSError:
        return None
    return _fingerprints().get(digest)


_FINGERPRINTS: dict[str, dict[bytes, str]] = {}




def _fingerprints() -> dict[bytes, str]:
    """SHA-256 of every library icon's deck-ready bytes, built once per process."""
    key = f"{BUILTIN_PACKS}|{os.environ.get(USER_PACKS_ENV, '')}"
    index = _FINGERPRINTS.get(key)
    if index is None:
        index = {}
        for pack in library():
            for icon in pack.icons:
                try:
                    index.setdefault(hashlib.sha256(icon.jpeg()).digest(), icon.reference)
                except (ValueError, OSError):
                    continue  # an icon that cannot be encoded can never be on a key
        _FINGERPRINTS[key] = index
    return index


def check_reference(key: int, reference: str, packs: Sequence[Pack] | None = None) -> str:
    """Validate one reference for ``key`` and return it unchanged."""
    if not isinstance(reference, str):
        raise ArtworkError(f"key{key}: an icon must be a reference string or null")
    if reference.startswith(FILE_PREFIX):
        name = reference[len(FILE_PREFIX):]
        if name != f"key{key}.jpg":
            raise ArtworkError(f"key{key}: may only keep its own managed file, not {name!r}")
        return reference
    if reference.startswith(PACK_PREFIX):
        find_icon(reference, packs)
        return reference
    raise ArtworkError(f"key{key}: {reference!r} must be pack:<pack>/<name> or file:key{key}.jpg")


def parse_assignments(raw: Any, *, packs: Sequence[Pack] | None = None) -> tuple[str | None, ...]:
    """Validate the six assignments the editor sends, in key order."""
    if not isinstance(raw, list) or len(raw) != KEY_COUNT:
        raise ArtworkError(f"icon assignments must be an array of exactly {KEY_COUNT} entries")
    return tuple(
        None if entry is None else check_reference(key, entry, packs)
        for key, entry in enumerate(raw)
    )


def render_images_table(assignments: Sequence[str | None]) -> str:
    """The ``[images]`` rows for a pushed draft; empty when no key wears artwork.

    Every reference ends up as the key's own managed name, because that is the
    only thing the daemon's config schema accepts; a ``pack:`` icon is copied
    into that name by :func:`materialise` before this text is written.
    """
    rows = [f'key{key} = "key{key}.jpg"' for key, reference in enumerate(assignments) if reference]
    if not rows:
        return ""
    return "[images]\n" + "\n".join(rows) + "\n"


def verify_pusheable(assignments: Sequence[str | None], config_path: Path) -> None:
    """Refuse a push that would promise artwork the deck cannot show.

    A ``file:`` reference means "keep the file that is already there", so a
    missing one is an error rather than a silently blank key.
    """
    for key, reference in enumerate(assignments):
        if reference is None or not reference.startswith(FILE_PREFIX):
            continue
        asset = managed_image_path(config_path, key)
        if not asset.is_file():
            raise ArtworkError(
                f"key{key} keeps {asset.name}, but there is no such image on the deck; "
                f"choose a library icon for key{key} or clear it"
            )


def diff_assignments(assignments: Sequence[str | None], config_path: Path, *, packs: Sequence[Pack] | None = None) -> dict[str, str]:
    """Per key, what pushing this draft would do to the artwork on the deck.

    This is what makes "staged, not on the deck" a fact rather than a feeling: a
    library icon is compared as *bytes* with the file the key currently has, so
    "same" means the push would write nothing for that key.
    """
    states: dict[str, str] = {}
    for key, reference in enumerate(assignments):
        asset = managed_image_path(config_path, key)
        present = asset.is_file()
        if reference is None:
            state = "removes" if present else "empty"
        elif reference.startswith(FILE_PREFIX):
            state = "same" if present else "missing"
        else:
            icon = find_icon(reference, packs)
            if not present:
                state = "adds"
            else:
                try:
                    state = "same" if asset.read_bytes() == icon.jpeg() else "changes"
                except OSError:
                    state = "changes"
        states[f"key{key}"] = state
    return states


DIFF_WORDS = {
    "same": "already on the deck",
    "empty": "nothing staged and nothing on the deck",
    "adds": "new icon, not on the deck",
    "changes": "a different icon, not pushed",
    "removes": "the deck still wears an icon here",
    "missing": "there is no image on the deck to keep",
}


def describe_diff(states: dict[str, str]) -> dict[str, str]:
    """Plain words for each key's staging state, for the editor to print."""
    return {key: DIFF_WORDS.get(state, state) for key, state in states.items()}


def materialise(assignments: Sequence[str | None], config_path: Path, *, packs: Sequence[Pack] | None = None) -> tuple[Path, ...]:
    """Write the managed JPEG each library assignment refers to; return what changed.

    This is the only device-bound work in this module, and the editor calls it only
    on push.  A key whose file already holds exactly these bytes is left alone, so
    re-pushing an unchanged draft writes nothing.  Files are written through a
    same-directory temporary and renamed, so a failure never leaves half an image
    behind.
    """
    directory = managed_image_path(config_path, 0).parent
    written: list[Path] = []
    for key, reference in enumerate(assignments):
        if reference is None or not reference.startswith(PACK_PREFIX):
            continue
        icon = find_icon(reference, packs)
        asset = managed_image_path(config_path, key)
        try:
            payload = icon.jpeg()
        except ValueError as error:
            raise ArtworkError(f"key{key}: {icon.reference} cannot be encoded: {error}") from error
        if asset.is_file():
            try:
                if asset.read_bytes() == payload:
                    continue  # already on the deck: nothing to write
            except OSError:
                pass
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{asset.name}.", dir=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, asset)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        written.append(asset)
    return tuple(written)
