"""Offline tests of tools/dev/a5-fprintd-dev.sh under a test root (A5_TEST_ROOT).

No root, no systemd, no fprintd, no USB: systemctl, fprintd-list and pacman are stubs,
the "fork build" is a tiny shared library compiled here, and the data files are synthetic
bytes of the right sizes (their module hash is passed in A5_MODULE_SHA256, which the script
honours only under a test root). Nothing outside pytest's tmp_path is written.
"""

from __future__ import annotations

import hashlib
import os
import pty
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "dev" / "a5-fprintd-dev.sh"
CC = shutil.which("cc") or shutil.which("gcc")

SYS_LIBDIR = "/usr/local/lib/libfprint-0078-dev"
SYS_DATA_DIR = "/var/lib/fprint/synaptics0078"
SYS_DIAG_DIR = "/var/lib/fprint/synaptics0078-diag"
DIAG_LINE = f"Environment=FP_SYNAPTICS0078_DUMP_DIR={SYS_DIAG_DIR}\n"
# The first A5 script (private history), the one that was installed for real: its manifest
# and drop-in must be accepted and upgraded by the current script. It is vendored byte for
# byte as a test fixture, so this test runs without that history; the fixture is not run
# anywhere else and keeps the script's own (absent) licence header.
FIRST_A5_SCRIPT = HERE / "fixtures" / "a5-fprintd-dev-first.sh"
SERIAL = "0123456789ab"
MODULE = bytes((i * 7 + 3) % 256 for i in range(10500))
PROGRAM = b"\x02" + bytes((i * 13 + 1) % 256 for i in range(18868))

STUB_SYSTEMCTL = r"""#!/usr/bin/env bash
# Log every call with a snapshot of the watched paths (1 = exists), then answer `show`.
# As systemd: daemon-reload loads the Environment= values of the drop-ins in STUB_DROPIN_DIR into
# STUB_LOADED; `show -p Environment` prints what is loaded, NeedDaemonReload whether that is stale.
on_disk() {
	local f
	for f in "$STUB_DROPIN_DIR"/*.conf; do
		[ -f "$f" ] && sed -n 's/^Environment=//p' "$f" | tr -d '"'
	done | paste -sd' '
}
loaded() { cat -- "$STUB_LOADED" 2>/dev/null || true; }
snap=""
IFS=: read -ra watch <<<"${STUB_WATCH:-}"
for p in "${watch[@]}"; do [ -e "$p" ] && snap+=" 1" || snap+=" 0"; done
echo "systemctl $*|$snap" >>"$STUB_LOG"
# STUB_FAIL: the verb (stop, daemon-reload, ...) that fails, as a broken systemd would.
[ "$1" != "${STUB_FAIL:-}" ] || exit 5
[ "$1" != daemon-reload ] || on_disk >"$STUB_LOADED"
if [ "$1" = show ]; then
	if [[ " $* " == *" --value "* ]]; then
		case " $* " in
		*" ActiveState "*) echo "${STUB_STATE:-inactive}" ;;
		*" ExecMainStartTimestamp "*) echo "${STUB_START:-}" ;;
		*" Environment "*) loaded ;;
		*" NeedDaemonReload "*) if [ "$(on_disk)" = "$(loaded)" ]; then echo no; else echo yes; fi ;;
		*) echo "${STUB_PID:-0}" ;;
		esac
		exit 0
	fi
	printf 'Environment=LD_LIBRARY_PATH=%s\nMainPID=%s\nActiveState=%s\n' "$STUB_LIBDIR" "${STUB_PID:-0}" "${STUB_STATE:-inactive}"
fi
exit 0
"""
STUB_FPRINTD_LIST = r"""#!/usr/bin/env bash
echo "fprintd-list $*" >>"$STUB_LOG"
echo "found 1 devices"
echo "Device at /net/reactivated/Fprint/Device/0"
echo "Using device /net/reactivated/Fprint/Device/0"
echo "User $1 has no fingers enrolled for Synaptics 06cb:0078."
"""
STUB_PACMAN = r"""#!/usr/bin/env bash
# -Qo -- PATH: owned only if PATH is listed in STUB_OWNED (colon-separated).
# It runs after the preflight checked the fork build: STUB_SWAP_TO (copy) or STUB_SWAP_LINK
# (symlink) then replace the build at STUB_SWAP_AT, as a racing user could.
[ -z "${STUB_SWAP_TO:-}" ] || cp -f -- "$STUB_SWAP_TO" "$STUB_SWAP_AT"
[ -z "${STUB_SWAP_LINK:-}" ] || ln -sfn -- "$STUB_SWAP_LINK" "$STUB_SWAP_AT"
path=${@: -1}
IFS=: read -ra owned <<<"${STUB_OWNED:-}"
for p in "${owned[@]}"; do
	if [ "$p" = "$path" ]; then echo "$path is owned by fakepkg 1.0"; exit 0; fi
done
echo "error: No package owns $path" >&2
exit 1
"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_lib(
    out: Path,
    soname="libfprint-2.so.2",
    data_dir=SYS_DATA_DIR,
    extra_symbol=True,
    rpath=None,
    tag="",
    pad=0,
    driver=True,
):
    src = out.with_suffix(".c")
    # pad: many printable strings after the data dir, so `strings` writes far more than a pipe
    # holds after the match (a `strings | grep -q` under pipefail then fails with SIGPIPE).
    padding = "".join(
        f'const char *a5_pad{i} = "padding string number {i:06d} xxxxxxxxxxxxxxxx";\n' for i in range(pad)
    )
    lines = [
        f'const char *a5_data_dir = "{data_dir}";\n',
        padding,
        'const char *a5_domain = "libfprint-synaptics0078";\n' if driver else "",
        f'const char *a5_tag = "{tag}";\n',
        "int fp_context_new (void) { return 0; }\n",
        "int fp_image_extract_sigfm_info (void) { return 1; }\n" if extra_symbol else "",
    ]
    src.write_text("".join(lines))
    cmd = [CC, "-shared", "-fPIC", "-o", str(out), str(src), f"-Wl,-soname,{soname}"]
    if rpath:
        cmd.append(f"-Wl,-rpath,{rpath}")
    subprocess.run(cmd, check=True)
    src.unlink()
    return out


class Env:
    """A test root with stubs, a fake fork build and a data source dir."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = tmp / "root"
        for d in ("usr/local/lib", "usr/lib", "etc/systemd/system", "var/lib", "proc"):
            (self.root / d).mkdir(parents=True)
        self.state = self.root / "var/lib/fprint"
        self.state.mkdir(mode=0o700)
        # Stock libfprint: no synaptics0078 driver (the package's lib has it, see install_package).
        self.system_lib = self.root / "usr/lib/libfprint-2.so.2"
        build_lib(self.system_lib, extra_symbol=False, driver=False)
        self.fork = tmp / "fork"
        self.fork_lib = self.fork / "build/libfprint/libfprint-2.so.2.0.0"
        self.fork_lib.parent.mkdir(parents=True)
        build_lib(self.fork_lib)
        self.data = tmp / "data"
        self.data.mkdir(mode=0o700)
        (self.data / "module.bin").write_bytes(MODULE)
        (self.data / f"program-{SERIAL}.bin").write_bytes(PROGRAM)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, text in (("systemctl", STUB_SYSTEMCTL), ("fprintd-list", STUB_FPRINTD_LIST), ("pacman", STUB_PACMAN)):
            p = self.bin / name
            p.write_text(text)
            p.chmod(0o755)
        self.log = tmp / "calls.log"
        self.loaded = tmp / "systemd-loaded-env"
        self.libdir = self.root / SYS_LIBDIR.lstrip("/")
        self.lib = self.libdir / "libfprint-2.so.2.0.0"
        self.link = self.libdir / "libfprint-2.so.2"
        self.manifest = self.libdir / "a5-install.manifest"
        self.datadir = self.root / SYS_DATA_DIR.lstrip("/")
        self.diagdir = self.root / SYS_DIAG_DIR.lstrip("/")
        self.dropin_dir = self.root / "etc/systemd/system/fprintd.service.d"
        self.dropin = self.dropin_dir / "libfprint-0078-dev.conf"
        self.debug_dropin = self.dropin_dir / "synaptics0078-debug.conf"
        self.extra_env: dict[str, str] = {}

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("A5_", "STUB_", "LIBFPRINT_FORK", "SUDO_"))}
        env.update(
            A5_TEST_ROOT=str(self.root),
            A5_SYSTEMCTL=str(self.bin / "systemctl"),
            A5_FPRINTD_LIST=str(self.bin / "fprintd-list"),
            A5_PACMAN=str(self.bin / "pacman"),
            A5_MODULE_SHA256=sha(MODULE),
            LIBFPRINT_FORK=str(self.fork),
            STUB_LOG=str(self.log),
            STUB_LOADED=str(self.loaded),
            STUB_DROPIN_DIR=str(self.dropin_dir),
            STUB_LIBDIR=SYS_LIBDIR,
            STUB_WATCH=":".join(str(p) for p in (self.dropin, self.lib, self.state / "alice/synaptics0078")),
        )
        env.update(self.extra_env)
        env.update(extra)
        return env

    def run(self, *args, stdin="", script=SCRIPT, cwd=None, **extra):
        if "--data-dir" not in args and args and args[0] == "install":
            args = (*args, "--data-dir", str(self.data))
        self.log.unlink(missing_ok=True)
        return subprocess.run(
            ["bash", str(script), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env(**extra),
            check=False,
            cwd=cwd,
        )

    def run_tty(self, *args, answer: str, **extra):
        """Run with stdin on a pseudo-terminal that already holds ANSWER (the prompts read it)."""
        if args and args[0] == "install":
            args = (*args, "--data-dir", str(self.data))
        self.log.unlink(missing_ok=True)
        master, slave = pty.openpty()
        try:
            os.write(master, answer.encode())
            return subprocess.run(
                ["bash", str(SCRIPT), *args],
                stdin=slave,
                capture_output=True,
                text=True,
                env=self.env(**extra),
                check=False,
                timeout=60,
            )
        finally:
            os.close(master)
            os.close(slave)

    def add_dumps(self, n: int = 3) -> list[Path]:
        out = []
        for i in range(n):
            p = self.diagdir / f"{i:06d}-{1000 + i}-capture.pgm"
            p.write_bytes(b"P5\n# synthetic\n144 56\n255\n" + bytes(144 * 56))
            p.chmod(0o600)
            out.append(p)
        return out

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def systemctl_calls(self) -> list[str]:
        return [c.split("|")[0] for c in self.calls() if c.startswith("systemctl ") and " show " not in c]

    def snapshot(self) -> dict[str, tuple]:
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            for name in dirnames + filenames:
                p = Path(dirpath) / name
                if p in (self.log, self.loaded):
                    continue
                st = os.lstat(p)
                if stat.S_ISLNK(st.st_mode):
                    out[str(p)] = ("link", os.readlink(p))
                elif stat.S_ISDIR(st.st_mode):
                    out[str(p)] = ("dir", stat.S_IMODE(st.st_mode))
                elif not stat.S_ISREG(st.st_mode):  # a fifo: opening it would block
                    out[str(p)] = ("other", stat.S_IFMT(st.st_mode))
                else:
                    out[str(p)] = ("file", stat.S_IMODE(st.st_mode), sha(p.read_bytes()))
        return out

    def install_package(self):
        """As pacman -U of libfprint-sigfm-0078: the system lib now has the driver."""
        build_lib(self.system_lib, tag="package")

    def add_print(self, user: str, driver: str, finger: str = "7") -> Path:
        p = self.state / user / driver / "0" / finger
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"synthetic print " + driver.encode())
        return p


@pytest.fixture
def env(tmp_path):
    if CC is None or not all(shutil.which(t) for t in ("readelf", "nm", "strings")):
        pytest.skip("needs a C compiler and binutils")
    return Env(tmp_path)


def mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def assert_nothing_installed(e: Env):
    assert not e.libdir.exists()
    assert not e.datadir.exists()
    assert not e.dropin_dir.exists()
    assert e.systemctl_calls() == []


# ------------------------------------------------------------------ static checks


def test_bash_syntax_and_mode():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert mode(SCRIPT) == 0o755


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck():
    subprocess.run(["shellcheck", "-x", str(SCRIPT)], check=True)


def test_help_and_usage_errors():
    r = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
    assert r.returncode == 0 and "install" in r.stdout and "TEST ONLY" in r.stdout
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert r.returncode == 2 and "no subcommand" in r.stderr
    r = subprocess.run(["bash", str(SCRIPT), "status", "--keep-prints"], capture_output=True, text=True, check=False)
    assert r.returncode == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="must run as a normal user")
@pytest.mark.parametrize("sub", ["install", "undo"])
def test_refuses_without_root_and_names_the_sudo_command(sub):
    env = {k: v for k, v in os.environ.items() if not k.startswith("A5_")}
    env["LIBFPRINT_FORK"] = "/some/fork"
    r = subprocess.run(["bash", str(SCRIPT), sub], capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 2
    assert f"sudo LIBFPRINT_FORK=/some/fork {SCRIPT} {sub}" in r.stderr
    assert "nothing was changed" in r.stderr
    assert "+ " not in r.stdout


# ------------------------------------------------------------------ install


def test_first_install(env):
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert mode(env.libdir) == 0o755
    assert env.lib.read_bytes() == env.fork_lib.read_bytes() and mode(env.lib) == 0o644
    assert not env.lib.is_symlink()
    assert env.link.is_symlink() and os.readlink(env.link) == "libfprint-2.so.2.0.0"
    assert mode(env.datadir) == 0o700
    assert (env.datadir / "module.bin").read_bytes() == MODULE
    assert (env.datadir / f"program-{SERIAL}.bin").read_bytes() == PROGRAM
    assert mode(env.datadir / "module.bin") == 0o600
    assert mode(env.datadir / f"program-{SERIAL}.bin") == 0o600
    text = env.dropin.read_text()
    assert "[Service]" in text
    assert f"Environment=LD_LIBRARY_PATH={SYS_LIBDIR}\n" in text
    assert "Environment=OPENCV_OPENCL_RUNTIME=disabled\n" in text
    assert 'Environment="G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print"\n' in text
    assert "FP_DRIVERS_ALLOWLIST" not in text
    assert mode(env.dropin) == 0o644
    # No staging leftovers; actions are printed before they run.
    assert not [p for p in env.root.rglob(".*a5-new*")]
    assert "+ stage " in r.stdout and "+ cp -- " in r.stdout and "+ chmod 0644" in r.stdout
    assert "+ mv -f -T" in r.stdout
    assert env.systemctl_calls() == ["systemctl daemon-reload", "systemctl try-restart fprintd.service"]
    entries = [ln.split("\t") for ln in env.manifest.read_text().splitlines() if not ln.startswith("#")]
    recorded = {p: t for t, p, _ in entries}
    assert recorded == {
        str(env.libdir): "dir",
        str(env.lib): "file",
        str(env.link): "link",
        str(env.datadir): "dir",
        str(env.datadir / "module.bin"): "file",
        str(env.datadir / f"program-{SERIAL}.bin"): "file",
        str(env.dropin_dir): "dir",
        str(env.dropin): "file",
    }
    assert str(env.state) not in recorded  # it existed before


@pytest.mark.parametrize(
    "cwd, script, shown",
    [
        (HERE.parent, "tools/dev/a5-fprintd-dev.sh", "tools/dev/a5-fprintd-dev.sh"),  # from the project root
        (HERE / "dev", "a5-fprintd-dev.sh", "./a5-fprintd-dev.sh"),  # bash a5-fprintd-dev.sh in tools/dev
        (None, str(SCRIPT), str(SCRIPT)),
    ],
)
def test_next_hint_is_a_command_that_runs(env, cwd, script, shown):
    """The "next:" line after install is the script as the user can run it from there, never a bare name."""
    r = env.run("install", script=script, cwd=cwd)
    assert r.returncode == 0, r.stdout + r.stderr
    (hint,) = [ln for ln in r.stdout.splitlines() if ln.startswith("next: ")]
    assert hint == f"next: {shown} status"
    ran = subprocess.run(
        ["bash", "-c", hint.removeprefix("next: ")],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env.env(),
        check=False,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "command not found" not in ran.stderr and "diag (raw image dumps" in ran.stdout
    bad = env.run("--bogus", script=script, cwd=cwd)
    assert f"  hint: see {shown} --help" in bad.stderr


def test_install_accepts_a_large_build(env):
    # A real build is ~4.7 MB: checks that read it through a pipe must not die of SIGPIPE.
    build_lib(env.fork_lib, pad=20000)
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr


def test_second_install_is_idempotent(env):
    assert env.run("install").returncode == 0
    before = env.snapshot()
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already installed: nothing changed, fprintd not restarted" in r.stdout
    assert r.stdout.count("already") >= 8
    assert env.snapshot() == before
    assert env.systemctl_calls() == []


def test_reinstall_after_rebuild_replaces_our_lib_and_restarts(env):
    assert env.run("install").returncode == 0
    build_lib(env.fork_lib, tag="rebuilt")
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.lib.read_bytes() == env.fork_lib.read_bytes()
    assert env.systemctl_calls() == ["systemctl try-restart fprintd.service"]
    assert sha(env.fork_lib.read_bytes()) in env.manifest.read_text()


def test_refuses_a_differing_data_file(env):
    env.datadir.mkdir(mode=0o700)
    (env.datadir / "module.bin").write_bytes(b"x" * 10500)
    r = env.run("install")
    assert r.returncode == 2
    assert "differs" in r.stderr and "refusing to overwrite" in r.stderr
    assert (env.datadir / "module.bin").read_bytes() == b"x" * 10500
    assert not env.libdir.exists() and not env.dropin_dir.exists()
    assert env.systemctl_calls() == []


def test_identical_preexisting_data_files_are_used_but_not_recorded(env):
    env.datadir.mkdir(mode=0o700)
    (env.datadir / "module.bin").write_bytes(MODULE)
    (env.datadir / f"program-{SERIAL}.bin").write_bytes(PROGRAM)
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "present: " in r.stdout
    assert str(env.datadir) not in env.manifest.read_text()
    assert env.run("undo").returncode == 0
    assert (env.datadir / "module.bin").read_bytes() == MODULE  # not ours: kept


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"data_dir": "/usr/local/var/lib/fprint/synaptics0078"}, "synaptics0078_data_dir"),
        ({"data_dir": SYS_DATA_DIR + "-test"}, "synaptics0078_data_dir"),
        ({"soname": "libfprint-2.so.3"}, "SONAME"),
        ({"rpath": "/home/someone/lib"}, "RPATH/RUNPATH"),
    ],
)
def test_refuses_a_wrong_build(env, kwargs, message):
    build_lib(env.fork_lib, **kwargs)
    r = env.run("install")
    assert r.returncode == 2
    assert message in r.stderr
    assert_nothing_installed(env)


def test_refuses_a_build_missing_stock_symbols(env):
    src = env.tmp / "stock.c"
    src.write_text("int fp_context_new (void) { return 0; }\nint fp_device_open (void) { return 0; }\n")
    subprocess.run(
        [
            CC,
            "-shared",
            "-fPIC",
            "-o",
            str(env.root / "usr/lib/libfprint-2.so.2"),
            str(src),
            "-Wl,-soname,libfprint-2.so.2",
        ],
        check=True,
    )
    r = env.run("install")
    assert r.returncode == 2 and "fp_device_open" in r.stderr
    assert_nothing_installed(env)


def test_refuses_a_symlinked_build(env):
    real = env.fork_lib.with_name("real.so")
    env.fork_lib.rename(real)
    env.fork_lib.symlink_to(real)
    r = env.run("install")
    assert r.returncode == 2 and "symlink" in r.stderr
    assert_nothing_installed(env)


@pytest.mark.parametrize("swap", ["wrong_build", "symlink_to_secret"])
def test_install_checks_the_staged_copy_not_the_source(env, swap):
    # The build passes the preflight, then is replaced before it is copied: the checks run
    # again on the root-owned copy, which is refused and removed.
    env.dropin_dir.mkdir()  # an existing target, so the pacman stub (the swap) runs
    secret = env.tmp / "secret"
    secret.write_bytes(b"A5-SECRET-CONTENT\n")
    extra = {"STUB_SWAP_AT": str(env.fork_lib)}
    if swap == "wrong_build":
        extra["STUB_SWAP_TO"] = str(build_lib(env.tmp / "bad.so", data_dir="/elsewhere/synaptics0078"))
    else:
        extra["STUB_SWAP_LINK"] = str(secret)
    r = env.run("install", **extra)
    assert r.returncode == 2, r.stdout + r.stderr
    message = "synaptics0078_data_dir" if swap == "wrong_build" else "SONAME"
    assert message in r.stderr and ".libfprint-2.so.2.0.0.a5-new." in r.stderr  # the copy was checked
    assert not env.lib.exists()
    assert not [p for p in env.root.rglob(".*a5-new*")]
    leaked = [p for p in env.root.rglob("*") if p.is_file() and b"A5-SECRET" in p.read_bytes()]
    assert leaked == []
    assert not env.dropin.exists() and env.systemctl_calls() == []


@pytest.mark.parametrize("case", ["module_hash", "module_size", "two_programs", "opcode", "no_program"])
def test_refuses_bad_data(env, case):
    extra = {}
    if case == "module_hash":
        extra["A5_MODULE_SHA256"] = "0" * 64
    elif case == "module_size":
        (env.data / "module.bin").write_bytes(MODULE[:-1])
    elif case == "two_programs":
        (env.data / "program-ba9876543210.bin").write_bytes(PROGRAM)
    elif case == "opcode":
        (env.data / f"program-{SERIAL}.bin").write_bytes(b"\x03" + PROGRAM[1:])
    elif case == "no_program":
        (env.data / f"program-{SERIAL}.bin").unlink()
    r = env.run("install", **extra)
    assert r.returncode == 2, r.stdout
    assert_nothing_installed(env)


def test_test_hooks_are_inert_without_a_test_root(env, tmp_path):
    # Without A5_TEST_ROOT the command overrides are ignored: status runs the commands found
    # on PATH (stubs here, so no real fprintd is started), never the A5_* ones.
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    for name in ("systemctl", "fprintd-list"):
        (shadow / name).write_text(f'#!/usr/bin/env bash\necho "PATH-{name} $*" >>"$STUB_LOG"\n')
        (shadow / name).chmod(0o755)
    run_env = env.env(PATH=f"{shadow}:{os.environ['PATH']}", USER="alice")
    del run_env["A5_TEST_ROOT"]
    r = subprocess.run(["bash", str(SCRIPT), "status"], capture_output=True, text=True, env=run_env, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = env.calls()
    assert "PATH-fprintd-list alice" in calls
    assert any(c.startswith("PATH-systemctl show fprintd.service") for c in calls)
    assert not any(c.startswith(("systemctl ", "fprintd-list ")) for c in calls)  # the A5_* stubs


@pytest.mark.skipif(os.geteuid() == 0, reason="A5_FAKE_EUID is only honoured when not root")
@pytest.mark.parametrize(
    "var", ["A5_TEST_ROOT", "A5_SYSTEMCTL", "A5_FPRINTD_LIST", "A5_PACMAN", "A5_MODULE_SHA256", "A5_FAKE_EUID"]
)
def test_refuses_test_hooks_as_root(env, var):
    # As root (faked: A5_FAKE_EUID=0, which a real root run refuses too) any test-only
    # variable stops the script before it does anything, e.g. when passed by sudo -E.
    run_env = {k: v for k, v in env.env().items() if not k.startswith("A5_")}
    run_env[var] = str(env.root) if var == "A5_TEST_ROOT" else "/tmp/x"
    run_env["A5_FAKE_EUID"] = "0"
    for sub in ("install", "undo", "status"):
        r = subprocess.run(["bash", str(SCRIPT), sub], capture_output=True, text=True, env=run_env, check=False)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "refused as root" in r.stderr and r.stdout == ""
    # A non-zero fake euid changes nothing: the test root still works.
    assert env.run("install", A5_FAKE_EUID="1000").returncode == 0
    assert env.calls() != []


def test_refuses_package_owned_targets(env):
    env.dropin_dir.mkdir()
    r = env.run("install", STUB_OWNED=str(env.dropin_dir))
    assert r.returncode == 2 and "belongs to a package" in r.stderr
    assert not env.libdir.exists() and not env.datadir.exists()


def test_refuses_a_foreign_lib_dir_and_a_foreign_dropin(env):
    env.libdir.mkdir()
    (env.libdir / "something").write_text("x")
    r = env.run("install")
    assert r.returncode == 2 and "no manifest" in r.stderr
    shutil.rmtree(env.libdir)
    env.dropin_dir.mkdir()
    env.dropin.write_text("[Service]\nEnvironment=FOO=bar\n")
    r = env.run("install")
    assert r.returncode == 2 and "not written by this script" in r.stderr
    assert not env.libdir.exists()


def test_refuses_a_symlinked_target_dir(env):
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    env.libdir.symlink_to(elsewhere)
    r = env.run("install")
    assert r.returncode == 2 and "symlink" in r.stderr
    assert list(elsewhere.iterdir()) == []


# ------------------------------------------------------------------ undo


def test_undo_removes_only_what_install_created(env):
    other_dropin = env.dropin_dir / "other.conf"
    env.dropin_dir.mkdir()
    other_dropin.write_text("[Service]\n")
    other_lib = env.root / "usr/local/lib/libother.so"
    other_lib.write_text("x")
    other_print = env.add_print("alice", "goodix")
    assert env.run("install").returncode == 0
    ours = env.add_print("alice", "synaptics0078")

    r = env.run("undo", "--keep-prints")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not env.libdir.exists()
    assert not env.dropin.exists()
    assert env.dropin_dir.is_dir() and other_dropin.exists()  # existed before install
    assert not env.datadir.exists()
    assert env.state.is_dir() and other_lib.exists() and other_print.exists()
    assert ours.exists()  # --keep-prints
    assert "prints kept" in r.stdout
    # Order: drop-in gone and daemon-reload before stop; the lib is deleted after the stop.
    calls = [c for c in env.calls() if c.startswith("systemctl ")]
    assert [c.split("|")[0] for c in calls] == ["systemctl daemon-reload", "systemctl stop fprintd.service"]
    stop_snapshot = calls[1].split("|")[1].split()
    assert stop_snapshot == ["0", "1", "1"]  # drop-in gone, lib still there, prints there


def test_undo_is_idempotent(env):
    assert env.run("install").returncode == 0
    assert env.run("undo").returncode == 0
    before = env.snapshot()
    r = env.run("undo")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already undone" in r.stdout
    assert env.snapshot() == before
    assert env.systemctl_calls() == []


@pytest.mark.parametrize("stdin", ["", "\n", "n\n", "maybe\n"])
def test_undo_prints_prompt_defaults_to_no(env, stdin):
    assert env.run("install").returncode == 0
    ours = env.add_print("alice", "synaptics0078")
    r = env.run("undo", stdin=stdin)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[y/N]" in r.stdout and str(ours) in r.stdout
    assert ours.exists() and "prints kept" in r.stdout


def test_undo_deletes_this_drivers_prints_on_yes_only(env):
    # The data dir existed before install, so it is not ours: never touched by undo.
    env.datadir.mkdir(mode=0o700)
    (env.datadir / "module.bin").write_bytes(MODULE)
    (env.datadir / f"program-{SERIAL}.bin").write_bytes(PROGRAM)
    assert env.run("install").returncode == 0
    ours_a = env.add_print("alice", "synaptics0078")
    ours_b = env.add_print("bob", "synaptics0078", "1")
    other = env.add_print("alice", "goodix")
    r = env.run("undo", stdin="y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not ours_a.parent.parent.exists() and not ours_b.parent.parent.exists()
    assert other.exists() and (env.state / "alice").is_dir()
    assert (env.datadir / "module.bin").read_bytes() == MODULE
    assert (env.datadir / f"program-{SERIAL}.bin").read_bytes() == PROGRAM
    calls = [c for c in env.calls() if c.startswith("systemctl stop")]
    assert calls and calls[0].split("|")[1].split()[2] == "1"  # prints still there at stop


def test_undo_yes_flag_without_manifest_still_stops_first(env):
    ours = env.add_print("alice", "synaptics0078")
    r = env.run("undo", "--yes-delete-prints")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not ours.parent.parent.exists()
    assert env.systemctl_calls() == ["systemctl stop fprintd.service"]


def test_undo_keeps_a_changed_file_and_its_record(env):
    assert env.run("install").returncode == 0
    (env.datadir / "module.bin").write_bytes(b"changed")
    r = env.run("undo")
    assert r.returncode == 1
    assert "WARN  kept" in r.stderr and "undo incomplete" in r.stdout
    assert (env.datadir / "module.bin").read_bytes() == b"changed"
    assert not env.lib.exists() and not env.dropin.exists()
    assert env.manifest.exists() and str(env.datadir / "module.bin") in env.manifest.read_text()


def test_undo_skips_symlinked_print_dirs_and_finishes(env):
    # A WARN about a symlinked user dir (or driver dir) must not become a "print dir" that
    # undo tries to delete: undo completes, the link targets are untouched.
    assert env.run("install").returncode == 0
    ours = env.add_print("alice", "synaptics0078")
    outside_user = env.tmp / "elsewhere-user"
    (outside_user / "synaptics0078/0").mkdir(parents=True)
    victim_a = outside_user / "synaptics0078/0/7"
    victim_a.write_bytes(b"not ours")
    (env.state / "mallory").symlink_to(outside_user)
    outside_driver = env.tmp / "elsewhere-driver"
    (outside_driver / "0").mkdir(parents=True)
    victim_b = outside_driver / "0/1"
    victim_b.write_bytes(b"not ours either")
    (env.state / "bob").mkdir()
    (env.state / "bob/synaptics0078").symlink_to(outside_driver)

    r = env.run("undo", "--yes-delete-prints")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "undo done" in r.stdout
    assert f"  {env.state / 'alice/synaptics0078'}/\n" in r.stdout
    assert "mallory" not in r.stdout and "bob" not in r.stdout
    assert r.stderr.count("WARN  skipping") == 2
    assert not ours.exists()
    assert victim_a.read_bytes() == b"not ours" and victim_b.read_bytes() == b"not ours either"
    assert (env.state / "mallory").is_symlink() and (env.state / "bob/synaptics0078").is_symlink()
    assert not env.libdir.exists()


def manifest_entries(e: Env) -> list[tuple[str, str]]:
    return [tuple(ln.split("\t")[:2]) for ln in e.manifest.read_text().splitlines() if not ln.startswith("#")]


@pytest.mark.parametrize("case", ["dotdot", "outside", "double_slash", "wrong_type", "symlinked_parent"])
def test_undo_refuses_a_manifest_entry_install_never_writes(env, case):
    assert env.run("install").returncode == 0
    victim = env.tmp / "victim.txt"
    victim.write_bytes(b"precious")
    vsha = sha(b"precious")
    up = "/".join([".."] * 5)  # libdir is root/usr/local/lib/libfprint-0078-dev
    entry = {
        "dotdot": f"file\t{env.libdir}/{up}/victim.txt\t{vsha}",
        "outside": f"file\t{victim}\t{vsha}",
        "double_slash": f"file\t{env.libdir}//libfprint-2.so.2.0.0\t{vsha}",
        "wrong_type": f"file\t{env.datadir}\t{vsha}",
        "symlinked_parent": None,
    }[case]
    if entry is None:
        # The data dir swapped for a symlink to a dir holding an identical module.bin.
        moved = env.tmp / "moved-data"
        shutil.copytree(env.datadir, moved)
        shutil.rmtree(env.datadir)
        env.datadir.symlink_to(moved)
        victim = moved / "module.bin"
    else:
        with env.manifest.open("a") as f:
            f.write(entry + "\n")
    before = env.snapshot()
    r = env.run("undo", "--yes-delete-prints")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "refusing to act on it" in r.stderr
    assert victim.exists()
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_undo_refuses_a_writable_manifest(env):
    assert env.run("install").returncode == 0
    env.manifest.chmod(0o664)
    before = env.snapshot()
    r = env.run("undo")
    assert r.returncode == 2 and "group- or world-writable" in r.stderr
    assert env.snapshot() == before and env.systemctl_calls() == []


@pytest.mark.parametrize("where", ["dropin_dir", "libdir"])
def test_undo_keeps_records_of_dirs_left_in_place_and_can_be_rerun(env, where):
    assert env.run("install").returncode == 0  # creates the drop-in dir: recorded
    target = env.dropin_dir if where == "dropin_dir" else env.libdir
    foreign = target / "foreign.conf"
    foreign.write_text("[Service]\n")
    r = env.run("undo")
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"left {target} in place" in r.stdout and "undo incomplete" in r.stdout
    assert foreign.exists() and not env.lib.exists() and not env.dropin.exists() and not env.datadir.exists()
    expected = [("dir", str(env.libdir))] if where == "libdir" else [("dir", str(env.libdir)), ("dir", str(target))]
    assert sorted(manifest_entries(env)) == sorted(expected)
    # Retry while it is still there: same result, nothing else changes.
    before = env.snapshot()
    assert env.run("undo").returncode == 1
    assert env.snapshot() == before
    # Once it is empty, undo finishes the job.
    foreign.unlink()
    r = env.run("undo")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not target.exists() and not env.libdir.exists()
    r = env.run("undo")
    assert r.returncode == 0 and "already undone" in r.stdout


# ------------------------------------------------------------------ dry run


def test_dry_run_changes_nothing(env):
    before = env.snapshot()
    r = env.run("install", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "+ install -d -m 0755" in r.stdout and "check the copy, chmod 0600" in r.stdout
    assert "systemctl daemon-reload" in r.stdout
    assert env.snapshot() == before
    assert env.systemctl_calls() == []

    assert env.run("install").returncode == 0
    ours = env.add_print("alice", "synaptics0078")
    before = env.snapshot()
    r = env.run("undo", "--dry-run", "--yes-delete-prints")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "+ rm -r --one-file-system" in r.stdout and "+ rmdir" in r.stdout
    assert env.snapshot() == before and ours.exists()
    assert env.systemctl_calls() == []


# ------------------------------------------------------------------ status


def test_status_after_install(env):
    assert env.run("install").returncode == 0
    (env.root / "proc/4242").mkdir()
    (env.root / "proc/4242/maps").write_text(
        f"7f0000000000-7f0000001000 r-xp 00000000 00:1f 123 {SYS_LIBDIR}/libfprint-2.so.2.0.0\n"
        "7f0000002000-7f0000003000 r--p 00000000 00:1f 124 /usr/lib/libglib-2.0.so.0.8800.3\n"
    )
    r = env.run("status", STUB_PID="4242", STUB_STATE="active", USER="alice")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Environment=LD_LIBRARY_PATH=" in r.stdout
    assert f"{SYS_LIBDIR}/libfprint-2.so.2.0.0  (the fork, from this dev install)" in r.stdout
    assert "same as the fork build" in r.stdout
    assert "module.bin: 10500 B, mode 600" in r.stdout and "sha256 ok" in r.stdout
    assert f"program-{SERIAL}.bin: 18869 B" in r.stdout
    assert "User alice has no fingers enrolled" in r.stdout
    assert "fprintd-list alice" in env.calls()


def test_status_without_install_and_not_running(env):
    r = env.run("status", USER="alice")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "absent: fprintd uses the packaged libfprint" in r.stdout
    assert "fprintd is not running" in r.stdout


def test_status_reports_a_replaced_lib_and_unreadable_maps(env):
    assert env.run("install").returncode == 0
    (env.root / "proc/77").mkdir()
    (env.root / "proc/77/maps").write_text(
        f"7f0000000000-7f0000001000 r-xp 00000000 00:1f 123 {SYS_LIBDIR}/libfprint-2.so.2.0.0 (deleted)\n"
    )
    r = env.run("status", STUB_PID="77")
    assert "replaced since fprintd started" in r.stdout
    r = env.run("status", STUB_PID="78")
    assert "cannot read" in r.stdout and "sudo" in r.stdout


# ------------------------------------------------------------------ upgrade in place


def manifest_values(e: Env) -> dict[str, tuple[str, str]]:
    rows = [ln.split("\t") for ln in e.manifest.read_text().splitlines() if not ln.startswith("#")]
    paths = [r[1] for r in rows]
    assert len(paths) == len(set(paths)), f"duplicate manifest entries: {paths}"
    return {r[1]: (r[0], r[2]) for r in rows}


def test_upgrade_replaces_the_lib_updates_the_manifest_and_restarts_once(env):
    assert env.run("install").returncode == 0
    v1 = sha(env.fork_lib.read_bytes())
    build_lib(env.fork_lib, tag="v2")
    v2 = sha(env.fork_lib.read_bytes())
    assert v1 != v2
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sha(env.lib.read_bytes()) == v2 and mode(env.lib) == 0o644
    assert manifest_values(env)[str(env.lib)] == ("file", v2)
    assert len(manifest_values(env)) == 8
    # The drop-in did not change: no daemon-reload; one try-restart.
    assert env.systemctl_calls() == ["systemctl try-restart fprintd.service"]
    assert f"upgraded: lib sha {v1[:12]}... -> {v2[:12]}...; fprintd restarted (if it was running)" in r.stdout
    assert not [p for p in env.root.rglob(".*a5-new*")] + [p for p in env.libdir.glob(".a5-install.manifest.*")]
    # The same build again: nothing changes, nothing restarts.
    before = env.snapshot()
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already installed: nothing changed, fprintd not restarted" in r.stdout
    assert "upgraded" not in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


def committed_script(tmp: Path) -> Path:
    """The first A5 script, placed at tools/dev/ of a scratch tree as it was installed."""
    old = tmp / "old/tools/dev/a5-fprintd-dev.sh"
    old.parent.mkdir(parents=True)
    old.write_bytes(FIRST_A5_SCRIPT.read_bytes())
    return old


def test_upgrade_from_the_committed_version(env):
    old = committed_script(env.tmp)
    r = env.run("install", script=old)
    assert r.returncode == 0, r.stdout + r.stderr
    old_dropin = env.dropin.read_bytes()
    assert "diagdir" not in env.manifest.read_text()
    # Its drop-in text is the current one without --diag: a plain install changes nothing.
    before = env.snapshot()
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing changed" in r.stdout and env.snapshot() == before
    # A rebuilt lib and --diag: the old drop-in is recognised as ours and rewritten.
    build_lib(env.fork_lib, tag="v2")
    r = env.run("install", "--diag", "--yes-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    text = env.dropin.read_text()
    assert text.startswith(old_dropin.decode()) and text.endswith(DIAG_LINE)
    entries = manifest_values(env)
    assert entries[str(env.lib)] == ("file", sha(env.fork_lib.read_bytes()))
    assert entries[str(env.dropin)] == ("file", sha(env.dropin.read_bytes()))
    assert entries[str(env.diagdir)][0] == "diagdir" and len(entries) == 9
    assert env.systemctl_calls() == ["systemctl daemon-reload", "systemctl try-restart fprintd.service"]
    assert "upgraded: lib sha " in r.stdout and "diag dir, drop-in rewritten (diag on);" in r.stdout
    # And back to the committed version's state with undo.
    r = env.run("undo", "--yes-delete-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not env.libdir.exists() and not env.diagdir.exists() and not env.dropin.exists()


def test_install_refuses_to_overwrite_an_edited_dropin(env):
    assert env.run("install").returncode == 0
    edited = env.dropin.read_text() + "Environment=MINE=1\n"
    env.dropin.write_text(edited)
    before = env.snapshot()
    r = env.run("install", "--diag", "--yes-diag")
    assert r.returncode == 2 and "edited since install" in r.stderr
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_install_refuses_to_overwrite_a_replaced_lib(env):
    # The installed lib no longer has the sha256 the manifest recorded: replaced by hand.
    assert env.run("install").returncode == 0
    env.lib.write_bytes(b"hand-replaced lib")
    build_lib(env.fork_lib, tag="v2")
    before = env.snapshot()
    r = env.run("install")
    assert r.returncode == 2, r.stdout + r.stderr
    assert f"{env.lib} was replaced since install (sha256 differs from the manifest)" in r.stderr
    assert "remove it" in r.stderr and "run undo" in r.stderr
    assert env.snapshot() == before and env.systemctl_calls() == []
    # Once it is removed, install puts the build back and records its sha256.
    env.lib.unlink()
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert manifest_values(env)[str(env.lib)] == ("file", sha(env.fork_lib.read_bytes()))
    assert "upgraded" not in r.stdout and "installed: lib;" in r.stdout


# ------------------------------------------------------------------ --diag


def test_diag_install(env):
    r = env.run("install", "--diag", "--yes-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.diagdir.is_dir() and not env.diagdir.is_symlink() and mode(env.diagdir) == 0o700
    assert manifest_values(env)[str(env.diagdir)] == ("diagdir", "")
    text = env.dropin.read_text()
    assert text.endswith(DIAG_LINE) and f"Environment=LD_LIBRARY_PATH={SYS_LIBDIR}\n" in text
    assert env.systemctl_calls() == ["systemctl daemon-reload", "systemctl try-restart fprintd.service"]
    assert "diag: confirmed by --yes-diag" in r.stdout
    assert r.stdout.count("installed: ") == 1 and "diag dir, drop-in (diag on);" in r.stdout
    # Again, with dumps in it: nothing changes.
    env.add_dumps()
    before = env.snapshot()
    r = env.run("install", "--diag", "--yes-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"present: {env.diagdir} (3 file(s))" in r.stdout and "nothing changed" in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


@pytest.mark.parametrize("installed", [False, True])
def test_diag_refused_without_a_terminal_or_flag(env, installed):
    if installed:
        assert env.run("install").returncode == 0
    before = env.snapshot()
    r = env.run("install", "--diag", stdin="y\n")  # a pipe is no terminal, even with a yes in it
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no terminal" in r.stderr and "--yes-diag" in r.stderr and "nothing was changed" in r.stderr
    assert env.snapshot() == before and env.systemctl_calls() == []


@pytest.mark.parametrize("answer, ok", [("y\n", True), ("n\n", False), ("\n", False)])
def test_diag_asks_on_a_terminal(env, answer, ok):
    before = env.snapshot()
    r = env.run_tty("install", "--diag", answer=answer)
    assert (
        f"--diag stores raw fingerprint images of every touch in {SYS_DIAG_DIR} (root only). Continue? [y/N]"
        in r.stdout
    )
    if ok:
        assert r.returncode == 0, r.stdout + r.stderr
        assert env.diagdir.is_dir() and env.dropin.read_text().endswith(DIAG_LINE)
    else:
        assert r.returncode == 2 and "not confirmed; nothing was changed" in r.stderr
        assert env.snapshot() == before and env.systemctl_calls() == []


@pytest.mark.parametrize(
    "args",
    [
        ("install", "--yes-diag"),
        ("undo", "--diag"),
        ("status", "--diag"),
        ("install", "--keep-diag"),
        ("install", "--diag", "--yes-delete-diag"),
    ],
)
def test_diag_flag_usage_errors(env, args):
    r = env.run(*args)
    assert r.returncode == 2 and ("only appl" in r.stderr or "add --diag" in r.stderr)
    assert_nothing_installed(env)


def test_diag_dry_run_changes_nothing(env):
    before = env.snapshot()
    r = env.run("install", "--diag", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dry run: would ask '--diag stores raw" in r.stdout
    assert f"+ install -d -m 0700 -- {env.diagdir}" in r.stdout and DIAG_LINE.strip() in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_install_without_diag_stops_the_dumps_and_keeps_the_dir(env):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    dumps = env.add_dumps()
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    text = env.dropin.read_text()
    assert "FP_SYNAPTICS0078_DUMP_DIR" not in text and f"Environment=LD_LIBRARY_PATH={SYS_LIBDIR}\n" in text
    assert all(p.exists() for p in dumps)
    assert f"kept {env.diagdir} (3 file(s)) from a --diag install" in r.stdout and "undo deletes it" in r.stdout
    assert "upgraded: drop-in rewritten (diag off); fprintd restarted" in r.stdout
    assert env.systemctl_calls() == ["systemctl daemon-reload", "systemctl try-restart fprintd.service"]
    entries = manifest_values(env)
    assert entries[str(env.diagdir)][0] == "diagdir"
    assert entries[str(env.dropin)] == ("file", sha(env.dropin.read_bytes()))
    # --diag again turns it back on and reuses the recorded dir.
    r = env.run("install", "--diag", "--yes-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.dropin.read_text().endswith(DIAG_LINE) and all(p.exists() for p in dumps)


def test_diag_install_refuses_an_unrecorded_diag_dir(env):
    assert env.run("install").returncode == 0
    env.diagdir.mkdir(mode=0o700)
    (env.diagdir / "someone-elses").write_text("x")
    before = env.snapshot()
    r = env.run("install", "--diag", "--yes-diag")
    assert r.returncode == 2 and "was not created by this script" in r.stderr
    assert env.snapshot() == before
    shutil.rmtree(env.diagdir)
    env.diagdir.symlink_to(env.tmp)
    r = env.run("install", "--diag", "--yes-diag")
    assert r.returncode == 2 and "symlink" in r.stderr


# ------------------------------------------------------------------ undo of --diag


def diag_env(e: Env) -> dict[str, str]:
    # The stop snapshot also says whether the diag dir still exists (4th position).
    watch = (e.dropin, e.lib, e.state / "alice/synaptics0078", e.diagdir)
    return {"STUB_WATCH": ":".join(str(p) for p in watch)}


def test_undo_deletes_the_diag_dir_on_yes_after_the_stop(env):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(2)
    r = env.run("undo", "--yes-delete-diag", **diag_env(env))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"2 file(s) in {env.diagdir}" in r.stdout and "diag: yes (from the command line)" in r.stdout
    assert not env.diagdir.exists() and not env.libdir.exists() and not env.dropin.exists()
    assert env.state.is_dir() and "undo done" in r.stdout
    stops = [c for c in env.calls() if c.startswith("systemctl stop")]
    assert len(stops) == 1 and stops[0].split("|")[1].split()[3] == "1"  # still there at the stop
    assert f"+ find -P {env.diagdir} -mindepth 1 -maxdepth 1 -type f -delete" in r.stdout


def test_undo_asks_about_the_diag_images_on_a_terminal(env):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(4)
    r = env.run_tty("undo", answer="y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Delete 4 diag images in {env.diagdir}? [y/N]" in r.stdout
    assert not env.diagdir.exists() and not env.libdir.exists()


@pytest.mark.parametrize("how", ["flag", "tty_no", "no_tty"])
def test_undo_keeps_the_diag_dir_and_can_delete_it_later(env, how):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    dumps = env.add_dumps()
    if how == "flag":
        r = env.run("undo", "--keep-diag")
    elif how == "tty_no":
        r = env.run_tty("undo", answer="n\n")
    else:
        r = env.run("undo", stdin="y\n")  # a pipe: no terminal, so no question and no delete
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(p.exists() for p in dumps)
    assert f"sudo rm -r --one-file-system -- {env.diagdir}" in r.stdout
    assert "undo done, except the diag images kept on request" in r.stdout
    if how == "no_tty":
        assert "no terminal to ask" in r.stdout
    assert not env.lib.exists() and not env.link.exists() and not env.dropin.exists()
    assert not env.datadir.exists() and not env.dropin_dir.exists()
    assert sorted(manifest_entries(env)) == sorted([("dir", str(env.libdir)), ("diagdir", str(env.diagdir))])
    assert sorted(p.name for p in env.libdir.iterdir()) == ["a5-install.manifest"]
    # Rerun while still keeping it: no stop (fprintd uses nothing that is left), same state.
    before = env.snapshot()
    r = env.run("undo", "--keep-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.snapshot() == before and env.systemctl_calls() == []
    # Later, yes: everything is gone.
    r = env.run("undo", "--yes-delete-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not env.diagdir.exists() and not env.libdir.exists()
    assert env.systemctl_calls() == ["systemctl stop fprintd.service"]
    r = env.run("undo", "--yes-delete-diag")
    assert r.returncode == 0 and "already undone" in r.stdout


@pytest.mark.parametrize("case", ["symlink", "subdir", "symlink_inside", "fifo", "file"])
def test_undo_refuses_an_unsafe_diag_dir(env, case):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(1)
    outside = env.tmp / "outside"
    outside.mkdir()
    victim = outside / "precious"
    victim.write_bytes(b"precious")
    if case == "symlink":
        shutil.rmtree(env.diagdir)
        env.diagdir.symlink_to(outside)
        message = "it is a symlink"
    elif case == "subdir":
        (env.diagdir / "sub").mkdir()
        (env.diagdir / "sub/x.pgm").write_bytes(b"x")
        message = "it holds 'sub', which is not a regular file"
    elif case == "symlink_inside":
        (env.diagdir / "link.pgm").symlink_to(victim)
        message = "it holds 'link.pgm', which is not a regular file"
    elif case == "fifo":
        os.mkfifo(env.diagdir / "pipe")
        message = "it holds 'pipe', which is not a regular file"
    else:
        shutil.rmtree(env.diagdir)
        env.diagdir.write_bytes(b"not a dir")
        message = "it is not a directory"
    r = env.run("undo", "--yes-delete-diag")
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"WARN  kept {env.diagdir}: {message}; refusing to delete it" in r.stderr
    assert "undo incomplete" in r.stdout
    assert victim.read_bytes() == b"precious" and (env.diagdir.exists() or env.diagdir.is_symlink())
    if case in ("subdir", "symlink_inside", "fifo"):
        assert len(list(env.diagdir.iterdir())) == 2  # nothing in it deleted
    assert ("diagdir", str(env.diagdir)) in manifest_entries(env)
    assert not env.lib.exists() and not env.dropin.exists()  # the rest is undone
    # Rerunnable: the same refusal, nothing else changes.
    before = env.snapshot()
    assert env.run("undo", "--yes-delete-diag").returncode == 1
    assert env.snapshot() == before


def test_undo_keep_diag_when_install_created_the_state_dir(env):
    # No /var/lib/fprint before install: install creates and records it, and the kept diag
    # dir is inside it. Keeping the images is not an incomplete undo.
    env.state.rmdir()
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    assert manifest_values(env)[str(env.state)] == ("dir", "")
    dumps = env.add_dumps()
    before = env.snapshot()
    r = env.run("undo", "--dry-run", "--keep-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"would keep {env.state}: it holds {env.diagdir}" in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []
    r = env.run("undo", "--keep-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "undo done, except the diag images kept on request" in r.stdout
    assert "undo incomplete" not in r.stdout and "WARN" not in r.stderr
    assert "files install did not create" not in r.stdout
    assert f"kept {env.state}: it holds {env.diagdir}, which stays" in r.stdout
    assert all(p.exists() for p in dumps)
    assert sorted(manifest_entries(env)) == sorted(
        [("dir", str(env.libdir)), ("dir", str(env.state)), ("diagdir", str(env.diagdir))]
    )
    # Rerun while keeping it: no stop, same state.
    before = env.snapshot()
    r = env.run("undo", "--keep-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.snapshot() == before and env.systemctl_calls() == []
    # Later, yes: the diag dir, then the state dir install created, then the lib dir go.
    r = env.run("undo", "--yes-delete-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "undo done" in r.stdout
    assert not env.diagdir.exists() and not env.state.exists() and not env.libdir.exists()
    assert env.systemctl_calls() == ["systemctl stop fprintd.service"]


@pytest.mark.parametrize("state_created", [False, True])
def test_install_after_an_undo_that_kept_only_the_diag_dir_says_installed(env, state_created):
    if state_created:
        env.state.rmdir()
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(1)
    assert env.run("undo", "--keep-diag").returncode == 0
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "upgraded" not in r.stdout
    assert "installed: lib, libfprint-2.so.2 link, module.bin," in r.stdout
    assert f"kept {env.diagdir} (1 file(s)) from a --diag install" in r.stdout
    assert manifest_values(env)[str(env.diagdir)][0] == "diagdir"


@pytest.mark.parametrize("with_diag", [True, False])
def test_install_sets_a_widened_recorded_diag_dir_back_to_0700(env, with_diag):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.diagdir.chmod(0o755)
    args = ("install", "--diag", "--yes-diag") if with_diag else ("install",)
    r = env.run(*args)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"WARN  {env.diagdir} is mode 755, not 0700; setting it back to 0700" in r.stderr
    assert f"+ chmod 0700 -- {env.diagdir}" in r.stdout
    assert mode(env.diagdir) == 0o700


def test_install_leaves_the_mode_of_an_unrecorded_diag_dir(env):
    env.diagdir.mkdir(mode=0o755)
    r = env.run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert mode(env.diagdir) == 0o755 and "0700" not in r.stderr


@pytest.mark.skipif(shutil.which("mountpoint") is None, reason="mountpoint not installed")
def test_undo_refuses_a_bind_mounted_diag_dir(env):
    # A bind mount from the same filesystem keeps its parent's device number; mountpoint(1)
    # sees it. No mount without root here: a mountpoint on PATH reports the diag dir as one.
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    dumps = env.add_dumps(1)
    stub = env.bin / "mountpoint"
    stub.write_text('#!/usr/bin/env bash\n[ "${@: -1}" = "$DIAG" ]\n')
    stub.chmod(0o755)
    r = env.run("undo", "--yes-delete-diag", PATH=f"{env.bin}:{os.environ['PATH']}", DIAG=str(env.diagdir))
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"WARN  kept {env.diagdir}: it is a mount point; refusing to delete it" in r.stderr
    assert all(p.exists() for p in dumps)
    assert ("diagdir", str(env.diagdir)) in manifest_entries(env)


def test_undo_deletes_odd_names_and_only_unlinks_a_hard_link(env):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    outside = env.tmp / "outside-precious"
    outside.write_bytes(b"precious")
    os.link(outside, env.diagdir / "hardlink.pgm")
    names = ["-rf", "new\nline.pgm", ".hidden.pgm"]
    for name in names:
        (env.diagdir / name).write_bytes(b"x")
    r = env.run("undo", "--yes-delete-diag")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"4 file(s) in {env.diagdir}" in r.stdout and "undo done" in r.stdout
    assert not env.diagdir.exists() and not env.libdir.exists()
    assert outside.read_bytes() == b"precious" and os.stat(outside).st_nlink == 1


def test_undo_refuses_a_foreign_owned_diag_dir(env):
    # Under a test root the expected owner is the test user. A directory of another owner
    # cannot be created without root, so a stat on PATH reports one for the diag dir only.
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(1)
    stub = env.bin / "stat"
    stub.write_text(
        '#!/usr/bin/env bash\nif [ "$2" = %u ] && [ "${@: -1}" = "$DIAG" ]; then echo 4242; exit 0; fi\n'
        'exec /usr/bin/stat "$@"\n'
    )
    stub.chmod(0o755)
    r = env.run("undo", "--yes-delete-diag", PATH=f"{env.bin}:{os.environ['PATH']}", DIAG=str(env.diagdir))
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"kept {env.diagdir}: it is owned by uid 4242, not {os.getuid()}" in r.stderr
    assert len(list(env.diagdir.iterdir())) == 1
    assert ("diagdir", str(env.diagdir)) in manifest_entries(env)


@pytest.mark.parametrize(
    "entry",
    [
        "diagdir\t{datadir}\t",
        "diagdir\t{state}/synaptics0078-diag2\t",
        "diagdir\t{state}\t",
        "diagdir\t{state}/synaptics0078-diag/\t",
        "diagdir\t{state}/../fprint/synaptics0078-diag\t",
        "dir\t{diagdir}\t",
    ],
)
def test_manifest_naming_a_diagdir_at_a_wrong_path_is_refused(env, entry):
    assert env.run("install").returncode == 0
    with env.manifest.open("a") as f:
        f.write(entry.format(datadir=env.datadir, state=env.state, diagdir=env.diagdir) + "\n")
    before = env.snapshot()
    for args in (("undo", "--yes-delete-diag"), ("install", "--diag", "--yes-diag")):
        r = env.run(*args)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "which install never writes; refusing to act on it" in r.stderr
        assert env.snapshot() == before and env.systemctl_calls() == []


# ------------------------------------------------------------------ status of --diag


def test_status_shows_diag_on_off_and_the_count(env):
    r = env.run("status")
    assert "diag (raw image dumps, install --diag)" in r.stdout and "  off\n" in r.stdout
    assert "  no directory" in r.stdout
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(5)
    r = env.run("status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "  on: the drop-in sets FP_SYNAPTICS0078_DUMP_DIR" in r.stdout
    assert f"  files: 5 in {env.diagdir} (mode 700" in r.stdout
    assert env.run("install").returncode == 0
    r = env.run("status")
    assert "  off\n" in r.stdout and "  files: 5 in " in r.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_status_needs_sudo_to_count_diag_files(env):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(2)
    env.state.chmod(0o600)  # as /var/lib/fprint (0700, root) looks to a normal user
    try:
        r = env.run("status")
    finally:
        env.state.chmod(0o700)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "  on: the drop-in sets" in r.stdout and "files: needs sudo to count" in r.stdout


# ------------------------------------------------------------------ undo --keep-data (A6 hand-over)


def kept_tree(e: Env) -> dict[str, tuple]:
    """Everything under /var/lib/fprint: the data, the prints and the diag images."""
    return {p: v for p, v in e.snapshot().items() if p.startswith(str(e.state) + "/")}


@pytest.mark.parametrize("state_created", [False, True])
def test_handover_keeps_data_prints_and_diag_and_removes_the_rest(env, state_created):
    if state_created:
        env.state.rmdir()  # install creates and records /var/lib/fprint
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    dumps = env.add_dumps(3)
    ours = env.add_print("alice", "synaptics0078")
    other = env.add_print("alice", "goodix")
    env.install_package()
    before = kept_tree(env)
    r = env.run("undo", "--keep-data", **diag_env(env))
    assert r.returncode == 0, r.stdout + r.stderr
    # Kept byte for byte: data dir and files, prints, diag images (and their modes).
    assert kept_tree(env) == before
    assert env.datadir.is_dir() and all(p.exists() for p in [*dumps, ours, other])
    # Removed: drop-in (and the dir install created), lib, link, lib dir with the manifest.
    assert not env.dropin.exists() and not env.dropin_dir.exists()
    assert not env.lib.exists() and not env.link.is_symlink() and not env.libdir.exists()
    # The drop-in goes first, then daemon-reload, then the stop while the lib is still there.
    calls = [c for c in env.calls() if c.startswith("systemctl ")]
    assert [c.split("|")[0] for c in calls] == ["systemctl daemon-reload", "systemctl stop fprintd.service"]
    assert calls[1].split("|")[1].split() == ["0", "1", "1", "1"]
    out = r.stdout
    for name in ("module.bin", f"program-{SERIAL}.bin"):
        assert f"ok    kept {env.datadir / name} (as installed; handed over)" in out
    assert f"ok    kept {env.datadir} (handed over)" in out
    assert (f"ok    kept {env.state} (handed over)" in out) == state_created
    assert "synaptics0078-setup status (from the package) checks them" in out
    assert "enrolled prints of the synaptics0078 driver: kept (1 file(s)" in out and "[y/N]" not in out
    assert f"3 file(s) in {env.diagdir}, kept" in out
    assert f"delete them later with: sudo rm -r --one-file-system -- {env.diagdir}\n" in out
    assert "--yes-delete-diag" not in out
    assert "D-Bus starts it on demand with the packaged libfprint" in out
    assert "hand-over done" in out and "WARN" not in r.stderr


def test_handover_warns_about_a_changed_data_file_and_keeps_it(env):
    assert env.run("install").returncode == 0
    (env.datadir / "module.bin").write_bytes(b"changed")
    env.install_package()
    r = env.run("undo", "--keep-data")
    assert r.returncode == 0, r.stdout + r.stderr
    module = env.datadir / "module.bin"
    assert f"WARN  kept {module}: it changed since install (sha256 differs from the manifest)" in r.stderr
    assert module.read_bytes() == b"changed"
    assert (env.datadir / f"program-{SERIAL}.bin").read_bytes() == PROGRAM
    assert not env.libdir.exists() and not env.dropin.exists() and "hand-over done" in r.stdout


@pytest.mark.parametrize("system_lib", ["stock", "absent"])
def test_handover_refuses_before_the_package_is_installed(env, system_lib):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(1)
    if system_lib == "absent":
        env.system_lib.unlink()
    before = env.snapshot()
    for extra in ((), ("--dry-run",)):
        r = env.run("undo", "--keep-data", *extra)
        assert r.returncode == 1, r.stdout + r.stderr
        assert f"FAILED: {env.system_lib} has no synaptics0078 driver" in r.stderr
        assert "nothing was changed" in r.stderr and "install the package first" in r.stderr
        assert "+ " not in r.stdout
        assert env.snapshot() == before and env.systemctl_calls() == []


@pytest.mark.parametrize(
    "args",
    [
        ("undo", "--keep-data", "--yes-delete-prints"),
        ("undo", "--yes-delete-diag", "--keep-data"),
        ("install", "--keep-data"),
        ("status", "--keep-data"),
    ],
)
def test_handover_usage_errors(env, args):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_print("alice", "synaptics0078")
    env.add_dumps(1)
    env.install_package()
    before = env.snapshot()
    r = env.run(*args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "cannot be combined" in r.stderr or "only applies to undo" in r.stderr
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_handover_dry_run_changes_nothing(env):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(2)
    env.add_print("alice", "synaptics0078")
    env.install_package()
    before = env.snapshot()
    r = env.run("undo", "--keep-data", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dry run: nothing is changed" in r.stdout
    assert f"+ rm -f -- {env.dropin}" in r.stdout and f"+ rm -f -- {env.lib}" in r.stdout
    assert f"+ rmdir -- {env.libdir}" in r.stdout and "systemctl stop fprintd.service" in r.stdout
    assert f"kept {env.datadir / 'module.bin'}" in r.stdout and "2 file(s) in " in r.stdout
    assert f"rm -f -- {env.datadir}" not in r.stdout and f"rmdir -- {env.datadir}" not in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_handover_twice_says_already_undone(env):
    assert env.run("install").returncode == 0
    env.install_package()
    assert env.run("undo", "--keep-data").returncode == 0
    before = env.snapshot()
    r = env.run("undo", "--keep-data")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already undone" in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_handover_after_an_undo_that_kept_the_diag_dir(env):
    # A manifest that lists only the lib dir and the kept diag dir: released, then all gone.
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    dumps = env.add_dumps(2)
    assert env.run("undo", "--keep-diag").returncode == 0
    env.install_package()
    r = env.run("undo", "--keep-data")
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(p.exists() for p in dumps) and not env.libdir.exists()
    assert "already none: the manifest records no data files" in r.stdout
    assert env.systemctl_calls() == []  # nothing fprintd uses was left


def test_handover_keeps_a_changed_lib_and_says_incomplete(env):
    # The lib's own checks are undo's: a lib replaced since install stays, with its record.
    assert env.run("install").returncode == 0
    env.lib.write_bytes(b"hand-replaced lib")
    env.install_package()
    r = env.run("undo", "--keep-data")
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"WARN  kept {env.lib}: it changed since install" in r.stderr and "hand-over incomplete" in r.stdout
    assert env.lib.read_bytes() == b"hand-replaced lib" and env.datadir.is_dir()
    assert sorted(manifest_entries(env)) == sorted([("dir", str(env.libdir)), ("file", str(env.lib))])


def data_entries(e: Env) -> list[tuple[str, str]]:
    """The manifest's records of the data files, the data dir and the state dir."""
    return [(t, p) for t, p in manifest_entries(e) if p == str(e.state) or p.startswith(str(e.state) + "/")]


@pytest.mark.parametrize("failing", ["daemon-reload", "stop"])
@pytest.mark.parametrize("then", [("undo",), ("undo", "--keep-data")])
def test_handover_with_a_failing_systemctl_keeps_the_data_on_a_rerun(env, failing, then):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(1)
    env.install_package()
    before = kept_tree(env)
    assert data_entries(env)
    r = env.run("undo", "--keep-data", STUB_FAIL=failing)
    assert r.returncode == 2, r.stdout + r.stderr
    assert f"FAILED: systemctl {failing}" in r.stderr and "failed (exit 5)" in r.stderr
    assert "undo --keep-data again: it is safe to re-run" in r.stderr
    # The data files left the manifest on disk before the drop-in went; nothing else changed.
    assert kept_tree(env) == before and not env.dropin.exists()
    assert data_entries(env) == [] and ("diagdir", str(env.diagdir)) not in manifest_entries(env)
    assert env.lib.exists() and env.link.is_symlink()
    # A later plain undo (or the re-run) removes the rest and leaves the handed-over files.
    r = env.run(*then)
    assert r.returncode == 0, r.stdout + r.stderr
    assert kept_tree(env) == before and not env.libdir.exists()
    assert env.systemctl_calls() == ["systemctl daemon-reload", "systemctl stop fprintd.service"]


@pytest.mark.parametrize("args", [(), ("--yes-delete-prints",), ("--yes-delete-diag",)])
def test_plain_undo_refuses_once_the_package_is_installed(env, args):
    assert env.run("install", "--diag", "--yes-diag").returncode == 0
    env.add_dumps(1)
    env.add_print("alice", "synaptics0078")
    env.install_package()
    before = env.snapshot()
    r = env.run("undo", *args)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAILED: libfprint-sigfm-0078 is installed and uses these data files; use undo --keep-data" in r.stderr
    assert "+ " not in r.stdout and env.snapshot() == before and env.systemctl_calls() == []
    # A dry run still shows the plan; the hand-over then works.
    assert env.run("undo", "--dry-run", *args).returncode == 0 and env.snapshot() == before
    assert env.run("undo", "--keep-data").returncode == 0 and (env.datadir / "module.bin").exists()


def test_handover_rerun_with_only_dirs_left_does_not_stop_fprintd(env):
    # Another .conf keeps the drop-in dir (and so the lib dir with the manifest) in place.
    assert env.run("install").returncode == 0
    (env.dropin_dir / "other.conf").write_text("[Service]\n")
    env.install_package()
    r = env.run("undo", "--keep-data")
    assert r.returncode == 1, r.stdout + r.stderr
    assert sorted(manifest_entries(env)) == sorted([("dir", str(env.dropin_dir)), ("dir", str(env.libdir))])
    r = env.run("undo", "--keep-data")
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"left {env.dropin_dir} in place" in r.stdout and "hand-over incomplete" in r.stdout
    assert env.systemctl_calls() == [] and "fprintd is stopped" not in r.stdout


def test_handover_dry_run_without_access_does_not_claim_no_prints(env):
    assert env.run("install").returncode == 0
    env.add_print("alice", "synaptics0078")
    env.install_package()
    before = env.snapshot()
    env.state.chmod(0o600)  # as /var/lib/fprint (0700, root) looks to a normal user
    try:
        r = env.run("undo", "--keep-data", "--dry-run")
    finally:
        env.state.chmod(0o700)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "enrolled prints of the synaptics0078 driver: not checked (needs root)" in r.stdout
    assert "none: no enrolled prints" not in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


# ------------------------------------------------------------------ check-lib


def nm_symbols(p: Path) -> set[str]:
    out = subprocess.run(["nm", "-D", "--defined-only", str(p)], capture_output=True, text=True, check=True).stdout
    return {ln.split()[2] for ln in out.splitlines() if len(ln.split()) >= 3}


def test_check_lib_passes_a_good_build(env):
    before = env.snapshot()
    r = env.run("check-lib", str(env.fork_lib))
    assert r.returncode == 0, r.stdout + r.stderr
    stock, ours = nm_symbols(env.system_lib), nm_symbols(env.fork_lib)
    new = ours - stock
    assert stock <= ours and "fp_image_extract_sigfm_info" in new
    assert "ok    SONAME libfprint-2.so.2, no RPATH/RUNPATH" in r.stdout
    assert (
        f"ok    data dir {SYS_DATA_DIR} compiled in" in r.stdout and "ok    driver synaptics0078 built in" in r.stdout
    )
    counts = f"{len(stock)} in {env.system_lib}, {len(ours)} in {env.fork_lib}, 0 missing, {len(new)} new in"
    assert f"ok    symbols: {counts} {env.fork_lib}\n" in r.stdout
    assert env.snapshot() == before and env.calls() == []


@pytest.mark.parametrize("case", ["missing_symbols", "soname", "rpath", "data_dir", "no_driver", "no_file", "no_stock"])
def test_check_lib_fails_a_bad_build(env, case):
    lib = env.tmp / "package.so"
    build_lib(lib)
    message = {
        "missing_symbols": "fp_device_open",
        "soname": "SONAME 'libfprint-2.so.3'",
        "rpath": "RPATH/RUNPATH",
        "data_dir": "synaptics0078_data_dir",
        "no_driver": "driver synaptics0078 is not built into",
        "no_file": "no file",
        "no_stock": f"no {env.system_lib} to compare",
    }[case]
    if case == "missing_symbols":
        src = env.tmp / "stock.c"
        src.write_text("int fp_context_new (void) { return 0; }\nint fp_device_open (void) { return 0; }\n")
        subprocess.run(
            [CC, "-shared", "-fPIC", "-o", str(env.system_lib), str(src), "-Wl,-soname,libfprint-2.so.2"], check=True
        )
    elif case == "soname":
        build_lib(lib, soname="libfprint-2.so.3")
    elif case == "rpath":
        build_lib(lib, rpath="/home/someone/lib")
    elif case == "data_dir":
        build_lib(lib, data_dir="/usr/local/var/lib/fprint/synaptics0078")
    elif case == "no_driver":
        build_lib(lib, driver=False)
    elif case == "no_file":
        lib.unlink()
    else:
        env.system_lib.unlink()
    before = env.snapshot()
    r = env.run("check-lib", str(lib))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAILED: " in r.stderr and message in r.stderr
    assert "ok    symbols" not in r.stdout
    assert env.snapshot() == before


@pytest.mark.parametrize("args", [("check-lib",), ("check-lib", "a.so", "b.so"), ("check-lib", "--keep-data", "a.so")])
def test_check_lib_usage_errors(env, args):
    r = env.run(*args)
    assert r.returncode == 2, r.stdout + r.stderr


def test_help_names_the_handover_and_check_lib():
    r = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
    assert r.returncode == 0
    assert "undo --keep-data" in r.stdout and "check-lib FILE" in r.stdout and "synaptics0078-setup status" in r.stdout


# ------------------------------------------------------------------ log (packaged install)

DEBUG_ENV_LINE = 'Environment="G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print"\n'
RESTART = ["systemctl daemon-reload", "systemctl try-restart fprintd.service"]


def packaged(e: Env) -> Env:
    """As after the A6 hand-over: the package's lib has the driver, no dev install."""
    e.install_package()
    return e


def test_log_on_writes_the_debug_dropin_and_restarts(env):
    packaged(env)
    r = env.run("log", "on")
    assert r.returncode == 0, r.stdout + r.stderr
    text = env.debug_dropin.read_text()
    lines = text.splitlines(keepends=True)
    assert lines[0].startswith("# Written by fingerprint-06cb-0078/tools/dev/a5-fprintd-dev.sh log on")
    # Only the env line (and the section header) besides the comments.
    assert [ln for ln in lines if not ln.startswith("#")] == ["[Service]\n", DEBUG_ENV_LINE]
    header = "".join(ln for ln in lines if ln.startswith("#"))
    assert "unit serial" in header and "sudo tools/dev/a5-fprintd-dev.sh log off" in header
    assert mode(env.debug_dropin) == 0o644 and mode(env.dropin_dir) == 0o755
    assert not [p for p in env.root.rglob(".*a5-new*")]
    assert f"+ write {env.debug_dropin} (mode 0644" in r.stdout and "+ mv -f -T" in r.stdout
    assert env.systemctl_calls() == RESTART
    assert f"log on: {env.debug_dropin} written; fprintd restarted (if it was running)" in r.stdout
    assert f"sudo {SCRIPT} log off" in r.stdout
    assert not env.dropin.exists() and not env.libdir.exists()
    assert "WARN" not in r.stderr


def test_log_on_and_off_are_idempotent(env):
    packaged(env)
    assert env.run("log", "on").returncode == 0
    before = env.snapshot()
    r = env.run("log", "on", STUB_STATE="active", STUB_START=f"@{int(env.debug_dropin.stat().st_mtime) + 5}")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already on: nothing changed, fprintd not restarted" in r.stdout and "+ " not in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []

    r = env.run("log", "off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not env.debug_dropin.exists()
    assert f"+ rm -f -- {env.debug_dropin}" in r.stdout and env.systemctl_calls() == RESTART
    assert f"log off: {env.debug_dropin} removed" in r.stdout and "journalctl --rotate" in r.stdout
    before = env.snapshot()
    r = env.run("log", "off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already off: nothing changed, fprintd not restarted" in r.stdout and "+ " not in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_log_on_restarts_a_fprintd_started_before_the_dropin(env):
    """The preflight's hint for that case is `log on`: it must restart, though the file is unchanged."""
    packaged(env)
    assert env.run("log", "on").returncode == 0
    before = env.snapshot()
    start = f"@{int(env.debug_dropin.stat().st_mtime) - 60}"
    r = env.run("log", "on", STUB_STATE="active", STUB_START=start)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "it started before" in r.stdout and "log on: fprintd restarted" in r.stdout
    # systemd has the drop-in loaded already: a restart, no daemon-reload.
    assert env.systemctl_calls() == ["systemctl try-restart fprintd.service"] and env.snapshot() == before
    # Not running: nothing to restart (D-Bus starts it with the drop-in).
    r = env.run("log", "on", STUB_STATE="inactive", STUB_START=start)
    assert r.returncode == 0 and env.systemctl_calls() == []


def test_log_on_rewrites_a_wrong_mode_or_an_older_text_of_ours(env):
    packaged(env)
    assert env.run("log", "on").returncode == 0
    want = env.debug_dropin.read_text()
    env.debug_dropin.chmod(0o600)
    r = env.run("log", "on")
    assert r.returncode == 0 and mode(env.debug_dropin) == 0o644 and env.systemctl_calls() == RESTART
    env.debug_dropin.write_text(want.splitlines(keepends=True)[0] + "[Service]\nEnvironment=OLD=1\n")
    r = env.run("log", "on")
    assert r.returncode == 0 and env.debug_dropin.read_text() == want and env.systemctl_calls() == RESTART


@pytest.mark.parametrize("dry", [False, True])
def test_log_on_refuses_while_the_dev_dropin_is_present(env, dry):
    assert env.run("install").returncode == 0
    env.install_package()
    before = env.snapshot()
    r = env.run("log", "on", *(["--dry-run"] if dry else []))
    assert r.returncode == 2, r.stdout + r.stderr
    assert f"FAILED: the dev drop-in {env.dropin} is present" in r.stderr
    assert "log on is for the packaged install only (nothing was changed)" in r.stderr
    assert f"sudo {SCRIPT} undo --keep-data" in r.stderr and f"sudo {SCRIPT} log on" in r.stderr
    assert "+ " not in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_log_off_removes_its_dropin_while_the_dev_dropin_is_present(env):
    """Removing the file log on wrote is safe in any state: off does not refuse."""
    packaged(env)
    assert env.run("log", "on").returncode == 0
    assert env.run("install").returncode == 0  # e.g. back to the dev install for a fix
    dev_text = env.dropin.read_text()
    r = env.run("log", "off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not env.debug_dropin.exists() and env.dropin.read_text() == dev_text
    assert env.systemctl_calls() == RESTART
    assert f"the dev drop-in {env.dropin} still sends the debug lines" in r.stdout
    # Again: nothing to do; the dev drop-in's lines are no reason to restart.
    r = env.run("log", "off")
    assert r.returncode == 0 and "already off: nothing changed" in r.stdout and env.systemctl_calls() == []


@pytest.mark.parametrize("action", ["on", "off"])
def test_log_refuses_a_debug_dropin_it_did_not_write(env, action):
    packaged(env)
    env.dropin_dir.mkdir()
    env.debug_dropin.write_text('[Service]\nEnvironment="G_MESSAGES_DEBUG=all"\n')
    before = env.snapshot()
    r = env.run("log", action)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "exists and was not written by this script" in r.stderr and f"log {action}" in r.stderr
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_log_refuses_a_symlinked_debug_dropin(env):
    packaged(env)
    env.dropin_dir.mkdir()
    target = env.tmp / "elsewhere.conf"
    target.write_text("x\n")
    env.debug_dropin.symlink_to(target)
    for action in ("on", "off"):
        r = env.run("log", action)
        assert r.returncode == 2 and "is a symlink" in r.stderr
    assert target.read_text() == "x\n" and env.systemctl_calls() == []


def test_log_dry_run_changes_nothing(env):
    packaged(env)
    before = env.snapshot()
    r = env.run("log", "on", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dry run: nothing is changed" in r.stdout and f"+ write {env.debug_dropin}" in r.stdout
    assert "systemctl daemon-reload\n" in r.stdout and "systemctl try-restart fprintd.service\n" in r.stdout
    assert "log on (dry run): would write" in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []
    assert env.run("log", "on").returncode == 0
    before = env.snapshot()
    r = env.run("log", "off", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"+ rm -f -- {env.debug_dropin}" in r.stdout and "log off (dry run): would remove" in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_log_on_warns_without_the_package(env):
    r = env.run("log", "on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "has no synaptics0078 driver: the libfprint-sigfm-0078 package is not installed" in r.stderr
    assert env.debug_dropin.exists()


def test_log_status_is_read_only(env):
    packaged(env)
    before = env.snapshot()
    r = env.run("log", "status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "has the synaptics0078 driver" in r.stdout and "  off: absent" in r.stdout
    assert f"turn it on: sudo {SCRIPT} log on" in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []

    assert env.run("log", "on").returncode == 0
    before = env.snapshot()
    r = env.run("log", "status")
    assert r.returncode == 0 and "  on (the journal lines include the unit serial" in r.stdout
    assert f"sudo {SCRIPT} log off" in r.stdout and "WARN" not in r.stdout
    start = f"@{int(env.debug_dropin.stat().st_mtime) - 60}"
    r = env.run("log", "status", STUB_STATE="active", STUB_START=start)
    assert r.returncode == 0 and "WARN fprintd started before the drop-in was written" in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_log_status_names_the_dev_dropin(env):
    assert env.run("install").returncode == 0
    before = env.snapshot()
    r = env.run("log", "status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"dev drop-in: {env.dropin} is present" in r.stdout and "has no synaptics0078 driver" in r.stdout
    assert "turn it on" not in r.stdout
    assert env.snapshot() == before and env.systemctl_calls() == []


def test_log_on_after_the_handover(env):
    """The A6 sequence: dev install, package, undo --keep-data, then log on; undo leaves the debug drop-in alone."""
    assert env.run("install").returncode == 0
    env.install_package()
    assert env.run("undo", "--keep-data").returncode == 0
    r = env.run("log", "on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.debug_dropin.read_text().endswith(DEBUG_ENV_LINE) and env.systemctl_calls() == RESTART


@pytest.mark.parametrize(
    "args, message",
    [
        (("log",), "log needs on, off or status"),
        (("log", "maybe"), "unknown argument: maybe"),
        (("log", "on", "off"), "unknown argument: off"),
        (("log", "on", "--keep-data"), "--keep-data only applies to undo"),
        (("log", "on", "--diag"), "--diag only applies to install"),
        (("log", "status", "install"), "more than one subcommand"),
    ],
)
def test_log_usage_errors(env, args, message):
    packaged(env)
    before = env.snapshot()
    r = env.run(*args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert message in r.stderr
    assert env.snapshot() == before and env.systemctl_calls() == []


@pytest.mark.skipif(os.geteuid() == 0, reason="must run as a normal user")
@pytest.mark.parametrize("action", ["on", "off"])
def test_log_refuses_without_root_and_names_the_sudo_command(action):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("A5_", "LIBFPRINT_FORK"))}
    r = subprocess.run(["bash", str(SCRIPT), "log", action], capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 2
    assert f"sudo {SCRIPT} log {action}" in r.stderr and "nothing was changed" in r.stderr
    assert "+ " not in r.stdout


def test_help_names_log():
    r = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
    assert r.returncode == 0
    assert "log on|off" in r.stdout and "log status" in r.stdout and "synaptics0078-debug.conf" in r.stdout


def fprintd_environ(e: Env, pid: int, debug: bool) -> dict[str, str]:
    """A running fprintd (pid PID) whose /proc environ has the debug lines or not; its stub env."""
    d = e.root / "proc" / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    env = b"LANG=C\0" + (b"G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print\0" if debug else b"")
    (d / "environ").write_bytes(env + b"PATH=/usr/bin\0")
    return {"STUB_PID": str(pid), "STUB_STATE": "active"}


@pytest.mark.parametrize("failing", ["daemon-reload", "try-restart"])
def test_log_on_with_a_failing_systemctl_is_repaired_by_a_rerun(env, failing):
    packaged(env)
    running = fprintd_environ(env, 4242, debug=False)  # started before log on
    r = env.run("log", "on", STUB_FAIL=failing, **running)
    assert r.returncode == 2, r.stdout + r.stderr
    assert f"FAILED: systemctl {failing}" in r.stderr and "is written, but fprintd may still run without it" in r.stderr
    assert f"then run sudo {SCRIPT} log on again" in r.stderr and "Traceback" not in r.stderr
    assert env.debug_dropin.exists()
    # The re-run finds the drop-in written and finishes the job.
    r = env.run("log", "on", **running)
    assert r.returncode == 0, r.stdout + r.stderr
    want = RESTART if failing == "daemon-reload" else ["systemctl try-restart fprintd.service"]
    assert env.systemctl_calls() == want and "already on: nothing changed" not in r.stdout
    # Now fprintd runs with the lines: nothing left to do.
    running = fprintd_environ(env, 4242, debug=True)
    r = env.run("log", "on", **running)
    assert r.returncode == 0 and "already on: nothing changed" in r.stdout and env.systemctl_calls() == []


@pytest.mark.parametrize("failing", ["daemon-reload", "try-restart"])
def test_log_off_with_a_failing_systemctl_is_repaired_by_a_rerun(env, failing):
    packaged(env)
    assert env.run("log", "on").returncode == 0
    os.utime(env.dropin_dir, (1_000, 1_000))
    # fprintd started after log on, with the lines; the removal comes after its start.
    running = {**fprintd_environ(env, 4242, debug=True), "STUB_START": "@2000"}
    r = env.run("log", "off", STUB_FAIL=failing, **running)
    assert r.returncode == 2, r.stdout + r.stderr
    assert f"FAILED: systemctl {failing}" in r.stderr and "fprintd may still write the debug lines" in r.stderr
    assert f"then run sudo {SCRIPT} log off again" in r.stderr
    assert not env.debug_dropin.exists()
    r = env.run("log", "off", **running)
    assert r.returncode == 0, r.stdout + r.stderr
    want = RESTART if failing == "daemon-reload" else ["systemctl try-restart fprintd.service"]
    assert env.systemctl_calls() == want and "already off: nothing changed" not in r.stdout
    running = fprintd_environ(env, 4242, debug=False)
    r = env.run("log", "off", **running)
    assert r.returncode == 0 and "already off: nothing changed" in r.stdout and env.systemctl_calls() == []


def test_log_off_leaves_a_fprintd_started_after_the_removal(env):
    """Debug lines in a fprintd started after the removal come from elsewhere: no restart loop."""
    packaged(env)
    assert env.run("log", "on").returncode == 0
    assert env.run("log", "off").returncode == 0
    after = f"@{env.dropin_dir.stat().st_mtime + 5:.6f}"
    r = env.run("log", "off", **fprintd_environ(env, 4242, debug=True), STUB_START=after)
    assert r.returncode == 0 and "already off: nothing changed" in r.stdout and env.systemctl_calls() == []


def mtime_ns(p: Path, ns: int) -> str:
    """Set P's mtime to NS (ns since the epoch); returns it as systemctl's @SECONDS.MICROSECONDS."""
    os.utime(p, ns=(ns, ns))
    return f"@{ns // 1_000_000_000}.{ns // 1000 % 1_000_000:06d}"


def test_log_on_staleness_is_sub_second(env):
    packaged(env)
    assert env.run("log", "on").returncode == 0
    t = 1_800_000_000_500_000_000  # .5 s past a whole second
    at = mtime_ns(env.debug_dropin, t)
    # Started at the very microsecond of the write: not stale.
    r = env.run("log", "on", STUB_STATE="active", STUB_START=at)
    assert r.returncode == 0 and "already on: nothing changed" in r.stdout and env.systemctl_calls() == []
    # Started in the same second, but 0.4 s before the write: stale, restarted.
    r = env.run("log", "on", STUB_STATE="active", STUB_START="@1800000000.100000")
    assert r.returncode == 0 and env.systemctl_calls() == ["systemctl try-restart fprintd.service"]
    r = env.run("log", "status", STUB_STATE="active", STUB_START="@1800000000.100000")
    assert "WARN fprintd started before the drop-in was written" in r.stdout


def test_log_status_names_a_drop_in_systemd_has_not_loaded(env):
    packaged(env)
    env.dropin_dir.mkdir()
    r = env.run("log", "on", STUB_FAIL="daemon-reload")
    assert r.returncode == 2
    r = env.run("log", "status")
    assert r.returncode == 0 and "WARN systemd has not loaded it" in r.stdout and env.systemctl_calls() == []


@pytest.mark.parametrize("action", ["on", "off"])
def test_log_refuses_a_symlinked_dropin_dir(env, action):
    packaged(env)
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    env.dropin_dir.symlink_to(elsewhere)
    r = env.run("log", action)
    assert r.returncode == 2 and f"{env.dropin_dir} is a symlink" in r.stderr
    assert list(elsewhere.iterdir()) == [] and env.systemctl_calls() == []


@pytest.mark.parametrize("action", ["on", "off"])
def test_log_refuses_a_foreign_owned_dropin_dir(env, action):
    # As in test_undo_refuses_a_foreign_owned_diag_dir: a stat stub reports another owner.
    packaged(env)
    env.dropin_dir.mkdir()
    stub = env.bin / "stat"
    stub.write_text(
        '#!/usr/bin/env bash\nif [ "$2" = %u ] && [ "${@: -1}" = "$DIR" ]; then echo 4242; exit 0; fi\n'
        'exec /usr/bin/stat "$@"\n'
    )
    stub.chmod(0o755)
    r = env.run("log", action, PATH=f"{env.bin}:{os.environ['PATH']}", DIR=str(env.dropin_dir))
    assert r.returncode == 2, r.stdout + r.stderr
    assert f"{env.dropin_dir} is not owned by uid {os.getuid()}" in r.stderr
    assert list(env.dropin_dir.iterdir()) == [] and env.systemctl_calls() == []


@pytest.mark.parametrize("action", ["on", "off"])
def test_log_refuses_a_debug_dropin_that_is_a_directory(env, action):
    packaged(env)
    env.debug_dropin.mkdir(parents=True)
    before = env.snapshot()
    r = env.run("log", action)
    assert r.returncode == 2 and f"{env.debug_dropin} exists and is not a regular file" in r.stderr
    assert env.snapshot() == before and env.systemctl_calls() == []
    r = env.run("log", "status")
    assert r.returncode == 0 and "WARN not a regular file" in r.stdout
