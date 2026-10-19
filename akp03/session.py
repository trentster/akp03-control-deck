"""Persistent AKP03 session and its local Unix-socket control protocol."""
from __future__ import annotations

import argparse
import os
import shlex
import signal
import socket
import stat
import sys
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable, Sequence, TextIO

from PIL import Image, ImageDraw, ImageFont

from .device import Deck, Event as DeckEvent

MAX_LINE = 4096
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SOCKET_NAME = "akp03.sock"


class CommandError(ValueError):
    """A client command that is not part of the local control API."""


def default_socket_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / SOCKET_NAME
    return Path.home() / "akp03" / "run" / SOCKET_NAME


def _integer(value: str, name: str, low: int, high: int) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise CommandError(f"{name} must be an integer from {low} through {high}") from error
    if not low <= result <= high:
        raise CommandError(f"{name} must be an integer from {low} through {high}")
    return result


def _image_file(value: str) -> Path:
    path = Path(value)
    try:
        details = path.stat()
    except OSError as error:
        raise CommandError("image file is not accessible") from error
    if not stat.S_ISREG(details.st_mode):
        raise CommandError("image file must be a regular file")
    if details.st_size > MAX_IMAGE_BYTES:
        raise CommandError("image file exceeds 5 MiB")
    return path


def parse_command(line: str) -> tuple[str, tuple[object, ...]]:
    """Parse one bounded, allowlisted control-protocol request."""
    if not line or len(line) > MAX_LINE or "\x00" in line:
        raise CommandError("invalid request")
    try:
        fields = shlex.split(line, posix=True)
    except ValueError as error:
        raise CommandError("invalid request") from error
    if not fields:
        raise CommandError("invalid request")
    command, *args = fields
    if command == "status" and not args:
        return command, ()
    if command == "brightness" and len(args) == 1:
        level = _integer(args[0], "brightness", 0, 100)
        if level == 0:
            raise CommandError("brightness 0 is ignored by AKP03E firmware; use 1-100, or clear keys for dark")
        return command, (level,)
    if command == "clear" and len(args) <= 1:
        return command, (() if not args else (_integer(args[0], "key", 0, 5),))
    if command == "color" and len(args) == 4:
        return command, (
            _integer(args[0], "key", 0, 5),
            tuple(_integer(part, "colour component", 0, 255) for part in args[1:]),
        )
    if command == "image" and len(args) == 2:
        return command, (_integer(args[0], "key", 0, 5), _image_file(args[1]))
    if command == "testcard" and not args:
        return command, ()
    if command == "quit" and not args:
        return command, ()
    raise CommandError("unknown command or invalid arguments")


def _testcard(key: int) -> Image.Image:
    """Orientation card: index, top-edge bar, and top-left colour marker."""
    colours = ("red", "green", "blue", "yellow", "magenta", "cyan")
    colour = colours[key]
    image = Image.new("RGB", (60, 60), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 59, 5), fill=colour)
    draw.rectangle((0, 0, 13, 13), fill=colour)
    try:
        font = ImageFont.load_default(size=38)
    except TypeError:  # Pillow versions before scalable default-font support.
        font = ImageFont.load_default()
    text = str(key)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((60 - (box[2] - box[0])) // 2, 13), text, fill="white", font=font)
    return image


class Session:
    """One open Deck and a deliberately small local command surface."""

    def __init__(self, deck: Deck, *, output: TextIO | None = None, log: TextIO | None = None) -> None:
        self.deck = deck
        self.output = output if output is not None else sys.stdout
        self.log = log
        self.stop = Event()
        self.disconnected: BaseException | None = None

    def event(self, event: DeckEvent) -> None:
        detail = f"delta={event.delta}" if event.delta is not None else f"pressed={event.pressed}"
        self._write(f"{datetime.now().astimezone().isoformat(timespec='milliseconds')} {event.control} {detail}")

    def disconnect(self, error: BaseException) -> None:
        self.disconnected = error
        self._write(f"disconnect: {error}", stream=sys.stderr)
        self.stop.set()

    def _write(self, text: str, *, stream: TextIO | None = None) -> None:
        target = stream if stream is not None else self.output
        print(text, file=target, flush=True)
        if self.log is not None:
            print(text, file=self.log, flush=True)

    def execute(self, line: str) -> str:
        command, args = parse_command(line)
        if command == "status":
            return "OK STATUS"
        if command == "brightness":
            self.deck.set_brightness(*args)  # type: ignore[arg-type]
        elif command == "clear":
            self.deck.clear(*args)  # type: ignore[arg-type]
        elif command == "color":
            self.deck.set_key_color(*args)  # type: ignore[arg-type]
        elif command == "image":
            self.deck.set_key_image(*args)  # type: ignore[arg-type]
        elif command == "testcard":
            for key in range(6):
                self.deck.set_key_image(key, _testcard(key))
        elif command == "quit":
            self.stop.set()
            return "OK QUIT"
        return "OK"


def _recv_line(connection: socket.socket) -> str:
    data = bytearray()
    while len(data) <= MAX_LINE:
        part = connection.recv(min(1024, MAX_LINE + 1 - len(data)))
        if not part:
            break
        data.extend(part)
        if b"\n" in part:
            break
    if len(data) > MAX_LINE or not data.endswith(b"\n") or data.count(b"\n") != 1:
        raise CommandError("invalid request")
    try:
        return data[:-1].decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise CommandError("invalid request") from error


def _send(connection: socket.socket, response: str) -> None:
    try:
        connection.sendall((response + "\n").encode("utf-8"))
    except (BrokenPipeError, ConnectionResetError):
        # A client timeout/disconnect must never terminate the deck owner.
        return


def socket_is_live(path: Path, *, timeout: float = 0.2) -> bool:
    try:
        response = send_command("status", path, timeout=timeout)
    except OSError:
        return False
    return response == "OK STATUS"


def open_socket(path: Path) -> socket.socket:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if not path.is_socket():
            raise RuntimeError(f"refusing to replace non-socket path: {path}")
        if socket_is_live(path):
            raise RuntimeError(f"session already running: {path}")
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        listener.bind(os.fspath(path))
    finally:
        os.umask(old_umask)
    try:
        # The 0177 umask creates the socket as 0600 from the first visible instant.
        os.chmod(path, 0o600)
        listener.listen()
        listener.settimeout(0.2)
        return listener
    except BaseException:
        listener.close()
        if path.exists() and path.is_socket():
            path.unlink()
        raise


def serve(session: Session, path: Path) -> int:
    listener = open_socket(path)
    try:
        while not session.stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                try:
                    response = session.execute(_recv_line(connection))
                except CommandError as error:
                    response = f"ERR {error}"
                except Exception:
                    response = "ERR command failed"
                _send(connection, response)
    finally:
        listener.close()
        if path.exists() and path.is_socket():
            path.unlink()
    return 1 if session.disconnected is not None else 0


def run_session(*, device: str, socket_path: Path, log_path: Path | None = None, deck_factory: Callable[[str], Deck] = Deck) -> int:
    log = log_path.open("a", encoding="utf-8") if log_path is not None else None
    deck = deck_factory(device)
    session = Session(deck, log=log)
    previous: dict[int, object] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        session.stop.set()

    result = 1
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        deck.on_event(session.event)
        deck.on_disconnect(session.disconnect)
        deck.open()
        result = serve(session, socket_path)
    except OSError as error:
        print(f"session error: {error}", file=sys.stderr)
    finally:
        try:
            deck.close()
        except OSError as error:
            print(f"session close error: {error}", file=sys.stderr)
            result = 1
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
        if log is not None:
            log.close()
    return result


def send_command(command: str, socket_path: Path | None = None, *, timeout: float = 2.0) -> str:
    path = default_socket_path() if socket_path is None else socket_path
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(os.fspath(path))
        client.sendall((command + "\n").encode("utf-8"))
        response = _recv_line(client)
    if not (response.startswith("OK") or response.startswith("ERR")):
        raise OSError("invalid response from session")
    return response


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akp03")
    commands = parser.add_subparsers(dest="command", required=True)
    session = commands.add_parser("session")
    session.add_argument("--log", type=Path)
    commands.add_parser("status")
    brightness = commands.add_parser("brightness")
    brightness.add_argument("level")
    clear = commands.add_parser("clear")
    clear.add_argument("key", nargs="?")
    color = commands.add_parser("color")
    color.add_argument("key")
    color.add_argument("red")
    color.add_argument("green")
    color.add_argument("blue")
    image = commands.add_parser("image")
    image.add_argument("key")
    image.add_argument("file")
    commands.add_parser("testcard")
    commands.add_parser("quit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    socket_path = default_socket_path()
    if args.command == "session":
        return run_session(device="/dev/hidraw0", socket_path=socket_path, log_path=args.log)
    values = [args.command]
    if args.command == "brightness":
        values.append(args.level)
    elif args.command == "clear" and args.key is not None:
        values.append(args.key)
    elif args.command == "color":
        values.extend((args.key, args.red, args.green, args.blue))
    elif args.command == "image":
        values.extend((args.key, os.fspath(Path(args.file).resolve())))
    try:
        response = send_command(" ".join(shlex.quote(value) for value in values), socket_path)
    except OSError:
        print("no session running — start: python -m akp03 session", file=sys.stderr)
        return 1
    print(response)
    return 0 if response.startswith("OK") else 1
