from __future__ import annotations

import pytest

from reelctl.contracts import validate_contract
from reelctl.feasibility import FeasibilityError, feasibility_template, validate_feasibility


def inventory() -> dict:
    return {
        "status": "PASS",
        "clips": [
            {"clip_id": "clip-a", "thumbnails": ["a1", "a2", "a3", "a4"]},
            {"clip_id": "clip-b", "thumbnails": ["b1", "b2", "b3", "b4"]},
        ],
    }


def test_feasibility_requires_one_inventory_bound_decision_per_picture_state() -> None:
    blueprint = {"picture_blocks": [{"id": "p001", "role": "sunset beach"}, {"id": "p002", "role": "night bike"}]}
    draft = feasibility_template(blueprint)
    assert [item["block_id"] for item in draft["blocks"]] == ["p001", "p002"]
    draft["blocks"][0].update(
        {
            "coverage": "role_equivalent_substitute",
            "evidence": "clip-a frame 40 shows a readable sunset beach walk",
            "evidence_clip_ids": ["clip-a"],
            "decision": "APPROVED_ROLE_EQUIVALENT_SUBSTITUTE",
        }
    )
    draft["blocks"][1].update(
        {
            "coverage": "missing",
            "evidence": "no night bike scene exists after full inventory review",
            "decision": "ACQUIRE_NEW_FOOTAGE",
        }
    )
    report = validate_feasibility(blueprint, draft, inventory=inventory())
    assert report["status"] == "BLOCKED"
    assert report["missing_blocks"] == ["p002"]
    draft["blocks"][1].update(
        {
            "coverage": "exact_scene_available",
            "evidence": "clip-b frame 12 shows the literal night bike mount",
            "evidence_clip_ids": ["clip-b"],
            "decision": "EXACT_SCENE_SELECTED",
        }
    )
    report = validate_feasibility(blueprint, draft, inventory=inventory())
    assert report["status"] == "BLOCKED"
    assert report["mode_switch_required"] is True
    assert report["exact_coverage_ratio"] == 0.5
    draft["blocks"][0].update(
        {
            "coverage": "exact_scene_available",
            "evidence": "clip-a frame 40 contains the literal sunset beach walk",
            "decision": "EXACT_SCENE_SELECTED",
        }
    )
    assert validate_feasibility(blueprint, draft, inventory=inventory())["status"] == "PASS"


def test_original_montage_allows_role_equivalent_coverage_without_claiming_literal_parity() -> None:
    blueprint = {"picture_blocks": [{"id": "p001", "role": "desert fire performer"}]}
    manifest = {
        "blocks": [
            {
                "block_id": "p001",
                "reference_role": "desert fire performer",
                "coverage": "role_equivalent_substitute",
                "evidence": "clip-a contains a high-motion night climax but not the literal desert scene",
                "evidence_clip_ids": ["clip-a"],
                "decision": "APPROVED_ROLE_EQUIVALENT_SUBSTITUTE",
            }
        ]
    }
    report = validate_feasibility(blueprint, manifest, inventory=inventory(), mode="original-montage")
    assert report["status"] == "PASS"
    assert report["exact_coverage_ratio"] == 0.0
    assert report["mode_switch_required"] is False


def test_locked_feasibility_schema_accepts_complete_validation_report() -> None:
    blueprint = {"picture_blocks": [{"id": "p001", "role": "desert fire performer"}]}
    manifest = {
        "schema_version": 1,
        "status": "DRAFT",
        "blocks": [
            {
                "block_id": "p001",
                "reference_role": "desert fire performer",
                "coverage": "role_equivalent_substitute",
                "evidence": "clip-a contains a high-motion night climax but not the literal desert scene",
                "evidence_clip_ids": ["clip-a"],
                "decision": "APPROVED_ROLE_EQUIVALENT_SUBSTITUTE",
            }
        ],
    }
    report = validate_feasibility(blueprint, manifest, inventory=inventory(), mode="original-montage")
    locked = {**manifest, "status": report["status"], "validation": report}
    assert validate_contract("feasibility", locked)["status"] == "PASS"


def test_feasibility_rejects_placeholder_evidence() -> None:
    blueprint = {"picture_blocks": [{"id": "p001", "role": "sunset beach"}]}
    draft = feasibility_template(blueprint)
    draft["blocks"][0].update(
        {
            "coverage": "role_equivalent_substitute",
            "evidence_clip_ids": ["clip-a"],
            "decision": "APPROVED_ROLE_EQUIVALENT_SUBSTITUTE",
        }
    )
    with pytest.raises(FeasibilityError):
        validate_feasibility(blueprint, draft, inventory=inventory())


def test_feasibility_rejects_uninventoried_clip_evidence() -> None:
    blueprint = {"picture_blocks": [{"id": "p001", "role": "sunset beach"}]}
    draft = feasibility_template(blueprint)
    draft["blocks"][0].update(
        {
            "coverage": "exact_scene_available",
            "evidence": "unknown clip allegedly contains scene",
            "evidence_clip_ids": ["not-in-inventory"],
            "decision": "EXACT_SCENE_SELECTED",
        }
    )
    with pytest.raises(FeasibilityError, match="outside the locked footage inventory"):
        validate_feasibility(blueprint, draft, inventory=inventory())
