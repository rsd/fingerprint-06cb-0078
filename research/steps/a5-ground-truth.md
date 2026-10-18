# Step A5: ground-truth enroll and verify (2026-10-15)

Run on hardware with the finger-naming helpers against the installed fork 40cda345 (--diag on). Logs in `local/logs/` (gitignored: they hold the unit serial and scores).

## Enroll

| Finger | Log | Result |
|---|---|---|
| right-index-finger (`--replace`, removed the misnamed print holding the left index) | a5-enroll-20261015-154220 | 18/18, 0 retries, about 1 min |
| left-index-finger | a5-enroll-20261015-154326 | 18/18, 0 retries, about 1 min |

`fprintd-list` afterwards: #0 left-index-finger, #1 right-index-finger.

## Verify (10 rounds each)

| Finger used | Against | Expected | Result | Best score per round |
|---|---|---|---|---|
| right index | right-index-finger | match | 10/10 match | 33,281 to 14,092,179 |
| left index | left-index-finger | match | 10/10 match | 33,727 to 14,159,309 |
| right middle | right-index-finger | no match | 10/10 no match | 180 comparisons: all 0 except one 4 |

The threshold is 105. There were no retries and no errors in any run. The genuine minimum (about 33k) is more than 300 times the threshold. The impostor maximum (4) is about 26 times below it.

This settles the apparent false accepts of the earlier run: they came from the mislabelled enrollment, not from the matcher (see far-diag.md).

## Side observation: long armed wait

In the no-match run, round 1 waited 24 min 45 s for its first touch. The tester had stepped away.

- The sensor stayed armed the whole time (finger event at +1,484,870 ms after `02`) and captured normally.
- The device stayed at power/control=on and runtime_status=active.
- There were no kernel USB messages in that window.

So a long armed wait does not lose the sensor.

## Still open for A5

- Suspend/resume, both with the device idle and during an active verify (the lock-screen case).
- Cross-day recognition on a later day.
