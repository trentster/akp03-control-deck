# AKP03 macOS menu-bar app

A menu-bar-only macOS app that **owns the AKP03 deck**: it starts, supervises and stops the daemon, shows the deck's real connection state, and opens the local HTML editor in the browser for configuration. The browser editor remains the only configuration plane.

## What the app does now

- **Starts the daemon as a child process** on launch and keeps it alive: an unexpected exit is restarted under a bounded backoff (`0.5, 1, 2, 4, 8, 15, 30` seconds) with a cap of **five rapid restarts**, so a daemon that crashes on startup is never spun.
- **Stops the daemon on Quit** — `SIGTERM`, then a bounded wait, escalating to `SIGKILL` only if it will not stop. The daemon reaps its own Swift helpers, so nothing is left running and no key is left held down.
- **Reports the deck itself**, not just the editor: the menu shows whether the daemon/editor is responding *and* what the deck is doing (connected and working / waiting for the deck / connection lost / starting), taken live from the daemon's `/api/events` feed rather than inferred from the health probe.
- **Reports missing permissions honestly**: Input Monitoring (deck input) and Accessibility (typing shortcuts) are named in one actionable line with the exact System Settings pane, and **Grant Accessibility…** asks macOS to offer the grant through the existing `AKP03KeyInject --request-accessibility` flag.

If a daemon is already running when the app starts (started outside the app), the app **adopts** it — it does not start a competing second daemon, and Quit then leaves the externally-started service alone.

## Development mode (important)

The daemon is Python and is **not bundled into the app yet**. The app runs the repository's own virtual environment:

```text
/…/akp03-control-deck/.venv/bin/python -m akp03.daemon --editor-port 17865
```

Bundling a Python runtime (and a signed, self-contained app) is a later packaging step. The launch command is configurable and validated before use — see **Launch command** below. The app never builds a shell string: it passes a fixed argv array straight to `Process`.

## Boundary

This package deliberately does **not**:

- install itself, register a Login Item, or create/modify a `LaunchAgent`;
- send HID output, feature reports, or device-value changes, except for the fixed, audited open-protocol packets described below (the diagnostic's four-packet Linux bootstrap, and the stream helper's `wake`/`full` modes);
- send API writes. Its only network requests are a local `GET` health probe and the local `GET /api/events` status stream;
- write any configuration — the editor and the daemon own that.


## Native IOKit input adapter and diagnostic

`AKP03IOKitInput` is a dedicated macOS library target. Its `AKP03IOKitInputSource` implements the input-only `AKP03HIDInputSource` protocol for capture and adds exactly one output method, `sendPaintOutputReport(_:)`, which writes one caller-supplied 1024-byte painting payload as a report-ID-0 output report through the same single audited sink the fixed lifecycle packets use. It passes only `.known` or `.unknownPacket` semantic events through `AKP03ControlReportDecoder`, which reads the control and its press/release state in the same decode. The adapter submits the complete report with `IOHIDDeviceRegisterInputReportCallback` and decodes it whole, mirroring Linux hidraw—it does not use a per-element value callback. It selects exactly one VID `0x0300`, PID `0x3002`, vendor-usage-page `0xffa0` physical candidate and refuses capture unless that selected vendor collection contains a logical input element on vendor page `0xffa1`. The confirmed input elements are usages `0x3` (8 bits) and `0x4` (4088 bits); keyboard, output, and feature elements are not selected. It retains its IOKit manager/device/runloop while active and closes/unschedules them on stop.

The first intentionally allowed device writes are opt-in only: after selecting and opening that exact vendor collection, `bootstrapEstablishedOpenProtocol: true` runs the fixed four-report Linux `Deck.open()` bootstrap via one audited output sink, all with report ID `0` and padded to exactly 1024 bytes: `CRT\0\0DIS`; `CRT\0\0LIG\0\0` followed by brightness byte `50`; `CRT\0\0CLE\0\0\0\xff`; then `CRT\0\0STP`. Only the bounded diagnostic explicitly opts in; production service checks do not. It then sends the fixed, zero-padded 1024-byte `CRT\0\0CONNECT` keepalive after 10 seconds and every 10 seconds only while that diagnostic remains active. Stop, error, and deinitialization invalidate that timer. This deliberately and visibly sets brightness to 50 and clears existing LCD content before flushing; it does **not** modify bindings, settings, or artwork beyond that one-time clear, or send arbitrary content. This is an exact port of known Linux lifecycle behavior, not a protocol guess, and still requires one reviewed physical verification.

The future native service must explicitly receive an adapter through `AKP03ControlService.startReceiving(from:)`; `AKP03ControlService --check` never activates HID. The adapter exists, but the packaged service does not open it yet and physical macOS input delivery remains unverified.

`AKP03HIDReadBridge` is the only executable path that exercises the adapter and explicitly enables the fixed bootstrap. It is a separately approved, bounded diagnostic—not a daemon—and is never run by verification. It rejects anything except `--seconds 1...30`, refuses missing or multiple physical candidates, and prints semantic known-control names or an opaque unrecognized-packet notice only. It never prints raw report bytes.

Run it only for an explicitly approved physical test:

```sh
swift run AKP03HIDReadBridge --seconds 20
```

A completed callback interval is **not** evidence that physical inputs were received or that macOS decoding is verified.

## Local input stream for the daemon

`AKP03HIDStream` is a long-running helper that exposes deck input to a local Python daemon as newline-delimited JSON on stdout, and takes the daemon's screen updates on its own stdin. It reuses the verified `AKP03IOKitInputSource` and report decoder. It takes no required arguments and accepts `--poll-seconds N` (default 2, clamped to 1...60) and `--open-protocol MODE` (default `wake`). On each successful device acquisition it sends fixed, audited open-protocol packets, and thereafter only the `paint` payloads its daemon parent writes to stdin — never any HID feature report, device-value write, or report ID other than `0`:

- `wake` (**default**): send only the single fixed wake packet `CRT\0\0DIS`. This is proven sufficient on hardware for the deck to deliver input, and it deliberately does **not** clear the LCDs — the daemon repaints them with `paint` commands instead.
- `full`: send the four fixed Linux `Deck.open()` packets in order — `CRT\0\0DIS`; `CRT\0\0LIG\0\0`+brightness `50`; `CRT\0\0CLE\0\0\0\xff`; `CRT\0\0STP` — including a one-time LCD clear. Kept for parity with Linux; painting does not need it.
- `none`: send nothing at all (diagnostic only).

In `wake` and `full` it also sends the fixed `CRT\0\0CONNECT` keepalive 10 seconds after open and every 10 seconds while that device session is active; the timer is invalidated on stop, device loss, error, and deinitialization. It runs until SIGINT/SIGTERM, then exits 0 after releasing the device cleanly. It survives unplug/replug by re-acquiring with a fresh `IOHIDManager` each attempt, and it never prints raw report bytes.

The mode is chosen on the Python side (default `wake`) and can be overridden with the `AKP03_HIDSTREAM_OPEN_PROTOCOL` environment variable.

### Screen updates: the `paint` command

macOS has no `/dev/hidraw`, so the daemon cannot write the deck itself. While the helper holds the device open it also reads commands from **its own stdin** (the pipe its daemon parent owns) and answers each one with exactly one result line:

```text
{"command":"paint","payload":"<base64 of exactly 1024 bytes>"}
{"result":"ok","command":"paint","bytes":1024}
{"result":"error","command":"paint","reason":"paint payload must be exactly 1024 bytes (received 5)"}
```

One command carries exactly one protocol packet — the same packet `akp03/protocol.py` builds for Linux (an image header, a JPEG chunk, a flush, a brightness level, a key clear). The daemon sends them in the Linux order, so what reaches the panel is byte-identical to the Linux write path. The boundary is deliberately narrow and is checked in `Checks` and the Python tests:

- the payload is base64 of **exactly 1024 bytes**, refused before any write otherwise (wrong length, non-base64, unknown command, missing field, malformed JSON, and over-long lines each get their own refusal reason);
- it is written as a report-ID-0 **output** report through the one audited `IOHIDDeviceSetReport` call — no feature report, no `IOHIDDeviceSetValue`, no second call site, no third report kind;
- the bytes can only come from this process's own stdin: no command-line argument carries a payload, and nothing is accepted over the network or from a file;
- the stdin buffer is bounded (`AKP03HIDStreamRequest.maximumLineCharacters`), so a burst of malformed input is discarded rather than buffered without limit;
- a paint before the device is open, and any failed or refused write, is answered with a `result: error` line instead of being dropped silently.

The line protocol for events and results taken together — exactly one JSON object per line, and nothing else is ever written to stdout (diagnostics go to stderr):

```text
{"event":"starting"}
{"event":"waiting"}                                     no matching device present / no active capture
{"event":"ready"}                                       device open and capture running
{"event":"control","control":"key0","state":"press"}    a decoded known control
{"event":"control","control":"key0","state":"release"}
{"event":"control","control":"topClockwise"}             a knob turn: no press/release, so state is omitted
{"event":"device-lost"}                                 the device disappeared
{"event":"unknown"}                                     an unrecognized report (bytes are never shown)
{"event":"error","message":"..."}                       a short, recoverable problem
{"result":"ok","command":"paint","bytes":1024}          one answer per paint command written
{"result":"error","command":"paint","reason":"..."}     a refused or failed paint; the device was not changed
```

```sh
swift run AKP03HIDStream                    # default wake mode
swift run AKP03HIDStream --open-protocol full
```

## Local key injection for the daemon

`AKP03KeyInject` is a long-running helper that turns the daemon's bound `keys`
(tap) and `keys_hold` (press-and-hold) actions into real macOS keystrokes. It
reads one command per line on stdin and writes one compact JSON result line per
command on stdout; diagnostics go to stderr. The command language is deliberately
tiny:

```text
ping                     {"result":"pong","accessibility":true|false,"dry_run":false}
quit                     release everything held and exit 0
tap   cmd+z              press and release a chord once
hold  ctrl+shift+space    press and keep the chord held until released
release ctrl+shift+space   release everything held
```

A chord is `+`-joined canonical tokens and accepts exactly the configuration
model's vocabulary: `ctrl`, `alt`, `shift`, `cmd`; the named keys `space`, `tab`,
`enter`, `escape`, `backspace`, `delete`, `insert`, `home`, `end`, `pageup`,
`pagedown`, `left`, `right`, `up`, `down`; letters `a`–`z`; digits `0`–`9`; and
`f1`–`f12` (67 tokens). `cmd` is the Command key on macOS. Any other token is
rejected rather than guessed, so a bad binding fails closed instead of injecting a
wrong keystroke.

It posts keyboard events only — `CGEvent(keyboardEventSource:virtualKey:keyDown:)`
posted to `.cghidEventTap`. It creates no mouse, click, or scroll events, installs
no event tap, monitors no input, runs no shell, and injects no arbitrary text. The
only keycodes it can emit are the fixed table above.

**Accessibility (TCC).** macOS requires the Accessibility grant for synthetic
keyboard events. The helper reports its status honestly on `ping`; without the
grant it posts nothing and every action fails closed. The Python side
(`akp03/backend.py`, `MacKeyBackend`) then logs one clear, actionable error naming
the permission — grant the helper in **System Settings → Privacy & Security →
Accessibility** — instead of a cryptic failure on every press.

**Safety.** At most one chord is held at a time; a duplicate `hold` and an
unmatched `release` are both refused. `quit`, stdin EOF, SIGINT, SIGTERM, and any
error all release every held key before the process exits, so a key is never left
logically down. The daemon stops the helper cleanly on shutdown.

**`--dry-run`** parses and validates commands, prints exactly what it *would*
post, and posts nothing. All automated verification uses it:

```sh
printf '%s\n' 'tap cmd+z' 'hold ctrl+shift+space' 'release ctrl+shift+space' \
  | swift run AKP03KeyInject --dry-run
```

The daemon finds the helper on `PATH` as `AKP03KeyInject`, or via the
`AKP03_KEYINJECT` environment variable; point that at the built binary (for
example `AKP03_KEYINJECT=$PWD/.build/debug/AKP03KeyInject`) for a local trial.
If the helper is absent the daemon fails closed with a named reason and injects
nothing.

Real injection — and the Accessibility grant — is **not** verified by any
automated check; it requires a deliberate physical test by the operator.

## Requirements

- macOS 13 or later
- Xcode Command Line Tools with Swift 6 (validated with Apple Swift 6.2.3 on arm64)

## Build, test, and run

From this directory:

```sh
./Scripts/verify.sh
swift run AKP03MenuBar
```

`./Scripts/verify.sh` builds the package and runs device-free tests/checks for endpoint parsing, the daemon launch-command validation, the supervision/backoff policy, deck-status wording for every state, the SSE frame parser, permissions wording, diagnostic argument limits, the stream's `--poll-seconds`/`--open-protocol` parsing, IOKit candidate selection, fixed bootstrap/wake/keepalive packet/order/allowlist, report decoding, input-source lifecycle, and key injection (keycode table accept/reject, chord and command parsing, hold/release bookkeeping, dry-run result lines, and a source audit that the injector exposes no mouse/shell/monitoring capability). It ends with a real `AKP03KeyInject --dry-run` smoke test that parses a tap, a hold/release pair, and an invalid key while posting nothing. It does not enumerate, open, or write HID hardware, and it never injects a keystroke.

`swift run AKP03MenuBar` launches the menu-bar app (no Dock icon), which starts and supervises the daemon. Use **Quit AKP03 (stops the deck)** when finished. Nothing is installed or configured for autostart.

Two extra modes of the checks executable drive real processes for verification (they are **not** part of `verify.sh`, which stays device-free and fast):

```sh
swift run AKP03MenuBarChecks --live-supervision
# real stub daemons: proves start, backoff/restart, the rapid-crash cap, and a clean stop

swift run AKP03MenuBarChecks --stream http://127.0.0.1:17890/api/events --seconds 10
# the app's own SSE client: prints each live deck state it hears, or that the service is not running
```


> On a Command Line Tools-only install, XCTest is unavailable, so the XCTest
> target compiles to nothing and `swift test` discovers no cases; the device-free
> checks that actually run are the `AKP03MenuBarChecks` assertions invoked by
> `swift run AKP03MenuBarChecks`.

## Local URL, health check, and live deck status

The default editor URL is exactly:

```text
http://127.0.0.1:17865/
```

Two read-only loopback requests back the menu. The health probe is a `GET`:

```text
http://127.0.0.1:17865/api/state
```

and the live deck status is the daemon's Server-Sent Events feed:

```text
http://127.0.0.1:17865/api/events
```

The health probe runs every 15 seconds, when the menu is presented, and when **Refresh status** is selected. A 2xx response means **the editor is reachable**. The deck's own state is *not* inferred from that probe: it comes from the `state` frames (`{connected, phase}`, phase `starting|waiting|ready|lost`) the daemon streams on `/api/events`, so it updates as it changes rather than every 15 seconds. **Configure AKP03** is enabled only when the editor is reachable; **Open AKP03 anyway** is the fallback that opens the configured URL without claiming it is ready.

For a different local WebUI base URL, supply `--url`:

```sh
swift run AKP03MenuBar --url http://127.0.0.1:9876/
```

Only plain `http://` loopback URLs are accepted (`127.0.0.1`, `localhost`, or `::1`), with no credentials, query, or fragment. The health and events endpoints are derived by appending `api/state` and `api/events` to that base URL; supply a trailing slash for a base path, for example `http://localhost:9876/editor/` probes `http://localhost:9876/editor/api/state`. The daemon is started with a matching `--editor-port`, so the URL is the single source of truth for the port.

## Launch command

The command the app runs is a fixed argv array, validated before use and never assembled as a shell string. Defaults are discovered from the repository (`--repo-root`, or by walking up for `.venv/bin/python`):

| Flag | Meaning |
|---|---|
| `--repo-root PATH` | Base for the default executable, helpers, and working directory. |
| `--daemon-command PATH` | The exact program to run (default `<repo>/.venv/bin/python`). |
| `--daemon-arg VALUE` | Repeatable. **Replaces** the default daemon argument list, so a custom command receives exactly its argv. |
| `--config PATH` / `--socket PATH` | Forwarded to the daemon only when given. |
| `--editor-port N` | The daemon's editor port (defaults to the port in `--url`). |
| `--hidstream PATH` / `--keyinject PATH` | Helper paths exported as `AKP03_HIDSTREAM` / `AKP03_KEYINJECT`. |

`UserDefaults` keys `AKP03DaemonExecutable`, `AKP03DaemonArguments`, `AKP03RepoRoot`, `AKP03ConfigPath`, `AKP03SocketPath`, `AKP03EditorPort`, `AKP03HIDSTREAM`, and `AKP03KEYINJECT` provide the same overrides; command-line flags win. Absolute paths or bare command names are accepted; anything containing shell metacharacters, NUL, or a newline is rejected, and the app fails closed rather than launching something unexpected. The menu shows the resolved command for transparency.

## Accessibility and wording

The status item names the most important fact first: **Ready** (deck working), **No deck** (nothing plugged in), **Deck lost**, **Starting**, **Idle** (no status yet), **Offline** (editor unreachable), **Checking…**, or **Stopped** (supervision gave up).

The menu is, top to bottom:

- the editor line (`Editor: reachable at …` / `Editor: not reachable at …`);
- the deck line (`Deck: connected and working.` / `Deck: waiting for the deck — no AKP03 is plugged in.` / `Deck: connection lost — the deck was disconnected.` / `Deck: starting up…`);
- the service line (`Service: running under this app.` / `Service: running, started outside this app.` / `Service: not running.` / `Service: stopped after N failed start attempts.`);
- one permission line, only when a permission is missing;
- **Configure AKP03** (enabled only when the editor is reachable);
- **Restart the deck** (stops and restarts the supervised daemon);
- **Open AKP03 anyway** (the fallback);
- **Grant Accessibility…** and/or **Open Input Monitoring settings…** when those permissions are missing;
- **Refresh status**;
- the resolved launch command (caption);
- **Quit AKP03 (stops the deck)** — ⌘Q. When the app adopted a daemon that was started outside it, the item reads **Quit AKP03 (leaves the running service)** instead.

Every line carries a VoiceOver label, and the Quit item states plainly that it stops the deck. Permission lines name the exact pane, for example: *Input Monitoring permission is not granted. The deck's buttons will not reach the app — allow AKP03MenuBar in System Settings, Privacy and Security, Input Monitoring, then reopen the app.*

The TCC model is not weakened or bypassed: Input Monitoring (`IOHIDCheckAccess`) and Accessibility (`AXIsProcessTrusted`) are only *read*; the one action asks macOS to offer the Accessibility grant via the existing helper flag.

