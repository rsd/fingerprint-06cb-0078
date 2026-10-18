"""Step A4 hardware check: capture N images from the 06cb:0078 through the libfprint fork.

Run it through tools/dev/a4-capture.sh, which runs the preflight checks and sets the
environment (FP_DRIVERS_ALLOWLIST, FP_SYNAPTICS0078_DATA_DIR, G_MESSAGES_DEBUG,
LD_LIBRARY_PATH and GI_TYPELIB_PATH pointing at the fork's build-gi). Exit 0 only when
all N images were delivered and the device closed; 1 on a driver or device error, a
timeout or an interrupt; 2 on misuse.

Core facts this relies on (libfprint/fpi-image-device.c, fp-device.c):

* Finger status flags. Entering AWAIT_FINGER_ON adds NEEDED; the driver's "finger on"
  report adds PRESENT; fpi_image_device_image_captured() enters AWAIT_FINGER_OFF, which
  removes NEEDED while PRESENT stays; the driver's "finger off" report removes PRESENT.
  So NEEDED set = touch now, PRESENT set = finger detected, NEEDED cleared with PRESENT
  still set = image taken, lift now.
* A CAPTURE action completes only after the finger-off report, so the capture callback
  fires after the lift. Finger-off time is measured from "image taken".
* Images are saved as the core delivered them, decimated back to the sensor's native
  144 x 56 when the driver upscales. The driver's algorithm is SIGFM, and
  fp_image_extract_sigfm_info copies the data and restores it unchanged (only the NBIS
  path normalises), so the delivered pixels are the driver's 2x replication of the raw
  pixels: decimating gives proto-comparable raw pixels.

The pure helpers up to main() need neither PyGObject nor a device; tools/test_a4_capture.py
tests them offline. Images go only under local/captures (mode 0700, files 0600): they are
biometric data and never belong in the repository.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import stat
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "proto") not in sys.path:
    sys.path.insert(0, str(ROOT / "proto"))

import parsers  # stdlib-only pure parsers: pgm_bytes, image_quality

DRIVER = "synaptics0078"
TIMEOUT_DEFAULT_S = 60.0  # per capture attempt, which includes waiting for the finger
TIMEOUT_MAX_S = 3600.0
STEP_TIMEOUT_S = 30.0  # open and close
COUNT_DEFAULT = 3
COUNT_MAX = 50
MAX_ATTEMPTS = 3  # per image: a retry error re-runs the capture
CAPTURE_DIR = ROOT / "local" / "captures"
STAMP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")

NATIVE_W, NATIVE_H = parsers.IMAGE_WIDTH, parsers.IMAGE_HEIGHT  # 144 x 56

# FpFingerStatusFlags (fp-device.h); main() checks them against the typelib.
FINGER_NEEDED = 1
FINGER_PRESENT = 2


# ------------------------------------------------------------------ pure helpers


def timeout_from_env() -> float | None:
    """A4_TIMEOUT_S as seconds (default 60), or None after printing why it is invalid."""
    raw = os.environ.get("A4_TIMEOUT_S", "")
    if raw == "":
        return TIMEOUT_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (0 < value <= TIMEOUT_MAX_S):  # also rejects nan and inf
        print(
            f"refusing: A4_TIMEOUT_S={raw!r} is not a number of seconds in (0, {TIMEOUT_MAX_S:g}]",
            file=sys.stderr,
        )
        return None
    return value


def classify_error(is_retry: bool, cancelled: bool = False) -> str:
    """'timeout' when our cancellable fired, 'retry' for an FP_DEVICE_RETRY error, else 'fail'.

    `is_retry` is err.matches(FPrint.device_retry_quark(), err.code), computed by the caller.
    """
    if cancelled:
        return "timeout"
    return "retry" if is_retry else "fail"


class FingerTracker:
    """Turns finger-status flag changes into prompt events and timestamps (times in monotonic seconds).

    update() returns the events this change produced, in order:
      'needed'  NEEDED appeared: touch the sensor now
      'present' PRESENT appeared: finger detected
      'taken'   NEEDED cleared while PRESENT stays set: the image is taken, lift now
      'off'     PRESENT cleared after 'taken': the driver reported the finger off
      'lifted'  PRESENT cleared before 'taken': the finger left early
    """

    def __init__(self) -> None:
        self.flags = 0
        self.present_at: float | None = None
        self.taken_at: float | None = None
        self.off_at: float | None = None

    def update(self, flags: int, now: float) -> list[str]:
        prev, self.flags = self.flags, int(flags)
        added, removed = self.flags & ~prev, prev & ~self.flags
        events = []
        if added & FINGER_NEEDED:
            events.append("needed")
        if added & FINGER_PRESENT:
            events.append("present")
            self.present_at = now
        # NEEDED|PRESENT -> NONE in one change (a retry or an abort resets both) is no image taken.
        if removed & FINGER_NEEDED and self.flags & FINGER_PRESENT and self.taken_at is None:
            events.append("taken")
            self.taken_at = now
        if removed & FINGER_PRESENT:
            if self.taken_at is not None and self.off_at is None:
                events.append("off")
                self.off_at = now
            elif self.taken_at is None:
                events.append("lifted")
        return events


def ms_between(start: float | None, end: float | None) -> int | None:
    """Whole milliseconds from start to end (monotonic seconds), None when either is missing."""
    if start is None or end is None:
        return None
    return round((end - start) * 1000)


def format_timing(taken_at: float | None, off_at: float | None, done_at: float | None) -> str:
    """'finger off N ms after image taken, capture completed M ms after image taken', n/a for a missing time."""
    off, done = ms_between(taken_at, off_at), ms_between(taken_at, done_at)
    off_s = "n/a" if off is None else f"{off} ms"
    done_s = "n/a" if done is None else f"{done} ms"
    return f"finger off {off_s} after image taken, capture completed {done_s} after image taken"


def image_stats(pixels: bytes) -> str:
    """Same text as proto/capture.py image_stats: 'min X, max Y, mean M.1f, sd S.2f'."""
    mean = statistics.fmean(pixels)
    sd = statistics.pstdev(pixels)
    return f"min {min(pixels)}, max {max(pixels)}, mean {mean:.1f}, sd {sd:.2f}"


def coverage_text(pixels: bytes, width: int, height: int) -> str:
    """'ridge coverage ~N%' from proto's block estimate, or 'ridge coverage n/a' when it cannot be computed."""
    try:
        q = parsers.image_quality(pixels, width, height)
    except (ValueError, ZeroDivisionError):
        return "ridge coverage n/a"
    return f"ridge coverage ~{q['coverage'] * 100:.0f}%"


@dataclass
class Native:
    pixels: bytes
    width: int
    height: int
    factor: int | None  # upscale factor found, None when the size is not a multiple of 144 x 56 or blocks differ
    warning: str | None


def to_native(pixels: bytes, width: int, height: int) -> Native:
    """Decimate a k x k nearest-neighbour upscale of 144 x 56 back to native; keep anything else as delivered.

    width == 144k and height == 56k (integer k >= 1) with every k x k block uniform gives the native image;
    k == 1 is returned unchanged. Otherwise the image is returned as delivered with a warning.
    Raises ValueError when the pixel count does not match width x height.
    """
    if width <= 0 or height <= 0 or len(pixels) != width * height:
        raise ValueError(f"{len(pixels)} pixel bytes do not fill {width} x {height}")
    if width % NATIVE_W or height % NATIVE_H or width // NATIVE_W != height // NATIVE_H:
        return Native(
            pixels,
            width,
            height,
            None,
            f"delivered size {width}x{height} is not an integer multiple of {NATIVE_W}x{NATIVE_H}; saved as delivered",
        )
    k = width // NATIVE_W
    if k == 1:
        return Native(pixels, width, height, 1, None)
    out = bytearray(NATIVE_W * NATIVE_H)
    for y in range(height):
        row = pixels[y * width : (y + 1) * width]
        top = (y // k) * k * width
        for x in range(width):
            if row[x] != pixels[top + (x // k) * k]:
                return Native(
                    pixels,
                    width,
                    height,
                    None,
                    f"delivered {width}x{height} is not a uniform {k}x{k} upscale of {NATIVE_W}x{NATIVE_H}; "
                    "saved as delivered",
                )
    for ny in range(NATIVE_H):
        for nx in range(NATIVE_W):
            out[ny * NATIVE_W + nx] = pixels[ny * k * width + nx * k]
    return Native(bytes(out), NATIVE_W, NATIVE_H, k, None)


def ensure_private_dir(path: Path, chmod: bool = True) -> None:
    """Create `path` mode 0700 if absent. Refuse a symlink or a non-directory (lstat, nothing is followed).

    An existing directory of ours with group/other bits gets chmod 0700 when `chmod` is true.
    Raises OSError with a readable message.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, 0o700)
        return
    if stat.S_ISLNK(st.st_mode):
        raise OSError(f"{path} is a symlink; refusing to write captures through it")
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} exists and is not a directory")
    if chmod and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)


def capture_path(directory: Path, stamp: str, index: int) -> Path:
    return directory / f"a4-{stamp}-{index:02d}.pgm"


def save_pgm(directory: Path, stamp: str, index: int, pixels: bytes, width: int, height: int) -> Path:
    """Write <directory>/a4-<stamp>-NN.pgm as a binary PGM, mode 0600, never overwriting, never through a symlink."""
    data = parsers.pgm_bytes(pixels, width, height)
    path = capture_path(directory, stamp, index)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def stamp_from_env() -> str:
    """A4_STAMP (set by the wrapper so log and images share it) when well formed, else the current time."""
    raw = os.environ.get("A4_STAMP", "")
    return raw if STAMP_RE.match(raw) else datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def prompt_for(event: str, index: int, count: int) -> str | None:
    """The line to show the user for a FingerTracker event, None for events that need none."""
    return {
        "needed": f"Capture {index}/{count}: touch the sensor now",
        "present": "Finger detected: keep still, then lift when told",
        "taken": "Image taken: lift your finger now",
        "lifted": "Finger lifted before the image was taken",
    }.get(event)


# ------------------------------------------------------------------ device part


def error_line(step: str, err, FPrint, Gio) -> str:
    """'<step>: FAILED domain=… code=NAME (n): message', naming FPrint.DeviceError codes."""
    code = str(err.code)
    known = (
        (FPrint.device_error_quark(), FPrint.DeviceError, "FPrint.DeviceError"),
        (FPrint.device_retry_quark(), FPrint.DeviceRetry, "FPrint.DeviceRetry"),
        (Gio.io_error_quark(), Gio.IOErrorEnum, "Gio.IOErrorEnum"),
    )
    for quark, enum, label in known:
        if err.matches(quark, err.code):
            try:
                code = f"{label}.{enum(err.code).name} ({err.code})"
            except ValueError:
                code = f"{label} {err.code}"
    return f"{step}: FAILED domain={err.domain!r} code={code}: {err.message}"


def main() -> int:
    parser = argparse.ArgumentParser(description="A4: capture N images through the libfprint fork (use a4-capture.sh)")
    parser.add_argument("--count", type=int, default=COUNT_DEFAULT, help=f"images to capture (default {COUNT_DEFAULT})")
    args = parser.parse_args()
    if not 1 <= args.count <= COUNT_MAX:
        print(f"refusing: --count must be 1..{COUNT_MAX}, it is {args.count}", file=sys.stderr)
        return 2
    timeout_s = timeout_from_env()
    if timeout_s is None:
        return 2
    allowlist = os.environ.get("FP_DRIVERS_ALLOWLIST")
    if allowlist != DRIVER:  # the safety rail: no other driver may touch a device
        print(
            f"refusing: FP_DRIVERS_ALLOWLIST must be exactly {DRIVER!r}, it is {allowlist!r}; run tools/dev/a4-capture.sh",
            file=sys.stderr,
        )
        return 2

    import signal

    import gi

    gi.require_version("FPrint", "2.0")
    from gi.repository import FPrint, Gio, GLib

    if int(FPrint.FingerStatusFlags.NEEDED) != FINGER_NEEDED or int(FPrint.FingerStatusFlags.PRESENT) != FINGER_PRESENT:
        print("refusing: FPrint.FingerStatusFlags values differ from what this script assumes", file=sys.stderr)
        return 2

    # Prepare the image directory before touching the device.
    try:
        ensure_private_dir(CAPTURE_DIR.parent, chmod=False)
        ensure_private_dir(CAPTURE_DIR)
    except OSError as err:
        print(f"FAIL: cannot prepare {CAPTURE_DIR}: {err}", flush=True)
        return 1
    stamp = stamp_from_env()

    ctx = FPrint.Context()
    devices = ctx.get_devices()
    ours = [d for d in devices if d.get_driver() == DRIVER]
    if len(ours) != 1:
        print(f"devices: FAILED expected exactly one {DRIVER} device, found {len(ours)}", flush=True)
        for d in devices:
            print(f"  found: driver={d.get_driver()} id={d.get_device_id()} name={d.get_name()!r}", flush=True)
        if not devices:
            print(f"  found: no devices (FP_DRIVERS_ALLOWLIST={allowlist})", flush=True)
        return 1
    dev = ours[0]
    print(f"driver: {dev.get_driver()}", flush=True)
    print(f"device id: {dev.get_device_id()}", flush=True)
    print(f"name: {dev.get_name()}", flush=True)
    print(f"scan type: {dev.get_scan_type().value_nick}", flush=True)

    current = {"cancellable": None, "interrupted": False}

    def on_sigint() -> bool:
        current["interrupted"] = True
        if current["cancellable"] is not None:
            current["cancellable"].cancel()
        return GLib.SOURCE_CONTINUE

    try:  # GLib 2.80+: GLibUnix.signal_add; GLib.unix_signal_add warns (on the terminal) that it is deprecated
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix

        unix_signal_add = GLibUnix.signal_add
    except (ImportError, ValueError):
        unix_signal_add = GLib.unix_signal_add
    sigint_id = unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, on_sigint)

    def run_step(step: str, call, step_timeout_s: float) -> bool:
        """Run `call(cancellable)`, cancelled after step_timeout_s; print OK or the error."""
        cancellable = Gio.Cancellable()
        current["cancellable"] = cancellable
        timer = GLib.timeout_add(
            max(1, round(step_timeout_s * 1000)), lambda: cancellable.cancel() or GLib.SOURCE_REMOVE
        )
        try:
            call(cancellable)
        except GLib.Error as err:
            print(error_line(step, err, FPrint, Gio), flush=True)
            if cancellable.is_cancelled():
                print(f"{step}: cancelled (timeout {step_timeout_s:g} s or interrupt)", flush=True)
            return False
        finally:
            current["cancellable"] = None
            if not cancellable.is_cancelled():
                GLib.source_remove(timer)
        print(f"{step}: OK", flush=True)
        return True

    def capture_once(index: int):
        """One capture attempt. Returns (kind, image_or_error, tracker, done_at); kind is ok/retry/timeout/fail."""
        tracker = FingerTracker()

        def on_finger_status(device, _pspec) -> None:
            for event in tracker.update(int(device.get_finger_status()), time.monotonic()):
                line = prompt_for(event, index, args.count)
                if line:
                    print(line, flush=True)

        cancellable = Gio.Cancellable()
        current["cancellable"] = cancellable
        timer = GLib.timeout_add(max(1, round(timeout_s * 1000)), lambda: cancellable.cancel() or GLib.SOURCE_REMOVE)
        handler = dev.connect("notify::finger-status", on_finger_status)
        try:
            image = dev.capture_sync(True, cancellable)
            return "ok", image, tracker, time.monotonic()
        except GLib.Error as err:
            done_at = time.monotonic()
            kind = classify_error(err.matches(FPrint.device_retry_quark(), err.code), cancellable.is_cancelled())
            if kind == "retry":
                try:
                    name = FPrint.DeviceRetry(err.code).name
                except ValueError:
                    name = str(err.code)
                return kind, f"{err.message} ({name})", tracker, done_at
            return kind, error_line(f"capture {index}/{args.count}", err, FPrint, Gio), tracker, done_at
        finally:
            dev.disconnect(handler)
            current["cancellable"] = None
            if not cancellable.is_cancelled():
                GLib.source_remove(timer)

    if not run_step("open", dev.open_sync, STEP_TIMEOUT_S):
        GLib.source_remove(sigint_id)
        return 1
    delivered = 0
    failure: str | None = None
    try:
        print(f"is open: {dev.is_open()}", flush=True)
        print(f"capturing {args.count} image(s), timeout {timeout_s:g} s per attempt, into {CAPTURE_DIR}", flush=True)
        for index in range(1, args.count + 1):
            attempt = 0
            while True:
                if current["interrupted"]:  # Ctrl-C landed while no capture was running: stop, do not wait for a finger
                    kind = "interrupted"
                    break
                attempt += 1
                kind, result, tracker, done_at = capture_once(index)
                if kind == "retry" and attempt < MAX_ATTEMPTS:
                    print(f"retry: {result}, touch again (attempt {attempt}/{MAX_ATTEMPTS})", flush=True)
                    continue
                break
            if kind != "ok":
                if kind == "retry":
                    failure = f"image {index}/{args.count}: {attempt} attempts all asked to retry (last: {result})"
                elif kind == "interrupted":
                    failure = f"image {index}/{args.count}: interrupted (attempt {attempt})"
                elif kind == "timeout":
                    why = "interrupted" if current["interrupted"] else f"no image within {timeout_s:g} s"
                    failure = f"image {index}/{args.count}: {why} (attempt {attempt}); {result}"
                else:
                    failure = result
                break
            width, height = result.get_width(), result.get_height()
            try:
                native = to_native(bytes(result.get_data()), width, height)
                path = save_pgm(CAPTURE_DIR, stamp, index, native.pixels, native.width, native.height)
            except (ValueError, OSError) as err:
                failure = f"image {index}/{args.count}: cannot save the image: {err}"
                break
            tag = f"image {index}/{args.count}"
            scale = f"upscale x{native.factor}" if native.factor else "no upscale factor"
            print(
                f"{tag}: delivered {width}x{height}, {scale}, saved {native.width}x{native.height} to {path}",
                flush=True,
            )
            if native.warning:
                print(f"{tag}: WARN {native.warning}", flush=True)
            print(
                f"{tag}: {image_stats(native.pixels)}, {coverage_text(native.pixels, native.width, native.height)}",
                flush=True,
            )
            print(
                f"{tag}: attempts {attempt}, {format_timing(tracker.taken_at, tracker.off_at, done_at)}",
                flush=True,
            )
            delivered += 1
    finally:
        closed = run_step("close", dev.close_sync, STEP_TIMEOUT_S)
        GLib.source_remove(sigint_id)
    if failure is not None:
        print(f"FAIL: {failure} ({delivered}/{args.count} images delivered)", flush=True)
        return 1
    if not closed:
        print(f"FAIL: {delivered}/{args.count} images delivered but close failed", flush=True)
        return 1
    print(f"PASS: {delivered}/{args.count} images delivered", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
