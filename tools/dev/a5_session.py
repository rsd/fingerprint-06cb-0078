"""Step A5 at the sensor: enroll and verify through the system fprintd with short, clear prompts.

Run it through tools/dev/a5-enroll.sh and tools/dev/a5-verify.sh, which check the install first (the
dev install, or the package with `sudo tools/dev/a5-fprintd-dev.sh log on`) and create the log. This module starts the fprintd client (fprintd-enroll or fprintd-verify) and
`journalctl -u fprintd -f -n 0 -o cat`, merges both line streams in arrival order and turns them into
prompts. The terminal shows ONLY the prompts and the final result; every client line, every journal
line and every prompt goes to the log with a timestamp (the log holds the unit serial: 0600).

What the prompts are driven by:

* fprintd-enroll prints "Enroll result: <name>" and fprintd-verify "Verify result: <name> (done|not done)"
  (fprintd 1.94.5, g_print, flushed per line).
* The driver's journal lines (GLib domain libfprint-synaptics0078; the dev drop-in's G_MESSAGES_DEBUG puts
  them in the journal), matched on their stable prefix:
    L6 "Image captured ..."                        a touch was taken          -> "Got it. LIFT your finger now"
    L1/L2 "Finger off after N probe(s), M ms ..."  finger off reported        -> "Lifted. Touch again"
    L3 "Same touch as the last capture ..."        nothing submitted          -> lift, then touch again
    E2/L4 "Finger-off probe N: finger still down" / "...: same touch", L5 "Finger still down for ..."
                                                   still-down evidence        -> one note after 5 s
    E3 "Capture failed: ..."                       the touch failed (retry)
    "Activation: ..."                              the sensor is armed for an action
* fprintd 1.94.5 runs a duplicate-check identify BEFORE the enroll stages. It takes a touch of its own and
  prints nothing on stdout. The enroll prompts start with a "first touch" that is announced as not one of
  the stages; the stage count only moves on "enroll-stage-passed". When fprintd skips the check (the first
  touch already passes a stage) the count follows the stage results and the user is told.
* Every touch prompt, and the final line, names the finger being enrolled (or touched with, for verify):
  a print saved under the wrong finger's name is the mistake these prompts exist to prevent.
* Overheat: libfprint cancels the action with FP_DEVICE_ERROR_TOO_HOT ("Device disabled to prevent
  overheating."). fprintd 1.94.5 logs "Device reported an error during <action>: <message>" to the journal and
  the client gets enroll-unknown-error / verify-unknown-error. Any line containing "overheat" marks it.

The machines (EnrollMachine, VerifyMachine) are pure: lines and a clock in, prompt lines out. The runner below
them is thin. tools/test_a5_session.py tests both offline with synthetic lines and fake commands.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

# The driver's SYNAPTICS0078_ENROLL_STAGES (libfprint/drivers/synaptics0078.c); a test checks they match.
ENROLL_STAGES = 18
STILL_DOWN_NOTE_S = 5.0  # after LIFT, still-down evidence for longer than this gets one note per touch
WAIT_SENSOR_NOTE_S = 30.0  # enroll: no sign of the armed sensor for this long gets one note
JOURNAL_SETTLE_S = 0.5  # journalctl -f needs a moment before it follows the tail
ENROLL_GRACE_S = 1.0  # journal lines still read after fprintd-enroll exits (overheat, finger off)
VERIFY_GRACE_S = 0.5  # the same after each fprintd-verify
LIFT_WAIT_S = 3.0  # verify: after a result, wait up to this long for the finger-off report
POLL_S = 0.2
ROUNDS_MAX = 50

FINGERS = (
    "left-thumb",
    "left-index-finger",
    "left-middle-finger",
    "left-ring-finger",
    "left-little-finger",
    "right-thumb",
    "right-index-finger",
    "right-middle-finger",
    "right-ring-finger",
    "right-little-finger",
)
DEFAULT_FINGER = "right-index-finger"

# Journal lines (bare messages, `journalctl -o cat`), matched on the driver's stable prefixes.
TAKEN_RE = re.compile(r"^Image captured\b")  # L6
OFF_RE = re.compile(r"^Finger off after (\d+) probe\(s\), (\d+) ms")  # L1, L2
STALE_RE = re.compile(r"^Same touch as the last capture\b")  # L3
STILL_RE = re.compile(r"^(?:Finger-off probe \d+: (?:finger still down|same touch)|Finger still down for \d+ s)")
CAPTURE_FAILED_RE = re.compile(r"^Capture failed:")  # E3
ACTIVATION_RE = re.compile(r"^Activation:")
SCORE_RE = re.compile(r"^sigfm score (-?\d+)/(-?\d+)")
OVERHEAT_RE = re.compile(r"overheat", re.IGNORECASE)

ENROLL_RESULT_RE = re.compile(r"^Enroll result: (\S+)")
VERIFY_RESULT_RE = re.compile(r"^Verify result: (\S+) \((done|not done)\)")
# Client lines worth quoting when the client stops without a result (claim/start failures, polkit).
CLIENT_ERROR_RE = re.compile(r"failed|Impossible|Error|not enrolled|No devices", re.IGNORECASE)

# Retry kinds, shared by enroll-* and verify-* results.
RETRY_TEXT = {
    "retry-scan": "the sensor could not read it",
    "swipe-too-short": "too short; keep your finger still until LIFT",
    "finger-not-centered": "not centred; cover the middle of the sensor",
    "remove-and-retry": "lift your finger completely first",
    "too-fast": "too fast; keep your finger still until LIFT",
}

MSG_LIFT = "Got it. LIFT your finger now"
MSG_LIFT_FAILED = "LIFT your finger now"
MSG_LIFTED = "Lifted. Touch again"
MSG_AGAIN = "Lift your finger, then touch again"
MSG_STALE = "The sensor still sees the last touch: lift your finger, then touch again"
MSG_STILL = "Still detecting a finger; if your finger is off, just touch again"
MSG_WAIT_SENSOR = "Still waiting for the sensor. If a password dialog is open, answer it"
MSG_JOURNAL_GONE = "The journal reader stopped: LIFT and Lifted prompts may be missing (see the log)"
MSG_STUCK = "fprintd is not responding (the reader looks stuck). Restarting it needs sudo, so this test stops here."
MSG_CLIENT_ERROR = "fprintd reported an error (details in the log)"  # VerifyMachine(quote_errors=False)
OVERHEAT_NEXT = "let the sensor rest about 5 minutes, then run this again"


def journal_kind(line: str) -> str:
    """The prompt-relevant kind of a journal line: taken, off, stale, still, failed, activation, score,
    overheat or other."""
    if OVERHEAT_RE.search(line):
        return "overheat"
    for kind, regex in (
        ("taken", TAKEN_RE),
        ("off", OFF_RE),
        ("stale", STALE_RE),
        ("still", STILL_RE),
        ("failed", CAPTURE_FAILED_RE),
        ("activation", ACTIVATION_RE),
        ("score", SCORE_RE),
    ):
        if regex.match(line):
            return kind
    return "other"


def still_note_due(machine, now: float) -> bool:
    """Still-down evidence earns the one note: more than still_note_s after a LIFT prompt (no finger-off since),
    or after an L3 prompt (the sensor still sees the last touch)."""
    if machine.still_noted:
        return False
    if machine.touch == "taken" and not machine.lifted and machine.taken_at is not None:
        return now - machine.taken_at > machine.still_note_s
    return machine.stale and machine.stale_at is not None and now - machine.stale_at > machine.still_note_s


def finger_label(finger: str) -> str:
    """'right-index-finger' -> 'right-index', 'right-thumb' -> 'right-thumb' (the --finger-used form)."""
    return finger.removesuffix("-finger")


def parse_finger_used(label: str) -> str | None:
    """'right-middle' or 'right-middle-finger' -> 'right-middle-finger'; None when not a finger name."""
    label = label.strip().lower()
    for finger in FINGERS:
        if label in (finger, finger_label(finger)):
            return finger
    return None


def finger_display(finger: str) -> str:
    """'right-middle-finger' -> 'RIGHT MIDDLE finger', 'left-thumb' -> 'LEFT THUMB'."""
    side, digit = finger_label(finger).split("-", 1)
    text = f"{side} {digit}".upper()
    return text if digit == "thumb" else f"{text} finger"


def retry_message(kind: str) -> str:
    return f"That touch did not count: {RETRY_TEXT.get(kind, 'the sensor asks for another touch')}"


def verify_retry_message(kind: str, used: str = DEFAULT_FINGER) -> str:
    """The retry line of a verify round, which also asks for the next touch (with the finger `used`)."""
    again = f"touch again with your {finger_display(used)}"
    if kind == "remove-and-retry":
        return f"That touch did not count: lift your finger completely, then {again}"
    return f"{retry_message(kind)}. Lift your finger, then {again}"


# ------------------------------------------------------------------ enroll


# Final lines; {again} is the command that enrolls the same finger again.
ENROLL_FINAL = {
    "enroll-failed": "Enrollment failed: fprintd could not finish or store the print. Run again: {again}",
    "enroll-data-full": "Enrollment failed: the device storage is full. Delete prints (fprintd-delete), then: {again}",
    "enroll-disconnected": (
        "Enrollment failed: the sensor stopped answering. Check tools/dev/a5-fprintd-dev.sh status, then: {again}"
    ),
    "enroll-unknown-error": "Enrollment failed with an unknown fprintd error. See the log, then run again: {again}",
    "enroll-duplicate": (
        "Stopped: this finger matches a print that is already enrolled, maybe one saved under another finger's "
        "name (fprintd-list shows the names). Re-enroll that name with its own finger first "
        "(tools/dev/a5-enroll.sh <that name> --replace), then: {again}"
    ),
}


@dataclass
class EnrollMachine:
    """Turns fprintd-enroll stdout and the driver's journal lines into prompts.

    feed_client / feed_journal / tick take the line and a monotonic time and return the prompt lines to show.
    finish(rc) returns the final lines and the exit code (0 only on enroll-completed).

    The two streams can arrive in either order, so touches and results are matched by count, not by order:
    in the enroll phase every touch the journal reports (L6, or E3 for a failed capture) gets exactly one
    stdout result (stage passed or a retry kind). A LIFT prompt is shown for whichever of the pair comes
    first; the next touch prompt is shown once the finger is off AND every touch has its result, so the count
    in it is always the number of stages fprintd has confirmed, plus one.
    """

    finger: str = DEFAULT_FINGER
    total: int = ENROLL_STAGES
    still_note_s: float = STILL_DOWN_NOTE_S
    wait_note_s: float = WAIT_SENSOR_NOTE_S
    check_window_s: float = 2.0  # see _touch_result: a stage result this soon after the check's finger-off
    doubt_s: float = 1.5  # see _touch_result: how long a doubtful result waits for the journal to decide it
    settle_s: float = 2.0  # after a finger-off, wait this long at most for a missing result (see tick)
    stages: int = 0  # enroll-stage-passed results (enroll-completed sets the total)
    phase: str = "check"  # check (fprintd's duplicate-check identify) -> enroll -> done
    touches: int = 0  # enroll phase: touches reported by the journal (L6, E3)
    results: int = 0  # enroll phase: stdout results for touches (stage passed, retry kinds)
    ready: bool = False  # the first touch prompt was shown
    started_at: float | None = None
    wait_noted: bool = False
    touch: str = "wait"  # wait for a touch | taken (LIFT shown)
    taken_at: float | None = None
    lifted: bool = False  # finger-off seen after the touch was taken
    lifted_at: float | None = None
    retry: bool = False  # the current touch failed (E3 or a retry result)
    stale: bool = False  # L3 shown, waiting for a finger-off
    stale_at: float | None = None
    still_noted: bool = False
    check_off_at: float | None = None  # when the check touch's finger-off moved us to the enroll phase
    reactivated: bool = False  # an Activation came after check_off_at: the enroll itself started
    doubtful_at: float | None = None  # a stage result in the check window, its touch not yet known (_touch_result)
    forgiven: int = 0  # results tick() stopped waiting for; a late one is absorbed, not a new touch
    outcome: str | None = None
    overheat: bool = False
    retries: int = 0
    check_done: bool | None = None  # True: a touch went to the duplicate check; False: fprintd skipped it
    client_errors: list[str] = field(default_factory=list)
    unknown_results: list[str] = field(default_factory=list)

    def display(self) -> str:
        return finger_display(self.finger)

    def again(self) -> str:
        """The command that enrolls this finger again."""
        return f"tools/dev/a5-enroll.sh {self.finger}"

    def touch_prompt(self) -> str:
        if self.phase == "check":
            return (
                f"First touch with your {self.display()}. fprintd first checks that this finger is not enrolled "
                f"yet (this touch is not one of the {self.total})"
            )
        return f"Touch {self.stages + 1} of {self.total} with your {self.display()} (vary the position a little)"

    def skip_note(self) -> str:
        return f"(fprintd skipped its check: the first touch counted as touch 1 of {self.total})"

    def start(self, now: float) -> list[str]:
        """Nothing yet: the first prompt waits for the armed sensor (after a possible password dialog)."""
        self.started_at = now
        return []

    def _ready(self) -> list[str]:
        if self.ready or self.phase == "done":
            return []
        self.ready = True
        return [self.touch_prompt()]

    def _take(self, now: float, text: str) -> list[str]:
        """The current touch is over: show `text` (a LIFT prompt) once per touch."""
        out = self._ready()
        if self.touch == "wait":
            self.touch, self.taken_at, self.lifted = "taken", now, False
            self.still_noted = self.stale = False
            out.append(text)
        return out

    def _new_touch(self) -> list[str]:
        self.touch, self.taken_at, self.lifted = "wait", None, False
        self.retry = self.still_noted = False
        return [self.touch_prompt()]

    def _settled(self) -> bool:
        """Every enroll touch has its result: the stage count is final for now."""
        return self.results >= self.touches

    def _after_off(self, now: float) -> list[str]:
        """The finger-off of a taken touch: the next touch prompt, once its count is known."""
        if self.phase == "check":
            # The duplicate-check identify reports nothing on stdout: its touch ends at the finger-off.
            if not self.retry:
                self.phase, self.check_done, self.check_off_at = "enroll", True, now
            return self._new_touch()
        if self._settled():
            return self._new_touch()
        return []  # a result is still on its way: the prompt comes with it

    def feed_journal(self, line: str, now: float) -> list[str]:
        kind = journal_kind(line)
        if kind == "overheat":
            self.overheat = True
            return []
        if self.phase == "done":
            return []
        if kind == "activation":
            if self.check_off_at is not None:
                # After a real duplicate check fprintd starts the enroll: the driver activates again.
                self.reactivated = True
            if self.doubtful_at is not None:
                # The check was real: the doubtful result is enroll touch 1's, its L6 still on its way.
                self.doubtful_at = None
                self.results += 1
                return self._take(now, MSG_LIFT)
            if self.touch == "taken" and not self.lifted:
                # The last action ended without a finger-off (a retry ends an identify at once): start over.
                return [MSG_AGAIN, *self._new_touch()]
            return self._ready()
        out = []
        if kind in ("taken", "failed", "stale") and self.doubtful_at is not None:
            # A touch without the enroll's Activation first: fprintd skipped its check.
            out = self._check_skipped(prompt=False)
        return out + self._feed_journal_kind(kind, now)

    def _feed_journal_kind(self, kind: str, now: float) -> list[str]:
        if kind in ("taken", "failed"):
            text = MSG_LIFT if kind == "taken" else MSG_LIFT_FAILED
            if kind == "failed":
                self.retry = True
            if self.phase == "check":
                return self._take(now, text)
            self.touches += 1
            self.check_off_at, self.reactivated = None, False
            # A result that came first already showed the LIFT prompt for this touch.
            return self._take(now, text) if self.touches > self.results else []
        if kind == "stale":
            out = self._ready()
            if not self.stale:
                self.touch, self.taken_at, self.lifted, self.still_noted = "wait", None, False, False
                self.stale, self.stale_at = True, now
                out.append(MSG_STALE)
            return out
        if kind == "off":
            if self.stale:
                self.stale = False
                return [MSG_LIFTED, self.touch_prompt()]
            if self.touch == "taken" and not self.lifted:
                self.lifted, self.lifted_at = True, now
                return [MSG_LIFTED, *self._after_off(now)]
            return []
        if kind == "still" and still_note_due(self, now):
            self.still_noted = True
            return [MSG_STILL]
        return []

    def feed_client(self, line: str, now: float) -> list[str]:
        if OVERHEAT_RE.search(line):
            self.overheat = True
        m = ENROLL_RESULT_RE.match(line)
        if not m:
            if CLIENT_ERROR_RE.search(line):
                self.client_errors.append(line.strip())
            return []
        name = m.group(1)
        if self.phase == "done":
            return []
        kind = name.removeprefix("enroll-")
        if name == "enroll-stage-passed" or kind in RETRY_TEXT:
            return self._touch_result(name, kind, now)
        if name == "enroll-completed":
            out = self._take(now, MSG_LIFT)
            self.stages, self.phase, self.outcome = self.total, "done", name
            return out
        if name in ENROLL_FINAL:
            self.phase, self.outcome = "done", name
            return []
        self.unknown_results.append(name)
        return []

    def _check_skipped(self, prompt: bool) -> list[str]:
        """The doubtful result was the "check" touch's: fprintd skipped its check, and that touch was enroll
        touch 1. With `prompt`, the next touch prompt (with the corrected count) follows the note."""
        self.doubtful_at = None
        self.check_done, self.check_off_at = False, None
        return [self.skip_note(), self.touch_prompt()] if prompt else [self.skip_note()]

    def _touch_result(self, name: str, kind: str, now: float) -> list[str]:
        if self.doubtful_at is not None:
            # A second result without the enroll's Activation in between: the first was the "check" touch's.
            return self._check_skipped(prompt=False) + self._touch_result(name, kind, now)
        passed = name == "enroll-stage-passed"
        if passed:
            self.stages = min(self.stages + 1, self.total - 1)  # the last stage reports enroll-completed
        else:
            self.retries += 1
        note = self.skip_note()
        if self.phase == "check":
            if not passed:  # a retry during the duplicate check: that touch is repeated
                self.retry = True
                return [*self._take(now, MSG_LIFT_FAILED), retry_message(kind)]
            # No duplicate check: the first touch was enroll touch 1.
            self.phase, self.check_done = "enroll", False
            self.touches = self.results = 1 if self.touch == "taken" else 0
            if self.touches == 0:
                self.results = 1  # its L6 is still on its way
            out = [*self._take(now, MSG_LIFT), note]
            return out + (self._new_touch() if self.lifted else [])
        if (
            passed
            and self.touches == 0
            and self.check_off_at is not None
            and not self.reactivated
            and now - self.check_off_at <= self.check_window_s
        ):
            # Doubtful. Either the "check" touch was really enroll touch 1 and its result came after its
            # finger-off (fprintd skipped the check), or the check was real and this is a quick enroll touch 1
            # whose result beat the enroll's Activation and L6 in a lagging journal: the finger may be down.
            # The journal decides: an Activation proves the check (feed_journal: LIFT), a touch line or result
            # without one, or doubt_s of silence, proves the skip (note and the corrected touch prompt).
            self.doubtful_at = now
            return []
        if self.results >= self.touches and self.forgiven > 0:
            # The late result of a touch tick() stopped waiting for: no new touch, no LIFT prompt; when it
            # passed and the next touch has not started, show the corrected count.
            self.forgiven -= 1
            return [self.touch_prompt()] if passed and self.touch == "wait" and not self.stale else []
        self.results += 1
        out = []
        if self.results > self.touches:  # the touch's L6/E3 is still on its way
            out += self._take(now, MSG_LIFT if passed else MSG_LIFT_FAILED)
        if not passed:
            self.retry = True
            out.append(retry_message(kind))
        if self.lifted and self.touch == "taken" and self._settled():
            out += self._new_touch()
        return out

    def tick(self, now: float) -> list[str]:
        if self.doubtful_at is not None and self.phase != "done" and now - self.doubtful_at > self.doubt_s:
            return self._check_skipped(prompt=True)
        if (
            self.phase == "enroll"
            and self.touch == "taken"
            and self.lifted
            and not self._settled()
            and self.lifted_at is not None
            and now - self.lifted_at > self.settle_s
        ):
            # A result never came for a touch (or was taken for another one): do not hold the user up.
            self.forgiven += self.touches - self.results
            self.results = self.touches
            return self._new_touch()
        if (
            not self.ready
            and not self.wait_noted
            and self.phase != "done"
            and self.started_at is not None
            and now - self.started_at > self.wait_note_s
        ):
            self.wait_noted = True
            return [MSG_WAIT_SENSOR]
        return []

    def finish(self, rc: int | None) -> tuple[list[str], int]:
        """The final line (what happened, what to do next) and the exit code."""
        if self.outcome == "enroll-completed":
            nxt = f"tools/dev/a5-verify.sh --finger-used {finger_label(self.finger)} --expect match --finger {self.finger}"
            done = f"Done: your {self.display()} is enrolled as {self.finger} ({self.total} of {self.total} touches)"
            return [f"{done}. Next: {nxt}"], 0
        lead = f"Your {self.display()} is NOT enrolled. "
        if self.overheat:
            why = "The sensor's overheat protection paused it; let it rest about 5 minutes"
            return [f"{lead}{why}, then: {self.again()}"], 1
        if self.outcome in ENROLL_FINAL:
            return [lead + ENROLL_FINAL[self.outcome].format(again=self.again())], 1
        if self.client_errors:
            return [f"{lead}fprintd-enroll stopped before finishing: {self.client_errors[0]}. See the log"], 1
        why = f"exit {rc}" if rc is not None else "no exit code"
        return [f"{lead}fprintd-enroll stopped without a result ({why}). See the log, then: {self.again()}"], 1


# ------------------------------------------------------------------ verify


VERIFY_ERRORS = {
    "verify-disconnected": "the sensor stopped answering",
    "verify-unknown-error": "unknown fprintd error",
}


@dataclass
class Round:
    index: int
    outcome: str | None = None  # match | no-match | error
    detail: str = ""  # the fprintd result name, or why the round is an error
    retries: int = 0
    scores: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class VerifyMachine:
    """Turns fprintd-verify stdout and the driver's journal lines into prompts, round by round, and tallies.

    start_round(i) opens a round; feed_client / feed_journal return prompt lines; end_round(rc) closes it
    (a round without a "(done)" result is an error); summary() returns the table and the exit code.
    """

    finger: str = DEFAULT_FINGER  # the enrolled finger fprintd-verify checks against
    used: str = "right-index-finger"  # the finger the user touches with
    expect: str = "match"
    rounds: int = 10
    still_note_s: float = STILL_DOWN_NOTE_S
    done: list[Round] = field(default_factory=list)
    current: Round | None = None
    touch: str = "wait"
    taken_at: float | None = None
    lifted: bool = False
    stale: bool = False
    stale_at: float | None = None
    still_noted: bool = False
    fails: int = 0  # "Capture failed" lines this round (each one is paired with a retry result)
    overheat: bool = False
    abort: str | None = None
    client_errors: list[str] = field(default_factory=list)
    alive: bool = False  # this round showed a sign of fprintd: the sensor armed (Activation), a touch or a result
    stuck: bool = False  # run_verify(stuck_s=...) gave up on a round without any sign of fprintd
    # False: the lines it returns (and abort) name a client error plainly (MSG_CLIENT_ERROR) instead of quoting
    # fprintd's own text; that text still reaches the log (the client line, cur.detail in score_log).
    quote_errors: bool = True

    def round_prompt(self) -> str:
        assert self.current is not None
        return f"Round {self.current.index}/{self.rounds}: touch with your {finger_display(self.used)}, then lift"

    def start_round(self, index: int) -> list[str]:
        self.current = Round(index)
        self.touch, self.taken_at, self.lifted, self.stale, self.still_noted = "wait", None, False, False, False
        self.fails = 0
        self.client_errors = []
        self.alive = False
        return [self.round_prompt()]

    def _take(self, now: float, text: str) -> list[str]:
        if self.touch == "wait":
            self.touch, self.taken_at, self.still_noted, self.stale = "taken", now, False, False
            return [text]
        return []

    def settled(self) -> bool:
        """The round needs no more waiting: the finger-off came, or no touch is outstanding."""
        return self.lifted or self.touch == "wait"

    def feed_journal(self, line: str, now: float) -> list[str]:
        kind = journal_kind(line)
        cur = self.current
        if kind == "overheat":
            self.overheat = True
            return []
        if cur is None:
            return []
        if kind in ("activation", "taken", "failed"):
            self.alive = True
        if kind == "score":
            m = SCORE_RE.match(line)
            cur.scores.append((int(m.group(1)), int(m.group(2))))
            return []
        if kind == "taken":
            return [] if cur.outcome else self._take(now, MSG_LIFT)
        if kind == "failed":
            self.fails += 1
            # A retry result that came first already asked for the lift.
            return [] if cur.outcome or self.fails <= cur.retries else self._take(now, MSG_LIFT_FAILED)
        if kind == "stale":
            if self.stale or cur.outcome:
                return []
            self.touch, self.taken_at, self.lifted, self.stale = "wait", None, False, True
            self.stale_at, self.still_noted = now, False
            return [MSG_STALE]
        if kind == "off":
            if self.stale:
                self.stale = False
                return [] if cur.outcome else [MSG_LIFTED, self.round_prompt()]
            if self.touch == "taken":
                self.lifted = True
            return []
        if kind == "still" and still_note_due(self, now):
            self.still_noted = True
            return [MSG_STILL]
        return []

    def feed_client(self, line: str, now: float) -> list[str]:
        if OVERHEAT_RE.search(line):
            self.overheat = True
        cur = self.current
        m = VERIFY_RESULT_RE.match(line)
        if not m:
            if CLIENT_ERROR_RE.search(line):
                self.client_errors.append(line.strip())
            return []
        if cur is None or cur.outcome:
            return []
        self.alive = True
        name, done = m.group(1), m.group(2) == "done"
        kind = name.removeprefix("verify-")
        if kind in RETRY_TEXT and not done:
            cur.retries += 1
            # The core ends the attempt at a retry (no finger-off wait); fprintd starts the next one.
            self.touch, self.taken_at, self.lifted, self.still_noted = "wait", None, False, False
            return [verify_retry_message(kind, self.used)]
        if not done:
            return []
        prefix = f"Round {cur.index}/{self.rounds}"
        if name in ("verify-match", "verify-no-match"):
            cur.outcome, cur.detail = name.removeprefix("verify-"), name
            out = self._take(now, MSG_LIFT)
            verdict = "as expected" if cur.outcome == self.expect else "NOT as expected"
            return out + [f"{prefix}: {cur.outcome.replace('-', ' ')} ({verdict})"]
        cur.outcome, cur.detail = "error", name
        if name not in VERIFY_ERRORS and not self.quote_errors:
            return [f"{prefix}: {MSG_CLIENT_ERROR}"]
        return [f"{prefix}: error ({VERIFY_ERRORS.get(name, name)})"]

    def end_round(self, rc: int | None) -> list[str]:
        """Close the current round after fprintd-verify exited; returns lines to show."""
        cur = self.current
        assert cur is not None
        out = []
        if self.overheat:
            self.abort = f"the sensor's overheat protection paused it; {OVERHEAT_NEXT}"
            if cur.outcome is None:  # a match or no-match it already gave stands
                cur.outcome, cur.detail = "error", "overheat"
                out.append(f"Round {cur.index}/{self.rounds}: error (overheat protection)")
        elif cur.outcome is None:
            cur.outcome = "error"
            if self.client_errors:
                cur.detail = self.client_errors[0]
                self.abort = (
                    f"fprintd-verify failed: {self.client_errors[0]}" if self.quote_errors else MSG_CLIENT_ERROR
                )
            else:
                cur.detail = f"no result (exit {rc})"
            if self.client_errors and not self.quote_errors:
                out.append(f"Round {cur.index}/{self.rounds}: {MSG_CLIENT_ERROR}")
            else:
                out.append(f"Round {cur.index}/{self.rounds}: error ({cur.detail})")
        self.done.append(cur)
        self.current = None
        return out

    def counts(self) -> dict[str, int]:
        c = {"match": 0, "no-match": 0, "error": 0}
        for r in self.done:
            c[r.outcome or "error"] += 1
        c["retries"] = sum(r.retries for r in self.done)
        c["as expected"] = sum(1 for r in self.done if r.outcome == self.expect)
        return c

    def summary(self) -> tuple[list[str], int]:
        c = self.counts()
        lines = []
        if self.abort:
            lines.append(f"Stopped after round {len(self.done)}: {self.abort}")
        lines += [
            (
                f"Summary: {finger_display(self.used)} against the enrolled {self.finger}, "
                f"expected {self.expect.replace('-', ' ')}"
            ),
            f"  rounds     {len(self.done)}/{self.rounds}",
            f"  match      {c['match']}",
            f"  no match   {c['no-match']}",
            f"  retries    {c['retries']}",
            f"  errors     {c['error']}",
            f"as expected: {c['as expected']}/{self.rounds}",
        ]
        return lines, 0 if c["as expected"] == self.rounds else 1

    def score_log(self) -> list[str]:
        """Per-round lines for the log only: outcome, retries and every sigfm score."""
        out = []
        for r in self.done:
            scores = " ".join(f"{s}/{t}" for s, t in r.scores) or "none"
            out.append(f"round {r.index}: {r.outcome} ({r.detail}), retries {r.retries}, sigfm scores {scores}")
        return out


# ------------------------------------------------------------------ runner


class Output:
    """Prompts to the terminal (and the log); everything else to the log only, timestamped."""

    def __init__(self, term, log) -> None:
        self.term, self.log = term, log

    def record(self, tag: str, line: str) -> None:
        self.log.write(f"{datetime.now().astimezone().strftime('%H:%M:%S.%f')[:-3]} [{tag}] {line}\n")
        self.log.flush()

    def say(self, text: str) -> None:
        self.term.write(text + "\n")
        self.term.flush()
        self.record("prompt", text)


class Proc:
    """A child whose stdout+stderr lines go to `q` as (tag, line), then (tag, None) at EOF."""

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
        )
        self.thread = threading.Thread(target=self._pump, args=(q,), daemon=True)
        self.thread.start()

    def _pump(self, q: queue.Queue) -> None:
        for line in self.p.stdout:
            q.put((self.tag, line.rstrip("\r\n")))
        q.put((self.tag, None))

    def stop(self) -> int | None:
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(2)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        return self.p.returncode


class NotStarted(Exception):
    """A command could not be started; the message is the one terminal line."""


def start(tag: str, argv: list[str], q: queue.Queue, io: Output, package: str) -> Proc:
    """Proc(tag, argv, q), or NotStarted with one plain line (the details go to the log)."""
    try:
        return Proc(tag, argv, q)
    except OSError as e:
        io.record("note", f"cannot start {argv}: {e!r}")
        if isinstance(e, FileNotFoundError):
            raise NotStarted(f"{argv[0]} not found: install {package}") from None
        raise NotStarted(f"{argv[0]} could not be started ({e.strerror or e}). See the log") from None


def journal_argv(cmd: str) -> list[str]:
    return [cmd, "-u", "fprintd", "-f", "-n", "0", "-o", "cat"]


def pump(q: queue.Queue, io: Output, machine, client: Proc, finished, clock=time.monotonic, give_up=None) -> int | None:
    """Feed every line to `machine` until `client` has exited and finished(now, exit_at) holds.

    give_up(now), when given, is asked while the client runs: once it holds, the client is stopped and pump
    returns as soon as its output has ended."""
    exit_at = None
    gave_up = False
    while True:
        try:
            tag, line = q.get(timeout=POLL_S)
        except queue.Empty:
            tag, line = None, None
        now = clock()
        if tag is not None:
            if line is None:
                if tag == client.tag:
                    exit_at = now
                else:
                    io.record("note", f"{tag} output ended")
                    io.say(MSG_JOURNAL_GONE)
            else:
                io.record(tag, line)
                feed = machine.feed_journal if tag == "journal" else machine.feed_client
                for text in feed(line, now):
                    io.say(text)
        for text in machine.tick(now) if hasattr(machine, "tick") else ():
            io.say(text)
        if exit_at is not None and (gave_up or finished(now, exit_at)):
            return client.p.wait()
        if give_up is not None and exit_at is None and not gave_up and give_up(now):
            gave_up = True
            io.record("note", f"{client.tag}: stopped, no sign of fprintd")
            client.stop()


def run_enroll(finger: str, enroll_cmd: str, journal_cmd: str, io: Output, settle_s: float = JOURNAL_SETTLE_S) -> int:
    machine = EnrollMachine(finger)
    q: queue.Queue = queue.Queue()
    journal = client = None
    try:
        journal = start("journal", journal_argv(journal_cmd), q, io, "systemd (journalctl)")
        time.sleep(settle_s)
        client = start("enroll", [enroll_cmd, "-f", finger], q, io, "fprintd")
        for text in machine.start(time.monotonic()):
            io.say(text)
        rc = pump(q, io, machine, client, lambda now, at: now - at >= ENROLL_GRACE_S)
        io.record(
            "note",
            f"fprintd-enroll exit {rc}; stages {machine.stages}/{machine.total}; "
            f"duplicate check {machine.check_done}; retries {machine.retries}; "
            f"unknown results {machine.unknown_results}",
        )
        lines, code = machine.finish(rc)
    except NotStarted as e:
        lines, code = [str(e)], 1
    except KeyboardInterrupt:
        lines = [f"Interrupted: your {machine.display()} was not enrolled. When ready: {machine.again()}"]
        code = 130
    finally:
        if client is not None:
            client.stop()
        if journal is not None:
            journal.stop()
    for text in lines:
        io.say(text)
    return code


def run_verify(
    finger: str,
    used: str,
    expect: str,
    rounds: int,
    verify_cmd: str,
    journal_cmd: str,
    io: Output,
    settle_s: float = JOURNAL_SETTLE_S,
    stuck_s: float | None = None,
    machine: VerifyMachine | None = None,
    show_summary: bool = True,
) -> int:
    """Verify rounds; the summary on the terminal and the exit code (0: every round as expected, 130: Ctrl-C).

    The defaults are a5-verify's behaviour. stuck_s: a round without any sign of fprintd for that long (no
    Activation, touch or result) is stopped, the user is told (MSG_STUCK) and the rounds end (machine.stuck).
    machine: a VerifyMachine built by the caller for the same finger/used/expect/rounds, to read its tally
    afterwards. show_summary=False: the summary table goes to the log only (an interruption or a stop reason
    is still shown)."""
    if machine is None:
        machine = VerifyMachine(finger=finger, used=used, expect=expect, rounds=rounds)
    q: queue.Queue = queue.Queue()
    journal = client = None
    interrupted = False

    def finished(now: float, exit_at: float) -> bool:
        waited = now - exit_at
        return waited >= VERIFY_GRACE_S and (machine.settled() or waited >= LIFT_WAIT_S)

    try:
        journal = start("journal", journal_argv(journal_cmd), q, io, "systemd (journalctl)")
        time.sleep(settle_s)
        for index in range(1, rounds + 1):
            io.record("note", f"--- round {index}/{rounds}")
            for text in machine.start_round(index):
                io.say(text)
            client = start("verify", [verify_cmd, "-f", finger], q, io, "fprintd")
            round_at = time.monotonic()
            give_up = None
            if stuck_s is not None:

                def give_up(now: float, round_at: float = round_at) -> bool:
                    return not machine.alive and now - round_at > stuck_s

            rc = pump(q, io, machine, client, finished, give_up=give_up)
            client = None
            if give_up is not None and give_up(time.monotonic()):
                cur = machine.current
                if cur.outcome is None:
                    cur.outcome, cur.detail = "error", "fprintd not responding"
                machine.stuck, machine.abort = True, "fprintd is not responding"
                io.record("note", f"round {index}: no sign of fprintd for {stuck_s:.0f} s (exit {rc})")
                machine.end_round(rc)
                io.say(MSG_STUCK)
                break
            for text in machine.end_round(rc):
                io.say(text)
            if machine.abort:
                break
    except NotStarted as e:
        io.say(str(e))
        return 1
    except KeyboardInterrupt:
        interrupted = True
        if machine.current is not None:
            machine.current = None
    finally:
        if client is not None:
            client.stop()
        if journal is not None:
            journal.stop()
    for line in machine.score_log():
        io.record("score", line)
    lines, code = machine.summary()
    if interrupted:
        lines.insert(0, f"Interrupted after {len(machine.done)} round(s)")
        code = 130
    for text in lines:
        if show_summary or text.startswith("Interrupted") or (text.startswith("Stopped") and not machine.stuck):
            io.say(text)
        else:
            io.record("summary", text)
    return code


def open_log(path: str):
    """Append to the wrapper's log (created 0600 by it); never through a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A5 enroll/verify prompts (use tools/dev/a5-enroll.sh / a5-verify.sh)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("enroll", "verify"):
        p = sub.add_parser(name)
        if name == "enroll":  # always named: the prompts tell the user which finger to touch with
            p.add_argument("--finger", required=True, choices=FINGERS)
        else:
            p.add_argument("--finger", default=DEFAULT_FINGER, choices=FINGERS)
        p.add_argument("--log", required=True)
        p.add_argument("--journalctl", default="journalctl")
        p.add_argument("--settle-s", type=float, default=JOURNAL_SETTLE_S, help=argparse.SUPPRESS)
        if name == "enroll":
            p.add_argument("--fprintd-enroll", default="fprintd-enroll")
        else:
            p.add_argument("--fprintd-verify", default="fprintd-verify")
            p.add_argument("--finger-used", required=True)
            p.add_argument("--expect", required=True, choices=("match", "no-match"))
            p.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args(argv)
    with open_log(args.log) as log:
        io = Output(sys.stdout, log)
        if args.cmd == "enroll":
            return run_enroll(args.finger, args.fprintd_enroll, args.journalctl, io, args.settle_s)
        used = parse_finger_used(args.finger_used)
        if used is None:
            print(f"refusing: --finger-used {args.finger_used!r} is not a finger such as right-middle", file=sys.stderr)
            return 2
        if not 1 <= args.rounds <= ROUNDS_MAX:
            print(f"refusing: --rounds must be 1..{ROUNDS_MAX}", file=sys.stderr)
            return 2
        return run_verify(
            args.finger, used, args.expect, args.rounds, args.fprintd_verify, args.journalctl, io, args.settle_s
        )


if __name__ == "__main__":
    sys.exit(main())
