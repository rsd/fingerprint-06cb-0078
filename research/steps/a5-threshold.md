# Step A5, part 1: SIGFM threshold, enroll stages and upscale (2026-10-14)

Offline. No USB access. The captures and everything derived from them (score matrices, keypoints, smoke fixtures) are kept outside git. This file holds aggregate numbers only.

**Decision** (in `libfprint/drivers/synaptics0078.c`, fork branch `sigfm-0078`):

| define | value | rule, in one line |
|---|---|---|
| `SYNAPTICS0078_UPSCALE` | 2 (unchanged) | 2x beats 1x everywhere: impostor max 0 vs 6, lower FRR at every k, no gate failures at either scale |
| `SYNAPTICS0078_SCORE_THRESHOLD` | **105** | the score of 6 fully consistent matches: the first whole match count at least 2 matches above the highest impostor score seen at either scale (6, about 3.4 matches) |
| `SYNAPTICS0078_ENROLL_STAGES` | **18** (brief D6 confirmed) | FRR is still falling fast at k = 9; 18 costs about 1.5 ms more per rejected verify than 15 (comparison share only; whole reject 14-44 ms median) and ~2.6 MiB per print |

Until now the driver set neither value, so the core defaults applied: 5 stages (`IMG_ENROLL_STAGES`) and threshold 40 (`BOZORTH3_DEFAULT_THRESHOLD`).

## Method

- **Data.**
  - `proto/captures/{left-index,right-index,right-middle}`: 10 accepted images each, 144x56, loaded with `research/matcher/fpdata.py`. That gives 270 genuine and 600 impostor ordered pairs.
  - `local/captures/a4-*.pgm`: 9 images from the A4 driver runs. The finger was not recorded, so they serve only as unlabeled cross-session probes and are not counted in FAR or FRR.
- **Harness** `research/a5/sigfm_matrix.cpp`, built by `research/a5/build_harness.sh`. It links the fork's own `build/libfprint/sigfm/libsigfm.a` and the same OpenCV 5.0.0 modules as `libfprint-2.so`. It mirrors the driver path exactly:
  - a nearest-neighbour upscale with the loop of `build_image()`;
  - `sigfm_extract()`;
  - the core's keypoint gate (`fp-image.c` `fp_image_sigfm_extract_thread_func`: NULL or `< 25` keypoints is rejected);
  - `score[p][g] = sigfm_match_score(info[g], info[p])`, the template stage as the query, as in `fpi_print_sigfm_match()`.

  It also times each extraction and comparison, records the `sigfm_serialize_binary()` size of each image (one stored stage), and with `--roundtrip` re-scores every pair against the serialize→deserialize copy of the template, which is how fprintd loads a print from disk.
- **Analysis** `research/a5/a5_threshold.py`, which reuses `fpscore`/`fpdata`:
  - pair distributions;
  - an enrollment simulation for k = 1..9, every k-subset of every finger (`fpscore.enroll_sim`, OR over the enrolled images);
  - a cautious extrapolation toward 18 stages;
  - the impostor side under the OR rule;
  - latency and print-size estimates;
  - the A4 probes.

  The harness runs twice per scale: once with all cores and once with `OPENCV_FOR_THREADS_NUM=1`. Both runs gave identical scores.

## Cross-check: harness vs libfprint's own path

The script exports a `tests/benchmark-matching.py --smoke` fixture: a manifest plus upscaled PGMs, with the expected per-stage scores taken from the harness. The fork's benchmark tool then enrolls and verifies every trial through the real `virtual_image` device (`build-gi`, `FP_VIRTUAL_IMAGE_ALGORITHM=sigfm`, `FP_PRINT_SCORE_LOG`). It checks:

- the pixel hashes;
- every logged per-stage score;
- the accept/reject decision at the threshold.

Limit: this cross-check never tests near the threshold. At 2x every impostor trial scores 0, and the genuine probes picked are the best and worst of the set. Score equivalence itself is proven by the identical per-stage scores; the decision column only confirms the plumbing.

| fixture | trials (genuine / impostor) | gallery | payload hashes | per-stage scores identical | non-zero among them | decisions @105 agree |
|---|---|---|---|---|---|---|
| 1x | 12 (6 / 6) | 3 | 48 | 36 / 36 | 4 | 12 / 12 |
| 2x | 12 (6 / 6) | 3 | 48 | 36 / 36 | 5 | 12 / 12 |
| 2x, 7 stages | 12 (6 / 6) | 7 | 96 | 84 / 84 | 6 | 12 / 12 |

All three runs report `equivalence: OK`, and the same held at 64 during the analysis. The serialize/deserialize round trip changed 0 of 1521 comparisons per scale. So the harness numbers below are libfprint's numbers.

## Results

Keypoints and gate:

| scale | keypoints min / median / max (30) | A4 probes min | rejected (< 25), all 39 images |
|---|---|---|---|
| 1x | 109 / 235 / 410 | 130 | 0 |
| 2x | 124 / 277 / 558 | 133 | 0 |

Single ordered pairs:

| scale | genuine > 0 (of 270) | lowest non-zero genuine scores | genuine max | impostor > 0 (of 600) | impostor max |
|---|---|---|---|---|---|
| 1x | 56 | 1, 2, 10, 27, 30, 34, 43, 60 | 2.0e6 | 2 (scores 1 and 6) | **6** |
| 2x | 59 | 1, 10, 17, 58, 64, 129, 150, 159 | 4.8e6 | 0 | **0** |

These are the same impostor maxima as recon-sigfm §4.3's Python port (6 at 1x, 0 at 2x NN). Under the OR rule, each finger enrolled with all 10 images against the other 20 images still gives at most 6 (1x) and 0 (2x).

SIGFM's score scale: m correspondences that all agree on one rigid motion score C(C(m,2),2).

| m | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|
| score | 15 | 45 | 105 | 210 | 378 |

A score of 6 is therefore worth about 3.4 matches.

Enrollment simulation, FRR (k images enrolled; probes are that finger's other images). FAR is 0 in every cell at 2x, and 0 at 1x for every threshold ≥ 7:

| k (splits) | 2x @7 | 2x @24 | 2x @64 | **2x @105** | 2x @138 | 2x @210 | 1x @7 | 1x @105 |
|---|---|---|---|---|---|---|---|---|
| 1 (30) | 78.5% | 79.3% | 79.6% | 80.0% | 80.4% | 81.9% | 80.0% | 84.1% |
| 3 (360) | 46.0% | 47.2% | 48.1% | 48.9% | 49.5% | 52.4% | 49.2% | 57.4% |
| 5 (756) | 24.9% | 25.9% | 26.9% | **27.8%** | 28.2% | 31.2% | 28.7% | 37.2% |
| 6 (630) | 17.7% | 18.6% | 19.4% | 20.3% | 20.5% | 23.5% | 21.7% | 29.2% |
| 7 (360) | 12.5% | 13.1% | 13.8% | **14.4%** | 14.5% | 17.4% | 16.4% | 22.7% |
| 8 (135) | 8.9% | 9.3% | 9.6% | 10.0% | 10.0% | 13.0% | 12.6% | 17.4% |
| 9 (30) | 6.7% | 6.7% | 6.7% | **6.7%** | 6.7% | 10.0% | 10.0% | 13.3% |

Every k has at least one split with 100% FRR, as found in matcher-evaluation §4.3: an enrollment that covers one region of the finger, probed from another.

Extrapolation toward more stages, with-replacement model (script docstring):
- Each probe p matches a fraction f_p of its finger's 9 other images.
- k independent touches miss it with probability (1 − f_p)^k.
- The model is pessimistic at k ≤ 9: it gives 21.8% at k = 9, where the exact simulation gives 6.7%.
- The floor is the probes that match no other image of their finger.

| 2x, threshold | floor | k = 9 | k = 12 | k = 15 | k = 18 | k = 24 |
|---|---|---|---|---|---|---|
| 64 | 6.7% | 21.0% | 15.5% | 12.3% | 10.4% | 8.4% |
| **105** | 6.7% | 21.8% | 16.1% | 12.8% | **10.7%** | 8.5% |
| 210 | 10.0% | 25.1% | 19.2% | 15.8% | 13.8% | 11.7% |

Reading: at 105 and 2x, FRR is 6.7% at k = 9. Those are 2 of 30 probes, both from regions that no other capture of the finger covers. More stages can only lower FRR. The model's 10.7% at 18 is an estimate under this sample's conditions (all captures from one sitting). The real value depends on how much of the finger the user's 18 touches cover.

Cross-session A4 probes (finger not recorded), best score against each finger's whole gallery at 2x:
- 7 of 9 clear 105 against left-index only, with best scores 7.2e3 to 7.8e5.
- 2 score 0 against all three fingers: an unenrolled finger, or an unenrolled region.
- **None clears the threshold for two fingers.**
- The 7 left-index-like probes score 0 against all 20 right-index and right-middle images. 1x shows the same pattern.

Latency, harness, 2x. Three runs on a shared, loaded machine; the ranges cover all three, with the all-cores run and the one-thread run in each:

| item | value |
|---|---|
| extraction, median / max | 6.4-10 ms / 10-30 ms |
| comparison scoring 0 (impostor or other region), median / p95 | 0.4-2.0 ms / 1.0-6.4 ms |
| comparison scoring > 0, median / max | 0.7-3.2 ms / 20-93 ms |
| comparison of an image with itself (near-duplicate worst case) | 230-445 ms |
| verify reject, 18 stages (extract + 18 misses), median / p95 | 14-44 ms / 28-128 ms |
| verify accept, worst case (max extract + 17 max misses + max hit) | 62-393 ms |

Print size: `sigfm_serialize_binary()` per stage is median 146 KiB and max 294 KiB at 2x (1x: 124 / 216 KiB). An 18-stage print is therefore about **2.6 MiB** (up to 5.2 MiB), plus a few hundred bytes of GVariant framing (not measured). This replaces the brief's "~2 MB per 15 stages" estimate.

## Decisions and rules

**Upscale 2x (unchanged).** The rule: lower FRR at zero observed FA, a lower impostor tail, no gate failures, and acceptable time.
- FRR: 2x is lower at every k and every threshold (k = 9 @105: 6.7% vs 13.3%; k = 5: 27.8% vs 37.2%).
- Impostor max: 0 at 2x vs 6 at 1x.
- Gate failures: none at either scale.
- Cost of 2x: extraction takes a few ms more and each stage is about 18% larger. Both are negligible.

**Threshold 105.** The rule has two parts.
1. Zero observed false accepts at every scale. This is mandatory, and needs T > 6.
2. A margin in match-equivalents rather than a raw multiple, because the score grows with about the 4th power of the matches. T is the score of the smallest whole number of fully consistent matches that is at least 2 matches above the highest impostor score seen at either scale: 3.37 + 2 = 5.37, rounded up to 6 matches, so T = 105.

In raw terms that is 17.5x the highest impostor score. The 1x impostor of 6 counts because it is the same matcher on the same pixels; it shows that 3-4 coincidentally consistent correspondences occur between different fingers.

Its FRR cost at 2x against 7, the lowest threshold with no false accept at either scale:
- k = 5: +2.9 points;
- k = 7: +1.9 points;
- k = 9: +0;
- extrapolated k = 18: +1.1 points.

The next whole step, 7 matches (210), would cost +3.3 points at k = 9, so the rule stops at 6. Upstream values are lower (Elan 24, FPC1022 10); they were set on the old comparator or on other sensors (recon-sigfm §4.3).

**Stages 18 (brief D6 confirmed).**
- FRR halves roughly every 2 stages between k = 5 and k = 9 (27.8 → 14.4 → 6.7%), and the extrapolation keeps falling to 18 and beyond. Coverage, not the matcher, limits this sensor (matcher-evaluation §4.4).
- Costs of 18 against 15:
  - +3 touches at enrollment, each a touch plus the 1-1.6 s finger-off detection seen in A4;
  - about 1.5 ms more per rejected verify;
  - +0.4 MiB per print;
  - **20% more chances per verify for an impostor**, since every stage is one more comparison under the OR rule.
- At the observed impostor scores (none above 0 at 2x, the threshold 17x above the 1x maximum), the last cost is the one that matters. It is the reason for the margin, and for B3.

## Caveats

- **This is three fingers of one person.** The impostor tail is a lower bound. The 600 ordered impostor pairs are not independent: 404 of the 435 unordered pairs score identically in both directions, so there are about 300 effective pairs, and they come from only 3 finger-pair identities. With 0 false accepts in about 300 pairs, the rule of three gives a 95% upper bound of about 1.0% per pair (3/300). Over an 18-stage verify that is 1 − (1 − 0.01)^18 = 16.6%, and the exposure multiplies further with the number of enrolled fingers (fprintd and PAM verify across fingers) and with PAM retries. The sample cannot certify the false-accept rate; the margin (threshold 105 against an impostor maximum of 6 at 1x and 0 at 2x) is the only protection beyond what was observed. **Phase B3 (false-accept benchmark beyond our 30 images) is still owed before relying on this for sudo and polkit.**
- The genuine side has only 30 probes at k = 9, so each probe moves FRR by 3.3 points.
- The 10 touches per finger came in one sitting, at about one touch per second. The A4 probes from another session matched strongly (best ≥ 7.2e3), but their finger is unknown.
- Timings come from a loaded multi-core desktop and varied by about 3x between runs. fprintd's extraction runs in a GTask thread with OpenCV's default threading.
- The A5 hardware acceptance run (20 genuine and 20 impostor fprintd verifies with `FP_PRINT_SCORE_LOG`) will give the first on-device scores. Raise the threshold if any impostor stage comes within 2 matches of 105.

## Rerunning

From the project root, with `SCRATCH` outside git (the script refuses a tracked output dir). System `python3` is needed for numpy; `proto/.venv` has none, so the pytest module skips there.

```sh
SCRATCH=/path/outside/git; FORK=~/Devel/Linux/Synaptics/libfprint
meson compile -C $FORK/build                         # provides libsigfm.a
research/a5/build_harness.sh $FORK/build $SCRATCH/bin/sigfm_matrix
mkdir -p $SCRATCH/out
python3 research/a5/a5_threshold.py proto/captures --a4 local/captures/a4-*.pgm \
    --harness $SCRATCH/bin/sigfm_matrix --out $SCRATCH/out --export-smoke $SCRATCH/smoke \
    > $SCRATCH/out/report.md                         # ~2 min; add --smoke-stages 7 for bigger galleries
(cd $FORK && for s in 1x 2x; do
    python3 tests/benchmark-matching.py --smoke $SCRATCH/smoke/$s --builddir build-gi --threshold 105; done)
python3 -m pytest -q research/a5                     # analysis logic, synthetic data
```

| file | role |
|---|---|
| `research/a5/sigfm_matrix.cpp` | harness: driver-identical upscale, extract, gate, ordered score matrix, timings, serialized size, round trip |
| `research/a5/build_harness.sh` | builds it against a fork build dir's `libsigfm.a` and OpenCV |
| `research/a5/a5_threshold.py` | the analysis and report, smoke-fixture export, the margin rule (`margin_threshold`) |
| `research/a5/test_a5_threshold.py` | pytest of the analysis logic |
