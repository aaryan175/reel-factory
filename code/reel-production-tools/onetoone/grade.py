#!/usr/bin/env python3
"""onetoone.grade — per-shot HONEST grade match.

What was wrong
--------------
`looksheet.mood_eq` is one global `eq`: a brightness offset and a saturation
multiplier. It can make a shot lighter or darker, but it CANNOT move a colour cast.
So a pale, high-key shot stayed pale where the reference is strongly orange (S09), a warm
low-light shot stayed warm where the reference is deep blue (S12), and a low-key opening shot
stayed flatter than the reference's high-contrast amber/near-black palette (S01). The earlier cure — forcing
the reference's raw percentiles onto our different footage — went garish/posterised
and was rejected.

The shape of the fix
--------------------
TONE and CAST are separated, because they are comparable across different footage to
very different degrees.

  * TONE is one curve on luma (`tone_points`), blended part of the way toward the
    reference's percentile ladder, clamped per level, hardest at the shadow end and
    softest at the highlight end — a percentile at the TOP is mostly composition, not
    grade. Interpolated with `interp=pchip`, monotone by construction, and capped in
    segment slope so a stretch cannot stripe a gradient.
  * CAST is a white-balance-style per-channel gain (`cast_gains`) taken from the
    CHROMATICITY of lit pixels, log-blended, clamped, split into a free warm-cool part
    and a tightly bounded green-magenta part, then re-normalised to preserve luma.
  * SATURATION is one multiplier, chosen from what the curve and the gain have ALREADY
    produced, so the three do not compound.

A per-channel percentile match was tried first and is the thing NOT to go back to: the
two shots' channels do not describe the same objects, so it turned the S09 sunset pink
and banded the S12 dusk. The notes on each function record what was measured.

Nothing here is nominal. `solve` runs its own filter on the shot's probe frame in numpy
(the same maths ffmpeg runs, verified to ~1 code) and on the native frame for banding,
measures skin hue and saturation, highlight blow-out, shadow crush, curve flatness and
posterisation, and walks the three knobs down until every guard passes. That is what
keeps "match the reference" from becoming "garish" without capping the honest moves so
tight that a sunset can never go orange.

If a shot's measurement is unusable, `shot_filter` returns `looksheet.mood_eq` — the
current, proven-safe behaviour — rather than guessing.

Runs in `gbrp10le`: the curve is the one place in this chain that can quantise a sky,
and an identity curve in 10 bits is bit-exact where the same curve in 8 bits moves 58
of 256 codes.

Applied AFTER lut3d, BEFORE the cover crop.
"""
from __future__ import annotations

import datetime
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from onetoone.looksheet import COVER, FPS, LUT, mood_eq, resolve_master

# --------------------------------------------------------------- measurement --

LEVELS: Tuple[int, ...] = (1, 10, 50, 90, 99)
STAT_SIZE = (640, 360)
HUE_BINS = 12
# Hue and saturation are meaningless in the noise floor. A frame that is 80% black
# (reference S01/S05/S12) reported sat_mean 0.79-0.97 before this threshold existed,
# which is what made the old saturation ratio nonsense on every dark shot.
V_FLOOR = 0.10

LUMA = (0.2126, 0.7152, 0.0722)


def _hue_sat_val(rgb: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """HSV hue (deg), saturation (0..1), value (0..1) for HxWx3 float RGB in 0..1."""
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    d = mx - mn
    sat = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    dd = np.maximum(d, 1e-6)
    hr = ((g - b) / dd) % 6.0
    hg = ((b - r) / dd) + 2.0
    hb = ((r - g) / dd) + 4.0
    hue = np.where(mx == r, hr, np.where(mx == g, hg, hb)) * 60.0
    hue = np.where(d > 1e-6, hue % 360.0, 0.0)
    return hue, sat, mx


def _circular_mean_deg(deg: np.ndarray, weights: Optional[np.ndarray] = None) -> float:
    deg = np.asarray(deg, dtype=np.float64)
    if deg.size == 0:
        return float("nan")
    rad = np.deg2rad(deg)
    w = np.ones_like(rad) if weights is None else np.asarray(weights, dtype=np.float64)
    s, c = float((np.sin(rad) * w).sum()), float((np.cos(rad) * w).sum())
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return float("nan")
    return float(math.degrees(math.atan2(s, c)) % 360.0)


def hue_delta(a: float, b: float) -> float:
    """Signed shortest angular distance a->b in degrees, in (-180, 180]."""
    if a is None or b is None or math.isnan(a) or math.isnan(b):
        return float("nan")
    return (b - a + 180.0) % 360.0 - 180.0


def _skin_mask(rgb: np.ndarray) -> np.ndarray:
    """Pixels in the SKIN RANGE: R>G>B, lit, some chroma, and inside the normalised-rgb
    box that skin occupies. Verified by eye on all 12 shots (grade/skinmask_check.png):
    it finds every visible face, and it also finds sand, warm stone and dry foliage.

    So this is a WARM-TONE detector that contains the skin, not a face detector, and it is
    used accordingly — as a self-referential garish limiter on our own frame (skin must not
    rocket in saturation or rotate out of the skin hue range), never as a target copied off
    the reference, whose warm pixels are frequently sky or sand rather than a person.
    """
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    s = np.maximum(r + g + b, 1e-6)
    rn, gn = r / s, g / s
    mx, mn = rgb.max(axis=2), rgb.min(axis=2)
    return ((r > 0.28) & (g > 0.16) & (b > 0.08) & (r > g) & (g > b)
            & ((mx - mn) > 0.055) & ((r - g) > 0.055) & (mx < 0.99)
            & (rn > 0.355) & (rn < 0.47) & (gn > 0.275) & (gn < 0.363))


def _analyse(a: np.ndarray) -> Dict[str, Any]:
    """Every number this module grades or guards on, from one HxWx3 float array (0..1)."""
    lum = a[..., 0] * LUMA[0] + a[..., 1] * LUMA[1] + a[..., 2] * LUMA[2]
    hue, sat, val = _hue_sat_val(a)
    lit = val >= V_FLOOR

    out: Dict[str, Any] = {
        "lum_pct": {str(k): float(np.percentile(lum, k)) for k in LEVELS},
        "ch_pct": {c: {str(k): float(np.percentile(a[..., i], k)) for k in LEVELS}
                   for i, c in enumerate("rgb")},
        "mean_rgb": [float(a[..., i].mean()) for i in range(3)],
        "lum_mean": float(lum.mean()),
        "lum_sd": float(lum.std()),
        "lit_fraction": float(lit.mean()),
        # saturation on LIT pixels only — the honest mood number
        "sat_mean": float(sat[lit].mean()) if lit.any() else 0.0,
        # channel means over LIT pixels — the basis for the cast, immune to the noise floor
        "lit_mean_rgb": ([float(a[..., i][lit].mean()) for i in range(3)] if lit.any()
                         else [float(a[..., i].mean()) for i in range(3)]),
        "clipped": float((a.max(axis=2) >= 0.995).mean()),
        "crushed": float((a.max(axis=2) <= 0.004).mean()),
        "highlight": float((a.max(axis=2) >= SPECULAR_V).mean()),
    }
    # colour cast = where the most-saturated fifth of the LIT picture sits on the wheel
    if lit.sum() > 64:
        hs, ws = hue[lit], sat[lit]
        thr = float(np.percentile(ws, 80))
        sel = ws >= max(thr, 0.02)
        hs, ws = hs[sel], ws[sel]
        hist, _ = np.histogram(hs, bins=HUE_BINS, range=(0.0, 360.0), weights=ws)
        tot = float(hist.sum())
        out["cast"] = {
            "hue_mean_deg": _circular_mean_deg(hs, ws),
            "sat_of_top": float(ws.mean()) if ws.size else 0.0,
            "hue_hist": [round(float(v) / tot, 4) for v in hist] if tot > 0 else [0.0] * HUE_BINS,
            "dominant_hue_bin": int(np.argmax(hist)) if tot > 0 else -1,
            "pixels": int(sel.sum()),
        }
    else:
        out["cast"] = {"hue_mean_deg": float("nan"), "sat_of_top": 0.0,
                       "hue_hist": [0.0] * HUE_BINS, "dominant_hue_bin": -1, "pixels": 0}
    sk = _skin_mask(a) & lit
    n = int(sk.sum())
    out["skin"] = {
        "fraction": float(n) / sk.size,
        "hue_mean_deg": _circular_mean_deg(hue[sk], sat[sk]) if n > 64 else None,
        "sat_mean": float(sat[sk].mean()) if n > 64 else None,
        "lum_mean": float(lum[sk].mean()) if n > 64 else None,
    }
    return out


def _load(path: Path) -> np.ndarray:
    im = Image.open(path).convert("RGB")
    if im.size != STAT_SIZE:
        im = im.resize(STAT_SIZE, Image.BILINEAR)
    return np.asarray(im, dtype=np.float32) / 255.0


def frame_profile(path: Path) -> Dict[str, Any]:
    prof = _analyse(_load(Path(path)))
    prof["frames"] = 1
    prof["probe"] = str(path)
    return prof


def banding_report(path: Path, *, block: int = 48, lo_sd: float = 0.8,
                   hi_sd: float = 6.0) -> Dict[str, Any]:
    """Posterisation probe: how stepped are this frame's SMOOTH GRADIENTS?

    Only gradient blocks are looked at — tiles whose 8-bit luma standard deviation is
    between `lo_sd` and `hi_sd`, i.e. varying but not detailed. Sky, walls and out-of-focus
    backgrounds qualify; foliage and faces do not. Within each such tile, `worst_run` is
    the widest run of 8-bit codes left unused between its own darkest and brightest pixel,
    which is the width of a visible step.

    A whole-strip histogram was tried first and is NOT this measurement: a strip holding
    bright sky and dark hair has a huge empty middle and scores as badly banded while
    looking perfect. Restricting to gradient tiles is what makes the number mean banding.
    """
    a = np.asarray(Image.open(Path(path)).convert("RGB"), dtype=np.float64)
    lum = np.rint(np.clip(a[..., 0] * LUMA[0] + a[..., 1] * LUMA[1] + a[..., 2] * LUMA[2],
                          0, 255)).astype(np.int32)
    h, w = lum.shape
    worst, runs, tiles = 0, [], 0
    for y in range(0, h - block + 1, block):
        for x in range(0, w - block + 1, block):
            t = lum[y:y + block, x:x + block]
            sd = float(t.std())
            if not (lo_sd <= sd <= hi_sd):
                continue
            tiles += 1
            used = np.unique(t)
            if used.size < 2:
                continue
            r = int(np.max(np.diff(used)) - 1)
            runs.append(r)
            worst = max(worst, r)
    return {"gradient_tiles": tiles, "worst_run": worst,
            "mean_run": round(float(np.mean(runs)), 3) if runs else 0.0,
            "tiles_stepped": int(sum(1 for r in runs if r > MAX_CODE_STEP))}


def shot_stats(paths: Sequence[Path], *, native_probe: Optional[Path] = None) -> Dict[str, Any]:
    """Per-shot stats = the MEDIAN across sampled frames; probe = the middle frame.

    `native_probe` is a full-resolution frame of the same shot, used only for the 8-bit
    code census the banding guard needs; defaults to the middle frame, which is already
    native when the caller (render.py) works at delivery geometry.
    """
    profs = [frame_profile(Path(p)) for p in paths]
    if not profs:
        return {}

    def med(fn) -> float:
        vals = [fn(p) for p in profs]
        vals = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
        return float(np.median(vals)) if vals else float("nan")

    out: Dict[str, Any] = {
        "frames": len(profs),
        "probe": str(paths[len(paths) // 2]),
        "lum_pct": {str(k): med(lambda p, k=k: p["lum_pct"][str(k)]) for k in LEVELS},
        "ch_pct": {c: {str(k): med(lambda p, c=c, k=k: p["ch_pct"][c][str(k)]) for k in LEVELS}
                   for c in "rgb"},
        "mean_rgb": [med(lambda p, i=i: p["mean_rgb"][i]) for i in range(3)],
        "lit_mean_rgb": [med(lambda p, i=i: p["lit_mean_rgb"][i]) for i in range(3)],
        "lum_mean": med(lambda p: p["lum_mean"]),
        "lum_sd": med(lambda p: p["lum_sd"]),
        "sat_mean": med(lambda p: p["sat_mean"]),
        "lit_fraction": med(lambda p: p["lit_fraction"]),
        "clipped": med(lambda p: p["clipped"]),
        "crushed": med(lambda p: p["crushed"]),
    }
    hues = [p["cast"]["hue_mean_deg"] for p in profs if not math.isnan(p["cast"]["hue_mean_deg"])]
    hist = np.mean([p["cast"]["hue_hist"] for p in profs], axis=0)
    out["cast"] = {"hue_mean_deg": _circular_mean_deg(np.array(hues)) if hues else float("nan"),
                   "sat_of_top": med(lambda p: p["cast"]["sat_of_top"]),
                   "hue_hist": [round(float(v), 4) for v in hist],
                   "dominant_hue_bin": int(np.argmax(hist)) if float(np.max(hist)) > 0 else -1}
    sk = [p["skin"] for p in profs if p["skin"]["hue_mean_deg"] is not None]
    out["skin"] = {"fraction": med(lambda p: p["skin"]["fraction"]),
                   "hue_mean_deg": _circular_mean_deg(np.array([s["hue_mean_deg"] for s in sk])) if sk else None,
                   "sat_mean": float(np.median([s["sat_mean"] for s in sk])) if sk else None,
                   "lum_mean": float(np.median([s["lum_mean"] for s in sk])) if sk else None}
    src = Path(native_probe) if native_probe else Path(paths[len(paths) // 2])
    out["probe_native"] = str(src)
    out["per_frame"] = [{k: v for k, v in p.items() if k != "per_frame"} for p in profs]
    return out


# ------------------------------------------------------------------- the law --

ALPHA_TONE = 0.75          # how far toward the reference's tone we aim (1.0 = force-match)
# ...but not equally at every level. A percentile says "this share of the frame is this
# bright", and at the TOP that is mostly composition, not grade: the reference's bright
# decile on S01 is a large bright region, ours is a small local highlight. Chasing it lit
# our background up and cost the shot the moodiness the whole fix was for. Black level and
# mid placement ARE comparable across content, so the shadow end is matched hardest.
ALPHA_LEVEL = {1: 0.85, 10: 0.75, 50: 0.70, 90: 0.45, 99: 0.35}
ALPHA_CAST = 0.70          # ...and toward its colour cast
ALPHA_SAT = 0.65
# Ceiling on one channel's white-balance gain, before luma re-normalisation.
CAST_CLAMP = 0.34
# Ceiling on the GREEN-MAGENTA half of the cast, in log gain. Real light moves a picture
# along the warm-cool axis; the green-magenta axis is a tint correction and is small in
# nature. Unconstrained, S01's reference (a narrow amber light band over near-black shadows) asked for
# green to drop 13% below red and blue, and at native pixels our neutral whites and greys
# came out pink while every other guard passed. Warmth is free; magenta is not.
CAST_TINT_MAX = 0.06
# Ceiling on one control point's travel, in 0..1 code units, per percentile level.
# Tight at the ends (protect black and white), loosest in the mids (where mood lives).
TONE_CLAMP = {1: 0.060, 10: 0.160, 50: 0.220, 90: 0.120, 99: 0.050}
SAT_MIN, SAT_MAX = 0.85, 1.45
MIN_STEP = 0.004

# Guards, checked against a SIMULATION of the filter on the shot's own probe frame.
#
# The rule they encode: moving TOWARD the reference is matching, and is allowed; moving
# PAST it, or away from it, is where "garish" starts. So most guards are stated relative
# to the reference's own measurement, not to a flat cosmetic number.
SKIN_HUE_RANGE = (5.0, 50.0)     # degrees; outside this, skin has stopped being skin
SKIN_HUE_SHIFT_MAX = 18.0        # degrees of rotation we will accept on skin-range pixels
SKIN_SAT_HARD = 0.65             # absolute ceiling on their mean saturation
SKIN_SAT_RISE_MAX = 0.22         # and on how much saturation we may ADD to them
SKIN_MIN_FRACTION = 0.02         # below this the sample is too small to guard on
# Blowing out a sun disc or a specular is what a sunset does; blowing out a MIDTONE is
# detail loss. Both guards therefore look only at pixels that had detail to lose.
SPECULAR_V = 0.90                # above this the pixel is already a highlight, not detail
SHADOW_V = 0.10                  # below this the pixel is already shadow, not detail
CLIP_RISE_MAX = 0.020            # extra fraction of MIDTONE pixels allowed to hit the top
CRUSH_RISE_MAX = 0.060           # extra fraction of MIDTONE pixels allowed to go to black
MIN_SLOPE = 0.12                 # flattest the curve may get inside p10..p90 (flat == banding)
MAX_CODE_STEP = 2                # a gradient tile is "stepped" once a gap exceeds this
# Banding budget: the share of gradient tiles that may carry a visible step. Calibrated
# against measured renders: untouched shots sit at 1-2%, the v006 grade at
# 1-2%, and the settings that visibly striped S02's shadows sat at 12%.
BAND_STEPPED_RISE = 0.02         # ...may rise this much above the ungraded frame's share
BAND_STEPPED_FLOOR = 0.04        # ...and is always allowed up to here
# A segment steeper than this pulls neighbouring input codes apart faster than 8-bit output
# can carry, and a smooth sky comes back striped. Measured on the v3 pass: S12's p90->p99
# segment ran at slope 5.1 and left 24 empty luma codes with a 9-code step in the dusk sky.
MAX_SEG_SLOPE = 2.2
STRENGTHS = (1.0, 0.85, 0.7, 0.55, 0.4, 0.25)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def reliable(cand: Mapping[str, Any], ref: Mapping[str, Any]) -> Tuple[bool, str]:
    """Is this measurement good enough to grade on? If not, the caller uses mood_eq."""
    for name, st in (("cand", cand), ("ref", ref)):
        if not st or "ch_pct" not in st or "lum_pct" not in st:
            return False, f"{name}: no measurement"
        if int(st.get("frames", 0)) < 1:
            return False, f"{name}: no frames measured"
        vals = ([st["ch_pct"][c][str(k)] for c in "rgb" for k in LEVELS]
                + [st["lum_pct"][str(k)] for k in LEVELS])
        if any(v is None or math.isnan(v) for v in vals):
            return False, f"{name}: NaN in percentiles"
    spread = cand["lum_pct"]["99"] - cand["lum_pct"]["1"]
    if spread < 0.06:
        return False, f"cand: flat frame, luma p99-p1 = {spread:.3f}"
    return True, "ok"


def tone_points(cand: Mapping[str, Any], ref: Mapping[str, Any],
                strength: float = 1.0) -> List[Tuple[float, float]]:
    """ONE curve, on luma, from the two percentile ladders: blended, clamped, monotonic.

    Deliberately not per-channel. A per-channel percentile match was tried first (v1 of
    this module) and it produced cross-content artefacts, because the two shots' channels
    do not describe the same objects: the reference's red p50 is its orange sky while ours
    is a mix of neutral high-key surfaces. Matching them independently turned the S09 sunset pink and
    put a green band across the S12 dusk. Tone is comparable across content; cast is not,
    and is handled separately by `cast_gains`.

    x strictly increasing, y non-decreasing, endpoints pinned at 0/0 and 1/1.
    """
    s = _clamp(strength, 0.0, 1.0)
    pts: List[Tuple[float, float]] = [(0.0, 0.0)]
    for k in LEVELS:
        x = _clamp(float(cand["lum_pct"][str(k)]), 0.0, 1.0)
        yr = _clamp(float(ref["lum_pct"][str(k)]), 0.0, 1.0)
        a = ALPHA_TONE * ALPHA_LEVEL[k] / ALPHA_LEVEL[50]      # p50 carries the headline alpha
        y = _clamp(x + a * s * (yr - x), x - TONE_CLAMP[k] * s, x + TONE_CLAMP[k] * s)
        if x <= pts[-1][0] + MIN_STEP or x >= 1.0 - MIN_STEP:
            continue                                     # percentiles collapsed here
        # never steeper than MAX_SEG_SLOPE: that is what stripes a gradient in 8-bit output
        y = min(y, pts[-1][1] + MAX_SEG_SLOPE * (x - pts[-1][0]))
        y = _clamp(max(y, pts[-1][1] + MIN_STEP / 2), 0.0, 1.0 - MIN_STEP / 2)
        pts.append((x, y))
    # The top point is NOT pinned at 1/1. Pinning it made the last segment as steep as it
    # had to be to reach white — slope 6.7 on S09, where p99 already sits at 0.988 — which
    # is exactly the gradient-striping this cap exists to stop. Rolling the white point off
    # at the same slope limit is also what the reference itself does: its own 99th
    # percentile peaks at 236-240 and nothing in it clips to 255 (brain/cutgrid.json).
    x_top, y_top = pts[-1]
    pts.append((1.0, min(1.0, y_top + MAX_SEG_SLOPE * (1.0 - x_top))))
    return pts


def cast_gains(cand: Mapping[str, Any], ref: Mapping[str, Any],
               strength: float = 1.0) -> Tuple[float, float, float]:
    """A white-balance-style per-channel gain that carries the reference's colour cast.

    Built from the CHROMATICITY of lit pixels (each channel's share of the total), so it
    says "the reference is redder and much less blue than us" without caring where in the
    frame that red is. Blended in log space, clamped per channel, then re-normalised so
    the gain is luma-preserving — the tone curve, not the cast, decides brightness.
    """
    s = _clamp(strength, 0.0, 1.0)
    c_m = [max(float(v), 1e-5) for v in cand.get("lit_mean_rgb", cand["mean_rgb"])]
    r_m = [max(float(v), 1e-5) for v in ref.get("lit_mean_rgb", ref["mean_rgb"])]
    c_n = [v / sum(c_m) for v in c_m]
    r_n = [v / sum(r_m) for v in r_m]
    lg = []
    for i in range(3):
        raw = math.log(r_n[i] / c_n[i]) * ALPHA_CAST * s
        lim = math.log(1.0 + CAST_CLAMP)
        lg.append(_clamp(raw, -lim, lim))
    # split into warm-cool (free) and green-magenta (clamped), then put it back together
    temp = (lg[0] - lg[2]) / 2.0
    tint = _clamp(lg[1] - (lg[0] + lg[2]) / 2.0, -CAST_TINT_MAX, CAST_TINT_MAX)
    base = (lg[0] + lg[2]) / 2.0
    g = [math.exp(base + temp), math.exp(base + tint), math.exp(base - temp)]
    k = 1.0 / max(sum(LUMA[i] * g[i] for i in range(3)), 1e-6)
    return tuple(round(v * k, 5) for v in g)  # type: ignore[return-value]


def saturation_target(cand: Mapping[str, Any], ref: Mapping[str, Any],
                      strength: float = 1.0) -> float:
    """The mean saturation we want the FINISHED shot to sit at (lit pixels only)."""
    cs, rs = float(cand["sat_mean"]), float(ref["sat_mean"])
    if cs <= 1e-4:
        return 0.0
    s = _clamp(strength, 0.0, 1.0)
    return float(_clamp(cs + (rs - cs) * ALPHA_SAT * s, cs * SAT_MIN, cs * SAT_MAX))


def saturation_factor(cand: Mapping[str, Any], ref: Mapping[str, Any],
                      strength: float = 1.0, *,
                      after_curve: Optional[float] = None) -> float:
    """The `colorchannelmixer` multiplier that lands the shot on `saturation_target`.

    `after_curve` is the saturation the curve ALREADY produced. Passing it is the whole
    point: the per-channel curve moves chroma by itself, so multiplying by the raw
    cand->ref ratio on top of it double-counts. Measured on the first pass, that
    double-count is what made the S09 sunset pink and tinted the S12 face magenta.
    """
    cs, rs = float(cand["sat_mean"]), float(ref["sat_mean"])
    if cs <= 1e-4:
        return 1.0
    base = cs if after_curve is None else max(float(after_curve), 1e-4)
    if base > rs:
        # the curve already produced MORE chroma than the reference itself carries:
        # pull back toward the reference, never below it
        return round(_clamp(rs / base, SAT_MIN, 1.0), 3)
    return round(_clamp(saturation_target(cand, ref, strength) / base, 1.0, SAT_MAX), 3)


# ----------------------------------------------------------------- the maths --

def pchip(pts: Sequence[Tuple[float, float]], x: np.ndarray) -> np.ndarray:
    """Fritsch-Carlson monotone cubic Hermite — what ffmpeg's `curves=interp=pchip` runs."""
    px = np.array([p[0] for p in pts], dtype=np.float64)
    py = np.array([p[1] for p in pts], dtype=np.float64)
    n = len(px)
    if n < 2:
        return np.clip(x, 0.0, 1.0)
    h = np.diff(px)
    delta = np.diff(py) / h
    m = np.zeros(n)
    m[1:-1] = np.where(delta[:-1] * delta[1:] > 0,
                       2.0 / (1.0 / np.maximum(delta[:-1], 1e-12) + 1.0 / np.maximum(delta[1:], 1e-12)),
                       0.0)
    m[0] = delta[0]
    m[-1] = delta[-1]
    for i in range(n - 1):                       # keep the tangents inside the monotone cone
        if delta[i] == 0:
            m[i] = m[i + 1] = 0.0
        else:
            a, b = m[i] / delta[i], m[i + 1] / delta[i]
            t = a * a + b * b
            if t > 9.0:
                f = 3.0 / math.sqrt(t)
                m[i], m[i + 1] = f * a * delta[i], f * b * delta[i]
    xv = np.clip(np.asarray(x, dtype=np.float64), px[0], px[-1])
    idx = np.clip(np.searchsorted(px, xv, side="right") - 1, 0, n - 2)
    hh = h[idx]
    t = (xv - px[idx]) / hh
    t2, t3 = t * t, t * t * t
    y = ((2 * t3 - 3 * t2 + 1) * py[idx] + (t3 - 2 * t2 + t) * hh * m[idx]
         + (-2 * t3 + 3 * t2) * py[idx + 1] + (t3 - t2) * hh * m[idx + 1])
    return np.clip(y, 0.0, 1.0)


def sat_matrix(s: float) -> np.ndarray:
    """The exact 3x3 `colorchannelmixer` uses: out_c = Y + s*(c - Y), Y = BT.709 luma."""
    ly, lg, lb = LUMA
    return np.array([
        [ly + s * (1 - ly), lg * (1 - s), lb * (1 - s)],
        [ly * (1 - s), lg + s * (1 - lg), lb * (1 - s)],
        [ly * (1 - s), lg * (1 - s), lb + s * (1 - lb)],
    ], dtype=np.float64)


def grade_matrix(gains: Sequence[float], sat: float) -> np.ndarray:
    """Cast gain and saturation are both 3x3 linear ops, so they ship as ONE mixer."""
    return sat_matrix(sat) @ np.diag(np.asarray(gains, dtype=np.float64))


def simulate(a: np.ndarray, pts: Sequence[Tuple[float, float]],
             gains: Sequence[float], sat: float) -> np.ndarray:
    """Run our own filter, in numpy, exactly as ffmpeg will: pchip luma curve, then mixer."""
    out = pchip(pts, np.asarray(a, dtype=np.float64))
    m = grade_matrix(gains, sat)
    if not np.allclose(m, np.eye(3), atol=1e-4):
        out = np.clip(np.einsum("...j,ij->...i", out, m), 0.0, 1.0)
    return out


def _slopes(pts: Sequence[Tuple[float, float]], lo: float, hi: float) -> Optional[np.ndarray]:
    lo, hi = max(0.0, min(lo, hi)), min(1.0, max(lo, hi))
    if hi - lo < 1e-3:
        return None
    x = np.linspace(lo, hi, 256)
    return np.diff(pchip(pts, x)) / np.diff(x)


def min_slope(pts: Sequence[Tuple[float, float]], lo: float, hi: float) -> float:
    """Flattest the rendered curve gets inside [lo, hi] — a flat step merges tones."""
    s = _slopes(pts, lo, hi)
    return 1.0 if s is None else float(np.min(s))


def max_slope(pts: Sequence[Tuple[float, float]], lo: float, hi: float) -> float:
    """Steepest the rendered curve gets inside [lo, hi] — a steep step stripes a gradient."""
    s = _slopes(pts, lo, hi)
    return 1.0 if s is None else float(np.max(s))


def banding_delta(native_probe: Path, pts: Sequence[Tuple[float, float]],
                  gains: Sequence[float], sat: float) -> Dict[str, Any]:
    """Run this filter on the shot's NATIVE frame and measure the banding it adds.

    Not a proxy. Curve slope was tried (condemned S09 over the top 1% of codes that
    nothing occupies) and a frame-wide code census was tried (said our darkest interiors
    were clean because they use 250 of 256 codes across the whole picture, while the
    single shadow tile that bands uses a dozen). This renders the frame and counts tiles.

    Native matters twice over: downsampling fills in codes the real frame never had, and
    the masters differ — S01/S02 are 8-bit yuvj420p while S09/S12 are 10-bit yuv422p10le,
    which is exactly why the two night interiors are the two that band under a stretch.
    """
    src_path = Path(native_probe)
    a = np.asarray(Image.open(src_path).convert("RGB"), dtype=np.float64) / 255.0
    before = banding_report(src_path)
    out = np.rint(simulate(a, pts, gains, sat) * 255.0).astype(np.uint8)
    tmp = src_path.with_name(src_path.stem + "__bandprobe.png")
    Image.fromarray(out).save(tmp)
    try:
        after = banding_report(tmp)
    finally:
        tmp.unlink(missing_ok=True)

    def frac(r: Mapping[str, Any]) -> float:
        return r["tiles_stepped"] / max(r["gradient_tiles"], 1)

    return {"before": before, "after": after,
            "stepped_before": round(frac(before), 4), "stepped_after": round(frac(after), 4)}


def check_guards(cand: Mapping[str, Any], ref: Mapping[str, Any],
                 pts: Sequence[Tuple[float, float]], gains: Sequence[float], sat: float,
                 banding: Optional[Mapping[str, Any]] = None) -> Tuple[bool, List[str], Dict[str, Any]]:
    """Measure the filter's own output on the probe frame. Returns (ok, failures, measured)."""
    fails: List[str] = []
    measured: Dict[str, Any] = {}

    lo = cand["lum_pct"]["10"] if "lum_pct" in cand else 0.05
    hi = cand["lum_pct"]["90"] if "lum_pct" in cand else 0.95
    slope = round(min_slope(pts, lo, hi), 4)
    measured["min_slope"] = slope
    if slope < MIN_SLOPE:
        fails.append(f"tone curve flattens to slope {slope:.3f} inside p10..p90 (banding risk)")
    measured["max_slope"] = round(max_slope(pts, cand["lum_pct"]["1"], cand["lum_pct"]["99"]), 4)
    if banding:
        measured["banding"] = {k: v for k, v in banding.items() if k in ("stepped_before", "stepped_after")}
        measured["banding"]["tiles"] = [banding["before"]["gradient_tiles"], banding["after"]["gradient_tiles"]]
        limit = max(banding["stepped_before"] + BAND_STEPPED_RISE, BAND_STEPPED_FLOOR)
        if banding["stepped_after"] > limit:
            fails.append(f"banding: {banding['stepped_after']:.1%} of gradient tiles carry a "
                         f"visible step, up from {banding['stepped_before']:.1%} (limit {limit:.1%})")
    else:
        measured["banding"] = "not checked: no native probe for this shot"

    probe = cand.get("probe")
    if not probe or not Path(probe).exists():
        measured["simulated"] = False
        return (not fails), fails, measured
    measured["simulated"] = True
    src = _load(Path(probe)).astype(np.float64)
    dst = simulate(src, pts, gains, sat)
    before, after = _analyse(src.astype(np.float32)), _analyse(dst.astype(np.float32))
    for tag, st in (("before", before), ("after", after)):
        measured[tag] = {"skin": st["skin"], "clipped": round(st["clipped"], 4),
                         "crushed": round(st["crushed"], 4), "sat_mean": round(st["sat_mean"], 4),
                         "lum_mean": round(st["lum_mean"], 4),
                         "cast_hue": st["cast"]["hue_mean_deg"]}

    # detail-only clip/crush: a pixel that was already a highlight or already shadow had
    # nothing left to lose, so pushing it to the rail is not damage.
    v0, v1 = src.max(axis=2), dst.max(axis=2)
    detail = (v0 < SPECULAR_V) & (v0 > SHADOW_V)
    n = max(int(detail.sum()), 1)
    blew = float(((v1 >= 0.995) & detail).sum()) / n
    sank = float(((v1 <= 0.004) & detail).sum()) / n
    measured["midtone_blown"] = round(blew, 4)
    measured["midtone_crushed"] = round(sank, 4)
    if blew > CLIP_RISE_MAX:
        fails.append(f"{blew:.3%} of midtone detail blows to white (max {CLIP_RISE_MAX:.1%})")
    if sank > CRUSH_RISE_MAX:
        fails.append(f"{sank:.3%} of midtone detail crushes to black (max {CRUSH_RISE_MAX:.1%})")

    b_sk, a_sk = before["skin"], after["skin"]
    measured["ref_skin_fyi"] = {k: (round(v, 4) if isinstance(v, float) else v)
                                for k, v in ((ref or {}).get("skin") or {}).items()}
    if (b_sk["hue_mean_deg"] is not None and a_sk["hue_mean_deg"] is not None
            and b_sk["fraction"] >= SKIN_MIN_FRACTION):
        h0, h1 = b_sk["hue_mean_deg"], a_sk["hue_mean_deg"]
        d = hue_delta(h0, h1)
        measured["skin_hue_shift"] = round(d, 2)
        if not (SKIN_HUE_RANGE[0] <= h1 <= SKIN_HUE_RANGE[1]):
            fails.append(f"skin hue lands at {h1:.1f} deg, outside {SKIN_HUE_RANGE}")
        if abs(d) > SKIN_HUE_SHIFT_MAX:
            fails.append(f"skin hue rotates {d:+.1f} deg (max {SKIN_HUE_SHIFT_MAX})")
        s0, s1 = b_sk["sat_mean"], a_sk["sat_mean"]
        ceiling = min(SKIN_SAT_HARD, s0 + SKIN_SAT_RISE_MAX)
        measured["skin_sat_ceiling"] = round(ceiling, 3)
        if s1 > ceiling:
            fails.append(f"skin saturation {s0:.3f} -> {s1:.3f}, over the ceiling {ceiling:.3f}")
    else:
        measured["skin_guard"] = f"not applied: skin fraction {b_sk['fraction']:.4f}"
    return (not fails), fails, measured


# --------------------------------------------------------------- the product --

def solve(cand_stats: Mapping[str, Any], ref_stats: Mapping[str, Any]) -> Dict[str, Any]:
    """Pick the strongest settings whose own MEASURED output passes every guard.

    Three knobs, searched independently, strongest first:
      * tone       — the luma curve: dark where the reference is dark;
      * cast       — the white-balance gain: orange where it is orange, blue where blue;
      * saturation — one global multiplier on top.
    They are separate because they fail for different reasons, and coupling them throws
    away the good half. On S01 the full cast rotated the face 19 degrees toward red, which
    is a cast fault; when the knobs were one, the shot lost its darkness to fix its redness.
    On S12 the full curve is exactly right while the full saturation multiplier would have
    taken the face past the reference. So each failure is charged to the knob that caused it.
    """
    ok, why = reliable(cand_stats, ref_stats)
    if not ok:
        return {"mode": "fallback", "why": why, "strength": 0.0}
    probe = cand_stats.get("probe")
    src = _load(Path(probe)).astype(np.float64) if probe and Path(probe).exists() else None
    tried: List[Dict[str, Any]] = []
    # Full grid, strongest first. Not a per-knob backoff: which knob caused a failure is
    # not readable from the failure. On S01 the blow-out guard fired on the cast/saturation
    # mixer, yet weakening the TONE curve made it worse (less darkening left more pixels
    # near the rail), so a classifier would have walked the wrong way. Ties prefer tone,
    # because "dark where the reference is dark" is the thing being fixed.
    # tone 0.0 is a real option, not a degenerate one: it is "cast and saturation only,
    # leave the tone curve alone". S02's master is 8-bit and its shadows band under any
    # stretch, but its colour still wants to move, and a multiplicative gain does not
    # stretch the tone scale. Without this rung that shot fell all the way to mood_eq.
    tones = tuple(STRENGTHS) + (0.0,)
    grid = sorted(((t, c) for t in tones for c in STRENGTHS),
                  key=lambda tc: (-(tc[0] + tc[1]), -tc[0]))
    last_pts: Dict[float, List[Tuple[float, float]]] = {}
    band_cache: Dict[Tuple[float, float], Optional[Dict[str, Any]]] = {}
    native = cand_stats.get("probe_native")
    for s_tone, s_cast in grid:
        pts = last_pts.setdefault(s_tone, tone_points(cand_stats, ref_stats, s_tone))
        gains = cast_gains(cand_stats, ref_stats, s_cast)
        if (s_tone, s_cast) not in band_cache:
            # Measured with the real gains. Measuring with an identity mixer was close but
            # not close enough: it passed S01 at a predicted 3.9% of stepped tiles and the
            # rendered frame came back at 4.9%, because the cast stretches channels too.
            # The saturation multiplier is left out of the key: it is a chroma rotation
            # about luma, so it moves banding only through clipping, which has its own guard.
            band_cache[(s_tone, s_cast)] = (banding_delta(Path(native), pts, gains, 1.0)
                                            if native and Path(native).exists() else None)
        band = band_cache[(s_tone, s_cast)]
        if True:
            if src is None:
                top = saturation_factor(cand_stats, ref_stats, s_cast)
            else:
                pre = _analyse(simulate(src, pts, gains, 1.0).astype(np.float32))["sat_mean"]
                top = saturation_factor(cand_stats, ref_stats, s_cast, after_curve=pre)
            # try the wanted multiplier first, then walk back toward neutral (1.0)
            lo, hi = (top, 1.0) if top <= 1.0 else (1.0, top)
            ladder = sorted({round(v, 3) for v in np.arange(lo, hi + 1e-6, 0.05)} | {round(top, 3), 1.0},
                            key=lambda v: abs(v - top))
            ladder = [v for v in ladder if lo - 1e-9 <= v <= hi + 1e-9] or [1.0]
            fails: List[str] = []
            for sat in ladder:
                passed, fails, measured = check_guards(cand_stats, ref_stats, pts, gains, sat, band)
                tried.append({"tone": s_tone, "cast": s_cast, "sat": sat,
                              "passed": passed, "fails": fails})
                if passed:
                    return {"mode": "curves", "why": why, "strength": s_tone, "cast_strength": s_cast,
                            "points": pts, "gains": list(gains), "sat": sat,
                            "sat_wanted": round(top, 3),
                            "sat_target": round(saturation_target(cand_stats, ref_stats, s_cast), 4),
                            "guards": measured, "tried": tried}
                if not any(f.startswith("skin saturation") for f in fails):
                    break       # a weaker multiplier cannot fix a tone or cast failure
    # Nothing passed even at the gentlest setting: do not guess, keep the proven behaviour.
    return {"mode": "fallback", "why": "guards failed at every strength", "strength": 0.0,
            "tried": tried}


def _fmt(pts: Sequence[Tuple[float, float]]) -> str:
    return " ".join(f"{x:.4f}/{y:.4f}" for x, y in pts)


def filter_from_solution(sol: Mapping[str, Any], cand_stats: Mapping[str, Any],
                         ref_stats: Mapping[str, Any]) -> str:
    if sol["mode"] != "curves":
        c = (float(cand_stats.get("lum_mean", 0.0)) * 255.0, float(cand_stats.get("sat_mean", 0.0)) * 255.0, 0.0)
        r = (float(ref_stats.get("lum_mean", 0.0)) * 255.0, float(ref_stats.get("sat_mean", 0.0)) * 255.0, 0.0)
        return mood_eq(c, r)
    chain = ["format=gbrp10le"]
    if sol["strength"] > 0:
        chain.append(f"curves=interp=pchip:all='{_fmt(sol['points'])}'")
    m = grade_matrix(sol["gains"], sol["sat"])
    if not np.allclose(m, np.eye(3), atol=1e-4):
        chain.append("colorchannelmixer="
                     + ":".join(f"{a}{b}={m[i][j]:.4f}"
                                for i, a in enumerate("rgb") for j, b in enumerate("rgb")))
    return ",".join(chain)


def shot_filter(cand_stats: Mapping[str, Any], ref_stats: Mapping[str, Any]) -> str:
    """The ffmpeg filter fragment for ONE shot. Applied after lut3d, before the cover crop.

    Falls back to `looksheet.mood_eq` whenever the measurement or the guards say so.
    """
    return filter_from_solution(solve(cand_stats, ref_stats), cand_stats, ref_stats)


def explain(slot: str, cand: Mapping[str, Any], ref: Mapping[str, Any]) -> Dict[str, Any]:
    """A measured account of what the filter does to one shot — no adjectives, no guesses."""
    sol = solve(cand, ref)
    row: Dict[str, Any] = {"slot": slot, "mode": sol["mode"], "why": sol["why"],
                           "strength": sol["strength"],
                           "cast_strength": sol.get("cast_strength"),
                           "filter": filter_from_solution(sol, cand, ref)}
    for tag, st in (("cand", cand), ("ref", ref)):
        if st:
            row[tag] = {"lum_p50": round(st["lum_pct"]["50"], 4),
                        "lum_mean": round(st["lum_mean"], 4),
                        "sat_mean": round(st["sat_mean"], 4),
                        "cast_hue": (None if math.isnan(st["cast"]["hue_mean_deg"])
                                     else round(st["cast"]["hue_mean_deg"], 1)),
                        "dominant_hue_bin": st["cast"]["dominant_hue_bin"],
                        "mean_rgb": [round(v, 4) for v in st["mean_rgb"]],
                        "skin_hue": (None if st["skin"]["hue_mean_deg"] is None
                                     else round(st["skin"]["hue_mean_deg"], 1)),
                        "skin_sat": (None if st["skin"]["sat_mean"] is None
                                     else round(st["skin"]["sat_mean"], 3))}
    if sol["mode"] == "curves":
        row["sat_factor"] = sol["sat"]
        row["sat_wanted"] = sol.get("sat_wanted")
        row["sat_target"] = sol.get("sat_target")
        row["gains"] = [round(v, 4) for v in sol["gains"]]
        row["guards"] = sol["guards"]
        row["points"] = [[round(x, 4), round(y, 4)] for x, y in sol["points"]]
        row["tried"] = sol["tried"]
    return row


# ------------------------------------------------------------ frame sampling --

def sample_frames(shot: Mapping[str, Any]) -> List[Tuple[str, int]]:
    """in / mid / out, pulled one frame inside the cut so no sample straddles a transition."""
    a, b = int(shot["in"]), int(shot["out"])
    if b - a >= 2:
        return [("in", a + 1), ("mid", (a + b) // 2), ("out", b - 1)]
    return [("in", a), ("mid", (a + b) // 2), ("out", b)]


def extract_reference(reference: Path, frame: int, out: Path, *,
                      size: Optional[Tuple[int, int]] = STAT_SIZE) -> None:
    from onetoone.ffx import run
    vf = f"scale={size[0]}:{size[1]}" if size else "null"
    run(["-y", "-loglevel", "error", "-ss", f"{frame / FPS:.4f}", "-i", str(reference),
         "-vf", vf, "-frames:v", "1", str(out)])


def extract_candidate(master: Path, t: float, out: Path, *, cover: Tuple[int, int] = (1916, 1078),
                      size: Optional[Tuple[int, int]] = STAT_SIZE, extra: str = "") -> None:
    """LUT'd (and optionally `extra`-graded) candidate frame at the delivery geometry."""
    from onetoone.ffx import run
    vf = f"lut3d=file={LUT}"
    if extra:
        vf += "," + extra
    vf += "," + COVER.format(w=cover[0], h=cover[1])
    if size:
        vf += f",scale={size[0]}:{size[1]}"
    run(["-y", "-loglevel", "error", "-ss", f"{t:.4f}", "-i", str(master),
         "-vf", vf, "-frames:v", "1", str(out)])


def measure(brain: Path, cast_json: Path, reference: Path, workdir: Path,
            *, cover: Tuple[int, int] = (1916, 1078)) -> Dict[str, Any]:
    """Measure the reference and OUR LUT'd candidate at in/mid/out of every shot."""
    workdir.mkdir(parents=True, exist_ok=True)
    cutgrid = json.loads((brain / "cutgrid.json").read_text())
    cast = {s["slot"]: s for s in json.loads(cast_json.read_text())["slots"]}
    report: Dict[str, Any] = {
        "measured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "method": (f"3 frames per shot (in+1 / mid / out-1). Reference decoded at frame/FPS; "
                   f"candidate = its cast master at in_s + shot-relative offset, through the "
                   f"official LC-709 LUT and the {cover[0]}x{cover[1]} cover crop. Both then "
                   f"scaled to {STAT_SIZE[0]}x{STAT_SIZE[1]} for statistics. Hue and saturation "
                   f"are measured on pixels with HSV value >= {V_FLOOR} only (the noise floor of "
                   f"a near-black frame otherwise reports nonsense saturation). Per-shot value = "
                   f"median across the shot's frames."),
        "levels": list(LEVELS), "v_floor": V_FLOOR, "stat_size": list(STAT_SIZE),
        "shots": {}, "problems": [],
    }
    for shot in cutgrid["shots"]:
        slot = shot["slot"]
        entry = cast.get(slot)
        if not entry:
            report["problems"].append(f"{slot}: not in cast"); continue
        master = resolve_master(str(entry["stem"]))
        if master is None:
            report["problems"].append(f"{slot}: master {entry['stem']} not on disk"); continue
        ref_paths, cand_paths, samples = [], [], []
        for tag, f in sample_frames(shot):
            rp = workdir / f"{slot}_{tag}_ref.png"
            cp = workdir / f"{slot}_{tag}_cand.png"
            extract_reference(reference, f, rp)
            t = float(entry["in_s"]) + (f - int(shot["in"])) / FPS
            extract_candidate(master, t, cp, cover=cover)
            ref_paths.append(rp); cand_paths.append(cp)
            samples.append({"tag": tag, "ref_frame": f, "cand_t": round(t, 4)})
        # one full-resolution frame per shot, purely for the 8-bit code census
        mid_t = float(entry["in_s"]) + (sample_frames(shot)[1][1] - int(shot["in"])) / FPS
        nat_c = workdir / f"{slot}_mid_cand_native.png"
        nat_r = workdir / f"{slot}_mid_ref_native.png"
        extract_candidate(master, mid_t, nat_c, cover=cover, size=None)
        extract_reference(reference, sample_frames(shot)[1][1], nat_r, size=None)
        report["shots"][slot] = {
            "stem": entry["stem"], "in_s": entry["in_s"], "samples": samples,
            "ref": shot_stats(ref_paths, native_probe=nat_r),
            "cand": shot_stats(cand_paths, native_probe=nat_c)}
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="measure reference vs LUT'd candidate, per shot")
    ap.add_argument("brain"); ap.add_argument("cast"); ap.add_argument("reference")
    ap.add_argument("workdir"); ap.add_argument("out_json")
    a = ap.parse_args()
    rep = measure(Path(a.brain), Path(a.cast), Path(a.reference), Path(a.workdir))
    for slot, st in rep["shots"].items():
        st["decision"] = explain(slot, st["cand"], st["ref"])
    Path(a.out_json).write_text(json.dumps(rep, indent=1))
    for slot, st in rep["shots"].items():
        d = st["decision"]
        print(f"{slot} {d['mode']:8} s={d['strength']:.2f} sat={d.get('sat_factor')}  {d['filter'][:110]}")
    if rep["problems"]:
        print("problems:", rep["problems"])
