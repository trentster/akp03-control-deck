import Darwin
import Foundation

/// The observable lifecycle of the supervised daemon.
public enum AKP03SupervisionState: Equatable, Sendable {
    case idle
    case starting
    case running(pid: Int32)
    case restarting(after: Double, attempt: Int)
    case stopped
    case gaveUp(afterAttempts: Int)
}

/// Starts the daemon as a child process and keeps it alive.
///
/// Responsibilities are deliberately narrow: spawn the configured argv, watch
/// for an unexpected exit, restart it under :class:`AKP03RestartTracker`'s
/// bounded backoff and rapid-restart cap, and — on stop — send `SIGTERM`, wait,
/// and only then escalate to `SIGKILL`. It never builds a shell string and
/// spawns nothing but the configured daemon; the daemon itself reaps its own
/// Swift helpers (they sit in their own sessions and exit when the daemon's
/// pipes close), so a clean stop leaves no orphan.
public final class AKP03DaemonSupervisor: @unchecked Sendable {
    public let configuration: AKP03LaunchConfiguration
    public let policy: AKP03SupervisionPolicy

    private let queue = DispatchQueue(label: "akp03.daemon.supervisor")
    private let lock = NSLock()
    private let logURL: URL?
    private var process: Process?
    private var startTime: Date?
    private var tracker: AKP03RestartTracker
    private var stopping = true
    private var pendingRestart: DispatchWorkItem?
    private var state: AKP03SupervisionState = .idle
    private var stateHandler: (@Sendable (AKP03SupervisionState) -> Void)?
    private var logHandle: FileHandle?
    /// Every process this supervisor has launched and has not yet reaped. Used
    /// by tests to prove nothing is left running.
    private var launchedPIDs: Set<Int32> = []

    public init(
        configuration: AKP03LaunchConfiguration,
        policy: AKP03SupervisionPolicy = .standard,
        logURL: URL? = nil
    ) {
        self.configuration = configuration
        self.policy = policy
        self.logURL = logURL
        self.tracker = AKP03RestartTracker(policy: policy)
    }

    public var currentState: AKP03SupervisionState {
        lock.lock(); defer { lock.unlock() }
        return state
    }

    /// The live child PID, or `nil` when nothing is running.
    public var runningPID: Int32? {
        lock.lock(); defer { lock.unlock() }
        guard let process, process.isRunning else { return nil }
        return process.processIdentifier
    }

    public func setStateHandler(_ handler: @escaping @Sendable (AKP03SupervisionState) -> Void) {
        lock.lock(); stateHandler = handler; lock.unlock()
    }

    /// Begin supervising. Idempotent while already running.
    public func start() {
        queue.async { [self] in
            guard stopping else { return }
            stopping = false
            tracker = AKP03RestartTracker(policy: policy)
            spawn()
        }
    }

    /// Stop the child (SIGTERM, wait, then SIGKILL) and start a fresh one.
    public func restart() {
        queue.async { [self] in
            _ = _terminateLocked(timeout: 5)
            stopping = false
            tracker = AKP03RestartTracker(policy: policy)
            spawn()
        }
    }

    /// Stop the child and stop supervising. Blocks until the child is gone (or
    /// the timeout plus the hard-kill grace elapses). Returns whether the child
    /// actually exited.
    @discardableResult
    public func stop(timeout: TimeInterval = 5) -> Bool {
        var exited = true
        queue.sync { [self] in
            stopping = true
            pendingRestart?.cancel()
            pendingRestart = nil
            exited = _terminateLocked(timeout: timeout)
        }
        publish(.stopped)
        return exited
    }

    // MARK: - Queue-confined internals

    private func spawn() {
        guard !stopping, process == nil else { return }
        let child = Process()
        child.executableURL = Self.resolveExecutable(configuration.executable)
        child.arguments = configuration.arguments
        if let workingDirectory = configuration.workingDirectory {
            child.currentDirectoryURL = URL(fileURLWithPath: workingDirectory)
        }
        var environment = ProcessInfo.processInfo.environment
        for (key, value) in configuration.environment { environment[key] = value }
        child.environment = environment
        if let logURL, let handle = Self.openLog(at: logURL) {
            child.standardOutput = handle
            child.standardError = handle
            logHandle = handle
        } else {
            child.standardOutput = FileHandle.nullDevice
            child.standardError = FileHandle.nullDevice
        }
        child.terminationHandler = { [weak self] terminated in
            guard let self else { return }
            self.queue.async { self.handleTermination(terminated) }
        }
        startTime = Date()
        process = child
        publish(.starting)
        do {
            try child.run()
        } catch {
            process = nil
            startTime = nil
            scheduleRestart(exitUptime: 0)
            return
        }
        launchedPIDs.insert(child.processIdentifier)
        publish(.running(pid: child.processIdentifier))
    }

    private func handleTermination(_ terminated: Process) {
        guard !stopping, process === terminated else { return }
        let uptime = startTime.map { Date().timeIntervalSince($0) } ?? 0
        process = nil
        startTime = nil
        launchedPIDs.remove(terminated.processIdentifier)
        scheduleRestart(exitUptime: uptime)
    }

    private func scheduleRestart(exitUptime: Double) {
        guard !stopping else { return }
        switch tracker.recordExit(uptime: exitUptime) {
        case let .giveUp(rapidExits):
            publish(.gaveUp(afterAttempts: rapidExits))
        case let .restart(after, rapidExits):
            publish(.restarting(after: after, attempt: rapidExits))
            let item = DispatchWorkItem { [weak self] in self?.spawn() }
            pendingRestart = item
            queue.asyncAfter(deadline: .now() + after, execute: item)
        }
    }

    /// Must be called on `queue`. Returns whether the child exited.
    private func _terminateLocked(timeout: TimeInterval) -> Bool {
        pendingRestart?.cancel()
        pendingRestart = nil
        guard let child = process else { return true }
        process = nil
        startTime = nil
        launchedPIDs.remove(child.processIdentifier)
        guard child.isRunning else { return true }
        child.terminate()  // SIGTERM: the daemon shuts down cleanly and reaps helpers
        let deadline = Date().addingTimeInterval(timeout)
        while child.isRunning && Date() < deadline { usleep(50_000) }
        guard child.isRunning else { return true }
        kill(child.processIdentifier, SIGKILL)
        let hardDeadline = Date().addingTimeInterval(2)
        while child.isRunning && Date() < hardDeadline { usleep(50_000) }
        return !child.isRunning
    }

    private func publish(_ newState: AKP03SupervisionState) {
        lock.lock()
        state = newState
        let handler = stateHandler
        lock.unlock()
        handler?(newState)
    }

    // MARK: - Helpers

    static func resolveExecutable(_ name: String) -> URL {
        if name.hasPrefix("/") { return URL(fileURLWithPath: name) }
        let path = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        for directory in path.split(separator: ":") {
            let candidate = URL(fileURLWithPath: String(directory)).appendingPathComponent(name)
            if FileManager.default.isExecutableFile(atPath: candidate.path) { return candidate }
        }
        return URL(fileURLWithPath: name)
    }

    static func openLog(at url: URL) -> FileHandle? {
        let directory = url.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: url) else { return nil }
        _ = try? handle.seekToEnd()
        return handle
    }
}
