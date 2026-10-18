# Review of prior efforts for Synaptics 06cb:0078 (build-or-write-our-own input)

Read-only review, 2026-10-13. Nothing from these repositories was built, installed or run. The USB device was not touched.
I only ran my own small Python scripts (`python3 -I`; local working files, not published) that read files as data: they parse hex strings out of source files and search our DLL for byte patterns.

Sources: read-only clones, each in its own new directory, of these public repositories:
- https://github.com/iusearch/Synaptics78, https://github.com/nmikhailov/Validity90, https://github.com/uunicorn/python-validity, https://github.com/uunicorn/open-fprintd
- https://github.com/ashh87/python-validity (checked out at branch `dev-00a8`), https://github.com/ashh87/open-fprintd

The ToLiKNoSoV repositories are reviewed separately in `research/review-toliknosov.md`; here they are used only to compare byte blobs (section 2).

API dumps, the Wortmann zip and the libfprint files I fetched for comparison are local working files, not published.

---

## 0. Headline findings (what changes the picture)

1. **The earlier search missed a full 00a8 implementation.** python-validity PR #288 "Add support for 06cb:00a8" (author ashh87, opened 2026-09-25, open, 0 comments, 14 commits, +3054/-30) is linked from issue #179's timeline. It implements pairing, capture, image parsing, host-side matching and an open-fprintd device for the sibling sensor, with tests. The branch is `ashh87:dev-00a8`. There is a companion open-fprintd PR #29 (`ashh87:integration`). This is by far the most complete prior art.
2. **0078 is the same sensor type as 00a8, 0x2449.** Evidence:
   - The 0x75 reply in Validity90 #63 is `00 00 00 00 00 00 37 00 b7 00`, i.e. major 0x00b7, minor 0x37.
   - python-validity `hw_tables.py:212` maps (0xb7, 0x37) to `DeviceInfo(0x00b7, 0x2449, 0x37, 0xff, 'SYN 86C  TM03267001')`.
   - The same row is in our DLL: bytes `b7 00 49 24 37 00 ff 00` followed by the name string, at file offset 0x136884.
   - The 0x2449 capture-program rows (#31 for ROM 6/0x14, #32 for 6/0x15) are in our DLL's 64-byte-record table at 0x1eca04.
3. **Windows itself talks to this 0078 in plaintext, with no TLS and no pairing, during identify.** Our own Windows USB ETW trace summary of a WinBioIdentify (kept locally, not published; no finger on the sensor) shows these completed bulk transfers on this machine:
   - OUT 1 / IN 2
   - OUT 1 / IN 38 (0x01 RomInfo)
   - OUT 1 / IN 68 (0x19)
   - OUT 1 / IN 10 (0x75)
   - **OUT 10501 / IN 2** (`06` + a 10500-byte module)
   - **OUT 18869 / IN 2154** (the `02` capture command)
   - IN 5 on EP 0x83 (interrupt)
   - Then, after a cancelled read, OUT 1 / IN 2 twice (cleanup).

   The ETW holds only URB metadata, no payload bytes, so the opcode names above are inferred from request and reply sizes (the 1-byte requests with 38-, 68- and 10-byte replies match 0x01, 0x19 and 0x75; the 1-byte request with a 2-byte reply is unidentified). The sizes 10501 and 18869 are exactly the sizes of the blobs that iusearch (2019) and ToLiKNoSoV (2026) replay in plaintext. There is no TLS handshake traffic, no 0x4f/0x50 and no flash command in that session. So capture on 0078 appears to need neither pairing nor TLS, and a driver that does only what Windows does during identify cannot disturb Windows Hello.
4. **0078 and 00a8 differ in firmware-module generation.** The 10500-byte capture module Windows sends on 0078 is byte-identical to DLL module-table record 30 (ROM 6/0x14, **generation f04=3**, kind 0x400080, file offset 0x19cca0). The 00a8 PR uses the generation-2 record 57. For ROM 6/0x14 the gen-2 and gen-3 modules of the same kind all differ in content (see section 2). The RomInfo capability byte also differs (0x00 on 0078, 0x21 on 00a8). So 00a8's pairing and TLS path cannot be assumed for 0078, and its gen-2 module blobs do not transfer. The sensor-level capture logic does transfer.
5. **Our DLL is identical to the one the 00a8 work used, apart from the signature.** `synaWudfBioUsb.dll` in `driverstore/` is byte-identical to the Wortmann package copy (sha256 7376e20f…) except 4 bytes in the PE header (checksum and certificate-directory size) and the Authenticode blob. All of byPARSE's and ashh87's table offsets apply to our file. The PR's `KNOWN_DLLS` hash check would reject ours as an "unknown build". That is a trivial fix, because it hashes the whole file including the signature.

---

## 1. Protocol flow per effort, with exercised/claimed status

Legend: **E** = exercised (logs, hex dumps or measurements in the source), **E-indep** = E and corroborated independently, **C** = claimed or implied only, **N** = not implemented.

### 1a. iusearch/Synaptics78 (2019), 3 commits, no licence file, no image sample, 0 forks, 5 stars

`main.py` and `synaptics78.cpp` are both pure replays of recorded traffic. There is no TLS, no pairing and no flash command.

| Step | What it does | Status |
|---|---|---|
| Device open | Python: find 06cb:0078, detach kernel driver, `dev.reset()`, `set_configuration()`, `clear_halt` on all endpoints (`main.py:149-161`). C++: `libusb_open_device_with_vid_pid`, `claim_interface(0)` (`synaptics78.cpp:208-231`). Endpoints: OUT 0x01, IN 0x81, interrupt 0x83 (`synaptics78.cpp:9-15`). | C. Author's claim. Endpoint roles match our ETW. |
| Handshake / session setup | None. Plaintext sequence `01`, `19`, `08 60200080 0b000000 04` (write HW register 0x80002060 = 0x0b), `07 80200080 04` (read register 0x80002080), `75`, then `01`, `19`, `75` again (`main.py:24-32`, `synaptics78.cpp:89-103`). | C for the author's device. Opcodes 01/19/75 and their reply sizes (38/68/10 B) are E-indep by our ETW. The 08/07 pair is not in our ETW. |
| Firmware upload | `06` + 10500-byte module (`main.py:33`, `synaptics78.cpp:104-107`). | **E-indep.** The bytes equal ToLiKNoSoV's blob and DLL record 30 (section 2). Our ETW shows OUT 10501 → IN 2. |
| Capture arming | `02` + 18869-byte command (16384 B + 2485 B), reply read as 2218 B (`main.py:36-40`). Re-sent after each capture (`main.py:105-109`). | Size E-indep (our ETW OUT 18869 / IN 2154). The first 1121 bytes are identical to ToLiKNoSoV's copy and 67 of 71 sampled 24-byte fragments are in the DLL's tables. The rest differs per unit (calibration or blank-frame data). Replay of another unit's tail is C. |
| Finger detection | Reads 5-byte interrupts on EP 0x83: `0200401000` = finger on (type 2); then `0340010000` (position ok) or `0350050000` (retry); then `0342040040`, or `0341030040` + `0343040041` (`main.py:63-91`, `synaptics78.cpp:129-197`). | C. No log in the repo. The type-2 and type-3 interrupt structure matches the PR #288 decoder (`sensor_2449.py:514-544`). The 5-byte interrupt itself is E-indep in our ETW (the start ack). |
| Image capture | `51 00 20 00 00`, read 8146 B; a first reply of `020000000000` means read again; image = last 8064 bytes reshaped 56 rows x 144 columns (`main.py:46-49,98-101`; `synaptics78.cpp:200-206`). | Claimed by the author. The format is E-indep: Validity91 issue #11 shows the head `00008c1f00003800…` (status 0, length 0x1f8c = 8076 = 12 + 56x144, then 0x38 = 56). The PR #288 parser reads exactly this layout (`sensor_2449.py:575-592`). The author says "clear fingerprint image" in Validity91#11 (comment 435889147), but no image is published. |
| Cleanup | Error path sends `04`, then `52` (`main.py:132-133`). | Matches PR #288's cleanup (`sensor_2449.py:569-571`) and the two final 1-byte commands in our ETW. |

Limitations: the demo captures one image and exits. The author's own words (issues #2 and #3, 2020 and 2023): "just a prototype", "a reverse engineering project, not a driver project", libfprint "is not supporting small sensor verification" (https://github.com/iusearch/Synaptics78/issues/2#issuecomment-590623955). Nobody forked it.

### 1b. Validity90 issue #63 (2019, cydia2020) and the prototype it ran

Issue: https://github.com/nmikhailov/Validity90/issues/63. Comments: iusearch only (2019-08-31), pointing at Synaptics78. No maintainer reply.

The prototype is Validity90's `prototype/main.c`, written for 138a:0090/0097-class sensors. It is a hand-replayed state machine, with TLS code and 0090-specific blobs (`prototype/constants.h`).

| Step | What happened on the 0078 (log in the issue body) | Status |
|---|---|---|
| Open | Found 06cb:0078 | E |
| `01` RomInfo | 38 bytes: `00 00 10 b6 04 54 9a 00 00 00 06 14 01 33 00 01 / 00 00 00 63 0a 00 63 0b 00 00 00 00 00 00 01 00 / …` (the serial bytes 0x12..0x17 are replaced here with synthetic ones). ROM 6/0x14, product 0x33. | E |
| `19` | 68-byte reply (differs from 00a8's) | E |
| `3e` get_flash_info | `00 00 ff 00 ff 00 00 10 10 00 00 00 01 00 00 00`: JEDEC 0xff:0xff, 0 partitions. Byte-identical to byPARSE's 00a8 reply. | E |
| `08 5c200080 07000000 04`, `07 80200080 04`, `75` | accepted. `75` → type 0x2449, see headline 2. | E |
| `06` + 0090 init blob and 0090 config blob | rejected: `be 04` (0x04be) | E (rejection) |
| `4f` 877-byte flash-partition command (`constants.h:2116`, `setup_sequence_msg11`) | rejected: `01 04` (0x0401) | E (rejection) |
| `1a` | `91 04` (0x0491, "nothing to commit") | E |
| Session, TLS, finger detection, image | not reached; the program ends with "Incomplete reverse engineering" (`main.c:308`) | N |

Where the prototype's constants come from: Windows 10 VM with `cdb.exe` attached to WUDFHost, with breakpoints on CryptoAPI, BCrypt and WinUsb (`dbg/attach.sh`, `dbg/tracer.txt`), and USB captures (`dumps/*.pcapng`). The README names no 0078 content.

The same issue is a warning (section 3): this prototype tried to send a flash-partitioning command to a 0078.

Related: Validity90 issue #69 is the same experiment on 00a8 (RomInfo byte 0x19 = 0x21, last byte 0x06). It failed the same way (https://github.com/nmikhailov/Validity90/issues/69).

### 1c. python-validity issue #179 (byPARSE, 2026-09-08/09) on 06cb:00a8

https://github.com/uunicorn/python-validity/issues/179 : 5 comments, all by byPARSE; no code was linked. The scripts were offered ("happy to share") but never posted. The probe script in comment 1 is read-only and inline. The thread links one other thing: the January 2018 fprint list thread. Its timeline shows PR #288 (section 1d).

| Step | Claim | Status |
|---|---|---|
| Open | Claim interface 0 only; `set_configuration()` wedges the device (reads on 0x81 time out); `USBDEVFS_RESET` recovers (comment 2, "Two USB findings") | E (stated as verified twice) |
| Identify | `75` → type 0x2449 via hw_tables (comment 1), plaintext, no pairing | E (hex dump) |
| Firmware modules | Module table at file offset 0x15e004, 66 records of 80 bytes; tried all ten modules for ROM 6/0x14; results `0x04be`/`0x04aa`/accepted (comment 2) | E (status codes listed). I re-parsed this table in our DLL and the layout matches (section 2). |
| Pairing | `06 + reset_blob`, `4f` + certificate, `50`, `handle_priv`, `tls.open()` gives a TLS session without flash; the ECDH signature check was skipped (comment 5) | E (statuses), with the caveat stated by the author |
| Calibration | `cmd 02` with line_update_type_1 accepted, 51224 B per call; line-mean stdev 0.19 → 22.07 with the line updates (comment 5) | E (measurements) |
| Image data | EP 0x82 records of 152 bytes, 96 active pixels; signal drops 177.4 → 156.4 with a finger, about 15x noise (comment 5) | E (measurements) |
| Finger detection | Start interrupt (type 0) arrives. **Type-2 finger-detect never arrives**, tested with threshold values 135 and 512 (comment 5). Chunk 0x26 layout decoded from `FUN_18008c720`. | Not working at that date. PR #288 later builds chunk 0x26 differently (section 1d) and handles it. |
| Image reconstruction | Chunk 0x2e not decoded, no ridge pattern reconstructed (comment 5) | N at that date |

The author retracted two earlier claims in the thread: "match-in-sensor" (comment 5) and that `0x04af` meant "fwext not loaded" (comment 3).

### 1d. python-validity PR #288 (ashh87, branch `dev-00a8`) and open-fprintd PR #29

https://github.com/uunicorn/python-validity/pull/288 ; https://github.com/uunicorn/open-fprintd/pull/29. Created 2026-09-25, 14 commits. The commit messages cite the Windows driver's USB captures "from issue #179, decrypted" and hardware confirmation. No captures, logs or images are in the repository (`git ls-files`). The tests embed decoded values, for example `REAL_FACTORY_CALIBRATION_VALUES` and `REAL_CHUNK_43` in `tests/test_sensor_2449.py:44-53`.

| Step | Code | Status |
|---|---|---|
| Open | `usb.py` diff: claim interface 0 without `set_configuration()`; `bus_reset()`; `wait_for_reboot()` (`usb.py:77-87,120-140`) | C, consistent with #179 |
| Session setup | `send_init`: `01`, `19` only (`usb.py:143-150`). Sensor open: `08 60200080 0b000000 04`, `07 80200080 04`, `75`, `01`, `19`, `75` (`sensor_2449.py:192-210`) | C. "Byte for byte as the Windows driver sends it" |
| Pairing | `06`+pair module (gen 2, ROM 6/0x15 tag), `4f` + client certificate, `01`, `50`, `1a`, then TLS handshake (`init_flashless.py:98-127`). The three blobs are stored in `/var/lib/python-validity/06cb_00a8-pairing.bin`, mode 0600 (`init_flashless.py:51-56`). | C. The commit says "confirmed on hardware" for the 6/0x14 firmware key. |
| Firmware upload | Three RAM modules from the user's DLL, via `06`: pair, calibrate, capture. Extractor reads the module table in the DLL (`dll_tables.py:24-98`). | C |
| Calibration | `06`+calibrate module, one `02` CALIBRATE, blank frame from a second `02`, `1a`; results cached under `/var/lib/python-validity/` (`sensor_2449.py:258-291`) | C |
| Finger detection | Builds chunk 0x26 (threshold from `WINDSOR_CAL_LOW=0xa0`, `HIGH=0x104`, quality 100 → `ival` 0xa0) and chunk 0x2e; waits on EP 0x83 for a type-2 interrupt, then a type-3 event code 4 or 7 (`sensor_2449.py:129-141,460-544`) | C. #179 comment 5 could not get type 2; this PR claims it does. Not verifiable from the repo. |
| Image capture | `51 00 20 00 00`; parse `status, u32 len, x, y, w1, w2, error, pixels`; 144x56, 8-bit (`sensor_2449.py:575-592`) | C for 00a8. Format is E-indep for 0078 by the Validity91#11 header. |
| Matching | Host-side: 2x cubic upscale + CLAHE, masked SIFT keypoints, ratio test 0.8, RANSAC similarity (partial affine) inliers, scale 0.8-1.25, rotation ≤ 30 degrees; score = min of both directions; accept at ≥ 6 (`hostmatch/matcher.py:20-120`). 18 enroll touches (`ops.py:32-34`). | C. The author reports FRR 2.1% at 18 touches (3.2% at 15, 7.6% at 10) on 145 captures of 5 fingers, no false accepts, highest impostor score 4; "convenience grade" (commit 7a98a4a, `ops.py:11,32`, `matcher.py:22-24`). Not independently reproduced. |
| Session end | `05 02 00` reboot after every operation (`init.py` `close_flashless`) | C |
| fprintd side | `HostDevice` registered with open-fprintd (`dbus_service/dbus-service:219-322,444`); open-fprintd PR #29 adds `DeleteEnrolledFinger`, device properties (num-enroll-stages 18, finger-present, finger-needed) and Fedora packaging | C |

Review status: single author, no review comments, no CI, no published logs or images. The sensor-level code reads as internally consistent and cross-checks against our DLL (section 2). The hardware numbers are claims that the repository does not let us verify.

### 1e. python-validity upstream (uunicorn, MIT, last push 2025-06-10, 207 open issues)

Supported: 138a:0090, 0097, 009d and 06cb:009a (`usb.py:14-27`). The flow for those (`init.py:29-45`): `init_flash` (format flash if no partitions), `send_init` (`01`, `19`, `43 02`, `init_hardcoded`, maybe `clean_slate`), read TLS blobs from flash, TLS open, upload fwext (write flash partition 2), `sensor.open`, init db. It is built around an external SPI flash and on-chip template storage. Sensor types are limited to what `sensor.py` knows, and 0x2449 was not among them (the PR adds a stub that raises, `sensor.py` diff).

---

## 2. Where the constants came from, and what is in our DLL

Our DLL: `driverstore/synaWudfBioUsb.dll` (a local copy of the vendor driver, not published), 2,617,864 bytes, sha256 19cada14…ba707.

| Constant | Source in the effort | In our DLL? (my read-only search) |
|---|---|---|
| 10500-byte module sent after `06` on 0078 (iusearch `main.py:33`; ToLiKNoSoV `CMD_LONG1`) | recorded from Windows traffic | **Yes**, exactly, at file offset 0x19cca0 = module-table record 30 (gen 3, ROM 6/0x14, kind 0x400080). The two efforts' blobs are byte-identical to each other (sha256 prefix aa819075…). Gen-2 record 57 (the one 00a8 uses) is a different 10500-byte blob. |
| Module table, 66 records x 80 bytes at 0x15e004 (byPARSE, `dll_tables.py:24-40`) | read from the DLL | Yes. Records parse as described. ROM 6/0x14 has gen-2 records 56-59 and gen-3 records 29-34; same-kind modules differ between generations (0x4: 6100 vs 5796 B; 0x80, 0x100050, 0x400080: same lengths, different content). |
| Capture-program table, 64-byte records at 0x1eca04, rows #31 (6/0x14) and #32 (6/0x15) for dev_type 0x2449 (byPARSE comment 2) | read from the DLL | Yes, rows #31 and #32 exist. Row #29/#30 (0x0b4d) are the programs the PR aliases for 0x2449. |
| First 1121 bytes of the `02` command (header + capture program chunks) in iusearch and ToLiKNoSoV | recorded; common to both | Not contiguous in the DLL (built at runtime), but 67 of 71 24-byte windows I sampled occur in DLL tables (region around 0x1df32c-0x1e2f5c). The tail after byte 1121 (iusearch) or 1749 (between ToLiKNoSoV's two blobs) varies per unit or per call, so it is per-device calibration or session data. |
| Sensor-name row `(0xb7, 0x2449, 0x37, 0xff, 'SYN 86C  TM03267001')` (`hw_tables.py:212`) | python-validity tables, from the DLL | Yes, at 0x136884 (name) with the numeric fields just before it. |
| Firmware ECDSA public keys per ROM (`tls.py` of PR: 6/0x07 and 6/0x14) | read from the DLL | Yes. Little-endian x,y for 6/0x07 at 0x13a236/0x13a27a, for 6/0x14 at 0x13a848/0x13a88c. 0078 is ROM 6/0x14, so the same key should validate its ECDH parameters (only if it uses TLS at all). |
| `password_hardcoded`, `gwk_sign_hardcoded` (`tls.py:16-17`) | python-validity: taken from the driver | Not as contiguous bytes, and none of the four 8-byte quarters of either appears anywhere in the file. Consistent with byPARSE's statement that the secret is assembled by code at run time (comment 5). I did not disassemble the generator functions. The labels `GWK`, `GWK_SIGN`, `HS_KEY_PAIR_GEN`, `master secret`, `key expansion`, `client finished` are present (0x22f7d0, 0x22a000, 0x2318f0…). Not needed for plaintext capture. |
| `crt_hardcoded` (420-byte EC parameters / certificate template, `tls.py:20-31`) | driver | Yes, twice (0x210c30 and 0x2289c0) |
| 0090 `init_hardcoded[1:]` (484 B) and `reset_blob[1:]` | driver | Yes at 0x1b8ed0 and 0x1b90c0. These are 0090-generation blobs; the 0078 rejected them with 0x04be. |
| Flash partition layout and RSA signature blobs (`init_flash.py:32-100`) | driver / captures | Not searched. Not relevant unless a flash path is wanted. |

Provenance summary: every protocol constant these projects use is either (a) replayed from USB captures of the Windows driver (iusearch, ToLiKNoSoV, Validity90 `init_sequence_*`), or (b) lifted from data tables or code in the Windows driver DLL (python-validity tables and blobs, byPARSE's module and program tables, the firmware key). Nothing came from Synaptics documentation.

Do 00a8 findings plausibly transfer to 0078?
- Transfer with high confidence (same sensor type, same DLL tables, same RomInfo head, same image format): the capture-program chunk construction for type 0x2449; calibration procedure; the 0x51 image reply; image size 144x56 8-bit; interrupt decoding; the 04/52 cleanup; `0x3e` showing no external flash.
- Do not transfer: the gen-2 firmware modules (0078 uses gen 3); the 08/07 preamble and the end-of-session reboot (not seen in our ETW); the TLS/pairing path (not seen in our ETW); `set_configuration()` behaviour (iusearch's Python calls it on 0078 and reports success; 00a8 wedges); the claimed hardware performance of the matcher (different sensor unit, similar image geometry).

---

## 3. Anything that could break Windows Hello

What Windows Hello relies on, as far as we can tell: its template database lives on the host (`C:\Windows\System32\WinBioDatabase\*.DAT`, 162 KB, per HANDOFF-LINUX.md section 7), the sensor has no external flash (`0x3e` → JEDEC ff:ff, same on 00a8), and the Windows session on this machine uses no pairing or TLS (section 0, point 3). None of that proves the sensor holds nothing relevant; it makes it unlikely that a capture-only Linux session can disturb Hello.

| Effort | Step | Effect | Optional? |
|---|---|---|---|
| Synaptics78 | `08 …` write of HW register 0x80002060 = 0x0b | Volatile register write. Not seen in our ETW. | Probably omittable; keep out unless needed. |
| Synaptics78 | `06` module load, `02` capture program, `51`, `04`, `52` | RAM-only. These are exactly what Windows sends during identify here. | No: this is the capture path. |
| Synaptics78 (Python) | `dev.reset()` then `set_configuration()` at start | USB-level reset and config; not a protocol reset. | Yes (C++ version only claims the interface). |
| Validity90 prototype | `06` + 0090 blobs, `4f` 877 B partition-flash command, `1a` commit | Rejected by the 0078 in #63 (0x04be, 0x0401, 0x0491). Had the device accepted `4f`, this is the "format flash and re-pair" sequence. | Do not run this prototype. Its `FORCE_RESET` environment variable (`main.c:318-336`) also sends a factory-reset-style command. |
| python-validity upstream (if 0078's ID were just added) | `init_flash()` → `get_flash_info` raises "Unknown flash IC" for JEDEC ff:ff before any write (`flash.py:38-50`) | Safe by accident. If it passed: `06 reset_blob`, `4f` partitioning, `erase_flash` of partitions 1, 2, 5, 6, 4, `write_flash(1…)`, reboot (`init_flash.py:121-183`). Also `upload_fwext` writes partition 2 (`upload_fwext.py:55`), `persist_clean_slate` writes partition 6 (`sensor.py:631-633`). Those would destroy Windows' pairing and templates on a flash device. | Never reached on a flashless ID, but should be removed from any 0078 path. |
| python-validity upstream | `scripts/factory-reset.py`, `sensor.factory_reset()` (`sensor.py:85-88`: `reset_blob`, then `10 00…`, then reboot) | Explicit wipe, user-invoked only. | Yes |
| PR #288 | `pair()`: `06` pair module, **`4f` client certificate**, `01`, `50`, **`1a` commit** (`init_flashless.py:98-127`). Runs on first use (no stored pairing file) and again whenever the stored pairing is rejected (`init.py:90-102`). | This installs a new host identity into the sensor. The PR says "the device remembers its side of the pairing"; `1a` is the commit command (`flash.py:115-121`). On 00a8, a Linux pairing would replace Windows' pairing, and what Windows then does is unknown. No source states the outcome. **Moot for 0078 if plaintext capture works**, because Windows does not pair in that session. | Yes. Skip for 0078. |
| PR #288 | `05 02 00` after every operation | Sensor reboot. Benign, not in our ETW. | Yes |
| PR #288 | USB reset on any failure (`init.py:64-71`) | Benign | Yes |

Dual-boot note for the flash-based family (not 0078): python-validity's README ("Windows interoperability", lines 121-138) and MR !626's description ("pairings made by either still open") say Windows and Linux share one DMI-derived key and the same on-chip templates, so re-pairing is not required there. HANDOFF-LINUX.md section 5 assumes the opposite for 0078. Given no flash and a plaintext Windows session, the real answer for 0078 is: probably no pairing at all.

Unverified and worth a read-only check later (when the device may be touched): the sensor's reply to `0x01` has byte 0x19 = 0x00 on every 0078 trace we have (#63) but 0x21 on 00a8. The Windows driver decides per this byte whether `4f` carries flash sections (byPARSE comment 5, `FUN_180080d60`). We have not seen whether our unit differs. Also, the first 1-byte command in our ETW (reply 2 bytes) is unidentified.

---

## 4. Architecture options

Facts both options share: capture on 0078 needs no TLS; the image is 144 x 56, 8-bit; the host does all matching; the Windows driver must be consulted for the capture program and modules, which cannot be redistributed.

### Option A: native libfprint driver in C
- Upstreamability: possible in principle, uncertain in practice. The module blobs (10.5 KB plus capture-program tables) are Synaptics-copyrighted; ToLiKNoSoV embeds 300 KB of replayed blobs; libfprint upstream does not take embedded vendor blobs. MR !626 (the validity VCSFW driver, open since 2026-07-16) loads such blobs at runtime from `/usr/share/libfprint/validity/*.bin` with an HMAC trailer (`validity_data.h`) and leaves the policy question to the maintainers. A data-package route is the only plausible one, and it is unresolved. MR !626 already ships TLS and pairing in C with OpenSSL, but it is match-on-chip and irrelevant to plaintext capture.
- fprintd integration: native. Real fprintd, GNOME/KDE UI, PAM, hwdb autosuspend entry; no replacement daemon, no Python or OpenCV in a root service.
- Effort: the protocol side is small if TLS is not needed (`01`, `19`, `75`, `06`, `02`, interrupt on 0x83, `51`, `04`, `52`). The `02` command builder is the bulk (about 600 lines in `sensor_2449.py` plus `timeslot.py` and three DLL tables); it has to be ported to C or replaced by a captured command with substitution of per-device calibration.
- Matching 144 x 56 images: libfprint's NBIS (mindtct + bozorth3, default threshold 40, `fp-image-device.c:25`) was never shown to work on this size. What others did in libfprint: egis0570 (114-wide strips, assembled, `fpi_image_resize` x2 at `egis0570.c:201`, bz3_threshold lowered to 25 with the developer's comment "foreget about security" at `egis0570.h:163-170`), nb1010 (256 x 180, press, threshold 24), elan (swipe, frame assembly). The image-device base class lets a driver set `bz3_threshold`, and `fpi_image_resize` exists (`fpi-image.h`). Alternatively a non-image `FpDevice` can do its own host-side comparison on a raw template, which allows a matcher like PR #288's, but then the algorithm has to be written in C (SIFT+RANSAC from OpenCV is not an acceptable libfprint dependency). Neither Synaptics78 (explicitly gave up on libfprint) nor ToLiKNoSoV (claims NBIS templates created, matching in fprintd not working; see `research/review-toliknosov.md`) has shown that NBIS gives usable FAR/FRR at this size.
- Risk: the accuracy question can be tested before committing, offline, once a few dozen captures exist.

### Option B: python-validity / open-fprintd extension
- Speed: fastest. PR #288 already contains most of the 0x2449 logic, the host matcher (tuned on the sibling's same-size images), quality gating, enroll/verify state machine and D-Bus glue. Adapting to 0078 means: add 06cb:0078, use the gen-3 capture module (record 30) and the matching calibrate module, drop pairing/TLS and (probably) the 08/07 preamble and reboot, fix the DLL hash check, and re-derive whatever differs in the chunk 0x26/0x2e builders. Licence: python-validity is MIT.
- Upstreamability: to python-validity only. The upstream repo's last push was 2025-06-10, it has 207 open issues, and PR #288 has no reply yet, so in practice the work would live in a fork. Nothing here goes to libfprint.
- fprintd integration: via open-fprintd, which replaces fprintd on the system bus (they conflict) and implements the fprintd D-Bus API. Works with pam_fprintd. open-fprintd 0.7 shows 5 enroll stages and cannot delete a single finger (PR #288 README); PR #29 fixes this but is open. Option B depends on both projects.
- Weight: Python, numpy and OpenCV in a root service; 18 touches to enroll (Windows also uses 18, per `ops.py:32`).
- Matching: SIFT/RANSAC on 2x upscaled, CLAHE-equalised 144x56 images works for the 00a8 author's data (claimed FRR 2.1% at 18 touches, impostor max 4 vs threshold 6; 145 captures, 5 fingers). That is the only quantitative small-image result in this whole material.

### Hybrid (suggested sequencing)
1. Prototype in Python against PR #288 to nail the 0078 plaintext flow with read-only commands first (`01`, `19`, `3e`, `75`).
2. Collect captures and score NBIS against the SIFT matcher offline.
3. Choose C or Python from that result. If NBIS cannot reach acceptable FAR/FRR, a libfprint driver needs a custom matcher, and Option B's value goes up.

---

## 5. Use of each effort in this project

| Effort | Use in this project | Why |
|---|---|---|
| iusearch/Synaptics78 | **Protocol reference** (the only open code for the 0078 plaintext flow) | Its wire sequence is corroborated by our ETW and by the DLL. Not used as code: it is a pure replay, the tail of the `02` command is one unit's calibration, there is no licence file, and it captures one image and exits; last commit 2019-09-03. Use it as a checklist of opcodes and interrupt strings. |
| Validity90 #63 and prototype | **Data point only; do not run the prototype** | The log gives clean 0078 replies for `01`, `19`, `3e`, `75` (and shows the sensor refusing the 0090 blobs and `4f`). The code is written for other devices, and it sends flash-partition and reset commands, which is why it must not be run on a 0078. |
| python-validity issue #179 (byPARSE) | **Protocol reference** for 0x2449 internals | Valuable decoding notes (module table, capture-program rows, image record format, chunk 0x26 layout). Superseded by PR #288 for working code. |
| ashh87 PR #288 + open-fprintd PR #29 | **Best base to build on or to port from, after adaptation** | Only complete chain for the 0x2449 family: capture program builder, calibration, interrupts, image parse, host matcher, D-Bus device. Caveats: 00a8-specific (gen-2 modules, TLS pairing, reboot); the PR has no review comments yet, and its hardware claims are not verifiable from the repository. Port the sensor module logic and the matcher ideas, not the pairing. |
| python-validity upstream | **Protocol reference and utility code only** | Useful: `hw_tables.py`, `generated_tables.py`, `timeslot.py`, TLS code if ever needed. For 0078 its flow is built around external flash, and its default path would try to partition flash if the flash probe did not fail first. |
| MR !626 (libfprint validity driver) | **Reference for a C driver's structure** | Shows how a C driver handles runtime data packages, USB lifecycle and OpenSSL TLS, but it is match-on-chip and its devices are a different family. |

---

## 6. What I did not verify

- I did not build or run anything, so all hardware claims (images, FRR, finger detection) stay claims.
- I did not disassemble the DLL. Statements about `FUN_…` functions are byPARSE's.
- The 1-byte command at the start of our ETW session and the meaning of the 5-byte interrupt payloads for 0078 are unidentified.
- Whether Windows pairs on this 0078 at some other time (enrollment, driver install) is unknown; the ETW covers one identify.
- Whether a different 0078 unit/firmware (bcdDevice other than 0x0154) uses the same flow: the #63 trace and the iusearch/ToLiKNoSoV blobs suggest yes (same ROM, same module), but we only have our own ETW for our unit.
- I could not check the PR's hardware results independently of its author.

Supporting scripts (data-only parsing, not from the repositories; local working files, not published): `s78_parse.py`, `cmp_blobs.py`, `dllcmp.py`, `dllsearch.py`, `dllsearch2.py`, `calcmp.py`.
