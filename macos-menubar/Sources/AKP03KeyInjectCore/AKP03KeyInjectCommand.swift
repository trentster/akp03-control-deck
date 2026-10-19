import Foundation

/// One validated command read from stdin.
///
/// The command language is deliberately tiny and whitespace-separated; the chord
/// is a single `+`-joined token field with no spaces:
///
///   * `ping`                     liveness + Accessibility status
///   * `quit`                     release everything and exit 0
///   * `tap cmd+z`                press and release a chord once
///   * `hold ctrl+shift+space`    press and keep held until released
///   * `release ctrl+shift+space` release everything held
///   * `media sound-up`           one press of a media key (tap only)
///
/// Anything else — an unknown verb, a missing chord, an extra field, an
/// unsupported key token, or a media name that is not one of the six — is
/// rejected rather than guessed.
public enum AKP03KeyInjectCommand: Equatable, Sendable {
    case ping
    case quit
    case tap(AKP03KeyChord)
    case hold(AKP03KeyChord)
    case release(AKP03KeyChord)
    case media(AKP03MediaKey)

    public static let verbs = ["ping", "quit", "tap", "hold", "release", "media"]

    public static func parse(_ line: String) throws -> AKP03KeyInjectCommand {
        let fields = line.split(separator: " ", omittingEmptySubsequences: true).map(String.init)
        guard let verb = fields.first else { throw AKP03KeyInjectCommandError.emptyCommand }
        switch verb {
        case "ping", "quit":
            guard fields.count == 1 else {
                throw AKP03KeyInjectCommandError.unexpectedArguments(verb, fields.dropFirst().joined(separator: " "))
            }
            return verb == "ping" ? .ping : .quit
        case "tap", "hold", "release":
            guard fields.count == 2 else {
                throw AKP03KeyInjectCommandError.wrongArgumentCount(verb)
            }
            let chord = try AKP03KeyChord.parse(fields[1])
            // A tap must complete a real key press, so it needs a non-modifier
            // key — the same rule the Linux wtype backend enforces. A hold or
            // release may be modifier-only.
            if verb == "tap", chord.isModifierOnly {
                throw AKP03KeyInjectCommandError.modifierOnlyTap(chord.rendered)
            }
            switch verb {
            case "tap": return .tap(chord)
            case "hold": return .hold(chord)
            default: return .release(chord)
            }
        case "media":
            // A media key is one system-defined event pair, never a chord: it
            // cannot be held, so there is no hold or release for it.
            guard fields.count == 2 else {
                throw AKP03KeyInjectCommandError.wrongArgumentCount(verb)
            }
            guard let key = AKP03MediaKey(rawValue: fields[1].lowercased()) else {
                throw AKP03KeyInjectCommandError.unsupportedMediaKey(fields[1])
            }
            return .media(key)
        default:
            throw AKP03KeyInjectCommandError.unknownCommand(verb)
        }
    }
}

public enum AKP03KeyInjectCommandError: LocalizedError, Equatable, Sendable {
    case emptyCommand
    case unknownCommand(String)
    case wrongArgumentCount(String)
    case unexpectedArguments(String, String)
    case modifierOnlyTap(String)
    case unsupportedMediaKey(String)

    public var errorDescription: String? {
        switch self {
        case .emptyCommand: "empty command"
        case let .unknownCommand(verb): "unknown command: '\(verb)'"
        case let .wrongArgumentCount(verb): "\(verb) requires exactly one chord argument"
        case let .unexpectedArguments(verb, extra): "\(verb) takes no arguments (received '\(extra)')"
        case let .modifierOnlyTap(chord): "tap requires a non-modifier key: '\(chord)'"
        case let .unsupportedMediaKey(name):
            "unsupported media key: '\(name)' (expected one of: "
                + AKP03MediaKey.allCases.map(\.rawValue).joined(separator: ", ") + ")"
        }
    }
}
