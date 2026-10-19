import AKP03ControlServiceCore
import Foundation

/// Exact, machine-checkable JSON line protocol for `AKP03HIDStream`.
///
/// One compact JSON object per line, no trailing commas and no pretty-printing.
/// A Python daemon parses these exact field names and value spellings, so this
/// type is the single source of truth for every emitted line. Nothing else may be
/// written to stdout by the streaming executable.
public enum AKP03HIDStreamLine {
    public static let starting = #"{"event":"starting"}"#
    public static let waiting = #"{"event":"waiting"}"#
    public static let ready = #"{"event":"ready"}"#
    public static let deviceLost = #"{"event":"device-lost"}"#
    public static let unknown = #"{"event":"unknown"}"#

    /// One decoded known control. A control with a documented press/release state
    /// carries `"state":"press"` or `"state":"release"`; a control that has no such
    /// state (a knob turn) omits `state` entirely rather than guessing one.
    public static func control(control: AKP03KnownControl, pressed: Bool?) -> String {
        guard let pressed else {
            return #"{"event":"control","control":"\#(control.rawValue)"}"#
        }
        let state = pressed ? "press" : "release"
        return #"{"event":"control","control":"\#(control.rawValue)","state":"\#(state)"}"#
    }

    public static func error(message: String) -> String {
        #"{"event":"error","message":"\#(jsonEscaped(message))"}"#
    }

    /// A deck is plugged in but could not be used: an acquisition attempt failed while the
    /// device was present.  This is not `waiting`, which means nothing is attached at all -
    /// the two call for different advice, and a reader that cannot tell them apart says
    /// "no AKP03 is plugged in" to someone who is looking straight at one.
    public static func blocked(message: String) -> String {
        #"{"event":"blocked","message":"\#(jsonEscaped(message))"}"#
    }

    /// Answers a `paint` command that was written to the device. Exactly one
    /// result line is written for every command line the helper reads; it is the
    /// only stdout line that carries a `result` field instead of an `event` field,
    /// so the two message families can never be confused.
    public static func paintResult(bytes: Int) -> String {
        #"{"result":"ok","command":"paint","bytes":\#(bytes)}"#
    }

    /// Answers a `paint` command that was refused or that failed to write. The
    /// reason is a short, stable string; the device was not changed.
    public static func paintError(reason: String) -> String {
        #"{"result":"error","command":"paint","reason":"\#(jsonEscaped(reason))"}"#
    }

    /// Minimal JSON string escaping so a short diagnostic can never break a line.
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
    Usage: AKP03HIDStream [--poll-seconds N] [--open-protocol MODE]

    Streams AJAZZ AKP03 deck input as newline-delimited JSON on stdout. It runs
    until SIGINT/SIGTERM and then exits 0 after releasing the device cleanly.
    Diagnostics are written to stderr.

    On each successful device acquisition it sends only fixed, audited
    open-protocol packets — never any HID feature or device-value write:

      wake (default)  send only the fixed wake packet CRT\\0\\0DIS
      full            send the four fixed Linux Deck.open() packets in order
                      (DIS, LIG+50, CLE+0xff, STP), including a one-time LCD clear
      none            send nothing at all

    In wake and full it also sends the fixed CRT\\0\\0CONNECT keepalive 10 seconds
    after open and every 10 seconds while the device session is active.

    It also reads one JSON command per line on its OWN stdin (its parent, the
    local daemon) and writes one JSON result line per command on stdout. The only
    command is:

      {"command":"paint","payload":"<base64 of exactly 1024 bytes>"}
        -> {"result":"ok","command":"paint","bytes":1024}
        -> {"result":"error","command":"paint","reason":"..."}

    A paint payload is written to the device as one report-ID-0 output report
    through the same single audited sink as the fixed packets. It is refused
    unless it is exactly 1024 bytes of valid base64; there is no other command, no
    payload from a command-line argument, socket, file, or the network, and no
    feature-report or device-value write. Nothing else is ever written to stdout.

    --poll-seconds N     device-presence retry interval in seconds (default 2, clamped to 1...60)
    --open-protocol MODE fixed open-protocol mode: wake (default), full, or none
    --help, -h           print this usage text and exit
    """
}
