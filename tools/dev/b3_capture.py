"""Step B3 at the sensor: a guided session that captures raw images for the offline false-accept benchmark.

Run it through tools/dev/b3-capture.sh, which runs the preflight checks, creates the log and sets the
environment (FP_DRIVERS_ALLOWLIST, FP_SYNAPTICS0078_DATA_DIR, G_MESSAGES_DEBUG=all, stderr to the log). The
images come straight from the reader through the INSTALLED libfprint (package libfprint-sigfm-0078,
/usr/lib/libfprint-2.so.2 and the system FPrint-2.0 typelib) over PyGObject, as tools/dev/a4_capture.py does
with the fork's build. Nothing here needs root.

The terminal shows ONLY short instructions (TOUCH / LIFT / touch again / the next finger), progress and the
final result. Every reason, image statistic, error domain and the driver's debug lines go to the log
(local/logs/b3-capture-<stamp>.log, 0600).

Data (biometric, gitignored): local/b3/<person>/<label>/NNN.pgm (native 144 x 56) plus NNN.json (sidecar).
A sample counts only when both exist; the image is written first, each file atomically (tmp + rename).
Resume: a label is complete when it has >= touches samples whose sidecar session is this run's session (the
local date when the run started); a partial label continues from its count, numbering continues across runs.

Exit codes: 0 all requested captures done; 1 device or driver error, interrupt or too many failures; 2 misuse.

Everything above the GI adapter (GiReader) is pure and tested offline by tools/test_b3_capture.py with a fake
reader, a fake clock and a fake sleep. Importing this module never imports gi.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import a4_capture as a4  # pure helpers: to_native, ensure_private_dir, FingerTracker, finger-status flags
import a5_session as a5  # Output: prompts to the terminal and the log, everything else to the log
import parsers  # proto/parsers.py (on sys.path via a4_capture): pgm_bytes, image_quality, mean_abs_diff

DRIVER = "synaptics0078"
# The ten fingers in session order.
LABELS = (
    "left-thumb",
    "left-index",
    "left-middle",
    "left-ring",
    "left-little",
    "right-thumb",
    "right-index",
    "right-middle",
    "right-ring",
    "right-little",
)
TOUCHES_DEFAULT = 20
TOUCHES_MAX = 100
PERSON_DEFAULT = "me"
PERSON_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Directory names under local/b3/ that the analysis skips: keep in step with RESERVED in
# research/b3/b3_analyze.py, or a person with one of these names would be captured and never analysed.
RESERVED_PERSONS = frozenset({"results", "bin", "logs"})
# The driver's same-touch guard: SYNAPTICS0078_NEW_TOUCH_DIFF in libfprint/drivers/synaptics0078.c, compared
# with image_diff() there (mean absolute difference of two native 144 x 56 images, 0..255). An image closer
# than this to the previous accepted one of the same label is the same touch, not a new one.
NEW_TOUCH_DIFF = 30
MAX_FAILURES = 5  # consecutive touches that did not count, on one touch, before the run stops
CHANGE_PAUSE_S = 3.0  # the pause after "Next: <finger>"
# Per-touch estimate for the intro. local/logs/a4-capture-*.log (normal runs, not --diag): one capture cycle,
# Activation to Activation, took 1.2-3.0 s (finger off 0.5-1.6 s after the image, 2.7-4.9 s in the run with
# a slower lift); 3 s is the round figure for a natural touch and lift.
SECONDS_PER_TOUCH = 3.0
CAPTURE_TIMEOUT_S = 60.0  # one capture attempt, which includes waiting for the finger
STEP_TIMEOUT_S = 30.0  # open and close
B3_DIR = ROOT / "local" / "b3"
NATIVE_W, NATIVE_H = parsers.IMAGE_WIDTH, parsers.IMAGE_HEIGHT  # 144 x 56
SAMPLE_RE = re.compile(r"^([0-9]{3,})\.(pgm|json)$")

# GUsb's GUsbDeviceError (gusb-device.h; domain "g-usb-device-error-quark"): what libusb_open and
# libusb_claim_interface failures become; the driver passes them through from open.
GUSB_DOMAIN = "g-usb-device-error-quark"
GUSB_PERMISSION_DENIED = 9
GUSB_BUSY = 10
# FpDeviceError (fp-device.h; domain "fp - device - error - quark", G_DEFINE_QUARK keeps the spaces).
FP_ERROR_DOMAIN = "fp-device-error-quark"
FP_ERROR_GENERAL = 0
FP_ERROR_NOT_SUPPORTED = 1
FP_ERROR_BUSY = 4
FP_ERROR_DATA_INVALID = 6
FP_ERROR_REMOVED = 0x100
FP_ERROR_TOO_HOT = 0x101
NO_DEVICE_DOMAIN = "b3"  # our own: the reader is not among libfprint's devices

UDEV_RULE = "/etc/udev/rules.d/70-fingerprint-06cb-0078.rules"
USB_VID, USB_PID = "06cb", "0078"
DATA_HELPER = "proto/.venv/bin/python tools/synaptics0078_data.py"

MSG_TOUCH_AGAIN = "Touch again… TOUCH"
MSG_LIFT = "LIFT"
MSG_BUSY = (
    "The reader is in use by fprintd or another program. Close any fingerprint prompt (lock screen, settings), "
    "wait a moment and run again. If it persists: sudo systemctl stop fprintd (it starts again on demand)."
)
# {node}: the reader's /dev/bus/usb/BBB/DDD (see usb_node), or the pattern when it is not known.
MSG_PERMISSION = (
    "No permission to use the reader. Check it with: getfacl {node} (your user should be listed with rw).\n"
    "Re-plug the reader, or log out and in again, then run the same command again.\n"
    f"If the udev rule {UDEV_RULE} was just added, load it first:\n"
    "  sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usb --action=add"
)
NODE_UNKNOWN = "/dev/bus/usb/BBB/DDD"
MSG_DATA = f"The reader's data files are missing or not valid. Create them (from the project folder): {DATA_HELPER}"
MSG_TOO_HOT = "The sensor is too warm. Let it rest about 5 minutes, then run the same command again."
MSG_REMOVED = "The reader was disconnected. Check it, then run the same command again."
MSG_NO_DEVICE = (
    "The fingerprint reader (06cb:0078) was not found. Check that it is connected and enabled, then run again."
)
MSG_DEVICE = "The reader reported an error, so this run stops here (details in the log)."
MSG_UNEXPECTED = "Stopped by an unexpected error (details in the log)."


# ------------------------------------------------------------------ names and arguments


def display(label: str) -> str:
    """'left-middle' -> 'LEFT MIDDLE'."""
    return label.replace("-", " ").upper()


def parse_fingers(text: str) -> list[str]:
    """'right-index,left-index' -> the labels in session order. Raises ValueError naming the problem."""
    seen: list[str] = []
    for raw in text.split(","):
        name = raw.strip().lower().removesuffix("-finger")
        if name not in LABELS:
            raise ValueError(f"unknown finger {raw.strip()!r}; the fingers are: {', '.join(LABELS)}")
        if name in seen:
            raise ValueError(f"{name} is listed twice")
        seen.append(name)
    return [label for label in LABELS if label in seen]


def _fingers_arg(text: str) -> list[str]:
    try:
        return parse_fingers(text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err)) from None


def _touches_arg(text: str) -> int:
    if not re.fullmatch(r"[0-9]{1,3}", text.strip()) or not 1 <= int(text) <= TOUCHES_MAX:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {TOUCHES_MAX}, got {text!r}")
    return int(text)


def _person_arg(text: str) -> str:
    if not PERSON_RE.match(text):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a short name: lowercase letters, digits, '-' and '_', up to 32, starting with a "
            "letter or digit"
        )
    if text in RESERVED_PERSONS:
        raise argparse.ArgumentTypeError(
            f"{text!r} is a reserved folder name (the analysis skips it); choose another name"
        )
    return text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The options; misuse exits 2 with argparse's message on stderr."""
    parser = argparse.ArgumentParser(
        prog="b3-capture.sh",
        # No prefixes: the wrapper blocks --log/--package/--validate-only by full name only.
        allow_abbrev=False,
        description="B3: guided capture for the false-accept benchmark (use tools/dev/b3-capture.sh)",
    )
    parser.add_argument(
        "--fingers", type=_fingers_arg, default=list(LABELS), help=f"comma-separated, from: {', '.join(LABELS)}"
    )
    parser.add_argument("--touches", type=_touches_arg, default=TOUCHES_DEFAULT, help=f"per finger, 1..{TOUCHES_MAX}")
    parser.add_argument("--person", type=_person_arg, default=PERSON_DEFAULT, help="whose fingers (a directory name)")
    # Set by the wrapper.
    parser.add_argument("--log", help=argparse.SUPPRESS)
    parser.add_argument("--package", default="unknown", help=argparse.SUPPRESS)
    parser.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def estimate_minutes(touches: int, fingers: int) -> int:
    """Whole minutes, rounded up, for `touches` touches and `fingers` finger changes."""
    return max(1, math.ceil((touches * SECONDS_PER_TOUCH + fingers * CHANGE_PAUSE_S) / 60))


# ------------------------------------------------------------------ errors to plain messages


def _norm_domain(domain) -> str:
    return str(domain or "").replace(" ", "")


def error_kind(domain, code, message) -> str:
    """busy, permission, data, too-hot, removed, no-device or other, from a GLib.Error's domain string and code,
    falling back to the message text (libusb_strerror's 'Resource busy' / 'Access denied ...')."""
    dom, msg = _norm_domain(domain), str(message or "").lower()
    if dom == NO_DEVICE_DOMAIN:
        return "no-device"
    if dom == GUSB_DOMAIN:
        if code == GUSB_BUSY:
            return "busy"
        if code == GUSB_PERMISSION_DENIED:
            return "permission"
    if dom == FP_ERROR_DOMAIN:
        if code == FP_ERROR_BUSY:
            return "busy"
        if code == FP_ERROR_TOO_HOT:
            return "too-hot"
        if code == FP_ERROR_REMOVED:
            return "removed"
        if code == FP_ERROR_DATA_INVALID or (code == FP_ERROR_GENERAL and "cannot read" in msg):
            return "data"
    if "resource busy" in msg or "device or resource busy" in msg or "busy" in msg.split():
        return "busy"
    if "access denied" in msg or "permission denied" in msg or "insufficient permissions" in msg:
        return "permission"
    return "other"


def error_message(domain, code, message, node: str | None = None) -> str:
    """The terminal text for a device error (the details go to the log); `node` is the reader's device node."""
    return {
        "busy": MSG_BUSY,
        "permission": MSG_PERMISSION.format(node=node or NODE_UNKNOWN),
        "data": MSG_DATA,
        "too-hot": MSG_TOO_HOT,
        "removed": MSG_REMOVED,
        "no-device": MSG_NO_DEVICE,
    }.get(error_kind(domain, code, message), MSG_DEVICE)


# ------------------------------------------------------------------ the reader interface


class ReaderError(Exception):
    """kind: retry (the driver asks for another touch), timeout (no image in time), interrupted (Ctrl-C) or
    error (anything else; domain/code/message from the GLib.Error)."""

    def __init__(self, kind: str, message: str = "", domain: str = "", code: int = 0) -> None:
        super().__init__(message)
        self.kind, self.message, self.domain, self.code = kind, message, domain, code


@dataclass
class Capture:
    pixels: bytes
    width: int
    height: int
    note: str = ""  # for the log (timing)


# ------------------------------------------------------------------ the store


def read_pgm(path: Path) -> tuple[bytes, int, int]:
    """Pixels, width, height of a binary 8-bit PGM as pgm_bytes writes it. Raises ValueError."""
    data = path.read_bytes()
    parts = data.split(maxsplit=4)
    if len(parts) < 4 or parts[0] != b"P5" or parts[3] != b"255":
        raise ValueError(f"{path.name} is not an 8-bit binary PGM")
    width, height = int(parts[1]), int(parts[2])
    pixels = data[len(data) - width * height :] if len(data) >= width * height else b""
    if len(pixels) != width * height:
        raise ValueError(f"{path.name} is short")
    return pixels, width, height


@dataclass
class Sample:
    index: int
    session: str | None
    pgm: Path


def scan_label(directory: Path) -> list[Sample]:
    """The complete samples (NNN.pgm with a readable NNN.json) in `directory`, by index; orphans are left out."""
    if not directory.is_dir():
        return []
    names: dict[int, set[str]] = {}
    for entry in os.listdir(directory):
        m = SAMPLE_RE.match(entry)
        if m:
            names.setdefault(int(m.group(1)), set()).add(m.group(2))
    samples = []
    for index in sorted(names):
        if names[index] != {"pgm", "json"}:
            continue
        try:
            meta = json.loads((directory / f"{index:03d}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        session = meta.get("session")
        samples.append(Sample(index, session if isinstance(session, str) else None, directory / f"{index:03d}.pgm"))
    return samples


@dataclass
class LabelState:
    label: str
    done: int  # samples of this session
    next_index: int  # NNN of the next sample (continues across runs)
    last: Sample | None  # the last sample of this session (the same-touch reference on resume)


def label_state(label: str, samples: list[Sample], session: str) -> LabelState:
    today = [s for s in samples if s.session == session]
    return LabelState(
        label=label,
        done=len(today),
        next_index=(samples[-1].index + 1) if samples else 1,
        last=today[-1] if today else None,
    )


def write_atomic(path: Path, data: bytes) -> None:
    """Write `path` mode 0600 through a temp file in the same directory and a rename (an existing file, or an
    orphan, is replaced; a symlink at `path` is replaced, never followed)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def sidecar(
    *,
    when: datetime,
    session: str,
    person: str,
    label: str,
    index: int,
    attempts: int,
    pixels: bytes,
    diff_prev: float | None,
    delivered: tuple[int, int],
    upscale: int | None,
    library: dict,
) -> dict:
    return {
        "time": when.isoformat(timespec="seconds"),
        "session": session,
        "person": person,
        "label": label,
        "index": index,
        "attempts": attempts,
        "image": {"width": NATIVE_W, "height": NATIVE_H, **parsers.image_quality(pixels, NATIVE_W, NATIVE_H)},
        "diff_prev": None if diff_prev is None else round(diff_prev, 2),
        "delivered": {"width": delivered[0], "height": delivered[1], "upscale": upscale},
        "library": dict(library),
    }


def save_sample(directory: Path, index: int, pixels: bytes, meta: dict) -> None:
    """NNN.pgm, then NNN.json: the sample counts only once both exist."""
    write_atomic(directory / f"{index:03d}.pgm", parsers.pgm_bytes(pixels, NATIVE_W, NATIVE_H))
    write_atomic(directory / f"{index:03d}.json", (json.dumps(meta, indent=1) + "\n").encode("utf-8"))


# ------------------------------------------------------------------ the session


class Stop(Exception):
    """Ends the run: the terminal line and the exit code."""

    def __init__(self, line: str, code: int = 1) -> None:
        super().__init__(line)
        self.line, self.code = line, code


@dataclass
class Session:
    reader: object  # open(), capture(on_event) -> Capture, close(), interrupted
    labels: list[str]
    touches: int
    person: str
    io: a5.Output
    sleep: object  # sleep(seconds); the real one keeps Ctrl-C working
    now: object = lambda: datetime.now().astimezone()
    root: Path = B3_DIR
    library: dict = field(default_factory=dict)
    pause_s: float = CHANGE_PAUSE_S
    saved: int = 0
    opened: bool = False
    session: str = ""  # the local date when the run started, YYYY-MM-DD

    def person_dir(self) -> Path:
        return self.root / self.person

    def shown_dir(self) -> str:
        try:
            return f"{self.person_dir().relative_to(ROOT)}/"
        except ValueError:
            return f"{self.person_dir()}/"

    def run(self) -> int:
        self.session = self.now().date().isoformat()
        self.io.record("note", f"session {self.session}, person {self.person}, {self.touches} touches, {self.labels}")
        states = [label_state(label, scan_label(self.person_dir() / label), self.session) for label in self.labels]
        todo = []
        for k, st in enumerate(states, 1):
            if st.done >= self.touches:
                self.io.say(f"{display(st.label)}: already done today ({st.done} of {self.touches}), skipping")
            else:
                if st.done:
                    self.io.say(f"{display(st.label)}: {st.done} of {self.touches} done today, continuing")
                todo.append((k, st))
        remaining = sum(self.touches - st.done for _, st in todo)
        if not todo:
            self.io.say(f"Nothing to capture: every finger asked for is done today, in {self.shown_dir()}")
            return 0
        try:
            self.prepare_dirs([st.label for _, st in todo])
            try:  # close() runs after a failed open too (it releases what open set up)
                try:
                    self.reader.open()
                except ReaderError as err:
                    raise self.device_stop(err, "open") from None
                self.opened = True
                mapped = getattr(self.reader, "library_path", None)
                if mapped:
                    self.library["path"] = mapped
                self.intro(remaining, len(todo))
                for k, st in todo:
                    self.capture_label(k, st)
            except KeyboardInterrupt:
                raise self.interrupted() from None
            finally:
                self.reader.close()
        except Stop as stop:
            self.io.say(stop.line)
            if self.opened:
                self.io.say(f"Saved {self.saved} image(s) this run in {self.shown_dir()}")
            return stop.code
        except KeyboardInterrupt:  # before the reader took over Ctrl-C
            self.io.say(self.interrupted().line)
            return 1
        self.io.say(f"Done: saved {self.saved} image(s) for {len(todo)} finger(s) in {self.shown_dir()}")
        return 0

    def prepare_dirs(self, labels: list[str]) -> None:
        try:
            a4.ensure_private_dir(self.root.parent, chmod=False)  # local/: create if absent, change nothing
            a4.ensure_private_dir(self.root)
            a4.ensure_private_dir(self.person_dir())
            for label in labels:
                a4.ensure_private_dir(self.person_dir() / label)
        except OSError as err:
            self.io.record("error", f"cannot prepare {self.person_dir()}: {err}")
            raise Stop(f"Cannot write to {self.shown_dir()}: {err}") from None

    def intro(self, remaining: int, fingers: int) -> None:
        say = self.io.say
        say(f"Fingerprint capture for the false-accept test (person: {self.person}).")
        say(
            f"{fingers} finger(s), {self.touches} touches each: {remaining} touches to go, "
            f"about {estimate_minutes(remaining, fingers)} minute(s)."
        )
        say("For each touch: wait for TOUCH, touch naturally, the way you would to unlock, and lift at LIFT.")
        say("Ctrl-C stops safely; run the same command again to continue where you stopped.")

    def interrupted(self) -> Stop:
        return Stop("Stopped (Ctrl-C). What was saved is kept; run the same command again to continue.")

    def device_stop(self, err: ReaderError, step: str) -> Stop:
        if err.kind == "interrupted":
            return self.interrupted()
        self.io.record("error", f"{step}: domain={err.domain!r} code={err.code}: {err.message}")
        return Stop(error_message(err.domain, err.code, err.message, getattr(self.reader, "node", None)))

    def load_reference(self, st: LabelState) -> bytes | None:
        if st.last is None:
            return None
        try:
            pixels, width, height = read_pgm(st.last.pgm)
        except (OSError, ValueError) as err:
            self.io.record("note", f"{st.label}: no same-touch reference from {st.last.pgm.name}: {err}")
            return None
        return pixels if (width, height) == (NATIVE_W, NATIVE_H) else None

    def capture_label(self, k: int, st: LabelState) -> None:
        n = len(self.labels)
        name = display(st.label)
        self.io.say("")
        self.io.say(f"Next: {name} — get it ready")
        self.sleep(self.pause_s)
        if getattr(self.reader, "interrupted", False):
            raise self.interrupted()
        directory = self.person_dir() / st.label
        reference = self.load_reference(st)
        index = st.next_index
        for touch in range(st.done + 1, self.touches + 1):
            reference = self.capture_touch(
                f"Finger {k} of {n}: {name}. Touch {touch} of {self.touches}… TOUCH",
                st.label,
                directory,
                index,
                reference,
            )
            index += 1
        self.io.say(f"{name} done.")

    def capture_touch(self, line: str, label: str, directory: Path, index: int, reference: bytes | None) -> bytes:
        """Capture until one image is saved as NNN = index; returns its native pixels (the next reference)."""
        failures = attempts = 0
        while True:
            attempts += 1
            prompt = line if failures == 0 else MSG_TOUCH_AGAIN

            def on_event(event: str, prompt: str = prompt) -> None:
                if event == "needed":
                    self.io.say(prompt)
                elif event == "taken":
                    self.io.say(MSG_LIFT)

            reason = None
            try:
                cap = self.reader.capture(on_event)
            except ReaderError as err:
                if err.kind in ("retry", "timeout"):
                    reason = f"{err.kind}: {err.message}"
                else:
                    raise self.device_stop(err, f"capture {label} {index:03d}") from None
            if reason is None:
                self.io.record(
                    "capture", f"{label} {index:03d} attempt {attempts}: {cap.width}x{cap.height} {cap.note}"
                )
                pixels, reason, upscale = self.check(cap)
            if reason is None:
                diff = None if reference is None else parsers.mean_abs_diff(pixels, reference)
                if diff is not None and diff < NEW_TOUCH_DIFF:
                    reason = f"same touch as the previous image (diff {diff:.1f} < {NEW_TOUCH_DIFF})"
            if reason is None:
                meta = sidecar(
                    when=self.now(),
                    session=self.session,
                    person=self.person,
                    label=label,
                    index=index,
                    attempts=attempts,
                    pixels=pixels,
                    diff_prev=diff,
                    delivered=(cap.width, cap.height),
                    upscale=upscale,
                    library=self.library,
                )
                try:
                    save_sample(directory, index, pixels, meta)
                except OSError as err:
                    self.io.record("error", f"cannot save {label} {index:03d}: {err}")
                    raise Stop(f"Cannot save the image in {self.shown_dir()}: {err.strerror or err}") from None
                self.saved += 1
                self.io.record(
                    "saved",
                    f"{label}/{index:03d}: attempts {attempts}, diff {meta['diff_prev']}, {parsers.format_quality(meta['image'])}",
                )
                return pixels
            failures += 1
            self.io.record("again", f"{label} {index:03d} attempt {attempts}: {reason}")
            if getattr(self.reader, "interrupted", False):
                raise self.interrupted()
            if failures >= MAX_FAILURES:
                raise Stop(
                    f"Stopped: {failures} touches in a row did not count. What was saved is kept; "
                    "run the same command again to continue."
                )

    def check(self, cap: Capture) -> tuple[bytes, str | None, int | None]:
        """Native pixels, None or the reason the capture does not count, the upscale factor."""
        try:
            native = a4.to_native(cap.pixels, cap.width, cap.height)
        except ValueError as err:
            return b"", f"bad capture: {err}", None
        if native.factor is None or (native.width, native.height) != (NATIVE_W, NATIVE_H):
            return b"", f"bad capture: {native.warning}", None
        return native.pixels, None, native.factor


# ------------------------------------------------------------------ the GI adapter (the only part touching libfprint)


def loaded_library(maps: str = "/proc/self/maps") -> str:
    """The path of the libfprint-2 this process mapped, or 'unknown'."""
    try:
        with open(maps, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                path = line.split(maxsplit=5)[-1].strip() if len(line.split()) >= 6 else ""
                if "/libfprint-2.so" in path:
                    return path
    except OSError:
        pass
    return "unknown"


def usb_node(sysfs: str = "/sys/bus/usb/devices") -> str | None:
    """/dev/bus/usb/BBB/DDD of the one 06cb:0078 on the bus (sysfs only, as the wrapper finds it), else None."""
    nodes = []
    try:
        entries = sorted(os.listdir(sysfs))
    except OSError:
        return None
    for name in entries:
        dev = Path(sysfs) / name
        try:
            if (dev / "idVendor").read_text().strip() != USB_VID or (dev / "idProduct").read_text().strip() != USB_PID:
                continue
            bus, num = int((dev / "busnum").read_text()), int((dev / "devnum").read_text())
        except (OSError, ValueError):
            continue
        nodes.append(f"/dev/bus/usb/{bus:03d}/{num:03d}")
    return nodes[0] if len(nodes) == 1 else None


def route_glib_log_to_stderr(GLib) -> str:
    """Send every GLib log line (the driver's DEBUG/INFO too) to stderr, which the wrapper sends to the log.

    GLib's default writer prints DEBUG and INFO to STDOUT (the terminal) when G_MESSAGES_DEBUG enables them.
    GLib 2.68+ can switch that off; before it, a writer of our own formats each line and writes it to stderr
    (it sees every level: the wrapper's G_MESSAGES_DEBUG=all would let them all through anyway)."""
    if hasattr(GLib, "log_writer_default_set_use_stderr"):
        GLib.log_writer_default_set_use_stderr(True)
        return "log_writer_default_set_use_stderr"

    def writer(level, fields, *_user_data):
        sys.stderr.write(GLib.log_writer_format_fields(level, fields, False) + "\n")
        sys.stderr.flush()
        return GLib.LogWriterOutput.HANDLED

    GLib.log_set_writer_func(writer, None)
    return "log_set_writer_func"


class GiReader:
    """Opens the one synaptics0078 device once, captures per touch, closes at the end and on Ctrl-C."""

    def __init__(self, io: a5.Output, timeout_s: float = CAPTURE_TIMEOUT_S) -> None:
        self.io, self.timeout_s = io, timeout_s
        self.dev = None
        # The FPrint.Context owns the libusb context: drop it and libusb_exit runs under the
        # still-open device, so the next transfer segfaults. Held until close().
        self.ctx = None
        self.interrupted = False
        self.library_path: str | None = None
        self.node: str | None = None  # /dev/bus/usb/BBB/DDD, for the permission message
        self.GLib = None
        self._cancellable = None
        self._sigint = None

    def _glib(self):
        from gi.repository import GLib

        return GLib

    def _gi(self):
        import gi

        gi.require_version("FPrint", "2.0")
        from gi.repository import FPrint, Gio, GLib

        return gi, FPrint, Gio, GLib

    def setup(self) -> None:
        """Route GLib's log lines to stderr, load FPrint and take over Ctrl-C (a GLib source that cancels the
        running step). The routing comes first: before it, any debug line would reach the terminal."""
        import signal

        how = route_glib_log_to_stderr(self._glib())  # before FPrint is even loaded
        gi, FPrint, Gio, GLib = self._gi()
        self.FPrint, self.Gio, self.GLib = FPrint, Gio, GLib
        self.io.record("note", f"GLib log lines go to stderr (the log) via {how}")
        if int(FPrint.FingerStatusFlags.NEEDED) != a4.FINGER_NEEDED or int(FPrint.FingerStatusFlags.PRESENT) != (
            a4.FINGER_PRESENT
        ):
            raise ReaderError("error", "FPrint.FingerStatusFlags differ from what this script assumes", "b3-setup")
        self.io.record("note", f"FPrint typelib: {FPrint!r}")

        def on_sigint() -> bool:
            self.interrupted = True
            if self._cancellable is not None:
                self._cancellable.cancel()
            return GLib.SOURCE_CONTINUE

        try:  # GLib 2.80+: GLibUnix.signal_add (GLib.unix_signal_add is deprecated)
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix

            add = GLibUnix.signal_add
        except (ImportError, ValueError):
            add = GLib.unix_signal_add
        self._sigint = add(GLib.PRIORITY_HIGH, signal.SIGINT, on_sigint)

    def _run(self, call, timeout_s: float, interruptible: bool = True):
        """call(cancellable) with a timeout (and Ctrl-C when interruptible); GLib.Error becomes ReaderError."""
        GLib, FPrint = self.GLib, self.FPrint
        cancellable = self.Gio.Cancellable()
        if interruptible:
            self._cancellable = cancellable
            if self.interrupted:
                cancellable.cancel()
        fired = {"timeout": False}

        def on_timeout() -> bool:
            fired["timeout"] = True
            cancellable.cancel()
            return GLib.SOURCE_REMOVE

        timer = GLib.timeout_add(max(1, round(timeout_s * 1000)), on_timeout)
        try:
            return call(cancellable)
        except GLib.Error as err:
            if interruptible and self.interrupted:
                raise ReaderError("interrupted", err.message, err.domain, err.code) from None
            if fired["timeout"]:
                raise ReaderError("timeout", f"no image within {timeout_s:g} s", err.domain, err.code) from None
            if err.matches(FPrint.device_retry_quark(), err.code):
                try:
                    name = FPrint.DeviceRetry(err.code).value_nick
                except (ValueError, AttributeError):
                    name = str(err.code)
                raise ReaderError("retry", f"{err.message} ({name})", err.domain, err.code) from None
            raise ReaderError("error", err.message, err.domain, err.code) from None
        finally:
            self._cancellable = None
            if not fired["timeout"]:
                GLib.source_remove(timer)

    def open(self) -> None:
        self.setup()
        self.node = usb_node()
        ctx = self.FPrint.Context()
        ours = [d for d in ctx.get_devices() if d.get_driver() == DRIVER]
        if len(ours) != 1:
            raise ReaderError("error", f"expected one {DRIVER} device, found {len(ours)}", NO_DEVICE_DOMAIN, 0)
        dev = ours[0]
        self.io.record("note", f"device: {dev.get_name()} ({dev.get_driver()}, {dev.get_device_id()})")
        self.ctx = ctx
        self._run(dev.open_sync, STEP_TIMEOUT_S)
        self.dev = dev
        self.library_path = loaded_library()
        self.io.record("note", f"open: OK; libfprint mapped from {self.library_path}")

    def capture(self, on_event) -> Capture:
        tracker = a4.FingerTracker()

        def on_finger_status(device, _pspec) -> None:
            for event in tracker.update(int(device.get_finger_status()), time.monotonic()):
                on_event(event)

        handler = self.dev.connect("notify::finger-status", on_finger_status)
        try:
            image = self._run(lambda c: self.dev.capture_sync(True, c), self.timeout_s)
            done_at = time.monotonic()
        finally:
            self.dev.disconnect(handler)
        note = a4.format_timing(tracker.taken_at, tracker.off_at, done_at)
        return Capture(bytes(image.get_data()), image.get_width(), image.get_height(), note)

    def wait(self, seconds: float) -> None:
        """Sleep while letting GLib run, so Ctrl-C is seen at once."""
        ctx = self.GLib.MainContext.default() if self.GLib is not None else None
        end = time.monotonic() + seconds
        while not self.interrupted and time.monotonic() < end:
            if ctx is not None:
                while ctx.pending():
                    ctx.iteration(False)
            time.sleep(0.05)

    def close(self) -> None:
        if self.dev is not None:
            try:  # not interruptible: a Ctrl-C must not leave the reader open
                self._run(self.dev.close_sync, STEP_TIMEOUT_S, interruptible=False)
                self.io.record("note", "close: OK")
            except ReaderError as err:
                self.io.record("error", f"close: domain={err.domain!r} code={err.code}: {err.message}")
            self.dev = None
        self.ctx = None
        if self._sigint is not None:
            self.GLib.source_remove(self._sigint)
            self._sigint = None


def open_log(path: str):
    """Append to the wrapper's log (created 0600 by it); never through a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.validate_only:
        return 0
    if not args.log:
        print("refusing: run tools/dev/b3-capture.sh (it makes the log)", file=sys.stderr)
        return 2
    allowlist = os.environ.get("FP_DRIVERS_ALLOWLIST")
    if allowlist != DRIVER:  # no other driver may touch a device
        print(f"refusing: FP_DRIVERS_ALLOWLIST must be {DRIVER!r}; run tools/dev/b3-capture.sh", file=sys.stderr)
        return 2
    with open_log(args.log) as log:
        io = a5.Output(sys.stdout, log)
        reader = GiReader(io)
        session = Session(
            reader=reader,
            labels=args.fingers,
            touches=args.touches,
            person=args.person,
            io=io,
            sleep=reader.wait,
            library={"path": "/usr/lib/libfprint-2.so.2", "package": args.package},
        )
        try:
            return session.run()
        except Exception:  # noqa: BLE001 -- the traceback goes to the log, one plain line to the terminal
            io.record("error", traceback.format_exc().rstrip())
            io.say(MSG_UNEXPECTED)
            return 1


if __name__ == "__main__":
    sys.exit(main())
