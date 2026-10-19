"""The editor's mirror of what the deck is actually showing.

The daemon publishes `screens` frames describing its own paint - text, artwork,
menu page, dial highlight - and serves one key's face as a PNG.  These tests hold
that description to what the daemon really puts on the keys, because the whole
point of the mirror is that the browser never has to guess.
"""
from __future__ import annotations
from support import start_server

import io
import json
import socket
import threading
from http.client import HTTPConnection
from pathlib import Path
from typing import Sequence

import pytest
from PIL import Image

from akp03.daemon import Daemon
from akp03.device import Event
from akp03.feed import ControlFeed
from akp03.images import to_jpeg
SELECTED = "#173f5f"

CONFIG = '''[actions.undo]
action = "keys"
keys = ["cmd", "z"]
[actions.copy]
action = "keys"
keys = ["cmd", "c"]
[actions.youtube]
action = "menu"
menu = "youtube"
[labels]
key5 = "Find"
[bindings.key0]
action_id = "undo"
[bindings.key1]
action_id = "youtube"
[custom_menus.youtube]
root = "home"
[[custom_menus.youtube.pages]]
id = "home"
title = "YouTube"
items = [{ key = 0, label = "Rewind", action = "undo" }, { key = 1, label = "Copy", action = "copy" }]
'''

WITH_ART = CONFIG + '''
[images]
key0 = "key0.jpg"
'''

ART_ON_EVERY_KEY = CONFIG + '''
[images]
''' + "".join(f'key{key} = "key{key}.jpg"\n' for key in range(6))


class FakeDeck:
    def __init__(self, _path: str):
        self.calls: list[tuple] = []

    def on_event(self, callback):
        self.event = callback

    def on_disconnect(self, callback):
        self.disconnect = callback

    def open(self):
        return self

    def close(self):
        return None

    def clear(self, key=None):
        self.calls.append(("clear", key))

    def set_key_image(self, key, source):
        self.calls.append(("image", key))

    def set_brightness(self, level):
        self.calls.append(("brightness", level))


def write_config(tmp_path: Path, text: str = CONFIG, *, art_for: Sequence[int] | None = None) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    # The daemon only accepts a regular 0600 config, exactly as it writes one.
    path.chmod(0o600)
    for key in art_for or ():
        images = tmp_path / "images"
        images.mkdir(mode=0o700, exist_ok=True)
        target = images / f"key{key}.jpg"
        target.write_bytes(to_jpeg(Image.new("RGB", (60, 60), "#123456")))
        target.chmod(0o600)
    return path


def running_daemon(tmp_path: Path, text: str = CONFIG, *, art_for: Sequence[int] | None = None) -> Daemon:
    daemon = Daemon(write_config(tmp_path, text, art_for=art_for), state_path=tmp_path / "state",
                    deck_factory=lambda path: FakeDeck(path))
    assert daemon.reload() and daemon.connect()
    return daemon


def frame_of(daemon: Daemon, key: int) -> dict:
    return daemon.screen_description()["keys"][key]


# --- the description the daemon publishes ------------------------------------

def test_idle_description_matches_the_keys(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    screens = daemon.screen_description()

    assert screens["layer"] == {"kind": "idle"}
    assert [entry["key"] for entry in screens["keys"]] == [0, 1, 2, 3, 4, 5]

    # A key bound to a named action shows that action's name, and nothing is
    # highlighted while no menu is open.
    assert frame_of(daemon, 0)["kind"] == "text"
    assert frame_of(daemon, 0)["text"] == "Undo"
    assert frame_of(daemon, 0)["background"] == "black"
    assert frame_of(daemon, 0)["selected"] is False
    # The fallback [labels] entry is what an unbound key shows.
    assert frame_of(daemon, 5)["text"] == "Find"
    # A key with neither a binding nor a label is blank, not invented text.
    assert frame_of(daemon, 3)["kind"] == "blank"
    assert frame_of(daemon, 3)["text"] is None


def test_artwork_is_reported_with_a_revision_and_served_as_a_png(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path, WITH_ART, art_for=[0])
    entry = frame_of(daemon, 0)

    assert entry["kind"] == "image"
    assert entry["revision"], "artwork needs a revision so the editor can cache it"
    # The tile colour still describes the key, even under artwork.
    assert entry["background"] == "black"

    png = daemon.screen_face(0)
    assert png is not None and png.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(png)) as opened:
        assert opened.size == (60, 60) and opened.mode == "RGB"

    # A blank key has no face to serve, and no revision to publish.
    assert daemon.screen_face(3) is None
    assert frame_of(daemon, 3)["revision"] is None
    assert daemon.screen_face(9) is None


def test_revision_changes_only_when_the_artwork_does(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path, WITH_ART, art_for=[0])
    first = frame_of(daemon, 0)["revision"]
    assert frame_of(daemon, 0)["revision"] == first

    art = Path(tmp_path) / "images" / "key0.jpg"
    art.write_bytes(to_jpeg(Image.new("RGB", (60, 60), "#ff8800")))
    art.chmod(0o600)
    assert daemon.reload()
    assert frame_of(daemon, 0)["revision"] != first


def test_menu_layer_reports_the_page_and_the_dials_selection(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    assert daemon.screen_description()["layer"] == {"kind": "idle"}

    daemon.event(Event("key", "key1", True, None, b""))
    screens = daemon.screen_description()
    assert screens["layer"] == {"kind": "menu", "page": "home", "title": "YouTube", "selected_key": 0}
    assert frame_of(daemon, 0)["selected"] is True
    assert frame_of(daemon, 0)["background"] == SELECTED
    assert frame_of(daemon, 1)["selected"] is False

    # A dial turn moves the highlight, and the mirror follows it.
    daemon.event(Event("knob_turn", "top", None, 1, b""))
    screens = daemon.screen_description()
    assert screens["layer"]["selected_key"] == 1
    assert frame_of(daemon, 1)["selected"] is True and frame_of(daemon, 0)["selected"] is False

    # Leaving the menu (the root page's own Back) puts the idle labels back.
    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    assert daemon.custom_menu is None
    assert daemon.screen_description()["layer"] == {"kind": "idle"}


def test_artwork_never_hides_an_open_menu(tmp_path: Path) -> None:
    """Regression: with artwork on every key, an open menu must still be visible.

    The menu owns the keys while it is open.  If idle artwork wins instead, the
    panel keeps looking idle while the menu quietly takes the presses - pressing
    "New Window" then ran the menu's first item (Find) and the person could not
    tell why.
    """
    daemon = running_daemon(tmp_path, ART_ON_EVERY_KEY, art_for=range(6))
    assert frame_of(daemon, 0)["kind"] == "image", "idle keys show their artwork"

    daemon.event(Event("key", "key1", True, None, b""))  # key1 opens the menu

    screens = daemon.screen_description()
    assert screens["layer"]["kind"] == "menu"
    for key in range(6):
        entry = frame_of(daemon, key)
        # No key may still be wearing its idle picture while a menu is open:
        # on this page only keys 0 and 1 have an item, the rest go blank.
        assert entry["kind"] != "image", f"key{key} is still covered by idle artwork"
        assert entry["revision"] is None
        assert entry["kind"] == ("text" if key < 2 else "blank")
    assert frame_of(daemon, 0)["text"] == "Rewind"
    assert frame_of(daemon, 0)["selected"] is True
    assert frame_of(daemon, 0)["background"] == SELECTED

    # The face served for the mirror is the menu tile, not the picture.
    png = daemon.screen_face(0)
    assert png is not None
    with Image.open(io.BytesIO(png)) as opened:
        assert opened.getpixel((30, 30)) != (0x12, 0x34, 0x56)

    # Leaving the menu puts the artwork back on the keys.
    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    assert daemon.custom_menu is None
    assert frame_of(daemon, 0)["kind"] == "image"
    assert frame_of(daemon, 0)["revision"]


# --- the frames the editor consumes ------------------------------------------

def test_every_paint_change_publishes_a_screens_frame(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    subscription = daemon.feed.subscribe()
    assert subscription.get(timeout=1).event == "state"
    seeded = subscription.get(timeout=1)
    assert seeded.event == "screens"
    assert seeded.data["layer"] == {"kind": "idle"}

    # Opening a menu repaints the keys, and the editor is told without asking.
    daemon.event(Event("key", "key1", True, None, b""))
    frame = subscription.get(timeout=1)
    assert frame.event == "screens"
    assert frame.data["layer"]["kind"] == "menu"

    daemon.event(Event("knob_turn", "top", None, 1, b""))
    frame = subscription.get(timeout=1)
    assert frame.data["layer"]["selected_key"] == 1

    daemon.event(Event("knob_press", "bottom_left", True, None, b""))
    frame = subscription.get(timeout=1)
    assert frame.data["layer"] == {"kind": "idle"}


def test_daemon_publishes_a_snapshot_the_editor_can_replay(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    published = daemon.feed.screens_snapshot()
    assert published is not None and published["keys"][0]["text"] == "Undo"


def test_malformed_screens_are_ignored(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    # A caller cannot put the mirror into a nonsense state: junk is ignored.
    assert daemon.feed.publish_screens("not a description") is False  # type: ignore[arg-type]
    published = daemon.feed.screens_snapshot()
    assert published is not None and published["keys"][0]["text"] == "Undo"
    daemon.feed.publish_screens(None)
    assert daemon.feed.screens_snapshot() is None


# --- the editor routes -------------------------------------------------------


def stop_server(server, thread) -> None:
    server.shutdown()
    thread.join()
    server.server_close()


def get(server, path: str) -> tuple[int, str, bytes]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    connection.request("GET", path)
    response = connection.getresponse()
    status, content_type, body = response.status, response.getheader("Content-Type"), response.read()
    connection.close()
    return status, content_type or "", body


def test_screens_routes_describe_and_serve_the_panel(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    server, thread = start_server(feed=daemon.feed, screens=daemon.screen_description, face=daemon.screen_face)
    try:
        status, content_type, body = get(server, "/api/screens")
        assert status == 200 and content_type.startswith("application/json")
        payload = json.loads(body)
        assert payload["source"] == "daemon"
        assert len(payload["screens"]["keys"]) == 6

        status, content_type, body = get(server, "/api/screen/0.png")
        assert status == 200 and content_type == "image/png"
        with Image.open(io.BytesIO(body)) as opened:
            assert opened.size == (60, 60)

        # Without the extension is fine too; anything else is not a key.
        assert get(server, "/api/screen/0")[0] == 200
        for bad in ("/api/screen/9.png", "/api/screen/foo.png", "/api/screen/-1.png"):
            assert get(server, bad)[0] == 404, bad

        # A blank key says so rather than serving an empty picture.
        status, _, body = get(server, "/api/screen/3.png")
        assert status == 404 and b"blank" in body
    finally:
        stop_server(server, thread)


def test_screens_route_falls_back_to_the_last_published_frame(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    server, thread = start_server(feed=daemon.feed)
    try:
        status, _, body = get(server, "/api/screens")
        assert status == 200
        payload = json.loads(body)
        assert payload["source"] == "feed"
        assert payload["screens"]["keys"][0]["text"] == "Undo"
        # With no daemon attached there is no artwork to serve and no lie about it.
        status, _, body = get(server, "/api/screen/0.png")
        assert status == 404 and b"no deck attached" in body
    finally:
        stop_server(server, thread)


def test_sse_delivers_a_screens_frame_to_a_new_editor(tmp_path: Path) -> None:
    daemon = running_daemon(tmp_path)
    server, thread = start_server(feed=daemon.feed, screens=daemon.screen_description)
    try:
        sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
        sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n")
        reader = sock.makefile("rb")
        reader.readline()
        while reader.readline() not in (b"\r\n", b"\n", b""):
            pass

        assert read_frame(reader)[0] == "state"
        event, data = read_frame(reader)
        assert event == "screens"
        assert len(data["keys"]) == 6 and data["layer"] == {"kind": "idle"}

        daemon.event(Event("key", "key1", True, None, b""))
        event, data = read_frame(reader)
        assert event == "screens" and data["layer"]["title"] == "YouTube"
        sock.close()
    finally:
        stop_server(server, thread)


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


@pytest.mark.parametrize("path", ["/api/screens", "/api/screen/0.png"])
def test_read_routes_never_require_a_body(path: str, tmp_path: Path) -> None:
    """Both routes are plain reads: a GET with no payload must be enough."""
    server, thread = start_server(feed=ControlFeed())
    try:
        assert get(server, path)[0] in {200, 404}
    finally:
        stop_server(server, thread)
