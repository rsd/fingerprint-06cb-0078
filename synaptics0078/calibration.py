# SPDX-License-Identifier: MIT
# bitpack() is ported from python-validity (validitysensor/sensor.py,
# https://github.com/uunicorn/python-validity), MIT licence:
#   Copyright (c) 2020 uunicorn
# The port and the rest of this file: Copyright (C) 2026 Raul Dias <raul@dias.com.br>, MIT.
# Both are under the MIT terms in LICENSE (same permission notice).
"""Build the 18869-byte `02` (capture program) command of the Synaptics 06cb:0078.

Pure functions: bytes in, bytes out. No USB, no file access beyond the
optional template loader. Moved from proto/ into the synaptics0078 package in B5a
(proto/calibration.py is an alias of it). Derived from research/calib/build_ours.py and
research/calibration-analysis.md (the "[CAL]" references below); the sections
cited there were verified against the vendor DLL (synaWudfBioUsb.dll 5.5.2707.1073)
and by a byte-exact round trip of two other units' payloads (see
test_calibration.py).

How the payload is made
-----------------------
`02 | u16 bytes_per_line = 0x0098 | u16 req_lines = 0 | chunks`, each chunk
`u16 type, u16 length, data` [CAL section 1]. About 2.1 KB of it is fixed and
identical in every reference; that part is proto/data/cmd02_template.bin (the
payload of iusearch/Synaptics78 with every per-unit region zeroed; since B5,
synaptics0078.template.build_from_dll rebuilds it byte-exact from the vendor DLL
plus code, and the pinned sha256 is the gate either way). Four
regions are per unit, and come from the unit's CalibrationData [CAL section 0]:

  chunk 0x34 "Timeslot Table 2D", first 144 bytes (key line)
  chunk 0x30 "Line Update", 8064 bytes of data (36 column groups x 56 rows x 4)
  chunk 0x43 "Line Update Transform", the two bit-packed factory-value entries
  chunk 0x48 "Baseline Frame", u16 152, u16 56, then the 56 x 152 baseline frame

assemble_cmd02 is the one splice of those four regions (as UnitChunks) into the
template, for regions computed from a CalibrationData (build_cmd02) as well as for
regions measured on Linux (proto/calibrate.py, proto/calib_program.py);
unit_regions cuts them out again and program_problem validates a finished program.

Two more bytes are capture settings, not unit data: the finger-detect
threshold `ival` in chunk 0x26 (0xa0 = quality 100, what PR #288 says Windows
passes; 0xaa = quality 90) and the capture flags byte of chunk 0x2e (0x02 for
identify; the references also use 0x23 after a capture).

CalibrationData_decrypted.bin layout [CAL section 2] (file offsets): u32 0x226c,
u32 0xe4, u32 2 (version), u32 0x2449 (sensor type) at 0x0c, a RomInfo copy at
0x10 with the unit serial at 0x20..0x25, the 0xC0-byte struct C at 0x2c, the 56
factory values at 0xEC (C+0x68, size at C+0x70), the calibration frame at 0x124
(C+0x80, size at C+0x88 = 8512), the frame descriptors at 0x2274, and the baseline
frame (descriptor ctx+0xd8) at 0x44ac.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

from . import pins

SENSOR_TYPE = 0x2449
# The repo's proto/data/cmd02_template.bin (gitignored; `python3 -m synaptics0078.template`
# recreates it from the vendor DLL). The path is relative to the checkout this package is in.
# A repo-only default: proto/data/ of this checkout (gitignored; synaptics0078.template
# rebuilds the file from the vendor DLL). The installed layout (/usr/lib/synaptics0078)
# has no proto/data: the setup tool there passes the template bytes itself, and
# load_template() with this default fails with FileNotFoundError.
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "proto" / "data" / "cmd02_template.bin"
TEMPLATE_SHA256 = pins.TEMPLATE_SHA256
PAYLOAD_LEN = pins.TEMPLATE_LEN

LINES = 56  # sensor rows [CAL section 1: 0x2f chunk, 0x38 = 56 lines]
BPL = 152  # bytes per stored line: 8 header + 144 pixels
COLUMNS = 144
FACTORY_COUNT = 56
FRAME_LEN = LINES * BPL  # 8512

# CalibrationData file offsets [CAL section 2]
CAL_STRUCT = 0x2C
FACTORY_OFFSET = 0xEC
FRAME_OFFSET = 0x124
BASELINE_OFFSET = 0x44AC
SERIAL_OFFSET, SERIAL_LEN = 0x20, 6
MIN_CAL_LEN = BASELINE_OFFSET + FRAME_LEN

KEY_ROWS = (18, 28, 38)  # rows 0x12, 0x1c, 0x26 [CAL section 4, function 0x18008af70]
KEY_BAND = 48

DEFAULT_IVAL = 0xA0  # chunk 0x26 threshold, quality 100 [CAL section 6]
DEFAULT_FLAGS = 0x02  # chunk 0x2e capture flags, identify


class CalibrationError(ValueError):
    """The input is not what this module can build a payload from."""


@dataclass(frozen=True)
class CalibrationInputs:
    factory: bytes  # 56 factory values
    frame: bytes  # calibration frame, 56 lines x 152 bytes (header + 144 px)
    baseline: bytes  # baseline frame, 56 lines x 152 bytes
    serial: bytes = b""  # 6-byte sensor serial the blob is bound to (may be empty)


def parse_calibration_data(blob: bytes) -> CalibrationInputs:
    """Validate a CalibrationData_decrypted.bin and cut out the three inputs.

    The checks are the ones the vendor deserializer makes (header tag 0xe4,
    version 2, sensor type) plus the sizes the layout in [CAL section 2]
    relies on. Anything else raises CalibrationError.
    """
    if len(blob) < MIN_CAL_LEN:
        raise CalibrationError(f"CalibrationData is {len(blob)} bytes, need at least {MIN_CAL_LEN}")
    tag, version, stype = struct.unpack_from("<III", blob, 4)
    if (tag, version, stype) != (0xE4, 2, SENSOR_TYPE):
        raise CalibrationError(
            f"header (tag 0x{tag:x}, version {version}, sensor type 0x{stype:x}) is not (0xe4, 2, 0x2449)"
        )
    fact_len = struct.unpack_from("<I", blob, CAL_STRUCT + 0x70)[0]
    frame_len = struct.unpack_from("<I", blob, CAL_STRUCT + 0x88)[0]
    if (fact_len, frame_len) != (FACTORY_COUNT, FRAME_LEN):
        raise CalibrationError(f"block sizes {(fact_len, frame_len)} are not ({FACTORY_COUNT}, {FRAME_LEN})")
    for desc in (0x2294, 0x22B4):  # descriptors of the two stored frames: {1, w, h, ...}
        if struct.unpack_from("<II", blob, desc + 4) != (BPL, LINES):
            raise CalibrationError(f"frame descriptor at 0x{desc:x} is not {BPL} x {LINES}")
    return CalibrationInputs(
        factory=bytes(blob[FACTORY_OFFSET : FACTORY_OFFSET + FACTORY_COUNT]),
        frame=bytes(blob[FRAME_OFFSET : FRAME_OFFSET + FRAME_LEN]),
        baseline=bytes(blob[BASELINE_OFFSET : BASELINE_OFFSET + FRAME_LEN]),
        serial=bytes(blob[SERIAL_OFFSET : SERIAL_OFFSET + SERIAL_LEN]),
    )


def rescale(b: int, five_to_four: bool) -> int:
    """q = clamp_s8((b - 0x80) * 10 / 4), truncating toward zero like C, as one byte.

    [CAL section 1 and 4]. The 5 -> 4 step applies only to the key line
    (function 0x18008af70), not to the Line Update data.
    """
    v = (b - 0x80) * 10
    q = -(-v // 4) if v < 0 else v // 4
    q = max(-128, min(127, q))
    if five_to_four and q == 5:
        q = 4
    return q & 0xFF


def bitpack(values: bytes) -> tuple[int, int, bytes]:
    """python-validity sensor.py bitpack(): (bits, minimum, little-endian bitstream)."""
    m = min(values)
    x = max(values) - m
    bits = 0
    while x > 0:
        x >>= 1
        bits += 1
    acc = 0
    for i, val in enumerate(values):
        acc |= (val - m) << (bits * i)
    return bits, m, acc.to_bytes((bits * len(values) + 7) // 8, "little")


def split_chunks(payload: bytes) -> tuple[bytes, list[tuple[int, bytes]]]:
    """(5-byte header, [(type, data), ...]); raises CalibrationError on a bad layout."""
    if len(payload) < 5 or payload[0] != 0x02:
        raise CalibrationError("not a 02 command")
    chunks, off = [], 5
    while off < len(payload):
        if off + 4 > len(payload):
            raise CalibrationError(f"trailing bytes at {off}")
        typ, size = struct.unpack_from("<HH", payload, off)
        if off + 4 + size > len(payload):
            raise CalibrationError(f"chunk 0x{typ:x} at {off} overruns the payload")
        chunks.append((typ, payload[off + 4 : off + 4 + size]))
        off += 4 + size
    return bytes(payload[:5]), chunks


def load_template(path: Path = TEMPLATE_PATH) -> bytes:
    """The fixed part of the payload; refuses a file whose hash is not the pinned one.

    The default path is the repo-only TEMPLATE_PATH (proto/data of a checkout); it does
    not exist in the installed /usr/lib/synaptics0078 layout.
    """
    data = Path(path).read_bytes()
    got = hashlib.sha256(data).hexdigest()
    if got != TEMPLATE_SHA256:
        raise CalibrationError(f"{path}: sha256 {got} is not the expected {TEMPLATE_SHA256}")
    return data


# ------------------------------------------------------- per-unit regions
# The four regions build_cmd02 fills per unit, as UnitChunks holds them. Everything
# else in the 18869-byte program comes from the template (plus ival and flags).

KEY_LINE_LEN = 144  # chunk 0x34 bytes 0..143
LINE_UPDATE_LEN = (COLUMNS // 4) * LINES * 4  # 8064: chunk 0x30 data after its entry table
FACTORY_ENTRY_OFFSET = 60  # first bit-packed entry inside chunk 0x43
FACTORY_BITS = 5
FACTORY_ENTRY_LEN = 4 + (FACTORY_BITS * FACTORY_COUNT + 7) // 8  # 39: format, minimum, u16 0, stream
FACTORY_ENTRIES_LEN = 2 * FACTORY_ENTRY_LEN  # 78: chunk 0x43 bytes 60..137
FACTORY_FORMAT = (FACTORY_BITS - 1) | 8  # 0x0c
BASELINE_CHUNK_LEN = 4 + FRAME_LEN  # chunk 0x48 data: u16 152, u16 56, then the frame
CHUNK_IVAL, CHUNK_FLAGS = 0x26, 0x2E


@dataclass(frozen=True)
class UnitRegion:
    """Where one per-unit region sits in the program: in the first chunk of type `chunk`."""

    name: str  # the UnitChunks field
    chunk: int  # chunk type
    start: int | None  # offset in the chunk data; None: right after the 0x30 entry table (u32 n, n x 8 B)
    length: int | None  # bytes; None: to the end of the chunk data


# The one table of the per-unit regions (class c of research/steps/b5-setup-design.md):
# unit_region_spans, and so unit_regions, program_problem and the template-pin check of
# synaptics0078.template, all locate the regions through it.
UNIT_REGIONS = (
    UnitRegion("key_line", 0x34, 0, KEY_LINE_LEN),
    UnitRegion("line_update", 0x30, None, None),
    UnitRegion("factory_entries", 0x43, FACTORY_ENTRY_OFFSET, FACTORY_ENTRIES_LEN),
    UnitRegion("baseline", 0x48, 0, None),
)
REGION_CHUNKS = {r.name: r.chunk for r in UNIT_REGIONS}
_SETTING_NAMES = {
    CHUNK_IVAL: f"finger-detect threshold, ival 0x{DEFAULT_IVAL:02x} expected",
    CHUNK_FLAGS: f"capture flags, 0x{DEFAULT_FLAGS:02x} expected",
}


@dataclass(frozen=True)
class UnitChunks:
    """The four per-unit regions of the identify program, exactly as they are spliced in."""

    key_line: bytes  # 0x34 bytes 0..143
    line_update: bytes  # 0x30 data, 8064 B
    factory_entries: bytes  # 0x43 bytes 60..137
    baseline: bytes  # 0x48 data, 4 + 8512 B


def check_unit_chunks(chunks: UnitChunks) -> None:
    """Region lengths, and the bytes inside the regions that are not per unit (raises CalibrationError)."""
    for name, want in (
        ("key_line", KEY_LINE_LEN),
        ("line_update", LINE_UPDATE_LEN),
        ("factory_entries", FACTORY_ENTRIES_LEN),
        ("baseline", BASELINE_CHUNK_LEN),
    ):
        got = len(getattr(chunks, name))
        if got != want:
            raise CalibrationError(f"{name} region (chunk 0x{REGION_CHUNKS[name]:02x}) is {got} bytes, need {want}")
    e = bytes(chunks.factory_entries)
    if e[:FACTORY_ENTRY_LEN] != e[FACTORY_ENTRY_LEN:]:
        raise CalibrationError("chunk 0x43: the two factory entries differ")
    if e[0] != FACTORY_FORMAT:
        raise CalibrationError(f"chunk 0x43: factory entry format byte is 0x{e[0]:02x}, need 0x{FACTORY_FORMAT:02x}")
    if e[2:4] != bytes(2):
        raise CalibrationError("chunk 0x43: factory entry reserved u16 is not 0")
    dims = struct.unpack_from("<HH", chunks.baseline, 0)
    if dims != (BPL, LINES):
        raise CalibrationError(f"chunk 0x48: baseline dimensions {dims[0]} x {dims[1]}, need {BPL} x {LINES}")


def assemble_cmd02(
    template: bytes | None,
    chunks: UnitChunks,
    ival: int = DEFAULT_IVAL,
    flags: int = DEFAULT_FLAGS,
) -> bytes:
    """Splice the four per-unit regions and the two capture settings into the template.

    The one splice behind both the Windows path (build_cmd02, regions computed from a
    CalibrationData) and the Linux path (regions measured by calibrate.py). `template`
    None loads proto/data/cmd02_template.bin. Bad region lengths, a 0x43 region whose two
    entries differ or whose format byte / reserved u16 is not 0x0c / 0, and a 0x48
    region not starting with u16 152, u16 56 raise CalibrationError.
    """
    if template is None:
        template = load_template()
    check_unit_chunks(chunks)
    if not 0 <= ival <= 0xFF or not 0 <= flags <= 0xFF:
        raise CalibrationError("ival and flags are single bytes")

    try:
        header, parts = split_chunks(template)
    except CalibrationError as exc:
        raise CalibrationError(f"template: {exc}") from None
    out = []
    seen = set()
    for typ, data in parts:
        p = bytearray(data)
        seen.add(typ)
        if typ == 0x34:  # key line
            if len(p) < KEY_LINE_LEN:
                raise CalibrationError("template chunk 0x34 is too short")
            p[0:KEY_LINE_LEN] = chunks.key_line
        elif typ == 0x30:  # Line Update: u32 n, n (mask, flags) pairs, then the data
            (n,) = struct.unpack_from("<L", p, 0)
            d0 = 4 + 8 * n
            if len(p) - d0 != LINE_UPDATE_LEN:
                raise CalibrationError(
                    f"chunk 0x30 data is {len(p) - d0} bytes in the template, need {LINE_UPDATE_LEN}"
                )
            p[d0:] = chunks.line_update
        elif typ == 0x43:  # the two bit-packed factory entries
            if len(p) < FACTORY_ENTRY_OFFSET + FACTORY_ENTRIES_LEN:
                raise CalibrationError("template chunk 0x43 is too short")
            p[FACTORY_ENTRY_OFFSET : FACTORY_ENTRY_OFFSET + FACTORY_ENTRIES_LEN] = chunks.factory_entries
        elif typ == 0x48:  # baseline frame, verbatim with its line headers
            if len(p) != BASELINE_CHUNK_LEN:
                raise CalibrationError(f"template chunk 0x48 is {len(p)} bytes, need {BASELINE_CHUNK_LEN}")
            p = bytearray(chunks.baseline)
        elif typ == CHUNK_IVAL:  # finger detect threshold [CAL section 6]
            struct.pack_into("<II", p, 12, ival + 0x20000, ival + 0x9FFE0)
        elif typ == CHUNK_FLAGS:  # capture flags
            p[4] = flags
        out.append(struct.pack("<HH", typ, len(p)) + bytes(p))
    missing = {0x34, 0x30, 0x43, 0x48, CHUNK_IVAL, CHUNK_FLAGS} - seen
    if missing:
        raise CalibrationError(f"template lacks chunk(s) {sorted(hex(m) for m in missing)}")
    result = header + b"".join(out)
    if len(result) != len(template):
        raise CalibrationError(f"built {len(result)} bytes from a {len(template)}-byte template")
    return result


def _regions_from_inputs(inputs: CalibrationInputs) -> UnitChunks:
    """The four regions of one unit from its CalibrationData inputs (sizes already checked)."""
    key = b"".join(
        bytes(
            rescale(x, True) for x in inputs.frame[r * BPL + 8 + band * KEY_BAND : r * BPL + 8 + (band + 1) * KEY_BAND]
        )
        for band, r in enumerate(KEY_ROWS)
    )
    cols = bytearray()
    for k in range(COLUMNS // 4):
        for j in range(LINES):
            base = j * BPL + 8 + 4 * k
            cols += bytes(rescale(x, False) for x in inputs.frame[base : base + 4])
    bits, mn, stream = bitpack(inputs.factory)
    if bits != FACTORY_BITS:
        raise CalibrationError(f"factory values need {bits} bits, the layout has {FACTORY_BITS}")
    entry = struct.pack("<BBH", (bits - 1) | 8, mn, 0) + stream
    return UnitChunks(key, bytes(cols), entry * 2, struct.pack("<HH", BPL, LINES) + inputs.baseline)


def build_cmd02(
    inputs: CalibrationInputs,
    template: bytes | None = None,
    ival: int = DEFAULT_IVAL,
    flags: int = DEFAULT_FLAGS,
) -> bytes:
    """Build the `02` command (including the leading 0x02 byte) for one unit.

    `template` is the fixed part (default: proto/data/cmd02_template.bin); `ival` is
    the chunk 0x26 finger-detect threshold, `flags` the chunk 0x2e capture flags.
    """
    if template is None:
        template = load_template()
    if len(inputs.factory) != FACTORY_COUNT:
        raise CalibrationError(f"{len(inputs.factory)} factory values, need {FACTORY_COUNT}")
    if len(inputs.frame) != FRAME_LEN or len(inputs.baseline) != FRAME_LEN:
        raise CalibrationError(f"frames must be {FRAME_LEN} bytes ({LINES} x {BPL})")
    if not 0 <= ival <= 0xFF or not 0 <= flags <= 0xFF:
        raise CalibrationError("ival and flags are single bytes")
    return assemble_cmd02(template, _regions_from_inputs(inputs), ival, flags)


def _chunk_offsets(program: bytes) -> list[tuple[int, int, int]]:
    """[(type, data offset, data length), ...] of a `02` command; raises CalibrationError on a bad layout."""
    _, chunks = split_chunks(program)
    out, off = [], 5
    for typ, data in chunks:
        out.append((typ, off + 4, len(data)))
        off += 4 + len(data)
    return out


def unit_region_spans(program: bytes) -> dict[str, tuple[int, int]]:
    """{UnitChunks field: (start, end)}, the program offsets of the four per-unit regions.

    The first chunk of each type counts; UNIT_REGIONS says where each region sits. A
    missing chunk or one too short for its region raises CalibrationError.
    """
    first = {}
    for typ, start, size in _chunk_offsets(program):
        first.setdefault(typ, (start, size))
    spans = {}
    for region in UNIT_REGIONS:
        typ = region.chunk
        if typ not in first:
            raise CalibrationError(f"no chunk 0x{typ:02x}")
        start, size = first[typ]
        if region.start is None:  # after the 0x30 entry table
            if size < 4:
                raise CalibrationError(f"chunk 0x{typ:02x} is too short")
            (n,) = struct.unpack_from("<L", program, start)
            lo = start + 4 + 8 * n
        else:
            lo = start + region.start
        hi = start + size if region.length is None else lo + region.length
        if hi > start + size or lo > hi:
            raise CalibrationError(f"chunk 0x{typ:02x} is too short for its per-unit region")
        spans[region.name] = (lo, hi)
    return spans


def unit_regions(program: bytes) -> UnitChunks:
    """Cut the four per-unit regions out of a `02` program (the inverse of assemble_cmd02)."""
    spans = unit_region_spans(program)
    return UnitChunks(**{name: bytes(program[lo:hi]) for name, (lo, hi) in spans.items()})


def describe_offset(program: bytes, offset: int) -> str:
    """Where `offset` lies in a `02` command, in words (no byte values)."""
    if offset < 5:
        return "the 5-byte header"
    for typ, start, size in _chunk_offsets(program):
        if start - 4 <= offset < start:
            return f"the type/length field of chunk 0x{typ:02x}"
        if start <= offset < start + size:
            what = f"chunk 0x{typ:02x}, data byte {offset - start}"
            return what + (f" ({_SETTING_NAMES[typ]})" if typ in _SETTING_NAMES else "")
    return "past the last chunk"


def program_problem(program: bytes, template: bytes | None = None) -> str | None:
    """None when `program` is a valid identify program for this reader, else a short reason.

    Valid: PAYLOAD_LEN bytes, first byte 0x02, and every byte outside the four per-unit
    regions equal to the template (default: the pinned proto/data/cmd02_template.bin) with
    ival 0xa0 and flags 0x02, the regions themselves passing assemble_cmd02's checks.
    In other words assemble_cmd02(template, unit_regions(program)) == program. The
    reason names the failed check (for a mismatch, the first differing offset and its
    chunk); it never contains program bytes. Pure: no output, no files besides the
    template read.
    """
    if len(program) != PAYLOAD_LEN:
        return f"program is {len(program)} bytes, need {PAYLOAD_LEN}"
    if program[0] != 0x02:
        return f"first byte is 0x{program[0]:02x}, need 0x02"
    if template is None:
        try:
            template = load_template()
        except (OSError, CalibrationError) as exc:
            return f"template not usable: {exc}"
    try:
        rebuilt = assemble_cmd02(template, unit_regions(bytes(program)))
    except CalibrationError as exc:
        return str(exc)
    if rebuilt == program:
        return None
    offset = next(i for i, (a, b) in enumerate(zip(rebuilt, program)) if a != b)
    return f"byte {offset} differs from the template: {describe_offset(rebuilt, offset)}"


def build_cmd02_from_calibration_data(blob: bytes, **kwargs) -> bytes:
    """CalibrationData_decrypted.bin bytes -> the `02` payload."""
    return build_cmd02(parse_calibration_data(blob), **kwargs)
