"""Offline tests of tools/sddm-fingerprint.sh (fingerprint login at the SDDM login screen).

No root and no real system file: the PAM file, fprintd's state dir and the PAM module dir
all live under pytest's tmp_path (SDDM_FP_TEST_PAM_FILE, SDDM_FP_TEST_FPRINT_DIR,
SDDM_FP_TEST_MODULE_DIR); the enrolled print is a synthetic file. The script passes its root
check under these hooks; SDDM_FP_FAKE_EUID makes it see a normal user instead.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import select
import shlex
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "sddm-fingerprint.sh"
TEST_VARS = ["SDDM_FP_TEST_PAM_FILE", "SDDM_FP_TEST_FPRINT_DIR", "SDDM_FP_TEST_MODULE_DIR", "SDDM_FP_FAKE_EUID"]
USER = "alice"
DEVICE_ID = "0123456789ab"  # synthetic: a real device id can embed the unit serial

# Arch's /etc/pam.d/sddm (the pam_ssh line has a real TAB after "auth").
FIXTURE = """#%PAM-1.0

auth        include     system-login
-auth       optional    pam_gnome_keyring.so
auth\t    optional    pam_ssh.so              try_first_pass

#-auth       optional    pam_kwallet5.so

account     include     system-login

password    include     system-login
-password   optional    pam_gnome_keyring.so    use_authtok

session     optional    pam_keyinit.so          force revoke
session     include     system-login
session     optional    pam_ssh.so

-session    optional    pam_gnome_keyring.so    auto_start
#-session    optional    pam_kwallet5.so         auto_start
"""
ANCHOR = "auth        include     system-login\n"
BLOCK_LINES = [
    "# synaptics0078: fingerprint login (tools/sddm-fingerprint.sh) begin",
    "auth        [success=1 new_authtok_reqd=1 default=ignore]  pam_unix.so  try_first_pass likeauth nullok",
    "auth        sufficient  pam_fprintd.so  timeout=10",
    "# synaptics0078: fingerprint login end",
]
BLOCK = "".join(f"{ln}\n" for ln in BLOCK_LINES)
FIXTURE_ON = FIXTURE.replace(ANCHOR, BLOCK + ANCHOR, 1)

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="the test hooks are refused as root")


def mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


class Env:
    """A PAM file holding FIXTURE, the two PAM modules and one enrolled print for USER."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.pam_dir = tmp / "etc" / "pam.d"
        self.pam_dir.mkdir(parents=True)
        self.pam = self.pam_dir / "sddm"
        self.pam.write_text(FIXTURE)
        self.pam.chmod(0o644)
        self.backup = self.pam_dir / "sddm.before-fingerprint"
        self.modules = tmp / "usr" / "lib" / "security"
        self.modules.mkdir(parents=True)
        for m in ("pam_fprintd.so", "pam_unix.so"):
            (self.modules / m).write_bytes(b"\x7fELF synthetic")
        self.fprint = tmp / "var" / "lib" / "fprint"
        self.user_dir = self.fprint / USER
        self.print_file = self.user_dir / "synaptics0078" / DEVICE_ID / "7"
        self.print_file.parent.mkdir(parents=True)
        self.print_file.write_bytes(b"synthetic print")

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SDDM_FP_", "SUDO_"))}
        env.update(
            SDDM_FP_TEST_PAM_FILE=str(self.pam),
            SDDM_FP_TEST_FPRINT_DIR=str(self.fprint),
            SDDM_FP_TEST_MODULE_DIR=str(self.modules),
            SUDO_USER=USER,
        )
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    def run(self, *args, script=SCRIPT, cwd=None, **extra):
        return subprocess.run(
            ["bash", str(script), *args],
            capture_output=True,
            text=True,
            env=self.env(**extra),
            check=False,
            cwd=cwd,
        )

    def pam_bytes(self) -> bytes:
        return self.pam.read_bytes()

    def pam_dir_names(self) -> list[str]:
        """What the PAM dir holds: a refused or finished run leaves no temp file behind."""
        return sorted(p.name for p in self.pam_dir.iterdir())


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def assert_unchanged(e: Env, before: bytes, backup=False):
    assert e.pam_bytes() == before
    assert e.pam_dir_names() == (["sddm", "sddm.before-fingerprint"] if backup else ["sddm"])


# ------------------------------------------------------------------ static checks


def test_bash_syntax_and_mode():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert mode(SCRIPT) == 0o755


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck():
    subprocess.run(["shellcheck", "-x", str(SCRIPT)], check=True)


def test_help_shows_the_subcommands_and_no_test_switch():
    for flag in ("-h", "--help"):
        r = subprocess.run(["bash", str(SCRIPT), flag], capture_output=True, text=True, check=False)
        assert r.returncode == 0 and r.stderr == ""
        for word in ("sddm-fingerprint.sh on", "sddm-fingerprint.sh off", "sddm-fingerprint.sh status"):
            assert word in r.stdout
        for var in TEST_VARS:
            assert var not in r.stdout
        assert "TEST" not in r.stdout and "SDDM_FP" not in r.stdout


@pytest.mark.parametrize(
    "args, message",
    [((), "no subcommand"), (("bogus",), "unknown argument: bogus"), (("on", "off"), "more than one subcommand")],
)
def test_usage_errors(env, args, message):
    r = env.run(*args)
    assert r.returncode == 2 and message in r.stderr
    assert_unchanged(env, FIXTURE.encode())


# ------------------------------------------------------------------ on / off


def test_on_off_round_trip_is_byte_identical(env):
    r = env.run("on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.pam.read_text() == FIXTURE_ON
    assert mode(env.pam) == 0o644 and not env.pam.is_symlink()
    lines = env.pam.read_text().splitlines()
    at = lines.index(ANCHOR.rstrip("\n"))
    assert lines[at - 4 : at] == BLOCK_LINES
    assert "Fingerprint login at the login screen is on." in r.stdout
    assert env.pam_dir_names() == ["sddm", "sddm.before-fingerprint"]

    r = env.run("off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.pam_bytes() == FIXTURE.encode()
    assert mode(env.pam) == 0o644
    assert "Fingerprint login at the login screen is off." in r.stdout
    assert env.pam_dir_names() == ["sddm", "sddm.before-fingerprint"]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(FIXTURE.rstrip("\n"), id="no-final-newline"),
        pytest.param(FIXTURE[FIXTURE.index(ANCHOR) :], id="anchor-on-line-1"),
        pytest.param(FIXTURE.replace(ANCHOR, "auth\tinclude\t system-login  \t\n"), id="tabs-in-anchor"),
        pytest.param(FIXTURE.replace("\n\n", "\n  \t\n") + "\n\n", id="blank-lines-with-spaces"),
    ],
)
def test_round_trip_keeps_every_byte(env, text):
    env.pam.write_text(text)
    r = env.run("on")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = env.pam.read_text().splitlines()
    at = next(i for i, ln in enumerate(lines) if ln.startswith("auth") and "system-login" in ln and "include" in ln)
    assert lines[at - 4 : at] == BLOCK_LINES
    assert env.backup.read_text() == text
    assert env.run("off").returncode == 0
    assert env.pam.read_text() == text


def test_crlf_file_round_trips_byte_identically(env):
    data = FIXTURE.replace("\n", "\r\n").encode()
    env.pam.write_bytes(data)
    r = env.run("on")
    assert r.returncode == 0, r.stdout + r.stderr
    on = env.pam_bytes()
    at = on.index(ANCHOR.rstrip("\n").encode() + b"\r\n")
    assert on[:at].endswith(BLOCK.encode())
    assert env.backup.read_bytes() == data
    r = env.run("off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.pam_bytes() == data


def test_two_anchor_lines_block_goes_before_the_first_only(env):
    text = FIXTURE.replace(ANCHOR, ANCHOR + "auth        optional    pam_env.so\n" + ANCHOR, 1)
    env.pam.write_text(text)
    r = env.run("on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.pam.read_text() == text.replace(ANCHOR, BLOCK + ANCHOR, 1)
    assert env.pam.read_text().count(BLOCK) == 1
    assert env.run("off").returncode == 0
    assert env.pam_bytes() == text.encode()


@pytest.mark.parametrize("lookalike", ["auth include system-loginX", "auth        include     system-login-extra"])
def test_a_lookalike_anchor_is_not_used(env, lookalike):
    text = FIXTURE.replace(ANCHOR, lookalike + "\n" + ANCHOR, 1)
    env.pam.write_text(text)
    r = env.run("on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.pam.read_text() == text.replace(ANCHOR, BLOCK + ANCHOR, 1)
    assert env.run("off").returncode == 0
    assert env.pam_bytes() == text.encode()
    # Alone, the lookalike is no anchor: on refuses.
    text = FIXTURE.replace(ANCHOR, lookalike + "\n", 1)
    env.pam.write_text(text)
    r = env.run("on")
    assert r.returncode == 2 and "system-login" in r.stderr and "nothing was changed" in r.stderr
    assert_unchanged(env, text.encode(), backup=True)


def test_on_twice_and_off_twice_are_idempotent(env):
    assert env.run("on").returncode == 0
    on_bytes = env.pam_bytes()
    r = env.run("on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already on" in r.stdout and "nothing was changed" in r.stdout
    assert_unchanged(env, on_bytes, backup=True)

    assert env.run("off").returncode == 0
    r = env.run("off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already off" in r.stdout and "nothing was changed" in r.stdout
    assert_unchanged(env, FIXTURE.encode(), backup=True)


def test_off_without_on_changes_nothing(env):
    r = env.run("off")
    assert r.returncode == 0 and "already off" in r.stdout
    assert_unchanged(env, FIXTURE.encode())


def test_backup_is_made_on_first_on_and_never_overwritten(env):
    assert env.run("on").returncode == 0
    assert env.backup.read_bytes() == FIXTURE.encode() and mode(env.backup) == 0o644
    # A backup that differs from the file (an older original) survives on/off/on.
    env.backup.write_text("# an older original\n")
    for sub in ("off", "on", "off", "on"):
        r = env.run(sub)
        assert r.returncode == 0, r.stdout + r.stderr
        assert env.backup.read_text() == "# an older original\n"
    assert "kept the existing backup" in r.stdout
    assert env.pam_dir_names() == ["sddm", "sddm.before-fingerprint"]


def test_preexisting_backup_is_kept_on_first_on(env):
    env.backup.write_text("# someone else's copy\n")
    r = env.run("on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.backup.read_text() == "# someone else's copy\n"
    assert env.pam.read_text() == FIXTURE_ON


# ------------------------------------------------------------------ refusals of on


def test_on_refuses_without_the_anchor_line(env):
    text = FIXTURE.replace(ANCHOR, "auth        include     system-local-login\n")
    env.pam.write_text(text)
    r = env.run("on")
    assert r.returncode == 2 and "system-login" in r.stderr and "nothing was changed" in r.stderr
    assert_unchanged(env, text.encode())


def test_on_ignores_a_commented_anchor(env):
    text = "#%PAM-1.0\n#auth include system-login\n-auth optional pam_gnome_keyring.so\n"
    env.pam.write_text(text)
    r = env.run("on")
    assert r.returncode == 2 and "nothing was changed" in r.stderr
    assert_unchanged(env, text.encode())


@pytest.mark.parametrize("module", ["pam_fprintd.so", "pam_unix.so"])
def test_on_refuses_without_a_pam_module(env, module):
    (env.modules / module).unlink()
    r = env.run("on")
    assert r.returncode == 2 and f"{module} is missing" in r.stderr
    assert_unchanged(env, FIXTURE.encode())


@pytest.mark.parametrize("case", ["empty-user-dir", "no-user-dir", "only-dirs"])
def test_on_refuses_without_an_enrolled_print(env, case):
    shutil.rmtree(env.user_dir)
    if case == "empty-user-dir":
        env.user_dir.mkdir()
    elif case == "only-dirs":
        env.print_file.parent.mkdir(parents=True)
    r = env.run("on")
    assert r.returncode == 2
    assert f"{USER} has no fingerprint enrolled" in r.stderr and "fprintd-enroll" in r.stderr
    assert str(env.fprint) not in r.stdout + r.stderr
    assert_unchanged(env, FIXTURE.encode())


def test_on_checks_the_sudo_user_not_another_one(env):
    r = env.run("on", SUDO_USER="bob")
    assert r.returncode == 2 and "bob has no fingerprint enrolled" in r.stderr
    assert_unchanged(env, FIXTURE.encode())


def test_on_refuses_without_sudo_user(env):
    r = env.run("on", SUDO_USER=None)
    assert r.returncode == 2 and "SUDO_USER is not set" in r.stderr
    assert_unchanged(env, FIXTURE.encode())


@pytest.mark.parametrize("user", ["../alice", "alice/..", "..", "a b", "-alice", ""])
def test_on_refuses_a_sudo_user_that_is_not_a_plain_name(env, user):
    # "../alice" would reach a print dir if it were used as a path.
    (env.fprint / "x").mkdir()
    r = env.run("on", SUDO_USER=user)
    assert r.returncode == 2 and "nothing was changed" in r.stderr
    assert_unchanged(env, FIXTURE.encode())


def test_on_refuses_a_symlinked_pam_file(env):
    real = env.tmp / "real-sddm"
    real.write_text(FIXTURE)
    env.pam.unlink()
    env.pam.symlink_to(real)
    for sub in ("on", "off"):
        r = env.run(sub)
        assert r.returncode == 2 and "symlink" in r.stderr
    assert env.pam.is_symlink() and real.read_text() == FIXTURE
    assert env.pam_dir_names() == ["sddm"]


def test_on_and_off_refuse_a_missing_pam_file(env):
    env.pam.unlink()
    for sub in ("on", "off"):
        r = env.run(sub)
        assert r.returncode == 2 and "does not exist" in r.stderr
    assert env.pam_dir_names() == []


@pytest.mark.parametrize(
    "line",
    ["auth        sufficient  pam_fprintd.so", "AUTH sufficient pam_fprintd.so", "-Auth\tsufficient pam_fprintd.so"],
)
def test_on_refuses_a_hand_made_pam_fprintd_line(env, line):
    text = FIXTURE.replace(ANCHOR, line + "\n" + ANCHOR)
    env.pam.write_text(text)
    r = env.run("on")
    assert r.returncode == 2 and "pam_fprintd.so outside" in r.stderr
    assert_unchanged(env, text.encode())
    r = env.run("status")
    assert r.returncode == 0 and "outside its lines" in r.stdout


SIGNALS = [signal.SIGTERM, signal.SIGHUP, signal.SIGINT]


def start_on(env: Env, **extra) -> subprocess.Popen:
    """Start `on` in a group of its own (the signal tests signal that group). SIGINT is reset to
    its default in the child: pytest started with SIGINT ignored (a background job of a
    non-interactive shell) would pass that on, and bash cannot trap a signal ignored at entry."""
    return subprocess.Popen(
        ["bash", str(SCRIPT), "on"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env.env(**extra),
        start_new_session=True,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),  # noqa: PLW1509 (one thread)
    )


def reap(proc: subprocess.Popen) -> str:
    """Kill what is left of the run's group and return its stderr, unless the run was already
    reaped with its pipes at EOF (then no process of the group holds them, and the group id may
    be reused)."""
    if proc.returncode is not None:
        return ""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return proc.communicate(timeout=10)[1]


@contextlib.contextmanager
def held_run(env: Env, name: str, before: str = "", then: str = "exec sleep 30"):
    """Run `on` with a fake `name` on PATH that runs `before`, says so on a FIFO, then runs
    `then` (by default it hangs until a signal to the group ends it). Yields the run once the
    fake has said so: a signal then lands in the fake, however loaded the machine is (no
    polling), and not in the gap after bash forks a command and before it runs, where bash
    keeps the signal until that command ends. On the way out the group is always killed and
    reaped and the FIFO removed."""
    fakebin = env.tmp / "bin"
    fakebin.mkdir()
    fifo = env.tmp / "held.fifo"
    os.mkfifo(fifo)
    fake = fakebin / name
    fake.write_text(f"#!/bin/sh\n{before}echo held >{shlex.quote(str(fifo))}\n{then}\n")
    fake.chmod(0o755)
    # Read and write end in one: the fake's open never blocks and the FIFO never reads EOF.
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    proc = None
    try:
        proc = start_on(env, PATH=f"{fakebin}:{os.environ['PATH']}")
        # The pidfd turns readable if the run ends before it reaches the fake.
        pidfd = os.pidfd_open(proc.pid)
        try:
            ready, _, _ = select.select([fd, pidfd], [], [], 10)
        finally:
            os.close(pidfd)
        if fd not in ready:
            err = reap(proc)
            pytest.fail(f"the run never reached the fake {name} (exit {proc.returncode}): {err}")
        assert os.read(fd, 64) == b"held\n"
        yield proc
    finally:
        os.close(fd)
        if proc is not None:
            reap(proc)
        fifo.unlink(missing_ok=True)


def end_held(env: Env, proc: subprocess.Popen, sig: int) -> None:
    """Send sig to the held run's group; the run must end through its trap (exit 2) and leave
    the PAM dir as it was."""
    os.killpg(proc.pid, sig)
    _, err = proc.communicate(timeout=10)
    assert proc.returncode == 2, err
    assert_unchanged(env, FIXTURE.encode())


@pytest.mark.parametrize("sig", SIGNALS)
def test_a_signal_while_writing_leaves_no_temp_file(env, sig):
    # A sync that hangs holds the run while the backup's temp file exists.
    with held_run(env, "sync") as proc:
        assert [n for n in env.pam_dir_names() if ".fingerprint-" in n], "no temp file while sync runs"
        end_held(env, proc, sig)


@pytest.mark.parametrize("sig", SIGNALS)
def test_a_signal_while_mktemp_runs_leaves_no_temp_file(env, sig):
    # mktemp has made the file but is killed before its name reaches the script: the run must
    # still remove it.
    real = shlex.quote(shutil.which("mktemp") or "mktemp")
    with held_run(env, "mktemp", f'{real} "$@" >/dev/null || exit 1\n') as proc:
        assert [n for n in env.pam_dir_names() if ".fingerprint-" in n], "no temp file while mktemp runs"
        end_held(env, proc, sig)


def test_a_run_without_the_lock_leaves_temp_files_alone(env):
    # A run stopped while it waits for the lock removes no temp-like file: it can be another
    # run's. The fake flock says so when the run starts waiting (flock -w), then waits for real.
    other = env.pam_dir / ".sddm.fingerprint-new.Ab12Cd"
    other.write_text("another run's new PAM file\n")
    real = shlex.quote(shutil.which("flock") or "flock")
    fd = os.open(env.pam_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with held_run(env, "flock", f'[ "$1" = -w ] || exec {real} "$@"\n', f'exec {real} "$@"') as proc:
            os.killpg(proc.pid, signal.SIGTERM)
            _, err = proc.communicate(timeout=10)
            assert proc.returncode == 2 and "waiting for another run" in err, err
    finally:
        os.close(fd)
    assert other.read_text() == "another run's new PAM file\n"
    assert env.pam_bytes() == FIXTURE.encode()
    assert env.pam_dir_names() == sorted(["sddm", other.name])


@pytest.mark.parametrize("sub", ["on", "off"])
def test_a_run_holding_the_lock_removes_stale_temp_files_only(env, sub):
    # Leftovers of a killed run go; a symlink with such a name and other names stay.
    stale = [env.pam_dir / ".sddm.fingerprint-new.Ab12Cd", env.pam_dir / ".sddm.fingerprint-backup.xY9zW8"]
    for p in stale:
        p.write_text("left by a killed run\n")
    target = env.tmp / "target"
    target.write_text("not ours\n")
    link = env.pam_dir / ".sddm.fingerprint-new.LnK123"
    link.symlink_to(target)
    kept = [".sddm.fingerprint-new.toolong7", ".other.fingerprint-new.Ab12Cd"]
    for n in kept:
        (env.pam_dir / n).write_text("not a temp file of ours\n")
    r = env.run(sub)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(p.exists() for p in stale)
    assert link.is_symlink() and target.read_text() == "not ours\n"
    expect = ["sddm", link.name, *kept] + (["sddm.before-fingerprint"] if sub == "on" else [])
    assert env.pam_dir_names() == sorted(expect)


# ------------------------------------------------------------------ changed by hand

CHANGED = {
    "middle-line-edited": FIXTURE_ON.replace("timeout=10", "timeout=30"),
    "end-marker-gone": FIXTURE_ON.replace(BLOCK_LINES[3] + "\n", ""),
    "begin-marker-gone": FIXTURE_ON.replace(BLOCK_LINES[0] + "\n", ""),
    "extra-line-inside": FIXTURE_ON.replace(BLOCK_LINES[2] + "\n", BLOCK_LINES[2] + "\n# mine\n"),
    "two-blocks": FIXTURE_ON.replace(BLOCK, BLOCK + BLOCK),
    "trailing-space": FIXTURE_ON.replace(BLOCK_LINES[1], BLOCK_LINES[1] + " "),
}


@pytest.mark.parametrize("case", sorted(CHANGED))
def test_block_changed_by_hand_is_reported_and_left_alone(env, case):
    text = CHANGED[case]
    env.pam.write_text(text)
    r = env.run("status")
    assert r.returncode == 1 and "changed by hand" in r.stdout
    for sub in ("off", "on"):
        r = env.run(sub)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "changed by hand" in r.stderr and "nothing was changed" in r.stderr
        assert "synaptics0078: fingerprint login" in r.stderr  # it names the markers
        assert_unchanged(env, text.encode())


def test_changed_by_hand_points_at_the_backup(env):
    assert env.run("on").returncode == 0
    env.pam.write_text(CHANGED["middle-line-edited"])
    r = env.run("off")
    assert r.returncode == 2 and str(env.backup) in r.stderr
    assert env.pam.read_text() == CHANGED["middle-line-edited"]


# ------------------------------------------------------------------ status


def status_line(r) -> str:
    (line,) = [ln for ln in r.stdout.splitlines() if ln.startswith("Fingerprint login at the login screen: ")]
    return line.removeprefix("Fingerprint login at the login screen: ")


def test_status_off_on_and_backup(env):
    r = env.run("status")
    assert r.returncode == 0 and status_line(r).startswith("off ")
    assert "Backup of the original: none yet" in r.stdout
    assert env.run("on").returncode == 0
    r = env.run("status")
    assert r.returncode == 0 and status_line(r).startswith("on ")
    assert f"Backup of the original: {env.backup} (present)" in r.stdout
    assert env.run("off").returncode == 0
    r = env.run("status")
    assert r.returncode == 0 and status_line(r).startswith("off ")
    assert f"Backup of the original: {env.backup} (present)" in r.stdout


def test_status_missing_pam_file(env):
    env.pam.unlink()
    r = env.run("status")
    assert r.returncode == 1 and "missing" in status_line(r)
    assert "Backup of the original: none yet" in r.stdout


def test_status_needs_no_root_and_no_fprint_dir(env):
    shutil.rmtree(env.fprint)
    r = env.run("status", SDDM_FP_FAKE_EUID="1000", SUDO_USER=None)
    assert r.returncode == 0 and status_line(r).startswith("off ")


# ------------------------------------------------------------------ root and test hooks


@pytest.mark.parametrize("sub", ["on", "off"])
def test_refuses_without_root(env, sub):
    if sub == "off":
        env.pam.write_text(FIXTURE_ON)
    before = env.pam_bytes()
    r = env.run(sub, SDDM_FP_FAKE_EUID="1000")
    assert r.returncode == 2
    assert f"run it with sudo: sudo {SCRIPT} {sub}" in r.stderr and "nothing was changed" in r.stderr
    assert_unchanged(env, before)


@pytest.mark.parametrize("var", TEST_VARS)
def test_refuses_test_hooks_as_root(env, var):
    # As root (faked: SDDM_FP_FAKE_EUID=0, which a real root run refuses too) any test-only
    # variable stops every subcommand before anything else.
    run_env = {k: v for k, v in os.environ.items() if not k.startswith(("SDDM_FP_", "SUDO_"))}
    run_env[var] = str(env.pam) if var != "SDDM_FP_FAKE_EUID" else "0"
    run_env["SDDM_FP_FAKE_EUID"] = "0"
    for sub in ("on", "off", "status", "--help"):
        r = subprocess.run(["bash", str(SCRIPT), sub], capture_output=True, text=True, env=run_env, check=False)
        assert r.returncode == 2 and "refused as root" in r.stderr and r.stdout == ""
    assert_unchanged(env, FIXTURE.encode())


def test_relative_test_pam_file_is_refused(env):
    r = env.run("status", SDDM_FP_TEST_PAM_FILE="etc/pam.d/sddm")
    assert r.returncode == 2 and "absolute" in r.stderr


def test_an_exported_cdpath_does_not_break_it(env):
    # A CDPATH entry holding a "tools" dir would send `cd tools` there (and make cd print it).
    decoy = env.tmp / "decoy"
    (decoy / "tools").mkdir(parents=True)
    for args in (("--help",), ("status",)):
        r = env.run(*args, script="tools/sddm-fingerprint.sh", cwd=HERE.parent, CDPATH=str(decoy))
        assert r.returncode == 0 and r.stderr == "", r.stdout + r.stderr
        assert "sddm-fingerprint.sh on" in r.stdout
        assert str(decoy) not in r.stdout


def test_concurrent_on_waits_for_the_lock(env):
    # on and off hold flock(2) on the PAM file's directory; another holder makes on wait.
    fd = os.open(env.pam_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        proc = subprocess.Popen(
            ["bash", str(SCRIPT), "on"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env.env()
        )
        time.sleep(0.5)
        assert proc.poll() is None
        assert_unchanged(env, FIXTURE.encode())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, out + err
    assert "waiting for another run" in err
    assert env.pam.read_text() == FIXTURE_ON


# ------------------------------------------------------------------ what on prints


@pytest.mark.parametrize(
    "cwd, script",
    [
        (HERE.parent, "tools/sddm-fingerprint.sh"),  # from the project root
        (HERE, "sddm-fingerprint.sh"),  # bash sddm-fingerprint.sh in tools/
        (HERE, "../tools/sddm-fingerprint.sh"),
        (None, str(SCRIPT)),
    ],
)
def test_next_steps_and_a_runnable_undo_line(env, cwd, script):
    r = env.run("on", script=script, cwd=cwd)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[-3:] == [
        (
            "At the login screen: type your password as usual, or leave it empty, press Enter and touch the reader"
            " within 10 s."
        ),
        f"Undo: sudo {shlex.quote(str(SCRIPT))} off",
        "If the login screen ever refuses you: Ctrl+Alt+F3, log in there, run the undo.",
    ]
    (undo,) = [ln for ln in r.stdout.splitlines() if ln.startswith("Undo: sudo ")]
    cmd = shlex.split(undo.removeprefix("Undo: sudo "))
    assert cmd == [str(SCRIPT), "off"]
    # Run as printed, minus sudo (the test hooks stand in for root), from elsewhere: the
    # recovery console starts in the home directory, not where on was run.
    ran = subprocess.run(cmd, cwd=env.tmp, capture_output=True, text=True, env=env.env(), check=False)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "command not found" not in ran.stderr
    assert env.pam_bytes() == FIXTURE.encode()
    assert f"(turn it on: sudo {shlex.quote(str(SCRIPT))} on)" in env.run("status", script=script, cwd=cwd).stdout
    bad = env.run("--bogus", script=script, cwd=cwd)
    assert f"  hint: see {shlex.quote(str(SCRIPT))} --help" in bad.stderr


def test_output_names_no_path_inside_the_fprint_dir(env):
    for sub in ("on", "status", "on", "off"):
        r = env.run(sub)
        assert r.returncode == 0, r.stdout + r.stderr
        out = r.stdout + r.stderr
        assert str(env.fprint) not in out
        assert DEVICE_ID not in out and "synaptics0078/" not in out
