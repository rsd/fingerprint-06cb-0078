# SPDX-License-Identifier: MIT
"""Audit: nothing public holds this unit's private data (research/steps/a7-b4-design.md, decision 11).

What is public here:
- every file of this project that git tracks or has staged (tools/test_no_serial.py's
  git_paths), read from disk as that guard reads it (disk_bytes), and every staged blob
  that differs from its disk copy (staged_blobs, labelled "path (staged)");
- every file git tracks under tests/synaptics0078/ in the libfprint fork
  (LIBFPRINT_FORK, default ../libfprint; read from disk, never written): the
  synthesized replay fixture custom.pcapng, its generator and test scripts, the
  umockdev device file and the stand-in data/module.bin and data/program-*.bin.

What is checked:
- no public file shares a 16-byte window (stride 1) with a private source. Each group
  of sources is its own test, per side (project, fork), and runs only with its files
  present locally; nothing root-only is read (never /var/lib/fprint):
  * module: the capture module, from local/synaptics0078/module.bin (the dev data dir,
    tools/synaptics0078_data.py) and from the vendor DLL (local/b5/x-bsdtar-2019/
    synaWudfBioUsb.dll, found as tools/test_package_audit.py finds it);
  * dll-tables: the eight pinned DLL tables the template takes, from that DLL;
  * template: the real template, proto/data/cmd02_template.bin;
  * program: this unit's program, local/synaptics0078/program-*.bin,
    research/calib/ours_cmd02.bin, proto/testdata/ours_cmd02.bin and the
    program-linux.bin of each calibration run (local/calib/<stamp>/);
  * calibration: extracted/CalibrationData_decrypted.bin;
  * calib-run: every other binary file directly in a calibration run dir
    (local/calib/<stamp>/: bf.bin, cf.bin, ep82-*.bin, chunk-*.bin, reply-*.bin, the
    programs sent); a file is binary when its name ends in .bin or it is not text (not
    UTF-8, or a control byte other than tab, LF and CR), so the run's calibrate.log and
    result.json (reports) are left out;
  * reply-6f: this unit's 6f (factory bits) reply, local/b2/ours_6f_reply.bin and the
    890-byte reply in the probe log (PROBE_LOG; the line proto/test_calib_model.py's
    our_6f_reply reads);
  * captures: the image files (CAPTURE_SUFFIXES) under proto/captures/ and
    local/captures/; images-b3-me, images-diag and images-diag-fprintd: the finger
    images under local/b3/me/, every local/diag*/ but local/diag-fprintd/, and
    local/diag-fprintd/. Images are read as bytes only, never decoded, shown or rendered.
  One group's sources and index are held at a time (the images are split by folder).
  The private side is indexed and the public side streamed past it. A cheap prefilter
  keeps the index small: every public window of both sides (the project's and, when
  found, the fork's) sets one bit of a FILTER_BITS-bit map by its hash, built once, and
  a private window is indexed only when its bit is set. A window both sides share always
  has its bit set, so the prefilter drops no hit; it only drops private windows that no
  public file holds. As in the package audit, windows with fewer than MIN_DISTINCT
  distinct byte values (zero padding, fill runs) are not indexed; a group whose present
  files yield no counted window at all fails, as nothing of it was audited. Nor is a
  window lying wholly inside a fixed format header (FORMAT_HEADERS) indexed, as the
  header says nothing of this unit and the tracked notes and driver quote its form:
  the first 0x20 bytes of CalibrationData (record length, struct size, version, sensor
  type, the RomInfo version fields; the serial follows); the first 0x12 bytes of a run's
  RomInfo reply (reply-*-01.bin: status, the firmware's timestamp and build, version,
  product and the module flag; the serial follows); the 18-byte 51 reply header that
  starts every raw capture; and the text header of a PGM image, parsed per file
  (pgm_header_len: "P5", width, height, maxval, each after whitespace and "#" comment
  lines, then the single whitespace byte; the driver's diag
  images carry a comment of its sequence number, clock and the 51 header fields, in
  the format its tracked source prints). A window reaching past a header, into the
  serial or the pixels, is indexed. Every run of 16 or more hex byte pairs in a public
  file ("0a1b...", "0x0a, 0x1b", "\\x0a\\x1b", the package audit's HEX_RUN) is decoded
  and scanned too, named by the run's offset in the file. A hit names
  public-path@offset=source+offset, never the bytes; a program-*.bin of the data dir is
  named by its index, as its file name holds the unit's serial, and every other private
  file name is shown with any serial form in it masked (test_no_serial.mask);
- no fork fixture file, by name or content, holds this unit's serial in any form
  tools/test_no_serial.py detects (that guard covers this project's own files); skipped
  without a local serial source;
- the fixture's protocol, with its own pcapng reader (stdlib struct, independent of the
  fork's generate.py, so that two models of the format agree): linktype 220
  (USB_LINUX_MMAPPED), the 64-byte usbmon header. The only traffic is bulk OUT on 0x01,
  bulk IN on 0x81 and interrupt IN on 0x83, as the driver uses (no control transfer:
  umockdev answers the descriptors from the device file, outside the pcap; no interrupt
  OUT, no isochronous). Every bulk OUT submit is one of the commands the driver sends
  (COMMANDS); each bulk IN completion is paired with the newest bulk OUT command not yet
  answered, and every reply is checked with synaptics0078.parsers: 01, 19 and 75 parse
  with status 0, no warning, the expected length, sensor type EXPECTED_SENSOR_TYPE and
  the synthetic serial of synaptics0078/tests/fixtures.py; 06, 02 and 52 have
  status_word 0; 04's status is one classify_stop_status knows (0, capture stopped, or
  0x0412, no capture in progress); every 51 reply is a kind parse_image_reply knows (not
  "bad") and every image is 144 x 56. Every EP 0x83 completion with data classifies
  (classify_interrupt) to finger, ready, fail or extend, or to one of the events the
  parsers document and ignore (KNOWN_IGNORED: kind 0 start ack, kind 3 codes 1 and 3);
  any other is unknown and fails. An IN completion with no command left to answer, a
  command never answered, a failed completion and an OUT submit that never completes all
  fail the test; an IN submit with no completion is the fixture's timeout or cancel and
  is allowed.

Not covered: files that are neither tracked nor staged (the clean-clone gate commits
them first); other encodings of private bytes in text (base64, decimal lists, multi-byte
integers); compressed public data (the fixture holds none); third-party payloads under
proto/testdata/ (other units' programs) and the rest of the vendor DLL (the package
audit compares the package with the whole DLL); private text (the hex dumps in the
logs under proto/logs/ and local/logs/ other than the probe log's 6f reply, a run's
calibrate.log and result.json, the images' .json metadata) and the private local
folders this audit does not name (local/far-diag/, local/fprintd-prints/, ...). The
window check is exact: a private source re-encoded or shifted by bits is not found.

Where a test skips: a window group when none of its files is present (the reason names
them); the fork's side, the fork's serial and the protocol check when no fork checkout is
found, or it tracks no tests/synaptics0078/custom.pcapng (the reason names LIBFPRINT_FORK);
the serial check when there is no local calibration data or RomInfo log to derive the
serial from.

An error inside a private-data step is reported as the step and the error type only,
with no traceback (as in tools/test_no_serial.py), so `pytest -l` cannot show a local
holding private bytes; Private's repr shows only names and sizes.
"""

from __future__ import annotations

import fnmatch
import functools
import os
import re
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest
import test_no_serial as tns
import test_package_audit as tpa

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from synaptics0078 import calibration, parsers
from synaptics0078.tests import fixtures as fx

FORK_ENV = tpa.FORK_ENV
FORK_TESTS = "tests/synaptics0078"
PCAPNG = f"{FORK_TESTS}/custom.pcapng"
WINDOW = tpa.WINDOW
MIN_DISTINCT = tpa.MIN_DISTINCT
HIT_LIMIT = 8  # hits shown per public file; the rest are counted
FILTER_BITS = 1 << 28  # the public-window prefilter: 32 MB, a few percent of its bits set

DLL = tpa.DLL
LOCAL = REPO / "local"
LOCAL_DATA = LOCAL / "synaptics0078"
MODULE_FILE = LOCAL_DATA / "module.bin"
TEMPLATE_FILE = REPO / "proto" / "data" / "cmd02_template.bin"
PROGRAM_FILES = (REPO / "research" / "calib" / "ours_cmd02.bin", REPO / "proto" / "testdata" / "ours_cmd02.bin")
CALIBRATION = tns.CALIBRATION
CALIB_RUNS = LOCAL / "calib"  # one dir per calibration run, local/calib/<stamp>/
RUN_PROGRAM = "program-linux.bin"  # a run's per-unit program: in the program group
SAVED_6F = LOCAL / "b2" / "ours_6f_reply.bin"
PROBE_LOG = REPO / "proto" / "logs" / "probe-20261013-204946-724232-p613500.log"
REPLY_6F_LEN = 890
CAPTURE_DIRS = (REPO / "proto" / "captures", LOCAL / "captures")
CAPTURE_SUFFIXES = (".pgm", ".raw", ".png")
B3_IMAGES = LOCAL / "b3" / "me"
DIAG_FPRINTD = LOCAL / "diag-fprintd"
DIAG_GLOB = "diag*"  # local/diag*/, local/diag-fprintd/ excepted (its own group)
IMAGE_HEADER_LEN = 18  # a 51 reply: u16 status, u32 length, x, y, w1, w2, u32 error (parse_image_reply)

# ------------------------------------------------------------------ format headers

PGM_FIELD = re.compile(rb"(?:[ \t\r\n\v\f]|#[^\r\n]*[\r\n])+(\d+)")  # whitespace and comments, then a number
PGM_SPACE = frozenset(b" \t\r\n\v\f")


def pgm_header_len(data: bytes) -> int:
    """Length of a binary PGM's text header ("P5", width, height, maxval, one whitespace byte), 0 if none parses.

    0 too unless exactly width * height pixels (two bytes each when maxval > 255) follow the header, so
    a malformed header (a comment running into the pixels) can never make pixel bytes "header".
    """
    if not data.startswith(b"P5"):
        return 0
    pos = 2
    fields = []
    for _ in range(3):  # width, height, maxval
        m = PGM_FIELD.match(data, pos)
        if m is None:
            return 0
        fields.append(int(m.group(1)))
        pos = m.end()
    if pos >= len(data) or data[pos] not in PGM_SPACE:
        return 0
    width, height, maxval = fields
    if len(data) - (pos + 1) != width * height * (1 if maxval < 256 else 2):
        return 0
    return pos + 1


IMAGE_FORMATS = {"*.raw": IMAGE_HEADER_LEN, "*.pgm": pgm_header_len}
IMAGE_GROUPS = ("captures", "images-b3-me", "images-diag", "images-diag-fprintd")
# Fixed format headers, (group, file name pattern) -> length, or a function of the file's
# bytes giving it: the same on every unit and every capture, its form quoted in the
# tracked notes and code, so a window wholly inside one says nothing private.
FORMAT_HEADERS: dict[tuple[str, str], int | Callable[[bytes], int]] = {
    # CalibrationData: u32 record length, u32 0xe4, u32 version 2, u32 sensor type 0x2449,
    # then the RomInfo version fields (research/calibration-analysis.md), up to the serial.
    ("calibration", "*.bin"): calibration.SERIAL_OFFSET,
    # A run's RomInfo reply: u16 status, the firmware's u32 timestamp and u32 build, major,
    # minor, product and the module flag (parsers.parse_rom_info), up to the serial at 0x12.
    ("calib-run", "reply-*-01.bin"): tns.ROM_SERIAL_OFFSET,
    # A raw capture is a 51 reply, its 18-byte header before the pixels; a PGM's text header.
    **{(group, pattern): n for group in IMAGE_GROUPS for pattern, n in IMAGE_FORMATS.items()},
}

# The commands the driver sends (libfprint/drivers/synaptics0078.c): RomInfo, query 19,
# sensor id, module upload, program, image, the two stop commands.
COMMANDS = frozenset({0x01, 0x19, 0x75, 0x06, 0x02, 0x51, 0x04, 0x52})
REPLY_PARSERS = {
    parsers.CMD_ROM_INFO: parsers.parse_rom_info,
    parsers.CMD_QUERY_19: parsers.parse_query_19,
    parsers.CMD_SENSOR_ID: parsers.parse_sensor_id,
}
CMD_IMAGE = 0x51
CMD_STOP = 0x04
STATUS_ONLY = frozenset({0x06, 0x02, 0x52})  # module upload, program, stop: status_word 0
STOP_KNOWN = frozenset(parsers.classify_stop_status(s)[0] for s in (0, parsers.IMAGE_STATUS_NO_CAPTURE))
INTERRUPT_EVENTS = frozenset({"finger", "ready", "fail", "extend"})
# (kind, code) of the events classify_interrupt ignores on purpose: kind 0 the start ack
# (parsers.Interrupt), kind 3 codes 1 and 3 the intermediate capture events (the comment
# above parsers.EVENT_READY). Any other ignored event is unknown.
KNOWN_IGNORED = frozenset({(0, None), (3, 1), (3, 3)})

LINKTYPE_USB_LINUX_MMAPPED = 220
USBMON_LEN = 64
XFER_INTERRUPT, XFER_CONTROL, XFER_BULK = 1, 2, 3
DIR_IN = 0x80
EP_OUT, EP_IN, EP_INT = 0x01, 0x81, 0x83
# (transfer type, endpoint) of all the driver's traffic: commands, replies, events.
TRAFFIC = frozenset({(XFER_BULK, EP_OUT), (XFER_BULK, EP_IN), (XFER_INTERRUPT, EP_INT)})
BLOCK_SHB, BLOCK_IDB, BLOCK_EPB = 0x0A0D0D0A, 0x00000001, 0x00000006
BYTE_ORDER_MAGIC = 0x1A2B3C4D


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


# ------------------------------------------------------------------ private sources


class Private:
    """A group's private sources ({name: bytes}), with a repr that shows none of their bytes (pytest -l)."""

    def __init__(self, group: str, sources: dict[str, bytes]):
        self.group = group
        self.sources = sources

    def __repr__(self) -> str:
        sizes = ", ".join(f"{name} {len(data)} bytes" for name, data in self.sources.items())
        return f"<private {self.group}: {sizes}>"


@functools.lru_cache(maxsize=1)
def _serial_patterns() -> tuple:
    """The serial forms of this machine's serial (none in a clean clone), to mask private file names."""
    return tuple(tns.serial_forms(tns.local_serials()))


def _label(path: Path) -> str:
    """A private file's name for a report: repo-relative, any serial form in it masked."""
    return tns.mask(_rel(path), _serial_patterns())


@functools.lru_cache(maxsize=1)
def _dll_sources() -> dict[str, bytes]:
    """The module and the pinned tables, found in the vendor DLL (tpa.vendor_sources)."""
    return tpa.vendor_sources(DLL.read_bytes())


def _module() -> dict[str, bytes]:
    out = {}
    if MODULE_FILE.is_file():
        out[_rel(MODULE_FILE)] = MODULE_FILE.read_bytes()
    if DLL.is_file():
        out["dll module"] = _dll_sources()["module"]
    return out


def _dll_tables() -> dict[str, bytes]:
    if not DLL.is_file():
        return {}
    return {f"dll table {name}": data for name, data in _dll_sources().items() if name != "module"}


def _template() -> dict[str, bytes]:
    return {_rel(TEMPLATE_FILE): TEMPLATE_FILE.read_bytes()} if TEMPLATE_FILE.is_file() else {}


def run_dirs(runs: Path = CALIB_RUNS) -> list[Path]:
    """The calibration run dirs, local/calib/<stamp>/."""
    return sorted(d for d in runs.iterdir() if d.is_dir()) if runs.is_dir() else []


def _program() -> dict[str, bytes]:
    out = {}
    # The local file names hold the unit's serial: each is named by its index only.
    for k, path in enumerate(sorted(LOCAL_DATA.glob("program-*.bin")) if LOCAL_DATA.is_dir() else ()):
        if path.is_file():
            out[f"{_rel(LOCAL_DATA)}/program #{k + 1}"] = path.read_bytes()
    for path in [*PROGRAM_FILES, *(d / RUN_PROGRAM for d in run_dirs())]:
        if path.is_file():
            out[_label(path)] = path.read_bytes()
    return out


def _calibration() -> dict[str, bytes]:
    return {_rel(CALIBRATION): CALIBRATION.read_bytes()} if CALIBRATION.is_file() else {}


TEXT_CONTROLS = frozenset(b"\t\n\r")


def is_binary(path: Path, data: bytes) -> bool:
    """A .bin file, or one that is not text: not UTF-8, or holding a control byte other than tab, LF and CR."""
    if path.suffix.lower() == ".bin":
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return any(b < 0x20 and b not in TEXT_CONTROLS for b in data) or 0x7F in data


def run_files(runs: Path = CALIB_RUNS) -> dict[Path, bytes]:
    """{path: bytes} of every binary regular file directly in a run dir, the run's program excepted."""
    out = {}
    for d in run_dirs(runs):
        for path in sorted(d.iterdir()):
            if path.name == RUN_PROGRAM or path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            if is_binary(path, data):
                out[path] = data
    return out


def _calib_run() -> dict[str, bytes]:
    return {_label(path): data for path, data in run_files().items()}


def log_6f_reply(path: Path) -> bytes | None:
    """The 890-byte 6f reply a probe log records (its RX line on EP 0x81), or None."""
    if not path.is_file():
        return None
    for line in path.read_bytes().decode("utf-8", "replace").splitlines():
        if " RX " in f" {line} " and f"EP 0x81 {REPLY_6F_LEN}" in line:
            return bytes.fromhex(line.split()[-1])
    return None


def _reply_6f() -> dict[str, bytes]:
    out = {}
    if SAVED_6F.is_file():
        out[_rel(SAVED_6F)] = SAVED_6F.read_bytes()
    logged = log_6f_reply(PROBE_LOG)
    if logged is not None:
        out[f"{_rel(PROBE_LOG)} 6f reply"] = logged
    return out


def image_files(dirs) -> list[Path]:
    """The image files (CAPTURE_SUFFIXES) under the dirs, recursively."""
    files = []
    for d in dirs:
        if d.is_dir():
            files += [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in CAPTURE_SUFFIXES]
    return sorted(files)


def diag_dirs(local: Path = LOCAL) -> list[Path]:
    """local/diag*/ but local/diag-fprintd/, which is a group of its own."""
    if not local.is_dir():
        return []
    return sorted(d for d in local.glob(DIAG_GLOB) if d.is_dir() and d.name != DIAG_FPRINTD.name)


def _images(dirs_of) -> Callable[[], dict[str, bytes]]:
    def load() -> dict[str, bytes]:
        return {_label(p): p.read_bytes() for p in image_files(dirs_of())}  # bytes only, never decoded

    return load


def _no_images(what: str) -> str:
    return "no " + "/".join(CAPTURE_SUFFIXES) + f" file under {what}"


# group -> (loader, what is missing when the loader finds nothing)
GROUPS = {
    "module": (_module, f"no capture module: neither {_rel(MODULE_FILE)} nor {_rel(DLL)}"),
    "dll-tables": (_dll_tables, f"no pinned DLL tables: no {_rel(DLL)}"),
    "template": (_template, f"no real template: no {_rel(TEMPLATE_FILE)}"),
    "program": (
        _program,
        "no per-unit program: no "
        + ", ".join(
            [f"{_rel(LOCAL_DATA)}/program-*.bin", *map(_rel, PROGRAM_FILES), f"{_rel(CALIB_RUNS)}/*/{RUN_PROGRAM}"]
        ),
    ),
    "calibration": (_calibration, f"no calibration data: no {_rel(CALIBRATION)}"),
    "calib-run": (_calib_run, f"no calibration run: no binary file in {_rel(CALIB_RUNS)}/*/ (besides {RUN_PROGRAM})"),
    "reply-6f": (
        _reply_6f,
        f"no 6f reply: neither {_rel(SAVED_6F)} nor the {REPLY_6F_LEN}-byte 6f reply of {_rel(PROBE_LOG)}",
    ),
    "captures": (_images(lambda: CAPTURE_DIRS), "no capture images: " + _no_images(", ".join(map(_rel, CAPTURE_DIRS)))),
    "images-b3-me": (_images(lambda: (B3_IMAGES,)), "no finger images: " + _no_images(_rel(B3_IMAGES))),
    "images-diag": (
        _images(diag_dirs),
        "no diag images: " + _no_images(f"{_rel(LOCAL)}/{DIAG_GLOB}/ (other than {_rel(DIAG_FPRINTD)})"),
    ),
    "images-diag-fprintd": (
        _images(lambda: (DIAG_FPRINTD,)),
        "no fprintd diag images: " + _no_images(_rel(DIAG_FPRINTD)),
    ),
}
assert set(IMAGE_GROUPS) <= set(GROUPS)


def load_group(group: str) -> Private | None:
    loader, _ = GROUPS[group]
    sources = loader()
    return Private(group, sources) if sources else None


def format_header_len(group: str, name: str, data: bytes = b"") -> int:
    """Length of the source's fixed format header (FORMAT_HEADERS, by its file name), 0 when it has none."""
    base = PurePosixPath(name).name.lower()
    for (g, pattern), n in FORMAT_HEADERS.items():
        if g == group and fnmatch.fnmatchcase(base, pattern):
            return n(data) if callable(n) else n
    return 0


def _bit(h: int) -> tuple[int, int]:
    h &= FILTER_BITS - 1
    return h >> 3, 1 << (h & 7)


def window_filter(items) -> bytearray:
    """The prefilter: one bit, by hash, for every 16-byte window of the items (stride 1)."""
    bits = bytearray(FILTER_BITS >> 3)
    for _, data in items:
        for i in range(len(data) - WINDOW + 1):
            byte, mask = _bit(hash(data[i : i + WINDOW]))
            bits[byte] |= mask
    return bits


def private_index(private: Private, bits: bytearray | None = None) -> tuple[dict[bytes, str], int]:
    """({window: "source+offset"}, number of counted windows) for the sources, stride 1.

    As tpa.window_index, except that a window lying wholly inside a source's fixed format
    header (format_header_len) is not counted; a window reaching past the header is. With
    the prefilter bits (window_filter), a counted window whose bit is clear is no public
    window and is left out of the index; the count includes it.
    """
    index, counted = {}, 0
    for name, data in private.sources.items():
        first = max(0, format_header_len(private.group, name, data) - WINDOW + 1)
        for i in range(first, len(data) - WINDOW + 1):
            w = data[i : i + WINDOW]
            if not tpa._counted(w):
                continue
            counted += 1
            if bits is not None:
                byte, mask = _bit(hash(w))
                if not bits[byte] & mask:
                    continue
            index.setdefault(w, f"{name}+{i}")
    return index, counted


_HELD: dict[str, tuple[Private | None, dict[bytes, str], int]] = {}  # one group's sources and index at a time


def group_index(group: str, step: list[str]) -> tuple[Private | None, dict[bytes, str], int]:
    """(the group's sources, their window index, their counted windows); the tests run group by group."""
    if group not in _HELD:
        _HELD.clear()
        step[0] = f"loading the {group} sources"
        private = load_group(group)
        index, counted = {}, 0
        if private is not None:
            step[0] = "reading the public files"
            bits = public_filter()
            step[0] = f"indexing the {group} sources"
            index, counted = private_index(private, bits)
        _HELD[group] = (private, index, counted)
    return _HELD[group]


# ------------------------------------------------------------------ public files


def hex_runs(data: bytes) -> list[tuple[int, bytes]]:
    """(offset in data, decoded bytes) of every run of 16 or more hex byte pairs (tpa.decoded_hex, with offsets)."""
    return [
        (m.start(), bytes.fromhex(b"".join(tpa.HEX_PAIR.findall(m.group())).decode()))
        for m in tpa.HEX_RUN.finditer(data)
    ]


def _with_hex_runs(label: str, data: bytes) -> list[tuple[str, bytes]]:
    return [(label, data), *((f"{label}[hex run at {at:#x}]", run) for at, run in hex_runs(data))]


def public_items(root: Path, paths) -> list[tuple[str, bytes]]:
    """(label, bytes) for each path's disk content and each hex run decoded from it ("path[hex run at 0x..]")."""
    items = []
    for rel in sorted(set(paths)):
        data = tns.disk_bytes(root, rel)
        if data is not None:
            items += _with_hex_runs(rel, data)
    return items


def staged_items(root: Path, env=None) -> list[tuple[str, bytes]]:
    """(label, bytes) for each staged blob that differs from its disk copy ("path (staged)"), and its hex runs."""
    items = []
    for rel, blob in tns.staged_blobs(root, tns.staged_paths(root, env), env):
        if tns.disk_bytes(root, rel) != blob:
            items += _with_hex_runs(f"{rel} (staged)", blob)
    return items


def fork_dir() -> Path:
    return tpa.fork_dir()


def fork_git(fork: Path, *args: str) -> bytes:
    """Read-only git in the fork, without the caller's GIT_* (a hook's GIT_INDEX_FILE names this project's index)."""
    env = {**tns.git_env(), "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(["git", "-C", str(fork), *args], capture_output=True, check=True, env=env).stdout


def fork_paths(fork: Path) -> list[str]:
    """The paths git tracks (or has staged) under tests/synaptics0078/ in the fork."""
    out = fork_git(fork, "ls-files", "-z", "--", FORK_TESTS)
    return sorted(p for p in os.fsdecode(out).split("\0") if p)


@functools.lru_cache(maxsize=1)
def _fork_state() -> tuple[Path, str | None]:
    """(the fork dir, why it cannot be audited or None)."""
    fork = fork_dir()
    if not (fork / ".git").exists():
        return fork, f"no libfprint fork checkout at {fork} (set {FORK_ENV})"
    if PCAPNG not in fork_paths(fork):
        return fork, f"the fork at {fork} tracks no {PCAPNG} (set {FORK_ENV} to a fork with the replay test)"
    return fork, None


def _require_fork() -> Path:
    fork, reason = _fork_state()
    if reason is not None:
        pytest.skip(reason)
    return fork


@pytest.fixture(scope="module")
def fork() -> Path:
    return _require_fork()


@functools.lru_cache(maxsize=2)
def _side_items(side: str) -> tuple[tuple[str, bytes], ...]:
    if side == "project":
        return tuple(public_items(REPO, tns.git_paths(REPO)) + staged_items(REPO))
    fork = _require_fork()
    return tuple((f"fork:{label}", data) for label, data in public_items(fork, fork_paths(fork)))


def _sides() -> list[str]:
    return ["project"] if _fork_state()[1] is not None else ["project", "fork"]


@functools.lru_cache(maxsize=1)
def public_filter() -> bytearray:
    """The prefilter of every public window, of this project and of the fork when found (built once)."""
    return window_filter(item for side in _sides() for item in _side_items(side))


# ------------------------------------------------------------------ windows


def window_hits(index: dict[bytes, str], items) -> dict[str, list[str]]:
    """{public label: ["@offset=source+offset", ...]} for every indexed window in the items; offsets, never bytes."""
    hits = {}
    if not index:
        return hits
    get = index.get
    for label, data in items:
        for i in range(len(data) - WINDOW + 1):
            src = get(data[i : i + WINDOW])
            if src is not None:
                hits.setdefault(label, []).append(f"@{i:#x}={src}")
    return hits


def format_hits(hits: dict[str, list[str]], limit: int = HIT_LIMIT) -> str:
    """One public-label@offset=source+offset line per hit, `limit` per label, then a count of the rest."""
    lines = []
    for label in sorted(hits):
        found = hits[label]
        lines += [label + h for h in found[:limit]]
        if len(found) > limit:
            lines.append(f"{label}: {len(found) - limit} more window(s)")
    return "\n".join(lines)


def window_report(group: str, side: str, step: list[str]) -> tuple[str | None, str]:
    """(skip reason or None, failure text or ""); every private step runs here, not in the test's frame."""
    private, index, counted = group_index(group, step)
    if private is None:
        return GROUPS[group][1], ""
    if not counted:
        return None, f"the {len(private.sources)} present {group} source(s) yield no counted window: nothing audited"
    step[0] = f"reading the {side} files"
    items = _side_items(side)
    step[0] = f"scanning the {side} files"
    hits = window_hits(index, items)
    if not hits:
        return None, ""
    return None, f"public files share 16-byte windows with private {group} data (public@offset=source+offset):\n" + (
        format_hits(hits)
    )


@pytest.mark.parametrize(("group", "side"), [(g, s) for g in GROUPS for s in ("project", "fork")])
def test_no_public_file_shares_a_window_with_private_data(group, side):
    if side == "fork":
        _require_fork()  # skips with the reason, before any private data is read
    step = ["starting"]
    error = None
    try:
        skip, report = window_report(group, side, step)
    except Exception as e:  # noqa: BLE001 - its message or frames could carry private bytes
        error = f"{step[0]}: {type(e).__name__}"
    if error is not None:
        pytest.fail(error, pytrace=False)
    if skip is not None:
        pytest.skip(skip)
    if report:
        pytest.fail(report, pytrace=False)


# ------------------------------------------------------------------ serial, in the fork


def fork_serial_report(step: list[str], fork: Path, serials=None) -> str | None:
    """None when there is no serial to look for, "" when clean, else path:line per hit (masked)."""
    step[0] = "deriving the serial"
    patterns = tns.serial_forms(tns.local_serials() if serials is None else serials)
    if not patterns:
        return None
    step[0] = "listing the fork's fixture files"
    paths = fork_paths(fork)
    step[0] = "scanning the fork's fixture files"
    disk = tns.scan_paths(fork, paths, patterns)
    names = tns.name_hits(paths, patterns)
    if not (disk or names):
        return ""
    return f"in the fork ({fork}): " + tns.failure_report(patterns, disk, None, names)


def test_the_project_side_holds_the_expected_files():
    project = {label for label, _ in _side_items("project")}
    assert {"README.md", "tools/test_no_serial.py", "synaptics0078/parsers.py"} <= project


def test_the_fork_side_holds_the_expected_files(fork):
    labels = {label for label, _ in _side_items("fork")}
    assert {f"fork:{PCAPNG}", f"fork:{FORK_TESTS}/device", f"fork:{FORK_TESTS}/data/module.bin"} <= labels
    assert any(label.startswith("fork:") and "[hex run at " in label for label in labels)  # generate.py's hex


def test_fork_serial_scan_sees_the_fixtures_synthetic_serial(fork):
    # The fixture spells fixtures.SERIAL (nibble-swapped, the driver's form) in the device
    # file and a data file name: the scan reaches those files and masks the name.
    report = fork_serial_report(["x"], fork, [fx.SERIAL])
    assert f"{FORK_TESTS}/data/program-<serial>.bin: path name" in report.splitlines()
    assert any(line.startswith(f"{FORK_TESTS}/device:") for line in report.splitlines())
    assert fx.SERIAL.hex() not in report and tns.nibble_swap(fx.SERIAL).hex() not in report


def test_no_fork_fixture_file_holds_the_unit_serial(fork):
    step = ["starting"]
    error = None
    try:
        report = fork_serial_report(step, fork)
    except Exception as e:  # noqa: BLE001 - its message or frames could carry the serial
        error = f"{step[0]}: {type(e).__name__}"
    if error is not None:
        pytest.fail(error, pytrace=False)
    if report is None:
        pytest.skip("no local calibration data or RomInfo log to derive the serial from")
    if report:
        pytest.fail(report, pytrace=False)


# ------------------------------------------------------------------ the fixture's protocol


def pcapng_packets(data: bytes) -> list[tuple[int, bytes]]:
    """(linktype, packet bytes) of every Enhanced Packet Block, in file order.

    Section Header (byte order from its magic), Interface Description (linktype per
    interface, per section) and Enhanced Packet blocks are read; other blocks are skipped.
    ValueError on a malformed block.
    """
    packets = []
    pos, order, linktypes = 0, None, []
    while pos < len(data):
        if len(data) - pos < 12:
            raise ValueError(f"truncated block at {pos:#x}")
        if data[pos : pos + 4] == struct.pack("<I", BLOCK_SHB):  # the same in either byte order
            magic = data[pos + 8 : pos + 12]
            if magic == struct.pack("<I", BYTE_ORDER_MAGIC):
                order = "<"
            elif magic == struct.pack(">I", BYTE_ORDER_MAGIC):
                order = ">"
            else:
                raise ValueError(f"bad byte-order magic at {pos:#x}")
            linktypes = []
        elif order is None:
            raise ValueError("no section header block first")
        kind, length = struct.unpack_from(order + "II", data, pos)
        if length < 12 or length % 4 or pos + length > len(data):
            raise ValueError(f"bad block length {length} at {pos:#x}")
        if struct.unpack_from(order + "I", data, pos + length - 4)[0] != length:
            raise ValueError(f"trailing block length differs at {pos:#x}")
        body = data[pos + 8 : pos + length - 4]
        if kind == BLOCK_IDB:
            linktypes.append(struct.unpack_from(order + "H", body)[0])
        elif kind == BLOCK_EPB:
            iface, _ts_hi, _ts_lo, cap_len, _orig_len = struct.unpack_from(order + "5I", body)
            if iface >= len(linktypes) or 20 + cap_len > len(body):
                raise ValueError(f"bad packet block at {pos:#x}")
            packets.append((linktypes[iface], body[20 : 20 + cap_len]))
        pos += length
    return packets


@dataclass(frozen=True)
class Urb:
    """One usbmon event (the 64-byte mmapped header, then the captured data)."""

    frame: int  # index of the packet in the file
    urb_id: int
    kind: str  # "S" submit, "C" completion, "E" error
    xfer: int  # 0 isochronous, 1 interrupt, 2 control, 3 bulk
    endpoint: int  # with the direction bit (DIR_IN)
    status: int
    length: int  # requested (submit) or transferred (completion)
    data: bytes

    @property
    def is_in(self) -> bool:
        return bool(self.endpoint & DIR_IN)


# id, type, xfer type, endpoint, devnum, busnum, setup flag, data flag, ts sec, ts usec,
# status, urb length, captured data length, setup bytes, interval, start frame, flags, ndesc
USBMON = struct.Struct("<QcBBBHbbqiiII8xiiII")
assert USBMON.size == USBMON_LEN


def usbmon_events(packets) -> list[Urb]:
    """The Urb of each LINKTYPE_USB_LINUX_MMAPPED packet; ValueError on another linktype or a short packet."""
    events = []
    for frame, (linktype, packet) in enumerate(packets):
        if linktype != LINKTYPE_USB_LINUX_MMAPPED:
            raise ValueError(f"packet {frame}: linktype {linktype}, not {LINKTYPE_USB_LINUX_MMAPPED}")
        if len(packet) < USBMON_LEN:
            raise ValueError(f"packet {frame}: {len(packet)} bytes, shorter than the usbmon header")
        urb_id, kind, xfer, ep, _dev, _bus, _setup, _flag, _s, _us, status, length, cap, *_ = USBMON.unpack_from(packet)
        data = packet[USBMON_LEN:]
        if len(data) != cap:
            raise ValueError(f"packet {frame}: {len(data)} data bytes, header says {cap}")
        events.append(Urb(frame, urb_id, kind.decode("latin-1"), xfer, ep, status, length, data))
    return events


@dataclass
class Exchanges:
    commands: list[Urb]  # every bulk OUT submit
    replies: list[tuple[Urb, Urb]]  # (bulk OUT submit, the bulk IN completion answering it)
    orphans: list[Urb]  # bulk IN completions with no command left to answer
    unanswered: list[Urb]  # commands no IN completion answered
    failed: list[Urb]  # completions with a non-zero status
    pending: list[Urb]  # submits that never complete


def pair_exchanges(events) -> Exchanges:
    """Pair each bulk IN completion with the newest bulk OUT command not yet answered.

    Submits and completions are matched by URB id; a command is answered once, so a
    second reply to it is an orphan, whatever the order of the events.
    """
    ex = Exchanges([], [], [], [], [], [])
    open_urbs = {}
    waiting = []  # commands not yet answered, oldest first
    for e in events:
        if e.kind == "S":
            open_urbs[e.urb_id] = e
            if e.xfer == XFER_BULK and not e.is_in:
                ex.commands.append(e)
                waiting.append(e)
            continue
        open_urbs.pop(e.urb_id, None)
        if e.status != 0:
            ex.failed.append(e)
        elif e.xfer == XFER_BULK and e.is_in:
            if waiting:
                ex.replies.append((waiting.pop(), e))
            else:
                ex.orphans.append(e)
    answered = {id(c) for c, _ in ex.replies}
    ex.unanswered = [c for c in ex.commands if id(c) not in answered]
    ex.pending = sorted(open_urbs.values(), key=lambda u: u.frame)
    return ex


def reply_problems(command: bytes, reply: bytes) -> list[str]:
    """What is wrong with a reply to one of the COMMANDS, by the project's parsers ([] when it parses)."""
    cmd = command[0]
    if cmd == CMD_IMAGE:
        r = parsers.parse_image_reply(reply)
        if r.kind == parsers.KIND_BAD:
            return [f"51 reply of {len(reply)} bytes does not parse: {r.problems}"]
        if r.kind == parsers.KIND_IMAGE and len(r.pixels) != parsers.IMAGE_WIDTH * parsers.IMAGE_HEIGHT:
            return [f"51 image of {len(r.pixels)} pixels"]
        return []
    if cmd in STATUS_ONLY:
        status = parsers.status_word(reply)
        return [] if status == 0 else [f"{cmd:02x}: status {status}"]
    if cmd == CMD_STOP:
        status = parsers.status_word(reply)
        label = parsers.classify_stop_status(status)[0]
        return [] if label in STOP_KNOWN else [f"04: status {status}: {label}"]
    d = REPLY_PARSERS[cmd](reply)
    problems = [f"{cmd:02x}: {w}" for w in d.warnings]
    if d.values.get("status") != 0:
        problems.append(f"{cmd:02x}: status {d.values.get('status')}")
    if d.reply_len != parsers.EXPECTED_LEN[cmd]:
        problems.append(f"{cmd:02x}: {d.reply_len} bytes, expected {parsers.EXPECTED_LEN[cmd]}")
    if cmd == parsers.CMD_SENSOR_ID and d.values.get("sensor_type") != parsers.EXPECTED_SENSOR_TYPE:
        problems.append(f"75: sensor type {d.values.get('sensor_type')}")
    if cmd == parsers.CMD_ROM_INFO and d.values.get("serial") != fx.SERIAL:
        problems.append("01: the RomInfo serial is not the synthetic fixtures.SERIAL")
    return problems


def foreign_traffic(events) -> list[tuple[int, str, int, int]]:
    """(frame, kind, transfer type, endpoint) of every event outside TRAFFIC (control, interrupt OUT, ...)."""
    return [(e.frame, e.kind, e.xfer, e.endpoint) for e in events if (e.xfer, e.endpoint) not in TRAFFIC]


def interrupt_events(events) -> tuple[list[str], list[str]]:
    """(the action of each EP 0x83 completion with data, one problem per unknown event)."""
    actions, problems = [], []
    for e in events:
        if e.kind != "C" or e.xfer != XFER_INTERRUPT or not e.is_in or not e.data:
            continue
        ev = parsers.classify_interrupt(e.data)
        actions.append(ev.action)
        if ev.action not in INTERRUPT_EVENTS and (ev.kind, ev.code) not in KNOWN_IGNORED:
            problems.append(f"frame {e.frame}: unknown EP {e.endpoint:#04x} event, kind {ev.kind} code {ev.code}")
    return actions, problems


@pytest.fixture(scope="module")
def events(fork) -> list[Urb]:
    return usbmon_events(pcapng_packets((fork / PCAPNG).read_bytes()))


@pytest.fixture(scope="module")
def exchanges(events) -> Exchanges:
    return pair_exchanges(events)


def test_fixture_has_only_the_driver_traffic(events):
    # No control transfer (umockdev answers the descriptors from the device file), no
    # interrupt OUT, no other endpoint: bulk 0x01 / 0x81 and interrupt IN 0x83 only.
    assert foreign_traffic(events) == []
    assert {(e.xfer, e.endpoint) for e in events} == TRAFFIC


def test_fixture_sends_only_the_driver_commands(exchanges):
    assert exchanges.commands
    assert [(c.frame, c.data[:1].hex()) for c in exchanges.commands if not c.data or c.data[0] not in COMMANDS] == []
    assert {c.data[0] for c in exchanges.commands} == COMMANDS  # each command is exercised


def test_every_fixture_reply_is_paired(exchanges):
    assert [(e.frame, e.endpoint) for e in exchanges.orphans] == []
    assert [(c.frame, c.data[:1].hex()) for c in exchanges.unanswered] == []
    assert [(e.frame, e.kind, e.status) for e in exchanges.failed] == []
    # A timeout or a cancel is a submit with no completion: only reads may have one.
    assert [(e.frame, e.endpoint) for e in exchanges.pending if not e.is_in] == []


def test_every_fixture_reply_parses(exchanges):
    seen = {}
    problems = []
    for command, reply in exchanges.replies:
        cmd = command.data[0]
        seen[cmd] = seen.get(cmd, 0) + 1
        problems += [f"frame {reply.frame}: {p}" for p in reply_problems(command.data, reply.data)]
    assert problems == []
    assert set(seen) == COMMANDS, seen  # every command's reply was checked
    kinds = {parsers.parse_image_reply(r.data).kind for c, r in exchanges.replies if c.data[0] == CMD_IMAGE}
    assert parsers.KIND_IMAGE in kinds
    stops = {parsers.status_word(r.data) for c, r in exchanges.replies if c.data[0] == CMD_STOP}
    assert parsers.IMAGE_STATUS_NO_CAPTURE in stops  # the 0x0412 sub-case (decision 10)


def test_every_fixture_interrupt_is_a_known_event(events):
    actions, problems = interrupt_events(events)
    assert problems == []
    assert {"finger", "ready", "extend"} <= set(actions)  # the extend sub-case (decision 10)


# ------------------------------------------------------------------ the audit itself, on synthetic bytes


SYN_SOURCE = fx.rand("public-data source", 400)  # synthetic, not private bytes
SYN_OTHER = fx.rand("public-data other", 300)


def test_window_scanner_finds_a_planted_window_and_reports_no_bytes():
    planted = SYN_SOURCE[100:132]
    items = public_items_from(
        {
            "a.bin": SYN_OTHER[:50] + planted + SYN_OTHER[50:],
            "b.txt": b"x = bytes.fromhex('" + SYN_SOURCE[200:216].hex().encode() + b"')\n",
            "clean": SYN_OTHER + bytes(64) + b"\xff" * 64,
        }
    )
    index = tpa.window_index({"src": SYN_SOURCE, "fill": bytes(32) + b"\x01\x02\x03" * 8})
    hits = window_hits(index, items)
    assert hits == {
        "a.bin": [f"@{50 + k:#x}=src+{100 + k}" for k in range(len(planted) - WINDOW + 1)],
        "b.txt[hex run at 0x13]": ["@0x0=src+200"],
    }
    report = format_hits(hits).encode()
    assert report.splitlines()[:2] == [b"a.bin@0x32=src+100", b"a.bin@0x33=src+101"]
    assert b"a.bin: 9 more window(s)" in report and b"b.txt[hex run at 0x13]@0x0=src+200" in report
    for k in range(len(planted) - WINDOW + 1):
        w = planted[k : k + WINDOW]
        assert w not in report and w.hex().encode() not in report.lower()


def public_items_from(files: dict[str, bytes]) -> list[tuple[str, bytes]]:
    items = []
    for name, data in files.items():
        items.append((name, data))
        items += [(f"{name}[hex run at {at:#x}]", run) for at, run in hex_runs(data)]
    return items


def _first(index: dict[bytes, str]) -> int:
    return min(int(v.rsplit("+", 1)[1]) for v in index.values())


def test_format_headers_are_not_indexed_but_windows_past_them_are():
    raw = fx.rand("raw header", 18) + SYN_SOURCE[:64]  # synthetic
    cal = fx.rand("cal header", calibration.SERIAL_OFFSET) + SYN_OTHER[:64]
    index, counted = private_index(Private("captures", {"x.raw": raw}))
    assert raw[:WINDOW] not in index and raw[2 : 2 + WINDOW] not in index  # wholly inside the 51 header
    assert index[raw[3 : 3 + WINDOW]] == "x.raw+3"  # reaches the first pixel
    assert counted == len(raw) - WINDOW + 1 - 3
    assert private_index(Private("captures", {"x.pgm": raw}))[0][raw[:WINDOW]] == "x.pgm+0"  # no PGM header parses
    assert private_index(Private("template", {"x.raw": raw}))[0][raw[:WINDOW]] == "x.raw+0"  # per group
    assert _first(private_index(Private("calibration", {"c.bin": cal}))[0]) == calibration.SERIAL_OFFSET - WINDOW + 1
    assert format_header_len("template", "t.bin") == 0


def test_a_runs_rominfo_reply_header_is_not_indexed():
    rom = bytearray(fx.rand("rom header", 38))  # synthetic
    rom[0x12:0x18] = fx.SERIAL
    for name in ("local/calib/r/reply-01-01.bin", "local/calib/r/reply-11-01.bin"):
        assert _first(private_index(Private("calib-run", {name: bytes(rom)}))[0]) == tns.ROM_SERIAL_OFFSET - WINDOW + 1
    assert _first(private_index(Private("calib-run", {"local/calib/r/reply-02-19.bin": bytes(rom)}))[0]) == 0
    assert _first(private_index(Private("program", {"local/calib/r/reply-01-01.bin": bytes(rom)}))[0]) == 0


PIXELS = fx.rand("pgm pixels", 144 * 56)  # synthetic
DIAG_COMMENT = b"# synaptics0078 seq 3 monotonic_us 123 capture status 0x0000 length 8076 x 144 y 56\n"


@pytest.mark.parametrize(
    ("header", "length"),
    [
        (b"P5\n144 56\n255\n", 14),
        (b"P5\n" + DIAG_COMMENT + b"144 56\n255\n", 14 + len(DIAG_COMMENT)),
        (b"P5 144\t56\r\n#c\n 255 ", 19),
        (b"P5\n144 56\n255", 0),  # no whitespace byte after maxval
        (b"P6\n144 56\n255\n", 0),
        (b"P5\n144 x\n255\n", 0),
        (b"P5\n144 55\n255\n", 0),  # more pixel bytes than the header gives
        (b"P5\n144 56\n65535\n", 0),  # two bytes a pixel: fewer pixel bytes than the header gives
        (b"P5\n#" + bytes(b % 10 + 0x30 for b in PIXELS) + b"\n1 1 1\n", 0),  # a comment over the pixels
        (b"", 0),
    ],
)
def test_pgm_header_len_parses_the_text_header(header, length):
    assert pgm_header_len(header + PIXELS) == length


def test_pgm_header_len_matches_the_writers():
    assert pgm_header_len(parsers.pgm_bytes(PIXELS)) == len(parsers.pgm_bytes(PIXELS)) - len(PIXELS)


@pytest.mark.parametrize("group", IMAGE_GROUPS)
def test_an_images_pgm_header_is_not_indexed(group):
    data = b"P5\n" + DIAG_COMMENT + b"144 56\n255\n" + PIXELS
    header = pgm_header_len(data)
    index, _ = private_index(Private(group, {"local/x/000001.pgm": data}))
    assert _first(index) == header - WINDOW + 1  # the first window reaching a pixel
    assert data[header - WINDOW : header] not in index


def test_prefilter_keeps_every_shared_window_and_drops_the_rest():
    public = [("a.bin", SYN_OTHER[:40] + SYN_SOURCE[100:140] + SYN_OTHER[40:]), ("b", SYN_SOURCE[300:316])]
    full, counted = private_index(Private("synthetic", {"src": SYN_SOURCE}))
    kept, counted_kept = private_index(Private("synthetic", {"src": SYN_SOURCE}), window_filter(public))
    assert counted_kept == counted == len(full)
    assert window_hits(kept, public) == window_hits(full, public) != {}
    assert len(kept) < len(full) // 4  # the bits of the public windows only (and a few false positives)


def test_counted_windows_are_counted_and_none_fails_the_group(monkeypatch):
    assert private_index(Private("synthetic", {"zeros": bytes(64), "fill": b"\x01\x02" * 40})) == ({}, 0)
    monkeypatch.setitem(GROUPS, "synthetic", (lambda: {"zeros": bytes(64)}, "absent"))
    monkeypatch.setattr(sys.modules[__name__], "public_filter", lambda: bytearray(FILTER_BITS >> 3))
    _HELD.clear()
    try:
        skip, report = window_report("synthetic", "project", ["x"])
    finally:
        _HELD.clear()
    assert skip is None and report == "the 1 present synthetic source(s) yield no counted window: nothing audited"


def test_run_files_takes_the_binary_files_of_each_run(tmp_path):
    run = tmp_path / "20260101-000000"
    (run / "sub").mkdir(parents=True)
    (run / "a.bin").write_bytes(b"text only")  # a .bin is binary data
    (run / "blob").write_bytes(b"\x00\x01")
    (run / "bad-utf8").write_bytes(b"caf\xe9")
    (run / "calibrate.log").write_bytes("log \u2014 ok\r\n\tdone\n".encode())  # UTF-8 text
    (run / "result.json").write_bytes(b'{"a": 1}')
    (run / RUN_PROGRAM).write_bytes(b"\x00")  # in the program group
    (run / "sub" / "deep.bin").write_bytes(b"\x00")  # not directly in the run dir
    (run / "link.bin").symlink_to(run / "blob")
    (tmp_path / "loose.bin").write_bytes(b"\x00")  # not in a run dir
    assert run_files(tmp_path) == {run / "a.bin": b"text only", run / "bad-utf8": b"caf\xe9", run / "blob": b"\x00\x01"}
    assert run_dirs(tmp_path / "missing") == []


def test_log_6f_reply_reads_the_rx_line(tmp_path):
    reply = fx.rand("6f reply", REPLY_6F_LEN)  # synthetic
    log = tmp_path / "probe.log"
    log.write_text(
        "2026-01-01T00:00:00.000000-03:00 TX EP 0x01 9 byte(s): 6f000e000000000000\n"
        f"2026-01-01T00:00:00.100000-03:00 RX EP 0x81 10 byte(s) in 1.0 ms: {bytes(10).hex()}\n"
        f"2026-01-01T00:00:00.200000-03:00 RX EP 0x81 {REPLY_6F_LEN} byte(s) in 1.0 ms: {reply.hex()}\n"
    )
    assert log_6f_reply(log) == reply
    log.write_text("no reply\n")
    assert log_6f_reply(log) is None
    assert log_6f_reply(tmp_path / "missing.log") is None


def test_diag_dirs_take_diag_star_but_diag_fprintd(tmp_path):
    for name in ("diag", "diag-2", DIAG_FPRINTD.name, "far-diag"):
        (tmp_path / name).mkdir()
    (tmp_path / "diag.txt").write_text("")
    assert diag_dirs(tmp_path) == [tmp_path / "diag", tmp_path / "diag-2"]
    assert diag_dirs(tmp_path / "missing") == []


def test_private_labels_mask_the_serial(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_serial_patterns", lambda: tuple(tns.serial_forms([fx.SERIAL])))
    label = _label(REPO / "local" / f"x-{fx.SERIAL.hex()}.pgm")
    assert label == "local/x-<serial>.pgm"


def test_staged_items_are_the_staged_blobs_that_differ_from_disk(tmp_path):
    env = tns.git_env()

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "same.txt").write_text("same\n")
    (tmp_path / "moved.txt").write_bytes(b"dump " + SYN_SOURCE[:16].hex().encode() + b"\n")
    git("add", "same.txt", "moved.txt")
    (tmp_path / "moved.txt").write_text("cleaned\n")  # the disk copy no longer holds the dump
    assert staged_items(tmp_path, env) == [
        ("moved.txt (staged)", b"dump " + SYN_SOURCE[:16].hex().encode() + b"\n"),
        ("moved.txt (staged)[hex run at 0x5]", SYN_SOURCE[:16]),
    ]


def test_hex_runs_match_the_package_audits_decoder():
    planted = SYN_SOURCE[:20]
    text = b"a = '" + planted.hex().encode() + b"'\nb = [" + ", ".join(f"0x{b:02x}" for b in planted).encode() + b"]\n"
    runs = hex_runs(text)
    assert [run for _, run in runs] == tpa.decoded_hex(text) == [planted, planted]
    assert runs[0][0] == 5


def test_public_items_read_disk_and_hex_runs(tmp_path):
    (tmp_path / "t.txt").write_bytes(b"dump " + SYN_SOURCE[:16].hex(" ").encode() + b"\n")
    (tmp_path / "d.bin").write_bytes(b"\x00\x01")
    assert public_items(tmp_path, ["t.txt", "d.bin", "gone"]) == [
        ("d.bin", b"\x00\x01"),
        ("t.txt", (tmp_path / "t.txt").read_bytes()),
        ("t.txt[hex run at 0x5]", SYN_SOURCE[:16]),
    ]


def test_private_repr_shows_no_bytes():
    p = Private("synthetic", {"src": SYN_SOURCE})
    assert repr(p) == f"<private synthetic: src {len(SYN_SOURCE)} bytes>"


def _block(order: str, kind: int, body: bytes) -> bytes:
    body += bytes(-len(body) % 4)
    return struct.pack(order + "II", kind, 12 + len(body)) + body + struct.pack(order + "I", 12 + len(body))


def _usbmon(urb_id, kind, xfer, ep, status, length, data=b"") -> bytes:
    return USBMON.pack(urb_id, kind, xfer, ep, 2, 1, 45, 0, 0, 0, status, length, len(data), 0, 0, 0, 0) + data


def _pcapng(order: str, packets, linktype=LINKTYPE_USB_LINUX_MMAPPED) -> bytes:
    out = _block(order, BLOCK_SHB, struct.pack(order + "IHHq", BYTE_ORDER_MAGIC, 1, 0, -1))
    out += _block(order, BLOCK_IDB, struct.pack(order + "HHI", linktype, 0, 0x40000))
    out += _block(order, 0x00000004, b"\x00\x00\x00\x00")  # a name resolution block: skipped
    for p in packets:
        out += _block(order, BLOCK_EPB, struct.pack(order + "5I", 0, 0, 0, len(p), len(p)) + p)
    return out


@pytest.mark.parametrize("order", ["<", ">"])
def test_pcapng_reader_on_hand_built_blocks(order):
    p1 = _usbmon(7, b"S", XFER_BULK, 0x01, -115, 1, b"\x01")
    p2 = _usbmon(7, b"C", XFER_BULK, 0x01, 0, 1)
    data = _pcapng(order, [p1, p2])
    assert pcapng_packets(data) == [(220, p1), (220, p2)]
    assert usbmon_events(pcapng_packets(data)) == [
        Urb(0, 7, "S", XFER_BULK, 0x01, -115, 1, b"\x01"),
        Urb(1, 7, "C", XFER_BULK, 0x01, 0, 1, b""),
    ]


def test_pcapng_reader_rejects_malformed_input():
    good = _pcapng("<", [_usbmon(1, b"S", XFER_BULK, 0x01, -115, 1, b"\x01")])
    with pytest.raises(ValueError, match="trailing"):
        pcapng_packets(good[:-4] + b"\x00\x00\x00\x00")
    with pytest.raises(ValueError, match="section header"):
        pcapng_packets(_block("<", BLOCK_IDB, bytes(8)))
    with pytest.raises(ValueError, match="linktype"):
        usbmon_events(pcapng_packets(_pcapng("<", [bytes(64)], linktype=1)))
    with pytest.raises(ValueError, match="data bytes"):
        usbmon_events([(220, _usbmon(1, b"S", XFER_BULK, 0x01, -115, 2, b"\x01\x02")[:-1])])


def _events(*specs) -> list[Urb]:
    return [Urb(k, *s) for k, s in enumerate(specs)]


def test_pairing_matches_replies_and_reports_the_rest():
    ev = _events(
        (1, "S", XFER_BULK, 0x01, -115, 1, b"\x01"),
        (1, "C", XFER_BULK, 0x01, 0, 1, b""),
        (2, "S", XFER_BULK, 0x81, -115, 1024, b""),
        (2, "C", XFER_BULK, 0x81, 0, 2, b"\x00\x00"),  # answers 01
        (3, "S", XFER_BULK, 0x81, -115, 1024, b""),
        (3, "C", XFER_BULK, 0x81, 0, 2, b"\x00\x00"),  # nothing left to answer: an orphan
        (4, "S", XFER_INTERRUPT, 0x83, -115, 8, b""),  # never completes: a timeout
        (5, "S", XFER_BULK, 0x01, -115, 1, b"\x19"),
        (6, "S", XFER_BULK, 0x01, -115, 1, b"\x75"),  # its OUT never completes
        (5, "C", XFER_BULK, 0x01, 0, 1, b""),
        (7, "S", XFER_BULK, 0x81, -115, 1024, b""),
        (7, "C", XFER_BULK, 0x81, 0, 10, bytes(10)),  # answers the newest command, 75
        (8, "S", XFER_BULK, 0x81, -115, 1024, b""),
        (8, "C", XFER_BULK, 0x81, -2, 0, b""),  # failed (cancelled)
    )
    ex = pair_exchanges(ev)
    assert [(c.data, r.frame) for c, r in ex.replies] == [(b"\x01", 3), (b"\x75", 11)]
    assert [e.frame for e in ex.orphans] == [5]
    assert [c.data for c in ex.unanswered] == [b"\x19"]
    assert [e.frame for e in ex.failed] == [13]
    assert [e.frame for e in ex.pending] == [6, 8]


def test_reply_checks_on_synthetic_replies():
    rom = bytearray(38)
    rom[10], rom[11] = 6, 0x14
    rom[0x12:0x18] = fx.SERIAL
    assert reply_problems(b"\x01", bytes(rom)) == []
    rom[0x12] ^= 1
    assert reply_problems(b"\x01", bytes(rom)) == ["01: the RomInfo serial is not the synthetic fixtures.SERIAL"]
    assert reply_problems(b"\x19", bytes(68)) == []
    assert reply_problems(b"\x19", b"\x01\x00" + bytes(66)) == [
        "19: non-zero status: remaining bytes not decoded",
        "19: status 1",
    ]
    assert reply_problems(b"\x75", fx.SENSOR_ID) == []
    assert reply_problems(b"\x75", bytes(10))[-1] == "75: sensor type None"
    pixels = bytes(parsers.IMAGE_WIDTH * parsers.IMAGE_HEIGHT)
    image = struct.pack("<HIHHHHI", 0, 12 + len(pixels), parsers.IMAGE_WIDTH, parsers.IMAGE_HEIGHT, 0, 0, 0) + pixels
    assert reply_problems(b"\x51\x00\x20\x00\x00", image) == []
    assert reply_problems(b"\x51", b"\x01\x00") == []  # not ready: a kind the parser knows
    assert reply_problems(b"\x51", b"\x00\x00\xff") != []
    for cmd in STATUS_ONLY:
        assert reply_problems(bytes([cmd]), b"\x00\x00") == []
        assert reply_problems(bytes([cmd]), b"\x01\x04") == [f"{cmd:02x}: status 1025"]
        assert reply_problems(bytes([cmd]), b"") == [f"{cmd:02x}: status None"]
    assert reply_problems(b"\x04", b"\x00\x00") == reply_problems(b"\x04", b"\x12\x04") == []
    assert reply_problems(b"\x04", b"\x01\x00") == ["04: status 1: unclassified non-zero status"]
    assert reply_problems(b"\x04", b"") == ["04: status None: no status word"]


def test_foreign_traffic_and_unknown_interrupts_are_reported():
    ev = _events(
        (1, "S", XFER_BULK, EP_OUT, -115, 1, b"\x01"),
        (2, "S", XFER_CONTROL, 0x00, -115, 0, b""),  # a control request
        (3, "S", XFER_INTERRUPT, 0x03, -115, 5, b"\x00" * 5),  # an interrupt OUT
        (4, "C", XFER_INTERRUPT, EP_INT, 0, 5, bytes.fromhex("0200401000")),
        (4, "C", XFER_INTERRUPT, EP_INT, 0, 5, bytes.fromhex("0000000000")),  # start ack: known, ignored
        (4, "C", XFER_INTERRUPT, EP_INT, 0, 5, bytes.fromhex("0340010000")),  # intermediate: known, ignored
        (4, "C", XFER_INTERRUPT, EP_INT, 0, 5, bytes.fromhex("0342040040")),
        (4, "C", XFER_INTERRUPT, EP_INT, 0, 5, bytes.fromhex("0342090040")),  # code 9: unknown
        (4, "C", XFER_INTERRUPT, EP_INT, 0, 5, bytes.fromhex("0900000000")),  # kind 9: unknown
        (4, "C", XFER_INTERRUPT, EP_INT, 0, 0, b""),  # no data: not an event
    )
    assert foreign_traffic(ev) == [(1, "S", XFER_CONTROL, 0x00), (2, "S", XFER_INTERRUPT, 0x03)]
    actions, problems = interrupt_events(ev)
    assert actions == ["finger", "ignore", "ignore", "ready", "ignore", "ignore"]
    assert problems == [
        "frame 7: unknown EP 0x83 event, kind 3 code 9",
        "frame 8: unknown EP 0x83 event, kind 9 code None",
    ]
