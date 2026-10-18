#!/usr/bin/env bash
# Step A5 at the sensor: enroll ONE named finger through the system fprintd, with short prompts.
#
#   tools/dev/a5-enroll.sh FINGER [--replace]   enroll FINGER; touch only with that finger
#   tools/dev/a5-enroll.sh FINGER --dry-run     the checks only; no fprintd client is started
#
# FINGER is required, so that every prompt can name it. It is one of fprintd's names: left-thumb,
# left-index-finger, left-middle-finger, left-ring-finger, left-little-finger, right-thumb,
# right-index-finger, right-middle-finger, right-ring-finger, right-little-finger (the short form
# left-index works too). Example: tools/dev/a5-enroll.sh left-index-finger
#
# It first shows the fingers already enrolled (fprintd-list). If FINGER is one of them it asks
# before deleting that print; --replace answers yes without asking. fprintd may open a password
# dialog for the delete and the enroll.
#
# Needs the A5 dev install current (sudo tools/dev/a5-fprintd-dev.sh install after each rebuild of
# the fork), or the libfprint-sigfm-0078 package with the driver's journal lines on (sudo
# tools/dev/a5-fprintd-dev.sh log on; log off when testing is done), and fprintd's journal readable
# by you: the prompts (LIFT, Lifted) come from the driver's journal lines.
#
# The terminal shows only the prompts and the result. Everything else (fprintd output, journal
# lines, debug) goes to local/logs/a5-enroll-<timestamp>.log (mode 0600: it holds the unit serial).
# Run it as yourself, never with sudo. Env: LIBFPRINT_FORK (default ../libfprint next to this
# project). Test-only variables: see tools/dev/a5-session-lib.sh.
set -euo pipefail
umask 077

SELF=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")
# shellcheck source=tools/dev/a5-session-lib.sh
. "$(dirname -- "$SELF")/a5-session-lib.sh"
usage() { sed -n '2,/^set /s/^# \{0,1\}//p' "$SELF"; }

FINGER=""
dry_run=0
replace=0
while [ $# -gt 0 ]; do
	case $1 in
	--dry-run) dry_run=1 ;;
	--replace) replace=1 ;;
	-h | --help)
		usage
		exit 0
		;;
	-*) fail "unknown option: $1" "see $0 --help" ;;
	*)
		[ -z "$FINGER" ] || fail "more than one finger: $FINGER, $1" "enroll one finger per run"
		FINGER=$1
		;;
	esac
	shift
done
[ -n "$FINGER" ] || fail "name the finger to enroll, e.g. tools/dev/a5-enroll.sh left-index-finger" \
	"one of: ${A5S_FINGERS[*]}"
FINGER_IN=$FINGER
FINGER=$(a5s_finger_name "$FINGER_IN") || fail "unknown finger '$FINGER_IN'" "one of: ${A5S_FINGERS[*]}"
SHOW=$(a5s_finger_display "$FINGER")

a5s_guard_root
a5s_setup
echo "Enrolling your $SHOW: use only that finger for all $A5S_ENROLL_STAGES touches"
printf 'Checking the setup... '
a5s_preflight enroll
echo ok
if [ "$dry_run" -eq 1 ]; then
	echo "dry run: checks passed; fprintd-enroll not started"
	exit 0
fi

a5s_open_log enroll
printf 'enrolling %s for %s (replace %s)\n' "$FINGER" "$USER_NAME" "$replace" | a5s_log wrapper
fingers=$(a5s_enrolled_fingers)
echo "Enrolled now: $(if [ -n "$fingers" ]; then paste -sd' ' <<<"$fingers"; else echo none; fi)"
password_note() { echo "A password dialog may appear: fprintd asks for your password before it changes prints."; }
if grep -qxF -- "$FINGER" <<<"$fingers"; then
	if [ "$replace" -eq 1 ]; then
		echo "Your $SHOW is already enrolled ($FINGER): deleting that print first (--replace)."
	else
		# Not read -p: it shows the question only when stdin is a terminal.
		printf 'Your %s is already enrolled (%s). Delete that print and enroll it again? [y/N] ' "$SHOW" "$FINGER"
		read -r answer || answer=""
		[ -t 0 ] || echo
		printf 'answer to the delete question: %s\n' "${answer:-<none>}" | a5s_log wrapper
		case $answer in
		[yY] | [yY][eE][sS]) ;;
		*)
			echo "Nothing changed: $FINGER stays enrolled. (--replace deletes it without asking.)"
			exit 1
			;;
		esac
	fi
	password_note
	rc=0
	out=$("$FPRINTD_DELETE" "$USER_NAME" -f "$FINGER" 2>&1) || rc=$?
	printf '%s\n' "$out" | a5s_log fprintd-delete
	if [ "$rc" -ne 0 ] || ! fingers=$(a5s_enrolled_fingers) || grep -qxF -- "$FINGER" <<<"$fingers"; then
		fail "could not delete $FINGER (fprintd-delete exit $rc)" "see the log: $LOG"
	fi
	echo "Deleted $FINGER."
else
	password_note
fi

echo "Log (private, it holds the sensor serial): $LOG"
echo "Touch the sensor only when asked, only with your $SHOW, and lift when told."
set +e
"$PY" "$SESSION" enroll --finger "$FINGER" --log "$LOG" --journalctl "$JOURNALCTL" --fprintd-enroll "$FPRINTD_ENROLL"
rc=$?
set -e
exit "$rc"
