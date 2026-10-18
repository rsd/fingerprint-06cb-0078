"""Shared helpers of the B3 false-accept benchmark (research/b3).

The scorer is the A5 harness (research/a5/sigfm_matrix.cpp, built by
research/a5/build_harness.sh against the fork's libsigfm.a). It mirrors the
driver path: nearest-neighbour upscale with build_image()'s loop,
sigfm_extract(), the core's < 25-keypoint gate, and
score[p][g] = sigfm_match_score(info[g], info[p]) (row = probe, column =
enrolled stage), the orientation of fpi_print_sigfm_match().

Everything these helpers write is biometric-derived: it goes under local/
(gitignored), directories 0700, files 0600.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
# Compiled into libfprint/drivers/synaptics0078.c (A5 decision, brief D6).
THRESHOLD = 105
SCALE = 2
STAGES = 18
PACKAGE = "libfprint-sigfm-0078"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def private_dir(path: Path) -> Path:
    """mkdir -p with mode 0700 for every directory created."""
    path = Path(path)
    if not path.is_dir():
        private_dir(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
    return path


def write_private(path: Path, text: str) -> None:
    """Write a file with mode 0600 (also when it already exists)."""
    path = Path(path)
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def write_json(path: Path, obj) -> None:
    write_private(path, json.dumps(obj, indent=1, sort_keys=True) + "\n")


def fork_head(fork: Path) -> dict:
    """HEAD of the libfprint fork and whether tracked files differ from it."""
    def git(*args):
        p = subprocess.run(["git", "-C", str(fork), *args], capture_output=True, text=True, check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"head": head, "dirty": None if status is None else bool(status)}


def package_version(pacman: str = "pacman") -> dict:
    """Installed package version (read-only `pacman -Q`) and the commit in it."""
    try:
        p = subprocess.run([pacman, "-Q", PACKAGE], capture_output=True, text=True, check=False)
    except OSError:
        return {"version": None, "commit": None}
    if p.returncode != 0:
        return {"version": None, "commit": None}
    version = p.stdout.split()[-1] if p.stdout.split() else None
    m = re.search(r"\.g([0-9a-f]{7,40})", version or "")
    return {"version": version, "commit": m.group(1) if m else None}


def commits_match(package_commit: str | None, head: str | None) -> bool | None:
    if not package_commit or not head:
        return None
    return head.startswith(package_commit)


def run_harness(harness: Path, paths: list[Path], out: Path, scale: int = SCALE) -> dict:
    """Run sigfm_matrix over `paths` (in that order) and return its JSON."""
    private_dir(Path(out).parent)
    proc = subprocess.run([str(harness), "--scale", str(scale), "--out", str(out)] + [str(p) for p in paths],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"{harness} exited {proc.returncode}: {proc.stderr.strip()}")
    os.chmod(out, 0o600)
    return json.loads(Path(out).read_text())


def score_matrix(res: dict) -> np.ndarray:
    """Scores as floats; NaN where an image was gate-rejected (harness null)."""
    return np.array([[np.nan if v is None else v for v in row] for row in res["scores"]], dtype=float)


def rejected_mask(res: dict) -> np.ndarray:
    return np.array([bool(im["rejected"]) for im in res["images"]], dtype=bool)


def match_ms_median(res: dict) -> float | None:
    vals = [v for row in res.get("match_ms", []) for v in row if v is not None]
    return float(np.median(vals)) if vals else None
