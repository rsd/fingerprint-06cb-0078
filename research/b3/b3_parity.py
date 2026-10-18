#!/usr/bin/env python3
"""B3 step 1: prove that the offline scorer reproduces fprintd's live scores.

Data set (local, biometric-derived, read-only here; see research/steps/far-diag.md):
  local/far-diag/map.csv     research/far-diag/map_dumps.py: dump -> role, journal scores
  local/far-diag/stages.tsv  far_replay stages: stored stage k -> source dump (cross-check)
  local/diag-fprintd/*.pgm   the raw 144x56 dumps of the 2026-10-15 enroll and verify

The 18 enroll-stage images (stage order from map.csv roles) and every submitted
verify capture with journal scores are scored by the B3 scorer, the A5 harness
(research/a5/sigfm_matrix, --scale 2). Parity is exact when, for every capture:
  - each journal-logged stage score equals score[probe][stage_k] (integers);
  - the number of logged stages equals the early-exit position under 105
    (fpi_print_sigfm_match returns at the first stage >= threshold);
  - the accept/reject decision is the same.
It is never exact vacuously: it needs at least one capture, at least one
compared stage score in every capture, and the expected size of the data set
(EXPECTED_CAPTURES captures with EXPECTED_STAGE_SCORES logged stage scores for
the 2026-10-15 set; --expect-captures / --expect-stage-scores for another one).

Output: local/b3/results/parity.json (0600) with the harness sha256 and the
fork HEAD and the map.csv sha256, and one line on stdout. Exit 0 exact, 1 mismatch, 4 data set absent.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import b3_common as c

STAGE_RE = re.compile(r"^enroll-stage-(\d+)$")
EXIT_MISMATCH = 1
EXIT_NO_DATA = 4
# Size of the 2026-10-15 data set (local/far-diag/map.csv): verify captures with
# journal scores, and the stage scores fprintd logged for them.
EXPECTED_CAPTURES = 32
EXPECTED_STAGE_SCORES = 219


class DataError(Exception):
    pass


def early_exit_len(row: list[int], threshold: int = c.THRESHOLD) -> int:
    """Number of stages fpi_print_sigfm_match scores: up to the first >= threshold."""
    for k, s in enumerate(row):
        if s >= threshold:
            return k + 1
    return len(row)


def compare(captures: list[dict], offline: dict, threshold: int = c.THRESHOLD,
            expect: tuple[int, int] | None = None) -> dict:
    """Compare journal scores with offline per-stage scores.

    captures: [{"name": str, "logged": [int, ...]}] in any order (logged = the
              stage scores fprintd's journal shows for that capture, stage order).
    offline:  name -> list of the per-stage offline scores, or None when the
              scorer's keypoint gate rejected the probe.
    expect:   (captures, stage scores) the data set must have, or None.
    Exact needs no mismatch, >= 1 capture and >= 1 compared stage score per capture.
    """
    mismatches = []
    n_scores = 0
    if not captures:
        mismatches.append({"capture": "data set", "kind": "no-captures",
                           "message": "no verify capture with journal scores: nothing was compared"})
    for cap in captures:
        name, logged = cap["name"], [int(s) for s in cap["logged"]]
        n_scores += len(logged)
        row = offline.get(name)
        if row is None:
            mismatches.append({"capture": name, "kind": "offline-rejected", "live": logged})
            continue
        row = [int(s) for s in row]
        if min(len(logged), len(row)) == 0:
            mismatches.append({"capture": name, "kind": "nothing-compared", "live_len": len(logged),
                               "offline_len": len(row)})
        for k in range(min(len(logged), len(row))):
            if logged[k] != row[k]:
                mismatches.append({"capture": name, "kind": "score", "stage": k,
                                   "live": logged[k], "offline": row[k]})
        if len(logged) > len(row):
            mismatches.append({"capture": name, "kind": "too-many-live-stages",
                               "live_len": len(logged), "offline_len": len(row)})
        exp = early_exit_len(row, threshold)
        if len(logged) != exp:
            mismatches.append({"capture": name, "kind": "early-exit", "live_len": len(logged),
                               "offline_len": exp})
        live_acc = any(s >= threshold for s in logged)
        off_acc = any(s >= threshold for s in row)
        if live_acc != off_acc:
            mismatches.append({"capture": name, "kind": "decision", "live": "match" if live_acc else "no-match",
                               "offline": "match" if off_acc else "no-match"})
    if expect is not None and (len(captures), n_scores) != tuple(expect):
        mismatches.append({"capture": "data set", "kind": "size", "message": (
            f"expected {expect[0]} captures with {expect[1]} stage scores, found {len(captures)} with "
            f"{n_scores}; for another data set pass --expect-captures {len(captures)} "
            f"--expect-stage-scores {n_scores}")})
    exact = not mismatches and len(captures) > 0
    out = {"exact": exact, "n_captures": len(captures), "n_stage_scores": n_scores,
           "threshold": threshold, "mismatches": mismatches}
    if expect is not None:
        out["expected_captures"], out["expected_stage_scores"] = int(expect[0]), int(expect[1])
    return out


def select(map_rows: list[dict], stages_tsv: list[dict] | None) -> tuple[list[str], list[dict]]:
    """(stage files in stage order, verify captures with journal scores) from map.csv rows."""
    stages = {}
    for r in map_rows:
        m = STAGE_RE.match(r["role"])
        if m and r["submitted"] == "1":
            k = int(m.group(1))
            if k in stages:
                raise DataError(f"stage {k} appears twice in map.csv")
            stages[k] = r["file"]
    if sorted(stages) != list(range(len(stages))) or not stages:
        raise DataError(f"map.csv stages are not 0..n-1: {sorted(stages)}")
    order = [stages[k] for k in range(len(stages))]
    if stages_tsv is not None:
        tsv = {int(r["stage"]): Path(r["source_image"]).name for r in stages_tsv}
        if [tsv.get(k) for k in range(len(order))] != order or len(tsv) != len(order):
            raise DataError("stages.tsv and map.csv disagree on the stage images")
    caps = []
    for r in map_rows:
        if r["submitted"] != "1" or r["role"].startswith("enroll") or int(r["n_scores"] or 0) == 0:
            continue
        logged = [int(s) for s in r["scores"].split()]
        if len(logged) != int(r["n_scores"]):
            raise DataError(f"{r['file']}: n_scores {r['n_scores']} but {len(logged)} scores")
        caps.append({"name": r["file"], "logged": logged, "role": r["role"], "session": r["session"]})
    if not caps:
        raise DataError("map.csv has no submitted verify capture with journal scores")
    return order, caps


def summary_line(res: dict) -> str:
    if res["exact"]:
        return f"parity: exact ({res['n_captures']} captures, {res['n_stage_scores']} stage scores)"
    parts = []
    for m in res["mismatches"][:20]:
        d = m.get("message") or ", ".join(f"{k} {v}" for k, v in m.items() if k not in ("capture", "kind"))
        parts.append(f"  {m['capture']}: {m['kind']}: {d}")
    more = len(res["mismatches"]) - 20
    tail = [f"  ... and {more} more"] if more > 0 else []
    head = (f"parity: MISMATCH ({len(res['mismatches'])} in {res['n_captures']} captures, "
            f"{res['n_stage_scores']} stage scores)")
    return "\n".join([head, *parts, *tail])


def default_fork() -> Path:
    return Path(os.environ.get("LIBFPRINT_FORK") or Path.home() / "Devel/Linux/Synaptics/libfprint")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--map", type=Path, default=c.ROOT / "local/far-diag/map.csv")
    ap.add_argument("--stages-tsv", type=Path, default=c.ROOT / "local/far-diag/stages.tsv")
    ap.add_argument("--dumps", type=Path, default=c.ROOT / "local/diag-fprintd")
    ap.add_argument("--harness", type=Path, default=c.ROOT / "local/b3/bin/sigfm_matrix")
    ap.add_argument("--fork", type=Path, default=default_fork())
    ap.add_argument("--out", type=Path, default=c.ROOT / "local/b3/results/parity.json")
    ap.add_argument("--expect-captures", type=int, default=EXPECTED_CAPTURES,
                    help=f"verify captures with journal scores in the data set (default {EXPECTED_CAPTURES})")
    ap.add_argument("--expect-stage-scores", type=int, default=EXPECTED_STAGE_SCORES,
                    help=f"logged stage scores in the data set (default {EXPECTED_STAGE_SCORES})")
    ap.add_argument("--check-data", action="store_true", help="only report whether the data set is present")
    a = ap.parse_args(argv)

    if not a.map.is_file() or not a.dumps.is_dir():
        print(f"parity: data set absent ({a.map} or {a.dumps} missing)")
        return EXIT_NO_DATA
    if a.check_data:
        print("parity: data set present")
        return 0
    if not a.harness.is_file():
        print(f"parity: no harness at {a.harness} (research/a5/build_harness.sh FORK/build {a.harness})",
              file=sys.stderr)
        return 2
    rows = list(csv.DictReader(a.map.open()))
    tsv = list(csv.DictReader(a.stages_tsv.open(), delimiter="\t")) if a.stages_tsv.is_file() else None
    try:
        order, caps = select(rows, tsv)
    except DataError as e:
        print(f"parity: data set inconsistent: {e}", file=sys.stderr)
        return 2
    paths = [a.dumps / f for f in order] + [a.dumps / cap["name"] for cap in caps]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"parity: {len(missing)} dumps missing, e.g. {missing[0]}", file=sys.stderr)
        return 2

    t0 = time.monotonic()
    out = c.run_harness(a.harness, paths, a.out.with_name("parity-matrix.json"))
    elapsed = time.monotonic() - t0
    n = len(order)
    offline = {}
    for i, cap in enumerate(caps):
        img = out["images"][n + i]
        offline[cap["name"]] = None if img["rejected"] else out["scores"][n + i][:n]
    res = compare(caps, offline, expect=(a.expect_captures, a.expect_stage_scores))
    stage_rej = [k for k in range(n) if out["images"][k]["rejected"]]
    if stage_rej:
        res["exact"] = False
        res["mismatches"].append({"capture": "stages", "kind": "stage-rejected-offline", "stages": stage_rej})
    fork = c.fork_head(a.fork)
    res.update({
        "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "scale": c.SCALE, "n_stages": n, "harness": str(a.harness), "harness_sha256": c.sha256_file(a.harness),
        "fork": str(a.fork), "fork_head": fork["head"], "fork_dirty": fork["dirty"],
        "package": c.package_version(), "map_csv_sha256": c.sha256_file(a.map),
        "harness_seconds": round(elapsed, 2), "match_ms_median": c.match_ms_median(out),
        "comparisons": len(paths) ** 2,
    })
    c.write_json(a.out, res)
    print(summary_line(res))
    return 0 if res["exact"] else EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
