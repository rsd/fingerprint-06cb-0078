"""Tests of tools/synaptics0078_data.py.

Everything but the last two tests runs on synthetic bytes: the module hash check is
pointed at a synthetic module's hash with monkeypatch, and the program is built on the
synthetic template (synaptics0078/tests/fixtures.py). The real-input tests need
the private vendor DLL and decrypted CalibrationData (this machine's owner only,
local-only) and are skipped without them; they never print or store the serial.
Their synthetic counterparts are test_build_data_on_synthetic_inputs and
test_main_on_synthetic_inputs: the same build_data and main, on a synthetic DLL and blob.
"""

import hashlib
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest
import synaptics0078_data as sd

from synaptics0078 import calibration
from synaptics0078.tests import fixtures as fx

HERE = Path(__file__).resolve().parent
SERIAL_RAW = bytes.fromhex("12ab30c4de5f")  # synthetic, not a real unit's
SERIAL = "21ba034cedf5"
MODULE = bytes(range(256)) * 41 + bytes(4)  # 10500 synthetic bytes
PROGRAM = b"\x02" + bytes(sd.PROGRAM_LEN - 1)
PROGRAM_2 = b"\x02" + b"\x01" * (sd.PROGRAM_LEN - 1)

HAVE_REAL = sd.DEFAULT_DLL.exists() and sd.DEFAULT_CALIBRATION.exists()


@pytest.fixture
def synthetic_module(monkeypatch):
    """Make the pinned module hash the synthetic module's for the test's duration."""
    monkeypatch.setattr(sd.capture, "MODULE_SHA256", hashlib.sha256(MODULE).hexdigest())
    return MODULE


def synthetic_inputs(tmp_path, monkeypatch):
    """(dll path, CalibrationData path, CalibrationInputs): a synthetic DLL and blob, synthetic template."""
    monkeypatch.setattr(sd.capture, "MODULE_SHA256", hashlib.sha256(MODULE).hexdigest())
    monkeypatch.setattr(calibration, "load_template", lambda path=None: fx.expected_template())
    dll = tmp_path / "synthetic.dll"
    dll.write_bytes(bytes(sd.capture.MODULE_OFFSET) + MODULE)  # the module where the real DLL has it
    inputs = calibration.CalibrationInputs(
        bytes(0x70 + (i * 7) % 32 for i in range(56)),  # packs to exactly 5 bits
        bytes((0x80 + ((i * 37) % 61) - 30) & 0xFF for i in range(calibration.FRAME_LEN)),
        bytes(128 + (i % 5) - 2 for i in range(calibration.FRAME_LEN)),
        SERIAL_RAW,
    )
    blob = bytearray(calibration.MIN_CAL_LEN)
    struct.pack_into("<III", blob, 4, 0xE4, 2, 0x2449)
    struct.pack_into("<II", blob, calibration.CAL_STRUCT + 0x70, 56, 0)
    struct.pack_into("<I", blob, calibration.CAL_STRUCT + 0x88, calibration.FRAME_LEN)
    for desc in (0x2294, 0x22B4):
        struct.pack_into("<II", blob, desc + 4, calibration.BPL, calibration.LINES)
    blob[calibration.SERIAL_OFFSET : calibration.SERIAL_OFFSET + 6] = inputs.serial
    blob[calibration.FACTORY_OFFSET : calibration.FACTORY_OFFSET + 56] = inputs.factory
    blob[calibration.FRAME_OFFSET : calibration.FRAME_OFFSET + calibration.FRAME_LEN] = inputs.frame
    blob[calibration.BASELINE_OFFSET : calibration.BASELINE_OFFSET + calibration.FRAME_LEN] = inputs.baseline
    cal = tmp_path / "CalibrationData_synthetic.bin"
    cal.write_bytes(bytes(blob))
    return dll, cal, inputs


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ------------------------------------------------------------------ naming


def test_serial_hex_swaps_each_bytes_nibbles():
    assert len(MODULE) == 10500
    assert sd.serial_hex(SERIAL_RAW) == SERIAL
    assert sd.serial_hex(SERIAL_RAW).startswith("21ba03")  # synthetic 12 ab 30 -> 21 ba 03
    assert sd.serial_hex(bytes.fromhex("0f f0 00 ff 01 10")) == "f00f00ff1001"


@pytest.mark.parametrize("raw", [b"", bytes(5), bytes(7)])
def test_serial_hex_needs_six_bytes(raw):
    with pytest.raises(sd.DataError, match="expected 6"):
        sd.serial_hex(raw)


def test_program_name():
    assert sd.program_name(SERIAL) == f"program-{SERIAL}.bin"


@pytest.mark.parametrize("bad", ["", "21BA034CEDF5", "21ba034cedf", "21ba034cedf5a", "21ba034cedfg", "../../etc/x"])
def test_program_name_rejects_malformed_serials(bad):
    with pytest.raises(sd.DataError):
        sd.program_name(bad)


# -------------------------------------------------------------- validation


def test_validate_accepts_good_data(synthetic_module):
    sd.validate(synthetic_module, PROGRAM, SERIAL)


def test_validate_rejects_module_with_wrong_hash():
    with pytest.raises(sd.calibration.CalibrationError, match="sha256"):
        sd.validate(MODULE, PROGRAM, SERIAL)


def test_validate_rejects_module_with_wrong_size(synthetic_module):
    with pytest.raises(sd.calibration.CalibrationError, match="expected 10500"):
        sd.validate(synthetic_module[:-1], PROGRAM, SERIAL)


@pytest.mark.parametrize("program", [PROGRAM[:-1], PROGRAM + b"\x00", b""])
def test_validate_rejects_program_with_wrong_size(synthetic_module, program):
    with pytest.raises(sd.DataError, match="expected 18869"):
        sd.validate(synthetic_module, program, SERIAL)


def test_validate_rejects_program_without_02_opcode(synthetic_module):
    with pytest.raises(sd.DataError, match="opcode"):
        sd.validate(synthetic_module, b"\x06" + PROGRAM[1:], SERIAL)


def test_validate_rejects_bad_serial(synthetic_module):
    with pytest.raises(sd.DataError, match="12 lowercase hex"):
        sd.validate(synthetic_module, PROGRAM, SERIAL.upper())


# ----------------------------------------------------------------- writing


def test_write_data_names_modes_and_contents(tmp_path, synthetic_module):
    out = tmp_path / "a" / "synaptics0078"
    paths = sd.write_data(out, synthetic_module, PROGRAM, SERIAL)
    assert paths == [out / "module.bin", out / f"program-{SERIAL}.bin"]
    assert mode(out) == 0o700
    assert [mode(p) for p in paths] == [0o600, 0o600]
    assert paths[0].read_bytes() == synthetic_module
    assert paths[1].read_bytes() == PROGRAM
    assert sorted(p.name for p in out.iterdir()) == sorted(p.name for p in paths)  # no temp files left


def test_write_data_refuses_overwrite_without_force(tmp_path, synthetic_module):
    sd.write_data(tmp_path, synthetic_module, PROGRAM, SERIAL)
    with pytest.raises(sd.DataError, match="--force"):
        sd.write_data(tmp_path, synthetic_module, PROGRAM_2, SERIAL)
    assert (tmp_path / f"program-{SERIAL}.bin").read_bytes() == PROGRAM


def test_write_data_refuses_when_only_module_exists(tmp_path, synthetic_module):
    (tmp_path / "module.bin").write_bytes(b"old")
    with pytest.raises(sd.DataError, match="module.bin"):
        sd.write_data(tmp_path, synthetic_module, PROGRAM, SERIAL)
    assert (tmp_path / "module.bin").read_bytes() == b"old"
    assert not (tmp_path / f"program-{SERIAL}.bin").exists()  # nothing written


def test_write_data_overwrites_with_force(tmp_path, synthetic_module):
    sd.write_data(tmp_path, synthetic_module, PROGRAM, SERIAL)
    paths = sd.write_data(tmp_path, synthetic_module, PROGRAM_2, SERIAL, force=True)
    assert paths[1].read_bytes() == PROGRAM_2
    assert mode(paths[1]) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in paths)


def test_write_data_validates_before_touching_disk(tmp_path, synthetic_module):
    out = tmp_path / "new"
    with pytest.raises(sd.DataError):
        sd.write_data(out, synthetic_module, PROGRAM[:-1], SERIAL)
    assert not out.exists()


def test_write_data_refuses_a_file_as_out_dir(tmp_path, synthetic_module):
    (tmp_path / "f").write_bytes(b"")
    with pytest.raises(sd.DataError, match="not a directory"):
        sd.write_data(tmp_path / "f", synthetic_module, PROGRAM, SERIAL)


def test_write_data_counts_a_dangling_symlink_as_present(tmp_path, synthetic_module):
    (tmp_path / f"program-{SERIAL}.bin").symlink_to(tmp_path / "nowhere")
    with pytest.raises(sd.DataError, match=f"program-{SERIAL}.bin"):
        sd.write_data(tmp_path, synthetic_module, PROGRAM, SERIAL)
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"program-{SERIAL}.bin"]  # nothing written
    assert not (tmp_path / "nowhere").exists()


def test_write_data_rolls_back_module_when_the_program_cannot_be_published(tmp_path, synthetic_module, monkeypatch):
    """A program that appears between the check and the link: module.bin must not stay behind."""
    real_link = sd.os.link

    def link(src, dst, **kwargs):
        if Path(dst).name.startswith("program-"):
            raise FileExistsError(dst)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(sd.os, "link", link)
    with pytest.raises(sd.DataError, match="--force"):
        sd.write_data(tmp_path, synthetic_module, PROGRAM, SERIAL)
    assert list(tmp_path.iterdir()) == []  # module.bin rolled back, no temp files


def test_write_data_with_force_restores_both_files_when_the_second_fails(tmp_path, synthetic_module, monkeypatch):
    sd.write_data(tmp_path, synthetic_module, PROGRAM, SERIAL)
    old_module = tmp_path / "module.bin"
    old_module.write_bytes(b"old module")  # distinguishable from the new one
    real_replace = sd.os.replace

    def replace(src, dst, **kwargs):
        if Path(dst).name.startswith("program-"):
            raise PermissionError(dst)
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(sd.os, "replace", replace)
    with pytest.raises(PermissionError):
        sd.write_data(tmp_path, synthetic_module, PROGRAM_2, SERIAL, force=True)
    assert old_module.read_bytes() == b"old module"
    assert (tmp_path / f"program-{SERIAL}.bin").read_bytes() == PROGRAM
    assert sorted(p.name for p in tmp_path.iterdir()) == ["module.bin", f"program-{SERIAL}.bin"]


def test_write_data_refuses_a_symlinked_out_dir(tmp_path, synthetic_module):
    (tmp_path / "real").mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(sd.DataError, match="symlink"):
        sd.write_data(tmp_path / "link", synthetic_module, PROGRAM, SERIAL)
    assert list((tmp_path / "real").iterdir()) == []


def test_write_data_tightens_a_loose_out_dir(tmp_path, synthetic_module, capsys):
    out = tmp_path / "loose"
    out.mkdir()
    out.chmod(0o755)
    sd.write_data(out, synthetic_module, PROGRAM, SERIAL)
    assert mode(out) == 0o700
    assert "was mode 0755, set to 0700" in capsys.readouterr().err


def test_write_data_leaves_a_tight_out_dir_alone(tmp_path, synthetic_module, capsys):
    out = tmp_path / "tight"
    out.mkdir()
    out.chmod(0o700)
    sd.write_data(out, synthetic_module, PROGRAM, SERIAL)
    assert mode(out) == 0o700
    assert capsys.readouterr().err == ""


def test_write_data_refusal_does_not_touch_the_out_dir_mode(tmp_path, synthetic_module):
    out = tmp_path / "loose"
    out.mkdir()
    out.chmod(0o755)
    (out / "module.bin").write_bytes(b"old")
    with pytest.raises(sd.DataError, match="already present"):
        sd.write_data(out, synthetic_module, PROGRAM, SERIAL)
    assert mode(out) == 0o755


# --------------------------------------------------------------------- CLI


def test_main_reports_missing_inputs(tmp_path, capsys):
    rc = sd.main(["--dll", str(tmp_path / "no.dll"), "--calibration", str(tmp_path / "no.bin"), "--out", str(tmp_path)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_main_ends_with_the_root_install_step(tmp_path, synthetic_module, monkeypatch, capsys):
    monkeypatch.setattr(sd, "build_data", lambda dll, cal: (synthetic_module, PROGRAM, SERIAL))
    out = tmp_path / "data dir"
    assert sd.main(["--out", str(out)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == f"next step: sudo synaptics0078-setup install --from '{out.resolve()}'"


# Imports the helper with -I in a fresh interpreter, then lists every module the
# import added that neither lives in the project tree (outside proto/.venv) nor
# is in sys.stdlib_module_names.
NON_STDLIB_CHECK = """
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "tools"))
before = set(sys.modules)
import synaptics0078_data
for extra in sys.argv[2:]:
    __import__(extra)

def in_project(mod):  # the project's own sources; proto/.venv's site-packages do not count
    f = getattr(mod, "__file__", None)
    if f is None or not Path(f).resolve().is_relative_to(root):
        return False
    parts = Path(f).resolve().relative_to(root).parts
    return ".venv" not in parts and "site-packages" not in parts

added = sorted(set(sys.modules) - before)
bad = [m for m in added if not in_project(sys.modules[m]) and m.partition(".")[0] not in sys.stdlib_module_names]
print(bad)
sys.exit(1 if bad else 0)
"""


def non_stdlib_imports(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-c", NON_STDLIB_CHECK, str(HERE.parent), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def test_import_pulls_in_only_stdlib_and_project_modules():
    """A6 runs stdlib-only: importing the helper must import nothing from outside the stdlib and the project."""
    proc = non_stdlib_imports()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "[]"


def test_the_stdlib_check_catches_a_third_party_import():
    pytest.importorskip("usb")  # pyusb, in proto's venv
    proc = non_stdlib_imports("usb")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "'usb'" in proc.stdout


def test_constants_match_proto():
    assert sd.capture.MODULE_LEN == 10500
    assert sd.capture.MODULE_OFFSET == 0x19CCA0
    assert sd.capture.MODULE_SHA256.startswith("67f4a332") and sd.capture.MODULE_SHA256.endswith("caacc1")
    assert sd.PROGRAM_LEN == 18869
    assert sd.PROGRAM_OPCODE == 0x02
    assert sd.DEFAULT_DLL == sd.capture.DEFAULT_DLL


# ------------------------------------------- the real-input tests, synthetic


def test_build_data_on_synthetic_inputs(tmp_path, monkeypatch):
    dll, cal, inputs = synthetic_inputs(tmp_path, monkeypatch)
    module, program, serial = sd.build_data(dll, cal)
    assert module == MODULE
    assert len(program) == 18869
    assert program[0] == 0x02
    assert program == calibration.build_cmd02(inputs, fx.expected_template())
    assert sd.SERIAL_HEX_RE.match(serial)
    assert serial == SERIAL


def test_main_on_synthetic_inputs(tmp_path, monkeypatch, capsys):
    dll, cal, _ = synthetic_inputs(tmp_path, monkeypatch)
    out = tmp_path / "data"
    args = ["--dll", str(dll), "--calibration", str(cal), "--out", str(out)]
    assert sd.main(args) == 0
    names = sorted(p.name for p in out.iterdir())
    assert names == ["module.bin", f"program-{SERIAL}.bin"]
    assert (out / "module.bin").read_bytes() == MODULE
    assert (out / names[1]).stat().st_size == 18869
    assert sd.main(args) == 1  # refuses to overwrite
    assert sd.main([*args, "--force"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == sd.next_step_line(out)


# ----------------------------------------------------- real private inputs


@pytest.mark.skipif(
    not HAVE_REAL, reason="driverstore DLL or extracted CalibrationData not present (this unit's data, local-only)"
)
def test_build_data_on_the_real_inputs():
    module, program, serial = sd.build_data(sd.DEFAULT_DLL, sd.DEFAULT_CALIBRATION)
    assert len(module) == 10500
    assert hashlib.sha256(module).hexdigest() == sd.capture.MODULE_SHA256
    assert len(program) == 18869
    assert program[0] == 0x02
    assert sd.SERIAL_HEX_RE.match(serial)


@pytest.mark.skipif(
    not HAVE_REAL, reason="driverstore DLL or extracted CalibrationData not present (this unit's data, local-only)"
)
def test_main_on_the_real_inputs(tmp_path, capsys):
    out = tmp_path / "data"
    assert sd.main(["--out", str(out)]) == 0
    names = sorted(p.name for p in out.iterdir())
    assert len(names) == 2 and names[0] == "module.bin" and names[1].startswith("program-")
    assert (out / "module.bin").stat().st_size == 10500
    assert (out / names[1]).stat().st_size == 18869
    assert sd.main(["--out", str(out)]) == 1  # refuses to overwrite
    assert sd.main(["--out", str(out), "--force"]) == 0
    capsys.readouterr()  # keep the serial-derived name out of the test report
    for path in out.iterdir():  # pytest keeps old tmp dirs; leave no private bytes behind
        path.unlink()
