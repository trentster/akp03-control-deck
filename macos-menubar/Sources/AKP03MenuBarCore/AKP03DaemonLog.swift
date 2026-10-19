import Foundation

/// Reads the tail of the daemon's own log so the menu can report *why* the
/// supervised daemon failed to start, in the daemon's own words — never a guess
/// and never an open-ended "starting up…".
///
/// The supervisor points the child's stdout/stderr at
/// `~/Library/Logs/AKP03/daemon.log` (see `ServiceModel.daemonLogURL()`), and the
/// daemon already logs a clear line for a rejected configuration. This reader is
/// total: it never throws, and returns `nil` for a missing, empty, or unreadable
/// file. Only the final `maxBytes` are read, so a large log costs one small read.
public enum AKP03DaemonLog {
    /// The last non-empty line of the log at `url`, trimmed and length-bounded,
    /// or `nil` when there is nothing honest to show.
    public static func lastLine(at url: URL?, maxBytes: Int = 8192) -> String? {
        guard let url, let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        guard let size = try? handle.seekToEnd() else { return nil }
        let window = UInt64(max(maxBytes, 1))
        let start = size > window ? size - window : 0
        guard (try? handle.seek(toOffset: start)) != nil,
              let data = try? handle.readToEnd(),
              !data.isEmpty else { return nil }
        let text = String(decoding: data, as: UTF8.self)
        // Split on the raw newline characters so CRLF logs split too (Swift folds
        // CR LF into one grapheme). Take the last line that is not blank.
        let lines = text.split(whereSeparator: { $0 == "\n" || $0 == "\r" })
        guard let last = lines.last(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty }) else {
            return nil
        }
        let trimmed = String(last).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        return String(trimmed.prefix(400))
    }
}
