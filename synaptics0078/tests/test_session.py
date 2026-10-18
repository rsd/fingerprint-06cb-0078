# SPDX-License-Identifier: MIT
"""session.py through an injected fake transport: the sequence, the guard, the hooks, no pyusb.

Everything is synthetic: the template comes from the fake DLL of fixtures.py and the
guard runs with synthetic pins (module and skeletons). proto/test_calibrate_flow.py
covers the same code through proto/calibrate.py and a fake pyusb.
"""

from __future__ import annotations

import subprocess
import sys
import types
from functools import partial

import pytest

from synaptics0078 import calib_model as cm
from synaptics0078 import session

from . import fixtures as fx

SP = fx.synthetic_pins()
TEMPLATE = fx.expected_template()
GUARD = partial(session.check_command, module_sha256=SP.module_sha256, skeletons=SP.skeletons)


class FakeReader:
    """A fake device behind session.UsbTransport: replies by opcode, one EP 0x82 buffer per 02."""

    def __init__(self, byte17=0, check=None, frames=None):
        self.rom = fx.rom_info(byte17)
        self.replies = {0x19: b"\x00\x00", 0x75: fx.SENSOR_ID, 0x6F: fx.factory_reply(check), 0x06: b"\x00\x00"}
        self.replies.update({0x02: b"\x00\x00" + bytes(30), 0x04: b"\x12\x04", 0x52: b"\x00\x00"})
        self.frames = list(frames if frames is not None else [fx.raw_capture(fx.CF_PIX), fx.raw_capture(fx.BF_RAW_PIX)])
        self.written, self.last, self.current = [], None, b""

    def write(self, ep, data, timeout=None):
        self.written.append(bytes(data))
        self.last = data[0]
        if self.last == 0x02:
            self.current = self.frames.pop(0)
        elif self.last == 0x06:
            r = bytearray(self.rom)
            r[17] = 0x0E
            self.rom = bytes(r)
        return len(data)

    def read(self, ep, size, timeout=None):
        if ep == 0x82:
            out, self.current = self.current[:size], self.current[size:]
            return out
        return self.rom if self.last == 0x01 else self.replies[self.last]


class Log:
    def __init__(self):
        self.lines = []

    def write(self, kind, text):
        self.lines.append((kind, text))


def transport(dev, log=None, guard=GUARD):
    t = session.UsbTransport(log, guard=guard)
    t.dev = dev
    t._core = types.SimpleNamespace(USBTimeoutError=TimeoutError, USBError=OSError)
    return t


def sequence(dev, **kw):
    log = kw.pop("log", Log())
    saved = {}
    seq = session.CalibrationSequence(
        transport(dev, log, kw.pop("tguard", GUARD)),
        log,
        template=TEMPLATE,
        module=fx.MODULE,
        guard=kw.pop("guard", GUARD),
        save=saved.__setitem__,
        **kw,
    )
    return seq, log, saved


def test_full_sequence_with_module_upload():
    dev = FakeReader(byte17=0)
    seq, log, saved = sequence(dev)
    res = seq.calibrate()
    p1, p2 = cm.build_program1(TEMPLATE, fx.FACTORY), cm.build_program2(TEMPLATE, fx.FACTORY, fx.CF)
    assert dev.written == [
        b"\x01",
        b"\x19",
        b"\x75",
        session.REQ_FACTORY_BITS,
        b"\x06" + fx.MODULE,
        b"\x01",
        p1,
        b"\x04",
        b"\x52",
        p2,
        b"\x04",
        b"\x52",
    ]
    assert (res.factory, res.cf, res.bf, res.score) == (fx.FACTORY, fx.CF, fx.BF, 0)
    assert res.score_ok and res.module_uploaded and res.rom == (6, 0x14)
    assert (res.rom_byte17, res.rom_byte17_after_module) == (0, 0x0E)
    assert res.serial == fx.SERIAL and fx.SERIAL.hex() not in repr(res)
    assert res.chunks() == fx.unit_chunks()
    assert not seq.state.armed and seq.state.module_sent
    assert saved["cf.bin"] == fx.CF and saved["bf.bin"] == fx.BF and saved["program1.bin"] == p1
    assert saved["reply-01-01.bin"][0x12:0x18] == bytes(6), "serial zeroed before it is saved"
    text = "\n".join(t for _, t in log.lines)
    assert fx.SERIAL.hex() not in text and "<redacted>" in text
    assert ("STEP", "Calibration capture 2 of 2…") in log.lines, "no say hook: progress goes to the log"
    assert seq.info["score"] == 0 and seq.info["module_sent"] is True


def test_rom_serial_is_read_only_and_none_before_rominfo():
    seq, _, _ = sequence(FakeReader())
    assert seq.rom_serial is None
    seq.read_identity()
    assert seq.rom_serial == fx.SERIAL
    with pytest.raises(AttributeError):
        seq.rom_serial = b"x"


def test_module_already_loaded_is_not_sent():
    dev = FakeReader(byte17=0x0E)
    seq, _, _ = sequence(dev, say=lambda text: None)
    res = seq.calibrate()
    assert not res.module_uploaded and res.rom_byte17_after_module is None
    assert all(w[0] != 0x06 for w in dev.written)


def test_finger_on_the_reader_stops_after_capture_1():
    dev = FakeReader(check=fx.sub12(shift=40))
    said = []
    seq, _, _ = sequence(dev, say=said.append)
    res = seq.calibrate()
    assert res.score > cm.CHECK_MAX and not res.score_ok and res.bf is None
    assert [w[0] for w in dev.written].count(0x02) == 1
    assert said[-1] == "Calibration capture 1 of 2…"
    with pytest.raises(session.calibration.CalibrationError):
        res.chunks()


def test_guard_with_real_pins_refuses_the_synthetic_module():
    dev = FakeReader(byte17=0)
    seq, _, _ = sequence(dev, guard=session.check_command, tguard=session.check_command)
    with pytest.raises(session.CalibrationUnsafeCommand, match="pinned sha256"):
        seq.calibrate()
    assert [w[0] for w in dev.written] == [0x01, 0x19, 0x75, 0x6F]


def test_sequence_guard_holds_whatever_the_transport():
    """A transport whose send() skips the guard still cannot get a refused command on the wire."""
    dev = FakeReader(byte17=0)
    open_transport = transport(dev, guard=lambda data, state: None)
    seq = session.CalibrationSequence(
        open_transport, template=TEMPLATE, module=fx.MODULE, guard=partial(GUARD, skeletons={})
    )
    with pytest.raises(session.CalibrationUnsafeCommand, match="not a calibration program"):
        seq.calibrate()
    assert all(w[0] != 0x02 for w in dev.written)


def test_cleanup_sends_stop_commands_when_armed():
    dev = FakeReader()
    seq, _, _ = sequence(dev)
    seq.state.armed = True
    seq.cleanup()
    assert dev.written == [b"\x04", b"\x52"] and not seq.state.armed
    seq.cleanup()
    assert dev.written == [b"\x04", b"\x52"]


def test_check_command_is_a_capture_unsafe_command():
    with pytest.raises(session.UnsafeCommand):
        session.check_command(b"\x1a", session.SessionState())
    assert issubclass(session.CalibrationUnsafeCommand, session.UnsafeCommand)


def test_check_command_defaults_are_the_pins():
    assert session.check_command.__kwdefaults__["module_sha256"] == session.pins.MODULE_SHA256
    session.check_command(b"\x04", session.SessionState())
    with pytest.raises(session.CalibrationUnsafeCommand, match="not a calibration program"):
        session.check_command(b"\x02" + bytes(100), session.SessionState())


def test_import_needs_no_pyusb():
    code = (
        "import sys, importlib.abc\n"
        "class B(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'usb' or name.startswith('usb.'):\n"
        "            raise ImportError(name)\n"
        "sys.meta_path.insert(0, B())\n"
        f"sys.path.insert(0, {str(fx.REPO)!r})\n"
        "from synaptics0078 import session, vendor, template\n"
        "t = session.UsbTransport()\n"
        "try:\n"
        "    t.open()\n"
        "except session.StepFailed as e:\n"
        "    print('RC', e.rc, e.reason)\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'usb'))\n"
    )
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [f"RC {session.EXIT_USB_ERROR} pyusb is missing", "[]"]
