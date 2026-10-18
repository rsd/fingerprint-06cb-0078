#!/usr/bin/env bash
# Step A5 at the sensor: does the fingerprint reader still work after the laptop sleeps?
#
#   tools/dev/a5-suspend.sh             the test (the laptop sleeps twice; about 3 minutes)
#   tools/dev/a5-suspend.sh --dry-run   the checks only; nothing sleeps, no fprintd client is started
#
# Every check uses your RIGHT INDEX finger (right-index-finger, enroll it first with
# tools/dev/a5-enroll.sh right-index-finger). Keep the laptop on mains power and the lid open.
#
# Part 1: the laptop goes to sleep while the reader is idle, then 3 checks (expect a match).
# Part 2: a check starts and waits for your finger; do NOT touch the sensor; the laptop goes to sleep
# while it waits (as on the lock screen). After waking it either says the check was interrupted by
# sleep (nothing to touch) or asks you to touch; then 2 more checks.
#
# Before each sleep the terminal says what happens: a 5-second countdown, the screen goes dark,
# wake the laptop with the power button or a key, unlock the lock screen with your password
# (fingerprint unlock is off there), then come back to the terminal and press Enter.
# Run it in a terminal: it waits for your Enter after each sleep (the dry run needs none).
# At the end: PASS or FAIL per part. Exit 0 only when both parts PASS. If the reader looks stuck
# (fprintd stops responding) the test stops; restarting fprintd needs sudo and is left to you.
#
# It never asks for a password or a finger to put the laptop to sleep: if the system wants
# authentication for that, the test refuses to start (here a password dialog would ask for a finger
# and get in the way of the check). If a program blocks sleep (a system update, say), it names that
# program and refuses to start too. It never uses sudo.
#
# It only observes and records: it never changes the driver or fprintd. The terminal shows only the
# instructions and the results. Everything else (fprintd output, journal and kernel USB lines, the
# reader's power state, suspend and resume times) goes to local/logs/a5-suspend-<timestamp>.log
# (mode 0600: it holds the unit serial). Run it as yourself, never with sudo. Env: LIBFPRINT_FORK.
# Needs the A5 dev install, or the libfprint-sigfm-0078 package with the driver's journal lines on
# (sudo tools/dev/a5-fprintd-dev.sh log on; log off when testing is done).
# Test-only variables: see tools/dev/a5-session-lib.sh.
set -euo pipefail
umask 077

SELF=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")
# shellcheck source=tools/dev/a5-session-lib.sh
. "$(dirname -- "$SELF")/a5-session-lib.sh"
usage() { sed -n '2,/^set /s/^# \{0,1\}//p' "$SELF"; }

dry_run=0
while [ $# -gt 0 ]; do
	case $1 in
	--dry-run) dry_run=1 ;;
	-h | --help)
		usage
		exit 0
		;;
	*) fail "unknown argument: $1" "see $0 --help" ;;
	esac
	shift
done

# a5_find_reader: the sysfs directory of the 06cb:0078 device, if connected.
a5_find_reader() {
	local d
	for d in "$SYS_USB"/*; do
		[ -r "$d/idVendor" ] && [ -r "$d/idProduct" ] || continue
		if [ "$(<"$d/idVendor")" = 06cb ] && [ "$(<"$d/idProduct")" = 0078 ]; then
			echo "$d"
			return 0
		fi
	done
	return 1
}

# a5_can_suspend: logind's answer (yes, challenge, no, na) for this session. Only "yes" will do: "challenge"
# means polkit would ask for authentication, and pam_fprintd serves polkit here, so that prompt would want a
# finger and compete with the verify under test.
a5_can_suspend() {
	local out
	command -v -- "$BUSCTL" >/dev/null || fail "${BUSCTL##*/} not found" "install systemd (it brings busctl)"
	out=$("$BUSCTL" --allow-interactive-authorization=no call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager \
		CanSuspend 2>&1) || fail "cannot ask the system whether it may sleep (busctl failed: $(tail -n 1 <<<"$out"))"
	sed -n 's/^s "\(.*\)"$/\1/p' <<<"$out"
}

# a5_sleep_blockers: who blocks sleep right now and why, from logind's block-mode sleep inhibitors, one
# per line: "omarchy-update (Omarchy update in progress)"; nothing when the list cannot be read.
a5_sleep_blockers() {
	"$BUSCTL" --allow-interactive-authorization=no --json=short call org.freedesktop.login1 \
		/org/freedesktop/login1 org.freedesktop.login1.Manager ListInhibitors 2>/dev/null |
		"$PY" -c '
import json, sys
names = []
for what, who, why, mode, _uid, pid in json.load(sys.stdin)["data"][0]:
    if mode.startswith("block") and "sleep" in what.split(":"):
        who, why = " ".join(who.split()), " ".join(why.split())
        name = who or f"process {pid}"
        names.append(f"{name} ({why})" if why else name)
for name in dict.fromkeys(names):
    print(name)
' 2>/dev/null || true
}

FINGER=$A5S_DEFAULT_FINGER
a5s_guard_root
a5s_setup
# The Enter after each sleep is read from stdin: without a terminal every wait would pass at once. The
# offline tests (A5_TEST_ROOT) feed it from a pipe.
if [ "$dry_run" -eq 0 ] && [ ! -t 0 ] && [ -z "$TR" ]; then
	fail "run this in a terminal: it waits for you to press Enter after each sleep"
fi
SUSPEND_PY=$ROOT/tools/dev/a5_suspend.py
SHOW=$(a5s_finger_display "$FINGER")
echo "A5 suspend: does the reader still work after the laptop sleeps? Every check uses your $SHOW."
printf 'Checking the setup... '
a5s_preflight verify
READER=$(a5_find_reader) ||
	fail "the fingerprint reader (USB 06cb:0078) is not connected" "check: lsusb | grep -i 06cb:0078"
note "reader in sysfs: ${READER##*/}"
CAN=$(a5_can_suspend)
note "logind CanSuspend: ${CAN:-unknown}"
case $CAN in
yes) ;;
challenge) fail "suspend would ask for authentication, and this test does not support that" ;;
inhibited)
	mapfile -t blockers < <(a5_sleep_blockers)
	if [ ${#blockers[@]} -gt 0 ]; then
		them=it
		[ ${#blockers[@]} -eq 1 ] || them=them
		printf -v list '%s, ' "${blockers[@]}"
		fail "sleep is blocked by: ${list%, }" "finish or close $them, then run this again"
	fi
	fail "this system does not let you put the laptop to sleep (logind says '$CAN')"
	;;
*) fail "this system does not let you put the laptop to sleep (logind says '${CAN:-nothing}')" ;;
esac
echo ok
if [ "$dry_run" -eq 1 ]; then
	echo "dry run: checks passed; nothing slept and fprintd-verify was not started. The test would:"
	echo "  1. put the laptop to sleep, then 3 checks with your $SHOW"
	echo "  2. start a check, put the laptop to sleep while it waits, then 2 more checks"
	echo "  Each time: wake it, unlock with your password, come back here and press Enter."
	exit 0
fi

a5s_open_log suspend
fingers=$(a5s_enrolled_fingers)
grep -qxF -- "$FINGER" <<<"$fingers" ||
	fail "$FINGER is not enrolled for $USER_NAME" "enroll it first: tools/dev/a5-enroll.sh $FINGER"

echo "Log (private, it holds the sensor serial): $LOG"
echo "Touch the sensor only when asked, only with your $SHOW, and lift when told."
set +e
"$PY" "$SUSPEND_PY" --log "$LOG" --journalctl "$JOURNALCTL" --fprintd-verify "$FPRINTD_VERIFY" \
	--systemctl "$SYSTEMCTL" --systemd-inhibit "$SYSTEMD_INHIBIT" --usb-devices "$SYS_USB" \
	--countdown-s "$SUSPEND_COUNTDOWN_S"
rc=$?
set -e
exit "$rc"
