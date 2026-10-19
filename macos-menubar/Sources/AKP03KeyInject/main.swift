import AKP03KeyInjectCore
import AppKit
import ApplicationServices
import CoreGraphics
import Darwin
import Dispatch
import Foundation

/// Local macOS keyboard-injection helper for the AKP03 daemon.
///
/// It reads one command per line on stdin and writes one compact JSON result
/// line per command on stdout (see ``AKP03KeyInjectLine``). Diagnostics go to
/// stderr only.
///
/// Capability is deliberately the narrowest that satisfies the action model. For
/// a chord it creates keyboard events (`CGEventCreateKeyboardEvent`); for one of
/// the six media keys it creates the system-defined event macOS actually listens
/// for, because a media key has no virtual keycode. It creates no mouse, click,
/// or scroll events, installs no event tap, monitors no input, runs no shell, and
/// injects no arbitrary text. The only keycodes it can emit are the fixed ones in
/// ``AKP03KeyTable``; the only media functions are the six in ``AKP03MediaKey``.
///
/// macOS requires the Accessibility (TCC) grant for synthetic keyboard events.
/// The process reports that status honestly and posts nothing while it is absent,
/// so the Python side can log one actionable error instead of failing per press.
@main
struct AKP03KeyInjectMain {
    static func main() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        switch arguments {
        case ["--help"], ["-h"]:
            print(AKP03KeyInjectLine.usage)
        case []:
            run(dryRun: false)
        case ["--dry-run"]:
            run(dryRun: true)
        case ["--request-accessibility"]:
            // Deliberate one-shot opt-in: have macOS itself offer the grant and add
            // this helper to the Accessibility list, instead of the user hunting
            // for the entry in System Settings. Posts nothing; exits immediately.
            // `kAXTrustedCheckOptionPrompt` is a mutable global, which Swift 6 strict
            // concurrency refuses to read; its documented value is this exact string.
            let options = ["AXTrustedCheckOptionPrompt": true] as CFDictionary
            let granted = AXIsProcessTrustedWithOptions(options)
            print("{\"result\":\"accessibility-request\",\"granted\":\(granted)}")
        default:
            writeStderr("AKP03KeyInject: unexpected arguments (only --dry-run, --request-accessibility or --help are accepted)\n")
            writeStderr(AKP03KeyInjectLine.usage + "\n")
            exit(EXIT_FAILURE)
        }
    }

    /// The injector outlives the whole process: `run()` only returns on shutdown,
    /// which exits. `withExtendedLifetime` keeps the temporary alive so the
    /// signal-source handler's `self` reference is never a dangling weak reference.
    private static func run(dryRun: Bool) {
        let injector = AKP03KeyInjector(dryRun: dryRun)
        injector.run()
        withExtendedLifetime(injector) {}
    }
}

/// Owns the held-key ledger, the event generation, and shutdown safety.
private final class AKP03KeyInjector: @unchecked Sendable {
    private let dryRun: Bool
    private let lock = NSLock()
    private var ledger = AKP03KeyLedger()
    private var signalSources: [DispatchSourceSignal] = []
    private var stopped = false

    init(dryRun: Bool) {
        self.dryRun = dryRun
    }

    func run() {
        installSignalHandlers()
        while !stopped, let line = readLine(strippingNewline: true) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty { continue }
            emit(handle(trimmed))
        }
        // EOF (the daemon closed our stdin) is a clean shutdown: never leave a key down.
        shutdown()
    }

    // MARK: - Command handling

    private func handle(_ line: String) -> String {
        lock.lock()
        defer { lock.unlock() }
        let command: AKP03KeyInjectCommand
        do {
            command = try AKP03KeyInjectCommand.parse(line)
        } catch {
            return AKP03KeyInjectLine.error(command: verb(of: line), reason: error.localizedDescription)
        }
        switch command {
        case .ping:
            return AKP03KeyInjectLine.pong(accessibility: accessibilityTrusted(), dryRun: dryRun)
        case let .tap(chord):
            return performTap(chord)
        case let .hold(chord):
            return performHold(chord)
        case let .release(chord):
            return performRelease(chord)
        case let .media(key):
            return performMedia(key)
        case .quit:
            releaseHeldUnderLock()
            stopped = true
            return AKP03KeyInjectLine.ok(command: "quit", chord: nil)
        }
    }

    private func performTap(_ chord: AKP03KeyChord) -> String {
        if dryRun {
            return AKP03KeyInjectLine.dryRun(
                command: "tap",
                chord: chord.rendered,
                wouldPost: chord.pressedDescription + chord.releasedDescription
            )
        }
        guard accessibilityTrusted() else {
            return AKP03KeyInjectLine.error(command: "tap", reason: AKP03KeyInjectLine.accessibilityReason)
        }
        post(steps: chord.pressSteps, down: true)
        Thread.sleep(forTimeInterval: 0.001)
        post(steps: chord.pressSteps.reversed(), down: false)
        return AKP03KeyInjectLine.ok(command: "tap", chord: chord.rendered)
    }

    private func performHold(_ chord: AKP03KeyChord) -> String {
        switch ledger.hold(chord) {
        case let .alreadyHeld(current):
            return AKP03KeyInjectLine.error(command: "hold", reason: "already holding \(current.rendered)")
        case let .held(held):
            if dryRun {
                return AKP03KeyInjectLine.dryRun(
                    command: "hold", chord: held.rendered, wouldPost: held.pressedDescription
                )
            }
            guard accessibilityTrusted() else {
                // Roll the ledger back so a later release stays unmatched rather
                // than pretending a hold that never reached the system.
                _ = ledger.releaseAll()
                return AKP03KeyInjectLine.error(command: "hold", reason: AKP03KeyInjectLine.accessibilityReason)
            }
            post(steps: held.pressSteps, down: true)
            return AKP03KeyInjectLine.ok(command: "hold", chord: held.rendered)
        }
    }

    /// One media key: a matched press and release, and nothing the ledger tracks.
    /// A media function has no "held" state, so there is no hold or release verb
    /// for it.
    private func performMedia(_ key: AKP03MediaKey) -> String {
        if dryRun {
            return AKP03KeyInjectLine.dryRun(
                command: "media", chord: key.rawValue, wouldPost: key.postDescription
            )
        }
        guard accessibilityTrusted() else {
            return AKP03KeyInjectLine.error(command: "media", reason: AKP03KeyInjectLine.accessibilityReason)
        }
        postMedia(key, down: true)
        Thread.sleep(forTimeInterval: 0.001)
        postMedia(key, down: false)
        return AKP03KeyInjectLine.ok(command: "media", chord: key.rawValue)
    }

    private func performRelease(_ requested: AKP03KeyChord) -> String {
        switch ledger.release() {
        case .nothingHeld:
            return AKP03KeyInjectLine.error(command: "release", reason: "not holding \(requested.rendered)")
        case let .released(held):
            if dryRun {
                return AKP03KeyInjectLine.dryRun(
                    command: "release", chord: held.rendered, wouldPost: held.releasedDescription
                )
            }
            post(steps: held.pressSteps.reversed(), down: false)
            return AKP03KeyInjectLine.ok(command: "release", chord: held.rendered)
        }
    }

    /// Release everything still held, used on quit / EOF / signal / error.
    private func releaseHeldUnderLock() {
        guard let held = ledger.releaseAll() else { return }
        if !dryRun, accessibilityTrusted() {
            post(steps: held.pressSteps.reversed(), down: false)
        }
    }

    // MARK: - Event generation

    /// Post one ordered run of key events. Modifier flags accumulate on press and
    /// unwind on release, so every event carries the flags a real keyboard would.
    ///
    /// Each event is followed by the small gap a real keyboard has. Posting is
    /// asynchronous: with no pause the arrow reaches the window server before the
    /// control key-down has been registered, and macOS delivers a plain arrow —
    /// seen as Up scrolling a Finder list instead of ⌃↑ opening Mission Control.
    private func post(steps: [AKP03KeyChord.Step], down: Bool) {
        var flags: CGEventFlags = []
        for step in steps {
            if step.isModifier, let flag = step.modifierFlag {
                if down { flags.insert(flag) } else { flags.remove(flag) }
            }
            postKey(step.keyCode, down: down, flags: flags)
            if step.isModifier { usleep(Self.modifierGapMicroseconds) }
        }
    }

    /// 12 ms: long enough for the modifier state to land, short enough to feel
    /// instant on a key press.
    private static let modifierGapMicroseconds: useconds_t = 12_000

    private func postKey(_ keyCode: CGKeyCode, down: Bool, flags: CGEventFlags) {
        // The source matters more than the keycode. An event created with a nil
        // source reaches ordinary application shortcuts — ⌘N opens a Finder
        // window — but the system hotkey engine ignores it, so Mission Control
        // and the other system shortcuts never fire. Tagging events with the HID
        // system state makes macOS deliver them as though a keyboard had sent
        // them, which is what a physical deck has to look like.
        let source = CGEventSource(stateID: .hidSystemState)
        guard let event = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: down) else { return }
        event.flags = flags
        event.post(tap: .cghidEventTap)
    }

    /// Post one half of a media key press as the system-defined event the window
    /// server reads: `NX_SYSDEFINED` (the same shape a real keyboard sends),
    /// subtype `NX_SUBTYPE_AUX_CONTROL_BUTTONS`, with the key code and the
    /// up/down flag packed into `data1` by ``AKP03MediaKey``.
    private func postMedia(_ key: AKP03MediaKey, down: Bool) {
        guard let event = NSEvent.otherEvent(
            with: .systemDefined,
            location: .zero,
            modifierFlags: [],
            timestamp: ProcessInfo.processInfo.systemUptime,
            windowNumber: 0,
            context: nil,
            subtype: 8,
            data1: key.eventData1(down: down),
            data2: -1
        )?.cgEvent else { return }
        event.post(tap: .cghidEventTap)
    }

    private func accessibilityTrusted() -> Bool {
        // Honest, cheap, and side-effect free: reports the current TCC decision.
        AXIsProcessTrustedWithOptions(nil)
    }

    // MARK: - Lifecycle safety

    private func shutdown() {
        lock.lock()
        releaseHeldUnderLock()
        stopped = true
        lock.unlock()
        exit(EXIT_SUCCESS)
    }

    private func installSignalHandlers() {
        // A closed stdout must end the process cleanly, not kill it mid-chord.
        signal(SIGPIPE, SIG_IGN)
        let queue = DispatchQueue(label: "akp03.keyinject.signals")
        for signum in [SIGINT, SIGTERM, SIGHUP] {
            signal(signum, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: signum, queue: queue)
            source.setEventHandler { [weak self] in self?.shutdown() }
            source.resume()
            signalSources.append(source)
        }
    }

    // MARK: - Output

    /// Writes exactly one protocol line to stdout. There is no other stdout output.
    private func emit(_ line: String) {
        guard let data = (line + "\n").data(using: .utf8) else { return }
        do {
            try FileHandle.standardOutput.write(contentsOf: data)
        } catch {
            shutdown()
        }
    }

    /// Best-effort verb extraction for an error line so it still names the command.
    private func verb(of line: String) -> String? {
        let field = line.split(separator: " ", omittingEmptySubsequences: true).first.map(String.init)
        return (field?.isEmpty ?? true) ? nil : field
    }
}

private func writeStderr(_ text: String) {
    FileHandle.standardError.write(Data(text.utf8))
}
