"""DOCTRINE 16.2 triple gate: glyph-anatomy comparison of two caption ink masks.

Why this module exists
----------------------
Dice alone passed an amputated ``they`` (Dice 0.9030, one-way residual 85 px) and it
passed a delivered word whose fused strokes a reviewer read as a different word. Dice is an
*area* statistic: it cannot see that a stroke was severed, that two letters fused into one
blob, or that a counter closed up. Those are topology and boundary facts.

So the gate is three independent measurements, ANDed:

1. **Dice** - area agreement.
2. **One-way source -> candidate boundary residual, p95, in pixels** - for every boundary
   pixel of the *source* (the reference letterform), the distance to the nearest boundary
   pixel of the *candidate*. A candidate that is missing a stroke leaves the source's
   boundary along that stroke with nothing nearby: the residual explodes while Dice barely
   moves. One-way on purpose: the candidate growing extra ink is a different defect, caught
   by Dice and by the component count.
3. **Topology equality** - connected-component count AND hole count must match exactly.
   Fused ``o-w`` drops a component. A closed counter that fills in drops a hole.

Normalisation
-------------
Tight-crop each mask, apply **one isotropic scale** so the candidate's crop height matches
the source's, then translate by centroid. Never an independent x/y resize: resizing each axis
to a common box is the historical false-pass mechanism, because it silently corrects the exact
aspect/tracking error the gate is supposed to catch.

After the centroid translation the gate runs a **bounded integer translation refinement**
(default +-6 px, ``refine_px``). Translation is a nuisance parameter here - recovered masks come
from different frames and different renders whose sub-pixel placement differs, and a centroid
is pulled off by any footage the recovery could not shed. Every defect this gate exists to
catch is translation-invariant: a severed stroke, a fused ``o-w``, a filled counter and a wrong
aspect all survive any shift. The refinement is therefore incapable of repairing a defect, and
the measured asymmetry proves it: on a real ground-truth pair it moved the reference's
self-comparison from Dice 0.522 to 0.916 (nuisance removed) while moving the broken delivered
word from 0.242 to only 0.257. Both the pre-refinement and post-refinement Dice are
reported on every call, so the size of the correction is always visible.

Topology floors
---------------
Component and hole counts are taken over *significant* features only: components below
``max(min_component_area, component_area_floor_fraction x ink)`` and holes below
``max(min_hole_area, hole_area_floor_fraction x ink)`` are recovery noise, not anatomy. Without
a floor the reference disagrees with ITSELF - measured on the ground truth, two adjacent
reference frames recovered 2 vs 17 holes, of which 15 were sub-32px threshold specks. The
floors and the raw (unfloored) counts are both reported.

Honesty vocabulary
------------------
Every function returns ``verdict`` in {"PASS", "FAIL", "UNMEASURABLE"} plus the numbers it
measured and the sample size it measured them over. A comparison that had no pixels to compare
returns UNMEASURABLE - never PASS. (This factory once shipped an alpha probe that compared
zero pixels on 40 of 42 states and reported PASS. That class is dead.)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from scipy import ndimage

__all__ = [
    "compare_masks",
    "recover_state_mask",
    "DICE_MIN",
    "RESIDUAL_P95_MAX",
    "AnatomyError",
]

# --- thresholds (DOCTRINE 16.2) ------------------------------------------------------------
DICE_MIN = 0.90
RESIDUAL_P95_MAX = 3.0

# Minimum ink for a comparison to mean anything. Below this a "mask" is noise, and any
# statistic computed over it is theatre.
MIN_INK_PIXELS = 64
MIN_BOUNDARY_PIXELS = 32

# Bounded translation refinement after centroid alignment (see module docstring).
REFINE_PX = 6

# Significance floors for the topology axis. Calibrated on a real reference/delivered word pair: at floor 0 the reference disagreed with itself (2 vs 17 holes, 15 of them <32px).
COMPONENT_AREA_FLOOR_FRACTION = 0.01
MIN_COMPONENT_AREA = 64
HOLE_AREA_FLOOR_FRACTION = 0.002
MIN_HOLE_AREA = 16

_EIGHT = np.ones((3, 3), dtype=bool)
_FOUR = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)


class AnatomyError(ValueError):
    """Raised for programmer errors (bad shapes, missing files), never for a bad caption."""


# --- small helpers -------------------------------------------------------------------------


def _as_binary(mask, name: str) -> np.ndarray:
    if isinstance(mask, (str, os.PathLike)):
        loaded = cv2.imread(str(mask), cv2.IMREAD_UNCHANGED)
        if loaded is None:
            raise AnatomyError(f"{name}: cannot read mask image {mask!r}")
        mask = loaded
    arr = np.asarray(mask)
    if arr.ndim == 3:
        # An RGBA plate carries its ink in alpha; anything else, collapse to max channel.
        arr = arr[..., 3] if arr.shape[2] == 4 else arr.max(axis=2)
    if arr.ndim != 2:
        raise AnatomyError(f"{name}: expected a 2-D mask, got shape {arr.shape}")
    return arr > 0


def _tight_crop(binary: np.ndarray) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    ys, xs = np.where(binary)
    if ys.size == 0:
        return binary[:0, :0], (0, 0, 0, 0)
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    return binary[y0:y1, x0:x1], (x0, y0, x1, y1)


def _inner_boundary(binary: np.ndarray) -> np.ndarray:
    """1-px inner boundary: ink pixels that touch a non-ink 8-neighbour."""
    eroded = ndimage.binary_erosion(binary, structure=_EIGHT, border_value=0)
    return binary & ~eroded


def _components(binary: np.ndarray, area_floor: float = 0.0) -> int:
    """8-connected component count of the ink, ignoring components below ``area_floor``."""
    labels, n = ndimage.label(binary, structure=_EIGHT)
    if n == 0:
        return 0
    if area_floor <= 1:
        return int(n)
    sizes = ndimage.sum(binary, labels, index=range(1, n + 1))
    return int((np.asarray(sizes) >= area_floor).sum())


def _holes(binary: np.ndarray, area_floor: float = 0.0) -> int:
    """Enclosed background regions (counters), ignoring holes below ``area_floor``.

    Object 8-connected -> background 4-connected, which is the pairing that keeps the Euler
    number consistent. The mask is padded so the outer background is a single border region.
    """
    padded = np.pad(binary, 1, mode="constant", constant_values=False)
    labels, n = ndimage.label(~padded, structure=_FOUR)
    if n == 0:
        return 0
    border = set(labels[0, :]) | set(labels[-1, :]) | set(labels[:, 0]) | set(labels[:, -1])
    border.discard(0)
    if area_floor <= 1:
        return int(n - len(border))
    sizes = np.asarray(ndimage.sum(~padded, labels, index=range(1, n + 1)))
    return int(sum(1 for i in range(1, n + 1) if i not in border and sizes[i - 1] >= area_floor))


def _isotropic_scale(binary: np.ndarray, factor: float) -> np.ndarray:
    """ONE scale factor on BOTH axes. Never an independent x/y resize."""
    if abs(factor - 1.0) < 1e-9:
        return binary.copy()
    h, w = binary.shape
    nh = max(1, int(round(h * factor)))
    nw = max(1, int(round(w * factor)))
    resized = cv2.resize(binary.astype(np.uint8) * 255, (nw, nh), interpolation=cv2.INTER_AREA)
    return resized >= 128


def _place_on_canvas(binary: np.ndarray, canvas_hw: Tuple[int, int], top_left) -> np.ndarray:
    out = np.zeros(canvas_hw, dtype=bool)
    ty, tx = int(top_left[0]), int(top_left[1])
    h, w = binary.shape
    y0, x0 = max(0, ty), max(0, tx)
    y1, x1 = min(canvas_hw[0], ty + h), min(canvas_hw[1], tx + w)
    if y1 <= y0 or x1 <= x0:
        return out
    out[y0:y1, x0:x1] = binary[y0 - ty : y1 - ty, x0 - tx : x1 - tx]
    return out


def _centroid(binary: np.ndarray) -> Tuple[float, float]:
    ys, xs = np.nonzero(binary)
    return float(ys.mean()), float(xs.mean())


def _write_evidence(out_dir: Optional[str], label: str, src: np.ndarray, cand: np.ndarray) -> Dict[str, str]:
    if not out_dir:
        return {}
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    src_u8 = src.astype(np.uint8) * 255
    cand_u8 = cand.astype(np.uint8) * 255
    overlay = np.zeros(src.shape + (3,), np.uint8)
    overlay[..., 2] = src_u8  # source in red
    overlay[..., 1] = cand_u8  # candidate in green -> agreement is yellow
    for name, img in (("source", src_u8), ("candidate", cand_u8), ("overlay", overlay)):
        p = os.path.join(out_dir, f"{label}-{name}.png")
        cv2.imwrite(p, img)
        paths[f"{name}_png"] = p
    return paths


# --- the triple gate -----------------------------------------------------------------------


def compare_masks(
    source_mask,
    candidate_mask,
    *,
    dice_min: float = DICE_MIN,
    residual_p95_max: float = RESIDUAL_P95_MAX,
    out_dir: Optional[str] = None,
    label: str = "anatomy",
    min_ink_pixels: int = MIN_INK_PIXELS,
    refine_px: int = REFINE_PX,
    component_area_floor_fraction: float = COMPONENT_AREA_FLOOR_FRACTION,
    min_component_area: int = MIN_COMPONENT_AREA,
    hole_area_floor_fraction: float = HOLE_AREA_FLOOR_FRACTION,
    min_hole_area: int = MIN_HOLE_AREA,
) -> Dict:
    """DOCTRINE 16.2 triple gate over two caption ink masks.

    ``source_mask`` is the authority (the reference letterform); ``candidate_mask`` is what we
    shipped. Accepts 2-D arrays, RGBA plates (alpha is the ink) or PNG paths.

    Returns a dict with ``verdict`` PASS / FAIL / UNMEASURABLE, all three axes, the sample size
    each was measured over, and evidence paths.
    """
    src_raw = _as_binary(source_mask, "source_mask")
    cand_raw = _as_binary(candidate_mask, "candidate_mask")

    result: Dict = {
        "gate": "anatomy_triple_16.2",
        "label": label,
        "thresholds": {
            "dice_min": dice_min,
            "residual_p95_max_px": residual_p95_max,
            "components_equal": True,
            "holes_equal": True,
            "refine_px": refine_px,
            "component_area_floor_fraction": component_area_floor_fraction,
            "min_component_area": min_component_area,
            "hole_area_floor_fraction": hole_area_floor_fraction,
            "min_hole_area": min_hole_area,
        },
        "source_ink_px": int(src_raw.sum()),
        "candidate_ink_px": int(cand_raw.sum()),
        "sample_size": 0,
        "evidence": {},
    }

    # --- honesty gate: nothing to measure --------------------------------------------------
    if result["source_ink_px"] < min_ink_pixels or result["candidate_ink_px"] < min_ink_pixels:
        result.update(
            verdict="UNMEASURABLE",
            reason=(
                f"ink below the {min_ink_pixels}px floor "
                f"(source={result['source_ink_px']}, candidate={result['candidate_ink_px']}); "
                "there is nothing to compare"
            ),
            dice=None,
            residual_p95_px=None,
            components_source=None,
            components_candidate=None,
            holes_source=None,
            holes_candidate=None,
        )
        return result

    # --- normalisation: tight crop -> ONE isotropic scale -> centroid translation -----------
    src_crop, src_box = _tight_crop(src_raw)
    cand_crop, cand_box = _tight_crop(cand_raw)
    sh, sw = src_crop.shape
    ch, cw = cand_crop.shape
    if sh == 0 or ch == 0:
        result.update(verdict="UNMEASURABLE", reason="an empty tight crop", dice=None,
                      residual_p95_px=None)
        return result

    scale = float(sh) / float(ch)
    cand_scaled = _isotropic_scale(cand_crop, scale)
    if not cand_scaled.any():
        result.update(
            verdict="UNMEASURABLE",
            reason=f"candidate vanished under the isotropic scale x{scale:.3f}",
            dice=None,
            residual_p95_px=None,
        )
        return result

    pad = 16
    canvas_h = max(src_crop.shape[0], cand_scaled.shape[0]) + 2 * pad
    canvas_w = max(src_crop.shape[1], cand_scaled.shape[1]) + 2 * pad
    src_n = _place_on_canvas(src_crop, (canvas_h, canvas_w), (pad, pad))
    cand_n = _place_on_canvas(cand_scaled, (canvas_h, canvas_w), (pad, pad))

    scy, scx = _centroid(src_n)
    ccy, ccx = _centroid(cand_n)
    dy, dx = int(round(scy - ccy)), int(round(scx - ccx))
    cand_n = _place_on_canvas(cand_scaled, (canvas_h, canvas_w), (pad + dy, pad + dx))
    if not cand_n.any():
        result.update(verdict="UNMEASURABLE", reason="candidate shifted off the canvas",
                      dice=None, residual_p95_px=None)
        return result

    def _dice(u: np.ndarray, v: np.ndarray) -> float:
        total = int(u.sum()) + int(v.sum())
        return (2.0 * int((u & v).sum())) / float(total) if total else 0.0

    dice_centroid = _dice(src_n, cand_n)

    # bounded integer translation refinement - a nuisance parameter, not an anatomy property
    best_dice, best_off = dice_centroid, (0, 0)
    if refine_px > 0:
        for ry in range(-refine_px, refine_px + 1):
            for rx in range(-refine_px, refine_px + 1):
                if ry == 0 and rx == 0:
                    continue
                trial = _place_on_canvas(
                    cand_scaled, (canvas_h, canvas_w), (pad + dy + ry, pad + dx + rx)
                )
                d = _dice(src_n, trial)
                if d > best_dice:
                    best_dice, best_off = d, (ry, rx)
    if best_off != (0, 0):
        cand_n = _place_on_canvas(
            cand_scaled, (canvas_h, canvas_w), (pad + dy + best_off[0], pad + dx + best_off[1])
        )

    result["normalisation"] = {
        "method": (
            "tight_crop + single isotropic scale to source height + centroid translate "
            f"+ bounded +-{refine_px}px translation refinement"
        ),
        "source_crop_hw": [int(sh), int(sw)],
        "candidate_crop_hw": [int(ch), int(cw)],
        "isotropic_scale": round(scale, 6),
        "translate_dy_dx": [dy, dx],
        "refine_dy_dx": [int(best_off[0]), int(best_off[1])],
        "dice_centroid_only": round(dice_centroid, 6),
        "refinement_gain_dice": round(best_dice - dice_centroid, 6),
        "canvas_hw": [int(canvas_h), int(canvas_w)],
    }

    # --- axis 1: Dice ----------------------------------------------------------------------
    inter = int((src_n & cand_n).sum())
    a, b = int(src_n.sum()), int(cand_n.sum())
    dice = (2.0 * inter) / float(a + b)

    # --- axis 2: one-way source -> candidate boundary residual -----------------------------
    src_b = _inner_boundary(src_n)
    cand_b = _inner_boundary(cand_n)
    n_src_b, n_cand_b = int(src_b.sum()), int(cand_b.sum())
    result["sample_size"] = n_src_b
    result["boundary_px"] = {"source": n_src_b, "candidate": n_cand_b}

    if n_src_b < MIN_BOUNDARY_PIXELS or n_cand_b < MIN_BOUNDARY_PIXELS:
        result.update(
            verdict="UNMEASURABLE",
            reason=(
                f"boundary sample too small (source={n_src_b}, candidate={n_cand_b}, "
                f"floor={MIN_BOUNDARY_PIXELS})"
            ),
            dice=round(dice, 6),
            residual_p95_px=None,
        )
        result["evidence"] = _write_evidence(out_dir, label, src_n, cand_n)
        return result

    # distance_transform_edt measures distance to the nearest ZERO, so invert the candidate
    # boundary to get "distance from anywhere to the nearest candidate boundary pixel".
    dist_to_cand_boundary = ndimage.distance_transform_edt(~cand_b)
    residuals = dist_to_cand_boundary[src_b]
    residual_p95 = float(np.percentile(residuals, 95))
    residual_max = float(residuals.max())
    residual_mean = float(residuals.mean())

    # --- axis 3: topology ------------------------------------------------------------------
    comp_floor_s = max(min_component_area, component_area_floor_fraction * a)
    comp_floor_c = max(min_component_area, component_area_floor_fraction * b)
    hole_floor_s = max(min_hole_area, hole_area_floor_fraction * a)
    hole_floor_c = max(min_hole_area, hole_area_floor_fraction * b)
    comp_s, comp_c = _components(src_n, comp_floor_s), _components(cand_n, comp_floor_c)
    hole_s, hole_c = _holes(src_n, hole_floor_s), _holes(cand_n, hole_floor_c)
    result["topology_raw"] = {
        "components_source_unfloored": _components(src_n),
        "components_candidate_unfloored": _components(cand_n),
        "holes_source_unfloored": _holes(src_n),
        "holes_candidate_unfloored": _holes(cand_n),
        "component_area_floor_px": [round(comp_floor_s, 1), round(comp_floor_c, 1)],
        "hole_area_floor_px": [round(hole_floor_s, 1), round(hole_floor_c, 1)],
    }

    failed: List[str] = []
    if dice < dice_min:
        failed.append("dice")
    if residual_p95 > residual_p95_max:
        failed.append("residual_p95")
    if comp_s != comp_c:
        failed.append("components")
    if hole_s != hole_c:
        failed.append("holes")

    result.update(
        verdict="PASS" if not failed else "FAIL",
        failed_axes=failed,
        dice=round(dice, 6),
        intersection_px=inter,
        residual_p95_px=round(residual_p95, 4),
        residual_max_px=round(residual_max, 4),
        residual_mean_px=round(residual_mean, 4),
        components_source=comp_s,
        components_candidate=comp_c,
        holes_source=hole_s,
        holes_candidate=hole_c,
        normalised_ink_px={"source": a, "candidate": b},
    )
    result["evidence"] = _write_evidence(out_dir, label, src_n, cand_n)
    return result


# --- reference/candidate mask recovery from a composited video -----------------------------


def _decode(video_path: str) -> List[np.ndarray]:
    """Every frame in ZERO-BASED DECODED ORDER (identical to ffmpeg -start_number 0)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise AnatomyError(f"cannot open video {video_path!r}")
    frames: List[np.ndarray] = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    if not frames:
        raise AnatomyError(f"decoded zero frames from {video_path!r}")
    return frames


def frame_diffs(frames: Sequence[np.ndarray]) -> List[float]:
    """Per-frame mean abs luma difference against the previous frame; diffs[0] == 0.0.

    Streams one frame at a time on purpose: materialising every float32 gray for a
    1036-frame 1440x1080 file costs 6.4 GB, which is why the sweep could not run the
    original list-comprehension form on smaller machines.  Values are identical.
    """
    out: List[float] = [0.0]
    prev: Optional[np.ndarray] = None
    for f in frames:
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            out.append(float(np.abs(gray - prev).mean()))
        prev = gray
    return out


def _shot_bounds(frames: Sequence[np.ndarray], index: int, cut_threshold: float = 25.0,
                 diffs: Optional[Sequence[float]] = None) -> Tuple[int, int]:
    """[start, end_exclusive) of the shot containing ``index``, by full-frame luma diff.

    ``diffs`` may be supplied by a caller that already computed them for this video
    (``frame_diffs``); this is only a cache, never a different measurement.
    """
    diffs = list(diffs) if diffs is not None else frame_diffs(frames)
    start = 0
    for i in range(index, 0, -1):
        if diffs[i] > cut_threshold:
            start = i
            break
    end = len(frames)
    for i in range(index + 1, len(frames)):
        if diffs[i] > cut_threshold:
            end = i
            break
    return start, end


def _state_box(state: Dict) -> List[int]:
    placement = state.get("placement", state)
    box = placement.get("core_bbox_xyxy") or placement.get("bbox_xyxy") or state.get("bbox")
    if box is None:
        raise AnatomyError("state has no core_bbox_xyxy / bbox_xyxy")
    return [int(v) for v in box]


def _state_span(state: Dict) -> Tuple[int, int]:
    start = state.get("start_frame")
    end = state.get("end_frame_exclusive", state.get("end_frame"))
    if start is None or end is None:
        raise AnatomyError("state has no start_frame / end_frame_exclusive")
    return int(start), int(end)


def recover_state_mask(
    video_path: str,
    state: Dict,
    out_dir: str,
    *,
    all_states: Optional[Sequence[Dict]] = None,
    frames: Optional[Sequence[int]] = None,
    blank_candidates: Optional[Sequence[int]] = None,
    pad: int = 60,
    max_background_mismatch: float = 10.0,
    min_area: int = 24,
    cut_threshold: float = 25.0,
    label: Optional[str] = None,
    frames_all: Optional[Sequence[np.ndarray]] = None,
    shot_diffs: Optional[Sequence[float]] = None,
) -> Dict:
    """Best-effort caption ink mask for one state, recovered from a composited video.

    Method (temporal difference, as DOCTRINE prescribes):

    * The state's own frames carry ink; text-free frames **in the same shot** do not. Take the
      per-pixel median of the state frames, difference it against a text-free neighbour, Otsu
      the abs-diff luma, keep the components that overlap the state's declared bbox.
    * The neighbour is **not** taken on faith. Every candidate blank in the shot is scored by
      the median abs luma difference over the padded ROI - a statistic the caption itself
      cannot dominate, because it is a minority of the ROI's pixels. A candidate whose score
      exceeds ``max_background_mismatch`` is footage that has moved on, and differencing
      against it measures the footage, not the ink. If nothing scores under the ceiling the
      state is **UNMEASURABLE from this file** - which is a real, reportable fact about the
      delivery, not a failure of the caller.
    * Components touching the padded ROI border are footage structures entering the box from
      outside (poles, wires, silhouettes); they are dropped. A caption lives inside its box.

    Frame indices are ZERO-BASED DECODED ORDER throughout.
    """
    # ``frames_all`` / ``shot_diffs`` let a caller that sweeps many states of the same file
    # decode it once (or lazily) instead of once per state.  Same frames, same indices,
    # zero-based decoded order either way.
    if frames_all is None:
        frames_all = _decode(video_path)
    n_frames = len(frames_all)
    box = _state_box(state)
    start, end = _state_span(state)
    sid = state.get("id", "state")
    label = label or f"{os.path.basename(str(video_path))}-{sid}"
    os.makedirs(out_dir, exist_ok=True)

    out: Dict = {
        "gate": "recover_state_mask",
        "label": label,
        "video": str(video_path),
        "state_id": sid,
        "state_span_zero_based": [start, end],
        "core_bbox_xyxy": box,
        "frame_count": n_frames,
        "sample_size": 0,
        "evidence": {},
    }

    if start >= n_frames:
        out.update(verdict="UNMEASURABLE", reason=f"state starts at frame {start}, file has {n_frames}")
        return out

    mid = min(n_frames - 1, max(start, (start + end) // 2))
    shot_start, shot_end = _shot_bounds(frames_all, mid, cut_threshold, diffs=shot_diffs)
    out["shot_zero_based"] = [shot_start, shot_end]

    # frames that carry this state's ink
    state_frames = list(frames) if frames else list(range(start, min(end, n_frames)))
    state_frames = [f for f in state_frames if 0 <= f < n_frames]
    if not state_frames:
        out.update(verdict="UNMEASURABLE", reason="no usable state frames")
        return out

    # text-free candidates: in the same shot, covered by no caption state
    if blank_candidates is None:
        occupied = set()
        for other in all_states or [state]:
            try:
                o_start, o_end = _state_span(other)
            except AnatomyError:
                continue
            occupied.update(range(o_start, o_end))
        blank_candidates = [i for i in range(shot_start, shot_end) if i not in occupied]
    blank_candidates = [i for i in blank_candidates if 0 <= i < n_frames]
    out["blank_candidates"] = blank_candidates

    if not blank_candidates:
        out.update(
            verdict="UNMEASURABLE",
            reason=(
                f"no text-free frame inside the state's own shot [{shot_start},{shot_end}); "
                "temporal difference has nothing to difference against"
            ),
        )
        return out

    h, w = frames_all[0].shape[:2]
    x0, y0, x1, y1 = box
    rx0, ry0 = max(0, x0 - pad), max(0, y0 - pad)
    rx1, ry1 = min(w, x1 + pad), min(h, y1 + pad)
    rh, rw = ry1 - ry0, rx1 - rx0
    if rh <= 0 or rw <= 0:
        out.update(verdict="UNMEASURABLE", reason=f"degenerate roi for bbox {box}")
        return out

    def roi_gray(i: int) -> np.ndarray:
        return cv2.cvtColor(frames_all[i][ry0:ry1, rx0:rx1], cv2.COLOR_BGR2GRAY).astype(np.float32)

    state_med = np.median(np.stack([roi_gray(i) for i in state_frames]), axis=0)
    scored = sorted(
        ((int(j), float(np.median(np.abs(state_med - roi_gray(j))))) for j in blank_candidates),
        key=lambda t: t[1],
    )
    out["blank_scores_best"] = [[j, round(s, 2)] for j, s in scored[:5]]
    out["max_background_mismatch"] = max_background_mismatch

    if scored[0][1] > max_background_mismatch:
        out.update(
            verdict="UNMEASURABLE",
            reason=(
                f"no compatible blank neighbourhood: best text-free frame in the shot is f{scored[0][0]} "
                f"at median background mismatch {scored[0][1]:.1f} luma, ceiling {max_background_mismatch}. "
                "The footage under this state has moved on; a temporal difference here would "
                "measure the footage, not the ink."
            ),
            state_frames_used=state_frames,
        )
        return out

    blank = scored[0][0]
    delta = np.clip(np.abs(state_med - roi_gray(blank)), 0, 255).astype(np.uint8)
    otsu, binary = cv2.threshold(delta, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    binary = cv2.morphologyEx(
        binary, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    )
    count, labels, stats, _ = cv2.connectedComponentsWithStats((binary > 0).astype(np.uint8), 8)
    in_box = np.zeros((rh, rw), bool)
    in_box[y0 - ry0 : y1 - ry0, x0 - rx0 : x1 - rx0] = True
    keep = np.zeros((rh, rw), np.uint8)
    dropped_border = 0
    for i in range(1, count):
        left, top = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
        cw_, ch_ = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
        if int(stats[i, cv2.CC_STAT_AREA]) < min_area:
            continue
        if left == 0 or top == 0 or left + cw_ >= rw or top + ch_ >= rh:
            dropped_border += 1
            continue
        comp = labels == i
        if (comp & in_box).any():
            keep[comp] = 255

    mask = np.zeros((h, w), np.uint8)
    mask[ry0:ry1, rx0:rx1] = keep
    ink = int((mask > 0).sum())

    mask_path = os.path.join(out_dir, f"{label}-mask.png")
    crop_path = os.path.join(out_dir, f"{label}-mask-crop.png")
    delta_path = os.path.join(out_dir, f"{label}-delta.png")
    cv2.imwrite(mask_path, mask)
    cv2.imwrite(crop_path, mask[ry0:ry1, rx0:rx1])
    cv2.imwrite(delta_path, delta)

    out.update(
        state_frames_used=state_frames,
        blank_frame_used=blank,
        background_mismatch_luma=round(scored[0][1], 2),
        otsu_threshold=float(otsu),
        roi_xyxy=[rx0, ry0, rx1, ry1],
        components_dropped_at_roi_border=dropped_border,
        ink_px=ink,
        sample_size=len(state_frames),
        components=_components(mask > 0) if ink else 0,
        evidence={"mask_png": mask_path, "mask_crop_png": crop_path, "delta_png": delta_path},
    )
    out["mask"] = mask
    if ink < MIN_INK_PIXELS:
        out.update(
            verdict="UNMEASURABLE",
            reason=f"recovered only {ink} ink px (floor {MIN_INK_PIXELS})",
        )
    else:
        out.update(verdict="PASS")
    return out


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items() if k != "mask"}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return f"<ndarray {obj.shape}>"
    return obj


if __name__ == "__main__":  # pragma: no cover - manual probe
    print(json.dumps(_json_safe({"module": "anatomy", "dice_min": DICE_MIN,
                                 "residual_p95_max": RESIDUAL_P95_MAX}), indent=2))
