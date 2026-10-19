"""Narrow keyboard backends for AKP03 bindings.

The daemon's test/default backend is :class:`LogBackend`.  Production startup
selects a real backend per platform through :func:`default_keyboard_backend`:
on Linux the narrow ``wtype`` backend for an active Wayland session, and on
macOS the :class:`MacKeyBackend`, which owns the ``AKP03KeyInject`` helper
process.  :func:`keyboard_backend` keeps the original Linux/Wayland selection.
"""
from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from .events import MEDIA_KEYS as _MEDIA_KEY_TOKENS


class KeyStroke(Protocol):
    def tap(self, keys: Sequence[str]) -> None: ...
    def press(self, keys: Sequence[str]) -> None: ...
    def release(self, keys: Sequence[str]) -> None: ...


class KeyboardBackendError(RuntimeError):
    """A vetted keyboard backend could not perform an action."""


class LogBackend:
    """Test-only backend: record configured shortcuts without injecting them."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("akp03d.keys")

    def tap(self, keys: Sequence[str]) -> None:
        self.logger.info("TAP %s", "+".join(keys))

    def press(self, keys: Sequence[str]) -> None:
        self.logger.info("DOWN %s", "+".join(keys))

    def release(self, keys: Sequence[str]) -> None:
        self.logger.info("UP %s", "+".join(keys))


class UnavailableBackend:
    """Fail closed when keyboard injection prerequisites are not met."""

    def __init__(self, reason: str, logger: logging.Logger | None = None) -> None:
        self.reason = reason
        self.logger = logger or logging.getLogger("akp03d.keys")

    def _fail(self, keys: Sequence[str]) -> None:
        del keys
        self.logger.error("keyboard injection unavailable: %s", self.reason)
        raise KeyboardBackendError(self.reason)

    def tap(self, keys: Sequence[str]) -> None:
        self._fail(keys)

    def press(self, keys: Sequence[str]) -> None:
        self._fail(keys)

    def release(self, keys: Sequence[str]) -> None:
        self._fail(keys)


# These are the only modifier tokens accepted from the validated configuration.
# wtype names the Command/Super family "logo"; accepting cmd in config keeps it
# platform-neutral while never passing a user-provided executable argument.
_MODIFIERS = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "cmd": "logo"}
_NAMED_KEYS = {
    "space": "space", "tab": "Tab", "enter": "Return", "escape": "Escape",
    "backspace": "BackSpace", "delete": "Delete", "insert": "Insert",
    "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
    "left": "Left", "right": "Right", "up": "Up", "down": "Down",
}
_SIMPLE_KEY = re.compile(r"(?:[a-z0-9]|f(?:[1-9]|1[0-2]))\Z")


def _wtype_key(key: str) -> tuple[bool, str]:
    """Translate one canonical configuration key to a fixed wtype key name."""
    if not isinstance(key, str):
        raise KeyboardBackendError("shortcut keys must be strings")
    normalized = key.lower()
    if normalized in _MODIFIERS:
        return True, _MODIFIERS[normalized]
    if normalized in _NAMED_KEYS:
        return False, _NAMED_KEYS[normalized]
    if _SIMPLE_KEY.fullmatch(normalized):
        return False, normalized
    raise KeyboardBackendError(f"unsupported canonical key: {key!r}")


class WtypeBackend:
    """Execute vetted tap chords through wtype without a shell.

    wtype tears down its virtual keyboard when a process exits, releasing every
    key.  Therefore a hold cannot safely span daemon events; ``press`` and
    ``release`` explicitly fail rather than claiming a held chord was injected.
    """

    def __init__(
        self, executable: str = "wtype", *, run: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run
    ) -> None:
        self.executable, self._run = executable, run

    def tap(self, keys: Sequence[str]) -> None:
        if not keys:
            raise KeyboardBackendError("shortcut must contain at least one key")
        function = media_key(keys)
        if function is not None:
            # Wayland sends the media functions as their X keysyms; there is no
            # modifier and nothing to hold, so it is a plain -k press.
            argv = [self.executable, "-k", MEDIA_KEY_SYMBOLS[function]]
            try:
                self._run(argv, check=True, capture_output=True, text=True, timeout=2)
            except (OSError, subprocess.SubprocessError) as error:
                raise KeyboardBackendError(f"wtype media key failed: {error}") from error
            return
        modifiers: list[str] = []
        normal: list[str] = []
        for key in keys:
            modifier, translated = _wtype_key(key)
            (modifiers if modifier else normal).append(translated)
        if not normal:
            raise KeyboardBackendError("shortcut must contain a non-modifier key")
        argv = [self.executable]
        for modifier in modifiers:
            argv.extend(("-M", modifier))
        for key in normal:
            argv.extend(("-k", key))
        for modifier in reversed(modifiers):
            argv.extend(("-m", modifier))
        try:
            self._run(argv, check=True, capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError) as error:
            raise KeyboardBackendError(f"wtype tap failed: {error}") from error

    def press(self, keys: Sequence[str]) -> None:
        raise KeyboardBackendError("wtype cannot safely hold keys across daemon events")

    def release(self, keys: Sequence[str]) -> None:
        del keys
        raise KeyboardBackendError("wtype cannot safely release keys across daemon events")


def keyboard_backend(
    environ: Mapping[str, str] | None = None, *, which: Callable[[str], str | None] = shutil.which,
    logger: logging.Logger | None = None,
) -> KeyStroke:
    """Select injection only for an explicitly detected Wayland+wtype session."""
    environment = os.environ if environ is None else environ
    logger = logger or logging.getLogger("akp03d.keys")
    if environment.get("XDG_SESSION_TYPE", "").lower() != "wayland":
        reason = "XDG_SESSION_TYPE is not wayland"
    elif not environment.get("WAYLAND_DISPLAY"):
        reason = "WAYLAND_DISPLAY is not set"
    else:
        executable = which("wtype")
        if executable:
            logger.info("keyboard injection enabled with wtype")
            return WtypeBackend(executable)
        reason = "wtype is not installed or not on PATH"
    logger.error("keyboard injection disabled: %s", reason)
    return UnavailableBackend(reason, logger)


# --- media keys ---------------------------------------------------------------
#
# A media function is a canonical key token like any other, and it is the
# *backend* that decides which event family carries it.  These six have no
# virtual keycode anywhere: macOS listens for a system-defined event, and on
# Wayland the same functions are keysyms.  Naming the function key printed
# beside them (F7-F12) looks right and does nothing - a synthetic F12 reaches
# macOS as a plain function key.
MEDIA_KEYS: tuple[str, ...] = _MEDIA_KEY_TOKENS
# The Wayland keysym for each, for the Linux backend.
MEDIA_KEY_SYMBOLS: dict[str, str] = {
    "mute": "XF86AudioMute",
    "sound-down": "XF86AudioLowerVolume",
    "sound-up": "XF86AudioRaiseVolume",
    "play-pause": "XF86AudioPlay",
    "next": "XF86AudioNext",
    "previous": "XF86AudioPrev",
}


def media_key(keys: Sequence[str]) -> str | None:
    """The media function a key sequence names, or None if it is an ordinary chord.

    A media function is one event pair, never part of a chord: combining it with
    a modifier or another key would promise something no keyboard sends.
    """
    tokens = [key.lower() for key in keys if isinstance(key, str)]
    if len(tokens) != len(keys) or not tokens:
        return None
    named = [token for token in tokens if token in MEDIA_KEYS]
    if not named:
        return None
    if len(tokens) > 1:
        # Wherever it sits: `sound-up` alone is a media action, `cmd+sound-up` is
        # a promise no keyboard can keep.
        raise KeyboardBackendError(
            f"a media key cannot be combined with other keys: {'+'.join(tokens)}"
        )
    return named[0]


# --- macOS key injection ------------------------------------------------------
#
# The daemon's action vocabulary (``keys`` and ``keys_hold``) is platform-neutral
# configuration; only the executor differs.  On macOS the ``AKP03KeyInject``
# helper owns the CGEvent posting and speaks a frozen line protocol.  This
# backend is the single place the daemon talks to it.

KEYINJECT_ENV_VAR = "AKP03_KEYINJECT"
KEYINJECT_DEFAULT = "AKP03KeyInject"
_KEYINJECT_RESULT_OK = "ok"
_KEYINJECT_RESULT_DRY_RUN = "dry-run"
_KEYINJECT_RESULT_ERROR = "error"
_KEYINJECT_RESULT_PONG = "pong"

# The helper reports this exact reason (``AKP03KeyInjectLine.accessibilityReason``)
# whenever macOS Accessibility (TCC) is not granted.  The daemon logs one
# actionable message naming the permission rather than a cryptic failure per press.
ACCESSIBILITY_REASON = "accessibility permission not granted"
ACCESSIBILITY_MESSAGE = (
    "key injection requires Accessibility permission; grant it to the "
    "AKP03KeyInject helper in System Settings > Privacy & Security > Accessibility"
)


def _chord(keys: Sequence[str]) -> str:
    """Render a validated key sequence as the helper's ``+``-joined chord."""
    tokens = list(keys)
    if not tokens or not all(isinstance(key, str) and key for key in tokens):
        raise KeyboardBackendError("shortcut must contain at least one key")
    return "+".join(tokens)


class MacKeyBackend:
    """Execute vetted tap/hold/release chords through the ``AKP03KeyInject`` helper.

    One long-lived helper process owns the macOS keyboard events; the backend
    speaks its frozen line protocol synchronously (one command line out, one JSON
    result line back).  The helper holds at most one chord at a time and releases
    everything on quit, stdin EOF, SIGINT, and SIGTERM, so a ``keys_hold`` chord
    can safely span daemon press/release events without a stuck key.

    Accessibility is reported honestly: with no TCC grant the helper posts
    nothing, the backend fails closed, and it logs one clear, actionable error
    (not a cryptic failure per press).  Nothing here raises anything other than
    :class:`KeyboardBackendError` if the helper is missing or misbehaves.
    """

    def __init__(
        self,
        executable: str = KEYINJECT_DEFAULT,
        *,
        popen: Callable[..., "subprocess.Popen[str]"] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        logger: logging.Logger | None = None,
        arguments: Sequence[str] = (),
    ) -> None:
        self.executable = executable
        self._popen = popen
        self._which = which
        self.logger = logger or logging.getLogger("akp03d.keys")
        self._arguments = tuple(arguments)
        self._lock = threading.RLock()
        self._process: "subprocess.Popen[str] | None" = None
        self._accessibility: bool | None = None
        self._permission_reported = False
        self._atexit_registered = False

    # -- KeyStroke -------------------------------------------------------------

    def tap(self, keys: Sequence[str]) -> None:
        function = media_key(keys)
        if function is not None:
            # The helper's own verb: it builds the system-defined event macOS
            # listens for, which a keyboard event cannot be.
            self._command("media", (function,))
            return
        self._command("tap", keys)

    def press(self, keys: Sequence[str]) -> None:
        self._command("hold", keys)

    def release(self, keys: Sequence[str]) -> None:
        self._command("release", keys)

    @property
    def accessibility(self) -> bool | None:
        """The last reported Accessibility result: ``None`` until first use."""
        return self._accessibility

    def check_accessibility(self) -> bool:
        """Report whether the helper may post keys, *before* the first press.

        Without this the status is only learned when a key is pressed, so a
        missing grant is invisible until someone tries a shortcut and nothing
        happens.  Asking is cheap and idempotent: the helper process is reused.
        """
        with self._lock:
            self._ensure_process()
        return self._accessibility is True

    def request_accessibility(self) -> dict[str, Any]:
        """Ask macOS itself to offer the Accessibility grant to the helper.

        One-shot and passive: the helper posts no keys, prints one JSON line and
        exits.  This is the documented flag, so the editor can offer the fix at
        the moment the problem is discovered instead of sending someone hunting
        through System Settings.
        """
        executable = self._which(self.executable) or self.executable
        try:
            completed = subprocess.run(
                [executable, "--request-accessibility"],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise KeyboardBackendError(f"cannot ask macOS for Accessibility: {error}") from error
        granted = False
        for line in reversed(completed.stdout.splitlines()):
            if not line.strip():
                continue
            try:
                granted = bool(json.loads(line).get("granted"))
            except ValueError:
                granted = False
            break
        if granted:
            # A grant that lands immediately needs no further nagging.
            self._accessibility = True
            self._permission_reported = True
        return {
            "granted": granted,
            "detail": (completed.stdout or completed.stderr).strip() or f"exit {completed.returncode}",
        }

    # -- Protocol exchange -----------------------------------------------------

    def _command(self, verb: str, keys: Sequence[str]) -> None:
        chord = _chord(keys)
        with self._lock:
            self._ensure_process()
            if self._accessibility is False:
                self._report_permission_once()
                raise KeyboardBackendError(ACCESSIBILITY_REASON)
            result = self._exchange(f"{verb} {chord}")
        if result.get("result") == _KEYINJECT_RESULT_ERROR:
            reason = result.get("reason")
            raise KeyboardBackendError(reason if isinstance(reason, str) and reason else "key injection failed")

    def _ensure_process(self) -> "subprocess.Popen[str]":
        process = self._process
        if process is not None and process.poll() is None:
            return process
        executable = self._which(self.executable) or self.executable
        try:
            process = self._popen(
                [executable, *self._arguments],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            # The daemon only catches KeyboardBackendError; a missing or
            # non-executable helper must fail closed, never escape as an OSError.
            raise KeyboardBackendError(f"cannot start {KEYINJECT_DEFAULT}: {error}") from error
        self._process = process
        if not self._atexit_registered:
            atexit.register(self._close_at_exit)
            self._atexit_registered = True
        self.logger.info("starting %s key-injection helper: %s", KEYINJECT_DEFAULT, executable)
        threading.Thread(target=self._drain_stderr, args=(process,), name="akp03-keyinject-err", daemon=True).start()
        # Ask the helper for its Accessibility status; it answers ``pong``.
        pong = self._exchange("ping")
        self._accessibility = bool(pong.get("accessibility")) if pong.get("result") == _KEYINJECT_RESULT_PONG else False
        if self._accessibility is False:
            self._report_permission_once()
        return process

    def _exchange(self, line: str) -> dict[str, Any]:
        """Write one command line and read one JSON result line back."""
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise KeyboardBackendError("key injection helper is unavailable")
        try:
            process.stdin.write(line + "\n")
            process.stdin.flush()
            raw = process.stdout.readline()
        except (OSError, ValueError) as error:
            self._discard_process()
            raise KeyboardBackendError(f"key injection helper write failed: {error}") from error
        if not raw:
            self._discard_process()
            raise KeyboardBackendError("key injection helper closed unexpectedly")
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as error:
            raise KeyboardBackendError("key injection helper returned a malformed result") from error
        if not isinstance(payload, dict):
            raise KeyboardBackendError("key injection helper returned a malformed result")
        return payload

    def _report_permission_once(self) -> None:
        if self._permission_reported:
            return
        self._permission_reported = True
        self.logger.error(ACCESSIBILITY_MESSAGE)

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        """Stop the helper cleanly, releasing any held chord before it exits."""
        with self._lock:
            process, self._process = self._process, None
        if process is not None:
            self._terminate(process)

    def _close_at_exit(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def _discard_process(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=1.0)

    def _terminate(self, process: "subprocess.Popen[str]") -> None:
        if process.poll() is not None:
            return
        # ``quit`` is the helper's own clean-shutdown command: it releases every
        # held key and exits 0.  Closing stdin right after is a belt-and-braces
        # EOF, which is also a clean shutdown path in the helper.
        try:
            if process.stdin is not None:
                process.stdin.write("quit\n")
                process.stdin.flush()
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
        self.logger.warning("%s ignored quit; terminating", KEYINJECT_DEFAULT)
        self._signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
        self._signal_group(process, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2.0)

    def _signal_group(self, process: "subprocess.Popen[str]", signum: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signum)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(Exception):
                process.terminate() if signum == signal.SIGTERM else process.kill()

    def _drain_stderr(self, process: "subprocess.Popen[str]") -> None:
        stream = process.stderr
        if stream is None:  # pragma: no cover - Popen always supplies stderr here
            return
        try:
            for line in stream:
                text = line.rstrip("\n")
                if text:
                    self.logger.debug("%s: %s", KEYINJECT_DEFAULT, text)
        except (OSError, ValueError):  # pragma: no cover - pipe teardown
            pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()


def default_keyboard_backend(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    logger: logging.Logger | None = None,
    popen: Callable[..., "subprocess.Popen[str]"] = subprocess.Popen,
) -> KeyStroke:
    """Select the real keyboard backend for the current platform.

    macOS uses :class:`MacKeyBackend` when the ``AKP03KeyInject`` helper is
    present (honouring ``AKP03_KEYINJECT``); anything else keeps the unchanged
    Linux/Wayland selection.  An absent helper fails closed with a named reason,
    exactly as an unselected Wayland session does today.
    """
    current = sys.platform if platform is None else platform
    logger = logger or logging.getLogger("akp03d.keys")
    if current == "darwin":
        environment = os.environ if environ is None else environ
        helper = environment.get(KEYINJECT_ENV_VAR) or which(KEYINJECT_DEFAULT)
        if helper:
            logger.info("keyboard injection enabled with %s (macOS)", KEYINJECT_DEFAULT)
            return MacKeyBackend(helper, popen=popen, which=which, logger=logger)
        reason = f"{KEYINJECT_DEFAULT} helper not found on PATH"
        logger.error("keyboard injection disabled: %s", reason)
        return UnavailableBackend(reason, logger)
    return keyboard_backend(environ, which=which, logger=logger)
