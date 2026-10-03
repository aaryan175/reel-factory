"""Colour-agnostic static ink detection.

``segment_temporally_static_ink`` gates on brightness: ``min(B,G,R) > threshold``. That
misses part of a reference whose sealed treatment contract says the ink is
"mostly near-white, but several states visibly carry cyan/pink/cream/navy or scene-reactive
internal color". On such a reference a brightness gate returns no ink at all for some states and too
little for others — exactly the pattern a brightness gate produces on coloured or dark ink.

DOCTRINE rule 3.4's "no default operator" applies to detection too: do not assume white ink.

The colour-agnostic signal is *local distinctness* — a glyph differs from its own
neighbourhood, whatever colour either happens to be. Combined with temporal stability it also
rejects a large flat bright region, which has no local contrast and which the brightness gate
wrongly accepted.
"""

from __future__ import annotations

import numpy as np
import pytest

from reelctl.captions.extract import (
    ExtractError,
    segment_static_distinct_ink,
    union_bbox,
)


def _moving(index: int, base: int = 175) -> np.ndarray:
    frame = np.zeros((160, 260, 3), dtype=np.uint8)
    column = (np.arange(260) + index * 21) % 260
    for channel in range(3):
        frame[:, :, channel] = base + (column % 40)
    return frame


def _text(frame: np.ndarray, box, colour) -> np.ndarray:
    """Stroke-shaped text so the mask has a realistic fill ratio."""
    out = frame.copy()
    x0, y0, x1, y1 = box
    stroke = 6
    for x in range(x0, x1, 28):
        out[y0:y1, x : x + stroke] = (colour[2], colour[1], colour[0])
    out[y0 : y0 + stroke, x0:x1] = (colour[2], colour[1], colour[0])
    return out


def test_near_white_ink_is_found() -> None:
    box = (70, 50, 190, 90)
    frames = [_text(_moving(i, base=60), box, (250, 250, 250)) for i in range(6)]
    mask = segment_static_distinct_ink(frames, roi_xyxy=(0, 0, 260, 160))
    found = union_bbox(mask)
    assert found is not None
    assert found[0] >= box[0] - 2 and found[2] <= box[2] + 2


def test_dark_ink_on_a_bright_plate_is_found() -> None:
    """A brightness gate cannot see this at all."""
    box = (70, 50, 190, 90)
    frames = [_text(_moving(i, base=200), box, (18, 20, 22)) for i in range(6)]
    mask = segment_static_distinct_ink(frames, roi_xyxy=(0, 0, 260, 160))
    assert union_bbox(mask) is not None


def test_cyan_ink_is_found() -> None:
    """The sealed treatment contract records cyan/pink/cream/navy states."""
    box = (70, 50, 190, 90)
    frames = [_text(_moving(i, base=90), box, (40, 230, 230)) for i in range(6)]
    mask = segment_static_distinct_ink(frames, roi_xyxy=(0, 0, 260, 160))
    assert union_bbox(mask) is not None


def test_moving_footage_alone_yields_no_ink() -> None:
    frames = [_moving(i) for i in range(6)]
    mask = segment_static_distinct_ink(frames, roi_xyxy=(0, 0, 260, 160))
    assert union_bbox(mask) is None


def test_a_large_flat_bright_region_is_rejected() -> None:
    """It is static and bright, so a brightness gate accepts it. It has no local contrast."""
    frames = []
    for index in range(6):
        frame = _moving(index, base=60)
        frame[20:140, 30:230] = 250  # a big flat bright slab
        frames.append(frame)
    mask = segment_static_distinct_ink(frames, roi_xyxy=(0, 0, 260, 160))
    # The slab's own edge legitimately differs from its neighbourhood, so the bounding box can
    # still span the slab. The property that matters is that the INTERIOR is empty: the ink
    # count must be a thin border, not a filled region.
    slab_area = (230 - 30) * (140 - 20)
    assert int((mask > 0).sum()) < 0.2 * slab_area
    interior = mask[40:120, 50:210]
    assert int((interior > 0).sum()) == 0


def test_needs_at_least_two_frames() -> None:
    with pytest.raises(ExtractError, match="two|frames"):
        segment_static_distinct_ink([_moving(0)], roi_xyxy=(0, 0, 260, 160))


def test_local_contrast_threshold_is_explicit() -> None:
    box = (70, 50, 190, 90)
    frames = [_text(_moving(i, base=120), box, (150, 150, 150)) for i in range(6)]
    strict = segment_static_distinct_ink(
        frames, roi_xyxy=(0, 0, 260, 160), min_local_contrast=120
    )
    loose = segment_static_distinct_ink(
        frames, roi_xyxy=(0, 0, 260, 160), min_local_contrast=10
    )
    assert union_bbox(strict) is None
    assert union_bbox(loose) is not None


def test_roi_is_respected() -> None:
    frames = [_text(_moving(i, base=60), (10, 10, 60, 40), (250, 250, 250)) for i in range(6)]
    frames = [_text(f, (150, 100, 240, 140), (250, 250, 250)) for f in frames]
    mask = segment_static_distinct_ink(frames, roi_xyxy=(140, 90, 260, 160))
    found = union_bbox(mask)
    assert found is not None
    assert found[0] >= 140 and found[1] >= 90
