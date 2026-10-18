# B3: false-accept benchmark, build notes

Capture tool, offline analysis and their review. Results of the first session are in `b3-results.md`.

## Scoping
- Baseline: fork HEAD 40cda345 = package libfprint-sigfm-0078 1.94.100.r23.g40cda34-1.
- Reader access as an ordinary user comes from the udev rule with `TAG+="uaccess"`. The system typelib FPrint-2.0 loads in the system python3 (gi). The packaged data dir `/var/lib/fprint/synaptics0078` is root-only; the driver honours FP_SYNAPTICS0078_DATA_DIR, and a `local/synaptics0078` directory (module.bin, program-<serial>.bin, mode 0600) is used instead.
- Scorer: `research/a5/sigfm_matrix.cpp` (the A5 harness, linking the fork's `build/libfprint/sigfm/libsigfm.a`), proven 576/576 identical to `far_replay` in `far-diag.md`. B3 re-proves it against the journal scores directly (`local/far-diag/map.csv` `scores` plus the fprintd diag dumps).
- Two independent parts: A = capture (`tools/dev/b3_capture.py`, `tools/dev/b3-capture.sh`, `tools/test_b3_capture.py`); B = parity, trials and wrapper (`research/b3/*`, `tools/dev/b3-analyze.sh`). The README B3 section was written after review.

## Part A: capture
- Files: tools/dev/b3_capture.py, tools/dev/b3-capture.sh, tools/test_b3_capture.py (all new).
- Tests: `proto/.venv/bin/python -m pytest -q -p no:cacheprovider tools/test_b3_capture.py`: 70 passed. bash -n OK; shellcheck not installed; ruff clean.
- Path: the wrapper unsets LD_LIBRARY_PATH/GI_TYPELIB_PATH, so it uses the system python3, the /usr/lib typelib and /usr/lib/libfprint-2.so.2. It sets FP_DRIVERS_ALLOWLIST, FP_SYNAPTICS0078_DATA_DIR=local/synaptics0078 (the driver's open reads module.bin and the program every time) and G_MESSAGES_DEBUG=all, and writes the log. Busy: GUsb BUSY=10 / FpDeviceError.BUSY / "Resource busy"; permission: PERMISSION_DENIED=9 / "Access denied".
- `a4_capture.py` works unchanged with the system lib (GI import only, flag values match); `a4-capture.sh` does not (it requires the fork's build-gi and refuses while fprintd runs).
- Added beyond the spec: `--dry-run` (checks only). Time: about 3 s per touch + 3 s per finger, about 11 min for 10x20 (from the A4 logs, 1.2-3.0 s cycles).
- Not verified: hardware; whether the package delivers the 2x image (any other size makes every touch "touch again", with the reason in the log); the sidecar stats lack a gradient (`parsers.image_quality` has none).

## Part B: parity and analysis
- PARITY EXACT: 32 captures (30 verify rounds + 2 unlabeled), 219 stage scores, early-exit lengths and decisions all equal to the journal. Stage order `map.csv` == `stages.tsv`. Harness sha256 88dbc9a3… (byte-identical to the far-diag harness); fork 40cda345 clean; package g40cda34.
- Files: research/b3/b3_common.py, b3_parity.py, b3_analyze.py, test_b3.py, tools/dev/b3-analyze.sh (all new).
- Tests: `python3 -m pytest -q -p no:cacheprovider research/b3`: 33 passed (the scipy Clopper-Pearson comparison ran). ruff clean. bash -n OK.
- Gate-rejected images (< 25 keypoints) count as failed captures; they are excluded and counted.
- Deviation: A5's `margin_threshold(0)` = 1 differs from the inversion s=0 -> 15 used in the analysis; the analysis uses the latter (more conservative). Open technical question.
- Design call: "lowering below 105 supported" only if the D6 rule allows it and no extra finger pair accepts at the lower threshold.
- Timing: about 0.9 ms per non-matching comparison; 2,500 comparisons take 28.5 s wall; 200 images take about 3-8 min once, then are cached.
- Smoke: proto 3x10 with `--stages 5 --k 3` gave rc 0 (plumbing only).
- Not verified: package compiler flags vs the fork build; the 18-stage path only ran on synthetic data.

Combined rerun: tools/test_b3_capture.py 70 passed; research/b3 33 passed. The sidecar schema (session/person/label) matches between the two parts.

## Review findings
- BLOCKER `b3-capture.sh:188` / `b3_capture.py:625`: the GLib 2.88 default log writer sends DEBUG/INFO to stdout; with G_MESSAGES_DEBUG=all the driver debug output reached the terminal. Fix: `GLib.log_writer_default_set_use_stderr(True)` in `GiReader.setup()` before `FPrint.Context()`.
- should-fix `b3_parity.py:86`: vacuous parity on zero captures. Fix: require n > 0, expected 32/219, and `map_csv_sha256` in the reuse key.
- should-fix `b3_analyze.py:353/:396`: the "no extra pair accepts" condition was always true (dead branch). Fix: no invented numeric target; the verdict is what the D6 rule allows plus the evidence scope (identities, persons, pairs, (c) bound, FRR gain), with the one-person caveat.
- nits: reserved `--person` names; `allow_abbrev=False`; soften the "honest" wording for level (c); runnable permission hint.
- Nothing found in: Clopper-Pearson math, (b)/(c) counts, held-out, seed, gate-rejected, cross-day, D6 inversion, cache keying, resume/atomic writes, modes, guard, decimation, error codes, privacy; `--dry-run` cannot open the reader.

## Fixes
- Capture: `route_glib_log_to_stderr()` runs first in `GiReader.setup()` (`log_writer_default_set_use_stderr`; old-GLib fallback via `log_set_writer_func`). Tests: an order check with fakes, the fallback, and a real system-python GI test with G_MESSAGES_DEBUG=all asserting a clean stdout (DEBUG reached stdout before the fix). Reserved person names results/bin/logs are refused (the set lives in `research/b3/b3_analyze.py:62`; a test checks both sets match). `allow_abbrev=False` (tests, including through the wrapper). Permission hint: `getfacl <node>`, re-plug / re-login, udevadm reload+trigger; the wrapper hint is aligned. `tools/test_b3_capture.py`: 85 passed, 0 skipped. bash -n OK, ruff clean. Not verified: old-GLib fallback only on fakes; the routing was not seen with real driver messages.
- Analysis: parity re-run still EXACT 32/219; `parity.json` now has `map_csv_sha256` and the expected 32/219; harness sha unchanged. Empty or short data is "not exact" (no-captures / nothing-compared / size); `select()` raises on empty. The wrapper reuse key is the harness sha AND the map.csv sha. Lowering verdict: the dead condition is gone; the JSON has `d6_rule_allows_lowering` plus scope (identities, persons, pairs, (c) bound at rec and 105, one_person, FRR gain); one-person caveat line; level (c) wording softened. `research/b3`: 41 passed.
- Verification after the fixes: tools/test_b3_capture.py 85 passed; research/b3 41 passed. `tools/dev/b3-analyze.sh` end to end on empty `local/b3`: rc 0, prints only "No B3 samples … Capture first: tools/dev/b3-capture.sh, then rerun tools/dev/b3-analyze.sh." (parity reused from parity.json).
- Not re-reviewed: each fix is covered by new tests (the blocker by a real system-GI test with G_MESSAGES_DEBUG=all asserting a clean stdout).

## Changes
- README.md (modified: "## False-accept benchmark (B3)")
- research/b3/b3_common.py, research/b3/b3_parity.py, research/b3/b3_analyze.py, research/b3/test_b3.py (new)
- tools/dev/b3_capture.py, tools/dev/b3-capture.sh (mode 0755), tools/dev/b3-analyze.sh, tools/test_b3_capture.py (new)

Change: B3 guided capture session over the system libfprint and an offline false-accept benchmark (parity-proven SIGFM scorer).

Tests: `proto/.venv/bin/python -m pytest -q -p no:cacheprovider tools/test_b3_capture.py`: 85 passed; `python3 -m pytest -q -p no:cacheprovider research/b3`: 41 passed; the wrapper end to end on empty data: rc 0. The full suite (`proto tools`, baseline 786 passed / 1 skipped) was not run for this step.

Parity: EXACT, 32 captures / 219 stage scores / early-exit lengths / decisions vs the 2026-10-15 journal; harness sha256 88dbc9a3…, fork 40cda345 = package g40cda34.

Usage: `tools/dev/b3-capture.sh [--fingers …] [--touches N] [--person NAME] [--dry-run]`; `tools/dev/b3-analyze.sh [--k 5] [--seed 0]`.

Not verified: any hardware capture; the package's delivered image size (non-2x makes every touch "touch again"); the package's compiler flags vs the fork build (parity proven against the fork's libsigfm.a); the old-GLib log fallback (fakes only); the 18-stage analysis path on real data; shellcheck is absent.
