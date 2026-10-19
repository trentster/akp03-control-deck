import Foundation

/// A macOS TCC permission relevant to this app.
public enum AKP03PermissionState: Equatable, Sendable {
    case granted
    case denied
    case unknown
}

/// The two permissions the app now becomes responsible for, because it is the
/// process that launches the daemon and the two Swift helpers: Input Monitoring
/// (so the deck's buttons reach the app) and Accessibility (so bound shortcuts
/// are typed). Reported honestly and surfaced as one clear, actionable line.
public struct AKP03PermissionReport: Equatable, Sendable {
    public let inputMonitoring: AKP03PermissionState
    public let accessibility: AKP03PermissionState

    public init(inputMonitoring: AKP03PermissionState, accessibility: AKP03PermissionState) {
        self.inputMonitoring = inputMonitoring
        self.accessibility = accessibility
    }

    public var inputMonitoringLine: String? {
        switch inputMonitoring {
        case .granted:
            return nil
        case .denied:
            return "Input Monitoring permission is not granted. The deck's buttons will not reach the app — allow AKP03 in System Settings, Privacy and Security, Input Monitoring; if AKP03 is not in that list, click + and add it from Applications. Then reopen the app."
        case .unknown:
            return "Input Monitoring permission could not be determined. Deck input may not reach the app until it is allowed in System Settings, Privacy and Security, Input Monitoring."
        }
    }

    public var accessibilityLine: String? {
        switch accessibility {
        case .granted:
            return nil
        case .denied:
            return "Accessibility permission is not granted. Bound shortcuts will not be typed — choose “Grant Accessibility…” below."
        case .unknown:
            return "Accessibility permission could not be determined. Bound shortcuts may not be typed until it is granted."
        }
    }

    /// The one line to show first, or `nil` when nothing is missing.
    public var summary: String? {
        inputMonitoringLine ?? accessibilityLine
    }

    public var hasMissingPermission: Bool {
        inputMonitoring != .granted || accessibility != .granted
    }
}
