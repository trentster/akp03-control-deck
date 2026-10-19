"""The guide is part of the app: reachable, unbroken, and honest about what exists."""
from __future__ import annotations

import threading
from http.client import HTTPConnection
from importlib.resources import files

import pytest

from akp03.webui import make_server


def running_server():
    server = make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def get(server, path: str) -> tuple[int, str]:
    connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def test_the_header_offers_the_guide_in_its_own_tab() -> None:
    """A quiet question mark, and the deck you were on is still there afterwards."""
    page = files("akp03").joinpath("webui_assets", "index.html").read_text()
    assert 'id="helpLink"' in page
    assert 'href="/assets/guide.html#deck"' in page, "the link points at the guide the app serves, at the page it opens on"
    assert 'target="_blank"' in page and 'rel="noopener"' in page
    assert '/assets/overrides.css' in page, "and the stylesheet carrying it is loaded"


def test_the_question_mark_answers_for_the_page_you_are_on() -> None:
    """A guide is only help if it answers the question in front of you: the icon part
    used to be all there was, so asking on the Deck page answered something else."""
    script = files("akp03").joinpath("webui_assets", "app.js").read_text()
    assert "const HELP_SECTIONS = {" in script
    for anchor in ("deck", "menus", "icons"):
        assert f"{{anchor: '{anchor}'" in script, f"the guide has a section for {anchor}"
    assert "link.href = `/assets/guide.html#${section.anchor}`;" in script
    # Every page the rail offers has a section, and the link is set as the pages change.
    assert "showHelpFor(name);" in script
    assert "showHelpFor(showing ? showing.id : 'deck');" in script

    body = files("akp03").joinpath("webui_assets", "guide.html").read_text()
    for anchor in ('id="deck"', 'id="menus"', 'id="icons"', 'id="sets"'):
        assert anchor in body, f"the guide carries {anchor}"
    assert "The small left dial goes back a level" in body, "and says how to get out of a menu"


def test_the_guide_is_served_and_is_the_repo_copy() -> None:
    server, thread = running_server()
    try:
        status, body = get(server, "/assets/guide.html")
        assert status == 200
        assert "Using AKP03 Control" in body
        on_disk = files("akp03").joinpath("webui_assets", "guide.html").read_text()
        assert body == on_disk, "what is served is the file in the repository"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_the_guide_documents_the_numbers_the_deck_uses() -> None:
    """If the guide drifts from the app, it is worse than no guide."""
    server, thread = running_server()
    try:
        _status, body = get(server, "/assets/guide.html")
        for number in ("180×180", "60×60", "18px", "512KB", "pack.json", "licence"):
            assert number in body, f"the guide should cover {number}"
        # And it must not promise what the app cannot do: the import exists, so the guide
        # is allowed to describe it, but the built-in sets staying read-only must be true.
        assert "Import a set…</b> takes a zip" in body
        assert "never overwritten" in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_the_asset_route_cannot_walk_out_of_its_directory() -> None:
    server, thread = running_server()
    try:
        status, body = get(server, "/assets/../webui.py")
        assert status != 200 or "IconLibraryError" not in body
        status, body = get(server, "/assets/%2e%2e/webui.py")
        assert status != 200 or "IconLibraryError" not in body
    finally:
        server.shutdown()
        thread.join(timeout=3)
