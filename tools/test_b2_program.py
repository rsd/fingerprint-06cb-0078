"""Offline tests of tools/dev/b2-program.sh (+ tools/dev/b2_program.py), the B2b program swap.

No root, no systemd, no USB: every system path lives under a test root (B2_TEST_ROOT), systemctl is a
stub that records its argv (B2_SYSTEMCTL), and the programs are synthetic: built with
calibration.build_cmd02 from made-up inputs and the synthetic template (synaptics0078/tests/fixtures.py),
which the script checks them against (B2_TEST_TEMPLATE) instead of the pinned proto/data one. So every
test runs on a clean clone too. The script is run as a user runs it (bash, from the project root);
every run is checked for the serial.
"""

from __future__ import annotations

import importlib.util
import os
import py_compile
import shlex
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
SCRIPT_REL = "tools/dev/b2-program.sh"
PY_SCRIPT = HERE / "dev" / "b2_program.py"
sys.path.insert(0, str(PROJECT / "proto"))

import calibration  # proto/calibration.py, on sys.path above; puts the repo root on sys.path

from synaptics0078.tests import fixtures as fx


def load_b2():
    """tools/dev/b2_program.py in-process, for the tests of its pieces (no bytecode written)."""
    spec = importlib.util.spec_from_file_location("b2_program", PY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["b2_program"] = module  # its dataclasses look it up here
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = keep
    return module


b2 = load_b2()

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="the test hooks are refused as root")

SERIAL = "5e7a1c0ffee5"  # synthetic, not a real unit's
SERIAL_2 = "abcdef012345"
SYS_DATA_DIR = "var/lib/fprint/synaptics0078"
DROPIN_DIR = "etc/systemd/system/fprintd.service.d"
STAMP = "20261017-000000"
STAMP_2 = "20261017-111111"
TEST_VARS = ["B2_TEST_ROOT", "B2_SYSTEMCTL", "B2_TEST_TEMPLATE", "B2_FAKE_EUID"]
PROG = f"program-{SERIAL}.bin"
BACKUP = PROG + ".b2-windows-backup"
VERIFY_RIGHT = "tools/dev/a5-verify.sh --finger-used right-index --expect match --finger right-index-finger"
VERIFY_LEFT = "tools/dev/a5-verify.sh --finger-used left-index --expect match --finger left-index-finger"

STUB_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "systemctl $*" >>"$STUB_LOG"
case $1 in
is-active)
	echo "${STUB_STATE:-inactive}"
	[ "${STUB_STATE:-inactive}" = active ]
	;;
stop) exit "${STUB_STOP_RC:-0}" ;;
esac
"""


SYNTH_TEMPLATE = fx.expected_template()  # random table bytes under the real layout; never the real one


def program(seed: int, template: bytes = SYNTH_TEMPLATE) -> bytes:
    factory = bytes(0x70 + (i * 7 + seed) % 32 for i in range(56))  # packs to exactly 5 bits
    frame = bytes((0x80 + ((i * 37 + seed * 11) % 61) - 30) & 0xFF for i in range(calibration.FRAME_LEN))
    baseline = bytes(128 + ((i + seed) % 5) - 2 for i in range(calibration.FRAME_LEN))
    return calibration.build_cmd02(calibration.CalibrationInputs(factory, frame, baseline), template)


WINDOWS, LINUX, OTHER = program(0), program(1), program(2)


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = tmp / "root"
        self.state = self.root / "var/lib/fprint"
        self.data = self.root / SYS_DATA_DIR
        self.data.mkdir(parents=True, mode=0o700)
        self.state.chmod(0o700)
        self.data.chmod(0o700)
        self.program = self.data / PROG
        self.backup = self.data / BACKUP
        self.program.write_bytes(WINDOWS)
        self.program.chmod(0o640)  # not the default: the mode must be kept, whatever it is
        self.dropins = self.root / DROPIN_DIR
        self.dropins.mkdir(parents=True)
        self.calib = self.root / "local/calib"
        self.run_dir = self.make_run(STAMP, LINUX)
        self.logs = self.root / "local/logs"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.systemctl = self.bin / "systemctl"
        self.systemctl.write_text(STUB_SYSTEMCTL)
        self.systemctl.chmod(0o755)
        self.calls_log = tmp / "calls.log"
        self.template = self.root / "cmd02_template.bin"
        self.template.write_bytes(SYNTH_TEMPLATE)
        self.outputs: list[str] = []

    def make_run(self, stamp: str, data: bytes) -> Path:
        d = self.calib / stamp
        d.mkdir(parents=True, mode=0o700)
        (d / "program-linux.bin").write_bytes(data)
        (d / "program-linux.bin").chmod(0o600)
        return d

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("B2_", "STUB_", "SUDO_"))}
        env.update(
            B2_TEST_ROOT=str(self.root),
            B2_SYSTEMCTL=str(self.systemctl),
            B2_TEST_TEMPLATE=str(self.template),
            STUB_LOG=str(self.calls_log),
        )
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    def run(self, *args, env=None, cwd=PROJECT, script=SCRIPT_REL, **extra):
        self.calls_log.unlink(missing_ok=True)
        r = subprocess.run(
            ["bash", script, *map(str, args)],
            capture_output=True,
            text=True,
            env=self.env(**extra) if env is None else env,
            cwd=cwd,
            check=False,
            timeout=60,
        )
        out = r.stdout + r.stderr
        self.outputs.append(out)
        assert SERIAL not in out and SERIAL_2 not in out, out
        assert "Traceback" not in out, out
        return r

    def calls(self) -> list[str]:
        return self.calls_log.read_text().splitlines() if self.calls_log.exists() else []

    def files(self) -> set[str]:
        return set(os.listdir(self.data))

    def snapshot(self):
        return {n: ((self.data / n).read_bytes(), os.lstat(self.data / n)) for n in self.files()}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    for p in (e.state, e.data):  # a test may have locked them
        if p.exists():
            p.chmod(0o700)


def mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def assert_unchanged(env: Env, before) -> None:
    after = env.snapshot()
    assert set(after) == set(before)
    for n, (data, st) in before.items():
        assert after[n][0] == data
        assert after[n][1].st_ino == st.st_ino and after[n][1].st_mtime_ns == st.st_mtime_ns


# ------------------------------------------------------------------- the swap


def test_use_linux_backs_up_once_and_installs(env):
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.program.read_bytes() == LINUX
    assert env.backup.read_bytes() == WINDOWS
    assert mode(env.program) == 0o640 and mode(env.backup) == 0o640
    st, me = os.lstat(env.program), os.getuid()
    assert st.st_uid == me and os.lstat(env.backup).st_uid == me
    assert env.files() == {PROG, BACKUP}  # no temp file left
    assert "backed up the Windows program: program-<serial>.bin -> program-<serial>.bin.b2-windows-backup" in r.stdout
    assert f"installed the Linux program from local/calib/{STAMP} as program-<serial>.bin" in r.stdout
    assert "B2b step 3" in r.stdout and VERIFY_RIGHT in r.stdout and VERIFY_LEFT in r.stdout
    assert "local/logs/a5-verify-<stamp>.log" in r.stdout
    assert "sudo tools/dev/b2-program.sh use-windows" in r.stdout

    # Again: nothing changes, the backup above all.
    backup_st = os.lstat(env.backup)
    before = env.snapshot()
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already the Linux program" in r.stdout and "nothing was changed" in r.stdout
    assert_unchanged(env, before)
    assert os.lstat(env.backup).st_ino == backup_st.st_ino
    assert "B2b step 3" in r.stdout


@pytest.mark.skipif(
    not calibration.TEMPLATE_PATH.exists(),
    reason="proto/data/cmd02_template.bin not present (the real template, gitignored, local-only)",
)
def test_use_linux_against_the_pinned_real_template(env):
    """B2_TEST_TEMPLATE unset (B2_TEST_ROOT still set): the programs are checked against the pinned template."""
    real = calibration.load_template()  # read at run time only; nothing of it is kept in the repo
    windows, linux = program(0, real), program(1, real)
    env.program.write_bytes(windows)
    (env.run_dir / "program-linux.bin").write_bytes(linux)
    r = env.run("use-linux", env.run_dir, B2_TEST_TEMPLATE=None)
    assert r.returncode == 0, r.stdout + r.stderr
    # Equality only: a byte diff would show the real template's table bytes (pytest -l, assertion rewrite).
    same = env.program.read_bytes() == linux and env.backup.read_bytes() == windows
    assert same, "installed program or backup differs from the expected one"
    # The synthetic-template programs are not usable programs under the pinned template.
    env.program.write_bytes(WINDOWS)
    before = env.snapshot()
    r = env.run("use-linux", env.run_dir, B2_TEST_TEMPLATE=None)
    assert r.returncode == 1 and "differs from the template" in r.stderr, r.stdout + r.stderr
    assert_unchanged(env, before)


def test_use_linux_after_use_windows_keeps_the_first_backup(env):
    assert env.run("use-linux", env.run_dir).returncode == 0
    backup_st = os.lstat(env.backup)
    assert env.run("use-windows").returncode == 0
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 0, r.stderr
    assert "backed up" not in r.stdout
    assert os.lstat(env.backup).st_ino == backup_st.st_ino and env.backup.read_bytes() == WINDOWS
    assert env.program.read_bytes() == LINUX


def test_use_windows_restores_byte_exact(env):
    assert env.run("use-linux", env.run_dir).returncode == 0
    r = env.run("use-windows")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.program.read_bytes() == WINDOWS
    assert env.backup.read_bytes() == WINDOWS  # kept
    assert mode(env.program) == 0o640
    assert env.files() == {PROG, BACKUP}
    assert "restored the Windows program" in r.stdout and "the backup is kept" in r.stdout
    assert f"use-linux {env.run_dir}" in r.stdout  # try again: the newest valid run

    before = env.snapshot()
    r = env.run("use-windows")
    assert r.returncode == 0 and "already the Windows program" in r.stdout
    assert_unchanged(env, before)


def test_use_windows_without_backup_refuses(env):
    before = env.snapshot()
    r = env.run("use-windows")
    assert r.returncode == 1
    assert "no program-<serial>.bin.b2-windows-backup" in r.stderr
    assert_unchanged(env, before)
    assert env.calls() == []


# -------------------------------------------------------------------- status


def test_status_through_the_procedure(env):
    r = env.run("status")
    assert r.returncode == 0, r.stderr
    assert "installed program-<serial>.bin: Windows (original, never swapped)" in r.stdout
    assert "backup program-<serial>.bin.b2-windows-backup: none" in r.stdout
    assert "B2b step 1" in r.stdout and VERIFY_RIGHT in r.stdout and VERIFY_LEFT in r.stdout
    assert f"then step 2: sudo tools/dev/b2-program.sh use-linux {env.run_dir}" in r.stdout

    env.run("use-linux", env.run_dir)
    r = env.run("status")
    assert r.returncode == 0
    assert f"installed program-<serial>.bin: Linux (from local/calib/{STAMP})" in r.stdout
    assert "B2b step 3" in r.stdout and "sudo tools/dev/b2-program.sh use-windows" in r.stdout
    r = env.run("status", env.run_dir)
    assert f"Linux (from local/calib/{STAMP})" in r.stdout

    env.run("use-windows")
    r = env.run("status")
    assert "installed program-<serial>.bin: Windows\n" in r.stdout
    assert "backup program-<serial>.bin.b2-windows-backup: present" in r.stdout
    assert "the Windows program is in place" in r.stdout and "use-linux" in r.stdout
    assert env.calls() == []  # status runs no systemctl


def test_status_unknown(env):
    env.backup.write_bytes(WINDOWS)
    env.program.write_bytes(OTHER)
    r = env.run("status")
    assert r.returncode == 0
    assert "installed program-<serial>.bin: unknown" in r.stdout
    assert "sudo tools/dev/b2-program.sh use-windows" in r.stdout
    r = env.run("status", env.run_dir)
    assert f"unknown: neither the Windows backup nor local/calib/{STAMP}/program-linux.bin nor any " in r.stdout
    # Any local/calib run counts as Linux, with or without RUN_DIR: labelled with the run it is from.
    other_run = env.make_run(STAMP_2, OTHER)
    for args in ((), (env.run_dir,), (other_run,)):
        r = env.run("status", *args)
        assert f"installed program-<serial>.bin: Linux (from local/calib/{STAMP_2})" in r.stdout, r.stdout


def test_use_linux_of_another_run_after_a_first(env):
    # use-linux A, then use-linux B: A's program is still in local/calib/A, so nothing is lost.
    run_b = env.make_run(STAMP_2, OTHER)
    assert env.run("use-linux", env.run_dir).returncode == 0
    r = env.run("use-linux", run_b)
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.program.read_bytes() == OTHER and env.backup.read_bytes() == WINDOWS
    assert "backed up" not in r.stdout
    assert f"installed the Linux program from local/calib/{STAMP_2}" in r.stdout
    assert f"Linux (from local/calib/{STAMP_2})" in env.run("status", env.run_dir).stdout
    # Refused only when the installed program is nowhere else: A's, once its file is gone.
    assert env.run("use-linux", env.run_dir).returncode == 0
    (env.run_dir / "program-linux.bin").unlink()
    before = env.snapshot()
    r = env.run("use-linux", run_b)
    assert r.returncode == 1 and "would lose it" in r.stderr
    assert_unchanged(env, before)


def test_status_unreadable_dir_gives_the_sudo_line(env):
    env.state.chmod(0)
    r = env.run("status")
    assert r.returncode == 2
    assert "/var/lib/fprint is root-only" in r.stderr
    assert "sudo tools/dev/b2-program.sh status\n" in r.stderr
    r = env.run("status", env.run_dir)
    assert f"sudo tools/dev/b2-program.sh status {env.run_dir}\n" in r.stderr


# ---------------------------------------------------------------- refusals


def mutated_skeleton(p: bytes) -> bytes:
    spans = calibration.unit_region_spans(p).values()
    off = 500
    assert not any(a <= off < b for a, b in spans)
    return p[:off] + bytes([p[off] ^ 0x01]) + p[off + 1 :]


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda p: p[:-1], "18868 bytes"),
        (lambda p: b"\x03" + p[1:], "starts with 0x03"),
        (mutated_skeleton, "byte 500 differs from the template"),
    ],
    ids=["size", "first-byte", "skeleton"],
)
def test_invalid_linux_program_refused(env, make, reason):
    (env.run_dir / "program-linux.bin").write_bytes(make(LINUX))
    before = env.snapshot()
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"local/calib/{STAMP}/program-linux.bin is not a usable program" in r.stderr
    assert reason in r.stderr and "nothing was changed" in r.stderr
    assert_unchanged(env, before)
    assert env.calls() == []


def test_invalid_installed_program_refused(env):
    env.program.write_bytes(mutated_skeleton(WINDOWS))
    before = env.snapshot()
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 1
    assert "the installed program-<serial>.bin is not a usable program" in r.stderr
    assert_unchanged(env, before)


def test_unknown_active_program_refused(env):
    env.backup.write_bytes(WINDOWS)
    env.program.write_bytes(OTHER)
    before = env.snapshot()
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 1
    assert "neither the Windows backup nor" in r.stderr and "would lose it" in r.stderr
    assert "sudo tools/dev/b2-program.sh use-windows" in r.stderr
    assert_unchanged(env, before)
    assert env.calls() == []


def test_zero_or_two_programs_refused(env):
    env.program.unlink()
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 1 and "no program-<serial>.bin in /var/lib/fprint/synaptics0078" in r.stderr
    assert env.files() == set()
    env.program.write_bytes(WINDOWS)
    (env.data / f"program-{SERIAL_2}.bin").write_bytes(WINDOWS)
    before = env.snapshot()
    for args in (("use-linux", env.run_dir), ("use-windows",), ("status",)):
        r = env.run(*args)
        assert r.returncode == 1 and "2 program-<serial>.bin files" in r.stderr
    assert_unchanged(env, before)


def test_symlinked_program_refused(env):
    real = env.tmp / "elsewhere.bin"
    real.write_bytes(WINDOWS)
    env.program.unlink()
    env.program.symlink_to(real)
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 1 and "program-<serial>.bin is a symlink" in r.stderr
    assert real.read_bytes() == WINDOWS and not env.backup.exists()


def test_missing_run_dir_program(env):
    (env.run_dir / "program-linux.bin").unlink()
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 1 and "no program-linux.bin in" in r.stderr
    assert f"build it (as yourself): proto/.venv/bin/python proto/calib_program.py {env.run_dir}" in r.stderr
    # From elsewhere: runnable from there (absolute outside the cwd), every path quoted.
    spaced = env.calib / "run with space"
    spaced.mkdir()
    r = env.run("use-linux", spaced, cwd=PROJECT / "tools", script="dev/b2-program.sh")
    assert r.returncode == 1
    py, script = PROJECT / "proto/.venv/bin/python", PROJECT / "proto/calib_program.py"
    want = f"{shlex.quote(str(py))} {shlex.quote(str(script))} {shlex.quote(str(spaced))}"
    assert want in r.stderr and "'" in want, r.stderr


def test_symlinked_backup_refused(env):
    real = env.tmp / "elsewhere.bin"
    real.write_bytes(WINDOWS)
    env.backup.symlink_to(real)
    env.program.write_bytes(LINUX)
    before = env.snapshot()
    for args in (("use-linux", env.run_dir), ("use-windows",), ("status",)):
        r = env.run(*args)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "program-<serial>.bin.b2-windows-backup is a symlink" in r.stderr
    assert_unchanged(env, before)
    assert os.path.islink(env.backup) and real.read_bytes() == WINDOWS
    assert env.calls() == []


def test_symlinked_data_dir_refused(env):
    real = env.state / "synaptics0078-real"
    env.data.rename(real)
    env.data.symlink_to(real)
    before = {n: (real / n).read_bytes() for n in os.listdir(real)}
    for args in (("use-linux", env.run_dir), ("use-windows",), ("status",)):
        r = env.run(*args)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "/var/lib/fprint/synaptics0078 is not a directory (or is a symlink)" in r.stderr
    assert {n: (real / n).read_bytes() for n in os.listdir(real)} == before == {PROG: WINDOWS}
    assert env.calls() == []


def test_symlinked_linux_program_refused(env):
    real = env.tmp / "linux-elsewhere.bin"
    real.write_bytes(LINUX)
    (env.run_dir / "program-linux.bin").unlink()
    (env.run_dir / "program-linux.bin").symlink_to(real)
    before = env.snapshot()
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 1
    assert f"local/calib/{STAMP}/program-linux.bin is a symlink; refusing to follow it" in r.stderr
    assert_unchanged(env, before)
    assert env.calls() == []


def test_write_failure_changes_nothing(env):
    before = env.snapshot()
    env.data.chmod(0o500)  # readable, not writable: no temp file can be made
    r = env.run("use-linux", env.run_dir)
    env.data.chmod(0o700)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "cannot create a temp file in /var/lib/fprint/synaptics0078" in r.stderr
    assert "nothing was changed" in r.stderr
    assert env.files() == {PROG}  # no temp file, no backup
    assert_unchanged(env, before)
    assert env.calls() == []


def test_interrupt_inside_the_write_leaves_no_temp_file(env, monkeypatch):
    before = env.snapshot()
    like = os.stat(env.program)

    def interrupt(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)
    dir_fd = os.open(env.data, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for replace in (True, False):
            name = PROG if replace else BACKUP
            with pytest.raises(KeyboardInterrupt):
                b2.atomic_write(dir_fd, name, OTHER, like, b2.SHOWN, replace=replace)
    finally:
        os.close(dir_fd)
    assert env.files() == {PROG}
    assert_unchanged(env, before)


@pytest.mark.parametrize("how", ["ctrl-c", "sigterm"])
@pytest.mark.parametrize("command", ["use-windows", "status"])
def test_interrupt_exits_130_with_one_line(env, monkeypatch, capsys, how, command):
    def interrupted(*args):
        if how == "sigterm":
            assert signal.getsignal(signal.SIGTERM) is b2._on_sigterm  # else the kill ends pytest
            os.kill(os.getpid(), signal.SIGTERM)
        raise KeyboardInterrupt

    monkeypatch.setattr(b2, f"cmd_{command.replace('-', '_')}", interrupted)
    keep = signal.getsignal(signal.SIGTERM)
    rc = b2.main(["--self", "tools/dev/b2-program.sh", "--", command], env.env())
    assert signal.getsignal(signal.SIGTERM) is keep  # restored
    out, err = capsys.readouterr()
    assert rc == 130
    assert "Traceback" not in out + err and SERIAL not in out + err
    lines = err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("interrupted; "), err
    if command == "status":
        assert lines[0] == "interrupted; nothing was changed"
    else:
        assert "no file is half-written" in lines[0] and "sudo tools/dev/b2-program.sh status" in lines[0]
    assert env.files() == {PROG}


def test_helpers_are_compiled_from_source_not_a_planted_pyc(tmp_path):
    # A .pyc whose header matches the source (its mtime and size) runs instead of the source under
    # an import loader; load_module must ignore it.
    src = tmp_path / "helper.py"
    src.write_text("WHO = 'pyc'\n")
    cache = importlib.util.cache_from_source(str(src))
    py_compile.compile(str(src), cfile=cache, doraise=True)
    st = os.stat(src)
    src.write_text("WHO = 'src'\n")  # the same size
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
    # The plant works: an import loader runs it.
    loader = importlib.machinery.SourceFileLoader("b2_test_control", str(src))
    control = importlib.util.module_from_spec(importlib.util.spec_from_loader("b2_test_control", loader))
    loader.exec_module(control)
    assert control.WHO == "pyc"
    try:
        module = b2.load_module("b2_test_planted", str(src))
        assert module.WHO == "src"
        assert module.__file__ == str(src) and sys.modules["b2_test_planted"] is module
    finally:
        sys.modules.pop("b2_test_planted", None)
    assert os.listdir(os.path.dirname(cache)) == [os.path.basename(cache)]  # nothing written


def test_hint_commands_quote_their_paths(env, monkeypatch):
    monkeypatch.setattr(b2, "HERE", "/x y/tools/dev")
    cfg = b2.load_config(env.env(), os.geteuid(), "tools/dev/b2-program.sh")
    lines = b2.log_on_lines(cfg) + b2.verify_lines(cfg)
    assert "sudo '/x y/tools/dev/a5-fprintd-dev.sh' log on" in lines[0]
    assert "sudo '/x y/tools/dev/a5-fprintd-dev.sh' log off" in lines[1]
    assert lines[2].startswith("  '/x y/tools/dev/a5-verify.sh' --finger-used right-index")


def test_systemctl_by_absolute_path(env):
    assert b2.load_config({}, os.geteuid(), "x").systemctl == ("/usr/bin/systemctl",)
    assert b2.load_config(env.env(), os.geteuid(), "x").systemctl == (str(env.systemctl),)


# ------------------------------------------------------------- the template hook


@pytest.fixture
def checker_modules():
    """Checker() puts its helpers in sys.modules (load_module); put back what was there."""
    names = ("calibration", "synaptics0078_setup")
    keep = {n: sys.modules[n] for n in names if n in sys.modules}
    yield
    for n in names:
        if n in keep:
            sys.modules[n] = keep[n]
        else:
            sys.modules.pop(n, None)


def test_test_template_only_with_test_root(env, checker_modules):
    assert b2.load_config(env.env(), os.geteuid(), "x").test_template == str(env.template)
    cfg = b2.load_config({"B2_TEST_TEMPLATE": str(env.template)}, os.geteuid(), "x")
    assert cfg.test_template == ""
    # Unset: the check is program_problem(data) as before, which reads the pinned template itself.
    checker = b2.Checker(cfg)
    assert checker.template is None
    seen = []
    checker.calibration.program_problem = lambda *args: seen.append(args)
    checker.problem(SERIAL, WINDOWS)
    assert seen == [(WINDOWS,)]
    checker = b2.Checker(b2.load_config(env.env(), os.geteuid(), "x"))
    assert checker.template == SYNTH_TEMPLATE and checker.problem(SERIAL, WINDOWS) is None


def test_test_template_is_the_one_checked(env):
    env.template.write_bytes(mutated_skeleton(SYNTH_TEMPLATE))
    r = env.run("status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "(not a usable program: byte 500 differs from the template" in r.stdout
    before = env.snapshot()
    r = env.run("use-linux", env.run_dir)
    assert r.returncode == 1 and "byte 500 differs from the template" in r.stderr
    assert_unchanged(env, before)


def bad_template(env: Env, how: str) -> str:
    """The B2_TEST_TEMPLATE value for one kind of bad template file."""
    t = env.template
    if how == "missing":
        return str(env.tmp / "missing.bin")
    if how == "no-dir":
        return str(env.tmp / "no-dir" / "t.bin")
    if how == "relative":
        return "cmd02_template.bin"
    if how == "size":
        t.write_bytes(SYNTH_TEMPLATE[:-1])
    elif how == "huge":
        t.write_bytes(bytes(b2.READ_CAP + 1))
    elif how == "symlink":
        (env.tmp / "t").write_bytes(SYNTH_TEMPLATE)  # a valid target: still refused
        t.unlink()
        t.symlink_to(env.tmp / "t")
    elif how == "directory":
        t.unlink()
        t.mkdir()
    return str(t)


@pytest.mark.parametrize(
    ("how", "reason"),
    [
        ("missing", "does not exist"),
        ("no-dir", "cannot be opened"),
        ("size", "is 18868 bytes, need 18869: not a template"),
        ("huge", f"is {b2.READ_CAP + 1} bytes"),
        ("symlink", "is a symlink"),
        ("directory", "is not a regular file"),
        ("relative", "must be an absolute path"),
    ],
)
def test_bad_test_template_fails_cleanly(env, how, reason):
    path = bad_template(env, how)
    before = env.snapshot()
    for args in (("use-linux", env.run_dir), ("use-windows",), ("status",)):
        r = env.run(*args, B2_TEST_TEMPLATE=path)
        assert r.returncode == 2, r.stdout + r.stderr
        assert r.stderr.startswith("FAILED: B2_TEST_TEMPLATE") and reason in r.stderr, r.stderr
    assert_unchanged(env, before)
    assert env.calls() == []


# --------------------------------------------------------------- root checks


def test_needs_root_without_test_root(env):
    for args in (("use-linux", "local/calib/x"), ("use-windows",)):
        r = env.run(*args, B2_TEST_ROOT=None, B2_SYSTEMCTL=None)
        assert r.returncode == 2
        assert f"{args[0]} needs root; nothing was changed. Run:\n  sudo tools/dev/b2-program.sh " in r.stderr
        assert r.stderr.rstrip().endswith(" ".join(args))
        assert r.stdout == ""
    r = env.run("use-windows", B2_TEST_ROOT=None, B2_SYSTEMCTL=None, B2_FAKE_EUID="1000")
    assert r.returncode == 2 and "needs root" in r.stderr
    assert env.calls() == [] and env.files() == {PROG}


def wrapper_test_vars() -> list[str]:
    """The TEST_VARS=(...) array of b2-program.sh."""
    text = (PROJECT / SCRIPT_REL).read_text()
    (line,) = [ln for ln in text.splitlines() if ln.startswith("TEST_VARS=(")]
    return line[len("TEST_VARS=(") : line.rindex(")")].split()


def test_test_vars_lists_agree():
    assert len(TEST_VARS) == len(set(TEST_VARS))
    assert set(wrapper_test_vars()) == set(b2.TEST_VARS) == set(TEST_VARS)
    assert len(wrapper_test_vars()) == len(b2.TEST_VARS) == len(TEST_VARS)


@pytest.mark.parametrize("var", TEST_VARS)
def test_test_vars_refused_as_root(env, var):
    # Both refusal loops go through the list in order and name the first variable set, and B2_FAKE_EUID
    # (which the wrapper needs to see root as a user) is last: so the message names `var` itself, and a
    # variable missing from either list would show up as B2_FAKE_EUID being named instead.
    said = f"FAILED: {var} is set; the test-only variables are refused as root"
    run_env = {k: v for k, v in env.env().items() if not k.startswith("B2_")}
    run_env[var] = str(env.root) if var == "B2_TEST_ROOT" else "/tmp/x"
    run_env["B2_FAKE_EUID"] = "0"
    for args in (("use-linux", env.run_dir), ("use-windows",), ("status",)):
        r = env.run(*args, env=run_env)
        assert r.returncode == 2, r.stdout + r.stderr
        assert said in r.stderr and r.stdout == ""
    # The Python half, run as root (no fake euid, so no other variable can stand in for `var`).
    with pytest.raises(b2.Fail) as exc:
        b2.load_config({var: "/tmp/x"}, 0, "x")
    assert exc.value.code == 2 and f"{var} is set; the test-only variables are refused as root" in str(exc.value)
    # ... and the script's own refusal, with the fake euid.
    r = subprocess.run(
        [sys.executable, "-I", "-B", str(PY_SCRIPT), "--", "status"],
        capture_output=True,
        text=True,
        env=run_env,
        check=False,
    )
    assert r.returncode == 2 and said in r.stderr
    assert env.files() == {PROG}


def test_help_prints_the_header(env):
    r = env.run("--help")
    assert r.returncode == 0
    assert r.stdout.startswith("Step B2b: swap the reader's capture program")
    assert "B2_TEST_ROOT" in r.stdout and "B2_FAKE_EUID" in r.stdout and "b2-windows-backup" in r.stdout
    assert "set -euo" not in r.stdout and "#!" not in r.stdout


def test_misuse(env):
    for args in ((), ("use-linux",), ("use-windows", "x"), ("status", "a", "b"), ("frob",)):
        r = env.run(*args)
        assert r.returncode == 2 and r.stderr.startswith("FAILED: ")
    assert env.files() == {PROG}


# ------------------------------------------------------------------- fprintd


def test_fprintd_stopped_only_when_active(env):
    r = env.run("use-linux", env.run_dir)
    assert env.calls() == ["systemctl is-active fprintd"]
    assert "fprintd is not running" in r.stdout
    r = env.run("use-windows", STUB_STATE="active")
    assert r.returncode == 0
    assert env.calls() == ["systemctl is-active fprintd", "systemctl stop fprintd"]
    assert "stopped fprintd" in r.stdout
    # Nothing to do: no stop, but the command to run if a client held the reader.
    r = env.run("use-windows", STUB_STATE="active")
    assert env.calls() == ["systemctl is-active fprintd"]
    assert "sudo systemctl stop fprintd" in r.stdout


@pytest.mark.parametrize(
    ("state", "stops"),
    [("active", True), ("activating", True), ("reloading", True), ("deactivating", True), ("failed", False)],
)
def test_fprintd_stopped_in_every_running_state(env, state, stops):
    r = env.run("use-linux", env.run_dir, STUB_STATE=state)
    assert r.returncode == 0, r.stdout + r.stderr
    stop = ["systemctl stop fprintd"] if stops else []
    assert env.calls() == ["systemctl is-active fprintd", *stop]
    assert ("stopped fprintd" in r.stdout) == stops and ("fprintd is not running" in r.stdout) != stops


def test_systemctl_output_decoded_leniently(env):
    env.systemctl.write_text("#!/usr/bin/env bash\nprintf 'act\\xffive\\n'\nprintf '\\xfe' >&2\n")
    cfg = b2.load_config(env.env(), os.geteuid(), "x")
    assert b2.run_systemctl(cfg, b2.NoLog(), "is-active", "fprintd") == (0, "act\ufffdive")


def test_symlinked_log_dirs_not_followed(env):
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    env.logs.symlink_to(elsewhere)
    r = env.run("status")
    assert r.returncode == 1 and "local/logs is a symlink or not a directory" in r.stderr, r.stderr
    env.logs.unlink()
    local_real = env.tmp / "local-real"
    (env.root / "local").rename(local_real)
    (env.root / "local").symlink_to(local_real)
    r = env.run("use-linux", local_real / "calib" / STAMP)
    assert r.returncode == 1 and "local is a symlink or not a directory" in r.stderr, r.stderr
    assert os.listdir(elsewhere) == [] and not (local_real / "logs").exists()
    assert env.files() == {PROG} and env.calls() == []


def test_fprintd_stop_failure_reported(env):
    r = env.run("use-linux", env.run_dir, STUB_STATE="active", STUB_STOP_RC="1")
    assert r.returncode == 1
    assert "the program is in place, but stopping fprintd failed" in r.stderr
    assert "sudo systemctl stop fprintd" in r.stderr
    assert env.program.read_bytes() == LINUX


# ----------------------------------------------------------- hints and the log


def test_log_on_hint_without_dropin(env):
    r = env.run("status")
    assert "sudo tools/dev/a5-fprintd-dev.sh log on" in r.stdout
    assert "sudo tools/dev/a5-fprintd-dev.sh log off" in r.stdout
    assert r.stdout.index("log on") < r.stdout.index("B2b step 1")
    (env.dropins / "synaptics0078-debug.conf").write_text("[Service]\n")
    r = env.run("use-linux", env.run_dir)
    assert "log on" not in r.stdout
    assert "when testing is done: sudo tools/dev/a5-fprintd-dev.sh log off" in r.stdout
    (env.dropins / "synaptics0078-debug.conf").unlink()
    (env.dropins / "libfprint-0078-dev.conf").write_text("[Service]\n")
    r = env.run("status")
    assert "log on" not in r.stdout and "log off" not in r.stdout


def test_log_is_private_and_has_the_details(env):
    r = env.run("use-linux", env.run_dir)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("log: ")]
    assert len(lines) == 1
    log = Path(lines[0].removeprefix("log: "))
    assert log.parent == env.logs and log.name.startswith("b2-program-")
    assert mode(log) == 0o600
    text = log.read_text()
    assert SERIAL in text and "sha256" in text and "systemctl is-active fprintd" in text


def test_serial_never_printed(env):
    # env.run checks every run; this walks every command and refusal once more in one place.
    env.run("--help")
    env.run("status")
    env.run("use-linux", env.run_dir, STUB_STATE="active")
    env.run("status", env.run_dir)
    env.run("use-linux", env.run_dir)
    env.run("use-windows")
    env.run("use-windows")
    env.program.write_bytes(OTHER)
    env.run("use-linux", env.run_dir)
    env.run("status")
    env.state.chmod(0)
    env.run("status")
    env.state.chmod(0o700)
    assert len(env.outputs) == 10
    assert not any(SERIAL in out for out in env.outputs)
