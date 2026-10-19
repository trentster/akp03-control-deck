import AKP03MenuBarCore
import AppKit
import ApplicationServices
import Foundation
import IOKit.hid

/// The narrow, honest macOS permission checks.
///
/// The app launches the daemon and its two Swift helpers, so the app is the
/// process macOS asks about. Nothing here weakens the TCC model: Input
/// Monitoring is only *read* (`IOHIDCheckAccess`) and Accessibility is only
/// *read* (`AXIsProcessTrusted`); the one action available asks macOS to offer
/// the Accessibility grant via the existing `AKP03KeyInject
/// --request-accessibility` flag.
enum AKP03PermissionProbe {
    static func report() -> AKP03PermissionReport {
        AKP03PermissionReport(
            inputMonitoring: inputMonitoring(),
            accessibility: accessibility()
        )
    }

    static func inputMonitoring() -> AKP03PermissionState {
        let access = IOHIDCheckAccess(kIOHIDRequestTypeListenEvent)
        if access == kIOHIDAccessTypeGranted { return .granted }
        if access == kIOHIDAccessTypeDenied { return .denied }
        return .unknown
    }

    static func accessibility() -> AKP03PermissionState {
        AXIsProcessTrusted() ? .granted : .denied
    }

    /// Ask macOS to offer the Accessibility grant by running the existing
    /// helper's one-shot prompt. Posts nothing else and exits.
    static func requestAccessibility(helper: String) {
        let process = Process()
        process.executableURL = resolve(helper)
        process.arguments = ["--request-accessibility"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try? process.run()
    }

    /// Ask macOS to offer the Input Monitoring grant by asking the same question the deck's
    /// own input source asks when it first reads the pad.  Until something asks, the app is
    /// simply absent from that pane - which on a first run looks like "it is not listed
    /// anywhere" with no deck attached to trigger it.
    static func requestInputMonitoring() {
        if IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) == kIOHIDAccessTypeGranted { return }
        _ = IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)
    }

    /// Clear this app's own permission decisions and ask again.
    ///
    /// macOS treats a row removed from the Input Monitoring pane as a decision: the app is
    /// never offered the grant again, and no amount of asking produces the row. The only
    /// way back is for the decision to be reset - `tccutil` does that for one bundle at a
    /// time, without privileges, after which the next ask is a first ask again.
    static func resetOwnPermissions() {
        guard let bundleID = Bundle.main.bundleIdentifier else { return }
        for service in ["ListenEvent", "Accessibility"] {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/tccutil")
            process.arguments = ["reset", service, bundleID]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            try? process.run()
            process.waitUntilExit()
        }
    }

    /// Open System Settings at the Input Monitoring pane so the user can grant
    /// the permission without hunting for it.
    static func openInputMonitoringSettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent") else {
            return
        }
        NSWorkspace.shared.open(url)
    }

    private static func resolve(_ name: String) -> URL {
        if name.hasPrefix("/") { return URL(fileURLWithPath: name) }
        let path = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        for directory in path.split(separator: ":") {
            let candidate = "\(directory)/\(name)"
            if FileManager.default.isExecutableFile(atPath: candidate) {
                return URL(fileURLWithPath: candidate)
            }
        }
        return URL(fileURLWithPath: name)
    }
}
