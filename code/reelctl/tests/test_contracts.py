from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from reelctl.color import ColorContractError, validate_color_contract
from reelctl.hashing import recipe_hash, sha256_file
from reelctl.selection import SelectionError, bind_selection_to_inventory, validate_selection
from reelctl.typography import (
    TypographyError,
    create_original_font_reference_match,
    create_traced_glyph_provenance,
    inspect_font_face,
    validate_typography_layer,
)


def test_reference_locked_selection_cannot_collapse_blocks() -> None:
    blueprint = {"picture_blocks": [{"id": "p01", "frames": 4}, {"id": "p02", "frames": 5}]}
    bad = {"slots": [{"block_id": "p01", "frames": 9, "candidate_observation": "different"}]}
    with pytest.raises(SelectionError):
        validate_selection(blueprint, bad, mode="reference-locked")


def test_selection_rejects_source_reuse_without_reference_repeat_group() -> None:
    blueprint = {
        "picture_blocks": [
            {"id": "p01", "frames": 4, "role": "wide work shot"},
            {"id": "p02", "frames": 4, "role": "night city close"},
        ]
    }
    selection = {
        "slots": [
            {
                "block_id": "p01",
                "frames": 4,
                "reference_role": "wide work shot",
                "candidate_observation": "subject typing at the desk",
                "source_path": "/tmp/same.mp4",
                "source_sha256": "a" * 64,
                "source_start_frame": 0,
                "speed": 1.0,
                "reverse": False,
            },
            {
                "block_id": "p02",
                "frames": 4,
                "reference_role": "night city close",
                "candidate_observation": "window lights over the skyline",
                "source_path": "/tmp/same.mp4",
                "source_sha256": "a" * 64,
                "source_start_frame": 12,
                "speed": 1.0,
                "reverse": False,
            },
        ]
    }
    with pytest.raises(SelectionError, match="reused"):
        validate_selection(blueprint, selection, mode="reference-locked")

    blueprint["picture_blocks"][0]["repeat_group"] = "reference-repeat-a"
    blueprint["picture_blocks"][1]["repeat_group"] = "reference-repeat-a"
    assert validate_selection(blueprint, selection, mode="reference-locked")["status"] == "PASS"


def test_candidate_observation_cannot_copy_reference_role() -> None:
    blueprint = {"picture_blocks": [{"id": "p01", "frames": 4, "role": "city at night"}]}
    bad = {
        "slots": [
            {
                "block_id": "p01",
                "frames": 4,
                "reference_role": "city at night",
                "candidate_observation": "city at night",
                "source_path": "/tmp/a.mp4",
                "source_start_frame": 0,
            }
        ]
    }
    with pytest.raises(SelectionError):
        validate_selection(blueprint, bad, mode="reference-locked")


def test_selection_is_bound_to_complete_authorized_inventory_and_current_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"current-source-bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    inventory = {
        "status": "PASS",
        "root": str(tmp_path),
        "expected_count": 1,
        "passed_count": 1,
        "failed_count": 0,
        "missing_count": 0,
        "failures": [],
        "clips": [
            {
                "status": "PASS",
                "path": str(source),
                "relative_path": "source.mp4",
                "sha256": digest,
                "bytes": source.stat().st_size,
                "clip_id": digest[:16],
                "video": {"frame_count": 100, "r_frame_rate": "24/1"},
            }
        ],
    }
    inventory["recipe_hash"] = recipe_hash(inventory)
    blueprint = {"clock": {"fps": "24/1"}, "picture_blocks": [{"id": "p01", "frames": 12, "role": "red field"}]}
    draft = {
        "slots": [
            {
                "block_id": "p01",
                "frames": 12,
                "candidate_observation": "solid red candidate frame",
                "source_path": str(source),
                "source_start_frame": 0,
                "speed": 1.0,
                "reverse": False,
            }
        ]
    }
    bound = bind_selection_to_inventory(draft, inventory)
    feasibility = {
        "blocks": [
            {
                "block_id": "p01",
                "coverage": "exact_scene_available",
                "evidence_clip_ids": [digest[:16]],
            }
        ]
    }
    assert bound["slots"][0]["source_sha256"] == digest
    assert validate_selection(blueprint, bound, mode="reference-locked", inventory=inventory, feasibility=feasibility)["status"] == "PASS"
    source.write_bytes(b"changed-source-bytes")
    with pytest.raises(SelectionError):
        validate_selection(blueprint, bound, mode="reference-locked", inventory=inventory, feasibility=feasibility)


def test_blanket_creative_grade_across_lighting_families_is_rejected() -> None:
    slots = [
        {
            "lighting_family": "day",
            "input_profile": "sony_slog3_sgamut3cine",
            "technical_transform": "sony_lc709",
            "creative": {"exposure_stops": -0.2, "contrast": 1.11, "saturation": 1.08},
        },
        {
            "lighting_family": "night",
            "input_profile": "sony_slog3_sgamut3cine",
            "technical_transform": "sony_lc709",
            "creative": {"exposure_stops": -0.2, "contrast": 1.11, "saturation": 1.08},
        },
    ]
    with pytest.raises(ColorContractError):
        validate_color_contract(slots)


def test_unknown_log_profile_fails_closed() -> None:
    with pytest.raises(ColorContractError):
        validate_color_contract(
            [{"lighting_family": "day", "input_profile": "log_unknown", "technical_transform": "identity", "creative": {}}]
        )


def test_nearest_font_is_not_exact_typography() -> None:
    with pytest.raises(TypographyError):
        validate_typography_layer({"text": "Sample", "proof": "nearest_available", "font_path": "/tmp/f.ttf"})


def test_exact_font_and_traced_glyph_are_two_valid_routes(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    font = Path("/System/Library/Fonts/Helvetica.ttc")
    font_hash = sha256_file(font)
    exact_layer = {
        "text": "Sample",
        "proof": "exact_font",
        "font_path": str(font),
        "font_sha256": font_hash,
        "face_index": 0,
        "font_identity": inspect_font_face(font, 0),
        "font_size": 64,
        "center_xy": [100, 100],
        "fill_rgba": [255, 255, 255, 255],
    }
    exact_layer["reference_match"] = create_original_font_reference_match(
        exact_layer,
        reference,
        asset_origin="test fixture original title asset",
        evidence="font bytes are declared as the fixture's original source",
    )
    assert validate_typography_layer(exact_layer)["status"] == "PASS"

    plate = tmp_path / "glyph.png"
    Image.new("RGBA", (10, 10), (255, 255, 255, 255)).save(plate)
    reference_mask = tmp_path / "glyph-mask.png"
    Image.new("L", (10, 10), 255).save(reference_mask)
    plate_hash = sha256_file(plate)
    provenance = create_traced_glyph_provenance(
        reference,
        reference_mask,
        plate,
        tmp_path / "trace-receipt.json",
        source_frame=12,
        extraction_roi=[0, 0, 10, 10],
        extraction_method="test-mask-v1",
    )
    assert (
        validate_typography_layer(
            {
                "text": "Sample",
                "proof": "traced_reference_glyph",
                "plate_path": str(plate),
                "plate_sha256": plate_hash,
                "source_frame": 12,
                "extraction_roi": [0, 0, 10, 10],
                "provenance_receipt_path": provenance["receipt_path"],
                "provenance_receipt_sha256": provenance["receipt_sha256"],
            }
        )["status"]
        == "PASS"
    )


def test_arbitrary_bytes_cannot_claim_exact_font_or_traced_glyph(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    fake_font = tmp_path / "font.ttf"
    fake_font.write_bytes(b"font")
    with pytest.raises(TypographyError, match="valid OpenType"):
        validate_typography_layer(
            {
                "text": "Sample",
                "proof": "exact_font",
                "font_path": str(fake_font),
                "font_sha256": sha256_file(fake_font),
                "face_index": 0,
                "font_identity": {},
                "font_size": 64,
                "center_xy": [100, 100],
                "fill_rgba": [255, 255, 255, 255],
                "reference_match": {
                    "status": "PASS",
                    "method": "original_asset_provenance",
                    "reference_path": str(reference),
                    "reference_sha256": sha256_file(reference),
                    "asset_origin": "fixture",
                    "evidence": "fixture",
                },
            }
        )

    fake_plate = tmp_path / "plate.png"
    fake_plate.write_bytes(b"plate")
    reference_mask = tmp_path / "reference-mask.png"
    Image.new("L", (10, 10), 255).save(reference_mask)
    with pytest.raises(TypographyError, match="valid RGBA"):
        create_traced_glyph_provenance(
            reference,
            reference_mask,
            fake_plate,
            tmp_path / "trace-receipt.json",
            source_frame=0,
            extraction_roi=[0, 0, 10, 10],
            extraction_method="fixture",
        )


def test_font_face_index_and_identity_are_verified(tmp_path: Path) -> None:
    font = Path("/System/Library/Fonts/Helvetica.ttc")
    with pytest.raises(TypographyError, match="face_index"):
        inspect_font_face(font, 999)

    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    identity = inspect_font_face(font, 0)
    identity["postscript_name"] = "forged-name"
    with pytest.raises(TypographyError, match="identity"):
        validate_typography_layer(
            {
                "text": "Sample",
                "proof": "exact_font",
                "font_path": str(font),
                "font_sha256": sha256_file(font),
                "face_index": 0,
                "font_identity": identity,
                "font_size": 64,
                "center_xy": [100, 100],
                "fill_rgba": [255, 255, 255, 255],
                "reference_match": {
                    "status": "PASS",
                    "method": "original_asset_provenance",
                    "reference_path": str(reference),
                    "reference_sha256": sha256_file(reference),
                    "asset_origin": "fixture",
                    "evidence": "fixture",
                },
            }
        )
