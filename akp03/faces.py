"""What a key shows when it has no picture: its label, drawn on the tile.

The device paints this onto the glass; the editor previews the very same picture
beside it.  One renderer with two callers, so a button without artwork cannot look
like one thing on the deck and another thing in the app.

"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class Paint:
    key: int
    text: str
    background: str = "black"
    foreground: str = "white"


LABEL_SIZE = 60
LABEL_PAD = 4
LABEL_FONT_SIZES = (12, 11, 10, 9, 8, 7)


def label_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow versions before scalable default-font support.
        return ImageFont.load_default()


def wrap_label(draw: ImageDraw.ImageDraw, text: str, font: "ImageFont.FreeTypeFont | ImageFont.ImageFont", width: int) -> list[str]:
    """Wrap to the tile width, breaking an over-long word only as a last resort."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= width:
                current = candidate
                continue
            lines.append(current)
            current = word
        while draw.textlength(current, font=font) > width and len(current) > 1:
            split = len(current) - 1
            while split > 1 and draw.textlength(current[:split], font=font) > width:
                split -= 1
            lines.append(current[:split])
            current = current[split:]
        lines.append(current)
    return lines


def label_image(paint: Paint) -> Image.Image:
    """Draw one key label, fitted to the tile so the tile edge never cuts it off."""
    image = Image.new("RGB", (LABEL_SIZE, LABEL_SIZE), paint.background)
    draw = ImageDraw.Draw(image)
    width = LABEL_SIZE - 2 * LABEL_PAD
    limit = LABEL_SIZE - 2 * LABEL_PAD
    chosen: tuple[object, list[str], tuple[float, float, float, float]] | None = None
    for size in LABEL_FONT_SIZES:
        font = label_font(size)
        lines = wrap_label(draw, paint.text, font, width)
        box = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font, spacing=1)
        chosen = (font, lines, box)
        if box[2] - box[0] <= width and box[3] - box[1] <= limit:
            break
    assert chosen is not None
    font, lines, box = chosen
    x = max(LABEL_PAD, (LABEL_SIZE - (box[2] - box[0])) // 2 - box[0])
    y = max(LABEL_PAD, (LABEL_SIZE - (box[3] - box[1])) // 2 - box[1])
    # Centre the block and every line inside it: text hugging the left edge of a
    # tile reads as misaligned next to its neighbours.
    draw.multiline_text((x, y), "\n".join(lines), fill=paint.foreground, font=font, spacing=1, align="center")
    return image




def label_png(paint: Paint) -> bytes:
    """One label face as PNG bytes, for the editor's preview of a button."""
    buffer = BytesIO()
    label_image(paint).save(buffer, format="PNG")
    return buffer.getvalue()
