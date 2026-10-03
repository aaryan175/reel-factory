from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from reelctl.cli import main
from reelctl.contracts import ContractValidationError, validate_contract
from reelctl.variants import TrialFamilyError, validate_trial_family


def _sha(character: str) -> str:
    return hashlib.sha256(character.encode("utf-8")).hexdigest()


def _slot(slot_id: str, role: str, frames: int, source: str, start: int = 0) -> dict:
    return {
        "slot_id": slot_id,
        "role": role,
        "frames": frames,
        "source_sha256": _sha(source),
        "source_start_frame": start,
        "source_end_frame_exclusive": start + frames,
    }


def _manifest() -> dict:
    master_slots = [
        _slot("S01", "opening work hook", 10, "a"),
        _slot("S02", "caption-adjacent action", 10, "b"),
        _slot("S03", "lifestyle escalation", 10, "c"),
        _slot("S04", "closing work beat", 10, "d"),
    ]
    variants = []
    for index, (opening_source, second_source) in enumerate(zip("efghi", "jklmn"), start=1):
        slots = copy.deepcopy(master_slots)
        slots[0]["source_sha256"] = _sha(opening_source)
        slots[1]["source_sha256"] = _sha(second_source)
        variants.append(
            {
                "variant_id": f"TR{index:02d}",
                "primary_test_variable": "footage_package",
                "slots": slots,
                "review": {
                    "status": "PLANNED",
                    "render_sha256": None,
                    "technical_qc": "PENDING",
                    "visual_qc": "PENDING",
                    "normal_speed_full_watch": False,
                },
            }
        )
    return {
        "schema_version": 1,
        "family_id": "caption-a-trial-family-v1",
        "family_state": "PLANNED",
        "mode": "trial-variant-family",
        "master": {
            "reel_id": "REEL-12",
            "selection_sha256": _sha("0"),
            "slots": master_slots,
        },
        "policy": {
            "minimum_variants": 5,
            "opening_slot_id": "S01",
            "minimum_changed_video_slots": 2,
            "minimum_changed_visual_ratio": 0.3,
            "unique_opening_source_per_variant": True,
            "locked_layers": [
                "frame_clock",
                "cut_clock",
                "audio",
                "caption_timing",
                "typography",
                "effects",
                "ending",
            ],
            "non_counting_changes": [
                "container_metadata",
                "encode_settings",
                "file_name",
                "cover_only",
                "public_caption_only",
            ],
        },
        "variants": variants,
    }


def test_valid_five_variant_family_passes_with_real_clip_changes() -> None:
    manifest = _manifest()

    validate_contract("trial-family", manifest)
    report = validate_trial_family(manifest)

    assert report["status"] == "PASS"
    assert report["variant_count"] == 5
    assert report["minimum_observed_changed_video_slots"] == 2
    assert report["minimum_observed_changed_visual_ratio"] == 0.5
    assert report["opening_sources_unique"] is True
    assert report["publication_allowed"] is False


def test_family_schema_rejects_fewer_than_five_variants() -> None:
    manifest = _manifest()
    manifest["variants"] = manifest["variants"][:4]

    with pytest.raises(ContractValidationError, match="too short"):
        validate_contract("trial-family", manifest)


def test_family_rejects_unchanged_opening_clip() -> None:
    manifest = _manifest()
    manifest["variants"][0]["slots"][0]["source_sha256"] = manifest["master"]["slots"][0]["source_sha256"]

    with pytest.raises(TrialFamilyError, match="opening clip"):
        validate_trial_family(manifest)


def test_different_window_from_same_clip_does_not_count_as_new_clip() -> None:
    manifest = _manifest()
    opening = manifest["variants"][0]["slots"][0]
    opening["source_sha256"] = manifest["master"]["slots"][0]["source_sha256"]
    opening["source_start_frame"] = 100
    opening["source_end_frame_exclusive"] = 110

    with pytest.raises(TrialFamilyError, match="opening clip"):
        validate_trial_family(manifest)


def test_family_rejects_duplicate_sibling_clip_package() -> None:
    manifest = _manifest()
    manifest["variants"][1]["slots"] = copy.deepcopy(manifest["variants"][0]["slots"])

    with pytest.raises(TrialFamilyError, match="duplicate clip package"):
        validate_trial_family(manifest)


def test_local_review_ready_requires_hash_unique_qc_passed_renders() -> None:
    manifest = _manifest()
    manifest["family_state"] = "LOCAL_REVIEW_READY"

    with pytest.raises(TrialFamilyError, match="LOCAL_REVIEW_READY"):
        validate_trial_family(manifest)

    for index, variant in enumerate(manifest["variants"], start=1):
        variant["review"] = {
            "status": "LOCAL_REVIEW_READY",
            "render_sha256": f"{index:064x}",
            "technical_qc": "PASS",
            "visual_qc": "PASS",
            "normal_speed_full_watch": True,
        }
    report = validate_trial_family(manifest)
    assert report["render_hashes_unique"] is True


def test_cli_validates_family_and_writes_hash_bound_receipt(tmp_path: Path, capsys) -> None:
    manifest_path = tmp_path / "family.json"
    receipt_path = tmp_path / "family-validation.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

    code = main(
        [
            "variants",
            "validate",
            "--manifest",
            str(manifest_path),
            "--output",
            str(receipt_path),
        ],
        exit_on_error=False,
    )

    assert code == 0
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "PASS"
    assert receipt["variant_count"] == 5
    assert len(receipt["manifest_sha256"]) == 64
    assert len(receipt["trial_family_schema_sha256"]) == 64
    assert len(receipt["recipe_hash"]) == 64
    stdout = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert stdout["output"] == str(receipt_path.absolute())
