"""Offline tests of the pure helpers in tools/dev/a4_capture.py (no PyGObject, no device, no USB).

All images are synthetic; nothing biometric is read or written outside pytest's tmp_path.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "dev"))

import a4_capture as a4
import capture  # proto/capture.py, on sys.path via a4_capture
import parsers

NEEDED, PRESENT = a4.FINGER_NEEDED, a4.FINGER_PRESENT


def upscale(pixels: bytes, width: int, k: int) -> bytes:
    rows = []
    for y in range(len(pixels) // width):
        row = b"".join(bytes([p]) * k for p in pixels[y * width : (y + 1) * width])
        rows.extend([row] * k)
    return b"".join(rows)


NATIVE = bytes((x * 7 + y * 13) % 256 for y in range(56) for x in range(144))


# ------------------------------------------------------------------ decimation


@pytest.mark.parametrize("k", [1, 2, 3])
def test_to_native_decimates_uniform_upscale(k):
    delivered = upscale(NATIVE, 144, k) if k > 1 else NATIVE
    n = a4.to_native(delivered, 144 * k, 56 * k)
    assert (n.width, n.height, n.factor, n.warning) == (144, 56, k, None)
    assert n.pixels == NATIVE


def test_to_native_non_uniform_block_kept_as_delivered_with_warning():
    delivered = bytearray(upscale(NATIVE, 144, 2))
    delivered[1] ^= 0xFF  # first pixel of the first block differs from the other three
    n = a4.to_native(bytes(delivered), 288, 112)
    assert (n.width, n.height, n.factor) == (288, 112, None)
    assert n.pixels == bytes(delivered)
    assert "not a uniform 2x2 upscale" in n.warning


@pytest.mark.parametrize("w, h", [(100, 40), (288, 56), (144, 112), (432, 112), (145, 56)])
def test_to_native_odd_sizes_kept_with_warning(w, h):
    px = bytes(range(256)) * (w * h // 256 + 1)
    n = a4.to_native(px[: w * h], w, h)
    assert (n.width, n.height, n.factor) == (w, h, None)
    assert n.warning and "not an integer multiple" in n.warning


def test_to_native_rejects_wrong_pixel_count():
    with pytest.raises(ValueError):
        a4.to_native(bytes(10), 144, 56)
    with pytest.raises(ValueError):
        a4.to_native(b"", 0, 0)


# ------------------------------------------------------------------ stats, coverage, PGM


def test_image_stats_matches_proto_capture_format():
    px = bytes((i * 31) % 251 for i in range(144 * 56))
    assert a4.image_stats(px) == capture.image_stats(px)
    assert a4.image_stats(bytes([0, 10, 20, 30])) == "min 0, max 30, mean 15.0, sd 11.18"


def test_coverage_text():
    flat = bytes([100]) * (144 * 56)
    assert a4.coverage_text(flat, 144, 56) == "ridge coverage ~0%"
    assert a4.coverage_text(NATIVE, 144, 56).startswith("ridge coverage ~")
    assert a4.coverage_text(bytes(3), 144, 56) == "ridge coverage n/a"


def test_save_pgm_bytes_mode_and_no_overwrite(tmp_path):
    d = tmp_path / "captures"
    a4.ensure_private_dir(d)
    path = a4.save_pgm(d, "20260101-000000", 2, NATIVE, 144, 56)
    assert path.name == "a4-20260101-000000-02.pgm"
    assert path.read_bytes() == b"P5\n144 56\n255\n" + NATIVE == parsers.pgm_bytes(NATIVE, 144, 56)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        a4.save_pgm(d, "20260101-000000", 2, NATIVE, 144, 56)
    with pytest.raises(ValueError):
        a4.save_pgm(d, "20260101-000000", 3, NATIVE[:-1], 144, 56)
    assert not a4.capture_path(d, "20260101-000000", 3).exists()


def test_save_pgm_does_not_follow_a_symlink_at_the_target(tmp_path):
    d = tmp_path / "c"
    a4.ensure_private_dir(d)
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    os.symlink(victim, a4.capture_path(d, "20260101-000000", 1))
    with pytest.raises(OSError):
        a4.save_pgm(d, "20260101-000000", 1, NATIVE, 144, 56)
    assert victim.read_bytes() == b"keep"


# ------------------------------------------------------------------ directory safety


def test_ensure_private_dir_creates_0700_and_tightens(tmp_path):
    d = tmp_path / "new"
    a4.ensure_private_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    d.chmod(0o755)
    a4.ensure_private_dir(d, chmod=False)
    assert stat.S_IMODE(d.stat().st_mode) == 0o755
    a4.ensure_private_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_ensure_private_dir_refuses_symlink_and_file(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(OSError, match="symlink"):
        a4.ensure_private_dir(link)
    dangling = tmp_path / "dangling"
    os.symlink(tmp_path / "nowhere", dangling)
    with pytest.raises(OSError, match="symlink"):
        a4.ensure_private_dir(dangling)
    assert not (tmp_path / "nowhere").exists()
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(OSError, match="not a directory"):
        a4.ensure_private_dir(f)


def test_capture_dir_is_under_gitignored_local():
    assert a4.CAPTURE_DIR == a4.ROOT / "local" / "captures"
    assert "local/" in (a4.ROOT / ".gitignore").read_text().splitlines()


# ------------------------------------------------------------------ error classification


def test_classify_retry_error():
    assert a4.classify_error(True) == "retry"


def test_classify_other_errors_fail():
    assert a4.classify_error(False) == "fail"


def test_classify_cancelled_is_timeout_whatever_the_error():
    assert a4.classify_error(False, cancelled=True) == "timeout"
    assert a4.classify_error(True, cancelled=True) == "timeout"


# ------------------------------------------------------------------ finger tracker and timing


def feed(steps):
    t = a4.FingerTracker()
    return t, [t.update(flags, now) for flags, now in steps]


def test_tracker_normal_capture_sequence():
    t, events = feed(
        [
            (NEEDED, 10.0),
            (NEEDED | PRESENT, 10.5),
            (PRESENT, 11.0),  # image taken: NEEDED cleared, finger still on
            (0, 11.4),  # finger off
        ]
    )
    assert events == [["needed"], ["present"], ["taken"], ["off"]]
    assert (t.present_at, t.taken_at, t.off_at) == (10.5, 11.0, 11.4)
    assert a4.ms_between(t.taken_at, t.off_at) == 400


def test_tracker_ignores_repeated_flags():
    t = a4.FingerTracker()
    assert t.update(NEEDED, 1.0) == ["needed"]
    assert t.update(NEEDED, 2.0) == []


def test_tracker_retry_reset_is_not_an_image_taken():
    # The driver reports finger present, then asks for a retry: the core never enters AWAIT_FINGER_OFF
    # and completion resets NEEDED|PRESENT -> NONE in one notify.
    t, events = feed([(NEEDED | PRESENT, 1.0), (0, 2.0)])
    assert events == [["needed", "present"], ["lifted"]]
    assert all("taken" not in e for e in events)
    assert t.taken_at is None and t.off_at is None


def test_tracker_lifted_before_image():
    t, events = feed([(NEEDED, 1.0), (NEEDED | PRESENT, 2.0), (NEEDED, 3.0)])
    assert events[-1] == ["lifted"]
    assert t.taken_at is None and t.off_at is None


def test_tracker_cancel_without_finger_is_not_taken():
    t, events = feed([(NEEDED, 1.0), (0, 2.0)])
    assert events[-1] == []
    assert t.taken_at is None


def test_prompts_use_the_documented_texts():
    assert a4.prompt_for("needed", 2, 3) == "Capture 2/3: touch the sensor now"
    assert a4.prompt_for("present", 2, 3) == "Finger detected: keep still, then lift when told"
    assert a4.prompt_for("taken", 2, 3) == "Image taken: lift your finger now"
    assert a4.prompt_for("off", 2, 3) is None


def test_timing_arithmetic_and_format():
    assert a4.ms_between(5.0, 5.0124) == 12
    assert a4.ms_between(None, 5.0) is None
    assert a4.format_timing(10.0, 10.4, 10.43) == (
        "finger off 400 ms after image taken, capture completed 430 ms after image taken"
    )
    assert a4.format_timing(10.0, None, 10.43) == (
        "finger off n/a after image taken, capture completed 430 ms after image taken"
    )
    assert "n/a after image taken, capture completed n/a" in a4.format_timing(None, None, 3.0)


# ------------------------------------------------------------------ environment


@pytest.mark.parametrize("raw, want", [("", 60.0), ("12.5", 12.5), ("3600", 3600.0)])
def test_timeout_from_env_valid(monkeypatch, raw, want):
    monkeypatch.setenv("A4_TIMEOUT_S", raw)
    assert a4.timeout_from_env() == want


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "nan", "inf", "3601"])
def test_timeout_from_env_invalid(monkeypatch, capsys, raw):
    monkeypatch.setenv("A4_TIMEOUT_S", raw)
    assert a4.timeout_from_env() is None
    assert "A4_TIMEOUT_S" in capsys.readouterr().err


def test_stamp_from_env(monkeypatch):
    monkeypatch.setenv("A4_STAMP", "20261014-183546")
    assert a4.stamp_from_env() == "20261014-183546"
    monkeypatch.setenv("A4_STAMP", "../../etc/x")
    assert a4.STAMP_RE.match(a4.stamp_from_env())


# ------------------------------------------------------------------ wrapper: terminal vs log

WRAPPER = HERE / "dev" / "a4-capture.sh"


def wrapper_pattern(name: str) -> str:
    """The value of NAME='...' in a4-capture.sh (single-quoted, one line)."""
    import re

    m = re.search(rf"^{name}='([^']*)'$", WRAPPER.read_text(), re.MULTILINE)
    assert m, name
    return m.group(1)


def grep(args, lines):
    import subprocess

    r = subprocess.run(
        ["grep", *args], input="".join(f"{x}\n" for x in lines), capture_output=True, text=True, check=False
    )
    return r.stdout.splitlines()


# Line shapes of a real a4 log, with a fake serial.
GLIB_DEBUG = [
    "(process:285386): libfprint-synaptics0078-DEBUG: 10:40:54.425: Sending command 0x01 (1 bytes)",
    "(process:285386): libfprint-synaptics0078-DEBUG: 10:40:54.430: Serial 0123456789ab",
    "(process:285386): libfprint-synaptics0078-DEBUG: 10:40:55.000: Finger off after 1 probe(s), 512 ms",
    "(process:285386): libfprint-image_device-DEBUG: 10:40:55.001: Image device reported finger status: off",
    "(process:1): libfprint-synaptics0078-INFO: 10:00:00.000: ROM version 10.1 (build 1)",
]
GLIB_WARN = ["(process:285386): libfprint-synaptics0078-WARNING **: 10:40:56.000: Cleanup command 0x04 failed: x"]
SCRIPT = [
    "driver: synaptics0078",
    "Capture 1/3: touch the sensor now",
    "Finger detected: keep still, then lift when told",
    "Image taken: lift your finger now",
    "image 1/3: delivered 288x112, upscale x2, saved 144x56 to /x/a4-1.pgm",
    "retry: Capture failed (GENERAL), touch again (attempt 1/3)",
    "PASS: 3/3 images delivered",
    "Traceback (most recent call last):",
]


def test_terminal_filter_drops_the_debug_stream_only():
    shown = grep(["-vE", "--", wrapper_pattern("GLIB_DEBUG_LINE")], GLIB_DEBUG + GLIB_WARN + SCRIPT)
    assert shown == GLIB_WARN + SCRIPT


def test_terminal_pipeline_logs_everything_then_filters():
    text = WRAPPER.read_text()
    assert '| tee -i -a -- "$log" | (' in text
    assert "trap '' INT" in text and 'exec grep --line-buffered -vE -- "$GLIB_DEBUG_LINE"' in text
    assert "rc=${PIPESTATUS[0]}" in text


def test_summary_lists_driver_info_lines_but_not_the_serial():
    shown = grep(["-E", "--", wrapper_pattern("DRIVER_LINES")], GLIB_DEBUG + GLIB_WARN + SCRIPT)
    assert shown == [GLIB_DEBUG[2], GLIB_DEBUG[4], GLIB_WARN[0]]
