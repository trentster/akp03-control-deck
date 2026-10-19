"""The daemon's honest report of whether it can actually send a shortcut.

A key bound to a plain keystroke does *nothing at all* while macOS withholds the
Accessibility (TCC) grant: the helper fails closed and posts no events.  That is
correct behaviour and useless on its own, so the daemon must be able to say it
before anyone presses the key, and offer the one action that fixes it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from akp03 import backend as backend_module
from akp03.backend import MacKeyBackend


class FakeStream:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = list(lines or [])
        self.written: list[str] = []

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def readline(self) -> str:
        return self.lines.pop(0) if self.lines else ""

    def __iter__(self) -> "FakeStream":
        return self

    def __next__(self) -> str:
        if not self.lines:
            raise StopIteration
        return self.lines.pop(0)

    def close(self) -> None:
        return None


class FakeProcess:
    """Just enough of ``subprocess.Popen[str]`` for the helper's line protocol."""

    def __init__(self, replies: list[str]) -> None:
        self.stdin = FakeStream()
        self.stdout = FakeStream(replies)
        self.stderr = FakeStream()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


def helper_backend(replies: list[str], **kwargs: Any) -> MacKeyBackend:
    return MacKeyBackend(
        executable="AKP03KeyInject",
        popen=lambda *args, **inner: FakeProcess(list(replies)),  # type: ignore[arg-type]
        which=lambda name: name,
        **kwargs,
    )


def test_check_accessibility_answers_before_the_first_press() -> None:
    """The status must be knowable without pressing a key that does nothing."""
    backend = helper_backend(['{"result":"pong","accessibility":false,"dry_run":false}'])
    assert backend.check_accessibility() is False
    # Asking is idempotent: the helper is reused, not restarted per question.
    assert backend.check_accessibility() is False


def test_check_accessibility_is_true_when_the_grant_is_present() -> None:
    backend = helper_backend(['{"result":"pong","accessibility":true,"dry_run":false}'])
    assert backend.check_accessibility() is True


def test_a_missing_grant_is_logged_once_with_the_fix(caplog: pytest.LogCaptureFixture) -> None:
    """One actionable line, not a cryptic failure on every press."""
    backend = helper_backend(['{"result":"pong","accessibility":false,"dry_run":false}'])
    with caplog.at_level(logging.ERROR, logger="akp03d.keys"):
        backend.check_accessibility()
        backend.check_accessibility()
    errors = [record for record in caplog.records if "Accessibility" in record.getMessage()]
    assert len(errors) == 1
    assert "System Settings" in errors[0].getMessage()


def test_request_accessibility_uses_the_documented_helper_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        seen["argv"] = argv
        return SimpleNamespace(
            stdout='{"result":"accessibility-request","granted":true}\n', stderr="", returncode=0
        )

    monkeypatch.setattr(backend_module.subprocess, "run", fake_run)
    backend = helper_backend([])
    result = backend.request_accessibility()
    assert seen["argv"][-1] == "--request-accessibility"
    assert result["granted"] is True
    # A grant that lands immediately is remembered, so nothing re-nags.
    assert backend.accessibility is True


def test_request_accessibility_reports_an_unconfirmed_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backend_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout='{"result":"accessibility-request","granted":false}\n', stderr="", returncode=0
        ),
    )
    backend = helper_backend([])
    result = backend.request_accessibility()
    assert result["granted"] is False
    assert "accessibility-request" in result["detail"]
    assert backend.accessibility is None


def test_the_editor_reports_the_injection_status_it_is_given() -> None:
    """``/api/state`` carries the daemon's ability to send keys, not a guess."""
    import threading
    from http.client import HTTPConnection

    from akp03.webui import make_server

    server = make_server(
        0,
        injection_status=lambda: {
            "permitted": False,
            "reason": "accessibility permission not granted",
            "can_request": True,
        },
        request_permission=lambda: {"granted": False, "detail": "waiting for macOS"},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        connection.request("GET", "/api/state")
        body = json.loads(connection.getresponse().read())
        connection.close()
        assert body["injection"]["permitted"] is False
        assert body["injection"]["can_request"] is True

        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        connection.request("POST", "/api/permission/request")
        asked = json.loads(connection.getresponse().read())
        connection.close()
        assert asked == {"supported": True, "granted": False, "detail": "waiting for macOS"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_key_panel_names_the_problem_and_offers_the_fix() -> None:
    """The page that binds keys is where the consequence has to be visible."""
    assets = Path(__file__).resolve().parent.parent / "akp03" / "webui_assets"
    page = (assets / "index.html").read_text(encoding="utf-8")
    script = (assets / "app.js").read_text(encoding="utf-8")
    assert 'id="permissionBanner"' in page
    assert 'id="permissionRequest"' in page
    assert "renderPermission" in script
    assert "/api/permission/request" in script
    # It must name what stops working, in the user's terms, not the TCC status code.
    assert "Mission Control" in script


def test_the_shortcut_field_accepts_keys_that_cannot_be_typed() -> None:
    """⌃↑ could not be entered at all while the field was one character wide.

    "up" is two letters, so the panel stored ⌃U instead — a different shortcut
    that does nothing. The field must take the key you press, and must be able to
    name every key the injector can send.
    """
    assets = Path(__file__).resolve().parent.parent / "akp03" / "webui_assets"
    script = (assets / "keypanel.js").read_text(encoding="utf-8")
    # The key half of a shortcut is tiles you click, not a box with a text cursor.
    assert "key-chip" in script
    assert 'data-key="${token}"' in script
    for tile in ("['up', '↑ up']", "['down', '↓ down']", "['left', '← left']", "['right', '→ right']",
                 "['enter', '⏎ return']", "['space', 'space']", "['tab', 'tab']",
                 "['escape', 'esc']", "['backspace', '⌫ delete']"):
        assert tile in script, tile
    assert "FUNCTION_KEYS" in script and "length: 12" in script
    # Clicking lights exactly one tile, and that choice is what gets saved.
    assert "other === chip" in script
    assert "chosenKey" in script
    # Pressing a chord that is already set must confirm, not sit silent.
    assert "already runs" in script
    # Selected must look selected: blue fill, blue border, same as the modifiers.  The
    # blue is the palette's own name for it now, so the two stay the same blue by
    # construction rather than by two people matching hex codes.
    css = (assets / "keypanel.css").read_text(encoding="utf-8")
    assert ".key-panel .modifier.on," in css
    selected = css.split(".key-panel .modifier.on,")[1].split("}")[0]
    assert "background: var(--key-blue)" in selected, selected
    assert "border-color: var(--key-blue)" in selected, selected
    assert ".key-panel .key-chip.on" in selected, selected
    palette = (assets / "app.css").read_text(encoding="utf-8")
    assert "--key-blue:#173f5f;" in palette, "and the palette still holds that blue"


def test_the_two_page_scripts_share_one_global_scope_without_collisions() -> None:
    """A name declared twice across app.js and keypanel.js kills the second file.

    They are both classic scripts, so their top-level declarations share one
    global scope. Re-declaring a `const` is a SyntaxError that stops the whole
    file — the page then says "renderPanelRows is not defined" and nothing in the
    panel works. Node checks each file separately, so only this catches it.
    """
    import re

    assets = Path(__file__).resolve().parent.parent / "akp03" / "webui_assets"
    declaration = re.compile(r"^(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", re.M)
    seen: dict[str, str] = {}
    for asset in sorted(assets.glob("*.js")):
        for name in set(declaration.findall(asset.read_text(encoding="utf-8"))):
            assert name not in seen, (
                f"“{name}” is declared in both {seen[name]} and {asset.name}; "
                "the browser rejects the second script entirely"
            )
            seen[name] = asset.name



def test_the_editor_can_run_one_action_but_nothing_arbitrary() -> None:
    """“Test it now” runs a configured action — it is not a way to send any keys.

    Bounded on purpose: the editor asks for an action by name, and the daemon
    answers only for actions the configuration already declares.
    """
    import threading
    from http.client import HTTPConnection

    from akp03.webui import make_server

    asked: list[str] = []

    def try_action(action_id: str) -> dict[str, Any]:
        asked.append(action_id)
        if action_id == "mission-control":
            return {"ok": True, "reason": None}
        return {"ok": False, "reason": f"no action named “{action_id}”"}

    server = make_server(0, try_action=try_action)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        connection.request("POST", "/api/action/test", body=json.dumps({"action": "mission-control"}),
                           headers={"Content-Type": "application/json"})
        good = json.loads(connection.getresponse().read())
        connection.close()
        assert good == {"ok": True, "reason": None}

        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        connection.request("POST", "/api/action/test", body=json.dumps({"action": "rm -rf /"}),
                           headers={"Content-Type": "application/json"})
        bad = json.loads(connection.getresponse().read())
        connection.close()
        assert bad["ok"] is False and "no action named" in bad["reason"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_an_existing_action_can_be_run_renamed_or_deleted_from_the_panel() -> None:
    """Editing what is already bound, right where it is bound.

    Someone who wants to look at the action on a key must not have to go hunting
    for a separate page: the three things you can do to it sit in the card that
    names it, and Test is reachable from the resting view too, because asking
    "does this work?" is not an edit.
    """
    assets = Path(__file__).resolve().parent.parent / "akp03" / "webui_assets"
    page = (assets / "index.html").read_text(encoding="utf-8")
    script = (assets / "keypanel.js").read_text(encoding="utf-8")
    css = (assets / "keypanel.css").read_text(encoding="utf-8")
    # The strip is one generated component now, shared with the Menus panel, so it is
    # asserted where it is made rather than in whichever page happens to host it.
    for control in ('done', 'rename', 'delete'):
        assert f'data-tool="{control}"' in script, control
    assert 'data-tool="rename-form"' in script
    assert 'class="rename-name"' in script
    # The tools are rendered for the view model of the whole panel, which is what
    # tells them whether the assignment is an action at all.
    assert "function paintToolStrip(root, view, target" in script
    assert "/api/action/test" in script
    # A withheld button explains itself instead of quietly not being there.
    assert "Ready-made actions come with the deck" in script
    # A rename follows through to every key and menu item that used the action.
    assert "binding === actionId ? next" in script
    assert "item.action === actionId" in script
    # Renaming a ready-made action makes it yours first, so the edit sticks.
    assert "...ready, id: next" in script
    # Deleting anything still in use is refused, and names what is using it.
    assert "still used by" in script and "nothing was removed" in script
    # `display: flex` must not defeat [hidden] - this panel's recurring trap.
    assert ".key-panel .action-tools[hidden]" in css
