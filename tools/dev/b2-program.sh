#!/usr/bin/env bash
# Step B2b: swap the reader's capture program between the Windows-derived one and the one built
# from Linux measurements (proto/calib_program.py RUN_DIR), reversibly, to test it with real fingers.
#
#   sudo tools/dev/b2-program.sh use-linux RUN_DIR   install RUN_DIR/program-linux.bin (backs up the Windows one once)
#   sudo tools/dev/b2-program.sh use-windows         put the backed-up Windows program back, byte for byte
#   tools/dev/b2-program.sh status [RUN_DIR]         which program is installed (sudo when /var/lib/fprint is root-only)
#
# RUN_DIR is a local/calib/<stamp> directory holding program-linux.bin. The driver reads only
# /var/lib/fprint/synaptics0078/program-<serial>.bin (the plugged unit's serial), at every open of the
# reader. use-linux checks both programs first (the size, the first byte 0x02, and every byte outside the
# per-unit regions equal to the pinned template: calibration.program_problem) and refuses, changing
# nothing, when one is not usable. The first use-linux copies the installed (Windows) program to
# program-<serial>.bin.b2-windows-backup next to it (the driver ignores that name; it is never
# overwritten), then replaces program-<serial>.bin atomically, keeping its owner and mode. use-linux
# refuses while a backup exists and the installed program is neither the backup nor RUN_DIR's nor any
# local/calib/*/program-linux.bin (it would be lost). use-windows restores the backup the same way and
# keeps it. When fprintd is running (or starting, reloading, stopping), both stop it (systemctl stop
# fprintd): a client holding the reader (a lock screen, a verify) would keep the old program until it
# lets go; D-Bus starts fprintd again on demand, and it reads the new file.
# status compares sha256s: the backup -> Windows; no backup yet -> Windows (original, never swapped);
# RUN_DIR's program or any local/calib/*/program-linux.bin -> Linux (from that run); else unknown.
#
# The terminal shows a few lines and the next step of B2b; the details (real paths, sha256s, the
# systemctl output) go to local/logs/b2-program-<timestamp>.log (0600, it names the unit serial; given
# to the sudo user). The serial itself is never printed: the file shows as program-<serial>.bin.
#
# Exit codes: 0 done (or nothing to do); 1 refused or failed (nothing changed unless the output says
# so); 2 misuse (bad arguments, root needed, test variables as root); 130 interrupted (Ctrl-C, SIGTERM:
# no file is left half-written).
#
# TEST ONLY (honoured only when not root; as root the script refuses to run while any is set):
#   B2_TEST_ROOT   prefix for every system path (the data dir, the fprintd drop-in dir); local/ (calib,
#                  logs) is $B2_TEST_ROOT/local; no root needed
#   B2_SYSTEMCTL   (with B2_TEST_ROOT) the command run instead of /usr/bin/systemctl
#   B2_TEST_TEMPLATE  (with B2_TEST_ROOT) an absolute path to an 18869-byte template file the programs
#                  are checked against instead of the pinned proto/data/cmd02_template.bin
#   B2_FAKE_EUID   the euid the root checks see, to test them
set -euo pipefail
umask 077

# The test hooks redirect what is read, written and run: never as root.
TEST_VARS=(B2_TEST_ROOT B2_SYSTEMCTL B2_TEST_TEMPLATE B2_FAKE_EUID)
guard_euid=$(id -u)
[ "$guard_euid" -eq 0 ] || guard_euid=${B2_FAKE_EUID:-$guard_euid}
if [ "$guard_euid" = 0 ]; then
	for v in "${TEST_VARS[@]}"; do
		if [ -n "${!v+set}" ]; then
			printf 'FAILED: %s is set; the test-only variables are refused as root\n' "$v" >&2
			printf '  hint: unset it (sudo -E and env_keep pass it on): sudo env -u %s ...\n' "$v" >&2
			exit 2
		fi
	done
fi

SELF=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")
PY=/usr/bin/python3
SCRIPT=$(dirname -- "$SELF")/b2_program.py

usage() { sed -n '2,/^set /s/^# \{0,1\}//p' "$SELF"; }
# This script as a command the user can run from where they are: the path as invoked
# (tools/dev/b2-program.sh from the project root), ./NAME for a bare name in this
# directory (bash b2-program.sh), else the full path. Never a bare name: it is not on PATH.
self_cmd() {
	local c=$0
	case $c in
	*/*) ;;
	*) if [ "$PWD/$c" -ef "$SELF" ]; then c=./$c; else c=$SELF; fi ;;
	esac
	printf '%q' "$c"
}
fail() {
	printf 'FAILED: %s\n' "$1" >&2
	[ $# -lt 2 ] || printf '  hint: %s\n' "$2" >&2
	exit 2
}

orig_args=("$@")
cmd=""
run_dir=""
for arg in "$@"; do
	case $arg in
	-h | --help)
		usage
		exit 0
		;;
	esac
done
[ $# -gt 0 ] || fail "no subcommand" "use use-linux RUN_DIR, use-windows or status; see $(self_cmd) --help"
cmd=$1
shift
case $cmd in
use-linux)
	[ $# -eq 1 ] && [ -n "$1" ] || fail "use-linux needs RUN_DIR, and only that" "e.g. sudo $(self_cmd) use-linux local/calib/<stamp>"
	run_dir=$1
	;;
use-windows)
	[ $# -eq 0 ] || fail "use-windows takes no arguments" "see $(self_cmd) --help"
	;;
status)
	[ $# -le 1 ] || fail "status takes at most RUN_DIR" "see $(self_cmd) --help"
	[ $# -eq 0 ] || run_dir=$1
	[ $# -eq 0 ] || [ -n "$run_dir" ] || fail "status: RUN_DIR is empty" "see $(self_cmd) --help"
	;;
*) fail "unknown subcommand: $cmd" "use use-linux RUN_DIR, use-windows or status; see $(self_cmd) --help" ;;
esac

# use-linux and use-windows write /var/lib/fprint (root only): root, or a test root.
need_root() {
	[ -z "${B2_TEST_ROOT:-}" ] || return 0
	[ "$guard_euid" != 0 ] || return 0
	local line=(sudo "$(self_cmd)") a
	for a in "${orig_args[@]}"; do line+=("$(printf '%q' "$a")"); done
	printf '%s needs root; nothing was changed. Run:\n  %s\n' "$cmd" "${line[*]}" >&2
	exit 2
}
[ "$cmd" = status ] || need_root

[ -x "$PY" ] || fail "$PY not found" "install python: sudo pacman -S python"
# -I: no PYTHONPATH, no user site, no script dir on sys.path (it runs as root); -B: no __pycache__
# written into the project tree by root. The script loads its two helpers by explicit path.
py_args=("$cmd")
[ -z "$run_dir" ] || py_args+=("$run_dir")
exec "$PY" -I -B "$SCRIPT" --self "$(self_cmd)" -- "${py_args[@]}"
