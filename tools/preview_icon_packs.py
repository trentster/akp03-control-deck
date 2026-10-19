"""Render a review sheet for every installed icon pack.

Reads the packs through the real library loader, so the sheet shows exactly the
tiles the deck would paint rather than anything this tool invented.  Run before
shipping a pack, and before judging one on the physical panel.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from akp03.iconpacks import Icon, Pack, load_packs  # noqa: E402

ZOOM = 3
SELECTED = "#173f5f"
COLUMNS = 6
OUT = Path("/tmp/ak03/library")


def font(size: int):
    for candidate in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            from PIL import ImageFont
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    from PIL import ImageFont
    return ImageFont.load_default()


TITLE, LABEL, SMALL = font(22), font(14), font(11)


def jpeg_round_trip(image: Image.Image, quality: int = 90) -> Image.Image:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def sheet_for(pack: Pack) -> Path:
    rows = (len(pack.icons) + COLUMNS - 1) // COLUMNS
    cell_w, cell_h = 60 * ZOOM + 120, 60 * ZOOM + 46
    pad, deck_h = 34, 220
    width = pad * 2 + cell_w * COLUMNS
    height = 170 + cell_h * rows + deck_h
    canvas = Image.new("RGB", (width, height), "#faf6ef")
    draw = ImageDraw.Draw(canvas)
    draw.text((pad, 26), f"{pack.name} pack - {len(pack.icons)} icons", fill="#2b2620", font=TITLE)
    draw.text((pad, 60), f"{pack.id}/  |  {pack.author}  |  {pack.licence}", fill="#7a6f60", font=SMALL)
    draw.text((pad, 84), "Each cell: the glyph on a real key face at 3x, with the true 60px face beside it. "
                         "Tiles are transparent, so the same glyph also sits on the selected tile below.",
              fill="#7a6f60", font=SMALL)
    for index, icon in enumerate(pack.icons):
        row, column = divmod(index, COLUMNS)
        x = pad + column * cell_w
        y = 150 + row * cell_h
        face = icon.face()
        canvas.paste(face.resize((60 * ZOOM, 60 * ZOOM), Image.Resampling.LANCZOS), (x, y))
        canvas.paste(jpeg_round_trip(face), (x + 60 * ZOOM + 12, y + 58))
        draw.text((x, y + 60 * ZOOM + 8), icon.label, fill="#5c5347", font=LABEL)
        draw.text((x, y + 60 * ZOOM + 26), icon.hue, fill=icon.hue, font=SMALL)
        draw.text((x + 60 * ZOOM + 12, y + 34), "true size", fill="#a99e8d", font=SMALL)

    deck_y = 150 + cell_h * rows + 10
    strip: list[tuple[Icon, str]] = [(pack.icons[index], "black") for index in range(min(5, len(pack.icons)))]
    if len(pack.icons) > 5:
        strip.append((pack.icons[-1], SELECTED))
    draw.text((pad, deck_y), "On the deck:", fill="#2b2620", font=LABEL)
    for index, (icon, background) in enumerate(strip):
        x = pad + 130 + index * (60 * ZOOM + 24)
        face = icon.face(background=background)
        canvas.paste(face.resize((60 * ZOOM, 60 * ZOOM), Image.Resampling.LANCZOS), (x, deck_y - 12))
        canvas.paste(jpeg_round_trip(face), (x + 60 * ZOOM + 8, deck_y + 14))
        note = f"{icon.label} (selected)" if background != "black" else icon.label
        draw.text((x, deck_y + 60 * ZOOM + 4), note, fill="#5c5347", font=LABEL)

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"{pack.id}_sheet.png"
    canvas.save(target)
    return target


def main() -> int:
    packs = load_packs()
    if not packs:
        print("no packs installed", file=sys.stderr)
        return 1
    for pack in packs:
        target = sheet_for(pack)
        print(f"{pack.id}: {len(pack.icons)} icons -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
