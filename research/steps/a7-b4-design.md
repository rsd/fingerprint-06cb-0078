Status: A7 is built in the fork (commits 13e51409 and f9c60a25); the text below is the design.

# A7 + B4: a synthesized replay test and public test data (design)

Design written 2026-10-17. Offline only: no USB, no sudo, no hardware. The detailed working notes
(sources, umockdev internals, the experiment code, the skip inventory) are local working files, not published.

## Verdict

- **The replay can be synthesized completely; no recording, and nothing has to be done on hardware.** Every
  byte is a protocol constant the driver already spells, a synthetic stand-in, or a public USB
  descriptor. A real recording would even replay wrongly: umockdev returns each recorded
  completion at once, so a recorded timeout becomes an instant empty read (vfs0050 needs a hack
  for this). A synthesized file leaves the completion out, and libusb's own timer fires.
- **Enroll and verify run end to end on synthetic images.** Measured through the fork's compiled
  SIGFM (virtual_image, threshold 105, 18 stages) with seeded blob fields: 18/18 stages accepted
  first try; a probe identical to an enrolled image scored about 1.1–1.3 million; unrelated
  synthetic probes scored 0 against all 18.
- **Two small fork changes besides the fixture:** a synthetic module hash that only emulation mode
  accepts, and removing a core warning at the last enroll stage that `G_DEBUG=fatal-warnings`
  would turn into an abort.

## Goal

- **A7.** `tests/synaptics0078/` in the fork: a libfprint-standard umockdev test that runs in
  `meson test` and in the PKGBUILD's `check()`. It replays the whole driver: open with the module
  upload, capture, an 18-stage enroll (with a retry and the same-touch guard), a matching verify
  (module uploaded again at activation), a non-matching verify, close.
- **B4.** Everything public is synthetic and checked to be so. Tests that skip on a clean clone
  only because the real template is absent run on the synthetic one instead.

## Non-goals

- New driver behaviour (on hardware only the core warning disappears); upstream submission;
  publishing (phase C); valgrind runs.
- Replacing the local-only regression tests against this unit's real data (CalibrationData, the
  Windows-built program) or the real vendor files. They stay local by design.

## Decisions

1. **Synthesize the replay; do not record one.** (Question 1)
   - The device side is a script of exchanges taken from the driver's own SSMs. Its contents
     follow the hardware logs: `0000000000` after every `02`, finger `0200401000`, the ignored
     `0340010000`, ready `0342040040`, the 2154-byte `02` reply, the 8082-byte image reply,
     `0x0412` on `04`.
   - Rejected: one fingers-off recording, scrubbed afterwards. It needs root, tshark and usbmon; it
     carries the real module, program and 19/02 reply payloads, all to be replaced; a fingers-off
     session has no image; it replays timeouts wrongly. The hardware logs and the hardware-tested
     Python model already show the sequence. Fallback if the spike fails: see "Operational notes".
2. **pcapng, not ioctl.**
   - umockdev's pcap replay matches each submit by type, endpoint and length, compares OUT data
     byte for byte, and returns a discarded URB as cancelled, so a timeout or cancel is written as
     "submit, no completion". It advertises scatter-gather, so the 18869-byte program is one URB
     (the brief's "one or two URBs" question, for the replay).
   - Rejected: the ioctl text format (no submit or discard record, so no timeout; twice the size;
     newer upstream tests use pcapng).
3. **The generator lives next to the fixture**, as `tests/synaptics0078/generate.py` in the fork.
   - Python stdlib only, deterministic, LGPL-2.1-or-later, new code (nothing copied from the
     project). It writes `device`, `custom.pcapng`, `data/module.bin` and
     `data/program-e5171a0cffee.bin`; `--check` regenerates into a temp dir and compares bytes.
   - Why: the fixture can be rebuilt and audited from the fork alone. The project cross-checks it
     against its own hardware-tested parsers (B4), so two protocol models must agree.
   - Rejected: the generator in the project (upstream would get binaries with no visible source).
4. **The module hash: a second pin, accepted only in emulation mode.** (Question 2)
   - Today the driver has: `FP_SYNAPTICS0078_DATA_DIR`, always honoured; a hard sha256 pin plus a
     size check on `module.bin`; for the program only a size check and first byte `0x02`; no use
     of emulation mode.
   - Add `SYNAPTICS0078_TEST_MODULE_SHA256`, the hash of the synthetic `data/module.bin`, accepted
     only when `fpi_device_emulation_mode_enabled()` is true. Nothing else changes.
   - Why the shipped build is not weakened:
     - emulation mode needs `FP_DEVICE_EMULATION=1` and `libfprint-test-emulation.so` loaded from
       the build tree or the installed-tests directory, and the package is built with
       `installed-tests=false`, so the library is not installed;
     - only root sets fprintd's environment;
     - the extra pin matches exactly one synthetic file, never "anything";
     - precedent: upstream drivers use the same switch for replay-only behaviour (uru4000 fixed
       seed, synaptics fixed serial, secugen, vfs0050).
   - Rejected:
     - an env var holding the pin: a production bypass for any bytes;
     - a meson option: `check()` tests the shipped build;
     - skipping the hash check in emulation: a blanket bypass;
     - loading the module lazily, or a replay where it is always loaded: changes the shipped error
       behaviour, or leaves the upload untested.
   - `generate.py --check` also asserts that the define in the driver equals the generated
     module's hash.
5. **The data directory comes from the existing variable.** `custom.py` sets
   `FP_SYNAPTICS0078_DATA_DIR` to its own `data/` before opening the device. No driver change.
6. **The synthetic program is opaque:** `0x02`, an ASCII tag saying "synthetic", then seeded
   filler, 18869 bytes.
   - Rejected: building it from the synthetic template. Its generated protocol parameters are
     byte-identical to the real template's, which would trip the 16-byte window audit
     (decision 11). The driver does not look inside the program anyway.
7. **Images: seeded random blob fields; enroll and verify run end to end.** (Question 3)
   - 144x56: about 90 Gaussian blobs (light and dark, sigma 1.6–4 px) from splitmix64 with a fixed
     seed, scaled to mean 128 and sd 64.
   - No ridges, no minutiae, nothing taken from a real image. Neighbouring images differ by a mean
     absolute difference of 58–71, well over the guard's 30.
   - The test checks outcomes (match / no match), never exact scores; margins of about 10⁴ on both
     sides survive OpenCV version drift. `custom.py` imports `generate.image()` to check that the
     captured image is exactly the 2x upscale of image 0.
8. **A `custom` test only.** (Question 4) Upstream's README allows `custom.py` + `custom.pcapng`
   for any driver.
   - Rejected: a `capture` test (the shared `capture.py` cannot set the data directory, and any
     `capture.*` file would register a second test that fails); per-driver environment variables
     in `tests/meson.build` (shared test code changed for no extra coverage).
   - Registration: `'synaptics0078': { 'timeout': 60 }` in `drivers_tests`. Without OpenCV the
     driver is dropped and meson makes the test a skip (exit 77) by itself. Shipped timings stay
     (500 ms probe window); the run takes about 15 s.
9. **A core commit: no warning when an enroll completes before finger-off.**
   - At the last enroll stage the core calls `fpi_image_device_deactivate (self, FALSE)` while it
     is still in AWAIT_FINGER_OFF. For a press sensor that needs a probe window to see the lift,
     that state is certain. The warning is the flag's only effect.
   - The fix is one condition in `fpi-image-device.c`, as its own commit, so it can go upstream
     alone. It also clears the warning from fprintd's journal on real hardware (vfs7552 hits it
     too). No upstream test expects the message.
   - Fallback if a core change is not wanted: `custom.py` makes warnings non-fatal only around
     the last stage, and fails on any warning except that one. More fragile.
10. **What the scenario covers**, in one replay (about 0.7 MB, about 23 capture cycles):
    1. open #1 with a module that has one byte flipped: DATA_INVALID. This shows the test pin is
       not a bypass and exercises the failed-open cleanup, whose retry in the same process was
       never checked on hardware;
    2. open #2: byte 17 = 0, so `06` + module, then `01` confirms;
    3. `capture_sync`, compared with image 0;
    4. enroll, 18 stages, with one each of: a `51` error header (retry, then a stale reference),
       a "not ready" reply and re-read, a code-5 extend event, `0x0412` on `04`, and a same-touch
       capture (guard, a probe without a finger, then the lift bypass);
    5. verify with image 0: byte 17 = 0 at activation, so the module is uploaded again; match;
    6. verify with an unrelated image: no match;
    7. close.

    The build may drop a sub-case umockdev cannot express, naming it in its report.
11. **B4 scope.** (Question 5) The clean-clone gate shows 105 skips:

    | why it skips | count | B4 action |
    |---|---|---|
    | the real template is absent: `test_b2_program` 29, `test_calib_program` 19, `test_calibration` 9, `test_calib_model` 7, `test_template` 2 | 66 | Move to the synthetic template (`proto/synth_template.py`). A test stays local only when its point is the real file. |
    | this unit's calibration data (CalibrationData, `ours_cmd02.bin`) | 13 | Stay local. Check each has a synthetic counterpart on the same code path. |
    | vendor or captured files (`local/b5`, driverstore, first capture) | 10 | Stay local (byte-exact checks against real downloads). |
    | environment (built package, shellcheck, fork location, history, the real serial) | 16 | Unchanged. |

    - `test_b2_program` partly runs the tool as a subprocess, which an in-process patch cannot
      reach. `tools/dev/b2_program.py` needs a file-based template-and-pins test hook, under its
      existing rule that the test variables are refused as root.
    - New `tools/test_public_data.py`:
      - the fork's `tests/synaptics0078/` and every tracked project file share no informative
        16-byte window with the real module, template, program, CalibrationData or local captures
        (reusing the package audit's window code; skips without local data);
      - neither holds the serial;
      - every reply in `custom.pcapng` parses as expected with `synaptics0078.parsers`;
      - every OUT command is in the closed list (01 19 75 06 02 51 04 52).
12. **Two builds, A7 first.** (Question 6)
    - A7 touches the fork (plus the PKGBUILD pin) and holds the one technical unknown, umockdev.
    - B4 is project-only and mechanical; its audit reads A7's fixture.
    - Rejected: one build. It would mix a spike that can fail with about 70 mechanical test edits,
      and a failure in one would hold up the other.

## Approach sketch

**Fork** (`../libfprint`, branch `sigfm-0078`, LGPL-2.1-or-later), three commits:
- `libfprint/fpi-image-device.c`: decision 9.
- `libfprint/drivers/synaptics0078.c`: decision 4, plus a "Tests" paragraph in the header comment.
- New `tests/synaptics0078/`: `generate.py`, `custom.py`, `README.md` (synthetic, how to
  regenerate, why not recorded), and the generated `device`, `custom.pcapng`,
  `data/{module.bin,program-e5171a0cffee.bin}`.
- `tests/meson.build`: the `drivers_tests` entry, plus `synaptics0078-fixture` (runs
  `generate.py --check`).
- Generator: pcapng with linktype 220 (USB_LINUX_MMAPPED) and the 64-byte usbmon header; every
  IN submit length equals the driver's read size; a timeout or cancel is an S with no C;
  synthetic serial bytes `5e71a1c0ffee` (driver string `e5171a0cffee`); `device` shaped like
  `tests/vfs7552/device`.
- `custom.py`: sets the data dir and `OPENCV_OPENCL_RUNTIME=disabled` before importing FPrint;
  asserts the features (as `capture.py` does) and 18 stages; always calls `close_sync`; leaves
  `G_DEBUG` alone.

**Project** (`fingerprint-06cb-0078`, MIT):
- A7:
  - `packaging/arch/libfprint-sigfm-0078/PKGBUILD`: `_commit` set to the new fork head, pkgver from
    `pkgver()`, pkgrel 1;
  - `docs/brief-libfprint-driver.md`: D7 superseded; A7 status; the open questions "probe window vs
    replay", "recording without the module" and "one or two URBs" resolved;
  - `README.md`: one paragraph.
- B4: the five test files in the table, the `b2_program.py` test hook, `tools/test_public_data.py`.

## Acceptance criteria

A7:
1. `meson test -C build-gi synaptics0078 synaptics0078-fixture` passes 5 times in a row, also while
   the whole suite runs in parallel. The full suite is otherwise unchanged (the known egis_etu905
   flake excepted). A strict `--werror` synaptics0078-only build has no warnings.
2. The replay contains every case of decision 10 (or the build names each one it dropped and why),
   under `fatal-warnings`, with no `G_DEBUG` change.
3. With `FP_DEVICE_EMULATION` unset, the same replay fails at open with DATA_INVALID, naming the
   synthetic module's hash: the shipped pin is untouched. A manual check for the build, not a
   permanent test.
4. Two `generate.py` runs, also under `python3 -I`, give byte-identical files. `--check` fails on a
   flipped byte and on a changed driver define.
5. makepkg: `check()` runs and passes the new test; package audit 16 passed; `nm -D` symbols
   unchanged against the current package.
6. The project suite still passes in the dev tree (`test_a5_session` and `test_b3_capture` read the
   driver source).

B4:
1. Clean-clone gate (a local script, not published): 0 failed. Template-only skips drop from 66 to
   the few that check the real file, each named in the report.
2. `tools/test_public_data.py` passes in the dev tree with the local data present; on a clean clone
   it skips only its local-data part. `tools/test_no_serial.py` passes.
3. Dev tree: no test that passed before fails or skips now.

## Implementation plan

### A7: synthesized replay test (the fork plus the PKGBUILD pin)

0. **Spike first; stop if it fails.**
   - Run a minimal generated replay with umockdev-run against `build-gi`: open (no module upload),
     one capture, one finger-off probe that times out, a deactivate that cancels a listen, close.
   - Pass: the driver logs a real TIMED_OUT for the window and CANCELLED for the listen.
   - On failure, do not work around it in the driver; fall back to the recording described in
     "Operational notes".
1. `generate.py` (decisions 3, 6, 7, 10), its README, the generated files.
2. Driver pin under emulation mode (decision 4), with the `--check` link to the define.
3. Core commit (decision 9).
4. `custom.py` and the meson entries (decisions 5, 8). Iterate on the scenario until acceptance 1-4
   hold.
5. Verify acceptance 1-6.
   - Fork commits: core, driver, tests, in that order; the messages explain why the fixture is
     synthesized.
   - Project: PKGBUILD pin, brief, README.
- Provenance rule: nothing in the fork is derived from `local/`, `extracted/`, `driverstore/`,
  `proto/data/` or any capture. Images are generated, never viewed, never taken from a real capture.

### B4: public test data (project only; after A7)

1. Move the 66 template-only tests to the synthetic template (`proto/synth_template.py`) and
   `fixtures.py`. Keep a skip only where the test checks the real file, and list those.
2. Add the `tools/dev/b2_program.py` test hook (template path and pins, refused as root, documented
   in its docstring); `test_b2_program.py` uses it.
3. For the 13 calibration-data tests, confirm a synthetic counterpart on the same code path; add one
   where it is missing.
4. Add `tools/test_public_data.py` (decision 11). It finds the fork via `LIBFPRINT_FORK` or
   `../libfprint`, as the existing tests do.
5. Verify acceptance B4 1-3 on the dev tree and on the clean-clone gate.

## Operational notes

- Nothing is owed on hardware: the driver change acts only in emulation mode. The tools needed to run the replay are umockdev 0.19.8, gobject-introspection, python-gobject and the fork's `build-gi`.
- Optional after a package rebuild: one `fprintd-verify` as a sanity check. The only visible difference is that the "not idle" warning is gone from the journal.
- Fallback only if the A7 spike fails (umockdev cannot replay a synthesized timeout): one fingers-off recording of about a minute (usbmon plus tshark, open, activate and one capture attempt cancelled with no finger). It would stay in `local/` (it holds the real module and program) and be used only to compare packet structure (types, endpoints, lengths, order); no bytes are reused.

## Open questions

- None block the build. Decision 9 is the one with a cost: it adds one small commit to
  libfprint's core (outside our driver). The fallback is test-side and more fragile.
