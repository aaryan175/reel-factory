"""Ink resolution — the caption engine's legibility stage.

A caption contract's ink is *measured* from the reference's own pixels (DOCTRINE rule 3.1:
there is no constant-colour branch). That measurement is the truth about the reference. It is
not automatically the right fill for **our** picture, because our picture is not the
reference's: the reference puts a word on a dark background, ours puts it on a light one, and the
measured white then reads as nothing at all.

This module is the stage that closes that gap, and it is deliberately narrow. It moves exactly
one field — `states[].ink.rgb_median` — and refuses to emit a contract in which anything else
has changed. Text, case, spans, plates, plate hashes, geometry, placement, styles, stacking,
lifecycle and font hypotheses are not this stage's to touch.

Where the rules come from
-------------------------
Every constant and every branch below is a rule recorded by approved caption passes, promoted
out of those passes' work scripts and into the engine because DOCTRINE 18 forbids the factory
from reaching for a per-reel caption script when the picture changes:

1. **The target is 80% of the reference's own ring Michelson.** Not a fixed contrast number —
   a low-contrast beat in the reference stays a low-contrast beat here.
2. **The move is pure gain: hue held, only luminance moves, and the smallest move that reaches
   the target.** Michelson is maximised by driving ink to zero luminance against any
   background, so maximising it is not a legible objective; doing so turns white words black on
   dark footage.
3. **Nothing lands within `MIN_SEPARATION` luma levels of its own background ring.** A fill
   that clears a contrast ratio while sitting on top of its background is not legible.
4. **A flip is reachable only when the reference's own polarity cannot get within
   `FLIP_SHORTFALL` of the target**, and the flip must additionally beat the best in-polarity
   fill by `FLIP_MARGIN` Michelson. Otherwise the reference's polarity stands.
5. **On a flip away from a light fill, saturation may fall only as far as the gamut requires**,
   and how far it fell is recorded.
6. **The decision is made once per lockup**, over the union of that lockup's own footprints.
   Deciding per state let `<word E>` come out near-black on one state and white on the next — one frame
   apart, same word — because a threshold landed either side of a bound.

What a green resolution is not
------------------------------
It is a metric that scopes the choice. The approved passes both then adjudicated their flips on
a native-scale board composited over their own picture, and both receipts say the board is the
decision, not the number (DOCTRINE 10: the machine proposes, the authority decides). This
module therefore reports `creative_approval: PENDING` on every resolution it produces and
carries an `adjudication` field for an authority to fill in. It never self-approves.
"""

from __future__ import annotations

import colorsys
import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --- the approved pass's own constants --------------------------------------------------

TARGET_FRACTION = 0.80  # of the reference's own ring Michelson
FLIP_SHORTFALL = 0.90  # a lift landing this close to the target has done its job
FLIP_MARGIN = 0.15  # Michelson a flip must beat the best in-polarity fill by
MIN_SEPARATION = 25.0  # luma levels; no fill may sit closer than this to its own ring
MIN_REFERENCE_MICHELSON = 0.02  # below this the reference itself carries no contrast to match

# --- contrast-ratio units -----------------------------------------------------------------
#
# Michelson is a ratio of SUMS and the 25-level floor is an ABSOLUTE difference. Both are
# blind at the dark end: a word can clear 27.0 levels of separation (over the floor) and 0.415
# Michelson (over a 0.353 target) while actually measuring 1.37:1 against its surround. A reader sees the ratio. So does WCAG.
MIN_CONTRAST_RATIO = 3.0  # WCAG large-text minimum; below this a state is UNRESOLVED
TARGET_CONTRAST_RATIO = 4.5  # the bar a state should clear; below it the state is MARGINAL
MAX_WEAK_INK_FRACTION = 0.10  # share of ink allowed under MIN_CONTRAST_RATIO *locally*
MAX_LOCAL_SEPARATION_FAIL_FRACTION = 0.05  # share of ink allowed within MIN_SEPARATION locally
LOCAL_BACKGROUND_SIGMA = 5.0  # gaussian sigma of the per-pixel background estimate
INPAINT_RADIUS_PX = 3  # telea radius when a burned-in caption has to be removed first

RING_OUTER_PX = 17  # the ring is the annulus between these two dilations of the plate
RING_INNER_PX = 7
CORE_ALPHA = 0.6  # plate alpha counted as glyph core
CORE_ALPHA_FALLBACK = 0.35  # a plate too thin to yield 30 core pixels at 0.6
CORE_MIN_PIXELS = 30
PLATE_EXTENT_ALPHA = 0.35  # the alpha the ring is grown from

HOLD_FRAMES = 4  # a state is sampled on the last frames of its span: its hold

# Rec.709 luma, on RGB.
LUMA_COEFFICIENTS = (0.2126, 0.7152, 0.0722)

GAIN_STEPS = 400  # the in-polarity gain sweep, 0.02..4.0
GAIN_MAX = 4.0
GAIN_MIN = 0.02
DARKEN_STEPS = 200  # the sweep used when flipping a light fill down
LIGHTEN_VALUE_STEPS = 30  # HSV value/saturation grid used when flipping a dark fill up
LIGHTEN_SATURATION_STEPS = 30
MIN_SATURATION = 0.05

DISPOSITIONS = (
    "measured",
    "lifted",
    "lifted, short of reference",
    "POLARITY FLIP",
    "low, left measured",
    "flipped, short of reference",
    "moved clear of the separation floor",
    "lifted for contrast ratio",
    "low reference contrast, left measured",
    "NO PALETTE-LEGAL FILL",
)

# The dispositions that put a state on screen without ever reaching the target. None of
# them may pass silently: in a variant batch a large share of lockup decisions can end in one
# of these, and each must produce a warning.
SHIPS_SHORT_DISPOSITIONS = (
    "lifted, short of reference",
    "low, left measured",
    "flipped, short of reference",
    "low reference contrast, left measured",
    "no readable fill in polarity, left measured",
    "NO PALETTE-LEGAL FILL",
)

VERDICTS = ("CLEAN", "MARGINAL", "UNRESOLVED")
STATUSES = ("PASS", "WARN", "FAIL")

__all__ = [
    "InkError",
    "MAX_WEAK_INK_FRACTION",
    "MIN_CONTRAST_RATIO",
    "SHIPS_SHORT_DISPOSITIONS",
    "STATUSES",
    "TARGET_CONTRAST_RATIO",
    "VERDICTS",
    "apply_resolution",
    "contrast_ratio",
    "contrast_ratio_from_luma",
    "local_background_luma",
    "luma",
    "michelson",
    "packaged_lockups_path",
    "plate_masks",
    "prove_only_ink_moved",
    "relative_luminance",
    "render_twin",
    "resolve_ink",
]


class InkError(RuntimeError):
    pass


# --- measurement ------------------------------------------------------------------------


def luma(rgb: Sequence[float]) -> float:
    """Rec.709 luma of an RGB triple."""
    r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
    return LUMA_COEFFICIENTS[0] * r + LUMA_COEFFICIENTS[1] * g + LUMA_COEFFICIENTS[2] * b


def michelson(a: float, b: float) -> float:
    """Michelson contrast between two luminances."""
    high, low = max(a, b), min(a, b)
    return (high - low) / max(high + low, 1e-6)


def _linearise(channel: "Any") -> "Any":
    """sRGB transfer inverse, on 0..1 channel values. Vectorised."""
    import numpy as np

    value = np.asarray(channel, dtype=np.float64)
    return np.where(value <= 0.04045, value / 12.92, ((value + 0.055) / 1.055) ** 2.4)


def relative_luminance(rgb: Sequence[float]) -> float:
    """WCAG relative luminance of an sRGB triple, 0..1."""
    import numpy as np

    linear = _linearise(np.asarray(rgb, dtype=np.float64) / 255.0)
    return float(np.dot(LUMA_COEFFICIENTS, linear))


def contrast_ratio(rgb_a: Sequence[float], rgb_b: Sequence[float]) -> float:
    """WCAG contrast ratio between two sRGB triples: 1.0 to 21.0.

    Why this and not Michelson: Michelson is a ratio of sums, so it is large whenever the sum
    is small. Against a dark surround an almost-black ink wins it on a few levels of absolute
    difference — which is how a word can reach the screen at 1.37:1 with a Michelson
    of 0.415. The ratio below is the number a reader's contrast sensitivity actually follows,
    and the one WCAG sets a floor on.
    """
    left, right = relative_luminance(rgb_a), relative_luminance(rgb_b)
    high, low = max(left, right), min(left, right)
    return (high + 0.05) / (low + 0.05)


def _relative_luminance_from_luma(values: "Any") -> "Any":
    """Relative luminance of a Rec.709 luma level, read as a neutral grey. Vectorised.

    The delivery surfaces this module measures are collapsed to luma before the fill is known,
    so the ratio is computed on the equivalent grey. For a neutral surround that is exact; for
    a saturated one it is the standard approximation, and it is the same approximation the
    delivery QC makes, which is what keeps the resolver's number and the gate's number the
    same number.
    """
    import numpy as np

    return _linearise(np.clip(np.asarray(values, dtype=np.float64), 0.0, 255.0) / 255.0)


def contrast_ratio_from_luma(a: "Any", b: "Any") -> "Any":
    """WCAG contrast ratio between two Rec.709 luma levels. Scalar or array."""
    import numpy as np

    left = _relative_luminance_from_luma(a)
    right = _relative_luminance_from_luma(b)
    high = np.maximum(left, right)
    low = np.minimum(left, right)
    ratio = (high + 0.05) / (low + 0.05)
    return float(ratio) if np.isscalar(a) and np.isscalar(b) else ratio


def local_background_luma(
    picture_rgb: "Any",
    *,
    occlusion_mask: Optional["Any"] = None,
    sigma: float = LOCAL_BACKGROUND_SIGMA,
) -> "Any":
    """A per-pixel estimate of what each pixel of a picture sits ON, in Rec.709 luma.

    A single ring mean answers for a word whose left half is on sky and whose right half is on
    dark timber with a number neither half has: a word can score 74.0 levels of global
    separation with 57% of its ink under 3:1 against its own local background. This is that
    local background — a gaussian at sigma 5, which is roughly a stroke width at this cap
    height, so each stroke is compared with the picture immediately behind it.

    ``occlusion_mask`` is for the DELIVERED frame, where the caption is already burned in and
    the pixels under a stroke ARE the stroke: the envelope is inpainted first so a word cannot
    supply its own background. On the caption-free picture the resolver measures, no mask is
    needed and none is used.
    """
    import cv2
    import numpy as np

    values = (
        LUMA_COEFFICIENTS[0] * np.asarray(picture_rgb)[..., 0].astype(np.float64)
        + LUMA_COEFFICIENTS[1] * np.asarray(picture_rgb)[..., 1].astype(np.float64)
        + LUMA_COEFFICIENTS[2] * np.asarray(picture_rgb)[..., 2].astype(np.float64)
    )
    if occlusion_mask is not None:
        mask = (np.asarray(occlusion_mask) > 0).astype(np.uint8)
        if mask.any():
            values = cv2.inpaint(
                np.clip(values, 0, 255).astype(np.uint8), mask, INPAINT_RADIUS_PX, cv2.INPAINT_TELEA
            ).astype(np.float64)
    return cv2.GaussianBlur(values.astype(np.float32), (0, 0), sigmaX=float(sigma), sigmaY=float(sigma)).astype(
        np.float64
    )


def plate_masks(alpha_u8: "Any") -> Tuple["Any", "Any"]:
    """The plate's glyph core and the background ring just outside it.

    The ring is an annulus, not a box: it follows the glyph's own shape, so a word measures
    itself against the pixels it actually sits on rather than against whatever happens to fall
    inside its bounding rectangle.
    """
    import cv2
    import numpy as np

    alpha = np.asarray(alpha_u8).astype(np.float32) / 255.0
    core = alpha >= CORE_ALPHA
    if int(core.sum()) < CORE_MIN_PIXELS:
        # A stroke this thin has no interior at the higher alpha; fall back rather than
        # measure an empty core.
        core = alpha >= CORE_ALPHA_FALLBACK
    extent = (alpha >= PLATE_EXTENT_ALPHA).astype(np.uint8)
    outer = cv2.dilate(extent, np.ones((RING_OUTER_PX, RING_OUTER_PX), np.uint8)) > 0
    inner = cv2.dilate(extent, np.ones((RING_INNER_PX, RING_INNER_PX), np.uint8)) > 0
    return core, outer & ~inner


def _frame_path(directory: Path, index: int, pattern: str, origin: int = 0) -> Path:
    """Resolve a zero-based frame number onto a directory's own filename convention.

    DOCTRINE 12.4: the frame namespace is declared, never inferred. This factory's directories
    come in both conventions — the caption passes wrote `r000.png` zero-based, ffmpeg's default
    image2 sequence is `f001.png` one-based — and a silent off-by-one there moves every measured
    background by one frame. `origin` is that declaration, and it is written into the receipt.
    """
    return Path(directory) / pattern.format(index=index + origin)


def median_picture(directory: Path, frames: Sequence[int], pattern: str, origin: int = 0) -> "Any":
    """The per-pixel median RGB over the named frames.

    A median across a state's hold, not a single frame: one frame can catch a highlight that
    the eye never registers as the background of a word held for a third of a second.
    """
    import numpy as np
    from PIL import Image

    if not frames:
        raise InkError("cannot measure a picture over an empty frame list")
    stack = []
    for index in frames:
        path = _frame_path(directory, index, pattern, origin)
        if not path.is_file():
            raise InkError(f"missing picture frame {path}")
        stack.append(np.asarray(Image.open(path).convert("RGB")).astype(np.float32))
    return np.median(np.stack(stack), axis=0)


def _window(picture: "Any", box_xy: Sequence[int], size_wh: Tuple[int, int]) -> "Any":
    """The picture under one plate, at plate size, edge-padded if the plate runs off canvas."""
    import numpy as np

    x, y = int(box_xy[0]), int(box_xy[1])
    width, height = size_wh
    window = picture[y : y + height, x : x + width]
    if window.shape[:2] != (height, width):
        window = np.pad(
            window,
            ((0, max(0, height - window.shape[0])), (0, max(0, width - window.shape[1])), (0, 0)),
            mode="edge",
        )
    return window


def sample_window(window: "Any", alpha_u8: "Any") -> Tuple["Any", "Any"]:
    """Luma inside the glyph core and in its ring, for one plate over one plate-sized window."""
    core, ring = plate_masks(alpha_u8)
    values = (
        LUMA_COEFFICIENTS[0] * window[..., 0]
        + LUMA_COEFFICIENTS[1] * window[..., 1]
        + LUMA_COEFFICIENTS[2] * window[..., 2]
    )
    return values[core], values[ring]


def sample_member(
    picture: "Any",
    alpha_u8: "Any",
    box_xy: Sequence[int],
) -> Tuple["Any", "Any"]:
    """Luma inside the glyph core and in its ring, for one plate on one picture."""
    height, width = alpha_u8.shape[:2]
    return sample_window(_window(picture, box_xy, (width, height)), alpha_u8)


DELIVERY_RING_PX = 6  # the immediate surround, as the delivery QC measures it
DELIVERY_INK_ALPHA = 128  # a pixel counts as ink at this alpha, as the delivery QC counts it


def delivery_masks(alpha_u8: "Any", ring_px: int = DELIVERY_RING_PX) -> Tuple["Any", "Any"]:
    """Ink and immediate-surround masks, matching how the delivered frame is measured.

    The 17/7 annulus this module measures the *reference* with deliberately skips the glyph's
    antialiased shoulder, which is right for recovering what colour the reference's ink was. It
    is not what a reader compares: that is the pixels immediately around the stroke. The QC gate
    on the delivered encode uses this pair, so the resolver optimises this pair too — a resolver
    that maximises a number its own gate does not use is not a resolver.
    """
    import cv2
    import numpy as np

    alpha = np.asarray(alpha_u8)
    ink = alpha > DELIVERY_INK_ALPHA
    envelope = alpha > 0
    kernel = np.ones((ring_px * 2 + 1, ring_px * 2 + 1), np.uint8)
    ring = (cv2.dilate(envelope.astype(np.uint8), kernel) > 0) & ~envelope
    return ink, ring


def sample_delivery_surface(
    picture: "Any",
    alpha_u8: "Any",
    box_xy: Sequence[int],
    *,
    scale: float = 1.0,
    offset_xy: Sequence[int] = (0, 0),
    with_local_background: bool = False,
) -> Dict[str, float]:
    """What the composited caption will actually read as, on one picture, for one plate.

    Returns the three numbers the composite is made of: the picture luma the ink lands ON, the
    picture luma immediately AROUND it, and the plate's own mean alpha over its ink — because a
    plate at 85% alpha does not deliver its fill, it delivers a blend of its fill and whatever
    is behind it.
    """
    import cv2
    import numpy as np

    height, width = alpha_u8.shape[:2]
    pad = DELIVERY_RING_PX + 2
    padded_alpha = np.pad(np.asarray(alpha_u8), pad, mode="constant", constant_values=0)
    if scale == 1.0 and tuple(offset_xy) == (0, 0):
        window = _window(
            picture,
            (int(box_xy[0]) - pad, int(box_xy[1]) - pad),
            (width + 2 * pad, height + 2 * pad),
        )
    else:
        box = [
            round(int(box_xy[0]) * scale) - pad + int(offset_xy[0]),
            round(int(box_xy[1]) * scale) - pad + int(offset_xy[1]),
        ]
        raw = _window(
            picture, box, (max(1, round(width * scale)) + 2 * pad, max(1, round(height * scale)) + 2 * pad)
        )
        window = cv2.resize(raw, (width + 2 * pad, height + 2 * pad), interpolation=cv2.INTER_LINEAR)

    ink, ring = delivery_masks(padded_alpha)
    values = (
        LUMA_COEFFICIENTS[0] * window[..., 0]
        + LUMA_COEFFICIENTS[1] * window[..., 1]
        + LUMA_COEFFICIENTS[2] * window[..., 2]
    )
    if not ink.any() or not ring.any():
        raise InkError("the delivery surface has no ink or no surround to measure against")
    surface = {
        "core": float(values[ink].mean()),
        "ring": float(values[ring].mean()),
        "alpha": float(np.asarray(padded_alpha)[ink].mean() / 255.0),
    }
    if with_local_background:
        # Per-pixel, not per-word: the three arrays needed to answer "what does THIS stroke
        # read as against the picture immediately behind IT". They are numpy arrays and never
        # reach a receipt — `resolve_lockup` reduces them to two fractions.
        local = local_background_luma(window)
        surface["ink_alpha"] = np.asarray(padded_alpha)[ink].astype(np.float64) / 255.0
        surface["picture_under_ink"] = values[ink].astype(np.float64)
        surface["local_background"] = local[ink]
    return surface


def sample_member_scaled(
    picture: "Any",
    alpha_u8: "Any",
    box_xy: Sequence[int],
    *,
    scale: float,
    offset_xy: Sequence[int],
) -> Tuple["Any", "Any"]:
    """The same measurement on a canvas the caption layer was scaled onto.

    The ring is a property of the typography — an annulus a fixed number of pixels outside the
    glyph *as the contract draws it* — not of the delivery canvas. So the region of the delivery
    picture that the layer covers is resampled back to plate resolution and measured with the
    unmodified plate, rather than the plate being shrunk to the canvas. Shrinking the plate would
    shrink the annulus too, and on a small lockup it collapses to nothing.
    """
    import cv2

    height, width = alpha_u8.shape[:2]
    box = [
        round(int(box_xy[0]) * scale) + int(offset_xy[0]),
        round(int(box_xy[1]) * scale) + int(offset_xy[1]),
    ]
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    window = _window(picture, box, size)
    resampled = cv2.resize(window, (width, height), interpolation=cv2.INTER_LINEAR)
    return sample_window(resampled, alpha_u8)


def hold_frames(span: Sequence[int], count: int = HOLD_FRAMES) -> List[int]:
    """The last `count` frames of a half-open span — the state at rest, not entering."""
    start, end = int(span[0]), int(span[1])
    frames = list(range(start, end))
    if not frames:
        raise InkError(f"empty span {list(span)}")
    return frames[-count:] if len(frames) > count else frames


# --- the candidate fills ----------------------------------------------------------------


def _delivered(value: float, surface: Dict[str, float]) -> float:
    """The luma a fill of `value` actually delivers on this surface.

    A plate is not opaque. At alpha `a` over a background `core`, a fill of luma `L` reads as
    `a*L + (1-a)*core` — so on a thin script plate over a dark room, a fill 26 levels clear of
    its surround on paper delivers 14. The approved passes assumed opacity; measuring the
    composite instead is what makes the resolver's number the same number the gate reports.
    """
    alpha = surface.get("alpha", 1.0)
    if alpha >= 1.0:
        return value
    return alpha * value + (1.0 - alpha) * surface.get("core", value)


def _worst_michelson(value: float, surfaces: Sequence[Dict[str, float]]) -> float:
    """The contrast the fill achieves on the least helpful of the delivered surfaces."""
    return min(michelson(_delivered(value, surface), surface["ring"]) for surface in surfaces)


def _worst_separation(value: float, surfaces: Sequence[Dict[str, float]]) -> float:
    return min(abs(_delivered(value, surface) - surface["ring"]) for surface in surfaces)


def _worst_contrast_ratio(value: float, surfaces: Sequence[Dict[str, float]]) -> float:
    """The WCAG ratio the fill achieves on the least helpful of the delivered surfaces."""
    return min(
        float(contrast_ratio_from_luma(_delivered(value, surface), surface["ring"]))
        for surface in surfaces
    )


def _local_fractions(value: float, surface: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """(weak-ink fraction, within-floor fraction) for one fill on one surface, per pixel.

    Weak ink is ink under `MIN_CONTRAST_RATIO` against its OWN local background, not against
    the word's average surround. A word can clear every global gate with half its strokes
    invisible; this is the number that says so.
    """
    import numpy as np

    if "local_background" not in surface:
        return None
    alpha = surface["ink_alpha"]
    delivered = alpha * float(value) + (1.0 - alpha) * surface["picture_under_ink"]
    background = surface["local_background"]
    ratios = contrast_ratio_from_luma(delivered, background)
    weak = float(np.mean(np.asarray(ratios) < MIN_CONTRAST_RATIO))
    close = float(np.mean(np.abs(delivered - background) < MIN_SEPARATION))
    return weak, close


def _clears_floor(value: float, surfaces: Sequence[Dict[str, float]], want_light: bool) -> bool:
    """In polarity, and clear of the floor, on EVERY delivered surface."""
    for surface in surfaces:
        delivered = _delivered(value, surface)
        if want_light and delivered <= surface["ring"] + MIN_SEPARATION:
            return False
        if (not want_light) and delivered >= surface["ring"] - MIN_SEPARATION:
            return False
    return True


def _quantise(rgb: "Any", enabled: bool) -> "Any":
    """Test the fill that will actually ship, not the one before rounding.

    A candidate is admitted on its separation from the background, then written to the contract
    as three integers. Rounding moves luma by up to half a level, so without this a fill can be
    admitted at 25.1 and shipped at 24.9 — under the floor it was selected for clearing.
    """
    import numpy as np

    return np.round(rgb) if enabled else rgb


def _gain_candidates(
    measured: "Any", backgrounds: Sequence[float], want_light: bool, quantise: bool = False
) -> List[Dict[str, Any]]:
    """Pure-gain fills of the measured ink that clear the separation floor in polarity."""
    import numpy as np

    out: List[Dict[str, Any]] = []
    for gain in np.linspace(GAIN_MIN, GAIN_MAX, GAIN_STEPS):
        rgb = _quantise(np.clip(measured * gain, 0, 255), quantise)
        value = luma(rgb)
        if _clears_floor(value, backgrounds, want_light):
            out.append(
                {
                    "michelson": _worst_michelson(value, backgrounds),
                    "contrast_ratio": _worst_contrast_ratio(value, backgrounds),
                    "gain": float(gain),
                    "rgb": rgb,
                }
            )
    return out


def _lighten_variants(measured: "Any") -> List[Tuple["Any", float]]:
    """Raise luminance holding hue; let saturation fall only as far as the gamut requires."""
    import numpy as np

    r, g, b = [float(v) / 255.0 for v in measured]
    hue, saturation0, value0 = colorsys.rgb_to_hsv(r, g, b)
    out = []
    for value in np.linspace(value0, 1.0, LIGHTEN_VALUE_STEPS):
        for saturation in np.linspace(saturation0, MIN_SATURATION, LIGHTEN_SATURATION_STEPS):
            rgb = np.array(colorsys.hsv_to_rgb(hue, saturation, value)) * 255.0
            out.append((rgb, saturation0 - saturation))
    return out


def _flip_candidates(
    measured: "Any", backgrounds: Sequence[float], want_light: bool, quantise: bool = False
) -> List[Dict[str, Any]]:
    """Fills in the opposite polarity to the reference's own."""
    import numpy as np

    out: List[Dict[str, Any]] = []
    if want_light:
        # the reference's ink is light; the flip darkens it, by gain alone
        for gain in np.linspace(GAIN_MIN, 1.0, DARKEN_STEPS):
            rgb = _quantise(np.clip(measured * gain, 0, 255), quantise)
            value = luma(rgb)
            if _clears_floor(value, backgrounds, False):
                out.append(
                    {
                        "michelson": _worst_michelson(value, backgrounds),
                        "contrast_ratio": _worst_contrast_ratio(value, backgrounds),
                        "rgb": rgb,
                        "saturation_drop": 0.0,
                    }
                )
    else:
        for raw, drop in _lighten_variants(measured):
            rgb = _quantise(raw, quantise)
            value = luma(rgb)
            if _clears_floor(value, backgrounds, True):
                out.append(
                    {
                        "michelson": _worst_michelson(value, backgrounds),
                        "contrast_ratio": _worst_contrast_ratio(value, backgrounds),
                        "rgb": rgb,
                        "saturation_drop": float(drop),
                    }
                )
    return out


def _assess(
    surfaces: Sequence[Dict[str, Any]],
    final: Sequence[int],
    *,
    disposition: str,
    achieved: float,
    target: float,
    skipped_low_reference_contrast: bool,
) -> Dict[str, Any]:
    """Judge one fill over one set of delivered surfaces: the numbers and the verdict.

    CLEAN is a measurement that reaches the reference's own target, clears the WCAG floor and
    the separation floor on the worst frame, and leaves at most `MAX_WEAK_INK_FRACTION` of its
    ink weak against its own local background. It is never a creative approval.
    """
    final_luma = luma(final)
    contrast_after = _worst_contrast_ratio(final_luma, surfaces)
    separation_after = _worst_separation(final_luma, surfaces)
    local = [row for row in (_local_fractions(final_luma, surface) for surface in surfaces) if row is not None]
    weak_ink_fraction = max((row[0] for row in local), default=None)
    local_separation_fraction = max((row[1] for row in local), default=None)
    worst_surface = min(
        surfaces,
        key=lambda surface: float(contrast_ratio_from_luma(_delivered(final_luma, surface), surface["ring"])),
    )

    reached_target = achieved >= target - 1e-9
    ships_short = disposition in SHIPS_SHORT_DISPOSITIONS or not reached_target
    reasons: List[str] = []
    if contrast_after < MIN_CONTRAST_RATIO:
        reasons.append(f"contrast ratio {contrast_after:.2f}:1 is under the {MIN_CONTRAST_RATIO}:1 floor")
    if separation_after < MIN_SEPARATION:
        reasons.append(f"separation {separation_after:.1f} is under the {MIN_SEPARATION} floor")
    if weak_ink_fraction is not None and weak_ink_fraction > MAX_WEAK_INK_FRACTION:
        reasons.append(
            f"{weak_ink_fraction:.1%} of the ink is under {MIN_CONTRAST_RATIO}:1 against its own "
            f"local background (max {MAX_WEAK_INK_FRACTION:.0%})"
        )
    if local_separation_fraction is not None and local_separation_fraction > MAX_LOCAL_SEPARATION_FAIL_FRACTION:
        reasons.append(
            f"{local_separation_fraction:.1%} of the ink sits within {MIN_SEPARATION} luma of its own "
            f"local background (max {MAX_LOCAL_SEPARATION_FAIL_FRACTION:.0%})"
        )
    if disposition == "NO PALETTE-LEGAL FILL":
        reasons.append("no fill in either polarity clears the floor against every delivered picture")
    if reasons:
        verdict = "UNRESOLVED"
    elif ships_short or contrast_after < TARGET_CONTRAST_RATIO or skipped_low_reference_contrast:
        verdict = "MARGINAL"
        if ships_short:
            reasons.append(f"shipped short of the reference's own target ({disposition})")
        if contrast_after < TARGET_CONTRAST_RATIO:
            reasons.append(
                f"contrast ratio {contrast_after:.2f}:1 is under the {TARGET_CONTRAST_RATIO}:1 target"
            )
    else:
        verdict = "CLEAN"

    return {
        "verdict": verdict,
        "verdict_reasons": reasons,
        "contrast_ratio_after": round(contrast_after, 2),
        "separation_after": round(separation_after, 1),
        "weak_ink_fraction": None if weak_ink_fraction is None else round(weak_ink_fraction, 4),
        "local_separation_fail_fraction": (
            None if local_separation_fraction is None else round(local_separation_fraction, 4)
        ),
        "frames_measured": len({surface.get("frame") for surface in surfaces if "frame" in surface}) or None,
        "worst_frame": worst_surface.get("frame"),
        "reached_target": bool(reached_target),
        "worst_surface": worst_surface,
    }


def _per_picture_rows(
    surfaces: Sequence[Dict[str, Any]], measured: "Any", final: Sequence[int]
) -> List[Dict[str, Any]]:
    """One row per delivered picture per member, reporting that group's WORST frame.

    A span is measured on every one of its frames (DOCTRINE 15.3 forbids sampling it at one
    convenient point), which is dozens of surfaces per lockup. A receipt carrying every frame
    is unreadable, and the number that matters is the worst one, so each group is collapsed to
    its own worst case and the frame that produced it is named.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for surface in surfaces:
        groups.setdefault(str(surface.get("group") or surface["name"]), []).append(surface)

    final_luma, measured_luma = luma(final), luma(measured)
    rows: List[Dict[str, Any]] = []
    for name, members in groups.items():
        worst = min(
            members,
            key=lambda surface: float(
                contrast_ratio_from_luma(_delivered(final_luma, surface), surface["ring"])
            ),
        )
        local = [_local_fractions(final_luma, surface) for surface in members]
        measured_local = [row for row in local if row is not None]
        rows.append(
            {
                "picture": name,
                "frames_measured": len({surface["frame"] for surface in members if "frame" in surface}) or None,
                "worst_frame": worst.get("frame"),
                "background_luma": round(worst["ring"], 1),
                "plate_alpha": round(worst.get("alpha", 1.0), 3),
                "picture_under_ink_luma": round(worst["core"], 1) if "core" in worst else None,
                "delivered_ink_luma": round(_delivered(final_luma, worst), 1),
                "separation_before": round(
                    min(abs(_delivered(measured_luma, surface) - surface["ring"]) for surface in members), 1
                ),
                "separation_after": round(
                    min(abs(_delivered(final_luma, surface) - surface["ring"]) for surface in members), 1
                ),
                "michelson_after": round(
                    min(michelson(_delivered(final_luma, surface), surface["ring"]) for surface in members), 3
                ),
                "contrast_ratio_after": round(
                    min(
                        float(contrast_ratio_from_luma(_delivered(final_luma, surface), surface["ring"]))
                        for surface in members
                    ),
                    2,
                ),
                "weak_ink_fraction": (
                    round(max(row[0] for row in measured_local), 4) if measured_local else None
                ),
            }
        )
    return rows


def resolve_lockup(
    *,
    reference_core: Sequence[float],
    reference_ring: Sequence[float],
    candidate_ring: Sequence[float] = None,
    candidate_rings: Optional[Sequence[Tuple[str, Sequence[float]]]] = None,
    candidate_surfaces: Optional[Sequence[Dict[str, Any]]] = None,
    measured_rgb: Sequence[int],
    base_rgb: Optional[Sequence[int]] = None,
    base_source: str = "reference_measurement",
    strict: bool = False,
) -> Dict[str, Any]:
    """Decide one lockup's fill. This function is the rule; everything else is plumbing.

    `strict` carries three amendments to the rule as the approved passes wrote it. Each closes a
    hole those passes did not hit, because their picture happened not to expose it, and each is
    a hole a variant's picture does expose. Default False, because reproducing the approved
    pass's own answer from its own inputs is what makes this stage trustworthy at all; the
    amendments are opt-in, named, and individually tested.

    **A1 — the floor applies to the branch that moves nothing.** The entry condition was contrast
    alone (`ourM < 0.80 * refM`), so a fill clearing 80% of a *low* reference Michelson was left
    where it was however close to its background it sat. Every candidate the rule generates must
    clear 25 luma levels; the branch that generates nothing was never asked. That is how an
    approved deliverable can ship a lockup at a separation of ~15.

    **A2 — a flip must win on the absolute difference, not only on the ratio.** Michelson is a
    ratio, so against a dark background a near-black ink wins it on a tiny absolute difference:
    e.g. a flip scoring 0.71 at 29 levels while the in-polarity lift scores 0.44 at
    220. The approved pass's own warning about maximising Michelson applies to the polarity choice
    as much as to the fill choice.

    **A3 — every member of a lockup is answered for.** The decision stays per lockup, but the
    backgrounds are taken per member rather than as one median over the union: `your` and `Thing`
    are two words in two places, and a median over both rings answers for neither.

    **A4 — the number optimised is the number delivered.** The fill is measured as it composites:
    the immediate surround the delivery QC measures against, and the plate's own alpha, so a
    fill is not admitted at 26 levels on paper and shipped at 14 on screen.

    **A5 — the number is a WCAG ratio and the ink is measured per pixel.** Michelson and the
    absolute floor are both blind at the dark end; a fill can clear both at 1.37:1. Every candidate now carries its worst contrast ratio, a
    fill under `MIN_CONTRAST_RATIO` can never be CLEAN, and where the caller supplies per-pixel
    surfaces the weak-ink fraction is measured against each stroke's own local background.

    **A1 — the base is the ratified fill when one is supplied.** `base_rgb` is the approved
    contract's own `states[].ink.rgb_median`. It is still a measured, receipted fill (DOCTRINE
    rule 3.1 is not breached), and it is the one the reviewer adjudicated; starting from the
    spec's reference measurement instead resets a gold fill to its dark reference measurement
    (e.g. [95, 83, 59]) on every variant.

    A surface is `{name, ring, core, alpha}`, optionally carrying the per-pixel arrays
    `{ink_alpha, picture_under_ink, local_background}`. `candidate_rings` (annulus samples,
    opaque) is the approved pass's own form and is accepted unchanged. A reel delivered both at
    reference geometry and as a 9:16 reframe has TWO pictures behind the same word and the ink
    is a single value in a single contract, so every number reported is the worst case across
    surfaces.
    """
    import numpy as np

    if candidate_surfaces is not None:
        surfaces = [dict(surface) for surface in candidate_surfaces]
    else:
        if candidate_rings is None:
            if candidate_ring is None:
                raise InkError("resolve_lockup needs at least one candidate background")
            candidate_rings = [("candidate", candidate_ring)]
        surfaces = [
            {"name": name, "ring": float(np.median(np.asarray(values))), "alpha": 1.0}
            for name, values in candidate_rings
        ]
    if not surfaces:
        raise InkError("resolve_lockup needs at least one candidate surface")

    reference_ink = float(np.median(np.asarray(reference_core)))
    reference_background = float(np.median(np.asarray(reference_ring)))
    reference_michelson = michelson(reference_ink, reference_background)
    want_light = reference_ink >= reference_background

    backgrounds = surfaces
    background = surfaces[0]["ring"]

    reference_measured = [int(v) for v in measured_rgb]
    measured = np.asarray(reference_measured if base_rgb is None else base_rgb, dtype=float)
    before = _worst_michelson(luma(measured), backgrounds)
    separation_before = _worst_separation(luma(measured), backgrounds)
    contrast_before = _worst_contrast_ratio(luma(measured), backgrounds)
    target = TARGET_FRACTION * reference_michelson

    final = [int(v) for v in measured]
    disposition = "measured"
    achieved = before
    saturation_drop = 0.0
    best_in_polarity: Optional[Dict[str, Any]] = None
    flip_considered = False
    skipped_low_reference_contrast = False

    under_target = before < target
    under_floor = strict and separation_before < MIN_SEPARATION
    under_ratio = strict and contrast_before < MIN_CONTRAST_RATIO
    if (under_target or under_floor or under_ratio) and reference_michelson > MIN_REFERENCE_MICHELSON:
        in_polarity = _gain_candidates(measured, backgrounds, want_light, strict)
        clearing = [row for row in in_polarity if row["michelson"] >= target]
        # A5: where a candidate both reaches the target AND clears the readability floor,
        # only those are eligible. Otherwise "the smallest move that reaches the target" can
        # pick a fill that reaches a low target and is still a ghost.
        readable = [row for row in clearing if row["contrast_ratio"] >= MIN_CONTRAST_RATIO]
        if strict and readable:
            clearing = readable
        if clearing:
            # the smallest move that reaches the target: the gain nearest 1.0
            pick = min(clearing, key=lambda row: abs(row["gain"] - 1.0))
            final = [int(round(v)) for v in pick["rgb"]]
            achieved = pick["michelson"]
            if final == [int(round(v)) for v in measured]:
                # The search ran and came back with the fill it started from. That happens when
                # the base already clears the Michelson target and the separation floor and the
                # only thing wrong with it is the contrast ratio, which no in-polarity gain can
                # fix: the nearest-to-1.0 pick among the merely-Michelson-clearing fills is the
                # base itself. Calling that a lift is a receipt claiming a move that never
                # happened (e.g. a near-black base such as [18, 16, 11]).
                disposition = "no readable fill in polarity, left measured"
            else:
                disposition = (
                    "lifted" if under_target
                    else "moved clear of the separation floor" if under_floor
                    else "lifted for contrast ratio"
                )
        else:
            best_in_polarity = max(in_polarity, key=lambda row: row["michelson"]) if in_polarity else None
            if best_in_polarity is not None and best_in_polarity["michelson"] >= FLIP_SHORTFALL * target:
                final = [int(round(v)) for v in best_in_polarity["rgb"]]
                disposition = "lifted, short of reference"
                achieved = best_in_polarity["michelson"]
            else:
                flip_considered = True
                flips = _flip_candidates(measured, backgrounds, want_light, strict)
                ok = [row for row in flips if row["michelson"] >= target]
                measured_luma = luma(measured)
                if ok:
                    # least saturation sacrificed first, then the fill nearest the measured one
                    pick = min(ok, key=lambda row: (row["saturation_drop"], abs(luma(row["rgb"]) - measured_luma)))
                elif flips:
                    pick = max(flips, key=lambda row: row["michelson"])
                else:
                    pick = None
                clears = pick is not None and pick["michelson"] >= target
                beats = best_in_polarity is None or (
                    pick is not None and pick["michelson"] >= best_in_polarity["michelson"] + FLIP_MARGIN
                )
                if strict and pick is not None and best_in_polarity is not None:
                    # Michelson is a RATIO, and against a dark background a dark ink wins it on
                    # a tiny absolute difference: e.g. a flip scoring 0.71 at 29
                    # levels of separation while the in-polarity lift scores 0.44 at 220. The
                    # approved pass's own warning — "Michelson is maximised by driving ink to
                    # zero luminance against any background, so maximising it is not a legible
                    # objective" — applies to the polarity choice too. So in strict mode a flip
                    # must win on the absolute difference as well as on the ratio.
                    beats = beats and (
                        _worst_separation(luma(pick["rgb"]), backgrounds)
                        >= _worst_separation(luma(best_in_polarity["rgb"]), backgrounds)
                    )
                if clears and beats:
                    final = [int(round(v)) for v in pick["rgb"]]
                    disposition = "POLARITY FLIP"
                    achieved = pick["michelson"]
                    saturation_drop = round(float(pick["saturation_drop"]), 3)
                elif best_in_polarity is not None:
                    final = [int(round(v)) for v in best_in_polarity["rgb"]]
                    disposition = "low, left measured"
                    achieved = best_in_polarity["michelson"]
                elif pick is not None:
                    final = [int(round(v)) for v in pick["rgb"]]
                    disposition = "flipped, short of reference"
                    achieved = pick["michelson"]
                    saturation_drop = round(float(pick["saturation_drop"]), 3)
                else:
                    # Neither polarity has a fill that clears the floor against every delivered
                    # picture. This is not a fill problem and no fill can answer it: the picture
                    # behind this word straddles the palette. It is reported, never smoothed
                    # over, and it is the signal to re-cast the slot rather than to ship a word
                    # nobody can read.
                    disposition = "NO PALETTE-LEGAL FILL"
                    achieved = before
    elif under_target or under_floor or under_ratio:
        # registry TOOL-DEFECT-captions-ink-silent-noop. The guard above has no else, so a
        # reference beat carrying no contrast of its own left the fill exactly where it was and
        # said nothing at all — a silent no-op reporting success. The skip is a real rule (a
        # low-contrast beat in the reference stays a low-contrast beat here), but it is a
        # decision, and a decision that ships a state has to be visible.
        skipped_low_reference_contrast = True
        disposition = "low reference contrast, left measured"

    assessment = _assess(
        backgrounds,
        final,
        disposition=disposition,
        achieved=achieved,
        target=target,
        skipped_low_reference_contrast=skipped_low_reference_contrast,
    )
    contrast_after = assessment["contrast_ratio_after"]
    separation_after = assessment["separation_after"]
    weak_ink_fraction = assessment["weak_ink_fraction"]
    local_separation_fraction = assessment["local_separation_fail_fraction"]
    worst_surface = assessment["worst_surface"]
    reached_target = assessment["reached_target"]
    verdict = assessment["verdict"]
    reasons = assessment["verdict_reasons"]

    # Gates run PER STATE, and a lockup is a group of states: two words can be one decision
    # in two places, and one word can be one decision over a 1-frame beat and a 6-frame
    # hold. One fill still ships, but which member the fill fails on is what tells a lane
    # whether to re-cast a slot or re-window a block.
    per_member: Dict[str, Any] = {}
    for layer in sorted({str(surface["layer"]) for surface in backgrounds if "layer" in surface}):
        member_surfaces = [surface for surface in backgrounds if str(surface.get("layer")) == layer]
        member = _assess(
            member_surfaces,
            final,
            disposition=disposition,
            achieved=achieved,
            target=target,
            skipped_low_reference_contrast=skipped_low_reference_contrast,
        )
        member.pop("worst_surface", None)
        per_member[layer] = member

    return {
        "reference_ink_luma": round(reference_ink, 1),
        "reference_background_luma": round(reference_background, 1),
        "reference_michelson": round(reference_michelson, 3),
        "reference_polarity": "light_ink" if want_light else "dark_ink",
        "target_michelson": round(target, 3),
        "candidate_background_luma": round(background, 1),
        "measured_rgb": [int(v) for v in measured],
        "reference_measured_rgb": reference_measured,
        "base_rgb": [int(v) for v in measured],
        "base_source": base_source if base_rgb is not None else "reference_measurement",
        "michelson_before": round(before, 3),
        "michelson_after": round(achieved, 3),
        "separation_before": round(separation_before, 1),
        "separation_after": round(separation_after, 1),
        "contrast_ratio_before": round(contrast_before, 2),
        "contrast_ratio_after": round(contrast_after, 2),
        "contrast_ratio_floor": MIN_CONTRAST_RATIO,
        "contrast_ratio_target": TARGET_CONTRAST_RATIO,
        "weak_ink_fraction": None if weak_ink_fraction is None else round(weak_ink_fraction, 4),
        "weak_ink_fraction_max": MAX_WEAK_INK_FRACTION,
        "local_separation_fail_fraction": (
            None if local_separation_fraction is None else round(local_separation_fraction, 4)
        ),
        "frames_measured": assessment["frames_measured"],
        "worst_frame": assessment["worst_frame"],
        "worst_surface": worst_surface.get("name"),
        "verdict": verdict,
        "verdict_reasons": reasons,
        "per_member": per_member,
        "skipped_low_reference_contrast": skipped_low_reference_contrast,
        "reached_target": bool(reached_target),
        "per_picture": _per_picture_rows(surfaces, measured, final),
        "separation_floor_enforced": bool(strict),
        "entered_on": (
            " and ".join(
                name
                for name, entered in (
                    ("contrast", under_target),
                    ("separation", under_floor),
                    ("contrast ratio", under_ratio),
                )
                if entered
            )
            or "nothing — the measured fill already carries this picture"
        ),
        "saturation_drop": saturation_drop,
        "rgb": final,
        "disposition": disposition,
        "clears_separation_floor": bool(
            _clears_floor(luma(final), backgrounds, want_light)
            or _clears_floor(luma(final), backgrounds, not want_light)
        ),
        "flip_considered": flip_considered,
        "best_in_polarity_michelson": (
            round(best_in_polarity["michelson"], 3) if best_in_polarity else None
        ),
    }


# --- the stage --------------------------------------------------------------------------


def _contract_base_rgb(contract: Dict[str, Any], lockup: Dict[str, Any]) -> List[int]:
    """The ratified fill this lockup starts from, taken from the approved contract.

    A lockup is decided once and applied to every member, so every state it covers must
    already carry the same fill in the contract. If they do not, the contract is telling this
    stage two different things about one decision and there is no honest base to pick.
    """
    index = {str(state["id"]): state for state in contract.get("states", [])}
    fills: Dict[str, List[int]] = {}
    for member in lockup["members"]:
        for state_id in member["states"]:
            state = index.get(str(state_id))
            if state is None:
                raise InkError(
                    f"lockup {lockup['id']}: the approved contract has no state {state_id!r}, so it "
                    "cannot supply a base fill for it"
                )
            fills[str(state_id)] = [int(value) for value in state["ink"][_INK_KEY]]
    if not fills:
        raise InkError(f"lockup {lockup['id']} names no states, so it has no base fill")
    unique = {tuple(value) for value in fills.values()}
    if len(unique) != 1:
        raise InkError(
            f"lockup {lockup['id']} is decided once and must carry one fill, but the approved "
            f"contract gives {fills}"
        )
    return list(unique.pop())


def resolve_ink(
    spec: Dict[str, Any],
    *,
    plates_dir: Path,
    reference_frames: Path,
    candidate_frames: Path,
    reference_pattern: str = "r{index:03d}.png",
    candidate_pattern: str = "r{index:03d}.png",
    reference_index_origin: int = 0,
    candidate_index_origin: int = 0,
    strict: bool = False,
    candidate_pictures: Optional[Sequence[Dict[str, Any]]] = None,
    base_from_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve every lockup in `spec` against the picture(s) the reel is delivered in.

    `spec` is data, not code: the lockup grouping, each member's plate/box/span and the measured
    reference ink. For the packaged example it restates an approved pass's own artifacts.

    `candidate_pictures` names more than one delivered geometry when there is more than one.
    Each entry is `{name, frames, pattern, index_origin, scale, offset_xy}`; `scale`/`offset_xy`
    say where the caption layer sits on that canvas, so a 9:16 reframe is measured under the
    caption as it is actually composited there rather than under the 16:9 box. One fill has to
    carry every delivered picture, so the rule sees the worst of them.

    `base_from_contract` is the approved caption contract. When supplied, each lockup starts
    from the fill the reviewer ratified (`states[].ink.rgb_median`, which every member of the
    lockup must agree on) instead of from the spec's reference measurement. The spec's
    measurement is kept and reported as `reference_measured_rgb`. Without it, lockups
    silently revert to a pre-adjudication fill on every re-ink.
    """
    import numpy as np
    from PIL import Image

    if spec.get("artifact_type") != "caption_ink_lockups":
        raise InkError(f"not a caption ink lockup spec: {spec.get('artifact_type')!r}")
    plates_dir = Path(plates_dir)
    hold = int(spec.get("reference_hold_frames", HOLD_FRAMES))

    if candidate_pictures is None:
        candidate_pictures = [
            {
                "name": "candidate",
                "frames": candidate_frames,
                "pattern": candidate_pattern,
                "index_origin": candidate_index_origin,
            }
        ]
    pictures = [
        {
            "name": str(picture.get("name") or f"picture{index}"),
            "frames": Path(picture["frames"]),
            "pattern": picture.get("pattern", "r{index:03d}.png"),
            "index_origin": int(picture.get("index_origin", 0)),
            "scale": float(picture.get("scale", 1.0)),
            "offset_xy": [int(v) for v in picture.get("offset_xy", (0, 0))],
        }
        for index, picture in enumerate(candidate_pictures)
    ]

    lockups: Dict[str, Any] = {}
    states: Dict[str, Any] = {}
    for lockup in spec["lockups"]:
        reference_core: List["Any"] = []
        reference_ring: List["Any"] = []
        candidate_rings: Dict[str, List["Any"]] = {picture["name"]: [] for picture in pictures}
        delivery_surfaces: List[Dict[str, Any]] = []
        members = []
        for member in lockup["members"]:
            plate_path = plates_dir / member["plate"]
            if not plate_path.is_file():
                raise InkError(f"missing plate {plate_path}")
            alpha = np.asarray(Image.open(plate_path))
            if alpha.ndim == 3:
                alpha = alpha[..., -1]
            span_frames = hold_frames(member["span"], hold)
            ref_frames = list(member.get("reference_frames") or span_frames)
            # A5: the candidate is measured on EVERY frame of the span. The 4-frame hold median
            # is the reference's own convention and stays there; on our picture it hides the
            # frame the word is unreadable on, which is the frame a viewer notices.
            candidate_span = list(range(int(member["span"][0]), int(member["span"][1])))
            core, ring = sample_member(
                median_picture(
                    Path(reference_frames), ref_frames, reference_pattern, reference_index_origin
                ),
                alpha,
                member["box_xy"],
            )
            reference_core.append(core)
            reference_ring.append(ring)
            for picture in pictures:
                if strict:
                    for frame in candidate_span:
                        decoded = median_picture(
                            picture["frames"], [frame], picture["pattern"], picture["index_origin"]
                        )
                        surface = sample_delivery_surface(
                            decoded, alpha, member["box_xy"],
                            scale=picture["scale"], offset_xy=picture["offset_xy"],
                            with_local_background=True,
                        )
                        surface["name"] = f"{picture['name']}:{member['layer']}:f{frame:03d}"
                        surface["group"] = f"{picture['name']}:{member['layer']}"
                        surface["layer"] = str(member["layer"])
                        surface["frame"] = frame
                        delivery_surfaces.append(surface)
                    continue
                decoded = median_picture(
                    picture["frames"], span_frames, picture["pattern"], picture["index_origin"]
                )
                if picture["scale"] == 1.0 and picture["offset_xy"] == [0, 0]:
                    _, our_ring = sample_member(decoded, alpha, member["box_xy"])
                else:
                    _, our_ring = sample_member_scaled(
                        decoded, alpha, member["box_xy"],
                        scale=picture["scale"], offset_xy=picture["offset_xy"],
                    )
                candidate_rings[picture["name"]].append(our_ring)
            members.append(
                {
                    "layer": member["layer"],
                    "text": member.get("text"),
                    "plate": member["plate"],
                    "box_xy": [int(member["box_xy"][0]), int(member["box_xy"][1])],
                    "plate_wh": [int(alpha.shape[1]), int(alpha.shape[0])],
                    "span": list(member["span"]),
                    "reference_frames": ref_frames,
                    "candidate_frames": candidate_span if strict else span_frames,
                    "states": list(member["states"]),
                }
            )

        base_rgb = _contract_base_rgb(base_from_contract, lockup) if base_from_contract else None

        # A3/A4: one surface per member per delivered picture per frame, measured as the
        # composite reads — not one annulus median over the union. A lockup is still decided
        # once, but `your` and `Thing` are two words in two places, and a median over both
        # rings answers for neither.
        row = resolve_lockup(
            reference_core=np.concatenate(reference_core),
            reference_ring=np.concatenate(reference_ring),
            candidate_surfaces=delivery_surfaces if strict else None,
            candidate_rings=None if strict else [
                (picture["name"], np.concatenate(candidate_rings[picture["name"]]))
                for picture in pictures
            ],
            measured_rgb=lockup["measured_rgb"],
            base_rgb=base_rgb,
            base_source="approved_contract" if base_rgb is not None else "reference_measurement",
            strict=strict,
        )
        row["id"] = lockup["id"]
        row["members"] = members
        row["adjudication"] = None
        lockups[lockup["id"]] = row
        for member in members:
            member_row = row["per_member"].get(str(member["layer"]))
            for state_id in member["states"]:
                states[state_id] = {
                    "lockup": lockup["id"],
                    "layer": member["layer"],
                    "rgb": list(row["rgb"]),
                    "disposition": row["disposition"],
                    # the lockup's verdict is the worst of its members'; the per-state verdict
                    # is what says WHICH word the fill fails on, which is the difference
                    # between re-casting a slot and re-windowing a block
                    "verdict": (member_row or row)["verdict"],
                    "verdict_reasons": (member_row or row)["verdict_reasons"],
                    "contrast_ratio_after": (member_row or row)["contrast_ratio_after"],
                    "weak_ink_fraction": (member_row or row)["weak_ink_fraction"],
                    "worst_frame": (member_row or row)["worst_frame"],
                }

    verdicts = {key: row["verdict"] for key, row in lockups.items()}
    below_floor = sorted(key for key, row in lockups.items() if row["verdict"] == "UNRESOLVED")
    short_of_target = sorted(key for key, row in lockups.items() if not row["reached_target"])
    skipped = sorted(key for key, row in lockups.items() if row["skipped_low_reference_contrast"])
    if any(verdict == "UNRESOLVED" for verdict in verdicts.values()):
        status = "FAIL"
    elif any(verdict == "MARGINAL" for verdict in verdicts.values()):
        status = "WARN"
    else:
        status = "PASS"

    return {
        "schema_version": 1,
        "artifact_type": "caption_ink_resolution",
        "status": status,
        "verdicts": verdicts,
        "lockups_below_contrast_floor": below_floor,
        "lockups_short_of_target": short_of_target,
        "lockups_skipped_low_reference_contrast": skipped,
        "gates": {
            "minimum_contrast_ratio": MIN_CONTRAST_RATIO,
            "target_contrast_ratio": TARGET_CONTRAST_RATIO,
            "maximum_weak_ink_fraction": MAX_WEAK_INK_FRACTION,
            "maximum_local_separation_fail_fraction": MAX_LOCAL_SEPARATION_FAIL_FRACTION,
            "note": (
                "UNRESOLVED = the picture cannot carry this word at this fill and no fill on the "
                "ray fixes it: re-cast the slot or take a human ruling. MARGINAL = it ships "
                "short of the reference's own target or under the 4.5:1 target ratio and belongs "
                "on the card. CLEAN is a measurement, never a creative approval."
            ),
        },
        "rules": {
            "target_fraction_of_reference_michelson": TARGET_FRACTION,
            "flip_reachable_below_fraction_of_target": FLIP_SHORTFALL,
            "flip_must_beat_in_polarity_by_michelson": FLIP_MARGIN,
            "minimum_separation_luma": MIN_SEPARATION,
            "ring": f"annulus between a {RING_OUTER_PX}px and a {RING_INNER_PX}px dilation of plate alpha >= {PLATE_EXTENT_ALPHA}",
            "decided": "once per lockup, over the union of the lockup's own footprints",
            "move": "pure gain, hue held, smallest move reaching the target",
            "separation_floor_enforced_on_the_no_move_branch": bool(strict),
            "separation_floor_note": (
                "The approved passes entered the move on contrast alone, so a fill clearing 80% of a "
                "LOW reference Michelson was left in place however close to its background it sat — "
                "which is how the approved deliverable shipped `your`/`Thing` at 14.69 levels. With "
                "this flag the declared 25-level floor is enforced on that branch too."
            ),
        },
        "inputs": {
            "plates": str(plates_dir),
            "reference_frames": str(reference_frames),
            "reference_frame_namespace": f"{reference_pattern} with zero-based frame + {reference_index_origin}",
            "candidate_pictures": [
                {
                    "name": picture["name"],
                    "frames": str(picture["frames"]),
                    "frame_namespace": f"{picture['pattern']} with zero-based frame + {picture['index_origin']}",
                    "caption_layer_scale": picture["scale"],
                    "caption_layer_offset_xy": picture["offset_xy"],
                }
                for picture in pictures
            ],
        },
        "lockups": lockups,
        "states": states,
        "creative_approval": "PENDING",
        "scope": (
            "A resolved fill is a measurement that scopes the choice. The approved passes both "
            "adjudicated their flips on a native-scale board over their own picture, and both "
            "receipts say the board is the decision, not the number. This stage never self-approves."
        ),
    }


# --- applying it to a contract ----------------------------------------------------------

_INK_KEY = "rgb_median"


def apply_resolution(
    contract: Dict[str, Any],
    resolution_states: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return a copy of `contract` with resolved ink, plus the diff. Nothing else moves."""
    patched = copy.deepcopy(contract)
    known = {state["id"] for state in patched["states"]}
    unknown = sorted(set(resolution_states) - known)
    if unknown:
        raise InkError(f"resolution names states this contract does not have: {unknown}")
    diff: Dict[str, Any] = {}
    for state in patched["states"]:
        row = resolution_states.get(state["id"])
        if row is None:
            continue
        rgb = [int(v) for v in row["rgb"]]
        before = list(state["ink"][_INK_KEY])
        if before != rgb:
            diff[state["id"]] = {"from": before, "to": rgb}
        state["ink"][_INK_KEY] = rgb
    return patched, diff


def prove_only_ink_moved(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Walk both contracts field by field; refuse if anything but ink RGB differs.

    The approved v002 caption pass proved its own edit this way — "the complete diff, provenance
    excluded, is six numeric fields" — and the same proof is what makes a per-variant ink
    re-resolution reviewable rather than a re-authored caption program.
    """
    moved: Dict[str, Any] = {}
    left = copy.deepcopy(before)
    right = copy.deepcopy(after)
    if [s["id"] for s in left["states"]] != [s["id"] for s in right["states"]]:
        raise InkError("the state list itself changed; this is not an ink-only patch")
    for a, b in zip(left["states"], right["states"]):
        ink_a, ink_b = a.pop("ink"), b.pop("ink")
        if list(ink_a[_INK_KEY]) != list(ink_b[_INK_KEY]):
            moved[a["id"]] = {"from": list(ink_a[_INK_KEY]), "to": list(ink_b[_INK_KEY])}
        ink_a.pop(_INK_KEY, None)
        ink_b.pop(_INK_KEY, None)
        if ink_a != ink_b:
            raise InkError(f"state {a['id']}: ink evidence changed, not only the fill")
        if a != b:
            differing = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
            raise InkError(f"state {a['id']}: fields other than ink changed: {differing}")
    for key in set(left) | set(right):
        if key == "states":
            continue
        if left.get(key) != right.get(key):
            raise InkError(f"contract field {key!r} changed; this is not an ink-only patch")
    return moved


def render_twin(contract: Dict[str, Any]) -> Dict[str, Any]:
    """The BGR twin the renderer consumes.

    `render.py` writes `rgb_median[0]` into array channel 0 and the CLI saves with
    `cv2.imwrite`, which is BGRA. The approved passes therefore keep two contracts: one
    carrying true RGB (the truth, and what QC compares against) and one whose every ink triple
    is reversed (what the engine is handed). This is that reversal, and it is its own inverse.
    """
    twin = copy.deepcopy(contract)
    for state in twin["states"]:
        state["ink"][_INK_KEY] = list(reversed([int(v) for v in state["ink"][_INK_KEY]]))
    return twin


def load_spec(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text())


def packaged_lockups_path(name: str = "refA-ink-lockups.json") -> Path:
    """A lockup spec that ships with the engine.

    `refA-ink-lockups.json` is a small synthetic example spec shipped in the package's own
    `data/` directory alongside the colour profiles and LUTs; a copy lives in
    `tests/captions/fixtures/` for the test-suite. Real lockup specs are project data.
    """
    return Path(__file__).resolve().parent.parent / "data" / name
