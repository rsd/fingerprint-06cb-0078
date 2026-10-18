#!/usr/bin/env python3
"""B2 one-shot calibration experiment for the Synaptics 06cb:0078 (sensor type 0x2449).

Runs Windows' calibration (research/steps/b2-calibration-source.md, variant L) from
Linux once and checks the result: two blank (no-finger) captures, their averaged
frames, Windows' own acceptance score, and a comparison with the CalibrationData
Windows stored for this unit when it is available. RAM only: a capture module (if
none is loaded) and two capture programs. No flash, no 1a, no 4f/50, no 0x48 chunk.

Without --run this is a dry run: it opens no USB device, never imports pyusb, and
prints the exact command sequence with lengths and sha256s.

Sequence on --run (each command: write EP 0x01, read the reply on EP 0x81, 3 s):
   01                RomInfo, 38 B, ROM 6/0x14; byte 17 = loaded-module id
   19                query, status 0
   75                sensor id, type 0x2449
   6f 00 0e 00 ..    factory bits: subtag 3 (56 factory values), subtag 12 (check rows)
   06 + module       only when RomInfo byte 17 == 0 (the driver's rule), 2 B status 0
   01                only after a 06: RomInfo again, byte 17 logged (0x0e expected); information only
   02 program #1     1917 B; then 51 224 B on EP 0x82 (5 s); 04, 52 -> CF, check score
   02 program #2     10 269 B, CF applied; then 51 224 B on EP 0x82; 04, 52 -> BF
The four reads (01, 19, 75, 6f) all come before the first write that changes state,
as in the DLL (scsCalibrate reads 6f before the module is requested), so a missing
subtag or unpackable factory values stop the run before anything is uploaded.

Safety in code: Transport.send() is the only path to EP 0x01 and calls
check_command() first; see its docstring for the closed set of allowed commands.
04 and 52 run on every exit path once a 02 write was attempted, unless the reader is
gone (ENODEV). Ctrl-C/SIGTERM exit 130 after that cleanup, at any point of the run.

Output: local/calib/<YYYYmmdd-HHMMSS>/ (0700; files 0600, never overwritten): every
reply, both EP 0x82 dumps, both programs as sent, cf.bin, bf.bin, the derived
per-unit chunks, result.json and calibrate.log. The unit serial (RomInfo bytes
0x12-0x17) is never printed, logged or written: it is zeroed where the 01 reply
enters the log/save path (Experiment.record_reply).

Exit codes: 0 PASS (or dry run), 1 USB error/timeout, 2 reader not found, 3 reader
busy, 4 unexpected reply, 5 bad or missing input, 6 FAIL, 7 command refused by the
guard, 8 unexpected error in this program (a bug: traceback in calibrate.log only),
130 interrupted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import calib_model as cm  # an alias of synaptics0078.calib_model; it puts the repo root on sys.path
import calibration
import capture

from synaptics0078 import session
from synaptics0078.session import (  # noqa: F401  (this program's public names, as before B5a)
    ENODEV,
    EP_FRAME,
    EP_IN,
    EP_OUT,
    EXIT_BAD_INPUT,
    EXIT_BAD_REPLY,
    EXIT_DRIVER_BUSY,
    EXIT_FAIL,
    EXIT_INTERRUPTED,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNEXPECTED,
    EXIT_USB_ERROR,
    EXPECTED_BYTE17_AFTER_MODULE,
    EXPECTED_ROM,
    FACTORY_READ_SIZE,
    FRAME_CHUNK,
    FRAME_TIMEOUT_S,
    INTERFACE,
    MSG_BUSY,
    MSG_DEVICE,
    MSG_FRAMES,
    MSG_LIBUSB,
    MSG_NOT_FOUND,
    MSG_PERMISSION,
    MSG_PYUSB,
    MSG_REMOVED,
    NAMED_REFUSALS,
    NODE_UNKNOWN,
    ONE_BYTE_COMMANDS,
    OP_CLEANUP,
    OP_FACTORY_BITS,
    OP_MODULE,
    OP_PROGRAM,
    OP_QUERY_19,
    OP_ROM_INFO,
    OP_SENSOR_ID,
    OP_STOP,
    PID,
    PROGRAM_READ_SIZE,
    READ_SIZE,
    REDACTED,
    REQ_FACTORY_BITS,
    ROM_INFO_LEN,
    SERIAL_END,
    SERIAL_START,
    STOP_OK,
    TIMEOUT_MS,
    UDEV_RULE,
    VID,
    DeviceError,
    SessionState,
    StepFailed,
    TransferTimeout,
    describe_tx,
    redact_rom_info,
)

# The USB transport, the command guard and the calibration sequence live in
# synaptics0078/session.py (shared with the setup tool); this program adds the run
# directory, result.json, the comparison with Windows and the terminal wording.
UnsafeCommand = session.CalibrationUnsafeCommand  # a capture.UnsafeCommand, as before

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "local" / "calib"
DEFAULT_MODULE = ROOT / "local" / "synaptics0078" / "module.bin"
DEFAULT_WINDOWS_CAL = ROOT / "extracted" / "CalibrationData_decrypted.bin"
LOG_NAME = "calibrate.log"

MSG_FINGERS_OFF = "Keep your fingers OFF the reader until it says done."
MSG_DONE = "Done. You can touch the reader again."
MSG_INPUT = "The data files this calibration needs are missing or not valid (details in the log)."
MSG_REFUSED = "Stopped before sending a command that is not allowed (details in the log)."
MSG_INTERRUPTED = "Interrupted. The reader was told to stop."
MSG_INTERRUPTED_EARLY = "Interrupted."
MSG_INTERRUPTED_CLOSE = "Interrupted while closing the reader; it was closed first."
MSG_UNEXPECTED = "Stopped by an unexpected error in this program (details in the log)."
MSG_NOTHING_SENT = "Stopped: {reason}. Nothing was sent to the reader."

# Dry-run preview input when no Windows CalibrationData is present: factory values
# 0x70 + (i % 32) (they pack to exactly 5 bits) and a flat CF of 0x80 (q = 0).
SYNTHETIC_FACTORY = bytes(0x70 + (i % 32) for i in range(cm.FACTORY_COUNT))
SYNTHETIC_CF = bytes([0x80]) * cm.FRAME_LEN


class RunDirExists(FileExistsError):
    """The run directory itself (not a parent) already exists: another run in the same second."""


def check_command(data: bytes, state: SessionState) -> None:
    """session.check_command with this program's pins, read at call time.

    The module pin is capture.MODULE_SHA256 and the skeleton pins cm.SKELETON_SHA256
    (both re-exports of synaptics0078/pins.py), so a test that patches either of them
    patches the guard too. See session.check_command for the allowed forms.
    """
    session.check_command(
        data,
        state,
        module_sha256=capture.MODULE_SHA256,
        module_len=capture.MODULE_LEN,
        skeletons=cm.SKELETON_SHA256,
    )


# ------------------------------------------------------------ output files


def _stamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def make_private_dirs(path: Path) -> None:
    """Create `path` and missing parents with mode 0700 (explicit chmod, not the umask).

    A parent that appears between the check and the mkdir (another run creating it) is
    accepted when it is a directory, and left as it is; anything else raises OSError.
    """
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            if not d.is_dir():
                raise
            continue
        os.chmod(d, 0o700)


class RunDir:
    """local/calib/<stamp>/, mode 0700; refuses an existing directory. Files 0600, never overwritten."""

    def __init__(self, root: Path, stamp: str):
        make_private_dirs(root)
        self.path = root / stamp
        try:
            os.mkdir(self.path, 0o700)  # never reuse a run directory
        except FileExistsError as exc:
            raise RunDirExists(exc.errno, exc.strerror, str(self.path)) from None
        os.chmod(self.path, 0o700)

    def _open(self, name: str) -> int:
        if "/" in name or name in ("", ".", ".."):
            raise ValueError(f"bad output name {name!r}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.path / name, flags, 0o600)
        os.fchmod(fd, 0o600)
        return fd

    def write(self, name: str, data: bytes) -> Path:
        fd = self._open(name)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return self.path / name

    def open_text(self, name: str):
        return os.fdopen(self._open(name), "w", encoding="utf-8")


class RunLog:
    """Timestamped log in the run directory, flushed on every line."""

    def __init__(self, run: RunDir):
        self._fh = run.open_text(LOG_NAME)
        self.path = run.path / LOG_NAME
        self.write("INFO", f"calibrate started, argv={sys.argv[1:]!r}")

    def write(self, kind: str, text: str) -> None:
        ts = dt.datetime.now().astimezone().isoformat(timespec="microseconds")
        self._fh.write(f"{ts} {kind:<6} {text}\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self.write("INFO", "log closed")
        finally:
            self._fh.close()


# ------------------------------------------------------------------ inputs


@dataclass
class Inputs:
    template: bytes
    module: bytes
    module_source: str
    windows: calibration.CalibrationInputs | None
    windows_source: str


def resolve_module(path_arg: str | None) -> tuple[bytes, str]:
    """(module, where it came from): --module, else local/synaptics0078/module.bin, else the DLL. Hash-checked."""
    if path_arg:
        return capture.check_module(Path(path_arg).read_bytes()), path_arg
    if DEFAULT_MODULE.exists():
        return capture.check_module(DEFAULT_MODULE.read_bytes()), str(DEFAULT_MODULE)
    if Path(capture.DEFAULT_DLL).exists():
        return capture.read_module(Path(capture.DEFAULT_DLL)), f"{capture.DEFAULT_DLL} @0x{capture.MODULE_OFFSET:x}"
    raise calibration.CalibrationError(f"no module: neither {DEFAULT_MODULE} nor {capture.DEFAULT_DLL} exists")


def load_windows(path: Path) -> calibration.CalibrationInputs | None:
    """The stored Windows CalibrationData, or None when the file is absent. A bad file raises."""
    if not path.exists():
        return None
    return calibration.parse_calibration_data(path.read_bytes())


def load_inputs(args) -> Inputs:
    template = calibration.load_template()
    module, source = resolve_module(args.module)
    wpath = Path(args.windows_calibration)
    return Inputs(template, module, source, load_windows(wpath), str(wpath))


# --------------------------------------------------------------- transport


class Transport(session.UsbTransport):
    """session.UsbTransport guarded by this program's check_command (see there)."""

    def __init__(self, log: RunLog):
        super().__init__(log, guard=check_command)


# -------------------------------------------------------------- experiment


def tell(log: RunLog | None, text: str) -> None:
    """A short user-facing line: to the terminal and the log."""
    print(text, flush=True)
    if log is not None:
        log.write("SAY", text.replace("\n", " | "))


class Experiment(session.CalibrationSequence):
    """One --run: the session's calibration sequence, plus the run directory and the verdict."""

    def __init__(self, transport: Transport, run: RunDir, log: RunLog, inputs: Inputs):
        super().__init__(
            transport,
            log,
            template=inputs.template,
            module=inputs.module,
            guard=check_command,
            save=run.write,
            say=lambda text: tell(log, text),
            clock=lambda: time.monotonic(),  # looked up at call time, so tests can patch calibrate.time
        )
        self.run, self.inputs = run, inputs
        self.result = self.info  # one dict: the session's facts and this program's verdict
        self.result.update(verdict="STOPPED", tool="proto/calibrate.py")

    def execute(self) -> int:
        res = self.calibrate()
        if res.bf is None:  # the check score failed: program #2 was not sent
            self.result["verdict"] = "FAIL"
            self.result["reason"] = f"check score {res.score} > {cm.CHECK_MAX}; program #2 not sent"
            tell(self.log, f"FAIL: check score {res.score} (limit {cm.CHECK_MAX}). The first capture does not match")
            tell(self.log, "the reader's factory reference, so capture 2 was not run. Keep fingers off and try again.")
            return EXIT_FAIL
        chunks = res.chunks()
        self.run.write("chunk-34-keyline.bin", chunks.key_line)
        self.run.write("chunk-30-data.bin", chunks.line_update)
        self.run.write("chunk-43-factory.bin", chunks.factory_entries)
        self.run.write("chunk-48-baseline.bin", chunks.baseline)
        return self.judge(res.factory, res.cf, res.bf, res.score)

    def judge(self, factory: bytes, cf: bytes, bf: bytes, score: int) -> int:
        ok, mean, sd = cm.bf_sanity(bf)
        gates = {"score": score <= cm.CHECK_MAX, "bf_sanity": ok}
        self.result["bf_sanity"] = {"ok": ok, "mean": round(mean, 3), "sd": round(sd, 3)}
        lines = [f"  BF: mean {mean:.1f}, sd {sd:.1f} (needs 128 ± 4, sd ≤ 6)"]
        win = self.inputs.windows
        if win is None:
            self.result["windows"] = "not available"
            lines.append("  Comparison with Windows: not available (no CalibrationData)")
        elif win.serial and win.serial != (self.rom_serial or b""):
            self.result["windows"] = "not available (CalibrationData is for another unit)"
            self.log.write("WARN", f"the Windows CalibrationData's serial differs from this unit's ({REDACTED})")
            lines.append("  Comparison with Windows: not available (that CalibrationData is for another unit)")
        else:
            self.result["windows"] = "compared"
            c = cm.compare_cf(cf, win.frame)
            b = cm.compare_bf(bf, win.baseline)
            f = cm.factory_matches(factory, win.factory)
            gates.update(cf=cm.cf_passes(c), bf=cm.bf_passes(b), factory=f >= cm.FACTORY_MIN_MATCH)
            self.result["cf_vs_windows"] = {**c.as_dict(), "pass": gates["cf"]}
            self.result["bf_vs_windows"] = {**b.as_dict(), "pass": gates["bf"]}
            self.result["factory_matches"] = f
            lines.append("  CF vs Windows: " + _fmt(c) + ("" if gates["cf"] else "  FAIL"))
            lines.append("  BF vs Windows: " + _fmt(b, gated_corr=False) + ("" if gates["bf"] else "  FAIL"))
            lines.append(f"  Factory values: {f}/{cm.FACTORY_COUNT} match" + ("" if gates["factory"] else "  FAIL"))
        passed = all(gates.values())
        self.result.update(verdict="PASS" if passed else "FAIL", gates=gates)
        tell(self.log, f"{'PASS' if passed else 'FAIL'}: check score {score} (limit {cm.CHECK_MAX})")
        for line in lines:
            tell(self.log, line)
        return EXIT_OK if passed else EXIT_FAIL


def _fmt(c: cm.Comparison, gated_corr: bool = True) -> str:
    corr = "n/a" if c.correlation is None else f"{c.correlation:.3f}"
    return (
        f"mean |Δ| {c.mean_abs:.2f}, max |Δ| {c.max_abs}, correlation {corr}"
        + ("" if gated_corr else " (not gated)")
        + f", {100 * c.within:.1f}% within ±{c.tolerance}"
    )


THRESHOLDS = {
    "check_max": cm.CHECK_MAX,
    "cf": {
        "min_correlation": cm.CF_MIN_CORRELATION,
        "max_mean_abs": cm.CF_MAX_MEAN_ABS,
        "tolerance": cm.CF_TOLERANCE,
        "min_within": cm.CF_MIN_WITHIN,
    },
    "bf": {"max_mean_abs": cm.BF_MAX_MEAN_ABS, "tolerance": cm.BF_TOLERANCE, "min_within": cm.BF_MIN_WITHIN},
    "bf_sanity": {"mean": cm.BF_SANITY_MEAN, "mean_tol": cm.BF_SANITY_MEAN_TOL, "max_sd": cm.BF_SANITY_MAX_SD},
    "factory_min_match": cm.FACTORY_MIN_MATCH,
}


def run_device(args) -> int:
    """--run: the real experiment."""
    try:
        run = RunDir(Path(OUT_ROOT), _stamp())
    except RunDirExists as exc:
        print(
            f"A run folder with this time stamp already exists (another run in the same second?), "
            f"so nothing was done: {exc.filename}",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT
    except OSError as exc:
        print(f"Cannot create the output folder, so nothing was done: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except KeyboardInterrupt:
        print(MSG_NOTHING_SENT.format(reason="interrupted"), flush=True)
        return EXIT_INTERRUPTED
    log = RunLog(run)
    rc = EXIT_OK
    exp = None
    try:
        try:
            inputs = load_inputs(args)
        except (OSError, calibration.CalibrationError) as exc:
            log.write("ERROR", f"inputs: {exc}")
            tell(None, MSG_INPUT)
            rc = EXIT_BAD_INPUT
            return rc
        except KeyboardInterrupt:
            log.write("WARN", "interrupted while loading the inputs")
            tell(log, MSG_NOTHING_SENT.format(reason="interrupted"))
            rc = EXIT_INTERRUPTED
            return rc
        except Exception as exc:  # noqa: BLE001
            log.write("ERROR", f"unexpected while loading the inputs: {exc!r}\n{traceback.format_exc()}")
            tell(log, MSG_UNEXPECTED)
            rc = EXIT_UNEXPECTED
            return rc
        log.write("INFO", f"template sha256 {calibration.TEMPLATE_SHA256}")
        log.write("INFO", f"module from {inputs.module_source}, sha256 {hashlib.sha256(inputs.module).hexdigest()}")
        log.write(
            "INFO",
            f"Windows CalibrationData: {inputs.windows_source} ({'present' if inputs.windows else 'absent'})",
        )
        tell(log, MSG_FINGERS_OFF)
        transport = Transport(log)
        exp = Experiment(transport, run, log, inputs)
        reason = "see the log"  # for the closing line when nothing reached the reader
        try:
            transport.open()
            rc = exp.execute()
        except StepFailed as exc:
            log.write("ERROR", str(exc))
            exp.result["reason"] = str(exc)
            tell(log, exc.user)
            rc, reason = exc.rc, exc.reason
        except UnsafeCommand as exc:
            log.write("ERROR", f"refused to send: {exc}")
            exp.result["reason"] = f"refused: {exc}"
            tell(log, MSG_REFUSED)
            rc, reason = EXIT_REFUSED, "a command was refused"
        except TransferTimeout as exc:
            log.write("ERROR", str(exc))
            exp.result["reason"] = str(exc)
            tell(log, MSG_DEVICE)
            rc, reason = EXIT_USB_ERROR, "USB timeout"
        except DeviceError as exc:
            # Only a vanished reader (ENODEV) stops all writing; after any other USB error
            # (e.g. EPIPE, or OVERFLOW on a too-long reply) 04/52 are still sent.
            exp.device_gone = exc.errno == ENODEV
            log.write("ERROR", f"USB error: {exc} (errno {exc.errno})")
            exp.result["reason"] = f"USB error: {exc}"
            failure = transport._usb_failure(exc)
            tell(log, failure.user)
            rc, reason = EXIT_USB_ERROR, failure.reason
        except calibration.CalibrationError as exc:
            log.write("ERROR", f"calibration data: {exc}\n{traceback.format_exc()}")
            exp.result["reason"] = str(exc)
            tell(log, MSG_DEVICE)
            rc, reason = EXIT_BAD_REPLY, "unexpected data from the reader"
        except KeyboardInterrupt:
            log.write("WARN", "interrupted; cleaning up")
            exp.result["reason"] = "interrupted"
            tell(log, MSG_INTERRUPTED if exp.state.armed else MSG_INTERRUPTED_EARLY)
            rc, reason = EXIT_INTERRUPTED, "interrupted"
        except Exception as exc:  # noqa: BLE001
            log.write("ERROR", f"unexpected: {exc!r}\n{traceback.format_exc()}")
            exp.result["reason"] = f"unexpected: {exc!r}"
            tell(log, MSG_UNEXPECTED)
            rc, reason = EXIT_UNEXPECTED, "unexpected error"
        finally:
            try:
                exp.cleanup()
            finally:
                try:
                    transport.close()
                except KeyboardInterrupt:
                    log.write("WARN", "interrupted while closing the reader (closing finished first)")
                    exp.result["interrupted_while_closing"] = True
                    tell(log, MSG_INTERRUPTED_CLOSE)
                    rc = EXIT_INTERRUPTED
                # Always close the "fingers OFF until it says done" instruction.
                if transport.writes:
                    tell(log, MSG_DONE)
                else:
                    tell(log, MSG_NOTHING_SENT.format(reason=reason))
        return rc
    finally:
        if exp is not None:
            exp.result.update(exit_code=rc, thresholds=THRESHOLDS)
            try:
                run.write("result.json", (json.dumps(exp.result, indent=2) + "\n").encode())
            except OSError as exc:
                log.write("WARN", f"could not write result.json: {exc!r}")
        log.close()
        tell(None, f"Log and data: {run.path}")


# ------------------------------------------------------------------ dry run


def dry_run(args) -> int:
    """Print the exact sequence with lengths and sha256s. Opens no USB device and imports no usb module."""

    def sha(b: bytes) -> str:
        return hashlib.sha256(b).hexdigest()

    print("Dry run: no USB device is opened. Use --run to do the experiment.")
    try:
        template = calibration.load_template()
        print(f"template: found ({calibration.TEMPLATE_PATH}, sha256 {calibration.TEMPLATE_SHA256})")
    except (OSError, calibration.CalibrationError) as exc:
        template = None
        print(f"template: NOT usable ({exc}); --run would refuse to start")
    try:
        module, source = resolve_module(args.module)
        print(f"module: found ({source})")
    except (OSError, calibration.CalibrationError) as exc:
        module = None
        print(f"module: NOT usable ({exc}); --run would refuse to start")
    wpath = Path(args.windows_calibration)
    try:
        win = load_windows(wpath)
        print(f"Windows CalibrationData: {'found' if win else 'not found'} ({wpath})")
    except (OSError, calibration.CalibrationError) as exc:
        win = None
        print(f"Windows CalibrationData: NOT usable ({exc}); --run would refuse to start")
    synthetic = (SYNTHETIC_FACTORY, SYNTHETIC_CF, "synthetic input (factory 0x70 + i % 32, flat CF 0x80)")
    if win is not None:
        factory, cf, origin = win.factory, win.frame, "the stored Windows CalibrationData"
    else:
        factory, cf, origin = synthetic

    def one(n: int, op: int, what: str) -> None:
        b = bytes([op])
        print(f"{n:2d}. {op:02x}  {len(b):5d} B  sha256 {sha(b)}  {what}")

    one(1, OP_ROM_INFO, "RomInfo: expect 38 B, status 0, ROM 6/0x14")
    one(2, OP_QUERY_19, "query: expect status 0")
    one(3, OP_SENSOR_ID, "sensor id: expect sensor type 0x2449")
    print(
        f" 4. 6f  {len(REQ_FACTORY_BITS):5d} B  sha256 {sha(REQ_FACTORY_BITS)}  factory bits {REQ_FACTORY_BITS.hex(' ')}: "
        "expect subtag 3 (56 values) and subtag 12 (724 B)"
    )
    full = f", whole command sha256 {sha(bytes([OP_MODULE]) + module)}" if module is not None else ""
    print(
        f" 5. 06  {1 + capture.MODULE_LEN:5d} B  module sha256 {capture.MODULE_SHA256} (pinned){full}: "
        "ONLY if RomInfo byte 17 == 0; expect 2 B, status 0"
    )
    one(
        6,
        OP_ROM_INFO,
        f"RomInfo again, ONLY after a 06: byte 17 is logged (expect 0x{EXPECTED_BYTE17_AFTER_MODULE:02x}), "
        "information only",
    )
    p1 = p2 = None
    if template is not None:
        try:
            p1 = cm.build_program1(template, factory)
            p2 = cm.build_program2(template, factory, cf)
        except calibration.CalibrationError as exc:
            print(
                f"preview: {origin} does not fit the program layout ({exc}); showing synthetic input instead. "
                "On --run the reader's own values are checked after 6f, before anything is uploaded."
            )
            factory, cf, origin = synthetic
            p1 = cm.build_program1(template, factory)
            p2 = cm.build_program2(template, factory, cf)
    for n, (step, length, pinned, prog, label) in enumerate(
        (
            (7, cm.PROGRAM1_LEN, cm.PROGRAM1_SKELETON_SHA256, p1, "program #1 (no calibration applied)"),
            (10, cm.PROGRAM2_LEN, cm.PROGRAM2_SKELETON_SHA256, p2, "program #2 (CF applied)"),
        ),
        start=1,
    ):
        print(f"{step:2d}. 02  {length:5d} B  skeleton sha256 {pinned} (pinned)  {label}")
        if prog is not None:
            print(f"              preview sha256 {sha(prog)} ({len(prog)} B, built from {origin})")
        else:
            print("              preview: not available (no template)")
        print(f"              then EP 0x82: read exactly {cm.RAW_LEN} B ({cm.REQ_LINES} x {cm.BPL}) within 5 s")
        one(step + 1, OP_STOP, "stop capture: status 0 or 0x0412")
        one(step + 2, OP_CLEANUP, "cleanup: status 0 or 0x0412")
        if n == 1:
            print(f"    CF = average of frame 2; check score must be <= {cm.CHECK_MAX}, else stop here (FAIL)")
    print("    BF = decode(average of frame 2)")
    print(
        "On --run the per-unit bytes of both programs (the 0x43 factory minimum and bit stream; in #2 also the key "
        "line and the 0x30 data) come from the sensor, so their sha256 will differ from the preview. The guard checks "
        "only the length and the skeleton sha256 (the program with those bytes zeroed), which pins everything else, "
        "and that the two 0x43 factory entries are identical."
    )
    print("04 and 52 are also sent on every exit path once a 02 write was attempted, unless the reader is gone.")
    print("Never sent: 1a, 4f, 50, 51, any flash or db command, a 0x48 chunk, the identify program.")
    print(f"Output on --run: {OUT_ROOT}/<YYYYmmdd-HHMMSS>/")
    return EXIT_OK


# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="B2 calibration experiment for Synaptics 06cb:0078. Without --run: dry run, no USB."
    )
    ap.add_argument("--run", action="store_true", help="really run it on the reader (keep fingers off)")
    ap.add_argument(
        "--module",
        metavar="PATH",
        help="capture module (10500 B, hash-checked); default local/synaptics0078/module.bin, then the vendor DLL",
    )
    ap.add_argument(
        "--windows-calibration",
        metavar="PATH",
        default=str(DEFAULT_WINDOWS_CAL),
        help="Windows CalibrationData_decrypted.bin to compare with (optional; default extracted/...)",
    )
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)
    if not args.run:
        return dry_run(args)
    try:
        return run_device(args)
    except KeyboardInterrupt:  # a second Ctrl-C/SIGTERM while run_device was already cleaning up
        print(MSG_INTERRUPTED_EARLY, flush=True)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
