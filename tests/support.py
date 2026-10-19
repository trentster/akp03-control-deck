"""Helpers the editor tests were each carrying their own copy of.

Three near-identical copies of ``wait_for_port`` and of ``wait_until`` stood in the test
tree, differing only in their default timeout, and two of ``start_server`` were identical
line for line.  The defaults here are the most patient of the copies: a longer wait can
only slow a test that was going to fail anyway.

The tests import these as ``support``.  pytest puts a test file's own directory on the
path, so no package or conftest is needed for that to resolve.
"""

from __future__ import annotations

import socket
import threading
import time
from akp03.webui import make_server


def wait_for_port(port: int, timeout: float = 6.0) -> bool:
    """True once something accepts a connection on the port, False if nothing does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.02)
    return False


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """True once the predicate does; asked once more at the end so a late pass counts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def start_server(**kwargs):
    """A server on an arbitrary port, serving in a thread.  Returns (server, thread)."""
    server = make_server(0, **kwargs)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    return server, thread


def asset_dir():
    """Where the editor's own files live, for a test that wants to read one."""
    from pathlib import Path
    return Path(__file__).resolve().parent.parent / "akp03" / "webui_assets"


def stylesheet_names(directory=None) -> list[str]:
    """The stylesheets the page links, in load order, by file name."""
    import re
    directory = directory or asset_dir()
    page = directory.joinpath("index.html").read_text(encoding="utf-8")
    return re.findall(r'<link[^>]+href="/assets/([^"]+\.css)"', page)


def stylesheets(directory=None) -> str:
    """Every stylesheet the page actually loads, joined in load order.

    Tests asked "does the app style this?" by reading one named file, which turned a rule's
    *address* into its identity: moving a rule into the file that owns the thing it styles
    then read as the rule vanishing.  Read what the browser reads instead - the order comes
    from the page's own links, so it follows the cascade rather than a list kept here.
    """
    directory = directory or asset_dir()
    return "\n".join(directory.joinpath(name).read_text(encoding="utf-8") for name in stylesheet_names(directory))


def script_names(directory=None) -> list[str]:
    """The scripts the page loads, in load order, by file name."""
    import re
    directory = directory or asset_dir()
    page = directory.joinpath("index.html").read_text(encoding="utf-8")
    return re.findall(r'<script[^>]+src="/assets/([^"]+\.js)"', page)


def scripts(directory=None) -> str:
    """Every script the page loads, joined in load order.

    The same lesson as the stylesheets: a piece of behaviour is the page's, not one file's.
    A test that reads app.js alone turns "the page explains what a control does" into "app.js
    contains this sentence", and then reads as a lost feature when the wording moves to the
    panel, or as a live one when a screen stops being reachable.
    """
    directory = directory or asset_dir()
    return "\n".join(directory.joinpath(name).read_text(encoding="utf-8") for name in script_names(directory))
