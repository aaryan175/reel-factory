from __future__ import annotations

from pathlib import Path

import pytest

from reelctl.contracts import ContractValidationError, schema_sha256, validate_contract


def test_runtime_schema_rejects_unknown_selection_fields() -> None:
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "slots": [],
        "unexpected": "must fail closed",
    }
    with pytest.raises(ContractValidationError, match="unexpected"):
        validate_contract("selection", payload)


def test_runtime_schema_rejects_non_integer_frame_boundaries() -> None:
    payload = {
        "schema_version": 1,
        "status": "LOCKED",
        "reference_sha256": "a" * 64,
        "reference_lock_recipe_hash": "c" * 64,
        "all_frames_board_sha256": "d" * 64,
        "clock": {
            "coded_width": 1080,
            "coded_height": 1920,
            "width": 1080,
            "height": 1920,
            "rotation": 0,
            "sample_aspect_ratio": "1:1",
            "fps": "24/1",
            "time_base": "1/24",
            "frame_count": 4,
            "pts_start": 0,
            "pts_step": 1,
            "pts": [0, 1, 2, 3],
            "duration_ts": 4,
        },
        "picture_boundaries_after": [],
        "hard_cuts_after": [],
        "boundary_provenance": [],
        "picture_blocks": [
            {
                "id": "p001",
                "start_frame": 0.5,
                "end_frame_exclusive": 4,
                "frames": 4,
                "transition_from_previous": "opening",
                "role": "opening",
                "reference_observation": "visible opening",
                "evidence_frames": [0],
            }
        ],
        "caption_layers": [],
        "effect_layers": [],
        "all_frames_reviewed": True,
        "sha256_contract": "b" * 64,
        "signature": {
            "algorithm": "HMAC-SHA256",
            "purpose": "blueprint-lock-v1",
            "key_id": "e" * 16,
            "value": "f" * 64,
            "key_location": "/tmp/key",
        },
    }
    with pytest.raises(ContractValidationError, match="start_frame"):
        validate_contract("blueprint", payload)


def test_packaged_schema_has_stable_sha256() -> None:
    digest = schema_sha256("selection")
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_project_schema_rejects_unknown_policy_claim(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "project_id": "demo",
        "mode": "reference-locked",
        "reference_input": str(tmp_path / "reference.mp4"),
        "reference_path": None,
        "footage_root": str(tmp_path / "footage"),
        "output": {
            "master_codec": "prores_ks",
            "master_pix_fmt": "yuv422p10le",
            "review_codec": "libx264",
            "color": "bt709",
        },
        "policies": {
            "audio": "licensed_track_required",
            "timing": "exact_reference_pts_and_picture_blocks",
            "typography": "exact_font_hash_or_traced_reference_glyph",
            "speed": "normal_only_no_reverse",
            "color": "identified_input_profile_then_per_shot_grade",
            "publication": "human_approval_required",
            "self_publish": True,
        },
    }
    with pytest.raises(ContractValidationError, match="self_publish"):
        validate_contract("project", payload)
