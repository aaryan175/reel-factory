"""Temporal stability must survive a minority of anomalous frames.

`segment_temporally_static_ink` combines two signals: bright in the per-pixel median, and
stable across the span. The brightness half is already robust — a median over the span
tolerates a minority of outlier frames by construction. The stability half was not: it used
the full temporal range (max - min), which any single outlier frame drives to ~255 for every
pixel at once, collapsing the whole mask to empty.

The motivating failure is real, not hypothetical: a final state that is a static end-card
spanning frames [71,238). Frame 72 is a flash
transition: the frame blows out to white and the caption inverts to black. Sampling frames
71-75 therefore contains exactly one anomalous frame, and the recovered mask was empty —
recorded downstream as `clock_ink_present: false` with a null bbox for 70% of the reel, while
the caption is plainly legible on every one of those frames (~4030 px above threshold 200 at
frame 71, and `f71 & f73 = 4020` shared pixels versus `f71 & f72 = 0`).

Flashes, whip pans and exposure pops inside a caption's own span are ordinary reference
grammar, so an all-or-nothing stability test is the wrong instrument. The gate must still
reject genuine motion, which is what the second test pins down.
"""

from __future__ import annotations

import numpy as np
import pytest

from reelctl.captions.extract import (
    ExtractError,
    segment_temporally_static_ink,
    union_bbox,
)

SHAPE = (240, 400, 3)
ROI = (40, 40, 360, 120)
TEXT_BOX = (100, 60, 260, 100)  # x0, y0, x1, y1


def _footage(seed: int) -> np.ndarray:
    """Dark moving footage — different every frame, never bright enough to read as ink."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 90, size=SHAPE, dtype=np.uint16).astype(np.uint8)


def _with_text(frame: np.ndarray, value: int = 250) -> np.ndarray:
    out = frame.copy()
    x0, y0, x1, y1 = TEXT_BOX
    out[y0:y1, x0:x1] = value
    return out


def _span_with_flash_at(index: int, length: int = 5) -> list[np.ndarray]:
    """A static caption over moving footage, with one blown-out flash frame."""
    frames = [_with_text(_footage(seed)) for seed in range(length)]
    flash = np.full(SHAPE, 255, dtype=np.uint8)
    x0, y0, x1, y1 = TEXT_BOX
    flash[y0:y1, x0:x1] = 10  # caption inverts to dark on the blown frame
    frames[index] = flash
    return frames


def test_a_single_flash_frame_does_not_erase_the_caption() -> None:
    """The static end-card regression: one flash frame must not empty the mask."""
    mask = segment_temporally_static_ink(_span_with_flash_at(1), roi_xyxy=ROI)
    assert int((mask > 0).sum()) > 0, "one anomalous frame collapsed the whole mask"

    box = union_bbox(mask)
    assert box is not None
    x0, y0, x1, y1 = TEXT_BOX
    assert abs(box[0] - x0) <= 2 and abs(box[1] - y0) <= 2
    assert abs(box[2] - x1) <= 2 and abs(box[3] - y1) <= 2


def test_the_flash_may_fall_anywhere_in_the_span() -> None:
    """Robustness must not depend on which frame is the outlier."""
    for index in range(5):
        mask = segment_temporally_static_ink(_span_with_flash_at(index), roi_xyxy=ROI)
        assert int((mask > 0).sum()) > 0, f"flash at index {index} emptied the mask"


def test_a_clean_span_still_recovers_the_caption() -> None:
    frames = [_with_text(_footage(seed)) for seed in range(5)]
    mask = segment_temporally_static_ink(frames, roi_xyxy=ROI)
    assert int((mask > 0).sum()) > 0


def test_genuine_motion_is_still_rejected() -> None:
    """The gate must not become a brightness detector: a moving bright block is not ink."""
    frames = []
    for step in range(5):
        frame = _footage(100 + step)
        frame[60:100, 100 + step * 25 : 160 + step * 25] = 250
        frames.append(frame)
    mask = segment_temporally_static_ink(frames, roi_xyxy=ROI)
    assert int((mask > 0).sum()) == 0, "a block that moves every frame must not read as ink"


def test_a_majority_of_anomalous_frames_is_not_tolerated() -> None:
    """Robust to a minority, not to a span that is mostly anomalous."""
    frames = _span_with_flash_at(1)
    frames[3] = frames[1].copy()
    frames[4] = frames[1].copy()  # 3 of 5 frames blown
    mask = segment_temporally_static_ink(frames, roi_xyxy=ROI)
    assert int((mask > 0).sum()) == 0


def test_two_frames_still_works_and_is_strict() -> None:
    """With only two frames there is no majority to appeal to; stay strict."""
    frames = [_with_text(_footage(7)), _with_text(_footage(8))]
    assert int((segment_temporally_static_ink(frames, roi_xyxy=ROI) > 0).sum()) > 0

    with pytest.raises(ExtractError, match="two frames"):
        segment_temporally_static_ink([_with_text(_footage(7))], roi_xyxy=ROI)
