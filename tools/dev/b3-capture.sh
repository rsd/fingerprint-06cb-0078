#!/usr/bin/env bash
# Step B3: guided capture of raw fingerprint images for the offline false-accept benchmark.
#
#   tools/dev/b3-capture.sh                                   all 10 fingers, 20 touches each, person "me"
#   tools/dev/b3-capture.sh --fingers right-index,left-index  only these fingers (names below)
#   tools/dev/b3-capture.sh --touches N                       N touches per finger (1..100)
#   tools/dev/b3-capture.sh --person NAME                     whose fingers: a consenting guest gets their
#                                                             own NAME (a-z, 0-9, '-', '_', up to 32)
#   tools/dev/b3-capture.sh --dry-run                         the checks only; the reader is not opened
#
# Fingers: left-thumb left-index left-middle left-ring left-little
#          right-thumb right-index right-middle right-ring right-little
#
# Run it as yourself, never with sudo. It uses the INSTALLED libfprint (package libfprint-sigfm-0078) through
# PyGObject and the system typelib, and reads the reader's data files from local/synaptics0078. The terminal
# shows only what to do (TOUCH, LIFT, touch again, the next finger) and the result; the driver's debug lines
# and every detail go to local/logs/b3-capture-<timestamp>.log (0600: it holds the unit's serial).
#
# Images: local/b3/<person>/<finger>/NNN.pgm plus NNN.json (0600, directories 0700, gitignored). They are
# biometric data: never copy them into the repository. Ctrl-C stops safely; running the same command again
# on the same day continues where it stopped. On another day it captures a new session.
#
# Exit codes: 0 all requested captures done; 1 a device, driver or setup problem, an interrupt or too many
# failed touches; 2 misuse.
set -euo pipefail
umask 077

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
PY=/usr/bin/python3
SCRIPT=$ROOT/tools/dev/b3_capture.py
DRIVER=synaptics0078
VID=06cb
PID=0078
PKG=libfprint-sigfm-0078
PKG_LIB=/usr/lib/libfprint-2.so.2
TYPELIB=/usr/lib/girepository-1.0/FPrint-2.0.typelib
DATA_DIR=$ROOT/local/synaptics0078
LOG_DIR=$ROOT/local/logs
UDEV_RULE=/etc/udev/rules.d/70-fingerprint-06cb-0078.rules
HELPER="proto/.venv/bin/python tools/synaptics0078_data.py"
PKG_HINT="build and install the package (README, A6): cd packaging/arch/$PKG && makepkg -f && sudo pacman -U $PKG-[0-9]*-x86_64.pkg.tar.zst"
# Without these the system libfprint and typelib are the ones that load.
CLEAN_ENV=(env -u LD_LIBRARY_PATH -u GI_TYPELIB_PATH)

usage() { sed -n '2,24s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; }
fail() {
	printf 'FAILED: %s\n' "$1" >&2
	[ $# -lt 2 ] || printf '  hint: %s\n' "$2" >&2
	exit 1
}
misuse() {
	printf 'FAILED: %s\n' "$1" >&2
	[ $# -lt 2 ] || printf '  hint: %s\n' "$2" >&2
	exit 2
}
# Preflight details for the log (it is opened after the preflight).
NOTES=()
note() { NOTES+=("$*"); }

dry_run=0
args=()
for arg in "$@"; do
	case $arg in
	-h | --help)
		usage
		exit 0
		;;
	--dry-run) dry_run=1 ;;
	--log | --log=* | --package | --package=* | --validate-only) misuse "unknown argument: $arg" "see $0 --help" ;;
	*) args+=("$arg") ;;
	esac
done
set -- "${args[@]}"
[ "$(id -u)" -ne 0 ] || misuse "do not run this as root or with sudo" "run it as yourself: tools/dev/b3-capture.sh"

# 1. Python, and the arguments (checked by the script itself, without PyGObject or the device).
[ -x "$PY" ] || fail "$PY not found" "install python: sudo pacman -S python"
"$PY" "$SCRIPT" --validate-only "$@" || exit 2

# 2. PyGObject and FPrint 2.0 from the system typelib.
"${CLEAN_ENV[@]}" "$PY" -c 'import gi' 2>/dev/null ||
	fail "$PY cannot import gi" "install PyGObject: sudo pacman -S python-gobject"
"${CLEAN_ENV[@]}" "$PY" -c 'import gi; gi.require_version("FPrint", "2.0"); from gi.repository import FPrint' 2>/dev/null ||
	fail "$PY cannot load FPrint 2.0 from the system typelib ($TYPELIB)" "$PKG_HINT"
note "$PY imports gi and FPrint 2.0 (system typelib $TYPELIB)"

# 3. The package and its driver (read-only queries).
command -v pacman >/dev/null || fail "pacman not found: cannot check that $PKG is installed" "this tool expects Arch's pacman"
pkg_version=$(pacman -Q "$PKG" 2>/dev/null) || fail "the $PKG package is not installed" "$PKG_HINT"
[ -f "$PKG_LIB" ] && LC_ALL=C grep -qaF "libfprint-$DRIVER" -- "$PKG_LIB" ||
	fail "$PKG_LIB has no $DRIVER driver" "reinstall the package: $PKG_HINT"
note "package: $pkg_version; $PKG_LIB -> $(readlink -f -- "$PKG_LIB")"

# 4. Exactly one 06cb:0078 on the bus (sysfs only), its device node readable and writable.
nodes=()
sysdirs=()
for dev in /sys/bus/usb/devices/*; do
	[ -f "$dev/idVendor" ] && [ -f "$dev/idProduct" ] || continue
	[ "$(<"$dev/idVendor")" = "$VID" ] && [ "$(<"$dev/idProduct")" = "$PID" ] || continue
	bus=$(<"$dev/busnum")
	num=$(<"$dev/devnum")
	nodes+=("$(printf '/dev/bus/usb/%03d/%03d' "$((10#$bus))" "$((10#$num))")")
	sysdirs+=("$dev")
done
[ ${#nodes[@]} -gt 0 ] || fail "the fingerprint reader ($VID:$PID) is not on the USB bus" \
	"is it enabled (BIOS) and listed by lsusb?"
[ ${#nodes[@]} -eq 1 ] || fail "found ${#nodes[@]} $VID:$PID devices (${nodes[*]}), expected exactly one"
node=${nodes[0]}
sysdir=${sysdirs[0]}
[ -e "$node" ] || fail "$node does not exist" "re-plug the reader or log in again"
[ -r "$node" ] && [ -w "$node" ] ||
	fail "you ($(id -un)) may not use the reader: $node is not readable and writable" \
		"check it with: getfacl $node (your user should be listed with rw); re-plug the reader or log out and in again; if $UDEV_RULE was just added: sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usb --action=add"
note "reader at $node, read+write for $(id -un)"

# 5. The data files: open reads module.bin and the unit's program file every time, loaded module or not.
shopt -s nullglob
programs=("$DATA_DIR"/program-*.bin)
shopt -u nullglob
if [ -r "$DATA_DIR/module.bin" ] && [ ${#programs[@]} -gt 0 ]; then
	note "data dir: $DATA_DIR (module.bin, ${#programs[@]} program file(s))"
else
	fail "the reader's data files are missing in $DATA_DIR" "create them (from $ROOT): $HELPER"
fi
usb_serial=""
[ ! -r "$sysdir/serial" ] || usb_serial=$(tr -d '[:space:]' <"$sysdir/serial" | tr '[:upper:]' '[:lower:]')
if ! [[ $usb_serial =~ ^[0-9a-f]{12}$ ]]; then
	note "cannot check the program file name: $sysdir/serial is missing or not 12 hex digits"
elif [ -r "$DATA_DIR/program-$usb_serial.bin" ]; then
	note "program-$usb_serial.bin matches the plugged unit"
else
	fail "the program file in $DATA_DIR is not this reader's" "build this unit's (from $ROOT): $HELPER"
fi

# 6. fprintd only holds the reader while a client (fprintd-verify, a lock screen, settings) is active; the
# script says so plainly if opening finds the reader busy.
if { command -v systemctl >/dev/null && systemctl is-active --quiet fprintd 2>/dev/null; } ||
	{ command -v pgrep >/dev/null && pgrep -x fprintd >/dev/null; }; then
	note "WARN fprintd is running; it holds the reader only while a fingerprint prompt is active"
else
	note "fprintd is not running"
fi

# 7. Where the images and the log go: real directories, never symlinks.
for d in "$ROOT/local" "$LOG_DIR" "$ROOT/local/b3"; do
	if [ -L "$d" ]; then
		fail "$d is a symlink" "images and logs are written only to real directories"
	elif [ -e "$d" ] && [ ! -d "$d" ]; then
		fail "$d exists and is not a directory"
	fi
done

if [ "$dry_run" -eq 1 ]; then
	# Not the notes: they name the unit's serial (program-<serial>.bin), which stays out of the terminal.
	echo "dry run: the checks passed ($pkg_version, reader at $node); the reader was not opened"
	exit 0
fi

# The log: local/logs 0700 (mode fixed only when it is ours), the log 0600.
mkdir -p -m 700 -- "$LOG_DIR"
read -r log_uid log_mode < <(stat -L -c '%u %a' -- "$LOG_DIR")
if ((8#$log_mode & 8#077)) && [ "$log_uid" -eq "$(id -u)" ]; then
	chmod 700 -- "$LOG_DIR"
fi
STAMP=$(date +%Y%m%d-%H%M%S)
log=$LOG_DIR/b3-capture-$STAMP.log
[ ! -e "$log" ] && [ ! -L "$log" ] || fail "$log already exists" "wait a second and run again"
: >"$log"
chmod 600 -- "$log"
run_env=(
	"FP_DRIVERS_ALLOWLIST=$DRIVER"
	"FP_SYNAPTICS0078_DATA_DIR=$DATA_DIR"
	"G_MESSAGES_DEBUG=all"
	"PYTHONUNBUFFERED=1"
)
cmd=("$PY" "$SCRIPT" "$@" --log "$log" --package "$pkg_version")
{
	echo "# b3-capture $(date -Is) user $(id -un)"
	printf '# preflight: %s\n' "${NOTES[@]}"
	printf '# env: %s\n' "${run_env[@]}"
	printf -v cmd_line '%q ' "${cmd[@]}"
	echo "# run: ${cmd_line% }"
} >>"$log"

set +e
# The script's own lines go to the terminal (and to the log); its stderr, with every GLib debug line, only
# to the log (GLib prints DEBUG/INFO to stdout by default: the script routes them to stderr first thing). Ctrl-C reaches the script, which closes the reader and keeps what was saved.
"${CLEAN_ENV[@]}" "${run_env[@]}" "${cmd[@]}" 2>>"$log"
rc=$?
set -e
[ "$rc" -eq 0 ] || echo "Details: $log"
exit "$rc"
