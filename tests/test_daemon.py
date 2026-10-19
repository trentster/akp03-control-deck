from __future__ import annotations

import json
import stat
import threading
import time
from pathlib import Path

import pytest

from akp03.config import ConfigError, load_config
from akp03.daemon import Daemon, Paint, parse_control_command
from akp03.device import Event


class Backend:
    def __init__(self): self.calls = []
    def tap(self, keys): self.calls.append(("tap", tuple(keys)))
    def press(self, keys): self.calls.append(("press", tuple(keys)))
    def release(self, keys): self.calls.append(("release", tuple(keys)))


class Deck:
    def __init__(self, path): self.path = path; self.calls = []; self.images = []
    def on_event(self, cb): self.event = cb
    def on_disconnect(self, cb): self.disconnect = cb
    def open(self): return self
    def close(self): self.calls.append(("close",))
    def clear(self, key=None): self.calls.append(("clear", key))
    def set_key_image(self, key, image): self.calls.append(("image", key)); self.images.append((key, image.copy()))
    def set_brightness(self, level): self.calls.append(("brightness", level))


def config(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def test_config_requires_exact_mode_and_initial_bindings(tmp_path):
    path = tmp_path / "config.toml"
    config(path, '''[bindings.button6]
action="keys_hold"
keys_hold=["ctrl","shift","space"]
[bindings.button7]
action="keys"
keys=["enter"]
[bindings.button8]
action="keys"
keys=["cmd","z"]
''')
    result = load_config(path)
    assert result.bindings["button6"].keys_hold == ("ctrl", "shift", "space")
    path.chmod(0o640)
    with pytest.raises(ConfigError, match="0600"):
        load_config(path)


def test_shell_and_http_bindings_validate_strict_schemas(tmp_path):
    path = tmp_path / "config.toml"
    config(path, '''[bindings.top]
action="shell"
shell=["/usr/bin/printf", "safe argument"]
[bindings.bottom_left]
action="http"
method="POST"
url="https://example.invalid/hook"
json={ source="akp03" }
headers={ Authorization="env:AKP03_WEBHOOK_TOKEN" }
''')
    bindings = load_config(path).bindings
    assert bindings["top"].shell == ("/usr/bin/printf", "safe argument")
    assert bindings["bottom_left"].http_method == "POST"
    assert bindings["bottom_left"].http_headers == (("Authorization", "env:AKP03_WEBHOOK_TOKEN"),)
    config(path, '''[bindings.top]
action="shell"
shell="echo unsafe"
''')
    with pytest.raises(ConfigError, match="shell must be a non-empty array"):
        load_config(path)
    config(path, '''[bindings.top]
action="http"
url="https://example.invalid"
headers={ Authorization="env:not-valid" }
''')
    with pytest.raises(ConfigError, match="invalid environment"):
        load_config(path)


def test_held_chord_releases_on_release_disconnect_reload_and_close(tmp_path):
    path = tmp_path / "config.toml"; state = tmp_path / "state.json"
    config(path, '[bindings.button6]\naction="keys_hold"\nkeys_hold=["ctrl","shift","space"]\n')
    backend = Backend(); daemon = Daemon(path, state_path=state, backend=backend)
    assert daemon.reload()
    press = Event("button", "button6", True, None, b""); release = Event("button", "button6", False, None, b"")
    daemon.event(press); daemon.event(press); daemon.event(release)
    daemon.event(press); daemon.disconnect(OSError("gone")); daemon.event(press); daemon.reload(); daemon.close_deck()
    assert backend.calls == [("press", ("ctrl", "shift", "space")), ("release", ("ctrl", "shift", "space")), ("press", ("ctrl", "shift", "space")), ("release", ("ctrl", "shift", "space")), ("press", ("ctrl", "shift", "space")), ("release", ("ctrl", "shift", "space"))]


def test_socket_commands_require_validated_paint_and_unpaint_forms(tmp_path):
    path = tmp_path / "config.toml"; state = tmp_path / "state.json"; deck = Deck("/dev/fake")
    config(path, '[labels]\nkey0="Hello"\n')
    daemon = Daemon(path, state_path=state, deck_factory=lambda _: deck)
    assert daemon.reload() and daemon.connect()
    assert daemon.handle("status").startswith("OK STATUS")
    assert daemon.handle("reload") == "OK RELOAD"
    assert daemon.handle('paint 1 "Build OK" "#112233" white') == "OK PAINT"
    assert daemon.handle("unpaint 1") == "OK UNPAINT"
    stored = json.loads(state.read_text())
    assert stored == {"key0": {"text": "Hello", "background": "black", "foreground": "white"}}
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    for invalid in ("paint", "paint 6 no", "paint 1 " + "x" * 33, "paint 1 text nope", "unpaint", "unpaint 9", "brightness 0", "shell /bin/true", "http https://example.invalid"):
        with pytest.raises(Exception):
            daemon.handle(invalid)
    assert parse_control_command('paint 2 "Hi there"') == ("paint", (Paint(2, "Hi there"),))


def test_daemon_socket_rejects_actions_and_reconnects(tmp_path):
    from akp03.session import send_command
    path = tmp_path / "config.toml"; sock = tmp_path / "akp03.sock"
    config(path, '[daemon]\npoll_seconds=0.1\n')
    attempts = []
    def factory(device):
        attempts.append(device)
        if len(attempts) == 1: raise OSError("not present")
        return Deck(device)
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=factory)
    thread = threading.Thread(target=lambda: daemon.run(sock)); thread.start()
    for _ in range(50):
        if sock.exists(): break
        time.sleep(.01)
    assert send_command("brightness 10", sock).startswith("ERR")
    assert send_command("shell /bin/true", sock).startswith("ERR")
    assert send_command("http https://example.invalid", sock).startswith("ERR")
    assert send_command("paint", sock).startswith("ERR")
    assert send_command('paint 2 "Ready" black white', sock) == "OK PAINT"
    assert send_command("unpaint 2", sock) == "OK UNPAINT"
    assert send_command("status", sock).startswith("OK STATUS")
    time.sleep(.15)
    assert len(attempts) >= 2
    daemon.stop.set(); thread.join(2)
    assert not thread.is_alive() and not sock.exists()


def test_shell_actions_are_nonblocking_limited_and_redacted(tmp_path, monkeypatch, caplog):
    from akp03 import daemon as daemon_module
    path = tmp_path / "config.toml"
    config(path, '[bindings.top]\naction="shell"\nshell=["/bin/echo", "safe"]\n')
    daemon = Daemon(path, state_path=tmp_path / "state")
    assert daemon.reload()
    started, release = threading.Event(), threading.Event()
    calls = []
    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 4: started.set()
        release.wait(2)
        return daemon_module.subprocess.CompletedProcess(args[0], 7, "token=supersecret", "stderr=supersecret")
    monkeypatch.setenv("AKP03_WEBHOOK_TOKEN", "supersecret")
    monkeypatch.setattr(daemon_module.subprocess, "run", fake_run)
    caplog.set_level("INFO", logger="akp03d.actions")
    event = Event("knob", "top", True, None, b"")
    begun = time.monotonic()
    for _ in range(4): daemon.event(event)
    assert time.monotonic() - begun < .2
    assert started.wait(1)
    daemon.event(event)
    release.set()
    daemon._actions.shutdown(wait=True)
    assert len(calls) == 4
    assert all(call[1]["shell"] is False and call[1]["timeout"] == 10 for call in calls)
    messages = "\n".join(record.message for record in caplog.records)
    assert "concurrent action limit" in messages
    assert "exit=7" in messages and "duration_ms=" in messages
    assert "supersecret" not in messages and "***" in messages


def test_http_action_resolves_environment_header_without_logging_secret(tmp_path, monkeypatch, caplog):
    from akp03 import daemon as daemon_module
    path = tmp_path / "config.toml"
    config(path, '''[bindings.bottom_left]
action="http"
method="POST"
url="https://example.invalid/hook"
json={ source="akp03" }
headers={ Authorization="env:AKP03_WEBHOOK_TOKEN" }
''')
    daemon = Daemon(path, state_path=tmp_path / "state")
    assert daemon.reload()
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def getcode(self): return 204
    def fake_urlopen(request, timeout):
        captured.update(method=request.method, body=request.data, auth=request.get_header("Authorization"), timeout=timeout)
        return Response()
    monkeypatch.setenv("AKP03_WEBHOOK_TOKEN", "secret-token")
    monkeypatch.setattr(daemon_module.urllib.request, "urlopen", fake_urlopen)
    caplog.set_level("INFO", logger="akp03d.actions")
    daemon.event(Event("knob", "bottom_left", True, None, b""))
    daemon._actions.shutdown(wait=True)
    assert captured == {"method": "POST", "body": b'{"source": "akp03"}', "auth": "secret-token", "timeout": 5}
    assert "status=204" in "\n".join(record.message for record in caplog.records)
    assert "secret-token" not in caplog.text


def test_log_backend_uses_exact_action_terms(caplog):
    from akp03.backend import LogBackend
    caplog.set_level("INFO", logger="akp03d.keys")
    backend = LogBackend()
    backend.press(("ctrl", "shift", "space"))
    backend.release(("ctrl", "shift", "space"))
    backend.tap(("enter",))
    assert [record.message for record in caplog.records] == ["DOWN ctrl+shift+space", "UP ctrl+shift+space", "TAP enter"]


def test_max_hold_releases_with_fake_clock(tmp_path):
    path = tmp_path / "config.toml"; state = tmp_path / "state.json"
    config(path, '[deck]\nmax_hold_s=5\n[bindings.button6]\naction="keys_hold"\nkeys_hold=["ctrl","shift","space"]\n')
    now = [100.0]
    backend = Backend(); daemon = Daemon(path, state_path=state, backend=backend, clock=lambda: now[0])
    assert daemon.reload()
    daemon.event(Event("button", "button6", True, None, b""))
    now[0] = 105.0
    daemon.expire_held()
    assert daemon.held == {}
    assert backend.calls == [("press", ("ctrl", "shift", "space")), ("release", ("ctrl", "shift", "space"))]


class FakeTimer:
    def __init__(self, delay, callback): self.delay, self.callback, self.cancelled, self.started = delay, callback, False, False
    def start(self): self.started = True
    def cancel(self): self.cancelled = True
    def fire(self): self.callback()


def test_lcd_press_feedback_is_disabled_by_default(tmp_path):
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake")
    config(path, "[labels]\nkey0=\"Hello\"\n")
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck)
    assert daemon.reload() and daemon.connect(); deck.calls.clear(); deck.images.clear()
    daemon.event(Event("key", "key0", True, None, b""))
    assert deck.calls == []


def test_lcd_press_feedback_overlays_and_restores_custom_paint(tmp_path):
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake"); timers = []
    config(path, "[feedback]\nenabled=true\nduration_ms=400\n[screen]\nblank_after_s=0\n")
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck, timer_factory=lambda delay, callback: timers.append(FakeTimer(delay, callback)) or timers[-1])
    assert daemon.reload() and daemon.connect(); daemon.paint(Paint(0, "Custom", "#112233", "white")); base = deck.images[-1][1].tobytes(); deck.calls.clear(); deck.images.clear()
    daemon.event(Event("key", "key0", True, None, b""))
    assert len(timers) == 1 and timers[0].delay == .4 and timers[0].started and deck.images[-1][1].tobytes() != base
    timers[0].fire()
    assert [call for call in deck.calls if call[0] == "image"] == [("image", 0), ("image", 0)] and deck.images[-1][1].tobytes() == base


def test_lcd_feedback_stale_timer_cannot_overwrite_new_paint_or_reconnect(tmp_path):
    path = tmp_path / "config.toml"; first, second = Deck("/dev/one"), Deck("/dev/two"); timers = []
    config(path, "[feedback]\nenabled=true\n"); decks = iter((first, second))
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: next(decks), timer_factory=lambda delay, callback: timers.append(FakeTimer(delay, callback)) or timers[-1])
    assert daemon.reload() and daemon.connect(); daemon.paint(Paint(0, "Old")); first.calls.clear()
    daemon.event(Event("key", "key0", True, None, b"")); stale = timers[-1]; daemon.paint(Paint(0, "New")); assert stale.cancelled
    calls_before = list(first.calls); stale.fire(); assert first.calls == calls_before
    daemon.event(Event("key", "key0", True, None, b"")); stale = timers[-1]; daemon.close_deck(); assert daemon.connect()
    calls_before = list(first.calls); stale.fire(); assert first.calls == calls_before


@pytest.mark.parametrize("feedback, message", [("enabled=\"yes\"", "enabled"), ("duration_ms=149", "duration_ms"), ("duration_ms=2001", "duration_ms"), ("duration_ms=true", "duration_ms")])
def test_feedback_config_validation(tmp_path, feedback, message):
    path = tmp_path / "config.toml"; config(path, "[feedback]\n" + feedback + "\n")
    with pytest.raises(ConfigError, match=message): load_config(path)


def test_feedback_config_defaults_and_bounds(tmp_path):
    default = tmp_path / "default.toml"; config(default, "")
    assert load_config(default).feedback.enabled is False and load_config(default).feedback.duration_ms == 180
    path = tmp_path / "config.toml"; config(path, "[feedback]\nenabled=true\nduration_ms=150\n")
    assert load_config(path).feedback.enabled is True and load_config(path).feedback.duration_ms == 150
    config(path, "[feedback]\nduration_ms=2000\n")
    assert load_config(path).feedback.duration_ms == 2000



def test_feedback_is_lcd_only_and_preserves_key_action(tmp_path):
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake"); backend = Backend(); timers = []
    config(path, "[feedback]\nenabled=true\n[screen]\nblank_after_s=0\n[bindings.key0]\naction=\"keys\"\nkeys=[\"enter\"]\n[bindings.button6]\naction=\"keys\"\nkeys=[\"space\"]\n")
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck, backend=backend, timer_factory=lambda delay, callback: timers.append(FakeTimer(delay, callback)) or timers[-1])
    assert daemon.reload() and daemon.connect(); deck.calls.clear()
    daemon.event(Event("button", "button6", True, None, b""))
    daemon.event(Event("knob_press", "top", True, None, b""))
    assert deck.calls == [] and timers == []
    daemon.event(Event("key", "key0", True, None, b""))
    assert backend.calls == [("tap", ("space",)), ("tap", ("enter",))]
    assert len(timers) == 1 and deck.calls == [("image", 0)]



def test_opt_in_menu_demo_routes_knobs_and_lcds_without_affecting_lower_buttons(tmp_path):
    path = tmp_path / "config.toml"; state = tmp_path / "state"; deck = Deck("/dev/fake"); backend = Backend()
    config(path, "[menu]\nenabled=true\n[bindings.button6]\naction=\"keys\"\nkeys=[\"space\"]\n")
    daemon = Daemon(path, state_path=state, deck_factory=lambda _: deck, backend=backend)
    assert daemon.reload() and daemon.connect()
    assert daemon.labels[0].text == "> Controls" and daemon.labels[0].background == "#173f5f"
    daemon.event(Event("knob_turn", "top", None, 1, b""))
    assert daemon.labels[1].text == "> Display" and daemon.labels[1].background == "#173f5f"
    daemon.event(Event("knob_press", "top", True, None, b""))
    assert daemon.labels[0].text == "> Bright 25"
    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    assert daemon.labels[0].text == "> Controls"
    daemon.event(Event("key", "key0", True, None, b""))
    assert daemon.labels[0].text == "> Top knob"
    daemon.event(Event("button", "button6", True, None, b""))
    assert backend.calls == [("tap", ("space",))]


def test_menu_config_requires_boolean_enabled(tmp_path):
    path = tmp_path / "config.toml"; config(path, "[menu]\nenabled=\"yes\"\n")
    with pytest.raises(ConfigError, match="menu enabled"):
        load_config(path)



def test_menu_demo_brightness_action_changes_deck_level(tmp_path):
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake")
    config(path, "[menu]\nenabled=true\n")
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck)
    assert daemon.reload() and daemon.connect(); deck.calls.clear()
    daemon.event(Event("knob_turn", "top", None, 1, b""))
    daemon.event(Event("knob_press", "top", True, None, b""))
    assert daemon.labels[0].text == "> Bright 25"
    daemon.event(Event("key", "key0", True, None, b""))
    assert ("brightness", 25) in deck.calls



def test_control_brightness_command_validates_and_calls_deck(tmp_path):
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake")
    config(path, "")
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck)
    assert daemon.reload() and daemon.connect()
    assert daemon.handle("brightness 75") == "OK BRIGHTNESS"
    assert deck.calls[-1] == ("brightness", 75)
    for invalid in ("brightness", "brightness 0", "brightness 101", "brightness nope"):
        with pytest.raises(Exception):
            daemon.handle(invalid)
