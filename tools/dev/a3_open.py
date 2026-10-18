"""Step A3 hardware check: open and close the 06cb:0078 through the libfprint fork.

Run it through tools/dev/a3-open.sh, which runs the preflight checks and sets the
environment (FP_DRIVERS_ALLOWLIST, FP_SYNAPTICS0078_DATA_DIR, G_MESSAGES_DEBUG,
LD_LIBRARY_PATH and GI_TYPELIB_PATH pointing at the fork's build-gi). Exit 0 only
when open and close both succeeded; 1 on a driver or device error; 2 on misuse.
"""

from __future__ import annotations

import math
import os
import sys

import gi

gi.require_version("FPrint", "2.0")
from gi.repository import FPrint, Gio, GLib

DRIVER = "synaptics0078"
TIMEOUT_DEFAULT_S = 30.0  # per step; cancels a hung open/close
TIMEOUT_MAX_S = 3600.0


def timeout_from_env() -> float | None:
    """A3_TIMEOUT_S as seconds (default 30), or None after printing why it is invalid."""
    raw = os.environ.get("A3_TIMEOUT_S", "")
    if raw == "":
        return TIMEOUT_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (0 < value <= TIMEOUT_MAX_S):  # also rejects nan and inf
        print(
            f"refusing: A3_TIMEOUT_S={raw!r} is not a number of seconds in (0, {TIMEOUT_MAX_S:g}]",
            file=sys.stderr,
        )
        return None
    return value


def error_line(step: str, err: GLib.Error) -> str:
    """'<step>: FAILED domain=… code=NAME (n): message', naming FPrint.DeviceError codes."""
    code = str(err.code)
    known = (
        (FPrint.device_error_quark(), FPrint.DeviceError, "FPrint.DeviceError"),
        (Gio.io_error_quark(), Gio.IOErrorEnum, "Gio.IOErrorEnum"),
    )
    for quark, enum, label in known:
        if err.matches(quark, err.code):
            try:
                code = f"{label}.{enum(err.code).name} ({err.code})"
            except ValueError:
                code = f"{label} {err.code}"
    return f"{step}: FAILED domain={err.domain!r} code={code}: {err.message}"


def run_step(step: str, call, timeout_s: float) -> bool:
    """Run `call(cancellable)`, cancelled after timeout_s; print OK or the error."""
    cancellable = Gio.Cancellable()
    timer = GLib.timeout_add(max(1, round(timeout_s * 1000)), lambda: cancellable.cancel() or GLib.SOURCE_REMOVE)
    try:
        call(cancellable)
    except GLib.Error as err:
        print(error_line(step, err), flush=True)
        if cancellable.is_cancelled():
            print(f"{step}: cancelled after the {timeout_s:g} s timeout", flush=True)
        return False
    finally:
        if not cancellable.is_cancelled():
            GLib.source_remove(timer)
    print(f"{step}: OK", flush=True)
    return True


def main() -> int:
    timeout_s = timeout_from_env()
    if timeout_s is None:
        return 2
    allowlist = os.environ.get("FP_DRIVERS_ALLOWLIST")
    if allowlist != DRIVER:  # the safety rail: no other driver may touch a device
        print(
            f"refusing: FP_DRIVERS_ALLOWLIST must be exactly {DRIVER!r}, it is {allowlist!r}; run tools/dev/a3-open.sh",
            file=sys.stderr,
        )
        return 2
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

    if not run_step("open", dev.open_sync, timeout_s):
        return 1
    closed = False
    try:  # the steps between open and close (A4 adds capture here); close runs even if they raise
        print(f"is open: {dev.is_open()}", flush=True)
    finally:
        closed = run_step("close", dev.close_sync, timeout_s)
    return 0 if closed else 1


if __name__ == "__main__":
    sys.exit(main())
