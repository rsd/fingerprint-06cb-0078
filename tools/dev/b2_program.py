"""Step B2b: swap the installed capture program between the Windows one and RUN_DIR/program-linux.bin.

Run it through tools/dev/b2-program.sh (its header is the user documentation), which checks the
arguments, refuses the test variables as root, asks for sudo where root is needed and starts this file
as `/usr/bin/python3 -I -B b2_program.py --self CMD -- COMMAND [RUN_DIR]`. It runs as root, so: the
standard library only, nothing from the environment on the import path, no bytecode written into the
project tree, and its two helpers loaded by explicit path:

  proto/calibration.py                                  program_problem(program): the template check
  packaging/arch/libfprint-sigfm-0078/synaptics0078-setup  program_problem(serial, data): name, size,
                                                          opcode, as the packaged setup tool checks them

The driver (libfprint/drivers/synaptics0078.c, load_data_files) reads exactly
/var/lib/fprint/synaptics0078/program-<serial>.bin at every open of the reader and frees it at close;
fprintd opens on a client's claim and closes on its release. So an installed file is used from the next
claim on, and stopping fprintd (D-Bus starts it again on demand) ends any claim that holds the old one.

Every file is opened without following symlinks and must be a regular file. Writes go through a temp
file in the data dir (O_EXCL, 0600, then the original's owner and mode, fsync), then rename (install)
or link (the backup: never overwrites one), then an fsync of the directory.

The terminal never shows the unit serial: the files show as program-<serial>.bin and
program-<serial>.bin.b2-windows-backup. The log (local/logs/b2-program-<stamp>.log, 0600, given to
the sudo user) has the real paths, the sha256s and the systemctl output.

Exit codes: 0 done or nothing to do; 1 refused or failed; 2 misuse or root needed; 130 interrupted
(Ctrl-C or SIGTERM).

The test variables (TEST_VARS; b2-program.sh's header) are refused as root, here and in the wrapper.
B2_SYSTEMCTL and B2_TEST_TEMPLATE count only with B2_TEST_ROOT. B2_TEST_TEMPLATE names a template file
(18869 bytes, read like the programs) that the template check compares with instead of the pinned
proto/data/cmd02_template.bin: the offline tests use a synthetic one. Unset, the pinned one is read.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import traceback
import types
from dataclasses import dataclass
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
WRAPPER = os.path.join(HERE, "b2-program.sh")
CALIBRATION_PATH = os.path.join(ROOT, "proto", "calibration.py")
SETUP_PATH = os.path.join(ROOT, "packaging", "arch", "libfprint-sigfm-0078", "synaptics0078-setup")

SYS_DATA_DIR = "/var/lib/fprint/synaptics0078"
SYS_DROPIN_DIR = "/etc/systemd/system/fprintd.service.d"
SYSTEMCTL = "/usr/bin/systemctl"  # by path: as root, PATH is not trusted
# Either one sends the driver's journal lines that tools/dev/a5-verify.sh needs: the packaged install's
# debug drop-in (a5-fprintd-dev.sh log on) or the dev install's drop-in.
DEBUG_DROPIN = "synaptics0078-debug.conf"
DEV_DROPIN = "libfprint-0078-dev.conf"
PROGRAM_RE = re.compile(r"program-([0-9a-f]{12})\.bin")  # always fullmatch
BACKUP_SUFFIX = ".b2-windows-backup"
SHOWN = "program-<serial>.bin"
SHOWN_BACKUP = SHOWN + BACKUP_SUFFIX
LINUX_NAME = "program-linux.bin"
READ_CAP = 1 << 20  # far above the 18869 bytes of a program: a bigger file is not one
TEST_VARS = ("B2_TEST_ROOT", "B2_SYSTEMCTL", "B2_TEST_TEMPLATE", "B2_FAKE_EUID")

_RDONLY = os.O_RDONLY | os.O_CLOEXEC | os.O_NOCTTY
_DIR_FLAGS = _RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Fail(Exception):
    """A refusal or error for the user: the message (no serial in it), an optional hint, the exit code."""

    def __init__(self, message: str, hint: str = "", code: int = 1):
        super().__init__(message)
        self.hint = hint
        self.code = code


def load_module(name: str, path: str):
    """The module at `path` (any file name), compiled from its source text.

    No import loader: a loader reads __pycache__/*.pyc next to the source (user-owned; this runs as
    root) and runs it instead when its header matches. Nothing is read from or written to a cache.
    """
    try:
        with open(path, "rb") as f:
            source = f.read()
    except FileNotFoundError:
        raise Fail(f"{os.path.relpath(path, ROOT)} is missing in the project") from None
    code = compile(source, path, "exec", dont_inherit=True)  # not this file's __future__ flags
    module = types.ModuleType(name)
    module.__file__ = path
    sys.modules[name] = module  # dataclasses look their module up here
    try:
        exec(code, module.__dict__)  # noqa: S102 - the project's own helper, by explicit path
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


# ----------------------------------------------------------------- configuration


@dataclass(frozen=True)
class Config:
    test_root: str  # "" on a real system
    euid: int  # what the root checks see (B2_FAKE_EUID when not root)
    real_euid: int
    data_dir: str
    dropin_dir: str
    local_dir: str  # the project's local/ ($B2_TEST_ROOT/local under a test root)
    systemctl: tuple[str, ...]
    self_cmd: str  # the wrapper as the user can run it
    test_template: str = ""  # B2_TEST_TEMPLATE (with B2_TEST_ROOT); "": the pinned proto/data template


def load_config(environ, real_euid: int, self_cmd: str) -> Config:
    euid = real_euid
    if real_euid != 0 and environ.get("B2_FAKE_EUID"):
        try:
            euid = int(environ["B2_FAKE_EUID"])
        except ValueError:
            raise Fail("B2_FAKE_EUID is not a number", code=2) from None
    if euid == 0:
        for var in TEST_VARS:
            if var in environ:
                raise Fail(
                    f"{var} is set; the test-only variables are refused as root",
                    hint=f"unset it (sudo -E and env_keep pass it on): sudo env -u {var} ...",
                    code=2,
                )
    tr = environ.get("B2_TEST_ROOT", "")
    systemctl: tuple[str, ...] = (SYSTEMCTL,)
    local_dir = os.path.join(ROOT, "local")
    test_template = ""
    if tr:
        if not os.path.isabs(tr):
            raise Fail("B2_TEST_ROOT must be an absolute path", code=2)
        tr = os.path.realpath(tr)
        if environ.get("B2_SYSTEMCTL"):
            systemctl = (environ["B2_SYSTEMCTL"],)
        test_template = environ.get("B2_TEST_TEMPLATE", "")
        if test_template and not os.path.isabs(test_template):
            raise Fail("B2_TEST_TEMPLATE must be an absolute path", code=2)
        local_dir = os.path.join(tr, "local")
    return Config(
        test_root=tr,
        euid=euid,
        real_euid=real_euid,
        data_dir=tr + SYS_DATA_DIR,
        dropin_dir=tr + SYS_DROPIN_DIR,
        local_dir=local_dir,
        systemctl=systemctl,
        self_cmd=self_cmd,
        test_template=test_template,
    )


# --------------------------------------------------------------------------- output


class Log:
    """local/logs/b2-program-<stamp>.log (0600; chowned to the sudo user), plus the terminal lines."""

    def __init__(self, cfg: Config, environ):
        self.path = ""
        self._fd = -1
        logs = os.path.join(cfg.local_dir, "logs")
        owner = sudo_owner(cfg, environ)
        # The project -> local -> logs by directory fds, never following a symlink: a local/ or logs/
        # swapped meanwhile cannot send root's writes elsewhere.
        base = os.path.realpath(os.path.dirname(cfg.local_dir))
        try:
            dir_fd = os.open(base, _DIR_FLAGS)
        except OSError as exc:
            raise Fail(f"cannot open {shown_path(base)}: {exc.strerror}") from None
        try:
            for path in (cfg.local_dir, logs):
                parent_fd, dir_fd = dir_fd, -1
                try:
                    dir_fd = sub_dir(parent_fd, os.path.basename(path), shown_path(path), owner)
                finally:
                    os.close(parent_fd)
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_APPEND
            for n in range(100):
                name = f"b2-program-{stamp}{'' if n == 0 else f'-{n}'}.log"
                try:
                    self._fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
                except FileExistsError:
                    continue
                self.path = os.path.join(logs, name)
                break
            else:
                raise Fail(f"cannot create a new log in {shown_path(logs)}", hint="wait a second and run again")
        finally:
            if dir_fd >= 0:
                os.close(dir_fd)
        os.fchmod(self._fd, 0o600)
        if owner:
            os.fchown(self._fd, *owner)

    def detail(self, text: str) -> None:
        if self._fd >= 0:
            os.write(self._fd, (text.rstrip("\n") + "\n").encode("utf-8", "replace"))

    def out(self, text: str = "") -> None:
        print(text, flush=True)
        self.detail(text)

    def failed(self, exc: Fail) -> None:
        sys.stdout.flush()
        print(f"FAILED: {exc}", file=sys.stderr)
        self.detail(f"FAILED: {exc}")
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr)
            self.detail(f"  hint: {exc.hint}")

    def close(self) -> None:
        if self._fd >= 0:
            os.fsync(self._fd)
            os.close(self._fd)
            self._fd = -1


class NoLog(Log):
    """Before the log exists (bad configuration): the terminal only."""

    def __init__(self):
        self.path = ""
        self._fd = -1


def sub_dir(parent_fd: int, name: str, shown: str, owner: tuple[int, int] | None) -> int:
    """An fd of the directory `name` in parent_fd, made (0700, given to `owner`) when missing; never a symlink."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        made = True
    except FileExistsError:
        made = False
    except OSError as exc:
        raise Fail(f"cannot create {shown}: {exc.strerror}") from None
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Fail(
                f"{shown} is a symlink or not a directory", hint="logs are written only to real directories"
            ) from None
        raise Fail(f"cannot open {shown}: {exc.strerror}") from None
    if made and owner:
        try:
            os.fchown(fd, *owner)
        except BaseException:
            os.close(fd)
            raise
    return fd


def sudo_owner(cfg: Config, environ) -> tuple[int, int] | None:
    """(uid, gid) of the user who ran sudo, when running as real root through sudo; else None."""
    if cfg.real_euid != 0:
        return None
    uid, gid = environ.get("SUDO_UID", ""), environ.get("SUDO_GID", "")
    if uid.isdigit() and gid.isdigit() and int(uid) != 0:
        return int(uid), int(gid)
    return None


def shown_path(path: str) -> str:
    """`path` as the user can type it from here: relative below the current directory, else absolute."""
    path = os.path.abspath(path)
    try:
        cwd = os.getcwd()
    except OSError:
        return path
    if path == cwd or not path.startswith(cwd.rstrip("/") + "/"):
        return path
    rel = os.path.relpath(path, cwd)
    return rel if "/" in rel else "./" + rel


def q(text: str) -> str:
    return shlex.quote(text)


# ---------------------------------------------------------------------- reading


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def open_data_dir(cfg: Config, command: str, args: list[str]) -> int:
    try:
        return os.open(cfg.data_dir, _DIR_FLAGS)
    except PermissionError:
        line = " ".join(["sudo", cfg.self_cmd, command, *(q(a) for a in args)])
        raise Fail(
            f"{command} needs root here: /var/lib/fprint is root-only (mode 0700), so {SYS_DATA_DIR} "
            f"cannot be read; nothing was changed",
            hint=f"run: {line}",
            code=2,
        ) from None
    except FileNotFoundError:
        raise Fail(
            f"no {SYS_DATA_DIR}: the driver's data files are not installed",
            hint="install them first (README, A6): sudo synaptics0078-setup install DIR",
        ) from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Fail(f"{SYS_DATA_DIR} is not a directory (or is a symlink); refusing to use it") from None
        raise Fail(f"cannot open {SYS_DATA_DIR}: {exc.strerror}") from None


def read_at(dir_fd: int, name: str, shown: str) -> tuple[bytes, os.stat_result] | None:
    """The bytes and fstat of `name` in dir_fd (a regular file, not a symlink), or None when absent."""
    try:
        fd = os.open(name, _RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Fail(f"{shown} is a symlink; refusing to follow it") from None
        raise Fail(f"{shown} cannot be opened: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Fail(f"{shown} is not a regular file")
        if st.st_size > READ_CAP:
            raise Fail(f"{shown} is {st.st_size} bytes: not a program")
        chunks, left = [], READ_CAP + 1
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks), st


def find_program(dir_fd: int) -> tuple[str, str]:
    """(file name, serial) of the one program-<12 hex>.bin in the data dir."""
    names = sorted(n for n in os.listdir(dir_fd) if PROGRAM_RE.fullmatch(n))
    if not names:
        raise Fail(
            f"no {SHOWN} in {SYS_DATA_DIR}: the driver has no program to use",
            hint="install the data files first (README, A6): sudo synaptics0078-setup install DIR",
        )
    if len(names) > 1:
        raise Fail(
            f"{len(names)} {SHOWN} files in {SYS_DATA_DIR}; this tool works only with exactly one (the plugged unit's)",
            hint=f"see them with: sudo ls -l {SYS_DATA_DIR}; remove the ones of other units",
        )
    return names[0], PROGRAM_RE.fullmatch(names[0]).group(1)


@dataclass
class Linux:
    """A program-linux.bin: its run dir as shown and as typed, its bytes."""

    label: str  # local/calib/<stamp>, or the path as given when elsewhere
    typed: str  # the run dir as the user can type it from here
    data: bytes
    path: str


def run_label(cfg: Config, run_dir: str) -> str:
    base = os.path.dirname(cfg.local_dir)
    real = os.path.realpath(run_dir)
    if real.startswith(os.path.realpath(cfg.local_dir) + "/"):
        return os.path.relpath(real, os.path.realpath(base))
    return run_dir.rstrip("/") or run_dir


def read_linux(cfg: Config, run_dir: str, required: bool) -> Linux | None:
    label = run_label(cfg, run_dir)
    shown = f"{label}/{LINUX_NAME}"
    try:
        dir_fd = os.open(run_dir, _RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        if not required:
            return None
        raise Fail(f"no directory {label}", hint="give a local/calib/<stamp> run dir") from None
    except OSError as exc:
        if not required:
            return None
        raise Fail(f"cannot open {label}: {exc.strerror}") from None
    try:
        got = read_at(dir_fd, LINUX_NAME, shown)
    finally:
        os.close(dir_fd)
    if got is None:
        if not required:
            return None
        raise Fail(
            f"no {LINUX_NAME} in {label}",
            hint=f"build it (as yourself): {build_line(run_dir)}",
        )
    return Linux(label, shown_path(run_dir), got[0], os.path.join(os.path.abspath(run_dir), LINUX_NAME))


def build_line(run_dir: str) -> str:
    """The proto/calib_program.py command for run_dir, runnable from the current directory."""
    python = shown_path(os.path.join(ROOT, "proto", ".venv", "bin", "python"))
    script = shown_path(os.path.join(ROOT, "proto", "calib_program.py"))
    return f"{q(python)} {q(script)} {q(shown_path(run_dir))}"


def calib_runs(cfg: Config, log: Log) -> list[Linux]:
    """Every readable local/calib/*/program-linux.bin, oldest stamp first."""
    calib = os.path.join(cfg.local_dir, "calib")
    try:
        names = sorted(os.listdir(calib))
    except OSError as exc:
        log.detail(f"calib runs: cannot list {calib}: {exc.strerror}")
        return []
    runs = []
    for name in names:
        path = os.path.join(calib, name)
        if not os.path.isdir(path):
            continue
        try:
            run = read_linux(cfg, path, required=False)
        except Fail as exc:
            log.detail(f"calib run {path}: skipped: {exc}")
            continue
        if run:
            log.detail(f"calib run {run.path}: sha256 {sha(run.data)}")
            runs.append(run)
    return runs


class Checker:
    """Both program checks: the setup tool's (name, size, opcode) and calibration's (the template)."""

    def __init__(self, cfg: Config):
        self.setup = load_module("synaptics0078_setup", SETUP_PATH)
        self.calibration = load_module("calibration", CALIBRATION_PATH)
        # None: program_problem reads the pinned template itself, as on a real system.
        self.template = read_test_template(cfg.test_template, self.calibration.PAYLOAD_LEN)

    def problem(self, serial: str, data: bytes) -> str | None:
        why = self.setup.program_problem(serial, data)
        if why:
            return f"it {why}"
        if self.template is None:
            return self.calibration.program_problem(data)
        return self.calibration.program_problem(data, self.template)


def read_test_template(path: str, size: int) -> bytes | None:
    """The bytes of the B2_TEST_TEMPLATE file (a regular file, not a symlink, `size` bytes); None when unset."""
    if not path:
        return None
    shown = f"B2_TEST_TEMPLATE {shown_path(path)}"
    try:
        dir_fd = os.open(os.path.dirname(path), _RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise Fail(f"{shown} cannot be opened: {exc.strerror}", code=2) from None
    try:
        got = read_at(dir_fd, os.path.basename(path), shown)
    except Fail as exc:
        raise Fail(str(exc), code=2) from None
    finally:
        os.close(dir_fd)
    if got is None:
        raise Fail(f"{shown} does not exist", code=2)
    if len(got[0]) != size:
        raise Fail(f"{shown} is {len(got[0])} bytes, need {size}: not a template", code=2)
    return got[0]


# ---------------------------------------------------------------------- writing


def atomic_write(dir_fd: int, name: str, data: bytes, like: os.stat_result, shown: str, replace: bool) -> None:
    """Write `name` in dir_fd with the owner and mode of `like`, atomically; never replace it unless `replace`."""
    tmp = f".b2-program-{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        raise Fail(f"cannot create a temp file in {SYS_DATA_DIR}: {exc.strerror}; nothing was changed") from None
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            st = os.fstat(fd)
            if (st.st_uid, st.st_gid) != (like.st_uid, like.st_gid):
                os.fchown(fd, like.st_uid, like.st_gid)
            os.fchmod(fd, stat.S_IMODE(like.st_mode))
            os.fsync(fd)
        finally:
            os.close(fd)
        if replace:
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.unlink(tmp, dir_fd=dir_fd)
    except BaseException as exc:  # an interrupt too: never leave the temp file behind
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        if not isinstance(exc, OSError):
            raise
        if exc.errno == errno.EEXIST:
            raise Fail(f"{shown} appeared meanwhile; it was left as it is. Run the command again") from None
        raise Fail(f"cannot write {shown}: {exc.strerror}; it was left as it was") from None
    os.fsync(dir_fd)


# ---------------------------------------------------------------------- fprintd


def run_systemctl(cfg: Config, log: Log, *args: str) -> tuple[int, str] | None:
    cmd = [*cfg.systemctl, *args]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=60, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.detail(f"$ {shlex.join(cmd)}: {exc!r}")
        return None
    log.detail(f"$ {shlex.join(cmd)} -> rc {r.returncode}; stdout {r.stdout.strip()!r}; stderr {r.stderr.strip()!r}")
    return r.returncode, r.stdout.strip()


# What `systemctl is-active` prints for a unit that runs, or is about to: stop it in every one of them.
RUNNING_STATES = ("active", "activating", "reloading", "deactivating")


def fprintd_active(cfg: Config, log: Log) -> bool | None:
    got = run_systemctl(cfg, log, "is-active", "fprintd")
    return None if got is None else got[1] in RUNNING_STATES


def stop_fprintd(cfg: Config, log: Log) -> None:
    """After a swap: stop a running fprintd, so no client keeps the old program."""
    active = fprintd_active(cfg, log)
    if active is None:
        log.out("fprintd: not checked (systemctl failed, see the log); if a client holds the reader:")
        log.out("  sudo systemctl stop fprintd")
        return
    if not active:
        log.out("fprintd is not running; it reads the new program when it next opens the reader")
        return
    got = run_systemctl(cfg, log, "stop", "fprintd")
    if got is None or got[0] != 0:
        raise Fail(
            "the program is in place, but stopping fprintd failed: a client holding the reader keeps the old one",
            hint="run: sudo systemctl stop fprintd (details in the log)",
        )
    log.out("stopped fprintd (a client holding the reader keeps the old program); D-Bus starts it again on demand")


def already_note(cfg: Config, log: Log) -> None:
    if fprintd_active(cfg, log):
        log.out("fprintd is running: a client holding the reader since before the swap keeps the old program;")
        log.out("  to end it: sudo systemctl stop fprintd")


# ---------------------------------------------------------------- next steps


def has_dropin(cfg: Config, name: str) -> bool:
    return os.path.lexists(os.path.join(cfg.dropin_dir, name))


def log_on_lines(cfg: Config) -> list[str]:
    if has_dropin(cfg, DEBUG_DROPIN) or has_dropin(cfg, DEV_DROPIN):
        return []
    dev = shown_path(os.path.join(HERE, "a5-fprintd-dev.sh"))
    return [
        f"first, the driver's journal lines the verify runs need: sudo {q(dev)} log on",
        f"  (when testing is done: sudo {q(dev)} log off; the lines hold the unit serial)",
    ]


def log_off_lines(cfg: Config) -> list[str]:
    if not has_dropin(cfg, DEBUG_DROPIN):
        return []
    dev = shown_path(os.path.join(HERE, "a5-fprintd-dev.sh"))
    return [f"when testing is done: sudo {q(dev)} log off (the journal lines hold the unit serial)"]


def verify_lines(cfg: Config) -> list[str]:
    verify = q(shown_path(os.path.join(HERE, "a5-verify.sh")))
    return [
        f"  {verify} --finger-used right-index --expect match --finger right-index-finger",
        f"  {verify} --finger-used left-index --expect match --finger left-index-finger",
    ]


def a5_logs() -> str:
    return shown_path(os.path.join(ROOT, "local", "logs")) + "/a5-verify-<stamp>.log"


def use_linux_line(cfg: Config, run_dir: str) -> str:
    return f"sudo {cfg.self_cmd} use-linux {run_dir}"


def step_1_2(cfg: Config, run_dir: str) -> list[str]:
    return [
        *log_on_lines(cfg),
        "next, B2b step 1: the baseline on the Windows program (as yourself, no sudo; 10 rounds each):",
        *verify_lines(cfg),
        f"  each run's rounds and SIGFM scores: {a5_logs()}",
        f"then step 2: {use_linux_line(cfg, run_dir)}",
    ]


def step_3_4(cfg: Config) -> list[str]:
    return [
        *log_on_lines(cfg),
        "next, B2b step 3: the same two runs on the Linux program (as yourself, no sudo; 10 rounds each):",
        *verify_lines(cfg),
        f"  compare their {a5_logs()} (rounds, SIGFM scores) with step 1's",
        f"then step 4: keep the Linux program, or go back: sudo {cfg.self_cmd} use-windows",
        *log_off_lines(cfg),
    ]


def step_done(cfg: Config, run_dir: str) -> list[str]:
    return [
        f"B2b: the Windows program is in place. To try the Linux program again: {use_linux_line(cfg, run_dir)}",
        *log_off_lines(cfg),
    ]


def hint_run_dir(cfg: Config, checker: Checker, serial: str, runs: list[Linux], given: Linux | None) -> str:
    """The run dir for a use-linux hint: the given one, else the newest valid local/calib run."""
    if given:
        return q(given.typed)
    for run in reversed(runs):
        if checker.problem(serial, run.data) is None:
            return q(run.typed)
    return "local/calib/<stamp>"


# ------------------------------------------------------------------- commands


def read_backup(dir_fd: int, name: str) -> tuple[bytes, os.stat_result] | None:
    return read_at(dir_fd, name + BACKUP_SUFFIX, SHOWN_BACKUP)


def cmd_use_linux(cfg: Config, log: Log, run_dir: str) -> int:
    checker = Checker(cfg)
    linux = read_linux(cfg, run_dir, required=True)
    log.detail(f"linux program {linux.path}: sha256 {sha(linux.data)}")
    dir_fd = open_data_dir(cfg, "use-linux", [run_dir])
    try:
        name, serial = find_program(dir_fd)
        if why := checker.problem(serial, linux.data):
            raise Fail(
                f"{linux.label}/{LINUX_NAME} is not a usable program: {why}; nothing was changed",
                hint="proto/calib_program.py writes it only when valid: it was changed since, or is not from there",
            )
        got = read_at(dir_fd, name, SHOWN)
        if got is None:
            raise Fail(f"{SHOWN} disappeared meanwhile; nothing was changed", hint="run the command again")
        active, active_st = got
        log.detail(f"installed {os.path.join(cfg.data_dir, name)}: sha256 {sha(active)}, {stat_text(active_st)}")
        if why := checker.problem(serial, active):
            raise Fail(f"the installed {SHOWN} is not a usable program: {why}; nothing was changed")
        backup = read_backup(dir_fd, name)
        if backup:
            log.detail(f"backup: sha256 {sha(backup[0])}, {stat_text(backup[1])}")
        if active == linux.data:
            log.out(f"{SHOWN} is already the Linux program from {linux.label}; nothing was changed")
            if not backup:
                log.out(f"note: there is no {SHOWN_BACKUP}, so use-windows has nothing to restore")
            already_note(cfg, log)
            for line in step_3_4(cfg):
                log.out(line)
            return 0
        # Known: the backup, or still in a local/calib run (use-linux of run A, then of run B).
        if backup and active != backup[0] and not any(r.data == active for r in calib_runs(cfg, log)):
            raise Fail(
                f"the installed {SHOWN} is neither the Windows backup nor {linux.label}/{LINUX_NAME} nor any "
                f"local/calib/*/{LINUX_NAME}: installing would lose it; nothing was changed",
                hint=f"see: sudo {cfg.self_cmd} status; put the Windows program back first: "
                f"sudo {cfg.self_cmd} use-windows",
            )
        if not backup:
            atomic_write(dir_fd, name + BACKUP_SUFFIX, active, active_st, SHOWN_BACKUP, replace=False)
            log.out(f"backed up the Windows program: {SHOWN} -> {SHOWN_BACKUP}")
        atomic_write(dir_fd, name, linux.data, active_st, SHOWN, replace=True)
        log.out(f"installed the Linux program from {linux.label} as {SHOWN}")
    finally:
        os.close(dir_fd)
    stop_fprintd(cfg, log)
    for line in step_3_4(cfg):
        log.out(line)
    return 0


def cmd_use_windows(cfg: Config, log: Log) -> int:
    checker = Checker(cfg)
    dir_fd = open_data_dir(cfg, "use-windows", [])
    try:
        name, serial = find_program(dir_fd)
        backup = read_backup(dir_fd, name)
        if not backup:
            raise Fail(
                f"no {SHOWN_BACKUP}: the Windows program was never swapped out by use-linux; nothing was changed",
                hint=f"see: sudo {cfg.self_cmd} status",
            )
        log.detail(f"backup {os.path.join(cfg.data_dir, name + BACKUP_SUFFIX)}: sha256 {sha(backup[0])}")
        if why := checker.problem(serial, backup[0]):
            raise Fail(f"{SHOWN_BACKUP} is not a usable program: {why}; nothing was changed")
        got = read_at(dir_fd, name, SHOWN)
        if got is None:
            raise Fail(f"{SHOWN} disappeared meanwhile; nothing was changed", hint="run the command again")
        active, active_st = got
        log.detail(f"installed {os.path.join(cfg.data_dir, name)}: sha256 {sha(active)}, {stat_text(active_st)}")
        runs = calib_runs(cfg, log)
        run_dir = hint_run_dir(cfg, checker, serial, runs, None)
        if active == backup[0]:
            log.out(f"{SHOWN} is already the Windows program (the backup); nothing was changed")
            already_note(cfg, log)
            for line in step_done(cfg, run_dir):
                log.out(line)
            return 0
        atomic_write(dir_fd, name, backup[0], active_st, SHOWN, replace=True)
        log.detail(f"replaced the program with sha256 {sha(active)}")
        log.out(f"restored the Windows program: {SHOWN_BACKUP} -> {SHOWN} (the backup is kept)")
    finally:
        os.close(dir_fd)
    stop_fprintd(cfg, log)
    for line in step_done(cfg, run_dir):
        log.out(line)
    return 0


def cmd_status(cfg: Config, log: Log, run_dir: str | None) -> int:
    checker = Checker(cfg)
    given = read_linux(cfg, run_dir, required=True) if run_dir else None
    dir_fd = open_data_dir(cfg, "status", [run_dir] if run_dir else [])
    try:
        name, serial = find_program(dir_fd)
        got = read_at(dir_fd, name, SHOWN)
        if got is None:
            raise Fail(f"{SHOWN} disappeared meanwhile", hint="run the command again")
        active, active_st = got
        backup = read_backup(dir_fd, name)
    finally:
        os.close(dir_fd)
    log.detail(f"installed {os.path.join(cfg.data_dir, name)}: sha256 {sha(active)}, {stat_text(active_st)}")
    if backup:
        log.detail(f"backup: sha256 {sha(backup[0])}, {stat_text(backup[1])}")
    if given:
        log.detail(f"linux program {given.path}: sha256 {sha(given.data)}")
    runs = [*calib_runs(cfg, log), *([given] if given else [])]  # the given run first when both match
    hint_dir = hint_run_dir(cfg, checker, serial, runs, given)
    linux = next((r for r in reversed(runs) if r.data == active), None)
    why = checker.problem(serial, active)
    bad = f" (not a usable program: {why})" if why else ""
    if backup and active == backup[0]:
        state, steps = "Windows", step_done(cfg, hint_dir)
    elif linux:
        state, steps = f"Linux (from {linux.label})", step_3_4(cfg)
    elif not backup:
        state, steps = "Windows (original, never swapped)", step_1_2(cfg, hint_dir)
    else:
        where = f"{given.label}/{LINUX_NAME} nor " if given else ""
        where += f"any local/calib/*/{LINUX_NAME}"
        state = f"unknown: neither the Windows backup nor {where}"
        steps = [f"to put the Windows program back: sudo {cfg.self_cmd} use-windows"]
    log.out(f"installed {SHOWN}: {state}{bad}")
    log.out(f"backup {SHOWN_BACKUP}: {'present' if backup else 'none'}")
    for line in steps:
        log.out(line)
    return 0


def stat_text(st: os.stat_result) -> str:
    return f"uid {st.st_uid} gid {st.st_gid} mode {stat.S_IMODE(st.st_mode):04o} size {st.st_size}"


# ------------------------------------------------------------------------ main


def parse_args(argv: list[str]) -> tuple[str, str, str | None]:
    """--self CMD -- COMMAND [RUN_DIR] -> (self_cmd, command, run_dir)."""
    self_cmd = shlex.quote(WRAPPER)
    if len(argv) >= 2 and argv[0] == "--self":
        self_cmd, argv = argv[1], argv[2:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    usage = f"see {self_cmd} --help"
    if not argv:
        raise Fail("no subcommand", hint=usage, code=2)
    command, rest = argv[0], argv[1:]
    if command == "use-linux" and len(rest) == 1 and rest[0]:
        return self_cmd, command, rest[0]
    if command == "use-windows" and not rest:
        return self_cmd, command, None
    if command == "status" and len(rest) <= 1 and all(rest):
        return self_cmd, command, rest[0] if rest else None
    raise Fail(f"bad arguments: {' '.join(q(a) for a in argv)}", hint=usage, code=2)


EXIT_INTERRUPTED = 130


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt


def interrupted_line(command: str, self_cmd: str) -> str:
    """What an interrupt leaves: every write is a temp file then a rename/link, so no file is half-written."""
    if command in ("use-linux", "use-windows"):
        return (
            "interrupted; no file is half-written: each is as it was, or as a line above says. "
            f"See: sudo {self_cmd} status"
        )
    return "interrupted; nothing was changed"


def main(argv: list[str] | None = None, environ=None) -> int:
    environ = os.environ if environ is None else environ
    keep_sigterm = signal.signal(signal.SIGTERM, _on_sigterm)  # SIGTERM ends the run like Ctrl-C
    try:
        return _main(argv, environ)
    finally:
        signal.signal(signal.SIGTERM, keep_sigterm)


def _main(argv: list[str] | None, environ) -> int:
    log: Log = NoLog()
    self_cmd, command = shlex.quote(WRAPPER), ""
    try:
        self_cmd, command, run_dir = parse_args(sys.argv[1:] if argv is None else argv)
        cfg = load_config(environ, os.geteuid(), self_cmd)
        if command != "status" and not cfg.test_root and cfg.euid != 0:
            args = [run_dir] if run_dir else []
            line = " ".join(["sudo", self_cmd, command, *(q(a) for a in args)])
            print(f"{command} needs root; nothing was changed. Run:\n  {line}", file=sys.stderr)
            return 2
        log = Log(cfg, environ)
        log.detail(
            f"# b2-program {datetime.now().astimezone().isoformat(timespec='seconds')} euid {cfg.real_euid} "
            f"sudo_uid {environ.get('SUDO_UID', '-')} command {command} run_dir {run_dir!r} "
            f"data_dir {cfg.data_dir} test_root {cfg.test_root!r}"
        )
        if cfg.test_template:
            log.detail(f"test template {cfg.test_template}")
        if command == "use-linux":
            rc = cmd_use_linux(cfg, log, run_dir)
        elif command == "use-windows":
            rc = cmd_use_windows(cfg, log)
        else:
            rc = cmd_status(cfg, log, run_dir)
    except Fail as exc:
        log.failed(exc)
        rc = exc.code
    except KeyboardInterrupt:
        line = interrupted_line(command, self_cmd)
        sys.stdout.flush()
        print(line, file=sys.stderr, flush=True)
        log.detail(line)
        log.detail(traceback.format_exc())
        rc = EXIT_INTERRUPTED
    except OSError as exc:
        # The message only: exc.filename may name program-<serial>.bin.
        log.detail(traceback.format_exc())
        log.failed(Fail(f"{exc.strerror or type(exc).__name__}; details in the log"))
        rc = 1
    except Exception as exc:  # noqa: BLE001 - no traceback on the terminal: it may hold the serial
        log.detail(traceback.format_exc())
        log.failed(Fail(f"unexpected error ({type(exc).__name__}); details in the log"))
        rc = 1
    if log.path:
        print(f"log: {shown_path(log.path)}", flush=True)
        log.close()
    return rc


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
