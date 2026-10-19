#!/usr/bin/env bash
# Deliver editor, asset or daemon changes to the installed app without relinking anything.
#
# Why this exists: macOS binds the Accessibility and Input Monitoring grants to the exact
# binaries.  AKP03MenuBar and the helpers in Contents/Resources/bin are replaced by every
# rebuild, so a rebuild costs Mark a trip through System Settings for a change that never
# touched them.  The web editor, its assets and the daemon payload live in
# Contents/Resources/app - copying those over the installed app and restarting it delivers the
# change with the same binaries and therefore the same grants.
#
# Usage: Scripts/install-live.sh [--app /Applications/AKP03.app] [--dry-run]
#
# Refuses to run when the Swift sources are newer than the installed binaries, because that
# change really does need Scripts/build-app.sh.
set -euo pipefail

MENUBAR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$MENUBAR_DIR/.." && pwd)"

APP="/Applications/AKP03.app"
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

PAYLOAD="$APP/Contents/Resources/app"
BINARY="$APP/Contents/MacOS/AKP03"

step() { printf '\n==> %s\n' "$*"; }

[ -d "$APP" ] || { echo "$APP is not installed; run Scripts/build-app.sh first" >&2; exit 1; }

# The one thing this script cannot deliver.  A newer Swift source means different binaries are
# wanted, and only the full build makes them - quietly copying assets would leave the app
# running old code that looks up to date.
newer_swift="$(find "$MENUBAR_DIR/Sources" -type f -name '*.swift' -newer "$BINARY" -print -quit 2>/dev/null || true)"
if [ -n "$newer_swift" ]; then
  echo "Swift sources are newer than the installed binaries, e.g." >&2
  echo "  ${newer_swift#"$REPO_ROOT"/}" >&2
  echo "That is a real code change: run Scripts/build-app.sh (it costs a permission re-tick)." >&2
  exit 3
fi

step "Copying the akp03 package over the installed payload"
if [ "$DRY" -eq 1 ]; then
  /usr/bin/rsync -an --itemize-changes --exclude '__pycache__' --exclude '*.pyc' \
    "$REPO_ROOT/akp03" "$PAYLOAD/"
else
  /usr/bin/rsync -a --itemize-changes --exclude '__pycache__' --exclude '*.pyc' \
    "$REPO_ROOT/akp03" "$PAYLOAD/"
fi
find "$PAYLOAD" -name '.DS_Store' -delete

if [ "$DRY" -eq 1 ]; then
  step "Dry run: not restarting the app"
  exit 0
fi

# One copy only.  Two daemons fight over 127.0.0.1:17865 and the loser serves half an editor,
# which is what "the whole app is messed up" looks like.
step "Restarting the app"
for pid in $(ps -eo pid,args | awk '/AKP03|akp03\.daemon/ && !/awk/ {print $1}'); do
  kill "$pid" 2>/dev/null || true
done
for _ in $(seq 1 20); do
  [ -z "$(lsof -nP -iTCP:17865 -sTCP:LISTEN 2>/dev/null)" ] && break
  sleep 0.5
done
if [ -n "$(lsof -nP -iTCP:17865 -sTCP:LISTEN 2>/dev/null)" ]; then
  echo "127.0.0.1:17865 is still held after the restart:" >&2
  lsof -nP -iTCP:17865 -sTCP:LISTEN >&2
  exit 1
fi
open -a "$APP"

step "Checking what came back"
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:17865/ || true)"
  [ "$code" = "200" ] && break
  sleep 1
done
echo "  editor: HTTP $code"
curl -s --max-time 8 http://127.0.0.1:17865/api/state | /usr/bin/python3 -c '
import json, sys
d = json.load(sys.stdin)
inj = d.get("injection") or {}
print("  send keys:", inj.get("permitted"), ("" if inj.get("permitted") else "- " + str(inj.get("reason"))))
print("  deck:", d.get("paint_error") or "painting")
print("  source:", d.get("source"))
' || echo "  (could not read /api/state yet)"
echo
echo "The binaries were not touched, so any grant already given still stands."
echo "Hard-reload the editor page (Cmd-Shift-R) to pick up new assets."
