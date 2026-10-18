"""Step A5 at the sensor: does the reader still work after the laptop sleeps (suspend to RAM)?

Run it through tools/dev/a5-suspend.sh, which checks the install (dev, or the package with
`sudo tools/dev/a5-fprintd-dev.sh log on`), the reader and the right to suspend first, and creates the log. It OBSERVES and RECORDS: it never changes the driver or fprintd, and it never
restarts fprintd (that needs sudo). The finger is always the RIGHT INDEX finger (right-index-finger).

Part 1, sleep while idle: the laptop sleeps (`systemctl suspend --no-ask-password`), the user wakes it, unlocks the lock screen
with the password and presses Enter here; then 3 verify rounds (expect match), the a5-verify prompts.
Part 2, sleep during a verify (the lock-screen case): one fprintd-verify waits for a finger, the laptop
sleeps; after the Enter either that check already ended (interrupted by sleep: nothing to touch) or it is
still waiting (touch, up to 30 s for a result); then 2 verify rounds. A round with no sign of fprintd for
30 s is a stuck reader: the test stops there. So does an overheat or an fprintd-verify error in the rounds (or
an overheat during the check in progress): no later check or part runs on that reader.

The sleep never asks for authentication: pam_fprintd serves polkit (and sudo) here, so a polkit prompt would
ask for a FINGER and compete with the verify under test. The wrapper refuses to start when logind answers
CanSuspend "challenge"; the suspend command runs with --no-ask-password, its stdin on /dev/null and in a
session of its own (no controlling terminal). If it exits non-zero or reports an authentication or
permission error, the test says so in one line and stops (no further part). Nothing here calls sudo or
pkexec, and nothing depends on the driver's "Diag image" lines (its --diag dumps may be off).

Resume detection needs no root. First signal: CLOCK_BOOTTIME - CLOCK_MONOTONIC grows by 2 s or more (the
monotonic clock stops while the system sleeps, the boot-time clock does not). Second signal: a line of the
whole system journal (`journalctl -f -n 0 -o cat`) with "PM: suspend exit" (kernel) or "System returned
from sleep" (systemd-sleep). The log records which one came first and when.

The terminal shows only short instructions, prompts and the summary; an fprintd error is named plainly there
(its own text goes to the log). The log gets every client and journal
line, timestamped and tagged with the phase it arrived in (before-suspend, during, after-resume), the
suspend and resume times, the sysfs power state of the 06cb:0078 device before and after each sleep, the
USB kernel lines around each resume (`journalctl -k`) and `systemd-inhibit --list` before each sleep.
tools/test_a5_suspend.py tests it offline with fake commands, an injected clock and an injected Enter key.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import select
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import a5_session as a5

FINGER = "right-index-finger"
DISPLAY = a5.finger_display(FINGER)  # RIGHT INDEX finger
ROUNDS_IDLE = 3  # part 1: verify rounds after the idle sleep
ROUNDS_AFTER = 2  # part 2: verify rounds after the sleep during a verify
COUNTDOWN = 5
VENDOR, PRODUCT = "06cb", "0078"
USB_DEVICES = "/sys/bus/usb/devices"
POWER_FILES = ("power/control", "power/runtime_status", "power/wakeup")
CMD_TIMEOUT_S = 15.0  # systemd-inhibit --list, journalctl -k
KERNEL_LINES_MAX = 400

RESUME_RE = re.compile(r"PM: suspend exit|System returned from sleep")
VERIFY_STARTED_RE = re.compile(r"^Verify started")
ROUND_PROMPT_RE = re.compile(r"^Round \d+/\d+: touch with your ")  # VerifyMachine.round_prompt()
KERNEL_KEEP_RE = re.compile(r"usb|xhci|PM: suspend", re.IGNORECASE)
# The suspend command refused for want of authentication or permission (logind/polkit, systemctl).
SUSPEND_AUTH_RE = re.compile(
    r"Interactive authentication required|Access denied|polkit|not authori[sz]ed|Permission denied", re.IGNORECASE
)
SUSPEND_ARGV = ["suspend", "--no-ask-password"]

PHASES = ("before-suspend", "during", "after-resume")

MSG_NO_SLEEP = "The laptop did not go to sleep."
MSG_WAITING_SLEEP = "Waiting for the laptop to sleep..."
MSG_WELCOME = "Welcome back. Press Enter when you are at this terminal."
MSG_INTERRUPTED = "The check was interrupted by sleep — no need to touch."
MSG_TOUCH = f"TOUCH the sensor with your {DISPLAY}"
MSG_ENDED_NOT_READY = "The check ended before the reader was ready, so this test stops here (details in the log)."
MSG_OVERHEAT_STOP = "The sensor's overheat protection paused it, so this test stops here. Let it rest about 5 minutes."
MSG_SUSPEND_AUTH = "The system asked for authentication to sleep; this test does not support that, so it stops here."
REASON_AUTH = "the system asked for authentication to sleep"
REASON_OVERHEAT = "the sensor's overheat protection paused it; let it rest about 5 minutes"
REASON_CLIENT = "fprintd reported an error"


def wake_instructions() -> list[str]:
    """What the user must know before every sleep."""
    return [
        "The screen will go dark. Wake the laptop with the power button or a key.",
        "The lock screen will appear: unlock it with your password (fingerprint unlock is off there).",
        "Then come back to this terminal and press Enter.",
    ]


def sleep_offset() -> float:
    """Seconds the system has slept since boot: CLOCK_BOOTTIME - CLOCK_MONOTONIC (no root needed)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


def stamp() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]


def find_device(usb_devices: Path) -> Path | None:
    """The sysfs directory of the 06cb:0078 device, or None."""
    try:
        entries = sorted(usb_devices.iterdir())
    except OSError:
        return None
    for d in entries:
        try:
            if (d / "idVendor").read_text().strip() == VENDOR and (d / "idProduct").read_text().strip() == PRODUCT:
                return d
        except OSError:
            continue
    return None


class DetachedProc(a5.Proc):
    """a5.Proc for the suspend command: stdin /dev/null and a session of its own, so it has no controlling
    terminal and nothing can ask a question there (no polkit text agent)."""

    def __init__(self, tag: str, argv: list[str], q: queue.Queue) -> None:
        self.tag = tag
        self.p = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        self.thread = threading.Thread(target=self._pump, args=(q,), daemon=True)
        self.thread.start()


class Keyboard:
    """Enter presses on stdin, read without blocking (select), so the children keep being pumped meanwhile.

    End of input counts as Enter (and stays so). On a terminal, discard() drops what was typed before the
    question (a key pressed to wake the laptop must not answer it)."""

    def __init__(self, stream=None) -> None:
        stream = sys.stdin if stream is None else stream
        try:
            self.fd: int | None = stream.fileno()
        except (AttributeError, OSError, ValueError):
            self.fd = None
        self.buf = b""
        self.eof = self.fd is None

    def discard(self) -> None:
        if self.fd is not None and os.isatty(self.fd):
            import termios

            termios.tcflush(self.fd, termios.TCIFLUSH)
            self.buf = b""

    def pressed(self) -> bool:
        while True:
            if b"\n" in self.buf:
                self.buf = self.buf.split(b"\n", 1)[1]
                return True
            if self.eof:
                return True
            ready, _, _ = select.select([self.fd], [], [], 0)
            if not ready:
                return False
            data = os.read(self.fd, 4096)
            if not data:
                self.eof = True
            self.buf += data


@dataclass
class Commands:
    verify: str = "fprintd-verify"
    journalctl: str = "journalctl"
    systemctl: str = "systemctl"
    inhibit: str = "systemd-inhibit"


@dataclass
class Timing:
    settle_s: float = a5.JOURNAL_SETTLE_S
    countdown_s: float = 1.0  # one step of the 5..1 countdown
    sleep_wait_s: float = 60.0  # no resume this long after the suspend command: it did not sleep
    resume_gap_s: float = 2.0  # growth of boottime - monotonic that proves a sleep
    ready_s: float = 30.0  # part 2: the verify must be started and the sensor armed within this
    result_s: float = 30.0  # part 2: a result after the touch prompt within this
    stuck_s: float = 30.0  # a verify round without any sign of fprintd this long: stuck
    enter_grace_s: float = 1.0  # part 2: after the Enter, lines still on their way are read before deciding


@dataclass
class ClientEnd:
    phase: str
    at: str
    mono: float
    rc: int | None


@dataclass
class Part:
    title: str
    rounds: int
    slept: bool = False
    ran_rounds: bool = False
    as_expected: int = 0
    fail: str | None = None  # why the part failed (None with ran_rounds and every round as expected: PASS)
    in_progress: str | None = None  # part 2: interrupted | survived | ended before sleep | no response | not ready
    result: str | None = None  # part 2, survived: match | no match | error (...)
    stuck: bool = False
    refused: bool = False  # the suspend command failed or wanted authentication: the whole run stops
    abort: str | None = None  # the checks stopped early (REASON_OVERHEAT, REASON_CLIENT): no later check runs

    def passed(self) -> bool:
        return self.fail is None and self.ran_rounds and self.as_expected == self.rounds


class Interrupted(Exception):
    """Ctrl-C (run_verify already told the user)."""


class SuspendSession:
    def __init__(
        self,
        io: a5.Output,
        cmds: Commands,
        timing: Timing | None = None,
        keyboard: Keyboard | None = None,
        usb_devices: str = USB_DEVICES,
        log_path: str = "",
        asleep=sleep_offset,
        mono=time.monotonic,
    ) -> None:
        self.io, self.cmds = io, cmds
        self.t = timing or Timing()
        self.keyboard = keyboard or Keyboard()
        self.usb_devices = Path(usb_devices)
        self.log_path = log_path
        self.asleep, self.mono = asleep, mono
        self.q: queue.Queue = queue.Queue()
        self.procs: dict[str, a5.Proc] = {}
        self.ended: set[str] = set()
        self.machine: a5.VerifyMachine | None = None
        self.loud = False  # part 2: the machine's prompts go to the terminal (after the touch prompt)
        self._reset_cycle()

    # ---------------------------------------------------------------- the pump

    def _reset_cycle(self) -> None:
        self.phase = "before-suspend"
        self.offset0: float | None = None
        self.signals: list[tuple[str, str]] = []  # (signal, time) in arrival order
        self.cmd_rc: int | None = None
        self.cmd_done_at: float | None = None
        self.cmd_auth: str | None = None  # the suspend command's first authentication/permission error line
        self.started = self.armed = False
        self.client_end: ClientEnd | None = None
        self.client_report: list[tuple[str, str, str]] = []  # (time, phase, line): results and errors

    def poll(self, timeout: float = a5.POLL_S) -> None:
        """One step: at most one line (recorded, fed, checked for a resume), then the clock and the command."""
        try:
            tag, line = self.q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            tag, line = None, None
        self._check_clock()
        if tag is not None:
            self._handle(tag, line)

    def wait(self, done, timeout: float | None = None) -> bool:
        """Pump until done() holds (True) or `timeout` seconds pass (False)."""
        end = None if timeout is None else self.mono() + timeout
        while not done():
            if end is not None:
                left = end - self.mono()
                if left <= 0:
                    return False
                self.poll(min(a5.POLL_S, left))
            else:
                self.poll()
        return True

    def _handle(self, tag: str, line: str | None) -> None:
        now = self.mono()
        if line is None:
            self.ended.add(tag)
            proc = self.procs.get(tag)
            rc = proc.p.wait() if proc is not None else None
            self.io.record("note", f"{tag} output ended ({self.phase}), exit {rc}")
            if tag == "verify":
                self.client_end = ClientEnd(self.phase, stamp(), now, rc)
            elif tag == "systemctl":
                self._cmd_done(rc)
            elif tag == "journal" and self.machine is not None and self.loud:
                self.io.say(a5.MSG_JOURNAL_GONE)
            return
        self.io.record(f"{tag}:{self.phase}", line)
        if tag == "systemctl" and self.cmd_auth is None and SUSPEND_AUTH_RE.search(line):
            self.cmd_auth = line
        if tag == "journal":
            if RESUME_RE.search(line):
                self._signal("journal line", line)
            if a5.ACTIVATION_RE.match(line):
                self.armed = True
        if tag == "verify":
            if VERIFY_STARTED_RE.match(line):
                self.started = True
            if a5.VERIFY_RESULT_RE.match(line) or a5.CLIENT_ERROR_RE.search(line):
                self.client_report.append((stamp(), self.phase, line))
        if self.machine is not None and tag in ("journal", "verify"):
            feed = self.machine.feed_journal if tag == "journal" else self.machine.feed_client
            for text in feed(line, now):
                self._prompt(text)

    def _prompt(self, text: str) -> None:
        """A machine prompt: shown only after the touch prompt. Its round result lines ("Round 1/1: match ...")
        stay muted; its touch prompt (again after a stale touch) is shown as MSG_TOUCH, which names the finger."""
        if self.loud and ROUND_PROMPT_RE.match(text):
            self.io.record("muted", text)
            self.io.say(MSG_TOUCH)
        elif self.loud and not text.startswith("Round "):
            self.io.say(text)
        else:
            self.io.record("muted", text)

    def _check_clock(self) -> None:
        if self.offset0 is None or any(s == "clock gap" for s, _ in self.signals):
            return
        gap = self.asleep() - self.offset0
        if gap >= self.t.resume_gap_s:
            self._signal("clock gap", f"CLOCK_BOOTTIME - CLOCK_MONOTONIC grew by {gap:.1f} s (time asleep)")

    def _signal(self, name: str, detail: str) -> None:
        if self.offset0 is None:  # no suspend asked for yet
            self.io.record("note", f"resume-like {name} before the suspend command, ignored: {detail}")
            return
        if any(s == name for s, _ in self.signals):
            return
        at = stamp()
        self.signals.append((name, at))
        self.io.record("resume", f"{name} at {at} ({self.phase}): {detail}")
        if self.phase == "during":
            self.phase = "after-resume"
            self.io.record("resume", f"resume detected first by: {name}")

    def _cmd_done(self, rc: int | None) -> None:
        self.cmd_rc, self.cmd_done_at = rc, self.mono()
        self.io.record("suspend", f"suspend command exit {rc} ({self.phase})")

    # ---------------------------------------------------------------- children

    def _start(self, tag: str, argv: list[str], package: str) -> a5.Proc:
        self.ended.discard(tag)
        proc = a5.start(tag, argv, self.q, self.io, package)
        self.procs[tag] = proc
        return proc

    def _start_detached(self, tag: str, argv: list[str]) -> a5.Proc | None:
        """The suspend command (DetachedProc); None when it could not be started (recorded)."""
        self.ended.discard(tag)
        try:
            proc = DetachedProc(tag, argv, self.q)
        except OSError as e:
            self.io.record("suspend", f"cannot start {argv}: {e!r}")
            return None
        self.procs[tag] = proc
        return proc

    def _stop(self, *tags: str) -> None:
        """Stop these children and read their output to its end (1 s at most)."""
        tags = tuple(t for t in tags if t in self.procs)
        for tag in tags:
            self.procs[tag].stop()
        self.wait(lambda: all(t in self.ended for t in tags), 1.0)
        for tag in tags:
            self.procs.pop(tag, None)

    def stop_all(self) -> None:
        for proc in list(self.procs.values()):
            proc.stop()
        self.procs.clear()

    def open_follow(self) -> None:
        """Follow the whole system journal (the resume lines, the driver's lines) from now on."""
        self.q = queue.Queue()
        self.ended.clear()
        self._reset_cycle()
        self._start("journal", [self.cmds.journalctl, "-f", "-n", "0", "-o", "cat"], "systemd (journalctl)")
        time.sleep(self.t.settle_s)

    # ---------------------------------------------------------------- records for the log

    def record_power(self, when: str) -> None:
        dev = find_device(self.usb_devices)
        if dev is None:
            self.io.record("power", f"{when}: no {VENDOR}:{PRODUCT} device under {self.usb_devices}")
            return
        for name in POWER_FILES:
            try:
                value = (dev / name).read_text().strip()
            except FileNotFoundError:
                continue
            except OSError as e:
                value = f"unreadable ({e.strerror})"
            self.io.record("power", f"{when}: {dev.name} {name} = {value}")

    def _run(self, tag: str, argv: list[str]) -> list[str] | None:
        """Run argv to its end; its output lines, or None when it failed (recorded)."""
        try:
            r = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=CMD_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self.io.record(tag, f"{' '.join(argv)}: could not run it ({e!r})")
            return None
        lines = (r.stdout + r.stderr).splitlines()
        if r.returncode != 0:
            last = lines[-1] if lines else "no output"
            self.io.record(tag, f"{' '.join(argv)}: exit {r.returncode} ({last})")
            return None
        return lines

    def record_inhibitors(self, when: str) -> None:
        lines = self._run("inhibit", [self.cmds.inhibit, "--list", "--no-pager"])
        if lines is not None:
            self.io.record("inhibit", f"{when}: systemd-inhibit --list")
            for line in lines:
                self.io.record("inhibit", line)

    def record_kernel(self, since: datetime, n: int) -> None:
        argv = [self.cmds.journalctl, "-k", "--since", since.strftime("%Y-%m-%d %H:%M:%S"), "--no-pager"]
        argv += ["-o", "short-precise"]
        lines = self._run("kernel", argv)
        if lines is None:
            return
        dev = find_device(self.usb_devices)
        keep = KERNEL_KEEP_RE
        if dev is not None:
            keep = re.compile(rf"{KERNEL_KEEP_RE.pattern}|\b{re.escape(dev.name)}\b", re.IGNORECASE)
        kept = [line for line in lines if keep.search(line)]
        self.io.record("kernel", f"sleep {n}: {len(kept)} USB line(s) of {len(lines)} kernel line(s) since the suspend")
        for line in kept[:KERNEL_LINES_MAX]:
            self.io.record("kernel", line)
        if len(kept) > KERNEL_LINES_MAX:
            self.io.record("kernel", f"... {len(kept) - KERNEL_LINES_MAX} more not kept")

    # ---------------------------------------------------------------- one sleep

    def _suspend_cmd(self) -> None:
        """`systemctl suspend --no-ask-password`, detached from the terminal: it can never raise a polkit prompt
        (pam_fprintd serves polkit here: a prompt would ask for a finger and fight the verify under test)."""
        argv = [self.cmds.systemctl, *SUSPEND_ARGV]
        self.io.record("suspend", f"running: {' '.join(argv)} (stdin /dev/null, own session)")
        if self._start_detached("systemctl", argv) is None:
            self._cmd_done(None)

    def _refusal(self) -> tuple[str, str] | None:
        """(reason, terminal line) when the suspend command failed or wanted authentication, else None."""
        if self.cmd_auth is not None:
            self.io.record(
                "suspend", f"the suspend command reported an authentication/permission error: {self.cmd_auth}"
            )
            return REASON_AUTH, MSG_SUSPEND_AUTH
        if self.cmd_done_at is not None and self.cmd_rc != 0:
            why = "it could not be started" if self.cmd_rc is None else f"exit {self.cmd_rc}"
            return (
                f"the sleep command failed ({why})",
                f"The sleep command failed ({why}), so this test stops here (see the log).",
            )
        return None

    def sleep_cycle(self, lead: str, n: int, part: Part) -> bool:
        """Warn, count down, suspend, detect the resume, wait for Enter. True when the laptop slept.

        A suspend command that fails or wants authentication sets part.refused and part.fail."""
        for text in [lead, *wake_instructions()]:
            self.io.say(text)
        self.record_power(f"before sleep {n}")
        self.record_inhibitors(f"before sleep {n}")
        for i in range(COUNTDOWN, 0, -1):
            self.io.say(f"{i}...")
            self.wait(lambda: False, self.t.countdown_s)
        self.io.say(MSG_WAITING_SLEEP)
        since = datetime.now().astimezone() - timedelta(seconds=2)
        self.phase, self.offset0 = "during", self.asleep()
        cmd_at = stamp()
        self.io.record("suspend", f"sleep {n}: suspend command at {cmd_at} (boottime - monotonic {self.offset0:.3f} s)")
        started = self.mono()
        self._suspend_cmd()
        deadline = started + self.t.sleep_wait_s
        failed = tell = None
        while self.phase == "during":
            now = self.mono()
            refusal = self._refusal()
            if refusal is not None:
                failed, tell = refusal
                part.refused, part.fail = True, failed
                self._stop("systemctl")
                break
            if self.cmd_done_at is not None:
                deadline = max(deadline, self.cmd_done_at + self.t.sleep_wait_s)
            if now >= deadline:
                failed = f"no sign of sleep within {self.t.sleep_wait_s:.0f} s"
                break
            self.poll(min(a5.POLL_S, deadline - now))
        if failed is not None:
            self.io.record("suspend", f"sleep {n}: the laptop did not go to sleep: {failed}")
            self.io.say(tell or MSG_NO_SLEEP)
            self.phase = "before-suspend"
            if part.fail is None:
                part.fail = "the laptop did not go to sleep"
            return False
        first, resumed_at = self.signals[0]
        self.keyboard.discard()
        self.io.say(MSG_WELCOME)
        self.wait(self.keyboard.pressed)
        self.io.record("note", f"sleep {n}: Enter at {stamp()}")
        self.record_power(f"after sleep {n}")
        self.record_kernel(since, n)
        others = ", ".join(f"{s} at {at}" for s, at in self.signals[1:]) or "none yet"
        self.io.record(
            "resume",
            f"sleep {n}: suspend command at {cmd_at}, resume detected at {resumed_at} by {first}; other signals: {others}",
        )
        return True

    # ---------------------------------------------------------------- the parts

    def rounds(self, part: Part) -> None:
        """part.rounds verify rounds, a5-verify's prompts; the tally into `part`."""
        machine = a5.VerifyMachine(finger=FINGER, used=FINGER, expect="match", rounds=part.rounds, quote_errors=False)
        self.io.record("note", f"--- part {part.title}: {part.rounds} verify round(s)")
        rc = a5.run_verify(
            FINGER,
            FINGER,
            "match",
            part.rounds,
            self.cmds.verify,
            self.cmds.journalctl,
            self.io,
            self.t.settle_s,
            stuck_s=self.t.stuck_s,
            machine=machine,
            show_summary=False,
        )
        if rc == 130:
            raise Interrupted
        part.ran_rounds = True
        part.as_expected = machine.counts()["as expected"]
        part.stuck = machine.stuck
        if machine.stuck:
            part.fail = part.fail or "fprintd is not responding (the reader looks stuck)"
        elif machine.abort:
            self.io.record("note", f"part {part.title}: the checks stopped early: {machine.abort}")
            self._abort(part, REASON_OVERHEAT if machine.overheat else REASON_CLIENT)
        elif part.as_expected < part.rounds and part.fail is None:
            part.fail = f"{part.as_expected}/{part.rounds} checks as expected"
        self.io.say(f"Checks: {part.as_expected}/{part.rounds} as expected")

    @staticmethod
    def _abort(part: Part, reason: str) -> None:
        """No later check or part runs; an earlier failure of the part stays first in its reason."""
        part.abort = reason
        part.fail = f"{part.fail}; then {reason}" if part.fail else reason

    def part1(self) -> Part:
        part = Part("1", ROUNDS_IDLE)
        self.io.say(f"Part 1 of 2: the laptop sleeps while the reader is idle, then {ROUNDS_IDLE} checks.")
        try:
            self.open_follow()
            part.slept = self.sleep_cycle(f"The laptop will go to sleep in {COUNTDOWN} seconds.", 1, part)
        finally:
            self._stop("journal", "systemctl")
        if not part.slept:
            return part
        self.io.say(f"Now {ROUNDS_IDLE} checks with your {DISPLAY}.")
        self.rounds(part)
        return part

    def part2(self) -> Part:
        part = Part("2", ROUNDS_AFTER)
        self.io.say("Part 2 of 2: the laptop sleeps while a check waits for your finger (as on the lock screen).")
        self.io.say("Getting the reader ready. Do NOT touch the sensor...")
        try:
            self.open_follow()
            self.machine = a5.VerifyMachine(finger=FINGER, used=FINGER, expect="match", rounds=1, quote_errors=False)
            for text in self.machine.start_round(1):
                self.io.record("muted", text)
            self._start("verify", [self.cmds.verify, "-f", FINGER], "fprintd")
            ready = self.wait(lambda: (self.started and self.armed) or self.client_end is not None, self.t.ready_s)
            if not (ready and self.started and self.armed):
                self.io.record(
                    "note",
                    f"part 2: the reader did not get ready (verify started {self.started}, sensor armed {self.armed}, "
                    f"client ended {self.client_end})",
                )
                if self.client_end is not None:  # fprintd-verify gave up at once (claim error, not enrolled...)
                    self.io.say(MSG_ENDED_NOT_READY)
                    part.fail = "the check ended before the reader was ready"
                else:
                    self.io.say(
                        f"The reader did not get ready within {self.t.ready_s:.0f} s, so this test stops here "
                        "(see the log)."
                    )
                    part.fail = "the reader did not get ready before sleep"
                part.in_progress = "not ready"
                return part
            self.io.record("note", "part 2: verify started and the sensor is armed")
            part.slept = self.sleep_cycle(
                f"Do NOT touch the sensor. The laptop will sleep in {COUNTDOWN} seconds.", 2, part
            )
            if not part.slept:
                return part
            self._in_progress(part)
            if self.machine.overheat:
                self.io.record("note", "part 2: overheat during the check in progress: the later checks are skipped")
                self.io.say(MSG_OVERHEAT_STOP)
                self._abort(part, REASON_OVERHEAT)
                return part
        finally:
            self.loud = False
            self.machine = None
            self._stop("verify", "journal", "systemctl")
        self.io.say(f"Now {ROUNDS_AFTER} more checks with your {DISPLAY}, to see that the reader works again.")
        self.rounds(part)
        return part

    def _report_client(self, what: str) -> None:
        end = self.client_end
        ended = f"ended {end.phase} at {end.at}, exit {end.rc}" if end else "still running"
        self.io.record("part2", f"the check in progress: {what}; fprintd-verify {ended}")
        for at, phase, line in self.client_report:
            self.io.record("part2", f"fprintd-verify reported at {at} ({phase}): {line}")
        if not self.client_report:
            self.io.record("part2", "fprintd-verify reported no result and no error")

    def _in_progress(self, part: Part) -> None:
        """After the resume and the Enter: what became of the check that was waiting when the laptop slept."""
        machine = self.machine
        cur = machine.current
        self.wait(lambda: self.client_end is not None, self.t.enter_grace_s)
        if self.client_end is not None:
            if self.client_end.phase == "before-suspend":
                part.in_progress = "ended before sleep"
                self.io.say("The check had already ended before the laptop slept (see the log).")
            elif self.client_end.phase == "after-resume" and cur.outcome in ("match", "no-match"):
                part.in_progress, part.result = ("survived", cur.outcome.replace("-", " "))
                self.io.say(f"The check gave a result after sleep: {part.result} — no need to touch.")
            else:
                part.in_progress = "interrupted"
                self.io.say(MSG_INTERRUPTED)
            self._report_client(part.in_progress)
            return
        self.loud = True
        self.io.say(MSG_TOUCH)
        got = self.wait(lambda: cur.outcome is not None or self.client_end is not None, self.t.result_s)
        if cur.outcome in ("match", "no-match"):
            part.in_progress, part.result = "survived", cur.outcome.replace("-", " ")
            self.io.say(f"Result: {part.result}")
            # Like a5-verify: let the client end and the finger-off come before the next check.
            self.wait(
                lambda: (
                    self.client_end is not None
                    and (machine.settled() or self.mono() - self.client_end.mono >= a5.LIFT_WAIT_S)
                ),
                a5.LIFT_WAIT_S + 2,
            )
        elif got:
            # fprintd's own words (a result name or an error line) go to the log, a plain phrase to the terminal.
            if cur.detail:
                plain = a5.VERIFY_ERRORS.get(cur.detail, REASON_CLIENT)
            elif self.client_errors_text():
                plain = REASON_CLIENT
            else:
                plain = f"no result, exit {self.client_end.rc}"
            raw = cur.detail or self.client_errors_text() or "no result"
            self.io.record("part2", f"error after resume: {raw}")
            part.in_progress, part.result = "survived", f"error ({plain})"
            self.io.say(f"Result: {a5.MSG_CLIENT_ERROR}" if plain == REASON_CLIENT else f"Result: error ({plain})")
        else:
            self._stop("verify")
            part.in_progress, part.result = "no response", "no response"
            part.fail = "no response after resume"
            self.io.record("part2", f"no response after resume: no result within {self.t.result_s:.0f} s")
            self.io.say(
                f"No response from the reader within {self.t.result_s:.0f} s after sleep; that check was stopped."
            )
        self.loud = False
        self._report_client(part.in_progress)

    def client_errors_text(self) -> str:
        return self.machine.client_errors[0] if self.machine and self.machine.client_errors else ""

    # ---------------------------------------------------------------- summary

    def summary(self, p1: Part, p2: Part | None, why_not: str = "") -> tuple[list[str], int]:
        lines = ["Summary:"]
        if p1.passed():
            lines.append(
                f"Part 1 (sleep while idle): worked after sleep, {p1.as_expected}/{p1.rounds} checks as expected — PASS"
            )
        else:
            lines.append(f"Part 1 (sleep while idle): FAIL ({p1.fail})")
        if p2 is None:
            lines.append(f"Part 2 (sleep during a check): not run ({why_not}) — FAIL")
        else:
            if p2.in_progress == "interrupted":
                lines.append("Part 2 (sleep during a check): the check in progress was interrupted by sleep")
            elif p2.in_progress == "survived":
                lines.append(
                    f"Part 2 (sleep during a check): the check in progress survived sleep (result: {p2.result})"
                )
            elif p2.in_progress == "no response":
                lines.append("Part 2 (sleep during a check): the check in progress did not respond after sleep")
            elif p2.in_progress == "ended before sleep":
                lines.append("Part 2 (sleep during a check): the check ended before the laptop slept")
            if p2.ran_rounds:
                worked = "yes" if p2.as_expected == p2.rounds and not p2.stuck else "no"
                verdict = "PASS" if p2.passed() else f"FAIL ({p2.fail})"
                lines.append(
                    f"Part 2: the reader worked again afterwards: {worked} ({p2.as_expected}/{p2.rounds} as expected)"
                    f" — {verdict}"
                )
            else:
                lines.append(f"Part 2 (sleep during a check): FAIL ({p2.fail})")
        lines.append(f"Log: {self.log_path}")
        return lines, 0 if p1.passed() and p2 is not None and p2.passed() else 1

    def run(self) -> int:
        p2: Part | None = None
        why_not = ""
        try:
            p1 = self.part1()
            if p1.refused:
                why_not = p1.fail
                self.io.record("note", f"the run stops after part 1: {p1.fail}")
            elif not p1.slept:
                why_not = "the laptop did not go to sleep in part 1"
            elif p1.stuck:
                why_not = "the reader looked stuck in part 1"
            elif p1.abort:
                why_not = f"part 1 stopped: {p1.abort}"
                self.io.record("note", f"the run stops after part 1: {p1.abort}")
            else:
                p2 = self.part2()
        except (KeyboardInterrupt, Interrupted):
            self.stop_all()
            self.io.say(f"Interrupted: the suspend test stopped. The log so far: {self.log_path}")
            return 130
        except a5.NotStarted as e:
            self.stop_all()
            self.io.say(str(e))
            return 1
        finally:
            self.stop_all()
        lines, code = self.summary(p1, p2, why_not)
        for text in lines:
            self.io.say(text)
        return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A5 suspend test (use tools/dev/a5-suspend.sh)")
    parser.add_argument("--log", required=True)
    parser.add_argument("--journalctl", default="journalctl")
    parser.add_argument("--fprintd-verify", default="fprintd-verify")
    parser.add_argument("--systemctl", default="systemctl")
    parser.add_argument("--systemd-inhibit", default="systemd-inhibit")
    parser.add_argument("--usb-devices", default=USB_DEVICES)
    parser.add_argument("--settle-s", type=float, default=a5.JOURNAL_SETTLE_S, help=argparse.SUPPRESS)
    parser.add_argument("--countdown-s", type=float, default=Timing.countdown_s, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    cmds = Commands(args.fprintd_verify, args.journalctl, args.systemctl, args.systemd_inhibit)
    timing = Timing(settle_s=args.settle_s, countdown_s=args.countdown_s)
    with a5.open_log(args.log) as log:
        session = SuspendSession(
            a5.Output(sys.stdout, log), cmds, timing, usb_devices=args.usb_devices, log_path=args.log
        )
        return session.run()


if __name__ == "__main__":
    sys.exit(main())
