"""Offline test of probe.run() against a fake pyusb: no device, no libusb.

Checks the safety properties: only 01/19/75 are written, only to EP 0x01,
replies are read only from EP 0x81, and cleanup runs on every exit path.
"""

import contextlib
import io
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import calibration
import probe
from test_parsers import V63_QUERY_19, V63_ROM_INFO, V63_SENSOR_ID

REPLIES = {0x01: V63_ROM_INFO, 0x19: V63_QUERY_19, 0x75: V63_SENSOR_ID}

CAL_BLOB = Path(__file__).resolve().parent.parent / "extracted" / "CalibrationData_decrypted.bin"


class USBError(Exception):
    errno = None


class USBTimeoutError(USBError):
    pass


class FakeDevice:
    bus, address, bcdDevice = 1, 3, 0x0154

    def __init__(self, driver_active=False, fail_on=None):
        self.calls = []  # every call that could touch the device
        self.driver_active = driver_active
        self.fail_on = fail_on  # (index_of_read, exception)
        self.last = None
        self.reads = 0

    def get_active_configuration(self):
        return types.SimpleNamespace(bConfigurationValue=1)

    def set_configuration(self, *a):
        self.calls.append(("set_configuration", a))

    def is_kernel_driver_active(self, i):
        return self.driver_active

    def detach_kernel_driver(self, i):
        self.calls.append(("detach", i))
        self.driver_active = False

    def attach_kernel_driver(self, i):
        self.calls.append(("attach", i))

    def write(self, ep, data, timeout=None):
        self.calls.append(("write", ep, bytes(data)))
        self.last = bytes(data)[0]
        return len(data)

    def read(self, ep, size, timeout=None):
        self.calls.append(("read", ep, size))
        self.reads += 1
        if self.fail_on and self.fail_on[0] == self.reads:
            raise self.fail_on[1]
        return list(REPLIES[self.last])


def fake_usb(dev):
    usb = types.ModuleType("usb")
    backend = types.ModuleType("usb.backend")
    libusb1 = types.ModuleType("usb.backend.libusb1")
    core = types.ModuleType("usb.core")
    util = types.ModuleType("usb.util")
    libusb1.get_backend = lambda: object()
    core.find = lambda **kw: [dev] if dev is not None else []
    core.USBError, core.USBTimeoutError = USBError, USBTimeoutError
    util.claim_interface = lambda d, i: d.calls.append(("claim", i))
    util.release_interface = lambda d, i: d.calls.append(("release", i))
    util.dispose_resources = lambda d: d.calls.append(("dispose",))
    usb.backend, usb.core, usb.util = backend, core, util
    backend.libusb1 = libusb1
    mods = {
        "usb": usb,
        "usb.backend": backend,
        "usb.backend.libusb1": libusb1,
        "usb.core": core,
        "usb.util": util,
    }
    return mock.patch.dict(sys.modules, mods)


def run_probe(dev, *extra):
    with tempfile.TemporaryDirectory() as td, fake_usb(dev):
        args = probe.argparse.Namespace(
            only=None,
            dry_run=False,
            detach_kernel_driver=False,
            timeout_ms=100,
            log_dir=td,
            calibration=str(CAL_BLOB),
        )
        for k, v in extra:
            setattr(args, k, v)
        rc = probe.run(args)
        logs = list(Path(td).glob("probe-*.log"))
        text = logs[0].read_text() if logs else ""
    return rc, text


class ProbeFlow(unittest.TestCase):
    def test_normal_run(self):
        dev = FakeDevice()
        rc, log = run_probe(dev)
        self.assertEqual(rc, 0)
        writes = [c for c in dev.calls if c[0] == "write"]
        self.assertEqual(writes, [("write", 0x01, b"\x01"), ("write", 0x01, b"\x19"), ("write", 0x01, b"\x75")])
        self.assertEqual({c[1] for c in dev.calls if c[0] == "read"}, {0x81})
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])
        self.assertNotIn("set_configuration", [c[0] for c in dev.calls])
        # Log holds raw exchanges losslessly.
        self.assertIn(V63_SENSOR_ID.hex(), log)
        self.assertIn(V63_ROM_INFO.hex(), log)
        self.assertIn("TX", log)
        self.assertIn("sensor type", log)

    def test_read_timeout_stops_and_cleans_up(self):
        dev = FakeDevice(fail_on=(2, USBTimeoutError("t")))
        rc, _ = run_probe(dev)
        self.assertEqual(rc, probe.EXIT_USB_ERROR)
        self.assertEqual(len([c for c in dev.calls if c[0] == "write"]), 2)  # 0x75 never sent
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])

    def test_ctrl_c_cleans_up(self):
        dev = FakeDevice(fail_on=(1, KeyboardInterrupt()))
        rc, _ = run_probe(dev)
        self.assertEqual(rc, probe.EXIT_INTERRUPTED)
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])

    def test_usb_error_cleans_up(self):
        dev = FakeDevice(fail_on=(1, USBError("boom")))
        rc, _ = run_probe(dev)
        self.assertEqual(rc, probe.EXIT_USB_ERROR)
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])

    def test_kernel_driver_refused_without_flag(self):
        dev = FakeDevice(driver_active=True)
        rc, _ = run_probe(dev)
        self.assertEqual(rc, probe.EXIT_DRIVER_BUSY)
        self.assertEqual([c[0] for c in dev.calls], ["dispose"])  # never claimed, never wrote

    def test_kernel_driver_detached_with_flag_and_reattached(self):
        dev = FakeDevice(driver_active=True)
        rc, _ = run_probe(dev, ("detach_kernel_driver", True))
        self.assertEqual(rc, 0)
        names = [c[0] for c in dev.calls]
        self.assertLess(names.index("detach"), names.index("claim"))
        self.assertEqual(names[-3:], ["release", "attach", "dispose"])

    def test_device_not_found(self):
        rc, _ = run_probe(None)
        self.assertEqual(rc, probe.EXIT_NOT_FOUND)

    def test_only_subset(self):
        dev = FakeDevice()
        rc, _ = run_probe(dev, ("only", "75"))
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in dev.calls if c[0] == "write"], [("write", 0x01, b"\x75")])

    def test_6f_is_off_by_default(self):
        dev = FakeDevice()
        rc, _ = run_probe(dev)
        self.assertEqual(rc, 0)
        self.assertNotIn(0x6F, [c[2][0] for c in dev.calls if c[0] == "write"])


def factory_reply(values):
    value = bytes.fromhex("07000000") + values
    hdr = struct.pack("<HLL", 0, 0xD88, 2)
    e3 = struct.pack("<LHHHH", 8, len(value), 0x0E, 3, 0x8000) + value
    e2 = struct.pack("<LHHHH", 0xA4, 8, 0x0E, 2, 0) + bytes.fromhex("00000000 0d007100")
    return hdr + e3 + e2


class FactoryBitsFlow(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.cal = Path(self._td.name) / "cal.bin"
        blob = bytearray(calibration.MIN_CAL_LEN)
        struct.pack_into("<III", blob, 4, 0xE4, 2, 0x2449)
        struct.pack_into("<I", blob, calibration.CAL_STRUCT + 0x70, 56)
        struct.pack_into("<I", blob, calibration.CAL_STRUCT + 0x88, 8512)
        for desc in (0x2294, 0x22B4):
            struct.pack_into("<II", blob, desc + 4, 152, 56)
        self.stored = bytes(0x76 + (i % 17) for i in range(56))
        blob[calibration.FACTORY_OFFSET : calibration.FACTORY_OFFSET + 56] = self.stored
        self.cal.write_bytes(bytes(blob))

    def tearDown(self):
        self._td.cleanup()

    def run_6f(self, live):
        dev = FakeDevice()
        original = dict(REPLIES)
        REPLIES[0x6F] = factory_reply(live)
        try:
            with mock.patch("builtins.print") as _p:
                rc, log = run_probe(dev, ("only", "6f"), ("calibration", str(self.cal)))
        finally:
            REPLIES.clear()
            REPLIES.update(original)
        return dev, rc, log

    def test_6f_only_sends_the_one_request_and_reads_with_a_big_buffer(self):
        dev, rc, log = self.run_6f(self.stored)
        self.assertEqual(rc, 0)
        self.assertEqual(
            [c for c in dev.calls if c[0] == "write"], [("write", 0x01, bytes.fromhex("6f000e000000000000"))]
        )
        self.assertEqual([c for c in dev.calls if c[0] == "read"], [("read", 0x81, 16384)])
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])
        self.assertIn("RESULT: MATCH", log)

    def test_6f_reports_differences(self):
        live = bytearray(self.stored)
        live[10] ^= 0x04
        _, rc, log = self.run_6f(bytes(live))
        self.assertEqual(rc, 0)
        self.assertIn("RESULT: DIFFER (1 of 56", log)
        self.assertIn("value 10", log)

    def test_6f_combined_with_others_keeps_order(self):
        dev = FakeDevice()
        original = dict(REPLIES)
        REPLIES[0x6F] = factory_reply(self.stored)
        try:
            rc, _ = run_probe(dev, ("only", "6f,01"), ("calibration", str(self.cal)))
        finally:
            REPLIES.clear()
            REPLIES.update(original)
        self.assertEqual(rc, 0)
        self.assertEqual([c[2][0] for c in dev.calls if c[0] == "write"], [0x01, 0x6F])

    def test_6f_without_calibration_file_still_decodes(self):
        dev = FakeDevice()
        original = dict(REPLIES)
        REPLIES[0x6F] = factory_reply(self.stored)
        try:
            rc, log = run_probe(dev, ("only", "6f"), ("calibration", "/nonexistent/cal.bin"))
        finally:
            REPLIES.clear()
            REPLIES.update(original)
        self.assertEqual(rc, 0)
        self.assertIn("cannot read the stored factory values", log)

    def test_6f_nonzero_status_is_a_bad_reply(self):
        dev = FakeDevice()
        original = dict(REPLIES)
        REPLIES[0x6F] = b"\xbe\x04"
        try:
            rc, _ = run_probe(dev, ("only", "6f"), ("calibration", str(self.cal)))
        finally:
            REPLIES.clear()
            REPLIES.update(original)
        self.assertEqual(rc, probe.EXIT_BAD_REPLY)

    def test_6f_dry_run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = probe.main(["--dry-run", "--only", "6f"])
        self.assertEqual(rc, 0)
        self.assertIn("6f 00 0e 00 00 00 00 00 00", buf.getvalue())


class LogNames(unittest.TestCase):
    def test_two_logs_same_second_get_different_files(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = probe.ExchangeLog(Path(td)), probe.ExchangeLog(Path(td))
            try:
                self.assertNotEqual(a.path, b.path)
                self.assertTrue(a.path.exists() and b.path.exists())
            finally:
                a.close()
                b.close()

    def test_same_clock_and_pid_still_unique_and_never_overwrites(self):
        frozen = probe.dt.datetime(2026, 10, 13, 12, 0, 0, 123456, tzinfo=probe.dt.timezone.utc)
        with tempfile.TemporaryDirectory() as td, mock.patch.object(probe, "_now", lambda: frozen):
            logs = [probe.ExchangeLog(Path(td)) for _ in range(3)]
            try:
                names = [lg.path.name for lg in logs]
                self.assertEqual(len(set(names)), 3)
                self.assertIn("20261013-120000-123456", names[0])
                self.assertIn(f"-p{os.getpid()}", names[0])
                self.assertTrue(names[1].endswith("-1.log"))
                for lg in logs:
                    lg.write("INFO", "x")
                    self.assertIn("started", lg.path.read_text())
            finally:
                for lg in logs:
                    lg.close()

    def test_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            lg = probe.ExchangeLog(Path(td), prefix="capture")
            lg.close()
            self.assertTrue(lg.path.name.startswith("capture-"))

    def test_two_probe_runs_in_the_same_second_both_succeed(self):
        with tempfile.TemporaryDirectory() as td:
            for _ in range(2):
                dev = FakeDevice()
                with fake_usb(dev):
                    args = probe.argparse.Namespace(
                        only=None,
                        dry_run=False,
                        detach_kernel_driver=False,
                        timeout_ms=100,
                        log_dir=td,
                        calibration=str(CAL_BLOB),
                    )
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(probe.run(args), 0)
            self.assertEqual(len(list(Path(td).glob("probe-*.log"))), 2)


if __name__ == "__main__":
    unittest.main()
