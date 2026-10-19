import AKP03ControlServiceCore
import AKP03HIDStreamCore
import AKP03IOKitInput
import CoreFoundation
import Darwin
import Dispatch
import Foundation
import IOKit

/// Long-running line-protocol helper for a local Python daemon.
///
/// It reuses the verified `AKP03IOKitInputSource` for capture and the verified
/// whole-report decode path. Device output is limited to the fixed, audited
/// open-protocol packets selected by `--open-protocol` (default `wake`), the
/// fixed `CRT\0\0CONNECT` keepalive, and — read from this process's OWN stdin,
/// nothing else — `paint` commands that carry exactly one 1024-byte report. Every
/// write goes through the single audited `AKP03IOKitBootstrap` sink as a
/// report-ID-0 output report; there are no feature reports, no device-value
/// changes, and no payload from a command-line argument, file, socket, or the
/// network. Stdout carries only the frozen JSON line protocol (`event` lines and
/// one `result` line per command); every diagnostic goes to stderr.
@main
struct AKP03HIDStreamMain {
    static func main() {
        do {
            switch try AKP03HIDStreamCommand.parse(arguments: Array(CommandLine.arguments.dropFirst())) {
            case .help:
                print(AKP03HIDStreamLine.usage)
            case let .run(pollSeconds, openProtocol):
                let runner = AKP03HIDStreamRunner(pollSeconds: pollSeconds, openProtocol: openProtocol)
                runner.run()
            }
        } catch {
            writeStderr("AKP03HIDStream: \(error.localizedDescription)\n")
            writeStderr(AKP03HIDStreamLine.usage + "\n")
            exit(EXIT_FAILURE)
        }
    }
}

private final class AKP03HIDStreamRunner: @unchecked Sendable {
    private let pollSeconds: Int
    private let openProtocol: AKP03OpenProtocolMode
    /// Mutated only on the main thread: the polling loop and the signal sources
    /// both run there, so no further synchronisation is required.
    private var stopping = false
    private var waitingAnnounced = false
    private var lastErrorMessage: String?
    private var signalSources: [DispatchSourceSignal] = []
    /// The source that currently owns the open device, or nil. Mutated only on
    /// the main thread (the polling loop and the stdin read source both run
    /// there), so a paint command never races a capture-session change.
    private var activeSource: AKP03IOKitInputSource?
    /// Stdin command assembly. Bounded: the buffer is discarded if a single
    /// unterminated line grows past the accepted maximum, so no burst of
    /// malformed input can grow memory without limit.
    private var stdinBuffer: [UInt8] = []
    private var stdinSource: DispatchSourceRead?

    init(pollSeconds: Int, openProtocol: AKP03OpenProtocolMode) {
        self.pollSeconds = pollSeconds
        self.openProtocol = openProtocol
    }

    func run() {
        installSignalHandlers()
        installStdinHandler()
        emit(AKP03HIDStreamLine.starting)
        while !stopping {
            if AKP03DevicePresence.isPresent() {
                runCaptureSession()
            }
            // Waiting means "no active capture session": the device is absent,
            // or it is present but could not be opened. Edge-triggered so a quiet
            // wait never spams the protocol.
            if !stopping, !waitingAnnounced {
                emit(AKP03HIDStreamLine.waiting)
                waitingAnnounced = true
            }
            if !stopping { runLoop(seconds: pollSeconds) }
        }
    }

    /// One acquisition attempt with a FRESH source (and therefore a fresh
    /// `IOHIDManager`). Reusing a device object across a re-enumeration fails to
    /// open, so each attempt starts clean.
    private func runCaptureSession() {
        let source = AKP03IOKitInputSource(openProtocol: openProtocol)
        do {
            try source.startReceiving { [weak self] event in
                switch event {
                case let .known(control, pressed):
                    self?.emit(AKP03HIDStreamLine.control(control: control, pressed: pressed))
                case .unknownPacket:
                    self?.emit(AKP03HIDStreamLine.unknown)
                }
            }
        } catch {
            source.stopReceiving()
            let message = shortMessage(for: error)
            if message != lastErrorMessage {
                // A failed open-protocol write must not kill the process. Log it
                // to stderr (the frozen stdout protocol still carries only the
                // pre-existing `error` event) and keep serving: the outer loop
                // retries acquisition on the next poll.
                writeStderr("AKP03HIDStream: device acquisition failed: \(message)\n")
                emit(AKP03HIDStreamLine.error(message: message))
                // Only claim the deck is blocked if it is still attached: a failure
                // caused by the cable being pulled mid-attempt is an absent deck, and
                // the loop below says so on its own.
                if AKP03DevicePresence.isPresent() {
                    emit(AKP03HIDStreamLine.blocked(message: message))
                }
                lastErrorMessage = message
            }
            return
        }

        waitingAnnounced = false
        lastErrorMessage = nil
        activeSource = source
        emit(AKP03HIDStreamLine.ready)

        // Serve input until the device disappears or we are told to stop.
        while !stopping, AKP03DevicePresence.isPresent() {
            runLoop(seconds: pollSeconds)
        }
        source.stopReceiving()
        activeSource = nil
        if !stopping {
            emit(AKP03HIDStreamLine.deviceLost)
        }
    }

    // MARK: - Stdin commands (the only caller-supplied device payload)

    /// Reads JSON command lines from this process's own stdin on the main queue,
    /// so a command is handled on the same thread that owns the capture session
    /// (the device is scheduled on this run loop). A closed stdin simply stops the
    /// reader and leaves input streaming: the helper still runs until told to stop.
    private func installStdinHandler() {
        let source = DispatchSource.makeReadSource(fileDescriptor: STDIN_FILENO, queue: .main)
        source.setEventHandler { [weak self] in self?.readStdin() }
        source.resume()
        stdinSource = source
    }

    private func readStdin() {
        var buffer = [UInt8](repeating: 0, count: 4096)
        let count = buffer.withUnsafeMutableBytes { raw -> Int in
            guard let base = raw.baseAddress else { return 0 }
            return read(STDIN_FILENO, base, raw.count)
        }
        if count > 0 {
            stdinBuffer.append(contentsOf: buffer[0..<count])
            consumeStdinLines()
            return
        }
        if count < 0, errno == EINTR { return }
        // EOF (0) or a read error: stop reading stdin but keep streaming input.
        stdinSource?.cancel()
        stdinSource = nil
        stdinBuffer.removeAll()
    }

    private func consumeStdinLines() {
        while let newline = stdinBuffer.firstIndex(of: 0x0A) {
            let line = stdinBuffer[..<newline]
            stdinBuffer.removeSubrange(...newline)
            if let text = String(bytes: line, encoding: .utf8) {
                handleCommand(text)
            } else {
                emit(AKP03HIDStreamLine.paintError(reason: "command line is not valid UTF-8"))
            }
        }
        if stdinBuffer.count > AKP03HIDStreamRequest.maximumLineCharacters {
            stdinBuffer.removeAll()
            emit(AKP03HIDStreamLine.paintError(reason: "command line is too long"))
        }
    }

    private func handleCommand(_ line: String) {
        let text = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { return }
        let request: AKP03HIDStreamRequest
        do {
            request = try AKP03HIDStreamRequest.parse(line: line)
        } catch {
            emit(AKP03HIDStreamLine.paintError(reason: error.localizedDescription))
            return
        }
        switch request {
        case let .paint(payload):
            guard let source = activeSource else {
                emit(AKP03HIDStreamLine.paintError(reason: "the AKP03 device is not open"))
                return
            }
            do {
                try source.sendPaintOutputReport(payload)
                emit(AKP03HIDStreamLine.paintResult(bytes: payload.count))
            } catch {
                emit(AKP03HIDStreamLine.paintError(reason: shortMessage(for: error)))
            }
        }
    }

    private func installSignalHandlers() {
        // A closed stdout should end the stream cleanly, not kill the process.
        signal(SIGPIPE, SIG_IGN)
        for signum in [SIGINT, SIGTERM] {
            signal(signum, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: signum, queue: .main)
            source.setEventHandler { [weak self] in self?.requestStop() }
            source.resume()
            signalSources.append(source)
        }
    }

    private func requestStop() {
        stopping = true
        CFRunLoopStop(CFRunLoopGetMain())
    }

    private func runLoop(seconds: Int) {
        _ = CFRunLoopRunInMode(.defaultMode, CFTimeInterval(seconds), false)
    }

    /// Writes exactly one protocol line to stdout. There is no other stdout output.
    private func emit(_ line: String) {
        guard let data = (line + "\n").data(using: .utf8) else { return }
        do {
            try FileHandle.standardOutput.write(contentsOf: data)
        } catch {
            requestStop()
        }
    }

    private func shortMessage(for error: Error) -> String {
        guard let adapterError = error as? AKP03IOKitInputError else {
            return "device acquisition failed"
        }
        switch adapterError {
        case .inputMonitoringPermissionDenied: return "input monitoring permission not granted"
        case .noMatchingInterface: return "device not found"
        case .ambiguousCandidates: return "multiple devices matched"
        case .unidentifiedCandidates: return "device identity unresolved"
        case .registryResolutionFailed: return "device registry resolution failed"
        case .noUsableVendorInputElement: return "no usable vendor input element"
        case .managerOpenFailed: return "hid manager open failed"
        case .deviceOpenFailed: return "device open failed"
        case .alreadyReceiving: return "already receiving"
        case .establishedOpenProtocolBootstrapFailed: return "bootstrap failed"
        case .fixedKeepaliveFailed: return "keepalive failed"
        case .deviceNotOpen: return "the AKP03 device is not open"
        case let .invalidOutputPayload(count): return "paint payload must be exactly 1024 bytes (received \(count))"
        case let .outputReportFailed(code): return "paint output report failed (\(code))"
        }
    }
}

/// Read-only device-presence probe.
///
/// It queries the IOKit registry with the exact same identity the verified
/// adapter matches (VendorID + ProductID + PrimaryUsagePage 0xffa0). It creates
/// no `IOHIDManager` and opens no device, so it never disturbs the live capture
/// handle; it is used to decide when to attempt acquisition and to notice a
/// device that has disappeared.
private enum AKP03DevicePresence {
    static func isPresent() -> Bool {
        let matching = IOServiceMatching("IOHIDDevice") as NSMutableDictionary
        matching["VendorID"] = AKP03IOKitInputSelection.vendorID
        matching["ProductID"] = AKP03IOKitInputSelection.productID
        matching["PrimaryUsagePage"] = AKP03IOKitInputSelection.vendorUsagePage
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, matching, &iterator) == KERN_SUCCESS else {
            return false
        }
        defer { IOObjectRelease(iterator) }
        let service = IOIteratorNext(iterator)
        if service == 0 { return false }
        IOObjectRelease(service)
        return true
    }
}

private func writeStderr(_ text: String) {
    FileHandle.standardError.write(Data(text.utf8))
}
