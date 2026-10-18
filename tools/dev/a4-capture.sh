#!/usr/bin/env bash
# Step A4 hardware check: capture N fingerprint images from the 06cb:0078 with the
# libfprint fork's synaptics0078 driver, from the fork's build dir (no install, no root).
#
#   tools/dev/a4-capture.sh                  3 captures, data from local/synaptics0078
#   tools/dev/a4-capture.sh --count N        N captures instead (1..50)
#   tools/dev/a4-capture.sh --data-dir DIR   data from DIR instead
#   tools/dev/a4-capture.sh --dry-run        preflight only; print env and command
#   tools/dev/a4-capture.sh --diag           finger-off diagnosis: 6 cycles (unless --count), every
#                                            image (captures and finger-off probes) dumped with stats
#                                            to local/diag/<timestamp>/, then a per-cycle probe table
#   --window-ms N / --backoff-ms N           debug only: finger-off probe window (50..10000) and the
#                                            pause between probes (0..60000), default 500 / 250
#
# You touch the sensor when asked, keep still, and lift when told. Each capture is
# only reported after the finger is off the sensor. Images go to
# local/captures/a4-<timestamp>-NN.pgm (mode 0600, directory 0700, gitignored):
# they are biometric data, never copy them into the repository.
#
# Env: LIBFPRINT_FORK (default ~/Devel/Linux/Synaptics/libfprint), A4_TIMEOUT_S
# (per capture attempt, default 60; 180 with --diag). The terminal shows the prompts, the
# results and a summary; everything, including the driver's debug lines, is logged to
# local/logs/a4-capture-<timestamp>.log (mode 0600: it holds the unit's serial).
# This script never calls sudo.
set -euo pipefail
umask 077

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
FORK=${LIBFPRINT_FORK:-$HOME/Devel/Linux/Synaptics/libfprint}
BUILD=$FORK/build-gi
LIBDIR=$BUILD/libfprint
PY=/usr/bin/python3
SCRIPT=$ROOT/tools/dev/a4_capture.py
HELPER="proto/.venv/bin/python tools/synaptics0078_data.py"
DRIVER=synaptics0078
VID=06cb
PID=0078
# The driver's log lines worth showing in the summary (GLib domain libfprint-synaptics0078).
# The texts come from the driver; align these patterns with it when its messages change.
# The fp_info texts (fp_info is g_debug, so they show as DEBUG) plus every WARNING/CRITICAL of the
# domain (GLib writes those as "-WARNING **:"). Not the serial: it stays in the log.
DRIVER_LINES='libfprint-synaptics0078-(DEBUG|INFO|MESSAGE|WARNING)( \*\*)?:( [0-9:.]+:)? (ROM version|Sensor type|Data directory|Capture module|Activation|Finger off after|Capture failed)|libfprint-synaptics0078-(WARNING|CRITICAL)( \*\*)?:'
# GLib's debug stream (G_MESSAGES_DEBUG): "(process:PID): DOMAIN-DEBUG: hh:mm:ss.mmm: text". It goes
# to the log only; the terminal keeps the script's own lines and any WARNING/CRITICAL.
GLIB_DEBUG_LINE='^\([^()]*:[0-9]+\): [^ ]+-(DEBUG|INFO|MESSAGE)( \*\*)?: '
# The script's own result lines (per image, devices/open/close, verdict).
SCRIPT_LINES='^(devices|driver|device id|name|scan type|open|close|is open|capturing|retry|image [0-9]+/[0-9]+|capture [0-9]+/[0-9]+|PASS|FAIL|  found):'

DATA_DIR=$ROOT/local/synaptics0078
LOG_DIR=$ROOT/local/logs
CAPTURE_DIR=$ROOT/local/captures
DIAG_ROOT=$ROOT/local/diag
COUNT=3
count_given=0
dry_run=0
diag=0
window_ms=""
backoff_ms=""
data_dir_given=0

usage() { sed -n '2,24s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; }
fail() {
	printf 'preflight FAILED: %s\n' "$1" >&2
	[ $# -lt 2 ] || printf '  hint: %s\n' "$2" >&2
	exit 2
}
ok() { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }

while [ $# -gt 0 ]; do
	case $1 in
	--dry-run) dry_run=1 ;;
	--diag) diag=1 ;;
	--count)
		[ $# -ge 2 ] || fail "--count needs a number"
		COUNT=$2
		count_given=1
		shift
		;;
	--count=*)
		COUNT=${1#--count=}
		count_given=1
		;;
	--window-ms)
		[ $# -ge 2 ] || fail "--window-ms needs a number"
		window_ms=$2
		shift
		;;
	--window-ms=*) window_ms=${1#--window-ms=} ;;
	--backoff-ms)
		[ $# -ge 2 ] || fail "--backoff-ms needs a number"
		backoff_ms=$2
		shift
		;;
	--backoff-ms=*) backoff_ms=${1#--backoff-ms=} ;;
	--data-dir)
		[ $# -ge 2 ] || fail "--data-dir needs a directory"
		[ -n "$2" ] || fail "--data-dir needs a non-empty directory path"
		DATA_DIR=$2
		data_dir_given=1
		shift
		;;
	--data-dir=*)
		DATA_DIR=${1#--data-dir=}
		[ -n "$DATA_DIR" ] || fail "--data-dir needs a non-empty directory path"
		data_dir_given=1
		;;
	-h | --help)
		usage
		exit 0
		;;
	*) fail "unknown argument: $1" "see $0 --help" ;;
	esac
	shift
done
[[ $COUNT =~ ^[0-9]{1,3}$ ]] && [ "$((10#$COUNT))" -ge 1 ] && [ "$((10#$COUNT))" -le 50 ] ||
	fail "--count must be a whole number from 1 to 50, got '$COUNT'"
COUNT=$((10#$COUNT))
[ "$diag" -eq 0 ] || [ "$count_given" -eq 1 ] || COUNT=6
# ms_in_range VALUE MIN MAX: a whole number of milliseconds within [MIN, MAX] (the driver's bounds).
ms_in_range() { [[ $1 =~ ^[0-9]{1,5}$ ]] && [ "$((10#$1))" -ge "$2" ] && [ "$((10#$1))" -le "$3" ]; }
[ -z "$window_ms" ] || ms_in_range "$window_ms" 50 10000 ||
	fail "--window-ms must be a whole number from 50 to 10000, got '$window_ms'"
[ -z "$backoff_ms" ] || ms_in_range "$backoff_ms" 0 60000 ||
	fail "--backoff-ms must be a whole number from 0 to 60000, got '$backoff_ms'"
# Finger-off probing can last tens of seconds when it misbehaves, which is what --diag looks at.
default_timeout=60
[ "$diag" -eq 0 ] || default_timeout=180
TIMEOUT_S=${A4_TIMEOUT_S:-$default_timeout}
[[ $TIMEOUT_S =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v t="$TIMEOUT_S" 'BEGIN { exit !(t > 0 && t <= 3600) }' ||
	fail "A4_TIMEOUT_S='$TIMEOUT_S' is not a number of seconds in (0, 3600]"

echo "A4 capture check: project $ROOT, $COUNT capture(s), ${TIMEOUT_S} s per attempt"
echo "preflight:"

# 1. The fork's build with introspection, and the driver built into it.
build_hint="build the fork: cd $FORK && meson setup build-gi -Ddrivers=all -Dintrospection=true -Dinstalled-tests=false && meson compile -C build-gi (or set LIBFPRINT_FORK)"
[ -d "$LIBDIR" ] || fail "no fork build dir $LIBDIR" "$build_hint"
# -f follows the libfprint-2.so -> .so.2 -> .so.2.0.0 symlinks: a dangling link fails here too.
[ -f "$LIBDIR/libfprint-2.so" ] || fail "libfprint-2.so is missing (or a dangling link) in $LIBDIR" "$build_hint"
[ -f "$LIBDIR/FPrint-2.0.typelib" ] || fail "no FPrint-2.0.typelib in $LIBDIR" "$build_hint"
ok "fork build: $LIBDIR"
lib=$(readlink -f -- "$LIBDIR/libfprint-2.so")
[ -r "$lib" ] || fail "$lib is not readable by $(id -un)"
LC_ALL=C grep -qaF "libfprint-$DRIVER" "$lib" ||
	fail "libfprint-2.so is there but driver $DRIVER is not built into $lib" \
		"rebuild after the driver lands: meson compile -C $BUILD (meson setup --reconfigure if -Ddrivers excludes it)"
ok "driver $DRIVER is in $(basename -- "$lib")"

# 2. Python with PyGObject, loading the fork's typelib (import only, no device access).
[ -x "$PY" ] || fail "$PY not found" "install python"
"$PY" -c 'import gi' 2>/dev/null || fail "$PY cannot import gi" "install PyGObject (Arch: pacman -S python-gobject)"
GI_TYPELIB_PATH=$LIBDIR LD_LIBRARY_PATH=$LIBDIR "$PY" -c \
	'import gi; gi.require_version("FPrint", "2.0"); from gi.repository import FPrint' 2>/dev/null ||
	fail "$PY cannot load FPrint 2.0 from $LIBDIR" "$build_hint"
ok "$PY imports gi and the fork's FPrint 2.0"

# 3. Exactly one 06cb:0078 on the bus (sysfs), its device node readable and writable.
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
[ ${#nodes[@]} -gt 0 ] || fail "no $VID:$PID device on the USB bus" "is the sensor enabled (BIOS) and visible in lsusb?"
[ ${#nodes[@]} -eq 1 ] || fail "found ${#nodes[@]} $VID:$PID devices (${nodes[*]}), expected exactly one"
node=${nodes[0]}
sysdir=${sysdirs[0]}
[ -e "$node" ] || fail "$node does not exist" "re-plug the device or check udev"
[ -r "$node" ] && [ -w "$node" ] ||
	fail "$node is not readable and writable by $(id -un)" \
		"add the uaccess udev rule from proto/README.md, section 'Access without root (udev)', then re-plug or log in again"
ok "$VID:$PID at $node, read+write for $(id -un)"

# 4. fprintd must not hold the sensor.
if { command -v systemctl >/dev/null && systemctl is-active --quiet fprintd; } ||
	{ command -v pgrep >/dev/null && pgrep -x fprintd >/dev/null; }; then
	fail "fprintd is running and may claim the sensor" \
		"run 'sudo systemctl stop fprintd' yourself; it is D-Bus activated, so any client (a lock screen, a settings panel) may start it again during the run: 'sudo systemctl mask --runtime fprintd' keeps it off until reboot, 'sudo systemctl unmask --runtime fprintd' undoes it"
fi
ok "fprintd is not running (D-Bus may still start it; 'sudo systemctl mask --runtime fprintd' prevents that, 'sudo systemctl unmask --runtime fprintd' undoes it)"

# 5. The data directory: module.bin plus the plugged unit's program file are required.
DATA_DIR=$(realpath -m -- "$DATA_DIR")
shopt -s nullglob
programs=("$DATA_DIR"/program-*.bin)
shopt -u nullglob
out_arg=""
[ "$data_dir_given" -eq 0 ] || out_arg=" --out $(printf '%q' "$DATA_DIR")"
if [ -f "$DATA_DIR/module.bin" ] && [ ${#programs[@]} -gt 0 ]; then
	ok "data dir: $DATA_DIR (module.bin, ${#programs[@]} program file(s))"
else
	fail "data dir $DATA_DIR lacks module.bin and/or program-<serial>.bin" \
		"create them (from $ROOT): $HELPER$out_arg"
fi

# 6. The program file is the plugged unit's: the driver picks program-<serial>.bin by
# the sensor serial, which equals the USB serial string (sysfs only, no USB I/O).
usb_serial=""
[ ! -r "$sysdir/serial" ] || usb_serial=$(tr -d '[:space:]' <"$sysdir/serial" | tr '[:upper:]' '[:lower:]')
if ! [[ $usb_serial =~ ^[0-9a-f]{12}$ ]]; then
	warn "cannot check the program name: $sysdir/serial is missing or not 12 hex digits"
elif [ -f "$DATA_DIR/program-$usb_serial.bin" ]; then
	ok "program-$usb_serial.bin matches the plugged unit's USB serial"
else
	fail "no program-$usb_serial.bin in $DATA_DIR: $(basename -a -- "${programs[@]}" | paste -sd' ') does not match the plugged unit" \
		"those were built from another unit's CalibrationData; build this unit's with $HELPER$out_arg"
fi

# 7. Where the images and the log go: real directories of ours, never symlinks.
dirs=("$ROOT/local" "$LOG_DIR" "$CAPTURE_DIR")
[ "$diag" -eq 0 ] || dirs+=("$DIAG_ROOT")
for d in "${dirs[@]}"; do
	if [ -L "$d" ]; then
		fail "$d is a symlink" "remove it or point nothing at it; images and logs are written only to real directories"
	elif [ -e "$d" ] && [ ! -d "$d" ]; then
		fail "$d exists and is not a directory"
	fi
done
ok "images go to $CAPTURE_DIR (0700, files 0600, gitignored)"
[ "$diag" -eq 0 ] || ok "diagnosis dumps go to $DIAG_ROOT/<timestamp>/ (0700, files 0600, gitignored)"

STAMP=$(date +%Y%m%d-%H%M%S)
run_env=(
	"FP_DRIVERS_ALLOWLIST=$DRIVER"
	"FP_SYNAPTICS0078_DATA_DIR=$DATA_DIR"
	"G_MESSAGES_DEBUG=libfprint-$DRIVER"
	"LD_LIBRARY_PATH=$LIBDIR"
	"GI_TYPELIB_PATH=$LIBDIR"
	"PYTHONUNBUFFERED=1"
	"A4_TIMEOUT_S=$TIMEOUT_S"
	"A4_STAMP=$STAMP"
)
DIAG_DIR=$DIAG_ROOT/$STAMP
[ "$diag" -eq 0 ] || run_env+=("FP_SYNAPTICS0078_DUMP_DIR=$DIAG_DIR")
[ -z "$window_ms" ] || run_env+=("FP_SYNAPTICS0078_PROBE_WINDOW_MS=$((10#$window_ms))")
[ -z "$backoff_ms" ] || run_env+=("FP_SYNAPTICS0078_PROBE_BACKOFF_MS=$((10#$backoff_ms))")
cmd=("$PY" "$SCRIPT" --count "$COUNT")

echo "run:"
printf '  %q\n' "${run_env[@]}"
printf -v cmd_line '%q ' "${cmd[@]}"
echo "  ${cmd_line% }"
if [ "$dry_run" -eq 1 ]; then
	echo "dry run: preflight passed; $(basename -- "$SCRIPT") not started"
	exit 0
fi

# The log holds the unit's serial: local/logs 0700, the log 0600, whatever existed before.
mkdir -p -m 700 -- "$LOG_DIR"
read -r log_uid log_mode < <(stat -L -c '%u %a' -- "$LOG_DIR")
if ((8#$log_mode & 8#077)); then
	if [ "$log_uid" -eq "$(id -u)" ]; then
		chmod 700 -- "$LOG_DIR"
		echo "note: $LOG_DIR was mode $log_mode, set to 700"
	else
		warn "$LOG_DIR is mode $log_mode and not owned by $(id -un): others may read the log"
	fi
fi
log=$LOG_DIR/a4-capture-$STAMP.log
[ ! -e "$log" ] && [ ! -L "$log" ] || fail "$log already exists" "wait a second and run again"
: >"$log"
chmod 600 -- "$log"
{
	echo "# a4-capture $(date -Is)"
	echo "# fork $FORK @ $(git --no-optional-locks -C "$FORK" rev-parse --short HEAD 2>/dev/null || echo unknown)"
	echo "# device $node"
	printf '# %s\n' "${run_env[@]}"
} >>"$log"

if [ "$diag" -eq 1 ]; then
	mkdir -p -m 700 -- "$DIAG_ROOT"
	mkdir -m 700 -- "$DIAG_DIR" || fail "cannot create $DIAG_DIR" "wait a second and run again"
fi

echo "--- $(basename -- "$SCRIPT") (log: $log)"
echo "Follow the prompts: touch the sensor when asked, keep still, lift when told."
[ "$diag" -eq 0 ] || echo "Diagnosis: lift at once when told and keep the finger (and the palm) clear of the sensor until the next prompt."
set +e
# tee and grep ignore Ctrl-C (it is the Python script's to handle), so the log keeps the tail.
env "${run_env[@]}" "${cmd[@]}" 2>&1 | tee -i -a -- "$log" | (
	trap '' INT
	exec grep --line-buffered -vE -- "$GLIB_DEBUG_LINE"
)
rc=${PIPESTATUS[0]}
set -e

echo "--- summary"
grep -E "$DRIVER_LINES" -- "$log" | sed 's/^/  /' || echo "  (no driver lines: ROM version / Sensor type / Data directory / Capture module / Activation / Finger off after / Capture failed)"
grep -E "$SCRIPT_LINES" -- "$log" | sed 's/^/  /' || true
# The script prints its own PASS/FAIL verdict (shown above); the wrapper adds only where things are,
# and a FAIL of its own when the script died without one (a crash, a signal).
echo "images: $CAPTURE_DIR/a4-$STAMP-NN.pgm; log: $log"
if [ "$diag" -eq 1 ]; then
	echo "--- finger-off probes per cycle (start/finger/ready in ms; finger '-' = finger off)"
	"$PY" "$ROOT/tools/dev/a4_diag.py" "$log" | tee -- "$DIAG_DIR/summary.txt" || true
	echo "diagnosis: every image as <seq>-<monotonic ms>-<capture|probeNN>[-err].pgm in $DIAG_DIR (summary.txt there)"
fi
if [ "$rc" -ne 0 ] && ! grep -qE '^FAIL:' -- "$log"; then
	echo "FAIL: $(basename -- "$SCRIPT") exited $rc without a verdict"
fi
exit "$rc"
