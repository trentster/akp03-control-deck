"""Image normalization and safe legacy reorientation for AKP03 LCD keys."""
from __future__ import annotations

import argparse
import os
import stat
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from os import PathLike
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageOps

KEY_SIZE = (60, 60)
MAX_JPEG_BYTES = 0xFFFF

# The panel presents whatever it receives turned a quarter-turn anticlockwise,
# so uploads are pre-rotated a quarter-turn clockwise. Confirmed by eye on the
# physical keys of the AKP03 (text otherwise renders sideways); the earlier
# 180-degree assumption came from the legacy artwork migration and was wrong.
# Change this one line if a future panel revision mounts differently.
DEVICE_TRANSFORM = Image.Transpose.ROTATE_270


def _open(source: Image.Image | bytes | str | PathLike[str]) -> Image.Image:
    if isinstance(source, Image.Image):
        return source.copy()
    if isinstance(source, bytes):
        return Image.open(BytesIO(source))
    return Image.open(Path(source))


def to_jpeg(source: Image.Image | bytes | str | PathLike[str], *, quality: int = 90) -> bytes:
    """Encode source orientation unchanged as a letterboxed 60×60 RGB JPEG.

    This is the sole orientation boundary: callers provide source-oriented art,
    and the bytes produced here are exactly what ``Deck.set_key_image`` uploads.
    """
    if not 1 <= quality <= 100:
        raise ValueError("quality must be from 1 through 100")
    with _open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail(KEY_SIZE, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", KEY_SIZE, "black")
        canvas.paste(image, ((60 - image.width) // 2, (60 - image.height) // 2))
        encoded = BytesIO()
        canvas.save(encoded, format="JPEG", quality=quality)
    result = encoded.getvalue()
    if len(result) > MAX_JPEG_BYTES:
        raise ValueError("encoded JPEG exceeds 65535 bytes")
    return result


def to_device_jpeg(source: Image.Image | bytes | str | PathLike[str], *, quality: int = 90) -> bytes:
    """Encode an image for the AKP03 panel's physical mounting.

    The panel presents what it receives turned a quarter-turn anticlockwise, so
    this applies the fixed quarter-turn-clockwise hardware transform.  Keep
    managed artwork in ordinary source orientation and apply the transform only
    at the upload/preview boundary (see DEVICE_TRANSFORM).
    """
    with _open(source) as opened:
        upright = ImageOps.exif_transpose(opened).convert("RGB")
        return to_jpeg(upright.transpose(DEVICE_TRANSFORM), quality=quality)


def _checked_regular(path: Path, *, mode: int, description: str) -> None:
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ValueError(f"{description} must be a regular file, not a link or special file")
    if stat.S_IMODE(details.st_mode) != mode:
        raise ValueError(f"{description} must have mode {mode:04o}")


def migrate_legacy_managed_images(config_path: Path, *, backup_name: str | None = None) -> tuple[Path, tuple[Path, ...]]:
    """Reorient old GUI-managed images without source paths or destructive edits.

    Old releases wrote each managed JPEG rotated clockwise, then rotated it again
    at upload.  New files are source-oriented.  This one-off migration copies
    every original to a private timestamped backup and replaces each configured
    managed JPEG with its counter-clockwise normalized equivalent.
    """
    from .config import load_config

    config = load_config(config_path)
    assets = config_path.parent / "images"
    if not config.images:
        raise ValueError("no managed images are configured")
    details = assets.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o700:
        raise ValueError("managed images directory must be a non-link directory with mode 0700")
    stamp = backup_name or datetime.now(timezone.utc).strftime("legacy-cw-%Y%m%dT%H%M%SZ")
    if "/" in stamp or stamp in {"", ".", ".."}:
        raise ValueError("backup name must be a single non-empty directory name")
    backup = assets / stamp
    try:
        backup.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ValueError(f"backup directory already exists: {backup}") from error
    staged: list[tuple[Path, Path]] = []
    replaced: list[tuple[Path, Path]] = []
    try:
        for key, source in sorted(config.images.items()):
            _checked_regular(source, mode=0o600, description=f"managed image key{key}")
            original = source.read_bytes()
            destination = backup / source.name
            with open(destination, "xb") as output:
                output.write(original)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(destination, 0o600)
            with Image.open(BytesIO(original)) as opened:
                corrected = opened.transpose(Image.Transpose.ROTATE_90)
                data = to_jpeg(corrected)
            fd, temporary_name = tempfile.mkstemp(prefix=f".key{key}.migrate.", dir=assets)
            temporary = Path(temporary_name)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            staged.append((source, temporary))
        for source, temporary in staged:
            os.replace(temporary, source)
            replaced.append((source, backup / source.name))
    except Exception:
        for source, original in reversed(replaced):
            os.replace(original, source)
        raise
    finally:
        for _, temporary in staged:
            if temporary.exists():
                temporary.unlink()
    return backup, tuple(source for source, _ in staged)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="akp03-migrate-images", description="Safely reorient legacy AKP03 managed artwork once.")
    parser.add_argument("--config", type=Path, required=True, help="existing mode-0600 AKP03 TOML configuration")
    parser.add_argument("--backup-name", help="optional unique backup directory name under images/")
    args = parser.parse_args(argv)
    try:
        backup, migrated = migrate_legacy_managed_images(args.config, backup_name=args.backup_name)
    except (OSError, ValueError) as error:
        print(f"migration failed: {error}")
        return 1
    print(f"migrated {len(migrated)} managed image(s); originals preserved in {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
