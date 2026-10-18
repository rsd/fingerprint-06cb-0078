#!/usr/bin/env bash
# Step A5 at the sensor: verify rounds through the system fprintd, with short prompts and a tally.
#
#   tools/dev/a5-verify.sh --finger-used LABEL --expect match|no-match [--rounds N] [--finger FINGER]
#
#   --finger-used LABEL   the finger you touch with: right-index, right-middle, left-thumb, ...
#   --expect              match (the enrolled finger) or no-match (any other finger)
#   --rounds N            rounds, 1..50 (default 10)
#   --finger FINGER       the enrolled finger fprintd-verify checks against (default right-index-finger;
#                         left-index-finger or left-index for the left index, and so on)
#   --dry-run             the checks only; no fprintd client is started
#
# Example: tools/dev/a5-verify.sh --finger-used left-index --expect match --finger left-index-finger
#
# Each round runs fprintd-verify once: touch with the --finger-used finger when asked (every prompt
# names it), lift when told. Retries ("touch again") are counted, not failures. At the end:
# match / no match / retries / errors and "as expected: X/N". Exit 0 only when every round was as
# expected.
#
# The terminal shows only the prompts and the results. Everything else (fprintd output, journal
# lines, every "sigfm score" per round) goes to local/logs/a5-verify-<timestamp>.log (mode 0600:
# it holds the unit serial). Run it as yourself, never with sudo. Env: LIBFPRINT_FORK.
# Needs the A5 dev install, or the libfprint-sigfm-0078 package with the driver's journal lines on
# (sudo tools/dev/a5-fprintd-dev.sh log on; log off when testing is done).
# Test-only variables: see tools/dev/a5-session-lib.sh.
set -euo pipefail
umask 077

SELF=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")
# shellcheck source=tools/dev/a5-session-lib.sh
. "$(dirname -- "$SELF")/a5-session-lib.sh"
usage() { sed -n '2,/^set /s/^# \{0,1\}//p' "$SELF"; }

FINGER=$A5S_DEFAULT_FINGER
USED=""
EXPECT=""
ROUNDS=10
dry_run=0
need() { [ "$2" -ge 2 ] || fail "$1 needs a value" "see $0 --help"; }
while [ $# -gt 0 ]; do
	case $1 in
	--finger-used)
		need "$1" $#
		USED=$2
		shift
		;;
	--finger-used=*) USED=${1#*=} ;;
	--expect)
		need "$1" $#
		EXPECT=$2
		shift
		;;
	--expect=*) EXPECT=${1#*=} ;;
	--rounds)
		need "$1" $#
		ROUNDS=$2
		shift
		;;
	--rounds=*) ROUNDS=${1#*=} ;;
	--finger)
		need "$1" $#
		FINGER=$2
		shift
		;;
	--finger=*) FINGER=${1#*=} ;;
	--dry-run) dry_run=1 ;;
	-h | --help)
		usage
		exit 0
		;;
	*) fail "unknown argument: $1" "see $0 --help" ;;
	esac
	shift
done
[ -n "$USED" ] || fail "--finger-used is required (the finger you touch with, e.g. right-middle)" "see $0 --help"
a5s_finger_name "$USED" >/dev/null ||
	fail "--finger-used '$USED' is not a finger" "e.g. right-index, right-middle, left-thumb"
case $EXPECT in
match | no-match) ;;
"") fail "--expect is required: match or no-match" "see $0 --help" ;;
*) fail "--expect must be match or no-match, got '$EXPECT'" ;;
esac
[[ $ROUNDS =~ ^[0-9]{1,2}$ ]] && [ "$((10#$ROUNDS))" -ge 1 ] && [ "$((10#$ROUNDS))" -le 50 ] ||
	fail "--rounds must be a whole number from 1 to 50, got '$ROUNDS'"
ROUNDS=$((10#$ROUNDS))
FINGER_IN=$FINGER
FINGER=$(a5s_finger_name "$FINGER_IN") || fail "unknown finger '$FINGER_IN'" "one of: ${A5S_FINGERS[*]}"
USED_NAME=$(a5s_finger_name "$USED")
SHOW_USED=$(a5s_finger_display "$USED_NAME")
# A match is expected exactly when the finger touched is the enrolled one; anything else is a mix-up.
if [ "$EXPECT" = match ] && [ "$USED_NAME" != "$FINGER" ]; then
	fail "--expect match, but you touch with $USED_NAME and fprintd checks against $FINGER" \
		"to check the $USED_NAME print: --finger $USED_NAME"
elif [ "$EXPECT" = no-match ] && [ "$USED_NAME" = "$FINGER" ]; then
	fail "--expect no-match, but you touch with the enrolled finger $FINGER" \
		"touch with another finger (--finger-used), or --expect match"
fi

a5s_guard_root
a5s_setup
echo "A5 verify: $ROUNDS round(s) with your $SHOW_USED against the enrolled $FINGER, expecting ${EXPECT/-/ }"
printf 'Checking the setup... '
a5s_preflight verify
echo ok
if [ "$dry_run" -eq 1 ]; then
	echo "dry run: checks passed; fprintd-verify not started"
	exit 0
fi

a5s_open_log verify
fingers=$(a5s_enrolled_fingers)
grep -qxF -- "$FINGER" <<<"$fingers" ||
	fail "$FINGER is not enrolled for $USER_NAME" "enroll it first: tools/dev/a5-enroll.sh $FINGER"

echo "Log (private, it holds the sensor serial): $LOG"
echo "Touch the sensor only when asked, only with your $SHOW_USED, and lift when told."
set +e
"$PY" "$SESSION" verify --finger "$FINGER" --finger-used "$USED" --expect "$EXPECT" --rounds "$ROUNDS" \
	--log "$LOG" --journalctl "$JOURNALCTL" --fprintd-verify "$FPRINTD_VERIFY"
rc=$?
set -e
exit "$rc"
