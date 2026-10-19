import Foundation

/// The exact, machine-checkable result line protocol for `AKP03KeyInject`.
///
/// Exactly one compact JSON object is written to stdout per command, and nothing
/// else is ever written to stdout (diagnostics go to stderr). The Python side
/// (`akp03/backend.py`) parses these exact field names and values, so this type
/// is the single source of truth for every emitted line.
///
///   {"result":"ok","command":"tap","chord":"cmd+z"}
///   {"result":"dry-run","command":"hold","chord":"ctrl+shift+space","would_post":["keyDown ctrl",…]}
///   {"result":"error","command":"tap","reason":"unsupported key: 'semi;colon'"}
///   {"result":"pong","accessibility":true,"dry_run":false}
public enum AKP03KeyInjectLine {
    /// The reason string the process reports when Accessibility is not granted.
    /// The Python side recognises this exact text so it can log one actionable
    /// message instead of a cryptic failure per press.
    public static let accessibilityReason = "accessibility permission not granted"

    public static func ok(command: String, chord: String?) -> String {
        guard let chord else {
            return #"{"result":"ok","command":"\#(jsonEscaped(command))"}"#
        }
        return #"{"result":"ok","command":"\#(jsonEscaped(command))","chord":"\#(jsonEscaped(chord))"}"#
    }

    public static func dryRun(command: String, chord: String, wouldPost: [String]) -> String {
        let steps = wouldPost.map { "\"\(jsonEscaped($0))\"" }.joined(separator: ",")
        return #"{"result":"dry-run","command":"\#(jsonEscaped(command))","chord":"\#(jsonEscaped(chord))","would_post":[\#(steps)]}"#
    }

    public static func error(command: String?, reason: String) -> String {
        guard let command else {
            return #"{"result":"error","reason":"\#(jsonEscaped(reason))"}"#
        }
        return #"{"result":"error","command":"\#(jsonEscaped(command))","reason":"\#(jsonEscaped(reason))"}"#
    }

    public static func pong(accessibility: Bool, dryRun: Bool) -> String {
        #"{"result":"pong","accessibility":\#(accessibility ? "true" : "false"),"dry_run":\#(dryRun ? "true" : "false")}"#
    }

    /// Minimal JSON string escaping so a diagnostic can never break a line.
    static func jsonEscaped(_ string: String) -> String {
        var output = String()
        output.reserveCapacity(string.count)
        for scalar in string.unicodeScalars {
            switch scalar {
            case "\"": output += "\\\""
            case "\\": output += "\\\\"
            case "\n": output += "\\n"
            case "\r": output += "\\r"
            case "\t": output += "\\t"
            default:
                if scalar.value < 0x20 {
                    output += String(format: "\\u%04x", scalar.value)
                } else {
                    output.unicodeScalars.append(scalar)
                }
            }
        }
        return output
    }

    public static let usage = """
    Usage: AKP03KeyInject [--dry-run] [--request-accessibility]

    Reads one command per line on stdin and writes one result line per command on
    stdout as compact JSON. Diagnostics go to stderr. It posts keyboard events,
    and for the six media keys the system-defined events macOS listens for
    (CGEvent, .cghidEventTap): it never posts mouse events, clicks, or scrolls,
    never runs a shell, never injects arbitrary text, and never monitors input.

    Commands:
      ping                     reply {"result":"pong","accessibility":BOOL,...}
      quit                     release everything held and exit 0
      tap   CHORD              press and release CHORD once
      hold  CHORD              press and keep CHORD held until released
      release CHORD            release everything held
      media NAME               one press of a media key: mute, sound-down,
                               sound-up, play-pause, next, previous

    A CHORD is '+'-joined canonical tokens, e.g. cmd+z, ctrl+shift+space. The
    accepted tokens are the configuration model's own: ctrl, alt, shift, cmd,
    space, tab, enter, escape, backspace, delete, insert, home, end, pageup,
    pagedown, left, right, up, down, a-z, 0-9, and f1-f12. Any other token is
    rejected rather than guessed.

    --dry-run   parse, validate, and print exactly what would be posted, but post
                nothing. Safe for automated verification.
    --help, -h  print this usage text and exit 0

    Safety: at most one chord is held at a time; a second hold is refused and an
    unmatched release is refused; the process releases every held key on quit,
    EOF, SIGINT, SIGTERM, and any error before exiting.
    """
}
