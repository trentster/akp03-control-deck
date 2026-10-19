"""macOS device backend: frozen-protocol decoding and helper supervision.

Nothing here needs hardware: the "deck" is a tiny fake helper script that prints
the frozen line protocol, and the backend is driven entirely through it.
"""
from __future__ import annotations
from support import wait_until

import base64
import json
import logging
import os
import queue
import stat
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pytest
from PIL import Image

from akp03 import images, protocol
from akp03.daemon import Daemon
from akp03.device import Deck, Event
from akp03.feed import ControlFeed
from akp03.hidstream import (
    DEFAULT_OPEN_PROTOCOL,
    OPEN_PROTOCOL_ENV_VAR,
    HidStreamDeck,
    HidStreamTransportError,
    StreamMessage,
    decode_result_line,
    decode_stream_line,
    event_from_control,
    resolve_open_protocol,
    select_deck_factory,
)

# A fake helper that emits the whole frozen protocol once, then idles until it
# is signalled.  It records its pid so a test can prove it is reaped on close,
# and it answers the paint stdin protocol faithfully so a paint never waits.
FAKE_HELPER = '''#!/usr/bin/env python3
import base64, json, os, sys, threading, time

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "helper.pid"), "w") as handle:
    handle.write(str(os.getpid()))

lines = [
    {"event": "starting"},
    {"event": "waiting"},
    {"event": "ready"},
    {"event": "control", "control": "key0", "state": "press"},
    {"event": "control", "control": "key0", "state": "release"},
    {"event": "control", "control": "topClockwise", "state": "press"},
    {"event": "control", "control": "topClockwise", "state": "release"},
    {"event": "control", "control": "button6", "state": "press"},
    {"event": "control", "control": "not-a-control", "state": "press"},
    {"event": "device-lost"},
    {"event": "unknown"},
    {"event": "error", "message": "synthetic failure"},
    "this is not json",
]
for payload in lines:
    sys.stdout.write((payload if isinstance(payload, str) else json.dumps(payload)) + "\\n")
    sys.stdout.flush()
    time.sleep(0.02)

def answer_paints():
    # Faithful emulation of the paint command: decode, then answer one result line.
    for raw in sys.stdin:
        try:
            command = json.loads(raw)
        except ValueError:
            continue
        if isinstance(command, dict) and command.get("command") == "paint":
            try:
                size = len(base64.b64decode(command.get("payload", ""), validate=True))
            except Exception:
                size = 0
            sys.stdout.write(json.dumps({"result": "ok", "command": "paint", "bytes": size}) + "\\n")
            sys.stdout.flush()

threading.Thread(target=answer_paints, daemon=True).start()

while True:
    time.sleep(0.5)
'''

# A helper that exits immediately, to exercise the restart-with-backoff path.
CRASH_HELPER = '''#!/usr/bin/env python3
import os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "starts.txt"), "a") as handle:
    handle.write("x")
sys.exit(0)
'''


BLOCKED_HELPER = '''#!/usr/bin/env python3
import json, os, sys, time

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "helper.pid"), "w") as handle:
    handle.write(str(os.getpid()))

# A deck is attached and will not answer: `waiting` says nothing is attached, and
# `blocked` says this.  The helper stays alive, as the real one does, retrying.
for line in (
    {"event": "starting"},
    {"event": "waiting"},
    {"event": "error", "message": "no AKP03 interface matched the required vendor HID identity"},
    {"event": "blocked", "message": "no AKP03 interface matched the required vendor HID identity"},
):
    print(json.dumps(line), flush=True)

time.sleep(30)
'''


def write_helper(directory: Path, source: str, name: str = "fake_helper.py") -> Path:
    path = directory / name
    path.write_text(source)
    path.chmod(0o755)
    return path


def drain(subscription) -> list:
    frames = []
    while True:
        try:
            frames.append(subscription.get(timeout=0.05))
        except queue.Empty:
            return frames


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def tap(self, keys) -> None:
        self.calls.append(("tap", tuple(keys)))

    def press(self, keys) -> None:
        self.calls.append(("press", tuple(keys)))

    def release(self, keys) -> None:
        self.calls.append(("release", tuple(keys)))


def config(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


# -- Pure decoding -------------------------------------------------------------

@pytest.mark.parametrize(
    "line",
    ["", "   ", "not json", "[]", "null", "123", '{"event": 3}',
     "{" + "x" * 70000 + "}"],
)
def test_malformed_lines_decode_to_none(line: str) -> None:
    assert decode_stream_line(line) is None


def test_valid_lines_decode_all_fields() -> None:
    assert decode_stream_line('{"event":"ready"}') == StreamMessage("ready")
    assert decode_stream_line('{"event":"control","control":"key0","state":"press"}') == StreamMessage(
        "control", "key0", "press"
    )
    assert decode_stream_line('{"event":"error","message":"boom"}') == StreamMessage("error", None, None, "boom")
    # A "control" event with missing fields still decodes; the deck rejects it
    # later rather than treating a well-formed line as garbage.
    assert decode_stream_line('{"event":"control"}') == StreamMessage("control")
    # Extra whitespace and unknown fields are tolerated.
    assert decode_stream_line('  {"event":"waiting","extra":1}  ') == StreamMessage("waiting")


@pytest.mark.parametrize(
    "control, state, expected",
    [
        ("key0", "press", Event("key", "key0", True, None, b"")),
        ("key5", "release", Event("key", "key5", False, None, b"")),
        ("button8", "press", Event("button", "button8", True, None, b"")),
        ("topPress", "release", Event("knob_press", "top", False, None, b"")),
        ("topClockwise", "press", Event("knob_turn", "top", None, 1, b"")),
        ("topCounterClockwise", "press", Event("knob_turn", "top", None, -1, b"")),
        ("bottomRightClockwise", "press", Event("knob_turn", "bottom_right", None, 1, b"")),
        # A rotation release is not a second edge.
        ("topClockwise", "release", None),
        # Unknown controls and states never become events.
        ("nope", "press", None),
        ("key0", "mash", None),
    ],
)
def test_control_events_map_to_the_daemon_model(control: str, state: str, expected) -> None:
    assert event_from_control(control, state) == expected


# -- Live helper supervision ---------------------------------------------------

def test_hidstream_deck_reacts_to_the_frozen_protocol(tmp_path: Path) -> None:
    helper = write_helper(tmp_path, FAKE_HELPER)
    feed = ControlFeed(connected=False, phase="starting")
    subscription = feed.subscribe()

    deck = HidStreamDeck("/dev/ignored", helper=str(helper), feed=feed)
    events: list[Event] = []
    disconnects: list[BaseException] = []
    readies: list[bool] = []
    deck.on_event(events.append)
    deck.on_disconnect(disconnects.append)
    deck.on_ready(lambda: readies.append(True))

    deck.open()
    pid_file = tmp_path / "helper.pid"
    try:
        assert wait_until(pid_file.exists)
        assert wait_until(lambda: disconnects)
        assert wait_until(lambda: feed.phase == "lost")
        states = [(f.data["connected"], f.data["phase"]) for f in drain(subscription) if f.event == "state"]
    finally:
        pid = int(pid_file.read_text())
        deck.close()

    assert readies == [True]
    assert disconnects and "device lost" in str(disconnects[0])
    assert [(event.kind, event.control, event.pressed, event.delta) for event in events] == [
        ("key", "key0", True, None),
        ("key", "key0", False, None),
        ("knob_turn", "top", None, 1),
        ("button", "button6", True, None),
    ]
    # The state timeline reached ready, then lost; malformed/unknown/error lines
    # never disturbed it.
    assert states == [(False, "starting"), (False, "waiting"), (True, "ready"), (False, "lost")]
    # close() reaped the helper: no orphan process is left behind.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert deck.connected is False


def test_a_deck_that_is_present_but_silent_is_not_reported_as_absent(tmp_path: Path) -> None:
    """The helper tells "nothing attached" from "attached and not answering", and the
    daemon has to keep them apart: only the first is answered by plugging a deck in."""
    helper = write_helper(tmp_path, BLOCKED_HELPER, name="blocked_helper.py")
    feed = ControlFeed(connected=False, phase="starting")
    subscription = feed.subscribe()

    deck = HidStreamDeck("/dev/ignored", helper=str(helper), feed=feed)
    deck.open()
    pid_file = tmp_path / "helper.pid"
    try:
        assert wait_until(pid_file.exists)
        assert wait_until(lambda: feed.phase == "blocked")
        states = [(f.data["connected"], f.data["phase"]) for f in drain(subscription) if f.event == "state"]
    finally:
        deck.close()

    assert states == [(False, "starting"), (False, "waiting"), (False, "blocked")]
    assert deck.connected is False


def test_helper_crash_is_restarted_with_backoff(tmp_path: Path) -> None:
    helper = write_helper(tmp_path, CRASH_HELPER, name="crash_helper.py")
    deck = HidStreamDeck("/dev/ignored", helper=str(helper), backoff=(0.05,))
    starts = tmp_path / "starts.txt"
    deck.open()
    try:
        assert wait_until(lambda: starts.exists() and len(starts.read_text()) >= 2, timeout=3)
        # A helper that dies without ever reporting ready leaves the deck "lost".
        assert deck.phase == "lost"
    finally:
        deck.close()
    assert deck.connected is False


def test_missing_helper_is_a_clean_unavailable_error(tmp_path: Path) -> None:
    deck = HidStreamDeck("/dev/ignored", helper=str(tmp_path / "absent-helper"))
    with pytest.raises(OSError, match="not found"):
        deck.open()
    deck.close()


def test_select_deck_factory_is_platform_specific() -> None:
    assert select_deck_factory("linux") is Deck
    factory = select_deck_factory("darwin", helper="/does/not/matter")
    deck = factory("/dev/hidraw0")
    assert isinstance(deck, HidStreamDeck)
    deck.close()


# -- Daemon wiring -------------------------------------------------------------

def test_daemon_dispatches_macos_stream_events(tmp_path: Path) -> None:
    helper = write_helper(tmp_path, FAKE_HELPER)
    path = tmp_path / "config.toml"
    config(path, '[bindings.key0]\naction="keys"\nkeys=["enter"]\n')
    backend = RecordingBackend()
    deck = HidStreamDeck("/dev/ignored", helper=str(helper))
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck, backend=backend)
    try:
        assert daemon.reload() and daemon.connect()
        assert wait_until(lambda: backend.calls)
    finally:
        daemon.close_deck()
    assert ("tap", ("enter",)) in backend.calls


def test_daemon_reapplies_state_when_backend_reports_ready(tmp_path: Path) -> None:
    helper = write_helper(tmp_path, FAKE_HELPER)
    path = tmp_path / "config.toml"
    config(path, '[labels]\nkey0="Hello"\n')
    deck = HidStreamDeck("/dev/ignored", helper=str(helper))
    paints: list[int] = []
    deck.set_key_image = lambda key, image: paints.append(key)  # type: ignore[method-assign]
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck)
    try:
        assert daemon.reload() and daemon.connect()
        paints.clear()
        deck._fire_ready()  # type: ignore[attr-defined]
        assert 0 in paints
    finally:
        daemon.close_deck()


def test_stat_mode_helper_is_executable_by_which(tmp_path: Path) -> None:
    helper = write_helper(tmp_path, CRASH_HELPER, name="mode_helper.py")
    assert stat.S_IMODE(helper.stat().st_mode) == 0o755
    deck = HidStreamDeck("/dev/ignored", helper=str(helper))
    deck.open()
    deck.close()


# -- Open-protocol mode: resolution and forwarding -----------------------------

class _FakeProcess:
    """Minimal Popen stand-in so ``_spawn`` can be observed without a helper."""

    def __init__(self) -> None:
        import io

        self.pid = 4242
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int:
        return 0


def _recording_popen(recorded: list[list[str]]) -> Callable[..., Any]:
    def popen(args: list[str], **_kwargs: Any) -> _FakeProcess:
        recorded.append(list(args))
        return _FakeProcess()

    return popen


def test_resolve_open_protocol_defaults_to_wake() -> None:
    assert DEFAULT_OPEN_PROTOCOL == "wake"
    assert resolve_open_protocol(environ={}) == "wake"


def test_resolve_open_protocol_env_overrides_default() -> None:
    assert resolve_open_protocol(environ={OPEN_PROTOCOL_ENV_VAR: "none"}) == "none"
    assert resolve_open_protocol(environ={OPEN_PROTOCOL_ENV_VAR: "full"}) == "full"


def test_resolve_open_protocol_explicit_beats_env() -> None:
    assert resolve_open_protocol("full", environ={OPEN_PROTOCOL_ENV_VAR: "none"}) == "full"


@pytest.mark.parametrize("bad", ["", "WAKE", "wake ", "everything", "bogus"])
def test_resolve_open_protocol_rejects_unknown(bad: str) -> None:
    assert resolve_open_protocol(bad, environ={}) == "wake"
    assert resolve_open_protocol(environ={OPEN_PROTOCOL_ENV_VAR: bad}) == "wake"


def test_spawn_forwards_open_protocol_and_poll_seconds() -> None:
    recorded: list[list[str]] = []
    deck = HidStreamDeck(
        "/dev/ignored",
        helper="/fake/AKP03HIDStream",
        open_protocol="full",
        poll_seconds=5,
        popen=_recording_popen(recorded),
    )
    deck._spawn()  # type: ignore[attr-defined]
    assert recorded == [["/fake/AKP03HIDStream", "--open-protocol", "full", "--poll-seconds", "5"]]


def test_spawn_defaults_to_wake_open_protocol() -> None:
    recorded: list[list[str]] = []
    deck = HidStreamDeck(
        "/dev/ignored",
        helper="/fake/AKP03HIDStream",
        open_protocol=None,
        popen=_recording_popen(recorded),
    )
    assert deck.open_protocol == "wake"
    deck._spawn()  # type: ignore[attr-defined]
    assert recorded == [["/fake/AKP03HIDStream", "--open-protocol", "wake"]]


def test_spawn_honours_open_protocol_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OPEN_PROTOCOL_ENV_VAR, "none")
    recorded: list[list[str]] = []
    deck = HidStreamDeck(
        "/dev/ignored",
        helper="/fake/AKP03HIDStream",
        popen=_recording_popen(recorded),
    )
    assert deck.open_protocol == "none"
    deck._spawn()  # type: ignore[attr-defined]
    assert recorded == [["/fake/AKP03HIDStream", "--open-protocol", "none"]]


def test_select_deck_factory_forwards_open_protocol() -> None:
    factory = select_deck_factory("darwin", helper="/does/not/matter", open_protocol="full")
    deck = factory("/dev/hidraw0")
    assert isinstance(deck, HidStreamDeck)
    assert deck.open_protocol == "full"
    deck.close()


# -- Painting: the byte-exact Linux packet sequence, over the helper's stdin -----
#
# The "deck" here is a fake helper that records every received paint line and
# answers it, so the test can assert the daemon hands the transport exactly the
# Linux packet sequence -- with no hardware and no /dev/hidraw.

# Records each received command line verbatim, then answers per a mode file:
# "ok" (default), "error" (refuse every paint), or "silent" (never answer).
RECORDING_HELPER = '''#!/usr/bin/env python3
import base64, json, os, sys

here = os.path.dirname(os.path.abspath(__file__))

def mode():
    try:
        with open(os.path.join(here, "mode.txt")) as handle:
            return handle.read().strip()
    except OSError:
        return "ok"

sys.stdout.write(json.dumps({"event": "ready"}) + "\\n")
sys.stdout.flush()

with open(os.path.join(here, "paints.txt"), "a") as record:
    for raw in sys.stdin:
        record.write(raw if raw.endswith("\\n") else raw + "\\n")
        record.flush()
        current = mode()
        if current == "silent":
            continue
        if current == "error":
            sys.stdout.write(json.dumps({"result": "error", "command": "paint", "reason": "device write failed"}) + "\\n")
        else:
            try:
                size = len(base64.b64decode(json.loads(raw).get("payload", ""), validate=True))
            except Exception:
                size = 0
            sys.stdout.write(json.dumps({"result": "ok", "command": "paint", "bytes": size}) + "\\n")
        sys.stdout.flush()
'''


def recorded_packets(path: Path, expected_count: int, timeout: float = 3.0) -> list[bytes]:
    """The decoded 1024-byte packets the fake helper recorded, once there are enough."""
    deadline = time.monotonic() + timeout
    packets: list[bytes] = []
    while time.monotonic() < deadline:
        packets = []
        if path.exists():
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    packets.append(base64.b64decode(json.loads(line)["payload"]))
                except (ValueError, KeyError, TypeError):
                    break
            if len(packets) >= expected_count:
                return packets
        time.sleep(0.01)
    return packets


class _Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def deck_logger() -> tuple[logging.Logger, "_Recorder"]:
    logger = logging.getLogger(f"test.hidstream.{uuid4().hex}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _Recorder()
    logger.addHandler(handler)
    return logger, handler


def test_result_lines_decode_and_are_distinct_from_events() -> None:
    assert decode_result_line('{"result":"ok","command":"paint","bytes":1024}') == {
        "result": "ok", "command": "paint", "bytes": 1024,
    }
    refused = decode_result_line('{"result":"error","command":"paint","reason":"device write failed"}')
    assert refused is not None and refused["reason"] == "device write failed"
    # An event line is not a result line, and a result line is not an event.
    assert decode_result_line('{"event":"ready"}') is None
    assert decode_result_line('{"event":"error","message":"x"}') is None
    assert decode_stream_line('{"result":"ok","command":"paint","bytes":1024}') is None
    for malformed in ["", "   ", "not json", "[]", "null", '{"command":"paint"}', '{"event":"ready","result":"ok"}']:
        assert decode_result_line(malformed) is None


def test_macos_paint_sequence_is_byte_identical_to_the_linux_sequence(tmp_path: Path) -> None:
    helper = write_helper(tmp_path, RECORDING_HELPER, name="recording_helper.py")
    deck = HidStreamDeck("/dev/ignored", helper=str(helper))
    deck.open()
    try:
        assert wait_until(lambda: deck.connected)
        source = Image.new("RGB", (60, 60), "#123456")
        deck.set_key_image(2, source)
        # Exactly what the Linux ``Deck.set_key_image`` writes to hidraw.
        jpeg = images.to_device_jpeg(source)
        expected = [protocol.image_header(2, len(jpeg)), *protocol.jpeg_chunks(jpeg), protocol.flush()]
        received = recorded_packets(tmp_path / "paints.txt", len(expected))
    finally:
        deck.close()

    assert received == expected
    assert len(received) == len(expected) >= 3
    assert all(len(packet) == 1024 for packet in received)
    # Header: CRT\0\0 prefix, BAT, then two big-endian length bytes, then key byte (key + 1).
    header = received[0]
    assert header[:5] == b"CRT\x00\x00"
    assert header[5:10] == b"BAT\x00\x00"
    assert header[10:12] == len(jpeg).to_bytes(2, "big")
    assert header[12] == 3
    # A flush always closes the upload.
    assert received[-1][:5] == b"CRT\x00\x00"
    assert received[-1][5:8] == b"STP"
    assert received[-1][:5] == protocol.flush()[:5]


def test_macos_brightness_and_clear_emit_the_linux_packets(tmp_path: Path) -> None:
    helper = write_helper(tmp_path, RECORDING_HELPER, name="recording_helper.py")
    deck = HidStreamDeck("/dev/ignored", helper=str(helper))
    deck.open()
    try:
        assert wait_until(lambda: deck.connected)
        deck.set_brightness(25)
        deck.clear()  # no key -> clear all
        expected = [protocol.brightness(25), protocol.clear_all(), protocol.flush()]
        received = recorded_packets(tmp_path / "paints.txt", len(expected))
    finally:
        deck.close()

    assert received == expected
    assert all(len(packet) == 1024 for packet in received)
    assert received[0][:11] == b"CRT\x00\x00LIG\x00\x00\x19"  # level 25
    assert received[1][:12] == b"CRT\x00\x00CLE\x00\x00\x00\xff"


def test_hid_stream_transport_refuses_anything_but_1024_bytes() -> None:
    deck = HidStreamDeck("/dev/ignored", helper="/does/not/exist")
    try:
        for bad in (b"", b"short", b"\x00" * 1023, b"\x00" * 1025):
            with pytest.raises(ValueError, match="exactly 1024 bytes"):
                deck.transport.write(bad)
        # A correct-length payload with no helper running fails honestly.
        with pytest.raises(HidStreamTransportError, match="not running"):
            deck.transport.write(b"\x00" * 1024)
    finally:
        deck.close()


def test_a_refused_paint_is_logged_once_and_never_raises(tmp_path: Path) -> None:
    helper = write_helper(tmp_path, RECORDING_HELPER, name="recording_helper.py")
    (tmp_path / "mode.txt").write_text("error")
    logger, handler = deck_logger()
    deck = HidStreamDeck("/dev/ignored", helper=str(helper), logger=logger)
    deck.open()
    try:
        assert wait_until(lambda: deck.connected)
        source = Image.new("RGB", (60, 60), "blue")
        deck.set_key_image(0, source)  # refused by the helper
        deck.set_key_image(0, source)  # same reason: deduplicated, still no raise
        assert deck.connected is True  # the input stream and daemon are unharmed
        received = recorded_packets(tmp_path / "paints.txt", 1)
    finally:
        deck.close()

    assert len(received) >= 1  # the paint was attempted
    errors = [record.getMessage() for record in handler.records if record.levelno == logging.ERROR]
    assert errors == ["cannot paint the AKP03 deck: device write failed"]


def test_an_unanswered_paint_is_reported_once_and_painting_is_suspended(tmp_path: Path) -> None:
    helper = write_helper(tmp_path, RECORDING_HELPER, name="recording_helper.py")
    (tmp_path / "mode.txt").write_text("silent")
    logger, handler = deck_logger()
    deck = HidStreamDeck("/dev/ignored", helper=str(helper), logger=logger, paint_timeout=0.3)
    deck.open()
    try:
        assert wait_until(lambda: deck.connected)
        source = Image.new("RGB", (60, 60), "green")
        deck.set_key_image(0, source)  # no answer -> one ERROR, painting suspended
        deck.set_key_image(0, source)  # skipped entirely: no new write, no new ERROR
        received = recorded_packets(tmp_path / "paints.txt", 1)
        time.sleep(0.2)
        lines = [line for line in (tmp_path / "paints.txt").read_text().splitlines() if line.strip()]
    finally:
        deck.close()

    errors = [record.getMessage() for record in handler.records if record.levelno == logging.ERROR]
    assert errors.count("cannot paint the AKP03 deck: AKP03HIDStream did not acknowledge the paint") == 1
    assert any("suspending AKP03 painting" in message for message in errors)
    assert deck._paint_unavailable is True  # type: ignore[attr-defined]
    # Only the first attempt wrote a command; the suspended one wrote nothing.
    assert len(received) >= 1
    assert len(lines) == len(received)


def test_paint_error_reports_the_reason_and_clears_after_a_paint_that_lands(tmp_path: Path) -> None:
    """The honest signal the editor uses: why the panel did not update, or None."""
    helper = write_helper(tmp_path, RECORDING_HELPER, name="recording_helper.py")
    deck = HidStreamDeck("/dev/ignored", helper=str(helper))
    deck.open()
    try:
        assert wait_until(lambda: deck.connected)
        (tmp_path / "mode.txt").write_text("error")
        deck.set_key_image(0, Image.new("RGB", (60, 60), "red"))
        assert deck.paint_error == "device write failed"

        (tmp_path / "mode.txt").write_text("ok")
        deck.set_key_image(0, Image.new("RGB", (60, 60), "red"))
        assert deck.paint_error is None
    finally:
        deck.close()


def test_a_paint_before_the_deck_is_ready_is_reported_and_not_claimed() -> None:
    """Nothing is written and nothing is claimed while the helper has no device."""
    deck = HidStreamDeck("/dev/ignored", helper="/does/not/exist")
    deck.set_key_image(0, Image.new("RGB", (60, 60), "red"))
    assert deck.paint_error == "the AKP03 deck is not ready yet"
    deck.close()
