"""Guard: the sensor unit's 6-byte serial never reaches a file git tracks.

The serial is never written here. It is derived at run time from this machine's
untracked data (extracted/CalibrationData_decrypted.bin, RomInfo replies in the
gitignored proto/logs and local/logs) and the test is skipped where none exists.

What is scanned. The path list is `git ls-files` plus the staged additions and
changes (`git diff --cached --name-only --diff-filter=d`); a staged deletion, e.g.
`git rm --cached` of a file kept on disk, is not in it. Each listed path's name
and on-disk content are scanned (files are staged by path afterwards). For
each path whose index entry differs from HEAD the staged blob is scanned too,
when it differs from the disk copy, so a serial staged and then removed only on
disk (`git add -p`) is still caught. The caller's GIT_INDEX_FILE
is honoured.

Forms detected, for the serial in raw, reversed, nibble-swapped and reversed
nibble-swapped byte order (the Windows USB instance ID is the nibble-swapped form):
- hex text in any case, each byte pair optionally prefixed by 0x or \\x, with up
  to three separators (space, tab, CR, LF, : - _ . ,) between byte pairs;
- the same hex text encoded as UTF-16LE or UTF-16BE;
- the raw bytes;
- a Python bytes repr, where each byte is \\xNN or its printable character.
Not detected: base64, decimal byte lists ("19, 87, ..."), the serial as an
integer, other encodings (UTF-32, compressed or encrypted data).

A failure names path:line only ("(staged)" for a staged blob, "path name" for a
name), never what matched; a path name holding the serial is shown with it
replaced by <serial>. An error inside the guard is reported as the step and the
error type only, with no traceback, so `pytest -l` cannot show a local holding it.

The scanner is a set of pure functions so a leaked blob (e.g. from HEAD) can be
fed to it directly; its unit tests use a synthetic serial.
"""

import bisect
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CALIBRATION = REPO / "extracted" / "CalibrationData_decrypted.bin"
CAL_SERIAL_OFFSET = 0x20  # proto/calibration.py SERIAL_OFFSET
SERIAL_LEN = 6
LOG_DIRS = (REPO / "proto" / "logs", REPO / "local" / "logs")
ROM_INFO_MARK = b"command 0x01: RomInfo"
ROM_INFO_LEN = 38
ROM_SERIAL_OFFSET = 0x12
RX_REPLY = re.compile(rb"RX\s.*?\b(\d+) byte\(s\) in [\d.]+ ms: ([0-9a-fA-F ]+)")
MASK = b"<serial>"

_SEP_CHAR = rb"[ \t\r\n:\-_.,]"  # between two byte pairs, up to _MAX_SEP of these
_MAX_SEP = 3
# How a Python bytes repr writes the bytes it does not show as themselves or \xNN.
_REPR_ESCAPES = {0x09: rb"\\t", 0x0A: rb"\\n", 0x0D: rb"\\r", 0x5C: rb"\\\\", 0x27: rb"\\?'", 0x22: rb'\\?"'}
_UTF16 = {"ascii": lambda a: a, "le": lambda a: b"(?:" + a + rb")\x00", "be": lambda a: rb"\x00(?:" + a + b")"}


# ------------------------------------------------------------------ scanner


def nibble_swap(b: bytes) -> bytes:
    return bytes(((x & 0x0F) << 4) | (x >> 4) for x in b)


def serial_variants(serial: bytes) -> set[bytes]:
    """Byte orders the serial is seen in: raw, reversed, nibble-swapped, both."""
    return {serial, serial[::-1], nibble_swap(serial), nibble_swap(serial[::-1])}


def _hex_digits(x: int) -> list[bytes]:
    """Regex atoms for the two hex digits of byte x, either case."""
    return [f"[{c}{c.upper()}]".encode() if c.isalpha() else c.encode() for c in f"{x:02x}"]


def _hex_text(v: bytes, encoding: str) -> re.Pattern[bytes]:
    """Hex text of v in an encoding ("ascii", UTF-16 "le" / "be")."""
    char = _UTF16[encoding]
    sep = b"(?:" + char(_SEP_CHAR) + b"){0,%d}" % _MAX_SEP
    prefix = b"(?:" + char(b"0") + char(b"[xX]") + b"|" + char(rb"\\") + char(b"[xX]") + b")?"
    pairs = [b"".join(char(d) for d in _hex_digits(x)) for x in v]
    # No prefix before the first pair: a match starting there is the same hit,
    # and a pattern opening with an optional group is ~15x slower to search.
    return re.compile((sep + prefix).join(pairs))


def _repr_form(v: bytes):
    """Python bytes repr of v (each byte \\xNN or its printable form), or None if it has none."""
    shown = [x for x in v if x in _REPR_ESCAPES or 0x20 <= x < 0x7F]
    if not shown:
        return None  # all \xNN: the hex text pattern already matches it
    atoms = []
    for x in v:
        alts = [rb"\\[xX]" + b"".join(_hex_digits(x))]
        if x in _REPR_ESCAPES:
            alts.append(_REPR_ESCAPES[x])
        elif 0x20 <= x < 0x7F:
            alts.append(re.escape(bytes([x])))
        atoms.append(b"(?:" + b"|".join(alts) + b")")
    return re.compile(b"".join(atoms))


def serial_forms(serials) -> list[re.Pattern[bytes]]:
    """Patterns matching every textual and binary form of each serial."""
    patterns = []
    for v in sorted({v for s in serials for v in serial_variants(bytes(s))}):
        patterns += [_hex_text(v, enc) for enc in _UTF16]
        patterns.append(re.compile(re.escape(v)))
        r = _repr_form(v)
        if r is not None:
            patterns.append(r)
    return patterns


def hit_lines(data: bytes, patterns) -> list[int]:
    """1-based numbers of the lines where any pattern matches (start of the match)."""
    starts = {m.start() for p in patterns for m in p.finditer(data)}
    if not starts:
        return []
    newlines = [m.start() for m in re.finditer(rb"\n", data)]
    return sorted({bisect.bisect_left(newlines, at) + 1 for at in starts})


def mask(path: str, patterns) -> str:
    """The path with every serial form in it replaced by <serial>."""
    name = os.fsencode(path)
    for p in patterns:
        name = p.sub(MASK, name)
    return os.fsdecode(name)


def name_hits(paths, patterns) -> list[str]:
    """The paths whose name holds a serial form."""
    return sorted(p for p in set(paths) if hit_lines(os.fsencode(p), patterns))


def disk_bytes(root: Path, rel: str):
    """What git would store for rel from the worktree, or None if it is not there."""
    full = Path(root) / rel
    if full.is_symlink():
        return os.fsencode(os.readlink(full))  # git tracks the link text, not the target
    if full.is_file():
        return full.read_bytes()
    return None  # staged deletion or not checked out


def scan_paths(root: Path, paths, patterns) -> dict[str, list[int]]:
    """{path: hit lines} for the on-disk content of each path; missing paths are skipped."""
    hits = {}
    for rel in sorted(set(paths)):
        data = disk_bytes(root, rel)
        lines = hit_lines(data, patterns) if data is not None else []
        if lines:
            hits[rel] = lines
    return hits


def _git_z(root, env, *args) -> list[str]:
    r = subprocess.run(["git", *args, "-z"], cwd=root, env=env, capture_output=True, check=True)
    return [p for p in os.fsdecode(r.stdout).split("\0") if p]


def staged_paths(root: Path, env=None) -> list[str]:
    """Paths whose index entry differs from HEAD, staged deletions excluded."""
    return sorted(set(_git_z(root, env, "diff", "--cached", "--name-only", "--diff-filter=d")))


def git_paths(root: Path, env=None) -> list[str]:
    """Tracked paths plus the staged additions (`git ls-files` + `git diff --cached --diff-filter=d`)."""
    return sorted(set(_git_z(root, env, "ls-files")) | set(staged_paths(root, env)))


def staged_blobs(root: Path, paths, env=None) -> list[tuple[str, bytes]]:
    """(path, staged blob) for each index entry of the given paths, read in one `git cat-file`."""
    wanted = set(paths)
    entries = []
    for record in _git_z(root, env, "ls-files", "--stage"):
        info, _, path = record.partition("\t")
        mode, oid, _stage = info.split()
        if path in wanted and mode != "160000":  # a submodule entry names a commit, not a blob
            entries.append((path, oid))
    if not entries:
        return []
    batch = "".join(oid + "\n" for _, oid in entries).encode()
    out = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=root, env=env, input=batch, capture_output=True, check=True
    ).stdout
    blobs, pos = [], 0
    for path, _ in entries:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        pos = end + 1
        if len(header) == 3:  # "<oid> blob <size>"; a missing object is "<oid> missing"
            size = int(header[2])
            blobs.append((path, out[pos : pos + size]))
            pos += size + 1
    return blobs


def scan_blobs(blobs, patterns, root=None) -> dict[str, list[int]]:
    """{path: hit lines} for staged blobs; with root, a blob equal to its disk copy is skipped."""
    hits = {}
    for path, data in blobs:
        if root is not None and disk_bytes(root, path) == data:
            continue  # same content: already reported by scan_paths
        lines = hit_lines(data, patterns)
        if lines:
            hits[path] = sorted(set(hits.get(path, [])) | set(lines))
    return hits


def failure_report(patterns, disk=None, staged=None, names=()) -> str:
    """path:line per hit, with every path masked; never the matched text."""

    def m(path):
        return mask(path, patterns)

    disk, staged = disk or {}, staged or {}
    lines = [f"{m(p)}:{n}" for p in sorted(disk) for n in disk[p]]
    lines += [f"{m(p)}:{n} (staged)" for p in sorted(staged) for n in staged[p]]
    lines += [f"{m(p)}: path name" for p in sorted(names)]
    return "the unit serial is in tracked files (path:line):\n" + "\n".join(lines)


def _blank(s: bytes) -> bool:
    return s in (bytes(SERIAL_LEN), b"\xff" * SERIAL_LEN)


def _log_serial(data: bytes):
    """Serial in the first RomInfo reply of a log's bytes, or None."""
    at = data.find(ROM_INFO_MARK)
    while at >= 0:
        start = data.find(b"\n", at) + 1  # the reply is on the next line
        if not start:
            return None
        end = data.find(b"\n", start)
        m = RX_REPLY.search(data, start, end if end >= 0 else len(data))
        if m and int(m.group(1)) == ROM_INFO_LEN:
            reply = bytes.fromhex(m.group(2).decode())
            if len(reply) == ROM_INFO_LEN:
                return reply[ROM_SERIAL_OFFSET : ROM_SERIAL_OFFSET + SERIAL_LEN]
        at = data.find(ROM_INFO_MARK, at + 1)
    return None


def local_serials(calibration=CALIBRATION, log_dirs=LOG_DIRS) -> set[bytes]:
    """Every serial this machine's untracked data knows of (calibration blob, RomInfo logs)."""
    found = set()
    if calibration.is_file():
        s = calibration.read_bytes()[CAL_SERIAL_OFFSET : CAL_SERIAL_OFFSET + SERIAL_LEN]
        if len(s) == SERIAL_LEN:
            found.add(s)
    for d in log_dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.log")):
            s = _log_serial(path.read_bytes())
            if s is not None:
                found.add(s)
    return {s for s in found if not _blank(s)}


# ------------------------------------------------------------------ the guard


def guard_report(step, root=REPO, env=None, serials=None):
    """None when there is no serial to look for, "" when clean, else the failure text.

    step is a one-item list naming the stage running, for the error message.
    """
    step[0] = "deriving the serial"
    patterns = serial_forms(local_serials() if serials is None else serials)
    if not patterns:
        return None
    step[0] = "listing the git paths"
    paths = git_paths(root, env)
    staged = staged_paths(root, env)
    step[0] = "scanning the worktree"
    disk = scan_paths(root, paths, patterns)
    names = name_hits(paths, patterns)
    step[0] = "scanning the staged blobs"
    in_index = scan_blobs(staged_blobs(root, staged, env), patterns, root)
    step[0] = "writing the report"
    if not (disk or in_index or names):
        return ""
    return failure_report(patterns, disk, in_index, names)


def test_no_tracked_file_contains_the_unit_serial():
    # Every step runs in guard_report, so this frame never holds the serial, and an
    # error leaves its frame before failing: no message, traceback or local shows it.
    step = ["starting"]
    error = None
    try:
        report = guard_report(step)
    except Exception as e:  # noqa: BLE001 - its message or frames could carry the serial
        error = f"{step[0]}: {type(e).__name__}"
    if error is not None:
        pytest.fail(error, pytrace=False)
    if report is None:
        pytest.skip("no local calibration data or RomInfo log to derive the serial from")
    if report:
        pytest.fail(report, pytrace=False)


# ------------------------------------------------------------------ scanner unit tests

FAKE = bytes.fromhex("13579bdf0246")  # synthetic, not a real unit's
FAKE_FORMS = [
    b"13579bdf0246",  # raw order, hex
    b"6420fdb97531",  # nibble-swapped of reversed
    b"4602df9b5713",  # reversed
    b"3175b9fd2064",  # nibble-swapped
]
# Synthetic, with bytes a repr shows as characters: A ' \ LF " and two \xNN.
PRINTABLE = bytes.fromhex("41275c0a2213")


def scan(data: bytes, serial=FAKE) -> list[int]:
    return hit_lines(data, serial_forms([serial]))


@pytest.mark.parametrize(
    "form",
    [
        b"13579bdf0246",
        b"13579BDF0246",
        b"13 57 9b df 02 46",
        b"13:57:9B:DF:02:46",
        b"13-57-9b-df-02-46",
        b"13_57_9b_df_02_46",
        b"13.57.9b.df.02.46",
        b"13\t57\t9b\tdf\t02\t46",
        b"13  57 9b df 02 46",
        b"13 : 57 : 9b : df : 02 : 46",
        b"0x13, 0x57, 0x9b, 0xdf, 0x02, 0x46",
        b"0x13,\r\n0x57,\r\n0x9b,\r\n0xdf,\r\n0x02,\r\n0x46",
        b"\\x13\\x57\\x9b\\xdf\\x02\\x46",
        b"4602df9b5713",
        b"46 02 DF 9B 57 13",
        b"3175b9fd2064",
        b"3175B9FD2064",
        b"6420fdb97531",
        b"6420FDB97531",
        FAKE,
        FAKE[::-1],
        repr(FAKE).encode(),  # b'\x13W\x9b\xdf\x02F'
        repr(FAKE[::-1]).encode(),
        "13579bdf0246".encode("utf-16-le"),
        "13579BDF0246".encode("utf-16-le"),
        "3175B9FD2064".encode("utf-16-le"),
        "31 75 b9 fd 20 64".encode("utf-16-le"),
        "0x13, 0x57, 0x9b, 0xdf, 0x02, 0x46".encode("utf-16-le"),
        "13579bdf0246".encode("utf-16-be"),
        "46:02:DF:9B:57:13".encode("utf-16-be"),
    ],
)
def test_every_form_is_found_on_its_line(form):
    data = b"first line\nsecond line\nprefix " + form + b" suffix\nlast\n"
    assert scan(data) == [3]


@pytest.mark.parametrize("which", range(4))
def test_bytes_repr_with_printable_bytes_is_found(which):
    v = sorted(serial_variants(PRINTABLE))[which]
    for data in (v, b"\x00\x01" + v + b"zz", b"\x7f'" + v + b'"'):
        text = repr(data).encode()
        assert scan(b"one\n" + text + b"\n", PRINTABLE) == [2]
        upper = re.sub(rb"\\x[0-9a-f]{2}", lambda m: m.group().upper(), text)  # \X41 from a tool
        assert scan(upper, PRINTABLE) == [1]


def test_hits_on_several_lines_are_each_reported():
    data = b"13579bdf0246\nclean\n\x00\x01" + FAKE + b"\nclean\n3175b9fd2064 and 4602df9b5713\n"
    assert scan(data) == [1, 3, 5]


def test_hex_inside_a_longer_hex_run_is_found():
    rom = bytes(0x12) + FAKE + bytes(14)
    assert scan(b'ROM = bytes.fromhex("' + rom.hex().encode() + b'")\n') == [1]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"nothing to see here\n" * 50,
        b"13579bdf024",  # one nibble short
        b"13579bdf0247",  # last byte differs
        b"13    57 9b df 02 46",  # four separators
        b"13579bdf0256",
        b"b'\\x13W\\x9b\\xdf\\x02G'",  # repr, last byte differs
        bytes(range(256)) * 4,
    ],
)
def test_clean_data_has_no_hits(data):
    assert scan(data) == []


def test_scan_paths_reports_only_files_with_hits_and_skips_missing(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "clean.txt").write_bytes(b"clean\n")
    (tmp_path / "sub" / "leak.txt").write_bytes(b"a\nb\nid 31:75:b9:fd:20:64\n")
    (tmp_path / "dump.bin").write_bytes(b"\x00" * 10 + FAKE)
    paths = ["clean.txt", "sub/leak.txt", "dump.bin", "gone.txt"]
    assert scan_paths(tmp_path, paths, serial_forms([FAKE])) == {"dump.bin": [1], "sub/leak.txt": [3]}


def test_path_names_with_the_serial_are_found_and_masked():
    patterns = serial_forms([FAKE])
    paths = ["clean.txt", "dev/usb-3175B9FD2064/info.txt", "x_13-57-9b-df-02-46.log"]
    assert name_hits(paths, patterns) == paths[1:]
    assert mask(paths[1], patterns) == "dev/usb-<serial>/info.txt"
    assert mask("clean.txt", patterns) == "clean.txt"


def test_failure_report_names_path_and_line_but_no_serial_form():
    patterns = serial_forms([FAKE])
    named = "logs/13579bdf0246.txt"
    report = failure_report(
        patterns, {"sub/leak.txt": [3, 7], "dump.bin": [1], named: [2]}, {"new.txt": [4]}, [named]
    ).encode()
    for entry in (
        b"dump.bin:1\n",
        b"sub/leak.txt:3\n",
        b"sub/leak.txt:7\n",
        b"logs/<serial>.txt:2\n",
        b"new.txt:4 (staged)\n",
        b"logs/<serial>.txt: path name",
    ):
        assert entry in report
    for form in FAKE_FORMS:
        assert form not in report.lower()
    assert FAKE not in report and FAKE[::-1] not in report
    assert hit_lines(report, patterns) == []


def git_env():
    """The caller's environment minus every GIT_* variable (a hook's GIT_INDEX_FILE etc.)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


@pytest.fixture
def repo(tmp_path):
    """A temp repo with one clean commit, and a git() runner that never sees the caller's GIT_*."""
    env = git_env()

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "tracked.txt").write_text("clean\n")
    (tmp_path / "dump.txt").write_text("serial 3175b9fd2064\n")
    git("add", "tracked.txt", "dump.txt")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
    return tmp_path, env, git


def test_git_paths_has_tracked_and_staged_only_files(repo):
    root, env, git = repo
    (root / "staged.txt").write_text("x\n3175b9fd2064\n")
    git("add", "staged.txt")
    (root / "untracked.txt").write_text("13579bdf0246\n")
    paths = git_paths(root, env=env)
    assert paths == ["dump.txt", "staged.txt", "tracked.txt"]
    assert scan_paths(root, paths, serial_forms([FAKE])) == {"dump.txt": [1], "staged.txt": [2]}


def test_staged_deletion_kept_on_disk_is_not_scanned(repo):
    root, env, git = repo
    git("rm", "-q", "--cached", "dump.txt")
    assert (root / "dump.txt").is_file()
    assert git_paths(root, env=env) == ["tracked.txt"]
    assert staged_paths(root, env=env) == []
    assert guard_report(["x"], root, env, [FAKE]) == ""


def test_staged_blob_is_scanned_after_the_disk_copy_is_cleaned(repo):
    root, env, git = repo
    git("rm", "-q", "--cached", "dump.txt")
    (root / "new.txt").write_text("a\nb\nid 13579bdf0246\n")  # staged-only new file
    (root / "tracked.txt").write_text("clean\n4602df9b5713\n")
    git("add", "new.txt", "tracked.txt")
    (root / "new.txt").write_text("a\nb\nid <serial>\n")  # removed on disk only
    (root / "tracked.txt").write_text("clean\n")
    assert staged_paths(root, env=env) == ["new.txt", "tracked.txt"]
    blobs = dict(staged_blobs(root, ["new.txt", "tracked.txt"], env=env))
    assert blobs == {"new.txt": b"a\nb\nid 13579bdf0246\n", "tracked.txt": b"clean\n4602df9b5713\n"}
    report = guard_report(["x"], root, env, [FAKE])
    assert report.splitlines()[1:] == ["new.txt:3 (staged)", "tracked.txt:2 (staged)"]


def test_staged_blob_equal_to_disk_is_reported_once(repo):
    root, env, git = repo
    (root / "new.txt").write_text("13579bdf0246\n")
    git("add", "new.txt")
    report = guard_report(["x"], root, env, [FAKE])
    assert report.splitlines()[1:] == ["dump.txt:1", "new.txt:1"]


def test_guard_reports_a_path_name_masked(repo):
    root, env, git = repo
    git("rm", "-q", "--cached", "dump.txt")
    (root / "id-13579bdf0246.txt").write_text("clean\n")
    git("add", "id-13579bdf0246.txt")
    report = guard_report(["x"], root, env, [FAKE])
    assert report.splitlines()[1:] == ["id-<serial>.txt: path name"]


def test_guard_without_a_serial_returns_none(repo):
    root, env, _ = repo
    assert guard_report(["x"], root, env, set()) is None


def test_local_serials_from_calibration_and_logs(tmp_path):
    other = bytes.fromhex("a0b1c2d3e4f5")  # synthetic
    cal = tmp_path / "cal.bin"
    cal.write_bytes(bytes(CAL_SERIAL_OFFSET) + FAKE + bytes(100))
    logs = tmp_path / "logs"
    logs.mkdir()
    reply = bytes(ROM_SERIAL_OFFSET) + other + bytes(ROM_INFO_LEN - ROM_SERIAL_OFFSET - SERIAL_LEN)
    (logs / "a.log").write_text(
        f"t INFO    | === command 0x01: RomInfo ===\nt RX     EP 0x83 38 byte(s) in 0.2 ms: {reply.hex()}\n"
    )
    (logs / "b.log").write_text("t INFO    | === command 0x01: RomInfo ===\nt RX timeout\n")
    (logs / "c.log").write_text("no rom info here\n")
    assert local_serials(cal, (logs, tmp_path / "missing")) == {FAKE, other}


def test_local_serials_ignores_blank_and_missing(tmp_path):
    cal = tmp_path / "cal.bin"
    cal.write_bytes(bytes(CAL_SERIAL_OFFSET + 100))
    assert local_serials(cal, ()) == set()
    assert local_serials(tmp_path / "missing.bin", ()) == set()
    assert serial_forms(set()) == []
