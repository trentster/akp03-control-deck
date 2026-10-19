import AKP03ControlServiceCore
import AKP03HIDStreamCore
import AKP03IOKitInput
import AKP03KeyInjectCore
import AKP03MenuBarCore
import CoreGraphics
import Darwin
import Foundation

@main
struct AKP03MenuBarChecks {
    static func main() {
        do {
            let arguments = Array(CommandLine.arguments.dropFirst())
            if arguments.first == "--live-supervision" {
                try runLiveSupervision()
                return
            }
            if arguments.first == "--stream" {
                guard arguments.count >= 2 else { throw CheckFailure(message: "--stream needs an events URL") }
                let seconds = (arguments.count >= 4 && arguments[2] == "--seconds") ? (Double(arguments[3]) ?? 6) : 6
                try runStreamProbe(urlString: arguments[1], seconds: seconds)
                return
            }
            let defaultEndpoint = try AKP03Endpoint.fromCommandLine(arguments: [])
            try require(defaultEndpoint.editorURL.absoluteString == "http://127.0.0.1:17865/", "default editor URL changed")
            try require(defaultEndpoint.healthURL.absoluteString == "http://127.0.0.1:17865/api/state", "default health URL changed")
            let customEndpoint = try AKP03Endpoint.fromCommandLine(arguments: ["--url", "http://localhost:9876/editor/"])
            try require(customEndpoint.healthURL.absoluteString == "http://localhost:9876/editor/api/state", "custom health URL should retain base path")
            for arguments in [["--url", "https://example.com/"], ["--url", "http://192.0.2.10:8765/"], ["--url", "http://user:pass@127.0.0.1:8765/"], ["--url", "http://127.0.0.1:8765/?debug=true"], ["--unexpected"]] {
                do { _ = try AKP03Endpoint.fromCommandLine(arguments: arguments); throw CheckFailure(message: "invalid arguments were accepted: \(arguments)") }
                catch is EndpointError { continue }
            }

            try require(try AKP03ReadBridge.parse(arguments: ["--seconds", "30"]) == .capture(seconds: 30), "native bridge should accept an explicit bounded duration")
            try require(try AKP03ReadBridge.parse(arguments: ["--help"]) == .help, "native bridge should accept help")
            for arguments in [[], ["--seconds", "0"], ["--seconds", "31"], ["--seconds"], ["--other", "1"]] {
                do { _ = try AKP03ReadBridge.parse(arguments: arguments); throw CheckFailure(message: "unsafe native bridge arguments were accepted: \(arguments)") }
                catch is AKP03ReadBridge.ArgumentError { continue }
            }

            let collectionA = AKP03IOKitInputSelection.Candidate(vendorID: 0x0300, productID: 0x3002, usagePage: 0xFFA0, registryID: 42)
            let collectionB = AKP03IOKitInputSelection.Candidate(vendorID: 0x0300, productID: 0x3002, usagePage: 0xFFA0, registryID: 42)
            try require(AKP03IOKitInputSelection.select([collectionA, collectionB]) == .exactlyOne(registryID: 42, collectionCount: 2), "two collections with one registry ID must be one physical candidate")
            let secondDevice = AKP03IOKitInputSelection.Candidate(vendorID: 0x0300, productID: 0x3002, usagePage: 0xFFA0, registryID: 43)
            try require(AKP03IOKitInputSelection.select([collectionA, secondDevice]) == .ambiguous(physicalDeviceCount: 2), "multiple physical registry IDs must be refused")
            try require(AKP03IOKitInputSelection.select([collectionA, .init(vendorID: 0x0300, productID: 0x3002, usagePage: 0xFFA0, registryID: nil)]) == .unidentified(collectionCount: 2), "matching collections with no registry ID must be refused")
            try require(AKP03IOKitInputSource().transport == .inputReport, "diagnostic transport must be whole-report only")
            try require(!AKP03IOKitInputSource().bootstrapEstablishedOpenProtocol, "open-protocol bootstrap must default off")
            try require(AKP03IOKitInputSource(bootstrapEstablishedOpenProtocol: true).bootstrapEstablishedOpenProtocol, "open-protocol bootstrap must be explicit")
            try require(AKP03IOKitInputSource.fixedKeepaliveInitialDelay == 10 && AKP03IOKitInputSource.fixedKeepaliveRepeatInterval == 10, "fixed keepalive must start after 10 seconds and repeat every 10 seconds")
            try verifyFixedBootstrapPackets()
            try verifyFixedKeepalivePacket()
            let confirmedInput = AKP03IOKitInputElementMetadata(usagePage: 0xFFA1, usage: 0x4, kind: .logicalInput, reportSizeBits: 4088)
            let keyboardInput = AKP03IOKitInputElementMetadata(usagePage: 0x01, usage: 0x06, kind: .logicalInput, reportSizeBits: 8)
            let vendorOutput = AKP03IOKitInputElementMetadata(usagePage: 0xFFA1, usage: 0x5, kind: .output, reportSizeBits: 8)
            try require(AKP03IOKitInputValueEligibility.hasUsableInputElement([confirmedInput]), "confirmed vendor logical input must be eligible")
            try require(!AKP03IOKitInputValueEligibility.hasUsableInputElement([keyboardInput, vendorOutput]), "keyboard and output elements must be refused")
            try verifyIOKitWriteAllowlist()

            let defaultCommand = try AKP03ControlServiceCommand.parse(arguments: [])
            let checkCommand = try AKP03ControlServiceCommand.parse(arguments: ["--check"])
            let helpCommand = try AKP03ControlServiceCommand.parse(arguments: ["--help"])
            try require(defaultCommand == .check && checkCommand == .check && helpCommand == .help, "control service must accept only finite check/help defaults")
            do { _ = try AKP03ControlServiceCommand.parse(arguments: ["--serve"]); throw CheckFailure(message: "control service accepted an indefinite mode") }
            catch is AKP03ControlServiceArgumentError { }

            let decoderKnownReport: [UInt8] = [0x41, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0x91, 0]
            try require(
                AKP03ControlReportDecoder.decode(decoderKnownReport) == .known(.bottomLeftClockwise, pressed: nil)
                    && AKP03ControlReportDecoder.decodeEventCode(0x91) == .known(.bottomLeftClockwise, pressed: nil),
                "decoder must map established ACK and one-byte vendor codes to typed controls"
            )
            try require(AKP03ControlReportDecoder.decode([0x41, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0, 0]) == .unknownPacket && AKP03ControlReportDecoder.decode([0x00, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0, 0x91]) == .unknownPacket && AKP03ControlReportDecoder.decode([0x41, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0xFF, 0]) == .unknownPacket, "decoder must redact too-short, non-ACK, and unknown input")
            // One pass yields control AND its press/release state together; there is no second decode.
            try require(AKP03ControlReportDecoder.decode([0x41, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0x01, 0x01]) == .known(.key0, pressed: true), "a nonzero state byte must decode as a press in the same event as the control")
            try require(AKP03ControlReportDecoder.decode([0x41, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0x01, 0x00]) == .known(.key0, pressed: false), "a zero state byte must decode as a release in the same event as the control")
            try require(AKP03ControlReportDecoder.decode([0x41, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0x31, 0x01]) == .known(.button7, pressed: true), "a button press must decode control and state together")
            try require(AKP03ControlReportDecoder.decode([0x41, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0x91, 0x01]).pressed == nil, "a knob turn must carry no press/release state")
            try require(AKP03ControlReportDecoder.decode([0x41, 0x43, 0x4B, 0, 0, 0, 0, 0, 0, 0x01, 0x00]).knownControl == .key0, "the control and its state must arrive through one decode path")

            let service = AKP03ControlService()
            let fakeSource = CheckInputSource()
            try service.startReceiving(from: fakeSource)
            try require(fakeSource.startCount == 1, "input source must start exactly once")
            fakeSource.emit(.known(.topClockwise, pressed: nil))
            fakeSource.emit(.known(.key0, pressed: true))
            try require(service.snapshot == AKP03ServiceSnapshot(status: .receivingInput, acceptedEventCount: 2, ignoredUnknownEventCount: 0, lastKnownControl: .key0), "known control events must produce only typed state")
            fakeSource.emit(.unknownPacket)
            try require(AKP03ControlEvent.unknownPacket.knownControl == nil && service.snapshot.acceptedEventCount == 2 && service.snapshot.ignoredUnknownEventCount == 1 && service.snapshot.lastKnownControl == .key0, "unknown packets must remain redacted and not replace known state")
            service.stop()
            service.stop()
            try require(service.snapshot.status == .stopped && fakeSource.stopCount == 1, "control service must stop its input source exactly once")
            do { try service.startReceiving(from: CheckInputSource()); throw CheckFailure(message: "stopped service accepted a new input source") }
            catch AKP03ControlServiceLifecycleError.stopped { }

            try verifyHIDStreamProtocol()
            try verifyHIDStreamPaintProtocol()
            try verifyKeyInjectSurface()

            try verifyLaunchConfiguration()
            try verifyCompanionArguments()
            try verifySupervisionPolicy()
            try verifyStatusWording()
            try verifyDaemonDiagnostics()
            try verifyPermissionsAndEventStreamParsing()

            print("AKP03MenuBar checks passed (endpoint, launch command, supervision policy, status wording, IOKit selection, control-service lifecycle, stream, screen-paint and key-injection boundaries).")
        } catch {
            FileHandle.standardError.write(Data("AKP03MenuBar checks failed: \(String(describing: type(of: error))): \(error.localizedDescription) [\(error)]\n".utf8))
            exit(EXIT_FAILURE)
        }
    }

    private static func require(_ condition: Bool, _ message: String) throws {
        guard condition else { throw CheckFailure(message: message) }
    }

    private static func verifyFixedBootstrapPackets() throws {
        let expectedBodies: [[UInt8]] = [
            [0x43, 0x52, 0x54, 0, 0, 0x44, 0x49, 0x53],
            [0x43, 0x52, 0x54, 0, 0, 0x4C, 0x49, 0x47, 0, 0, 50],
            [0x43, 0x52, 0x54, 0, 0, 0x43, 0x4C, 0x45, 0, 0, 0, 0xFF],
            [0x43, 0x52, 0x54, 0, 0, 0x53, 0x54, 0x50],
        ]
        let packets = AKP03IOKitBootstrap.establishedOpenProtocolPackets()
        try require(packets.count == 4, "bootstrap must define exactly four reports")
        for (packet, body) in zip(packets, expectedBodies) {
            try require(packet.count == 1024 && Array(packet.prefix(body.count)) == body && packet.dropFirst(body.count).allSatisfy({ $0 == 0 }), "bootstrap report body or padding changed")
        }
        var emitted: [[UInt8]] = []
        try AKP03IOKitBootstrap.bootstrap { packet in
            emitted.append(packet)
            return kIOReturnSuccess
        }
        try require(emitted == packets, "bootstrap must emit exactly the four fixed reports in order")
    }

    private static func verifyFixedKeepalivePacket() throws {
        let body: [UInt8] = [0x43, 0x52, 0x54, 0, 0, 0x43, 0x4F, 0x4E, 0x4E, 0x45, 0x43, 0x54]
        let packet = AKP03IOKitBootstrap.fixedKeepalivePacket()
        try require(packet.count == 1024 && Array(packet.prefix(body.count)) == body && packet.dropFirst(body.count).allSatisfy({ $0 == 0 }), "fixed keepalive body or padding changed")
        var emitted: [[UInt8]] = []
        try AKP03IOKitBootstrap.sendFixedKeepalive { packet in
            emitted.append(packet)
            return kIOReturnSuccess
        }
        try require(emitted == [packet], "keepalive must emit exactly one fixed report")
    }

    /// Proves the only reachable device output in any mode is the five fixed,
    /// compile-time packets: the wake report, the four Linux `Deck.open()` reports,
    /// and the keepalive. No mode can emit anything else, and no payload is
    /// caller-supplied.
    private static func verifyOpenProtocolPacketSurface() throws {
        let fixedSet: Set<[UInt8]> = Set(
            AKP03IOKitBootstrap.establishedOpenProtocolPackets()
                + [AKP03IOKitBootstrap.fixedWakePacket()]
                + [AKP03IOKitBootstrap.fixedKeepalivePacket()]
        )
        try require(fixedSet.count == 5, "there must be exactly five distinct fixed packets")

        let full = AKP03IOKitBootstrap.establishedOpenProtocolPackets()
        try require(AKP03IOKitBootstrap.openProtocolPackets(.none).isEmpty, "none mode must emit nothing")
        try require(AKP03IOKitBootstrap.openProtocolPackets(.wake) == [AKP03IOKitBootstrap.fixedWakePacket()], "wake mode must emit exactly the fixed wake packet")
        try require(AKP03IOKitBootstrap.openProtocolPackets(.wake) == Array(full.prefix(1)), "wake must be the first Linux open packet")
        try require(AKP03IOKitBootstrap.openProtocolPackets(.full) == full, "full mode must emit the four Linux open packets in order")

        for mode in AKP03OpenProtocolMode.allCases {
            try require(AKP03IOKitBootstrap.openProtocolPackets(mode).allSatisfy { fixedSet.contains($0) }, "mode \(mode.rawValue) emitted a packet outside the fixed set")
            var emitted: [[UInt8]] = []
            try AKP03IOKitBootstrap.bootstrap(mode) { packet in
                emitted.append(packet)
                return kIOReturnSuccess
            }
            try require(emitted == AKP03IOKitBootstrap.openProtocolPackets(mode), "bootstrap must emit exactly the fixed packets for mode \(mode.rawValue)")
        }

        // A failing sink must surface as the fixed bootstrap failure, never a new path.
        var attempts = 0
        do {
            try AKP03IOKitBootstrap.bootstrap(.wake) { _ in attempts += 1; return kIOReturnError }
            throw CheckFailure(message: "a failing output sink was not surfaced")
        } catch is AKP03IOKitInputError {
            try require(attempts == 1, "a failing wake write must be attempted exactly once")
        }
    }

    /// Source-level audit: exactly one direct IOKit write API is permitted, and it
    /// is the dedicated fixed output-report call. No output/feature/value API may
    /// be introduced elsewhere without making this device-free check fail.
    private static func verifyIOKitWriteAllowlist() throws {
        let source = try String(contentsOfFile: "Sources/AKP03IOKitInput/AKP03IOKitBootstrap.swift", encoding: .utf8)
        let inputSource = try String(contentsOfFile: "Sources/AKP03IOKitInput/AKP03IOKitInput.swift", encoding: .utf8)
        let inputDirectory = URL(fileURLWithPath: "Sources/AKP03IOKitInput", isDirectory: true)
        let files = try FileManager.default.contentsOfDirectory(at: inputDirectory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
        let allSource = try files.map { try String(contentsOf: $0, encoding: .utf8) }.joined(separator: "\n")
        try require(occurrences(of: "IOHIDDeviceSetReport(", in: allSource) == 1, "IOKit input target must contain exactly one direct SetReport call")
        try require(source.contains("kIOHIDReportTypeOutput") && source.contains("reportID,") && source.contains("establishedOpenProtocolPackets()") && source.contains("fixedWakePacket()") && source.contains("fixedKeepalivePacket()"), "SetReport must use only the fixed lifecycle packets and report ID 0")
        try require(source.contains("fixedPacket([0x43, 0x52, 0x54, 0x00, 0x00, 0x43, 0x4F, 0x4E, 0x4E, 0x45, 0x43, 0x54])"), "fixed keepalive must remain CRT\\0\\0CONNECT")
        try require(source.contains("fixedPacket([0x43, 0x52, 0x54, 0x00, 0x00, 0x44, 0x49, 0x53])"), "fixed wake must remain CRT\\0\\0DIS")
        try require(inputSource.contains("openProtocol.sendsDeviceOutput ? makeFixedKeepaliveTimer() : nil") && inputSource.contains("CFRunLoopTimerInvalidate(keepaliveTimer)") && inputSource.contains("self.keepaliveTimer = nil"), "keepalive timer must be opt-in and invalidated during stop and on error")
        for forbidden in ["IOHIDDeviceSetValue(", "IOHIDDeviceSetReportWithCallback(", "IOHIDDeviceSetFeature(", "IOHIDDeviceSetOutputReport("] {
            try require(!allSource.contains(forbidden), "forbidden IOKit write API present: \(forbidden)")
        }
    }

    /// Device-free verification of the AKP03HIDStream line protocol, CLI, and the
    /// single semantic event that carries a control with its press/release state.
    /// It never enumerates, opens, or writes HID hardware.
    private static func verifyHIDStreamProtocol() throws {
        try require(AKP03HIDStreamCommand.parse(arguments: []) == .run(pollSeconds: 2, openProtocol: .wake), "stream must default to a 2s poll and the wake open-protocol")
        try require(AKP03HIDStreamCommand.parse(arguments: ["--help"]) == .help, "stream must accept --help")
        try require(AKP03HIDStreamCommand.parse(arguments: ["-h"]) == .help, "stream must accept -h")
        try require(AKP03HIDStreamCommand.parse(arguments: ["--poll-seconds", "5"]) == .run(pollSeconds: 5, openProtocol: .wake), "stream must accept --poll-seconds")
        try require(AKP03HIDStreamCommand.parse(arguments: ["--poll-seconds", "0"]) == .run(pollSeconds: 1, openProtocol: .wake), "stream must clamp a too-small poll interval up to 1")
        try require(AKP03HIDStreamCommand.parse(arguments: ["--poll-seconds", "-7"]) == .run(pollSeconds: 1, openProtocol: .wake), "stream must clamp a negative poll interval up to 1")
        try require(AKP03HIDStreamCommand.parse(arguments: ["--poll-seconds", "9999"]) == .run(pollSeconds: 60, openProtocol: .wake), "stream must clamp a too-large poll interval down to 60")
        for arguments in [["--seconds", "5"], ["--poll-seconds"], ["--poll-seconds", "abc"], ["--poll-seconds", "1", "2"], ["extra"]] {
            do { _ = try AKP03HIDStreamCommand.parse(arguments: arguments); throw CheckFailure(message: "stream accepted invalid arguments: \(arguments)") }
            catch is AKP03HIDStreamArgumentError { continue }
        }

        // --open-protocol: exactly three accepted values, wake as the default, and
        // every invalid or missing value rejected.
        try require(AKP03HIDStreamCommand.defaultOpenProtocol == .wake, "the stream default open-protocol must be wake")
        try require(AKP03HIDStreamCommand.parse(arguments: ["--open-protocol", "wake"]) == .run(pollSeconds: 2, openProtocol: .wake), "stream must accept --open-protocol wake")
        try require(AKP03HIDStreamCommand.parse(arguments: ["--open-protocol", "full"]) == .run(pollSeconds: 2, openProtocol: .full), "stream must accept --open-protocol full")
        try require(AKP03HIDStreamCommand.parse(arguments: ["--open-protocol", "none"]) == .run(pollSeconds: 2, openProtocol: .none), "stream must accept --open-protocol none")
        try require(AKP03HIDStreamCommand.parse(arguments: ["--open-protocol", "none", "--poll-seconds", "9"]) == .run(pollSeconds: 9, openProtocol: .none), "options must be order-independent")
        for arguments in [["--open-protocol"], ["--open-protocol", "WAKE"], ["--open-protocol", "everything"], ["--open-protocol", "wake ", "--open-protocol", "full"]] {
            do { _ = try AKP03HIDStreamCommand.parse(arguments: arguments); throw CheckFailure(message: "stream accepted invalid open-protocol arguments: \(arguments)") }
            catch is AKP03HIDStreamArgumentError { continue }
        }

        // -- Only the five fixed packets are reachable, in every mode and order.
        try verifyOpenProtocolPacketSurface()

        try require(AKP03HIDStreamLine.starting == "{\"event\":\"starting\"}", "starting line changed")
        try require(AKP03HIDStreamLine.waiting == "{\"event\":\"waiting\"}", "waiting line changed")
        try require(AKP03HIDStreamLine.ready == "{\"event\":\"ready\"}", "ready line changed")
        try require(AKP03HIDStreamLine.deviceLost == "{\"event\":\"device-lost\"}", "device-lost line changed")
        try require(AKP03HIDStreamLine.unknown == "{\"event\":\"unknown\"}", "unknown line changed")
        try require(AKP03HIDStreamLine.control(control: .key0, pressed: true) == "{\"event\":\"control\",\"control\":\"key0\",\"state\":\"press\"}", "control press line changed")
        try require(AKP03HIDStreamLine.control(control: .key5, pressed: false) == "{\"event\":\"control\",\"control\":\"key5\",\"state\":\"release\"}", "control release line changed")
        try require(AKP03HIDStreamLine.control(control: .bottomLeftCounterClockwise, pressed: false) == "{\"event\":\"control\",\"control\":\"bottomLeftCounterClockwise\",\"state\":\"release\"}", "control name spelling must match the decoder")
        try require(AKP03HIDStreamLine.control(control: .topClockwise, pressed: nil) == "{\"event\":\"control\",\"control\":\"topClockwise\"}", "a stateless control must omit state rather than guess one")
        try require(AKP03HIDStreamLine.error(message: "x\"y") == #"{"event":"error","message":"x\"y"}"#, "error line must escape quotes")
        try require(AKP03HIDStreamLine.error(message: "a\nb") == #"{"event":"error","message":"a\nb"}"#, "error line must escape control characters")

        // The state now travels inside the single semantic event; there is no separate state decoder.
        try require(AKP03ControlEvent.unknownPacket.pressed == nil && AKP03ControlEvent.unknownPacket.knownControl == nil, "an unknown packet must carry no control or state")
        try require(AKP03ControlEvent.known(.key0, pressed: true).pressed == true && AKP03ControlEvent.known(.key0, pressed: true).knownControl == .key0, "a known event must expose its control and state together")
        try require(AKP03ControlEvent.known(.topClockwise, pressed: nil).pressed == nil, "a stateless known event must expose no press state")
    }

    private static func occurrences(of needle: String, in haystack: String) -> Int {
        haystack.components(separatedBy: needle).count - 1
    }

    /// Device-free verification of the `paint` command the daemon uses to put
    /// key labels, brightness and menu pages on the deck's own LCD keys: the
    /// fixed 1024-byte report rule, every refusal, the answer lines, and the
    /// fact that a payload can only ever come from this process's own stdin.
    /// It enumerates, opens and writes no HID hardware.
    private static func verifyHIDStreamPaintProtocol() throws {
        let packet = [UInt8](repeating: 0x41, count: AKP03HIDStreamRequest.payloadLength)
        let encoded = Data(packet).base64EncodedString()
        try require(AKP03HIDStreamRequest.payloadLength == 1024, "a paint payload must be exactly one 1024-byte report")
        try require(AKP03HIDStreamRequest.maximumLineCharacters == 8192, "the accepted command line must stay bounded")
        let accepted = try AKP03HIDStreamRequest.parse(line: "{\"command\":\"paint\",\"payload\":\"\(encoded)\"}")
        try require(accepted == .paint(payload: packet), "a 1024-byte base64 paint command must be accepted byte-for-byte")

        // -- Refusals: wrong length, non-base64, unknown command, missing fields, bad JSON.
        func base64Line(_ count: Int) -> String {
            "{\"command\":\"paint\",\"payload\":\"\(Data([UInt8](repeating: 0x41, count: count)).base64EncodedString())\"}"
        }
        let refusals: [(String, AKP03HIDStreamRequestError)] = [
            (base64Line(1023), .wrongLength(1023)),
            (base64Line(1025), .wrongLength(1025)),
            (base64Line(0), .wrongLength(0)),
            ("{\"command\":\"paint\",\"payload\":\"not base64!\"}", .invalidBase64),
            ("{\"command\":\"paint\"}", .missingField("payload")),
            ("{\"payload\":\"\(encoded)\"}", .missingField("command")),
            ("{\"command\":\"reboot\"}", .unknownCommand("reboot")),
            ("{\"command\":\"write\",\"payload\":\"\(encoded)\"}", .unknownCommand("write")),
            ("{\"command\":\"report\",\"payload\":\"\(encoded)\"}", .unknownCommand("report")),
            ("not json", .malformedLine),
            ("[1,2,3]", .malformedLine),
            (String(repeating: "a", count: AKP03HIDStreamRequest.maximumLineCharacters + 1), .lineTooLong(AKP03HIDStreamRequest.maximumLineCharacters + 1)),
        ]
        for (line, expected) in refusals {
            do {
                let parsed = try AKP03HIDStreamRequest.parse(line: line)
                throw CheckFailure(message: "a refused paint command was accepted: \(parsed) from \(line.prefix(40))")
            } catch let error as AKP03HIDStreamRequestError {
                try require(error == expected, "wrong refusal for \(line.prefix(40)): \(error) != \(expected)")
                try require(error.errorDescription?.isEmpty == false, "every refusal must carry a reason the daemon can log")
            }
        }

        // -- Answer lines: one acknowledgement per command, and a reason when refused.
        try require(AKP03HIDStreamLine.paintResult(bytes: 1024) == #"{"result":"ok","command":"paint","bytes":1024}"#, "paint acknowledgement line changed")
        try require(
            AKP03HIDStreamLine.paintError(reason: "paint payload must be exactly 1024 bytes (received 5)")
                == #"{"result":"error","command":"paint","reason":"paint payload must be exactly 1024 bytes (received 5)"}"#,
            "paint refusal line changed"
        )
        try require(AKP03HIDStreamLine.paintError(reason: "x\"y") == #"{"result":"error","command":"paint","reason":"x\"y"}"#, "a refused paint must escape its reason")
        try require(!AKP03HIDStreamLine.paintResult(bytes: 1024).contains("\n"), "an answer must be one newline-delimited line")

        // -- Source audit: the payload arrives only on stdin, never as an argument,
        //    and the one audited sink is still the only write path.
        let runner = try String(contentsOfFile: "Sources/AKP03HIDStream/main.swift", encoding: .utf8)
        let request = try String(contentsOfFile: "Sources/AKP03HIDStreamCore/AKP03HIDStreamRequest.swift", encoding: .utf8)
        let sink = try String(contentsOfFile: "Sources/AKP03IOKitInput/AKP03IOKitBootstrap.swift", encoding: .utf8)
        try require(runner.contains("STDIN_FILENO") && runner.contains("readStdin") && runner.contains("consumeStdinLines"), "paint commands must be read from the helper's own stdin")
        try require(runner.contains("sendPaintOutputReport"), "the runner must write through the one audited paint sink")
        // The CLI cannot carry bytes: an argument named like a payload is unknown.
        for arguments in [["--payload", encoded], ["--report", encoded], [encoded]] {
            do { _ = try AKP03HIDStreamCommand.parse(arguments: arguments); throw CheckFailure(message: "the stream CLI accepted a byte-carrying argument: \(arguments.first ?? "")") }
            catch is AKP03HIDStreamArgumentError { continue }
        }
        try require(!request.contains("URLSession") && !request.contains("Socket") && !request.contains("FileManager"), "a paint payload must never arrive over the network or from a file")
        try require(sink.contains("public static let payloadLength = 1024") && sink.contains("guard payload.count == payloadLength"), "the sink must refuse anything but one 1024-byte report before writing")
        try require(sink.contains("public static func paint(") && sink.contains("invalidOutputPayload"), "a painting payload must be length-checked and fail loudly")
    }

    /// Device-free verification of the `AKP03KeyInject` helper: the keycode
    /// table's accept/reject boundary, chord and command parsing, the hold/release
    /// ledger (duplicate hold, unmatched release), the dry-run result lines, and a
    /// source-level audit that the injector exposes no mouse, scroll, event-tap,
    /// shell, or text-injection capability. It creates no `CGEvent` and posts
    /// nothing.
    private static func verifyKeyInjectSurface() throws {
        // -- The keycode table is closed and exact.
        try require(AKP03KeyTable.supportedTokenCount == 67, "the keycode table must expose exactly 67 canonical tokens")
        try require(AKP03KeyTable.lookup("cmd")?.modifierFlag == .maskCommand && AKP03KeyTable.lookup("cmd")?.keyCode == 0x37, "cmd must map to the Command keycode and flag")
        try require(AKP03KeyTable.lookup("ctrl")?.modifierFlag == .maskControl, "ctrl must map to the Control flag")
        try require(AKP03KeyTable.lookup("shift")?.modifierFlag == .maskShift, "shift must map to the Shift flag")
        try require(AKP03KeyTable.lookup("alt")?.modifierFlag == .maskAlternate, "alt must map to the Option flag")
        try require(AKP03KeyTable.lookup("enter")?.keyCode == 0x24 && AKP03KeyTable.lookup("enter")?.isModifier == false, "enter must map to a non-modifier keycode")
        try require(AKP03KeyTable.lookup("space")?.keyCode == 0x31, "space keycode changed")
        try require(AKP03KeyTable.lookup("a")?.keyCode == 0x00, "letter a keycode changed")
        try require(AKP03KeyTable.lookup("f12")?.keyCode == 0x6F, "function-key keycode changed")
        for rejected in ["semi;colon", "f13", "cmd+", "meta", "", "return", "ESC", "cmd+z"] {
            try require(AKP03KeyTable.lookup(rejected) == nil, "the table must reject the token \(rejected)")
        }

        // -- Chord parsing: normalisation, rejection, duplicates.
        let undo = try AKP03KeyChord.parse("cmd+z")
        try require(undo.rendered == "cmd+z" && undo.pressSteps.map(\.token) == ["cmd", "z"], "cmd+z must normalise modifiers first")
        try require(undo.pressedDescription == ["keyDown cmd", "keyDown z"] && undo.releasedDescription == ["keyUp z", "keyUp cmd"], "tap chord press/release order changed")
        let talk = try AKP03KeyChord.parse("ctrl+shift+space")
        try require(talk.rendered == "ctrl+shift+space" && talk.pressedDescription == ["keyDown ctrl", "keyDown shift", "keyDown space"] && talk.releasedDescription == ["keyUp space", "keyUp shift", "keyUp ctrl"], "hold chord order changed")
        try require(try AKP03KeyChord.parse("space+ctrl").rendered == "ctrl+space", "a trailing modifier must be reordered to the front")
        try require(try AKP03KeyChord.parse("ctrl+shift").isModifierOnly, "a modifier-only chord must report itself as such")
        for bad in ["ctrl+", "semi;colon", "cmd+z+z", ""] {
            do { _ = try AKP03KeyChord.parse(bad); throw CheckFailure(message: "chord parser accepted \(bad)") }
            catch is AKP03KeyChordError { continue }
        }

        // -- Command parsing.
        try require(try AKP03KeyInjectCommand.parse("ping") == .ping && AKP03KeyInjectCommand.parse("quit") == .quit, "ping/quit must parse")
        try require(try AKP03KeyInjectCommand.parse("tap cmd+z") == .tap(undo), "tap must parse")
        try require(try AKP03KeyInjectCommand.parse("hold ctrl+shift+space") == .hold(talk), "hold must parse")
        try require(try AKP03KeyInjectCommand.parse("release ctrl+shift+space") == .release(talk), "release must parse")
        for bad in ["tap", "tap cmd+z extra", "hold", "release", "wiggle cmd+z", "tap ctrl", "ping now"] {
            do { _ = try AKP03KeyInjectCommand.parse(bad); throw CheckFailure(message: "command parser accepted \(bad)") }
            catch is AKP03KeyInjectCommandError { continue }
        }

        // -- The media keys: the six functions, and the exact event layout.
        try require(AKP03MediaKey.allCases.count == 6, "there are exactly six media keys")
        for key in AKP03MediaKey.allCases {
            try require(try AKP03KeyInjectCommand.parse("media \(key.rawValue)") == .media(key), "media \(key.rawValue) must parse")
            try require(AKP03KeyInjectCommand.parse("media \(key.rawValue.uppercased())") == .media(key), "a media name must be case-insensitive")
            // The code and the direction live in one field: code high, NX_KEYDOWN
            // / NX_KEYUP low. A press and its release must differ only there.
            let down = key.eventData1(down: true), up = key.eventData1(down: false)
            try require(down >> 16 == Int(key.keyType) && up >> 16 == Int(key.keyType), "media \(key.rawValue) must carry its NX_KEYTYPE in the high half")
            try require(down & 0xFFFF == 0x0A00 && up & 0xFFFF == 0x0B00, "media \(key.rawValue) must use NX_KEYDOWN/NX_KEYUP in the low half")
            try require(key.postDescription == ["systemDefined \(key.rawValue) down", "systemDefined \(key.rawValue) up"], "media \(key.rawValue) dry-run description changed")
        }
        // The values are the system's own (`IOKit/hidsystem/ev_keymap.h`), pinned
        // here so a plausible-looking edit to one of them fails verification.
        try require(AKP03MediaKey.soundUp.keyType == 0 && AKP03MediaKey.soundDown.keyType == 1, "the volume codes must match ev_keymap.h")
        try require(AKP03MediaKey.mute.keyType == 7 && AKP03MediaKey.playPause.keyType == 16, "the mute and play codes must match ev_keymap.h")
        try require(AKP03MediaKey.next.keyType == 17 && AKP03MediaKey.previous.keyType == 18, "the track codes must match ev_keymap.h")
        // They are not keycodes. This is the whole reason the verb exists: F12 is
        // a function key that macOS only reads as "louder" in front of a volume
        // panel, and no token of a media name may reach the chord parser.
        for name in AKP03MediaKey.allCases.map(\.rawValue) {
            try require(AKP03KeyTable.lookup(name) == nil, "\(name) must not be a keycode token")
        }
        for bad in ["media", "media volume-up", "media f12", "media sound-up extra"] {
            do { _ = try AKP03KeyInjectCommand.parse(bad); throw CheckFailure(message: "command parser accepted \(bad)") }
            catch is AKP03KeyInjectCommandError { continue }
        }
        // A media key cannot be held, and that falls out of the vocabulary rather
        // than being special-cased: no media name is a chord token.
        for bad in ["hold sound-up", "tap sound-up", "release mute"] {
            do { _ = try AKP03KeyInjectCommand.parse(bad); throw CheckFailure(message: "command parser accepted \(bad)") }
            catch is AKP03KeyChordError { continue }
        }

        // -- The media event is the system-defined one, built in one place.
        let main = try String(contentsOfFile: "Sources/AKP03KeyInject/main.swift", encoding: .utf8)
        try require(main.contains("NSEvent.otherEvent(") && main.contains("with: .systemDefined") && main.contains("subtype: 8"), "a media key must be posted as a system-defined event")
        try require(main.contains("key.eventData1(down: down)") && main.contains("event.post(tap: .cghidEventTap)"), "the media event must carry data1 and reach the HID tap")
        try require(main.contains("guard accessibilityTrusted()") , "media posting must fail closed without Accessibility")

        // -- Hold/release ledger: the whole stuck-key defence.
        var ledger = AKP03KeyLedger()
        try require(ledger.hold(talk) == .held(talk) && ledger.held == talk, "the first hold must be recorded")
        try require(ledger.hold(talk) == .alreadyHeld(talk), "a duplicate hold must be refused")
        try require(ledger.release() == .released(talk) && ledger.held == nil, "a release must release what was held")
        try require(ledger.release() == .nothingHeld, "an unmatched release must be refused")
        _ = ledger.hold(undo)
        try require(ledger.hold(talk) == .alreadyHeld(undo), "a second, different hold must be refused while one is held")
        try require(ledger.releaseAll() == undo && ledger.held == nil && ledger.releaseAll() == nil, "releaseAll must release everything exactly once")

        // -- Result line protocol, byte for byte. The Python side parses these.
        try require(AKP03KeyInjectLine.ok(command: "tap", chord: "cmd+z") == #"{"result":"ok","command":"tap","chord":"cmd+z"}"#, "ok line changed")
        try require(AKP03KeyInjectLine.ok(command: "quit", chord: nil) == #"{"result":"ok","command":"quit"}"#, "quit ok line changed")
        try require(
            AKP03KeyInjectLine.dryRun(command: "tap", chord: "cmd+z", wouldPost: undo.pressedDescription + undo.releasedDescription)
                == #"{"result":"dry-run","command":"tap","chord":"cmd+z","would_post":["keyDown cmd","keyDown z","keyUp z","keyUp cmd"]}"#,
            "dry-run line changed"
        )
        try require(AKP03KeyInjectLine.error(command: "tap", reason: "unsupported key: 'semi;colon'") == #"{"result":"error","command":"tap","reason":"unsupported key: 'semi;colon'"}"#, "error line changed")
        try require(AKP03KeyInjectLine.error(command: nil, reason: #"x"y"#) == #"{"result":"error","reason":"x\"y"}"#, "error line must escape quotes")
        try require(AKP03KeyInjectLine.pong(accessibility: true, dryRun: false) == #"{"result":"pong","accessibility":true,"dry_run":false}"#, "pong line changed")
        try require(AKP03KeyInjectLine.accessibilityReason == "accessibility permission not granted", "the accessibility reason text is the Python contract")

        try verifyKeyInjectSourceAllowlist()
    }

    /// Source-level audit of `AKP03KeyInject`. Exactly two event constructors and
    /// two post calls are permitted — one keyboard event for a chord, one
    /// system-defined event for a media key — and no mouse, scroll, click,
    /// event-tap/monitoring, shell, or text-injection API may appear anywhere in
    /// the executable or its core, so adding one makes this check fail.
    ///
    /// `NSEvent` moved from the forbidden list to a count of exactly one, and the
    /// reason is worth keeping: a media key has no keycode, so the system-defined
    /// event macOS listens for is built with `NSEvent.otherEvent(with:
    /// .systemDefined, …)`. That is a constructor for one narrow event, not a
    /// capability, and it is the only AppKit call the helper makes — asserting the
    /// count keeps it that way rather than reopening the door.
    private static func verifyKeyInjectSourceAllowlist() throws {
        let executableSource = try String(contentsOfFile: "Sources/AKP03KeyInject/main.swift", encoding: .utf8)
        let coreDirectory = URL(fileURLWithPath: "Sources/AKP03KeyInjectCore", isDirectory: true)
        let coreFiles = try FileManager.default.contentsOfDirectory(at: coreDirectory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
        let coreSource = try coreFiles.map { try String(contentsOf: $0, encoding: .utf8) }.joined(separator: "\n")
        let allSource = executableSource + "\n" + coreSource

        try require(coreFiles.count >= 5, "the key-injection core must keep its table, chord, command, ledger, and line sources")
        try require(occurrences(of: "CGEvent(keyboardEventSource:", in: allSource) == 1, "the injector must contain exactly one keyboard-event constructor")
        try require(occurrences(of: "NSEvent.otherEvent(", in: allSource) == 1, "the injector must contain exactly one media-event constructor")
        // AppKit appears exactly once, for that constructor. Anything else from
        // AppKit is a capability this helper does not need.
        try require(occurrences(of: "NSEvent", in: allSource) == 1, "the injector may use NSEvent only for the one media-event constructor")
        // Two posts and only two: the keyboard event from `postKey`, the
        // system-defined event from `postMedia`. A third is a capability nobody
        // asked for, which is the whole point of counting them.
        try require(occurrences(of: ".post(tap:", in: allSource) == 2, "the injector must post from exactly two places: a key and a media key")
        try require(executableSource.contains(".cghidEventTap"), "events must be posted to the HID event tap")
        try require(executableSource.contains("AXIsProcessTrustedWithOptions("), "the injector must report Accessibility status")
        try require(
            executableSource.contains("let source = CGEventSource(stateID: .hidSystemState)"),
            "events must come from the HID system event source: with a nil source macOS ignores them for its own hotkeys"
        )
        try require(
            executableSource.contains("CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: down)"),
            "keyboard events must be built from the fixed table only, and from that source"
        )
        try require(
            executableSource.contains("if step.isModifier { usleep(Self.modifierGapMicroseconds) }"),
            "a modifier must be given a gap before the key it modifies, or the key can arrive first"
        )

        for forbidden in [
            "MouseEvent", "ScrollWheel", "CGEventCreateMouseEvent", "CGEventCreateScrollWheelEvent",
            "CGEventTapCreate", "addGlobalMonitorForEvents", "addLocalMonitorForEvents",
            "CGEventKeyboardSetUnicodeString", "Process(", "NSTask", "system(", "popen",
            "NSAppleScript", "NSWorkspace", "openURL", "URLSession",
        ] {
            try require(!allSource.contains(forbidden), "forbidden capability present: \(forbidden)")
        }
    }
    // MARK: - Launch command validation

    /// Device-free verification of the development-mode daemon launch command:
    /// sensible defaults, override precedence, and *rejection* of anything that
    /// is not a fixed argv (never a shell string).
    private static func verifyLaunchConfiguration() throws {
        let defaults = try AKP03LaunchConfigurationResolver.resolve(
            arguments: [],
            discoveredRepoRoot: "/repo",
            fileExists: { _ in false }
        )
        try require(defaults.executable == "/repo/.venv/bin/python", "default executable must be the repository venv")
        try require(defaults.arguments == ["-m", "akp03.daemon", "--editor-port", "17865"], "default daemon arguments changed")
        try require(defaults.workingDirectory == "/repo", "default working directory must be the repo root")
        try require(defaults.isDevelopmentMode, "the launch must be labelled development mode")

        // --config / --socket are added only when supplied.
        let withPaths = try AKP03LaunchConfigurationResolver.resolve(
            arguments: ["--config", "/tmp/c.toml", "--socket", "/tmp/s.sock"],
            discoveredRepoRoot: "/repo",
            fileExists: { _ in false }
        )
        try require(
            withPaths.arguments == ["-m", "akp03.daemon", "--config", "/tmp/c.toml", "--socket", "/tmp/s.sock", "--editor-port", "17865"],
            "config and socket must be forwarded only when given"
        )

        // --daemon-command with --daemon-arg fully specifies a custom argv.
        let custom = try AKP03LaunchConfigurationResolver.resolve(
            arguments: ["--daemon-command", "/opt/stub.sh", "--daemon-arg", "a", "--daemon-arg", "b"],
            discoveredRepoRoot: "/repo",
            fileExists: { _ in false }
        )
        try require(custom.executable == "/opt/stub.sh" && custom.arguments == ["a", "b"], "a custom command must receive exactly its argv")

        // Helper paths default from the repo, but only when the built helper exists.
        let enriched = try AKP03LaunchConfigurationResolver.resolve(
            arguments: [],
            discoveredRepoRoot: "/repo",
            fileExists: { $0.hasSuffix("AKP03KeyInject") }
        )
        try require(enriched.environment["AKP03_KEYINJECT"] == "/repo/macos-menubar/.build/debug/AKP03KeyInject", "the key-injection helper default must point at the built binary")
        try require(enriched.environment["AKP03_HIDSTREAM"] == nil, "a helper that was not built must not be pointed at")
        try require(enriched.environment["PYTHONPATH"] == "/repo", "the child must be able to import the repository")

        // An explicit helper override wins and is validated.
        let overridden = try AKP03LaunchConfigurationResolver.resolve(
            arguments: ["--hidstream", "/custom/AKP03HIDStream", "--keyinject", "/custom/AKP03KeyInject"],
            discoveredRepoRoot: "/repo",
            fileExists: { _ in false }
        )
        try require(overridden.environment["AKP03_HIDSTREAM"] == "/custom/AKP03HIDStream" && overridden.environment["AKP03_KEYINJECT"] == "/custom/AKP03KeyInject", "explicit helper paths must win")

        // Everything malformed is rejected before use.
        for arguments in [
            ["--daemon-command", "py thon"],
            ["--daemon-command", "a;b"],
            ["--daemon-command", ""],
            ["--daemon-arg", "a\u{0}b"],
            ["--editor-port", "70000"],
            ["--editor-port", "-1"],
            ["--editor-port", "x"],
            ["--repo-root"],
            ["--hidstream", "a b"],
            ["--unexpected"],
        ] {
            do {
                _ = try AKP03LaunchConfigurationResolver.resolve(arguments: arguments, discoveredRepoRoot: "/repo", fileExists: { _ in false })
                throw CheckFailure(message: "launch configuration accepted invalid arguments: \(arguments)")
            } catch is LaunchConfigurationError { }
        }

        // Nothing to launch without a repository or an explicit command.
        do {
            _ = try AKP03LaunchConfigurationResolver.resolve(arguments: [], discoveredRepoRoot: nil, fileExists: { _ in false })
            throw CheckFailure(message: "launch configuration produced a command with no Python environment")
        } catch is LaunchConfigurationError { }

        // A self-contained bundle is found from its own path, and its payload
        // replaces the repository entirely - this is the shipping case.
        let bundlePath = "/Applications/AKP03.app/Contents/MacOS/AKP03"
        try require(
            BundledPayload.discover(executablePath: bundlePath, fileExists: { _ in false }) == nil,
            "a bundle with no runtime must not be mistaken for a payload"
        )
        let payload = BundledPayload.discover(executablePath: bundlePath, fileExists: { $0.hasSuffix("/python/bin/python3") })
        try require(payload?.resourcesRoot == "/Applications/AKP03.app/Contents/Resources", "bundle resources must sit beside Contents/MacOS")
        try require(
            BundledPayload.discover(executablePath: "/usr/local/bin/AKP03", fileExists: { _ in true }) == nil,
            "a loose binary outside a bundle is not a payload"
        )

        let installed = try AKP03LaunchConfigurationResolver.resolve(
            arguments: [],
            discoveredRepoRoot: nil,
            discoveredBundle: payload,
            fileExists: { path in path == payload?.pythonExecutable || path == payload?.hidStream || path == payload?.keyInject }
        )
        try require(
            installed.executable == "/Applications/AKP03.app/Contents/Resources/python/bin/python3",
            "an installed app must launch its own bundled runtime"
        )
        try require(installed.arguments == ["-m", "akp03.daemon", "--editor-port", "17865"], "bundled daemon arguments changed")
        try require(
            installed.environment["PYTHONPATH"] == "/Applications/AKP03.app/Contents/Resources/app",
            "the bundled package root must be on PYTHONPATH"
        )
        try require(installed.environment["AKP03_HIDSTREAM"]?.hasSuffix("/bin/AKP03HIDStream") == true, "the bundled HID helper must be used")
        try require(installed.environment["AKP03_KEYINJECT"]?.hasSuffix("/bin/AKP03KeyInject") == true, "the bundled key helper must be used")
        try require(installed.workingDirectory == "/Applications/AKP03.app/Contents/Resources/app", "the child must work inside the bundle")
        try require(!installed.isDevelopmentMode, "a bundled launch must not be labelled development mode")
        try require(
            installed.environment["PYTHONDONTWRITEBYTECODE"] == "1",
            "a bundled launch must not write bytecode into its own sealed bundle"
        )

        // An explicit command still beats the bundle, and a bundle without one
        // of its helpers must not point at a path that does not exist.
        let explicitOverBundle = try AKP03LaunchConfigurationResolver.resolve(
            arguments: ["--daemon-command", "/opt/other/python", "--daemon-arg", "-m", "--daemon-arg", "akp03.daemon"],
            discoveredRepoRoot: nil,
            discoveredBundle: payload,
            fileExists: { _ in false }
        )
        try require(explicitOverBundle.executable == "/opt/other/python", "an explicit command must win over the bundled runtime")
        let partialBundle = try AKP03LaunchConfigurationResolver.resolve(
            arguments: [],
            discoveredRepoRoot: nil,
            discoveredBundle: payload,
            fileExists: { path in path.hasSuffix("/python/bin/python3") }
        )
        try require(partialBundle.environment["AKP03_HIDSTREAM"] == nil, "a helper that is not in the bundle must not be pointed at")

        // The displayed command quotes spaces so the menu cannot mislead.
        let spaced = AKP03LaunchConfiguration(executable: "/a b/python", arguments: ["-m", "akp03.daemon"])
        try require(spaced.displayCommand == "\"/a b/python\" -m akp03.daemon", "display command must quote paths with spaces")
    }

    private static func verifyCompanionArguments() throws {
        let parsed = try AKP03CompanionArguments.parse(
            arguments: ["--url", "http://127.0.0.1:17890/"],
            discoveredRepoRoot: "/repo",
            fileExists: { _ in false }
        )
        try require(parsed.endpoint.healthURL.absoluteString == "http://127.0.0.1:17890/api/state", "health URL changed")
        try require(parsed.endpoint.eventsURL.absoluteString == "http://127.0.0.1:17890/api/events", "events URL must be derived from the editor URL")
        try require(parsed.launch?.arguments.contains("17890") == true, "the daemon's --editor-port must follow the probed URL")

        // A launch problem is reported, not thrown, so the app still runs.
        let noEnvironment = try AKP03CompanionArguments.parse(arguments: [], discoveredRepoRoot: nil, fileExists: { _ in false })
        try require(noEnvironment.launch == nil && noEnvironment.launchError != nil, "a missing Python environment must be surfaced, not fatal")

        // An endpoint problem (and an unknown flag) is still fatal.
        for arguments in [["--unexpected"], ["--url", "https://example.com/"], ["--url", "http://192.0.2.10/"]] {
            do {
                _ = try AKP03CompanionArguments.parse(arguments: arguments, discoveredRepoRoot: "/repo", fileExists: { _ in false })
                throw CheckFailure(message: "companion arguments accepted invalid input: \(arguments)")
            } catch is EndpointError { }
        }
    }

    // MARK: - Supervision policy

    private static func verifySupervisionPolicy() throws {
        let policy = AKP03SupervisionPolicy.standard
        try require(policy.backoff == [0.5, 1, 2, 4, 8, 15, 30], "the standard backoff changed")
        try require(policy.delay(forFailures: 0) == 0.5 && policy.delay(forFailures: 3) == 4, "backoff must escalate with failures")
        try require(policy.delay(forFailures: 99) == 30, "backoff must clamp at the last delay")

        var tracker = AKP03RestartTracker(policy: policy)
        try require(tracker.recordExit(uptime: 0.1) == .restart(after: 0.5, rapidExits: 1), "the first rapid exit must restart after 0.5s")
        try require(tracker.recordExit(uptime: 0.1) == .restart(after: 1.0, rapidExits: 2), "the second rapid exit must back off further")
        _ = tracker.recordExit(uptime: 0.1)
        _ = tracker.recordExit(uptime: 0.1)
        _ = tracker.recordExit(uptime: 0.1)
        try require(tracker.consecutiveRapidExits == 5, "five rapid exits must be counted")
        try require(tracker.recordExit(uptime: 0.1) == .giveUp(rapidExits: 6), "a rapid-crash loop must be capped, never spun")

        // A run that stayed up resets the escalation.
        var recovered = AKP03RestartTracker(policy: policy)
        _ = recovered.recordExit(uptime: 0.1)
        _ = recovered.recordExit(uptime: 0.1)
        try require(recovered.recordExit(uptime: 30) == .restart(after: 0.5, rapidExits: 0), "a healthy run must reset the backoff")
        try require(recovered.failures == 0 && recovered.consecutiveRapidExits == 0, "health must clear the counters")
    }

    // MARK: - Status wording

    private static func verifyStatusWording() throws {
        let ready = AKP03DeckStatus(connected: true, phase: .ready)
        let waiting = AKP03DeckStatus(connected: false, phase: .waiting)
        let lost = AKP03DeckStatus(connected: false, phase: .lost)
        let starting = AKP03DeckStatus(connected: false, phase: .starting)
        let blocked = AKP03DeckStatus(connected: false, phase: .blocked)

        try require(AKP03DeckStatusWording.detail(nil).contains("not getting a live feed"), "an un-reported deck must say so")
        try require(AKP03DeckStatusWording.detail(ready) == "Deck: connected and working.", "ready wording changed")
        try require(AKP03DeckStatusWording.detail(waiting).contains("no AKP03 is plugged in"), "waiting must name the unplugged deck")
        try require(AKP03DeckStatusWording.detail(lost).contains("disconnected"), "lost must name the disconnection")
        try require(AKP03DeckStatusWording.detail(starting).contains("starting"), "starting wording changed")
        // A deck that is present and silent must never be reported as absent: that is the
        // sentence that sends someone looking for a cable when the cable is fine.
        try require(AKP03DeckStatusWording.detail(blocked).contains("plugged in"), "blocked must say the deck is attached")
        try require(!AKP03DeckStatusWording.detail(blocked).contains("no AKP03 is plugged in"), "blocked must not claim the deck is absent")

        // The five deck facts, plus "no feed", must all read differently.
        let phrases = Set(
            [nil, ready, waiting, lost, starting, blocked].map { AKP03DeckStatusWording.detail($0) }
        )
        try require(phrases.count == 6, "each deck state must read differently")

        // Decoding is strict: a missing or unknown phase is rejected, never guessed.
        try require(AKP03DeckStatus(snapshot: ["connected": true, "phase": "ready"]) == ready, "a state frame must decode")
        try require(AKP03DeckStatus(snapshot: ["connected": true]) == nil, "a phase-less frame must be rejected")
        try require(AKP03DeckStatus(snapshot: ["connected": true, "phase": "bogus"]) == nil, "an unknown phase must be rejected")

        // The menu-bar title names the important fact first.
        try require(AKP03MenuPresentation.title(service: .reachable, deck: ready, supervision: .running(pid: 1)) == "AKP03: Ready", "ready title changed")
        try require(AKP03MenuPresentation.title(service: .reachable, deck: waiting, supervision: .running(pid: 1)) == "AKP03: No deck", "waiting title changed")
        try require(AKP03MenuPresentation.title(service: .reachable, deck: lost, supervision: .running(pid: 1)) == "AKP03: Deck lost", "lost title changed")
        try require(AKP03MenuPresentation.title(service: .reachable, deck: nil, supervision: .running(pid: 1)) == "AKP03: Idle", "idle title changed")
        try require(AKP03MenuPresentation.title(service: .unreachable, deck: nil, supervision: .idle) == "AKP03: Offline", "offline title changed")
        try require(AKP03MenuPresentation.title(service: .checking, deck: nil, supervision: .idle) == "AKP03: Checking…", "checking title changed")
        try require(AKP03MenuPresentation.title(service: .unreachable, deck: nil, supervision: .gaveUp(afterAttempts: 6)) == "AKP03: Stopped", "a stopped service must be unmistakable")
        try require(AKP03MenuPresentation.symbolName(service: .reachable, deck: ready, supervision: .running(pid: 1)) == "slider.horizontal.3", "ready symbol changed")
        try require(AKP03MenuPresentation.symbolName(service: .unreachable, deck: nil, supervision: .idle) == "exclamationmark.triangle", "offline symbol changed")
    }

    // MARK: - Daemon failure diagnostics

    /// A supervised daemon that fails to start must be explained with its own
    /// last log line, never a vague state or a guess. Device-free: it only reads
    /// a temporary log file this check writes.
    private static func verifyDaemonDiagnostics() throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("akp03-log-\(getpid())")
        try? FileManager.default.removeItem(at: directory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let log = directory.appendingPathComponent("daemon.log")
        try "first line\nsecond line\nconfiguration rejected: invalid config: boom\n\n".write(to: log, atomically: true, encoding: .utf8)

        // The reader returns the daemon's own last non-empty line, verbatim.
        try require(AKP03DaemonLog.lastLine(at: log) == "configuration rejected: invalid config: boom", "the daemon log reader must return the last non-empty line")
        try require(AKP03DaemonLog.lastLine(at: nil) == nil, "no URL must read as no message")
        try require(AKP03DaemonLog.lastLine(at: directory.appendingPathComponent("absent.log")) == nil, "a missing log must read as no message")

        // A gave-up daemon names the cause using that line, not a vague state.
        let stopped = AKP03MenuPresentation.serviceDetail(
            service: .unreachable, ownershipIsManaged: true, hasLaunchCommand: true,
            supervision: .gaveUp(afterAttempts: 6),
            lastLogLine: AKP03DaemonLog.lastLine(at: log)
        )
        try require(stopped.contains("could not start"), "a failed daemon must say it could not start: \(stopped)")
        try require(stopped.contains("configuration rejected: invalid config: boom"), "the failure line must quote the daemon's own last log line: \(stopped)")
        try require(stopped.contains("Last message from the daemon:"), "the failure line must introduce the daemon message plainly: \(stopped)")

        // With no log line it still reads honestly — it never invents a reason.
        let noReason = AKP03MenuPresentation.serviceDetail(
            service: .unreachable, ownershipIsManaged: true, hasLaunchCommand: true,
            supervision: .gaveUp(afterAttempts: 3)
        )
        try require(noReason.contains("could not start") && !noReason.contains("Last message"), "a failure without a log line must not invent one: \(noReason)")

        // An immediate exit under supervision names the restart, with the reason.
        let restarting = AKP03MenuPresentation.serviceDetail(
            service: .unreachable, ownershipIsManaged: true, hasLaunchCommand: true,
            supervision: .restarting(after: 0.5, attempt: 1),
            lastLogLine: "configuration rejected: cannot read config"
        )
        try require(restarting.contains("exited unexpectedly") && restarting.contains("restarting in 0.5s"), "an immediate exit must name the restart: \(restarting)")
        try require(restarting.contains("configuration rejected: cannot read config"), "the restart line must quote the daemon's last log line: \(restarting)")

        // A healthy service's ownership wording is unchanged by any of this.
        try require(AKP03MenuPresentation.serviceDetail(service: .reachable, ownershipIsManaged: true, hasLaunchCommand: true, supervision: .running(pid: 1)) == "Service: running under this app.", "managed-service wording changed")
        try require(AKP03DeckStatusWording.detail(AKP03DeckStatus(connected: true, phase: .ready)) == "Deck: connected and working.", "ready wording must be unchanged")
    }

    private static func verifyPermissionsAndEventStreamParsing() throws {
        // The two facts the menu must separate: service reachability and deck state.
        let granted = AKP03PermissionReport(inputMonitoring: .granted, accessibility: .granted)
        try require(granted.summary == nil && !granted.hasMissingPermission, "a fully-granted app must raise nothing")
        let noInput = AKP03PermissionReport(inputMonitoring: .denied, accessibility: .granted)
        let noAccessibility = AKP03PermissionReport(inputMonitoring: .granted, accessibility: .denied)
        try require(noInput.summary?.contains("Input Monitoring") == true, "a missing Input Monitoring grant must name itself")
        try require(noInput.summary?.contains("System Settings") == true, "the Input Monitoring line must say where to grant it")
        try require(noAccessibility.summary?.contains("Accessibility") == true, "a missing Accessibility grant must name itself")
        try require(noInput.summary != noAccessibility.summary, "each missing permission must read differently")
        try require(AKP03PermissionReport(inputMonitoring: .unknown, accessibility: .granted).summary?.contains("could not be determined") == true, "an undetermined permission must be reported honestly")

        // SSE parsing: comments are ignored, a frame dispatches on the blank line.
        var parser = AKP03ServerSentEventParser()
        try require(parser.consume(line: ": keepalive") == nil, "an SSE comment must dispatch nothing")
        try require(parser.consume(line: "event: state") == nil, "the event field alone must not dispatch")
        try require(parser.consume(line: #"data: {"connected":true,"phase":"ready"}"#) == nil, "data alone must not dispatch")
        try require(
            parser.consume(line: "") == AKP03ServerSentEvent(event: "state", data: #"{"connected":true,"phase":"ready"}"#),
            "a blank line must dispatch the frame"
        )
        try require(parser.consume(line: "") == nil, "a second blank line must dispatch nothing")
        var multi = AKP03ServerSentEventParser()
        _ = multi.consume(line: "event: x")
        _ = multi.consume(line: "data: a")
        _ = multi.consume(line: "data: b")
        try require(multi.consume(line: "") == AKP03ServerSentEvent(event: "x", data: "a\nb"), "multi-line data must join")

        // A whole frame, including CRLF endings, must dispatch exactly once. The
        // strings are built from bytes so no source escaping is involved.
        let lf = String(decoding: [10] as [UInt8], as: UTF8.self)
        let crlf = String(decoding: [13, 10] as [UInt8], as: UTF8.self)
        let waitingPayload = #"{"connected":false,"phase":"waiting"}"#
        var lfFramed = AKP03ServerSentEventParser()
        try require(
            lfFramed.consume(frame: "event: state" + lf + "data: " + waitingPayload) == AKP03ServerSentEvent(event: "state", data: waitingPayload),
            "an LF-terminated frame must dispatch"
        )
        var framed = AKP03ServerSentEventParser()
        try require(
            framed.consume(frame: "event: state" + crlf + "data: " + waitingPayload) == AKP03ServerSentEvent(event: "state", data: waitingPayload),
            "a CRLF-terminated frame must dispatch"
        )
        try require(framed.consume(frame: ": keepalive") == nil, "a comment-only frame must dispatch nothing")
    }

    // MARK: - Live supervision (real processes; --live-supervision only)

    /// Runs the real supervisor against stub scripts. Not part of the default
    /// gate; it spawns processes and prints a transcript as evidence.
    private static func runLiveSupervision() throws {
        let fileManager = FileManager.default
        let base = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("akp03-live-\(getpid())")
        try fileManager.createDirectory(at: base, withIntermediateDirectories: true)
        defer { try? fileManager.removeItem(at: base) }

        let exitStub = base.appendingPathComponent("stub-exit.sh")
        try "#!/bin/sh\necho stub-exit starting\nexit 0\n".write(to: exitStub, atomically: true, encoding: .utf8)
        let sleepStub = base.appendingPathComponent("stub-sleep.sh")
        try "#!/bin/sh\necho \"stub-sleep starting pid=$$\"\ntrap 'echo \"stub-sleep got SIGTERM\"; exit 0' TERM\nwhile true; do sleep 1; done\n"
            .write(to: sleepStub, atomically: true, encoding: .utf8)
        for stub in [exitStub, sleepStub] {
            try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: stub.path)
        }
        print("stub scripts: \(exitStub.path), \(sleepStub.path)")

        func configuration(for executable: URL) throws -> AKP03LaunchConfiguration {
            try AKP03LaunchConfigurationResolver.resolve(
                arguments: ["--daemon-command", executable.path, "--daemon-arg", "stub"],
                discoveredRepoRoot: base.path,
                fileExists: { _ in false }
            )
        }

        // 1. Crash loop: an immediately-exiting stub must restart with a rising
        //    backoff and then be capped — never spun.
        let policy = AKP03SupervisionPolicy(backoff: [0.3, 0.6, 1.2], rapidExitThreshold: 5, maxRapidRestarts: 3)
        let crashConfig = try configuration(for: exitStub)
        print("crash-loop command: \(crashConfig.displayCommand)")
        let crash = AKP03DaemonSupervisor(configuration: crashConfig, policy: policy)
        let transcript = SupervisionTranscript()
        crash.setStateHandler { state in transcript.append(state) }
        let started = Date()
        crash.start()
        let capDeadline = Date().addingTimeInterval(6)
        while Date() < capDeadline {
            if case .gaveUp = crash.currentState { break }
            usleep(50_000)
        }
        print("--- crash loop transcript ---")
        var previous = started
        let recorded = transcript.snapshot()
        for (time, state) in recorded {
            print(String(format: "  +%.2fs (delta %.2fs)  %@", time.timeIntervalSince(started), time.timeIntervalSince(previous), describe(state)))
            previous = time
        }
        guard case let .gaveUp(attempts) = crash.currentState else {
            throw CheckFailure(message: "a rapid crash loop was not capped (state \(crash.currentState))")
        }
        print("capped after \(attempts) rapid restarts; supervisor state = gaveUp")
        let crashedPIDs = pgrep("stub-exit.sh")
        print("pgrep stub-exit.sh after cap: \(crashedPIDs)")
        try require(crashedPIDs.isEmpty, "a capped crash loop must leave no stub running")

        // 2. Clean stop: a long-running stub must be stopped, leaving nothing behind.
        let sleepConfig = try configuration(for: sleepStub)
        print("stop-test command: \(sleepConfig.displayCommand)")
        let daemon = AKP03DaemonSupervisor(configuration: sleepConfig, policy: .standard)
        daemon.start()
        let runDeadline = Date().addingTimeInterval(3)
        while Date() < runDeadline, daemon.runningPID == nil { usleep(50_000) }
        guard let pid = daemon.runningPID else { throw CheckFailure(message: "the stub daemon never started") }
        print("stub daemon running pid=\(pid); pgrep stub-sleep.sh: \(pgrep("stub-sleep.sh"))")
        let exited = daemon.stop()
        usleep(300_000)
        let afterStop = pgrep("stub-sleep.sh")
        print("stop() returned \(exited); runningPID now \(String(describing: daemon.runningPID)); pgrep stub-sleep.sh after stop: \(afterStop)")
        try require(exited, "stop() must confirm the daemon exited")
        try require(daemon.runningPID == nil, "no PID must remain after stop")
        try require(afterStop.isEmpty, "no stub must be left running after stop")

        print("live supervision demo passed.")
    }

    // MARK: - Live stream probe (real daemon; --stream only)

    /// Connects with the same `AKP03EventStreamClient` the app uses and prints
    /// each deck-state change it hears, or that the service is unreachable.
    private static func runStreamProbe(urlString: String, seconds: Double) throws {
        guard let url = URL(string: urlString) else {
            throw CheckFailure(message: "--stream URL is not valid: \(urlString)")
        }
        print("stream probe: \(url.absoluteString) for \(seconds)s")
        let client = AKP03EventStreamClient(url: url)
        client.start { update in
            switch update {
            case .connected:
                print("  service: reachable")
            case let .deck(status):
                print("  deck: phase=\(status.phase.rawValue) connected=\(status.connected) -> \(AKP03DeckStatusWording.detail(status))")
            case let .ended(reason):
                print("  service: not running (\(reason))")
            }
        }
        let finished = DispatchSemaphore(value: 0)
        DispatchQueue.global().asyncAfter(deadline: .now() + seconds) {
            client.stop()
            finished.signal()
        }
        finished.wait()
        print("stream probe finished.")
    }

    private static func describe(_ state: AKP03SupervisionState) -> String {
        switch state {
        case .idle: return "idle"
        case .starting: return "starting"
        case let .running(pid): return "running(pid: \(pid))"
        case let .restarting(after, attempt): return String(format: "restarting(after: %.2fs, attempt: %d)", after, attempt)
        case .stopped: return "stopped"
        case let .gaveUp(after): return "gaveUp(afterAttempts: \(after))"
        }
    }

    private static func pgrep(_ pattern: String) -> [Int32] {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        process.arguments = ["-f", pattern]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return [] }
        process.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(decoding: data, as: UTF8.self)
            .split(separator: "\n")
            .compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
    }
}

private struct CheckFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// Thread-safe collector for the live-supervision transcript.
private final class SupervisionTranscript: @unchecked Sendable {
    private let lock = NSLock()
    private var entries: [(Date, AKP03SupervisionState)] = []

    func append(_ state: AKP03SupervisionState) {
        lock.lock()
        entries.append((Date(), state))
        lock.unlock()
    }

    func snapshot() -> [(Date, AKP03SupervisionState)] {
        lock.lock()
        defer { lock.unlock() }
        return entries
    }
}

private final class CheckInputSource: AKP03HIDInputSource, @unchecked Sendable {
    private var receive: (@Sendable (AKP03ControlEvent) -> Void)?
    private(set) var startCount = 0
    private(set) var stopCount = 0
    func startReceiving(_ receive: @escaping @Sendable (AKP03ControlEvent) -> Void) throws { startCount += 1; self.receive = receive }
    func stopReceiving() { stopCount += 1; receive = nil }
    func emit(_ event: AKP03ControlEvent) { receive?(event) }
}
