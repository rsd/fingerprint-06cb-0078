"""Offline tests of fingerlift.py against a fake pyusb: no device, no libusb, no real time.

A fake monotonic clock advances only when the fake device "waits" (an EP 0x83 read that times out,
or a scripted silence) and on time.sleep, so the scenario timings are exact and the tests are fast.
Checks the scenario sequencing (exact opcodes per block), that every EP 0x83 event is logged with
its time since the prompt, the closed command set, cleanup on Ctrl-C, and the dry run.
"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import capture
import fingerlift
from synth_template import setUpModule, tearDownModule  # noqa: F401 (unittest module fixtures)
from test_capture_flow import EVENTS_OK, Env, fake_usb
from test_collect_flow import CollectDevice, image_reply
from test_probe_flow import USBTimeoutError

ACK = EVENTS_OK[0]  # 00 00 00 00 00, the start ack after 02
LIFT_EV = bytes.fromhex("02 00 00 00 00")  # a made-up "finger-up" event, only to stage one


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class TimedDevice(CollectDevice):
    """CollectDevice whose EP 0x83 queues may hold floats: seconds of silence before the next item."""

    def __init__(self, clock, **kw):
        super().__init__(**kw)
        self.clock = clock

    def read(self, ep, size, timeout=None):
        if ep != 0x83:
            return super().read(ep, size, timeout)
        self.calls.append(("read", ep, size))
        wait = (timeout or 0) / 1000
        while self.interrupts and isinstance(self.interrupts[0], float):
            gap = self.interrupts[0]
            if gap > wait:
                self.interrupts[0] = gap - wait
                self.clock.now += wait
                raise USBTimeoutError("silence")
            self.interrupts.pop(0)
            self.clock.now += gap
            wait -= gap
        if not self.interrupts:
            self.clock.now += wait
            raise USBTimeoutError("no event")
        item = self.interrupts.pop(0)
        if isinstance(item, BaseException):
            raise item
        self.clock.now += 0.001
        return list(item)


# One 02 per entry, in the order "all" sends them.
ARMS_ALL = [
    [ACK],  # 1 baseline
    EVENTS_OK + [1.5, LIFT_EV],  # 2a: an event 1.5 s into the lift listen
    EVENTS_OK,  # 2b
    EVENTS_OK,  # 3 first touch
    EVENTS_OK,  # 3 re-armed with the finger down: finger + ready at once -> second image
    EVENTS_OK,  # 4 first touch
    [ACK],  # 4 re-armed after lifting: nothing
]

BLOCK_OPCODES = {
    "baseline": [0x02, 0x04, 0x52],
    "lift": [0x02, 0x51, 0x04, 0x52] * 2,
    "rearm-down": [0x02, 0x51, 0x04, 0x52, 0x02, 0x51, 0x04, 0x52],
    "rearm-up": [0x02, 0x51, 0x04, 0x52, 0x02, 0x04, 0x52],
}
SETUP = [0x01, 0x19, 0x75, 0x06]


def args_for(env, *extra):
    argv = [
        *extra,
        "--dll",
        str(env.dir / "fake.dll"),
        "--calibration",
        str(env.dir / "cal.bin"),
        "--log-dir",
        str(env.dir / "logs"),
        "--out-dir",
        str(env.dir / "caps"),
        "--timeout-ms",
        "100",
        "--finger-timeout-ms",
        "2000",
    ]
    args = fingerlift.build_parser().parse_args(argv)
    return args


def run_fl(arms, *extra, stale=None, images=None, interactive=False, answers=None):
    env = Env()
    clock = Clock()
    try:
        dev = TimedDevice(clock, arms=arms, stale=stale, images=images or [image_reply(k) for k in range(1, 11)])
        out = io.StringIO()
        asks = iter(answers or [])

        def ask(prompt):
            item = next(asks, KeyboardInterrupt())
            if isinstance(item, BaseException):
                raise item
            return item

        with (
            fake_usb(dev),
            contextlib.redirect_stdout(out),
            mock.patch.object(fingerlift.time, "monotonic", clock),
            mock.patch.object(fingerlift.time, "sleep", clock.sleep),
            mock.patch.object(capture, "_interactive", lambda: interactive),
            mock.patch.object(capture, "_ask", ask),
        ):
            rc = fingerlift.run(args_for(env, *extra))
        logs = sorted((env.dir / "logs").glob("fingerlift-*.log"))
        jsons = sorted((env.dir / "logs").glob("fingerlift-*.json"))
        runs = sorted((env.dir / "caps").glob("fingerlift-*"))
        files = sorted(p.name for p in runs[0].iterdir()) if runs else []
        return types_ns(
            rc=rc,
            dev=dev,
            out=out.getvalue(),
            log=logs[-1].read_text() if logs else "",
            data=json.loads(jsons[-1].read_text()) if jsons else None,
            files=files,
            clock=clock,
        )
    finally:
        env.close()


def types_ns(**kw):
    import types

    return types.SimpleNamespace(**kw)


def events(data, block, phase=None):
    return [
        r
        for r in data["records"]
        if r["block"] == block and r["what"] == "EV" and (phase is None or r["phase"] == phase)
    ]


class Sequencing(unittest.TestCase):
    def test_all_scenarios_exact_opcodes_and_endpoints(self):
        r = run_fl(ARMS_ALL)
        self.assertEqual(r.rc, 0, r.out)
        expected = SETUP + [op for s in fingerlift.SCENARIOS for op in BLOCK_OPCODES[s]]
        self.assertEqual(r.dev.opcodes(), expected)
        self.assertEqual({c[1] for c in r.dev.calls if c[0] == "write"}, {0x01})
        self.assertEqual({c[1] for c in r.dev.calls if c[0] == "read"}, {0x81, 0x83})
        self.assertEqual(r.dev.calls[-2:], [("release", 0), ("dispose",)])
        self.assertEqual(set(r.dev.names()) - {"write", "read", "claim", "release", "dispose"}, set())
        self.assertEqual(
            set(r.data["outcomes"].values()), {"complete"}, r.data["outcomes"]
        )  # every block ran to its end

    def test_each_scenario_alone(self):
        arms = {
            "baseline": [[ACK]],
            "lift": [EVENTS_OK, EVENTS_OK],
            "rearm-down": [EVENTS_OK, EVENTS_OK],
            "rearm-up": [EVENTS_OK, [ACK]],
        }
        for name in fingerlift.SCENARIOS:
            with self.subTest(name):
                r = run_fl(arms[name], name)
                self.assertEqual(r.rc, 0, r.out)
                self.assertEqual(r.dev.opcodes(), SETUP + BLOCK_OPCODES[name])

    def test_scenarios_run_in_the_given_order(self):
        r = run_fl([[ACK], EVENTS_OK, [ACK]], "baseline", "rearm-up", "baseline")
        self.assertEqual(r.dev.opcodes(), SETUP + BLOCK_OPCODES["baseline"] + BLOCK_OPCODES["rearm-up"])
        self.assertEqual(list(r.data["outcomes"]), ["1-baseline", "4-rearm-up"])

    def test_module_not_resent_when_loaded(self):
        from test_capture_flow import ROM_LOADED

        env_dev_replies = {0x01: ROM_LOADED}
        env = Env()
        clock = Clock()
        try:
            dev = TimedDevice(clock, arms=[[ACK]], replies=env_dev_replies)
            with (
                fake_usb(dev),
                contextlib.redirect_stdout(io.StringIO()),
                mock.patch.object(fingerlift.time, "monotonic", clock),
                mock.patch.object(fingerlift.time, "sleep", clock.sleep),
                mock.patch.object(capture, "_interactive", lambda: False),
            ):
                rc = fingerlift.run(args_for(env, "baseline"))
            self.assertEqual(rc, 0)
            self.assertEqual(dev.opcodes(), [0x01, 0x19, 0x75, 0x02, 0x04, 0x52])
        finally:
            env.close()

    def test_listen_durations_follow_the_options(self):
        r = run_fl([[ACK]], "baseline", "--baseline-s", "7", "--pause-s", "0")
        notes = [x for x in r.data["records"] if x["what"] == "NOTE" and x["detail"].startswith("listen")]
        self.assertEqual(len(notes), 1)
        self.assertIn("(7 s): 1 event(s)", notes[0]["detail"])
        # the listen ran for its full time after the 02
        self.assertAlmostEqual(notes[0]["since_02"], 7.0, places=2)

    def test_blocks_are_pure_and_use_only_known_prompts(self):
        blocks = fingerlift.build_blocks(fingerlift.SCENARIOS, fingerlift.Durations(1, 2, 3, 4))
        self.assertEqual(
            [b.id for b in blocks],
            ["1-baseline", "2a-lift-before-stop", "2b-lift-after-stop", "3-rearm-down", "4-rearm-up"],
        )
        prompts = {op.text for b in blocks for op in b.ops if op.kind in ("gate", "prompt")}
        self.assertEqual(prompts, {fingerlift.OFF, fingerlift.PLACE, fingerlift.LIFT, fingerlift.KEEP})
        listens = [(b.id, op.seconds) for b in blocks for op in b.ops if op.kind == "listen"]
        self.assertIn(("1-baseline", 1), listens)
        self.assertIn(("2a-lift-before-stop", 2), listens)
        self.assertIn(("3-rearm-down", 3), listens)
        self.assertIn(("4-rearm-up", 4), listens)  # the gap between lift and re-arm
        # 2a lifts before 04/52, 2b after
        a = [op.kind for op in blocks[1].ops]
        b = [op.kind for op in blocks[2].ops]
        self.assertLess(a.index("listen"), a.index("stop"))
        self.assertGreater(b.index("listen"), b.index("stop"))

    def test_resolve_scenarios(self):
        self.assertEqual(fingerlift.resolve_scenarios([]), list(fingerlift.SCENARIOS))
        self.assertEqual(fingerlift.resolve_scenarios(["all"]), list(fingerlift.SCENARIOS))
        self.assertEqual(fingerlift.resolve_scenarios(["lift", "lift", "baseline"]), ["lift", "baseline"])
        with self.assertRaises(ValueError):
            fingerlift.resolve_scenarios(["pairing"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fingerlift.main(["bogus", "--dry-run"])


class EventLogging(unittest.TestCase):
    def test_every_event_is_logged_with_monotonic_time(self):
        r = run_fl(ARMS_ALL)
        delivered = sum(sum(1 for x in arm if isinstance(x, bytes)) for arm in ARMS_ALL)
        self.assertEqual(r.log.count(" INT "), delivered)
        self.assertEqual(sum(1 for x in r.data["records"] if x["what"] == "EV"), delivered)
        # every log line after the header carries t=
        body = [ln for ln in r.log.splitlines() if "fingerlift started" not in ln and "log closed" not in ln]
        self.assertTrue(all(" t=" in ln for ln in body), [ln for ln in body if " t=" not in ln][:3])
        self.assertIn("PROMPT", r.log)
        self.assertIn("RX", r.log)
        self.assertIn("SUMMARY", r.log)
        # every command reply is a record too
        rx = [x for x in r.data["records"] if x["what"] == "RX"]
        self.assertEqual(len(rx), len(r.dev.opcodes()))

    def test_lift_event_time_since_prompt(self):
        r = run_fl(ARMS_ALL)
        lift = events(r.data, "2a-lift-before-stop", "lifting, still armed (before 04/52)")
        self.assertEqual([x["detail"].split()[0] for x in lift], [LIFT_EV.hex()])
        self.assertEqual(lift[0]["prompt"], fingerlift.LIFT)
        self.assertAlmostEqual(lift[0]["since_prompt"], 1.5, places=2)
        self.assertRegex(r.out, r"\+1\.50\ds  EV     0200000000")  # shown in the summary

    def test_rearm_finger_down_fetches_a_same_touch_image(self):
        r = run_fl(ARMS_ALL)
        ev = events(r.data, "3-rearm-down", "re-armed, finger still down")
        self.assertEqual([x["detail"].split()[0] for x in ev], [e.hex() for e in EVENTS_OK])
        self.assertEqual(ev[1]["prompt"], fingerlift.KEEP)
        self.assertLess(ev[1]["since_02"], 0.1)  # the finger event came at once
        self.assertIn("3-rearm-down-1.pgm", r.files)
        self.assertIn("3-rearm-down-2.pgm", r.files)
        notes = [x["detail"] for x in r.data["records"] if x["what"] == "IMAGE"]
        self.assertTrue(any("vs 3-rearm-down-1 (same touch)" in n for n in notes), notes)

    def test_rearm_after_lift_shows_no_finger_event(self):
        r = run_fl(ARMS_ALL)
        ev = events(r.data, "4-rearm-up", "re-armed, nothing touching")
        self.assertEqual([x["detail"].split()[0] for x in ev], [ACK.hex()])
        self.assertNotIn("4-rearm-up-2.pgm", r.files)

    def test_images_saved_per_block(self):
        r = run_fl(ARMS_ALL)
        pgms = [f for f in r.files if f.endswith(".pgm")]
        self.assertEqual(
            pgms,
            [
                "2a-lift-before-stop-1.pgm",
                "2b-lift-after-stop-1.pgm",
                "3-rearm-down-1.pgm",
                "3-rearm-down-2.pgm",
                "4-rearm-up-1.pgm",
            ],
        )
        self.assertEqual(len([f for f in r.files if f.endswith(".raw")]), 5)

    def test_drained_events_are_recorded(self):
        # a late event after the 52 of 2a must show up as DRAIN at the start of 2b
        r = run_fl(ARMS_ALL, stale={2: [LIFT_EV]})
        drains = [x for x in r.data["records"] if x["what"] == "DRAIN"]
        self.assertEqual(
            [(x["block"], x["detail"].split()[0]) for x in drains], [("2b-lift-after-stop", LIFT_EV.hex())]
        )
        self.assertIn("DRAIN", r.out)

    def test_summary_lists_every_block(self):
        r = run_fl(ARMS_ALL)
        summary = r.out[r.out.index("=== Summary") :]
        for b in ("1-baseline", "2a-lift-before-stop", "2b-lift-after-stop", "3-rearm-down", "4-rearm-up"):
            self.assertIn(f"[{b}]", summary)
        self.assertIn('PROMPT "Lift your finger now"', summary)
        self.assertIn("since 02", summary)

    def test_prompts_are_printed(self):
        r = run_fl(ARMS_ALL)
        for text in (fingerlift.OFF, fingerlift.PLACE, fingerlift.LIFT, fingerlift.KEEP):
            self.assertIn(f">>> {text} <<<", r.out)

    def test_interactive_gate_waits_for_enter(self):
        r = run_fl([[ACK]], "baseline", interactive=True, answers=[""])
        self.assertEqual(r.rc, 0, r.out)


class FailuresAndCleanup(unittest.TestCase):
    def test_no_finger_marks_the_block_incomplete_and_goes_on(self):
        # 2a never sees a finger; 2b works
        r = run_fl([[ACK], EVENTS_OK], "lift")
        self.assertEqual(r.rc, capture.EXIT_CAPTURE_FAILED)
        self.assertEqual(r.dev.opcodes(), SETUP + [0x02, 0x04, 0x52] + [0x02, 0x51, 0x04, 0x52])
        self.assertTrue(r.data["outcomes"]["2a-lift-before-stop"].startswith("INCOMPLETE"))
        self.assertEqual(r.data["outcomes"]["2b-lift-after-stop"], "complete")

    def test_ctrl_c_mid_listen_cleans_up(self):
        r = run_fl([EVENTS_OK + [2.0, KeyboardInterrupt()]], "lift")
        self.assertEqual(r.rc, capture.EXIT_INTERRUPTED)
        self.assertEqual(r.dev.opcodes(), SETUP + [0x02, 0x51, 0x04, 0x52])
        self.assertEqual(r.dev.calls[-2:], [("release", 0), ("dispose",)])
        self.assertIn("stopped: KeyboardInterrupt", r.data["outcomes"]["2a-lift-before-stop"])
        self.assertIn("=== Summary", r.out)  # the summary is printed anyway

    def test_ctrl_c_at_the_gate_after_an_armed_block_needs_no_extra_cleanup(self):
        r = run_fl([[ACK]], "baseline", "lift", interactive=True, answers=[""])  # second gate: Ctrl-C
        self.assertEqual(r.rc, capture.EXIT_INTERRUPTED)
        self.assertEqual(r.dev.opcodes()[-2:], [0x04, 0x52])
        self.assertEqual(r.dev.calls[-2:], [("release", 0), ("dispose",)])

    def test_bad_status_on_02_stops_the_run_with_cleanup(self):
        env = Env()
        clock = Clock()
        try:
            dev = TimedDevice(clock, arms=[[ACK]], replies={0x02: b"\x91\x04" + bytes(2152)})
            with (
                fake_usb(dev),
                contextlib.redirect_stdout(io.StringIO()),
                mock.patch.object(fingerlift.time, "monotonic", clock),
                mock.patch.object(fingerlift.time, "sleep", clock.sleep),
                mock.patch.object(capture, "_interactive", lambda: False),
            ):
                rc = fingerlift.run(args_for(env))
            self.assertEqual(rc, capture.EXIT_BAD_REPLY)
            self.assertEqual(dev.opcodes(), SETUP + [0x02, 0x04, 0x52])
            self.assertEqual(dev.calls[-2:], [("release", 0), ("dispose",)])
        finally:
            env.close()


class ClosedCommandSet(unittest.TestCase):
    """Like test_parsers.ProbeSafety: the experiment can only ever send capture.py's commands."""

    def test_command_set_is_closed(self):
        self.assertEqual(fingerlift.COMMANDS, frozenset({0x01, 0x19, 0x75, 0x06, 0x02, 0x51, 0x04, 0x52}))
        self.assertIs(fingerlift.COMMANDS, capture.ALLOWED_OPCODES)
        self.assertEqual(capture.EP_OUT, 0x01)
        self.assertEqual(capture.EP_INT, 0x83)

    def test_every_op_stays_inside_the_set(self):
        blocks = fingerlift.build_blocks(fingerlift.SCENARIOS, fingerlift.Durations())
        used = {c for b in blocks for op in b.ops for c in fingerlift.opcodes_of(op)}
        self.assertLessEqual(used, fingerlift.COMMANDS)
        self.assertEqual(
            {op.kind for b in blocks for op in b.ops}, {"gate", "prompt", "arm", "capture", "listen", "stop"}
        )

    def test_a_full_run_writes_nothing_else(self):
        r = run_fl(ARMS_ALL)
        self.assertLessEqual(set(r.dev.opcodes()), fingerlift.COMMANDS)

    def test_session_write_refuses_anything_else(self):
        env = Env()
        try:
            dev = TimedDevice(Clock())
            with fake_usb(dev):
                sess = fingerlift.LiftSession(dev, mock.Mock(), args_for(env), [_image_step()], env.dir)
                for op in (0x4F, 0x1A, 0x50, 0x6F, 0x08, 0x07, 0x00):
                    with self.assertRaises(capture.UnsafeCommand, msg=hex(op)):
                        sess.write(bytes([op, 0, 0]), 100)
            self.assertEqual(dev.writes(), [])
            self.assertEqual(sess.records, [])  # refused before it is even recorded
        finally:
            env.close()


def _image_step():
    return capture.Step("read image", "image", capture.REQ_IMAGE)


class DryRun(unittest.TestCase):
    def test_dry_run_prints_the_plan_without_usb(self):
        env = Env()
        try:
            out = io.StringIO()
            no_usb = {"usb": None, "usb.core": None, "usb.util": None, "usb.backend": None}
            with mock.patch.dict(sys.modules, no_usb), contextlib.redirect_stdout(out):
                rc = fingerlift.run(args_for(env, "--dry-run"))
            text = out.getvalue()
            self.assertEqual(rc, 0)
            for needle in (
                "no USB",
                "1-baseline",
                "2a-lift-before-stop",
                "2b-lift-after-stop",
                "3-rearm-down",
                "4-rearm-up",
                'PROMPT "Lift your finger now"',
                'PROMPT "Place your finger and keep it there"',
                "Don't touch the sensor",
                "LISTEN EP 0x83 for 8 s",
                "LISTEN EP 0x83 for 5 s",
                "LISTEN EP 0x83 for 2 s",
                "WRITE 04, then 52",
                "01 02 04 06 19 51 52 75",
                "byte 17 == 0",
            ):
                self.assertIn(needle, text)
            self.assertFalse((env.dir / "logs").exists())
            self.assertFalse((env.dir / "caps").exists())
        finally:
            env.close()

    def test_dry_run_one_scenario(self):
        env = Env()
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = fingerlift.run(args_for(env, "rearm-up", "--dry-run", "--rearm-s", "6"))
            self.assertEqual(rc, 0)
            self.assertIn("4-rearm-up", out.getvalue())
            self.assertNotIn("1-baseline", out.getvalue())
            self.assertIn("LISTEN EP 0x83 for 6 s", out.getvalue())
        finally:
            env.close()


class Defaults(unittest.TestCase):
    def test_output_dirs_are_gitignored_locations(self):
        args = fingerlift.build_parser().parse_args([])
        here = Path(fingerlift.__file__).resolve().parent
        self.assertEqual(Path(args.out_dir), here / "captures" / "fingerlift")
        self.assertEqual(Path(args.log_dir), here / "logs")
        self.assertEqual((args.baseline_s, args.lift_s, args.rearm_s, args.gap_s), (5.0, 8.0, 5.0, 2.0))


if __name__ == "__main__":
    unittest.main()
