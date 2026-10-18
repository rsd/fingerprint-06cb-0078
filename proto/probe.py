#!/usr/bin/env python3
"""Read-only probe for the Synaptics 06cb:0078 fingerprint reader.

Sends exactly three one-byte plaintext commands, one at a time, each followed
by a read of the reply:

    0x01  RomInfo           (38-byte reply expected)
    0x19  opaque query      (68-byte reply expected)
    0x75  sensor id         (10-byte reply expected)

on bulk OUT 0x01, reading replies from bulk IN 0x81. A fourth read-only query,
off by default and selected only with --only 6f, asks for the factory bits:

    0x6f  get factory bits  (9-byte request, tag 0x0e00)

Nothing else is ever written. In particular: no USB reset, no clear_halt, no vendor/class control
requests, no string-descriptor reads, nothing to other endpoints.

The only control-pipe traffic is whatever libusb itself does on open/claim
(on Linux libusb reads the active configuration from sysfs, falling back to a
standard GET_CONFIGURATION read), and set_configuration() ONLY when the
device reports that it is unconfigured.

Usage: see README.md. Parsing lives in parsers.py (pure functions).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import sys
import time
from pathlib import Path

import calibration
import parsers

VID, PID = 0x06CB, 0x0078
EP_OUT = 0x01
EP_IN = 0x81
INTERFACE = 0

# The complete, closed set of bytes this program may ever write. Order matters:
# it is the order the references send them in (python-validity sensor_2449.py
# open(): 0x01, 0x19, 0x75; iusearch/Synaptics78 main.py:29-32).
REQUESTS = (
    (parsers.CMD_ROM_INFO, b"\x01", "RomInfo"),
    (parsers.CMD_QUERY_19, b"\x19", "query 0x19 (layout undocumented)"),
    (parsers.CMD_SENSOR_ID, b"\x75", "sensor id"),
)

# Read-only query, never part of the default run (--only 6f). The request is
# pack('<BHHL', 0x6f, 0x0e00, 0, 0) = [PV] sensor.py:128-129 get_factory_bits(0x0e00);
# the DLL builds the same 9-byte request in function 0x180083cf0
# (research/calibration-analysis.md section 3).
OPTIONAL_REQUESTS = (
    (parsers.CMD_FACTORY_BITS, parsers.REQ_FACTORY_BITS, "factory bits (get_factory_bits tag 0x0e00)"),
)
ALL_REQUESTS = REQUESTS + OPTIONAL_REQUESTS

# Large enough for any expected reply (the longest is 68 bytes, which is one
# 64-byte packet plus a 4-byte short packet; the transfer ends on the short
# packet). Reads never ask for more than this.
READ_SIZE = 1024
# The factory-bits reply is much longer (a reference sample is 0xd88 = 3464
# bytes of payload, [PV] sensor.py:116-127), so it gets a larger buffer.
READ_SIZES = {parsers.CMD_FACTORY_BITS: 16384}

DEFAULT_CALIBRATION = Path(__file__).resolve().parent.parent / "extracted" / "CalibrationData_decrypted.bin"

EXIT_OK, EXIT_USB_ERROR, EXIT_NOT_FOUND, EXIT_DRIVER_BUSY, EXIT_BAD_REPLY, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


class ExchangeLog:
    """Timestamped, line-buffered log. Flushed on every line so an
    interrupted run keeps everything that happened.

    The file name is <prefix>-<date>-<time>-<microseconds>-p<pid>.log. If that
    name is taken anyway (same microsecond and PID, or a clock stepping back)
    a -N counter is appended: the file is opened with mode "x" so an existing
    log is never overwritten, and a name clash never aborts the run.
    """

    def __init__(self, directory: Path, prefix: str = "probe"):
        directory.mkdir(parents=True, exist_ok=True)
        base = f"{prefix}-{_now().strftime('%Y%m%d-%H%M%S-%f')}-p{os.getpid()}"
        for n in range(1000):
            path = directory / (f"{base}.log" if n == 0 else f"{base}-{n}.log")
            try:
                self._fh = open(path, "x", encoding="utf-8")  # noqa: SIM115 (kept open for the run)
            except FileExistsError:
                continue
            self.path = path
            break
        else:
            raise FileExistsError(f"no free log name under {directory} for {base}")
        self.write("INFO", f"{prefix} started, argv={sys.argv[1:]!r}")

    def write(self, kind: str, text: str) -> None:
        ts = dt.datetime.now().astimezone().isoformat(timespec="microseconds")
        self._fh.write(f"{ts} {kind:<6} {text}\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self.write("INFO", "log closed")
        finally:
            self._fh.close()


def say(log: ExchangeLog | None, kind: str, text: str) -> None:
    """Print to the terminal and, if a log exists, record it too."""
    print(text)
    if log is not None:
        log.write(kind, text.replace("\n", " | "))


def show_exchange(log, cmd, req, label, reply, elapsed_ms) -> parsers.Decoded:
    say(log, "INFO", f"\n=== command 0x{cmd:02x}: {label} ===")
    print(f"request  EP 0x{EP_OUT:02x}, {len(req)} byte(s): {parsers.hexstr(req)}")
    # Raw reply goes to the log as one hex line (lossless) ...
    if log is not None:
        log.write("RX", f"EP 0x{EP_IN:02x} {len(reply)} byte(s) in {elapsed_ms:.1f} ms: {reply.hex()}")
    # ... and to the terminal as an offset dump.
    print(f"reply    EP 0x{EP_IN:02x}, {len(reply)} byte(s), {elapsed_ms:.1f} ms:")
    print(parsers.hexdump(reply))
    decoded = parsers.PARSERS[cmd](reply)
    print("decoded:")
    for line in decoded.render():
        print(line)
        if log is not None:
            log.write("DECODE", f"0x{cmd:02x} {line.strip()}")
    return decoded


def selected_requests(only: str | None):
    if not only:
        return REQUESTS
    want = {int(x, 16) for x in only.split(",") if x}
    unknown = want - {c for c, _, _ in ALL_REQUESTS}
    if unknown:
        raise SystemExit(f"--only: unsupported command(s) {sorted(hex(u) for u in unknown)}; allowed: 01,19,75,6f")
    return tuple(r for r in ALL_REQUESTS if r[0] in want)


def hint_for(exc) -> str:
    errno = getattr(exc, "errno", None)
    if errno in (13, 1):  # EACCES / EPERM
        return " (permission denied: see the udev rule in README.md)"
    if errno == 16:  # EBUSY
        return " (interface busy: a kernel driver or another process holds it)"
    return ""


class UsbSession:
    """Find, configure (only if unconfigured), claim and finally release the
    device. Shared by probe.py and capture.py.

    open() returns None on success or an exit code on a refusal; close() is safe
    to call on every path, including after a failed or partial open(), and never
    raises over the original outcome. The USB reset is only done when
    usb_reset is True (capture.py --usb-reset); probe.py never asks for it.
    """

    def __init__(self, log: ExchangeLog, detach_kernel_driver: bool = False, usb_reset: bool = False):
        self.log = log
        self.detach_kernel_driver = detach_kernel_driver
        self.usb_reset = usb_reset
        self.dev = None
        self.claimed = False
        self.detached = False

    def open(self) -> int | None:
        # Imported late so --dry-run and the tests work without pyusb installed.
        import usb.backend.libusb1
        import usb.core
        import usb.util

        log = self.log
        backend = usb.backend.libusb1.get_backend()
        if backend is None:
            say(log, "ERROR", "libusb1 backend not available (is libusb-1.0 installed?)")
            return EXIT_USB_ERROR

        found = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=backend))
        if not found:
            say(log, "ERROR", f"device {VID:04x}:{PID:04x} not found")
            return EXIT_NOT_FOUND
        if len(found) > 1:
            say(log, "ERROR", f"{len(found)} devices match {VID:04x}:{PID:04x}; refusing to guess")
            return EXIT_NOT_FOUND
        self.dev = dev = found[0]
        say(
            log,
            "INFO",
            f"found {VID:04x}:{PID:04x} bus {dev.bus} address {dev.address}, bcdDevice 0x{dev.bcdDevice:04x}",
        )

        # Configuration: only touch it if the device is unconfigured.
        try:
            cfg = dev.get_active_configuration()
            say(log, "INFO", f"active configuration {cfg.bConfigurationValue}; leaving it alone")
        except usb.core.USBError as exc:
            if "Configuration not set" not in str(exc):
                raise
            say(log, "WARN", "device is unconfigured: calling set_configuration() once (the only case allowed)")
            dev.set_configuration()

        # Kernel driver: detach only on explicit request.
        try:
            driver_active = dev.is_kernel_driver_active(INTERFACE)
        except (NotImplementedError, usb.core.USBError) as exc:
            say(log, "WARN", f"cannot query kernel driver state: {exc!r}")
            driver_active = False
        if driver_active:
            if not self.detach_kernel_driver:
                say(
                    log,
                    "ERROR",
                    "a kernel driver is attached to interface 0; re-run with --detach-kernel-driver to detach it (it is re-attached on exit)",
                )
                return EXIT_DRIVER_BUSY
            say(log, "INFO", "detaching kernel driver from interface 0 (explicit flag)")
            dev.detach_kernel_driver(INTERFACE)
            self.detached = True

        if self.usb_reset:
            say(log, "WARN", "USB reset requested by --usb-reset")
            dev.reset()

        usb.util.claim_interface(dev, INTERFACE)
        self.claimed = True
        say(log, "INFO", f"claimed interface {INTERFACE}")
        return None

    def close(self) -> None:
        # Cleanup must never raise over the original outcome.
        import usb.util

        dev, log = self.dev, self.log
        if dev is None:
            return
        if self.claimed:
            try:
                usb.util.release_interface(dev, INTERFACE)
                log.write("INFO", f"released interface {INTERFACE}")
            except Exception as exc:  # noqa: BLE001
                log.write("WARN", f"release_interface failed: {exc!r}")
            self.claimed = False
        if self.detached:
            try:
                dev.attach_kernel_driver(INTERFACE)
                log.write("INFO", "kernel driver re-attached")
            except Exception as exc:  # noqa: BLE001
                log.write("WARN", f"re-attach failed: {exc!r}")
            self.detached = False
        try:
            usb.util.dispose_resources(dev)
            log.write("INFO", "resources disposed")
        except Exception as exc:  # noqa: BLE001
            log.write("WARN", f"dispose_resources failed: {exc!r}")
        self.dev = None


def check_factory_bits(log, decoded: parsers.Decoded, calibration_path: Path) -> None:
    """Compare the live 56 factory values with the ones cached by Windows."""
    say(log, "INFO", f"\n--- comparison with {calibration_path} (offset 0x{calibration.FACTORY_OFFSET:x}) ---")
    try:
        blob = calibration_path.read_bytes()
        stored = calibration.parse_calibration_data(blob).factory
    except (OSError, calibration.CalibrationError) as exc:
        say(log, "WARN", f"cannot read the stored factory values: {exc}")
        return
    if decoded.values.get("status") != 0 or "entries" not in decoded.values:
        say(log, "WARN", "reply not decodable as factory bits; nothing to compare")
        return
    result = parsers.compare_factory_values(decoded.values["by_subtag"], stored)
    for line in result["lines"]:
        say(log, "COMPARE", line)
    say(log, "COMPARE", "RESULT: " + result["verdict"])


def run(args) -> int:
    requests = selected_requests(args.only)

    if args.dry_run:
        print("dry run: no USB access. Would send, in order, on EP 0x01 (reading EP 0x81 after each):")
        for cmd, req, label in requests:
            print(f"  0x{cmd:02x}  bytes: {parsers.hexstr(req)}   {label}")
        return EXIT_OK

    # Imported late so --dry-run and the tests work without pyusb installed.
    import usb.core

    log = ExchangeLog(Path(args.log_dir))
    print(f"logging to {log.path}")

    session = UsbSession(log, detach_kernel_driver=args.detach_kernel_driver)
    rc = EXIT_OK
    try:
        failed = session.open()
        if failed is not None:
            return failed
        dev = session.dev

        for cmd, req, label in requests:
            log.write("TX", f"EP 0x{EP_OUT:02x} {len(req)} byte(s): {req.hex()}")
            written = dev.write(EP_OUT, req, timeout=args.timeout_ms)
            if written != len(req):
                say(log, "ERROR", f"short write: {written} of {len(req)} bytes")
                rc = EXIT_USB_ERROR
                break
            t0 = time.monotonic()
            try:
                reply = bytes(dev.read(EP_IN, READ_SIZES.get(cmd, READ_SIZE), timeout=args.timeout_ms))
            except usb.core.USBTimeoutError:
                say(
                    log,
                    "ERROR",
                    f"read on EP 0x{EP_IN:02x} timed out after {args.timeout_ms} ms for command 0x{cmd:02x}; stopping, nothing further is sent",
                )
                rc = EXIT_USB_ERROR
                break
            elapsed = (time.monotonic() - t0) * 1000
            decoded = show_exchange(log, cmd, req, label, reply, elapsed)
            if decoded.values.get("status") != 0:
                rc = max(rc, EXIT_BAD_REPLY)
            elif cmd == parsers.CMD_FACTORY_BITS:
                check_factory_bits(log, decoded, Path(args.calibration))
    except KeyboardInterrupt:
        say(log, "WARN", "interrupted; cleaning up")
        rc = EXIT_INTERRUPTED
    except usb.core.USBError as exc:
        say(log, "ERROR", f"USB error: {exc!r}{hint_for(exc)}")
        rc = EXIT_USB_ERROR
    finally:
        session.close()
        log.close()
        print(f"\nlog saved: {log.path}")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only probe for Synaptics 06cb:0078 (sends only 0x01, 0x19, 0x75; and 0x6f with --only 6f)."
    )
    ap.add_argument(
        "--detach-kernel-driver",
        action="store_true",
        help="detach a kernel driver from interface 0 if one is attached (default: refuse); re-attached on exit",
    )
    ap.add_argument(
        "--timeout-ms",
        type=int,
        default=3000,
        help="timeout per write and per read (default 3000); Ctrl-C is honoured after the current transfer returns",
    )
    ap.add_argument(
        "--only",
        metavar="CMDS",
        help="comma-separated subset of 01,19,75,6f (default: 01,19,75 in that order; 6f, the read-only factory-bits query, only runs when named here)",
    )
    ap.add_argument(
        "--calibration",
        default=str(DEFAULT_CALIBRATION),
        help="CalibrationData_decrypted.bin to compare the 6f factory values with (default: ../extracted/CalibrationData_decrypted.bin)",
    )
    ap.add_argument(
        "--log-dir",
        default=str(Path(__file__).resolve().parent / "logs"),
        help="directory for the timestamped log (default: proto/logs)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent and exit without touching USB")
    args = ap.parse_args(argv)
    if args.timeout_ms <= 0:
        ap.error("--timeout-ms must be positive")

    # Make SIGTERM take the same clean-up path as Ctrl-C.
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
