from __future__ import annotations

import subprocess

import pytest

from akp03.backend import (
    MEDIA_KEY_SYMBOLS,
    KeyboardBackendError,
    LogBackend,
    UnavailableBackend,
    WtypeBackend,
    keyboard_backend,
)
from akp03.daemon import Daemon
from akp03.device import Event


def test_wtype_tap_uses_fixed_argv_and_maps_cmd_to_logo() -> None:
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "", "")

    backend = WtypeBackend("/usr/bin/wtype", run=run)
    backend.tap(("ctrl", "cmd", "space"))

    assert calls == [
        ((["/usr/bin/wtype", "-M", "ctrl", "-M", "logo", "-k", "space", "-m", "logo", "-m", "ctrl"],),
         {"check": True, "capture_output": True, "text": True, "timeout": 2})
    ]


@pytest.mark.parametrize("keys", [("cmd",), ("semi;colon",), ("ctrl", "unknown-key")])
def test_wtype_rejects_noncanonical_or_modifier_only_shortcuts(keys) -> None:
    with pytest.raises(KeyboardBackendError):
        WtypeBackend().tap(keys)


def test_wtype_fails_holds_explicitly_because_wtype_releases_on_process_exit() -> None:
    backend = WtypeBackend()
    with pytest.raises(KeyboardBackendError, match="cannot safely hold"):
        backend.press(("ctrl", "space"))
    with pytest.raises(KeyboardBackendError, match="cannot safely release"):
        backend.release(("ctrl", "space"))


def test_backend_selection_requires_wayland_display_and_wtype(caplog) -> None:
    caplog.set_level("INFO", logger="akp03d.keys")
    missing = keyboard_backend({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-1"}, which=lambda _: "/usr/bin/wtype")
    assert isinstance(missing, UnavailableBackend)
    available = keyboard_backend({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-1"}, which=lambda _: "/usr/bin/wtype")
    assert isinstance(available, WtypeBackend)
    assert "keyboard injection disabled" in caplog.text
    assert "keyboard injection enabled with wtype" in caplog.text


def test_unavailable_backend_does_not_pretend_key_action_succeeded(tmp_path, caplog) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[bindings.key0]\naction="keys"\nkeys=["cmd","space"]\n')
    path.chmod(0o600)
    caplog.set_level("ERROR", logger="akp03d.keys")
    daemon = Daemon(path, state_path=tmp_path / "state", backend=UnavailableBackend("wtype missing"))
    assert daemon.reload()
    daemon.event(Event("key", "key0", True, None, b""))
    assert "keyboard tap failed: wtype missing" in caplog.text


def test_log_backend_remains_available_for_test_mode(caplog) -> None:
    caplog.set_level("INFO", logger="akp03d.keys")
    backend = LogBackend()
    backend.tap(("enter",))
    assert "TAP enter" in caplog.text


# -- media keys ----------------------------------------------------------------


def test_a_media_key_is_its_own_event_not_a_function_key() -> None:
    """F12 for volume up was the bug: macOS moves nothing for a synthetic F12."""
    calls: list[list[str]] = []
    backend = WtypeBackend("/usr/bin/wtype", run=lambda argv, **kw: calls.append(argv))
    for token, symbol in MEDIA_KEY_SYMBOLS.items():
        backend.tap((token,))
        assert calls[-1] == ["/usr/bin/wtype", "-k", symbol], token
    assert len(set(MEDIA_KEY_SYMBOLS.values())) == 6
    assert set(MEDIA_KEY_SYMBOLS) == {"mute", "sound-down", "sound-up", "play-pause", "next", "previous"}


def test_a_media_key_cannot_be_combined_with_other_keys() -> None:
    backend = WtypeBackend("/usr/bin/wtype", run=lambda argv, **kw: None)
    with pytest.raises(KeyboardBackendError):
        backend.tap(("cmd", "sound-up"))


def test_an_ordinary_chord_still_goes_through_wtype_unchanged() -> None:
    calls: list[list[str]] = []
    backend = WtypeBackend("/usr/bin/wtype", run=lambda argv, **kw: calls.append(argv))
    backend.tap(("cmd", "z"))
    assert calls[-1] == ["/usr/bin/wtype", "-M", "logo", "-k", "z", "-m", "logo"]
