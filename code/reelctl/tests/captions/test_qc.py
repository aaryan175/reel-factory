"""Renderer-independent caption QC (DOCTRINE.md section 7).

Gates never read the renderer's own constants. QC compares the render against the
**reference frames** and recomputes every number. The circularity being avoided is the
documented one: "render a candidate, force it into a hard-coded target envelope, then assert
the resulting bbox equals that envelope - proves only that the resize instruction ran"
(IMPL section 8).

Three properties this file pins down:

* **state-class aware gates** - a uniform threshold is wrong, because `threshold_bbox >= 128`
  "may legitimately be null" on heavily blurred entry frames (A1 contradiction C8).
* **codec artifact vs contour defect** - one or two pixels of YUV420 chroma bleed and ordinary
  antialias change are not authored defects (NFS section 7).
* **chronological localisation** - first defective frame, first mature/readable frame, the
  affected interval, the authority layer, and the evidence limit (NFS section 5). Reporting a
  convenient midpoint is not allowed.
"""

from __future__ import annotations

import numpy as np
import pytest

from reelctl.captions.qc import (
    BLUR_MIN_DICE,
    CRISP_MIN_DICE,
    QcError,
    dice,
    ink_distance,
    iou,
    is_codec_artifact,
    qc_state,
    qc_timing,
)


def _box_mask(box=(60, 40, 100, 60), shape=(120, 200)) -> np.ndarray:
    mask = np.zeros(shape, dtype=np.uint8)
    x0, y0, x1, y1 = box
    mask[y0:y1, x0:x1] = 255
    return mask


# --- metrics -----------------------------------------------------------------


def test_dice_of_identical_masks_is_one() -> None:
    mask = _box_mask()
    assert dice(mask, mask) == 1.0


def test_iou_of_identical_masks_is_one() -> None:
    mask = _box_mask()
    assert iou(mask, mask) == 1.0


def test_dice_and_iou_of_disjoint_masks_are_zero() -> None:
    left = _box_mask((10, 10, 30, 30))
    right = _box_mask((150, 80, 190, 110))
    assert dice(left, right) == 0.0
    assert iou(left, right) == 0.0


def test_dice_exceeds_iou_for_partial_overlap() -> None:
    reference = _box_mask((60, 40, 100, 60))
    candidate = _box_mask((70, 40, 110, 60))
    assert dice(reference, candidate) > iou(reference, candidate)


def test_two_empty_masks_are_not_silently_a_perfect_match() -> None:
    empty = np.zeros((120, 200), dtype=np.uint8)
    with pytest.raises(QcError, match="empty"):
        dice(empty, empty)


def test_ink_distance_is_zero_for_the_same_colour() -> None:
    assert ink_distance((247, 249, 251), (247, 249, 251)) == 0.0


def test_ink_distance_grows_with_channel_separation() -> None:
    near = ink_distance((247, 249, 251), (245, 247, 249))
    far = ink_distance((247, 249, 251), (125, 13, 34))
    assert far > near > 0


# --- codec artifact vs contour defect ----------------------------------------


def test_one_pixel_boundary_difference_is_a_codec_artifact() -> None:
    reference = _box_mask((60, 40, 100, 60))
    candidate = _box_mask((60, 40, 101, 60))
    assert is_codec_artifact(reference, candidate, tolerance_px=2) is True


def test_a_shifted_glyph_is_not_a_codec_artifact() -> None:
    reference = _box_mask((60, 40, 100, 60))
    candidate = _box_mask((70, 40, 110, 60))
    assert is_codec_artifact(reference, candidate, tolerance_px=2) is False


def test_a_missing_component_is_not_a_codec_artifact() -> None:
    """A missing dot or apostrophe alters a meaningful structure (NFS section 7)."""
    reference = _box_mask((60, 40, 100, 60))
    reference[20:26, 60:66] = 255  # the dot
    candidate = _box_mask((60, 40, 100, 60))  # dot absent
    assert is_codec_artifact(reference, candidate, tolerance_px=2) is False


def test_tolerance_is_explicit_not_assumed() -> None:
    reference = _box_mask((60, 40, 100, 60))
    candidate = _box_mask((60, 40, 104, 60))
    assert is_codec_artifact(reference, candidate, tolerance_px=2) is False
    assert is_codec_artifact(reference, candidate, tolerance_px=5) is True


# --- per-state gates ---------------------------------------------------------


def test_crisp_state_needs_the_strict_gate() -> None:
    assert CRISP_MIN_DICE > BLUR_MIN_DICE
    reference = _box_mask((60, 40, 100, 60))
    candidate = _box_mask((66, 40, 106, 60))  # visibly displaced
    verdict = qc_state(
        reference_mask=reference,
        rendered_alpha=candidate,
        lifecycle_kind="hard_state",
        is_crisp_frame=True,
        frame=3,
    )
    assert verdict.passed is False
    assert verdict.gate == "crisp"
    assert verdict.dice < CRISP_MIN_DICE


def test_same_displacement_is_tolerated_on_a_blur_entry_frame() -> None:
    """An entry-blur frame is judged by the relaxed gate, not the crisp one."""
    reference = _box_mask((60, 40, 100, 60))
    candidate = _box_mask((64, 40, 104, 60))
    verdict = qc_state(
        reference_mask=reference,
        rendered_alpha=candidate,
        lifecycle_kind="blur_to_crisp",
        is_crisp_frame=False,
        frame=2,
    )
    assert verdict.gate == "blur"
    assert verdict.passed is True


def test_a_codec_artifact_does_not_fail_a_crisp_state() -> None:
    reference = _box_mask((60, 40, 100, 60))
    candidate = _box_mask((60, 40, 101, 60))
    verdict = qc_state(
        reference_mask=reference,
        rendered_alpha=candidate,
        lifecycle_kind="hard_state",
        is_crisp_frame=True,
        frame=3,
    )
    assert verdict.passed is True
    assert verdict.codec_artifact is True


def test_verdict_records_the_evidence_limit() -> None:
    reference = _box_mask()
    verdict = qc_state(
        reference_mask=reference,
        rendered_alpha=reference,
        lifecycle_kind="hard_state",
        is_crisp_frame=True,
        frame=3,
    )
    record = verdict.to_dict()
    assert record["evidence_limit"]
    assert record["frame"] == 3
    assert "dice" in record and "iou" in record


def test_technical_pass_is_not_a_creative_pass() -> None:
    reference = _box_mask()
    verdict = qc_state(
        reference_mask=reference,
        rendered_alpha=reference,
        lifecycle_kind="hard_state",
        is_crisp_frame=True,
        frame=3,
    )
    assert verdict.passed is True
    assert verdict.to_dict()["creative_approval"] == "PENDING"


# --- timing ------------------------------------------------------------------


def _expected_states():
    return [
        {"id": "C01", "start_frame": 2, "end_frame_exclusive": 5},
        {"id": "C02", "start_frame": 7, "end_frame_exclusive": 9},
    ]


def _rendered(present_frames, count=12, shape=(120, 200)):
    frames = []
    for index in range(count):
        frame = np.zeros((*shape, 4), dtype=np.uint8)
        if index in present_frames:
            frame[40:60, 60:100, 3] = 255
        frames.append(frame)
    return frames


def test_timing_passes_when_ink_lands_exactly_on_the_contract_frames() -> None:
    report = qc_timing(_rendered({2, 3, 4, 7, 8}), _expected_states(), frame_count=12)
    assert report.passed is True
    assert report.first_divergent_frame is None


def test_timing_reports_the_first_divergent_frame_not_a_summary() -> None:
    # Frame 5 carries ink but no state declares it.
    report = qc_timing(_rendered({2, 3, 4, 5, 7, 8}), _expected_states(), frame_count=12)
    assert report.passed is False
    assert report.first_divergent_frame == 5


def test_timing_catches_a_dropped_final_frame() -> None:
    """The -shortest endpoint hazard: the last frame silently vanishes (CFR)."""
    report = qc_timing(_rendered({2, 3, 4, 7}), _expected_states(), frame_count=12)
    assert report.passed is False
    assert report.first_divergent_frame == 8


def test_timing_requires_the_declared_blank_frames_to_be_blank() -> None:
    report = qc_timing(_rendered({2, 3, 4, 7, 8}), _expected_states(), frame_count=12)
    assert report.blank_frames == (0, 1, 5, 6, 9, 10, 11)


def test_timing_frame_count_mismatch_fails_closed() -> None:
    with pytest.raises(QcError, match="frame_count|12|10"):
        qc_timing(_rendered({2, 3}, count=10), _expected_states(), frame_count=12)


def test_timing_report_lists_every_divergence_for_localisation() -> None:
    report = qc_timing(_rendered({2, 3, 5, 7, 8}), _expected_states(), frame_count=12)
    assert report.first_divergent_frame == 4
    assert set(report.divergent_frames) == {4, 5}
    record = report.to_dict()
    assert record["divergent_frames"] == [4, 5]
    assert record["evidence_limit"]
