# B2: where the per-unit parts of the `02` program come from

Research step B2, 2026-10-16. Offline only: the sensor was not touched. Sources:
- static disassembly of `driverstore/synaWudfBioUsb.dll` 5.5.2707.1073 (sha256 `19cada14…a707`; addresses are VAs, image base 0x180000000);
- our decrypted CalibrationData and our earlier read-only `6f` probe log (`proto/logs/`, 2026-10-13);
- the three reference payloads (iusearch, ToLiKNoSoV, ours);
- python-validity master and PR #288 (00a8, sensor type 0x2449).

iusearch/Synaptics78 and both ToLiKNoSoV repos contain no calibration code; they replay recorded bytes.

The full working notes, with every address, are local working files, not published.

## Verdict

**Feasible on Linux without Windows, but it needs one on-device experiment first.** It cannot be done fully offline: two of the three per-unit inputs are measured frames that only the sensor can produce.

- Windows makes CalibrationData from:
  - factory values it reads from the sensor with the read-only command `6f`;
  - two blank (no-finger) calibration captures, which the host averages and decodes.
- None of the three inputs is Windows-only. None needs a flash write. The calibration commands are RAM-only: a module upload and capture programs.
- The capture command for the calibration has never been sent from Linux, so it needs one experiment (below). The experiment was not run in this analysis step; it was run afterwards (see `b2-run.md`).

## Byte map of the 18869-byte `02` payload

"Calibration frame" (CF) = C+0x80, 56 × 152 B. "Baseline frame" (BF) = descriptor `ctx+0xd8`, 56 × 152 B. `q(b) = clamp_s8(trunc((b − 0x80) · 10 / 4))`.

| Payload bytes | Chunk | Content | Origin |
|---|---|---|---|
| 1121–1264 | 0x34 key line | CF rows 18/28/38, one 48-column band each, `q`, then 5 → 4 | blank capture #1 |
| 2129–10192 | 0x30 data | CF, 56 rows × 144 columns, `q`, column-group-major | blank capture #1 |
| 10258–10334 | 0x43 | 56 factory values, bit-packed, written twice | sensor command `6f 00 0e 00 00 00 00 00 00`, entry subtag 3 (flags 0x8000), value bytes 4..59 |
| 10357–18868 | 0x48 frame | BF, verbatim (line headers included) | blank capture #2 |
| everything else | fixed chunks | DLL capture-program tables plus run-time builders (0x26 and 0x2e are capture settings) | not per unit |

Each blank capture works the same way:
- `02` with `req_lines = 337` (336 + 1, so the read is not a multiple of 64 bytes).
- EP 0x82 delivers 337 lines × 152 B = 51 224 B.
- The host keeps frame 2 only (lines 112–223). Output row r is the bytewise `floor((L[112+2r] + L[113+2r]) / 2)`, the 8 header bytes included.

The two captures differ like this:

| | Capture #1 → CF | Capture #2 → BF |
|---|---|---|
| Calibration applied? | none: key line all zero, 0x30 has only its 6 fixed entries | the CF just measured: key line plus the 36 per-column 0x30 entries |
| Decode after averaging | none: CF stays in the raw (encoded) domain | each line's 144 samples are decoded |

The decode works on 9 blocks of 16 samples:
- `y = clamp((M·x) >> 2, 0, 255)`;
- `M = A ⊗ A`, with `A = 2·I₄ − J₄` (a 16×16 Hadamard matrix, `M·Mᵀ = 16·I`);
- in the DLL: 0x1800c7ac0 / 0x1800c8010, selected by chunk 0x42 "Data Encoder" (`02 08 90 …`: type 2, offset 8, 144 samples).

## How Windows creates CalibrationData

The trigger:
- `CBiometricDevice::CalibrateDevice` restores the registry value.
- When the value is missing, cannot be restored, or a recalibration is forced, it calls the vfm layer, which calls `scsCalibrate` (0x18005e2a0).
- After a successful calibration, `BackupCalibrationData` writes the result to the registry.

`scsCalibrate` then works in this order:
1. **Factory data.** It allocates a fresh C struct (0xC0 B) and reads factory bits with `6f`, tag 0x0e. The parsed entries are cached at C+0xb0, their count at C+0xb8 (6 on our unit).
2. **Second factory query.** `6f` with tag 0x0a is optional. Its result goes to C+0x28 and the flag C+0x34. On our unit it is empty (C+0x34 = 0).
3. **Stage loop** (0x18006b280 → 0x18006b320, stages 0–9). The flags of the 0x2449 type record (`T+0x1c = 0x81`) enable:
   - **stage 3** (0x18006d240): C+0x68 = the 56 values of subtag 3. C+0x78/0x7a come from the entry's 4-byte prefix.
   - **stage 6** (0x18006ef50, arguments 10, 4, 160, rows table, 0, 200):
     - It frees CF, then runs blank capture #1 (0x180065790 → 0x180061c20 → 0x180065d90 → 0x180062d70, averaging callback 0x1800907e0 / 0x180091f70) and copies the result into CF.
     - Then it checks CF against factory **subtag 12**, which holds 720 B: int8 offsets measured at the factory for rows 4/18/28/38/51 × 144 columns. The score is `Σ(F − q(CF))² / 4096` and must be ≤ 200; it is stored in C+0xbc.
     - There is no iteration (`T+0x98 = 0`), unlike python-validity's older sensors.
   - **stage 7**: blank capture #2, with CF applied and the decode on. The result is the BF.
   - **stage 9**: sets the "calibrated" flag.
4. **Module.** The calibration requests firmware module kind mask 0x80. The DLL's matcher (0x1800922c0) picks the smallest match: record 40, kind 0x000080, ROM 6.20, gen 3, 4676 B, module id 0x0f, sha256 `abdf9188aa18b2570353869d93ac85a66fc48a00461bb43cded16c1b03166085`, at file offset 0x19f5b0 of our DLL.
   - Our CalibrationData's stored RomInfo copy has byte 0xf = **0x0f**, so this module was resident when Windows calibrated our unit.
   - The capture module (record 39, kind 0x400080, id 0x0e) also satisfies mask 0x80. The DLL skips the upload when it is already loaded (0x1800646b0).

Calibration-path commands on the wire: `6f` (read), `1a` (only when another module is loaded), `06` + module, `02` (calibration program), and the EP 0x82 read. There is no flash command, no `4f`/`50`, and no `0x48` chunk.

## What a Linux tool has to do

1. `01`, `19`, `75` as now. Then `6f 00 0e 00 00 00 00 00 00`: keep subtag 3 (values) and subtag 12 (check reference).
2. Make sure a module with kind bit 0x80 is resident:
   - module 39, our normal state;
   - or module 40, the way Windows did it from a fresh state.
3. Send calibration program #1:
   - `02 98 00 51 01`, then the fixed chunks 0x23 … 0x35 with the key line zeroed;
   - then `0x17`, `0x44` (u32 1), 0x30 with only its 6 fixed entries, and `0x43` (factory values);
   - no 0x26, 0x2e, 0x47 or 0x48;
   - 1917 B in total.
   
   Read 51 224 B from EP 0x82 and average frame 2 to get CF. Accept CF only if the subtag-12 score is ≤ 200; otherwise retry with no finger on the sensor.
4. Send program #2, the same but with the key line and the 36 column entries built from CF (10 269 B). Read 51 224 B, average frame 2 and decode to get BF.
5. Build the 18869-byte identify program exactly as now (`build_ours.py` logic) from the factory values, CF and BF.

The fixed chunks still come from `proto/data/cmd02_template.bin`. Generating them from the DLL tables is a separate B2 item.

Program #1 and #2 differ from the identify program only as derived from the DLL:
- 0x26 is skipped for capture type 0 (0x18008c720);
- 0x2e and 0x47 need flags 0x800/0x10000/0x80000, and calibration passes 0x430;
- 0x48 needs capture type 4;
- the 0x33 line-update builder ignores the capture type.

PR #288's CALIBRATE request has the same shape: its test asserts that there is no 0x26/0x2e/0x47/0x48.

## Evidence

1. **Factory values = `6f` subtag 3.**
   - Code: stage 3 looks up key 0x80000003 and copies it into C+0x68.
   - Hardware: the 2026-10-13 probe matched 56/56 against CalibrationData 0xEC–0x123.
2. **CF = averaged, uncalibrated, undecoded blank capture.**
   - Code: stage 6 frees CF, runs one mode-1 capture and copies the descriptor.
   - Recomputing Windows' acceptance score from our stored CF and the live subtag 12 gives **7**, exactly the stored C+0xbc. Shifting the rows by one gives 223 (fail).
   - The correlation between factory subtag 12 and q(CF) on those 5 rows is 0.994.
3. **BF = decoded average of a calibrated blank capture.**
   - Mode 0 also stores the undecoded row 28 as the descriptor's extra 57th line, before the decode.
   - Decoding our stored extra line reproduces BF row 28 **144/144**.
4. **All three units: the 0x30 and 0x48 of each unit fit together.**
   - Re-encoding each BF with `Mᵀ/4` leaves a mean residual of only 1.2–1.4 on most pixels.
   - The residuals above 8 sit almost all on pixels whose 0x30 value is clamped: iusearch 22/24, ToLiKNoSoV 42/42, ours 19/20.
   - BF line-header byte 2 = 2r+1 in all three, as averaging line pairs predicts.
5. **Offline is not enough.** Rows 10 apart correlate at 0.04. Interpolating the 56 rows from the 5 factory rows misses by 34 q-units on average, against a signal sd of 48.
6. Windows' identify session reads nothing beyond RomInfo/19/75 (earlier ETW), so the frames come from the cache, not from the sensor.

## Confidence

| Item | Confidence | Basis |
|---|---|---|
| Source of each per-unit byte (table above) | high | code path traced; items 1–4 verified |
| Averaging and decode algorithm | very high | exact reproductions |
| Calibration program bytes (#1/#2) | medium | derived from builder conditions; matches PR #288's shape; never seen on the wire |
| Module choice | medium-high | DLL matcher plus stored module id |

## Proposed experiment (run afterwards: `b2-run.md`)

Conditions:
- no finger on the sensor;
- fprintd stopped;
- run as a proto script with `--dry-run` first;
- dumps go to `local/` (per unit; gitignored).

Steps:
1. **Read-only:** `01`, `19`, `75`; `6f 00 0e 00 00 00 00 00 00`.
   - Expect: ROM 6.20, type 0x2449, an 890 B reply with subtags 3 and 12.
2. **Variant L (preferred, nothing new loaded):**
   - If byte 17 = 0, upload module 39 as the driver does (`06` → `00 00`, byte 17 → 0x0e).
   - Send program #1 → expect status `00 00` (the reply length is unknown; identify answers 2154 B).
   - Read EP 0x82 until 51 224 B; time out at 5 s.
   - Then `04`, `52` (benign: 0 or 0x0412).
   - Compute CF and its score: pass ≤ 200; Windows got 7.
   - Then program #2 → read 51 224 B → BF.
   - Expected: line headers `01 fe .. 02 f? f7 01 00`; BF mean ≈ 127.6–127.9, sd ≈ 2.
3. **Variant W (only if L fails), from a fresh state** (after a reboot, byte 17 = 0):
   - `06` + module 40 (4676 B) → `00 00`, then `01` → byte 17 = 0x0f.
   - Then the two captures, then a USB reset (or reboot) to clear the module before normal use.
4. **Compare with Windows:**
   - CF against the stored CF: per-pixel correlation of q, mean |Δ|;
   - BF statistics;
   - then build the program and A/B it against today's program: finger-off flatness and SIGFM scores.

Risk:
- low; everything is RAM-only and in the command family we already use;
- a bad program can at worst give an error status or stall the device; a USB reset or replug clears it;
- never sent: `1a` (the DLL sends it before swapping modules; python-validity calls it `call_cleanups`, which on flash sensors commits pending writes), `4f`, `50`, any flash or db command, and a `0x48` chunk in a calibration program.

## Side findings

- **PR #288** stores its 0x48 blank frame without the Hadamard decode. Windows decodes it (item 3).
- **PR #288** reads `1a` as "firmware calibrated". In the DLL, `1a` is sent only to clear a resident module before another `06`.
- **Driver after variant W:** it uploads `06` only when byte 17 = 0, so with module 40 resident (0x0f) it would never load the capture module. Do a USB reset after calibrating. Long term, the driver could check for 0x0e instead of non-zero.
- **Module 40 in the public cab:** not checked in this step. The setup tool should scan for it by sha256, as B1 does for module 39.

## Next step

A proto script, `proto/calibrate.py`, runs the experiment above:
- dry-run by default;
- variant L only;
- it saves the raw EP 0x82 dumps;
- it prints the check score and the comparison with the Windows frames.

It is run once on the device. If it passes, B2 continues with a `calibrate` subcommand in `synaptics0078-setup` and the DLL-table generator for the fixed chunks.
