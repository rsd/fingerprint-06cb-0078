"""Write the data files the libfprint `synaptics0078` driver loads (DEV USE ONLY).

The driver (06cb:0078, libfprint fork) reads two files from its data directory,
`/var/lib/fprint/synaptics0078` by default, overridden by the environment
variable FP_SYNAPTICS0078_DATA_DIR [brief D4]:

  module.bin              the 10500 raw module bytes from the vendor DLL (file
                          offset 0x19cca0), sha256-pinned; no opcode, the driver
                          prepends the 0x06 itself
  program-<serial>.bin    the complete 18869-byte `02` command exactly as proto/
                          sends it, first byte 0x02, built from the unit's
                          decrypted CalibrationData

`<serial>` is the 6-byte sensor serial (RomInfo 0x12-0x17, equal to the serial in
CalibrationData, CalibrationInputs.serial) with each byte's nibbles swapped, as
12 lowercase hex characters; it equals the USB serial string. The driver picks
the program by the serial it reads from the sensor, so a program built for
another unit is never sent.

This is the user-side generator (step A3, kept in A6): it runs as the user and
writes to local/synaptics0078 (git-ignored), which tools/dev/a3-open.sh points
the driver at. It never runs as root: it imports proto/, which the user owns.
The root step is the separate, self-contained setup tool of the package
(packaging/arch/libfprint-sigfm-0078/synaptics0078-setup, installed as
/usr/bin/synaptics0078-setup), which checks these files again and copies them
to /var/lib/fprint/synaptics0078:

  sudo synaptics0078-setup install --from <out dir>

It stays stdlib-only; it reuses synaptics0078/calibration.py and proto/capture.py,
neither of which imports a third-party module at import time (pyusb is imported
late, inside probe.py's USB transport).

The files are proprietary (module) and per unit (program): never commit them.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (ROOT / "proto", ROOT):  # proto/capture.py, and the synaptics0078 package
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import capture

from synaptics0078 import calibration

DEFAULT_DLL = ROOT / "driverstore" / "synaWudfBioUsb.dll"
DEFAULT_CALIBRATION = ROOT / "extracted" / "CalibrationData_decrypted.bin"
DEFAULT_OUT = ROOT / "local" / "synaptics0078"

MODULE_NAME = "module.bin"
PROGRAM_LEN = calibration.PAYLOAD_LEN  # 18869, opcode byte included
PROGRAM_OPCODE = capture.OP_CAPTURE_PROGRAM  # 0x02
SERIAL_LEN = calibration.SERIAL_LEN  # 6
SERIAL_HEX_RE = re.compile(r"^[0-9a-f]{12}$")


class DataError(calibration.CalibrationError):
    """The inputs or outputs are not what the driver accepts."""


def serial_hex(raw: bytes) -> str:
    """The 6 sensor-serial bytes, each byte's nibbles swapped, as lowercase hex.

    b"\\x12\\xab\\x30..." -> "21ba03...". This is the `<serial>` in the program name.
    """
    if len(raw) != SERIAL_LEN:
        raise DataError(f"sensor serial is {len(raw)} bytes, expected {SERIAL_LEN}")
    return "".join(f"{((b & 0x0F) << 4) | (b >> 4):02x}" for b in raw)


def program_name(serial: str) -> str:
    """program-<serial>.bin, for a serial as returned by serial_hex()."""
    if not SERIAL_HEX_RE.match(serial):
        raise DataError(f"serial {serial!r} is not 12 lowercase hex characters")
    return f"program-{serial}.bin"


def check_program(program: bytes) -> bytes:
    if len(program) != PROGRAM_LEN:
        raise DataError(f"program is {len(program)} bytes, expected {PROGRAM_LEN}")
    if program[0] != PROGRAM_OPCODE:
        raise DataError(f"program starts with 0x{program[0]:02x}, expected the 0x{PROGRAM_OPCODE:02x} opcode")
    return program


def validate(module: bytes, program: bytes, serial: str) -> None:
    """Refuse anything the driver would refuse (sizes, module hash, opcode, name)."""
    capture.check_module(module)  # 10500 bytes and the pinned sha256
    check_program(program)
    program_name(serial)


def build_data(dll_path: Path, calibration_path: Path) -> tuple[bytes, bytes, str]:
    """(module, program, serial hex) from the vendor DLL and the decrypted CalibrationData."""
    module, program, inputs = capture.load_inputs(Path(dll_path), Path(calibration_path))
    serial = serial_hex(inputs.serial)
    validate(module, program, serial)
    return module, program, serial


def _out_dir_state(out_dir: Path) -> os.stat_result | None:
    """lstat out_dir: None if absent; refuse a symlink or a non-directory."""
    try:
        st = os.lstat(out_dir)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise DataError(f"{out_dir} is a symlink; give the directory itself")
    if not stat.S_ISDIR(st.st_mode):
        raise DataError(f"{out_dir} exists and is not a directory")
    return st


def _stage(path: Path, data: bytes) -> str:
    """Write `data` to a temp file, mode 0600, next to `path`; return its name."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def _publish(staged: list[tuple[str, Path]], force: bool) -> None:
    """Move every staged temp file to its target; all of them or none.

    Without force os.link refuses to clobber a target that appeared after the
    existence check. With force the replaced file is first hard-linked to a
    backup name, so a failure on a later target restores it. On any failure
    the targets already published are rolled back and the error re-raised.
    """
    published: list[tuple[Path, str | None]] = []  # (target, backup of the file it replaced)
    backups: list[str] = []
    try:
        for tmp, path in staged:
            if not force:
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    raise DataError(f"{path} exists; --force overwrites it") from None
                published.append((path, None))
                continue
            backup = None
            if os.path.lexists(path):
                backup = str(path.with_name(f".{path.name}.{secrets.token_hex(6)}.bak"))
                os.link(path, backup, follow_symlinks=False)
                backups.append(backup)
            os.replace(tmp, path)
            published.append((path, backup))
    except BaseException as exc:
        for path, backup in reversed(published):
            try:
                if backup is None:
                    os.unlink(path)
                else:
                    os.replace(backup, path)
            except OSError as undo:
                note = f"rolling back {path} failed: {undo}"
                if backup is not None:
                    backups.remove(backup)  # keep the previous bytes for the user
                    note += f"; the previous file is kept as {backup}"
                exc.add_note(note)
        raise
    finally:
        for name in [tmp for tmp, _ in staged] + backups:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)


def write_data(out_dir: Path, module: bytes, program: bytes, serial: str, force: bool = False) -> list[Path]:
    """Validate, then write module.bin and program-<serial>.bin into out_dir.

    out_dir is created mode 0700 if absent; an existing one is refused if it is
    a symlink and tightened to 0700 if group or others have any access (it
    holds proprietary and per-unit bytes; A6 runs this as root). Without force
    nothing is written when either target exists, even as a dangling symlink.
    Both files are staged first and then published together: a failure leaves
    the directory as it was. Returns the two paths.
    """
    validate(module, program, serial)
    out_dir = Path(out_dir)
    targets = [(out_dir / MODULE_NAME, module), (out_dir / program_name(serial), program)]
    st = _out_dir_state(out_dir)
    if not force:
        present = [str(p) for p, _ in targets if os.path.lexists(p)]
        if present:
            raise DataError(f"already present: {', '.join(present)}; --force overwrites")
    if st is None:
        out_dir.mkdir(mode=0o700, parents=True)
        os.chmod(out_dir, 0o700)
    elif stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(out_dir, 0o700)
        print(f"note: {out_dir} was mode {stat.S_IMODE(st.st_mode):04o}, set to 0700", file=sys.stderr)
    staged: list[tuple[str, Path]] = []
    try:
        for path, data in targets:
            staged.append((_stage(path, data), path))
    except BaseException:
        for tmp, _ in staged:
            os.unlink(tmp)
        raise
    _publish(staged, force)
    dir_fd = os.open(out_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return [p for p, _ in targets]


def next_step_line(out_dir: Path) -> str:
    """The command that installs what was written for the system fprintd (as root)."""
    return f"next step: sudo synaptics0078-setup install --from {shlex.quote(str(Path(out_dir).resolve()))}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Write module.bin and program-<serial>.bin for the libfprint synaptics0078 driver (dev use)."
    )
    p.add_argument("--dll", type=Path, default=DEFAULT_DLL, help="vendor DLL (default: %(default)s)")
    p.add_argument(
        "--calibration",
        type=Path,
        default=DEFAULT_CALIBRATION,
        help="decrypted CalibrationData (default: %(default)s)",
    )
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="data directory (default: %(default)s)")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        module, program, serial = build_data(args.dll, args.calibration)
        paths = write_data(args.out, module, program, serial, force=args.force)
    except (calibration.CalibrationError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"serial-derived name: {program_name(serial)}")
    for path in paths:
        data = path.read_bytes()
        print(f"{path}  {len(data)} bytes  sha256 {hashlib.sha256(data).hexdigest()}")
    print(f"driver env: FP_SYNAPTICS0078_DATA_DIR={args.out.resolve()}")
    print(next_step_line(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
