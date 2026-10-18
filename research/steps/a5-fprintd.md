# A5: enroll and verify with fprintd

Implemented and tested without hardware; regression tests cover each fix. Docs are updated. No sudo or USB access was used; the hardware run is still to be done.

## Decision (Part 1)
- **Upscale stays 2x.**
  - Highest impostor score: 0 at 2x, 6 at 1x.
  - False rejects are lower at every enrolled count k (at k=9 and threshold 105: 6.7% at 2x, 13.3% at 1x).
  - No image fails the 25-keypoint gate at either scale.
- **score_threshold is 105.**
  - 105 is the score of 6 fully consistent matches.
  - Rule: the first whole match count at least 2 matches above the highest impostor seen at either scale (6, about 3.4 matches).
  - It is 17.5x the impostor maximum, with 0 false accepts at both scales.
  - FRR cost against threshold 7: +2.9 points at k=5, +1.9 at k=7, 0 at k=9.
- **nr_enroll_stages stays 18.**
  - FRR at 2x/105 falls 27.8% → 14.4% → 6.7% for k = 5 → 7 → 9 (all captures from one sitting). Estimate at 18 is ≤ ~10.7%.
  - A rejected verify takes 14–44 ms median.
  - A print is about 2.6 MiB.
- **Before this change** the driver set neither value, so the core defaults applied: 5 stages and threshold 40.
- **Cross-check against libfprint's own path.** benchmark-matching.py `--smoke` on build-gi (virtual_image, dev.verify, fpi_print_sigfm_match) gave scores identical to the harness's. A serialize/deserialize round trip changed 0 of 1521 scores. Limit: no trial landed near the threshold, so the decision column only confirms the plumbing.
- **Caveat.** One person, 3 fingers. There are about 300 effective impostor pairs, which bounds the false-accept rate only to about 1% per pair (95%), or about 16.6% per 18-stage verify. The rate multiplies further with enrolled fingers and PAM retries. The margin is the only protection beyond the observed data. B3 is still owed before relying on this for sudo/polkit.
- **The 9 unlabeled A4 captures:** 7 clear 105 against left-index only, 2 match no finger, and none clears two fingers.

## Part 2
- **MDWE: PASS, no escalation.**
  - Static check over the lib's ldd closure (43 libs) and /usr/lib/fprintd: no RWE stack, no TEXTREL, no W+X LOAD segment.
  - A SIGFM workload through the fork's `fp_image_extract_sigfm_info` gave identical scores under two wrappers: prctl PR_SET_MDWE, and `systemd-run --user` with MDWE, NoNewPrivileges and @system-service.
  - A control program's W+X mmap/mprotect was refused under both wrappers, so the filter was really on.
  - Side note: the NVIDIA GLX libs try RWX only when `__GLX_VENDOR_LIBRARY_NAME=nvidia` is set. MDWE refuses it and they carry on. fprintd's environment does not have that variable.
- **The drop-in** `/etc/systemd/system/fprintd.service.d/libfprint-0078-dev.conf` sets three variables:
  - `LD_LIBRARY_PATH=/usr/local/lib/libfprint-0078-dev`;
  - `OPENCV_OPENCL_RUNTIME=disabled`;
  - `G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print`, so the module re-upload and scores reach the journal. The journal then also holds the serial; the README says so.
  - It changes no sandbox directive.
- **FP_DRIVERS_ALLOWLIST is not set:** 06cb:0078 was the only reader on the test laptop that the fork's id tables match.
- **install also:**
  - copies the lib, staged as 0600 and checked on the copy, to `/usr/local/lib/libfprint-0078-dev/` (root, 0755/0644), with a manifest `a5-install.manifest`;
  - copies the data files to `/var/lib/fprint/synaptics0078/` (0700/0600), refusing to overwrite a differing file;
  - runs daemon-reload and try-restart.
- **undo:**
  - removes only what the manifest lists (canonical exact targets, root-owned manifest);
  - asks y/N before deleting `/var/lib/fprint/<user>/synaptics0078/` prints;
  - can be rerun.
- **fprintd lists the data dir as a "user" with no prints.** Harmless; undo skips it.
- **Journal vacuum (README):** `--vacuum-time=1h` after `--rotate` keeps the A5 lines; hence `--rotate --vacuum-time=1s`, with a warning that it deletes the whole system journal.

## Residual nits, not fixed
1. a5-fprintd-dev.sh:459/584-586/679. When install created /var/lib/fprint itself and the prints are kept, every undo exits 1 and keeps the lib dir. It does not apply when /var/lib/fprint already existed.
2. :246. The trap does not cover the manifest's mktemp file; an interrupt there leaves a `.a5-install.manifest.*` file behind.
3. :412/489. install silently overwrites a recorded drop-in that has been edited.

## Fork (`../libfprint`, branch sigfm-0078, on 0f66107d)
- libfprint/drivers/synaptics0078.c (modified)

Change: SIGFM threshold 105 and 18 enroll stages. Bump: none (meson version stays 1.94.100, per D8).

## Project
- tools/dev/a5-fprintd-dev.sh (new, 0755)
- tools/test_a5_fprintd_dev.py (new)
- research/a5/sigfm_matrix.cpp (new)
- research/a5/build_harness.sh (new, 0755)
- research/a5/a5_threshold.py (new, 0755)
- research/a5/test_a5_threshold.py (new)
- research/steps/a5-threshold.md (new)
- README.md (modified)
- docs/brief-libfprint-driver.md (modified)

Change: A5 fprintd dev setup, SIGFM threshold analysis, brief A5 status. Bump: none (unversioned).

## Tests
- **Fork:**
  - `meson compile -C build` and `-C build-gi`: 0 warnings.
  - Strict scratch build (`-Ddrivers=synaptics0078 --werror`): 0 warnings.
  - `meson test -C build`: 8 OK / 0 fail / 33 skip.
  - `meson test -C build-gi`: 132/133. The one failure is egis_etu905, the known upstream flake.
  - The lib contains `/var/lib/fprint/synaptics0078`.
- **Project:**
  - `proto/.venv/bin/python -m pytest -q -p no:cacheprovider proto tools`: 330 passed, 1 skipped. The skip is shellcheck, which is not installed.
  - `python3 -m pytest -q research/a5`: 16 passed. proto/.venv has no numpy, so the module skips there.
  - ruff at 120: clean. `bash -n`: OK.
  - `a5-fprintd-dev.sh install --dry-run` against the real build: rc 0.

## Not verified
- A real root install, status and undo, including systemd parsing the drop-in.
- fprintd under its full sandbox (DeviceAllow and ProtectSystem cannot be reproduced in a user unit).
- On hardware: the 18-stage enroll, 20+ genuine and 20+ impostor verifies, suspend/resume module re-upload, and the finger-off delays.
- shellcheck and uncrustify (neither is installed).
- The manifest-ownership refusal for a foreign owner (needs root).

## Commands
- `sudo tools/dev/a5-fprintd-dev.sh install` (from the project root; preview with `--dry-run`).
- `tools/dev/a5-fprintd-dev.sh status`
- Then follow the README A5 guide.
- `sudo tools/dev/a5-fprintd-dev.sh undo` (asks before deleting this driver's prints).
