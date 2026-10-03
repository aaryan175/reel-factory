"""onetoone.sharpness — caption edge acutance on DECODED frames.

edge_metrics(frame, roi): ink = min(R,G,B) >= 235 inside roi; boundary = ink pixels touching non-ink;
width_1090 = 0.8 * C / Gpk, C = ink luma - local bed (7x7 min), Gpk = 5x5-max Sobel magnitude. A hard 1-px step
reads 1.6 px by this definition; the approved reels' captions read 1.6-1.8 px; the reference's VP9 text reads ~2.45 px.
EDGE_WIDTH_MAX_PX is the floor a caption must beat to be called sharp."""
from __future__ import annotations

import numpy as np

INK_T = 235
EDGE_WIDTH_MAX_PX = 2.0


def _luma(rgb):
    x = rgb.astype(np.float64)
    return 0.2126 * x[..., 0] + 0.7152 * x[..., 1] + 0.0722 * x[..., 2]


def _stack(a, r, fn):
    h, w = a.shape
    p = np.pad(a, r, mode="edge")
    out = None
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            s = p[dy:dy + h, dx:dx + w]
            out = s.copy() if out is None else fn(out, s)
    return out


def _sobel(y):
    p = np.pad(y, 1, mode="edge")
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])
    return np.hypot(gx, gy) / 8.0


def edge_width_px(frame, roi, mask=None):
    """Median 10-90 edge width (px) of the white-ink boundary in roi=(x0,y0,x1,y1), or None if no ink."""
    x0, y0, x1, y1 = roi
    crop = frame[y0:y1, x0:x1]
    y = _luma(crop)
    if mask is None:
        mask = crop.min(-1) >= INK_T
    if mask.sum() < 200:
        return None
    p = np.pad(mask, 1, mode="constant")
    inner = p[1:-1, 1:-1] & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    boundary = mask & ~inner
    gpk = _stack(_sobel(y), 2, np.maximum)
    bed = _stack(y, 3, np.minimum)
    C = np.clip(float(np.median(y[mask])) - bed, 1, None)
    ok = boundary & (C > 25)
    if ok.sum() < 50:
        return None
    return float(np.median(0.8 * C[ok] / np.clip(gpk[ok], 1e-6, None)))


def is_sharp(frame, roi) -> bool:
    w = edge_width_px(frame, roi)
    return w is not None and w <= EDGE_WIDTH_MAX_PX
