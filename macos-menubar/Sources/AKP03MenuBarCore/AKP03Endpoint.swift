import Foundation

/// The editor URL and its fixed read-only health endpoint.
///
/// The companion accepts only an HTTP loopback URL so it cannot be aimed at a
/// remote service. It uses this value only for a GET probe and browser opening.
public struct AKP03Endpoint: Equatable, Sendable {
    public static let defaultEditorURL = URL(string: "http://127.0.0.1:17865/")!

    public let editorURL: URL

    public init(editorURL: URL) throws {
        guard editorURL.scheme?.lowercased() == "http" else {
            throw EndpointError.requiresHTTP
        }
        guard let host = editorURL.host?.lowercased(), Self.isLoopbackHost(host) else {
            throw EndpointError.requiresLoopbackHost
        }
        guard editorURL.user == nil, editorURL.password == nil,
              editorURL.query == nil, editorURL.fragment == nil else {
            throw EndpointError.requiresSimpleBaseURL
        }
        self.editorURL = editorURL
    }

    public var healthURL: URL {
        editorURL.appending(path: "api/state")
    }

    /// The daemon's read-only live status feed (Server-Sent Events).
    public var eventsURL: URL {
        editorURL.appending(path: "api/events")
    }

    public static func fromCommandLine(arguments: [String]) throws -> AKP03Endpoint {
        var suppliedURL: URL?
        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            switch argument {
            case "--url":
                index += 1
                guard index < arguments.count, let url = URL(string: arguments[index]) else {
                    throw EndpointError.missingURLValue
                }
                suppliedURL = url
            case "--help", "-h":
                throw EndpointError.helpRequested
            default:
                throw EndpointError.unrecognizedArgument(argument)
            }
            index += 1
        }
        return try AKP03Endpoint(editorURL: suppliedURL ?? Self.defaultEditorURL)
    }

    private static func isLoopbackHost(_ host: String) -> Bool {
        host == "127.0.0.1" || host == "localhost" || host == "::1"
    }
}

public enum EndpointError: LocalizedError, Equatable {
    case requiresHTTP
    case requiresLoopbackHost
    case requiresSimpleBaseURL
    case missingURLValue
    case unrecognizedArgument(String)
    case helpRequested

    public var errorDescription: String? {
        switch self {
        case .requiresHTTP:
            return "--url must use http://."
        case .requiresLoopbackHost:
            return "--url must use a loopback host (127.0.0.1, localhost, or ::1)."
        case .requiresSimpleBaseURL:
            return "--url must be a base URL without credentials, a query, or a fragment."
        case .missingURLValue:
            return "--url requires a URL value."
        case let .unrecognizedArgument(argument):
            return "Unrecognized argument: \(argument)"
        case .helpRequested:
            return "Usage: AKP03MenuBar [--url http://127.0.0.1:17865/]"
        }
    }
}
