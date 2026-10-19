import CoreGraphics
import Foundation

/// A validated, ordered key chord, e.g. `ctrl+shift+space`.
///
/// A chord is parsed from a single `+`-joined token string. Every token must be
/// in ``AKP03KeyTable``; an unknown token, an empty token (`ctrl+`), an empty
/// chord, or a repeated token all fail validation. The chord is normalised so
/// modifiers are pressed first (in the order given) and released last (in
/// reverse), which is what a real user's keyboard does.
public struct AKP03KeyChord: Equatable, Sendable {
    /// One resolved press step. Release is this list reversed.
    public struct Step: Equatable, Sendable {
        public let token: String
        public let keyCode: CGKeyCode
        public let modifierFlag: CGEventFlags?
        public var isModifier: Bool { modifierFlag != nil }

        public init(token: String, entry: AKP03KeyTable.Entry) {
            self.token = token
            self.keyCode = entry.keyCode
            self.modifierFlag = entry.modifierFlag
        }
    }

    /// The canonical, lower-cased rendering of the chord, e.g. `ctrl+shift+space`.
    public let rendered: String
    /// The press order: modifiers first, then the remaining keys.
    public let pressSteps: [Step]

    /// True when the chord contains no non-modifier key (for example `ctrl+shift`).
    public var isModifierOnly: Bool { pressSteps.allSatisfy(\.isModifier) }

    /// What a press would post, in order: `keyDown <token> …`.
    public var pressedDescription: [String] { pressSteps.map { "keyDown \($0.token)" } }

    /// What a release would post, in order: `keyUp <token> …` (reverse of press).
    public var releasedDescription: [String] { pressSteps.reversed().map { "keyUp \($0.token)" } }

    private init(rendered: String, pressSteps: [Step]) {
        self.rendered = rendered
        self.pressSteps = pressSteps
    }

    /// Parse and validate a `+`-joined chord string. Throws
    /// ``AKP03KeyChordError`` on any token that is not in the table.
    public static func parse(_ text: String) throws -> AKP03KeyChord {
        let tokens = text.split(separator: "+", omittingEmptySubsequences: false).map(String.init)
        guard !text.isEmpty, !tokens.isEmpty else { throw AKP03KeyChordError.emptyChord }
        guard tokens.allSatisfy({ !$0.isEmpty }) else { throw AKP03KeyChordError.emptyToken(text) }
        var steps: [Step] = []
        var seen = Set<String>()
        for token in tokens {
            let normalized = token.lowercased()
            guard let entry = AKP03KeyTable.lookup(normalized) else {
                throw AKP03KeyChordError.unsupportedKey(token)
            }
            guard seen.insert(normalized).inserted else {
                throw AKP03KeyChordError.duplicateKey(normalized)
            }
            steps.append(Step(token: normalized, entry: entry))
        }
        // Modifiers press first (stable), then non-modifiers (stable).
        let ordered = steps.filter(\.isModifier) + steps.filter { !$0.isModifier }
        return AKP03KeyChord(rendered: ordered.map(\.token).joined(separator: "+"), pressSteps: ordered)
    }
}

public enum AKP03KeyChordError: LocalizedError, Equatable, Sendable {
    case emptyChord
    case emptyToken(String)
    case unsupportedKey(String)
    case duplicateKey(String)

    public var errorDescription: String? {
        switch self {
        case .emptyChord: "chord is empty"
        case let .emptyToken(text): "chord has an empty key token: \(text)"
        case let .unsupportedKey(token): "unsupported key: '\(token)'"
        case let .duplicateKey(token): "chord repeats the key '\(token)'"
        }
    }
}
