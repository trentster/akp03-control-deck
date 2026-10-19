import Foundation

/// Whether the app's read-only health probe can reach the editor.
public enum AKP03ServiceReachability: Equatable, Sendable {
    case checking
    case reachable
    case unreachable
}

/// The menu-bar title and symbol. The title names the most important fact first:
/// whether the *app's* service is up, and what the *deck* is doing.
public enum AKP03MenuPresentation {
    public static func title(
        service: AKP03ServiceReachability,
        deck: AKP03DeckStatus?,
        supervision: AKP03SupervisionState
    ) -> String {
        if case .gaveUp = supervision { return "AKP03: Stopped" }
        switch service {
        case .checking:
            return "AKP03: Checking…"
        case .unreachable:
            return "AKP03: Offline"
        case .reachable:
            guard let deck else { return "AKP03: Idle" }
            switch deck.phase {
            case .ready: return "AKP03: Ready"
            case .waiting: return "AKP03: No deck"
            case .lost: return "AKP03: Deck lost"
            case .blocked: return "AKP03: Not answering"
            case .starting: return "AKP03: Starting"
            }
        }
    }

    public static func symbolName(
        service: AKP03ServiceReachability,
        deck: AKP03DeckStatus?,
        supervision: AKP03SupervisionState
    ) -> String {
        if case .gaveUp = supervision { return "exclamationmark.triangle" }
        switch service {
        case .checking:
            return "clock"
        case .unreachable:
            return "exclamationmark.triangle"
        case .reachable:
            guard let deck else { return "circle.dashed" }
            switch deck.phase {
            case .ready: return "slider.horizontal.3"
            case .waiting, .starting: return "circle.dashed"
            case .lost, .blocked: return "exclamationmark.triangle"
            }
        }
    }

    /// The service line, describing who owns the running daemon.
    ///
    /// A supervised daemon that failed to start, or that exited immediately,
    /// must say *why* — quoting the daemon's own last log line — rather than
    /// leaving the menu on a vague state. Ready/waiting/lost deck wording is
    /// untouched; ownership wording is unchanged for a reachable service.
    public static func serviceDetail(
        service: AKP03ServiceReachability,
        ownershipIsManaged: Bool,
        hasLaunchCommand: Bool,
        supervision: AKP03SupervisionState,
        lastLogLine: String? = nil
    ) -> String {
        if case let .gaveUp(attempts) = supervision {
            return "Service: the daemon could not start — stopped after \(attempts) failed attempts."
                + daemonReason(lastLogLine)
        }
        if case let .restarting(after, attempt) = supervision {
            return "Service: the daemon exited unexpectedly and is restarting in \(seconds(after))s (attempt \(attempt))."
                + daemonReason(lastLogLine)
        }
        switch service {
        case .checking:
            return "Service: checking…"
        case .reachable:
            return ownershipIsManaged
                ? "Service: running under this app."
                : "Service: running, started outside this app."
        case .unreachable:
            if !hasLaunchCommand { return "Service: cannot start — no daemon command." }
            return "Service: not running."
        }
    }

    /// Append the daemon's own last message in plain language, or nothing at all
    /// when there is no line to show. Empty means "do not invent a reason".
    private static func daemonReason(_ lastLogLine: String?) -> String {
        guard let line = lastLogLine?.trimmingCharacters(in: .whitespacesAndNewlines), !line.isEmpty else {
            return ""
        }
        return " Last message from the daemon: \(line)"
    }

    /// A short, honest delay rendering for the menu (whole seconds read cleaner).
    private static func seconds(_ value: Double) -> String {
        String(format: value == value.rounded() ? "%.0f" : "%.1f", value)
    }
}
