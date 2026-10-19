from __future__ import annotations

import stat
import threading
from pathlib import Path

import pytest

from akp03 import session


class FakeDeck:
    def __init__(self, _path: str = "/dev/fake") -> None:
        self.calls: list[tuple[object, ...]] = []
        self.events = None
        self.disconnect = None
        self.opened = False

    def on_event(self, callback):
        self.events = callback

    def on_disconnect(self, callback):
        self.disconnect = callback

    def open(self):
        self.opened = True
        return self

    def close(self):
        self.calls.append(("close",))

    def set_brightness(self, level):
        self.calls.append(("brightness", level))

    def clear(self, key=None):
        self.calls.append(("clear", key))

    def set_key_color(self, key, color):
        self.calls.append(("color", key, color))

    def set_key_image(self, key, image):
        self.calls.append(("image", key, image))


def test_parse_command_allowlist_and_validation(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    assert session.parse_command("brightness 100") == ("brightness", (100,))
    assert session.parse_command("clear") == ("clear", ())
    assert session.parse_command("color 5 1 2 3") == ("color", (5, (1, 2, 3)))
    assert session.parse_command(f"image 0 {image}") == ("image", (0, image))
    for line in ("brightness 101", "clear 6", "color 0 0 0 256", "raw x", "quit now"):
        with pytest.raises(session.CommandError):
            session.parse_command(line)
    with pytest.raises(session.CommandError, match="ignored by AKP03E firmware"):
        session.parse_command("brightness 0")
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(session.CommandError, match="regular"):
        session.parse_command(f"image 0 {directory}")
    huge = tmp_path / "huge"
    huge.write_bytes(b"x" * (session.MAX_IMAGE_BYTES + 1))
    with pytest.raises(session.CommandError, match="5 MiB"):
        session.parse_command(f"image 0 {huge}")


def test_session_execute_only_exposes_deck_methods() -> None:
    deck = FakeDeck()
    running = session.Session(deck)  # type: ignore[arg-type]
    assert running.execute("status") == "OK STATUS"
    assert running.execute("brightness 20") == "OK"
    assert running.execute("clear 1") == "OK"
    assert running.execute("color 2 3 4 5") == "OK"
    assert deck.calls == [("brightness", 20), ("clear", 1), ("color", 2, (3, 4, 5))]
    assert running.execute("quit") == "OK QUIT"
    assert running.stop.is_set()


def test_socket_server_client_and_socket_mode(tmp_path: Path) -> None:
    path = tmp_path / "akp03.sock"
    deck = FakeDeck()
    running = session.Session(deck)  # type: ignore[arg-type]
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(session.serve(running, path)))
    thread.start()
    for _ in range(50):
        if path.exists():
            break
        threading.Event().wait(0.01)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert session.send_command("brightness 7", path) == "OK"
    assert session.send_command("brightness 1000", path).startswith("ERR")
    assert session.send_command("quit", path) == "OK QUIT"
    thread.join(2)
    assert not thread.is_alive()
    assert result == [0]
    assert deck.calls == [("brightness", 7)]
    assert not path.exists()


def test_open_socket_preserves_live_session(tmp_path: Path) -> None:
    path = tmp_path / "akp03.sock"
    deck = FakeDeck()
    running = session.Session(deck)  # type: ignore[arg-type]
    thread = threading.Thread(target=lambda: session.serve(running, path))
    thread.start()
    for _ in range(50):
        if path.exists():
            break
        threading.Event().wait(0.01)
    with pytest.raises(RuntimeError, match="already running"):
        session.open_socket(path)
    assert session.send_command("quit", path) == "OK QUIT"
    thread.join(2)


def test_main_client_does_not_construct_deck(monkeypatch, tmp_path: Path, capsys) -> None:
    calls: list[tuple[str, Path]] = []
    path = tmp_path / "deck.sock"
    monkeypatch.setattr(session, "default_socket_path", lambda: path)
    monkeypatch.setattr(session, "send_command", lambda command, socket: calls.append((command, socket)) or "OK")
    assert session.main(["color", "1", "2", "3", "4"]) == 0
    assert calls == [("color 1 2 3 4", path)]
    assert capsys.readouterr().out == "OK\n"


def test_disconnect_closes_deck_unlinks_socket_and_returns_nonzero(tmp_path: Path) -> None:
    class DisconnectingDeck(FakeDeck):
        def open(self):
            super().open()
            assert self.disconnect is not None
            self.disconnect(OSError("device gone"))
            return self

    deck = DisconnectingDeck()
    path = tmp_path / "akp03.sock"
    assert session.run_session(device="/dev/fake", socket_path=path, deck_factory=lambda _: deck) == 1
    assert ("close",) in deck.calls
    assert not path.exists()


def test_send_ignores_client_disconnect() -> None:
    class GoneClient:
        def sendall(self, _payload):
            raise BrokenPipeError("client went away")
    session._send(GoneClient(), "OK")  # type: ignore[arg-type]
