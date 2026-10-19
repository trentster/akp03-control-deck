"""macOS device backend: own the AKP03 deck through the ``AKP03HIDStream`` helper.

On Linux the daemon owns the deck over ``/dev/hidraw`` (see :mod:`akp03.device`).
macOS has no hidraw, so a Swift helper owns the single open device: it reads the
buttons and streams the frozen newline-delimited JSON protocol on stdout, and it
accepts ``paint`` commands on its own stdin.  This module spawns and supervises
that helper, turns its stdout lines into the same :class:`akp03.device.Event`
stream the Linux path produces, and drives the panel through the helper with
:class:`HidStreamTransport` — so ``akp03.device``, ``akp03.transport`` and the
Linux write path stay completely untouched.

Painting reuses the byte-exact Linux packet builders in :mod:`akp03.protocol`
and :mod:`akp03.images`: ``set_key_image``, ``set_brightness`` and ``clear``
emit exactly the same 1024-byte packets the Linux ``Deck`` writes to hidraw, and
:class:`HidStreamTransport` carries each one — base64-encoded, one per line — to
the helper's stdin.  The helper writes it as a report-ID-0 output report and
answers with one result line, so a refused or failed paint is reported honestly
instead of silently vanishing.  Nothing else crosses the boundary: the input path
never carries device bytes in either direction.

Frozen protocol (one compact JSON object per line on stdout; diagnostics on
stderr), reproduced faithfully from ``AKP03HIDStreamLine``::

    {"event":"starting"}
    {"event":"waiting"}
    {"event":"ready"}
    {"event":"control","control":"key0","state":"press"}   (also "release")
    {"event":"device-lost"}
    {"event":"unknown"}
    {"event":"error","message":"..."}
    {"result":"ok","command":"paint","bytes":1024}         (per paint command)
    {"result":"error","command":"paint","reason":"..."}

and one command line per paint packet on stdin, from this daemon only::

    {"command":"paint","payload":"<base64 of exactly 1024 bytes>"}
"""
from __future__ import annotations

import atexit
import base64
import contextlib
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from PIL import Image

from . import images, protocol
from .device import Deck, Event
from .feed import ControlFeed

HELPER_ENV_VAR = "AKP03_HIDSTREAM"
HELPER_DEFAULT = "AKP03HIDStream"
MAX_LINE_CHARS = 65536

# The helper's fixed, audited open-protocol modes. Only these may be forwarded;
# anything else falls back to the default so a bad env value can never widen the
# helper's output surface. ``wake`` is the default because the single fixed wake
# packet is proven sufficient for input and deliberately does not clear the LCDs.
OPEN_PROTOCOL_ENV_VAR = "AKP03_HIDSTREAM_OPEN_PROTOCOL"
OPEN_PROTOCOLS = ("none", "wake", "full")
DEFAULT_OPEN_PROTOCOL = "wake"

PHASE_STARTING = "starting"
PHASE_WAITING = "waiting"
PHASE_READY = "ready"
PHASE_LOST = "lost"
# A deck is attached but would not answer.  Distinct from ``waiting``, which means no
# deck is attached: only the second one is answered by "plug it in".
PHASE_BLOCKED = "blocked"

# Escalating restart backoff for a helper that keeps dying; a healthy ``ready``
# resets it, so a normal unplug/replug is not penalised.
RESTART_BACKOFF: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

# Frozen stream control spellings -> (daemon kind, control, delta).  These match
# the spellings in ``AKP03ControlServiceCore.AKP03KnownControl`` and the daemon's
# own ``Event`` model exactly, so no event vocabulary is invented here.
CONTROL_MAP: dict[str, tuple[str, str, int | None]] = {
    **{f"key{number}": ("key", f"key{number}", None) for number in range(6)},
    **{f"button{number}": ("button", f"button{number}", None) for number in range(6, 9)},
    "topClockwise": ("knob_turn", "top", 1),
    "topCounterClockwise": ("knob_turn", "top", -1),
    "bottomLeftClockwise": ("knob_turn", "bottom_left", 1),
    "bottomLeftCounterClockwise": ("knob_turn", "bottom_left", -1),
    "bottomRightClockwise": ("knob_turn", "bottom_right", 1),
    "bottomRightCounterClockwise": ("knob_turn", "bottom_right", -1),
    "topPress": ("knob_press", "top", None),
    "bottomLeftPress": ("knob_press", "bottom_left", None),
    "bottomRightPress": ("knob_press", "bottom_right", None),
}

CONTROL_STATES = ("press", "release")

# Painting. Each paint command carries exactly one protocol packet and is
# answered by exactly one result line. A bounded wait keeps a helper that has
# stopped answering from hanging a paint forever (the write is reported as a
# failure instead), and the result mailbox is bounded so a stray burst of result
# lines cannot grow without limit.
PAINT_ACK_TIMEOUT = 5.0
MAX_PENDING_RESULTS = 32


@dataclass(frozen=True)
class StreamMessage:
    """One parsed, still-untrusted protocol line."""

    event: str
    control: str | None = None
    state: str | None = None
    message: str | None = None


def decode_stream_line(line: str) -> StreamMessage | None:
    """Parse one protocol line defensively; malformed input yields ``None``.

    Nothing here can raise: a truncated line, a non-object, a missing or
    mistyped field, and an over-long line are all reported as ``None`` so a
    single bad line can never terminate the reading thread.
    """
    if not isinstance(line, str):
        return None
    text = line.strip()
    if not text or len(text) > MAX_LINE_CHARS:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    if not isinstance(event, str):
        return None
    control, state, message = payload.get("control"), payload.get("state"), payload.get("message")
    return StreamMessage(
        event,
        control if isinstance(control, str) else None,
        state if isinstance(state, str) else None,
        message if isinstance(message, str) else None,
    )


def decode_result_line(line: str) -> dict[str, Any] | None:
    """Parse one helper *result* line (a paint acknowledgement); ``None`` otherwise.

    A result line carries a ``result`` field and no ``event`` field, which keeps
    it unambiguous within the frozen stdout protocol: an event line is decoded by
    :func:`decode_stream_line`, a result line here, and everything else is
    ignored. Like the event decoder this never raises, so one malformed line can
    never terminate the reading thread.
    """
    if not isinstance(line, str):
        return None
    text = line.strip()
    if not text or len(text) > MAX_LINE_CHARS:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if "result" not in payload or "event" in payload:
        return None
    return payload


def event_from_control(control: str, state: str | None, *, raw: bytes = b"") -> Event | None:
    """Map a stream ``control``/``state`` pair onto the daemon's :class:`Event`.

    Rotations (``knob_turn``) carry a ``delta`` and are edge events: only the
    ``press`` edge is actioned so one detent cannot act twice.  Momentary
    controls keep press/release so a held chord is released exactly once.
    """
    mapped = CONTROL_MAP.get(control)
    if mapped is None:
        return None
    kind, name, delta = mapped
    if delta is not None:
        # A rotation is a single-shot edge.  The helper deliberately omits its
        # state, so both a bare turn and an explicit "press" are the one edge;
        # a rotation "release" is never a second edge.
        return Event(kind, name, None, delta, raw) if state in ("press", None) else None
    if state == "press":
        return Event(kind, name, True, None, raw)
    if state == "release":
        return Event(kind, name, False, None, raw)
    return None


def resolve_open_protocol(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the helper's open-protocol mode.

    Precedence: an explicit argument, then ``AKP03_HIDSTREAM_OPEN_PROTOCOL``,
    then :data:`DEFAULT_OPEN_PROTOCOL` (``wake``).  Any value outside
    :data:`OPEN_PROTOCOLS` falls back to the default, so a bad env value can
    never forward an unvetted mode to the helper.
    """
    env = os.environ if environ is None else environ
    candidate = explicit if explicit is not None else env.get(OPEN_PROTOCOL_ENV_VAR)
    return candidate if candidate in OPEN_PROTOCOLS else DEFAULT_OPEN_PROTOCOL


class HidStreamTransportError(OSError):
    """A paint payload could not be written to the helper or was refused by it.

    ``unavailable`` is ``True`` when the helper did not answer the paint protocol
    at all — it is not running, the write failed, or it did not acknowledge in
    time. That is a failure of the protocol rather than of one payload, so the
    deck suspends further painting until the deck is ready again.
    """

    def __init__(self, message: str, *, unavailable: bool = False) -> None:
        super().__init__(message)
        self.unavailable = unavailable


class HidStreamTransport:
    """The macOS write path: protocol payloads ride the helper's own stdin.

    Same contract as :class:`akp03.transport.HidrawTransport`: :meth:`write`
    accepts exactly one 1024-byte packet and raises on any failure. The decoded
    bytes are base64-encoded into one newline-delimited JSON command line on the
    helper's stdin and the helper answers with exactly one result line, so a
    refused or failed write is reported instead of being lost. Painting is the
    only reason raw bytes cross this boundary, and they only ever go *to* the
    helper that already holds the device — never over a socket, a file, or a
    command-line argument.
    """

    def __init__(
        self,
        *,
        process: Callable[[], "subprocess.Popen[str] | None"],
        results: "queue.Queue[dict[str, Any]]",
        lock: "threading.RLock",
        timeout: float = PAINT_ACK_TIMEOUT,
        logger: logging.Logger | None = None,
    ) -> None:
        self._process = process
        self._results = results
        self._lock = lock
        self._timeout = timeout
        self.logger = logger or logging.getLogger("akp03d.hidstream")

    def write(self, payload: bytes) -> None:
        if not isinstance(payload, (bytes, bytearray)) or len(payload) != protocol.PAYLOAD_SIZE:
            raise ValueError("AKP03 protocol payloads must be exactly 1024 bytes")
        with self._lock:
            # No request may be in flight here (the lock serialises writers), so
            # any result still sitting in the mailbox is a stale acknowledgement
            # from an earlier timed-out write: drop it so it cannot be mistaken
            # for this packet's reply.
            self._discard_stale_results()
            stream = self._stdin()
            if stream is None:
                raise HidStreamTransportError("AKP03HIDStream helper is not running", unavailable=True)
            line = json.dumps(
                {"command": "paint", "payload": base64.b64encode(bytes(payload)).decode("ascii")},
                separators=(",", ":"),
            )
            try:
                stream.write(line + "\n")
                stream.flush()
            except (OSError, ValueError) as error:
                raise HidStreamTransportError(f"paint write failed: {error}", unavailable=True) from error
            try:
                result = self._results.get(timeout=self._timeout)
            except queue.Empty:
                raise HidStreamTransportError("AKP03HIDStream did not acknowledge the paint", unavailable=True) from None
        if not isinstance(result, dict) or result.get("result") != "ok":
            reason = result.get("reason") if isinstance(result, dict) else None
            raise HidStreamTransportError(reason if isinstance(reason, str) and reason else "AKP03HIDStream refused the paint")

    def _stdin(self) -> Any:
        process = self._process()
        if process is None:
            return None
        return process.stdin

    def _discard_stale_results(self) -> None:
        while True:
            try:
                self._results.get_nowait()
            except queue.Empty:
                return


class HidStreamDeck:
    """A Deck-compatible macOS backend that owns the ``AKP03HIDStream`` helper.

    The helper runs under its own session so :meth:`close` can reap the whole
    process group; it is never detached from the daemon's lifetime by accident.
    """

    # The streaming backend is the authority for its own connection state and
    # publishes the finer-grained starting/waiting/ready/lost timeline into the
    # shared feed itself.  The daemon must not overwrite it with a coarse
    # "ready" the instant ``open()`` returns (which only spawns the helper).
    publishes_feed_state = True

    def __init__(
        self,
        path: str = "",
        *,
        helper: str | None = None,
        poll_seconds: int | None = None,
        open_protocol: str | None = None,
        feed: ControlFeed | None = None,
        logger: logging.Logger | None = None,
        popen: Callable[..., "subprocess.Popen[str]"] = subprocess.Popen,
        backoff: Sequence[float] = RESTART_BACKOFF,
        clock_join_timeout: float = 2.0,
        paint_timeout: float = PAINT_ACK_TIMEOUT,
    ) -> None:
        self.path = path
        self.helper = helper or os.environ.get(HELPER_ENV_VAR) or shutil.which(HELPER_DEFAULT) or HELPER_DEFAULT
        self.poll_seconds = poll_seconds
        self.open_protocol = resolve_open_protocol(open_protocol)
        self.feed = feed if feed is not None else ControlFeed(connected=False, phase=PHASE_STARTING)
        self.logger = logger or logging.getLogger("akp03d.hidstream")
        self._popen = popen
        self._backoff = tuple(backoff) or (1.0,)
        self._join_timeout = clock_join_timeout
        self._callbacks: list[Callable[[Event], None]] = []
        self._disconnect_callbacks: list[Callable[[BaseException], None]] = []
        self._ready_callbacks: list[Callable[[], None]] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._process: "subprocess.Popen[str] | None" = None
        self._threads: list[threading.Thread] = []
        self._phase = PHASE_STARTING
        self._connected = False
        self._healthy = False
        self._atexit_registered = False
        # Painting.  ``_write_lock`` serialises whole paint sequences (an image
        # upload is never interleaved, exactly as on Linux); the stdout reader
        # only ever fills these two queues and never takes the lock, so a paint
        # that is waiting for its acknowledgement cannot stall input delivery.
        self._write_lock = threading.RLock()
        self._events: "queue.Queue[StreamMessage]" = queue.Queue()
        self._results: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=MAX_PENDING_RESULTS)
        self._last_paint_error: str | None = None
        # Set when the helper stops answering the paint protocol at all; cleared
        # the next time the deck reports ``ready``, which is when painting can
        # work again. This is what keeps a broken paint path from being retried
        # over and over.
        self._paint_unavailable = False
        self.transport = HidStreamTransport(
            process=lambda: self._process,
            results=self._results,
            lock=self._write_lock,
            timeout=paint_timeout,
            logger=self.logger,
        )

    # -- Deck-compatible callback registration ---------------------------------

    def on_event(self, callback: Callable[[Event], None]) -> Callable[[Event], None]:
        self._callbacks.append(callback)
        return callback

    def on_disconnect(self, callback: Callable[[BaseException], None]) -> Callable[[BaseException], None]:
        self._disconnect_callbacks.append(callback)
        return callback

    def on_ready(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired whenever the deck becomes ready again."""
        self._ready_callbacks.append(callback)
        return callback

    # -- Observation -----------------------------------------------------------

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def paint_error(self) -> str | None:
        """Why the most recent paint failed, or ``None`` if it did not.

        Painting is best-effort and never raises, so this is how a caller can
        tell that the panel did *not* receive what it was asked to show instead
        of assuming the update landed. Cleared by the next paint that succeeds.
        """
        return self._last_paint_error

    # -- Lifecycle -------------------------------------------------------------

    def open(self) -> "HidStreamDeck":
        if shutil.which(self.helper) is None:
            raise OSError(f"AKP03HIDStream helper not found: {self.helper!r}")
        self._stop.clear()
        self._set_state(PHASE_STARTING, connected=False)
        if not self._atexit_registered:
            atexit.register(self._close_at_exit)
            self._atexit_registered = True
        self._threads = [
            threading.Thread(target=self._supervise, name="akp03-hidstream-supervisor", daemon=True),
            threading.Thread(target=self._dispatch, name="akp03-hidstream-dispatch", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            process, self._process = self._process, None
        if process is not None:
            self._terminate(process)
        for thread in self._threads:
            thread.join(timeout=self._join_timeout)
        self._threads = []
        self._set_state(PHASE_WAITING, connected=False)

    def _close_at_exit(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    # -- Supervision -----------------------------------------------------------

    def _restart_delay(self, failures: int) -> float:
        return self._backoff[min(failures, len(self._backoff) - 1)]

    def _supervise(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if self._process is None:
                try:
                    self._spawn()
                except OSError as error:
                    self.logger.error("cannot start AKP03HIDStream helper: %s", error)
                    self._set_state(PHASE_LOST, connected=False)
                    if self._stop.wait(self._restart_delay(failures)):
                        break
                    failures += 1
                    continue
            process = self._process
            if process is None:  # pragma: no cover - close() raced the loop
                continue
            process.wait()
            if self._stop.is_set():
                break
            if self._healthy:
                failures = 0
            with self._lock:
                self._process = None
            self.logger.warning("AKP03HIDStream helper exited; restarting in %.1fs", self._restart_delay(failures))
            self._set_state(PHASE_LOST, connected=False)
            if self._stop.wait(self._restart_delay(failures)):
                break
            failures += 1

    def _spawn(self) -> None:
        arguments = [self.helper, "--open-protocol", self.open_protocol]
        if self.poll_seconds is not None:
            arguments += ["--poll-seconds", str(int(self.poll_seconds))]
        self.logger.info("starting AKP03HIDStream helper: %s", " ".join(arguments))
        process = self._popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        with self._lock:
            self._process = process
        self._healthy = False
        threading.Thread(target=self._read_stdout, args=(process,), name="akp03-hidstream-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(process,), name="akp03-hidstream-err", daemon=True).start()

    def _read_stdout(self, process: "subprocess.Popen[str]") -> None:
        # The sole consumer of the helper's stdout. It only ever parses a line and
        # hands it to a queue, so it never blocks on a paint: a result line (the
        # answer to a paint) is delivered here and can always unblock a writer even
        # if the dispatcher is itself waiting on a paint.
        stream = process.stdout
        if stream is None:  # pragma: no cover - Popen always supplies stdout here
            return
        try:
            for line in stream:
                if self._stop.is_set():
                    break
                self._route_line(line)
        except (OSError, ValueError) as error:  # pragma: no cover - pipe teardown
            self.logger.debug("stdout reader stopped: %s", error)
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    def _route_line(self, line: str) -> None:
        try:
            message = decode_stream_line(line)
        except Exception as error:  # defensive: a decode bug must not kill the reader
            self.logger.warning("discarding undecodable stream line: %s", error.__class__.__name__)
            return
        if message is not None:
            self._events.put(message)
            return
        result = decode_result_line(line)
        if result is not None:
            with contextlib.suppress(queue.Full):
                self._results.put_nowait(result)
            return
        if line.strip():
            self.logger.debug("ignoring malformed stream line")

    def _dispatch(self) -> None:
        # Applies event lines on their own thread, exactly as the Linux ``Deck``
        # dispatches from its reader queue. Keeping dispatch separate from stdout
        # reading means a paint that is waiting for its acknowledgement (and any
        # callback a paint triggers) can never stop a result line from arriving.
        while not self._stop.is_set() or not self._events.empty():
            try:
                message = self._events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._apply(message)
            except Exception as error:  # a callback bug must not kill dispatch
                self.logger.warning("discarding stream event %r: %s", getattr(message, "event", "?"), error.__class__.__name__)

    def _read_stderr(self, process: "subprocess.Popen[str]") -> None:
        stream = process.stderr
        if stream is None:  # pragma: no cover
            return
        try:
            for line in stream:
                text = line.rstrip("\n")
                if text:
                    self.logger.debug("AKP03HIDStream: %s", text)
        except (OSError, ValueError):  # pragma: no cover - pipe teardown
            pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    def _apply(self, message: StreamMessage) -> None:
        event = message.event
        if event == "starting":
            self._set_state(PHASE_STARTING, connected=False)
        elif event == "waiting":
            self._set_state(PHASE_WAITING, connected=False)
        elif event == "ready":
            self._healthy = True
            self._set_state(PHASE_READY, connected=True)
            self._fire_ready()
        elif event == "device-lost":
            self._set_state(PHASE_LOST, connected=False)
            self._fire_disconnect(RuntimeError("AKP03 device lost"))
        elif event == "blocked":
            # A deck is attached and would not answer.  Said plainly rather than folded
            # into ``waiting``, because "no AKP03 is plugged in" is the wrong sentence
            # for someone looking at the deck they have just plugged in.
            self.logger.warning("AKP03HIDStream: deck present but not usable: %s", message.message or "no reason given")
            self._set_state(PHASE_BLOCKED, connected=False)
        elif event == "control":
            self._deliver_control(message.control, message.state)
        elif event == "unknown":
            self.logger.debug("AKP03HIDStream reported an unknown input packet")
        elif event == "error":
            self.logger.warning("AKP03HIDStream error: %s", message.message)
        else:
            self.logger.debug("ignoring unknown stream event %r", event)

    def _deliver_control(self, control: str | None, state: str | None) -> None:
        if control is None or control not in CONTROL_MAP:
            if control is not None:
                self.logger.debug("ignoring unknown control %r/%r", control, state)
            return
        if CONTROL_MAP[control][2] is not None:
            # A dial rotation is single-shot: the helper sends no state for it,
            # so the editor is given one momentary ``press`` frame to light the
            # dial (it self-clears); a rotation ``release`` is not a second edge.
            if state == "release":
                return
            self.feed.publish_control(control, "press")
        elif state in CONTROL_STATES:
            self.feed.publish_control(control, state)
        else:
            self.logger.debug("ignoring unknown control %r/%r", control, state)
            return
        event = event_from_control(control, state)
        if event is None:
            return
        for callback in tuple(self._callbacks):
            callback(event)

    def _fire_ready(self) -> None:
        for callback in tuple(self._ready_callbacks):
            callback()

    def _fire_disconnect(self, error: BaseException) -> None:
        for callback in tuple(self._disconnect_callbacks):
            callback(error)

    def _set_state(self, phase: str, *, connected: bool) -> None:
        self._phase = phase
        self._connected = connected
        if phase == PHASE_READY:
            # The deck is usable again, so give painting a fresh start: a helper
            # that was not answering may now be, and a repeated failure should be
            # logged as an ERROR once more rather than staying deduplicated.
            self._paint_unavailable = False
            self._last_paint_error = None
        self.feed.set_state(connected=connected, phase=phase)

    def _terminate(self, process: "subprocess.Popen[str]") -> None:
        if process.poll() is not None:
            return
        self._signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=self._join_timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        self.logger.warning("AKP03HIDStream helper ignored SIGTERM; killing")
        self._signal_group(process, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=self._join_timeout)

    def _signal_group(self, process: "subprocess.Popen[str]", signum: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signum)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(Exception):
                process.terminate() if signum == signal.SIGTERM else process.kill()

    # -- Painting (the byte-exact Linux packet sequence, through the helper) -----

    def set_brightness(self, level: int) -> None:
        self._paint(lambda: self.transport.write(protocol.brightness(level)))

    def clear(self, key: int | None = None) -> None:
        packet = protocol.clear_all() if key is None else protocol.clear_key(key)
        flush = protocol.flush()

        def wipe() -> None:
            self.transport.write(packet)
            self.transport.write(flush)

        self._paint(wipe)

    def set_key_image(self, key: int, source: object) -> None:
        # Byte-for-byte the Linux ``Deck.set_key_image`` sequence: the header, every
        # JPEG chunk, then a flush -- each exactly 1024 bytes, never interleaved.
        jpeg = images.to_device_jpeg(source)  # type: ignore[arg-type]
        header = protocol.image_header(key, len(jpeg))
        chunks = protocol.jpeg_chunks(jpeg)
        flush = protocol.flush()

        def upload() -> None:
            self.transport.write(header)
            for chunk in chunks:
                self.transport.write(chunk)
            self.transport.write(flush)

        self._paint(upload)

    def set_key_color(self, key: int, color: Any) -> None:
        self.set_key_image(key, Image.new("RGB", images.KEY_SIZE, color))

    def _paint(self, operation: Callable[[], None]) -> None:
        """Run one paint sequence, reporting a failure once and never raising.

        The panel is a best-effort side channel: a paint must never kill the
        daemon or the input stream, so a write that the helper refuses or fails
        is logged once per distinct reason (the same pattern the daemon uses for
        keyboard failures) and swallowed. It is attempted exactly once — there is
        no retry loop — and a skipped paint (because the deck is not ready yet)
        claims nothing and is repainted on the next ``ready``.
        """
        if not self._connected or self._paint_unavailable:
            # ``connect()`` paints right after spawning the helper, before it has
            # reported ``ready``; a helper that stopped answering the paint
            # protocol is not attempted again until the deck is ready again. In
            # neither case is anything attempted (so nothing is claimed); the
            # daemon repaints on ``ready`` via ``_deck_ready``.
            if not self._paint_unavailable:
                # A deck that has not reported ``ready`` yet is normal at
                # startup, so this is recorded rather than logged as a failure --
                # but no caller may report the screens as updated either.
                self._last_paint_error = "the AKP03 deck is not ready yet"
            self.logger.debug("skipping paint while the deck is not ready")
            return
        try:
            # Hold the sequence lock across every packet so an image upload can
            # never be interleaved with another write, exactly as on Linux.
            with self._write_lock:
                operation()
        except HidStreamTransportError as error:
            self._log_paint_error(str(error) or error.__class__.__name__)
            if error.unavailable:
                self._paint_unavailable = True
                self.logger.error(
                    "suspending AKP03 painting: the helper did not acknowledge the paint protocol"
                )
        except (OSError, ValueError) as error:
            self._log_paint_error(str(error) or error.__class__.__name__)
        else:
            self._last_paint_error = None

    def _log_paint_error(self, message: str) -> None:
        """Log a paint failure once per distinct reason.

        A refused or failed paint repeats the same reason, so only the first
        occurrence is an ERROR and consecutive duplicates are DEBUG — a genuinely
        new failure stays visible without a cryptic line on every key redraw.
        """
        if message == self._last_paint_error:
            self.logger.debug("%s", message)
            return
        self._last_paint_error = message
        self.logger.error("cannot paint the AKP03 deck: %s", message)


def select_deck_factory(
    platform: str | None = None,
    *,
    helper: str | None = None,
    open_protocol: str | None = None,
    feed: ControlFeed | None = None,
) -> Callable[[str], Any]:
    """Return the deck factory for the current platform.

    Linux (and anything else) keeps the unchanged hidraw :class:`Deck`; only
    macOS selects the helper-driven :class:`HidStreamDeck`.
    """
    current = sys.platform if platform is None else platform
    if current != "darwin":
        return Deck
    return lambda device: HidStreamDeck(device, helper=helper, open_protocol=open_protocol, feed=feed)
