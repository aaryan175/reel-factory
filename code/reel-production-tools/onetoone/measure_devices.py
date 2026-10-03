#!/usr/bin/env python3
"""onetoone.measure_devices — MEASURE the reference's caption ENTRY DEVICES, frame by frame.

The study's `entry` prose ("blur-in begins n=41, resolves sharp by n=45-46") is a hint, not a
number. This reads the reference's own frames and writes the per-frame curves that
onetoone.devices replays: onset frame, sharp frame, blur sigma (x and y separately, so a
horizontal smear is distinguishable from an isotropic defocus), opacity, scale and drift.

METHOD — align first, then measure.
A first pass tried to read blur out of stroke-scale energy statistics alone. It reported every
state sharp on its first frame, which the reference frames refute: on the S09
silhouette bed and shot S08 (bright exterior) the bed's own edges carry as much stroke-scale energy as
the ink does, and — the real killer — the reference's captions are NOT stationary. Every state
carries a slow scale ramp plus a drift (c06 "<Script C>" grows 0.90x -> 1.00x across its 30 frames;
c01 shrinks, which the study already recorded). An unaligned fit reads that displacement as
blur. So:

  1. res = polarity * (L - grey_open/close(L, 71)) — what sits above its own local bed at
     stroke scale, for the whole padded crop of the part's measured bbox;
  2. TEMPLATE = the SETTLED frame's residual, cut to exactly the study's bbox_settled (so bed
     texture outside the measured box can never enter the template);
  3. GEOMETRY per frame: cv2.matchTemplate(TM_CCOEFF_NORMED) of the template, resampled over a
     scale grid, against the frame's residual — coarse then fine, the second pass using the
     template blurred by the blur fitted in between so a soft entry frame still locates. Gives
     scale s(n) and the ink's offset (dx, dy) from where the settled layer sits;
  4. BLUR at that alignment: blur the aligned template over a sigma grid (isotropic pass, then
     refine y, then x) and keep the (sigma_x, sigma_y) with the highest normalised correlation
     against the frame patch. Sigma is therefore measured RELATIVE to the reference's own
     settled sharpness, which is what the renderer must add on top of our settled layer;
  5. OPACITY: least-squares alpha of the frame patch against that blurred template, both
     zero-meaned. Blur conserves the ink integral, so this stays an opacity measure;
  6. WORD REVEAL: lines whose study records a `word_reveal` are additionally measured one word
     at a time (column-gap segmentation of the settled ink) using the part's own alignment, so
     "<line B1>" arriving before "<line B2>" is a measured onset and not a guess.

Honesty rule: a part or word whose fit never clears the onset floors is written out with
device: null and a reason. The renderer falls back to the static layer for it.

    python3 -m onetoone.measure_devices <brain_dir> <refframes_dir> [-o caption_devices.json]
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage

# --- tunables -------------------------------------------------------------------------------
TOPHAT = 71                       # grey open/close window; must exceed the widest glyph stroke
PAD_FRAC, PAD_MIN = 0.35, 200     # crop padding around the measured bbox
COARSE = np.round(np.arange(0.60, 1.361, 0.04), 3)
FINE_SPAN, FINE_STEP = 0.06, 0.005
COARSE_BLUR = 4.0                 # template blur for the locate pass (entry frames are soft)
FINE_BLUR = 2.0                   # and for the refine pass, before any blur has been fitted
MIN_CONTAIN = 0.60                # the located ink must sit on the settled box, not elsewhere
# every scale outside this band was a stray match on a different word in the same box (c02
# "<Script A>" latching onto "<line A>" at 0.54x before it had entered). The reference's
# own ramps are inside it: c06 0.89->1.00, c01 1.11->0.94, c05b 1.18->0.99.
SCALE_BAND = (0.80, 1.30)
SIGMA_GRID = np.array([0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.5, 8.0, 10.0, 12.0, 15.0,
                       18.0, 22.0, 26.0, 30.0])
ONSET_ALPHA, ONSET_FIT = 0.22, 0.55
# Onsets read off the reference contact sheets by eye, for the two units the fit cannot
# separate on its own, with the reason. Both are recorded alongside the automatic value, never
# instead of it: `first_frame_auto` keeps what the fit said and `first_frame_source` says which
# one the renderer uses. Per-REFERENCE data: the entries below are the shape, with placeholder words —
# replace them with your own reference's state ids and words (an unmatched entry is simply ignored).
EYE_ONSETS: Dict[str, Dict[str, Tuple[int, str]]] = {
    "c02": {"<Script A>": (41, "the plain line '<line A>' sits INSIDE the <Script A> box from n=32, so the "
                           "fit scores 0.57-0.66 on every frame from 33 with no separation; the "
                           "contact sheet shows the script's first ghost on n=41")},
    "c03": {"<line B2>": (55, "the second word's own column slice lands mostly on bed, so its opacity "
                        "normalisation is unstable (reads 2.6-4.7 where 1.0 is full ink); the "
                        "contact sheet shows '<line B2>' first appearing on n=55, which is also what "
                        "the study recorded")},
}   # on screen = this much ink AND a fit that really matches
SHARP_SIGMA, SHARP_ALPHA = 1.2, 0.60  # sharp = matches the settled look


def _luma_rgb(rgb: Sequence[float]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _frame_rgb(refframes: Path, n: int) -> np.ndarray:
    return np.asarray(Image.open(refframes / f"ref-{n:03d}.png").convert("RGB"), dtype=np.float32)


def _frame_luma(refframes: Path, n: int) -> np.ndarray:
    a = _frame_rgb(refframes, n)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def _ink_gate(rgb: np.ndarray, ink: Sequence[float], tol: float = 80.0) -> np.ndarray:
    """Keep only what could be this ink. Removes bed objects that intrude into the measured
    box (a foreground subject under c05, the shot S08 (bright exterior) background under c04a) from the TEMPLATE. Where ink
    and bed are close in colour (c04b crimson on orange) it keeps everything, which is honest:
    it can only ever remove things that are far from the recorded ink."""
    d = np.sqrt(((rgb - np.asarray(ink, dtype=np.float32)) ** 2).sum(axis=-1))
    return (d <= tol).astype(np.float32)


def _residual(L: np.ndarray, polarity: int, ink_L: float) -> np.ndarray:
    """Stroke-scale residual NORMALISED to ink coverage: 1.0 where the ink is fully opaque.

    The raw residual is a contrast, so it rises and falls with the bed: on c01's dark exterior bed the
    same white line read alpha 0.5 at n=0 and 2.0 at n=13 purely because the bed got darker,
    which would have been replayed as an opacity ramp that is not there. Dividing by the
    ink-to-bed contrast the recorded ink would produce against THIS pixel's bed makes it an
    opacity again.
    """
    if polarity > 0:
        bed = ndimage.grey_opening(L, size=(TOPHAT, TOPHAT))
        res, span = L - bed, ink_L - bed
    else:
        bed = ndimage.grey_closing(L, size=(TOPHAT, TOPHAT))
        res, span = bed - L, bed - ink_L
    return np.clip(res / np.maximum(15.0, span), 0, None).astype(np.float32)


def _blur(a: np.ndarray, sx: float, sy: float) -> np.ndarray:
    if sx > 0:
        a = ndimage.gaussian_filter1d(a, sx, axis=1, mode="constant")
    if sy > 0:
        a = ndimage.gaussian_filter1d(a, sy, axis=0, mode="constant")
    return a


def _resize(a: np.ndarray, size: Tuple[int, int]) -> np.ndarray:
    """Resample to (w, h). INTER_AREA only ever DOWN: it aliases badly on upscale, and that
    aliasing is what the blur fit then 'explains' as blur — it read the c05 hard swaps (all of
    which sit at scale > 1 early) as 10-26 px smears until this was split."""
    up = size[0] >= a.shape[1] or size[1] >= a.shape[0]
    return cv2.resize(a, size, interpolation=cv2.INTER_LINEAR if up else cv2.INTER_AREA)


def _scale(a: np.ndarray, s: float) -> np.ndarray:
    return _resize(a, (max(1, int(round(a.shape[1] * s))), max(1, int(round(a.shape[0] * s)))))


def _pad_for_blur(T: np.ndarray, sx: float, sy: float) -> Tuple[np.ndarray, int, int]:
    px, py = int(np.ceil(3 * sx)), int(np.ceil(3 * sy))
    if px == 0 and py == 0:
        return T, 0, 0
    return np.pad(T, ((py, py), (px, px))), px, py


def _support(tmpl: np.ndarray, grow: int = 5) -> np.ndarray:
    """Where the (blurred) template actually has ink, grown a little.

    Correlating over the WHOLE patch lets whatever the bed is doing inside the caption's box
    drive the fit: the c05 box has the subject's head in its lower left, and with the head in
    play the sharp template scored 0.60 while a 22 px smear scored 0.67, so the fit called a
    visibly sharp frame a heavy blur. Judge on the ink's own pixels instead.
    """
    m = tmpl >= 0.03 * float(tmpl.max() or 1.0)
    if grow:
        m = ndimage.binary_dilation(m, np.ones((2 * grow + 1, 2 * grow + 1)))
    return m.astype(np.float32)


def _coverage(patch: np.ndarray, tmpl: np.ndarray, w: np.ndarray) -> Tuple[float, float]:
    """(ink integral inside the mask, bed rate outside it).

    The normalised residual IS a coverage map — a pixel that is fully this ink reads 1.0 — so
    summing it measures how much ink is on screen. A Gaussian conserves that sum, which makes
    this the one opacity estimate a blur cannot bias. The least-squares alpha cannot say the
    same: it is attenuated by however noisy the frame is (regression dilution), which read c06
    "<Script C>" at 0.22 on n=140 where its actual coverage is 0.43.
    """
    inside = float((patch * w).sum())
    out_mask = 1.0 - w
    n_out = float(out_mask.sum())
    rate = float((patch * out_mask).sum() / n_out) if n_out > 32 else 0.0
    return inside - rate * float(w.sum()), rate


def _wide_support(tmpl: np.ndarray, sigma: float) -> np.ndarray:
    m = tmpl >= 0.01 * float(tmpl.max() or 1.0)
    grow = int(np.ceil(3 * sigma)) + 4
    return ndimage.binary_dilation(m, np.ones((2 * grow + 1, 2 * grow + 1))).astype(np.float32)


def _ncc(a: np.ndarray, b: np.ndarray, w: np.ndarray | None = None) -> float:
    if w is None:
        w = np.ones_like(a)
    n = float(w.sum())
    if n < 16:
        return 0.0
    da = (a - (a * w).sum() / n) * w
    db = (b - (b * w).sum() / n) * w
    d = float(np.sqrt((da ** 2).sum() * (db ** 2).sum()))
    return float((da * db).sum() / d) if d > 0 else 0.0


def _alpha(patch: np.ndarray, tmpl: np.ndarray, w: np.ndarray | None = None) -> float:
    if w is None:
        w = np.ones_like(patch)
    n = float(w.sum())
    if n < 16:
        return 0.0
    dp = (patch - (patch * w).sum() / n) * w
    dt = (tmpl - (tmpl * w).sum() / n) * w
    d = float((dt ** 2).sum())
    return float((dp * dt).sum() / d) if d > 0 else 0.0


def _locate(res: np.ndarray, T: np.ndarray, scales: Sequence[float],
            blur: Tuple[float, float]) -> Dict[str, Any] | None:
    Tb, px, py = _pad_for_blur(T, *blur)
    Tb = _blur(Tb, *blur)
    best = None
    for s in scales:
        Ts = _scale(Tb, float(s))
        if Ts.shape[0] >= res.shape[0] or Ts.shape[1] >= res.shape[1] or min(Ts.shape) < 8:
            continue
        m = cv2.matchTemplate(res, Ts, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(m)
        if best is None or mv > best["fit"]:
            best = {"fit": float(mv), "scale": float(s),
                    "x": ml[0] + int(round(px * s)), "y": ml[1] + int(round(py * s)),
                    "w": Ts.shape[1] - 2 * int(round(px * s)), "h": Ts.shape[0] - 2 * int(round(py * s))}
    return best


def _locate_near(res: np.ndarray, T: np.ndarray, scales: Sequence[float], blur: Tuple[float, float],
                 cx: int, cy: int, rad: int = 25) -> Dict[str, Any] | None:
    """Same as _locate but only within `rad` px of a predicted top-left.

    A template blurred by 20-30 px is a featureless blob that matches anywhere, so an
    unconstrained search on the softest entry frames collapsed to nonsense (c04a "<Script D>" read
    scale 0.74 at dx=-133 on its first three frames). The geometry is a smooth ramp, so those
    frames are searched around what the ramp predicts instead.
    """
    h, w = T.shape
    best = None
    for s in scales:
        th, tw = int(round(h * s)), int(round(w * s))
        x0, y0 = max(0, cx - rad), max(0, cy - rad)
        x1, y1 = min(res.shape[1], cx + tw + rad), min(res.shape[0], cy + th + rad)
        sub = res[y0:y1, x0:x1]
        Tb, px, py = _pad_for_blur(T, *blur)
        Ts = _scale(_blur(Tb, *blur), float(s))
        if Ts.shape[0] > sub.shape[0] or Ts.shape[1] > sub.shape[1] or min(Ts.shape) < 8:
            continue
        m = cv2.matchTemplate(sub, Ts, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(m)
        if best is None or mv > best["fit"]:
            best = {"fit": float(mv), "scale": float(s),
                    "x": x0 + ml[0] + int(round(px * s)), "y": y0 + ml[1] + int(round(py * s)),
                    "w": Ts.shape[1] - 2 * int(round(px * s)), "h": Ts.shape[0] - 2 * int(round(py * s))}
    return best


def _poly(ns: Sequence[float], vals: Sequence[float], deg: int = 2):
    """Smooth model of a geometry channel over the confident frames (falls back as data thins)."""
    if len(ns) >= 6:
        c = np.polyfit(ns, vals, min(deg, 2))
    elif len(ns) >= 3:
        c = np.polyfit(ns, vals, 1)
    elif ns:
        c = np.array([float(np.median(vals))])
    else:
        return lambda n: None
    return lambda n: float(np.polyval(c, n))


def _patch(res: np.ndarray, loc: Mapping[str, Any]) -> np.ndarray:
    y, x, h, w = loc["y"], loc["x"], loc["h"], loc["w"]
    out = np.zeros((h, w), dtype=np.float32)
    ys, xs = max(0, y), max(0, x)
    ye, xe = min(res.shape[0], y + h), min(res.shape[1], x + w)
    if ye > ys and xe > xs:
        out[ys - y:ye - y, xs - x:xe - x] = res[ys:ye, xs:xe]
    return out


def _fit_blur(patch: np.ndarray, T: np.ndarray, scale: float) -> Tuple[float, float, float]:
    """(sigma_x, sigma_y, ncc) of the scaled template against the aligned patch."""
    Ts = _resize(T, (patch.shape[1], patch.shape[0]))

    def score(sx: float, sy: float) -> float:
        Tb, px, py = _pad_for_blur(Ts, sx, sy)
        Tb = _blur(Tb, sx, sy)
        Tb = Tb[py:Tb.shape[0] - py or None, px:Tb.shape[1] - px or None]
        return _ncc(patch, Tb, _support(Tb))

    iso = max(SIGMA_GRID, key=lambda s: score(float(s), float(s)))
    sy = max(SIGMA_GRID, key=lambda s: score(float(iso), float(s)))
    sx = max(SIGMA_GRID, key=lambda s: score(float(s), float(sy)))
    return float(sx), float(sy), score(float(sx), float(sy))


def _blurred_template(T: np.ndarray, shape: Tuple[int, int], sx: float, sy: float) -> np.ndarray:
    Ts = _resize(T, (shape[1], shape[0]))
    Tb, px, py = _pad_for_blur(Ts, sx, sy)
    Tb = _blur(Tb, sx, sy)
    return Tb[py:Tb.shape[0] - py or None, px:Tb.shape[1] - px or None]


def _word_slices(T: np.ndarray, n_words: int) -> List[Tuple[int, int]]:
    """Column spans of each word in the settled template, split at its widest interior gaps.

    Strict zero-ink gaps are not reliable: on the low-contrast beds (c04b's crimson on orange)
    the residual never reaches zero between words. So split on the widest runs of NEAR-zero
    column energy instead.
    """
    prof = T.sum(axis=0).astype(np.float32)
    if prof.max() <= 0:
        return []
    prof = ndimage.uniform_filter1d(prof, 5)
    low = prof < 0.03 * float(prof.max())
    runs: List[List[int]] = []
    start = None
    for i, v in enumerate(low):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append([start, i - 1]); start = None
    if start is not None:
        runs.append([start, len(low) - 1])
    interior = [r for r in runs if r[0] > 0 and r[1] < len(low) - 1]
    if len(interior) < n_words - 1:
        return []
    interior.sort(key=lambda r: r[1] - r[0], reverse=True)
    cuts = sorted((r[0] + r[1]) // 2 for r in interior[:n_words - 1])
    ink = np.flatnonzero(~low)
    if not ink.size:
        return []
    bounds = [int(ink[0])] + cuts + [int(ink[-1])]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _sub(res: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray:
    return _patch(res, {"x": x, "y": y, "w": max(2, w), "h": max(2, h)})


def _sub_like(full: np.ndarray, x: int, y: int, shape: Tuple[int, int]) -> np.ndarray:
    return _sub(full, int(x), int(y), int(shape[1]), int(shape[0]))


def _other_ink(state: Mapping[str, Any], part: Mapping[str, Any], shape: Tuple[int, int],
               cx0: int, cy0: int) -> np.ndarray:
    """Boxes of the state's OTHER parts, in crop coordinates, to be excluded from measurement.

    Every two-part state in this reel sets the small plain line INSIDE the big ornate word's
    box, so each part's box contains the other's ink. Measured naively, "<line A>" (up from n=32)
    makes "<Script A>" look present from n=33, and "<line C>" inflates "<Script C>". Excluding the
    other part's own box is what separates them.
    """
    excl = np.zeros(shape, dtype=np.float32)
    own = part.get("bbox_settled") or part.get("bbox_settled_approx")
    own_area = (own[2] - own[0]) * (own[3] - own[1]) if own else 0
    for q in state.get("parts", []):
        if q is part:
            continue
        b = q.get("bbox_settled") or q.get("bbox_settled_approx")
        if not b:
            continue
        b = [int(v) for v in b]
        # Only ever exclude a SMALLER neighbour. The pairing in this reel is one big ornate word
        # with a small plain line set inside its box: the big box needs the small line taken out
        # of it, but doing the reverse erases the plain line's own measurement region entirely
        #.
        if (b[2] - b[0]) * (b[3] - b[1]) >= own_area:
            continue
        x0 = max(0, b[0] - cx0 - 8); y0 = max(0, b[1] - cy0 - 8)
        x1 = min(shape[1], b[2] - cx0 + 8); y1 = min(shape[0], b[3] - cy0 + 8)
        if x1 > x0 and y1 > y0:
            excl[y0:y1, x0:x1] = 1.0
    return excl


def _guarded(mask: np.ndarray, excl: np.ndarray) -> np.ndarray:
    """Drop the exclusion if it would eat the measurement region it is meant to clean."""
    kept = mask * (1.0 - excl)
    return kept if kept.sum() >= 0.35 * float(mask.sum()) else mask


def measure_part(refframes: Path, state: Mapping[str, Any], part: Mapping[str, Any],
                 frames_total: int) -> Dict[str, Any]:
    text = str(part.get("text") or " ".join(part.get("words") or []))
    box = part.get("bbox_settled") or part.get("bbox_settled_approx")
    if not box:
        return {"text": text, "device": None, "why": "no bbox in the study"}
    box = [int(v) for v in box]
    bw, bh = box[2] - box[0], box[3] - box[1]
    settled_n = int(part.get("bbox_frame", (int(state["in"]) + int(state["out"])) // 2))
    ink = part.get("ink_rgb") or part.get("ink_rgb_approx") or (255, 255, 255)
    bed = part.get("local_bed_rgb")
    ink_L = _luma_rgb(ink)
    polarity = 1 if ink_L >= (_luma_rgb(bed) if bed else 128.0) else -1

    L = _frame_luma(refframes, settled_n)
    H, W = L.shape
    px, py = max(PAD_MIN, int(PAD_FRAC * bw)), max(PAD_MIN, int(PAD_FRAC * bh))
    cx0, cy0 = max(0, box[0] - px), max(0, box[1] - py)
    cx1, cy1 = min(W, box[2] + px), min(H, box[3] + py)
    S_raw = _residual(L[cy0:cy1, cx0:cx1], polarity, ink_L)
    S = S_raw * _ink_gate(_frame_rgb(refframes, settled_n)[cy0:cy1, cx0:cx1], ink)
    ysl, xsl = slice(box[1] - cy0, box[3] - cy0), slice(box[0] - cx0, box[2] - cx0)
    T = S[ysl, xsl].copy()
    T_seen = S_raw[ysl, xsl].copy()   # the same box as the FRAMES deliver it, bed and all
    if T.size < 400 or float(T.max()) < 0.15:
        return {"text": text, "device": None, "polarity": polarity,
                "why": "settled ink did not separate from its bed at stroke scale"}

    words = list(part.get("words") or [text])
    wslices = _word_slices(T, len(words)) if (part.get("word_reveal") and len(words) > 1) else []
    if len(wslices) != len(words):
        wslices = []
    # ALIGN on the FIRST word when the line reveals word by word: the later words are simply
    # not on screen for the early frames, and matching the whole line against one word drops
    # the alignment somewhere else entirely.
    a0, b0 = (wslices[0] if wslices else (0, T.shape[1] - 1))
    A = T[:, a0:b0 + 1].copy()
    anchor = [box[0] + a0, box[1]]            # canvas point the scale is measured about
    abw = b0 - a0 + 1
    # Self-match floor: the template is ink-gated but every FRAME patch still arrives with
    # whatever the bed is doing inside the box. Measuring the settled frame against its own
    # gated template gives the best score this part can possibly reach, so opacity and fit are
    # reported RELATIVE to it — 1.0 means "as much of this ink as the settled frame has".
    sup0 = _support(A)
    a_self = _alpha(T_seen[:, a0:b0 + 1], A, sup0) or 1.0
    f_self = max(_ncc(T_seen[:, a0:b0 + 1], A, sup0), 1e-3)  # recomputed below with the exclusion
    w_self = {}
    for (a, b), w in zip(wslices, words):
        Tw0 = T[:, a:b + 1]
        sw = _support(Tw0)
        w_self[w] = (_alpha(T_seen[:, a:b + 1], Tw0, sw) or 1.0,
                     max(_ncc(T_seen[:, a:b + 1], Tw0, sw), 1e-3))

    # the settled layer's own top-left inside the crop — where scale 1.0, dx=dy=0 puts the ink
    hx, hy = anchor[0] - cx0, anchor[1] - cy0
    excl_full = _other_ink(state, part, S_raw.shape, cx0, cy0)
    # coverage of the settled frame through the same mask, as the denominator for opacity
    sup_settled = _guarded(_wide_support(A, 0.0), _sub_like(excl_full, hx, hy, A.shape))
    cov_settled = _coverage(T_seen[:, a0:b0 + 1], A, sup_settled)[0] or 1.0
    f_self = max(_ncc(T_seen[:, a0:b0 + 1], A, _guarded(sup0, _sub_like(excl_full, hx, hy, A.shape))), 1e-3)
    cov_words = {}
    for (a, b), w in zip(wslices, words):
        Tw0 = T[:, a:b + 1]
        sw = _guarded(_wide_support(Tw0, 0.0), _sub_like(excl_full, hx + a, hy, Tw0.shape))
        cov_words[w] = _coverage(T_seen[:, a:b + 1], Tw0, sw)[0] or 1.0

    def contain(loc: Mapping[str, Any]) -> float:
        ax0, ay0 = cx0 + loc["x"], cy0 + loc["y"]
        ax1, ay1 = ax0 + loc["w"], ay0 + loc["h"]
        ox = max(0, min(ax1, anchor[0] + abw) - max(ax0, anchor[0]))
        oy = max(0, min(ay1, box[3]) - max(ay0, box[1]))
        return (ox * oy) / max(1, min(loc["w"] * loc["h"], abw * bh))

    lo, hi = max(0, int(state["in"]) - 2), min(frames_total - 1, int(state["out"]))
    residuals = {n: _residual(_frame_luma(refframes, n)[cy0:cy1, cx0:cx1], polarity, ink_L)
                 for n in range(lo, hi + 1)}

    # ---- pass 1: free alignment, to find the frames whose geometry can be trusted -----------
    free: Dict[int, Dict[str, Any]] = {}
    for n, res in residuals.items():
        loc = _locate(res, A, COARSE, (COARSE_BLUR, COARSE_BLUR))
        if loc is None:
            continue
        fine = np.round(np.arange(loc["scale"] - FINE_SPAN, loc["scale"] + FINE_SPAN + 1e-9,
                                  FINE_STEP), 4)
        loc = _locate(res, A, fine, (FINE_BLUR, FINE_BLUR)) or loc
        loc["contain"] = contain(loc)
        free[n] = loc
    good = [(n, l) for n, l in sorted(free.items())
            if l["contain"] >= MIN_CONTAIN and SCALE_BAND[0] <= l["scale"] <= SCALE_BAND[1]
            and l["fit"] >= 0.5]
    if not good:
        return {"text": text, "device": None, "polarity": polarity,
                "why": "no frame of this state aligned to the settled ink well enough to model"}
    ns = [float(n) for n, _ in good]
    m_scale = _poly(ns, [l["scale"] for _, l in good])
    m_x = _poly(ns, [float(l["x"]) for _, l in good])
    m_y = _poly(ns, [float(l["y"]) for _, l in good])

    # ---- pass 2: measure every frame at (or near) the geometry the ramp predicts ------------
    curve: List[Dict[str, Any]] = []
    for n in range(lo, hi + 1):
        res = residuals[n]
        ps = float(np.clip(m_scale(n), *SCALE_BAND))
        band = np.round(np.arange(ps - 0.03, ps + 0.03 + 1e-9, FINE_STEP), 4)
        loc = _locate_near(res, A, band, (FINE_BLUR, FINE_BLUR),
                           int(round(m_x(n))), int(round(m_y(n))))
        if loc is None:
            curve.append({"n": n, "opacity": 0.0, "fit": 0.0, "contain": 0.0, "sigma_x": 0.0,
                          "sigma_y": 0.0, "scale": round(ps, 4), "dx": 0.0, "dy": 0.0}); continue
        patch = _patch(res, loc)
        sx, sy, fitq = _fit_blur(patch, A, loc["scale"])
        Tb = _blurred_template(A, patch.shape, sx, sy)
        wsup = _guarded(_support(Tb), _sub_like(excl_full, loc["x"], loc["y"], patch.shape))
        fitq = max(fitq, _ncc(patch, Tb, wsup)) / f_self
        cov_sup = _guarded(_wide_support(Tb, max(sx, sy)), _sub_like(excl_full, loc["x"], loc["y"], patch.shape))
        alpha = _coverage(patch, Tb, cov_sup)[0] / cov_settled
        s = loc["scale"]
        row = {"n": n, "opacity": round(alpha, 3), "fit": round(fitq, 3),
               "contain": round(contain(loc), 3),
               "sigma_x": sx, "sigma_y": sy, "scale": round(s, 4),
               "dx": round(loc["x"] - hx, 1), "dy": round(loc["y"] - hy, 1)}
        if wslices:
            per = {}
            for (a, b), w in zip(wslices, words):
                wx = int(round(loc["x"] + (a - a0) * s))
                ww = int(round((b - a + 1) * s))
                pw = _sub(res, wx, loc["y"], ww, loc["h"])
                Tw = T[:, a:b + 1]
                wsx, wsy, wfit = _fit_blur(pw, Tw, s)
                Twb = _blurred_template(Tw, pw.shape, wsx, wsy)
                ex = _sub_like(excl_full, wx, loc["y"], pw.shape)
                wsw = _guarded(_support(Twb), ex)
                aw, fw = w_self[w]
                cw = _guarded(_wide_support(Twb, max(wsx, wsy)), ex)
                per[w] = {"opacity": round(_coverage(pw, Twb, cw)[0] / cov_words[w], 3),
                          "fit": round(max(wfit, _ncc(pw, Twb, wsw)) / fw, 3),
                          "sigma_x": wsx, "sigma_y": wsy}
            row["words"] = per
        curve.append(row)
    return {"text": text, "part": part.get("part"), "box": box, "settled_frame": settled_n,
            "polarity": polarity, "words": words, "word_split": bool(wslices),
            "anchor": anchor, "modelled_from": [int(n) for n, _ in good], "curve": curve}


def _present(c: Mapping[str, Any], key: str | None = None) -> bool:
    """Is the ink really on screen on this frame — this ink, on its own box, at a sane size?"""
    if c.get("contain", 0.0) < MIN_CONTAIN or not (SCALE_BAND[0] <= c["scale"] <= SCALE_BAND[1]):
        return False
    o = c["words"][key] if (key and "words" in c) else c
    return ONSET_ALPHA <= o["opacity"] <= 2.0 and o["fit"] >= ONSET_FIT


def _onset(curve: Sequence[Mapping[str, Any]], in_n: int, key: str | None = None) -> int | None:
    """The frame the ink arrives on.

    A caption does not flicker: once it is up it stays up to the end of its state. So rather
    than tripping an absolute threshold — which a faint ghost entry misses and an overlapping
    neighbour ("<line A>" sitting inside the "<Script A>" box) trips early — find the CHANGEPOINT
    that best splits the state into "not there" then "there", and keep it only if the "there"
    half really is a match.
    """
    live = [c for c in curve if c["n"] >= in_n]
    if not live:
        return None
    fits = [float((c["words"][key] if (key and "words" in c) else c)["fit"]) for c in live]
    if all(_present(c, key) for c in live[:2]):
        return int(live[0]["n"])
    best, best_gain = None, 0.0
    for k in range(1, len(live)):
        after = fits[k:]
        if float(np.mean(after)) < ONSET_FIT or not _present(live[k], key):
            continue
        gain = float(np.mean(after)) - float(np.mean(fits[:k]))
        if gain > best_gain:
            best, best_gain = int(live[k]["n"]), gain
    if best is None or best_gain < 0.10:
        best = next((int(c["n"]) for i, c in enumerate(live)
                     if _present(c, key) and (i + 1 >= len(live) or _present(live[i + 1], key))), None)
    if best is None:
        return None
    return _walk_back(live, best, key)


def _walk_back(live: Sequence[Mapping[str, Any]], landed: int, key: str | None = None) -> int:
    """Walk back from where the ink LANDED to where its ramp started.

    A changepoint fires when the ink becomes a good match, which on a soft entry is several
    frames after it first appears: c06 "<Script C>" is plainly on screen at n=140 and only scores
    like itself from n=145. But a blur-in is monotone — opacity climbing, blur falling — so the
    first frame is the earliest one from which that climb runs without reversing.
    """
    rows = [(int(c["n"]), (c["words"][key] if (key and "words" in c) else c)) for c in live]
    idx = {n: i for i, (n, _) in enumerate(rows)}
    i = idx.get(landed)
    if i is None:
        return landed
    floor = max(0.25, 0.55 * float(rows[i][1]["fit"]))
    while i > 0:
        if float(rows[i - 1][1]["fit"]) < floor:
            break                                   # that far back it is not this ink at all
        cur, prev = rows[i][1], rows[i - 1][1]
        if float(prev["opacity"]) > float(cur["opacity"]) + 0.03:
            break                                   # opacity rises going back: not the ramp
        if float(prev["sigma_x"]) + float(prev["sigma_y"]) < float(cur["sigma_x"]) + float(cur["sigma_y"]) - 0.6:
            break                                   # blur falls going back: not the ramp
        if float(prev["opacity"]) < 0.05:
            break                                   # nothing there at all
        i -= 1
    return int(rows[i][0])


def _floor(live: Sequence[Mapping[str, Any]], key: str | None = None) -> Tuple[float, float]:
    """The sigma this part reads once it has settled — its own measurement bias, not a device.

    Alignment quantisation and bed texture leave a residual sigma that never reaches zero on
    some parts (c01 sits at ~4 px for its whole life while the eye sees it sharp from n=7). The
    device is what rises ABOVE this floor.
    """
    tail = list(live[-max(3, len(live) // 3):])
    if not tail:
        return 0.0, 0.0
    rows = [(c["words"][key] if (key and "words" in c) else c) for c in tail]
    return (float(np.median([r["sigma_x"] for r in rows])),
            float(np.median([r["sigma_y"] for r in rows])))


def _sharp_frame(live: Sequence[Mapping[str, Any]], first: int, key: str | None = None) -> int:
    fx, fy = _floor(live, key)
    for c in live:
        if c["n"] < first:
            continue
        o = c["words"][key] if (key and "words" in c) else c
        if (o["sigma_x"] <= max(SHARP_SIGMA, fx + 0.6) and o["sigma_y"] <= max(SHARP_SIGMA, fy + 0.6)
                and o["opacity"] >= SHARP_ALPHA):
            return int(c["n"])
    return int(live[-1]["n"])


def _ramp(live: Sequence[Mapping[str, Any]], first: int, sharp: int,
          key: str | None = None, floor: Tuple[float, float] = (0.0, 0.0)) -> Dict[int, Dict[str, float]]:
    """Clean one unit's measured entry into something replayable.

    The raw per-frame numbers wobble after the entry has landed, because the bed under the ink
    keeps moving and the least-squares alpha picks that up. That wobble is not a device. So the
    opacity is made monotone up to the sharp frame and pinned at 1.0 from there, and the blur is
    made non-increasing over the same window and pinned at 0 from there. Both raw and cleaned
    numbers are written out; this is the cleaned one.
    """
    rows = {int(c["n"]): (c["words"][key] if (key and "words" in c) else c) for c in live}
    ns = sorted(rows)
    out: Dict[int, Dict[str, float]] = {}
    run = 0.0
    for n in ns:
        if n < first:
            out[n] = {"opacity": 0.0, "sigma_x": 0.0, "sigma_y": 0.0}; continue
        if n >= sharp:
            out[n] = {"opacity": 1.0, "sigma_x": 0.0, "sigma_y": 0.0}; continue
        run = max(run, min(1.0, max(0.0, float(rows[n]["opacity"]))))
        out[n] = {"opacity": round(run, 3),
                  "sigma_x": round(max(0.0, float(rows[n]["sigma_x"]) - floor[0]), 2),
                  "sigma_y": round(max(0.0, float(rows[n]["sigma_y"]) - floor[1]), 2)}
    for n in reversed([n for n in ns if first <= n < sharp]):          # blur non-increasing
        nxt = out.get(n + 1)
        if nxt is not None:
            out[n]["sigma_x"] = max(out[n]["sigma_x"], nxt["sigma_x"])
            out[n]["sigma_y"] = max(out[n]["sigma_y"], nxt["sigma_y"])
    return out


def _median3(vals: Sequence[float]) -> List[float]:
    if len(vals) < 3:
        return list(vals)
    out = [vals[0]]
    for i in range(1, len(vals) - 1):
        out.append(float(np.median(vals[i - 1:i + 2])))
    out.append(vals[-1])
    return out


def _axis(sx: float, sy: float) -> str:
    return ("h" if sx >= 1.5 and sx >= 1.6 * max(sy, 0.3) else
            "v" if sy >= 1.5 and sy >= 1.6 * max(sx, 0.3) else "iso")


def summarise(meas: Mapping[str, Any], state: Mapping[str, Any]) -> Dict[str, Any]:
    if "curve" not in meas:
        return dict(meas)
    curve, in_n = meas["curve"], int(state["in"])
    live = [c for c in curve if c["n"] >= in_n]
    first = _onset(curve, in_n)
    if first is None:
        return {"text": meas["text"], "part": meas["part"], "box": meas["box"], "device": None,
                "first_frame": None, "confidence": "low",
                "why": "no frame of this state fits the settled ink above the onset floor",
                "raw_curve": curve}
    eye = EYE_ONSETS.get(str(state.get("id")), {}).get(str(meas["text"]))
    first_auto, first_source = first, "fit"
    if eye:
        first, first_source = eye[0], "eye (reference contact sheet)"
    sharp = _sharp_frame(live, first)
    fl = _floor(live)
    win = [c for c in live if first <= c["n"] <= sharp]
    mx = max((max(0.0, c["sigma_x"] - fl[0]) for c in win), default=0.0)
    my = max((max(0.0, c["sigma_y"] - fl[1]) for c in win), default=0.0)
    ramp = _ramp(live, first, sharp, floor=fl)
    word_onsets, word_ramps = {}, {}
    if meas.get("word_split"):
        for w in meas["words"]:
            wf = _onset(curve, in_n, w)
            eye = EYE_ONSETS.get(str(state.get("id")), {}).get(w)
            if eye:
                word_onsets[w + "__auto"] = wf
                word_onsets[w + "__why"] = eye[1]
                wf = eye[0]
            word_onsets[w] = wf
            if wf is not None:
                word_ramps[w] = _ramp(live, wf, _sharp_frame(live, wf, w), w, _floor(live, w))
    # geometry: a 3-frame median kills the +/-0.005 scale and +/-2 px locate jitter
    sm_scale = _median3([c["scale"] for c in live])
    sm_dx = _median3([c["dx"] for c in live])
    sm_dy = _median3([c["dy"] for c in live])
    out_curve = []
    for i, c in enumerate(live):
        n = int(c["n"])
        r = ramp[n]
        on = n >= first
        row = {"n": n, "opacity": r["opacity"],
               "blur_px": round(max(r["sigma_x"], r["sigma_y"]), 2),
               "blur_axis": _axis(r["sigma_x"], r["sigma_y"]),
               "sigma_x": r["sigma_x"], "sigma_y": r["sigma_y"],
               "scale": round(sm_scale[i], 4) if on else 1.0,
               "dx": round(sm_dx[i], 1) if on else 0.0,
               "dy": round(sm_dy[i], 1) if on else 0.0,
               "fit": c["fit"], "contain": c.get("contain")}
        if word_ramps:
            row["words"] = {w: {"opacity": word_ramps[w][n]["opacity"] if w in word_ramps else 1.0,
                                "sigma_x": word_ramps[w][n]["sigma_x"] if w in word_ramps else 0.0,
                                "sigma_y": word_ramps[w][n]["sigma_y"] if w in word_ramps else 0.0}
                            for w in meas["words"]}
        out_curve.append(row)
    fit_med = float(np.median([c["fit"] for c in win])) if win else 0.0
    return {"text": meas["text"], "part": meas["part"], "box": meas["box"],
            "anchor": meas["anchor"], "settled_frame": meas["settled_frame"],
            "polarity": meas["polarity"], "word_split": meas["word_split"],
            "first_frame": first, "first_frame_auto": first_auto,
            "first_frame_source": first_source,
            "first_frame_why": None if not eye else eye[1],
            "sharp_frame": sharp, "blur_axis": _axis(mx, my),
            "max_sigma_x": round(mx, 2), "max_sigma_y": round(my, 2),
            "sigma_floor": [round(fl[0], 2), round(fl[1], 2)],
            "scale_first": out_curve[0]["scale"] if out_curve else 1.0,
            "scale_last": out_curve[-1]["scale"] if out_curve else 1.0,
            "word_onsets": word_onsets, "median_fit": round(fit_med, 3),
            "confidence": "high" if fit_med >= 0.6 else "medium" if fit_med >= 0.4 else "low",
            "curve": out_curve, "raw_curve": curve}


def count_refframes(refframes: Path) -> int:
    """How many ref-NNN.png frames the directory holds — the reference's own length, never a fixed number."""
    return len([p for p in Path(refframes).glob("ref-*.png")])


def measure(brain: Path, refframes: Path, frames_total: int | None = None) -> Dict[str, Any]:
    if frames_total is None:
        frames_total = count_refframes(refframes)
        if frames_total <= 0:
            raise SystemExit(f"measure_devices: no ref-NNN.png frames under {refframes}")
    states = json.loads((brain / "captions.plaintext.json").read_text())["states"]
    out: Dict[str, Any] = {"schema": "reel-caption-devices-v3", "frames_total": frames_total,
                           "measured_utc": None, "measured_from": str(refframes), "states": {}}
    for st in states:
        per: Dict[str, Any] = {}
        for part in st.get("parts", []):
            m = measure_part(refframes, st, part, frames_total)
            per[m["text"]] = summarise(m, st)
        out["states"][st["id"]] = per
    out["swaps"] = swaps(out, states)
    return out


def swaps(measured: Mapping[str, Any], states: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """In-place replacements (c05a-c, c07a-d): crossfade or hard swap?"""
    by_id = {s["id"]: s for s in states}
    res = {}
    for a, b in (("c05a", "c05b"), ("c05b", "c05c"), ("c07a", "c07b"),
                 ("c07b", "c07c"), ("c07c", "c07d")):
        if a not in by_id or b not in by_id:
            continue
        mb = next(iter(measured["states"][b].values()))
        if "curve" not in mb:
            continue
        cut = int(by_id[b]["in"])
        at = next((c for c in mb["curve"] if c["n"] == cut), None)
        res[f"{a}->{b}"] = {
            "cut_frame": cut, "incoming_first_frame": mb.get("first_frame"),
            "incoming_at_cut": None if not at else {"opacity": at["opacity"], "blur_px": at["blur_px"],
                                                    "scale": at["scale"], "fit": at["fit"]},
            "verdict": "hard swap (full opacity, settled sharpness on the cut frame)"
            if at and at["opacity"] >= 0.75 and at["blur_px"] <= SHARP_SIGMA
            else "ramped — see the curve"}
    return res


if __name__ == "__main__":
    import argparse
    from datetime import datetime, timezone
    ap = argparse.ArgumentParser()
    ap.add_argument("brain"); ap.add_argument("refframes")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--frames", type=int, default=None, help="reference length in frames (default: count the ref-NNN.png files)")
    a = ap.parse_args()
    facts = measure(Path(a.brain), Path(a.refframes), a.frames)
    facts["measured_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dest = Path(a.out) if a.out else Path(a.brain) / "caption_devices.json"
    dest.write_text(json.dumps(facts, indent=1))
    for sid, parts in facts["states"].items():
        for text, m in parts.items():
            if m.get("first_frame") is None:
                print(f"{sid:5s} {text!r:26s} STATIC ({m.get('why', m.get('confidence'))})"); continue
            print(f"{sid:5s} {text!r:26s} first={m['first_frame']:<4d} sharp={m['sharp_frame']:<4d} "
                  f"axis={m['blur_axis']:<4s} sx={m['max_sigma_x']:<5} sy={m['max_sigma_y']:<5} "
                  f"scale {m['scale_first']}->{m['scale_last']}  fit={m['median_fit']} {m['confidence']}"
                  + (f"  words={m['word_onsets']}" if m["word_onsets"] else ""))
    print("\nswaps:", json.dumps(facts["swaps"], indent=1))
    print("\nwrote", dest)
