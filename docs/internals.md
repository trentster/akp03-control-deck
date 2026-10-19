# AKP03 control deck

`akp03d` is the automation daemon. On production startup it selects a keyboard
backend for the host platform. On Linux it uses the narrow `wtype` backend only
when both `XDG_SESSION_TYPE=wayland` and `WAYLAND_DISPLAY` are present and
`wtype` is on `PATH`. On macOS it uses the `AKP03KeyInject` helper (see
`macos-menubar/`), which posts `CGEvent` keyboard events for both `keys` (tap)
and `keys_hold` (press-and-hold) and reports its Accessibility (TCC) status
honestly; without the grant it posts nothing and logs one actionable error
instead of a cryptic failure per press. When no backend applies, keyboard
actions fail closed with an explicit `akp03d.keys` error; they are not reported
as successful injections. `LogBackend` remains the explicit test backend.

## The editor

The daemon also serves a local web page for setting the deck up, which is how
most people use it. It listens on loopback only - nothing on the network can
reach it - and `--editor-port` chooses the port. There are three pages, all of
them editing **one draft**:

- **Deck** - what each of the six LCD keys, the three buttons underneath, and the
  three dials does. A key can be pressed and held (`hold`) or fired once (`tap`);
  a dial has a slot for its press and for each way it turns, and its press alone
  cannot be held.
- **Menus** - nested pages of keys, what the three buttons do while a menu is
  open, and which parts of the big and right dials a menu claims. The left dial's
  press always goes back out of a menu, so it is stated rather than offered.
- **Icons** - the pictures a key can show, in the sets that ship with the app and
  the sets you make.

Nothing is written until you press **Save to deck** in the header, which saves
everything you changed, whichever page each change was on. **Discard** throws the
whole draft away; inside a control's editor, **Done** closes it and **Cancel** puts
back just that control. A keyboard shortcut can be tested before saving with
**Test it now**.

A deck-wide setting - not one about a single control - lives behind **Settings**
at the foot of the left rail. Anything about one control stays in that control's
own panel.

## The icon library

The pictures a key shows come from the icon library. Two sets ship with the app -
**General** and **macOS**, 24 icons each - and they are read-only on purpose:
copy one into a set of your own and change it there. Copying is what "of your
own" means, so a built-in icon offers exactly one action, `Copy…`.

Your own sets live under `~/.config/akp03/icons/` as plain PNG, JPEG or WebP
files, one file per icon, and each is used **exactly as it is** - never redrawn,
resampled or recoloured. A set can be exported as a `.zip` and imported back, so
a set can move between machines. Adding pictures means choosing them or dropping
them on the page.

## Sleeping the keys

The six LCD keys are backlit, and a panel left lit all day keeps the same picture
on the glass. After `[screen] blank_after_s` seconds with no key, button or dial
touched, the daemon **clears all six keys** and leaves the backlight alone; any
touch brings back exactly what was there, a menu included, on the item it was open
at. A key that is being held never blanks.

```toml
[screen]
blank_after_s = 900   # 15 minutes; 0 keeps the keys lit
```

The default is 900. It is a setting rather than code, so it can be changed in
**Settings** in the editor and takes effect on the next save - no daemon restart.
The keys are cleared rather than dimmed because the firmware ignores a brightness
of zero, and a dimmed panel would not save the glass anyway.

## macOS readiness preflight (discovery only)

On macOS, run the package command (or `python -m akp03.preflight`) before any
future plugged-in test:

```sh
akp03-preflight
```

It lazily uses the optional `hid`/hidapi Python module if present; it is not a
base installation dependency. For the dedicated Mac readiness environment, use
the optional `macos-hid` extra (for example `pip install -e '.[macos-hid]'`).
It enumerates only and accepts a candidate only when VID `0x0300`, PID `0x3002`, **and** the enumerated usage page `0xffa0` match.
An entry with no usage page is rejected rather than guessed. The command reports
exactly one of: hidapi unavailable, no matching vendor interface, exactly one
candidate, or ambiguous candidates.

The preflight never opens, reads, writes, configures, uploads to, or sends data
to a HID device; it never injects keys or invokes a shell. A plugged-in physical
test is allowed only after it reports exactly one candidate, and that next test
must be explicit and non-destructive. This is **not** a macOS driver: the Linux
hidraw transport (`/dev/hidraw` and `/sys`) remains Linux-only, and macOS event,
read, and write support is unported and unverified.

## macOS bounded read-only event probe

After preflight reports exactly one **physical interface path** (macOS can list
two HID usage collections for that one path), a separately explicit physical
trial may use:

```sh
akp03-macos-probe --seconds 10
# or: python -m akp03.macos_probe --seconds 10
```

`--seconds` is required and accepts only whole values from 1 through 30; there
is no continuous-monitoring default. The probe runs preflight itself and refuses
before constructing or opening a hidapi device unless discovery still finds
exactly one physical path. It then opens that one opaque path, enables
nonblocking reads, reads input reports only for the requested interval, and
always closes it. It never invokes the Linux transport, `Deck`, daemon, service,
configuration, key injection, HID output reports, feature reports, or any write,
brightness, clear, upload, keepalive, or wake operation.

The Linux control mapping is already known; this macOS stage validates transport
only and does not rediscover or validate that mapping. Known reports are decoded
into sanitized `control`, `kind`, `press`, `delta`, and report-length lines.
Unknown reports are counted but their contents are not printed. A successful
process only means that this bounded read-only operation completed; **it does not
verify any macOS event control** until a physical run is performed and reviewed.
Do not run this command until Mark authorizes that physical trial.

## Install and service setup

```sh
python -m venv ~/akp03/.venv
~/akp03/.venv/bin/pip install --no-deps --editable ~/akp03/driver
install -D -m 0644 ~/akp03/driver/deploy/akp03d.service ~/.config/systemd/user/akp03d.service
systemctl --user daemon-reload
akp03d --init-config
chmod 600 ~/.config/akp03/config.toml
# Review the config before separately enabling or starting the service.
```

The TOML config is mandatory and is rejected unless it is a regular mode-`0600` file; a genuinely missing file is bootstrapped on first run (see below). The service and daemon both use the retained control socket at `$XDG_RUNTIME_DIR/akp03.sock` (or `~/akp03/run/akp03.sock` without `XDG_RUNTIME_DIR`), with mode `0600`.

### First run: a missing config is bootstrapped, a broken one still fails

The config path (`~/.config/akp03/config.toml` by default) does **not** have to
exist before the daemon starts. When it is **missing**, the daemon writes the
same starter file `akp03d --init-config` produces, logs that decision exactly
once (`no config at …; wrote the starter config for first-run setup`), and
carries on so it comes up and hosts the editor immediately. This is why the
macOS menu-bar app, which launches the daemon with default paths, works on a
fresh Mac: launch the app, get a working editor, configure the deck, save.

That bootstrap is deliberately narrow. A config that **exists** but is
unreadable, malformed, or fails the validators is a different case: the daemon
fails loudly with a clear `configuration rejected: …` error and exits non-zero.
It is **never** bootstrapped or overwritten, so an edit you got wrong is never
hidden and your work is never destroyed. The two cases are separate code paths:
`akp03.config.ConfigNotFound` (missing, bootstrap) versus
`akp03.config.ConfigError` (present but broken, fail).

The socket accepts only these daemon controls:

```text
status
reload
paint KEY TEXT [BG] [FG]
unpaint KEY
```

`KEY` must be `0` through `5`; `TEXT` is 1–32 characters; `BG` and `FG` are Pillow colour values. `paint` and `unpaint` never accept action, key-injection, image-file, shell, HTTP, or arbitrary command requests.

The keyboard backend accepts only canonical configured shortcut keys. `cmd` is
translated to wtype's `logo` (Super) modifier; `ctrl`, `alt`, `shift`, letters,
digits, F1–F12, and the documented navigation/editing keys are fixed mappings.
It invokes `wtype` with an argv list, never a shell. `keys_hold` is deliberately
rejected by this backend: wtype destroys and releases its virtual keyboard when
its process exits, so a cross-event hold would be a false promise. The daemon
logs the failure and leaves no held state.

Configured shell actions use a strict executable argv list and never invoke shell parsing. They run asynchronously, time out after 10 seconds, and no more than four can run concurrently; additional action events are rejected and logged.

```toml
[bindings.top]
action = "shell"
shell = ["/usr/bin/notify-send", "AKP03 top knob"]
```

HTTP actions are also asynchronous, time out after 5 seconds, and log status/duration without request or response bodies:

```toml
[bindings.bottom_left]
action = "http"
method = "POST"
url = "https://example.invalid/hook"
json = { source = "akp03" }
headers = { Authorization = "env:AKP03_WEBHOOK_TOKEN" }
```

Header values using `env:NAME` resolve only from the daemon environment. Store secret values only in `~/.config/akp03/env` with mode `0600`; the service loads it through `EnvironmentFile`. Do not put secret values in TOML. Action logs redact environment values; HTTP logs never include headers, URLs, request data, or response bodies.

Physical mappings remain position-based: `top` is raw `0x51/0x50`, `bottom_left` is `0x91/0x90`, and `bottom_right` is `0x61/0x60`. The separate lower buttons are left/first = internal `button6` hold-to-talk, middle = `button8` in the historical numeric ordering, and right = `button7`; their key bindings are configuration data and are not remapped by the daemon.

Static key labels are repainted on reconnect and saved atomically to `~/.local/state/akp03/keys.json` with mode `0600`.

Each LCD key shows the option assigned to it. On load the daemon derives the text from the key's
binding: a key bound to a brightness action shows `Brightness N%`, and a key bound to any other named
action shows that action's name as the editor presents it (`new-window` → `New Window`, `undo` →
`Undo`). A key with no binding keeps its configured `[labels] keyN` text, and a key with neither is
left blank. The derivation is never written back to the configuration — `[labels]` keeps round-tripping
untouched — and because it is recomputed when a save is applied, the key repaints with its new
assignment the moment the editor saves. When the Action library saves a direct LCD-key assignment it
still writes that safe action ID (for example, `dim`) to the matching `[labels] keyN` entry, which
remains the fallback text for an unassigned key. LCD labels are limited to 32 characters, so an action
ID longer than that is rejected rather than silently shortened. This does not enable the menu or press
feedback.

On Linux the key image is written straight to `/dev/hidraw`. macOS has no hidraw, so the daemon sends
the same packets to the `AKP03HIDStream` helper, which owns the open device and writes them as
report-ID-0 output reports (see `macos-menubar/README.md`). A paint the helper refuses or that fails is
logged once per distinct reason and never kills the daemon or the input stream; while a paint is
failing, the editor's `GET /api/state` reports `screens_updated: false` with the reason instead of
implying the deck was updated.

## Omarchy Menu Library

The Action Library can create a named **Omarchy menu** action and assign it to an
LCD key like any other safe action. Its in-app, browsable catalogue groups the
available navigation menus and shows each menu path and description. The first
safe catalogue contains **Go…** (`root`) plus the primary submenu routes:
**Apps, Learn, Trigger, Style, Setup, Install, Remove, Update,** and **System**.

The GUI writes only a named action such as:

```toml
[actions.omarchy-go]
action = "omarchy_menu"
route = "root"

[bindings.key0]
action_id = "omarchy-go"
```

`route` is a closed, application-owned allowlist, not a command or free-form
Omarchy path. Inline `omarchy_menu` bindings are rejected: bind a reviewed named
action via `action_id`. On activation, the daemon inherits its existing Wayland
session environment and invokes exactly `/usr/bin/omarchy-menu summon ROUTE` as
an argv list with `shell=False`; it does not inject a virtual keyboard shortcut.
This first version intentionally exposes navigation menus only, never individual
leaf actions such as shutdown or suspend. Extending it requires adding a reviewed
non-destructive route to `akp03.omarchy_menu_catalogue`.

## Custom LCD menu profiles

The Action Library's menu workspace stores named menu profiles separately under
`[custom_menus.NAME]`. Assign its named `action = "menu"` action to an LCD key;
pressing that key enters the profile root and redraws all six keys. The first
four keys are explicit page content. The final two keys are always visibly
labelled **Back** and **Home**, so navigation never depends on a hidden mode or
a dial. Home returns to the menu root; Back returns one page.

Menu content can reference only named GUI-safe `keys`, `brightness`, or reviewed
`omarchy_menu` actions, or another page. The GUI never creates shell, HTTP, or
held-key actions. The validator rejects malformed pages, unknown pages/actions,
duplicate or reserved content controls, cycles, and invalid labels. Existing
`[menu]` remains the independent legacy hardware-demo compatibility feature.

The built-in editor template is inert: **YouTube → Playback → Rewind / Play /
Pause / Forward**. It is only a hierarchy example until its named safe actions
are explicitly assigned; it does not add media integrations or dial controls.

A menu may also take over the three buttons under the LCD keys for as long as it
is open, under `[custom_menus.NAME.controls]`. A button a menu does not name keeps
its ordinary binding, so an action such as hold-to-talk still works with a menu on
the glass. Only those three buttons can be claimed: the six LCD keys are the menu's
content and the dials are its navigation. A claim names a GUI-safe action, written
as a bare action name for a tap, or as a table when the button is held down until it
is let go - the one place a menu may hold a chord, because a button under the keys
can be pressed and held.

```toml
[custom_menus.youtube.controls]
button6 = "play_pause"                                 # left, tapped
button8 = { action_id = "dictate", mode = "hold" }     # middle, held down
# button7 (right) is not named here, so it keeps its ordinary deck binding.
```

```toml
[actions.youtube]
action = "menu"
menu = "youtube"
[bindings.key0]
action_id = "youtube"
[custom_menus.youtube]
root = "home"
[[custom_menus.youtube.pages]]
id = "home"
title = "YouTube"
items = [{ key = 0, label = "Playback", submenu = "playback" }]
```

## Optional LCD press feedback

LCD-key feedback is disabled by default, affects only the six LCD keys (`key0` through `key5`), and never changes a key's configured action. Enable it explicitly for a physical trial:

```toml
[feedback]
enabled = true
duration_ms = 180 # integer, 150 through 2000
```

After saving the configuration, restart the daemon: `systemctl --user restart akp03d.service`.

On press, the daemon draws a brief local dimming pulse over that key's current visual, then restores the same base visual after the configured duration. Plain buttons and knobs receive no feedback. Pending feedback is cancelled when that key is painted/unpainted, the configuration reloads, or the deck reconnects, so an old timer cannot restore stale content.

## Optional physical menu demo

The physical menu demo is also disabled by default and does not alter the three lower-button bindings. It is a safe hardware-control check before real menu actions are added:

```toml
[menu]
enabled = true
```

- Turn the top knob to move the highlighted item.
- Press the top knob or a visible LCD key to enter/select.
- Press the bottom-left knob to go back.
- The Display submenu changes only the deck brightness (25, 50, or 75); other leaf selections only log an action and never run shell, HTTP, or key actions.

### Persisted custom menu definitions

When `menu.enabled` is true, omitting `pages` preserves the built-in demo. To replace it,
declare named actions under `[actions.ID]`, then reference their IDs from menu items or
physical bindings. Action IDs match `[a-z][a-z0-9_-]{0,63}`. Each action uses the same
strict schema as a legacy inline binding: `keys`, `keys_hold`, `brightness`, `shell`,
`http`, or `none`; named actions may additionally use the closed `omarchy_menu` route
catalogue. A menu item has a non-empty `label` and exactly one of `submenu` or an
action ID; it never embeds key values, shell argv, HTTP URLs, or other executable values.
`keys_hold` is rejected for menus because a menu activation has no safe matching release.

```toml
[actions.copy]
action = "keys"
keys = ["ctrl", "c"]

[actions.dim]
action = "brightness"
brightness = 25

[actions.notify]
action = "shell"
shell = ["/usr/bin/notify-send", "AKP03 selected"]

[bindings.top]
action_id = "copy"

[menu]
enabled = true
root = "home"

[[menu.pages]]
id = "home"
title = "Home"
items = [
  { label = "Copy", action = "copy" },
  { label = "Dim", action = "dim" },
  { label = "Notify", action = "notify" },
]
```

An assignment can hold a chord instead of tapping it: add `mode = "hold"` beside the
binding. The chord goes down when the control is pressed and comes up when it is let go,
which is what a push-to-talk button needs. The mode belongs to the binding and not the
action, so one action can be tapped on one control and held on another. It is only
meaningful for a `keys` action; `keys_hold` remains the older way of saying the same
thing and still works in hand-written configs. A held chord is released automatically if
it is still down after `deck.max_hold_s` (default 120 seconds), as protection against a
stuck key.

```toml
[actions.dictation]
action = "keys"
keys = ["ctrl", "shift", "space"]

[bindings.button6]
action_id = "dictation"
mode = "hold"            # held down while the button is held

[bindings.key0]
action_id = "dictation"  # the same action, tapped
```

Unknown action IDs, malformed IDs, unknown action fields, inline fields mixed with a
binding `action_id`, menu references to `keys_hold`, cycles, unknown pages, and duplicate
page IDs are rejected at config load. Existing inline bindings continue to work. For
compatibility, existing menu `brightness:1` through `brightness:100` tokens continue to
work, but new configurations should use named actions.

### Custom LCD images

The Action library Inspector has a built-in **Starter set** chooser. The initial **General-purpose** set contains **Dim, Bright, Copy, Paste, Voice,** and **Undo**, alongside **Choose image…** and **Remove image** for the selected LCD key. Select a starter and press **Apply starter artwork**, or choose a local image, then save the deck. Applying a starter changes artwork for the selected tile only: it never creates actions, hotkeys, bindings, or menu items. No file browsing or configuration setup is needed for the built-in set. The source path is never stored: the GUI normalizes and copies either source into the daemon-owned directory beside the chosen config file, as `images/keyN.jpg` (`key0.jpg` through `key5.jpg`). PNG, JPEG, WebP, BMP, GIF, and TIFF work when Pillow can decode them.

Each stored asset is an RGB JPEG in the source's visual orientation, fitted within
60×60 with black letterboxing. `akp03.images.to_jpeg` is the one normalization
and orientation boundary; `Deck.set_key_image` uploads those source-oriented
pixels without a second rotation. The GUI canvas previews the same normalized
JPEG that the physical key receives. Artwork is the base visual and deliberately
supersedes the automatic action-ID label; the label remains as a semantic
fallback in config. The daemon reloads and reconnects to the same base art, and
configured press feedback dims that exact image then restores it. An enabled
menu temporarily renders its menu labels instead; leaving the menu via
reload/reconnect returns the base image.

`[images]` is intentionally closed: it may contain only `key0` through `key5`, and every value must be exactly the matching basename, e.g. `key2 = "key2.jpg"`. Image files and the `images` directory are managed by the GUI (`0600` files, `0700` directory); do not hand-edit source paths into TOML. Removing an image first atomically removes its configuration reference, then deletes its managed asset. Configurations with no `[images]` keep the existing text-label behaviour.

### One-off migration of legacy rotated managed images

Managed files written by the old GUI were stored clockwise-rotated. After
installing this version, run the following **once**, while the daemon is stopped:

```sh
akp03-migrate-images --config ~/.config/akp03/config.toml
```

It needs no original source paths. It only processes configured managed images,
requires the existing `0700` image directory and `0600` image files, writes
replacement files with mode `0600`, and first copies every original into a unique
private `images/legacy-cw-YYYYMMDDTHHMMSSZ/` directory (`0700`). It refuses to
reuse a backup directory. Keep that backup until a physical visual check confirms
all tiles are upright.

### Reproducible physical Wayland verification

On the target Hyprland desktop, with the service stopped and no deployed config
changed, run `tools/verify-wayland-keyboard.sh /dev/hidrawN`. It creates a private
mode-`0600` temporary config binding only LCD key 1 to inert `F20`, starts a
separate daemon process, and opens a focused native `wev` detector window. Press
the physical key once. The probe passes only if `wev` records `sym: F20` and the
temporary daemon has no keyboard-backend failure. It requires `wev`, `wtype`, and
an active Wayland session; it changes neither the user service nor its config.


## Portable menu-navigation core

`akp03.menu` is a pure-Python, UI-independent navigation core. It does not open a
device, read daemon configuration, or start a service. Build an immutable hierarchy
of `MenuPage` and `MenuItem` values, then pass its root to `Menu`:

```python
from akp03.menu import Menu, MenuItem, MenuPage

menu = Menu(MenuPage(
    title="Home",
    items=(
        MenuItem("Record", action="record"),
        MenuItem("Settings", submenu=MenuPage(items=(
            MenuItem("Brightness", action="brightness"),
        ))),
    ),
))
```

Pages contain one through six items. Each item has a non-empty label and exactly one
of a non-empty action token or a submenu. Invalid trees and navigation inputs raise
`MenuValidationError`. The public `menu.state` is an immutable `MenuState` containing
`title`, exactly six padded `labels`, and `selected_index`, ready for a renderer.

- `top_clockwise()` and `top_counterclockwise()` move selection with wrapping.
- `top_press()` activates the selected item.
- `bottom_left_press()` returns to the parent (and is a no-op at root).
- `lcd_press(0)` through `lcd_press(5)` activate visible LCD-key items directly.

Press methods return an immutable `Activation`: its `state` is the resulting render
state and its `action` is the action token for leaf items, otherwise `None` for a
submenu transition or back navigation. LCD-key activation does not alter the current
selection before activation.
