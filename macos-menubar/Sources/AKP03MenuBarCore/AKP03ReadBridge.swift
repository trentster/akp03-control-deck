import Foundation

/// Pure command-line parsing for the explicitly bounded native HID diagnostic.
public enum AKP03ReadBridge {
    public enum Arguments: Equatable, Sendable {
        case help
        case capture(seconds: Int)
    }

    public enum ArgumentError: LocalizedError, Equatable {
        case missingSeconds
        case invalidSeconds(String)
        case unexpectedArgument(String)

        public var errorDescription: String? {
            switch self {
            case .missingSeconds: "--seconds is required."
            case let .invalidSeconds(value): "--seconds must be an integer from 1 through 30 (received \(value))."
            case let .unexpectedArgument(value): "Unexpected argument: \(value)"
            }
        }
    }

    /// Deliberately requires a finite duration; there is no implicit endless run.
    public static func parse(arguments: [String]) throws -> Arguments {
        if arguments == ["--help"] || arguments == ["-h"] { return .help }
        guard arguments.count == 2, arguments[0] == "--seconds" else {
            if arguments.isEmpty { throw ArgumentError.missingSeconds }
            throw ArgumentError.unexpectedArgument(arguments.first ?? "")
        }
        guard let seconds = Int(arguments[1]), (1...30).contains(seconds) else {
            throw ArgumentError.invalidSeconds(arguments[1])
        }
        return .capture(seconds: seconds)
    }
}
