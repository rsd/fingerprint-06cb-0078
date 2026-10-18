# SPDX-License-Identifier: MIT
"""vendor.py: module scan, containers (with fixtures written here), the unprivileged-child protocol.

Containers are built by the tiny writers in fixtures.py and read through the real
/usr/bin/bsdtar (skipped without it). With local/b5 present (skipped otherwise), the
three real archives are checked against their pins, then must give the pinned module
and template.
"""

from __future__ import annotations

import io
import os
import struct
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from synaptics0078 import pins, vendor

from . import fixtures as fx

SP = fx.synthetic_pins()
needs_bsdtar = pytest.mark.skipif(not fx.BSDTAR.exists(), reason="/usr/bin/bsdtar not present")
DLL = fx.synthetic_dll()


def assert_complete(ex: vendor.Extracted, p=SP):
    assert ex.module is not None and fx.sha(ex.module) == p.module_sha256
    assert ex.template is not None and fx.sha(ex.template) == p.template_sha256
    assert ex.complete


# ----------------------------------------------------------- module scan


def blob_with(module_at: dict[int, bytes], size: int = 0x8000) -> bytes:
    out = bytearray(fx.rand("scan", size))
    for off, data in module_at.items():
        out[off : off + len(data)] = data
    return bytes(out)


@pytest.mark.parametrize("offset", [0x1000, 0x1003, 0])
def test_module_found_at_aligned_and_odd_offsets(offset):
    assert vendor.find_module(blob_with({offset: fx.MODULE}), SP) == offset


def test_odd_offset_needs_the_stride_1_pass():
    data = blob_with({0x1003: fx.MODULE})
    assert vendor.find_module(data, SP, strides=(16,)) is None
    assert vendor.find_module(data, SP, strides=(16, 1)) == 0x1003


def test_module_at_the_very_end():
    data = blob_with({}) + fx.MODULE
    assert vendor.find_module(data, SP) == 0x8000


def test_module_absent():
    assert vendor.find_module(blob_with({}), SP) is None
    assert vendor.find_module(fx.MODULE[:-1], SP) is None
    assert vendor.find_module(fx.MODULE, pins.DEFAULT_PINS) is None


def test_head_matches_but_body_does_not():
    fake = fx.MODULE[:32] + fx.rand("other body", pins.MODULE_LEN - 32)
    assert vendor.find_module(blob_with({0x1000: fake}), SP) is None
    assert vendor.find_module(blob_with({0x1000: fake, 0x5000: fx.MODULE}), SP) == 0x5000


# ---------------------------------------------------------- detection


def test_detect():
    t = fx.expected_template()
    assert vendor.detect(fx.make_cab([("a", b"x")])) == "cab"
    assert vendor.detect(fx.make_zip([("a", b"x")])) == "zip"
    assert vendor.detect(fx.make_ole([("a", bytes(4096))])) == "ole"
    assert vendor.detect(DLL) == "pe"
    assert vendor.detect(fx.MODULE, SP) == "module"
    assert vendor.detect(t, SP) == "template"
    assert vendor.detect(fx.MODULE) is None  # not the pinned module
    assert vendor.detect(b"hello") is None
    assert vendor.is_dll(DLL) and not vendor.is_dll(fx.fake_setup_exe(b"")) and not vendor.is_dll(b"MZ")


def test_bare_files():
    ex = vendor.extract(fx.MODULE, name="module.bin", pins=SP)
    assert fx.sha(ex.module) == SP.module_sha256 and ex.template is None
    assert ex.provenance == ["module.bin (module): the bare module"]
    ex = vendor.extract(fx.expected_template(), pins=SP)
    assert ex.module is None and fx.sha(ex.template) == SP.template_sha256
    ex = vendor.extract(b"not an archive", name="x.txt", pins=SP)
    assert (ex.module, ex.template) == (None, None)
    assert ex.provenance == ["x.txt: not a cab, zip, exe, MSI, DLL, module or template"]


def test_bare_dll():
    ex = vendor.extract(DLL, name="synaWudfBioUsb.dll", pins=SP)
    assert_complete(ex)
    assert ex.module_source == "synaWudfBioUsb.dll (PE) -> module at 0x9000"
    assert ex.template_source == "synaWudfBioUsb.dll (PE) -> template built, sha256 matches the pin"


def test_dll_with_module_but_no_template():
    ex = vendor.extract(fx.synthetic_dll(omit=("encoder",)), pins=SP)
    assert ex.module is not None and ex.template is None
    assert any("no template (the DLL has no copy of chunk encoder" in line for line in ex.provenance)


def test_too_large_input_is_refused(monkeypatch):
    monkeypatch.setattr(vendor, "MAX_INPUT", 1000)
    with pytest.raises(vendor.VendorError, match="larger than"):
        vendor.extract(DLL, pins=SP)


# ---------------------------------------------------------- containers


@needs_bsdtar
def test_stored_cab_through_bsdtar():
    cab = fx.make_cab([("readme.txt", b"synthetic\n"), ("synaWudfBioUsb.dll", DLL)])
    listed = subprocess.run([str(fx.BSDTAR), "-tf", "-"], input=cab, capture_output=True, check=True).stdout
    assert listed.split() == [b"readme.txt", b"synaWudfBioUsb.dll"]
    ex = vendor.extract(cab, name="x.cab", pins=SP)
    assert_complete(ex)
    assert ex.module_source == "x.cab (cab) -> synaWudfBioUsb.dll -> module at 0x9000"
    assert len(ex.provenance) == 2, "the DLL member is read first, then the walk stops"


@needs_bsdtar
def test_member_names_are_literal_patterns():
    """A member called "*" is asked for literally, not as a glob that would match the DLL."""
    cab = fx.make_cab([("synaWudfBioUsb.dll", b"MZ junk"), ("*", DLL)])
    assert_complete(vendor.extract(cab, pins=SP))
    cab = fx.make_cab([("-q", DLL)])
    ex = vendor.extract(cab, pins=SP)
    assert ex.module is None
    assert any("skipped, a name bsdtar cannot be asked for safely" in line for line in ex.provenance)


@needs_bsdtar
def test_origin_style_chain():
    """zip -> setup exe -> OLE (scattered sectors) -> stored cab -> DLL."""
    ex = vendor.extract(fx.origin_style_zip(DLL), name="02_Finger.zip", pins=SP)
    assert_complete(ex)
    assert ex.module_source == (
        "02_Finger.zip (zip) -> 02_Finger/Synaptics/Setup.exe -> OLE at 0x1058 -> stream 'cab stream'"
        " -> _synaWudfBioUsb_dll_x64 -> module at 0x9000"
    )


@needs_bsdtar
def test_msi_and_setup_exe_alone():
    cab = fx.make_cab([("_synaWudfBioUsb_dll_x64", DLL)])
    assert_complete(vendor.extract(fx.make_ole([("c", cab)]), pins=SP))
    assert_complete(vendor.extract(fx.fake_setup_exe(fx.make_ole([("c", cab)])), pins=SP))


@needs_bsdtar
def test_cab_stored_as_is_in_an_exe():
    exe = fx.fake_setup_exe(b"") + fx.make_cab([("synaWudfBioUsb.dll", DLL)]) + b"tail"
    ex = vendor.extract(exe, pins=SP)
    assert_complete(ex)
    assert "-> cab at 0x" in ex.module_source


def test_member_size_cap(monkeypatch):
    monkeypatch.setattr(vendor, "MAX_MEMBER_BYTES", 1000)
    ex = vendor.extract(fx.make_zip([("big.dll", DLL)]), pins=SP)
    assert ex.module is None
    assert any("skipped, larger than" in line for line in ex.provenance)


def test_depth_cap():
    data = DLL
    for i in range(vendor.MAX_DEPTH + 1):
        data = fx.make_zip([(f"level{i}.zip", data)])
    ex = vendor.extract(data, pins=SP)
    assert ex.module is None
    assert any("nested deeper than" in line for line in ex.provenance)


def test_total_bytes_cap(monkeypatch):
    monkeypatch.setattr(vendor, "MAX_TOTAL_BYTES", len(DLL) + 10)
    ex = vendor.extract(fx.make_zip([("a.dll", fx.rand("junk", len(DLL))), ("b.dll", DLL)]), pins=SP)
    assert ex.module is None
    assert any("stopped, more than" in line for line in ex.provenance)


def test_member_count_cap(monkeypatch):
    monkeypatch.setattr(vendor, "MAX_MEMBERS", 2)
    ex = vendor.extract(fx.make_zip([("a.txt", b"a"), ("b.txt", b"b"), ("z.dll", DLL)]), pins=SP)
    assert any("only the first 2 are read" in line for line in ex.provenance)


def test_broken_zip_and_ole_are_notes_not_errors():
    ex = vendor.extract(b"PK\x03\x04" + bytes(100), pins=SP)
    assert ex.provenance and "not a readable zip" in ex.provenance[0]
    ex = vendor.extract(fx.make_ole([("c", bytes(5000))])[:600], pins=SP)
    assert "skipped" in ex.provenance[0]


def test_names_are_sanitised():
    ex = vendor.extract(fx.make_zip([("a\x1b[31mred\u00e9.dll", DLL)]), name="z\x07.zip", pins=SP)
    assert ex.module_source == "z?.zip (zip) -> a?[31mred?.dll -> module at 0x9000"
    ex = vendor.extract(b"x", name="evil\x1b]0;title\x07", pins=SP)
    assert ex.provenance == ["evil?]0;title?: not a cab, zip, exe, MSI, DLL, module or template"]


def test_missing_bsdtar_is_an_error(tmp_path):
    with pytest.raises(vendor.VendorError, match="not found"):
        vendor.extract(fx.make_cab([("a", b"x")]), pins=SP, bsdtar=str(tmp_path / "bsdtar"))


@needs_bsdtar
def test_bsdtar_output_is_capped(monkeypatch):
    monkeypatch.setattr(vendor, "MAX_MEMBER_BYTES", 1000)
    ex = vendor.extract(fx.make_cab([("synaWudfBioUsb.dll", DLL)]), pins=SP)
    assert ex.module is None
    assert any(line.endswith("cannot extract it: bsdtar wrote more than 1000 bytes") for line in ex.provenance)


@needs_bsdtar
def test_cab_member_count_cap(monkeypatch):
    monkeypatch.setattr(vendor, "MAX_MEMBERS", 2)
    cab = fx.make_cab([("a.txt", b"a"), ("b.txt", b"b"), ("synaWudfBioUsb.dll", DLL)])
    ex = vendor.extract(cab, pins=SP)
    assert any("3 members, only the first 2 are read" in line for line in ex.provenance)


def test_setup_exe_gets_the_stride_1_pass_too():
    exe = fx.fake_setup_exe(b"") + bytes(3) + fx.MODULE + bytes(64)
    off = len(exe) - 64 - len(fx.MODULE)
    assert off % 16 and vendor.find_module(exe, SP, (16,)) is None
    ex = vendor.extract(exe, name="Setup.exe", pins=SP)
    assert fx.sha(ex.module) == SP.module_sha256
    assert ex.module_source == f"Setup.exe (PE) -> module at 0x{off:x}"


def test_setup_exe_template_is_tried_without_the_module():
    ex = vendor.extract(fx.fake_setup_exe(b"") + fx.synthetic_dll(module=None), pins=SP)
    assert ex.module is None and fx.sha(ex.template) == SP.template_sha256


def test_cabs_cut_out_of_an_exe_count_against_the_total(monkeypatch):
    cab = fx.make_cab([("synaWudfBioUsb.dll", DLL)])
    monkeypatch.setattr(vendor, "MAX_TOTAL_BYTES", len(cab) - 1)
    ex = vendor.extract(fx.fake_setup_exe(b"") + cab, pins=SP, bsdtar="/nonexistent/bsdtar")  # never run
    assert ex.module is None
    assert any("-> cab at 0x" in line and "stopped, more than" in line for line in ex.provenance)


def test_ole_streams_count_against_what_is_left_of_the_total(monkeypatch):
    cab = fx.make_cab([("synaWudfBioUsb.dll", DLL)])
    monkeypatch.setattr(vendor, "MAX_TOTAL_BYTES", len(cab) - 1)
    ex = vendor.extract(fx.make_ole([("c", cab)]), pins=SP, bsdtar="/nonexistent/bsdtar")  # never run
    assert ex.module is None
    assert ex.provenance == ["OLE: stopped, more than 0 MB read out of containers"]


# ----------------------------------------------- run_capped and its process group


def script(tmp_path, name: str, body: str) -> str:
    """An executable /bin/sh script in the test's own directory; its background job's pid goes to name.pid."""
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body.replace('PIDFILE', str(tmp_path / (name + '.pid')))}\n", encoding="ascii")
    path.chmod(0o755)
    return str(path)


def background_pid(tmp_path, name: str) -> int:
    deadline = time.monotonic() + 5
    pidfile = tmp_path / (name + ".pid")
    while not (pidfile.exists() and pidfile.read_text().strip()):
        assert time.monotonic() < deadline, "the script never wrote its pid"
        time.sleep(0.01)
    return int(pidfile.read_text())


def gone(pid: int, wait: float = 5.0) -> bool:
    """True once `pid` no longer runs (absent, or a zombie waiting for init to reap it)."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            state = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state in ("Z", "X"):
            return True
        time.sleep(0.02)
    return False


BACKGROUND = "sleep 60 </dev/null >/dev/null 2>&1 &\necho $! > PIDFILE"


def test_fake_bsdtar_that_hangs_times_out_and_its_group_dies(tmp_path, monkeypatch):
    monkeypatch.setattr(vendor, "BSDTAR_TIMEOUT_S", 1)
    bsdtar = script(tmp_path, "bsdtar", BACKGROUND + "\nexec sleep 60")
    started = time.monotonic()
    ex = vendor.extract(fx.make_cab([("a", b"x")]), pins=SP, bsdtar=bsdtar)
    assert time.monotonic() - started < 10
    assert any("cannot list it: bsdtar took longer than 1 s" in line for line in ex.provenance)
    assert gone(background_pid(tmp_path, "bsdtar")), "the hung tool's background job survived"


def test_run_capped_kills_the_group_after_a_normal_exit(tmp_path):
    tool = script(tmp_path, "tool", BACKGROUND + "\ncat >/dev/null\necho done")
    rc, out, err = vendor.run_capped([tool], b"input", 1000, 30)
    assert (rc, out, err) == (0, b"done\n", "")
    assert gone(background_pid(tmp_path, "tool"))


def test_run_capped_kills_the_group_past_the_cap(tmp_path):
    tool = script(tmp_path, "tool", BACKGROUND + "\nexec head -c 100000 /dev/zero")
    with pytest.raises(vendor.VendorError, match="^tool wrote more than 1000 bytes$"):
        vendor.run_capped([tool], b"", 1000, 30)
    assert gone(background_pid(tmp_path, "tool"))


def test_over_cap_message_for_the_child_output_cap(tmp_path):
    """The setup tool's call: cap MAX_CHILD_OUTPUT, the message names the tool, not a member."""
    tool = script(tmp_path, "setpriv", f"exec head -c {vendor.MAX_CHILD_OUTPUT + 1} /dev/zero")
    with pytest.raises(vendor.VendorError, match="^setpriv wrote more than 1 MB$"):
        vendor.run_capped([tool], b"", vendor.MAX_CHILD_OUTPUT, 30)


def test_over_cap_message_for_the_cab_listing(tmp_path):
    bsdtar = script(tmp_path, "bsdtar", "exec head -c 2000000 /dev/zero")
    ex = vendor.extract(fx.make_cab([("a", b"x")]), pins=SP, bsdtar=bsdtar)
    assert ex.provenance == ["cab: cannot list it: bsdtar wrote more than 1 MB"]


def test_run_capped_without_a_session_kills_only_the_child(tmp_path):
    """new_session=False: the tool stays in our process group and is killed and reaped by pid."""
    tool = script(tmp_path, "tool", "awk '{print $5}' /proc/$$/stat > PIDFILE.pgid\necho $$ > PIDFILE\nexec sleep 60")
    with pytest.raises(vendor.VendorError, match="took longer than 1 s"):
        vendor.run_capped([tool], b"", 1000, 1, new_session=False)
    pid = background_pid(tmp_path, "tool")
    assert int((tmp_path / "tool.pid.pgid").read_text()) == os.getpgrp()
    assert gone(pid)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)  # reaped already


def test_run_capped_kills_and_reaps_when_the_writer_thread_cannot_start(tmp_path, monkeypatch):
    tool = script(tmp_path, "tool", "echo $$ > PIDFILE\nexec sleep 60")

    class NoThread(threading.Thread):
        def start(self):
            background_pid(tmp_path, "tool")  # the tool runs: there is something to kill
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(vendor, "threading", types.SimpleNamespace(Thread=NoThread))
    with pytest.raises(RuntimeError, match="can't start new thread"):
        vendor.run_capped([tool], b"input", 1000, 30)
    pid = background_pid(tmp_path, "tool")
    assert gone(pid)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)  # reaped by run_capped, no zombie of ours left


def test_child_under_run_capped_takes_its_hung_bsdtar_down_with_it(tmp_path):
    """The setup tool's shape: the child run through run_capped, its bsdtar hangs (the child's
    own bsdtar timeout is 120 s). The parent's timeout kills the child's group: bsdtar and
    its background job are in it, so both die."""
    pins_file = tmp_path / "pins.json"
    pins_file.write_text(SP.to_json(), encoding="utf-8")
    bsdtar = script(
        tmp_path, "bsdtar", BACKGROUND.replace("PIDFILE", "PIDFILE.bg") + "\necho $$ > PIDFILE\nexec sleep 60"
    )
    code = (
        f"import sys; sys.path.insert(0, {str(fx.REPO)!r}); "
        "from synaptics0078 import vendor; sys.exit(vendor.child_entry())"
    )
    argv = [sys.executable, "-IB", "-c", code, "--pins", str(pins_file), "--bsdtar", bsdtar]
    started = time.monotonic()
    with pytest.raises(vendor.VendorError, match="took longer than 5 s"):
        vendor.run_capped(argv, fx.make_cab([("a", b"x")]), vendor.MAX_CHILD_OUTPUT, 5)
    assert time.monotonic() - started < 15
    tar = background_pid(tmp_path, "bsdtar")
    job = int((tmp_path / "bsdtar.pid.bg").read_text())
    assert gone(tar), "the child's bsdtar survived the parent's kill"
    assert gone(job), "the child's bsdtar's background job survived the parent's kill"


def test_run_capped_times_out_a_child_that_closed_its_output(tmp_path):
    tool = script(tmp_path, "tool", "exec >/dev/null 2>&1\nsleep 60")
    started = time.monotonic()
    with pytest.raises(vendor.VendorError, match="took longer than 1 s"):
        vendor.run_capped([tool], b"", 1000, 1)
    assert time.monotonic() - started < 10


# ------------------------------------------------------------ the child


@needs_bsdtar
def test_child_round_trip():
    out = vendor.child_main(fx.origin_style_zip(DLL), pins=SP)
    assert out.startswith(vendor.CHILD_MAGIC) and out.endswith(b"Z\0\0\0\0")
    ex = vendor.parse_child_output(out, pins=SP)
    assert_complete(ex)
    assert ex.provenance and all(line.startswith("extractor: ") for line in ex.provenance)


def frame(*records: tuple[bytes, bytes], magic=vendor.CHILD_MAGIC, end=True) -> bytes:
    body = b"".join(k + struct.pack(">I", len(p)) + p for k, p in records)
    return magic + body + (b"Z\0\0\0\0" if end else b"")


def test_parent_never_rebuilds_anything_from_the_child():
    """No pinned M or T record: nothing is found as root, the result is just incomplete."""
    ex = vendor.parse_child_output(frame((b"P", b"one line")), pins=SP)
    assert (ex.module, ex.template, ex.complete) == (None, None, False)
    assert ex.provenance == ["extractor: one line"]
    assert DLL[:4096] not in vendor.encode_child_output(vendor.extract(DLL, pins=SP)), "no DLL bytes are sent"


def test_parent_refuses_dll_records_without_scanning_them():
    """B5a's D record (a DLL for a re-scan as root) is an unknown kind now, refused at once."""
    hostile = fx.rand("hostile", 4 << 20)
    started = time.monotonic()
    with pytest.raises(vendor.VendorError, match="unknown record"):
        vendor.parse_child_output(frame((b"D", DLL)), pins=SP)
    with pytest.raises(vendor.VendorError, match="larger than allowed"):
        vendor.parse_child_output(frame((b"D", hostile)), pins=SP)  # over the whole-output cap
    with pytest.raises(vendor.VendorError, match="unknown record"):
        vendor.parse_child_output(frame((b"D", hostile[: 512 << 10])), pins=SP)
    assert time.monotonic() - started < 1.0


def test_parent_applies_its_own_pins():
    """A child that ran with other pins: the parent's (real) pins refuse its module."""
    out = vendor.child_main(DLL, pins=SP)
    with pytest.raises(vendor.VendorError, match="module that does not have the pinned sha256"):
        vendor.parse_child_output(out)


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        pytest.param(frame((b"M", fx.rand("wrong", pins.MODULE_LEN))), "module that does not", id="wrong module"),
        pytest.param(frame((b"T", bytes(pins.TEMPLATE_LEN))), "template that does not", id="wrong template"),
        pytest.param(frame((b"M", fx.MODULE[:-1])), "module that does not", id="short module"),
        pytest.param(frame((b"M", fx.MODULE + b"x")), "larger than allowed", id="long module"),
        pytest.param(
            vendor.CHILD_MAGIC + b"T" + struct.pack(">I", 0xFFFFFFFF) + b"Z\0\0\0\0",
            "larger than allowed or truncated",
            id="oversize frame",
        ),
        pytest.param(frame((b"P", b"x" * (vendor.MAX_TEXT + 1))), "larger than allowed", id="long text"),
        pytest.param(frame(end=False), "no end record", id="truncated"),
        pytest.param(frame() + b"x", "after its end record", id="trailing bytes"),
        pytest.param(frame(magic=b"S0078X2\n"), "expected header", id="bad magic"),
        pytest.param(frame((b"Q", b"")), "unknown record", id="unknown kind"),
        pytest.param(frame((b"E", b"boom")), "the extractor failed: boom", id="error record"),
        pytest.param(vendor.CHILD_MAGIC + b"M\0\0", "inside a record header", id="cut header"),
        pytest.param(frame((b"M", fx.MODULE), (b"M", fx.MODULE)), "module that does not", id="two modules"),
        pytest.param(frame((b"D", b"MZ")), "unknown record", id="DLL record"),
        pytest.param(frame(*[(b"P", b"x")] * (vendor.MAX_LINES + 1)), "more lines", id="too many lines"),
    ],
)
def test_bad_child_output_is_refused(data, needle):
    with pytest.raises(vendor.VendorError, match=needle):
        vendor.parse_child_output(data, pins=SP)


def test_oversize_output_is_refused(monkeypatch):
    monkeypatch.setattr(vendor, "MAX_CHILD_OUTPUT", 100)
    with pytest.raises(vendor.VendorError, match="larger than allowed"):
        vendor.parse_child_output(frame((b"P", b"x" * 200)), pins=SP)


def test_child_reports_errors_as_a_record(tmp_path):
    out = vendor.child_main(fx.make_cab([("a", b"x")]), pins=SP, bsdtar=str(tmp_path / "none"))
    with pytest.raises(vendor.VendorError, match="the extractor failed: .*not found"):
        vendor.parse_child_output(out, pins=SP)


@needs_bsdtar
def test_child_entry_as_the_setup_tool_starts_it(tmp_path):
    """python3 -IB -c '...vendor.child_entry()' with the archive on stdin, from an unrelated cwd."""
    pins_file = tmp_path / "pins.json"
    pins_file.write_text(SP.to_json(), encoding="utf-8")
    code = (
        f"import sys; sys.path.insert(0, {str(fx.REPO)!r}); "
        "from synaptics0078 import vendor; sys.exit(vendor.child_entry())"
    )
    r = subprocess.run(
        [sys.executable, "-IB", "-c", code, "--pins", str(pins_file)],
        input=fx.origin_style_zip(DLL),
        capture_output=True,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stderr == b""
    assert_complete(vendor.parse_child_output(r.stdout, pins=SP))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pins.json"], "the child wrote nothing"


def test_child_entry_bad_pins_file(tmp_path, monkeypatch, capsysbinary):
    bad = tmp_path / "pins.json"
    bad.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"")))
    assert vendor.child_entry(["--pins", str(bad)]) == 1
    with pytest.raises(vendor.VendorError, match="cannot start"):
        vendor.parse_child_output(capsysbinary.readouterr().out, pins=SP)


def test_pins_round_trip_through_json():
    assert pins.Pins.from_json(SP.to_json()) == SP
    assert pins.Pins.from_json(pins.DEFAULT_PINS.to_json()) == pins.DEFAULT_PINS
    for bad in ("", "[]", '{"module_len": 1}', SP.to_json().replace('"module_len": 10500', '"module_len": "x"')):
        with pytest.raises(ValueError):
            pins.Pins.from_json(bad)


# --------------------------------------------------------- the real archives

REAL = {a.name: a for a in pins.ARCHIVES}
LOCAL_ARCHIVES = [
    ("catalog-2019", fx.LOCAL_B5 / "cab-2019" / f"{REAL['catalog-2019'].url.rsplit('/', 1)[1]}"),
    ("catalog-2020", fx.LOCAL_B5 / "cab-2020" / f"{REAL['catalog-2020'].url.rsplit('/', 1)[1]}"),
    ("origin-2017", fx.LOCAL_B5 / "origin-zip" / "02_Finger.zip"),
]


def test_archive_table():
    assert [a.name for a in pins.ARCHIVES] == [
        "catalog-2019",
        "catalog-2020",
        "wayback-2019",
        "wayback-2020",
        "origin-2017",
        "wayback-origin-2017",
    ]
    for a in pins.ARCHIVES:
        assert a.url.startswith("https://") and len(a.sha256) == 64
        assert (a.kind, a.max_bytes) in (("cab", 16 << 20), ("zip", 32 << 20))
        assert a.max_time == (600 if a.label == pins.LABEL_WAYBACK else 300)
        assert a.size < a.max_bytes
    assert REAL["wayback-2019"].url.endswith(REAL["catalog-2019"].url)
    assert REAL["wayback-2020"].url.endswith(REAL["catalog-2020"].url)
    assert (REAL["wayback-origin-2017"].sha256, REAL["wayback-origin-2017"].size) == (
        REAL["origin-2017"].sha256,
        REAL["origin-2017"].size,
    )


@needs_bsdtar
@pytest.mark.parametrize(("name", "path"), LOCAL_ARCHIVES, ids=[n for n, _ in LOCAL_ARCHIVES])
def test_real_archive_gives_the_pinned_files(name, path):
    if not path.exists():
        pytest.skip(f"{path} not present")
    data = path.read_bytes()
    assert (len(data), fx.sha(data)) == (REAL[name].size, REAL[name].sha256)
    ex = vendor.extract(data, name=path.name)
    assert_complete(ex, pins.DEFAULT_PINS)  # extract() ran with the real pins
