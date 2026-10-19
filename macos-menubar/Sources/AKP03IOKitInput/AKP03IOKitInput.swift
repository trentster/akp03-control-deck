import AKP03ControlServiceCore
import CoreFoundation
import Foundation
import IOKit
import IOKit.hid

/// AKP03-specific IOKit candidate selection. This is kept out of the portable
/// control-service core because its identities come from the macOS HID registry.
public enum AKP03IOKitInputSelection {
    public static let vendorID = 0x0300
    public static let productID = 0x3002
    public static let vendorUsagePage = 0xFFA0
    public static let vendorElementUsagePage = 0xFFA1

    public struct Candidate: Equatable, Sendable {
        public let vendorID: Int
        public let productID: Int
        public let usagePage: Int
        public let registryID: UInt64?

        public init(vendorID: Int, productID: Int, usagePage: Int, registryID: UInt64?) {
            self.vendorID = vendorID
            self.productID = productID
            self.usagePage = usagePage
            self.registryID = registryID
        }

        public var matchesAKP03VendorInterface: Bool {
            vendorID == AKP03IOKitInputSelection.vendorID
                && productID == AKP03IOKitInputSelection.productID
                && usagePage == AKP03IOKitInputSelection.vendorUsagePage
        }
    }

    public enum Result: Equatable, Sendable {
        case none
        case exactlyOne(registryID: UInt64, collectionCount: Int)
        case ambiguous(physicalDeviceCount: Int)
        case unidentified(collectionCount: Int)
    }

    /// Refuses missing device identity and more than one physical device.
    public static func select(_ candidates: [Candidate]) -> Result {
        let matching = candidates.filter(\.matchesAKP03VendorInterface)
        guard !matching.isEmpty else { return .none }
        guard !matching.contains(where: { $0.registryID == nil }) else {
            return .unidentified(collectionCount: matching.count)
        }
        let groups = Dictionary(grouping: matching, by: { $0.registryID! })
        guard groups.count == 1, let group = groups.first else {
            return .ambiguous(physicalDeviceCount: groups.count)
        }
        return .exactlyOne(registryID: group.key, collectionCount: group.value.count)
    }
}

/// Verified on hardware: macOS splits the device's single 512-byte input report
/// into two elements (`0xFFA1/0x3` = byte 0, `0xFFA1/0x4` = bytes 1-511), so no
/// individual element ever carries a complete `ACK` frame and a per-element
/// value callback can only ever yield `.unknownPacket`. The whole-report
/// callback is therefore the only transport that can decode events, mirroring
/// Linux hidraw.
public enum AKP03IOKitInputTransport: Equatable, Sendable {
    case inputReport
}

/// Device-free element metadata used to reject keyboard, output, feature, and
/// collection elements before value callback registration.
public struct AKP03IOKitInputElementMetadata: Equatable, Sendable {
    public enum Kind: Equatable, Sendable { case logicalInput, output, feature, collection, other }

    public let usagePage: Int
    public let usage: Int
    public let kind: Kind
    public let reportSizeBits: Int

    public init(usagePage: Int, usage: Int, kind: Kind, reportSizeBits: Int) {
        self.usagePage = usagePage
        self.usage = usage
        self.kind = kind
        self.reportSizeBits = reportSizeBits
    }
}

public enum AKP03IOKitInputValueEligibility {
    /// Device inspection confirmed vendor-page logical inputs usage 0x3 (8 bits)
    /// and usage 0x4 (4088 bits). The rule intentionally does not select from
    /// the separate keyboard collection or vendor output elements.
    public static func hasUsableInputElement(_ elements: [AKP03IOKitInputElementMetadata]) -> Bool {
        elements.contains {
            $0.usagePage == AKP03IOKitInputSelection.vendorElementUsagePage
                && ($0.usage == 0x3 || $0.usage == 0x4)
                && $0.kind == .logicalInput
                && $0.reportSizeBits > 0
        }
    }
}

public enum AKP03IOKitInputError: LocalizedError, Equatable, Sendable {
    case alreadyReceiving
    case noMatchingInterface
    case unidentifiedCandidates(Int)
    case ambiguousCandidates(Int)
    case registryResolutionFailed
    case noUsableVendorInputElement
    case managerOpenFailed(Int32)
    case deviceOpenFailed(Int32)
    case establishedOpenProtocolBootstrapFailed(Int32)
    case fixedKeepaliveFailed(Int32)
    case deviceNotOpen
    case invalidOutputPayload(Int)
    case outputReportFailed(Int32)
    case inputMonitoringPermissionDenied

    public var errorDescription: String? {
        switch self {
        case .alreadyReceiving: "IOKit input source is already receiving"
        case .noMatchingInterface: "no AKP03 interface matched the required vendor HID identity"
        case let .unidentifiedCandidates(count): "refusing \(count) matching collection(s) without a registry identity"
        case let .ambiguousCandidates(count): "refusing ambiguity: \(count) physical AKP03 interfaces matched"
        case .registryResolutionFailed: "matching registry device could not be resolved"
        case .noUsableVendorInputElement: "selected vendor collection has no usable logical input element"
        case .managerOpenFailed: "input-only HID manager open failed"
        case .deviceOpenFailed: "input-only HID device open failed"
        case .establishedOpenProtocolBootstrapFailed: "established Linux open-protocol bootstrap output report failed"
        case .fixedKeepaliveFailed: "established Linux fixed keepalive output report failed"
        case .deviceNotOpen: "no AKP03 device is open, so the paint payload cannot be written"
        case let .invalidOutputPayload(count): "paint payload must be exactly 1024 bytes (received \(count))"
        case let .outputReportFailed(code): "paint output report failed (\(code))"
        case .inputMonitoringPermissionDenied:
            """
            Input Monitoring permission is not granted, so macOS will open the device but never \
            deliver a single input report. Grant it in System Settings → Privacy & Security → \
            Input Monitoring for the running app, then relaunch: a process does not pick up a new \
            grant while it is running, and a bare command-line binary launched from a terminal may \
            never be offered the grant at all.
            """
        }
    }
}

/// AKP03 IOKit adapter. It emits only semantic events decoded by
/// `AKP03ControlReportDecoder`; no raw value representation crosses the callback
/// boundary. Its output is exactly two things, both through the single audited
/// `AKP03IOKitBootstrap` sink as report-ID-0 output reports: the opt-in fixed
/// Linux lifecycle packets, and one caller-supplied 1024-byte painting payload at
/// a time (see `sendPaintOutputReport`). There is no feature report, no
/// device-value write, and no other output API.
public final class AKP03IOKitInputSource: AKP03HIDInputSource, @unchecked Sendable {
    /// One input report is 512 bytes with no report ID in use; keep headroom
    /// rather than assuming the exact length.
    private static let maximumReportBytes = 1024
    /// Bounded wait for the device to appear. After a re-enumeration macOS
    /// briefly publishes no matching HID device even though the deck is
    /// attached, so a single immediate lookup fails spuriously.
    private static let candidateDiscoveryAttempts = 20
    private static let candidateDiscoveryInterval: TimeInterval = 0.1
    public static let fixedKeepaliveInitialDelay: TimeInterval = 10
    public static let fixedKeepaliveRepeatInterval: TimeInterval = 10

    public let transport: AKP03IOKitInputTransport = .inputReport
    /// Which fixed open-protocol packets this source sends on acquisition. Defaults
    /// to `.none`; the streaming helper opts into `.wake`, and the bounded
    /// diagnostic opts into `.full`. It never carries a caller-supplied payload.
    public let openProtocol: AKP03OpenProtocolMode
    /// Compatibility accessor: true only for the full four-packet Linux bootstrap.
    public var bootstrapEstablishedOpenProtocol: Bool { openProtocol == .full }
    private let lock = NSLock()
    private var manager: IOHIDManager?
    private var device: IOHIDDevice?
    private var reportBuffer: UnsafeMutablePointer<UInt8>?
    private var isStarting = false
    private var receive: (@Sendable (AKP03ControlEvent) -> Void)?
    private var runLoop: CFRunLoop?
    private var keepaliveTimer: CFRunLoopTimer?

    public init(openProtocol: AKP03OpenProtocolMode = .none) {
        self.openProtocol = openProtocol
    }

    /// Convenience initializer retained for the bounded diagnostic and checks.
    public convenience init(bootstrapEstablishedOpenProtocol: Bool) {
        self.init(openProtocol: bootstrapEstablishedOpenProtocol ? .full : .none)
    }
    deinit { stopReceiving() }

    public func startReceiving(_ receive: @escaping @Sendable (AKP03ControlEvent) -> Void) throws {
        // Checked before any state is claimed: without this grant macOS opens the
        // device successfully but never delivers a single input report.
        try Self.ensureInputMonitoringGranted()

        try lock.withLock {
            guard self.manager == nil, !isStarting else { throw AKP03IOKitInputError.alreadyReceiving }
            isStarting = true
        }

        let manager = IOHIDManagerCreate(kCFAllocatorDefault, IOOptionBits(kIOHIDOptionsTypeNone))
        IOHIDManagerSetDeviceMatching(manager, [
            "VendorID": AKP03IOKitInputSelection.vendorID,
            "ProductID": AKP03IOKitInputSelection.productID,
            "PrimaryUsagePage": AKP03IOKitInputSelection.vendorUsagePage,
        ] as NSDictionary)
        let managerResult = IOHIDManagerOpen(manager, IOOptionBits(kIOHIDOptionsTypeNone))
        guard managerResult == kIOReturnSuccess else {
            lock.withLock { isStarting = false }
            _ = IOHIDManagerClose(manager, IOOptionBits(kIOHIDOptionsTypeNone))
            throw AKP03IOKitInputError.managerOpenFailed(managerResult)
        }

        var selectedDevice: IOHIDDevice?
        var scheduledRunLoop: CFRunLoop?
        var openedDevice = false
        var allocatedBuffer: UnsafeMutablePointer<UInt8>?
        do {
            let candidates = try Self.waitForCandidates(from: manager)
            let selected: NativeCandidate
            switch AKP03IOKitInputSelection.select(candidates.map(\.candidate)) {
            case .none: throw AKP03IOKitInputError.noMatchingInterface
            case let .unidentified(collectionCount): throw AKP03IOKitInputError.unidentifiedCandidates(collectionCount)
            case let .ambiguous(physicalDeviceCount): throw AKP03IOKitInputError.ambiguousCandidates(physicalDeviceCount)
            case let .exactlyOne(registryID, _):
                guard let candidate = candidates.first(where: { $0.registryID == registryID }) else {
                    throw AKP03IOKitInputError.registryResolutionFailed
                }
                selected = candidate
            }
            guard AKP03IOKitInputValueEligibility.hasUsableInputElement(selected.elements) else {
                throw AKP03IOKitInputError.noUsableVendorInputElement
            }

            let runLoop = CFRunLoopGetCurrent()!
            selectedDevice = selected.device
            scheduledRunLoop = runLoop
            // The report buffer must stay alive for as long as the callback is
            // registered, so the instance owns it rather than the stack.
            let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: Self.maximumReportBytes)
            buffer.initialize(repeating: 0, count: Self.maximumReportBytes)
            allocatedBuffer = buffer
            IOHIDDeviceRegisterInputReportCallback(
                selected.device,
                buffer,
                CFIndex(Self.maximumReportBytes),
                inputReportCallback,
                Unmanaged.passUnretained(self).toOpaque()
            )
            IOHIDDeviceScheduleWithRunLoop(selected.device, runLoop, CFRunLoopMode.defaultMode.rawValue)
            let deviceResult = IOHIDDeviceOpen(selected.device, IOOptionBits(kIOHIDOptionsTypeNone))
            guard deviceResult == kIOReturnSuccess else {
                throw AKP03IOKitInputError.deviceOpenFailed(deviceResult)
            }
            openedDevice = true
            if openProtocol != .none {
                try AKP03IOKitBootstrap.bootstrap(openProtocol, to: selected.device)
            }

            let keepaliveTimer = openProtocol.sendsDeviceOutput ? makeFixedKeepaliveTimer() : nil
            lock.withLock {
                self.manager = manager
                self.device = selected.device
                self.reportBuffer = allocatedBuffer
                self.receive = receive
                self.runLoop = runLoop
                self.keepaliveTimer = keepaliveTimer
                isStarting = false
            }
            if let keepaliveTimer {
                CFRunLoopAddTimer(runLoop, keepaliveTimer, CFRunLoopMode.defaultMode)
            }
        } catch {
            if let selectedDevice, let scheduledRunLoop {
                IOHIDDeviceUnscheduleFromRunLoop(selectedDevice, scheduledRunLoop, CFRunLoopMode.defaultMode.rawValue)
            }
            if let selectedDevice, openedDevice {
                _ = IOHIDDeviceClose(selectedDevice, IOOptionBits(kIOHIDOptionsTypeNone))
            }
            allocatedBuffer?.deallocate()
            lock.withLock { isStarting = false }
            _ = IOHIDManagerClose(manager, IOOptionBits(kIOHIDOptionsTypeNone))
            throw error
        }
    }

    public func stopReceiving() {
        let resources = lock.withLock { () -> (IOHIDManager, IOHIDDevice, CFRunLoop, CFRunLoopTimer?, UnsafeMutablePointer<UInt8>?)? in
            guard let manager, let device, let runLoop else { return nil }
            self.manager = nil
            self.device = nil
            receive = nil
            self.runLoop = nil
            let keepaliveTimer = self.keepaliveTimer
            self.keepaliveTimer = nil
            let buffer = reportBuffer
            reportBuffer = nil
            return (manager, device, runLoop, keepaliveTimer, buffer)
        }
        guard let (manager, device, runLoop, keepaliveTimer, buffer) = resources else { return }
        if let keepaliveTimer { CFRunLoopTimerInvalidate(keepaliveTimer) }
        IOHIDDeviceUnscheduleFromRunLoop(device, runLoop, CFRunLoopMode.defaultMode.rawValue)
        _ = IOHIDDeviceClose(device, IOOptionBits(kIOHIDOptionsTypeNone))
        _ = IOHIDManagerClose(manager, IOOptionBits(kIOHIDOptionsTypeNone))
        buffer?.deallocate()
    }

    /// Writes one validated 1024-byte painting payload to the currently open
    /// device as a report-ID-0 output report, through the single audited sink.
    ///
    /// This is the only API a caller can hand raw bytes to, and it deliberately
    /// accepts no length but `AKP03IOKitBootstrap.payloadLength` (the payload is
    /// length-checked by `AKP03IOKitBootstrap.paint` before any write). It throws
    /// `deviceNotOpen` when there is no open device, so the caller can report an
    /// honest failure instead of silently dropping a screen update.
    public func sendPaintOutputReport(_ payload: [UInt8]) throws {
        let device = lock.withLock { self.device }
        guard let device else { throw AKP03IOKitInputError.deviceNotOpen }
        try AKP03IOKitBootstrap.paint(payload, to: device)
    }

    /// Decodes the complete 512-byte report, mirroring Linux hidraw. Reading the
    /// whole report is required: the two elements macOS splits it into can never
    /// carry a decodable `ACK` frame on their own. The single decode returns the
    /// control and its press/release state together; there is no second path.
    fileprivate func receiveInputReport(status: IOReturn, report: UnsafeMutablePointer<UInt8>, length: CFIndex) {
        guard status == kIOReturnSuccess, length > 0, length <= Self.maximumReportBytes else {
            receiveSemanticEvent(.unknownPacket)
            return
        }
        let bytes = Array(UnsafeBufferPointer(start: report, count: Int(length)))
        receiveSemanticEvent(AKP03ControlReportDecoder.decode(bytes))
    }

    private func receiveSemanticEvent(_ event: AKP03ControlEvent) {
        let callback = lock.withLock { receive }
        callback?(event)
    }

    private func makeFixedKeepaliveTimer() -> CFRunLoopTimer {
        CFRunLoopTimerCreateWithHandler(
            kCFAllocatorDefault,
            CFAbsoluteTimeGetCurrent() + Self.fixedKeepaliveInitialDelay,
            Self.fixedKeepaliveRepeatInterval,
            0,
            0
        ) { [weak self] _ in
            self?.sendFixedKeepalive()
        }
    }

    private func sendFixedKeepalive() {
        let device = lock.withLock { self.device }
        guard let device else { return }
        do {
            try AKP03IOKitBootstrap.sendFixedKeepalive(to: device)
        } catch {
            // A failed keepalive write must not end input capture: invalidate the
            // timer so it can never fire against a gone device, but keep the
            // device open and the input callback registered so input keeps being
            // served. This never terminates the process.
            invalidateKeepaliveTimer()
        }
    }

    private func invalidateKeepaliveTimer() {
        let timer = lock.withLock { () -> CFRunLoopTimer? in
            let timer = keepaliveTimer
            keepaliveTimer = nil
            return timer
        }
        if let timer { CFRunLoopTimerInvalidate(timer) }
    }

    /// Without this grant macOS opens the device successfully and then delivers
    /// no input reports whatsoever, with no error anywhere — so it is checked
    /// explicitly and reported as a named failure rather than silently.
    private static func ensureInputMonitoringGranted() throws {
        if IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) == kIOHIDAccessTypeGranted { return }
        _ = IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)
        guard IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) == kIOHIDAccessTypeGranted else {
            throw AKP03IOKitInputError.inputMonitoringPermissionDenied
        }
    }

    /// The manager's device set is populated asynchronously, and after the deck
    /// re-enumerates macOS briefly publishes no matching HID device at all, so a
    /// single immediate lookup fails spuriously. Bounded wait, then fail.
    private static func waitForCandidates(from manager: IOHIDManager) throws -> [NativeCandidate] {
        for attempt in 0..<candidateDiscoveryAttempts {
            let candidates = nativeCandidates(from: manager)
            if !candidates.isEmpty { return candidates }
            if attempt < candidateDiscoveryAttempts - 1 {
                Thread.sleep(forTimeInterval: candidateDiscoveryInterval)
            }
        }
        throw AKP03IOKitInputError.noMatchingInterface
    }
}

private let inputReportCallback: IOHIDReportCallback = { context, status, _, _, _, report, length in
    guard let context else { return }
    Unmanaged<AKP03IOKitInputSource>.fromOpaque(context).takeUnretainedValue()
        .receiveInputReport(status: status, report: report, length: length)
}

private struct NativeCandidate {
    let device: IOHIDDevice
    let candidate: AKP03IOKitInputSelection.Candidate
    let registryID: UInt64?
    let elements: [AKP03IOKitInputElementMetadata]

    init?(device: IOHIDDevice) {
        guard let vendorID = integerProperty(device, "VendorID"),
              let productID = integerProperty(device, "ProductID"),
              let usagePage = integerProperty(device, "PrimaryUsagePage") ?? integerProperty(device, "DeviceUsagePage") else {
            return nil
        }
        let service = IOHIDDeviceGetService(device)
        var registryID: UInt64 = 0
        let identity = service != 0 && IORegistryEntryGetRegistryEntryID(service, &registryID) == KERN_SUCCESS ? registryID : nil
        self.device = device
        self.registryID = identity
        candidate = .init(vendorID: vendorID, productID: productID, usagePage: usagePage, registryID: identity)
        elements = nativeElements(from: device)
    }
}

private func nativeCandidates(from manager: IOHIDManager) -> [NativeCandidate] {
    guard let devices = IOHIDManagerCopyDevices(manager) else { return [] }
    return (devices as NSSet).compactMap { NativeCandidate(device: $0 as! IOHIDDevice) }
}

private func nativeElements(from device: IOHIDDevice) -> [AKP03IOKitInputElementMetadata] {
    guard let elements = IOHIDDeviceCopyMatchingElements(device, nil, IOOptionBits(kIOHIDOptionsTypeNone)) else { return [] }
    return (elements as NSArray).map { item in
        let element = item as! IOHIDElement
        return .init(
            usagePage: Int(IOHIDElementGetUsagePage(element)),
            usage: Int(IOHIDElementGetUsage(element)),
            kind: elementKind(IOHIDElementGetType(element)),
            reportSizeBits: Int(IOHIDElementGetReportSize(element))
        )
    }
}

private func elementKind(_ type: IOHIDElementType) -> AKP03IOKitInputElementMetadata.Kind {
    switch type {
    case kIOHIDElementTypeInput_Misc, kIOHIDElementTypeInput_Button, kIOHIDElementTypeInput_Axis, kIOHIDElementTypeInput_ScanCodes: .logicalInput
    case kIOHIDElementTypeOutput: .output
    case kIOHIDElementTypeFeature: .feature
    case kIOHIDElementTypeCollection: .collection
    default: .other
    }
}

private func integerProperty(_ device: IOHIDDevice, _ key: String) -> Int? {
    (IOHIDDeviceGetProperty(device, key as CFString) as? NSNumber)?.intValue
}
