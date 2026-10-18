# Step A2: finger-lift experiment (2026-10-14)

Script: `proto/fingerlift.py`. Run on hardware twice:

- `proto/logs/fingerlift-20261014-174347-*`: baseline and 4-rearm-up complete; 2a, 2b and 3 timed out with no finger within 20 s (no touch in time, not a sensor fault).
- `proto/logs/fingerlift-20261014-174546-*`: all five scenarios complete.

Logs and images are gitignored (biometric).

## Findings

1. **The sensor sends nothing when the finger lifts.** Zero EP 0x83 events in every lift window:
   - still armed, before 04/52: 8 s;
   - after 04/52: 8 s;
   - re-armed, after the re-arm capture: 5 s;
   - not armed: 2 s.
2. **Arming with nothing touching gives only `0000000000`.** It arrives about 21 ms after the `02`; nothing else follows in 5 s. This holds in the baseline and in the re-arm after lifting.
3. **Re-arming with the finger still down fires at once.**
   - `0200401000` arrives 117 ms after the `02`, then `0340010000` and ready `0342040040` at 169 ms.
   - `51` then returns a normal image.
   - A fresh human touch after arming took 1.6 to 6.2 s.
4. The status reply to `04` after a delivered image is 0x0412, as before.

## Consequence for the driver (decision D5, resolved)

Detect finger-off by **re-arm probing**:

1. After a capture (in the AWAIT_FINGER_OFF state), send `02` and wait about 500 ms. The observed still-down event came at 117 ms, so this leaves margin.
2. **If a finger event arrives:** the finger is still down.
   - Read and discard the image (`51`), then `04`/`52`.
   - Wait about 250 ms and probe again.
3. **If only `0000000000` arrives in the window:** report the finger off.
   - The sensor is then already armed for the next touch, so AWAIT_FINGER_ON just keeps listening.

The timed-gap fallback is no longer needed. Tune both delays in A5.
