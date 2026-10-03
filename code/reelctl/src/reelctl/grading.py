from __future__ import annotations

import shutil
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from .color import estimate_bounded_grade, ffmpeg_color_filter, image_metrics
from .hashing import atomic_external_output, atomic_write_bytes, atomic_write_json, load_json, sha256_file
from .media import run_checked
from .paths import canonical_root, confined_path, secure_mkdirs

DATA_ROOT = Path(__file__).resolve().parent / "data"
PROFILE_REGISTRY = DATA_ROOT / "color-profiles.json"


class GradeError(RuntimeError):
    pass


def install_color_profile(project_dir: Path, profile: str) -> Dict[str, Any]:
    project_dir = canonical_root(project_dir, create=True)
    registry = load_json(PROFILE_REGISTRY)
    record = registry.get("profiles", {}).get(profile)
    if record is None:
        raise GradeError(f"unknown packaged color profile: {profile}")
    if record.get("lut") is None:
        return {"status": "PASS", "profile": profile, "technical_transform": record["technical_transform"], "lut": None}
    source = DATA_ROOT / record["lut"]
    if not source.is_file() or sha256_file(source) != record["lut_sha256"]:
        raise GradeError("packaged LUT is missing or its provenance hash changed")
    destination = project_dir / "assets/luts" / source.name
    secure_mkdirs(project_dir, "assets/luts")
    destination = confined_path(project_dir, destination)
    if destination.exists() and sha256_file(destination) != record["lut_sha256"]:
        raise GradeError(f"refusing different LUT at {destination}")
    if not destination.exists():
        with atomic_external_output(destination, root=project_dir) as temporary:
            shutil.copyfile(source, temporary)
    receipt = {
        "schema_version": 1,
        "status": "PASS",
        "profile": profile,
        "technical_transform": record["technical_transform"],
        "lut": str(destination),
        "lut_sha256": sha256_file(destination),
        "verification": record["verification"],
        "profile_lock": "PENDING_AGENT_SPOT_CHECK",
    }
    atomic_write_json(project_dir / "assets/color-profile.json", receipt, root=project_dir)
    return receipt


def _resolve(project_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return path.resolve()


def _extract_reference(path: Path, frame: int, output: Path, *, root: Path) -> None:
    with atomic_external_output(output, root=root) as temporary:
        run_checked(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-i",
                str(path),
                "-vf",
                f"select=eq(n\\,{frame})",
                "-frames:v",
                "1",
                str(temporary),
            ]
        )


def _extract_technical_sample(project_dir: Path, slot: Dict[str, Any], output: Path) -> None:
    source = _resolve(project_dir, str(slot["source_path"]))
    start = int(slot.get("source_start_frame", 0))
    technical = dict(slot)
    technical["creative"] = {}
    if technical.get("technical_lut"):
        technical["technical_lut"] = str(_resolve(project_dir, str(technical["technical_lut"])))
    filters = f"select=eq(n\\,{start})," + ffmpeg_color_filter(technical)
    with atomic_external_output(output, root=project_dir) as temporary:
        run_checked(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-i",
                str(source),
                "-vf",
                filters,
                "-frames:v",
                "1",
                str(temporary),
            ]
        )


def _write_grade_board(rows: List[Dict[str, Any]], output: Path) -> None:
    from PIL import Image, ImageDraw

    tile_width = 360
    label_height = 48
    row_images = []
    for row in rows:
        images = []
        for label, key in (("TECHNICAL NORMALIZATION", "source_sample"), ("REFERENCE TARGET", "reference_sample")):
            image = Image.open(row[key]).convert("RGB")
            height = round(image.height * tile_width / image.width)
            image = image.resize((tile_width, height), Image.Resampling.LANCZOS)
            tile = Image.new("RGB", (tile_width, height + label_height), "white")
            tile.paste(image, (0, label_height))
            ImageDraw.Draw(tile).text((7, 6), f"{row['block_id']} {label}", fill="black")
            images.append(tile)
        row_images.append((images[0], images[1]))
    if not row_images:
        return
    row_height = row_images[0][0].height
    board = Image.new("RGB", (tile_width * 2, row_height * len(row_images)), (235, 235, 235))
    for index, (left, right) in enumerate(row_images):
        board.paste(left, (0, index * row_height))
        board.paste(right, (tile_width, index * row_height))
    buffer = BytesIO()
    board.save(buffer, format="JPEG", quality=93)
    atomic_write_bytes(output, buffer.getvalue(), root=output.parent, mode=0o600)


def propose_source_aware_grades(project_dir: Path, *, selection_path: Optional[Path] = None) -> Dict[str, Any]:
    project_dir = canonical_root(project_dir)
    project = load_json(project_dir / "project.json", root=project_dir)
    blueprint = load_json(project_dir / "reference/blueprint.json", root=project_dir)
    selection_path = Path(selection_path) if selection_path else project_dir / "edit/selection.json"
    selection = load_json(selection_path, root=project_dir if selection_path.is_relative_to(project_dir) else None)
    slots = selection.get("slots", selection.get("shots", []))
    blocks = {str(block["id"]): block for block in blueprint["picture_blocks"]}
    reference = confined_path(project_dir, Path(str(project["reference_path"])), require="file", allow_missing=False)
    samples_dir = secure_mkdirs(project_dir, "review/color-grade-proposal/samples")
    proposals: List[Dict[str, Any]] = []
    board_rows: List[Dict[str, Any]] = []
    for index, slot in enumerate(slots, start=1):
        block_id = str(slot.get("block_id"))
        if block_id not in blocks:
            raise GradeError(f"selection slot {index} references unknown block {block_id}")
        block = blocks[block_id]
        ref_frame = (int(block["start_frame"]) + int(block["end_frame_exclusive"]) - 1) // 2
        source_sample = samples_dir / f"{block_id}-technical.png"
        reference_sample = samples_dir / f"{block_id}-reference.png"
        _extract_technical_sample(project_dir, slot, source_sample)
        _extract_reference(reference, ref_frame, reference_sample, root=project_dir)
        source_metrics = image_metrics(source_sample)
        target_metrics = image_metrics(reference_sample)
        creative = estimate_bounded_grade(source_metrics, target_metrics)
        proof = {
            "status": "PENDING_AGENT_REVIEW",
            "block_id": block_id,
            "reference_frame": ref_frame,
            "source_frame": int(slot.get("source_start_frame", 0)),
            "source_sample": str(source_sample),
            "source_sample_sha256": sha256_file(source_sample),
            "reference_sample": str(reference_sample),
            "reference_sample_sha256": sha256_file(reference_sample),
            "source_metrics": source_metrics,
            "target_metrics": target_metrics,
            "bounded_proposal": creative,
        }
        proposals.append({"slot_index": index, "block_id": block_id, "creative": creative, "grade_proof": proof})
        board_rows.append({"block_id": block_id, "source_sample": str(source_sample), "reference_sample": str(reference_sample)})
    board = project_dir / "review/color-grade-proposal/source-reference-board.jpg"
    _write_grade_board(board_rows, board)
    report = {
        "schema_version": 1,
        "status": "PENDING_AGENT_REVIEW",
        "selection": str(selection_path.resolve()),
        "board": str(board),
        "proposals": proposals,
        "instructions": "Inspect every row. Apply a proposal per slot only after checking skin, highlight retention, neutral balance, and scene continuity. Set each grade_proof.status to AGENT_REVIEWED; never copy one recipe across unrelated lighting families.",
    }
    atomic_write_json(project_dir / "review/color-grade-proposal/grade-proposals.json", report, root=project_dir)
    return report
