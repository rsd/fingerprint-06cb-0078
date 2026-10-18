# A3: driver synaptics0078 opens and identifies

Outcome: implemented and tested without hardware; regression tests cover each fix; docs updated. Two sets of changes: the fork and this project. No USB access was used; the hardware run is still to be done (see "Hardware check").

## Scope
Scoped from the brief (D3/D4/A3), `research/recon/recon-driver.md`, the proto capture/parsers/calibration code and the logged 38-byte RomInfo reply, which confirmed the offsets and the nibble-swap mapping.

## Fork (`../libfprint`, branch sigfm-0078)
Files:
- libfprint/drivers/synaptics0078.c (new)
- meson.build (modified)
- meson_options.txt (modified)
- libfprint/meson.build (modified)
- data/autosuspend.hwdb (modified; regenerated, diff = only the synaptics0078 section)

Commit message: "synaptics0078: add driver for Synaptics 06cb:0078 (open and identify)". Behaviour: open SSM 01/19/75 checks (ROM 6.20, sensor type 0x2449), loads module.bin + program-<serial>.bin from the synaptics0078_data_dir meson option (default /var/lib/fprint/synaptics0078) or FP_SYNAPTICS0078_DATA_DIR, uploads 06+module only when RomInfo byte 17 == 0 and confirms it, closes the USB handle after a failed open, stub capture (NOT_SUPPORTED); optional when OpenCV is absent; hwdb regenerated.
Version: no bump. The meson version stays 1.94.100 (SONAME and provides, per D8).

## Project
Files:
- .gitignore (modified: local/)
- README.md (modified)
- docs/brief-libfprint-driver.md (modified)
- tools/synaptics0078_data.py (new)
- tools/test_synaptics0078_data.py (new)
- tools/dev/a3_open.py (new)
- tools/dev/a3-open.sh (new, mode 0755)

Change: A3 dev data helper and hardware check for the synaptics0078 driver; brief updated (D5 resolved by A2, A3 status).

## Tests
- Fork, after the fixes below: meson compile of build and build-gi with 0 warnings. Standalone `-Ddrivers=synaptics0078 --werror -Dintrospection=false -Ddoc=false -Dinstalled-tests=false` OK.
- `meson test -C build`: 8 OK, 0 fail, 33 skip.
- `meson test -C build-gi`: 132/133 OK. The one failure is egis_etu905, the known upstream-flaky test (see `a1-fork.md`). udev-hwdb passes in both.
- Simulated no-OpenCV configure (PKG_CONFIG_LIBDIR without opencv .pc): configures, drops the driver, udev-hwdb content check skips.
- Project: `proto/.venv/bin/python -m pytest -q proto tools`: 238 passed (200 proto + 38 tools). ruff clean at 120, bash -n, py_compile, `a3-open.sh --dry-run` exit 0.

## Defects found in testing and review, and their fixes
Fork:
- Fixed: `'sigfm': true` made no-OpenCV default builds fail. Now `'optional': not have_opencv` plus the 19f4bb5b hwdb guard.
- Fixed: after a failed open the core never closes the GUsbDevice, so a retry fails "already open". The driver now calls g_usb_device_close on both failure paths.
- Fixed: the failed-activate stub left the core in ACTIVATING. Now activate_complete(NULL) + session_error(NOT_SUPPORTED).
- Fixed: fp_warn on tolerated lengths is now fp_dbg.
- Kept: the post-06 01 re-read, flagged for the hardware run.

Project:
- Fixed: `--missing-files` now reports PASS on the expected GENERAL error.
- Fixed: umask 077, so logs are 0600 and the log dir 0700.
- Fixed: lexists, plus two-file staged publish with rollback.
- Fixed: a symlinked out dir is refused and a loose mode is tightened.
- Fixed: the stdlib-only test now uses sys.stdlib_module_names.
- Fixed: a sysfs serial vs program-file preflight was added.
- Fixed: the nits (timeout validation, strict allowlist guard, .so vs driver message, empty --data-dir, D-Bus mask hint, test comment).
- Recorded only: the A6 root/sys.path concern (now an open question in the brief).

Re-checked after the fixes (fork meson + open/close/activate; helper publish/rollback): no further defects.

## Not verified (needs hardware)
- The open sequence on real hardware: the 01 re-read right after 06 (proto never did it), the failed-open retry, and the serial derivation on real replies.
- uncrustify and shellcheck are not installed.
- A real no-OpenCV system (only simulated).

## Design notes
- The 06 upload happens at open in the A3 scope; D3 puts it at activation. A4 must re-check byte 17 per activation (recorded in the brief).
- The env override is honoured always (brief D4), not only in emulation mode (the recon suggested the latter).

## Hardware check
From the project root:
1. `sudo systemctl stop fprintd`, or `sudo systemctl mask --runtime fprintd` (undo later with `unmask --runtime`).
2. `proto/.venv/bin/python tools/synaptics0078_data.py`
3. `tools/dev/a3-open.sh`
   - Success: the driver lines "ROM version 6.20 …", "Sensor type 0x2449 …", "Serial …", "Data directory …", "Capture module: …", then `open: OK`, `close: OK`, `PASS: open and close succeeded`, exit 0.
4. `tools/dev/a3-open.sh --missing-files`
   - Success: `PASS: --missing-files: open FAILED with FPrint.DeviceError.GENERAL naming …/module.bin`, exit 0.

Logs go to `local/logs/` (local working files, not published).
