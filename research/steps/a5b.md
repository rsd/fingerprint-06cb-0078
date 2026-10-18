# A5b: robust finger-off (D5 v2) and clear enroll/verify helpers

Implemented and tested without hardware; each fix has a regression test. The docs are updated. No sudo, USB access, fprintd client or service restart was used; the hardware run is described below.

## Decisions
- **New-touch threshold: `SYNAPTICS0078_NEW_TOUCH_DIFF` = 30.** This is the mean absolute pixel difference on the native 144x56 image, measured against the last capture handed to the core.
  - The same touch held still measured 3.6–16.5; a new touch measured 48–54, even when aimed at the same spot.
  - 30 sits 13.5 above the highest same-touch value and 18 below the lowest new-touch value. The geometric midpoint is about 28.
- **Finger-off probing:**
  - A probe image with diff ≥ 30 means the finger was lifted and placed again. The driver disarms (04/52) and reports finger off without a pause, and the next arm captures normally.
  - No finger event in the window still means finger off; the sensor is left armed.
  - After 30 s of continuous still-down probing (`SYNAPTICS0078_PROBE_SLOW_AFTER_MS`), the driver probes every ≥ 1 s (`SYNAPTICS0078_PROBE_SLOW_BACKOFF_MS`).
- **Same-touch guard (AWAIT_FINGER_ON):**
  - A capture with diff < 30 is not submitted. The same capture run waits for finger off.
  - A new touch seen during that wait becomes the capture.
  - A lift seen during that wait lets the next touch through without the check. So the guard costs at most one extra lift and touch, and never locks the tester out.
- **Reference-image policy:**
  - The reference is the last image handed to the core. It is replaced only when an image is delivered.
  - It survives deactivate/activate and close/open for the life of the fprintd process (it is freed at finalize). This covers fprintd's identify-then-enroll activations, 3 ms apart, and back-to-back verifies.
  - It is kept in memory only, never written to disk except by the opt-in dump.
  - After a retry it is marked stale. While stale, neither the new-touch rule nor the guard compares against it, because comparing with an older touch can only misfire.
  - A time-based expiry was rejected: a stale reference costs at most one extra touch.
- **`temp_hot_seconds = -1`:**
  - Why: Windows leaves the sensor armed indefinitely, and the 18-stage enroll plus the duplicate check tripped the 180 s simulated overheat (at 180.9 s on 2026-10-15). Probing churn is now bounded by the slow back-off.
  - `fpi_device_class_auto_initialize_features` adds ALWAYS_ON, as the MOC drivers get it. It is informational: nothing in the core reads it.
- **Driver log-line contract:** the helpers parse these lines (L1–L6). The contract is at the end of this file.

## Defects found in review
The log-line contract was fixed up front so that the driver, the helpers and the installer could be built independently. Findings per part:
  - Driver: 3 should-fix, all fixed and re-checked clean.
  - Installer: 2 should-fix and 4 nits, all fixed and re-checked clean, with 3 residual nits listed below.
  - Helpers: 3 should-fix and 5 nits, all fixed. Each fix has a regression test that fails when the fix is reverted, and the helpers touch no documented invariant.
- **Docs:** the README A5 guide, and the brief's D5 v2, A5 status and open questions. The brief's old A4 finger-off bullet is marked superseded.
- There is no UI beyond terminal prompts, which are plain text strings covered by tests.

## Residual nits, not fixed
- **Installer:**
  - Without `--diag`, `diag_mode_fix` does no owner check. It only touches the recorded path.
  - A run whose only action is a chmod says "nothing changed".
  - An upgrade interrupted between moving the lib into place and saving the manifest makes the next install refuse with "replaced since install". The message tells how to recover.
  - The old nit 1 is still open: undo exits 1 when install created /var/lib/fprint and the prints are kept. It does not apply when /var/lib/fprint already existed.
- **Driver:**
  - The debug dump logs its diff even when the reference is stale.
  - `guard_bypass` is reset per run but not cleared when used. It is correct as the code stands.
  - A touch thrown away by a session error or a deactivation does not mark the reference stale. This is minor.

## Fork (`../libfprint`, branch sigfm-0078, on 7b1ddbcd)
- libfprint/drivers/synaptics0078.c (modified)

Change: finger-off v2 (new-touch rule, same-touch guard, slow back-off), temp_hot_seconds -1. Bump: none (meson version stays, per D8).

## Project
- tools/dev/a5_session.py (new, 0644)
- tools/dev/a5-enroll.sh (new, 0755)
- tools/dev/a5-verify.sh (new, 0755)
- tools/dev/a5-session-lib.sh (new, 0644)
- tools/test_a5_session.py (new)
- tools/dev/a4-capture.sh (modified)
- tools/dev/a4_capture.py (modified)
- tools/test_a4_capture.py (modified)
- tools/dev/a5-fprintd-dev.sh (modified)
- tools/test_a5_fprintd_dev.py (modified)
- README.md (modified)
- docs/brief-libfprint-driver.md (modified)
- research/steps/a5b.md (new: this note)

Change: A5b enroll/verify helpers, installer upgrade and --diag, quieter a4-capture, docs for D5 v2. Bump: none (unversioned).

## Tests
- **Fork:**
  - `meson compile -C build` and `-C build-gi`: 0 warnings. `ninja -n` shows both are current with the final source.
  - Strict scratch build (`-Ddrivers=synaptics0078 --werror`): clean.
  - Scratch harness for `image_diff`, `is_new_touch` (29.9 counts as the same touch, 30.0 as new) and `probe_pause_ms`: passes. A deliberately broken copy fails 6 checks.
  - `meson test -C build`: 8 OK, 33 skipped. There is no umockdev test for synaptics0078.
  - The full build-gi suite was not run for this change.
  - `strings build/.../libfprint-2.so.2.0.0` contains the new lines.
- **Project:**
  - `proto/.venv/bin/python -m pytest -q -p no:cacheprovider tools/test_a5_session.py tools/test_a4_capture.py tools/test_a4_diag.py tools/test_a5_fprintd_dev.py`: 275 passed, 1 skipped (shellcheck is not installed).
  - `ruff check --line-length 120 tools/`: clean. `bash -n` on every tools/dev/*.sh: OK.
- **Installer upgrade check:** the manifest of an existing dev install (`/usr/local/lib/libfprint-0078-dev/a5-install.manifest`) records the lib's sha, which equalled the installed lib. So the new "lib replaced by hand" refusal does not block a normal upgrade. The test suite also runs an upgrade over an install made by the previous version of the script.

## Sandbox (--diag)
- fprintd.service has no User= and runs as root.
- `StateDirectory=fprint` (/var/lib/fprint, 0700) is writable under `ProtectSystem=strict`.
- /var/lib/fprint/synaptics0078-diag is inside it, so the driver can write its dumps there. No unit change is needed.

## Not verified
- Anything on hardware or against a live fprintd: the guard, the new-touch rule and the slow back-off; the shapes of the prompts' stdout lines (inferred from the client binaries' strings); and that fprintd's duplicate check prints nothing.
- A real root install, upgrade, --diag or undo; systemd parsing the new Environment= line; dumps written under the real sandbox.
- shellcheck and uncrustify (neither is installed).

## Hardware run sequence (from the project root)
1. `sudo tools/dev/a5-fprintd-dev.sh install --diag`
   - It asks "--diag stores raw fingerprint images ... Continue? [y/N]". Answer y.
   - It prints "upgraded: lib sha <old>... -> <new>..., diag dir, drop-in rewritten (diag on); fprintd restarted".
2. `tools/dev/a5-fprintd-dev.sh status` (optional): shows "diag ...: on".
3. `tools/dev/a5-enroll.sh`
   - "Checking the setup... ok". If the finger is already enrolled, it offers "Delete it and enroll again? [y/N]". A password dialog may appear.
   - "First touch: place your finger. fprintd first checks that this finger is not enrolled yet (this touch is not one of the 18)"
   - "Got it. LIFT your finger now", then "Lifted. Touch again"
   - "Touch 1 of 18: place your finger (vary the position a little)", repeated through 18.
   - Ends with "Done: right-index-finger is enrolled (18 of 18 touches). Next: ...".
   - Only if they apply: "That touch did not count: <reason>", "The sensor still sees the last touch: lift your finger, then touch again", "Still detecting a finger; if your finger is off, just touch again".
4. Verify, 10 rounds per finger:
   - `tools/dev/a5-verify.sh --finger-used right-index --expect match`
   - then `--finger-used right-middle --expect no-match`
   - then `--finger-used left-index --expect no-match`
   - Each round shows "Round 3/10: touch with your RIGHT MIDDLE finger, then lift", "Got it. LIFT your finger now", then "Round 3/10: no match (as expected)".
   - At the end, a table (match, no match, retries, errors) and "as expected: X/10".
5. Suspend/resume:
   - `systemctl suspend`, wake the laptop, then `tools/dev/a5-verify.sh --finger-used right-index --expect match --rounds 3`.
   - `grep -h Activation local/logs/a5-verify-*.log | tail -2` shows "re-uploading" or "already loaded".
6. Note whether any "Still detecting a finger" message appeared.
7. Later: `sudo tools/dev/a5-fprintd-dev.sh install` (no `--diag`) to stop the dumps, or `sudo tools/dev/a5-fprintd-dev.sh undo`, which asks before deleting prints and diag images.

## Log-line contract (driver → helpers)
- L1: "Finger off after %u probe(s), %ld ms: no finger event"
- L2: "Finger off after %u probe(s), %ld ms: new touch (diff %.1f >= %d)"
- L3: "Same touch as the last capture (diff %.1f < %d), not submitted; waiting for finger off"
- L4: "Finger-off probe %u: same touch (diff %.1f < %d)"
- L5: "Finger still down for %u s, probing every %u ms from now on"
- L6: "Image captured (diff vs last capture %s)"
- Extra lines: "Lift seen during the guard wait, accepting the next touch without the same-touch check"; "Same-touch check skipped: ..."; "Finger-off probe %u: reference image is stale (a retry since), image not compared"; "Finger-off probe %u: new touch (diff %.1f >= %d), taking this image as the capture".

## Late review of the helpers

The review gave 152 tests passing and no must-fix findings. The real serial appears in none of the tests or fixtures.

Should-fix:
1. `tools/dev/a5_session.py:349-358`: the "fprintd skipped its duplicate check" branch misfires when a real check is followed by a quick first enroll touch and the journal lags. The tester then sees "Touch 2 of 18" while the finger is still down. Fix: treat the enroll's `Activation:` line after `check_off_at` as proof that the check happened.
2. `a5_session.py:544-548`: in `end_round`, an overheat line in the same round turns an already recorded match or no-match into an error. Overwrite only when `cur.outcome is None`; still set `abort`.
3. `tools/test_a5_session.py` has these gaps:
   - no test of the guard wait handing a new touch over directly;
   - the four new driver lines are not in `test_journal_kind` as "other";
   - no Ctrl-C cleanup test;
   - no test for `MSG_JOURNAL_GONE`.

Nits:
- `a5_session.py:472-508`: verify ignores "Capture failed" lines, so the LIFT prompt waits for the stdout retry.
- `a5_session.py:527`: the remove-and-retry message repeats itself.
- `a5_session.py:380`: a late stage result after the forced settle shows a spurious LIFT.
- `a5_session.py:296`: after an L3 "same touch" line, holding the finger never brings the still-down note.
- `a5_session.py:687/735`: a missing fprintd client binary prints a traceback.

**Fixed (rework after the wrong-finger enroll).** Most items were already fixed and tested. The remaining ones are now fixed with tests: should-fix 1 when the enroll's Activation reaches the journal after the stage result (the doubtful result now waits for the Activation, a touch line or 1.5 s); a missing fprintd client now fails in the preflight with one plain line. The same rework makes the enroll finger a required argument named in every prompt, adds `--replace`, and makes a5-verify.sh refuse a finger mix-up.

## Hardware result: enroll (2026-10-15 12:00–12:02)

- `tools/dev/a5-enroll.sh`: 18/18 stages, 0 retries, in about 1.5 min.
- All 18 lifts were detected as "no finger event", 1.8–2.9 s after each image (4–6 probes).
- No ghost occurred; the new-touch rule (diff ≥ 30) never fired.
- Same-touch diffs while the finger was held reached 23.6 (earlier data: up to 16.5), so the margin below 30 is narrower than measured before. A new touch is ≥ 48. Consider 35–36 (the midpoint) when tuning.
