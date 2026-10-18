"""Tests for research/far-diag on synthetic journals and logs (no biometric data)."""

import csv
import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _jline(t: dt.datetime, msg: str) -> str:
    us = int(t.timestamp() * 1e6)
    return json.dumps({"MESSAGE": msg, "__REALTIME_TIMESTAMP": str(us), "__MONOTONIC_TIMESTAMP": "1"})


def _diag(seq, tag, name):
    return (f"Diag image {seq} {tag}: mean 128.5, sd 45.00, coverage 90%, gradient 25.0, "
            f"diff vs last capture 50.0, 100 ms after 02, file {name}")


@pytest.fixture
def scene(tmp_path):
    day = dt.datetime(2026, 10, 15, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=-3)))
    enroll_log = tmp_path / "enroll.log"
    enroll_log.write_text("# a5-enroll 2026-10-15T12:00:00-03:00 user u\n12:00:30.000 [note] end\n")
    vstart = day.replace(hour=13)
    verify_log = tmp_path / "verify.log"
    verify_log.write_text(
        "# a5-verify 2026-10-15T13:00:00-03:00 user u\n"
        "13:00:01.000 [prompt] Round 1/2: touch with your RIGHT INDEX finger, then lift\n"
        "13:00:20.000 [score] round 1: no-match (verify-no-match), retries 0, sigfm scores 0/105 0/105\n"
        "13:00:20.000 [score] round 2: match (verify-match), retries 1, sigfm scores 500/105\n"
    )
    t = day.replace(second=5)
    lines = []

    def add(msg, dt_s=0.1):
        nonlocal t
        t += dt.timedelta(seconds=dt_s)
        lines.append(_jline(t, msg))

    # enroll: dup-check capture, one probe, then two stages, one failed capture
    add(_diag(1, "capture", "000001-a-capture.pgm"))
    add("Image captured (diff vs last capture -)")
    add(_diag(2, "probe01", "000002-a-probe01.pgm"))
    add(_diag(3, "capture", "000003-a-capture.pgm"))
    add("Capture failed: the image reply carries an error, asking for a retry")
    add(_diag(4, "capture", "000004-a-capture.pgm"))
    add("Image captured (diff vs last capture 50.0)")
    add(_diag(5, "probe01", "000005-a-probe01.pgm"))
    add("Finger-off probe 1: new touch (diff 50.0 >= 30), taking this image as the capture")
    add("Image captured (diff vs last capture 50.0)")
    # verify
    t = vstart.replace(second=2)
    add(_diag(1, "capture", "000001-b-capture.pgm"))
    add("Image captured (diff vs last capture -)")
    add("sigfm score 0/105")
    add("sigfm score 0/105")
    add(_diag(2, "capture", "000002-b-capture.pgm"))
    add("Same touch as the last capture (diff 5.0 < 30), not submitted; waiting for finger off")
    add(_diag(3, "capture", "000003-b-capture.pgm"))
    add("Image captured (diff vs last capture 40.0)")
    add("sigfm score 500/105")
    journal = tmp_path / "j.json"
    journal.write_text("\n".join(lines) + "\n")
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    for ln in lines:
        m = json.loads(ln)["MESSAGE"]
        if m.startswith("Diag image"):
            (dumps / m.split("file ")[1]).write_bytes(b"")
    return {"journal": journal, "dumps": dumps, "enroll": enroll_log, "verify": verify_log, "tmp": tmp_path}


def test_map_dumps_roles_and_scores(scene):
    out = scene["tmp"] / "map.csv"
    r = subprocess.run(
        [sys.executable, str(HERE / "map_dumps.py"), "--journal", str(scene["journal"]), "--dumps",
         str(scene["dumps"]), "--enroll-log", str(scene["enroll"]), "--verify-log", str(scene["verify"]),
         "--out", str(out)],
        capture_output=True, text=True, check=True)
    assert "on disk only 0, journal only 0" in r.stdout
    assert "verify-right-index: 3 dumps, 2 submitted, score check vs log: OK" in r.stdout
    rows = {row["file"]: row for row in csv.DictReader(out.open())}
    assert rows["000001-a-capture.pgm"]["role"] == "enroll-dupcheck-identify"
    assert rows["000002-a-probe01.pgm"]["role"] == "finger-off-probe"
    assert rows["000003-a-capture.pgm"]["role"].startswith("not-submitted (Capture failed")
    assert rows["000004-a-capture.pgm"]["role"] == "enroll-stage-00"
    # a probe promoted to the capture becomes the next stage
    assert rows["000005-a-probe01.pgm"]["role"] == "enroll-stage-01"
    assert rows["000001-b-capture.pgm"]["role"] == "verify-round-01"
    assert rows["000001-b-capture.pgm"]["scores"] == "0 0"
    assert rows["000002-b-capture.pgm"]["role"].startswith("not-submitted (Same touch")
    assert rows["000003-b-capture.pgm"]["role"] == "verify-round-02"
    assert rows["000003-b-capture.pgm"]["max_score"] == "500"


def test_map_dumps_flags_score_mismatch(scene):
    scene["verify"].write_text(scene["verify"].read_text().replace("500/105", "501/105"))
    out = scene["tmp"] / "map.csv"
    r = subprocess.run(
        [sys.executable, str(HERE / "map_dumps.py"), "--journal", str(scene["journal"]), "--dumps",
         str(scene["dumps"]), "--enroll-log", str(scene["enroll"]), "--verify-log", str(scene["verify"]),
         "--out", str(out)],
        capture_output=True, text=True, check=True)
    assert "MISMATCH" in r.stdout


def test_wilson_upper():
    pytest.importorskip("numpy")
    fa = _load("far_analysis")
    assert fa.wilson_upper(0, 31) == pytest.approx(0.110, abs=0.001)
    assert fa.wilson_upper(10, 20) > 0.5
    assert fa.wilson_upper(0, 0) != fa.wilson_upper(0, 0)  # nan
