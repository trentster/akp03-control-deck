import Foundation

/// One validated command line read from `AKP03HIDStream`'s own stdin.
///
/// This is the *only* place a caller-supplied device payload can be built, and it
/// is deliberately narrow: `paint` carries exactly one 1024-byte report. The
/// bytes come from this process's own stdin (`AKP03HIDStream`'s parent, the local
/// Python daemon) — never from a command-line argument, a socket, a file, or the
/// network. Anything that is not a well-formed paint line is refused with a
/// reason, and the caller (the streaming runner) answers with a result line
/// instead of writing anything to the device.
public enum AKP03HIDStreamRequest: Equatable, Sendable {
    case paint(payload: [UInt8])

    /// The one supported payload length: a single report-ID-0 output report.
    public static let payloadLength = 1024
    /// The longest accepted command line, including its base64 payload. A real
    /// paint line is ~1.4 KB; anything materially larger is refused before it is
    /// decoded so a burst of malformed input cannot grow memory without bound.
    public static let maximumLineCharacters = 8192

    /// Parses one non-empty command line. Empty input is the caller's to skip.
    public static func parse(line: String) throws -> Self {
        guard line.utf8.count <= maximumLineCharacters else {
            throw AKP03HIDStreamRequestError.lineTooLong(line.utf8.count)
        }
        let text = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            throw AKP03HIDStreamRequestError.malformedLine
        }
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let fields = object as? [String: Any] else {
            throw AKP03HIDStreamRequestError.malformedLine
        }
        guard let command = fields["command"] as? String else {
            throw AKP03HIDStreamRequestError.missingField("command")
        }
        switch command {
        case "paint":
            guard let encoded = fields["payload"] as? String else {
                throw AKP03HIDStreamRequestError.missingField("payload")
            }
            guard let decoded = Data(base64Encoded: encoded) else {
                throw AKP03HIDStreamRequestError.invalidBase64
            }
            guard decoded.count == payloadLength else {
                throw AKP03HIDStreamRequestError.wrongLength(decoded.count)
            }
            return .paint(payload: [UInt8](decoded))
        default:
            throw AKP03HIDStreamRequestError.unknownCommand(command)
        }
    }
}

/// Why a command line was refused. Each case maps to a short, stable reason string
/// that `AKP03HIDStream` sends back to the daemon verbatim.
public enum AKP03HIDStreamRequestError: LocalizedError, Equatable, Sendable {
    case malformedLine
    case lineTooLong(Int)
    case missingField(String)
    case unknownCommand(String)
    case invalidBase64
    case wrongLength(Int)

    public var errorDescription: String? {
        switch self {
        case .malformedLine: "command line is not a JSON object"
        case let .lineTooLong(count): "command line is too long (\(count) characters)"
        case let .missingField(name): "command line is missing the \(name) field"
        case let .unknownCommand(name): "unknown command: \(name)"
        case .invalidBase64: "paint payload is not valid base64"
        case let .wrongLength(count): "paint payload must be exactly 1024 bytes (received \(count))"
        }
    }
}
