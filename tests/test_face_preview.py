"""A menu button's key shows the face the deck will paint, not one of its own.

A button with no artwork shows its words on the glass - the deck draws the label
onto the tile.  The buttons in a menu's chain used to show no picture at all for
those, so one level down you could not see what a key looks like.  These tests hold
the preview to the deck's own renderer, so the two cannot disagree.
"""

from __future__ import annotations
from support import start_server

import io
import threading
from http.client import HTTPConnection
from pathlib import Path

from PIL import Image

from akp03 import faces
from akp03.config import LABEL_MAX_LENGTH, ConfigError, load_config
from akp03.daemon import Paint, _label_image

import support
ASSETS = Path(__file__).resolve().parents[1] / "akp03" / "webui_assets"


def asset(name: str) -> str:
    return (ASSETS / name).read_text()


def stop_server(server, thread) -> None:
    server.shutdown()
    thread.join()
    server.server_close()


def get(server, path: str) -> tuple[int, str, bytes]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    connection.request("GET", path)
    response = connection.getresponse()
    result = response.status, response.getheader("Content-Type") or "", response.read()
    connection.close()
    return result


def test_the_route_serves_the_deck_s_own_face() -> None:
    """Byte for byte the picture the daemon would paint on the key."""
    server, thread = start_server()
    try:
        status, content_type, body = get(server, "/api/label/0.png?text=Find")
        assert status == 200 and content_type == "image/png"
        assert body == faces.label_png(Paint(0, "Find"))
        with Image.open(io.BytesIO(body)) as opened:
            assert opened.size == (60, 60) and opened.mode == "RGB"

        # A label that has to wrap and shrink comes back exactly as the deck draws it.
        long = "a very long label that must wrap"
        assert get(server, f"/api/label/3.png?text={long.replace(' ', '%20')}")[2] == faces.label_png(Paint(3, long))
    finally:
        stop_server(server, thread)


def test_the_route_refuses_what_the_deck_would_refuse() -> None:
    """No preview may show a face the config would not accept for a real label."""
    server, thread = start_server()
    try:
        # Not a key, or no text at all.
        for bad in ("/api/label/9.png?text=Find", "/api/label/foo.png?text=Find", "/api/label/0.png"):
            assert get(server, bad)[0] in {400, 404}, bad
        # One character too long, and a control character, are both refused.
        assert get(server, f"/api/label/0.png?text={'x' * (LABEL_MAX_LENGTH + 1)}")[0] == 400
        assert get(server, "/api/label/0.png?text=two%0Awords")[0] == 400
        # The limit is the config's own, so the two cannot drift apart.
        assert get(server, f"/api/label/0.png?text={'x' * LABEL_MAX_LENGTH}")[0] == 200
    finally:
        stop_server(server, thread)


def test_the_limit_is_the_one_the_config_enforces(tmp_path: Path) -> None:
    """`LABEL_MAX_LENGTH` is the number config.py validates against, not a copy."""
    assert LABEL_MAX_LENGTH == 32
    text = (Path(__file__).resolve().parents[1] / "akp03" / "config.py").read_text()
    assert "LABEL_MAX_LENGTH = 32" in text
    assert "{LABEL_MAX_LENGTH}" in text, "the regex and the message must use the constant"
    long = "x" * (LABEL_MAX_LENGTH + 1)
    path = tmp_path / "config.toml"
    path.write_text('[labels]\nkey0 = "%s"\n' % long)
    path.chmod(0o600)  # the loader insists on a private config
    try:
        load_config(path)
    except ConfigError as error:
        assert str(LABEL_MAX_LENGTH) in str(error)
    else:  # pragma: no cover - the config must refuse an over-long label
        raise AssertionError("an over-long label was accepted")


def test_a_menu_button_shows_the_face_the_deck_will_paint() -> None:
    """The chain's buttons carry the face, fetched from the renderer, not a re-make."""
    flow = asset("flow.js")
    assert "function fxFaceSrc(item, key)" in flow
    # A picture when there is one, otherwise the label the deck draws.
    assert "if (item.icon) return artworkPreviewSrc(item.icon, key);" in flow
    assert "`/api/label/${key}.png?text=${encodeURIComponent(item.label)}`" in flow
    # Every button in the chain draws a face, not just the ones with artwork.  The
    # size is pinned because it is the difference between a face and a smudge: at
    # 22px the words the deck draws on a tile could not be read at all.
    assert 'class="fx-pic"><img src="${esc(face)}" alt="" width="40" height="40"' in flow
    assert "art: fxFaceSrc(item, key)," in flow
    assert "data-fx-slot=\"${menu.name}:${key}\"" in flow


def test_one_face_renderer_with_two_callers() -> None:
    """The device and the editor must not each grow their own renderer."""
    root = Path(__file__).resolve().parents[1] / "akp03"
    shared = (root / "faces.py").read_text()
    assert "def label_image(" in shared and "def label_png(" in shared
    # The daemon keeps its own surface, re-exported from the one renderer.
    daemon = (root / "daemon.py").read_text()
    assert "from .faces import Paint, label_image as _label_image" in daemon
    assert "def _label_image(" not in daemon, "the renderer was copied, not shared"
    # And the editor uses that same renderer for the preview.
    webui = (root / "webui.py").read_text()
    assert "from .faces import Paint, label_png" in webui
    assert "data = label_png(Paint(key[0], text))" in webui


def test_a_face_is_painted_the_same_way_wherever_it_appears() -> None:
    """One look for a face: the deck's keys and the chain's buttons cannot drift."""
    overrides = support.stylesheets()
    assert "#deckKeys .key-art,\n.key .key-art {" in overrides
    assert "image-rendering: auto;" in overrides
    # The Menus canvas keeps its own rule: a picture only when one is assigned.
    assert ".key .key-art { width: 26px; height: 26px;" in asset("deckdraw.css")
    assert ".fx-slot .fx-pic img { width: 40px; height: 40px; }" in asset("flow.css")
