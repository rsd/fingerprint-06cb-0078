# SPDX-License-Identifier: MIT
"""cfb.py on synthetic compound files: the good case, and malformed ones that must raise CfbError."""

from __future__ import annotations

import struct
import tracemalloc

import pytest

from synaptics0078 import cfb

from . import fixtures as fx

DATA = fx.rand("stream", 9000)
SMALL = b"tiny"  # under the mini-stream cutoff: never read


def ole(**kw) -> bytearray:
    return bytearray(fx.make_ole([("cab", DATA)], **kw))


def fat_entry_offset(buf: bytes, sector: int) -> int:
    """File offset of the FAT entry of `sector` (FAT sector 0 is the first sector)."""
    return 512 + 4 * sector


def first_stream_sector(buf: bytes) -> int:
    (first_dir,) = struct.unpack_from("<I", buf, 0x30)
    entry = 512 + first_dir * 512 + 128  # the stream's directory entry
    return struct.unpack_from("<I", buf, entry + 0x74)[0]


@pytest.mark.parametrize("scatter", [True, False])
def test_reads_a_stream(scatter):
    assert cfb.streams(bytes(ole(scatter=scatter))) == [("cab", DATA)]


def test_embedded_at_an_offset():
    buf = b"junk" * 100 + bytes(ole()) + b"tail"
    pos = cfb.find_signatures(buf)
    assert pos == [400]
    assert cfb.streams(buf, pos[0]) == [("cab", DATA)]


def test_streams_under_the_cutoff_and_over_the_cap_are_skipped():
    buf = bytes(fx.make_ole([("a", DATA), ("b", fx.rand("b", 5000))]))
    assert [n for n, _ in cfb.streams(buf, max_stream=6000)] == ["b"]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda b: b[:100], id="truncated header"),
        pytest.param(lambda b: b"\x00" + b[1:], id="no signature"),
        pytest.param(lambda b: b[:0x1A] + struct.pack("<H", 5) + b[0x1C:], id="bad version"),
        pytest.param(lambda b: b[:0x1C] + b"\xff\xfe" + b[0x1E:], id="bad byte order"),
        pytest.param(lambda b: b[:0x2C] + struct.pack("<I", 0) + b[0x30:], id="no FAT sectors"),
        pytest.param(lambda b: b[:0x2C] + struct.pack("<I", 1 << 20) + b[0x30:], id="huge FAT count"),
        pytest.param(lambda b: b[:0x38] + struct.pack("<I", 0) + b[0x3C:], id="mini cutoff 0"),
        pytest.param(lambda b: b[:0x4C] + struct.pack("<I", 9999) + b[0x50:], id="FAT sector out of range"),
        pytest.param(lambda b: b[:0x30] + struct.pack("<I", 9999) + b[0x34:], id="directory out of range"),
        pytest.param(lambda b: b[:1024], id="file cut short"),
    ],
)
def test_malformed_raises(mutate):
    with pytest.raises(cfb.CfbError):
        cfb.streams(bytes(mutate(bytes(ole()))))


def test_chain_loop_raises():
    buf = ole(scatter=False)
    start = first_stream_sector(buf)
    struct.pack_into("<I", buf, fat_entry_offset(buf, start + 2), start)  # third sector points back to the first
    with pytest.raises(cfb.CfbError, match="loops"):
        cfb.streams(bytes(buf))


def test_chain_to_an_out_of_range_sector_raises():
    buf = ole(scatter=False)
    start = first_stream_sector(buf)
    struct.pack_into("<I", buf, fat_entry_offset(buf, start), 100)  # inside the FAT, past the file
    with pytest.raises(cfb.CfbError, match="outside"):
        cfb.streams(bytes(buf))


def test_chain_past_the_fat_raises():
    buf = ole(scatter=False)
    start = first_stream_sector(buf)
    struct.pack_into("<I", buf, fat_entry_offset(buf, start), 5000)  # past the FAT's 128 entries
    with pytest.raises(cfb.CfbError, match="outside the FAT"):
        cfb.streams(bytes(buf))


def test_short_chain_raises():
    buf = ole(scatter=False)
    start = first_stream_sector(buf)
    struct.pack_into("<I", buf, fat_entry_offset(buf, start), cfb.ENDOFCHAIN)  # chain ends after one sector
    with pytest.raises(cfb.CfbError, match="shorter than its size"):
        cfb.streams(bytes(buf))


def sector_offset(sector: int) -> int:
    return 512 + 512 * sector  # version 3: the header sector is 512 B


def test_difat_loop_raises():
    buf = ole()  # 20 sectors: FAT, directory, 18 stream sectors
    last = 19
    struct.pack_into("<I", buf, 0x2C, 2)  # 2 FAT sectors: the header lists 1, the DIFAT must list the other
    struct.pack_into("<II", buf, 0x44, last, 2)  # first DIFAT sector, 2 DIFAT sectors
    struct.pack_into("<127II", buf, sector_offset(last), *[cfb.FREESECT] * 127, last)  # next: itself
    with pytest.raises(cfb.CfbError, match="DIFAT chain loops"):
        cfb.streams(bytes(buf))


def test_difat_is_not_read_past_the_declared_fat_sectors():
    """The header lists the one FAT sector: a DIFAT chain beyond it (here outside the file) is never read."""
    buf = ole()
    struct.pack_into("<II", buf, 0x44, 9999, 5)
    assert cfb.streams(bytes(buf)) == [("cab", DATA)]


@pytest.mark.parametrize(("offset", "value"), [(0x2C, 21), (0x48, 21)], ids=["FAT sectors", "DIFAT sectors"])
def test_sector_counts_are_bounded_by_the_file(offset, value):
    buf = ole()  # 20 sectors after the header
    struct.pack_into("<I", buf, offset, value)
    with pytest.raises(cfb.CfbError, match="not plausible"):
        cfb.streams(bytes(buf))


def with_duplicate_entries(buf: bytearray, copies: int) -> bytearray:
    """The stream's directory entry (slot 1) copied into slots 2.. (one 512 B sector holds 4)."""
    (first_dir,) = struct.unpack_from("<I", buf, 0x30)
    at = sector_offset(first_dir)
    for i in range(copies):
        buf[at + 128 * (2 + i) : at + 128 * (3 + i)] = buf[at + 128 : at + 256]
    return buf


def test_total_cap_counts_every_copy():
    buf = bytes(with_duplicate_entries(ole(), 2))  # three entries, one 9000 B stream
    assert cfb.streams(buf) == [("cab", DATA)] * 3
    assert len(cfb.streams(buf, max_total=3 * len(DATA))) == 3
    with pytest.raises(cfb.CfbBudgetError, match="allowed in total"):
        cfb.streams(buf, max_total=3 * len(DATA) - 1)
    got = cfb.iter_streams(buf, max_total=2 * len(DATA))
    assert [next(got), next(got)] == [("cab", DATA)] * 2, "what came before the cap stays usable"
    with pytest.raises(cfb.CfbBudgetError):
        next(got)


def test_prefixes_skip_streams_without_copying_them():
    buf = bytes(fx.make_ole([("a", b"MSCF" + DATA[4:]), ("b", DATA)]))
    assert [n for n, _ in cfb.streams(buf, prefixes=(b"MSCF", b"MZ"))] == ["a"]
    assert [n for n, _ in cfb.streams(buf, prefixes=(b"MSCF",), max_total=len(DATA))] == ["a"], "b costs nothing"


def v4_file_with_random_fat(n_fat: int = 109) -> bytes:
    """A version 4 file (4096 B sectors) whose n_fat FAT sectors are random u32 values."""
    hdr = bytearray(4096)
    hdr[0:8] = cfb.SIGNATURE
    struct.pack_into("<HHHHH", hdr, 0x18, 0x3E, 4, 0xFFFE, 12, 6)
    struct.pack_into("<9I", hdr, 0x28, 0, n_fat, 0, 0, 4096, cfb.ENDOFCHAIN, 0, cfb.ENDOFCHAIN, 0)
    struct.pack_into("<109I", hdr, 0x4C, *range(n_fat), *[cfb.FREESECT] * (109 - n_fat))
    return bytes(hdr) + fx.rand("random FAT", n_fat * 4096)


def test_fat_memory_stays_in_proportion_to_the_file():
    """111616 random FAT entries: as a list of ints ~4.5 MB for a 450 KB file; as array('I') its size."""
    buf = v4_file_with_random_fat()
    tracemalloc.start()
    try:
        with pytest.raises(cfb.CfbError):  # the directory chain runs off the random FAT
            cfb.streams(buf)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * len(buf), f"peak {peak} B for a {len(buf)} B file"


def test_oversized_stream_is_refused_while_reading():
    buf = ole(scatter=False)
    (first_dir,) = struct.unpack_from("<I", buf, 0x30)
    struct.pack_into("<Q", buf, 512 + first_dir * 512 + 128 + 0x78, 4096)  # claims 4096 B, chain is longer
    with pytest.raises(cfb.CfbError, match="longer than allowed"):
        cfb.streams(bytes(buf))
