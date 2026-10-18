#!/usr/bin/env bash
# Fingerprint login at the SDDM login screen (Synaptics 06cb:0078 with libfprint-sigfm-0078).
#
#   sudo tools/sddm-fingerprint.sh on       turn it on (edits /etc/pam.d/sddm)
#   sudo tools/sddm-fingerprint.sh off      turn it off again (removes exactly those lines)
#   tools/sddm-fingerprint.sh status        on or off (no root needed, changes nothing)
#   -h, --help                              this text
#
# With it on, the login screen takes your password as before, or an empty password: press
# Enter with the field empty and touch the reader within 10 seconds.
#
# on adds four lines (a begin marker, two auth lines, an end marker) right before the first
# "auth include system-login" line of /etc/pam.d/sddm. Before that it checks that the file
# is a regular file holding that line, that pam_fprintd.so and pam_unix.so are installed and
# that the user who ran sudo has a fingerprint enrolled (enroll one first, as yourself:
# fprintd-enroll). The first on saves the file as it was to /etc/pam.d/sddm.before-fingerprint;
# that backup is never overwritten or removed. The file is replaced in one step (a temp copy,
# flushed to disk, then a rename), so it is never half written. Two on/off runs at the same time
# take turns.
#
# off removes exactly those four lines, which gives back the file as it was before on.
# on and off are safe to run twice. If the lines between the markers were changed by hand,
# on and off refuse and change nothing: fix the file by hand, the backup shows the original.
#
# If the login screen ever refuses you: press Ctrl+Alt+F3, log in on the text console there,
# and run the "Undo:" line that on printed. It names this script by its full path, as the console
# starts in your home directory: sudo /full/path/to/tools/sddm-fingerprint.sh off
#
# Exit status: 0 done (or nothing to do; status: on or off), 1 status: changed by hand or no
# PAM file, 2 refused or failed (nothing was changed unless the message says otherwise).
set -euo pipefail
umask 022

# TEST ONLY (not in --help; never set them on a real system; as root the script refuses to
# run while any of them is set, e.g. passed by sudo -E). The others are honoured only while
# SDDM_FP_TEST_PAM_FILE is set:
#   SDDM_FP_TEST_PAM_FILE    the PAM file instead of /etc/pam.d/sddm (absolute); the backup is
#                            this path plus .before-fingerprint; no chown; on/off pass the root
#                            check unless SDDM_FP_FAKE_EUID says otherwise
#   SDDM_FP_TEST_FPRINT_DIR  fprintd's state dir instead of /var/lib/fprint
#   SDDM_FP_TEST_MODULE_DIR  the PAM module dir instead of /usr/lib/security
#   SDDM_FP_FAKE_EUID        (not root only) the euid the root checks see, to test them

# The test hooks redirect what is written: never as root.
TEST_VARS=(SDDM_FP_TEST_PAM_FILE SDDM_FP_TEST_FPRINT_DIR SDDM_FP_TEST_MODULE_DIR SDDM_FP_FAKE_EUID)
euid=$(id -u)
[ "$euid" -eq 0 ] || euid=${SDDM_FP_FAKE_EUID:-$euid}
if [ "$euid" = 0 ]; then
	for v in "${TEST_VARS[@]}"; do
		if [ -n "${!v+set}" ]; then
			printf 'FAILED: %s is set; the test-only variables are refused as root\n' "$v" >&2
			printf '  hint: unset it (sudo -E and env_keep pass it on): sudo env -u %s ...\n' "$v" >&2
			exit 2
		fi
	done
fi

# CDPATH= : an exported CDPATH must not send cd elsewhere (or make it print the dir).
SELF=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")

PAM=/etc/pam.d/sddm
FPRINT_DIR=/var/lib/fprint
MODULE_DIR=/usr/lib/security
TEST=0
if [ -n "${SDDM_FP_TEST_PAM_FILE:-}" ]; then
	case $SDDM_FP_TEST_PAM_FILE in /*) ;; *) echo "SDDM_FP_TEST_PAM_FILE must be an absolute path" >&2 && exit 2 ;; esac
	TEST=1
	PAM=$SDDM_FP_TEST_PAM_FILE
	FPRINT_DIR=${SDDM_FP_TEST_FPRINT_DIR:-$FPRINT_DIR}
	MODULE_DIR=${SDDM_FP_TEST_MODULE_DIR:-$MODULE_DIR}
	# As a5-fprintd-dev.sh under its test root: no root needed, unless a fake euid is given.
	[ -n "${SDDM_FP_FAKE_EUID:-}" ] || euid=0
fi
BACKUP=$PAM.before-fingerprint
PAM_DIR=${PAM%/*}
PAM_DIR=${PAM_DIR:-/}

# The block on writes, line by line. The markers are matched as whole lines.
BEGIN_LINE="# synaptics0078: fingerprint login (tools/sddm-fingerprint.sh) begin"
UNIX_LINE="auth        [success=1 new_authtok_reqd=1 default=ignore]  pam_unix.so  try_first_pass likeauth nullok"
FPRINTD_LINE="auth        sufficient  pam_fprintd.so  timeout=10"
END_LINE="# synaptics0078: fingerprint login end"
# The block goes right before the first line matching this (system-login as a whole word:
# not system-loginX). Every match and check of the anchor goes through anchor_line.
ANCHOR_RE='^auth[[:space:]]+include[[:space:]]+system-login([[:space:]]|$)'

usage() { sed -n '2,/^set /s/^# \{0,1\}//p' "$SELF"; }
# This script as a command the user can run from anywhere: its full path, shell-quoted. Not
# the path as invoked: the undo line is for the recovery console, which starts in $HOME.
self_cmd() { printf '%q' "$SELF"; }
fail() {
	printf 'FAILED: %s\n' "$1" >&2
	[ $# -lt 2 ] || printf '  hint: %s\n' "$2" >&2
	exit 2
}

cmd=""
while [ $# -gt 0 ]; do
	case $1 in
	on | off | status)
		[ -z "$cmd" ] || fail "more than one subcommand: $cmd, $1" "see $(self_cmd) --help"
		cmd=$1
		;;
	-h | --help)
		usage
		exit 0
		;;
	*) fail "unknown argument: $1" "see $(self_cmd) --help" ;;
	esac
	shift
done
[ -n "$cmd" ] || fail "no subcommand" "use on, off or status; see $(self_cmd) --help"

# ---------------------------------------------------------------- reading the PAM file
# Line numbers (one per output line) of the lines of FILE that are exactly TEXT.
lines_equal() { grep -n -x -F -e "$2" -- "$1" | cut -d: -f1 || true; }
# Line number of the first anchor line of FILE, or nothing.
anchor_line() { grep -n -m1 -E -e "$ANCHOR_RE" -- "$1" | cut -d: -f1 || true; }
# Line N of FILE (without its newline).
line_at() { sed -n "${2}p" -- "$1"; }
count() { [ -z "$1" ] && echo 0 || printf '%s\n' "$1" | wc -l; }

# The state of FILE: sets STATE to on, off or changed, and WHY (for changed) and BLOCK_AT
# (for on: the line number of the begin marker). on means one begin marker, one end marker
# three lines below it and exactly our two lines between them.
STATE=""
WHY=""
BLOCK_AT=""
read_state() {
	local f=$1 begins ends nb ne
	begins=$(lines_equal "$f" "$BEGIN_LINE")
	ends=$(lines_equal "$f" "$END_LINE")
	nb=$(count "$begins")
	ne=$(count "$ends")
	BLOCK_AT=""
	WHY=""
	if [ "$nb" -eq 0 ] && [ "$ne" -eq 0 ]; then
		STATE=off
	elif [ "$nb" -gt 1 ] || [ "$ne" -gt 1 ]; then
		STATE=changed
		WHY="the fingerprint lines are in it more than once"
	elif [ "$nb" -eq 0 ]; then
		STATE=changed
		WHY="the end marker is there without the begin marker"
	elif [ "$ne" -eq 0 ]; then
		STATE=changed
		WHY="the begin marker is there without the end marker"
	elif [ "$ends" -ne $((begins + 3)) ] ||
		[ "$(line_at "$f" $((begins + 1)))" != "$UNIX_LINE" ] ||
		[ "$(line_at "$f" $((begins + 2)))" != "$FPRINTD_LINE" ]; then
		STATE=changed
		WHY="the lines between the markers differ from what on writes"
	else
		STATE=on
		BLOCK_AT=$begins
	fi
}
# A line using pam_fprintd.so that is not a comment (outside our block when the state is off).
# Any case for the type: libpam reads "AUTH" as auth.
uses_fprintd() { grep -q -E -e '^[[:space:]]*-?[A-Za-z]+[[:space:]].*pam_fprintd\.so' -- "$1"; }

changed_hint() {
	local h="look at the lines between \"$BEGIN_LINE\" and \"$END_LINE\" in $PAM and fix them by hand"
	if [ -e "$BACKUP" ]; then
		h+="; the file as it was before on is $BACKUP"
	fi
	printf '%s' "$h"
}

# ---------------------------------------------------------------- writing
# TMP: the new PAM file while it is written; BTMP: the backup's temp copy. Both are made by
# mktemp in the PAM file's directory (rename and link stay on one filesystem) as
# .<pam>.fingerprint-{new,backup}.XXXXXX, and only while this run holds the lock (LOCKED=1).
TMP=""
BTMP=""
LOCKED=0
# On exit, remove TMP and BTMP. A signal that lands while mktemp runs leaves a file whose name
# never reached TMP or BTMP, so a run that holds the lock also removes every regular file named
# like a temp file of ours: under the lock no other run makes one, so they are this run's or
# leftovers of a run that was killed. Without the lock it removes no such file (they can be
# another run's). Symlinks and other non-regular files are left alone. Always succeeds: under
# set -e a failing command in the trap would replace the exit status.
cleanup() {
	local f
	[ -z "$TMP" ] || rm -f -- "$TMP" || true
	[ -z "$BTMP" ] || rm -f -- "$BTMP" || true
	if [ "$LOCKED" -eq 1 ]; then
		# Without nullglob a pattern that matches nothing stays as it is and fails the -f test.
		for f in "$PAM_DIR/.${PAM##*/}.fingerprint-new."?????? "$PAM_DIR/.${PAM##*/}.fingerprint-backup."??????; do
			if [ -f "$f" ] && [ ! -L "$f" ]; then
				rm -f -- "$f" || true
			fi
		done
	fi
	return 0
}
trap cleanup EXIT
# A signal ends the run through the EXIT trap, which removes the temp files.
trap 'exit 2' INT TERM HUP

# on and off take turns: each holds an exclusive flock(2) on the PAM file's directory (fd 9)
# from before it reads the file until it exits. Locking the directory creates no lock file
# (nothing to own or clean up in /etc/pam.d), and the lock is not lost when the rename
# replaces the file. status takes no lock.
lock_pam_dir() {
	[ -d "$PAM_DIR" ] || fail "$PAM_DIR is not a directory; nothing was changed"
	{ exec 9<"$PAM_DIR"; } 2>/dev/null || fail "cannot open $PAM_DIR to lock it; nothing was changed"
	if ! flock -n 9; then
		echo "waiting for another run of this tool to finish (up to 60 s) ..." >&2
		flock -w 60 9 || fail "another run of this tool still holds $PAM_DIR after 60 s; nothing was changed" \
			"wait for it to finish, then run this again"
	fi
	LOCKED=1
}

# Mode 0644, root:root (the test user's under the test hooks).
own_and_mode() {
	[ "$TEST" -eq 1 ] || chown root:root -- "$1"
	chmod 0644 -- "$1"
}
# Replace the PAM file with what the function named in $1 writes to stdout, in one rename.
write_pam() {
	TMP=$(mktemp -- "$PAM_DIR/.${PAM##*/}.fingerprint-new.XXXXXX")
	"$1" >"$TMP"
	own_and_mode "$TMP"
	sync -- "$TMP"
	mv -f -T -- "$TMP" "$PAM"
	TMP=""
}
# The backup, only if there is none yet: a temp copy linked to the backup name (ln fails
# if that name exists, so a backup is never overwritten, not even by a racing run).
# Prints what it did.
make_backup() {
	if [ -e "$BACKUP" ] || [ -L "$BACKUP" ]; then
		echo "  kept the existing backup $BACKUP"
		return 0
	fi
	BTMP=$(mktemp -- "$PAM_DIR/.${PAM##*/}.fingerprint-backup.XXXXXX")
	cp -- "$PAM" "$BTMP"
	own_and_mode "$BTMP"
	sync -- "$BTMP"
	if ln -- "$BTMP" "$BACKUP" 2>/dev/null; then
		echo "  saved the file as it was to $BACKUP"
	elif [ -e "$BACKUP" ] || [ -L "$BACKUP" ]; then
		echo "  kept the existing backup $BACKUP"
	else
		fail "could not save the backup $BACKUP; nothing was changed"
	fi
	rm -f -- "$BTMP"
	BTMP=""
}

ANCHOR_AT=""
with_block() {
	head -n $((ANCHOR_AT - 1)) -- "$PAM"
	printf '%s\n' "$BEGIN_LINE" "$UNIX_LINE" "$FPRINTD_LINE" "$END_LINE"
	tail -n +"$ANCHOR_AT" -- "$PAM"
}
without_block() {
	head -n $((BLOCK_AT - 1)) -- "$PAM"
	tail -n +$((BLOCK_AT + 4)) -- "$PAM"
}

# ---------------------------------------------------------------- checks
need_root() {
	[ "$euid" = 0 ] || fail "$cmd changes $PAM and needs root; nothing was changed" \
		"run it with sudo: sudo $(self_cmd) $cmd"
}
check_pam_file() {
	[ ! -L "$PAM" ] || fail "$PAM is a symlink; refusing to replace it (nothing was changed)" \
		"look at it: ls -l $PAM"
	[ -e "$PAM" ] || fail "$PAM does not exist; nothing was changed" "is SDDM installed? (pacman -Qo $PAM)"
	[ -f "$PAM" ] || fail "$PAM is not a regular file; nothing was changed"
	[ -r "$PAM" ] || fail "cannot read $PAM; nothing was changed"
}
# The user who ran sudo has at least one enrolled print. Only lists fprintd's state dir for a
# regular file (the reader is not opened), and prints no path below it: the names there carry
# the device id.
check_enrolled() {
	local user=${SUDO_USER:-} found
	[ -n "$user" ] || fail "cannot tell whose fingerprint to check: SUDO_USER is not set; nothing was changed" \
		"run it with sudo from your own account (not from a root shell): sudo $(self_cmd) on"
	[[ $user =~ ^[A-Za-z_][A-Za-z0-9._-]{0,31}$ ]] ||
		fail "SUDO_USER is not a plain user name; nothing was changed" \
			"run it with sudo from your own account: sudo $(self_cmd) on"
	found=$(find "$FPRINT_DIR/$user/" -type f -print -quit 2>/dev/null || true)
	[ -n "$found" ] || fail "$user has no fingerprint enrolled; nothing was changed" \
		"enroll one first, as yourself (no sudo): fprintd-enroll; then run: sudo $(self_cmd) on"
}

next_steps() {
	echo "At the login screen: type your password as usual, or leave it empty, press Enter and touch the reader within 10 s."
	echo "Undo: sudo $(self_cmd) off"
	echo "If the login screen ever refuses you: Ctrl+Alt+F3, log in there, run the undo."
}

# ---------------------------------------------------------------- subcommands
cmd_on() {
	need_root
	lock_pam_dir
	check_pam_file
	local m
	for m in pam_fprintd.so pam_unix.so; do
		[ -f "$MODULE_DIR/$m" ] || fail "$MODULE_DIR/$m is missing; nothing was changed" \
			"install it first ($([ "$m" = pam_fprintd.so ] && echo "pacman -S fprintd" || echo "pacman -S pam"))"
	done
	ANCHOR_AT=$(anchor_line "$PAM")
	[ -n "$ANCHOR_AT" ] || fail "$PAM has no \"auth include system-login\" line to put the fingerprint lines before; nothing was changed" \
		"this tool expects Arch's SDDM PAM file; compare yours with: pacman -Qkk sddm"
	check_enrolled

	read_state "$PAM"
	case $STATE in
	on)
		echo "Fingerprint login at the login screen is already on; nothing was changed."
		next_steps
		return 0
		;;
	changed) fail "$PAM was changed by hand: $WHY; refusing to touch it (nothing was changed)" "$(changed_hint)" ;;
	esac
	if uses_fprintd "$PAM"; then
		fail "$PAM already uses pam_fprintd.so outside this tool's lines; refusing to add a second one (nothing was changed)" \
			"look at the pam_fprintd.so lines in $PAM"
	fi

	make_backup
	write_pam with_block

	# Check what is on disk now: our four lines, right before the anchor line.
	read_state "$PAM"
	local now
	now=$(anchor_line "$PAM")
	if [ "$STATE" != on ] || [ -z "$now" ] || [ "$BLOCK_AT" -ne $((now - 4)) ]; then
		fail "$PAM does not hold the fingerprint lines right before the \"auth include system-login\" line after writing it; check it now" \
			"undo: sudo $(self_cmd) off; the file as it was before on is $BACKUP"
	fi
	echo "Fingerprint login at the login screen is on."
	next_steps
}

cmd_off() {
	need_root
	lock_pam_dir
	check_pam_file
	read_state "$PAM"
	case $STATE in
	off)
		echo "Fingerprint login at the login screen is already off; nothing was changed."
		return 0
		;;
	changed) fail "$PAM was changed by hand: $WHY; refusing to touch it (nothing was changed)" "$(changed_hint)" ;;
	esac
	write_pam without_block
	read_state "$PAM"
	[ "$STATE" = off ] || fail "$PAM still holds fingerprint lines after writing it; check it now" "$(changed_hint)"
	echo "Fingerprint login at the login screen is off."
	if [ -e "$BACKUP" ]; then
		echo "  the backup $BACKUP stays (the file as it was before the first on)"
	fi
}

cmd_status() {
	local rc=0
	if [ -L "$PAM" ] || { [ -e "$PAM" ] && [ ! -f "$PAM" ]; }; then
		echo "Fingerprint login at the login screen: unknown ($PAM is not a regular file)"
		rc=1
	elif [ ! -e "$PAM" ]; then
		echo "Fingerprint login at the login screen: unknown ($PAM is missing; is SDDM installed?)"
		rc=1
	elif [ ! -r "$PAM" ]; then
		echo "Fingerprint login at the login screen: unknown (cannot read $PAM)"
		rc=1
	else
		read_state "$PAM"
		case $STATE in
		on) echo "Fingerprint login at the login screen: on (undo: sudo $(self_cmd) off)" ;;
		off)
			if uses_fprintd "$PAM"; then
				echo "Fingerprint login at the login screen: off as far as this tool knows ($PAM uses pam_fprintd.so outside its lines)"
			else
				echo "Fingerprint login at the login screen: off (turn it on: sudo $(self_cmd) on)"
			fi
			;;
		changed)
			echo "Fingerprint login at the login screen: changed by hand ($WHY); on and off refuse to touch it"
			echo "  $(changed_hint)"
			rc=1
			;;
		esac
	fi
	if [ -e "$BACKUP" ] || [ -L "$BACKUP" ]; then
		echo "Backup of the original: $BACKUP (present)"
	else
		echo "Backup of the original: none yet (the first on saves it as $BACKUP)"
	fi
	return "$rc"
}

case $cmd in
on) cmd_on ;;
off) cmd_off ;;
status) cmd_status ;;
esac
