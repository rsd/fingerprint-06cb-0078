"""B2b part 1: assemble the full 18869-byte `02` identify program from a calibrate.py run.

Offline: no USB, no sudo. Reads RUN_DIR (a local/calib/<stamp>/ directory written by
proto/calibrate.py --run), checks that its verdict is PASS, splices its four per-unit
chunk files into the pinned template (calibration.assemble_cmd02, the same splice the
Windows-derived program goes through) and writes RUN_DIR/program-linux.bin (0600).

Self-check, when both are present: the Windows CalibrationData run through the same
derivation (calib_model.derive_chunks) and the same splice must give the Windows
program byte for byte; otherwise nothing is written (exit 6). Then a per-chunk numeric
diff of the Linux program against the Windows one.

The unit serial in CalibrationData (0x20..0x25) is never printed or written; neither
is any per-unit byte. Only counts, means and the sha256 of program-linux.bin are shown.

Usage: proto/.venv/bin/python proto/calib_program.py RUN_DIR [--windows-program PATH]
       [--windows-calibration PATH] [--force]

Exit codes (as calibrate.py): 0 done, 5 bad or missing input, a different
program-linux.bin already there (without --force) or an unwritable output, 6 the
self-check failed or the built program is not valid (nothing written).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import calib_model as cm
import calibration
from calibration import CalibrationError, UnitChunks

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WINDOWS_PROGRAM = ROOT / "research" / "calib" / "ours_cmd02.bin"
DEFAULT_WINDOWS_CAL = ROOT / "extracted" / "CalibrationData_decrypted.bin"  # = calibrate.DEFAULT_WINDOWS_CAL
A5_VERIFY = ROOT / "tools" / "dev" / "a5-verify.sh"
B2_PROGRAM = ROOT / "tools" / "dev" / "b2-program.sh"
OUTPUT_NAME = "program-linux.bin"
RESULT_NAME = "result.json"
# (UnitChunks field, file written by calibrate.py, exact size)
CHUNK_FILES = (
    ("key_line", "chunk-34-keyline.bin", calibration.KEY_LINE_LEN),
    ("line_update", "chunk-30-data.bin", calibration.LINE_UPDATE_LEN),
    ("factory_entries", "chunk-43-factory.bin", calibration.FACTORY_ENTRIES_LEN),
    ("baseline", "chunk-48-baseline.bin", calibration.BASELINE_CHUNK_LEN),
)

EXIT_OK, EXIT_BAD_INPUT, EXIT_FAIL = 0, 5, 6  # calibrate.EXIT_BAD_INPUT, calibrate.EXIT_FAIL


class Refused(Exception):
    """Stop with one user-facing line and an exit code."""

    def __init__(self, text: str, rc: int = EXIT_BAD_INPUT):
        super().__init__(text)
        self.text, self.rc = text, rc


def shown(path, cwd=None) -> str:
    """`path` as the user can type it from `cwd`: relative under cwd (never a bare basename), else absolute."""
    p = Path(os.path.abspath(path))
    base = Path(os.path.abspath(cwd if cwd is not None else os.getcwd()))
    try:
        rel = str(p.relative_to(base))
    except ValueError:
        return str(p)
    if rel == ".":
        return "."
    return rel if "/" in rel else f"./{rel}"


def read_regular(path: Path, size: int | None = None) -> bytes:
    """Bytes of a regular file (not a symlink); Refused when missing, not regular or not `size` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise Refused(f"{shown(path)} is missing") from None
    except OSError as exc:
        raise Refused(f"{shown(path)} is not a readable regular file ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise Refused(f"{shown(path)} is not a regular file")
        data = fh.read()
    if size is not None and len(data) != size:
        raise Refused(f"{shown(path)} is {len(data)} bytes, need {size}")
    return data


def load_run(run_dir: Path) -> UnitChunks:
    """The four chunk files of a PASS calibration run; Refused otherwise."""
    if not run_dir.is_dir():
        raise Refused(f"{shown(run_dir)} is not a directory (give a local/calib/<stamp> run of proto/calibrate.py)")
    try:
        result = json.loads(read_regular(run_dir / RESULT_NAME))
    except (ValueError, UnicodeDecodeError):
        raise Refused(f"{shown(run_dir / RESULT_NAME)} is not valid JSON") from None
    verdict = result.get("verdict") if isinstance(result, dict) else None
    if verdict != "PASS":
        raise Refused(f"{shown(run_dir)}: the calibration verdict is {str(verdict)[:20]!r}, not 'PASS'; use a PASS run")
    return UnitChunks(**{field: read_regular(run_dir / name, size) for field, name, size in CHUNK_FILES})


def self_check(template: bytes, program_path: Path, cal_path: Path) -> str:
    """The Windows CalibrationData through derive_chunks + assemble_cmd02 must equal the Windows program.

    Returns the line to print; Refused(EXIT_FAIL) on a mismatch. Nothing of the
    CalibrationData (its serial in particular) goes into any message.
    """
    absent = [shown(p) for p in (program_path, cal_path) if not p.exists()]
    if absent:
        return f"self-check: skipped ({' and '.join(absent)} not present)"
    windows_program = read_regular(program_path)
    try:
        win = calibration.parse_calibration_data(read_regular(cal_path))
    except CalibrationError as exc:
        raise Refused(f"{shown(cal_path)} is not a usable CalibrationData: {exc}") from None
    try:
        rebuilt = calibration.assemble_cmd02(template, cm.derive_chunks(win.factory, win.frame, win.baseline))
    except CalibrationError as exc:
        raise Refused(f"self-check FAILED: the Windows CalibrationData does not assemble: {exc}", EXIT_FAIL) from None
    if rebuilt != windows_program:
        if len(rebuilt) != len(windows_program):
            where = f"{len(windows_program)} bytes, rebuilt {len(rebuilt)}"
        else:
            off = next(i for i, (a, b) in enumerate(zip(rebuilt, windows_program)) if a != b)
            where = f"first difference at byte {off}: {calibration.describe_offset(rebuilt, off)}"
        raise Refused(
            f"self-check FAILED: rebuilt from {shown(cal_path)} is not {shown(program_path)} ({where}); "
            "nothing written",
            EXIT_FAIL,
        )
    return f"self-check: passed (the Windows program rebuilt from its CalibrationData is byte-exact, {len(rebuilt)} B)"


def write_atomic(directory: Path, name: str, data: bytes) -> None:
    """directory/name = data, mode 0600, via an O_EXCL temp file in `directory`, fsync and rename."""
    tmp = directory / f".{name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, directory / name)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    dfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def store(run_dir: Path, program: bytes, force: bool) -> str:
    """Write RUN_DIR/program-linux.bin; returns what happened, in words. Refused on a different existing file."""
    target = run_dir / OUTPUT_NAME
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        write_atomic(run_dir, OUTPUT_NAME, program)
        return "written"
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{shown(target)} exists and is not a regular file; move it away first")
    if read_regular(target) == program:
        return "already present and identical, left as it is"
    if not force:
        raise Refused(f"{shown(target)} exists with different content; rerun with --force to replace it")
    write_atomic(run_dir, OUTPUT_NAME, program)
    return "replaced"


# ---------------------------------------------------------------- diff


@dataclass(frozen=True)
class DiffRow:
    label: str
    differing: int
    total: int
    mean_abs: float | None  # None: not meaningful (bit-packed bytes)
    max_abs: int | None


def _numeric(label: str, a, b) -> DiffRow:
    d = [abs(x - y) for x, y in zip(a, b)]
    return DiffRow(label, sum(1 for v in d if v), len(d), sum(d) / len(d), max(d))


def _s8(data: bytes) -> list[int]:
    return [cm.s8(x) for x in data]


def factory_values(entries: bytes) -> list[int]:
    """The 56 five-bit factory values of a chunk 0x43 region (first entry: format, minimum, u16 0, stream)."""
    mn = entries[1]
    stream = int.from_bytes(entries[4 : calibration.FACTORY_ENTRY_LEN], "little")
    mask = (1 << calibration.FACTORY_BITS) - 1
    return [mn + ((stream >> (calibration.FACTORY_BITS * i)) & mask) for i in range(calibration.FACTORY_COUNT)]


def diff_programs(linux: bytes, windows: bytes) -> tuple[list[DiffRow], int]:
    """(per-region rows, bytes differing outside the four regions). CalibrationError if either does not parse."""
    if len(linux) != len(windows):
        raise CalibrationError(f"the Windows program is {len(windows)} bytes, the Linux one {len(linux)}")
    a, b = calibration.unit_regions(linux), calibration.unit_regions(windows)
    bpl, hdr = calibration.BPL, cm.HEADER_LEN

    def px(base: bytes) -> bytes:
        f = base[4:]
        return b"".join(f[r * bpl + hdr : (r + 1) * bpl] for r in range(calibration.LINES))

    def heads(base: bytes) -> bytes:
        f = base[4:]
        return b"".join(f[r * bpl : r * bpl + hdr] for r in range(calibration.LINES))

    fb = sum(1 for x, y in zip(a.factory_entries, b.factory_entries) if x != y)
    rows = [
        _numeric("0x34 key line (s8)", _s8(a.key_line), _s8(b.key_line)),
        _numeric("0x30 line update (s8)", _s8(a.line_update), _s8(b.line_update)),
        _numeric("0x43 factory values", factory_values(a.factory_entries), factory_values(b.factory_entries)),
        DiffRow("0x43 bytes", fb, len(a.factory_entries), None, None),
        _numeric("0x48 baseline pixels (u8)", px(a.baseline), px(b.baseline)),
        _numeric("0x48 line headers (u8)", heads(a.baseline), heads(b.baseline)),
    ]
    inside = bytearray(len(linux))
    for lo, hi in calibration.unit_region_spans(linux).values():
        inside[lo:hi] = b"\x01" * (hi - lo)
    outside = sum(1 for i, (x, y) in enumerate(zip(linux, windows)) if x != y and not inside[i])
    return rows, outside


def diff_lines(rows: list[DiffRow], outside: int) -> list[str]:
    w = max(len(r.label) for r in rows)
    out = [f"  {'region':<{w}}  {'differing':>13}  {'mean |Δ|':>8}  {'max |Δ|':>7}"]
    for r in rows:
        mean = "-" if r.mean_abs is None else f"{r.mean_abs:.2f}"
        mx = "-" if r.max_abs is None else str(r.max_abs)
        out.append(f"  {r.label:<{w}}  {r.differing:>5} / {r.total:<5}  {mean:>8}  {mx:>7}")
    out.append(f"  bytes differing outside the four regions: {outside} (expected 0)")
    return out


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Assemble the 18869-byte 02 identify program from a PASS calibrate.py run (offline, no USB)."
    )
    ap.add_argument("run_dir", metavar="RUN_DIR", help="a local/calib/<stamp> directory of proto/calibrate.py --run")
    ap.add_argument(
        "--windows-program",
        metavar="PATH",
        default=str(DEFAULT_WINDOWS_PROGRAM),
        help="Windows-derived identify program, for the self-check and the diff (default research/calib/...)",
    )
    ap.add_argument(
        "--windows-calibration",
        metavar="PATH",
        default=str(DEFAULT_WINDOWS_CAL),
        help="Windows CalibrationData_decrypted.bin, for the self-check (default extracted/...)",
    )
    ap.add_argument("--force", action="store_true", help="replace a different existing program-linux.bin")
    return ap


def run(args) -> int:
    run_dir = Path(args.run_dir)
    win_program, win_cal = Path(args.windows_program), Path(args.windows_calibration)
    chunks = load_run(run_dir)
    print(f"Run {shown(run_dir)}: calibration PASS, chunk files complete")
    try:
        template = calibration.load_template()
    except (OSError, CalibrationError) as exc:
        raise Refused(f"template not usable: {exc}") from None
    try:
        program = calibration.assemble_cmd02(template, chunks)
    except CalibrationError as exc:
        raise Refused(f"the run's chunk files do not form a program: {exc}") from None
    problem = calibration.program_problem(program, template)
    if problem is not None:
        raise Refused(f"the built program is not valid ({problem}); nothing written", EXIT_FAIL)
    print(self_check(template, win_program, win_cal))
    what = store(run_dir, program, args.force)
    print(f"{shown(run_dir / OUTPUT_NAME)}: {what} ({len(program)} B, sha256 {hashlib.sha256(program).hexdigest()})")
    if win_program.exists():
        try:
            rows, outside = diff_programs(program, read_regular(win_program))
        except CalibrationError as exc:
            print(f"diff vs the Windows program: not available ({exc})")
        else:
            print(f"Linux vs Windows program ({shown(win_program)}):")
            for line in diff_lines(rows, outside):
                print(line)
    else:
        print(f"diff vs the Windows program: skipped ({shown(win_program)} not present)")
    print(f"Next (README, B2b): first measure today's Windows baseline with {shown(A5_VERIFY)}, then run:")
    print(f"  sudo {shlex.quote(shown(B2_PROGRAM))} use-linux {shlex.quote(shown(run_dir))}")
    return EXIT_OK


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Refused as exc:
        print(exc.text, file=sys.stderr)
        return exc.rc
    except OSError as exc:
        name = exc.filename if exc.filename is None else shown(exc.filename)
        print(f"could not complete: {exc.strerror} ({name})", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    sys.exit(main())
