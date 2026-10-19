#!/bin/bash
# Build AKP03.app: a self-contained, installable menu-bar app, plus a .dmg for it.
#
# The app owns and supervises the Python daemon as a child process. Inside a real
# bundle there is no repository to discover, so the app carries everything it
# needs: a relocatable Python runtime, the `akp03` package, the web editor assets
# and the Swift helpers. `BundledPayload.discover` in AKP03MenuBarCore finds them
# at Contents/Resources and the menu reports "Launch (bundled)".
#
# Layout it produces:
#   AKP03.app/Contents/MacOS/AKP03                the menu-bar app
#   AKP03.app/Contents/Resources/bin/*             HID stream + key injection helpers
#   AKP03.app/Contents/Resources/python/           relocatable Python (Pillow, tomli-w)
#   AKP03.app/Contents/Resources/app/akp03/        the daemon, editor and icon packs
#
# Usage: Scripts/build-app.sh [--no-dmg]
set -euo pipefail

MENUBAR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$MENUBAR_DIR/.." && pwd)"
DIST="$REPO_ROOT/dist"
APP="$DIST/AKP03.app"
VERSION="$(/usr/bin/python3 -c "import re,pathlib;print(re.search(r'^version = \"([^\"]+)\"', pathlib.Path('$REPO_ROOT/pyproject.toml').read_text(), re.M).group(1))")"
BUNDLE_ID="com.oscarserves.akp03.deck"
RUNTIME_CACHE="${AKP03_RUNTIME_CACHE:-$REPO_ROOT/.cache/python-runtime}"
MAKE_DMG=1
[ "${1:-}" = "--no-dmg" ] && MAKE_DMG=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "Building the Swift side (release)"
swift build --package-path "$MENUBAR_DIR" -c release

step "Locating a relocatable Python runtime"
if [ ! -x "$RUNTIME_CACHE/python/bin/python3" ]; then
  echo "    no runtime in $RUNTIME_CACHE - fetching python-build-standalone"
  mkdir -p "$RUNTIME_CACHE"
  URL="$(curl -sL "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" \
    | /usr/bin/python3 -c "
import json,sys
release=json.load(sys.stdin)
for asset in release.get('assets',[]):
    name=asset['name']
    if name.startswith('cpython-3.14') and name.endswith('aarch64-apple-darwin-install_only.tar.gz'):
        print(asset['browser_download_url']); break
")"
  [ -n "$URL" ] || { echo "FAILED: no aarch64 macOS 3.14 runtime found"; exit 1; }
  curl -sL -o "$RUNTIME_CACHE/runtime.tar.gz" "$URL"
  tar xzf "$RUNTIME_CACHE/runtime.tar.gz" -C "$RUNTIME_CACHE"
  [ -x "$RUNTIME_CACHE/python/bin/python3" ] || { echo "FAILED: runtime did not unpack as expected"; exit 1; }
fi
echo "    runtime: $("$RUNTIME_CACHE/python/bin/python3" --version 2>&1)"

step "Assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/bin" "$APP/Contents/Resources/app"
cp "$MENUBAR_DIR/.build/release/AKP03MenuBar" "$APP/Contents/MacOS/AKP03"
# Named AKP03 inside the bundle, not AKP03MenuBar.  The Accessibility pane lists an
# app by its app name and the Input Monitoring pane lists it by its program name, so
# a program called AKP03MenuBar inside an app called AKP03 appeared twice under two
# names and a first run had to be told which was which.  One name, both panes.
# The SwiftPM product keeps its own name; only the file in the bundle is renamed.
for helper in AKP03HIDStream AKP03KeyInject AKP03HIDReadBridge AKP03ControlService; do
  cp "$MENUBAR_DIR/.build/release/$helper" "$APP/Contents/Resources/bin/$helper"
done

step "Copying the Python runtime into the bundle"
cp -R "$RUNTIME_CACHE/python" "$APP/Contents/Resources/python"
# Trim what this app cannot need. The daemon serves the *web* editor, and the
# action/menu model no longer lives in the tkinter module, so Tcl/Tk, the test
# suite and the IDEs come out. The smoke test below proves the payload still
# imports without them.
RUNTIME_LIB="$APP/Contents/Resources/python/lib"
PY_LIB="$(find "$RUNTIME_LIB" -maxdepth 1 -type d -name 'python3.*' | head -1)"
for unwanted in tkinter idlelib turtledemo test ensurepip lib2to3; do
  rm -rf "$PY_LIB/$unwanted" "$RUNTIME_LIB/$unwanted"
done
rm -rf "$PY_LIB/__pycache__" "$PY_LIB/site-packages/__pycache__" "$APP/Contents/Resources/python/share"

step "Installing the two runtime dependencies"
# PYTHONNOUSERSITE, and --ignore-installed, because pip otherwise looks in the
# developer's own account: with Pillow already installed at ~/.local, the install
# reports "requirement already satisfied", puts nothing in the app, and the dmg's
# daemon dies with "No module named 'PIL'" on every Mac but the one that built it.
# The app must carry its own copy of both.
PYTHONNOUSERSITE=1 "$APP/Contents/Resources/python/bin/python3" -m pip install \
  --no-warn-script-location --no-cache-dir --disable-pip-version-check --quiet \
  --ignore-installed Pillow tomli-w

step "Copying the akp03 package, editor assets and icon packs"
/usr/bin/rsync -a --exclude '__pycache__' --exclude '*.pyc' \
  "$REPO_ROOT/akp03" "$APP/Contents/Resources/app/"
find "$APP/Contents/Resources/app" -name '.DS_Store' -delete
/usr/bin/python3 - "$APP" "$VERSION" "$BUNDLE_ID" <<'PLIST'
import plistlib, sys, pathlib
app, version, bundle_id = sys.argv[1], sys.argv[2], sys.argv[3]
info = {
    "CFBundleName": "AKP03",
    "CFBundleDisplayName": "AKP03 Deck",
    "CFBundleIdentifier": bundle_id,
    "CFBundleExecutable": "AKP03",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": version,
    "CFBundleVersion": "1",
    "CFBundleInfoDictionaryVersion": "6.0",
    "LSMinimumSystemVersion": "14.0",
    # A menu-bar app: no Dock icon and no window of its own.
    "LSUIElement": True,
    "NSHighResolutionCapable": True,
    "NSHumanReadableCopyright": "AJAZZ AKP03 control deck",
}
pathlib.Path(app, "Contents", "Info.plist").write_bytes(plistlib.dumps(info))
print("    wrote Info.plist")
PLIST

step "Smoke test: does the bundled payload actually run?"
# Deliberately before signing, and with bytecode writing off: a single .pyc
# created inside the bundle after signing breaks its seal, which is exactly how
# the first build shipped a dmg whose app failed `codesign --verify`.
# PYTHONNOUSERSITE again, so this answers the only question that matters before
# a dmg is handed to someone: does the *app* carry what it needs, or is it
# quietly borrowing it from the machine that built it?
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH="$APP/Contents/Resources/app" \
  "$APP/Contents/Resources/python/bin/python3" - <<'PY'
import pathlib, sys

import akp03.actions_library, akp03.daemon, akp03.iconpacks, akp03.webui
import PIL, tomli_w

print("    imports ok -", len(akp03.actions_library.CATALOGUE), "library actions,",
      len(akp03.iconpacks.load_packs()), "icon packs,", len(akp03.webui.LCD_CONTROLS), "LCD keys")

prefix = pathlib.Path(sys.prefix).resolve()
outside = [m.__name__ for m in (PIL, tomli_w)
           if prefix not in pathlib.Path(m.__file__).resolve().parents]
if outside:
    sys.exit(f"    FAILED: {', '.join(outside)} came from outside the app "
             f"({sys.prefix}), so this dmg would not start on another Mac")
print("    dependencies carried by the app:", prefix.name, "->", 
      pathlib.Path(PIL.__file__).resolve().parent.parent.relative_to(prefix))
PY

step "Signing (ad-hoc, leaf first)"
# Inner binaries are signed before the bundle that contains them. Ad-hoc is
# enough for a locally installed app; macOS attributes Input Monitoring and
# Accessibility to the bundle, which is why the app must be the one that runs.
sign() { /usr/bin/codesign --force --sign - --timestamp=none "$1" >/dev/null 2>&1; }
sign "$APP/Contents/Resources/python/bin/python3"
for lib in "$APP/Contents/Resources/python/lib/"*.dylib; do [ -e "$lib" ] && sign "$lib"; done
for helper in "$APP/Contents/Resources/bin/"*; do sign "$helper"; done
sign "$APP/Contents/MacOS/AKP03"
sign "$APP"
# --strict, because a bundle that verifies loosely can still fail on a machine
# that has no developer certificate to fall back on.
"/usr/bin/codesign" --verify --strict --verbose=2 "$APP" 2>&1 | sed 's/^/    /'

step "Wrapping the .dmg"
if [ "$MAKE_DMG" = "1" ]; then
  STAGE="$DIST/dmg-stage"
  rm -rf "$STAGE"; mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"
  cat > "$STAGE/READ ME FIRST.txt" <<'README'
AKP03 Deck - install

1. Drag AKP03 onto Applications.
2. Open it from Applications (menu-bar icon: no Dock icon, no window).
   If macOS refuses the first launch, right-click > Open once.
3. Grant the two permissions it asks for, both to "AKP03":
   - Input Monitoring  -> so the deck's buttons can be read
   - Accessibility     -> so it can type the shortcuts you bind
   Then quit and reopen the app.
   If a permission is missing and AKP03 is not listed in that pane, click + in the
   pane and add AKP03 from Applications. macOS gives an app no way to add its own
   Input Monitoring row back once it has been removed; the menu's "Clear and
   re-ask for permissions…" restores Accessibility that way, but not this one.
4. Open "Configure AKP03" from the menu-bar icon to reach the editor.

Notes
- Your configuration is ~/.config/akp03/config.toml; the log is
  ~/Library/Logs/AKP03/daemon.log (both shown in the menu's Diagnostics).
- If the deck shows up and then keeps dropping off the bus, try a USB 2.0 hub
  between the Mac and the deck: the M2 mini needs one, the M4 is untested.
- macOS-owned shortcuts (Mission Control, screenshots, Spotlight, the app
  switcher) cannot be triggered by any deck: the system handles them below the
  application layer. Application shortcuts (Cmd-N, Cmd-S, Cmd-W, ...) do work.
README
  hdiutil create -volname "AKP03 $VERSION" -srcfolder "$STAGE" -ov -format UDZO \
    "$DIST/AKP03-$VERSION.dmg" >/dev/null
  echo "    $DIST/AKP03-$VERSION.dmg"
fi

step "Done"
du -sh "$APP" | sed 's/^/    app: /'
ls -1 "$DIST"/*.dmg 2>/dev/null | sed 's/^/    dmg: /' || true
