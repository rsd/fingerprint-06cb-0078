"""Offline tests of the collection mode (capture.py --count N --label NAME) against a fake pyusb.

The fake device is phase aware: it queues a scripted list of EP 0x83 events after every 02,
and a second list ("stale", e.g. a late finger-up) after every 52, so late events, a timeout
partway and a finger that is never lifted can be staged.
"""

import contextlib
import io
import json
import struct
import unittest
from pathlib import Path
from unittest import mock

import capture
import parsers
from synth_template import setUpModule, tearDownModule  # noqa: F401 (unittest module fixtures)
from test_capture_flow import EVENTS_OK, NOT_READY, CapDevice, Env, fake_usb
from test_probe_flow import USBError, USBTimeoutError  # noqa: F401 (USBError kept for staging)


def image_reply(seed: int) -> bytes:
    pixels = bytes(((i * (seed + 3)) ^ (i // 144 * 11 * seed)) & 0xFF for i in range(144 * 56))
    return struct.pack("<HIHHHHI", 0, 12 + len(pixels), 56, 144, 1, 1, 0) + pixels


class CollectDevice(CapDevice):
    def __init__(self, arms=None, stale=None, images=None, **kw):
        super().__init__(interrupts=[], **kw)
        self.arms = arms if arms is not None else [EVENTS_OK] * 10
        self.stale = stale or {}
        self.arm = 0
        self.replies[0x51] = images if images is not None else [image_reply(k) for k in range(1, 11)]

    def write(self, ep, data, timeout=None):
        n = super().write(ep, data, timeout)
        if bytes(data)[0] == 0x02:
            self.arm += 1
            self.interrupts = list(self.arms[self.arm - 1]) if self.arm <= len(self.arms) else []
        elif bytes(data)[0] == 0x52:
            self.interrupts = list(self.stale.get(self.arm, []))
        return n

    def opcodes(self):
        return [w[0] for w in self.writes()]


def run_collect(dev, *extra, answers=None, interactive=False, env=None):
    own = env is None
    env = env or Env()
    sleeps = []
    try:
        out = io.StringIO()
        asks = iter(answers or [])
        with (
            fake_usb(dev),
            contextlib.redirect_stdout(out),
            mock.patch.object(capture.time, "sleep", lambda s: sleeps.append(s)),
            mock.patch.object(capture, "_interactive", lambda: interactive),
            mock.patch.object(capture, "_ask", lambda prompt: next(asks)),
        ):
            rc = capture.run(env.args(*extra))
        label = extra[extra.index("--label") + 1] if "--label" in extra else None
        files, manifest = {}, None
        if label and (env.dir / "caps" / label).exists():
            for f in sorted((env.dir / "caps" / label).iterdir()):
                files[f.name] = f.read_bytes()
            if "manifest.json" in files:
                manifest = json.loads(files["manifest.json"])
        log = env.logs()[-1].read_text() if env.logs() else ""
        return types_ns(rc=rc, out=out.getvalue(), log=log, files=files, manifest=manifest, sleeps=sleeps)
    finally:
        if own:
            env.close()


def types_ns(**kw):
    import types

    return types.SimpleNamespace(**kw)


COLLECT = ("--label", "fp", "--lift-pause-s", "0")


class ThreeImages(unittest.TestCase):
    def test_exact_sequence_files_and_manifest(self):
        dev = CollectDevice()
        r = run_collect(dev, *COLLECT, "--count", "3")
        self.assertEqual(r.rc, 0, r.out)
        # one 06 for the whole session, then 02 / 51 / 04 / 52 per image
        self.assertEqual(dev.opcodes(), [0x01, 0x19, 0x75, 0x06] + [0x02, 0x51, 0x04, 0x52] * 3)
        self.assertEqual(sum(1 for w in dev.writes() if w[0] == 0x02), 3)
        self.assertEqual({c[1] for c in dev.calls if c[0] == "read"} - {0x81, 0x83}, set())
        names = sorted(r.files)
        self.assertEqual(
            names,
            ["fp-01.pgm", "fp-01.raw", "fp-02.pgm", "fp-02.raw", "fp-03.pgm", "fp-03.raw", "manifest.json"],
        )
        for k in (1, 2, 3):
            self.assertEqual(r.files[f"fp-0{k}.raw"], image_reply(k))
            self.assertTrue(r.files[f"fp-0{k}.pgm"].startswith(b"P5\n144 56\n255\n"))
        m = r.manifest
        self.assertEqual((m["label"], m["requested_count"], m["status"], m["accepted_count"]), ("fp", 3, "complete", 3))
        self.assertEqual(len(m["images"]), 3)
        for k, e in enumerate(m["images"], 1):
            self.assertEqual((e["pgm"], e["raw"], e["accepted"]), (f"fp-0{k}.pgm", f"fp-0{k}.raw", True))
            self.assertTrue(e["timestamp"])
            self.assertEqual(sorted(e["stats"]), ["coverage", "max", "mean", "min", "sd"])
            self.assertEqual(e["events"], [ev.hex() for ev in EVENTS_OK])
            self.assertEqual((e["status_04"], e["status_52"]), ("0x0000", "0x0000"))
            self.assertEqual(e["drained_before"], [])
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])

    def test_prompts_bell_and_quality_line(self):
        r = run_collect(COLLECT_DEV(), *COLLECT, "--count", "2")
        self.assertIn("[1/2] touch the sensor now", r.out)
        self.assertIn("[2/2] touch the sensor now", r.out)
        self.assertIn("\a", r.out)
        self.assertIn("[1/2] lift your finger", r.out)
        self.assertIn("fixed pause", r.out)
        self.assertRegex(
            r.out, r"\[1/2\] saved fp-01\.pgm: min \d+, max \d+, mean [\d.]+, sd [\d.]+, ridge coverage ~\d+%"
        )
        self.assertIn("PROMPT", r.log)
        self.assertIn("SUMMARY", r.log)

    def test_no_bell_option(self):
        r = run_collect(COLLECT_DEV(), *COLLECT, "--count", "1", "--no-bell")
        self.assertNotIn("\a", r.out)

    def test_single_image_with_label_uses_the_collection_layout(self):
        r = run_collect(COLLECT_DEV(), *COLLECT)
        self.assertEqual(sorted(r.files), ["fp-01.pgm", "fp-01.raw", "manifest.json"])

    def test_pause_defaults_to_three_seconds_when_not_a_terminal(self):
        r = run_collect(COLLECT_DEV(), "--label", "fp", "--count", "2")
        self.assertEqual(r.sleeps, [capture.DEFAULT_LIFT_PAUSE_S] * 2)


def COLLECT_DEV(**kw):
    return CollectDevice(**kw)


class StopStatus(unittest.TestCase):
    def test_0412_after_an_image_is_not_an_error(self):
        dev = CollectDevice(replies={0x04: b"\x12\x04"})
        r = run_collect(dev, *COLLECT)
        self.assertEqual(r.rc, 0)
        self.assertIn("no capture in progress (inferred)", r.log)
        line = next(x for x in r.log.splitlines() if "no capture in progress" in x)
        self.assertIn(" INFO ", line)
        self.assertIn("[UNCERTAIN]", line)
        self.assertEqual(r.manifest["images"][0]["status_04"], "0x0412")
        self.assertNotIn("ERROR", r.log)

    def test_0412_in_single_image_mode(self):
        dev = CapDevice(replies={0x04: b"\x12\x04"})
        env = Env()
        try:
            out = io.StringIO()
            with fake_usb(dev), contextlib.redirect_stdout(out):
                rc = capture.run(env.args())
            log = env.logs()[-1].read_text()
        finally:
            env.close()
        self.assertEqual(rc, 0)
        self.assertIn("no capture in progress (inferred) [UNCERTAIN]", log)
        self.assertIn("no capture in progress (inferred)", out.getvalue())

    def test_0412_without_an_image_is_also_just_information(self):
        # a finger timeout: 04 normally says 0; if it says 0x0412 that is the same "nothing to stop"
        dev = CapDevice(interrupts=[EVENTS_OK[0]], replies={0x04: b"\x12\x04"})
        env = Env()
        try:
            with fake_usb(dev), contextlib.redirect_stdout(io.StringIO()):
                rc = capture.run(env.args())
            log = env.logs()[-1].read_text()
        finally:
            env.close()
        self.assertEqual(rc, capture.EXIT_USB_ERROR)
        line = next(x for x in log.splitlines() if "no capture in progress" in x)
        self.assertIn(" INFO ", line)

    def test_classifier(self):
        c = parsers.classify_stop_status
        self.assertEqual(c(0, False)[0], "capture stopped")
        for done in (True, False):
            self.assertEqual(c(0x0412, done)[0], "no capture in progress (inferred)")
            self.assertEqual(c(0x0412, done)[1], parsers.UNCERTAIN)
        self.assertIn("failed capture", c(0x0412, True)[2])  # both observations are cited
        self.assertIn("delivered image", c(0x0412, True)[2])
        self.assertIn("unclassified", c(0x04BE, True)[0])
        self.assertIn("no status", c(None, True)[0])


class LateFingerAndStaleEvents(unittest.TestCase):
    def test_late_events_after_an_image_are_drained_not_taken_for_the_next_touch(self):
        late = [bytes.fromhex("02 00 40 10 00"), bytes.fromhex("03 42 04 00 40")]  # looks like finger + ready
        dev = CollectDevice(stale={1: late})
        r = run_collect(dev, *COLLECT, "--count", "2")
        self.assertEqual(r.rc, 0, r.out)
        e1, e2 = r.manifest["images"]
        self.assertEqual(e1["drained_before"], [])
        self.assertEqual(e2["drained_before"], [x.hex() for x in late])
        self.assertEqual(e2["events"], [ev.hex() for ev in EVENTS_OK])  # only its own events
        self.assertIn("DRAIN", r.log)
        self.assertEqual(r.files["fp-02.raw"], image_reply(2))

    def test_finger_never_lifted_is_flagged_as_a_duplicate(self):
        same = image_reply(5)
        dev = CollectDevice(images=[same])
        r = run_collect(dev, *COLLECT, "--count", "2")
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.manifest["images"][0]["warnings"], [])
        self.assertTrue(r.manifest["images"][1]["warnings"])
        self.assertIn("not lifted", r.manifest["images"][1]["warnings"][0])
        self.assertIn("WARNING", r.out)

    def test_late_lift_slow_events_still_within_the_timeout(self):
        # the second touch is preceded by two start/ack style events, all ignored until the finger event
        arms = [EVENTS_OK, [EVENTS_OK[0], EVENTS_OK[0]] + EVENTS_OK[1:]]
        r = run_collect(CollectDevice(arms=arms), *COLLECT, "--count", "2")
        self.assertEqual(r.rc, 0)
        self.assertEqual(len(r.manifest["images"][1]["events"]), 5)


class Failures(unittest.TestCase):
    def test_timeout_partway_keeps_the_images_already_saved(self):
        arms = [EVENTS_OK, EVENTS_OK, [EVENTS_OK[0]]]  # third touch never comes
        dev = CollectDevice(arms=arms)
        r = run_collect(dev, *COLLECT, "--count", "4")
        self.assertEqual(r.rc, capture.EXIT_USB_ERROR)
        self.assertEqual(sorted(r.files), ["fp-01.pgm", "fp-01.raw", "fp-02.pgm", "fp-02.raw", "manifest.json"])
        self.assertEqual(r.files["fp-01.raw"], image_reply(1))
        self.assertEqual(r.files["fp-02.raw"], image_reply(2))
        m = r.manifest
        self.assertTrue(m["status"].startswith("incomplete after 2 accepted image(s)"), m["status"])
        self.assertEqual([e["accepted"] for e in m["images"]], [True, True])
        # the armed third block is cleaned up, then everything is released
        self.assertEqual(dev.opcodes()[-3:], [0x02, 0x04, 0x52])
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])
        self.assertIn("timeout waiting for a finger-down event", r.log)

    def test_failed_event_restarts_the_image_and_gives_up_after_max_attempts(self):
        bad = [EVENTS_OK[1], bytes.fromhex("03 58 06 00 50")]
        arms = [EVENTS_OK, bad, bad, bad]
        dev = CollectDevice(arms=arms)
        r = run_collect(dev, *COLLECT, "--count", "3")
        self.assertEqual(r.rc, capture.EXIT_CAPTURE_FAILED)
        self.assertIn("fp-01.pgm", r.files)
        self.assertNotIn("fp-02.pgm", r.files)
        self.assertEqual(len(r.manifest["images"]), 1)
        self.assertEqual(len(r.manifest["failed_attempts"]), 3)
        self.assertIn("giving up on image 2 of 3 after 3 failed captures", r.manifest["status"])
        # each failed block is closed with 04/52 before the next 02; no 51 for a failed event
        self.assertEqual(dev.opcodes()[4:], [0x02, 0x51, 0x04, 0x52] + [0x02, 0x04, 0x52] * 3)

    def test_ctrl_c_partway_keeps_images_and_cleans_up(self):
        arms = [EVENTS_OK, [EVENTS_OK[0], KeyboardInterrupt()]]
        dev = CollectDevice(arms=arms)
        r = run_collect(dev, *COLLECT, "--count", "3")
        self.assertEqual(r.rc, capture.EXIT_INTERRUPTED)
        self.assertIn("fp-01.raw", r.files)
        self.assertIn("KeyboardInterrupt", r.manifest["status"])
        self.assertEqual(dev.opcodes()[-3:], [0x02, 0x04, 0x52])
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])

    def test_bad_status_on_02_of_the_second_image(self):
        dev = CollectDevice(replies={0x02: [b"\x00\x00" + bytes(2152), b"\x91\x04" + bytes(2152)]})
        r = run_collect(dev, *COLLECT, "--count", "3")
        self.assertEqual(r.rc, capture.EXIT_BAD_REPLY)
        self.assertIn("fp-01.pgm", r.files)
        self.assertEqual(dev.opcodes()[-3:], [0x02, 0x04, 0x52])
        self.assertEqual(len(r.manifest["images"]), 1)

    def test_existing_collection_is_refused_before_any_usb_traffic(self):
        env = Env()
        try:
            (env.dir / "caps" / "fp").mkdir(parents=True)
            (env.dir / "caps" / "fp" / "manifest.json").write_text("{}")
            dev = CollectDevice()
            r = run_collect(dev, *COLLECT, env=env)
        finally:
            env.close()
        self.assertEqual(r.rc, capture.EXIT_BAD_INPUT)
        self.assertEqual(dev.calls, [])

    def test_nothing_is_ever_overwritten(self):
        env = Env()
        try:
            run_collect(CollectDevice(), "--label", "a", "--lift-pause-s", "0", env=env)
            first = (env.dir / "caps" / "a" / "a-01.raw").read_bytes()
            r = run_collect(CollectDevice(images=[image_reply(9)]), "--label", "a", "--lift-pause-s", "0", env=env)
            self.assertEqual(r.rc, capture.EXIT_BAD_INPUT)
            self.assertEqual((env.dir / "caps" / "a" / "a-01.raw").read_bytes(), first)
        finally:
            env.close()


# The real failed reply (capture-20261013-231635-*.log, image 8) and what followed it.
ERROR_REPLY = bytes.fromhex("07000c000000000000004d01080002000080")
NO_CAPTURE = bytes.fromhex("120400000000")
BAD_EVENTS = [
    bytes.fromhex("0200401000"),
    bytes.fromhex("0340010000"),
    bytes.fromhex("0360070040"),  # code 7, as before the failure
]


class FailedCapture(unittest.TestCase):
    def test_error_reply_restarts_the_image_from_02_and_succeeds(self):
        dev = CollectDevice(arms=[EVENTS_OK, BAD_EVENTS, EVENTS_OK, EVENTS_OK])
        dev.replies[0x51] = [image_reply(1), ERROR_REPLY, image_reply(2), image_reply(3)]
        r = run_collect(dev, *COLLECT, "--count", "3")
        self.assertEqual(r.rc, 0, r.out)
        # after 06: image 1; image 2 fails (02 51 04 52) and is retaken (02 51 04 52); image 3
        self.assertEqual(dev.opcodes()[4:], [0x02, 0x51, 0x04, 0x52] * 4)
        # 51 is never re-sent after the error reply
        self.assertEqual(dev.opcodes().count(0x51), 4)
        # file numbers are not consumed by the failed take
        self.assertEqual(sorted(f for f in r.files if f.endswith(".pgm")), ["fp-01.pgm", "fp-02.pgm", "fp-03.pgm"])
        self.assertEqual(r.files["fp-02.raw"], image_reply(2))
        m = r.manifest
        self.assertEqual(m["status"], "complete")
        self.assertEqual(len(m["failed_attempts"]), 1)
        f = m["failed_attempts"][0]
        self.assertEqual((f["for_index"], f["attempt"], f["kind"]), (2, 1, "capture_error"))
        self.assertEqual(f["reply_hex"], ERROR_REPLY.hex())
        self.assertEqual(f["events"], [e.hex() for e in BAD_EVENTS])
        self.assertIn("bad capture (error 2), lift and touch again", f["message"])
        self.assertEqual([e["index"] for e in m["images"]], [1, 2, 3])
        # told to the user and logged
        self.assertIn("bad capture (error 2), lift and touch again", r.out)
        self.assertIn("[2/3] bad capture (error 2), lift and touch again (fixed pause", r.out)
        self.assertIn("capture error: error 2 [UNCERTAIN]", r.log)
        self.assertIn("event code 7 counts as 'ready'", r.log)

    def test_retry_limit_gives_up_cleanly_and_keeps_what_was_saved(self):
        dev = CollectDevice(arms=[EVENTS_OK] + [BAD_EVENTS] * 5)
        dev.replies[0x51] = [image_reply(1), ERROR_REPLY]
        r = run_collect(dev, *COLLECT, "--count", "3")
        self.assertEqual(r.rc, capture.EXIT_CAPTURE_FAILED)
        self.assertEqual(sorted(f for f in r.files if f.endswith(".pgm")), ["fp-01.pgm"])
        self.assertEqual(len(r.manifest["failed_attempts"]), 3)  # default --max-attempts 3
        self.assertIn("giving up on image 2 of 3 after 3 failed captures", r.manifest["status"])
        self.assertIn("giving up on image 2", r.out)
        # image 1 (4 commands) + 3 failed attempts (02 51 04 52 each); nothing armed is left
        self.assertEqual(dev.opcodes()[4:], [0x02, 0x51, 0x04, 0x52] * 4)
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])

    def test_max_attempts_option(self):
        dev = CollectDevice(arms=[BAD_EVENTS] * 5)
        dev.replies[0x51] = [ERROR_REPLY]
        r = run_collect(dev, *COLLECT, "--count", "1", "--max-attempts", "5")
        self.assertEqual(r.rc, capture.EXIT_CAPTURE_FAILED)
        self.assertEqual(len(r.manifest["failed_attempts"]), 5)
        self.assertEqual(dev.opcodes().count(0x02), 5)
        dev = CollectDevice(arms=[BAD_EVENTS] * 5)
        dev.replies[0x51] = [ERROR_REPLY]
        r = run_collect(dev, *COLLECT, "--count", "1", "--max-attempts", "1")
        self.assertEqual(dev.opcodes().count(0x02), 1)

    def test_a_success_resets_the_failure_count(self):
        # fail, ok, fail, ok, fail, ok: never 3 failures for the same image
        arms = [BAD_EVENTS, EVENTS_OK, BAD_EVENTS, EVENTS_OK, BAD_EVENTS, EVENTS_OK]
        dev = CollectDevice(arms=arms)
        dev.replies[0x51] = [ERROR_REPLY, image_reply(1), ERROR_REPLY, image_reply(2), ERROR_REPLY, image_reply(3)]
        r = run_collect(dev, *COLLECT, "--count", "3", "--max-attempts", "2")
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(len(r.manifest["failed_attempts"]), 3)

    def test_0412_as_the_first_51_reply_is_a_failed_capture_too(self):
        dev = CollectDevice(arms=[EVENTS_OK, EVENTS_OK])
        dev.replies[0x51] = [NO_CAPTURE, image_reply(1)]
        r = run_collect(dev, *COLLECT, "--count", "1")
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(r.manifest["failed_attempts"][0]["kind"], "no_capture")
        self.assertIn("no capture in progress", r.manifest["failed_attempts"][0]["message"])

    def test_error_reply_is_not_treated_as_not_ready(self):
        # the old behaviour: wait 1 s, re-send 51, get 0x0412 and abort. Now: no plain read, no re-send.
        dev = CollectDevice(arms=[BAD_EVENTS])
        dev.replies[0x51] = [ERROR_REPLY]
        run_collect(dev, *COLLECT, "--count", "1", "--max-attempts", "1")
        reads81_big = [c for c in dev.calls if c[0] == "read" and c[2] == 16384]
        self.assertEqual(len(reads81_big), 1)
        self.assertEqual(dev.opcodes().count(0x51), 1)

    def test_genuine_not_ready_still_uses_the_plain_read_then_resend(self):
        dev = CollectDevice(arms=[EVENTS_OK])
        dev.replies[0x51] = [NOT_READY, image_reply(1)]
        r = run_collect(dev, *COLLECT, "--count", "1")
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(dev.opcodes().count(0x51), 1)  # the plain read got the image

    def test_status_7_without_a_header_is_not_a_retry_status(self):
        dev = CollectDevice(arms=[EVENTS_OK])
        dev.replies[0x51] = [bytes.fromhex("070000000000")]
        r = run_collect(dev, *COLLECT, "--count", "1")
        self.assertEqual(r.rc, capture.EXIT_BAD_REPLY)
        self.assertEqual(dev.opcodes().count(0x51), 1)

    def test_single_image_mode_ends_with_a_clear_message_and_non_zero_exit(self):
        dev = CapDevice(replies={0x51: [ERROR_REPLY]})
        env = Env()
        try:
            out = io.StringIO()
            with fake_usb(dev), contextlib.redirect_stdout(out):
                rc = capture.run(env.args())
            log = env.logs()[-1].read_text()
            raws = [p.read_bytes() for p in env.captures("raw")]
            pgms = env.captures("pgm")
        finally:
            env.close()
        self.assertEqual(rc, capture.EXIT_CAPTURE_FAILED)
        self.assertIn("bad capture (error 2), lift and touch again", log)
        self.assertEqual([w[0] for w in dev.writes()][-2:], [0x04, 0x52])
        self.assertEqual([w[0] for w in dev.writes()].count(0x51), 1)  # not re-sent
        self.assertEqual(raws, [ERROR_REPLY])  # kept for diagnosis
        self.assertEqual(pgms, [])
        self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])


class Append(unittest.TestCase):
    def fill(self, env, count=2, label="fp"):
        r = run_collect(CollectDevice(), "--label", label, "--lift-pause-s", "0", "--count", str(count), env=env)
        self.assertEqual(r.rc, 0, r.out)

    def test_append_continues_numbering_in_the_same_folder_and_manifest(self):
        env = Env()
        try:
            self.fill(env, 2)
            dev = CollectDevice(images=[image_reply(7), image_reply(8), image_reply(9)])
            r = run_collect(dev, *COLLECT, "--count", "3", "--append", env=env)
            self.assertEqual(r.rc, 0, r.out)
            names = sorted(f for f in r.files if f.endswith(".pgm"))
            self.assertEqual(names, [f"fp-0{k}.pgm" for k in range(1, 6)])
            self.assertEqual(r.files["fp-03.raw"], image_reply(7))
            self.assertEqual(r.files["fp-05.raw"], image_reply(9))
            m = r.manifest
            self.assertEqual([e["index"] for e in m["images"]], [1, 2, 3, 4, 5])
            self.assertEqual(m["accepted_count"], 5)
            self.assertEqual(m["requested_count"], 5)
            self.assertEqual(len(m["sessions"]), 2)
            self.assertEqual(m["sessions"][1]["first_index"], 3)
            self.assertEqual(m["sessions"][1]["requested_count"], 3)
            self.assertEqual(m["status"], "complete")
            self.assertIn("[1/3] touch the sensor now", r.out)
            self.assertIn("fp-03", r.out)
        finally:
            env.close()

    def test_without_append_the_label_is_still_refused(self):
        env = Env()
        try:
            self.fill(env, 1)
            dev = CollectDevice()
            r = run_collect(dev, *COLLECT, env=env)
            self.assertEqual(r.rc, capture.EXIT_BAD_INPUT)
            self.assertEqual(dev.calls, [])
        finally:
            env.close()

    def test_append_to_a_label_that_does_not_exist_just_starts_it(self):
        env = Env()
        try:
            r = run_collect(CollectDevice(), *COLLECT, "--count", "2", "--append", env=env)
            self.assertEqual(r.rc, 0)
            self.assertEqual([e["index"] for e in r.manifest["images"]], [1, 2])
        finally:
            env.close()

    def test_append_after_an_aborted_run_numbers_after_the_highest_file(self):
        env = Env()
        try:
            # first run: 2 images, then a timeout on the third touch
            dev = CollectDevice(arms=[EVENTS_OK, EVENTS_OK, [EVENTS_OK[0]]])
            r = run_collect(dev, *COLLECT, "--count", "5", env=env)
            self.assertEqual(r.rc, capture.EXIT_USB_ERROR)
            r = run_collect(CollectDevice(), *COLLECT, "--count", "1", "--append", env=env)
            self.assertEqual(r.rc, 0)
            self.assertIn("fp-03.pgm", r.files)
            self.assertEqual(r.manifest["accepted_count"], 3)
        finally:
            env.close()

    def test_append_reads_a_manifest_from_before_sessions_existed(self):
        env = Env()
        try:
            d = env.dir / "caps" / "old"
            d.mkdir(parents=True)
            for k in (1, 2, 3, 4, 5, 6, 7):
                (d / f"old-{k:02d}.pgm").write_bytes(b"x")
                (d / f"old-{k:02d}.raw").write_bytes(b"x")
            old = {
                "label": "old",
                "tool": "proto/capture.py",
                "started": "2026-10-13T23:16:35-03:00",
                "requested_count": 10,
                "status": "incomplete after 7 accepted image(s): StepFailed: non-zero status 0x0412",
                "info": {"log": "capture-x.log"},
                "images": [{"index": k, "accepted": True, "pgm": f"old-{k:02d}.pgm"} for k in range(1, 8)],
                "finished": "2026-10-13T23:16:45-03:00",
                "accepted_count": 7,
            }
            (d / "manifest.json").write_text(json.dumps(old))
            r = run_collect(
                CollectDevice(), "--label", "old", "--lift-pause-s", "0", "--count", "3", "--append", env=env
            )
            self.assertEqual(r.rc, 0, r.out)
            m = r.manifest
            self.assertEqual([e["index"] for e in m["images"]][-3:], [8, 9, 10])
            self.assertEqual(m["accepted_count"], 10)
            self.assertEqual(len(m["sessions"]), 2)
            self.assertEqual(m["sessions"][0]["requested_count"], 10)
            self.assertIn("old-10.pgm", r.files)
            self.assertEqual(m["failed_attempts"], [])
        finally:
            env.close()

    def test_append_never_overwrites_and_rejects_a_foreign_manifest(self):
        env = Env()
        try:
            d = env.dir / "caps" / "fp"
            d.mkdir(parents=True)
            (d / "manifest.json").write_text(json.dumps({"label": "other", "images": []}))
            r = run_collect(CollectDevice(), *COLLECT, "--append", env=env)
            self.assertEqual(r.rc, capture.EXIT_BAD_INPUT)
            self.assertEqual(json.loads((d / "manifest.json").read_text())["label"], "other")
        finally:
            env.close()


class Redo(unittest.TestCase):
    def test_r_discards_the_last_image_and_retakes(self):
        dev = CollectDevice()
        r = run_collect(dev, "--label", "fp", "--count", "2", interactive=True, answers=["r", "", ""])
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(dev.opcodes()[4:], [0x02, 0x51, 0x04, 0x52] * 3)
        m = r.manifest
        self.assertEqual([e["accepted"] for e in m["images"]], [False, True, True])
        self.assertTrue(m["images"][0]["rejected_by_user"])
        self.assertEqual(m["accepted_count"], 2)
        self.assertIn("fp-01.pgm", r.files)  # nothing deleted: the rejected take stays on disk
        self.assertIn("fp-03.pgm", r.files)
        self.assertIn("lift your finger, then press Enter", r.log + r.out)
        self.assertEqual(r.sleeps, [])

    def test_enter_continues(self):
        r = run_collect(CollectDevice(), "--label", "fp", "--count", "2", interactive=True, answers=["", ""])
        self.assertEqual(r.rc, 0)
        self.assertEqual([e["accepted"] for e in r.manifest["images"]], [True, True])

    def test_explicit_pause_overrides_the_terminal_prompt(self):
        r = run_collect(CollectDevice(), *COLLECT, "--count", "2", interactive=True, answers=[])
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.sleeps, [0.0, 0.0])


class Cli(unittest.TestCase):
    def parse(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return capture.build_parser().parse_args(argv)

    def test_label_rules(self):
        self.assertEqual(self.parse("--label", "ok-1.a_b", "--count", "5").count, 5)
        for bad in ("../x", "a/b", "", ".hidden", "x y"):
            with self.assertRaises(SystemExit, msg=bad):
                capture.main(["--dry-run", "--label", bad])

    def test_count_needs_label_and_a_finger(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["--count", "3"], ["--count", "0", "--label", "x"], ["--label", "x", "--no-finger"]):
                with self.assertRaises(SystemExit, msg=argv):
                    capture.main(["--dry-run", *argv])

    def test_append_and_max_attempts_rules(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["--append"], ["--label", "x", "--max-attempts", "0"]):
                with self.assertRaises(SystemExit, msg=argv):
                    capture.main(["--dry-run", *argv])
        a = self.parse("--label", "x", "--append")
        self.assertTrue(a.append)
        self.assertEqual(a.max_attempts, 3)

    def test_dry_run_describes_the_collection_and_creates_nothing(self):
        env = Env()
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = capture.run(env.args("--dry-run", "--label", "fp", "--count", "4"))
            self.assertEqual(rc, 0)
            self.assertIn("Collection mode", out.getvalue())
            self.assertIn("fp-01.pgm", out.getvalue())
            self.assertFalse((env.dir / "caps").exists())
        finally:
            env.close()


class ImageQuality(unittest.TestCase):
    def test_flat_frame_has_no_coverage(self):
        q = parsers.image_quality(bytes([128]) * 8064)
        self.assertEqual((q["min"], q["max"], q["mean"], q["sd"], q["coverage"]), (128, 128, 128.0, 0.0, 0.0))

    def test_textured_frame_is_covered(self):
        px = bytes(255 if (x // 2 + y // 2) % 2 else 0 for y in range(56) for x in range(144))
        q = parsers.image_quality(px)
        self.assertEqual(q["coverage"], 1.0)
        self.assertGreater(q["sd"], 100)

    def test_half_covered(self):
        px = bytearray([128]) * 8064
        for y in range(56):
            for x in range(72):
                px[y * 144 + x] = 255 if (x // 2 + y // 2) % 2 else 0
        self.assertAlmostEqual(parsers.image_quality(bytes(px))["coverage"], 0.5, places=2)

    def test_real_first_capture_if_present(self):
        path = Path(__file__).resolve().parent / "captures" / "capture-20261013-225024-086552-p925357.pgm"
        if not path.exists():
            self.skipTest("first capture not present")
        px = path.read_bytes()[len(b"P5\n144 56\n255\n") :]
        q = parsers.image_quality(px)
        self.assertEqual((q["min"], q["max"]), (4, 255))
        self.assertGreater(q["coverage"], 0.8)

    def test_helpers(self):
        self.assertEqual(parsers.mean_abs_diff(b"\x00\x10", b"\x04\x10"), 2.0)
        with self.assertRaises(ValueError):
            parsers.mean_abs_diff(b"\x00", b"\x00\x00")
        with self.assertRaises(ValueError):
            parsers.image_quality(b"\x00" * 10)
        self.assertIn("ridge coverage ~50%", parsers.format_quality(dict(min=1, max=2, mean=1.5, sd=0.5, coverage=0.5)))


if __name__ == "__main__":
    unittest.main()
