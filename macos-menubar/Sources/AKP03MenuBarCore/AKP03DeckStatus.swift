import Foundation

/// The deck connection phases the daemon's feed uses. Mirrors
/// `akp03.feed.PHASES` and the `AKP03HIDStream` helper exactly.
public enum AKP03DeckPhase: String, Equatable, Sendable, CaseIterable {
    case starting
    case waiting
    case ready
    case lost
    /// A deck is attached but would not answer.  Kept apart from ``waiting`` so the menu
    /// can say something useful to someone who has just plugged one in.
    case blocked
}

/// One truthfully-reported deck state, taken from the daemon's real `state`
/// feed frame — never inferred from the editor health probe.
public struct AKP03DeckStatus: Equatable, Sendable {
    public let connected: Bool
    public let phase: AKP03DeckPhase

    public init(connected: Bool, phase: AKP03DeckPhase) {
        self.connected = connected
        self.phase = phase
    }

    /// Decode a `state` frame's `{connected, phase}` object; anything unexpected
    /// is rejected rather than guessed at.
    public init?(snapshot: [String: Any]) {
        guard let raw = snapshot["phase"] as? String, let phase = AKP03DeckPhase(rawValue: raw) else {
            return nil
        }
        self.init(connected: (snapshot["connected"] as? Bool) ?? false, phase: phase)
    }
}

/// Plain-language wording for the deck fact. Every phrase is written so a
/// non-technical reader can tell "the deck is working" from "the deck is not
/// plugged in" from "the app is not running".
public enum AKP03DeckStatusWording {
    /// The menu's deck line.
    public static func detail(_ status: AKP03DeckStatus?) -> String {
        guard let status else {
            return "Deck: not reported yet — the app is not getting a live feed from the daemon."
        }
        switch status.phase {
        case .ready:
            return "Deck: connected and working."
        case .waiting:
            return "Deck: waiting for the deck — no AKP03 is plugged in."
        case .lost:
            return "Deck: connection lost — the deck was disconnected."
        case .blocked:
            return "Deck: plugged in, but it is not answering — try a different USB port, or a powered hub."
        case .starting:
            return "Deck: starting up…"
        }
    }

    /// A longer spoken form for VoiceOver.
    public static func spoken(_ status: AKP03DeckStatus?) -> String {
        guard let status else {
            return "Deck status unknown. The app is not receiving live status from the daemon."
        }
        switch status.phase {
        case .ready:
            return "Deck connected and working."
        case .waiting:
            return "Waiting for the deck. No AKP03 is plugged in."
        case .lost:
            return "Deck connection lost. The deck was disconnected."
        case .blocked:
            return "The deck is plugged in but not answering. Try a different USB port, or a powered hub."
        case .starting:
            return "Deck starting up."
        }
    }
}
