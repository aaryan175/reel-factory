"""Caption contract fail-closed tests.

Every test here encodes a historical failure class. The mechanisms are recorded in
``src/reelctl/captions/DOCTRINE.md``; the short version:

* ``blueprint.schema.json`` declared ``caption_layers`` as an unconstrained
  ``{"type": "array", "items": {"type": "object"}}`` — any caption payload validated,
  so no caption defect could ever be caught by the schema layer.
* ``assets.schema.json`` exposes ``scale_xy`` (an x/y pair), which permits per-word
  anisotropic resize. Per-word anisotropic fitting is what let a wrong face score as a
  match in a legacy per-reel font-fitting script (sx and sy each swept
  independently over +/-4%, script fonts over +/-18%).
* ``assets.schema.json`` exposes ``fill_rgba`` as a bare constant with no obligation to
  record where the colour came from, against the rule that ink is sampled from the
  reference's own pixels.
"""

from __future__ import annotations

import copy

import pytest

from reelctl.captions.contract import (
    CaptionContractError,
    validate_caption_contract,
)

REFERENCE_SHA = "c" * 64
MASK_SHA = "d" * 64


def _state(
    state_id: str = "C01",
    start: int = 0,
    end: int = 47,
    text: str = "this is a sample caption:",
) -> dict:
    return {
        "id": state_id,
        "start_frame": start,
        "end_frame_exclusive": end,
        "frames": end - start,
        "text": text,
        "lines": [text],
        "style_id": "T01_sans_white",
        "placement": {
            "core_bbox_xyxy": [511, 518, 1406, 572],
            "treatment_bbox_xyxy": [509, 516, 1408, 574],
            "anchor": "optical_center",
        },
        "geometry": {"scale": 1.0, "translate_xy": [0.0, 0.0]},
        "ink": {
            "source": "sampled_reference_pixels",
            "sample": {
                "method": "min_channel_threshold",
                "frames": [20],
                "roi_xyxy": [511, 518, 1406, 572],
                "interior_pixels": 4211,
            },
            "rgb_median": [247, 249, 251],
        },
        "lifecycle": {"kind": "static", "note": "footage moves beneath a static layer"},
        "stacking": {"z": 0, "persists": False},
        "evidence": {
            "tier": "MASK_VERIFIED",
            "mask_path": "evidence/captions/C01.png",
            "mask_sha256": MASK_SHA,
        },
    }


def _contract(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "artifact_type": "caption_contract",
        "status": "EXTRACTED",
        "render_allowed": False,
        "reference": {
            "path": "reference/reference-source.mp4",
            "sha256": REFERENCE_SHA,
            "frame_count": 172,
            "fps": "2997/125",
            "width": 1916,
            "height": 1078,
            "sample_aspect_ratio": "1:1",
        },
        "styles": {
            "T01_sans_white": {
                "visual_family": "grotesk sans",
                "render_path": "source_contour",
                "production_proof_rule": "traced reference glyph plates only",
            }
        },
        "states": [_state()],
        "font_hypotheses": [],
    }
    payload.update(overrides)
    return payload


def test_valid_contract_passes() -> None:
    assert validate_caption_contract(_contract())["status"] == "PASS"


def test_unknown_top_level_field_fails_closed() -> None:
    payload = _contract()
    payload["caption_preset"] = "fade_in"
    with pytest.raises(CaptionContractError, match="caption_preset"):
        validate_caption_contract(payload)


def test_unknown_state_field_fails_closed() -> None:
    payload = _contract()
    payload["states"][0]["entrance_animation"] = "typewriter"
    with pytest.raises(CaptionContractError, match="entrance_animation"):
        validate_caption_contract(payload)


def test_frames_must_equal_the_declared_span() -> None:
    payload = _contract()
    payload["states"][0]["frames"] = 46
    with pytest.raises(CaptionContractError, match="frames"):
        validate_caption_contract(payload)


def test_state_may_not_end_past_the_reference_clock() -> None:
    payload = _contract()
    payload["states"][0]["end_frame_exclusive"] = 173
    payload["states"][0]["frames"] = 173
    with pytest.raises(CaptionContractError, match="clock|frame_count|172"):
        validate_caption_contract(payload)


def test_zero_length_state_is_rejected() -> None:
    payload = _contract()
    payload["states"][0]["end_frame_exclusive"] = 0
    payload["states"][0]["frames"] = 0
    with pytest.raises(CaptionContractError):
        validate_caption_contract(payload)


def test_single_frame_state_is_allowed() -> None:
    """One-frame caption blinks are real reference behaviour, not a defect."""
    payload = _contract()
    payload["states"] = [_state("C02", 47, 48, "Wow")]
    payload["styles"]["T02_script_white"] = payload["styles"]["T01_sans_white"]
    payload["states"][0]["style_id"] = "T02_script_white"
    assert validate_caption_contract(payload)["status"] == "PASS"


def test_anisotropic_scale_is_structurally_unrepresentable() -> None:
    """Per-word anisotropic resize is the documented false-match mechanism."""
    payload = _contract()
    payload["states"][0]["geometry"]["scale"] = [1.0, 1.04]
    with pytest.raises(CaptionContractError, match="scale"):
        validate_caption_contract(payload)


def test_separate_scale_axes_are_rejected() -> None:
    payload = _contract()
    payload["states"][0]["geometry"] = {
        "scale_x": 1.0,
        "scale_y": 1.04,
        "translate_xy": [0.0, 0.0],
    }
    with pytest.raises(CaptionContractError, match="scale_x|scale_y|scale"):
        validate_caption_contract(payload)


def test_constant_ink_without_sampling_provenance_is_rejected() -> None:
    payload = _contract()
    payload["states"][0]["ink"] = {"source": "constant", "rgb_median": [255, 255, 255]}
    with pytest.raises(CaptionContractError, match="constant|sampled_reference_pixels|source"):
        validate_caption_contract(payload)


def test_sampled_ink_must_carry_its_sample_evidence() -> None:
    payload = _contract()
    del payload["states"][0]["ink"]["sample"]
    with pytest.raises(CaptionContractError, match="sample"):
        validate_caption_contract(payload)


def test_state_referencing_an_undeclared_style_is_rejected() -> None:
    payload = _contract()
    payload["states"][0]["style_id"] = "T99_missing"
    with pytest.raises(CaptionContractError, match="T99_missing|style"):
        validate_caption_contract(payload)


def test_duplicate_state_ids_are_rejected() -> None:
    payload = _contract()
    payload["states"] = [_state("C01", 0, 47), _state("C01", 47, 60, "sample")]
    with pytest.raises(CaptionContractError, match="C01|duplicate"):
        validate_caption_contract(payload)


def test_overlapping_states_must_declare_stacking_order() -> None:
    """Concurrent captions are legal (stacked builds) but the order must be explicit."""
    first = _state("C01", 0, 47)
    second = _state("C02", 20, 60, "sample")
    second["stacking"] = {"z": 0, "persists": False}
    payload = _contract(states=[first, second])
    with pytest.raises(CaptionContractError, match="stacking|z|overlap"):
        validate_caption_contract(payload)


def test_overlapping_states_with_distinct_z_are_allowed() -> None:
    first = _state("C01", 0, 47)
    second = _state("C02", 20, 60, "sample")
    second["stacking"] = {"z": 1, "persists": True}
    payload = _contract(states=[first, second])
    assert validate_caption_contract(payload)["status"] == "PASS"


def test_render_allowed_cannot_be_true_while_a_style_is_unproven() -> None:
    """A hypothesis-only font may never unlock the native-font render path."""
    payload = _contract()
    payload["render_allowed"] = True
    payload["styles"]["T01_sans_white"]["render_path"] = "native_font"
    payload["font_hypotheses"] = [
        {
            "style_id": "T01_sans_white",
            "family": "Helvetica Neue",
            "full_name": "Helvetica Neue Bold",
            "path": "/System/Library/Fonts/HelveticaNeue.ttc",
            "face_index": 1,
            "sha256": "a" * 64,
            "identity_status": "HYPOTHESIS",
        }
    ]
    with pytest.raises(CaptionContractError, match="HYPOTHESIS|render_allowed|native_font"):
        validate_caption_contract(payload)


def test_native_font_path_requires_a_holdout_proven_hypothesis() -> None:
    payload = _contract()
    payload["styles"]["T01_sans_white"]["render_path"] = "native_font"
    with pytest.raises(CaptionContractError, match="native_font|holdout|hypoth"):
        validate_caption_contract(payload)


def test_source_contour_path_needs_no_font_hypothesis() -> None:
    """The 1:1 default must work with zero font claims."""
    payload = _contract()
    assert payload["styles"]["T01_sans_white"]["render_path"] == "source_contour"
    assert validate_caption_contract(payload)["status"] == "PASS"


def test_identity_status_proven_requires_holdout_evidence() -> None:
    payload = _contract()
    payload["font_hypotheses"] = [
        {
            "style_id": "T01_sans_white",
            "family": "Helvetica Neue",
            "full_name": "Helvetica Neue Bold",
            "path": "/System/Library/Fonts/HelveticaNeue.ttc",
            "face_index": 1,
            "sha256": "a" * 64,
            "identity_status": "HOLDOUT_PROVEN",
        }
    ]
    with pytest.raises(CaptionContractError, match="holdout"):
        validate_caption_contract(payload)


def test_placement_requires_both_the_core_and_treatment_box() -> None:
    """Rule 4.5: report the core 50%-alpha ink box AND the glow/shadow extent."""
    payload = _contract()
    del payload["states"][0]["placement"]["treatment_bbox_xyxy"]
    with pytest.raises(CaptionContractError, match="treatment_bbox_xyxy"):
        validate_caption_contract(payload)


def test_treatment_box_may_not_be_smaller_than_the_core_box() -> None:
    payload = _contract()
    payload["states"][0]["placement"]["treatment_bbox_xyxy"] = [600, 530, 900, 560]
    with pytest.raises(CaptionContractError, match="treatment|core|contain"):
        validate_caption_contract(payload)


def test_untreated_state_may_declare_identical_core_and_treatment_boxes() -> None:
    payload = _contract()
    core = payload["states"][0]["placement"]["core_bbox_xyxy"]
    payload["states"][0]["placement"]["treatment_bbox_xyxy"] = list(core)
    assert validate_caption_contract(payload)["status"] == "PASS"


def test_ambiguous_single_bbox_field_is_rejected() -> None:
    """A lone 'bbox_xyxy' is a placement envelope of unknown provenance (Rule 4.6)."""
    payload = _contract()
    del payload["states"][0]["placement"]["core_bbox_xyxy"]
    del payload["states"][0]["placement"]["treatment_bbox_xyxy"]
    payload["states"][0]["placement"]["bbox_xyxy"] = [511, 518, 1406, 572]
    with pytest.raises(CaptionContractError, match="bbox_xyxy|core_bbox_xyxy"):
        validate_caption_contract(payload)


def test_contract_is_not_mutated_by_validation() -> None:
    payload = _contract()
    before = copy.deepcopy(payload)
    validate_caption_contract(payload)
    assert payload == before
