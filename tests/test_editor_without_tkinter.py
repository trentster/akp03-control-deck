"""The web editor must load on a host where tkinter cannot be imported.

The daemon serves the web editor as its *only* configuration plane, and it used
to reach that editor through :mod:`akp03.gui` - the Tk control panel - for the
shared action and menu shapes.  A host without tkinter therefore made the daemon
log ``editor unavailable`` and serve no editor at all: the entire configuration
surface disappearing behind an optional GUI dependency.

The model now lives in :mod:`akp03.model`.  These tests keep it there: they prove
the editor and the daemon import with tkinter genuinely blocked, prove the block
is real, and fail if a production module starts importing the Tk module again.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

BLOCKING_TKINTER = 'raise ImportError("tkinter is not available (test shim)")\n'
PROBE = (
    "import akp03.webui as webui\n"
    "import akp03.daemon\n"
    "print('keys:', ','.join(webui.LCD_CONTROLS))\n"
)


def blocking_environment(tmp_path: Path) -> dict[str, str]:
    """Environment in which any ``import tkinter`` raises ImportError."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "tkinter.py").write_text(BLOCKING_TKINTER, encoding="utf-8")
    return {**os.environ, "PYTHONPATH": os.pathsep.join([str(shim), str(REPO_ROOT)])}


def run_probe(source: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, cwd=str(REPO_ROOT), env=env
    )


def test_the_editor_and_daemon_import_where_tkinter_cannot(tmp_path):
    completed = run_probe(PROBE, blocking_environment(tmp_path))
    assert completed.returncode == 0, completed.stderr
    assert "keys: key0,key1,key2,key3,key4,key5" in completed.stdout


def test_the_blocking_environment_really_blocks_tkinter(tmp_path):
    """Without this, the test above could pass for the wrong reason."""
    completed = run_probe("import tkinter\n", blocking_environment(tmp_path))
    assert completed.returncode != 0
    assert "is not available" in completed.stderr


def test_no_production_module_imports_a_gui_toolkit():
    """The shipping path is Tk-free.

    The Tk control panel is gone; this now asks the wider question it was really about:
    no module under akp03/ may import tkinter, so a host without it can
    still serve and use the editor.
    """
    banned = ("import tkinter", "from tkinter")
    offenders = [
        path.name
        for path in sorted((REPO_ROOT / "akp03").glob("*.py"))
        if any(line.startswith(banned) for line in path.read_text(encoding="utf-8").splitlines())
    ]
    assert offenders == [], f"these modules need a GUI toolkit at import time: {offenders}"
