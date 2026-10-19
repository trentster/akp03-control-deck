import AKP03ControlServiceCore
import Foundation

/// Pure, device-free command-line parsing for `AKP03HIDStream`.
///
/// There are no required arguments: the default is an endless input stream. The
/// two options are `--poll-seconds`, which is clamped to a sane range rather than
/// rejected, and `--open-protocol`, which is validated strictly against the three
/// fixed modes and rejected otherwise. No option can carry a caller-supplied
/// packet: the mode selects between fixed, compile-time packet sets only.
public enum AKP03HIDStreamCommand: Equatable, Sendable {
    case help
    case run(pollSeconds: Int, openProtocol: AKP03OpenProtocolMode)

    public static let defaultPollSeconds = 2
    public static let minimumPollSeconds = 1
    public static let maximumPollSeconds = 60

    /// The stream default: the fixed wake packet only, proven sufficient for input
    /// and deliberately not clearing the (unrepaintable) LCDs.
    public static let defaultOpenProtocol: AKP03OpenProtocolMode = .defaultMode

    public static func clamp(pollSeconds: Int) -> Int {
        min(max(pollSeconds, minimumPollSeconds), maximumPollSeconds)
    }

    public static func parse(arguments: [String]) throws -> Self {
        if arguments == ["--help"] || arguments == ["-h"] { return .help }
        var pollSeconds = defaultPollSeconds
        var openProtocol = defaultOpenProtocol
        var index = 0
        while index < arguments.count {
            let flag = arguments[index]
            switch flag {
            case "--poll-seconds":
                guard index + 1 < arguments.count else {
                    throw AKP03HIDStreamArgumentError.missingValue(flag)
                }
                guard let value = Int(arguments[index + 1]) else {
                    throw AKP03HIDStreamArgumentError.invalidPollSeconds(arguments[index + 1])
                }
                pollSeconds = clamp(pollSeconds: value)
                index += 2
            case "--open-protocol":
                guard index + 1 < arguments.count else {
                    throw AKP03HIDStreamArgumentError.missingValue(flag)
                }
                guard let mode = AKP03OpenProtocolMode(rawValue: arguments[index + 1]) else {
                    throw AKP03HIDStreamArgumentError.invalidOpenProtocol(
                        arguments[index + 1],
                        allowed: AKP03OpenProtocolMode.allCases.map(\.rawValue)
                    )
                }
                openProtocol = mode
                index += 2
            default:
                throw AKP03HIDStreamArgumentError.unexpected(arguments.joined(separator: " "))
            }
        }
        return .run(pollSeconds: pollSeconds, openProtocol: openProtocol)
    }
}

public enum AKP03HIDStreamArgumentError: LocalizedError, Equatable, Sendable {
    case unexpected(String)
    case invalidPollSeconds(String)
    case missingValue(String)
    case invalidOpenProtocol(String, allowed: [String])

    public var errorDescription: String? {
        switch self {
        case let .unexpected(arguments): "unexpected arguments: \(arguments)"
        case let .invalidPollSeconds(value): "--poll-seconds must be an integer (received \(value))"
        case let .missingValue(flag): "\(flag) requires a value"
        case let .invalidOpenProtocol(value, allowed):
            "--open-protocol must be one of \(allowed.joined(separator: ", ")) (received \(value))"
        }
    }
}
