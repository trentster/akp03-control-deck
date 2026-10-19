import CoreGraphics

/// The explicit, closed keycode table for AKP03 key injection.
///
/// This mirrors the *decision* the Linux `wtype` backend already makes
/// (`akp03/backend.py`): the validated configuration may name only these
/// canonical tokens, and nothing else is accepted. The macOS shape differs only
/// in the numbers: each token becomes a fixed macOS virtual keycode, and the four
/// modifiers additionally carry a `CGEventFlags` bit.
///
/// The accepted vocabulary is exactly the configuration model's:
///
///   * modifiers: `ctrl`, `alt`, `shift`, `cmd`
///   * named: `space`, `tab`, `enter`, `escape`, `backspace`, `delete`,
///     `insert`, `home`, `end`, `pageup`, `pagedown`, `left`, `right`, `up`,
///     `down`
///   * simple: letters `a`-`z`, digits `0`-`9`, function keys `f1`-`f12`
///
/// Total: 4 + 15 + 26 + 10 + 12 = 67 canonical tokens.
///
/// A token that is not in this table is *rejected* (see `AKP03KeyChord.parse`),
/// never guessed. `cmd` is the platform-neutral Command key on macOS (it is the
/// `logo`/Super family on Linux), keeping one configuration vocabulary across
/// both platforms.
public enum AKP03KeyTable {
    /// One resolved key: its macOS virtual keycode, and — for the four
    /// modifiers only — the event flag that must be set while it is held.
    public struct Entry: Equatable, Sendable {
        public let keyCode: CGKeyCode
        public let modifierFlag: CGEventFlags?

        public init(keyCode: CGKeyCode, modifierFlag: CGEventFlags? = nil) {
            self.keyCode = keyCode
            self.modifierFlag = modifierFlag
        }

        public var isModifier: Bool { modifierFlag != nil }
    }

    // Modifier virtual keycodes (kVK_Command/Shift/Option/Control).
    private static let modifiers: [String: Entry] = [
        "ctrl": Entry(keyCode: 0x3B, modifierFlag: .maskControl),
        "shift": Entry(keyCode: 0x38, modifierFlag: .maskShift),
        "alt": Entry(keyCode: 0x3A, modifierFlag: .maskAlternate),
        "cmd": Entry(keyCode: 0x37, modifierFlag: .maskCommand),
    ]

    private static let named: [String: CGKeyCode] = [
        "space": 0x31, "tab": 0x30, "enter": 0x24, "escape": 0x35,
        "backspace": 0x33, "delete": 0x75, "insert": 0x72,
        "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
        "left": 0x7B, "right": 0x7C, "up": 0x7E, "down": 0x7D,
    ]

    private static let letters: [String: CGKeyCode] = [
        "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05,
        "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C,
        "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11, "o": 0x1F,
        "u": 0x20, "i": 0x22, "p": 0x23, "l": 0x25, "j": 0x26, "k": 0x28,
        "n": 0x2D, "m": 0x2E,
    ]

    private static let digits: [String: CGKeyCode] = [
        "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17, "6": 0x16,
        "7": 0x1A, "8": 0x1C, "9": 0x19, "0": 0x1D,
    ]

    private static let functionKeys: [String: CGKeyCode] = [
        "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61,
        "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    ]

    /// Resolve one canonical token. Returns `nil` for anything not in the table,
    /// so a caller rejects rather than invents a keycode.
    public static func lookup(_ token: String) -> Entry? {
        if let modifier = modifiers[token] { return modifier }
        if let code = named[token] { return Entry(keyCode: code) }
        if let code = letters[token] { return Entry(keyCode: code) }
        if let code = digits[token] { return Entry(keyCode: code) }
        if let code = functionKeys[token] { return Entry(keyCode: code) }
        return nil
    }

    /// The exact number of canonical tokens the table accepts; the check suite
    /// pins this so a silent vocabulary change fails verification.
    public static var supportedTokenCount: Int {
        modifiers.count + named.count + letters.count + digits.count + functionKeys.count
    }
}
