"""Offline tests of capture.run() against a fake pyusb: no device, no libusb.

Checks the exact bytes written in the finger and --no-finger flows, that the
first bad status or timeout stops the run, that forbidden opcodes can never be
written, and that cleanup (04, 52, release, dispose) happens on every exit path.
"""

import contextlib
import hashlib
import io
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import calibration
import capture
import parsers
from synth_template import setUpModule, tearDownModule  # noqa: F401 (unittest module fixtures)
from test_parsers import V63_QUERY_19, V63_ROM_INFO, V63_SENSOR_ID
from test_probe_flow import USBError, USBTimeoutError, fake_usb

SERIAL = bytes.fromhex("c0ffee000078")  # synthetic, not a real unit's
ROM = bytearray(V63_ROM_INFO)
ROM[0x12:0x18] = SERIAL
ROM = bytes(ROM)

# RomInfo after the module upload, as logged on the unit (hardware run 2):
# byte 17 = 0x0e, bytes 0x1c-0x1d = 02 01.
ROM_LOADED = bytearray(ROM)
ROM_LOADED[17] = 0x0E
ROM_LOADED[0x1C:0x1E] = bytes([2, 1])
ROM_LOADED = bytes(ROM_LOADED)

MODULE = bytes((i * 7 + 3) & 0xFF for i in range(capture.MODULE_LEN))
PIXELS = bytes((i * 5) & 0xFF for i in range(144 * 56))
IMAGE_REPLY = struct.pack("<HIHHHHI", 0, 12 + len(PIXELS), 56, 144, 1, 1, 0) + PIXELS

NOT_READY = bytes.fromhex("020000000000")  # iusearch main.py:100
NO_FINGER = bytes.fromhex("010000000000")  # hardware run 1, straight after 02

EVENTS_OK = [
    bytes.fromhex("00 00 00 00 00"),
    bytes.fromhex("02 00 40 10 00"),
    bytes.fromhex("03 40 01 00 00"),
    bytes.fromhex("03 42 04 00 40"),
]


def synthetic_calibration() -> bytes:
    b = bytearray(calibration.MIN_CAL_LEN)
    struct.pack_into("<III", b, 4, 0xE4, 2, 0x2449)
    b[calibration.SERIAL_OFFSET : calibration.SERIAL_OFFSET + 6] = SERIAL
    struct.pack_into("<I", b, calibration.CAL_STRUCT + 0x70, 56)
    struct.pack_into("<I", b, calibration.CAL_STRUCT + 0x88, 8512)
    for desc in (0x2294, 0x22B4):
        struct.pack_into("<II", b, desc + 4, 152, 56)
    for i in range(56):
        b[calibration.FACTORY_OFFSET + i] = 0x76 + (i % 17)
    for i in range(8512):
        b[calibration.FRAME_OFFSET + i] = 0x70 + (i % 31)
        b[calibration.BASELINE_OFFSET + i] = 0x7C + (i % 7)
    return bytes(b)


class CapDevice:
    bus, address, bcdDevice = 1, 3, 0x0154

    def __init__(self, interrupts=None, replies=None, driver_active=False):
        self.calls = []
        self.driver_active = driver_active
        self.interrupts = list(EVENTS_OK if interrupts is None else interrupts)
        self.replies = {
            0x01: ROM,
            0x19: V63_QUERY_19,
            0x75: V63_SENSOR_ID,
            0x06: b"\x00\x00",
            0x02: b"\x00\x00" + bytes(2152),
            0x51: IMAGE_REPLY,
            0x04: b"\x00\x00",
            0x52: b"\x00\x00",
        }
        self.replies.update(replies or {})
        self.last = None

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

    def reset(self):
        self.calls.append(("reset",))

    def clear_halt(self, ep):
        self.calls.append(("clear_halt", ep))

    def write(self, ep, data, timeout=None):
        self.calls.append(("write", ep, bytes(data)))
        self.last = bytes(data)[0]
        return len(data)

    def read(self, ep, size, timeout=None):
        self.calls.append(("read", ep, size))
        if ep == 0x83:
            if not self.interrupts:
                raise USBTimeoutError("no event")
            item = self.interrupts.pop(0)
            if isinstance(item, BaseException):
                raise item
            return list(item)
        item = self.replies[self.last]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, list):  # successive replies for repeated commands
            item = item.pop(0) if len(item) > 1 else item[0]
            if isinstance(item, BaseException):
                raise item
        return list(item)

    def writes(self):
        return [c[2] for c in self.calls if c[0] == "write"]

    def names(self):
        return [c[0] for c in self.calls]


class Env:
    """Temp dir with a fake DLL (module at the real offset), a synthetic CalibrationData, log and out dirs."""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        dll = bytearray(capture.MODULE_OFFSET + capture.MODULE_LEN + 16)
        dll[capture.MODULE_OFFSET : capture.MODULE_OFFSET + capture.MODULE_LEN] = MODULE
        (self.dir / "fake.dll").write_bytes(bytes(dll))
        (self.dir / "cal.bin").write_bytes(synthetic_calibration())
        self.patch = mock.patch.object(capture, "MODULE_SHA256", hashlib.sha256(MODULE).hexdigest())
        self.patch.start()

    def args(self, *extra):
        argv = [
            "--dll",
            str(self.dir / "fake.dll"),
            "--calibration",
            str(self.dir / "cal.bin"),
            "--log-dir",
            str(self.dir / "logs"),
            "--out-dir",
            str(self.dir / "caps"),
            "--timeout-ms",
            "100",
            "--finger-timeout-ms",
            "100",
            *extra,
        ]
        return capture.build_parser().parse_args(argv)

    def close(self):
        self.patch.stop()
        self._td.cleanup()

    def logs(self):
        return sorted((self.dir / "logs").glob("capture-*.log"))

    def captures(self, ext):
        return sorted((self.dir / "caps").glob(f"capture-*.{ext}"))


def snapshot(paths):
    """Name + contents now, so they outlive the temp dir."""
    out = []
    for p in paths:
        data = p.read_bytes()
        out.append(types.SimpleNamespace(name=p.name, read_bytes=lambda d=data: d))
    return out


def run_capture(dev, *extra, env=None):
    own = env is None
    env = env or Env()
    try:
        out = io.StringIO()
        with fake_usb(dev), contextlib.redirect_stdout(out):
            rc = capture.run(env.args(*extra))
        log = env.logs()[-1].read_text() if env.logs() else ""
        files = (snapshot(env.captures("pgm")), snapshot(env.captures("raw")))
        return rc, log, out.getvalue(), files
    finally:
        if own:
            env.close()


def expected_writes(finger=True):
    cmd02 = calibration.build_cmd02_from_calibration_data(synthetic_calibration())
    return [b"\x01", b"\x19", b"\x75", b"\x06" + MODULE, cmd02, bytes.fromhex("5100200000"), b"\x04", b"\x52"]


class NormalFlows(unittest.TestCase):
    def test_finger_flow_exact_writes(self):
        dev = CapDevice()
        rc, log, out, (pgms, raws) = run_capture(dev)
        self.assertEqual(rc, 0, out)
        self.assertEqual(dev.writes(), expected_writes())
        self.assertEqual({c[1] for c in dev.calls if c[0] == "write"}, {0x01})
        reads = [c for c in dev.calls if c[0] == "read"]
        self.assertEqual({c[1] for c in reads}, {0x81, 0x83})
        # all four interrupt events consumed, none beyond
        self.assertEqual(len([c for c in reads if c[1] == 0x83]), 4)
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])
        # never anything but writes/reads/claim/release/dispose
        self.assertEqual(set(dev.names()) - {"write", "read", "claim", "release", "dispose"}, set())
        self.assertEqual(len(pgms), 1)
        self.assertEqual(len(raws), 1)
        self.assertEqual(raws[0].read_bytes(), IMAGE_REPLY)
        self.assertEqual(pgms[0].read_bytes(), parsers.pgm_bytes(PIXELS))
        self.assertTrue(pgms[0].name.startswith("capture-") and "-p" in pgms[0].name)
        self.assertIn("TX", log)
        self.assertIn("RX", log)

    def test_no_finger_flow_exact_writes_and_no_interrupt_reads(self):
        dev = CapDevice(interrupts=[])
        rc, log, out, (pgms, _) = run_capture(dev, "--no-finger")
        self.assertEqual(rc, 0, out)
        self.assertEqual(dev.writes(), expected_writes(finger=False))
        self.assertEqual({c[1] for c in dev.calls if c[0] == "read"}, {0x81})
        self.assertEqual(len(pgms), 1)

    def test_tx_log_summarises_long_payloads(self):
        dev = CapDevice()
        _, log, _, _ = run_capture(dev)
        self.assertIn(hashlib.sha256(expected_writes()[4]).hexdigest(), log)
        self.assertNotIn(expected_writes()[4].hex(), log)

    def test_two_runs_same_log_dir_get_distinct_files(self):
        env = Env()
        try:
            for _ in range(2):
                rc, _, _, _ = run_capture(CapDevice(), env=env)
                self.assertEqual(rc, 0)
            self.assertEqual(len(env.logs()), 2)
            self.assertEqual(len(env.captures("pgm")), 2)
            self.assertEqual(len(env.captures("raw")), 2)
        finally:
            env.close()

    def test_no_usb_reset_by_default_and_never_clear_halt(self):
        dev = CapDevice()
        run_capture(dev, "--no-finger")
        self.assertNotIn("reset", dev.names())
        self.assertNotIn("clear_halt", dev.names())

    def test_usb_reset_only_with_flag(self):
        dev = CapDevice()
        rc, _, _, _ = run_capture(dev, "--no-finger", "--usb-reset")
        self.assertEqual(rc, 0)
        names = dev.names()
        self.assertEqual(names.count("reset"), 1)
        self.assertLess(names.index("reset"), names.index("claim"))

    def test_extend_event_keeps_waiting(self):
        events = EVENTS_OK[:2] + [bytes.fromhex("03 50 05 00 00")] + EVENTS_OK[3:]
        rc, _, out, _ = run_capture(CapDevice(interrupts=events))
        self.assertEqual(rc, 0, out)


class ModuleLoadedState(unittest.TestCase):
    def test_module_is_sent_when_romInfo_says_nothing_is_loaded(self):
        dev = CapDevice()
        rc, log, _, _ = run_capture(dev, "--no-finger")
        self.assertEqual(rc, 0)
        self.assertIn(0x06, [w[0] for w in dev.writes()])
        self.assertIn("sending 06", log)

    def test_module_skipped_when_romInfo_shows_it_loaded(self):
        dev = CapDevice(replies={0x01: ROM_LOADED})
        rc, log, out, _ = run_capture(dev, "--no-finger")
        self.assertEqual(rc, 0, out)
        ops = [w[0] for w in dev.writes()]
        self.assertEqual(ops, [0x01, 0x19, 0x75, 0x02, 0x51, 0x04, 0x52])  # no 06, 02 still sent
        self.assertIn("skipping 06", log)
        self.assertIn("UNCERTAIN", log)
        self.assertIn("MODULE", log)

    def test_force_module_sends_it_anyway(self):
        dev = CapDevice(replies={0x01: ROM_LOADED})
        rc, log, _, _ = run_capture(dev, "--no-finger", "--force-module")
        self.assertEqual(rc, 0)
        self.assertEqual([w[0] for w in dev.writes()], [0x01, 0x19, 0x75, 0x06, 0x02, 0x51, 0x04, 0x52])
        self.assertIn("--force-module", log)

    def test_force_module_refused_with_04aa_stops_without_arming(self):
        # hardware run 2: 06 with a module already loaded -> aa 04
        dev = CapDevice(replies={0x01: ROM_LOADED, 0x06: b"\xaa\x04"})
        rc, log, _, _ = run_capture(dev, "--no-finger", "--force-module")
        self.assertEqual(rc, capture.EXIT_BAD_REPLY)
        self.assertEqual([w[0] for w in dev.writes()], [0x01, 0x19, 0x75, 0x06])
        self.assertIn("0x04aa", log)
        self.assertIn("already loaded", log)

    def test_finger_flow_also_skips_the_module(self):
        dev = CapDevice(replies={0x01: ROM_LOADED})
        rc, _, out, _ = run_capture(dev)
        self.assertEqual(rc, 0, out)
        self.assertNotIn(0x06, [w[0] for w in dev.writes()])

    def test_module_decision_is_pure(self):
        self.assertEqual(capture.module_decision({"u1": 0}, False)[0], False)
        self.assertEqual(capture.module_decision({"u1": 0x0E}, False)[0], True)
        self.assertEqual(capture.module_decision({"u1": 0x0E}, True)[0], False)
        self.assertEqual(capture.module_decision({}, False)[0], False)
        self.assertIn("UNCERTAIN", capture.module_decision({"u1": 0x0E}, False)[1])


class DryRun(unittest.TestCase):
    def test_dry_run_prints_sequence_and_never_imports_usb(self):
        env = Env()
        try:
            out = io.StringIO()
            with mock.patch.dict(sys.modules, {"usb": None, "usb.core": None, "usb.util": None, "usb.backend": None}):
                with contextlib.redirect_stdout(out):
                    rc = capture.run(env.args("--dry-run"))
            text = out.getvalue()
            self.assertEqual(rc, 0)
            for needle in (
                "WRITE EP 0x01: 01",
                "WRITE EP 0x01: 19",
                "WRITE EP 0x01: 75",
                "51 00 20 00 00",
                "04",
                "52",
                "10501 bytes, sha256",
                "CONDITIONAL",
                "--force-module",
                "01 00 00 00 00 00",
                "18869 bytes, sha256",
                "ETW lines",
                "Never sent",
            ):
                self.assertIn(needle, text)
            self.assertFalse(env.logs())  # a dry run writes no log and no captures
            self.assertFalse(env.captures("pgm"))
            self.assertNotIn("wait for finger down", capture_text_no_finger(env))
        finally:
            env.close()


def capture_text_no_finger(env):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        capture.run(env.args("--dry-run", "--no-finger"))
    return out.getvalue()


class Aborts(unittest.TestCase):
    def assert_cleanup(self, dev):
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])

    def test_bad_status_on_19_stops_everything(self):
        dev = CapDevice(replies={0x19: b"\xbe\x04" + bytes(66)})
        rc, _, _, (pgms, _) = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_BAD_REPLY)
        self.assertEqual(dev.writes(), [b"\x01", b"\x19"])  # not armed: no 04/52 either
        self.assertEqual(pgms, [])
        self.assert_cleanup(dev)

    def test_bad_status_on_module_never_sends_02(self):
        dev = CapDevice(replies={0x06: b"\xbe\x04"})
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_BAD_REPLY)
        self.assertEqual(dev.writes()[-1][0], 0x06)
        self.assertNotIn(0x02, [w[0] for w in dev.writes()])
        self.assertNotIn(b"\x04", dev.writes())
        self.assert_cleanup(dev)

    def test_bad_status_on_02_sends_04_52_and_stops(self):
        dev = CapDevice(replies={0x02: b"\x91\x04" + bytes(2152)})
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_BAD_REPLY)
        self.assertEqual([w[0] for w in dev.writes()], [0x01, 0x19, 0x75, 0x06, 0x02, 0x04, 0x52])
        self.assert_cleanup(dev)

    def test_short_reply_status_is_a_failure(self):
        dev = CapDevice(replies={0x06: b"\x00"})
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_BAD_REPLY)

    def test_read_timeout_on_first_query(self):
        dev = CapDevice(replies={0x01: USBTimeoutError("t")})
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_USB_ERROR)
        self.assertEqual(dev.writes(), [b"\x01"])
        self.assert_cleanup(dev)

    def test_no_finger_timeout_cleans_up_and_never_requests_image(self):
        dev = CapDevice(interrupts=[EVENTS_OK[0]])
        rc, _, _, (pgms, _) = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_USB_ERROR)
        self.assertEqual([w[0] for w in dev.writes()], [0x01, 0x19, 0x75, 0x06, 0x02, 0x04, 0x52])
        self.assertEqual(pgms, [])
        self.assert_cleanup(dev)

    def test_failed_event_code_aborts(self):
        dev = CapDevice(interrupts=[EVENTS_OK[1], bytes.fromhex("03 58 06 00 50")])
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_CAPTURE_FAILED)
        self.assertNotIn(0x51, [w[0] for w in dev.writes()])
        self.assertEqual([w[0] for w in dev.writes()][-2:], [0x04, 0x52])

    def test_bad_image_status_stops_by_default_and_keeps_raw(self):
        dev = CapDevice(replies={0x51: b"\x02\x00\x00\x00\x00\x00"})
        env = Env()
        try:
            rc, _, out, (pgms, raws) = run_capture(dev, "--no-finger", env=env)
            self.assertEqual(rc, capture.EXIT_BAD_REPLY)
            self.assertEqual([w[0] for w in dev.writes()].count(0x51), 1)
            self.assertEqual(pgms, [])
            self.assertEqual([r.read_bytes() for r in raws], [b"\x02\x00\x00\x00\x00\x00"])
            self.assertEqual([w[0] for w in dev.writes()][-2:], [0x04, 0x52])
        finally:
            env.close()

    def test_not_ready_status_is_followed_by_a_plain_read_first(self):
        # iusearch main.py:100-101: a 6-byte 02 00 ... reply is answered by another READ, no re-send
        dev = CapDevice(replies={0x51: [NOT_READY, IMAGE_REPLY]})
        rc, _, out, (pgms, _) = run_capture(dev, "--no-finger")
        self.assertEqual(rc, 0, out)
        self.assertEqual([w[0] for w in dev.writes()].count(0x51), 1)
        self.assertEqual(len(pgms), 1)

    def test_not_ready_then_silence_resends_51(self):
        # PR #288 sensor_2449.py:546-553: ask again
        dev = CapDevice(replies={0x51: [NOT_READY, USBTimeoutError("quiet"), IMAGE_REPLY]})
        rc, log, out, (pgms, _) = run_capture(dev, "--no-finger")
        self.assertEqual(rc, 0, out)
        self.assertEqual([w[0] for w in dev.writes()].count(0x51), 2)
        self.assertIn("re-sending 51", log)
        self.assertEqual(len(pgms), 1)

    def test_not_ready_forever_stops_after_the_bounded_number_of_replies(self):
        dev = CapDevice(replies={0x51: [NOT_READY]})
        rc, log, _, (pgms, raws) = run_capture(dev, "--no-finger", "--image-retries", "2")
        self.assertEqual(rc, capture.EXIT_BAD_REPLY)
        self.assertEqual(len([c for c in dev.calls if c[0] == "read" and c[1] == 0x81 and c[2] == 16384]), 3)
        self.assertEqual(pgms, [])
        self.assertEqual(len(raws), 1)
        self.assertEqual([w[0] for w in dev.writes()][-2:], [0x04, 0x52])
        self.assertIn("not ready", log)

    def test_zero_retries_stops_on_the_first_not_ready(self):
        dev = CapDevice(replies={0x51: [NOT_READY]})
        rc, _, _, _ = run_capture(dev, "--no-finger", "--image-retries", "0")
        self.assertEqual(rc, capture.EXIT_BAD_REPLY)
        self.assertEqual(len([c for c in dev.calls if c[0] == "read" and c[2] == 16384]), 1)

    def test_other_nonzero_status_is_never_retried(self):
        dev = CapDevice(replies={0x51: [b"\xbe\x04"]})
        rc, _, _, _ = run_capture(dev, "--no-finger", "--image-retries", "5")
        self.assertEqual(rc, capture.EXIT_BAD_REPLY)
        self.assertEqual(len([c for c in dev.calls if c[0] == "read" and c[2] == 16384]), 1)
        self.assertEqual([w[0] for w in dev.writes()].count(0x51), 1)

    def test_no_finger_status_1_is_the_expected_answer(self):
        # hardware run 1: 51 straight after 02 gave 01 00 00 00 00 00
        dev = CapDevice(replies={0x51: [NO_FINGER]})
        rc, log, out, (pgms, raws) = run_capture(dev, "--no-finger")
        self.assertEqual(rc, 0)
        self.assertIn("RESULT: no image, as expected without a finger", out)
        self.assertIn("RESULT", log)
        self.assertEqual(pgms, [])
        self.assertEqual([r.read_bytes() for r in raws], [NO_FINGER])  # kept for diagnosis
        self.assertEqual([w[0] for w in dev.writes()].count(0x51), 1)  # no retries for the expected answer
        self.assertEqual([w[0] for w in dev.writes()][-2:], [0x04, 0x52])
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])

    def test_status_1_with_a_finger_is_retried_then_reported_as_no_finger(self):
        dev = CapDevice(replies={0x51: [NO_FINGER]})
        rc, _, _, _ = run_capture(dev, "--image-retries", "1")
        self.assertEqual(rc, capture.EXIT_BAD_REPLY)
        self.assertEqual(len([c for c in dev.calls if c[0] == "read" and c[2] == 16384]), 2)

    def test_image_is_read_on_ep_0x81_never_0x82(self):
        dev = CapDevice()
        run_capture(dev)
        self.assertNotIn(0x82, {c[1] for c in dev.calls if c[0] == "read"})

    def test_ctrl_c_while_waiting_cleans_up(self):
        dev = CapDevice(interrupts=[EVENTS_OK[0], KeyboardInterrupt()])
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_INTERRUPTED)
        self.assertEqual([w[0] for w in dev.writes()][-2:], [0x04, 0x52])
        self.assert_cleanup(dev)

    def test_usb_error_mid_run_does_not_keep_writing(self):
        dev = CapDevice(replies={0x06: USBError("pipe")})
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_USB_ERROR)
        self.assertEqual(dev.writes()[-1][0], 0x06)
        self.assert_cleanup(dev)

    def test_usb_error_after_02_skips_cleanup_writes(self):
        dev = CapDevice(interrupts=[EVENTS_OK[0], USBError("gone")])
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_USB_ERROR)
        self.assertEqual(dev.writes()[-1][0], 0x02)
        self.assert_cleanup(dev)

    def test_failing_cleanup_does_not_mask_outcome_or_skip_release(self):
        dev = CapDevice(interrupts=[EVENTS_OK[0]], replies={0x04: USBTimeoutError("t")})
        rc, log, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_USB_ERROR)
        self.assertEqual([w[0] for w in dev.writes()][-2:], [0x04, 0x52])  # 52 still tried after 04 timed out
        self.assertIn("cleanup step", log)
        self.assert_cleanup(dev)

    def test_kernel_driver_refused_without_flag(self):
        dev = CapDevice(driver_active=True)
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_DRIVER_BUSY)
        self.assertEqual(dev.names(), ["dispose"])

    def test_kernel_driver_detached_and_reattached(self):
        dev = CapDevice(driver_active=True)
        rc, _, _, _ = run_capture(dev, "--no-finger", "--detach-kernel-driver")
        self.assertEqual(rc, 0)
        self.assertEqual(dev.names()[-3:], ["release", "attach", "dispose"])

    def test_device_not_found(self):
        rc, _, _, _ = run_capture(None)
        self.assertEqual(rc, capture.EXIT_NOT_FOUND)


class IdentityChecks(unittest.TestCase):
    def test_serial_mismatch_aborts_before_anything_is_written_to_the_sensor(self):
        other = bytearray(ROM)
        other[0x12:0x18] = bytes.fromhex("000000000001")
        dev = CapDevice(replies={0x01: bytes(other)})
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_BAD_INPUT)
        self.assertEqual(dev.writes(), [b"\x01"])

    def test_wrong_rom_aborts(self):
        other = bytearray(ROM)
        other[11] = 0x15
        dev = CapDevice(replies={0x01: bytes(other)})
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_BAD_INPUT)
        self.assertEqual(dev.writes(), [b"\x01"])

    def test_wrong_sensor_type_aborts_before_the_module(self):
        dev = CapDevice(replies={0x75: bytes.fromhex("00 00 00 00 00 00 01 00 01 00")})
        rc, _, _, _ = run_capture(dev)
        self.assertEqual(rc, capture.EXIT_BAD_INPUT)
        self.assertEqual(dev.writes(), [b"\x01", b"\x19", b"\x75"])

    def test_skip_flag_lets_a_serial_mismatch_through(self):
        other = bytearray(ROM)
        other[0x12:0x18] = bytes.fromhex("000000000001")
        dev = CapDevice(replies={0x01: bytes(other)})
        rc, _, _, _ = run_capture(dev, "--no-finger", "--skip-identity-checks")
        self.assertEqual(rc, 0)

    def test_bad_module_hash_is_refused_without_touching_usb(self):
        env = Env()
        try:
            env.patch.stop()
            with mock.patch.object(capture, "MODULE_SHA256", "0" * 64):
                dev = CapDevice()
                rc, _, _, _ = run_capture(dev, env=env)
            self.assertEqual(rc, capture.EXIT_BAD_INPUT)
            self.assertEqual(dev.calls, [])
            env.patch.start()
        finally:
            env.close()

    def test_wrong_calibration_is_refused_without_touching_usb(self):
        env = Env()
        try:
            (env.dir / "cal.bin").write_bytes(b"\x00" * 100)
            dev = CapDevice()
            rc, _, _, _ = run_capture(dev, env=env)
            self.assertEqual(rc, capture.EXIT_BAD_INPUT)
            self.assertEqual(dev.calls, [])
        finally:
            env.close()


class OpcodeGuard(unittest.TestCase):
    def test_forbidden_and_unknown_opcodes_refused(self):
        for op in (0x4F, 0x1A, 0x50, 0x3E, 0x08, 0x07, 0x6F, 0x00, 0xFF):
            with self.assertRaises(capture.UnsafeCommand, msg=hex(op)):
                capture.check_opcode(bytes([op, 1, 2]))
        with self.assertRaises(capture.UnsafeCommand):
            capture.check_opcode(b"")

    def test_allowed_opcodes(self):
        for op in (0x01, 0x19, 0x75, 0x06, 0x02, 0x51, 0x04, 0x52):
            capture.check_opcode(bytes([op]))

    def test_session_write_goes_through_the_guard(self):
        dev = CapDevice()
        env = Env()
        try:
            with fake_usb(dev):
                sess = capture.Session(dev, mock.Mock(), env.args())
                with self.assertRaises(capture.UnsafeCommand):
                    sess.write(b"\x4f" + bytes(876), 100)
            self.assertEqual(dev.writes(), [])
        finally:
            env.close()

    def test_plan_only_contains_allowed_opcodes(self):
        cmd02 = calibration.build_cmd02_from_calibration_data(synthetic_calibration())
        for finger in (True, False):
            for step in capture.build_plan(MODULE, cmd02, finger=finger):
                if step.tx:
                    capture.check_opcode(step.tx)

    def test_plan_cites_a_source_for_every_step(self):
        cmd02 = calibration.build_cmd02_from_calibration_data(synthetic_calibration())
        for step in capture.build_plan(MODULE, cmd02):
            self.assertTrue(step.source, step.name)


class RealInputs(unittest.TestCase):
    DLL = capture.DEFAULT_DLL

    @unittest.skipUnless(DLL.exists(), "driverstore DLL not present")
    def test_real_dll_module_matches_pinned_hash(self):
        module = capture.read_module(self.DLL)
        self.assertEqual(len(module), 10500)
        self.assertEqual(module[:8].hex(), "020000016200213c")


if __name__ == "__main__":
    unittest.main()
