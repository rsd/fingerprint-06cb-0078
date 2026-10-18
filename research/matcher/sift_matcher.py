#!/usr/bin/env python3
# SPDX-License-Identifier: MIT AND LGPL-2.1-or-later
#
# Parts: the SIGFM constants and Matcher.sigfm() are ported from SIGFM
# (libfprint/sigfm/sigfm.cpp) and KEEP its terms, LGPL-2.1-or-later. Everything else
# is MIT: the PR #288 constants, preprocess()'s pr288 path, ridge_coherence() and
# Matcher.extract() / transform() / one_way() / pair() are ported from python-validity
# (MIT); Variant / VARIANTS, the ORB / AKAZE / raw / nn / linear variants,
# score_matrix(), run_variant(), overlap_diagnostic(), summary_rows(), main() and the
# rest of this file are our own code (MIT).
#
# The pr288* scorer, ridge_coherence() and the PR #288 constants are ported from
# python-validity (validitysensor/hostmatch/matcher.py, ops.py, quality.py of
# https://github.com/uunicorn/python-validity/pull/288, by ashh87), MIT licence:
#   Copyright (c) 2020 uunicorn
#   Copyright (c) 2026 ashh87 (PR #288, commit 451cd11)
# The sigfm* scorer (Matcher.sigfm) and the SIGFM constants are ported from SIGFM
# (libfprint/sigfm/sigfm.cpp, https://gitlab.freedesktop.org/libfprint/libfprint
# merge requests !418 / !530), LGPL-2.1-or-later:
#   Copyright (C) 2022 Matthieu CHARETTE <matthieu.charette@gmail.com>
#   Copyright (c) 2022 Natasha England-Elbro <natasha@natashaee.me>
#   Copyright (c) 2022 Timur Mangliev <tigrmango@gmail.com>
# The port and our own code in this file:
#   Copyright (C) 2026 Raul Dias <raul@dias.com.br>
# The SIGFM part stays under LGPL-2.1-or-later; the python-validity parts keep their
# upstream copyright lines and the MIT notice below; our own code here, like the rest
# of the project, is MIT.
#
# MIT licence (python-validity parts and our own code):
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Score a capture set with keypoint matchers: python-validity PR #288 and libfprint SIGFM.

pr288*  Port of validitysensor/hostmatch/matcher.py (MATCHER_VERSION 1) from
        https://github.com/uunicorn/python-validity/pull/288 (ashh87, 451cd11):
        2x bicubic upscale, CLAHE (clip 2, 4x8 tiles), SIFT keypoints inside the
        eroded foreground mask, Lowe ratio 0.8, RANSAC similarity
        (cv2.estimateAffinePartial2D, 6 px at 2x); a transform outside scale
        0.8-1.25 or beyond 30 degrees scores 0; a pair's score is the inlier
        count, min over both directions; accept at >= 6. Variants change one
        thing: the foreground-mask threshold (PR default 40 was measured on
        06cb:00a8; 25 fits 06cb:0078's lower contrast), no mask, no upscale, or
        ORB / AKAZE instead of SIFT.
sigfm*  Port of the SIGFM matcher proposed for libfprint in merge requests !418 /
        !530 (libfprint/sigfm/sigfm.cpp at bc98662, unmerged as of Oct 2026):
        plain SIFT on the image the driver hands over, ratio 0.75, matches
        de-duplicated by the integer y of the template keypoint (the std::set
        comparator only compares y; reproduced as is), then the count of pairs
        of length-consistent match pairs whose two angle terms agree within 5%.
        libfprint calls sigfm_match_score(template view, probe), so S[p, g] is
        directional. Upstream thresholds: 24 (elan in !530), 10 (fpcmoh in !570,
        which also upscales 2x nearest-neighbour).

The PR #288 capture-time quality gates (ridge coherence >= 0.45, foreground >=
25% at block std 40, >= 50 keypoints) are evaluated and reported per image but
do not remove images from the analysis. RANSAC is seeded (cv2.setRNGSeed(0)).

--overlap VARIANT runs a coverage diagnostic with a pr288-family variant: pairs
scoring >= --link (default 10, above every impostor score seen) are chained
into per-finger mosaics, which shows how many separate regions of each finger
the captures sampled and, for genuine pairs that did not match, how much their
touches are predicted to overlap.

Usage:
  sift_matcher.py CAPTURES [CAPTURES...] --out DIR [--variants all|v1,v2]
                  [--ks 3,5] [--overlap pr288-mask25] [--link 10]

Results (biometric-derived; keep DIR out of git):
  DIR/sift-<variant>.json, .analysis.json, .md, DIR/sift-summary.md,
  DIR/overlap-<variant>.json / .md
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fpdata  # noqa: E402
import fpscore  # noqa: E402
from preprocess import foreground_mask  # noqa: E402

# PR #288 constants (matcher.py / ops.py / quality.py)
ACCEPT_THRESHOLD = 6
RATIO = 0.8
RANSAC_REPROJ_PX = 6.0
MIN_SCALE, MAX_SCALE = 0.8, 1.25
MAX_ROTATION_DEG = 30.0
MASK_ERODE = 13  # at 2x
PR_MIN_FOREGROUND_STD = 40.0
MIN_RIDGE_COHERENCE = 0.45
MIN_FOREGROUND = 0.25
MIN_KEYPOINTS = 50

# SIGFM constants (sigfm.cpp)
SIGFM_DISTANCE = 0.75
SIGFM_LENGTH = 0.05
SIGFM_ANGLE = 0.05
SIGFM_MIN_MATCH = 5

PR_THRESHOLDS = [4, 5, 6, 7, 8, 10, 12, 15, 20]
SIGFM_THRESHOLDS = [5, 10, 15, 20, 24, 30, 40, 60, 100]


@dataclass
class Variant:
    scorer: str = "pr288"  # pr288 | sigfm
    detector: str = "sift"  # sift | orb | akaze
    prep: str = "pr288"  # pr288 (cubic + CLAHE) | raw | nn | linear
    mask_std: float | None = PR_MIN_FOREGROUND_STD  # None = no mask
    upscale: int = 2

    @property
    def thresholds(self):
        return PR_THRESHOLDS if self.scorer == "pr288" else SIGFM_THRESHOLDS

    @property
    def default_threshold(self):
        return ACCEPT_THRESHOLD if self.scorer == "pr288" else 24


VARIANTS: dict[str, Variant] = {
    "pr288": Variant(),
    "pr288-mask25": Variant(mask_std=25.0),
    "pr288-nomask": Variant(mask_std=None),
    "sift-1x-mask25": Variant(mask_std=25.0, upscale=1),
    "orb-mask25": Variant(detector="orb", mask_std=25.0),
    "akaze-mask25": Variant(detector="akaze", mask_std=25.0),
    "sigfm-1x": Variant(scorer="sigfm", prep="raw", mask_std=None, upscale=1),
    "sigfm-up2nn": Variant(scorer="sigfm", prep="nn", mask_std=None, upscale=2),
    "sigfm-up2linear": Variant(scorer="sigfm", prep="linear", mask_std=None, upscale=2),
}


def preprocess(img: np.ndarray, v: Variant):
    """Matcher input image and (optional) eroded foreground mask."""
    interp = {"pr288": cv2.INTER_CUBIC, "raw": cv2.INTER_CUBIC, "nn": cv2.INTER_NEAREST,
              "linear": cv2.INTER_LINEAR}[v.prep]
    im = img.copy() if v.upscale == 1 else cv2.resize(img, None, fx=v.upscale, fy=v.upscale,
                                                      interpolation=interp)
    if v.prep == "pr288":
        im = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 8)).apply(im)
    if v.mask_std is None:
        return im, None
    fg = np.kron(foreground_mask(img, min_std=v.mask_std),
                 np.ones((v.upscale, v.upscale), dtype=bool)).astype(np.uint8) * 255
    erode = max(3, int(round(MASK_ERODE * v.upscale / 2)) | 1)
    return im, cv2.erode(fg, np.ones((erode, erode), np.uint8))


def ridge_coherence(img: np.ndarray, block: int = 8) -> float:
    """PR #288 quality.ridge_coherence."""
    a = np.asarray(img, dtype=np.float64)
    gy, gx = (np.asarray(g) for g in np.gradient(a))
    h, w = a.shape
    h -= h % block
    w -= w % block

    def tiles(x):
        return x[:h, :w].reshape(h // block, block, w // block, block).sum(axis=(1, 3))

    xx, yy, xy = tiles(gx * gx), tiles(gy * gy), tiles(gx * gy)
    coh = np.sqrt((xx - yy) ** 2 + 4 * xy ** 2) / np.maximum(xx + yy, 1e-9)
    return float(coh.mean())


class Matcher:
    def __init__(self, v: Variant):
        self.v = v
        if v.detector == "sift":
            self.det, self.norm = cv2.SIFT_create(), cv2.NORM_L2
        elif v.detector == "orb":
            self.det, self.norm = cv2.ORB_create(nfeatures=1000, edgeThreshold=15, patchSize=15), cv2.NORM_HAMMING
        elif v.detector == "akaze":
            self.det, self.norm = cv2.AKAZE_create(threshold=0.0005), cv2.NORM_HAMMING
        else:
            raise ValueError(v.detector)
        # the PR's RANSAC tolerance is in 2x pixels
        self.reproj = RANSAC_REPROJ_PX * v.upscale / 2

    def extract(self, img):
        im, mask = preprocess(img, self.v)
        kps, desc = self.det.detectAndCompute(im, mask)
        pts = np.array([k.pt for k in kps], dtype=np.float32).reshape(-1, 2)
        return pts, desc

    # ---- PR #288
    def transform(self, a, b):
        """One-way ratio matches + RANSAC similarity: (inliers, 2x3 transform or None)."""
        p1, d1 = a
        p2, d2 = b
        if d1 is None or d2 is None or len(d1) < 2 or len(d2) < 2:
            return 0.0, None
        knn = cv2.BFMatcher(self.norm).knnMatch(d1, d2, k=2)
        good = [m for m, n in (x for x in knn if len(x) == 2) if m.distance < RATIO * n.distance]
        if len(good) < 3:
            return float(len(good)), None
        src = p1[[m.queryIdx for m in good]]
        dst = p2[[m.trainIdx for m in good]]
        t, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                             ransacReprojThreshold=self.reproj)
        if t is None or inl is None:
            return 0.0, None
        scale = float(np.hypot(t[0, 0], t[1, 0]))
        angle = float(np.degrees(np.arctan2(t[1, 0], t[0, 0])))
        if not MIN_SCALE <= scale <= MAX_SCALE or abs(angle) > MAX_ROTATION_DEG:
            return 0.0, None
        return float(np.count_nonzero(inl)), t

    def one_way(self, a, b) -> float:
        return self.transform(a, b)[0]

    # ---- SIGFM
    def sigfm(self, frame, enrolled) -> float:
        """sigfm_match_score(frame, enrolled), reproduced including its quirks."""
        p1, d1 = frame
        p2, d2 = enrolled
        if d1 is None or d2 is None or len(d1) == 0 or len(d2) == 0:
            return 0.0
        knn = cv2.BFMatcher(cv2.NORM_L2).knnMatch(d1, d2, k=2)
        unique: dict[int, tuple] = {}
        nb = 0
        for pair in knn:
            if len(pair) < 2:
                continue
            m, n = pair
            if m.distance < SIGFM_DISTANCE * n.distance:
                a = np.rint(p1[m.queryIdx]).astype(int)
                b = np.rint(p2[m.trainIdx]).astype(int)
                unique.setdefault(int(a[1]), (a, b))  # set ordered/deduped by p1.y only
                nb += 1
        if nb < SIGFM_MIN_MATCH:
            return 0.0
        ms = [unique[k] for k in sorted(unique)]
        P1 = np.array([m[0] for m in ms], dtype=np.float64)
        P2 = np.array([m[1] for m in ms], dtype=np.float64)
        j, k = np.triu_indices(len(ms), 1)
        v1 = P1[j] - P1[k]
        v2 = P2[j] - P2[k]
        l1 = np.hypot(v1[:, 0], v1[:, 1])
        l2 = np.hypot(v2[:, 0], v2[:, 1])
        with np.errstate(divide="ignore", invalid="ignore"):
            ok = (1 - np.minimum(l1, l2) / np.maximum(l1, l2)) <= SIGFM_LENGTH
            prod = l1[ok] * l2[ok]
            dot = (v1[ok, 0] * v2[ok, 0] + v1[ok, 1] * v2[ok, 1]) / prod
            cross = (v1[ok, 0] * v2[ok, 1] - v1[ok, 1] * v2[ok, 0]) / prod
            cosv = np.pi / 2 + np.arcsin(dot)  # NaN outside [-1, 1], as in C
            sinv = np.arccos(cross)
        n_ang = len(cosv)
        if n_ang < SIGFM_MIN_MATCH:
            return 0.0
        count = 0
        chunk = 2048
        with np.errstate(divide="ignore", invalid="ignore"):
            for s in range(0, n_ang, chunk):
                cs, ss = cosv[s:s + chunk, None], sinv[s:s + chunk, None]
                okc = (1 - np.minimum(cs, cosv[None]) / np.maximum(cs, cosv[None])) <= SIGFM_ANGLE
                oks = (1 - np.minimum(ss, sinv[None]) / np.maximum(ss, sinv[None])) <= SIGFM_ANGLE
                both = okc & oks
                rows = np.arange(s, min(s + chunk, n_ang))[:, None]
                count += int(np.count_nonzero(both & (np.arange(n_ang)[None] > rows)))
        return float(count)

    def pair(self, a, b) -> float:
        return min(self.one_way(a, b), self.one_way(b, a))


def score_matrix(m: Matcher, feats) -> np.ndarray:
    n = len(feats)
    S = np.zeros((n, n))
    if m.v.scorer == "pr288":
        for i in range(n):
            for j in range(i, n):
                S[i, j] = S[j, i] = m.pair(feats[i], feats[j])
    else:  # libfprint: sigfm_match_score(template view g, probe p)
        for p in range(n):
            for g in range(n):
                S[p, g] = m.sigfm(feats[g], feats[p])
    return S


def run_variant(samples, name: str, out: Path, ks) -> tuple[dict, dict]:
    v = VARIANTS[name]
    cv2.setRNGSeed(0)
    m = Matcher(v)
    t0 = time.monotonic()
    feats = [m.extract(s.image) for s in samples]
    t_extract = (time.monotonic() - t0) / len(samples)
    t0 = time.monotonic()
    S = score_matrix(m, feats)
    n = len(samples)
    t_pair = (time.monotonic() - t0) / (n * (n + 1) / 2 if v.scorer == "pr288" else n * n)
    gates = []
    for s, f in zip(samples, feats):
        coh = ridge_coherence(s.image)
        cov = float(foreground_mask(s.image, min_std=PR_MIN_FOREGROUND_STD).mean())
        gates.append({"name": s.name, "coherence": coh, "foreground_pr": cov, "keypoints": len(f[0]),
                      "pass": coh >= MIN_RIDGE_COHERENCE and cov >= MIN_FOREGROUND
                      and len(f[0]) >= MIN_KEYPOINTS})
    res = {"method": "keypoint matcher", "variant": name, "scorer": v.scorer, "detector": v.detector,
           "prep": v.prep, "mask_std": v.mask_std, "upscale": v.upscale, "opencv": cv2.__version__,
           "default_threshold": v.default_threshold,
           "names": [s.name for s in samples], "fingers": [s.finger for s in samples],
           "coverage": [s.coverage for s in samples], "matrix": S, "gates": gates,
           "ms_extract": 1000 * t_extract, "ms_pair": 1000 * t_pair}
    fpdata.save_json(out / f"sift-{name}.json", res)
    ana = fpscore.analyze(S, res["fingers"], v.thresholds, ks=ks,
                          symmetric=(v.scorer == "pr288"))
    ana["keypoints"] = fpscore.stats(np.array([g["keypoints"] for g in gates], float))
    ana["default_threshold"] = v.default_threshold
    fpdata.save_json(out / f"sift-{name}.analysis.json", ana)
    failed_gate = [g["name"] for g in gates if not g["pass"]]
    mask = "none" if v.mask_std is None else f"block std >= {v.mask_std:g}"
    extra = (f"Scorer {v.scorer}, detector {v.detector}, preprocessing {v.prep}, upscale x{v.upscale}, "
             f"mask {mask}; OpenCV {cv2.__version__}. "
             f"Keypoints per image: median {fpscore.fmt(ana['keypoints']['median'])} "
             f"(min {fpscore.fmt(ana['keypoints']['min'])}). "
             f"Images failing PR #288's capture gates: {len(failed_gate)}"
             + (f" ({', '.join(failed_gate)})" if failed_gate else "") + ". "
             f"Time: {res['ms_extract']:.1f} ms/extract, {res['ms_pair']:.2f} ms/comparison.")
    (out / f"sift-{name}.md").write_text(fpscore.markdown(f"Keypoint matcher: {name}", ana, extra))
    return res, ana


# ---------------------------------------------------------------- coverage diagnostic

def _h(t: np.ndarray) -> np.ndarray:
    return np.vstack([t, [0, 0, 1]])


def overlap_diagnostic(samples, name: str, out: Path, link: float, ks=(3, 5)) -> dict:
    v = VARIANTS[name]
    if v.scorer != "pr288":
        raise SystemExit("--overlap needs a pr288-family variant (it uses the RANSAC transforms)")
    cv2.setRNGSeed(0)
    m = Matcher(v)
    feats = [m.extract(s.image) for s in samples]
    up = v.upscale
    to2x, to1x = np.diag([up, up, 1.0]), np.diag([1 / up, 1 / up, 1.0])
    report = {"variant": name, "link_threshold": link, "fingers": {}}
    md = [f"### Coverage diagnostic ({name}, link score >= {fpscore.fmt(float(link))})", "",
          "| finger | images | linked regions (sizes) | genuine pairs | matched (>= 6) | same region, unmatched: "
          "predicted overlap median (max) | different regions |", "|---|---|---|---|---|---|---|"]
    for f in sorted({s.finger for s in samples}):
        idx = [i for i, s in enumerate(samples) if s.finger == f]
        n = len(idx)
        score = np.zeros((n, n))
        T: dict[tuple[int, int], np.ndarray] = {}
        for a in range(n):
            for b in range(a + 1, n):
                s_ab, t_ab = m.transform(feats[idx[a]], feats[idx[b]])
                s_ba, _ = m.transform(feats[idx[b]], feats[idx[a]])
                score[a, b] = score[b, a] = min(s_ab, s_ba)
                if score[a, b] >= link and t_ab is not None:
                    Ht = to1x @ _h(t_ab) @ to2x  # a's 1x pixels -> b's 1x pixels
                    T[(a, b)] = Ht
                    T[(b, a)] = np.linalg.inv(Ht)
        comp = [-1] * n
        pose: dict[int, np.ndarray] = {}
        regions = []
        for r in range(n):
            if comp[r] >= 0:
                continue
            comp[r] = len(regions)
            pose[r] = np.eye(3)
            members, q = [r], deque([r])
            while q:
                a = q.popleft()
                for b in range(n):
                    if comp[b] < 0 and (a, b) in T:
                        comp[b] = comp[r]
                        pose[b] = pose[a] @ T[(b, a)]  # b -> a -> root
                        members.append(b)
                        q.append(b)
            regions.append(members)
        # rasterise each touch's foreground into its region's frame
        masks = {}
        for r, members in enumerate(regions):
            corners = []
            for a in members:
                h, w = samples[idx[a]].image.shape
                c = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]], float).T
                corners.append((pose[a] @ c)[:2].T)
            c = np.vstack(corners)
            off = np.floor(c.min(axis=0)) - 2
            size = (np.ceil(c.max(axis=0) - off) + 2).astype(int)
            shift = np.array([[1, 0, -off[0]], [0, 1, -off[1]], [0, 0, 1]])
            for a in members:
                fg = foreground_mask(samples[idx[a]].image, min_std=25.0).astype(np.uint8)
                masks[a] = cv2.warpAffine(fg, (shift @ pose[a])[:2], (int(size[0]), int(size[1])),
                                          flags=cv2.INTER_NEAREST) > 0
            union = np.zeros(masks[members[0]].shape, bool)
            for a in members:
                union |= masks[a]
            regions[r] = {"members": [samples[idx[a]].name for a in members],
                          "union_mm2": float(union.sum()) * 0.05 ** 2}  # ~508 dpi: 0.05 mm/px
        pairs = []
        for a in range(n):
            for b in range(a + 1, n):
                row = {"a": samples[idx[a]].name, "b": samples[idx[b]].name, "score": float(score[a, b]),
                       "same_region": comp[a] == comp[b]}
                if comp[a] == comp[b]:
                    inter = np.logical_and(masks[a], masks[b]).sum()
                    row["predicted_overlap"] = float(inter / max(1, min(masks[a].sum(), masks[b].sum())))
                pairs.append(row)
        unmatched_same = [p["predicted_overlap"] for p in pairs
                          if p["same_region"] and p["score"] < ACCEPT_THRESHOLD]
        diff = [p for p in pairs if not p["same_region"]]
        report["fingers"][f] = {"regions": regions, "pairs": pairs}
        sizes = ", ".join(str(len(r["members"])) for r in regions)
        um = (f"{len(unmatched_same)}: {100 * np.median(unmatched_same):.0f}% ({100 * max(unmatched_same):.0f}%)"
              if unmatched_same else "0")
        md.append(f"| {f} | {n} | {len(regions)} ({sizes}) | {len(pairs)} | "
                  f"{sum(p['score'] >= ACCEPT_THRESHOLD for p in pairs)} | {um} | {len(diff)} |")
    # Coverage ceiling: a probe can only match if an enrolled touch lies in its
    # linked region. Scoring same-region pairs 1 and all others 0 gives the FRR
    # that a matcher which never misses a linked pair would still have.
    region_of = {}
    for f, d in report["fingers"].items():
        for r, reg in enumerate(d["regions"]):
            for nm in reg["members"]:
                region_of[nm] = (f, r)
    names = [s.name for s in samples]
    same = np.array([[1.0 if region_of[a] == region_of[b] else 0.0 for b in names] for a in names])
    report["ceiling"] = {}
    md += ["", "Coverage ceiling (FRR of a matcher that matches every pair within a linked region "
           "and nothing else):", "", "| enrolled touches | FRR | per finger |", "|---|---|---|"]
    for k in ks:
        e = fpscore.enroll_sim(same, [s.finger for s in samples], k, [1])
        if not e["genuine"].get("n"):
            continue
        per = e["per_finger_frr_at_zero_fa"]
        report["ceiling"][str(k)] = {"frr": e["at"][0]["frr"], "per_finger": per}
        md.append(f"| {k} | {fpscore.pct(e['at'][0]['frr'])} | "
                  + ", ".join(f"{f} {fpscore.pct(v)}" for f, v in per.items()) + " |")
    fpdata.save_json(out / f"overlap-{name}.json", report)
    (out / f"overlap-{name}.md").write_text("\n".join(md) + "\n")
    return report


def summary_rows(results) -> str:
    lines = ["| variant | keypoints (median) | genuine median | impostor max | pair FRR @0FA | pair FRR / FAR @default | "
             "enroll-5 FRR @0FA (thr) | enroll-5 FRR / FAR @default |",
             "|---|---|---|---|---|---|---|---|"]
    for v, res, ana in results:
        d = ana["default_threshold"]
        at = {a["threshold"]: a for a in ana["at"]}
        e5 = ana["enroll"].get("5", {})
        e5at = {a["threshold"]: a for a in e5.get("at", [])}
        z5 = e5.get("zero_fa", {})
        lines.append(
            f"| {v} | {fpscore.fmt(ana['keypoints']['median'])} | {fpscore.fmt(ana['genuine']['median'])} | "
            f"{fpscore.fmt(ana['impostor']['max'])} | {fpscore.pct(ana['zero_fa']['frr'])} | "
            f"{fpscore.pct(at[d]['frr'])} / {fpscore.pct(at[d]['far'])} (@{d}) | "
            f"{fpscore.pct(z5.get('frr', float('nan')))} ({fpscore.fmt(z5.get('threshold', float('nan')))}) | "
            f"{fpscore.pct(e5at.get(d, {}).get('frr', float('nan')))} / "
            f"{fpscore.pct(e5at.get(d, {}).get('far', float('nan')))} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="+", help="finger folder(s) or their parent folder")
    ap.add_argument("--out", required=True, help="results directory (outside git, or git-ignored)")
    ap.add_argument("--variants", default="all")
    ap.add_argument("--ks", default="3,5")
    ap.add_argument("--overlap", help="run the coverage diagnostic with this pr288-family variant")
    ap.add_argument("--link", type=float, default=10.0)
    ap.add_argument("--allow-tracked", action="store_true")
    args = ap.parse_args()
    out = fpdata.check_out_dir(args.out, args.allow_tracked)
    samples = fpdata.load_captures(args.captures)
    ks = tuple(int(k) for k in args.ks.split(","))
    names = [] if args.variants == "none" else (list(VARIANTS) if args.variants == "all" else args.variants.split(","))
    results = []
    for name in names:
        if name not in VARIANTS:
            raise SystemExit(f"unknown variant {name}; known: {', '.join(VARIANTS)}")
        res, ana = run_variant(samples, name, out, ks)
        results.append((name, res, ana))
        print(f"{name}: done", flush=True)
    if results:
        table = summary_rows(results)
        (out / "sift-summary.md").write_text(table)
        print(table)
    if args.overlap:
        overlap_diagnostic(samples, args.overlap, out, args.link, ks)
        print((out / f"overlap-{args.overlap}.md").read_text())


if __name__ == "__main__":
    main()
