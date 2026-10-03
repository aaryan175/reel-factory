from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from reelctl.assets import AssetContractError, compose_overlay_sequence, create_effect_provenance, validate_assets_manifest
from reelctl.hashing import atomic_write_json, load_json, recipe_hash, sha256_file
from reelctl.paths import PathSafetyError
from reelctl.typography import (
    TypographyError,
    create_original_font_reference_match,
    create_traced_glyph_provenance,
    inspect_font_face,
)


def test_traced_glyph_layers_compose_into_exact_frame_sequence(tmp_path: Path) -> None:
    plate = tmp_path / "plate.png"
    rgba = np.zeros((40, 80, 4), dtype=np.uint8)
    rgba[10:30, 20:60, :3] = 255
    rgba[10:30, 20:60, 3] = 255
    Image.fromarray(rgba).save(plate)
    reference_mask = tmp_path / "reference-mask.png"
    Image.fromarray(rgba[..., 3]).save(reference_mask)
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference-bytes")
    provenance = create_traced_glyph_provenance(
        reference,
        reference_mask,
        plate,
        tmp_path / "trace-receipt.json",
        source_frame=7,
        extraction_roi=[0, 0, 80, 40],
        extraction_method="bright-ink-mask-v1",
    )
    manifest = {
        "schema_version": 1,
        "typography_layers": [
            {
                "id": "caption",
                "text": "EXACT",
                "proof": "traced_reference_glyph",
                "plate_path": str(plate),
                "plate_sha256": sha256_file(plate),
                "source_frame": 7,
                "extraction_roi": [0, 0, 80, 40],
                "provenance_receipt_path": provenance["receipt_path"],
                "provenance_receipt_sha256": provenance["receipt_sha256"],
                "start_frame": 1,
                "end_frame_exclusive": 3,
            }
        ],
        "effect_layers": [],
    }
    assert validate_assets_manifest(manifest, frame_count=4)["status"] == "PASS"
    output = tmp_path / "overlay"
    receipt = compose_overlay_sequence(manifest, output, width=80, height=40, frame_count=4)
    assert receipt["frame_count"] == 4
    assert np.asarray(Image.open(output / "000000.png"))[..., 3].max() == 0
    assert np.asarray(Image.open(output / "000001.png"))[..., 3].max() == 255
    assert np.asarray(Image.open(output / "000002.png"))[..., 3].max() == 255
    assert np.asarray(Image.open(output / "000003.png"))[..., 3].max() == 0
    assert compose_overlay_sequence(manifest, output, width=80, height=40, frame_count=4)["cache_hit"] is True
    plate.write_bytes(b"changed")
    with pytest.raises(TypographyError, match="locked RGBA plate"):
        compose_overlay_sequence(manifest, output, width=80, height=40, frame_count=4)


def test_exact_font_bytes_can_render_a_hash_bound_plate(tmp_path: Path) -> None:
    font = Path("/System/Library/Fonts/Helvetica.ttc")
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference-bytes")
    layer = {
        "id": "exact-font",
        "text": "EXACT",
        "proof": "exact_font",
        "font_path": str(font),
        "font_sha256": sha256_file(font),
        "face_index": 0,
        "font_identity": inspect_font_face(font, 0),
        "font_size": 34,
        "center_xy": [80, 20],
        "fill_rgba": [255, 245, 230, 255],
        "tracking_px": 1.0,
        "scale_xy": [1.0, 1.0],
        "start_frame": 0,
        "end_frame_exclusive": 1,
    }
    layer["reference_match"] = create_original_font_reference_match(
        layer,
        reference,
        asset_origin="synthetic test fixture declares Helvetica as the original title asset",
        evidence="fixture construction supplies the original font bytes",
    )
    manifest = {
        "schema_version": 1,
        "typography_layers": [layer],
        "effect_layers": [],
    }
    output = tmp_path / "font-overlay"
    receipt = compose_overlay_sequence(manifest, output, width=160, height=40, frame_count=1)
    assert receipt["frame_count"] == 1
    alpha = np.asarray(Image.open(output / "000000.png"))[..., 3]
    assert alpha.max() == 255
    assert int((alpha > 0).sum()) > 100


def test_signed_font_reference_match_rejects_layer_recipe_tamper(tmp_path: Path) -> None:
    font = Path("/System/Library/Fonts/Helvetica.ttc")
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    layer = {
        "id": "font",
        "text": "EXACT",
        "proof": "exact_font",
        "font_path": str(font),
        "font_sha256": sha256_file(font),
        "face_index": 0,
        "font_identity": inspect_font_face(font, 0),
        "font_size": 34,
        "center_xy": [80, 20],
        "fill_rgba": [255, 255, 255, 255],
        "tracking_px": 0.0,
        "scale_xy": [1.0, 1.0],
        "start_frame": 0,
        "end_frame_exclusive": 1,
    }
    layer["reference_match"] = create_original_font_reference_match(
        layer,
        reference,
        asset_origin="fixture original",
        evidence="fixture original font bytes",
    )
    layer["tracking_px"] = 4.0
    with pytest.raises(TypographyError, match="layer recipe"):
        validate_assets_manifest({"schema_version": 1, "typography_layers": [layer], "effect_layers": []}, frame_count=1)


def test_traced_glyph_receipt_rejects_self_consistent_unkeyed_forgery(tmp_path: Path) -> None:
    plate = tmp_path / "plate.png"
    mask = tmp_path / "mask.png"
    Image.new("RGBA", (8, 8), (255, 255, 255, 255)).save(plate)
    Image.new("L", (8, 8), 255).save(mask)
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    provenance = create_traced_glyph_provenance(
        reference,
        mask,
        plate,
        tmp_path / "trace-receipt.json",
        source_frame=0,
        extraction_roi=[0, 0, 8, 8],
        extraction_method="fixture",
    )
    receipt_path = Path(provenance["receipt_path"])
    forged = load_json(receipt_path)
    forged["source_frame"] = 1
    core = {key: value for key, value in forged.items() if key not in {"receipt_id", "signature"}}
    forged["receipt_id"] = recipe_hash(core)
    atomic_write_json(receipt_path, forged)
    layer = {
        "id": "glyph",
        "text": "x",
        "proof": "traced_reference_glyph",
        "plate_path": str(plate),
        "plate_sha256": sha256_file(plate),
        "source_frame": 1,
        "extraction_roi": [0, 0, 8, 8],
        "provenance_receipt_path": str(receipt_path),
        "provenance_receipt_sha256": sha256_file(receipt_path),
        "start_frame": 0,
        "end_frame_exclusive": 1,
    }
    with pytest.raises(TypographyError, match="signature"):
        validate_assets_manifest({"schema_version": 1, "typography_layers": [layer], "effect_layers": []}, frame_count=1)


def test_overlay_cache_rejects_changed_frame_bytes(tmp_path: Path) -> None:
    plate = tmp_path / "plate.png"
    Image.new("RGBA", (16, 16), (255, 255, 255, 255)).save(plate)
    reference_mask = tmp_path / "reference-mask.png"
    Image.new("L", (16, 16), 255).save(reference_mask)
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    provenance = create_traced_glyph_provenance(
        reference,
        reference_mask,
        plate,
        tmp_path / "trace-receipt.json",
        source_frame=0,
        extraction_roi=[0, 0, 16, 16],
        extraction_method="fixture",
    )
    manifest = {
        "schema_version": 1,
        "typography_layers": [
            {
                "id": "caption",
                "text": "x",
                "proof": "traced_reference_glyph",
                "plate_path": str(plate),
                "plate_sha256": sha256_file(plate),
                "source_frame": 0,
                "extraction_roi": [0, 0, 16, 16],
                "provenance_receipt_path": provenance["receipt_path"],
                "provenance_receipt_sha256": provenance["receipt_sha256"],
                "start_frame": 0,
                "end_frame_exclusive": 1,
            }
        ],
        "effect_layers": [],
    }
    output = tmp_path / "overlay"
    compose_overlay_sequence(manifest, output, width=16, height=16, frame_count=1)
    (output / "000000.png").write_bytes(b"tampered")
    with pytest.raises(AssetContractError, match="matching receipt"):
        compose_overlay_sequence(manifest, output, width=16, height=16, frame_count=1)


def test_overlay_output_directory_symlink_is_rejected(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    output = tmp_path / "overlay"
    output.symlink_to(external, target_is_directory=True)
    manifest = {"schema_version": 1, "typography_layers": [], "effect_layers": []}
    with pytest.raises(PathSafetyError):
        compose_overlay_sequence(manifest, output, width=16, height=16, frame_count=1, root=tmp_path)


def test_overlay_cache_rejects_self_consistent_unkeyed_receipt_forgery(tmp_path: Path) -> None:
    manifest = {"schema_version": 1, "typography_layers": [], "effect_layers": []}
    output = tmp_path / "overlay"
    compose_overlay_sequence(manifest, output, width=16, height=16, frame_count=1)
    frame = output / "000000.png"
    frame.write_bytes(b"forged-frame")
    receipt_path = output / "overlay-receipt.json"
    receipt = load_json(receipt_path, root=output)
    receipt["frame_sha256"] = [sha256_file(frame)]
    atomic_write_json(receipt_path, receipt, root=output)
    with pytest.raises(AssetContractError, match="signature"):
        compose_overlay_sequence(manifest, output, width=16, height=16, frame_count=1)


def test_effect_layer_requires_signed_reference_bound_provenance(tmp_path: Path) -> None:
    plate = tmp_path / "effect.png"
    Image.new("RGBA", (16, 16), (0, 80, 255, 128)).save(plate)
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    layer = {
        "id": "flash",
        "proof": "reference_extracted_plate",
        "plate_path": str(plate),
        "plate_sha256": sha256_file(plate),
        "source_frame": 4,
        "start_frame": 4,
        "end_frame_exclusive": 5,
    }
    provenance = create_effect_provenance(
        layer,
        reference,
        tmp_path / "effect-provenance.json",
        frame_count=8,
        evidence="reference frame 4 contains the measured blue flash plate",
    )
    layer["provenance_receipt_path"] = provenance["receipt_path"]
    layer["provenance_receipt_sha256"] = provenance["receipt_sha256"]
    manifest = {"schema_version": 1, "typography_layers": [], "effect_layers": [layer]}
    assert validate_assets_manifest(manifest, frame_count=8)["status"] == "PASS"

    receipt_path = Path(provenance["receipt_path"])
    forged = load_json(receipt_path)
    forged["evidence"] = "arbitrary blue effect falsely relabelled as reference-derived"
    atomic_write_json(receipt_path, forged)
    layer["provenance_receipt_sha256"] = sha256_file(receipt_path)
    with pytest.raises(AssetContractError, match="signature"):
        validate_assets_manifest(manifest, frame_count=8)


def test_effect_sequence_symlink_frame_is_rejected(tmp_path: Path) -> None:
    effects = tmp_path / "effects"
    effects.mkdir()
    external = tmp_path / "external.png"
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(external)
    (effects / "000000.png").symlink_to(external)
    manifest = {
        "schema_version": 1,
        "typography_layers": [],
        "effect_layers": [
            {
                "id": "effect",
                "proof": "reference_extracted_plate",
                "sequence_pattern": str(effects / "%06d.png"),
                "start_frame": 0,
                "end_frame_exclusive": 1,
            }
        ],
    }
    with pytest.raises(PathSafetyError):
        validate_assets_manifest(manifest, frame_count=1, root=tmp_path)
