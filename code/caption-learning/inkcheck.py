"""Per-frame ink cleanliness for a caption state, measured on the DELIVERED composite.

The complaint this answers is "muffled, not clean". Muffled ink is ink that does not separate
from what is immediately behind it: pale letters over a pale background, a bloomed edge that
smears the stroke into its own surround, a plate composited at an alpha that lets the footage
through. None of that shows up in a span median, and none of it shows up in a plan-vs-plan
check - it is a per-frame, per-state pixel fact.

Measurement, per sampled frame:

* **interior**  - the mask eroded by 1px so antialiased shoulder pixels (a blend of ink and
  footage) cannot drag the reading toward the background. If erosion empties a thin stroke,
  the unmodified mask is used and the frame is flagged ``thin_stroke``.
* **ring**      - dilate the mask by ``inner_dilate`` (default 7px) and by ``outer_dilate``
  (default 17px); the ring is the difference. That is the footage the eye compares the ink
  against - close enough to be the same lighting, far enough to exclude the glyph's own halo.
* **separation** - |median(interior luma) - median(ring luma)|, in 8-bit luma levels.
* **Michelson**  - (Lmax - Lmin) / (Lmax + Lmin) over those two medians.

Verdict (W3 - BOTH conditions, on the worst sampled frame, because the viewer sees the worst
frame):

1. **absolute** - separation >= ``min_separation`` (default 25 luma). Below it: FAIL.
2. **reference-relative** - Michelson >= ``min_michelson_ratio`` (default 0.80) x the
   reference's own ring Michelson for the same state. Below it: FAIL. This is the condition
   that catches ink which is legible in isolation but *muffled next to the reference* - it
   clears the absolute floor while carrying a fraction of the contrast of the approved reference.

Condition 2 needs a reference. ``reference_michelson`` supplies it; if it is absent or itself
unmeasurable (<= 0), the state is UNMEASURABLE (``reference_relative`` =
``UNMEASURABLE_REFERENCE``) - never PASS on the absolute floor alone. A caller measuring the
REFERENCE itself has nothing to be relative to and must say so with ``reference_required=False``,
which records ``reference_relative`` = ``NOT_APPLICABLE`` so an absolute-only verdict can never
be mistaken for a full W3. A frame with no interior pixels or no ring pixels is UNMEASURABLE,
never PASS.

**Per-frame registration.** One mask is measured against several frames, and a caption drifts:
an animated state that moves and scales ~1%/frame means a mask recovered at the end of the span
can sit on bare background at its start and read a separation of ~1 luma - a FAIL that was about mask
placement, not about ink. Before measuring, each frame therefore re-registers the mask over a
bounded integer offset grid (``registration_px``, default +-8 at step 2) and keeps the offset
with the largest interior/ring separation; the offset used is reported per frame. This cannot
manufacture contrast: if the ink really is muffled, every offset reads low and offset (0,0)
wins on a tie. Set ``registration_px=0`` to measure strictly in place.

A best offset sitting ON the edge of that window means the optimum is somewhere outside it -
the caption has walked away from this mask - so the frame is reported ``unregistered`` and
excluded from ``sample_size`` rather than being scored as a FAIL. If no sampled frame
registers, the whole state is UNMEASURABLE. Honest limit: a caption that animates FURTHER than
``registration_px`` between frames (e.g. a word scaling ~1%/frame) can land
a spurious in-window optimum on bare footage and read as low separation. The module does not
guess: pass ``mask_frames`` (the frames the mask was actually recovered from), or a per-frame
mask, when the caption animates. The per-frame registration offset is always reported, so a
drifting state is visible as a run of large offsets.

``ink.py:519`` in the shipped engine no-ops and reports PASS when the reference's own Michelson
is at or below the floor. This module never does that: an unmeasurable reference produces
``michelson_ratio_vs_reference`` = None, the absolute floor still runs, and a state that clears
it is reported UNMEASURABLE rather than green.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Sequence

import cv2
import numpy as np

__all__ = ["ink_state", "MIN_SEPARATION_LUMA", "MIN_MICHELSON_RATIO", "InkCheckError"]

# DOCTRINE: 25 luma levels of separation between glyph interior and its own surround.
MIN_SEPARATION_LUMA = 25.0
# DOCTRINE (W3): and at least 80% of the reference's own ring Michelson.
MIN_MICHELSON_RATIO = 0.80
MIN_INTERIOR_PIXELS = 32
MIN_RING_PIXELS = 64


class InkCheckError(ValueError):
    """Programmer error (bad shapes, unreadable file), never a bad caption."""


def _as_binary(mask, name: str) -> np.ndarray:
    if isinstance(mask, (str, os.PathLike)):
        loaded = cv2.imread(str(mask), cv2.IMREAD_UNCHANGED)
        if loaded is None:
            raise InkCheckError(f"{name}: cannot read mask image {mask!r}")
        mask = loaded
    arr = np.asarray(mask)
    if arr.ndim == 3:
        arr = arr[..., 3] if arr.shape[2] == 4 else arr.max(axis=2)
    if arr.ndim != 2:
        raise InkCheckError(f"{name}: expected a 2-D mask, got shape {arr.shape}")
    return (arr > 0).astype(np.uint8)


def _decode(video_path: str) -> List[np.ndarray]:
    """Every frame in ZERO-BASED DECODED ORDER."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise InkCheckError(f"cannot open video {video_path!r}")
    frames: List[np.ndarray] = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    if not frames:
        raise InkCheckError(f"decoded zero frames from {video_path!r}")
    return frames


def _disk(radius: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))


def _sample_frames(start: int, end_exclusive: int, n_frames: int) -> List[int]:
    """Entry / mid / exit at minimum, clamped to the file."""
    end = min(end_exclusive, n_frames)
    if end <= start:
        return []
    entry = start
    exit_ = end - 1
    mid = (start + end - 1) // 2
    return sorted({entry, mid, exit_})


def ink_state(
    video_path: str,
    state: Dict,
    mask,
    *,
    frames: Optional[Sequence[int]] = None,
    mask_frames: Optional[Sequence[int]] = None,
    inner_dilate: int = 7,
    outer_dilate: int = 17,
    erode_px: int = 1,
    registration_px: int = 8,
    registration_step: int = 2,
    min_separation: float = MIN_SEPARATION_LUMA,
    min_michelson_ratio: float = MIN_MICHELSON_RATIO,
    reference_michelson: Optional[float] = None,
    reference_separation: Optional[float] = None,
    reference_required: bool = True,
    out_dir: Optional[str] = None,
    label: Optional[str] = None,
    frames_all: Optional[Sequence[np.ndarray]] = None,
) -> Dict:
    """Per-frame ink cleanliness for one caption state on one composited file.

    ``mask`` is this state's ink mask in FULL-FRAME coordinates (e.g. from
    ``anatomy.recover_state_mask``). ``frames`` defaults to entry / mid / exit of the state's
    own span, in zero-based decoded order.
    """
    binary = _as_binary(mask, "mask")
    # ``frames_all``: same frames, supplied by a caller that already has them (see
    # anatomy.recover_state_mask).  Never a different decode order.
    if frames_all is None:
        frames_all = _decode(video_path)
    n_frames = len(frames_all)
    h, w = frames_all[0].shape[:2]
    if binary.shape != (h, w):
        raise InkCheckError(f"mask shape {binary.shape} does not match video frames {(h, w)}")

    start = int(state.get("start_frame", 0))
    end = int(state.get("end_frame_exclusive", state.get("end_frame", n_frames)))
    sid = state.get("id", "state")
    label = label or f"{os.path.basename(str(video_path))}-{sid}"

    if frames:
        sampled = list(frames)
    elif mask_frames:
        # The mask belongs to specific frames; measuring it anywhere else is the caller's
        # explicit choice, not a default.
        sampled = sorted(set(int(f) for f in mask_frames))
    else:
        sampled = _sample_frames(start, end, n_frames)
    sampled = [f for f in sampled if 0 <= f < n_frames]

    out: Dict = {
        "gate": "ink_cleanliness",
        "label": label,
        "video": str(video_path),
        "state_id": sid,
        "state_span_zero_based": [start, end],
        "thresholds": {
            "min_separation_luma": min_separation,
            "min_michelson_ratio_vs_reference": min_michelson_ratio,
            "reference_required": reference_required,
            "inner_dilate_px": inner_dilate,
            "outer_dilate_px": outer_dilate,
            "erode_px": erode_px,
            "registration_px": registration_px,
            "registration_step": registration_step,
        },
        "mask_ink_px": int(binary.sum()),
        "frames": [],
        "sample_size": 0,
        "evidence": {},
    }

    if not sampled:
        out.update(verdict="UNMEASURABLE", reason="no sampled frames inside the state's span",
                   worst_frame=None, worst_separation=None, worst_michelson=None)
        return out
    if out["mask_ink_px"] == 0:
        out.update(verdict="UNMEASURABLE", reason="mask carries zero ink pixels",
                   worst_frame=None, worst_separation=None, worst_michelson=None)
        return out

    interior = cv2.erode(binary, _disk(erode_px)) if erode_px > 0 else binary.copy()
    thin_stroke = False
    if interior.sum() < MIN_INTERIOR_PIXELS:
        interior = binary.copy()
        thin_stroke = True
    inner = cv2.dilate(binary, _disk(inner_dilate))
    outer = cv2.dilate(binary, _disk(outer_dilate))
    ring = ((outer > 0) & (inner == 0)).astype(np.uint8)

    out["interior_px"] = int(interior.sum())
    out["ring_px"] = int(ring.sum())
    out["thin_stroke_fallback"] = thin_stroke

    if out["interior_px"] < MIN_INTERIOR_PIXELS or out["ring_px"] < MIN_RING_PIXELS:
        out.update(
            verdict="UNMEASURABLE",
            reason=(
                f"interior={out['interior_px']}px (floor {MIN_INTERIOR_PIXELS}), "
                f"ring={out['ring_px']}px (floor {MIN_RING_PIXELS}); nothing to compare"
            ),
            worst_frame=None,
            worst_separation=None,
            worst_michelson=None,
        )
        return out

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        band = np.zeros((h, w, 3), np.uint8)
        band[..., 2] = interior * 255
        band[..., 1] = ring * 255
        p = os.path.join(out_dir, f"{label}-ink-bands.png")
        cv2.imwrite(p, band)
        out["evidence"]["bands_png"] = p

    offsets = [(0, 0)]
    if registration_px > 0:
        step = max(1, registration_step)
        offsets += [
            (dy, dx)
            for dy in range(-registration_px, registration_px + 1, step)
            for dx in range(-registration_px, registration_px + 1, step)
            if (dy, dx) != (0, 0)
        ]

    # Work inside a window around the mask so the offset scan stays cheap on 1916x1078 frames.
    ys, xs = np.nonzero(outer)
    margin = registration_px + 2
    wy0, wy1 = max(0, int(ys.min()) - margin), min(h, int(ys.max()) + 1 + margin)
    wx0, wx1 = max(0, int(xs.min()) - margin), min(w, int(xs.max()) + 1 + margin)
    interior_w = interior[wy0:wy1, wx0:wx1]
    ring_w = ring[wy0:wy1, wx0:wx1]
    out["work_window_xyxy"] = [wx0, wy0, wx1, wy1]

    rows: List[Dict] = []
    for f in sampled:
        gray = cv2.cvtColor(frames_all[f], cv2.COLOR_BGR2GRAY).astype(np.float64)[wy0:wy1, wx0:wx1]
        best = None
        for dy, dx in offsets:
            i_sel = np.roll(np.roll(interior_w, dy, 0), dx, 1) > 0
            r_sel = np.roll(np.roll(ring_w, dy, 0), dx, 1) > 0
            if not i_sel.any() or not r_sel.any():
                continue
            i_med = float(np.median(gray[i_sel]))
            r_med = float(np.median(gray[r_sel]))
            sep = abs(i_med - r_med)
            if best is None or sep > best[0]:
                best = (sep, dy, dx, i_sel, r_sel, i_med, r_med)
        sep, dy, dx, i_sel, r_sel, i_med, r_med = best
        # A best offset sitting ON the edge of the search window means the true optimum is
        # outside it: the mask is not registered to this frame (the caption drifted or scaled
        # away from it). Such a frame cannot distinguish "muffled ink" from "wrong place", so
        # it is reported UNMEASURABLE instead of being scored as a FAIL.
        if registration_px <= 0:
            status = "measured"
        elif abs(dy) >= registration_px or abs(dx) >= registration_px:
            status = "unregistered"
        else:
            status = "measured"
        i_vals, r_vals = gray[i_sel], gray[r_sel]
        lmax, lmin = max(i_med, r_med), min(i_med, r_med)
        michelson = (lmax - lmin) / (lmax + lmin) if (lmax + lmin) > 0 else 0.0
        rows.append(
            {
                "frame": int(f),
                "status": status,
                "registration_dy_dx": [int(dy), int(dx)],
                "interior_median": round(i_med, 2),
                "interior_p05": round(float(np.percentile(i_vals, 5)), 2),
                "interior_p95": round(float(np.percentile(i_vals, 95)), 2),
                "ring_median": round(r_med, 2),
                "separation": round(sep, 2),
                "michelson": round(michelson, 4),
                "interior_px": int(i_vals.size),
                "ring_px": int(r_vals.size),
                "polarity": "ink_brighter" if i_med >= r_med else "ink_darker",
            }
        )

    out["frames"] = rows
    measured = [r for r in rows if r["status"] == "measured"]
    unregistered = [r["frame"] for r in rows if r["status"] == "unregistered"]
    out["frames_sampled"] = len(rows)
    out["sample_size"] = len(measured)
    out["frames_unregistered"] = unregistered

    if not measured:
        out.update(
            verdict="UNMEASURABLE",
            reason=(
                f"the mask registered to none of the {len(rows)} sampled frames "
                f"(unregistered: {unregistered}); the caption has drifted or scaled away from "
                "this mask, so muffled ink cannot be told apart from a misplaced mask"
            ),
            worst_frame=None,
            worst_separation=None,
            worst_michelson=None,
        )
        return out

    worst = min(measured, key=lambda r: r["separation"])
    out["worst_frame"] = worst["frame"]
    out["worst_separation"] = worst["separation"]
    out["worst_michelson"] = worst["michelson"]
    out["median_separation"] = round(float(np.median([r["separation"] for r in measured])), 2)

    # reference-relative reporting (never a silent no-op: an absent reference is reported)
    if reference_michelson is not None and reference_michelson > 0:
        out["michelson_ratio_vs_reference"] = round(worst["michelson"] / reference_michelson, 4)
        out["reference_michelson"] = reference_michelson
    else:
        out["michelson_ratio_vs_reference"] = None
        out["reference_michelson"] = reference_michelson
    if reference_separation is not None and reference_separation > 0:
        out["separation_ratio_vs_reference"] = round(worst["separation"] / reference_separation, 4)
        out["reference_separation"] = reference_separation
    else:
        out["separation_ratio_vs_reference"] = None
        out["reference_separation"] = reference_separation

    # ---- verdict: BOTH specified conditions, absolute first -------------------------------
    ratio = out["michelson_ratio_vs_reference"]
    if ratio is not None:
        out["reference_relative"] = "ENFORCED"
    elif reference_required:
        out["reference_relative"] = "UNMEASURABLE_REFERENCE"
    else:
        out["reference_relative"] = "NOT_APPLICABLE"

    if worst["separation"] < min_separation:
        # An absolutely unreadable state is a FAIL whatever the reference did; naming it
        # UNMEASURABLE because the reference is missing would lose a real defect.
        out["verdict"] = "FAIL"
        out["reason"] = (
            f"worst sampled frame f{worst['frame']} separates interior from ring by only "
            f"{worst['separation']} luma levels, below the {min_separation} floor"
        )
    elif ratio is not None and ratio < min_michelson_ratio:
        out["verdict"] = "FAIL"
        out["reason"] = (
            f"worst sampled frame f{worst['frame']} clears the {min_separation}-luma floor "
            f"({worst['separation']}) but carries Michelson {worst['michelson']} against the "
            f"reference's own {reference_michelson} for this state - ratio {ratio}, below the "
            f"{min_michelson_ratio} reference-relative floor: muffled next to the reference, "
            "not clean"
        )
    elif ratio is None and reference_required:
        # ink.py:519's hole, refused: half the specification is not a PASS.
        detail = (
            f"the reference's own ring Michelson for this state is {reference_michelson}"
            if reference_michelson is not None
            else "no reference ring Michelson was supplied for this state"
        )
        out["verdict"] = "UNMEASURABLE"
        out["reason"] = (
            f"the absolute {min_separation}-luma floor is cleared "
            f"({worst['separation']} on f{worst['frame']}), but the reference-relative "
            f"condition (Michelson >= {min_michelson_ratio}x the reference's own) could not "
            f"be evaluated: {detail}. Half the W3 specification is not a PASS - pass "
            "reference_required=False if this measurement IS the reference"
        )
    else:
        out["verdict"] = "PASS"
    return out


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps({"module": "inkcheck", "min_separation": MIN_SEPARATION_LUMA}, indent=2))
