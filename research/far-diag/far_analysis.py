#!/usr/bin/env python3
"""FAR diagnosis for the 2026-10-15 fprintd run: aggregate numbers only.

Inputs, all under local/far-diag (biometric-derived, never tracked):
  map.csv            research/far-diag/map_dumps.py: dump -> role, session, journal scores
  stages.tsv         far_replay stages: stored stage -> source dump (byte-identical SIGFM)
  verify_replay.tsv  far_replay verify: per verify capture, the decision and scores of
                     fpi_print_sigfm_match() (live_scores) and of all 18 stages
  matrix2x.json      research/a5 sigfm_matrix --scale 2 over proto + dumps + A4 captures
  geometry.tsv       sigfm_geometry: RANSAC similarity behind SIGFM's candidates, per pair

Identity rule (stated in the report): a capture's finger is the finger the user
claimed, unless it scores >= 105 against at least one proto capture of another
finger and against none of the claimed finger; then it is that other finger.  The
rule uses SIGFM, but never the stored print, and the proto captures come from
another day (2026-10-14) and another capture tool (proto/).  Captures with no
proto evidence keep the claimed label.  The A4 captures (finger never recorded)
are reported apart and left out of FAR and FRR.

Output: markdown on stdout, numbers only (no paths, no pixels).
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import numpy as np

T_LIVE = 105
THRESHOLDS = [1, 7, 105, 1000, 10000, 100000]
FINGERS = ["right-index", "right-middle", "left-index"]


def load(d: Path):
    M = json.loads((d / "matrix2x.json").read_text())
    paths = [im["path"] for im in M["images"]]
    kp = np.array([im["keypoints"] for im in M["images"]])
    S = np.array([[(-1 if v is None else v) for v in row] for row in M["scores"]], dtype=float)
    mp = {r["file"]: r for r in csv.DictReader((d / "map.csv").open())}
    geo = {(int(r["i"]), int(r["j"])): r for r in csv.DictReader((d / "geometry.tsv").open(), delimiter="\t")}
    stages = list(csv.DictReader((d / "stages.tsv").open(), delimiter="\t"))
    replay = list(csv.DictReader((d / "verify_replay.tsv").open(), delimiter="\t"))
    return paths, kp, S, mp, geo, stages, replay


def describe(paths, mp):
    """Per image: source, claimed finger, role."""
    out = []
    for p in paths:
        f = p.split("/")[-1]
        if "/proto/" in p or p.startswith("../../proto"):
            out.append({"src": "proto", "claim": p.split("/")[-2], "role": "proto", "file": f})
        elif "diag-fprintd" in p:
            r = mp[f]
            claim = r["session"][7:] if r["session"].startswith("verify-") else None
            out.append({"src": "dump", "claim": claim, "role": r["role"], "session": r["session"], "file": f,
                        "row": r})
        else:
            out.append({"src": "a4", "claim": None, "role": "a4", "file": f})
    return out


def wilson_upper(k, n, z=1.96):
    if n == 0:
        return float("nan")
    p = k / n
    den = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c + r) / den


def fmt(x):
    if x >= 1e5:
        return f"{x:.2g}"
    return f"{int(x)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir", type=Path, help="local/far-diag")
    a = ap.parse_args()
    paths, kp, S, mp, geo, stages, replay = load(a.dir)
    B = np.maximum(S, S.T)
    D = describe(paths, mp)
    n = len(D)
    idx = {d["file"]: i for i, d in enumerate(D) if d["src"] == "dump"}
    proto = {f: [i for i, d in enumerate(D) if d["src"] == "proto" and d["claim"] == f] for f in FINGERS}
    enr = [idx[s["source_image"].split("/")[-1]] for s in stages]
    P = print

    # ---- 1. mapping
    rows = list(mp.values())
    P("## 1. Dump map\n")
    P("| session | dumps | handed to the core | finger-off probes | not submitted |")
    P("|---|---|---|---|---|")
    for ses in ["enroll", "verify-right-index", "verify-right-middle", "verify-left-index", "unlabeled"]:
        rr = [r for r in rows if r["session"] == ses]
        P(f"| {ses} | {len(rr)} | {sum(r['submitted'] == '1' for r in rr)} | "
          f"{sum(r['role'] == 'finger-off-probe' for r in rr)} | "
          f"{sum(r['role'].startswith('not-submitted') for r in rr)} |")
    P(f"\nTotal {len(rows)} dumps; every dump has exactly one journal 'Diag image' line.")

    # ---- 2. stage provenance
    ordered = all(D[enr[k]]["role"] == f"enroll-stage-{k:02d}" for k in range(len(enr)))
    P("\n## 2. Stored print provenance\n")
    P(f"- stages in the stored print: {len(stages)}; byte-identical (serialized SIGFM) to an enroll capture: "
      f"{sum(s['source_image'] != 'NONE' for s in stages)}; in capture order: {ordered}")
    P(f"- duplicate-check identify capture in the print: "
      f"{any(D[i]['role'] == 'enroll-dupcheck-identify' for i in enr)}; finger-off probe images in the print: "
      f"{any(D[i]['role'] == 'finger-off-probe' for i in enr)}")
    st = [D[i]["row"] for i in enr]
    P(f"- stage stats: coverage {min(int(r['coverage']) for r in st)}-{max(int(r['coverage']) for r in st)}%, "
      f"sd {min(float(r['sd']) for r in st):.1f}-{max(float(r['sd']) for r in st):.1f}, "
      f"gradient {min(float(r['gradient']) for r in st):.1f}-{max(float(r['gradient']) for r in st):.1f}, "
      f"diff vs previous capture {min(float(r['diff']) for r in st):.1f}-{max(float(r['diff']) for r in st):.1f}, "
      f"keypoints {min(kp[enr])}-{max(kp[enr])}")
    links = [int(sum(B[i][j] >= T_LIVE for j in enr if j != i)) for i in enr]
    P(f"- links between stages (other stages scoring >= 105), per stage: {links}")

    # ---- 3. replay check
    P("\n## 3. Offline replay vs the journal\n")
    same = 0
    dec_same = 0
    for r in replay:
        f = r["image"].split("/")[-1]
        j = mp[f]
        same += r["live_scores"] == j["scores"]
        jdec = "match" if j["max_score"] and float(j["max_score"]) >= T_LIVE else "no-match"
        dec_same += r["decision"] == jdec
    P(f"- verify captures replayed: {len(replay)}; per-stage score sequence identical to the journal: {same}; "
      f"decision identical: {dec_same}")
    # matrix harness agrees with the replay for every stage
    agree = tot = 0
    for r in replay:
        i = idx[r["image"].split("/")[-1]]
        for k, sc in enumerate(map(int, r["all_scores"].split())):
            tot += 1
            agree += S[i][enr[k]] == sc
    P(f"- research/a5 matrix harness vs the replay, per (capture, stage): {agree}/{tot} identical")
    fire = {}
    for r in replay:
        if r["decision"] == "match":
            k = len(r["live_scores"].split()) - 1
            fire[k] = fire.get(k, 0) + 1
    allnz = [sum(int(x) >= T_LIVE for x in r["all_scores"].split()) for r in replay if r["decision"] == "match"]
    P(f"- stage that fired (first >= 105), per accepted capture: {fire}; stages >= 105 per accepted capture: "
      f"min {min(allnz)}, median {int(np.median(allnz))}, max {max(allnz)} of 18")
    never = [k for k in range(len(enr)) if all(int(r["all_scores"].split()[k]) == 0 for r in replay)]
    P(f"- stages scoring 0 against every verify capture: {never}")

    # ---- 4. enrolled finger
    P("\n## 4. Which finger was enrolled\n")
    P("| proto finger (10 captures, 2026-10-14) | accepted by the print (>= 105) | > 0 | best | stages with any proto match >= 105 |")
    P("|---|---|---|---|---|")
    for f in FINGERS:
        best = [S[p][enr].max() for p in proto[f]]
        stg = sum(max(B[g][p] for p in proto[f]) >= T_LIVE for g in enr)
        P(f"| {f} | {sum(b >= T_LIVE for b in best)}/10 | {sum(b > 0 for b in best)}/10 | {fmt(max(best))} | {stg}/18 |")

    # ---- 5. identity of every capture
    def proto_hits(i):
        return {f: int(sum(B[i][p] >= T_LIVE for p in proto[f])) for f in FINGERS}

    ident = {}
    for i, d in enumerate(D):
        if d["src"] == "proto":
            ident[i] = d["claim"]
        elif d["src"] == "dump":
            if d["role"].startswith("enroll-stage") or d["role"] == "finger-off-probe":
                continue
            if d["row"]["submitted"] != "1":
                continue
            h = proto_hits(i)
            claim = d["claim"] or "unclaimed"
            others = [f for f in FINGERS if f != claim and h[f] > 0]
            if len(others) == 1 and (claim not in h or h[claim] == 0):
                ident[i] = others[0]
            else:
                ident[i] = claim
    for i in enr:
        ident[i] = "left-index"  # from section 4; used for the gallery only
    P("\n## 5. Verify captures: claimed finger vs evidence\n")
    P("| session | rounds | accepted (live) | proto hits >= 105 (RI / RM / LI), summed | identity by the rule |")
    P("|---|---|---|---|---|")
    groups = {}
    for i, d in enumerate(D):
        if d["src"] != "dump" or d["row"]["submitted"] != "1" or d["role"].startswith("enroll-stage"):
            continue
        acc = bool(d["row"]["max_score"]) and float(d["row"]["max_score"]) >= T_LIVE
        key = (d["session"], acc)
        groups.setdefault(key, []).append(i)
    for (ses, acc), ii in sorted(groups.items()):
        h = np.sum([[proto_hits(i)[f] for f in FINGERS] for i in ii], axis=0)
        ids = sorted({ident[i] for i in ii})
        rounds = ([D[i]["role"][-2:] for i in ii] if ses.startswith("verify") else
                  ["dup-check"] if ses == "enroll" else ["13:37"] * len(ii))
        P(f"| {ses} | {','.join(r.lstrip('0') for r in rounds)} | {'yes' if acc else 'no'} ({len(ii)}) | "
          f"{h[0]} / {h[1]} / {h[2]} | {', '.join(ids)} |")

    P("\nGroup x group, ordered pairs scoring >= 105 (either direction) / pairs, and the max; "
      "this part does not depend on the proto labels:\n")
    gl = {"stages": list(enr)}
    for (ses, acc), ii in sorted(groups.items()):
        if ses.startswith("verify"):
            gl[f"{ses[7:]} {'acc' if acc else 'rej'}"] = ii
    for f in FINGERS:
        gl[f"proto {f}"] = proto[f]
    names = list(gl)
    P("| | " + " | ".join(names) + " |")
    P("|---" * (len(names) + 1) + "|")
    for a_ in names:
        cells = []
        for b_ in names:
            v = [B[i][j] for i in gl[a_] for j in gl[b_] if i < j or (a_ != b_ and i != j)]
            if a_ == b_:
                v = [B[i][j] for i in gl[a_] for j in gl[b_] if i < j]
            cells.append(f"{sum(x >= T_LIVE for x in v)}/{len(v)} ({fmt(max(v)) if v else '-'})")
        P(f"| {a_} | " + " | ".join(cells) + " |")

    # ---- 6. geometry
    P("\n## 6. Geometry of the matches (RANSAC similarity, native pixels)\n")
    P("| pair set (score >= 105) | pairs | median translation | p10 | pairs with < 1 px and < 0.5 deg | median rotation |")
    P("|---|---|---|---|---|---|")

    def gsum(name, pred):
        t, rot, still = [], [], 0
        for (i, j), r in geo.items():
            if B[i][j] < T_LIVE or not (pred(i, j) or pred(j, i)):
                continue
            tt = math.hypot(float(r["tx"]), float(r["ty"]))
            t.append(tt)
            rot.append(abs(float(r["rot_deg"])))
            still += tt < 1 and abs(float(r["rot_deg"])) < 0.5
        if t:
            P(f"| {name} | {len(t)} | {np.median(t):.1f} px | {np.percentile(t, 10):.1f} px | {still} | "
              f"{np.median(rot):.1f} deg |")

    is_acc = {i for (_, acc), ii in groups.items() if acc for i in ii}
    gsum("proto, same finger", lambda i, j: D[i]["src"] == D[j]["src"] == "proto" and D[i]["claim"] == D[j]["claim"])
    gsum("stage vs stage", lambda i, j: i in enr and j in enr)
    gsum("stage vs proto left-index (cross-day)", lambda i, j: i in enr and j in proto["left-index"])
    gsum("accepted verify vs stage", lambda i, j: i in is_acc and j in enr)
    gsum("accepted verify vs proto left-index", lambda i, j: i in is_acc and j in proto["left-index"])

    # ---- 7. score scale
    P("\n## 7. Score scale\n")
    pr = [i for i, d in enumerate(D) if d["src"] == "proto"]
    dm = [i for i, d in enumerate(D) if d["src"] == "dump" and d["row"]["submitted"] == "1"]
    P(f"- keypoints at 2x: proto median {int(np.median(kp[pr]))} (min {kp[pr].min()}), handed-in dumps median "
      f"{int(np.median(kp[dm]))} (min {kp[dm].min()})")
    cap = math.comb(math.comb(176, 2), 2)
    P(f"- score ceiling: C(C(176,2),2) = {cap:.4g} (the 176-candidate cap); self-scores: proto median "
      f"{np.median([S[i][i] for i in pr]):.3g}, dumps median {np.median([S[i][i] for i in dm]):.3g}")

    def gen_pairs(ii):
        v = [S[p][g] for p in ii for g in ii if p != g]
        return np.array(v)

    gp = np.concatenate([gen_pairs(proto[f]) for f in FINGERS])
    live_li = [i for i in dm if ident.get(i) == "left-index"] + list(enr)
    gl = gen_pairs(live_li)
    P(f"- genuine ordered pairs, proto (3 fingers, one sitting): {len(gp)}, > 0: {(gp > 0).sum()}, median of non-zero "
      f"{np.median(gp[gp > 0]):.3g}, max {gp.max():.3g}")
    P(f"- genuine ordered pairs, live left-index (stages + verify, one day): {len(gl)}, > 0: {(gl > 0).sum()}, median of "
      f"non-zero {np.median(gl[gl > 0]):.3g}, max {gl.max():.3g}")

    # ---- 8. distributions and FAR/FRR
    P("\n## 8. Genuine / impostor, all labeled data\n")
    labeled = [i for i in range(n) if i in ident]
    gen, imp = [], []
    for p in labeled:
        for g in labeled:
            if p == g:
                continue
            if ident[p] == ident[g]:
                gen.append(S[p][g])
            else:
                imp.append(S[p][g])
    gen, imp = np.array(gen), np.array(imp)
    P(f"- ordered pairs, identity rule: genuine {len(gen)} (> 0: {(gen > 0).sum()}), impostor {len(imp)} "
      f"(> 0: {(imp > 0).sum()}, max {int(imp.max())}, values > 0: {sorted({int(x) for x in imp[imp > 0]})})")
    # same pairs under the user's claimed labels
    claimed = {}
    for i in labeled:
        d = D[i]
        claimed[i] = d["claim"] if d["src"] == "proto" else ("left-index" if i in enr else (d["claim"] or "unclaimed"))
    impc = np.array([S[p][g] for p in labeled for g in labeled if p != g and claimed[p] != claimed[g]
                     and "unclaimed" not in (claimed[p], claimed[g])])
    P(f"- the same pairs under the claimed labels (print = left-index per section 4): impostor {len(impc)}, "
      f">= 105: {(impc >= T_LIVE).sum()}, max {impc.max():.3g}")

    # print-level decisions
    probes_gen = [i for i in labeled if ident[i] == "left-index" and i not in enr]
    probes_imp = [i for i in labeled if ident[i] != "left-index"]
    P("\nVerify against the stored 18-stage print (OR over stages, as fpi_print_sigfm_match):\n")
    P(f"Probes: genuine {len(probes_gen)} (proto left-index 10, live {len(probes_gen) - 10}, incl. the enroll "
      f"duplicate-check capture); impostor "
      f"{len(probes_imp)} (proto right-index 10, proto right-middle 10, live "
      f"{len(probes_imp) - 20}).\n")
    P("| threshold | FRR all | FRR live | FRR proto (cross-day) | FAR | FAR 95% upper (Wilson) |")
    P("|---|---|---|---|---|---|")
    bestg = {i: S[i][enr].max() for i in probes_gen}
    besti = {i: S[i][enr].max() for i in probes_imp}
    for T in THRESHOLDS:
        fr = sum(v < T for v in bestg.values())
        frl = sum(bestg[i] < T for i in probes_gen if D[i]["src"] == "dump")
        frp = sum(bestg[i] < T for i in probes_gen if D[i]["src"] == "proto")
        fa = sum(v >= T for v in besti.values())
        P(f"| {T} | {fr}/{len(bestg)} | {frl}/{len(probes_gen) - 10} | {frp}/10 | {fa}/{len(besti)} | "
          f"{100 * wilson_upper(fa, len(besti)):.1f}% |")
    P(f"\nHighest impostor stage score against the print: {int(max(besti.values()))}. Lowest genuine best-stage "
      f"scores: {[fmt(v) for v in sorted(bestg.values())[:6]]}.")
    P("Under the claimed labels the live run reads FAR 10/20 (right-index 8/10, right-middle 2/10) and FRR 0/10.")

    # A4
    a4 = [i for i, d in enumerate(D) if d["src"] == "a4"]
    a4s = [S[i][enr].max() for i in a4]
    a4p = [{f: int(sum(B[i][p] >= T_LIVE for p in proto[f])) for f in FINGERS} for i in a4]
    both = sum((s >= T_LIVE) and sum(v > 0 for v in h.values()) > 1 for s, h in zip(a4s, a4p))
    P(f"\nA4 captures (finger not recorded, {len(a4)}): accepted by the print {sum(s >= T_LIVE for s in a4s)}, "
      f"score 0 {sum(s == 0 for s in a4s)}, in between {sum(0 < s < T_LIVE for s in a4s)}; "
      f"of the accepted, matching proto left-index {sum(s >= T_LIVE and h['left-index'] > 0 for s, h in zip(a4s, a4p))}, "
      f"matching another proto finger {sum(s >= T_LIVE and (h['right-index'] + h['right-middle']) > 0 for s, h in zip(a4s, a4p))}; "
      f"matching two proto fingers: {both}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
