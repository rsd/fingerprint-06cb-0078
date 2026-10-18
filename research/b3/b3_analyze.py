#!/usr/bin/env python3
"""B3 step 2: false-accept benchmark over the B3 captures, at template level.

Input: local/b3/<person>/<label>/NNN.pgm with an NNN.json sidecar
({"session": "YYYY-MM-DD", "person": ..., "label": ...}); a sample counts only
when both files exist. Identity = (person, label).

Scorer: the A5 harness (research/a5/sigfm_matrix --scale 2), the driver path
(see research/b3/b3_common.py), whose parity with fprintd's live scores
research/b3/b3_parity.py proves. The full score matrix over all images is
cached in local/b3/results/ under a key made of the sorted image list, the
file hashes and the harness sha256. An image the core's keypoint gate would
reject is a failed capture (the device asks again): it is no template stage
and no probe, and is counted.

Trials (verify semantics): for every (identity, session) with >= stages + 2
images, K random templates of `stages` images (fixed seed).
  score(template, probe) = max over the stages of score[probe][stage]
  (the OR of fpi_print_sigfm_match; it accepts at threshold t iff this >= t).
  genuine probes:  the identity's held-out images of the same session;
  impostor probes: every image of every other identity, any session.

FAR upper bounds (one-sided 95% Clopper-Pearson) at three correlation levels:
  (a) all trials           naive, assumes every trial independent;
  (b) probe level          one template per identity (k = 0) x each impostor probe;
  (c) finger-pair level    unordered pairs of distinct identities with at least
                           one tested direction; event = any accept in either
                           direction over any template and probe. The most
                           conservative of the three, but its bound still assumes
                           the pairs independent, and pairs sharing a finger are not.

Recommended threshold: brief D6's rule applied to the highest impostor score
s seen pairwise (stage level, a superset of the trial maxima): the score of
the smallest whole match count at least 2 above the match-equivalent of s.
That is what the rule allows, not a FAR target: the verdict prints it next to
the evidence scope (identities, persons, finger pairs, the level-(c) bound and
the FRR gain against 105 at every threshold the rule allows).

Output: a short summary on stdout and every number in
local/b3/results/b3-YYYYMMDD-HHMMSS.json (0600).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import random
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import b3_common as c
import b3_parity

SCHEMA = "b3-analyze/1"
THRESHOLDS = [105, 75, 50, 30, 20, 10, 5]
QUANTILES = [50, 90, 99, 99.9]
ALPHA = 0.05
MARGIN_MATCHES = 2
RESERVED = {"results", "bin", "logs"}
STEM_RE = re.compile(r"^\d+$")
SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CAPTURE_HINT = "tools/dev/b3-capture.sh"


# ---------------------------------------------------------------- data

def load_samples(data: Path) -> tuple[list[dict], list[dict]]:
    """(samples, orphans). Samples sorted by person, label, stem; orphans carry a reason."""
    samples, orphans = [], []
    if not data.is_dir():
        return samples, orphans
    for pdir in sorted(p for p in data.iterdir() if p.is_dir()):
        if pdir.name in RESERVED or pdir.name.startswith("."):
            continue
        for ldir in sorted(p for p in pdir.iterdir() if p.is_dir()):
            if ldir.name.startswith("."):
                continue
            pgms = {f.stem for f in ldir.glob("*.pgm") if STEM_RE.match(f.stem)}
            jsons = {f.stem for f in ldir.glob("*.json") if STEM_RE.match(f.stem)}
            for stem in sorted(pgms | jsons):
                where = f"{pdir.name}/{ldir.name}/{stem}"
                if stem not in pgms or stem not in jsons:
                    orphans.append({"sample": where, "reason": "no .json sidecar" if stem in pgms else "no .pgm"})
                    continue
                try:
                    side = json.loads((ldir / f"{stem}.json").read_text())
                except (OSError, ValueError):
                    orphans.append({"sample": where, "reason": "unreadable sidecar"})
                    continue
                session = side.get("session") if isinstance(side, dict) else None
                if not isinstance(session, str) or not SESSION_RE.match(session):
                    orphans.append({"sample": where, "reason": "sidecar without a YYYY-MM-DD session"})
                    continue
                if side.get("person", pdir.name) != pdir.name or side.get("label", ldir.name) != ldir.name:
                    orphans.append({"sample": where, "reason": "sidecar person/label differ from the directory"})
                    continue
                samples.append({"path": ldir / f"{stem}.pgm", "person": pdir.name, "label": ldir.name,
                                "identity": f"{pdir.name}/{ldir.name}", "session": session, "stem": stem})
    return samples, orphans


def cache_key(images: list[tuple[str, str]], harness_sha: str, scale: int) -> str:
    """Key of a score matrix: sorted (relative path, file sha256) list, harness, scale."""
    blob = json.dumps({"harness_sha256": harness_sha, "scale": scale, "images": sorted(images)},
                      sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def get_matrix(samples: list[dict], data: Path, harness: Path, results: Path, scale: int = c.SCALE) -> dict:
    """The harness JSON over the samples (in sample order), from the cache when the key matches."""
    harness_sha = c.sha256_file(harness)
    images = [(str(s["path"].relative_to(data)), c.sha256_file(s["path"])) for s in samples]
    key = cache_key(images, harness_sha, scale)
    cached = results / f"matrix-{key[:16]}.json"
    if cached.is_file():
        try:
            box = json.loads(cached.read_text())
            if box.get("key") == key:
                return {"res": box["harness"], "key": key, "cached": True, "seconds": box.get("seconds"),
                        "harness_sha256": harness_sha}
        except ValueError:
            pass
    tmp = results / f".matrix-{key[:16]}.tmp.json"
    t0 = time.monotonic()
    res = c.run_harness(harness, [s["path"] for s in samples], tmp, scale)
    seconds = round(time.monotonic() - t0, 2)
    tmp.unlink()
    c.write_private(cached, json.dumps({"key": key, "seconds": seconds, "harness": res}))
    return {"res": res, "key": key, "cached": False, "seconds": seconds, "harness_sha256": harness_sha}


# ---------------------------------------------------------------- trials

def group_by(samples: list[dict], usable: list[int]) -> dict:
    """(identity, session) -> usable sample indices, in sample order."""
    g: dict = {}
    for i in usable:
        g.setdefault((samples[i]["identity"], samples[i]["session"]), []).append(i)
    return g


def draw_templates(groups: dict, stages: int, k: int, seed: int) -> list[dict]:
    """K templates per (identity, session) with >= stages + 2 images; the rest is held out.

    Each group gets its own generator, seeded by (seed, identity, session), so
    adding a person never changes another group's draws."""
    out = []
    for (ident, session) in sorted(groups):
        idx = groups[(ident, session)]
        if len(idx) < stages + 2:
            continue
        rng = random.Random(f"{seed}:{ident}:{session}")
        for j in range(k):
            t = sorted(rng.sample(idx, stages))
            ts = set(t)
            out.append({"identity": ident, "session": session, "k": j, "stages": t,
                        "held_out": [i for i in idx if i not in ts]})
    return out


def template_scores(S: np.ndarray, stages: list[int], probes: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """(max over stages of S[probe][stage], index of that stage in the template) per probe."""
    if not probes:
        return np.zeros(0), np.zeros(0, dtype=int)
    sub = S[np.ix_(probes, stages)]
    return sub.max(axis=1), sub.argmax(axis=1)


def run_trials(S: np.ndarray, samples: list[dict], usable: list[int], templates: list[dict]) -> dict:
    gen, imp = [], []
    for ti, t in enumerate(templates):
        sc, st = template_scores(S, t["stages"], t["held_out"])
        gen += [(ti, p, float(s), int(k)) for p, s, k in zip(t["held_out"], sc, st)]
        others = [i for i in usable if samples[i]["identity"] != t["identity"]]
        sc, st = template_scores(S, t["stages"], others)
        imp += [(ti, p, float(s), int(k)) for p, s, k in zip(others, sc, st)]
    return {"genuine": gen, "impostor": imp}


def rates(gen_scores, imp_scores, t: float) -> dict:
    g = np.asarray(gen_scores, dtype=float)
    i = np.asarray(imp_scores, dtype=float)
    return {"threshold": t, "far_x": int((i >= t).sum()), "far_n": int(i.size),
            "frr_x": int((g < t).sum()), "frr_n": int(g.size)}


# ---------------------------------------------------------------- statistics

def binom_logcdf(x: int, n: int, p: float) -> float:
    """log P(X <= x), X ~ Binomial(n, p), summed in log space."""
    if p <= 0:
        return 0.0
    if p >= 1:
        return 0.0 if x >= n else -math.inf
    lp, lq = math.log(p), math.log1p(-p)
    terms = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
             for i in range(min(x, n) + 1)]
    m = max(terms)
    return m + math.log(sum(math.exp(v - m) for v in terms))


def cp_upper(x: int, n: int, alpha: float = ALPHA) -> float | None:
    """One-sided (1 - alpha) Clopper-Pearson upper bound of a binomial rate."""
    if n <= 0:
        return None
    if x >= n:
        return 1.0
    if x == 0:
        return 1 - alpha ** (1 / n)
    lo, hi, target = x / n, 1.0, math.log(alpha)
    while hi - lo > 1e-15:
        mid = (lo + hi) / 2
        if binom_logcdf(x, n, mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def correlation_levels(trials: dict, templates: list[dict], samples: list[dict], t: float) -> dict:
    """FAR events and n at the three correlation levels for threshold t."""
    imp = trials["impostor"]
    a = {"x": sum(1 for _, _, s, _ in imp if s >= t), "n": len(imp)}
    # (b) one template per identity: k = 0 of its earliest session with templates
    first = {}
    for ti, tp in enumerate(templates):
        if tp["k"] == 0 and tp["identity"] not in first:
            first[tp["identity"]] = ti
    keep = set(first.values())
    b_tr = [tr for tr in imp if tr[0] in keep]
    b = {"x": sum(1 for _, _, s, _ in b_tr if s >= t), "n": len(b_tr)}
    # (c) unordered identity pairs
    tested, hit = set(), set()
    for ti, p, s, _ in imp:
        pair = tuple(sorted((templates[ti]["identity"], samples[p]["identity"])))
        tested.add(pair)
        if s >= t:
            hit.add(pair)
    cc = {"x": len(hit), "n": len(tested)}
    for lv in (a, b, cc):
        lv["upper95"] = cp_upper(lv["x"], lv["n"])
    return {"a_all_trials": a, "b_probe_level": b, "c_finger_pairs": cc}


# ---------------------------------------------------------------- threshold rule

def matches_score(m: float) -> float:
    """SIGFM score of m fully consistent matches: C(C(m, 2), 2)."""
    a = m * (m - 1) / 2
    return a * (a - 1) / 2


def score_matches(s: float) -> float:
    """Inverse of matches_score (brief D6): p = (1 + sqrt(1 + 8s)) / 2, m = (1 + sqrt(1 + 8p)) / 2."""
    p = (1 + math.sqrt(1 + 8 * max(s, 0.0))) / 2
    return (1 + math.sqrt(1 + 8 * p)) / 2


def recommend(impostor_max: float, margin: int = MARGIN_MATCHES) -> dict:
    """D6: the score of the smallest whole m >= m_imp + margin."""
    m_imp = score_matches(impostor_max)
    m = math.ceil(m_imp + margin - 1e-9)
    return {"impostor_max": impostor_max, "m_impostor": round(m_imp, 3), "m": m,
            "threshold": round(matches_score(m))}


def allowed_below_105(rec_threshold: float, thresholds) -> list:
    """Thresholds below 105 that the D6 rule allows (>= its threshold), highest first."""
    if rec_threshold >= c.THRESHOLD:
        return []
    return sorted({t for t in thresholds if rec_threshold <= t < c.THRESHOLD} | {rec_threshold}, reverse=True)


def frr_gain(x105: int, x: int, n: int) -> dict:
    """FRR at 105 vs at a lower threshold over the same n genuine trials (gain in percentage points)."""
    return {"frr_x_105": x105, "frr_x": x, "n": n, "gain_pp": None if n == 0 else round(100 * (x105 - x) / n, 3)}


# ---------------------------------------------------------------- cross-day

def cross_day(S: np.ndarray, samples: list[dict], usable: list[int], stages: int, k: int, seed: int,
              thresholds) -> list[dict]:
    """Templates from an earlier session, probes = every image of a later one, per identity."""
    by: dict = {}
    for i in usable:
        by.setdefault(samples[i]["identity"], {}).setdefault(samples[i]["session"], []).append(i)
    out = []
    for ident in sorted(by):
        sess = sorted(by[ident])
        for ai, a in enumerate(sess):
            for b in sess[ai + 1:]:
                row = {"identity": ident, "template_session": a, "probe_session": b,
                       "probes": len(by[ident][b])}
                if len(by[ident][a]) < stages:
                    row["skipped"] = f"{len(by[ident][a])} images in {a}, fewer than {stages}"
                    out.append(row)
                    continue
                rng = random.Random(f"{seed}:xday:{ident}:{a}")
                scores = []
                for _ in range(k):
                    t = sorted(rng.sample(by[ident][a], stages))
                    scores += list(template_scores(S, t, by[ident][b])[0])
                row["trials"] = len(scores)
                row["frr"] = [{"threshold": t, "x": int(sum(1 for s in scores if s < t)), "n": len(scores)}
                              for t in thresholds]
                out.append(row)
    return out


# ---------------------------------------------------------------- report

def pct(x: int, n: int) -> str:
    return "-" if n == 0 else f"{100 * x / n:.2f}%"


def bound(v) -> str:
    return "-" if v is None else f"{100 * v:.3g}%"


def fmt_score(x: float) -> str:
    return f"{x:.3g}" if x >= 1e5 else f"{int(x)}"


def analyze(S: np.ndarray, samples: list[dict], rejected: np.ndarray, stages: int, k: int, seed: int,
            thresholds=THRESHOLDS) -> dict:
    """Every number of the report, from a score matrix (rows probes, columns stages)."""
    n = len(samples)
    usable = [i for i in range(n) if not rejected[i]]
    groups = group_by(samples, usable)
    templates = draw_templates(groups, stages, k, seed)
    trials = run_trials(S, samples, usable, templates)
    gen = [s for _, _, s, _ in trials["genuine"]]
    imp = [s for _, _, s, _ in trials["impostor"]]

    pair = [S[i, j] for i in usable for j in usable
            if i != j and samples[i]["identity"] != samples[j]["identity"]]
    pairwise = {"n": len(pair), "nonzero": int(sum(1 for v in pair if v > 0)),
                "max": float(max(pair)) if pair else None}
    imp_trial = {"n": len(imp), "max": float(max(imp)) if imp else None,
                 "quantiles": {str(q): float(np.percentile(imp, q)) for q in QUANTILES} if imp else {}}

    top = sorted(trials["impostor"], key=lambda tr: (-tr[2], tr[0], tr[1]))[:10]
    top_out = [{"template": templates[ti]["identity"], "template_session": templates[ti]["session"],
                "k": templates[ti]["k"], "probe": samples[p]["identity"], "probe_stem": samples[p]["stem"],
                "probe_session": samples[p]["session"], "score": s, "stage": st} for ti, p, s, st in top]

    table = []
    for t in thresholds:
        r = rates(gen, imp, t)
        r["levels"] = correlation_levels(trials, templates, samples, t)
        table.append(r)

    rec = None
    if pairwise["max"] is not None:
        rec = recommend(pairwise["max"])
        r = rates(gen, imp, rec["threshold"])
        r["levels"] = correlation_levels(trials, templates, samples, rec["threshold"])
        rec["at_threshold"] = r
        r105 = correlation_levels(trials, templates, samples, c.THRESHOLD)["c_finger_pairs"]
        rec["c_at_105"] = r105
        # The D6 margin rule only: whether its threshold is below 105. No FAR target is implied.
        rec["d6_rule_allows_lowering"] = bool(rec["threshold"] < c.THRESHOLD)
        allowed = allowed_below_105(rec["threshold"], thresholds)
        x105 = sum(1 for g in gen if g < c.THRESHOLD)
        pooled: dict = {}
        if allowed:
            for row in cross_day(S, samples, usable, stages, k, seed, [c.THRESHOLD, *allowed]):
                for f in row.get("frr", []):
                    acc = pooled.setdefault(f["threshold"], [0, 0])
                    acc[0] += f["x"]
                    acc[1] += f["n"]
        gains = []
        for t in allowed:
            xd = None
            if pooled.get(t, [0, 0])[1] > 0:
                xd = frr_gain(pooled[c.THRESHOLD][0], pooled[t][0], pooled[t][1])
            gains.append({"threshold": t, "same_session": frr_gain(x105, sum(1 for g in gen if g < t), len(gen)),
                          "cross_day": xd})
        persons = {samples[i]["person"] for i in usable}
        rec["scope"] = {"identities": len({samples[i]["identity"] for i in usable}), "persons": len(persons),
                        "finger_pairs": r["levels"]["c_finger_pairs"]["n"],
                        "c_upper95": r["levels"]["c_finger_pairs"]["upper95"], "c_upper95_at_105": r105["upper95"],
                        "one_person": len(persons) == 1, "frr_gain_vs_105": gains}
        rec["rationale"] = (f"highest impostor score {fmt_score(pairwise['max'])} is about "
                            f"{rec['m_impostor']:.2f} consistent matches; {MARGIN_MATCHES} more rounds up to "
                            f"m = {rec['m']}, score C(C({rec['m']},2),2) = {rec['threshold']}")

    rej_by = {}
    for i in range(n):
        if rejected[i]:
            rej_by[samples[i]["identity"]] = rej_by.get(samples[i]["identity"], 0) + 1
    ids = sorted({s["identity"] for s in samples})
    return {
        "data": {"images": n, "usable": len(usable), "persons": len({s["person"] for s in samples}),
                 "identities": len(ids), "sessions": sorted({s["session"] for s in samples}),
                 "per_identity_session": {f"{i}@{s}": len(v) for (i, s), v in sorted(groups.items())}},
        "failed_captures": {"count": int(rejected.sum()), "by_identity": rej_by,
                            "rule": "rejected by the core's keypoint gate: no template stage, no probe"},
        "trials": {"stages": stages, "k": k, "seed": seed, "templates": len(templates),
                   "template_groups": len({(t["identity"], t["session"]) for t in templates}),
                   "genuine": len(gen), "impostor": len(imp)},
        "impostor": {"trial_max": imp_trial, "pairwise_stage": pairwise},
        "top_impostors": top_out,
        "thresholds": table,
        "recommendation": rec,
        "cross_day": cross_day(S, samples, usable, stages, k, seed, thresholds),
    }


def parity_status(parity: Path, harness_sha: str) -> dict:
    if not parity.is_file():
        return {"status": "missing", "line": "parity: NOT RUN (no parity.json)"}
    p = json.loads(parity.read_text())
    same = p.get("harness_sha256") == harness_sha
    st = "exact" if p.get("exact") else "MISMATCH"
    line = f"parity: {st} ({p.get('n_captures')} captures, {p.get('n_stage_scores')} stage scores)"
    line += ", same harness" if same else ", OTHER HARNESS (rerun b3_parity.py)"
    return {"status": st, "same_harness": same, "line": line, "fork_head": p.get("fork_head")}


ONE_PERSON = "one person only: a guest's fingers would be the independent test; the bound is per finger pair of this person"


def gain_text(g: dict | None) -> str:
    if g is None:
        return "-"
    pp = "-" if g["gain_pp"] is None else f"{g['gain_pp']:+.2f} pp"
    return f"{pp} ({g['frr_x_105']} -> {g['frr_x']} of {g['n']})"


def verdict_lines(rec: dict) -> list[str]:
    """The D6 verdict and, right next to it, the scope of the evidence behind it."""
    sc = rec["scope"]
    if rec["d6_rule_allows_lowering"]:
        rule = f"allows lowering below 105, down to {rec['threshold']}"
    else:
        rule = f"does not allow lowering below 105 (it gives {rec['threshold']})"
    L = [f"  D6 margin rule: {rule}; a rule, not a FAR target",
         (f"  evidence: {sc['identities']} identities, {sc['persons']} persons, {sc['finger_pairs']} unordered "
          f"finger pairs; FAR bound (c) {bound(sc['c_upper95'])} at {rec['threshold']}, "
          f"{bound(sc['c_upper95_at_105'])} at 105 (one-sided 95%, pairs assumed independent)")]
    if sc["frr_gain_vs_105"]:
        L.append("  FRR gain vs 105 (same-session; cross-day):")
        L += [f"    @{g['threshold']}: {gain_text(g['same_session'])}; {gain_text(g['cross_day'])}"
              for g in sc["frr_gain_vs_105"]]
    if sc["one_person"]:
        L.append("  " + ONE_PERSON)
    return L


def summary(rep: dict) -> str:
    sc, d, tr = rep["scorer"], rep["data"], rep["trials"]
    match = {True: "commits match", False: "COMMITS DIFFER", None: "commit match unknown"}[sc["commits_match"]]
    head = (f"scorer: fork build {(sc['fork_head'] or '?')[:8]}{' (dirty)' if sc['fork_dirty'] else ''} "
            f"(libsigfm.a, harness {sc['harness_sha256'][:12]}), installed {c.PACKAGE} "
            f"{sc['package_version'] or 'not installed'}: {match}")
    L = [f"B3 false-accept benchmark ({rep['created']})", head, rep["parity"]["line"]]
    if tr["stages"] != c.STAGES:
        L.append(f"SMOKE RUN: --stages {tr['stages']} (the driver enrolls {c.STAGES}); numbers are plumbing only")
    fc = rep["failed_captures"]
    L.append(f"data: {d['persons']} persons, {d['identities']} identities, {len(d['sessions'])} sessions, "
             f"{d['images']} images ({fc['count']} gate-rejected = failed captures, "
             f"{rep['orphans_count']} orphans skipped)")
    mx = rep["matrix"]
    took = "cached" if mx["cached"] else f"{mx['seconds']} s"
    per = "" if mx["match_ms_median"] is None else f", median {mx['match_ms_median']:.2f} ms per comparison"
    L.append(f"matrix: {mx['images']} images, {mx['comparisons']} comparisons ({took}){per}")
    L.append(f"trials: K={tr['k']} templates of {tr['stages']} per (identity, session) with >= {tr['stages'] + 2} "
             f"images, seed {tr['seed']}: {tr['templates']} templates in {tr['template_groups']} groups; "
             f"{tr['genuine']} genuine, {tr['impostor']} impostor trials")
    if tr["templates"] == 0:
        L.append(f"  no (identity, session) has {tr['stages'] + 2} usable images yet: capture more "
                 f"({CAPTURE_HINT})")
    if d["identities"] < 2:
        L.append("  only one identity: no impostor trials (capture another finger or person)")
    im = rep["impostor"]
    q = im["trial_max"]["quantiles"]
    L += ["", "impostor scores"]
    if q:
        L.append("  trial (max over stages): " + ", ".join(f"p{k} {fmt_score(v)}" for k, v in q.items())
                 + f", max {fmt_score(im['trial_max']['max'])}")
    pw = im["pairwise_stage"]
    L.append(f"  pairwise stage level: {pw['n']} pairs, {pw['nonzero']} > 0, max "
             f"{'-' if pw['max'] is None else fmt_score(pw['max'])}")
    L += ["", "                                             FAR one-sided 95% upper bound",
          "threshold  FAR                  FRR                (a) trials  (b) probes   (c) pairs"]
    for r in rep["thresholds"]:
        lv = r["levels"]
        far = f"{r['far_x']}/{r['far_n']} {pct(r['far_x'], r['far_n'])}"
        frr = f"{r['frr_x']}/{r['frr_n']} {pct(r['frr_x'], r['frr_n'])}"
        L.append(f"{r['threshold']:>9}  {far:<20} {frr:<18} {bound(lv['a_all_trials']['upper95']):>10}  "
                 f"{bound(lv['b_probe_level']['upper95']):>10}  {bound(lv['c_finger_pairs']['upper95']):>10}")
    if rep["thresholds"]:
        lv = rep["thresholds"][0]["levels"]
        L.append(f"  n: (a) {lv['a_all_trials']['n']} trials, naive (assumes independence); (b) "
                 f"{lv['b_probe_level']['n']} trials, one template per identity x each impostor probe; (c) "
                 f"{lv['c_finger_pairs']['n']} finger pairs, the most conservative, still assuming pairs "
                 "independent (pairs sharing a finger are not)")
    top = [t for t in rep["top_impostors"] if t["score"] > 0]
    if top:
        L += ["", "top impostor trials (score > 0)"]
        for i, t in enumerate(top, 1):
            L.append(f"  {i:>2}. {fmt_score(t['score'])} at stage {t['stage']}: template {t['template']} "
                     f"k={t['k']} ({t['template_session']}) <- probe {t['probe']} {t['probe_stem']} "
                     f"({t['probe_session']})")
    elif rep["trials"]["impostor"]:
        L += ["", "top impostor trials: every impostor trial scored 0"]
    rec = rep["recommendation"]
    L.append("")
    if rec is None:
        L.append("recommended threshold: none (no impostor pairs)")
    else:
        at = rec["at_threshold"]
        cc = at["levels"]["c_finger_pairs"]
        at_line = (f"  at {rec['threshold']}: FAR bound (c) {bound(cc['upper95'])} ({cc['x']}/{cc['n']} finger "
                   f"pairs), FRR {at['frr_x']}/{at['frr_n']} {pct(at['frr_x'], at['frr_n'])}")
        L += [f"recommended threshold: {rec['threshold']} ({rec['rationale']})", at_line, *verdict_lines(rec)]
    xd = rep["cross_day"]
    if xd:
        L += ["", "cross-day genuine (templates from the earlier session)"]
        for row in xd:
            head = f"  {row['identity']} {row['template_session']} -> {row['probe_session']}"
            if "skipped" in row:
                L.append(f"{head}: skipped ({row['skipped']})")
            else:
                L.append(f"{head}, {row['trials']} trials: FRR " + ", ".join(
                    f"@{f['threshold']} {pct(f['x'], f['n'])}" for f in row["frr"]))
    L += ["", f"json: {rep['json_path']}"]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, default=c.ROOT / "local/b3")
    ap.add_argument("--results", type=Path, help="default: DATA/results")
    ap.add_argument("--harness", type=Path, default=c.ROOT / "local/b3/bin/sigfm_matrix")
    ap.add_argument("--fork", type=Path, default=None, help="default: $LIBFPRINT_FORK or ~/Devel/Linux/Synaptics/libfprint")
    ap.add_argument("--parity", type=Path, default=c.ROOT / "local/b3/results/parity.json")
    ap.add_argument("--k", type=int, default=5, help="templates per (identity, session) (default 5)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--stages", type=int, default=c.STAGES, help="smoke runs on small data only (default 18)")
    a = ap.parse_args(argv)
    if a.k < 1 or a.stages < 1:
        ap.error("--k and --stages must be >= 1")
    results = a.results or a.data / "results"
    fork_path = a.fork or b3_parity.default_fork()

    samples, orphans = load_samples(a.data)
    if not samples:
        print(f"No B3 samples under {a.data} yet ({len(orphans)} orphan files). "
              f"Capture first: {CAPTURE_HINT}, then rerun tools/dev/b3-analyze.sh.")
        return 0
    if not a.harness.is_file():
        print(f"no harness at {a.harness}: run tools/dev/b3-analyze.sh (it builds it)", file=sys.stderr)
        return 2
    c.private_dir(results)
    mx = get_matrix(samples, a.data, a.harness, results)
    res = mx["res"]
    S = c.score_matrix(res)
    rejected = c.rejected_mask(res)
    rep = analyze(S, samples, rejected, a.stages, a.k, a.seed)

    fork = c.fork_head(fork_path)
    pkg = c.package_version()
    stamp = dt.datetime.now().astimezone()
    out = results / f"b3-{stamp:%Y%m%d-%H%M%S}.json"
    rep.update({
        "schema": SCHEMA,
        "created": stamp.strftime("%Y-%m-%d %H:%M:%S"),
        "scorer": {"fork": str(fork_path), "fork_head": fork["head"], "fork_dirty": fork["dirty"],
                   "harness": str(a.harness), "harness_sha256": mx["harness_sha256"], "scale": c.SCALE,
                   "package_version": pkg["version"], "package_commit": pkg["commit"],
                   "commits_match": c.commits_match(pkg["commit"], fork["head"])},
        "parity": parity_status(a.parity, mx["harness_sha256"]),
        "orphans": orphans, "orphans_count": len(orphans),
        "matrix": {"key": mx["key"], "cached": mx["cached"], "seconds": mx["seconds"], "images": len(samples),
                   "comparisons": len(samples) ** 2, "match_ms_median": c.match_ms_median(res)},
        "json_path": str(out),
    })
    c.write_json(out, rep)
    print(summary(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
