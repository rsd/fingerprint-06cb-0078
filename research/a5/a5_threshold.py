#!/usr/bin/env python3
"""A5: SIGFM score threshold, enrollment stage count and upscale for synaptics0078.

Scores the capture set with the fork's compiled SIGFM through the harness
research/a5/sigfm_matrix.cpp (built by build_harness.sh), which mirrors the
driver path: nearest-neighbour upscale by 1 or 2, sigfm_extract(), the
25-keypoint gate of fp-image.c, and sigfm_match_score(template, probe) as
fpi_print_sigfm_match() calls it. libfprint verifies with an OR over the
enrolled stages: a probe matches at the first stage scoring >= threshold.

Per scale this reports, on the observed sample only:
  * keypoints per image and gate rejections;
  * genuine / impostor ordered-pair distributions (fpscore);
  * an enrollment simulation for k = 1..9 enrolled images per finger (every
    k-subset of every finger, fpscore.enroll_sim), FRR / FAR at candidate
    thresholds, and a cautious extrapolation toward more stages (below);
  * the impostor side under the OR rule: the highest impostor ordered-pair
    score is what any gallery size can reach, plus every finger enrolled with
    all its images against all other fingers' images;
  * extraction and comparison times, and the serialized size of one stage;
  * the unlabeled cross-session probes (--a4): max score against each
    finger's whole gallery, flagged when one clears the threshold for two
    fingers.

Extrapolation toward k > 9 (with-replacement model): for each probe p of a
finger with n images, f_p = (number of the other n - 1 images g with
S[p, g] >= t) / (n - 1). If enrollment touches were independent draws from
the same population as the capture set, a probe is missed by k touches with
probability (1 - f_p) ** k, and FRR(k) is the mean over probes. A probe that
matches no other image stays rejected for every k: that floor is a property of
the sample (10 touches per finger), not of the sensor, so the extrapolation is
optimistic about f_p for well-covered probes and pessimistic for the floor.

--export-smoke DIR writes a tests/benchmark-matching.py --smoke fixture (a
manifest.tsv plus upscaled PGMs) whose expected scores come from the harness,
so libfprint's own virtual_image path can confirm them byte for byte.

Everything written (matrices, fixtures, reports) is biometric-derived and must
stay out of git: fpdata.check_out_dir refuses a tracked output directory.

Usage:
  a5_threshold.py proto/captures --a4 local/captures/a4-*.pgm \\
      --harness SCRATCH/bin/sigfm_matrix --out SCRATCH/out \\
      [--export-smoke SCRATCH/smoke] > SCRATCH/out/report.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "matcher"))
import fpdata
import fpscore

# Values compiled into libfprint/drivers/synaptics0078.c (A5 decision).
CHOSEN_SCALE = 2
CHOSEN_THRESHOLD = 105
CHOSEN_STAGES = 18

THRESHOLDS = [1, 7, 15, 24, 45, 64, 105, 138, 210, 378, 1000]
KS = tuple(range(1, 10))
EXTRAPOLATE_KS = (9, 12, 15, 18, 24)


# ---------------------------------------------------------------- score scale

def matches_score(m: float) -> float:
    """SIGFM score of m correspondences that all agree with one rigid motion.

    Every pair of such matches is length-consistent, giving C(m, 2) "angles",
    and every pair of angles agrees, so the score is C(C(m, 2), 2). Below 5
    angles (m < 4) sigfm.cpp returns 0; the formula is the continuous
    extension used to express scores as match counts.
    """
    a = m * (m - 1) / 2
    return a * (a - 1) / 2


def score_matches(s: float) -> float:
    """Inverse of matches_score: the match-equivalent m of a score (m >= 1)."""
    if s <= 0:
        return 0.0
    a = (1 + math.sqrt(1 + 8 * s)) / 2
    return (1 + math.sqrt(1 + 8 * a)) / 2


def margin_threshold(max_impostor: float, extra_matches: float, whole: bool = True) -> int:
    """The A5 margin rule: the score of the smallest number of fully consistent
    matches (a whole number when `whole`) that is at least `extra_matches` above
    the match-equivalent of the strongest impostor score; never at or below that
    score itself."""
    m = score_matches(max_impostor) + extra_matches
    if whole:
        m = math.ceil(m - 1e-9)
    return max(math.ceil(matches_score(m) - 1e-9), math.floor(max_impostor) + 1)


# ---------------------------------------------------------------- analysis

def frr_by_k(S: np.ndarray, fingers: list[str], ks, thresholds, max_splits=None) -> dict:
    """fpscore.enroll_sim per k: {k: {"genuine_n", "impostor_n", "splits", "frr": {t},
    "far": {t}, "false_accepts": {t}, "max_impostor"}}."""
    out = {}
    for k in ks:
        e = fpscore.enroll_sim(S, fingers, k, list(thresholds), max_splits)
        if not e["genuine"].get("n"):
            continue
        out[k] = {"genuine_n": e["genuine"]["n"], "impostor_n": e["impostor"]["n"],
                  "splits": e["splits"],
                  "frr": {a["threshold"]: a["frr"] for a in e["at"]},
                  "far": {a["threshold"]: a["far"] for a in e["at"]},
                  "false_accepts": {a["threshold"]: a["false_accepts"] for a in e["at"]},
                  "max_impostor": e["impostor"]["max"],
                  "worst_split_frr": {float(t): w["worst_split_frr"] for t, w in e["per_split"].items()}}
    return out


def extrapolate_frr(S: np.ndarray, fingers: list[str], t: float, ks) -> dict:
    """With-replacement extrapolation (module docstring): {"floor", "frr": {k}}."""
    S, _ = fpscore.clean(S)
    fs = []
    for p, fp in enumerate(fingers):
        others = [g for g, fg in enumerate(fingers) if fg == fp and g != p]
        if not others:
            continue
        fs.append(float(np.mean([S[p, g] >= t for g in others])))
    fs = np.array(fs)
    if not len(fs):
        return {"floor": float("nan"), "frr": {k: float("nan") for k in ks}, "probes": 0}
    return {"floor": float(np.mean(fs == 0)), "probes": len(fs),
            "frr": {k: float(np.mean((1 - fs) ** k)) for k in ks}}


def full_gallery_impostor(S: np.ndarray, fingers: list[str]) -> dict:
    """Each finger enrolled with all its images; every other finger's image as probe.
    Returns the max over probes of the OR-rule score, overall and per gallery."""
    S, _ = fpscore.clean(S)
    per = {}
    for f in sorted(set(fingers)):
        gal = [i for i, x in enumerate(fingers) if x == f]
        probes = [i for i, x in enumerate(fingers) if x != f]
        per[f] = float(max(S[p, gal].max() for p in probes)) if probes else float("nan")
    return {"max": max(per.values()) if per else float("nan"), "per_gallery": per}


def probe_vs_galleries(S: np.ndarray, probe_idx: list[int], fingers: list[str], t: float) -> list[dict]:
    """Unlabeled probes: max score (OR rule) against each finger's whole gallery.
    S may contain extra rows/columns; gallery columns are the indices of `fingers`."""
    Sc, failed = fpscore.clean(S)
    names = sorted(set(fingers))
    rows = []
    for p in probe_idx:
        best = {f: float(Sc[p, [g for g, x in enumerate(fingers) if x == f]].max()) for f in names}
        cleared = [f for f in names if best[f] >= t]
        rows.append({"index": p, "rejected": bool(failed[p].all()), "best": best,
                     "cleared": cleared, "flag": len(cleared) >= 2})
    return rows


def timing_summary(match_ms: np.ndarray, S: np.ndarray, n_set: int) -> dict:
    """Comparison times over the labeled set, off-diagonal, split by score 0 / > 0."""
    zero, nonzero, diag = [], [], []
    for p in range(n_set):
        for g in range(n_set):
            ms = match_ms[p, g]
            if np.isnan(ms):
                continue
            if p == g:
                diag.append(ms)
            elif S[p, g] > 0:
                nonzero.append(ms)
            else:
                zero.append(ms)

    def st(x):
        x = np.array(x)
        if not len(x):
            return {"n": 0}
        return {"n": len(x), "median": float(np.median(x)), "p95": float(np.percentile(x, 95)),
                "max": float(x.max())}
    return {"score0": st(zero), "score_pos": st(nonzero), "self": st(diag)}


def verify_latency(extract_ms: dict, timing: dict, stages: int) -> dict:
    """Verify time model: one extraction, then the OR loop over `stages`.
    Impostor or miss: every stage compared, all at score-0 cost.
    Genuine worst case: stages - 1 misses, then one matching comparison."""
    miss = timing["score0"]
    hit = timing["score_pos"]
    return {
        "stages": stages,
        "reject_median_ms": extract_ms["median"] + stages * miss["median"],
        "reject_p95_ms": extract_ms["p95"] + stages * miss["p95"],
        "accept_worst_ms": extract_ms["max"] + (stages - 1) * miss["max"] + hit.get("max", 0.0),
    }


def print_size(serialized: list[int], stages: int) -> dict:
    """Stored FpPrint estimate: one sigfm_serialize_binary() blob per stage.
    fp_print_serialize adds the GVariant framing and the print metadata, a few
    hundred bytes; not counted."""
    s = np.array(serialized, dtype=float)
    return {"stages": stages, "per_stage_median": float(np.median(s)), "per_stage_max": float(s.max()),
            "print_median": float(np.median(s) * stages), "print_max": float(s.max() * stages)}


# ---------------------------------------------------------------- harness and export

def run_harness(harness: str, paths: list[Path], scale: int, out: Path, threads1: bool) -> dict:
    env = dict(os.environ)
    if threads1:
        env["OPENCV_FOR_THREADS_NUM"] = "1"
    else:
        env.pop("OPENCV_FOR_THREADS_NUM", None)
    proc = subprocess.run([harness, "--scale", str(scale), "--roundtrip", "--out", str(out)]
                          + [str(p) for p in paths], env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"{harness} exited {proc.returncode}: {proc.stderr.strip()}")
    return json.loads(out.read_text())


def matrix(res: dict, key: str) -> np.ndarray:
    return np.array([[np.nan if v is None else v for v in row] for row in res[key]], dtype=float)


def upscale(img: np.ndarray, scale: int) -> np.ndarray:
    """build_image() in synaptics0078.c: dst[y][x] = src[y / scale][x / scale]."""
    return np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)


SMOKE_COLUMNS = ["trial_id", "kind", "dataset", "probe_finger", "gallery_finger", "stages",
                 "probe_sha256", "gallery_sha256s", "expected_scores", "expected_best",
                 "window", "scale", "seed"]


def smoke_trials(S: np.ndarray, fingers: list[str], stages: int) -> list[dict]:
    """Deterministic replay subset: per finger, the first `stages` images form the
    gallery; genuine probes are that finger's other images with the highest and the
    lowest best score, impostor probes the two other-finger images with the highest."""
    Sc, _ = fpscore.clean(S)
    trials = []
    for f in sorted(set(fingers)):
        idx = [i for i, x in enumerate(fingers) if x == f]
        gal = idx[:stages]
        gen = sorted((p for p in idx if p not in gal), key=lambda p: (Sc[p, gal].max(), p))
        imp = sorted((p for p, x in enumerate(fingers) if x != f), key=lambda p: (-Sc[p, gal].max(), p))
        picks = [("genuine", p) for p in gen[-1:] + gen[:1]] + [("impostor", p) for p in imp[:2]]
        seen = set()
        for kind, p in picks:
            if p in seen:
                continue
            seen.add(p)
            trials.append({"kind": kind, "probe": p, "gallery": gal, "finger": f,
                           "scores": [int(Sc[p, g]) for g in gal]})
    return trials


def write_pgm(path: Path, img: np.ndarray) -> str:
    h, w = img.shape
    payload = np.ascontiguousarray(img, dtype=np.uint8).tobytes()
    if path.exists():
        path.unlink()
    path.write_bytes(b"P5\n%d %d\n255\n" % (w, h) + payload)
    return hashlib.sha256(payload).hexdigest()


def export_smoke(out: Path, samples, S: np.ndarray, scale: int, stages: int) -> int:
    out = fpdata.check_out_dir(out)
    fingers = [s.finger for s in samples]
    rows = ["\t".join(SMOKE_COLUMNS)]
    trials = smoke_trials(S, fingers, stages)
    for tid, t in enumerate(trials, start=1):
        probe = samples[t["probe"]]
        h, w = probe.image.shape
        psha = write_pgm(out / f"trial{tid}_probe.pgm", upscale(probe.image, scale))
        gshas = [write_pgm(out / f"trial{tid}_g{i}.pgm", upscale(samples[g].image, scale))
                 for i, g in enumerate(t["gallery"])]
        pf = probe.finger
        rows.append("\t".join(str(x) for x in (
            tid, t["kind"], "a5", pf, t["finger"], stages, psha, ",".join(gshas),
            ",".join(str(s) for s in t["scores"]), max(t["scores"]), f"{w}x{h}", scale, 0)))
    (out / "manifest.tsv").write_text("\n".join(rows) + "\n")
    return len(trials)


# ---------------------------------------------------------------- report

def fmt(x) -> str:
    if isinstance(x, float) and math.isnan(x):
        return "n/a"
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return f"{x:.3g}" if isinstance(x, float) else str(x)


def pct(x) -> str:
    return fpscore.pct(x)


def scale_report(scale: int, res: dict, res1: dict, samples, a4_paths, out_lines: list[str]) -> dict:
    n = len(samples)
    fingers = [s.finger for s in samples]
    S_all = matrix(res, "scores")
    S = S_all[:n, :n]
    imgs = res["images"]
    kp = np.array([i["keypoints"] for i in imgs[:n]])
    rejected = [Path(i["path"]).stem for i in imgs if i["rejected"]]
    gen, imp, _, _, _ = fpscore.pair_scores(S, fingers, symmetric=False)
    gs, is_ = fpscore.stats(gen), fpscore.stats(imp)
    S1 = matrix(res1, "scores")
    deterministic = bool(np.array_equal(np.nan_to_num(S_all, nan=-1), np.nan_to_num(S1, nan=-1)))
    L = out_lines
    L += [f"## Scale {scale}x ({imgs[0]['width']}x{imgs[0]['height']})", ""]
    L += [f"Keypoints (labeled set, n={n}): min {kp.min()}, median {fmt(float(np.median(kp)))}, max {kp.max()}; "
          f"gate rejections (< 25 keypoints, all {len(imgs)} images): {len(rejected)}"
          + (f" ({', '.join(rejected)})" if rejected else "") + ".",
          (f"Serialize/deserialize round trip: {res['roundtrip']['differ']} of {res['roundtrip']['checked']} "
           f"comparisons differ. Scores identical with OPENCV_FOR_THREADS_NUM=1: {deterministic}."), ""]
    L += ["Single ordered pairs:", "", "| | n | min | p25 | median | p75 | p95 | max | > 0 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for lab, d, arr in (("genuine", gs, gen), ("impostor", is_, imp)):
        L.append(f"| {lab} | {d['n']} | " + " | ".join(fmt(d[k]) for k in ("min", "p25", "median", "p75", "p95", "max"))
                 + f" | {int(np.sum(arr > 0))} |")
    nz = np.sort(gen[gen > 0])
    L += ["", f"Lowest non-zero genuine scores: {', '.join(fmt(float(x)) for x in nz[:8])}.", "",
          "| threshold | match-equiv. | genuine pairs rejected | impostor pairs accepted |", "|---|---|---|---|"]
    for t in THRESHOLDS:
        r = fpscore.rates_at(gen, imp, t)
        L.append(f"| {t} | {score_matches(t):.2f} | {pct(r['frr'])} ({r['false_rejects']}) | "
                 f"{pct(r['far'])} ({r['false_accepts']}) |")
    L += ["", "Histogram (ordered pairs):", "", "| score | genuine | impostor |", "|---|---|---|"]
    for r in fpscore.histogram(gen, imp):
        L.append(f"| {r['bin']} | {r['genuine']} | {r['impostor']} |")

    ek = frr_by_k(S, fingers, KS, THRESHOLDS)
    L += ["", "Enrollment simulation, FRR (every k-subset of every finger; probes = the finger's other images):", "",
          "| k | splits | genuine / impostor attempts | " + " | ".join(f"@{t}" for t in THRESHOLDS) + " |",
          "|---|---|---|" + "---|" * len(THRESHOLDS)]
    for k, e in ek.items():
        L.append(f"| {k} | {sum(e['splits'].values())} | {e['genuine_n']} / {e['impostor_n']} | "
                 + " | ".join(pct(e["frr"][t]) for t in THRESHOLDS) + " |")
    L += ["", "False accepts in the simulation (impostor attempts at or above the threshold, all k): "
          + ", ".join(f"@{t}: {sum(e['false_accepts'][t] for e in ek.values())}" for t in THRESHOLDS) + ".",
          "Worst split FRR at k = 9: "
          + ", ".join(f"@{t}: {pct(ek[9]['worst_split_frr'][float(t)])}" for t in THRESHOLDS if 9 in ek) + "."]
    fg = full_gallery_impostor(S, fingers)
    L += ["", f"OR rule, each finger enrolled with all its images, every other finger's image as probe: "
          f"highest impostor score {fmt(fg['max'])} (" + ", ".join(f"{f}: {fmt(v)}" for f, v in fg["per_gallery"].items())
          + ")."]
    L += ["", "Extrapolation toward more stages (with-replacement model, see the script docstring):", "",
          "| threshold | probes never matched (floor) | " + " | ".join(f"k={k}" for k in EXTRAPOLATE_KS) + " |",
          "|---|---|" + "---|" * len(EXTRAPOLATE_KS)]
    ex = {}
    for t in THRESHOLDS:
        x = extrapolate_frr(S, fingers, t, EXTRAPOLATE_KS)
        ex[t] = x
        L.append(f"| {t} | {pct(x['floor'])} | " + " | ".join(pct(x["frr"][k]) for k in EXTRAPOLATE_KS) + " |")

    # Timing: default threads (res) and one thread (res1).
    tim = {}
    for lab, r in (("all cores", res), ("1 thread", res1)):
        em = np.array([i["extract_ms"] for i in r["images"]])
        ext = {"median": float(np.median(em)), "p95": float(np.percentile(em, 95)), "max": float(em.max())}
        tm = timing_summary(matrix(r, "match_ms"), S_all, n)
        tim[lab] = (ext, tm)
    L += ["", "Timing (ms; comparisons over the labeled set, off-diagonal unless 'self'):", "",
          ("| OpenCV threads | extract median / p95 / max | compare, score 0: median / p95 / max | "
           "compare, score > 0: median / p95 / max | self-compare median |"), "|---|---|---|---|---|"]
    for lab, (ext, tm) in tim.items():
        z, p, s = tm["score0"], tm["score_pos"], tm["self"]
        L.append(f"| {lab} | {ext['median']:.1f} / {ext['p95']:.1f} / {ext['max']:.1f} | "
                 f"{z['median']:.2f} / {z['p95']:.2f} / {z['max']:.2f} | "
                 f"{p['median']:.1f} / {p['p95']:.1f} / {p['max']:.1f} | {s['median']:.0f} |")
    L += ["", "| OpenCV threads | stages | reject (median) | reject (p95) | accept, worst case |", "|---|---|---|---|---|"]
    lat = {}
    for lab, (ext, tm) in tim.items():
        for st in sorted({CHOSEN_STAGES, 18}):
            v = verify_latency(ext, tm, st)
            lat[(lab, st)] = v
            L.append(f"| {lab} | {st} | {v['reject_median_ms']:.0f} ms | {v['reject_p95_ms']:.0f} ms | "
                     f"{v['accept_worst_ms']:.0f} ms |")
    ser = [i["serialized_bytes"] for i in imgs[:n]]
    ps = print_size(ser, CHOSEN_STAGES)
    L += ["", (f"Serialized stage (sigfm_serialize_binary): median {ps['per_stage_median'] / 1024:.0f} KiB, "
               f"max {ps['per_stage_max'] / 1024:.0f} KiB; print of {CHOSEN_STAGES} stages: "
               f"~{ps['print_median'] / 1048576:.2f} MiB (median), up to {ps['print_max'] / 1048576:.2f} MiB.")]

    if a4_paths:
        rows = probe_vs_galleries(S_all, list(range(n, n + len(a4_paths))), fingers, CHOSEN_THRESHOLD)
        names = sorted(set(fingers))
        L += ["", ("Cross-session probes (A4 driver captures, finger not recorded; not counted in FAR/FRR): "
                   f"best score against each finger's whole gallery; flag = clears {CHOSEN_THRESHOLD} for two "
                   "fingers."), "",
              "| probe | keypoints | " + " | ".join(names) + " | clears | flag |", "|---|---|" + "---|" * len(names) + "---|---|"]
        for r in rows:
            L.append(f"| A4 #{r['index'] - n + 1} | {imgs[r['index']]['keypoints']} | "
                     + " | ".join(fmt(r["best"][f]) for f in names)
                     + f" | {', '.join(r['cleared']) or '-'} | {'YES' if r['flag'] else 'no'} |")
    L.append("")
    return {"scale": scale, "max_impostor": is_["max"], "ek": ek, "extrapolation": ex, "rejected": rejected,
            "keypoints": kp.tolist(), "latency": {f"{k[0]}|{k[1]}": v for k, v in lat.items()},
            "print_size": ps, "full_gallery_impostor": fg, "deterministic": deterministic}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("captures", nargs="+", help="proto/captures or finger folders (manifest.json)")
    ap.add_argument("--a4", nargs="*", default=[], help="unlabeled cross-session probe PGMs (native 144x56)")
    ap.add_argument("--harness", required=True, help="sigfm_matrix binary (build_harness.sh)")
    ap.add_argument("--out", required=True, help="output dir, outside git or git-ignored")
    ap.add_argument("--scales", default="1,2")
    ap.add_argument("--export-smoke", metavar="DIR",
                    help="also write a benchmark-matching.py --smoke fixture per scale (DIR/<scale>x)")
    ap.add_argument("--smoke-stages", type=int, default=3)
    args = ap.parse_args(argv)

    out = fpdata.check_out_dir(args.out)
    samples = fpdata.load_captures(args.captures)
    a4 = sorted(Path(p) for p in args.a4)
    paths = [s.path for s in samples] + a4
    lines = ["# A5 threshold analysis (generated by research/a5/a5_threshold.py; biometric-derived, keep out of git)",
             "", f"Labeled set: {fpdata.describe(samples)}; cross-session probes: {len(a4)}.",
             (f"Chosen values in the script: scale {CHOSEN_SCALE}x, threshold {CHOSEN_THRESHOLD}, "
              f"stages {CHOSEN_STAGES}."), "",
             "Match-equivalents (score of m fully consistent correspondences, C(C(m,2),2)): "
             + ", ".join(f"m={m}: {fmt(matches_score(m))}" for m in range(4, 10)) + ".", ""]
    summary = {}
    for scale in (int(x) for x in args.scales.split(",")):
        res = run_harness(args.harness, paths, scale, out / f"matrix-{scale}x.json", threads1=False)
        res1 = run_harness(args.harness, paths, scale, out / f"matrix-{scale}x-1thread.json", threads1=True)
        summary[scale] = scale_report(scale, res, res1, samples, a4, lines)
        if args.export_smoke:
            n = len(samples)
            k = export_smoke(Path(args.export_smoke) / f"{scale}x", samples, matrix(res, "scores")[:n, :n],
                             scale, args.smoke_stages)
            lines += [f"Smoke fixture: {k} trials in {Path(args.export_smoke) / f'{scale}x'}.", ""]
    imp_all = max(s["max_impostor"] for s in summary.values())
    lines += ["## Margin rule", "",
              (f"Highest impostor ordered-pair score over all scales: {fmt(imp_all)} "
               f"(match-equivalent {score_matches(imp_all):.2f})."),
              "Thresholds this many consistent matches above it (exact / rounded up to whole matches): "
              + ", ".join(f"+{d}: {margin_threshold(imp_all, d, whole=False)} / {margin_threshold(imp_all, d)}"
                          for d in (1, 2, 3)) + ".", ""]
    fpdata.save_json(out / "summary.json", {str(k): v for k, v in summary.items()})
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
