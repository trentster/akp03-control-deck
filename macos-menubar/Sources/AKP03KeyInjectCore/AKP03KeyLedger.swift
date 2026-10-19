import Foundation

/// Pure hold/release bookkeeping for the injector.
///
/// Exactly one chord may be held at a time. This is the whole stuck-key defence
/// and it is device-free, so it is exercised directly by the check suite:
///
///   * a second `hold` while a chord is held is a *duplicate* and changes
///     nothing — the daemon's own per-control guard is an optimisation, this is
///     the authoritative one;
///   * a `release` when nothing is held is *unmatched* and releases nothing;
///   * any `release` releases *everything* held;
///   * `releaseAll` is used on quit / EOF / signal so the process can never exit
///     with a key still logically down.
public struct AKP03KeyLedger: Equatable, Sendable {
    public private(set) var held: AKP03KeyChord?

    public init() {}

    public mutating func hold(_ chord: AKP03KeyChord) -> AKP03KeyHoldOutcome {
        if let current = held { return .alreadyHeld(current) }
        held = chord
        return .held(chord)
    }

    /// Release everything held. Returns the chord that was released, or `nil`
    /// when nothing was held (the unmatched-release case).
    public mutating func release() -> AKP03KeyReleaseOutcome {
        guard let current = held else { return .nothingHeld }
        held = nil
        return .released(current)
    }

    /// Release everything held on shutdown. Idempotent.
    @discardableResult
    public mutating func releaseAll() -> AKP03KeyChord? {
        defer { held = nil }
        return held
    }
}

public enum AKP03KeyHoldOutcome: Equatable, Sendable {
    case held(AKP03KeyChord)
    case alreadyHeld(AKP03KeyChord)
}

public enum AKP03KeyReleaseOutcome: Equatable, Sendable {
    case released(AKP03KeyChord)
    case nothingHeld
}
