"""Score-matrix analysis shared by the matcher evaluation scripts.

Every matcher run produces one square matrix S over the capture set, where
S[p, g] is the score of probe image p against a template made from gallery
image g (higher = more similar). A matrix that is not symmetric (bozorth3 run
through libfprint is directional: probe vs template) is analysed over ordered
pairs; a symmetric one (the PR #288 SIFT score is min of both directions) over
unordered pairs. NaN marks a failed extraction (libfprint retry, no keypoints);
it is scored as 0, i.e. a reject, and counted separately as a failure to acquire.

Metrics, all on the observed sample only:
  * pairwise genuine (same finger) and impostor (different finger) distributions;
  * FRR at zero observed false accepts: the threshold is set just above the
    highest impostor score seen, so with few impostor attempts this is an
    optimistic operating point, not a measured FAR;
  * FRR / FAR at fixed thresholds (the matcher's shipped default and others);
  * an enrollment simulation, as libfprint and PR #288 both do it: a finger is
    enrolled from k of its images, a probe's score is the max over the k
    (libfprint's fpi_print_bz3_match accepts if any enrolled view reaches the
    threshold, which is the same decision), and every k-subset of every finger
    is tried (or a seeded random sample of them when there are too many).
"""

from __future__ import annotations

import itertools
import math
import random

import numpy as np


def clean(S: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    S = np.asarray(S, dtype=float)
    failed = np.isnan(S)
    return np.where(failed, 0.0, S), failed


def is_symmetric(S: np.ndarray) -> bool:
    S = np.nan_to_num(np.asarray(S, dtype=float), nan=-1.0)
    return bool(np.allclose(S, S.T))


def pair_scores(S: np.ndarray, fingers: list[str], symmetric: bool | None = None):
    """Genuine and impostor score arrays, plus the (probe, gallery) index pairs."""
    S, _ = clean(S)
    if symmetric is None:
        symmetric = is_symmetric(S)
    n = len(fingers)
    gen, imp, gp, ip = [], [], [], []
    for p in range(n):
        for g in range(n):
            if p == g or (symmetric and g < p):
                continue
            (gen if fingers[p] == fingers[g] else imp).append(S[p, g])
            (gp if fingers[p] == fingers[g] else ip).append((p, g))
    return np.array(gen), np.array(imp), gp, ip, symmetric


def is_integer_valued(*arrays: np.ndarray) -> bool:
    return all(np.allclose(a, np.round(a)) for a in arrays if len(a))


def zero_fa(gen: np.ndarray, imp: np.ndarray) -> dict:
    """Lowest threshold with no observed false accept, and the FRR there."""
    max_imp = float(imp.max()) if len(imp) else 0.0
    if is_integer_valued(gen, imp):
        t0 = max_imp + 1
    else:
        t0 = float(np.nextafter(max_imp, np.inf))
    frr = float(np.mean(gen < t0)) if len(gen) else float("nan")
    return {"threshold": t0, "max_impostor": max_imp, "frr": frr,
            "false_rejects": int(np.sum(gen < t0)), "genuine_attempts": int(len(gen))}


def rates_at(gen: np.ndarray, imp: np.ndarray, t: float) -> dict:
    return {"threshold": t,
            "frr": float(np.mean(gen < t)) if len(gen) else float("nan"),
            "far": float(np.mean(imp >= t)) if len(imp) else float("nan"),
            "false_rejects": int(np.sum(gen < t)), "false_accepts": int(np.sum(imp >= t))}


def eer(gen: np.ndarray, imp: np.ndarray) -> dict:
    """Equal error rate over all candidate thresholds (accept if score >= t)."""
    if not len(gen) or not len(imp):
        return {"eer": float("nan"), "threshold": float("nan")}
    cands = np.unique(np.concatenate([gen, imp, [gen.max() + 1, imp.max() + 1]]))
    best = None
    for t in cands:
        frr, far = np.mean(gen < t), np.mean(imp >= t)
        gap = abs(frr - far)
        if best is None or gap < best[0]:
            best = (gap, (frr + far) / 2, float(t))
    return {"eer": float(best[1]), "threshold": best[2]}


def stats(x: np.ndarray) -> dict:
    if not len(x):
        return {"n": 0}
    q = np.percentile(x, [0, 5, 25, 50, 75, 95, 100])
    return {"n": int(len(x)), "min": float(q[0]), "p05": float(q[1]), "p25": float(q[2]),
            "median": float(q[3]), "p75": float(q[4]), "p95": float(q[5]), "max": float(q[6]),
            "mean": float(np.mean(x))}


def histogram(gen: np.ndarray, imp: np.ndarray, max_bins: int = 16) -> list[dict]:
    """Shared bins over both distributions: one per value for small integer ranges."""
    allv = np.concatenate([gen, imp]) if len(gen) + len(imp) else np.array([0.0])
    top = float(allv.max())
    if is_integer_valued(gen, imp) and top >= 1000:  # heavy-tailed (SIGFM): 0, then decades
        edges = [0, 1] + [10 ** k for k in range(1, int(math.log10(top)) + 2)]
        rows = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            rows.append({"bin": "0" if hi == 1 else f"{lo}-{hi - 1}",
                         "genuine": int(np.sum((gen >= lo) & (gen < hi))),
                         "impostor": int(np.sum((imp >= lo) & (imp < hi)))})
        return rows
    if is_integer_valued(gen, imp):
        width = max(1, math.ceil((top + 1) / max_bins))
        if width > 1:  # round to 2, 5, 10, 20, ...
            width = min(w for w in (2, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 10**9) if w >= width)
        edges = np.arange(0, top + width + 1, width)
    else:
        edges = np.linspace(0, top if top > 0 else 1, max_bins + 1)
        edges[-1] = np.nextafter(edges[-1], np.inf)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        g = int(np.sum((gen >= lo) & (gen < hi)))
        i = int(np.sum((imp >= lo) & (imp < hi)))
        if is_integer_valued(gen, imp):
            label = f"{int(lo)}" if hi - lo == 1 else f"{int(lo)}-{int(hi) - 1}"
        else:
            label = f"{lo:.3g}-{hi:.3g}"
        rows.append({"bin": label, "genuine": g, "impostor": i})
    while rows and rows[-1]["genuine"] == 0 and rows[-1]["impostor"] == 0:
        rows.pop()
    return rows


def _splits(members: list[int], k: int, max_splits: int | None, rng: random.Random):
    total = math.comb(len(members), k)
    if max_splits is None or total <= max_splits:
        return list(itertools.combinations(members, k))
    seen: set[tuple[int, ...]] = set()
    while len(seen) < max_splits:
        seen.add(tuple(sorted(rng.sample(members, k))))
    return sorted(seen)


def enroll_sim(S: np.ndarray, fingers: list[str], k: int, thresholds: list[float],
               max_splits: int | None = None, seed: int = 0) -> dict:
    """Enroll k images per finger, verify the rest of the finger and all other fingers' images."""
    S, _ = clean(S)
    rng = random.Random(seed)
    names = sorted(set(fingers))
    idx = {f: [i for i, x in enumerate(fingers) if x == f] for f in names}
    gen, imp, gen_f, imp_f, split_rows = [], [], [], [], []
    per_finger_splits = {}
    for f in names:
        if len(idx[f]) <= k:
            per_finger_splits[f] = 0
            continue
        splits = _splits(idx[f], k, max_splits, rng)
        per_finger_splits[f] = len(splits)
        others = [i for i in range(len(fingers)) if fingers[i] != f]
        for E in splits:
            E = list(E)
            g = [S[p, E].max() for p in idx[f] if p not in E]
            i = [S[p, E].max() for p in others]
            gen += g
            imp += i
            gen_f += [f] * len(g)
            imp_f += [f] * len(i)
            split_rows.append((f, np.array(g), np.array(i)))
    gen, imp = np.array(gen), np.array(imp)
    out = {"k": k, "splits": per_finger_splits, "genuine": stats(gen), "impostor": stats(imp),
           "zero_fa": zero_fa(gen, imp), "eer": eer(gen, imp),
           "at": [rates_at(gen, imp, t) for t in thresholds]}
    if not len(gen):
        return out
    t0 = out["zero_fa"]["threshold"]
    out["per_finger_frr_at_zero_fa"] = {
        f: float(np.mean(gen[np.array(gen_f) == f] < t0)) for f in names if per_finger_splits[f]}
    worst = {}
    for t in thresholds:
        frrs = [float(np.mean(g < t)) for _, g, _ in split_rows if len(g)]
        fa_splits = sum(1 for _, _, i in split_rows if np.any(i >= t))
        worst[str(t)] = {"worst_split_frr": max(frrs) if frrs else float("nan"),
                         "splits_with_a_false_accept": fa_splits, "n_splits": len(split_rows)}
    out["per_split"] = worst
    return out


def analyze(S: np.ndarray, samples_fingers: list[str], thresholds: list[float],
            ks: tuple[int, ...] = (3, 5), max_splits: int | None = None,
            symmetric: bool | None = None) -> dict:
    """symmetric: True if S[p, g] == S[g, p] by construction, False if the matcher
    is directional; None guesses from the values (unsafe when most scores are 0)."""
    Sc, failed = clean(S)
    gen, imp, _, _, sym = pair_scores(S, samples_fingers, symmetric)
    res = {"pairs": "unordered" if sym else "ordered (probe vs template)",
           "failed_entries": int(failed.sum()),
           "genuine": stats(gen), "impostor": stats(imp),
           "histogram": histogram(gen, imp),
           "zero_fa": zero_fa(gen, imp), "eer": eer(gen, imp),
           "at": [rates_at(gen, imp, t) for t in thresholds],
           "genuine_sorted": sorted(gen.tolist()), "impostor_sorted": sorted(imp.tolist()),
           "enroll": {}}
    for k in ks:
        res["enroll"][str(k)] = enroll_sim(S, samples_fingers, k, thresholds, max_splits)
    return res


def pct(x: float) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}%"


def fmt(x: float) -> str:
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return f"{x:.3g}" if isinstance(x, float) else str(x)


def markdown(name: str, res: dict, extra: str = "") -> str:
    """Per-variant markdown block: distributions, operating points, enrollment."""
    g, i = res["genuine"], res["impostor"]
    lines = [f"### {name}", ""]
    if extra:
        lines += [extra, ""]
    lines += [f"Pairs: {res['pairs']}; genuine n={g['n']}, impostor n={i['n']}; "
              f"failed score entries: {res['failed_entries']}.", "",
              "| | min | p5 | p25 | median | p75 | p95 | max |", "|---|---|---|---|---|---|---|---|"]
    for lab, d in (("genuine", g), ("impostor", i)):
        lines.append(f"| {lab} | " + " | ".join(fmt(d[k]) for k in
                                                  ("min", "p05", "p25", "median", "p75", "p95", "max")) + " |")
    z = res["zero_fa"]
    lines += ["", f"Single-image pairs: zero-FA threshold {fmt(z['threshold'])} "
              f"(max impostor {fmt(z['max_impostor'])}) -> FRR {pct(z['frr'])} "
              f"({z['false_rejects']}/{z['genuine_attempts']}); EER {pct(res['eer']['eer'])}.", ""]
    lines += ["| threshold | FRR | FAR |", "|---|---|---|"]
    for a in res["at"]:
        lines.append(f"| {fmt(a['threshold'])} | {pct(a['frr'])} ({a['false_rejects']}) | "
                     f"{pct(a['far'])} ({a['false_accepts']}) |")
    lines += ["", "Score histogram (pairs):", "", "| score | genuine | impostor |", "|---|---|---|"]
    for r in res["histogram"]:
        lines.append(f"| {r['bin']} | {r['genuine']} | {r['impostor']} |")
    for k, e in res["enroll"].items():
        if not e["genuine"].get("n"):
            continue
        z = e["zero_fa"]
        lines += ["", f"Enrollment of {k} images per finger ({', '.join(f'{f}: {n} splits' for f, n in e['splits'].items())}); "
                  f"genuine attempts {e['genuine']['n']}, impostor attempts {e['impostor']['n']}:", "",
                  "| | min | median | max |", "|---|---|---|---|",
                  f"| genuine (max over enrolled) | {fmt(e['genuine']['min'])} | {fmt(e['genuine']['median'])} | {fmt(e['genuine']['max'])} |",
                  f"| impostor (max over enrolled) | {fmt(e['impostor']['min'])} | {fmt(e['impostor']['median'])} | {fmt(e['impostor']['max'])} |",
                  "",
                  f"Zero-FA threshold {fmt(z['threshold'])} -> FRR {pct(z['frr'])} ({z['false_rejects']}/{z['genuine_attempts']}); "
                  f"per finger: " + ", ".join(f"{f} {pct(v)}" for f, v in e.get("per_finger_frr_at_zero_fa", {}).items()),
                  "", "| threshold | FRR | FAR | worst split FRR | splits with a false accept |", "|---|---|---|---|---|"]
        for a in e["at"]:
            w = e["per_split"][str(a["threshold"])]
            lines.append(f"| {fmt(a['threshold'])} | {pct(a['frr'])} | {pct(a['far'])} ({a['false_accepts']}) | "
                         f"{pct(w['worst_split_frr'])} | {w['splits_with_a_false_accept']}/{w['n_splits']} |")
    return "\n".join(lines) + "\n"
