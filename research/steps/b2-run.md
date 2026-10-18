# Step B2: on-device calibration experiment (2026-10-17 00:54)

Tool: `proto/calibrate.py --run`, run once with no finger on the reader. Output went to local/calib/20261017-005409/ (gitignored). The dry run matched the plan: 12 steps, both skeleton sha256s pinned, and no forbidden command listed.

## Result: PASS

| Check | Linux run | Gate | Windows reference |
|---|---|---|---|
| Check score (CF against the subtag-12 factory block) | **10** | ≤ 200 | 7 |
| Baseline frame (BF) mean, sd | 127.3, 1.5 | 128 ± 4, sd ≤ 6 | — |
| CF vs Windows CF | mean \|Δ\| 4.98, max 25, corr 0.995, 98.8% within ±16 | corr ≥ 0.95, mean ≤ 8, ≥ 95% | — |
| BF vs Windows BF | mean \|Δ\| 1.79, max 10, 99.9% within ±8 (corr 0.184, not gated) | mean ≤ 3, ≥ 95% | — |
| Factory values (6f subtag 3) | 56/56 identical | 56/56 | — |

- Both calibration programs, derived from the DLL's builders and never seen on the wire before, were accepted by the sensor. They returned the expected EP 0x82 frames.
- The calibration frame is the per-unit part with real structure. It reproduces Windows' frame at correlation 0.995, and its check score of 10 is close to Windows' 7.
- The baseline frame is close to flat noise around 128, so its low correlation is expected. The absolute agreement, 1.79 mean |Δ|, is what matters there.
- Nothing about the sensor changed: no flash command was sent, and fingerprint use is unaffected.

## What this settles

Every per-unit input of the 18869-byte `02` program can be measured on Linux:
- the factory values via `6f`;
- the CF via calibration program 1;
- the BF via calibration program 2.

Together with B1 (the module comes from a public cab), a 0078 owner needs no Windows, provided the Linux-made program captures images as good as the Windows one. That is the next step.

## Next: B2b, prove the Linux-made program with real fingers
1. Assemble the full 18869-byte program from this run, offline, and diff each chunk against the Windows-derived one.
2. Run a5-verify on today's Windows program as a same-day baseline. Then swap in the Linux program (reversible) and verify again against the existing enrolled prints. Compare the match rate and the best scores.
