# Ghost diagnosis: finger-off "ghost" probing in synaptics0078 (diagnosis + instrumentation)

No USB access and no sudo during this work. It builds on the A5 change (threshold 105, 18 stages).

## 1. Journal (fprintd, 2026-10-15 00:29–00:33) compared with the A4 logs

The per-probe tables were made with the new `tools/dev/a4_diag.py`, which also reads journal exports.

1. **fprintd's flow is not "enroll stage 1, then stage 2".** fprintd 1.94.5 first runs a duplicate-check identify. `/usr/lib/fprintd` contains "identify for enroll" and "enroll-duplicate".
   - The touch at 00:29:46 was that **identify**. Its finger-off came after 32 probes / 16.4 s. Because it is not an enroll action, the core then deactivated: 04 answered 0x0000 "capture stopped", meaning the sensor was still armed.
   - The **enroll** activated 3 ms later (00:30:02.761). Its first finger event came **336 ms after the 02**. That is a ghost-grid latency (see 2). The real touches came 4.5 s and 77 s after arming.
   - So enroll "stage 1" (00:30:03) was very likely captured from the ghost, not from a touch. Whether a real touch happened at 00:30:03 is unconfirmed.
   - The ghost then ran 81 probes / 44.6 s, and later 60 probes / 29.6 s at stage 3 (00:32:04, a real touch with finger byte 0x05).
2. **The finger-event latency after the 02 reply sits on a ~48 ms scan grid:** 97, 145, 193, 241, 289, 337, 385, 433, 481 ms.
   - A2 and A4 still-down probes were always at the 2nd scan, 93–117 ms.
   - Ghost probes: 120 of 170 at 97 ms; 50 at scans 3–10, up to 481 ms. This looks like a marginal, noisy detection.
   - The probe window is 500 ms. Each "finger off" may therefore be a window that a late ghost just missed, not a real lift.
3. **Ready codes.**
   - Ghosts: 0342040040 ×138; **0360070040 ×28**, after which 51 returned 18 B, status 0x0007, error 0x80000002, no pixels (never seen in A2 or A4); 0320070000 ×4, after which 51 returned 8082 B with error 0x80200000 (also seen in A4).
   - About 80% of ghost probes got a full status-0 image.
   - Finger bytes: 0200401000 ×169, 0200400e00 ×1.
4. **Same driver path and pacing in both.** Each probe takes about 430–650 ms (02 reply about 20 ms, 51 about 8 ms, 250 ms back-off). Nothing in fprintd changes the USB timing.
5. **The A4 script never exercised two things:**
   - the left-armed "Sensor already armed, listening" path. In enroll, stage 3's touch arrived on it after 77 s with no false event, so the idle armed sensor did not ghost;
   - the identify→enroll back-to-back activation.

## 2. Instrumentation in the driver (off by default)

All three variables are read at open.

- **`FP_SYNAPTICS0078_DUMP_DIR`** (dir created 0700, files 0600, written via temp file + rename):
  - Logs "Diag image N <capture|probeNN>: mean, sd, coverage (proto's 8x8/sd>15), gradient, diff vs last capture, ms after 02, file".
  - Writes `<seq>-<monotonic ms>-<tag>[-err].pgm` at native 144x56. The PGM comment carries all 18 header fields.
- **`FP_SYNAPTICS0078_PROBE_WINDOW_MS`** (50..10000) and **`FP_SYNAPTICS0078_PROBE_BACKOFF_MS`** (0..60000). An override is logged; an invalid value is warned about and ignored.
- **Always added to existing debug lines (zero cost):**
  - `(+N ms after 02)` on every EP 0x83 event line;
  - `length` on the "Image reply" line.
- **Checked with a scratch C harness** (not in the tree):
  - stats are identical to `parsers.image_quality` and `mean_abs_diff` at 100% and 50% coverage, and the gradient matches Python;
  - the PGM bytes round-trip, the files are 0600, and the bounds and invalid values are handled.

## 3/4. `tools/dev/a4-capture.sh --diag [--backoff-ms N] [--window-ms N]`

- 6 cycles by default, A4_TIMEOUT_S 180.
- Dumps to `local/diag/<stamp>/` (gitignored via `local/`).
- Afterwards it prints the per-cycle probe table and saves it there as `summary.txt`.
- `tools/dev/a4_diag.py LOG…` also works on `journalctl -u fprintd -o short-precise` exports.

## Temperature (analysis only; nothing changed)

- **Defaults** (fp-device-private.h): `DEFAULT_TEMP_HOT_SECONDS` 180, `DEFAULT_TEMP_COLD_SECONDS` 540. synaptics0078 sets neither, so they apply.
- **The model counts action time**, from the start to the end of an enroll, identify, verify or capture. It does not count the driver's armed or probing state.
- **Why it tripped here:** from COLD, 180 s of continuous actions reach HOT. Identify 21 s + enroll 160 s = 180.9 s, so it went HOT at 00:32:42, exactly on schedule. Cooling back to WARM takes about 205 s (540·ln(0.731/0.5)).
- **Comparable drivers:**
  - every upstream image (non-MOC) driver keeps the default;
  - egismoc sets 0 explicitly (= default);
  - the MOC drivers synaptics, goodixmoc, elanmoc, fpcmoc, focaltech_moc, realtek, mafpmoc and egis_etu905 set −1 (always-on, which also sets FP_DEVICE_FEATURE_ALWAYS_ON).
- **Recommendation:** set `temp_hot_seconds = -1`. The armed sensor waits on its own interrupt, as Windows does indefinitely, and the 18-stage enroll plus the duplicate-check identify must otherwise fit in 180 s. Do it **together with a bound on finger-off probing** (for example, give up after N s). Probing is the one part that really captures continuously, at about 2 images/s.
- **Alternative:** keep the model with a larger hot time, for example 600 s.
- Decided in A5b: `temp_hot_seconds = -1` together with a slow back-off for long still-down probing (see `a5b.md`).

## Changes
- libfprint fork: libfprint/drivers/synaptics0078.c (modified)
- project: tools/dev/a4-capture.sh (modified)
- project: tools/dev/a4_diag.py (new)
- project: tools/test_a4_diag.py (new)

## Tests
- `meson compile -C build-gi`: 0 warnings. Strict scratch build (`-Ddrivers=synaptics0078 --werror`): clean.
- `meson test -C build-gi`: 129 OK, egis_etu905 FAIL (known flake), and 3 TIMEOUTs: aes2550-custom, aes2550-capture and vfs7552.
  - The timeouts come from heavy machine load (load average 26-28). The same tests pass with `--timeout-multiplier 6`, and they do not involve this driver.
- `proto/.venv/bin/python -m pytest -q -p no:cacheprovider proto tools research/a5`: 345 passed, 2 skipped (shellcheck not installed; research/a5 needs numpy). That is the baseline 330 plus 15 new.
- ruff (line length 120): clean. `bash -n`: OK. `--diag --backoff-ms 2000 --dry-run`: rc 0.

## Not verified
- Anything on hardware: dump writes during a real capture, and the effect of the cadence.
- Writing dumps under fprintd's sandbox. The stats are still logged when the directory cannot be written.
- shellcheck and uncrustify are not installed.
- Open question: whether a real touch happened at 00:30:03.
