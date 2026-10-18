# SPDX-License-Identifier: MIT
"""Pure parsers for replies from the Synaptics 06cb:0078 fingerprint reader.

No USB, no I/O, no side effects: bytes in, structured result out. Everything
here is unit-testable offline (see test_parsers.py).

Commands covered (all sent plaintext on bulk OUT 0x01, reply on bulk IN 0x81):

  0x01  RomInfo
  0x19  opaque 1-byte command, 68-byte reply, layout NOT documented
  0x75  sensor identification
  0x6f  get factory bits (read-only, probe.py --only 6f)

and, for capture.py, the pure decoders for the EP 0x83 interrupt events and
the 0x51 image reply.

Every field is tagged with how well the references establish it:

  CERTAIN    layout from reference code AND confirmed by a real 0078 reply
  UNCERTAIN  named or guessed by a reference but the meaning is unproven, or
             only an observation from sample replies

References (all read-only, untrusted third-party material):
  [PV]   ashh87/python-validity branch dev-00a8 (PR #288), validitysensor/:
           util.py:6-12      assert_status(): status is u16 LE, 0 == OK
           sensor.py:91-97   RomInfo.get(): cmd 0x01, unpack('<LLBBxBxxxB', rsp[2:18])
           sensor.py:107-117 identify_sensor(): cmd 0x75, unpack('<LHH', rsp[2:10])
           hw_tables.py:209,212  (0x00b7, 0x2449, minor 0x48 / 0x37) rows
           sensor.py:128-151 get_factory_bits(): 0x6f request, entry layout
           sensor.py:259-263 subtag 3 minus a 4-byte prefix = factory calibration values
           sensor_2449.py:85,87,515-545  EP 0x83 event codes, IMAGE_READ_AGAIN
           sensor_2449.py:575-592  parse_image(): the 0x51 reply layout
  [CAL]  research/calibration-analysis.md (sections 0, 2, 3, 7)
  [V63]  nmikhailov/Validity90 issue #63 (reporter cydia2020, a real 06cb:0078):
           recorded replies to 0x01, 0x19, 0x75
  [V69]  nmikhailov/Validity90 issue #69 (reporter junocomp, a 06cb:00a8):
           recorded replies to 0x01, 0x19 (used only for comparison)
  [V90]  nmikhailov/Validity90 prototype/main.c:338 (treats the last byte of
           the 0x01 reply as an "init byte"; 0x07 means initialised on the
           0090 family)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

CMD_ROM_INFO = 0x01
CMD_QUERY_19 = 0x19
CMD_SENSOR_ID = 0x75
CMD_FACTORY_BITS = 0x6F
# pack('<BHHL', 0x6f, 0x0e00, 0, 0) -- [PV] sensor.py:129 with get_factory_bits(0x0e00)
REQ_FACTORY_BITS = bytes.fromhex("6f 00 0e 00 00 00 00 00 00")

# Reply sizes seen on a real 0078 in [V63] (and in the Windows ETW trace).
EXPECTED_LEN = {CMD_ROM_INFO: 38, CMD_QUERY_19: 68, CMD_SENSOR_ID: 10}

EXPECTED_SENSOR_TYPE = 0x2449

CERTAIN = "CERTAIN"
UNCERTAIN = "UNCERTAIN"

# Subset of python-validity hw_tables.py dev_info_table (the two rows that
# resolve to sensor type 0x2449 for major 0x00b7). Key: (major, minor).
# This is NOT the full table: a (major, minor) missing here means "not in this
# subset", not "unknown to the world".
DEVICE_INFO_SUBSET = {
    (0x00B7, 0x37): (0x2449, "SYN 86C  TM03267001"),  # hw_tables.py:212
    (0x00B7, 0x48): (0x2449, "SYN 86C  FM03290001"),  # hw_tables.py:209
}


@dataclass
class Field:
    name: str
    value: str
    certainty: str  # CERTAIN or UNCERTAIN
    note: str = ""


@dataclass
class Decoded:
    command: int
    reply_len: int
    fields: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # Machine-readable values, for tests and callers.
    values: dict = field(default_factory=dict)

    def add(self, name: str, value: str, certainty: str, note: str = "") -> None:
        self.fields.append(Field(name, value, certainty, note))

    def render(self) -> list:
        """Human-readable lines. Uncertain fields are flagged loudly."""
        out = []
        for f in self.fields:
            tag = "[CERTAIN]  " if f.certainty == CERTAIN else "[UNCERTAIN]"
            line = f"  {tag} {f.name}: {f.value}"
            if f.note:
                line += f"  -- {f.note}"
            out.append(line)
        for w in self.warnings:
            out.append(f"  [WARNING]   {w}")
        return out


# ---------------------------------------------------------------- helpers


def hexstr(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


def hexdump(data: bytes, width: int = 16) -> str:
    """Offset + hex rows like the reference logs (offset, 16 bytes per row)."""
    if not data:
        return "  (empty)"
    rows = []
    for off in range(0, len(data), width):
        chunk = data[off : off + width]
        rows.append(f"  {off:04x}  {hexstr(chunk)}")
    return "\n".join(rows)


def u16le(data: bytes, off: int) -> int:
    return int.from_bytes(data[off : off + 2], "little")


def u32le(data: bytes, off: int) -> int:
    return int.from_bytes(data[off : off + 4], "little")


def status_word(reply: bytes) -> int | None:
    """Leading u16 little-endian status of any reply, or None if < 2 bytes.

    CERTAIN: [PV] util.py:6 assert_status(); 0 means OK. Non-zero codes seen
    in [V63] as replies to rejected commands: 0x04be, 0x0401, 0x0491 - their
    meanings are not documented in the references.
    """
    if len(reply) < 2:
        return None
    return u16le(reply, 0)


def nonzero_runs(data: bytes, base: int = 0) -> list:
    """Contiguous runs of non-zero bytes as (absolute_offset, bytes)."""
    runs = []
    i = 0
    while i < len(data):
        if data[i] != 0:
            j = i
            while j < len(data) and data[j] != 0:
                j += 1
            runs.append((base + i, bytes(data[i:j])))
            i = j
        else:
            i += 1
    return runs


def _unix_utc(ts: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))
    except (OverflowError, OSError, ValueError):
        return "n/a"


def _decode_status(d: Decoded, reply: bytes) -> int | None:
    """Fill the status field; return the status or None when undecodable."""
    st = status_word(reply)
    if st is None:
        d.warnings.append(f"reply is {len(reply)} byte(s), too short for a status word")
        return None
    d.values["status"] = st
    if st == 0:
        d.add("status", "0x0000 (OK)", CERTAIN, "[PV] util.py:6")
    else:
        d.add(
            "status",
            f"0x{st:04x} (non-zero = error)",
            CERTAIN,
            "[PV] util.py:6; meaning of this code is not documented",
        )
        d.warnings.append("non-zero status: remaining bytes not decoded")
        return st
    return st


def _check_len(d: Decoded, reply: bytes, need: int) -> bool:
    """Warn on unexpected length; return False if too short to decode body."""
    exp = EXPECTED_LEN[d.command]
    if len(reply) != exp:
        d.warnings.append(f"reply length {len(reply)}, expected {exp} on a 0078 ([V63])")
    if len(reply) < need:
        d.warnings.append(f"reply shorter than the {need} bytes needed to decode; body not decoded")
        return False
    return True


# --------------------------------------------------------------- 0x01


def module_probably_loaded(u1: int) -> tuple:
    """(loaded?, reason) from RomInfo byte 17 ('u1'). UNCERTAIN, see parse_rom_info.

    Evidence: [DLL] 0x18005f960 / 0x180080c00 (u1 is the third version component and is
    tested for non-zero); this unit read 0x00 before the 06 module upload and 0x0e after
    it, and refused a second 06 with status 0x04aa. No python reference tests the byte.
    """
    if u1 != 0:
        return (
            True,
            f"u1 = 0x{u1:02x} is non-zero, so the capture module is probably already loaded (inference, uncertain)",
        )
    return False, "u1 = 0, so no module is loaded (inference, uncertain)"


def parse_rom_info(reply: bytes) -> Decoded:
    """Decode the reply to command 0x01 (RomInfo, 38 bytes on a 0078).

    Layout per [PV] sensor.py:91-97: after the u16 status, rsp[2:18] is
    '<LLBBxBxxxB' = timestamp u32, build u32, major u8, minor u8, 1 skipped
    byte, product u8, 3 skipped bytes, u1 u8. Offsets in the full reply:
    timestamp 2, build 6, major 10, minor 11, (12 skipped), product 13,
    (14-16 skipped), u1 17.
    """
    d = Decoded(CMD_ROM_INFO, len(reply))
    st = _decode_status(d, reply)
    if st != 0:
        return d
    if not _check_len(d, reply, 18):
        return d

    ts = u32le(reply, 2)
    build = u32le(reply, 6)
    major, minor, product, u1 = reply[10], reply[11], reply[13], reply[17]
    d.values.update(timestamp=ts, build=build, major=major, minor=minor, product=product, u1=u1)

    d.add(
        "ROM version (major.minor)",
        f"{major}.{minor}  (0x{major:02x}/0x{minor:02x})",
        CERTAIN,
        "bytes 10,11; [PV] sensor.py:97; 6/0x14 is the key the driver tables use for the 0078 ([V63] reply)",
    )
    d.add(
        "product",
        f"0x{product:02x}",
        CERTAIN,
        "byte 13; field name from [PV]; what it denotes is not documented",
    )
    d.add(
        "timestamp (u32le @2)",
        f"0x{ts:08x} = {ts}; as unix time {_unix_utc(ts)}",
        UNCERTAIN,
        "[PV] names it 'timestamp'; the unix-epoch reading is a guess",
    )
    d.add(
        "build (u32le @6)",
        f"{build} (0x{build:x})",
        UNCERTAIN,
        "[PV] names it 'build'; note the [V63] value 154 equals bcdDevice 1.54 - an observation, not a documented link",
    )
    loaded, why = module_probably_loaded(u1)
    d.values["module_probably_loaded"] = loaded
    d.add(
        "u1 (byte 17) = third version component",
        f"0x{u1:02x} = {u1}; version string would read {major:02d}.{minor:02d}.{u1:02d}.{build:04d}",
        UNCERTAIN,
        "[PV] only names it 'u1'. [DLL] function 0x18005f960 prints RomInfo as '%02u.%02u.%02u.%04u' = major.minor.u1.build "
        "(here 06.20.00.0154 with bcdDevice 0x0154); function 0x180080c00 branches on u1 != 0. "
        "Hardware runs 1-2 (capture-20261013-2049*.log): 0x00 before the 06 module upload, 0x0e after it, and a second 06 "
        "is then refused with status 0x04aa. Reading: non-zero = firmware extension (the module) is loaded. " + why,
    )
    if len(reply) >= 0x1E:
        w = u16le(reply, 0x1C)
        d.values["word_0x1c"] = w
        d.add(
            "u16le @0x1c",
            f"0x{w:04x}",
            UNCERTAIN,
            "0x0000 before the 06 module upload, 0x0102 (bytes 02 01) after it (hardware runs 1-2); no reference decodes it and "
            "the DLL's only RomInfo+0x1a accesses are in unrelated bitmap code, so the meaning is unknown, possibly module info",
        )
    if len(reply) >= 0x18:
        serial = bytes(reply[0x12:0x18])
        d.values["serial"] = serial
        d.add(
            "serial (bytes 0x12-0x17)",
            serial.hex(),
            UNCERTAIN,
            "[CAL] section 2: the vendor driver memcmp's these 6 bytes (RomInfo+0x10) with the serial stored in CalibrationData; not documented by the python references",
        )

    if len(reply) > 0x19:
        d.values["byte_0x19"] = reply[0x19]
        d.add(
            "byte 0x19",
            f"0x{reply[0x19]:02x}",
            UNCERTAIN,
            "0x00 in [V63] (0078), 0x21 in [V69] (00a8); the Windows driver reportedly branches on it; meaning unknown",
        )
    d.values["last_byte"] = reply[-1]
    d.add(
        "last byte",
        f"0x{reply[-1]:02x}",
        UNCERTAIN,
        "[V90] main.c:338 reads it as an 'init byte' (0x07 = initialised, 0090 family); 0x00 in [V63], 0x06 in [V69]; meaning for this sensor unknown",
    )
    known = {0, 1} | set(range(2, 10)) | {10, 11, 13, 17, 0x19, 0x1C, 0x1D, len(reply) - 1} | set(range(0x12, 0x18))
    rest = [(i, reply[i]) for i in range(len(reply)) if i not in known and reply[i] != 0]
    if rest:
        d.add(
            "other non-zero bytes",
            ", ".join(f"[0x{i:02x}]=0x{v:02x}" for i, v in rest),
            UNCERTAIN,
            "not decoded by any reference (differ between the [V63] and [V69] units at some offsets)",
        )
    return d


# --------------------------------------------------------------- 0x19


def parse_query_19(reply: bytes) -> Decoded:
    """Decode the reply to command 0x19 (68 bytes on a 0078).

    No reference documents the layout. python-validity sends it right after
    0x01 as part of the Windows init sequence and ignores the payload (only
    the status is asserted). Only the status is CERTAIN. Everything else is
    reported as an observation.
    """
    d = Decoded(CMD_QUERY_19, len(reply))
    st = _decode_status(d, reply)
    if st != 0:
        return d
    if not _check_len(d, reply, 2):
        return d

    body = reply[2:]
    d.values["body"] = bytes(body)
    d.add(
        "payload",
        f"{len(body)} bytes, layout not documented in any reference",
        UNCERTAIN,
        "[PV] usb.py send_init() asserts only the status",
    )
    runs = nonzero_runs(body, base=2)
    if runs:
        d.add(
            "non-zero runs (offset: bytes)",
            "; ".join(f"0x{o:02x}: {hexstr(b)}" for o, b in runs),
            UNCERTAIN,
            "observation only",
        )
    if len(reply) >= 0x20:
        w1, w2 = u32le(reply, 0x14), u32le(reply, 0x1C)
        d.values.update(word_0x14=w1, word_0x1c=w2)
        d.add(
            "u32le @0x14, @0x1c",
            f"0x{w1:08x}, 0x{w2:08x}",
            UNCERTAIN,
            "these two words differ between the [V63] (0078) and [V69] (00a8) samples while the rest matches; whether per-unit or per-model is unknown",
        )
    return d


# --------------------------------------------------------------- 0x75


def parse_sensor_id(reply: bytes) -> Decoded:
    """Decode the reply to command 0x75 (sensor identification, 10 bytes).

    Layout per [PV] sensor.py:107-117: after the u16 status, rsp[2:10] is
    '<LHH' = zeroes u32 (must be 0), minor u16, major u16. The sensor type is
    NOT in the reply: python-validity looks (major, minor) up in a table
    (hw_tables.py dev_info_lookup). [V63] reply
    '00 00 00 00 00 00 37 00 b7 00' -> major 0x00b7, minor 0x0037 -> table
    row (0x00b7, 0x2449, 0x37, 'SYN 86C  TM03267001') -> sensor type 0x2449.
    """
    d = Decoded(CMD_SENSOR_ID, len(reply))
    st = _decode_status(d, reply)
    if st != 0:
        return d
    if not _check_len(d, reply, 10):
        return d

    zeroes, minor, major = u32le(reply, 2), u16le(reply, 6), u16le(reply, 8)
    d.values.update(zeroes=zeroes, minor=minor, major=major)
    d.add(
        "reserved u32le @2",
        f"0x{zeroes:08x}",
        CERTAIN,
        "[PV] raises 'This was not expected' if non-zero" if zeroes else "zero as [PV] expects",
    )
    if zeroes != 0:
        d.warnings.append("reserved word non-zero: python-validity treats this as unexpected")
    d.add("major (u16le @8)", f"0x{major:04x}", CERTAIN, "[PV] sensor.py:112")
    d.add("minor (u16le @6)", f"0x{minor:04x}", CERTAIN, "[PV] sensor.py:112")

    hit = DEVICE_INFO_SUBSET.get((major, minor))
    if hit is None:
        d.values["sensor_type"] = None
        d.add(
            "sensor type",
            "not determined",
            UNCERTAIN,
            f"(0x{major:04x}, 0x{minor:04x}) is not in the embedded two-row subset of python-validity's table",
        )
    else:
        stype, name = hit
        d.values["sensor_type"] = stype
        d.values["sensor_name"] = name
        d.add(
            "sensor type",
            f"0x{stype:04x}  ('{name}')",
            CERTAIN,
            "by table lookup (python-validity hw_tables.py), not carried in the reply itself; row also present in the vendor DLL",
        )
        if stype != EXPECTED_SENSOR_TYPE:
            d.warnings.append(f"sensor type 0x{stype:04x} differs from the expected 0x{EXPECTED_SENSOR_TYPE:04x}")
    return d


# --------------------------------------------------------------- 0x6f

FACTORY_SUBTAG_VALUES = 3  # [PV] sensor.py:260 factory_bits[3][4:]
FACTORY_PREFIX_LEN = 4  # [PV] sensor.py:260; [CAL] section 2: 07 00 00 00 precedes the 56 values
FACTORY_VALUE_COUNT = 56  # [CAL] section 0


def parse_factory_bits(reply: bytes) -> Decoded:
    """Decode the reply to 0x6f / tag 0x0e00 (get factory bits).

    Layout per [PV] sensor.py:128-151: u16 status, u32 'wtf' (unknown),
    u32 entry count, then per entry a 12-byte header '<LHHHH' = ptr, length,
    tag, subtag, flags followed by `length` value bytes. Nothing else may
    follow. No reference recorded this reply from a 0078, so every field here
    is UNCERTAIN; the parse never raises, it reports what does not fit.
    values: wtf, entries (list of dicts), by_subtag (subtag -> value bytes).
    """
    d = Decoded(CMD_FACTORY_BITS, len(reply))
    st = _decode_status(d, reply)
    if st != 0:
        return d
    if len(reply) < 10:
        d.warnings.append(f"reply is {len(reply)} byte(s), too short for the 10-byte preamble")
        return d
    wtf, count = u32le(reply, 2), u32le(reply, 6)
    d.add(
        "preamble",
        f"u32 0x{wtf:08x}, entry count {count}",
        UNCERTAIN,
        "[PV] sensor.py:133; the first word's meaning is unknown",
    )
    entries, by_subtag = [], {}
    pos = 10
    for i in range(count):
        if pos + 12 > len(reply):
            d.warnings.append(f"entry {i}: header truncated at offset {pos}")
            break
        ptr, ln, tag, subtag, flags = (
            u32le(reply, pos),
            u16le(reply, pos + 4),
            u16le(reply, pos + 6),
            u16le(reply, pos + 8),
            u16le(reply, pos + 10),
        )
        value = bytes(reply[pos + 12 : pos + 12 + ln])
        if len(value) != ln:
            d.warnings.append(f"entry {i} (subtag {subtag}): truncated, {len(value)} of {ln} bytes")
            break
        entries.append(dict(ptr=ptr, length=ln, tag=tag, subtag=subtag, flags=flags, value=value))
        by_subtag[subtag] = value
        d.add(
            f"entry {i}",
            f"ptr 0x{ptr:x}, {ln} bytes, tag 0x{tag:04x}, subtag {subtag}, flags 0x{flags:04x}: {value[:24].hex()}{'...' if ln > 24 else ''}",
            UNCERTAIN,
            "[PV] sensor.py:138-146",
        )
        pos += 12 + ln
    if pos != len(reply) and len(entries) == count:
        d.warnings.append(
            f"{len(reply) - pos} byte(s) after the last entry ([PV] raises 'Garbage at the end of reply')"
        )
    d.values.update(wtf=wtf, entries=entries, by_subtag=by_subtag)
    return d


def compare_factory_values(by_subtag: dict, stored: bytes) -> dict:
    """Compare the live factory values with the 56 stored ones (pure).

    The live values are subtag 3 minus its first 4 bytes ([PV] sensor.py:260;
    [CAL] section 2). Returns {"match": bool|None, "verdict": str, "lines": [str]}.
    If subtag 3 is absent, or does not fit, every entry is also searched for
    the stored block as a substring, so a layout surprise still gets reported.
    """
    lines = []
    live_full = by_subtag.get(FACTORY_SUBTAG_VALUES)
    if live_full is None:
        lines.append(f"no entry with subtag {FACTORY_SUBTAG_VALUES}; subtags present: {sorted(by_subtag)}")
        for sub, val in sorted(by_subtag.items()):
            at = val.find(stored)
            if at >= 0:
                lines.append(f"the stored block occurs inside subtag {sub} at offset {at}")
        return dict(match=None, verdict="NOT COMPARABLE (no subtag 3)", lines=lines)
    prefix, live = live_full[:FACTORY_PREFIX_LEN], live_full[FACTORY_PREFIX_LEN:]
    lines.append(f"subtag 3: {len(live_full)} bytes = prefix {prefix.hex()} + {len(live)} value bytes")
    lines.append(f"live   : {live.hex()}")
    lines.append(f"stored : {stored.hex()}")
    if len(live) != len(stored):
        at = live_full.find(stored)
        lines.append(
            f"length differs: live {len(live)}, stored {len(stored)}"
            + (f"; the stored block does occur in subtag 3 at offset {at}" if at >= 0 else "")
        )
        return dict(match=False, verdict=f"DIFFER (length {len(live)} vs {len(stored)})", lines=lines)
    diffs = [(i, live[i], stored[i]) for i in range(len(stored)) if live[i] != stored[i]]
    if not diffs:
        lines.append(f"all {len(stored)} values are identical")
        return dict(match=True, verdict=f"MATCH ({len(stored)} of {len(stored)} values identical)", lines=lines)
    for i, a, b in diffs:
        lines.append(f"value {i:2d}: live 0x{a:02x}, stored 0x{b:02x} (live - stored = {a - b:+d})")
    return dict(match=False, verdict=f"DIFFER ({len(diffs)} of {len(stored)} values differ)", lines=lines)


# ------------------------------------------------- EP 0x83 interrupt events

# Event codes in 03-type interrupts ([PV] sensor_2449.py:515-545; iusearch
# main.py:63-91 shows the raw five-byte patterns): 4 image ready, 7 read,
# 6 aborted, 0 failed, 5 keep waiting. 1 and 3 are intermediate (iusearch:
# '03 40 01 00 00' position ok, '03 41 03 00 40' then '03 43 04 00 41').
EVENT_READY = (4, 7)
EVENT_FAILED = (0, 6)
EVENT_EXTEND = 5


@dataclass
class Interrupt:
    raw: bytes
    kind: int  # first byte: 0 start ack, 2 finger detect, 3 capture event
    code: int | None  # u16le at [2:4] for kind 3
    action: str  # "finger", "ready", "fail", "extend" or "ignore"


def classify_interrupt(data: bytes) -> Interrupt:
    """Pure classifier of one EP 0x83 payload (five bytes on this sensor)."""
    data = bytes(data)
    if not data:
        return Interrupt(data, -1, None, "ignore")
    kind = data[0]
    if kind == 2:
        return Interrupt(data, kind, None, "finger")
    if kind == 3 and len(data) >= 4:
        code = u16le(data, 2)
        if code in EVENT_READY:
            return Interrupt(data, kind, code, "ready")
        if code in EVENT_FAILED:
            return Interrupt(data, kind, code, "fail")
        if code == EVENT_EXTEND:
            return Interrupt(data, kind, code, "extend")
        return Interrupt(data, kind, code, "ignore")
    return Interrupt(data, kind, None, "ignore")


# ------------------------------------------------------------ 0x51 image

IMAGE_WIDTH, IMAGE_HEIGHT = 144, 56
# Statuses python-validity treats as "image not there yet, ask again" ([PV] sensor_2449.py:85).
# Kept as the reference set; capture.py retries only IMAGE_NOT_READY (see below).
IMAGE_READ_AGAIN = frozenset({0x0001, 0x0002, 0x0005, 0x0006, 0x0007})

# The 0x51 status words, as far as the references and our own hardware runs show.
# All UNCERTAIN: no reference documents them.
IMAGE_STATUS_NO_FINGER = 0x0001
# Genuine "not ready" statuses: short replies with no image header (observed: 01 00 00 00 00 00 right
# after 02 with nothing on the sensor; iusearch got 02 00 00 00 00 00). 7 is NOT in this set: the one
# time it was seen (capture-20261013-231635-*.log, image 8) the reply carried an image header with a
# non-zero error field, i.e. the capture had finished and failed, and re-asking only produced 0x0412.
IMAGE_NOT_READY = frozenset({0x0001, 0x0002, 0x0005, 0x0006})
IMAGE_STATUS_NO_CAPTURE = 0x0412
IMAGE_STATUS_NOTES = {
    0x0001: "no image available / no finger (bytes 01 00; ToLiKNoSoV treats 'bytes 01 00' as 'No finger detected', "
    "synaptics_0078.c:72-73,266-274; our run 1 got it straight after 02 with nothing on the sensor)",
    0x0002: "not ready (iusearch main.py:100-101 got bytes 02 00 00 00 00 00 and read again)",
    0x0007: "capture finished with an error: the 18-byte reply carried an image header with a non-zero error field "
    "(one observation, capture-20261013-231635-*.log); python-validity lists it as 'read again'",
    0x0412: "no capture in progress (inferred): 51 answered this right after the failed 0x0007 reply, and 04 answers it "
    "after every delivered image and after that failed capture; 04 answers 0 while a capture is still waiting",
}

# Kinds of 51 reply, see classify in parse_image_reply().
KIND_IMAGE, KIND_NOT_READY, KIND_CAPTURE_ERROR, KIND_NO_CAPTURE, KIND_BAD = (
    "image",
    "not_ready",
    "capture_error",
    "no_capture",
    "bad",
)


def describe_capture_error(error: int) -> tuple:
    """(short text, certainty, evidence) for the u32 error field of the 51 reply header.

    [PV] sensor.py:733-734 raises 'Scanning problem: %04x' for any non-zero value and sensor_2449.py
    warns the same; no reference, and no string in the vendor DLL, says what a given value means. Our
    one observation: 0x80000002 (low word 2, bit 31 set) with status 0x0007, after a ready event with
    code 7 (03 60 07 00 40) instead of the usual code 4 (03 42 04 00 40), on a fast touch (~1 s per image).
    (0x80000002 also occurs in the DLL, most likely as the HKEY_LOCAL_MACHINE constant; probably unrelated.)
    """
    if error == 0:
        return "no error", UNCERTAIN, "field is 0"
    low = error & 0xFFFF
    text = f"error {low}" if error & 0xFFFF0000 in (0, 0x80000000) else f"error 0x{error:08x}"
    return (
        text,
        UNCERTAIN,
        f"field 0x{error:08x}: any non-zero value is a 'scanning problem' ([PV] sensor.py:733-734); "
        "the meaning of this code is not documented; seen once, on a fast touch",
    )


@dataclass
class ImageReply:
    status: int | None
    pixels: bytes | None = None
    header: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)
    kind: str = KIND_BAD
    error: int | None = None  # u32 error field of the image header, when a header is present


def parse_image_reply(reply: bytes) -> ImageReply:
    """Decode the reply to 0x51 00 20 00 00 ([PV] sensor_2449.py:575-592).

    u16 status, u32 length, then `length` bytes: u16 x, u16 y, u16 w1, u16 w2,
    u32 error, pixels (8064 for 144 x 56). Total 18 + 8064 = 8082 bytes (iusearch
    reads into an 8146 byte buffer and keeps the last 8064). Never raises.

    `kind` says what the reply is: "image"; "capture_error" (a header with a non-zero error
    field, whatever the status: the capture ended and failed); "not_ready" (status 1, 2, 5, 6
    without a header); "no_capture" (status 0x0412); "bad" (anything else).
    """
    st = status_word(reply)
    out = ImageReply(status=st)
    if st is None:
        out.problems.append(f"reply is {len(reply)} byte(s), too short for a status word")
        return out
    # Header: u32 length at 2, then at least the 12-byte header; present for status 0 and also for 0x0007.
    body = reply[6:]
    has_header = len(reply) >= 18 and u32le(reply, 2) == len(body) and len(body) >= 12
    if has_header:
        x, y, w1, w2, error = u16le(body, 0), u16le(body, 2), u16le(body, 4), u16le(body, 6), u32le(body, 8)
        out.header = dict(x=x, y=y, w1=w1, w2=w2, error=error)
        out.error = error
    if st != 0:
        out.problems.append(f"non-zero status 0x{st:04x}")
        if has_header and out.error:
            out.kind = KIND_CAPTURE_ERROR
            out.problems.append(f"image error field 0x{out.error:x}")
        elif st == IMAGE_STATUS_NO_CAPTURE:
            out.kind = KIND_NO_CAPTURE
        elif st in IMAGE_NOT_READY and not has_header:
            out.kind = KIND_NOT_READY
        return out
    if len(reply) < 18:
        out.problems.append(f"reply too short for an image: {len(reply)} bytes")
        return out
    if not has_header:
        out.problems.append(f"length field {u32le(reply, 2)} does not match the {len(body)} bytes received")
        return out
    pixels = bytes(body[12:])
    if out.error != 0:
        out.problems.append(f"image error field 0x{out.error:x}")
        out.kind = KIND_CAPTURE_ERROR
    if len(pixels) != out.header["x"] * out.header["y"]:
        out.problems.append(f"{len(pixels)} pixel bytes but header says {out.header['x']} x {out.header['y']}")
    elif len(pixels) != IMAGE_WIDTH * IMAGE_HEIGHT:
        out.problems.append(f"{len(pixels)} pixel bytes, expected {IMAGE_WIDTH * IMAGE_HEIGHT} (144 x 56)")
    if not out.problems:
        out.pixels = pixels
        out.kind = KIND_IMAGE
    return out


# ------------------------------------------------- image statistics (host side)

# Heuristic, UNCERTAIN: coverage = share of 8 x 8 blocks whose standard deviation exceeds
# 15 grey levels. Measured on the first real capture (capture-20261013-225024-*.pgm): blocks
# under the finger have sd 30-70, the stored blank baseline frame has sd < 5 in every block
# (max 4.6), partial blocks at the finger edge 5-13. It estimates how much of the 144 x 56
# window shows ridge texture; it says nothing about image quality for matching.
COVERAGE_BLOCK = 8
COVERAGE_SD = 15.0
# Two takes whose mean absolute pixel difference is below this look like the same frame
# (a finger that was not lifted). UNCERTAIN heuristic: independent touches differ by tens.
DUPLICATE_MEAN_ABS_DIFF = 3.0


def _mean_sd(values) -> tuple:
    n = len(values)
    mean = sum(values) / n
    return mean, (sum((v - mean) ** 2 for v in values) / n) ** 0.5


def image_quality(pixels: bytes, width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> dict:
    """min, max, mean, sd and the ridge-texture coverage estimate (0..1) of one image. Pure."""
    if len(pixels) != width * height:
        raise ValueError(f"{len(pixels)} pixels do not fill {width} x {height}")
    mean, sd = _mean_sd(pixels)
    covered = total = 0
    b = COVERAGE_BLOCK
    for by in range(height // b):
        for bx in range(width // b):
            block = [pixels[(by * b + y) * width + bx * b + x] for y in range(b) for x in range(b)]
            total += 1
            if _mean_sd(block)[1] > COVERAGE_SD:
                covered += 1
    return dict(
        min=min(pixels), max=max(pixels), mean=round(mean, 1), sd=round(sd, 2), coverage=round(covered / total, 3)
    )


def format_quality(q: dict) -> str:
    return f"min {q['min']}, max {q['max']}, mean {q['mean']:.1f}, sd {q['sd']:.1f}, ridge coverage ~{q['coverage'] * 100:.0f}%"


def mean_abs_diff(a: bytes, b: bytes) -> float:
    if len(a) != len(b) or not a:
        raise ValueError("images differ in size")
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


# ----------------------------------------------------- 04 (stop capture) status


def classify_stop_status(status: int | None, image_done: bool = False) -> tuple:
    """(label, certainty, evidence) for the status word of the cleanup command 04 (and of a 51 reply).

    Evidence from our own hardware runs (proto/logs/capture-*.log): 04 answered 0x0000 in the three
    runs where the capture program was still waiting (--no-finger twice: status 1; finger timeout)
    and 0x0412 after every delivered image (the many collection runs) and after the one failed capture
    (23:16:45, image 8: 51 had returned the 0x0007 error reply, a re-sent 51 returned 0x0412 and so
    did 04). So 0x0412 is read as "no capture in progress": nothing left to stop. The references
    never look at it (python-validity sensor.py:726 and iusearch main.py:132 discard the reply) and the
    ETW only shows a 2-byte reply. An inference from those observations. `image_done` is accepted for
    compatibility and no longer changes the answer.
    """
    if status is None:
        return "no status word", UNCERTAIN, "reply shorter than 2 bytes"
    if status == 0:
        return "capture stopped", UNCERTAIN, "status 0 in the three runs where the capture was still waiting"
    if status == IMAGE_STATUS_NO_CAPTURE:
        return (
            "no capture in progress (inferred)",
            UNCERTAIN,
            "0x0412 after every delivered image and after one failed capture (error reply 0x0007); 0x0000 while waiting",
        )
    return "unclassified non-zero status", UNCERTAIN, "no evidence for this code"


def pgm_bytes(pixels: bytes, width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> bytes:
    """Binary PGM (P5), 8 bit."""
    if len(pixels) != width * height:
        raise ValueError(f"{len(pixels)} pixels do not fill {width} x {height}")
    return f"P5\n{width} {height}\n255\n".encode("ascii") + bytes(pixels)


PARSERS = {
    CMD_ROM_INFO: parse_rom_info,
    CMD_QUERY_19: parse_query_19,
    CMD_SENSOR_ID: parse_sensor_id,
    CMD_FACTORY_BITS: parse_factory_bits,
}
