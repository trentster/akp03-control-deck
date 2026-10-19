import AKP03MenuBarCore
import AppKit
import Foundation
import SwiftUI

@main
struct AKP03MenuBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model: ServiceModel

    init() {
        // This is a menu-bar-only app: do not create a Dock icon or app window.
        NSApplication.shared.setActivationPolicy(.accessory)

        let executablePath = Bundle.main.executableURL?.path ?? ""
        let repoRoot = AKP03LaunchConfigurationResolver.discoverRepoRoot(
            startingAt: [
                FileManager.default.currentDirectoryPath,
                executablePath,
            ]
        )
        // Inside an installed app there is no repository to find: the runtime,
        // the Python package and the helpers travel with the app, so the launch
        // is self-contained and no longer "development mode".
        let bundled = BundledPayload.discover(executablePath: executablePath)
        let parsed: AKP03CompanionArguments
        do {
            parsed = try AKP03CompanionArguments.parse(
                arguments: Array(CommandLine.arguments.dropFirst()),
                userDefaults: UserDefaults.standard.dictionaryRepresentation(),
                discoveredRepoRoot: repoRoot,
                discoveredBundle: bundled,
                environment: ProcessInfo.processInfo.environment
            )
        } catch {
            let alert = NSAlert(error: error)
            alert.runModal()
            parsed = try! AKP03CompanionArguments.parse(arguments: [])
        }

        let model = ServiceModel(
            endpoint: parsed.endpoint,
            launch: parsed.launch,
            launchError: parsed.launchError
        )
        _model = StateObject(wrappedValue: model)
        AppDelegate.onTerminate = { [weak model] in model?.stopDaemonNow() }
        model.start()
    }

    var body: some Scene {
        MenuBarExtra(model.menuBarTitle, systemImage: model.menuBarSymbolName) {
            CompanionMenu(model: model)
        }
        .menuBarExtraStyle(.menu)
    }
}

/// Guarantees the daemon is stopped however the app exits.
final class AppDelegate: NSObject, NSApplicationDelegate {
    @MainActor static var onTerminate: (@MainActor () -> Void)?

    func applicationWillTerminate(_ notification: Notification) {
        AppDelegate.onTerminate?()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        AppDelegate.onTerminate?()
        return .terminateNow
    }
}

/// Owns the supervised daemon, the live status feed, the health probe and the
/// permission facts the menu shows.
@MainActor
final class ServiceModel: ObservableObject {
    enum Ownership: Equatable {
        case unknown
        case managed     // started and supervised by this app
        case external    // a daemon is already running, started outside this app
        case unavailable // no launch command could be resolved
    }

    @Published private(set) var service: AKP03ServiceReachability = .checking
    @Published private(set) var deck: AKP03DeckStatus?
    @Published private(set) var supervision: AKP03SupervisionState = .idle
    @Published private(set) var permissions = AKP03PermissionReport(inputMonitoring: .unknown, accessibility: .unknown)
    @Published private(set) var ownership: Ownership = .unknown
    /// The supervised daemon's own last log line, read only when it matters (a
    /// failed or restarting daemon) so the menu can say why in plain language.
    @Published private(set) var daemonLogLine: String?

    let endpoint: AKP03Endpoint
    let launch: AKP03LaunchConfiguration?
    let launchError: String?

    private var supervisor: AKP03DaemonSupervisor?
    private var stream: AKP03EventStreamClient?
    private var healthTimer: Timer?
    private var streamProblem: String?
    private static let healthSession = URLSession(
        configuration: .ephemeral,
        delegate: AKP03RejectRedirects.shared,
        delegateQueue: nil
    )

    init(endpoint: AKP03Endpoint, launch: AKP03LaunchConfiguration?, launchError: String?) {
        self.endpoint = endpoint
        self.launch = launch
        self.launchError = launchError
        refreshPermissions()
    }

    // MARK: - Lifecycle

    func start() {
        refreshPermissions()
        askForPermissionsOnce()
        guard let launch else {
            ownership = .unavailable
            service = .unreachable
            beginHealthChecks()
            return
        }
        Task { [weak self] in
            guard let self else { return }
            if await self.probeOnce() {
                self.ownership = .external
                self.service = .reachable
            } else {
                self.ownership = .managed
                self.startSupervisor(launch)
            }
            self.beginHealthChecks()
            self.beginStreaming()
        }
    }

    /// Called on quit. Stops only the daemon this app started.
    func stopDaemonNow() {
        stream?.stop()
        healthTimer?.invalidate()
        healthTimer = nil
        supervisor?.stop()
    }

    func startSupervisor(_ launch: AKP03LaunchConfiguration) {
        let supervisor = AKP03DaemonSupervisor(configuration: launch, logURL: Self.daemonLogURL())
        supervisor.setStateHandler { [weak self] state in
            Task { @MainActor in
                self?.supervision = state
                self?.updateDaemonLogLine()
            }
        }
        self.supervisor = supervisor
        supervisor.start()
    }

    /// Read the daemon's last log line when the supervised daemon is not running,
    /// and clear it once the daemon is healthy again. A failed start must be
    /// explained with the daemon's own words, not a vague state.
    private func updateDaemonLogLine() {
        switch supervision {
        case .gaveUp, .restarting:
            daemonLogLine = AKP03DaemonLog.lastLine(at: Self.daemonLogURL())
        case .running:
            daemonLogLine = nil
        case .idle, .starting, .stopped:
            break
        }
    }

    // MARK: - Health probe

    private func beginHealthChecks() {
        guard healthTimer == nil else { return }
        healthTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refreshHealth() }
        }
    }

    func refresh() {
        updateDaemonLogLine()
        Task { [weak self] in await self?.refreshHealth() }
    }

    private func refreshHealth() async {
        service = await probeOnce() ? .reachable : .unreachable
    }

    private func probeOnce() async -> Bool {
        var request = URLRequest(url: endpoint.healthURL)
        request.httpMethod = "GET"
        request.timeoutInterval = 3
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (_, response) = try await Self.healthSession.data(for: request)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else { return false }
            return true
        } catch {
            return false
        }
    }

    // MARK: - Live deck feed

    private func beginStreaming() {
        let client = AKP03EventStreamClient(url: endpoint.eventsURL)
        client.start { [weak self] update in
            Task { @MainActor in self?.apply(update) }
        }
        stream = client
    }

    private func apply(_ update: AKP03DeckStreamUpdate) {
        switch update {
        case .connected:
            service = .reachable
            streamProblem = nil
        case let .deck(status):
            deck = status
        case let .ended(reason):
            if streamProblem != reason { streamProblem = reason }
        }
    }

    // MARK: - Actions

    func openEditor() {
        NSWorkspace.shared.open(endpoint.editorURL)
    }

    func restartDeck() {
        supervisor?.restart()
    }

    func refreshPermissions() {
        permissions = AKP03PermissionProbe.report()
    }

    func requestAccessibility() {
        let helper = launch?.environment["AKP03_KEYINJECT"] ?? "AKP03KeyInject"
        AKP03PermissionProbe.requestAccessibility(helper: helper)
    }

    /// A fresh copy of the app has no entries in either pane to switch on: macOS only lists an
    /// app once it has asked, and this one asks late - Accessibility when a shortcut is first
    /// typed, Input Monitoring when the deck is first read.  On a first run that reads as
    /// "there is nothing here to grant", and with no deck attached the Input Monitoring entry
    /// never appears at all.  So the app asks on first run, for both.
    ///
    /// Accessibility is asked once *per build*: macOS ties a grant to the exact code the
    /// person granted it to, and every build of an ad-hoc signed app is different code, so
    /// after an update the old grant belongs to a copy that no longer exists.
    ///
    /// Input Monitoring is asked for on **every launch while it is not granted**.  That is
    /// not the same question twice: `IOHIDRequestAccess` is what makes macOS offer the app,
    /// and once the person has removed the row from the pane macOS never offers it again by
    /// itself - the app has to ask once more before the row can come back.  Nothing happens
    /// when the grant is already in place, because the call is skipped.
    private func askForPermissionsOnce() {
        let defaults = UserDefaults.standard
        let build = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String)
            ?? (Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String)
            ?? "unknown"
        logPermissionStep("launch build=\(build) inputMonitoring=\(permissions.inputMonitoring) accessibility=\(permissions.accessibility)")
        if permissions.accessibility != .granted,
           defaults.string(forKey: "AKP03AskedForPermissionsBuild") != build {
            defaults.set(build, forKey: "AKP03AskedForPermissionsBuild")
            requestAccessibility()
        }
        if permissions.inputMonitoring != .granted {
            // macOS shows the prompt on behalf of a running app, and answers a request
            // made during startup before the prompt can appear - the row then never
            // reaches the pane. So the ask waits until the app is up.
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                AKP03PermissionProbe.requestInputMonitoring()
                self?.refreshPermissions()
                self?.logPermissionStep("asked for Input Monitoring; now \(AKP03PermissionProbe.report().inputMonitoring)")
            }
        }
    }

    /// Appends one line to `~/Library/Logs/AKP03/menubar.log`.
    ///
    /// The permission panes live outside the app, so when asking macOS for a grant does
    /// not produce a row there is nothing on screen to read. This is the only way to see
    /// what macOS answered instead of guessing at it.
    private func logPermissionStep(_ text: String) {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/AKP03", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let url = directory.appendingPathComponent("menubar.log")
        let data = Data("\(stamp) permissions: \(text)\n".utf8)
        if let handle = try? FileHandle(forWritingTo: url) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: data)
        } else {
            try? data.write(to: url)
        }
    }

    func openInputMonitoringSettings() {
        askForInputMonitoringNow()
        AKP03PermissionProbe.openInputMonitoringSettings()
    }

    /// Wipe this app's own permission decisions, ask again, and show the pane.
    ///
    /// The deliberate escape hatch. macOS will not re-offer a permission whose row has been
    /// removed, so nothing the app does can put the row back by itself; after a reset the
    /// next ask is a first ask, and the prompt that makes the row appear always follows.
    func resetPermissionsAndAskAgain() {
        AKP03PermissionProbe.resetOwnPermissions()
        requestAccessibility()
        AKP03PermissionProbe.requestInputMonitoring()
        refreshPermissions()
        AKP03PermissionProbe.openInputMonitoringSettings()
    }

    /// Ask macOS for Input Monitoring with the app fully up and a person waiting on it.
    ///
    /// A request made while the app is still starting is answered before macOS can put a
    /// prompt on screen, so nothing reaches the pane. A request made from a click the
    /// person has just taken always produces the prompt - and the prompt is what makes the
    /// row appear at all. Both permission menu items use this, so one click nudges both.
    func askForInputMonitoringNow() {
        AKP03PermissionProbe.requestInputMonitoring()
        refreshPermissions()
    }

    // MARK: - Derived menu content

    var menuBarTitle: String {
        AKP03MenuPresentation.title(service: service, deck: deck, supervision: supervision)
    }

    var menuBarSymbolName: String {
        AKP03MenuPresentation.symbolName(service: service, deck: deck, supervision: supervision)
    }

    var canConfigure: Bool { service == .reachable }

    var canRestartDeck: Bool { ownership == .managed }

    var editorLine: String {
        switch service {
        case .checking:
            return "Editor: checking \(endpoint.healthURL.absoluteString)…"
        case .reachable:
            return "Editor: reachable at \(endpoint.editorURL.absoluteString)"
        case .unreachable:
            return "Editor: not reachable at \(endpoint.editorURL.absoluteString)"
        }
    }

    var deckLine: String {
        // A supervised daemon that has stopped cannot be feeding live deck
        // state: never keep claiming "starting up…" once we know it is down.
        if daemonNotRunning, deck == nil || deck?.phase == .starting {
            return AKP03DeckStatusWording.detail(nil)
        }
        return AKP03DeckStatusWording.detail(deck)
    }

    /// True while the app supervises a daemon that is not running.
    private var daemonNotRunning: Bool {
        switch supervision {
        case .gaveUp, .restarting: return true
        default: return false
        }
    }

    var serviceLine: String {
        AKP03MenuPresentation.serviceDetail(
            service: service,
            ownershipIsManaged: ownership == .managed,
            hasLaunchCommand: launch != nil,
            supervision: supervision,
            lastLogLine: daemonLogLine
        )
    }

    var spokenSummary: String {
        [editorLine, AKP03DeckStatusWording.spoken(deck), serviceLine].joined(separator: " ")
    }

    var permissionSummary: String? { permissions.summary }

    var launchDetail: String? {
        if let launchError { return launchError }
        guard let launch else { return nil }
        return launch.isDevelopmentMode
            ? "Launch (development mode): \(launch.displayCommand)"
            : "Launch (bundled): \(launch.displayCommand)"
    }

    /// Where the daemon keeps its configuration: the explicit `--config` the app
    /// passes when it launches the service, otherwise the daemon's own default.
    var configPathForDisplay: String {
        if let launch, let index = launch.arguments.firstIndex(of: "--config"),
           launch.arguments.indices.contains(index + 1) {
            return launch.arguments[index + 1]
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent(".config/akp03/config.toml").path
    }

    var logPathForDisplay: String { Self.daemonLogURL()?.path ?? "unavailable" }

    func revealDaemonLog() {
        guard let url = Self.daemonLogURL() else { return }
        if FileManager.default.fileExists(atPath: url.path) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } else {
            NSWorkspace.shared.open(url.deletingLastPathComponent())
        }
    }

    func copyLaunchCommand() {
        guard let command = launch?.displayCommand else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(command, forType: .string)
    }

    /// The Quit item must be unambiguous about what actually happens.
    var quitTitle: String {
        ownership == .external ? "Quit AKP03 (leaves the running service)" : "Quit AKP03 (stops the deck)"
    }

    var quitHint: String {
        ownership == .external
            ? "Quits the AKP03 app. The daemon was started outside this app, so it is left running."
            : "Stops the AKP03 daemon this app started, then quits. Any keys it is holding are released."
    }

    static func daemonLogURL() -> URL? {
        guard let library = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first else {
            return nil
        }
        return library.appendingPathComponent("Logs/AKP03/daemon.log")
    }
}

private struct CompanionMenu: View {
    @ObservedObject var model: ServiceModel

    var body: some View {
        Group {
            Text(model.editorLine)
                .font(.caption)
                .accessibilityLabel(model.spokenSummary)

            Text(model.deckLine)
                .font(.caption)
                .accessibilityLabel(model.deckLine)

            Text(model.serviceLine)
                .font(.caption)
                .accessibilityLabel(model.serviceLine)

            if let permission = model.permissionSummary {
                Divider()
                Text(permission)
                    .font(.caption)
                    .accessibilityLabel(permission)
            }

            Divider()

            Button("Configure AKP03") {
                model.openEditor()
            }
            .disabled(!model.canConfigure)
            .accessibilityHint("Opens the local AKP03 editor in your default browser.")

            Button("Restart the deck") {
                model.restartDeck()
            }
            .disabled(!model.canRestartDeck)
            .accessibilityHint("Stops and restarts the AKP03 daemon this app is supervising.")

            // A fallback, not a duplicate: show it only when the status check says the
            // editor is unreachable, so the menu stays honest and uncluttered.
            if !model.canConfigure {
                Button("Open AKP03 anyway") {
                    model.openEditor()
                }
                .accessibilityHint("Opens the configured local AKP03 address even when the status check is offline.")
            }

            if model.permissions.accessibility != .granted {
                Button("Grant Accessibility…") {
                    model.requestAccessibility()
                    model.askForInputMonitoringNow()
                }
                .accessibilityHint("Asks macOS to offer both permissions the app needs: Accessibility to type bound shortcuts, and Input Monitoring to read the deck.")
            }

            if model.permissions.inputMonitoring != .granted {
                Button("Open Input Monitoring settings…") {
                    model.openInputMonitoringSettings()
                }
                .accessibilityHint("Opens System Settings at the Input Monitoring pane so deck input can be allowed.")
            }

            if model.permissions.inputMonitoring != .granted || model.permissions.accessibility != .granted {
                Button("Clear and re-ask for permissions…") {
                    model.resetPermissionsAndAskAgain()
                }
                .accessibilityHint("Clears this app's own permission decisions and asks macOS again. macOS restores Accessibility this way; it will not put the Input Monitoring row back, so add AKP03 with + in that pane.")
            }

            Divider()

            Button("Refresh status") {
                model.refresh()
                model.refreshPermissions()
            }
            .accessibilityHint("Checks the local editor immediately using a read-only HTTP GET request.")

            // A launch failure is the user's business, so it stays in the menu proper.
            if let launchError = model.launchError {
                Text("Service could not start: \(launchError)")
                    .font(.caption)
                    .accessibilityLabel("Service could not start: \(launchError)")
            }

            Menu("Diagnostics") {
                Text("Config: \(model.configPathForDisplay)")
                Text("Log: \(model.logPathForDisplay)")
                Divider()
                Button("Reveal the daemon log in Finder") {
                    model.revealDaemonLog()
                }
                Button("Copy launch command") {
                    model.copyLaunchCommand()
                }
                .disabled(model.launch == nil)
                if let detail = model.launchDetail {
                    Divider()
                    Text(detail)
                }
            }
            .accessibilityHint("Where the deck's configuration and log live, and how the service is launched.")

            Divider()

            Button(model.quitTitle) {
                NSApplication.shared.terminate(nil)
            }
            .keyboardShortcut("q")
            .accessibilityHint(model.quitHint)
        }
        // MenuBarExtra recreates this menu when clicked; refresh so it is fresh.
        .onAppear {
            model.refresh()
            model.refreshPermissions()
        }
    }
}
