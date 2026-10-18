# SPDX-License-Identifier: MIT
"""Model of the Windows calibration of the Synaptics 06cb:0078 (sensor type 0x2449).

Pure functions, stdlib only, no USB. Read from synaWudfBioUsb.dll 5.5.2707.1073
(addresses are VAs, image base 0x180000000) in research step B2; see
research/steps/b2-calibration-source.md (the "[B2]" note) for the byte map and
the evidence. The per-unit `02` regions are filled the same way as in
calibration.py, whose helpers (rescale, bitpack, split_chunks, load_template)
are reused here.

Terms ([B2] "Byte map"):
  CF  calibration frame, C+0x80, 56 x 152 B: the averaged, UNDECODED frame of
      blank capture #1 (no calibration applied).
  BF  baseline frame, descriptor ctx+0xd8, 56 x 152 B: the averaged and DECODED
      frame of blank capture #2 (CF applied).
  q   q(b) = clamp_s8(trunc((b - 0x80) * 10 / 4)); 10 and 4 are T.u16@0x88 and
      T.u16@0x8a of the 0x2449 type record at VA 0x180134b48.

A blank capture is a `02` with req_lines = 337: EP 0x82 then delivers 337 lines
of 152 bytes (51 224 B). The host keeps frame 2 only (lines 112..223).

The two calibration programs (assembler 0x18007e380, capture type 0, flags 0x430):
  #1 (1917 B from our template): header 02 98 00 51 01, the template's chunks
     without 0x26/0x2e/0x47/0x48, key line zero, 0x30 with only its 6 fixed
     entries, 0x43 with the factory values;
  #2 (10 269 B): the same with the key line and the 36 per-column 0x30 entries
     plus their 8064 data bytes built from CF.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass

from . import calibration, pins
from .calibration import CalibrationError, UnitChunks  # UnitChunks: the splice lives in calibration

BPL = calibration.BPL  # 152 bytes per line: 8 header + 144 samples
HEADER_LEN = 8
ROWS = calibration.LINES  # 56
COLS = calibration.COLUMNS  # 144
FRAME_LEN = calibration.FRAME_LEN  # 8512
LINES_PER_FRAME = 112  # 336 = 3 frames x 112 lines; interleave 112 / 56 = 2 (0x180091f70)
REQ_LINES = 337  # 336 + 1 so the EP 0x82 read is not a multiple of 64 bytes
RAW_LEN = REQ_LINES * BPL  # 51 224 bytes on EP 0x82 per blank capture
FACTORY_COUNT = calibration.FACTORY_COUNT  # 56

# 6f tag 0x0e entries ([B2] "How Windows creates CalibrationData", stage 3 and stage 6)
SUBTAG_VALUES = 3  # key 0x80000003: 4-byte prefix + 56 factory values (0x18006d240)
SUBTAG_VALUES_LEN = 4 + FACTORY_COUNT
SUBTAG_CHECK = 12  # key 0x8000000c: 4-byte prefix + 5 x 144 int8 (0x18006f880)
SUBTAG_CHECK_LEN = 4 + 5 * COLS  # 724
CHECK_ROWS = (4, 18, 28, 38, 51)  # rows table T+0x90 -> VA 0x18021c118
CHECK_MAX = 200  # threshold argument of stage 6 (0x18006ef50) for type 0x2449

CAL_HEADER = struct.pack("<BHH", 0x02, BPL, REQ_LINES)  # 02 98 00 51 01
CAL_SKIPPED = frozenset({0x26, 0x2E, 0x47, 0x48})
COLUMN_ENTRY_TAG = 0x85  # 0x30 entries ffffffff,850000cc: one per 4-column group
FIXED_LINE_UPDATE_ENTRIES = 6
COLUMN_GROUPS = COLS // 4  # 36
KEY_LINE_LEN = 144
LINE_UPDATE_LEN = COLUMN_GROUPS * ROWS * 4  # 8064
FACTORY_ENTRY_OFFSET = 60  # first bit-packed entry inside chunk 0x43
FACTORY_BITS = 5
FACTORY_ENTRY_LEN = 4 + (FACTORY_BITS * FACTORY_COUNT + 7) // 8  # 39: format, minimum, u16 0, stream
FACTORY_ENTRIES_LEN = 2 * FACTORY_ENTRY_LEN  # 78

PROGRAM1_LEN = pins.PROGRAM1_LEN  # 1917
PROGRAM2_LEN = pins.PROGRAM2_LEN  # 10269
# sha256 of skeleton(program) for programs built from proto/data/cmd02_template.bin
# (calibration.TEMPLATE_SHA256). They pin every byte that is not per unit; computed
# with build_program1/build_program2 on synthetic inputs and checked by
# test_calib_model.py (skeleton() does not depend on the inputs). Program #1's
# skeleton keeps its all-zero key line, and both keep the 0x43 format bytes and
# reserved u16s, so those are pinned too. The values live in synaptics0078/pins.py.
PROGRAM1_SKELETON_SHA256 = pins.PROGRAM1_SKELETON_SHA256
PROGRAM2_SKELETON_SHA256 = pins.PROGRAM2_SKELETON_SHA256
SKELETON_SHA256 = {PROGRAM1_LEN: PROGRAM1_SKELETON_SHA256, PROGRAM2_LEN: PROGRAM2_SKELETON_SHA256}

# Decode matrix of 0x1800c8010 (unrolled in the DLL, selected by chunk 0x42 "Data
# Encoder" 02 08 90 ..: type 2, offset 8, 144 samples, via 0x1800c7ac0):
# M = A (x) A with A = 2*I4 - J4, a 16 x 16 Hadamard matrix with M M^T = 16 I.
_A = tuple(tuple(1 if i == j else -1 for j in range(4)) for i in range(4))
M = tuple(tuple(_A[i // 4][j // 4] * _A[i % 4][j % 4] for j in range(16)) for i in range(16))

# ---------------------------------------------------------------- thresholds
# Gates of the Linux-vs-Windows comparison (cited by proto/README.md).
# CF, compared as q(CF), signed int8 over 56 x 144 (the 0x30 domain):
#   correlation >= 0.95, mean |d| <= 8, fraction within +-16 >= 0.95.
#   Basis: Windows' own check scored 7, i.e. RMS ~6.3 q-units against the factory
#   rows; factory subtag 12 vs Windows q(CF) correlates at 0.994; the signal sd is
#   48; adjacent rows correlate at only 0.72, so a one-row slip fails the gate.
CF_MIN_CORRELATION = 0.95
CF_MAX_MEAN_ABS = 8.0
CF_TOLERANCE = 16
CF_MIN_WITHIN = 0.95
# BF, compared on the decoded pixels (line headers excluded):
#   mean |d| <= 3, fraction within +-8 >= 0.95. The three known units have BF mean
#   127.5-127.9 and sd ~2; re-encode residuals above 8 occur only on ~20 clamped
#   pixels. BF is nearly flat, so its correlation is reported but does not gate.
#   Sanity (needs no Windows data): BF mean within 128 +- 4, sd <= 6.
BF_MAX_MEAN_ABS = 3.0
BF_TOLERANCE = 8
BF_MIN_WITHIN = 0.95
BF_SANITY_MEAN, BF_SANITY_MEAN_TOL, BF_SANITY_MAX_SD = 128.0, 4.0, 6.0
# Factory values (6f subtag 3 vs CalibrationData 0xEC): all 56 must match.
FACTORY_MIN_MATCH = FACTORY_COUNT


def s8(b: int) -> int:
    """A byte as a signed int8."""
    return b - 256 if b > 127 else b


def q(b: int) -> int:
    """q(b) as a signed int: calibration.rescale (truncation toward zero, clamp to int8)."""
    return s8(calibration.rescale(b, False))


# ------------------------------------------------------------- frames


def average_frame2(raw: bytes) -> bytes:
    """Frame 2 of a blank capture, averaged as the DLL's callback does (0x1800907e0 -> 0x180091f70).

    Output row r is the bytewise floor((L[112 + 2r] + L[113 + 2r]) / 2), the 8 header
    bytes included. Frame 1, frame 3 and the extra 337th line are ignored.
    """
    need = (LINES_PER_FRAME + 2 * ROWS) * BPL
    if len(raw) < need:
        raise CalibrationError(f"raw capture is {len(raw)} bytes, frame 2 needs {need}")
    src = memoryview(raw)[LINES_PER_FRAME * BPL :]
    out = bytearray()
    for r in range(ROWS):
        a = src[(2 * r) * BPL : (2 * r + 1) * BPL]
        b = src[(2 * r + 1) * BPL : (2 * r + 2) * BPL]
        out += bytes((x + y) // 2 for x, y in zip(a, b))
    return bytes(out)


def decode_line(samples: bytes) -> bytes:
    """Decode 144 samples in 9 blocks of 16: y = clamp((M x) >> 2, 0, 255) (0x1800c8010)."""
    if len(samples) != COLS:
        raise CalibrationError(f"a line has {COLS} samples, got {len(samples)}")
    out = bytearray()
    for blk in range(0, COLS, 16):
        x = samples[blk : blk + 16]
        for row in M:
            out.append(max(0, min(255, sum(m * v for m, v in zip(row, x)) >> 2)))
    return bytes(out)


def decode_frame(avg: bytes) -> bytes:
    """Decode every line of an averaged 56 x 152 frame; the 8-byte line headers stay as they are."""
    if len(avg) != FRAME_LEN:
        raise CalibrationError(f"frame is {len(avg)} bytes, need {FRAME_LEN}")
    return b"".join(
        avg[r * BPL : r * BPL + HEADER_LEN] + decode_line(avg[r * BPL + HEADER_LEN : (r + 1) * BPL])
        for r in range(ROWS)
    )


def pixels(frame: bytes) -> bytes:
    """The 56 x 144 samples of a frame, line headers dropped."""
    if len(frame) != FRAME_LEN:
        raise CalibrationError(f"frame is {len(frame)} bytes, need {FRAME_LEN}")
    return b"".join(frame[r * BPL + HEADER_LEN : (r + 1) * BPL] for r in range(ROWS))


def header_rows_ok(frame: bytes) -> int:
    """How many lines have header byte 2 == 2r + 1, as averaging line pairs predicts (information only)."""
    return sum(1 for r in range(ROWS) if frame[r * BPL + 2] == 2 * r + 1)


# --------------------------------------------------------- factory data


def factory_values(by_subtag: dict) -> bytes:
    """The 56 factory values: 6f tag 0x0e, subtag 3, value[4:60] (stage 3, 0x18006d240)."""
    value = by_subtag.get(SUBTAG_VALUES)
    if value is None:
        raise CalibrationError(f"6f reply has no subtag {SUBTAG_VALUES}")
    if len(value) != SUBTAG_VALUES_LEN:
        raise CalibrationError(f"subtag {SUBTAG_VALUES} is {len(value)} bytes, need {SUBTAG_VALUES_LEN}")
    return bytes(value[4:])


def check_reference(by_subtag: dict) -> bytes:
    """The 724-byte subtag 12 value Windows checks CF against (stage 6, 0x18006f880)."""
    value = by_subtag.get(SUBTAG_CHECK)
    if value is None:
        raise CalibrationError(f"6f reply has no subtag {SUBTAG_CHECK}")
    if len(value) != SUBTAG_CHECK_LEN:
        raise CalibrationError(f"subtag {SUBTAG_CHECK} is {len(value)} bytes, need {SUBTAG_CHECK_LEN}")
    return bytes(value)


def check_score(cf: bytes, factory_sub12: bytes, rows: tuple = CHECK_ROWS) -> int:
    """Windows' acceptance score of a CF (0x180071430): sum (s8(F[i][c]) - q(CF[row_i][c]))^2 // 4096.

    `factory_sub12` is the 724-byte subtag 12 value: a 4-byte prefix, then 5 x 144 int8
    for the rows in `rows`. The CF passes when the score is <= CHECK_MAX (stored at C+0xbc).
    """
    if len(cf) != FRAME_LEN:
        raise CalibrationError(f"CF is {len(cf)} bytes, need {FRAME_LEN}")
    if len(factory_sub12) != SUBTAG_CHECK_LEN:
        raise CalibrationError(f"subtag 12 is {len(factory_sub12)} bytes, need {SUBTAG_CHECK_LEN}")
    if len(rows) != 5 or not all(0 <= r < ROWS for r in rows):
        raise CalibrationError(f"check rows {rows} are not 5 rows of 0..{ROWS - 1}")
    ref = factory_sub12[4:]
    total = sum(
        (s8(ref[i * COLS + c]) - q(cf[r * BPL + HEADER_LEN + c])) ** 2 for i, r in enumerate(rows) for c in range(COLS)
    )
    return total // 4096


# ------------------------------------------------------ per-unit chunks


def key_line(cf: bytes) -> bytes:
    """Chunk 0x34 bytes 0..143: CF rows 18/28/38, one 48-column band each, q then 5 -> 4 (0x18008af70)."""
    if len(cf) != FRAME_LEN:
        raise CalibrationError(f"CF is {len(cf)} bytes, need {FRAME_LEN}")
    band = calibration.KEY_BAND
    return b"".join(
        bytes(
            calibration.rescale(x, True)
            for x in cf[r * BPL + HEADER_LEN + b * band : r * BPL + HEADER_LEN + (b + 1) * band]
        )
        for b, r in enumerate(calibration.KEY_ROWS)
    )


def line_update_data(cf: bytes) -> bytes:
    """Chunk 0x30 data: q(CF), 36 column groups x 56 rows x 4 columns (column-group-major)."""
    if len(cf) != FRAME_LEN:
        raise CalibrationError(f"CF is {len(cf)} bytes, need {FRAME_LEN}")
    out = bytearray()
    for k in range(COLUMN_GROUPS):
        for j in range(ROWS):
            base = j * BPL + HEADER_LEN + 4 * k
            out += bytes(calibration.rescale(x, False) for x in cf[base : base + 4])
    return bytes(out)


def factory_entries(factory56: bytes) -> bytes:
    """Chunk 0x43 bytes 60..137: the factory values bit-packed, written twice.

    Each entry is (bits - 1) | 8, minimum, u16 0, then the 5-bit stream. Values that do
    not pack to exactly 5 bits do not fit the template's layout: CalibrationError.
    """
    if len(factory56) != FACTORY_COUNT:
        raise CalibrationError(f"{len(factory56)} factory values, need {FACTORY_COUNT}")
    bits, mn, stream = calibration.bitpack(bytes(factory56))
    if bits != FACTORY_BITS:
        raise CalibrationError(f"factory values need {bits} bits, the layout has {FACTORY_BITS}")
    entry = struct.pack("<BBH", (bits - 1) | 8, mn, 0) + stream
    return entry * 2


def baseline_chunk(bf: bytes) -> bytes:
    """Chunk 0x48 data of the identify program: u16 152, u16 56, then BF verbatim (headers included)."""
    if len(bf) != FRAME_LEN:
        raise CalibrationError(f"BF is {len(bf)} bytes, need {FRAME_LEN}")
    return struct.pack("<HH", BPL, ROWS) + bytes(bf)


def derive_chunks(factory56: bytes, cf: bytes, bf: bytes) -> UnitChunks:
    """The four per-unit regions of the 18869-byte identify program, without building it."""
    return UnitChunks(key_line(cf), line_update_data(cf), factory_entries(factory56), baseline_chunk(bf))


# ---------------------------------------------------- calibration programs


def _line_update_entries(data: bytes) -> tuple[list[bytes], list[bytes]]:
    """(fixed entries, per-column entries) of a 0x30 chunk."""
    if len(data) < 4:
        raise CalibrationError("chunk 0x30 is too short")
    (n,) = struct.unpack_from("<L", data, 0)
    if 4 + 8 * n > len(data):
        raise CalibrationError(f"chunk 0x30 claims {n} entries, more than it holds")
    ents = [bytes(data[4 + 8 * i : 12 + 8 * i]) for i in range(n)]
    cols = [e for e in ents if struct.unpack_from("<L", e, 4)[0] >> 24 == COLUMN_ENTRY_TAG]
    fixed = [e for e in ents if struct.unpack_from("<L", e, 4)[0] >> 24 != COLUMN_ENTRY_TAG]
    return fixed, cols


def build_program(template: bytes, factory56: bytes, cf: bytes | None = None) -> bytes:
    """Calibration program #1 (cf=None) or #2 (cf given), from the identify template.

    Assembler 0x18007e380 with capture type 0: no 0x26 (0x18008c720 returns for type 0),
    no 0x2e/0x47 (0x18008cc90 needs flags 0x800/0x10000/0x80000; 0x47 only comes from
    0x18008d960, called by it), no 0x48 (0x18008dde0 needs capture type 4). The 0x30
    line update (0x18008dac0 -> 0x18009ba40) keeps its 6 fixed entries and adds the 36
    per-column entries and their data only when CF exists (null checks on C+0x80 at
    0x18009c480). The key line stays as in the DLL table (all zero) without a CF.
    """
    entries = factory_entries(factory56)
    if cf is not None and len(cf) != FRAME_LEN:
        raise CalibrationError(f"CF is {len(cf)} bytes, need {FRAME_LEN}")
    _, chunks = calibration.split_chunks(template)
    out = []
    seen = set()
    for typ, data in chunks:
        if typ in CAL_SKIPPED:
            continue
        seen.add(typ)
        p = bytearray(data)
        if typ == 0x34:
            p[0:KEY_LINE_LEN] = bytes(KEY_LINE_LEN) if cf is None else key_line(cf)
        elif typ == 0x30:
            fixed, cols = _line_update_entries(p)
            if len(fixed) != FIXED_LINE_UPDATE_ENTRIES or len(cols) != COLUMN_GROUPS:
                raise CalibrationError(
                    f"template 0x30 has {len(fixed)} fixed and {len(cols)} column entries, "
                    f"need {FIXED_LINE_UPDATE_ENTRIES} and {COLUMN_GROUPS}"
                )
            if cf is None:
                p = bytearray(struct.pack("<L", len(fixed)) + b"".join(fixed))
            else:
                p = bytearray(
                    struct.pack("<L", len(fixed) + len(cols)) + b"".join(fixed) + b"".join(cols) + line_update_data(cf)
                )
        elif typ == 0x43:
            if len(p) < FACTORY_ENTRY_OFFSET + FACTORY_ENTRIES_LEN:
                raise CalibrationError("template chunk 0x43 is too short")
            p[FACTORY_ENTRY_OFFSET : FACTORY_ENTRY_OFFSET + FACTORY_ENTRIES_LEN] = entries
        out.append(struct.pack("<HH", typ, len(p)) + bytes(p))
    missing = {0x34, 0x30, 0x43} - seen
    if missing:
        raise CalibrationError(f"template lacks chunk(s) {sorted(hex(m) for m in missing)}")
    return CAL_HEADER + b"".join(out)


def build_program1(template: bytes, factory56: bytes) -> bytes:
    """Program #1: blank capture #1, no calibration applied (gives CF)."""
    return build_program(template, factory56, None)


def build_program2(template: bytes, factory56: bytes, cf: bytes) -> bytes:
    """Program #2: blank capture #2 with CF applied (gives BF after decoding)."""
    return build_program(template, factory56, cf)


def program_chunks(program: bytes) -> dict:
    """{type: data} of a `02` command (first occurrence of each type)."""
    _, chunks = calibration.split_chunks(program)
    out = {}
    for typ, data in chunks:
        out.setdefault(typ, bytes(data))
    return out


def skeleton(program: bytes) -> bytes:
    """`program` with its per-unit byte ranges zeroed; everything else is kept.

    Zeroed:
      - chunk 0x34 bytes 0..143 (key line), but NOT in a PROGRAM1_LEN-byte program:
        program #1 has no CF, so its key line must stay all zero and is pinned;
      - the 0x30 data after its entry table (the per-column q(CF) values; none in #1);
      - in each of the two bit-packed 0x43 entries (bytes 60..98 and 99..137, each
        format byte (bits - 1) | 8, minimum, u16 reserved, 35-byte stream) only the
        minimum and the stream. The format byte (0x0c) and the reserved u16 (0) stay
        and are pinned.
    The header, every chunk type and length, all fixed chunk bytes and the 0x30 entry
    table stay, so the sha256 of the result pins all of them. The two 0x43 entries must
    be identical (Windows writes the same entry twice): CalibrationError otherwise, as
    on a layout that does not parse.
    """
    header, chunks = calibration.split_chunks(program)
    keep_key_line = len(program) == PROGRAM1_LEN
    out = bytearray(header)
    for typ, data in chunks:
        p = bytearray(data)
        if typ == 0x34:
            if len(p) < KEY_LINE_LEN:
                raise CalibrationError("chunk 0x34 is too short")
            if not keep_key_line:
                p[0:KEY_LINE_LEN] = bytes(KEY_LINE_LEN)
        elif typ == 0x30:
            if len(p) < 4:
                raise CalibrationError("chunk 0x30 is too short")
            (n,) = struct.unpack_from("<L", p, 0)
            d0 = 4 + 8 * n
            if d0 > len(p):
                raise CalibrationError(f"chunk 0x30 claims {n} entries, more than it holds")
            p[d0:] = bytes(len(p) - d0)
        elif typ == 0x43:
            if len(p) < FACTORY_ENTRY_OFFSET + FACTORY_ENTRIES_LEN:
                raise CalibrationError("chunk 0x43 is too short")
            first = FACTORY_ENTRY_OFFSET
            second = FACTORY_ENTRY_OFFSET + FACTORY_ENTRY_LEN
            if p[first:second] != p[second : second + FACTORY_ENTRY_LEN]:
                raise CalibrationError("chunk 0x43: the two factory entries differ")
            for e in (first, second):
                p[e + 1] = 0  # minimum
                p[e + 4 : e + FACTORY_ENTRY_LEN] = bytes(FACTORY_ENTRY_LEN - 4)  # 5-bit stream
        out += struct.pack("<HH", typ, len(p)) + p
    return bytes(out)


def skeleton_sha256(program: bytes) -> str:
    return hashlib.sha256(skeleton(program)).hexdigest()


# ------------------------------------------------------------ comparison


@dataclass(frozen=True)
class Comparison:
    n: int
    mean_abs: float
    max_abs: int
    correlation: float | None  # None when either side is constant
    within: float  # fraction of |d| <= tolerance
    tolerance: int

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "mean_abs": round(self.mean_abs, 4),
            "max_abs": self.max_abs,
            "correlation": None if self.correlation is None else round(self.correlation, 6),
            "within": round(self.within, 6),
            "tolerance": self.tolerance,
        }


def compare(linux, windows, tolerance: int) -> Comparison:
    """Mean and max |d|, Pearson correlation and the fraction within +-tolerance of two equal-length series."""
    a, b = list(linux), list(windows)
    if len(a) != len(b) or not a:
        raise ValueError(f"cannot compare {len(a)} with {len(b)} values")
    n = len(a)
    d = [abs(x - y) for x, y in zip(a, b)]
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    corr = sab / math.sqrt(saa * sbb) if saa > 0 and sbb > 0 else None
    return Comparison(n, sum(d) / n, max(d), corr, sum(1 for v in d if v <= tolerance) / n, tolerance)


def q_plane(cf: bytes) -> list[int]:
    """q(CF) as signed int8 over 56 x 144 (the 0x30 domain)."""
    return [q(x) for x in pixels(cf)]


def compare_cf(linux_cf: bytes, windows_cf: bytes) -> Comparison:
    return compare(q_plane(linux_cf), q_plane(windows_cf), CF_TOLERANCE)


def compare_bf(linux_bf: bytes, windows_bf: bytes) -> Comparison:
    return compare(pixels(linux_bf), pixels(windows_bf), BF_TOLERANCE)


def cf_passes(c: Comparison) -> bool:
    return (
        c.correlation is not None
        and c.correlation >= CF_MIN_CORRELATION
        and c.mean_abs <= CF_MAX_MEAN_ABS
        and c.within >= CF_MIN_WITHIN
    )


def bf_passes(c: Comparison) -> bool:
    """Correlation is not part of the BF gate (BF is nearly flat)."""
    return c.mean_abs <= BF_MAX_MEAN_ABS and c.within >= BF_MIN_WITHIN


def factory_matches(linux: bytes, windows: bytes) -> int:
    """How many of the 56 factory values are identical."""
    if len(linux) != len(windows):
        return 0
    return sum(1 for x, y in zip(linux, windows) if x == y)


def bf_sanity(bf: bytes) -> tuple[bool, float, float]:
    """(ok, mean, sd) of the BF pixels: ok when the mean is within 128 +- 4 and sd <= 6."""
    px = pixels(bf)
    n = len(px)
    mean = sum(px) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in px) / n)
    ok = abs(mean - BF_SANITY_MEAN) <= BF_SANITY_MEAN_TOL and sd <= BF_SANITY_MAX_SD
    return ok, mean, sd
