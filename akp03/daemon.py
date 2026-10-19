"""Safe, reconnecting AKP03 automation daemon."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Timer
from typing import Any, Callable, Protocol, Sequence

from PIL import Image, ImageColor, ImageEnhance

from .backend import ACCESSIBILITY_MESSAGE, ACCESSIBILITY_REASON, KeyboardBackendError, KeyStroke, LogBackend, default_keyboard_backend
from .artwork import ArtworkError, find_icon
from .config import Config, ConfigError, ConfigNotFound, default_config_path, load_config, write_example
from .device import Deck, Event as DeckEvent
from .events import binding_name
from .faces import Paint, label_image as _label_image
from .feed import ControlFeed
from .hidstream import select_deck_factory
from .menu import Menu, MenuItem, MenuPage
from .custom_menu import MENU_DIAL_CONTROLS, CustomMenuNavigator
from .omarchy_menu_catalogue import OMARCHY_MENU_EXECUTABLE
from .session import CommandError, MAX_LINE, _recv_line, _send, default_socket_path, open_socket

STATE_PATH = Path.home() / ".local" / "state" / "akp03" / "keys.json"
ACTION_TIMEOUT_S = 10
HTTP_TIMEOUT_S = 5
MAX_CONCURRENT_ACTIONS = 4
LOG_OUTPUT_LIMIT = 1024

# The daemon hosts the browser editor on loopback by default.  8765 is the
# editor's own historical default but is held by an unrelated service on this
# machine, so the daemon binds 17865 instead.  ``--editor-port 0`` disables the
# hosted editor entirely (the daemon keeps running headless).
DEFAULT_EDITOR_PORT = 17865


class DeckLike(Protocol):
    """The duck-typed surface the daemon needs from any platform deck.

    Both the Linux hidraw :class:`akp03.device.Deck` and the macOS
    :class:`akp03.hidstream.HidStreamDeck` satisfy this; a macOS deck may also
    expose ``on_ready`` so the daemon can re-apply state when it returns.
    """

    def on_event(self, callback: Callable[[DeckEvent], None]) -> object: ...

    def on_disconnect(self, callback: Callable[[BaseException], None]) -> object: ...

    def open(self) -> object: ...

    def close(self) -> None: ...

    def clear(self, key: int | None = None) -> None: ...

    def set_key_image(self, key: int, source: object) -> None: ...

    def set_brightness(self, level: int) -> None: ...


class EditorHost(Protocol):
    """The surface the daemon needs from an in-process hosted editor."""

    @property
    def port(self) -> int: ...

    def stop(self, timeout: float = 2.0) -> None: ...


def _feedback_image(base: Paint | Image.Image | None) -> Image.Image:
    """Create a press state without obscuring the key's artwork."""
    image = _label_image(base) if isinstance(base, Paint) else base.copy() if base is not None else Image.new("RGB", (60, 60), "black")
    return ImageEnhance.Brightness(image).enhance(0.5)


def _artwork_revision(image: Image.Image) -> str:
    """A short, stable revision for a key's artwork.

    The editor caches a key's face by this value, so publishing the description
    stays small: a frame names the artwork, and the browser fetches the picture
    only when the revision actually changes.
    """
    digest = hashlib.sha1()
    digest.update(str(image.size).encode("ascii"))
    digest.update(image.tobytes())
    return digest.hexdigest()[:12]


def _demo_menu() -> Menu:
    return Menu(MenuPage(title="Deck", items=(
        MenuItem("Controls", submenu=MenuPage(title="Controls", items=(
            MenuItem("Top knob", action="top_knob"),
            MenuItem("LCD keys", action="lcd_keys"),
        ))),
        MenuItem("Display", submenu=MenuPage(title="Display", items=(
            MenuItem("Bright 25", action="brightness:25"),
            MenuItem("Bright 50", action="brightness:50"),
            MenuItem("Bright 75", action="brightness:75"),
        ))),
        MenuItem("Status", action="status"),
    )))


def _key(value: str) -> int:
    try:
        key = int(value)
    except ValueError as error:
        raise CommandError("key must be an integer from 0 through 5") from error
    if not 0 <= key <= 5:
        raise CommandError("key must be an integer from 0 through 5")
    return key


def _brightness(value: str) -> int:
    try:
        level = int(value)
    except ValueError as error:
        raise CommandError("brightness must be an integer from 1 through 100") from error
    if not 1 <= level <= 100:
        raise CommandError("brightness must be an integer from 1 through 100")
    return level


def _colour(value: str, name: str) -> str:
    try:
        ImageColor.getrgb(value)
    except ValueError as error:
        raise CommandError(f"{name} must be a Pillow colour") from error
    return value


def parse_control_command(line: str) -> tuple[str, tuple[object, ...]]:
    """Parse the daemon's deliberately small, validated local control protocol."""
    if not line or len(line) > MAX_LINE or "\x00" in line:
        raise CommandError("invalid request")
    try:
        fields = shlex.split(line, posix=True)
    except ValueError as error:
        raise CommandError("invalid request") from error
    if not fields:
        raise CommandError("invalid request")
    command, *args = fields
    if command in {"status", "reload"} and not args:
        return command, ()
    if command == "paint" and 2 <= len(args) <= 4:
        key, text = _key(args[0]), args[1]
        if not text or len(text) > 32:
            raise CommandError("text must contain 1 through 32 characters")
        background = _colour(args[2], "background") if len(args) >= 3 else "black"
        foreground = _colour(args[3], "foreground") if len(args) == 4 else "white"
        return command, (Paint(key, text, background, foreground),)
    if command == "unpaint" and len(args) == 1:
        return command, (_key(args[0]),)
    if command == "brightness" and len(args) == 1:
        return command, (_brightness(args[0]),)
    raise CommandError("unknown command or invalid arguments")


def start_editor_server(port: int, *, config: Path | None, feed: ControlFeed, apply: Callable[[], bool] | None = None, paint_status: Callable[[], str | None] | None = None, screens: Callable[[], dict[str, Any]] | None = None, face: Callable[[int], bytes | None] | None = None, injection_status: Callable[[], dict[str, Any] | None] | None = None, request_permission: Callable[[], dict[str, Any]] | None = None, try_action: Callable[[str], dict[str, Any]] | None = None) -> EditorHost | None:
    """Start the read-only editor in-process, sharing the daemon's live feed.

    Returns ``None`` (rather than raising) when the editor cannot be built, so a
    headless box, an occupied port, or an unreadable daemon config never stops the
    daemon from driving the deck.  The editor is imported lazily so that the daemon
    core never depends on the configuration plane.

    ``apply`` is the daemon's in-place rebind hook, called by ``POST /api/save``
    after a successful atomic write so the running deck uses the new bindings
    without a restart.  On the fallback sample draft (an unrepresentable config)
    there is nothing to save, so no hook is passed.

    ``paint_status`` reports why the deck's LCD keys may not show what was asked
    of them (``None`` when they do), so the editor never claims the screens were
    updated when painting failed.

    ``injection_status`` reports whether the daemon may actually send a
    shortcut, and ``request_permission`` asks macOS for the Accessibility grant.
    Without them a missing grant is invisible until a key is pressed and
    silently does nothing.
    """
    logger = logging.getLogger("akp03d")
    try:
        from .model import ControlPlaneEditorError
        from .webui import EditorServer, make_server
    except ImportError as error:  # e.g. Pillow absent on a minimal host: stay headless
        logger.warning("editor unavailable: %s", error)
        return None
    try:
        server = make_server(port, config=config, feed=feed, apply=apply, paint_status=paint_status,
                             screens=screens, face=face, injection_status=injection_status,
                             request_permission=request_permission, try_action=try_action)
    except ControlPlaneEditorError as error:
        # A daemon config the editor cannot represent is not fatal; fall back to
        # the editor's own sample draft while still streaming live state.
        logger.warning("editor draft from config rejected (%s); serving sample draft", error)
        server = make_server(port, feed=feed, paint_status=paint_status, screens=screens, face=face,
                             injection_status=injection_status, request_permission=request_permission,
                             try_action=try_action)
    return EditorServer(server).start()


class Daemon:
    def __init__(self, config_path: Path, *, state_path: Path = STATE_PATH, deck_factory: Callable[[str], DeckLike] = Deck, backend: KeyStroke | None = None, clock: Callable[[], float] = time.monotonic, timer_factory: Callable[[float, Callable[[], None]], object] = Timer, feed: ControlFeed | None = None) -> None:
        self.config_path, self.state_path, self.deck_factory, self.backend, self.clock, self.timer_factory = config_path, state_path, deck_factory, backend or LogBackend(), clock, timer_factory
        # The live feed the browser editor streams.  The daemon and (on macOS)
        # its streaming deck publish into this one feed; when none is injected
        # the daemon owns a detached one so tests and headless use still work.
        self.feed = feed if feed is not None else ControlFeed(connected=False, phase="waiting")
        self.config: Config | None = None
        self.deck: DeckLike | None = None
        self.labels: dict[int, Paint] = {}
        self.base_images: dict[int, Image.Image] = {}
        # A custom menu may draw its own library icons on the keys.  These live
        # beside the labels (not in ``base_images``) because they belong to the
        # open menu, and the menu owns all six keys while it is up.
        self.menu_faces: dict[int, Image.Image] = {}
        # None is cached too: a reference that fails must not be retried on every
        # repaint, and the key simply keeps its word.
        self._face_cache: dict[str, Image.Image | None] = {}
        self.menu: Menu | None = None
        self.custom_menu: CustomMenuNavigator | None = None
        self.held: dict[str, tuple[tuple[str, ...], float]] = {}
        self.last_error: str | None = None
        # Deduplicate keyboard-backend failures: a backend that is unavailable
        # (e.g. no macOS Accessibility grant) fails every press with the same
        # reason, and repeating it per press adds no information.
        self._last_key_error: str | None = None
        self.stop, self.reload_requested, self.disconnected, self._lock = Event(), Event(), Event(), Lock()
        self._action_slots = BoundedSemaphore(MAX_CONCURRENT_ACTIONS)
        self._actions = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_ACTIONS, thread_name_prefix="akp03-action")
        self._feedback_lock = Lock()
        self._feedback_generation = [0] * 6
        self._feedback_timers: dict[int, object] = {}
        # Sparing the keys: how long the deck may sit untouched before they go
        # dark, and whether they are dark now.  The countdown is armed on the
        # same injected timer the held keys use, so it can be tested without
        # waiting a quarter of an hour.
        self._screen_timer: object | None = None
        self._last_activity = self.clock()
        self._blanked = False

    # ── Sparing the keys when nobody is using the deck ───────────────────────────
    def _stop_screen_timer(self) -> None:
        """Drop the countdown, whatever it was doing."""
        timer = self._screen_timer
        self._screen_timer = None
        if timer is not None:
            timer.cancel()  # type: ignore[attr-defined]

    def _arm_screen_timer(self) -> None:
        """(Re)start the countdown to a dark deck, or stop it when the setting is off."""
        self._stop_screen_timer()
        config = self.config
        seconds = config.screen.blank_after_s if config is not None else 0
        if seconds and self.deck is not None:
            timer = self.timer_factory(seconds, self._blank_keys_if_idle)
            self._screen_timer = timer
            # A timer that is never started never fires: the countdown has to be
            # running, not merely built.  (The live run is what found this.)
            # It is also a daemon thread: a countdown of minutes must never be the
            # reason a process will not exit, and nothing is lost by dropping it.
            if isinstance(timer, Timer):
                timer.daemon = True
            timer.start()  # type: ignore[attr-defined]

    def _blank_keys_if_idle(self) -> None:
        """The countdown ran out: a deck nobody is using puts its keys out.

        A cleared key is not a picture, and that is the whole point: a fixed
        bright screen is what burns into a panel, and the deck's own firmware
        ignores a brightness of 0 ("use 1-100, or clear keys for dark").  The
        backlight stays where the person set it; the keys simply stop showing.
        """
        self._screen_timer = None
        config = self.config
        seconds = config.screen.blank_after_s if config is not None else 0
        deck = self.deck
        if not seconds or deck is None or self._blanked:
            return
        if self.held or self.clock() - self._last_activity < seconds:
            # A hand is still on the deck, or the timer was armed before the last
            # thing that happened: wait again instead of switching the keys off.
            self._arm_screen_timer()
            return
        self._blanked = True
        for key in range(6):
            deck.clear(key)
        self._publish_screens()
        logging.getLogger("akp03d").info(
            "nothing has touched the deck for %s seconds; the keys are dark until it is used again",
            seconds,
        )

    def _note_activity(self) -> None:
        """Somebody touched the deck, or something painted it: the keys are wanted.

        Every path that means "a person is here" comes through here, so there is
        one answer to when the countdown starts rather than several.
        """
        self._last_activity = self.clock()
        if self._blanked:
            self._wake()
        self._arm_screen_timer()

    def _wake(self) -> None:
        """Bring the deck back exactly as it was the moment it is used again."""
        self.paint_all()
        logging.getLogger("akp03d").info("the deck was touched; the keys are showing again")

    def _cancel_feedback(self, key: int | None = None) -> None:
        keys = range(6) if key is None else (key,)
        with self._feedback_lock:
            for item in keys:
                self._feedback_generation[item] += 1
                timer = self._feedback_timers.pop(item, None)
                if timer is not None:
                    timer.cancel()  # type: ignore[attr-defined]

    def _restore_feedback(self, key: int, generation: int, deck: DeckLike) -> None:
        with self._feedback_lock:
            if self._feedback_generation[key] != generation or self.deck is not deck:
                return
            self._feedback_timers.pop(key, None)
            image = self._base_image(key)
            if image is None:
                deck.clear(key)
            else:
                deck.set_key_image(key, image)

    def _menu_face(self, reference: str) -> Image.Image | None:
        """The deck-ready picture for one library reference, or None.

        Cached per reference: a menu re-paints on every knob step, and re-encoding
        an icon each time a key is highlighted would be work for nothing.  A
        reference that cannot be resolved (a pack edited on disk, an icon removed)
        returns None so the key falls back to its word instead of the daemon
        failing to draw a menu at all.
        """
        if reference in self._face_cache:
            return self._face_cache[reference]
        image: Image.Image | None = None
        try:
            icon = find_icon(reference)
            with Image.open(BytesIO(icon.jpeg())) as opened:
                image = opened.convert("RGB").copy()
        except (ArtworkError, OSError, ValueError) as error:
            logging.getLogger("akp03d.menu").warning("menu icon %s cannot be shown: %s", reference, error)
        self._face_cache[reference] = image
        return image

    def _set_menu_faces(self, icons: Sequence[str | None]) -> None:
        """Resolve the open menu's per-key icons into pictures to paint."""
        self.menu_faces = {}
        for key, reference in enumerate(icons):
            if not reference:
                continue
            image = self._menu_face(reference)
            if image is not None:
                self.menu_faces[key] = image

    def _face_image(self, key: int) -> Image.Image | None:
        """The picture ``key`` shows if it shows one at all: no text, no fallback.

        One rule in one place.  An open menu's own icons come first and are the
        only thing allowed to cover it; configured artwork describes the idle deck
        and is deliberately invisible while a menu owns the keys - otherwise an
        open menu reads as a dead deck that still takes the presses.
        """
        face = self.menu_faces.get(key)
        if face is not None:
            return face
        if self.custom_menu is not None or self.menu is not None:
            return None
        return self.base_images.get(key)

    def _artwork_key(self, key: int) -> Image.Image | None:
        """The artwork configured for ``key``, if the idle deck owns the keys.

        Kept as the named rule the editor's mirror and tests ask for; it is now a
        thin view of :meth:`_face_image` so the two can never disagree.
        """
        if self.custom_menu is not None or self.menu is not None:
            return None
        return self.base_images.get(key)

    def _base_image(self, key: int) -> Image.Image | None:
        """What key ``key`` should be showing right now."""
        image = self._face_image(key)
        if image is not None:
            return image.copy()
        paint = self.labels.get(key)
        return _label_image(paint) if paint is not None else None

    def _show_feedback(self, key: int) -> None:
        config, deck = self.config, self.deck
        if config is None or not config.feedback.enabled or deck is None:
            return
        with self._feedback_lock:
            previous = self._feedback_timers.pop(key, None)
            if previous is not None:
                previous.cancel()  # type: ignore[attr-defined]
            self._feedback_generation[key] += 1
            generation = self._feedback_generation[key]
            deck.set_key_image(key, _feedback_image(self._base_image(key)))
            timer = self.timer_factory(config.feedback.duration_ms / 1000, lambda: self._restore_feedback(key, generation, deck))
            self._feedback_timers[key] = timer
            timer.start()  # type: ignore[attr-defined]

    def _load_or_bootstrap(self) -> Config:
        """Load the config, bootstrapping the starter file on a true first run.

        A *missing* config is the first-run case the macOS menu-bar app hits on
        a fresh machine.  Rather than dying, the daemon writes the same starter
        file ``akp03d --init-config`` produces — once, logged clearly — and
        carries on, so it comes up and hosts the editor immediately.

        A config that *exists* but is unreadable, malformed, or invalid is a
        different path entirely: it raises :class:`ConfigError` and the daemon
        fails loudly.  It is never bootstrapped or overwritten, so a user's
        mistake is never hidden and their work is never destroyed.
        """
        try:
            return load_config(self.config_path)
        except ConfigNotFound:
            pass
        try:
            write_example(self.config_path)
        except FileExistsError:
            # Something created the file between our check and our O_EXCL write
            # (or a stray entry sits at the path): load it, never touch it.
            return load_config(self.config_path)
        except OSError as error:
            raise ConfigError(f"cannot create starter config: {error}") from error
        logging.getLogger("akp03d").info(
            "no config at %s; wrote the starter config for first-run setup", self.config_path
        )
        return load_config(self.config_path)

    def _install_idle_labels(self) -> bool:
        """Load the deck's ordinary labels and artwork, with no menu open.

        The on-screen text is derived from the bindings (the assigned action's
        human name), falling back to the static [labels] text for an unbound key
        -- so a key keeps showing its assignment, not a stale label, and a save
        repaints the key the moment it is applied.
        """
        if self.config is None:
            self.last_error = "no configuration is loaded"
            return False
        self.labels = {key: Paint(key, text) for key, text in self.config.key_labels.items()}
        # No menu is drawing the keys now, so nothing may outrank their artwork.
        self.menu_faces = {}
        try:
            self.base_images = {}
            for key, path in self.config.images.items():
                with Image.open(path) as opened:
                    self.base_images[key] = opened.convert("RGB").copy()
        except OSError as error:
            self.last_error = f"configured image cannot be read: {error}"
            return False
        return True

    def _leave_custom_menu(self) -> bool:
        """Close the open menu and put the deck's ordinary key labels back.

        Reached by pressing Back on a menu's root page.  Without it the deck
        would have no way out of a menu once one was opened, which is a dead end
        the person cannot recover from at the hardware.
        """
        self.custom_menu = None
        if self.config is None:
            self.last_error = "no configuration is loaded"
            return False
        restored = self._install_idle_labels()
        self.paint_all()
        if restored:
            self.last_error = None
            logging.getLogger("akp03d.menu").info("left the custom menu; ordinary key labels restored")
        return restored

    def _injection_status(self) -> dict[str, Any]:
        """Whether this daemon can really send a shortcut, for the editor to show.

        Keys that only send a keystroke do nothing at all while macOS withholds
        the Accessibility grant, and nothing on the deck can show that, so the
        editor says it outright rather than leaving someone to work out that the
        key they just bound does nothing.
        """
        check = getattr(self.backend, "check_accessibility", None)
        if check is None:
            return {"permitted": None, "reason": "this keyboard backend sends no keystrokes",
                    "can_request": False}
        try:
            permitted = check()
        except KeyboardBackendError as error:
            return {"permitted": False, "reason": str(error), "can_request": True}
        if permitted:
            return {"permitted": True, "reason": None, "can_request": False}
        return {"permitted": False, "reason": ACCESSIBILITY_REASON, "can_request": True,
                "fix": ACCESSIBILITY_MESSAGE}

    def _request_permission(self) -> dict[str, Any]:
        """Ask macOS to offer the Accessibility grant, on the editor's behalf."""
        ask = getattr(self.backend, "request_accessibility", None)
        if ask is None:
            return {"granted": False, "detail": "this keyboard backend cannot ask macOS for permission"}
        result = ask()
        return {"granted": bool(result.get("granted")), "detail": result.get("detail", "")}

    def try_action(self, action_id: str) -> dict[str, Any]:
        """Run one already-configured action once, for the editor's Try it button.

        Bounded on purpose: the id must name an action this configuration already
        declares, so the editor can never ask the daemon to send anything
        arbitrary.  It answers "does this shortcut actually work?" from where the
        binding is made, instead of sending someone to the deck to guess.
        """
        if self.config is None:
            return {"ok": False, "reason": "no configuration is loaded"}
        if action_id not in self.config.actions:
            return {"ok": False, "reason": f"no action named “{action_id}”"}
        # Trying an action happens at the editor, with a person in front of it.
        self._note_activity()
        try:
            self._run_menu_action(action_id)
        except KeyboardBackendError as error:
            return {"ok": False, "reason": str(error)}
        return {"ok": True, "reason": None}

    def reload(self) -> bool:
        try:
            self.config = self._load_or_bootstrap()
        except ConfigError as error:
            self.last_error = str(error)
            logging.getLogger("akp03d").error("configuration rejected: %s", error)
            return False
        self.release_all()
        self._cancel_feedback()
        if self.config.menu_enabled:
            self.menu = Menu(self.config.menu_root, dials=self.config.menu_dials) if self.config.menu_root is not None else _demo_menu()
        else:
            self.menu = None
        self.custom_menu = None
        if self.menu is None:
            if not self._install_idle_labels():
                return False
        else:
            self.base_images = {}
            self._set_menu_labels()
        self.last_error = None
        self.paint_all()
        self._note_activity()
        return True

    def _apply_editor_save(self) -> bool:
        """Rebind after the editor saved the configuration.

        The editor posts a draft, the service writes it atomically, then the
        daemon is asked to pick it up.  Both outcomes deserve a log line: the
        user has to be able to tell from the log whether a save took effect.
        """
        applied = self.reload()
        if applied:
            logging.getLogger("akp03d").info(
                "configuration saved from the editor; bindings applied without a restart",
            )
        else:
            logging.getLogger("akp03d").error(
                "a configuration saved from the editor could not be applied: %s",
                self.last_error or "unknown error",
            )
        return applied

    def paint_status(self) -> str | None:
        """Why the deck's own LCD keys may not show what was asked, or ``None``.

        Painting into the panel is best-effort and never raises, and on macOS the
        screens are painted through a helper that can refuse or fail a packet.  A
        caller (the browser editor) therefore asks this instead of assuming that
        a save reached the screens: ``None`` means the last paint landed, and any
        string is the honest reason it did not.
        """
        deck = self.deck
        if deck is None:
            return self.last_error or "no deck is attached"
        return getattr(deck, "paint_error", None)

    def _log_key_error(self, message: str) -> None:
        """Log a keyboard-backend failure once per distinct reason.

        A backend that is unavailable — for example without the macOS
        Accessibility grant — fails every press with the same reason.  The first
        occurrence is logged at ERROR and consecutive duplicates at DEBUG, so a
        genuinely new failure is still visible without repeating a cryptic
        message on every single press.
        """
        logger = logging.getLogger("akp03d.keys")
        if message == self._last_key_error:
            logger.debug("%s", message)
            return
        self._last_key_error = message
        logger.error("%s", message)

    def release_all(self) -> None:
        with self._lock:
            held = tuple(self.held.values())
            self.held.clear()
        for keys, _started in held:
            try:
                self.backend.release(keys)
            except KeyboardBackendError as error:
                self._log_key_error(f"keyboard release failed: {error}")
            else:
                self._last_key_error = None

    def expire_held(self) -> None:
        """Release held chords that exceed the configured stuck-key limit."""
        config = self.config
        if config is None:
            return
        now = self.clock()
        expired: list[tuple[str, tuple[str, ...]]] = []
        with self._lock:
            for control, (keys, started) in tuple(self.held.items()):
                if now - started >= config.max_hold_s:
                    self.held.pop(control, None)
                    expired.append((control, keys))
        for control, keys in expired:
            logging.getLogger("akp03d").warning("max_hold_s exceeded for %s; releasing chord", control)
            try:
                self.backend.release(keys)
            except KeyboardBackendError as error:
                self._log_key_error(f"keyboard release failed: {error}")
            else:
                self._last_key_error = None

    def disconnect(self, error: BaseException) -> None:
        self.last_error = f"device disconnected: {error}"
        self.release_all()
        self._cancel_feedback()
        self._announce_state(connected=False, phase="lost")
        self.disconnected.set()

    def _announce_state(self, *, connected: bool, phase: str) -> None:
        """Mirror the daemon's coarse deck state into the editor feed.

        A streaming backend (macOS) owns its own finer-grained
        starting/waiting/ready/lost timeline and publishes it itself, so the
        daemon stays out of its way.  The hidraw backend (Linux) has no feed,
        so the daemon reports the ready/waiting/lost transitions instead.  The
        feed ignores a no-op change, so this is always safe to call.
        """
        if getattr(self.deck, "publishes_feed_state", False):
            return
        self.feed.set_state(connected=connected, phase=phase)

    def _set_menu_labels(self) -> None:
        assert self.menu is not None
        state = self.menu.state
        self.labels = {
            key: Paint(key, ("> " if key == state.selected_index else "") + text, "#173f5f" if key == state.selected_index else "black")
            for key, text in enumerate(state.labels) if text
        }

    def _render_menu(self) -> None:
        self._set_menu_labels()
        self.paint_all()

    def _set_custom_menu_labels(self) -> None:
        assert self.custom_menu is not None
        state = self.custom_menu.state
        # Each key carries its own item name and nothing else.  The selected key
        # is shown by its tile colour alone: the knob needs a visible effect, but
        # an arrow beside the text is redundant on top of the colour.  Back is the
        # bottom-left knob, so no LCD key is spent on navigation.
        self.labels = {
            key: Paint(
                key,
                text,
                "#173f5f" if key == state.selected_key else "black",
            )
            for key, text in enumerate(state.labels) if text
        }
        # An item with an icon is drawn as that icon instead: the words stay in
        # the model (the editor and the mirror still name the item) but nothing
        # of them reaches the glass, which is the point of assigning one.
        self._set_menu_faces(state.icons)

    def _render_custom_menu(self) -> None:
        self._set_custom_menu_labels()
        self.paint_all()

    def _run_menu_action(self, action: str) -> None:
        # brightness:N is the retained legacy menu token. All other values are
        # validated action IDs resolved from Config.actions at reload time.
        if action.startswith("brightness:"):
            level = int(action.partition(":")[2])
            if self.deck is not None:
                self.deck.set_brightness(level)
        elif self.config is not None:
            binding = self.config.actions.get(action)
            if binding is not None:
                self._dispatch_binding(binding, f"menu:{action}", True)
        logging.getLogger("akp03d.menu").info("menu action=%s", action)

    def _menu_event(self, event: DeckEvent) -> bool:
        custom_menu = self.custom_menu
        if custom_menu is not None:
            # While a menu is open the menu owns the deck: all six LCD keys carry
            # page content, the top knob turns to move the highlight and presses to
            # select it, and the bottom-left knob goes back a page.  Each of the three
            # buttons under the keys keeps its ordinary binding unless this menu
            # claims it; every other control always keeps its own.
            if event.kind == "button":
                # Held down or tapped, so a menu's own press-and-hold still works.
                taken = custom_menu.controls.get(event.control)
                if taken is None:
                    return False
                binding = self.config.actions.get(taken.action) if self.config is not None else None
                if binding is None:
                    # Claimed but no longer resolvable: never quietly do something
                    # else instead, and never fall through to the deck's own action.
                    return True
                if taken.mode == "hold" and binding.mode != "hold":
                    binding = replace(binding, mode="hold")
                self._dispatch_binding(binding, event.control, event.pressed)
                return True
            if event.kind == "key" and event.pressed is True and event.control.startswith("key") and event.control[3:].isdigit():
                index = int(event.control[3:])
                if not 0 <= index <= 5:
                    return False
                activation = custom_menu.press(index)
                if activation.action:
                    self._run_menu_action(activation.action)
                self._render_custom_menu()
                return True
            # The menu may take the big dial and the right dial for as long as it is
            # open.  Whatever it names runs that action; whatever it leaves alone keeps
            # its ordinary job, so a menu that claims no part of the big dial still
            # turns to move its highlight and presses to select.
            dial_claims = custom_menu.dials or {}
            big_dial_taken = any(slot in dial_claims for slot in ("top", "top_turn_left", "top_turn_right"))
            if event.kind in ("knob_turn", "knob_press") and event.control in MENU_DIAL_CONTROLS:
                claimed = dial_claims.get(binding_name(event))
                if claimed is not None:
                    binding = self.config.actions.get(claimed) if self.config is not None else None
                    if binding is not None:
                        self._dispatch_binding(binding, event.control, event.pressed)
                        self._render_custom_menu()
                    # Claimed but no longer resolvable: never quietly do something else
                    # instead, and never fall through to the deck's own action.
                    return True
            if event.kind == "knob_turn" and event.control == "top" and event.delta is not None:
                # Not claimed by this menu, so the deck's own job for the turn wins and
                # the highlight only walks when nothing at all is bound to it: pushing
                # keys is how a menu is meant to be used, cycling is the fallback.
                fallback = self._deck_dial_binding(event)
                if fallback is not None:
                    self._dispatch_binding(fallback, binding_name(event), event.pressed)
                else:
                    custom_menu.rotate(event.delta)
            elif event.kind == "knob_press" and event.control == "top":
                if big_dial_taken:
                    return True
                activation = custom_menu.select()
                if activation.action:
                    self._run_menu_action(activation.action)
            elif event.kind == "knob_press" and event.control == "bottom_left":
                if custom_menu.back() is None:
                    # Backing out of the root page leaves the menu: the deck must
                    # always have a way back to its ordinary keys.
                    self._leave_custom_menu()
                    return True
            elif event.kind == "knob_turn" and event.control == "bottom_left":
                # The left dial turns to nothing while a menu is open, and the menu
                # owns it: whatever the deck has on that turn stays out of the way.
                return True
            else:
                return False
            self._render_custom_menu()
            return True
        menu = self.menu
        if menu is None or event.pressed is False:
            return False
        # The deck's own menu may take parts of the big and right dials for as long as it
        # is open, exactly as a custom menu may: a menu opened to work in should still
        # turn the volume down.  Whatever it claims runs that action; whatever it leaves
        # alone keeps its ordinary job here, which is moving the highlight and selecting.
        dial_claims = menu.dials or {}
        if event.kind in ("knob_turn", "knob_press") and event.control in MENU_DIAL_CONTROLS:
            claimed = dial_claims.get(binding_name(event))
            if claimed is not None:
                binding = self.config.actions.get(claimed) if self.config is not None else None
                if binding is not None:
                    self._dispatch_binding(binding, event.control, event.pressed)
                    self._render_menu()
                # Claimed but no longer resolvable: never quietly do something else
                # instead, and never move the highlight with a dial the menu has taken.
                return True
        if event.kind == "knob_turn" and event.control == "top" and event.delta is not None:
            # As in a custom menu: the deck's own job for the turn wins, and the
            # highlight walks only when nothing is bound to it.
            fallback = self._deck_dial_binding(event)
            if fallback is not None:
                self._dispatch_binding(fallback, binding_name(event), event.pressed)
            else:
                menu.rotate(event.delta)
        elif event.kind == "knob_press" and event.control == "top":
            activation = menu.top_press()
            if activation.action:
                self._run_menu_action(activation.action)
        elif event.kind == "knob_press" and event.control == "bottom_left":
            menu.bottom_left_press()
        elif event.kind == "knob_turn" and event.control == "bottom_left":
            # As in a custom menu: the left dial turns to nothing while a menu is
            # open, and the menu owns that turn.
            return True
        elif event.kind == "key" and event.pressed is True and event.control.startswith("key") and event.control[3:].isdigit():
            index = int(event.control[3:])
            if not 0 <= index <= 5:
                return False
            activation = menu.lcd_press(index)
            if activation.action:
                self._run_menu_action(activation.action)
        else:
            return False
        self._render_menu()
        return True

    def _deck_dial_binding(self, event: DeckEvent) -> Any:
        """The deck's own job for this part of a dial, or ``None`` if it has none.

        Used while a menu is open: a dial the menu has not claimed keeps its ordinary
        job, and only a dial with nothing bound walks a menu.  Cycling a highlight is
        the fallback, not the default - the keys are there to be pushed.
        """
        if self.config is None:
            return None
        return self.config.bindings.get(binding_name(event))

    def event(self, event: DeckEvent) -> None:
        # A physical event is a person at the deck, so it wakes the keys and
        # restarts the countdown before anything is dispatched.
        self._note_activity()
        config = self.config
        if self._menu_event(event):
            return
        if event.kind == "key" and event.pressed is True and event.control.startswith("key") and event.control[3:].isdigit():
            key = int(event.control[3:])
            if 0 <= key <= 5:
                self._show_feedback(key)
        # A press is filed under the control itself; each way a dial turns is filed
        # under the way it went, so a dial's press and its turns never collide.
        name = binding_name(event)
        binding = config.bindings.get(name) if config else None
        if binding is not None:
            self._dispatch_binding(binding, name, event.pressed)

    def _dispatch_binding(self, binding, control: str, pressed: bool | None) -> None:
        """Execute a validated binding for either a physical control or menu item."""
        if binding.action == "none":
            return
        # ``keys_hold`` is the older spelling of a held chord; a keys action
        # carrying ``mode = "hold"`` says the same thing on the assignment, so
        # one shared action can be tapped on one control and held on another.
        holds = binding.action == "keys_hold" or binding.mode == "hold"
        chord = binding.keys_hold if binding.action == "keys_hold" else binding.keys
        if pressed is False:
            if holds:
                with self._lock:
                    held = self.held.pop(control, None)
                if held:
                    try:
                        self.backend.release(held[0])
                    except KeyboardBackendError as error:
                        self._log_key_error(f"keyboard release failed: {error}")
                    else:
                        self._last_key_error = None
            return
        if holds:
            with self._lock:
                if control not in self.held:
                    try:
                        self.backend.press(chord)
                    except KeyboardBackendError as error:
                        self._log_key_error(f"keyboard hold failed: {error}")
                    else:
                        self._last_key_error = None
                        self.held[control] = (chord, self.clock())
        elif binding.action == "keys":
            try:
                self.backend.tap(binding.keys)
            except KeyboardBackendError as error:
                self._log_key_error(f"keyboard tap failed: {error}")
            else:
                self._last_key_error = None
        elif binding.action == "brightness" and self.deck:
            self.deck.set_brightness(binding.brightness or 1)
        elif binding.action == "menu" and self.config is not None:
            # Entered by name, with every menu in hand, so a key inside a menu can
            # open any other menu and Back pops one level at a time.
            menus = self.config.custom_menus or {}
            if binding.menu_name in menus:
                self.menu = None
                self.custom_menu = CustomMenuNavigator(menus, binding.menu_name)
                self._render_custom_menu()
        elif binding.action == "shell":
            self._submit_action("shell", binding)
        elif binding.action == "http":
            self._submit_action("http", binding)
        elif binding.action == "omarchy_menu":
            self._submit_action("omarchy_menu", binding)

    @staticmethod
    def _redact(value: str) -> str:
        """Mask process environment values before action output reaches logs."""
        for secret in os.environ.values():
            if secret and len(secret) >= 3:
                value = value.replace(secret, "***")
        return value

    def _submit_action(self, action: str, binding) -> None:
        if not self._action_slots.acquire(blocking=False):
            logging.getLogger("akp03d.actions").warning("%s action rejected: concurrent action limit reached", action)
            return
        self._actions.submit(self._run_action, action, binding)

    def _run_action(self, action: str, binding) -> None:
        try:
            if action == "shell":
                self._run_shell(binding)
            elif action == "http":
                self._run_http(binding)
            else:
                self._run_omarchy_menu(binding)
        finally:
            self._action_slots.release()

    def _run_shell(self, binding) -> None:
        logger = logging.getLogger("akp03d.actions")
        started = time.monotonic()
        try:
            completed = subprocess.run(
                binding.shell,
                shell=False,
                check=False,
                timeout=ACTION_TIMEOUT_S,
                capture_output=True,
                text=True,
                errors="replace",
            )
            logger.info(
                "shell completed exit=%d duration_ms=%d stdout=%r stderr=%r",
                completed.returncode,
                round((time.monotonic() - started) * 1000),
                self._redact(completed.stdout[-LOG_OUTPUT_LIMIT:]),
                self._redact(completed.stderr[-LOG_OUTPUT_LIMIT:]),
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""
            stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""
            logger.warning(
                "shell timed out duration_ms=%d stdout=%r stderr=%r",
                round((time.monotonic() - started) * 1000),
                self._redact(stdout[-LOG_OUTPUT_LIMIT:]),
                self._redact(stderr[-LOG_OUTPUT_LIMIT:]),
            )
        except OSError as error:
            logger.warning("shell failed duration_ms=%d error=%s", round((time.monotonic() - started) * 1000), error.__class__.__name__)

    def _run_http(self, binding) -> None:
        logger = logging.getLogger("akp03d.actions")
        started = time.monotonic()
        try:
            headers = {name: os.environ[value[4:]] if value.startswith("env:") else value for name, value in binding.http_headers}
            data = json.dumps(binding.http_json).encode("utf-8") if binding.http_json is not None else None
            if data is not None and "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(binding.http_url or "", data=data, headers=headers, method=binding.http_method)
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                status = response.getcode()
            logger.info("http completed status=%d duration_ms=%d", status, round((time.monotonic() - started) * 1000))
        except Exception as error:
            logger.warning("http failed duration_ms=%d error=%s", round((time.monotonic() - started) * 1000), error.__class__.__name__)

    def _run_omarchy_menu(self, binding) -> None:
        """Launch only a config-validated Omarchy route in the user session."""
        logger = logging.getLogger("akp03d.actions")
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [OMARCHY_MENU_EXECUTABLE, "summon", binding.omarchy_route or ""],
                shell=False,
                check=False,
                timeout=ACTION_TIMEOUT_S,
                capture_output=True,
                text=True,
                errors="replace",
            )
            logger.info(
                "omarchy_menu completed route=%s exit=%d duration_ms=%d stdout=%r stderr=%r",
                binding.omarchy_route,
                completed.returncode,
                round((time.monotonic() - started) * 1000),
                self._redact(completed.stdout[-LOG_OUTPUT_LIMIT:]),
                self._redact(completed.stderr[-LOG_OUTPUT_LIMIT:]),
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""
            stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""
            logger.warning(
                "omarchy_menu timed out route=%s duration_ms=%d stdout=%r stderr=%r",
                binding.omarchy_route,
                round((time.monotonic() - started) * 1000),
                self._redact(stdout[-LOG_OUTPUT_LIMIT:]),
                self._redact(stderr[-LOG_OUTPUT_LIMIT:]),
            )
        except OSError as error:
            logger.warning("omarchy_menu failed route=%s duration_ms=%d error=%s", binding.omarchy_route, round((time.monotonic() - started) * 1000), error.__class__.__name__)

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {f"key{key}": {"text": item.text, "background": item.background, "foreground": item.foreground} for key, item in sorted(self.labels.items())}
        fd, temp = tempfile.mkstemp(prefix="keys.", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, sort_keys=True)
            os.chmod(temp, 0o600)
            os.replace(temp, self.state_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def paint_all(self) -> None:
        # Anything that repaints has put something back on the glass, so the deck
        # is no longer dark, whatever asked for it.
        self._blanked = False
        self._cancel_feedback()
        # Publish before the deck check: the description is what the daemon has
        # asked the keys to show, which the editor mirrors whether or not a panel
        # is currently attached.
        self._publish_screens()
        if not self.deck:
            return
        for key in range(6):
            image = self._base_image(key)
            if image is None:
                self.deck.clear(key)
            else:
                self.deck.set_key_image(key, image)
        self._save_state()

    def paint(self, paint: Paint) -> None:
        self._cancel_feedback(paint.key)
        self.labels[paint.key] = paint
        if self.deck:
            image = self._base_image(paint.key)
            if image is not None:
                self.deck.set_key_image(paint.key, image)
        self._save_state()
        self._publish_screens()

    def unpaint(self, key: int) -> None:
        self._cancel_feedback(key)
        self.labels.pop(key, None)
        if self.deck:
            image = self._base_image(key)
            if image is None:
                self.deck.clear(key)
            else:
                self.deck.set_key_image(key, image)
        self._save_state()
        self._publish_screens()

    def screen_description(self) -> dict[str, Any]:
        """Describe what the six keys are showing, for the editor's live mirror.

        This is the daemon's own account of its current paint: each key's text or
        artwork, its tile colour, and - when a custom menu is open - the page and
        the item the top knob has highlighted.  The editor mirrors this rather
        than working anything out for itself, so the two can never drift.
        """
        menu = self.custom_menu.state if self.custom_menu is not None else None
        blanked = self._blanked
        keys: list[dict[str, Any]] = []
        for key in range(6):
            paint = self.labels.get(key)
            # Ask the same rule the painter uses, never a second copy of it: a
            # description that re-derives what is on the glass drifts from it.
            image = self._face_image(key)
            if blanked:
                # Dark keys are dark: the mirror says what is on the glass, not
                # what would be there if the deck were awake.
                kind = "blank"
            elif image is not None:
                kind = "image"
            elif paint is not None and paint.text:
                kind = "text"
            else:
                kind = "blank"
            keys.append({
                "key": key,
                "kind": kind,
                "text": paint.text if paint is not None else None,
                "background": paint.background if paint is not None else "black",
                "foreground": paint.foreground if paint is not None else "white",
                "selected": bool(menu is not None and menu.selected_key == key),
                "revision": _artwork_revision(image) if image is not None else None,
            })
        if blanked:
            # The keys are dark because the deck is idle, not because they are
            # empty: the editor says so rather than showing a lie about them.
            layer: dict[str, Any] = {"kind": "idle", "dark": True}
        elif menu is None:
            layer = {"kind": "idle"}
        else:
            layer = {"kind": "menu", "page": menu.page_id, "title": menu.title,
                     "selected_key": menu.selected_key}
        return {"layer": layer, "keys": keys, "blanked": blanked}

    def screen_face(self, key: int) -> bytes | None:
        """PNG bytes of the face key ``key`` is showing, or ``None`` when blank.

        Encoded from the daemon's own base image, so the editor can display the
        deck's artwork without reaching for the managed files itself.  The image
        is source-oriented, which is how the panel presents it.
        """
        if not 0 <= key <= 5:
            return None
        image = self._base_image(key)
        if image is None:
            return None
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _publish_screens(self) -> None:
        """Keep the editor's mirror of the deck in step with the paint."""
        self.feed.publish_screens(self.screen_description())

    def connect(self) -> bool:
        assert self.config
        try:
            deck = self.deck_factory(self.config.device)
            deck.on_event(self.event)
            deck.on_disconnect(self.disconnect)
            # A streaming backend (macOS) can recover the deck in place and
            # re-announce readiness; re-apply state when it does.
            on_ready = getattr(deck, "on_ready", None)
            if on_ready is not None:
                on_ready(lambda: self._deck_ready(deck))
            deck.open()
            self.deck = deck
            self.disconnected.clear()
            self.last_error = None
            self._announce_state(connected=True, phase="ready")
            self.paint_all()
            self._note_activity()
            return True
        except OSError as error:
            self.last_error = f"device unavailable: {error}"
            self.deck = None
            self._announce_state(connected=False, phase="waiting")
            return False

    def _deck_ready(self, deck: DeckLike) -> None:
        """Re-apply state when a backend reports the panel came back."""
        if self.deck is deck:
            self.paint_all()
            self._note_activity()

    def close_deck(self) -> None:
        self.release_all()
        self._cancel_feedback()
        self._stop_screen_timer()
        if self.deck:
            try:
                self.deck.close()
            except OSError:
                pass
            self.deck = None

    def status(self) -> str:
        return "OK STATUS " + json.dumps({"connected": self.deck is not None, "held": sorted(self.held), "error": self.last_error}, separators=(",", ":"))

    def handle(self, line: str) -> str:
        command, args = parse_control_command(line)
        if command == "status":
            return self.status()
        if command == "reload":
            self.reload_requested.set()
            return "OK RELOAD"
        if command == "paint":
            self._note_activity()
            self.paint(args[0])  # type: ignore[arg-type]
            return "OK PAINT"
        if command == "unpaint":
            self.unpaint(args[0])  # type: ignore[arg-type]
            return "OK UNPAINT"
        if command == "brightness":
            if self.deck is None:
                raise CommandError("device is unavailable")
            self.deck.set_brightness(args[0])  # type: ignore[arg-type]
            return "OK BRIGHTNESS"
        raise CommandError("unknown command")

    def run(self, socket_path: Path, *, editor_port: int = 0) -> int:
        if not self.reload():
            return 1
        editor = None
        listener = open_socket(socket_path)
        try:
            if editor_port:
                logger = logging.getLogger("akp03d")
                try:
                    editor = start_editor_server(
                        editor_port, config=self.config_path, feed=self.feed, apply=self._apply_editor_save,
                        paint_status=self.paint_status, screens=self.screen_description, face=self.screen_face,
                        injection_status=self._injection_status, request_permission=self._request_permission,
                        try_action=self.try_action,
                    )
                except OSError as error:
                    logger.error("editor unavailable on port %d: %s", editor_port, error)
                    editor = None
                else:
                    if editor is not None:
                        logger.info("editor listening on http://127.0.0.1:%d", editor.port)
            while not self.stop.is_set():
                if self.reload_requested.is_set():
                    self.reload_requested.clear()
                    self.reload()
                if not self.deck or self.disconnected.is_set():
                    self.close_deck()
                    self.connect()
                self.expire_held()
                listener.settimeout(self.config.poll_seconds if self.config else 0.2)
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    try:
                        response = self.handle(_recv_line(connection))
                    except CommandError as error:
                        response = f"ERR {error}"
                    except Exception:
                        response = "ERR command failed"
                    _send(connection, response)
        finally:
            listener.close()
            if socket_path.exists() and socket_path.is_socket():
                socket_path.unlink()
            self.close_deck()
            # Stop the keyboard helper (if any) last: close_deck() above releases
            # any held chord through the backend, so it must still be alive.
            shutdown_backend = getattr(self.backend, "close", None)
            if callable(shutdown_backend):
                shutdown_backend()
            self._actions.shutdown(wait=False, cancel_futures=True)
            if editor is not None:
                editor.stop()
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="akp03d")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument(
        "--editor-port",
        type=int,
        default=DEFAULT_EDITOR_PORT,
        help="Loopback port for the hosted browser editor; 0 disables hosting. Default: %(default)s.",
    )
    parser.add_argument("--init-config", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.editor_port <= 65535:
        parser.error("--editor-port must be 0 through 65535")
    if args.init_config:
        try:
            write_example(args.config)
        except FileExistsError:
            print(f"refusing to overwrite existing config: {args.config}")
            return 1
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    # One feed for the whole daemon: the deck backend publishes live state and
    # control frames into it, and the hosted editor streams it to the browser.
    feed = ControlFeed(connected=False, phase="waiting")
    daemon = Daemon(args.config, backend=default_keyboard_backend(), deck_factory=select_deck_factory(feed=feed), feed=feed)
    previous = {s: signal.signal(s, lambda *_: daemon.stop.set()) for s in (signal.SIGINT, signal.SIGTERM)}
    hup = signal.signal(signal.SIGHUP, lambda *_: daemon.reload_requested.set())
    try:
        return daemon.run(args.socket, editor_port=args.editor_port)
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        signal.signal(signal.SIGHUP, hup)


if __name__ == "__main__":
    raise SystemExit(main())
