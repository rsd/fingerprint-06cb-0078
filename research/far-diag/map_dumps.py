#!/usr/bin/env python3
"""Map every synaptics0078 debug dump (FP_SYNAPTICS0078_DUMP_DIR) to its role.

Inputs (all local, biometric-derived, never tracked):
  --journal  journalctl -u fprintd -o json (fields MESSAGE, __REALTIME_TIMESTAMP,
             __MONOTONIC_TIMESTAMP)
  --dumps    the dump directory (only file NAMES are used here, never pixels)
  --enroll-log / --verify-log  the a5-enroll.sh / a5-verify.sh logs

Output: a CSV (one row per dump) with the session, the role (enroll stage,
duplicate-check identify, verify round, retry, finger-off probe), the driver's
image stats from its "Diag image" line, whether the image reached the core,
and the sigfm scores the core logged for it.

Roles come from the driver's log-line contract (research/steps/a5b.md):
"Image captured" hands the last capture to the core; "Capture failed" and
"Same touch ... not submitted" do not; "taking this image as the capture"
promotes a probe image.  "sigfm score" lines belong to the last image handed
to the core.  Verify rounds are numbered in order inside each a5-verify run,
and cross-checked against the run's "[score]" summary lines.
"""

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

DIAG_RE = re.compile(
    r"^Diag image (\d+) (\S+): mean ([\d.]+), sd ([\d.]+), coverage (\d+)%, gradient ([\d.]+), "
    r"diff vs last capture (\S+), (-?\d+) ms after 02, file (\S+)"
)
SCORE_RE = re.compile(r"^sigfm score (-?\d+)/(-?\d+)")
LOGSCORE_RE = re.compile(r"\[score\] round (\d+): (\S+) \(([^)]*)\), retries (\d+), sigfm scores (.*)$")
FINGER_RE = re.compile(r"touch with your (\w+ \w+) finger", re.IGNORECASE)


def log_window(path: Path):
    """(start, end, finger, rounds) of an a5 helper log; times are aware, in the log's offset."""
    lines = path.read_text(errors="replace").splitlines()
    m = re.match(r"# a5-\w+ (\S+) ", lines[0])
    start = dt.datetime.fromisoformat(m.group(1))  # aware: the log carries its UTC offset
    last = None
    for ln in lines:
        mm = re.match(r"^(\d\d):(\d\d):(\d\d)\.(\d+) ", ln)
        if mm:
            last = start.replace(hour=int(mm[1]), minute=int(mm[2]), second=int(mm[3]))
    finger = None
    for ln in lines:
        mf = FINGER_RE.search(ln)
        if mf:
            finger = mf.group(1).lower().replace(" ", "-")
            break
    rounds = []
    for ln in lines:
        ms = LOGSCORE_RE.search(ln)
        if ms:
            scores = [int(s.split("/")[0]) for s in ms.group(5).split()] if ms.group(5) != "none" else []
            rounds.append({"round": int(ms.group(1)), "outcome": ms.group(2), "retries": int(ms.group(4)),
                           "scores": scores})
    return start, last + dt.timedelta(seconds=2), finger, rounds


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", required=True, type=Path)
    ap.add_argument("--dumps", required=True, type=Path)
    ap.add_argument("--enroll-log", required=True, type=Path)
    ap.add_argument("--verify-log", action="append", default=[], type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()

    sessions = []
    s, e, _, _ = log_window(a.enroll_log)
    sessions.append({"name": "enroll", "start": s, "end": e, "finger": None, "rounds": []})
    for p in a.verify_log:
        s, e, f, r = log_window(p)
        sessions.append({"name": f"verify-{f}", "start": s, "end": e, "finger": f, "rounds": r})

    local_tz = sessions[0]["start"].tzinfo

    def session_of(t):
        for ss in sessions:
            if ss["start"] <= t <= ss["end"]:
                return ss
        return None

    rows = []
    last_capture = None  # the last capture-tagged dump row (not yet decided)
    last_submitted = None
    for line in a.journal.read_text().splitlines():
        j = json.loads(line)
        msg = j.get("MESSAGE")
        if not isinstance(msg, str):
            continue
        t = dt.datetime.fromtimestamp(int(j["__REALTIME_TIMESTAMP"]) / 1e6, tz=local_tz)
        m = DIAG_RE.match(msg)
        if m:
            ss = session_of(t)
            row = {
                "file": m.group(9), "seq": int(m.group(1)), "tag": m.group(2),
                "err": m.group(9).endswith("-err.pgm"),
                "mean": float(m.group(3)), "sd": float(m.group(4)), "coverage": int(m.group(5)),
                "gradient": float(m.group(6)), "diff": m.group(7), "ms_after_02": int(m.group(8)),
                "time": t.strftime("%H:%M:%S.%f")[:-3],
                "session": ss["name"] if ss else "unlabeled", "submitted": False, "role": "",
                "scores": [],
            }
            rows.append(row)
            if row["tag"] == "capture":
                last_capture = row
                row["role"] = "capture-pending"
            else:
                row["role"] = "finger-off-probe"
            continue
        if msg.startswith("Image captured"):
            if last_capture is None:
                print("warning: Image captured without a capture dump", file=sys.stderr)
                continue
            last_capture["submitted"] = True
            last_capture["role"] = "submitted"
            last_submitted = last_capture
            last_capture = None
        elif msg.startswith(("Capture failed", "Same touch as the last capture")):
            if last_capture is not None:
                last_capture["role"] = "not-submitted (" + msg.split(":")[0].split("(")[0].strip() + ")"
                last_capture = None
        elif "taking this image as the capture" in msg:
            # the probe just dumped becomes the capture
            probe = rows[-1]
            probe["role"] = "capture-pending"
            last_capture = probe
        else:
            ms = SCORE_RE.match(msg)
            if ms and last_submitted is not None:
                last_submitted["scores"].append(int(ms.group(1)))

    # Assign roles to submitted images, per session.
    for ss in sessions:
        sub = [r for r in rows if r["session"] == ss["name"] and r["submitted"]]
        if ss["name"] == "enroll":
            # fprintd's duplicate check (identify) comes first, then the 18 stages.
            if sub:
                sub[0]["role"] = "enroll-dupcheck-identify"
            for i, r in enumerate(sub[1:]):
                r["role"] = f"enroll-stage-{i:02d}"
        else:
            for i, r in enumerate(sub):
                r["role"] = f"verify-round-{i + 1:02d}"
            got = [r["scores"] for r in sub]
            want = [r["scores"] for r in ss["rounds"]]
            ss["check"] = "OK" if got == want else f"MISMATCH journal {len(got)} rounds vs log {len(want)}"
    for r in rows:
        if r["session"] == "unlabeled" and r["submitted"]:
            r["role"] = "unlabeled-submitted"
        if r["role"] == "capture-pending":
            r["role"] = "capture-undecided"

    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "seq", "tag", "err", "time", "session", "role", "submitted", "mean", "sd",
                    "coverage", "gradient", "diff", "ms_after_02", "n_scores", "max_score", "scores"])
        for r in rows:
            w.writerow([r["file"], r["seq"], r["tag"], int(r["err"]), r["time"], r["session"], r["role"],
                        int(r["submitted"]), r["mean"], r["sd"], r["coverage"], r["gradient"], r["diff"],
                        r["ms_after_02"], len(r["scores"]), max(r["scores"]) if r["scores"] else "",
                        " ".join(map(str, r["scores"]))])

    on_disk = {p.name for p in a.dumps.glob("*.pgm")}
    named = {r["file"] for r in rows}
    print(f"dumps on disk {len(on_disk)}, in journal {len(named)}, on disk only {len(on_disk - named)}, "
          f"journal only {len(named - on_disk)}")
    for ss in sessions:
        sub = [r for r in rows if r["session"] == ss["name"]]
        print(f"{ss['name']}: {len(sub)} dumps, {sum(r['submitted'] for r in sub)} submitted, "
              f"score check vs log: {ss.get('check', 'n/a')}")
    un = [r for r in rows if r["session"] == "unlabeled"]
    print(f"unlabeled: {len(un)} dumps, {sum(r['submitted'] for r in un)} submitted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
