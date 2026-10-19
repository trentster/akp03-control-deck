"""First-run config handling: a *missing* config is bootstrapped, a *broken* one is not.

The macOS menu-bar app launches the daemon with default paths on a fresh Mac, so
a machine with no config must still come up and host the editor.  A config that
exists but is unreadable, malformed, or invalid must keep failing loudly and must
never be overwritten — the two cases are deliberately different code paths.
"""
from __future__ import annotations
from support import wait_for_port as _wait_for_port

import json
import logging
import stat
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from akp03 import daemon as daemon_module
from akp03.config import ConfigError, ConfigNotFound, load_config, write_example
from akp03.daemon import Daemon
from akp03.feed import ControlFeed


def starter_text() -> str:
    """The exact bytes ``akp03d --init-config`` writes (the daemon's starter)."""
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        write_example(path)
        return path.read_text()


def first_run_messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if "first-run" in record.getMessage()]


# -- Missing file is a distinct, non-fatal case -------------------------------


def test_missing_config_raises_config_not_found(tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFound):
        load_config(tmp_path / "absent.toml")
    # Still a ConfigError, so every existing caller keeps working unchanged.
    assert issubclass(ConfigNotFound, ConfigError)


def test_first_run_bootstraps_the_starter_config_and_logs_once(tmp_path: Path, caplog) -> None:
    path = tmp_path / "nested" / "akp03" / "config.toml"
    daemon = Daemon(path, state_path=tmp_path / "state")
    caplog.set_level(logging.INFO, logger="akp03d")
    assert not path.exists()

    assert daemon.reload() is True

    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert path.read_text() == starter_text()
    assert daemon.config is not None and daemon.config.device == "/dev/hidraw0"
    assert len(first_run_messages(caplog)) == 1

    # A second reload loads the now-present file and never re-announces first run.
    assert daemon.reload() is True
    assert len(first_run_messages(caplog)) == 1


def test_bootstrap_race_loads_the_file_that_appeared(tmp_path: Path, monkeypatch, caplog) -> None:
    """If the file appears between the check and our O_EXCL write, load it, never clobber."""
    path = tmp_path / "config.toml"
    real_write = daemon_module.write_example

    def racing_write(target: Path) -> None:
        real_write(target)  # a concurrent writer wins the race ...
        raise FileExistsError(target)  # ... and our exclusive create then fails

    monkeypatch.setattr(daemon_module, "write_example", racing_write)
    caplog.set_level(logging.INFO, logger="akp03d")
    daemon = Daemon(path, state_path=tmp_path / "state")

    assert daemon.reload() is True
    assert daemon.config is not None
    assert first_run_messages(caplog) == []


def test_bootstrap_write_failure_is_a_clear_config_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"

    def broken_write(target: Path) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(daemon_module, "write_example", broken_write)
    daemon = Daemon(path, state_path=tmp_path / "state")

    # Reported as a ConfigError (never an uncaught traceback), so the daemon
    # exits non-zero with a clear message.
    assert daemon.reload() is False
    assert daemon.last_error is not None and "starter config" in daemon.last_error


# -- Existing-but-broken file still fails loudly and is untouched -------------


@pytest.mark.parametrize(
    "original",
    [
        "[daemon]\ndevice = \"/dev/hidraw0\"\nthis is not toml =\n",  # malformed TOML
        "[daemon]\npoll_seconds = 999\n",  # fails the existing validators
    ],
    ids=["malformed-toml", "validation-failure"],
)
def test_broken_config_fails_loudly_and_is_never_overwritten(tmp_path: Path, caplog, original) -> None:
    path = tmp_path / "config.toml"
    path.write_text(original)
    path.chmod(0o600)
    before = path.read_bytes()

    daemon = Daemon(path, state_path=tmp_path / "state")
    caplog.set_level(logging.ERROR, logger="akp03d")

    assert daemon.reload() is False
    assert daemon.config is None
    # The user's edited file is byte-for-byte untouched, and the daemon said why.
    assert path.read_bytes() == before
    assert any("configuration rejected" in record.getMessage() for record in caplog.records)
    assert first_run_messages(caplog) == []


def test_unreadable_config_is_not_bootstrapped(tmp_path: Path) -> None:
    """A present file with the wrong mode is a broken config, not a first run."""
    path = tmp_path / "config.toml"
    path.write_text("[daemon]\ndevice = \"/dev/hidraw0\"\n")
    path.chmod(0o640)
    before = path.read_bytes()

    daemon = Daemon(path, state_path=tmp_path / "state")

    assert daemon.reload() is False
    assert path.read_bytes() == before


# -- The daemon actually comes up and hosts the editor on first run -----------


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(port: int, path: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return response.getcode(), response.read()


def test_first_run_daemon_serves_the_editor(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    feed = ControlFeed(connected=False, phase="waiting")
    daemon = Daemon(path, state_path=tmp_path / "state", feed=feed)
    sock_path = tmp_path / "daemon.sock"
    port = _free_port()
    thread = threading.Thread(target=lambda: daemon.run(sock_path, editor_port=port), name="first-run-daemon")
    thread.start()
    try:
        assert _wait_for_port(port), "first-run daemon never hosted the editor"
        status, body = _get(port, "/")
        assert status == 200 and body
        status, body = _get(port, "/api/state")
        assert status == 200
        assert json.loads(body)["draft"]["bindings"] == [None] * 6
    finally:
        daemon.stop.set()
        thread.join(timeout=4)
    assert not thread.is_alive()
    # The starter config was written and is a real, loadable config.
    assert path.is_file()
    assert load_config(path).device == "/dev/hidraw0"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
