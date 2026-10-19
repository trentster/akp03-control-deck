#!/bin/sh
# Local, non-installing verification for the macOS menu-bar companion.
set -eu

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$package_dir"

swift build
swift test
swift run AKP03MenuBarChecks

help_output=$(./.build/debug/AKP03ControlService --help)
check_output=$(./.build/debug/AKP03ControlService --check)
case "$help_output" in
  *"Usage: AKP03ControlService [--check]"*) ;;
  *) echo "AKP03ControlService --help did not print usage" >&2; exit 1 ;;
esac
case "$check_output" in
  *"status=interfaceReady"*) ;;
  *) echo "AKP03ControlService --check did not report interface readiness" >&2; exit 1 ;;
esac

# The key-injection helper is exercised only in --dry-run, which parses and
# validates commands and prints what it *would* post while posting nothing: no
# CGEvent is created and no key is injected. It must show a valid chord, reject
# an invalid key, and refuse a duplicate hold and an unmatched release.
inject_dry=$(
  printf '%s\n' \
    "ping" \
    "tap cmd+z" \
    "hold ctrl+shift+space" \
    "hold ctrl+shift+space" \
    "release ctrl+shift+space" \
    "release ctrl+shift+space" \
    "tap semicolon" \
    "quit" \
  | ./.build/debug/AKP03KeyInject --dry-run
)
for expectation in \
  '"result":"dry-run","command":"tap","chord":"cmd+z","would_post":["keyDown cmd","keyDown z","keyUp z","keyUp cmd"]' \
  '"result":"dry-run","command":"hold","chord":"ctrl+shift+space","would_post":["keyDown ctrl","keyDown shift","keyDown space"]' \
  '"result":"dry-run","command":"release","chord":"ctrl+shift+space","would_post":["keyUp space","keyUp shift","keyUp ctrl"]' \
  '"result":"error","command":"hold","reason":"already holding ctrl+shift+space"' \
  '"result":"error","command":"release","reason":"not holding ctrl+shift+space"' \
  '"result":"error","command":"tap","reason":"unsupported key: '\''semicolon'\''"' \
  '"result":"ok","command":"quit"'
do
  case "$inject_dry" in
    *"$expectation"*) ;;
    *) echo "AKP03KeyInject --dry-run missing expected line: $expectation" >&2; exit 1 ;;
  esac
done
