import Foundation

/// The editor endpoint plus the validated daemon launch configuration, parsed
/// together from one argv so the two parsers cannot disagree about a flag.
public struct AKP03CompanionArguments: Sendable {
    public let endpoint: AKP03Endpoint
    /// `nil` when the launch command could not be resolved; `launchError` then
    /// explains why, and the app runs without starting a daemon.
    public let launch: AKP03LaunchConfiguration?
    public let launchError: String?

    public init(endpoint: AKP03Endpoint, launch: AKP03LaunchConfiguration?, launchError: String?) {
        self.endpoint = endpoint
        self.launch = launch
        self.launchError = launchError
    }

    /// Parse the app's command line. An endpoint error is fatal (as before); a
    /// launch-command error is captured rather than thrown, so the app can still
    /// show status and open the editor.
    public static func parse(
        arguments: [String],
        userDefaults: [String: Any] = [:],
        discoveredRepoRoot: String? = nil,
        discoveredBundle: BundledPayload? = nil,
        environment: [String: String] = [:],
        fileExists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) throws -> AKP03CompanionArguments {
        var suppliedURL: URL?
        var index = 0
        while index < arguments.count {
            let flag = arguments[index]
            switch flag {
            case "--help", "-h":
                throw EndpointError.helpRequested
            case "--url":
                index += 1
                guard index < arguments.count, let url = URL(string: arguments[index]) else {
                    throw EndpointError.missingURLValue
                }
                suppliedURL = url
            case "--repo-root", "--daemon-command", "--daemon-arg", "--config", "--socket",
                 "--editor-port", "--hidstream", "--keyinject":
                index += 1
                guard index < arguments.count else {
                    throw LaunchConfigurationError.missingValue(flag)
                }
            default:
                throw EndpointError.unrecognizedArgument(flag)
            }
            index += 1
        }

        let endpoint = try AKP03Endpoint(editorURL: suppliedURL ?? AKP03Endpoint.defaultEditorURL)
        let editorPort = endpoint.editorURL.port ?? AKP03LaunchConfigurationResolver.defaultEditorPort
        do {
            let launch = try AKP03LaunchConfigurationResolver.resolve(
                arguments: arguments,
                defaultEditorPort: editorPort,
                userDefaults: userDefaults,
                discoveredRepoRoot: discoveredRepoRoot,
                discoveredBundle: discoveredBundle,
                environment: environment,
                fileExists: fileExists
            )
            return AKP03CompanionArguments(endpoint: endpoint, launch: launch, launchError: nil)
        } catch {
            return AKP03CompanionArguments(endpoint: endpoint, launch: nil, launchError: error.localizedDescription)
        }
    }
}
