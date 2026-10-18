#!/usr/bin/env bash
# Step A3 hardware check: open and close the 06cb:0078 with the libfprint fork's
# synaptics0078 driver, from the fork's build dir (no install, no root).
#
#   tools/dev/a3-open.sh                  the check, data from local/synaptics0078
#   tools/dev/a3-open.sh --data-dir DIR   data from DIR instead
#   tools/dev/a3-open.sh --missing-files  against a fresh empty dir: shows the
#                                         driver's missing-file error on purpose
#   tools/dev/a3-open.sh --dry-run        preflight only; print env and command
#
# Env: LIBFPRINT_FORK (default ~/Devel/Linux/Synaptics/libfprint), A3_TIMEOUT_S
# (per open/close step, default 30). The output is also logged to
# local/logs/a3-open-<timestamp>.log (mode 0600: it holds the unit's serial).
# This script never calls sudo.
set -euo pipefail
umask 077

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
FORK=${LIBFPRINT_FORK:-$HOME/Devel/Linux/Synaptics/libfprint}
BUILD=$FORK/build-gi
LIBDIR=$BUILD/libfprint
PY=/usr/bin/python3
SCRIPT=$ROOT/tools/dev/a3_open.py
HELPER="proto/.venv/bin/python tools/synaptics0078_data.py"
DRIVER=synaptics0078
VID=06cb
PID=0078
# The driver's log lines worth showing in the summary (GLib domain libfprint-synaptics0078).
DRIVER_LINES='libfprint-synaptics0078-[A-Z]+:( [0-9:.]+:)? (ROM version|Sensor type|Serial|Data directory|Capture module:)'

DATA_DIR=$ROOT/local/synaptics0078
LOG_DIR=$ROOT/local/logs
dry_run=0
missing=0
data_dir_given=0

usage() { sed -n '2,14s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; }
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
	--missing-files) missing=1 ;;
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
[ "$missing" -eq 0 ] || [ "$data_dir_given" -eq 0 ] || fail "--missing-files and --data-dir exclude each other"

echo "A3 open check: project $ROOT"
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

# 5. The data directory.
if [ "$missing" -eq 1 ]; then
	DATA_DIR=$(mktemp -d "${TMPDIR:-/tmp}/a3-missing-files.XXXXXX")
	trap 'rmdir -- "$DATA_DIR" 2>/dev/null || true' EXIT
	ok "data dir: $DATA_DIR (fresh and empty, --missing-files)"
else
	DATA_DIR=$(realpath -m -- "$DATA_DIR")
	shopt -s nullglob
	programs=("$DATA_DIR"/program-*.bin)
	shopt -u nullglob
	out_arg=""
	[ "$data_dir_given" -eq 0 ] || out_arg=" --out $(printf '%q' "$DATA_DIR")"
	if [ -f "$DATA_DIR/module.bin" ] && [ ${#programs[@]} -gt 0 ]; then
		ok "data dir: $DATA_DIR (module.bin, ${#programs[@]} program file(s))"
	else
		warn "data dir $DATA_DIR lacks module.bin and/or program-<serial>.bin"
		warn "the driver should fail with FP_DEVICE_ERROR_GENERAL naming the missing file;"
		warn "to create them (from $ROOT): $HELPER$out_arg"
	fi

	# 6. The program file is the plugged unit's: the driver picks program-<serial>.bin by
	# the sensor serial, which equals the USB serial string (sysfs only, no USB I/O).
	usb_serial=""
	[ ! -r "$sysdir/serial" ] || usb_serial=$(tr -d '[:space:]' <"$sysdir/serial" | tr '[:upper:]' '[:lower:]')
	if ! [[ $usb_serial =~ ^[0-9a-f]{12}$ ]]; then
		warn "cannot check the program name: $sysdir/serial is missing or not 12 hex digits"
	elif [ -f "$DATA_DIR/program-$usb_serial.bin" ]; then
		ok "program-$usb_serial.bin matches the plugged unit's USB serial"
	elif [ ${#programs[@]} -gt 0 ]; then
		fail "no program-$usb_serial.bin in $DATA_DIR: $(basename -a -- "${programs[@]}" | paste -sd' ') does not match the plugged unit" \
			"those were built from another unit's CalibrationData; build this unit's with $HELPER$out_arg"
	fi
fi

run_env=(
	"FP_DRIVERS_ALLOWLIST=$DRIVER"
	"FP_SYNAPTICS0078_DATA_DIR=$DATA_DIR"
	"G_MESSAGES_DEBUG=libfprint-$DRIVER"
	"LD_LIBRARY_PATH=$LIBDIR"
	"GI_TYPELIB_PATH=$LIBDIR"
	"PYTHONUNBUFFERED=1"
)
cmd=("$PY" "$SCRIPT")

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
log=$LOG_DIR/a3-open-$(date +%Y%m%d-%H%M%S).log
: >"$log"
chmod 600 -- "$log"
{
	echo "# a3-open $(date -Is)"
	echo "# fork $FORK @ $(git --no-optional-locks -C "$FORK" rev-parse --short HEAD 2>/dev/null || echo unknown)"
	echo "# device $node"
	printf '# %s\n' "${run_env[@]}"
} >>"$log"

echo "--- $(basename -- "$SCRIPT") (log: $log)"
set +e
env "${run_env[@]}" "${cmd[@]}" 2>&1 | tee -a -- "$log"
rc=${PIPESTATUS[0]}
set -e

echo "--- summary"
grep -E "$DRIVER_LINES" -- "$log" | sed 's/^/  /' || echo "  (no driver lines: ROM version / Sensor type / Serial / Data directory / Capture module:)"
grep -E '^(devices|driver|device id|name|scan type|open|close|is open|  found):' -- "$log" | sed 's/^/  /' || true
if [ "$missing" -eq 1 ]; then
	expected="open FAILED with FPrint.DeviceError.GENERAL naming $DATA_DIR/module.bin"
	# no grep -q: under pipefail an early exit could SIGPIPE the greps before it
	if [ "$rc" -eq 1 ] && grep -E '^open: FAILED ' -- "$log" | grep -F 'FPrint.DeviceError.GENERAL' |
		grep -F -- "$DATA_DIR/module.bin" >/dev/null; then
		echo "PASS: --missing-files: $expected (exit 1 as expected; log $log)"
		exit 0
	fi
	echo "FAIL: --missing-files expected exit 1 and $expected; $(basename -- "$SCRIPT") exited $rc (log $log)"
	[ "$rc" -ne 0 ] || exit 1
	exit "$rc"
fi
if [ "$rc" -eq 0 ]; then
	echo "PASS: open and close succeeded (exit 0; log $log)"
else
	echo "FAIL: $(basename -- "$SCRIPT") exited $rc (log $log)"
fi
exit "$rc"
