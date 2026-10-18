"""Offline tests of tools/dev/b3_capture.py and the b3-capture.sh wrapper's argument handling.

No PyGObject, no device, no USB: the session runs against a fake reader, a fake clock and a fake sleep, and
every image is synthetic (random bytes), written only under pytest's tmp_path.
"""

import ast
import io
import json
import os
import random
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "dev"))

import a5_session as a5
import b3_capture as b3
import parsers

WRAPPER = HERE / "dev" / "b3-capture.sh"
FORK_DRIVER = HERE.parent.parent / "libfprint" / "libfprint" / "drivers" / "synaptics0078.c"
W, H = 144, 56
TZ = timezone(timedelta(hours=-3))
NOW = datetime(2026, 10, 16, 10, 0, 0, tzinfo=TZ)
TODAY = "2026-10-16"


def native(seed: int) -> bytes:
    return random.Random(seed).randbytes(W * H)


def near(pixels: bytes, seed: int = 0) -> bytes:
    """The same touch: every pixel moved by at most 3 grey levels."""
    rnd = random.Random(seed)
    return bytes(min(255, max(0, p + rnd.randint(-3, 3))) for p in pixels)


def upscale2(pixels: bytes) -> bytes:
    rows = []
    for y in range(H):
        row = b"".join(bytes([p]) * 2 for p in pixels[y * W : (y + 1) * W])
        rows.extend([row, row])
    return b"".join(rows)


def cap(pixels: bytes) -> b3.Capture:
    """What the driver delivers: the 2x replication of a native image."""
    return b3.Capture(upscale2(pixels), 2 * W, 2 * H, "finger off 900 ms after image taken")


class FakeReader:
    """Plays a script of Capture / ReaderError items; emits the finger-status events a4's tracker would."""

    def __init__(self, script=(), open_error=None):
        self.script = list(script)
        self.open_error = open_error
        self.opened = self.closed = False
        self.captures = 0
        self.interrupted = False
        self.library_path = "/usr/lib/libfprint-2.so.2.0.0"

    def open(self):
        if self.open_error is not None:
            raise self.open_error
        self.opened = True

    def capture(self, on_event):
        assert self.opened and not self.closed
        self.captures += 1
        item = self.script.pop(0)
        on_event("needed")
        on_event("present")
        if isinstance(item, Exception):
            raise item
        on_event("taken")
        on_event("off")
        return item

    def close(self):
        self.closed = True


class Run:
    def __init__(self, tmp_path, script=(), labels=("left-thumb",), touches=2, person="me", now=NOW, **kw):
        self.term, self.log = io.StringIO(), io.StringIO()
        self.reader = FakeReader(script, **kw)
        self.sleeps = []
        self.root = tmp_path / "local" / "b3"
        self.session = b3.Session(
            reader=self.reader,
            labels=list(labels),
            touches=touches,
            person=person,
            io=a5.Output(self.term, self.log),
            sleep=self.sleeps.append,
            now=lambda: now,
            root=self.root,
            library={"path": "/usr/lib/libfprint-2.so.2", "package": "libfprint-sigfm-0078 1.94.100-1"},
        )

    def __call__(self):
        self.rc = self.session.run()
        return self.rc

    @property
    def lines(self):
        return self.term.getvalue().splitlines()

    def label_dir(self, label, person="me"):
        return self.root / person / label


def seed_sample(directory: Path, index: int, session: str, pixels: bytes | None = None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{index:03d}.pgm").write_bytes(parsers.pgm_bytes(pixels or native(1000 + index), W, H))
    (directory / f"{index:03d}.json").write_text(json.dumps({"session": session, "index": index}))


# ------------------------------------------------------------------ arguments


def test_defaults():
    args = b3.parse_args([])
    assert args.fingers == list(b3.LABELS)
    assert (args.touches, args.person) == (20, "me")
    assert b3.LABELS == (
        "left-thumb",
        "left-index",
        "left-middle",
        "left-ring",
        "left-little",
        "right-thumb",
        "right-index",
        "right-middle",
        "right-ring",
        "right-little",
    )


def test_fingers_are_put_in_session_order_and_accept_the_finger_suffix():
    assert b3.parse_args(["--fingers", "right-index,left-index"]).fingers == ["left-index", "right-index"]
    assert b3.parse_args(["--fingers", " Right-Middle-finger "]).fingers == ["right-middle"]


@pytest.mark.parametrize(
    "argv, needle",
    [
        (["--fingers", "left-pinky"], "left-thumb, left-index"),
        (["--fingers", "left-index,left-index"], "listed twice"),
        (["--fingers", ""], "unknown finger"),
        (["--touches", "0"], "1 to 100"),
        (["--touches", "101"], "1 to 100"),
        (["--touches", "abc"], "1 to 100"),
        (["--touches", "-3"], "1 to 100"),
        (["--person", "Me"], "short name"),
        (["--person", "../x"], "short name"),
        (["--person=-x"], "short name"),
        (["--person", "a" * 33], "short name"),
        (["--bogus"], "unrecognized"),
        # The folders the analysis skips (RESERVED in research/b3/b3_analyze.py).
        (["--person", "results"], "reserved folder name"),
        (["--person", "bin"], "reserved folder name"),
        (["--person", "logs"], "reserved folder name"),
        # No abbreviations: the wrapper blocks the hidden options by full name only.
        (["--valid"], "unrecognized arguments: --valid"),
        (["--lo", "/tmp/x"], "unrecognized arguments: --lo"),
        (["--pack", "x"], "unrecognized arguments: --pack"),
        (["--touch", "3"], "unrecognized arguments: --touch"),
    ],
)
def test_misuse_exits_2_with_the_reason(capsys, argv, needle):
    with pytest.raises(SystemExit) as exc:
        b3.parse_args(argv)
    assert exc.value.code == 2
    assert needle in capsys.readouterr().err


@pytest.mark.parametrize("person", ["me", "guest-1", "ana_b", "0", "a" * 32])
def test_person_names_accepted(person):
    assert b3.parse_args(["--person", person]).person == person


def test_reserved_persons_mirror_the_analysis():
    analyze = HERE.parent / "research" / "b3" / "b3_analyze.py"
    if not analyze.is_file():
        pytest.skip("research/b3/b3_analyze.py not present")
    m = re.search(r"^RESERVED = (\{[^}]*\})$", analyze.read_text(), re.MULTILINE)
    assert m and ast.literal_eval(m.group(1)) == set(b3.RESERVED_PERSONS)


def test_display_names():
    assert b3.display("left-middle") == "LEFT MIDDLE"
    assert b3.display("right-thumb") == "RIGHT THUMB"


def test_estimate_minutes():
    assert b3.estimate_minutes(200, 10) == 11  # 600 s + 30 s
    assert b3.estimate_minutes(1, 1) == 1


def test_same_touch_threshold_matches_the_driver():
    if not FORK_DRIVER.is_file():
        pytest.skip("libfprint fork not checked out next to this project")
    m = re.search(r"#define SYNAPTICS0078_NEW_TOUCH_DIFF (\d+)", FORK_DRIVER.read_text())
    assert m and int(m.group(1)) == b3.NEW_TOUCH_DIFF == 30


# ------------------------------------------------------------------ a normal session


def test_session_prompts_files_and_modes(tmp_path):
    old = os.umask(0o022)
    try:
        run = Run(
            tmp_path, [cap(native(1)), cap(native(2)), cap(native(3)), cap(native(4))], ("left-thumb", "right-little")
        )
        assert run() == 0
    finally:
        os.umask(old)
    lines = run.lines
    assert lines[0].startswith("Fingerprint capture for the false-accept test (person: me)")
    assert "2 finger(s), 2 touches each: 4 touches to go, about 1 minute(s)." in lines
    assert any("the way you would to unlock" in line for line in lines)
    assert any("Ctrl-C stops safely" in line for line in lines)
    i = lines.index("Next: LEFT THUMB — get it ready")
    assert lines[i + 1 : i + 5] == [
        "Finger 1 of 2: LEFT THUMB. Touch 1 of 2… TOUCH",
        "LIFT",
        "Finger 1 of 2: LEFT THUMB. Touch 2 of 2… TOUCH",
        "LIFT",
    ]
    assert "Finger 2 of 2: RIGHT LITTLE. Touch 2 of 2… TOUCH" in lines
    assert lines[-1] == f"Done: saved 4 image(s) for 2 finger(s) in {run.root / 'me'}/"
    assert run.sleeps == [b3.CHANGE_PAUSE_S, b3.CHANGE_PAUSE_S]
    assert run.reader.closed

    for d in (run.root, run.root / "me", run.label_dir("left-thumb"), run.label_dir("right-little")):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    names = sorted(os.listdir(run.label_dir("left-thumb")))
    assert names == ["001.json", "001.pgm", "002.json", "002.pgm"]  # no temp files left
    for name in names:
        assert stat.S_IMODE((run.label_dir("left-thumb") / name).stat().st_mode) == 0o600
    pixels, w, h = b3.read_pgm(run.label_dir("right-little") / "002.pgm")
    assert (pixels, w, h) == (native(4), W, H)


def test_sidecar_content(tmp_path):
    run = Run(tmp_path, [cap(native(1)), cap(native(2))])
    assert run() == 0
    first = json.loads((run.label_dir("left-thumb") / "001.json").read_text())
    second = json.loads((run.label_dir("left-thumb") / "002.json").read_text())
    q = parsers.image_quality(native(2), W, H)
    assert second == {
        "time": "2026-10-16T10:00:00-03:00",
        "session": TODAY,
        "person": "me",
        "label": "left-thumb",
        "index": 2,
        "attempts": 1,
        "image": {"width": 144, "height": 56, **q},
        "diff_prev": round(parsers.mean_abs_diff(native(2), native(1)), 2),
        "delivered": {"width": 288, "height": 112, "upscale": 2},
        # The path of the libfprint the reader mapped replaces the wrapper's default.
        "library": {"path": "/usr/lib/libfprint-2.so.2.0.0", "package": "libfprint-sigfm-0078 1.94.100-1"},
    }
    assert first["diff_prev"] is None
    assert "serial" not in json.dumps(first).lower()


def test_terminal_shows_nothing_debug_like(tmp_path):
    script = [
        cap(native(1)),
        b3.ReaderError("retry", "Please try again (general)", "fp - device - retry - quark", 0),
        cap(near(native(1))),
        cap(native(2)),
    ]
    run = Run(tmp_path, script)
    assert run() == 0
    allowed = re.compile(
        r"^(|Fingerprint capture .*|\d+ finger\(s\), .*|For each touch: .*|Ctrl-C stops safely.*|Next: [A-Z ]+ — get it ready"
        r"|Finger \d+ of \d+: [A-Z ]+\. Touch \d+ of \d+… TOUCH|LIFT|Touch again… TOUCH|[A-Z ]+ done\.|Done: saved .*)$"
    )
    for line in run.lines:
        assert allowed.match(line), line
    text = run.term.getvalue().lower()
    for word in ("diff", "domain", "retry", "attempt", "debug", "traceback", ".pgm", "same touch", "quark"):
        assert word not in text
    log = run.log.getvalue()
    assert "retry: Please try again (general)" in log and "same touch as the previous image" in log


# ------------------------------------------------------------------ touch again


def test_same_touch_is_rejected_then_a_new_touch_is_accepted(tmp_path):
    run = Run(tmp_path, [cap(native(1)), cap(near(native(1))), cap(native(2))])
    assert run() == 0
    lines = run.lines
    i = lines.index("Finger 1 of 1: LEFT THUMB. Touch 2 of 2… TOUCH")
    assert lines[i : i + 4] == ["Finger 1 of 1: LEFT THUMB. Touch 2 of 2… TOUCH", "LIFT", "Touch again… TOUCH", "LIFT"]
    assert b3.read_pgm(run.label_dir("left-thumb") / "002.pgm")[0] == native(2)
    assert json.loads((run.label_dir("left-thumb") / "002.json").read_text())["attempts"] == 2
    assert re.search(
        r"\[again\] left-thumb 002 attempt 1: same touch as the previous image \(diff \d\.\d < 30\)", run.log.getvalue()
    )


def test_retry_error_is_touch_again(tmp_path):
    retry = b3.ReaderError("retry", "Please try again (too-short)", "fp - device - retry - quark", 1)
    run = Run(tmp_path, [retry, cap(native(1))], touches=1)
    assert run() == 0
    assert run.lines.count("Touch again… TOUCH") == 1
    assert json.loads((run.label_dir("left-thumb") / "001.json").read_text())["attempts"] == 2


def test_timeout_is_touch_again(tmp_path):
    run = Run(tmp_path, [b3.ReaderError("timeout", "no image within 60 s"), cap(native(1))], touches=1)
    assert run() == 0
    assert "Touch again… TOUCH" in run.lines


def test_bad_replication_is_touch_again(tmp_path):
    bad = bytearray(upscale2(native(1)))
    bad[1] ^= 0xFF  # one 2x2 block is not uniform
    run = Run(tmp_path, [b3.Capture(bytes(bad), 2 * W, 2 * H), cap(native(2))], touches=1)
    assert run() == 0
    assert "Touch again… TOUCH" in run.lines
    assert "bad capture: delivered 288x112 is not a uniform 2x2 upscale" in run.log.getvalue()
    assert b3.read_pgm(run.label_dir("left-thumb") / "001.pgm")[0] == native(2)


def test_odd_size_is_touch_again(tmp_path):
    run = Run(tmp_path, [b3.Capture(bytes(100 * 40), 100, 40), cap(native(2))], touches=1)
    assert run() == 0
    assert "not an integer multiple" in run.log.getvalue()


def test_too_many_failures_stop_and_keep_what_was_saved(tmp_path):
    retry = b3.ReaderError("retry", "Please try again (general)", "fp - device - retry - quark", 0)
    run = Run(tmp_path, [cap(native(1))] + [retry] * b3.MAX_FAILURES, touches=3)
    assert run() == 1
    assert run.lines.count("Touch again… TOUCH") == b3.MAX_FAILURES - 1
    assert run.lines[-2] == (
        f"Stopped: {b3.MAX_FAILURES} touches in a row did not count. What was saved is kept; "
        "run the same command again to continue."
    )
    assert run.lines[-1] == f"Saved 1 image(s) this run in {run.root / 'me'}/"
    assert sorted(os.listdir(run.label_dir("left-thumb"))) == ["001.json", "001.pgm"]
    assert run.reader.closed


# ------------------------------------------------------------------ resume


def test_resume_skips_complete_continues_partial_ignores_other_days_and_orphans(tmp_path):
    run = Run(
        tmp_path,
        [cap(native(50)), cap(native(51)), cap(native(52))],
        ("left-thumb", "left-index", "left-middle"),
        touches=2,
    )
    thumb, index_ = run.label_dir("left-thumb"), run.label_dir("left-index")
    seed_sample(thumb, 1, TODAY)
    seed_sample(thumb, 2, TODAY)
    seed_sample(index_, 1, "2026-10-15")  # another day: does not count
    seed_sample(index_, 2, "2026-10-15")
    seed_sample(index_, 3, TODAY)
    (index_ / "004.pgm").write_bytes(b"orphan")  # an image without its sidecar
    (index_ / "007.json").write_text("{}")  # a sidecar without its image
    assert run() == 0
    lines = run.lines
    assert lines[0] == "LEFT THUMB: already done today (2 of 2), skipping"
    assert lines[1] == "LEFT INDEX: 1 of 2 done today, continuing"
    assert "Next: LEFT THUMB — get it ready" not in lines
    assert "Finger 2 of 3: LEFT INDEX. Touch 2 of 2… TOUCH" in lines
    assert "Finger 3 of 3: LEFT MIDDLE. Touch 1 of 2… TOUCH" in lines
    assert "2 finger(s), 2 touches each: 3 touches to go, about 1 minute(s)." in lines
    # The partial label continued numbering after the last complete sample and overwrote the orphan.
    assert b3.read_pgm(index_ / "004.pgm")[0] == native(50)
    assert json.loads((index_ / "004.json").read_text())["session"] == TODAY
    assert sorted(os.listdir(thumb)) == ["001.json", "001.pgm", "002.json", "002.pgm"]
    assert b3.read_pgm(run.label_dir("left-middle") / "001.pgm")[0] == native(51)
    assert run.lines[-1] == f"Done: saved 3 image(s) for 2 finger(s) in {run.root / 'me'}/"


def test_another_day_captures_again(tmp_path):
    run = Run(tmp_path, [cap(native(1))], touches=1, now=NOW + timedelta(days=1))
    seed_sample(run.label_dir("left-thumb"), 1, TODAY)
    assert run() == 0
    meta = json.loads((run.label_dir("left-thumb") / "002.json").read_text())
    assert (meta["session"], meta["index"], meta["diff_prev"]) == ("2026-10-17", 2, None)


def test_resume_guards_against_the_last_saved_sample_of_today(tmp_path):
    run = Run(tmp_path, [cap(near(native(9))), cap(native(10))], touches=2)
    seed_sample(run.label_dir("left-thumb"), 1, TODAY, native(9))
    assert run() == 0
    assert "Touch again… TOUCH" in run.lines
    assert b3.read_pgm(run.label_dir("left-thumb") / "002.pgm")[0] == native(10)


def test_nothing_to_do_does_not_open_the_reader(tmp_path):
    run = Run(tmp_path, [], touches=1)
    seed_sample(run.label_dir("left-thumb"), 1, TODAY)
    assert run() == 0
    assert not run.reader.opened and not run.reader.closed
    assert run.lines[-1].startswith("Nothing to capture")


def test_scan_ignores_unreadable_sidecars_and_temp_files(tmp_path):
    d = tmp_path / "x"
    seed_sample(d, 1, TODAY)
    seed_sample(d, 2, TODAY)
    (d / "002.json").write_text("{not json")
    (d / ".003.pgm.abc.tmp").write_bytes(b"x")
    assert [s.index for s in b3.scan_label(d)] == [1]
    st = b3.label_state("left-thumb", b3.scan_label(d), TODAY)
    assert (st.done, st.next_index, st.last.index) == (1, 2, 1)


# ------------------------------------------------------------------ writing


def test_image_is_written_before_its_sidecar(tmp_path, monkeypatch):
    order = []
    real = b3.write_atomic
    monkeypatch.setattr(b3, "write_atomic", lambda path, data: (order.append(path.name), real(path, data)))
    run = Run(tmp_path, [cap(native(1))], touches=1)
    assert run() == 0
    assert order == ["001.pgm", "001.json"]


def test_write_atomic_replaces_and_leaves_no_temp(tmp_path):
    target = tmp_path / "001.pgm"
    target.write_bytes(b"old")
    os.chmod(target, 0o644)
    b3.write_atomic(target, b"new")
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert os.listdir(tmp_path) == ["001.pgm"]


def test_save_failure_stops_plainly(tmp_path, monkeypatch):
    def boom(path, data):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(b3, "write_atomic", boom)
    run = Run(tmp_path, [cap(native(1))], touches=1)
    assert run() == 1
    assert run.lines[-2] == f"Cannot save the image in {run.root / 'me'}/: Permission denied"
    assert run.reader.closed


def test_symlinked_person_dir_is_refused(tmp_path):
    run = Run(tmp_path, [cap(native(1))], touches=1)
    run.root.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (run.root / "me").symlink_to(tmp_path / "elsewhere")
    assert run() == 1
    assert run.lines[-1].startswith("Cannot write to ")
    assert not run.reader.opened


# ------------------------------------------------------------------ device errors


GUSB = "g-usb-device-error-quark"
FPERR = "fp - device - error - quark"


@pytest.mark.parametrize(
    "domain, code, message, kind",
    [
        (GUSB, 10, "USB error on device 06cb:0078 : Resource busy [-6]", "busy"),
        (GUSB, 9, "USB error on device 06cb:0078 : Access denied (insufficient permissions) [-3]", "permission"),
        (FPERR, 4, "The device is still busy with another operation, please try again later.", "busy"),
        (FPERR, 257, "Device disabled to prevent overheating.", "too-hot"),
        (FPERR, 256, "This device has been removed from the system.", "removed"),
        (FPERR, 0, "Cannot read /x/module.bin: No such file or directory. The fingerprint-06cb-0078 setup ...", "data"),
        (FPERR, 6, "/x/module.bin is 10 bytes, expected 10500", "data"),
        (FPERR, 5, "Reply too short", "other"),
        ("b3", 0, "expected one synaptics0078 device, found 0", "no-device"),
        # Fallbacks on the message when the domain or code is not the expected one.
        ("other-quark", 99, "libusb_claim_interface: Resource busy", "busy"),
        (GUSB, 8, "Failed: LIBUSB_ERROR_ACCESS: Access denied (insufficient permissions)", "permission"),
        ("g-io-error-quark", 14, "Permission denied", "permission"),
        ("g-io-error-quark", 19, "Operation was cancelled", "other"),
    ],
)
def test_error_kind(domain, code, message, kind):
    assert b3.error_kind(domain, code, message) == kind


def test_busy_and_permission_messages():
    busy = b3.error_message(GUSB, 10, "Resource busy")
    assert "in use by fprintd or another program" in busy
    assert "close any fingerprint prompt" in busy.lower() and "wait a moment and run again" in busy
    assert "sudo systemctl stop fprintd" in busy
    perm = b3.error_message(GUSB, 9, "Access denied")
    assert "getfacl /dev/bus/usb/BBB/DDD" in perm
    assert "Re-plug the reader, or log out and in again" in perm
    assert "/etc/udev/rules.d/70-fingerprint-06cb-0078.rules" in perm
    assert "sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usb --action=add" in perm
    assert "getfacl /dev/bus/usb/001/004 " in b3.error_message(GUSB, 9, "Access denied", "/dev/bus/usb/001/004")
    assert "tools/synaptics0078_data.py" in b3.error_message(FPERR, 0, "Cannot read /x")
    assert b3.error_message(FPERR, 5, "x") == b3.MSG_DEVICE


def test_open_busy_stops_with_the_plain_message(tmp_path):
    err = b3.ReaderError("error", "USB error on device 06cb:0078 : Resource busy [-6]", GUSB, 10)
    run = Run(tmp_path, [], touches=1, open_error=err)
    assert run() == 1
    assert run.lines == [b3.MSG_BUSY]  # no intro, no "saved" line
    assert run.reader.closed
    assert "open: domain='g-usb-device-error-quark' code=10: USB error" in run.log.getvalue()


def test_open_permission_names_the_readers_node(tmp_path):
    err = b3.ReaderError("error", "USB error on device 06cb:0078 : Access denied [-3]", GUSB, 9)
    run = Run(tmp_path, [], touches=1, open_error=err)
    run.reader.node = "/dev/bus/usb/003/007"
    assert run() == 1
    assert run.lines[0].startswith("No permission to use the reader. Check it with: getfacl /dev/bus/usb/003/007 ")
    assert run.lines[-1].startswith("  sudo udevadm control --reload")


def test_device_error_during_capture_stops(tmp_path):
    err = b3.ReaderError("error", "Device disabled to prevent overheating.", FPERR, 257)
    run = Run(tmp_path, [cap(native(1)), err], touches=3)
    assert run() == 1
    assert run.lines[-2:] == [b3.MSG_TOO_HOT, f"Saved 1 image(s) this run in {run.root / 'me'}/"]
    assert run.reader.closed


def test_interrupt_during_capture_stops_safely(tmp_path):
    run = Run(tmp_path, [cap(native(1)), b3.ReaderError("interrupted", "Operation was cancelled")], touches=3)
    assert run() == 1
    assert run.lines[-2].startswith("Stopped (Ctrl-C). What was saved is kept")
    assert run.reader.closed


def test_interrupt_during_the_finger_change_pause(tmp_path):
    run = Run(tmp_path, [], touches=1)
    run.session.sleep = lambda s: setattr(run.reader, "interrupted", True)
    assert run() == 1
    assert run.reader.captures == 0 and run.reader.closed
    assert run.lines[-2].startswith("Stopped (Ctrl-C)")


def test_keyboard_interrupt_is_a_safe_stop(tmp_path):
    class Boom(FakeReader):
        def capture(self, on_event):
            raise KeyboardInterrupt

    run = Run(tmp_path, [], touches=1)
    run.session.reader = run.reader = Boom()
    assert run() == 1
    assert run.reader.closed and run.lines[-2].startswith("Stopped (Ctrl-C)")


# ------------------------------------------------------------------ helpers of the GI part and main


def test_loaded_library_reads_maps(tmp_path):
    maps = tmp_path / "maps"
    maps.write_text(
        "7f00-7f01 r--p 00000000 00:1a 123 /usr/lib/libglib-2.0.so.0.8400.0\n"
        "7f02-7f03 r-xp 00001000 00:1a 456 /usr/lib/libfprint-2.so.2.0.0\n"
    )
    assert b3.loaded_library(str(maps)) == "/usr/lib/libfprint-2.so.2.0.0"
    assert b3.loaded_library(str(tmp_path / "none")) == "unknown"


def _usb(sysfs: Path, name: str, vid: str, pid: str, bus: str, num: str) -> None:
    d = sysfs / name
    d.mkdir(parents=True)
    for key, value in (("idVendor", vid), ("idProduct", pid), ("busnum", bus), ("devnum", num)):
        (d / key).write_text(value + "\n")


def test_usb_node_from_sysfs(tmp_path):
    sysfs = tmp_path / "devices"
    assert b3.usb_node(str(sysfs)) is None  # no sysfs
    sysfs.mkdir()
    (sysfs / "usb1").mkdir()  # a hub without the files
    _usb(sysfs, "1-2", "8087", "0026", "1", "2")
    assert b3.usb_node(str(sysfs)) is None
    _usb(sysfs, "1-9", "06cb", "0078", "1", "4")
    assert b3.usb_node(str(sysfs)) == "/dev/bus/usb/001/004"
    _usb(sysfs, "3-1", "06cb", "0078", "3", "12")
    assert b3.usb_node(str(sysfs)) is None  # two readers: not one node to name


class FakeGLib:
    """Records the log-routing calls; `modern` = GLib 2.68+ (log_writer_default_set_use_stderr)."""

    class LogWriterOutput:
        HANDLED = 1

    PRIORITY_HIGH = -100
    SOURCE_CONTINUE = True

    def __init__(self, calls: list, modern: bool = True):
        self.calls = calls
        self.writer = None
        if modern:
            self.log_writer_default_set_use_stderr = lambda flag: calls.append(("use_stderr", flag))

    def log_set_writer_func(self, func, user_data):
        self.calls.append(("writer_func",))
        self.writer = func

    def log_writer_format_fields(self, level, fields, use_color):
        return f"(process:1): drv-DEBUG: {fields}"

    def unix_signal_add(self, priority, signum, handler):
        self.calls.append(("signal",))
        return 7

    def source_remove(self, source):
        pass


def test_route_glib_log_to_stderr_modern_and_fallback(capsys):
    calls = []
    assert b3.route_glib_log_to_stderr(FakeGLib(calls)) == "log_writer_default_set_use_stderr"
    assert calls == [("use_stderr", True)]
    calls.clear()
    old = FakeGLib(calls, modern=False)
    assert b3.route_glib_log_to_stderr(old) == "log_set_writer_func"
    assert calls == [("writer_func",)]
    assert old.writer(128, "probe", None) == FakeGLib.LogWriterOutput.HANDLED
    out = capsys.readouterr()
    assert out.out == "" and out.err == "(process:1): drv-DEBUG: probe\n"


def test_setup_routes_glib_log_before_loading_fprint_and_before_the_context():
    """GLib's default writer prints DEBUG/INFO to stdout (the terminal); the routing must come first."""
    calls = []
    glib = FakeGLib(calls)

    class Flags:
        NEEDED, PRESENT = b3.a4.FINGER_NEEDED, b3.a4.FINGER_PRESENT

    class FakeFPrint:
        FingerStatusFlags = Flags

        @staticmethod
        def Context():
            calls.append(("context",))
            raise RuntimeError("stop here: no device in this test")

    class FakeGi:
        @staticmethod
        def require_version(name, version):
            raise ValueError("no GLibUnix in this test")

    reader = b3.GiReader(a5.Output(io.StringIO(), io.StringIO()))
    reader._glib = lambda: glib

    def load():
        calls.append(("fprint",))
        return FakeGi, FakeFPrint, object(), glib

    reader._gi = load
    with pytest.raises(RuntimeError, match="stop here"):
        reader.open()
    assert calls == [("use_stderr", True), ("fprint",), ("signal",), ("context",)]
    assert "GLib log lines go to stderr (the log) via log_writer_default_set_use_stderr" in reader.io.log.getvalue()


def test_read_pgm_roundtrip(tmp_path):
    p = tmp_path / "a.pgm"
    p.write_bytes(parsers.pgm_bytes(native(3), W, H))
    assert b3.read_pgm(p) == (native(3), W, H)
    p.write_bytes(b"P6\n1 1\n255\n\x00\x00\x00")
    with pytest.raises(ValueError):
        b3.read_pgm(p)


def test_module_does_not_import_gi():
    # The system python has gi; importing the module there must still not load it.
    py = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
    code = f"import sys; sys.path.insert(0, {str(HERE / 'dev')!r}); import b3_capture; print('gi' in sys.modules)"
    r = subprocess.run(
        [py, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout.strip() == "False"


def test_main_refuses_without_log_or_allowlist(tmp_path, monkeypatch, capsys):
    assert b3.main([]) == 2
    assert "b3-capture.sh" in capsys.readouterr().err
    monkeypatch.delenv("FP_DRIVERS_ALLOWLIST", raising=False)
    assert b3.main(["--log", str(tmp_path / "log")]) == 2
    assert "FP_DRIVERS_ALLOWLIST" in capsys.readouterr().err
    assert b3.main(["--validate-only", "--touches", "5"]) == 0


# ------------------------------------------------------------------ the wrapper (argument handling only)


def wrapper(*args):
    if not Path("/usr/bin/python3").is_file():
        pytest.skip("no /usr/bin/python3")
    if os.geteuid() == 0:
        pytest.skip("the wrapper refuses root")
    return subprocess.run(["bash", str(WRAPPER), *args], capture_output=True, text=True, timeout=30, check=False)


def test_wrapper_help():
    r = wrapper("--help")
    assert r.returncode == 0
    assert "--fingers right-index,left-index" in r.stdout and "never with sudo" in r.stdout


@pytest.mark.parametrize(
    "args, needle",
    [
        (("--touches", "0"), "1 to 100"),
        (("--fingers", "left-pinky"), "left-thumb"),
        (("--person", "Bad Name"), "short name"),
        (("--log", "/tmp/x"), "unknown argument"),
        (("--validate-only",), "unknown argument"),
        (("--valid",), "unrecognized arguments: --valid"),
        (("--lo", "/tmp/x"), "unrecognized arguments: --lo"),
        (("--person", "results"), "reserved folder name"),
    ],
)
def test_wrapper_misuse_exits_2_before_any_device_check(args, needle):
    r = wrapper(*args)
    assert r.returncode == 2
    assert needle in r.stderr
    assert "reader" not in r.stdout


GI_GLUE = """
import io, os, signal, sys, time
sys.path.insert(0, sys.argv[1])
import b3_capture as b3, a5_session as a5
log = io.StringIO()
r = b3.GiReader(a5.Output(io.StringIO(), log))
r.setup()  # routes GLib's log, loads FPrint and takes over Ctrl-C; no Context, no device
from gi.repository import GLib
# A debug line as the driver's would be (G_MESSAGES_DEBUG=all): it must reach stderr, never stdout.
GLib.log_default_handler("b3-probe", GLib.LogLevelFlags.LEVEL_DEBUG, "b3-debug-probe", None)
GLib.log_default_handler("b3-probe", GLib.LogLevelFlags.LEVEL_INFO, "b3-info-probe", None)
r.wait(0.05)
assert not r.interrupted
t = time.monotonic()
os.kill(os.getpid(), signal.SIGINT)
r.wait(5)
assert r.interrupted and time.monotonic() - t < 2, "Ctrl-C not seen during wait"
r.close()
print("typelib" in log.getvalue(), "/usr/lib/girepository-1.0/FPrint-2.0.typelib" in log.getvalue())
"""


def test_gi_glue_with_the_system_typelib_without_a_device():
    """Only with the system python and its PyGObject: FPrint loads, GLib's debug lines go to stderr (not the
    terminal) and Ctrl-C is caught. The reader is never looked for (no FPrint.Context), let alone opened."""
    py = "/usr/bin/python3"
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "GI_TYPELIB_PATH")}
    env["G_MESSAGES_DEBUG"] = "all"  # as the wrapper sets it
    probe = 'import gi; gi.require_version("FPrint", "2.0"); from gi.repository import FPrint'
    if (
        not Path(py).is_file()
        or subprocess.run([py, "-c", probe], env=env, capture_output=True, check=False).returncode
    ):
        pytest.skip("no system python with PyGObject and the FPrint 2.0 typelib")
    r = subprocess.run(
        [py, "-c", GI_GLUE, str(HERE / "dev")], env=env, capture_output=True, text=True, timeout=30, check=False
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["True", "True"]  # nothing else on stdout
    assert "b3-probe-DEBUG" in r.stderr and "b3-debug-probe" in r.stderr
    assert "b3-info-probe" in r.stderr
