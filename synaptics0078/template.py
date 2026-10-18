# SPDX-License-Identifier: MIT
"""The 18869-byte identify-program template, rebuilt from the vendor DLL plus code.

research/steps/b5-setup-design.md, "Provenance of the fixed bytes" and decision 1.
Every template chunk is one of:

  DLL   class a: located in synaWudfBioUsb.dll by its 4-byte header (u16 type, u16
        length) and accepted only when sha256(header + data) is the pin in pins.py.
        No DLL byte is in this file; build_from_dll copies them from the DLL it is given.
  CODE  class b: protocol structure and parameters generated here, field by field
        (the DLL builds these at run time from immediates).
  ZERO  class c: per unit, zero in the template (calibration.UNIT_REGIONS).

The result must hash to the template pin, so any slip fails closed (TemplateError).

program_matches_template (decision 10) checks an installed program without the template
bytes: its per-unit regions zeroed, it must hash to the template pin; on a mismatch the
per-chunk pins in pins.py name the first chunk that differs.

Command line, from the repo root (a fresh checkout recreates proto/data/cmd02_template.bin):
    python3 -m synaptics0078.template --dll PATH --out proto/data/cmd02_template.bin [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import calibration
from . import pins as _pins

DEFAULT_PINS = _pins.DEFAULT_PINS
MAX_DLL_BYTES = 64 << 20  # the known DLLs are 2.6 MB


class TemplateError(ValueError):
    """The template cannot be built from this DLL, or a program does not match the template."""


# ---------------------------------------------------------- generated fields


@dataclass(frozen=True)
class Field:
    """One generated field: a name, a struct format code (little endian) and its value."""

    name: str
    fmt: str
    value: int


def pack_fields(fields: tuple[Field, ...]) -> bytes:
    return b"".join(struct.pack("<" + f.fmt, f.value) for f in fields)


ROWS = calibration.LINES  # 56: type record T+0x12
COLS = calibration.COLUMNS  # 144: type record T+0x32
BYTES_PER_LINE = calibration.BPL  # 152: 8 header bytes + 144 samples

# Program header: opcode 02, u16 bytes per line, u16 req_lines (0: the identify program).
HEADER = (Field("opcode", "B", 0x02), Field("bytes_per_line", "H", BYTES_PER_LINE), Field("req_lines", "H", 0))

# 0x34 Timeslot Table 2D: the DLL table, with its key line (data bytes 0..143) zeroed as
# a per-unit region and these 6 bytes set. The DLL patches them at run time for the
# identify program (rule not traced; the same in two other units' payloads). Data offsets.
TIMESLOT_PATCH = ((149, 0x2B), (150, 0x26), (152, 0x2B), (153, 0x26), (155, 0x2B), (156, 0x24))

# Small u32 chunks: 0x2f rows, 0x29 timeslot table offset, 0x35 timeslot table offset
# for finger detect, 0x44 line update interleave.
ROWS_VALUE = ROWS
TIMESLOT_OFFSET = 144
FINGER_DETECT_TIMESLOT_OFFSET = 160
LINE_UPDATE_INTERLEAVE = 1


def finger_detect_fields(ival: int = calibration.DEFAULT_IVAL) -> tuple[Field, ...]:
    """0x26 Finger Detect, 40 B: what the DLL's builder 0x18008eda0 writes (immediates plus ival).

    The two threshold words at data bytes 12 and 16 are the ones calibration.assemble_cmd02
    rewrites for another ival.
    """
    return (
        Field("tag", "H", 0xB2FB),
        Field("u8_2", "B", 0x0F),
        Field("u8_3", "B", 0),
        Field("u16_4", "H", 0),
        Field("u8_6", "B", 0x0F),
        Field("u8_7", "B", 0),
        Field("u32_8", "I", 0x30),
        Field("threshold_low", "I", ival + 0x20000),
        Field("threshold_high", "I", ival + 0x9FFE0),
        Field("u32_20", "I", 0x8001),
        Field("u32_24", "I", 0x20A),
        Field("u32_28", "I", 0x190B),
        Field("u16_32", "H", 50000),
        Field("u16_34", "H", 60000),
        Field("u8_36", "B", 1),
        Field("u8_37", "B", 9),
        Field("u8_38", "B", 0x10),
        Field("u8_39", "B", 0),
    )


def image_reconstruction_fields(flags: int = calibration.DEFAULT_FLAGS) -> tuple[Field, ...]:
    """0x2e Image Reconstruction, 28 B (builder 0x18008cc90).

    u16 2, u16 24 (the bytes that follow), the capture flags byte (calibration.DEFAULT_FLAGS,
    identify), then the dimensions from the 0x2449 type record, then a parameter block the
    DLL fills in 0x1800a6550 (not traced; reproduced and pinned).
    """
    return (
        Field("u16_0", "H", 2),
        Field("body_len", "H", 24),
        Field("capture_flags", "B", flags),
        Field("u8_5", "B", 0),
        Field("u16_6", "H", 0),
        Field("columns", "H", COLS),  # T+0x32
        Field("rows", "H", ROWS),  # T+0x12
        Field("type_record_0e", "H", 333),  # T+0x0e
        Field("u16_14", "H", 0),
        Field("param_400", "H", 400),
        Field("param_380", "H", 380),
        Field("param_b20", "B", 60),
        Field("param_b21", "B", 50),
        Field("param_b22", "B", 50),
        Field("param_b23", "B", 50),
        Field("param_b24", "B", 100),
        Field("param_b25", "B", 10),
        Field("param_b26", "B", 2),
        Field("param_b27", "B", 2),
    )


# 0x30 Line Update: u32 count, (mask, flags) entries, then per-column data (per unit, zero).
# The 6 fixed entries name timeslot-program PCs of register writes; Windows finds them by
# searching the timeslot program (0x18009ba40 / 0x18009e050). Reproduced as constants.
LINE_UPDATE_FIXED_ENTRIES = (
    (0xFF, 0x072000AD),
    (0xFF, 0x073000B5),
    (0xFF, 0x074000B8),
    (0xFFFF, 0x4750012F),
    (0xFFFF, 0x4760019C),
    (0xFF, 0x17700374),
)
LINE_UPDATE_COLUMN_MASK = 0xFFFFFFFF
LINE_UPDATE_COLUMN_TAG = 0x85000000  # | first column of the 4-column group (0, 4, ..., 140)

# 0x43 Line Update Transform, 152 B: u32 count, the 56 row indexes, the two factory
# entries (per unit, zero), then three empty entries; the last one's format byte is set.
LINE_TRANSFORM_COUNT = 15
LINE_TRANSFORM_FIRST_ROW = 12  # row indexes 12..67
LINE_TRANSFORM_TAIL_LEN = 14
LINE_TRANSFORM_LAST_FORMAT_OFFSET = 147  # data offset
LINE_TRANSFORM_LAST_FORMAT = 0x0C


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _chunk(typ: int, data: bytes) -> bytes:
    return struct.pack("<HH", typ, len(data)) + data


def line_update_data() -> bytes:
    """0x30 data: 42 entries (6 fixed, 36 per column group), then 8064 zero bytes (per unit)."""
    cols = [(LINE_UPDATE_COLUMN_MASK, LINE_UPDATE_COLUMN_TAG | c) for c in range(0, COLS, 4)]
    ents = list(LINE_UPDATE_FIXED_ENTRIES) + cols
    table = _u32(len(ents)) + b"".join(struct.pack("<II", m, f) for m, f in ents)
    return table + bytes(calibration.LINE_UPDATE_LEN)


def line_transform_data() -> bytes:
    """0x43 data, 152 B, with the two factory entries zero (per unit)."""
    out = bytearray(
        _u32(LINE_TRANSFORM_COUNT)
        + bytes(range(LINE_TRANSFORM_FIRST_ROW, LINE_TRANSFORM_FIRST_ROW + ROWS))
        + bytes(calibration.FACTORY_ENTRIES_LEN)
        + bytes(LINE_TRANSFORM_TAIL_LEN)
    )
    out[LINE_TRANSFORM_LAST_FORMAT_OFFSET] = LINE_TRANSFORM_LAST_FORMAT
    return bytes(out)


# ------------------------------------------------------------------ builder


def locate_chunk(dll: bytes, pin: _pins.ChunkPin) -> bytes:
    """The first header + data in `dll` with the pin's type, length and sha256; TemplateError if none."""
    hdr = struct.pack("<HH", pin.type, pin.length)
    view = memoryview(dll)
    i = dll.find(hdr)
    while i >= 0:
        whole = view[i : i + 4 + pin.length]
        if len(whole) == 4 + pin.length and hashlib.sha256(whole).hexdigest() == pin.sha256:
            return bytes(whole)
        i = dll.find(hdr, i + 1)
    raise TemplateError(
        f"the DLL has no copy of chunk {pin.name} (type 0x{pin.type:02x}, {pin.length} B) with the pinned sha256"
    )


def zero_unit_regions(program: bytes) -> bytes:
    """`program` with the four per-unit regions (calibration.UNIT_REGIONS) zeroed; raises CalibrationError."""
    out = bytearray(program)
    for lo, hi in calibration.unit_region_spans(bytes(program)).values():
        out[lo:hi] = bytes(hi - lo)
    return bytes(out)


def assemble_template(dll: bytes, *, pins: _pins.Pins = DEFAULT_PINS) -> bytes:
    """The template from `dll` and the generated fields, without the final pin check."""
    got = {c.name: locate_chunk(dll, c) for c in pins.dll_chunks}
    timeslot = bytearray(got["timeslot"][4:])
    for off, val in TIMESLOT_PATCH:
        timeslot[off] = val
    parts = [
        _chunk(0x23, b""),
        got["reg32"],
        got["list16_92"],
        got["list32_40"],
        got["list16_64"],
        got["list32_868"],
        got["list16_12"],
        _chunk(0x34, bytes(timeslot)),  # key line zeroed below, with the other per-unit regions
        got["encoder"],
        _chunk(0x2F, _u32(ROWS_VALUE)),
        _chunk(0x29, _u32(TIMESLOT_OFFSET)),
        _chunk(0x35, _u32(FINGER_DETECT_TIMESLOT_OFFSET)),
        _chunk(0x17, b""),
        _chunk(0x26, pack_fields(finger_detect_fields())),
        _chunk(0x2E, pack_fields(image_reconstruction_fields())),
        _chunk(0x47, b""),
        _chunk(0x44, _u32(LINE_UPDATE_INTERLEAVE)),
        _chunk(0x30, line_update_data()),
        _chunk(0x43, line_transform_data()),
        _chunk(0x48, bytes(calibration.BASELINE_CHUNK_LEN)),  # baseline, dimensions included
    ]
    return zero_unit_regions(pack_fields(HEADER) + b"".join(parts))


def build_from_dll(dll: bytes, *, pins: _pins.Pins = DEFAULT_PINS) -> bytes:
    """The pinned 18869-byte template from a vendor DLL's bytes; TemplateError when it cannot be.

    The message names the missing chunk, or says the result does not hash to the pin.
    It never contains DLL bytes.
    """
    out = assemble_template(dll, pins=pins)
    got = hashlib.sha256(out).hexdigest()
    if len(out) != pins.template_len or got != pins.template_sha256:
        raise TemplateError(f"the built template ({len(out)} B) does not have the pinned sha256")
    return out


def chunk_pins(template: bytes, names: tuple[str, ...] | None = None) -> tuple[str, tuple[_pins.ChunkPin, ...]]:
    """(header sha256, per-chunk pins) of a template, as pins.TEMPLATE_HEADER_SHA256 / TEMPLATE_CHUNKS hold them.

    `names` default to the names of _pins.TEMPLATE_CHUNKS (same order). For tests and
    for recomputing the pins after a deliberate template change.
    """
    _, chunks = calibration.split_chunks(template)
    names = names or tuple(c.name for c in _pins.TEMPLATE_CHUNKS)
    out, off = [], 5
    for i, (typ, data) in enumerate(chunks):
        whole = template[off : off + 4 + len(data)]
        name = names[i] if i < len(names) else f"0x{typ:02x}"
        out.append(_pins.ChunkPin(name, typ, len(data), hashlib.sha256(whole).hexdigest()))
        off += 4 + len(data)
    return hashlib.sha256(template[:5]).hexdigest(), tuple(out)


# ------------------------------------------------------------ program check

_SETTINGS = {
    calibration.CHUNK_IVAL: f"; the finger-detect threshold must be ival 0x{calibration.DEFAULT_IVAL:02x}",
    calibration.CHUNK_FLAGS: f"; the capture flags must be 0x{calibration.DEFAULT_FLAGS:02x}",
}


def _first_differing_chunk(zeroed: bytes, pins: _pins.Pins) -> str:
    """Name the first part of a zeroed program that differs from the per-chunk pins."""
    if hashlib.sha256(zeroed[:5]).hexdigest() != pins.template_header_sha256:
        return "the 5-byte header differs from the template"
    _, chunks = calibration.split_chunks(zeroed)
    want = pins.template_chunks
    off = 5
    for i, (typ, data) in enumerate(chunks):
        if i >= len(want):
            return f"chunk {i + 1} (0x{typ:02x}) is extra: the template has {len(want)} chunks"
        w = want[i]
        if (typ, len(data)) != (w.type, w.length):
            return f"chunk {i + 1} is 0x{typ:02x} of {len(data)} B where the template has {w.name} of {w.length} B"
        if hashlib.sha256(zeroed[off : off + 4 + len(data)]).hexdigest() != w.sha256:
            return f"chunk {w.name} differs from the template{_SETTINGS.get(typ, '')}"
        off += 4 + len(data)
    if len(chunks) < len(want):
        return f"chunk {want[len(chunks)].name} is missing"
    return "the program differs from the template, but every chunk matches its pin (the pins disagree)"


def program_matches_template(
    program: bytes, *, pins: _pins.Pins = DEFAULT_PINS, template: bytes | None = None
) -> tuple[bool, str | None]:
    """(True, None) when `program` is a valid identify program for this reader, else (False, reason).

    Valid (design decision 10): with its four per-unit regions zeroed, the program hashes
    to the template pin, and the regions themselves pass calibration.check_unit_chunks
    (the 0x43 entries identical with format byte 0x0c and reserved u16 0, the 0x48
    dimensions 152 x 56). The template bytes are not needed. The reason names the first
    differing chunk (by the per-chunk pins), or with `template` the first differing
    offset. It never contains program bytes.
    """
    program = bytes(program)
    if len(program) != pins.template_len:
        return False, f"program is {len(program)} bytes, need {pins.template_len}"
    if program[0] != 0x02:
        return False, f"first byte is 0x{program[0]:02x}, need 0x02"
    try:
        zeroed = zero_unit_regions(program)
    except calibration.CalibrationError as exc:
        return False, f"the program's layout is not the template's: {exc}"
    if hashlib.sha256(zeroed).hexdigest() != pins.template_sha256:
        if template is not None and len(template) == len(zeroed):
            offset = next((i for i, (a, b) in enumerate(zip(template, zeroed)) if a != b), None)
            if offset is not None:
                return (
                    False,
                    f"byte {offset} differs from the template: {calibration.describe_offset(template, offset)}",
                )
        try:
            return False, _first_differing_chunk(zeroed, pins)
        except calibration.CalibrationError as exc:
            return False, f"the program differs from the template: {exc}"
    try:
        calibration.check_unit_chunks(calibration.unit_regions(program))
    except calibration.CalibrationError as exc:
        return False, str(exc)
    return True, None


# --------------------------------------------------------------------- CLI


def _write_file(path: Path, data: bytes, force: bool) -> None:
    """Write `data` to `path`: a new file, or with force an atomic replace. Never follows a symlink."""
    if not force:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _make_parent(out: Path) -> str | None:
    """Create the directory `out` goes into when it is missing (0755, that one level only).

    None when it exists or was made; otherwise the reason, with a command that fixes it.
    A fresh checkout has no proto/data, the default --out's directory.
    """
    parent = out.parent
    if parent.is_dir():
        return None
    hint = f"Create it, then run this again:\n  mkdir -p {shlex.quote(str(parent))}"
    try:
        os.mkdir(parent, 0o755)
    except FileNotFoundError:
        return f"Could not write {out}: its directory {parent} does not exist, nor does {parent.parent}. {hint}"
    except FileExistsError:
        return f"Could not write {out}: {parent} exists and is not a directory."
    except OSError as exc:
        return f"Could not create the directory {parent}: {exc.strerror or exc}. {hint}"
    print(f"Created the directory {parent}.")
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m synaptics0078.template",
        description="Rebuild the 18869-byte template (proto/data/cmd02_template.bin) from synaWudfBioUsb.dll.",
    )
    ap.add_argument("--dll", required=True, metavar="PATH", help="the vendor DLL (synaWudfBioUsb.dll), read only")
    ap.add_argument("--out", required=True, metavar="PATH", help="where to write the template")
    ap.add_argument("--force", action="store_true", help="replace PATH if it exists")
    args = ap.parse_args(argv)
    dll_path, out = Path(args.dll), Path(args.out)
    try:
        with open(dll_path, "rb") as fh:
            dll = fh.read(MAX_DLL_BYTES + 1)
    except OSError as exc:
        print(f"Could not read the DLL {dll_path}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    if len(dll) > MAX_DLL_BYTES:
        print(f"{dll_path} is larger than {MAX_DLL_BYTES >> 20} MB, so it is not the vendor DLL.", file=sys.stderr)
        return 1
    try:
        template = build_from_dll(dll)
    except TemplateError as exc:
        print(f"Could not build the template from {dll_path}: {exc}.", file=sys.stderr)
        return 1
    if (out.exists() or out.is_symlink()) and not args.force:
        print(f"{out} already exists; nothing was written. To replace it, run again with --force.", file=sys.stderr)
        return 2
    problem = _make_parent(out)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    try:
        _write_file(out, template, args.force)
    except OSError as exc:
        print(f"Could not write {out}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    print(f"Template written: {out} ({len(template)} bytes, sha256 matches the pin).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
