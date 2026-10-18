"""Tests for research/b3 on synthetic matrices and files (no hardware, no biometric data)."""

import csv
import importlib.util
import json
import math
import os
import stat
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


par = _load("b3_parity")
ana = _load("b3_analyze")

# A stand-in for sigfm_matrix: scores from FAKE_SCORES {"probe|enrolled": score} (by file name, default 0,
# diagonal 1e6), gate rejection for the names in FAKE_REJECT. Same CLI and JSON shape as the harness.
FAKE_HARNESS = r'''#!/usr/bin/python3
import json, os, sys
a = sys.argv[1:]
out = a[a.index("--out") + 1]
paths = [p for i, p in enumerate(a) if not p.startswith("--") and a[i - 1] not in ("--out", "--scale")]
table = json.loads(os.environ.get("FAKE_SCORES", "{}"))
rej = set(json.loads(os.environ.get("FAKE_REJECT", "[]")))
names = [os.path.basename(p) for p in paths]
for p in paths:
    open(p, "rb").read()
imgs = [{"path": p, "width": 288, "height": 112, "keypoints": 10 if n in rej else 300, "rejected": n in rej,
         "reason": "", "extract_ms": 1.0, "serialized_bytes": 1} for p, n in zip(paths, names)]
S, T = [], []
for pn in names:
    row, trow = [], []
    for gn in names:
        if pn in rej or gn in rej:
            row.append(None); trow.append(None)
        else:
            row.append(1000000 if pn == gn else table.get(pn + "|" + gn, 0)); trow.append(0.5)
    S.append(row); T.append(trow)
json.dump({"tool": "sigfm_matrix", "scale": 2, "images": imgs, "scores": S, "match_ms": T}, open(out, "w"))
'''

PGM = b"P5\n2 1\n255\n\x10\x20"


@pytest.fixture
def harness(tmp_path):
    h = tmp_path / "fake_matrix"
    h.write_text(FAKE_HARNESS)
    h.chmod(0o700)
    return h


# ---------------------------------------------------------------- parity comparator

def test_early_exit_len():
    assert par.early_exit_len([0, 0, 105, 7]) == 3
    assert par.early_exit_len([200]) == 1
    assert par.early_exit_len([0] * 18) == 18
    assert par.early_exit_len([104] * 18) == 18


def test_compare_exact():
    off = {"a": [0, 5, 300, 0], "b": [0, 0, 0, 104]}
    caps = [{"name": "a", "logged": [0, 5, 300]}, {"name": "b", "logged": [0, 0, 0, 104]}]
    r = par.compare(caps, off)
    assert r["exact"] and r["n_captures"] == 2 and r["n_stage_scores"] == 7 and r["mismatches"] == []


def test_compare_score_mismatch_details():
    r = par.compare([{"name": "a", "logged": [0, 6, 300]}], {"a": [0, 5, 300, 0]})
    assert not r["exact"]
    assert r["mismatches"] == [{"capture": "a", "kind": "score", "stage": 1, "live": 6, "offline": 5}]
    assert "a: score: stage 1, live 6, offline 5" in par.summary_line(r)


def test_compare_early_exit_and_decision():
    # live scored all stages and rejected; offline accepts at stage 2
    r = par.compare([{"name": "a", "logged": [0, 0, 0, 0]}], {"a": [0, 0, 150, 0]})
    kinds = [m["kind"] for m in r["mismatches"]]
    assert kinds == ["score", "early-exit", "decision"]
    ee = r["mismatches"][1]
    assert ee["live_len"] == 4 and ee["offline_len"] == 3
    # live stopped early (accept) where offline would go on and reject
    r = par.compare([{"name": "a", "logged": [105]}], {"a": [0, 0]})
    assert {m["kind"] for m in r["mismatches"]} == {"score", "early-exit", "decision"}


def test_compare_offline_rejected():
    r = par.compare([{"name": "a", "logged": [0]}], {"a": None})
    assert r["mismatches"][0]["kind"] == "offline-rejected" and not r["exact"]


def test_compare_empty_is_not_exact():
    r = par.compare([], {})
    assert not r["exact"] and r["n_captures"] == 0
    assert [m["kind"] for m in r["mismatches"]] == ["no-captures"]
    assert "nothing was compared" in par.summary_line(r)


def test_compare_capture_without_compared_score_is_not_exact():
    r = par.compare([{"name": "a", "logged": []}], {"a": [0, 0]})
    assert not r["exact"] and r["mismatches"][0]["kind"] == "nothing-compared"
    r = par.compare([{"name": "a", "logged": [0]}], {"a": []})
    assert not r["exact"] and r["mismatches"][0]["kind"] == "nothing-compared"


def test_compare_expected_size():
    caps, off = [{"name": "a", "logged": [0, 5, 300]}], {"a": [0, 5, 300, 0]}
    r = par.compare(caps, off, expect=(1, 3))
    assert r["exact"] and (r["expected_captures"], r["expected_stage_scores"]) == (1, 3)
    # a short data set: every score agrees, but it is not the expected one
    r = par.compare(caps, off, expect=(par.EXPECTED_CAPTURES, par.EXPECTED_STAGE_SCORES))
    assert not r["exact"] and [m["kind"] for m in r["mismatches"]] == ["size"]
    assert ("expected 32 captures with 219 stage scores, found 1 with 3; for another data set pass "
            "--expect-captures 1 --expect-stage-scores 3") in par.summary_line(r)


def test_select_without_captures_raises():
    rows = [r for r in _map_rows() if r["role"].startswith("enroll")]
    with pytest.raises(par.DataError, match="no submitted verify capture"):
        par.select(rows, None)


def _map_rows():
    rows = []
    for k in range(3):
        rows.append({"file": f"s{k}.pgm", "role": f"enroll-stage-{k:02d}", "submitted": "1", "session": "enroll",
                     "n_scores": "0", "scores": ""})
    rows.append({"file": "dup.pgm", "role": "enroll-dupcheck-identify", "submitted": "1", "session": "enroll",
                 "n_scores": "0", "scores": ""})
    rows.append({"file": "probe.pgm", "role": "finger-off-probe", "submitted": "0", "session": "verify-x",
                 "n_scores": "0", "scores": ""})
    rows.append({"file": "v1.pgm", "role": "verify-round-01", "submitted": "1", "session": "verify-x",
                 "n_scores": "3", "scores": "0 0 0"})
    rows.append({"file": "v2.pgm", "role": "unlabeled-submitted", "submitted": "1", "session": "unlabeled",
                 "n_scores": "1", "scores": "500"})
    return rows


def test_select_stage_order_and_captures():
    rows = _map_rows()
    rows[0], rows[2] = rows[2], rows[0]  # file order is not stage order
    order, caps = par.select(rows, [{"stage": str(k), "source_image": f"../d/s{k}.pgm"} for k in range(3)])
    assert order == ["s0.pgm", "s1.pgm", "s2.pgm"]
    assert [(c_["name"], c_["logged"]) for c_ in caps] == [("v1.pgm", [0, 0, 0]), ("v2.pgm", [500])]


def test_select_stages_tsv_disagrees():
    with pytest.raises(par.DataError):
        par.select(_map_rows(), [{"stage": "0", "source_image": "x.pgm"}])


def _parity_scene(tmp_path):
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    for n in ["s0.pgm", "s1.pgm", "s2.pgm", "v1.pgm", "v2.pgm"]:
        (dumps / n).write_bytes(PGM)
    mp = tmp_path / "map.csv"
    rows = _map_rows()
    with mp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return dumps, mp


def test_parity_cli(tmp_path, harness, monkeypatch, capsys):
    dumps, mp = _parity_scene(tmp_path)
    out = tmp_path / "res" / "parity.json"
    base = ["--map", str(mp), "--stages-tsv", str(tmp_path / "none.tsv"), "--dumps", str(dumps),
            "--harness", str(harness), "--fork", str(tmp_path), "--out", str(out)]
    argv = base + ["--expect-captures", "2", "--expect-stage-scores", "4"]
    monkeypatch.setenv("FAKE_SCORES", json.dumps({"v2.pgm|s0.pgm": 500}))
    assert par.main(argv) == 0
    assert capsys.readouterr().out.strip() == "parity: exact (2 captures, 4 stage scores)"
    d = json.loads(out.read_text())
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    for key in ("exact", "n_captures", "n_stage_scores", "harness_sha256", "fork_head", "mismatches", "scale",
                "map_csv_sha256", "expected_captures", "expected_stage_scores"):
        assert key in d
    assert d["map_csv_sha256"] == par.c.sha256_file(mp)
    # the default expectation is the 2026-10-15 set: this short one is a mismatch, said plainly
    assert par.main(base) == par.EXIT_MISMATCH
    assert "expected 32 captures with 219 stage scores, found 2 with 4" in capsys.readouterr().out
    assert json.loads(out.read_text())["exact"] is False
    monkeypatch.setenv("FAKE_SCORES", json.dumps({"v2.pgm|s0.pgm": 400, "v1.pgm|s1.pgm": 2}))
    assert par.main(argv) == par.EXIT_MISMATCH
    text = capsys.readouterr().out
    assert "MISMATCH" in text and "v1.pgm: score: stage 1, live 0, offline 2" in text
    assert par.main(argv[:2] + ["--dumps", str(tmp_path / "absent")] + argv[6:]) == par.EXIT_NO_DATA


def test_parity_cli_no_captures(tmp_path, harness, capsys):
    dumps, mp = _parity_scene(tmp_path)
    rows = [r for r in _map_rows() if r["role"].startswith("enroll")]
    with mp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    out = tmp_path / "parity.json"
    assert par.main(["--map", str(mp), "--stages-tsv", str(tmp_path / "none.tsv"), "--dumps", str(dumps),
                     "--harness", str(harness), "--fork", str(tmp_path), "--out", str(out)]) == 2
    assert "no submitted verify capture" in capsys.readouterr().err and not out.exists()


# ---------------------------------------------------------------- trials

def _samples(spec):
    """spec: [(person, label, session, count)] -> sample dicts in order."""
    out = []
    for person, label, session, n in spec:
        for i in range(n):
            out.append({"path": Path(f"{person}/{label}/{i:03d}.pgm"), "person": person, "label": label,
                        "identity": f"{person}/{label}", "session": session, "stem": f"{i:03d}"})
    return out


def test_draw_templates_deterministic_and_held_out():
    s = _samples([("p", "a", "2026-10-16", 6), ("p", "b", "2026-10-16", 5), ("q", "a", "2026-10-16", 3)])
    groups = ana.group_by(s, list(range(len(s))))
    t1 = ana.draw_templates(groups, 3, 4, seed=1)
    assert t1 == ana.draw_templates(groups, 3, 4, seed=1)
    assert t1 != ana.draw_templates(groups, 3, 4, seed=2)
    # q/a has 3 < 3 + 2 images: no template
    assert {t["identity"] for t in t1} == {"p/a", "p/b"} and len(t1) == 8
    for t in t1:
        idx = groups[(t["identity"], t["session"])]
        assert len(t["stages"]) == 3 and t["stages"] == sorted(t["stages"])
        assert sorted(t["stages"] + t["held_out"]) == idx
    # another group does not change these draws
    s2 = s + _samples([("r", "x", "2026-10-16", 9)])
    t2 = ana.draw_templates(ana.group_by(s2, list(range(len(s2)))), 3, 4, seed=1)
    assert [t for t in t2 if t["identity"] != "r/x"] == t1


def test_template_scores_max_over_stages():
    S = np.array([[0, 1, 9, 3], [5, 0, 2, 7], [0, 0, 0, 0]], dtype=float)
    sc, st = ana.template_scores(S, [1, 2, 3], [0, 1, 2])
    assert list(sc) == [9, 7, 0] and list(st) == [1, 2, 0]


def test_rates_counting():
    r = ana.rates([104, 105, 0, 300], [0, 105, 104.9, 7], 105)
    assert (r["far_x"], r["far_n"], r["frr_x"], r["frr_n"]) == (1, 4, 2, 4)


def test_run_trials_genuine_and_impostor_sets():
    s = _samples([("p", "a", "d", 4), ("p", "b", "d", 2)])
    S = np.zeros((6, 6))
    S[4, 1] = 50  # p/b image 0 against p/a image 1
    t = [{"identity": "p/a", "session": "d", "k": 0, "stages": [0, 1], "held_out": [2, 3]}]
    tr = ana.run_trials(S, s, list(range(6)), t)
    assert [p for _, p, _, _ in tr["genuine"]] == [2, 3]
    assert [p for _, p, _, _ in tr["impostor"]] == [4, 5]
    assert tr["impostor"][0][2:] == (50.0, 1)


# ---------------------------------------------------------------- statistics

def test_cp_known_values():
    assert ana.cp_upper(0, 1) == pytest.approx(0.95)
    assert ana.cp_upper(0, 3) == pytest.approx(1 - 0.05 ** (1 / 3))
    assert ana.cp_upper(0, 300) == pytest.approx(0.009936, abs=1e-6)
    assert ana.cp_upper(5, 5) == 1.0
    assert ana.cp_upper(0, 0) is None


def test_cp_against_scipy():
    beta = pytest.importorskip("scipy.stats").beta
    for x, n in [(1, 10), (3, 31), (7, 300), (50, 8000), (1, 2), (99, 100)]:
        assert ana.cp_upper(x, n) == pytest.approx(beta.ppf(0.95, x + 1, n - x), rel=1e-9, abs=1e-12)


def test_binom_logcdf_edges():
    assert ana.binom_logcdf(3, 3, 0.5) == pytest.approx(0.0)
    assert math.exp(ana.binom_logcdf(0, 4, 0.5)) == pytest.approx(1 / 16)


def test_correlation_levels_hand_built():
    # identities A (5 imgs), B (5 imgs), C (2 imgs); 2-stage templates, k = 2 -> templates for A and B only
    s = _samples([("p", "A", "d", 5), ("p", "B", "d", 5), ("q", "C", "d", 2)])
    n = len(s)
    S = np.zeros((n, n))
    S[10, 0:5] = 200  # C image 0 matches every A image: accepted by every A template
    S[5, 10] = 200  # B image 0 vs C (C has no template: never a trial)
    groups = ana.group_by(s, list(range(n)))
    tps = ana.draw_templates(groups, 2, 2, seed=0)
    assert len(tps) == 4
    tr = ana.run_trials(S, s, list(range(n)), tps)
    lv = ana.correlation_levels(tr, tps, s, 105)
    # (a) every trial: 2 A templates x 7 probes + 2 B templates x 7 probes; the 2 A templates accept C0
    assert lv["a_all_trials"]["n"] == 28 and lv["a_all_trials"]["x"] == 2
    # (b) k = 0 only: 7 + 7 trials, one accept
    assert lv["b_probe_level"]["n"] == 14 and lv["b_probe_level"]["x"] == 1
    # (c) pairs {A,B}, {A,C}, {B,C}; only {A,C} accepts
    assert lv["c_finger_pairs"] == {"x": 1, "n": 3, "upper95": ana.cp_upper(1, 3)}


# ---------------------------------------------------------------- threshold rule

@pytest.mark.parametrize("s,t", [(6, 105), (4, 105), (0, 15), (7, 105), (105, 378), (1, 45)])
def test_recommend(s, t):
    assert ana.recommend(s)["threshold"] == t


def test_matches_score_inverse():
    for m in (4, 5, 6, 7):
        assert ana.score_matches(ana.matches_score(m)) == pytest.approx(m)
    assert [ana.matches_score(m) for m in (4, 5, 6, 7)] == [15, 45, 105, 210]


def test_allowed_below_105():
    assert ana.allowed_below_105(45, ana.THRESHOLDS) == [75, 50, 45]
    assert ana.allowed_below_105(50, ana.THRESHOLDS) == [75, 50]
    assert ana.allowed_below_105(105, ana.THRESHOLDS) == []
    assert ana.allowed_below_105(378, ana.THRESHOLDS) == []


def _verdict_scene(spec, impostor_max):
    """analyze() over identities from spec; one impostor stage score impostor_max, genuine scores 1e6 / 60."""
    s = _samples(spec)
    n = len(s)
    S = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            if s[i]["identity"] == s[j]["identity"]:
                S[i, j] = 1e6 if s[i]["session"] == s[j]["session"] else 60
    S[n - 1, 0] = impostor_max
    return ana.analyze(S, s, np.zeros(n, dtype=bool), stages=2, k=2, seed=0)


def test_recommendation_scope_one_person():
    rep = _verdict_scene([("p", "a", "2026-10-16", 4), ("p", "a", "2026-10-17", 4), ("p", "b", "2026-10-16", 4)], 1)
    rec = rep["recommendation"]
    assert rec["threshold"] == 45 and rec["d6_rule_allows_lowering"] is True
    assert "lowering_below_105_supported" not in rec
    sc = rec["scope"]
    assert (sc["identities"], sc["persons"], sc["finger_pairs"], sc["one_person"]) == (2, 1, 1, True)
    assert sc["c_upper95"] == rec["at_threshold"]["levels"]["c_finger_pairs"]["upper95"] == ana.cp_upper(0, 1)
    assert sc["c_upper95_at_105"] == rec["c_at_105"]["upper95"]
    assert [g["threshold"] for g in sc["frr_gain_vs_105"]] == [75, 50, 45]
    g45 = sc["frr_gain_vs_105"][-1]
    # same-session genuine all score 1e6: no gain; cross-day probes score 60: all rejected at 105, accepted at 45
    assert g45["same_session"]["gain_pp"] == 0 and g45["same_session"]["frr_x_105"] == 0
    assert g45["cross_day"] == {"frr_x_105": 8, "frr_x": 0, "n": 8, "gain_pp": 100.0}
    assert sc["frr_gain_vs_105"][0]["cross_day"]["gain_pp"] == 0  # 60 < 75
    text = "\n".join(ana.verdict_lines(rec))
    assert "D6 margin rule: allows lowering below 105, down to 45; a rule, not a FAR target" in text
    assert "evidence: 2 identities, 1 persons, 1 unordered finger pairs; FAR bound (c) 95% at 45" in text
    assert "@45: +0.00 pp (0 -> 0 of" in text and "+100.00 pp (8 -> 0 of 8)" in text
    assert ana.ONE_PERSON in text


def test_recommendation_rule_does_not_allow_lowering():
    rep = _verdict_scene([("p", "a", "2026-10-16", 4), ("q", "b", "2026-10-16", 4)], 105)
    rec = rep["recommendation"]
    assert rec["threshold"] == 378 and rec["d6_rule_allows_lowering"] is False
    assert rec["scope"]["frr_gain_vs_105"] == [] and rec["scope"]["persons"] == 2
    text = "\n".join(ana.verdict_lines(rec))
    assert "does not allow lowering below 105 (it gives 378)" in text
    assert "FRR gain" not in text and ana.ONE_PERSON not in text


# ---------------------------------------------------------------- cross-day

def test_cross_day_grouping():
    s = _samples([("p", "a", "2026-10-16", 4), ("p", "a", "2026-10-17", 3), ("p", "b", "2026-10-17", 2),
                  ("p", "b", "2026-10-18", 2)])
    n = len(s)
    S = np.zeros((n, n))
    S[4, 0] = 300  # later image 0 of p/a matches earlier image 0
    rows = ana.cross_day(S, s, list(range(n)), stages=4, k=2, seed=0, thresholds=[105, 5])
    a, b = rows
    assert (a["identity"], a["template_session"], a["probe_session"]) == ("p/a", "2026-10-16", "2026-10-17")
    # one template (all 4 images) drawn twice: 2 x 3 trials, 2 accepted
    assert a["trials"] == 6 and a["frr"] == [{"threshold": 105, "x": 4, "n": 6}, {"threshold": 5, "x": 4, "n": 6}]
    assert b["identity"] == "p/b" and "skipped" in b


# ---------------------------------------------------------------- loading and the report

def _sample_tree(data: Path, spec):
    for person, label, session, n in spec:
        d = data / person / label
        d.mkdir(parents=True, exist_ok=True)
        start = len(list(d.glob("*.pgm")))
        for i in range(start, start + n):
            (d / f"{i:03d}.pgm").write_bytes(PGM + bytes([i]))
            (d / f"{i:03d}.json").write_text(json.dumps({"session": session, "person": person, "label": label}))


def test_load_samples_and_orphans(tmp_path):
    _sample_tree(tmp_path, [("p", "a", "2026-10-16", 2), ("q", "b", "2026-10-17", 1)])
    (tmp_path / "p/a/007.pgm").write_bytes(PGM)
    (tmp_path / "p/a/008.json").write_text("{}")
    (tmp_path / "q/b/009.pgm").write_bytes(PGM)
    (tmp_path / "q/b/009.json").write_text(json.dumps({"session": "2026-10-17", "person": "zz", "label": "b"}))
    (tmp_path / "results/x").mkdir(parents=True)
    (tmp_path / "results/x/001.pgm").write_bytes(PGM)
    (tmp_path / "results/x/001.json").write_text(json.dumps({"session": "2026-10-17"}))
    s, o = ana.load_samples(tmp_path)
    assert [(x["identity"], x["stem"], x["session"]) for x in s] == [
        ("p/a", "000", "2026-10-16"), ("p/a", "001", "2026-10-16"), ("q/b", "000", "2026-10-17")]
    reasons = {x["sample"]: x["reason"] for x in o}
    assert reasons == {"p/a/007": "no .json sidecar", "p/a/008": "no .pgm",
                       "q/b/009": "sidecar person/label differ from the directory"}


def test_cache_key_order_independent():
    a = ana.cache_key([("x", "1"), ("y", "2")], "h", 2)
    assert a == ana.cache_key([("y", "2"), ("x", "1")], "h", 2)
    assert a != ana.cache_key([("x", "1"), ("y", "3")], "h", 2)
    assert a != ana.cache_key([("x", "1"), ("y", "2")], "h2", 2)


def test_analyze_main_json_and_cache(tmp_path, harness, monkeypatch, capsys):
    data = tmp_path / "b3"
    _sample_tree(data, [("p", "a", "2026-10-16", 7), ("p", "b", "2026-10-16", 7), ("q", "a", "2026-10-16", 3),
                        ("p", "a", "2026-10-17", 3)])
    monkeypatch.setenv("FAKE_SCORES", json.dumps({"000.pgm|001.pgm": 3}))
    monkeypatch.setenv("FAKE_REJECT", json.dumps([]))
    argv = ["--data", str(data), "--harness", str(harness), "--fork", str(tmp_path), "--parity",
            str(tmp_path / "none.json"), "--stages", "5", "--k", "2"]
    assert ana.main(argv) == 0
    text = capsys.readouterr().out
    assert "SMOKE RUN: --stages 5" in text and "parity: NOT RUN" in text and "recommended threshold:" in text
    outs = sorted((data / "results").glob("b3-*.json"))
    assert len(outs) == 1 and stat.S_IMODE(outs[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((data / "results").stat().st_mode) == 0o700
    rep = json.loads(outs[0].read_text())
    for key in ("schema", "created", "scorer", "parity", "data", "failed_captures", "trials", "impostor",
                "top_impostors", "thresholds", "recommendation", "cross_day", "matrix", "orphans"):
        assert key in rep, key
    assert rep["schema"] == ana.SCHEMA and rep["matrix"]["cached"] is False
    assert [r["threshold"] for r in rep["thresholds"]] == ana.THRESHOLDS
    assert set(rep["thresholds"][0]["levels"]) == {"a_all_trials", "b_probe_level", "c_finger_pairs"}
    assert rep["trials"]["templates"] == 4 and rep["data"]["identities"] == 3
    assert rep["cross_day"][0]["identity"] == "p/a"
    for o in outs:
        o.unlink()
    assert ana.main(argv) == 0
    capsys.readouterr()
    rep = json.loads(next((data / "results").glob("b3-*.json")).read_text())
    assert rep["matrix"]["cached"] is True


def test_analyze_gate_rejected_is_failed_capture(tmp_path, harness, monkeypatch, capsys):
    data = tmp_path / "b3"
    _sample_tree(data, [("p", "a", "2026-10-16", 4), ("p", "b", "2026-10-16", 4)])
    monkeypatch.setenv("FAKE_REJECT", json.dumps(["003.pgm"]))
    monkeypatch.setenv("FAKE_SCORES", "{}")
    assert ana.main(["--data", str(data), "--harness", str(harness), "--fork", str(tmp_path),
                     "--parity", str(tmp_path / "n.json"), "--stages", "2", "--k", "1"]) == 0
    capsys.readouterr()
    rep = json.loads(next((data / "results").glob("b3-*.json")).read_text())
    assert rep["failed_captures"]["count"] == 2 and rep["data"]["usable"] == 6
    # 3 usable per identity: 2 stages + 1 held out is below stages + 2, so no templates
    assert rep["trials"]["templates"] == 0


def test_analyze_no_data(tmp_path, capsys):
    assert ana.main(["--data", str(tmp_path / "empty"), "--harness", str(tmp_path / "h")]) == 0
    assert "tools/dev/b3-capture.sh" in capsys.readouterr().out


def test_parity_status(tmp_path):
    p = tmp_path / "parity.json"
    p.write_text(json.dumps({"exact": True, "n_captures": 32, "n_stage_scores": 219, "harness_sha256": "aa"}))
    assert ana.parity_status(p, "aa")["line"] == "parity: exact (32 captures, 219 stage scores), same harness"
    assert "OTHER HARNESS" in ana.parity_status(p, "bb")["line"]
    assert ana.parity_status(tmp_path / "none", "aa")["status"] == "missing"


# ---------------------------------------------------------------- wrapper

WRAPPER = ROOT / "tools/dev/b3-analyze.sh"


def test_wrapper_syntax():
    assert subprocess.run(["bash", "-n", str(WRAPPER)], check=False).returncode == 0


def test_wrapper_unbuilt_fork_fails_with_hint(tmp_path):
    env = dict(os.environ, LIBFPRINT_FORK=str(tmp_path / "fork"))
    p = subprocess.run([str(WRAPPER)], env=env, capture_output=True, text=True, check=False)
    assert p.returncode == 2 and p.stdout == ""
    assert "FAILED: no " in p.stderr and "hint: meson compile -C" in p.stderr


def test_wrapper_unknown_argument():
    p = subprocess.run([str(WRAPPER), "--bogus"], capture_output=True, text=True, check=False)
    assert p.returncode == 2 and "unknown argument: --bogus" in p.stderr
