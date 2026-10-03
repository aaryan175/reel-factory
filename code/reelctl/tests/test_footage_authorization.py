from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from test_render_e2e import prepare_project

from reelctl.assets import validate_assets_manifest
from reelctl.authorization import FootageAuthorizationError, authorized_footage_root
from reelctl.hashing import atomic_write_json, load_json
from reelctl.render import render_project
from reelctl.typography import create_traced_glyph_provenance


def _repoint_first_slot(project: Path, source: Path) -> None:
    selection_path = project / "edit/selection.locked.json"
    selection = load_json(selection_path, root=project)
    selection["slots"][0]["source_path"] = str(source)
    atomic_write_json(selection_path, selection, root=project)


def _copy_authorized_clip(project: Path, destination: Path) -> Path:
    """Copy an already authorized clip byte-for-byte so only its location differs."""
    authorized = Path(load_json(project / "project.json", root=project)["footage_root"]) / "red.mp4"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(authorized.read_bytes())
    return destination


def _traced_glyph_manifest(directory: Path, plate: Path) -> dict:
    rgba = Image.new("RGBA", (16, 16), (255, 255, 255, 255))
    plate.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(plate)
    mask = plate.with_name(f"{plate.stem}-mask.png")
    Image.new("L", (16, 16), 255).save(mask)
    reference = directory / "reference.mp4"
    reference.write_bytes(b"reference-bytes")
    provenance = create_traced_glyph_provenance(
        reference,
        mask,
        plate,
        directory / "trace-receipt.json",
        source_frame=0,
        extraction_roi=[0, 0, 16, 16],
        extraction_method="fixture",
    )
    return {
        "schema_version": 1,
        "typography_layers": [
            {
                "id": "caption",
                "text": "x",
                "proof": "traced_reference_glyph",
                "plate_path": str(plate),
                "plate_sha256": provenance["plate_sha256"],
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


def test_render_refuses_a_selected_source_outside_the_authorized_footage_root(tmp_path: Path) -> None:
    project = prepare_project(tmp_path)
    research_pool = _copy_authorized_clip(project, tmp_path / "research-folder/red.mp4")
    _repoint_first_slot(project, research_pool)
    with pytest.raises(FootageAuthorizationError) as excinfo:
        render_project(project, revision="v001")
    message = str(excinfo.value)
    assert str(research_pool) in message
    assert str(tmp_path / "source-footage") in message


def test_render_accepts_sources_inside_the_authorized_footage_root(tmp_path: Path) -> None:
    project = prepare_project(tmp_path)
    result = render_project(project, revision="v001")
    assert result["status"] == "PASS"
    assert Path(result["review_path"]).is_file()


def test_render_refuses_a_symlink_that_escapes_the_authorized_footage_root(tmp_path: Path) -> None:
    project = prepare_project(tmp_path)
    outside = _copy_authorized_clip(project, tmp_path / "outside/red.mp4")
    escape = tmp_path / "source-footage/escape.mp4"
    escape.symlink_to(outside)
    _repoint_first_slot(project, escape)
    with pytest.raises(FootageAuthorizationError) as excinfo:
        render_project(project, revision="v001")
    assert str(outside) in str(excinfo.value)


def test_render_refuses_a_project_without_a_declared_authorized_footage_root(tmp_path: Path) -> None:
    project = prepare_project(tmp_path)
    config = load_json(project / "project.json", root=project)
    del config["footage_root"]
    atomic_write_json(project / "project.json", config, root=project)
    with pytest.raises(FootageAuthorizationError, match="authorized_footage_root"):
        render_project(project, revision="v001")


def test_assets_refuse_plate_media_outside_the_authorized_footage_root(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    footage_root = tmp_path / "authorized"
    footage_root.mkdir()
    plate = tmp_path / "research-folder/plate.png"
    plate.parent.mkdir()
    Image.new("RGBA", (16, 16), (255, 255, 255, 255)).save(plate)
    manifest = {
        "schema_version": 1,
        "typography_layers": [
            {
                "id": "caption",
                "text": "x",
                "proof": "traced_reference_glyph",
                "plate_path": str(plate),
                "start_frame": 0,
                "end_frame_exclusive": 1,
            }
        ],
        "effect_layers": [],
    }
    with pytest.raises(FootageAuthorizationError) as excinfo:
        validate_assets_manifest(manifest, frame_count=4, root=project_dir, footage_root=footage_root)
    message = str(excinfo.value)
    assert str(plate) in message
    assert str(footage_root) in message


def test_assets_accept_project_owned_and_authorized_footage_media(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    footage_root = tmp_path / "authorized"
    footage_root.mkdir()
    project_manifest = _traced_glyph_manifest(project_dir, project_dir / "assets/plate.png")
    assert validate_assets_manifest(project_manifest, frame_count=4, root=project_dir, footage_root=footage_root)["status"] == "PASS"
    footage_manifest = _traced_glyph_manifest(footage_root, footage_root / "plates/plate.png")
    assert validate_assets_manifest(footage_manifest, frame_count=4, footage_root=footage_root)["status"] == "PASS"


def test_declared_root_prefers_the_explicit_key_and_refuses_conflicting_declarations(tmp_path: Path) -> None:
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    research = tmp_path / "research"
    research.mkdir()
    assert authorized_footage_root({"authorized_footage_root": str(authorized)}) == authorized
    assert authorized_footage_root({"footage_root": str(authorized)}) == authorized
    with pytest.raises(FootageAuthorizationError, match="conflicting"):
        authorized_footage_root({"authorized_footage_root": str(authorized), "footage_root": str(research)})
    with pytest.raises(FootageAuthorizationError, match="authorized_footage_root"):
        authorized_footage_root({"project_id": "demo"})
    with pytest.raises(FootageAuthorizationError, match="does not exist"):
        authorized_footage_root({"authorized_footage_root": str(tmp_path / "missing")})
