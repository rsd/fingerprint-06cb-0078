# Brief: libfprint driver for Synaptics 06cb:0078

Written 2026-10-14 and updated as the steps landed. The decisions below were final when written; the
status notes record later changes. Each step below was built and checked on its own.

Sources: `research/recon/recon-{driver,packaging,sigfm}.md` (cited below as [DRV], [PKG], [SIGFM]),
`research/calibration-analysis.md` [CAL], `research/matcher-evaluation.md` [MATCH],
`research/support-search.md` [SUP], and the working prototype in `proto/`.

## Goal & why

- **Now:** the fingerprint reader of this Clevo P870KM_KM1 (Synaptics 06cb:0078) works with fprintd on Omarchy:
  - enroll and verify;
  - sudo and polkit;
  - the lock screen;
  - Omarchy's own "Setup Fingerprint" flow, unchanged.
- **Eventually:** any 06cb:0078 owner can install it on Omarchy without Windows, and Omarchy offers it.
- **Why a fork:**
  - Upstream libfprint matches with NBIS, and NBIS cannot match a 144x56 image (FRR 100% [MATCH]).
  - The keypoint matcher SIGFM works on our images, with no false accepts observed [MATCH, SIGFM §4.3], but it is not merged upstream (MR !530).
  - So we ship libfprint + SIGFM + our driver as a drop-in replacement for Omarchy's `libfprint-git`.

## Non-goals (this brief's build work)

- Implementing Phase B (no-Windows provisioning) and Phase C (publishing and Omarchy inclusion). They are outlined below so Phase A does not block them.
- The sibling sensor 06cb:00a8.
- Windows Hello coexistence; breaking it is accepted on this laptop.
- Upstream libfprint submission.
- Rebuilding fprintd.

## Decisions

**D1 Code layout.**
- A new git repo `~/Devel/Linux/Synaptics/libfprint`, cloned from `https://gitlab.freedesktop.org/libfprint/libfprint.git`.
- Branch `sigfm-0078`, started at `6f9479c3d55f847c1b3769f28ceb99227f9858cf`. This is Omarchy's `libfprint-git` pin and the current upstream master (`v1.94.100-10`) [PKG §2, SIGFM §1].
- On top, in order:
  1. ssubbotin's SIGFM series: the 8 commits `d258eb51^..7263fb2c` from `https://gitlab.freedesktop.org/ssubbotin/libfprint`, branch `sigfm-benchmark-fpc1022`, without the FPC1022 driver commit `19f4bb5b` [SIGFM §5];
  2. gotenksIN's `094b65d0` from `https://github.com/gotenksIN/libfprint-fpc-disum`;
  3. our driver.
- Rebase whenever Omarchy moves its pin.
- This project, `fingerprint-06cb-0078`, keeps the research, the prototype, the private data, `tools/` and `packaging/`.
- Rejected:
  - Patch files applied at build time: clumsy while developing. Packaging can still generate patches from the branch.
  - The `v1.94.100` tag: Omarchy builds master+10.
- Note: the scratch branch `sigfm-ssb-series` from recon was not kept. A1 recreated the series on `sigfm-0078`.

**D2 SIGFM source.**
- The ssubbotin series. Its `sigfm.cpp` carries the comparator fix: dedup key (p1.y, p1.x, p2.y, p2.x), capped at the 176 strongest correspondences.
- Plus gotenksIN `094b65d0` ("device: Validate gallery equality only for raw prints"). Without it, `fpi_device_verify_complete`/`identify_complete` call `fp_print_equal` on SIGFM prints, which logs a spurious warning and drops the scanned print [SIGFM §2.5].
- Not taken: gotenksIN `406c10e1` hardening. Its `max_angles = 500` cap changes scores; re-evaluate it later.
- The series keeps OpenCV optional (`HAVE_SIGFM`) and doctest optional, and adds a per-driver `'sigfm': true` key in `drivers_info`.
- Its driver API is identical to !530: `img_class->algorithm = FPI_DEVICE_ALGO_SIGFM`, `img_class->score_threshold`. Track !530 upstream.
- The OpenCV dependency (5.0.0 in Arch at the time) is accepted.
- Rejected: MR !530 as-is and Jenium-lab's Omarchy build. Both have the buggy comparator that drops ~80% of correspondences. !530 also hard-requires doctest and OpenCV.
- Kept in reserve: lbssousa's OpenCV-free C rewrite [SIGFM §5], should OpenCV block Phase C.

**D3 Driver `synaptics0078`.**
- An `FpImageDevice`; template `drivers/vfs7552.c`; idioms from nb1010.c, aes3k.c and uru4000.c [DRV §1, §2].
- `dev_class->id = "synaptics0078"`, which equals the driver key and the GType `fpi_device_synaptics0078`.
- Id table `{0x06cb, 0x0078}`; `scan_type = FP_SCAN_TYPE_PRESS`.
- Files touched: `meson.build` `drivers_info` (with `'sigfm': true`), `libfprint/meson.build` `driver_sources`, `tests/meson.build` `drivers_tests`, and `libfprint/drivers/synaptics0078.c`.
- `data/autosuspend.hwdb` is regenerated with `meson compile -C build sync-udev-hwdb`, never hand-edited.
- **Open** (SSM, nothing is written to the sensor before these checks pass):
  - `01`: ROM must be 6.20. The serial is RomInfo bytes 0x12–0x17 with each byte's nibbles swapped.
  - `19`.
  - `75`: sensor type 0x2449.
  - Load and validate `module.bin` and `program-<serial>.bin`.
- **Activate:**
  - Re-read RomInfo. Send `06` + module only when byte 17 == 0, at every activation, because USB reset or resume wipes the RAM-resident module [DRV §3.3].
  - Never resend `06` when it is already loaded (the reply would be 0x04aa).
- **Capture:**
  - `02` + program.
  - Wait on EP 0x83 with `fpi_usb_transfer_fill_interrupt` (not `fill_bulk`, or umockdev replay breaks [DRV §4.3]): finger `02…`, then ready `0342040040`.
  - `51 00 20 00 00`, which returns 8082 B: an 18-byte header plus the 144x56 8-bit image.
  - Clean up with `04` and `52` on every exit path after `02`.
- **Errors:**
  - Status 0x0007 or header error 2 → `fpi_image_device_retry_scan (dev, FP_DEVICE_RETRY_GENERAL)`.
  - 0x0412 on `04` is benign.
  - Wrong ROM or sensor type → `FP_DEVICE_ERROR_NOT_SUPPORTED`.
  - Missing file → `FP_DEVICE_ERROR_GENERAL`, with the path in the message.
  - Size or hash mismatch → `FP_DEVICE_ERROR_DATA_INVALID` [DRV §3.5].
- No hard-coded serial: any 0078 unit works, given its files.
- The image goes to SIGFM as plain 8-bit grayscale; SIGFM ignores FpiImageFlags, so any upscale or inversion happens in the driver [SIGFM §2.2].

**D4 Data files** live in `/var/lib/fprint/synaptics0078/`:
- `module.bin`: 10500 B, from the DLL at offset 0x19cca0, sha256 `67f4a332…caacc1` (`proto/capture.py` MODULE_SHA256).
- `program-<serial>.bin`: 18869 B.
- The default path is a meson-configured string. The env override `FP_SYNAPTICS0078_DATA_DIR` is honoured so that tests, and development runs as the user, can point elsewhere. Only root controls fprintd's environment, so the override grants nothing.
- A setup tool writes these files as root, mode 0600:
  - Python, stdlib only, at `fingerprint-06cb-0078/tools/`.
  - It reuses `proto/calibration.py` (`build_cmd02_from_calibration_data`) and `research/calib/build_ours.py`.
  - Phase A inputs: the Windows DLL, the DPAPI-decrypted CalibrationData, and the fixed-part template `proto/data/cmd02_template.bin`, pinned by sha256.
- Why this place and this split:
  - fprintd runs with `ProtectSystem=strict` and `ProtectHome=true`, so it can't see `/home`, and `/var/lib/fprint` is its own `StateDirectory` [DRV §3.3].
  - The blobs never ship in a package.
  - Phase B changes only the tool, never the driver.
- Rejected:
  - Embedding the blobs, which is illegal to redistribute and fails upstream's HACKING.md.
  - Porting the calibration builder to C now.
  - Putting the module under `/usr/share`: more moving parts for no gain yet.

**D5 v2 Finger lift.** A2 (`research/steps/a2-fingerlift.md`) showed no finger-up event exists. A4 added re-arm probing (`02`, 500 ms window, discard the image, `04`/`52`, 250 ms pause, probe again). Hardware logs and the fprintd "ghost" diagnosis (`research/steps/ghost-diag.md`) led to v2 (A5b):
- **New-touch rule:** a finger-off probe image is compared with the last delivered capture. A mean absolute difference of at least `SYNAPTICS0078_NEW_TOUCH_DIFF` (30) is a new touch: the finger was lifted and placed again. Then `04`/`52`, finger off at once, and the next arm captures normally.
  - Data: the same touch held still differs by 3.6–16.5. A new touch differs by 48–54.
- **No finger event in the window** still means finger off. The sensor stays armed.
- **Same-touch guard** (in AWAIT_FINGER_ON): a captured image with a difference below 30 is not submitted. The capture waits for finger off in the same run.
  - A new touch seen during that wait is taken as the capture.
  - A lift seen during the wait lets the next touch through without the check. The cost is at most one extra lift and touch.
- **Reference policy:** the reference is the last image handed to the core.
  - It survives deactivate/activate and close/open within the fprintd process.
  - It is never written to disk.
  - After a retry it is marked stale. Then neither the new-touch rule nor the guard compares against it.
- **Slow back-off:** after 30 s of "still down", the probe pause grows to 1 s (`SYNAPTICS0078_PROBE_SLOW_BACKOFF_MS`). This bounds the image churn.
- **Temperature:** `temp_hot_seconds = -1`, plus ALWAYS_ON via `fpi_device_class_auto_initialize_features`.
  - Why: Windows idles armed indefinitely. The 18-stage enroll plus fprintd's duplicate-check identify tripped the 180 s simulated overheat on 2026-10-15. The one continuous-imaging part, probing, is now bounded.
- Rejected:
  - A larger hot time (600 s) instead of -1: it only moves the limit.
  - A time-based expiry of the reference: a stale reference costs at most one extra touch, so expiry added nothing.
  - Discarding the guard-wait probe image and re-arming: the re-check could fail a borderline new touch.
  - Reporting finger-off right after the image: a resting finger would produce duplicate enroll stages [DRV §2.3].
- The timed-gap fallback (and the near-duplicate rejection) is dropped.
- Still open: whether the 500/250 ms delays and the threshold 30 hold in real fprintd use (A5 hardware run).

**D6 Enrollment and threshold.**
- **Settled in A5** (evidence and decision table: `research/steps/a5-threshold.md`). Three `#define`s in the driver:
  - `SYNAPTICS0078_ENROLL_STAGES` = 18 (confirmed) [MATCH: FRR is bounded by enrollment coverage; 15–18 touches];
  - `SYNAPTICS0078_SCORE_THRESHOLD` = 105, the score of 6 fully consistent matches;
  - `SYNAPTICS0078_UPSCALE` = 2 (2x stays).
- Rule: the first whole match count that is at least 2 above the highest impostor score seen at either scale. Impostor max: 6 at 1x, 0 at 2x. The Python-port numbers did not carry over (genuine scores rose about 10x with the fix [SIGFM §4.3]), so the threshold comes from the compiled SIGFM in the fork.
- FRR at 2x / threshold 105 falls 27.8% → 14.4% → 6.7% for 5 → 7 → 9 enrolled images (one sitting). Estimate at 18 stages: ≤ ~10.7%.
- Cross-checked against libfprint's own verify path: identical scores.
- Caveat: one person, 3 fingers. About 300 effective impostor pairs bound the per-pair false-accept rate only to ~1% (95% confidence), which is ~16.6% per 18-stage verify. The margin is the protection beyond the data. B3 is owed before relying on sudo or polkit.

**D7 Tests.** Superseded by A7: a synthesized replay instead of a hardware recording.
- The original plan was a libfprint-standard umockdev `capture` replay recorded on hardware with `create-driver-test.py` (needing `umockdev`, `usbmon`, root and the user's finger). Rejected, for two reasons:
  - umockdev returns recorded completions at once, so a recorded timeout replays as an instant empty read; a synthesized `S` with no `C` lets libusb's own timer fire (a spike confirmed both TIMED_OUT and CANCELLED);
  - a recording carries the real module, program and images, and umockdev compares every OUT buffer byte for byte, so it could never leave a private branch.
- Also rejected: the ioctl format, an env-var or meson-option pin bypass, and a capture test.

**D8 Packaging.**
- PKGBUILD at `fingerprint-06cb-0078/packaging/`, package `libfprint-sigfm-0078` [PKG §3, scheme B]:
  - `provides=("libfprint=1.94.100" "libfprint-git=1.94.100" "libfprint-2.so=2-64")`;
  - `conflicts=(libfprint libfprint-git)`;
  - no `replaces`, no IgnorePkg, no pacman.conf edits.
- pkgver `1.94.100.rN.gSHA`; license LGPL-2.1-or-later (fork) and MIT (this project).
- Decided path: `packaging/arch/libfprint-sigfm-0078/`. `libfprint-2.so` is listed bare in `provides`; makepkg adds `=2-64`.
- Keep SONAME `libfprint-2.so.2` and the exported ABI, checked with `nm -D --defined-only` against stock. fprintd is unchanged.
- It ships the regenerated `60-autosuspend-libfprint-2.hwdb` and `70-libfprint-2.rules`.
- Install it **before** running Omarchy's fingerprint setup. That setup's `pacman -S --ask 4 libfprint-git` is the one path that can swap ours out, and it only runs when fprintd or usbutils is missing.
- Optional: a `~/.config/omarchy/hooks/post-update.d/` hook that warns if `pacman -Q libfprint-sigfm-0078` fails.
- Rejected:
  - Naming it `libfprint-git`: Omarchy's epoch/pkgrel bumps and `-Suu` would overwrite it.
  - IgnorePkg: `omarchy-refresh-pacman` rewrites pacman.conf.

**D9 Location.** This brief lives in `fingerprint-06cb-0078/docs/`, the project's design-doc folder.

## Approach sketch

- **Data flow at runtime:**
  - fprintd → libfprint (fork) → `synaptics0078` driver;
  - the driver reads `/var/lib/fprint/synaptics0078/{module.bin,program-<serial>.bin}` and USB-talks to the sensor;
  - the 144x56 image goes to SIGFM (keypoints, then score);
  - fprintd stores the FpPrint (FPI_PRINT_SIGFM) under `/var/lib/fprint/<user>/synaptics0078/0/`.
- **Instance state:** the blobs (GBytes), the serial hex, a `deactivating` flag, the SSM pointer and a cancellable for the EP 0x83 wait. "Module loaded" is re-read per activation [DRV §5.1].
- **device_id:** default `"0"`, so prints follow the driver, not the unit [DRV §5.2].
- **Image to SIGFM:** start with a 2x nearest-neighbour upscale to 288x112. It was the best measured variant (enroll-5 FRR 25.9%, impostor max 0), and FPC1022 does the same; confirmed in A5. Images under 25 keypoints are rejected by the core as RETRY_GENERAL.
- **Comparable code:** vfs7552.c (same 01/19/04/52 family, EP 0x83); nb1010.c (loop SSM, deactivating flag); vfs0050.c (cancellable interrupt wait); ssubbotin's fpcmoh driver (SIGFM opt-in, upscale) for reference only.
- **Prototype mapping:** `proto/capture.py` `build_plan()` is the on-wire reference for every transfer, and `proto/parsers.py` for reply parsing.

## Phases and steps

Phase A: it works on this laptop. Each step was built and checked separately, and each step is done when:
- **A1. Fork + SIGFM.**
  - The repo and branch exist, with the 8 + 1 commits cherry-picked (`-x`).
  - `build/config.h` has `HAVE_SIGFM 1`.
  - `meson test -C build` shows 0 failures; skips are expected without umockdev.
  - No driver yet.
- **A2. Finger-lift experiment.**
  - A proto script uses only the known capture path (01/19/75/[06]/02/51/04/52), so it makes no new writes.
  - It logs EP 0x83 events through lift and re-arm while the tester touches the sensor.
  - Findings go into the brief's D5.
- **A3. Driver opens and identifies.**
  - With `FP_DRIVERS_ALLOWLIST=synaptics0078`, the fork's `examples/` list or open the device.
  - The ROM, sensor type and serial are logged.
  - Missing files give the documented error.
- **A3 status: done, verified on hardware 2026-10-14** (open/close PASS, missing-files PASS; module upload path untested because the module was already loaded; see README, "libfprint driver: hardware check (A3)", and research/steps/a3-open.md).
  - Fork: new `libfprint/drivers/synaptics0078.c`.
    - `meson.build` `drivers_info` entry `{ 'sigfm': true, 'optional': not have_opencv }`: a build without OpenCV drops the driver instead of failing (plus the upstream-series guard that then skips the hwdb content check).
    - `libfprint/meson.build` `driver_sources`.
    - New meson option `synaptics0078_data_dir`, default `/var/lib/fprint/synaptics0078`. It is not derived from `localstatedir`, because fprintd's StateDirectory is fixed.
    - Env override `FP_SYNAPTICS0078_DATA_DIR` is always honoured (D4).
    - `data/autosuspend.hwdb` regenerated.
    - No `tests/meson.build` entry: `drivers_tests` asserts the fixtures exist, so registration waits for A7.
  - Open SSM:
    - claim interface 0;
    - `01`: ROM 6.20, else NOT_SUPPORTED; serial = RomInfo 0x12–0x17 nibble-swapped; byte 17 = module state;
    - `19`;
    - `75`: (0x00b7, 0x0037|0x0048) → 0x2449, else NOT_SUPPORTED;
    - load `module.bin` (10500 B + sha256) and `program-<serial>.bin` (18869 B, the full `02` command, first byte 0x02);
    - only when byte 17 == 0: send `06` + module, then re-read `01` to confirm byte 17 != 0.
  - Errors: missing or unreadable file → GENERAL (path, setup-tool hint, env var); wrong size, hash or opcode → DATA_INVALID; bad reply or status, 0x04aa on `06`, or upload not confirmed → PROTO; USB errors pass through.
  - Deviation from D3: the `06` upload happens at OPEN in A3, so the check shows the module state. D3's per-activation re-check of byte 17 still applies in A4, because resume wipes the module.
  - After a failed open the driver releases the interface AND closes the GUsbDevice itself. The core never calls `img_close` after a failed open, and only `fpi_device_close_complete` closes the USB handle; without this, a retry in the same process (fprintd after the setup tool ran) fails "already open". Upstream drivers share this core gap.
  - Capture stub: activate completes, then raises `session_error(NOT_SUPPORTED)`, so the core returns to INACTIVE cleanly (failing activate itself left it stuck in ACTIVATING). A4 replaces it.
  - Verified on hardware 2026-10-14: the serial derivation on real replies (the derived serial matches the one in the program file).
  - Verified on hardware after a reboot, 2026-10-14 23:23: byte 17 = 0 → `06` upload (status 0) → `01` re-read shows byte 17 = 0x0e at once → open OK.
  - Still unverified: the failed-open retry.
- **A4. One image through libfprint.**
  - `examples/img-capture` (or equivalent) writes a 144x56 image matching proto's quality.
  - An error header gives a retry, not a failure.
- **A4 status: done, verified on hardware 2026-10-14.** Three runs of `tools/dev/a4-capture.sh`, all `PASS: 3/3 images delivered` (9 images, sd 43–51, ridge coverage 82–100%). Finger-off by re-arm probing took 0.96–1.6 s after the image; one bad touch (code 7, error header 0x80200000) was retried automatically, and a bad capture during a finger-off probe was discarded. Module upload at activation not exercised (module already loaded). Cosmetic: a retry also prints "Finger lifted before the image was taken".
  - Fork: `libfprint/drivers/synaptics0078.c` only (no meson, hwdb or tests/meson.build change; the replay test is A7).
  - Activate: re-reads `01` at every activation; sends `06` + module only when byte 17 == 0, then re-reads `01` to confirm. The open-time upload from A3 stays.
  - Capture (change_state driven, vfs7552 template):
    - arm with `02` unless already armed; wait on EP 0x83 (interrupt, cancellable); finger event reports finger on;
    - ready codes 4/7 go on, 5 extends the 5000 ms wait, 0/6 fail and retry;
    - `51 00 20 00 00` reply (8082 B) parsed like `proto/parsers.parse_image_reply`: image, or error header / status 0x0412 → `fpi_image_device_retry_scan(GENERAL)`; not-ready 1/2/5/6 → read again (1 s), then re-send, at most 4 replies;
    - `04` then `52` after every `02` on every exit path (0 and 0x0412 on `04` are benign), except deliberately after finger-off.
  - Finger off (A4 version; D5 v2 supersedes it): `02`, 500 ms window (`SYNAPTICS0078_FINGER_OFF_PROBE_MS`); finger event → still down: wait ready, read and discard the image, `04`/`52`, wait 250 ms (`SYNAPTICS0078_FINGER_OFF_RETRY_MS`), probe again; no finger event in the window → finger off, sensor left armed. Logs "Finger off after N probe(s), M ms".
  - Image: 2x nearest-neighbour upscale to 288x112 (`SYNAPTICS0078_UPSCALE`); SIGFM leaves pixels unchanged, so decimating gives proto-comparable raw pixels.
  - Deactivate: cancels the EP 0x83 wait and any pause, unwinds a running SSM, `04`/`52` if armed; errors while deactivating are logged and ignored, so a finished enroll is not failed.
  - Deviations:
    - code 7 is treated as ready as in proto: the `51` reply decides (the one hardware observation gave an error header → retry);
    - a fail event or missing ready event while probing is inconclusive and probes again;
    - capture-path bulk transfers carry no cancellable (each is bounded by its timeout; keeps the pipe in step);
    - the 2x upscale is in place from A4 (A5 confirms or changes it).
  - Tests: meson compile, build-gi and a strict `--werror` synaptics0078-only build: 0 warnings. `meson test`: build 8 OK / 0 fail / 33 skip; build-gi 132/133, only egis_etu905 (known upstream flake). Project suite: 275 passed.
  - Project files: `tools/dev/a4-capture.sh`, `tools/dev/a4_capture.py`, `tools/test_a4_capture.py`.
  - Verified on hardware 2026-10-14: all transfers and timings, the probe windows, the retry path on a real error header.
  - Module upload after a reboot verified (at open, see A3). Still unverified: re-upload at activation after suspend/resume (an A5 acceptance item).
- **A5. Enroll and verify on hardware.**
  - fprintd (the system unit, under its sandbox) runs against the fork, from the build tree or a local install.
  - Set the threshold, the stage count and the upscale.
  - Check that OpenCV works under `MemoryDenyWriteExecute=true`.
- **A5 status: enroll/verify done 2026-10-15; suspend/resume and cross-day recognition still open** (`research/steps/a5-ground-truth.md`: two fingers enrolled 18/18, verify runs as expected; still open there: suspend/resume with the device idle and during an active verify, and recognition on a later day). A5b is finger-off v2 and the temperature decision (D5 v2). The procedure is in README.md, section "libfprint driver: enroll and verify with fprintd (A5)" (`a5-fprintd-dev.sh install --diag`, `a5-enroll.sh`, `a5-verify.sh`).
  - Part 1, values (see D6):
    - Fork change in `synaptics0078.c`: the three `#define`s, and class_init sets `nr_enroll_stages` and `score_threshold`. Before this, the core defaults (5 stages, threshold 40) applied.
  - Part 2, fprintd against the fork: `tools/dev/a5-fprintd-dev.sh install | status | undo`.
    - The library is copied to `/usr/local/lib/libfprint-0078-dev/`.
    - A systemd drop-in for `fprintd.service` sets `LD_LIBRARY_PATH`, `OPENCV_OPENCL_RUNTIME=disabled` and `G_MESSAGES_DEBUG` (for the driver's info lines).
    - Data files go to `/var/lib/fprint/synaptics0078/`, root, directory 0700, files 0600.
    - A manifest lets `undo` remove only what `install` created; `undo` asks before deleting this driver's prints.
    - No sandbox directive is changed. `FP_DRIVERS_ALLOWLIST` is not set, because 06cb:0078 is the only matching reader.
    - fprintd lists the data dir `/var/lib/fprint/synaptics0078` as a "user" with no prints. Harmless.
  - `MemoryDenyWriteExecute`: **PASS**.
    - No executable stacks, TEXTREL or W+X segments across the lib's 43-library `ldd` closure.
    - A SIGFM workload under MDWE (prctl, and `systemd-run --user` with MDWE + `@system-service` + `NoNewPrivileges`) gave identical scores, while a control W+X mmap was refused.
    - Side note: NVIDIA GLX libs attempt RWX when `__GLX_VENDOR_LIBRARY_NAME=nvidia` is in the environment. It is refused and harmless, and the variable is absent from fprintd's environment.
  - Measured offline: a SIGFM print is ~2.6 MiB for 18 stages (sum of per-stage data); verify reject takes 14–44 ms (median).
  - The hardware items of the A5 plan (written before the run): the 18-stage enroll; 3 x 10 verifies (right-index match, right-middle and left-index no-match); suspend/resume module re-upload; finger-off behaviour under fprintd, checked with the `--diag` dumps. Results: `research/steps/a5-ground-truth.md`; suspend/resume preparation: `a5-suspend-prep.md`.
- **A6. Setup tool and package.**
  - `tools/` writes the data files.
  - `packaging/` builds and installs `libfprint-sigfm-0078`.
  - Omarchy "Setup Fingerprint" keeps our package, and sudo, polkit and the lock screen accept the finger.
- **A6 status: done; built and checked, then installed on the hardware 2026-10-16** (`research/steps/a6-switch.md`). Guide: README.md, section "libfprint driver: switch to the package (A6)".
  - Package: `packaging/arch/libfprint-sigfm-0078/PKGBUILD`.
    - At A6: source `git+file` of the fork pinned at 40cda345, pkgver `1.94.100.r23.g40cda34`. Now (C1): the fork `https://github.com/rsd/libfprint.git` pinned by commit f9c60a25 and this project by release tag v0.1.0; pkgver `1.94.100.r25.gf9c60a2`, pkgrel 3. The checksummed copy is the AUR package `libfprint-sigfm-0078`; the in-tree copy differs only in its `SKIP` sums. Local development builds: `tools/dev/build-package.sh`.
    - `provides` libfprint=1.94.100, libfprint-git=1.94.100, libfprint-2.so=2-64; `conflicts` libfprint, libfprint-git.
    - `depends`: Arch's list plus opencv, libstdc++, python. The build fails unless `HAVE_SIGFM 1` (meson would otherwise drop the driver silently).
  - `check()`: 132 OK / 1 fail (egis_etu905) on the full run; egis_etu905 then passed on its first solo rerun. `check()` reruns egis_etu905 alone, up to 3 times, only when it is the sole failure (it failed 3/8 solo runs on unmodified upstream v1.94.100 and 2/5 on the fork; umockdev replay timeout while cancelling).
  - Symbols (`check-lib`): 92 stock, 94 ours, 0 missing, 2 added (`fp_image_extract_sigfm_info`, `fp_image_get_sigfm_info`); SONAME `libfprint-2.so.2`, no RPATH.
  - Files: stock layout + `/usr/bin/synaptics0078-setup` + `/usr/include/libfprint-2/sigfm/sigfm.h`. Nothing under /var, no .bin; the unit serial appears nowhere in the archive. namcap clean on PKGBUILD and package.
  - Setup tool: `synaptics0078-setup install --from DIR | status`. Stdlib-only Python with `#!/usr/bin/python3 -I`, imports nothing from the project (resolves "A6 helper and root"). Refuses a source dir not owned by the sudo user. Phase B adds a loader in the same tool (seam: `load_from_dir`).
  - Dev hand-over: `a5-fprintd-dev.sh undo --keep-data`; `a5-fprintd-dev.sh check-lib FILE`.
  - Autosuspend: the fork's hwdb lists 06cb:0078 (ID_AUTOSUSPEND=1, ID_PERSIST=0), stock does not. With the package, udev sets `power/control=auto` at the next device add (reboot). While fprintd has the device open, the usbfs file descriptor holds a runtime-PM reference, so autosuspend cannot hit during a capture; it can hit between sessions, and the driver re-checks RomInfo byte 17 at every open and activation. Hardware check owed: README A6 step 7.
  - Omarchy: `provides libfprint-git` keeps `omarchy-setup-security-fingerprint` from swapping the library (its `omarchy-pkg-missing libfprint-git fprintd usbutils` is false). The remaining hazard is the Remove then Setup cycle. The setup also enables lock-screen fingerprint, so it is not run until B3.
  - At A6 the lock screen and the acceptance item "Omarchy Setup Fingerprint keeps our package" waited for B3 (first session 2026-10-16, `research/steps/b3-results.md`: 0/9000 impostor trials accepted, one person; threshold kept at 105).
- **A7. Replay test.** Status: done, fork commits 13e51409 (core) and f9c60a25 (driver and tests).
  - `tests/synaptics0078/` (`generate.py`, `custom.py`, `hwpin.py`, `README.md`, `device`, `custom.pcapng`, `data/`) runs in `meson test` and in the PKGBUILD `check()`. Nothing is done at the sensor.
  - It is fully synthetic (serial 5e71a1c0ffee, opaque module and program, seeded blob images), with no recording. Sequence: open with a flipped-byte module (DATA_INVALID), open with upload, capture, an 18-stage enroll covering the error and retry cases, a matching verify (module re-upload), a non-matching verify, close.
  - Driver: a second module pin `SYNAPTICS0078_TEST_MODULE_SHA256`, accepted only when `fpi_device_emulation_mode_enabled()`. On hardware the real pin is the only one; the permanent test `synaptics0078-hwpin` proves it (emulation off gives DATA_INVALID naming the synthetic hash).
  - `synaptics0078-fixture` runs `generate.py --check`: regenerates, compares bytes, checks the driver define.
  - Core commit (`fpi-image-device.c`, upstreamable alone): no "Deactivating image device while it is not idle" warning when an enroll completes while still awaiting finger-off. On hardware the only visible change is that warning leaving the journal.
  - Timing limit: under a test wrapper (`LIBFPRINT_TEST_WRAPPER`, e.g. valgrind) `custom.py` widens the probe window to 5000 ms, and the test needs a timeout multiplier (about 127 s).

Phase B: any 0078 owner, without Windows. Outline only; research comes before code.
- **B1. Module from a public vendor download.**
  - The Microsoft Update Catalog lists "USB 073" 5.5.2706.1073 / 5.5.2707.1073 [SUP §4].
  - An OEM zip with `synaWudfBioUsbClevoProd.inf` (Wortmann, 5.5.2707.1073) was found [SUP].
  - The tool downloads, extracts and hash-checks the file, which must equal our module's sha256.
  - Precedent: python-validity's `validity-sensors-firmware`.
- **B2. Linux-only calibration.**
  - The 56 factory values come from `6f 00 0e …`, confirmed equal to CalibrationData 0xEC–0x123 on our unit.
  - The calibration and baseline frames need Windows' calibration procedure recreated; python-validity `calibrate()` is a reference.
  - The fixed payload template must come from the DLL's capture-program tables, not from iusearch's payload [CAL §5].
  - Research first. Flag any sensor write outside the known capture path before running it.
- **B3. False-accept benchmark beyond our 30 images**, since sudo and polkit depend on it: more people, or public datasets cropped to 144x56.
- **B4. A public replay test** with no proprietary or biometric bytes.

Phase C: in Omarchy.
- **C1. Publish.** The fork and this project on GitHub (github.com/rsd/libfprint, github.com/rsd/fingerprint-06cb-0078), and the AUR package `libfprint-sigfm-0078`. Both were audited for private data first (blobs, calibration, captures, logs, the serial).
  - The public project repo starts from one fresh commit built from the audited tree. `tools/test_no_serial.py` (the tracked-file serial guard) must pass on the tree being published.
- **C2. Propose to Omarchy.**
  - A package in `omacom/omarchy-pkgs`.
  - `omarchy-setup-security-fingerprint` installs it instead of `libfprint-git` when 06cb:0078 is present. `omarchy-hw-fingerprint` already detects it.
  - This keeps OpenCV off other users' machines.
  - Precedent: omarchy-pkgs already patches libfprint-git (the focaltech PID patch; `aurora-apple-sep.patch` on aarch64) [PKG §2].
- **C3. Upstream.** Track SIGFM !530. libfprint is unlikely to take a driver that uploads a proprietary module (HACKING.md); revisit later.

## Acceptance criteria (Phase A)

- The fork builds; the existing tests plus the new `synaptics0078` replay test pass.
- `fprintd-enroll` completes 18 stages on the real sensor.
- `fprintd-verify` accepts the enrolled finger and rejects the user's other enrolled-candidate fingers, over repeated trials (at least 20 genuine and 20 impostor attempts, logged).
- With the package installed, Omarchy's "Setup Fingerprint" leaves it in place (`pacman -Q libfprint-sigfm-0078` afterwards), and sudo and the lock screen accept the finger.
- Works after suspend/resume and after a reboot: the module is re-uploaded only when byte 17 == 0.
- A package upgrade or reinstall leaves `/var/lib/fprint/synaptics0078/` untouched.
- `nm -D --defined-only` shows no removed or changed exported symbols against stock 1.94.100.

## Open questions

- **Hardware-dependent items below** (EP 0x83 drain, long-armed program, suspend/resume) are checked in the A5 hardware run.
- **Finger-lift signal:** resolved by A2 (`research/steps/a2-fingerlift.md`): no lift event; re-arm probing, see D5 v2. Delays are bounded by the slow back-off (A5b); the final check is the A5 hardware run.
- **Temperature:** decided in A5b: `temp_hot_seconds = -1` (D5 v2).
- **A6 helper and root:** resolved in A6: `synaptics0078-setup` is stdlib-only and imports nothing from the project. (Was: the data helper puts the user-owned `proto/` on `sys.path`. When A6 runs it as root, user-editable code would run as root, so A6 must copy/vendor what it imports, or make it root-owned.)
- **EP 0x83 drain before `02`:** a stale fail event could cause a spurious retry; decide in A5 whether to drain first.
- **Long-armed program:** the sensor may time out an armed program sensor-side; check in A5.
- **Probe window is wall-clock based:** resolved in A7: the synthesized trace works with the real window, and under a test wrapper `custom.py` widens it to 5000 ms.
- **Core warnings seen in review:** "ACTIVATING→ACTIVATING" after a failed activation; "Deactivating … not idle" at the last enroll stage (vfs7552 also hits it).
- **Threshold value and upscale choice:** settled in A5 (105, 2x, 18 stages; D6).
- **False-accept bound:** our sample bounds it only to ~1% per pair (95%), so B3 is needed before relying on sudo or polkit.
- **device_id:** stays `"0"` unless A5 shows a reason.
- **SIGFM print size:** measured offline in A5: ~2.6 MiB per 18-stage print. The on-disk size with framing is still to see.
- **Suspend/resume:** does resume (or autosuspend) wipe the module? Re-checking per activation handles both cases; confirm it.
- **Recording without the module:** resolved in A7: nothing is recorded; the trace is synthesized with an opaque module, so no real module bytes are involved (see D7).
- **The 18869 B program as one or two URBs:** resolved in A7: umockdev advertises scatter-gather, so the program is one URB under replay.
- **Payload template source for B2:** from the DLL's tables.
- **Public test data for B4.**
- **OpenCV install footprint** on a clean Omarchy, which matters for C2; not measured.
- **SDDM login with fingerprint** (asked 2026-10-15): possible through PAM (`pam_fprintd` in /etc/pam.d/sddm); the driver needs nothing new. Built as `tools/sddm-fingerprint.sh` (README, "Fingerprint at the SDDM login screen").
  - Omarchy uses SDDM with the omarchy theme.
  - Omarchy's /etc/pam.d/sddm unlocks gnome-keyring with the login password. A fingerprint login leaves a password-protected keyring locked, so apps then ask for the password.
  - Gated on B3, like the lock screen. Decide then between the lock screen only (recommended first: used more often, and the keyring is already unlocked) and SDDM too. Test how the omarchy SDDM theme shows the prompt.

## Step status

| step | what | status |
|---|---|---|
| A1 | fork + SIGFM series | done (`research/steps/a1-fork.md`) |
| A2 | finger-lift experiment | done (`research/steps/a2-fingerlift.md`) |
| A3 | driver opens and identifies | done, hardware 2026-10-14 (`a3-open.md`) |
| A4 | one image through libfprint | done, hardware 2026-10-14 (`a4-capture.md`) |
| A5 | enroll and verify with fprintd | enroll/verify done 2026-10-15; suspend/resume and cross-day recognition still open (`a5-ground-truth.md`, `a5b.md`) |
| A6 | setup tool and package | done, installed 2026-10-16 (`a6.md`, `a6-switch.md`, `a6-sddm.md`) |
| A7 | synthesized replay test | done, fork 13e51409 and f9c60a25 (`a7-b4-design.md`) |
| B1 | module from a public vendor download | done (`b1-module-source.md`) |
| B2 | Linux-only calibration | done (`b2-calibration-source.md`, `b2-run.md`, `b2b-results.md`) |
| B3 | false-accept benchmark | first session done, one person (`b3-results.md`) |
| B4 | public replay test | covered by A7 |
| B5 | setup without Windows | done, hardware 2026-10-17 (`b5-run.md`) |
| C1 | publish | this release (v0.1.0) |
| C2 | propose to Omarchy | open |
| C3 | upstream | open |

The A1 recipe, for rebuilding the fork branch from upstream (D1, D2):
1. `git clone https://gitlab.freedesktop.org/libfprint/libfprint.git ~/Devel/Linux/Synaptics/libfprint` (full clone), then `git switch -c sigfm-0078 6f9479c3d55f847c1b3769f28ceb99227f9858cf`.
2. Add remote `ssubbotin` = `https://gitlab.freedesktop.org/ssubbotin/libfprint.git` and fetch `sigfm-benchmark-fpc1022`.
   - List `d258eb51^..7263fb2c` and confirm it is 8 commits and that none touches the FPC1022/fpcmoh driver (`19f4bb5b` must not be in the range).
   - Then `git cherry-pick -x d258eb51^..7263fb2c`.
3. Add remote `gotenks` = `https://github.com/gotenksIN/libfprint-fpc-disum.git`, fetch `main` and `git cherry-pick -x 094b65d0` ("device: Validate gallery equality only for raw prints"). Do not take `406c10e1`.
4. Check the comparator fix in `libfprint/sigfm/sigfm.cpp`: the dedup key covers (p1.y, p1.x, p2.y, p2.x) and the cap is 176 correspondences.
5. `meson setup build -Ddrivers=all -Dintrospection=false -Dinstalled-tests=false`, then `meson compile -C build`. Confirm `HAVE_SIGFM 1` in `build/config.h`, and run `meson test -C build`.
