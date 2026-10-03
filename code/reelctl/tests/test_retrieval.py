from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelctl.cli import main
from reelctl.contracts import validate_contract
from reelctl.retrieval import RetrievalError, load_jsonl_catalog, shortlist_reference_roles


def test_shortlist_ranks_role_evidence_without_granting_acceptance() -> None:
    blueprint = {
        "source_lock": {"video": {"fps": "24/1"}},
        "shot_roles": [
            {
                "id": "S02",
                "start_frame": 0,
                "end_frame_exclusive": 12,
                "role": "hero_word_architecture",
                "reference_observation": "Monumental glass tower with a strong vertical axis.",
                "composition": "Pale architectural negative space supports the caption.",
                "substitute_requirements": ["Use a monumental architectural detail shot."],
                "reject_if": ["generic desk portrait"],
                "grade_metrics": {"luma_p50_8bit": 170.0, "saturation_mean_8bit": 30.0},
            },
            {
                "id": "S12",
                "start_frame": 12,
                "end_frame_exclusive": 31,
                "role": "discipline_action",
                "reference_observation": "Wide outdoor sport action with a visible athletic peak.",
                "composition": "Full-body athlete in an outdoor environment.",
                "substitute_requirements": ["Use a real sport or training action."],
                "reject_if": ["static pose"],
                "grade_metrics": {"luma_p50_8bit": 85.0, "saturation_mean_8bit": 125.0},
            },
        ],
    }
    catalog = [
        {
            "clip_id": "architecture",
            "path": "day/shot-a-exterior.mp4",
            "source_sha256": "a" * 64,
            "duration_s": 4.0,
            "orientation": "landscape",
            "camera_motion": {"label": "locked_or_nearly_locked", "translation_speed_p90": 0.005},
            "top_visual_labels": [{"label": "structure", "confidence": 0.9}, {"label": "building", "confidence": 0.8}],
            "semantic_groups": ["interior_or_architecture"],
            "duplicate_or_setup_families": [
                {"family_id": "SHOT_A_EXTERIOR", "description": "Monumental vertical architecture facade."}
            ],
            "editorial_role_counts": {"subject_or_local_motion_peak": 0},
            "coverage_board": "/proof/architecture.jpg",
        },
        {
            "clip_id": "sport",
            "path": "day/shot-b-activity.mp4",
            "source_sha256": "b" * 64,
            "duration_s": 5.0,
            "orientation": "landscape",
            "camera_motion": {"label": "pan_right", "translation_speed_p90": 0.09},
            "top_visual_labels": [{"label": "people", "confidence": 0.9}, {"label": "outdoor", "confidence": 0.8}],
            "semantic_groups": ["human_action_or_portrait", "outdoor_or_environment"],
            "duplicate_or_setup_families": [
                {"family_id": "SHOT_B_ACTIVITY", "description": "Two-person sport and training action."}
            ],
            "pose_count_in_representative_frame": 2,
            "editorial_role_counts": {"subject_or_local_motion_peak": 3},
            "coverage_board": "/proof/sport.jpg",
        },
        {
            "clip_id": "desk",
            "path": "night/shot-c-interior.mp4",
            "source_sha256": "c" * 64,
            "duration_s": 8.0,
            "orientation": "landscape",
            "camera_motion": {"label": "locked_or_nearly_locked", "translation_speed_p90": 0.002},
            "top_visual_labels": [{"label": "computer", "confidence": 0.9}, {"label": "people", "confidence": 0.7}],
            "semantic_groups": ["technology_or_object_detail", "interior_or_architecture"],
            "duplicate_or_setup_families": [
                {"family_id": "SHOT_C_INTERIOR", "description": "Seated computer work at night."}
            ],
            "pose_count_in_representative_frame": 1,
            "editorial_role_counts": {"subject_or_local_motion_peak": 0},
            "coverage_board": "/proof/desk.jpg",
        },
    ]
    features = {
        "day/shot-a-exterior.mp4": {"luma_p50": 168.0, "sat_p50": 32.0, "laplacian_var": 80.0},
        "day/shot-b-activity.mp4": {"luma_p50": 90.0, "sat_p50": 120.0, "laplacian_var": 70.0},
        "night/shot-c-interior.mp4": {"luma_p50": 18.0, "sat_p50": 60.0, "laplacian_var": 40.0},
    }

    result = shortlist_reference_roles(blueprint, catalog, features=features, top_n=2)

    assert result["status"] == "MACHINE_SHORTLIST_REVIEW_REQUIRED"
    validate_contract("role-shortlist", result)
    assert result["source_count"] == 3
    assert result["role_count"] == 2
    assert result["roles"]["S02"]["candidates"][0]["clip_id"] == "architecture"
    assert result["roles"]["S12"]["candidates"][0]["clip_id"] == "sport"
    assert all(
        candidate["manual_review_status"] == "NOT_REVIEWED"
        for role in result["roles"].values()
        for candidate in role["candidates"]
    )
    assert result["roles"]["S02"]["manual_reject_checks"] == ["generic desk portrait"]


def test_shortlist_prefers_visible_action_role_over_static_scene_overlap() -> None:
    blueprint = {
        "source_lock": {"video": {"fps": "24/1"}},
        "shot_roles": [
            {
                "id": "S03",
                "start_frame": 0,
                "end_frame_exclusive": 12,
                "role": "rear_walk",
                "reference_observation": "Rear-follow walk through interior architecture.",
                "composition": "Subject is centered and walking away from camera.",
                "substitute_requirements": ["Rear or profile walking action must already be visible."],
                "reject_if": ["static standing clip"],
            }
        ],
    }
    catalog = [
        {
            "clip_id": "seated",
            "path": "day/rear-seated.mp4",
            "source_sha256": "a" * 64,
            "duration_s": 4.0,
            "orientation": "landscape",
            "camera_motion": {"label": "locked_or_nearly_locked", "translation_speed_p90": 0.001},
            "top_visual_labels": [{"label": "people", "confidence": 0.9}, {"label": "structure", "confidence": 0.8}],
            "semantic_groups": ["human_action_or_portrait", "interior_or_architecture"],
            "duplicate_or_setup_families": [
                {
                    "family_id": "SHOT_E_SEATED",
                    "description": "Rear profile human subject centered in interior architecture, with motion and walking vocabulary in the metadata, but visibly seated and static at a desk.",
                }
            ],
            "pose_count_in_representative_frame": 1,
            "editorial_role_counts": {"subject_or_local_motion_peak": 0},
        },
        {
            "clip_id": "walk",
            "path": "day/concrete-corridor-walk.mp4",
            "source_sha256": "b" * 64,
            "duration_s": 4.0,
            "orientation": "landscape",
            "camera_motion": {"label": "push_in", "translation_speed_p90": 0.03},
            "top_visual_labels": [{"label": "people", "confidence": 0.8}, {"label": "structure", "confidence": 0.8}],
            "semantic_groups": ["human_action_or_portrait", "interior_or_architecture"],
            "duplicate_or_setup_families": [
                {"family_id": "SHOT_D_CORRIDOR_WALK", "description": "Rear walking passage through concrete interior architecture."}
            ],
            "pose_count_in_representative_frame": 1,
            "editorial_role_counts": {"subject_or_local_motion_peak": 1},
        },
    ]

    result = shortlist_reference_roles(blueprint, catalog, top_n=2)

    assert result["roles"]["S03"]["candidates"][0]["clip_id"] == "walk"


def test_cli_shortlist_writes_a_machine_only_review_artifact(tmp_path: Path, capsys) -> None:
    blueprint = {
        "source_lock": {"video": {"fps": "24/1"}},
        "shot_roles": [
            {
                "id": "S01",
                "start_frame": 0,
                "end_frame_exclusive": 12,
                "role": "discipline_action",
                "reference_observation": "Outdoor sport action.",
                "composition": "Full-body movement.",
                "substitute_requirements": ["Use genuine training action."],
                "reject_if": ["static pose"],
            }
        ],
    }
    catalog_row = {
        "clip_id": "sport",
        "path": "day/shot-b-activity.mp4",
        "source_sha256": "d" * 64,
        "duration_s": 4.0,
        "orientation": "landscape",
        "camera_motion": {"label": "pan_right", "translation_speed_p90": 0.1},
        "top_visual_labels": [{"label": "people", "confidence": 0.9}, {"label": "outdoor", "confidence": 0.8}],
        "semantic_groups": ["human_action_or_portrait", "outdoor_or_environment"],
        "duplicate_or_setup_families": [
            {"family_id": "SHOT_B_ACTIVITY", "description": "Two-person sport and training action."}
        ],
        "pose_count_in_representative_frame": 2,
        "editorial_role_counts": {"subject_or_local_motion_peak": 2},
        "coverage_board": "/proof/sport.jpg",
    }
    features = {"features": [{"path": "day/shot-b-activity.mp4", "luma_p50": 90.0, "sat_p50": 100.0}]}
    blueprint_path = tmp_path / "blueprint.json"
    catalog_path = tmp_path / "catalog.jsonl"
    features_path = tmp_path / "features.json"
    output_path = tmp_path / "shortlist.json"
    blueprint_path.write_text(json.dumps(blueprint), encoding="utf-8")
    catalog_path.write_text(json.dumps(catalog_row) + "\n", encoding="utf-8")
    features_path.write_text(json.dumps(features), encoding="utf-8")

    code = main(
        [
            "footage",
            "shortlist",
            "--blueprint",
            str(blueprint_path),
            "--catalog",
            str(catalog_path),
            "--features",
            str(features_path),
            "--output",
            str(output_path),
            "--top-n",
            "1",
        ],
        exit_on_error=False,
    )

    assert code == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["status"] == "MACHINE_SHORTLIST_REVIEW_REQUIRED"
    assert result["roles"]["S01"]["candidates"][0]["clip_id"] == "sport"
    assert result["engine"]["reelctl_version"] == "0.1.0"
    assert len(result["engine"]["retrieval_module_sha256"]) == 64
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["roles"] == 1
    assert payload["output"] == str(output_path.resolve())


def test_jsonl_catalog_rejects_symlink_input(tmp_path: Path) -> None:
    target = tmp_path / "catalog.jsonl"
    target.write_text('{"clip_id": "one"}\n', encoding="utf-8")
    link = tmp_path / "catalog-link.jsonl"
    link.symlink_to(target)

    with pytest.raises(RetrievalError, match="symlink"):
        load_jsonl_catalog(link)


def test_cli_shortlist_rejects_symlink_catalog_before_reading(tmp_path: Path) -> None:
    blueprint = {
        "source_lock": {"video": {"fps": "24/1"}},
        "shot_roles": [
            {
                "id": "S01",
                "start_frame": 0,
                "end_frame_exclusive": 2,
                "role": "architecture",
                "reference_observation": "Building",
                "composition": "Centered",
                "substitute_requirements": [],
                "reject_if": [],
            }
        ],
    }
    blueprint_path = tmp_path / "blueprint.json"
    blueprint_path.write_text(json.dumps(blueprint), encoding="utf-8")
    target = tmp_path / "catalog.jsonl"
    target.write_text(
        json.dumps(
            {
                "clip_id": "one",
                "path": "one.mp4",
                "source_sha256": "f" * 64,
                "duration_s": 1.0,
                "orientation": "landscape",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    link = tmp_path / "catalog-link.jsonl"
    link.symlink_to(target)

    with pytest.raises(RetrievalError, match="symlink"):
        main(
            [
                "footage",
                "shortlist",
                "--blueprint",
                str(blueprint_path),
                "--catalog",
                str(link),
                "--output",
                str(tmp_path / "shortlist.json"),
            ],
            exit_on_error=False,
        )
