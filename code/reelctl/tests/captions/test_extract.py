"""Pixel measurement for the EXTRACT stage.

These are the three segmentation models the corpus actually proved, generalised out of the
per-reel reference-intake scripts where they existed as hand-tuned ROIs
and magic numbers per word:

* ``min_channel_threshold``  - ``min(B,G,R) > t``, for white/near-white ink
* ``chromatic_difference``   - channel-difference predicates, for coloured ink
* ``temporal_difference``    - median(state frames) vs median(adjacent blank frames),
                               for static ink over moving footage

The third is the non-circular workhorse: it uses only the reference's own neighbouring
text-free frames, so no font, palette or renderer constant enters the measurement.

Synthetic fixtures throughout — the real-reference run is a receipt, not a unit test.
"""

from __future__ import annotations

import numpy as np
import pytest

from reelctl.captions.extract import (
    MIN_GEOMETRIC_RIDGE_WIDTH,
    ExtractError,
    clean_components,
    core_and_treatment_bbox,
    detect_state_span,
    dominant_text_line,
    filter_text_like_components,
    measure_ink,
    restrict_to_box,
    segment_chromatic_difference,
    segment_min_channel,
    segment_temporal_difference,
    segment_temporally_static_ink,
    union_bbox,
)

WHITE = (250, 252, 251)


def _blank(height: int = 120, width: int = 200, value: int = 30) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


def _with_bar(
    frame: np.ndarray,
    box: tuple[int, int, int, int],
    colour: tuple[int, int, int] = WHITE,
) -> np.ndarray:
    out = frame.copy()
    x0, y0, x1, y1 = box
    # OpenCV order is BGR.
    out[y0:y1, x0:x1] = (colour[2], colour[1], colour[0])
    return out


# --- component cleaning ------------------------------------------------------


def test_clean_components_drops_specks_below_min_area() -> None:
    mask = np.zeros((40, 40), dtype=np.uint8)
    mask[10:20, 10:20] = 255  # 100 px blob
    mask[35, 35] = 255  # 1 px speck
    cleaned = clean_components(mask, min_area=4)
    assert cleaned[15, 15] == 255
    assert cleaned[35, 35] == 0


def test_clean_components_keeps_every_blob_at_or_above_min_area() -> None:
    mask = np.zeros((40, 40), dtype=np.uint8)
    mask[0:2, 0:2] = 255  # exactly 4 px
    cleaned = clean_components(mask, min_area=4)
    assert cleaned.sum() == 4 * 255


def test_union_bbox_spans_every_surviving_component() -> None:
    mask = np.zeros((60, 60), dtype=np.uint8)
    mask[10:15, 5:10] = 255
    mask[40:45, 50:55] = 255
    assert union_bbox(mask) == (5, 10, 55, 45)


def test_union_bbox_of_an_empty_mask_is_none() -> None:
    assert union_bbox(np.zeros((10, 10), dtype=np.uint8)) is None


# --- min-channel segmentation ------------------------------------------------


def test_min_channel_recovers_white_ink_and_its_exact_box() -> None:
    frame = _with_bar(_blank(), (40, 50, 120, 70))
    mask = segment_min_channel(frame, roi_xyxy=(0, 0, 200, 120), threshold=220)
    assert union_bbox(mask) == (40, 50, 120, 70)


def test_min_channel_uses_the_minimum_channel_not_luma() -> None:
    """A saturated bright colour has high luma but a low minimum channel."""
    frame = _with_bar(_blank(), (40, 50, 120, 70), colour=(255, 240, 10))
    mask = segment_min_channel(frame, roi_xyxy=(0, 0, 200, 120), threshold=220)
    assert union_bbox(mask) is None


def test_min_channel_respects_the_roi() -> None:
    frame = _with_bar(_blank(), (10, 10, 30, 30))
    frame = _with_bar(frame, (150, 90, 190, 110))
    mask = segment_min_channel(frame, roi_xyxy=(140, 80, 200, 120), threshold=220)
    assert union_bbox(mask) == (150, 90, 190, 110)


def test_roi_outside_the_frame_is_rejected() -> None:
    with pytest.raises(ExtractError, match="roi|bounds"):
        segment_min_channel(_blank(), roi_xyxy=(0, 0, 500, 500), threshold=220)


# --- chromatic segmentation --------------------------------------------------


def test_chromatic_difference_recovers_burgundy_ink_over_orange_ground() -> None:
    """A burgundy word: suppressed green, red-minus-green gap."""
    ground = np.zeros((120, 200, 3), dtype=np.uint8)
    ground[:, :] = (40, 120, 220)  # BGR: orange ground
    frame = _with_bar(ground, (60, 40, 140, 80), colour=(125, 13, 34))
    mask = segment_chromatic_difference(
        frame,
        roi_xyxy=(0, 0, 200, 120),
        red_range=(45, 175),
        green_max=60,
        blue_range=(18, 110),
        red_minus_green_min=35,
    )
    assert union_bbox(mask) == (60, 40, 140, 80)


# --- temporal-difference segmentation ---------------------------------------


def _moving_background(index: int) -> np.ndarray:
    """A bright field whose content moves frame to frame, as real footage does."""
    frame = np.zeros((120, 200, 3), dtype=np.uint8)
    column = (np.arange(200) + index * 17) % 200
    frame[:, :, 0] = 170 + (column % 40)
    frame[:, :, 1] = 175 + (column % 40)
    frame[:, :, 2] = 180 + (column % 40)
    return frame


def test_temporal_difference_recovers_dark_static_text_over_moving_footage() -> None:
    box = (70, 40, 150, 75)
    state_frames = [_with_bar(_moving_background(i), box, colour=(20, 22, 24)) for i in range(6)]
    blank_frames = [_moving_background(i) for i in range(6, 14)]
    mask = segment_temporal_difference(
        state_frames,
        blank_frames,
        roi_xyxy=(0, 0, 200, 120),
        min_delta=24,
    )
    assert union_bbox(mask) == box


def test_temporal_difference_finds_nothing_when_no_text_is_present() -> None:
    state_frames = [_moving_background(i) for i in range(6)]
    blank_frames = [_moving_background(i) for i in range(6, 14)]
    mask = segment_temporal_difference(
        state_frames, blank_frames, roi_xyxy=(0, 0, 200, 120), min_delta=24
    )
    assert union_bbox(mask) is None


def test_temporal_difference_needs_blank_frames() -> None:
    with pytest.raises(ExtractError, match="blank"):
        segment_temporal_difference(
            [_moving_background(0)], [], roi_xyxy=(0, 0, 200, 120), min_delta=24
        )


# --- two placement boxes -----------------------------------------------------


def test_core_and_treatment_boxes_separate_ink_from_its_glow() -> None:
    """Rule 4.5: the core 50%-alpha box and the low-alpha treatment extent."""
    alpha = np.zeros((120, 200), dtype=np.uint8)
    alpha[50:70, 60:140] = 255  # solid core
    alpha[46:74, 56:144] = np.maximum(alpha[46:74, 56:144], 40)  # low-alpha halo
    core, treatment = core_and_treatment_bbox(alpha)
    assert core == (60, 50, 140, 70)
    assert treatment == (56, 46, 144, 74)


def test_untreated_ink_yields_identical_boxes() -> None:
    alpha = np.zeros((60, 60), dtype=np.uint8)
    alpha[20:30, 20:40] = 255
    core, treatment = core_and_treatment_bbox(alpha)
    assert core == treatment == (20, 20, 40, 30)


def test_treatment_box_always_contains_the_core_box() -> None:
    rng = np.random.default_rng(20250113)
    for _ in range(20):
        alpha = np.zeros((80, 80), dtype=np.uint8)
        x0, y0 = rng.integers(5, 30, size=2)
        x1, y1 = x0 + rng.integers(5, 20), y0 + rng.integers(5, 20)
        alpha[y0:y1, x0:x1] = 255
        halo = rng.integers(1, 4)
        alpha[
            max(0, y0 - halo) : y1 + halo, max(0, x0 - halo) : x1 + halo
        ] = np.maximum(
            alpha[max(0, y0 - halo) : y1 + halo, max(0, x0 - halo) : x1 + halo], 30
        )
        core, treatment = core_and_treatment_bbox(alpha)
        assert treatment[0] <= core[0] and treatment[1] <= core[1]
        assert treatment[2] >= core[2] and treatment[3] >= core[3]


# --- ink sampling ------------------------------------------------------------


def test_measure_ink_samples_interior_pixels_from_the_reference() -> None:
    frame = _with_bar(_blank(), (60, 40, 140, 80), colour=(247, 249, 251))
    mask = segment_min_channel(frame, roi_xyxy=(0, 0, 200, 120), threshold=220)
    ink = measure_ink(frame, mask)
    assert ink.rgb_median == (247, 249, 251)
    assert ink.interior_pixels > 0


def test_measure_ink_erodes_so_antialiased_edges_do_not_pollute_the_median() -> None:
    """An edge ring of half-lit pixels must not drag the measured ink toward the ground."""
    frame = _blank(value=0)
    frame[40:80, 60:140] = (251, 249, 247)  # BGR solid interior
    frame[38:40, 58:142] = (125, 124, 123)  # dim edge ring above
    frame[80:82, 58:142] = (125, 124, 123)
    mask = np.zeros((120, 200), dtype=np.uint8)
    mask[38:82, 58:142] = 255
    ink = measure_ink(frame, mask, erode_px=2)
    assert ink.rgb_median == (247, 249, 251)


def test_measure_ink_refuses_an_empty_mask() -> None:
    with pytest.raises(ExtractError, match="empty|mask"):
        measure_ink(_blank(), np.zeros((120, 200), dtype=np.uint8))


def test_measure_ink_reports_the_method_free_percentiles() -> None:
    frame = _with_bar(_blank(), (60, 40, 140, 80), colour=(240, 250, 255))
    mask = segment_min_channel(frame, roi_xyxy=(0, 0, 200, 120), threshold=220)
    ink = measure_ink(frame, mask)
    assert ink.rgb_p05 <= ink.rgb_median <= ink.rgb_p95


# --- state span detection ----------------------------------------------------


def test_detect_state_span_returns_a_half_open_interval() -> None:
    frames = []
    for index in range(12):
        frame = _blank()
        if 3 <= index < 8:
            frame = _with_bar(frame, (60, 40, 140, 80))
        frames.append(frame)
    assert detect_state_span(frames, roi_xyxy=(0, 0, 200, 120), threshold=220) == (3, 8)


def test_detect_state_span_finds_a_single_frame_blink() -> None:
    frames = [_blank() for _ in range(6)]
    frames[4] = _with_bar(frames[4], (60, 40, 140, 80))
    assert detect_state_span(frames, roi_xyxy=(0, 0, 200, 120), threshold=220) == (4, 5)


def test_detect_state_span_is_none_when_the_state_never_appears() -> None:
    frames = [_blank() for _ in range(6)]
    assert detect_state_span(frames, roi_xyxy=(0, 0, 200, 120), threshold=220) is None


def test_detect_state_span_ignores_sub_threshold_specks() -> None:
    frames = [_blank() for _ in range(6)]
    frames[2][10, 10] = (255, 255, 255)
    assert detect_state_span(
        frames, roi_xyxy=(0, 0, 200, 120), threshold=220, min_ink_pixels=8
    ) is None


# --- guards the real-reference run exposed -----------------------------------
#
# Running the extractor against real authority pixels surfaced three ways a
# naive measurement silently returns garbage instead of failing. Each is encoded here.


def test_temporal_difference_refuses_blank_frames_from_a_different_scene() -> None:
    """The blank frames must be same-shot neighbours, not any caption-free frames.

    A reference can have only two blank frames that sit in a different shot from most
    states. Differencing across a shot change makes the whole ROI 'ink', which must fail
    closed rather than return a full-frame box.
    """
    box = (70, 40, 150, 75)
    state_frames = [_with_bar(_moving_background(i), box, colour=(20, 22, 24)) for i in range(4)]
    # A different scene entirely: dark where the state's shot was bright.
    other_scene = [np.full((120, 200, 3), 12, dtype=np.uint8) for _ in range(4)]
    with pytest.raises(ExtractError, match="scene|fraction|too much"):
        segment_temporal_difference(
            state_frames, other_scene, roi_xyxy=(0, 0, 200, 120), min_delta=24
        )


def test_temporal_difference_ink_fraction_guard_is_tunable() -> None:
    box = (10, 10, 190, 110)  # deliberately covers most of the ROI
    state_frames = [_with_bar(_moving_background(i), box, colour=(20, 22, 24)) for i in range(4)]
    blank_frames = [_moving_background(i) for i in range(4, 10)]
    with pytest.raises(ExtractError, match="scene|fraction|too much"):
        segment_temporal_difference(
            state_frames, blank_frames, roi_xyxy=(0, 0, 200, 120), min_delta=24
        )
    mask = segment_temporal_difference(
        state_frames,
        blank_frames,
        roi_xyxy=(0, 0, 200, 120),
        min_delta=24,
        max_ink_fraction=0.95,
    )
    assert union_bbox(mask) == box


def test_treatment_box_is_bounded_to_a_dilation_of_the_core() -> None:
    """Bright footage far from the glyph is not that glyph's glow.

    Measured on the real authority, an unbounded low-alpha sweep returned
    (50, 496, 1048, 1078) for a state whose core ink box was (869, 497, 1047, 583).
    """
    alpha = np.zeros((300, 400), dtype=np.uint8)
    alpha[100:140, 150:250] = 255  # core ink
    alpha[96:144, 146:254] = np.maximum(alpha[96:144, 146:254], 40)  # real glow
    alpha[280:290, 10:30] = 40  # unrelated bright footage far away
    core, treatment = core_and_treatment_bbox(alpha, max_treatment_dilation_px=8)
    assert core == (150, 100, 250, 140)
    assert treatment == (146, 96, 254, 144)


def test_unbounded_dilation_is_still_available_when_asked_for() -> None:
    alpha = np.zeros((300, 400), dtype=np.uint8)
    alpha[100:140, 150:250] = 255
    alpha[280:290, 10:30] = 40
    _, treatment = core_and_treatment_bbox(alpha, max_treatment_dilation_px=None)
    assert treatment == (10, 100, 250, 290)


def test_restrict_to_box_confines_a_mask_to_one_state() -> None:
    """Concurrent states share a frame; ink must be attributed to one box at a time."""
    mask = np.zeros((120, 200), dtype=np.uint8)
    mask[20:40, 20:60] = 255  # state A
    mask[80:100, 120:180] = 255  # state B
    confined = restrict_to_box(mask, (120, 80, 180, 100))
    assert union_bbox(confined) == (120, 80, 180, 100)


def test_ink_measured_through_a_restricted_mask_is_attributed_to_one_state() -> None:
    """A concurrent neighbouring state's ink must not be mixed into this state's colour.

    The median is deliberately robust, so a small pollutant does not move it - which is why
    the failure mode is quiet. Here the neighbour is larger than the target, so an
    unrestricted mask reports the neighbour's ink as though it were the target's.
    """
    frame = _blank(value=0)
    frame[40:80, 60:140] = (251, 249, 247)  # target state: near-white
    frame[20:100, 150:200] = (60, 62, 64)  # a different, larger concurrent state: dark
    mask = np.zeros((120, 200), dtype=np.uint8)
    mask[40:80, 60:140] = 255
    mask[20:100, 150:200] = 255

    both = measure_ink(frame, mask, erode_px=1)
    target_only = measure_ink(frame, restrict_to_box(mask, (60, 40, 140, 80)), erode_px=1)

    assert target_only.rgb_median == (247, 249, 251)
    assert target_only.interior_pixels < both.interior_pixels
    assert both.rgb_median != target_only.rgb_median


# --- temporally-static ink ----------------------------------------------------
#
# Re-deriving an approved composited output exposes the need for this. A caption composited over
# bright footage cannot be isolated by brightness alone: full-frame min-channel over the
# approved 1920x1080 review MP4 matched only 1 of 12 states, because bright footage reads as
# ink. But a caption is *static* across its own state span while the footage moves, and the
# approved output contains both signals. No blank frames are needed, which matters because a
# composited reel rarely has any.


def test_static_bright_ink_is_recovered_from_moving_bright_footage() -> None:
    box = (70, 40, 150, 75)
    frames = [_with_bar(_moving_background(i), box, colour=(252, 250, 248)) for i in range(6)]
    mask = segment_temporally_static_ink(
        frames, roi_xyxy=(0, 0, 200, 120), threshold=200, max_temporal_delta=6
    )
    assert union_bbox(mask) == box


def test_moving_bright_footage_alone_yields_no_ink() -> None:
    frames = [_moving_background(i) for i in range(6)]
    mask = segment_temporally_static_ink(
        frames, roi_xyxy=(0, 0, 200, 120), threshold=200, max_temporal_delta=6
    )
    assert union_bbox(mask) is None


def test_static_bright_footage_is_not_distinguishable_and_is_reported() -> None:
    """Honest limit: a static bright region is indistinguishable from static ink."""
    frames = [np.full((120, 200, 3), 250, dtype=np.uint8) for _ in range(4)]
    mask = segment_temporally_static_ink(
        frames, roi_xyxy=(0, 0, 200, 120), threshold=200, max_temporal_delta=6
    )
    # It does find "ink" - the whole ROI - which the caller must treat as unusable.
    assert union_bbox(mask) == (0, 0, 200, 120)


def test_static_ink_needs_at_least_two_frames() -> None:
    with pytest.raises(ExtractError, match="two|frames"):
        segment_temporally_static_ink(
            [_moving_background(0)], roi_xyxy=(0, 0, 200, 120), threshold=200
        )


def test_static_ink_tolerates_codec_jitter_on_the_caption() -> None:
    """The same overlay re-encodes with small per-frame variation; that is not motion."""
    box = (70, 40, 150, 75)
    frames = []
    for index in range(6):
        frame = _with_bar(_moving_background(index), box, colour=(252, 250, 248))
        jitter = (index % 3) - 1
        frame[40:75, 70:150] = np.clip(
            frame[40:75, 70:150].astype(np.int16) + jitter, 0, 255
        ).astype(np.uint8)
        frames.append(frame)
    mask = segment_temporally_static_ink(
        frames, roi_xyxy=(0, 0, 200, 120), threshold=200, max_temporal_delta=6
    )
    assert union_bbox(mask) == box


# --- text-like component prior ------------------------------------------------
#
# Re-deriving an approved composited output shows that brightness plus temporal
# stability still admits large static bright footage regions (sky, walls). A caption is text:
# many small components, none of them occupying a big share of the frame. Filtering by that
# is a general prior on what glyphs are, not a per-reel ROI.


def test_text_like_filter_drops_a_large_static_region() -> None:
    mask = np.zeros((1080, 1920), dtype=np.uint8)
    mask[100:700, 200:1400] = 255  # a huge bright region: 720k px, 34% of frame
    mask[900:960, 1600:1640] = 255  # a glyph-sized component, clear of that region
    filtered = filter_text_like_components(mask, max_area_fraction=0.02)
    assert union_bbox(filtered) == (1600, 900, 1640, 960)


def test_text_like_filter_keeps_every_glyph_of_a_word() -> None:
    mask = np.zeros((1080, 1920), dtype=np.uint8)
    for index in range(6):
        x = 800 + index * 45
        mask[520:580, x : x + 40] = 255
    filtered = filter_text_like_components(mask, max_area_fraction=0.02)
    assert union_bbox(filtered) == (800, 520, 1065, 580)


def test_text_like_filter_returns_empty_when_nothing_is_text_like() -> None:
    mask = np.zeros((1080, 1920), dtype=np.uint8)
    mask[100:900, 200:1700] = 255
    filtered = filter_text_like_components(mask, max_area_fraction=0.02)
    assert union_bbox(filtered) is None


def test_text_like_filter_threshold_is_explicit() -> None:
    mask = np.zeros((200, 400), dtype=np.uint8)
    mask[10:60, 10:110] = 255  # 5000 px of 80000 = 6.25%
    assert union_bbox(filter_text_like_components(mask, max_area_fraction=0.02)) is None
    assert union_bbox(filter_text_like_components(mask, max_area_fraction=0.10)) == (10, 10, 110, 60)


# --- dominant text line -------------------------------------------------------
#
# Re-deriving a reference video: most states extract at `clean` tier, but only
# a few land within tolerance because scattered static bright regions elsewhere in the frame
# survived the size filter and stretched the union box. A caption is a horizontal line of
# glyphs sharing a vertical band; unrelated bright regions do not share it. Selecting the
# dominant line is a general prior on text layout, not a per-reel ROI.


def test_dominant_line_drops_components_outside_the_caption_band() -> None:
    mask = np.zeros((400, 800), dtype=np.uint8)
    # The caption: four glyphs on one baseline.
    for index in range(4):
        x = 300 + index * 50
        mask[200:240, x : x + 40] = 255
    # Static bright noise elsewhere, glyph-sized so the size filter keeps it.
    mask[60:100, 40:80] = 255
    mask[340:380, 700:740] = 255
    kept = dominant_text_line(mask)
    assert union_bbox(kept) == (300, 200, 490, 240)


def test_dominant_line_keeps_a_multi_line_caption_when_lines_are_grouped() -> None:
    """Two stacked lines of one caption are separate lines; the larger one wins."""
    mask = np.zeros((400, 800), dtype=np.uint8)
    for index in range(5):
        mask[200:240, 200 + index * 50 : 240 + index * 50] = 255  # long line
    for index in range(2):
        mask[260:300, 300 + index * 50 : 340 + index * 50] = 255  # short line
    kept = dominant_text_line(mask)
    assert union_bbox(kept) == (200, 200, 440, 240)


def test_dominant_line_of_a_single_component_is_that_component() -> None:
    mask = np.zeros((200, 400), dtype=np.uint8)
    mask[80:120, 100:180] = 255
    assert union_bbox(dominant_text_line(mask)) == (100, 80, 180, 120)


def test_dominant_line_of_an_empty_mask_is_empty() -> None:
    assert union_bbox(dominant_text_line(np.zeros((100, 100), dtype=np.uint8))) is None


def test_glyphs_with_descenders_stay_on_the_same_line() -> None:
    """A 'y' descends below the x-height band but belongs to the same line."""
    mask = np.zeros((400, 800), dtype=np.uint8)
    mask[200:240, 300:340] = 255
    mask[200:260, 350:390] = 255  # descender
    mask[200:240, 400:440] = 255
    kept = dominant_text_line(mask)
    assert union_bbox(kept) == (300, 200, 440, 260)


# --- thin-stroke ink sampling -------------------------------------------------
#
# Formal high-contrast script (Imperial Script, Pinyon-like) has
# strokes only a few pixels wide. Erosion is the wrong tool there: a 1-2px stroke erodes to
# nothing, so measure_ink falls back to the unmodified mask and samples the antialiased edge
# along with the core, moving measured ink by ~20 RGB units on thin script
# states. Sampling the stroke's ridge - the pixels furthest from its own boundary, found by a
# distance transform - generalises erosion: for a thick stroke the ridge is a broad region, for
# a thin one it is the centreline.


def _thin_stroke_frame() -> tuple:
    """A 1px bright core with one antialiased shoulder row, on a dark ground.

    Total masked thickness is 2px, which has no geometric interior at all: erosion empties it
    and a distance-transform ridge cannot separate core from shoulder either. This is the real
    script-stroke case.
    """
    frame = np.zeros((80, 200, 3), dtype=np.uint8)
    mask = np.zeros((80, 200), dtype=np.uint8)
    for x in range(40, 160):
        frame[38, x] = (251, 249, 247)  # BGR core
        frame[39, x] = (120, 119, 118)  # antialiased shoulder
        mask[38:40, x] = 255
    return frame, mask


def test_thin_stroke_ink_is_sampled_from_the_ridge_not_the_shoulders() -> None:
    frame, mask = _thin_stroke_frame()
    ink = measure_ink(frame, mask, sample="ridge")
    assert ink.rgb_median == (247, 249, 251)


def test_eroding_a_thin_stroke_pollutes_the_measurement() -> None:
    """The regression this closes: erosion cannot help a stroke thinner than the kernel."""
    frame, mask = _thin_stroke_frame()
    eroded = measure_ink(frame, mask, erode_px=1)
    ridge = measure_ink(frame, mask, sample="ridge")
    assert eroded.rgb_median != ridge.rgb_median
    assert ridge.rgb_median == (247, 249, 251)


def test_ridge_sampling_still_works_on_a_thick_stroke() -> None:
    frame = _blank(value=0)
    frame[40:80, 60:140] = (251, 249, 247)
    frame[38:40, 58:142] = (125, 124, 123)
    mask = np.zeros((120, 200), dtype=np.uint8)
    mask[38:82, 58:142] = 255
    assert measure_ink(frame, mask, sample="ridge").rgb_median == (247, 249, 251)


def test_ridge_sampling_never_empties_the_sample() -> None:
    mask = np.zeros((40, 40), dtype=np.uint8)
    mask[10, 10:20] = 255  # a 1px line
    frame = np.zeros((40, 40, 3), dtype=np.uint8)
    frame[10, 10:20] = (200, 100, 50)
    ink = measure_ink(frame, mask, sample="ridge")
    assert ink.interior_pixels > 0
    assert ink.rgb_median == (50, 100, 200)


def test_unknown_sample_mode_fails_closed() -> None:
    frame, mask = _thin_stroke_frame()
    with pytest.raises(ExtractError, match="sample|ridge|erode"):
        measure_ink(frame, mask, sample="whatever")


def test_stroke_width_is_reported_so_thin_states_can_be_flagged() -> None:
    frame, mask = _thin_stroke_frame()
    thin = measure_ink(frame, mask, sample="ridge")
    frame2 = _blank(value=0)
    frame2[40:80, 60:140] = (251, 249, 247)
    mask2 = np.zeros((120, 200), dtype=np.uint8)
    mask2[40:80, 60:140] = 255
    thick = measure_ink(frame2, mask2, sample="ridge")
    assert thin.stroke_width_px < thick.stroke_width_px
    assert thin.stroke_width_px <= 4


def test_auto_sampling_uses_erosion_on_a_thick_stroke() -> None:
    """Measured on real V12 states: strokes are 7-8px, where erosion is the accurate tool."""
    frame = _blank(value=0)
    frame[40:80, 60:140] = (251, 249, 247)
    frame[38:40, 58:142] = (125, 124, 123)
    mask = np.zeros((120, 200), dtype=np.uint8)
    mask[38:82, 58:142] = 255
    auto = measure_ink(frame, mask, sample="auto")
    erode = measure_ink(frame, mask, erode_px=1)
    assert auto.rgb_median == erode.rgb_median
    assert auto.sample == "auto:erode"


def test_auto_sampling_uses_the_thin_path_on_a_script_stroke() -> None:
    frame, mask = _thin_stroke_frame()
    auto = measure_ink(frame, mask, sample="auto")
    assert auto.rgb_median == (247, 249, 251)
    assert auto.sample == "auto:thin"
    assert auto.stroke_width_px <= MIN_GEOMETRIC_RIDGE_WIDTH
