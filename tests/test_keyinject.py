"""macOS key-injection executor and its daemon wiring.

These tests never post a real keystroke.  They drive ``MacKeyBackend`` with a
real fake injector process (a tiny stdin/stdout script) so the exact command
language, the honest Accessibility reporting, and clean shutdown are verified
end to end without touching the keyboard.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from akp03.backend import (
    ACCESSIBILITY_MESSAGE,
    KeyboardBackendError,
    ACCESSIBILITY_REASON,
    MacKeyBackend,
    UnavailableBackend,
    WtypeBackend,
    default_keyboard_backend,
)
from akp03.daemon import Daemon
from akp03.device import Event

# A minimal injector: reads one command per line, echoes one JSON result line,
# and (when given a log path) records every command it received. It never posts
# anything.
FAKE_INJECTOR = '''\
import json
import sys


def main() -> None:
    accessibility = (sys.argv[1] == "1") if len(sys.argv) > 1 else True
    log = open(sys.argv[2], "a", buffering=1) if len(sys.argv) > 2 else None
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if log is not None:
            log.write(line + "\\n")
        verb = line.split()[0]
        if verb == "ping":
            out = {"result": "pong", "accessibility": accessibility, "dry_run": False}
        elif verb == "quit":
            out = {"result": "ok", "command": "quit"}
        else:
            parts = line.split()
            out = {"result": "ok", "command": verb, "chord": parts[1] if len(parts) > 1 else ""}
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.stdout.flush()
        if verb == "quit":
            break


main()
'''


@pytest.fixture
def fake_injector(tmp_path: Path):
    def build(*, accessibility: bool = True) -> tuple[MacKeyBackend, Path]:
        script = tmp_path / "fake_keyinject.py"
        script.write_text(f"#!{sys.executable}\n{FAKE_INJECTOR}")
        script.chmod(0o700)
        log_path = tmp_path / "commands.log"
        backend = MacKeyBackend(
            str(script),
            which=lambda _name: str(script),
            arguments=("1" if accessibility else "0", str(log_path)),
        )
        return backend, log_path

    return build


def _commands(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    return [line for line in log_path.read_text().splitlines() if line not in ("ping", "quit")]


# -- Backend selection ---------------------------------------------------------


def test_a_media_action_reaches_the_helper_as_its_own_verb(fake_injector) -> None:
    """`media sound-up`, never `tap f12`.

    The helper builds the system-defined event macOS listens for; a keyboard event
    cannot carry a media function at all.  This is the whole fix, end to end.
    """
    backend, log_path = fake_injector()
    backend.tap(("sound-up",))
    backend.tap(("cmd", "z"))
    assert _commands(log_path) == ["media sound-up", "tap cmd+z"]


def test_a_media_key_cannot_be_held_through_the_helper(fake_injector) -> None:
    backend, log_path = fake_injector()
    with pytest.raises(KeyboardBackendError):
        backend.tap(("cmd", "sound-up"))
    assert _commands(log_path) == []


def test_default_backend_selects_mac_helper_on_darwin() -> None:
    backend = default_keyboard_backend(
        platform="darwin",
        which=lambda name: "/opt/akp03/AKP03KeyInject" if name == "AKP03KeyInject" else None,
    )
    assert isinstance(backend, MacKeyBackend)
    assert backend.executable == "/opt/akp03/AKP03KeyInject"


def test_default_backend_honours_env_override_on_darwin() -> None:
    backend = default_keyboard_backend(
        platform="darwin",
        environ={"AKP03_KEYINJECT": "/custom/AKP03KeyInject"},
        which=lambda _name: None,
    )
    assert isinstance(backend, MacKeyBackend)
    assert backend.executable == "/custom/AKP03KeyInject"


def test_default_backend_darwin_without_helper_fails_closed(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="akp03d.keys")
    backend = default_keyboard_backend(platform="darwin", which=lambda _name: None)
    assert isinstance(backend, UnavailableBackend)
    assert "AKP03KeyInject helper not found" in caplog.text


def test_default_backend_keeps_linux_wayland_selection() -> None:
    wayland = default_keyboard_backend(
        platform="linux",
        environ={"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-1"},
        which=lambda _name: "/usr/bin/wtype",
    )
    assert isinstance(wayland, WtypeBackend)
    x11 = default_keyboard_backend(
        platform="linux",
        environ={"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-1"},
        which=lambda _name: "/usr/bin/wtype",
    )
    assert isinstance(x11, UnavailableBackend)


# -- Command language ----------------------------------------------------------


def test_mac_backend_formats_tap_hold_and_release_commands(fake_injector) -> None:
    backend, log_path = fake_injector()
    try:
        backend.tap(("cmd", "z"))
        backend.press(("ctrl", "shift", "space"))
        backend.release(("ctrl", "shift", "space"))
    finally:
        backend.close()
    assert _commands(log_path) == ["tap cmd+z", "hold ctrl+shift+space", "release ctrl+shift+space"]


def test_mac_backend_rejects_empty_chord_before_spawning(fake_injector) -> None:
    backend, log_path = fake_injector()
    from akp03.backend import KeyboardBackendError

    with pytest.raises(KeyboardBackendError):
        backend.tap(())
    assert backend._process is None and _commands(log_path) == []


# -- Permission handling -------------------------------------------------------


def test_mac_backend_logs_one_actionable_error_when_permission_absent(fake_injector, caplog) -> None:
    from akp03.backend import KeyboardBackendError

    backend, _ = fake_injector(accessibility=False)
    caplog.set_level(logging.DEBUG, logger="akp03d.keys")
    try:
        for _ in range(4):
            with pytest.raises(KeyboardBackendError, match="accessibility permission not granted"):
                backend.tap(("cmd", "z"))
    finally:
        backend.close()
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    # The actionable error is logged exactly once, not once per press...
    assert sum(ACCESSIBILITY_MESSAGE in message for message in errors) == 1
    assert sum("Accessibility" in message and "System Settings" in message for message in errors) == 1


def test_mac_backend_reports_accessibility_status_from_helper(fake_injector) -> None:
    granted, _ = fake_injector(accessibility=True)
    denied, _ = fake_injector(accessibility=False)
    try:
        granted.tap(("enter",))
        assert granted.accessibility is True
        from akp03.backend import KeyboardBackendError

        with pytest.raises(KeyboardBackendError):
            denied.tap(("enter",))
        assert denied.accessibility is False
    finally:
        granted.close()
        denied.close()


def test_mac_backend_missing_helper_fails_closed(tmp_path: Path) -> None:
    from akp03.backend import KeyboardBackendError

    backend = MacKeyBackend(str(tmp_path / "does-not-exist"), which=lambda _name: None)
    with pytest.raises(KeyboardBackendError, match="cannot start AKP03KeyInject"):
        backend.tap(("cmd", "z"))


# -- Lifecycle -----------------------------------------------------------------


def test_mac_backend_close_sends_quit_and_reaps(fake_injector) -> None:
    backend, log_path = fake_injector()
    backend.tap(("enter",))
    process = backend._process
    assert process is not None and process.poll() is None
    backend.close()
    assert "quit" in log_path.read_text().splitlines()
    assert process.poll() is not None
    # A second close is a harmless no-op.
    backend.close()


def test_daemon_tap_reaches_the_mac_helper(fake_injector, tmp_path: Path) -> None:
    backend, log_path = fake_injector()
    config = tmp_path / "config.toml"
    config.write_text('[bindings.key0]\naction="keys"\nkeys=["cmd","z"]\n')
    config.chmod(0o600)
    daemon = Daemon(config, state_path=tmp_path / "state", backend=backend)
    try:
        assert daemon.reload()
        daemon.event(Event("key", "key0", True, None, b""))
    finally:
        backend.close()
    assert _commands(log_path) == ["tap cmd+z"]


def test_daemon_hold_release_pair_reaches_the_mac_helper(fake_injector, tmp_path: Path) -> None:
    backend, log_path = fake_injector()
    config = tmp_path / "config.toml"
    config.write_text('[bindings.button6]\naction="keys_hold"\nkeys_hold=["ctrl","shift","space"]\n')
    config.chmod(0o600)
    daemon = Daemon(config, state_path=tmp_path / "state", backend=backend)
    try:
        assert daemon.reload()
        press = Event("button", "button6", True, None, b"")
        release = Event("button", "button6", False, None, b"")
        daemon.event(press)
        daemon.event(press)  # duplicate press must not re-hold
        daemon.event(release)
    finally:
        backend.close()
    assert _commands(log_path) == ["hold ctrl+shift+space", "release ctrl+shift+space"]


def test_daemon_close_deck_releases_a_held_chord_through_the_helper(fake_injector, tmp_path: Path) -> None:
    backend, log_path = fake_injector()
    config = tmp_path / "config.toml"
    config.write_text('[bindings.button6]\naction="keys_hold"\nkeys_hold=["ctrl","shift","space"]\n')
    config.chmod(0o600)
    daemon = Daemon(config, state_path=tmp_path / "state", backend=backend)
    try:
        assert daemon.reload()
        daemon.event(Event("button", "button6", True, None, b""))
        daemon.close_deck()  # release_all() runs here
    finally:
        backend.close()
    assert _commands(log_path) == ["hold ctrl+shift+space", "release ctrl+shift+space"]


def test_daemon_deduplicates_repeated_backend_failures(tmp_path: Path, caplog) -> None:
    from akp03.backend import KeyboardBackendError

    class Failing:
        def tap(self, keys):
            raise KeyboardBackendError(ACCESSIBILITY_REASON)

        def press(self, keys):
            raise KeyboardBackendError(ACCESSIBILITY_REASON)

        def release(self, keys):
            raise KeyboardBackendError(ACCESSIBILITY_REASON)

    config = tmp_path / "config.toml"
    config.write_text('[bindings.key0]\naction="keys"\nkeys=["cmd","z"]\n')
    config.chmod(0o600)
    caplog.set_level(logging.DEBUG, logger="akp03d.keys")
    daemon = Daemon(config, state_path=tmp_path / "state", backend=Failing())
    assert daemon.reload()
    for _ in range(5):
        daemon.event(Event("key", "key0", True, None, b""))
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert "keyboard tap failed" in error_records[0].getMessage()
