# SPDX-License-Identifier: MIT
"""Tests of packaging/arch/libfprint-sigfm-0078/synaptics0078-setup (the root-side setup tool).

No root, no USB, no network, no real curl and no real data: every system path lives
under a test root (SYNAPTICS0078_SETUP_TEST_ROOT). Phase A (install --from DIR): the
module is synthetic bytes whose hash is passed in SYNAPTICS0078_SETUP_TEST_MODULE_SHA256,
and the programs are synthetic too. Phase B (install, recalibrate, fetch, status): the
synthetic DLL, module, template and pins of synaptics0078/tests/fixtures.py, a stored cab
built from them, fake curl / systemctl / setpriv scripts and a fake USB transport, all
written by the tests (no vendor bytes). Most tests call main() in-process with an
explicit environment; a few run the script itself, so its shebang and the test-hook
guard are exercised as a user would meet them.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import synaptics0078_data as sd

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPT = REPO / "packaging" / "arch" / "libfprint-sigfm-0078" / "synaptics0078-setup"
if str(REPO) not in sys.path:  # the synaptics0078 package and its synthetic fixtures
    sys.path.insert(0, str(REPO))

from synaptics0078 import calibration
from synaptics0078 import pins as pins_mod
from synaptics0078.tests import fixtures as fx


def load_script():
    loader = importlib.machinery.SourceFileLoader("synaptics0078_setup", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module  # dataclasses look their module up here
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ in packaging/
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = keep
    return module


setup = load_script()

SERIAL = "0123456789ab"  # synthetic, not a real unit's
SERIAL_2 = "fedcba987654"
MODULE = bytes((i * 7 + 3) % 256 for i in range(10500))
PROGRAM = b"\x02" + bytes((i * 13 + 1) % 256 for i in range(18868))
PROGRAM_2 = b"\x02" + b"\x01" * 18868
DONE = setup.DONE_LINE
TEST_VARS = ["SYNAPTICS0078_SETUP_TEST_ROOT", "SYNAPTICS0078_SETUP_TEST_MODULE_SHA256", "SYNAPTICS0078_SETUP_TEST_EUID"]

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="the test hooks are refused as root")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def tree(path: Path) -> list[str]:
    """Every path under `path`, relative, sorted: what a refused run must leave unchanged."""
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


class Env:
    """A test root (only var/lib exists) and a source dir holding a valid synthetic set."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = tmp / "root"
        (self.root / "var" / "lib").mkdir(parents=True)
        self.state = self.root / "var" / "lib" / "fprint"
        self.data = self.state / "synaptics0078"
        self.src = tmp / "src"
        self.src.mkdir()
        (self.src / "module.bin").write_bytes(MODULE)
        (self.src / f"program-{SERIAL}.bin").write_bytes(PROGRAM)

    def environ(self, **extra) -> dict[str, str]:
        env = {
            "SYNAPTICS0078_SETUP_TEST_ROOT": str(self.root),
            "SYNAPTICS0078_SETUP_TEST_MODULE_SHA256": sha(MODULE),
        }
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, *argv: str, **extra) -> tuple[int, str, str]:
        """main() in-process: (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = setup.main(list(argv), self.environ(**extra))
        return rc, out.getvalue(), err.getvalue()

    def install(self, *extra_args: str, src: Path | None = None, **extra) -> tuple[int, str, str]:
        return self.run("install", "--from", str(src or self.src), *extra_args, **extra)

    def cli(self, *argv: str, env: dict[str, str] | None = None, cwd=None) -> subprocess.CompletedProcess:
        """The script itself, through its shebang."""
        base = {k: v for k, v in os.environ.items() if not k.startswith("SYNAPTICS0078_SETUP_") and k != "SUDO_UID"}
        base.update(self.environ() if env is None else env)
        return subprocess.run([str(SCRIPT), *argv], capture_output=True, text=True, env=base, cwd=cwd, check=False)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ------------------------------------------------------------- the script file


def test_shebang_is_isolated_python():
    # -I: no PYTHONPATH, user site, script or current directory; -B: no bytecode written (B5).
    assert SCRIPT.read_text().splitlines()[0] == "#!/usr/bin/python3 -IB"
    assert SCRIPT.read_text().splitlines()[1] == "# SPDX-License-Identifier: MIT"


def test_script_is_executable_0755():
    assert mode(SCRIPT) == 0o755


def test_script_imports_only_the_stdlib():
    tree_ = ast.parse(SCRIPT.read_text())
    names = set()
    for node in ast.walk(tree_):
        if isinstance(node, ast.Import):
            names.update(a.name.partition(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            names.add(node.module.partition(".")[0])
    assert names, "no imports found: the parse is wrong"
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)
    assert not any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "__import__" for n in ast.walk(tree_))


def test_constants_match_the_generator_and_the_driver():
    assert setup.MODULE_SIZE == sd.capture.MODULE_LEN == 10500
    assert setup.MODULE_SHA256 == sd.capture.MODULE_SHA256
    assert setup.PROGRAM_SIZE == sd.PROGRAM_LEN == 18869
    assert setup.PROGRAM_OPCODE == sd.PROGRAM_OPCODE == 0x02
    assert setup.DATA_DIR == "/var/lib/fprint/synaptics0078"
    assert setup.program_name(SERIAL) == sd.program_name(SERIAL)


def test_help_says_where_the_files_come_from(env):
    r = env.cli("--help")
    assert r.returncode == 0, r.stderr
    assert "tools/synaptics0078_data.py" in r.stdout and "Windows driver files" in r.stdout
    assert "install" in r.stdout and "status" in r.stdout


# ------------------------------------------------------------------ install


def test_first_install(env):
    rc, out, err = env.install()
    assert rc == 0, out + err
    assert mode(env.state) == 0o700 and mode(env.data) == 0o700
    assert (env.data / "module.bin").read_bytes() == MODULE
    assert (env.data / f"program-{SERIAL}.bin").read_bytes() == PROGRAM
    assert mode(env.data / "module.bin") == 0o600 and mode(env.data / f"program-{SERIAL}.bin") == 0o600
    assert sorted(p.name for p in env.data.iterdir()) == ["module.bin", f"program-{SERIAL}.bin"]  # no temp files
    assert "  ok    installed module.bin" in out and f"  ok    installed program-{SERIAL}.bin" in out
    assert out.splitlines()[-1] == DONE
    assert "no restart needed" in DONE and "synaptics0078-setup status" in DONE
    assert err == ""


def test_install_through_the_script(env):
    r = env.cli("install", "--from", str(env.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (env.data / "module.bin").read_bytes() == MODULE
    assert r.stdout.splitlines()[-1] == DONE


def test_rerun_says_already_installed(env):
    assert env.install()[0] == 0
    rc, out, _ = env.install()
    assert rc == 0
    assert "  already installed: module.bin (identical)" in out
    assert f"  already installed: program-{SERIAL}.bin (identical)" in out
    assert "installed module.bin" not in out.replace("already installed", "")
    assert out.splitlines()[-1] == DONE


def test_relative_from_is_made_absolute(env):
    r = env.cli("install", "--from", "src", cwd=env.tmp)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"data from {env.src}" in r.stdout


def test_a_differing_program_is_refused_without_force(env):
    assert env.install()[0] == 0
    (env.src / f"program-{SERIAL}.bin").write_bytes(PROGRAM_2)
    rc, out, err = env.install()
    assert rc == 1
    assert f"FAILED program-{SERIAL}.bin is already installed with different contents" in out
    assert f"sudo synaptics0078-setup install --from {env.src} --force" in err
    assert (env.data / f"program-{SERIAL}.bin").read_bytes() == PROGRAM


def test_a_differing_program_is_replaced_with_force(env):
    assert env.install()[0] == 0
    (env.src / f"program-{SERIAL}.bin").write_bytes(PROGRAM_2)
    rc, out, err = env.install("--force")
    assert rc == 0, out + err
    assert f"  ok    replaced program-{SERIAL}.bin (--force)" in out
    assert "  already installed: module.bin (identical)" in out
    assert (env.data / f"program-{SERIAL}.bin").read_bytes() == PROGRAM_2
    assert mode(env.data / f"program-{SERIAL}.bin") == 0o600
    assert sorted(p.name for p in env.data.iterdir()) == ["module.bin", f"program-{SERIAL}.bin"]


def test_an_invalid_installed_module_is_replaced_without_force(env):
    assert env.install()[0] == 0
    (env.data / "module.bin").write_bytes(b"old junk")
    rc, out, err = env.install()
    assert rc == 0, out + err
    assert "replaced module.bin (the installed one was not valid: it is 8 bytes)" in out
    assert (env.data / "module.bin").read_bytes() == MODULE


def test_a_second_program_is_added_and_the_first_kept(env):
    assert env.install()[0] == 0
    (env.src / f"program-{SERIAL}.bin").unlink()
    (env.src / f"program-{SERIAL_2}.bin").write_bytes(PROGRAM_2)
    rc, out, err = env.install()
    assert rc == 0, out + err
    assert sorted(p.name for p in env.data.iterdir()) == [
        "module.bin",
        f"program-{SERIAL}.bin",
        f"program-{SERIAL_2}.bin",
    ]


def test_other_source_files_are_ignored_and_listed(env):
    (env.src / "README.txt").write_text("hi")
    (env.src / "sub").mkdir()
    rc, out, _ = env.install()
    assert rc == 0
    assert "  note  ignored: README.txt (not a data file)" in out
    assert "  note  ignored: sub (not a data file)" in out
    assert sorted(p.name for p in env.data.iterdir()) == ["module.bin", f"program-{SERIAL}.bin"]


INVALID_SOURCES = {
    "module size": ("module.bin", MODULE[:-1], "module.bin is 10499 bytes, expected 10500"),
    "module hash": ("module.bin", bytes(10500), "module.bin has sha256"),
    "program size": (
        f"program-{SERIAL}.bin",
        PROGRAM + b"\x00",
        f"program-{SERIAL}.bin is 18870 bytes, expected 18869",
    ),
    "program opcode": (f"program-{SERIAL}.bin", b"\x06" + PROGRAM[1:], "starts with 0x06, expected 0x02"),
    "serial upper case": ("program-0123456789AB.bin", PROGRAM, "program-0123456789AB.bin is not named"),
    "serial too short": ("program-0123456789a.bin", PROGRAM, "program-0123456789a.bin is not named"),
    "serial empty": ("program-.bin", PROGRAM, "program-.bin is not named"),
    "serial newline": ("program-0123456789ab\n.bin", PROGRAM, "is not named program-<12 lowercase hex"),
}


@pytest.mark.parametrize("case", INVALID_SOURCES, ids=list(INVALID_SOURCES))
def test_an_invalid_source_file_writes_nothing(env, case):
    name, data, message = INVALID_SOURCES[case]
    (env.src / name).write_bytes(data)  # replaces the valid file, or adds a bad program next to it
    rc, out, err = env.install()
    assert rc == 1, out + err
    assert f"  FAILED {message}" in out or message in out
    assert "nothing was installed" in err
    assert not env.state.exists()


def test_a_huge_source_file_is_refused_by_size_before_reading(env, monkeypatch):
    with open(env.src / "module.bin", "r+b") as fh:
        fh.truncate(1 << 40)  # sparse, 1 TiB
    reads = []
    real_read = os.read
    monkeypatch.setattr(setup.os, "read", lambda fd, n: reads.append(n) or real_read(fd, n))
    rc, out, _ = env.install()
    assert rc == 1
    assert f"module.bin is {1 << 40} bytes, expected 10500" in out
    assert all(n <= 18870 for n in reads)  # only the program was read
    assert not env.state.exists()


def test_a_source_file_that_changes_while_read_is_refused(env, monkeypatch):
    (env.src / "module.bin").write_bytes(MODULE + b"grown")
    real_fstat = os.fstat

    def fstat(fd):  # the size seen before the read is the expected one; the file then holds more
        st = real_fstat(fd)
        if st.st_size == len(MODULE) + 5:
            fields = list(st)
            fields[stat.ST_SIZE] = len(MODULE)
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(setup.os, "fstat", fstat)
    rc, out, _ = env.install()
    assert rc == 1
    assert "module.bin changed while it was read" in out
    assert not env.state.exists()


@pytest.mark.parametrize("missing", ["module.bin", f"program-{SERIAL}.bin"])
def test_a_missing_source_file_writes_nothing(env, missing):
    (env.src / missing).unlink()
    rc, out, err = env.install()
    assert rc == 1, out + err
    shown = "module.bin" if missing == "module.bin" else "program-<serial>.bin"
    assert f"  FAILED {shown} is missing from {env.src}" in out
    assert not env.state.exists()


def test_a_missing_source_dir_fails(env):
    rc, _, err = env.install(src=env.tmp / "nope")
    assert rc == 1
    assert f"no directory {env.tmp / 'nope'}" in err and "tools/synaptics0078_data.py" in err
    assert not env.state.exists()


@pytest.mark.parametrize("name", ["module.bin", f"program-{SERIAL}.bin"])
def test_a_symlink_as_a_source_file_is_refused(env, name):
    real = env.tmp / "elsewhere.bin"
    (env.src / name).rename(real)
    (env.src / name).symlink_to(real)
    rc, out, _ = env.install()
    assert rc == 1
    assert f"  FAILED {name} is a symlink" in out
    assert not env.state.exists()


def test_a_fifo_as_a_source_file_is_refused_without_blocking(env):
    (env.src / "module.bin").unlink()
    os.mkfifo(env.src / "module.bin")
    rc, out, _ = env.install()
    assert rc == 1
    assert "  FAILED module.bin is not a regular file" in out


def test_a_symlinked_source_dir_is_refused(env):
    (env.tmp / "link").symlink_to(env.src)
    rc, _, err = env.install(src=env.tmp / "link")
    assert rc == 1
    assert "is a symlink; give the directory itself" in err
    assert not env.state.exists()


@pytest.mark.parametrize("which", ["state", "data"])
def test_a_symlink_as_a_target_dir_is_refused(env, which):
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    if which == "state":
        env.state.symlink_to(elsewhere)
        link = env.state
    else:
        env.state.mkdir(mode=0o700)
        env.data.symlink_to(elsewhere)
        link = env.data
    rc, out, err = env.install()
    assert rc == 1, out + err
    assert f"  FAILED {link} is a symlink; refusing to write through it" in out
    assert list(elsewhere.iterdir()) == []


def test_a_file_as_the_data_dir_is_refused(env):
    env.state.mkdir(mode=0o700)
    env.data.write_bytes(b"")
    rc, out, _ = env.install()
    assert rc == 1
    assert f"{env.data} exists and is not a directory" in out


def test_a_symlink_as_a_target_file_is_refused(env):
    assert env.install()[0] == 0
    victim = env.tmp / "victim"
    victim.write_bytes(b"keep me")
    target = env.data / f"program-{SERIAL}.bin"
    target.unlink()
    target.symlink_to(victim)
    for args in ((), ("--force",)):
        rc, out, _ = env.install(*args)
        assert rc == 1
        assert "is a symlink; refusing to replace or follow it" in out
        assert victim.read_bytes() == b"keep me"
        assert target.is_symlink()


def test_a_target_dir_of_another_owner_is_refused(env, monkeypatch):
    env.state.mkdir(mode=0o700)
    monkeypatch.setattr(setup.os, "getuid", lambda: os.geteuid() + 4242)  # the owner the tool expects
    rc, out, _ = env.install()
    assert rc == 1
    assert f"{env.state} is owned by" in out and "refusing to use it" in out
    assert not env.data.exists()


def test_a_loose_data_dir_mode_is_fixed(env):
    env.state.mkdir(mode=0o700)
    env.data.mkdir()
    env.data.chmod(0o755)
    rc, out, _ = env.install()
    assert rc == 0
    assert f"  ok    set {env.data} to mode 0700 (it was 0755)" in out
    assert mode(env.data) == 0o700


def test_an_existing_state_dir_is_kept(env):
    env.state.mkdir(mode=0o700)
    (env.state / "alice").mkdir()  # fprintd's prints: never touched
    rc, out, _ = env.install()
    assert rc == 0
    assert f"created {env.state} (" not in out and f"created {env.data} (" in out
    assert (env.state / "alice").is_dir()


def test_an_identical_file_with_a_loose_mode_is_fixed(env):
    assert env.install()[0] == 0
    target = env.data / "module.bin"
    target.chmod(0o644)
    rc, out, _ = env.install("--dry-run")
    assert rc == 0
    assert "  would set module.bin to mode 0600, " in out and "mode 0644)" in out
    assert mode(target) == 0o644
    rc, out, err = env.install()
    assert rc == 0, out + err
    assert "  already installed: module.bin (identical)" in out
    assert "  ok    set module.bin to mode 0600, " in out and "(it was owner " in out and "mode 0644)" in out
    assert mode(target) == 0o600 and target.read_bytes() == MODULE
    assert env.run("status")[0] == 0


def test_a_failed_write_leaves_no_temp_file(env, monkeypatch):
    def rename(*args, **kwargs):
        raise PermissionError("simulated")

    monkeypatch.setattr(setup.os, "rename", rename)
    rc, _, err = env.install()
    assert rc == 1
    assert "simulated" in err
    assert list(env.data.iterdir()) == []


def test_dry_run_writes_nothing(env):
    before = tree(env.tmp)
    rc, out, err = env.install("--dry-run")
    assert rc == 0, out + err
    assert tree(env.tmp) == before
    assert f"  would create {env.state} (mode 0700" in out
    assert f"  would create {env.data} (mode 0700" in out
    assert "  would install module.bin (mode 0600" in out
    assert f"  would install program-{SERIAL}.bin (mode 0600" in out
    assert "nothing was changed" in out.splitlines()[-1]


def test_dry_run_with_force_changes_nothing(env):
    assert env.install()[0] == 0
    env.data.chmod(0o750)
    (env.src / f"program-{SERIAL}.bin").write_bytes(PROGRAM_2)
    rc, out, _ = env.install("--force", "--dry-run")
    assert rc == 0
    assert f"  would set {env.data} to mode 0700 (it is 0750)" in out
    assert f"  would replace program-{SERIAL}.bin (--force)" in out
    assert (env.data / f"program-{SERIAL}.bin").read_bytes() == PROGRAM
    assert mode(env.data) == 0o750


def test_dry_run_still_checks_the_source(env):
    (env.src / "module.bin").write_bytes(MODULE[:-1])
    rc, out, _ = env.install("--dry-run")
    assert rc == 1
    assert "FAILED module.bin is 10499 bytes" in out


def test_dry_run_when_the_state_dir_cannot_be_looked_into(env):
    env.state.mkdir(mode=0o700)
    env.state.chmod(0)
    try:
        rc, out, err = env.install("--dry-run")
    finally:
        env.state.chmod(0o700)
    assert rc == 0, out + err
    assert f"cannot look inside {env.state}" in out and "assumes it is empty" in out
    assert not env.data.exists()


def test_dry_run_when_an_installed_file_cannot_be_read(env):
    assert env.install()[0] == 0
    env.data.chmod(0)  # the data dir can be seen but not entered
    try:
        rc, out, err = env.install("--dry-run")
    finally:
        env.data.chmod(0o700)
    assert rc == 0, out + err
    for name in ("module.bin", f"program-{SERIAL}.bin"):
        assert f"  note  cannot read the installed {name} as " in out
    assert "(root only): not checked" in out
    assert f"  would set {env.data} to mode 0700 (it is 0000)" in out
    assert "Errno" not in out + err


def test_non_root_without_dry_run_is_refused_with_the_sudo_line(env):
    no_hooks = {}
    r = env.cli("install", "--from", "src", env=no_hooks, cwd=env.tmp)
    assert r.returncode == 2, r.stdout + r.stderr
    assert f"sudo synaptics0078-setup install --from {env.src}\n" in r.stderr
    assert "needs root; nothing was changed" in r.stderr
    assert r.stdout == ""


def test_non_root_refusal_keeps_force_in_the_sudo_line(env):
    r = env.cli("install", "--from", str(env.src), "--force", env={})
    assert r.returncode == 2
    assert f"sudo synaptics0078-setup install --from {env.src} --force" in r.stderr


def test_a_relative_test_root_is_refused(env):
    rc, _, err = env.install(SYNAPTICS0078_SETUP_TEST_ROOT="relative/root")
    assert rc == 2
    assert "must be an absolute path" in err


@pytest.mark.parametrize("var", TEST_VARS)
def test_the_test_hooks_are_refused_as_root(env, var):
    # As root (faked with TEST_EUID=0, which a real root run refuses as well) any test-only
    # variable stops the tool before it does anything, e.g. when passed by sudo -E.
    hooks = {var: str(env.root) if var.endswith("ROOT") else "x", "SYNAPTICS0078_SETUP_TEST_EUID": "0"}
    for argv in (("install", "--from", str(env.src)), ("status",), ("install", "--from", str(env.src), "--dry-run")):
        r = env.cli(*argv, env=hooks)
        assert r.returncode == 2, r.stdout + r.stderr
        assert f"FAILED: {var} is set" in r.stderr or "is set; the test-only variables are refused as root" in r.stderr
        assert r.stdout == ""
    assert not env.state.exists()
    # A non-zero fake euid changes nothing: the test root still works.
    r = env.cli("install", "--from", str(env.src), env=env.environ(SYNAPTICS0078_SETUP_TEST_EUID="1000"))
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_guard_reads_the_real_euid_first():
    # Real root: every test variable is refused, even with a fake non-zero euid.
    with pytest.raises(setup.Fail, match="refused as root") as exc:
        setup.load_config({"SYNAPTICS0078_SETUP_TEST_EUID": "1000"}, 0)
    assert exc.value.code == 2
    cfg = setup.load_config({}, 0)
    assert cfg.data_dir == "/var/lib/fprint/synaptics0078" and cfg.module_sha256 == setup.MODULE_SHA256
    assert cfg.expect_uid == 0 and cfg.test_root == ""


# ------------------------------------------------- the source's owner (sudo)

OTHER_UID = os.getuid() + 4242  # the "user who ran sudo" when the source is not theirs


def with_uid_of_regular_files(monkeypatch, uid: int, size: int | None = None) -> None:
    """Make fstat report `uid` as the owner of regular files (of `size` bytes, if given)."""
    real_fstat = os.fstat

    def fstat(fd):
        st = real_fstat(fd)
        if stat.S_ISREG(st.st_mode) and size in (None, st.st_size):
            fields = list(st)
            fields[stat.ST_UID] = uid
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(setup.os, "fstat", fstat)


def test_a_source_dir_of_another_user_is_refused_unlisted(env):
    # e.g. sudo synaptics0078-setup install --from /root/.ssh --dry-run
    (env.src / "secret-name.txt").write_text("secret contents")
    for args in (("--dry-run",), ()):
        rc, out, err = env.install(*args, SUDO_UID=str(OTHER_UID))
        assert rc == 1, out + err
        me, other = setup.user_name(os.getuid()), setup.user_name(OTHER_UID)
        assert f"FAILED: {env.src} is owned by {me}, not by {other} who ran sudo; refusing to look inside it" in err
        for leak in ("secret", "module.bin", "program-", "bytes", "sha256", "ignored"):
            assert leak not in out + err, leak
    assert not env.state.exists()


def test_a_source_dir_of_the_sudo_user_is_read(env):
    rc, out, err = env.install(SUDO_UID=str(os.getuid()))
    assert rc == 0, out + err
    assert "not checked" not in out
    assert (env.data / "module.bin").read_bytes() == MODULE


def test_a_source_file_of_another_user_is_refused_unread(env, monkeypatch):
    # The directory is the sudo user's, a file in it (say a hard link) is not.
    reads = []
    real_read = os.read
    monkeypatch.setattr(setup.os, "read", lambda fd, n: reads.append(n) or real_read(fd, n))
    with_uid_of_regular_files(monkeypatch, OTHER_UID, size=len(PROGRAM))
    calls = opens_recorded(monkeypatch)
    rc, out, err = env.install(SUDO_UID=str(os.getuid()))
    assert rc == 1, out + err
    me, other = setup.user_name(os.getuid()), setup.user_name(OTHER_UID)
    assert f"  FAILED program-{SERIAL}.bin is owned by {other}, not by {me} who ran sudo; not read" in out
    assert len(PROGRAM) + 1 not in reads  # only the module was read
    assert sum(path.startswith("/proc/self/fd/") for path, _, _ in calls) == 1  # and only it was opened to read
    assert all(flags & os.O_PATH for path, flags, _ in calls if path in ("module.bin", f"program-{SERIAL}.bin"))
    assert "nothing was installed" in err
    assert not env.state.exists()


def test_without_sudo_uid_the_owner_is_not_checked_and_said_so(env):
    rc, out, _ = env.install("--dry-run")
    assert rc == 0
    assert f"  note  not run through sudo (SUDO_UID is not set): the owner of {env.src} was not checked" in out


def test_sudo_uid_on_the_real_root_path():
    cfg = setup.load_config({"SUDO_UID": "1000"}, 0)
    assert cfg.source_uid == 1000 and not cfg.source_unchecked and cfg.test_root == ""
    cfg = setup.load_config({}, 0)
    assert cfg.source_uid is None and cfg.source_unchecked
    for bad in ("1000x", " 1000", "-1", "\u0661"):
        with pytest.raises(setup.Fail, match="SUDO_UID is not a number") as exc:
            setup.load_config({"SUDO_UID": bad}, 0)
        assert exc.value.code == 2
    # SUDO_UID is no test hook: as root it brings none of them back.
    with pytest.raises(setup.Fail, match="refused as root"):
        setup.load_config({"SUDO_UID": "1000", "SYNAPTICS0078_SETUP_TEST_ROOT": "/tmp/x"}, 0)
    # A plain user reads with their own rights: SUDO_UID changes nothing then.
    cfg = setup.load_config({"SUDO_UID": "4242"}, 1000)
    assert cfg.source_uid is None and not cfg.source_unchecked


# ------------------------------------------- between the plan and the writes


def plan_now(env) -> tuple:
    cfg = setup.load_config(env.environ(), os.geteuid())
    with contextlib.redirect_stdout(io.StringIO()):
        plan = setup.make_plan(cfg, setup.load_from_dir(str(env.src)), force=False, dry_run=False)
    assert plan.refusals == []
    return cfg, plan


def carry_out(cfg, plan) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup.carry_out(plan, cfg)
    return out.getvalue()


@pytest.mark.parametrize("which", ["state", "data"])
def test_a_dir_swapped_for_a_symlink_after_the_plan_is_refused(env, which):
    assert env.install()[0] == 0
    (env.src / f"program-{SERIAL_2}.bin").write_bytes(PROGRAM_2)
    cfg, plan = plan_now(env)
    assert [w[0] for w in plan.writes] == [f"program-{SERIAL_2}.bin"]
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    if which == "state":
        (elsewhere / "synaptics0078").mkdir(mode=0o700)
        env.state.rename(env.tmp / "state-old")
        link = env.state
    else:
        env.data.rename(env.tmp / "data-old")
        link = env.data
    link.symlink_to(elsewhere)
    before = tree(elsewhere)
    with pytest.raises(setup.Fail) as exc:
        carry_out(cfg, plan)
    assert str(exc.value) == f"{link} is a symlink; refusing to write through it"
    assert tree(elsewhere) == before


def test_a_dir_swapped_for_one_of_another_owner_after_the_plan_is_refused(env, monkeypatch):
    assert env.install()[0] == 0
    (env.src / f"program-{SERIAL_2}.bin").write_bytes(PROGRAM_2)
    cfg, plan = plan_now(env)
    env.data.rename(env.tmp / "data-old")
    env.data.mkdir(mode=0o700)
    swapped = (os.lstat(env.data).st_dev, os.lstat(env.data).st_ino)
    real_fstat = os.fstat

    def fstat(fd):  # the new directory belongs to someone else
        st = real_fstat(fd)
        if (st.st_dev, st.st_ino) == swapped:
            fields = list(st)
            fields[stat.ST_UID] = OTHER_UID
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(setup.os, "fstat", fstat)
    with pytest.raises(setup.Fail) as exc:
        carry_out(cfg, plan)
    assert f"{env.data} is owned by {setup.user_name(OTHER_UID)}" in str(exc.value)
    assert "refusing to use it" in str(exc.value)
    assert list(env.data.iterdir()) == []


def test_a_state_dir_that_appears_after_the_plan_is_used(env):
    cfg, plan = plan_now(env)
    assert plan.make_state_dir and plan.make_data_dir
    env.state.mkdir(mode=0o700)  # e.g. fprintd starting meanwhile
    (env.state / "alice").mkdir()
    out = carry_out(cfg, plan)
    assert f"created {env.state} (" not in out and f"  ok    created {env.data} (" in out
    assert (env.data / "module.bin").read_bytes() == MODULE
    assert (env.state / "alice").is_dir()


def test_a_data_dir_that_appears_after_the_plan_is_checked_and_used(env):
    cfg, plan = plan_now(env)
    env.state.mkdir(mode=0o700)
    env.data.mkdir()
    env.data.chmod(0o755)
    out = carry_out(cfg, plan)
    assert f"created {env.data} (" not in out
    assert f"  ok    set {env.data} to mode 0700 (it was 0755)" in out
    assert mode(env.data) == 0o700
    assert (env.data / f"program-{SERIAL}.bin").read_bytes() == PROGRAM


def test_a_symlink_that_appears_where_the_plan_makes_a_dir_is_refused(env):
    cfg, plan = plan_now(env)
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    env.state.mkdir(mode=0o700)
    env.data.symlink_to(elsewhere)
    with pytest.raises(setup.Fail) as exc:
        carry_out(cfg, plan)
    assert str(exc.value) == f"{env.data} is a symlink; refusing to write through it"
    assert list(elsewhere.iterdir()) == []


# ------------------------------------------------------------------- status


def test_status_of_a_valid_install(env):
    assert env.install()[0] == 0
    (env.data / "notes").write_text("x")
    rc, out, err = env.run("status")
    assert rc == 0, out + err
    assert "  ok    exists, owner" in out and "mode 0700" in out
    assert "  ok    module.bin: valid (10500 bytes, sha256 ok)" in out
    assert f"  ok    program-{SERIAL}.bin: valid (serial {SERIAL}, 18869 bytes" in out
    assert "  note  other file (not used by the driver): notes" in out


def test_status_through_the_script(env):
    assert env.install()[0] == 0
    r = env.cli("status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[-1].startswith("ready")


def test_status_without_the_data_dir(env):
    rc, out, _ = env.run("status")
    assert rc == 1
    assert "  FAILED missing: nothing is installed yet" in out
    assert "sudo synaptics0078-setup install --from DIR" in out


def test_status_with_an_invalid_module(env):
    assert env.install()[0] == 0
    (env.data / "module.bin").write_bytes(bytes(10500))
    rc, out, _ = env.run("status")
    assert rc == 1
    assert "  FAILED module.bin: invalid: it has sha256" in out


def test_status_with_an_invalid_program(env):
    assert env.install()[0] == 0
    (env.data / f"program-{SERIAL_2}.bin").write_bytes(b"\x06" + PROGRAM[1:])
    rc, out, _ = env.run("status")
    assert rc == 1
    assert f"  FAILED program-{SERIAL_2}.bin: invalid: it starts with 0x06, expected 0x02" in out


def test_status_without_a_program(env):
    assert env.install()[0] == 0
    (env.data / f"program-{SERIAL}.bin").unlink()
    rc, out, _ = env.run("status")
    assert rc == 1
    assert "  FAILED program-<serial>.bin: missing" in out


def test_status_with_a_symlinked_file(env):
    assert env.install()[0] == 0
    target = env.data / "module.bin"
    target.rename(env.tmp / "m")
    target.symlink_to(env.tmp / "m")
    rc, out, _ = env.run("status")
    assert rc == 1
    assert "  FAILED module.bin: invalid: it is a symlink" in out


def test_status_with_the_wrong_mode(env):
    assert env.install()[0] == 0
    env.data.chmod(0o755)
    rc, out, _ = env.run("status")
    assert rc == 1
    assert "mode 0755: expected mode 0700" in out


def test_status_with_the_wrong_owner(env, monkeypatch):
    assert env.install()[0] == 0
    monkeypatch.setattr(setup.os, "getuid", lambda: os.geteuid() + 4242)
    rc, out, _ = env.run("status")
    assert rc == 1
    assert "expected owner" in out


def test_status_flags_a_data_file_with_a_loose_mode(env):
    assert env.install()[0] == 0
    (env.data / f"program-{SERIAL}.bin").chmod(0o640)
    rc, out, _ = env.run("status")
    assert rc == 1
    assert f"  FAILED program-{SERIAL}.bin: valid, but owner " in out
    assert "mode 0640: expected mode 0600, " in out and "(install sets it)" in out
    assert "  ok    module.bin: valid" in out
    assert out.splitlines()[-1].startswith("not ready")


def test_status_flags_a_data_file_of_another_owner(env, monkeypatch):
    assert env.install()[0] == 0
    with_uid_of_regular_files(monkeypatch, OTHER_UID)
    rc, out, _ = env.run("status")
    assert rc == 1
    assert f"  FAILED module.bin: valid, but owner {setup.user_name(OTHER_UID)}:" in out
    assert out.splitlines()[-1].startswith("not ready")


def test_status_with_a_symlinked_data_dir(env):
    env.state.mkdir(mode=0o700)
    (env.tmp / "real").mkdir(mode=0o700)
    env.data.symlink_to(env.tmp / "real")
    rc, out, _ = env.run("status")
    assert rc == 1
    assert "  FAILED is a symlink" in out


def test_status_as_a_user_who_cannot_look_inside(env):
    assert env.install()[0] == 0
    env.state.chmod(0)  # like /var/lib/fprint (0700 root) seen by a user
    try:
        rc, out, _ = env.run("status")
    finally:
        env.state.chmod(0o700)
    assert rc == 1
    assert f"{env.state} is readable by root only" in out
    assert out.splitlines()[-1] == "    sudo synaptics0078-setup status"


def test_status_never_writes(env):
    assert env.install()[0] == 0
    env.data.chmod(0o750)
    before = [(p, p.stat().st_mtime_ns, mode(p)) for p in [env.data, *env.data.iterdir()]]
    assert env.run("status")[0] == 1
    assert [(p, p.stat().st_mtime_ns, mode(p)) for p in [env.data, *env.data.iterdir()]] == before


# ------------------------------------------------------------ phase B seam


def test_install_takes_a_dataset_from_any_loader(env):
    """Checking and installing look only at the DataSet, not at where it came from."""
    cfg = setup.load_config(env.environ(), os.geteuid())
    ds = setup.DataSet(origin="phase B loader", module=MODULE, programs={SERIAL: PROGRAM})
    assert [c.problem for c in setup.check_dataset(ds, cfg)] == [None, None]
    with contextlib.redirect_stdout(io.StringIO()):
        setup.carry_out(setup.make_plan(cfg, ds, force=False, dry_run=False), cfg)
    assert (env.data / f"program-{SERIAL}.bin").read_bytes() == PROGRAM
    empty = setup.DataSet(origin="nowhere")
    assert [(c.name, c.present) for c in setup.check_dataset(empty, cfg)] == [
        ("module.bin", False),
        ("program-<serial>.bin", False),
    ]


def test_load_from_dir_returns_the_bytes(env):
    ds = setup.load_from_dir(str(env.src))
    assert ds.module == MODULE and ds.programs == {SERIAL: PROGRAM}
    assert ds.unreadable == {} and ds.others == []


# =================================================================== phase B (B5b)
# install without --from, --from-file, recalibrate, fetch, the extended status. Synthetic
# fixtures only (synaptics0078/tests/fixtures.py): a fake DLL with the fake module, the
# template it gives, pins for both, a stored cab around the DLL, and the fake reader's
# replies. The serial is fixtures' synthetic one.

SP = fx.synthetic_pins()
TEMPLATE = fx.expected_template()
DLL = fx.synthetic_dll()
CAB = fx.make_cab([("synaWudfBioUsb.dll", DLL), ("readme.txt", b"synthetic\n")])
CAB_SHA = hashlib.sha256(CAB).hexdigest()
SYS_SERIAL = setup.serial_hex(fx.SERIAL)  # the USB serial sysfs shows: nibble-swapped RomInfo bytes
OTHER_SERIAL_RAW = "0102030405a6"
ROM_SERIAL_RAW = fx.SERIAL.hex()
PROGRAM_OK = calibration.assemble_cmd02(TEMPLATE, fx.unit_chunks())
LABEL = "Test Catalog"

NEW_HOOKS = [
    "SYNAPTICS0078_SETUP_TEST_PINS",
    "SYNAPTICS0078_SETUP_TEST_ARCHIVES",
    "SYNAPTICS0078_SETUP_TEST_LIBDIR",
    "SYNAPTICS0078_SETUP_TEST_CURL",
    "SYNAPTICS0078_SETUP_TEST_SYSTEMCTL",
    "SYNAPTICS0078_SETUP_TEST_SETPRIV",
    "SYNAPTICS0078_SETUP_TEST_PRLIMIT",
    "SYNAPTICS0078_SETUP_TEST_SYSFS",
    "SYNAPTICS0078_SETUP_TEST_TRANSPORT",
]


def archive(name, url, sha=CAB_SHA, label=LABEL, max_time=300, max_bytes=16 << 20):
    return {
        "name": name,
        "label": label,
        "url": url,
        "size": len(CAB),
        "sha256": sha,
        "kind": "cab",
        "max_bytes": max_bytes,
        "max_time": max_time,
    }


ARCHIVES = [
    archive("catalog-a", "https://downloads.invalid/a.cab"),
    archive("catalog-b", "https://downloads.invalid/b.cab"),
    archive("mirror-c", "https://mirror.invalid/c.cab", label="Test Mirror", max_time=600),
]
URL_A, URL_B, URL_C = (a["url"] for a in ARCHIVES)

FAKE_CURL = """#!/usr/bin/python3 -I
import json, os, shutil, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "curl-calls.jsonl"), "a") as fh:
    fh.write(json.dumps(sys.argv) + "\\n")
with open(os.path.join(here, "curl-serve.json")) as fh:
    serve = json.load(fh)
src = serve.get(sys.argv[-1])
if src is None:
    sys.stderr.write("curl: (22) The requested URL returned error: 404\\n")
    sys.exit(22)
shutil.copyfile(src, sys.argv[sys.argv.index("--output") + 1])
"""

FAKE_SYSTEMCTL = """#!/usr/bin/python3 -I
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "systemctl-calls.jsonl"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
state = os.path.join(here, "fprintd-state")
if sys.argv[1] == "is-active":
    with open(state) as fh:
        sys.exit(0 if fh.read().strip() == "active" else 3)
if sys.argv[1] == "stop":
    with open(state, "w") as fh:
        fh.write("inactive")
    sys.exit(0)
sys.exit(1)
"""

# Stands in for setpriv: records its argv, checks the privilege options, runs the rest.
FAKE_SETPRIV = """#!/usr/bin/python3 -I
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "setpriv-calls.jsonl"), "a") as fh:
    fh.write(json.dumps(sys.argv) + "\\n")
if sys.argv[1:5] != ["--reuid=65534", "--regid=65534", "--clear-groups", "--no-new-privs"]:
    sys.exit(97)
os.execv(sys.argv[5], sys.argv[5:])
"""

# Stands in for prlimit (run by setpriv): records its argv and whether it leads a session of
# its own (so it has no controlling terminal), applies the limits, runs the command after
# `--`. Not --nproc: the kernel counts it per real uid, and here the uid is the test
# user's (the fake setpriv changes none), with all their other processes.
FAKE_PRLIMIT = """#!/usr/bin/python3 -I
import json, os, resource, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "prlimit-calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": sys.argv, "session_leader": os.getsid(0) == os.getpid()}) + "\\n")
end = sys.argv.index("--")
for opt in sys.argv[1:end]:
    name, value = opt[2:].split("=")
    if name != "nproc":
        which = {"as": resource.RLIMIT_AS, "cpu": resource.RLIMIT_CPU, "fsize": resource.RLIMIT_FSIZE}[name]
        resource.setrlimit(which, (int(value), int(value)))
os.execv(sys.argv[end + 1], sys.argv[end + 1 :])
"""

# The fake reader behind session.UsbTransport: replies by opcode, one EP 0x82 capture per 02,
# RomInfo byte 17 becomes 0x0e after a 06. SCENARIO and EVENTS are filled in per test.
FAKE_TRANSPORT = """
import json
import os
import signal
import types

from synaptics0078 import session
from synaptics0078.tests import fixtures as fx

SCENARIO = __SCENARIO__
EVENTS = __EVENTS__
REPLIES = {0x19: b"\\x00\\x00", 0x75: fx.SENSOR_ID, 0x06: b"\\x00\\x00", 0x02: b"\\x00\\x00" + bytes(30),
           0x04: b"\\x12\\x04", 0x52: b"\\x00\\x00"}


def event(**kw):
    with open(EVENTS, "a") as fh:
        fh.write(json.dumps(kw) + "\\n")


class Reader:
    def __init__(self):
        rom = bytearray(fx.rom_info(SCENARIO["byte17"]))
        rom[0x12:0x18] = bytes.fromhex(SCENARIO["rom_serial"])
        self.rom, self.touched, self.last, self.current, self.check = bytes(rom), SCENARIO["touched"], None, b"", None
        self.stopped = False

    def write(self, ep, data, timeout=None):
        data = bytes(data)
        event(op="write", opcode=data[0], length=len(data))
        self.last = data[0]
        if self.last == 0x04 and self.stopped and SCENARIO["stop"] != "interrupt":
            os.kill(os.getpid(), getattr(signal, SCENARIO["stop"]))  # the second copy: ignored
        if self.last == 0x02:
            self.current = fx.raw_capture(fx.CF_PIX if len(data) == 1917 else fx.BF_RAW_PIX)
        elif self.last == 0x06:
            rom = bytearray(self.rom)
            rom[17] = 0x0E
            self.rom = bytes(rom)
        elif self.last == 0x6F:
            self.check = fx.sub12(shift=40) if self.touched > 0 else None
            self.touched -= 1
        return len(data)

    def read(self, ep, size, timeout=None):
        if ep == 0x82 and SCENARIO["ep82_errno"]:
            raise OSError(SCENARIO["ep82_errno"], "fake USB error")
        if ep == 0x82 and SCENARIO["stop"] and not self.stopped:  # mid-capture: Ctrl-C, or a signal
            self.stopped = True
            if SCENARIO["stop"] == "interrupt":
                raise KeyboardInterrupt
            os.kill(os.getpid(), getattr(signal, SCENARIO["stop"]))
        if ep == 0x82:
            out, self.current = self.current[:size], self.current[size:]
            return out
        if self.last == 0x01:
            return self.rom
        if self.last == 0x6F:
            return fx.factory_reply(self.check)
        return REPLIES[self.last]


STATE = {"opens": 0}
READER = Reader()


class FakeTransport(session.UsbTransport):
    def open(self):
        STATE["opens"] += 1
        event(op="open", n=STATE["opens"])
        if STATE["opens"] <= SCENARIO["busy_opens"]:
            raise session.StepFailed("interface busy (fake)", session.EXIT_DRIVER_BUSY, session.MSG_BUSY,
                                     "the reader is busy")
        self.dev = READER
        self._core = types.SimpleNamespace(USBTimeoutError=TimeoutError, USBError=OSError)
        self.claimed = True

    def close(self):
        if self.dev is not None:
            event(op="close")
        self.dev = None


def make_transport(log, guard):
    return FakeTransport(log, guard=guard)
"""


def write_exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def jsonl(path: Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Setup:
    """A test root with sysfs, /var/lib, /var/log; fake curl, systemctl, setpriv and reader."""

    def __init__(self, tmp: Path, monkeypatch):
        self.tmp, self.mp = tmp, monkeypatch
        self.root = tmp / "root"
        for d in ("var/lib", "var/log"):
            (self.root / d).mkdir(parents=True)
        self.state = self.root / "var" / "lib" / "fprint"
        self.data = self.state / "synaptics0078"
        self.setup_dir = self.root / "var" / "lib" / "synaptics0078"
        self.vendor = self.setup_dir / "vendor"
        self.backup = self.setup_dir / "backup"
        self.log = self.root / "var" / "log" / "synaptics0078-setup.log"
        self.program = self.data / f"program-{SYS_SERIAL}.bin"
        self.fakes = tmp / "fakes"
        self.fakes.mkdir()
        self.curl = write_exe(self.fakes / "curl", FAKE_CURL)
        self.systemctl = write_exe(self.fakes / "systemctl", FAKE_SYSTEMCTL)
        self.setpriv = write_exe(self.fakes / "setpriv", FAKE_SETPRIV)
        self.prlimit = write_exe(self.fakes / "prlimit", FAKE_PRLIMIT)
        self.events_path = tmp / "usb-events.jsonl"
        self.transport = tmp / "fake_transport.py"
        self.served = tmp / "served"
        self.served.mkdir()
        (self.served / "a.cab").write_bytes(CAB)
        self.files = tmp / "files"
        self.files.mkdir()
        self.set_pins(SP)
        self.set_archives(ARCHIVES)
        self.serve({URL_A: "a.cab"})
        self.fprintd("inactive")
        self.add_reader()
        self.scenario()
        self.sleeps: list[float] = []
        self.prompts = 0
        monkeypatch.setattr(setup, "sleep", self.sleeps.append)
        self.tty(False)

    # -- configuration --------------------------------------------------------

    def set_pins(self, pins) -> None:
        self.pins_path = self.tmp / f"pins-{pins.template_sha256[:8]}.json"
        self.pins_path.write_text(pins.to_json())

    def set_archives(self, archives) -> None:
        self.archives_path = self.tmp / "archives.json"
        self.archives_path.write_text(json.dumps(archives))

    def serve(self, mapping: dict) -> None:
        (self.fakes / "curl-serve.json").write_text(json.dumps({u: str(self.served / f) for u, f in mapping.items()}))

    def fprintd(self, state: str) -> None:
        (self.fakes / "fprintd-state").write_text(state)

    def add_reader(self, serial: str = SYS_SERIAL, name: str = "1-3") -> None:
        devices = self.root / "sys" / "bus" / "usb" / "devices"
        for dev, vid, pid, ser in ((name, "06cb", "0078", serial), ("1-4", "8087", "0029", "")):
            d = devices / dev
            d.mkdir(parents=True, exist_ok=True)
            (d / "idVendor").write_text(vid + "\n")
            (d / "idProduct").write_text(pid + "\n")
            if ser:
                (d / "serial").write_text(ser + "\n")
        (devices / f"{name}:1.0").mkdir(exist_ok=True)  # an interface: skipped

    def remove_readers(self) -> None:
        for d in (self.root / "sys" / "bus" / "usb" / "devices").iterdir():
            for f in d.iterdir():
                f.unlink()
            d.rmdir()

    def scenario(self, byte17=0, rom_serial=ROM_SERIAL_RAW, touched=0, busy_opens=0, ep82_errno=0, stop="") -> None:
        sc = {
            "byte17": byte17,
            "rom_serial": rom_serial,
            "touched": touched,
            "busy_opens": busy_opens,
            "ep82_errno": ep82_errno,
            "stop": stop,  # "interrupt" (Ctrl-C) or a signal name, at the first capture read
        }
        text = FAKE_TRANSPORT.replace("__SCENARIO__", repr(sc)).replace("__EVENTS__", repr(str(self.events_path)))
        self.transport.write_text(text)

    def tty(self, on: bool) -> None:
        def read_line():
            self.prompts += 1
            return "\n"

        self.mp.setattr(setup, "stdin_is_tty", lambda: on)
        self.mp.setattr(setup, "read_line", read_line)

    # -- running --------------------------------------------------------------

    def environ(self, **extra) -> dict[str, str]:
        env = {
            "SYNAPTICS0078_SETUP_TEST_ROOT": str(self.root),
            "SYNAPTICS0078_SETUP_TEST_PINS": str(self.pins_path),
            "SYNAPTICS0078_SETUP_TEST_ARCHIVES": str(self.archives_path),
            "SYNAPTICS0078_SETUP_TEST_CURL": str(self.curl),
            "SYNAPTICS0078_SETUP_TEST_SYSTEMCTL": str(self.systemctl),
            "SYNAPTICS0078_SETUP_TEST_SETPRIV": str(self.setpriv),
            "SYNAPTICS0078_SETUP_TEST_PRLIMIT": str(self.prlimit),
            "SYNAPTICS0078_SETUP_TEST_TRANSPORT": str(self.transport),
        }
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, *argv: str, **extra) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = setup.main(list(argv), self.environ(**extra))
        return rc, out.getvalue(), err.getvalue()

    def cli(self, *argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        base = {k: v for k, v in os.environ.items() if not k.startswith("SYNAPTICS0078_SETUP_") and k != "SUDO_UID"}
        base.update(self.environ() if env is None else env)
        return subprocess.run(
            [str(SCRIPT), *argv], capture_output=True, text=True, env=base, stdin=subprocess.DEVNULL, check=False
        )

    # -- what happened --------------------------------------------------------

    def curl_calls(self) -> list[list[str]]:
        return jsonl(self.fakes / "curl-calls.jsonl")

    def curl_urls(self) -> list[str]:
        return [c[-1] for c in self.curl_calls()]

    def setpriv_calls(self) -> list[list[str]]:
        return jsonl(self.fakes / "setpriv-calls.jsonl")

    def prlimit_calls(self) -> list[dict]:
        return jsonl(self.fakes / "prlimit-calls.jsonl")

    def systemctl_calls(self) -> list[list[str]]:
        return jsonl(self.fakes / "systemctl-calls.jsonl")

    def events(self) -> list[dict]:
        return jsonl(self.events_path)

    def opcodes(self) -> list[int]:
        return [e["opcode"] for e in self.events() if e["op"] == "write"]

    def reset_calls(self) -> None:
        for name in ("curl-calls.jsonl", "setpriv-calls.jsonl", "prlimit-calls.jsonl", "systemctl-calls.jsonl"):
            (self.fakes / name).unlink(missing_ok=True)
        self.events_path.unlink(missing_ok=True)

    def cached(self) -> list[str]:
        return sorted(p.name for p in self.vendor.iterdir()) if self.vendor.exists() else []


@pytest.fixture
def b5(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


FULL_SEQUENCE = [0x01, 0x19, 0x75, 0x6F, 0x06, 0x01, 0x02, 0x04, 0x52, 0x02, 0x04, 0x52]
NO_MODULE_SEQUENCE = [0x01, 0x19, 0x75, 0x6F, 0x02, 0x04, 0x52, 0x02, 0x04, 0x52]


def assert_installed(b5: Setup, program: bytes = PROGRAM_OK) -> None:
    assert (b5.data / "module.bin").read_bytes() == fx.MODULE
    assert b5.program.read_bytes() == program
    assert mode(b5.data) == 0o700
    assert mode(b5.data / "module.bin") == 0o600 and mode(b5.program) == 0o600
    assert sorted(p.name for p in b5.data.iterdir()) == ["module.bin", b5.program.name]  # no temp files


def assert_nothing_installed(b5: Setup) -> None:
    assert not b5.data.exists() or list(b5.data.iterdir()) == []
    assert not (b5.setup_dir / "calibration.json").exists()


def serial_forms() -> list[str]:
    forms = []
    for s in (SYS_SERIAL, fx.SERIAL.hex()):
        forms += [s, s.upper()]
    return forms


# --------------------------------------------------------------- downloads


def test_curl_argv_is_exact():
    a = pins_mod.Archive(**ARCHIVES[2])
    assert setup.curl_argv("/usr/bin/curl", a, "/var/lib/synaptics0078/vendor/.download-x.tmp") == [
        "/usr/bin/curl",
        "-q",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--tlsv1.2",
        "--fail",
        "--location",
        "--max-redirs",
        "3",
        "--connect-timeout",
        "20",
        "--max-time",
        "600",
        "--max-filesize",
        str(16 << 20),
        "--retry",
        "2",
        "--retry-delay",
        "2",
        "--silent",
        "--show-error",
        "--remove-on-error",
        "--output",
        "/var/lib/synaptics0078/vendor/.download-x.tmp",
        "https://mirror.invalid/c.cab",
    ]


def test_the_real_sources_are_https_with_their_caps():
    real = pins_mod.ARCHIVES
    assert len(real) == 6
    for a in real:
        argv = setup.curl_argv(setup.CURL, a, "/tmp/x")
        assert argv[:2] == ["/usr/bin/curl", "-q"] and argv[-1] == a.url and a.url.startswith("https://")
        assert argv[argv.index("--max-time") + 1] == str(a.max_time)
        assert argv[argv.index("--max-filesize") + 1] == str(a.max_bytes)
    assert [a.max_time for a in real] == [300, 300, 600, 600, 300, 600]  # the Wayback copies are slower


def test_the_curl_run_outlasts_every_try(b5, monkeypatch):
    # --max-time caps each try, --retry 2 makes 3 tries with --retry-delay 2 between them.
    a = pins_mod.Archive(**ARCHIVES[2])
    argv = setup.curl_argv("/usr/bin/curl", a, "/x")
    tries = int(argv[argv.index("--retry") + 1]) + 1
    delays = (tries - 1) * int(argv[argv.index("--retry-delay") + 1])
    assert setup.curl_timeout_s(a) == tries * a.max_time + delays + 60 == 1864
    timeouts, real_run = [], subprocess.run
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: timeouts.append(k.get("timeout")) or real_run(*a, **k))
    assert b5.run("fetch")[0] == 0
    assert timeouts == [3 * 300 + 4 + 60]


def test_a_download_runs_curl_exactly_and_is_parsed_in_process(b5):
    rc, out, err = b5.run("fetch")
    assert rc == 0, out + err
    (call,) = b5.curl_calls()
    output = call[call.index("--output") + 1]
    assert re.fullmatch(re.escape(str(b5.vendor)) + r"/\.download-[0-9a-f]{16}\.tmp", output)
    assert call == setup.curl_argv(str(b5.curl), pins_mod.Archive(**ARCHIVES[0]), output)
    assert b5.setpriv_calls() == []  # a known archive (its sha256 is pinned): parsed in this process
    assert b5.cached() == [f"{CAB_SHA}-catalog-a", "manifest.json"]
    assert (b5.vendor / f"{CAB_SHA}-catalog-a").read_bytes() == CAB
    assert mode(b5.setup_dir) == 0o700 and mode(b5.vendor) == 0o700
    assert mode(b5.vendor / f"{CAB_SHA}-catalog-a") == 0o600
    manifest = json.loads((b5.vendor / "manifest.json").read_text())
    entry = manifest[f"{CAB_SHA}-catalog-a"]
    assert entry["url"] == URL_A and entry["gave"] == ["module", "template"] and entry["label"] == LABEL
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT.*", entry["date"]) and entry["size"] == len(CAB)
    assert out.splitlines() == [
        f"Getting the vendor files ({LABEL})... done, checked.",
        f"Saved in {b5.vendor}; sudo synaptics0078-setup install now works without the network.",
    ]


def test_sources_are_tried_in_order_until_one_serves(b5):
    b5.serve({URL_C: "a.cab"})  # sources 1 and 2 fail (404), 3 serves
    rc, out, err = b5.run("fetch")
    assert rc == 0, out + err
    assert b5.curl_urls() == [URL_A, URL_B, URL_C]
    assert out.splitlines()[:3] == [
        f"Getting the vendor files ({LABEL})... not available, trying the next source.",
        f"Getting the vendor files ({LABEL})... not available, trying the next source.",
        "Getting the vendor files (Test Mirror)... done, checked.",
    ]
    assert b5.cached() == [f"{CAB_SHA}-mirror-c", "manifest.json"]
    assert [p.name for p in b5.vendor.glob(".download-*")] == []  # failed downloads leave nothing


def test_source_2_serves_when_source_1_fails(b5):
    b5.serve({URL_B: "a.cab"})
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    assert b5.curl_urls() == [URL_A, URL_B]
    assert_installed(b5)


def test_no_source_serves(b5):
    b5.serve({})
    rc, out, err = b5.run("install")
    assert rc == 1
    assert b5.curl_urls() == [URL_A, URL_B, URL_C]
    assert out.splitlines()[-1] == "Getting the vendor files (Test Mirror)... not available."
    assert "FAILED: Could not download the vendor files from any source. Download one of them yourself" in err
    assert "(synaptics0078-setup status lists the URLs)" in err
    assert "sudo synaptics0078-setup install --from-file FILE" in err
    assert "hint: behind a proxy (sudo drops https_proxy), run: " in err
    assert "sudo --preserve-env=https_proxy synaptics0078-setup install\n" in err
    assert b5.events() == []  # never got to the reader
    assert_nothing_installed(b5)


def test_no_source_serves_through_a_proxy(b5):
    b5.serve({})
    rc, _, err = b5.run("fetch", https_proxy="http://proxy.invalid:3128")
    assert rc == 1
    assert "preserve-env" not in err and f"hint: details in the log: {b5.log}" in err
    assert all(c[-1].startswith("https://") for c in b5.curl_calls())


def test_a_bad_download_is_not_used(b5):
    (b5.served / "junk.cab").write_bytes(b"MSCF" + bytes(200))
    b5.serve({URL_A: "junk.cab", URL_B: "a.cab"})
    rc, out, err = b5.run("fetch")
    assert rc == 0, out + err
    assert b5.curl_urls() == [URL_A, URL_B]
    assert b5.cached() == [f"{CAB_SHA}-catalog-b", "manifest.json"]


def test_the_cache_is_used_offline(b5):
    assert b5.run("fetch")[0] == 0
    b5.reset_calls()
    b5.serve({})  # the network is gone
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    assert b5.curl_calls() == []
    assert f"Using the vendor files saved earlier ({LABEL})... done, checked." in out.splitlines()
    assert_installed(b5)


def test_a_corrupt_cache_entry_is_ignored_and_replaced(b5):
    b5.vendor.mkdir(parents=True)
    b5.vendor.chmod(0o700)
    b5.setup_dir.chmod(0o700)
    bad = b5.vendor / f"{CAB_SHA}-catalog-a"
    bad.write_bytes(CAB[:-1] + b"X")  # not the content its name says
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    assert b5.curl_urls() == [URL_A]
    assert bad.read_bytes() == CAB
    assert "Using the vendor files saved earlier" not in out
    assert "does not have the sha256 of its name; removed" in b5.log.read_text()
    assert_installed(b5)


def test_an_unknown_archive_is_parsed_unprivileged(b5):
    resigned = [archive("catalog-a", URL_A, sha="11" * 32)]  # e.g. Microsoft re-signed the cab
    b5.set_archives(resigned)
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    (call,) = b5.setpriv_calls()
    pkgdir = str(REPO / "synaptics0078")
    child = ["/usr/bin/python3", "-IB", "-c", setup.child_code(pkgdir), "--pins", str(b5.pins_path)]
    limits = ["--as=1073741824", "--cpu=120", "--nproc=64", "--fsize=0", "--"]
    assert call == [
        str(b5.setpriv),
        "--reuid=65534",
        "--regid=65534",
        "--clear-groups",
        "--no-new-privs",
        str(b5.prlimit),
        *limits,
        *child,
    ]
    assert repr(pkgdir) in child[3] and "vendor.child_entry()" in child[3]
    (limited,) = b5.prlimit_calls()  # run by setpriv, so after the switch to nobody
    assert limited["argv"] == [str(b5.prlimit), *limits, *child]
    assert limited["session_leader"]  # a session of its own: no controlling terminal
    assert_installed(b5)
    assert b5.cached() == [f"{CAB_SHA}-catalog-a", "manifest.json"]  # named by its real sha256


def test_a_failing_unprivileged_extractor_is_a_failed_source(b5):
    b5.set_archives([archive("catalog-a", URL_A, sha="11" * 32)])
    write_exe(b5.setpriv, "#!/bin/sh\nexit 1\n")  # e.g. setpriv refused
    rc, _out, err = b5.run("install")
    assert rc == 1
    assert "Could not download the vendor files from any source" in err
    assert_nothing_installed(b5)


def test_status_lists_the_source_urls(b5):
    rc, out, _ = b5.run("status")
    assert rc == 1
    lines = out.splitlines()
    i = lines.index("vendor file sources (tried in this order; for --from-file any one of them will do):")
    assert lines[i + 1 : i + 4] == [f"  1. {LABEL}: {URL_A}", f"  2. {LABEL}: {URL_B}", f"  3. Test Mirror: {URL_C}"]


def test_status_lists_the_real_sources_without_the_archives_hook(b5):
    _rc, out, _ = b5.run("status", SYNAPTICS0078_SETUP_TEST_ARCHIVES=None)
    for a in pins_mod.ARCHIVES:
        assert f": {a.url}" in out


# ------------------------------------------------------------- calibration


def test_full_install(b5):
    b5.tty(True)
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    assert out.splitlines() == [
        "Fingerprint reader 06cb:0078 found.",
        f"Getting the vendor files ({LABEL})... done, checked.",
        "Keep your fingers OFF the reader, then press Enter.",
        "Calibrating... done.",
        f"Ready. Enroll a finger with: fprintd-enroll   (log: {b5.log})",
    ]
    assert err == ""
    assert b5.prompts == 1
    assert_installed(b5)
    assert b5.events()[0]["op"] == "open" and b5.events()[-1] == {"op": "close"}
    assert b5.opcodes() == FULL_SEQUENCE  # byte 17 was 0: the module was uploaded once
    assert b5.systemctl_calls() == [["is-active", "--quiet", "fprintd"]]  # inactive: not stopped
    record = json.loads((b5.setup_dir / "calibration.json").read_text())
    assert record["check_score"] == 0 and record["check_max"] == 200 and record["attempts"] == 1
    assert record["module_uploaded"] is True and record["template_sha256"] == SP.template_sha256
    assert record["tool_version"] == setup.TOOL_VERSION and record["how"] == "calibrated"
    assert 127 < record["bf_mean"] < 129 and record["bf_sd"] < 6
    assert mode(b5.setup_dir / "calibration.json") == 0o600
    text = (b5.setup_dir / "calibration.json").read_text()
    for form in serial_forms():
        assert form not in text


def test_no_serial_on_the_terminal_or_in_the_log(b5):
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    log = b5.log.read_text()
    assert mode(b5.log) == 0o600
    assert "<serial>" in log  # the program's name, redacted
    for form in serial_forms():
        assert form not in out + err, form
        assert form not in log, form
    assert "calibration attempt 1 of 3" in log and "RomInfo: ROM 6/0x14" in log  # the detail is there


def test_without_a_tty_a_countdown_replaces_enter(b5):
    rc, out, _ = b5.run("install")
    assert rc == 0
    assert "Keep your fingers OFF the reader; starting in 3 seconds." in out.splitlines()
    assert b5.prompts == 0 and b5.sleeps == [3]


def test_yes_gives_the_countdown_on_a_tty_too(b5):
    b5.tty(True)
    rc, _out, _ = b5.run("install", "--yes")
    assert rc == 0
    assert b5.prompts == 0 and b5.sleeps == [3]


def test_a_touched_reader_is_retried(b5):
    b5.tty(True)
    b5.scenario(touched=1)
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    assert out.splitlines()[2:6] == [
        "Keep your fingers OFF the reader, then press Enter.",
        "Calibrating... the reader seemed touched.",
        "The reader seemed touched; lift your fingers and press Enter.",
        "Calibrating... done.",
    ]
    assert b5.prompts == 2
    # attempt 1 stops after capture 1 (score > 200); attempt 2 runs both; the module goes once
    assert b5.opcodes() == FULL_SEQUENCE[:9] + NO_MODULE_SEQUENCE
    assert json.loads((b5.setup_dir / "calibration.json").read_text())["attempts"] == 2
    assert_installed(b5)


def test_a_reader_touched_three_times_writes_nothing(b5):
    b5.scenario(touched=3)
    rc, out, err = b5.run("install")
    assert rc == 1
    assert out.count("Calibrating... the reader seemed touched.") == 3
    assert "FAILED: The reader seemed touched in 3 attempts, so nothing was written." in err
    assert "hint: sudo synaptics0078-setup install" in err
    assert b5.opcodes().count(0x02) == 3
    assert_nothing_installed(b5)


def test_busy_then_fprintd_stopped_then_retry(b5):
    b5.fprintd("active")
    b5.scenario(busy_opens=1)
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    assert "Stopped fprintd for the calibration (it starts again by itself when needed)." in out.splitlines()
    assert b5.systemctl_calls() == [["is-active", "--quiet", "fprintd"], ["stop", "fprintd"], ["stop", "fprintd"]]
    assert [e["n"] for e in b5.events() if e["op"] == "open"] == [1, 2]
    assert b5.sleeps[0] == 1  # one second between the stop and the retry
    assert_installed(b5)


def test_still_busy_after_the_retry(b5):
    b5.scenario(busy_opens=2)
    rc, _out, err = b5.run("install")
    assert rc == 1
    assert "FAILED: The reader is still in use by another program. Close every fingerprint prompt" in err
    assert "hint: sudo synaptics0078-setup install" in err
    assert [e["op"] for e in b5.events()] == ["open", "open"]
    assert_nothing_installed(b5)


def test_a_loaded_module_is_not_uploaded_again(b5):
    b5.scenario(byte17=0x0E)
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    assert b5.opcodes() == NO_MODULE_SEQUENCE
    assert json.loads((b5.setup_dir / "calibration.json").read_text())["module_uploaded"] is False
    assert_installed(b5)


def test_an_unexpected_module_is_refused(b5):
    b5.scenario(byte17=0x0F)  # e.g. module 40 left by Windows
    rc, out, err = b5.run("install")
    assert rc == 1
    assert "FAILED: Another capture module is loaded in the reader" in err
    assert "Restart the computer, then run again." in err and "hint: sudo synaptics0078-setup install" in err
    assert b5.opcodes() == [0x01, 0x19, 0x75, 0x6F]  # nothing that changes the reader's state
    assert out.splitlines()[-1] == "Calibrating... stopped."
    assert_nothing_installed(b5)


def test_a_serial_mismatch_is_refused(b5):
    b5.scenario(rom_serial=OTHER_SERIAL_RAW)
    rc, out, err = b5.run("install")
    assert rc == 1
    assert "FAILED: The reader reported a different serial number than the one seen on USB" in err
    assert b5.opcodes() == [0x01, 0x19, 0x75, 0x6F]
    assert_nothing_installed(b5)
    for form in serial_forms() + [OTHER_SERIAL_RAW]:
        assert form not in out + err


def test_the_guard_refuses_a_program_whose_skeleton_differs(b5):
    b5.set_pins(dataclasses.replace(SP, program1_skeleton_sha256="0" * 64))
    rc, _out, err = b5.run("install")
    assert rc == 1
    assert "FAILED: Stopped before sending a command that is not allowed, so nothing was written" in err
    assert 0x02 not in b5.opcodes()
    assert "skeleton sha256" in b5.log.read_text()
    assert_nothing_installed(b5)


def test_a_disconnect_during_the_capture_sends_nothing_more(b5):
    b5.scenario(ep82_errno=19)  # ENODEV
    rc, out, err = b5.run("install")
    assert rc == 1
    assert "FAILED: The reader was disconnected." in err and "Nothing was written." in err
    assert f"(log: {b5.log})" in err and "hint: run again: sudo synaptics0078-setup install" in err
    assert b5.opcodes()[-1] == 0x02  # no 04/52 to a reader that is gone
    assert out.splitlines()[-1] == "Calibrating... stopped."
    assert_nothing_installed(b5)


def test_a_usb_error_during_the_capture_still_stops_the_reader(b5):
    b5.scenario(ep82_errno=32)  # EPIPE
    rc, _, err = b5.run("install")
    assert rc == 1
    assert "FAILED: The reader did not answer as expected" in err
    assert b5.opcodes()[-3:] == [0x02, 0x04, 0x52]  # the exit-path cleanup
    assert b5.events()[-1] == {"op": "close"}
    assert_nothing_installed(b5)


def test_ctrl_c_during_the_capture_still_stops_the_reader(b5):
    b5.scenario(stop="interrupt")
    rc, out, err = b5.run("install")
    assert rc == 130
    assert err == "Interrupted. Nothing more was written.\n"
    assert out.splitlines()[-1] == "Calibrating... stopped."
    assert b5.opcodes()[-3:] == [0x02, 0x04, 0x52]  # the exit-path cleanup
    assert b5.events()[-1] == {"op": "close"}
    assert "interrupted" in b5.log.read_text()
    assert_nothing_installed(b5)


@pytest.mark.parametrize("sig", ["SIGHUP", "SIGTERM"])
def test_a_hangup_or_term_during_the_capture_still_stops_the_reader(b5, sig):
    # Through the script, whose handlers are installed: the signal comes at the first
    # capture read, and a second copy at the 04 write (sudo relays a hangup) is ignored.
    b5.scenario(stop=sig)
    r = b5.cli("install")
    assert r.returncode == 130, r.stdout + r.stderr
    assert r.stderr == "Interrupted. Nothing more was written.\n"
    assert b5.opcodes()[-3:] == [0x02, 0x04, 0x52]
    assert b5.events()[-1] == {"op": "close"}
    assert_nothing_installed(b5)


def test_the_stop_signals_raise_once_then_are_ignored():
    keep = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        setup.install_signal_handlers()
        assert {signal.getsignal(sig) for sig in keep} == {setup._on_stop_signal}
        with pytest.raises(KeyboardInterrupt):
            setup._on_stop_signal(signal.SIGHUP, None)
        assert {signal.getsignal(sig) for sig in keep} == {signal.SIG_IGN}
    finally:
        for sig, handler in keep.items():
            signal.signal(sig, handler)


@pytest.mark.parametrize("which", ["data", "vendor"])
def test_a_symlinked_target_dir_is_refused_before_anything(b5, which):
    elsewhere = b5.tmp / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    link = b5.data if which == "data" else b5.vendor
    link.parent.mkdir(mode=0o700)
    link.symlink_to(elsewhere)
    rc, _, err = b5.run("install")
    assert rc == 1
    assert f"FAILED: Nothing was installed: {link} is a symlink; refusing to write through it" in err
    assert list(elsewhere.iterdir()) == []
    assert b5.curl_calls() == [] and b5.events() == []
    for form in serial_forms():
        assert form not in err


@pytest.mark.parametrize("which", ["module", "program"])
def test_a_symlinked_data_file_is_refused_before_calibrating(b5, which):
    b5.data.mkdir(parents=True)
    b5.data.chmod(0o700)
    b5.state.chmod(0o700)
    elsewhere = b5.tmp / "elsewhere.bin"
    elsewhere.write_bytes(b"")
    link = b5.data / "module.bin" if which == "module" else b5.program
    link.symlink_to(elsewhere)
    rc, out, err = b5.run("install")
    assert rc == 1
    shown = "module.bin" if which == "module" else "program-<serial>.bin"
    assert f"FAILED: Nothing was installed: {b5.data / shown} is a symlink; refusing to replace or follow it" in err
    assert b5.curl_calls() == [] and b5.events() == [] and b5.systemctl_calls() == []
    assert elsewhere.read_bytes() == b""
    for form in serial_forms():
        assert form not in out + err


def test_no_reader_in_sysfs(b5):
    b5.remove_readers()
    rc, _out, err = b5.run("install")
    assert rc == 1
    assert "FAILED: The fingerprint reader 06cb:0078 was not found." in err
    assert b5.curl_calls() == [] and b5.events() == []  # checked before any download


def test_two_readers_are_refused(b5):
    b5.add_reader("0a0b0c0d0e0f", name="2-1")
    rc, _, err = b5.run("install")
    assert rc == 1
    assert "2 readers 06cb:0078 were found" in err
    assert b5.curl_calls() == []


def test_without_the_fake_transport_nothing_calibrates_under_a_test_root(b5):
    rc, _, err = b5.run("install", SYNAPTICS0078_SETUP_TEST_TRANSPORT=None)
    assert rc == 2
    assert "no fake transport under the test root" in err
    assert_nothing_installed(b5)


# ----------------------------------------------------------- the program check


def flip(data: bytes, offset: int) -> bytes:
    out = bytearray(data)
    out[offset] ^= 0x01
    return bytes(out)


def test_status_of_a_valid_install_matches_the_reader(b5):
    assert b5.run("install")[0] == 0
    rc, out, err = b5.run("status")
    assert rc == 0, out + err
    assert "  ok    06cb:0078 found (USB serial readable)" in out
    assert (
        f"  ok    {b5.program.name}: valid (serial {SYS_SERIAL}, 18869 bytes, starts with 0x02, matches the template)"
        in out
    )
    assert "  ok    this reader's program is installed and valid" in out
    assert f"  ok    {LABEL} (catalog-a): gave module and template; saved " in out
    assert re.search(
        r"^last calibration: .*, check score 0 \(limit 200\), baseline mean 127\.\d, sd 0\.\d$", out, re.MULTILINE
    )
    assert out.splitlines()[-1] == "ready: the driver finds valid data files"


def test_status_names_the_chunk_of_a_flipped_fixed_byte(b5):
    assert b5.run("install")[0] == 0
    b5.program.write_bytes(flip(PROGRAM_OK, 15))  # data byte 2 of chunk 0x20 (offsets 13..20)
    rc, out, _ = b5.run("status")
    assert rc == 1
    assert (
        f"  FAILED {b5.program.name}: invalid: it does not match the template: "
        "chunk 0x20 set register differs from the template" in out
    )
    assert "  FAILED this reader has no valid program: sudo synaptics0078-setup install" in out
    assert out.splitlines()[-1].startswith("not ready")


def test_status_without_the_reader_program(b5):
    assert b5.run("install")[0] == 0
    b5.program.rename(b5.data / "program-0a0b0c0d0e0f.bin")  # some other unit's program
    rc, out, _ = b5.run("status")
    assert rc == 1
    assert "  ok    program-0a0b0c0d0e0f.bin: valid" in out
    assert "  FAILED this reader has no valid program" in out


def test_status_without_a_reader_judges_the_files(b5):
    assert b5.run("install")[0] == 0
    b5.remove_readers()
    rc, out, _ = b5.run("status")
    assert rc == 0
    assert "  note  no 06cb:0078 reader found in sysfs" in out


@pytest.mark.parametrize("trouble", ["two readers", "no serial"])
def test_status_is_not_ready_when_the_reader_cannot_be_matched(b5, trouble):
    assert b5.run("install")[0] == 0
    if trouble == "two readers":
        b5.add_reader("0a0b0c0d0e0f", name="2-1")
        needle = "  FAILED 2 readers 06cb:0078 found; this tool sets up one (unplug the others)"
    else:
        (b5.root / "sys" / "bus" / "usb" / "devices" / "1-3" / "serial").unlink()
        needle = "  FAILED 06cb:0078 found, but its USB serial number cannot be read (restart the computer)"
    rc, out, _ = b5.run("status")
    assert rc == 1
    assert needle in out
    assert "ready: the driver finds valid data files" not in out
    assert out.splitlines()[-1].startswith("not ready: see the FAILED lines above")


def test_status_survives_deeply_nested_json(b5):
    assert b5.run("install")[0] == 0
    deep = "[" * 500_000 + "]" * 500_000  # under the 1 MiB cap; json raises RecursionError
    (b5.setup_dir / "calibration.json").write_text(deep)
    (b5.vendor / "manifest.json").write_text(deep)
    rc, out, err = b5.run("status")
    assert rc == 0, out + err
    assert "last calibration" not in out  # unreadable: treated as absent
    assert f"  ok    {LABEL} (catalog-a): gave not recorded; saved date not recorded" in out


def test_status_before_anything_is_installed(b5):
    rc, out, _ = b5.run("status")
    assert rc == 1
    assert "  FAILED missing: nothing is installed yet" in out
    assert "  install with: sudo synaptics0078-setup install" in out


def test_install_replaces_an_invalid_program_and_keeps_it(b5):
    assert b5.run("install")[0] == 0
    bad = flip(PROGRAM_OK, 15)
    b5.program.write_bytes(bad)
    b5.reset_calls()
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    assert b5.opcodes() == FULL_SEQUENCE  # calibrated again (a fresh fake reader: no module loaded)
    assert_installed(b5)
    (kept,) = b5.backup.iterdir()
    assert kept.read_bytes() == bad and mode(kept) == 0o600 and mode(b5.backup) == 0o700
    assert out.splitlines()[-2:] == [
        f"The previous program was not valid; it was kept in {b5.backup}.",
        f"Ready. Enroll a finger with: fprintd-enroll   (log: {b5.log})",
    ]


# ----------------------------------- idempotence, recalibrate, upgrade re-splice


def test_a_second_install_says_already_set_up(b5):
    assert b5.run("install")[0] == 0
    b5.reset_calls()
    before = {p: p.stat().st_mtime_ns for p in b5.data.iterdir()}
    rc, out, err = b5.run("install")
    assert rc == 0, out + err
    assert out.splitlines() == [
        "Fingerprint reader 06cb:0078 found.",
        (
            "Already set up: the module and this reader's program are installed and valid. "
            "Enroll a finger with: fprintd-enroll"
        ),
    ]
    assert b5.curl_calls() == [] and b5.events() == [] and b5.systemctl_calls() == []
    assert {p: p.stat().st_mtime_ns for p in b5.data.iterdir()} == before


def test_already_set_up_still_fixes_a_loose_mode(b5):
    assert b5.run("install")[0] == 0
    b5.program.chmod(0o644)
    rc, out, _ = b5.run("install")
    assert rc == 0 and "Already set up" in out
    assert mode(b5.program) == 0o600


def test_recalibrate_keeps_the_previous_program(b5):
    assert b5.run("install")[0] == 0
    b5.reset_calls()
    b5.serve({})  # offline: the cache is enough
    rc, out, err = b5.run("recalibrate")
    assert rc == 0, out + err
    assert b5.curl_calls() == []
    assert f"Using the vendor files saved earlier ({LABEL})... done, checked." in out
    assert f"The previous program was kept in {b5.backup}." in out
    (kept,) = b5.backup.iterdir()
    assert re.fullmatch(rf"program-{SYS_SERIAL}-\d{{8}}-\d{{6}}\.bin", kept.name)
    assert kept.read_bytes() == PROGRAM_OK and mode(kept) == 0o600
    assert b5.opcodes() == FULL_SEQUENCE
    assert_installed(b5)


def test_recalibrate_twice_in_one_second_keeps_both(b5):
    assert b5.run("install")[0] == 0
    assert b5.run("recalibrate")[0] == 0
    assert b5.run("recalibrate")[0] == 0
    assert len(list(b5.backup.iterdir())) == 2


FAKE_B = dict(fx.FAKE_CHUNKS, encoder=fx.rand("encoder of the next package", 8))
TEMPLATE_B = fx.expected_template(FAKE_B)
SP_B = fx.synthetic_pins(TEMPLATE_B, fake=FAKE_B)
DLL_B = fx.synthetic_dll(fake=FAKE_B)


def test_an_upgrade_re_splices_into_the_new_template(b5):
    assert TEMPLATE_B != TEMPLATE and SP_B.module_sha256 == SP.module_sha256
    assert b5.run("install")[0] == 0
    b5.reset_calls()
    b5.set_pins(SP_B)  # a later package with another template pin
    (b5.files / "next.dll").write_bytes(DLL_B)
    rc, out, err = b5.run("install", "--from-file", str(b5.files / "next.dll"))
    assert rc == 0, out + err
    assert out.splitlines() == [
        "Fingerprint reader 06cb:0078 found.",
        f"Reading the vendor files ({b5.files / 'next.dll'})... done, checked.",
        "Moving this reader's program to the new template... done.",
        f"Ready. Enroll a finger with: fprintd-enroll   (log: {b5.log})",
    ]
    assert b5.events() == []  # no calibration
    assert len(b5.setpriv_calls()) == 1  # a DLL given by the user: parsed unprivileged
    assert_installed(b5, calibration.assemble_cmd02(TEMPLATE_B, fx.unit_chunks()))
    (kept,) = b5.backup.iterdir()
    assert kept.read_bytes() == PROGRAM_OK
    record = json.loads((b5.setup_dir / "calibration.json").read_text())
    assert record["template_sha256"] == SP_B.template_sha256 and record["check_score"] == 0
    assert "resplice_date" in record
    assert b5.run("status")[0] == 0


def test_an_upgrade_without_the_record_calibrates_again(b5):
    assert b5.run("install")[0] == 0
    (b5.setup_dir / "calibration.json").unlink()
    b5.reset_calls()
    b5.set_pins(SP_B)
    (b5.files / "next.dll").write_bytes(DLL_B)
    rc, out, err = b5.run("install", "--from-file", str(b5.files / "next.dll"))
    assert rc == 0, out + err
    assert b5.opcodes() == FULL_SEQUENCE
    assert_installed(b5, calibration.assemble_cmd02(TEMPLATE_B, fx.unit_chunks()))


def test_an_upgrade_re_splices_with_a_previous_pin_in_the_tool(b5, monkeypatch):
    assert b5.run("install")[0] == 0
    (b5.setup_dir / "calibration.json").unlink()
    monkeypatch.setattr(setup, "PREVIOUS_TEMPLATE_SHA256", (SP.template_sha256,))
    b5.reset_calls()
    b5.set_pins(SP_B)
    (b5.files / "next.dll").write_bytes(DLL_B)
    rc, out, err = b5.run("install", "--from-file", str(b5.files / "next.dll"))
    assert rc == 0, out + err
    assert "Moving this reader's program to the new template... done." in out
    assert b5.events() == []


def test_a_corrupted_program_is_not_re_spliced(b5):
    assert b5.run("install")[0] == 0
    b5.program.write_bytes(flip(PROGRAM_OK, 15))  # matches neither the old nor the new template
    b5.reset_calls()
    b5.set_pins(SP_B)
    (b5.files / "next.dll").write_bytes(DLL_B)
    rc, out, err = b5.run("install", "--from-file", str(b5.files / "next.dll"))
    assert rc == 0, out + err
    assert "Moving" not in out and b5.opcodes() == FULL_SEQUENCE


# ----------------------------------------------------------------- --from-file


def test_install_from_a_cab_file(b5):
    (b5.files / "driver.cab").write_bytes(CAB)
    b5.serve({})
    rc, out, err = b5.run("install", "--from-file", str(b5.files / "driver.cab"))
    assert rc == 0, out + err
    assert f"Reading the vendor files ({b5.files / 'driver.cab'})... done, checked." in out.splitlines()
    assert b5.curl_calls() == [] and b5.setpriv_calls() == []  # a known archive: parsed here
    assert b5.cached() == [f"{CAB_SHA}-catalog-a", "manifest.json"]
    assert_installed(b5)


def test_install_from_an_origin_style_zip(b5):
    (b5.files / "02_Finger.zip").write_bytes(fx.origin_style_zip(DLL))
    rc, out, err = b5.run("install", "--from-file", str(b5.files / "02_Finger.zip"))
    assert rc == 0, out + err
    assert len(b5.setpriv_calls()) == 1
    assert_installed(b5)


def test_a_bare_module_alone_cannot_be_calibrated(b5):
    (b5.files / "module.bin").write_bytes(fx.MODULE)
    rc, _out, err = b5.run("install", "--from-file", str(b5.files / "module.bin"))
    assert rc == 1
    assert "FAILED: A bare module alone cannot be calibrated: the calibration also needs the vendor DLL" in err
    assert b5.setpriv_calls() == [] and b5.events() == []
    assert_nothing_installed(b5)


def test_a_bare_module_and_template(b5):
    (b5.files / "module.bin").write_bytes(fx.MODULE)
    (b5.files / "template.bin").write_bytes(TEMPLATE)
    rc, out, err = b5.run(
        "install", "--from-file", str(b5.files / "module.bin"), "--from-file", str(b5.files / "template.bin")
    )
    assert rc == 0, out + err
    assert b5.setpriv_calls() == []  # bare files are only hashed
    assert_installed(b5)


def test_a_bare_module_is_enough_when_only_the_module_is_missing(b5):
    assert b5.run("install")[0] == 0
    (b5.data / "module.bin").unlink()
    (b5.files / "module.bin").write_bytes(fx.MODULE)
    b5.reset_calls()
    rc, out, err = b5.run("install", "--from-file", str(b5.files / "module.bin"))
    assert rc == 0, out + err
    assert b5.events() == []
    assert_installed(b5)


def test_a_file_with_nothing_usable(b5):
    (b5.files / "notes.txt").write_bytes(b"hello")
    rc, out, err = b5.run("install", "--from-file", str(b5.files / "notes.txt"))
    assert rc == 1
    assert f"Reading the vendor files ({b5.files / 'notes.txt'})... nothing usable in it." in out
    assert "do not contain the vendor files this reader needs" in err
    assert_nothing_installed(b5)


def test_a_from_file_of_another_user_is_refused_unread(b5):
    (b5.files / "driver.cab").write_bytes(CAB)
    rc, _out, err = b5.run("install", "--from-file", str(b5.files / "driver.cab"), SUDO_UID=str(OTHER_UID))
    assert rc == 1
    me, other = setup.user_name(os.getuid()), setup.user_name(OTHER_UID)
    assert f"is owned by {me}, not by {other} who ran sudo; not read" in err
    assert_nothing_installed(b5)


def test_a_from_file_symlink_is_refused(b5):
    (b5.files / "driver.cab").write_bytes(CAB)
    (b5.files / "link.cab").symlink_to(b5.files / "driver.cab")
    rc, _, err = b5.run("install", "--from-file", str(b5.files / "link.cab"))
    assert rc == 1
    assert "is a symlink; give the file itself" in err


def opens_recorded(monkeypatch) -> list[tuple[str, int, int]]:
    """Every os.open of the tool: (path, flags, the fd it gave)."""
    calls, real_open = [], os.open

    def record(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        calls.append((str(path), flags, fd))
        return fd

    monkeypatch.setattr(setup.os, "open", record)
    return calls


@pytest.mark.parametrize("sudo", [False, True])
def test_a_device_node_given_as_from_file_is_never_opened(b5, monkeypatch, sudo):
    # e.g. sudo synaptics0078-setup install --from-file /dev/watchdog: opening it would act
    calls = opens_recorded(monkeypatch)
    extra = {"SUDO_UID": str(os.getuid())} if sudo else {}
    rc, _, err = b5.run("install", "--from-file", "/dev/null", **extra)
    assert rc == 1
    me = setup.user_name(os.getuid())
    why = f"is owned by root, not by {me} who ran sudo; not read" if sudo else "is not a regular file"
    assert f"FAILED: /dev/null {why}" in err
    assert [flags & os.O_PATH for path, flags, _ in calls if path == "/dev/null"] == [os.O_PATH]
    assert not any(path.startswith("/proc/self/fd/") for path, _, _ in calls)


def test_a_fifo_given_as_from_file_is_refused_without_blocking(b5):
    os.mkfifo(b5.files / "pipe.cab")
    rc, _, err = b5.run("install", "--from-file", str(b5.files / "pipe.cab"))
    assert rc == 1 and f"FAILED: {b5.files / 'pipe.cab'} is not a regular file" in err


def test_a_from_file_read_is_of_the_inode_that_was_checked(b5, monkeypatch):
    (b5.files / "driver.cab").write_bytes(CAB)
    calls = opens_recorded(monkeypatch)
    real_fstat = os.fstat

    def fstat(fd):  # the fd opened for reading reports another inode: swapped in between
        st = real_fstat(fd)
        if any(path.startswith("/proc/self/fd/") and got == fd for path, _, got in calls):
            fields = list(st)
            fields[stat.ST_INO] += 1
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(setup.os, "fstat", fstat)
    rc, _, err = b5.run("install", "--from-file", str(b5.files / "driver.cab"))
    assert rc == 1 and "driver.cab changed while it was read; run the command again" in err
    paths = [path for path, _, _ in calls]
    i = paths.index(str(b5.files / "driver.cab"))
    assert calls[i][1] & os.O_PATH and paths[i + 1] == f"/proc/self/fd/{calls[i][2]}"
    assert_nothing_installed(b5)


def test_from_and_from_file_do_not_mix(b5):
    rc, _, err = b5.run("install", "--from", str(b5.files), "--from-file", str(b5.files / "x"))
    assert rc == 2 and "cannot be combined" in err
    rc, _, err = b5.run("install", "--force")
    assert rc == 2 and "--force applies only to install --from DIR" in err


# ------------------------------------------------------------ fetch, dry run


def test_fetch_needs_no_reader(b5):
    b5.remove_readers()
    rc, out, err = b5.run("fetch")
    assert rc == 0, out + err
    assert b5.cached() == [f"{CAB_SHA}-catalog-a", "manifest.json"]
    assert b5.events() == [] and b5.systemctl_calls() == []


def test_fetch_twice_downloads_once(b5):
    assert b5.run("fetch")[0] == 0
    b5.reset_calls()
    rc, out, _ = b5.run("fetch")
    assert rc == 0
    assert b5.curl_calls() == []
    assert out.splitlines() == [
        f"Using the vendor files saved earlier ({LABEL})... done, checked.",
        "Nothing to download: the vendor files are already saved.",
    ]


def test_fetch_from_a_bare_module_saves_it_and_says_it_is_not_enough(b5):
    (b5.files / "module.bin").write_bytes(fx.MODULE)
    rc, out, err = b5.run("fetch", "--from-file", str(b5.files / "module.bin"))
    assert rc == 0, out + err
    assert "Saved. Note: A bare module alone cannot be calibrated" in out
    assert b5.cached() == [f"{hashlib.sha256(fx.MODULE).hexdigest()}-file-module", "manifest.json"]


def test_fetch_from_a_file_with_nothing_usable(b5):
    (b5.files / "notes.txt").write_bytes(b"hello")
    rc, _, err = b5.run("fetch", "--from-file", str(b5.files / "notes.txt"))
    assert rc == 1
    assert "contain neither the module nor the DLL tables, so nothing was saved" in err
    assert b5.cached() == []


def test_fetch_stops_at_a_refused_file(b5):
    (b5.files / "module.bin").write_bytes(fx.MODULE)
    rc, _, err = b5.run(
        "fetch", "--from-file", str(b5.files / "module.bin"), "--from-file", str(b5.files / "missing.cab")
    )
    assert rc == 1
    assert f"FAILED: {b5.files / 'missing.cab'} does not exist" in err


@pytest.mark.parametrize("argv", [("install",), ("recalibrate",), ("fetch",)])
def test_b5_dry_run_writes_nothing(b5, argv):
    before = tree(b5.tmp)
    rc, out, err = b5.run(*argv, "--dry-run")
    assert rc == 0, out + err
    assert tree(b5.tmp) == before
    assert b5.curl_calls() == [] and b5.setpriv_calls() == [] and b5.systemctl_calls() == [] and b5.events() == []
    assert out.splitlines()[-1].startswith("dry run: nothing was changed")
    if argv[0] != "fetch":
        assert "  would stop fprintd if it runs, and calibrate the reader (fingers off, about 10 seconds)" in out


def test_dry_run_when_already_set_up(b5):
    assert b5.run("install")[0] == 0
    before = tree(b5.tmp)
    rc, out, _ = b5.run("install", "--dry-run")
    assert rc == 0 and "Already set up" in out
    assert tree(b5.tmp) == before


def test_non_root_install_without_dry_run_is_refused(b5):
    r = b5.cli("install", env={})
    assert r.returncode == 2
    assert "install needs root; nothing was changed. Run:\n  sudo synaptics0078-setup install\n" in r.stderr
    r = b5.cli("fetch", "--from-file", "x.cab", env={})
    assert r.returncode == 2 and "--from-file " in r.stderr


# ----------------------------------------------------------------- the script


def test_full_install_through_the_script(b5):
    r = b5.cli("install")  # no TTY: the countdown (3 s)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[-1] == f"Ready. Enroll a finger with: fprintd-enroll   (log: {b5.log})"
    assert_installed(b5)
    assert "usb" not in r.stderr
    assert not list((REPO / "packaging").rglob("__pycache__"))


@pytest.mark.parametrize("var", NEW_HOOKS)
def test_the_new_test_hooks_are_refused_as_root(b5, var):
    hooks = {var: str(b5.tmp / "x"), "SYNAPTICS0078_SETUP_TEST_EUID": "0"}
    for argv in (("install",), ("recalibrate",), ("fetch",), ("status",), ("install", "--dry-run")):
        r = b5.cli(*argv, env=hooks)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "is set; the test-only variables are refused as root" in r.stderr
        assert r.stdout == ""
    assert not b5.data.exists() and not b5.setup_dir.exists() and not b5.log.exists()
    # Real root, this variable alone: refused by name.
    with pytest.raises(setup.Fail, match=f"^{var} is set; the test-only variables are refused as root$") as exc:
        setup.load_config({var: str(b5.tmp / "x")}, 0)
    assert exc.value.code == 2 and f"sudo env -u {var} " in exc.value.hint


def test_a_relative_hook_path_is_refused(b5):
    rc, _, err = b5.run("status", SYNAPTICS0078_SETUP_TEST_CURL="bin/curl")
    assert rc == 2 and "SYNAPTICS0078_SETUP_TEST_CURL must be an absolute path" in err


def test_pins_and_module_hooks_must_agree(b5):
    rc, _, err = b5.run("status", SYNAPTICS0078_SETUP_TEST_MODULE_SHA256="ab" * 32)
    assert rc == 2 and "differ" in err


# ------------------------------------------------------- loading the package


def test_tree_problem_accepts_a_clean_tree(tmp_path):
    pkg = tmp_path / "lib" / "synaptics0078"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__pycache__" / "x.pyc").write_bytes(b"")
    for p in (tmp_path / "lib", pkg, pkg / "__pycache__"):
        p.chmod(0o755)
    assert setup.tree_problem(str(pkg), {os.getuid()}, top=str(tmp_path)) is None


@pytest.mark.parametrize("what", ["group-writable dir", "world-writable file", "symlink", "foreign owner", "no init"])
def test_tree_problem_refuses(tmp_path, what):
    lib = tmp_path / "lib"
    pkg = lib / "synaptics0078"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "pins.py").write_text("")
    for p in (lib, pkg):
        p.chmod(0o755)
    owners = {os.getuid()}
    if what == "group-writable dir":
        lib.chmod(0o775)
        needle = f"{lib} is writable by its group or by others"
    elif what == "world-writable file":
        (pkg / "pins.py").chmod(0o646)
        needle = f"{pkg / 'pins.py'} is writable by its group or by others"
    elif what == "symlink":
        (pkg / "vendor.py").symlink_to(pkg / "pins.py")
        needle = f"{pkg / 'vendor.py'} is a symlink"
    elif what == "foreign owner":
        owners = {os.getuid() + 4242}
        needle = "is owned by"
    else:
        (pkg / "__init__.py").unlink()
        needle = "__init__.py does not exist"
    why = setup.tree_problem(str(pkg), owners, top=str(tmp_path))
    assert why is not None and needle in why


def clean_package(base: Path) -> Path:
    """base/lib/synaptics0078 with an __init__.py and a module, all 0755/0644."""
    lib = base / "lib"
    pkg = lib / "synaptics0078"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "pins.py").write_text("")
    for p in (lib, pkg):
        p.chmod(0o755)
    return pkg


def as_root_owned(monkeypatch, *paths: Path) -> None:
    """Make lstat report root as the owner of `paths` (a test cannot chown)."""
    real_lstat, fake = os.lstat, {str(p) for p in paths}

    def lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if str(path) in fake:
            fields = list(st)
            fields[stat.ST_UID] = 0
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(setup.os, "lstat", lstat)


def test_tree_problem_accepts_a_chain_through_tmp(tmp_path):
    # makepkg's check() runs under BUILDDIR=/tmp/makepkg: /tmp is root's, 1777.
    tmp = os.lstat("/tmp")
    if not (tmp.st_uid == 0 and stat.S_IMODE(tmp.st_mode) == 0o1777 and str(tmp_path).startswith("/tmp/")):
        pytest.skip("needs the test's tmp_path under a root-owned 1777 /tmp")
    pkg = clean_package(tmp_path)
    assert setup.tree_problem(str(pkg), {0, os.getuid()}) is None


def test_tree_problem_accepts_a_root_owned_sticky_dir_above_the_package(tmp_path, monkeypatch):
    pkg = clean_package(tmp_path)
    (tmp_path / "lib").chmod(0o1777)
    as_root_owned(monkeypatch, tmp_path / "lib")
    assert setup.tree_problem(str(pkg), {0, os.getuid()}, top=str(tmp_path)) is None


@pytest.mark.parametrize("what", ["world-writable, not sticky", "sticky, not root's", "the package dir", "inside"])
def test_tree_problem_refuses_a_world_writable_dir(tmp_path, monkeypatch, what):
    pkg = clean_package(tmp_path)
    lib = tmp_path / "lib"
    if what == "world-writable, not sticky":
        target = lib
        lib.chmod(0o777)
        as_root_owned(monkeypatch, lib)
    elif what == "sticky, not root's":
        target = lib
        lib.chmod(0o1777)
    elif what == "the package dir":  # root's and sticky, but the package itself: never
        target = pkg
        pkg.chmod(0o1777)
        as_root_owned(monkeypatch, pkg)
    else:
        target = pkg / "__pycache__"
        target.mkdir()
        target.chmod(0o1777)
        as_root_owned(monkeypatch, target)
    why = setup.tree_problem(str(pkg), {0, os.getuid()}, top=str(tmp_path))
    assert why is not None and f"{target} is writable by its group or by others" in why


def test_an_untrusted_libdir_is_refused_before_anything(b5, tmp_path):
    lib = tmp_path / "lib"
    (lib / "synaptics0078").mkdir(parents=True)
    (lib / "synaptics0078" / "__init__.py").write_text("raise SystemExit('executed')\n")
    lib.chmod(0o777)
    rc, _, err = b5.run("status", SYNAPTICS0078_SETUP_TEST_LIBDIR=str(lib))
    assert rc == 2
    assert "package in" in err and "cannot be trusted" in err
    assert f"{lib} is writable by its group or by others (mode 0777)" in err


def test_the_package_is_not_put_on_sys_path(b5):
    before = list(sys.path)
    assert b5.run("status")[0] == 1
    assert sys.path == before
    assert sys.modules["synaptics0078"].__file__ == str(REPO / "synaptics0078" / "__init__.py")


def test_the_docstring_shows_the_child_limits_in_force():
    line = "prlimit " + " ".join(f"--{name}={value}" for name, value in setup.CHILD_RLIMITS) + " --"
    assert line in setup.__doc__
    assert dict(setup.CHILD_RLIMITS) == {"as": 1 << 30, "cpu": 120, "nproc": 64, "fsize": 0}


def test_child_code_loads_the_package_by_location():
    code = setup.child_code("/usr/lib/synaptics0078/synaptics0078")
    assert "spec_from_file_location('synaptics0078', '/usr/lib/synaptics0078/synaptics0078/__init__.py'" in code
    assert "sys.path" not in code and code.endswith("sys.exit(vendor.child_entry())")


# ------------------------------------------------------------------- helpers


def test_serial_hex_is_the_driver_rule():
    assert setup.serial_hex(fx.SERIAL) == sd.serial_hex(fx.SERIAL)


def test_redactor_hides_both_forms_in_any_case():
    r = setup.Redactor()
    r.add_serial(SYS_SERIAL)
    text = f"program-{SYS_SERIAL}.bin {SYS_SERIAL.upper()} raw {fx.SERIAL.hex()}"
    assert r(text) == "program-<serial>.bin <serial> raw <serial>"
