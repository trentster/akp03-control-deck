"""The editor loads the daemon's real config, and Save persists it atomically
through the service while preserving every part the editor does not model.

These tests cover the closed loop end to end: ``GET /api/state`` reflects the
daemon's on-disk configuration; ``POST /api/save`` merges only the tables the
editor owns, writes atomically, and rebinds the *running* daemon without a
restart; and every failure leaves the config byte-identical and the server up.
"""
from __future__ import annotations
from support import wait_for_port as _wait_for_port, wait_until as _wait_until

import hashlib
import json
import logging
import socket
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

from akp03.daemon import Daemon
from akp03.device import Event
from akp03.feed import ControlFeed
from akp03.webui import make_server, merge_draft_into_config, parse_draft

import support

# A config with modelled content (safe [actions.*], [bindings.key0]) alongside
# everything the editor does NOT model: button/dial bindings, a hold action, an
# http action, [feedback], the legacy [menu], [labels], a comment, and a custom
# menu.  The save must preserve all of the latter byte-for-byte.
FIXTURE = '''[daemon]
device = "/dev/hidraw0"
poll_seconds = 1

# a comment the editor must never rewrite
[deck]
max_hold_s = 90

[actions.copy]
action = "keys"
keys = ["ctrl", "c"]
[actions.dim]
action = "brightness"
brightness = 40

[bindings.key0]
action_id = "copy"

[bindings.button6]
action = "keys_hold"
keys_hold = ["ctrl", "shift", "space"]
[bindings.button7]
action = "keys"
keys = ["enter"]
# A dial the editor now has slots for: this must round-trip as it stands.
[bindings.top]
action_id = "dim"

# A control the editor has no slot for, hand-written: it must survive untouched.
[bindings.bottom_middle]
action = "http"
method = "POST"
url = "https://example.invalid/hook"
json = { source = "akp03" }

[feedback]
enabled = true
duration_ms = 250

[menu]
enabled = false

[labels]
key0 = "One"
key1 = "Two"

[custom_menus.studio]
root = "home"
[[custom_menus.studio.pages]]
id = "home"
title = "Studio"
items = [ { key = 0, label = "Copy", action = "copy" } ]
'''

# Exact byte-identical fragments that a save must leave untouched.
UNMODELLED = [
    "# a comment the editor must never rewrite",
    '[bindings.bottom_middle]\naction = "http"\nmethod = "POST"\nurl = "https://example.invalid/hook"\njson = { source = "akp03" }',
    "[feedback]\nenabled = true\nduration_ms = 250",
    "[menu]\nenabled = false",
    '[labels]\nkey0 = "One"\nkey1 = "Two"',
    "[[custom_menus.studio.pages]]",
]


def write_config(directory: Path, text: str, name: str = "config.toml") -> Path:
    path = directory / name
    path.write_text(text)
    path.chmod(0o600)
    return path


def request(server, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    return response.status, data


def running_server(*, config: Path | None = None, apply=None, paint_status=None):
    server = make_server(0, config=config, apply=apply, paint_status=paint_status)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    return server, thread


def stop_server(server, thread) -> None:
    server.shutdown()
    thread.join()
    server.server_close()


def temp_files(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f".{path.name}.*"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- Load: the editor shows the daemon's real configuration --------------------


def test_state_shows_the_daemons_real_configuration_not_an_empty_draft(tmp_path: Path) -> None:
    path = write_config(tmp_path, FIXTURE)
    server, thread = running_server(config=path)
    try:
        status, body = request(server, "GET", "/api/state")
        assert status == 200
        # Modelled content is the daemon's real values, not a blank draft.
        assert body["draft"]["bindings"] == ["copy", None, None, None, None, None]
        assert {"id": "copy", "kind": "keys", "keys": ["ctrl", "c"]} in body["draft"]["actions"]
        assert {"id": "dim", "kind": "brightness", "brightness": 40} in body["draft"]["actions"]
        assert body["draft"]["menus"][0]["name"] == "studio"
        # The editor names its source and whether it can save.
        assert body["source"] == "daemon"
        assert body["config_path"] == str(path)
        assert body["can_save"] is True
        assert "not implemented" not in body["persistence"]
    finally:
        stop_server(server, thread)


def test_sample_draft_is_honest_that_it_cannot_save() -> None:
    server, thread = running_server()
    try:
        status, body = request(server, "GET", "/api/state")
        assert status == 200
        assert body["source"] == "sample"
        assert body["can_save"] is False
        assert body["draft"]["bindings"] == ["record", "marker", "dim", "studio", None, None]
        status, body = request(server, "POST", "/api/save", {"actions": [], "bindings": [None] * 6, "dials": [None] * 8, "menus": []})
        assert status == 409
        assert "nothing to save" in body["error"]
    finally:
        stop_server(server, thread)


def test_a_dial_carrying_something_the_editor_cannot_show_is_refused(tmp_path: Path) -> None:
    # The editor has a slot for every dial now, so it owns those tables.  A
    # hand-written http binding on one is refused with a reason rather than
    # quietly erased, exactly as an unsafe binding on a cap already is.
    path = write_config(tmp_path, FIXTURE + '\n[bindings.top_turn_right]\naction = "http"\nurl = "https://example.invalid/hook"\n')
    before = path.read_text()
    server, thread = running_server(config=path)
    try:
        _, current = request(server, "GET", "/api/state")
        status, body = request(server, "POST", "/api/save", current["draft"])
        assert status in (400, 409), body
        assert "top_turn_right" in body["error"]
    finally:
        stop_server(server, thread)

    # Refused means untouched: the file is exactly as it was.
    assert path.read_text() == before


# -- Save: round trip with byte-for-byte preservation --------------------------


def test_save_changes_one_key_and_preserves_every_unmodelled_section(tmp_path: Path) -> None:
    path = write_config(tmp_path, FIXTURE)
    before = path.read_text()
    server, thread = running_server(config=path)
    try:
        _, current = request(server, "GET", "/api/state")
        draft = current["draft"]
        draft["bindings"][0] = "dim"  # change one LCD key binding
        status, saved = request(server, "POST", "/api/save", draft)
        assert status == 200
        assert saved["path"] == str(path)
        assert saved["saved"] == str(path)
        assert saved["draft"]["bindings"][0] == "dim"
    finally:
        stop_server(server, thread)

    after = path.read_text()
    # (a) the change applied
    assert '[bindings.key0]\naction_id = "dim"' in after
    assert '[bindings.key0]\naction_id = "copy"' not in after
    # (a2) the two hand-written button chords were adopted under names rather
    # than erased, and each keeps the chord it had
    assert '[bindings.button6]\naction_id = "hold-ctrl-shift-space"\nmode = "hold"' in after
    assert '[bindings.button7]\naction_id = "keys-enter"' in after
    assert '[actions.hold-ctrl-shift-space]\naction = "keys"\nkeys = ["ctrl", "shift", "space"]' in after
    # the cap in the middle of the deck was unassigned, so nothing was invented
    assert "[bindings.button8]" not in after
    # (a3) the big dial's press was set on the deck page, so its own table round
    # trips exactly as it was written
    assert '[bindings.top]\naction_id = "dim"' in after
    # (b) every unmodelled section is preserved byte-for-byte and all present
    for fragment in UNMODELLED:
        assert fragment in before, f"fixture missing {fragment!r}"
        assert fragment in after, f"save dropped or rewrote {fragment!r}"
    # (c) no temporary file was left behind, and mode/permissions are intact
    assert temp_files(path) == []
    assert (path.stat().st_mode & 0o777) == 0o600
    # and it still loads as a valid daemon config
    from akp03.config import load_config

    reloaded = load_config(path)
    assert reloaded.bindings["key0"] is reloaded.actions["dim"]
    # the cap's hand-written chord survived as a held assignment under a name
    assert reloaded.bindings["button6"].keys == ("ctrl", "shift", "space")
    assert reloaded.bindings["button6"].mode == "hold"
    assert reloaded.feedback.enabled is True
    assert reloaded.labels == {0: "One", 1: "Two"}


def test_saving_the_same_configuration_twice_writes_the_same_bytes(tmp_path: Path) -> None:
    """A save must be idempotent: an unchanged draft cannot keep editing the file.

    The blank line between the configuration the editor keeps and the tables it owns
    came back inside the retained text, so appending another one each time made the
    file grow a blank line per save - two saves of identical content wrote different
    bytes, which no diff of the *content* could explain.  A drifted file is tidied
    rather than grown.
    """
    path = write_config(tmp_path, FIXTURE)
    server, thread = running_server(config=path)
    try:
        _, current = request(server, "GET", "/api/state")
        draft = current["draft"]
        draft["bindings"][0] = "dim"
        assert request(server, "POST", "/api/save", draft)[0] == 200
        first = path.read_text()
        assert request(server, "POST", "/api/save", draft)[0] == 200
        second = path.read_text()
    finally:
        stop_server(server, thread)

    assert first == second, "saving an unchanged draft edited the file"
    # One blank line where the kept configuration meets the editor's own tables.
    assert "\n\n\n" not in first

    # A file that has already drifted (the old behaviour) is pulled back in line.
    drifted = first.replace("\n\n[actions.", "\n\n\n\n[actions.")
    assert merged_clean(drifted, draft)


def merged_clean(config_text: str, payload: dict) -> bool:
    merged = merge_draft_into_config(config_text, parse_draft(payload))
    return "\n\n\n" not in merged and merged.endswith("\n")


# -- Save: live apply through the daemon hook ----------------------------------


def test_save_reports_applied_and_calls_the_daemon_rebind_hook(tmp_path: Path) -> None:
    path = write_config(tmp_path, FIXTURE)
    calls: list[int] = []
    server, thread = running_server(config=path, apply=lambda: calls.append(1) or True)
    try:
        _, current = request(server, "GET", "/api/state")
        draft = current["draft"]
        draft["bindings"][1] = "dim"
        status, saved = request(server, "POST", "/api/save", draft)
        assert status == 200
        assert saved["applied"] is True
        assert calls == [1]
        assert "applied to the running deck" in saved["persistence"]
    finally:
        stop_server(server, thread)


def test_apply_returning_false_is_reported_honestly(tmp_path: Path) -> None:
    path = write_config(tmp_path, FIXTURE)
    server, thread = running_server(config=path, apply=lambda: False)
    try:
        _, current = request(server, "GET", "/api/state")
        draft = current["draft"]
        draft["bindings"][1] = "dim"
        status, saved = request(server, "POST", "/api/save", draft)
        assert status == 200
        assert saved["applied"] is False
        assert "did not apply" in saved["persistence"]
    finally:
        stop_server(server, thread)


def test_the_editor_never_claims_the_screens_updated_when_a_paint_failed(tmp_path: Path) -> None:
    """A save that lands on disk is not a claim that the deck's LCD keys changed."""
    path = write_config(tmp_path, FIXTURE)
    reason: dict[str, str | None] = {"value": "device write failed"}
    server, thread = running_server(config=path, apply=lambda: True, paint_status=lambda: reason["value"])
    try:
        status, body = request(server, "GET", "/api/state")
        assert status == 200
        assert body["screens_updated"] is False
        assert body["paint_error"] == "device write failed"

        _, current = request(server, "GET", "/api/state")
        draft = current["draft"]
        draft["bindings"][1] = "dim"
        status, saved = request(server, "POST", "/api/save", draft)
        assert status == 200
        assert saved["applied"] is True
        # "applied" is about the bindings; the note says the panel did not change.
        assert "applied to the running deck" in saved["persistence"]
        assert "not updated: device write failed" in saved["persistence"]

        # Once a paint lands again the editor reports the screens as current.
        reason["value"] = None
        _, body = request(server, "GET", "/api/state")
        assert body["screens_updated"] is True
        assert body["paint_error"] is None
    finally:
        stop_server(server, thread)


def test_an_editor_without_a_paint_report_makes_no_claim_about_the_screens(tmp_path: Path) -> None:
    """The standalone server (no daemon attached) must not invent a screen state."""
    path = write_config(tmp_path, FIXTURE)
    server, thread = running_server(config=path)
    try:
        _, body = request(server, "GET", "/api/state")
        assert "screens_updated" not in body
        assert "paint_error" not in body
    finally:
        stop_server(server, thread)


# -- Failure honesty: nothing is written, the server keeps serving -------------


def test_invalid_draft_leaves_the_config_untouched_and_the_server_serving(tmp_path: Path) -> None:
    path = write_config(tmp_path, FIXTURE)
    before = digest(path)
    apply_calls: list[int] = []
    server, thread = running_server(config=path, apply=lambda: apply_calls.append(1) or True)
    try:
        # A draft that fails validation: an unsafe action kind.
        bad = {"actions": [{"id": "boom", "kind": "shell", "shell": ["/bin/sh"]}], "bindings": [None] * 6, "dials": [None] * 8, "menus": []}
        status, body = request(server, "POST", "/api/save", bad)
        assert status == 400
        assert "safe" in body["error"]
        # Nothing written, and the daemon was never asked to rebind.
        assert digest(path) == before
        assert apply_calls == []
        # The server is still up and serving the old configuration.
        status, body = request(server, "GET", "/api/state")
        assert status == 200
        assert body["draft"]["bindings"] == ["copy", None, None, None, None, None]
    finally:
        stop_server(server, thread)
    assert digest(path) == before


def test_draft_route_validates_in_memory_only_and_never_writes(tmp_path: Path) -> None:
    path = write_config(tmp_path, FIXTURE)
    before = digest(path)
    server, thread = running_server(config=path)
    try:
        _, current = request(server, "GET", "/api/state")
        draft = current["draft"]
        draft["bindings"][0] = "dim"
        status, body = request(server, "POST", "/api/draft", draft)
        assert status == 200
        assert "in memory only" in body["persistence"]
        assert "not saved" in body["persistence"]
    finally:
        stop_server(server, thread)
    # The draft route is validation only: the on-disk config is byte-identical.
    assert digest(path) == before


def test_save_refuses_to_overwrite_a_non_editable_lcd_binding(tmp_path: Path) -> None:
    # key0 holds an inline shell binding the editor cannot display or edit.
    config_text = '[actions.copy]\naction = "keys"\nkeys = ["ctrl", "c"]\n\n[bindings.key0]\naction = "shell"\nshell = ["/bin/true"]\n'
    path = write_config(tmp_path, config_text)
    before = digest(path)
    server, thread = running_server(config=path)
    try:
        _, current = request(server, "GET", "/api/state")
        draft = current["draft"]
        draft["bindings"][0] = "copy"
        status, body = request(server, "POST", "/api/save", draft)
        assert status == 400
        assert "not an editable safe action" in body["error"]
    finally:
        stop_server(server, thread)
    assert digest(path) == before


def test_save_write_failure_keeps_the_original_and_leaves_no_temp(tmp_path: Path, monkeypatch) -> None:
    path = write_config(tmp_path, FIXTURE)
    before = path.read_text()
    import akp03.webui as webui_module

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(webui_module.os, "replace", explode)
    server, thread = running_server(config=path)
    try:
        _, current = request(server, "GET", "/api/state")
        draft = current["draft"]
        draft["bindings"][0] = "dim"
        status, body = request(server, "POST", "/api/save", draft)
        assert status == 500
        assert "could not write" in body["error"]
    finally:
        stop_server(server, thread)
    assert path.read_text() == before
    assert temp_files(path) == []


# -- The merge is a pure function and refuses erasure --------------------------


def test_merge_rejects_a_new_id_that_collides_with_a_non_editable_action(tmp_path: Path) -> None:
    # 'hold' already exists as an unsafe (keys_hold) action; reusing the id is refused.
    config_text = '[actions.hold]\naction = "keys_hold"\nkeys_hold = ["ctrl"]\n'
    payload = {"actions": [{"id": "hold", "kind": "keys", "keys": ["a"]}], "bindings": [None] * 6, "dials": [None] * 8, "menus": []}
    draft = parse_draft(payload)
    try:
        merge_draft_into_config(config_text, draft)
    except Exception as error:  # ControlPlaneEditorError
        assert "non-editable actions" in str(error)
    else:
        raise AssertionError("colliding action id was accepted")


# -- The shipped page carries a real Save control -----------------------------


def test_editor_page_ships_a_save_control_with_honest_states() -> None:
    from importlib.resources import files

    assets = files("akp03").joinpath("webui_assets")
    page = assets.joinpath("index.html").read_text()
    script = assets.joinpath("app.js").read_text()
    css = support.stylesheets(assets)

    assert 'id="saveChanges"' in page and "Save to deck" in page
    assert 'id="discardChanges"' in page and "Discard changes" in page
    assert 'id="sourcePill"' in page
    assert 'role="status"' in page and 'aria-live="polite"' in page
    # The script talks to the save route and covers every honest state.
    assert "fetch('/api/save'" in script
    assert "saveToDeck" in script and "discardChanges" in script
    assert "Unsaved changes" in script
    assert "Saving…" in script
    assert "Not saved:" in script
    assert "aria-busy" in script
    # Warm styling, with the save bar present.
    assert ".save-bar" in css


# -- End to end: the RUNNING daemon switches behaviour on save, no restart ------


class _FakeDeck:
    """A deck stub whose input path the test can fire directly."""

    publishes_feed_state = False

    def __init__(self) -> None:
        self.event = None

    def on_event(self, callback):
        self.event = callback
        return callback

    def on_disconnect(self, callback):
        self.disconnect = callback
        return callback

    def open(self):
        return self

    def close(self) -> None:
        pass

    def clear(self, key=None) -> None:
        pass

    def set_key_image(self, key, image) -> None:
        pass

    def set_brightness(self, level: int) -> None:
        pass


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _request_port(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    return response.status, data


def test_running_daemon_picks_up_a_saved_binding_without_a_restart(tmp_path: Path, caplog) -> None:
    from akp03.backend import LogBackend

    config_text = '[daemon]\ndevice = "/dev/hidraw0"\npoll_seconds = 0.1\n\n[actions.ping]\naction = "keys"\nkeys = ["ctrl", "alt", "p"]\n'
    path = write_config(tmp_path, config_text)
    created: list[_FakeDeck] = []
    feed = ControlFeed(connected=False, phase="waiting")
    daemon = Daemon(
        path,
        state_path=tmp_path / "state.json",
        deck_factory=lambda device: created.append(_FakeDeck()) or created[-1],  # type: ignore[arg-type,return-value]
        backend=LogBackend(logging.getLogger("akp03d.keys")),
        feed=feed,
    )
    port = _free_port()
    sock = tmp_path / "daemon.sock"
    thread = threading.Thread(target=lambda: daemon.run(sock, editor_port=port), name="live-apply-daemon")
    thread.start()
    try:
        assert _wait_for_port(port), "daemon never hosted the editor"
        assert _wait_until(lambda: daemon.deck is not None and bool(created)), "deck never connected"
        deck = created[-1]
        assert deck.event is not None

        caplog.set_level(logging.INFO, logger="akp03d.keys")
        # BEFORE the save: key0 has no binding, so a press runs nothing.
        deck.event(Event("key", "key0", True, None, b""))
        assert "TAP ctrl+alt+p" not in caplog.text

        # Save a binding for key0 through the editor's own save route.
        status, body = _request_port(port, "GET", "/api/state")
        assert status == 200
        draft = body["draft"]
        assert draft["bindings"] == [None] * 6
        draft["bindings"][0] = "ping"
        status, saved = _request_port(port, "POST", "/api/save", draft)
        assert status == 200, saved
        assert saved["path"] == str(path)
        assert saved["applied"] is True

        # AFTER the save: the SAME running process now runs the new binding,
        # with no restart of the daemon or its deck.
        caplog.clear()
        deck.event(Event("key", "key0", True, None, b""))
        assert _wait_until(lambda: "TAP ctrl+alt+p" in caplog.text), caplog.text
        assert created[-1] is deck  # the deck was rebound in place, not reconnected
    finally:
        daemon.stop.set()
        thread.join(timeout=6)
    assert not thread.is_alive()


# -- The editor must not be cached, and the picker must survive a real click ----


def test_editor_refuses_to_be_cached_by_the_browser(tmp_path: Path) -> None:
    """A fix sitting in the browser's cache looks exactly like a fix that never
    landed, so the page and its assets are never cacheable."""
    path = write_config(tmp_path, FIXTURE)
    server, thread = running_server(config=path)
    try:
        for target in ("/", "/assets/app.js", "/assets/overrides.css"):
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            connection.request("GET", target)
            response = connection.getresponse()
            response.read()
            connection.close()
            assert response.status == 200, target
            assert response.getheader("Cache-Control") == "no-store", target
    finally:
        stop_server(server, thread)


def test_the_action_chooser_is_a_row_in_the_key_panel_and_not_a_popup(tmp_path: Path) -> None:
    """The chooser used to be a popup that closed itself on `focusout`, and Safari
    does not move focus when a checkbox is clicked, so ticking a modifier dismissed
    the form mid-keystroke. It is now a row inside the key panel: only the row's own
    button toggles it, so a click anywhere inside it cannot dismiss it mid-edit.
    Assert the popup and its focus guard do not come back, and that the modifier
    form and the ability to make your own action both still exist."""
    from importlib.resources import files

    script = files("akp03").joinpath("webui_assets/app.js").read_text()
    page = files("akp03").joinpath("webui_assets/index.html").read_text()
    panel = files("akp03").joinpath("webui_assets/keypanel.js").read_text()
    assert "deckActionPicker" not in script
    assert "deckActionPicker" not in page
    assert "next === document.body" not in script
    assert "submitNewAction" not in script

    # The choices live in the panel component, opened by their own row: the shell and
    # the deck's page hold a place for a panel, not the panel's markup.
    assert 'data-panel="action-choices"' in panel and 'data-panel="icon-choices"' in panel
    assert 'data-panel-open="action"' in panel and 'data-panel-open="icon"' in panel
    assert 'id="actionChoices"' not in page and 'id="iconChoices"' not in page
    assert "function closePanelChoosers(root)" in panel
    # Only the row buttons toggle a list: nothing closes it as a side effect of a
    # click inside it.  One row is open at a time, and the state says which.
    # A dial's three rows share one list, so the row that opened it is part of what
    # is open; every other target has a single row and says so.
    assert "const same = state.open === wanted && state.openSlot === slot;" in panel
    assert "state.open = same ? null : wanted;" in panel
    assert "focusout" not in panel
    # Your own action, with its modifiers, is made in the panel.
    assert 'data-new-form' in panel and 'data-new-add' in panel
    assert 'data-modifier=' in panel
