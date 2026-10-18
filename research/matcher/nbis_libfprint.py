#!/usr/bin/env python3
"""Score a capture set with libfprint's own NBIS pipeline (mindtct + bozorth3).

The images go through libfprint's virtual_image driver (FP_VIRTUAL_IMAGE socket)
via GObject introspection (FPrint-2.0), so minutiae extraction, enrollment and
verification run exactly as fprintd would run them for an FpImageDevice. The
bozorth3 score of each comparison is read from libfprint's own debug output
("score N/40", fpi_print_bz3_match), so any threshold can be applied afterwards.

For every image g a template is enrolled from 5 copies of g (an image device has
5 enroll stages); every image p is then verified against every template, giving
the full matrix S[p, g]. Because fpi_print_bz3_match accepts when any enrolled
view reaches the threshold, multi-image enrollment equals the max over S entries;
--crosscheck re-checks that with real 5-image libfprint enrollments.

Variants. "stock" uses the installed libfprint unchanged. Variants with driver
flags or a driver-side resize need the patched build from build_libfprint.sh: its
virtual_image driver reads FP_VIRTUAL_IMAGE_FLAGS=partial,inverted and
FP_VIRTUAL_IMAGE_RESIZE=N and applies them the way egis0570/elanspi treat their
frames (FPI_IMAGE_PARTIAL makes mindtct drop perimeter minutiae; the resize is
libfprint's fpi_image_resize, pixman bilinear). Host-side preprocessing (see
preprocess.py) is applied before the image is sent.

Usage:
  nbis_libfprint.py CAPTURES [CAPTURES...] --out DIR [--variants all|v1,v2]
                    [--libfprint-build DIR] [--crosscheck N] [--ks 3,5]
  nbis_libfprint.py --list-variants

Results (score matrices are biometric-derived; keep DIR out of git):
  DIR/nbis-<variant>.json            matrix, minutiae counts, failures, provenance
  DIR/nbis-<variant>.analysis.json   fpscore.analyze() output
  DIR/nbis-<variant>.md              per-variant tables
  DIR/nbis-summary.md                one row per variant
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fpdata  # noqa: E402
import fpscore  # noqa: E402

# name: (host preprocessing, driver resize factor, driver flags, needs patched build)
VARIANTS: dict[str, tuple[str, int, str, bool]] = {
    "stock":              ("", 1, "", False),
    "partial":            ("", 1, "partial", True),
    "up2":                ("", 2, "", True),
    "up2-partial":        ("", 2, "partial", True),
    "up3-partial":        ("", 3, "partial", True),
    "inverted-up2-partial": ("", 2, "partial,inverted", True),
    "clahe-up2-partial":  ("clahe", 2, "partial", True),
    "mask-up2-partial":   ("mask25", 2, "partial", True),
    "gabor":              ("gabor", 1, "", False),
    "gabor-up2-partial":  ("gabor", 2, "partial", True),
    "gabor-up3-partial":  ("gabor", 3, "partial", True),
    "stock-cubic2":       ("up2cubic", 1, "", False),
}
THRESHOLDS = [40, 30, 25, 24, 20, 15, 12, 10]
SCORE_RE = re.compile(rb"score (-?\d+)/(\d+)")


# ---------------------------------------------------------------- worker side

class Rig:
    """One opened virtual_image device plus the socket feeding it."""

    def __init__(self, sock_path: str, log_path: Path):
        from gi.repository import FPrint, GLib  # imported after env + fd setup
        self.FPrint, self.GLib = FPrint, GLib
        self.log_path = log_path
        self.ctx = GLib.main_context_default()
        self.fpctx = FPrint.Context()
        devs = [d for d in self.fpctx.get_devices() if d.get_driver() == "virtual_image"]
        if not devs:
            raise SystemExit("libfprint has no virtual_image device (FP_VIRTUAL_IMAGE unset or driver missing)")
        self.dev = devs[0]
        self.dev.open_sync()
        self.con = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.con.connect(sock_path)
        self.nr_stages = self.dev.get_nr_enroll_stages()

    def close(self):
        self.con.close()
        self.dev.close_sync()

    def _iterate_until(self, cond, timeout: float = 30.0):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                raise TimeoutError("libfprint did not respond")
            if not self.ctx.iteration(False):
                time.sleep(0.0005)

    def _wait_needed(self):
        F = self.FPrint.FingerStatusFlags
        self._iterate_until(lambda: self.dev.get_finger_status() & F.NEEDED)

    def _send(self, img):
        # libfprint reads the socket from the main loop, so a frame larger than the
        # socket buffer must be fed while iterating it (a blocking sendall deadlocks).
        h, w = img.shape
        data = memoryview(struct.pack("ii", w, h) + img.tobytes())
        self.con.setblocking(False)
        try:
            while data:
                try:
                    data = data[self.con.send(data):]
                except BlockingIOError:
                    if not self.ctx.iteration(False):
                        time.sleep(0.0005)
        finally:
            self.con.setblocking(True)

    def _log_offset(self) -> int:
        return self.log_path.stat().st_size

    def _scores_since(self, off: int) -> list[int]:
        with open(self.log_path, "rb") as f:
            f.seek(off)
            return [int(m.group(1)) for m in SCORE_RE.finditer(f.read())]

    def enroll(self, images) -> tuple[object | None, list[int | None], int]:
        """Enroll ``images`` in order (len must equal the stage count).

        Returns (print or None, minutiae count per accepted stage, retries).
        Stops at the first retry: the template is then incomplete and None."""
        st = {"progress": 0, "done": False, "print": None, "error": None, "retry": False,
              "minutiae": []}

        def progress_cb(*args):  # (device, completed_stages, print, user_data[, error])
            st["progress"] += 1
            err = next((a for a in args if isinstance(a, self.GLib.Error)), None)
            fp = next((a for a in args if isinstance(a, self.FPrint.Print)), None)
            if err is not None or args[1] < st["progress"]:
                st["retry"] = True
            elif fp is not None and fp.props.image is not None:
                st["minutiae"].append(len(fp.props.image.get_minutiae()))

        def done_cb(dev, res):
            try:
                st["print"] = dev.enroll_finish(res)
            except self.GLib.Error as e:
                st["error"] = e
            st["done"] = True

        from gi.repository import Gio
        cancel = Gio.Cancellable()
        tmpl = self.FPrint.Print.new(self.dev)
        tmpl.props.finger = self.FPrint.Finger.RIGHT_INDEX
        tmpl.props.username = "matcher-eval"
        self.dev.enroll(tmpl, cancel, progress_cb, tuple(), done_cb)
        for n, img in enumerate(images, 1):
            self._wait_needed()
            self._send(img)
            self._iterate_until(lambda: st["progress"] >= n or st["done"])
            if st["retry"]:
                cancel.cancel()
                self._iterate_until(lambda: st["done"])
                return None, st["minutiae"], 1
        self._iterate_until(lambda: st["done"])
        if st["error"] is not None:
            raise RuntimeError(f"enroll failed: {st['error'].message}")
        return st["print"], st["minutiae"], 0

    def verify(self, template, img) -> tuple[bool | None, list[int], int | None]:
        """Verify ``img``; returns (match or None on retry, logged scores, probe minutiae)."""
        st = {"done": False, "match": None, "fp": None, "error": None}

        def done_cb(dev, res):
            try:
                st["match"], st["fp"] = dev.verify_finish(res)
            except self.GLib.Error as e:
                st["error"] = e
            st["done"] = True

        off = self._log_offset()
        self.dev.verify(template, callback=done_cb)
        self._wait_needed()
        self._send(img)
        self._iterate_until(lambda: st["done"])
        scores = self._scores_since(off)
        nmin = None
        if st["fp"] is not None and st["fp"].props.image is not None:
            nmin = len(st["fp"].props.image.get_minutiae())
        if st["error"] is not None:
            return None, scores, nmin
        return bool(st["match"]), scores, nmin


def loaded_libfprint() -> str:
    for line in Path("/proc/self/maps").read_text().splitlines():
        if "libfprint-2.so" in line:
            return line.split()[-1]
    return "?"


def worker(args) -> None:
    import numpy as np
    import preprocess

    out = Path(args.out)
    variant = args.worker
    pre, resize, flags, _ = VARIANTS[variant]
    log_path = out / f"nbis-{variant}.glib.log"
    # GLib writes debug lines to stdout and warnings to stderr; send both to the
    # log file and keep Python's own output on the original descriptors.
    keep_out, keep_err = os.dup(1), os.dup(2)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    sys.stdout = os.fdopen(keep_out, "w", buffering=1)
    sys.stderr = os.fdopen(keep_err, "w", buffering=1)

    import gi
    gi.require_version("FPrint", "2.0")

    samples = fpdata.load_captures(args.captures)
    imgs = [preprocess.apply(s.image, pre) if pre else s.image for s in samples]
    rig = Rig(os.environ["FP_VIRTUAL_IMAGE"], log_path)
    lib = loaded_libfprint()
    n = len(samples)
    t_start = time.monotonic()

    templates, enroll_minutiae, fte = [], [], []
    for i in range(n):
        tp, mins, retries = rig.enroll([imgs[i]] * rig.nr_stages)
        templates.append(tp)
        enroll_minutiae.append(mins[0] if mins else None)
        if tp is None:
            fte.append(samples[i].name)
    S = np.full((n, n), np.nan)
    probe_minutiae: list[int | None] = [None] * n
    retries = 0
    for p in range(n):
        for g in range(n):
            if templates[g] is None:
                continue
            match, scores, nmin = rig.verify(templates[g], imgs[p])
            if nmin is not None:
                probe_minutiae[p] = nmin
            if match is None or not scores:
                retries += 1
                continue
            S[p, g] = scores[0]  # all stored views are copies of image g
            if bool(match) != (scores[0] >= 40):
                raise RuntimeError(f"decision/score mismatch at {p},{g}: {match} {scores}")
        print(f"[{variant}] probe {p + 1}/{n} done", flush=True)

    cross = []
    if args.crosscheck:
        import random
        rng = random.Random(1)
        fingers = sorted({s.finger for s in samples})
        for f in fingers:
            members = [i for i, s in enumerate(samples) if s.finger == f and templates[i] is not None]
            if len(members) <= rig.nr_stages:
                continue
            for _ in range(args.crosscheck):
                E = rng.sample(members, rig.nr_stages)
                tp, _, r = rig.enroll([imgs[e] for e in E])
                if tp is None:
                    continue
                for p in range(n):
                    if p in E:
                        continue
                    match, scores, _ = rig.verify(tp, imgs[p])
                    if match is None:
                        continue
                    expect_scores = []
                    for e in E:  # libfprint stops at the first view reaching the threshold
                        expect_scores.append(int(S[p, e]))
                        if S[p, e] >= 40:
                            break
                    cross.append({"enrolled": [samples[e].name for e in E], "probe": samples[p].name,
                                  "match": match, "expected_match": bool(np.nanmax(S[p, E]) >= 40),
                                  "scores": scores, "expected_scores": expect_scores})
    rig.close()
    res = {
        "method": "libfprint NBIS (mindtct + bozorth3) via virtual_image",
        "variant": variant, "preprocess": pre, "driver_resize": resize, "driver_flags": flags,
        "libfprint": lib, "enroll_stages": rig.nr_stages,
        "names": [s.name for s in samples], "fingers": [s.finger for s in samples],
        "coverage": [s.coverage for s in samples],
        "matrix": S, "enroll_minutiae": enroll_minutiae, "probe_minutiae": probe_minutiae,
        "failed_to_enroll": fte, "verify_retries": retries,
        "crosscheck": cross, "seconds": time.monotonic() - t_start,
    }
    fpdata.save_json(out / f"nbis-{variant}.json", res)


# ---------------------------------------------------------------- parent side

def run_variant(args, variant: str, build: Path | None) -> Path:
    pre, resize, flags, patched = VARIANTS[variant]
    env = dict(os.environ)
    tmp = tempfile.mkdtemp(prefix="fpvi-")
    env.update({"FP_VIRTUAL_IMAGE": os.path.join(tmp, "vi.socket"),
                "FP_VIRTUAL_IMAGE_HOT_SECONDS": "-1",  # disable the thermal model
                "G_MESSAGES_DEBUG": "all"})
    for k in ("FP_VIRTUAL_IMAGE_FLAGS", "FP_VIRTUAL_IMAGE_RESIZE", "LD_LIBRARY_PATH"):
        env.pop(k, None)
    if patched:
        if build is None:
            raise SystemExit(f"variant {variant} needs --libfprint-build (see build_libfprint.sh)")
        env["LD_LIBRARY_PATH"] = str(build / "libfprint")
        env["FP_VIRTUAL_IMAGE_FLAGS"] = flags
        env["FP_VIRTUAL_IMAGE_RESIZE"] = str(resize)
    cmd = [sys.executable, str(Path(__file__).resolve()), *args.captures, "--out", str(args.out),
           "--worker", variant, "--crosscheck", str(args.crosscheck)]
    subprocess.run(cmd, env=env, check=True)
    path = Path(args.out) / f"nbis-{variant}.json"
    res = json.loads(path.read_text())
    if patched and str(build) not in res["libfprint"]:
        raise SystemExit(f"{variant}: expected the patched libfprint, loaded {res['libfprint']}")
    return path


def analyze_file(path: Path, ks) -> tuple[dict, dict]:
    import numpy as np
    res = json.loads(path.read_text())
    S = np.array(res["matrix"], dtype=float)
    ana = fpscore.analyze(S, res["fingers"], THRESHOLDS, ks=ks, symmetric=False)
    ana["minutiae"] = fpscore.stats(np.array([m for m in res["enroll_minutiae"] if m is not None], float))
    cc = res.get("crosscheck") or []
    ana["crosscheck"] = {"n": len(cc),
                         "decision_agree": sum(c["match"] == c["expected_match"] for c in cc),
                         "scores_agree": sum(c["scores"] == c["expected_scores"] for c in cc)}
    base = path.with_suffix("")
    fpdata.save_json(base.with_suffix(".analysis.json"), ana)
    v = res["variant"]
    extra = (f"libfprint `{res['libfprint']}`; host preprocessing `{res['preprocess'] or 'none'}`, "
             f"driver resize x{res['driver_resize']}, driver flags `{res['driver_flags'] or 'none'}`. "
             f"Minutiae per image: median {fpscore.fmt(ana['minutiae'].get('median', float('nan')))} "
             f"(min {fpscore.fmt(ana['minutiae'].get('min', float('nan')))}, "
             f"max {fpscore.fmt(ana['minutiae'].get('max', float('nan')))}); "
             f"failed to enroll: {len(res['failed_to_enroll'])}; verify retries: {res['verify_retries']}.")
    if cc:
        extra += (f" Cross-check with real {res['enroll_stages']}-image enrollments: "
                  f"{ana['crosscheck']['decision_agree']}/{len(cc)} decisions and "
                  f"{ana['crosscheck']['scores_agree']}/{len(cc)} logged score sequences match the matrix.")
    (base.with_suffix(".md")).write_text(fpscore.markdown(f"NBIS / libfprint: {v}", ana, extra))
    return res, ana


def summary_rows(results) -> str:
    lines = ["| variant | minutiae (median) | genuine median | impostor max | pair FRR @0FA | "
             "pair FRR @40 | pair FAR @25 | enroll-5 FRR @0FA (thr) | enroll-5 FRR @40 | enroll-5 FAR @25 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for v, res, ana in results:
        at = {a["threshold"]: a for a in ana["at"]}
        e5 = ana["enroll"].get("5", {})
        e5at = {a["threshold"]: a for a in e5.get("at", [])}
        z5 = e5.get("zero_fa", {})
        lines.append(
            f"| {v} | {fpscore.fmt(ana['minutiae'].get('median', float('nan')))} | "
            f"{fpscore.fmt(ana['genuine']['median'])} | {fpscore.fmt(ana['impostor']['max'])} | "
            f"{fpscore.pct(ana['zero_fa']['frr'])} | {fpscore.pct(at[40]['frr'])} | {fpscore.pct(at[25]['far'])} | "
            f"{fpscore.pct(z5.get('frr', float('nan')))} ({fpscore.fmt(z5.get('threshold', float('nan')))}) | "
            f"{fpscore.pct(e5at.get(40, {}).get('frr', float('nan')))} | {fpscore.pct(e5at.get(25, {}).get('far', float('nan')))} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="*", help="finger folder(s) or their parent folder")
    ap.add_argument("--out", help="results directory (outside git, or git-ignored)")
    ap.add_argument("--variants", default="all", help="comma-separated variant names, or all")
    ap.add_argument("--libfprint-build", type=Path, help="build dir from build_libfprint.sh")
    ap.add_argument("--crosscheck", type=int, default=0,
                    help="real 5-image enrollments per finger to cross-check the max-fusion model")
    ap.add_argument("--ks", default="3,5", help="enrollment sizes to simulate")
    ap.add_argument("--analyze-only", action="store_true", help="re-analyse existing nbis-*.json")
    ap.add_argument("--allow-tracked", action="store_true")
    ap.add_argument("--list-variants", action="store_true")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.list_variants:
        for k, (pre, rs, fl, pt) in VARIANTS.items():
            print(f"{k:24s} pre={pre or '-':10s} resize=x{rs} flags={fl or '-':18s} "
                  f"{'patched build' if pt else 'installed libfprint'}")
        return
    if not args.captures or not args.out:
        ap.error("CAPTURES and --out are required")
    if args.worker:
        worker(args)
        return
    args.out = fpdata.check_out_dir(args.out, args.allow_tracked)
    variants = list(VARIANTS) if args.variants == "all" else args.variants.split(",")
    ks = tuple(int(k) for k in args.ks.split(","))
    build = args.libfprint_build.resolve() if args.libfprint_build else None
    results = []
    for v in variants:
        if v not in VARIANTS:
            raise SystemExit(f"unknown variant {v}; see --list-variants")
        path = Path(args.out) / f"nbis-{v}.json"
        if not args.analyze_only:
            if VARIANTS[v][3] and build is None:
                print(f"skipping {v}: needs --libfprint-build", file=sys.stderr)
                continue
            t = time.monotonic()
            run_variant(args, v, build)
            print(f"{v}: {time.monotonic() - t:.0f}s", flush=True)
        if path.exists():
            res, ana = analyze_file(path, ks)
            results.append((v, res, ana))
    table = summary_rows(results)
    (Path(args.out) / "nbis-summary.md").write_text(table)
    print(table)


if __name__ == "__main__":
    main()
