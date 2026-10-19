import Foundation

/// Development-mode launch settings for the Python AKP03 daemon.
///
/// The app now owns the daemon as a child process, but the daemon is **not**
/// bundled yet: it is the repository's own `.venv/bin/python` running
/// `-m akp03.daemon`. This is explicitly *development mode*; bundling a Python
/// runtime into the app (so it is self-contained) is a later packaging step.
///
/// Everything the app launches is a fixed argv array handed straight to
/// `Process`. The app never builds a shell string, so no path or argument can be
/// reinterpreted by a shell.
public struct AKP03LaunchConfiguration: Equatable, Sendable {
    /// The program to run: an absolute path, or a bare name resolved on `PATH`.
    public let executable: String
    /// The exact arguments passed to `executable`, in order.
    public let arguments: [String]
    /// Extra environment variables for the child, merged over the app's own.
    public let environment: [String: String]
    /// Working directory for the child, when one is known.
    public let workingDirectory: String?

    /// True when this launch comes from the app's own payload rather than a
    /// developer's checkout.
    public let bundled: Bool

    public init(
        executable: String,
        arguments: [String],
        environment: [String: String] = [:],
        workingDirectory: String? = nil,
        bundled: Bool = false
    ) {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
        self.workingDirectory = workingDirectory
        self.bundled = bundled
    }

    /// False once the launch comes from inside a self-contained app bundle: the
    /// menu must stop calling that "development mode".
    public var isDevelopmentMode: Bool { !bundled }

    /// A human-readable rendering for the menu. Quoting is display-only; the real
    /// launch always uses `arguments` verbatim.
    public var displayCommand: String {
        ([executable] + arguments).map(Self.displayQuoted).joined(separator: " ")
    }

    static func displayQuoted(_ token: String) -> String {
        guard token.contains(" ") || token.contains("\"") else { return token }
        return "\"" + token.replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }
}

/// The payload a self-contained app bundle carries.
///
/// A packaged app has no repository to discover, so its Python runtime, the
/// `akp03` package and the two Swift helpers live inside its own
/// `Contents/Resources`. Discovery is deliberately strict - the runtime must
/// actually be there - so a bare copy of the binary never pretends to be a
/// finished bundle.
public struct BundledPayload: Equatable, Sendable {
    public let resourcesRoot: String

    public init(resourcesRoot: String) {
        self.resourcesRoot = resourcesRoot
    }

    public var pythonExecutable: String { "\(resourcesRoot)/python/bin/python3" }
    /// Holds the `akp03` package; becomes the child's `PYTHONPATH` and cwd.
    public var packageRoot: String { "\(resourcesRoot)/app" }
    public var hidStream: String { "\(resourcesRoot)/bin/AKP03HIDStream" }
    public var keyInject: String { "\(resourcesRoot)/bin/AKP03KeyInject" }

    /// Resolve the payload from the app's own executable path.
    public static func discover(
        executablePath: String,
        fileExists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) -> BundledPayload? {
        guard let contents = executablePath.range(of: "/Contents/MacOS/") else { return nil }
        let payload = BundledPayload(
            resourcesRoot: String(executablePath[..<contents.lowerBound]) + "/Contents/Resources"
        )
        guard fileExists(payload.pythonExecutable) else { return nil }
        return payload
    }
}

/// Validation failures for the daemon launch command. Every case is actionable.
public enum LaunchConfigurationError: LocalizedError, Equatable {
    case invalidExecutable(String)
    case invalidArgument(String)
    case invalidPath(flag: String, value: String)
    case missingValue(String)
    case invalidEditorPort(String)
    case unrecognizedArgument(String)
    case helpRequested
    case noPythonEnvironment

    public var errorDescription: String? {
        switch self {
        case let .invalidExecutable(value):
            return "The daemon command is not runnable: \(value.isEmpty ? "(empty)" : value). Use an absolute path or a bare command name."
        case let .invalidArgument(value):
            return "The daemon argument \(value.debugDescription) contains characters that cannot be passed to a process."
        case let .invalidPath(flag, value):
            return "\(flag) must be an absolute path or a bare command name (received \(value.isEmpty ? "(empty)" : value))."
        case let .missingValue(flag):
            return "\(flag) requires a value."
        case let .invalidEditorPort(value):
            return "--editor-port must be an integer from 0 through 65535 (received \(value))."
        case let .unrecognizedArgument(value):
            return "Unrecognized argument: \(value)"
        case .helpRequested:
            return "Usage: AKP03MenuBar [--url http://127.0.0.1:17865/] [--repo-root PATH] [--daemon-command PATH] [--daemon-arg VALUE ...]"
        case .noPythonEnvironment:
            return "No AKP03 Python environment was found. Pass --repo-root or --daemon-command (development mode)."
        }
    }
}

/// Builds a validated :class:`AKP03LaunchConfiguration` from command-line
/// arguments and `UserDefaults` overrides.
///
/// Overrides, highest precedence first: command-line arguments, then
/// `UserDefaults`, then discovery from the repository. Nothing is accepted
/// without validation.
public enum AKP03LaunchConfigurationResolver {
    public static let defaultEditorPort = 17865
    public static let defaultDaemonArguments = ["-m", "akp03.daemon"]

    /// `--daemon-arg` (repeatable) *replaces* the default daemon argument list,
    /// so a custom `--daemon-command` receives exactly the arguments given. With
    /// no `--daemon-arg`, the default Python invocation is used, always carrying
    /// `--editor-port` so the daemon hosts the editor on the port the app probes.
    public static func resolve(
        arguments: [String],
        defaultEditorPort: Int = defaultEditorPort,
        userDefaults: [String: Any] = [:],
        discoveredRepoRoot: String? = nil,
        discoveredBundle: BundledPayload? = nil,
        environment: [String: String] = [:],
        fileExists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) throws -> AKP03LaunchConfiguration {
        var explicitExecutable = userDefaults["AKP03DaemonExecutable"] as? String
        var explicitDaemonArguments = userDefaults["AKP03DaemonArguments"] as? [String]
        var repoRoot = (userDefaults["AKP03RepoRoot"] as? String) ?? discoveredRepoRoot
        var configPath = userDefaults["AKP03ConfigPath"] as? String
        var socketPath = userDefaults["AKP03SocketPath"] as? String
        var editorPort = (userDefaults["AKP03EditorPort"] as? Int) ?? defaultEditorPort
        var hidStream = userDefaults["AKP03HIDStream"] as? String
        var keyInject = userDefaults["AKP03KeyInject"] as? String

        var index = 0
        while index < arguments.count {
            let flag = arguments[index]
            func takeValue() throws -> String {
                index += 1
                guard index < arguments.count else { throw LaunchConfigurationError.missingValue(flag) }
                return arguments[index]
            }
            switch flag {
            case "--url":
                // Validated by the endpoint parser; only the value is skipped here.
                _ = try takeValue()
            case "--repo-root":
                repoRoot = try takeValue()
            case "--daemon-command":
                explicitExecutable = try takeValue()
            case "--daemon-arg":
                let token = try takeValue()
                explicitDaemonArguments = (explicitDaemonArguments ?? []) + [token]
            case "--config":
                configPath = try takeValue()
            case "--socket":
                socketPath = try takeValue()
            case "--editor-port":
                let raw = try takeValue()
                guard let port = Int(raw), (0...65535).contains(port) else {
                    throw LaunchConfigurationError.invalidEditorPort(raw)
                }
                editorPort = port
            case "--hidstream":
                hidStream = try takeValue()
            case "--keyinject":
                keyInject = try takeValue()
            case "--help", "-h":
                throw LaunchConfigurationError.helpRequested
            default:
                throw LaunchConfigurationError.unrecognizedArgument(flag)
            }
            index += 1
        }

        let resolvedRepoRoot = repoRoot.map(normalizePath)
        // A packaged app has no `.venv` to discover: its runtime, package and
        // helpers ship inside its own `Contents/Resources`. An explicit flag or
        // a UserDefaults override still wins over both.
        let payload = (resolvedRepoRoot == nil && explicitExecutable == nil) ? discoveredBundle : nil
        if resolvedRepoRoot == nil, explicitExecutable == nil, payload == nil {
            throw LaunchConfigurationError.noPythonEnvironment
        }

        let executable: String
        if let explicitExecutable {
            executable = try validateExecutable(explicitExecutable)
        } else if let resolvedRepoRoot {
            executable = try validateExecutable("\(resolvedRepoRoot)/.venv/bin/python")
        } else if let payload {
            executable = try validateExecutable(payload.pythonExecutable)
        } else {
            throw LaunchConfigurationError.noPythonEnvironment
        }

        var daemonArguments: [String]
        if let explicitDaemonArguments {
            for token in explicitDaemonArguments { try validateArgument(token) }
            daemonArguments = explicitDaemonArguments
        } else {
            var built = defaultDaemonArguments
            if let configPath, !configPath.isEmpty {
                built += ["--config", try validatePath(configPath, flag: "--config")]
            }
            if let socketPath, !socketPath.isEmpty {
                built += ["--socket", try validatePath(socketPath, flag: "--socket")]
            }
            built += ["--editor-port", String(editorPort)]
            daemonArguments = built
        }

        var childEnvironment = environment
        if let importRoot = resolvedRepoRoot ?? payload?.packageRoot {
            let existing = environment["PYTHONPATH"].flatMap { $0.isEmpty ? nil : $0 }
            childEnvironment["PYTHONPATH"] = existing.map { "\(importRoot):\($0)" } ?? importRoot
        }
        if payload != nil {
            // The daemon runs from inside a signed bundle. Writing bytecode
            // caches into it would mutate a sealed resource and invalidate the
            // signature, so a bundled launch never writes .pyc files; the cost is
            // a little compile time at each start, once per process.
            childEnvironment["PYTHONDONTWRITEBYTECODE"] = "1"
        }
        if let hidStream, !hidStream.isEmpty {
            childEnvironment["AKP03_HIDSTREAM"] = try validatePath(hidStream, flag: "--hidstream")
        } else if let resolvedRepoRoot {
            let candidate = "\(resolvedRepoRoot)/macos-menubar/.build/debug/AKP03HIDStream"
            if fileExists(candidate) { childEnvironment["AKP03_HIDSTREAM"] = candidate }
        } else if let payload, fileExists(payload.hidStream) {
            childEnvironment["AKP03_HIDSTREAM"] = payload.hidStream
        }
        if let keyInject, !keyInject.isEmpty {
            childEnvironment["AKP03_KEYINJECT"] = try validatePath(keyInject, flag: "--keyinject")
        } else if let resolvedRepoRoot {
            let candidate = "\(resolvedRepoRoot)/macos-menubar/.build/debug/AKP03KeyInject"
            if fileExists(candidate) { childEnvironment["AKP03_KEYINJECT"] = candidate }
        } else if let payload, fileExists(payload.keyInject) {
            childEnvironment["AKP03_KEYINJECT"] = payload.keyInject
        }

        return AKP03LaunchConfiguration(
            executable: executable,
            arguments: daemonArguments,
            environment: childEnvironment,
            workingDirectory: resolvedRepoRoot ?? payload?.packageRoot,
            bundled: payload != nil
        )
    }

    /// Walk up from each starting path looking for the repository's virtual
    /// environment. Used only for the development-mode default; an explicit
    /// `--repo-root`/`--daemon-command` always wins.
    public static func discoverRepoRoot(
        startingAt paths: [String],
        fileExists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) -> String? {
        for path in paths where !path.isEmpty {
            var url = URL(fileURLWithPath: path)
            if !url.hasDirectoryPath { url.deleteLastPathComponent() }
            for _ in 0..<16 {
                if fileExists(url.appendingPathComponent(".venv/bin/python").path) { return url.path }
                let parent = url.deletingLastPathComponent()
                if parent.path == url.path { break }
                url = parent
            }
        }
        return nil
    }

    static func normalizePath(_ path: String) -> String {
        guard path.count > 1, path.hasSuffix("/") else { return path }
        return String(path.dropLast())
    }

    /// An executable is either an absolute path or a bare command name. A bare
    /// name may not contain shell metacharacters, slashes, or whitespace.
    static func validateExecutable(_ value: String) throws -> String {
        guard !value.isEmpty, !value.contains("\0"), !value.contains("\n"), value.count <= 4096 else {
            throw LaunchConfigurationError.invalidExecutable(value)
        }
        if value.hasPrefix("/") { return value }
        guard value.range(of: "^[A-Za-z0-9][A-Za-z0-9._+-]*$", options: .regularExpression) != nil else {
            throw LaunchConfigurationError.invalidExecutable(value)
        }
        return value
    }

    static func validatePath(_ value: String, flag: String) throws -> String {
        guard !value.isEmpty, !value.contains("\0"), !value.contains("\n"), value.count <= 4096 else {
            throw LaunchConfigurationError.invalidPath(flag: flag, value: value)
        }
        if value.hasPrefix("/") { return value }
        guard value.range(of: "^[A-Za-z0-9][A-Za-z0-9._+/-]*$", options: .regularExpression) != nil else {
            throw LaunchConfigurationError.invalidPath(flag: flag, value: value)
        }
        return value
    }

    @discardableResult
    static func validateArgument(_ value: String) throws -> String {
        guard !value.isEmpty, !value.contains("\0"), !value.contains("\n"), value.count <= 4096 else {
            throw LaunchConfigurationError.invalidArgument(value)
        }
        return value
    }
}
