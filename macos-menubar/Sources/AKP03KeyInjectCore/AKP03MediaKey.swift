import Foundation

/// The media functions a Mac keyboard sends: the six the configuration model may
/// name, and the system-defined event code macOS listens for.
///
/// They are deliberately *not* in ``AKP03KeyTable``.  A media key has no virtual
/// keycode: a synthetic `f12` reaches macOS as the plain F12 function key and
/// moves nothing, which is why a configuration that named F12 for "volume up"
/// only did anything while a volume-aware panel happened to be in front.  macOS
/// delivers these as system-defined (`NX_SYSDEFINED`) events carrying an
/// `NX_KEYTYPE_*` code — the values below are the ones in
/// `IOKit/hidsystem/ev_keymap.h` on every macOS.
public enum AKP03MediaKey: String, CaseIterable, Sendable {
    case mute
    case soundDown = "sound-down"
    case soundUp = "sound-up"
    case playPause = "play-pause"
    case next
    case previous

    /// `NX_KEYTYPE_*` from `IOKit/hidsystem/ev_keymap.h`.
    public var keyType: UInt16 {
        switch self {
        case .soundUp: 0      // NX_KEYTYPE_SOUND_UP
        case .soundDown: 1    // NX_KEYTYPE_SOUND_DOWN
        case .mute: 7         // NX_KEYTYPE_MUTE
        case .playPause: 16   // NX_KEYTYPE_PLAY
        case .next: 17        // NX_KEYTYPE_NEXT
        case .previous: 18    // NX_KEYTYPE_PREVIOUS
        }
    }

    /// The `data1` for one press or release: the key code in the high 16 bits,
    /// `NX_KEYDOWN` / `NX_KEYUP` (0x0A00 / 0x0B00) in the low 16.  This is the
    /// layout the window server reads, not an invention of this helper.
    public func eventData1(down: Bool) -> Int {
        (Int(keyType) << 16) | (down ? 0x0A00 : 0x0B00)
    }

    /// What a press and release would post, for the dry-run line.
    public var postDescription: [String] {
        ["systemDefined \(rawValue) down", "systemDefined \(rawValue) up"]
    }
}
