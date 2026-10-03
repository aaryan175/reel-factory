#!/usr/bin/env python3
"""onetoone.motion — the camera move between two frames as a similarity transform (zoom, pan, rotation).

Why: global phase correlation reported the reference's S08 as a -15.9 px
horizontal pan; the real move is a centred PUSH-IN of +2.6%. Phase correlation cannot see zoom and
returns a meaningless shift for it. This tracks features (Shi-Tomasi + pyramidal Lucas-Kanade) and
fits a partial affine (scale + rotation + translation) with RANSAC, reporting the scale, the
translation at the frame centre, and the fit error, so a zoom reads as a zoom.

    python3 -m onetoone.motion <video> <frame_a> <frame_b>
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Dict

import cv2
import numpy as np

from onetoone.ffx import run


def grab(video: Path, n: int, out: Path) -> np.ndarray:
    run(["-y", "-loglevel", "error", "-i", str(video), "-vf", f"select=eq(n\\,{n})", "-vsync", "0",
         "-frames:v", "1", str(out)])
    return cv2.cvtColor(cv2.imread(str(out)), cv2.COLOR_BGR2GRAY)


def fit(a: np.ndarray, b: np.ndarray) -> Dict[str, Any]:
    pts = cv2.goodFeaturesToTrack(a, maxCorners=800, qualityLevel=0.01, minDistance=12)
    if pts is None or len(pts) < 12:
        return {"ok": False, "why": "too few features"}
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(a, b, pts, None, winSize=(31, 31), maxLevel=4)
    good_a, good_b = pts[st.ravel() == 1], nxt[st.ravel() == 1]
    M, inl = cv2.estimateAffinePartial2D(good_a, good_b, method=cv2.RANSAC, ransacReprojThreshold=2.0)
    if M is None:
        return {"ok": False, "why": "no fit"}
    s = math.hypot(M[0, 0], M[1, 0]); rot = math.degrees(math.atan2(M[1, 0], M[0, 0]))
    h, w = a.shape
    c = np.array([w / 2, h / 2, 1.0]); tc = M @ c - c[:2]
    ia = good_a[inl.ravel() == 1].reshape(-1, 2); ib = good_b[inl.ravel() == 1].reshape(-1, 2)
    err = float(np.mean(np.linalg.norm((ia @ M[:, :2].T + M[:, 2]) - ib, axis=1))) if len(ia) else float("nan")
    return {"ok": True, "scale_pct": round((s - 1) * 100, 2), "rot_deg": round(rot, 3),
            "centre_shift_px": [round(float(tc[0]), 2), round(float(tc[1]), 2)],
            "inliers": int(inl.sum()), "tracked": int(len(good_a)), "fit_err_px": round(err, 2)}


def between(video: Path, fa: int, fb: int, scratch: Path) -> Dict[str, Any]:
    scratch.mkdir(parents=True, exist_ok=True)
    return fit(grab(video, fa, scratch / f"m_{fa}.png"), grab(video, fb, scratch / f"m_{fb}.png"))


if __name__ == "__main__":
    v, fa, fb = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    print(between(v, fa, fb, Path("/tmp/onetoone-motion") / v.stem))


def track_roi(video: Path, n_frames: int, roi, scratch: Path) -> list:
    """Follow a subject (roi = x0,y0,x1,y1 as fractions, on frame 0) frame to frame: LK-tracked
    features inside the roi, similarity fit per step, integrated. Returns per frame
    {"scale": cumulative size vs frame 0, "cx","cy": roi centre as fractions}. Used to hold a subject
    steady with crop keyframes when our camera moves and the reference's does not (S12)."""
    scratch.mkdir(parents=True, exist_ok=True)
    frames = [grab(video, k, scratch / f"t_{k}.png") for k in range(n_frames)]
    h, w = frames[0].shape
    x0, y0, x1, y1 = roi[0] * w, roi[1] * h, roi[2] * w, roi[3] * h
    s, cx, cy = 1.0, (x0 + x1) / 2, (y0 + y1) / 2
    bw, bh = x1 - x0, y1 - y0
    out = [{"scale": 1.0, "cx": cx / w, "cy": cy / h}]
    for k in range(1, n_frames):
        mask = np.zeros_like(frames[k - 1])
        mask[max(0, int(cy - bh * s / 2)):int(cy + bh * s / 2), max(0, int(cx - bw * s / 2)):int(cx + bw * s / 2)] = 255
        pts = cv2.goodFeaturesToTrack(frames[k - 1], maxCorners=400, qualityLevel=0.005, minDistance=6, mask=mask)
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(frames[k - 1], frames[k], pts, None, winSize=(25, 25), maxLevel=3)
        a, b = pts[st.ravel() == 1], nxt[st.ravel() == 1]
        M, _ = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=1.5)
        if M is not None:
            ds = math.hypot(M[0, 0], M[1, 0])
            c = M @ np.array([cx, cy, 1.0])
            s, cx, cy = s * ds, float(c[0]), float(c[1])
        out.append({"scale": round(s, 4), "cx": round(cx / w, 4), "cy": round(cy / h, 4)})
    return out
