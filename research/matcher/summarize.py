#!/usr/bin/env python3
"""Combine the results of nbis_libfprint.py and sift_matcher.py into one markdown digest.

Usage: summarize.py OUT [--detail v1,v2,...] > digest.md

OUT is the results directory both scripts wrote to (control-<size>/ subfolders
from run_all.sh are summarised too). The digest holds aggregate numbers only
(no per-image data), so it can go into a report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fpscore import fmt, pct  # noqa: E402


def load(out: Path):
    rows = []
    for a in sorted(out.glob("*.analysis.json")):
        r = json.loads(a.with_name(a.name.replace(".analysis", "")).read_text())
        ana = json.loads(a.read_text())
        family = "NBIS" if a.name.startswith("nbis-") else (
            "SIGFM" if r.get("scorer") == "sigfm" else "PR #288-style")
        defaults = [40, 25] if family == "NBIS" else ([24, 10] if family == "SIGFM" else [6])
        feats = ana.get("minutiae") or ana.get("keypoints") or {}
        rows.append({"family": family, "variant": r["variant"], "res": r, "ana": ana,
                     "defaults": defaults, "features": feats})
    import nbis_libfprint
    import sift_matcher
    known = list(nbis_libfprint.VARIANTS) + list(sift_matcher.VARIANTS)
    order = {"NBIS": 0, "PR #288-style": 1, "SIGFM": 2}
    rows.sort(key=lambda x: (order[x["family"]],
                             known.index(x["variant"]) if x["variant"] in known else len(known)))
    return rows


def at(block, t):
    for a in block.get("at", []):
        if a["threshold"] == t:
            return a
    return None


def main_table(rows) -> str:
    out = ["| family | variant | features/image (median) | genuine > 0 | genuine median / max | impostor max | "
           "pair FRR @0FA | enroll-3 FRR @0FA | enroll-5 FRR @0FA (threshold) | enroll-5 FRR / FAR at shipped threshold |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for x in rows:
        ana = x["ana"]
        g = ana["genuine_sorted"]
        e3, e5 = ana["enroll"].get("3", {}), ana["enroll"].get("5", {})
        shipped = []
        for t in x["defaults"]:
            a = at(e5, t)
            if a:
                shipped.append(f"{pct(a['frr'])} / {pct(a['far'])} @{t}")
        out.append(
            f"| {x['family']} | {x['variant']} | {fmt(x['features'].get('median', float('nan')))} | "
            f"{sum(v > 0 for v in g)}/{len(g)} | {fmt(ana['genuine']['median'])} / {fmt(ana['genuine']['max'])} | "
            f"{fmt(ana['impostor']['max'])} | {pct(ana['zero_fa']['frr'])} | "
            f"{pct(e3.get('zero_fa', {}).get('frr', float('nan')))} | "
            f"{pct(e5.get('zero_fa', {}).get('frr', float('nan')))} ({fmt(e5.get('zero_fa', {}).get('threshold', float('nan')))}) | "
            f"{'; '.join(shipped)} |")
    return "\n".join(out) + "\n"


def nonzero_table(rows) -> str:
    """For NBIS, where almost every score is 0: every non-zero score, listed."""
    out = ["| variant | failed to enroll (no minutiae) | self-match > 0 | non-zero genuine scores | "
           "non-zero impostor scores | cross-check (decisions / score sequences) |", "|---|---|---|---|---|---|"]
    for x in rows:
        if x["family"] != "NBIS":
            continue
        r, ana = x["res"], x["ana"]
        diag = [r["matrix"][i][i] for i in range(len(r["matrix"]))]
        gz = [int(v) for v in ana["genuine_sorted"] if v > 0]
        iz = [int(v) for v in ana["impostor_sorted"] if v > 0]
        cc = ana.get("crosscheck", {})
        out.append(f"| {x['variant']} | {len(r['failed_to_enroll'])}/{len(diag)} | "
                   f"{sum(1 for v in diag if v and v > 0)}/{len(diag)} | {', '.join(map(str, gz)) or 'none'} | "
                   f"{', '.join(map(str, iz)) or 'none'} | "
                   f"{cc.get('decision_agree', 0)}/{cc.get('n', 0)} / {cc.get('scores_agree', 0)}/{cc.get('n', 0)} |")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--detail", default="", help="variants to print in full (histograms, enrollment)")
    args = ap.parse_args()
    rows = load(args.out)
    print("## Results digest\n")
    print(main_table(rows))
    if any(x["family"] == "NBIS" for x in rows):
        print("### NBIS: every non-zero score\n")
        print(nonzero_table(rows))
    for ov in sorted(args.out.glob("overlap-*.md")):
        print(ov.read_text())
    detail = [d for d in args.detail.split(",") if d]
    for x in rows:
        if x["variant"] in detail:
            md = args.out / (("nbis-" if x["family"] == "NBIS" else "sift-") + x["variant"] + ".md")
            print(md.read_text().replace("### ", "### Detail: ", 1))
    for c in sorted(args.out.glob("control-*")):
        crow = load(c)
        if crow:
            print(f"### Control: {c.name}\n")
            print(main_table(crow))


if __name__ == "__main__":
    main()
