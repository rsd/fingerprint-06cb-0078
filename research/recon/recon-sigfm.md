# recon-sigfm: state of SIGFM for libfprint (MR !530), integration, forks, rebase cost

Date of research: 2026-10-14. Read-only except for scratch clones and local builds in a scratch directory (nothing installed, no USB access).
Sources: GitLab web/API on gitlab.freedesktop.org (MR metadata via `/api/v4`, discussions via `/-/merge_requests/N/discussions.json?per_page=100`; the notes API itself needs a login), GitHub via `gh`, AUR RPC/cgit, and the code itself.

Local working files (not published):

- `src/libfprint-sigfm/`: a full clone of https://gitlab.freedesktop.org/libfprint/libfprint at master (6f9479c3). Local branches: `mr530` (MR head bc986621), `mr418`, `mr570`, `mr580`, `mr662`, `mr664`, `mr665`, `ssb-bench` (ssubbotin's `sigfm-benchmark-fpc1022`), `ssb-elan`, **`sigfm-ssb-series`** (master + the 8 SIGFM-only commits from ssubbotin, no fpc1022 driver), `trial-ssb`. Remote `ssubbotin` added.
- `ssb-series-patches/0001..0008-*.patch` (`git format-patch master..sigfm-ssb-series`), `mr530-squashed.patch`.
- `discussion.txt`, `discussion2.txt`, `key_notes.txt` (MR !530 thread text), `d418.json`, `d570.json`, `d664.json` ... (raw discussions), `forks/` (GitHub metadata and probe results), `aur/` (PKGBUILDs).
- `src/trial/{mr530,ssb,series}` source exports and `src/trial/b-*` build dirs.
- `rerun/`: a copy of `research/matcher/*.py` with two added "corrected SIGFM" variants and the results in `rerun/out/`.
- The shallow libfprint clone used by the matcher evaluation carries an uncommitted eval patch; I did not touch it.

---

## 1. State of MR !530

URL: https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/530 (title "add sigfm algorithm implementation (rebased and enabled Elan sensors)").

| item | value |
|---|---|
| author | Tooniis (project fork `Tooniis/libfprint`, branch `sigfm`) |
| opened / last activity | 2025-04-04 / **2026-10-10 09:21 UTC** (force-push rebase onto master + `opencv5` added to meson, plus a reply to ssubbotin) |
| state | open, not draft, no reviewer or assignee, no milestone, 6 upvotes, 103 notes, 20 commits, 38 files |
| merge status | `can_be_merged`, `ci_must_pass`. The 2026-10-10 pipeline (https://gitlab.freedesktop.org/Tooniis/libfprint/-/pipelines/1763438) failed in the `test_indent` (uncrustify) job, so the build and test stages were skipped. Per ssubbotin (2026-07-16) upstream CI has never compiled the sigfm sources because the CI image has no OpenCV. |
| base | master **6f9479c3** = `v1.94.100-10-g6f9479c3` (2026-09-02, "synaptics: Add new PID 0x10b"). That is the current tip of `origin/master` (verified 2026-10-14: 0 commits ahead). Arch ships `libfprint 1.94.100-1`. |
| predecessor | !418 by 0x00002a (2022-11-24, based on `v1.94.6-10-g2414dbdb`), still open, `need_rebase`, last updated 2025-10-02. Algorithm originally by mpi3d (https://github.com/goodix-fp-linux-dev/sigfm, MIT, last push 2022-11-13). |
| replaced by something? | **No newer MR or branch replaces it.** !530 supersedes !418 in practice. Related: !570 (ssubbotin, FPC1022 driver) carries SIGFM as its first commit, with fixes. ssubbotin offered on 2026-09-01 to push his corrected series to Tooniis's branch, to open a companion MR that closes !530, or to leave patches. Tooniis answered on 2026-10-10 that it is better to stay on !530 and that he can cherry-pick additional commits, "but first let's see if any maintainers respond". The 2026-10-10 head still has the buggy comparator (checked in the file), so ssubbotin's fixes are not in !530. |

Maintainers involved: anarsoul (Vasily Khoruzhick), 3v1n0 (Marco Trevisan), benzea (Benjamin Berg). benzea has not commented on !530 (only 4 notes on !418). Last maintainer words on !530:

- 2026-04-08 anarsoul: cannot review until May or later; asks 3v1n0 or benzea to review and reproduce the benchmark results, "it should be good to merge once benchmark results are confirmed/reproduced".
- 2026-05-19 anarsoul: "libfprint needs a call for maintainers since a lot of MRs stay open for months."
- 2026-07-11 3v1n0: wants a benchmark through a libfprint virtual driver running SIGFM, ideally compared with NBIS. 2026-07-27 3v1n0: SDCP-less devices cannot defend against a crafted device anyway.
- Nobody from the maintainers has replied to ssubbotin's 2026-09-01 benchmark post (which implements what 3v1n0 asked for) nor to the 2026-10-10 rebase, as of 2026-10-14.
- On !580 (mafp8800, 2026-06-18) 3v1n0 asked whether using !530 would be better than a custom matcher. So the maintainers' direction is a shared matcher, not per-driver matchers.

Objections and blockers raised over the life of !418 and !530:

1. **Security evidence (the main blocker).** "Can't merge something that allows logging into the system without knowing the false-positive rate" (anarsoul 2022-10 and 2025-04-04). A benchmark on a public database is required and must be reproducible by a maintainer. Several people produced benchmarks (Tooniis's paper on https://gitlab.com/Tooniis/sigfm-eval, wl2776's https://gitlab.com/wl2776/sigfm-eval and C++ tool, mrickma's algo-test-sigfm, ssubbotin's `tests/benchmark-matching.py`). Reviewers criticised protocols (crops of the same image, same-finger id parsing bug in wl2776's tool found by ArcticLampyrid) and none was accepted yet.
2. **The matcher itself was found defective in 2026-07**: `match::operator<` repeats `p1.y < right.p1.y`, so the `std::set` collapses all correspondences with the same integer y (about 80% dropped); also O(n^4) worst-case work (svlmrc review 2026-07-22). Found independently by ssubbotin (2026-07-15) and svlmrc. All earlier benchmark numbers were declared void by ssubbotin (2026-09-01). Not fixed in !530.
3. **Thresholds do not transfer between sensors.** The shipped default 24 sits inside the measured impostor tail for some sensors (ssubbotin, FVC2000 DB3: 20 false accepts in 29,700 at 24; chytrik, Elan 0c6e: impostor max 105, non-enrolled finger accepted 2 of 5 at 24; ssubbotin's FPC1022: an impostor scored 45 against a driver threshold of 10). Verification is "any of N enrolled images clears the threshold", so a deeper enrollment buys more chances of a false accept (DB3: 20 to 51 false accepts going from 5 to 15 images). Request: per-driver constants (`distance_match`, `length_match`, `min_match`, threshold), ideally from DPI and size.
4. **Image quality sensitivity** (anarsoul 2023-03-01): works with good-contrast 108x88 images, unusable with raw Goodix images; wants pre-processing helpers rather than in-matcher pre-processing.
5. **Print migration** (benzea 2023-01-24): a driver switching from NBIS to SIGFM invalidates existing prints; the prints already store their algorithm type; the practical answer is "new/renamed driver or re-enroll".
6. **Housekeeping:** SPDX headers, uncrustify, clean history, C++ standard (c++14 vs c++17), hosting of the library (3v1n0 suggested moving it under the libfprint umbrella), license compatibility (the standalone library is MIT).
7. **Dependency:** OpenCV is a new hard build dependency in !530 (and doctest). Nobody on the maintainer side has said yes or no. Competing approaches exist: !665 (rhys.saldanha, draft) vendors ezSIFT (Apache-2.0, ~2k LOC) to avoid OpenCV; lbssousa's fork reimplemented SIGFM in C (see section 3).

---

## 2. How SIGFM integrates

All of this is from the code at `mr530` (bc986621) and, where different, from ssubbotin's series `sigfm-ssb-series` (section 3 explains why that one is the better copy source). Both share the same API names; a driver written against one compiles against the other.

### 2.1 Files added or changed in !530 (38 files, +6597/-121, of which 5479 lines are the embedded test data `tests-embedded.hpp`)

New in `libfprint/sigfm/`: `sigfm.cpp` (205 lines, the matcher), `sigfm.h` (C API), `binary.hpp` (home-made serializer for `cv::Mat`/`cv::KeyPoint`), `img-info.hpp` (`struct SigfmImgInfo { std::vector<cv::KeyPoint> keypoints; cv::Mat descriptors; }`), `meson.build`, `tests.cpp` + `tests-embedded.hpp` (doctest).

Changed core files: `fp-image.c/.h` (async `fp_image_extract_sigfm_info` in a GTask thread; stores `image->sigfm_info`), `fpi-image.h` (`FpImage.sigfm_info`), `fpi-image-device.c/.h` (algorithm dispatch, `FpiImageDeviceAlgorithm`, rename to `fpi_image_device_set_score_threshold`), `fp-image-device.c`, `fp-image-device-private.h`, `fpi-print.c/.h` (`FPI_PRINT_SIGFM`, `fpi_print_sigfm_match`, add/copy/free handling), `fp-print.c` (serialize/deserialize), `libfprint/meson.build`, `meson.build`, `.gitlab-ci/libfprint-templates.yaml`.

Driver files: 18 one-line renames `bz3_threshold` -> `score_threshold` (aes1610, aes1660, aes2660, aes3k, egis0570 .c/.h, elanspi, nb1010, secugen, upeksonly, upektc, upektc_img, vfs0050, vfs101, vfs301, vfs5011, vfs7552, elan) plus `elan.c` switching to SIGFM (`scan_type = PRESS`, `nr_enroll_stages = 15`, `algorithm = FPI_DEVICE_ALGO_SIGFM`, `score_threshold = 24`) and dropping the swipe-stitching comment.

### 2.2 How a driver opts in

Class fields on `FpImageDeviceClass` (private `fpi-image-device.h`), set in `class_init`:

```c
img_class->algorithm       = FPI_DEVICE_ALGO_SIGFM;   /* default 0 = NBIS */
img_class->score_threshold = <int>;                    /* renamed from bz3_threshold; default 40 */
dev_class->nr_enroll_stages = <n>;                     /* existing FpDeviceClass field, default 5 */
dev_class->scan_type = FP_SCAN_TYPE_PRESS;
img_class->img_width / img_height                      /* optional, if constant */
```

Runtime override: `fpi_image_device_set_score_threshold (self, n)` (renamed `fpi_image_device_set_bz3_threshold`). Nothing else per driver in !530. In ssubbotin's series a driver additionally needs `'sigfm': true` in its `drivers_info` entry in the top-level `meson.build` (see 2.4), e.g. `'fpc1022': { 'sigfm': true, 'endian_dependent': true, 'helper': ['openssl'], 'optional': not have_opencv }`.

What the driver must deliver: an 8-bit grayscale `FpImage` exactly as SIFT should see it. The SIGFM path in `fp-image.c` ignores the FpiImageFlags that the NBIS path honours (`FPI_IMAGE_COLORS_INVERTED`, `H/V_FLIPPED`, `PARTIAL`); do any flip, inversion, upscale or contrast work in the driver. ssubbotin's fpc1022 driver does a 2x nearest-neighbour upscale itself and sets `img_width/img_height` to the scaled size; that equals our `sigfm-up2nn` variant. Images with fewer than 25 keypoints are rejected in the extract thread ("No enough keypoints found"), which turns into `FP_DEVICE_RETRY_GENERAL` and the enroll stage does not advance (hard-coded constant in `fp-image.c`).

### 2.3 What is stored in an FpPrint

- `FpPrint.type = FPI_PRINT_SIGFM` (new enum value 3, after UNDEFINED/RAW/NBIS). `fpi_print_set_type` creates `print->prints`, a `GPtrArray` whose elements are `SigfmImgInfo *` with `sigfm_free_info` as free function.
- One `SigfmImgInfo` per enrollment stage: all SIFT keypoints (`cv::KeyPoint`) and the float32 descriptor matrix (N x 128). It is the full descriptor set, not a reduced template. Rough size, my estimate from the 30 captures (median 235 keypoints at 1x, 277 at 2x NN, 512 bytes of descriptor per keypoint): about 120-150 KB per stage, so about 2 MB for a 15-stage print. Not measured on disk.
- Persistence: `fp_print_serialize` writes the usual FP3 GVariant but with the SIGFM branch: `(a(ay))`, one byte array per stage produced by `sigfm_serialize_binary` (native-endian `memcpy` of the OpenCV structures in !530, hence not portable across architectures). `fp_print_deserialize` rebuilds the prints via `sigfm_deserialize_binary` and fails with invalid-format on `NULL`. In ssubbotin's series each blob starts with the bytes `SGFM` + version 1 and is validated on load.
- The scanned probe print holds one entry. In `fpi_print_add_print` the enroll print gets a copy (`sigfm_copy_info`) per stage. `fpi_print_add_from_image` stores `image->sigfm_info` directly in the print (shared pointer in !530; ssubbotin made image and print hold independent copies, because the shared pointer is a leak/double-free hazard).
- Matching, `fpi_print_sigfm_match(template, probe, threshold)`: loops over the template's stages, calls `sigfm_match_score (template_stage, probe)` (argument order matters: it is directional, kNN query = template, train = probe), logs `sigfm score %d/%d` at debug level and returns MATCH at the first stage with `score >= threshold`. So it is "OR over the enrolled images". Verify and identify both go through `fpi_image_device_minutiae_detected`, which dispatches on `priv->algorithm`.
- Score: SIFT (`cv::SIFT::create()`, default parameters, plain image), `BFMatcher` kNN 2, Lowe ratio 0.75, dedup of correspondences in a `std::set`, then the number of pairs of length-consistent match pairs (length tolerance 5%) whose two angle terms agree within 5%. It grows roughly with the 4th power of the consistent match count, so the scale is huge (genuine up to 1e6 or more) and has no natural unit.

### 2.4 Dependencies and meson changes

- Requires OpenCV (C++), `cpp_std=c++17`. Modules linked with OpenCV 5.0.0 in the local build (`ldd` of the built `libfprint-2.so`): `opencv_core`, `opencv_features`, `opencv_flann`, `opencv_imgproc`, `opencv_geometry`, `opencv_dnn` (the last comes from the pkg-config requirement list, not from our code). Code uses `cv::SIFT`, `cv::BFMatcher`, `cv::Mat`, `cv::KeyPoint`. No other libraries.
- !530 `libfprint/sigfm/meson.build`: `dependency('opencv4', 'opencv5', required: true)` and **`dependency('doctest', required: true)`**, static library `sigfm` linked into `libfprint-private`, executable `sigfm-tests` (no `test()` registered), `install_headers('sigfm/sigfm.h')`. So **every libfprint build needs OpenCV and doctest**, not only builds with a SIGFM driver. Verified here: on this machine configure fails at `libfprint/sigfm/meson.build:5:10: Dependency "doctest" not found` (doctest is not installed; OpenCV 5.0.0 is found via `opencv5.pc`). With doctest made optional in a scratch copy, !530 compiles and links with GCC 16.2.1 and OpenCV 5.0.0 (warnings only: `-Wswitch` in `fp_print_equal` for `FPI_PRINT_SIGFM`, OpenCV header warnings).
- `.gitlab-ci/libfprint-templates.yaml` edit in !530 is malformed (two lines appended after `python3-gobject-base` with no line continuation) and the image tag is not bumped although the description says so.
- ssubbotin's series makes it optional: `opencv_dep = dependency('opencv4')`, else `opencv5` (required: false), `libfprint_conf.set10('HAVE_SIGFM', true)`, all SIGFM code under `#ifdef HAVE_SIGFM`, a new `'sigfm': true` key in `drivers_info` that makes such drivers optional (like SPI drivers) and errors out if requested without OpenCV, `doctest` optional (`sigfm-tests` built only if found), `sigfm-bench` with `build_by_default: false`, CI image `opencv-devel` + tag v8 -> v9, and `data/autosuspend.hwdb` handling when OpenCV is missing.

### 2.5 Enrollment stages

SIGFM does not change the enrollment machinery. Each good capture is one stage: `fpi_image_device_minutiae_detected` adds the print to the enroll print and increments the stage; `nr_enroll_stages` comes from the driver's `FpDeviceClass` (default 5). Elan in !530 uses 15, FPC1022 uses 6, mrickma used 10, CS9711 forks 15-ish. There is no overlap check, deduplication, quality gate, template merging or mosaicking; the only gate is >= 25 keypoints. The print simply grows by one full descriptor set per stage, and every stage is one more chance of a false accept. ssubbotin suggests (not implemented) a quality floor per stage or requiring two images to agree.

Latent issue found by reading: `fpi_device_verify_complete` and `fpi_device_identify_complete` (`libfprint/fpi-device.c` lines ~2174 and ~2275) call `fp_print_equal` for every non-NBIS print. For SIGFM the scanned probe has one entry and the template N, so it can never be equal (in !530 `fp_print_equal` has no SIGFM case at all, which is the `-Wswitch` warning): the result is a spurious `g_warning` and the scanned print is dropped from the callback, while the match itself is still reported. gotenksIN fixed this in `094b65d0` ("device: Validate gallery equality only for raw prints"). Not run-time tested here.

---

## 3. Forks and packages that carry SIGFM

Bottom line: most forks copy the original !418/!530 matcher (buggy comparator, `distance_match 0.75`) onto old libfprint bases (1.94.5-1.94.10). Only the ssubbotin line, its descendant gotenksIN, and a few independent rewrites carry the corrected matcher, and only those are on 1.94.100.

Check legend: "fixed" = comparator uses the full tuple key (checked in `sigfm.cpp`).

| repo / package | what | base libfprint | last push | SIGFM state |
|---|---|---|---|---|
| **ssubbotin/libfprint, branch `sigfm-benchmark-fpc1022`** (https://gitlab.freedesktop.org/ssubbotin/libfprint), also MR !570 | FPC1022 / "fpcmoh" driver 10a5:9200/9201 + SIGFM series with benchmark | `v1.94.100-9` (1f335cd5, one commit behind master) | 2026-09-01 (branch); !570 touched 2026-10-03 | **fixed**, 176-correspondence cap, blob magic+version, independent print/image copies, `HAVE_SIGFM` optional, virtual-image env knobs, benchmark, CI. He says he will maintain the SIGFM code. |
| **gotenksIN/libfprint-fpc-disum** (GitHub, `main`) | ssubbotin's series + driver, hardened, native packages (Ubuntu CI, Arch PKGBUILD) | `v1.94.100-10` (6f9479c3, current master) | 2026-09-05 | fixed + extra hardening: bounds on keypoints/descriptors/stream, `max_angles = 500` cap (changes scores, would need re-evaluation), little-endian fixed-width blob v2 with legacy read, `fp_print_equal` fix, SIGFM tests registered in meson |
| AUR `libfprint-fpc1022` | FPC1022 | builds MR !570 commit ba10c939 (`v1.94.10-8` base) | 2026-07-25 | older base (`v1.94.10-8`) |
| MR !530 head, `Tooniis/libfprint` branch `sigfm` | the MR | `v1.94.100-10` | 2026-10-10 | buggy comparator; opencv+doctest hard-required |
| archeYR/libfprint-CS9711 (`cs9711-rebase`), AUR `libfprint-cs9711-rebase-git` | Chipsailing CS9711 (2541:0236), 56 stars | 1.94.10 | 2026-02-17 (AUR 2026-04-28) | buggy (!418/!530 code) |
| wl2776/libfprint-CS9711 (`cs9711-rebase`) | same + `tests/eval_sigfm_1_n.cpp` benchmark | 1.94.10 | 2026-07-27 | buggy; he tuned constants locally |
| ddlsmurf/libfprint-CS9711 | archived ("unmaintained") | 1.94.6 | 2026-01-03 | buggy |
| TheWeirdDev/libfprint (`55b4-experimental`), AUR `libfprint-goodixtls-55x4` (6 votes) | Goodix 55b4/55a4 (TLS) community fork, the "goodix small-sensor" one | 1.94.6 | 2026-08-03 | buggy (!418 copy) |
| goodix-fp-linux-dev/sigfm | the original standalone library by mpi3d | n/a | 2022-11-13 | origin, MIT; no commits after that date |
| goodix-fp-linux-dev/libfprint | goodixtls org fork | 1.94.5 | 2023-07-23 | no sigfm directory |
| Han-Haocheng/libfprint (`sigfm`), AUR `libfprint-gxfp` | Goodix GXFP5130 eSPI | 1.94.5 | 2026-08-25 | OpenCV-based, comparator not checked |
| Jenium-lab/libfprint-sigfm-fix-omarchy | **Omarchy** user build: Elan 04f3:0c4f via !530 + `opencv5.patch`, PKGBUILD `libfprint-sigfm-git`, setup notes (2026-08-21) | 1.94.9 | 2026-08-25 | a single-user build; the only Omarchy-specific SIGFM repo found |
| lbssousa/libfprint (`goodix-538d-sigfm-gtls`) | Goodix 538d; **SIGFM reimplemented in C, no OpenCV** (`libfprint/sigfm/sigfm.c`, 1006 lines: CLAHE + SIFT + brute-force kNN + the SIGFM score; commit 7081ce3f "sigfm: reimplement in C") | 1.94.10 | 2026-09-25 | uses CLAHE(4, 4x4) and ratio 0.85, not the !530 constants; validated by the author on 144 frames from one 538d sensor only |
| pythontyphon/libfprint-bf63112-oxp3 | Betterlife BF63112 SPI; own `sigfm.cpp` (fixed comparator, ratio 0.85) + `sigfm-ransac.cpp` (SIGFM score plus a RANSAC similarity score) | 1.94.100 | 2026-09-25 | independent variant |
| Sbenazar/goodix-5f10-libfprint | Goodix 5f10; own `drivers/goodixtls/sigfm.c` | 1.94.100 | 2026-09-08 | own C implementation |
| others (not examined in depth) | gulp/libfprint (5117, 1.94.5), danieledisint/libfprint-goodixtls511, carminezac (5e0a, 1.94.5), cathartyc (Elan, 1.94.9, 2025-03), Light-log (347d:0304), vrichv (Elan 0c28), r4nd3l (Elan 3104), t0mka14 (521d), ejsergeev (gm168sec), Rockytkg (5125), sergiomelas (multi-model Goodix) | mixed | mixed | mostly copies of the buggy !418/!530 code |

Drivers using SIGFM today: upstream, only Elan in !530 (not merged) and FPC1022 in !570 (not merged). Everything else is a downstream fork driver (Goodix 5110/52xd/53xd/538d/55b4/5117/5f10/521d/5125, CS9711, ELAN 0c28/0c4f/0c6e/3104, FPC1022, GXFP5130, BF63112, Betterlife 347d). Others took a different route: !662 (egis0570 touch) and !664/!665 (Synaptics 06cb:0082) embed their own OpenCV SIFT or vendored SIFT; !580 (mafp8800) uses its own SIFT-WHT matcher.

Relevant sibling: **MR !664 (rhys.saldanha, 2026-09-25) adds a driver for Synaptics 06cb:0082** with its own small-area matcher (`syna0082-matcher.cpp/.h` on OpenCV), and draft !665 replaces OpenCV with a vendored ezSIFT core plus upscale/CLAHE/kNN/RANSAC in the driver. It is another 144x56-class match-on-host Synaptics sensor, so it is worth reading for the driver work. Only 3 notes, no maintainer response (the author asked on 2026-09-25 "new dependency or custom matcher? what is preferred?", unanswered).

Omarchy context: Omarchy's own repo is `omacom/omarchy` (GitHub). Its `omarchy setup security fingerprint` installs the pinned `libfprint-git` and (issue https://github.com/omacom/omarchy/issues/11899, open) removes any fork that `provides=libfprint`, then the stale daemon in memory hides the breakage. Relevant to packaging: a carried fork must survive that script. I found no Omarchy-official SIGFM package.

### Best base and best source for the SIGFM code

Ranking for "copy the SIGFM code from":

1. **ssubbotin's SIGFM series (8 commits d258eb51..7263fb2c on branch `sigfm-benchmark-fpc1022`)**, already cherry-picked here as `sigfm-ssb-series` and exported as `ssb-series-patches/` (local). It is the only complete, correctness-fixed, optional-OpenCV, benchmarkable version, written against the same API as !530. Risk: single-author, not yet seen by a maintainer; the fixed matcher changes score scales (see section 4.3).
2. gotenksIN's additional commits on top (`094b65d0` fp_print_equal fix is worth taking; `406c10e1` hardening is useful for untrusted print files, but its `max_angles = 500` cap alters scores, so re-evaluate on our images first).
3. !530 as-is: acceptable only as the thing to rebase onto if/when upstream moves; do not copy from it now (buggy comparator, doctest/OpenCV hard dependency, broken CI edit, shared-pointer ownership).
4. lbssousa's C rewrite is the only OpenCV-free option that exists as code, but it deviates from SIGFM (CLAHE, 0.85) and was validated on one sensor; keep it as the fallback if OpenCV in the fork is rejected for packaging reasons.

---

## 4. Rebase cost onto current libfprint master

All tests were run in `src/libfprint-sigfm` (never in the earlier shallow clone), branches deleted afterwards except where listed above.

### 4.1 Merge and rebase results

| operation | result |
|---|---|
| `git merge --no-commit mr530` into `master` (6f9479c3) | **clean**, no conflicts (MR base is the master tip, so it is a fast-forward in effect) |
| same into tag `v1.94.100` | clean |
| `git merge mr530` into tag `v1.94.10` | clean, but that merge drags in everything between 1.94.10 and master, so it does not answer the question |
| rebase of the 20 !530 commits onto `v1.94.100` | clean, all 20 apply |
| squashed MR diff (`mr530-squashed.patch`) `git apply --3way` onto `v1.94.100` | clean |
| rebase of the 20 commits onto `v1.94.10` | **conflicts** from commit 1 in `libfprint/fp-print.c` and `libfprint/fpi-print.c`; squashed diff onto 1.94.10 also conflicts in `.gitlab-ci/libfprint-templates.yaml` and fails on `drivers/secugen.c` (driver does not exist in 1.94.10). Four files in total. Not resolved, as asked. |
| `git merge --no-commit ssb-bench` (SIGFM + fpc1022 driver) into `master` | clean (auto-merge only in `data/autosuspend.hwdb`) |
| cherry-pick of the 8 SIGFM-only commits d258eb51^..7263fb2c onto `master` and onto `v1.94.100` | **clean, 8/8 applied on both**; result is branch `sigfm-ssb-series` (58 files, +10714/-99, of which `libfprint/sigfm/` is 9 files and 8009 lines incl. the 5479-line test data and the 1461-line bench) |
| same 8 commits onto `v1.94.10` | conflicts in `fp-print.c`, `fpi-print.c`, `meson.build`, `drivers/secugen.c` (modify/delete) |

So the answer: onto master or v1.94.100 the rebase is trivial today, for both !530 and the ssubbotin series. The expected ongoing cost is the 18-driver `bz3_threshold` -> `score_threshold` rename: any driver added upstream that still sets `bz3_threshold` will stop compiling after the rename, and any hunk upstream touches in `fp-print.c`/`fpi-print.c`/`fpi-image-device.c` conflicts. In the 10 commits between v1.94.100 and master nothing did, and a full `-Ddrivers=all` build of `sigfm-ssb-series` on master links without errors (156 steps).

### 4.2 Build and test evidence (scratch, nothing installed)

- Host: GCC 16.2.1, meson 1.12, OpenCV 5.0.0 (`opencv5.pc` only; no `opencv4`), doctest absent.
- `sigfm-ssb-series` on master, `-Ddrivers=virtual_image,elan`: configures (`opencv5` found, doctest not found, tests skipped without error), builds, `meson test`: 7 OK / 0 fail / 34 skipped (skips are umockdev driver tests). `HAVE_SIGFM 1` in `config.h`. `-Ddrivers=all`: builds and links.
- `trial-ssb` (master + ssb-bench incl. FPC1022 driver, virtual_image+elan): builds, same test result.
- `mr530` as-is: fails at configure on missing doctest. With doctest made optional in a scratch copy it builds with warnings only. NONAN23x reported on !570 (2026-07-16) that an OpenCV 5 + GCC 16 `--werror` build fails in the SIGFM test code; I did not hit that because doctest is absent here (tests not compiled).
- Not run: the benchmark smoke test (needs the GObject introspection typelib; I disabled introspection), `sigfm-tests` (needs doctest), any hardware.

### 4.3 Re-run of our matcher evaluation with the corrected comparator

The earlier `research/matcher-evaluation.md` reproduced the !530 comparator quirk (dedup by integer y only). ssubbotin's fix changes the dedup key to (p1.y, p1.x, p2.y, p2.x) and caps the surviving correspondences at the 176 strongest by descriptor distance. I added that as variants `sigfmfix-1x` and `sigfmfix-up2nn` in a copy of `sift_matcher.py` (`recon-sigfm/rerun/`) and ran all four SIGFM variants on the current capture set (`proto/captures`: 30 images, 3 fingers x 10; the document's 27-image numbers are from before the top-up) with OpenCV 5.0.0. Results in `recon-sigfm/rerun/out/`.

| variant | keypoints median | impostor max (600 ordered pairs) | enroll-5 FRR @24 / FAR | enroll-5 FRR at zero FA | splits with a false accept |
|---|---|---|---|---|---|
| sigfm-1x (as !530) | 235 | 0 | 33.9% / 0.0% | 26.9% (thr 1) | 0 |
| **sigfmfix-1x** | 235 | **6** | **29.1% / 0.0%** | 28.7% (thr 7) | 0/360 at k=3 for thresholds >= 10 |
| sigfm-up2nn (as !530) | 277 | 0 | 26.3% / 0.0% | 25.0% (thr 1) | 0/756 |
| **sigfmfix-up2nn** | 277 | **0** | **25.9% / 0.0%** | 23.0% (thr 1) | 0/756 |

Reading: the correction does not hurt on this sensor. Genuine scores rise by roughly an order of magnitude (up to 4.75e6 for up2nn) and the impostor side stays at or near zero (max 6 at 1x, 0 at 2x NN), so a threshold in the tens remains above the observed impostor maximum. FRR is dominated by enrollment coverage exactly as the earlier document says. Caveats: three fingers of one person only, so the impostor tail is a lower bound; the numbers are from a Python port of the C++ (not the compiled matcher); `FRR @0FA` thresholds of 1 or 7 are not usable settings. Upstream threshold values (24 for Elan, 10 for FPC1022) were chosen on the buggy scale or on other sensors, so the real threshold for 0078 has to be set from on-sensor genuine/impostor scores (`FP_PRINT_SCORE_LOG`-style logging exists in ssubbotin's tree) and must sit above the impostor tail with margin.

---

## 5. Recommendation: where the SIGFM code should come from

Take it from **ssubbotin's SIGFM series** (`ssubbotin/libfprint` branch `sigfm-benchmark-fpc1022`, commits d258eb51..7263fb2c; local ready-made branch `sigfm-ssb-series` and local patch files in `ssb-series-patches/`), on top of `v1.94.100` or current master. Do not take the FPC1022 driver commit (19f4bb5b). Reasons: the matcher is corrected, OpenCV is optional and gated by `HAVE_SIGFM` plus a per-driver `'sigfm': true` key, the print format is versioned, the virtual-image driver can run SIGFM from environment variables (useful for our offline tests), a benchmark harness exists, it merges and builds cleanly today with the OpenCV 5 and GCC 16 that Arch ships, and the API (`img_class->algorithm = FPI_DEVICE_ALGO_SIGFM`, `score_threshold`) is identical to !530, so our driver code will port unchanged if !530 (or a merged variant) lands. Add gotenksIN's `094b65d0` fp_print_equal fix, and consider (after re-evaluating) his `406c10e1` hardening. Track !530: if it merges, drop the carried series and rebase the driver. The open decision: if depending on OpenCV in the fork is unacceptable for packaging, the fallbacks are lbssousa's C rewrite (no review yet, different constants) or !665's vendored ezSIFT approach, both of which need their own evaluation on our images.

## 6. Open points and things I did not verify

- The notes of !530 beyond what `discussions.json?per_page=100` returned (51 discussions, 103 notes counted by GitLab) were read in full except the long benchmark tables and a few long comments, which were truncated in my reading to 450-1500 characters; the maintainers' own comments were read in full.
- I did not contact anyone or post anything.
- Fork "maintained" judgements rest on last-push dates and commit messages only; I did not build the forks. Only the three trees listed in 4.2 were built.
- The Omarchy issue #11899 describes what Omarchy's setup script does today; I did not read the script itself.
- The `fp_print_equal` finding is from reading the code; not reproduced at run time.
- The ~2 MB-per-print size figure is an estimate, not a measurement.
- Related topics covered in other notes, not here: python-validity PR #288, driver protocol and packaging (recon-driver, recon-packaging).
