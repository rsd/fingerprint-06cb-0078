# recon-driver: libfprint driver conventions for a 06cb:0078 image-sensor driver

Read-only research, 2026-10-14. Nothing was built or installed, no USB device touched.

## 0. Sources used

- LF = a read-only shallow clone of https://gitlab.freedesktop.org/libfprint/libfprint at `80a4b5e` "Release libfprint 1.94.100", 2026-07-26,
  i.e. exactly what Arch ships as libfprint 1.94.100-1. The same clone carried 3 local edits from the matcher evaluation
  (`meson.build`, `tests/meson.build`, `libfprint/drivers/virtual-image.c`, an eval patch). Line numbers below are from that working tree, which differs from HEAD only in those hunks; the pristine text is `git -C LF show HEAD:<path>`.
- SIGFM fork files (a flat local copy, not a git repo, provenance unknown; local working files, not published): `{elan.c,fpcmoh.c,fpi-image-device.[ch],fp-image.c,fpi-print.c,sigfm.cpp}`.
- umockdev source: a read-only shallow clone of https://github.com/martinpitt/umockdev.
- Installed system: fprintd 1.94.5-2, libfprint 1.94.100-1, `/usr/lib/systemd/system/fprintd.service`.
- Prototype: `proto/README.md` (flow: 01, 19, 75, [06+module], 02+program, EP83 events, 51, 04, 52).

---

## 1. Best templates

Key facts about our device that drive the choice: press sensor, fixed 144x56 8-bit frame, bulk OUT 0x01 / bulk IN 0x81, interrupt IN 0x83 for finger/ready events, command opcodes 01/19/04/52 (the same Validity command family), host-side matching, firmware module + program upload at activation.

Survey of every USB image driver in `LF/libfprint/drivers/` (scan_type from `dev_class->scan_type`; "int" = uses `fpi_usb_transfer_fill_interrupt`):

| driver | lines | scan | notes for us |
|---|---|---|---|
| **vfs7552.c** | 1072 | PRESS | Validity VFS7552 138a:0091. Same command family (`01`, `19`, `04`, `52`, `is_image_ready`), EP 0x01 OUT / 0x81 IN / **0x83 interrupt for finger-down**, fixed frame (112x112), `change_state` vfunc, SSMs per phase. Best protocol-lineage match. Messy (usb_action tables, 50 ms delayed state changes) and uses `fill_bulk` on the interrupt EP (see section 4 pitfall). |
| **nb1010.c** | 446 | PRESS | Smallest clean fixed-frame press driver: one loop SSM started from `change_state(AWAIT_FINGER_ON)`, `deactivating` flag, `fpi_device_get_cancellable()` on every transfer, frame assembled from partial reads, `fpi_image_device_image_captured` + `report_finger_status(FALSE)`. Polls finger over bulk (no interrupt). |
| aes3k.c (aes3500/aes4000) | 272 | PRESS | Tiny pattern: blocking read with timeout 0 + own `GCancellable` that is both "capture in flight" flag and the deactivate path (`aes3k_dev_deactivate`, `img_cb`). Good reference for the "wait for EP83 event, deactivate cancels it" part. |
| uru4000.c | 1503 | PRESS | The one real interrupt-EP image driver (EP_INTR via `fill_interrupt`, uru4000.c:399, handler :330-440), `change_state` switch (:1264-1324), checks interface on open (:1349), has a **pcapng** umockdev test with interrupt URBs. Needs openssl, large; use only for the interrupt-handling and open-time validation idioms. |
| vfs0050.c | 773 | SWIPE | Interrupt-on-0x83 SSM with `fpi_device_get_cancellable()` and timeout 0 (:539-560), cancellation-aware callback (:403-420). Swipe assembly is irrelevant. |
| upektc_img.c | 804 | SWIPE | Clean activate/capture/deactivate SSM triple, control+bulk, `usb_discover` (:759). No interrupt, stream-oriented; useful for SSM layout only. |
| elan.c | 1027 | SWIPE | Activate SSM = calibration, capture SSM, `stop_capture` SSM that ends with `report_finger_status(FALSE)` (:488-520), `fpi_device_emulation_mode_enabled` workaround (:560). |
| elanspi.c | 1713 | SWIPE | SPI, not USB. Only useful for: calibration SSM, `nr_enroll_stages = 7` (:1703), `fpi_device_emulation_mode_enabled` skips, `fpi_image_resize`. Not a template. |
| egis0570.c | 444 | SWIPE | Looping SSM + frame assembly; no finger-event channel. Not a template. |
| vfs5011/vfs101/vfs301/aes2xxx/aesx660/etes603/upeksonly | | SWIPE | Swipe line-assemblers or register-table drivers. Not templates. |
| secugen.c | 2345 | PRESS | New (2026) large fixed-frame driver; reads calibration *from the device's firmware*, not a file; emulation-mode shortcut at :2226. Reference for `fpi_device_emulation_mode_enabled`. |

**Template 1 (primary): `LF/libfprint/drivers/vfs7552.c` (+ `vfs7552_proto.h`).** Same vendor lineage and command set, same three endpoints incl. interrupt EP for finger detection, PRESS, fixed frame, real hardware-proven `change_state`-driven structure, `dev_class->id_table`/`img_width`/`img_height` declarations are the minimal shape we need. Copy its *structure* (open SSM -> activate -> change_state dispatch -> capture SSM -> stop SSM) but not its code style: do not copy the `usb_action` table machinery (we have only ~10 commands) and use `fpi_usb_transfer_fill_interrupt` for EP 0x83.

**Template 2 (secondary, code hygiene): `LF/libfprint/drivers/nb1010.c`.** Smallest, cleanest state machine for a fixed-frame press sensor; shows the `deactivating`-flag + SSM-callback idiom (`nb1010_check_fingerprint_cb`: `if (self->deactivating) fpi_ssm_mark_completed`) and the minimal GObject boilerplate (:98-99, :300-327). Pair with **aes3k.c** for the cancel-the-pending-read-on-deactivate trick, and **uru4000.c:330-440 / vfs0050.c:403-560** for how an interrupt callback is written (cancel error -> jump to cleanup state, unknown interrupt -> `FP_DEVICE_ERROR_PROTO`).

Fork-only reference (not upstream): `matcher/src/sigfm/fpcmoh.c` (FPC MOH, small USB sensor, SIGFM, 2x NN upscale, `interrupt_cancellable`). It also shows the SIGFM fork's driver-facing API (section 2). Treat its code quality with caution (no review trail).

---

## 2. Driver conventions

### 2.1 Files a new driver touches (upstream tree, version 1.94.100)

Driver key `<name>` (suggestion: `synaptics0078`; `synaptics` is already taken by the MOC driver) must be one C identifier used consistently, because it is both a meson key and a GType prefix:

| file | change |
|---|---|
| `LF/libfprint/drivers/<name>.c` (+ `<name>.h` / `<name>_proto.h`) | the driver. `G_DEFINE_TYPE (FpDevice<Name>, fpi_device_<name>, FP_TYPE_IMAGE_DEVICE)`; `dev_class->id` equals `<name>` (egis0570 passes `"egis0570"`, nb1010 uses `FP_COMPONENT`). `#define FP_COMPONENT "<name>"`, `#include "drivers_api.h"`. |
| `LF/meson.build` `drivers_info` dict (line ~104-157; vfs7552 at :120, nb1010 at :144) | add `'<name>': {}` (non-optional means it is in the `default` set; add `'helper': [...]` only for pixman/openssl/udev; `'endian_dependent': true` if you cast buffers to structs). |
| `LF/libfprint/meson.build` `driver_sources` dict (:85-140; vfs7552 :112) | `'<name>' : files('drivers/<name>.c')`. The generated `fpi-drivers.c` externs `fpi_device_<name>_get_type`, so the dict key and the GType name must agree. |
| `LF/tests/meson.build` `drivers_tests` dict (:34) | `'<name>': {}` (optional keys: `timeout`, `suite`, `test_types`...). Test dir name `tests/<name>[-<variant>]`; the part before the first `-` must be a driver key (`driver_test.split('-')[0]`). Skipped with exit 77 if the driver is not built. |
| `LF/tests/<name>/` | fixtures (section 4). |
| `LF/data/autosuspend.hwdb` | **generated, never hand-edited**: `meson compile -C <builddir> sync-udev-hwdb` (target defined in `libfprint/meson.build`, runs `fprint-list-udev-hwdb`, copies into `data/`). The meson test `udev-hwdb` (`tests/test-generated-hwdb.sh`, `UDEV_HWDB_CHECK_CONTENTS=1` when default drivers are enabled) fails if the committed file is stale. Output per USB id: `usb:v06CBp0078*` + `ID_AUTOSUSPEND=1` + `ID_PERSIST=0`, grouped under `# Supported by libfprint driver <name>`, driver sections sorted by id. |
| udev rules (`70-libfprint-2.rules`) | generated by `fprint-list-udev-rules.c`; it only emits lines for **SPI/udev-type** drivers. USB drivers need nothing there (installed `/usr/lib/udev/rules.d/70-libfprint-2.rules` has no 06cb lines). fprintd runs as root, so no uaccess rule is needed for the daemon. |
| `LF/libfprint/fprint-list-udev-hwdb.c` `allowlist_id_table` | "known unsupported devices" synced from the libfprint wiki. **06cb:0078 is not in it** (checked). 06cb:00a8 (same sensor type 0x2449) and 06cb:009a *are*. If a driver claims an id that is also in this list, the generator emits `g_warning ("... implemented by driver ...")`, and tests run with `G_DEBUG=fatal-warnings`, so the id must be removed from that list too. Only matters if we later add 00a8. |
| `fprint-list-supported-devices`, `org.freedesktop.libfprint.metainfo.xml` | generated from id tables automatically. |

Other conventions: CI builds each driver alone with `--werror` (`.gitlab-ci.yml:49` `meson setup _build --werror -Ddrivers=$driver`), so the driver must compile standalone; style is enforced by `scripts/uncrustify.sh` (`scripts/uncrustify.cfg`, GLib style with space before parens). `-Werror=implicit`, `-Wmissing-prototypes`, `-Wshadow` etc. are on (top `meson.build:60-80`); static functions only. Optional compile-time constants go through `libfprint_conf.set_quoted(...)` in top `meson.build` (`config.h` is generated; today it only carries `LIBFPRINT_VERSION`, `HAVE_*`).

### 2.2 Declarations in class_init

All verified in the sources named:

```c
dev_class->id         = "synaptics0078";            /* == driver key */
dev_class->full_name  = "Synaptics ... 06cb:0078";
dev_class->type       = FP_DEVICE_TYPE_USB;
dev_class->id_table   = id_table;                   /* { .vid=0x06cb, .pid=0x0078 }, { .vid=0 } */
dev_class->scan_type  = FP_SCAN_TYPE_PRESS;
dev_class->nr_enroll_stages = N;                    /* optional, see below */
img_class->img_open / img_close / activate / deactivate / change_state
img_class->img_width  = 144;  img_class->img_height = 56;   /* informational only */
img_class->bz3_threshold = T;                        /* upstream (NBIS) */
```

- **nr_enroll_stages**: base class sets it to `IMG_ENROLL_STAGES = 5` (`LF/libfprint/fp-image-device-private.h:24`, applied in `fp_image_device_class_init`, fp-image-device.c:214). A driver overrides it in its own `class_init` (elanspi.c:1703 uses 7 with comment "very hit or miss, may as well record a few extras"; the SIGFM-fork elan.c:1002 uses 15). Dynamic variant: `fpi_device_set_nr_enroll_stages()` from `probe`.
- **Image size / ppmm**: `img_width`/`img_height` are documentation only ("only provide if constant", `fpi-image-device.h:76-77`); nothing in the core reads them (grep: only the header). The real size is what you pass to `fp_image_new (w, h)`. `FpImage.ppmm` is a public field (`fpi-image.h:63`), defaults to 0, and only the NBIS minutiae code reads it (`fp-image.c:326`, `nbis/mindtct/quality.c:236`); the only driver that sets it is secugen.c:1682. So: **ppmm is irrelevant with SIGFM**, and with NBIS upstream compensates small sensors by integer upscaling with `fpi_image_resize (img, k, k)` (egis0570.c:201, aes3k.c "ugly hack to make the image big enough for NBIS", vfs7552.c scale loop), plus `FPI_IMAGE_COLORS_INVERTED | V_FLIPPED | H_FLIPPED | PARTIAL` flags (`fpi-image.h:25-40`) that the core normalises before extraction. (Fork drivers do a 2x nearest-neighbour upscale for SIGFM: fpcmoh.c:38.)
- **Matching algorithm**: upstream `FpImageDevice` hard-wires NBIS: `fp-image-device.c:128-129` (`fpi_print_set_type (enroll_print, FPI_PRINT_NBIS)`), `fpi-image-device.c:285,340` (`fpi_print_add_from_image`, `fpi_print_bz3_match` with `priv->bz3_threshold`, default 40). The SIGFM fork replaces the class field `bz3_threshold` by `score_threshold` and adds `img_class->algorithm = FPI_DEVICE_ALGO_SIGFM` (`matcher/src/sigfm/fpi-image-device.h`, elan.c:1008-1011, fpcmoh.c:1579-1580) and a new print type `FPI_PRINT_SIGFM` (`sigfm/fpi-print.c`). Consequence: **the driver source depends on which base we fork**; the SIGFM research (`research/recon/recon-sigfm.md`) should fix the base commit first. In the fork, "too few keypoints" (<25, `fp-image.c:332`) is turned by the core into `FP_DEVICE_RETRY_GENERAL` (`fpi-image-device.c:267-271`), so the driver does not need its own keypoint-quality retry.

### 2.3 State machine and reporting (all in `LF/libfprint/fpi-image-device.c`)

Core state machine (`valid_transitions`, :90-110): INACTIVE -> ACTIVATING -> IDLE -> AWAIT_FINGER_ON -> CAPTURE -> AWAIT_FINGER_OFF -> IDLE -> (AWAIT_FINGER_ON again for the next enroll stage | DEACTIVATING) -> INACTIVE. One activation spans an entire enroll/verify/identify/capture operation, so for an N-stage enroll our `02`+arm sequence must be redone per stage from `change_state(AWAIT_FINGER_ON)`, not only in `activate`.

| event | call | semantics (verified) |
|---|---|---|
| activation done | `fpi_image_device_activate_complete (dev, err)` | moves core to IDLE then AWAIT_FINGER_ON (:~620); error aborts the action. |
| finger on | `fpi_image_device_report_finger_status (dev, TRUE)` | in AWAIT_FINGER_ON -> core switches to CAPTURE (:~437-440) and fires `change_state`. Also updates FpFinger status for fprintd ("finger needed/present"). |
| image | `fpi_image_device_image_captured (dev, img)` | `g_return_if_fail (state == CAPTURE)`; starts async extraction; core goes to AWAIT_FINGER_OFF immediately. |
| finger off | `fpi_image_device_report_finger_status (dev, FALSE)` | only effective in AWAIT_FINGER_OFF; non-enroll -> deactivate; enroll -> next AWAIT_FINGER_ON after extraction finished. |
| retry | `fpi_image_device_retry_scan (dev, FP_DEVICE_RETRY_*)` | codes: GENERAL, TOO_SHORT, CENTER_FINGER, REMOVE_FINGER, TOO_FAST (`fp-device.h:123-127`). Enroll: reports progress+retry error and moves to AWAIT_FINGER_OFF (driver must then still report finger off); identify/verify: reports retry result and deactivates; capture: aborts action. Do not raise retry GErrors through `session_error` (core warns, :~590). |
| fatal | `fpi_image_device_session_error (dev, err)` | aborts whole action and deactivates. Never during deactivation; cancellation errors are swallowed. |
| deactivate | `img_class->deactivate (dev)` -> `fpi_image_device_deactivate_complete (dev, err)` | called on cancel, success, or error; the driver must stop the hardware (`04`,`52`) and then complete. In-flight transfers: the action cancellable (`fpi_device_get_cancellable (dev)`) fires only on user cancel; for normal deactivation keep your own `GCancellable` (aes3k.c:`img_capture_cancel`, fpcmoh `interrupt_cancellable`) and finish deactivation from the cancellation callback. |

How the existing press drivers handle the event channel: vfs0050.c waits on EP 0x83 with `fpi_usb_transfer_fill_interrupt (transfer, 0x83, 5)` + `fpi_usb_transfer_submit (transfer, 0 /*no timeout*/, fpi_device_get_cancellable (dev), interrupt_callback, NULL)` and treats `G_IO_ERROR_CANCELLED` during deactivation as the normal exit (:403-420, :539-560).

**Design point to flag (finger-off):** the prototype found no finger-up event on 0x83. Upstream drivers either (a) report finger-off immediately after the image and rely on the next AWAIT_FINGER_ON detection (nb1010.c:266, aes3k.c "FIXME: rather than assuming finger has gone, we should poll regs until it really has"), (b) re-capture and threshold image variance until the finger is gone (vfs7552.c:`FINGER_OFF_VARIANCE_THRESHOLD`, CAPTURE_COMPLETE state), or (c) stop-command then report off (elan.c:488-520). With a resting finger (a) would immediately re-trigger a duplicate enroll stage; (b) needs extra `02`+`51` round trips or a cheap frame. This needs a decision in the brainstorm, using the prototype's "finger probably not lifted" heuristic (mean abs diff < 3) as a possible basis.

---

## 3. External data (firmware / calibration)

### 3.1 Upstream precedent: none

- No driver under `LF/libfprint/drivers/` reads firmware or calibration from a file. Greps for `g_file_*`, `GFile`, `fopen`, `g_key_file`, `g_resource`, `g_get_user*`, `g_get_system*`, `g_build_filename` over `libfprint/` (excluding nbis) find exactly: `fpi-spi-transfer.c:109` (`g_file_get_contents` of the spidev bufsiz sysfs parameter, falling back to a constant plus `g_message` on failure), `fpi-device.c:112` (emulation helper module path), and device-node `open()` in elanspi.c:104,1565 (paths come from udev, not data files).
- All calibration data in tree is either read **from the device** (secugen.c:640-745 reads FW calibration over USB and degrades with a warning if absent; aes2550 `READ_CALIBRATION_DATA`; goodix/egis report firmware version and tell the user to use fwupd, goodix.c:1055-1064) or **compiled in** (elanspi calibration tables, elan registers, vfs/validity command blobs in headers).
- Environment variables used by drivers: `FP_DEVICE_EMULATION` (via `fpi_device_emulation_mode_enabled`, which only works if the test-emulation helper library is loadable, `fpi-device.c:96-125`), `FP_VIRTUAL_*`, `MAFP_ENV_ENROLL_SAMPLES` (mafpmoc.c:2543). So an env-var data-dir override restricted to emulation mode would be a new but unsurprising idiom.

### 3.2 Upstream policy on proprietary blobs (`LF/HACKING.md`, section "License clarification")

Quote: "no code to integrate proprietary drivers will be accepted in libfprint upstream. Proprietary drivers would make it impossible to debug problems in libfprint, as we wouldn't know what the proprietary driver does behind the library's back." They encourage using the source to reverse-engineer a free driver. Contribution rule for maintainers writing drivers: "3 stand-alone devices ... specifications of the protocol" (HACKING.md "Contributing"); contributors send merge requests. `MAINTAINERS`: synaptics MOC is vendor-maintained; most others unmaintained.
Reading: a driver whose operation requires a vendor-DLL-derived firmware module and a Windows-calibration-derived program is **not upstreamable** (policy text is about code, but a mandatory proprietary blob is the same problem and the data cannot be reproduced from the repo). That matches the decision to run a fork; `research/recon/recon-packaging.md` covers how the fork replaces `/usr/lib/libfprint-2.so.2`.

### 3.3 fprintd sandbox (read from `/usr/lib/systemd/system/fprintd.service`, 1.94.5-2)

```
ProtectSystem=strict          -> whole file system read-only for the daemon, except /dev /proc /sys
ReadWritePaths=/sys/devices   -> only exception besides StateDirectory
StateDirectory=fprint         -> /var/lib/fprint, writable, mode 0700, root
StateDirectoryMode=0700
ProtectHome=true              -> /home, /root, /run/user are invisible (!)
PrivateTmp=true               -> own /tmp
ProtectKernelTunables/Logs/Modules/ControlGroups, ProtectClock
DeviceAllow=char-usb_device rw, char-spi rw, char-hidraw rw, /dev/cros_fp rw
SystemCallFilter=@system-service
MemoryDenyWriteExecute=true
RestrictAddressFamilies=AF_UNIX AF_LOCAL AF_NETLINK
NoNewPrivileges=true
```

Implications:
- Anything in `/home/...` (including the current prototype's `driverstore/synaWudfBioUsb.dll` and `extracted/CalibrationData_decrypted.bin`) is **unreachable** from the daemon. The runtime must not read the DLL; ship an extracted 10500-byte module file and a built 18869-byte program file.
- `/usr`, `/etc`, `/var` are all **readable**; only `/var/lib/fprint` is writable. Reading is all we need.
- `MemoryDenyWriteExecute=true` is a point for the SIGFM work (`research/recon/recon-sigfm.md`): OpenCV/JIT-style W+X mappings in the matcher would break under this unit (cannot be verified here; SIGFM-in-C++ is what the fork links).
- Hotplug/suspend: the hwdb entry sets `ID_PERSIST=0` + autosuspend, so a USB reset at resume wipes the RAM-resident module; the driver must test the module state (RomInfo byte 17, per the prototype) at every activation/open and never cache "already loaded".

### 3.4 Where the two blobs should live (recommendation)

Two files: `module.bin` (10500 B, same for every unit, sha256 pinnable) and the per-unit program (18869 B).

- **Recommended: one directory `/var/lib/fprint/synaptics0078/`** (compile-time default, e.g. a meson string option baked via `libfprint_conf.set_quoted`, built from `get_option('localstatedir')`; with `--prefix=/usr` meson resolves localstatedir to `/var`). Files: `module.bin` and `program-<serial-hex>.bin` (serial = the 6 bytes at RomInfo 0x12-0x17 the driver already reads). Why: it is the one directory the unit file already creates, owns and permits (0700 root, survives `pacman -Syu`/package removal), so no packaging or ownership question; it keeps vendor-derived and unit-specific data private to root; a single provisioning tool (the Python prototype's `calibration.py` + a small DLL extractor) writes it once. Caveat: it shares the tree with fprintd's per-user print storage (`/var/lib/fprint/<user>/<driver>/<device_id>/`); I did not verify fprintd never enumerates the base directory, so use a name that cannot be a username (a driver-id directory is fine in practice; a dot-prefixed name is safer).
- Runner-up: `/usr/share/libfprint/synaptics0078/module.bin` (static, package-managed, good if an AUR package extracts it at build time from a user-supplied DLL) plus the per-unit file under `/etc/libfprint/synaptics0078/` (host-specific, `backup=`). More moving parts; only worth it if the fork gets packaged for other people.
- Precedents outside libfprint: python-validity keeps pairing/config in `/etc/python-validity/`, runtime data in `/var/run/python-validity/` (`validitysensor/init_data_dir.py`), tools in `/usr/share/python-validity/`; kernel-style firmware lives in `/usr/lib/firmware/`. Not recommended: `/tmp` (PrivateTmp), `/home`, `/run/user`, anything needing `ReadWritePaths`.
- Test override: honour an env var (for example `FP_SYNAPTICS0078_DATA_DIR`) **only** when `fpi_device_emulation_mode_enabled ()` is true, never in production.

### 3.5 API and error handling

- Load with plain GLib in `img_open` (sync is acceptable: libfprint's own `fpi-spi-transfer.c:109` does a blocking `g_file_get_contents`; 29 KB is trivial). Verify before use: size (`10500`, `18869`) and `g_compute_checksum_for_data (G_CHECKSUM_SHA256, ...)` for the module (the prototype already pins its sha256). Keep the buffers in the instance struct (`g_bytes`/`g_free` in `img_close`), pass them to `fpi_usb_transfer_fill_bulk_full (transfer, EP_OUT, buf, len, NULL)` (caller-owned buffer, see upektc_img.c:94-101) so nothing is copied.
- Missing or invalid blob: there is no upstream convention to copy, so follow the nearest ones. Two layers: (1) **probe** (`dev_class->probe`, see 5.2): check the files exist; failing makes libfprint drop the device so fprintd reports no device and logs the message (the synaptics driver hides a device with "unsupported firmware version" this way, synaptics.c:1217-1245). (2) **open**: `fpi_image_device_open_complete (dev, fpi_device_error_new_msg (...))` with `FP_DEVICE_ERROR_GENERAL` for missing file (precedent: "Could not find interface", uru4000.c:1366), `FP_DEVICE_ERROR_DATA_INVALID` for size/hash mismatch, `FP_DEVICE_ERROR_NOT_SUPPORTED` for wrong ROM/sensor type, and put the path and the provisioning hint in the message. Do not fall back to sending partial/garbage data; the module goes to the sensor's RAM.

---

## 4. Testing (umockdev)

### 4.1 Layout and registration (verified)

- `LF/tests/<name>[-<variant>]/` holds `device` (umockdev device description), `capture.pcapng` (or the older `capture.ioctl`), `capture.png` (expected image), and optionally `custom.py` + `custom.pcapng` (MOC drivers: synaptics, goodixmoc, egismoc, ...). Existing image-driver fixtures: `egis0570`, `upektc_img`, `uru4000-4500` (pcapng); `nb1010`, `vfs7552` (ioctl).
- Registration: only the `drivers_tests` dict in `LF/tests/meson.build:34`. `tests/meson.build` then creates one meson test per type found (`capture`, `custom`) running `tests/umockdev-test.py <dir> --test <type>` with `FP_DRIVERS_ALLOWLIST=<driver>` and `FP_DEVICE_EMULATION=1`, suites `drivers` + type, default timeout 15 s (`timeout` per-driver override).
- Run: `tests/umockdev-test.py` calls `umockdev-run -d device -p <sysfs path>=capture.pcapng -- python tests/capture.py <out.png>`; the capture script opens the first device, `capture_sync (True)`, writes a PNG; the runner then requires the PNG to be **bit-identical** to the stored `capture.png` (`cmp_pngs`). Exit 77 (skip) if `umockdev-run` is missing or older than 0.16.3 (pcap support).

### 4.2 How a test is recorded from real hardware (`LF/tests/create-driver-test.py.in`, `tests/README.md`)

1. Build libfprint with the driver and introspection (`-Dintrospection=true`, required to run any driver test). The meson build generates `<builddir>/tests/create-driver-test.py`.
2. As root: `sudo <builddir>/tests/create-driver-test.py [--test capture|custom] <driver> [variant]`. The script relaunches itself with `LD_LIBRARY_PATH`/`GI_TYPELIB_PATH` to the build tree and **`FP_DEVICE_EMULATION=1`**, sets `FP_DRIVERS_ALLOWLIST=<driver>`, requires exactly one supported reader plugged in, then:
   - runs `umockdev-record /dev/bus/usb/BBB/DDD > tests/<name>/device`;
   - **resets the USB device** (`usb_device.reset ()`, "as descriptors could be missing in the dump otherwise") so every recording starts from a fresh device state (for us: ROM state, module not loaded, so the recording includes `06`+module);
   - starts `tshark -i usbmon<bus>` (needs the `usbmon` kernel module and capture rights), runs `tests/capture.py tests/<name>/capture.png` (user presses a finger, or something else to avoid recording a real print: README says use the side of a finger/arm), stops tshark, filters to bus/device with `tshark -Y 'usb.bus_id == B and usb.device_address == D'` into `capture.pcapng`.
3. Add the name to `drivers_tests`, fix ownership of the new dir, `meson test` it.
4. What the machine needs: root, `tshark` (wireshark-cli) with usbmon access, `umockdev` (`umockdev-record`, `umockdev-run` >= 0.16.3), the libfprint build with introspection, python-gobject + pycairo, libgusb typelib.

Arch packages for this: `wireshark-cli`, `meson`, `libgusb`, `python-gobject`, `python-cairo`, `gobject-introspection-runtime`, plus `umockdev` (`extra/umockdev 0.19.8-1` at the time) and the build-time `gobject-introspection` (g-ir-scanner); the `usbmon` kernel module must be loaded. At the time of research (2026-10-14): wireshark-cli 4.7.3, meson 1.12, libgusb 0.4.9; umockdev and g-ir-scanner were not installed. The wiki on umockdev says the pcap method is "a pure in-order replay" (umockdev README "Record and replay USB devices using usbmon pcap captures").

### 4.3 Pitfalls specific to us (verified in umockdev and libfprint sources)

1. **Replay compares every OUT buffer byte-for-byte.** `umockdev/src/umockdev-pcap.vala:296-318`: a queued URB is only "submitted" if type, endpoint, length and the full OUT data equal the recording ("Compare the full buffer (as we are outgoing)"); otherwise the replay stalls ("buffer mismatch"). So the test replay needs the **exact same 10500-byte module and 18869-byte program** that were on the wire when recorded. The pcapng therefore embeds both the proprietary module and the per-unit calibration. Also the `device` file embeds the unit's USB serial, and the capture embeds the finger image (8064 B) and the serial inside RomInfo. The project keeps proprietary, per-unit and biometric data git-ignored, so fixtures cannot go to any shared repo as recorded. Options for the brief: (a) keep fixtures local/untracked, tests skip (77) when blobs or fixtures are absent; (b) post-process the pcapng (python pcapng/scapy) to substitute deterministic dummy blobs, a synthetic image and a fake serial, and make the driver in emulation mode read the dummy blobs from an env-var dir (tshark/editcap cannot rewrite payloads); then regenerate `capture.png` by running the replay. Either way the driver must read blobs from a path overridable in emulation mode.
2. **URB type must match the recording.** `umockdev-pcap.vala:278`: `urb.type != urb_hdr.transfer_type` -> mismatch. usbmon records EP 0x83 as INTERRUPT; libusb submits BULK for `fill_bulk` even on an interrupt endpoint. vfs7552.c does exactly that (`fill_bulk` on `VFS7552_INTERRUPT_ENDPOINT`) and its test is the old `.ioctl` format. **Use `fpi_usb_transfer_fill_interrupt` for EP 0x83.** Evidence that interrupt + pcapng works upstream: `uru4000-4500/capture.pcapng` contains 12 interrupt, 28 control, 2 bulk URBs (tshark `usb.transfer_type` count).
3. **`FP_DEVICE_EMULATION=1` is also set while recording**, so any emulation-mode shortcut in the driver changes the recorded conversation too. Emulation hooks must therefore not alter USB traffic (secugen.c:2226 skips a timer-driven poll *and* needs the recording made to match; synaptics.c:1224 and goodix.c:1378 only fake the serial *string*, which is read outside the bus capture). Keep emulation-mode differences to: blob dir override, fixed random numbers (uru4000.c:1415), a fake serial string.
4. **Strict ordering and determinism.** Replay is an in-order URB match (pending URBs are matched oldest-first), timeouts are emulated. A different retry/re-send policy than recorded (e.g. re-sending `51`) breaks the replay. Driver output must be deterministic for the PNG compare.
5. A `capture` test only checks one image through `capture_sync (True)`, which on image devices runs through `fp_image_device_start_capture_action` (`fp-image-device.c:~95`). End-to-end enroll/verify (SIGFM) coverage needs a `custom.py` (no image-driver precedent in tree, but `drivers_tests` accepts it).
6. libusb splits large bulk transfers (16 KiB per URB historically); the 18869-byte program may show up as 2 URBs in the capture. Record and replay run the same libusb so this is self-consistent, but a different libusb version at replay time could change the split; check when the first fixture exists (not verified).

---

## 5. Per-device state and identity

### 5.1 Instance state
- Instance struct `struct _FpDevice<Name> { FpImageDevice parent; ... }` declared with `G_DECLARE_FINAL_TYPE` + `G_DEFINE_TYPE` (nb1010.c:90-99, egis0570.c:31-46). Allocate in `img_open`, free in `img_close` (uru4000/nb1010 do so; `g_assert (priv->active == FALSE)` is checked by the core before `img_close`). Fields we need: blob buffers (GBytes), serial hex string, `deactivating`, `loop_running`/ssm pointer, cancellable for the EP83 wait, the "module already loaded" flag re-read at every activation.
- Per-model constants: `FpIdEntry.driver_data` retrieved with `fpi_device_get_driver_data (FP_DEVICE (dev))` (uru4000.c:1412 uses it as an index into `uru4k_dev_info[]`). Useful if 00a8 (same sensor type 0x2449) is added later.

### 5.2 Identity checks (what the in-tree drivers do)
- **id_table** matches vid:pid only. **`dev_class->usb_discover`** can raise or reject by score (upektc_img.c:759-771 compares pid and bcdDevice; default score 50, highest wins). No need unless 0078 shares an id with another device.
- **`dev_class->probe`** (FpDeviceClass, called for every device right after enumeration; FpImageDevice does not override it, so an image driver may set it, although no in-tree *image* driver does; all `probe` users are FpDevice-based MOC drivers): may open the USB device and talk to it, calls `fpi_device_probe_complete (dev, device_id, device_name, error)`; an error removes the device before the API user sees it. Used to (a) set `device_id` from the USB serial string (goodix.c:1378-1437, synaptics.c:1230-1235, with `"emulated-device"` in emulation mode) and (b) verify firmware/hardware support (synaptics.c, goodix.c:1386 warns on unsupported serial suffix).
- **device_id** defaults to `"0"` (`fp-device.c:179`); all upstream image drivers keep it, since their prints are host-side. fprintd stores prints under `driver/device_id`. For us keeping `"0"` makes prints portable between units, which matches host-side matching; the per-unit serial is only needed for locating the program file. (Decision for the brief; setting device_id to the serial would bind prints to the unit.)
- **open-time validation by reply content**: vfs7552.c (:150-181) compares replies of `01`/`19`/init commands with expected prefixes (`RECV_CHECK`, `RECV_CHECK_SIZE`) and fails the SSM with `FP_DEVICE_ERROR_PROTO` ("Received a wrong reply from the driver", :260-285); uru4000.c:1349-1370 checks the interface class triplet and fails open with `FP_DEVICE_ERROR_GENERAL` ("Could not find interface").

### 5.3 Suggested mapping of the prototype's checks
Prototype checks (proto README, "Checks, before anything is written to the sensor"): `01` reply says ROM 6/0x14, `75` says sensor type 0x2449, serial in `01` bytes 0x12-0x17 equals the serial in the calibration data. In the driver:
- `img_open` SSM: claim interface 0, `01` (ROM check, read serial), `75` (sensor type); derive `program-<serial-hex>.bin` path; load + validate blobs; failure -> `fpi_image_device_open_complete (dev, error)` as in 3.5. Nothing is written to the sensor before these pass. The serial binding is then structural (file name) instead of a content comparison, plus a size check; if the program file carries the serial internally, compare it as the prototype does.
- `img_activate` SSM: re-read RomInfo byte 17; `06`+module only if 0 (otherwise skip, prototype hardware runs 1-2: a second `06` answers 0x04aa); `02`+program; `fpi_image_device_activate_complete`.
- `change_state(AWAIT_FINGER_ON)`: (re)send `02` as the prototype does per image, arm the EP 0x83 interrupt read (timeout 0, own cancellable), finger-down event -> `report_finger_status (TRUE)`; `change_state(CAPTURE)`: wait for the ready event, `51`, read 8082 B reply, parse 18-byte header, error field != 0 or status 0x0007/0x0412 -> `fpi_image_device_retry_scan (dev, FP_DEVICE_RETRY_GENERAL or TOO_FAST)`, else `image_captured`; then `04`, `52` and finger-off handling (see the design point in 2.3).
- `img_deactivate`: `04`, `52` on every exit path once `02` has been sent; then `fpi_image_device_deactivate_complete`.

---

## 6. Open points for the brief (not decided here)

1. Base commit for the fork (stock 1.94.100 NBIS-only vs the SIGFM fork's modified `FpImageDevice` API: `score_threshold`, `algorithm`).
2. Finger-off strategy (no finger-up event known) and `nr_enroll_stages` (small 144x56 window; matcher-eval says FRR is limited by enroll coverage, so more stages than 5 look plausible, as elan-in-SIGFM-fork uses 15).
3. Blob location (recommendation above) and fixture strategy for umockdev (local-only vs sanitized synthetic fixtures with an emulation-mode data-dir override).
4. Whether to set `device_id` to the unit serial (default `"0"`).
5. Prerequisites for the umockdev test step: `umockdev`, `gobject-introspection` installed; `usbmon` loaded; root for `create-driver-test.py`.
