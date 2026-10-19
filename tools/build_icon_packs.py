"""Build the shipped icon packs: SVG source -> transparent glyph tiles + manifest.

Developer tool, not a runtime dependency. The deck only ever needs Pillow at run
time; resvg is required here, at build time, to rasterise the SVG sources.

For each icon we write:
  <pack>/<name>.60.png   60x60 RGBA, glyph about 65% of the tile, transparent
  <pack>/<name>.180.png  180x180 RGBA, for editor previews
  <pack>/sources/<name>.svg   the vector source, kept so packs stay rebuildable
  <pack>/pack.json        manifest: name, licence, icons with label + hue + action
  <pack>/LICENCE          attribution for the upstream art

Tiles are transparent on purpose: the same glyph composites onto a black idle
key or onto the blue selected tile at paint time.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

RESVG = "/opt/homebrew/bin/resvg"
REPO = Path(__file__).resolve().parent.parent
PACKS_ROOT = REPO / "akp03" / "icons"
RENDER_PX = 600
COVERAGE = 0.65
SIZES = (60, 180)
PHOSPHOR = "https://raw.githubusercontent.com/phosphor-icons/core/main/assets/fill"
LICENCE_TEXT = """Phosphor Icons
https://github.com/phosphor-icons/core

MIT License

Copyright (c) 2023 Phosphor Icons

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

GENERAL = [
    ("Copy", ["copy-simple-fill", "files-fill"], "#2563eb", "copy"),
    ("Paste", ["clipboard-text-fill", "clipboard-fill"], "#7c3aed", "paste"),
    ("Cut", ["scissors-fill"], "#e11d48", "cut"),
    ("Undo", ["arrow-u-up-left-fill"], "#f59e0b", "undo"),
    ("Redo", ["arrow-u-up-right-fill"], "#059669", "redo"),
    ("Save", ["floppy-disk-fill"], "#16a34a", "save"),
    ("Find", ["magnifying-glass-fill"], "#0284c7", "find"),
    ("New", ["plus-circle-fill"], "#65a30d", "new"),
    ("Delete", ["trash-fill"], "#dc2626", "delete"),
    ("Mute", ["speaker-slash-fill"], "#ea580c", "mute"),
    ("Volume", ["speaker-high-fill"], "#0d9488", "volume"),
    ("Play / Pause", ["play-pause-fill", "play-fill"], "#06b6d4", "play-pause"),
    ("Next", ["skip-forward-fill"], "#4f46e5", "next"),
    ("Previous", ["skip-back-fill"], "#9333ea", "previous"),
    ("Record", ["record-fill"], "#c026d3", "record"),
    ("Screenshot", ["camera-fill", "screenshot-fill"], "#db2777", "screenshot"),
    ("Folder", ["folder-fill"], "#d97706", "folder"),
    ("Browser", ["globe-fill", "globe-hemisphere-west-fill"], "#38bdf8", "browser"),
    ("Refresh", ["arrows-clockwise-fill"], "#34d399", "refresh"),
    ("Settings", ["gear-fill"], "#a78bfa", "settings"),
    ("Apps", ["squares-four-fill", "app-window-fill"], "#c084fc", "apps"),
    ("Power", ["power-fill"], "#f87171", "power"),
    ("Share", ["share-network-fill"], "#f472b6", "share"),
    ("Zoom", ["magnifying-glass-plus-fill"], "#facc15", "zoom"),
]

MACOS = [
    ("Finder", ["folder-open-fill"], "#0ea5e9", "shell: open ~"),
    ("Brave", ["globe-hemisphere-west-fill", "compass-fill"], "#f97316", "shell: open -a Brave"),
    ("Spotlight", ["magnifying-glass-fill"], "#a78bfa", "cmd + space"),
    ("Mission Control", ["squares-four-fill"], "#22c55e", "ctrl + up"),
    ("Launchpad", ["rocket-launch-fill"], "#e879f9", "fn + f4"),
    ("App switcher", ["arrows-left-right-fill"], "#38bdf8", "cmd + tab"),
    ("New window", ["plus-square-fill"], "#34d399", "cmd + n"),
    ("Close window", ["x-circle-fill"], "#f43f5e", "cmd + w"),
    ("Minimise", ["caret-down-fill", "minus-circle-fill"], "#facc15", "cmd + m"),
    ("Hide app", ["eye-slash-fill"], "#cbd5e1", "cmd + h"),
    ("Quit app", ["power-fill"], "#ef4444", "cmd + q"),
    ("Full screen", ["arrows-out-fill"], "#06b6d4", "ctrl + cmd + f"),
    ("Screenshot", ["camera-fill"], "#db2777", "cmd + shift + 4"),
    ("Screen record", ["record-fill"], "#e11d48", "cmd + shift + 5"),
    ("Lock", ["lock-fill"], "#7c3aed", "ctrl + cmd + q"),
    ("Sleep", ["moon-fill"], "#6366f1", "shell: pmset sleepnow"),
    ("Do Not Disturb", ["bell-slash-fill"], "#f59e0b", "shell: shortcuts run"),
    ("Desktop", ["desktop-fill"], "#5eead4", "fn + f11"),
    ("Trash", ["trash-simple-fill"], "#65a30d", "shell: open ~/.Trash"),
    ("Eject", ["eject-fill"], "#c084fc", "media: eject"),
    ("Wi-Fi", ["wifi-high-fill"], "#14b8a6", "shell: networksetup"),
    ("Bluetooth", ["bluetooth-fill"], "#60a5fa", "shell: blueutil -p"),
    ("Brightness", ["sun-fill"], "#fbaa24", "media: brightness"),
    ("Dark mode", ["circle-half-fill"], "#a855f7", "shell: osascript"),
]

PACK_SPECS = {
    "general": ("General", "General-purpose actions that work in any app", GENERAL),
    "macos": ("macOS", "Common macOS system tasks", MACOS),
}
CACHE = Path("/tmp/ak03/pack/svg")


def rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def svg_source(name: str, sources: Path) -> Path | None:
    """Cached copy if we have one, otherwise download from Phosphor (MIT)."""
    target = sources / f"{name}.svg"
    if target.exists():
        return target
    for candidate in sorted(CACHE.glob(f"{name}-*.svg")):
        if candidate.stat().st_size > 40:
            target.write_bytes(candidate.read_bytes())
            return target
    try:
        with urllib.request.urlopen(f"{PHOSPHOR}/{name}.svg", timeout=25) as response:
            target.write_bytes(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None
    return target


def glyph_mask(source: Path) -> Image.Image:
    """Alpha mask of the glyph, rendered big and trimmed to its bounding box."""
    renders = source.parent / ".renders"
    renders.mkdir(exist_ok=True)
    rendered = renders / (source.stem + ".png")
    if not rendered.exists():
        result = subprocess.run([RESVG, "-w", str(RENDER_PX), "-h", str(RENDER_PX),
                                 str(source), str(rendered)], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"resvg failed for {source.name}: {result.stderr.decode()[:200]}")
    with Image.open(rendered) as opened:
        mask = opened.convert("RGBA").getchannel("A")
    box = mask.getbbox()
    if box is None:
        raise RuntimeError(f"{source.name} rendered empty")
    return mask.crop(box)


def ramp(top: tuple[int, int, int], bottom: tuple[int, int, int], size: int) -> Image.Image:
    strip = Image.new("RGB", (1, size))
    for y in range(size):
        share = y / (size - 1)
        strip.putpixel((0, y), tuple(round(a * (1 - share) + b * share) for a, b in zip(top, bottom)))
    return strip.resize((size, size))


def build_icon(label: str, candidates: list[str], hue: str, sources: Path, out: Path) -> str | None:
    mask = None
    chosen = None
    for name in candidates:
        source = svg_source(name, sources)
        if source is None:
            continue
        try:
            mask = glyph_mask(source)
        except RuntimeError:
            continue
        chosen = name
        break
    if mask is None or chosen is None:
        return None
    base = slug(label)
    shade = rgb(hue)
    for size in SIZES:
        side = int(size * COVERAGE)
        scale = side / max(mask.size)
        small = mask.resize((max(1, round(mask.width * scale)), max(1, round(mask.height * scale))),
                            Image.Resampling.LANCZOS)
        tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        light: tuple[int, int, int] = (round(shade[0] + (255 - shade[0]) * 0.55),
                                       round(shade[1] + (255 - shade[1]) * 0.55),
                                       round(shade[2] + (255 - shade[2]) * 0.55))
        paint = ramp(shade, light, size).convert("RGBA")
        offset = ((size - small.width) // 2, (size - small.height) // 2)
        # The gradient spans the whole tile, so take the patch that sits behind the
        # glyph and composite it through the glyph mask.
        patch = paint.crop((offset[0], offset[1], offset[0] + small.width, offset[1] + small.height))
        tile.paste(patch, offset, small)
        tile.save(out / f"{base}.{size}.png")
    return chosen


def build_pack(pack_id: str, title: str, description: str, spec: list) -> None:
    out = PACKS_ROOT / pack_id
    sources = out / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    icons = []
    for label, candidates, hue, action in spec:
        used = build_icon(label, candidates, hue, sources, out)
        if used is None:
            print(f"  !! no glyph for {label} ({candidates})", file=sys.stderr)
            continue
        icons.append({"name": slug(label), "label": label, "hue": hue, "action": action, "source": used})
    manifest = {
        "id": pack_id,
        "name": title,
        "description": description,
        "author": "AKP03 icon pipeline",
        "version": 1,
        "licence": "MIT (Phosphor Icons, Copyright (c) 2023 Phosphor Icons)",
        "tile_sizes": list(SIZES),
        "coverage": COVERAGE,
        "icons": icons,
    }
    (out / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "LICENCE").write_text(LICENCE_TEXT)
    print(f"{pack_id}: {len(icons)} icons -> {out}")


def main() -> int:
    for pack_id, (title, description, spec) in PACK_SPECS.items():
        build_pack(pack_id, title, description, spec)
    # Rasterised intermediates are a build detail, not shipped artwork.
    for renders in PACKS_ROOT.glob("*/sources/.renders"):
        for leftover in renders.glob("*.png"):
            leftover.unlink()
        renders.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
