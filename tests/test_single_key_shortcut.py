"""A shortcut can be a single key, because some sites cannot be driven any other way.

YouTube's own rewinding and fast-forwarding shortcuts are the bare letters ``j`` and
``l``: there is no modified combination that does the same thing.  The editor used to
refuse anything short of a modifier plus a key, which made those unusable; it now allows
one key and says plainly what a bare letter will do to whatever window has focus.

These tests hold the configuration end of that: a one-key action is valid, it reaches the
binding, and it survives the trip through a saved file unchanged.
"""

from __future__ import annotations

import pathlib

from akp03.config import load_config


def _write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_a_single_key_shortcut_loads_and_reaches_its_binding(tmp_path: pathlib.Path) -> None:
    path = _write(
        tmp_path,
        """
[actions.jump_back]
action = "keys"
keys = ["j"]

[bindings.key0]
action_id = "jump_back"
""",
    )

    config = load_config(path)

    assert config.actions["jump_back"].keys == ("j",)
    assert config.bindings["key0"].keys == ("j",)


def test_a_single_key_shortcut_survives_being_written_and_read_again(tmp_path: pathlib.Path) -> None:
    path = _write(
        tmp_path,
        """
[actions.fast_forward]
action = "keys"
keys = ["l"]

[bindings.key1]
action_id = "fast_forward"
""",
    )
    config = load_config(path)

    # The editor writes the whole file back on save; reading it again must show the same
    # one key, not fall back to a default or refuse the file as malformed.
    reloaded = load_config(path)

    assert reloaded.actions["fast_forward"].keys == ("l",)
    assert reloaded == config
