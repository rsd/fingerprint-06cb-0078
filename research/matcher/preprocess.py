# SPDX-License-Identifier: MIT
#
# Parts: foreground_mask() and the CLAHE settings of clahe() are ported from
# python-validity (MIT); local_norm(), mask_background(), ridge_period(), gabor(),
# apply() and the rest of this file are our own code, also MIT.
#
# foreground_mask() and the CLAHE settings are ported from python-validity
# (validitysensor/hostmatch/quality.py of
# https://github.com/uunicorn/python-validity/pull/288, by ashh87), MIT licence:
#   Copyright (c) 2020 uunicorn
#   Copyright (c) 2026 ashh87 (PR #288, commit 451cd11)
# The port and our own code in this file:
#   Copyright (C) 2026 Raul Dias <raul@dias.com.br>
# The ported parts keep their upstream copyright lines and the MIT notice below; our
# own code here, like the rest of the project, is MIT.
#
# MIT licence (python-validity parts and the project):
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Image preprocessing a host driver could apply before handing a frame to a matcher.

Steps are named so a variant can be written as a comma-separated list, applied in
order, e.g. "mask25,gabor" or "clahe" or "invert,up2cubic":

  invert      255 - x (ridge polarity swap)
  clahe       OpenCV CLAHE, clip 2.0, 4x8 tiles (the PR #288 settings)
  norm        local mean/variance normalisation (Gaussian window, sigma 8 px)
  maskNN      replace uncovered sensor area with flat grey 128; foreground =
              8x8 blocks with pixel std >= NN (PR #288's foreground_mask; its
              default 40 was measured on 06cb:00a8, 25 suits 06cb:0078's contrast)
  gabor       contextual Gabor filtering along the local ridge orientation
              (Hong/Wan/Jain style) with a per-image ridge period estimate;
              background (std < 25) set to 128
  upNcubic    N-times bicubic upscale on the host (OpenCV)

libfprint's own fpi_image_resize (pixman bilinear) is applied inside the
patched virtual_image driver instead; see nbis_libfprint.py.
"""

from __future__ import annotations

import cv2
import numpy as np


def foreground_mask(img: np.ndarray, block: int = 8, min_std: float = 25.0) -> np.ndarray:
    """Per-pixel bool mask of the touched area (port of PR #288 quality.foreground_mask)."""
    a = np.asarray(img, dtype=np.float64)
    h, w = a.shape
    bh, bw = -(-h // block), -(-w // block)
    p = np.pad(a, ((0, bh * block - h), (0, bw * block - w)), mode="edge")
    fg = p.reshape(bh, block, bw, block).std(axis=(1, 3)) >= min_std
    return np.kron(fg, np.ones((block, block), dtype=bool))[:h, :w]


def clahe(img: np.ndarray) -> np.ndarray:
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 8)).apply(img)


def local_norm(img: np.ndarray, sigma: float = 8.0) -> np.ndarray:
    f = img.astype(np.float32)
    m = cv2.GaussianBlur(f, (0, 0), sigma)
    v = cv2.GaussianBlur((f - m) ** 2, (0, 0), sigma)
    z = (f - m) / np.sqrt(v + 25.0)
    return np.clip(128 + 50 * z, 0, 255).astype(np.uint8)


def mask_background(img: np.ndarray, min_std: float) -> np.ndarray:
    out = img.copy()
    out[~foreground_mask(img, min_std=min_std)] = 128
    return out


def ridge_period(z: np.ndarray, mask: np.ndarray, lo: float = 6.0, hi: float = 14.0) -> float:
    """Dominant ridge wavelength in pixels from the 2-D spectrum of the masked image."""
    f = np.where(mask, z, 0.0)
    h, w = f.shape
    win = np.outer(np.hanning(h), np.hanning(w))
    spec = np.abs(np.fft.fftshift(np.fft.fft2(f * win))) ** 2
    fy = np.fft.fftshift(np.fft.fftfreq(h))[:, None]
    fx = np.fft.fftshift(np.fft.fftfreq(w))[None, :]
    r = np.hypot(fx, fy)
    band = (r >= 1 / hi) & (r <= 1 / lo)
    if not band.any() or spec[band].sum() <= 0:
        return 9.0
    # power-weighted mean frequency within the plausible band
    return float(1.0 / (np.sum(r[band] * spec[band]) / np.sum(spec[band])))


def gabor(img: np.ndarray, min_std: float = 25.0, n_orient: int = 16) -> np.ndarray:
    f = img.astype(np.float32)
    m = cv2.GaussianBlur(f, (0, 0), 8)
    v = cv2.GaussianBlur((f - m) ** 2, (0, 0), 8)
    z = (f - m) / np.sqrt(v + 25.0)
    mask = foreground_mask(img, min_std=min_std)
    period = ridge_period(z, mask)
    gx = cv2.Sobel(z, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(z, cv2.CV_32F, 0, 1, ksize=3)
    gxx = cv2.GaussianBlur(gx * gx, (0, 0), period * 0.6)
    gyy = cv2.GaussianBlur(gy * gy, (0, 0), period * 0.6)
    gxy = cv2.GaussianBlur(gx * gy, (0, 0), period * 0.6)
    theta = 0.5 * np.arctan2(2 * gxy, gxx - gyy)  # gradient direction = normal to the ridges
    sigma = 0.45 * period
    ksize = int(2 * round(3 * sigma) + 1)
    responses = []
    angles = np.arange(n_orient) * np.pi / n_orient
    for a in angles:
        k = cv2.getGaborKernel((ksize, ksize), sigma, float(a), period, 0.6, 0)
        k -= k.mean()
        k /= np.abs(k).sum()
        responses.append(cv2.filter2D(z, cv2.CV_32F, k, borderType=cv2.BORDER_REFLECT))
    responses = np.stack(responses)
    # cv2 kernel orientation: theta is the stripe normal measured from x; our theta is
    # the gradient angle in image coordinates (y down); both index the same direction.
    sel = np.round(((theta % np.pi) / np.pi) * n_orient).astype(int) % n_orient
    r = np.take_along_axis(responses, sel[None], axis=0)[0]
    s = r[mask].std() if mask.any() else r.std()
    out = np.clip(128 + 60 * r / (s + 1e-6), 0, 255).astype(np.uint8)
    out[~mask] = 128
    return out


def apply(img: np.ndarray, steps: str) -> np.ndarray:
    out = img
    for step in [s.strip() for s in steps.split(",") if s.strip()]:
        if step == "invert":
            out = 255 - out
        elif step == "clahe":
            out = clahe(out)
        elif step == "norm":
            out = local_norm(out)
        elif step.startswith("mask"):
            out = mask_background(out, float(step[4:] or 25))
        elif step == "gabor":
            out = gabor(out)
        elif step.startswith("up") and step.endswith("cubic"):
            n = int(step[2:-5])
            out = cv2.resize(out, None, fx=n, fy=n, interpolation=cv2.INTER_CUBIC)
        else:
            raise ValueError(f"unknown preprocessing step {step!r}")
    return np.ascontiguousarray(out, dtype=np.uint8)
