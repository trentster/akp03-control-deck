import Foundation

/// The semantic, input-only boundary implemented by a platform adapter.
///
/// An adapter may deliver only typed events. This protocol deliberately has no
/// output, feature-report, configuration, networking, or shell operation.
public protocol AKP03HIDInputSource: AnyObject, Sendable {
    func startReceiving(
        _ receive: @escaping @Sendable (AKP03ControlEvent) -> Void
    ) throws
    func stopReceiving()
}

/// The fixed, audited AKP03 lifecycle output a source may send on acquisition.
///
/// Every packet in every mode is a compile-time constant defined by the single
/// audited output sink; there is no caller-supplied payload and no mode that can
/// send anything else.
///
/// - `none`: send nothing at all (diagnostic-only).
/// - `wake`: send only the fixed wake packet `CRT\0\0DIS`. This is the default:
///   it is proven sufficient to make the deck deliver input, and it deliberately
///   does *not* clear the LCDs (macOS cannot repaint them yet).
/// - `full`: send the four fixed Linux `Deck.open()` packets in order (DIS,
///   LIG+50, CLE+0xff, STP), retained for parity with Linux.
///
/// In `wake` and `full` the fixed `CRT\0\0CONNECT` keepalive is also sent; in
/// `none` nothing is sent.
public enum AKP03OpenProtocolMode: String, CaseIterable, Equatable, Sendable {
    case none
    case wake
    case full

    /// The mode the streaming helper uses unless overridden.
    public static let defaultMode: AKP03OpenProtocolMode = .wake

    /// Whether this mode sends any device output at all (open packets + keepalive).
    public var sendsDeviceOutput: Bool { self != .none }
}

/// Controls that the service recognizes as safe semantic input.
public enum AKP03KnownControl: String, CaseIterable, Equatable, Sendable {
    case key0, key1, key2, key3, key4, key5
    case button6, button7, button8
    case topClockwise, topCounterClockwise
    case bottomLeftClockwise, bottomLeftCounterClockwise
    case bottomRightClockwise, bottomRightCounterClockwise
    case topPress, bottomLeftPress, bottomRightPress
}

/// A sanitized input event. Unknown input is intentionally represented without
/// packet bytes, report IDs, lengths, or decoded vendor data.
///
/// A known control carries its press/release state in the same event, so a
/// consumer never has to correlate a second side channel. `pressed` is the
/// documented state byte when the control has press/release semantics (keys,
/// buttons, knob presses); it is `nil` for controls with none (knob turns), and
/// is never a guess. Raw bytes still cannot cross this boundary.
public enum AKP03ControlEvent: Equatable, Sendable {
    case known(AKP03KnownControl, pressed: Bool?)
    case unknownPacket

    public var knownControl: AKP03KnownControl? {
        guard case let .known(control, _) = self else { return nil }
        return control
    }

    /// The documented press state of a known control, or nil when the event is an
    /// opaque packet or the control carries no press/release state.
    public var pressed: Bool? {
        guard case let .known(_, pressed) = self else { return nil }
        return pressed
    }
}

/// Decodes an inbound AKP03 report into the service's sanitized event boundary.
///
/// This is the sole native semantic map from the established Linux ACK event
/// code to `AKP03KnownControl`. It performs the whole job in one pass: it
/// validates the ACK framing, maps the control code, and reads the documented
/// press/release state byte, returning control and state together. All
/// malformed, non-ACK, and unrecognized input collapses to `.unknownPacket`, so
/// report bytes, IDs, lengths, and vendor data cannot escape through the result.
public enum AKP03ControlReportDecoder {
    public static func decode(_ report: [UInt8]) -> AKP03ControlEvent {
        guard report.count > 10,
              report[0] == 0x41,
              report[1] == 0x43,
              report[2] == 0x4B,
              let control = knownControl(forEventCode: report[9]) else {
            return .unknownPacket
        }
        return .known(control, pressed: pressState(for: control, stateByte: report[10]))
    }

    /// Decodes the established one-byte vendor input-value element. That element
    /// carries no state byte, so its state is reported as `nil` rather than
    /// guessed. Unknown values remain redacted rather than surfaced as raw data.
    public static func decodeEventCode(_ code: UInt8) -> AKP03ControlEvent {
        guard let control = knownControl(forEventCode: code) else { return .unknownPacket }
        return .known(control, pressed: nil)
    }

    /// The established Linux driver reports `pressed = None` for the six
    /// delta-carrying knob turns, so they carry no press/release state here. Every
    /// other control takes the documented state byte: nonzero is a press, zero is
    /// a release.
    private static func pressState(for control: AKP03KnownControl, stateByte: UInt8) -> Bool? {
        switch control {
        case .topClockwise, .topCounterClockwise,
             .bottomLeftClockwise, .bottomLeftCounterClockwise,
             .bottomRightClockwise, .bottomRightCounterClockwise:
            return nil
        default:
            return stateByte != 0
        }
    }

    private static func knownControl(forEventCode code: UInt8) -> AKP03KnownControl? {
        switch code {
        case 0x01: .key0
        case 0x02: .key1
        case 0x03: .key2
        case 0x04: .key3
        case 0x05: .key4
        case 0x06: .key5
        case 0x25: .button6
        case 0x31: .button7
        case 0x30: .button8
        case 0x51: .topClockwise
        case 0x50: .topCounterClockwise
        case 0x91: .bottomLeftClockwise
        case 0x90: .bottomLeftCounterClockwise
        case 0x61: .bottomRightClockwise
        case 0x60: .bottomRightCounterClockwise
        case 0x35: .topPress
        case 0x33: .bottomLeftPress
        case 0x34: .bottomRightPress
        default: nil
        }
    }
}

/// Lifecycle state for the in-process, input-only service boundary.
public enum AKP03ServiceStatus: String, Equatable, Sendable {
    case interfaceReady
    case receivingInput
    case stopped
}

/// Harmless state that may later be made visible to a local UI/API adapter.
public struct AKP03ServiceSnapshot: Equatable, Sendable {
    public let status: AKP03ServiceStatus
    public let acceptedEventCount: UInt64
    public let ignoredUnknownEventCount: UInt64
    public let lastKnownControl: AKP03KnownControl?

    public init(status: AKP03ServiceStatus, acceptedEventCount: UInt64, ignoredUnknownEventCount: UInt64, lastKnownControl: AKP03KnownControl?) {
        self.status = status
        self.acceptedEventCount = acceptedEventCount
        self.ignoredUnknownEventCount = ignoredUnknownEventCount
        self.lastKnownControl = lastKnownControl
    }
}

public enum AKP03ControlServiceLifecycleError: Error, Equatable, Sendable {
    case alreadyStarted
    case stopped
}

/// Coordinates semantic input state and owns an explicitly supplied input source.
/// It does not construct or automatically activate a platform HID adapter.
public final class AKP03ControlService: @unchecked Sendable {
    private enum Lifecycle { case ready, starting, receiving, stopped }

    private let lock = NSLock()
    private var lifecycle: Lifecycle = .ready
    private var inputSource: (any AKP03HIDInputSource)?
    private var state = AKP03ServiceSnapshot(status: .interfaceReady, acceptedEventCount: 0, ignoredUnknownEventCount: 0, lastKnownControl: nil)

    public init() {}

    public var snapshot: AKP03ServiceSnapshot { lock.withLock { state } }

    /// Starts one explicitly supplied source. Calls after a successful or pending
    /// start, and calls after stop, are rejected without touching the source.
    public func startReceiving(from source: any AKP03HIDInputSource) throws {
        try lock.withLock {
            switch lifecycle {
            case .ready:
                lifecycle = .starting
                inputSource = source
            case .starting, .receiving:
                throw AKP03ControlServiceLifecycleError.alreadyStarted
            case .stopped:
                throw AKP03ControlServiceLifecycleError.stopped
            }
        }

        do {
            try source.startReceiving { [weak self] event in
                self?.accept(event)
            }
        } catch {
            lock.withLock {
                guard inputSource === source else { return }
                inputSource = nil
                if lifecycle == .starting { lifecycle = .ready }
            }
            throw error
        }

        let stopAfterStart = lock.withLock { () -> Bool in
            guard inputSource === source else { return false }
            guard lifecycle == .stopped else {
                lifecycle = .receiving
                state = AKP03ServiceSnapshot(status: .receivingInput, acceptedEventCount: state.acceptedEventCount, ignoredUnknownEventCount: state.ignoredUnknownEventCount, lastKnownControl: state.lastKnownControl)
                return false
            }
            inputSource = nil
            return true
        }
        if stopAfterStart { source.stopReceiving() }
    }

    /// Records one sanitized event. Unknown packets never enter the snapshot as packet data.
    public func accept(_ event: AKP03ControlEvent) {
        lock.withLock {
            let status: AKP03ServiceStatus = state.status == .stopped ? .stopped : .receivingInput
            switch event {
            case let .known(control, _):
                state = AKP03ServiceSnapshot(status: status, acceptedEventCount: state.acceptedEventCount + 1, ignoredUnknownEventCount: state.ignoredUnknownEventCount, lastKnownControl: control)
            case .unknownPacket:
                state = AKP03ServiceSnapshot(status: status, acceptedEventCount: state.acceptedEventCount, ignoredUnknownEventCount: state.ignoredUnknownEventCount + 1, lastKnownControl: state.lastKnownControl)
            }
        }
    }

    /// Stops the active source at most once. Stopping an unstarted service is safe.
    public func stop() {
        let source = lock.withLock { () -> (any AKP03HIDInputSource)? in
            guard lifecycle != .stopped else { return nil }
            let source = lifecycle == .receiving ? inputSource : nil
            lifecycle = .stopped
            if source != nil { inputSource = nil }
            state = AKP03ServiceSnapshot(status: .stopped, acceptedEventCount: state.acceptedEventCount, ignoredUnknownEventCount: state.ignoredUnknownEventCount, lastKnownControl: state.lastKnownControl)
            return source
        }
        source?.stopReceiving()
    }
}

/// Finite commands supported by the native service executable.
public enum AKP03ControlServiceCommand: Equatable, Sendable {
    case check
    case help

    public static func parse(arguments: [String]) throws -> Self {
        switch arguments {
        case [], ["--check"]: return .check
        case ["--help"], ["-h"]: return .help
        default: throw AKP03ControlServiceArgumentError.unrecognized(arguments)
        }
    }
}

public enum AKP03ControlServiceArgumentError: LocalizedError, Equatable {
    case unrecognized([String])
    public var errorDescription: String? {
        switch self { case let .unrecognized(arguments): "Unrecognized arguments: \(arguments.joined(separator: " "))" }
    }
}

public let akp03ControlServiceUsage = """
Usage: AKP03ControlService [--check]

Runs one finite interface-readiness diagnostic. It does not enumerate or open HID hardware,
start a server, modify configuration, or send HID output/feature reports.
"""
