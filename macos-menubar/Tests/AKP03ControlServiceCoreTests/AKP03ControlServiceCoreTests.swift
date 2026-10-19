#if canImport(XCTest)
import XCTest
@testable import AKP03ControlServiceCore
@testable import AKP03IOKitInput

final class AKP03ControlServiceCoreTests: XCTestCase {
    func testArgumentsDefaultAndCheckAreFiniteChecks() throws {
        XCTAssertEqual(try AKP03ControlServiceCommand.parse(arguments: []), .check)
        XCTAssertEqual(try AKP03ControlServiceCommand.parse(arguments: ["--check"]), .check)
        XCTAssertEqual(try AKP03ControlServiceCommand.parse(arguments: ["--help"]), .help)
        XCTAssertThrowsError(try AKP03ControlServiceCommand.parse(arguments: ["--serve"]))
    }

    func testInputSourceLifecycleDeliversSemanticEventsAndStopsOnce() throws {
        let service = AKP03ControlService()
        let source = FakeInputSource()
        try service.startReceiving(from: source)
        XCTAssertEqual(source.startCount, 1)
        XCTAssertEqual(service.snapshot.status, .receivingInput)

        source.emit(.known(.topClockwise, pressed: nil))
        source.emit(.unknownPacket)
        XCTAssertEqual(service.snapshot.acceptedEventCount, 1)
        XCTAssertEqual(service.snapshot.ignoredUnknownEventCount, 1)
        XCTAssertEqual(service.snapshot.lastKnownControl, .topClockwise)

        service.stop()
        service.stop()
        XCTAssertEqual(source.stopCount, 1)
        XCTAssertEqual(service.snapshot.status, .stopped)
    }

    func testDoubleStartAndStartAfterStopAreRejectedWithoutTouchingSources() throws {
        let service = AKP03ControlService()
        let first = FakeInputSource()
        let second = FakeInputSource()
        try service.startReceiving(from: first)
        XCTAssertThrowsError(try service.startReceiving(from: second)) {
            XCTAssertEqual($0 as? AKP03ControlServiceLifecycleError, .alreadyStarted)
        }
        XCTAssertEqual(first.startCount, 1)
        XCTAssertEqual(second.startCount, 0)

        service.stop()
        XCTAssertThrowsError(try service.startReceiving(from: second)) {
            XCTAssertEqual($0 as? AKP03ControlServiceLifecycleError, .stopped)
        }
        XCTAssertEqual(second.startCount, 0)
    }

    func testKnownEventsTransitionAndCount() {
        let service = AKP03ControlService()
        XCTAssertEqual(service.snapshot.status, .interfaceReady)
        service.accept(.known(.topClockwise, pressed: nil))
        service.accept(.known(.key0, pressed: true))
        XCTAssertEqual(service.snapshot.status, .receivingInput)
        XCTAssertEqual(service.snapshot.acceptedEventCount, 2)
        XCTAssertEqual(service.snapshot.lastKnownControl, .key0)
        service.stop()
        XCTAssertEqual(service.snapshot.status, .stopped)
    }

    func testUnknownPacketIsRedactedAndDoesNotChangeLastKnownControl() {
        let service = AKP03ControlService()
        service.accept(.known(.button6, pressed: true))
        service.accept(.unknownPacket)
        XCTAssertNil(AKP03ControlEvent.unknownPacket.knownControl)
        XCTAssertEqual(service.snapshot.acceptedEventCount, 1)
        XCTAssertEqual(service.snapshot.ignoredUnknownEventCount, 1)
        XCTAssertEqual(service.snapshot.lastKnownControl, .button6)
    }

    func testDecoderMapsEveryEstablishedLinuxControlCode() {
        let expected: [(UInt8, AKP03KnownControl)] = [
            (0x01, .key0), (0x02, .key1), (0x03, .key2), (0x04, .key3), (0x05, .key4), (0x06, .key5),
            (0x25, .button6), (0x31, .button7), (0x30, .button8),
            (0x51, .topClockwise), (0x50, .topCounterClockwise),
            (0x91, .bottomLeftClockwise), (0x90, .bottomLeftCounterClockwise),
            (0x61, .bottomRightClockwise), (0x60, .bottomRightCounterClockwise),
            (0x35, .topPress), (0x33, .bottomLeftPress), (0x34, .bottomRightPress),
        ]
        for (code, control) in expected {
            XCTAssertEqual(AKP03ControlReportDecoder.decode(ackReport(code)).knownControl, control, "code 0x\(String(code, radix: 16))")
            XCTAssertEqual(AKP03ControlReportDecoder.decodeEventCode(code), .known(control, pressed: nil), "one-byte code 0x\(String(code, radix: 16))")
        }
    }

    func testDecoderCarriesPressReleaseStateInTheSameEventAsTheControl() {
        XCTAssertEqual(AKP03ControlReportDecoder.decode(ackReport(0x01, state: 1)), .known(.key0, pressed: true))
        XCTAssertEqual(AKP03ControlReportDecoder.decode(ackReport(0x01, state: 0)), .known(.key0, pressed: false))
        XCTAssertEqual(AKP03ControlReportDecoder.decode(ackReport(0x31, state: 1)), .known(.button7, pressed: true))
        XCTAssertEqual(AKP03ControlReportDecoder.decode(ackReport(0x35, state: 1)), .known(.topPress, pressed: true))
        XCTAssertEqual(AKP03ControlReportDecoder.decode(ackReport(0x33, state: 0)), .known(.bottomLeftPress, pressed: false))
        // The six delta-carrying knob turns have no press/release semantics, so
        // their state is nil even when the state byte is nonzero.
        XCTAssertEqual(AKP03ControlReportDecoder.decode(ackReport(0x91, state: 1)), .known(.bottomLeftClockwise, pressed: nil))
        XCTAssertEqual(AKP03ControlReportDecoder.decode(ackReport(0x50, state: 1)), .known(.topCounterClockwise, pressed: nil))
        XCTAssertNil(AKP03ControlReportDecoder.decode(ackReport(0x91, state: 1)).pressed)
        XCTAssertEqual(AKP03ControlReportDecoder.decode(ackReport(0x01, state: 1)).knownControl, .key0)
    }

    func testDecoderRedactsMalformedNonACKTooShortAndUnknownPackets() {
        XCTAssertEqual(AKP03ControlReportDecoder.decode([]), .unknownPacket)
        XCTAssertEqual(AKP03ControlReportDecoder.decode([0x41, 0x43, 0x4B]), .unknownPacket)
        XCTAssertEqual(AKP03ControlReportDecoder.decode([0x41, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0]), .unknownPacket)
        XCTAssertEqual(AKP03ControlReportDecoder.decode([0x00, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0, 0x01]), .unknownPacket)
        XCTAssertEqual(AKP03ControlReportDecoder.decode(ackReport(0xFF)), .unknownPacket)
        XCTAssertNil(AKP03ControlReportDecoder.decode(ackReport(0xFF)).knownControl)
    }

    func testInputReportIsTheSelectedDiagnosticTransport() {
        XCTAssertEqual(AKP03IOKitInputSource().transport, .inputReport)
    }

    func testEstablishedOpenProtocolPacketsAreExactFixedPaddedAndIndependentOfCallerMutation() {
        let expectedBodies: [[UInt8]] = [
            [0x43, 0x52, 0x54, 0x00, 0x00, 0x44, 0x49, 0x53],
            [0x43, 0x52, 0x54, 0x00, 0x00, 0x4C, 0x49, 0x47, 0x00, 0x00, 50],
            [0x43, 0x52, 0x54, 0x00, 0x00, 0x43, 0x4C, 0x45, 0x00, 0x00, 0x00, 0xFF],
            [0x43, 0x52, 0x54, 0x00, 0x00, 0x53, 0x54, 0x50],
        ]
        let packets = AKP03IOKitBootstrap.establishedOpenProtocolPackets()
        XCTAssertEqual(packets.count, 4)
        for (packet, body) in zip(packets, expectedBodies) {
            XCTAssertEqual(packet.count, 1024)
            XCTAssertEqual(Array(packet.prefix(body.count)), body)
            XCTAssertTrue(packet.dropFirst(body.count).allSatisfy { $0 == 0 })
        }

        var callerMutablePackets = packets
        callerMutablePackets[0][0] = 0
        XCTAssertEqual(AKP03IOKitBootstrap.establishedOpenProtocolPackets(), packets)
    }

    func testBootstrapOutputSinkEmitsAllFourFixedReportsInOrderAndReportsTypedFailure() throws {
        var sent: [[UInt8]] = []
        try AKP03IOKitBootstrap.bootstrap { packet in
            sent.append(packet)
            return kIOReturnSuccess
        }
        XCTAssertEqual(sent, AKP03IOKitBootstrap.establishedOpenProtocolPackets())

        var failureAttempts = 0
        XCTAssertThrowsError(try AKP03IOKitBootstrap.bootstrap { _ in
            failureAttempts += 1
            return kIOReturnError
        }) {
            XCTAssertEqual($0 as? AKP03IOKitInputError, .establishedOpenProtocolBootstrapFailed(kIOReturnError))
        }
        XCTAssertEqual(failureAttempts, 1)
    }

    func testFixedKeepaliveIsExactPaddedAndUsesOnlyItsDedicatedSink() throws {
        let expectedBody: [UInt8] = [0x43, 0x52, 0x54, 0x00, 0x00, 0x43, 0x4F, 0x4E, 0x4E, 0x45, 0x43, 0x54]
        let packet = AKP03IOKitBootstrap.fixedKeepalivePacket()
        XCTAssertEqual(packet.count, 1024)
        XCTAssertEqual(Array(packet.prefix(expectedBody.count)), expectedBody)
        XCTAssertTrue(packet.dropFirst(expectedBody.count).allSatisfy { $0 == 0 })

        var sent: [[UInt8]] = []
        try AKP03IOKitBootstrap.sendFixedKeepalive { packet in
            sent.append(packet)
            return kIOReturnSuccess
        }
        XCTAssertEqual(sent, [packet])

        XCTAssertThrowsError(try AKP03IOKitBootstrap.sendFixedKeepalive { _ in kIOReturnError }) {
            XCTAssertEqual($0 as? AKP03IOKitInputError, .fixedKeepaliveFailed(kIOReturnError))
        }
    }

    func testOpenProtocolBootstrapIsExplicitAndOffByDefault() {
        XCTAssertFalse(AKP03IOKitInputSource().bootstrapEstablishedOpenProtocol)
        XCTAssertTrue(AKP03IOKitInputSource(bootstrapEstablishedOpenProtocol: true).bootstrapEstablishedOpenProtocol)
        XCTAssertEqual(AKP03IOKitInputSource.fixedKeepaliveInitialDelay, 10)
        XCTAssertEqual(AKP03IOKitInputSource.fixedKeepaliveRepeatInterval, 10)
    }

    func testValueCallbackEligibilityAcceptsConfirmedVendorInputsOnly() {
        let confirmedVendorInputs = [
            AKP03IOKitInputElementMetadata(usagePage: 0xFFA1, usage: 0x3, kind: .logicalInput, reportSizeBits: 8),
            AKP03IOKitInputElementMetadata(usagePage: 0xFFA1, usage: 0x4, kind: .logicalInput, reportSizeBits: 4088),
        ]
        XCTAssertTrue(AKP03IOKitInputValueEligibility.hasUsableInputElement(confirmedVendorInputs))
    }

    func testValueCallbackEligibilityRefusesKeyboardOutputFeatureAndEmptyElements() {
        let keyboardInput = AKP03IOKitInputElementMetadata(usagePage: 0x01, usage: 0x06, kind: .logicalInput, reportSizeBits: 8)
        let vendorOutput = AKP03IOKitInputElementMetadata(usagePage: 0xFFA1, usage: 0x5, kind: .output, reportSizeBits: 8)
        let vendorFeature = AKP03IOKitInputElementMetadata(usagePage: 0xFFA1, usage: 0x6, kind: .feature, reportSizeBits: 8)
        let zeroSizedInput = AKP03IOKitInputElementMetadata(usagePage: 0xFFA1, usage: 0x3, kind: .logicalInput, reportSizeBits: 0)
        let unknownVendorInput = AKP03IOKitInputElementMetadata(usagePage: 0xFFA1, usage: 0x99, kind: .logicalInput, reportSizeBits: 8)
        XCTAssertFalse(AKP03IOKitInputValueEligibility.hasUsableInputElement([keyboardInput, vendorOutput, vendorFeature, zeroSizedInput, unknownVendorInput]))
        XCTAssertFalse(AKP03IOKitInputValueEligibility.hasUsableInputElement([]))
    }

    private func ackReport(_ code: UInt8, state: UInt8 = 0) -> [UInt8] { [0x41, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, code, state] }
}

private final class FakeInputSource: AKP03HIDInputSource, @unchecked Sendable {
    private var receive: (@Sendable (AKP03ControlEvent) -> Void)?
    private(set) var startCount = 0
    private(set) var stopCount = 0

    func startReceiving(_ receive: @escaping @Sendable (AKP03ControlEvent) -> Void) throws {
        startCount += 1
        self.receive = receive
    }

    func stopReceiving() {
        stopCount += 1
        receive = nil
    }

    func emit(_ event: AKP03ControlEvent) { receive?(event) }
}
#endif
