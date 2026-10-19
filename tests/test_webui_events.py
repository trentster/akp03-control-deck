"""The read-only ``/api/events`` Server-Sent Events endpoint for the editor."""
from __future__ import annotations

import json
import socket
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

from akp03.feed import ControlFeed
from akp03.hidstream import HidStreamDeck
from akp03.webui import make_server

SAMPLE_BINDINGS = ["record", "marker", "dim", "studio", None, None]

MINIMAL_HELPER = '''#!/usr/bin/env python3
import json, sys, time
for payload in [
    {"event": "ready"},
    {"event": "control", "control": "key0", "state": "press"},
]:
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()
    time.sleep(0.05)
while True:
    time.sleep(0.5)
'''


def running_server(feed=None):
    server = make_server(0, feed=feed)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    return server, thread


def stop_server(server, thread) -> None:
    server.shutdown()
    thread.join()
    server.server_close()


def request(server, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    return response.status, data


def request_status(server, method: str, path: str, body: dict | None = None) -> int:
    """Return only the status, for endpoints that answer with an HTML error page."""
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
    status = connection.getresponse().status
    connection.close()
    return status


def open_sse(server):
    sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n")
    reader = sock.makefile("rb")
    status = reader.readline()
    headers: dict[str, str] = {}
    while True:
        line = reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode().partition(":")
        headers[name.strip().lower()] = value.strip()
    return sock, reader, status, headers


def read_frame(reader) -> tuple[str, dict]:
    event = None
    data: list[str] = []
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


def test_events_streams_initial_state_then_state_and_control_frames() -> None:
    feed = ControlFeed(connected=False, phase="waiting")
    server, thread = running_server(feed=feed)
    try:
        sock, reader, status, headers = open_sse(server)
        assert status.startswith(b"HTTP/1.1 200")
        assert headers["content-type"].startswith("text/event-stream")
        assert headers["cache-control"] == "no-store"
        # The very first frame is the current state, before anything changes.
        assert read_frame(reader) == ("state", {"connected": False, "phase": "waiting"})

        feed.set_state(connected=True, phase="ready")
        feed.publish_control("key0", "press")

        assert read_frame(reader) == ("state", {"connected": True, "phase": "ready"})
        assert read_frame(reader) == ("control", {"control": "key0", "state": "press"})
        sock.close()
    finally:
        stop_server(server, thread)


def test_default_server_reports_no_deck_attached() -> None:
    server, thread = running_server()
    try:
        sock, reader, _status, _headers = open_sse(server)
        assert read_frame(reader) == ("state", {"connected": False, "phase": "waiting"})
        sock.close()
    finally:
        stop_server(server, thread)


def test_events_is_get_only_and_existing_endpoints_are_unchanged() -> None:
    feed = ControlFeed()
    server, thread = running_server(feed=feed)
    try:
        status = request_status(server, "POST", "/api/events", {})
        assert status == 404

        status, body = request(server, "GET", "/api/state")
        assert status == 200
        assert body["draft"]["bindings"] == SAMPLE_BINDINGS

        draft = body["draft"]
        draft["bindings"][0] = "marker"
        status, updated = request(server, "POST", "/api/draft", draft)
        assert status == 200 and updated["draft"]["bindings"][0] == "marker"
    finally:
        stop_server(server, thread)


def test_editor_sees_live_deck_state_through_the_feed(tmp_path: Path) -> None:
    feed = ControlFeed(connected=False, phase="waiting")
    helper = tmp_path / "helper.py"
    helper.write_text(MINIMAL_HELPER)
    helper.chmod(0o755)
    deck = HidStreamDeck("/dev/ignored", helper=str(helper), feed=feed)
    server, thread = running_server(feed=feed)
    try:
        sock, reader, _status, _headers = open_sse(server)
        assert read_frame(reader) == ("state", {"connected": False, "phase": "waiting"})

        deck.open()
        frames = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            frames.append(read_frame(reader))
            if ("control", {"control": "key0", "state": "press"}) in frames:
                break

        assert ("state", {"connected": True, "phase": "ready"}) in frames
        assert ("control", {"control": "key0", "state": "press"}) in frames
        sock.close()
    finally:
        deck.close()
        stop_server(server, thread)
