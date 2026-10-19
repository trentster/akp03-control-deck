"""Thread-safe pub/sub feed for live deck state and control events.

The daemon owns the physical deck; the browser editor only reads.  This module
is the read-only seam between them: a device backend publishes connection state
and control events into a :class:`ControlFeed`, and the web editor serves them
to local SSE subscribers.  It performs no device, shell, or persistence work.

The daemon also publishes *what the six keys are showing* (``screens`` frames),
so the editor mirrors the panel instead of guessing at it.  That description is
the daemon's own account of its current paint - labels, artwork, menu page and
the dial's highlight - and it is the only source the editor may use for it.
"""
from __future__ import annotations

import contextlib
import json
import queue
import threading
from dataclasses import dataclass
from typing import Any

# The only connection phases the editor understands.  Kept in step with the
# ``phase`` values the macOS helper and the daemon emit.
PHASES = ("ready", "waiting", "starting", "lost", "blocked")
CONTROL_STATES = ("press", "release")

# A slow reader must never grow the daemon's memory: the oldest frame is dropped.
MAX_QUEUE = 256


@dataclass(frozen=True)
class FeedMessage:
    """One Server-Sent Event frame."""

    event: str
    data: dict[str, Any]

    def encode(self) -> str:
        payload = json.dumps(self.data, separators=(",", ":"), ensure_ascii=False)
        return f"event: {self.event}\ndata: {payload}\n\n"


class Subscription:
    """A bounded, live view of a :class:`ControlFeed`."""

    def __init__(self, feed: "ControlFeed", *, maxsize: int = MAX_QUEUE) -> None:
        self._feed = feed
        self._queue: "queue.Queue[FeedMessage]" = queue.Queue(maxsize=maxsize)

    def _offer(self, message: FeedMessage) -> None:
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(message)

    def get(self, timeout: float | None = None) -> FeedMessage:
        """Return the next frame, raising :class:`queue.Empty` on timeout."""
        return self._queue.get(timeout=timeout)

    def close(self) -> None:
        self._feed._remove(self)


class ControlFeed:
    """Broadcasts connection state and control events to local subscribers."""

    def __init__(self, *, connected: bool = False, phase: str = "waiting") -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown connection phase: {phase!r}")
        self._lock = threading.RLock()
        self._connected = bool(connected)
        self._phase = phase
        self._subscribers: set[Subscription] = set()
        # The last published description of what the keys are showing, so a
        # browser that connects mid-session is not blind until the next change.
        self._screens: dict[str, Any] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def phase(self) -> str:
        return self._phase

    def snapshot(self) -> dict[str, Any]:
        """Return the current state exactly as the editor's ``state`` event shows it."""
        return {"connected": self._connected, "phase": self._phase}

    def set_state(self, *, connected: bool, phase: str) -> bool:
        """Publish a connection-state change; returns whether anything changed."""
        if phase not in PHASES:
            raise ValueError(f"unknown connection phase: {phase!r}")
        with self._lock:
            if bool(connected) == self._connected and phase == self._phase:
                return False
            self._connected = bool(connected)
            self._phase = phase
            payload = {"connected": self._connected, "phase": self._phase}
            subscribers = tuple(self._subscribers)
        message = FeedMessage("state", payload)
        for subscriber in subscribers:
            subscriber._offer(message)
        return True

    def publish_control(self, control: str, state: str) -> bool:
        """Publish one control press/release frame; ignores malformed input."""
        if not isinstance(control, str) or not control or state not in CONTROL_STATES:
            return False
        with self._lock:
            subscribers = tuple(self._subscribers)
        message = FeedMessage("control", {"control": control, "state": state})
        for subscriber in subscribers:
            subscriber._offer(message)
        return True

    def subscribe(self) -> Subscription:
        """Register a subscriber, seeded with the current frames first."""
        subscription = Subscription(self)
        with self._lock:
            subscription._offer(FeedMessage("state", {"connected": self._connected, "phase": self._phase}))
            if self._screens is not None:
                subscription._offer(FeedMessage("screens", self._screens))
            self._subscribers.add(subscription)
        return subscription

    def publish_screens(self, screens: dict[str, Any] | None) -> bool:
        """Publish what the six keys are showing; ``None`` clears the mirror.

        Malformed input is ignored, exactly as :meth:`publish_control` ignores it,
        so a caller can never put the editor's mirror into a nonsense state.
        """
        if screens is not None and not isinstance(screens, dict):
            return False
        with self._lock:
            self._screens = screens
            subscribers = tuple(self._subscribers)
        message = FeedMessage("screens", screens if screens is not None else {})
        for subscriber in subscribers:
            subscriber._offer(message)
        return True

    def screens_snapshot(self) -> dict[str, Any] | None:
        """The last published description of the keys, or ``None`` if never published."""
        with self._lock:
            return self._screens

    def _remove(self, subscription: Subscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription)
