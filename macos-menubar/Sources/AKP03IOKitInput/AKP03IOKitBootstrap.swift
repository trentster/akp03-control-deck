import AKP03ControlServiceCore
import Foundation
import IOKit
import IOKit.hid

/// The single reviewed AKP03 output path.
///
/// It carries exactly two kinds of report-ID-0 **output** report, both through
/// the one audited `IOHIDDeviceSetReport` call below:
///
/// 1. the fixed, compile-time lifecycle packets (`DIS`/`LIG`/`CLE`/`STP`, the
///    wake packet, and the `CONNECT` keepalive); and
/// 2. one caller-supplied **painting** payload at a time — exactly 1024 bytes,
///    only ever from `AKP03HIDStream`'s own stdin (see ``paint(_:sendOutputReport:)``).
///
/// There is no feature-report path, no device-value write, and no third call
/// site. `paint` refuses anything that is not exactly 1024 bytes before a write
/// is attempted, and the sink is never widened: every write — a fixed lifecycle
/// packet or a caller-supplied painting payload — still goes through the one
/// `sendOutputReport(to:packet:)` below.
public enum AKP03IOKitBootstrap {
    public static let reportID: CFIndex = 0
    public static let payloadLength = 1024

    /// Returns fresh copies of the four fixed Linux `Deck.open()` reports, in order.
    public static func establishedOpenProtocolPackets() -> [[UInt8]] {
        [
            fixedPacket([0x43, 0x52, 0x54, 0x00, 0x00, 0x44, 0x49, 0x53]),
            fixedPacket([0x43, 0x52, 0x54, 0x00, 0x00, 0x4C, 0x49, 0x47, 0x00, 0x00, 50]),
            fixedPacket([0x43, 0x52, 0x54, 0x00, 0x00, 0x43, 0x4C, 0x45, 0x00, 0x00, 0x00, 0xFF]),
            fixedPacket([0x43, 0x52, 0x54, 0x00, 0x00, 0x53, 0x54, 0x50]),
        ]
    }

    /// Returns a fresh copy of the fixed Linux wake report (`CRT\0\0DIS`). It is
    /// the first packet of `establishedOpenProtocolPackets()` and is proven
    /// sufficient on hardware for the deck to begin delivering input on its own.
    public static func fixedWakePacket() -> [UInt8] {
        fixedPacket([0x43, 0x52, 0x54, 0x00, 0x00, 0x44, 0x49, 0x53])
    }

    /// The fixed packets for one allowed mode. `none` emits nothing; `wake` emits
    /// the single fixed wake report; `full` emits the four Linux `Deck.open()`
    /// reports in order. No mode can emit anything but these compile-time constants.
    public static func openProtocolPackets(_ mode: AKP03OpenProtocolMode) -> [[UInt8]] {
        switch mode {
        case .none: []
        case .wake: [fixedWakePacket()]
        case .full: establishedOpenProtocolPackets()
        }
    }

    /// Returns a fresh copy of the fixed Linux periodic keepalive report.
    public static func fixedKeepalivePacket() -> [UInt8] {
        fixedPacket([0x43, 0x52, 0x54, 0x00, 0x00, 0x43, 0x4F, 0x4E, 0x4E, 0x45, 0x43, 0x54])
    }

    /// Device-free output-sink seam. It emits only the fixed reports for `mode`,
    /// in order; the caller supplies the sink, never the bytes.
    public static func bootstrap(
        _ mode: AKP03OpenProtocolMode,
        sendFixedOutputReport: ([UInt8]) -> IOReturn
    ) throws {
        for packet in openProtocolPackets(mode) {
            let result = sendFixedOutputReport(packet)
            guard result == kIOReturnSuccess else {
                throw AKP03IOKitInputError.establishedOpenProtocolBootstrapFailed(result)
            }
        }
    }

    /// Device-free output-sink seam. It emits only the fixed four-report bootstrap in order.
    public static func bootstrap(sendFixedOutputReport: ([UInt8]) -> IOReturn) throws {
        try bootstrap(.full, sendFixedOutputReport: sendFixedOutputReport)
    }

    /// Device-free output-sink seam. It emits only the fixed Linux keepalive.
    public static func sendFixedKeepalive(sendFixedOutputReport: ([UInt8]) -> IOReturn) throws {
        let result = sendFixedOutputReport(fixedKeepalivePacket())
        guard result == kIOReturnSuccess else {
            throw AKP03IOKitInputError.fixedKeepaliveFailed(result)
        }
    }

    /// Device-free output-sink seam for a single caller-supplied painting payload.
    ///
    /// This is the only place a caller's bytes reach the device: the payload must
    /// be exactly one 1024-byte report, and it is written as report-ID-0 output
    /// through the same single audited sink the fixed lifecycle packets use. A
    /// payload of any other length is refused (`invalidOutputPayload`) before the
    /// sink is called, and a failing sink surfaces as `outputReportFailed`.
    public static func paint(
        _ payload: [UInt8],
        sendOutputReport: ([UInt8]) -> IOReturn
    ) throws {
        guard payload.count == payloadLength else {
            throw AKP03IOKitInputError.invalidOutputPayload(payload.count)
        }
        let result = sendOutputReport(payload)
        guard result == kIOReturnSuccess else {
            throw AKP03IOKitInputError.outputReportFailed(result)
        }
    }

    /// Writes one validated 1024-byte painting payload to the given device.
    static func paint(_ payload: [UInt8], to device: IOHIDDevice) throws {
        try paint(payload) { sendOutputReport(to: device, packet: $0) }
    }

    static func bootstrap(_ mode: AKP03OpenProtocolMode, to device: IOHIDDevice) throws {
        try bootstrap(mode) { fixedPacket in
            sendOutputReport(to: device, packet: fixedPacket)
        }
    }

    static func sendFixedKeepalive(to device: IOHIDDevice) throws {
        try sendFixedKeepalive { fixedPacket in
            sendOutputReport(to: device, packet: fixedPacket)
        }
    }

    private static func sendOutputReport(to device: IOHIDDevice, packet: [UInt8]) -> IOReturn {
        packet.withUnsafeBufferPointer { buffer in
            IOHIDDeviceSetReport(
                device,
                kIOHIDReportTypeOutput,
                reportID,
                buffer.baseAddress!,
                CFIndex(buffer.count)
            )
        }
    }

    private static func fixedPacket(_ body: [UInt8]) -> [UInt8] {
        body + Array(repeating: 0, count: payloadLength - body.count)
    }
}
