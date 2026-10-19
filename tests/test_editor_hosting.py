"""The daemon hosts the editor in-process and streams its own live feed.

These tests cover the vertical slice: ``akp03d`` serves the browser editor on
loopback with the daemon's real :class:`akp03.feed.ControlFeed`, the editor's
``/api/events`` therefore shows true deck state and real control frames, the
hosted editor stops with the daemon without leaking a thread, and a state-less
dial turn (the frozen protocol's single-shot rotation) reaches the feed.
"""
from __future__ import annotations
from support import wait_for_port, wait_until

import json
import queue
import socket
import threading
import time
from pathlib import Path

from akp03.daemon import Daemon, start_editor_server
from akp03.device import Event
from akp03.feed import ControlFeed
from akp03.hidstream import HidStreamDeck, event_from_control
from akp03.webui import make_server

import support

# A tiny helper that reports a healthy deck, presses key0, and a state-less
# dial turn, then idles.  It never exits, so the backend stays "ready", and it
# answers the paint stdin protocol faithfully so a paint never waits.
HELPER = '''#!/usr/bin/env python3
import base64, json, sys, threading, time
for payload in [
    {"event": "starting"},
    {"event": "waiting"},
    {"event": "ready"},
    {"event": "control", "control": "key0", "state": "press"},
    {"event": "control", "control": "topClockwise"},
]:
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()
    time.sleep(0.03)

def answer_paints():
    for raw in sys.stdin:
        try:
            command = json.loads(raw)
        except ValueError:
            continue
        if isinstance(command, dict) and command.get("command") == "paint":
            try:
                size = len(base64.b64decode(command.get("payload", ""), validate=True))
            except Exception:
                size = 0
            sys.stdout.write(json.dumps({"result": "ok", "command": "paint", "bytes": size}) + "\\n")
            sys.stdout.flush()

threading.Thread(target=answer_paints, daemon=True).start()

while True:
    time.sleep(0.5)
'''


def write_helper(directory: Path, source: str, name: str = "editor_helper.py") -> Path:
    path = directory / name
    path.write_text(source)
    path.chmod(0o755)
    return path


def config(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def open_sse(port: int):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n")
    reader = sock.makefile("rb")
    status = reader.readline()
    while True:
        line = reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    return sock, reader, status


def read_frame(reader) -> tuple[str, dict]:
    event, data = None, []
    while True:
        raw = reader.readline()
        if raw == b"":
            raise AssertionError("SSE stream closed before a frame was delivered")
        line = raw.rstrip(b"\r\n")
        if line == b"":
            if event is None:
                continue
            return event, json.loads("".join(data))
        if line.startswith(b":"):
            continue
        if line.startswith(b"event:"):
            event = line.split(b":", 1)[1].strip().decode()
        elif line.startswith(b"data:"):
            data.append(line.split(b":", 1)[1].strip().decode())


def collect_until(reader, predicate, *, timeout: float = 4.0) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frames.append(read_frame(reader))
        if predicate(frames):
            break
    return frames


# -- The daemon hosts the editor on its own live feed --------------------------


def test_daemon_serves_the_editor_streaming_its_own_live_feed(tmp_path: Path) -> None:
    helper = write_helper(tmp_path, HELPER)
    path = tmp_path / "config.toml"
    config(path, "")
    feed = ControlFeed(connected=False, phase="waiting")
    daemon = Daemon(
        path,
        state_path=tmp_path / "state",
        deck_factory=lambda device: HidStreamDeck(device, helper=str(helper), feed=feed),
        feed=feed,
    )
    sock_path = tmp_path / "daemon.sock"
    port = free_port()
    thread = threading.Thread(target=lambda: daemon.run(sock_path, editor_port=port), name="test-daemon")
    thread.start()
    try:
        assert wait_for_port(port), "daemon-hosted editor never accepted a connection"
        client, reader, status = open_sse(port)
        assert status.startswith(b"HTTP/1.1 200")
        dial = ("control", {"control": "topClockwise", "state": "press"})
        frames = collect_until(reader, lambda collected: dial in collected)
        client.close()
        # The daemon passes ITS feed to both the deck and the editor, so the
        # editor streams the real deck state (ready) and real control frames --
        # a detached feed could only ever show a frozen "waiting" with no events.
        assert ("state", {"connected": True, "phase": "ready"}) in frames
        assert ("control", {"control": "key0", "state": "press"}) in frames
        # A state-less dial turn reaches the editor too, as one press frame.
        assert dial in frames
    finally:
        daemon.stop.set()
        thread.join(timeout=4)
    assert not thread.is_alive()
    # The hosted editor thread is gone, so nothing was leaked.
    assert not any(t.name == "akp03-editor" for t in threading.enumerate())


def test_editor_port_zero_disables_hosting(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config(path, "")
    feed = ControlFeed()
    daemon = Daemon(path, state_path=tmp_path / "state", feed=feed)
    sock_path = tmp_path / "daemon.sock"
    thread = threading.Thread(target=lambda: daemon.run(sock_path, editor_port=0), name="test-daemon-headless")
    thread.start()
    try:
        assert wait_until(sock_path.exists)
        # With hosting disabled no editor thread is started at all.
        assert not any(t.name == "akp03-editor" for t in threading.enumerate())
    finally:
        daemon.stop.set()
        thread.join(timeout=4)
    assert not thread.is_alive()


def test_standalone_editor_entry_point_still_serves_the_same_endpoints() -> None:
    """The development entry point keeps working alongside daemon hosting."""
    server = make_server(0, feed=ControlFeed(connected=False, phase="waiting"))
    thread = threading.Thread(target=server.serve_forever, name="standalone-editor", daemon=True)
    thread.start()
    try:
        sock, reader, status = open_sse(server.server_address[1])
        assert status.startswith(b"HTTP/1.1 200")
        assert read_frame(reader) == ("state", {"connected": False, "phase": "waiting"})
        sock.close()
    finally:
        server.shutdown()
        thread.join(timeout=4)
        server.server_close()


def test_editor_server_stop_is_idempotent_and_releases_the_thread() -> None:
    editor = start_editor_server(0, config=None, feed=ControlFeed())
    assert editor is not None
    assert any(t.name == "akp03-editor" for t in threading.enumerate())
    editor.stop()
    editor.stop()  # a second stop must not raise
    assert not any(t.name == "akp03-editor" for t in threading.enumerate())


# -- A state-less dial turn is the single-shot edge ----------------------------


def test_stateless_dial_turn_is_a_single_press_edge() -> None:
    # The frozen helper omits `state` for a rotation; that bare turn is the edge.
    assert event_from_control("topClockwise", None) == Event("knob_turn", "top", None, 1, b"")
    assert event_from_control("bottomRightCounterClockwise", None) == Event("knob_turn", "bottom_right", None, -1, b"")
    # A rotation release is never a second edge.
    assert event_from_control("topClockwise", "release") is None


def test_stateless_dial_turn_reaches_the_feed_and_the_daemon() -> None:
    feed = ControlFeed()
    subscription = feed.subscribe()
    subscription.get(timeout=1)  # discard the seeded state frame

    deck = HidStreamDeck("/dev/ignored", feed=feed)
    events: list[Event] = []
    deck.on_event(events.append)

    deck._deliver_control("topClockwise", None)  # type: ignore[attr-defined]

    frames = []
    while True:
        try:
            frames.append(subscription.get(timeout=0.1))
        except queue.Empty:
            break
    assert [(f.event, f.data) for f in frames] == [("control", {"control": "topClockwise", "state": "press"})]
    assert events == [Event("knob_turn", "top", None, 1, b"")]
    deck.close()


# -- The browser front-end uses the feed and shows readable tiles --------------


def test_editor_ships_live_feed_ui_and_readable_tile_rules() -> None:
    from importlib.resources import files

    assets = files("akp03").joinpath("webui_assets")
    page = assets.joinpath("index.html").read_text()
    script = assets.joinpath("app.js").read_text()
    css = support.stylesheets(assets)

    # Live status element and per-control hooks the feed frames target.
    assert 'id="deckLive"' in page and 'id="deckState"' in page
    # The deck's controls are drawn by the shared drawing, which the page mounts.
    drawing = assets.joinpath("deckdraw.js").read_text()
    assert 'id="deckDevice"' in page and "mountDeckDrawing($('#deckDevice')" in script
    for control in ("button6", "button8", "button7", "top", "bottom_left", "bottom_right"):
        assert f"control: '{control}'" in drawing, control

    # The feed subscription and the press/release handling.
    assert "new EventSource('/api/events')" in script
    assert "addEventListener('control'" in script and "addEventListener('state'" in script
    assert "releaseControl" in script and "pressControl" in script
    assert "PRESS_FADE_MS" in script and "repaintActiveControls" in script

    # Press highlights for all three control families.
    assert "#deck .key.pressed" in css
    assert "#deck .button-cap.pressed" in css
    assert "#deck .dial.pressed" in css
    assert ".live-status.ok .status-dot" in css
    # Tile copy must fit inside the tile: the label is one line that clears the
    # tile's bottom edge, and the shortcut belongs to the panel beside it.  It was
    # a third line on the tile, and the tile's `overflow: hidden` sliced it off
    # along with the bottom of the label.
    # Tile copy fits inside the tile because the drawing clips the face's own words to one
    # line: the shortcut belongs to the panel beside it, never to a third line on the tile.
    drawing = assets.joinpath("deckdraw.css").read_text()
    assert ".key {" in drawing and "overflow: hidden;" in drawing
    # One line, never wrapped: a key is a fixed size, so a name that wraps moves the tile's
    # contents about and stops matching its neighbours.
    assert "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" in drawing
    assert "-webkit-line-clamp" not in drawing
