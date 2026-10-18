# Step B2b: the Linux-built capture program with real fingers (2026-10-17)

## Setup
- **Program:** `proto/calib_program.py local/calib/20261017-005409` gave program-linux.bin (sha256 d065a968…84cfb).
  - Self-check: the same code rebuilds the Windows program byte-exact.
  - Differences from the Windows program are confined to the four per-unit regions:
    - key line 125/144 bytes, mean |Δ| 5.21;
    - 0x30 data 6729/8064 bytes, mean |Δ| 4.98;
    - factory values 0/56;
    - baseline pixels 6627/8064, mean |Δ| 1.79.
  - Nothing differs outside those regions.
- **Prints:** the existing enrolled prints, made with the Windows program. Right index enrolled 2026-10-16, left index 2026-10-15.
- **Order:**
  1. Windows program: right index (08:14), then left index (08:15).
  2. `sudo tools/dev/b2-program.sh use-linux …` (08:16:25). This backed up the Windows program, installed the Linux one and stopped fprintd.
  3. Linux program: right index (08:16), then left index (08:17).

## Result: 40/40 match; the Linux program is equivalent

Decisive SIGFM score per round, against threshold 105:

| Program | Finger | Result | Rounds < 1000 | Lowest | Median |
|---|---|---|---|---|---|
| Windows | right index | 10/10, 0 retries | 1 | 328 | 27 186 |
| Windows | left index | 10/10, 1 retry | 1 | 183 | 182 876 |
| Linux | right index | 10/10, 0 retries | 3 (161, 257, 1011) | 161 | 18 490 |
| Linux | left index | 10/10, 0 retries | 1 | 378 | 796 232 |

- Scores vary by three to four orders of magnitude from touch to touch under both programs. The differences between the programs (right median lower, left median 4x higher) are within that spread. Both programs give a few thin rounds, in the low hundreds.
- **Prints enrolled under the Windows calibration match probes captured under the Linux calibration.** An owner who switches does not need to re-enroll.
- Limits: one calibration run and one session, 20 rounds per program. Daily use with the Linux program is the longer test.

## Decision
- Keep the Linux program in daily use. It is what Windows-free owners will get. `sudo tools/dev/b2-program.sh use-windows` restores the Windows program byte-exact if rejections show up.
- Phase B status:
  - B1 is done: the module comes from a public Microsoft Update Catalog cab.
  - B2 is done: the per-unit calibration is measured on Linux and works with real fingers.
  - Left: make synaptics0078-setup Windows-free (download plus calibrate), the A7 replay test, and B4 test data, which must not be biometric.
