# AKP03 native macOS control-service architecture

## Purpose and process boundaries

The production direction is a signed, packaged macOS container app. Its menu-bar UI and a native Swift local control service live inside that app boundary. The service can be given an IOKit adapter that captures deck input and paints the deck's LCD keys; the browser remains only an editor for configuration and presentation.

```text
Future signed/container app
├── Menu-bar UI (native Swift)
├── AKP03ControlService (native Swift)
│   └── AKP03IOKitInputSource (IOKit adapter: capture, plus LCD painting)
└── Strict loopback-only editor API
    └── Browser editor on this Mac
```

The browser must reach the service only over a validated loopback address. There is no third-party web integration, remote origin, LAN binding, cloud relay, or externally reachable API in this architecture.

## Native input boundary

`AKP03ControlServiceCore` is platform-independent and defines the testable semantic boundary:

- `AKP03HIDInputSource` is a capture protocol, not an output one: it accepts a semantic callback and has only start/stop lifecycle methods. The concrete adapter adds one separate output method (`sendPaintOutputReport`), so a consumer that only wants input cannot reach the write path through this protocol.
- `AKP03ControlEvent` carries a typed known control or opaque `unknownPacket`; raw bytes, IDs, lengths, and vendor data cannot cross this boundary. A known control carries its documented press/release state in the same event (a semantic `Bool`), so a consumer never correlates a second channel; a control with no press/release semantics (a knob turn) carries `nil`.
- `AKP03ControlReportDecoder` is the sole semantic mapping from established Linux ACK reports to typed controls. It does the whole job in one pass: it validates the ACK framing, maps the control code, and reads the documented state byte (nonzero = press, zero = release), returning control and state together. The six delta-carrying knob turns are reported stateless, matching the established Linux driver's `pressed = None`.
- `AKP03ControlService.startReceiving(from:)` explicitly owns one supplied source, forwards its sanitized events, and stops it exactly once. It never constructs an adapter itself.
- `AKP03ControlService --check` remains finite and does not enumerate, open, or activate HID hardware.

`AKP03IOKitInput` is the native macOS library target. `AKP03IOKitInputSource` implements the capture protocol (and adds one separate painting method, `sendPaintOutputReport`), selects exactly one physical VID `0x0300`/PID `0x3002`/usage-page `0xffa0` candidate, and retains its manager/device/runloop while active. **Verified on hardware:** macOS splits this device's single 512-byte input report into two elements (`0xffa1/0x3` = byte 0, `0xffa1/0x4` = bytes 1-511), so no single element can ever carry a decodable `ACK` frame and a value-callback adapter returns `unknownPacket` for every real press. The adapter therefore registers `IOHIDDeviceRegisterInputReportCallback` against an instance-owned buffer and decodes the complete report, mirroring Linux hidraw. Before registration it requires a logical input element on the vendor `0xffa1` element page (confirmed usages `0x3`, 8 bits, and `0x4`, 4088 bits), refusing keyboard, output, feature, or missing elements. It also requires Input Monitoring permission and fails with a named, actionable error when it is absent, because macOS otherwise opens the device successfully and then delivers nothing at all, with no error anywhere. `stopReceiving()` unschedules and closes the retained IOKit resources and frees the report buffer.

The first intentionally allowed device writes are controlled by `bootstrapEstablishedOpenProtocol`, which defaults false. After exact selection and a successful device open, it uses the single audited `AKP03IOKitBootstrap` output sink to emit exactly four report-ID-`0` packets, each zero-padded to 1024 bytes, in Linux `Deck.open()` order: `CRT\0\0DIS`; `CRT\0\0LIG\0\0` plus brightness byte `50`; `CRT\0\0CLE\0\0\0\xff`; then `CRT\0\0STP`. Only the bounded diagnostic explicitly enables it; `AKP03ControlService --check` does not. That diagnostic also schedules the fixed, zero-padded 1024-byte `CRT\0\0CONNECT` keepalive after 10 seconds and every 10 seconds, invalidating the timer on stop, error, or deinitialization. This deliberate visible change sets brightness to 50 and clears existing LCD content before flush; it does not modify bindings, settings, or artwork beyond that one-time clear, or send arbitrary content. It is an exact port of known Linux lifecycle behavior, not a protocol guess. A reviewed physical verification remains required.

The adapter exists but is not yet opened by the future packaged service. Physical input delivery and mapping are now **verified**: the bounded diagnostic decoded 30 of 30 events with zero unrecognized packets, covering key0-5, button6-8, and all three dials in both directions and pressed.

`AKP03HIDStream` is the long-running helper the Python daemon spawns to own the deck on macOS. It reuses the same verified adapter and decoder and exposes input as the frozen newline-delimited JSON protocol. Because the deck delivers nothing until it is woken, the helper sends the fixed open-protocol packets on each successful acquisition, selected by `--open-protocol`:

- `wake` (**default**): the single fixed wake packet `CRT\0\0DIS` only. It is proven sufficient for input and deliberately does not clear the LCDs. Painting is a separate, variable-data path (`paint` commands on the helper's own stdin), so nothing about the default mode changes when the daemon repaints.
- `full`: the four fixed Linux `Deck.open()` packets in order (`DIS`, `LIG`+50, `CLE`+0xff, `STP`), including the one-time clear — retained for parity with Linux.
- `none`: send nothing at all (diagnostics only).

In `wake` and `full` the fixed `CRT\0\0CONNECT` keepalive is also sent 10 seconds after open and every 10 seconds while the session is active (timer invalidated on stop, device loss, error, and deinitialization). All of this goes through the same single audited `AKP03IOKitBootstrap` sink: exactly five fixed, compile-time report-ID-`0` packets, each zero-padded to 1024 bytes. A failed write is logged to stderr and does not kill the process. The Python side defaults the mode to `wake` and honours `AKP03_HIDSTREAM_OPEN_PROTOCOL`.

### Painting: the second and last kind of output

The deck's own LCD keys are the point of the device, so macOS must be able to write screen content, not just read input. That is the only place a caller's bytes reach the device, and it is bounded on every side:

- `AKP03HIDStreamRequest` is the sole parser of a `paint` command, and it reads from the helper's **own stdin** — the pipe held by the daemon that spawned it. No command-line argument, socket, file, or network input can carry a payload.
- The payload must decode to **exactly 1024 bytes** (one report-ID-`0` output report) or it is refused; wrong length, non-base64, unknown command, missing field, malformed JSON, and an over-long line each produce their own short reason. The stdin buffer is bounded, so malformed bursts are discarded rather than accumulated.
- `AKP03IOKitBootstrap.paint` length-checks again before handing the bytes to the same private `sendOutputReport(to:packet:)` the fixed packets use. There is still exactly one `IOHIDDeviceSetReport` call site, still report ID `0`, still an **output** report: no feature report, no `IOHIDDeviceSetValue`, no other report kind or length.
- Every command is answered with exactly one result line (`result: ok` with the byte count, or `result: error` with the reason), so a refusal or a failed write is reported to the daemon instead of being dropped. A closed stdin stops the reader and leaves input streaming.

The packets themselves are not invented on the macOS side: the daemon builds them with the unchanged, tested `akp03/protocol.py` builders (`image_header`, `jpeg_chunks`, `flush`, `brightness`, `clear_key`/`clear_all`) and sends them in the same order the Linux hidraw path uses, so an image upload is byte-identical on both platforms. Painting does **not** depend on the destructive `full` bootstrap, which is why the default `wake` mode is unchanged.

Host link stability must be validated per machine and is not a property of this design. On this Mac mini M2 the deck dropped off the USB bus every 2.0 seconds when on the ASMedia xHCI root port, and the macOS HID stack then tore the device down; interposing a USB 2.0 hub stopped it entirely. That is a hardware-path quirk of this development machine, not a requirement of the adapter: the M4 deployment target connects over USB-C through a different controller and must be re-checked with the stability diagnostic rather than assumed to need the same hub.

## macOS key injection

The daemon's action vocabulary is platform-neutral configuration; only the executor differs. On macOS, `AKP03KeyInject` is the long-running helper the Python daemon spawns (via `akp03.backend.MacKeyBackend`, selected by `default_keyboard_backend`) to turn a validated `keys` chord into a tap and a `keys_hold` chord into a press-and-hold that spans deck events. Its pure logic — the fixed keycode table, chord/command parsing, the hold/release ledger, and the result-line protocol — lives in the device-free `AKP03KeyInjectCore` library so the check suite can verify it without a deck or a keystroke.

The capability is the narrowest that satisfies the action model. `AKP03KeyInject` creates only keyboard events (`CGEvent(keyboardEventSource:virtualKey:keyDown:)`) and posts them to `.cghidEventTap`. It creates no mouse, click, or scroll events, installs no event tap, monitors no input, runs no shell, and injects no text. The only keycodes it can emit are the fixed table of 67 canonical tokens (the same vocabulary the Linux `wtype` backend accepts, with `cmd` mapped to the macOS Command key). Any token outside that table is rejected rather than guessed, so a bad binding fails closed. A source-level audit in `Checks/main.swift` fails the build's checks if a mouse, scroll, tap, monitoring, shell, or text-injection API appears in the executable or its core.

macOS requires the Accessibility (TCC) grant for synthetic keyboard events. The helper answers `ping` with its honest status via `AXIsProcessTrustedWithOptions`; without the grant it posts nothing and every action fails closed. The Python side logs one clear, actionable error naming the permission (not a cryptic failure per press), and nothing crashes when the grant is absent.

The stuck-key defence is structural: at most one chord is held at a time, a duplicate `hold` and an unmatched `release` are refused, and `quit`, stdin EOF, SIGINT, SIGTERM, and any error all release every held key before the process exits. The daemon stops the helper cleanly (its own `quit`, then EOF, then SIGTERM/SIGKILL) after `close_deck()` has released any held chord through it.

Automated checks exercise only `--dry-run`, which parses, validates, and prints what it *would* post while posting nothing. Real injection and the Accessibility grant remain unverified by any automated check and require a deliberate physical test.

## Bounded diagnostic

`AKP03HIDReadBridge` exercises that same adapter only for an explicitly approved diagnostic and is the only path that enables the fixed bootstrap. It requires `--seconds 1...30`, rejects zero, indefinite, malformed, missing-identity, and multi-device cases, and emits only known semantic control names or an opaque unrecognized-packet notice. It never prints raw report bytes.

A completed interval means only that the bounded diagnostic ran; it is not proof that physical inputs were received or that macOS delivery is verified. Verification must not run this executable without a separately approved physical test.

## Ownership of the daemon (development mode)

The menu-bar app is no longer a passive companion: it starts the daemon as a child process, keeps it alive, and stops it on Quit.

```text
AKP03MenuBar (menu-bar app)
├── starts & supervises → python -m akp03.daemon  (child process)
│     ├── spawns AKP03HIDStream  (deck input; LCD painting on its stdin)
│     └── spawns AKP03KeyInject  (keyboard actions)
├── reads  GET /api/state   (editor reachable?)
└── reads  GET /api/events  (live deck state, SSE)
```

- **Supervision.** An unexpected child exit is restarted under the bounded backoff `0.5, 1, 2, 4, 8, 15, 30` seconds (the same shape `akp03.hidstream` uses for its helper), with a cap of five *rapid* restarts; a run that stayed up resets the escalation. A daemon that crashes on startup therefore stops after five attempts instead of spinning. On **Quit** the app sends `SIGTERM`, waits, and escalates to `SIGKILL` only if the daemon will not stop. The daemon reaps its two helpers itself, so a clean stop leaves no orphan and no held key.
- **Status is two facts, not one.** Service reachability comes from the read-only health probe; the deck's own state (`starting|waiting|ready|lost`) comes from the daemon's `state` frames on `/api/events` and is never inferred from the probe.
- **Adoption.** If a daemon is already running when the app starts, the app adopts it instead of starting a competing second daemon, and does not stop what it did not start.
- **Permissions.** Because the app is now the parent of the daemon and its helpers, it is the process macOS asks about. It *reads* Input Monitoring (`IOHIDCheckAccess`) and Accessibility (`AXIsProcessTrusted`) and surfaces one actionable line; it never weakens or bypasses TCC. Asking for the Accessibility grant goes through the existing `AKP03KeyInject --request-accessibility` flag.
- **Development mode.** The daemon is Python and is *not* bundled. The app launches the repository's own `.venv/bin/python -m akp03.daemon`, discovered from `--repo-root` or by walking up for `.venv/bin/python`, with overrides validated before use. The launch is always a fixed argv array handed to `Process`; the app never builds a shell string. Bundling a Python runtime so the app is self-contained is a later packaging step.

## Explicit non-goals

Device output is limited to exactly three kinds, all through the single audited `AKP03IOKitBootstrap` sink, all report ID `0`, all exactly 1024 bytes, and all **output** reports: the diagnostic's optional four-packet Linux bootstrap, the stream helper's `wake`/`full` open-protocol modes and fixed keepalive, and the daemon's `paint` packets (screen content) read from the helper's own stdin. Beyond those there is no device output of any kind: no feature reports, no device-value changes, no LED commands, no generic write API, and no caller-supplied bytes from anywhere but that one stdin pipe. In the default `wake` mode even the LCD clear is deliberately omitted. Normal startup and automated verification perform no physical device enumeration or capture.

DriverKit is not part of this design. `SMAppService` is deferred until packaging, code signing, a reviewed lifecycle, and user-facing installation behavior exist. Bundling a Python runtime into the app (so the daemon needs no developer checkout) is likewise deferred to packaging; until then the app runs the repository's virtual environment and says so. The existing Python WebUI safety and configuration behavior remains unchanged.
