from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from . import __version__
from .assets import compose_overlay_sequence, validate_assets_manifest
from .authorization import authorized_footage_root
from .captions_cli import (
    command_captions_ink,
    command_captions_program,
    command_captions_qc,
    command_captions_render,
    command_captions_validate,
)
from .color import create_camera_profile_proof, validate_color_contract
from .contracts import validate_contract
from .errors import ReelctlError
from .feasibility import feasibility_template, validate_feasibility
from .fixtures import audit_real_fixtures
from .footage import index_footage
from .grading import install_color_profile, propose_source_aware_grades
from .hashing import atomic_external_output, atomic_write_json, load_json, recipe_hash, sha256_file
from .identifiers import validate_identifier
from .locks import project_lock
from .paths import canonical_root, confined_path, secure_mkdirs, validate_project_tree
from .qc import run_qc
from .reference import analyze_reference, lock_blueprint
from .render import render_project
from .retrieval import load_feature_map, load_jsonl_catalog, shortlist_reference_roles
from .selection import bind_selection_to_inventory, validate_selection
from .signing import sign_payload
from .state import ProjectState
from .studio.authority import require_factory_open
from .studio.config import DEFAULT_PORT as DEFAULT_STUDIO_PORT
from .typography import discover_fonts, extract_ink_mask, match_fonts, trace_glyph_plate
from .variants import validate_trial_family

DEFAULT_PROJECTS_ROOT = Path(os.environ.get("REEL_FACTORY_HOME") or "~/reel-production").expanduser()
FIXED_PROJECT_DIRECTORIES = ("reference", "footage", "edit", "assets", "review", "deliver", ".reelctl", ".reelctl/cache")


def _print(payload: Any, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(payload, indent=None, sort_keys=True, ensure_ascii=False))
    else:
        print(payload)


def _project(root: Path, project_id: str) -> Path:
    safe_id = validate_identifier(project_id, kind="project")
    trusted_root = canonical_root(root)
    return confined_path(trusted_root, safe_id)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _load_project(root: Path, project_id: str) -> tuple[Path, Dict[str, Any], ProjectState]:
    directory = _project(root, project_id)
    if not directory.exists():
        raise ReelctlError(f"unknown reel project: {project_id}")
    validate_project_tree(directory, fixed_directories=FIXED_PROJECT_DIRECTORIES)
    config_path = confined_path(directory, "project.json", require="file", allow_missing=False)
    state_path = confined_path(directory, "state.json", require="file", allow_missing=False)
    config = load_json(config_path, root=directory)
    validate_contract("project", config)
    return directory, config, ProjectState.load(state_path)


def _copy_licensed_audio(source: str, destination_dir: Path) -> Path:
    """Lock a user-supplied licensed audio track into the project (byte copy, never re-encoded)."""
    destination_dir = canonical_root(destination_dir, create=True)
    path = Path(source).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ReelctlError(f"licensed audio track does not exist or is a symlink: {path}")
    destination = destination_dir / f"licensed-audio{path.suffix.lower() or '.m4a'}"
    if destination.is_symlink():
        raise ReelctlError(f"licensed audio destination is a symlink: {destination}")
    if destination.exists():
        if sha256_file(destination) != sha256_file(path):
            raise ReelctlError(f"licensed audio track already exists with different bytes: {destination}")
        return destination
    with atomic_external_output(destination, root=destination_dir) as temporary:
        shutil.copyfile(path, temporary)
    return destination


REFERENCE_DOWNLOAD_NOTICE = (
    "NOTICE: downloading a reference from a URL. You are responsible for respecting the platform's terms of "
    "service and the creator's rights; keep downloaded references as private study material and never use them "
    "as footage. The recommended input is a local reference file path you are allowed to use."
)


def _copy_reference(source: str, destination_dir: Path) -> Path:
    destination_dir = canonical_root(destination_dir, create=True)
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        destination = destination_dir / "reference-source.mp4"
        if destination.is_symlink():
            raise ReelctlError(f"locked reference destination is a symlink: {destination}")
        if destination.exists():
            return destination
        print(REFERENCE_DOWNLOAD_NOTICE, file=sys.stderr)
        try:
            import yt_dlp
        except ImportError as exc:
            raise ReelctlError("yt-dlp is required for reference URLs") from exc
        try:
            with atomic_external_output(destination, root=destination_dir) as temporary:
                options = {
                    "noplaylist": True,
                    "quiet": False,
                    "no_warnings": False,
                    "overwrites": True,
                    "format": "bv*+ba/b",
                    "merge_output_format": "mp4",
                    "outtmpl": str(temporary),
                }
                with yt_dlp.YoutubeDL(options) as downloader:
                    code = downloader.download([source])
                if code not in {None, 0}:
                    raise ReelctlError("yt-dlp did not complete the exact reference download")
        except Exception as exc:
            raise ReelctlError(
                "reference URL download failed. Do not guess or substitute another copy; save the exact reel locally and rerun `reelctl reference analyze --source /path/to/file`."
            ) from exc
        if not destination.is_file():
            raise ReelctlError("yt-dlp reported success but did not create the locked reference")
        return destination
    path = Path(source).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ReelctlError(f"reference does not exist or is a symlink: {path}")
    destination = destination_dir / f"reference-source{path.suffix.lower() or '.mp4'}"
    if destination.is_symlink():
        raise ReelctlError(f"locked reference destination is a symlink: {destination}")
    if destination.exists():
        if sha256_file(destination) != sha256_file(path):
            raise ReelctlError(f"locked reference already exists with different bytes: {destination}")
        return destination
    with atomic_external_output(destination, root=destination_dir) as temporary:
        shutil.copyfile(path, temporary)
    return destination


def command_new(args: argparse.Namespace) -> Dict[str, Any]:
    directory = _project(args.projects_root, args.project_id)
    if directory.exists():
        raise ReelctlError(f"refusing existing project directory: {directory}")
    secure_mkdirs(args.projects_root, args.project_id)
    for child in FIXED_PROJECT_DIRECTORIES:
        secure_mkdirs(directory, child)
    audio_policy = "reference_audio_rights_held" if getattr(args, "reference_audio_rights_held", False) else "licensed_track_required"
    config = {
        "schema_version": 1,
        "project_id": args.project_id,
        "mode": args.mode,
        "reference_input": args.reference,
        "reference_path": None,
        "audio_track": None,
        "footage_root": str(Path(args.footage).expanduser().resolve()),
        "output": {"master_codec": "prores_ks", "master_pix_fmt": "yuv422p10le", "review_codec": "libx264", "color": "bt709"},
        "policies": {
            "audio": audio_policy,
            "timing": "exact_reference_pts_and_picture_blocks",
            "typography": "exact_font_hash_or_traced_reference_glyph",
            "speed": "normal_only_no_reverse",
            "color": "identified_input_profile_then_per_shot_grade",
            "publication": "human_approval_required",
        },
    }
    if getattr(args, "audio_track", None):
        config["audio_track"] = str(_copy_licensed_audio(args.audio_track, directory / "assets"))
    validate_contract("project", config)
    atomic_write_json(directory / "project.json", config, root=directory)
    state = ProjectState.create(directory / "state.json", args.project_id)
    if not args.defer_analysis:
        locked = _copy_reference(args.reference, directory / "reference")
        config["reference_path"] = str(locked)
        atomic_write_json(directory / "project.json", config, root=directory)
        lock = analyze_reference(locked, directory / "reference")
        state.complete(
            "REFERENCE_LOCKED",
            lock["recipe_hash"],
            [
                "project.json",
                "reference/reference-lock.json",
                "reference/blueprint-draft-board.jpg",
                "reference/reference-all-frames-board.jpg",
            ],
        )
    return {"status": "PASS", "project_id": args.project_id, "project_dir": str(directory), "next_stage": state.next_stage()}


def command_status(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    result = state.summary()
    result.update({"project_dir": str(directory), "mode": config["mode"]})
    return result


def command_reference_analyze(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    source = args.source or config["reference_input"]
    locked = _copy_reference(source, directory / "reference")
    config["reference_input"] = source
    config["reference_path"] = str(locked)
    atomic_write_json(directory / "project.json", config, root=directory)
    lock = analyze_reference(locked, directory / "reference")
    state.complete(
        "REFERENCE_LOCKED",
        lock["recipe_hash"],
        [
            "project.json",
            "reference/reference-lock.json",
            "reference/blueprint-draft-board.jpg",
            "reference/reference-all-frames-board.jpg",
        ],
    )
    return {
        "status": "PASS",
        "reference": str(locked),
        "sha256": lock["source"]["sha256"],
        "draft_boundaries_after": lock["draft_boundaries_after"],
        "blueprint_board": str(directory / "reference/blueprint-draft-board.jpg"),
        "all_frames_board": str(directory / "reference/reference-all-frames-board.jpg"),
        "next_stage": state.next_stage(),
    }


def _parse_boundaries(value: str) -> List[int]:
    if not value.strip():
        return []
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def command_blueprint_lock(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    state.require_predecessor("BLUEPRINT_LOCKED")
    reference_lock = load_json(directory / "reference/reference-lock.json", root=directory)
    boundaries = reference_lock["draft_boundaries_after"] if args.accept_draft else _parse_boundaries(args.boundaries)
    hard_cuts = boundaries if not args.hard_cuts else _parse_boundaries(args.hard_cuts)
    observations = load_json(Path(args.observations).expanduser().resolve()) if args.observations else None
    blueprint = lock_blueprint(
        reference_lock,
        boundaries,
        directory / "reference/blueprint.json",
        hard_cuts_after=hard_cuts,
        observations=observations,
        all_frames_reviewed=args.all_frames_reviewed,
    )
    validate_contract("blueprint", blueprint)
    state.complete("BLUEPRINT_LOCKED", blueprint["sha256_contract"], ["reference/blueprint.json"])
    return {
        "status": "PASS",
        "picture_blocks": len(blueprint["picture_blocks"]),
        "hard_cuts": len(blueprint["hard_cuts_after"]),
        "next_stage": state.next_stage(),
    }


def _footage_stage_outputs(directory: Path, manifest: Dict[str, Any]) -> List[str]:
    outputs = ["footage/footage-index.json"]
    for clip in manifest.get("clips", []):
        for receipt in clip.get("thumbnail_receipts", []):
            path = confined_path(directory, Path(str(receipt["path"])), require="file", allow_missing=False)
            outputs.append(str(path.relative_to(directory)))
    return outputs


def command_footage_index(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    state.require_predecessor("FOOTAGE_INDEXED")
    footage_root = Path(args.footage or config["footage_root"]).expanduser().absolute()
    if _is_within(directory, footage_root):
        raise ReelctlError(
            "project output directory is inside the footage root; use a source-only root so references/renders cannot contaminate inventory"
        )
    manifest = index_footage(
        footage_root,
        directory / "footage",
        default_profile=args.default_profile,
        project_root=directory,
    )
    state.complete("FOOTAGE_INDEXED", manifest["recipe_hash"], _footage_stage_outputs(directory, manifest))
    return {"status": "PASS", "clip_count": manifest["clip_count"], "next_stage": state.next_stage()}


def command_footage_shortlist(args: argparse.Namespace) -> Dict[str, Any]:
    blueprint_path = Path(args.blueprint).expanduser().absolute()
    catalog_path = Path(args.catalog).expanduser().absolute()
    features_path = Path(args.features).expanduser().absolute() if args.features else None
    output_path = Path(args.output).expanduser().absolute()
    blueprint = load_json(blueprint_path)
    catalog = load_jsonl_catalog(catalog_path)
    features = load_feature_map(features_path)
    result = shortlist_reference_roles(blueprint, catalog, features=features, top_n=args.top_n)
    result["inputs"] = {
        "blueprint": str(blueprint_path),
        "blueprint_sha256": sha256_file(blueprint_path),
        "catalog": str(catalog_path),
        "catalog_sha256": sha256_file(catalog_path),
        "features": str(features_path) if features_path else None,
        "features_sha256": sha256_file(features_path) if features_path else None,
    }
    result["engine"] = {
        "reelctl_version": __version__,
        "retrieval_module_sha256": sha256_file(Path(__file__).with_name("retrieval.py")),
        "role_shortlist_schema_sha256": sha256_file(Path(__file__).with_name("schemas") / "role-shortlist.schema.json"),
    }
    result["recipe_hash"] = recipe_hash(result)
    validate_contract("role-shortlist", result)
    atomic_write_json(output_path, result)
    return {
        "status": result["status"],
        "output": str(output_path),
        "sources": result["source_count"],
        "roles": result["role_count"],
        "top_n": result["top_n"],
        "recipe_hash": result["recipe_hash"],
    }


def command_variants_validate(args: argparse.Namespace) -> Dict[str, Any]:
    manifest_path = Path(args.manifest).expanduser().absolute()
    output_path = Path(args.output).expanduser().absolute()
    manifest = load_json(manifest_path)
    contract = validate_contract("trial-family", manifest)
    result = validate_trial_family(manifest)
    result["manifest"] = str(manifest_path)
    result["manifest_sha256"] = sha256_file(manifest_path)
    result["trial_family_schema_sha256"] = contract["schema_sha256"]
    result["engine"] = {
        "reelctl_version": __version__,
        "variants_module_sha256": sha256_file(Path(__file__).with_name("variants.py")),
    }
    result["recipe_hash"] = recipe_hash(result)
    atomic_write_json(output_path, result)
    return {
        "status": result["status"],
        "family_id": result["family_id"],
        "variants": result["variant_count"],
        "output": str(output_path),
        "recipe_hash": result["recipe_hash"],
    }


def command_feasibility_template(args: argparse.Namespace) -> Dict[str, Any]:
    directory, _config, state = _load_project(args.projects_root, args.project_id)
    state.require_predecessor("FEASIBILITY_REPORTED")
    blueprint = load_json(directory / "reference/blueprint.json", root=directory)
    path = directory / "edit/feasibility.json"
    if path.exists() and not args.force:
        raise ReelctlError(f"feasibility draft already exists: {path}")
    atomic_write_json(path, feasibility_template(blueprint), root=directory)
    return {"status": "PASS", "path": str(path), "blocks": len(blueprint["picture_blocks"])}


def command_feasibility_lock(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    state.require_predecessor("FEASIBILITY_REPORTED")
    blueprint = load_json(directory / "reference/blueprint.json", root=directory)
    inventory = load_json(directory / "footage/footage-index.json", root=directory)
    source = Path(args.manifest).expanduser().resolve() if args.manifest else directory / "edit/feasibility.json"
    manifest = load_json(source)
    validate_contract("feasibility", manifest)
    report = validate_feasibility(blueprint, manifest, inventory=inventory, mode=config["mode"])
    locked = dict(manifest)
    locked["validation"] = report
    locked["status"] = report["status"]
    validate_contract("feasibility", locked)
    path = directory / "edit/feasibility.locked.json"
    atomic_write_json(path, locked, root=directory)
    digest = recipe_hash({"blueprint": blueprint.get("sha256_contract"), "feasibility": locked})
    state.complete(
        "FEASIBILITY_REPORTED",
        digest,
        ["edit/feasibility.locked.json"],
        status="PASS" if report["status"] == "PASS" else "BLOCKED",
        reason=(
            "reference roles are missing or exact-scene coverage is below the 80% reference-locked threshold; acquire footage or switch to original-montage"
            if report["status"] == "BLOCKED"
            else None
        ),
    )
    return {**report, "path": str(path), "next_stage": state.next_stage()}


def command_selection_validate(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    state.require_predecessor("SELECTION_LOCKED")
    blueprint = load_json(directory / "reference/blueprint.json", root=directory)
    feasibility = load_json(directory / "edit/feasibility.locked.json", root=directory)
    inventory = load_json(directory / "footage/footage-index.json", root=directory)
    feasibility_report = validate_feasibility(blueprint, feasibility, inventory=inventory, mode=config["mode"])
    if feasibility_report["status"] != "PASS":
        raise ReelctlError("feasibility remains blocked by missing roles or insufficient exact-scene coverage; selection cannot be locked")
    path = Path(args.selection).expanduser().resolve() if args.selection else directory / "edit/selection.json"
    draft = load_json(path)
    validate_contract("selection", draft)
    for slot in draft.get("slots", draft.get("shots", [])):
        if slot.get("technical_lut"):
            lut = Path(str(slot["technical_lut"])).expanduser()
            if not lut.is_absolute():
                lut = directory / lut
            slot["technical_lut"] = str(lut.resolve())
    selection = bind_selection_to_inventory(draft, inventory)
    validate_contract("selection", selection)
    result = validate_selection(blueprint, selection, mode=config["mode"], inventory=inventory, feasibility=feasibility)
    validate_color_contract(selection["slots"])
    locked = directory / "edit/selection.locked.json"
    atomic_write_json(locked, selection, root=directory)
    input_hash = recipe_hash({"blueprint": blueprint.get("sha256_contract"), "selection": selection})
    state.complete("SELECTION_LOCKED", input_hash, ["edit/selection.locked.json"])
    return {**result, "selection": str(locked), "next_stage": state.next_stage()}


def command_selection_template(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    state.require_predecessor("SELECTION_LOCKED")
    blueprint = load_json(directory / "reference/blueprint.json", root=directory)
    slots = []
    for block in blueprint["picture_blocks"]:
        slots.append(
            {
                "block_id": block["id"],
                "frames": block["frames"],
                "reference_role": block.get("role", "UNOBSERVED"),
                "candidate_observation": "REPLACE_WITH_INDEPENDENT_VISIBLE_OBSERVATION",
                "source_path": "REPLACE_WITH_EXACT_SOURCE_PATH",
                "source_start_frame": 0,
                "speed": 1.0,
                "reverse": False,
                "crop_anchor_xy": [0.5, 0.5],
                "lighting_family": "REPLACE_WITH_DAY_NIGHT_INTERIOR_ETC",
                "input_profile": "PENDING_PROFILE_IDENTIFICATION",
                "input_range": "PENDING_RANGE_IDENTIFICATION",
                "profile_proof": {
                    "schema_version": 1,
                    "status": "PENDING_PROFILE_IDENTIFICATION",
                    "method": "PENDING_PROFILE_IDENTIFICATION",
                    "source_sha256": "PENDING_INVENTORY_BINDING",
                    "input_range": "PENDING_RANGE_IDENTIFICATION",
                    "evidence": "PENDING_PROFILE_IDENTIFICATION",
                    "signature": {},
                },
                "technical_transform": "PENDING_PROFILE_IDENTIFICATION",
                "creative": {"exposure_stops": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0},
                "grade_proof": {"status": "PENDING_SOURCE_AWARE_GRADE_REVIEW"},
            }
        )
    path = directory / "edit/selection.json"
    if path.exists() and not args.force:
        raise ReelctlError(f"selection template already exists: {path}")
    atomic_write_json(path, {"schema_version": 1, "status": "DRAFT", "slots": slots}, root=directory)
    return {
        "status": "PASS",
        "path": str(path),
        "slots": len(slots),
        "warning": "This is a draft. The reviewing agent must independently observe every candidate, choose an exact normal-speed source window, prove source profile/range, keep creative grading at identity by default, and only apply a bounded per-shot correction after reviewing its source/reference board.",
    }


def command_assets_template(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    state.require_predecessor("ASSETS_LOCKED")
    path = directory / "assets/assets.json"
    if path.exists() and not args.force:
        raise ReelctlError(f"assets template already exists: {path}")
    atomic_write_json(
        path,
        {"schema_version": 1, "status": "DRAFT", "typography_layers": [], "effect_layers": []},
        root=directory,
    )
    return {
        "status": "PASS",
        "path": str(path),
        "warning": "Empty layers are valid only when the locked reference visibly has no typography/effects. Otherwise require a signed, layer-recipe-bound exact-font match or traced reference mask/plate, and a signed reference-bound receipt for every effect.",
    }


def command_assets_lock(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    state.require_predecessor("ASSETS_LOCKED")
    reference_lock = load_json(directory / "reference/reference-lock.json", root=directory)
    frame_count = int(reference_lock["clock"]["frame_count"])
    width = int(reference_lock["clock"]["width"])
    height = int(reference_lock["clock"]["height"])
    footage_root = authorized_footage_root(config)
    source = Path(args.manifest).expanduser().resolve() if args.manifest else directory / "assets/assets.json"
    manifest = load_json(source)
    validate_contract("assets", manifest)
    validation = validate_assets_manifest(manifest, frame_count=frame_count, root=directory, footage_root=footage_root)
    locked = dict(manifest)
    if manifest.get("typography_layers") or manifest.get("effect_layers"):
        sequence_dir = directory / "assets/overlay-locked"
        receipt = compose_overlay_sequence(
            manifest,
            sequence_dir,
            width=width,
            height=height,
            frame_count=frame_count,
            root=directory,
            footage_root=footage_root,
        )
        locked["overlay_sequence"] = receipt["pattern"]
        locked["overlay_recipe_hash"] = receipt["recipe_hash"]
        locked["overlay_receipt_sha256"] = sha256_file(sequence_dir / "overlay-receipt.json")
    else:
        locked["overlay_sequence"] = None
    locked["status"] = "PASS"
    locked["validation"] = validation
    validate_contract("assets", locked)
    locked_path = directory / "assets/assets.locked.json"
    atomic_write_json(locked_path, locked, root=directory)
    digest = recipe_hash(locked)
    state.complete("ASSETS_LOCKED", digest, ["assets/assets.locked.json"])
    return {"status": "PASS", "path": str(locked_path), "layers": validation["layers"], "next_stage": state.next_stage()}


def command_render(args: argparse.Namespace) -> Dict[str, Any]:
    validate_identifier(args.revision, kind="revision")
    directory, config, state = _load_project(args.projects_root, args.project_id)
    state.require_predecessor("RENDERED")
    result = render_project(directory, revision=args.revision)
    state.complete(
        "RENDERED",
        result["recipe_hash"],
        [
            str(Path(result["master_path"]).relative_to(directory)),
            str(Path(result["review_path"]).relative_to(directory)),
            str(Path(result["render_receipt_path"]).relative_to(directory)),
        ],
    )
    return {**result, "next_stage": state.next_stage()}


def command_review_record_agent(args: argparse.Namespace) -> Dict[str, Any]:
    validate_identifier(args.revision, kind="revision")
    directory, config, state = _load_project(args.projects_root, args.project_id)
    state.verify_stage("RENDERED")
    render_receipt_path = directory / "edit" / f"render-{args.revision}" / "render-receipt.json"
    receipt = load_json(render_receipt_path, root=directory)
    candidate_hash = receipt["review"]["sha256"]
    board = directory / "review" / f"qc-{args.revision}" / "reference-candidate-board.jpg"
    if not board.is_file():
        raise ReelctlError("run reelctl qc once to generate the exact comparison board before recording visual review")
    reference_lock = load_json(directory / "reference/reference-lock.json", root=directory)
    checks = {
        "normal_speed_full_watch": args.normal_speed_full_watch,
        "reference_side_by_side_checked": args.reference_side_by_side_checked,
        "typography_checked": args.typography_checked,
        "color_checked": args.color_checked,
        "cut_and_beat_checked": args.cut_and_beat_checked,
    }
    if args.status == "PASS" and not all(checks.values()):
        raise ReelctlError("agent visual PASS requires every inspection flag")
    result = {
        "schema_version": 1,
        "candidate_sha256": candidate_hash,
        "status": args.status,
        **checks,
        "reviewer": "agent-vision",
        "reelctl_version": __version__,
        "reference_sha256": reference_lock["source"]["sha256"],
        "comparison_board_sha256": sha256_file(board),
        "render_receipt_sha256": sha256_file(render_receipt_path),
        "notes": args.notes,
    }
    result = sign_payload(result, purpose="agent-visual-review-v1")
    path = directory / "review" / f"agent-visual-review-{args.revision}.json"
    atomic_write_json(path, result, root=directory)
    return {"status": "PASS", "receipt": str(path), "visual_verdict": args.status, "candidate_sha256": candidate_hash}


def command_qc(args: argparse.Namespace) -> Dict[str, Any]:
    validate_identifier(args.revision, kind="revision")
    directory, config, state = _load_project(args.projects_root, args.project_id)
    state.require_predecessor("TECHNICAL_QC")
    report = run_qc(directory, revision=args.revision)
    candidate_hash = report["candidate_sha256"]
    state.complete(
        "TECHNICAL_QC", candidate_hash, [str(Path(report["report_path"]).relative_to(directory))], status=report["technical"]["status"]
    )
    if report["technical"]["status"] == "PASS":
        state.complete(
            "STRUCTURE_QC", candidate_hash, [str(Path(report["report_path"]).relative_to(directory))], status=report["structure"]["status"]
        )
    if report["technical"]["status"] == report["structure"]["status"] == "PASS":
        state.complete(
            "VISUAL_QC",
            candidate_hash,
            [str(Path(report["report_path"]).relative_to(directory))],
            status=report["visual"]["status"],
            reason=report["visual"].get("reason"),
        )
    if report["authorities"]["local_review_ready"]:
        state.complete("LOCAL_REVIEW_READY", candidate_hash, [report["candidate"]])
    return {
        "status": report["authorities"]["overall"],
        "report": report["report_path"],
        "candidate": report["candidate"],
        "candidate_sha256": candidate_hash,
        "authorities": report["authorities"],
        "next_stage": state.next_stage(),
    }


def command_typography_inventory(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    records = discover_fonts([Path(p).expanduser() for p in args.extra_dir])
    path = directory / "assets/font-inventory.json"
    atomic_write_json(path, {"schema_version": 1, "count": len(records), "fonts": records}, root=directory)
    return {"status": "PASS", "count": len(records), "path": str(path)}


def command_typography_match(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    inventory = load_json(Path(args.inventory) if args.inventory else directory / "assets/font-inventory.json")["fonts"]
    results = match_fonts(args.text, Path(args.mask), inventory, limit=args.limit)
    path = directory / "assets/font-match-candidates.json"
    atomic_write_json(
        path,
        {
            "text": args.text,
            "mask": str(Path(args.mask).resolve()),
            "candidates": results,
            "warning": "a high silhouette score is evidence, not proof; exact mode still requires the matching font bytes or traced glyph plate",
        },
        root=directory,
    )
    return {"status": "PASS", "candidates": results, "path": str(path)}


def command_typography_trace(args: argparse.Namespace) -> Dict[str, Any]:
    result = trace_glyph_plate(Path(args.mask), Path(args.output), rgba=tuple(args.rgba))
    return {"status": "PASS", **result}


def command_typography_extract(args: argparse.Namespace) -> Dict[str, Any]:
    roi = tuple(args.roi) if args.roi else None
    result = extract_ink_mask(Path(args.image), Path(args.output), roi=roi, ink=args.ink)
    return {"status": "PASS", **result, "warning": "Inspect this mask against the reference before using traced_reference_glyph proof."}


def command_color_identify(args: argparse.Namespace) -> Dict[str, Any]:
    source = Path(args.source).expanduser().resolve()
    output = Path(args.output).expanduser().resolve() if args.output else source.with_suffix(source.suffix + ".color-profile.json")
    proof = create_camera_profile_proof(source, output)
    return {
        "status": "PASS",
        "source": str(source),
        "source_sha256": proof["source_sha256"],
        "profile": "sony_slog3_sgamut3cine",
        "input_range": proof["input_range"],
        "proof": str(output),
        "proof_sha256": sha256_file(output),
    }


def command_color_validate(args: argparse.Namespace) -> Dict[str, Any]:
    data = load_json(Path(args.selection))
    return validate_color_contract(data.get("slots", data.get("shots", [])))


def command_color_setup(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    return install_color_profile(directory, args.profile)


def command_color_propose(args: argparse.Namespace) -> Dict[str, Any]:
    directory, config, state = _load_project(args.projects_root, args.project_id)
    path = Path(args.selection).expanduser().resolve() if args.selection else None
    report = propose_source_aware_grades(directory, selection_path=path)
    return {
        "status": report["status"],
        "board": report["board"],
        "report": str(directory / "review/color-grade-proposal/grade-proposals.json"),
        "proposals": len(report["proposals"]),
    }


def command_serve(args: argparse.Namespace) -> Dict[str, Any]:
    from .web import run_server

    run_server(projects_root=args.projects_root, host=args.host, port=args.port)
    return {"status": "PASS"}


def command_studio_daemon(args: argparse.Namespace) -> Dict[str, Any]:
    from .studio.config import StudioConfig
    from .studio.daemon import Orchestrator
    from .studio.judgment import judgment_adapter

    config = StudioConfig.from_env().with_projects_root(args.projects_root)
    orchestrator = Orchestrator(config, judgment=judgment_adapter(config, enabled=not args.no_judgment))
    orchestrator.start()
    if args.once:
        return orchestrator.tick()
    return orchestrator.run_forever(interval=args.interval)


def command_doctor(args: argparse.Namespace) -> Dict[str, Any]:
    executables = {}
    for name in ("ffmpeg", "ffprobe", "yt-dlp", "tesseract"):
        executables[name] = shutil.which(name)
    missing_required = [name for name in ("ffmpeg", "ffprobe") if not executables[name]]
    return {
        "status": "FAIL" if missing_required else "PASS",
        "version": __version__,
        "python": sys.version,
        "executables": executables,
        "missing_required": missing_required,
    }


def command_fixtures_audit(args: argparse.Namespace) -> Dict[str, Any]:
    return audit_real_fixtures(Path(args.manifest).expanduser() if args.manifest else None)


def command_run(args: argparse.Namespace) -> Dict[str, Any]:
    validate_identifier(args.revision, kind="revision")
    directory, config, state = _load_project(args.projects_root, args.project_id)
    next_stage = state.next_stage()
    if next_stage == "REFERENCE_LOCKED":
        source = config["reference_input"]
        locked = _copy_reference(source, directory / "reference")
        config["reference_path"] = str(locked)
        atomic_write_json(directory / "project.json", config, root=directory)
        lock = analyze_reference(locked, directory / "reference")
        state.complete(
            "REFERENCE_LOCKED",
            lock["recipe_hash"],
            [
                "project.json",
                "reference/reference-lock.json",
                "reference/blueprint-draft-board.jpg",
                "reference/reference-all-frames-board.jpg",
            ],
        )
        next_stage = state.next_stage()
    if next_stage == "BLUEPRINT_LOCKED":
        lock = load_json(directory / "reference/reference-lock.json")
        return {
            "status": "BLOCKED",
            "next_stage": next_stage,
            "reason": "Automatic cut detection is only a draft. The reviewing agent must inspect the reference at normal speed and the blueprint board, correct every picture/effect boundary, and add independent observations.",
            "artifacts": {
                "reference": config.get("reference_path"),
                "board": str(directory / "reference/blueprint-draft-board.jpg"),
                "all_frames_board": str(directory / "reference/reference-all-frames-board.jpg"),
                "lock": str(directory / "reference/reference-lock.json"),
            },
            "next_action": f"reelctl blueprint lock {args.project_id} --boundaries <all-picture-state-boundaries> --hard-cuts <hard-cut-subset> --observations <observations.json>",
            "draft_boundaries_after": lock["draft_boundaries_after"],
        }
    if next_stage == "FOOTAGE_INDEXED":
        footage_root = Path(config["footage_root"]).expanduser().resolve()
        if _is_within(directory, footage_root):
            raise ReelctlError("project output directory is inside the footage root; use a source-only root")
        manifest = index_footage(
            footage_root,
            directory / "footage",
            default_profile=args.default_profile,
            project_root=directory,
        )
        state.complete("FOOTAGE_INDEXED", manifest["recipe_hash"], _footage_stage_outputs(directory, manifest))
        next_stage = state.next_stage()
    if next_stage == "FEASIBILITY_REPORTED":
        feasibility_path = directory / "edit/feasibility.json"
        if not feasibility_path.exists():
            template_args = argparse.Namespace(projects_root=args.projects_root, project_id=args.project_id, force=False)
            command_feasibility_template(template_args)
        return {
            "status": "BLOCKED",
            "next_stage": next_stage,
            "reason": "The reviewing agent must map every locked picture state against the complete footage inventory as exact, role-equivalent, or missing before selecting clips.",
            "artifacts": {
                "blueprint": str(directory / "reference/blueprint.json"),
                "footage_index": str(directory / "footage/footage-index.json"),
                "feasibility_draft": str(feasibility_path),
            },
            "next_action": f"edit feasibility.json with independent evidence; reelctl feasibility lock {args.project_id}",
        }
    if next_stage == "SELECTION_LOCKED":
        selection_path = directory / "edit/selection.json"
        if not selection_path.exists():
            template_args = argparse.Namespace(projects_root=args.projects_root, project_id=args.project_id, force=False)
            command_selection_template(template_args)
        return {
            "status": "BLOCKED",
            "next_stage": next_stage,
            "reason": "The reviewing agent must inspect indexed source boards, independently observe each candidate, preserve every locked picture block, and choose an exact normal-speed source window.",
            "artifacts": {
                "blueprint": str(directory / "reference/blueprint.json"),
                "footage_index": str(directory / "footage/footage-index.json"),
                "feasibility": str(directory / "edit/feasibility.locked.json"),
                "selection_draft": str(selection_path),
            },
            "next_action": f"reelctl color setup {args.project_id} --profile sony_slog3_sgamut3cine; run reelctl color identify for every selected source; keep creative corrections at identity unless a per-slot board proves a bounded change is needed; reelctl selection validate {args.project_id}",
        }
    if next_stage == "ASSETS_LOCKED":
        assets_path = directory / "assets/assets.json"
        if not assets_path.exists():
            template_args = argparse.Namespace(projects_root=args.projects_root, project_id=args.project_id, force=False)
            command_assets_template(template_args)
        return {
            "status": "BLOCKED",
            "next_stage": next_stage,
            "reason": "The reviewing agent must inventory every visible caption/effect state. Guessed fonts are forbidden: use hash-locked exact font bytes or a traced reference glyph/effect plate.",
            "artifacts": {"assets_draft": str(assets_path), "font_inventory": str(directory / "assets/font-inventory.json")},
            "next_action": f"reelctl typography inventory {args.project_id}; build exact assets; reelctl assets lock {args.project_id}",
        }
    if next_stage == "RENDERED":
        result = render_project(directory, revision=args.revision)
        state.complete(
            "RENDERED",
            result["recipe_hash"],
            [
                str(Path(result["master_path"]).relative_to(directory)),
                str(Path(result["review_path"]).relative_to(directory)),
                str(Path(result["render_receipt_path"]).relative_to(directory)),
            ],
        )
        next_stage = state.next_stage()
    if next_stage in {"TECHNICAL_QC", "STRUCTURE_QC", "VISUAL_QC", "LOCAL_REVIEW_READY"}:
        report = run_qc(directory, revision=args.revision)
        candidate_hash = report["candidate_sha256"]
        state.complete(
            "TECHNICAL_QC", candidate_hash, [str(Path(report["report_path"]).relative_to(directory))], status=report["technical"]["status"]
        )
        if report["technical"]["status"] == "PASS":
            state.complete(
                "STRUCTURE_QC",
                candidate_hash,
                [str(Path(report["report_path"]).relative_to(directory))],
                status=report["structure"]["status"],
            )
        if report["technical"]["status"] == report["structure"]["status"] == "PASS":
            state.complete(
                "VISUAL_QC",
                candidate_hash,
                [str(Path(report["report_path"]).relative_to(directory))],
                status=report["visual"]["status"],
                reason=report["visual"].get("reason"),
            )
        if report["authorities"]["local_review_ready"]:
            state.complete("LOCAL_REVIEW_READY", candidate_hash, [report["candidate"]])
            return {
                "status": "LOCAL_REVIEW_READY",
                "candidate": report["candidate"],
                "candidate_sha256": candidate_hash,
                "qc_report": report["report_path"],
                "next_stage": state.next_stage(),
            }
        return {
            "status": "BLOCKED" if report["visual"]["status"] == "BLOCKED" else "FAIL",
            "candidate": report["candidate"],
            "candidate_sha256": candidate_hash,
            "qc_report": report["report_path"],
            "visual": report["visual"],
            "next_action": f"Inspect the candidate at normal speed and {report['visual'].get('comparison_board')}; then record the hash-bound agent review with reelctl review record-agent {args.project_id} --revision {args.revision} ...",
        }
    return {
        "status": "BLOCKED",
        "next_stage": next_stage,
        "reason": "Human or delivery approval is required; reelctl will not self-publish.",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reelctl", description="Deterministic reference-reel reconstruction runtime")
    parser.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_STUDIO_PORT)
    serve.set_defaults(func=command_serve)

    doctor = sub.add_parser("doctor")
    doctor.set_defaults(func=command_doctor)

    # No `project_id` argument by design: main() wraps any command that has one in a
    # project flock, and the orchestrator must never hold a lock across its whole run.
    studio = sub.add_parser("studio", help="reel studio orchestrator")
    studio_sub = studio.add_subparsers(dest="studio_command", required=True)
    studio_daemon = studio_sub.add_parser("daemon", help="run the orchestrator tick loop")
    studio_daemon.add_argument("--once", action="store_true", help="run a single tick and print its report")
    studio_daemon.add_argument("--interval", type=float, default=None, help="seconds between ticks (default: REEL_STUDIO_TICK_SECONDS)")
    studio_daemon.add_argument(
        "--no-judgment",
        action="store_true",
        help="run deterministic stages only; judgment stages withhold instead of spawning a headless worker",
    )
    studio_daemon.set_defaults(func=command_studio_daemon)

    fixtures = sub.add_parser("fixtures")
    fixtures_sub = fixtures.add_subparsers(dest="fixtures_command", required=True)
    fixtures_audit = fixtures_sub.add_parser("audit")
    fixtures_audit.add_argument("--manifest")
    fixtures_audit.set_defaults(func=command_fixtures_audit)

    run = sub.add_parser("run")
    run.add_argument("project_id")
    run.add_argument("--through", choices=("local-review",), default="local-review")
    run.add_argument("--revision", default="v001")
    run.add_argument("--default-profile", default="log_unknown")
    run.set_defaults(func=command_run)

    new = sub.add_parser("new")
    new.add_argument("project_id")
    new.add_argument(
        "--reference",
        required=True,
        help="local path to the reference video (recommended). A URL is accepted, but downloads must respect the "
        "platform's terms and the creator's rights.",
    )
    new.add_argument("--footage", required=True)
    new.add_argument("--mode", choices=("reference-locked", "original-montage"), default="reference-locked")
    new.add_argument("--defer-analysis", action="store_true")
    new.add_argument(
        "--audio-track",
        help="licensed audio track you hold rights to, cut to the reel's length (required before render under the "
        "default audio policy licensed_track_required)",
    )
    new.add_argument(
        "--reference-audio-rights-held",
        action="store_true",
        help="opt-in: mux the reference's own audio instead of a licensed track. Use ONLY if you hold the rights to it.",
    )
    new.set_defaults(func=command_new)

    status = sub.add_parser("status")
    status.add_argument("project_id")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=command_status)

    reference = sub.add_parser("reference")
    reference_sub = reference.add_subparsers(dest="reference_command", required=True)
    analyze = reference_sub.add_parser("analyze")
    analyze.add_argument("project_id")
    analyze.add_argument("--source")
    analyze.set_defaults(func=command_reference_analyze)

    blueprint = sub.add_parser("blueprint")
    blueprint_sub = blueprint.add_subparsers(dest="blueprint_command", required=True)
    lock = blueprint_sub.add_parser("lock")
    lock.add_argument("project_id")
    group = lock.add_mutually_exclusive_group(required=True)
    group.add_argument("--boundaries", help="comma-separated zero-based frames after which picture changes")
    group.add_argument("--accept-draft", action="store_true")
    lock.add_argument(
        "--hard-cuts", help="comma-separated subset of picture boundaries that are true hard cuts; defaults to every boundary"
    )
    lock.add_argument(
        "--observations",
        required=True,
        help="JSON object mapping every p001/p002/... to structured role, description, transition, and evidence frames",
    )
    lock.add_argument("--all-frames-reviewed", action="store_true", required=True)
    lock.set_defaults(func=command_blueprint_lock)

    footage = sub.add_parser("footage")
    footage_sub = footage.add_subparsers(dest="footage_command", required=True)
    index = footage_sub.add_parser("index")
    index.add_argument("project_id")
    index.add_argument("--footage")
    index.add_argument("--default-profile", default="log_unknown")
    index.set_defaults(func=command_footage_index)
    shortlist = footage_sub.add_parser("shortlist")
    shortlist.add_argument("--blueprint", required=True, help="reference blueprint containing shot_roles")
    shortlist.add_argument("--catalog", required=True, help="complete editorial catalog JSONL")
    shortlist.add_argument("--features", help="optional thumbnail feature JSON")
    shortlist.add_argument("--output", required=True, help="machine-only shortlist JSON artifact")
    shortlist.add_argument("--top-n", type=int, default=20)
    shortlist.set_defaults(func=command_footage_shortlist)

    variants = sub.add_parser("variants")
    variants_sub = variants.add_subparsers(dest="variants_command", required=True)
    variants_validate = variants_sub.add_parser("validate")
    variants_validate.add_argument("--manifest", required=True, help="variant family manifest JSON")
    variants_validate.add_argument("--output", required=True, help="hash-bound validation receipt JSON")
    variants_validate.set_defaults(func=command_variants_validate)

    feasibility = sub.add_parser("feasibility")
    feasibility_sub = feasibility.add_subparsers(dest="feasibility_command", required=True)
    feasibility_template_parser = feasibility_sub.add_parser("template")
    feasibility_template_parser.add_argument("project_id")
    feasibility_template_parser.add_argument("--force", action="store_true")
    feasibility_template_parser.set_defaults(func=command_feasibility_template)
    feasibility_lock_parser = feasibility_sub.add_parser("lock")
    feasibility_lock_parser.add_argument("project_id")
    feasibility_lock_parser.add_argument("--manifest")
    feasibility_lock_parser.set_defaults(func=command_feasibility_lock)

    selection = sub.add_parser("selection")
    selection_sub = selection.add_subparsers(dest="selection_command", required=True)
    template = selection_sub.add_parser("template")
    template.add_argument("project_id")
    template.add_argument("--force", action="store_true")
    template.set_defaults(func=command_selection_template)
    validate = selection_sub.add_parser("validate")
    validate.add_argument("project_id")
    validate.add_argument("--selection")
    validate.set_defaults(func=command_selection_validate)

    assets = sub.add_parser("assets")
    assets_sub = assets.add_subparsers(dest="assets_command", required=True)
    assets_template = assets_sub.add_parser("template")
    assets_template.add_argument("project_id")
    assets_template.add_argument("--force", action="store_true")
    assets_template.set_defaults(func=command_assets_template)
    assets_lock = assets_sub.add_parser("lock")
    assets_lock.add_argument("project_id")
    assets_lock.add_argument("--manifest")
    assets_lock.set_defaults(func=command_assets_lock)

    render = sub.add_parser("render")
    render.add_argument("project_id")
    render.add_argument("--revision", required=True)
    render.set_defaults(func=command_render)

    review = sub.add_parser("review")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    record_agent = review_sub.add_parser("record-agent")
    record_agent.add_argument("project_id")
    record_agent.add_argument("--revision", required=True)
    record_agent.add_argument("--status", choices=("PASS", "FAIL"), required=True)
    record_agent.add_argument("--normal-speed-full-watch", action="store_true")
    record_agent.add_argument("--reference-side-by-side-checked", action="store_true")
    record_agent.add_argument("--typography-checked", action="store_true")
    record_agent.add_argument("--color-checked", action="store_true")
    record_agent.add_argument("--cut-and-beat-checked", action="store_true")
    record_agent.add_argument("--notes", default="")
    record_agent.set_defaults(func=command_review_record_agent)

    qc = sub.add_parser("qc")
    qc.add_argument("project_id")
    qc.add_argument("--revision", required=True)
    qc.set_defaults(func=command_qc)

    typography = sub.add_parser("typography")
    typography_sub = typography.add_subparsers(dest="typography_command", required=True)
    inventory = typography_sub.add_parser("inventory")
    inventory.add_argument("project_id")
    inventory.add_argument("--extra-dir", action="append", default=[])
    inventory.set_defaults(func=command_typography_inventory)
    match = typography_sub.add_parser("match")
    match.add_argument("project_id")
    match.add_argument("--text", required=True)
    match.add_argument("--mask", required=True)
    match.add_argument("--inventory")
    match.add_argument("--limit", type=int, default=20)
    match.set_defaults(func=command_typography_match)
    trace = typography_sub.add_parser("trace")
    trace.add_argument("--mask", required=True)
    trace.add_argument("--output", required=True)
    trace.add_argument("--rgba", type=int, nargs=4, default=(255, 255, 255, 255))
    trace.set_defaults(func=command_typography_trace)
    extract = typography_sub.add_parser("extract-mask")
    extract.add_argument("--image", required=True)
    extract.add_argument("--output", required=True)
    extract.add_argument("--roi", type=int, nargs=4)
    extract.add_argument("--ink", choices=("bright", "blue", "dark"), default="bright")
    extract.set_defaults(func=command_typography_extract)

    color = sub.add_parser("color")
    color_sub = color.add_subparsers(dest="color_command", required=True)
    identify_color = color_sub.add_parser("identify")
    identify_color.add_argument("--source", required=True)
    identify_color.add_argument("--output")
    identify_color.set_defaults(func=command_color_identify)
    setup_color = color_sub.add_parser("setup")
    setup_color.add_argument("project_id")
    setup_color.add_argument("--profile", choices=("rec709", "sony_slog3_sgamut3cine"), default="sony_slog3_sgamut3cine")
    setup_color.set_defaults(func=command_color_setup)
    propose_color = color_sub.add_parser("propose")
    propose_color.add_argument("project_id")
    propose_color.add_argument("--selection")
    propose_color.set_defaults(func=command_color_propose)
    validate_color = color_sub.add_parser("validate")
    validate_color.add_argument("--selection", required=True)
    validate_color.set_defaults(func=command_color_validate)
    captions = sub.add_parser("captions", help="unified caption/typography engine")
    captions_sub = captions.add_subparsers(dest="captions_command", required=True)

    captions_program = captions_sub.add_parser("program")
    captions_program.add_argument("--authority", type=Path, required=True)
    captions_program.add_argument("--output", type=Path, required=True)
    captions_program.set_defaults(func=command_captions_program)

    captions_validate = captions_sub.add_parser("validate")
    captions_validate.add_argument("--contract", type=Path, required=True)
    captions_validate.set_defaults(func=command_captions_validate)

    captions_ink = captions_sub.add_parser(
        "ink", help="resolve caption fills against the picture they will sit on"
    )
    captions_ink.add_argument("--contract", type=Path, required=True)
    captions_ink.add_argument("--lockups", type=Path, required=True)
    captions_ink.add_argument("--plates", type=Path, required=True)
    captions_ink.add_argument("--reference-frames", type=Path, required=True)
    captions_ink.add_argument("--candidate-frames", type=Path, required=True)
    captions_ink.add_argument("--reference-pattern", default="r{index:03d}.png")
    captions_ink.add_argument("--candidate-pattern", default="r{index:03d}.png")
    captions_ink.add_argument("--reference-index-origin", type=int, default=0)
    captions_ink.add_argument("--candidate-index-origin", type=int, default=0)
    captions_ink.add_argument(
        "--strict",
        action="store_true",
        help="also move a fill that clears the contrast target but sits inside the 25-level floor",
    )
    captions_ink.add_argument(
        "--base-from-contract",
        action="store_true",
        help=(
            "start each lockup from the fill the approved contract carries rather than from the "
            "spec's reference measurement; every state in a lockup must agree. Without it a "
            "re-ink silently reverts any fill an operator has already adjudicated."
        ),
    )
    captions_ink.add_argument("--output", type=Path, required=True)
    captions_ink.add_argument("--out-contract", type=Path, default=None)
    captions_ink.add_argument("--out-contract-render", type=Path, default=None)
    captions_ink.set_defaults(func=command_captions_ink)

    captions_render = captions_sub.add_parser("render")
    captions_render.add_argument("--contract", type=Path, required=True)
    captions_render.add_argument("--plates", type=Path, required=True)
    captions_render.add_argument("--output", type=Path, required=True)
    captions_render.add_argument("--allow-unsealed-local-review", action="store_true")
    captions_render.set_defaults(func=command_captions_render)

    captions_qc = captions_sub.add_parser("qc")
    captions_qc.add_argument("--contract", type=Path, required=True)
    captions_qc.add_argument("--rendered", type=Path, required=True)
    captions_qc.add_argument(
        "--plates",
        type=Path,
        default=None,
        help=(
            "reference-traced alpha plates; enables the per-state Dice parity gate. Without it "
            "the run reports visual_parity BLOCKED and names the gate it skipped."
        ),
    )
    captions_qc.add_argument(
        "--authority",
        type=Path,
        default=None,
        help="sealed authority document; enables the verbatim word-identity gate",
    )
    captions_qc.add_argument(
        "--ratified-contract",
        type=Path,
        default=None,
        help=(
            "the approved contract whose fills the operator ratified; enables the ink-identity "
            "gate (per-state ink distance <= 6.0). Without it nothing checks that a state's "
            "fill is still the colour that was approved."
        ),
    )
    captions_qc.add_argument("--output", type=Path, default=None)
    captions_qc.set_defaults(func=command_captions_qc)

    return parser


def main(argv: Optional[Sequence[str]] = None, *, exit_on_error: bool = True) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        # Preserve status/diagnostics/review serving; all work commands need Pilot.
        if args.command not in {"status", "doctor", "serve", "fixtures"}:
            require_factory_open(f"reelctl {args.command}")
        if hasattr(args, "project_id"):
            with project_lock(args.projects_root, args.project_id):
                result = args.func(args)
        else:
            result = args.func(args)
        _print(result)
        return 0 if result.get("status") != "FAIL" else 2
    except SystemExit as exc:
        if exit_on_error:
            raise
        return int(exc.code or 0)
    except Exception as exc:
        if exit_on_error:
            print(json.dumps({"status": "FAIL", "error": str(exc), "error_type": type(exc).__name__}), file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
