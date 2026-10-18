# SPDX-License-Identifier: MIT
"""The capture module and the template out of a vendor file, gated only by the pins.

research/steps/b5-setup-design.md, decisions 2-4 and 6. Accepted input, detected by
magic bytes, or by exact size and pin for the bare files:

  MSCF                a cab (Microsoft Update Catalog): members via bsdtar
  PK\\x03\\x04          a zip (Origin PC): members via zipfile
  MZ                  a PE: the DLL itself (scanned for the module, template built from
                      it), or a setup exe holding MSIs (OLE files) and cabs
  D0CF11E0A1B11AE1    an OLE / MSI file: its cab streams via cfb.py
  10500 B             the bare module (its sha256 pinned)
  18869 B             the bare template (its sha256 pinned)

Containers are followed recursively (the Origin chain is zip -> exe -> MSI -> cab ->
DLL), with caps on the depth, the member count, each member's size and the total bytes
read. Everything read is untrusted:
- bsdtar always reads the archive from stdin and writes one member to stdout
  (`bsdtar -xOqf - -- MEMBER`): a member name never becomes a path. Only data that
  starts with MSCF is handed to it. That keeps libarchive's detection off the readers
  that open files by name (mtree) for most inputs, but it is not a guarantee of a cab:
  a ustar archive whose first member is named MSCF starts with MSCF\\0\\0\\0\\0 and bsdtar
  reads it as a tar. That is harmless here: an archive not known by its sha256 is
  parsed only by the unprivileged child, and whatever comes out must match the pins.
  bsdtar's output is size-capped and time-limited. The executable is /usr/bin/bsdtar
  (tests pass another path as an argument; no environment variable changes it).
- Nothing is written to disk, nothing found is executed.
- In every PE (DLL or setup exe) the module is found by the sha256 of its first 32
  bytes at every 16th offset, then at every offset, then its full sha256 (pins.py; in a
  setup exe the every-offset pass runs after its containers); independently, the
  template is built by template.build_from_dll and must hash to its pin.

Unprivileged child (decision 4): the setup tool runs, as an unprivileged user,

    python3 -IB -c "import sys; sys.path.insert(0, ROOT); from synaptics0078 import vendor; sys.exit(vendor.child_entry())"

with the archive on stdin. child_entry() reads at most MAX_INPUT bytes, runs extract()
and writes the result to stdout in this framing (integers big endian):

    b"S0078X1\\n"               8-byte magic
    records, each: 1-byte kind, u32 length, payload
      b"M"  the module candidate     exactly pins.module_len bytes, at most one
      b"T"  the template candidate   exactly pins.template_len bytes, at most one
      b"P"  one provenance line      UTF-8, at most MAX_TEXT bytes, at most MAX_LINES records
      b"E"  an error line            UTF-8, at most MAX_TEXT bytes (the child failed)
      b"Z"  end                      length 0; the last record, nothing may follow
    the whole output at most MAX_CHILD_OUTPUT bytes

The parent calls parse_child_output(), which refuses any framing error and re-applies
every pin itself: the child is untrusted. The parent accepts nothing but the two pinned
records; it never scans or rebuilds anything out of the child's bytes (B5a also sent
the DLLs, b"D", for a re-scan as root; that record is gone and is now refused as an
unknown kind).

Optional child arguments: --pins FILE (a pins.Pins.to_json document, for synthetic test
fixtures; the parent's own pins are the gate either way) and --bsdtar PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import lzma
import os
import selectors
import signal
import struct
import subprocess
import sys
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field

from . import cfb
from . import pins as _pins
from . import template as _template

DEFAULT_PINS = _pins.DEFAULT_PINS
BSDTAR = "/usr/bin/bsdtar"
MAGIC_CAB = b"MSCF"
MAGIC_ZIP = b"PK\x03\x04"
MAGIC_MZ = b"MZ"

MAX_INPUT = 64 << 20  # one input file
MAX_DEPTH = 6  # zip -> exe -> MSI -> cab -> DLL is depth 4
MAX_MEMBERS = 512  # per cab or zip
MAX_MEMBER_BYTES = 64 << 20  # one extracted member
MAX_TOTAL_BYTES = 512 << 20  # everything one extract() reads out of containers
MAX_CARVED_CABS = 8  # cabs cut out of one PE at their MSCF signature
BSDTAR_TIMEOUT_S = 120
MAX_STDERR = 4096

CHILD_MAGIC = b"S0078X1\n"
MAX_TEXT = 512
MAX_LINES = 256
# magic, M (10500 B) and T (18869 B), MAX_LINES P lines and one E line of at most
# 5 + MAX_TEXT bytes each, Z: about 160 KB. The cap leaves room for other pins' lengths.
MAX_CHILD_OUTPUT = 1 << 20
_KINDS = frozenset(b"MTPEZ")

_ZIP_READ_ERRORS = (
    zipfile.BadZipFile,
    NotImplementedError,  # a compression method zipfile lacks
    RuntimeError,  # an encrypted member
    ValueError,
    OSError,
    EOFError,
    zlib.error,
    lzma.LZMAError,
)
_STREAM_MAGICS = (MAGIC_CAB, MAGIC_MZ, cfb.SIGNATURE)  # the OLE streams worth reading
KIND_LABEL = {"cab": "cab", "zip": "zip", "pe": "PE", "ole": "OLE", "module": "module", "template": "template"}


class VendorError(Exception):
    """The input cannot be handled at all (too large, bsdtar missing) or a child output is refused."""


@dataclass
class Extracted:
    """What one vendor file gave. module and template are pinned; provenance holds no bytes."""

    module: bytes | None = field(default=None, repr=False)
    template: bytes | None = field(default=None, repr=False)
    provenance: list[str] = field(default_factory=list)  # the steps, e.g. "cab -> x.dll -> module at 0x19cca0"
    module_source: str = ""  # the provenance line of the module
    template_source: str = ""  # the provenance line of the template

    @property
    def complete(self) -> bool:
        return self.module is not None and self.template is not None


# ------------------------------------------------------------------ helpers


def _safe(text: str, limit: int = 160) -> str:
    """A name from an archive, fit for a log or a terminal: printable ASCII only, capped."""
    out = "".join(c if 32 <= ord(c) < 127 else "?" for c in text)
    return out if len(out) <= limit else out[: limit - 3] + "..."


def _sha(data) -> str:
    return hashlib.sha256(data).hexdigest()


def is_module(data: bytes, pins: _pins.Pins = DEFAULT_PINS) -> bool:
    return len(data) == pins.module_len and _sha(data) == pins.module_sha256


def is_template(data: bytes, pins: _pins.Pins = DEFAULT_PINS) -> bool:
    return len(data) == pins.template_len and _sha(data) == pins.template_sha256


def detect(data: bytes, pins: _pins.Pins = DEFAULT_PINS) -> str | None:
    """ "cab", "zip", "pe", "ole", "module", "template" or None, by magic bytes, then by size and pin."""
    head = bytes(data[:8])
    if head[:4] == MAGIC_CAB:
        return "cab"
    if head[:4] == MAGIC_ZIP:
        return "zip"
    if head == cfb.SIGNATURE:
        return "ole"
    if head[:2] == MAGIC_MZ:
        return "pe"
    if is_module(data, pins):
        return "module"
    if is_template(data, pins):
        return "template"
    return None


def is_dll(data: bytes) -> bool:
    """True for a PE whose COFF header has IMAGE_FILE_DLL; False for anything else (bounds-checked)."""
    if len(data) < 0x40 or data[:2] != MAGIC_MZ:
        return False
    (e_lfanew,) = struct.unpack_from("<I", data, 0x3C)
    if e_lfanew + 24 > len(data) or data[e_lfanew : e_lfanew + 4] != b"PE\0\0":
        return False
    (characteristics,) = struct.unpack_from("<H", data, e_lfanew + 22)
    return bool(characteristics & 0x2000)


def find_module(data: bytes, pins: _pins.Pins = DEFAULT_PINS, strides: tuple[int, ...] = (16, 1)) -> int | None:
    """Offset of the pinned module in `data`, or None. Head-hash prefilter, then the full sha256.

    Each stride is a full pass (16 first: every known offset is 32-byte aligned).
    """
    size, head_len = pins.module_len, pins.module_head_len
    if len(data) < size or not 0 < head_len <= size:
        return None
    head = bytes.fromhex(pins.module_head_sha256)
    mv, sha, last = memoryview(data), hashlib.sha256, len(data) - size
    for stride in strides:
        for off in range(0, last + 1, stride):
            if sha(mv[off : off + head_len]).digest() == head and sha(mv[off : off + size]).hexdigest() == (
                pins.module_sha256
            ):
                return off
    return None


def _exited(pid: int, deadline: float) -> bool:
    """Wait until `pid` has exited, at most until `deadline`, WITHOUT reaping it.

    The zombie keeps its pid (and so its process group id) reserved until it is reaped,
    so the killpg that follows can never hit an unrelated group that reused the number.
    """
    while True:
        try:
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return True
        except ChildProcessError:  # already reaped elsewhere
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _stop(proc: subprocess.Popen, group: bool) -> None:
    """SIGKILL the child, then reap it: its whole process group when it leads one
    (start_new_session), else the child alone (it shares the caller's group)."""
    try:
        if group:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()  # polls first: a child already reaped is not signalled
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()  # a second interrupt here skips closing the pipes: harmless, the tool exits


def _feed(fh, data: bytes) -> None:
    try:
        fh.write(data)
    except OSError:  # the reader stopped early (BrokenPipe): its exit status says why
        pass
    finally:
        try:
            fh.close()
        except OSError:
            pass


def _amount(n: int) -> str:
    return f"{n >> 20} MB" if n and not n % (1 << 20) else f"{n} bytes"


def run_capped(
    argv: list[str], data: bytes, cap: int, timeout: float, *, new_session: bool = True
) -> tuple[int, bytes, str]:
    """(returncode, stdout, stderr text) of argv with `data` on stdin; stdout capped, time-limited.

    By default the child starts in a new session, so it leads its own process group; on
    every way out (normal exit, timeout, output over `cap`, an error here) that whole group
    is killed with SIGKILL before the child is reaped, so nothing that stayed in the group
    outlives the call (a descendant that starts a session of its own escapes it). With
    new_session=False (the unprivileged extractor, itself run through this) the child
    stays in the caller's group, so the caller's own killer reaches it and what it
    started; here only the child itself is killed and reaped. Raises VendorError on a
    timeout or when stdout grows past `cap` bytes; OSError when argv[0] cannot be run.
    The setup tool runs its unprivileged extractor through this too.
    """
    writer = None  # all setup comes before Popen: the try must open as soon as it returns
    out, err = bytearray(), bytearray()
    tool = os.path.basename(argv[0])
    too_long = f"{tool} took longer than {timeout:g} s"
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/",
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
        close_fds=True,
        start_new_session=new_session,
    )
    try:
        writer = threading.Thread(target=_feed, args=(proc.stdin, data), daemon=True)
        writer.start()
        with selectors.DefaultSelector() as sel:
            sel.register(proc.stdout, selectors.EVENT_READ)
            sel.register(proc.stderr, selectors.EVENT_READ)
            while sel.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise VendorError(too_long)
                for key, _ in sel.select(left):
                    chunk = os.read(key.fd, 1 << 16)
                    if not chunk:
                        sel.unregister(key.fileobj)
                    elif key.fileobj is proc.stdout:
                        out += chunk
                        if len(out) > cap:
                            raise VendorError(f"{tool} wrote more than {_amount(cap)}")
                    elif len(err) < MAX_STDERR:
                        err += chunk
        if not _exited(proc.pid, max(time.monotonic() + 1.0, deadline)):
            raise VendorError(too_long)
    finally:
        _stop(proc, new_session)  # also on a normal exit: a descendant left in its group dies here
        if writer is not None and writer.ident is not None:
            writer.join(timeout=5)
        if writer is None or not writer.is_alive():  # never started: the pipe is still ours to close
            try:
                proc.stdin.close()
            except OSError:
                pass
        for fh in (proc.stdout, proc.stderr):
            fh.close()
    return proc.returncode, bytes(out), err[:MAX_STDERR].decode("utf-8", "replace")


def _escape_pattern(name: str) -> str:
    """A member name as a bsdtar pattern that matches only itself."""
    return "".join("\\" + c if c in "*?[]\\" else c for c in name)


def _member_order(name: str) -> tuple[int, str]:
    low = name.lower()
    if "synawudfbiousb" in low:
        return 0, low
    if low.endswith((".dll", "_dll_x64", ".exe", ".msi", ".cab")):
        return 1, low
    return 2, low


# ------------------------------------------------------------------- walker


class _Walker:
    def __init__(self, pins: _pins.Pins, bsdtar: str, new_session: bool = True):
        self.pins, self.bsdtar, self.new_session = pins, bsdtar, new_session
        self.result = Extracted()
        self.total = 0
        self.exhausted = False

    def note(self, line: str) -> None:
        if len(self.result.provenance) < MAX_LINES:
            self.result.provenance.append(line)

    @property
    def done(self) -> bool:
        return self.result.complete or self.exhausted

    def charge(self, n: int, path: str) -> bool:
        """Count `n` bytes read out of a container against the total cap."""
        self.total += n
        if self.total > MAX_TOTAL_BYTES:
            if not self.exhausted:
                self.note(f"{path}: stopped, more than {MAX_TOTAL_BYTES >> 20} MB read out of containers")
            self.exhausted = True
            return False
        return True

    def visit(self, data: bytes, path: str, depth: int) -> None:
        if self.done:
            return
        if depth > MAX_DEPTH:
            self.note(f"{path}: skipped, nested deeper than {MAX_DEPTH}")
            return
        kind = detect(data, self.pins)
        here = path
        if kind == "module":
            self.found_module(data, f"{here}: the bare module")
        elif kind == "template":
            self.found_template(data, f"{here}: the bare template")
        elif kind == "cab":
            self.cab(data, here, depth)
        elif kind == "zip":
            self.zip(data, here, depth)
        elif kind == "pe":
            self.pe(data, here, depth)
        elif kind == "ole":
            self.ole(data, 0, here, depth)
        elif depth == 0:
            self.note(f"{path}: not a cab, zip, exe, MSI, DLL, module or template")

    def found_module(self, module: bytes, line: str) -> None:
        if self.result.module is None:
            self.result.module, self.result.module_source = bytes(module), line
        self.note(line)

    def found_template(self, tmpl: bytes, line: str) -> None:
        if self.result.template is None:
            self.result.template, self.result.template_source = bytes(tmpl), line
        self.note(line)

    # -- containers ---------------------------------------------------------

    def cab(self, data: bytes, here: str, depth: int) -> None:
        if data[:4] != MAGIC_CAB:  # bsdtar must never pick a format by itself
            raise VendorError("internal: bsdtar called on data that is not a cab")
        try:
            rc, out, err = run_capped(
                [self.bsdtar, "-tf", "-"], data, 1 << 20, BSDTAR_TIMEOUT_S, new_session=self.new_session
            )
        except FileNotFoundError:
            raise VendorError(f"{self.bsdtar} not found (it comes with libarchive)") from None
        except OSError as exc:
            raise VendorError(f"cannot run {self.bsdtar}: {exc.strerror or exc}") from None
        except VendorError as exc:
            self.note(f"{here}: cannot list it: {exc}")
            return
        if rc != 0:
            self.note(f"{here}: cannot list it (bsdtar: {_safe(err.strip().splitlines()[-1] if err.strip() else '?')})")
            return
        names = [n for n in out.decode("utf-8", "replace").split("\n") if n]
        if len(names) > MAX_MEMBERS:
            self.note(f"{here}: {len(names)} members, only the first {MAX_MEMBERS} are read")
            names = names[:MAX_MEMBERS]
        for name in sorted(names, key=_member_order):
            if self.done:
                return
            label = f"{here} -> {_safe(name)}"
            if name.startswith("-") or any(ord(c) < 32 or c == "�" for c in name):
                self.note(f"{label}: skipped, a name bsdtar cannot be asked for safely")
                continue
            try:
                rc, member, err = run_capped(
                    [self.bsdtar, "-xOqf", "-", "--", _escape_pattern(name)],
                    data,
                    MAX_MEMBER_BYTES,
                    BSDTAR_TIMEOUT_S,
                    new_session=self.new_session,
                )
            except VendorError as exc:
                self.note(f"{label}: cannot extract it: {exc}")
                continue
            if rc != 0:
                last = err.strip().splitlines()[-1] if err.strip() else "?"
                self.note(f"{label}: cannot extract it (bsdtar: {_safe(last)})")
                continue
            if self.charge(len(member), label):
                self.visit(member, label, depth + 1)

    def zip(self, data: bytes, here: str, depth: int) -> None:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            infos = [i for i in zf.infolist() if not i.is_dir()]
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError, EOFError) as exc:
            self.note(f"{here}: not a readable zip ({_safe(str(exc))})")
            return
        if len(infos) > MAX_MEMBERS:
            self.note(f"{here}: {len(infos)} members, only the first {MAX_MEMBERS} are read")
            infos = infos[:MAX_MEMBERS]
        for info in sorted(infos, key=lambda i: _member_order(i.filename)):
            if self.done:
                return
            label = f"{here} -> {_safe(info.filename)}"
            if info.file_size > MAX_MEMBER_BYTES:
                self.note(f"{label}: skipped, larger than {MAX_MEMBER_BYTES >> 20} MB")
                continue
            try:
                with zf.open(info) as fh:
                    member = fh.read(MAX_MEMBER_BYTES + 1)
            except _ZIP_READ_ERRORS as exc:
                self.note(f"{label}: cannot read it ({_safe(type(exc).__name__)})")
                continue
            if len(member) > MAX_MEMBER_BYTES:
                self.note(f"{label}: skipped, larger than {MAX_MEMBER_BYTES >> 20} MB")
                continue
            if self.charge(len(member), label):
                self.visit(member, label, depth + 1)

    def scan_module(self, data: bytes, here: str, strides: tuple[int, ...]) -> bool:
        """Look for the module in a PE (unless it was already found); True when this PE gave it."""
        if self.result.module is not None:
            return False
        off = find_module(data, self.pins, strides)
        if off is None:
            return False
        self.found_module(data[off : off + self.pins.module_len], f"{here} -> module at 0x{off:x}")
        return True

    def pe(self, data: bytes, here: str, depth: int) -> None:
        """A DLL or a setup exe. The module scan and the template build run on either, independently.

        A DLL gets both scan passes (stride 16, then 1) at once. A setup exe gets the
        stride-16 pass, then its containers are walked, then the stride-1 pass runs if the
        module is still missing: that pass costs about 1.5 s per MB, and the known Origin
        exe gives the module through its MSI, so it never pays for it.
        """
        dll = is_dll(data)
        gave_module = self.scan_module(data, here, (16, 1) if dll else (16,))
        gave_template = False
        if self.result.template is None:
            try:
                tmpl = _template.build_from_dll(data, pins=self.pins)
            except _template.TemplateError as exc:
                if dll or gave_module:
                    self.note(f"{here}: no template ({exc})")
            else:
                self.found_template(tmpl, f"{here} -> template built, sha256 matches the pin")
                gave_template = True
        if dll:
            if not (gave_module or gave_template):
                self.note(f"{here}: a DLL without the module")
            return
        # A setup exe: the MSIs it embeds (OLE files), then any cab stored as is.
        for pos in cfb.find_signatures(data):
            if self.done:
                return
            self.ole(data, pos, f"{here} -> OLE at 0x{pos:x}", depth)
        pos, carved = data.find(MAGIC_CAB), 0
        while pos >= 0 and carved < MAX_CARVED_CABS and not self.done:
            if pos + 12 <= len(data):
                (size,) = struct.unpack_from("<I", data, pos + 8)
                if 36 <= size <= len(data) - pos:
                    carved += 1
                    label = f"{here} -> cab at 0x{pos:x}"
                    if not self.charge(size, label):  # the slice below is a copy: it counts
                        return
                    self.visit(data[pos : pos + size], label, depth + 1)
            pos = data.find(MAGIC_CAB, pos + 1)
        if not self.done:
            self.scan_module(data, here, (1,))

    def ole(self, data: bytes, base: int, here: str, depth: int) -> None:
        """The cab, PE and OLE streams of the compound file at `base`, read one at a time.

        cfb copies out only streams that start like a container, each one counted against
        what is left of the total budget before it is copied (a directory entry repeating
        another's stream costs again), and each one charged here as it is visited.
        """
        try:
            for name, stream in cfb.iter_streams(
                data, base, max_total=max(0, MAX_TOTAL_BYTES - self.total), prefixes=_STREAM_MAGICS
            ):
                if self.done:
                    return
                label = f"{here} -> stream {_safe(name)!r}"
                if self.charge(len(stream), label):
                    self.visit(stream, label, depth + 1)
        except cfb.CfbBudgetError:
            if not self.exhausted:
                self.note(f"{here}: stopped, more than {MAX_TOTAL_BYTES >> 20} MB read out of containers")
            self.exhausted = True
        except cfb.CfbError as exc:
            self.note(f"{here}: skipped ({exc})")


def extract(
    data: bytes, *, name: str = "", pins: _pins.Pins = DEFAULT_PINS, bsdtar: str = BSDTAR, new_session: bool = True
) -> Extracted:
    """The pinned module and template found in a vendor file's bytes (either may be None).

    `name` labels the input in the provenance. Raises VendorError only for an input over
    MAX_INPUT bytes or a missing bsdtar; everything else becomes a provenance line.
    `new_session` is passed to every bsdtar run_capped (False in the child).
    """
    if len(data) > MAX_INPUT:
        raise VendorError(f"the file is larger than {MAX_INPUT >> 20} MB")
    w = _Walker(pins, bsdtar, new_session)
    kind = detect(data, pins)
    label = KIND_LABEL[kind] if kind else "input"
    w.visit(bytes(data), (f"{_safe(name)} ({label})" if kind else _safe(name)) if name else label, 0)
    return w.result


# ------------------------------------------------------------------- child


def _record(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def _text(line: str) -> bytes:
    return _safe(line, MAX_TEXT).encode("ascii")


def encode_child_output(ex: Extracted, error: str = "") -> bytes:
    """The framed child output for an Extracted (see the module docstring)."""
    out = [CHILD_MAGIC]
    if ex.module is not None:
        out.append(_record(b"M", ex.module))
    if ex.template is not None:
        out.append(_record(b"T", ex.template))
    for line in ex.provenance[:MAX_LINES]:
        out.append(_record(b"P", _text(line)))
    if error:
        out.append(_record(b"E", _text(error)))
    out.append(_record(b"Z", b""))
    return b"".join(out)


def child_main(stdin_bytes: bytes, *, pins: _pins.Pins = DEFAULT_PINS, bsdtar: str = BSDTAR) -> bytes:
    """Extract from `stdin_bytes` and return the framed output. Never raises for bad input.

    bsdtar stays in this process's group (new_session=False): the parent's killpg of the
    child's group reaches it and whatever it started.
    """
    try:
        return encode_child_output(extract(stdin_bytes, pins=pins, bsdtar=bsdtar, new_session=False))
    except VendorError as exc:
        return encode_child_output(Extracted(), str(exc))


def child_entry(argv: list[str] | None = None) -> int:
    """The child's main: archive on stdin, framed result on stdout. 0, or 1 when the child failed."""
    ap = argparse.ArgumentParser(prog="synaptics0078.vendor", add_help=False)
    ap.add_argument("--pins", metavar="FILE")
    ap.add_argument("--bsdtar", metavar="PATH", default=BSDTAR)
    try:
        args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        sys.stdout.buffer.write(encode_child_output(Extracted(), "bad arguments"))
        return 1
    try:
        pins = DEFAULT_PINS
        if args.pins:
            with open(args.pins, encoding="utf-8") as fh:
                pins = _pins.Pins.from_json(fh.read(1 << 20))
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
    except (OSError, ValueError) as exc:
        sys.stdout.buffer.write(encode_child_output(Extracted(), f"cannot start: {exc}"))
        return 1
    if len(data) > MAX_INPUT:
        sys.stdout.buffer.write(encode_child_output(Extracted(), f"the file is larger than {MAX_INPUT >> 20} MB"))
        return 1
    out = child_main(data, pins=pins, bsdtar=args.bsdtar)
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


def parse_child_output(data: bytes, *, pins: _pins.Pins = DEFAULT_PINS) -> Extracted:
    """The child's output, checked: framing, sizes, and every pin applied again here.

    Raises VendorError on any framing error (an unknown record kind included), on a
    module or template record that does not have the exact pinned length and sha256, and
    on an error record. Only those two records can give anything: nothing the child sent
    is scanned or rebuilt here (this runs as root, and a scan of attacker-chosen bytes
    costs root CPU). A missing record leaves that field None, so the result is
    incomplete and the source fails.
    """
    if len(data) > MAX_CHILD_OUTPUT:
        raise VendorError("the extractor's output is larger than allowed")
    if data[: len(CHILD_MAGIC)] != CHILD_MAGIC:
        raise VendorError("the extractor's output does not start with the expected header")
    ex = Extracted()
    pos, ended, seen_m, seen_t, lines = len(CHILD_MAGIC), False, False, False, []
    while pos < len(data):
        if ended:
            raise VendorError("the extractor's output continues after its end record")
        if pos + 5 > len(data):
            raise VendorError("the extractor's output ends inside a record header")
        kind = data[pos : pos + 1]
        (n,) = struct.unpack_from(">I", data, pos + 1)
        pos += 5
        if kind[0] not in _KINDS:
            raise VendorError("the extractor's output has an unknown record")
        limit = {
            b"M": pins.module_len,
            b"T": pins.template_len,
            b"P": MAX_TEXT,
            b"E": MAX_TEXT,
            b"Z": 0,
        }[kind]
        if n > limit or pos + n > len(data):
            raise VendorError(f"the extractor's {kind.decode()} record is larger than allowed or truncated")
        payload = bytes(data[pos : pos + n])
        pos += n
        if kind == b"M":
            if seen_m or not is_module(payload, pins):
                raise VendorError("the extractor returned a module that does not have the pinned sha256")
            seen_m, ex.module, ex.module_source = True, payload, "extractor: module (sha256 checked again)"
        elif kind == b"T":
            if seen_t or not is_template(payload, pins):
                raise VendorError("the extractor returned a template that does not have the pinned sha256")
            seen_t, ex.template, ex.template_source = True, payload, "extractor: template (sha256 checked again)"
        elif kind == b"E":
            raise VendorError(f"the extractor failed: {_safe(payload.decode('ascii', 'replace'), MAX_TEXT)}")
        elif kind == b"P":
            if len(lines) >= MAX_LINES:
                raise VendorError("the extractor returned more lines than allowed")
            lines.append(_safe(payload.decode("ascii", "replace"), MAX_TEXT))
        else:
            ended = True
    if not ended:
        raise VendorError("the extractor's output has no end record (truncated)")
    ex.provenance = [f"extractor: {line}" for line in lines]
    return ex


if __name__ == "__main__":
    sys.exit(child_entry())
