# SPDX-License-Identifier: MIT
"""A small, defensive OLE / Compound File Binary (MSI) reader on bytes in memory, stdlib only.

What it does: parse the header, the DIFAT, the FAT and the directory of one compound file
that starts at `base` in a buffer (a bare MSI, or an MSI embedded contiguously in a setup
exe as a PE resource), and return the regular streams of at least the mini-stream cutoff
(4096 B); the MSI cab streams are of that kind. Mini streams are not read.

Every input is untrusted: sector numbers are bounds-checked against the buffer, every
chain is loop-checked, and the number of FAT and DIFAT sectors, directory entries and
the stream sizes are capped. Memory stays in proportion to the file: the FAT sector
count and the directory are bounded by the number of sectors the buffer holds, the
DIFAT is read only until it has listed the declared FAT sectors, the FAT is an
array('I') (4 bytes an entry), and every stream copied out counts against one byte
budget per call, so directory entries that all point at one big stream cannot each cost
a copy for free. Anything malformed raises CfbError; nothing is written anywhere.
Layout per [MS-CFB] (header fields at the offsets named below).
"""

from __future__ import annotations

import struct
import sys
from array import array

SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
HEADER_LEN = 512
FREESECT, ENDOFCHAIN, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
MAXREGSECT = 0xFFFFFFFA
DIR_ENTRY_LEN = 128
STREAM_OBJECT = 2
MINI_CUTOFF = 4096  # [MS-CFB]: always 4096; smaller streams live in the mini stream

MAX_STREAM = 64 << 20  # one stream
MAX_TOTAL = 256 << 20  # all streams returned by one call
MAX_FAT_SECTORS = 1 << 16
MAX_DIR_ENTRIES = 1 << 16
MAX_SIGNATURES = 64  # OLE signatures tried in one buffer


class CfbError(ValueError):
    """Not a compound file this reader accepts, or a malformed one."""


class CfbBudgetError(CfbError):
    """The streams of one call would go past its byte budget (max_total)."""


def find_signatures(buf: bytes, limit: int = MAX_SIGNATURES) -> list[int]:
    """Offsets of the OLE signature in `buf` (at most `limit`)."""
    out, pos = [], buf.find(SIGNATURE)
    while pos >= 0 and len(out) < limit:
        out.append(pos)
        pos = buf.find(SIGNATURE, pos + 1)
    return out


def _u32_array(data: bytes) -> array:
    """Little-endian u32 values of `data` as an array('I') (4 bytes each, whatever the host)."""
    out = array("I")
    if out.itemsize != 4:  # pragma: no cover - every Linux target has a 4-byte unsigned int
        raise CfbError("this platform has no 4-byte array type")
    out.frombytes(data)
    if sys.byteorder == "big":  # pragma: no cover
        out.byteswap()
    return out


class _File:
    def __init__(self, buf: bytes, base: int):
        if base < 0 or len(buf) - base < HEADER_LEN:
            raise CfbError("too short for a compound file header")
        hdr = bytes(buf[base : base + HEADER_LEN])
        if hdr[:8] != SIGNATURE:
            raise CfbError("no compound file signature")
        major, byte_order, shift, mini_shift = struct.unpack_from("<HHHH", hdr, 0x1A)
        if (major, shift) not in ((3, 9), (4, 12)):
            raise CfbError(f"version {major} with sector shift {shift} is not supported")
        if byte_order != 0xFFFE or mini_shift != 6:
            raise CfbError("bad byte order mark or mini sector shift")
        self.major = major
        self.ss = 1 << shift
        (
            self.n_fat,
            self.first_dir,
            _transaction,
            self.mini_cutoff,
            _first_mini_fat,
            _n_mini_fat,
            first_difat,
            n_difat,
        ) = struct.unpack_from("<8I", hdr, 0x2C)
        if self.mini_cutoff != MINI_CUTOFF:
            raise CfbError(f"mini stream cutoff {self.mini_cutoff} is not {MINI_CUTOFF}")
        self.buf, self.base = buf, base
        # Sector i starts one sector after the base (the header sector, 512 B or 4096 B).
        self.n_sectors = (len(buf) - base) // self.ss - 1
        if self.n_sectors <= 0:
            raise CfbError("no sectors after the header")
        # Each FAT sector is a sector of this buffer: the count is bounded by the file size.
        if self.n_fat == 0 or self.n_fat > MAX_FAT_SECTORS or self.n_fat > self.n_sectors:
            raise CfbError(f"{self.n_fat} FAT sectors is not plausible")
        if n_difat > self.n_sectors:
            raise CfbError(f"{n_difat} DIFAT sectors is not plausible")
        per = self.ss // 4
        fat_sectors = [x for x in struct.unpack_from("<109I", hdr, 0x4C) if x <= MAXREGSECT][: self.n_fat]
        nxt, seen = first_difat, set()
        for _ in range(n_difat):
            if len(fat_sectors) >= self.n_fat or nxt in (FREESECT, ENDOFCHAIN):
                break  # every declared FAT sector is listed: the rest of the DIFAT is not read
            if nxt in seen:
                raise CfbError("DIFAT chain loops")
            seen.add(nxt)
            vals = struct.unpack(f"<{per}I", self.sector(nxt))
            fat_sectors += [x for x in vals[:-1] if x <= MAXREGSECT][: self.n_fat - len(fat_sectors)]
            nxt = vals[-1]
        if len(fat_sectors) != self.n_fat:
            raise CfbError(f"the DIFAT lists {len(fat_sectors)} FAT sectors, the header says {self.n_fat}")
        fat = array("I")
        for fs in fat_sectors:
            fat += _u32_array(self.sector(fs))
        del fat[self.n_sectors :]  # entries past the last sector of the buffer can never be used
        self.fat = fat

    def sector(self, i: int) -> bytes:
        if not 0 <= i < self.n_sectors:
            raise CfbError(f"sector {i} is outside the file")
        off = self.base + (i + 1) * self.ss
        return self.buf[off : off + self.ss]

    def chain(self, start: int, limit: int) -> bytes:
        """The sectors of the chain from `start`, cut to at most `limit` bytes; a longer chain raises."""
        out, cur, seen = bytearray(), start, bytearray(self.n_sectors)
        while cur not in (ENDOFCHAIN, FREESECT):
            if cur >= len(self.fat):
                raise CfbError(f"sector {cur} is outside the FAT or the file")
            if seen[cur]:
                raise CfbError("sector chain loops")
            seen[cur] = 1
            out += self.sector(cur)
            if len(out) > limit + self.ss:
                raise CfbError("stream longer than allowed")
            cur = self.fat[cur]
        del out[limit:]  # the slack of the last sector
        return bytes(out)


def iter_streams(
    buf: bytes,
    base: int = 0,
    *,
    max_stream: int = MAX_STREAM,
    max_total: int = MAX_TOTAL,
    prefixes: tuple[bytes, ...] | None = None,
):
    """Yield (entry name, data) for the regular streams of the compound file at `base`, in directory order.

    Streams under the mini-stream cutoff (stored in the mini stream) and over
    `max_stream` bytes are skipped; with `prefixes`, so is a stream whose first sector
    starts with none of them (it is not copied). `max_total` is the byte budget of the
    call: every stream copied out counts, a directory entry repeating another one's
    stream included, and the stream that would go past it raises CfbBudgetError before it
    is read. Raises CfbError on any malformed structure (streams already yielded stay valid).
    """
    f = _File(buf, base)
    dirs = f.chain(f.first_dir, min(MAX_DIR_ENTRIES * DIR_ENTRY_LEN, f.n_sectors * f.ss))
    total = 0
    for i in range(0, min(len(dirs), MAX_DIR_ENTRIES * DIR_ENTRY_LEN), DIR_ENTRY_LEN):
        e = dirs[i : i + DIR_ENTRY_LEN]
        if len(e) < DIR_ENTRY_LEN or e[0x42] != STREAM_OBJECT:
            continue
        (name_len,) = struct.unpack_from("<H", e, 0x40)
        name = e[: min(name_len, 64)].decode("utf-16-le", "replace").rstrip("\x00")
        (start,) = struct.unpack_from("<I", e, 0x74)
        (size,) = struct.unpack_from("<Q", e, 0x78)
        if f.major == 3:
            size &= 0xFFFFFFFF  # [MS-CFB]: the high 32 bits may hold garbage in version 3
        if size < f.mini_cutoff or size > max_stream:
            continue
        if prefixes is not None and not f.sector(start).startswith(prefixes):
            continue
        if total + size > max_total:
            raise CfbBudgetError(f"streams larger than the {max_total} bytes allowed in total")
        total += size
        data = f.chain(start, size)
        if len(data) != size:
            raise CfbError(f"stream {name!r}: its chain is shorter than its size")
        yield name, data


def streams(buf: bytes, base: int = 0, *, max_stream: int = MAX_STREAM, max_total: int = MAX_TOTAL, prefixes=None):
    """[(entry name, data), ...]: iter_streams as a list (CfbError on any malformed structure)."""
    return list(iter_streams(buf, base, max_stream=max_stream, max_total=max_total, prefixes=prefixes))
