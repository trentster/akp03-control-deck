import AKP03ControlServiceCore
import AKP03IOKitInput
import AKP03MenuBarCore
import CoreFoundation
import Darwin
import Foundation

@main
struct AKP03HIDReadBridgeMain {
    static func main() {
        do {
            switch try AKP03ReadBridge.parse(arguments: Array(CommandLine.arguments.dropFirst())) {
            case .help:
                print(usage)
            case let .capture(seconds):
                try NativeEstablishedProtocolCapture(seconds: seconds).run()
            }
        } catch {
            FileHandle.standardError.write(Data("AKP03 native read bridge: \(error.localizedDescription)\n".utf8))
            FileHandle.standardError.write(Data("\(usage)\n".utf8))
            exit(EXIT_FAILURE)
        }
    }

    private static let usage = "Usage: AKP03HIDReadBridge --seconds 1...30\nBounded native macOS input diagnostic; runs the fixed Linux Deck.open() bootstrap before capture."
}

/// An approval-gated finite diagnostic that exercises the production adapter.
/// It has no separate IOKit callback implementation and never prints raw bytes.
private final class NativeEstablishedProtocolCapture {
    private let seconds: Int
    /// The diagnostic is the sole explicit consumer of the reviewed bootstrap opt-in.
    private let source = AKP03IOKitInputSource(bootstrapEstablishedOpenProtocol: true)

    init(seconds: Int) { self.seconds = seconds }

    func run() throws {
        try source.startReceiving { event in
            switch event {
            case let .known(control, _): print("known control=\(control.rawValue)")
            case .unknownPacket: print("unrecognized input packet")
            }
        }
        defer { source.stopReceiving() }
        _ = CFRunLoopRunInMode(.defaultMode, CFTimeInterval(seconds), false)
    }
}
