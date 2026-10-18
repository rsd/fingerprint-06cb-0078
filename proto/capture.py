#!/usr/bin/env python3
"""First image capture for the Synaptics 06cb:0078 (Clevo P870KM_KM1).

Replicates the Windows driver's identify session as closely as the evidence
allows. Unlike probe.py this WRITES to the sensor (a RAM module and a capture
program, see below); it never writes flash, pairs, or sends 4f or 1a.

Sequence (see build_plan(); every step names its source):

   1  01             RomInfo               -> 38 bytes
   2  19             opaque query          -> 68 bytes
   3  75             sensor id             -> 10 bytes (type 0x2449)
   4  06 + module    capture module, 10500 bytes from the vendor DLL (10501 sent);
                     SKIPPED when the 01 reply says a module is already loaded (RomInfo
                     byte 17 != 0, an UNCERTAIN inference, see module_decision()); a second
                     06 is refused with status 0x04aa. --force-module sends it anyway
   5  02 + payload   capture program, 18869 bytes built by calibration.py
   6  EP 0x83        wait for the finger-down event, then "image ready"
                     (--no-finger skips this and goes straight to step 7)
   7  51 00 20 00 00 read the 144 x 56 8-bit image on EP 0x81. Status 0x0001 (6-byte reply
                     01 00 00 00 00 00) means "no image / no finger": expected with
                     --no-finger, retried then reported as a failure otherwise
   8  04, 52         stop capture / cleanup (sent on EVERY exit path once 02 was sent)

Sources are cited as:
  ETW  the Windows USB ETW trace summary of one identify (sizes and order only, no
       payloads; a local file, not published)
  [CAL] research/calibration-analysis.md
  [TK] research/review-toliknosov.md   (ToLiKNoSoV driver, claims only)
  [PRIOR] research/review-prior.md     (iusearch/Synaptics78 main.py, python-validity PR #288)
  [PV] ashh87/python-validity dev-00a8 validitysensor/ (file:line as in parsers.py)

Safety rules enforced in code, not by convention:
  * check_opcode(): only the eight opcodes above can be written, and 4f / 1a /
    anything touching flash, pairing or partitions is refused by name;
  * every reply status is checked; the first non-zero status or timeout stops the
    run. The one exception is the 51 image request: statuses 1, 2, 5, 6 (no image header) are
    "no image yet" states in the references, so 51 is retried up to --image-retries
    times (read again, then re-send; see Session.read_image) before the run stops.
    With --no-finger, status 1 on the first 51 is the expected answer and ends the run
    successfully without a frame;
  * no USB reset unless --usb-reset; no clear_halt; no vendor control transfers;
  * the cleanup (04, 52, release interface, dispose) runs on every exit path.

Usage: see README.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import signal
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import calibration  # an alias of synaptics0078.calibration; it puts the repo root on sys.path
import parsers
import probe

from synaptics0078 import pins, session

EP_OUT, EP_IN, EP_INT = probe.EP_OUT, probe.EP_IN, 0x83
INT_READ_SIZE = 8  # ETW lines 512, 514: the interrupt IN reads are 8 bytes (wMaxPacketSize 8)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DLL = ROOT / "driverstore" / "synaWudfBioUsb.dll"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "captures"
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"

# The 10500-byte capture module: [TK] section 2 found it once in the DLL at file
# offset 0x19cca0 (byte-identical to ToLiKNoSoV's CMD_LONG1[1:] and iusearch's
# 10501-byte literal); [PRIOR] headline 4: DLL module-table record 30, ROM 6/0x14,
# generation 3. The hash is of the 10500 bytes, measured on our DLL (5.5.2707.1073).
# The pins live in synaptics0078/pins.py; these names re-export them.
MODULE_OFFSET = 0x19CCA0
MODULE_LEN = pins.MODULE_LEN
MODULE_SHA256 = pins.MODULE_SHA256

# Opcodes this program may write. Anything else is refused before it reaches the
# wire. 01/19/75/06/02/51/04/52 are exactly the commands of the Windows identify
# session (ETW lines 486-531 by size) and of [PRIOR] iusearch main.py / PR #288.
OP_ROM_INFO, OP_QUERY_19, OP_SENSOR_ID = 0x01, 0x19, 0x75
OP_MODULE, OP_CAPTURE_PROGRAM, OP_IMAGE, OP_STOP, OP_CLEANUP = 0x06, 0x02, 0x51, 0x04, 0x52
ALLOWED_OPCODES = frozenset(
    {OP_ROM_INFO, OP_QUERY_19, OP_SENSOR_ID, OP_MODULE, OP_CAPTURE_PROGRAM, OP_IMAGE, OP_STOP, OP_CLEANUP}
)
# Named so the refusal message says why. Not exhaustive; the allow-list is the real gate.
# Shared with the calibration guard (synaptics0078/session.py), where the dict lives.
NEVER_SEND = session.NEVER_SEND

REQ_IMAGE = bytes.fromhex("51 00 20 00 00")  # [PV] sensor.py:60-61 get_prg_status2(); [TK] section 1 step 8

EXIT_OK, EXIT_USB_ERROR, EXIT_NOT_FOUND, EXIT_DRIVER_BUSY, EXIT_BAD_REPLY = 0, 1, 2, 3, 4
EXIT_BAD_INPUT, EXIT_CAPTURE_FAILED, EXIT_INTERRUPTED = 5, 6, 130


class StepFailed(Exception):
    """A step did not go as required; the run stops. `rc` becomes the exit code."""

    def __init__(self, message: str, rc: int):
        super().__init__(message)
        self.rc = rc


class CaptureFailed(StepFailed):
    """The capture itself failed (error reply, no capture in progress, failed/aborted event).

    Unlike a timeout or a bad status on a command this is something a new attempt can fix:
    collection mode stops the block with 04/52 and restarts the image from 02. `user_message`
    is what the user is told; `reply` is the offending 51 reply, if any.
    """

    def __init__(self, user_message: str, reply: bytes = b"", kind: str = "", events=None, detail: str = ""):
        super().__init__(user_message, EXIT_CAPTURE_FAILED)
        self.user_message, self.reply, self.kind, self.detail = user_message, reply, kind, detail
        self.events = list(events or [])


# Raised before the wire is touched when a write would leave the allow-list. The class
# lives in synaptics0078/session.py, the base of the calibration guard's refusals too.
UnsafeCommand = session.UnsafeCommand


def check_opcode(data: bytes) -> None:
    """Refuse any write whose first byte is not on the allow-list."""
    if not data:
        raise UnsafeCommand("empty write")
    op = data[0]
    if op in NEVER_SEND:
        raise UnsafeCommand(f"opcode 0x{op:02x} is never sent: {NEVER_SEND[op]}")
    if op not in ALLOWED_OPCODES:
        raise UnsafeCommand(f"opcode 0x{op:02x} is not on the allow-list")


# --------------------------------------------------------------------- plan


@dataclass(frozen=True)
class Step:
    name: str
    kind: str  # "cmd", "wait_finger", "wait_ready", "image"
    tx: bytes = b""
    read_size: int = probe.READ_SIZE
    expect: str = ""  # expected reply, for the dry-run listing
    source: str = ""
    cleanup: bool = False
    note: str = ""  # extra line in the dry-run listing (conditions, caveats)


def build_plan(module: bytes, cmd02: bytes, finger: bool = True) -> list[Step]:
    """The complete, ordered list of steps. Pure; this is what --dry-run prints."""
    if len(module) != MODULE_LEN:
        raise ValueError(f"module is {len(module)} bytes, expected {MODULE_LEN}")
    if len(cmd02) != calibration.PAYLOAD_LEN or cmd02[0] != OP_CAPTURE_PROGRAM:
        raise ValueError("cmd02 is not an 18869-byte 02 command")
    plan = [
        Step(
            "RomInfo",
            "cmd",
            bytes([OP_ROM_INFO]),
            expect="38 bytes, status 0",
            # The ETW also shows one earlier 1-byte command with a 2-byte reply and a
            # vendor control read 0xC0/0x14 (2 bytes) first (lines 484-489). Their
            # opcode/meaning is unknown, so they are NOT replicated (see README).
            source="ETW lines 490-493 (1 B out, 0x40 read, 0x26 = 38 B in); opcode from [PRIOR] sec.1a, [PV] sensor.py:91",
        ),
        Step(
            "query 0x19",
            "cmd",
            bytes([OP_QUERY_19]),
            expect="68 bytes, status 0",
            source="ETW lines 494-499 (1 B out, 64 + 4 B in); opcode from [PRIOR] sec.1a, [PV] usb.py:143-150",
        ),
        Step(
            "sensor id",
            "cmd",
            bytes([OP_SENSOR_ID]),
            expect="10 bytes, status 0",
            source="ETW lines 500-503 (1 B out, 10 B in); opcode from [PRIOR] sec.1a, [PV] sensor.py:105-117",
        ),
        Step(
            "capture module",
            "cmd",
            bytes([OP_MODULE]) + module,
            expect="2 bytes, status 0",
            source="ETW lines 504-507 (0x2905 = 10501 B out, 2 B in); bytes = DLL 0x19cca0 ([TK] sec.2, [PRIOR] headline 4)",
            note="CONDITIONAL: skipped when the 01 reply's byte 17 (RomInfo u1) is non-zero, i.e. a module is probably loaded "
            "(UNCERTAIN: hardware runs 1-2 saw 0x00 -> 0x0e after the upload and 0x04aa on a second 06; DLL 0x18005f960 "
            "prints it as the third version component). --force-module sends 06 regardless",
        ),
        Step(
            "capture program",
            "cmd",
            cmd02,
            read_size=0x1000,
            expect="2154 bytes, status 0",
            source="ETW lines 508-511 (0x49B5 = 18869 B out, 0x1000 read, 0x86A = 2154 B in); bytes = calibration.py ([CAL] sec.0-2)",
        ),
    ]
    if finger:
        plan += [
            Step(
                "wait for finger down",
                "wait_finger",
                expect="EP 0x83 type-2 event",
                source="ETW lines 512-514 (8 B interrupt reads on 0x83, a 5 B event right after 02); event 02 = finger: [PV] sensor_2449.py:515-523, iusearch main.py:63",
            ),
            Step(
                "wait for image ready",
                "wait_ready",
                expect="EP 0x83 type-3 event code 4 or 7",
                source="[PV] sensor_2449.py:525-545 (codes 4/7 ready, 0/6 fail, 5 keep waiting); iusearch main.py:70-91",
            ),
        ]
    plan.append(
        Step(
            "read image" + ("" if finger else " (no finger wait)"),
            "image",
            REQ_IMAGE,
            read_size=16384,
            expect="8082 bytes: status, u32 len, 12-byte header, 144 x 56 pixels",
            source="[PV] sensor_2449.py:575-592 and sensor.py:60-61 (image comes on EP 0x81; EP 0x82 is only for calibration frames, "
            "sensor_2449.py:293-296); [TK] sec.1 steps 8,10,11; iusearch main.py:98-101. NOT in the ETW (no finger touched)",
            note="reply 6 bytes 01 00 00 00 00 00 = status 0x0001 = no image / no finger ([TK] synaptics_0078.c:72-73,266-274; "
            "hardware run 1). With --no-finger this is the expected answer and the run ends OK without a frame. Otherwise "
            "statuses 1,2,5,6 without an image header ([PV] sensor_2449.py:85 lists 7 too; here 7 came with an error header and is a failed capture) are retried up to --image-retries times: first a plain read "
            "again (iusearch main.py:100-101), and if nothing arrives within 1 s 51 is re-sent ([PV] sensor_2449.py:546-553)",
        )
    )
    plan += [
        Step(
            "stop capture",
            "cmd",
            bytes([OP_STOP]),
            cleanup=True,
            expect="2 bytes",
            source="ETW lines 524-527 (final 1 B out, 2 B in, after the pending interrupt read was cancelled); opcode 04 from [PV] sensor_2449.py:569-571, iusearch main.py:132",
        ),
        Step(
            "cleanup",
            "cmd",
            bytes([OP_CLEANUP]),
            cleanup=True,
            expect="2 bytes",
            source="ETW lines 528-531 (second final 1 B out, 2 B in); opcode 52 from [PV] sensor_2449.py:569-571, iusearch main.py:133",
        ),
    ]
    return plan


def summarise(data: bytes, full_up_to: int = 32) -> str:
    """Short text for a payload: the bytes if small, else length + sha256 + head."""
    if len(data) <= full_up_to:
        return parsers.hexstr(data) if data else "(none)"
    return f"{len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}, head {parsers.hexstr(data[:12])} ..."


def describe_plan(plan: list[Step]) -> list[str]:
    lines = []
    main = [s for s in plan if not s.cleanup]
    clean = [s for s in plan if s.cleanup]
    n = 0
    for s in main:
        n += 1
        if s.kind == "cmd" or s.kind == "image":
            lines.append(f"{n:2d}. WRITE EP 0x{EP_OUT:02x}: {summarise(s.tx)}")
            lines.append(f"      then READ EP 0x{EP_IN:02x} (buffer {s.read_size}): expect {s.expect}")
        else:
            lines.append(f"{n:2d}. READ EP 0x{EP_INT:02x} ({INT_READ_SIZE}-byte reads): {s.name}: expect {s.expect}")
        lines.append(f"      step: {s.name}")
        lines.append(f"      source: {s.source}")
        if s.note:
            lines.append(f"      note: {s.note}")
    lines.append(
        "Cleanup, sent on every exit path once the 02 write has been attempted (errors there never mask the real outcome):"
    )
    for s in clean:
        n += 1
        lines.append(
            f"{n:2d}. WRITE EP 0x{EP_OUT:02x}: {summarise(s.tx)}  then READ EP 0x{EP_IN:02x}: expect {s.expect}"
        )
        lines.append(f"      source: {s.source}")
    return lines


def module_decision(rom_values: dict, force: bool) -> tuple[bool, str]:
    """(skip 06?, reason) from the decoded 01 reply. Pure.

    UNCERTAIN: the 06 module is treated as loaded when RomInfo byte 17 ('u1') is non-zero.
    Evidence (weak but consistent): DLL 0x18005f960 prints u1 as the third version component
    and 0x180080c00 tests it for non-zero; on this unit u1 read 0x00 before the first 06 and
    0x0e after it (capture-20261013-2049*.log), and a second 06 was refused with 0x04aa. No
    python reference tests the byte (python-validity's capture_module_loaded is a host flag).
    """
    loaded, why = parsers.module_probably_loaded(rom_values.get("u1", 0))
    if force:
        return False, f"--force-module: sending 06 regardless ({why})"
    if loaded:
        return True, f"skipping 06: {why}; UNCERTAIN check, --force-module sends it anyway"
    return False, f"sending 06: {why}"


# ------------------------------------------------------------------- inputs


def check_module(module: bytes) -> bytes:
    if len(module) != MODULE_LEN:
        raise calibration.CalibrationError(f"module is {len(module)} bytes, expected {MODULE_LEN}")
    got = hashlib.sha256(module).hexdigest()
    if got != MODULE_SHA256:
        raise calibration.CalibrationError(f"module sha256 {got} is not the expected {MODULE_SHA256}")
    return module


def read_module(dll_path: Path) -> bytes:
    """The 10500 module bytes at file offset 0x19cca0 of the vendor DLL, hash-checked."""
    with open(dll_path, "rb") as fh:
        fh.seek(MODULE_OFFSET)
        data = fh.read(MODULE_LEN)
    return check_module(data)


def load_inputs(dll_path: Path, calibration_path: Path):
    """(module, cmd02, calibration inputs); raises calibration.CalibrationError / OSError."""
    module = read_module(dll_path)
    inputs = calibration.parse_calibration_data(Path(calibration_path).read_bytes())
    return module, calibration.build_cmd02(inputs), inputs


# ------------------------------------------------------------ output files


def claim_paths(directory: Path, exts: tuple[str, ...]) -> list[Path]:
    """Unique, timestamped output paths: capture-<date>-<time>-<us>-p<pid>[-N].<ext>.

    The first file is created with mode "x" (exclusive) so two runs, in the same
    microsecond or not, can never share or overwrite a name; every extension of a
    set carries the same stem.
    """
    directory.mkdir(parents=True, exist_ok=True)
    base = f"capture-{dt.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')}-p{os.getpid()}"
    for n in range(1000):
        stem = base if n == 0 else f"{base}-{n}"
        paths = [directory / f"{stem}.{e}" for e in exts]
        if any(p.exists() for p in paths):
            continue
        try:
            with open(paths[0], "xb"):
                pass
        except FileExistsError:
            continue
        return paths
    raise FileExistsError(f"no free output name in {directory}")


def image_stats(pixels: bytes) -> str:
    mean = statistics.fmean(pixels)
    sd = statistics.pstdev(pixels)
    return f"min {min(pixels)}, max {max(pixels)}, mean {mean:.1f}, sd {sd:.2f}"


# --------------------------------------------------------------------- run


LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_LIFT_PAUSE_S = 3.0


def label_paths(directory: Path, label: str, index: int) -> tuple:
    """(pgm, raw) for take `index` of a label: <dir>/<label>-01.pgm and .raw."""
    return directory / f"{label}-{index:02d}.pgm", directory / f"{label}-{index:02d}.raw"


def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _ask(prompt: str) -> str:
    return input(prompt)


class Manifest:
    """<out-dir>/<label>/manifest.json, rewritten atomically after every change so an
    interrupted or failed collection keeps a readable record of what was saved.

    With append=True an existing manifest is extended: a new entry in `sessions`, new images appended
    to `images`. Manifests written before `sessions` existed get one synthesised from their top-level fields.
    """

    def __init__(self, directory: Path, label: str, requested: int, info: dict, append: bool = False):
        self.directory = directory
        self.path = directory / "manifest.json"
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        self.session = {"started": now, "requested_count": requested, "status": "in progress", "info": info}
        if append and self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("label") != label:
                raise StepFailed(
                    f"{self.path} belongs to label {self.data.get('label')!r}, not {label!r}", EXIT_BAD_INPUT
                )
            if "sessions" not in self.data:
                self.data["sessions"] = [
                    {
                        k: self.data[k]
                        for k in ("started", "finished", "requested_count", "status", "info")
                        if k in self.data
                    }
                ]
            self.data.setdefault("failed_attempts", [])
            self.data["sessions"].append(self.session)
            self.data["requested_count"] = self.data.get("requested_count", 0) + requested
            self.data["status"] = "in progress"
        else:
            self.data = {
                "label": label,
                "tool": "proto/capture.py",
                "started": now,
                "requested_count": requested,
                "status": "in progress",
                "info": info,
                "images": [],
                "failed_attempts": [],
                "sessions": [self.session],
            }
        self.write()

    def next_index(self) -> int:
        """First unused image number: above everything in the manifest and everything on disk."""
        label = self.data["label"]
        used = [e.get("index", 0) for e in self.data["images"]]
        pat = re.compile(rf"^{re.escape(label)}-(\d+)\.(pgm|raw)$")
        for f in self.directory.iterdir():
            m = pat.match(f.name)
            if m:
                used.append(int(m.group(1)))
        return max(used, default=0) + 1

    def add(self, entry: dict) -> dict:
        self.data["images"].append(entry)
        self.write()
        return entry

    def add_failure(self, record: dict) -> None:
        self.data["failed_attempts"].append(record)
        self.write()

    def finish(self, status: str) -> None:
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        self.session.update(status=status, finished=now)
        self.data.update(status=status, finished=now)
        self.data["accepted_count"] = sum(1 for e in self.data["images"] if e.get("accepted"))
        self.write()

    def write(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def holds_collection(directory: Path, label: str) -> bool:
    return directory.exists() and (any(directory.glob(f"{label}-*.pgm")) or (directory / "manifest.json").exists())


class Session:
    """One capture run: the exchange helpers share the device, log and timeout."""

    def __init__(self, dev, log, args):
        self.dev, self.log, self.args = dev, log, args
        self.armed = False  # True once the 02 write has been attempted
        self.image_done = False  # True once the current block's image has been received and saved
        self.block_events = []  # EP 0x83 payloads (hex) seen since the current 02
        import usb.core

        self._usb_core = usb.core

    # -- low level -----------------------------------------------------------

    def write(self, data: bytes, timeout_ms: int) -> None:
        check_opcode(data)
        self.log.write(
            "TX", f"EP 0x{EP_OUT:02x} {len(data)} byte(s): {data.hex() if len(data) <= 64 else summarise(data)}"
        )
        written = self.dev.write(EP_OUT, data, timeout=timeout_ms)
        if written != len(data):
            raise StepFailed(f"short write: {written} of {len(data)} bytes", EXIT_USB_ERROR)

    def read(self, size: int, timeout_ms: int, what: str) -> tuple[bytes, float]:
        t0 = time.monotonic()
        try:
            reply = bytes(self.dev.read(EP_IN, size, timeout=timeout_ms))
        except self._usb_core.USBTimeoutError:
            raise StepFailed(
                f"read on EP 0x{EP_IN:02x} timed out after {timeout_ms} ms ({what}); stopping", EXIT_USB_ERROR
            ) from None
        return reply, (time.monotonic() - t0) * 1000

    def exchange(self, step: Step) -> bytes:
        """Write, read the reply, require status 0. Returns the reply."""
        tx = step.tx
        if tx[0] == OP_CAPTURE_PROGRAM:
            self.armed = True  # even a partial write may have started the program
        self.write(tx, self.args.timeout_ms)
        reply, ms = self.read(step.read_size, self.args.timeout_ms, f"{step.name}, opcode 0x{tx[0]:02x}")
        self.log.write("RX", f"EP 0x{EP_IN:02x} {len(reply)} byte(s) in {ms:.1f} ms: {reply.hex()}")
        status = parsers.status_word(reply)
        print(
            f"  {step.name}: {len(reply)} byte(s), status {'n/a' if status is None else f'0x{status:04x}'}, {ms:.1f} ms"
        )
        if status != 0:
            hint = ""
            if tx[0] == OP_MODULE and status == 0x04AA:
                hint = (
                    " (0x04aa after a successful earlier upload: the module is probably already loaded, "
                    "run without --force-module)"
                )
            raise StepFailed(
                f"{step.name}: reply status {'missing' if status is None else f'0x{status:04x}'} (non-zero){hint}; stopping",
                EXIT_BAD_REPLY,
            )
        return reply

    def query(self, step: Step) -> parsers.Decoded:
        """01 / 19 / 75: exchange plus the decoded view, as probe.py shows it."""
        op = step.tx[0]
        self.write(step.tx, self.args.timeout_ms)
        reply, ms = self.read(step.read_size, self.args.timeout_ms, f"{step.name}, opcode 0x{op:02x}")
        decoded = probe.show_exchange(self.log, op, step.tx, step.name, reply, ms)
        if decoded.values.get("status") != 0:
            raise StepFailed(f"{step.name}: reply status is not 0; stopping", EXIT_BAD_REPLY)
        return decoded

    def interrupt(self, deadline: float, what: str) -> parsers.Interrupt:
        """One EP 0x83 event before `deadline` (monotonic); timeout is a failure."""
        ms = int((deadline - time.monotonic()) * 1000)
        if ms <= 0:
            raise StepFailed(f"timeout waiting for {what}", EXIT_USB_ERROR)
        try:
            raw = bytes(self.dev.read(EP_INT, INT_READ_SIZE, timeout=ms))
        except self._usb_core.USBTimeoutError:
            raise StepFailed(f"timeout waiting for {what}", EXIT_USB_ERROR) from None
        ev = parsers.classify_interrupt(raw)
        self.log.write(
            "INT", f"EP 0x{EP_INT:02x} {len(raw)} byte(s): {raw.hex()} kind {ev.kind} code {ev.code} -> {ev.action}"
        )
        print(f"  event {raw.hex()} ({ev.action})")
        if ev.kind == 3 and ev.code == 7:
            self.log.write(
                "INFO",
                "event code 7 counts as 'ready' in PR #288 (bit 4 set); the one time it was seen on this unit "
                "(capture-20261013-231635-*.log, image 8) the 51 reply was a capture error",
            )
        self.block_events.append(raw.hex())
        return ev

    def drain_events(self, max_events: int = 16, quiet_ms: int = 100) -> list:
        """Read and log whatever is queued on EP 0x83 until it is quiet for `quiet_ms`.

        Used between images in collection mode: a late finger-up (or any other) event from the
        previous touch must not be mistaken for the next finger-down. The references document no
        finger-up event, so every payload found here is logged ("DRAIN") and kept in the manifest;
        a timeout here is the normal end of the drain, not an error.
        """
        found = []
        for _ in range(max_events):
            try:
                raw = bytes(self.dev.read(EP_INT, INT_READ_SIZE, timeout=quiet_ms))
            except self._usb_core.USBTimeoutError:
                break
            ev = parsers.classify_interrupt(raw)
            self.log.write("DRAIN", f"EP 0x{EP_INT:02x} {len(raw)} byte(s): {raw.hex()} kind {ev.kind} code {ev.code}")
            print(f"  stale event drained: {raw.hex()}")
            found.append(raw.hex())
        return found

    # -- steps -----------------------------------------------------------

    def preflight_rom(self, decoded: parsers.Decoded, inputs) -> None:
        """Identity checks on the 01 reply, before anything is written to the sensor."""
        if self.args.skip_identity_checks:
            say_warn(self.log, "identity checks skipped (--skip-identity-checks)")
            return
        v = decoded.values
        if (v.get("major"), v.get("minor")) != (6, 0x14):
            raise StepFailed(
                f"ROM {v.get('major')}/{v.get('minor')} is not 6/0x14, the version the module in the DLL is for ([PRIOR] headline 4); stopping",
                EXIT_BAD_INPUT,
            )
        reply_serial = v.get("serial", b"")
        if inputs.serial and reply_serial != inputs.serial:
            raise StepFailed(
                f"sensor serial {reply_serial.hex() or 'missing'} differs from the one in CalibrationData ({inputs.serial.hex()}); "
                "that calibration belongs to another unit ([CAL] section 4); stopping",
                EXIT_BAD_INPUT,
            )

    def prepare(self, plan: list[Step], inputs, force_module: bool, headings=("[1/3] queries", "[2/3] module")):
        """01, 19, 75 with the identity checks, then 06 unless a module is already loaded.

        Returns (decoded RomInfo, skip) where skip is True when 06 was not sent. Shared with
        fingerlift.py so both tools open a session the same way.
        """
        by_name = {s.name: s for s in plan}
        print(headings[0])
        rom = self.query(by_name["RomInfo"])
        self.preflight_rom(rom, inputs)
        self.query(by_name["query 0x19"])
        sid = self.query(by_name["sensor id"])
        if not self.args.skip_identity_checks and sid.values.get("sensor_type") != calibration.SENSOR_TYPE:
            raise StepFailed(f"sensor type {sid.values.get('sensor_type')} is not 0x2449; stopping", EXIT_BAD_INPUT)

        print(headings[1])
        skip, reason = module_decision(rom.values, force_module)
        self.log.write("MODULE", reason)
        print(f"  {reason}")
        if not skip:
            self.exchange(by_name["capture module"])
        return rom, skip

    def wait_finger(self, timeout_ms: int, prompt=None) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        if prompt is not None:
            prompt()
        print(f"  waiting up to {timeout_ms} ms for a finger on the sensor ...")
        while True:
            ev = self.interrupt(deadline, f"a finger-down event ({timeout_ms} ms)")
            if ev.action == "finger":
                return
            if ev.action == "fail":
                raise CaptureFailed(
                    f"capture event code {ev.code} (failed/aborted) before the finger event, lift and touch again",
                    kind="failed_event",
                    events=self.block_events,
                )

    def wait_ready(self, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            ev = self.interrupt(deadline, f"the image-ready event ({timeout_ms} ms)")
            if ev.action == "ready":
                return
            if ev.action == "fail":
                raise CaptureFailed(
                    f"capture event code {ev.code} (failed/aborted), lift and touch again",
                    kind="failed_event",
                    events=self.block_events,
                )
            if ev.action == "extend":
                deadline = time.monotonic() + timeout_ms / 1000

    def read_image(self, step: Step) -> int:
        """Single-image mode: fetch, save with a timestamped name, return the exit code."""
        kind, reply, img = self.fetch_image(step)
        if kind == "image":
            self.save_image(reply, img)
            self.image_done = True
        return EXIT_OK

    def fetch_image(self, step: Step):
        """51, then the reply on EP 0x81. Returns ("image", reply, ImageReply) or ("no_finger", reply, None).

        Evidence for the flow ([PV] = python-validity PR #288, iusearch main.py):
          * the image arrives as the reply to 51 on EP 0x81, 8082 bytes ([PV] sensor_2449.py:575-592, [TK]);
          * a short reply with status 1/2/5/6 and no image header means "no image yet" ([PV] sensor_2449.py:85
            lists 7 as well, but on this unit 7 came with a header whose error field was non-zero: a failed
            capture, see below);
          * a reply whose image header has a non-zero error field (status 0x0007, error 2 seen once), or the
            status 0x0412 ("no capture in progress", inferred), ends the capture: CaptureFailed is raised and
            51 is NOT re-sent. Collection mode restarts the image from 02; single-image mode stops;
          * iusearch (main.py:100-101) answers a 6-byte 02 00 00 00 00 00 with another READ (no re-send),
            PR #288 (sensor_2449.py:546-553) re-sends 51. The references disagree, so both are tried:
            a plain read first (1 s), then a re-send, up to --image-retries replies in total;
          * status 1 (bytes 01 00) is "No finger detected" in ToLiKNoSoV (synaptics_0078.c:270-274).
        """
        total = 1 + max(0, self.args.image_retries)
        short_ms = min(self.args.timeout_ms, 1000)
        need_write, replies = True, 0
        while True:
            if need_write:
                self.write(step.tx, self.args.timeout_ms)
                reply, ms = self.read(step.read_size, self.args.timeout_ms, "image")
            else:
                got = self.read_soft(step.read_size, short_ms)
                if got is None:
                    self.log.write(
                        "INFO", f"no further reply within {short_ms} ms; re-sending 51 ([PV] sensor_2449.py:546-553)"
                    )
                    print(f"  nothing more to read within {short_ms} ms; re-sending 51")
                    need_write = True
                    continue
                reply, ms = got
            replies += 1
            self.log.write("RX", f"EP 0x{EP_IN:02x} {len(reply)} byte(s) in {ms:.1f} ms: {reply.hex()}")
            img = parsers.parse_image_reply(reply)
            st = "n/a" if img.status is None else f"0x{img.status:04x}"
            print(f"  image reply: {len(reply)} byte(s), status {st}, {ms:.1f} ms")
            if img.pixels is not None:
                return "image", reply, img
            if img.status == parsers.IMAGE_STATUS_NO_FINGER and self.args.no_finger:
                self.save_raw_only(reply)
                msg = (
                    "RESULT: no image, as expected without a finger. The sensor answered 51 with status 0x0001 "
                    "(01 00 00 00 00 00 = no finger). The identify-mode capture program delivers an image only after a "
                    "finger is detected; nothing in the references takes a blank frame this way. The sequence 01 19 75 [06] 02 "
                    "51 04 52 itself worked. Run without --no-finger and touch the sensor to get an image."
                )
                self.log.write("RESULT", msg)
                print(f"  {msg}")
                return "no_finger", reply, None
            if img.kind == parsers.KIND_CAPTURE_ERROR:
                text, certainty, why = parsers.describe_capture_error(img.error or 0)
                self.log.write("WARN", f"51 reply is a capture error: {text} [{certainty}]; {why}; header {img.header}")
                if not self.args.label:
                    self.save_raw_only(reply)
                raise CaptureFailed(
                    f"bad capture ({text}), lift and touch again",
                    reply=reply,
                    kind=img.kind,
                    events=self.block_events,
                    detail=f"status {st}, {why}",
                )
            if img.kind == parsers.KIND_NO_CAPTURE:
                label, certainty, why = parsers.classify_stop_status(img.status)
                self.log.write("WARN", f"51 status {st}: {label} [{certainty}]; {why}")
                if not self.args.label:
                    self.save_raw_only(reply)
                raise CaptureFailed(
                    f"the sensor says there is no capture in progress (status {st}), lift and touch again",
                    reply=reply,
                    kind=img.kind,
                    events=self.block_events,
                    detail=f"status {st}, {label} [{certainty}]",
                )
            if img.kind == parsers.KIND_NOT_READY and replies < total:
                note = parsers.IMAGE_STATUS_NOTES.get(img.status, "a 'not ready' status in the references")
                self.log.write("WARN", f"51 status {st}: {note}; trying again ({replies}/{total - 1})")
                print(f"  status {st} is a 'not ready' state; trying again ({replies}/{total - 1})")
                need_write = False
                continue
            self.save_raw_only(reply)
            hint = ""
            if img.status == parsers.IMAGE_STATUS_NO_FINGER:
                hint = " (the sensor reports no finger: touch it before the image-ready event, or hold the finger on)"
            elif img.kind == parsers.KIND_NOT_READY:
                hint = f" (still 'not ready' after {replies} replies; --image-retries N allows more)"
            raise StepFailed("; ".join(img.problems) + hint, EXIT_BAD_REPLY)

    def read_soft(self, size: int, timeout_ms: int):
        """Like read(), but a timeout returns None (used only for the read-again step of 51)."""
        t0 = time.monotonic()
        try:
            reply = bytes(self.dev.read(EP_IN, size, timeout=timeout_ms))
        except self._usb_core.USBTimeoutError:
            return None
        return reply, (time.monotonic() - t0) * 1000

    def save_image(self, reply: bytes, img: parsers.ImageReply, paths=None) -> tuple:
        """Write the .pgm and .raw (exclusively: never over an existing file). Returns (pgm, raw)."""
        pgm, raw = paths or claim_paths(Path(self.args.out_dir), ("pgm", "raw"))
        with open(pgm, "wb" if paths is None else "xb") as fh:
            fh.write(parsers.pgm_bytes(img.pixels))
        with open(raw, "xb") as fh:
            fh.write(reply)
        say = f"image {parsers.IMAGE_WIDTH} x {parsers.IMAGE_HEIGHT}: {image_stats(img.pixels)}; header {img.header}"
        self.log.write("IMAGE", say)
        print(f"  {say}")
        print(f"  saved {pgm}")
        print(f"  saved {raw} (the complete 0x51 reply, {len(reply)} bytes)")
        self.log.write("IMAGE", f"saved {pgm} and {raw}")
        return pgm, raw

    def save_raw_only(self, reply: bytes) -> None:
        """Keep a reply that did not parse as an image, for diagnosis."""
        try:
            (raw,) = claim_paths(Path(self.args.out_dir), ("raw",))
            raw.write_bytes(reply)
            self.log.write("IMAGE", f"unparsed 0x51 reply ({len(reply)} bytes) saved to {raw}")
        except OSError as exc:
            self.log.write("WARN", f"could not save the raw reply: {exc!r}")

    def bell(self) -> None:
        if not self.args.no_bell:
            print("\a", end="", flush=True)

    def lift_wait(self, tag: str, headline: str, allow_redo: bool) -> str:
        """Ask the user to lift the finger. Returns "next" or "redo".

        No finger-up event is documented anywhere (python-validity, iusearch, ToLiKNoSoV and the
        ETW show finger-down type 2 and capture-event type 3 only), so this is a prompt, not a
        sensor signal. Interactive terminal: wait for Enter ("r" + Enter discards the image just
        taken and takes a replacement). Otherwise, or with --lift-pause-s, a logged fixed pause.
        """
        self.bell()
        pause = self.args.lift_pause_s
        if pause is None and _interactive():
            prompt = f"{tag} {headline}, then press Enter to continue"
            prompt += " (r + Enter: discard this image and retake it): " if allow_redo else ": "
            self.log.write("PROMPT", prompt.strip() + " [no finger-up event is known, so this waits for the user]")
            try:
                answer = _ask(prompt).strip().lower()
            except EOFError:
                answer = ""
                time.sleep(DEFAULT_LIFT_PAUSE_S)
            if allow_redo and answer.startswith("r"):
                self.log.write("INFO", "user asked to redo the last image")
                return "redo"
            return "next"
        pause = DEFAULT_LIFT_PAUSE_S if pause is None else pause
        msg = f"{tag} {headline} (fixed pause of {pause:g} s; no finger-up event is known)"
        self.log.write("INFO", msg)
        print(msg)
        time.sleep(pause)
        return "next"

    def between_images(self, done: int, total: int) -> str:
        return self.lift_wait(f"[{done}/{total}]", "lift your finger", True)

    def collect(self, plan: list[Step], info: dict) -> int:
        """--count N --label NAME [--append]: N more images in this one USB session.

        Per image the proven block is repeated: 02, wait for finger-down and image-ready, 51, then
        04 and 52. Evidence that 02 must be re-sent: iusearch main.py:105-109 re-sends it after
        every capture; PR #288 get_print() sends it at the start of every capture
        (sensor_2449.py:476-482); hardware confirmed 27 images this way. 06 is not repeated (once per
        session in PR #288; a second one is refused with 0x04aa). Between images the 04/52 cleanup has
        already run, the user is told to lift the finger, and EP 0x83 is drained so a late event cannot
        count as the next finger-down.

        A failed capture (error reply on 51, 0x0412, failed/aborted event) is not fatal: the block is
        ended with 04/52, the user is told to lift and touch again, and the same image restarts from 02,
        at most --max-attempts times per image; then the run gives up cleanly (saved images and manifest
        stay). Timeouts and bad statuses on commands still stop the run.
        """
        args = self.args
        total = args.count
        directory = Path(args.out_dir) / args.label
        if holds_collection(directory, args.label) and not args.append:
            raise StepFailed(
                f"{directory} already holds a collection; use another --label, or --append to add to it", EXIT_BAD_INPUT
            )
        directory.mkdir(parents=True, exist_ok=True)
        manifest = Manifest(directory, args.label, total, info, append=args.append)
        by_name = {s.name: s for s in plan}
        program = by_name["capture program"]
        image_step = next(s for s in plan if s.kind == "image")
        index = manifest.next_index()
        manifest.session["first_index"] = index
        if args.append and index > 1:
            print(f"  appending to {directory}: the next image is {args.label}-{index:02d}")
            self.log.write("INFO", f"append: continuing at image number {index}")
        accepted = 0
        failures = 0  # failed captures for the image currently being taken
        previous = None
        status = "incomplete"
        try:
            while accepted < total:
                n = accepted + 1
                print(f"\n=== image {n} of {total} -> {args.label}-{index:02d} ===")
                entry = {"index": index, "accepted": None}
                drained = self.drain_events()
                self.block_events = []
                self.image_done = False
                try:
                    self.exchange(program)

                    def touch(n=n):
                        self.bell()
                        msg = f"[{n}/{total}] touch the sensor now"
                        self.log.write("PROMPT", msg)
                        print(f"  >>> {msg}")

                    self.wait_finger(args.finger_timeout_ms, prompt=touch)
                    self.wait_ready(args.timeout_ms)
                    kind, reply, img = self.fetch_image(image_step)
                except CaptureFailed as exc:
                    failures += 1
                    record = {
                        "for_index": index,
                        "attempt": failures,
                        "timestamp": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        "kind": exc.kind,
                        "message": exc.user_message,
                        "detail": exc.detail,
                        "reply_hex": exc.reply.hex(),
                        "events": list(exc.events),
                        "drained_before": drained,
                    }
                    manifest.add_failure(record)
                    line = f"[{n}/{total}] {exc.user_message} (failed capture {failures} of {args.max_attempts} for this image)"
                    self.log.write("WARN", line)
                    print(f"  !!! {line}")
                    self.cleanup_commands(plan, image_done=False)  # 04, 52: ends this block cleanly
                    if failures >= args.max_attempts:
                        raise CaptureFailed(
                            f"giving up on image {n} of {total} after {failures} failed captures (last: {exc.user_message})",
                            reply=exc.reply,
                            kind="gave_up",
                        ) from exc
                    self.lift_wait(f"[{n}/{total}]", exc.user_message, False)
                    continue
                failures = 0
                pgm, raw = self.save_image(reply, img, paths=label_paths(directory, args.label, index))
                self.image_done = True
                q = parsers.image_quality(img.pixels)
                warnings = []
                if (
                    previous is not None
                    and parsers.mean_abs_diff(previous, img.pixels) < parsers.DUPLICATE_MEAN_ABS_DIFF
                ):
                    warnings.append(
                        "nearly identical to the previous take: the finger was probably not lifted (heuristic, UNCERTAIN)"
                    )
                entry.update(
                    pgm=pgm.name,
                    raw=raw.name,
                    timestamp=dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    stats=q,
                    events=list(self.block_events),
                    drained_before=drained,
                    reply_header=img.header,
                    warnings=warnings,
                )
                previous = img.pixels
                manifest.add(entry)
                line = f"[{n}/{total}] saved {pgm.name}: {parsers.format_quality(q)}"
                self.log.write("SUMMARY", line)
                print(f"  {line}")
                for w in warnings:
                    self.log.write("WARN", w)
                    print(f"  WARNING: {w}")
                statuses = self.cleanup_commands(plan, image_done=True)
                entry["status_04"] = None if statuses.get(OP_STOP) is None else f"0x{statuses[OP_STOP]:04x}"
                entry["status_52"] = None if statuses.get(OP_CLEANUP) is None else f"0x{statuses[OP_CLEANUP]:04x}"
                entry["accepted"] = True
                manifest.write()
                index += 1
                if self.between_images(n, total) == "redo":
                    entry["accepted"] = False
                    entry["rejected_by_user"] = True
                    manifest.write()
                else:
                    accepted += 1
            status = "complete"
        except BaseException as exc:
            status = f"incomplete after {accepted} accepted image(s): {type(exc).__name__}: {exc}"
            raise
        finally:
            manifest.finish(status)
            self.log.write("INFO", f"manifest {manifest.path}: {status}")
            print(f"\n  manifest: {manifest.path} ({status})")
        return EXIT_OK

    def cleanup_commands(self, plan: list[Step], image_done: bool = False) -> dict:
        """04 then 52, each best effort. Never raises over the real outcome.

        Returns {opcode: status or None}. The 04 status is classified (parsers.classify_stop_status):
        0x0412 after a delivered image is logged as "capture already finished (inferred)", not as an
        error. Afterwards the session is no longer armed.
        """
        statuses = {}
        try:
            for step in (s for s in plan if s.cleanup):
                try:
                    self.write(step.tx, min(self.args.timeout_ms, 2000))
                    reply, ms = self.read(step.read_size, min(self.args.timeout_ms, 2000), step.name)
                    self.log.write("RX", f"EP 0x{EP_IN:02x} {len(reply)} byte(s) in {ms:.1f} ms: {reply.hex()}")
                    st = parsers.status_word(reply)
                    statuses[step.tx[0]] = st
                    shown = "n/a" if st is None else f"0x{st:04x}"
                    if step.tx[0] == OP_STOP:
                        label, certainty, why = parsers.classify_stop_status(st, image_done)
                        kind = (
                            "INFO"
                            if st in (0, 0x0412) and label != "0x0412 without a delivered image (unexpected)"
                            else "WARN"
                        )
                        self.log.write(kind, f"cleanup {step.name}: status {shown} -> {label} [{certainty}]; {why}")
                        print(f"  cleanup {step.name}: status {shown}: {label} [{certainty}]")
                    else:
                        self.log.write("INFO", f"cleanup {step.name}: status {shown}")
                        print(f"  cleanup {step.name}: status {shown}")
                except BaseException as exc:  # noqa: BLE001 (KeyboardInterrupt too: finish cleaning up)
                    self.log.write("WARN", f"cleanup step {step.name!r} failed: {exc!r}")
                    if isinstance(exc, self._usb_core.USBError) and not isinstance(exc, self._usb_core.USBTimeoutError):
                        break  # device gone or wedged: stop talking to it
                    if isinstance(exc, KeyboardInterrupt):
                        break
        finally:
            self.armed = False
        return statuses


def say_warn(log, text):
    probe.say(log, "WARN", text)


def run(args) -> int:
    finger = not args.no_finger
    try:
        module, cmd02, inputs = load_inputs(Path(args.dll), Path(args.calibration))
    except (OSError, calibration.CalibrationError) as exc:
        print(f"cannot prepare the inputs: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    plan = build_plan(module, cmd02, finger=finger)
    if args.label and not args.dry_run:
        existing = Path(args.out_dir) / args.label
        if holds_collection(existing, args.label) and not args.append:
            print(
                f"{existing} already holds a collection; use another --label, or --append to add to it "
                "(nothing is overwritten)",
                file=sys.stderr,
            )
            return EXIT_BAD_INPUT
    if args.dry_run:
        print("dry run: no USB access. Full sequence, in order:")
        print(f"inputs: module {summarise(module, 0)}")
        print(f"        cmd02  {summarise(cmd02, 0)}")
        print(f"        calibration serial {inputs.serial.hex() or 'n/a'}")
        for line in describe_plan(plan):
            print(line)
        print(
            "Checks before step 4: ROM 6/0x14, sensor type 0x2449, serial in the 01 reply == serial in CalibrationData. "
            "Step 4 is skipped if the 01 reply shows a module already loaded (RomInfo byte 17 != 0, uncertain; --force-module overrides)."
        )
        print(
            "Never sent: 4f, 1a, any flash write/erase, pairing or partition command"
            + ("" if args.usb_reset else ", USB reset")
        )
        if args.label:
            print(
                f"Collection mode: steps 5-8 (02, wait for finger and ready, 51, then 04 and 52) are repeated for each of "
                f"{args.count} image(s) in this one session; 06 is not repeated. Before each 02 EP 0x83 is drained "
                "(stale events are logged); after each image the user is asked to lift the finger "
                "(no finger-up event is known: Enter on a terminal, else a fixed pause). A failed capture (51 error reply, "
                "0x0412, failed event) ends the block with 04/52 and restarts that image from 02, at most "
                f"{args.max_attempts} times per image. --append adds to an existing label."
            )
            print(f"Output: {args.out_dir}/{args.label}/{args.label}-01.pgm and .raw, ..., manifest.json")
        else:
            print(f"Output on success: {args.out_dir}/capture-<time>-<us>-p<pid>.pgm and .raw")
        return EXIT_OK

    # Imported late so --dry-run and the tests of the pure parts work without pyusb.
    import usb.core

    log = probe.ExchangeLog(Path(args.log_dir), prefix="capture")
    print(f"logging to {log.path}")
    log.write(
        "INFO", f"module sha256 {hashlib.sha256(module).hexdigest()}, cmd02 sha256 {hashlib.sha256(cmd02).hexdigest()}"
    )

    session = probe.UsbSession(log, detach_kernel_driver=args.detach_kernel_driver, usb_reset=args.usb_reset)
    sess = None
    rc = EXIT_OK
    try:
        failed = session.open()
        if failed is not None:
            return failed
        sess = Session(session.dev, log, args)
        by_name = {s.name: s for s in plan}

        rom, skip = sess.prepare(
            plan, inputs, args.force_module, headings=("[1/3] queries", "[2/3] module and capture program")
        )
        if not args.label:  # collection mode sends 02 itself, once per image
            sess.exchange(by_name["capture program"])

        print("[3/3] capture")
        if args.label:
            info = {
                "module_sha256": hashlib.sha256(module).hexdigest(),
                "cmd02_sha256": hashlib.sha256(cmd02).hexdigest(),
                "rom": f"{rom.values.get('major')}/{rom.values.get('minor')}",
                "rom_u1": rom.values.get("u1"),
                "module_sent": not skip,
                "log": log.path.name,
                "lift": "no finger-up event is known; prompt or fixed pause",
            }
            rc = sess.collect(plan, info)
        else:
            if finger:
                sess.wait_finger(args.finger_timeout_ms)
                sess.wait_ready(args.timeout_ms)
            rc = sess.read_image(next(s for s in plan if s.kind == "image"))
    except StepFailed as exc:
        probe.say(log, "ERROR", str(exc))
        rc = exc.rc
    except UnsafeCommand as exc:
        probe.say(log, "ERROR", f"refused to send: {exc}")
        rc = EXIT_CAPTURE_FAILED
    except KeyboardInterrupt:
        probe.say(log, "WARN", "interrupted; cleaning up")
        rc = EXIT_INTERRUPTED
    except usb.core.USBError as exc:
        probe.say(log, "ERROR", f"USB error: {exc!r}{probe.hint_for(exc)}")
        rc = EXIT_USB_ERROR
        if sess is not None and not isinstance(exc, usb.core.USBTimeoutError):
            sess.armed = False  # device gone or wedged: do not keep writing to it
    finally:
        try:
            if sess is not None and sess.armed:
                sess.cleanup_commands(plan, image_done=sess.image_done)
        finally:
            session.close()
            log.close()
            print(f"\nlog saved: {log.path}")
    return rc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="First image capture for Synaptics 06cb:0078. Writes a RAM module and a capture program to the sensor; "
        "never flash, pairing, 4f or 1a."
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the full byte sequence (long payloads summarised by length and hash) and exit; no USB",
    )
    ap.add_argument(
        "--no-finger",
        action="store_true",
        help="do not wait for a finger: send the image request right after 02 (blank frame)",
    )
    ap.add_argument(
        "--timeout-ms", type=int, default=5000, help="timeout per transfer and for the image-ready event (default 5000)"
    )
    ap.add_argument(
        "--finger-timeout-ms",
        type=int,
        default=20000,
        help="how long to wait for the finger-down event (default 20000)",
    )
    ap.add_argument(
        "--log-dir", default=str(DEFAULT_LOG_DIR), help="directory for the exchange log (default proto/logs)"
    )
    ap.add_argument(
        "--out-dir", default=str(DEFAULT_OUT_DIR), help="directory for the .pgm/.raw images (default proto/captures)"
    )
    ap.add_argument(
        "--dll",
        default=str(DEFAULT_DLL),
        help="vendor synaWudfBioUsb.dll holding the capture module (default ../driverstore/...)",
    )
    ap.add_argument(
        "--calibration",
        default=str(probe.DEFAULT_CALIBRATION),
        help="CalibrationData_decrypted.bin of THIS unit (default ../extracted/...)",
    )
    ap.add_argument(
        "--detach-kernel-driver",
        action="store_true",
        help="detach a kernel driver from interface 0 if present; re-attached on exit",
    )
    ap.add_argument(
        "--usb-reset",
        action="store_true",
        help="reset the USB device before claiming it (Windows does not; off by default)",
    )
    ap.add_argument(
        "--image-retries",
        type=int,
        default=3,
        metavar="N",
        help="after a 'not ready' status (1, 2, 5, 6, no image header) on 51, try up to N more replies (a plain read, then a re-send) before stopping (default 3)",
    )
    ap.add_argument(
        "--count",
        type=int,
        default=1,
        metavar="N",
        help="collection mode (needs --label): take N images in one USB session, one touch each (default 1)",
    )
    ap.add_argument(
        "--label",
        metavar="NAME",
        help="collection mode: save as <out-dir>/NAME/NAME-01.pgm (+ .raw), NAME-02..., plus manifest.json",
    )
    ap.add_argument(
        "--append",
        action="store_true",
        help="top up an existing --label: --count N adds N more images after the highest existing number, "
        "to the same folder and manifest",
    )
    ap.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        metavar="N",
        help="collection mode: failed captures (error reply, no capture in progress, failed event) allowed per image "
        "before giving up (default 3)",
    )
    ap.add_argument(
        "--lift-pause-s",
        type=float,
        default=None,
        metavar="S",
        help="between images wait a fixed S seconds instead of asking for Enter (default: Enter on a terminal, 3 s otherwise)",
    )
    ap.add_argument("--no-bell", action="store_true", help="do not ring the terminal bell at the prompts")
    ap.add_argument(
        "--force-module",
        action="store_true",
        help="send 06 + module even when the 01 reply suggests one is already loaded (the sensor then answers 0x04aa)",
    )
    ap.add_argument(
        "--skip-identity-checks",
        action="store_true",
        help="skip the ROM 6/0x14, sensor type 0x2449 and calibration serial checks (not recommended)",
    )
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    for name in ("timeout_ms", "finger_timeout_ms"):
        if getattr(args, name) <= 0:
            ap.error(f"--{name.replace('_', '-')} must be positive")
    if args.image_retries < 0:
        ap.error("--image-retries must be >= 0")
    if args.count < 1:
        ap.error("--count must be >= 1")
    if args.count > 1 and not args.label:
        ap.error("--count N > 1 needs --label NAME")
    if args.label is not None and not LABEL_RE.match(args.label):
        ap.error("--label may contain only letters, digits, '.', '_' and '-', and must start with a letter or digit")
    if args.label and args.no_finger:
        ap.error("--label/--count need a finger; they cannot be combined with --no-finger")
    if args.append and not args.label:
        ap.error("--append needs --label")
    if args.max_attempts < 1:
        ap.error("--max-attempts must be >= 1")
    if args.lift_pause_s is not None and args.lift_pause_s < 0:
        ap.error("--lift-pause-s must be >= 0")

    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
