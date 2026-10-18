# SPDX-License-Identifier: MIT
"""The guarded USB session and the Linux calibration sequence of the 06cb:0078 (variant L).

Factored out of proto/calibrate.py (B5a) so proto/calibrate.py and the setup tool run
the same code. proto/calibrate.py keeps its run directory, its result.json and its
terminal wording on top of it; see its docstring for the sequence and the exit codes.

Importing this module needs only the standard library: pyusb is imported inside
UsbTransport.open(). The transport is injectable (CalibrationSequence takes any object
with open/send/read/close and a `writes` count), and everything the sequence records
goes through three optional hooks:

  log.write(kind, text)   one log line (the 01 serial is already zeroed, never shown);
  save(name, data)        one output file (replies, EP 0x82 dumps, programs, cf.bin, bf.bin);
  say(text)               one short user-facing progress line (without it: a log line).

The guard (check_command) is the one gate on what may reach EP 0x01: a closed set of
exact forms, the module pin, the calibration-program skeleton pins, a single 06 per
session. Both UsbTransport.send() and CalibrationSequence.exchange() apply it, so a fake
transport cannot bypass it. 04 and 52 run on every exit path once a 02 write was
attempted (CalibrationSequence.cleanup), unless the reader is gone (ENODEV).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from . import calib_model as cm
from . import calibration, parsers, pins

VID, PID = 0x06CB, 0x0078
EP_OUT, EP_IN, EP_FRAME = 0x01, 0x81, 0x82
INTERFACE = 0
TIMEOUT_MS = 3000
FRAME_TIMEOUT_S = 5.0
# EP 0x82 read size: a multiple of 64 (51 224 is not, so the last read ends on a short
# packet). pyusb raises USBTimeoutError and drops whatever a timed-out transfer had
# already received, so a dump saved after a timeout can miss at most FRAME_CHUNK bytes
# (one read buffer) at its end; small reads keep that loss under 1 KiB.
FRAME_CHUNK = 1024
READ_SIZE = 1024
FACTORY_READ_SIZE = 16384
PROGRAM_READ_SIZE = 0x1000  # the identify program's reply buffer (ETW lines 508-511)
ROM_INFO_LEN = 38
EXPECTED_ROM = (6, 0x14)
STOP_OK = (0x0000, parsers.IMAGE_STATUS_NO_CAPTURE)  # 04/52: stopped, or nothing to stop (0x0412)

SERIAL_START, SERIAL_END = 0x12, 0x18  # RomInfo bytes 0x12-0x17 ([CAL] section 2)
REDACTED = "<redacted>"

OP_ROM_INFO, OP_QUERY_19, OP_SENSOR_ID = 0x01, 0x19, 0x75
OP_FACTORY_BITS, OP_MODULE, OP_PROGRAM = 0x6F, 0x06, 0x02
OP_STOP, OP_CLEANUP = 0x04, 0x52
ONE_BYTE_COMMANDS = frozenset({OP_ROM_INFO, OP_QUERY_19, OP_SENSOR_ID, OP_STOP, OP_CLEANUP})
REQ_FACTORY_BITS = parsers.REQ_FACTORY_BITS  # 6f 00 0e 00 00 00 00 00 00
# Opcodes no program of this project sends (proto/capture.py's NEVER_SEND is this dict).
NEVER_SEND = {
    0x4F: "flash partition / pairing command",
    0x1A: "calibration commit (writes flash)",
    0x50: "pairing step",
    0x3E: "flash info query (outside the identify sequence)",
    0x08: "register write (not in the Windows ETW sequence)",
    0x07: "register read (not in the Windows ETW sequence)",
}
# Named so a refusal says why. Not exhaustive: the allow-list in check_command is the gate.
NAMED_REFUSALS = {
    **NEVER_SEND,
    0x1A: "module clear before a module swap in the DLL (job 8, 0x180080c00); python-validity's call_cleanups, "
    "which commits pending writes on flash sensors",
    0x3F: "flash erase (python-validity flash.py erase_flash; not cited elsewhere in this repo)",
    0x40: "flash read (python-validity flash.py read_flash; not cited elsewhere in this repo)",
    0x41: "flash write (python-validity flash.py write_flash; not cited elsewhere in this repo)",
    0x51: "image request (not part of the calibration)",
}

EXIT_OK, EXIT_USB_ERROR, EXIT_NOT_FOUND, EXIT_DRIVER_BUSY, EXIT_BAD_REPLY = 0, 1, 2, 3, 4
EXIT_BAD_INPUT, EXIT_FAIL, EXIT_REFUSED, EXIT_UNEXPECTED, EXIT_INTERRUPTED = 5, 6, 7, 8, 130
ENODEV = 19  # the only USB error after which nothing more is written to the reader
EXPECTED_BYTE17_AFTER_MODULE = 0x0E  # this unit after a 06 (parsers.module_probably_loaded)

UDEV_RULE = "/etc/udev/rules.d/70-fingerprint-06cb-0078.rules"
NODE_UNKNOWN = "/dev/bus/usb/BBB/DDD"
MSG_BUSY = (
    "The reader is in use by fprintd or another program. Close any fingerprint prompt (lock screen, settings), "
    "wait a moment and run again. If it persists: sudo systemctl stop fprintd (it starts again on demand)."
)
MSG_PERMISSION = (
    "No permission to use the reader. Check it with: getfacl {node} (your user should be listed with rw).\n"
    "Re-plug the reader, or log out and in again, then run the same command again.\n"
    f"If the udev rule {UDEV_RULE} was just added, load it first:\n"
    "  sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usb --action=add"
)
MSG_NOT_FOUND = (
    "The fingerprint reader (06cb:0078) was not found. Check that it is connected and enabled, then run again."
)
MSG_REMOVED = "The reader was disconnected. Check it, then run the same command again."
MSG_DEVICE = "The reader did not answer as expected, so this run stops here (details in the log)."
MSG_FRAMES = (
    "The reader did not deliver the calibration capture as expected, so this run stops here (details in the log)."
)
MSG_PYUSB = "pyusb is not installed in this Python (see proto/requirements.txt)."
MSG_LIBUSB = "libusb-1.0 is not available."


class UnsafeCommand(Exception):
    """Raised before the wire is touched when a write would leave the allow-list.

    The base of every guard refusal in this project (proto/capture.py's UnsafeCommand is
    this class).
    """


class CalibrationUnsafeCommand(UnsafeCommand):
    """Raised before the wire is touched when a write is not one of the allowed forms."""


class StepFailed(Exception):
    """The run stops. `detail` goes to the log, `user` to the terminal, `rc` is the exit code."""

    def __init__(self, detail: str, rc: int, user: str = MSG_DEVICE, reason: str = "the reader did not answer"):
        super().__init__(detail)
        self.rc, self.user, self.reason = rc, user, reason  # reason: a short phrase, e.g. "the reader is busy"


class TransferTimeout(Exception):
    """A USB transfer timed out (translated from pyusb by UsbTransport)."""


class DeviceError(Exception):
    """Any other USB error (translated from pyusb by UsbTransport)."""

    def __init__(self, text: str, errno: int | None):
        super().__init__(text)
        self.errno = errno


@dataclass
class SessionState:
    """What check_command needs to know about the session."""

    rom_u1: int | None = None  # RomInfo byte 17 of this session's parsed 01 reply
    module_sent: bool = False  # a 06 write was attempted
    armed: bool = False  # a 02 write was attempted and 04/52 have not completed since


class NullLog:
    """A log that drops every line (the default sink)."""

    def write(self, kind: str, text: str) -> None:
        pass


def redact_rom_info(reply: bytes) -> bytes:
    """The 01 reply with the unit serial (bytes 0x12-0x17) zeroed."""
    out = bytearray(reply)
    end = min(len(out), SERIAL_END)
    if end > SERIAL_START:
        out[SERIAL_START:end] = bytes(end - SERIAL_START)
    return bytes(out)


def check_command(
    data: bytes,
    state: SessionState,
    *,
    module_sha256: str = pins.MODULE_SHA256,
    module_len: int = pins.MODULE_LEN,
    skeletons: dict[int, str] | None = None,
) -> None:
    """Refuse every write that is not exactly one of these forms (raises CalibrationUnsafeCommand):

    01, 19, 75, 04, 52   exactly 1 byte;
    6f                   exactly 6f 00 0e 00 00 00 00 00 00 (factory bits, tag 0x0e);
    06 + 10500 bytes     sha256 == the module pin, only when this session's parsed
                         RomInfo byte 17 == 0 (no module loaded) and no 06 was sent yet;
    02                   1917 or 10269 bytes whose calib_model.skeleton() sha256 is the
                         pinned one for that length (calibration programs #1 / #2 only);
                         this pins program #1's all-zero key line and the 0x43 format and
                         reserved bytes, and skeleton() refuses two different 0x43 copies.

    `skeletons` ({length: skeleton sha256}) defaults to the pins in pins.py; the pins are
    arguments so a test can pass synthetic ones. Pure: it never changes `state`.
    """
    if skeletons is None:
        skeletons = pins.DEFAULT_PINS.skeletons
    if not data:
        raise CalibrationUnsafeCommand("empty write")
    op = data[0]
    n = len(data)
    if op in ONE_BYTE_COMMANDS:
        if n != 1:
            raise CalibrationUnsafeCommand(f"0x{op:02x} must be 1 byte, not {n}")
        return
    if op == OP_FACTORY_BITS:
        if bytes(data) != REQ_FACTORY_BITS:
            raise CalibrationUnsafeCommand("6f is allowed only as 6f 00 0e 00 00 00 00 00 00")
        return
    if op == OP_MODULE:
        if n != 1 + module_len:
            raise CalibrationUnsafeCommand(f"06 must carry {module_len} module bytes, not {n - 1}")
        if hashlib.sha256(bytes(data[1:])).hexdigest() != module_sha256:
            raise CalibrationUnsafeCommand("06 module bytes do not have the pinned sha256")
        if state.rom_u1 is None:
            raise CalibrationUnsafeCommand("06 before this session's RomInfo was parsed")
        if state.rom_u1 != 0:
            raise CalibrationUnsafeCommand(f"06 while RomInfo byte 17 = 0x{state.rom_u1:02x} (a module is loaded)")
        if state.module_sent:
            raise CalibrationUnsafeCommand("a second 06 in one session")
        return
    if op == OP_PROGRAM:
        want = skeletons.get(n)
        if want is None:
            raise CalibrationUnsafeCommand(f"02 of {n} bytes is not a calibration program ({sorted(skeletons)})")
        try:
            got = cm.skeleton_sha256(bytes(data))
        except calibration.CalibrationError as exc:
            raise CalibrationUnsafeCommand(f"02 is not a valid calibration program: {exc}") from None
        if got != want:
            raise CalibrationUnsafeCommand(f"02 of {n} bytes: skeleton sha256 {got} is not the pinned {want}")
        return
    if op in NAMED_REFUSALS:
        raise CalibrationUnsafeCommand(f"opcode 0x{op:02x} is never sent: {NAMED_REFUSALS[op]}")
    raise CalibrationUnsafeCommand(f"opcode 0x{op:02x} is not on the allow-list")


def describe_tx(data: bytes) -> str:
    if len(data) <= 64:
        return data.hex()
    return f"{len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}"


# --------------------------------------------------------------- transport


class UsbTransport:
    """Quiet USB transport modelled on probe.UsbSession: everything goes to the log, nothing to the terminal.

    open() raises StepFailed with the user-facing message. close() finishes every step
    and raises nothing, except a KeyboardInterrupt it caught, re-raised after the last step.
    send() is the only path to EP 0x01 and applies `guard` (default check_command with the
    pins of pins.py) first; `writes` counts its write attempts.
    """

    def __init__(self, log=None, *, guard=None):
        self.log = log if log is not None else NullLog()
        self.guard = guard if guard is not None else check_command
        self.dev = None
        self.claimed = False
        self._core = None
        self.node = NODE_UNKNOWN
        self.writes = 0

    def usb_failure(self, exc) -> StepFailed:
        """The StepFailed (exit code, user message, reason) for a USB error, by its errno."""
        errno = getattr(exc, "errno", None)
        if errno in (1, 13):
            return StepFailed(
                f"permission denied: {exc!r}", EXIT_USB_ERROR, MSG_PERMISSION.format(node=self.node), "no permission"
            )
        if errno == 16:
            return StepFailed(f"interface busy: {exc!r}", EXIT_DRIVER_BUSY, MSG_BUSY, "the reader is busy")
        if errno == ENODEV:
            return StepFailed(f"device gone: {exc!r}", EXIT_USB_ERROR, MSG_REMOVED, "the reader was disconnected")
        return StepFailed(f"USB error: {exc!r}", EXIT_USB_ERROR, MSG_DEVICE, "USB error")

    _usb_failure = usb_failure  # the name proto/calibrate.py has always used

    def open(self) -> None:
        # Imported late: importing this module and the pure parts never import pyusb.
        try:
            import usb.backend.libusb1
            import usb.core
            import usb.util
        except ImportError as exc:
            raise StepFailed(f"pyusb not importable: {exc!r}", EXIT_USB_ERROR, MSG_PYUSB, "pyusb is missing") from None

        self._core = usb.core
        backend = usb.backend.libusb1.get_backend()
        if backend is None:
            raise StepFailed("libusb1 backend not available", EXIT_USB_ERROR, MSG_LIBUSB, "libusb-1.0 is missing")
        try:
            found = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=backend))
        except usb.core.USBError as exc:
            raise self.usb_failure(exc) from None
        if not found:
            raise StepFailed(f"device {VID:04x}:{PID:04x} not found", EXIT_NOT_FOUND, MSG_NOT_FOUND, "reader not found")
        if len(found) > 1:
            raise StepFailed(
                f"{len(found)} devices match {VID:04x}:{PID:04x}; refusing to guess",
                EXIT_NOT_FOUND,
                MSG_DEVICE,
                "more than one reader found",
            )
        self.dev = dev = found[0]
        try:
            self.node = f"/dev/bus/usb/{int(dev.bus):03d}/{int(dev.address):03d}"
        except (TypeError, ValueError):
            self.node = NODE_UNKNOWN
        self.log.write("INFO", f"found {VID:04x}:{PID:04x} at {self.node}")
        try:
            try:
                cfg = dev.get_active_configuration()
                self.log.write("INFO", f"active configuration {cfg.bConfigurationValue}; leaving it alone")
            except usb.core.USBError as exc:
                if "Configuration not set" not in str(exc):
                    raise
                self.log.write("WARN", "device is unconfigured: calling set_configuration() once")
                dev.set_configuration()
            try:
                active = dev.is_kernel_driver_active(INTERFACE)
            except NotImplementedError:
                active = False
            if active:
                raise StepFailed(
                    "a kernel driver is attached to interface 0", EXIT_DRIVER_BUSY, MSG_BUSY, "the reader is busy"
                )
            usb.util.claim_interface(dev, INTERFACE)
            self.claimed = True
            self.log.write("INFO", f"claimed interface {INTERFACE}")
        except usb.core.USBError as exc:
            raise self.usb_failure(exc) from None

    def send(self, data: bytes, state: SessionState) -> None:
        """The one send function: the guard, then the bulk OUT write."""
        data = bytes(data)
        self.guard(data, state)
        if data[0] == OP_MODULE:
            state.module_sent = True
        elif data[0] == OP_PROGRAM:
            state.armed = True  # even a partial write may have started the program
        self.log.write("TX", f"EP 0x{EP_OUT:02x} {len(data)} byte(s): {describe_tx(data)}")
        self.writes += 1
        try:
            written = self.dev.write(EP_OUT, data, timeout=TIMEOUT_MS)
        except self._core.USBTimeoutError:
            raise TransferTimeout(f"write of 0x{data[0]:02x} timed out after {TIMEOUT_MS} ms") from None
        except self._core.USBError as exc:
            raise DeviceError(repr(exc), getattr(exc, "errno", None)) from None
        if written != len(data):
            raise StepFailed(f"short write: {written} of {len(data)} bytes", EXIT_USB_ERROR)

    def read(self, ep: int, size: int, timeout_ms: int) -> bytes:
        """One bulk IN read. Logs nothing about the content (CalibrationSequence.record_reply does)."""
        try:
            return bytes(self.dev.read(ep, size, timeout=timeout_ms))
        except self._core.USBTimeoutError:
            raise TransferTimeout(f"read on EP 0x{ep:02x} timed out after {timeout_ms} ms") from None
        except self._core.USBError as exc:
            raise DeviceError(repr(exc), getattr(exc, "errno", None)) from None

    def close(self) -> None:
        if self.dev is None:
            return
        try:
            import usb.util
        except ImportError:
            self.dev = None
            return
        interrupted = None
        if self.claimed:
            try:
                usb.util.release_interface(self.dev, INTERFACE)
                self.log.write("INFO", f"released interface {INTERFACE}")
            except KeyboardInterrupt as exc:
                interrupted = exc
                self.log.write("WARN", "interrupted during release_interface; still disposing")
            except Exception as exc:  # noqa: BLE001
                self.log.write("WARN", f"release_interface failed: {exc!r}")
            self.claimed = False
        try:
            usb.util.dispose_resources(self.dev)
            self.log.write("INFO", "resources disposed")
        except KeyboardInterrupt as exc:
            interrupted = exc
            self.log.write("WARN", "interrupted during dispose_resources")
        except Exception as exc:  # noqa: BLE001
            self.log.write("WARN", f"dispose_resources failed: {exc!r}")
        self.dev = None
        if interrupted is not None:
            raise interrupted


# ---------------------------------------------------------------- sequence


@dataclass
class SequenceResult:
    """What one calibration sequence measured. `bf` is None when the check score failed.

    `serial` is the unit's RomInfo serial (bytes 0x12-0x17, raw): kept out of repr so a
    traceback or a log of the result never shows it.
    """

    factory: bytes  # the 56 factory values (6f subtag 3)
    check_reference: bytes  # 6f subtag 12, the 724 B Windows checks CF against
    cf: bytes  # calibration frame, 56 x 152
    score: int  # Windows' check score of CF; passes when <= cm.CHECK_MAX
    bf: bytes | None  # baseline frame (decoded), 56 x 152; None when program #2 was not run
    rom: tuple[int, int]  # ROM major, minor
    rom_byte17: int | None  # RomInfo byte 17 before the session (0: no module loaded)
    module_uploaded: bool  # a 06 was sent in this session
    rom_byte17_after_module: int | None = None  # after a 06 (0x0e expected), information only
    serial: bytes = field(default=b"", repr=False)

    @property
    def score_ok(self) -> bool:
        return self.score <= cm.CHECK_MAX

    def chunks(self) -> calibration.UnitChunks:
        """The four per-unit regions of the identify program (needs bf)."""
        if self.bf is None:
            raise calibration.CalibrationError("no baseline frame: program #2 was not run")
        return cm.derive_chunks(self.factory, self.cf, self.bf)


class CalibrationSequence:
    """01, 19, 75, 6f, the module if none is loaded, then the two blank captures.

    calibrate() returns a SequenceResult; on a check score above cm.CHECK_MAX it stops after
    capture 1 (bf None). Errors raise StepFailed, CalibrationUnsafeCommand, TransferTimeout,
    DeviceError or CalibrationError; the caller then calls cleanup() (04/52 if armed) and
    closes the transport. `info` collects the JSON-able facts of the run (proto/calibrate.py
    writes it into result.json).
    """

    def __init__(
        self, transport, log=None, *, template: bytes, module: bytes, guard=None, save=None, say=None, clock=None
    ):
        self.t = transport
        self.log = log if log is not None else NullLog()
        self.template, self.module = template, module
        self.guard = guard if guard is not None else check_command
        self._save, self._say = save, say
        self.clock = clock if clock is not None else time.monotonic
        self.state = SessionState()
        self.seq = 0
        self.device_gone = False
        self.info: dict = {}
        self._rom_serial: bytes | None = None
        self._rom: tuple[int, int] | None = None

    @property
    def rom_serial(self) -> bytes | None:
        """The raw serial bytes of the RomInfo (01) reply (0x12..0x17), None before it was read.

        Read-only. The unit serial: compare it, never log or print it.
        """
        return self._rom_serial

    # -- hooks --------------------------------------------------------------

    def save(self, name: str, data: bytes) -> None:
        if self._save is not None:
            self._save(name, data)

    def say(self, text: str) -> None:
        if self._say is not None:
            self._say(text)
        else:
            self.log.write("STEP", text)

    # -- I/O ----------------------------------------------------------------

    def record_reply(self, op: int, reply: bytes, what: str) -> None:
        """The one place a reply enters the log and the saved files; the 01 serial is zeroed here."""
        self.seq += 1
        safe = redact_rom_info(reply) if op == OP_ROM_INFO else reply
        note = f" (bytes 0x{SERIAL_START:02x}-0x{SERIAL_END - 1:02x} = serial {REDACTED})" if op == OP_ROM_INFO else ""
        self.log.write("RX", f"EP 0x{EP_IN:02x} {len(reply)} byte(s) for {what}{note}: {safe.hex()}")
        self.save(f"reply-{self.seq:02d}-{op:02x}.bin", safe)

    def exchange(self, data: bytes, read_size: int, what: str) -> bytes:
        self.guard(bytes(data), self.state)  # also here, whatever transport was injected
        self.t.send(data, self.state)
        try:
            reply = self.t.read(EP_IN, read_size, TIMEOUT_MS)
        except TransferTimeout as exc:
            raise StepFailed(f"{what}: {exc}", EXIT_USB_ERROR) from None
        self.record_reply(data[0], reply, what)
        return reply

    def require_status(self, reply: bytes, what: str, allowed=(0,)) -> int:
        st = parsers.status_word(reply)
        if st not in allowed:
            shown = "missing" if st is None else f"0x{st:04x}"
            raise StepFailed(f"{what}: status {shown}, expected {[f'0x{a:04x}' for a in allowed]}", EXIT_BAD_REPLY)
        return st

    def read_frames(self, n: int) -> bytes:
        """EP 0x82 until exactly 51 224 bytes, 5 s overall. The dump is saved whatever happens."""
        buf = bytearray()
        deadline = self.clock() + FRAME_TIMEOUT_S
        reads = 0
        try:
            while len(buf) < cm.RAW_LEN:
                left_ms = int((deadline - self.clock()) * 1000)
                if left_ms <= 0:
                    raise StepFailed(
                        f"EP 0x82 capture {n}: {len(buf)} of {cm.RAW_LEN} bytes within {FRAME_TIMEOUT_S:g} s",
                        EXIT_USB_ERROR,
                        MSG_FRAMES,
                    )
                try:
                    chunk = self.t.read(EP_FRAME, FRAME_CHUNK, left_ms)
                except TransferTimeout:
                    raise StepFailed(
                        f"EP 0x82 capture {n}: timeout after {len(buf)} of {cm.RAW_LEN} bytes",
                        EXIT_USB_ERROR,
                        MSG_FRAMES,
                    ) from None
                reads += 1
                buf += chunk
            if len(buf) != cm.RAW_LEN:
                raise StepFailed(
                    f"EP 0x82 capture {n}: {len(buf)} bytes, more than the expected {cm.RAW_LEN}",
                    EXIT_BAD_REPLY,
                    MSG_FRAMES,
                )
            return bytes(buf)
        finally:
            self.log.write("RX", f"EP 0x{EP_FRAME:02x} capture {n}: {len(buf)} byte(s) in {reads} read(s)")
            self.save(f"ep82-{n}.bin", bytes(buf))

    def stop_capture(self, strict: bool) -> dict:
        """04 then 52. strict: both must answer 0 or 0x0412. Otherwise best effort, never raises."""
        statuses = {}
        for op, name in ((OP_STOP, "stop capture (04)"), (OP_CLEANUP, "cleanup (52)")):
            if not strict:
                try:
                    reply = self.exchange(bytes([op]), READ_SIZE, name)
                except BaseException as exc:  # noqa: BLE001 (Ctrl-C too: the real outcome is already decided)
                    self.log.write("WARN", f"cleanup {name} failed: {exc!r}")
                    if isinstance(exc, KeyboardInterrupt) or (isinstance(exc, DeviceError) and exc.errno == ENODEV):
                        break
                    continue
            else:
                reply = self.exchange(bytes([op]), READ_SIZE, name)
            st = parsers.status_word(reply)
            statuses[f"{op:02x}"] = None if st is None else f"0x{st:04x}"
            label, certainty, _ = parsers.classify_stop_status(st)
            self.log.write("INFO", f"{name}: status {statuses[f'{op:02x}']} -> {label} [{certainty}]")
            if strict:
                self.require_status(reply, name, STOP_OK)
        self.state.armed = False
        return statuses

    def cleanup(self) -> None:
        """Exit-path cleanup: 04/52 if a 02 write was attempted and not yet stopped."""
        if self.state.armed and not self.device_gone:
            self.log.write("INFO", "exit-path cleanup: 04, 52")
            self.stop_capture(strict=False)
        self.state.armed = False

    # -- steps --------------------------------------------------------------

    def capture(self, n: int, program: bytes) -> bytes:
        self.save(f"program{n}.bin", program)
        self.log.write("INFO", f"program #{n}: {len(program)} bytes, sha256 {hashlib.sha256(program).hexdigest()}")
        reply = self.exchange(program, PROGRAM_READ_SIZE, f"program #{n}")
        self.require_status(reply, f"program #{n}")
        self.info.setdefault("program_reply_sizes", []).append(len(reply))
        self.log.write("INFO", f"program #{n} reply: {len(reply)} bytes, status 0")
        raw = self.read_frames(n)
        self.info.setdefault("stop_statuses", []).append(self.stop_capture(strict=True))
        return raw

    def recheck_rom_info(self) -> int | None:
        """01 again after a 06: byte 17 is logged (0x0e expected). Information only, never a gate.

        The reply goes through record_reply like the first one, so its serial is zeroed.
        """
        rom = self.exchange(bytes([OP_ROM_INFO]), READ_SIZE, "RomInfo after the module (01)")
        u1 = parsers.parse_rom_info(rom).values.get("u1")
        self.info["rom_byte17_after_module"] = u1
        if u1 is None:
            self.log.write(
                "WARN", f"RomInfo after the module: {len(rom)} bytes, byte 17 not decoded (information only)"
            )
            return None
        note = (
            "as expected"
            if u1 == EXPECTED_BYTE17_AFTER_MODULE
            else f"expected 0x{EXPECTED_BYTE17_AFTER_MODULE:02x}; information only"
        )
        self.log.write("INFO", f"RomInfo after the module: byte 17 0x{u1:02x} ({note}), serial {REDACTED}")
        return u1

    def read_identity(self) -> tuple[bytes, bytes]:
        """01, 19, 75, 6f. Returns (56 factory values, subtag 12)."""
        rom = self.exchange(bytes([OP_ROM_INFO]), READ_SIZE, "RomInfo (01)")
        if len(rom) != ROM_INFO_LEN:
            raise StepFailed(f"RomInfo reply is {len(rom)} bytes, expected {ROM_INFO_LEN}", EXIT_BAD_REPLY)
        self.require_status(rom, "RomInfo (01)")
        d = parsers.parse_rom_info(rom)
        major, minor, u1 = d.values.get("major"), d.values.get("minor"), d.values.get("u1")
        self._rom_serial = bytes(rom[SERIAL_START:SERIAL_END])
        # Only these fields are logged; the decoded view also holds the serial.
        self.log.write(
            "INFO",
            f"RomInfo: ROM {major}/0x{minor:02x}, product 0x{d.values.get('product', 0):02x}, byte 17 0x{u1:02x}, serial {REDACTED}",
        )
        if (major, minor) != EXPECTED_ROM:
            raise StepFailed(f"ROM {major}/0x{minor:02x} is not 6/0x14", EXIT_BAD_REPLY)
        self.state.rom_u1 = u1
        self.info.update(rom=f"{major}/0x{minor:02x}", rom_byte17=u1)
        self._rom = (major, minor)

        q19 = self.exchange(bytes([OP_QUERY_19]), READ_SIZE, "query (19)")
        self.require_status(q19, "query (19)")
        sid = self.exchange(bytes([OP_SENSOR_ID]), READ_SIZE, "sensor id (75)")
        self.require_status(sid, "sensor id (75)")
        stype = parsers.parse_sensor_id(sid).values.get("sensor_type")
        if stype != calibration.SENSOR_TYPE:
            raise StepFailed(f"sensor type {stype} is not 0x2449", EXIT_BAD_REPLY)

        fb = self.exchange(REQ_FACTORY_BITS, FACTORY_READ_SIZE, "factory bits (6f)")
        self.require_status(fb, "factory bits (6f)")
        dec = parsers.parse_factory_bits(fb)
        by_subtag = dec.values.get("by_subtag", {})
        self.log.write("INFO", f"6f: {len(fb)} bytes, subtags {sorted(by_subtag)}, warnings {dec.warnings}")
        try:
            factory = cm.factory_values(by_subtag)
            sub12 = cm.check_reference(by_subtag)
            cm.factory_entries(factory)  # must pack to exactly 5 bits
        except calibration.CalibrationError as exc:
            raise StepFailed(f"factory bits: {exc}", EXIT_BAD_REPLY) from None
        return factory, sub12

    def calibrate(self) -> SequenceResult:
        """The whole sequence; see the class docstring."""
        self.say("Reading the reader's factory values…")
        factory, sub12 = self.read_identity()
        program1 = cm.build_program1(self.template, factory)

        loaded, why = parsers.module_probably_loaded(self.state.rom_u1)
        self.log.write("MODULE", ("skipping 06: " if loaded else "sending 06: ") + why)
        self.info["module_sent"] = not loaded
        after = None
        if not loaded:
            self.say("Loading the capture module…")
            reply = self.exchange(bytes([OP_MODULE]) + self.module, READ_SIZE, "module (06)")
            if len(reply) != 2:
                raise StepFailed(f"module reply is {len(reply)} bytes, expected 2", EXIT_BAD_REPLY)
            self.require_status(reply, "module (06)")
            after = self.recheck_rom_info()

        self.say("Calibration capture 1 of 2…")
        raw1 = self.capture(1, program1)
        cf = cm.average_frame2(raw1)
        self.save("cf.bin", cf)
        score = cm.check_score(cf, sub12)
        self.info.update(score=score, score_max=cm.CHECK_MAX, header_rows_ok={"cf": cm.header_rows_ok(cf)})
        self.log.write("INFO", f"check score {score} (limit {cm.CHECK_MAX}; Windows scored 7 on this unit)")
        self.log.write("INFO", f"CF header byte 2 == 2r+1 on {cm.header_rows_ok(cf)} of {cm.ROWS} rows (information)")
        result = SequenceResult(
            factory=factory,
            check_reference=sub12,
            cf=cf,
            score=score,
            bf=None,
            rom=self._rom,
            rom_byte17=self.state.rom_u1,
            module_uploaded=not loaded,
            rom_byte17_after_module=after,
            serial=self._rom_serial or b"",
        )
        if score > cm.CHECK_MAX:
            return result

        self.say("Calibration capture 2 of 2…")
        raw2 = self.capture(2, cm.build_program2(self.template, factory, cf))
        bf = cm.decode_frame(cm.average_frame2(raw2))
        self.save("bf.bin", bf)
        self.info["header_rows_ok"]["bf"] = cm.header_rows_ok(bf)
        self.log.write("INFO", f"BF header byte 2 == 2r+1 on {cm.header_rows_ok(bf)} of {cm.ROWS} rows (information)")
        result.bf = bf
        return result
