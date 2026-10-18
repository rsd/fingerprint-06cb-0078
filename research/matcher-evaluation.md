# Matcher evaluation for 06cb:0078: NBIS vs keypoint matching on 144x56 images

Offline evaluation, 2026-10-13/14. The USB device was not accessed. The scripts are in `research/matcher/`. The captures, the score matrices and everything else derived from the images stay outside git (local working files, not published). This report contains aggregate numbers only.

## 1. Answer

- **libfprint's NBIS pipeline cannot tell these fingers apart at 144x56.** Run through libfprint 1.94.100 itself (virtual_image driver, enrolled and verified as fprintd would), mindtct finds a median of 2 minutiae per image. bozorth3 scores 0 for all 222 genuine comparisons, and also when an image is compared with itself (0 of 25; the other 2 images have no minutiae at all and cannot be enrolled). Twelve variants were tried: driver-side 2x/3x upscaling (egis0570/elanspi style), `FPI_IMAGE_PARTIAL`, inverted polarity, CLAHE, background masking, Gabor ridge enhancement, and host-side bicubic 2x. The best of them, bicubic 2x, gets genuine scores of at most 9, with impostors at up to 3. At libfprint's threshold 40, and at egis0570's 25, the false-reject rate is 100% in every variant, with or without 5-image enrollment. A control run on sensor-sized crops of libfprint's own NIST example prints fails the same way, and NBIS starts working only at 4x the area. So the cause is the capture area (about 7.3 x 2.8 mm, roughly 20 mm²), not image quality or tuning.
- **Keypoint matchers do separate the fingers on this sample.** Tested: the PR #288 SIFT/RANSAC matcher, the SIGFM matcher proposed for libfprint, ORB and AKAZE. The highest impostor score is 3 inliers for PR #288 with a 0078-tuned mask, against a threshold of 6. SIGFM scored 0 on all 480 impostor comparisons. Genuine pairs either match clearly (9 to 117 inliers) or score about 0. With 5 enrolled images, the false-reject rate at zero observed false accepts is 30-34% across all keypoint variants, against a ceiling of 26% that even a matcher with no misses would hit on this set. The touches sample 2-4 separate regions of each finger, and a probe from a region with no enrolled touch cannot match. With 7 enrolled images it falls to 17-20% (ceiling 16%). Coverage at enrollment is what limits this sensor, which is why PR #288 enrolls 18 touches.
- **Recommendation: (c), python-validity + open-fprintd with the PR #288 matcher**, with the foreground-mask threshold changed from 40 to 25 for 0078's lower contrast and an enrollment of 15-18 touches. Option (a) as specified (NBIS) is ruled out by the data. An upstream libfprint driver becomes realistic only if the SIGFM merge request (!530) lands. SIGFM performs as well as PR #288 here, so that path stays open (section 7).

## 2. Data

| finger | accepted images | ridge coverage (manifest) |
|---|---|---|
| right-index | 10 | 0.39-0.97 |
| right-middle | 10 | 0.44-1.00 |
| left-index | 7 (capture stopped on status 0x0412) | 0.73-0.98 |

- 144x56 pixels, 8-bit grey, captured 2026-10-13 between 23:15 and 23:17 at about one touch per second (left-index 1-7 in 7 s).
- Three more left-index images (08-10) were added to the capture folder at 00:09 on 2026-10-14, while this evaluation was running. They are not included here; rerunning the scripts picks them up.
- The ridge period is 7-10 px, consistent with roughly 500 dpi, so the window is about 7.3 x 2.8 mm.
- Ridges appear dark, with light pore-like dots inside the dark bands. NBIS was also run with inverted polarity to rule this out.
- Uncovered sensor area has 8x8-block pixel std 4-10, and finger area 35-65.

## 3. What was run

### 3.1 NBIS exactly as libfprint runs it (`nbis_libfprint.py`)

- **Library.** The installed libfprint is 1.94.100 (pacman `1.94.100-1`, pkg-config `1.94.100`; the brief said 1.94.10). It includes the `virtual_image` driver. The script drives that driver through GObject introspection (`FPrint-2.0`): it opens the device, enrolls with `fp_device_enroll` and verifies with `fp_device_verify`. Images reach the driver through its `FP_VIRTUAL_IMAGE` Unix socket.
- **Pipeline.** Images go through the stock FpImageDevice path:
  - `fp_image_detect_minutiae`: mindtct with `g_lfsparms_V2`;
  - `fpi_print_add_from_image`: up to 200 minutiae to xyt;
  - `fpi_print_bz3_match`: bozorth3, default threshold 40.
  - An image with no minutiae is a retry, as in fprintd.
- **Scores.** libfprint's API exposes only match or no match. The score comes from libfprint's own debug line `score N/40`, which `fpi_print_bz3_match` logs for every comparison.
  - For each image g, the template is enrolled from 5 copies of g (image devices have 5 enroll stages). Each image p is then verified against every template, giving a full 27x27 matrix S[p, g]. bozorth3 is directional, so ordered pairs are used: 222 genuine, 480 impostor.
- **Multi-image enrollment.** `fpi_print_bz3_match` accepts a probe if any enrolled view reaches the threshold, so 5-image enrollment is the maximum over S entries.
  - This was cross-checked with real 5-image libfprint enrollments (4 random splits per finger per variant). All of the 56-264 checks per variant agree with the matrix, both in the match decision and in the exact sequence of logged scores.
- **Driver-side variants.** These need image flags and a resize inside the driver. They use a private libfprint v1.94.100 build (`build_libfprint.sh` and `libfprint-virtual-image-eval.patch`) in which `virtual_image` reads `FP_VIRTUAL_IMAGE_FLAGS=partial,inverted` and `FP_VIRTUAL_IMAGE_RESIZE=N`. It applies them the way egis0570 and elanspi treat their frames: `FPI_IMAGE_PARTIAL` makes mindtct drop perimeter minutiae, and `fpi_image_resize` is pixman bilinear.
  - The script checks `/proc/self/maps` to confirm which library each run actually loaded.
  - Host-side preprocessing (CLAHE, masking, Gabor, bicubic 2x) happens before the image is sent.
- **Precedent.** egis0570 (114x57 frames, assembled) resizes 2x, sets `PARTIAL | COLORS_INVERTED` and uses threshold 25 (`#define EGIS0570_BZ3_THRESHOLD 25 /* and even less What a joke */`). elanspi resizes 2x and uses 24.

### 3.2 Keypoint matchers (`sift_matcher.py`)

- **`pr288`**: a line-for-line port of python-validity PR #288 `validitysensor/hostmatch/matcher.py` (ashh87, commit 451cd11, `MATCHER_VERSION 1`).
  - 2x bicubic upscale, then CLAHE (clip 2, 4x8 tiles), then SIFT inside the eroded foreground mask (8x8-block std ≥ 40).
  - Ratio test 0.8, then RANSAC similarity (`estimateAffinePartial2D`, 6 px). Scale must be 0.8-1.25 and rotation at most 30°.
  - Score = inlier count, min over both directions. Accept at ≥ 6.
- **`pr288-mask25`** and **`pr288-nomask`**: the PR's mask threshold of 40 was measured on 06cb:00a8, whose finger blocks have std 45-98. On 0078 the finger blocks are at 35-65, so threshold 40 discards real ridge area. 25 sits between background (≤ 10) and finger. It was chosen from the image statistics, not from match results.
- **`sift-1x-mask25`**, **`orb-mask25`**, **`akaze-mask25`**: the same pipeline without the upscale, or with ORB or AKAZE instead of SIFT.
- **`sigfm-*`**: a port of SIGFM, the SIFT-based matcher proposed for libfprint in MR !418 and its rebase !530 (`libfprint/sigfm/sigfm.cpp` at bc98662).
  - Plain SIFT on the image the driver delivers, ratio 0.75.
  - Matches are de-duplicated by the template keypoint's integer y. The `std::set` comparator only compares y; this quirk is reproduced.
  - Score = number of pairs of length-consistent match pairs whose angle terms agree within 5%.
  - Directional, as libfprint calls it. Upstream thresholds: 24 (elan in !530) and 10 (FPC1022 driver in !570, which also upscales 2x nearest-neighbour). Variants: 1x, 2x nearest, 2x linear.
- **Reproducibility.** RANSAC is seeded. Results are identical under OpenCV 4.14.0 (used for the tables) and 5.0.0, which Arch ships. OpenCV 5 drops AKAZE, so it is the only variant that needs 4.x.
- **PR #288 capture gates** were also evaluated: coherence ≥ 0.45, foreground ≥ 25% at std 40, ≥ 50 keypoints. They would reject right-index-03 and right-middle-06 (foreground 17% and 21% at std 40). All images stay in the analysis.

### 3.3 Metrics (`fpscore.py`)

- **Pair distributions.**
  - NBIS and SIGFM are directional: 222 genuine and 480 impostor ordered pairs.
  - PR #288 is symmetric: 111 genuine and 240 impostor unordered pairs.
- **FRR at zero observed FA.** The threshold is set just above the highest impostor score seen.
- **FRR / FAR at the shipped thresholds.**
- **Enrollment simulation.**
  - k = 3, 5 and 7 images enrolled per finger, using every k-subset: 275, 525 and 240 splits.
  - Genuine probes are the rest of that finger. Impostor probes are all images of the other two fingers.
  - A probe's score is the maximum over the k enrolled images, which is libfprint's rule and PR #288's.
  - k = 7 covers only the two 10-image fingers.

### 3.4 Coverage diagnostic

Genuine PR #288 pairs scoring ≥ 10 are chained into per-finger mosaics. 10 is above every impostor score seen, and the chained transforms visibly align the ridges. The chains show:

- how many separate regions of each finger the touches sampled;
- for genuine pairs that did not match but share a region, how much they are predicted to overlap;
- a coverage ceiling: the FRR of a matcher that matches every pair inside a linked region and nothing else.

### 3.5 Control: sensor-sized crops of clean prints (`make_crop_set.py`)

- **Source.** libfprint's `examples/prints` (NIST, public domain), scaled by 1.6 to 0078's ridge period. Each print is cut into a 3x3 grid of crops.
- **Pairs.** Every genuine pair is a pure translation of the same pixels, with 33-83% overlap. This is the best possible case for any matcher.
- **Sizes.**
  - 144x56: this sensor.
  - 288x112: 4x the area.
  - 288x224: 8x the area, comparable to an ordinary area sensor.

## 4. Results on the 06cb:0078 captures

### 4.1 All methods

"Shipped threshold" is libfprint's 40 and egis0570's 25 for NBIS, 6 for PR #288, and 24 / 10 for SIGFM. FAR was 0 in every cell shown.

| family | variant | features per image (median) | genuine pairs > 0 | genuine median / max | impostor max | pair FRR @0FA | enroll-3 FRR @0FA | enroll-5 FRR @0FA (threshold) | enroll-5 FRR at shipped threshold | enroll-7 FRR @0FA |
|---|---|---|---|---|---|---|---|---|---|---|
| NBIS | stock (installed libfprint) | 2 minutiae | 0/222 | 0 / 0 | 0 | 100% | 100% | 100% (1) | 100% @40, 100% @25 | 100% |
| NBIS | partial | 1.5 | 0/222 | 0 / 0 | 0 | 100% | 100% | 100% (1) | 100%, 100% | 100% |
| NBIS | up2 (pixman) | 5.5 | 2/222 | 0 / 4 | 0 | 99.1% | 96.9% | 94.5% (1) | 100%, 100% | 92.2% |
| NBIS | up2-partial (egis0570/elanspi) | 5 | 0/222 | 0 / 0 | 0 | 100% | 100% | 100% (1) | 100%, 100% | 100% |
| NBIS | up3-partial | 8 | 0/222 | 0 / 0 | 0 | 100% | 100% | 100% (1) | 100%, 100% | 100% |
| NBIS | inverted-up2-partial | 5 | 0/222 | 0 / 0 | 0 | 100% | 100% | 100% (1) | 100%, 100% | 100% |
| NBIS | clahe-up2-partial | 6 | 4/222 | 0 / 5 | 0 | 98.2% | 95.8% | 94.1% (1) | 100%, 100% | 92.2% |
| NBIS | mask-up2-partial | 5 | 0/222 | 0 / 0 | 0 | 100% | 100% | 100% (1) | 100%, 100% | 100% |
| NBIS | gabor | 2 | 0/222 | 0 / 0 | 0 | 100% | 100% | 100% (1) | 100%, 100% | 100% |
| NBIS | gabor-up2-partial | 2 | 0/222 | 0 / 0 | 0 | 100% | 100% | 100% (1) | 100%, 100% | 100% |
| NBIS | gabor-up3-partial | 3.5 | 0/222 | 0 / 0 | 0 | 100% | 100% | 100% (1) | 100%, 100% | 100% |
| NBIS | stock-cubic2 (host bicubic 2x) | 8 | 18/222 | 0 / 9 | 3 | 94.6% | 84.5% | 77.0% (4) | 100%, 100% | 72.5% |
| PR #288 | pr288 (as published) | 183 keypoints | 27/111 | 0 / 80 | 1 | 76.6% | 48.0% | 29.7% (2) | 36.3% @6 | 16.9% |
| PR #288 | pr288-mask25 | 310 | 26/111 | 0 / 117 | 3 | 76.6% | 49.0% | 31.6% (4) | 31.6% @6 | 18.8% |
| PR #288 | pr288-nomask | 409 | 28/111 | 0 / 116 | 4 | 76.6% | 49.0% | 31.6% (5) | 31.6% @6 | 18.8% |
| PR #288 | sift-1x-mask25 | 214 | 25/111 | 0 / 84 | 2 | 78.4% | 52.0% | 33.6% (3) | 36.3% @6 | 19.2% |
| PR #288 | orb-mask25 | 883 | 22/111 | 0 / 80 | 0 | 80.2% | 51.8% | 32.2% (1) | 32.2% @6 | 18.2% |
| PR #288 | akaze-mask25 | 182 | 24/111 | 0 / 79 | 2 | 81.1% | 53.5% | 33.8% (3) | 35.8% @6 | 19.2% |
| SIGFM | sigfm-1x | 231 | 49/222 | 0 / 100956 | 0 | 77.9% | 50.8% | 33.2% (1) | 41.0% @24, 37.1% @10 | 19.7% |
| SIGFM | sigfm-up2nn (fpcmoh style) | 273 | 51/222 | 0 / 625303 | 0 | 77.0% | 49.2% | 31.0% (1) | 32.4% @24, 31.0% @10 | 17.9% |
| SIGFM | sigfm-up2linear | 251 | 49/222 | 0 / 493862 | 0 | 77.9% | 50.5% | 32.4% (1) | 32.4% @24, 32.4% @10 | 18.9% |
| Ceiling | perfect matcher on the linked regions (4.4) | | | | | | 42.5% | 26.2% | | 15.7% |

The pair FRRs near 77% for every keypoint matcher are not matcher errors (section 4.4): most genuine pairs simply do not overlap.

### 4.2 NBIS: the full distributions

Every score not listed is 0. Ordered pairs: 222 genuine, 480 impostor.

| variant | images with no minutiae (cannot enroll) | self-match > 0 | non-zero genuine scores | non-zero impostor scores | cross-check: decisions / score sequences |
|---|---|---|---|---|---|
| stock | 2/27 | 0/27 | none | none | 160/160 / 160/160 |
| partial | 5/27 | 0/27 | none | none | 136/136 / 136/136 |
| up2 | 1/27 | 6/27 | 3, 4 | none | 252/252 / 252/252 |
| up2-partial | 1/27 | 4/27 | none | none | 252/252 / 252/252 |
| up3-partial | 11/27 | 5/27 | none | none | 88/88 / 88/88 |
| inverted-up2-partial | 1/27 | 3/27 | none | none | 252/252 / 252/252 |
| clahe-up2-partial | 1/27 | 7/27 | 3, 3, 5, 5 | none | 252/252 / 252/252 |
| mask-up2-partial | 2/27 | 3/27 | none | none | 240/240 / 240/240 |
| gabor | 3/27 | 0/27 | none | none | 152/152 / 152/152 |
| gabor-up2-partial | 4/27 | 1/27 | none | none | 216/216 / 216/216 |
| gabor-up3-partial | 15/27 | 0/27 | none | none | 56/56 / 56/56 |
| stock-cubic2 | 0/27 | 11/27 | 3 (x6), 4 (x6), 5 (x2), 8 (x3), 9 | 3, 3 | 264/264 / 264/264 |

Minutiae per image under the installed library, unmodified images: median 2, range 0-5.

- Upscaling raises the count: median 5-8, max 18 for bicubic 2x. Those extra minutiae do not match across touches. Even identical images reach a positive self-score in at most 11 of 27 cases.
- Gabor enhancement lowers the count. Smoothing removes the spurious endings mindtct was finding, and few real minutiae are left in 20 mm².
- With `PARTIAL` plus 3x upscaling, 11-15 of 27 images end with no minutiae at all. Those images fail enrollment.

### 4.3 Keypoint matchers: the full distributions

PR #288 family, unordered pairs:

| variant | | 0 | 1-3 | 4-5 | 6-9 | 10-19 | 20-49 | ≥ 50 |
|---|---|---|---|---|---|---|---|---|
| pr288 | genuine (111) | 84 | 2 | 2 | 4 | 8 | 5 | 6 |
| pr288 | impostor (240) | 239 | 1 | 0 | 0 | 0 | 0 | 0 |
| pr288-mask25 | genuine (111) | 85 | 0 | 0 | 1 | 5 | 13 | 7 |
| pr288-mask25 | impostor (240) | 239 | 1 | 0 | 0 | 0 | 0 | 0 |
| pr288-nomask | genuine (111) | 83 | 1 | 1 | 1 | 6 | 12 | 7 |
| pr288-nomask | impostor (240) | 236 | 1 | 3 | 0 | 0 | 0 | 0 |
| sift-1x-mask25 | genuine (111) | 86 | 1 | 1 | 6 | 5 | 9 | 3 |
| sift-1x-mask25 | impostor (240) | 237 | 3 | 0 | 0 | 0 | 0 | 0 |
| orb-mask25 | genuine (111) | 89 | 0 | 0 | 1 | 6 | 12 | 3 |
| orb-mask25 | impostor (240) | 240 | 0 | 0 | 0 | 0 | 0 | 0 |
| akaze-mask25 | genuine (111) | 87 | 3 | 1 | 4 | 7 | 8 | 1 |
| akaze-mask25 | impostor (240) | 199 | 41 | 0 | 0 | 0 | 0 | 0 |

SIGFM, ordered pairs (the score grows roughly with the fourth power of the match count):

| variant | | 0 | 1-9 | 10-23 | 24-99 | 100-999 | ≥ 1000 |
|---|---|---|---|---|---|---|---|
| sigfm-1x | genuine (222) | 173 | 4 | 5 | 5 | 8 | 27 |
| sigfm-1x | impostor (480) | 480 | 0 | 0 | 0 | 0 | 0 |
| sigfm-up2nn | genuine (222) | 171 | 1 | 1 | 5 | 11 | 33 |
| sigfm-up2nn | impostor (480) | 480 | 0 | 0 | 0 | 0 | 0 |
| sigfm-up2linear | genuine (222) | 173 | 1 | 1 | 6 | 9 | 32 |
| sigfm-up2linear | impostor (480) | 480 | 0 | 0 | 0 | 0 | 0 |

Observations:

- **The PR's mask threshold matters on 0078.** At 40 (as published), the mask keeps a median of only 183 keypoints. Eight genuine pairs then land at 1-9 inliers, close to the impostor maximum. At 25 there is a clean gap: impostors at most 3, genuine pairs either 0 or at least 9, so FRR is flat from threshold 4 to 8.
- **Without a mask, the uncovered sensor area produces impostor scores of 4.** PR #288's source warns about exactly this: fixed-pattern streaks that look alike in every capture. At threshold 4, that would mean 478 false accepts in the 5-image simulation.
- **SIGFM at 1x loses a few weak genuine pairs** at threshold 24. The 2x nearest-neighbour upscale used by the pending FPC1022 driver avoids that.

Enrollment simulation, FRR at the shipped threshold:

| variant | threshold | k = 3 (1820 genuine / 4780 impostor attempts) | k = 5 (2562 / 8988) | k = 7 (720 / 4080) |
|---|---|---|---|---|
| pr288 | 6 | 54.3% | 36.3% | 21.1% |
| pr288-mask25 | 6 | 49.0% | 31.6% | 18.8% |
| sigfm-up2nn | 24 | 50.5% | 32.4% | 18.9% |
| ceiling (4.4) | | 42.5% | 26.2% | 15.7% |

FAR is 0 in every cell. At their zero-FA thresholds, k = 5 gives these three variants 0% FRR for left-index, 28-33% for right-index and 30-33% for right-middle. Every variant has at least one split with 100% FRR: an enrollment that covers one region and probes that all come from another.

### 4.4 Coverage: why FRR stays high with any matcher

| finger | images | linked regions (sizes) | genuine pairs | matched (≥ 6) | same region but unmatched: predicted overlap | in different regions |
|---|---|---|---|---|---|---|
| left-index | 7 | 2 (4, 3) | 21 | 9 | none | 12 |
| right-index | 10 | 4 (2, 3, 2, 3) | 45 | 7 | 1 pair, 0% | 37 |
| right-middle | 10 | 4 (1, 5, 3, 1) | 45 | 10 | 3 pairs, 14% / 37% / 54% | 32 |

- Each linked region covers 7-37 mm² of skin, and a single touch covers about 20 mm². The "slight shifts" between touches therefore move the 2.8 mm-tall window onto different skin: 81 of 111 genuine pairs come from touches that share no chain of matches.
- Only three genuine pairs look like real matcher misses: right-middle-02 against three members of its own region, at 14-54% predicted overlap.
- The ceiling row in 4.1 is what a matcher with no misses would score. At k = 5 and their zero-FA thresholds, PR #288 and SIGFM sit 3.5-7 points above it (29.7-33.2% against 26.2%).
- This ceiling is estimated with the same matcher. It is therefore a lower bound on FRR for this set, not an independent ground truth.

## 5. Control: crops of clean prints (NIST examples from libfprint)

Each set has 4 prints x 9 crops. Genuine pairs are exact translations with 33-83% overlap.

| crop | area vs 0078 | NBIS stock: minutiae, genuine median, impostor max | NBIS enroll-5 FRR @40 (stock / up2-partial / cubic2) | NBIS enroll-5 FRR @0FA (stock) | PR #288 mask25: enroll-5 FRR @6 | SIGFM up2nn: enroll-5 FRR @24 |
|---|---|---|---|---|---|---|
| 144x56 | 1x | 3, 0, 0 | 100% / 100% / 96.5% | 100% | 0% | 0% |
| 288x112 | 4x | 18, 14, 8 | 45.6% / 33.4% / 25.9% | 1.7% (thr 9) | 0% | not run (slow) |
| 288x224 | 8x | 45.5, 138, 21 | 0% / 0% / 0.6% | 0% (thr 22) | 0% | not run (slow) |

Even when every genuine pair is a large exact overlap of the same pixels, NBIS does not work at 0078's window size. It begins to work at 4x the area and works normally at 8x, where genuine pairs score a median of 138 against threshold 40. The keypoint matchers have no difficulty with these crops. The real captures are harder (pressure, distortion, partial overlap), so the control does not predict their FRR; it only isolates the area effect.

## 6. Limits of this sample

- **Small and narrow.** 27 images, 3 fingers, 1 person, 1 session, 1 sensor unit. There are 3 impostor finger pairs in all, and all of them come from the same person.
  - "Zero false accepts" here means 0 of 240-480 pair comparisons, or 0 of 4080-8988 simulated attempts. Those attempts reuse the same 27 images, so they are strongly correlated.
  - Even treated as independent, 0/240 bounds FAR only below about 1.2% (rule of three, 95%). That is two orders of magnitude short of a convenience-grade target such as 1/10,000.
  - The zero-FA thresholds are set on the observed maximum, so they are optimistic.
- **Not answered:**
  - impostor scores between different people;
  - genuine scores across days (dry or wet skin, pressure);
  - FRR at the 15-18-touch enrollments that 0078 will need. The most any finger here allows is 7 enrolled touches with 3 probes.
- **Robust despite the size:** the NBIS negative.
  - It rests on genuine scores, which are at most 9 against a threshold of 40, with self-matches mostly 0.
  - It holds in all 12 variants and in the control.
  - It agrees with the pending libfprint FPC1022 driver (MR !570, 112x88): "NBIS/Bozorth3 produces essentially zero stable minutiae on images this size".
- **The keypoint result is encouraging, not conclusive.**
  - On this sample: a gap between impostor scores (≤ 3) and the genuine scores of overlapping touches (≥ 9).
  - The pattern matches PR #288's own evaluation on 06cb:00a8 (145 captures, 5 fingers, 2 days, impostor maximum 4, FRR 2.1% at 18 touches). That is the same sensor type 0x2449 and the same 144x56 frame.

## 7. Architecture implications

### (a) Native libfprint FpImageDevice relying on NBIS

Not viable, on the evidence above. No threshold yields a usable operating point: at 40 or 25 nothing is accepted, and anything low enough to accept genuine scores of 3-9 sits at the impostor scores already seen with only 3 fingers.

There is an upstream route that keeps the FpImageDevice shape: swap the algorithm, not the driver model. MR !530 adds `img_class->algorithm = FPI_DEVICE_ALGO_SIGFM` with `score_threshold`.

- It is a rebase of !418 (2022) and is still unmerged. It was last updated on 2026-10-10, and GitLab shows its merge status as "CI must pass".
- It adds a C++/OpenCV dependency to libfprint. That dependency is the open question for the maintainers.
- The FPC1022 driver (!570) depends on it.

On this data SIGFM is as good as PR #288. If !530 lands, a 0078 FpImageDevice would need little more than capture code plus `nr_enroll_stages` of about 15 and the SIGFM algorithm. Upstream acceptance of the capture blobs (`research/review-prior.md`) remains a separate obstacle.

### (b) libfprint FpDevice doing its own host matching

This works technically: the driver stores keypoints in the FpPrint and implements verify and identify itself. It inherits every cost of (a)+SIGFM without the shared infrastructure:

- a SIFT-class matcher in C/C++ inside one driver, meaning OpenCV or a hand-written SIFT;
- review resistance to per-driver matchers.

It is the fallback if !530 stalls and a libfprint-based stack is still required. It could be kept out of tree, for example via the libfprint-tod fork, which I have not checked for this case.

### (c) python-validity + open-fprintd with the SIFT matcher

The matcher is measured to work on 0078 images, and PR #288 already covers the same sensor type (0x2449) and frame format:

- enroll and verify state machine;
- quality gates;
- template store;
- 18-touch enrollment, which is exactly what the coverage result calls for;
- the firmware-extraction helper.

`research/review-prior.md` lists what 0078 needs on top: the capture module, no pairing or TLS, and the calibration builder. Costs:

- open-fprintd replaces fprintd;
- PR #288 and open-fprintd PR #29 are unmerged;
- distribution is via the AUR or by hand.

### Recommendation

**Go with (c)**, the PR #288 matcher, with these 0078 adjustments:

1. Foreground-mask block std threshold 25 instead of 40, for matching and for the coverage gate. Then re-check `MIN_FOREGROUND` and `MIN_KEYPOINTS` on 0078 data.
2. Keep the 18-touch enrollment, or at least 15, and prompt the user to move the finger between touches.
3. Keep `ACCEPT_THRESHOLD = 6` until a larger set says otherwise.

Why (c):

- It is the only option whose matcher is both measured to separate these fingers and already written.
- Option (a) with NBIS is out.
- Options (b) and (a)+SIGFM need either C/C++ matcher work or an upstream decision we do not control. Because SIGFM performs the same on this data, the work in (c) does not block a later move to libfprint if !530 lands.

Before trusting any threshold, collect a larger set and rerun `run_all.sh` on it:

- at least 5 fingers, 20+ touches per finger;
- 2-3 sessions on different days;
- ideally prints from 2-3 people, for impostor comparisons between different people.

## 8. Rerunning

```sh
# venv with OpenCV 4.x + numpy (system gi), patched libfprint build, everything, digest:
research/matcher/run_all.sh proto/captures <OUT outside git> <WORKDIR, e.g. a scratch directory>

# or piecewise:
research/matcher/build_libfprint.sh WORKDIR                      # once
python nbis_libfprint.py CAPTURES --out OUT --libfprint-build WORKDIR/build-lf --crosscheck 4
python sift_matcher.py  CAPTURES --out OUT --overlap pr288-mask25 --ks 3,5,7
python make_crop_set.py PRINTS/*.png --out WORKDIR/control/crops-144x56 --crop 144x56   # control
python summarize.py OUT --detail stock-cubic2,pr288-mask25,sigfm-up2nn > OUT/digest.md
```

- **CAPTURES** may be one finger folder or a parent of several. Each finger folder needs `manifest.json` from `proto/capture.py`, and only images with `accepted: true` are used. The scripts refuse an output directory inside a git work tree unless it is git-ignored.
- **Tunables.** `nbis_libfprint.py --list-variants` lists the NBIS variants, and `sift_matcher.py` has its variants in `VARIANTS`. Thresholds and enrollment sizes are module constants or flags.
- **Run time on this set.** About 3-25 s per NBIS variant; about 1-12 ms per keypoint comparison in Python.
- **Larger images.** SIGFM is slow on crops larger than the sensor: its score counts pairs of match pairs.

| file | role |
|---|---|
| `research/matcher/fpdata.py` | capture-set loader (manifest, PGM), refuses tracked output paths |
| `research/matcher/fpscore.py` | distributions, zero-FA FRR, fixed-threshold rates, EER, enrollment simulation, markdown |
| `research/matcher/preprocess.py` | host preprocessing variants (CLAHE, mask, Gabor, bicubic) |
| `research/matcher/nbis_libfprint.py` | libfprint virtual_image runner, NBIS variants, real-enrollment cross-check |
| `research/matcher/sift_matcher.py` | PR #288 port and variants, SIGFM port, coverage diagnostic |
| `research/matcher/make_crop_set.py` | crop control sets from larger prints |
| `research/matcher/summarize.py` | one digest across all results |
| `research/matcher/build_libfprint.sh`, `libfprint-virtual-image-eval.patch` | private libfprint build for driver-side flags and resize (eval only, not for installation) |
| `research/matcher/run_all.sh` | everything above in order |
