"""A deck nobody is using puts its keys out.

A fixed bright picture is what burns into a panel, and the deck's own firmware
agrees: it ignores a brightness of 0, so the way to be dark is to clear the keys
and leave the backlight where the person set it.  These tests hold the countdown,
the going dark, and the coming back to the deck's own clock - the daemon takes its
clock and its timer by injection, so a quarter of an hour is one line here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akp03.config import ConfigError, load_config
from akp03.daemon import Daemon
from akp03.device import Event

import support


class Deck:
    def __init__(self, path): self.path, self.calls, self.images = path, [], []
    def on_event(self, cb): self.event = cb
    def on_disconnect(self, cb): self.disconnect = cb
    def open(self): return self
    def close(self): self.calls.append(("close",))
    def clear(self, key=None): self.calls.append(("clear", key))
    def set_key_image(self, key, image): self.calls.append(("image", key)); self.images.append((key, image.copy()))
    def set_brightness(self, level): self.calls.append(("brightness", level))


class Clock:
    """A clock a test can move, without moving it in real time."""
    def __init__(self, now: float = 1_000.0): self.now = now
    def __call__(self) -> float: return self.now
    def tick(self, seconds: float) -> None: self.now += seconds


class FakeTimer:
    def __init__(self, delay, callback): self.delay, self.callback, self.cancelled, self.started = delay, callback, False, False
    def start(self): self.started = True
    def cancel(self): self.cancelled = True
    def fire(self): self.callback()


def config(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def deck_daemon(path: Path, deck: Deck, timers: list, clock: Clock) -> Daemon:
    return Daemon(  # type: ignore[arg-type]
        path, state_path=path.parent / "state", deck_factory=lambda _: deck,
        clock=clock, timer_factory=lambda delay, callback: timers.append(FakeTimer(delay, callback)) or timers[-1],
    )


def showing(deck: Deck) -> dict[int, bytes]:
    """What each key is showing, as the panel last received it.

    Per key rather than in order, because a paint only touches the keys it has
    something for: a menu page with two items leaves the other four alone, and a
    test that read the last six uploads would be reading a mix of two paints.
    """
    latest: dict[int, bytes] = {}
    for key, image in deck.images:
        latest[key] = image.tobytes()
    return latest


MENU = '''[screen]
blank_after_s = 30

[menu]
enabled = true
root = "home"
[[menu.pages]]
id = "home"
title = "Home"
items = [{ label = "Dim", action = "brightness:25" }, { label = "Display", submenu = "display" }]
[[menu.pages]]
id = "display"
title = "Display"
items = [{ label = "Full", action = "brightness:100" }]
'''


def test_a_deck_nobody_touches_puts_its_keys_out(tmp_path):
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake"); timers = []; clock = Clock()
    config(path, '[screen]\nblank_after_s = 30\n[labels]\nkey0 = "Hello"\n')
    daemon = deck_daemon(path, deck, timers, clock)
    assert daemon.reload() and daemon.connect()
    assert len(timers) == 1 and timers[0].delay == 30, "a thirty second countdown starts with the deck"
    assert timers[0].started, "and it is running: a timer that is built but never started never fires"
    before = showing(deck)
    deck.calls.clear()
    clock.tick(30); timers[0].fire()
    # Every key is cleared.  Not one image is uploaded: dark is the cheap half.
    assert [call for call in deck.calls if call[0] == "clear"] == [("clear", key) for key in range(6)]
    assert [call for call in deck.calls if call[0] == "image"] == []
    assert daemon.screen_description()["blanked"] is True
    assert all(key["kind"] == "blank" for key in daemon.screen_description()["keys"])
    assert before, "the keys were showing something before they went dark"


def test_a_countdown_that_outlived_the_last_touch_cannot_darken_a_busy_deck(tmp_path):
    """The timer is a hint; the clock is the fact."""
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake"); timers = []; clock = Clock()
    config(path, '[screen]\nblank_after_s = 30\n[labels]\nkey0 = "Hello"\n')
    daemon = deck_daemon(path, deck, timers, clock)
    assert daemon.reload() and daemon.connect(); stale = timers[0]
    clock.tick(5); daemon.event(Event("key", "key0", True, None, b""))
    assert stale.cancelled, "touching the deck restarts the countdown"
    clock.tick(1); deck.calls.clear()
    stale.fire()
    assert [call for call in deck.calls if call[0] == "clear"] == [], "a stale timer must not put out the keys"
    assert daemon.screen_description()["blanked"] is False
    # And the deck still has a countdown running, so it will go dark when it should.
    assert timers[-1] is not stale and not timers[-1].cancelled


def test_the_next_touch_brings_back_exactly_what_was_there(tmp_path):
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake"); timers = []; clock = Clock()
    config(path, '[screen]\nblank_after_s = 30\n[labels]\nkey0 = "One"\nkey1 = "Two"\nkey2 = "Three"\n'
                 'key3 = "Four"\nkey4 = "Five"\nkey5 = "Six"\n\n[bindings.key0]\naction = "keys"\nkeys = ["enter"]\n')
    daemon = deck_daemon(path, deck, timers, clock)
    assert daemon.reload() and daemon.connect()
    before = showing(deck)
    clock.tick(30); timers[0].fire()
    assert daemon.screen_description()["blanked"] is True
    daemon.event(Event("key", "key0", False, None, b""))
    assert daemon.screen_description()["blanked"] is False
    assert showing(deck) == before, "the deck comes back as it was, not as an empty deck"


def test_a_menu_comes_back_with_its_highlight(tmp_path):
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake"); timers = []; clock = Clock()
    config(path, MENU)
    daemon = deck_daemon(path, deck, timers, clock)
    assert daemon.reload() and daemon.connect()
    fresh = showing(deck)
    daemon.event(Event("knob_turn", "top", None, 1, b""))
    turned = showing(deck)
    assert turned != fresh, "a turn moves the menu's highlight"
    clock.tick(30); timers[0].fire()
    assert daemon.screen_description()["blanked"] is True
    # A button with no binding changes nothing, so whatever the deck shows next it
    # can only have got there by coming back.
    daemon.event(Event("button", "button6", True, None, b""))
    assert daemon.screen_description()["blanked"] is False
    back = showing(deck)
    for key, face in turned.items():
        assert back.get(key) == face, f"key {key} did not come back as it was"
    assert any(back.get(key) != fresh.get(key) for key in turned), "and not reset to the top of the menu"


def test_a_key_still_held_keeps_the_keys_lit(tmp_path):
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake"); timers = []; clock = Clock()
    config(path, '[screen]\nblank_after_s = 30\n'
                 '[bindings.button6]\naction = "keys_hold"\nkeys_hold = ["ctrl", "shift", "space"]\n')
    daemon = deck_daemon(path, deck, timers, clock)
    assert daemon.reload() and daemon.connect()
    daemon.event(Event("button", "button6", True, None, b""))
    assert daemon.held, "the press is being held"
    clock.tick(300); deck.calls.clear()
    timers[-1].fire()
    assert [call for call in deck.calls if call[0] == "clear"] == [], "nothing goes dark under a finger"
    assert daemon.screen_description()["blanked"] is False


def test_zero_never_blanks_the_keys(tmp_path):
    path = tmp_path / "config.toml"; deck = Deck("/dev/fake"); timers = []; clock = Clock()
    config(path, '[screen]\nblank_after_s = 0\n[labels]\nkey0 = "Hello"\n')
    daemon = deck_daemon(path, deck, timers, clock)
    assert daemon.reload() and daemon.connect()
    assert timers == [], "with the setting off there is no countdown at all"
    clock.tick(86_400); deck.calls.clear()
    daemon.event(Event("key", "key0", True, None, b""))
    assert [call for call in deck.calls if call[0] == "clear"] == []
    assert daemon.screen_description()["blanked"] is False


def test_the_default_is_a_quarter_of_an_hour_and_the_setting_says_so(tmp_path):
    plain = tmp_path / "plain.toml"; config(plain, '[bindings.button6]\naction = "keys"\nkeys = ["enter"]\n')
    assert load_config(plain).screen.blank_after_s == 900
    chosen = tmp_path / "chosen.toml"; config(chosen, "[screen]\nblank_after_s = 60\n")
    assert load_config(chosen).screen.blank_after_s == 60


@pytest.mark.parametrize("value", ["-5", "86401", "true", '"900"', "12.5"])
def test_a_countdown_that_makes_no_sense_is_refused(tmp_path, value):
    path = tmp_path / "config.toml"; config(path, f"[screen]\nblank_after_s = {value}\n")
    with pytest.raises(ConfigError, match="blank_after_s"):
        load_config(path)


# ── the editor ──────────────────────────────────────────────────────────────────

def test_the_editor_reads_and_writes_the_setting_it_shows(tmp_path):
    """What the editor shows, the editor can save - and only that line of the file."""
    from akp03.webui import Draft, draft_payload, merge_draft_into_config

    def draft(seconds):
        return Draft(actions=(), bindings=(None,) * 6, menus=(), icons=None,
                     buttons=(None,) * 3, held=(), dials=(None,) * 8, blank_after_s=seconds)

    assert draft_payload(draft(300))["screen"] == 300

    original = ('[daemon]\ndevice = "/dev/hidraw0"\npoll_seconds = 1\n\n'
                '# A comment somebody wrote by hand.\n[feedback]\nenabled = false\n')
    once = merge_draft_into_config(original, draft(300))
    twice = merge_draft_into_config(once, draft(300))
    assert once == twice, "saving the same draft twice writes the same bytes"
    assert "[screen]" in once and "blank_after_s = 300" in once
    # Everything the editor does not own comes through untouched.
    assert '[daemon]\ndevice = "/dev/hidraw0"\npoll_seconds = 1\n' in once
    assert "# A comment somebody wrote by hand." in once
    assert "[feedback]\nenabled = false" in once
    # And the file it wrote loads with the setting it meant.
    path = tmp_path / "config.toml"; config(path, once)
    assert load_config(path).screen.blank_after_s == 300
    # Off is a real answer, not "unset": it survives the round trip as a zero.
    path2 = tmp_path / "off.toml"; config(path2, merge_draft_into_config(original, draft(0)))
    assert load_config(path2).screen.blank_after_s == 0


ASSETS = Path(__file__).resolve().parent.parent / "akp03" / "webui_assets"


def test_the_setting_is_reached_from_the_rail_and_not_from_the_deck_page():
    """It belongs to the deck, so it lives where the deck's own state is reported."""
    index = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert 'id="settingsEntry"' in index and 'id="settingsPanel"' in index
    assert 'role="dialog" aria-label="Settings" hidden' in index, "closed until it is asked for"
    # The entry sits above the deck's state, and the setting sits inside the panel.
    assert index.index('id="settingsEntry"') < index.index('id="deckLive"')
    panel = index.split('id="settingsPanel"')[1].split("</section>")[0]
    assert 'id="screenBlankAfter"' in panel, "the countdown is set in the panel"
    # Nothing of it is left on the deck page.
    assert "deck-screen" not in index
    assert index.count('id="screenBlankAfter"') == 1

    script = (ASSETS / "app.js").read_text(encoding="utf-8")
    # Escape closes it, a click elsewhere closes it, and focus goes back to the entry.
    assert "function closeSettings()" in script and "function openSettings()" in script
    assert "event.key === 'Escape'" in script and "closeSettings();" in script
    assert "entry.focus();" in script
    # Going to another page puts it away, and the boot wires it up.
    assert script.index("function showView(name)") < script.index("closeSettings();\n  document.querySelectorAll('.nav')")
    assert "wireSettings();" in script

    css = support.stylesheets(ASSETS)
    # A panel with its own display rule would beat the browser's [hidden] attribute.
    assert ".settings-panel[hidden] { display: none; }" in css
    # And a strip of a rail has no room above it, so the panel comes up from the bottom.
    narrow = css.split("@media (max-width: 900px) {")[1]
    assert ".settings-panel { position: fixed; left: 12px; right: 12px; bottom: 12px;" in narrow


def test_the_editor_shows_the_countdown_the_file_actually_has(tmp_path):
    """A value set by hand is what the editor shows, so a save cannot reset it."""
    from akp03.webui import draft_from_config

    path = tmp_path / "config.toml"
    config(path, '[screen]\nblank_after_s = 120\n\n[bindings.button6]\naction = "keys"\nkeys = ["enter"]\n')
    assert draft_from_config(path).blank_after_s == 120


def test_a_hand_written_screen_table_is_refused_if_it_makes_no_sense(tmp_path):
    path = tmp_path / "config.toml"; config(path, "[screen]\nblank_after_s = \"soon\"\n")
    with pytest.raises(ConfigError, match="blank_after_s"):
        load_config(path)
