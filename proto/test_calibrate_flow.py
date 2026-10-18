"""Offline tests of calibrate.py against a fake pyusb: no device, no libusb, never real USB.

The dry run is checked with every `usb` import blocked. The --run flow is driven by a
fake reader that produces synthetic EP 0x82 frames: the exact write sequence, PASS,
FAIL on the check score, the stop paths with their cleanup, the allow-list guard, the
output modes, serial redaction and the terminal wording. Everything that builds a
calibration program is built on the synthetic template of synth_template.py (patched in for
the whole module), so nothing here needs proto/data/cmd02_template.bin.
"""

import contextlib
import hashlib
import importlib.abc
import io
import json
import os
import re
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import calib_model as cm
import calibrate
import calibration
import capture
from synth_template import setUpModule, tearDownModule  # noqa: F401 (unittest module fixtures)
from test_parsers import V63_QUERY_19, V63_ROM_INFO, V63_SENSOR_ID
from test_probe_flow import USBError, USBTimeoutError, fake_usb

SERIAL = bytes.fromhex("5e71a1c0ffee")  # distinctive: must never leave memory


def rom_info(byte17=0):
    r = bytearray(V63_ROM_INFO)
    r[0x12:0x18] = SERIAL
    r[17] = byte17
    return bytes(r)


MODULE = bytes((i * 11 + 5) & 0xFF for i in range(capture.MODULE_LEN))
FACTORY = bytes(0x70 + (i * 7) % 32 for i in range(56))


def header(k):
    """Line header of frame line k: byte 2 counts lines from 1 (so averaged pairs give 2r + 1)."""
    return bytes([0x01, 0xFE, k + 1, 0x02, 0xF0, 0xF7, 0x01, 0x00])


CF_PIX = [[0x80 + ((r * 7 + c * 3) % 41) - 20 for c in range(144)] for r in range(56)]
BF_RAW_PIX = [[128 + ((r + c) % 3) - 1 for c in range(144)] for r in range(56)]


def raw_capture(rows):
    """337 lines: frame 2 holds each row twice (second copy +1 on odd columns: floor keeps the row)."""
    out = bytearray()
    for k in range(cm.REQ_LINES):
        if 112 <= k < 224:
            j = k - 112
            px = rows[j // 2]
            if j % 2:
                px = [v + (c % 2) for c, v in enumerate(px)]
            out += header(j) + bytes(px)
        else:
            out += bytes((0x40 + (k * 3 + c) % 61) for c in range(152))
    assert len(out) == cm.RAW_LEN
    return bytes(out)


def averaged(rows):
    return b"".join(bytes([1, 0xFE, 2 * r + 1, 2, 0xF0, 0xF7, 1, 0]) + bytes(rows[r]) for r in range(56))


RAW1 = raw_capture(CF_PIX)
RAW2 = raw_capture(BF_RAW_PIX)
CF = averaged(CF_PIX)
BF = cm.decode_frame(averaged(BF_RAW_PIX))


def sub12(shift=0):
    v = bytearray(b"\x07\x00\x00\x00")
    for r in cm.CHECK_ROWS:
        v += bytes((max(-128, min(127, cm.q(x) + shift)) & 0xFF) for x in CF_PIX[r])
    return bytes(v)


def factory_reply(subtags=None):
    subtags = subtags if subtags is not None else {3: b"\x07\x00\x00\x00" + FACTORY, 2: bytes(8), 12: sub12()}
    body = b""
    ptr = 8
    for sub, value in subtags.items():
        body += struct.pack("<LHHHH", ptr, len(value), 0x0E, sub, 0x8000) + value
        ptr += 12 + len(value)
    return struct.pack("<HLL", 0, 0xC9C, len(subtags)) + body


def windows_calibration(serial=SERIAL, factory=FACTORY, frame=CF, baseline=BF) -> bytes:
    b = bytearray(calibration.MIN_CAL_LEN)
    struct.pack_into("<III", b, 4, 0xE4, 2, 0x2449)
    b[calibration.SERIAL_OFFSET : calibration.SERIAL_OFFSET + 6] = serial
    struct.pack_into("<I", b, calibration.CAL_STRUCT + 0x70, 56)
    struct.pack_into("<I", b, calibration.CAL_STRUCT + 0x88, 8512)
    for desc in (0x2294, 0x22B4):
        struct.pack_into("<II", b, desc + 4, 152, 56)
    b[calibration.FACTORY_OFFSET : calibration.FACTORY_OFFSET + 56] = factory
    b[calibration.FRAME_OFFSET : calibration.FRAME_OFFSET + 8512] = frame
    b[calibration.BASELINE_OFFSET : calibration.BASELINE_OFFSET + 8512] = baseline
    return bytes(b)


class CalDevice:
    """Fake reader: replies by opcode on EP 0x81, one EP 0x82 buffer per 02 write."""

    bus, address, bcdDevice = 1, 3, 0x0154

    def __init__(self, byte17=0, replies=None, frames=None, driver_active=False, frame_error=None):
        self.calls = []
        self.driver_active = driver_active
        self.replies = {
            0x01: rom_info(byte17),
            0x19: V63_QUERY_19,
            0x75: V63_SENSOR_ID,
            0x6F: factory_reply(),
            0x06: b"\x00\x00",
            0x02: [b"\x00\x00" + bytes(30), b"\x00\x00" + bytes(40)],
            0x04: b"\x12\x04",
            0x52: b"\x00\x00",
        }
        self.replies.update(replies or {})
        self.frames = list(frames if frames is not None else [RAW1, RAW2])
        self.frame_error = frame_error  # raised on the first EP 0x82 read
        self.current = b""
        self.last = None

    def get_active_configuration(self):
        return types.SimpleNamespace(bConfigurationValue=1)

    def set_configuration(self, *a):
        self.calls.append(("set_configuration", a))

    def is_kernel_driver_active(self, i):
        return self.driver_active

    def detach_kernel_driver(self, i):
        self.calls.append(("detach", i))

    def attach_kernel_driver(self, i):
        self.calls.append(("attach", i))

    def reset(self):
        self.calls.append(("reset",))

    def write(self, ep, data, timeout=None):
        data = bytes(data)
        self.calls.append(("write", ep, data))
        self.last = data[0]
        if self.last == 0x02:
            self.current = self.frames.pop(0) if self.frames else b""
        elif self.last == 0x06 and isinstance(self.replies[0x01], bytes):
            loaded = bytearray(self.replies[0x01])  # this unit: byte 17 reads 0x0e once the module is in
            loaded[17] = calibrate.EXPECTED_BYTE17_AFTER_MODULE
            self.replies[0x01] = bytes(loaded)
        return len(data)

    def read(self, ep, size, timeout=None):
        self.calls.append(("read", ep, size))
        if ep == 0x82:
            if self.frame_error is not None:
                exc, self.frame_error = self.frame_error, None
                raise exc
            if not self.current:
                raise USBTimeoutError("no data")
            out, self.current = self.current[:size], self.current[size:]
            return list(out)
        item = self.replies[self.last]
        if isinstance(item, list):
            item = item.pop(0) if len(item) > 1 else item[0]
        if isinstance(item, BaseException):
            raise item
        return list(item)

    def writes(self):
        return [c[2] for c in self.calls if c[0] == "write"]

    def names(self):
        return [c[0] for c in self.calls]


class Env:
    def __init__(self, windows=True, windows_blob=None):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.module = self.dir / "module.bin"
        self.module.write_bytes(MODULE)
        self.cal = self.dir / "cal.bin"
        if windows:
            self.cal.write_bytes(windows_blob or windows_calibration())
        self.out_root = self.dir / "local" / "calib"
        self.patches = [
            mock.patch.object(capture, "MODULE_SHA256", hashlib.sha256(MODULE).hexdigest()),
            mock.patch.object(calibrate, "OUT_ROOT", self.out_root),
        ]
        for p in self.patches:
            p.start()

    def argv(self, *extra):
        return ["--module", str(self.module), "--windows-calibration", str(self.cal), *extra]

    def run_dirs(self):
        return sorted(self.out_root.iterdir()) if self.out_root.is_dir() else []

    def close(self):
        for p in self.patches:
            p.stop()
        self._td.cleanup()


@contextlib.contextmanager
def environment(**kw):
    env = Env(**kw)
    try:
        yield env
    finally:
        env.close()


def run_cal(env, dev, *extra, umask=0o022, setup=None, via_main=False):
    """One --run against `dev`. via_main: through main(), so its SIGTERM handler is installed."""
    out, err = io.StringIO(), io.StringIO()
    old = os.umask(umask)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        with fake_usb(dev), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if setup:
                setup()
            if via_main:
                rc = calibrate.main(env.argv("--run", *extra))
            else:
                rc = calibrate.run_device(calibrate.build_parser().parse_args(env.argv("--run", *extra)))
    finally:
        os.umask(old)
        signal.signal(signal.SIGTERM, old_term)
    dirs = env.run_dirs()
    files = {}
    if dirs:
        files = {p.name: p.read_bytes() for p in dirs[-1].iterdir()}
    return types.SimpleNamespace(
        rc=rc, out=out.getvalue(), err=err.getvalue(), dir=dirs[-1] if dirs else None, files=files, dev=dev
    )


def program1():
    return cm.build_program1(calibration.load_template(), FACTORY)


def program2():
    return cm.build_program2(calibration.load_template(), FACTORY, CF)


def full_writes(module=True):
    return (
        [b"\x01", b"\x19", b"\x75", calibrate.REQ_FACTORY_BITS]
        + ([b"\x06" + MODULE, b"\x01"] if module else [])
        + [
            program1(),
            b"\x04",
            b"\x52",
            program2(),
            b"\x04",
            b"\x52",
        ]
    )


def through(n, extra=0):
    """Number of writes of the full sequence up to and including program #n, plus `extra`."""
    return full_writes().index(program1() if n == 1 else program2()) + 1 + extra


def usb_error(errno):
    e = USBError(f"errno {errno}")
    e.errno = errno
    return e


def sigterm_self():
    """Send SIGTERM to this process and give the handler time to run here."""
    os.kill(os.getpid(), signal.SIGTERM)
    for _ in range(100):
        time.sleep(0.01)
    raise AssertionError("the SIGTERM handler did not run")


# ------------------------------------------------------------------ dry run


class UsbBlocker(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.hits = []

    def find_spec(self, name, path=None, target=None):
        if name == "usb" or name.startswith("usb."):
            self.hits.append(name)
            raise ImportError(f"usb import blocked in this test: {name}")


@contextlib.contextmanager
def no_usb():
    blocker = UsbBlocker()
    saved = {k: v for k, v in sys.modules.items() if k == "usb" or k.startswith("usb.")}
    for k in saved:
        del sys.modules[k]
    sys.meta_path.insert(0, blocker)
    opened = []
    try:
        with mock.patch.object(calibrate.Transport, "open", lambda self: opened.append(1) or 1 / 0):
            yield blocker, opened
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(saved)


class DryRun(unittest.TestCase):
    def dry(self, env, *extra):
        out = io.StringIO()
        old = signal.getsignal(signal.SIGTERM)
        try:
            with no_usb() as (blocker, opened), contextlib.redirect_stdout(out):
                rc = calibrate.main(env.argv(*extra))
        finally:
            signal.signal(signal.SIGTERM, old)
        self.assertEqual(blocker.hits, [])
        self.assertEqual(opened, [])
        self.assertFalse(env.out_root.exists(), "the dry run must not write anything")
        return rc, out.getvalue()

    def test_default_is_dry_and_lists_everything(self):
        with environment() as env:
            rc, out = self.dry(env)
        self.assertEqual(rc, 0)
        for cmd in (b"\x01", b"\x19", b"\x75", b"\x04", b"\x52", calibrate.REQ_FACTORY_BITS):
            self.assertIn(hashlib.sha256(cmd).hexdigest(), out)
            self.assertRegex(out, rf"\b{cmd[:1].hex()}\s+{len(cmd)} B")
        self.assertRegex(out, rf"\b06\s+{1 + capture.MODULE_LEN} B")
        self.assertIn(hashlib.sha256(MODULE).hexdigest(), out)  # pinned module sha (patched to the fake)
        self.assertIn(hashlib.sha256(b"\x06" + MODULE).hexdigest(), out)
        self.assertIn(cm.PROGRAM1_SKELETON_SHA256, out)
        self.assertIn(cm.PROGRAM2_SKELETON_SHA256, out)
        self.assertRegex(out, r"\b02\s+1917 B")
        self.assertRegex(out, r"\b02\s+10269 B")
        self.assertIn("51224", out)
        self.assertRegex(
            out, r"(?m)^ 6\. 01\s+1 B .*ONLY after a 06: byte 17 is logged \(expect 0x0e\), information only"
        )
        self.assertRegex(out, r"(?m)^ 7\. 02\s+1917 B")
        self.assertRegex(out, r"(?m)^10\. 02\s+10269 B")
        self.assertIn("skeleton", out)
        self.assertIn("will differ from the preview", out)
        # preview from the (synthetic) Windows CalibrationData given
        t = calibration.load_template()
        self.assertIn(hashlib.sha256(cm.build_program1(t, FACTORY)).hexdigest(), out)
        self.assertIn(hashlib.sha256(cm.build_program2(t, FACTORY, CF)).hexdigest(), out)

    def test_synthetic_preview_without_windows_data(self):
        with environment(windows=False) as env:
            rc, out = self.dry(env)
        self.assertEqual(rc, 0)
        t = calibration.load_template()
        self.assertIn("not found", out)
        self.assertIn(hashlib.sha256(cm.build_program1(t, calibrate.SYNTHETIC_FACTORY)).hexdigest(), out)
        p2 = cm.build_program2(t, calibrate.SYNTHETIC_FACTORY, calibrate.SYNTHETIC_CF)
        self.assertIn(hashlib.sha256(p2).hexdigest(), out)

    def test_unpackable_windows_values_fall_back_to_synthetic(self):
        blob = windows_calibration(factory=bytes([0x80]) * 56)  # packs to 0 bits, not 5
        with environment(windows_blob=blob) as env:
            rc, out = self.dry(env)
        self.assertEqual(rc, 0)
        self.assertIn("showing synthetic input instead", out)
        self.assertNotIn("Traceback", out)
        t = calibration.load_template()
        self.assertIn(hashlib.sha256(cm.build_program1(t, calibrate.SYNTHETIC_FACTORY)).hexdigest(), out)
        p2 = cm.build_program2(t, calibrate.SYNTHETIC_FACTORY, calibrate.SYNTHETIC_CF)
        self.assertIn(hashlib.sha256(p2).hexdigest(), out)

    def test_missing_files_are_not_an_error(self):
        with environment(windows=False) as env:
            env.module.unlink()
            with (
                mock.patch.object(calibrate, "DEFAULT_MODULE", env.dir / "none.bin"),
                mock.patch.object(capture, "DEFAULT_DLL", env.dir / "none.dll"),
            ):
                rc, out = self.dry(env, "--module", str(env.dir / "absent.bin"))
        self.assertEqual(rc, 0)
        self.assertIn("module: NOT usable", out)

    def test_fresh_process_never_imports_usb(self):
        """Importing calibrate and its dry run in a clean interpreter with `usb` blocked and open() trapped."""
        code = (
            "import sys, importlib.abc\n"
            "class B(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'usb' or name.startswith('usb.'):\n"
            "            print('USB-IMPORT', name); raise ImportError(name)\n"
            "sys.meta_path.insert(0, B())\n"
            f"sys.path.insert(0, {str(Path(calibrate.__file__).parent)!r})\n"
            "import calibrate\n"
            "def trap(self):\n"
            "    print('USB-OPEN'); raise SystemExit(99)\n"
            "calibrate.Transport.open = trap\n"
            "rc = calibrate.main(['--module', '/nonexistent/m.bin', '--windows-calibration', '/nonexistent/c.bin'])\n"
            "print('RC', rc, sorted(m for m in sys.modules if m.split('.')[0] == 'usb'))\n"
        )
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", code], cwd=td, capture_output=True, text=True, timeout=60, check=False
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("USB-IMPORT", proc.stdout)
        self.assertNotIn("USB-OPEN", proc.stdout)
        self.assertIn("RC 0 []", proc.stdout)


# -------------------------------------------------------------------- guard


class StubLog:
    def __init__(self):
        self.lines = []

    def write(self, kind, text):
        self.lines.append((kind, text))


class Guard(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(capture, "MODULE_SHA256", hashlib.sha256(MODULE).hexdigest())
        self.p.start()
        self.dev = CalDevice()
        self.t = calibrate.Transport(StubLog())
        self.t.dev = self.dev

    def tearDown(self):
        self.p.stop()

    def refused(self, data, state=None):
        state = state or calibrate.SessionState(rom_u1=0)
        with self.assertRaises(calibrate.UnsafeCommand, msg=data[:12].hex()):
            self.t.send(data, state)
        self.assertEqual(self.dev.calls, [], "a refused command reached the device")
        self.assertFalse(state.armed)

    def test_named_and_unknown_opcodes(self):
        for op in (0x1A, 0x4F, 0x50, 0x3E, 0x3F, 0x40, 0x41, 0x07, 0x08, 0x51, 0x99, 0xFF, 0x00):
            self.refused(bytes([op]))
            self.refused(bytes([op]) + bytes(8))
        for op in (0x1A, 0x4F, 0x50, 0x3F, 0x40, 0x41):
            self.assertIn(op, calibrate.NAMED_REFUSALS)
        self.refused(b"")

    def test_exact_forms(self):
        self.refused(bytes.fromhex("6f 00 0a 00 00 00 00 00 00"))  # another tag
        self.refused(bytes.fromhex("6f 00 0e 00 00 00 00 00"))  # short
        self.refused(bytes.fromhex("6f 00 0e 00 00 00 00 00 00 00"))  # long
        for op in (0x01, 0x19, 0x75, 0x04, 0x52):
            self.refused(bytes([op, 0x00]))

    def test_module_rules(self):
        self.refused(b"\x06" + bytes(capture.MODULE_LEN))  # wrong module
        self.refused(b"\x06" + MODULE[:-1])  # wrong length
        self.refused(b"\x06" + MODULE, calibrate.SessionState(rom_u1=0x0E))  # module loaded
        self.refused(b"\x06" + MODULE, calibrate.SessionState(rom_u1=None))  # RomInfo not parsed
        self.refused(b"\x06" + MODULE, calibrate.SessionState(rom_u1=0, module_sent=True))  # second 06

    def test_module_sent_once(self):
        state = calibrate.SessionState(rom_u1=0)
        self.t.send(b"\x06" + MODULE, state)
        self.assertTrue(state.module_sent)
        self.assertEqual(len(self.dev.writes()), 1)
        with self.assertRaises(calibrate.UnsafeCommand):
            self.t.send(b"\x06" + MODULE, state)
        self.assertEqual(len(self.dev.writes()), 1)

    def test_program_rules(self):
        t = calibration.load_template()
        p1, p2 = program1(), program2()
        self.refused(p1[:-1])
        self.refused(p1 + b"\x00")
        self.refused(calibration.build_cmd02(calibration.CalibrationInputs(FACTORY, CF, BF), t))  # identify, 18869 B
        self.refused(b"\x02")
        for prog in (p1, p2):
            for at in (1, 3, 9, 200, len(prog) - 5):  # header, req_lines, fixed chunk bytes, 0x43 tail
                bad = bytearray(prog)
                bad[at] ^= 0x01
                self.refused(bytes(bad))
        # same length, but chunk 0x17 (empty) renamed to 0x48
        _, chunks = calibration.split_chunks(p1)
        off = 5
        for typ, data in chunks:
            if typ == 0x17:
                break
            off += 4 + len(data)
        bad = bytearray(p1)
        struct.pack_into("<H", bad, off, 0x48)
        self.refused(bytes(bad))
        # a 02 that does not even parse
        self.refused(b"\x02" + bytes(1916))

    def test_allowed_forms_go_through(self):
        state = calibrate.SessionState(rom_u1=0)
        for data in (b"\x01", b"\x19", b"\x75", calibrate.REQ_FACTORY_BITS, b"\x04", b"\x52", program1(), program2()):
            self.t.send(data, state)
        self.assertTrue(state.armed)
        self.assertEqual(len(self.dev.writes()), 8)
        self.assertEqual({c[1] for c in self.dev.calls}, {0x01})


# --------------------------------------------------------------------- flow


class Flow(unittest.TestCase):
    def test_pass_exact_sequence(self):
        with environment() as env:
            r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, calibrate.EXIT_OK, r.out + r.err)
        self.assertEqual(r.dev.writes(), full_writes())
        self.assertEqual({c[1] for c in r.dev.calls if c[0] == "write"}, {0x01})
        self.assertEqual({c[1] for c in r.dev.calls if c[0] == "read"}, {0x81, 0x82})
        self.assertEqual(r.dev.names()[-2:], ["release", "dispose"])
        res = json.loads(r.files["result.json"])
        self.assertEqual(res["verdict"], "PASS")
        self.assertEqual(res["score"], 0)
        self.assertEqual(res["windows"], "compared")
        self.assertEqual(res["factory_matches"], 56)
        self.assertEqual(res["header_rows_ok"], {"cf": 56, "bf": 56})
        self.assertTrue(res["module_sent"])
        self.assertEqual(r.files["cf.bin"], CF)
        self.assertEqual(r.files["bf.bin"], BF)
        self.assertEqual(r.files["ep82-1.bin"], RAW1)
        self.assertEqual(r.files["ep82-2.bin"], RAW2)
        self.assertEqual(r.files["program1.bin"], program1())
        self.assertEqual(r.files["program2.bin"], program2())
        ch = cm.derive_chunks(FACTORY, CF, BF)
        self.assertEqual(r.files["chunk-34-keyline.bin"], ch.key_line)
        self.assertEqual(r.files["chunk-30-data.bin"], ch.line_update)
        self.assertEqual(r.files["chunk-43-factory.bin"], ch.factory_entries)
        self.assertEqual(r.files["chunk-48-baseline.bin"], ch.baseline)
        self.assertIn("calibrate.log", r.files)
        self.assertIn("PASS", r.out)

    def test_module_skipped_when_loaded(self):
        with environment() as env:
            r = run_cal(env, CalDevice(byte17=0x0E))
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(r.dev.writes(), full_writes(module=False))
        self.assertNotIn(0x06, [w[0] for w in r.dev.writes()])
        self.assertEqual([w for w in r.dev.writes() if w == b"\x01"], [b"\x01"])  # no second 01
        self.assertNotIn("rom_byte17_after_module", json.loads(r.files["result.json"]))

    def test_rominfo_rechecked_after_module(self):
        with environment() as env:
            r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, 0, r.out)
        w = r.dev.writes()
        self.assertEqual(w[w.index(b"\x06" + MODULE) + 1], b"\x01")
        self.assertEqual(json.loads(r.files["result.json"])["rom_byte17_after_module"], 0x0E)
        self.assertIn(b"after the module: byte 17 0x0e (as expected), serial <redacted>", r.files["calibrate.log"])
        self.assertEqual(sorted(k for k in r.files if k.endswith("-01.bin")), ["reply-01-01.bin", "reply-06-01.bin"])

    def test_rominfo_after_module_is_not_a_gate(self):
        for second in (rom_info(0), b"\x00\x00"):  # byte 17 still 0; reply too short to decode
            with environment() as env:
                r = run_cal(env, CalDevice(replies={0x01: [rom_info(0), second]}))
            self.assertEqual(r.rc, 0, r.out)
            self.assertEqual(r.dev.writes(), full_writes())
            log = r.files["calibrate.log"]
            if len(second) == 38:
                self.assertIn(b"byte 17 0x00 (expected 0x0e; information only)", log)
            else:
                self.assertIn(b"byte 17 not decoded (information only)", log)

    def test_no_windows_data_is_not_a_gate(self):
        with environment(windows=False) as env:
            r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, 0, r.out)
        self.assertIn("not available", r.out)
        self.assertEqual(json.loads(r.files["result.json"])["windows"], "not available")

    def test_other_units_calibration_is_not_compared(self):
        with environment(windows_blob=windows_calibration(serial=bytes.fromhex("010203040506"))) as env:
            r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, 0, r.out)
        self.assertIn("another unit", r.out)
        self.assertNotIn("cf_vs_windows", json.loads(r.files["result.json"]))

    def test_windows_mismatch_fails(self):
        cases = {
            "factory": windows_calibration(factory=bytes([FACTORY[0] ^ 1]) + FACTORY[1:]),
            "cf": windows_calibration(frame=CF[152:] + CF[:152]),
            "bf": windows_calibration(baseline=bytes([140]) * 8512),
        }
        for name, blob in cases.items():
            with environment(windows_blob=blob) as env:
                r = run_cal(env, CalDevice())
            self.assertEqual(r.rc, calibrate.EXIT_FAIL, name)
            self.assertEqual(r.dev.writes(), full_writes(), name)
            res = json.loads(r.files["result.json"])
            self.assertEqual(res["verdict"], "FAIL", name)
            self.assertFalse(res["gates"][name], name)
            self.assertIn("FAIL", r.out)

    def test_score_over_200_stops_before_program_2(self):
        with environment() as env:
            dev = CalDevice(replies={0x6F: factory_reply({3: b"\x07\x00\x00\x00" + FACTORY, 12: sub12(60)})})
            r = run_cal(env, dev)
        self.assertEqual(r.rc, calibrate.EXIT_FAIL)
        self.assertEqual(r.dev.writes(), full_writes()[: through(1, 2)])  # ... program #1, 04, 52; no #2
        self.assertEqual(r.dev.names()[-2:], ["release", "dispose"])
        res = json.loads(r.files["result.json"])
        self.assertEqual(res["verdict"], "FAIL")
        self.assertGreater(res["score"], 200)
        self.assertNotIn("program2.bin", r.files)
        self.assertIn("FAIL", r.out)

    def test_bf_sanity_gates(self):
        hot = [[200] * 144 for _ in range(56)]
        with environment(windows=False) as env:
            r = run_cal(env, CalDevice(frames=[RAW1, raw_capture(hot)]))
        self.assertEqual(r.rc, calibrate.EXIT_FAIL)
        self.assertFalse(json.loads(r.files["result.json"])["bf_sanity"]["ok"])


class StopPaths(unittest.TestCase):
    def stopped(self, dev, rc, writes, armed):
        with environment() as env:
            r = run_cal(env, dev)
        self.assertEqual(r.rc, rc, r.out)
        expect = full_writes()[:writes]
        if armed:
            expect += [b"\x04", b"\x52"]
        self.assertEqual(r.dev.writes(), expect)
        self.assertEqual(r.dev.names()[-2:], ["release", "dispose"])
        self.assertEqual(json.loads(r.files["result.json"])["verdict"], "STOPPED")
        self.assertIn("Log and data:", r.out)
        return r

    def test_wrong_rominfo_size(self):
        self.stopped(CalDevice(replies={0x01: rom_info()[:30]}), calibrate.EXIT_BAD_REPLY, 1, False)

    def test_wrong_rom_version(self):
        rom = bytearray(rom_info())
        rom[11] = 0x15
        self.stopped(CalDevice(replies={0x01: bytes(rom)}), calibrate.EXIT_BAD_REPLY, 1, False)

    def test_wrong_sensor_type(self):
        self.stopped(CalDevice(replies={0x75: bytes(10)}), calibrate.EXIT_BAD_REPLY, 3, False)

    def test_missing_subtag_12(self):
        dev = CalDevice(replies={0x6F: factory_reply({3: b"\x07\x00\x00\x00" + FACTORY})})
        self.stopped(dev, calibrate.EXIT_BAD_REPLY, 4, False)  # stops before 06

    def test_unpackable_factory_values(self):
        dev = CalDevice(replies={0x6F: factory_reply({3: bytes(4) + bytes([0x80]) * 56, 12: sub12()})})
        self.stopped(dev, calibrate.EXIT_BAD_REPLY, 4, False)

    def test_bad_module_status(self):
        self.stopped(CalDevice(replies={0x06: b"\xaa\x04"}), calibrate.EXIT_BAD_REPLY, 5, False)

    def test_bad_status_on_program_1(self):
        self.stopped(CalDevice(replies={0x02: b"\x01\x04"}), calibrate.EXIT_BAD_REPLY, through(1), True)

    def test_reply_timeout_on_program_1(self):
        self.stopped(CalDevice(replies={0x02: USBTimeoutError("t")}), calibrate.EXIT_USB_ERROR, through(1), True)

    def test_ep82_timeout(self):
        r = self.stopped(CalDevice(frames=[b""]), calibrate.EXIT_USB_ERROR, through(1), True)
        self.assertEqual(r.files["ep82-1.bin"], b"")

    def test_ep82_short(self):
        r = self.stopped(CalDevice(frames=[RAW1[:-152]]), calibrate.EXIT_USB_ERROR, through(1), True)
        self.assertEqual(r.files["ep82-1.bin"], RAW1[:-152])

    def test_ep82_too_long(self):
        r = self.stopped(CalDevice(frames=[RAW1 + bytes(152)]), calibrate.EXIT_BAD_REPLY, through(1), True)
        self.assertGreater(len(r.files["ep82-1.bin"]), cm.RAW_LEN)

    def test_ep82_timeout_on_capture_2(self):
        self.stopped(CalDevice(frames=[RAW1, b""]), calibrate.EXIT_USB_ERROR, through(2), True)

    def test_bad_stop_status(self):
        self.stopped(CalDevice(replies={0x04: b"\x91\x04"}), calibrate.EXIT_BAD_REPLY, through(1, 1), True)

    def test_ctrl_c_during_capture(self):
        r = self.stopped(CalDevice(frame_error=KeyboardInterrupt()), calibrate.EXIT_INTERRUPTED, through(1), True)
        self.assertIn("Interrupted", r.out)

    def test_usb_error_stops_writing(self):
        with environment() as env:
            r = run_cal(env, CalDevice(frame_error=usb_error(19)))
        self.assertEqual(r.rc, calibrate.EXIT_USB_ERROR)
        self.assertEqual(r.dev.writes(), full_writes()[: through(1)])  # device gone: no 04/52 into the void
        self.assertIn(calibrate.MSG_REMOVED, r.out)
        self.assertIn(calibrate.MSG_DONE, r.out)

    def test_other_usb_errors_still_send_04_52(self):
        cases = {
            "OVERFLOW on the program #1 reply": CalDevice(replies={0x02: usb_error(75)}),
            "EPIPE on EP 0x82": CalDevice(frame_error=usb_error(32)),
        }
        for name, dev in cases.items():
            with self.subTest(name):
                r = self.stopped(dev, calibrate.EXIT_USB_ERROR, through(1), True)
                self.assertIn(calibrate.MSG_DEVICE, r.out)
                self.assertNotIn(calibrate.MSG_REMOVED, r.out)

    def test_enodev_during_cleanup_stops_it(self):
        dev = CalDevice(frames=[b""], replies={0x04: usb_error(19)})
        with environment() as env:
            r = run_cal(env, dev)
        self.assertEqual(r.rc, calibrate.EXIT_USB_ERROR)
        self.assertEqual(r.dev.writes(), full_writes()[: through(1)] + [b"\x04"])  # no 52 after ENODEV

    def test_unexpected_error_has_its_own_exit_code(self):
        with environment() as env, mock.patch.object(cm, "average_frame2", side_effect=RuntimeError("boom")):
            r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, calibrate.EXIT_UNEXPECTED)
        self.assertNotIn(
            calibrate.EXIT_UNEXPECTED, (calibrate.EXIT_USB_ERROR, calibrate.EXIT_OK, 2, 3, 4, 5, 6, 7, 130)
        )
        self.assertEqual(r.dev.writes(), full_writes()[: through(1, 2)])  # 04/52 after program #1
        self.assertIn(calibrate.MSG_UNEXPECTED, r.out)
        self.assertNotIn("Traceback", r.out + r.err)
        self.assertNotIn("boom", r.out + r.err)
        self.assertIn(b"Traceback", r.files["calibrate.log"])
        self.assertEqual(json.loads(r.files["result.json"])["exit_code"], calibrate.EXIT_UNEXPECTED)
        self.assertIn("8 unexpected error", calibrate.__doc__)

    def test_frame_deadline_is_5_s_overall(self):
        clock = types.SimpleNamespace(now=1000.0)
        timeouts = []

        class Slow(CalDevice):
            def read(self, ep, size, timeout=None):
                if ep == 0x82:
                    timeouts.append(timeout)
                    clock.now += 1.5  # every chunk arrives just in time, but too slowly overall
                return super().read(ep, size, timeout)

        fake_time = types.SimpleNamespace(monotonic=lambda: clock.now)
        with mock.patch.object(calibrate, "time", fake_time):
            r = self.stopped(Slow(), calibrate.EXIT_USB_ERROR, through(1), True)
        self.assertEqual(timeouts, [5000, 3500, 2000, 500])
        self.assertEqual(r.files["ep82-1.bin"], RAW1[: 4 * calibrate.FRAME_CHUNK])
        self.assertIn(b"within 5 s", r.files["calibrate.log"])
        reads = {c[2] for c in r.dev.calls if c[0] == "read" and c[1] == 0x82}
        self.assertEqual(reads, {calibrate.FRAME_CHUNK})
        self.assertEqual(calibrate.FRAME_CHUNK % 64, 0)
        self.assertLessEqual(calibrate.FRAME_CHUNK, 4096)

    def test_guard_refusal_mid_flow(self):
        with environment() as env, mock.patch.dict(cm.SKELETON_SHA256, {cm.PROGRAM1_LEN: "0" * 64}):
            r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, calibrate.EXIT_REFUSED)
        self.assertEqual(r.dev.writes(), full_writes()[: through(1) - 1])  # program #1 never written: no 04/52
        self.assertIn(calibrate.MSG_REFUSED, r.out)


class OpenFailures(unittest.TestCase):
    def test_kernel_driver_attached_is_busy(self):
        with environment() as env:
            r = run_cal(env, CalDevice(driver_active=True))
        self.assertEqual(r.rc, calibrate.EXIT_DRIVER_BUSY)
        self.assertEqual(r.dev.writes(), [])
        self.assertIn("sudo systemctl stop fprintd", r.out)
        self.assertIn("in use by fprintd", r.out)

    def claim_error(self, errno):
        def setup():
            def claim(d, i):
                e = USBError("claim")
                e.errno = errno
                raise e

            sys.modules["usb.util"].claim_interface = claim

        return setup

    def test_claim_ebusy(self):
        with environment() as env:
            r = run_cal(env, CalDevice(), setup=self.claim_error(16))
        self.assertEqual(r.rc, calibrate.EXIT_DRIVER_BUSY)
        self.assertEqual(r.dev.writes(), [])
        self.assertIn("sudo systemctl stop fprintd", r.out)

    def test_claim_permission(self):
        with environment() as env:
            r = run_cal(env, CalDevice(), setup=self.claim_error(13))
        self.assertEqual(r.rc, calibrate.EXIT_USB_ERROR)
        self.assertIn("getfacl /dev/bus/usb/001/003", r.out)
        self.assertIn("No permission", r.out)

    def assert_closed_without_sending(self, r, reason):
        lines = r.out.splitlines()
        self.assertEqual(lines[0], calibrate.MSG_FINGERS_OFF)
        self.assertEqual(lines[-2], f"Stopped: {reason}. Nothing was sent to the reader.")
        self.assertTrue(lines[-1].startswith("Log and data: "))
        self.assertNotIn(calibrate.MSG_DONE, lines)
        self.assertNotIn("Traceback", r.out + r.err)

    def test_not_found(self):
        with environment() as env:
            r = run_cal(env, None)
        self.assertEqual(r.rc, calibrate.EXIT_NOT_FOUND)
        self.assertIn("was not found", r.out)
        self.assert_closed_without_sending(r, "reader not found")

    def test_libusb_missing(self):
        def setup():
            sys.modules["usb.backend.libusb1"].get_backend = lambda: None

        with environment() as env:
            r = run_cal(env, CalDevice(), setup=setup)
        self.assertEqual(r.rc, calibrate.EXIT_USB_ERROR)
        self.assertEqual(r.dev.calls, [])
        self.assertIn(calibrate.MSG_LIBUSB, r.out)
        self.assert_closed_without_sending(r, "libusb-1.0 is missing")

    def test_pyusb_missing(self):
        out = io.StringIO()
        blocked = {"usb": None, "usb.backend": None, "usb.backend.libusb1": None}
        with (
            environment() as env,
            mock.patch.dict(sys.modules, blocked),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rc = calibrate.run_device(calibrate.build_parser().parse_args(env.argv("--run")))
        self.assertEqual(rc, calibrate.EXIT_USB_ERROR)
        r = types.SimpleNamespace(out=out.getvalue(), err="")
        self.assertIn(calibrate.MSG_PYUSB, r.out)
        self.assert_closed_without_sending(r, "pyusb is missing")

    def test_busy_closes_without_sending(self):
        with environment() as env:
            r = run_cal(env, CalDevice(driver_active=True))
        self.assert_closed_without_sending(r, "the reader is busy")

    def test_missing_module_refuses_before_usb(self):
        with environment() as env:
            env.module.unlink()
            r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, calibrate.EXIT_BAD_INPUT)
        self.assertEqual(r.dev.calls, [])
        self.assertIn(calibrate.MSG_INPUT, r.out)

    def test_bad_windows_file_refuses_before_usb(self):
        with environment(windows_blob=b"not a calibration") as env:
            r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, calibrate.EXIT_BAD_INPUT)
        self.assertEqual(r.dev.calls, [])

    def test_existing_run_dir_refused(self):
        with environment() as env:
            (env.out_root / "20260101-000000").mkdir(parents=True)
            with mock.patch.object(calibrate, "_stamp", lambda: "20260101-000000"):
                r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, calibrate.EXIT_BAD_INPUT)
        self.assertEqual(r.dev.calls, [])
        self.assertEqual(r.files, {})
        self.assertIn("with this time stamp already exists", r.err)

    def test_parent_created_concurrently_is_not_a_clash(self):
        real_mkdir = os.mkdir
        raced = []

        def racing_mkdir(path, mode=0o777, **kw):
            if Path(path).name == "calib" and not raced:  # another run creates local/calib first
                raced.append(path)
                real_mkdir(path, 0o700)
                raise FileExistsError(17, "File exists", str(path))
            return real_mkdir(path, mode, **kw)

        with environment() as env:
            with mock.patch.object(calibrate.os, "mkdir", racing_mkdir):
                run = calibrate.RunDir(env.out_root, "20260101-000000")
            self.assertEqual(raced, [env.out_root])
            self.assertTrue(run.path.is_dir())
            self.assertEqual(stat.S_IMODE(run.path.stat().st_mode), 0o700)

    def test_parent_that_is_a_file_is_reported_as_such(self):
        with environment() as env:
            env.out_root.parent.mkdir(parents=True)
            env.out_root.write_bytes(b"")  # local/calib is a file
            r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, calibrate.EXIT_BAD_INPUT)
        self.assertEqual(r.dev.calls, [])
        self.assertIn("Cannot create the output folder", r.err)
        self.assertNotIn("already exists, so", r.err)


class Signals(unittest.TestCase):
    """SIGTERM (as Ctrl-C) outside the experiment proper: short line, cleanup, exit 130, no traceback."""

    def check_quiet(self, r):
        self.assertEqual(r.rc, calibrate.EXIT_INTERRUPTED, r.out + r.err)
        self.assertNotIn("Traceback", r.out + r.err)
        self.assertTrue(r.out.splitlines()[-1].startswith("Log and data: "))

    def test_sigterm_while_loading_inputs(self):
        with environment() as env, mock.patch.object(calibrate, "load_inputs", lambda args: sigterm_self()):
            r = run_cal(env, CalDevice(), via_main=True)
        self.check_quiet(r)
        self.assertEqual(r.dev.calls, [])
        self.assertIn("Stopped: interrupted. Nothing was sent to the reader.", r.out.splitlines())
        self.assertIn(b"interrupted while loading the inputs", r.files["calibrate.log"])

    def test_sigterm_while_closing(self):
        def setup():
            def release(d, i):
                d.calls.append(("release", i))
                sigterm_self()

            sys.modules["usb.util"].release_interface = release

        with environment() as env:
            r = run_cal(env, CalDevice(), setup=setup, via_main=True)
        self.check_quiet(r)
        self.assertEqual(r.dev.writes(), full_writes())
        self.assertEqual(r.dev.names()[-2:], ["release", "dispose"])  # dispose still ran
        lines = r.out.splitlines()
        self.assertIn(calibrate.MSG_INTERRUPTED_CLOSE, lines)
        self.assertEqual(lines[-2], calibrate.MSG_DONE)
        res = json.loads(r.files["result.json"])
        self.assertEqual((res["exit_code"], res["interrupted_while_closing"]), (calibrate.EXIT_INTERRUPTED, True))

    def test_sigterm_during_capture_cleans_up(self):
        with environment() as env:
            r = run_cal(env, CalDevice(frame_error=KeyboardInterrupt()), via_main=True)
        self.check_quiet(r)
        self.assertEqual(r.dev.writes(), full_writes()[: through(1)] + [b"\x04", b"\x52"])


# ------------------------------------------------------- files and privacy


def serial_forms():
    forms = [SERIAL, SERIAL[::-1]]
    for s in (SERIAL, SERIAL[::-1]):
        for text in (s.hex(), s.hex().upper(), s.hex(" "), s.hex(" ").upper()):
            forms.append(text.encode())
    return forms


class FilesAndPrivacy(unittest.TestCase):
    def test_modes_under_permissive_umasks(self):
        for umask in (0o022, 0o000):
            with self.subTest(umask=oct(umask)), environment() as env:
                r = run_cal(env, CalDevice(), umask=umask)
                self.assertEqual(r.rc, 0, r.out)
                for d in (env.out_root.parent, env.out_root, r.dir):
                    self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700, d)
                for f in r.dir.iterdir():
                    self.assertTrue(f.is_file())
                    self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f.name)
                # nothing outside the run directory
                outside = [
                    p
                    for p in env.dir.rglob("*")
                    if p.is_file() and r.dir not in p.parents and p not in (env.module, env.cal)
                ]
                self.assertEqual(outside, [])
                self.assertTrue(str(r.dir).startswith(str(env.out_root)))

    def test_serial_never_leaves_memory(self):
        runs = []
        with environment() as env:
            runs.append(run_cal(env, CalDevice()))
        with environment() as env:  # stop path right after 01
            runs.append(run_cal(env, CalDevice(replies={0x19: b"\x01\x04"})))
        with environment(windows_blob=windows_calibration(serial=bytes.fromhex("010203040506"))) as env:
            runs.append(run_cal(env, CalDevice()))
        for r in runs:
            blobs = {"stdout": r.out.encode(), "stderr": r.err.encode(), **r.files}
            self.assertIn("calibrate.log", r.files)
            for name, blob in blobs.items():
                for form in serial_forms():
                    self.assertNotIn(form, blob, f"serial form {form!r} found in {name}")
            self.assertIn(b"<redacted>", r.files["calibrate.log"])
            roms = [v for k, v in sorted(r.files.items()) if k.endswith("-01.bin")]
            self.assertTrue(roms)
            for rom in roms:  # the first 01 and, after a 06, the second one
                self.assertEqual(rom[0x12:0x18], bytes(6))
                self.assertEqual(rom[:0x11], rom_info()[:0x11])

    def test_terminal_shows_only_short_lines(self):
        with environment() as env:
            r = run_cal(env, CalDevice())
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.err, "")
        lines = r.out.splitlines()
        self.assertEqual(lines[0], calibrate.MSG_FINGERS_OFF)
        for want in (
            "Reading the reader's factory values…",
            "Calibration capture 1 of 2…",
            "Calibration capture 2 of 2…",
        ):
            self.assertIn(want, lines)
        self.assertTrue(lines[-1].startswith("Log and data: "))
        self.assertIn(calibrate.MSG_DONE, lines)
        self.assertTrue(any(line.startswith("PASS: check score 0") for line in lines))
        body = r.out.replace(str(r.dir), "")
        self.assertIsNone(re.search(r"[0-9a-fA-F]{16,}", body), "hex run in the terminal output")
        self.assertIsNone(re.search(r"\b[0-9a-f]{2}( [0-9a-f]{2}){3,}\b", body), "hex dump in the terminal output")
        self.assertNotIn("Traceback", r.out)
        self.assertNotIn("sha256", r.out)
        self.assertNotIn("EP 0x", r.out)
        self.assertLessEqual(len(lines), 14)


if __name__ == "__main__":
    unittest.main()
