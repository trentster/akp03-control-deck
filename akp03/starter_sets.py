"""Inert, data-driven starter artwork catalogues for the Action Library.

Catalogues describe optional artwork and future ideas only.  They do not create
or change actions, bindings, menus, or device state.  The GUI currently stages
the selected tile's artwork for the selected LCD key; any future action template
must be designed and explicitly surfaced separately.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StarterActionTemplate:
    """Reserved metadata for a future, explicitly designed safe action workflow.

    No current catalogue item exposes or applies these templates.
    """

    stable_id: str
    display_name: str
    description: str


@dataclass(frozen=True)
class StarterTile:
    """One artwork choice within a coherent starter set."""

    stable_id: str
    display_name: str
    description: str
    asset_resource: str
    future_action_templates: tuple[StarterActionTemplate, ...] = ()


@dataclass(frozen=True)
class StarterSet:
    """A named collection of tiles for a particular workflow or domain."""

    stable_id: str
    display_name: str
    description: str
    tiles: tuple[StarterTile, ...]


@dataclass(frozen=True)
class ArtworkGalleryCategory:
    """The presentation-safe view of one artwork category.

    It deliberately carries only catalogue metadata and packaged tile entries:
    choosing an entry remains an artwork-only operation in the GUI.
    """

    stable_id: str
    display_name: str
    description: str
    tiles: tuple[StarterTile, ...]


GENERAL_PURPOSE_SET = StarterSet(
    stable_id="general-purpose",
    display_name="General-purpose",
    description="Everyday controls for common desktop workflows.",
    tiles=(
        StarterTile("dim", "Dim", "Lower display brightness.", "assets/starter-artwork/01-dim.png"),
        StarterTile("bright", "Bright", "Raise display brightness.", "assets/starter-artwork/02-bright.png"),
        StarterTile("copy", "Copy", "Copy the current selection.", "assets/starter-artwork/03-copy.png"),
        StarterTile("paste", "Paste", "Paste from the clipboard.", "assets/starter-artwork/04-paste.png"),
        StarterTile("voice", "Voice", "Start a voice-oriented workflow.", "assets/starter-artwork/05-voice.png"),
        StarterTile("undo", "Undo", "Undo the most recent change.", "assets/starter-artwork/06-undo.png"),
    ),
)

STARTER_SETS: tuple[StarterSet, ...] = (GENERAL_PURPOSE_SET,)


def artwork_gallery_categories(
    starter_sets: tuple[StarterSet, ...] = STARTER_SETS,
) -> tuple[ArtworkGalleryCategory, ...]:
    """Build gallery categories from the catalogue without adding UI-owned data.

    Future starter sets automatically become selectable gallery categories.
    Empty sets are retained so their authored description can explain that they
    are not populated yet, rather than disappearing from the gallery.
    """
    return tuple(
        ArtworkGalleryCategory(
            stable_id=starter_set.stable_id,
            display_name=starter_set.display_name,
            description=starter_set.description,
            tiles=starter_set.tiles,
        )
        for starter_set in starter_sets
    )


def starter_tile_path(tile: StarterTile) -> Path:
    """Resolve a packaged artwork resource without accepting arbitrary paths."""
    resource = Path(__file__).parent / tile.asset_resource
    if not resource.is_file():
        raise FileNotFoundError(f"packaged starter artwork is unavailable: {tile.asset_resource}")
    return resource


