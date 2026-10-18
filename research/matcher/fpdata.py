"""Capture-set loading and output-path safety, shared by the matcher evaluation scripts.

A capture set is one or more finger folders, each holding a manifest.json written
by proto/capture.py and the PGM images it lists. A path given on the command line
may be a finger folder itself or a parent folder whose immediate subfolders are
finger folders (e.g. proto/captures/). Only images with "accepted": true are used.
The finger label is the manifest's "label" (falling back to the folder name).

The images are biometric data, and so is everything computed from them (minutiae,
keypoints, score matrices). save_json()/check_out_dir() refuse to write into a git
work tree unless the target is git-ignored, so results stay out of commits.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class Sample:
    finger: str
    name: str  # PGM file stem, e.g. right-index-03
    path: Path
    image: np.ndarray  # (h, w) uint8, as captured
    coverage: float | None = None  # manifest stats.coverage, if present
    meta: dict = field(default_factory=dict)


def read_pgm(path: Path) -> np.ndarray:
    """Binary (P5) 8-bit PGM reader; tolerates comment lines."""
    data = path.read_bytes()
    if not data.startswith(b"P5"):
        raise ValueError(f"{path}: not a binary PGM")
    tokens: list[bytes] = []
    pos = 2
    while len(tokens) < 3:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        tokens.append(data[pos:end])
        pos = end
    pos += 1  # the single whitespace after maxval
    w, h, maxval = (int(t) for t in tokens)
    if maxval > 255:
        raise ValueError(f"{path}: 16-bit PGM not supported")
    return np.frombuffer(data, dtype=np.uint8, count=w * h, offset=pos).reshape(h, w).copy()


def _finger_dirs(path: Path) -> list[Path]:
    if (path / "manifest.json").is_file():
        return [path]
    dirs = sorted(p for p in path.iterdir() if p.is_dir() and (p / "manifest.json").is_file())
    if not dirs:
        raise SystemExit(f"{path}: no manifest.json here or in its immediate subfolders")
    return dirs


def load_captures(paths: list[str | Path], min_coverage: float = 0.0) -> list[Sample]:
    """Accepted images of every finger folder under ``paths``, ordered by finger then name."""
    samples: list[Sample] = []
    for p in paths:
        for d in _finger_dirs(Path(p)):
            manifest = json.loads((d / "manifest.json").read_text())
            finger = manifest.get("label") or d.name
            for item in manifest.get("images", []):
                if not item.get("accepted"):
                    continue
                pgm = d / item["pgm"]
                cov = (item.get("stats") or {}).get("coverage")
                if cov is not None and cov < min_coverage:
                    continue
                samples.append(Sample(finger, pgm.stem, pgm, read_pgm(pgm), cov,
                                      {"timestamp": item.get("timestamp")}))
    samples.sort(key=lambda s: (s.finger, s.name))
    if len({s.name for s in samples}) != len(samples):
        raise SystemExit("duplicate image names across finger folders")
    return samples


def describe(samples: list[Sample]) -> dict:
    fingers: dict[str, int] = {}
    for s in samples:
        fingers[s.finger] = fingers.get(s.finger, 0) + 1
    shapes = sorted({s.image.shape for s in samples})
    return {"n_images": len(samples), "fingers": fingers, "shapes": [list(x) for x in shapes]}


def _git_toplevel(path: Path) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 else None


def check_out_dir(out: str | Path, allow_tracked: bool = False) -> Path:
    """Create ``out`` and refuse it if it sits in a git work tree without being ignored."""
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    top = _git_toplevel(out)
    if top is not None and not allow_tracked:
        ignored = subprocess.run(["git", "-C", str(top), "check-ignore", "-q", str(out)],
                                 check=False).returncode == 0
        if not ignored:
            raise SystemExit(f"{out} is inside the git work tree {top} and not ignored; results are "
                             "biometric-derived. Use a scratch directory, or pass --allow-tracked.")
    return out


def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1, default=_json_default) + "\n")


def _json_default(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))
