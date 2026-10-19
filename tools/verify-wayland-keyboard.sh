#!/bin/sh
# Deliberate physical end-to-end probe.  It never reads or alters the deployed
# config/service: this process uses a private temporary config and F20 only.
set -eu

if [ "$#" -ne 1 ]; then
  printf '%s\n' "usage: $0 /dev/hidrawN" >&2
  exit 64
fi
for command in akp03d wev wtype; do
  command -v "$command" >/dev/null || { printf '%s\n' "missing required command: $command" >&2; exit 69; }
done
[ "${XDG_SESSION_TYPE:-}" = wayland ] || { printf '%s\n' "requires XDG_SESSION_TYPE=wayland" >&2; exit 69; }
[ -n "${WAYLAND_DISPLAY:-}" ] || { printf '%s\n' "requires WAYLAND_DISPLAY" >&2; exit 69; }

probe_dir="$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/akp03-wayland-probe.XXXXXX")"
config="$probe_dir/config.toml"
socket="$probe_dir/akp03.sock"
cleanup() {
  [ -n "${daemon_pid:-}" ] && kill "$daemon_pid" 2>/dev/null || true
  [ -n "${wev_pid:-}" ] && kill "$wev_pid" 2>/dev/null || true
  rm -rf "$probe_dir"
}
trap cleanup EXIT HUP INT TERM
umask 077
cat >"$config" <<EOF
[daemon]
device = "$1"

[bindings.key0]
action = "keys"
keys = ["f20"]
EOF
chmod 600 "$config"

# wev creates a focused Wayland detector window.  F20 is inert in Hyprland by
# default, so the only expected observable is its key event in this window.
wev >"$probe_dir/wev.log" 2>&1 &
wev_pid=$!
akp03d --config "$config" --socket "$socket" >"$probe_dir/daemon.log" 2>&1 &
daemon_pid=$!
printf '%s\n' "Focus the wev window, then press physical AKP03 LCD key 1 once."
printf '%s\n' "Success: wev reports key F20 and daemon.log has no 'keyboard tap failed'."
printf '%s\n' "Logs remain available until you press Enter: $probe_dir"
read _
if grep -q 'sym: F20' "$probe_dir/wev.log" && ! grep -q 'keyboard tap failed\|keyboard injection unavailable' "$probe_dir/daemon.log"; then
  printf '%s\n' "PASS: physical deck event reached the active Wayland detector as F20."
else
  printf '%s\n' "FAIL: no clean F20 detector event; inspect $probe_dir/wev.log and $probe_dir/daemon.log" >&2
  exit 1
fi
