# Icon design standard

How to make key icons for the AKP03 deck that look like they belong beside the
shipped packs, and the physical limits every icon has to live inside.

Two kinds of statement below. **Device** means the hardware or the encoder forces
it — you cannot design around it. **House** means it is our own design rule, and
it exists so icons from different packs still read as one family.

---

## 1. The hard numbers

| What | Value | Source |
| --- | --- | --- |
| Final canvas | **60 x 60 px** | `akp03/images.py` `KEY_SIZE` |
| Colour | **RGB, no alpha** — transparency is dropped | `to_jpeg()` converts to RGB |
| Delivered format | **JPEG**, quality 90 | `to_jpeg(..., quality=90)` |
| Maximum encoded size | **65,535 bytes**, hard error if exceeded | `MAX_JPEG_BYTES = 0xFFFF` |
| Scaling | **Letterboxed**, aspect ratio preserved, black bars | `thumbnail()` + centred paste |
| Panel orientation | Panel shows what it receives turned **90 degrees anticlockwise**, so uploads are pre-rotated clockwise | `DEVICE_TRANSFORM` |
| Transport | one JPEG per key, 1024-byte HID chunks | `protocol.jpeg_chunks()` |

Practical consequences:

- **Author square.** A 1:1 canvas fills the key; anything else gets black bars.
- **Stay flat and simple.** 65,535 bytes is generous for flat art at q90 — the
  general pack's icons measure **1.4-2.0 KB each** — but photographic or noisy
  artwork can trip it, and the encoder raises an error rather than quietly reducing
  quality.
- **Do not compensate for the rotation.** Author upright; the pipeline applies the
  hardware turn at the upload boundary.
- **Light art on a dark face.** The keys are backlit on a dark bezel. Mid-grey art
  on black disappears, which is exactly how OBS grey failed the earlier test.

## 2. Authoring format

**SVG is the right format to submit, but it is not accepted everywhere yet.** The
desktop picker currently offers raster images only (png, jpg, jpeg, webp, bmp, gif,
tif, tiff) and the encoder opens files with Pillow, which cannot read SVG. Adding
SVG means rasterising at the ingest boundary with `resvg` — the same tool the packs
are built with. Treat SVG as the target standard and raster as today's fallback.

- **SVG:** 24 x 24 viewBox (the convention Phosphor and Tabler use), one flat
  single-colour shape, no baked-in colour, no filters, no clipping masks.
- **Colour is applied by the pipeline, not by you.** Submit a silhouette; the pack
  palette supplies the hue and the gloss gradient. This is what keeps a growing
  library coherent and recolourable.
- **Raster fallback:** 180 x 180 px minimum (3x, so the downscale is clean), square,
  on a background you are happy with, no reliance on transparency.
- **Stroke-based art:** minimum stroke weight about **1.5 px at 60 px**, roughly
  0.6 units in a 24-unit viewBox. Thinner strokes survive the render and then die in
  the JPEG step.

## 2b. Which group a picture appears under

Every icon in a manifest names the group it belongs to in the editor's picture
list:

```json
{ "name": "copy", "label": "Copy", "section": "editing", "hue": "#2563eb", "action": "copy" }
```

The four groups are the ones the action picker already uses — `editing`,
`windows`, `media`, `system` — and their names and order come from that same
list, so "Editing" means one thing in both rows and the two cannot drift.

- **A pack that names no group is not an error.** Its pictures file under **Your
  own**, which is where a pack's own art belongs.
- **An unknown group also files under Your own** rather than inventing a heading.
- **Aim for a handful per group.** One picture alone under a heading reads as a
  mistake; four or more reads as a section.

## 3. House design rules

- **Fill about 65% of the canvas.** Art-safe area is 46 x 46 px centred; target a
  glyph roughly 39 px across. 80% looked bulky on the panel.
- **Silhouette first.** Bold, filled shapes. An icon must be recognisable as a solid
  black blob before any colour is added.
- **One hue per icon.** Hues come from the pack palette and are spaced so that no two
  icons in the *same menu* sit within about 30 degrees of each other. Colour is an
  identification cue, not a category — blue is used for Copy and also for Browser.
- **No text, no words, no thin outlines, no drop shadows, no photographic detail.**
- **The test that matters:** at true 60 px, you can name the icon in under a second,
  and tell it apart from the five icons beside it. Judge on the glass, never from a
  desktop screenshot.

## 4. What the pipeline does to your art

So that icons from every source line up optically, ingest normalises each one:

1. rasterise the SVG at 300 px (or take the raster as-is);
2. trim to the alpha bounding box;
3. scale so the longest side is 65% of the canvas and paste it centred;
4. apply the pack hue with the vertical gloss gradient on a dark tile;
5. encode as 60 x 60 JPEG at quality 90, checking the 65,535-byte cap;
6. pre-rotate a quarter turn clockwise for the panel.

Because of step 2, **do not add your own padding or margins.** Trim-and-centre is
what makes a hand-drawn icon line up with a library icon.

## 5. Pack layout

```
icons/
  general/
    pack.json
    copy.svg
    paste.svg
  macos/
    pack.json
    finder.svg
```

`pack.json` per pack:

```json
{
  "name": "General",
  "version": 1,
  "author": "Mark",
  "licence": "MIT",
  "icons": [
    {"name": "copy", "label": "Copy", "hue": "#2563eb", "action": null}
  ]
}
```

- File names: lowercase, hyphenated, no spaces.
- Every pack states its **licence** and **author**. Community packs must be MIT, CC0
  or equivalent so they can ship alongside the built-in ones.
- Brand marks (YouTube, Spotify, Slack, OBS) keep their brand colour and are for the
  owner's own deck: distributing brand logos inside a product needs the brand
  owner's permission.

## 6. Checklist before submitting

- [ ] Square SVG, 24 x 24 viewBox, single flat shape, no baked colour
- [ ] Reads as a silhouette with the colour removed
- [ ] Glyph is about 65% of the canvas, nothing touching the edge
- [ ] No text, no strokes under 1.5 px at 60 px
- [ ] Hue assigned from the palette and distinct from the pack's neighbours
- [ ] `pack.json` filled in, licence and author stated
- [ ] Rendered at true 60 px and named in under a second on the panel

## Status

Today the deck accepts one managed JPEG per key, referenced from the config's
`[images]` section, and the general pack is a rendered reference sheet rather than
installed artwork. Not built yet: SVG ingest, the pack manifest reader, and the
editor picker that would let a menu item carry an icon instead of a text label.
