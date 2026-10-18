# SPDX-License-Identifier: MIT
"""Synthetic fixtures for the synaptics0078 tests: no vendor bytes, no unit data.

- a fake "DLL": random bytes under the real chunk headers (types and lengths), a
  random 10500-byte "module", and decoy copies of the headers;
- the template those give, restated here field by field from the design note's table
  (research/steps/b5-setup-design.md), independently of template.py;
- pins for them (synthetic_pins), so the real pins are never needed;
- tiny writers for a stored CAB, a version 3 OLE (MSI-like) file with a scattered
  stream, a fake setup exe and a zip, for the Origin-style chain;
- synthetic calibration inputs (factory values, CF, BF, the 6f reply).
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import random
import struct
import zipfile
from pathlib import Path

from synaptics0078 import calib_model as cm
from synaptics0078 import calibration, pins, template

REPO = Path(__file__).resolve().parents[2]
LOCAL_B5 = REPO / "local" / "b5"
BSDTAR = Path("/usr/bin/bsdtar")


def sha(data) -> str:
    return hashlib.sha256(data).hexdigest()


def rand(seed: str, n: int) -> bytes:
    return random.Random(seed).randbytes(n)


# ----------------------------------------------------------------- the DLL

FAKE_CHUNKS = {c.name: rand("chunk " + c.name, c.length) for c in pins.DEFAULT_PINS.dll_chunks}
MODULE = rand("module", pins.MODULE_LEN)


def _hdr(typ: int, n: int) -> bytes:
    return struct.pack("<HH", typ, n)


def expected_template(fake: dict[str, bytes] = FAKE_CHUNKS) -> bytes:
    """The template a DLL holding `fake` must give: the design table, restated byte by byte."""

    def chunk(typ, data):
        return _hdr(typ, len(data)) + data

    def dll(name):
        c = pins.DEFAULT_PINS.dll_chunk(name)
        return chunk(c.type, fake[name])

    ts = bytearray(fake["timeslot"])
    ts[0:144] = bytes(144)  # key line: per unit
    for off, val in {149: 0x2B, 150: 0x26, 152: 0x2B, 153: 0x26, 155: 0x2B, 156: 0x24}.items():
        ts[off] = val
    finger = struct.pack(
        "<HBBHBBIIIIIIHHBBBB",
        *(0xB2FB, 0x0F, 0, 0, 0x0F, 0, 0x30, 0xA0 + 0x20000, 0xA0 + 0x9FFE0, 0x8001, 0x20A, 0x190B),
        *(50000, 60000, 1, 9, 0x10, 0),
    )
    image = struct.pack("<HHBBHHHHHHH8B", 2, 24, 0x02, 0, 0, 144, 56, 333, 0, 400, 380, 60, 50, 50, 50, 100, 10, 2, 2)
    fixed = [(0xFF, 0x072000AD), (0xFF, 0x073000B5), (0xFF, 0x074000B8)]
    fixed += [(0xFFFF, 0x4750012F), (0xFFFF, 0x4760019C), (0xFF, 0x17700374)]
    entries = fixed + [(0xFFFFFFFF, 0x85000000 | c) for c in range(0, 144, 4)]
    line_update = struct.pack("<I", 42) + b"".join(struct.pack("<II", *e) for e in entries) + bytes(8064)
    transform = bytearray(struct.pack("<I", 15) + bytes(range(12, 68)) + bytes(78) + bytes(14))
    transform[147] = 0x0C
    body = [
        chunk(0x23, b""),
        *(dll(n) for n in ("reg32", "list16_92", "list32_40", "list16_64", "list32_868", "list16_12")),
        chunk(0x34, bytes(ts)),
        dll("encoder"),
        chunk(0x2F, struct.pack("<I", 56)),
        chunk(0x29, struct.pack("<I", 144)),
        chunk(0x35, struct.pack("<I", 160)),
        chunk(0x17, b""),
        chunk(0x26, finger),
        chunk(0x2E, image),
        chunk(0x47, b""),
        chunk(0x44, struct.pack("<I", 1)),
        chunk(0x30, line_update),
        chunk(0x43, bytes(transform)),
        chunk(0x48, bytes(4 + 56 * 152)),
    ]
    out = b"\x02" + struct.pack("<HH", 152, 0) + b"".join(body)
    assert len(out) == 18869
    return out


def pe_header(dll: bool = True) -> bytes:
    """64-byte MZ stub, e_lfanew 0x40, PE signature and a COFF header (IMAGE_FILE_DLL when dll)."""
    mz = bytearray(0x40)
    mz[0:2] = b"MZ"
    struct.pack_into("<I", mz, 0x3C, 0x40)
    coff = struct.pack("<HHIIIHH", 0x8664, 0, 0, 0, 0, 0xF0, 0x2022 if dll else 0x0022)
    return bytes(mz) + b"PE\0\0" + coff


def synthetic_dll(
    fake: dict[str, bytes] = FAKE_CHUNKS,
    module: bytes | None = MODULE,
    omit: tuple[str, ...] = (),
    module_offset: int = 0x9000,
    size: int = 0x20000,
) -> bytes:
    """A fake DLL: PE header, random filler, decoys, the fake chunks, the fake module."""
    out = bytearray(rand("filler", size))
    head = pe_header()
    out[: len(head)] = head
    at = 0x400
    for c in pins.DEFAULT_PINS.dll_chunks:
        # a decoy first: the right header, other data (the builder must skip it)
        decoy = _hdr(c.type, c.length) + rand("decoy " + c.name, c.length)
        out[at : at + len(decoy)] = decoy
        at += len(decoy) + 0x40
        if c.name not in omit:
            whole = _hdr(c.type, c.length) + fake[c.name]
            out[at : at + len(whole)] = whole
            at += len(whole) + 0x40
    assert at < module_offset
    if module is not None:
        out[module_offset : module_offset + len(module)] = module
    return bytes(out)


def synthetic_pins(tmpl: bytes | None = None, module: bytes = MODULE, fake: dict[str, bytes] = FAKE_CHUNKS):
    """Pins for the synthetic DLL, module and template (every gate the real pins have)."""
    tmpl = expected_template(fake) if tmpl is None else tmpl
    header_sha, chunk_pins = template.chunk_pins(tmpl)
    p1 = cm.build_program1(tmpl, FACTORY)
    p2 = cm.build_program2(tmpl, FACTORY, CF)
    return dataclasses.replace(
        pins.DEFAULT_PINS,
        module_sha256=sha(module),
        module_head_sha256=sha(module[:32]),
        template_sha256=sha(tmpl),
        template_header_sha256=header_sha,
        template_chunks=chunk_pins,
        dll_chunks=tuple(
            pins.ChunkPin(c.name, c.type, c.length, sha(_hdr(c.type, c.length) + fake[c.name]))
            for c in pins.DEFAULT_PINS.dll_chunks
        ),
        program1_skeleton_sha256=cm.skeleton_sha256(p1),
        program2_skeleton_sha256=cm.skeleton_sha256(p2),
    )


# ------------------------------------------------------------- containers


def make_cab(files: list[tuple[str, bytes]]) -> bytes:
    """A stored (uncompressed) cabinet, one folder, CFDATA checksums 0 (= not checked)."""
    names = [n.encode("ascii") + b"\0" for n, _ in files]
    coff_files = 36 + 8
    files_len = sum(16 + len(n) for n in names)
    blob = b"".join(d for _, d in files)
    blocks = [blob[i : i + 0x8000] for i in range(0, len(blob), 0x8000)] or [b""]
    data_start = coff_files + files_len
    cfdata = b"".join(struct.pack("<IHH", 0, len(b), len(b)) + b for b in blocks)
    total = data_start + len(cfdata)
    header = b"MSCF" + struct.pack("<IIIIIBBHHHHH", 0, total, 0, coff_files, 0, 3, 1, 1, len(files), 0, 0x1234, 0)
    folder = struct.pack("<IHH", data_start, len(blocks), 0)
    entries, off = b"", 0
    date, tm = (40 << 9) | (1 << 5) | 1, 0
    for (_, d), n in zip(files, names):
        entries += struct.pack("<IIHHHH", len(d), off, 0, date, tm, 0x20) + n
        off += len(d)
    out = header + folder + entries + cfdata
    assert len(out) == total
    return out


FREESECT, ENDOFCHAIN, FATSECT, NOSTREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF


def make_ole(streams: list[tuple[str, bytes]], scatter: bool = True) -> bytes:
    """A version 3 compound file (512-byte sectors) with regular streams (each >= 4096 B).

    scatter: each stream's sectors are chained out of order (as in the Origin MSI), so the
    stream is not a contiguous run in the file.
    """
    assert len(streams) <= 3 and all(len(d) >= 4096 for _, d in streams)
    counts = [-(-len(d) // 512) for _, d in streams]
    n_data = sum(counts)
    n_fat = 1
    while n_fat * 128 < n_fat + 1 + n_data:
        n_fat += 1
    dir_sector = n_fat
    total = n_fat + 1 + n_data
    fat = [FREESECT] * (n_fat * 128)
    for i in range(n_fat):
        fat[i] = FATSECT
    fat[dir_sector] = ENDOFCHAIN
    sectors: dict[int, bytes] = {}
    starts, nxt = [], dir_sector + 1
    for (_, data), k in zip(streams, counts):
        idx = list(range(nxt, nxt + k))
        nxt += k
        order = (idx[0::2] + idx[1::2]) if scatter else idx
        for j, s in enumerate(order):
            fat[s] = order[j + 1] if j + 1 < len(order) else ENDOFCHAIN
            sectors[s] = data[j * 512 : (j + 1) * 512].ljust(512, b"\0")
        starts.append(order[0])

    def entry(name, typ, start, size, child=NOSTREAM, right=NOSTREAM):
        raw = (name + "\0").encode("utf-16-le")
        return (
            raw.ljust(64, b"\0")
            + struct.pack("<HBBIII", len(raw), typ, 1, NOSTREAM, right, child)
            + bytes(16 + 4 + 16)
            + struct.pack("<IQ", start, size)
        )

    dirs = entry("Root Entry", 5, ENDOFCHAIN, 0, child=1 if streams else NOSTREAM)
    for i, ((name, data), start) in enumerate(zip(streams, starts)):
        dirs += entry(name, 2, start, len(data), right=i + 2 if i + 1 < len(streams) else NOSTREAM)
    dirs = dirs.ljust(512, b"\0")
    header = bytearray(512)
    header[0:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHHH", header, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 0x28, 0, n_fat, dir_sector, 0, 4096, ENDOFCHAIN, 0, ENDOFCHAIN, 0)
    difat = list(range(n_fat)) + [FREESECT] * (109 - n_fat)
    struct.pack_into("<109I", header, 0x4C, *difat)
    body = bytearray()
    for s in range(total):
        if s < n_fat:
            body += struct.pack("<128I", *fat[s * 128 : (s + 1) * 128])
        elif s == dir_sector:
            body += dirs
        else:
            body += sectors[s]
    return bytes(header) + bytes(body)


def fake_setup_exe(ole: bytes) -> bytes:
    """A non-DLL "setup exe": an MZ stub, padding, the OLE file as a resource, more padding."""
    return pe_header(dll=False) + bytes(0x1000) + ole + b"resource tail" + bytes(0x200)


def make_zip(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)
    return buf.getvalue()


def origin_style_zip(dll: bytes) -> bytes:
    """zip -> setup exe -> OLE (MSI) -> stored cab -> DLL, as in the Origin PC archive."""
    cab = make_cab([("_synaWudfBioUsb_dll_x64", dll), ("readme.txt", b"synthetic\n")])
    ole = make_ole([("cab stream", cab)])
    exe = fake_setup_exe(ole)
    return make_zip([("02_Finger/readme.txt", b"synthetic\n"), ("02_Finger/Synaptics/Setup.exe", exe)])


# ------------------------------------------------------------ calibration

FACTORY = bytes(0x70 + (i * 7) % 32 for i in range(56))  # packs to exactly 5 bits
CF_PIX = [[0x80 + ((r * 7 + c * 3) % 41) - 20 for c in range(144)] for r in range(56)]
BF_RAW_PIX = [[128 + ((r + c) % 3) - 1 for c in range(144)] for r in range(56)]
SERIAL = bytes.fromhex("5e71a1c0ffee")  # synthetic


def _line_header(k: int) -> bytes:
    return bytes([0x01, 0xFE, k + 1, 0x02, 0xF0, 0xF7, 0x01, 0x00])


def raw_capture(rows) -> bytes:
    """337 lines; frame 2 (lines 112..223) holds each row twice, so averaging gives the row back."""
    out = bytearray()
    for k in range(cm.REQ_LINES):
        if 112 <= k < 224:
            j = k - 112
            out += _line_header(j) + bytes(rows[j // 2])
        else:
            out += bytes((0x40 + (k * 3 + c) % 61) for c in range(152))
    return bytes(out)


def averaged(rows) -> bytes:
    return b"".join(bytes([1, 0xFE, 2 * r + 1, 2, 0xF0, 0xF7, 1, 0]) + bytes(rows[r]) for r in range(56))


CF = averaged(CF_PIX)
BF = cm.decode_frame(averaged(BF_RAW_PIX))


def sub12(shift: int = 0) -> bytes:
    v = bytearray(b"\x07\x00\x00\x00")
    for r in cm.CHECK_ROWS:
        v += bytes((max(-128, min(127, cm.q(x) + shift)) & 0xFF) for x in CF_PIX[r])
    return bytes(v)


def factory_reply(check: bytes | None = None) -> bytes:
    subtags = {3: b"\x07\x00\x00\x00" + FACTORY, 12: check if check is not None else sub12()}
    body, ptr = b"", 8
    for sub, value in subtags.items():
        body += struct.pack("<LHHHH", ptr, len(value), 0x0E, sub, 0x8000) + value
        ptr += 12 + len(value)
    return struct.pack("<HLL", 0, 0xC9C, len(subtags)) + body


def rom_info(byte17: int = 0) -> bytes:
    r = bytearray(38)
    r[10], r[11], r[13], r[17] = 6, 0x14, 0x41, byte17
    r[0x12:0x18] = SERIAL
    return bytes(r)


SENSOR_ID = bytes.fromhex("0000 00000000 3700 b700")  # (major 0xb7, minor 0x37): type 0x2449


def unit_chunks() -> calibration.UnitChunks:
    return cm.derive_chunks(FACTORY, CF, BF)
