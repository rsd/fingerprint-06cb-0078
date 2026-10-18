#!/usr/bin/env python3
"""Build a synthetic capture set of sensor-sized crops from larger fingerprint images.

Control experiment: if a matcher fails on crops of a clean reference print cut to
the sensor's size, where every "genuine" pair is an exact translation with a known
overlap, the failure is due to the capture area and not to image quality.

Each input image becomes one "finger" folder holding a 3x3 grid of crops around
the image centre, offset by --step (fractions of the crop size), plus a
manifest.json in the format proto/capture.py writes, so nbis_libfprint.py and
sift_matcher.py run on it unchanged. A PNG with an alpha channel is read the way
libfprint's tests read examples/prints/*.png (the alpha channel is the image).

Usage:
  make_crop_set.py IMAGES... --out DIR [--scale 1.6] [--crop 144x56] [--step 0.17,0.25]

The default --scale brings libfprint's examples/prints (ridge period ~5.3 px)
to 06cb:0078's ridge period (~8.5 px).
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import cv2
import numpy as np


def load(path: Path) -> np.ndarray:
    a = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if a is None:
        raise SystemExit(f"{path}: cannot read")
    if a.ndim == 3 and a.shape[2] == 4:
        return a[..., 3]
    if a.ndim == 3:
        return cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    return a


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--scale", type=float, default=1.6)
    ap.add_argument("--crop", default="144x56", help="WxH")
    ap.add_argument("--step", default="0.17,0.25", help="grid step as fraction of crop W,H")
    args = ap.parse_args()
    cw, ch = (int(x) for x in args.crop.split("x"))
    sx, sy = (float(x) for x in args.step.split(","))
    dx, dy = round(cw * sx), round(ch * sy)
    for path in args.images:
        img = load(path)
        if args.scale != 1:
            img = cv2.resize(img, None, fx=args.scale, fy=args.scale, interpolation=cv2.INTER_CUBIC)
        h, w = img.shape
        cx, cy = w // 2 - cw // 2, h // 2 - ch // 2
        if cx - dx < 0 or cy - dy < 0 or cx + dx + cw > w or cy + dy + ch > h:
            raise SystemExit(f"{path}: {w}x{h} too small for {cw}x{ch} crops at this step")
        label = path.stem
        d = args.out / label
        d.mkdir(parents=True, exist_ok=True)
        items = []
        for n, (oy, ox) in enumerate(itertools.product((-dy, 0, dy), (-dx, 0, dx)), 1):
            crop = np.ascontiguousarray(img[cy + oy:cy + oy + ch, cx + ox:cx + ox + cw])
            name = f"{label}-{n:02d}.pgm"
            (d / name).write_bytes(f"P5\n{cw} {ch}\n255\n".encode() + crop.tobytes())
            items.append({"index": n, "accepted": True, "pgm": name, "offset": [ox, oy],
                          "stats": {"mean": float(crop.mean()), "sd": float(crop.std())}})
        (d / "manifest.json").write_text(json.dumps(
            {"label": label, "tool": "make_crop_set.py", "source": str(path), "scale": args.scale,
             "crop": [cw, ch], "images": items}, indent=1))
        print(f"{label}: 9 crops {cw}x{ch}, offsets +-{dx},+-{dy} px")


if __name__ == "__main__":
    main()
