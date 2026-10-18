#!/usr/bin/env bash
# Step B3, offline: the false-accept benchmark over the B3 captures (tools/dev/b3-capture.sh).
#
#   tools/dev/b3-analyze.sh [--k N] [--seed N] [--data DIR] [--stages N] [--skip-parity]
#
#   --k N           random 18-stage templates per (identity, session) (default 5)
#   --seed N        seed of the template draws (default 0)
#   --data DIR      the capture tree (default local/b3: <person>/<label>/NNN.pgm + NNN.json)
#   --stages N      template size; for smoke runs on small data only (default 18, the driver's)
#   --skip-parity   only when the parity data set (local/far-diag/map.csv, local/diag-fprintd) is absent
#
# Steps:
#   1. builds the scorer, local/b3/bin/sigfm_matrix (research/a5/build_harness.sh against the fork's
#      build/libfprint/sigfm/libsigfm.a), when it is missing or older than libsigfm.a or its source;
#   2. proves parity (research/b3/b3_parity.py: the scorer reproduces every score fprintd's journal
#      logged on 2026-10-15) when local/b3/results/parity.json is missing or was made by another
#      scorer binary or from another local/far-diag/map.csv (sha256 of both); it stops on a parity failure;
#   3. runs research/b3/b3_analyze.py and prints its summary.
#
# Prints only the summary. Compiler and harness output go to local/logs/b3-analyze-<timestamp>.log
# (0600). Results, all biometric-derived: local/b3/results/ (0700). No sensor access, no root.
# Env: LIBFPRINT_FORK (default ~/Devel/Linux/Synaptics/libfprint; its build dir is $LIBFPRINT_FORK/build).
set -euo pipefail
umask 077

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
FORK=${LIBFPRINT_FORK:-$HOME/Devel/Linux/Synaptics/libfprint}
BUILD=$FORK/build
LIB=$BUILD/libfprint/sigfm/libsigfm.a
SRC=$ROOT/research/a5/sigfm_matrix.cpp
BUILDER=$ROOT/research/a5/build_harness.sh
BIN=$ROOT/local/b3/bin/sigfm_matrix
PARITY=$ROOT/local/b3/results/parity.json
PY=/usr/bin/python3
PARITY_PY=$ROOT/research/b3/b3_parity.py
MAP=$ROOT/local/far-diag/map.csv
ANALYZE_PY=$ROOT/research/b3/b3_analyze.py
LOG_DIR=$ROOT/local/logs
STAMP=$(date +%Y%m%d-%H%M%S)
LOG=$LOG_DIR/b3-analyze-$STAMP.log

fail() {
	printf 'FAILED: %s\n' "$1" >&2
	[ $# -lt 2 ] || printf '  hint: %s\n' "$2" >&2
	exit 2
}
usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

skip_parity=0
args=()
while [ $# -gt 0 ]; do
	case $1 in
	--k | --seed | --data | --stages)
		[ $# -ge 2 ] || fail "$1 needs a value" "tools/dev/b3-analyze.sh --help"
		args+=("$1" "$2")
		shift 2
		;;
	--skip-parity)
		skip_parity=1
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	*) fail "unknown argument: $1" "tools/dev/b3-analyze.sh --help" ;;
	esac
done

[ "$(id -u)" != 0 ] || fail "do not run this as root" "run it as yourself: tools/dev/b3-analyze.sh"
[ -f "$LIB" ] || fail "no $LIB (the fork is not built)" "meson compile -C $BUILD"
if [ ! -d "$LOG_DIR" ]; then
	mkdir -p -- "$LOG_DIR"
	chmod 700 -- "$LOG_DIR"
fi
: >"$LOG"
chmod 600 -- "$LOG"
printf '# b3-analyze %s fork %s\n' "$(date -Iseconds)" "$FORK" >>"$LOG"

# 1. the scorer
if [ ! -x "$BIN" ] || [ "$LIB" -nt "$BIN" ] || [ "$SRC" -nt "$BIN" ] || [ "$BUILDER" -nt "$BIN" ]; then
	mkdir -p -- "$(dirname -- "$BIN")"
	"$BUILDER" "$BUILD" "$BIN" >>"$LOG" 2>&1 ||
		fail "building the scorer failed (details: $LOG)" "meson compile -C $BUILD, then rerun tools/dev/b3-analyze.sh"
fi
SHA=$(sha256sum -- "$BIN" | cut -d' ' -f1)
printf 'scorer %s sha256 %s\n' "$BIN" "$SHA" >>"$LOG"

# 2. parity
rc=0
"$PY" "$PARITY_PY" --map "$MAP" --check-data >>"$LOG" 2>&1 || rc=$?
if [ "$rc" = 0 ]; then
	[ "$skip_parity" = 0 ] ||
		fail "--skip-parity is refused: the parity data set is present (local/far-diag, local/diag-fprintd)" \
			"drop --skip-parity: tools/dev/b3-analyze.sh"
	MAP_SHA=$(sha256sum -- "$MAP" | cut -d' ' -f1)
	# reuse key: harness sha256, map.csv sha256; then the recorded verdict
	rec_sha=- rec_map=- rec_exact=-
	read -r rec_sha rec_map rec_exact < <("$PY" -c 'import json,sys
d = json.load(open(sys.argv[1]))
print(d.get("harness_sha256") or "-", d.get("map_csv_sha256") or "-", d.get("exact"))' "$PARITY" 2>/dev/null || true) || true
	if [ "$rec_sha" != "$SHA" ] || [ "$rec_map" != "$MAP_SHA" ]; then
		rc=0
		"$PY" "$PARITY_PY" --map "$MAP" --harness "$BIN" --fork "$FORK" --out "$PARITY" >>"$LOG" 2>&1 || rc=$?
		if [ "$rc" = 1 ]; then
			fail "parity MISMATCH: the scorer does not reproduce fprintd's journal scores; not continuing" \
				"see the mismatches in $LOG and $PARITY"
		elif [ "$rc" != 0 ]; then
			fail "the parity run failed (exit $rc; details: $LOG)" "fix it, then rerun tools/dev/b3-analyze.sh"
		fi
	elif [ "$rec_exact" != True ]; then
		fail "$PARITY records a parity MISMATCH for this scorer; not continuing" \
			"see the mismatches in $PARITY; rm it to rerun the parity check"
	fi
elif [ "$rc" = 4 ]; then
	[ "$skip_parity" = 1 ] ||
		fail "the parity data set is absent (local/far-diag/map.csv, local/diag-fprintd): parity cannot be proven" \
			"rerun with --skip-parity to analyze without the parity proof"
	echo "WARNING: PARITY NOT PROVEN on this machine (--skip-parity: the parity data set is absent)."
	echo "WARNING: the scores below are not checked against fprintd's own."
	echo
else
	fail "the parity data check failed (exit $rc; details: $LOG)" "$PY $PARITY_PY --check-data"
fi

# 3. the analysis
"$PY" "$ANALYZE_PY" --harness "$BIN" --fork "$FORK" --parity "$PARITY" "${args[@]}" 2>>"$LOG" | tee -a "$LOG" ||
	fail "the analysis failed (details: $LOG)" "rerun tools/dev/b3-analyze.sh after fixing it"
