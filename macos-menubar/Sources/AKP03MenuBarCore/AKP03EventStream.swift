import Foundation

/// One Server-Sent Event frame from the daemon's `/api/events` feed.
public struct AKP03ServerSentEvent: Equatable, Sendable {
    public let event: String
    public let data: String

    public init(event: String, data: String) {
        self.event = event
        self.data = data
    }
}

/// Minimal, device-free SSE line parser. It handles multi-line `data`, the
/// `event` field, and comments / keepalives (`: keepalive`), and dispatches a
/// frame on the blank line that ends it — exactly what the daemon writes.
public struct AKP03ServerSentEventParser: Sendable {
    private var event: String?
    private var data: [String] = []

    public init() {}

    /// Feed one line without its trailing newline. Returns a frame when the line
    /// completes one.
    public mutating func consume(line: String) -> AKP03ServerSentEvent? {
        if line.isEmpty {
            defer { event = nil; data.removeAll() }
            guard let event, !data.isEmpty else { return nil }
            return AKP03ServerSentEvent(event: event, data: data.joined(separator: "\n"))
        }
        if line.hasPrefix(":") { return nil }
        guard let separator = line.firstIndex(of: ":") else {
            if line == "event" { event = "" }
            return nil
        }
        let field = String(line[line.startIndex..<separator])
        var value = String(line[line.index(after: separator)...])
        if value.hasPrefix(" ") { value.removeFirst() }
        switch field {
        case "event": event = value
        case "data": data.append(value)
        default: break
        }
        return nil
    }

    /// Feed one complete frame (the text between two blank lines) and dispatch it.
    ///
    /// Framing is done here rather than with `AsyncLineSequence`, because that
    /// sequence *drops* blank lines and therefore never lets a caller see the
    /// frame terminator. Lines are split on the raw `LF` byte, because Swift
    /// treats `CR LF` as a *single* grapheme cluster — so splitting on `"\n"` as
    /// a `Character` would silently not split a CRLF-framed frame at all.
    public mutating func consume(frame: String) -> AKP03ServerSentEvent? {
        let bytes = Array(frame.utf8)
        var start = 0
        var index = 0
        while index <= bytes.count {
            if index == bytes.count || bytes[index] == 0x0A {
                var end = index
                if end > start, bytes[end - 1] == 0x0D { end -= 1 }
                _ = consume(line: String(decoding: bytes[start..<end], as: UTF8.self))
                start = index + 1
            }
            index += 1
        }
        return consume(line: "")
    }
}

/// A change the live status feed reported.
public enum AKP03DeckStreamUpdate: Equatable, Sendable {
    case connected
    case deck(AKP03DeckStatus)
    case ended(String)
}

/// Reads the daemon's live `state` frames over SSE and reports them as they
/// change. It talks only to the validated loopback events URL and reconnects on
/// its own when the daemon restarts.
public final class AKP03EventStreamClient: @unchecked Sendable {
    private let url: URL
    private let session: URLSession
    private let lock = NSLock()
    private var task: Task<Void, Never>?
    private var handler: (@Sendable (AKP03DeckStreamUpdate) -> Void)?

    public init(url: URL, session: URLSession? = nil) {
        self.url = url
        if let session {
            self.session = session
        } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = 10
            configuration.timeoutIntervalForResource = 86_400
            self.session = URLSession(
                configuration: configuration,
                delegate: AKP03RejectRedirects.shared,
                delegateQueue: nil
            )
        }
    }

    public func start(handler: @escaping @Sendable (AKP03DeckStreamUpdate) -> Void) {
        lock.lock()
        self.handler = handler
        guard task == nil else { lock.unlock(); return }
        let task = Task.detached { [weak self] in
            guard let self else { return }
            await self.run()
        }
        self.task = task
        lock.unlock()
    }

    public func stop() {
        lock.lock()
        task?.cancel()
        task = nil
        handler = nil
        lock.unlock()
    }

    private var currentHandler: (@Sendable (AKP03DeckStreamUpdate) -> Void)? {
        lock.lock(); defer { lock.unlock() }
        return handler
    }

    private func run() async {
        while !Task.isCancelled {
            do {
                try await connectOnce()
            } catch is CancellationError {
                return
            } catch {
                currentHandler?(.ended(Self.concise(error)))
            }
            if Task.isCancelled { return }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
    }

    private func connectOnce() async throws {
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (bytes, response) = try await session.bytes(for: request)
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        currentHandler?(.connected)
        var parser = AKP03ServerSentEventParser()
        var buffer = [UInt8]()
        for try await byte in bytes {
            if Task.isCancelled { return }
            buffer.append(byte)
            // Guard against an unbounded buffer if the peer never frames anything.
            if buffer.count > 262_144 {
                buffer.removeAll(keepingCapacity: true)
                continue
            }
            // A complete SSE frame ends with a blank line: LF LF, or CR LF CR LF.
            // Detect it as bytes arrive, because the async line sequence drops
            // blank lines (and Swift folds CR LF into one grapheme).
            if buffer.count >= 2, buffer[buffer.count - 2] == 0x0A, buffer[buffer.count - 1] == 0x0A {
                buffer.removeLast(2)
                if buffer.last == 0x0D { buffer.removeLast() }
                frameComplete(&buffer, parser: &parser)
            } else if buffer.count >= 4,
                      buffer[buffer.count - 4] == 0x0D, buffer[buffer.count - 3] == 0x0A,
                      buffer[buffer.count - 2] == 0x0D, buffer[buffer.count - 1] == 0x0A {
                buffer.removeLast(4)
                frameComplete(&buffer, parser: &parser)
            }
        }
    }

    private func frameComplete(_ buffer: inout [UInt8], parser: inout AKP03ServerSentEventParser) {
        let text = String(decoding: buffer, as: UTF8.self)
        buffer.removeAll(keepingCapacity: true)
        if let frame = parser.consume(frame: text) { deliver(frame) }
    }

    private func deliver(_ frame: AKP03ServerSentEvent) {
        guard frame.event == "state" else { return }
        guard let object = try? JSONSerialization.jsonObject(with: Data(frame.data.utf8)) as? [String: Any],
              let status = AKP03DeckStatus(snapshot: object) else { return }
        currentHandler?(.deck(status))
    }

    private static func concise(_ error: Error) -> String {
        if let urlError = error as? URLError {
            return urlError.localizedDescription.isEmpty ? "connection failed" : urlError.localizedDescription
        }
        let message = (error as NSError).localizedDescription
        return message.isEmpty ? "connection failed" : message
    }
}

/// Refuses any redirect, so the loopback probe and stream can never be aimed at
/// another host by a hostile response.
public final class AKP03RejectRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    public static let shared = AKP03RejectRedirects()

    public func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping @Sendable (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}
