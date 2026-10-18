#!/usr/bin/env python3
"""Finger-lift experiment for the Synaptics 06cb:0078 (step A2, decision D5 of docs/brief-libfprint-driver.md).

The question: a driver must know when the finger has LIFTED between enrollment touches, and no
finger-up event is known (python-validity, iusearch, ToLiKNoSoV and the ETW show only the type-2
finger event and the type-3 capture events). This guided run tells the user when to touch, keep,
lift or keep off the sensor, and logs EVERY EP 0x83 event and every command reply with a monotonic
timestamp (`t=` seconds since the run started), so the answer comes from the data.

Scenarios (positional arguments, default all four in this order):

  baseline    1   02 with nothing touching; listen --baseline-s (5 s). What arrives on its own?
  lift        2a  capture an image, then "lift now" and listen --lift-s (8 s) while still armed;
                  then 04/52
              2b  capture an image, 04/52 first, then "lift now" and listen --lift-s
  rearm-down  3   capture, 04/52, re-arm 02 with the finger STILL DOWN; listen --rearm-s (5 s).
                  Does the finger event come at once? Then "lift now", listen --rearm-s, 04/52
  rearm-up    4   capture, 04/52, "lift now", wait --gap-s (2 s, listening), re-arm 02 with
                  nothing touching; listen --rearm-s, 04/52. Compared with 3 this tells whether
                  "a finger event right after a re-arm" can serve as a still-down signal

Every image is saved (proto/captures/fingerlift/fingerlift-<time>-p<pid>/<block>-<n>.pgm/.raw) and
compared with the previous one (mean abs pixel difference), so same-touch images can be compared.
If an image-ready event arrives during a listen (e.g. after the re-arm in 3) it is fetched with 51
and saved too. At the end a compact per-scenario summary lists each prompt, command and event with
the time since the prompt (and since the last 02).

Safety:
  * the only writes are capture.py's, through the same guard (capture.check_opcode):
    01 19 75 [06] 02 51 04 52. COMMANDS below is capture.ALLOWED_OPCODES, nothing more. 06 is
    sent at most once, exactly when capture.py would (RomInfo byte 17 == 0); 51 only after an
    image-ready event;
  * an EP 0x83 read timeout is "no event", never an error;
  * 04 and 52 end every armed block and run on every exit path (errors, Ctrl-C, SIGTERM) before
    the interface is released, so the script can simply be run again;
  * no USB reset, no clear_halt, no control transfers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import calibration
import capture
import parsers
import probe

COMMANDS = capture.ALLOWED_OPCODES  # 01 19 75 06 02 51 04 52: the closed command set
DEFAULT_OUT_DIR = capture.DEFAULT_OUT_DIR / "fingerlift"
LISTEN_POLL_MS = 1000  # one EP 0x83 read at most this long, so Ctrl-C is honoured within ~1 s

OFF = "Don't touch the sensor"
PLACE = "Place your finger and keep it there"
LIFT = "Lift your finger now"
KEEP = "Keep your finger on the sensor, do not lift"

SCENARIOS = ("baseline", "lift", "rearm-down", "rearm-up")


@dataclass(frozen=True)
class Op:
    kind: str  # "gate", "prompt", "arm", "capture", "listen", "stop"
    text: str = ""  # prompt text, or the listen phase
    seconds: float = 0.0  # listen duration


@dataclass(frozen=True)
class Block:
    id: str
    scenario: str
    title: str
    question: str
    ops: tuple


@dataclass(frozen=True)
class Durations:
    baseline_s: float = 5.0
    lift_s: float = 8.0
    rearm_s: float = 5.0
    gap_s: float = 2.0


def build_blocks(names, d: Durations) -> list[Block]:
    """The ordered blocks for the chosen scenarios. Pure; the dry run prints exactly this."""
    gate, arm, cap, stop = Op("gate", OFF), Op("arm"), Op("capture"), Op("stop")
    touch = (gate, arm, Op("prompt", PLACE), cap)
    table = {
        "baseline": [
            Block(
                "1-baseline",
                "baseline",
                "armed, nothing touching",
                "what arrives on EP 0x83 after 02 with nothing touching?",
                (gate, arm, Op("listen", "armed, nothing touching", d.baseline_s), stop),
            )
        ],
        "lift": [
            Block(
                "2a-lift-before-stop",
                "lift",
                "lift after the image, still armed",
                "does anything arrive on lift before 04/52?",
                touch + (Op("prompt", LIFT), Op("listen", "lifting, still armed (before 04/52)", d.lift_s), stop),
            ),
            Block(
                "2b-lift-after-stop",
                "lift",
                "lift after the image and 04/52",
                "does anything arrive on lift after 04/52?",
                touch + (stop, Op("prompt", LIFT), Op("listen", "lifting, after 04/52", d.lift_s)),
            ),
        ],
        "rearm-down": [
            Block(
                "3-rearm-down",
                "rearm-down",
                "re-arm with the finger still down",
                "after 02 with the finger kept on, does the finger event come at once? anything on lift?",
                touch
                + (
                    stop,
                    Op("prompt", KEEP),
                    arm,
                    Op("listen", "re-armed, finger still down", d.rearm_s),
                    Op("prompt", LIFT),
                    Op("listen", "lifting, re-armed", d.rearm_s),
                    stop,
                ),
            )
        ],
        "rearm-up": [
            Block(
                "4-rearm-up",
                "rearm-up",
                "re-arm after lifting",
                "after a lift and 02 with nothing touching, does any finger event come? (compare with 3)",
                touch
                + (
                    stop,
                    Op("prompt", LIFT),
                    Op("listen", "lifting, not armed", d.gap_s),
                    Op("prompt", OFF),
                    arm,
                    Op("listen", "re-armed, nothing touching", d.rearm_s),
                    stop,
                ),
            )
        ],
    }
    blocks = []
    for name in names:
        blocks += table[name]
    return blocks


def resolve_scenarios(given) -> list[str]:
    """Positional arguments -> scenario names in the given order, deduplicated; empty or 'all' = all."""
    if not given or "all" in given:
        return list(SCENARIOS)
    out = []
    for name in given:
        if name not in SCENARIOS:
            raise ValueError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)} or all")
        if name not in out:
            out.append(name)
    return out


def opcodes_of(op: Op) -> tuple:
    """Opcodes an op can write (listen: 51 only after an image-ready event)."""
    return {
        "arm": (capture.OP_CAPTURE_PROGRAM,),
        "capture": (capture.OP_IMAGE,),
        "listen": (capture.OP_IMAGE,),
        "stop": (capture.OP_STOP, capture.OP_CLEANUP),
    }.get(op.kind, ())


def describe_op(op: Op, pause_s: float, finger_timeout_ms: int) -> str:
    if op.kind == "gate":
        return (
            f'PROMPT "{op.text}" and wait (Enter on a terminal, else {pause_s:g} s); '
            "then drain EP 0x83 (every stale event logged)"
        )
    if op.kind == "prompt":
        return f'PROMPT "{op.text}"'
    if op.kind == "arm":
        return "WRITE 02 + capture program (18869 bytes), read the reply (status 0 required)"
    if op.kind == "capture":
        return (
            f"READ EP 0x83: finger event (up to {finger_timeout_ms} ms), image-ready event; "
            "WRITE 51 00 20 00 00, read the 8082-byte image, save it"
        )
    if op.kind == "listen":
        return (
            f'LISTEN EP 0x83 for {op.seconds:g} s ("{op.text}"), every event logged; '
            "an image-ready event, if the block has no image yet, is answered with 51 and the image saved"
        )
    if op.kind == "stop":
        return "WRITE 04, then 52 (stop capture, cleanup)"
    raise ValueError(op.kind)


# ------------------------------------------------------------------ log


class MonoLog:
    """probe.ExchangeLog with a monotonic `t=` stamp (seconds since the run started) on every line."""

    def __init__(self, inner):
        self.inner, self.t0 = inner, time.monotonic()
        self.path = inner.path

    def write(self, kind: str, text: str) -> None:
        self.inner.write(kind, f"t={time.monotonic() - self.t0:10.4f} {text}")

    def close(self) -> None:
        self.inner.close()


def claim_run_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    stem = f"fingerlift-{dt.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}-p{os.getpid()}"
    for n in range(1000):
        path = base / (stem if n == 0 else f"{stem}-{n}")
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise FileExistsError(f"no free run directory under {base}")


# -------------------------------------------------------------- session


class LiftSession(capture.Session):
    """capture.Session plus the experiment: every TX, reply and EP 0x83 event becomes a timed record."""

    def __init__(self, dev, log, args, plan, run_dir: Path):
        super().__init__(dev, log, args)
        self.plan = plan
        self.by_name = {s.name: s for s in plan}
        self.image_step = next(s for s in plan if s.kind == "image")
        self.run_dir = run_dir
        self.t0 = time.monotonic()
        self.records: list[dict] = []
        self.outcomes: dict[str, str] = {}
        self.images: list[tuple] = []  # (block id, stem, pixels)
        self.block = None
        self.phase = "setup"
        self.prompt_text, self.prompt_t, self.arm_t = "", None, None
        self.last_note = ""

    # -- records -------------------------------------------------------

    def record(self, what: str, detail: str) -> dict:
        now = time.monotonic()
        rec = {
            "block": self.block.id if self.block else "setup",
            "phase": self.phase,
            "t": round(now - self.t0, 4),
            "prompt": self.prompt_text,
            "since_prompt": None if self.prompt_t is None else round(now - self.prompt_t, 4),
            "since_02": None if self.arm_t is None else round(now - self.arm_t, 4),
            "what": what,
            "detail": detail,
        }
        self.records.append(rec)
        return rec

    def record_event(self, what: str, ev: parsers.Interrupt) -> None:
        code = "" if ev.code is None else f" code {ev.code}"
        self.record(what, f"{ev.raw.hex()} kind {ev.kind}{code} -> {ev.action}")

    # -- capture.Session hooks: same guard and logging, plus a record ------

    def write(self, data: bytes, timeout_ms: int) -> None:
        capture.check_opcode(data)  # refused before anything is recorded or written
        if data[0] == capture.OP_CAPTURE_PROGRAM:
            self.arm_t = time.monotonic()
        self.record("TX", data.hex() if len(data) <= 8 else f"{data[0]:02x} + {len(data) - 1} bytes")
        super().write(data, timeout_ms)

    def _record_reply(self, reply: bytes) -> None:
        st = parsers.status_word(reply)
        self.record("RX", f"{len(reply)} byte(s), status {'n/a' if st is None else f'0x{st:04x}'}")

    def read(self, size: int, timeout_ms: int, what: str):
        reply, ms = super().read(size, timeout_ms, what)
        self._record_reply(reply)
        return reply, ms

    def read_soft(self, size: int, timeout_ms: int):
        got = super().read_soft(size, timeout_ms)
        if got is not None:
            self._record_reply(got[0])
        return got

    def interrupt(self, deadline: float, what: str) -> parsers.Interrupt:
        ev = super().interrupt(deadline, what)
        self.record_event("EV", ev)
        return ev

    # -- ops -------------------------------------------------------------

    def prompt(self, text: str, show: bool = True) -> None:
        if show:
            self.bell()
            print(f"\n  >>> {text} <<<")
        self.prompt_text, self.prompt_t = text, time.monotonic()
        self.log.write("PROMPT", f"[{self.block.id}] {text}")
        self.record("PROMPT", text)

    def gate(self, text: str) -> None:
        """Ask for the start state, wait for the user (Enter, or a fixed pause), then drain EP 0x83."""
        self.phase = "gate"
        self.bell()
        print(f"\n  >>> {text} <<<")
        if capture._interactive():
            try:
                capture._ask("      press Enter to start this step (Ctrl-C stops and cleans up): ")
            except EOFError:
                time.sleep(self.args.pause_s)
        else:
            print(f"      starting in {self.args.pause_s:g} s")
            time.sleep(self.args.pause_s)
        self.prompt(text, show=False)
        for h in self.drain_events():
            self.record_event("DRAIN", parsers.classify_interrupt(bytes.fromhex(h)))

    def arm(self) -> None:
        self.phase = "arm"
        self.image_done = False
        self.block_events = []
        self.exchange(self.by_name["capture program"])

    def note(self, text: str, kind: str = "INFO") -> None:
        self.last_note = text
        self.log.write(kind, f"[{self.block.id}] {text}")
        self.record("NOTE", text)
        print(f"  {'!!! ' if kind == 'WARN' else ''}{text}")

    def capture_image(self) -> bool:
        """Finger event, ready event, 51, save. False (a note, not an exception) if no image came."""
        self.phase = "capture"
        try:
            self.wait_finger(self.args.finger_timeout_ms)
            self.wait_ready(self.args.timeout_ms)
        except capture.CaptureFailed as exc:
            self.note(f"capture failed: {exc.user_message}", "WARN")
            return False
        except capture.StepFailed as exc:  # wait_finger/wait_ready raise it only for an EP 0x83 timeout
            self.note(f"no capture: {exc}", "WARN")
            return False
        return self.fetch_and_save()

    def fetch_and_save(self) -> bool:
        try:
            _, reply, img = self.fetch_image(self.image_step)
        except capture.CaptureFailed as exc:
            self.note(f"capture failed: {exc.user_message}", "WARN")
            return False
        self.image_done = True
        k = sum(1 for b, _, _ in self.images if b == self.block.id) + 1
        stem = f"{self.block.id}-{k}"
        pgm, _ = self.save_image(reply, img, paths=(self.run_dir / f"{stem}.pgm", self.run_dir / f"{stem}.raw"))
        text = f"image {pgm.name}: {parsers.format_quality(parsers.image_quality(img.pixels))}"
        if self.images:
            prev_block, prev_stem, prev_pixels = self.images[-1]
            d = parsers.mean_abs_diff(prev_pixels, img.pixels)
            rel = "same touch" if prev_block == self.block.id else "earlier touch"
            dup = " (near-duplicate)" if d < parsers.DUPLICATE_MEAN_ABS_DIFF else ""
            text += f"; mean abs diff vs {prev_stem} ({rel}) {d:.2f}{dup}"
        self.images.append((self.block.id, stem, img.pixels))
        self.log.write("IMAGE", f"[{self.block.id}] {text}")
        self.record("IMAGE", text)
        return True

    def listen(self, seconds: float, phase: str) -> int:
        """Read EP 0x83 until `seconds` have passed; a timeout is "no event". Returns the event count."""
        self.phase = phase
        self.log.write("LISTEN", f"[{self.block.id}] {phase}: {seconds:g} s on EP 0x{capture.EP_INT:02x}")
        print(f"  listening {seconds:g} s on EP 0x{capture.EP_INT:02x} ({phase}) ...")
        deadline = time.monotonic() + seconds
        count = 0
        while True:
            left_ms = int((deadline - time.monotonic()) * 1000)
            if left_ms <= 0:
                break
            try:
                raw = bytes(self.dev.read(capture.EP_INT, capture.INT_READ_SIZE, timeout=min(left_ms, LISTEN_POLL_MS)))
            except self._usb_core.USBTimeoutError:
                continue
            ev = parsers.classify_interrupt(raw)
            self.log.write(
                "INT",
                f"EP 0x{capture.EP_INT:02x} {len(raw)} byte(s): {raw.hex()} kind {ev.kind} code {ev.code} "
                f"-> {ev.action} ({phase})",
            )
            self.record_event("EV", ev)
            print(f"  event {raw.hex()} ({ev.action})")
            count += 1
            if ev.action == "ready" and self.armed and not self.image_done:
                self.fetch_and_save()
        self.note(f"listen '{phase}' ({seconds:g} s): {count} event(s)")
        return count

    def stop(self) -> None:
        self.phase = "stop"
        self.cleanup_commands(self.plan, image_done=self.image_done)

    def do(self, op: Op) -> bool:
        if op.kind == "gate":
            self.gate(op.text)
        elif op.kind == "prompt":
            self.prompt(op.text)
        elif op.kind == "arm":
            self.arm()
        elif op.kind == "capture":
            return self.capture_image()
        elif op.kind == "listen":
            self.listen(op.seconds, op.text)
        elif op.kind == "stop":
            self.stop()
        else:
            raise ValueError(op.kind)
        return True

    def run_block(self, block: Block, n: int, total: int) -> None:
        self.block = block
        self.prompt_text, self.prompt_t, self.arm_t = "", None, None
        print(f"\n=== [{n}/{total}] {block.id}: {block.title} ===\n  question: {block.question}")
        self.log.write("BLOCK", f"[{block.id}] {block.title}: {block.question}")
        outcome = "complete"
        try:
            for op in block.ops:
                if not self.do(op):
                    outcome = f"INCOMPLETE ({self.last_note}); rest of the block skipped"
                    break
        except BaseException as exc:
            # run() sends 04/52 (or, for a wedged device, does not) on its own exit path.
            self.outcomes[block.id] = f"stopped: {type(exc).__name__}: {exc}"
            raise
        self.outcomes[block.id] = outcome
        if self.armed:  # a block that ended early: close it before the next one
            self.stop()


# ---------------------------------------------------------------- summary


def format_record(r: dict) -> str:
    sp = "     --  " if r["since_prompt"] is None else f"{r['since_prompt']:+8.3f}s"
    line = f"{sp}  {r['what']:<6} {r['detail']}"
    if r["what"] in ("EV", "DRAIN"):
        line += f"   [{r['phase']}]"
        if r["since_02"] is not None:
            line += f" (since 02 {r['since_02']:+.3f}s)"
    if r["what"] == "PROMPT":
        line = f'{sp}  PROMPT "{r["detail"]}"'
    return line


def summary_lines(blocks: list[Block], records: list[dict], outcomes: dict) -> list[str]:
    lines = ["=== Summary: time since the prompt; events also since the last 02 ==="]
    for b in blocks:
        lines.append(f"[{b.id}] {b.title}: {outcomes.get(b.id, 'not run')}")
        lines.append(f"    question: {b.question}")
        for r in records:
            if r["block"] == b.id and r["what"] not in ("RX",):
                lines.append("    " + format_record(r))
    return lines


# --------------------------------------------------------------------- run


def session_args(args, run_dir: Path) -> argparse.Namespace:
    """The attributes capture.Session reads, fixed to capture.py's single-image defaults."""
    ns = argparse.Namespace(**vars(args))
    ns.out_dir = str(run_dir)
    ns.label = None
    ns.no_finger = False
    ns.lift_pause_s = None
    ns.skip_identity_checks = False
    ns.force_module = False
    return ns


def print_dry_run(args, blocks, module, cmd02, inputs) -> None:
    print("dry run: no USB access. Plan, in order:")
    print(f"inputs: module {capture.summarise(module, 0)}")
    print(f"        cmd02  {capture.summarise(cmd02, 0)}")
    print(f"        calibration serial {inputs.serial.hex() or 'n/a'}")
    print("setup: WRITE 01, 19, 75 (RomInfo, query, sensor id; ROM 6/0x14, type 0x2449, serial checked),")
    print("       then 06 + module ONLY if RomInfo byte 17 == 0 (no module loaded yet; same rule as capture.py)")
    for n, b in enumerate(blocks, 1):
        print(f"\n[{n}/{len(blocks)}] {b.id}: {b.title}")
        print(f"    question: {b.question}")
        for k, op in enumerate(b.ops, 1):
            print(f"    {k:2d}. {describe_op(op, args.pause_s, args.finger_timeout_ms)}")
    print(
        "\nCommands this script can send: "
        + " ".join(f"{c:02x}" for c in sorted(COMMANDS))
        + " (capture.ALLOWED_OPCODES); nothing else. 04 and 52 end every armed block and every exit path."
    )
    print(f"Logs: {args.log_dir}/fingerlift-<time>.log (+ .json records)")
    print(f"Images: {args.out_dir}/fingerlift-<time>-p<pid>/<block>-<n>.pgm and .raw")


def run(args) -> int:
    try:
        names = resolve_scenarios(args.scenarios)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return capture.EXIT_BAD_INPUT
    blocks = build_blocks(names, Durations(args.baseline_s, args.lift_s, args.rearm_s, args.gap_s))
    try:
        module, cmd02, inputs = capture.load_inputs(Path(args.dll), Path(args.calibration))
    except (OSError, calibration.CalibrationError) as exc:
        print(f"cannot prepare the inputs: {exc}", file=sys.stderr)
        return capture.EXIT_BAD_INPUT
    plan = capture.build_plan(module, cmd02, finger=True)
    if args.dry_run:
        print_dry_run(args, blocks, module, cmd02, inputs)
        return capture.EXIT_OK

    # Imported late so --dry-run and the tests of the pure parts work without pyusb.
    import usb.core

    log = MonoLog(probe.ExchangeLog(Path(args.log_dir), prefix="fingerlift"))
    run_dir = claim_run_dir(Path(args.out_dir))
    print(f"logging to {log.path}\nimages to {run_dir}")
    log.write("INFO", f"scenarios {names}; images in {run_dir}")
    session = probe.UsbSession(log, detach_kernel_driver=args.detach_kernel_driver, usb_reset=False)
    sess = None
    rc = capture.EXIT_OK
    try:
        failed = session.open()
        if failed is not None:
            return failed
        sess = LiftSession(session.dev, log, session_args(args, run_dir), plan, run_dir)
        sess.prepare(plan, inputs, False, headings=("[setup] queries", "[setup] module"))
        for n, block in enumerate(blocks, 1):
            sess.run_block(block, n, len(blocks))
        if any(o.startswith("INCOMPLETE") for o in sess.outcomes.values()):
            rc = capture.EXIT_CAPTURE_FAILED
    except capture.StepFailed as exc:
        probe.say(log, "ERROR", str(exc))
        rc = exc.rc
    except capture.UnsafeCommand as exc:
        probe.say(log, "ERROR", f"refused to send: {exc}")
        rc = capture.EXIT_CAPTURE_FAILED
    except KeyboardInterrupt:
        probe.say(log, "WARN", "interrupted; cleaning up")
        rc = capture.EXIT_INTERRUPTED
    except usb.core.USBError as exc:
        probe.say(log, "ERROR", f"USB error: {exc!r}{probe.hint_for(exc)}")
        rc = capture.EXIT_USB_ERROR
        if sess is not None and not isinstance(exc, usb.core.USBTimeoutError):
            sess.armed = False  # device gone or wedged: do not keep writing to it
    finally:
        try:
            if sess is not None and sess.armed:
                sess.cleanup_commands(plan, image_done=sess.image_done)
        finally:
            session.close()
            try:
                if sess is not None:
                    report(sess, blocks, log)
            finally:
                log.close()
                print(f"\nlog saved: {log.path}")
    return rc


def report(sess: LiftSession, blocks: list[Block], log: MonoLog) -> None:
    lines = summary_lines(blocks, sess.records, sess.outcomes)
    print()
    for line in lines:
        print(line)
        log.write("SUMMARY", line)
    data = {
        "tool": "proto/fingerlift.py",
        "images": str(sess.run_dir),
        "blocks": [{"id": b.id, "title": b.title, "question": b.question} for b in blocks],
        "outcomes": sess.outcomes,
        "records": sess.records,
    }
    path = Path(log.path).with_suffix(".json")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"records saved: {path}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Finger-lift experiment for Synaptics 06cb:0078: guided touches, every EP 0x83 event logged. "
        "Sends only capture.py's commands (01 19 75 [06] 02 51 04 52)."
    )
    ap.add_argument(
        "scenarios",
        nargs="*",
        metavar="SCENARIO",
        help=f"any of {', '.join(SCENARIOS)}, or all (default: all, in that order)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit; no USB")
    ap.add_argument("--baseline-s", type=float, default=5.0, help="scenario 1 listen time (default 5)")
    ap.add_argument("--lift-s", type=float, default=8.0, help="scenario 2 listen time after 'lift' (default 8)")
    ap.add_argument("--rearm-s", type=float, default=5.0, help="scenarios 3/4 listen time after re-arm (default 5)")
    ap.add_argument("--gap-s", type=float, default=2.0, help="scenario 4 wait between lift and re-arm (default 2)")
    ap.add_argument(
        "--pause-s",
        type=float,
        default=3.0,
        help="without a terminal, the pause at the start of each step instead of Enter (default 3)",
    )
    ap.add_argument(
        "--finger-timeout-ms", type=int, default=20000, help="how long to wait for the finger event (default 20000)"
    )
    ap.add_argument(
        "--timeout-ms", type=int, default=5000, help="timeout per transfer and for the image-ready event (default 5000)"
    )
    ap.add_argument(
        "--image-retries", type=int, default=3, metavar="N", help="'not ready' 51 replies allowed (as capture.py)"
    )
    ap.add_argument("--log-dir", default=str(capture.DEFAULT_LOG_DIR), help="log directory (default proto/logs)")
    ap.add_argument(
        "--out-dir", default=str(DEFAULT_OUT_DIR), help="image directory (default proto/captures/fingerlift)"
    )
    ap.add_argument("--dll", default=str(capture.DEFAULT_DLL), help="vendor DLL holding the capture module")
    ap.add_argument(
        "--calibration", default=str(probe.DEFAULT_CALIBRATION), help="CalibrationData_decrypted.bin of THIS unit"
    )
    ap.add_argument(
        "--detach-kernel-driver",
        action="store_true",
        help="detach a kernel driver from interface 0 if present; re-attached on exit",
    )
    ap.add_argument("--no-bell", action="store_true", help="do not ring the terminal bell at the prompts")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        resolve_scenarios(args.scenarios)
    except ValueError as exc:
        ap.error(str(exc))
    for name in ("baseline_s", "lift_s", "rearm_s", "gap_s", "timeout_ms", "finger_timeout_ms"):
        if getattr(args, name) <= 0:
            ap.error(f"--{name.replace('_', '-')} must be positive")
    if args.pause_s < 0:
        ap.error("--pause-s must be >= 0")
    if args.image_retries < 0:
        ap.error("--image-retries must be >= 0")

    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
