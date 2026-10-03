"""Pixel measurement for the EXTRACT stage.

Placement and ink are measurements, never document values. This module holds the three
segmentation models the corpus proved, generalised out of the per-reel scripts where they
lived as hand-tuned ROIs and per-word magic numbers:

* :func:`segment_min_channel` - ``min(B,G,R) > t`` for white/near-white ink. The minimum
  channel rather than luma, so a saturated bright colour is not mistaken for white ink.
* :func:`segment_chromatic_difference` - channel-difference predicates for coloured ink.
* :func:`segment_temporal_difference` - median of the state's frames against the median of
  adjacent **blank** frames. Non-circular by construction: it uses only the reference's own
  text-free neighbours, so no font, palette or renderer constant enters the measurement.

See ``DOCTRINE.md`` sections 3 and 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

__all__ = [
    "CLEAN_MAX_INTERIOR_SPREAD",
    "CLEAN_SEPARATION",
    "CORE_ALPHA",
    "ExtractError",
    "InkMeasurement",
    "MASK_TIERS",
    "MAX_COMPONENTS_PER_GLYPH",
    "LOCAL_BACKGROUND_KERNEL",
    "MIN_GEOMETRIC_RIDGE_WIDTH",
    "MIN_LOCAL_CONTRAST",
    "MaskTierVerdict",
    "ResolvedTier",
    "SECONDARY_SEPARATION",
    "TEXT_ASPECT_PER_GLYPH",
    "TEXT_FILL_RATIO_RANGE",
    "TREATMENT_ALPHA",
    "TextConsistency",
    "text_consistency",
    "classify_mask_tier",
    "clean_components",
    "is_promotable_tier",
    "core_and_treatment_bbox",
    "detect_state_span",
    "dominant_text_line",
    "filter_text_like_components",
    "fit_compositing_operator",
    "measure_ink",
    "resolve_mask_tier",
    "restrict_to_box",
    "OperatorFit",
    "segment_chromatic_difference",
    "segment_difference_family_ink",
    "segment_min_channel",
    "segment_temporal_difference",
    "segment_static_distinct_ink",
    "segment_temporally_static_ink",
    "union_bbox",
]

# Below this stroke thickness a glyph has no geometric interior: every pixel touches the
# boundary, so a distance-transform ridge cannot separate core ink from antialiased shoulder.
MIN_GEOMETRIC_RIDGE_WIDTH = 3.0

# The core box is the 50%-alpha ink; the treatment box is the low-alpha glow/shadow
# extent (DOCTRINE.md rule 4.5).
CORE_ALPHA = 128
TREATMENT_ALPHA = 8

Box = Tuple[int, int, int, int]


class ExtractError(ValueError):
    pass


def _check_roi(frame: np.ndarray, roi_xyxy: Sequence[int]) -> Box:
    if len(roi_xyxy) != 4:
        raise ExtractError(f"roi must be (x0, y0, x1, y1), got {tuple(roi_xyxy)!r}")
    x0, y0, x1, y1 = (int(value) for value in roi_xyxy)
    height, width = frame.shape[:2]
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        raise ExtractError(
            f"roi ({x0}, {y0}, {x1}, {y1}) is out of bounds for a {width}x{height} frame"
        )
    if x1 <= x0 or y1 <= y0:
        raise ExtractError(f"roi ({x0}, {y0}, {x1}, {y1}) is empty")
    return x0, y0, x1, y1


def clean_components(mask: np.ndarray, min_area: int = 4) -> np.ndarray:
    """Drop connected components smaller than ``min_area`` pixels.

    Codec noise and compression flecks are single pixels; glyph strokes are not.
    """
    binary = (mask > 0).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    out = np.zeros(mask.shape, dtype=np.uint8)
    for index in range(1, count):
        if stats[index, cv2.CC_STAT_AREA] >= min_area:
            out[labels == index] = 255
    return out


def union_bbox(mask: np.ndarray) -> Optional[Box]:
    """Half-open bounding box over every set pixel, or ``None`` if the mask is empty."""
    ys, xs = np.where(mask > 0)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _place(local: np.ndarray, frame_shape: Sequence[int], roi: Box) -> np.ndarray:
    full = np.zeros(frame_shape[:2], dtype=np.uint8)
    x0, y0, x1, y1 = roi
    full[y0:y1, x0:x1] = local
    return full


def segment_min_channel(
    frame_bgr: np.ndarray,
    *,
    roi_xyxy: Sequence[int],
    threshold: int = 220,
    min_area: int = 4,
) -> np.ndarray:
    """White/near-white ink: every channel above ``threshold``.

    Equivalent to ``min(B, G, R) > threshold``. Using the minimum channel rather than luma
    means a saturated bright colour (high luma, low minimum channel) is not read as white
    ink.
    """
    roi = _check_roi(frame_bgr, roi_xyxy)
    x0, y0, x1, y1 = roi
    crop = frame_bgr[y0:y1, x0:x1]
    minimum = np.minimum.reduce([crop[:, :, 0], crop[:, :, 1], crop[:, :, 2]])
    local = (minimum > threshold).astype(np.uint8) * 255
    return _place(clean_components(local, min_area), frame_bgr.shape, roi)


def segment_chromatic_difference(
    frame_bgr: np.ndarray,
    *,
    roi_xyxy: Sequence[int],
    red_range: Tuple[int, int],
    green_max: int,
    blue_range: Tuple[int, int],
    red_minus_green_min: int,
    blue_minus_green_min: int = 0,
    min_area: int = 4,
) -> np.ndarray:
    """Coloured ink isolated by channel-difference predicates.

    Absolute channel ranges alone are not enough over real footage; the discriminating
    signal is the *gap* between channels (e.g. burgundy ink over an orange ground has a
    comparable red channel but a strongly suppressed green).
    """
    roi = _check_roi(frame_bgr, roi_xyxy)
    x0, y0, x1, y1 = roi
    crop = frame_bgr[y0:y1, x0:x1]
    blue = crop[:, :, 0].astype(np.int16)
    green = crop[:, :, 1].astype(np.int16)
    red = crop[:, :, 2].astype(np.int16)
    keep = (
        (red > red_range[0])
        & (red < red_range[1])
        & (green < green_max)
        & (blue > blue_range[0])
        & (blue < blue_range[1])
        & ((red - green) > red_minus_green_min)
        & ((blue - green) >= blue_minus_green_min)
    )
    local = keep.astype(np.uint8) * 255
    return _place(clean_components(local, min_area), frame_bgr.shape, roi)


def segment_temporal_difference(
    state_frames: Sequence[np.ndarray],
    blank_frames: Sequence[np.ndarray],
    *,
    roi_xyxy: Sequence[int],
    min_delta: int = 24,
    min_area: int = 4,
    max_ink_fraction: float = 0.25,
) -> np.ndarray:
    """Static ink over moving footage, recovered from the reference's own blank frames.

    Takes the per-pixel median of the state's frames and of adjacent text-free frames.
    Moving content cancels in both medians; static ink survives in only one. Both polarities
    are detected, so dark ink on a bright plate and bright ink on a dark plate are handled
    without being told which to expect.

    The blank frames must be **same-shot neighbours**. Differencing across a shot change
    makes the entire ROI read as ink, so a result covering more than ``max_ink_fraction`` of
    the ROI fails closed instead of returning a full-frame box. This is not hypothetical:
    RefB has exactly two blank frames (115, 116) and they sit in a different shot from most
    of its 39 states.
    """
    if not len(state_frames):
        raise ExtractError("no state frames supplied")
    if not len(blank_frames):
        raise ExtractError(
            "temporal difference needs adjacent blank (text-free) frames; none supplied"
        )
    roi = _check_roi(state_frames[0], roi_xyxy)
    x0, y0, x1, y1 = roi

    def _median_gray(frames: Sequence[np.ndarray]) -> np.ndarray:
        stack = np.stack([frame[y0:y1, x0:x1] for frame in frames])
        median = np.median(stack, axis=0).astype(np.uint8)
        return cv2.cvtColor(median, cv2.COLOR_BGR2GRAY).astype(np.int16)

    state_gray = _median_gray(state_frames)
    blank_gray = _median_gray(blank_frames)
    delta = np.abs(blank_gray - state_gray)
    local = (delta > min_delta).astype(np.uint8) * 255
    # Close single-pixel codec holes without bridging separate glyph flourishes.
    # The kernel must be odd-sized and symmetric: an even kernel such as np.ones((2, 2))
    # anchors off-centre and shifts every recovered box by one pixel in each direction,
    # which silently biases the placement geometry of every extracted state.
    local = cv2.morphologyEx(
        local, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    )
    local = clean_components(local, min_area)

    roi_pixels = (x1 - x0) * (y1 - y0)
    ink_fraction = float((local > 0).sum()) / float(roi_pixels)
    if ink_fraction > max_ink_fraction:
        raise ExtractError(
            f"temporal difference marked {ink_fraction:.1%} of the roi as ink, above the "
            f"{max_ink_fraction:.1%} limit; the blank frames are almost certainly from a "
            "different scene than the state frames. Supply same-shot text-free neighbours, "
            "or raise max_ink_fraction deliberately if the caption really is this large."
        )

    return _place(local, state_frames[0].shape, roi)


def restrict_to_box(mask: np.ndarray, box: Sequence[int]) -> np.ndarray:
    """Zero every pixel outside ``box``.

    Concurrent caption states share a frame, so ink has to be attributed to one state's
    box at a time. Measuring through an unrestricted full-frame mask mixes a neighbouring
    line — or plain bright footage — into this state's geometry and ink.
    """
    x0, y0, x1, y1 = (int(value) for value in box)
    out = np.zeros_like(mask)
    out[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
    return out


def dominant_text_line(
    mask: np.ndarray,
    *,
    min_vertical_overlap: float = 0.35,
    min_area: int = 4,
) -> np.ndarray:
    """Keep only the components belonging to the largest horizontal text line.

    A caption is a horizontal run of glyphs sharing a vertical band. Unrelated static bright
    regions elsewhere in the frame do not share that band, but they do survive a size filter,
    and a union box computed over them is meaningless.

    Components are grouped when their vertical extents overlap by at least
    ``min_vertical_overlap`` of the shorter one, which keeps descenders and dots on the line
    they belong to. The line carrying the most ink wins.
    """
    binary = (mask > 0).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    components = [
        (
            index,
            int(stats[index, cv2.CC_STAT_TOP]),
            int(stats[index, cv2.CC_STAT_TOP] + stats[index, cv2.CC_STAT_HEIGHT]),
            int(stats[index, cv2.CC_STAT_AREA]),
        )
        for index in range(1, count)
        if stats[index, cv2.CC_STAT_AREA] >= min_area
    ]
    if not components:
        return np.zeros(mask.shape, dtype=np.uint8)

    # Group by vertical overlap, walking top to bottom.
    components.sort(key=lambda item: item[1])
    lines: List[List[tuple]] = []
    for component in components:
        _, top, bottom, _ = component
        placed = False
        for line in lines:
            line_top = min(item[1] for item in line)
            line_bottom = max(item[2] for item in line)
            overlap = min(bottom, line_bottom) - max(top, line_top)
            shorter = min(bottom - top, line_bottom - line_top) or 1
            if overlap / shorter >= min_vertical_overlap:
                line.append(component)
                placed = True
                break
        if not placed:
            lines.append([component])

    winner = max(lines, key=lambda line: sum(item[3] for item in line))
    out = np.zeros(mask.shape, dtype=np.uint8)
    for index, _, _, _ in winner:
        out[labels == index] = 255
    return out


def filter_text_like_components(
    mask: np.ndarray,
    *,
    max_area_fraction: float = 0.02,
    min_area: int = 4,
) -> np.ndarray:
    """Keep only components small enough to plausibly be glyphs.

    A caption is text: many small components, none occupying a large share of the frame. A
    glyph at 1920x1080 is on the order of 40x60px, about 0.1% of the frame; a static bright
    sky or wall is orders of magnitude larger. Filtering on that is a general prior on what
    glyphs are, not a hand-tuned per-reel ROI.

    Brightness plus temporal stability alone still admits large static bright footage
    regions when re-deriving an approved composited output.
    """
    if not 0 < max_area_fraction <= 1:
        raise ExtractError(
            f"max_area_fraction must be in (0, 1], got {max_area_fraction}"
        )
    binary = (mask > 0).astype(np.uint8)
    frame_area = float(binary.shape[0] * binary.shape[1])
    ceiling = frame_area * max_area_fraction

    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    out = np.zeros(mask.shape, dtype=np.uint8)
    for index in range(1, count):
        area = stats[index, cv2.CC_STAT_AREA]
        if min_area <= area <= ceiling:
            out[labels == index] = 255
    return out


def segment_temporally_static_ink(
    state_frames: Sequence[np.ndarray],
    *,
    roi_xyxy: Sequence[int],
    threshold: int = 200,
    max_temporal_delta: int = 6,
    min_area: int = 4,
    stability_trim: float = 0.2,
) -> np.ndarray:
    """Bright ink that holds still while the footage under it moves.

    For an already-composited output there are usually no blank frames to difference against,
    so :func:`segment_temporal_difference` does not apply. But a caption is static across its
    own state span by definition, and footage is not. This combines both signals: bright in
    the per-pixel median, and stable across the span.

    ``max_temporal_delta`` tolerates the small per-frame variation a re-encode puts on an
    unchanging overlay; that is codec jitter, not motion.

    Both signals are robust to a minority of anomalous frames. Brightness always was — a
    median ignores outliers by construction. Stability was not: measured as the full temporal
    range, a single blown frame drives the range to ~255 for every pixel at once and empties
    the entire mask. ``stability_trim`` discards that fraction of the most extreme samples at
    each end per pixel before measuring the range, so a flash, whip pan or exposure pop inside
    a caption's own span no longer erases the caption. Genuine motion is still rejected: a
    pixel that moves is either dark in the median or spread across the surviving samples.

    Regression class: a static end-card spanning frames [71,238) where frame 72 is a flash
    transition; without trimming, sampling 71-75 recovers nothing at all.

    A span of two frames has no majority to appeal to, so no trimming is applied there.

    Honest limit: a genuinely static bright region of footage is indistinguishable from static
    ink by this method. Pair it with :func:`classify_mask_tier`, which flags the result.
    """
    if not 0.0 <= stability_trim < 0.5:
        raise ExtractError(
            f"stability_trim must be in [0.0, 0.5), got {stability_trim}"
        )
    if len(state_frames) < 2:
        raise ExtractError(
            f"temporally static ink needs at least two frames, got {len(state_frames)}"
        )
    roi = _check_roi(state_frames[0], roi_xyxy)
    x0, y0, x1, y1 = roi

    stack = np.stack([frame[y0:y1, x0:x1] for frame in state_frames]).astype(np.int16)
    median = np.median(stack, axis=0)
    minimum_channel = median.min(axis=2)
    bright = minimum_channel > threshold

    # Temporal range per pixel, over the channel that varies most. The range is measured
    # after trimming the most extreme samples at each end, so one anomalous frame in the span
    # cannot collapse the mask; with too few frames to spare, fall back to the full range.
    trim = int(len(stack) * stability_trim)
    if trim and len(stack) - 2 * trim >= 2:
        kept = np.sort(stack, axis=0)[trim : len(stack) - trim]
    else:
        kept = stack
    spread = (kept.max(axis=0) - kept.min(axis=0)).max(axis=2)
    stable = spread <= max_temporal_delta

    local = (bright & stable).astype(np.uint8) * 255
    return _place(clean_components(local, min_area), state_frames[0].shape, roi)


# Local-contrast neighbourhood for colour-agnostic ink detection. Large enough that a glyph
# stroke is "distinct" against the plate behind it, small enough that a whole caption line is
# not swallowed into its own background estimate.
LOCAL_BACKGROUND_KERNEL = 31
MIN_LOCAL_CONTRAST = 22


def segment_static_distinct_ink(
    state_frames: Sequence[np.ndarray],
    *,
    roi_xyxy: Sequence[int],
    max_temporal_delta: int = 12,
    min_local_contrast: int = MIN_LOCAL_CONTRAST,
    min_area: int = 8,
) -> np.ndarray:
    """Ink of ANY colour that holds still while the footage under it moves.

    ``segment_temporally_static_ink`` gates on brightness and therefore only sees near-white
    ink. The sealed RefB treatment contract records states carrying "cyan/pink/cream/navy or
    scene-reactive internal color", and measured across all 39 states a brightness gate returned
    no ink at all on 7 of them. DOCTRINE rule 3.4's "no default operator" applies to detection
    as much as to compositing: do not assume white ink.

    Two colour-agnostic signals, both required:

    * **temporally stable** across the state's own span - a caption is, footage is not;
    * **locally distinct** - the pixel differs from its own neighbourhood by more than
      ``min_local_contrast`` in some channel, whatever colour either happens to be.

    The second signal also fixes a false positive the brightness gate had: a large flat bright
    region is static and bright, but it does not differ from its own neighbourhood, so only its
    edge can survive - never its interior.
    """
    if len(state_frames) < 2:
        raise ExtractError(
            f"static distinct ink needs at least two frames, got {len(state_frames)}"
        )
    roi = _check_roi(state_frames[0], roi_xyxy)
    x0, y0, x1, y1 = roi

    stack = np.stack([frame[y0:y1, x0:x1] for frame in state_frames]).astype(np.int16)
    median = np.median(stack, axis=0).astype(np.uint8)

    # Local background estimate: a heavy blur of the state's own median frame.
    background = cv2.GaussianBlur(
        median, (LOCAL_BACKGROUND_KERNEL, LOCAL_BACKGROUND_KERNEL), 0
    )
    distinct = (
        np.abs(median.astype(np.int16) - background.astype(np.int16)).max(axis=2)
        > min_local_contrast
    )

    spread = (stack.max(axis=0) - stack.min(axis=0)).max(axis=2)
    stable = spread <= max_temporal_delta

    local = (distinct & stable).astype(np.uint8) * 255
    return _place(clean_components(local, min_area), state_frames[0].shape, roi)


def core_and_treatment_bbox(
    alpha: np.ndarray,
    *,
    max_treatment_dilation_px: Optional[int] = 32,
) -> Tuple[Box, Box]:
    """Return the core (>=50% alpha) box and the low-alpha treatment extent.

    Two boxes, never one: the ink and the glow/shadow around it are different
    measurements, and collapsing them loses the treatment evidence.

    ``max_treatment_dilation_px`` bounds the low-alpha sweep to a neighbourhood of the core
    ink. Without it, bright footage elsewhere in the frame is swept up as though it were
    this glyph's glow — measured on the real RefB authority, an unbounded sweep returned
    ``(50, 496, 1048, 1078)`` for a state whose core box was ``(869, 497, 1047, 583)``.
    Pass ``None`` to sweep the whole field deliberately.
    """
    core = union_bbox((alpha >= CORE_ALPHA).astype(np.uint8))
    if core is None:
        raise ExtractError("alpha field carries no ink at the core threshold")

    soft = (alpha >= TREATMENT_ALPHA).astype(np.uint8)
    if max_treatment_dilation_px is not None:
        if max_treatment_dilation_px < 0:
            raise ExtractError(
                f"max_treatment_dilation_px must be >= 0, got {max_treatment_dilation_px}"
            )
        height, width = alpha.shape[:2]
        pad = int(max_treatment_dilation_px)
        soft = restrict_to_box(
            soft,
            (
                max(0, core[0] - pad),
                max(0, core[1] - pad),
                min(width, core[2] + pad),
                min(height, core[3] + pad),
            ),
        )

    treatment = union_bbox(soft)
    if treatment is None:
        raise ExtractError("alpha field carries no ink at the treatment threshold")
    return core, treatment


@dataclass(frozen=True)
class InkMeasurement:
    method: str
    rgb_median: Tuple[int, int, int]
    rgb_p05: Tuple[int, int, int]
    rgb_p95: Tuple[int, int, int]
    interior_pixels: int
    sample: str = "erode"
    stroke_width_px: float = 0.0

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "sample": self.sample,
            "rgb_median": list(self.rgb_median),
            "rgb_p05": list(self.rgb_p05),
            "rgb_p95": list(self.rgb_p95),
            "interior_pixels": self.interior_pixels,
            "stroke_width_px": round(self.stroke_width_px, 3),
        }


def measure_ink(
    frame_bgr: np.ndarray,
    mask: np.ndarray,
    *,
    method: str = "min_channel_threshold",
    erode_px: int = 1,
    sample: str = "erode",
) -> InkMeasurement:
    """Sample ink from the interior of a glyph in the reference frame.

    Two sampling modes:

    ``erode``
        Shrink the mask by ``erode_px`` so antialiased edge pixels - a blend of ink and whatever
        lies behind it - cannot drag the measured colour toward the background. Fine for thick
        strokes; useless for thin ones, because a stroke narrower than the kernel erodes to
        nothing and the function falls back to the unmodified mask, edges included.

    ``ridge``
        Take the pixels furthest from the mask's own boundary, via a distance transform. This
        generalises erosion: for a thick stroke the ridge is a broad interior region, for a
        2px script stroke it is the centreline. Formal high-contrast script needs this - on
        thin formal script states, erosion was measured moving the sampled ink by ~20 RGB units.
    """
    if sample not in {"erode", "ridge", "auto"}:
        raise ExtractError(
            f"unknown ink sample mode {sample!r}; expected 'auto', 'erode' or 'ridge'"
        )
    if mask.shape[:2] != frame_bgr.shape[:2]:
        raise ExtractError(
            f"mask shape {mask.shape[:2]} does not match frame shape {frame_bgr.shape[:2]}"
        )
    binary = (mask > 0).astype(np.uint8)
    if binary.sum() == 0:
        raise ExtractError("cannot measure ink from an empty mask")

    distance = cv2.distanceTransform(binary, cv2.DIST_L2, 3)
    # Twice the peak distance-to-boundary is the stroke's thickness.
    stroke_width = float(distance.max() * 2.0)

    resolved = sample
    if sample == "auto":
        # Measured on real states whose strokes are 7-8px wide: erosion is the accurate
        # tool for a stroke with a real interior, and forcing the ridge on one over-narrows the
        # sample and moves the measurement away from the truth. The thin path is only correct
        # where there is no interior to find.
        resolved = "auto:erode" if stroke_width > MIN_GEOMETRIC_RIDGE_WIDTH else "auto:thin"

    if resolved in {"ridge", "auto:thin"}:
        if resolved == "ridge" and stroke_width > MIN_GEOMETRIC_RIDGE_WIDTH:
            # Thick enough to have a geometric interior: keep the deepest band.
            threshold = max(distance.max() * 0.75, 1.0)
            interior = (distance >= threshold).astype(np.uint8)
            if interior.sum() == 0:
                interior = binary
        else:
            # A stroke this thin has NO interior - every pixel touches the boundary - so
            # geometry cannot separate core from antialiased shoulder. The core is instead the
            # extreme of the masked luma distribution, and which extreme depends on the ink's
            # polarity against its own surround: bright ink on a dark plate has the brightest
            # core, dark ink on a bright plate the darkest.
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).astype(np.float64)
            ring = (
                (cv2.dilate(binary, np.ones((5, 5), np.uint8)) > 0) & (binary == 0)
            ).astype(np.uint8)
            masked = gray[binary > 0]
            surround = gray[ring > 0] if ring.sum() else masked
            brighter_than_surround = float(np.median(masked)) >= float(np.median(surround))
            cut = np.percentile(masked, 60 if brighter_than_surround else 40)
            keep = (gray >= cut) if brighter_than_surround else (gray <= cut)
            interior = ((binary > 0) & keep).astype(np.uint8)
            if interior.sum() == 0:
                interior = binary
    else:
        interior = binary
        if erode_px > 0:
            kernel = np.ones((erode_px * 2 + 1, erode_px * 2 + 1), np.uint8)
            eroded = cv2.erode(binary, kernel)
            if eroded.sum() > 0:
                interior = eroded

    ys, xs = np.where(interior > 0)
    samples = frame_bgr[ys, xs].astype(np.float64)  # BGR

    def _percentile(value: float) -> Tuple[int, int, int]:
        channel = np.percentile(samples, value, axis=0)
        # Report RGB, not OpenCV's BGR.
        return (
            int(round(channel[2])),
            int(round(channel[1])),
            int(round(channel[0])),
        )

    return InkMeasurement(
        method=method,
        rgb_median=_percentile(50),
        rgb_p05=_percentile(5),
        rgb_p95=_percentile(95),
        interior_pixels=int(interior.sum()),
        sample=resolved,
        stroke_width_px=stroke_width,
    )


def detect_state_span(
    frames: Sequence[np.ndarray],
    *,
    roi_xyxy: Sequence[int],
    threshold: int = 220,
    min_area: int = 4,
    min_ink_pixels: int = 4,
) -> Optional[Tuple[int, int]]:
    """First and last frame carrying ink, as a half-open ``[start, end_exclusive)``.

    Returns ``None`` when the state never appears. A single-frame result is legitimate:
    one-frame caption blinks are real reference behaviour.
    """
    present: List[int] = []
    for index, frame in enumerate(frames):
        mask = segment_min_channel(
            frame, roi_xyxy=roi_xyxy, threshold=threshold, min_area=min_area
        )
        if int((mask > 0).sum()) >= min_ink_pixels:
            present.append(index)
    if not present:
        return None
    return present[0], present[-1] + 1


# --- mask evidence tiering ---------------------------------------------------

# <ref-11>: clean | secondary | diagnostic_partial, and only `clean` enters aggregate
# ranking (DOCTRINE rule 6.2).
MASK_TIERS = ("clean", "secondary", "diagnostic_partial")

# Separation is measured in 8-bit luma between the eroded interior of the mask and a ring
# just outside it. Calibrated against the real RefB authority, where a state over bright
# footage measured ink median RGB (215, 225, 224) against a comparably bright surround -
# a mask with no separation from its own surround is not isolating ink.
CLEAN_SEPARATION = 60.0
SECONDARY_SEPARATION = 30.0

# Separation alone is not sufficient. Measured on the real RefB authority, the '<word C>' state
# cleared the separation gate at 82 while its ink median read RGB (215, 225, 224) against a
# p95 of (247, 242, 235) - the mask interior was ink *plus* background. <ref-11> classifies that
# state `secondary`. A bimodal interior also catches footage-reactive ink, which <ref-11>
# likewise downgrades because it prevents clean whole-word authority.
CLEAN_MAX_INTERIOR_SPREAD = 40.0


@dataclass(frozen=True)
class MaskTierVerdict:
    tier: str
    separation: float
    interior_spread: float
    interior_luma: float
    ring_luma: float
    interior_pixels: int
    ring_pixels: int
    components: int = 1
    worst_component_separation: float = 0.0
    worst_component_spread: float = 0.0
    reason: str = ""

    @property
    def promotable(self) -> bool:
        return self.tier == "clean"

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "promotable": self.promotable,
            "reason": self.reason,
            "separation": round(self.separation, 3),
            "interior_spread": round(self.interior_spread, 3),
            "interior_luma": round(self.interior_luma, 3),
            "ring_luma": round(self.ring_luma, 3),
            "interior_pixels": self.interior_pixels,
            "ring_pixels": self.ring_pixels,
            "components": self.components,
            "worst_component_separation": round(self.worst_component_separation, 3),
            "worst_component_spread": round(self.worst_component_spread, 3),
            "gates": {
                "clean_separation": CLEAN_SEPARATION,
                "secondary_separation": SECONDARY_SEPARATION,
                "clean_max_interior_spread": CLEAN_MAX_INTERIOR_SPREAD,
            },
        }


def is_promotable_tier(tier: str) -> bool:
    """Only a `clean` mask may become production alpha."""
    if tier not in MASK_TIERS:
        raise ExtractError(f"unknown mask tier {tier!r}; expected one of {list(MASK_TIERS)}")
    return tier == "clean"


def classify_mask_tier(
    frame_bgr: np.ndarray,
    mask: np.ndarray,
    *,
    ring_px: int = 3,
    erode_px: int = 1,
    min_component_area_fraction: float = 0.02,
) -> MaskTierVerdict:
    """Classify how cleanly a mask isolates ink from its surround.

    The discriminating signal is *separation* between the mask's interior and a ring just
    outside it — not internal variance. Scene-reactive ink legitimately varies inside the
    glyph, so variance must not disqualify a well-separated mask.
    """
    if mask.shape[:2] != frame_bgr.shape[:2]:
        raise ExtractError(
            f"mask shape {mask.shape[:2]} does not match frame shape {frame_bgr.shape[:2]}"
        )
    binary = (mask > 0).astype(np.uint8)
    if binary.sum() == 0:
        raise ExtractError("cannot tier an empty mask")

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).astype(np.float64)

    interior = binary
    if erode_px > 0:
        eroded = cv2.erode(binary, np.ones((erode_px * 2 + 1,) * 2, np.uint8))
        if eroded.sum() > 0:
            interior = eroded

    dilated = cv2.dilate(binary, np.ones((ring_px * 2 + 1,) * 2, np.uint8))
    ring = ((dilated > 0) & (binary == 0)).astype(np.uint8)
    if ring.sum() == 0:
        raise ExtractError("mask fills the frame; no surrounding ring to compare against")

    interior_values = gray[interior > 0]
    interior_luma = float(np.median(interior_values))
    ring_luma = float(np.median(gray[ring > 0]))
    separation = abs(interior_luma - ring_luma)
    interior_spread = float(
        np.percentile(interior_values, 90) - np.percentile(interior_values, 10)
    )

    # Whole-word authority: a word is only clean when EVERY component separates cleanly.
    # A global median averages over the mask and hides one letter lost against bright
    # background, which is exactly how states the sealed authority calls
    # `diagnostic_partial` scored as clean during calibration.
    count, labels, comp_stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    components = max(count - 1, 1)
    total_ink = float(binary.sum()) or 1.0
    # Dots, commas and antialias fragments are legitimately low-contrast against whatever
    # sits behind them. Letting them govern the worst-component score over-downgrades
    # genuinely clean words, so components below a share of the total ink are excluded.
    significant = [
        index
        for index in range(1, count)
        if comp_stats[index, cv2.CC_STAT_AREA] / total_ink >= min_component_area_fraction
    ]
    worst_separation = separation
    worst_spread = interior_spread
    if len(significant) > 1:
        worst_separation = float("inf")
        worst_spread = 0.0
        ring_kernel = np.ones((ring_px * 2 + 1,) * 2, np.uint8)
        for index in significant:
            component = (labels == index).astype(np.uint8)
            comp_interior = component
            if erode_px > 0:
                comp_eroded = cv2.erode(component, np.ones((erode_px * 2 + 1,) * 2, np.uint8))
                if comp_eroded.sum() > 0:
                    comp_interior = comp_eroded
            comp_ring = (
                (cv2.dilate(component, ring_kernel) > 0) & (binary == 0)
            ).astype(np.uint8)
            if comp_ring.sum() == 0 or comp_interior.sum() == 0:
                continue
            comp_values = gray[comp_interior > 0]
            comp_luma = float(np.median(comp_values))
            comp_ring_luma = float(np.median(gray[comp_ring > 0]))
            worst_separation = min(worst_separation, abs(comp_luma - comp_ring_luma))
            worst_spread = max(
                worst_spread,
                float(
                    np.percentile(comp_values, 90) - np.percentile(comp_values, 10)
                ),
            )
        if worst_separation == float("inf"):
            worst_separation = separation
            worst_spread = interior_spread

    separation = min(separation, worst_separation)
    interior_spread = max(interior_spread, worst_spread)

    if separation < SECONDARY_SEPARATION:
        tier = "diagnostic_partial"
        reason = (
            f"interior/ring separation {separation:.1f} is below the "
            f"{SECONDARY_SEPARATION:.0f} floor; the mask is not isolating ink from its "
            "surround"
        )
    elif separation < CLEAN_SEPARATION:
        tier = "secondary"
        reason = (
            f"interior/ring separation {separation:.1f} clears the "
            f"{SECONDARY_SEPARATION:.0f} floor but not the {CLEAN_SEPARATION:.0f} clean gate"
        )
    elif interior_spread > CLEAN_MAX_INTERIOR_SPREAD:
        tier = "secondary"
        reason = (
            f"separation {separation:.1f} is clean but the interior spread "
            f"{interior_spread:.1f} exceeds {CLEAN_MAX_INTERIOR_SPREAD:.0f}: the masked "
            "interior does not read as one ink field, so it is either footage-reactive ink "
            "or a mask carrying background. Either way it is not clean whole-word authority."
        )
    else:
        tier = "clean"
        reason = (
            f"separation {separation:.1f} and interior spread {interior_spread:.1f} both "
            "within the clean gates"
        )

    return MaskTierVerdict(
        tier=tier,
        separation=separation,
        interior_spread=interior_spread,
        interior_luma=interior_luma,
        ring_luma=ring_luma,
        interior_pixels=int(interior.sum()),
        ring_pixels=int(ring.sum()),
        components=components,
        worst_component_separation=worst_separation,
        worst_component_spread=worst_spread,
        reason=reason,
    )


@dataclass(frozen=True)
class ResolvedTier:
    tier: str
    tier_source: str
    machine_proposal: str
    authority_tier: Optional[str]
    human_tier: Optional[str]

    @property
    def promotable(self) -> bool:
        """Only an authority- or human-blessed `clean` mask becomes production alpha.

        A machine `clean` on its own is a proposal, not a blessing. Calibration against the
        sealed authority's 12 classified states agreed exactly 4 times out of 12 and
        over-promoted 5, so machine cleanliness cannot be treated as authority.
        """
        return self.tier == "clean" and self.tier_source in {
            "sealed_authority",
            "human_review",
        }

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "tier_source": self.tier_source,
            "promotable": self.promotable,
            "machine_proposal": self.machine_proposal,
            "authority_tier": self.authority_tier,
            "human_tier": self.human_tier,
        }


def resolve_mask_tier(
    *,
    machine_proposal: str,
    authority_tier: Optional[str] = None,
    human_tier: Optional[str] = None,
) -> ResolvedTier:
    """Combine a machine proposal with any authority or human classification.

    Two asymmetric powers:

    * A machine ``diagnostic_partial`` is a hard veto on the pixels' own evidence. Neither a
      sealed authority nor a human reviewer may promote past it by assertion.
    * A machine ``clean`` or ``secondary`` is advisory. It cannot promote anything on its
      own, but it does not block a declared classification either, so a human may resolve
      an ambiguous ``secondary`` upward after inspecting native overlays.
    """
    for label, value in (
        ("machine_proposal", machine_proposal),
        ("authority_tier", authority_tier),
        ("human_tier", human_tier),
    ):
        if value is not None and value not in MASK_TIERS:
            raise ExtractError(
                f"unknown {label} {value!r}; expected one of {list(MASK_TIERS)}"
            )

    declared = authority_tier if authority_tier is not None else human_tier
    declared_source = "sealed_authority" if authority_tier is not None else "human_review"

    if declared is None:
        return ResolvedTier(
            tier=machine_proposal,
            tier_source="machine_proposal",
            machine_proposal=machine_proposal,
            authority_tier=None,
            human_tier=None,
        )

    if machine_proposal == "diagnostic_partial" and declared != "diagnostic_partial":
        return ResolvedTier(
            tier=machine_proposal,
            tier_source="machine_downgrade",
            machine_proposal=machine_proposal,
            authority_tier=authority_tier,
            human_tier=human_tier,
        )
    return ResolvedTier(
        tier=declared,
        tier_source=declared_source,
        machine_proposal=machine_proposal,
        authority_tier=authority_tier,
        human_tier=human_tier,
    )


# --- compositing-operator fitting --------------------------------------------
#
# DOCTRINE rule 3.4 / BITF section 5. The engine has NO default operator: <ref-06>'s reference fits
# a Difference-family inversion, <ref-22>'s fits near-white SourceOver, and both must be fitted per
# reference per stack and compared by residual distributions.
#
#     SourceOver         O = B + a*(F - B)
#     Difference-family  O = B + a*(|B - S| - B)

# Below this relative margin between the two models' residuals, the fit is undecided. <ref-22>'s
# `<word D>` case fit near-black SourceOver at MAE 5.01 against a best Difference fit of 5.24 -
# a 4.6% margin, which that file explicitly calls a warning against appearance-only naming.
UNDECIDED_MARGIN = 0.15

# A background with almost no variation cannot constrain either model.
MIN_BACKGROUND_STD = 4.0


@dataclass(frozen=True)
class OperatorFit:
    model: str
    alpha: float
    source_rgb: Tuple[int, int, int]
    mae: float
    mae_source_over: float
    mae_difference_family: float
    margin: float
    decided: bool
    reason: str
    undecided_margin: float = UNDECIDED_MARGIN

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "alpha": round(self.alpha, 4),
            "source_rgb": list(self.source_rgb),
            "mae": round(self.mae, 4),
            "mae_source_over": round(self.mae_source_over, 4),
            "mae_difference_family": round(self.mae_difference_family, 4),
            "margin": round(self.margin, 4),
            "decided": self.decided,
            "reason": self.reason,
            "undecided_margin": self.undecided_margin,
            "naming_rule": (
                "A flattened encode cannot distinguish exact editor blend metadata. The verdict "
                "is 'difference_family_inversion', never a named editor mode."
            ),
        }


def _model_error(
    values: np.ndarray,
    base: np.ndarray,
    target: np.ndarray,
    alpha: float,
    invert: bool,
) -> np.ndarray:
    """Mean absolute residual of one model for each candidate source value."""
    layer = (
        np.abs(base[None, :] - values[:, None])
        if invert
        else np.repeat(values[:, None], base.size, axis=1)
    )
    predicted = base[None, :] + alpha * (layer - base[None, :])
    return np.abs(predicted - target[None, :]).mean(axis=1)


def _fit_one(observed: np.ndarray, plate: np.ndarray, invert: bool):
    """Grid-fit alpha and the per-channel source value for one model.

    The Difference-family model is not linear in the source value, so both models are fitted by
    sweeping candidate source values rather than solving analytically. A closed-form inverse for
    the inversion branch collapses to mid-grey, which fits nothing.
    """
    coarse = np.arange(0, 256, 4, dtype=np.float64)
    best = None
    for alpha in np.linspace(0.2, 1.0, 33):
        total_error = 0.0
        source = []
        for channel in range(3):
            base = plate[:, channel].astype(np.float64)
            target = observed[:, channel].astype(np.float64)

            errors = _model_error(coarse, base, target, alpha, invert)
            centre = coarse[int(np.argmin(errors))]
            fine = np.clip(np.arange(centre - 4, centre + 5, 1, dtype=np.float64), 0, 255)
            fine_errors = _model_error(fine, base, target, alpha, invert)
            index = int(np.argmin(fine_errors))
            source.append(float(fine[index]))
            total_error += float(fine_errors[index])
        mae = total_error / 3.0
        if best is None or mae < best[0]:
            best = (mae, float(alpha), tuple(int(round(value)) for value in source))
    return best


def fit_compositing_operator(
    output_bgr: np.ndarray,
    background_bgr: np.ndarray,
    mask: np.ndarray,
) -> OperatorFit:
    """Fit SourceOver and Difference-family to the masked region and compare residuals.

    Returns whichever model the pixels support, plus both residuals so the margin is visible.
    A thin margin is reported as undecided rather than resolved by preference, and a background
    with no variation is reported as unable to constrain the operator at all.
    """
    if output_bgr.shape != background_bgr.shape:
        raise ExtractError(
            f"output shape {output_bgr.shape} does not match background shape "
            f"{background_bgr.shape}"
        )
    if mask.shape[:2] != output_bgr.shape[:2]:
        raise ExtractError(
            f"mask shape {mask.shape[:2]} does not match frame shape {output_bgr.shape[:2]}"
        )
    where = mask > 0
    if not where.any():
        raise ExtractError("cannot fit a compositing operator through an empty mask")

    ys, xs = np.where(where)
    observed = output_bgr[ys, xs].reshape(-1, 3)
    plate = background_bgr[ys, xs].reshape(-1, 3)

    background_std = float(plate.astype(np.float64).std())

    over = _fit_one(observed, plate, invert=False)
    difference = _fit_one(observed, plate, invert=True)
    mae_over, alpha_over, source_over = over
    mae_diff, alpha_diff, source_diff = difference

    if mae_over <= mae_diff:
        model, alpha, source, mae = "source_over", alpha_over, source_over, mae_over
    else:
        model, alpha, source, mae = (
            "difference_family_inversion", alpha_diff, source_diff, mae_diff,
        )

    worse = max(mae_over, mae_diff)
    margin = 0.0 if worse <= 0 else abs(mae_over - mae_diff) / worse

    if background_std < MIN_BACKGROUND_STD:
        decided = False
        reason = (
            f"background standard deviation {background_std:.2f} is below "
            f"{MIN_BACKGROUND_STD}: a background with no variation cannot constrain either "
            "model, so the operator is not recoverable from these pixels"
        )
    elif margin < UNDECIDED_MARGIN:
        decided = False
        reason = (
            f"undecided: source_over MAE {mae_over:.3f} vs difference_family MAE "
            f"{mae_diff:.3f} is a {margin:.1%} margin, below the {UNDECIDED_MARGIN:.0%} "
            "threshold. Appearance-only mode naming is forbidden at this margin."
        )
    else:
        decided = True
        reason = (
            f"{model} supported: MAE {mae:.3f} against {worse:.3f} for the alternative, a "
            f"{margin:.1%} margin"
        )

    # Report RGB, not OpenCV's BGR.
    source_rgb = (source[2], source[1], source[0])
    return OperatorFit(
        model=model, alpha=alpha, source_rgb=source_rgb, mae=mae,
        mae_source_over=mae_over, mae_difference_family=mae_diff,
        margin=margin, decided=decided, reason=reason,
    )


def segment_difference_family_ink(
    output_bgr: np.ndarray,
    background_bgr: np.ndarray,
    *,
    roi_xyxy: Sequence[int],
    min_delta: int = 24,
    min_area: int = 4,
) -> np.ndarray:
    """Ink composited through a Difference-family inversion.

    Under inversion against a white source the ink reads as ``255 - B``, so inside the glyph the
    output moves *away* from the background in the direction opposite its brightness. <ref-22>
    measured background/output channel correlations around -0.89 for exactly this case.

    A near-white SourceOver caption over a bright plate does not satisfy it, because there the
    output moves toward white regardless of the background.
    """
    roi = _check_roi(output_bgr, roi_xyxy)
    if output_bgr.shape != background_bgr.shape:
        raise ExtractError(
            f"output shape {output_bgr.shape} does not match background shape "
            f"{background_bgr.shape}"
        )
    x0, y0, x1, y1 = roi
    observed = output_bgr[y0:y1, x0:x1].astype(np.int16)
    plate = background_bgr[y0:y1, x0:x1].astype(np.int16)

    inverted = 255 - plate
    # Close to the inversion of the plate, and far from the plate itself.
    near_inversion = np.abs(observed - inverted).max(axis=2) <= min_delta
    far_from_plate = np.abs(observed - plate).max(axis=2) > min_delta
    local = (near_inversion & far_from_plate).astype(np.uint8) * 255
    return _place(clean_components(local, min_area), output_bgr.shape, roi)


# --- text consistency ---------------------------------------------------------
#
# The mask tier asks whether ink separates from its surround. It cannot ask whether the mask is
# the glyph at all. The sealed authority declares the exact string for every state, so that is
# directly checkable, and it is not circular: the text comes from the document, the mask from
# pixels, and neither informed the other. NFS section 6 names these dimensions - class/case and
# anatomy/topology including component counts - as ones to compare separately.

# Text is sparse inside its own bounding box; a background region is dense. Calibrated on recovered
# glyph masks rather than guessed: long thin script words filled ~0.10-0.12 of their box,
# short heavy sans words ~0.50-0.52, and a two-glyph word 0.687. A solid background region
# fills ~1.0. The range carries margin either side of that measured span.
TEXT_FILL_RATIO_RANGE = (0.06, 0.80)

# Per-glyph advance is on the order of the cap height. Measured aspect-per-glyph on the same
# real masks: 0.37 (a long word) to 0.68 (a short word). The range is deliberately wider, because script
# faces, punctuation and single-glyph states vary far more than that sample shows.
TEXT_ASPECT_PER_GLYPH = (0.30, 1.80)

# A word cannot be built from far more blobs than it has glyphs; speckle can.
MAX_COMPONENTS_PER_GLYPH = 3.0


@dataclass(frozen=True)
class TextConsistency:
    consistent: bool
    reason: str
    components: int
    glyphs: int
    lines: int
    fill_ratio: float
    aspect_ratio: float
    expected_aspect_range: Tuple[float, float]

    def to_dict(self) -> dict:
        return {
            "consistent": self.consistent,
            "reason": self.reason,
            "components": self.components,
            "glyphs": self.glyphs,
            "lines": self.lines,
            "fill_ratio": round(self.fill_ratio, 4),
            "aspect_ratio": round(self.aspect_ratio, 4),
            "expected_aspect_range": [
                round(self.expected_aspect_range[0], 3),
                round(self.expected_aspect_range[1], 3),
            ],
            "gates": {
                "fill_ratio_range": list(TEXT_FILL_RATIO_RANGE),
                "aspect_per_glyph": list(TEXT_ASPECT_PER_GLYPH),
                "max_components_per_glyph": MAX_COMPONENTS_PER_GLYPH,
            },
        }


def text_consistency(
    mask: np.ndarray,
    text: str,
    *,
    min_area: int = 8,
) -> TextConsistency:
    """Whether a recovered mask could plausibly hold the declared string.

    Checks three things a background region fails and a word passes: ink is sparse within its
    own box, the box's aspect matches the widest line's glyph count, and the component count is
    within a few times the glyph count. Merged glyphs are fine — script and tight kerning merge
    routinely, so fewer components than glyphs is normal and only far more is suspicious.
    """
    if not text or not text.strip():
        raise ExtractError("cannot check text consistency against empty text")

    lines = [line for line in text.splitlines() if line.strip()]
    widest = max(lines, key=lambda line: len(line.replace(" ", "")))
    glyphs = len(widest.replace(" ", ""))
    total_glyphs = sum(len(line.replace(" ", "")) for line in lines)
    line_count = len(lines)

    binary = (mask > 0).astype(np.uint8)
    box = union_bbox(binary)
    if box is None:
        return TextConsistency(
            consistent=False, reason="mask is empty", components=0, glyphs=glyphs,
            lines=line_count, fill_ratio=0.0, aspect_ratio=0.0,
            expected_aspect_range=(0.0, 0.0),
        )

    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    area = float(width * height)
    ink = float(binary.sum())
    fill_ratio = ink / area if area else 0.0
    aspect = width / height if height else 0.0

    count, _, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    components = sum(
        1 for index in range(1, count) if stats[index, cv2.CC_STAT_AREA] >= min_area
    )

    # A stacked caption is as many lines tall, so the per-line aspect is the box aspect
    # multiplied by the line count.
    low, high = TEXT_ASPECT_PER_GLYPH
    expected = (low * glyphs / line_count, high * glyphs / line_count)

    reasons = []
    if not TEXT_FILL_RATIO_RANGE[0] <= fill_ratio <= TEXT_FILL_RATIO_RANGE[1]:
        reasons.append(
            f"fill ratio {fill_ratio:.3f} outside the text range "
            f"{TEXT_FILL_RATIO_RANGE} - text is sparse inside its own box, a background region "
            "is dense"
        )
    if not expected[0] <= aspect <= expected[1]:
        reasons.append(
            f"aspect ratio {aspect:.2f} outside the {expected[0]:.2f}-{expected[1]:.2f} range "
            f"expected for {glyphs} glyphs on the widest of {line_count} line(s)"
        )
    if components > MAX_COMPONENTS_PER_GLYPH * max(total_glyphs, 1):
        reasons.append(
            f"{components} components for {total_glyphs} glyphs exceeds "
            f"{MAX_COMPONENTS_PER_GLYPH} per glyph - that is speckle, not a word"
        )

    if reasons:
        return TextConsistency(
            consistent=False, reason="; ".join(reasons), components=components,
            glyphs=glyphs, lines=line_count, fill_ratio=fill_ratio, aspect_ratio=aspect,
            expected_aspect_range=expected,
        )
    return TextConsistency(
        consistent=True,
        reason=(
            f"{components} components, fill ratio {fill_ratio:.3f}, aspect {aspect:.2f} - all "
            f"plausible for {glyphs} glyphs on the widest of {line_count} line(s)"
        ),
        components=components, glyphs=glyphs, lines=line_count,
        fill_ratio=fill_ratio, aspect_ratio=aspect, expected_aspect_range=expected,
    )
