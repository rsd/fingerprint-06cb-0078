# Review of ToLiKNoSoV's 06cb:0078 driver and backend

Read-only review, 2026-10-13. Nothing from either repository was built or run, and the USB device was not touched. The scripts I wrote only parse text and search bytes in files (python3 -I); they are local working files, not published. Sources:

- driver: a read-only clone of https://github.com/ToLiKNoSoV/synaptics-06cb-0078-driver at HEAD 78cd8cd, 8 commits, 2026-03-01 to 03-08.
- backend: a read-only clone of https://github.com/ToLiKNoSoV/synaptics-0078-backend at HEAD d2e3e44, 15 commits, 2026-03-13 to 03-21.

Paths below are relative to those repositories, `file:line`. "Our DLL" is `driverstore/synaWudfBioUsb.dll` (5.5.2707.1073; a local copy of the vendor driver, not published). "Our ETW" is the Windows USB ETW trace of a WinBioIdentify (kept locally, not published).

## 0. Bottom line

- The driver is a C port of iusearch/Synaptics78 `main.py` (2019). It replays recorded byte strings in plaintext. There is no TLS, no pairing, no key handling and no flash code in it.
- Its constants are real. The 10500-byte module it uploads is byte-identical to an entry in our DLL. The sizes of its init exchanges (38, 68, 10, 10501, 18869, 2154 bytes) are the same as the transfers our own Windows ETW shows the Windows driver doing on this machine.
- This project did not build on it. The driver derives from `FpDevice` rather than libfprint's `FpImageDevice` model, verify and matching are not implemented, and two of the three embedded blobs hold another unit's calibration data. The backend implements no verify method.
- This project uses it as a protocol reference. The primary references are iusearch's `main.py` and our own ETW and DLL.

## 1. Protocol flow implemented

Sources: driver `synaptics_0078.c` (1418 lines), `synaptics_0078_data.c` (3063 lines of byte arrays), `test_capture.c`.

Evidence available for "exercised":

- The driver README says "Драйвер работает. Захватывает изображение, находит минуции, создаёт шаблоны" (README.md:6).
- The backend `builddir/meson-logs/meson-log.txt` proves only that the author built on Arch (GCC 15.2.1, 2026-03-21).
- Neither repo has a log, capture, sample image, pcap, test or issue.
- Therefore every step below is "claimed by the author, no artifact". Where iusearch's `main.py` does the same step, that script has independent claims of working: the Validity91 issue #11 image and the iusearch README, both found in the support search (`research/support-search.md`).
- Our own ETW (Windows, this laptop) confirms a subset by transfer size. That is marked "ETW".

| # | Step | Where (driver) | Status |
|---|---|---|---|
| 1 | Open: `g_usb_device_reset`, 0.5 s sleep, `set_configuration(1)`, 0.5 s sleep, claim interface 0 | 1094-1126 (reset 1109, config 1113, claim 1116) | Claimed. Same as `main.py` (dev.reset, set_configuration, clear_halt). The (filtered) Windows ETW shows no reset and no SET_CONFIGURATION. It shows a standard CLEAR_FEATURE(remote wakeup) and a vendor control read 0xC0/0x14 of 2 bytes, which the driver does not do. |
| 2 | Probe: bulk OUT EP 0x01 `01`, read 38 B; `19`, read 68 B | 464-492 | Claimed. ETW: Windows does 1 byte out, then reads of 38 B and 68 B (64+4). The opcodes are not logged, so the sizes match but the opcodes are unconfirmed. |
| 3 | `08 60 20 00 80 0b 00 00 00 04`, read 2 B; `07 80 20 00 80 04`, read 6 B | 494-522, consts 62-63 | Claimed, copied from `main.py:26-27`. Not present in our ETW as separate exchanges. By shape (my reading): hardware-register write of 0x0b to 0x80002060, and a 4-byte register read of 0x80002080. |
| 4 | `75`, read 10 B; then `01`/`19`/`75` again | 524-582 | Claimed. ETW has `75`-sized (10 B) once. The repeat of 01/19/75 is from `main.py:29-31`. Windows does the triple once. |
| 5 | Firmware-extension-like module upload: `CMD_LONG1`, 10501 B on EP 0x01, read 2 B | 584-597 | Claimed. ETW: Windows writes exactly 0x2905 = 10501 B then reads 2 B. |
| 6 | Config/calibration table writes: `CMD_LONG2` then `CMD_LONG3`, 18869 B each, read 2154 B after each | 599-627 | Claimed. ETW: Windows writes 0x49B5 = 18869 B once, reads 0x86A = 2154 B. The driver sends it twice (see section 2). |
| 7 | Init complete, interrupt polling starts on EP 0x83, 8-byte reads, resubmitted forever | 629-660, 346-446 | Claimed. ETW: Windows posts 8-byte interrupt reads and gets a 5-byte event right after the 18869 exchange. |
| 8 | Capture: write `51 00 20 00 00` (5 B), read status (2 B `0x0604` = preparing, `0x0100` = no finger) | 683-699, consts 59, 72-75, 262-277 | Claimed, from `main.py:98`. |
| 9 | Finger detect via interrupt patterns `02 00 40 10 00` = finger on; `03 40 01 00 00`, `03 43 04 00 41`, `03 42 04 00 40` = ready; `03 50 05 00 00` = bad position | consts 52-56, handler 386-412 | Claimed, from `main.py:48-70`. The handler does `g_usleep(100 ms / 200 ms)` inside the USB callback (396, 405). |
| 10 | Second `51 00 20 00 00` after 0.5 s sleep, read image from EP 0x81 in 64-byte chunks until 8082 B | 709-726, 242-320, consts 45-49 | Claimed. `main.py` reads one 8146-byte transfer from EP 0x81. 8082 = 8146 - 64; my reading is that the C code discards the first 64-byte read (status), which is consistent with the +18 header offset. |
| 11 | Image format: 144 x 56, 8-bit grey, 8064 B, at offset 18 in the 8082 B buffer, so 82 bytes of header before the pixels in the full 8146-byte read | 45-49, 735, 851 | Claimed. Same as `main.py:46-60` (`imgarray[-8064:].reshape(56,144)`) and iusearch's Validity91 report. Not independently seen on our hardware. |
| 12 | Post-processing (enroll only): NBIS `fp_image_detect_minutiae` in a nested `g_main_loop_run`, `fpi_print_add_from_image`, dump to `/run/fingerprint_raw.raw` | 793-957 (856, 879-888, 908) | Claimed. Single image, one "stage". |
| 13 | Verify | 1335-1356, 1005-1009 | Not implemented. Verify runs the same capture then calls `fpi_device_verify_complete(dev, NULL)`. There is no `fpi_device_verify_report`, no match, no comparison (grep: no `verify_report`, no `bozorth`, no `match` call). |

What the driver does not do (checked by reading the whole file and by grep):

- No Windows-style TLS session.
- No key exchange or pairing.
- No firmware-version check before the module upload.
- No read of the sensor's own enrolled data.
- No post-capture re-arm sequence. In `main.py` the table with byte 0x23 at offset 1749 is sent after each capture. In the C driver that variant is sent during init instead.

Two details worth knowing:

- The sizes in the C port follow a "+64" pattern against iusearch's read buffers (102/66/70/74 against 38/2/6/10). That suggests the author sized them from real replies rather than from `main.py`. The pair 128 against 68 breaks the pattern, so this is weak.
- The ETW shows exactly one 1-byte command with a 2-byte reply *before* the 38-byte one, and a vendor control request 0xC0/0x14. Neither is in the driver, so its start-up differs from what Windows does.

## 2. Where the constants came from, and checks against our DLL

### Provenance

- The driver says "Based on reverse engineering work and Python implementation" (synaptics_0078.c:3) and "Endpoints - согласно Python скрипту" (:39).
- I compared against iusearch/Synaptics78 `main.py` (a local copy of the public script). That script's init sequence is `01, 19, 0860.., 0780.., 75, 01, 19, 75, [06 blob], [02 98 table], [81 7e frame]`, followed by the 0x51 capture command, the interrupt patterns, and `epi.read(8146)`. The C driver matches it constant for constant (`main.py:23-33, 48-70, 98`).
- `CMD_LONG1` (10501 B) is byte-identical to the 10501-byte literal in `main.py:34`.
- `CMD_LONG2` and `CMD_LONG3` are the iusearch "02 98 table (16384 B)" and "81 7e frame (2485 B)" writes, concatenated into one 18869-byte write. They are not the same bytes:
  - Bytes 0 to 1120 of the table are identical to iusearch's.
  - From offset 1121 on, the contents differ (about 12,800 of 16,384 bytes differ), with the same statistical shape. These are 9.2 KB at entropy about 6.5, then about 8.5 KB of near-0x80 pixel-like bytes.
  - So the author recorded his own copy of these writes from his own unit, and these sections are unit-specific. They are almost certainly the calibration/baseline data, the same kind of thing our registry `CalibrationData` holds.
  - The two copies differ at one byte (offset 1749: 0x02 in LONG2, 0x23 in LONG3), as in iusearch's script.
- Where the author recorded his bytes, and with what tool, is not documented anywhere.
- No keys, passwords or certificates appear in either repository.

### Byte searches in our DLL (read-only)

- **LONG1 bytes 1..10500 (without the leading 0x06) occur exactly once in our DLL, at file offset 0x19cca0** (.rdata), 100% match and contiguous. It sits inside our high-entropy region 0x160000-0x1dc000 (`extracted/synaWudfBioUsb_hiEntropy_0x160000.bin`, offset 0x3cc20 there).
  - That region is a library of about 80 blobs, each headed `02 00 00 01`, preceded by zero padding, and 16-byte aligned (the first at 0x15f6e0). They are unrelated ciphertexts of various sizes (about 80 candidates by that header pattern). At least one other entry is also 10500 B long (0x1cc580); it shares about 49 of 10500 bytes with ours, which is chance level.
  - So: the 06-blob the author uploads is the DLL's own entry for sensors of this kind, and the Windows driver at version 5.5.2707.1073 carries exactly that entry. A Linux driver can read it from the DLL at run time, as python-validity does with the Windows driver, instead of embedding it.
  - The same blob is not present in `synaBscAdapter.dll`.
- **LONG2/LONG3 bytes 0..1120 (the fixed register-table header) occur in our DLL**: 1119 of 1121 bytes are covered, as two long contiguous runs, one of 73 B at 0x1df31d and one of 1026 B at 0x1e2f66. They also occur in several other places in .rdata (the table is repeated for other variants). So the fixed part matches our driver version.
- **LONG2/LONG3 bytes 1121..18868 are not in the DLL** (0 of the rest, apart from chance matches), nor in `CalibrationData_decrypted.bin` (0 hits, correlation of the pixel part with our baseline -0.003). Expected, since they are per-unit.
- Short constants (`51 00 20 00 00`, `08 60...`, `07 80...`, the five interrupt patterns) are not present as literals in either DLL. That is not informative for 5-byte strings, since the driver most likely builds them at run time.
- Our `CalibrationData_decrypted.bin` header contains the RomInfo bytes `10 b6 04 54 9a 00 00 00 06 14 01 33 00 01`, the same ROM 6/0x14, product 0x33 identity found earlier in the 0078 traces.

Conclusion: the shared protocol skeleton and the module blob match our driver version. The calibration/config content does not belong to our sensor and must not be reused.

## 3. Anything that could disturb Windows Hello

No code path in either repo pairs the sensor, resets ownership, erases or partitions flash, or sends a TLS or key command. There are no opcodes for that in the C source, and no setup of a host key. The writes the driver does make are:

| Step | What | Persistent? | Optional? |
|---|---|---|---|
| 1 | USB port reset and `set_configuration(1)` on every open (synaptics_0078.c:1109, 1113) | No ownership effect expected. Our notes show repeated pnputil restarts of the sensor under Windows, with Hello still working. | Yes (iusearch does it too). |
| 3 | Register write `08 60 20 00 80 0b 00 00 00 04` (:494-500) | Unknown. By shape a hardware-register write. | Not in the Windows ETW exchanges, so probably optional. |
| 5 | Upload of the 10500-byte module (:584-590) on every open, without a version check | Unknown. Windows has the `UpdateFirmwareExtension` path and `FwextMajorver` strings, so this is the kind of thing Windows itself does. Whether it lands in RAM or in flash on a sensor with no external flash is unknown. | Windows does it in our ETW (10501 B). A Linux driver should first query and then decide. |
| 6 | Two 18,869-byte config/calibration writes carrying another unit's calibration (:599-627) | Unknown. Windows stores `CalibrationData` in the registry (`BackupCalibrationData` string), which suggests the host re-sends it at open and the sensor does not keep it. Not proven. | Windows sends one such write. Replaying a foreign one would at best give a wrong baseline for the images. |

Reading of the risk: for Windows Hello the likely harm is image quality or a changed calibration state, not pairing loss. Our Windows setup uses SensorMode 2 with a host-side template database (HANDOFF §3, §7), so plain image capture seems not to need ownership at all. That is an inference, not a verified fact. For this project's own unit, the first experiment is therefore read-only: steps 2 and 4 only (`01`, `19`, `75`), nothing else; the writes in steps 3, 5 and 6 wait until their effect is known.

A separate caution that applies to the HANDOFF plan, not to these repositories: running the Windows driver inside a VM with USB passthrough could pair the sensor from the VM. That is outside what these repos do.

## 4. Structure relative to libfprint

- The driver class derives from `FpDevice` (:156, `G_DEFINE_TYPE(..., FP_TYPE_DEVICE)`), not from libfprint's `FpImageDevice`. It declares only `FP_DEVICE_FEATURE_CAPTURE` (:1402) and implements `enroll` and `verify` itself, outside the `FpImageDevice` activate/deactivate model that gives libfprint image devices enrolment stages, matching and finger-status reporting. Enroll produces one print from one image.
- Verify is not implemented (item 13 in section 1). The backend's introspection XML and dispatcher (`main.c:497-526`) list no `VerifyStart`, `VerifyStop` or `VerifyStatus`.
- The blobs are embedded: `synaptics_0078_data.c` holds the 10501-byte module and the two 18869-byte tables as byte arrays (section 2 for where they come from).
- It builds outside the libfprint source tree. The driver's `meson.build` (labelled `# drivers/synaptics_0078/meson.build`) builds a `shared_module`, and the repositories contain no patch to libfprint's own `meson.build` or driver list. libfprint 1.94 compiles its drivers into `libfprint-2.so` (from memory of its design, not checked here), which fits the README statement that fprintd "does not see" the device.

## 5. How this project uses it

- **This project did not build on it.** The driver is not structured as a libfprint image device, verify is not implemented, and two of its three blobs are recordings from another unit. The backend implements no verify path, and this project uses fprintd as it is.
- **Used as a protocol reference, in this form:**
  1. The step order and message sizes in the init exchange, confirmed by our own ETW: `1B->2B`, `01->38`, `19->68`, `75->10`, `10501->2`, `18869->2154`, then interrupt EP 0x83 with 8-byte reads.
  2. Interrupt codes and capture sequence from `main.py` (EP 0x83 patterns; command `51 00 20 00 00`; image from EP 0x81), unverified on our unit.
  3. Image geometry: 144 x 56, 8-bit, 8064 pixel bytes, 82 bytes of header in the 8146-byte read.
  4. The finding in section 2 that the module blob is entry 0x19cca0 of our DLL's blob library, and that the fixed header of the 18869-byte table is in the DLL (0x1df31d, 0x1e2f66).
  5. The negative information: no TLS, no pairing and no flash writes are needed to get a plaintext image on the author's unit(s). That points to a capture-only path that need not touch ownership. It was not verified on a unit that Windows has already set up, such as ours.
- **Reused:** the constants and sequence above, as notes. Possibly the interrupt-pattern table and the size list as a test oracle.
- **Written new for this project:**
  - A new `FpImageDevice` driver with a state machine written from our ETW and a fresh USB capture.
  - Module loaded from our DLL (offset 0x19cca0) or skipped if the sensor does not need it.
  - The 18869-byte table built from our own unit's data, not replayed from anyone else's. Where that data comes from is the main open question: possibly our registry `CalibrationData`, or a capture of what Windows sends.
  - Matching done with libfprint's NBIS path or another matcher.
  - No custom backend: use fprintd and `pam_fprintd` as they are.
- **Open questions that the repos cannot answer:**
  - What is the opcode of the first 1-byte command (2-byte reply) and of the vendor control request 0xC0/0x14 that Windows issues?
  - Are the module upload and the table writes volatile?
  - Does a Windows-initialised unit accept plaintext commands?
  - Does the image really come on EP 0x81 here? Earlier notes on the 00a8 sensor say EP 0x82.

## Files I produced

Local working files, not published:

- `parse_blobs.py` and `an1.py` to `an12.py`: text-parse and byte-search scripts (read-only).
- `CMD_LONG{1,2,3}.bin`: the three arrays extracted from `synaptics_0078_data.c` by text parsing, for later comparison.
