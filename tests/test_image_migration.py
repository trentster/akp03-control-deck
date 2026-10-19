from __future__ import annotations

import stat
from io import BytesIO

import pytest
from PIL import Image

from akp03.images import migrate_legacy_managed_images, to_jpeg


def test_legacy_migration_reorients_without_source_paths_and_preserves_private_original(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[images]\nkey0 = "key0.jpg"\n')
    config.chmod(0o600)
    assets = tmp_path / "images"
    assets.mkdir(mode=0o700)
    # Model a legacy managed image: source art was stored 90° clockwise.
    source = Image.new("RGB", (60, 30), "black")
    for x in range(30):
        for y in range(30):
            source.putpixel((x, y), (255, 0, 0))
    legacy = to_jpeg(source.transpose(Image.Transpose.ROTATE_270))
    managed = assets / "key0.jpg"
    managed.write_bytes(legacy)
    managed.chmod(0o600)

    backup, migrated = migrate_legacy_managed_images(config, backup_name="legacy-cw-test")

    assert migrated == (managed,)
    assert backup == assets / "legacy-cw-test"
    assert (backup / "key0.jpg").read_bytes() == legacy
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert stat.S_IMODE((backup / "key0.jpg").stat().st_mode) == 0o600
    with Image.open(BytesIO(managed.read_bytes())) as result:
        # Red region is horizontally oriented again, matching the source.
        assert result.getpixel((15, 30))[0] > 200
        assert result.getpixel((30, 5))[0] < 20
    assert stat.S_IMODE(managed.stat().st_mode) == 0o600


def test_legacy_migration_refuses_insecure_managed_directory_or_no_images(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")
    config.chmod(0o600)
    with pytest.raises(ValueError, match="no managed images"):
        migrate_legacy_managed_images(config)
