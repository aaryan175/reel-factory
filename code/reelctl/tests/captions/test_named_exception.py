"""The evidence-backed named geometry exception (DOCTRINE.md rule 4.3).

This resolves contradiction C3. <ref-06> and BITF say per-word X/Y stretching is forbidden
outright; AFFINE says one named lockup may use a stronger affine fit when direct multi-frame
evidence proves it. Neither file states the combined rule, and AFFINE itself warns against
"using a global unbounded vertical stretch to fix one opening word".

An approved script-caption baseline forces the issue: a record that ships
`font_size: 190, scale_y: 1.15` and was reviewed and approved. An engine that cannot represent
it cannot re-derive an approved baseline. An engine that represents it
with a free `scale_xy` reopens the false-match mechanism.

So anisotropy stays unrepresentable in the default geometry, and is reachable only through a
named exception that must carry all five things AFFINE requires:

1. direct multi-frame evidence (>= 2 frames),
2. an explicit contract flag,
3. the retained measured bbox AND clean native bbox,
4. a written justification that it is geometry reproduction, not font substitution,
5. a failing test first — which is this file.
"""

from __future__ import annotations

import pytest

from reelctl.captions.contract import CaptionContractError, validate_caption_contract

MASK_SHA = "d" * 64


def _state(geometry=None) -> dict:
    return {
        "id": "C08",
        "start_frame": 29,
        "end_frame_exclusive": 45,
        "frames": 16,
        "text": "Sample",
        "lines": ["Sample"],
        "style_id": "emphasis_clean_imperial_script",
        "placement": {
            "core_bbox_xyxy": [729, 477, 1184, 625],
            "treatment_bbox_xyxy": [729, 477, 1184, 625],
            "anchor": "optical_center",
        },
        "geometry": geometry or {"scale": 1.0, "translate_xy": [0.0, 0.0]},
        "ink": {
            "source": "sampled_reference_pixels",
            "sample": {
                "method": "min_channel_threshold",
                "frames": [36],
                "roi_xyxy": [729, 477, 1184, 625],
            },
            "rgb_median": [255, 248, 238],
        },
        "lifecycle": {"kind": "hard_state"},
        "stacking": {"z": 0, "persists": False},
        "evidence": {
            "tier": "MASK_VERIFIED",
            "mask_path": "assets/captions/029-044-sample.png",
            "mask_sha256": MASK_SHA,
        },
    }


def _contract(state) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "caption_contract",
        "status": "EXTRACTED",
        "render_allowed": False,
        "reference": {
            "path": "reference/reference-source.mp4",
            "sha256": "c" * 64,
            "frame_count": 238,
            "fps": "2997/125",
            "width": 1916,
            "height": 1078,
            "sample_aspect_ratio": "1:1",
        },
        "styles": {"emphasis_clean_imperial_script": {"render_path": "source_contour"}},
        "states": [state],
        "font_hypotheses": [],
    }


def _exception(**overrides) -> dict:
    payload = {
        "flag": "reference_height_fit",
        "axis": "y",
        "axis_scale": 1.15,
        "evidence_frames": [30, 36, 42],
        "measured_bbox_xyxy": [729, 477, 1184, 625],
        "clean_native_bbox_xyxy": [729, 490, 1184, 619],
        "justification": (
            "The reference H swash is vertically taller than the face renders at the shared "
            "tier size; this reproduces the reference's own geometry and does not substitute "
            "a different font."
        ),
    }
    payload.update(overrides)
    return payload


def test_free_anisotropic_scale_is_still_unrepresentable() -> None:
    payload = _contract(_state({"scale": 1.0, "scale_y": 1.15, "translate_xy": [0.0, 0.0]}))
    with pytest.raises(CaptionContractError, match="scale_y|scale"):
        validate_caption_contract(payload)


def test_fully_evidenced_named_exception_is_accepted() -> None:
    geometry = {
        "scale": 1.0,
        "translate_xy": [0.0, 0.0],
        "named_exception": _exception(),
    }
    assert validate_caption_contract(_contract(_state(geometry)))["status"] == "PASS"


def test_named_exception_needs_multi_frame_evidence() -> None:
    geometry = {
        "scale": 1.0,
        "translate_xy": [0.0, 0.0],
        "named_exception": _exception(evidence_frames=[36]),
    }
    with pytest.raises(CaptionContractError, match="evidence_frames|multi-frame"):
        validate_caption_contract(_contract(_state(geometry)))


def test_named_exception_needs_both_retained_boxes() -> None:
    exception = _exception()
    del exception["clean_native_bbox_xyxy"]
    geometry = {"scale": 1.0, "translate_xy": [0.0, 0.0], "named_exception": exception}
    with pytest.raises(CaptionContractError, match="clean_native_bbox_xyxy"):
        validate_caption_contract(_contract(_state(geometry)))


def test_named_exception_needs_a_written_justification() -> None:
    geometry = {
        "scale": 1.0,
        "translate_xy": [0.0, 0.0],
        "named_exception": _exception(justification="looks better"),
    }
    with pytest.raises(CaptionContractError, match="justification"):
        validate_caption_contract(_contract(_state(geometry)))


def test_justification_must_claim_geometry_reproduction_not_font_substitution() -> None:
    geometry = {
        "scale": 1.0,
        "translate_xy": [0.0, 0.0],
        "named_exception": _exception(
            justification=(
                "Stretched vertically so the word fills the target envelope we drew for it, "
                "which makes the layout look correct at this size in the timeline."
            )
        ),
    }
    with pytest.raises(CaptionContractError, match="geometry reproduction|envelope|substitut"):
        validate_caption_contract(_contract(_state(geometry)))


def test_evidence_frames_must_lie_inside_the_state_span() -> None:
    geometry = {
        "scale": 1.0,
        "translate_xy": [0.0, 0.0],
        "named_exception": _exception(evidence_frames=[30, 200]),
    }
    with pytest.raises(CaptionContractError, match="200|span|evidence_frames"):
        validate_caption_contract(_contract(_state(geometry)))


def test_unbounded_axis_scale_is_refused() -> None:
    """AFFINE warns against a global unbounded vertical stretch to fix one word."""
    geometry = {
        "scale": 1.0,
        "translate_xy": [0.0, 0.0],
        "named_exception": _exception(axis_scale=2.4),
    }
    with pytest.raises(CaptionContractError, match="axis_scale|bounded|1.15|range"):
        validate_caption_contract(_contract(_state(geometry)))


def test_axis_scale_of_one_is_pointless_and_refused() -> None:
    geometry = {
        "scale": 1.0,
        "translate_xy": [0.0, 0.0],
        "named_exception": _exception(axis_scale=1.0),
    }
    with pytest.raises(CaptionContractError, match="axis_scale"):
        validate_caption_contract(_contract(_state(geometry)))


def test_exception_is_named_per_state_not_global() -> None:
    """Two states may not share one exception; each carries its own evidence."""
    first = _state({"scale": 1.0, "translate_xy": [0.0, 0.0], "named_exception": _exception()})
    second = _state({"scale": 1.0, "translate_xy": [0.0, 0.0], "named_exception": _exception()})
    second["id"] = "C09"
    second["start_frame"] = 60
    second["end_frame_exclusive"] = 76
    second["frames"] = 16
    payload = _contract(first)
    payload["states"].append(second)
    # The second state's evidence frames sit in the first state's span, not its own.
    with pytest.raises(CaptionContractError, match="evidence_frames|span"):
        validate_caption_contract(payload)


def test_named_exception_does_not_grant_a_font_claim() -> None:
    """Geometry reproduction must never be read as proof of the face."""
    geometry = {"scale": 1.0, "translate_xy": [0.0, 0.0], "named_exception": _exception()}
    payload = _contract(_state(geometry))
    payload["styles"]["emphasis_clean_imperial_script"]["render_path"] = "native_font"
    with pytest.raises(CaptionContractError, match="native_font|holdout|proven"):
        validate_caption_contract(payload)
