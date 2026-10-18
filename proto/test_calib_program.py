"""Offline tests of calib_program.py (no USB): temp run directories and synthetic data only.

The synthetic Windows side is a CalibrationData blob from test_calibrate_flow (with a
distinctive serial that must never reach any output) and the program build_cmd02 makes
from it. Everything runs on the synthetic template of synth_template.py (patched in for
the whole module), so it runs the same on a clean clone. The one real-unit check needs
research/calib/ours_cmd02.bin, extracted/CalibrationData_decrypted.bin and the real
proto/data/cmd02_template.bin (all local-only) and is skipped without them; it never
reads local/calib. Its synthetic counterpart is Build.test_written_program_is_valid_and_private
(the same self_check, on a synthetic Windows program and CalibrationData).
"""

import contextlib
import io
import json
import os
import shlex
import stat
import tempfile
import unittest
from pathlib import Path

import calib_model as cm
import calib_program as cp
import calibrate
import calibration
from synth_template import setUpModule, tearDownModule  # noqa: F401 (unittest module fixtures)
from test_calibrate_flow import BF, CF, FACTORY, SERIAL, serial_forms, windows_calibration

REAL_LOAD_TEMPLATE = calibration.load_template  # setUpModule swaps the module's; this one reads the real file
HAVE_TEMPLATE = calibration.TEMPLATE_PATH.exists()
REAL_PROGRAM = cp.DEFAULT_WINDOWS_PROGRAM
REAL_CAL = cp.DEFAULT_WINDOWS_CAL


def shifted(frame: bytes, by: int) -> bytes:
    """`frame` with every pixel (not the 8-byte line headers) moved by `by`, clamped."""
    return bytes(x if i % 152 < 8 else max(0, min(255, x + by)) for i, x in enumerate(frame))


LINUX_CF = shifted(CF, 3)
LINUX_BF = shifted(BF, 1)
LINUX_CHUNKS = cm.derive_chunks(FACTORY, LINUX_CF, LINUX_BF)


def write_run(path: Path, chunks=LINUX_CHUNKS, verdict="PASS") -> Path:
    path.mkdir(mode=0o700)
    (path / "result.json").write_text(json.dumps({"verdict": verdict, "tool": "proto/calibrate.py"}))
    for field, name, _ in cp.CHUNK_FILES:
        (path / name).write_bytes(getattr(chunks, field))
    return path


class Env:
    """A temp directory with a run directory and (optionally) a synthetic Windows pair."""

    def __init__(self, td: str, windows_program=True, windows_cal=True, chunks=LINUX_CHUNKS, verdict="PASS"):
        self.dir = Path(td)
        self.run = write_run(self.dir / "20261017-000000", chunks, verdict)
        self.blob = windows_calibration()
        self.win_program = self.dir / "ours_cmd02.bin"
        self.win_cal = self.dir / "CalibrationData_decrypted.bin"
        if windows_program:
            self.win_program.write_bytes(calibration.build_cmd02_from_calibration_data(self.blob))
        if windows_cal:
            self.win_cal.write_bytes(self.blob)
        self.output = self.run / cp.OUTPUT_NAME

    def main(self, *extra, umask=None):
        argv = [
            str(self.run),
            "--windows-program",
            str(self.win_program),
            "--windows-calibration",
            str(self.win_cal),
            *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        old = os.umask(umask) if umask is not None else None
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cp.main(argv)
        finally:
            if old is not None:
                os.umask(old)
        return rc, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def env(**kwargs):
    with tempfile.TemporaryDirectory() as td:
        yield Env(td, **kwargs)


def leftovers(run: Path) -> list[str]:
    return sorted(p.name for p in run.iterdir() if p.name.startswith("."))


class Constants(unittest.TestCase):
    def test_match_calibrate(self):
        self.assertEqual((cp.EXIT_BAD_INPUT, cp.EXIT_FAIL), (calibrate.EXIT_BAD_INPUT, calibrate.EXIT_FAIL))
        self.assertEqual(cp.DEFAULT_WINDOWS_CAL, calibrate.DEFAULT_WINDOWS_CAL)
        self.assertEqual([size for _, _, size in cp.CHUNK_FILES], [144, 8064, 78, 8516])
        self.assertEqual([f for f, _, _ in cp.CHUNK_FILES], list(calibration.UnitChunks.__dataclass_fields__))


class Shown(unittest.TestCase):
    def test_relative_under_cwd_never_bare(self):
        self.assertEqual(cp.shown("/r/tools/dev/b2-program.sh", "/r"), "tools/dev/b2-program.sh")
        self.assertEqual(cp.shown("/r/tools/dev/b2-program.sh", "/r/tools/dev"), "./b2-program.sh")
        self.assertEqual(cp.shown("/r/tools/dev/b2-program.sh", "/home"), "/r/tools/dev/b2-program.sh")
        self.assertEqual(cp.shown("/r", "/r"), ".")


class Refusals(unittest.TestCase):
    def check_refused(self, e, needle):
        rc, out, err = e.main()
        self.assertEqual(rc, cp.EXIT_BAD_INPUT, out + err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn(needle, err)
        self.assertFalse(e.output.exists())
        self.assertEqual(leftovers(e.run), [])

    def test_non_pass_verdict(self):
        with env(verdict="FAIL") as e:
            self.check_refused(e, "verdict is 'FAIL'")

    def test_no_result_json(self):
        with env() as e:
            (e.run / "result.json").unlink()
            self.check_refused(e, "result.json is missing")

    def test_bad_result_json(self):
        with env() as e:
            (e.run / "result.json").write_text("{not json")
            self.check_refused(e, "not valid JSON")
        with env() as e:
            (e.run / "result.json").write_text("[1, 2]")
            self.check_refused(e, "not 'PASS'")

    def test_missing_chunk(self):
        with env() as e:
            (e.run / "chunk-30-data.bin").unlink()
            self.check_refused(e, "chunk-30-data.bin is missing")

    def test_wrong_size_chunk(self):
        with env() as e:
            (e.run / "chunk-48-baseline.bin").write_bytes(LINUX_CHUNKS.baseline[:-1])
            self.check_refused(e, "chunk-48-baseline.bin is 8515 bytes, need 8516")

    def test_symlinked_chunk(self):
        with env() as e:
            real = e.dir / "elsewhere.bin"
            real.write_bytes(LINUX_CHUNKS.key_line)
            (e.run / "chunk-34-keyline.bin").unlink()
            (e.run / "chunk-34-keyline.bin").symlink_to(real)
            self.check_refused(e, "chunk-34-keyline.bin is not a readable regular file")

    def test_run_dir_not_a_directory(self):
        with env() as e:
            e.run = e.dir / "nope"
            rc, _, err = e.main()
            self.assertEqual(rc, cp.EXIT_BAD_INPUT)
            self.assertIn("is not a directory", err)

    def test_chunk_content_that_does_not_fit(self):
        bad = LINUX_CHUNKS.factory_entries[:-1] + bytes([LINUX_CHUNKS.factory_entries[-1] ^ 1])
        with env(
            chunks=calibration.UnitChunks(LINUX_CHUNKS.key_line, LINUX_CHUNKS.line_update, bad, LINUX_CHUNKS.baseline)
        ) as e:
            self.check_refused(e, "the two factory entries differ")


class Build(unittest.TestCase):
    def test_written_program_is_valid_and_private(self):
        for umask in (0o022, 0o000):
            with self.subTest(umask=oct(umask)), env() as e:
                rc, out, err = e.main(umask=umask)
                self.assertEqual(rc, 0, out + err)
                data = e.output.read_bytes()
                self.assertEqual(len(data), calibration.PAYLOAD_LEN)
                self.assertIsNone(calibration.program_problem(data))
                self.assertEqual(data, calibration.assemble_cmd02(None, LINUX_CHUNKS))
                self.assertEqual(stat.S_IMODE(e.output.stat().st_mode), 0o600)
                self.assertEqual(leftovers(e.run), [])
                self.assertIn("self-check: passed", out)
                self.assertIn("written", out)

    def test_self_check_skipped_when_absent(self):
        for kwargs, which in (
            ({"windows_program": False}, "ours_cmd02.bin not present"),
            ({"windows_cal": False}, "CalibrationData_decrypted.bin not present"),
        ):
            with self.subTest(which=which), env(**kwargs) as e:
                rc, out, err = e.main()
                self.assertEqual(rc, 0, out + err)
                self.assertIn("self-check: skipped (", out)
                self.assertIn(which, out)
                self.assertTrue(e.output.exists())
        with env(windows_program=False, windows_cal=False) as e:
            rc, out, _ = e.main()
            self.assertEqual(rc, 0)
            self.assertIn(" and ", out.splitlines()[1])
            self.assertIn("diff vs the Windows program: skipped", out)

    def test_self_check_fails_when_windows_program_differs(self):
        with env() as e:
            prog = bytearray(e.win_program.read_bytes())
            prog[-1] ^= 0x01  # last baseline byte
            e.win_program.write_bytes(bytes(prog))
            rc, out, err = e.main()
            self.assertEqual(rc, cp.EXIT_FAIL, out + err)
            self.assertIn("self-check FAILED", err)
            self.assertIn("chunk 0x48", err)
            self.assertFalse(e.output.exists())
            self.assertEqual(leftovers(e.run), [])

    def test_identical_existing_is_left_alone(self):
        with env() as e:
            self.assertEqual(e.main()[0], 0)
            before = e.output.stat()
            rc, out, _ = e.main()
            self.assertEqual(rc, 0)
            self.assertIn("already present and identical", out)
            self.assertEqual(e.output.stat().st_ino, before.st_ino)

    def test_differing_existing_refused_then_forced(self):
        with env() as e:
            e.output.write_bytes(b"something else")
            os.chmod(e.output, 0o644)
            rc, out, err = e.main()
            self.assertEqual(rc, cp.EXIT_BAD_INPUT, out + err)
            self.assertIn("--force", err)
            self.assertEqual(e.output.read_bytes(), b"something else")
            rc, out, err = e.main("--force")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("replaced", out)
            self.assertEqual(e.output.read_bytes(), calibration.assemble_cmd02(None, LINUX_CHUNKS))
            self.assertEqual(stat.S_IMODE(e.output.stat().st_mode), 0o600)
            self.assertEqual(leftovers(e.run), [])

    def test_existing_non_regular_refused_even_forced(self):
        with env() as e:
            e.output.symlink_to(e.dir / "target")
            rc, _, err = e.main("--force")
            self.assertEqual(rc, cp.EXIT_BAD_INPUT)
            self.assertIn("not a regular file", err)
            self.assertFalse((e.dir / "target").exists())

    def test_diff_table_and_hint(self):
        with env() as e:
            rc, out, err = e.main()
            self.assertEqual(rc, 0, out + err)
            lines = out.splitlines()
            self.assertIn("bytes differing outside the four regions: 0 (expected 0)", out)
            row = {line.split("  ")[1].strip(): line for line in lines if line.startswith("  0x")}
            self.assertIn("    0 / 56", row["0x43 factory values"])  # same factory values
            self.assertNotIn("    0 / 8064", row["0x30 line update (s8)"])  # CF shifted by 3
            self.assertNotIn("    0 / 8064", row["0x48 baseline pixels (u8)"])  # BF shifted by 1
            self.assertIn("    0 / 448", row["0x48 line headers (u8)"])
            self.assertTrue(lines[-1].startswith("  sudo "), lines[-1])
            self.assertTrue(lines[-1].endswith(f"use-linux {e.run}"), lines[-1])  # absolute: outside cwd
            self.assertIn(cp.shown(cp.B2_PROGRAM), lines[-1])
            self.assertIn(cp.shown(cp.A5_VERIFY), lines[-2])
            self.assertLessEqual(len(lines), 15)

    def test_hint_quotes_the_paths(self):
        with env() as e:
            e.run = write_run(e.dir / "run with space")
            rc, out, err = e.main()
            self.assertEqual(rc, 0, out + err)
            self.assertTrue(out.splitlines()[-1].endswith(f"use-linux {shlex.quote(str(e.run))}"), out)

    def test_diff_numbers(self):
        a = calibration.assemble_cmd02(None, LINUX_CHUNKS)
        b = calibration.assemble_cmd02(None, cm.derive_chunks(FACTORY, CF, BF))
        rows, outside = cp.diff_programs(a, b)
        self.assertEqual(outside, 0)
        by = {r.label: r for r in rows}
        px = by["0x48 baseline pixels (u8)"]
        moved = sum(1 for i, x in enumerate(BF) if i % 152 >= 8 and x < 255)
        self.assertEqual((px.differing, px.total, px.max_abs), (moved, 8064, 1))
        self.assertEqual(by["0x43 factory values"].differing, 0)
        self.assertEqual(cp.factory_values(LINUX_CHUNKS.factory_entries), list(FACTORY))
        rows, outside = cp.diff_programs(a, a[:100] + bytes([a[100] ^ 1]) + a[101:])
        self.assertEqual(outside, 1)
        self.assertTrue(all(r.differing == 0 for r in rows))

    def test_serial_never_printed(self):
        forms = serial_forms()
        self.assertIn(SERIAL, windows_calibration())
        runs = []
        with env() as e:
            runs.append(e.main())
            data = e.output.read_bytes()
            for form in forms:
                self.assertNotIn(form, data)
        with env() as e:  # failure path
            e.win_program.write_bytes(e.win_program.read_bytes()[:-1] + b"\x00")
            runs.append(e.main())
        with env() as e:  # unusable CalibrationData
            e.win_cal.write_bytes(e.blob[:0x30] + bytes(64))
            runs.append(e.main())
            self.assertEqual(runs[-1][0], cp.EXIT_BAD_INPUT)
        for rc, out, err in runs:
            text = (out + err).encode("utf-8")
            for form in forms:
                self.assertNotIn(form, text)
            self.assertNotIn(SERIAL.decode("latin-1"), out + err)


@unittest.skipUnless(HAVE_TEMPLATE, "proto/data/cmd02_template.bin not present (real template, local-only)")
@unittest.skipUnless(
    REAL_PROGRAM.exists() and REAL_CAL.exists(),
    "research/calib/ours_cmd02.bin or extracted/CalibrationData_decrypted.bin not present (this unit's data, local-only)",
)
class OurUnit(unittest.TestCase):
    def test_self_check_passes(self):
        line = cp.self_check(REAL_LOAD_TEMPLATE(), REAL_PROGRAM, REAL_CAL)
        self.assertTrue(line.startswith("self-check: passed"), line)


if __name__ == "__main__":
    unittest.main()
