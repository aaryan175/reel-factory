from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from reelctl.cli import _copy_reference, main
from reelctl.errors import ReelctlError
from reelctl.footage import FootageError, index_footage
from reelctl.hashing import atomic_external_output, atomic_write_json, recipe_hash, sha256_file
from reelctl.locks import LockError, project_lock
from reelctl.paths import PathSafetyError, confined_path, secure_mkdirs
from reelctl.render import _run_external_atomic
from reelctl.state import ProjectState, StateError


def test_confined_path_rejects_symlinked_project_component(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "demo").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathSafetyError, match="symlink"):
        confined_path(root, "demo", require="directory")


def test_secure_atomic_json_rejects_symlink_leaf_and_does_not_touch_target(tmp_path: Path) -> None:
    root = tmp_path / "project"
    secure_mkdirs(root, "reference")
    target = tmp_path / "outside.json"
    target.write_text('{"safe": true}\n', encoding="utf-8")
    (root / "reference/lock.json").symlink_to(target)
    with pytest.raises(PathSafetyError, match="symlink"):
        atomic_write_json(root / "reference/lock.json", {"unsafe": True}, root=root)
    assert json.loads(target.read_text(encoding="utf-8")) == {"safe": True}


def test_sha256_file_refuses_symlink_leaf(tmp_path: Path) -> None:
    target = tmp_path / "outside.bin"
    target.write_bytes(b"outside")
    link = tmp_path / "inside.bin"
    link.symlink_to(target)
    with pytest.raises(PathSafetyError, match="symlink"):
        sha256_file(link)


def test_reference_copy_rejects_symlink_destination_without_touching_target(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"new-reference")
    destination_dir = tmp_path / "project/reference"
    destination_dir.mkdir(parents=True)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    (destination_dir / "reference-source.mp4").symlink_to(outside)
    with pytest.raises(ReelctlError, match="symlink"):
        _copy_reference(str(source), destination_dir)
    assert outside.read_bytes() == b"outside"


def test_project_lock_rejects_symlinked_lock_directory(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".reelctl-locks").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LockError, match="symlink"):
        with project_lock(root, "demo"):
            pass


def test_cli_status_rejects_fixed_subdirectory_symlink(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"placeholder")
    footage = tmp_path / "footage"
    footage.mkdir()
    assert (
        main(
            [
                "--projects-root",
                str(projects),
                "new",
                "demo",
                "--reference",
                str(reference),
                "--footage",
                str(footage),
                "--defer-analysis",
            ],
            exit_on_error=False,
        )
        == 0
    )
    original = projects / "demo/reference"
    original.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathSafetyError, match="symlink"):
        main(["--projects-root", str(projects), "status", "demo"], exit_on_error=False)


def test_footage_inventory_rejects_file_symlink_outside_authorized_root(tmp_path: Path) -> None:
    root = tmp_path / "authorized"
    root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not-even-media")
    (root / "escape.mp4").symlink_to(outside)
    with pytest.raises(FootageError, match="symlink"):
        index_footage(root, tmp_path / "output", default_profile="rec709")


def test_stage_receipt_detects_mutated_upstream_artifact_before_transition(tmp_path: Path) -> None:
    state = ProjectState.create(tmp_path / "state.json", "demo")
    artifact = tmp_path / "reference.json"
    artifact.write_text('{"version": 1}\n', encoding="utf-8")
    state.complete("REFERENCE_LOCKED", "ref-v1", ["reference.json"])
    assert state.verify_stage("REFERENCE_LOCKED")["status"] == "PASS"
    artifact.write_text('{"version": 2}\n', encoding="utf-8")
    downstream = tmp_path / "blueprint.json"
    downstream.write_text("{}\n", encoding="utf-8")
    with pytest.raises(StateError, match="artifact changed"):
        state.complete("BLUEPRINT_LOCKED", "blue-v1", ["blueprint.json"])


def test_latest_stage_verification_recursively_rechecks_the_full_predecessor_chain(tmp_path: Path) -> None:
    state = ProjectState.create(tmp_path / "state.json", "demo")
    reference = tmp_path / "reference.json"
    blueprint = tmp_path / "blueprint.json"
    reference.write_text('{"version": 1}\n', encoding="utf-8")
    blueprint.write_text('{"version": 1}\n', encoding="utf-8")
    state.complete("REFERENCE_LOCKED", "ref-v1", ["reference.json"])
    state.complete("BLUEPRINT_LOCKED", "blue-v1", ["blueprint.json"])
    reference.write_text('{"version": 2}\n', encoding="utf-8")
    with pytest.raises(StateError, match="REFERENCE_LOCKED artifact changed"):
        state.verify_stage("BLUEPRINT_LOCKED")


def test_same_stage_same_input_cannot_silently_change_outputs(tmp_path: Path) -> None:
    state = ProjectState.create(tmp_path / "state.json", "demo")
    artifact = tmp_path / "reference.json"
    artifact.write_text('{"version": 1}\n', encoding="utf-8")
    state.complete("REFERENCE_LOCKED", "same-input", ["reference.json"])
    first_receipt = state.stage("REFERENCE_LOCKED")["receipt_sha256"]
    artifact.write_text('{"version": 2}\n', encoding="utf-8")
    with pytest.raises(StateError, match="same input hash"):
        state.complete("REFERENCE_LOCKED", "same-input", ["reference.json"])
    assert state.stage("REFERENCE_LOCKED")["receipt_sha256"] == first_receipt


def test_self_consistent_local_stage_forgery_fails_hmac_authority(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = ProjectState.create(state_path, "demo")
    artifact = tmp_path / "reference.json"
    artifact.write_text('{"version": 1}\n', encoding="utf-8")
    state.complete("REFERENCE_LOCKED", "ref-v1", ["reference.json"])

    artifact.write_text('{"forged": true}\n', encoding="utf-8")
    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    stage = raw_state["stages"]["REFERENCE_LOCKED"]
    receipt_path = tmp_path / stage["receipt"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    forged_output = {
        "path": "reference.json",
        "bytes": artifact.stat().st_size,
        "sha256": sha256_file(artifact),
    }
    receipt["outputs"] = [forged_output]
    receipt["receipt_id"] = recipe_hash({key: value for key, value in receipt.items() if key not in {"receipt_id", "signature"}})
    atomic_write_json(receipt_path, receipt, root=tmp_path)
    stage["output_receipts"] = [forged_output]
    stage["receipt_id"] = receipt["receipt_id"]
    stage["receipt_sha256"] = sha256_file(receipt_path)
    atomic_write_json(state_path, raw_state, root=tmp_path)

    with pytest.raises(StateError, match="signature"):
        ProjectState.load(state_path).verify_stage("REFERENCE_LOCKED")


def test_atomic_json_fsyncs_and_uses_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    atomic_write_json(path, {"ok": True}, root=tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not any(p.name.startswith(f".{path.name}.") for p in tmp_path.iterdir())
    assert os.path.isfile(path)


def test_atomic_external_output_promotes_bytes_without_exposing_partial_target(tmp_path: Path) -> None:
    target = tmp_path / "result.mov"
    with atomic_external_output(target, root=tmp_path) as temporary:
        assert temporary != target
        assert not target.exists()
        temporary.write_bytes(b"complete-media")
    assert target.read_bytes() == b"complete-media"
    assert not temporary.exists()


def test_atomic_external_output_rejects_symlink_destination(tmp_path: Path) -> None:
    outside = tmp_path / "outside.mov"
    outside.write_bytes(b"outside")
    target = tmp_path / "result.mov"
    target.symlink_to(outside)
    with pytest.raises(PathSafetyError, match="symlink"):
        with atomic_external_output(target, root=tmp_path):
            pass
    assert outside.read_bytes() == b"outside"


def test_failed_external_encoder_never_promotes_partial_output(tmp_path: Path) -> None:
    target = tmp_path / "failed.mov"
    command = [
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'partial'); raise SystemExit(7)",
        str(target),
    ]
    with pytest.raises(Exception, match="command failed"):
        _run_external_atomic(command, output=target, root=tmp_path)
    assert not target.exists()
    assert not any(".tmp.mov" in item.name for item in tmp_path.iterdir())
