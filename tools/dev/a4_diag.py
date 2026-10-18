"""Finger-off diagnosis: one compact table per touch/lift cycle from a synaptics0078 debug log.

Reads the driver's debug lines (G_MESSAGES_DEBUG=libfprint-synaptics0078) from an a4-capture log
or from a journal export of fprintd (`journalctl -u fprintd -o short-precise`), and prints per
cycle the delivered image's stats, then one row per finger-off probe:

  probe   n-th probe of the cycle
  start   ms from the image to the probe's 02
  finger  ms from the 02 reply to the finger event ("-": none in the window, i.e. finger off)
  ready   the capture event that followed (hex) and its ms after the 02 reply
  51      what the probe's 51 returned: image, capture error, ...
  sd cov grad diff   the driver's "Diag image" stats of the probe image (FP_SYNAPTICS0078_DUMP_DIR
          set): standard deviation, ridge coverage, neighbour gradient, mean absolute difference to
          the last delivered image

Usage: a4_diag.py LOG [LOG ...]. Pure parsing, no device access; tools/test_a4_diag.py tests it.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

TIME_RE = re.compile(r"\b(\d\d):(\d\d):(\d\d\.\d+)")
ARM_RE = re.compile(r"Finger-off probe (\d+): arming")
EVENT_RE = re.compile(r"EP 0x83 event ([0-9a-f]+): (\w+)(?: \(\+(-?\d+) ms after 02\))?")
REPLY_02_RE = re.compile(r"Command 0x02: reply")
IMAGE_REPLY_RE = re.compile(r"Image reply: (\d+) bytes, status (0x[0-9a-f]{4})")
DISCARD_RE = re.compile(r"Finger-off probe \d+: discarding the 51 reply \(([^)]*)\)")
DIAG_RE = re.compile(
    r"Diag image (\d+) (\S+): mean ([\d.]+), sd ([\d.]+), coverage (\d+)%, gradient ([\d.]+), "
    r"diff vs last capture ([\d.]+|-)"
)
OFF_RE = re.compile(r"Finger off after (\d+) probe\(s\), (\d+) ms")
CAPTURED = "Image captured"


@dataclass
class Stats:
    sd: float
    coverage: int
    gradient: float
    diff: float | None


@dataclass
class Probe:
    n: int
    start_ms: int | None = None
    finger_ms: int | None = None
    ready: str | None = None
    ready_ms: int | None = None
    reply: str | None = None
    stats: Stats | None = None


@dataclass
class Cycle:
    capture: Stats | None = None
    probes: list[Probe] = field(default_factory=list)
    off_probes: int | None = None
    off_ms: int | None = None


def line_seconds(line: str) -> float | None:
    """Seconds since midnight of the first HH:MM:SS.fff in the line, None without one."""
    m = TIME_RE.search(line)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def ms_since(start: float | None, now: float | None) -> int | None:
    if start is None or now is None:
        return None
    delta = now - start
    if delta < 0:  # past midnight
        delta += 86400
    return round(delta * 1000)


def parse_stats(m: re.Match) -> Stats:
    diff = None if m.group(7) == "-" else float(m.group(7))
    return Stats(float(m.group(4)), int(m.group(5)), float(m.group(6)), diff)


def parse_log(lines) -> list[Cycle]:
    """Cycles in log order. A cycle opens at probe 1 and closes at "Finger off after"."""
    cycles: list[Cycle] = []
    cycle: Cycle | None = None
    probe: Probe | None = None
    capture_stats: Stats | None = None
    captured_at: float | None = None
    reply_02_at: float | None = None
    for line in lines:
        now = line_seconds(line)
        if REPLY_02_RE.search(line):
            reply_02_at = now
            continue
        m = DIAG_RE.search(line)
        if m:
            stats = parse_stats(m)
            if m.group(2) == "capture":
                capture_stats = stats
            elif probe is not None:
                probe.stats = stats
            continue
        if CAPTURED in line:
            captured_at = now
            continue
        m = ARM_RE.search(line)
        if m:
            n = int(m.group(1))
            if n == 1 or cycle is None:
                cycle = Cycle(capture=capture_stats)
                cycles.append(cycle)
                capture_stats = None
            probe = Probe(n, start_ms=ms_since(captured_at, now))
            cycle.probes.append(probe)
            reply_02_at = None
            continue
        m = EVENT_RE.search(line)
        if m and probe is not None:
            hexa, action = m.group(1), m.group(2)
            latency = int(m.group(3)) if m.group(3) is not None else ms_since(reply_02_at, now)
            if action == "finger" and probe.finger_ms is None:
                probe.finger_ms = latency
            elif action in ("ready", "fail", "extend") and probe.finger_ms is not None and probe.ready is None:
                probe.ready, probe.ready_ms = hexa, latency
            continue
        m = IMAGE_REPLY_RE.search(line)
        if m and probe is not None and probe.reply is None:
            probe.reply = f"{m.group(1)}B/{m.group(2)}"
            continue
        m = DISCARD_RE.search(line)
        if m and probe is not None:
            probe.reply = m.group(1) if probe.reply is None else f"{m.group(1)} {probe.reply}"
            continue
        m = OFF_RE.search(line)
        if m and cycle is not None:
            cycle.off_probes, cycle.off_ms = int(m.group(1)), int(m.group(2))
            cycle, probe = None, None
    return cycles


def fmt_stats(s: Stats | None) -> str:
    if s is None:
        return f"{'-':>6} {'-':>4} {'-':>5} {'-':>5}"
    diff = "-" if s.diff is None else f"{s.diff:.1f}"
    return f"{s.sd:6.1f} {s.coverage:3d}% {s.gradient:5.1f} {diff:>5}"


def fmt_ms(v: int | None) -> str:
    return "-" if v is None else str(v)


def format_table(cycles: list[Cycle]) -> str:
    out = []
    for i, c in enumerate(cycles, 1):
        off = (
            f"finger off after {c.off_probes} probe(s), {c.off_ms} ms"
            if c.off_ms is not None
            else "no finger-off report (interrupted?)"
        )
        image = "no stats" if c.capture is None else f"sd/cov/grad {fmt_stats(c.capture).strip()}"
        out.append(f"cycle {i}: {off}; image {image}")
        out.append(
            f"  {'probe':>5} {'start':>6} {'finger':>6} {'ready':>15} {'51':<28} "
            f"{'sd':>6} {'cov':>4} {'grad':>5} {'diff':>5}"
        )
        for p in c.probes:
            ready = "-" if p.ready is None else f"{p.ready}@{fmt_ms(p.ready_ms)}"
            out.append(
                f"  {p.n:5d} {fmt_ms(p.start_ms):>6} {fmt_ms(p.finger_ms):>6} {ready:>15} "
                f"{(p.reply or '-')[:28]:<28} {fmt_stats(p.stats)}"
            )
    if not cycles:
        out.append("no finger-off probing found (is G_MESSAGES_DEBUG=libfprint-synaptics0078 set?)")
    else:
        offs = [c.off_ms for c in cycles if c.off_ms is not None]
        counts = [c.off_probes for c in cycles if c.off_probes is not None]
        if offs:
            out.append(
                f"summary: {len(cycles)} cycle(s), finger off {min(offs)}..{max(offs)} ms, "
                f"probes {min(counts)}..{max(counts)}"
            )
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: a4_diag.py LOG [LOG ...]", file=sys.stderr)
        return 2
    for path in argv:
        with open(path, encoding="utf-8", errors="replace") as fh:
            print(f"== {path}")
            print(format_table(parse_log(fh)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
