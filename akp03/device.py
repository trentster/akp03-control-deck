"""High-level, threaded AKP03 deck API."""
from __future__ import annotations

from queue import Queue
from threading import Event as ThreadEvent, Lock, Thread
from time import monotonic
from typing import Callable

from PIL import Image

from . import images, protocol
from .events import Event, decode_event
from .transport import HidrawTransport


class Deck:
    """Own one AKP03 device. Calls to image upload are never interleaved."""

    def __init__(self, path: str = "/dev/hidraw0", *, brightness: int = 50) -> None:
        self.transport = HidrawTransport(path)
        self.brightness = brightness
        self._callbacks: list[Callable[[Event], None]] = []
        self._disconnect_callbacks: list[Callable[[BaseException], None]] = []
        self._events: Queue[Event] = Queue()
        self._stop = ThreadEvent()
        self._write_lock = Lock()
        self._threads: list[Thread] = []

    def on_event(self, callback: Callable[[Event], None]) -> Callable[[Event], None]:
        self._callbacks.append(callback)
        return callback

    def on_disconnect(self, callback: Callable[[BaseException], None]) -> Callable[[BaseException], None]:
        self._disconnect_callbacks.append(callback)
        return callback

    def _send(self, payload: bytes) -> None:
        with self._write_lock:
            self.transport.write(payload)

    def open(self) -> "Deck":
        self.transport.open()
        self._stop.clear()
        with self._write_lock:
            self.transport.write(protocol.wake())
            self.transport.write(protocol.brightness(self.brightness))
            self.transport.write(protocol.clear_all())
            self.transport.write(protocol.flush())
        self._threads = [
            Thread(target=self._reader, name="akp03-reader", daemon=True),
            Thread(target=self._dispatcher, name="akp03-dispatcher", daemon=True),
            Thread(target=self._keepalive, name="akp03-keepalive", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        return self

    def set_brightness(self, level: int) -> None:
        self._send(protocol.brightness(level))
        self.brightness = level

    def clear(self, key: int | None = None) -> None:
        with self._write_lock:
            self.transport.write(protocol.clear_all() if key is None else protocol.clear_key(key))
            self.transport.write(protocol.flush())

    def set_key_image(self, key: int, source: object) -> None:
        # Store artwork source-oriented; the fixed physical panel transform is
        # applied exactly once at this upload boundary.
        jpeg = images.to_device_jpeg(source)  # type: ignore[arg-type]
        chunks = protocol.jpeg_chunks(jpeg)
        with self._write_lock:
            self.transport.write(protocol.image_header(key, len(jpeg)))
            for chunk in chunks:
                self.transport.write(chunk)
            self.transport.write(protocol.flush())

    def set_key_color(self, key: int, color: str | tuple[int, int, int]) -> None:
        self.set_key_image(key, Image.new("RGB", images.KEY_SIZE, color))

    def _reader(self) -> None:
        try:
            while not self._stop.is_set():
                event = decode_event(self.transport.read())
                if event is not None:
                    self._events.put(event)
        except OSError as error:
            if not self._stop.is_set():
                self._stop.set()
                for callback in tuple(self._disconnect_callbacks):
                    callback(error)

    def _dispatcher(self) -> None:
        while not self._stop.is_set() or not self._events.empty():
            try:
                event = self._events.get(timeout=0.2)
            except Exception:
                continue
            for callback in tuple(self._callbacks):
                callback(event)

    def _keepalive(self) -> None:
        while not self._stop.wait(10):
            try:
                self._send(protocol.keepalive())
            except OSError as error:
                if not self._stop.is_set():
                    self._stop.set()
                    for callback in tuple(self._disconnect_callbacks):
                        callback(error)
                return

    def close(self) -> None:
        if self.transport._fd is None:
            return
        self._stop.set()
        try:
            with self._write_lock:
                self.transport.write(protocol.clear_all())
                self.transport.write(protocol.flush())
        finally:
            self.transport.close()
        for thread in self._threads:
            thread.join(timeout=1)
        self._threads = []

    def __enter__(self) -> "Deck":
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()
