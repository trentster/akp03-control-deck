import Foundation

/// The bounded restart policy for the supervised daemon.
///
/// It mirrors the shape the Python side already uses for its helper
/// (`akp03.hidstream.RESTART_BACKOFF`): escalating backoff that resets after a
/// run that stayed up, plus a hard cap on *rapid* restarts so a daemon that
/// crashes on startup is never spun on.
public struct AKP03SupervisionPolicy: Equatable, Sendable {
    public let backoff: [Double]
    /// A run shorter than this counts as a "rapid" exit and counts toward the cap.
    public let rapidExitThreshold: Double
    /// Once this many rapid exits happen in a row, supervision gives up.
    public let maxRapidRestarts: Int

    public init(backoff: [Double], rapidExitThreshold: Double = 3, maxRapidRestarts: Int = 5) {
        self.backoff = backoff.isEmpty ? [1] : backoff
        self.rapidExitThreshold = rapidExitThreshold
        self.maxRapidRestarts = maxRapidRestarts
    }

    public static let standard = AKP03SupervisionPolicy(
        backoff: [0.5, 1, 2, 4, 8, 15, 30],
        rapidExitThreshold: 3,
        maxRapidRestarts: 5
    )

    public func delay(forFailures failures: Int) -> Double {
        backoff[min(max(failures, 0), backoff.count - 1)]
    }
}

/// What to do after the daemon process exited.
public enum AKP03RestartDecision: Equatable, Sendable {
    case restart(after: Double, rapidExits: Int)
    case giveUp(rapidExits: Int)
}

/// Pure restart bookkeeping: no clock, no processes, no side effects.
public struct AKP03RestartTracker: Equatable, Sendable {
    public let policy: AKP03SupervisionPolicy
    public private(set) var consecutiveRapidExits = 0
    public private(set) var failures = 0

    public init(policy: AKP03SupervisionPolicy) {
        self.policy = policy
    }

    /// Record an exit that ran for `uptime` seconds and decide what to do next.
    public mutating func recordExit(uptime: Double) -> AKP03RestartDecision {
        if uptime >= policy.rapidExitThreshold {
            // It ran long enough to be considered healthy: reset and restart
            // after the base delay rather than escalating.
            failures = 0
            consecutiveRapidExits = 0
            return .restart(after: policy.delay(forFailures: 0), rapidExits: 0)
        }
        consecutiveRapidExits += 1
        if consecutiveRapidExits > policy.maxRapidRestarts {
            return .giveUp(rapidExits: consecutiveRapidExits)
        }
        let delay = policy.delay(forFailures: failures)
        failures += 1
        return .restart(after: delay, rapidExits: consecutiveRapidExits)
    }
}
