# FAR diagnosis: the 2026-10-15 fprintd enroll and verify (offline)

Offline. No USB, no fprintd, no driver change. The dumps, the stored print, the journal and everything derived from them stay in `local/far-diag/` (gitignored). This file holds aggregate numbers only.

## Verdict

**The matcher made no false accept.** All 10 "false accepts" (right-index 8/10, right-middle 2/10) are captures of the enrolled finger itself. The finger on the sensor was not the one named:

- **The enrolled finger is the left index.** The stored print `right-index-finger` matches 6/10 left-index proto captures (16/18 stages match some left-index proto capture). It scores **0** against all 10 right-index and all 10 right-middle proto captures.
- In the "right-index" verify run, **rounds 1-2 were the right index**: they were rejected, and they match right-index proto captures. **Rounds 3-10 were the left index.**
- In the "right-middle" run, **rounds 8-9 were the left index**. The other 8 rounds were one finger that matches nothing enrolled.
- The 10 "left-index" rounds were the left index (10/10 accepted, as genuine).

The data only show which finger touched. The follow-up in `a5-ground-truth.md` resolves it: the print named `right-index-finger` had been enrolled with the left index (the enrollment was mislabelled). Rounds 3-10 and 8-9 were therefore the enrolled finger matching itself, not false accepts. After re-enrolling each finger under its right name, 10/10 genuine verifies matched and 10/10 right-middle verifies did not.

**No driver fix is indicated.** The threshold (105) is on the right scale, the OR aggregation matches what A5 simulated, and no enrolled image is defective. The fix is procedural: re-enroll the intended finger, and give the acceptance run a ground truth (see Recommendation).

## 1. Dump map

`research/far-diag/map_dumps.py` reads the journal (JSON, `journalctl -u fprintd -o json`). It uses the driver's log contract (a5b.md: "Image captured", "Capture failed", "Same touch ... not submitted", "taking this image as the capture") and the a5 helper logs.

| session | dumps | handed to the core | finger-off probes | not submitted (retry) |
|---|---|---|---|---|
| enroll (12:00:47-12:02:05) | 88 | 19 = 1 duplicate-check identify + 18 stages | 69 | 0 |
| verify right-index (13:12) | 28 | 10 | 16 | 2 |
| verify right-middle (13:13) | 25 | 10 | 14 | 1 |
| verify left-index (13:13-13:14) | 26 | 10 | 14 | 2 |
| unlabeled (13:37, not in any helper log) | 4 | 2 | 2 | 0 |

- Totals: 171 dumps, 171 "Diag image" journal lines, and no file on either side alone.
- The score lines the journal attributes to each verify capture equal the helper log's `[score]` line for that round, in all 30 rounds.
- The 13:37 pair is two extra verifies. The first was rejected (it matches right-index proto). The second was accepted at 85,655 (it matches left-index proto).

## 2. The stored print and its provenance

- `local/fprintd-prints/synaptics0078/0/7`: driver synaptics0078, finger 7 (right index), type SIGFM, 18 stages.
- **Every stage is byte-identical**, as serialized SIGFM, to the extraction of one enroll capture dump, in capture order. Stage k is enroll capture k+1.
- The duplicate-check identify image is not in the print. No finger-off probe image is in the print.
- Stage image stats: coverage 75-100%, sd 41.0-54.9, gradient 18.0-29.8, keypoints 255-487. Diff to the previous capture is 44.8-62.4: every stage is a new touch, none a held finger.
- Stages linked to other stages (≥ 105): 12, 14, 12, 14, 13, 11, 8, 11, 14, **0**, **1**, 9, 7, 9, 8, 9, 11, 9.
  - Stages 9 and 10 are isolated regions. Stage 9 has the lowest sd and gradient (41.0 / 18.0); stage 10 has the fewest keypoints (255).
  - Neither ever scores above 0 against any verify capture. They add nothing, and they harm nothing.
- No residue, blank or near-blank image (coverage ≥ 75%). Stage-to-stage matches all carry real motion (section 5).

## 3. Offline replay through fprintd's path

`research/far-diag/far_replay.c` links the fork's `build/libfprint-private.a` and `libsigfm.a`. That is the build the installed lib came from (sha256 33e877e8…, identical to `/usr/local/lib/libfprint-0078-dev`). The replay runs these steps:
1. `fp_print_deserialize()` of the stored file;
2. the PGM upscaled with `build_image()`'s loop;
3. `fp_image_new()` and `fp_image_extract_sigfm_info()` (the core's GTask and its 25-keypoint gate);
4. `fpi_print_add_from_image()`;
5. `fpi_print_sigfm_match(template, print, 105)`, whose own per-stage scores are captured through `FP_PRINT_SCORE_LOG`;
6. as an addition, `sigfm_match_score()` for all 18 stages.

| check | result |
|---|---|
| verify captures replayed (30 rounds + 2 unlabeled) | 32 |
| per-stage score sequence identical to the journal (including the early exit) | **32/32** |
| decision identical | **32/32** |
| A5 matrix harness vs the replay, per (capture, stage) | 576/576 identical |

## 4. Score semantics and aggregation

- **Same quantity, same scale.**
  - The live "sigfm score X/105" is `sigfm_match_score(stage_i, probe)`. The A5 harness computes the same thing for `score[p][g]`.
  - The match is an OR across stages: `fpi_print_sigfm_match` returns at the first stage ≥ threshold. A5's enrollment simulation used the same OR.
- **Why the logs showed only stage 0.** Every one of the 21 accepts fired at stage 0. The journal and the helper's `[score]` line therefore show only stage 0's score, never the maximum. With all stages scored, each accepted capture clears 105 on 6-14 of 18 stages (median 13).
- **Why the scores are in the millions.**
  - The score counts pairs of angle-consistent match pairs. It grows with about the 4th power of the consistent matches and is capped at C(C(176,2),2) = 1.19e8 by the 176-candidate cap. Self-scores reach 8.9e7.
  - Proto genuine pairs (one sitting, 3 fingers): 59/270 non-zero, median 1.3e4, max 4.8e6.
  - Live left-index genuine pairs (one day): 1,537/2,912 non-zero, median 3.4e5, max 1.0e8.
  - The driver's images carry more keypoints than the proto tool's: median 430 vs 277 at 2x; gradient median 25.4 vs 20.7, sd equal at 46.5. More keypoints give more consistent matches, and the 4th-power growth turns those into much larger scores. A5's threshold rule is stated in matches (6 consistent matches = 105) and stays valid.

## 5. Identity of every capture

The identity rule: a capture keeps the claimed finger unless it scores ≥ 105 against a proto capture of another finger and against none of its claimed finger.
- The rule uses SIGFM but never the stored print.
- The proto captures are from another day (2026-10-14) and another capture tool.

| group | n | accepted live | proto hits ≥ 105, summed (RI / RM / LI) | identity |
|---|---|---|---|---|
| enroll duplicate-check capture | 1 | (identify against no prints) | 0 / 0 / 2 | left index |
| verify "right-index" rounds 1-2 | 2 | 0 | 7 / 0 / 0 | right index |
| verify "right-index" rounds 3-10 | 8 | 8 | 0 / 0 / 20 | **left index** |
| verify "right-middle" rounds 1-7, 10 | 8 | 0 | 0 / 0 / 0 | right middle (claimed; no proto evidence) |
| verify "right-middle" rounds 8-9 | 2 | 2 | 0 / 0 / 8 | **left index** |
| verify "left-index" rounds 1-10 | 10 | 10 | 0 / 0 / 28 | left index |
| unlabeled 13:37 | 2 | 1 | 2 / 0 / 0 and 0 / 0 / 3 | right index, left index |

The proto labels are not needed to see that the claims are inconsistent. In the table below, a cell gives the pairs ≥ 105 out of all pairs between the two groups, with the max score in parentheses:

| | stages | LI-run acc | RI-run rej | RI-run acc | RM-run rej | RM-run acc |
|---|---|---|---|---|---|---|
| stages | 86/153 (8.4e7) | 118/180 (1.8e7) | 0/36 (1) | 109/144 (5.4e7) | 0/144 (2) | 20/36 (1.1e7) |
| RI-run acc (claimed right index) | 109/144 | **80/80 (9.0e7)** | **0/16 (0)** | 28/28 (1.0e8) | 0/64 (7) | 13/16 (8.7e7) |
| RM-run rej | 0/144 (2) | 0/80 (1) | 0/16 (1) | 0/64 (7) | 16/28 (1.0e8) | 0/16 (0) |

- Every one of the 8 accepted "right-index" captures matches every one of the 10 "left-index" captures.
- The same 8 captures score 0 against the 2 rejected "right-index" captures of their own run.
- Same-finger pairs match at 15-27% (proto, within one finger). Rates of 80/80 and 0/16 mean one finger in the accepted groups and another in the rejected ones.

**Geometry (artefact check).** For every pair, a RANSAC similarity was fitted to SIGFM's own candidates (`sigfm_geometry.cpp`). An artefact or residue match would show no motion; these matches move like fingers:

| pairs scoring ≥ 105 | n | median translation | p10 | no motion (< 1 px, < 0.5°) | median rotation |
|---|---|---|---|---|---|
| proto, same finger | 28 | 28.6 px | 12.9 px | 0 | 1.9° |
| stage vs stage | 86 | 34.2 px | 10.9 px | 0 | 1.2° |
| stage vs proto left index (cross-day) | 40 | 25.3 px | 8.3 px | 0 | 9.3° |
| accepted verify vs stage | 261 | 41.8 px | 15.2 px | 0 | 3.9° |
| accepted verify vs proto left index | 59 | 45.1 px | 26.2 px | 0 | 4.3° |

**Image path.**
- No difference was found. The stages reproduce byte-for-byte from the dumps through the replay's upscale and extraction.
- Proto and driver images are both 144x56, row-major and sensor-normalized (mean 128.5).
- Proto left-index captures from the day before match the stages with ordinary motion, so orientation and normalization are compatible. A flipped or inverted path would match nothing.

**Which enrolled images drive the impostor matches:** none. Against the print, the highest stage score of any non-left-index capture (31 impostor probes x 18 stages) is **0**. The highest over all cross-identity image pairs is 7.

## 6. Distributions, FAR and FRR on all data

Ordered pairs among all labeled images (proto 30, stages 18, live captures 33):
- genuine: 2,912 pairs, 1,537 > 0;
- impostor: 3,568 pairs, 22 > 0, **max 7** (values 1, 2, 3, 6, 7), 0 ≥ 105.
- The same pairs under the claimed labels give 528 impostor pairs ≥ 105 (max 9.0e7). That is the reported "false accept".

Verify against the stored 18-stage print (OR, as `fpi_print_sigfm_match`):
- 32 genuine probes: 10 proto left-index from the day before, and 22 live (20 verify rounds, the 13:37 accept and the duplicate-check capture);
- 31 impostor probes: proto right-index 10, proto right-middle 10, live 11.

| threshold | FRR, all | FRR, live (same day) | FRR, proto (cross-day) | FAR | FAR 95% upper (Wilson) |
|---|---|---|---|---|---|
| 7 | 3/32 | 0/22 | 3/10 | 0/31 | 11.0% |
| **105 (current)** | **4/32** | **0/22** | **4/10** | **0/31** | **11.0%** |
| 1,000 | 6/32 | 0/22 | 6/10 | 0/31 | 11.0% |
| 10,000 | 9/32 | 0/22 | 9/10 | 0/31 | 11.0% |
| 100,000 | 10/32 | 0/22 | 10/10 | 0/31 | 11.0% |

- The lowest genuine best-stage scores are 0, 0, 0, 85, 258 and 824, all proto.
  - The three 0s are proto captures of regions that the 18 touches did not cover.
  - The 85 is the one genuine probe between 7 and 105.
- Raising the threshold buys no FAR (the impostor maximum is 0 against the print and 7 pairwise) and costs cross-day FRR.
- Lowering it to 7 would sit at the pairwise impostor maximum.
- So 105 stays.

A4 captures (finger never recorded, 27 from 2026-10-14/15): the print accepts 24. All 24 match left-index proto and none matches another finger. The remaining 3 score 0, 0 and 1-104. Most of the tester's untargeted touches on this sensor have been the left index.

## Recommendation

1. **No driver change.** Keep `SYNAPTICS0078_SCORE_THRESHOLD` 105, the OR aggregation and the 2x upscale. No enrollment quality gate is warranted by false-accept evidence. Stages 9 and 10 are isolated, not harmful, and every stage passes coverage ≥ 75% and keypoints ≥ 255.
2. **Re-enroll under the right name.** Delete print 7 and enroll again, saying aloud or noting which hand is used. Or keep the print and rename it left-index-finger.
3. **Give the acceptance run a ground truth.** The A5 verify table cannot tell a false accept from a wrong finger. Use either:
   - (a) enroll all three fingers as three prints, so that a cross-finger accept must beat the finger's own print; or
   - (b) after each run, run this diagnosis: `map_dumps.py` then `far_replay` against the proto references. It names the finger of every capture without the print.

   Only an accept that (b) labels as another finger is a real false accept.
4. **Still owed:** cross-session FRR. It is 4/10 at 105 for captures one day older than the print, against 0/22 the same day. This, not FAR, is what the data say to watch. B3 (false accepts beyond one person's three fingers) is also still owed.

## Caveats

- One person, three to four finger identities. FAR 0/31 probes bounds the per-verify rate only at 11.0% (95%, Wilson). The 3,568 impostor pairs are far from independent.
- The identity rule uses SIGFM itself, though never the stored print. The label-independent table in section 5, the geometry, and the cross-day proto matches support it. `a5-ground-truth.md` confirms it: the enrollment was mislabelled (left index stored as `right-index-finger`), so rounds 3-10 and 8-9 were genuine matches.
- The "right-middle" rejected rounds match no proto right-middle capture (0/80). That finger's identity is unconfirmed; they are impostors to the left-index print either way (0/144 against the stages).
- Proto right-middle and right-index captures: 10 each, one sitting.

## Rerunning

From the project root (local data must be present; outputs go to `local/far-diag/`, mode 0700):

```sh
FORK=~/Devel/Linux/Synaptics/libfprint; O=local/far-diag
journalctl -u fprintd --since "2026-10-15 11:55" -o json \
  --output-fields=MESSAGE,__MONOTONIC_TIMESTAMP,__REALTIME_TIMESTAMP,_PID,_BOOT_ID > $O/journal.json
python3 research/far-diag/map_dumps.py --journal $O/journal.json --dumps local/diag-fprintd \
  --enroll-log local/logs/a5-enroll-20261015-120031.log \
  --verify-log local/logs/a5-verify-20261015-131225.log --verify-log local/logs/a5-verify-20261015-131311.log \
  --verify-log local/logs/a5-verify-20261015-131347.log --out $O/map.csv
research/far-diag/build_replay.sh $FORK/build $O/bin/far_replay
research/far-diag/build_geometry.sh $FORK/build $O/bin/sigfm_geometry
research/a5/build_harness.sh $FORK/build $O/bin/sigfm_matrix
# then, in bash, from $O: stages.tsv / verify_replay.tsv (far_replay stages|verify PRINT IMG...),
# matrix2x.json (sigfm_matrix --scale 2 over proto + handed-in dumps + A4), geometry.tsv (same list)
python3 research/far-diag/far_analysis.py $O > $O/analysis.md
python3 -m pytest -q research/far-diag
```

| file | role |
|---|---|
| `research/far-diag/map_dumps.py` | dump → session/role/stats/journal scores, cross-checked against the helper logs |
| `research/far-diag/far_replay.c`, `build_replay.sh` | fprintd's verify path offline: deserialize, upscale, core extraction, `fpi_print_sigfm_match`; stage provenance |
| `research/far-diag/sigfm_geometry.cpp`, `build_geometry.sh` | RANSAC motion behind SIGFM's candidates (artefact check) |
| `research/far-diag/far_analysis.py` | every number in this file |
| `research/far-diag/test_far_diag.py` | pytest on synthetic journals and logs |
