"""Offline tests of tools/dev/a5_suspend.py and the a5-suspend.sh wrapper.

Nothing sleeps: the fake `systemctl suspend --no-ask-password` only counts the suspends in a file. The resume is seen either
through an injected clock (boottime - monotonic grows with that count) or through the fake system journal,
which prints "PM: suspend exit" after a counted suspend. fprintd-verify, journalctl, busctl and
systemd-inhibit are fakes too, and the 06cb:0078 device is a fake sysfs tree under A5_TEST_ROOT.

No test depends on how fast the machine is. The fakes take turns through events, never through sleeps:
the fake finger touches only after the session has shown its touch prompt; a check that sleep interrupts
has ended before the fake suspend counts the sleep; the fake system journal replays what happened since its
own process started, however late it got going; the injected Enter waits for what a person would see
first. The time limits of the session are generous upper bounds on the pass path (it never waits for them);
only the tests of a timeout set a short one.
"""

import io
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "dev"))

import a5_session as a5
import a5_suspend as sus
from test_a5_session import ACT, L1, L3, L6, SERIAL, Env, allowed_prompt, fake

SUSPEND_SH = HERE / "dev" / "a5-suspend.sh"
SUSPEND_PY = HERE / "dev" / "a5_suspend.py"

CAPTURE = "Capture module: already loaded (RomInfo byte 17 = 0x1e)"
EP83 = "EP 0x83 event 0200401000: finger (+97 ms after 02)"
OTHER_UNIT = "Started Session 3 of User someone."
OVERHEAT = "Device reported an error during verify: Device disabled to prevent overheating."
# fprintd-verify's own error text: the log keeps it, the terminal never shows it.
RAW_ERROR = "failed to claim device: GDBus.Error:net.reactivated.Fprint.Error.Internal: Open failed with error: x42"
KERNEL = [
    "Oct 15 10:00:01.000000 host kernel: PM: suspend entry (s2idle)",
    "Oct 15 10:00:09.000000 host kernel: usb 1-9: reset full-speed USB device number 3 using xhci_hcd",
    "Oct 15 10:00:09.100000 host kernel: ACPI: EC: interrupt unblocked",
    "Oct 15 10:00:09.200000 host kernel: PM: suspend exit",
]
# Lines of the fakes that must never reach the terminal.
NOISE = [SERIAL, CAPTURE, EP83, OTHER_UNIT, "PM: suspend", "usb 1-9", "Verify started", "Verify result", "Using device"]
WAKE = sus.wake_instructions()


class SuspendEnv(Env):
    """Env plus the suspend fakes. State files under tmp/state: `plan` (one fprintd-verify behaviour per call,
    comma separated), `suspends` (counted by systemctl suspend, after `suspend-asked` and after every
    `interruptible-<pid>.pid` client has ended), `events` (every count bump with its CLOCK_BOOTTIME time),
    `journal-resume` (present: the system journal prints "PM: suspend exit" after each suspend), `prompted`
    (the session showed its touch prompt: counted by the Rig's terminal), `touches` (the fake finger touched:
    the system journal prints the L6 and L1 lines), `stales` (a stale touch: the system journal prints L3,
    then L1), `overheat` (run_verify's fprintd journal starts with an overheat line, then marks
    `overheat-told`; a "match" check starts only after that), `overheat-resume` (the system journal prints one after a suspend, then marks `overheat-printed`),
    `suspend-how` (stdin and session of the last suspend command). Suspend refusals: `suspend-fails` (Access
    denied, exit 1), `suspend-auth` (Interactive authentication required, exit 1), `suspend-exit` (a plain
    failure, exit 1), `suspend-polkit-0` (a polkit error, exit 0). Without --no-ask-password the fake suspend
    wants authentication."""

    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.state = tmp / "state"
        self.state.mkdir()
        self.plan("match")
        dev = self.root / "sys/bus/usb/devices/1-9"
        (dev / "power").mkdir(parents=True)
        (dev / "idVendor").write_text("06cb\n")
        (dev / "idProduct").write_text("0078\n")
        (dev / "power/control").write_text("auto\n")
        (dev / "power/runtime_status").write_text("active\n")
        other = self.root / "sys/bus/usb/devices/1-1"
        other.mkdir()
        (other / "idVendor").write_text("1d6b\n")
        (other / "idProduct").write_text("0002\n")
        self.usb = str(self.root / "sys/bus/usb/devices")
        st = repr(str(self.state))
        rec = f"""
        import os, pathlib, sys, time
        with open({str(self.calls)!r}, "a") as f:
            f.write(pathlib.Path(sys.argv[0]).name + " " + " ".join(sys.argv[1:]) + "\\n")
        st = pathlib.Path({st})
        def count(name):
            p = st / name
            return int(p.read_text()) if p.exists() else 0
        def bump(name):
            # Atomic (a reader never sees a half-written count), and logged with its CLOCK_BOOTTIME time.
            n = count(name) + 1
            tmp = st / f".{{name}}.{{os.getpid()}}"
            tmp.write_text(str(n))
            os.replace(tmp, st / name)
            with open(st / "events", "a") as f:
                f.write(f"{{name}} {{time.clock_gettime(time.CLOCK_BOOTTIME)}}\\n")
            return n
        def until(cond, limit=60.0):
            end = time.monotonic() + limit
            while not cond() and time.monotonic() < end:
                time.sleep(0.02)
        def gone(pid):
            # Exited (a zombie counts: its output has ended) or never there.
            try:
                stat = pathlib.Path(f"/proc/{{pid}}/stat").read_text()
            except OSError:
                return True
            return stat.rsplit(")", 1)[1].split()[0] in ("Z", "X")
        """
        fake(
            self.bin / "systemctl",
            rec
            + """
        if "ActiveState" in sys.argv:
            print(os.environ.get("FAKE_ACTIVE", "inactive"))
        elif "ExecMainStartTimestamp" in sys.argv:
            print(os.environ.get("FAKE_START", ""))
        elif "Environment" in sys.argv:
            print(os.environ.get("FAKE_ENV", "G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print"))
        elif sys.argv[1:2] == ["suspend"]:
            stdin = os.path.realpath("/proc/self/fd/0")
            (st / "suspend-how").write_text(f"stdin={stdin} own-session={os.getsid(0) == os.getpid()}")
            if "--no-ask-password" not in sys.argv or (st / "suspend-auth").exists():
                print("Call to Suspend failed: Interactive authentication required.", file=sys.stderr)
                sys.exit(1)
            if (st / "suspend-fails").exists():
                print("Call to Suspend failed: Access denied", file=sys.stderr)
                sys.exit(1)
            if (st / "suspend-exit").exists():
                print('Failed to suspend system via logind: Sleep verb "suspend" not supported', file=sys.stderr)
                sys.exit(1)
            if (st / "suspend-polkit-0").exists():
                print("polkit: no authorization agent available", file=sys.stderr)
                sys.exit(0)
            # Going to sleep: a check that sleep interrupts ends first, then the sleep is counted (the clock
            # and the journal see the resume only after that check's output has ended).
            bump("suspend-asked")
            pids = [int(p.read_text()) for p in st.glob("interruptible-*.pid")]
            until(lambda: all(gone(pid) for pid in pids))
            bump("suspends")
        """,
        )
        fake(
            self.bin / "journalctl",
            rec
            + f"""
        if os.environ.get("FAKE_JOURNAL_HIDDEN"):
            print("Hint: You are currently not seeing messages from other users and the system.", file=sys.stderr)
            sys.exit(0)
        if "-k" in sys.argv:
            if (st / "kernel-denied").exists():
                print("No journal files were opened due to insufficient permissions.", file=sys.stderr)
                sys.exit(1)
            print("\\n".join({KERNEL!r}))
            sys.exit(0)
        if "-f" not in sys.argv:
            sys.exit(0)
        (st / f"journal-{{os.getpid()}}.pid").write_text(str(os.getpid()))
        if "-u" in sys.argv:  # run_verify's follow of fprintd
            if (st / "overheat").exists():
                print({OVERHEAT!r}, flush=True)
                (st / "overheat-told").write_text("1")
            print({CAPTURE!r}, flush=True)
            print({SERIAL!r}, flush=True)
            time.sleep(60)
            sys.exit(0)
        # Like `journalctl -f -n 0`: only what happens after this process started, however late it got
        # going (its start time from /proc, CLOCK_BOOTTIME like the event times).
        t0 = int(pathlib.Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK")
        def before(name):
            p = st / "events"
            rows = [ln.split() for ln in p.read_text().split("\\n")[:-1]] if p.exists() else []  # whole lines
            return sum(1 for r in rows if len(r) == 2 and r[0] == name and float(r[1]) < t0)
        suspends, started, touches = before("suspends"), before("verify-started"), before("touches")
        stales = before("stales")
        print({OTHER_UNIT!r}, flush=True)
        print({SERIAL!r}, flush=True)
        while True:
            if count("verify-started") > started:
                started = count("verify-started")
                print({ACT!r}, flush=True)
                print({CAPTURE!r}, flush=True)
            if count("stales") > stales:
                stales = count("stales")
                print({L3!r}, flush=True)
                time.sleep(0.3)
                print({L1!r}, flush=True)
            if count("touches") > touches:
                touches = count("touches")
                print({L6!r}, flush=True)
                time.sleep(0.3)
                print({L1!r}, flush=True)
            if count("suspends") > suspends:
                suspends = count("suspends")
                print("PM: suspend entry (s2idle)", flush=True)
                if (st / "journal-resume").exists():
                    print("PM: suspend exit", flush=True)
                    print({EP83!r}, flush=True)
                if (st / "overheat-resume").exists():
                    print({OVERHEAT!r}, flush=True)
                    (st / "overheat-printed").write_text("1")
            time.sleep(0.02)
        """,
        )
        fake(
            self.bin / "fprintd-verify",
            rec
            + f"""
        n = bump("verify-calls")
        plan = (st / "plan").read_text().split(",")
        mode = plan[n - 1] if n <= len(plan) else "match"
        def started():
            print("Using device /net/reactivated/Fprint/Device/0", flush=True)
            print("Verify started!", flush=True)
            bump("verify-started")
        def prompted(n=1):
            # The session has shown its touch prompt n times (the Rig's terminal counts them).
            return count("prompted") >= n
        if mode == "match":
            if (st / "overheat").exists():  # the overheat line is in the journal before the check starts
                until(lambda: (st / "overheat-told").exists())
            started()
            time.sleep(0.1)
            print("Verify result: verify-match (done)", flush=True)
        elif mode == "silent":
            time.sleep(60)
        elif mode == "wait-error":  # sleep interrupts it: it ends before the fake suspend counts the sleep
            (st / f"interruptible-{{os.getpid()}}.pid").write_text(str(os.getpid()))
            s0 = count("suspend-asked")
            started()
            until(lambda: count("suspend-asked") > s0)
            print("Verify result: verify-unknown-error (done)", flush=True)
            sys.exit(1)
        elif mode == "wait-alive":  # still waiting after the resume: the finger comes after the touch prompt
            started()
            until(prompted)
            bump("touches")
            time.sleep(0.1)
            print("Verify result: verify-match (done)", flush=True)
        elif mode == "wait-forever":
            (st / f"client-{{os.getpid()}}.pid").write_text(str(os.getpid()))
            started()
            time.sleep(60)
        elif mode == "claim-error":  # gives up at once, before the sensor is armed
            print({RAW_ERROR!r}, flush=True)
            sys.exit(1)
        elif mode == "wait-alive-error":  # still waiting after the resume, then an error of its own
            started()
            until(prompted)
            print({RAW_ERROR!r}, flush=True)
            sys.exit(1)
        elif mode == "wait-stale":  # after the touch prompt: a stale touch first, then (asked again) a real one
            started()
            until(prompted)
            bump("stales")
            until(lambda: prompted(2))
            bump("touches")
            time.sleep(0.1)
            print("Verify result: verify-match (done)", flush=True)
        elif mode == "wait-overheat":  # the overheat comes with the resume; the check then ends in an error
            started()
            until(lambda: (st / "overheat-printed").exists())
            print("Verify result: verify-unknown-error (done)", flush=True)
            sys.exit(1)
        """,
        )
        fake(
            self.bin / "busctl",
            rec
            + """
        if os.environ.get("FAKE_BUSCTL_FAIL"):
            print("Failed to connect to bus: No such file or directory", file=sys.stderr)
            sys.exit(1)
        if "ListInhibitors" in sys.argv:
            if os.environ.get("FAKE_INHIBITORS_FAIL"):
                print("Call failed: Access denied", file=sys.stderr)
                sys.exit(1)
            print(os.environ.get("FAKE_INHIBITORS", '{"type":"a(ssssuu)","data":[[]]}'))
            sys.exit(0)
        print('s "' + os.environ.get("FAKE_CAN_SUSPEND", "yes") + '"')
        """,
        )
        fake(
            self.bin / "systemd-inhibit",
            rec
            + """
        print("WHO     UID  USER PID  COMM    WHAT  WHY                         MODE")
        print("fprintd 0    root 812  fprintd sleep Suspend fingerprint readers delay")
        print("")
        print("1 inhibitors listed.")
        """,
        )

    def plan(self, *modes: str) -> None:
        (self.state / "plan").write_text(",".join(modes))

    def flag(self, name: str) -> None:
        (self.state / name).write_text("1")

    def count(self, name: str) -> int:
        p = self.state / name
        return int(p.read_text()) if p.exists() else 0

    def bump(self, name: str) -> None:
        """The fakes' atomic bump, from the test process (no event time: no fake replays these)."""
        tmp = self.state / f".{name}.test"
        tmp.write_text(str(self.count(name) + 1))
        os.replace(tmp, self.state / name)

    def env(self, **extra) -> dict:
        e = super().env(
            A5_BUSCTL=str(self.bin / "busctl"),
            A5_SYSTEMD_INHIBIT=str(self.bin / "systemd-inhibit"),
            A5_SUSPEND_COUNTDOWN_S="0.01",
        )
        e.update(extra)
        return e

    def cmds(self) -> sus.Commands:
        return sus.Commands(
            str(self.bin / "fprintd-verify"),
            str(self.bin / "journalctl"),
            str(self.bin / "systemctl"),
            str(self.bin / "systemd-inhibit"),
        )


class Keys:
    """The Enter key: pressed as soon as `hold()` allows (at once by default), as a person presses it only
    after what the screen shows has played out. A hold that never ends presses it after HOLD_MAX_S, so a
    broken session fails its asserts instead of hanging."""

    HOLD_MAX_S = 60.0

    def __init__(self) -> None:
        self.presses, self.discards = 0, 0
        self.hold = lambda: True
        self.since: float | None = None

    def discard(self) -> None:
        self.discards += 1

    def pressed(self) -> bool:
        now = time.monotonic()
        if self.since is None:
            self.since = now
        if not self.hold() and now - self.since < self.HOLD_MAX_S:
            return False
        self.presses += 1
        self.since = None
        return True


class Terminal(io.StringIO):
    """The session's terminal; each touch prompt it shows bumps `prompted`, the fake finger's cue."""

    def __init__(self, env: "SuspendEnv") -> None:
        super().__init__()
        self.env = env

    def write(self, text: str) -> int:
        n = super().write(text)
        if text.rstrip("\n") == sus.MSG_TOUCH:
            self.env.bump("prompted")
        return n


# The session's time limits: generous upper bounds, never waited for on the pass path (the fakes answer as
# soon as their cue comes). The tests of a timeout pass a short one of their own.
FAST = {
    "settle_s": 0.1,
    "countdown_s": 0.01,
    "sleep_wait_s": 60.0,
    "ready_s": 60.0,
    "result_s": 60.0,
    "stuck_s": 60.0,
    "enter_grace_s": 0.2,  # a check ended by sleep has ended before the resume (the fake suspend sees to it)
}


class Rig:
    """A SuspendSession on the fakes, its terminal and its log."""

    def __init__(self, tmp: Path, clock: bool = True, **timing) -> None:
        self.env = SuspendEnv(tmp)
        self.term = Terminal(self.env)
        self.log_path = tmp / "a5-suspend.log"
        self.log = a5.open_log(str(self.log_path))
        self.keys = Keys()
        # The injected clock: 20 s asleep per counted suspend (or never asleep).
        asleep = (lambda: 20.0 * self.env.count("suspends")) if clock else (lambda: 0.0)
        self.session = sus.SuspendSession(
            a5.Output(self.term, self.log),
            self.env.cmds(),
            sus.Timing(**{**FAST, **timing}),
            keyboard=self.keys,
            usb_devices=self.env.usb,
            log_path=str(self.log_path),
            asleep=asleep,
        )

    def enter_after_the_check_ends(self, overheat: bool = False) -> None:
        """Part 2: Enter only once the session has seen the check in progress end (and the overheat line):
        the user is back after what sleep did to that check has played out."""
        s = self.session
        self.keys.hold = lambda: (
            s.machine is None or (s.client_end is not None and (s.machine.overheat or not overheat))
        )

    def shown(self) -> list[str]:
        return self.term.getvalue().splitlines()

    def text(self) -> str:
        self.log.flush()
        return self.log_path.read_text()

    def close(self) -> None:
        self.log.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.session.stop_all()
    r.close()


def make_rig(tmp_path, **kw):
    return Rig(tmp_path, **kw)


def suspend_terminal_bad(lines: list[str]) -> list[str]:
    """Terminal lines that are neither the suspend tool's own short lines nor the a5 prompts."""
    own = {
        sus.MSG_WAITING_SLEEP,
        sus.MSG_WELCOME,
        sus.MSG_INTERRUPTED,
        sus.MSG_TOUCH,
        sus.MSG_NO_SLEEP,
        a5.MSG_STUCK,
        *WAKE,
        *(f"{i}..." for i in range(1, 6)),
    }
    prefixes = (
        "A5 suspend: ",
        "Checking the setup... ok",
        "Log (private, it holds the sensor serial): ",
        "Touch the sensor only when asked",
        "Part 1 of 2: ",
        "Part 2 of 2: ",
        "Getting the reader ready",
        "The laptop will go to sleep in 5 seconds.",
        "Do NOT touch the sensor. The laptop will sleep in 5 seconds.",
        "Now ",
        "Checks: ",
        "Result: ",
        "No response from the reader",
        "The reader did not get ready",
        "Stopped after round ",
        sus.MSG_ENDED_NOT_READY,
        sus.MSG_OVERHEAT_STOP,
        "The laptop did not go to sleep.",
        "The sleep command failed (",
        "The check gave a result after sleep: ",
        "The check had already ended before the laptop slept",
        sus.MSG_SUSPEND_AUTH,
        "Summary:",
        "Part 1 (sleep while idle): ",
        "Part 2 (sleep during a check): ",
        "Part 2: the reader worked again afterwards: ",
        "Log: ",
    )
    return [ln for ln in lines if not (ln in own or ln.startswith(prefixes) or allowed_prompt(ln))]


def assert_quiet(terminal: str) -> None:
    for noise in NOISE:
        assert noise not in terminal, noise


def gone(pid: int, wait_s: float = 3.0) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    return False


# ------------------------------------------------------------------ the session, end to end


def test_full_session_pass_resume_by_clock_and_check_interrupted(rig):
    rig.env.plan("match", "match", "match", "wait-error", "match", "match")
    rig.env.flag("journal-resume")
    rig.enter_after_the_check_ends()
    code = rig.session.run()
    shown = rig.shown()
    assert code == 0, shown
    assert suspend_terminal_bad(shown) == []
    assert_quiet(rig.term.getvalue())
    # Before each sleep: the wake/unlock/Enter instructions, then the countdown, then the wait.
    for lead in (
        "The laptop will go to sleep in 5 seconds.",
        "Do NOT touch the sensor. The laptop will sleep in 5 seconds.",
    ):
        i = shown.index(lead)
        assert shown[i + 1 : i + 10] == [*WAKE, "5...", "4...", "3...", "2...", "1...", sus.MSG_WAITING_SLEEP]
        assert shown[i + 10] == sus.MSG_WELCOME
    assert shown.count(sus.MSG_WELCOME) == 2 and rig.keys.presses == 2
    assert sus.MSG_INTERRUPTED in shown and sus.MSG_TOUCH not in shown
    assert "Round 1/3: touch with your RIGHT INDEX finger, then lift" in shown
    assert "Round 2/2: touch with your RIGHT INDEX finger, then lift" in shown
    assert shown[-5:] == [
        "Summary:",
        "Part 1 (sleep while idle): worked after sleep, 3/3 checks as expected — PASS",
        "Part 2 (sleep during a check): the check in progress was interrupted by sleep",
        "Part 2: the reader worked again afterwards: yes (2/2 as expected) — PASS",
        f"Log: {rig.log_path}",
    ]
    calls = rig.env.called()
    # Never a polkit prompt: --no-ask-password, stdin on /dev/null, no controlling terminal (own session).
    assert [c for c in calls if " suspend" in c] == ["systemctl suspend --no-ask-password"] * 2
    assert (rig.env.state / "suspend-how").read_text() == "stdin=/dev/null own-session=True"
    assert not any("restart" in c for c in calls)
    text = rig.text()
    assert "Diag image" not in text  # the fakes send none (--diag off), and the session still passes
    assert stat.S_IMODE(rig.log_path.stat().st_mode) == 0o600
    assert text.count("[resume] resume detected first by: clock gap") == 2
    assert "[resume] journal line at " in text  # the second signal is recorded too
    assert "[journal:after-resume] PM: suspend exit" in text or "[journal:during] PM: suspend exit" in text
    assert f"[journal:before-suspend] {SERIAL}" in text and f"[journal:before-suspend] {OTHER_UNIT}" in text
    assert "[suspend] sleep 1: suspend command at " in text and "[suspend] sleep 2: suspend command at " in text
    for when in ("before sleep 1", "after sleep 1", "before sleep 2", "after sleep 2"):
        assert f"[power] {when}: 1-9 power/control = auto" in text
        assert f"[power] {when}: 1-9 power/runtime_status = active" in text
    assert "power/wakeup" not in text  # absent in this sysfs: skipped
    assert "[inhibit] fprintd 0    root 812  fprintd sleep Suspend fingerprint readers delay" in text
    assert "[kernel] " + KERNEL[1] in text and "ACPI: EC" not in text  # only the USB lines (and PM)
    assert "-k --since" in "\n".join(calls)
    # What fprintd reported for the check in progress, with its phase and time.
    assert "[part2] the check in progress: interrupted; fprintd-verify ended " in text
    assert "verify-unknown-error (done)" in text
    assert "[verify:before-suspend] Verify started!" in text
    assert f"[journal] {CAPTURE}" in text  # run_verify's rounds: the driver lines stay in the log


def test_resume_seen_by_the_journal_line_when_the_clock_shows_nothing(tmp_path):
    r = Rig(tmp_path, clock=False)
    try:
        r.env.plan("match", "match", "match")
        r.env.flag("journal-resume")
        part = r.session.part1()
        assert part.passed(), r.shown()
        text = r.text()
        assert "[resume] resume detected first by: journal line" in text
        assert "clock gap" not in text
        assert "[journal:after-resume] PM: suspend exit" in text or "[journal:during] PM: suspend exit" in text
    finally:
        r.session.stop_all()
        r.close()


def test_resume_by_clock_gap_alone(tmp_path):
    r = Rig(tmp_path)
    try:
        r.env.plan("match", "match", "match")
        part = r.session.part1()  # no journal resume line at all
        assert part.passed()
        text = r.text()
        assert "[resume] clock gap at " in text and "grew by 20.0 s" in text
        assert "other signals: none yet" in text
    finally:
        r.session.stop_all()
        r.close()


def test_clock_gap_below_the_threshold_is_not_a_sleep(tmp_path):
    r = Rig(tmp_path, clock=False, sleep_wait_s=0.6)
    try:
        r.session.asleep = lambda: 1.5 * r.env.count("suspends")  # less than resume_gap_s
        code = r.session.run()
        assert code == 1
        assert sus.MSG_NO_SLEEP in r.shown()
    finally:
        r.close()


def test_laptop_did_not_go_to_sleep(tmp_path):
    r = Rig(tmp_path, clock=False, sleep_wait_s=0.6)
    try:
        code = r.session.run()
        shown = r.shown()
        assert code == 1
        assert sus.MSG_NO_SLEEP in shown and sus.MSG_WELCOME not in shown
        assert shown[-4:-1] == [
            "Summary:",
            "Part 1 (sleep while idle): FAIL (the laptop did not go to sleep)",
            "Part 2 (sleep during a check): not run (the laptop did not go to sleep in part 1) — FAIL",
        ]
        assert r.env.count("suspends") == 1
        assert not any(c.startswith("fprintd-verify") for c in r.env.called())
        assert "the laptop did not go to sleep: no sign of sleep within 1 s" in r.text()
    finally:
        r.close()


@pytest.mark.parametrize(
    "flag, said, why, raw",
    [
        ("suspend-fails", sus.MSG_SUSPEND_AUTH, sus.REASON_AUTH, "Call to Suspend failed: Access denied"),
        (
            "suspend-auth",
            sus.MSG_SUSPEND_AUTH,
            sus.REASON_AUTH,
            "Call to Suspend failed: Interactive authentication required.",
        ),
        ("suspend-polkit-0", sus.MSG_SUSPEND_AUTH, sus.REASON_AUTH, "polkit: no authorization agent available"),
        (
            "suspend-exit",
            "The sleep command failed (exit 1), so this test stops here (see the log).",
            "the sleep command failed (exit 1)",
            'Failed to suspend system via logind: Sleep verb "suspend" not supported',
        ),
    ],
)
def test_suspend_refused_stops_the_whole_run_with_one_plain_line(tmp_path, flag, said, why, raw):
    r = Rig(tmp_path)
    try:
        r.env.flag(flag)
        code = r.session.run()
        shown = r.shown()
        assert code == 1
        i = shown.index(sus.MSG_WAITING_SLEEP)
        assert shown[i + 1] == said  # one plain line, then the summary
        assert shown[i + 2 :] == [
            "Summary:",
            f"Part 1 (sleep while idle): FAIL ({why})",
            f"Part 2 (sleep during a check): not run ({why}) — FAIL",
            f"Log: {r.log_path}",
        ]
        assert sus.MSG_WELCOME not in shown and sus.MSG_NO_SLEEP not in shown
        assert suspend_terminal_bad(shown) == [] and raw not in r.term.getvalue()
        assert r.env.count("suspends") == 0
        calls = r.env.called()
        assert [c for c in calls if " suspend" in c] == ["systemctl suspend --no-ask-password"]  # no retry
        assert not any(c.startswith("fprintd-verify") for c in calls)  # no further part
        text = r.text()
        assert f"[systemctl:during] {raw}" in text
        assert f"the laptop did not go to sleep: {why}" in text
        assert f"[note] the run stops after part 1: {why}" in text
    finally:
        r.close()


def test_suspend_refused_in_part2_ends_it_without_the_later_checks(rig):
    rig.env.plan("wait-forever")
    rig.env.flag("suspend-auth")
    part = rig.session.part2()
    shown = rig.shown()
    assert part.refused and not part.slept and part.fail == sus.REASON_AUTH
    assert shown[-1] == sus.MSG_SUSPEND_AUTH
    assert not part.ran_rounds and rig.env.count("verify-calls") == 1  # only the check in progress, stopped
    (pid_file,) = rig.env.state.glob("client-*.pid")
    assert gone(int(pid_file.read_text())), "the waiting fprintd-verify was not stopped"
    lines, code = rig.session.summary(sus.Part("1", 3, slept=True, ran_rounds=True, as_expected=3), part)
    assert code == 1 and f"Part 2 (sleep during a check): FAIL ({sus.REASON_AUTH})" in lines


def test_suspend_without_no_ask_password_would_be_refused(tmp_path, monkeypatch):
    """The fake wants authentication unless --no-ask-password is passed: dropping the flag fails loudly."""
    monkeypatch.setattr(sus, "SUSPEND_ARGV", ["suspend"])
    r = Rig(tmp_path)
    try:
        assert r.session.run() == 1
        assert sus.MSG_SUSPEND_AUTH in r.shown()
    finally:
        r.close()


def test_kernel_log_not_readable_is_recorded_and_the_test_goes_on(tmp_path):
    r = Rig(tmp_path)
    try:
        r.env.plan("match", "match", "match")
        r.env.flag("kernel-denied")
        assert r.session.part1().passed()
        assert "--no-pager -o short-precise: exit 1 (No journal files were opened" in r.text()
    finally:
        r.session.stop_all()
        r.close()


# ------------------------------------------------------------------ part 2 branches


def test_part2_check_still_waiting_after_resume_touch_and_match(rig):
    rig.env.plan("wait-alive", "match", "match")
    part = rig.session.part2()
    shown = rig.shown()
    assert part.passed(), shown
    assert part.in_progress == "survived" and part.result == "match"
    i = shown.index(sus.MSG_TOUCH)
    assert sus.MSG_TOUCH == "TOUCH the sensor with your RIGHT INDEX finger"
    assert shown[i - 1] == sus.MSG_WELCOME
    assert a5.MSG_LIFT in shown[i:] and "Result: match" in shown[i:]
    assert sus.MSG_INTERRUPTED not in shown
    assert not any(line.startswith("Round 1/1") for line in shown)  # the machine's own result line stays muted
    assert suspend_terminal_bad(shown) == []
    lines, code = rig.session.summary(sus.Part("1", 3, slept=True, ran_rounds=True, as_expected=3), part)
    assert code == 0
    assert "Part 2 (sleep during a check): the check in progress survived sleep (result: match)" in lines
    text = rig.text()
    assert "[part2] the check in progress: survived; fprintd-verify ended after-resume" in text
    assert "(after-resume): Verify result: verify-match (done)" in text


def test_part2_check_still_waiting_but_no_result_is_stopped_and_fails(tmp_path):
    r = Rig(tmp_path, result_s=1.0)  # the one test of this timeout
    try:
        r.env.plan("wait-forever", "match", "match")
        part = r.session.part2()
        shown = r.shown()
        assert not part.passed()
        assert part.fail == "no response after resume" and part.in_progress == "no response"
        assert sus.MSG_TOUCH in shown
        assert "No response from the reader within 1 s after sleep; that check was stopped." in shown
        (pid_file,) = r.env.state.glob("client-*.pid")
        assert gone(int(pid_file.read_text())), "the waiting fprintd-verify was not stopped"
        assert part.ran_rounds and part.as_expected == 2  # the reader worked again: information, still FAIL
        lines, code = r.session.summary(sus.Part("1", 3, slept=True, ran_rounds=True, as_expected=3), part)
        assert code == 1
        assert "Part 2 (sleep during a check): the check in progress did not respond after sleep" in lines
        assert not any("survived" in line for line in lines)
        assert (
            "Part 2: the reader worked again afterwards: yes (2/2 as expected) — FAIL (no response after resume)"
            in lines
        )
        assert "[part2] no response after resume" in r.text()
    finally:
        r.session.stop_all()
        r.close()


def test_part2_reader_not_ready_stops_without_sleeping(tmp_path):
    r = Rig(tmp_path, ready_s=0.6)
    try:
        r.env.plan("silent")
        part = r.session.part2()
        assert part.fail == "the reader did not get ready before sleep" and not part.slept
        assert "The reader did not get ready within 1 s, so this test stops here (see the log)." in r.shown()
        assert r.env.count("suspends") == 0
        assert "part 2: the reader did not get ready (verify started False, sensor armed False" in r.text()
        lines, code = r.session.summary(sus.Part("1", 3, slept=True, ran_rounds=True, as_expected=3), part)
        assert code == 1 and "Part 2 (sleep during a check): FAIL (the reader did not get ready before sleep)" in lines
    finally:
        r.session.stop_all()
        r.close()


def test_part2_stale_touch_asks_again_naming_the_finger(rig):
    rig.env.plan("wait-stale", "match", "match")
    part = rig.session.part2()
    shown = rig.shown()
    assert part.passed(), shown
    assert part.in_progress == "survived" and part.result == "match"
    i = shown.index(a5.MSG_STALE)
    assert shown[i + 1 : i + 3] == [a5.MSG_LIFTED, sus.MSG_TOUCH]  # the touch prompt again, with the finger
    assert shown.count(sus.MSG_TOUCH) == 2 and "Result: match" in shown[i:]
    assert not any(line.startswith("Round 1/1") for line in shown)
    assert suspend_terminal_bad(shown) == []
    assert "[muted] Round 1/1: touch with your RIGHT INDEX finger, then lift" in rig.text()


def test_part2_check_ended_before_the_reader_was_ready(rig):
    rig.env.plan("claim-error")
    started = time.monotonic()
    part = rig.session.part2()
    shown = rig.shown()
    assert time.monotonic() - started < sus.Timing().ready_s / 2  # said at once, not after the ready timeout
    assert shown[-1] == sus.MSG_ENDED_NOT_READY
    assert not any("did not get ready" in line for line in shown)
    assert part.fail == "the check ended before the reader was ready" and not part.slept and not part.ran_rounds
    assert rig.env.count("suspends") == 0
    assert RAW_ERROR not in rig.term.getvalue() and f"[verify:before-suspend] {RAW_ERROR}" in rig.text()
    lines, code = rig.session.summary(sus.Part("1", 3, slept=True, ran_rounds=True, as_expected=3), part)
    assert code == 1 and "Part 2 (sleep during a check): FAIL (the check ended before the reader was ready)" in lines


def test_part2_error_after_resume_is_named_plainly_and_logged_in_full(rig):
    rig.env.plan("wait-alive-error", "match", "match")
    part = rig.session.part2()
    shown = rig.shown()
    assert part.passed(), shown  # an error of the check in progress is information; the later checks decide
    assert part.in_progress == "survived" and part.result == f"error ({sus.REASON_CLIENT})"
    assert f"Result: {a5.MSG_CLIENT_ERROR}" in shown
    assert RAW_ERROR not in rig.term.getvalue() and "GDBus" not in rig.term.getvalue()
    assert suspend_terminal_bad(shown) == []
    text = rig.text()
    assert f"[verify:after-resume] {RAW_ERROR}" in text and f"[part2] error after resume: {RAW_ERROR}" in text
    lines, _ = rig.session.summary(sus.Part("1", 3, slept=True, ran_rounds=True, as_expected=3), part)
    survived = "Part 2 (sleep during a check): the check in progress survived sleep"
    assert f"{survived} (result: error (fprintd reported an error))" in lines


# ------------------------------------------------------------------ an overheat or an fprintd error stops the test


def test_client_error_in_part1_skips_part2_and_keeps_the_raw_text_off_the_terminal(rig):
    rig.env.plan("claim-error", "match", "match")
    code = rig.session.run()
    shown = rig.shown()
    assert code == 1
    assert "Round 1/3: fprintd reported an error (details in the log)" in shown
    assert "Stopped after round 1: fprintd reported an error (details in the log)" in shown
    assert shown[-4:-1] == [
        "Summary:",
        f"Part 1 (sleep while idle): FAIL ({sus.REASON_CLIENT})",
        f"Part 2 (sleep during a check): not run (part 1 stopped: {sus.REASON_CLIENT}) — FAIL",
    ]
    assert not any(line.startswith(("Part 2 of 2", "Do NOT touch")) for line in shown)
    assert rig.env.count("verify-calls") == 1 and rig.env.count("suspends") == 1
    terminal = rig.term.getvalue()
    assert RAW_ERROR not in terminal and "GDBus" not in terminal
    assert suspend_terminal_bad(shown) == []
    text = rig.text()
    assert f"[verify] {RAW_ERROR}" in text  # fprintd's own words stay in the log
    assert f"[score] round 1: error ({RAW_ERROR})" in text
    assert f"[note] the run stops after part 1: {sus.REASON_CLIENT}" in text


def test_overheat_in_part1_skips_part2(rig):
    rig.env.plan("match", "match", "match")
    rig.env.flag("overheat")
    code = rig.session.run()
    shown = rig.shown()
    assert code == 1
    assert "Stopped after round 1: the sensor's overheat protection paused it; " + a5.OVERHEAT_NEXT in shown
    assert shown[-4:-1] == [
        "Summary:",
        f"Part 1 (sleep while idle): FAIL ({sus.REASON_OVERHEAT})",
        f"Part 2 (sleep during a check): not run (part 1 stopped: {sus.REASON_OVERHEAT}) — FAIL",
    ]
    assert not any(line.startswith(("Part 2 of 2", "Do NOT touch")) for line in shown)
    assert rig.env.count("verify-calls") == 1 and rig.env.count("suspends") == 1
    assert suspend_terminal_bad(shown) == [] and OVERHEAT not in rig.term.getvalue()


@pytest.mark.parametrize("cause", ["client error", "overheat"])
def test_abort_in_part2_later_rounds_stops_there(rig, cause):
    if cause == "client error":
        rig.env.plan("wait-error", "claim-error", "match")
        reason = sus.REASON_CLIENT
    else:
        rig.env.plan("wait-error", "match", "match")
        rig.env.flag("overheat")
        reason = sus.REASON_OVERHEAT
    rig.enter_after_the_check_ends()
    part = rig.session.part2()
    shown = rig.shown()
    assert part.in_progress == "interrupted" and part.ran_rounds
    assert part.abort == reason and part.fail == reason and not part.passed()
    assert rig.env.count("verify-calls") == 2  # the check in progress, then round 1 of 2 only
    assert "Round 2/2: touch with your RIGHT INDEX finger, then lift" not in shown
    assert any(line.startswith("Stopped after round 1: ") for line in shown)
    assert suspend_terminal_bad(shown) == [] and RAW_ERROR not in rig.term.getvalue()
    lines, code = rig.session.summary(sus.Part("1", 3, slept=True, ran_rounds=True, as_expected=3), part)
    assert code == 1 and lines[-2].endswith(f"— FAIL ({reason})")


def test_overheat_during_the_check_in_progress_skips_the_later_rounds(rig):
    rig.env.plan("wait-overheat", "match", "match")
    rig.env.flag("overheat-resume")
    rig.enter_after_the_check_ends(overheat=True)
    part = rig.session.part2()
    shown = rig.shown()
    assert shown[-1] == sus.MSG_OVERHEAT_STOP
    assert part.slept and not part.ran_rounds and part.abort == sus.REASON_OVERHEAT
    assert rig.env.count("verify-calls") == 1
    assert suspend_terminal_bad(shown) == [] and OVERHEAT not in rig.term.getvalue()
    lines, code = rig.session.summary(sus.Part("1", 3, slept=True, ran_rounds=True, as_expected=3), part)
    assert code == 1 and f"Part 2 (sleep during a check): FAIL ({sus.REASON_OVERHEAT})" in lines


# ------------------------------------------------------------------ a stuck reader


def test_wedge_after_sleep_stops_the_test_without_restarting_fprintd(tmp_path):
    r = Rig(tmp_path, stuck_s=0.8)
    try:
        r.env.plan("silent")  # the first round after the sleep: fprintd says nothing at all
        code = r.session.run()
        shown = r.shown()
        assert code == 1
        assert a5.MSG_STUCK in shown
        assert a5.MSG_STUCK == (
            "fprintd is not responding (the reader looks stuck). Restarting it needs sudo, so this test stops here."
        )
        assert "Part 1 (sleep while idle): FAIL (fprintd is not responding (the reader looks stuck))" in shown
        assert "Part 2 (sleep during a check): not run (the reader looked stuck in part 1) — FAIL" in shown
        calls = r.env.called()
        assert [c for c in calls if c.startswith("fprintd-verify")] == ["fprintd-verify -f right-index-finger"]
        assert not any("restart" in c or "start fprintd" in c for c in calls)
        assert r.env.count("suspends") == 1
        assert "[note] round 1: no sign of fprintd for 1 s" in r.text()
        assert suspend_terminal_bad(shown) == []
    finally:
        r.close()


def test_run_verify_stuck_hook(tmp_path):
    env = SuspendEnv(tmp_path)
    env.plan("silent", "match")
    term = io.StringIO()
    log_path = tmp_path / "v.log"
    machine = a5.VerifyMachine(rounds=2)
    with a5.open_log(str(log_path)) as log:
        code = a5.run_verify(
            "right-index-finger",
            "right-index-finger",
            "match",
            2,
            str(env.bin / "fprintd-verify"),
            str(env.bin / "journalctl"),
            a5.Output(term, log),
            0.05,
            stuck_s=0.5,
            machine=machine,
            show_summary=False,
        )
    shown = term.getvalue().splitlines()
    assert code == 1
    assert machine.stuck and machine.abort == "fprintd is not responding"
    assert machine.done[0].outcome == "error" and machine.done[0].detail == "fprintd not responding"
    assert shown == ["Round 1/2: touch with your RIGHT INDEX finger, then lift", a5.MSG_STUCK]
    assert env.count("verify-calls") == 1  # no second round
    assert "[summary] as expected: 0/2" in log_path.read_text()


def test_run_verify_stuck_hook_spares_a_round_that_answers(tmp_path):
    env = SuspendEnv(tmp_path)
    env.plan("match", "match")
    term = io.StringIO()
    with a5.open_log(str(tmp_path / "v.log")) as log:
        code = a5.run_verify(
            "right-index-finger",
            "right-index-finger",
            "match",
            2,
            str(env.bin / "fprintd-verify"),
            str(env.bin / "journalctl"),
            a5.Output(term, log),
            0.05,
            stuck_s=60.0,  # the result is the sign of life; an upper bound only, never waited for here
        )
    assert code == 0 and term.getvalue().splitlines()[-1] == "as expected: 2/2"


def test_verify_machine_alive_flag():
    m = a5.VerifyMachine(rounds=2)
    m.start_round(1)
    assert not m.alive
    m.feed_client("Verify started!", 0.0)
    m.feed_journal("Sending command 0x02 (1 bytes)", 0.1)
    assert not m.alive
    m.feed_journal(ACT, 0.2)
    assert m.alive
    m.feed_client("Verify result: verify-match (done)", 0.3)
    m.end_round(0)
    m.start_round(2)
    assert not m.alive
    m.feed_journal(L6, 1.0)
    assert m.alive
    m.feed_journal(L1, 1.1)


# ------------------------------------------------------------------ pieces


def test_summary_lines():
    s = sus.SuspendSession(a5.Output(io.StringIO(), io.StringIO()), sus.Commands(), log_path="/x/a5-suspend-1.log")
    ok1 = sus.Part("1", 3, slept=True, ran_rounds=True, as_expected=3)
    bad1 = sus.Part("1", 3, slept=True, ran_rounds=True, as_expected=2, fail="2/3 checks as expected")
    ok2 = sus.Part("2", 2, slept=True, ran_rounds=True, as_expected=2, in_progress="interrupted")
    bad2 = sus.Part("2", 2, slept=True, ran_rounds=True, as_expected=1, fail="1/2 checks as expected")
    bad2.in_progress, bad2.result = "survived", "no match"
    lines, code = s.summary(ok1, ok2)
    assert code == 0 and lines[-1] == "Log: /x/a5-suspend-1.log"
    lines, code = s.summary(bad1, bad2)
    assert code == 1
    assert lines[1:4] == [
        "Part 1 (sleep while idle): FAIL (2/3 checks as expected)",
        "Part 2 (sleep during a check): the check in progress survived sleep (result: no match)",
        "Part 2: the reader worked again afterwards: no (1/2 as expected) — FAIL (1/2 checks as expected)",
    ]
    assert s.summary(ok1, None, "the laptop did not go to sleep in part 1")[1] == 1


def test_keyboard_reads_enter_without_blocking():
    r, w = os.pipe()
    with os.fdopen(r, "rb", buffering=0) as stream:
        keys = sus.Keyboard(stream)
        assert not keys.pressed()
        os.write(w, b"\n\n")
        assert keys.pressed() and keys.pressed()
        assert not keys.pressed()
        os.write(w, b"typed")
        assert not keys.pressed()
        os.close(w)
        assert keys.pressed() and keys.pressed()  # end of input counts as Enter, for good
        keys.discard()  # not a terminal: nothing to flush


def test_keyboard_without_stdin_counts_as_enter():
    assert sus.Keyboard(io.StringIO("")).pressed()


def test_find_device(tmp_path):
    env = SuspendEnv(tmp_path)
    assert sus.find_device(Path(env.usb)).name == "1-9"
    assert sus.find_device(tmp_path / "nowhere") is None


def test_sleep_offset_is_small_and_not_negative():
    assert sus.sleep_offset() >= -0.01


def test_ctrl_c_during_the_countdown_stops_every_child_and_exits_130(tmp_path):
    env = SuspendEnv(tmp_path)
    log_path = tmp_path / "s.log"
    # Through a launcher that gives the session the default Ctrl-C: a runner started in the background (a
    # shell's `&`) ignores SIGINT, its children inherit that, and Python then never raises KeyboardInterrupt.
    dfl = "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); os.execv(sys.argv[1], sys.argv[1:])"
    argv = [sys.executable, "-c", dfl, sys.executable, str(SUSPEND_PY), "--log", str(log_path)]
    argv += ["--journalctl", str(env.bin / "journalctl")]
    argv += ["--fprintd-verify", str(env.bin / "fprintd-verify"), "--systemctl", str(env.bin / "systemctl")]
    argv += ["--systemd-inhibit", str(env.bin / "systemd-inhibit"), "--usb-devices", env.usb]
    argv += ["--settle-s", "0.05", "--countdown-s", "10"]
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not list(env.state.glob("journal-*.pid")):
            assert time.monotonic() < deadline, "the journal follow did not start"
            time.sleep(0.05)
        time.sleep(0.5)  # into the countdown
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130, stdout + stderr
    assert "Traceback" not in stderr
    assert stdout.splitlines()[-1] == f"Interrupted: the suspend test stopped. The log so far: {log_path}"
    assert env.count("suspends") == 0
    for p in env.state.glob("journal-*.pid"):
        assert gone(int(p.read_text())), "the journal follow outlived the session"


# ------------------------------------------------------------------ the wrapper


@pytest.fixture
def senv(tmp_path):
    return SuspendEnv(tmp_path)


def test_wrapper_syntax_and_mode():
    subprocess.run(["bash", "-n", str(SUSPEND_SH)], check=True)
    assert os.access(SUSPEND_SH, os.X_OK)


def test_wrapper_help():
    r = subprocess.run(["bash", str(SUSPEND_SH), "--help"], capture_output=True, text=True, check=False)
    assert r.returncode == 0
    assert "never with sudo" in r.stdout and "set -euo" not in r.stdout
    assert "RIGHT INDEX finger" in r.stdout and "--dry-run" in r.stdout


def test_wrapper_rejects_unknown_arguments(senv):
    r = senv.run(SUSPEND_SH, "--rounds", "3")
    assert r.returncode == 2 and "unknown argument: --rounds" in r.stderr
    assert senv.called() == []


def test_wrapper_refuses_test_variables_as_root(senv):
    r = senv.run(SUSPEND_SH, "--dry-run", A5_FAKE_EUID="0")
    assert r.returncode == 2 and "test-only variables are refused as root" in r.stderr
    assert senv.called() == []


def test_dry_run_checks_and_starts_nothing(senv):
    r = senv.run(SUSPEND_SH, "--dry-run")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert "Checking the setup... ok" in lines
    assert "dry run: checks passed; nothing slept and fprintd-verify was not started. The test would:" in lines
    assert "  1. put the laptop to sleep, then 3 checks with your RIGHT INDEX finger" in lines
    assert [c.split()[0] for c in senv.called()] == ["systemctl", "journalctl", "busctl"]
    assert (
        "busctl --allow-interactive-authorization=no call org.freedesktop.login1 /org/freedesktop/login1 "
        "org.freedesktop.login1.Manager CanSuspend"
    ) in senv.called()
    assert senv.count("suspends") == 0 and senv.logs() == []


def test_dry_run_with_the_packaged_install(senv):
    """After the A6 hand-over: the package plus the debug drop-in pass; without the drop-in, one FAILED line."""
    senv.to_package()
    r = senv.run(SUSPEND_SH, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "Checking the setup... ok" in r.stdout
    senv.debug_dropin.unlink()
    r = senv.run(SUSPEND_SH, "--dry-run")
    assert r.returncode == 2, r.stdout
    assert r.stderr.splitlines() == [
        (
            f"FAILED: no debug drop-in at {senv.debug_dropin}: fprintd does not send the driver's lines to the"
            " journal; the prompts need them"
        ),
        "  hint: run: sudo tools/dev/a5-fprintd-dev.sh log on",
    ]
    assert senv.count("suspends") == 0


@pytest.mark.parametrize(
    "breakage, message",
    [
        ("no device", "the fingerprint reader (USB 06cb:0078) is not connected"),
        ("can suspend no", "this system does not let you put the laptop to sleep (logind says 'no')"),
        ("can suspend na", "logind says 'na'"),
        ("busctl fails", "cannot ask the system whether it may sleep (busctl failed: Failed to connect to bus"),
        ("busctl missing", "FAILED: busctl not found"),
        ("journal hidden", "cannot read fprintd's journal"),
    ],
)
def test_wrapper_preflight_failures(senv, breakage, message):
    extra = {}
    if breakage == "no device":
        (senv.root / "sys/bus/usb/devices/1-9/idProduct").write_text("0077\n")
    elif breakage == "can suspend no":
        extra = {"FAKE_CAN_SUSPEND": "no"}
    elif breakage == "can suspend na":
        extra = {"FAKE_CAN_SUSPEND": "na"}
    elif breakage == "busctl fails":
        extra = {"FAKE_BUSCTL_FAIL": "1"}
    elif breakage == "busctl missing":
        extra = {"A5_BUSCTL": str(senv.tmp / "nowhere" / "busctl")}
    elif breakage == "journal hidden":
        extra = {"FAKE_JOURNAL_HIDDEN": "1"}
    r = senv.run(SUSPEND_SH, "--dry-run", **extra)
    assert r.returncode == 2, r.stdout
    assert message in r.stderr
    assert len([ln for ln in r.stderr.splitlines() if ln.startswith("FAILED")]) == 1
    assert "Traceback" not in r.stderr and senv.count("suspends") == 0


@pytest.mark.parametrize("dry", [True, False])
def test_wrapper_challenge_refuses_in_one_plain_line(senv, dry):
    senv.enrolled.write_text("right-index-finger")
    r = senv.run(SUSPEND_SH, *(["--dry-run"] if dry else []), FAKE_CAN_SUSPEND="challenge")
    assert r.returncode == 2, r.stdout
    assert r.stderr.splitlines() == [
        "FAILED: suspend would ask for authentication, and this test does not support that"
    ]
    assert "WARN" not in r.stdout and "password" not in r.stdout
    assert senv.count("suspends") == 0 and senv.logs() == []
    assert not any(c.startswith(("fprintd-verify", "fprintd-list")) for c in senv.called())


def inhibitors(*rows) -> str:
    """logind's ListInhibitors as `busctl --json=short` prints it: (what, who, why, mode, uid, pid) rows."""
    import json

    return json.dumps({"type": "a(ssssuu)", "data": [[list(r) for r in rows]]})


# What this laptop really had while an update ran: delay-mode sleep inhibitors (they never block), one
# block-mode sleep inhibitor, and a block-mode inhibitor that is not about sleep.
INHIBITORS_REAL = inhibitors(
    ("sleep", "UPower", "Pause device polling", "delay", 0, 1732),
    ("sleep", "Omarchy", "Lock screen before suspend", "delay", 1000, 47644),
    ("sleep:idle", "omarchy-update", "Omarchy update in progress", "block", 0, 54782),
    ("shutdown", "packagekit", "Upgrading packages", "block", 0, 900),
)


@pytest.mark.parametrize("dry", [True, False])
def test_wrapper_names_what_blocks_sleep(senv, dry):
    senv.enrolled.write_text("right-index-finger")
    extra = {"FAKE_CAN_SUSPEND": "inhibited", "FAKE_INHIBITORS": INHIBITORS_REAL}
    r = senv.run(SUSPEND_SH, *(["--dry-run"] if dry else []), **extra)
    assert r.returncode == 2, r.stdout
    assert r.stderr.splitlines() == [
        "FAILED: sleep is blocked by: omarchy-update (Omarchy update in progress)",
        "  hint: finish or close it, then run this again",
    ]
    assert "UPower" not in r.stderr and "packagekit" not in r.stderr  # delay mode, and not about sleep
    assert senv.count("suspends") == 0 and senv.logs() == []
    assert not any(c.startswith(("fprintd-verify", "fprintd-list")) for c in senv.called())
    assert (
        "busctl --allow-interactive-authorization=no --json=short call org.freedesktop.login1 "
        "/org/freedesktop/login1 org.freedesktop.login1.Manager ListInhibitors"
    ) in senv.called()


def test_wrapper_names_every_sleep_blocker(senv):
    rows = inhibitors(
        ("idle:sleep", "backup", "Nightly\nbackup  running", "block", 1000, 41),
        ("sleep", "", "", "block-weak", 1000, 42),
        ("sleep", "backup", "Nightly backup running", "block", 1000, 41),  # the same twice: named once
        ("handle-lid-switch", "hypridle", "Lid", "block", 1000, 43),
    )
    r = senv.run(SUSPEND_SH, "--dry-run", FAKE_CAN_SUSPEND="inhibited", FAKE_INHIBITORS=rows)
    assert r.returncode == 2, r.stdout
    assert r.stderr.splitlines() == [
        "FAILED: sleep is blocked by: backup (Nightly backup running), process 42",
        "  hint: finish or close them, then run this again",
    ]


@pytest.mark.parametrize(
    "extra",
    [
        {"FAKE_INHIBITORS_FAIL": "1"},
        {"FAKE_INHIBITORS": "not json"},
        {"FAKE_INHIBITORS": '{"type":"a(ssssuu)","data":[[["sleep","x"]]]}'},  # an unexpected shape
        {"FAKE_INHIBITORS": inhibitors(("sleep", "UPower", "Pause device polling", "delay", 0, 1))},  # no blocker
    ],
)
def test_wrapper_blocked_sleep_falls_back_to_the_plain_line(senv, extra):
    r = senv.run(SUSPEND_SH, "--dry-run", FAKE_CAN_SUSPEND="inhibited", **extra)
    assert r.returncode == 2, r.stdout
    assert r.stderr.splitlines() == [
        "FAILED: this system does not let you put the laptop to sleep (logind says 'inhibited')"
    ]
    assert "Traceback" not in r.stderr and senv.count("suspends") == 0


def test_wrapper_full_run(senv):
    senv.enrolled.write_text("right-index-finger")
    senv.plan("match", "match", "match", "wait-error", "match", "match")
    senv.flag("journal-resume")  # the real clock shows no sleep here: the journal line is the signal
    # sudo and pkexec on PATH that only record a call: none may happen.
    for name in ("sudo", "pkexec"):
        fake(
            senv.bin / name,
            f"import sys\nopen({str(senv.calls)!r}, 'a').write('{name} ' + ' '.join(sys.argv[1:]) + '\\n')\n",
        )
    path = f"{senv.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
    r = senv.run(SUSPEND_SH, stdin="\n\n", PATH=path)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert suspend_terminal_bad(lines) == []
    assert_quiet(r.stdout)
    assert lines[0] == "A5 suspend: does the reader still work after the laptop sleeps? Every check uses your " + (
        "RIGHT INDEX finger."
    )
    assert sus.MSG_INTERRUPTED in lines
    assert lines[-2] == "Part 2: the reader worked again afterwards: yes (2/2 as expected) — PASS"
    (log,) = senv.logs()
    assert log.name.startswith("a5-suspend-") and lines[-1] == f"Log: {log}"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    text = log.read_text()
    assert "# preflight: reader in sysfs: 1-9" in text and "# preflight: logind CanSuspend: yes" in text
    assert text.count("[resume] resume detected first by: journal line") == 2
    assert f"[journal:before-suspend] {SERIAL}" in text and EP83 in text
    calls = senv.called()
    assert calls.count("systemctl suspend --no-ask-password") == 2
    assert (senv.state / "suspend-how").read_text() == "stdin=/dev/null own-session=True"
    assert [c for c in calls if c.startswith("fprintd-verify")] == ["fprintd-verify -f right-index-finger"] * 6
    assert not any("restart" in c for c in calls)
    assert not any(c.startswith(("sudo", "pkexec")) for c in calls)


def test_wrapper_refuses_a_real_run_without_a_terminal(senv):
    """Without A5_TEST_ROOT and with stdin not a terminal: one plain line, before anything is run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("A5_", "LIBFPRINT_FORK", "FAKE_"))}
    # Recording fakes first on PATH: none may be called (the refusal comes before the preflight).
    env["PATH"] = f"{senv.bin}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}"
    r = subprocess.run(
        ["bash", str(SUSPEND_SH)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert r.stderr.splitlines() == ["FAILED: run this in a terminal: it waits for you to press Enter after each sleep"]
    assert r.stdout == ""
    assert senv.called() == []


def test_wrapper_finger_not_enrolled(senv):
    senv.enrolled.write_text("left-index-finger")
    r = senv.run(SUSPEND_SH)
    assert r.returncode == 2
    assert "right-index-finger is not enrolled" in r.stderr
    assert senv.count("suspends") == 0 and not any(c.startswith("fprintd-verify") for c in senv.called())


# ------------------------------------------------------------------ never sudo, pkexec or "Diag image"


def code_strings(path: Path) -> list[str]:
    """The string literals of a Python file, without its docstrings (the code's own words)."""
    import ast

    tree = ast.parse(path.read_text())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
    ]


def shell_code(path: Path) -> list[str]:
    """The shell lines that are not comments, without their quoted strings (the words the user reads)."""
    lines = [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]
    return [re.sub(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'', '""', ln) for ln in lines]


def test_nothing_runs_sudo_or_pkexec():
    for path in (SUSPEND_PY, HERE / "dev" / "a5_session.py"):
        for text in code_strings(path):
            assert not re.match(r"\s*(sudo|pkexec)\b", text), (path.name, text)
            assert "pkexec" not in text, (path.name, text)
    for path in (SUSPEND_SH, HERE / "dev" / "a5-session-lib.sh"):
        for line in shell_code(path):
            # sudo only inside the hints the user reads ("run: sudo ..."), never as a command.
            assert "pkexec" not in line, line
            assert not re.search(r"(^|[;&|(]|\$\()\s*sudo\b", line), line


def test_nothing_depends_on_diag_image_lines():
    for path in (SUSPEND_PY, HERE / "dev" / "a5_session.py"):
        assert not any("Diag" in text for text in code_strings(path)), path.name
    for path in (SUSPEND_SH, HERE / "dev" / "a5-session-lib.sh"):
        assert not any("Diag" in line for line in shell_code(path)), path.name


def test_session_has_no_password_path():
    """The challenge-mode suspend (a password question on the terminal) is gone for good."""
    import inspect

    assert "challenge" not in inspect.signature(sus.SuspendSession).parameters
    assert sus.SUSPEND_ARGV == ["suspend", "--no-ask-password"]
    with pytest.raises(SystemExit):
        sus.main(["--log", "/dev/null", "--can-suspend", "challenge"])
