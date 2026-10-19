"""The ready-made actions the editor offers, grouped so they can be browsed.

A fresh configuration has no ``[actions.*]`` at all, so the editor has to be able
to *make* the ordinary ones.  Each entry here is a plain shortcut (or a deck
brightness level) - exactly the safe action kinds the daemon accepts - and is
created in the draft the moment a key is pointed at it.

Two rules keep this honest:

* every key used must be one the deck can actually send (``ACCEPTED_KEYS``); a
  shortcut built from a key the hardware cannot press looks fine and does nothing;
* a blurb says what the shortcut really does, including where macOS or the
  frontmost app can ignore it.  Never imply the deck can do something the
  shortcut does not.

The macOS entries are the standard shortcuts.  They fire in whatever app is in
front, so an app that does not define one simply ignores it - that is stated in
the library rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The media functions are part of the shared key vocabulary (``model.MEDIA_KEYS``):
# canonical tokens like any other, whose event family the backend decides.
from .events import MEDIA_KEYS

# Modifiers plus the keys the daemon can genuinely send.
MODIFIERS = ("ctrl", "alt", "shift", "cmd")
SIMPLE_KEYS = tuple("abcdefghijklmnopqrstuvwxyz0123456789") + tuple(f"f{n}" for n in range(1, 13))
NAMED_KEYS = (
    "space",
    "tab",
    "enter",
    "escape",
    "backspace",
    "delete",
    "insert",
    "home",
    "end",
    "pageup",
    "pagedown",
    "left",
    "right",
    "up",
    "down",
)
ACCEPTED_KEYS = frozenset(MODIFIERS + SIMPLE_KEYS + NAMED_KEYS + MEDIA_KEYS)

# How a shortcut reads on the page, most-specific first.
GLYPHS = {
    "cmd": "\u2318",
    "ctrl": "\u2303",
    "alt": "\u2325",
    "shift": "\u21e7",
    "enter": "\u21a9",
    "tab": "\u21e5",
    "escape": "\u238b",
    "backspace": "\u232b",
    "delete": "\u2326",
    "space": "Space",
    "pageup": "PgUp",
    "pagedown": "PgDn",
    "left": "\u2190",
    "right": "\u2192",
    "up": "\u2191",
    "down": "\u2193",
    "mute": "\U0001F507",
    "sound-down": "\U0001F509",
    "sound-up": "\U0001F50A",
    "play-pause": "\u23EF",
    "next": "\u23ED",
    "previous": "\u23EE",
}


@dataclass(frozen=True)
class ReadyAction:
    """One ready-made action: its id, its place in the library, what it sends."""

    id: str
    section: str
    label: str
    blurb: str
    keys: tuple[str, ...] = ()
    brightness: int | None = None
    # The icon library may hold a picture for this action; matched by name.
    icon: str | None = None
    aliases: tuple[str, ...] = field(default=())

    @property
    def spec(self) -> dict:
        """The draft-shaped action, the same shape the editor builds by hand."""
        if self.brightness is not None:
            return {"id": self.id, "kind": "brightness", "brightness": self.brightness}
        return {"id": self.id, "kind": "keys", "keys": list(self.keys)}


SECTIONS = (
    ("editing", "Editing", "Everyday edits in whatever app is in front."),
    ("windows", "Windows & apps", "Opening, arranging and closing things."),
    ("media", "Sound & media", "The media keys a Mac keyboard sends."),
    ("system", "System", "Locking, hiding and quitting."),
    ("deck", "On the deck itself", "The deck's own backlight - no app involved."),
)

CATALOGUE: tuple[ReadyAction, ...] = (
    # ── Editing ──────────────────────────────────────────────────────────────
    ReadyAction("undo", "editing", "Undo", "Steps back in most apps.", ("cmd", "z")),
    ReadyAction("redo", "editing", "Redo", "Steps forward again.", ("shift", "cmd", "z")),
    ReadyAction("cut", "editing", "Cut", "Removes the selection and keeps it.", ("cmd", "x")),
    ReadyAction("copy", "editing", "Copy", "Keeps a copy of the selection.", ("cmd", "c")),
    ReadyAction("paste", "editing", "Paste", "Drops in what you copied.", ("cmd", "v")),
    ReadyAction("select-all", "editing", "Select all", "Selects everything in the front window.", ("cmd", "a")),
    ReadyAction("save", "editing", "Save", "Saves the open document.", ("cmd", "s")),
    ReadyAction("find", "editing", "Find", "Searches the front window.", ("cmd", "f")),
    ReadyAction("delete", "editing", "Delete", "Deletes the selection (the ⌫ key).", ("backspace",)),
    # ── Windows & apps ───────────────────────────────────────────────────────
    ReadyAction("new-window", "windows", "New window", "A new window in the front app.", ("cmd", "n"), icon="new-window"),
    ReadyAction("new-tab", "windows", "New tab", "A new tab, in apps that have tabs.", ("cmd", "t")),
    ReadyAction("close-window", "windows", "Close window", "Closes the front window or tab.", ("cmd", "w"), icon="close-window"),
    ReadyAction("minimise", "windows", "Minimise", "Sends the front window to the Dock.", ("cmd", "m"), icon="minimise"),
    ReadyAction("full-screen", "windows", "Full screen", "Toggles full screen in most apps.", ("ctrl", "cmd", "f"), icon="full-screen"),
    ReadyAction("hide-app", "windows", "Hide app", "Hides the front app entirely.", ("cmd", "h"), icon="hide-app"),
    ReadyAction("quit-app", "windows", "Quit app", "Quits the front app (it may ask to save).", ("cmd", "q"), icon="quit-app"),
    ReadyAction("spotlight", "windows", "Spotlight", "Opens Spotlight search. Do nothing if you remapped ⌘Space.", ("cmd", "space"), icon="spotlight"),
    ReadyAction("mission-control", "windows", "Mission Control", "Shows every open window.", ("ctrl", "up"), icon="mission-control"),
    ReadyAction("app-switcher", "windows", "App switcher", "Switches to the last-used app; press again to step on.", ("cmd", "tab"), icon="app-switcher"),
    ReadyAction("screenshot", "windows", "Screenshot", "Drag to capture an area to the Desktop.", ("shift", "cmd", "4"), icon="screenshot"),
    ReadyAction("screen-record", "windows", "Screen capture bar", "Opens the recording and capture bar.", ("shift", "cmd", "5"), icon="screen-record"),
    # ── Sound & media ────────────────────────────────────────────────────────
    ReadyAction("previous", "media", "Previous track", "The ⏮ media key, printed under F7 on a Mac keyboard.", ("previous",), icon="previous"),
    ReadyAction("play-pause", "media", "Play / Pause", "The ⏯ media key, printed under F8.", ("play-pause",), icon="play-pause"),
    ReadyAction("next", "media", "Next track", "The ⏭ media key, printed under F9.", ("next",), icon="next"),
    ReadyAction("mute", "media", "Mute", "The 🔇 media key, printed under F10.", ("mute",), icon="mute"),
    ReadyAction("volume-down", "media", "Volume down", "The 🔉 media key, printed under F11.", ("sound-down",), icon="volume"),
    ReadyAction("volume-up", "media", "Volume up", "The 🔊 media key, printed under F12.", ("sound-up",), icon="volume"),
    # ── System ───────────────────────────────────────────────────────────────
    ReadyAction("lock", "system", "Lock screen", "Locks the Mac immediately.", ("ctrl", "cmd", "q"), icon="lock"),
    ReadyAction("hide-others", "system", "Hide others", "Hides every app except the front one.", ("alt", "cmd", "h")),
    ReadyAction("force-quit", "system", "Force Quit window", "Opens the Force Quit window; pick the app there.", ("alt", "cmd", "escape")),
    ReadyAction("log-out", "system", "Log out", "Asks first, then logs out.", ("shift", "cmd", "q"), aliases=("log-out",)),
    # ── The deck's own backlight ─────────────────────────────────────────────
    ReadyAction(
        "brightness-25",
        "deck",
        "Dim the keys",
        "Sets the deck's backlight. One level is enough to prove it works.",
        brightness=25,
    ),
)

BY_ID = {entry.id: entry for entry in CATALOGUE}
BY_SECTION = {section_id: (name, blurb) for section_id, name, blurb in SECTIONS}


def spec(action_id: str) -> dict | None:
    """The draft-shaped action for a ready-made id, or None if it is not one."""
    entry = BY_ID.get(action_id)
    return entry.spec if entry else None


def shortcut_text(keys: tuple[str, ...] | list[str]) -> str:
    """`('shift', 'cmd', 'z')` reads as `⇧ ⌘ Z`."""
    parts = []
    for key in keys:
        if key in MODIFIERS:
            parts.append(GLYPHS[key])
        elif len(key) == 1:
            parts.append(key.upper())
        else:
            parts.append(GLYPHS.get(key, key.capitalize()))
    return " ".join(parts)


def catalogue() -> dict:
    """Every section and entry, ready for the editor to render."""
    sections = []
    for section_id, name, blurb in SECTIONS:
        entries = [entry for entry in CATALOGUE if entry.section == section_id]
        if not entries:
            continue
        sections.append({
            "id": section_id,
            "name": name,
            "blurb": blurb,
            "actions": [{
                "id": entry.id,
                "label": entry.label,
                "blurb": entry.blurb,
                "keys": list(entry.keys),
                "shortcut": shortcut_text(entry.keys) if entry.keys else "",
                "brightness": entry.brightness,
                "icon": entry.icon or entry.id,
            } for entry in entries],
        })
    return {"sections": sections, "count": len(CATALOGUE)}


def bad_keys() -> list[str]:
    """Every key in the catalogue the deck cannot send. Empty is the healthy case."""
    offender: list[str] = []
    for entry in CATALOGUE:
        offender.extend(f"{entry.id}:{key}" for key in entry.keys if key not in ACCEPTED_KEYS)
    return offender
