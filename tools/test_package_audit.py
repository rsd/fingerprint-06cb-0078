# SPDX-License-Identifier: MIT
"""Audit of a built libfprint-sigfm-0078 package (research/steps/b5-setup-design.md, test 8).

The package is named by SYNAPTICS0078_PKG (a .pkg.tar.zst); every test skips when it
is unset or missing. The archive is read in memory (Python's tarfile with zstd, or
`zstd -dc` to stdout): nothing is extracted to disk.

What is checked:
- the file list: no `.bin` file, no tests/ in the Python package, the setup script,
  the Python package (the same modules as this tree's synaptics0078/), the licence;
- the pyc files: checked-hash, matching the installed source, recording the installed
  path (no build dir in them);
- with the private vendor DLL (local/b5/x-bsdtar-2019/synaWudfBioUsb.dll, skipped
  without it), read as data only:
  * every file, at stride 1, against every 16-byte window of the capture module (found
    with synaptics0078.vendor.find_module) and of the eight pinned DLL tables the
    template takes (found with synaptics0078.template.locate_chunk);
  * every file that is not an ELF object (this project's setup script, Python sources
    and pyc, licence, install script, the fork's headers, typelib, udev and metainfo
    files, the package metadata), against every 16-byte window of the whole DLL at
    stride 1: their windows are indexed and the DLL is scanned against them;
- this project's code inside the fork's ELF objects, through its source: the driver files
  (libfprint/drivers/synaptics0078*.c/.h) of the libfprint fork (LIBFPRINT_FORK, default
  ../libfprint; skipped without it or without the commit) at the commit the PKGBUILD pins
  (`_commit=`), read with `git show` (never the work tree), their C literals decoded into
  byte runs (c_byte_runs: strings with their escapes, brace lists of byte values, hex
  text), against the module and the tables as above and against the whole DLL as below;
  and the package's pkgver names that commit, so the source checked is the one built;
- the unit serial, with tools/test_no_serial.py's forms, in every member name and file.

Windows with fewer than MIN_DISTINCT distinct byte values (zero padding, fill runs) are
not counted anywhere. For the whole-DLL check, two more kinds are not counted: windows
with more than MAX_ZEROS zero bytes (sparse little-endian integers, which every binary
format holds: marshal's ints in a pyc, PE and typelib headers), and windows of printable
text: the DLL's strings (firmware ids such as "SYN 86C  FM03290001", debug names) are
names the code legitimately spells; the module and the tables hold no such window.
Text sources are therefore covered through their compiled and decoded forms: a bytes
literal is stored raw in its pyc (the setup script, which ships without one, is compiled
in memory: compile(), never run), and every run of 16 or more hex byte pairs in this
project's sources ("0a1b...", "0x0a, 0x1b, ...", "\\x0a\\x1b...") is decoded and indexed
too. Not decoded: other encodings (base64, decimal lists, multi-byte integer literals).

Not covered: the fork's ELF objects against the whole DLL. Upstream libfprint's own
vfs7552 driver (libfprint/drivers/vfs7552_proto.h, upstream since 2020, shipped by Arch's
libfprint too) carries Validity init data that shares hundreds of 16-byte windows with
this DLL, and compiled x86 code shares compiler idioms with it, so a whole-DLL overlap
there says nothing about this project. That same upstream data holds the one window of a
pinned table the fork's library contains, allowed by its sha256 in UPSTREAM_SHARED.
Subtracting an upstream-only build's windows would need such a build offline, and the
libfprint installed here is this package's own; so the fork-driver source check is what
covers this project's code inside libfprint-2.so. The driver's source windows are
filtered as the non-ELF files' are (counted, dense, not text: a firmware id or name the
driver spells as the DLL does is not counted). Not decoded there: multi-byte integer
arrays, byte values computed by expressions, literals outside braces (call arguments).
The fork's other changes (the SIGFM matcher, build files) are not checked.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import marshal
import os
import re
import stat
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest
import test_no_serial as tns

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from synaptics0078 import pins, template, vendor

PKG_ENV = "SYNAPTICS0078_PKG"
DLL = REPO / "local" / "b5" / "x-bsdtar-2019" / "synaWudfBioUsb.dll"
PKGNAME = "libfprint-sigfm-0078"
LIBDIR = "usr/lib/synaptics0078"
PYPKG = f"{LIBDIR}/synaptics0078"
SETUP = "usr/bin/synaptics0078-setup"
LICENSE = f"usr/share/licenses/{PKGNAME}/LICENSE-synaptics0078"
FORK_LIB_PREFIX = "usr/lib/libfprint-2.so"
FORK_ENV = "LIBFPRINT_FORK"
PKGBUILD = REPO / "packaging" / "arch" / PKGNAME / "PKGBUILD"
DRIVER_DIR = "libfprint/drivers"
WINDOW = 16
MIN_DISTINCT = 4
MAX_ZEROS = 8
MAX_GUNZIP = 64 << 20
TEXT = frozenset(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}
# 16 or more hex byte pairs, each optionally 0x- or \\x-prefixed, up to 3 separators apart.
HEX_RUN = re.compile(rb"(?:(?:0[xX]|\\[xX])?[0-9a-fA-F]{2}[\s,:_.-]{0,3}){16,}")
HEX_PAIR = re.compile(rb"(?:0[xX]|\\[xX])?([0-9a-fA-F]{2})")

# sha256 of the 16-byte windows of a pinned DLL table that upstream libfprint ships,
# allowed only in the fork's library (FORK_LIB_PREFIX). A hash ships no bytes.
UPSTREAM_SHARED = {
    # list16_92, data offset 72: four register/value pairs that the vfs7552 init
    # sequence (vfs7552_proto.h, upstream e0de8c67, 2020) also writes.
    "8c9a835433d44e6915558b04acb2f7b110fbe2c8a6bf5f9bdc56608381e39fdb": "upstream vfs7552_proto.h",
}


# ------------------------------------------------------------------ reading


def _open_package(path: Path) -> tarfile.TarFile:
    try:
        return tarfile.open(path, "r:zst")
    except (tarfile.CompressionError, ValueError):
        raw = subprocess.run(["/usr/bin/zstd", "-dcq", "--", str(path)], capture_output=True, check=True).stdout
        return tarfile.open(fileobj=io.BytesIO(raw), mode="r:")


class Package:
    """The members of a package archive and the bytes of its regular files, in memory."""

    def __init__(self, tf: tarfile.TarFile):
        self.members = {m.name.removeprefix("./"): m for m in tf.getmembers()}
        self.files = {name: tf.extractfile(m).read() for name, m in self.members.items() if m.isreg()}

    @classmethod
    def load(cls, path: Path) -> Package:
        with _open_package(path) as tf:
            return cls(tf)

    def contents(self, name: str):
        """The file's bytes, plus its gunzipped bytes when it is gzip (.MTREE, man pages)."""
        data = self.files[name]
        yield data
        if data[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
                yield g.read(MAX_GUNZIP)

    def under(self, prefix: str) -> list[str]:
        return sorted(n for n in self.members if n == prefix or n.startswith(prefix + "/"))


@pytest.fixture(scope="module")
def pkg() -> Package:
    value = os.environ.get(PKG_ENV)
    if not value:
        pytest.skip(f"{PKG_ENV} is not set (path to a built {PKGNAME} .pkg.tar.zst)")
    path = Path(value)
    if not path.is_file():
        pytest.skip(f"{PKG_ENV} names no file")
    return Package.load(path)


class VendorRef:
    """The reference DLL's bytes, with a repr that shows none of them (pytest -l)."""

    def __init__(self, data: bytes):
        self.data = data

    def __repr__(self) -> str:
        return f"<vendor DLL, {len(self.data)} bytes>"


@pytest.fixture(scope="module")
def dll() -> VendorRef:
    if not DLL.is_file():
        pytest.skip("no private vendor DLL in local/b5")
    return VendorRef(DLL.read_bytes())


# ------------------------------------------------------------------ windows


def _counted(w: bytes) -> bool:
    return len(set(w)) >= MIN_DISTINCT


def _dense(w: bytes) -> bool:
    return w.count(0) <= MAX_ZEROS


def _is_text(w: bytes) -> bool:
    return all(b in TEXT for b in w)


def windows(data: bytes):
    """(offset, window) for every 16-byte window of data (stride 1)."""
    mv = memoryview(data)
    for i in range(len(data) - WINDOW + 1):
        yield i, bytes(mv[i : i + WINDOW])


def vendor_sources(dll: bytes) -> dict[str, bytes]:
    """{name: bytes} of the module and the pinned DLL tables; AssertionError when one is missing."""
    off = vendor.find_module(dll)
    assert off is not None, "the pinned module is not in the reference DLL"
    sources = {"module": dll[off : off + pins.MODULE_LEN]}
    for chunk in pins.DLL_CHUNKS:
        sources[chunk.name] = template.locate_chunk(dll, chunk)
    return sources


def window_index(sources: dict[str, bytes]) -> dict[bytes, str]:
    """{window: "source+offset"} for every counted window of the sources, stride 1."""
    index = {}
    for name, data in sources.items():
        for i, w in windows(data):
            if _counted(w):
                index.setdefault(w, f"{name}+{i}")
    return index


def is_elf(data: bytes) -> bool:
    return data[:4] == b"\x7fELF"


# ------------------------------------------------------------------ file list


def test_no_bin_file(pkg):
    assert [n for n in pkg.members if n.casefold().endswith(".bin")] == []


def test_python_package_has_no_tests(pkg):
    assert [n for n in pkg.under(LIBDIR) if "tests" in n.split("/")[3:]] == []


def test_setup_script_and_licence_are_present(pkg):
    for name, mode in ((SETUP, 0o755), (LICENSE, 0o644)):
        m = pkg.members.get(name)
        assert m is not None and m.isreg(), name
        assert stat.S_IMODE(m.mode) == mode, name
    assert pkg.files[LICENSE] == (REPO / "LICENSE").read_bytes()
    assert pkg.files[LICENSE].startswith(b"MIT License")
    licenses = re.findall(rb"^license = (.+)$", pkg.files[".PKGINFO"], re.MULTILINE)
    assert sorted(licenses) == [b"LGPL-2.1-or-later", b"MIT"]
    assert pkg.files[SETUP].startswith(b"#!/usr/bin/python3 ")


def test_python_package_matches_the_tree_and_modes(pkg):
    want = sorted(p.name for p in (REPO / "synaptics0078").glob("*.py"))
    got = sorted(n.rsplit("/", 1)[1] for n in pkg.under(PYPKG) if n.rsplit("/", 1)[0] == PYPKG and n.endswith(".py"))
    assert got == want
    for name in pkg.under(LIBDIR):
        m = pkg.members[name]
        assert (m.isdir() and stat.S_IMODE(m.mode) == 0o755) or (m.isreg() and stat.S_IMODE(m.mode) == 0o644), name
        assert m.uname == "root" and m.gname == "root", name


def _code_filenames(code: types.CodeType):
    yield code.co_filename
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _code_filenames(const)


def test_pyc_are_checked_hash_and_record_the_installed_path(pkg):
    sources = [n for n in pkg.under(PYPKG) if n.endswith(".py")]
    assert sources
    for src in sources:
        folder, base = src.rsplit("/", 1)
        pycs = [n for n in pkg.under(f"{folder}/__pycache__") if n.rsplit("/", 1)[1].startswith(base[:-3] + ".")]
        assert pycs, f"no pyc for {src}"
        for pyc in pycs:
            data = pkg.files[pyc]
            flags = int.from_bytes(data[4:8], "little")
            assert flags == 0b11, f"{pyc}: not a checked-hash pyc"
            assert data[8:16] == importlib.util.source_hash(pkg.files[src]), f"{pyc}: not of the installed source"
            if data[:4] != importlib.util.MAGIC_NUMBER:
                continue  # built by another Python: marshal cannot read it here
            code = marshal.loads(data[16:])  # the package's own build output, data only
            assert set(_code_filenames(code)) == {"/" + src}, pyc


# ------------------------------------------------------------------ vendor bytes


def source_hits(pkg: Package, index: dict[bytes, str], allowed=UPSTREAM_SHARED) -> dict[str, list[str]]:
    """{member: ["@offset=source+offset", ...]} for every indexed window found in a file.

    A window whose sha256 is in `allowed` is not counted in the fork's library. The
    result names offsets only, never bytes.
    """
    hits = {}
    for name in sorted(pkg.files):
        for data in pkg.contents(name):
            for i, w in windows(data):
                if w not in index:
                    continue
                if name.startswith(FORK_LIB_PREFIX) and hashlib.sha256(w).hexdigest() in allowed:
                    continue
                hits.setdefault(name, []).append(f"@{i:#x}={index[w]}")
    return hits


def decoded_hex(text: bytes) -> list[bytes]:
    """The bytes of every run of 16 or more hex byte pairs in text."""
    return [bytes.fromhex(b"".join(HEX_PAIR.findall(m.group())).decode()) for m in HEX_RUN.finditer(text)]


def project_forms(pkg: Package) -> dict[str, list[bytes]]:
    """{member: [derived bytes]} for this project's text sources: hex runs, and the setup script compiled."""
    out = {}
    for name in sorted(pkg.files):
        if name == SETUP or (name.startswith(LIBDIR + "/") and name.endswith(".py")):
            data = pkg.files[name]
            out[name] = decoded_hex(data)
            if name == SETUP:
                out[name].append(marshal.dumps(compile(data, "/" + SETUP, "exec", dont_inherit=True)))
    return out


def dll_hits(pkg: Package, dll: bytes) -> tuple[set[str], dict[str, list[str]]]:
    """(the members indexed, {member: ["dll@offset", ...]}) for the non-ELF files against the whole DLL.

    The windows (counted, dense, not text) of every non-ELF file and of the project_forms of
    this project's sources are indexed, then the DLL is scanned at stride 1 against them.
    """
    index = {}
    derived = project_forms(pkg)
    for name in sorted(pkg.files):
        if is_elf(pkg.files[name]):
            continue
        for data in (*pkg.contents(name), *derived.get(name, ())):
            for _, w in windows(data):
                if _counted(w) and _dense(w) and not _is_text(w):
                    index.setdefault(w, set()).add(name)
    hits = {}
    for i, w in windows(dll):
        for name in index.get(w, ()):
            hits.setdefault(name, []).append(f"dll@{i:#x}")
    return set().union(*index.values()), hits


def test_no_file_holds_a_module_or_table_window(pkg, dll):
    assert source_hits(pkg, window_index(vendor_sources(dll.data))) == {}


def test_non_elf_files_share_no_window_with_the_dll(pkg, dll):
    indexed, hits = dll_hits(pkg, dll.data)
    assert SETUP in indexed and any(n.startswith(PYPKG + "/") for n in indexed)
    assert hits == {}


# ------------------------------------------------------------------ the fork's driver, at the pinned commit

C_TOKEN = re.compile(
    rb"""(?P<comment>//[^\n]*|/\*.*?\*/)
    |(?P<string>(?:u8|[uUL])?"(?:\\.|[^"\\\n])*")
    |(?P<char>[uUL]?'(?:\\.|[^'\\\n])+')
    |(?P<number>\.?[0-9](?:[eEpP][+-]|[0-9A-Za-z_.'])*)
    |(?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    |(?P<punct>\S)""",
    re.DOTALL | re.VERBOSE,
)
C_DEFINE = re.compile(rb"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+([^\s/]+)[ \t]*(?:/[/*].*)?$", re.MULTILINE)
C_ESCAPE = re.compile(rb"\\(?:([xX][0-9a-fA-F]+)|([0-7]{1,3})|u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|(.))", re.DOTALL)
C_SIMPLE_ESCAPES = {b"n": 10, b"t": 9, b"r": 13, b"a": 7, b"b": 8, b"f": 12, b"v": 11, b"e": 27}


def _c_int(literal: bytes) -> int | None:
    """The value of a C integer literal (0x, 0b, octal, decimal; suffixes, ' separators), else None."""
    s = literal.replace(b"'", b"").rstrip(b"uUlLzZ").lower()
    try:
        if s.startswith((b"0x", b"0b")):
            return int(s[2:], 16 if s[1:2] == b"x" else 2)
        return int(s, 8 if len(s) > 1 and s.startswith(b"0") else 10)
    except ValueError:
        return None


def _c_unescape(body: bytes) -> bytes:
    """The bytes of a C string or character literal body (between the quotes)."""

    def one(m: re.Match) -> bytes:
        hexa, octal, u4, u8, other = m.groups()
        if hexa or octal:
            return bytes([int(hexa[1:], 16) & 0xFF if hexa else int(octal, 8) & 0xFF])
        if u4 or u8:
            return chr(int(u4 or u8, 16)).encode("utf-8", "surrogatepass")
        return bytes([C_SIMPLE_ESCAPES.get(other, other[0])])

    return C_ESCAPE.sub(one, body)


def c_byte_runs(text: bytes) -> list[bytes]:
    """The byte runs a C source spells: string literals and brace lists of byte values, plus hex runs.

    - adjacent string literals (comments between them allowed) are concatenated and
      decoded, escapes included (\\xNN, octal, \\n..., \\u);
    - in a brace initializer, consecutive elements that are each one byte-valued token
      (an integer literal, a character literal, or a name #defined as one) form a run;
      nested braces continue it (a 2-D byte array is contiguous), any other element
      (a larger value, an expression, a string, a name with no such #define) ends it;
    - decoded_hex() of the raw text adds hex dumps in comments or strings.
    Not decoded: multi-byte integer arrays (guint16/guint32 element values above 255),
    byte values computed by expressions, and literals outside braces (call arguments).
    """
    defines = {}
    for name, value in C_DEFINE.findall(text):
        v = _c_int(value) if value[:1].isdigit() else None
        if v is not None:
            defines[name] = v
    runs: list[bytes] = []
    depth = 0
    string: bytes | None = None  # the pending adjacent string literals
    run = bytearray()  # the pending brace run
    element: list[tuple[str, bytes]] = []  # the tokens of the current brace element

    def end_run():
        nonlocal run
        if run:
            runs.append(bytes(run))
        run = bytearray()

    def end_element():
        if not element:
            return  # "}," or "{{": no element, the run goes on
        value = None
        if len(element) == 1:
            kind, tok = element[0]
            if kind == "number":
                value = _c_int(tok)
            elif kind == "char":
                raw = _c_unescape(tok[tok.index(b"'") + 1 : -1])
                value = raw[0] if len(raw) == 1 else None
            elif kind == "ident":
                value = defines.get(tok)
        if value is not None and 0 <= value <= 0xFF:
            run.append(value)
        else:
            end_run()
        element.clear()

    for m in C_TOKEN.finditer(text):
        kind, tok = m.lastgroup, m.group()
        if kind == "comment":
            continue
        if kind == "string":
            body = _c_unescape(tok[tok.index(b'"') + 1 : -1])
            string = body if string is None else string + body
        elif string is not None:
            runs.append(string)
            string = None
        if depth == 0:
            if tok == b"{":
                depth = 1
            continue
        if tok in (b"{", b"}", b","):
            end_element()
            depth += 1 if tok == b"{" else -1 if tok == b"}" else 0
            if depth == 0:
                end_run()
        else:
            element.append((kind, tok))
    if string is not None:
        runs.append(string)
    end_run()
    return runs + decoded_hex(text)


def fork_dir() -> Path:
    return Path(os.environ.get(FORK_ENV) or REPO.parent / "libfprint")


def pinned_commit() -> str:
    m = re.search(r"^_commit=([0-9a-f]{40})$", PKGBUILD.read_text(), re.MULTILINE)
    assert m, f"no _commit= pin in {PKGBUILD.relative_to(REPO)}"
    return m.group(1)


def _git(fork: Path, *args: str) -> bytes:
    """Read-only git in the fork (rev-parse, ls-tree, show): no index or tree is written."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(["git", "-C", str(fork), *args], capture_output=True, check=True, env=env).stdout


def driver_sources(fork: Path, commit: str) -> dict[str, bytes]:
    """{path: bytes} of the project's driver files in the fork at commit (git show, never the work tree)."""
    names = _git(fork, "ls-tree", "--name-only", commit, DRIVER_DIR + "/").decode().splitlines()
    names = sorted(n for n in names if re.fullmatch(re.escape(DRIVER_DIR) + r"/synaptics0078[^/]*\.[ch]", n))
    assert f"{DRIVER_DIR}/synaptics0078.c" in names, f"no synaptics0078.c at {commit[:12]}"
    return {n: _git(fork, "show", f"{commit}:{n}") for n in names}


@pytest.fixture(scope="module")
def driver() -> dict[str, bytes]:
    fork = fork_dir()
    if not (fork / ".git").exists():
        pytest.skip(f"no libfprint fork checkout at {fork} (set {FORK_ENV})")
    commit = pinned_commit()
    try:
        _git(fork, "rev-parse", "--quiet", "--verify", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError:
        pytest.skip(f"the fork at {fork} has no commit {commit[:12]} (the PKGBUILD pin)")
    return driver_sources(fork, commit)


def driver_hits(sources: dict[str, bytes], dll: bytes, vendor_index: dict[bytes, str]):
    """(the files with a 16-byte run, {file: ["dll@offset" | "source+offset", ...]}) for C sources.

    Each c_byte_runs() run is windowed separately (no window spans two runs). Against the
    module and the pinned tables (vendor_index) every counted window is looked up, as in
    source_hits(); against the whole DLL only the windows dll_hits() counts (counted,
    dense, not text), so names the code spells the way the DLL does are not counted.
    """
    index, hits, indexed = {}, {}, set()
    for name, text in sorted(sources.items()):
        seen = set()  # a window decoded twice (a \\x string is a hex run too) counts once
        for run in c_byte_runs(text):
            for _, w in windows(run):
                indexed.add(name)
                if w in seen or not _counted(w):
                    continue
                seen.add(w)
                if w in vendor_index:
                    hits.setdefault(name, []).append(vendor_index[w])
                if _dense(w) and not _is_text(w):
                    index.setdefault(w, set()).add(name)
    for i, w in windows(dll):
        for name in index.get(w, ()):
            hits.setdefault(name, []).append(f"dll@{i:#x}")
    return indexed, hits


def test_fork_driver_source_shares_no_window_with_the_dll(driver, dll):
    indexed, hits = driver_hits(driver, dll.data, window_index(vendor_sources(dll.data)))
    assert f"{DRIVER_DIR}/synaptics0078.c" in indexed
    assert hits == {}


def test_package_is_built_from_the_pinned_fork_commit(pkg):
    m = re.search(rb"^pkgver = \S+\.g([0-9a-f]{7,})-\d+$", pkg.files[".PKGINFO"], re.MULTILINE)
    assert m, "no git pkgver in .PKGINFO"
    assert pinned_commit().startswith(m.group(1).decode())


# ------------------------------------------------------------------ the audit itself, on synthetic bytes


def _rand(seed: str, n: int) -> bytes:
    out = b""
    while len(out) < n:
        out += hashlib.sha256(f"{seed}/{len(out)}".encode()).digest()
    return out[:n]


def _synthetic_package(files: dict[str, bytes]) -> Package:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r:") as tf:
        return Package(tf)


SYN_MODULE = _rand("module", 600)  # synthetic, not vendor bytes
SYN_TABLE = _rand("table", 64)
SPARSE = bytes.fromhex("01000000060000000300000009000000")  # four small u32: in any binary format
SYN_DLL = (
    _rand("dll head", 300) + SYN_MODULE + _rand("dll mid", 200) + SYN_TABLE + b"debug name: Timeslot Table\0" + SPARSE
)


def test_audit_finds_a_planted_module_or_table_window():
    index = window_index({"module": SYN_MODULE, "table": SYN_TABLE})
    allowed = {hashlib.sha256(SYN_TABLE[8:24]).hexdigest(): "test"}
    pkg = _synthetic_package(
        {
            f"{PYPKG}/a.py": b"x = 1\n" + SYN_MODULE[100:116] + b"\n",
            ".MTREE": gzip.compress(b"#mtree\n" + SYN_TABLE[30:50]),
            "usr/lib/libfprint-2.so.2.0.0": b"\x7fELF" + bytes(32) + SYN_TABLE[8:24] + bytes(16),
            "usr/include/x.h": SYN_TABLE[8:24],  # allowed only in the fork's library
            "usr/share/clean": bytes(100) + b"\xff" * 64 + _rand("other", 200),
        }
    )
    assert source_hits(pkg, index, allowed) == {
        f"{PYPKG}/a.py": ["@0x6=module+100"],
        ".MTREE": [f"@{0x7 + k:#x}=table+{30 + k}" for k in range(5)],
        "usr/include/x.h": ["@0x0=table+8"],
    }


def test_audit_finds_a_planted_dll_window_but_not_text_or_elf():
    pkg = _synthetic_package(
        {
            SETUP: b"#!/usr/bin/python3 -IB\n# Timeslot Table\n",  # text shared with the DLL: not counted
            f"{PYPKG}/__pycache__/a.cpython-314.pyc": b"\xe3" + SYN_DLL[40:56] + b"\x00",
            "usr/lib/libfprint-2.so.2.0.0": b"\x7fELF" + SYN_DLL[:200],  # ELF: not compared with the whole DLL
            "usr/share/zeros": bytes(64),
            f"{PYPKG}/__pycache__/b.cpython-314.pyc": b"\xe3" + SPARSE + b"\x00",  # sparse: not counted
        }
    )
    indexed, hits = dll_hits(pkg, SYN_DLL)
    assert indexed == {SETUP, f"{PYPKG}/__pycache__/a.cpython-314.pyc"}  # the setup only through its compiled form
    assert hits == {f"{PYPKG}/__pycache__/a.cpython-314.pyc": ["dll@0x28"]}


def test_audit_decodes_hex_literals_and_compiles_the_setup_script():
    planted = SYN_DLL[500:532]
    spaced = ", ".join(f"0x{b:02x}" for b in planted).encode()
    pkg = _synthetic_package(
        {
            SETUP: b"#!/usr/bin/python3 -IB\nTABLE = " + repr(planted).encode() + b"\n",
            f"{PYPKG}/a.py": b"A = bytes.fromhex('" + planted.hex().encode() + b"')\nB = bytes([" + spaced + b"])\n",
            f"{PYPKG}/b.py": b"C = '" + "".join(f"\\x{b:02x}" for b in planted).encode() + b"'\n",
        }
    )
    assert decoded_hex(pkg.files[f"{PYPKG}/a.py"]) == [planted, planted]
    assert decoded_hex(pkg.files[f"{PYPKG}/b.py"]) == [planted]
    indexed, hits = dll_hits(pkg, SYN_DLL)
    assert indexed == {SETUP, f"{PYPKG}/a.py", f"{PYPKG}/b.py"}
    assert sorted(hits) == sorted(indexed)
    assert all(len(v) == len(planted) - WINDOW + 1 for v in hits.values())


FAKE_C = rb"""/* fake driver: { 0x99, 0x98 } in a comment is not an array */
#define CMD_X 0x51 /* a byte command */
#define WIDE 0x1234
static const guint8 table[] = { CMD_X, 0x00, 0x20, /* gap */ 0, 255, 'A', '\x7f', 017,
                                0b11, 10u };
static const guint8 grid[2][2] = { { 1, 2 }, { 3, 4 } };
static const guint16 words[] = { 1, 2, WIDE, 3, 4 };
static const struct { int a; const char *n; } named[] = { { 5, "five" }, { 6, NULL } };
static const char *s = "ab\x01\002\n" /* joined */ "\"cd\"";
"""


def test_c_literal_decoder():
    assert c_byte_runs(FAKE_C) == [
        b"Q\x00\x20\x00\xffA\x7f\x0f\x03\x0a",  # CMD_X #defined, comments skipped, all literal forms
        b"\x01\x02\x03\x04",  # nested braces continue the run
        b"\x01\x02",  # WIDE is above 255: it ends the run
        b"\x03\x04",
        b"five",  # a string literal is its own run, and as an element it ends the brace run
        b"\x05",
        b"\x06",  # NULL is no byte: it ends the run
        b"ab\x01\x02\n" + b'"cd"',  # adjacent literals joined across a comment, escapes decoded
    ]
    planted = SYN_DLL[500:532]
    assert c_byte_runs(b"/* dump: " + planted.hex(" ").encode() + b" */\n") == [planted]  # hex text too


def test_audit_finds_a_planted_window_in_the_driver_source():
    planted = SYN_DLL[950:982]  # "dll mid": outside the module and the table
    sources = {
        "drv.c": b"static const guint8 t[] = { " + b", ".join(b"0x%02x" % b for b in planted) + b" };\n",
        "drv.h": b'#define M "' + b"".join(b"\\x%02x" % b for b in SYN_MODULE[10:26]) + b'"\n',
        "names.c": b'const char *n = "debug name: Timeslot Table";\n',  # text shared with the DLL: not counted
        "short.c": b"static const guint8 t[] = { " + b", ".join(b"%d" % b for b in planted[:15]) + b" };\n",
    }
    vendor_index = window_index({"module": SYN_MODULE, "table": SYN_TABLE})
    indexed, hits = driver_hits(sources, SYN_DLL, vendor_index)
    assert indexed == {"drv.c", "drv.h", "names.c"}  # short.c has no 16-byte run
    assert hits == {
        "drv.c": [f"dll@{950 + k:#x}" for k in range(len(planted) - WINDOW + 1)],
        "drv.h": ["module+10", f"dll@{300 + 10:#x}"],
    }


def test_vendor_ref_repr_shows_no_bytes():
    assert repr(VendorRef(SYN_DLL)) == f"<vendor DLL, {len(SYN_DLL)} bytes>"


# ------------------------------------------------------------------ serial


def serial_report(pkg: Package, step: list[str], serials=None):
    """None when there is no serial to look for, "" when clean, else member:line per hit.

    Runs every step itself, so the test's frame never holds the serial (as in
    tools/test_no_serial.py); step names the stage running, for an error message.
    """
    step[0] = "deriving the serial"
    patterns = tns.serial_forms(tns.local_serials() if serials is None else serials)
    if not patterns:
        return None
    step[0] = "scanning the package"
    hits = [f"{tns.mask(n, patterns)}: path name" for n in tns.name_hits(list(pkg.members), patterns)]
    for name in sorted(pkg.files):
        for k, data in enumerate(pkg.contents(name)):
            where = name if k == 0 else f"{name} (gunzipped)"
            hits += [f"{tns.mask(where, patterns)}:{line}" for line in tns.hit_lines(data, patterns)]
    return "\n".join(hits)


def test_no_member_holds_the_unit_serial(pkg):
    step = ["starting"]
    error = None
    try:
        report = serial_report(pkg, step)
    except Exception as e:  # noqa: BLE001 - its message or frames could carry the serial
        error = f"{step[0]}: {type(e).__name__}"
    if error is not None:
        pytest.fail(error, pytrace=False)
    if report is None:
        pytest.skip("no local calibration data or RomInfo log to derive the serial from")
    if report:
        pytest.fail("the unit serial is in the package (member:line):\n" + report, pytrace=False)
