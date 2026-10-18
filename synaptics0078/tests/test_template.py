# SPDX-License-Identifier: MIT
"""template.build_from_dll and program_matches_template (design decisions 1 and 10).

Synthetic first: a fake DLL (random tables under the real chunk headers) with synthetic
pins must give the template restated in fixtures.expected_template. Then, where the
files exist on this machine (skipped otherwise): the gitignored proto/data template
re-derived through a DLL made of its own chunks, and the four vendor DLLs of local/b5
and driverstore/, which must give the pinned template and both pinned skeletons.
"""

from __future__ import annotations

import dataclasses
import os
import struct
import subprocess
import sys

import pytest

from synaptics0078 import calib_model as cm
from synaptics0078 import calibration, pins, template

from . import fixtures as fx

SP = fx.synthetic_pins()
REAL_TEMPLATE = calibration.TEMPLATE_PATH
VENDOR_DLLS = [
    fx.LOCAL_B5 / "x-bsdtar-2019" / "synaWudfBioUsb.dll",
    fx.LOCAL_B5 / "x-bsdtar-2020" / "synaWudfBioUsb.dll",
    fx.LOCAL_B5 / "x-origin-cfb" / "cab-1.cab.dll",
    fx.REPO / "driverstore" / "synaWudfBioUsb.dll",
]


def flip(data: bytes, offset: int) -> bytes:
    out = bytearray(data)
    out[offset] ^= 0x01
    return bytes(out)


def data_offset(program: bytes, typ: int) -> int:
    off = 5
    while off < len(program):
        t, n = struct.unpack_from("<HH", program, off)
        if t == typ:
            return off + 4
        off += 4 + n
    raise KeyError(typ)


# ------------------------------------------------------------ synthetic DLL


def test_synthetic_dll_gives_the_expected_template():
    out = template.build_from_dll(fx.synthetic_dll(), pins=SP)
    assert out == fx.expected_template()
    assert fx.sha(out) == SP.template_sha256 and len(out) == pins.TEMPLATE_LEN


def test_generated_fields_and_regions():
    out = template.build_from_dll(fx.synthetic_dll(), pins=SP)
    ts = data_offset(out, 0x34)
    assert out[ts : ts + 144] == bytes(144), "key line zeroed"
    assert out[ts + 144 : ts + 149] == fx.FAKE_CHUNKS["timeslot"][144:149], "the DLL table after the key line"
    for lo, hi in calibration.unit_region_spans(out).values():
        assert out[lo:hi] == bytes(hi - lo)
    assert calibration.split_chunks(out)[0] == bytes.fromhex("02 98 00 00 00")


def test_unit_regions_table_is_the_one_shared():
    """template.zero_unit_regions and calibration.unit_regions use calibration.UNIT_REGIONS."""
    t = fx.expected_template()
    program = calibration.assemble_cmd02(t, fx.unit_chunks())
    assert template.zero_unit_regions(program) == t
    assert [r.name for r in calibration.UNIT_REGIONS] == list(calibration.unit_region_spans(program))
    assert calibration.unit_regions(program) == fx.unit_chunks()


def test_default_pins_refuse_the_synthetic_dll():
    with pytest.raises(template.TemplateError, match="chunk reg32 "):
        template.build_from_dll(fx.synthetic_dll())


@pytest.mark.parametrize("name", [c.name for c in pins.DLL_CHUNKS])
def test_missing_chunk_is_named(name):
    with pytest.raises(template.TemplateError, match=f"no copy of chunk {name} "):
        template.build_from_dll(fx.synthetic_dll(omit=(name,)), pins=SP)


def test_template_pin_is_the_final_gate():
    with pytest.raises(template.TemplateError, match="does not have the pinned sha256"):
        template.build_from_dll(fx.synthetic_dll(), pins=dataclasses.replace(SP, template_sha256="0" * 64))


def test_error_messages_hold_no_dll_bytes():
    dll = fx.synthetic_dll(omit=("encoder",))
    with pytest.raises(template.TemplateError) as err:
        template.build_from_dll(dll, pins=SP)
    for chunk in fx.FAKE_CHUNKS.values():
        assert chunk[:8].hex() not in str(err.value)


def test_skeletons_from_the_synthetic_template():
    t = template.build_from_dll(fx.synthetic_dll(), pins=SP)
    assert cm.skeleton_sha256(cm.build_program1(t, fx.FACTORY)) == SP.program1_skeleton_sha256
    assert cm.skeleton_sha256(cm.build_program2(t, fx.FACTORY, fx.CF)) == SP.program2_skeleton_sha256


# --------------------------------------------------------- program check


def test_valid_program_passes():
    program = calibration.assemble_cmd02(fx.expected_template(), fx.unit_chunks())
    assert template.program_matches_template(program, pins=SP) == (True, None)


def test_other_unit_data_still_passes():
    other = cm.derive_chunks(bytes(0x60 + (i % 32) for i in range(56)), bytes(fx.BF), bytes(fx.CF))
    program = calibration.assemble_cmd02(fx.expected_template(), other)
    assert template.program_matches_template(program, pins=SP) == (True, None)


@pytest.mark.parametrize(
    ("typ", "at", "needle"),
    [
        (0x2E, 20, "chunk 0x2e image reconstruction differs"),
        (0x32, 10, "chunk 0x32 register list (92) differs"),
        (0x26, 12, "finger-detect threshold"),
        (0x2E, 4, "capture flags"),
        (0x34, 200, "chunk 0x34 timeslot table differs"),
        (0x30, 7, "chunk 0x30 line update differs"),
        (0x43, 2, "chunk 0x43 line transform differs"),
    ],
)
def test_flipped_fixed_byte_names_the_chunk(typ, at, needle):
    program = calibration.assemble_cmd02(fx.expected_template(), fx.unit_chunks())
    bad = flip(program, data_offset(program, typ) + at)
    ok, why = template.program_matches_template(bad, pins=SP)
    assert not ok
    assert needle in why
    assert program[5:20].hex() not in why and len(why) < 160


def test_flipped_header_and_length():
    program = calibration.assemble_cmd02(fx.expected_template(), fx.unit_chunks())
    assert template.program_matches_template(flip(program, 1), pins=SP) == (
        False,
        "the 5-byte header differs from the template",
    )
    assert template.program_matches_template(program[:-1], pins=SP)[1] == "program is 18868 bytes, need 18869"
    assert template.program_matches_template(b"\x03" + program[1:], pins=SP)[1] == "first byte is 0x03, need 0x02"


def test_with_the_template_the_offset_is_named():
    t = fx.expected_template()
    program = calibration.assemble_cmd02(t, fx.unit_chunks())
    at = data_offset(program, 0x2E) + 20
    ok, why = template.program_matches_template(flip(program, at), pins=SP, template=t)
    assert not ok and why.startswith(f"byte {at} differs from the template: chunk 0x2e, data byte 20")


def test_region_internals_are_checked():
    """Inside the per-unit regions, the 0x43 format byte and the 0x48 dimensions are fixed too."""
    program = calibration.assemble_cmd02(fx.expected_template(), fx.unit_chunks())
    c43 = data_offset(program, 0x43) + 60
    bad = bytearray(program)
    bad[c43] = bad[c43 + 39] = 0x0D
    ok, why = template.program_matches_template(bytes(bad), pins=SP)
    assert not ok and "format byte is 0x0d" in why
    ok, why = template.program_matches_template(flip(program, data_offset(program, 0x48)), pins=SP)
    assert not ok and "baseline dimensions" in why
    bad = bytearray(program)
    bad[c43 + 10] ^= 1
    ok, why = template.program_matches_template(bytes(bad), pins=SP)
    assert not ok and "entries differ" in why


def test_broken_layout():
    program = calibration.assemble_cmd02(fx.expected_template(), fx.unit_chunks())
    bad = bytearray(program)
    struct.pack_into("<H", bad, data_offset(program, 0x23) - 2, 0x7FFF)  # chunk 0x23 now overruns
    ok, why = template.program_matches_template(bytes(bad), pins=SP)
    assert not ok and "layout" in why


def test_renamed_chunk_is_reported_by_position():
    program = calibration.assemble_cmd02(fx.expected_template(), fx.unit_chunks())
    bad = bytearray(program)
    struct.pack_into("<H", bad, data_offset(program, 0x17) - 4, 0x18)  # chunk 0x17 (empty) renamed
    ok, why = template.program_matches_template(bytes(bad), pins=SP)
    assert not ok and "is 0x18 of 0 B where the template has 0x17 reply config of 0 B" in why


# ----------------------------------------------------------------- the CLI


def run_cli(*args, cwd):
    code = (
        f"import sys; sys.path.insert(0, {str(fx.REPO)!r}); "
        "from synaptics0078.template import main; sys.exit(main(sys.argv[1:]))"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", code, *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=False
    )


def test_cli_refuses_a_dll_without_the_tables(tmp_path):
    dll = tmp_path / "fake.dll"
    dll.write_bytes(fx.synthetic_dll())
    r = run_cli("--dll", str(dll), "--out", str(tmp_path / "t.bin"), cwd=tmp_path)
    assert r.returncode == 1
    assert "Could not build the template" in r.stderr and "chunk reg32" in r.stderr
    assert not (tmp_path / "t.bin").exists()
    assert fx.FAKE_CHUNKS["reg32"].hex() not in r.stdout + r.stderr


def test_cli_missing_dll(tmp_path):
    r = run_cli("--dll", str(tmp_path / "none.dll"), "--out", str(tmp_path / "t.bin"), cwd=tmp_path)
    assert r.returncode == 1 and "Could not read the DLL" in r.stderr


def synthetic_cli(monkeypatch, tmp_path):
    """template.main in this process, building from the synthetic DLL with the synthetic pins."""
    dll = tmp_path / "fake.dll"
    dll.write_bytes(fx.synthetic_dll())
    real = template.build_from_dll
    monkeypatch.setattr(template, "build_from_dll", lambda data: real(data, pins=SP))
    return lambda out, *more: template.main(["--dll", str(dll), "--out", str(out), *more])


def test_cli_creates_the_missing_output_directory(tmp_path, monkeypatch, capsys):
    """A fresh checkout has no proto/data: --out proto/data/cmd02_template.bin makes that one directory."""
    cli = synthetic_cli(monkeypatch, tmp_path)
    (tmp_path / "proto").mkdir()
    out = tmp_path / "proto" / "data" / "cmd02_template.bin"
    assert cli(out) == 0
    assert out.read_bytes() == fx.expected_template()
    assert (out.parent.stat().st_mode & 0o7777) == 0o755 & ~current_umask()
    stdout = capsys.readouterr().out
    assert stdout.splitlines()[0] == f"Created the directory {out.parent}."
    assert stdout.splitlines()[1].startswith("Template written: ")


def test_cli_creates_only_the_immediate_parent(tmp_path, monkeypatch, capsys):
    cli = synthetic_cli(monkeypatch, tmp_path)
    out = tmp_path / "no such" / "data" / "t.bin"
    assert cli(out) == 1
    err = capsys.readouterr().err
    assert "does not exist" in err and err.rstrip().endswith(f"mkdir -p '{out.parent}'")
    assert not (tmp_path / "no such").exists()
    (tmp_path / "file").write_bytes(b"")
    assert cli(tmp_path / "file" / "t.bin") == 1
    assert "exists and is not a directory" in capsys.readouterr().err


def current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


@pytest.mark.skipif(not VENDOR_DLLS[0].exists(), reason="local/b5 vendor DLL not present")
def test_cli_writes_and_refuses_to_overwrite(tmp_path):
    out = tmp_path / "cmd02_template.bin"
    r = run_cli("--dll", str(VENDOR_DLLS[0]), "--out", str(out), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("Template written: ") and "sha256 matches the pin" in r.stdout
    assert fx.sha(out.read_bytes()) == pins.TEMPLATE_SHA256
    if REAL_TEMPLATE.exists():
        assert out.read_bytes() == REAL_TEMPLATE.read_bytes()
    r = run_cli("--dll", str(VENDOR_DLLS[0]), "--out", str(out), cwd=tmp_path)
    assert r.returncode == 2 and "--force" in r.stderr
    out.write_bytes(b"old")
    r = run_cli("--dll", str(VENDOR_DLLS[0]), "--out", str(out), "--force", cwd=tmp_path)
    assert r.returncode == 0 and fx.sha(out.read_bytes()) == pins.TEMPLATE_SHA256
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cmd02_template.bin"]


# ---------------------------------------------------------- local files


@pytest.mark.skipif(
    not REAL_TEMPLATE.exists(),
    reason="proto/data/cmd02_template.bin (the real template, gitignored) not present; local-only",
)
def test_real_template_rederived_through_its_own_chunks():
    """A DLL made of the real template's own chunks (patch bytes scrambled) gives the real template.

    The chunk bytes come from the gitignored template file at run time, never from this
    source. This checks every generated field against the real template.
    """
    real = REAL_TEMPLATE.read_bytes()
    _, chunks = calibration.split_chunks(real)
    order = [("reg32", 1), ("list16_92", 2), ("list32_40", 3), ("list16_64", 4), ("list32_868", 5)]
    order += [("list16_12", 6), ("timeslot", 7), ("encoder", 8)]
    fake = {name: chunks[i][1] for name, i in order}
    ts = bytearray(fake["timeslot"])
    ts[0:144] = fx.rand("key line", 144)
    for off, _ in template.TIMESLOT_PATCH:
        ts[off] ^= 0x5A
    fake["timeslot"] = bytes(ts)
    p = fx.synthetic_pins(tmpl=real, fake=fake)
    assert p.dll_chunks[:6] == pins.DLL_CHUNKS[:6] and p.dll_chunks[7] == pins.DLL_CHUNKS[7]
    assert (p.template_header_sha256, p.template_chunks) == (pins.TEMPLATE_HEADER_SHA256, pins.TEMPLATE_CHUNKS)
    assert template.build_from_dll(fx.synthetic_dll(fake=fake), pins=p) == real


@pytest.mark.skipif(
    not REAL_TEMPLATE.exists(),
    reason="proto/data/cmd02_template.bin (the real template, gitignored) not present; local-only",
)
def test_real_template_program_check():
    real = REAL_TEMPLATE.read_bytes()
    program = calibration.assemble_cmd02(real, fx.unit_chunks())
    assert template.program_matches_template(program) == (True, None)
    ok, why = template.program_matches_template(flip(program, data_offset(program, 0x2E) + 20))
    assert not ok and "chunk 0x2e image reconstruction differs" in why
    assert calibration.program_problem(program) is None


@pytest.mark.parametrize("path", VENDOR_DLLS, ids=lambda p: "/".join(p.parts[-2:]))
def test_vendor_dll_gives_the_pinned_template_and_skeletons(path):
    if not path.exists():
        pytest.skip(f"{path} not present")
    t = template.build_from_dll(path.read_bytes())
    assert fx.sha(t) == pins.TEMPLATE_SHA256
    assert cm.skeleton_sha256(cm.build_program1(t, fx.FACTORY)) == pins.PROGRAM1_SKELETON_SHA256
    assert cm.skeleton_sha256(cm.build_program2(t, fx.FACTORY, fx.CF)) == pins.PROGRAM2_SKELETON_SHA256
    assert template.program_matches_template(calibration.assemble_cmd02(t, fx.unit_chunks())) == (True, None)
