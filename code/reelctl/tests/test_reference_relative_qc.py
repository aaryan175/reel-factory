from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from reelctl.qc import _caption_contrast_measure, _reference_relative_frame_qc, _resolve_visual_status, reference_relative_visual_qc


def _solid_bgr(color: tuple[int, int, int], count: int = 4) -> list[np.ndarray]:
    return [np.full((48, 64, 3), color, dtype=np.uint8) for _ in range(count)]


def test_reference_locked_frame_qc_rejects_post_boundary_color_drift() -> None:
    reference = _solid_bgr((20, 30, 180), 4)
    candidate = reference[:2] + _solid_bgr((180, 180, 180), 2)
    blocks = [
        {"id": "p001", "start_frame": 0, "end_frame_exclusive": 2},
        {"id": "p002", "start_frame": 2, "end_frame_exclusive": 4},
    ]

    report = _reference_relative_frame_qc(reference, candidate, blocks, mode="reference-locked")

    assert report["status"] == "FAIL"
    assert report["authority"] == "SHIP_BLOCKING"
    assert report["blocks"][0]["status"] == "PASS"
    assert report["blocks"][1]["status"] == "FAIL"
    assert "saturation_parity" in report["blocks"][1]["failed_checks"]


def test_original_montage_reports_reference_metrics_without_false_pixel_parity_gate() -> None:
    reference = _solid_bgr((20, 30, 180), 2)
    candidate = _solid_bgr((180, 180, 180), 2)
    blocks = [{"id": "p001", "start_frame": 0, "end_frame_exclusive": 2}]

    report = _reference_relative_frame_qc(reference, candidate, blocks, mode="original-montage")

    assert report["status"] == "NOT_APPLICABLE"
    assert report["authority"] == "DIAGNOSTIC_ONLY"
    assert report["blocks"][0]["status"] == "DIAGNOSTIC"


def test_caption_contrast_measure_catches_text_that_disappears_into_underlay() -> None:
    mask = np.zeros((48, 64), dtype=np.uint8)
    mask[16:32, 20:44] = 255
    invisible = np.full((48, 64, 3), 128, dtype=np.uint8)
    visible = np.full((48, 64, 3), 24, dtype=np.uint8)
    visible[16:32, 20:44] = 245

    assert _caption_contrast_measure(invisible, mask)["contrast_ratio"] < 1.8
    assert _caption_contrast_measure(visible, mask)["contrast_ratio"] >= 1.8


def test_machine_reference_failure_overrides_signed_agent_self_attestation() -> None:
    all_receipt_checks = {"signature": True, "candidate_hash": True, "receipt_pass": True}

    assert _resolve_visual_status({"status": "FAIL"}, all_receipt_checks) == "FAIL"
    assert _resolve_visual_status({"status": "PASS"}, all_receipt_checks) == "PASS"
    assert _resolve_visual_status({"status": "NOT_APPLICABLE"}, all_receipt_checks) == "PASS"


def test_a_known_human_rejected_candidate_fails_reference_relative_gate() -> None:
    """Optional real-media check. Set REELCTL_NEGATIVE_FIXTURE_DIR to a project containing
    reference/reference.mp4, edit/candidate.mp4 and reference/blueprint.json for a candidate
    a human rejected after frame 48."""
    import os

    configured = os.environ.get("REELCTL_NEGATIVE_FIXTURE_DIR")
    project = Path(configured).expanduser() if configured else Path("/nonexistent")
    reference = project / "reference/reference.mp4"
    candidate = project / "edit/candidate.mp4"
    blueprint_path = project / "reference/blueprint.json"
    if not (reference.is_file() and candidate.is_file() and blueprint_path.is_file()):
        pytest.skip("set REELCTL_NEGATIVE_FIXTURE_DIR to a human-rejected negative fixture to run this check")

    import json

    blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
    report = reference_relative_visual_qc(
        reference,
        candidate,
        blueprint["picture_blocks"],
        mode="reference-locked",
    )

    assert report["status"] == "FAIL"
    assert report["failing_blocks"]
    assert any(block["start_frame"] >= 48 for block in report["blocks"] if block["status"] == "FAIL")
