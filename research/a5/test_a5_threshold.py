"""Tests of the analysis logic in research/a5/a5_threshold.py.

Synthetic score matrices and images only: no capture is read, no harness is run.
Needs numpy (system python3; proto/.venv has none, so the module is skipped there).
"""

import hashlib
import math

import pytest

np = pytest.importorskip("numpy")
import a5_threshold as a5

NAN = float("nan")


def two_fingers():
    """3 images of finger A, 3 of finger B. S[p, g] = score of probe p against template g."""
    fingers = ["A", "A", "A", "B", "B", "B"]
    S = np.zeros((6, 6))
    np.fill_diagonal(S, 1e6)
    S[0, 1] = 500  # A0 matches template A1 only
    S[1, 0] = 300
    S[2, 0] = 20  # A2 matches A0 weakly
    S[3, 4] = 1000  # B3 matches B4
    S[4, 3] = 1000
    # B5 matches nothing of its own finger
    S[5, 0] = 6  # impostor: B5 against template A0
    return S, fingers


# ---------------------------------------------------------------- score scale

def test_matches_score_known_values():
    assert [a5.matches_score(m) for m in (4, 5, 6, 7)] == [15, 45, 105, 210]


def test_score_matches_inverts_matches_score():
    for m in (2.5, 3.37, 4, 5.5, 9, 20):
        assert a5.score_matches(a5.matches_score(m)) == pytest.approx(m)
    assert a5.score_matches(0) == 0


def test_margin_threshold_rule():
    # impostor max 6 = 3.37 match-equivalents; +2 matches -> 5.37 -> 63.2 -> 64
    assert a5.margin_threshold(6, 2, whole=False) == 64
    assert a5.margin_threshold(6, 1, whole=False) == 24
    # rounded up to whole matches: 5.37 -> 6 -> 105 (the A5 rule); 4.37 -> 5 -> 45
    assert a5.margin_threshold(6, 2) == 105
    assert a5.margin_threshold(6, 1) == 45
    # an impostor score that is already a whole match count stays exact
    assert a5.margin_threshold(45, 2) == 210
    # never at or below the impostor score, even when the margin rounds to 0
    assert a5.margin_threshold(0, 2, whole=False) == 1
    assert a5.margin_threshold(0, 2) == 1
    assert a5.margin_threshold(1000, 0, whole=False) == 1001


# ---------------------------------------------------------------- analysis

def test_frr_by_k_exact_at_k1():
    S, fingers = two_fingers()
    res = a5.frr_by_k(S, fingers, [1, 2], [1, 7, 400])
    # k = 1: 6 genuine attempts per finger (3 enrolled singles x 2 probes), 12 total
    e1 = res[1]
    assert e1["genuine_n"] == 12
    # genuine scores >= 1: A0<-A1 500, A1<-A0 300, A2<-A0 20, B3<-B4, B4<-B3: 5 of 12
    assert e1["frr"][1] == pytest.approx(7 / 12)
    # >= 400: 500, 1000, 1000
    assert e1["frr"][400] == pytest.approx(9 / 12)
    # the impostor score 6 is a false accept at 1, not at 7
    assert e1["false_accepts"][1] == 1
    assert e1["false_accepts"][7] == 0


def test_extrapolate_frr_floor_and_decay():
    S, fingers = two_fingers()
    x = a5.extrapolate_frr(S, fingers, 1, [1, 2, 10])
    # f_p over the 2 other images of the finger: A0 .5, A1 .5, A2 .5, B3 .5, B4 .5, B5 0
    assert x["probes"] == 6
    assert x["floor"] == pytest.approx(1 / 6)
    assert x["frr"][1] == pytest.approx((5 * 0.5 + 1) / 6)
    assert x["frr"][2] == pytest.approx((5 * 0.25 + 1) / 6)
    assert x["frr"][10] == pytest.approx((5 * 0.5**10 + 1) / 6)
    # decreasing in k, bounded below by the floor
    assert x["frr"][1] > x["frr"][2] > x["frr"][10] > x["floor"]


def test_extrapolate_frr_counts_nan_as_reject():
    S, fingers = two_fingers()
    S[2, 0] = NAN
    x = a5.extrapolate_frr(S, fingers, 1, [1])
    assert x["floor"] == pytest.approx(2 / 6)


def test_full_gallery_impostor_is_or_over_the_gallery():
    S, fingers = two_fingers()
    fg = a5.full_gallery_impostor(S, fingers)
    assert fg["per_gallery"] == {"A": 6.0, "B": 0.0}
    assert fg["max"] == 6.0


def test_probe_vs_galleries_flags_two_fingers():
    S, fingers = two_fingers()
    # two extra unlabeled probes (rows 6, 7); columns 6, 7 are not gallery images
    big = np.zeros((8, 8))
    big[:6, :6] = S
    big[6, 1] = 100  # clears A only
    big[7, 0] = 70
    big[7, 5] = 64  # clears A and B at 64
    rows = a5.probe_vs_galleries(big, [6, 7], fingers, 64)
    assert rows[0]["best"] == {"A": 100.0, "B": 0.0}
    assert rows[0]["cleared"] == ["A"] and not rows[0]["flag"]
    assert rows[1]["cleared"] == ["A", "B"] and rows[1]["flag"]


def test_probe_vs_galleries_rejected_probe():
    S, fingers = two_fingers()
    big = np.full((7, 7), NAN)
    big[:6, :6] = S
    rows = a5.probe_vs_galleries(big, [6], fingers, 1)
    assert rows[0]["rejected"] and rows[0]["cleared"] == []


def test_timing_and_latency_model():
    S, _ = two_fingers()
    ms = np.full((6, 6), 1.0)
    np.fill_diagonal(ms, 200.0)
    ms[0, 1] = 10.0  # the pair scoring 500
    ms[3, 4] = 30.0
    t = a5.timing_summary(ms, S, 6)
    assert t["self"]["n"] == 6 and t["self"]["median"] == 200.0
    assert t["score_pos"]["n"] == 6  # 500, 300, 20, 1000, 1000, 6 (impostor, still > 0)
    assert t["score_pos"]["max"] == 30.0
    assert t["score0"]["n"] == 30 - 6 and t["score0"]["max"] == 1.0
    lat = a5.verify_latency({"median": 5.0, "p95": 8.0, "max": 9.0}, t, 18)
    assert lat["reject_median_ms"] == 5.0 + 18 * 1.0
    assert lat["reject_p95_ms"] == 8.0 + 18 * 1.0
    assert lat["accept_worst_ms"] == 9.0 + 17 * 1.0 + 30.0


def test_print_size():
    ps = a5.print_size([100, 200, 300], 18)
    assert ps["per_stage_median"] == 200 and ps["per_stage_max"] == 300
    assert ps["print_median"] == 3600 and ps["print_max"] == 5400


# ---------------------------------------------------------------- driver mirror and export

def test_upscale_matches_driver_loop():
    img = np.arange(12, dtype=np.uint8).reshape(3, 4)
    for scale in (1, 2, 3):
        up = a5.upscale(img, scale)
        assert up.shape == (3 * scale, 4 * scale)
        for y in range(up.shape[0]):
            for x in range(up.shape[1]):
                assert up[y, x] == img[y // scale, x // scale]


def test_smoke_trials_selection():
    S, fingers = two_fingers()
    trials = a5.smoke_trials(S, fingers, stages=1)
    a_trials = [t for t in trials if t["finger"] == "A"]
    assert all(t["gallery"] == [0] for t in a_trials)
    gen = [t["probe"] for t in a_trials if t["kind"] == "genuine"]
    imp = [t["probe"] for t in a_trials if t["kind"] == "impostor"]
    # genuine: highest (A1, 300) then lowest (A2, 20); impostor: B5 (6) first
    assert gen == [1, 2]
    assert imp[0] == 5 and len(imp) == 2
    assert next(t for t in a_trials if t["probe"] == 1)["scores"] == [300]


class _Sample:
    def __init__(self, finger, image):
        self.finger = finger
        self.image = image


def test_export_smoke_manifest(tmp_path):
    S, fingers = two_fingers()
    rng = np.random.default_rng(0)
    samples = [_Sample(f, rng.integers(0, 256, (2, 3), dtype=np.uint8)) for f in fingers]
    n = a5.export_smoke(tmp_path / "fx", samples, S, scale=2, stages=1)
    rows = [r.split("\t") for r in (tmp_path / "fx" / "manifest.tsv").read_text().splitlines()]
    assert rows[0] == a5.SMOKE_COLUMNS
    assert len(rows) == n + 1
    row = dict(zip(rows[0], rows[1]))
    assert row["window"] == "3x2" and row["scale"] == "2" and row["stages"] == "1"
    data = (tmp_path / "fx" / f"trial{row['trial_id']}_probe.pgm").read_bytes()
    assert data.startswith(b"P5\n6 4\n255\n")
    payload = data[len(b"P5\n6 4\n255\n"):]
    assert len(payload) == 24
    assert hashlib.sha256(payload).hexdigest() == row["probe_sha256"]
    assert int(row["expected_best"]) == max(int(s) for s in row["expected_scores"].split(","))


def test_matrix_maps_null_to_nan():
    m = a5.matrix({"scores": [[1, None], [None, 4]]}, "scores")
    assert m[0, 0] == 1 and math.isnan(m[0, 1]) and m[1, 1] == 4


def test_chosen_constants_match_driver_defines():
    """CHOSEN_* must equal the #defines of the fork's driver (skipped when the fork is absent)."""
    import os
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    fork = Path(os.environ.get("LIBFPRINT_FORK", repo.parent / "libfprint"))
    src = fork / "libfprint" / "drivers" / "synaptics0078.c"
    if not src.is_file():
        pytest.skip(f"{src} not found")
    text = src.read_text()

    def define(name):
        m = re.search(rf"^#define\s+{name}\s+(\d+)\b", text, re.MULTILINE)
        assert m, f"{name} not found in {src}"
        return int(m.group(1))

    assert define("SYNAPTICS0078_SCORE_THRESHOLD") == a5.CHOSEN_THRESHOLD
    assert define("SYNAPTICS0078_ENROLL_STAGES") == a5.CHOSEN_STAGES
    assert define("SYNAPTICS0078_UPSCALE") == a5.CHOSEN_SCALE
