#!/usr/bin/env bash
# Step A5 dev setup: make the system fprintd (its unit and sandbox unchanged) load the
# libfprint fork's build, and install the synaptics0078 data files where it reads them.
#
#   sudo tools/dev/a5-fprintd-dev.sh install   copy the fork's lib, the data files and a drop-in
#   sudo tools/dev/a5-fprintd-dev.sh install --diag   the same, plus raw image dumps (below)
#   tools/dev/a5-fprintd-dev.sh status         what fprintd loads and sees (more with sudo)
#   sudo tools/dev/a5-fprintd-dev.sh undo      remove exactly what install created
#   sudo tools/dev/a5-fprintd-dev.sh undo --keep-data   A6 hand-over to the package (below)
#   tools/dev/a5-fprintd-dev.sh check-lib FILE   install's lib checks on FILE (no root, read-only)
#   sudo tools/dev/a5-fprintd-dev.sh log on|off   packaged install: the driver's journal lines on/off
#   tools/dev/a5-fprintd-dev.sh log status       whether they are on (read-only)
#
# Options: --dry-run (print every action, change nothing), --data-dir DIR (install: data
# source, default local/synaptics0078), --diag (install: also dump every raw image, see
# below; asks first), --yes-diag (install: answer that question yes, e.g. without a
# terminal), --yes-delete-prints / --keep-prints (undo: answer the prompt about enrolled
# prints of this driver; the default answer is no), --yes-delete-diag / --keep-diag (undo:
# answer the prompt about the diag images; the default answer is no), --keep-data (undo:
# hand-over, below), -h/--help.
# Env: LIBFPRINT_FORK (default: ../libfprint next to this project). sudo drops it, so
# pass it on the sudo line: sudo LIBFPRINT_FORK=DIR tools/dev/a5-fprintd-dev.sh install
#
# install copies FORK/build/libfprint/libfprint-2.so.2.0.0 to /usr/local/lib/libfprint-0078-dev/,
# module.bin and program-<serial>.bin to /var/lib/fprint/synaptics0078/ (0700, files 0600),
# and adds /etc/systemd/system/fprintd.service.d/libfprint-0078-dev.conf (LD_LIBRARY_PATH,
# OPENCV_OPENCL_RUNTIME=disabled, G_MESSAGES_DEBUG for the driver's journal lines). It
# records what it created in a manifest; undo removes only that. Nothing under a
# package-owned path is written. Dev only: A6 ships a package.
#
# Upgrade: run install again after rebuilding the fork. It replaces the lib when the build
# differs (staged and checked as on the first run), rewrites the drop-in when its text
# changed (only one this script wrote and nobody edited since), updates the manifest, runs
# daemon-reload if the drop-in changed and try-restart if anything changed, and prints one
# "upgraded: ..." line. A run that changes nothing restarts nothing.
#
# A6 hand-over: first install the libfprint-sigfm-0078 package (pacman -U), then run
# undo --keep-data. It refuses while /usr/lib/libfprint-2.so.2 lacks the driver. It removes the
# drop-in, the lib, its link and the manifest as undo does, and keeps the data files, the
# enrolled prints and the diag dir untouched, dropped from the manifest, without asking
# (so not with --yes-delete-prints/--yes-delete-diag): the data files are the package's from
# then on (synaptics0078-setup status checks them). They leave the manifest on disk before the
# drop-in is touched, so a failed run is safe to re-run. Plain undo refuses (exit 1, nothing
# changed) once the package is installed while the manifest still records data files.
# check-lib FILE runs install's lib checks (SONAME, no RPATH, data dir, driver, every
# symbol of /usr/lib/libfprint-2.so.2) on FILE, e.g. the package's build; exit 0 if all
# pass, else 1.
#
# log (after the hand-over, for the A5 helpers a5-enroll, a5-verify and a5-suspend): the
# packaged fprintd sends no debug lines, by design. log on writes the drop-in
# /etc/systemd/system/fprintd.service.d/synaptics0078-debug.conf (mode 0644, through a 0600
# stage), which sets only G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print (the driver's
# lines and the SIGFM scores), then runs daemon-reload and try-restart. log off removes it, the
# same way. Those lines include the unit serial: run log off when testing is done. A run that
# changes nothing restarts nothing (log on still restarts a fprintd started before the drop-in
# was written, and reloads systemd if an earlier run stopped before that). log on refuses while
# the dev drop-in is present: it carries the same lines already. A failed systemctl is a FAILED
# line; running the same command again finishes the job. log status changes nothing.
#
# --diag: creates /var/lib/fprint/synaptics0078-diag (root only, 0700) and sets
# FP_SYNAPTICS0078_DUMP_DIR to it in the drop-in: the driver then writes a raw image of
# every touch there (biometric data). install without --diag turns the dumps off again and
# keeps the directory; undo deletes it after asking "Delete N diag images ...? [y/N]"
# (without a terminal, and always with --keep-data, it keeps it and prints the rm command).
# status shows diag on/off and, with sudo, how many files the directory holds.
#
# TEST ONLY (honoured only while A5_TEST_ROOT is set; never set them on a real system;
# as root the script refuses to run while any of them is set, e.g. passed by sudo -E):
#   A5_TEST_ROOT     prefix for every system path; no root needed, no chown
#   A5_SYSTEMCTL, A5_FPRINTD_LIST, A5_PACMAN   commands to run instead of the real ones
#   A5_MODULE_SHA256 expected module.bin sha256 (synthetic test data)
#   A5_FAKE_EUID     (not root only) the euid the root check above sees, to test it
set -euo pipefail
umask 022

# The test hooks redirect what is written and run: never as root.
TEST_VARS=(A5_TEST_ROOT A5_SYSTEMCTL A5_FPRINTD_LIST A5_PACMAN A5_MODULE_SHA256 A5_FAKE_EUID)
guard_euid=$(id -u)
[ "$guard_euid" -eq 0 ] || guard_euid=${A5_FAKE_EUID:-$guard_euid}
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
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
FORK_DEFAULT=$(cd -- "$ROOT/.." && pwd)/libfprint
FORK=${LIBFPRINT_FORK:-$FORK_DEFAULT}
DATA_SRC=$ROOT/local/synaptics0078

# The path fprintd sees (the drop-in names it), and the data dir compiled into the driver.
SYS_LIBDIR=/usr/local/lib/libfprint-0078-dev
SYS_DATA_DIR=/var/lib/fprint/synaptics0078
DRIVER=synaptics0078
SONAME=libfprint-2.so.2
LIBFILE=libfprint-2.so.2.0.0
MODULE_SIZE=10500
MODULE_SHA256=67f4a332d89f76fe12c57fa7f67b43a076f689e78267b888c694e26b05caacc1
PROGRAM_SIZE=18869
UNIT=fprintd.service
# --diag: where the driver dumps every raw image (FP_SYNAPTICS0078_DUMP_DIR, read at open).
# Sandbox check against the packaged unit (`systemctl cat fprintd.service`, A5b): fprintd
# runs as root (no User=, DynamicUser=no) with the full CapabilityBoundingSet.
# ProtectSystem=strict mounts everything read-only except /dev, /proc, /sys, the
# ReadWritePaths (/sys/devices) and the StateDirectory=fprint, i.e. /var/lib/fprint
# (read-write, StateDirectoryMode=0700). A directory below it is therefore writable; any
# path outside /var/lib/fprint would be read-only to fprintd. ProtectHome and PrivateTmp do
# not touch it; SystemCallFilter=@system-service allows mkdir, openat, fsync and rename
# (the driver's temp file + rename); MemoryDenyWriteExecute is unrelated. The driver passes
# explicit modes (dir 0700, files 0600), which UMask=0022 does not widen. So no unit change
# is needed. install creates the directory itself, so the driver's mkdir is a no-op.
SYS_DIAG_DIR=/var/lib/fprint/synaptics0078-diag
DIAG_ENV=FP_SYNAPTICS0078_DUMP_DIR

TR=${A5_TEST_ROOT:-}
SYSTEMCTL=(systemctl)
FPRINTD_LIST=(fprintd-list)
PACMAN=(pacman)
if [ -n "$TR" ]; then
	case $TR in /*) ;; *) echo "A5_TEST_ROOT must be an absolute path" >&2 && exit 2 ;; esac
	# Canonical, so the manifest's canonical-path check holds under it.
	TR=$(realpath -e -- "$TR") || exit 2
	[ -z "${A5_SYSTEMCTL:-}" ] || SYSTEMCTL=("$A5_SYSTEMCTL")
	[ -z "${A5_FPRINTD_LIST:-}" ] || FPRINTD_LIST=("$A5_FPRINTD_LIST")
	[ -z "${A5_PACMAN:-}" ] || PACMAN=("$A5_PACMAN")
	MODULE_SHA256=${A5_MODULE_SHA256:-$MODULE_SHA256}
fi

LIBDIR=$TR$SYS_LIBDIR
LIB=$LIBDIR/$LIBFILE
LINK=$LIBDIR/$SONAME
MANIFEST=$LIBDIR/a5-install.manifest
# The packaged libfprint fprintd loads without the drop-in: stock libfprint before A6, the
# libfprint-sigfm-0078 package's lib (with the driver) once it is installed.
STOCK_LIB=$TR/usr/lib/$SONAME
STATE_DIR=$TR/var/lib/fprint
DATA_DIR=$TR$SYS_DATA_DIR
DIAG_DIR=$TR$SYS_DIAG_DIR
DROPIN_DIR=$TR/etc/systemd/system/fprintd.service.d
DROPIN=$DROPIN_DIR/libfprint-0078-dev.conf
# log on: the packaged install's debug drop-in. Its first line marks it as ours: log on rewrites
# and log off removes only a file that carries it.
DEBUG_DROPIN=$DROPIN_DIR/synaptics0078-debug.conf
DEBUG_MARK="# Written by fingerprint-06cb-0078/tools/dev/a5-fprintd-dev.sh log on (packaged libfprint)."
DEBUG_TEXT="$DEBUG_MARK
# The driver's lines (ROM, serial, module upload, finger off) and the SIGFM scores in fprintd's
# journal, for the A5 helpers. The lines include the unit serial: keep the journal private.
# Turn it off when testing is done: sudo tools/dev/a5-fprintd-dev.sh log off
[Service]
Environment=\"G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print\"
"
PROC=$TR/proc
DROPIN_TEXT="# Written by fingerprint-06cb-0078/tools/dev/a5-fprintd-dev.sh (A5, dev only).
# Remove with: sudo tools/dev/a5-fprintd-dev.sh undo
# Loads the libfprint fork instead of the packaged libfprint. The sandbox is unchanged.
[Service]
Environment=LD_LIBRARY_PATH=$SYS_LIBDIR
# SIGFM needs no OpenCL; keep OpenCV from loading an OpenCL runtime inside fprintd.
Environment=OPENCV_OPENCL_RUNTIME=disabled
# The driver's info lines (ROM, serial, module upload, finger off) and the SIGFM scores in
# the journal, for the A5 checks. The serial makes the journal private.
Environment=\"G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print\"
"
# The text above is byte-identical to what the first A5 version wrote, so a plain install
# over that version changes nothing. --diag appends these lines.
DROPIN_DIAG_TEXT="# --diag: a raw image of every touch (biometric data) goes to this root-only directory.
# Turn it off with install (without --diag); undo deletes the directory after asking.
Environment=$DIAG_ENV=$SYS_DIAG_DIR
"

usage() { sed -n '2,/^set /s/^# \{0,1\}//p' "$SELF"; }
# This script as a command the user can run from where they are: the path as invoked
# (tools/dev/a5-fprintd-dev.sh from the project root), ./NAME for a bare name in this
# directory (bash a5-fprintd-dev.sh), else the full path. Never a bare name: it is not on PATH.
self_cmd() {
	local c=$0
	case $c in
	*/*) ;;
	*) if [ "$PWD/$c" -ef "$SELF" ]; then c=./$c; else c=$SELF; fi ;;
	esac
	printf '%q' "$c"
}
# The exit status of fail: 2, or 1 where a failed check is the answer (check-lib, the
# preflight of undo --keep-data).
FAIL_RC=2
fail() {
	printf 'FAILED: %s\n' "$1" >&2
	[ $# -lt 2 ] || printf '  hint: %s\n' "$2" >&2
	exit "$FAIL_RC"
}
ok() { printf '  ok    %s\n' "$*"; }
note() { printf '  note  %s\n' "$*"; }
# To stderr: some functions' stdout is data (prints_dirs), a warning must never become a path.
warn() { printf '  WARN  %s\n' "$*" >&2; }
already() { printf '  already %s\n' "$*"; }

dry_run=0
prints_answer=""
diag=0
yes_diag=0
diag_answer=""
cmd=""
data_given=0
keep_data=0
lib_arg=""
log_arg=""
orig_args=("$@")
while [ $# -gt 0 ]; do
	# log's ACTION first: `log status` is not the status subcommand.
	if [ "$cmd" = log ] && [ -z "$log_arg" ]; then
		case $1 in on | off | status)
			log_arg=$1
			shift
			continue
			;;
		esac
	fi
	case $1 in
	install | status | undo | check-lib | log)
		[ -z "$cmd" ] || fail "more than one subcommand: $cmd, $1" "see $(self_cmd) --help"
		cmd=$1
		;;
	--dry-run) dry_run=1 ;;
	--data-dir)
		[ $# -ge 2 ] && [ -n "$2" ] || fail "--data-dir needs a directory"
		DATA_SRC=$2
		data_given=1
		shift
		;;
	--data-dir=*)
		DATA_SRC=${1#--data-dir=}
		[ -n "$DATA_SRC" ] || fail "--data-dir needs a directory"
		data_given=1
		;;
	--yes-delete-prints) prints_answer=yes ;;
	--keep-prints) prints_answer=no ;;
	--diag) diag=1 ;;
	--yes-diag) yes_diag=1 ;;
	--yes-delete-diag) diag_answer=yes ;;
	--keep-diag) diag_answer=no ;;
	--keep-data) keep_data=1 ;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		# check-lib's FILE (a name starting with - is passed as ./-name).
		if [ "$cmd" = check-lib ] && [ -z "$lib_arg" ] && [ -n "$1" ] && [[ $1 != -* ]]; then
			lib_arg=$1
		else
			fail "unknown argument: $1" "see $(self_cmd) --help"
		fi
		;;
	esac
	shift
done
[ -n "$cmd" ] || fail "no subcommand" "use install, status, undo, check-lib or log; see $(self_cmd) --help"
[ "$cmd" != log ] || [ -n "$log_arg" ] ||
	fail "log needs on, off or status" "e.g. sudo $(self_cmd) log on"
[ "$cmd" != check-lib ] || [ -n "$lib_arg" ] ||
	fail "check-lib needs a FILE" "e.g. $(self_cmd) check-lib build/libfprint/$LIBFILE"
[ "$keep_data" -eq 0 ] || [ "$cmd" = undo ] || fail "--keep-data only applies to undo"
[ "$keep_data" -eq 0 ] || [ "$prints_answer" != yes ] ||
	fail "--keep-data keeps the enrolled prints; it cannot be combined with --yes-delete-prints"
[ "$keep_data" -eq 0 ] || [ "$diag_answer" != yes ] ||
	fail "--keep-data keeps the diag images; it cannot be combined with --yes-delete-diag" \
		"delete them by hand afterwards (undo --keep-data prints the command)"
[ "$data_given" -eq 0 ] || [ "$cmd" = install ] || fail "--data-dir only applies to install"
[ -z "$prints_answer" ] || [ "$cmd" = undo ] || fail "--yes-delete-prints/--keep-prints only apply to undo"
[ -z "$diag_answer" ] || [ "$cmd" = undo ] || fail "--yes-delete-diag/--keep-diag only apply to undo"
[ "$diag" -eq 0 ] || [ "$cmd" = install ] || fail "--diag only applies to install"
[ "$yes_diag" -eq 0 ] || [ "$diag" -eq 1 ] || fail "--yes-diag answers the --diag question; add --diag"

is_root() { [ "$(id -u)" -eq 0 ]; }
# install/undo write system paths: root, or a test root. A dry run may preview as a user.
need_root() {
	[ -z "$TR" ] || return 0
	is_root && return 0
	if [ "$dry_run" -eq 1 ]; then
		[ "$cmd" = log ] ||
			note "not root: dry run only; paths under /var/lib/fprint (mode 0700) cannot be inspected, so the plan assumes they are absent"
		return 0
	fi
	local line=(sudo)
	[ -z "${LIBFPRINT_FORK:-}" ] || line+=("LIBFPRINT_FORK=$LIBFPRINT_FORK")
	line+=("$SELF" "${orig_args[@]}")
	printf -v line_q '%q ' "${line[@]}"
	printf '%s needs root; nothing was changed. Run:\n  %s\n' "$cmd" "${line_q% }" >&2
	exit 2
}

# Owner of what install writes: root:root, or the test user under a test root.
OWN=()
expect_uid=0
if [ -n "$TR" ]; then expect_uid=$(id -u); else OWN=(-o root -g root); fi

sha() { sha256sum -- "$1" | cut -d' ' -f1; }
fsize() { stat -c %s -- "$1"; }
# Print an action, then do it (unless --dry-run).
act() {
	local q
	printf -v q '%q ' "$@"
	printf '+ %s\n' "${q% }"
	[ "$dry_run" -eq 1 ] || "$@"
}
# A real directory (not a symlink) owned by the expected user, or absent.
check_dir() {
	local d=$1
	[ ! -L "$d" ] || fail "$d is a symlink; refusing to write through it"
	[ -e "$d" ] || return 0
	[ -d "$d" ] || fail "$d exists and is not a directory"
	[ "$(stat -c %u -- "$d")" = "$expect_uid" ] || fail "$d is not owned by uid $expect_uid; refusing to use it"
}
# A regular file (not a symlink), or absent.
check_file() {
	local f=$1
	[ ! -L "$f" ] || fail "$f is a symlink; refusing to replace or follow it"
	[ ! -e "$f" ] || [ -f "$f" ] || fail "$f exists and is not a regular file"
}

# ---------------------------------------------------------------- manifest
# One line per thing install created: "dir PATH", "file PATH SHA256", "link PATH TARGET",
# "diagdir PATH" (the --diag dump dir: undo deletes it with its files, after asking).
declare -A MTYPE=() MVAL=()
MORDER=()
# A path install writes, of that type, spelled canonically (no //, ., .., trailing /) and
# with no symlink in its parent directories: undo deletes what the manifest names, so an
# entry must never lead it outside these exact targets.
manifest_path_ok() {
	local t=$1 p=$2 parent
	case $p in /*) ;; *) return 1 ;; esac
	case $p in *//* | */./* | */../* | */. | */.. | */) return 1 ;; esac
	parent=$(realpath -m -- "${p%/*}" 2>/dev/null) || return 1
	[ "$parent/${p##*/}" = "$p" ] || return 1
	case $t:$p in
	"dir:$LIBDIR" | "dir:$STATE_DIR" | "dir:$DATA_DIR" | "dir:$DROPIN_DIR") return 0 ;;
	"file:$LIB" | "file:$DROPIN" | "file:$DATA_DIR/module.bin" | "link:$LINK") return 0 ;;
	"diagdir:$DIAG_DIR") return 0 ;;
	esac
	[ "$t" = file ] && [ "${p%/*}" = "$DATA_DIR" ] && [[ ${p##*/} =~ ^program-[0-9a-f]{12}\.bin$ ]]
}
manifest_load() {
	check_file "$MANIFEST"
	[ -f "$MANIFEST" ] || return 0
	# Only a manifest its owner alone can write is trusted: root's, or the test user's.
	[ "$(stat -c %u -- "$MANIFEST")" = "$expect_uid" ] ||
		fail "$MANIFEST is not owned by uid $expect_uid; refusing to act on it" "inspect it; remove it by hand if it is not ours"
	! (($(stat -c '%#a' -- "$MANIFEST") & 8#022)) ||
		fail "$MANIFEST is group- or world-writable; refusing to act on it" "inspect it; remove it by hand if it is not ours"
	local t p v
	while IFS=$'\t' read -r t p v; do
		case $t in '' | '#'*) continue ;; esac
		manifest_path_ok "$t" "$p" ||
			fail "$MANIFEST names '$t $p', which install never writes; refusing to act on it" \
				"inspect it; nothing was changed"
		[ -n "${MTYPE[$p]:-}" ] || MORDER+=("$p")
		MTYPE[$p]=$t
		MVAL[$p]=$v
	done <"$MANIFEST"
}
manifest_add() {
	[ -n "${MTYPE[$2]:-}" ] || MORDER+=("$2")
	MTYPE[$2]=$1
	MVAL[$2]=${3:-}
	manifest_save
}
manifest_drop() {
	unset "MTYPE[$1]" "MVAL[$1]"
	local p rest=()
	for p in "${MORDER[@]}"; do [ "$p" = "$1" ] || rest+=("$p"); done
	MORDER=("${rest[@]}")
}
# MTMP: the manifest's temp file while it is written (the EXIT trap below removes it).
MTMP=""
manifest_save() {
	[ "$dry_run" -eq 0 ] || return 0
	local p
	MTMP=$(mktemp "$LIBDIR/.a5-install.manifest.XXXXXX")
	{
		printf '# a5-fprintd-dev.sh install manifest: what undo removes. Do not edit.\n'
		for p in "${MORDER[@]}"; do printf '%s\t%s\t%s\n' "${MTYPE[$p]}" "$p" "${MVAL[$p]}"; done
	} >"$MTMP"
	chmod 0644 "$MTMP"
	mv -f -- "$MTMP" "$MANIFEST"
	MTMP=""
}

# ---------------------------------------------------------------- staged copies
# Install SRC as DST: copy it to a stage that mktemp creates 0600 in DST's root-owned dir,
# run CHECK on that copy (nobody else can change it, unlike SRC), then chmod, chown and
# rename it into place (atomic). Sets STAGED_SHA to the sha256 of what was installed.
STAGE=""
STAGED_SHA=""
trap '[ -z "$STAGE" ] || rm -f -- "$STAGE"; [ -z "$MTMP" ] || rm -f -- "$MTMP"' EXIT
install_staged() {
	local src=$1 dst=$2 mode=$3
	shift 3
	if [ "$dry_run" -eq 1 ]; then
		printf '+ copy %q to a 0600 stage in %q, check the copy, chmod %s, rename it to %q\n' "$src" "${dst%/*}" "$mode" "$dst"
		return 0
	fi
	STAGE=$(mktemp -- "${dst%/*}/.${dst##*/}.a5-new.XXXXXX")
	printf '+ stage %q (0600)\n' "$STAGE"
	act cp -- "$src" "$STAGE"
	"$@" "$STAGE"
	STAGED_SHA=$(sha "$STAGE")
	[ -n "$TR" ] || act chown root:root -- "$STAGE"
	act chmod "$mode" -- "$STAGE"
	act mv -f -T -- "$STAGE" "$dst"
	STAGE=""
}

# ---------------------------------------------------------------- diag dir
# Regular files in the diag dir: the images (and any temp file the driver left behind).
diag_count() { find "$1" -mindepth 1 -maxdepth 1 -type f -printf . | wc -c; }
# Prints "ok" if undo may delete the diag dir, else why not. Only the exact canonical path
# install records, a real directory (no symlink, no mount point) of the expected owner,
# holding regular files only (no subdirectory, symlink, fifo...): a recursive delete as root
# then cannot reach past it. Any failing check prints something other than "ok".
diag_check() {
	local p=$1 uid dev pdev bad
	manifest_path_ok diagdir "$p" || { echo "it is not the path install writes" && return 0; }
	[ ! -L "$p" ] || { echo "it is a symlink" && return 0; }
	[ -d "$p" ] || { echo "it is not a directory" && return 0; }
	uid=$(stat -c %u -- "$p") && dev=$(stat -c %d -- "$p") && pdev=$(stat -c %d -- "${p%/*}") ||
		{ echo "stat failed" && return 0; }
	[ "$uid" = "$expect_uid" ] || { echo "it is owned by uid $uid, not $expect_uid" && return 0; }
	[ "$dev" = "$pdev" ] || { echo "it is a mount point" && return 0; }
	# A bind mount from the same filesystem has its parent's device number: ask mountpoint.
	if command -v mountpoint >/dev/null && mountpoint -q -- "$p"; then
		echo "it is a mount point" && return 0
	fi
	bad=$(find "$p" -mindepth 1 ! -type f -printf '%P\n' -quit) || { echo "find failed" && return 0; }
	[ -z "$bad" ] || { echo "it holds '$bad', which is not a regular file" && return 0; }
	echo ok
}
# install, on the diag dir it recorded (only that one: it never chmods a dir it did not
# create): back to 0700 if its mode was widened since, as it holds raw fingerprint images.
diag_mode_fix() {
	local m
	m=$(stat -c '%#a' -- "$DIAG_DIR")
	((m & 8#077)) || return 0
	warn "$DIAG_DIR is mode $(stat -c %a -- "$DIAG_DIR"), not 0700; setting it back to 0700 (install created it)"
	act chmod 0700 -- "$DIAG_DIR"
}
# The command that deletes the diag dir by hand (printed when undo keeps it).
diag_rm_hint() {
	printf 'sudo rm -r --one-file-system -- %q   (or: sudo %q undo --yes-delete-diag)' "$DIAG_DIR" "$SELF"
}
# Ask a y/N question on the terminal; no terminal or EOF is no.
ask_yes() {
	local reply=""
	printf '%s [y/N] ' "$1"
	read -r reply || {
		reply=""
		echo
	}
	case $reply in [yY] | [yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- systemd
systemd_show() { "${SYSTEMCTL[@]}" show "$UNIT" -p Environment,MainPID,ActiveState 2>&1 || true; }
main_pid() { "${SYSTEMCTL[@]}" show "$UNIT" -p MainPID --value 2>/dev/null || echo 0; }

# Paths of libfprint-2.so* that a process maps, " (deleted)" kept when the file was replaced.
mapped_libfprint() {
	local maps=$PROC/$1/maps
	[ -r "$maps" ] || return 1
	awk '$6 ~ /\/libfprint-2\.so/ { p = $6; for (i = 7; i <= NF; i++) p = p " " $i; print p }' "$maps" | sort -u
}

# ---------------------------------------------------------------- install
# The checks each installed file must pass: run on the source in the preflight (early,
# clear failures), and again on the root-owned staged copy that is actually installed.
BUILD_HINT=""
check_lib() {
	local f=$1 soname missing
	# Not an ELF file: readelf fails, and under pipefail that must not end the script silently.
	soname=$(readelf -dW -- "$f" 2>/dev/null | sed -n 's/.*(SONAME).*\[\(.*\)\].*/\1/p') || soname=""
	[ "$soname" = "$SONAME" ] || fail "$f has SONAME '${soname:-none}', expected $SONAME"
	# No `grep -q` at the end of these pipes: it exits at the first match, the writer then
	# dies of SIGPIPE on a large file, and pipefail turns a match into a failure.
	! readelf -dW -- "$f" | grep -E '\((RPATH|RUNPATH)\)' >/dev/null ||
		fail "$f carries an RPATH/RUNPATH; a library fprintd loads as root must not search other paths" "$BUILD_HINT"
	LC_ALL=C strings -a -- "$f" | grep -xF "$SYS_DATA_DIR" >/dev/null ||
		fail "$f was not configured with synaptics0078_data_dir=$SYS_DATA_DIR (fprintd only reads its StateDirectory)" \
			"cd $FORK && meson configure build -Dsynaptics0078_data_dir=$SYS_DATA_DIR && meson compile -C build"
	LC_ALL=C grep -qaF "libfprint-$DRIVER" -- "$f" ||
		fail "driver $DRIVER is not built into $f" "$BUILD_HINT"
	if [ -f "$STOCK_LIB" ]; then
		missing=$(comm -23 <(nm -D --defined-only -- "$STOCK_LIB" | awk '{print $3}' | sort -u) \
			<(nm -D --defined-only -- "$f" | awk '{print $3}' | sort -u))
		[ -z "$missing" ] || fail "$f lacks symbols of the packaged $SONAME: $(echo "$missing" | paste -sd' ')"
	fi
}
check_module() {
	local f=$1 got
	[ "$(fsize "$f")" = "$MODULE_SIZE" ] || fail "$f is $(fsize "$f") bytes, expected $MODULE_SIZE"
	got=$(sha "$f")
	[ "$got" = "$MODULE_SHA256" ] || fail "$f has sha256 $got, expected $MODULE_SHA256"
}
PROGRAM_SHA=""
check_program() {
	local f=$1
	[ "$(fsize "$f")" = "$PROGRAM_SIZE" ] || fail "$f is $(fsize "$f") bytes, expected $PROGRAM_SIZE"
	[ "$(head -c1 -- "$f" | od -An -tx1 | tr -d ' \n')" = 02 ] || fail "$f does not start with the 0x02 opcode"
	# The copy must be the program the preflight checked (and compared with an installed one).
	[ -z "$PROGRAM_SHA" ] || [ "$(sha "$f")" = "$PROGRAM_SHA" ] ||
		fail "$f differs from the program checked in the preflight (changed while copied?)" "run install again"
}

cmd_install() {
	need_root
	echo "A5 fprintd dev install: fork $FORK, data from $DATA_SRC${TR:+, TEST ROOT $TR}"
	[ "$dry_run" -eq 0 ] || echo "dry run: nothing is changed"
	echo "preflight:"
	local tool
	for tool in readelf nm strings sha256sum install; do
		command -v "$tool" >/dev/null || fail "$tool not found" "install binutils/coreutils"
	done

	# 1. The fork's library: a regular file, SONAME libfprint-2.so.2, the driver and the data dir
	#    fprintd needs compiled in, no RPATH/RUNPATH, and the stock ABI (fprintd stays unchanged).
	local src=$FORK/build/libfprint/$LIBFILE
	BUILD_HINT="build the fork: cd $FORK && meson setup build -Dprefix=/usr/local && meson compile -C build (or set LIBFPRINT_FORK)"
	[ -e "$src" ] || [ -L "$src" ] || fail "no fork build at $src" "$BUILD_HINT"
	[ ! -L "$src" ] || fail "$src is a symlink; expected the real library file"
	[ -f "$src" ] || fail "$src is not a regular file"
	check_lib "$src"
	if [ -f "$STOCK_LIB" ]; then
		ok "exports every symbol of the packaged $STOCK_LIB"
	else
		note "no $STOCK_LIB to compare exported symbols with"
	fi
	local src_sha
	src_sha=$(sha "$src")
	ok "fork lib: $src ($SONAME, driver $DRIVER, data dir $SYS_DATA_DIR, sha256 ${src_sha:0:16}...)"

	# 2. The data files: module.bin (size, pinned sha256) and exactly one program-<serial>.bin.
	local data_hint="create them (from $ROOT): proto/.venv/bin/python tools/synaptics0078_data.py"
	[ -d "$DATA_SRC" ] || fail "no data directory $DATA_SRC" "$data_hint"
	local module=$DATA_SRC/module.bin
	[ -e "$module" ] || fail "no $module" "$data_hint"
	[ ! -L "$module" ] && [ -f "$module" ] || fail "$module is not a regular file"
	check_module "$module"
	local module_sha=$MODULE_SHA256
	local programs
	shopt -s nullglob
	programs=("$DATA_SRC"/program-*.bin)
	shopt -u nullglob
	[ ${#programs[@]} -eq 1 ] || fail "expected exactly one program-<serial>.bin in $DATA_SRC, found ${#programs[@]}" "$data_hint"
	local program=${programs[0]} pname
	pname=$(basename -- "$program")
	[[ $pname =~ ^program-[0-9a-f]{12}\.bin$ ]] || fail "$pname is not named program-<12 lowercase hex>.bin"
	[ ! -L "$program" ] && [ -f "$program" ] || fail "$program is not a regular file"
	check_program "$program"
	local program_sha
	program_sha=$(sha "$program")
	PROGRAM_SHA=$program_sha
	ok "data: module.bin ($MODULE_SIZE B, sha256 ok), $pname ($PROGRAM_SIZE B, opcode 0x02)"

	# 3. The targets: real directories of root, files we created or identical ones, no package paths.
	[ -d "$TR/usr/local/lib" ] || fail "$TR/usr/local/lib does not exist"
	[ -d "$TR/etc/systemd/system" ] || fail "$TR/etc/systemd/system does not exist"
	[ -d "$TR/var/lib" ] || fail "$TR/var/lib does not exist"
	local d
	for d in "$LIBDIR" "$STATE_DIR" "$DATA_DIR" "$DROPIN_DIR"; do check_dir "$d"; done
	[ "$diag" -eq 0 ] || check_dir "$DIAG_DIR"
	if [ -d "$LIBDIR" ]; then
		[ -f "$MANIFEST" ] || fail "$LIBDIR exists but has no manifest: not created by this script, refusing to touch it" \
			"look at it and remove it yourself if it is a leftover"
	fi
	manifest_load
	# An earlier install of the lib: this run upgrades it (the summary line says "upgraded").
	# A manifest that lists only what an undo kept (e.g. the diag dir) is no install.
	local upgrade=0
	[ "${MTYPE[$LIB]:-}" != file ] || [ ! -e "$LIB" ] || upgrade=1
	check_file "$LIB"
	if [ -e "$LIB" ] && [ "${MTYPE[$LIB]:-}" != file ]; then fail "$LIB exists and is not recorded in the manifest"; fi
	# The installed lib must be the one install wrote (the manifest's sha256): a lib replaced
	# by hand since then is never overwritten, as the edited drop-in below.
	if [ -f "$LIB" ] && [ "$(sha "$LIB")" != "${MVAL[$LIB]:-}" ]; then
		fail "$LIB was replaced since install (sha256 differs from the manifest); refusing to overwrite it" \
			"inspect it; remove it (sudo rm -- $LIB) or run undo, then install again"
	fi
	if [ -L "$LINK" ]; then
		[ "$(readlink -- "$LINK")" = "$LIBFILE" ] || fail "$LINK points to $(readlink -- "$LINK"), not $LIBFILE"
	elif [ -e "$LINK" ]; then
		fail "$LINK exists and is not a symlink"
	fi
	local f
	for f in "$DATA_DIR/module.bin:$module_sha" "$DATA_DIR/$pname:$program_sha"; do
		local path=${f%:*} want=${f##*:}
		check_file "$path"
		if [ -e "$path" ] && [ "$(sha "$path")" != "$want" ]; then
			fail "$path exists and differs from $DATA_SRC/$(basename -- "$path"); refusing to overwrite it" \
				"compare them; if the installed one is stale, remove it (or run undo if install created it) and install again"
		fi
	done
	if [ -d "$DATA_DIR" ] && (($(stat -c '%#a' -- "$DATA_DIR") & 8#077)); then
		warn "$DATA_DIR is mode $(stat -c %a -- "$DATA_DIR"), not 0700 (left as is: install did not create it)"
	fi
	if [ "$diag" -eq 1 ] && { [ -e "$DIAG_DIR" ] || [ -L "$DIAG_DIR" ]; } && [ "${MTYPE[$DIAG_DIR]:-}" != diagdir ]; then
		fail "$DIAG_DIR exists and was not created by this script; refusing to use it for raw images" \
			"look at it; move it away or remove it (sudo rm -r -- $DIAG_DIR) and install again"
	fi
	# The drop-in: rewritten only if this script wrote it and nobody edited it since (its
	# sha256 is the one in the manifest); identical text is fine either way.
	local dropin_text=$DROPIN_TEXT
	[ "$diag" -eq 0 ] || dropin_text+=$DROPIN_DIAG_TEXT
	check_file "$DROPIN"
	if [ -e "$DROPIN" ] && [ "$(cat -- "$DROPIN")" != "${dropin_text%$'\n'}" ]; then
		[ "${MTYPE[$DROPIN]:-}" = file ] ||
			fail "$DROPIN exists with other content and was not written by this script"
		[ "$(sha "$DROPIN")" = "${MVAL[$DROPIN]}" ] ||
			fail "$DROPIN was edited since install (sha256 differs from the manifest); refusing to overwrite it" \
				"keep your edits in another drop-in; then remove it (sudo rm -- $DROPIN) and install again"
	fi
	if command -v "${PACMAN[0]}" >/dev/null; then
		for f in "$LIBDIR" "$LIB" "$LINK" "$MANIFEST" "$DATA_DIR" "$DATA_DIR/module.bin" "$DATA_DIR/$pname" "$DROPIN_DIR" "$DROPIN" "$DIAG_DIR"; do
			if [ -e "$f" ] || [ -L "$f" ]; then
				! "${PACMAN[@]}" -Qo -- "$f" >/dev/null 2>&1 || fail "$f belongs to a package; refusing to write it"
			fi
		done
		ok "no target path belongs to a package"
	else
		note "pacman not found: package-ownership guard skipped"
	fi
	ok "targets: $LIBDIR, $DATA_DIR, $DROPIN$([ "$diag" -eq 0 ] || printf ', %s' "$DIAG_DIR")"

	# 4. --diag: raw images of every touch are biometric data; ask before turning it on.
	if [ "$diag" -eq 1 ]; then
		local question="--diag stores raw fingerprint images of every touch in $SYS_DIAG_DIR (root only). Continue?"
		if [ "$yes_diag" -eq 1 ]; then
			ok "diag: confirmed by --yes-diag"
		elif [ "$dry_run" -eq 1 ]; then
			echo "dry run: would ask '$question [y/N]' (default no)"
		elif [ -t 0 ]; then
			ask_yes "$question" || fail "--diag not confirmed; nothing was changed"
		else
			fail "--diag stores raw fingerprint images and needs a yes, but there is no terminal to ask; nothing was changed" \
				"run it in a terminal, or add --yes-diag to confirm"
		fi
	fi

	echo "install:"
	local changed=0 dropin_changed=0 done_parts=()
	# The lib dir first: it holds the manifest that records everything after it.
	if [ -d "$LIBDIR" ]; then
		already "present: $LIBDIR"
	else
		act install -d -m 0755 "${OWN[@]}" -- "$LIBDIR"
		manifest_add dir "$LIBDIR"
		changed=1
	fi
	# The library: the checks run again on the staged copy (the source is the user's).
	# An installed lib matches its record (preflight), so the record needs no update here.
	local old_sha=""
	[ ! -f "$LIB" ] || old_sha=$(sha "$LIB")
	if [ "$old_sha" = "$src_sha" ]; then
		already "installed: $LIB (same as the fork build)"
	else
		install_staged "$src" "$LIB" 0644 check_lib
		# manifest_add replaces the entry of an existing path in place: never a duplicate.
		[ "$dry_run" -eq 1 ] || manifest_add file "$LIB" "$STAGED_SHA"
		changed=1
		local new_sha=${STAGED_SHA:-$src_sha}
		if [ -n "$old_sha" ]; then
			done_parts+=("lib sha ${old_sha:0:12}... -> ${new_sha:0:12}...")
		else
			done_parts+=("lib")
		fi
	fi
	if [ -L "$LINK" ]; then
		already "linked: $LINK -> $LIBFILE"
		[ "${MTYPE[$LINK]:-}" = link ] || manifest_add link "$LINK" "$LIBFILE"
	else
		act ln -s -T -- "$LIBFILE" "$LINK"
		manifest_add link "$LINK" "$LIBFILE"
		changed=1
		done_parts+=("$SONAME link")
	fi

	# The data files: the driver's directory under fprintd's StateDirectory, root only.
	if [ -d "$STATE_DIR" ]; then
		already "present: $STATE_DIR"
	else
		act install -d -m 0700 "${OWN[@]}" -- "$STATE_DIR"
		manifest_add dir "$STATE_DIR"
	fi
	if [ -d "$DATA_DIR" ]; then
		already "present: $DATA_DIR"
	else
		act install -d -m 0700 "${OWN[@]}" -- "$DATA_DIR"
		manifest_add dir "$DATA_DIR"
		changed=1
	fi
	for f in "$module:check_module" "$program:check_program"; do
		local s=${f%:*} check=${f##*:}
		local dst
		dst=$DATA_DIR/$(basename -- "$s")
		if [ -f "$dst" ]; then
			already "present: $dst (identical)"
			continue
		fi
		install_staged "$s" "$dst" 0600 "$check"
		[ "$dry_run" -eq 1 ] || manifest_add file "$dst" "$STAGED_SHA"
		changed=1
		done_parts+=("$(basename -- "$dst")")
	done

	# --diag: the dump dir next to the data dir, root only. Without --diag a dir from an
	# earlier --diag install stays (the drop-in below stops the dumps); undo removes it.
	if [ "$diag" -eq 1 ]; then
		if [ -d "$DIAG_DIR" ]; then
			already "present: $DIAG_DIR ($(diag_count "$DIAG_DIR") file(s))"
			diag_mode_fix
		else
			act install -d -m 0700 "${OWN[@]}" -- "$DIAG_DIR"
			manifest_add diagdir "$DIAG_DIR"
			changed=1
			done_parts+=("diag dir")
		fi
	elif [ "${MTYPE[$DIAG_DIR]:-}" = diagdir ] && [ -d "$DIAG_DIR" ] && [ ! -L "$DIAG_DIR" ]; then
		note "kept $DIAG_DIR ($(diag_count "$DIAG_DIR") file(s)) from a --diag install: no new dumps without --diag; undo deletes it (asks first)"
		diag_mode_fix
	fi

	# The drop-in last, so fprintd never points at a lib that is not there yet.
	if [ -d "$DROPIN_DIR" ]; then
		already "present: $DROPIN_DIR"
	else
		act install -d -m 0755 "${OWN[@]}" -- "$DROPIN_DIR"
		manifest_add dir "$DROPIN_DIR"
	fi
	if [ -f "$DROPIN" ] && [ "$(cat -- "$DROPIN")" = "${dropin_text%$'\n'}" ]; then
		already "written: $DROPIN"
	else
		if [ -e "$DROPIN" ]; then
			done_parts+=("drop-in rewritten (diag $([ "$diag" -eq 1 ] && echo on || echo off))")
		else
			done_parts+=("drop-in$([ "$diag" -eq 0 ] || echo ' (diag on)')")
		fi
		echo "+ write $DROPIN (mode 0644, through a 0600 stage in $DROPIN_DIR):"
		printf '%s' "$dropin_text" | sed 's/^/    | /'
		if [ "$dry_run" -eq 0 ]; then
			STAGE=$(mktemp -- "$DROPIN_DIR/.libfprint-0078-dev.conf.a5-new.XXXXXX")
			printf '%s' "$dropin_text" >"$STAGE"
			[ -n "$TR" ] || chown root:root -- "$STAGE"
			chmod 0644 -- "$STAGE"
			act mv -f -T -- "$STAGE" "$DROPIN"
			STAGE=""
			manifest_add file "$DROPIN" "$(sha "$DROPIN")"
		fi
		changed=1
		dropin_changed=1
	fi

	echo "fprintd:"
	if [ "$changed" -eq 0 ]; then
		already "installed: nothing changed, fprintd not restarted"
	else
		[ "$dropin_changed" -eq 0 ] || act "${SYSTEMCTL[@]}" daemon-reload
		# Restarts a running fprintd only; otherwise D-Bus starts it on demand with the drop-in.
		act "${SYSTEMCTL[@]}" try-restart "$UNIT"
		note "fprintd is D-Bus activated: if it was not running, the next client (fprintd-list, a lock screen) starts it with the fork"
		local summary verb=installed
		[ "$upgrade" -eq 0 ] || verb=upgraded
		[ "$dry_run" -eq 0 ] || verb="dry run, would be $verb"
		summary=$(printf '%s, ' "${done_parts[@]}")
		summary=${summary%, }
		echo "$verb: ${summary:-directories}; fprintd restarted (if it was running)"
	fi
	[ "$dry_run" -eq 1 ] || systemd_show | sed 's/^/  /'
	echo "next: $(self_cmd) status"
}

# ---------------------------------------------------------------- undo
# The packaged lib fprintd loads without the drop-in has the driver: the
# libfprint-sigfm-0078 package is installed (A6), and the data files are its own.
stock_has_driver() { [ -f "$STOCK_LIB" ] && LC_ALL=C grep -qaF "libfprint-$DRIVER" -- "$STOCK_LIB"; }
# The manifest (as loaded) records data files: install's copies in the data dir.
manifest_has_data_files() {
	local p
	for p in "${MORDER[@]}"; do
		[ "${MTYPE[$p]}" = file ] && [ "${p%/*}" = "$DATA_DIR" ] && return 0
	done
	return 1
}
prints_dirs() {
	# Enrolled prints of THIS driver: STATE_DIR/<user>/synaptics0078. Never the data dir
	# (STATE_DIR/synaptics0078, which fprintd lists as a "user"), never other drivers.
	local d user
	shopt -s nullglob
	for d in "$STATE_DIR"/*/"$DRIVER"; do
		user=$(basename -- "$(dirname -- "$d")")
		[ "$user" != "$DRIVER" ] || continue
		if [ -L "$d" ] || [ -L "$(dirname -- "$d")" ] || [ ! -d "$d" ]; then
			warn "skipping $d: not a real directory"
			continue
		fi
		printf '%s\n' "$d"
	done
	shopt -u nullglob
}

remove_recorded() {
	# Remove one manifest entry if it is still what install created. Returns 0 when it is
	# gone, 1 when it was kept (changed since install, or cannot be removed), 2 when a
	# directory is left in place because it is not empty, 3 when the state dir stays only
	# because it holds the diag dir kept on request (no failure). Only 0 drops the entry:
	# the manifest keeps the rest, so undo can be run again.
	# Called in an `||` context, where set -e is off: every step checks its own status.
	local p=$1 t=${MTYPE[$1]} v=${MVAL[$1]}
	if ! manifest_path_ok "$t" "$p"; then
		warn "kept $p: not a path install writes"
		return 1
	fi
	case $t in
	file)
		if [ ! -e "$p" ] && [ ! -L "$p" ]; then
			already "removed: $p"
		elif [ -L "$p" ] || [ ! -f "$p" ] || [ "$(sha "$p")" != "$v" ]; then
			warn "kept $p: it changed since install (sha256 differs); remove it yourself if you want it gone"
			return 1
		else
			act rm -f -- "$p" || { warn "kept $p: rm failed" && return 1; }
		fi
		;;
	link)
		if [ ! -L "$p" ]; then
			if [ -e "$p" ]; then
				warn "kept $p: no longer a symlink"
				return 1
			fi
			already "removed: $p"
		elif [ "$(readlink -- "$p")" != "$v" ]; then
			warn "kept $p: it points elsewhere now"
			return 1
		else
			act rm -f -- "$p" || { warn "kept $p: rm failed" && return 1; }
		fi
		;;
	dir)
		# A diag dir this undo keeps (its record stays) is not "a file install did not
		# create": its parent, the state dir, stays with it, and that is no failure.
		local skip=(! -path "$MANIFEST") holds_diag=0
		if [ -n "${MTYPE[$DIAG_DIR]:-}" ] && [ "$p" = "$STATE_DIR" ]; then
			skip+=(! -path "$DIAG_DIR")
			[ ! -e "$DIAG_DIR" ] && [ ! -L "$DIAG_DIR" ] || holds_diag=1
		fi
		if [ ! -e "$p" ] && [ ! -L "$p" ]; then
			already "removed: $p"
		elif [ -L "$p" ] || [ ! -d "$p" ]; then
			warn "kept $p: no longer a directory"
			return 1
		elif [ "$dry_run" -eq 1 ] && [ "$holds_diag" -eq 1 ]; then
			note "would keep $p: it holds $DIAG_DIR, which stays (the manifest keeps both)"
			return 3
		elif [ "$dry_run" -eq 1 ]; then
			# What a real run removed before this point is still there: show the plan.
			[ "$p" != "$LIBDIR" ] || act rm -f -- "$MANIFEST"
			act rmdir -- "$p"
		elif [ -n "$(find "$p" -mindepth 1 -maxdepth 1 "${skip[@]}" -print -quit)" ]; then
			note "left $p in place: it holds files install did not create ($(find "$p" -mindepth 1 -maxdepth 1 "${skip[@]}" -printf '%f ' | head -c 200))"
			return 2
		elif [ "$holds_diag" -eq 1 ]; then
			note "kept $p: it holds $DIAG_DIR, which stays (the manifest keeps both; undo --yes-delete-diag removes them)"
			return 3
		else
			if [ "$p" = "$LIBDIR" ] && [ -e "$MANIFEST" ]; then
				act rm -f -- "$MANIFEST" || { warn "kept $MANIFEST: rm failed" && return 1; }
			fi
			act rmdir -- "$p" || { warn "kept $p: rmdir failed" && return 1; }
		fi
		;;
	diagdir)
		# Called only after the user said yes. Checked again right here; the delete removes
		# the regular files at depth 1 and then the (now empty) directory, so nothing that
		# appeared since the check (a subdir, a symlink) is followed or removed.
		local verdict
		if [ ! -e "$p" ] && [ ! -L "$p" ]; then
			already "removed: $p"
		else
			verdict=$(diag_check "$p")
			if [ "$verdict" != ok ]; then
				warn "kept $p: $verdict; refusing to delete it (it stays in the manifest)"
				return 1
			fi
			act find -P "$p" -mindepth 1 -maxdepth 1 -type f -delete || { warn "kept $p: deleting its files failed" && return 1; }
			act rmdir -- "$p" || { warn "kept $p: rmdir failed" && return 1; }
		fi
		;;
	esac
	return 0
}

# Undo one manifest entry; keep its record unless it is gone. Sets kept/left of cmd_undo.
undo_entry() {
	local rc=0
	remove_recorded "$1" || rc=$?
	case $rc in
	0) manifest_drop "$1" ;;
	2) left=1 ;;
	3) ;;
	*) kept=1 ;;
	esac
}

cmd_undo() {
	need_root
	echo "A5 fprintd dev undo${TR:+ (TEST ROOT $TR)}"
	[ "$dry_run" -eq 0 ] || echo "dry run: nothing is changed"
	check_dir "$LIBDIR"
	manifest_load
	if [ ${#MORDER[@]} -eq 0 ]; then
		already "undone: no manifest at $MANIFEST"
		[ ! -e "$DROPIN" ] || warn "$DROPIN exists but no manifest records it: not removed (remove it yourself if it is ours)"
	fi
	# Once the package is installed, the recorded data files are the ones its driver uses:
	# plain undo would delete them. Refused before any change; the hand-over keeps them.
	if [ "$dry_run" -eq 0 ] && stock_has_driver && manifest_has_data_files; then
		FAIL_RC=1
		fail "libfprint-sigfm-0078 is installed and uses these data files; use undo --keep-data (nothing was changed)" \
			"sudo $(self_cmd) undo --keep-data"
	fi

	# 1. The drop-in first: from here on, a D-Bus activated fprintd loads the packaged lib.
	local reload=0 kept=0 left=0
	if [ "${MTYPE[$DROPIN]:-}" = file ]; then
		undo_entry "$DROPIN"
		reload=1
	fi
	[ "$reload" -eq 0 ] || act "${SYSTEMCTL[@]}" daemon-reload

	# 2. Enrolled prints of this driver: listed, deleted only on an explicit yes.
	local dirs=() d files=() answer=no
	while IFS= read -r d; do [ -z "$d" ] || dirs+=("$d"); done < <(prints_dirs)
	if [ ${#dirs[@]} -gt 0 ]; then
		for d in "${dirs[@]}"; do
			while IFS= read -r -d '' f; do files+=("$f"); done < <(find "$d" -type f -print0)
		done
		echo "enrolled prints of the $DRIVER driver (${#files[@]} file(s)):"
		for d in "${dirs[@]}"; do echo "  $d/"; done
		for f in "${files[@]}"; do echo "    $f"; done
		if [ -n "$prints_answer" ]; then
			answer=$prints_answer
			echo "prints: $answer (from the command line)"
		elif [ "$dry_run" -eq 1 ]; then
			echo "dry run: would ask 'Delete them? [y/N]' (default no)"
		else
			local reply=""
			printf 'Delete them? They are useless with the packaged libfprint. [y/N] '
			read -r reply || {
				reply=""
				echo
			}
			case $reply in [yY] | [yY][eE][sS]) answer=yes ;; *) answer=no ;; esac
		fi
		[ "$answer" = yes ] || echo "prints kept"
	else
		already "none: no enrolled prints of the $DRIVER driver"
	fi

	# 2b. The --diag images: deleted only on an explicit yes, and only if the directory
	#     passes diag_check. Kept on request: its record stays, a later undo can delete it.
	local diag_del=0 diag_keep=0 pending=${#MORDER[@]}
	if [ "${MTYPE[$DIAG_DIR]:-}" = diagdir ]; then
		pending=$((pending - 1))
		if [ ! -e "$DIAG_DIR" ] && [ ! -L "$DIAG_DIR" ]; then
			undo_entry "$DIAG_DIR"
		else
			local verdict n
			verdict=$(diag_check "$DIAG_DIR")
			if [ "$verdict" != ok ]; then
				warn "kept $DIAG_DIR: $verdict; refusing to delete it (it stays in the manifest)"
				kept=1
			else
				n=$(diag_count "$DIAG_DIR")
				echo "diag images (raw fingerprint images, from install --diag): $n file(s) in $DIAG_DIR"
				local question="Delete $n diag images in $DIAG_DIR?"
				if [ -n "$diag_answer" ]; then
					echo "diag: $diag_answer (from the command line)"
					[ "$diag_answer" = no ] || diag_del=1
				elif [ "$dry_run" -eq 1 ]; then
					echo "dry run: would ask '$question [y/N]' (default no)"
				elif [ -t 0 ]; then
					! ask_yes "$question" || diag_del=1
				else
					echo "diag: no terminal to ask, so the images are kept"
				fi
				if [ "$diag_del" -eq 0 ]; then
					diag_keep=1
					echo "diag images kept; delete them later with: $(diag_rm_hint)"
				fi
			fi
		fi
	fi

	# Only the lib dir (its manifest), the state dir holding it and a kept diag dir left:
	# nothing fprintd uses.
	if [ -n "${MTYPE[$DIAG_DIR]:-}" ] && [ "$diag_del" -eq 0 ]; then
		local rest=$pending
		[ -z "${MTYPE[$LIBDIR]:-}" ] || rest=$((rest - 1))
		[ -z "${MTYPE[$STATE_DIR]:-}" ] || rest=$((rest - 1))
		[ "$rest" -ne 0 ] || pending=0
	fi

	# 3. Stop fprintd before deleting anything it may have mapped or be writing.
	local stopped=0
	if [ "$pending" -gt 0 ] || [ "$answer" = yes ] || [ "$diag_del" -eq 1 ]; then
		act "${SYSTEMCTL[@]}" stop "$UNIT"
		stopped=1
	fi
	if [ "$answer" = yes ]; then
		for d in "${dirs[@]}"; do act rm -r --one-file-system -- "$d"; done
	fi
	[ "$diag_del" -eq 0 ] || undo_entry "$DIAG_DIR"

	# 4. Everything else install created: files and links newest first (the drop-in was
	#    step 1), then directories, deepest first. The lib dir and its manifest go last.
	local i p
	for ((i = ${#MORDER[@]} - 1; i >= 0; i--)); do
		p=${MORDER[$i]}
		case ${MTYPE[$p]} in file | link) ;; *) continue ;; esac
		[ "$p" != "$DROPIN" ] || continue
		undo_entry "$p"
	done
	while IFS= read -r p; do
		[ -n "$p" ] && [ "$p" != "$LIBDIR" ] || continue
		undo_entry "$p"
	done < <(for p in "${MORDER[@]}"; do [ "${MTYPE[$p]}" != dir ] || printf '%d\t%s\n' "${#p}" "$p"; done | sort -rn | cut -f2-)
	# The lib dir holds the manifest: it stays while the manifest still lists something.
	if [ -n "${MTYPE[$LIBDIR]:-}" ] && [ "$kept" -eq 0 ] && [ "$left" -eq 0 ] && [ -z "${MTYPE[$DIAG_DIR]:-}" ]; then
		undo_entry "$LIBDIR"
	fi
	if [ ${#MORDER[@]} -gt 0 ]; then
		# Something is left: the manifest lists exactly that, for the next undo.
		note "kept $LIBDIR and its manifest: it lists what is left ($(printf '%s ' "${MORDER[@]}" | head -c 300))"
		manifest_save
	elif [ "$dry_run" -eq 0 ] && [ -e "$MANIFEST" ]; then
		act rm -f -- "$MANIFEST"
	fi
	if [ "$stopped" -eq 1 ]; then
		note "fprintd is stopped; D-Bus starts it on demand with the packaged libfprint (e.g. fprintd-list \"\$USER\")"
	fi
	if [ "$kept" -eq 1 ] || [ "$left" -eq 1 ]; then
		echo "undo incomplete: see the WARN and note lines above; once that is sorted out, run undo again"
		exit 1
	fi
	if [ "$diag_keep" -eq 1 ]; then
		echo "undo done, except the diag images kept on request in $DIAG_DIR (the manifest still lists them)"
	elif [ "$stopped" -eq 1 ]; then
		echo "undo done"
	fi
}

# ---------------------------------------------------------------- undo --keep-data (A6)
# The hand-over to the libfprint-sigfm-0078 package, run after it is installed: the drop-in,
# the lib, its link, the drop-in dir and the lib dir with the manifest go exactly as with
# undo (same records, same checks). The data dir, its files and the state dir stay untouched
# and leave the manifest: the package's synaptics0078-setup owns them from now on. Enrolled
# prints and the diag dir stay too (the diag dir leaves the manifest as well). Never asks.
# The manifest without them is saved first, before the drop-in or fprintd is touched.
# Report one recorded data file that is handed over (kept as it is). INSPECT 0: no access.
handover_data_file() {
	local p=$1 inspect=$2
	if [ "$inspect" -eq 0 ]; then
		note "keep $p (handed over; not checked: no access as $(id -un))"
	elif [ -L "$p" ] || { [ -e "$p" ] && [ ! -f "$p" ]; }; then
		warn "kept $p: no longer a regular file; handed over as it is (look at it)"
	elif [ ! -e "$p" ]; then
		warn "$p is gone (deleted since install); the package's driver needs it"
	elif [ "$(sha "$p")" != "${MVAL[$p]}" ]; then
		warn "kept $p: it changed since install (sha256 differs from the manifest); handed over as it is"
	else
		ok "kept $p (as installed; handed over)"
	fi
}
# systemctl ARGS for the hand-over: a failure is a FAILED line, not a bare set -e abort.
# The data files already left the manifest on disk, so a re-run (or a plain undo) keeps them.
handover_systemctl() {
	local rc=0
	act "${SYSTEMCTL[@]}" "$@" || rc=$?
	[ "$rc" -eq 0 ] ||
		fail "systemctl $* failed (exit $rc); the data files, enrolled prints and diag images are kept and already out of the manifest" \
			"fix that (see the systemctl output above), then run sudo $(self_cmd) undo --keep-data again: it is safe to re-run"
}
cmd_handover() {
	need_root
	echo "A5 fprintd dev undo --keep-data: hand-over to the packaged libfprint${TR:+ (TEST ROOT $TR)}"
	[ "$dry_run" -eq 0 ] || echo "dry run: nothing is changed"
	# Without the drop-in fprintd loads $STOCK_LIB: it must already be the package's.
	FAIL_RC=1
	stock_has_driver ||
		fail "$STOCK_LIB has no $DRIVER driver: the package is not installed yet, and without the drop-in fprintd would run without the driver; nothing was changed" \
			"install the package first (sudo pacman -U libfprint-sigfm-0078-....pkg.tar.zst), then run undo --keep-data again"
	FAIL_RC=2
	ok "$STOCK_LIB has the $DRIVER driver (the package is installed)"
	check_dir "$LIBDIR"
	manifest_load
	if [ ${#MORDER[@]} -eq 0 ]; then
		already "undone: no manifest at $MANIFEST"
		[ ! -e "$DROPIN" ] || warn "$DROPIN exists but no manifest records it: not removed (remove it yourself if it is ours)"
		return 0
	fi
	# A user's dry run cannot look into /var/lib/fprint (root only, 0700).
	local inspect=1
	if [ -e "$STATE_DIR" ] && { [ ! -r "$STATE_DIR" ] || [ ! -x "$STATE_DIR" ]; }; then
		inspect=0
		note "cannot look into $STATE_DIR as $(id -un): the data files are not checked in this dry run"
	fi

	# 1. The data files, the data dir and the state dir: kept as they are, handed over (out
	#    of the manifest). A file that changed since install is a WARN, never deleted.
	local p handed=0
	echo "data files (kept, handed over to the package):"
	for p in "${MORDER[@]}"; do
		[ "${MTYPE[$p]}" = file ] && [ "${p%/*}" = "$DATA_DIR" ] || continue
		handover_data_file "$p" "$inspect"
		manifest_drop "$p"
		handed=1
	done
	for p in "$DATA_DIR" "$STATE_DIR"; do
		[ "${MTYPE[$p]:-}" = dir ] || continue
		if [ "$inspect" -eq 1 ] && { [ -L "$p" ] || [ ! -d "$p" ]; }; then
			warn "$p is no longer a directory; handed over as it is (look at it)"
		else
			ok "kept $p (handed over)"
		fi
		manifest_drop "$p"
		handed=1
	done
	if [ "$handed" -eq 1 ]; then
		note "they are the package's now: synaptics0078-setup status (from the package) checks them"
	else
		already "none: the manifest records no data files"
	fi

	# 2. The --diag images: kept, out of the manifest; the command that deletes them later.
	if [ "${MTYPE[$DIAG_DIR]:-}" = diagdir ]; then
		if [ "$inspect" -eq 0 ]; then
			note "keep $DIAG_DIR (diag images; not counted: no access as $(id -un))"
		elif [ ! -e "$DIAG_DIR" ] && [ ! -L "$DIAG_DIR" ]; then
			already "removed: $DIAG_DIR"
		elif [ -L "$DIAG_DIR" ] || [ ! -d "$DIAG_DIR" ]; then
			warn "kept $DIAG_DIR: no longer a directory; look at it"
		else
			echo "diag images (raw fingerprint images, from install --diag): $(diag_count "$DIAG_DIR") file(s) in $DIAG_DIR, kept"
			printf '  delete them later with: sudo rm -r --one-file-system -- %q\n' "$DIAG_DIR"
		fi
		manifest_drop "$DIAG_DIR"
		handed=1
	fi
	# On disk before anything is touched: from here on, whatever fails, neither a re-run
	# nor a plain undo deletes what was handed over (a dry run saves nothing).
	[ "$handed" -eq 0 ] || manifest_save

	# 3. Enrolled prints of this driver: kept.
	local dirs=() d n=0
	while IFS= read -r d; do [ -z "$d" ] || dirs+=("$d"); done < <(prints_dirs)
	if [ ${#dirs[@]} -gt 0 ]; then
		for d in "${dirs[@]}"; do n=$((n + $(find "$d" -type f -printf . | wc -c))); done
		echo "enrolled prints of the $DRIVER driver: kept ($n file(s), --keep-data)"
		for d in "${dirs[@]}"; do echo "  $d/"; done
	elif [ -e "$STATE_DIR" ] && { [ ! -r "$STATE_DIR" ] || [ ! -x "$STATE_DIR" ]; }; then
		note "enrolled prints of the $DRIVER driver: not checked (needs root); kept in any case (--keep-data)"
	else
		already "none: no enrolled prints of the $DRIVER driver"
	fi

	# 4. The drop-in: from here on, a D-Bus activated fprintd loads the packaged lib.
	local reload=0 kept=0 left=0
	if [ "${MTYPE[$DROPIN]:-}" = file ]; then
		undo_entry "$DROPIN"
		reload=1
	fi
	[ "$reload" -eq 0 ] || handover_systemctl daemon-reload

	# 5. Stop fprintd before deleting the lib it may have mapped. Only files and links count:
	#    directories left in place (holding files of others) are nothing fprintd uses.
	local stopped=0 pending=0
	for p in "${MORDER[@]}"; do
		case ${MTYPE[$p]} in file | link) pending=$((pending + 1)) ;; esac
	done
	if [ "$pending" -gt 0 ] || [ "$reload" -eq 1 ]; then
		handover_systemctl stop "$UNIT"
		stopped=1
	fi

	# 6. What is left is undo's: files and links newest first, then directories deepest
	#    first; the lib dir and its manifest last.
	local i
	for ((i = ${#MORDER[@]} - 1; i >= 0; i--)); do
		p=${MORDER[$i]}
		case ${MTYPE[$p]} in file | link) ;; *) continue ;; esac
		[ "$p" != "$DROPIN" ] || continue
		undo_entry "$p"
	done
	while IFS= read -r p; do
		[ -n "$p" ] && [ "$p" != "$LIBDIR" ] || continue
		undo_entry "$p"
	done < <(for p in "${MORDER[@]}"; do [ "${MTYPE[$p]}" != dir ] || printf '%d\t%s\n' "${#p}" "$p"; done | sort -rn | cut -f2-)
	if [ -n "${MTYPE[$LIBDIR]:-}" ] && [ "$kept" -eq 0 ] && [ "$left" -eq 0 ]; then
		undo_entry "$LIBDIR"
	fi
	if [ ${#MORDER[@]} -gt 0 ]; then
		note "kept $LIBDIR and its manifest: it lists what is left ($(printf '%s ' "${MORDER[@]}" | head -c 300))"
		manifest_save
	elif [ "$dry_run" -eq 0 ] && [ -e "$MANIFEST" ]; then
		act rm -f -- "$MANIFEST"
	fi
	if [ "$stopped" -eq 1 ]; then
		note "fprintd is stopped; D-Bus starts it on demand with the packaged libfprint (e.g. fprintd-list \"\$USER\")"
	fi
	if [ "$kept" -eq 1 ] || [ "$left" -eq 1 ]; then
		echo "hand-over incomplete: see the WARN and note lines above; once that is sorted out, run undo --keep-data again"
		exit 1
	fi
	echo "hand-over done: the dev install is gone; the data files, enrolled prints and diag images stay"
}

# ---------------------------------------------------------------- status
cmd_status() {
	local user=${SUDO_USER:-${USER:-$(id -un)}}
	echo "A5 fprintd dev status (as $(id -un))${TR:+ (TEST ROOT $TR)}"
	echo "drop-in: $DROPIN"
	if [ -f "$DROPIN" ] && [ -r "$DROPIN" ]; then
		sed 's/^/    | /' -- "$DROPIN"
	elif [ -e "$DROPIN" ]; then
		echo "  present but not readable as $(id -un)"
	else
		echo "  absent: fprintd uses the packaged libfprint"
	fi

	echo "fprintd ($UNIT):"
	systemd_show | sed 's/^/  /'

	echo "fprintd-list $user:"
	local out rc=0
	out=$("${FPRINTD_LIST[@]}" "$user" 2>&1) || rc=$?
	printf '%s\n' "$out" | sed 's/^/    /'
	[ "$rc" -eq 0 ] || echo "  fprintd-list exited $rc"

	echo "libfprint mapped by fprintd:"
	local pid
	pid=$(main_pid)
	if ! [[ $pid =~ ^[0-9]+$ ]] || [ "$pid" -eq 0 ]; then
		echo "  fprintd is not running; D-Bus starts it on demand: run 'fprintd-list $user', then status again"
	elif ! mapped_libfprint "$pid" >/dev/null 2>&1; then
		echo "  cannot read $PROC/$pid/maps as $(id -un): run status with sudo for this part"
	else
		local maps
		maps=$(mapped_libfprint "$pid")
		if [ -z "$maps" ]; then
			echo "  pid $pid maps no libfprint-2.so"
		fi
		while IFS= read -r p; do
			[ -n "$p" ] || continue
			case $p in
			*" (deleted)") echo "  $p  <- replaced since fprintd started: restart it (sudo systemctl restart fprintd)" ;;
			"$SYS_LIBDIR"/*) echo "  $p  (the fork, from this dev install)" ;;
			*) echo "  $p  (not the dev install)" ;;
			esac
		done <<<"$maps"
	fi

	echo "installed lib: $LIB"
	if [ -f "$LIB" ]; then
		local lsha fsha="" src=$FORK/build/libfprint/$LIBFILE
		lsha=$(sha "$LIB")
		[ ! -r "$src" ] || fsha=$(sha "$src")
		echo "  sha256 $lsha"
		if [ -z "$fsha" ]; then
			echo "  fork build $src not readable: cannot compare"
		elif [ "$fsha" = "$lsha" ]; then
			echo "  same as the fork build $src"
		else
			echo "  differs from the fork build $src (rebuilt since install?): run install again"
		fi
		if [ -L "$LINK" ]; then echo "  $SONAME -> $(readlink -- "$LINK")"; else echo "  WARN $LINK missing"; fi
		if [ -f "$MANIFEST" ]; then echo "  manifest: $MANIFEST"; else echo "  WARN no manifest"; fi
	else
		echo "  absent"
	fi

	echo "diag (raw image dumps, install --diag): $DIAG_DIR"
	if [ -r "$DROPIN" ] && grep -qxF "Environment=$DIAG_ENV=$SYS_DIAG_DIR" -- "$DROPIN"; then
		echo "  on: the drop-in sets $DIAG_ENV (fprintd started since then writes every image)"
	elif [ -e "$DROPIN" ] && [ ! -r "$DROPIN" ]; then
		echo "  unknown: cannot read $DROPIN as $(id -un)"
	else
		echo "  off"
	fi
	if [ ! -x "$STATE_DIR" ] && [ -e "$STATE_DIR" ]; then
		echo "  files: needs sudo to count ($STATE_DIR is root only)"
	elif [ -L "$DIAG_DIR" ]; then
		echo "  WARN $DIAG_DIR is a symlink"
	elif [ ! -d "$DIAG_DIR" ]; then
		echo "  no directory"
	elif [ ! -r "$DIAG_DIR" ] || [ ! -x "$DIAG_DIR" ]; then
		echo "  files: needs sudo to count"
	else
		echo "  files: $(diag_count "$DIAG_DIR") in $DIAG_DIR (mode $(stat -c '%a %U:%G' -- "$DIAG_DIR")); undo deletes them (asks first)"
	fi

	echo "data files: $DATA_DIR"
	if [ ! -x "$STATE_DIR" ] && [ -e "$STATE_DIR" ]; then
		echo "  cannot look into $STATE_DIR as $(id -un) (mode 0700, root only): run status with sudo for this part"
	elif [ ! -d "$DATA_DIR" ]; then
		echo "  absent"
	else
		echo "  dir mode $(stat -c '%a %U:%G' -- "$DATA_DIR")"
		local f found=0
		shopt -s nullglob
		for f in "$DATA_DIR"/module.bin "$DATA_DIR"/program-*.bin; do
			found=1
			local line
			line="$(basename -- "$f"): $(stat -c '%s B, mode %a %U:%G' -- "$f")"
			if [ "$(basename -- "$f")" = module.bin ]; then
				if [ -r "$f" ] && [ "$(sha "$f")" = "$MODULE_SHA256" ]; then
					line+=", sha256 ok"
				elif [ -r "$f" ]; then
					line+=", sha256 MISMATCH"
				fi
			fi
			echo "  $line"
		done
		shopt -u nullglob
		[ "$found" -eq 1 ] || echo "  no module.bin or program-*.bin"
	fi
}

# ---------------------------------------------------------------- log (packaged install)
# The debug drop-in for the packaged fprintd (see the header). log on refuses while the dev
# drop-in is present: it carries the same G_MESSAGES_DEBUG, and the A5 helpers check that one in
# the dev install. log off removes only the file log on wrote, which is safe either way.
debug_dropin_ours() { [ "$(head -n 1 -- "$DEBUG_DROPIN")" = "$DEBUG_MARK" ]; }
dev_dropin_present() { [ -e "$DROPIN" ] || [ -L "$DROPIN" ]; }
# A property of the unit as systemd has it loaded ("" if systemctl fails).
unit_prop() { "${SYSTEMCTL[@]}" show "$UNIT" -p "$1" --value 2>/dev/null || true; }
need_reload() { [ "$(unit_prop NeedDaemonReload)" = yes ]; }
# The loaded unit (since the last daemon-reload) sets G_MESSAGES_DEBUG for the driver.
unit_env_has_debug() { [[ $(unit_prop Environment) == *G_MESSAGES_DEBUG=*libfprint-synaptics0078* ]]; }
# fprintd runs and started before PATH was last modified, to the microsecond (the A5 helpers'
# preflight makes the same comparison). Not running, or no start time: no.
fprintd_predates() {
	local start start_us mtime
	[ "$(unit_prop ActiveState)" = active ] || return 1
	start=$("${SYSTEMCTL[@]}" show "$UNIT" --timestamp=us+utc -p ExecMainStartTimestamp --value 2>/dev/null) || return 1
	[ -n "$start" ] || return 1
	start_us=$(date -u -d "$start" +%s%6N 2>/dev/null) || return 1
	[[ $start_us =~ ^[0-9]+$ ]] || return 1
	mtime=$(LC_ALL=C stat -c %.6Y -- "$1") || return 1
	[ "$start_us" -lt "${mtime/./}" ]
}
# fprintd's own environment (/proc/PID/environ, readable as root): 0 it has the driver's
# G_MESSAGES_DEBUG, 1 it lacks it, 2 unknown (not running, or not readable).
fprintd_env_has_debug() {
	local pid
	pid=$(main_pid)
	[[ $pid =~ ^[1-9][0-9]*$ ]] && [ -r "$PROC/$pid/environ" ] || return 2
	if tr '\0' '\n' <"$PROC/$pid/environ" | grep -E '^G_MESSAGES_DEBUG=.*libfprint-synaptics0078' >/dev/null; then
		return 0
	fi
	return 1
}
log_refuse_dev() {
	dev_dropin_present || return 0
	fail "the dev drop-in $DROPIN is present: it already sends the driver's lines to the journal; log on is for the packaged install only (nothing was changed)" \
		"for the dev install nothing more is needed; after the switch to the package (sudo $(self_cmd) undo --keep-data) run: sudo $(self_cmd) log on"
}
# The checks log on/off share: the dir and the file safe to use, the file ours if present.
log_preflight() {
	[ -d "$TR/etc/systemd/system" ] || fail "$TR/etc/systemd/system does not exist"
	check_dir "$DROPIN_DIR"
	check_file "$DEBUG_DROPIN"
	if [ -e "$DEBUG_DROPIN" ] && ! debug_dropin_ours; then
		fail "$DEBUG_DROPIN exists and was not written by this script; refusing to change it (nothing was changed)" \
			"look at it; remove it (sudo rm -- $DEBUG_DROPIN), then run: sudo $(self_cmd) log $log_arg"
	fi
}
# systemctl ARGS for log on/off: a failure is a FAILED line (LOG_STATE says what is left) and the
# re-run that finishes the job, never a bare set -e abort.
LOG_STATE=""
log_systemctl() {
	local rc=0
	act "${SYSTEMCTL[@]}" "$@" || rc=$?
	[ "$rc" -eq 0 ] ||
		fail "systemctl $* failed (exit $rc): $LOG_STATE" \
			"fix that (see the systemctl output above), then run sudo $(self_cmd) log $log_arg again: it reloads and restarts what is left"
}
log_restart() {
	log_systemctl daemon-reload
	# Restarts a running fprintd only; otherwise D-Bus starts it on demand with the new unit.
	log_systemctl try-restart "$UNIT"
}

cmd_log_on() {
	need_root
	echo "A5 fprintd debug log on${TR:+ (TEST ROOT $TR)}"
	[ "$dry_run" -eq 0 ] || echo "dry run: nothing is changed"
	log_refuse_dev
	log_preflight
	stock_has_driver ||
		warn "$STOCK_LIB has no $DRIVER driver: the libfprint-sigfm-0078 package is not installed; the lines appear once it is"
	LOG_STATE="$DEBUG_DROPIN is written, but fprintd may still run without it"
	if [ -f "$DEBUG_DROPIN" ] && [ "$(cat -- "$DEBUG_DROPIN")" = "${DEBUG_TEXT%$'\n'}" ] &&
		[ "$(stat -c '%a' -- "$DEBUG_DROPIN")" = 644 ]; then
		already "written: $DEBUG_DROPIN"
		local penv=0
		fprintd_env_has_debug || penv=$?
		if need_reload || ! unit_env_has_debug; then
			# An earlier run stopped before its daemon-reload (or systemd was not told).
			note "systemd has not loaded $DEBUG_DROPIN: reloading it and restarting fprintd"
			log_restart
			echo "log on: systemd reloaded; fprintd restarted (if it was running)"
		elif fprintd_predates "$DEBUG_DROPIN" || [ "$penv" -eq 1 ]; then
			note "fprintd runs without the debug lines (it started before $DEBUG_DROPIN was written): restarting it"
			log_systemctl try-restart "$UNIT"
			echo "log on: fprintd restarted"
		else
			already "on: nothing changed, fprintd not restarted"
		fi
	else
		if [ -d "$DROPIN_DIR" ]; then
			already "present: $DROPIN_DIR"
		else
			act install -d -m 0755 "${OWN[@]}" -- "$DROPIN_DIR"
		fi
		echo "+ write $DEBUG_DROPIN (mode 0644, through a 0600 stage in $DROPIN_DIR):"
		printf '%s' "$DEBUG_TEXT" | sed 's/^/    | /'
		if [ "$dry_run" -eq 0 ]; then
			STAGE=$(mktemp -- "$DROPIN_DIR/.synaptics0078-debug.conf.a5-new.XXXXXX")
			printf '%s' "$DEBUG_TEXT" >"$STAGE"
			[ -n "$TR" ] || chown root:root -- "$STAGE"
			chmod 0644 -- "$STAGE"
			act mv -f -T -- "$STAGE" "$DEBUG_DROPIN"
			STAGE=""
		fi
		log_restart
		note "fprintd is D-Bus activated: if it was not running, the next client (fprintd-list, a lock screen) starts it with the debug lines"
		if [ "$dry_run" -eq 1 ]; then
			echo "log on (dry run): would write $DEBUG_DROPIN and restart fprintd (if it was running)"
		else
			echo "log on: $DEBUG_DROPIN written; fprintd restarted (if it was running)"
		fi
	fi
	if [ "$dry_run" -eq 0 ] && ! unit_env_has_debug; then
		warn "fprintd.service still has no G_MESSAGES_DEBUG for $DRIVER after the reload: another drop-in may override it; look: systemctl cat $UNIT"
	fi
	echo "next: the A5 helpers (tools/dev/a5-verify.sh ...); when testing is done (the lines include the unit serial): sudo $(self_cmd) log off"
}

cmd_log_off() {
	need_root
	echo "A5 fprintd debug log off${TR:+ (TEST ROOT $TR)}"
	[ "$dry_run" -eq 0 ] || echo "dry run: nothing is changed"
	log_preflight
	LOG_STATE="$DEBUG_DROPIN is removed, but fprintd may still write the debug lines (with the unit serial) to the journal"
	local dev=0
	! dev_dropin_present || dev=1
	if [ ! -e "$DEBUG_DROPIN" ]; then
		already "removed: $DEBUG_DROPIN"
		if need_reload || { [ "$dev" -eq 0 ] && unit_env_has_debug; }; then
			# An earlier run stopped before its daemon-reload.
			note "systemd still has the removed drop-in loaded: reloading it and restarting fprintd"
			log_restart
			echo "log off: systemd reloaded; fprintd restarted (if it was running)"
		elif [ "$dev" -eq 0 ] && fprintd_env_has_debug && fprintd_predates "$DROPIN_DIR"; then
			# Started before the removal (it changed the directory) and still with the lines.
			note "fprintd still runs with the debug lines (it started before the drop-in was removed): restarting it"
			log_systemctl try-restart "$UNIT"
			echo "log off: fprintd restarted"
		else
			already "off: nothing changed, fprintd not restarted"
		fi
	else
		act rm -f -- "$DEBUG_DROPIN"
		log_restart
		if [ "$dry_run" -eq 1 ]; then
			echo "log off (dry run): would remove $DEBUG_DROPIN and restart fprintd (if it was running)"
		else
			echo "log off: $DEBUG_DROPIN removed; fprintd restarted (if it was running)"
		fi
		note "the journal keeps the lines already written (with the unit serial); to drop them: sudo journalctl --rotate --vacuum-time=1s (it deletes every archived journal of every service)"
	fi
	[ "$dev" -eq 0 ] ||
		note "the dev drop-in $DROPIN still sends the debug lines; it goes with: sudo $(self_cmd) undo --keep-data (after installing the package)"
}

cmd_log_status() {
	echo "A5 fprintd debug log status (as $(id -un))${TR:+ (TEST ROOT $TR)}"
	if stock_has_driver; then
		echo "packaged lib: $STOCK_LIB has the $DRIVER driver"
	else
		echo "packaged lib: $STOCK_LIB has no $DRIVER driver (libfprint-sigfm-0078 not installed)"
	fi
	if dev_dropin_present; then
		echo "dev drop-in: $DROPIN is present: it carries the debug lines; log on is for the packaged install only"
	fi
	echo "debug drop-in: $DEBUG_DROPIN"
	if [ -L "$DEBUG_DROPIN" ] || { [ -e "$DEBUG_DROPIN" ] && [ ! -f "$DEBUG_DROPIN" ]; }; then
		echo "  WARN not a regular file: look at it"
	elif [ ! -e "$DEBUG_DROPIN" ]; then
		echo "  off: absent"
		dev_dropin_present || echo "  turn it on: sudo $(self_cmd) log on"
		if need_reload; then
			echo "  WARN systemd has not reloaded since a change: sudo $(self_cmd) log off"
		fi
	elif [ ! -r "$DEBUG_DROPIN" ]; then
		echo "  present but not readable as $(id -un)"
	elif ! debug_dropin_ours; then
		echo "  WARN present, but not written by this script (log on/off leave it alone)"
		sed 's/^/    | /' -- "$DEBUG_DROPIN"
	else
		echo "  on (the journal lines include the unit serial; turn it off when testing is done: sudo $(self_cmd) log off)"
		if need_reload || ! unit_env_has_debug; then
			echo "  WARN systemd has not loaded it: fprintd runs without the lines; fix: sudo $(self_cmd) log on"
		elif fprintd_predates "$DEBUG_DROPIN"; then
			echo "  WARN fprintd started before the drop-in was written: it runs without the lines; restart it: sudo $(self_cmd) log on"
		fi
	fi
}

# ---------------------------------------------------------------- check-lib
# install's lib checks on any FILE, e.g. the package's build: read-only, no root. A failed
# check exits 1 (FAILED line on stderr); all passed prints the ok lines and exits 0.
# Dynamic symbols as check_lib compares them: one name per line, sorted, unique.
dyn_symbols() { nm -D --defined-only -- "$1" | awk '{print $3}' | sort -u; }
count_lines() { awk 'NF { n++ } END { print n + 0 }'; }
cmd_check_lib() {
	local f=$lib_arg tool n_stock n_file n_new
	FAIL_RC=1
	echo "A5 check-lib: $f${TR:+ (TEST ROOT $TR)}"
	for tool in readelf nm strings; do
		command -v "$tool" >/dev/null || fail "$tool not found" "install binutils"
	done
	[ -e "$f" ] || fail "no file $f"
	[ -f "$f" ] || fail "$f is not a regular file"
	# Without the packaged lib there is nothing to compare the symbols with: no verdict.
	[ -f "$STOCK_LIB" ] || fail "no $STOCK_LIB to compare exported symbols with"
	n_stock=$(dyn_symbols "$STOCK_LIB" | count_lines) || n_stock=0
	[ "$n_stock" -gt 0 ] || fail "no dynamic symbols read from $STOCK_LIB; cannot compare"
	BUILD_HINT="check-lib runs the checks install runs on the fork build; fix the build of $f"
	check_lib "$f"
	n_file=$(dyn_symbols "$f" | count_lines) || fail "cannot read the dynamic symbols of $f"
	n_new=$(comm -13 <(dyn_symbols "$STOCK_LIB") <(dyn_symbols "$f") | count_lines)
	ok "SONAME $SONAME, no RPATH/RUNPATH"
	ok "data dir $SYS_DATA_DIR compiled in"
	ok "driver $DRIVER built in"
	ok "symbols: $n_stock in $STOCK_LIB, $n_file in $f, 0 missing, $n_new new in $f"
	echo "check-lib: all checks passed"
}

case $cmd in
install) cmd_install ;;
undo) if [ "$keep_data" -eq 1 ]; then cmd_handover; else cmd_undo; fi ;;
status) cmd_status ;;
check-lib) cmd_check_lib ;;
log) case $log_arg in on) cmd_log_on ;; off) cmd_log_off ;; status) cmd_log_status ;; esac ;;
esac
