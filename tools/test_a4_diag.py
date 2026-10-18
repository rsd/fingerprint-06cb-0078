"""Offline tests of tools/dev/a4_diag.py and the a4-capture.sh diagnosis options (no device, no USB).

The log lines are synthetic, in the formats the synaptics0078 driver prints; nothing biometric.
"""

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "dev"))

import a4_diag

WRAPPER = HERE / "dev" / "a4-capture.sh"

GLIB = "(process:1): libfprint-synaptics0078-DEBUG: {t}: {msg}"
JOURNAL = "Oct 15 {t} host fprintd[2]: {msg}"


def log(fmt, rows):
    return [fmt.format(t=t, msg=msg) for t, msg in rows]


# One cycle as the instrumented driver prints it: the delivered image, a still-down probe whose
# image is discarded, a probe ending in a capture error, then an empty window (finger off).
DIAG_CYCLE = [
    ("10:00:00.000", "Command 0x02: reply 2154 bytes, status 0x0000"),
    ("10:00:01.000", "EP 0x83 event 0200401000: finger (+1000 ms after 02)"),
    ("10:00:01.050", "EP 0x83 event 0342040040: ready (+1050 ms after 02)"),
    (
        "10:00:01.060",
        (
            "Diag image 1 capture: mean 128.5, sd 44.10, coverage 95%, gradient 18.7, "
            "diff vs last capture -, 1060 ms after 02, file 000001-00000001000ms-capture.pgm"
        ),
    ),
    ("10:00:01.061", "Image captured"),
    ("10:00:01.062", "Finger-off probe 1: arming"),
    ("10:00:01.082", "Command 0x02: reply 2154 bytes, status 0x0000"),
    ("10:00:01.084", "EP 0x83 event 0000000000: ignore (+2 ms after 02)"),
    ("10:00:01.179", "EP 0x83 event 0200401000: finger (+97 ms after 02)"),
    ("10:00:01.179", "Finger-off probe 1: finger still down"),
    ("10:00:01.211", "EP 0x83 event 0340010000: ignore (+129 ms after 02)"),
    ("10:00:01.231", "EP 0x83 event 0342040040: ready (+149 ms after 02)"),
    (
        "10:00:01.240",
        "Image reply: 8082 bytes, status 0x0000, length 8076, x 56, y 144, w1 333, w2 8, error 0x00000000",
    ),
    (
        "10:00:01.240",
        (
            "Diag image 2 probe01: mean 128.5, sd 40.00, coverage 90%, gradient 17.0, "
            "diff vs last capture 3.5, 158 ms after 02, file 000002-00000001240ms-probe01.pgm"
        ),
    ),
    ("10:00:01.240", "Finger-off probe 1: discarding the 51 reply (image)"),
    ("10:00:01.491", "Finger-off probe 2: arming"),
    ("10:00:01.511", "Command 0x02: reply 2154 bytes, status 0x0000"),
    ("10:00:01.849", "EP 0x83 event 0200401000: finger (+338 ms after 02)"),
    ("10:00:01.871", "EP 0x83 event 0360070040: ready (+360 ms after 02)"),
    (
        "10:00:01.872",
        "Image reply: 18 bytes, status 0x0007, length 12, x 0, y 0, w1 333, w2 8, error 0x80000002",
    ),
    ("10:00:01.872", "Finger-off probe 2: discarding the 51 reply (capture error)"),
    ("10:00:02.123", "Finger-off probe 3: arming"),
    ("10:00:02.143", "Command 0x02: reply 2154 bytes, status 0x0000"),
    ("10:00:02.145", "EP 0x83 event 0000000000: ignore (+2 ms after 02)"),
    ("10:00:02.643", "Finger off after 3 probe(s), 1581 ms"),
]


def test_parse_instrumented_cycle():
    (cycle,) = a4_diag.parse_log(log(GLIB, DIAG_CYCLE))
    assert (cycle.off_probes, cycle.off_ms) == (3, 1581)
    assert cycle.capture == a4_diag.Stats(44.10, 95, 18.7, None)
    p1, p2, p3 = cycle.probes
    assert (p1.n, p1.start_ms, p1.finger_ms, p1.ready, p1.ready_ms) == (1, 1, 97, "0342040040", 149)
    assert p1.reply == "image 8082B/0x0000"
    assert p1.stats == a4_diag.Stats(40.0, 90, 17.0, 3.5)
    assert (p2.finger_ms, p2.ready, p2.reply, p2.stats) == (338, "0360070040", "capture error 18B/0x0007", None)
    assert (p3.finger_ms, p3.ready, p3.reply) == (None, None, None)


def test_journal_without_latency_suffix_uses_the_02_reply_time():
    rows = [(t, msg.split(" (+")[0] if msg.startswith("EP 0x83") else msg) for t, msg in DIAG_CYCLE]
    (cycle,) = a4_diag.parse_log(log(JOURNAL, rows))
    assert [p.finger_ms for p in cycle.probes] == [97, 338, None]
    assert cycle.probes[0].ready_ms == 149


def test_two_cycles_and_table():
    lines = log(GLIB, DIAG_CYCLE) * 2
    cycles = a4_diag.parse_log(lines)
    assert len(cycles) == 2
    table = a4_diag.format_table(cycles)
    assert "cycle 1: finger off after 3 probe(s), 1581 ms; image sd/cov/grad 44.1  95%  18.7" in table
    assert "0342040040@149" in table and "0360070040@360" in table
    assert "summary: 2 cycle(s), finger off 1581..1581 ms, probes 3..3" in table
    row = next(line for line in table.splitlines() if line.strip().startswith("1 ") and "image" in line)
    assert row.split()[:4] == ["1", "1", "97", "0342040040@149"]
    assert row.rstrip().endswith("40.0  90%  17.0   3.5")


def test_interrupted_cycle_and_empty_log():
    cycles = a4_diag.parse_log(log(GLIB, DIAG_CYCLE[:-1]))
    assert cycles[0].off_ms is None
    assert "no finger-off report" in a4_diag.format_table(cycles)
    assert "no finger-off probing found" in a4_diag.format_table(a4_diag.parse_log(["nothing here"]))


def test_midnight_wrap():
    assert a4_diag.ms_since(a4_diag.line_seconds("23:59:59.900"), a4_diag.line_seconds("00:00:00.100")) == 200


def test_main_usage_and_file(tmp_path, capsys):
    assert a4_diag.main([]) == 2
    path = tmp_path / "run.log"
    path.write_text("\n".join(log(GLIB, DIAG_CYCLE)) + "\n")
    assert a4_diag.main([str(path)]) == 0
    assert "finger off after 3 probe(s)" in capsys.readouterr().out


# ------------------------------------------------------------------ wrapper options (fail before any device check)


def run_wrapper(*args):
    return subprocess.run(["bash", str(WRAPPER), *args], capture_output=True, text=True, timeout=30, check=False)


def test_wrapper_syntax():
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True)


def test_wrapper_help_lists_diag_options():
    out = run_wrapper("--help").stdout
    assert "--diag" in out and "--window-ms" in out and "--backoff-ms" in out
    assert "This script never calls sudo." in out


@pytest.mark.parametrize(
    "args, message",
    [
        (["--window-ms", "49"], "--window-ms must be a whole number from 50 to 10000"),
        (["--window-ms=10001"], "--window-ms must be a whole number from 50 to 10000"),
        (["--backoff-ms", "60001"], "--backoff-ms must be a whole number from 0 to 60000"),
        (["--backoff-ms", "-1"], "--backoff-ms must be a whole number from 0 to 60000"),
        (["--backoff-ms=2s"], "--backoff-ms must be a whole number from 0 to 60000"),
        (["--window-ms"], "--window-ms needs a number"),
    ],
)
def test_wrapper_rejects_bad_cadence(args, message):
    r = run_wrapper(*args)
    assert r.returncode == 2
    assert message in r.stderr


def test_diag_dir_is_under_gitignored_local():
    text = WRAPPER.read_text()
    assert "DIAG_ROOT=$ROOT/local/diag" in text
    gitignore = (HERE.parent / ".gitignore").read_text().splitlines()
    assert "local/" in gitignore
