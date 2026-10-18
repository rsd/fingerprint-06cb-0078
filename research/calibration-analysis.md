# Where the per-unit part of the 18869-byte `02` write comes from

Offline analysis, 2026-10-13. The USB device was not touched. Third-party code was read as data and never executed. The payload builder and its chunk parser are in `research/calib/` (`build_ours.py`, `chunks.py`); the other scripts and intermediate files are local working files, not published. I disassembled our `driverstore/synaWudfBioUsb.dll` (a local copy of the vendor driver, not published) with capstone, in a private venv. Function addresses below are virtual addresses (image base 0x180000000). File offsets are marked as such.

Reference payloads:
- `iu.bin`: iusearch/Synaptics78 `main.py`, the `02` table write plus the `81 7e` frame, concatenated to 18869 B.
- `tk2.bin` and `tk3.bin`: ToLiKNoSoV's `CMD_LONG2` and `CMD_LONG3`.

## 0. Answer

**Conclusion (a), high confidence (about 90%).** Our unit's data is already in `extracted/CalibrationData_decrypted.bin`, and the mapping is exact. With it I built our unit's 18869-byte payload offline: `ours_cmd02.bin` (per-unit, local, not published), sha256 prefix `fac382964da2e908`.

The 18869-byte write is the `02` capture command. It is a chunked capture program. All per-unit bytes come from three cached inputs, through a transform I confirmed in the DLL and verified byte for byte:

| Payload part (file offsets in the 18869 B write) | DLL chunk name | Source in CalibrationData (file offsets) | Transform |
|---|---|---|---|
| 1121–1264, first 144 B of chunk 0x34 | "Timeslot Table 2D" (key line) | calibration frame, 0x124–0x2263 (56 lines x 152 B) | rows 18/28/38, one 48-column band each; `q = clamp_s8((b-0x80)*10/4)`, then `5 -> 4` |
| 2129–10192, chunk 0x30 data (8064 B) | "Line Update" | same calibration frame | 36 column groups x 56 rows x 4 columns, same formula, no 5->4 step |
| 10258–10334, inside chunk 0x43 | "Line Update Transform" | 56 factory values, 0xEC–0x123 | python-validity `bitpack()`: `v0=(bits-1)|8`, `v1=min`, bitstream; written twice (regs 0x8000203c and 0x80002040) |
| 10357–18868, chunk 0x48 data (8512 B) | "Baseline Frame" | baseline frame, 0x44AC–0x65EB (56 x 152 B) | verbatim copy, line headers included |

The other variable bytes are capture settings, not unit data:
- 0x26 bytes 1713 and 1717: finger-detect threshold `ival`, 0xa0 at quality 100 versus 0xaa at quality 90.
- 0x2e byte 1749: capture flags, 0x02 versus 0x23.

Evidence, in order of strength:
1. **DLL code** (section 4). The `02` assembler's sub-builders read exactly these fields of the driver's calibration struct. The registry serializer writes the same fields to `CalibrationData` in the layout found in our blob. The deserializer refuses a blob whose stored serial does not match the live RomInfo serial. Our blob stores `<serial>`, our unit's serial.
2. **Byte-exact round trip.** From each reference payload I took its three inputs (factory values; the calibration frame, inverted from the int8 values; the baseline frame). Re-encoding them with my builder reproduces `iu.bin` and `tk2.bin` byte for byte. So nothing else in the payload varies per unit.
3. **Statistics and layout.** Our blob's blocks have the sizes, descriptor format and value distributions of the corresponding reference regions (section 2).

What Linux can do:
- Use the cached data as Windows does. During identify, Windows reads nothing from the sensor before this write except RomInfo, `19` and `75` (section 3).
- (b), partly: the 56 factory values can probably also be read from the sensor with the read-only command `0x6f` (get factory bits), which the DLL builds as a 9-byte request.
- (c): the two frames cannot be read from the sensor. Windows computes them in a calibration run with no finger on the sensor. A Linux re-calibration would follow PR #288's procedure for 00a8, with the gen-3 modules. That is the fallback if our cached data goes stale, and the path for other users.

## 1. Structure of the 18869-byte payload

`02 | u16 bytes_per_line = 0x0098 | u16 req_lines = 0 | chunks`. Each chunk is `u16 type, u16 len, data`. The DLL's own chunk-name table (function 0x180087870) gives the names. All three reference copies parse identically:

| Chunk | DLL name | Data at file offset | Length | iu vs tk |
|---|---|---|---|---|
| 0x23 | Reply Config + Timeslot Table | 9 | 0 | same |
| 0x20 | Set Register 32 | 13 | 8 | same |
| 0x32 | Register List 16 | 25 | 92 | same |
| 0x33 | Register list 32 | 121 | 40 | same |
| 0x32 | Register List 16 | 165 | 64 | same |
| 0x33 | Register list 32 | 233 | 868 | same |
| 0x32 | Register List 16 | 1105 | 12 | same |
| 0x34 | Timeslot Table 2D | 1121 | 536 | **bytes 0–143 differ** (142 of 144); the timeslot table after them is the same |
| 0x42 | Data Encoder | 1661 | 8 | same |
| 0x2f | 2D | 1673 | 4 | same (0x38 = 56 lines) |
| 0x29 | Timeslot Table Offset | 1681 | 4 | same |
| 0x35 | Timeslot Table Offset for Finger Detect | 1689 | 4 | same |
| 0x17 | Reply Configuration | 1697 | 0 | same |
| 0x26 | Finger Detect | 1701 | 40 | 2 bytes (1713, 1717): `ival` 0xa0 versus 0xaa, a quality setting |
| 0x2e | Image Reconstruction | 1745 | 28 | iu = tk2; tk3 differs at 1749 (flags 0x02 versus 0x23) |
| 0x47 | Enable ASP Feature | 1777 | 0 | same |
| 0x44 | Line Update InterLeave | 1781 | 4 | same |
| 0x30 | Line Update | 1789 | 8404 | 340-byte line table the same; **8064-byte data differs** (7910 B) |
| 0x43 | Line Update Transform | 10197 | 152 | **66 bytes differ** (10258–10334) |
| 0x48 | Baseline Frame | 10353 | 8516 | `u16 152, u16 56` the same; **frame differs** (6674 B); its 56 x 8 line-header bytes are the same in both |

All 14,794 differing bytes between iu and tk2 fall in the regions marked above.

Fixed part: everything else, about 2.1 KB. Most fixed chunks occur verbatim in our DLL's capture-program tables:
- 0x20 at file 0x1dbfa4
- 0x33/40 at 0x1df390
- 0x32/64 at 0x1df3bc
- 0x33/868 at 0x1e0e40
- 0x32/12 at 0x1dfa70
- 0x32/92 at 0x1e2f30
- 0x42 at 0x1dfc90
- 0x2f, 0x29 and 0x35 at 0x1de018–0x1de028

The 0x34 timeslot remainder (patched at run time) and the 0x26 and 0x2e chunks (built at run time; PR #288 has the builders) do not occur verbatim. The 1121-byte "header" from the earlier reviews is the first seven chunks of this program.

Inside the variable chunks:
- **0x30 Line Update**: `u32 n = 42`, then 42 `(mask, flags)` pairs, then data. The entries are:
  - the calibration-blob line;
  - two factory-value lines, at timeslot-program PCs that write 0x8000203c and 0x80002040;
  - two empty "Enable Rx" lines;
  - one fixed line;
  - 36 lines with flags `0x85000000 | col`, col = 0, 4, …, 140, each carrying 56 rows x 4 columns of int8.
  So the 8064 data bytes are a 56 x 144 int8 array stored column-group-major. This is the per-pixel offset correction (sensor rows x columns = 56 x 144, the image size).
- **0x34 key line**: 144 int8 = columns 0–47 of row 18, 48–95 of row 28 and 96–143 of row 38 of the same array, with q = 5 replaced by 4. It matches the 0x30 array 144/144 in both references.
- **0x43**: `0f 00 00 00` + 56-byte calibration blob (0c..43). Then twice `[v0 = 0x0c, v1 = min, 0, 0]` + 35-byte bitstream, which encodes 56 values with 5 bits each above `min`. Then three empty entries.
  - Decoded factory values: iu `76 82 8c 88 88 8d …`, tk `77 82 8c 87 89 8c …`.
  - These are 56 per-row values. The two units correlate 0.973 with each other: a shared design profile plus unit variation.
- **0x48**: 56 lines x 152 B. Each line has an 8-byte header `01 fe (2r+1) 02 f0 f7 01 00` and 144 pixels near 0x80 (sd about 1.9). It is a flat "blank" frame taken after correction.
- No checksum anywhere. The round trip in section 0 shows nothing else varies.

The scale is fixed by the data. All 16,128 int8 values in the two references lie in the image of `(b-0x80)*10/4` (or 5/2, the same ratio). Ratios 10/0x22, 10/3 and 10/5 leave thousands outside. 0x30 contains q = 5 and never q = 4; the key line contains 4 and never 5. That is the 5->4 step applied only to the key line, which the DLL code confirms.

## 2. Our unit's counterpart in CalibrationData_decrypted.bin

Layout, confirmed against the serializer and deserializer code (section 4):

| File offset | Content |
|---|---|
| 0x0000 | u32 0x226c: length of record 1 |
| 0x0004 | u32 0xe4: header + struct size (the deserializer requires 0xe4) |
| 0x0008 | u32 2: version (the deserializer requires 2) |
| 0x000c | u32 0x2449: sensor type (must equal the live type) |
| 0x0010 | RomInfo copy: `10b60454 9a000000 0614 0133 0001 000f`, then **serial `<serial>`** at 0x20 (the deserializer memcmp's 6 bytes against live RomInfo+0x10). Byte 0x1f is 0x0f here but 0x00 in today's Linux reply; not explained, not used. |
| 0x002c | the calibration struct C, 0xC0 bytes, a raw dump with stale heap pointers. Pairs `(ptr, u32 size)` at C+0x00/08, 0x18/20, 0x28/30, 0x38/40, 0x50/58, **0x68/70 = 56**, **0x80/88 = 8512**, 0x98/a0 = 0. C+0xb8 = 6, C+0xbc = 7. |
| **0x00ec** | **block C+0x68: 56 factory calibration values** (`<unit-specific>`, not quoted) |
| **0x0124** | **block C+0x80: calibration frame**, 56 lines x 152 B (header `01 fe (2r+1) 02 f2 f7 01 00` + 144 px) |
| 0x2264 | 3 x u32 = 0 (driver fields `ctx+0x298/0x29c/0x2a0`) |
| 0x2270 | u32 0x4410: length of record 2 |
| 0x2274 | three 0x20-byte frame descriptors `{u32 1, u32 w = 152, u32 h = 56, 0, ptr data, ptr extra}` for `ctx+0x98` (empty), `ctx+0xb8` and `ctx+0xd8` |
| 0x22d4 | frame `ctx+0xb8`: 8512 B, plus one extra 152-byte line (descriptor +0x18) |
| **0x44ac** | **frame `ctx+0xd8` = Baseline Frame**: 8512 B, plus one extra line. It is byte-identical to the frame at 0x22d4. |

The 4 bytes before the factory values (`07 00 00 00`, C+0xbc) look like the 4-byte prefix that python-validity strips from factory-bits subtag 3 (`factory_bits[3][4:]`; its sample reply starts `07000000 7e7f80…`).

Alignment with the reference regions:

| Quantity | iusearch | ToLiKNoSoV | ours |
|---|---|---|---|
| factory values: count, range | 56, 0x76–0x90 (5 bits) | 56, 0x77–0x91 (5 bits) | 56, 0x76–0x8f (5 bits) |
| factory correlation with the other units | iu-tk 0.973 | | ours-iu 0.920, ours-tk 0.913 |
| calibration frame: sd, row-mean sd, col-mean sd | 19.8\*, 2.97, 1.26 | 20.5\*, 2.54, 1.14 | 19.4, 2.47, 1.02 |
| calibration int8: clamped share, count of q = 5 | 1.02 %, 184 | 1.49 %, 170 | 1.07 %, 178 |
| baseline frame: mean, sd, row-mean sd, col-mean sd | 127.6, 1.98, 0.26, 0.29 | 127.9, 1.89, 0.31, 0.28 | 127.6, 1.98, 0.23, 0.28 |
| per-pixel correlation of calibration frames between units | iu-tk 0.18 | | ours-iu 0.11, ours-tk 0.10 |

\* recovered from the int8 values; values beyond ±51 are clamped in the payload.

Per-pixel patterns are unit-specific, as fixed-pattern data should be. That is why the earlier byte and correlation searches (`research/review-toliknosov.md`, `research/review-prior.md`) found no substring or correlation match against the foreign copies: they were comparing two different sensors.

One small difference: the line headers of all our stored frames carry `f2 f7`, while both references' 0x48 lines carry `f0 f7`. The DLL copies the frame verbatim (section 4), so Windows would send `f2` for our unit. I kept the stored bytes.

## 3. Values from the sensor itself; what Windows reads before the write

- The variable region holds no serial and no `0x19` reply words. The round trip leaves no room for them, and byte searches found neither ours (`<serial>`, `<unit-specific>`, `<unit-specific>`) nor #63's (its serial and its two unit-specific `0x19` words; shown here with synthetic placeholders, `00630a00630b`, `63190001`, `63190002`) in either payload.
- Neither reference repository recorded its own unit's RomInfo or `0x19` reply; only Validity90 #63 (cydia2020's unit) did, and that unit has no `02` copy. So a cross-unit identity check is not possible.
- The serial appears only in CalibrationData, as the key that binds the blob to the sensor.
- ETW, our identify session. Completed transfers before the 18869-byte OUT, in order:
  - control 0xC0/0x14 → 2 B
  - OUT 1 → IN 2
  - OUT 1 → IN 38 (RomInfo)
  - OUT 1 → IN 64 + 4 (`19`)
  - OUT 1 → IN 10 (`75`)
  - OUT 10501 → IN 2
  - then OUT 18869 → IN 2154

  No IN transfer could carry 56 factory bytes plus their header, let alone a 17 KB frame. Nothing was read from EP 0x82 (calibration frames). So Windows built the payload from the cached registry data, not from the sensor.
- Commands that could read per-unit data:
  - `0x6f` "get factory bits". In python-validity, `get_factory_bits(0x0e00)` returns subtag 3 = the per-row factory calibration values. PR #288 calls it on 00a8.
  - Our DLL builds a 9-byte `0x6f` request in function 0x180083cf0 (reply buffer 0x1008 + 12·n), matching python-validity's `pack('<BHHL', 0x6f, tag, 0, 0)`.
  - It is not in our Windows identify ETW.
  - No command returns the frames. They are host-computed averages of finger-off captures.

## 4. DLL code that builds the payload and stores the data

All addresses are functions in `synaWudfBioUsb.dll` 5.5.2707.1073 (our `driverstore/` copy). The code is barely optimised, so struct offsets read directly.

- **0x18007e380**: assembler of the `02` capture program (`scsFwV4.c`). It gets its context through 0x18007cc80 (`ctx`) and calls the builders below. The sensor type is at `ctx+0x128`, the RomInfo pointer at `ctx+0x28`, and the calibration struct pointer C at `ctx+0x288`.
- **0x18008af70**: key line, chunk 0x34.
  - Stride = `C.u32@0x88 / T.u16@0x12` (8512 / 56 = 152).
  - Rows 0x12, 0x1c, 0x26 (18, 28, 38) for every type except 0x195.
  - Band width `T.u16@0x32 / 3` (48).
  - Source `[C+0x80] + row*stride + 8 + band offset`.
  - `q = (b-0x80) * T.u16@0x88 / T.u16@0x8a`, clamped to [-0x80, 0x7f]; then `if q == 5: q--` (0x18008bb80–0x18008bc45).
  - T is the sensor-type record. The ratio 10/4 is fixed by the data (section 1), not read from T.
- **0x18008dac0**: line-update dispatcher. If RomInfo product (`[ctx+0x28]+0xb`) is 0x30, it calls 0x180098710 or 0x180095470 by sensor type. If it is **0x32 or 0x33** (ours is 0x33), it calls **0x18009ba40**.
- **0x18009ba40**: Line Update and Transform for product 0x32/0x33. It has explicit `cmp 0x2449` branches.
  - It locates the register writes to 0x800020fc, **0x8000203c** and **0x80002040** in the timeslot program, then reads **C+0x68/C+0x70** (the 56-byte block). These are the 0x43 factory-value entries.
  - The per-column entries read **C+0x80/C+0x88** with a +8 line-header skip, 4 columns per entry, the same formula and no 5->4 step (0x18009dbe2–0x18009dcec).
- **0x18008dde0**: Baseline Frame, chunk 0x48. Called from 0x18007f267 with `ctx+0xd8`, only when the capture type argument is 4 and a feature check (0x180061480) passes. It writes `0x48, len, u16 w(@+4), u16 h(@+8)`, then a memcpy of `w*h` bytes from descriptor +0x10. No transform.
- **0x18008eda0**: Finger Detect, chunk 0x26 (constant 0xb2fb), via 0x18008c720.
- **0x18005e470 / 0x18005eab0**: serializer and deserializer of record 1. They write and read the 0x24-byte header (sensor type, RomInfo), the C struct (0xC0 bytes), then each `(ptr, size)` block of C in field order, then `ctx+0x298/0x29c/0x2a0`. The deserializer checks length ≥ 0xe8, the 0xe4 tag, version 2, the sensor type, and `memcmp(stored serial, RomInfo+0x10, 6)`.
- **0x1800628e0**: serializer of record 2. It writes the 0x60 bytes at `ctx+0x98` (three frame descriptors), then for each descriptor `w*h` bytes from +0x10 and `w` bytes from +0x18.
- **0x1800473f0**: calls both serializers and writes the record lengths.
- **0x18001a700** (`CBiometricDevice::BackupCalibrationData`): fetches the serialized buffer (size query, alloc, fill through the core API) and writes the registry value `CalibrationData` (wide string at file offset 0x12c140) through 0x1800248cc. The registry copy is DPAPI-protected, as found earlier.
- **0x180083cf0**: builder of sensor command 0x6f.

How far the algorithm is understood:
- From cached data to payload: complete, verified by the byte-exact rebuild of both references.
- From raw calibration captures to the cached frames: not traced in the DLL. PR #288 describes it for 00a8: calibrate module, `02` CALIBRATE with `req_lines = 3·112 + 1`, read EP 0x82, skip frame 1, average interleaved line pairs, then a second CALIBRATE for the baseline frame, then `0x1a`.
- Where C+0x68 is first filled from (presumably `0x6f`): not traced.

## 5. Producing it on Linux

- `research/calib/build_ours.py <template.bin> <CalibrationData_decrypted.bin> <out.bin>` builds the payload. It imports `chunks.py`, which sits next to it.
  - It takes the fixed chunks from a template (iusearch's copy; it is the same in both references).
  - It replaces the 0x34 key line, the 0x30 data, the 0x43 factory entries and the 0x48 frame from our blob.
  - It asserts the stored sizes (56, 8512, descriptor 152 x 56).
- Output: `ours_cmd02.bin`, 18869 B (per-unit, local, not published; `.gitignore` excludes `research/calib/ours_cmd02.bin`). It differs from iusearch's copy in 14,964 bytes, all inside the per-unit regions plus the 56 `f2`/`f0` header bytes.
- It decodes back to our inputs: factory values equal, key line consistent 144/144, baseline frame equal to the stored frame.
- It keeps iusearch's 0x26 `ival` 0xa0 (quality 100, which PR #288 says Windows passes) and 0x2e flags 0x02 (identify).
- For a real driver, generate the fixed chunks from the DLL's capture-program tables, as PR #288 does, instead of copying a reference payload. Most of them occur verbatim in our DLL (section 1).

Scripts (data-only): `chunks.py` and `build_ours.py` are in `research/calib/`. `decode.py`, `diff.py`, `stats.py`, `corr.py`, `checks.py`, `dis.py`/`xr.py`/`callargs.py`/`names.py`/`strs.py`/`funcof.py` are local working files, not published. `dis.txt` (local, not published) holds the full disassembly, with function bounds taken from `.pdata`, and `xref.pkl` the RIP-relative cross-reference index.

## 6. Caveats

- **Staleness.** The cache is whatever Windows last calibrated. I did not trace when Windows recalibrates. Our identify session did not.
- **Firmware state.** PR #288 reports that on 00a8, chunk 0x48 crashes the device unless the firmware was calibrated (`0x1a`) since pairing. On 0078 the evidence points the other way, but is indirect:
  - Our Windows identify sends the full 18869 bytes, so 0x48 is included, with no `0x1a` in the session.
  - iusearch and ToLiKNoSoV both send `06` and then `02` with 0x48 straight after `01/19/75`, and claim images.
  - Fallback: drop the 0x48 chunk (10349 bytes).
- **Header byte.** Our frames carry the line-header byte `f2` where the references have `f0`. Windows would send ours as stored.
- **The extra 57th line.** Each stored frame has one more 152-byte line (descriptor +0x18). The `02` builder does not use it.

## 7. Cheapest next experiments (sensor, when allowed)

1. **Read-only check of the factory block.**
   - Send `6f 00 0e 00 00 00 00 00 00`, then parse the entries as python-validity's `get_factory_bits` does. Subtag 3's value minus its 4-byte prefix should equal `CalibrationData[0xEC:0x124]` byte for byte.
   - This confirms the C+0x68 mapping and gives Linux a live source for those 56 bytes.
   - `0x6f` is a read in python-validity, which sends it on every open, but it is not in our Windows ETW.
2. **Functional test of the payload.** Mirror the Windows identify session exactly:
   - `01`, `19`, `75`;
   - `06` + DLL module at file offset 0x19cca0;
   - `ours_cmd02.bin` as is (it already starts with the `02` byte). Expect `00 00` and a 2154-byte reply, then a 5-byte EP 0x83 interrupt.
   - Touch, then `51 00 20 00 00` for the image, then `04`, `52`.

   Compare image flatness and contrast with the same capture using iusearch's foreign payload. Ours should give a flatter background.
