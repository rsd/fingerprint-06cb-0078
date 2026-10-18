# Shared part of tools/dev/a5-enroll.sh, tools/dev/a5-verify.sh and tools/dev/a5-suspend.sh: sourced, never run.
#
# Paths, the test-only overrides, the preflight and the private log. The terminal gets only short lines;
# the details go to the log. The preflight accepts either install of the driver:
#   dev      the fork's build through the A5 dev drop-in (sudo tools/dev/a5-fprintd-dev.sh install)
#   package  the libfprint-sigfm-0078 package (A6) plus the debug drop-in that sends the driver's lines to
#            the journal (sudo tools/dev/a5-fprintd-dev.sh log on; log off when testing is done)
#
# TEST ONLY (honoured only while A5_TEST_ROOT is set; never set them on a real system; as root the
# wrappers refuse to run at all, and name the variable when one of these is set):
#   A5_TEST_ROOT        prefix for the system paths below (also /sys/bus/usb/devices); the log goes to
#                       $A5_TEST_ROOT/logs
#   A5_SYSTEMCTL, A5_JOURNALCTL, A5_FPRINTD_LIST, A5_FPRINTD_DELETE, A5_FPRINTD_ENROLL, A5_FPRINTD_VERIFY,
#   A5_BUSCTL, A5_SYSTEMD_INHIBIT, A5_PACMAN
#                       commands to run instead of the real ones (a5-suspend.sh runs
#                       A5_SYSTEMCTL suspend --no-ask-password)
#   A5_SUSPEND_COUNTDOWN_S  a5-suspend.sh: seconds per step of its 5..1 countdown (default 1)
#   A5_FAKE_EUID        (not root only) the euid the root check sees, to test it

A5S_TEST_VARS=(A5_TEST_ROOT A5_SYSTEMCTL A5_JOURNALCTL A5_FPRINTD_LIST A5_FPRINTD_DELETE A5_FPRINTD_ENROLL
	A5_FPRINTD_VERIFY A5_BUSCTL A5_SYSTEMD_INHIBIT A5_PACMAN A5_SUSPEND_COUNTDOWN_S A5_FAKE_EUID)
# fprintd's finger names (fprintd-enroll --help, fprintd(1)).
A5S_FINGERS=(left-thumb left-index-finger left-middle-finger left-ring-finger left-little-finger
	right-thumb right-index-finger right-middle-finger right-ring-finger right-little-finger)
A5S_DEFAULT_FINGER=right-index-finger
# The driver's SYNAPTICS0078_ENROLL_STAGES, as tools/dev/a5_session.py has it (a test checks they match).
A5S_ENROLL_STAGES=18
A5S_INSTALL_HINT="run: sudo tools/dev/a5-fprintd-dev.sh install"
A5S_LOG_HINT="run: sudo tools/dev/a5-fprintd-dev.sh log on"
# The install the preflight found: dev or package (a5s_preflight sets it).
A5S_MODE=""

fail() {
	printf 'FAILED: %s\n' "$1" >&2
	[ $# -lt 2 ] || printf '  hint: %s\n' "$2" >&2
	exit 2
}
warn() { printf '  WARN  %s\n' "$*"; }
# Preflight details for the log (it is opened after the preflight).
A5S_NOTES=()
note() { A5S_NOTES+=("$*"); }

# Root never: fprintd enrolls and verifies for the user who runs the client, and the test hooks
# redirect what is run.
a5s_guard_root() {
	local euid v
	euid=$(id -u)
	[ "$euid" -eq 0 ] || euid=${A5_FAKE_EUID:-$euid}
	[ "$euid" = 0 ] || return 0
	for v in "${A5S_TEST_VARS[@]}"; do
		if [ -n "${!v+set}" ]; then
			fail "$v is set; the test-only variables are refused as root" \
				"unset it (sudo -E and env_keep pass it on): sudo env -u $v ..."
		fi
	done
	fail "do not run this as root or with sudo" \
		"run it as yourself: fprintd enrolls and verifies the prints of the user who runs it"
}

# a5s_is_finger NAME: one of fprintd's finger names.
a5s_is_finger() {
	local f
	for f in "${A5S_FINGERS[@]}"; do [ "$1" = "$f" ] && return 0; done
	return 1
}

# a5s_finger_name LABEL: the fprintd name for 'left-index' or 'left-index-finger' (any case); fails if none.
a5s_finger_name() {
	local f=${1,,}
	if a5s_is_finger "$f"; then
		echo "$f"
	elif a5s_is_finger "$f-finger"; then
		echo "$f-finger"
	else
		return 1
	fi
}

# a5s_finger_display NAME: 'left-index-finger' -> 'LEFT INDEX finger', 'left-thumb' -> 'LEFT THUMB'.
a5s_finger_display() {
	local f=${1%-finger} side digit
	side=${f%%-*}
	digit=${f#*-}
	if [ "$digit" = thumb ]; then
		echo "${side^^} ${digit^^}"
	else
		echo "${side^^} ${digit^^} finger"
	fi
}

a5s_setup() {
	ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
	FORK=${LIBFPRINT_FORK:-$(cd -- "$ROOT/.." && pwd)/libfprint}
	PY=/usr/bin/python3
	SESSION=$ROOT/tools/dev/a5_session.py
	USER_NAME=$(id -un)
	TR=${A5_TEST_ROOT:-}
	SYSTEMCTL=systemctl
	JOURNALCTL=journalctl
	FPRINTD_LIST=fprintd-list
	FPRINTD_DELETE=fprintd-delete
	FPRINTD_ENROLL=fprintd-enroll
	FPRINTD_VERIFY=fprintd-verify
	BUSCTL=busctl
	SYSTEMD_INHIBIT=systemd-inhibit
	PACMAN=pacman
	SUSPEND_COUNTDOWN_S=1
	LOG_DIR=$ROOT/local/logs
	if [ -n "$TR" ]; then
		case $TR in /*) ;; *) fail "A5_TEST_ROOT must be an absolute path" ;; esac
		SYSTEMCTL=${A5_SYSTEMCTL:-$SYSTEMCTL}
		JOURNALCTL=${A5_JOURNALCTL:-$JOURNALCTL}
		FPRINTD_LIST=${A5_FPRINTD_LIST:-$FPRINTD_LIST}
		FPRINTD_DELETE=${A5_FPRINTD_DELETE:-$FPRINTD_DELETE}
		FPRINTD_ENROLL=${A5_FPRINTD_ENROLL:-$FPRINTD_ENROLL}
		FPRINTD_VERIFY=${A5_FPRINTD_VERIFY:-$FPRINTD_VERIFY}
		BUSCTL=${A5_BUSCTL:-$BUSCTL}
		SYSTEMD_INHIBIT=${A5_SYSTEMD_INHIBIT:-$SYSTEMD_INHIBIT}
		PACMAN=${A5_PACMAN:-$PACMAN}
		SUSPEND_COUNTDOWN_S=${A5_SUSPEND_COUNTDOWN_S:-$SUSPEND_COUNTDOWN_S}
		LOG_DIR=$TR/logs
	fi
	# The paths tools/dev/a5-fprintd-dev.sh installs (SYS_LIBDIR, LIBFILE, DROPIN) and builds from (FORK).
	SYS_LIB=$TR/usr/local/lib/libfprint-0078-dev/libfprint-2.so.2.0.0
	DROPIN=$TR/etc/systemd/system/fprintd.service.d/libfprint-0078-dev.conf
	# The packaged install: the lib fprintd loads without the dev drop-in, and the debug drop-in
	# a5-fprintd-dev.sh log on writes (DEBUG_DROPIN there).
	PKG_LIB=$TR/usr/lib/libfprint-2.so.2
	DEBUG_DROPIN=$TR/etc/systemd/system/fprintd.service.d/synaptics0078-debug.conf
	BUILD_LIB=$FORK/build/libfprint/libfprint-2.so.2.0.0
	DRIVER_SRC=$FORK/libfprint/drivers/synaptics0078.c
	SYS_USB=$TR/sys/bus/usb/devices
	STAMP=$(date +%Y%m%d-%H%M%S)
}

a5s_sha() { sha256sum <"$1" | cut -d' ' -f1; }
# a5s_mtime_us FILE: its mtime in microseconds since the epoch.
a5s_mtime_us() {
	local m
	m=$(LC_ALL=C stat -c %.6Y -- "$1") || return 1
	echo "${m/./}"
}
# a5s_start_us: when fprintd's main process started, in microseconds since the epoch ("" if unknown).
a5s_start_us() {
	local start
	start=$("$SYSTEMCTL" show --timestamp=us+utc -p ExecMainStartTimestamp --value fprintd.service 2>/dev/null) || return 0
	[ -n "$start" ] || return 0
	date -u -d "$start" +%s%6N 2>/dev/null || true
}
# a5s_pkg_version: pacman's line for the package, for the log.
a5s_pkg_version() {
	command -v -- "$PACMAN" >/dev/null || {
		echo "pacman not found"
		return 0
	}
	"$PACMAN" -Q libfprint-sigfm-0078 2>&1 | head -n 1 || true
}

# a5s_pkg_has_driver: the packaged libfprint has the synaptics0078 driver (as a5-fprintd-dev.sh
# undo --keep-data checks it): the libfprint-sigfm-0078 package is installed.
a5s_pkg_has_driver() { [ -f "$PKG_LIB" ] && LC_ALL=C grep -qaF libfprint-synaptics0078 -- "$PKG_LIB"; }

# a5s_preflight [verify]: one of the two installs is in place (the dev install current, or the package
# with the debug drop-in), fprintd started after its drop-in, and its journal is readable.
a5s_preflight() {
	local mode=${1:-enroll} sys_sha build_sha active start_us ref ref_us hint stale what jerr jrc dropin unit_env
	if [ -f "$SYS_LIB" ]; then
		# dev: the fork's build, as tools/dev/a5-fprintd-dev.sh install copied it.
		A5S_MODE=dev
		note "mode: dev ($SYS_LIB)"
		[ -r "$BUILD_LIB" ] ||
			fail "no fork build at $BUILD_LIB" "build the fork (meson compile -C $FORK/build) or set LIBFPRINT_FORK"
		sys_sha=$(a5s_sha "$SYS_LIB")
		build_sha=$(a5s_sha "$BUILD_LIB")
		[ "$sys_sha" = "$build_sha" ] ||
			fail "the installed libfprint differs from the fork build (rebuilt since the install?)" "$A5S_INSTALL_HINT"
		note "installed $SYS_LIB = fork build $BUILD_LIB (sha256 $sys_sha)"
		if [ -r "$DRIVER_SRC" ] && [ "$DRIVER_SRC" -nt "$BUILD_LIB" ]; then
			warn "the fork build is older than synaptics0078.c: rebuild: meson compile -C $FORK/build, then $A5S_INSTALL_HINT"
		fi
		[ -r "$DROPIN" ] || fail "no fprintd drop-in at $DROPIN" "$A5S_INSTALL_HINT"
		dropin=$DROPIN
		hint=$A5S_INSTALL_HINT
		# fprintd must have started after the lib was installed.
		ref=$SYS_LIB
		stale="fprintd has been running since before the lib was installed: it still runs the old libfprint"
		what="the install"
	elif a5s_pkg_has_driver; then
		# package: libfprint-sigfm-0078; its fprintd unit sends no debug lines without the debug drop-in.
		A5S_MODE=package
		note "mode: package ($PKG_LIB has the synaptics0078 driver; no dev lib at $SYS_LIB)"
		note "package: $(a5s_pkg_version)"
		note "package lib $PKG_LIB -> $(readlink -f -- "$PKG_LIB") (sha256 $(a5s_sha "$PKG_LIB"))"
		[ ! -e "$DROPIN" ] && [ ! -L "$DROPIN" ] ||
			fail "the dev drop-in $DROPIN is still there, but the dev lib is gone: the hand-over to the package is incomplete" \
				"run: sudo tools/dev/a5-fprintd-dev.sh undo --keep-data"
		[ -r "$DEBUG_DROPIN" ] ||
			fail "no debug drop-in at $DEBUG_DROPIN: fprintd does not send the driver's lines to the journal; the prompts need them" \
				"$A5S_LOG_HINT"
		dropin=$DEBUG_DROPIN
		hint=$A5S_LOG_HINT
		# fprintd must have started after the debug drop-in was written (never the lib's mtime:
		# pacman keeps the build time), and systemd must have loaded the drop-in.
		ref=$DEBUG_DROPIN
		stale="fprintd has been running since before the debug drop-in was written: it runs without the debug lines"
		what="log on"
	else
		fail "the dev libfprint is not installed ($SYS_LIB is missing), and $PKG_LIB has no synaptics0078 driver (the libfprint-sigfm-0078 package is not installed)" \
			"$A5S_INSTALL_HINT; or build and install the package (README, A6): cd packaging/arch/libfprint-sigfm-0078 && makepkg -f && sudo pacman -U libfprint-sigfm-0078-[0-9]*-x86_64.pkg.tar.zst, then sudo tools/dev/a5-fprintd-dev.sh log on"
	fi

	grep -Eq '^Environment=.*G_MESSAGES_DEBUG=[^"]*libfprint-synaptics0078' -- "$dropin" ||
		fail "the drop-in does not send the driver's lines to the journal (G_MESSAGES_DEBUG lacks libfprint-synaptics0078); the prompts need them" \
			"$hint"
	if [ "$mode" = verify ] && ! grep -Eq '^Environment=.*G_MESSAGES_DEBUG=[^"]*libfprint-print' -- "$dropin"; then
		warn "the drop-in's G_MESSAGES_DEBUG lacks libfprint-print: no sigfm scores in the log"
	fi
	note "drop-in $dropin has G_MESSAGES_DEBUG for libfprint-synaptics0078"
	if [ "$A5S_MODE" = package ]; then
		# Written but never loaded (a log on that stopped before its daemon-reload): fprintd,
		# however recently started, runs without it.
		if unit_env=$("$SYSTEMCTL" show -p Environment --value fprintd.service 2>/dev/null); then
			[[ $unit_env == *G_MESSAGES_DEBUG=*libfprint-synaptics0078* ]] ||
				fail "systemd has not loaded the debug drop-in: fprintd.service has no G_MESSAGES_DEBUG for the driver" \
					"$hint (it reloads systemd and restarts fprintd)"
			note "fprintd.service Environment has G_MESSAGES_DEBUG for libfprint-synaptics0078"
		else
			warn "cannot read fprintd.service's Environment; if the debug lines are missing: $hint"
		fi
	fi

	active=$("$SYSTEMCTL" show -p ActiveState --value fprintd.service 2>/dev/null) || active=unknown
	note "fprintd.service ActiveState: ${active:-unknown}"
	if [ "$active" = active ]; then
		# To the microsecond: a start in the same second, before the write, is stale too.
		start_us=$(a5s_start_us)
		ref_us=$(a5s_mtime_us "$ref")
		if ! [[ $start_us =~ ^[0-9]+$ ]]; then
			warn "cannot tell when fprintd started; if it predates $what: sudo systemctl restart fprintd"
		elif [ "$start_us" -lt "$ref_us" ]; then
			fail "$stale" "$hint (it restarts fprintd)"
		fi
	fi

	jrc=0
	jerr=$("$JOURNALCTL" -u fprintd -n 1 -o cat 2>&1 >/dev/null) || jrc=$?
	if [ "$jrc" -ne 0 ] || [[ $jerr == *"not seeing messages"* ]]; then
		fail "cannot read fprintd's journal as $USER_NAME; the prompts need it" \
			"join a group that may read it (sudo usermod -aG systemd-journal $USER_NAME), then log in again"
	fi
	note "journalctl -u fprintd is readable"
	[ -x "$PY" ] || fail "$PY not found" "install python"
	local client=$FPRINTD_ENROLL c
	[ "$mode" = verify ] && client=$FPRINTD_VERIFY
	for c in "$FPRINTD_LIST" "$FPRINTD_DELETE" "$client"; do
		[ "$mode" = verify ] && [ "$c" = "$FPRINTD_DELETE" ] && continue
		command -v -- "$c" >/dev/null || fail "${c##*/} not found" "install fprintd (it brings the fprintd-* clients)"
	done
}

# a5s_open_log NAME: local/logs (0700) and a new log (0600: it holds the unit serial) in LOG.
a5s_open_log() {
	local d log_uid log_mode
	for d in "$(dirname -- "$LOG_DIR")" "$LOG_DIR"; do
		if [ -L "$d" ]; then
			fail "$d is a symlink" "logs are written only to real directories"
		elif [ -e "$d" ] && [ ! -d "$d" ]; then
			fail "$d exists and is not a directory"
		fi
	done
	mkdir -p -m 700 -- "$LOG_DIR"
	read -r log_uid log_mode < <(stat -L -c '%u %a' -- "$LOG_DIR")
	if ((8#$log_mode & 8#077)); then
		if [ "$log_uid" -eq "$(id -u)" ]; then
			chmod 700 -- "$LOG_DIR"
		else
			warn "$LOG_DIR is mode $log_mode and not owned by $USER_NAME: others may read the log"
		fi
	fi
	LOG=$LOG_DIR/a5-$1-$STAMP.log
	[ ! -e "$LOG" ] && [ ! -L "$LOG" ] || fail "$LOG already exists" "wait a second and run again"
	: >"$LOG"
	chmod 600 -- "$LOG"
	{
		echo "# a5-$1 $(date -Is) user $USER_NAME"
		if [ "$A5S_MODE" = package ]; then
			echo "# libfprint: package $PKG_LIB (libfprint-sigfm-0078)"
		else
			echo "# fork $FORK @ $(git --no-optional-locks -C "$FORK" rev-parse --short HEAD 2>/dev/null || echo unknown)"
		fi
		printf '# preflight: %s\n' "${A5S_NOTES[@]}"
	} >>"$LOG"
}

# a5s_log TAG < lines: append to the log with a timestamp, like a5_session.py does.
a5s_log() {
	local line now
	while IFS= read -r line; do
		now=$(date +%H:%M:%S.%3N)
		printf '%s [%s] %s\n' "$now" "$1" "$line" >>"$LOG"
	done
}

# a5s_enrolled_fingers: the user's enrolled finger names, one per line (fprintd-list output to the log).
# fprintd-list D-Bus activates fprintd; that is fine here, after the preflight.
a5s_enrolled_fingers() {
	local out rc=0
	out=$("$FPRINTD_LIST" "$USER_NAME" 2>&1) || rc=$?
	printf '%s\n' "$out" | a5s_log fprintd-list
	if [ "$rc" -ne 0 ] || grep -q 'No devices available' <<<"$out"; then
		fail "fprintd-list failed (exit $rc): $(tail -n 1 <<<"$out")" "check: tools/dev/a5-fprintd-dev.sh status; the log has the output: $LOG"
	fi
	sed -n 's/^ - #[0-9]*: \([a-z-]*\)$/\1/p' <<<"$out"
}
