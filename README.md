# Synaptics fingerprint reader 06cb:0078 (collected from Windows, 2026-10-13)

Laptop: Clevo P870KM. Sensor: "Synaptics WBDI Fingerprint Reader - USB 073".
USB ID 06cb:0078, rev 0154, vendor class 0xFF. Internal USB.

## Install (Arch Linux, Omarchy)
The package `libfprint-sigfm-0078` is on the AUR, not in the pacman repositories: https://aur.archlinux.org/packages/libfprint-sigfm-0078. It replaces Arch's `libfprint` (it provides `libfprint` and `libfprint-git`, and conflicts with both). pacman or yay asks to remove whichever of the two is installed: answer yes. See "libfprint driver: switch to the package (A6)" for what pacman asks. The package does not depend on `fprintd`, so install that too.

Before you use the finger for sudo, polkit or the lock screen, read the false-accept limits in "False-accept benchmark (B3)". Setup needs no Windows; "Setup without Windows (B5)" says what it does.

### Omarchy
Omarchy ships `yay`. Run these in order, with your fingers off the reader for the second command:

```sh
yay -S libfprint-sigfm-0078 fprintd usbutils
sudo synaptics0078-setup install      # fingers off the reader
omarchy-setup-security-fingerprint    # enrolls, verifies, then sets up sudo, polkit and the lock screen
```

`fprintd` and `usbutils` go in the same command on purpose. Omarchy's own fingerprint setup installs its `libfprint-git`, `fprintd` and `usbutils` if any of the three is missing, and that would replace this package. With all three present it installs nothing: it enrolls a finger, checks it, and only after a verified print sets up the fingerprint for sudo, polkit and the lock screen. It does not ask first. Read "False-accept benchmark (B3)" before the third command. If you do not want all three, skip it and run `fprintd-enroll` instead.

Omarchy's fingerprint removal (`omarchy-remove-security-fingerprint`) removes its PAM lines and `fprintd` but keeps this package. Before you run its setup again, reinstall `fprintd` (`sudo pacman -S fprintd`); otherwise the setup installs its `libfprint-git` over this package without asking.

The setup finds this reader by its vendor ID. Omarchy's first-run notification runs the same setup: use it only after the first two commands.

### Arch Linux
With an AUR helper:

```sh
yay -S libfprint-sigfm-0078 fprintd
```

Without one:

```sh
git clone https://aur.archlinux.org/libfprint-sigfm-0078.git
cd libfprint-sigfm-0078
makepkg -si
sudo pacman -S --needed fprintd
```

Then, with your fingers off the reader, run `sudo synaptics0078-setup install`, and enroll with `fprintd-enroll`. To use the finger at the login screen, see "Fingerprint at the SDDM login screen".

### Going back
`sudo pacman -S libfprint`. pacman asks to remove this package. Prints enrolled with this reader will not work with stock libfprint, and Omarchy's PAM lines stay until you run its removal.

## Why a libfprint fork?
The package is built from [a fork of libfprint](https://github.com/rsd/libfprint) (branch `sigfm-0078`). Three things are missing upstream:

- **A driver and a matcher.** libfprint has no driver for 06cb:0078. The reader returns a 144x56 pixel image. libfprint's matcher (NBIS) rejected every genuine attempt in our tests (research/matcher-evaluation.md), while keypoint matchers told fingers apart. The fork carries SIGFM, proposed upstream in [MR !530](https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/530) and not merged.
- **Per-device data.** The reader keeps its vendor firmware module only in RAM, so it is lost on reboot and the driver sends it again when it is missing. A per-unit capture program goes with every capture. Neither is shipped inside libfprint (one is proprietary, the other unique to each unit), and libfprint has no place yet for per-device data ([issue #868](https://gitlab.freedesktop.org/libfprint/libfprint/-/issues/868), [MR !368](https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/368)). This project's setup builds that data on your machine.
- **One small core fix.** An enrollment that finishes while the reader still waits for the finger to lift raised a spurious warning; the fork fixes it in `libfprint/fpi-image-device.c`.

The fork is meant to be a drop-in. It is based on upstream master 6f9479c3 (current as of October 2026) and removes no upstream driver, feature or public function. The library name, soname and version are unchanged (`libfprint-2.so.2`, 1.94.100), and no upstream public function or symbol is changed or removed (the fork only adds two SIGFM image helpers), so fprintd works unchanged. Other readers keep NBIS; only `synaptics0078` selects SIGFM. The package builds all upstream drivers and runs the upstream test suite (egis_etu905, a known upstream flake, is rerun up to three times when it is the only failure).

The rest of this file is the project log: the Windows collection, then each development step.

## Windows driver stack
- INF: synawudfbiousbclevoprod.inf, v5.5.2707.1073, dated 2018-03-08, Clevo build.
- synaWudfBioUsb.dll: the UMDF user-mode driver. It talks to the sensor over WinUSB and holds all of the protocol logic. This is the file to reverse-engineer.
- synaBscAdapter.dll: the WinBio sensor and engine adapter (template matching).
- WudfUpdate_01011.dll: the UMDF co-installer (not relevant to the protocol).
- Services: WUDFRd and WinUsb (lower filter). The INF sets SensorMode=2 (advanced).

## Hints from strings in synaWudfBioUsb.dll
- `scsTLS.c`: the host and sensor talk over a TLS-like encrypted channel. That is how the Synaptics/Validity "match-on-chip" sensors work.
- `.xpfwext` / `UpdateFirmwareExtension`: the driver uploads a firmware extension to the sensor. The INF's FWextCopy section is empty, so the blob is probably embedded in the DLL.
- `checkSGXModeAndTransportOpen`: there is an Intel SGX mode.
- Build path: `clevo-HDRFP-2685`.

These match the protocol family that python-validity (github.com/uunicorn/python-validity) handles for 138a:0090/0097, 06cb:009a and others. 06cb:0078 is not on its list as far as we know, so start there.

## Collected files (local only)
Every file of the Windows collection named in this file and in HANDOFF-LINUX.md (driverstore/, installed/, registry/, logs/, programdata/, extracted/, usb_capture/ and the raw dumps) was kept on the owner's machine and is not part of the public repo: they hold proprietary vendor files or per-machine data, and `.gitignore` excludes them. The notes keep the findings drawn from them.
- driverstore/: the full driver package, including the INF, the CAT and the DLLs.
- installed/: copies of the installed DLLs (same SHA256 as the driverstore copies).
- registry/: the device enum key, the Biometric class key, the WbioSrvc service key and HKLM\SOFTWARE\Synaptics (that key also covers the touchpad).
- pnp_properties.txt and system_info.txt: raw PnP and system dumps.

## Second pass of capture
- The sensor works in Windows. Windows Hello identified the user on 2026-10-12, so fingerprints are enrolled. Synaptics match-on-chip sensors store templates on the sensor. Pairing or resetting it from Linux (e.g. python-validity's factory reset) will probably break Windows Hello until you re-enroll.
- logs/: the Biometrics/Operational event log (.evtx) and a text copy filtered to this sensor.
- Registry `Device Parameters\Device Data\CalibrationData` (26470 bytes) is a DPAPI blob (it starts with `01000000 d08c9ddf…`). It is likely the host-side pairing/TLS state that the driver's `PairingData` / `DoPairing` code uses. The non-elevated, LocalMachine-scope decrypt failed, so it probably needs the SYSTEM account.
  - extracted/CalibrationData_dpapi.bin: the raw encrypted blob.
- WinBio configuration 1 references `synaAdvAdapter.DLL` (DB F9E92F37-…), which isn't on disk. It's a leftover.
- The driver looks for its firmware extension at `C:\ProgramData\Validity\fwext\*.xpfwext`. That folder doesn't exist. `C:\ProgramData\Validity\valfpssc` (72 bytes, "SSC!" header, from 2017) is copied to programdata/.
- extracted/*_strings.txt: all ASCII and UTF-16 strings with file offsets, from both DLLs. Useful function names: DoPairing, DoUnpairing, OnResetOwnership, UpdateFirmwareExtension, CBiometricDeviceUSB::DeviceReset. Source path: `usdk5Core\src\{vfm,scs}`.
- extracted/synaWudfBioUsb_hiEntropy_0x160000.bin: a 496 KB high-entropy region of the DLL (file offset 0x160000–0x1dc000). It's probably the embedded or encrypted firmware or the matcher.
- extracted/synaWudfBioUsb_resources/: only the message table and version info. No firmware.
- The WinBioDatabase files in C:\Windows\System32\WinBioDatabase were NOT copied (they're per-user and Windows-encrypted). The sensor's DB is 7B5D3242-….DAT.

## Decrypted calibration and USB capture (third pass)
- CalibrationData decrypted as SYSTEM (DPAPI, SYSTEM CurrentUser scope) to extracted/CalibrationData_decrypted.bin (26244 bytes). It is a struct dump that contains heap pointers, followed by per-pixel baseline values (~0x88). It is sensor calibration, NOT pairing keys.
- There is no `PairingData` value anywhere in HKLM. The pairing/TLS state is probably stored on the sensor or derived at runtime.
- USB descriptor (from ETW): class FF / subclass 10 / protocol FF, bcdUSB 2.00, bMaxPacketSize0 8, bcdDevice 0x0154, iSerial 1, 1 configuration.
- Endpoints:
  - 0x01 OUT bulk 64
  - 0x81 IN bulk 64
  - 0x82 IN bulk 64
  - 0x83 IN interrupt 8 (bInterval 4)
  - 0x84 IN interrupt 16 (bInterval 10)
- Windows USB ETW events (USBXHCI, UCX and USBHUB3) for this sensor only, recorded during a WinBioIdentify (kept locally, not published), show timing, endpoints and transfer lengths, but **no payload bytes**, because ETW logs only buffer pointers. The endpoints above and the transfer lengths of the Windows identify sequence come from them.
- USBPcap (installed, on \\.\USBPcap1) did NOT see any traffic from this sensor, even with -A and after re-enumeration. Every USB restart changes the device address.
- To get real payloads: run this Windows driver in a QEMU/KVM VM with USB passthrough of 06cb:0078 and capture on the Linux host with usbmon/Wireshark. That is how python-validity's protocol was reversed.

## To do on Linux
    lsusb -v -d 06cb:0078 > lsusb.txt   # endpoints and descriptors

`lsusb -v` prints the reader's serial, so keep that file private; `.gitignore` excludes `/lsusb*.txt`.
For protocol captures, run Windows in a VM with USB passthrough and record with usbmon or Wireshark.

## libfprint driver: hardware check (A3)
Opens and closes the sensor through the libfprint fork's `synaptics0078` driver. Design: docs/brief-libfprint-driver.md.

Prerequisites:
- The fork built in ~/Devel/Linux/Synaptics/libfprint/build-gi (another checkout: set `LIBFPRINT_FORK`).
- The uaccess udev rule from proto/README.md.
- fprintd stopped: `sudo systemctl stop fprintd`. D-Bus can restart it, so also `sudo systemctl mask --runtime fprintd` for the session, and `sudo systemctl unmask --runtime fprintd` afterwards.

Step 1, write the data files (dev-only; A6's setup tool grows from it):

    proto/.venv/bin/python tools/synaptics0078_data.py

It writes module.bin and program-<serial>.bin into local/synaptics0078/ (gitignored, dir 0700, files 0600), from driverstore/synaWudfBioUsb.dll and extracted/CalibrationData_decrypted.bin. Options: `--dll`, `--calibration`, `--out`, `--force`.

Step 2, run the check:

    tools/dev/a3-open.sh

- `--dry-run`: preflight only. `--data-dir DIR`: use another data directory. `--missing-files`: run against an empty directory to show the documented error.
- Success: the driver lines "ROM version 6.20 ...", "Sensor type 0x2449 ...", "Serial ...", "Data directory ...", "Capture module: ...", then `open: OK`, `close: OK`, `PASS: open and close succeeded`, exit 0.
- `--missing-files` success: `PASS: --missing-files: open FAILED with FPrint.DeviceError.GENERAL naming .../module.bin`, exit 0.
- Logs go to local/logs/ (gitignored, 0600). They contain the unit serial: do not share them.

## libfprint driver: hardware check (A4)
Captures images through the libfprint fork's `synaptics0078` driver (activate, finger on, image, finger off). Design: docs/brief-libfprint-driver.md.

Prerequisites: as for A3 (fork built, uaccess rule, fprintd stopped or masked, data files in local/synaptics0078).

Run from the project root:

    tools/dev/a4-capture.sh

- Options: `--count N` (default 3), `--data-dir DIR`, `--dry-run`. Env: `LIBFPRINT_FORK`, `A4_TIMEOUT_S` (per attempt, default 60).
- Per image it prompts "Capture i/N: touch the sensor now", "Finger detected: keep still, then lift when told", "Image taken: lift your finger now". A retry prints "retry: <message>, touch again" (up to 3 attempts per image); a retry is not a failure.
- Per image it prints the delivered size and upscale factor, the PGM path, stats ("min X, max Y, mean M, sd S" plus ridge coverage), attempts, and "finger off N ms after image taken, capture completed M ms after image taken". The capture completes only after the finger is lifted (the core waits for finger-off).
- Success: driver lines (ROM version, "Activation: capture module ...", "Finger off after ..."), then `PASS: 3/3 images delivered`, then `images: .../local/captures/a4-<stamp>-NN.pgm; log: ...`, exit 0.
- Images (native 144x56 PGM) go to local/captures/ and the log to local/logs/; both gitignored, mode 0600. The log contains the unit serial: do not share it.
- The terminal shows only prompts and results; the debug lines go to the log.
- What to look at: the images resemble proto's captures in stats; finger-off is reported shortly after the lift (about one probe cycle, 500-1000 ms).

## libfprint driver: enroll and verify with fprintd (A5)
Runs the system fprintd (its unit and sandbox unchanged) against the libfprint fork, then enrolls and verifies a finger. Dev only: A6 replaces this with a package. Design: docs/brief-libfprint-driver.md.

Prerequisites:
- The fork rebuilt in ~/Devel/Linux/Synaptics/libfprint/build (prefix /usr/local, `synaptics0078_data_dir` left at /var/lib/fprint/synaptics0078). Another checkout: `sudo LIBFPRINT_FORK=DIR tools/dev/a5-fprintd-dev.sh install`.
- The data files in local/synaptics0078 (A3, step 1).
- fprintd not masked: if you masked it for A3/A4, `sudo systemctl unmask --runtime fprintd`.

After the switch to the package (A6, below), the helpers of steps 4 to 6 run against the package instead of the dev install. They need the driver's journal lines, which the package's fprintd does not write by itself. So first run `sudo tools/dev/a5-fprintd-dev.sh log on`, and `sudo tools/dev/a5-fprintd-dev.sh log off` when testing is done (the lines include the unit serial). `tools/dev/a5-fprintd-dev.sh log status` shows which it is. Steps 1 to 3 and 8 are for the dev install only.

Steps (each is small; run them from the project root):

1. Rebuild after driver changes: `meson compile -C ../libfprint/build`.
2. Install, with raw image dumps on:

       sudo tools/dev/a5-fprintd-dev.sh install --diag

   - It upgrades the earlier install in place: it replaces the lib, updates the manifest and restarts fprintd. It prints "upgraded: lib sha ... -> ...; fprintd restarted".
   - It asks first: "--diag stores raw fingerprint images of every touch in /var/lib/fprint/synaptics0078-diag (root only). Continue? [y/N]". These are biometric data.
   - A plain `install` (without --diag) later turns the dumps off. The directory stays until `undo`.
   - Run it again after every rebuild. Nothing else is needed.
3. Optional check (shows diag on or off):

       tools/dev/a5-fprintd-dev.sh status

4. Enroll, one named finger per run. The finger is required; every prompt names it, so touch only with that finger:

       tools/dev/a5-enroll.sh right-index-finger --replace
       tools/dev/a5-enroll.sh left-index-finger

   - Finger names are fprintd's (left-thumb, left-index-finger, ..., right-little-finger); the short form `left-index` works too.
   - It opens with "Enrolling your LEFT INDEX finger: use only that finger for all 18 touches", checks that the installed lib is the current build (dev install) or that the debug lines are on (package, `log on`), and shows "Enrolled now: ..." (from fprintd-list).
   - If that finger is already enrolled it asks "Delete that print and enroll it again? [y/N]". `--replace` answers yes without asking. A password dialog may appear.
   - Order matters when a print was saved under the wrong finger's name: re-enroll that name first (`--replace`), then the other finger. Otherwise fprintd's duplicate check matches the misnamed print and stops the enroll.
   - It first says "First touch with your LEFT INDEX finger. fprintd first checks that this finger is not enrolled yet". This touch is not one of the 18.
   - Then, per touch: "Touch 3 of 18 with your LEFT INDEX finger (vary the position a little)", "Got it. LIFT your finger now", "Lifted. Touch again". Touch only when asked. Lift when told.
   - Extra lines can appear: "That touch did not count: <reason>", "The sensor still sees the last touch: lift your finger, then touch again", "Still detecting a finger; if your finger is off, just touch again".
   - It ends with "Done: your LEFT INDEX finger is enrolled as left-index-finger (18 of 18 touches). Next: tools/dev/a5-verify.sh --finger-used left-index --expect match --finger left-index-finger". On failure the last line starts "Your LEFT INDEX finger is NOT enrolled" and gives the command to run again. All other output is in local/logs/a5-enroll-<stamp>.log.
5. Verify, 10 rounds each. `--finger-used` is the finger you touch with; `--finger` is the enrolled print fprintd checks against (default right-index-finger):

       tools/dev/a5-verify.sh --finger-used right-index --expect match --finger right-index-finger
       tools/dev/a5-verify.sh --finger-used left-index --expect match --finger left-index-finger
       tools/dev/a5-verify.sh --finger-used right-middle --expect no-match --finger right-index-finger

   - Each round asks, for example, "Round 3/10: touch with your RIGHT MIDDLE finger, then lift". A retry line ends "touch again with your RIGHT MIDDLE finger".
   - It refuses a mix-up before starting: `--expect match` with a touched finger that is not the `--finger` print, or `--expect no-match` with the same finger.
   - At the end it prints a small table (match / no match / retries / errors) and "as expected: X/10". Success is 10/10 for each run.
   - The SIGFM scores are only in local/logs/a5-verify-<stamp>.log.
6. Suspend test helper (the laptop sleeps twice, about 3 minutes). Needs the right index finger enrolled, mains power and the lid open. Run it as yourself, never with sudo:

       tools/dev/a5-suspend.sh --dry-run
       tools/dev/a5-suspend.sh

   - The dry run only does the checks. Nothing sleeps. Run it first.
   - It refuses to start if putting the laptop to sleep would ask for authentication: when pam_fprintd serves polkit or sudo, such a prompt would ask for a finger.
   - It also refuses while a program blocks sleep, and names it (for example `omarchy-update (Omarchy update in progress)`). Let that program finish or close it, then run the test again.
   - Part 1: the laptop sleeps while idle, then 3 checks with the RIGHT INDEX finger.
   - Part 2: a check starts and waits for your finger. Do NOT touch the sensor. The laptop sleeps while the check waits (the lock-screen case). After waking, it says the check was interrupted by sleep (nothing to touch) or asks for a touch. Then 2 more checks.
   - Before each sleep: a 5 s countdown, then the screen goes dark. Wake it with the power button or a key, unlock the lock screen, go back to the terminal and press Enter.
   - If fprintd stops responding, the test stops and says so. Restarting fprintd needs sudo and is left to you.
   - It ends with PASS or FAIL per part; exit 0 only when both pass. Expected for part 2: the driver has no suspend handler, so libfprint should cancel the waiting check on sleep. "Interrupted", with the reader working again afterwards, is the right answer (details: research/steps/a5-suspend-prep.md).
   - The log is local/logs/a5-suspend-<stamp>.log (private, 0600): fprintd, journal and kernel USB lines, the reader's power state, suspend and resume times.
7. Results: the verify tables, and whether any "Still detecting a finger" note appeared. Keep the logs and /var/lib/fprint/synaptics0078-diag local: they hold biometric data and the unit serial.
8. Undo, when you want it gone:

       sudo tools/dev/a5-fprintd-dev.sh undo

   - It removes only what install created (the manifest in /usr/local/lib/libfprint-0078-dev lists it). Fprintd then runs with the packaged libfprint again.
   - It asks before deleting this driver's prints (`--yes-delete-prints` / `--keep-prints`) and before deleting the diag images: "Delete N diag images in DIR? [y/N]" (`--yes-delete-diag` / `--keep-diag`). Prints of other drivers are never touched.
   - "undo incomplete" means it kept something that changed since install. The manifest lists it. Sort it out, then run undo again.

Privacy: the journal and the logs hold the unit serial. The drop-in's G_MESSAGES_DEBUG (the dev drop-in, or the one `log on` writes) also writes match scores and event bytes to the journal. Do not share them. The journal lines stay after undo. To drop them: `sudo journalctl --rotate --vacuum-time=1s`. This deletes every archived journal of every service, not only fprintd's.

## libfprint driver: switch to the package (A6)
The package `libfprint-sigfm-0078` replaces Arch's `libfprint` for every reader (all upstream drivers, SIGFM and `synaptics0078`). It ships no per-unit data: the data files (`module.bin`, `program-<serial>.bin`) stay in `/var/lib/fprint/synaptics0078`, and `synaptics0078-setup` installs and checks them. Your enrolled fingers stay valid: the package builds the same SIGFM matcher and driver settings as the A5 dev install (fork commit f9c60a25 adds to 40cda345 only the replay test, its emulation-only module pin and one core warning fix). Design: docs/brief-libfprint-driver.md.

Steps (run them from the project root):

1. Build as yourself (no sudo). It takes about 2 minutes. This is the A6 build as it was run: the dependencies were already installed and the install is step 2, so it has no `-s` and no `-i`. For a normal install, `makepkg -si` (see "Install" at the top) does both in one go. Use the AUR package (see "Package" below: `git clone https://aur.archlinux.org/libfprint-sigfm-0078.git && cd libfprint-sigfm-0078 && makepkg -f`), or the in-tree copy (the same file with b2sums `SKIP`):

       cd packaging/arch/libfprint-sigfm-0078 && makepkg -f

   - It makes TWO files. Install only `libfprint-sigfm-0078-1.94.100.r25.gf9c60a2-3-x86_64.pkg.tar.zst`, not the `-debug-` one.
   - A line "check: egis_etu905 was the only failure (known upstream flake); rerun 1 of 3" can appear. That is normal: it is a test of another vendor's reader that also fails at random upstream. The build still fails on any other test failure.
   - Keep the `.pkg.tar.zst` file, so you can reinstall without rebuilding.
2. Install it:

       sudo pacman -U libfprint-sigfm-0078-1.94.100.r25.gf9c60a2-3-x86_64.pkg.tar.zst

   - pacman asks `:: libfprint-sigfm-0078-1.94.100.r25.gf9c60a2-3 and libfprint-1.94.100-1 are in conflict. Remove libfprint? [y/N]`. Answer **y**.
   - Then `Proceed with installation? [Y/n]`: press Enter. It then runs hooks (udev hardware database and so on).
   - Until step 3, fprintd still runs the dev copy, so nothing changes yet.
3. Hand over from the dev copy:

       sudo tools/dev/a5-fprintd-dev.sh undo --keep-data

   - It removes the dev drop-in and `/usr/local/lib/libfprint-0078-dev`, and stops fprintd (it restarts by itself with the package's library).
   - It keeps the data files, your enrolled fingers and the diag images. It prints the command that deletes the diag images: do not run it now, that is decided later, separately.
   - It refuses if step 2 was not done. Plain `undo` (without `--keep-data`) now refuses too once the package is installed, because it would delete the data files.
   - If it prints FAILED for systemctl, running the same command again is safe.
4. Check the data files:

       sudo synaptics0078-setup status

   Expect ok lines for the folder, `module.bin` and `program-<serial>.bin`, and exit code 0.
5. Check the fingers: `fprintd-list "$USER"` lists your enrolled fingers (the same as before).
6. Turn on the driver's journal lines, which the A5 helpers need (the package's fprintd does not write them by itself):

       sudo tools/dev/a5-fprintd-dev.sh log on

   - It writes `/etc/systemd/system/fprintd.service.d/synaptics0078-debug.conf` and restarts fprintd. Running it again changes nothing.
   - It refuses while the dev drop-in is still there: then step 3 was not done.
   - If it prints FAILED for systemctl, running the same command again finishes the job (the same holds for `log off`).

   Then do not touch the reader for at least 10 seconds, and run:

       tools/dev/a5-verify.sh --finger-used right-index --expect match --rounds 3

   Expect 3 matches.
7. After the next reboot (the power setting only applies then), find the reader's power setting:

       for d in /sys/bus/usb/devices/*; do [ "$(cat $d/idProduct 2>/dev/null)" = 0078 ] && cat $d/power/control; done

   It should say `auto` (it said `on` with the old library). Leave the reader alone for at least 10 seconds, then run the a5-verify command of step 6 again. This checks that the reader still works after being powered down while idle.
8. When testing is done, turn the journal lines off again, because they include the unit serial:

       sudo tools/dev/a5-fprintd-dev.sh log off

   It removes the drop-in and restarts fprintd. The lines already in the journal stay; the A5 privacy note above says how to drop them.

Data files on another machine, or later:
- Without Windows (B5, below): `sudo synaptics0078-setup install`.
- With Windows files (phase A): run `tools/synaptics0078_data.py` as your user (it needs the Windows driver files). It writes `local/synaptics0078`.
- Then `sudo synaptics0078-setup install --from local/synaptics0078`. `--force` replaces a file that differs; `--dry-run` only prints what it would do.
- fprintd reads the files at the next device open: no restart.

*Update: superseded by "Install (Arch Linux, Omarchy)" at the top, which runs Omarchy's setup safely once `fprintd` and `usbutils` are installed. The lock-screen and Remove-then-Setup notes below still apply.*

**Warning: do NOT run Omarchy's "Setup Fingerprint" (`omarchy-setup-security-fingerprint`) for now.**
- With this package it no longer swaps the library (the package provides `libfprint-git`, which is what it checks).
- But after enrolling it turns fingerprint on for sudo, polkit and the lock screen (`/etc/pam.d/omarchy-lock-fingerprint`) without asking. Decide that yourself, knowing the false-accept limits below (B3: one person tested). It also enrolls the right index finger again.
- Do not run "Remove Fingerprint" and then "Setup Fingerprint" either: removal uninstalls fprintd, and the next setup would then install Omarchy's `libfprint-git` over this package without asking (`--ask 4`).

Going back to the stock library: `sudo pacman -S libfprint`, and answer y to removing `libfprint-sigfm-0078`. If no stock package is in the pacman cache, it downloads one. The stock library has no driver for this reader; the data files and enrolled fingers stay on disk.

## Fingerprint at the SDDM login screen
Needs the A6 package and at least one enrolled finger (`fprintd-enroll`, as yourself). `tools/sddm-fingerprint.sh` edits `/etc/pam.d/sddm`:

    sudo tools/sddm-fingerprint.sh on       # turn on
    tools/sddm-fingerprint.sh status        # on or off, no root, changes nothing
    sudo tools/sddm-fingerprint.sh off      # turn off

- `on` checks first: the file, `pam_fprintd.so` and `pam_unix.so`, the anchor line `auth include system-login`, and a print enrolled for the sudo user (it lists the fprint data dir; it does not open the reader).
- It inserts a marked 4-line block (two comment markers, two auth lines) before the first `auth include system-login` line:

      auth        [success=1 new_authtok_reqd=1 default=ignore]  pam_unix.so  try_first_pass likeauth nullok
      auth        sufficient  pam_fprintd.so  timeout=10

- This is the ArchWiki "password first, empty Enter for fingerprint" recipe. A typed password succeeds in the first `pam_unix` and skips `pam_fprintd`, so the original stack runs unchanged and the keyring and `pam_ssh` still get the password. An empty password plus Enter falls through to `pam_fprintd`. `timeout=10` is its minimum.
- Rejected: fingerprint-only or fingerprint-first stacks. They wait for a finger before the password and starve the keyring and `pam_ssh` of a password on every login.
- The first `on` keeps the original as `/etc/pam.d/sddm.before-fingerprint` (never overwritten). Writes are atomic; `on` and `off` are idempotent. `off` restores the file byte for byte and keeps the backup. If the block was edited by hand, `on` and `off` refuse and change nothing.

Test: log out (or reboot). At the login screen leave the password empty, press Enter, and touch the reader within 10 seconds. Typing the password works as before.

Recovery if the login screen refuses you: press Ctrl+Alt+F3, log in on the console, and run the `Undo:` line that `on` printed (it has the script's full path, as the console starts in your home directory). Or restore the backup: `sudo cp /etc/pam.d/sddm.before-fingerprint /etc/pam.d/sddm`.

Caveats:
- After a fingerprint login `pam_ssh` gets no password, so SSH keys are not unlocked. A password-protected login keyring is not unlocked either (Omarchy's default keyring has no password).
- A mistyped password falls through to `pam_fprintd` and waits up to 10 seconds for a finger before failing.
- The theme shows no "touch the reader" prompt. In `/usr/share/sddm/themes/omarchy/Main.qml` the Enter handler (lines 104-107) calls `sddm.login(root.currentUser, password.text, root.sessionIndex)` unconditionally, so an empty field is submitted. Its `Connections` only handles `onLoginFailed` and `onLoginSucceeded`, with no `informationMessage` handler, so PAM info messages are not shown.
- A failed fingerprint attempt counts as one faillock failure.

## Linux-built capture program (B2b)
Proves that a capture program built only from the Linux calibration measurements (proto/calibrate.py) works with real fingers, checked against your existing enrolled prints. Compare it with the program the package uses today (built from the Windows calibration data). Nothing is enrolled again.

Prerequisites:
- A PASS run of `proto/calibrate.py` in `local/calib/<stamp>` (use your run's directory name for `<stamp>` below).
- The package installed (A6), with both fingers enrolled (A5 step 4).
- `sudo tools/dev/a5-fprintd-dev.sh log on` (the verify runs need the driver's journal lines).

Steps (run from the project root):

0. Build the Linux program. Offline, no USB, no sudo:

       proto/.venv/bin/python proto/calib_program.py local/calib/<stamp>

   - It checks that the run's verdict is PASS and that the four chunk files are there.
   - Self-check: the same code must rebuild the Windows program byte for byte from the Windows calibration data. If either file is missing it says so and skips this. If the check fails, nothing is written.
   - It writes `local/calib/<stamp>/program-linux.bin` (private, 0600), then prints a table of how much each chunk differs from the Windows program (bytes differing, mean and max difference).
   - An identical existing file is left alone. A different one is refused unless you add `--force`.
1. Baseline with today's (Windows) program:

       tools/dev/b2-program.sh status
       tools/dev/a5-verify.sh --finger-used right-index --expect match --finger right-index-finger
       tools/dev/a5-verify.sh --finger-used left-index --expect match --finger left-index-finger

   - `status` should say "Windows". Without sudo it only tells you to use sudo for the details.
   - Each verify run is 10 rounds. Success is 10/10.
2. Switch to the Linux program:

       sudo tools/dev/b2-program.sh use-linux local/calib/<stamp>

   - The first time, it saves the Windows program as `program-<serial>.bin.b2-windows-backup` in /var/lib/fprint/synaptics0078. The driver ignores that name; `synaptics0078-setup status` lists it as "other file". It is never overwritten.
   - It checks the file with the setup tool's rules, stops fprintd if it runs (the driver reads the program when it opens the reader; D-Bus starts fprintd again on demand), and prints the next step.
   - Details go to `local/logs/b2-program-<stamp>.log` (private, holds the serial).
3. The same two verify runs on the Linux program:

       tools/dev/a5-verify.sh --finger-used right-index --expect match --finger right-index-finger
       tools/dev/a5-verify.sh --finger-used left-index --expect match --finger left-index-finger

4. Keep the Linux program, or go back:

       sudo tools/dev/b2-program.sh use-windows

   - It restores the backup byte for byte (the backup stays). `tools/dev/b2-program.sh status` says which program is in use: Windows, Linux (and from which run) or unknown.
   - While the backup exists, `a5-fprintd-dev.sh undo` keeps the data directory and warns.
   - Exit codes: 0 done or nothing to do, 1 refused or failed, 2 wrong use or needs root, 130 interrupted.
5. Results: the two verify tables of each program (four in all). The per-round results and SIGFM scores are in `local/logs/a5-verify-<stamp>.log` ("round N: ..., sigfm scores ..." lines); that is where the two programs are compared. Keep the logs local. When done: `sudo tools/dev/a5-fprintd-dev.sh log off`.

## False-accept benchmark (B3)
Estimates how often a wrong finger is accepted (the false-accept rate per verify) at threshold 105 and at lower thresholds, with 95% upper bounds. This decides whether the lock screen and SDDM may use the fingerprint, and whether lowering the threshold is safe (brief D6/B3).

Capture (run as yourself, no sudo; it reads the system libfprint, package `libfprint-sigfm-0078`, directly, fprintd is not involved):

    tools/dev/b3-capture.sh

- It captures all 10 fingers, 20 touches each, for the person "me". About 3 s per touch and 3 s per finger: about 11 minutes.
- Options: `--fingers right-index,left-index` (a subset; labels are left-/right- thumb, index, middle, ring, little), `--touches N`, `--person NAME` (a consenting guest; their images get their own directory), `--dry-run` (checks only, the reader is not touched).
- You see an intro, then per finger a banner and a pause of about 3 s, then "Finger 3 of 10: LEFT MIDDLE. Touch 4 of 20..." with TOUCH and LIFT. Touch naturally, the way you unlock.
- "touch again" means the touch was a duplicate, a retry or a bad image. Just lift and touch again.
- Ctrl-C stops safely. Running the same command again the same day continues where it stopped (complete fingers are skipped).
- If the reader is busy: close any fingerprint prompt (fprintd holds the reader only while a prompt is open) and run again. The tool prints the hint.
- If permission is denied: the tool prints what to check. The rule is the uaccess udev rule `/etc/udev/rules.d/70-fingerprint-06cb-0078.rules`.
- Second day (cross-day recognition): on a later day run `tools/dev/b3-capture.sh --fingers right-index,left-index`. The analysis then reports the cross-day FRR.

Analysis (no sensor, no root):

    tools/dev/b3-analyze.sh

- Options: `--k`, `--seed`. It builds the offline scorer from the libfprint fork (`LIBFPRINT_FORK`, default ~/Devel/Linux/Synaptics/libfprint, build dir `build/`).
- First it proves that the scorer reproduces fprintd's live scores exactly on the earlier diagnostic data (parity: 32 captures, 219 stage scores). If not exact, it refuses to continue.
- Then it scores every image pair once (a few minutes for 200 images, cached afterwards) and prints a summary: the impostor score distribution, FAR and FRR at thresholds 105, 75, 50, 30, 20, 10 and 5, the 95% upper bounds at three correlation levels (the finger-pair level is the most conservative), and the threshold D6's margin rule allows, with the evidence scope.
- The full JSON goes to local/b3/results/.

Privacy: images and sidecars go to local/b3/<person>/<label>/ (files 0600, directories 0700). local/ is gitignored, so nothing biometric is committed. Debug goes to local/logs/b3-capture-<stamp>.log and b3-analyze-<stamp>.log (0600). No unit serial is stored in the sidecars. To remove a guest's data, delete local/b3/<their name>/.

## Setup without Windows (B5)
Status: built (B5a-c). On the hardware (2026-10-17), installing from a local cab and the network install passed, with 10/10 on both enrolled fingers. The offline run was not done on the hardware; it is covered by tests. Results: research/steps/b5-run.md. Design and decisions: research/steps/b5-setup-design.md.

`sudo synaptics0078-setup install` does it all on any 06cb:0078, with no Windows files:
- It downloads the public vendor cab and takes the 10500-byte module out of it.
- It builds the `02` template from the vendor DLL in the cab (the same file `proto/data/cmd02_template.bin` holds).
- It calibrates the reader on Linux (fingers off) and writes `module.bin` and `program-<serial>.bin` to `/var/lib/fprint/synaptics0078`.

Commands:

    sudo synaptics0078-setup install                    # idempotent; a second run says already set up
    sudo synaptics0078-setup install --from-file F      # F: cab, zip, exe, MSI, DLL, bare module or bare template; repeatable
    sudo synaptics0078-setup install --from DIR         # phase A, unchanged: files made on Windows
    sudo synaptics0078-setup recalibrate                # the previous program is kept in /var/lib/synaptics0078/backup/
    sudo synaptics0078-setup fetch [--from-file F]      # fills the cache only; no reader needed
    synaptics0078-setup status                          # exit 0 ready, 1 not

- `--dry-run` changes nothing. `--yes` replaces the Enter prompt with a 3-second countdown.
- Cache: `/var/lib/synaptics0078/vendor/` (root, 0700). Log: `/var/log/synaptics0078-setup.log` (0600, serial redacted).
- Sources, tried in this order: Microsoft Update Catalog 2019, 2020, their Internet Archive copies, the Origin PC zip, its Internet Archive copy.
- Behind a proxy: `sudo --preserve-env=https_proxy synaptics0078-setup install`.
- A downloaded archive is untrusted. A known one (six are pinned) is read in the tool; any other is read by an unprivileged child (setpriv as nobody, with prlimit). Nothing downloaded is executed. The module and template hash pins are the gate.
- fprintd is stopped if it is running (it restarts on demand).

Shared code: the Python package `synaptics0078/` at the repo root.
- `pins.py` module, template, skeleton and chunk pins, and the six known archives.
- `template.py` builds `cmd02_template.bin` from the vendor DLL. `vendor.py` extracts cab, zip, exe, MSI and DLL (bsdtar from stdin plus the standard library). `cfb.py` reads OLE/MSI files.
- `session.py` the USB transport, the command guard and the calibration sequence, shared by `proto/calibrate.py` and the setup tool.
- `calibration.py`, `calib_model.py`, `parsers.py` moved here from `proto/`. The `proto/` files are alias shims, so `import calibration` in `proto/` and `tools/` still works.
- Tests are in `synaptics0078/tests`.

Rebuild the template on a fresh checkout (`proto/data/` is gitignored), from the repo root:

    python3 -m synaptics0078.template --dll PATH --out proto/data/cmd02_template.bin

`--force` replaces an existing file that differs. The DLL comes from the vendor cab (`synaptics0078-setup fetch`, then see the cache) or from any of the archives above.

The three third-party payloads `proto/testdata/{iusearch_cmd02,toliknosov_cmd02,toliknosov_cmd03}.bin` are no longer tracked (gitignored, kept locally). Tests that use them skip when they are absent; synthetic fixtures cover the same logic.

On a clean clone (no `proto/data/`), the template-dependent tests do not skip: they run on a synthetic template (`proto/synth_template.py`, `synaptics0078/tests/fixtures.py`: the real layout, seeded random table bytes). Only tests whose point is this unit's real data, vendor or captured files, or the environment (built package, shellcheck, the fork checkout) still skip, each with a reason.

Test gate (from the project root):

    proto/.venv/bin/python -m pytest -q -p no:cacheprovider proto tools synaptics0078

Package (`packaging/arch/libfprint-sigfm-0078/PKGBUILD`, version `1.94.100.r25.gf9c60a2-3`):
- Two git sources, both pinned: the libfprint fork `https://github.com/rsd/libfprint.git` by commit (`_commit`, f9c60a25), and this project `https://github.com/rsd/fingerprint-06cb-0078.git` by release tag (`_tooltag`, v0.1.0). The version is static; there is no `pkgver()`. `prepare()` refuses a tag without `synaptics0078/`, the setup tool or `LICENSE`.
- The published, checksummed copy is the AUR package [`libfprint-sigfm-0078`](https://aur.archlinux.org/packages/libfprint-sigfm-0078), with real b2sums. The in-tree copy is identical except that its b2sums are `SKIP`: a repo cannot checksum its own release tag.
- A tool-only release bumps `_tooltag`, its b2sum and `pkgrel`. A new fork commit bumps `_commit`, its b2sum and `pkgver`.
- Depends: Arch's libfprint list plus `opencv libstdc++ python python-pyusb curl libarchive util-linux`. Checkdepends: `cairo umockdev python-pytest`. `check()` runs the fork's meson tests and the package and setup tests.
- It installs `/usr/lib/synaptics0078/synaptics0078/` (no tests, no `.bin`, pyc compiled at build time), `/usr/bin/synaptics0078-setup` and `/usr/share/licenses/libfprint-sigfm-0078/LICENSE-synaptics0078`.
- License: `LGPL-2.1-or-later MIT`. The fork is LGPL-2.1-or-later; this project's code (setup tool and Python package) is MIT (see "License" below).
- The `.install` file only prints what to run.
- `tools/test_package_audit.py` audits a built package when `SYNAPTICS0078_PKG` points at it.
- `tools/test_public_data.py` audits that no tracked project file (plus staged blobs) and no tracked file of the fork's `tests/synaptics0078/` (`LIBFPRINT_FORK` or `../libfprint`) shares a 16-byte window with private local data (module, DLL tables, template, per-unit programs, CalibrationData, calibration runs, images; fixed format headers excluded), that none holds the unit serial, and that the fork's `custom.pcapng` uses only the closed command list with replies that parse. Each local-data group skips with a reason when absent; it reads the copies under `local/` and needs no root.

Install from the AUR (as yourself; `makepkg -si` asks for sudo to install):

    git clone https://aur.archlinux.org/libfprint-sigfm-0078.git
    cd libfprint-sigfm-0078 && makepkg -si

An AUR helper works too. Then, with fingers off the reader, `sudo synaptics0078-setup install` (above), and enroll with `fprintd-enroll`.

Local development builds of this working tree (run as yourself, never with sudo):

    tools/dev/build-package.sh [--prepare-only]

- It snapshots the project's working tree into a throwaway git repo: tracked files plus untracked files that are not ignored, leaving out deleted files and tracked files that are now ignored. It takes the fork from `$LIBFPRINT_FORK` (default `../libfprint`, next to the project) at `${FORK_REV:-HEAD}`.
- It writes a PKGBUILD copy under `${BUILD_ROOT:-local/pkg-dev}` with `file://` sources pinned by commit and `SKIP` sums, then runs `makepkg -f -C -L`. Extra makepkg flags go in `$MAKEPKG_FLAGS` (for example `-s` to install missing dependencies). `--prepare-only` stops before makepkg.
- `MESON_TESTTHREADS` (parallel meson tests) defaults to 2.
- `BUILD_ROOT` must be empty or one the script created (it writes a marker file there). It refuses any other non-empty directory.
- The dev package's version: `pkgver` comes from the fork commit (`git describe`, the same scheme as the release) and `pkgrel` is `0.<UTC timestamp>`. So the AUR release of the same pkgver (pkgrel 3) upgrades over a dev build.
- It prints the built package path. Install it with `sudo pacman -U <printed path>`.
- It never touches the project's git index.

Upgrading the package (fingers off the reader during steps 3 and 4):

1. Optional backup of the data folder: `sudo cp -a /var/lib/fprint/synaptics0078 /var/lib/fprint/synaptics0078.bak`
2. Build and install the new version: in the AUR clone, `git pull && makepkg -si` (or your AUR helper's upgrade).
3. `sudo synaptics0078-setup status`. A package upgrade leaves `/var/lib/fprint/synaptics0078` untouched, so it should report ready (exit 0). If it does not, run `sudo synaptics0078-setup install`.
4. Wait 10 seconds with the reader untouched, then check a finger: `fprintd-verify` (touch when asked, then lift).

## libfprint driver: replay test (A7)
`tests/synaptics0078/` in the fork is a synthesized umockdev pcapng replay test. It runs in `meson test` and in the package `check()`, and you do nothing. It covers open with a bad and a good module, capture, an 18-stage enroll with the error and retry cases, a matching and a non-matching verify, and close. The trace is generated (serial, module, program and images are synthetic, nothing is recorded from a device), and the driver accepts the synthetic module hash only in emulation mode. Under a test wrapper such as valgrind the probe window is widened and the test needs a timeout multiplier (about 127 s).

## Acknowledgements
- SIGFM, the matcher: copyright 2022 Matthieu Charette, Natasha England-Elbro and Timur Mangliev. Natalie Klestrup Röijezon first carried it toward libfprint, and Tooniis wrote the merge request (MR !530) that proposes it upstream.
- Sergey Subbotin, whose SIGFM branch the fork is based on, and Omkar Chandorkar (gallery fix).
- The libfprint project and its contributors.
- [python-validity](https://github.com/uunicorn/python-validity) by uunicorn (MIT): `bitpack()` in `synaptics0078/calibration.py` and `research/calib/build_ours.py`, and the parts of `research/matcher/preprocess.py` and `research/matcher/sift_matcher.py` taken from PR #288 by ashh87.

## License
- This project's own code is licensed under the MIT License (SPDX `MIT`). `LICENSE` holds the MIT text.
- Files kept under other terms, because they are derived from libfprint's SIGFM or are a libfprint patch:
  - `research/matcher/sift_matcher.py`: the SIGFM constants and `Matcher.sigfm()` (from `libfprint/sigfm/sigfm.cpp`) are LGPL-2.1-or-later; the rest of the file is MIT (SPDX `MIT AND LGPL-2.1-or-later`).
  - `research/far-diag/sigfm_geometry.cpp`: the `Match` struct and `candidates()` (derived from SIGFM's `sigfm_match_score()`) are LGPL-2.1-or-later; the rest is MIT (SPDX `MIT AND LGPL-2.1-or-later`).
  - `research/matcher/libfprint-virtual-image-eval.patch`: a patch to libfprint, so LGPL-2.1-or-later like libfprint.
- The python-validity ports are MIT, with the upstream copyright lines and notice kept in each file's header: the PR #288 parts of `research/matcher/preprocess.py` and `research/matcher/sift_matcher.py`, and `bitpack()` (from python-validity's `sensor.py`) in `synaptics0078/calibration.py` and `research/calib/build_ours.py`.
- The libfprint fork (github.com/rsd/libfprint, branch `sigfm-0078`) stays LGPL-2.1-or-later.
- No vendor file or calibration is shipped. `synaptics0078-setup install` downloads the vendor's public driver archive, extracts the capture module and builds the `02` template from it, and measures the calibration, all on the user's machine at install time.
