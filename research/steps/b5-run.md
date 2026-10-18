# Step B5: the Windows-free setup on the hardware (2026-10-17)

- **Package:** libfprint-sigfm-0078 1.94.100.r23.g40cda34-2, built with makepkg.
  - `check()`: 317 python tests passed. The meson egis_etu905 flake failed once and passed on its rerun.
  - The package audit (`SYNAPTICS0078_PKG`) passed: 16 tests.
- **Install:** `pacman -U`, which pulled in python-pyusb from extra. The `.install` message names `synaptics0078-setup install`, then `fprintd-enroll`.
- **Prints:** the existing enrolled prints, made under the Windows calibration. Nobody re-enrolled.

## Acceptance (design: b5-setup-design.md, "Acceptance")

| # | Check | How | Result |
|---|---|---|---|
| 1 | Install from a local vendor file | Data dir moved aside, then `install --from-file` with the 2019 cab | PASS |
| 2 | Network install with an empty cache | Cache and data dir removed, then `install` twice | PASS |
| 3 | Offline `install` and `recalibrate` from the cache | Not run on hardware: skipped by choice (2026-10-17) | Tests only |
| 4 | Source fallback order; `fetch` with no reader | Offline tests (fake curl) | Tests only |
| 5 | `status` | On hardware: module, program (serial matched), cache, last calibration, ready | PASS |
| 6 | Content audit | Package audit 16 passed; no-serial guard passed | PASS |
| 7 | Phase A path (`--from DIR`) | Tests only | Tests only |

Details:
- **Row 1.**
  - `module.bin` is valid: 10500 bytes, pinned sha256. It is the same module as before.
  - The program is valid: 18869 bytes, matches the template pin.
  - Check score 10 (limit 200); baseline mean 127.3, sd 1.8.
  - `tools/dev/a5-verify.sh`:
    - right index 10/10, 0 retries, lowest decisive score 211;
    - left index 10/10, 0 retries, lowest 191.
  - That matches B2b, where the lowest scores were 161 and 378.
- **Row 2.**
  - The first `install` fetched source 1 (Microsoft Update Catalog 2019), checked it, saved it to the cache and calibrated.
  - The second said "Already set up" and fetched nothing.
  - `fprintd-verify -f right-index-finger` then gave verify-match.

## What this settles
An 06cb:0078 owner without Windows sets the reader up with one command, `sudo synaptics0078-setup install`, and prints enrolled under Windows keep matching. This was the goal of phase B.

## Left
- Phase B: the A7 replay test and B4 test data, which must not be biometric.
