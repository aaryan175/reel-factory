from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Sequence

from . import __version__
from .contracts import validate_contract
from .hashing import atomic_write_bytes, atomic_write_json, load_json, sha256_file
from .identifiers import validate_identifier
from .media import (
    audio_packet_ledger,
    audio_payload_sha256,
    decode_pcm_sha256,
    frame_pts,
    full_decode,
    legal_luma_range,
    probe_media,
)
from .paths import canonical_root, confined_path, secure_mkdirs
from .reference import _decode_analysis_frames, detect_hard_cuts, verify_blueprint_identity, verify_reference_identity
from .selection import validate_selection
from .signing import SignatureError, verify_payload
from .typography import validate_typography_layer

VALID = {"PASS", "FAIL", "BLOCKED", "PENDING", "REJECTED", "APPROVED"}


def combine_authorities(*, technical: str, structure: str, visual: str, human: str) -> Dict[str, Any]:
    for label, value in {"technical": technical, "structure": structure, "visual": visual, "human": human}.items():
        if value not in VALID:
            raise ValueError(f"invalid {label} authority: {value}")
    machine_pass = technical == structure == visual == "PASS"
    machine_reject = "FAIL" in {technical, structure, visual}
    machine_blocked = any(value in {"BLOCKED", "PENDING"} for value in (technical, structure, visual))
    if human == "REJECTED" or machine_reject:
        overall = "REJECT"
    elif machine_blocked:
        overall = "BLOCKED"
    elif human == "APPROVED":
        overall = "APPROVED"
    else:
        overall = "LOCAL_REVIEW_READY"
    return {
        "technical_integrity": technical,
        "reference_structure_parity": structure,
        "visual_parity": visual,
        "human_creative_approval": human,
        "overall": overall,
        "local_review_ready": machine_pass,
        "publishable": machine_pass and human == "APPROVED",
    }


def _make_comparison_board(reference: Path, candidate: Path, blocks: Sequence[Dict[str, Any]], output: Path) -> None:
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    ref_frames = _decode_analysis_frames(reference)
    cand_frames = _decode_analysis_frames(candidate)
    pairs = []
    for block in blocks:
        frame_index = (int(block["start_frame"]) + int(block["end_frame_exclusive"]) - 1) // 2
        ref = ref_frames[frame_index]
        cand = cand_frames[frame_index]
        target_w = 360

        def resized(frame: Any, target_width: int = target_w) -> Any:
            h, w = frame.shape[:2]
            return cv2.resize(frame, (target_width, round(h * target_width / w)), interpolation=cv2.INTER_AREA)

        pairs.append((block["id"], resized(ref), resized(cand), frame_index))
    if not pairs:
        return
    tile_h = pairs[0][1].shape[0]
    label_h = 46
    rows = []
    for block_id, ref, cand, frame_index in pairs:
        joined = np.concatenate([ref, cand], axis=1)
        rgb = cv2.cvtColor(joined, cv2.COLOR_BGR2RGB)
        tile = Image.new("RGB", (joined.shape[1], tile_h + label_h), "white")
        tile.paste(Image.fromarray(rgb), (0, label_h))
        draw = ImageDraw.Draw(tile)
        draw.text((8, 5), f"{block_id} frame {frame_index}: REFERENCE", fill="black")
        draw.text((target_w + 8, 5), "CANDIDATE", fill="black")
        rows.append(tile)
    board = Image.new("RGB", (rows[0].width, sum(row.height for row in rows)), "white")
    y = 0
    for row in rows:
        board.paste(row, (0, y))
        y += row.height
    secure_mkdirs(output.parent)
    buffer = BytesIO()
    board.save(buffer, format="JPEG", quality=92)
    atomic_write_bytes(output, buffer.getvalue(), root=output.parent, mode=0o600)


REFERENCE_RELATIVE_THRESHOLDS = {
    "luma_q50_max_abs_delta": 0.10,
    "saturation_ratio_min": 0.75,
    "saturation_ratio_max": 1.25,
    "low_saturation_absolute_delta": 0.08,
    "lab_neutral_axis_max_delta": 12.0,
    "unexpected_black_y_q95": 0.03,
    "unexpected_white_y_q05": 0.97,
}


def _block_end_exclusive(block: Dict[str, Any]) -> int:
    if "end_frame_exclusive" in block:
        return int(block["end_frame_exclusive"])
    if "end_frame_inclusive" in block:
        return int(block["end_frame_inclusive"]) + 1
    raise ValueError(f"reference block {block.get('id')} has no end-frame boundary")


def _visual_frame_metrics(frame: Any) -> Dict[str, float]:
    import cv2
    import numpy as np

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    y = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB).astype(np.float32)
    return {
        "y_q05": float(np.quantile(y, 0.05)),
        "y_q50": float(np.quantile(y, 0.50)),
        "y_q95": float(np.quantile(y, 0.95)),
        "saturation_mean": float(hsv[..., 1].mean() / 255.0),
        "lab_a_mean": float(lab[..., 1].mean() - 128.0),
        "lab_b_mean": float(lab[..., 2].mean() - 128.0),
    }


def _aggregate_visual_metrics(metrics: Sequence[Dict[str, float]]) -> Dict[str, float]:
    import numpy as np

    if not metrics:
        raise ValueError("cannot aggregate an empty reference-relative frame range")
    return {key: round(float(np.median([frame[key] for frame in metrics])), 8) for key in metrics[0]}


def _reference_relative_frame_qc(
    reference_frames: Sequence[Any],
    candidate_frames: Sequence[Any],
    blocks: Sequence[Dict[str, Any]],
    *,
    mode: str,
) -> Dict[str, Any]:
    """Compare the reference and candidate on the same locked frame clock.

    Literal reference recreations use this as a ship-blocking gate. Original
    montages intentionally use different pictures, so the same numbers remain
    diagnostic and cannot be misrepresented as pixel-parity proof.
    """

    import math

    if mode not in {"reference-locked", "original-montage"}:
        raise ValueError(f"unsupported reel mode for reference-relative QC: {mode}")
    if len(reference_frames) != len(candidate_frames):
        return {
            "status": "FAIL" if mode == "reference-locked" else "NOT_APPLICABLE",
            "authority": "SHIP_BLOCKING" if mode == "reference-locked" else "DIAGNOSTIC_ONLY",
            "mode": mode,
            "reason": "reference and candidate frame counts differ",
            "reference_frames": len(reference_frames),
            "candidate_frames": len(candidate_frames),
            "thresholds": REFERENCE_RELATIVE_THRESHOLDS,
            "blocks": [],
            "failing_blocks": [str(block.get("id")) for block in blocks] if mode == "reference-locked" else [],
        }

    reference_metrics = [_visual_frame_metrics(frame) for frame in reference_frames]
    candidate_metrics = [_visual_frame_metrics(frame) for frame in candidate_frames]
    rows = []
    strict_failures = []
    frame_count = len(reference_frames)
    thresholds = REFERENCE_RELATIVE_THRESHOLDS
    for block in blocks:
        block_id = str(block.get("id"))
        start = int(block["start_frame"])
        end = _block_end_exclusive(block)
        if start < 0 or end <= start or end > frame_count:
            raise ValueError(f"reference block {block_id} is outside the locked frame clock")
        reference_block = reference_metrics[start:end]
        candidate_block = candidate_metrics[start:end]
        reference_aggregate = _aggregate_visual_metrics(reference_block)
        candidate_aggregate = _aggregate_visual_metrics(candidate_block)
        luma_delta = abs(candidate_aggregate["y_q50"] - reference_aggregate["y_q50"])
        reference_saturation = reference_aggregate["saturation_mean"]
        candidate_saturation = candidate_aggregate["saturation_mean"]
        saturation_ratio = candidate_saturation / max(reference_saturation, 1e-9)
        if reference_saturation < 0.05:
            saturation_ok = abs(candidate_saturation - reference_saturation) <= thresholds["low_saturation_absolute_delta"]
        else:
            saturation_ok = thresholds["saturation_ratio_min"] <= saturation_ratio <= thresholds["saturation_ratio_max"]
        neutral_axis_delta = math.hypot(
            candidate_aggregate["lab_a_mean"] - reference_aggregate["lab_a_mean"],
            candidate_aggregate["lab_b_mean"] - reference_aggregate["lab_b_mean"],
        )
        unexpected_black = [
            start + index
            for index, (reference_frame, candidate_frame) in enumerate(zip(reference_block, candidate_block))
            if candidate_frame["y_q95"] < thresholds["unexpected_black_y_q95"]
            and reference_frame["y_q95"] >= thresholds["unexpected_black_y_q95"]
        ]
        unexpected_white = [
            start + index
            for index, (reference_frame, candidate_frame) in enumerate(zip(reference_block, candidate_block))
            if candidate_frame["y_q05"] > thresholds["unexpected_white_y_q05"]
            and reference_frame["y_q05"] <= thresholds["unexpected_white_y_q05"]
        ]
        checks = {
            "luma_parity": luma_delta <= thresholds["luma_q50_max_abs_delta"],
            "saturation_parity": saturation_ok,
            "neutral_axis_cast": neutral_axis_delta <= thresholds["lab_neutral_axis_max_delta"],
            "no_unexpected_black_frames": not unexpected_black,
            "no_unexpected_white_frames": not unexpected_white,
        }
        failed_checks = [name for name, passed in checks.items() if not passed]
        strict_status = "PASS" if not failed_checks else "FAIL"
        if mode == "reference-locked" and failed_checks:
            strict_failures.append(block_id)
        rows.append(
            {
                "id": block_id,
                "start_frame": start,
                "end_frame_exclusive": end,
                "status": strict_status if mode == "reference-locked" else "DIAGNOSTIC",
                "checks": checks,
                "failed_checks": failed_checks,
                "reference": reference_aggregate,
                "candidate": candidate_aggregate,
                "delta": {
                    "luma_q50_abs": round(luma_delta, 8),
                    "saturation_ratio": round(saturation_ratio, 8),
                    "lab_neutral_axis": round(neutral_axis_delta, 8),
                },
                "unexpected_black_frames": unexpected_black,
                "unexpected_white_frames": unexpected_white,
            }
        )
    enforced = mode == "reference-locked"
    return {
        "status": ("FAIL" if strict_failures else "PASS") if enforced else "NOT_APPLICABLE",
        "authority": "SHIP_BLOCKING" if enforced else "DIAGNOSTIC_ONLY",
        "mode": mode,
        "reference_frames": len(reference_frames),
        "candidate_frames": len(candidate_frames),
        "thresholds": thresholds,
        "blocks": rows,
        "failing_blocks": strict_failures,
        "failing_block_count": len(strict_failures),
    }


def reference_relative_visual_qc(
    reference: Path,
    candidate: Path,
    blocks: Sequence[Dict[str, Any]],
    *,
    mode: str,
) -> Dict[str, Any]:
    reference_frames = _decode_analysis_frames(reference)
    candidate_frames = _decode_analysis_frames(candidate)
    return _reference_relative_frame_qc(reference_frames, candidate_frames, blocks, mode=mode)


def _resolve_visual_status(reference_relative: Dict[str, Any], checks: Dict[str, bool]) -> str:
    machine_status = str(reference_relative.get("status"))
    if machine_status == "FAIL":
        return "FAIL"
    if machine_status == "BLOCKED":
        return "BLOCKED"
    if machine_status not in {"PASS", "NOT_APPLICABLE"}:
        return "FAIL"
    return "PASS" if all(checks.values()) else "FAIL"


CAPTION_CONTRAST_MIN_RATIO = 1.8


def _caption_contrast_measure(frame: Any, alpha_mask: Any) -> Dict[str, float]:
    import cv2
    import numpy as np

    mask = np.asarray(alpha_mask) >= 128
    visible_pixels = int(mask.sum())
    if visible_pixels < 8:
        return {"contrast_ratio": 0.0, "visible_pixels": visible_pixels, "foreground_luma": 0.0, "surround_luma": 0.0}
    kernel = np.ones((9, 9), np.uint8)
    ring = (cv2.dilate(mask.astype(np.uint8), kernel, iterations=1) > 0) & ~mask
    if not ring.any():
        return {"contrast_ratio": 0.0, "visible_pixels": visible_pixels, "foreground_luma": 0.0, "surround_luma": 0.0}
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    y = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    foreground = float(np.median(y[mask]))
    surround = float(np.median(y[ring]))
    high, low = max(foreground, surround), min(foreground, surround)
    ratio = (high + 0.05) / (low + 0.05)
    return {
        "contrast_ratio": round(ratio, 8),
        "visible_pixels": visible_pixels,
        "foreground_luma": round(foreground, 8),
        "surround_luma": round(surround, 8),
    }


def caption_contrast_qc(candidate: Path, assets: Dict[str, Any], project_dir: Path) -> Dict[str, Any]:
    import cv2
    import numpy as np
    from PIL import Image

    layers = assets.get("typography_layers", [])
    if not layers:
        return {"status": "PASS", "threshold": CAPTION_CONTRAST_MIN_RATIO, "layers": [], "failing_layers": []}
    frames = _decode_analysis_frames(candidate)
    overlay_pattern = assets.get("overlay_sequence")
    overlay_root = None
    if overlay_pattern:
        overlay_path = Path(str(overlay_pattern)).expanduser()
        if not overlay_path.is_absolute():
            overlay_path = project_dir / overlay_path
        overlay_root = overlay_path.parent
    rows = []
    failures = []
    for layer in layers:
        layer_id = str(layer.get("id", ""))
        raw_plate = layer.get("plate_path")
        plate = Path(str(raw_plate)).expanduser() if raw_plate else None
        if plate is not None and not plate.is_absolute():
            plate = project_dir / plate
        if (plate is None or not plate.is_file()) and overlay_root is not None:
            plate = overlay_root / ".layers" / f"{layer_id}.png"
        if plate is None or not plate.is_file():
            rows.append({"id": layer_id, "status": "FAIL", "reason": "rendered typography plate is unavailable"})
            failures.append(layer_id)
            continue
        with Image.open(plate) as image:
            alpha = np.asarray(image.convert("RGBA").getchannel("A"))
        start = int(layer["start_frame"])
        end = int(layer["end_frame_exclusive"])
        measurements = []
        for frame_index in range(start, end):
            frame = frames[frame_index]
            resized_alpha = cv2.resize(alpha, (frame.shape[1], frame.shape[0]), interpolation=cv2.INTER_AREA)
            measurements.append({"frame": frame_index, **_caption_contrast_measure(frame, resized_alpha)})
        minimum = min((item["contrast_ratio"] for item in measurements), default=0.0)
        status = "PASS" if minimum >= CAPTION_CONTRAST_MIN_RATIO else "FAIL"
        if status == "FAIL":
            failures.append(layer_id)
        rows.append(
            {
                "id": layer_id,
                "status": status,
                "start_frame": start,
                "end_frame_exclusive": end,
                "minimum_contrast_ratio": round(minimum, 8),
                "worst_frames": sorted(measurements, key=lambda item: item["contrast_ratio"])[:3],
                "plate_path": str(plate),
                "plate_sha256": sha256_file(plate),
            }
        )
    return {
        "status": "PASS" if not failures else "FAIL",
        "threshold": CAPTION_CONTRAST_MIN_RATIO,
        "layers": rows,
        "failing_layers": failures,
    }


def _technical_qc(reference: Path, candidate: Path, reference_lock: Dict[str, Any]) -> Dict[str, Any]:
    facts = probe_media(candidate)
    video = facts["video"]
    audio = facts["audio"] or {}
    clock = reference_lock["clock"]
    range_report = legal_luma_range(candidate)
    candidate_pts = frame_pts(candidate)
    checks = {
        "full_decode_video_and_audio": full_decode(candidate)["status"] == "PASS",
        "frame_count": int(video.get("frame_count") or 0) == int(clock["frame_count"]),
        "presentation_pts_ledger": candidate_pts == clock["pts"],
        "fps": video.get("r_frame_rate") == clock["fps"],
        "time_base": video.get("time_base") == clock["time_base"],
        "duration_ts": int(video.get("duration_ts") or 0) == int(clock["duration_ts"]),
        "geometry": [int(video.get("width", 0)), int(video.get("height", 0))] == [int(clock["width"]), int(clock["height"])],
        "rotation_normalized": int(video.get("rotation", 0)) == 0,
        "sar": video.get("sample_aspect_ratio") == "1:1",
        "rec709_tags": [video.get("color_space"), video.get("color_transfer"), video.get("color_primaries")] == ["bt709", "bt709", "bt709"],
        "legal_luma_range": range_report["status"] == "PASS",
        "audio_pcm_identity": decode_pcm_sha256(candidate) == reference_lock["audio"]["pcm_s16le_48k_mono_sha256"],
        "audio_payload_identity": audio_payload_sha256(candidate) == reference_lock["audio"]["payload_sha256"],
        "audio_packet_ledger_identity": audio_packet_ledger(candidate) == reference_lock["audio"]["packet_ledger"],
        "audio_start_pts": (not reference_lock["audio"]["present"])
        or int(audio.get("start_pts") or 0) == int(reference_lock["audio"]["start_pts"]),
        "audio_time_base": (not reference_lock["audio"]["present"]) or audio.get("time_base") == reference_lock["audio"]["time_base"],
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "facts": facts,
        "range": range_report,
        "candidate_pts": candidate_pts,
    }


BEAT_TOLERANCE_FRAMES = 2


def beat_alignment_rows(
    cut_frames: Sequence[int],
    onset_frames: Sequence[int],
    tolerance: int = BEAT_TOLERANCE_FRAMES,
) -> list:
    """How far each cut sits from the nearest reference audio onset.

    `cut_frames` are the frames a cut lands ON (a boundary_after + 1). With no onsets at all
    there is nothing to align to and nothing may claim alignment.
    """
    rows = []
    for cut_frame in cut_frames:
        nearest = min(onset_frames, key=lambda value: abs(value - cut_frame)) if onset_frames else None
        distance = abs(int(nearest) - int(cut_frame)) if nearest is not None else None
        rows.append(
            {
                "cut_frame": int(cut_frame),
                "nearest_reference_audio_onset_frame": None if nearest is None else int(nearest),
                "distance_frames": distance,
                "within_tolerance": distance is not None and distance <= tolerance,
            }
        )
    return rows


def beat_alignment_passed(
    cut_frames: Sequence[int],
    *,
    onsets: Sequence[int],
    expected_cuts: Sequence[int],
    tolerance: int = BEAT_TOLERANCE_FRAMES,
) -> bool:
    """Every cut in the delivered file lands on a beat — and a build that expects cuts and
    shows none has measured nothing, which is not a pass."""
    if not cut_frames:
        return not expected_cuts
    return all(row["within_tolerance"] for row in beat_alignment_rows(cut_frames, onsets, tolerance))


def _structure_qc(
    candidate: Path,
    blueprint: Dict[str, Any],
    selection: Dict[str, Any],
    mode: str,
    reference_lock: Dict[str, Any],
    render_receipt: Dict[str, Any],
) -> Dict[str, Any]:
    selection_receipt = validate_selection(blueprint, selection, mode=mode)
    expected_blocks = [str(block["id"]) for block in blueprint["picture_blocks"]]
    expected_boundaries = [int(value) for value in blueprint.get("picture_boundaries_after", [])]
    expected_hard_cuts = [int(value) for value in blueprint.get("hard_cuts_after", [])]
    segments = render_receipt.get("segments", [])
    rendered_blocks = [str(segment.get("block_id")) for segment in segments]
    rendered_boundaries = [int(segment["output_end_frame_exclusive"]) - 1 for segment in segments[:-1]]
    rendered_frames = [int(segment.get("frames", 0)) for segment in segments]
    expected_frames = [int(block["frames"]) for block in blueprint["picture_blocks"]]
    detector_report = detect_hard_cuts(_decode_analysis_frames(candidate))
    detected_hard_cuts = [int(value) for value in detector_report["boundaries_after"]]
    checks = {
        "ordered_block_ids": rendered_blocks == expected_blocks == selection_receipt["block_ids"],
        "block_frame_lengths": rendered_frames == expected_frames,
        "segment_boundaries": rendered_boundaries == expected_boundaries,
        "presentation_clock_identity": frame_pts(candidate) == reference_lock["clock"]["pts"],
        "reference_audio_timeline_identity": audio_packet_ledger(candidate) == reference_lock["audio"]["packet_ledger"],
    }
    onset_frames = [int(item["frame"]) for item in reference_lock.get("audio_onsets", [])]
    # registry TOOL-DEFECT-structure-qc-reads-blueprint-not-candidate. This gate used to walk
    # the BLUEPRINT's cut list and never read the candidate, so it could neither catch a build
    # whose cuts drifted nor be passed by any build of a reference whose own cuts sit off its
    # own onsets. The candidate's cuts were already detected above and discarded into a
    # "supporting signal"; they are the measurement, and this is where they belong.
    candidate_cut_frames = [value + 1 for value in detected_hard_cuts]
    beat_alignment = beat_alignment_rows(candidate_cut_frames, onset_frames)
    checks["hard_cuts_within_two_frames_of_reference_onsets"] = beat_alignment_passed(
        candidate_cut_frames, onsets=onset_frames, expected_cuts=expected_hard_cuts
    )
    blueprint_beat_alignment = beat_alignment_rows(
        [value + 1 for value in expected_hard_cuts], onset_frames
    )
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "picture_blocks_expected": len(expected_blocks),
        "picture_blocks_selected": selection_receipt["slots"],
        "ordered_block_ids": rendered_blocks,
        "picture_boundaries_expected": expected_boundaries,
        "picture_boundaries_rendered": rendered_boundaries,
        "hard_cuts_expected": expected_hard_cuts,
        "visual_cut_detector_supporting_evidence": {
            "boundaries": detected_hard_cuts,
            "threshold": detector_report["threshold"],
            "authority": "CANDIDATE_MEASUREMENT — this detector's output is what the beat gate is computed from",
        },
        "beat_alignment": beat_alignment,
        "beat_alignment_scope": (
            "measured on the CANDIDATE's own detected cuts against the reference's audio onsets. "
            "An earlier version of this gate walked the blueprint's cut list and never read the "
            "delivered file, which made every pass vacuous and made a reference whose own cuts "
            "sit off its own onsets impossible to build against."
        ),
        "blueprint_beat_alignment": blueprint_beat_alignment,
        "blueprint_beat_alignment_scope": (
            "REPORTED, NOT GATED: how far the blueprint's own planned cuts sit from the "
            "reference's onsets. A blueprint-side drift is a BLUEPRINT_LOCKED question, not a "
            "reason to fail a build that faithfully executed it."
        ),
    }


def _visual_qc(
    project_dir: Path,
    revision: str,
    candidate_hash: str,
    assets: Dict[str, Any],
    board: Path,
    reference_lock: Dict[str, Any],
    render_receipt_path: Path,
    reference_relative: Dict[str, Any],
    caption_contrast: Dict[str, Any],
) -> Dict[str, Any]:
    typography = []
    typography_ok = True
    for layer in assets.get("typography_layers", []):
        try:
            typography.append(validate_typography_layer(layer))
        except Exception as exc:
            typography_ok = False
            typography.append({"status": "FAIL", "error": str(exc), "text": layer.get("text")})
    receipt_path = project_dir / "review" / f"agent-visual-review-{revision}.json"
    if not receipt_path.is_file():
        machine_failed = reference_relative.get("status") == "FAIL" or caption_contrast.get("status") == "FAIL" or not typography_ok
        return {
            "status": "FAIL" if machine_failed else "BLOCKED",
            "reason": (
                "ship-blocking reference-relative, typography, or caption-contrast comparison failed; an agent receipt cannot override it"
                if machine_failed
                else "The reviewing agent must inspect the complete normal-speed candidate plus the reference/candidate board and record a CLI-signed, hash-bound visual review receipt"
            ),
            "candidate_sha256": candidate_hash,
            "comparison_board": str(board),
            "comparison_board_sha256": sha256_file(board),
            "receipt_required": str(receipt_path),
            "reference_relative": reference_relative,
            "caption_contrast": caption_contrast,
            "typography": typography,
        }
    receipt = load_json(receipt_path, root=project_dir)
    try:
        signature = verify_payload(receipt, purpose="agent-visual-review-v1")
        signature_error = None
    except SignatureError as exc:
        signature = {"status": "FAIL"}
        signature_error = str(exc)
    required_true = [
        "normal_speed_full_watch",
        "reference_side_by_side_checked",
        "typography_checked",
        "color_checked",
        "cut_and_beat_checked",
    ]
    checks = {
        "reference_relative_gate": reference_relative.get("status") in {"PASS", "NOT_APPLICABLE"},
        "caption_contrast_gate": caption_contrast.get("status") == "PASS",
        "signature": signature.get("status") == "PASS",
        "candidate_hash": receipt.get("candidate_sha256") == candidate_hash,
        "reference_hash": receipt.get("reference_sha256") == reference_lock["source"]["sha256"],
        "comparison_board_hash": receipt.get("comparison_board_sha256") == sha256_file(board),
        "render_receipt_hash": receipt.get("render_receipt_sha256") == sha256_file(render_receipt_path),
        "reelctl_version": receipt.get("reelctl_version") == __version__,
        "receipt_pass": receipt.get("status") == "PASS",
        "reviewer": receipt.get("reviewer") == "agent-vision",
        "typography_contracts": typography_ok,
        **{key: receipt.get(key) is True for key in required_true},
    }
    return {
        "status": _resolve_visual_status(reference_relative, checks),
        "checks": checks,
        "signature": signature,
        "signature_error": signature_error,
        "receipt": str(receipt_path),
        "comparison_board": str(board),
        "comparison_board_sha256": sha256_file(board),
        "reference_relative": reference_relative,
        "caption_contrast": caption_contrast,
        "typography": typography,
    }


def _next_qc_report_path(project_dir: Path, qc_dir: Path, revision: str) -> Path:
    base = qc_dir / "qc-report.json"
    if not base.is_file():
        return base
    agent_receipt = project_dir / "review" / f"agent-visual-review-{revision}.json"
    token_source = agent_receipt if agent_receipt.is_file() else base
    token = sha256_file(token_source)[:12]
    return qc_dir / f"qc-report-{token}.json"


def run_qc(project_dir: Path, *, revision: str) -> Dict[str, Any]:
    validate_identifier(revision, kind="revision")
    project_dir = canonical_root(project_dir)
    project = load_json(project_dir / "project.json", root=project_dir)
    validate_contract("project", project)
    reference = confined_path(project_dir, Path(str(project["reference_path"])), require="file", allow_missing=False)
    reference_lock = load_json(project_dir / "reference/reference-lock.json", root=project_dir)
    verify_reference_identity(reference, reference_lock)
    blueprint = load_json(project_dir / "reference/blueprint.json", root=project_dir)
    validate_contract("blueprint", blueprint)
    verify_blueprint_identity(blueprint, reference_lock)
    selection = load_json(project_dir / "edit/selection.locked.json", root=project_dir)
    validate_contract("selection", selection)
    assets = load_json(project_dir / "assets/assets.locked.json", root=project_dir)
    validate_contract("assets", assets)
    render_receipt_path = confined_path(
        project_dir,
        project_dir / "edit" / f"render-{revision}" / "render-receipt.json",
        require="file",
        allow_missing=False,
    )
    receipt = load_json(render_receipt_path, root=project_dir)
    try:
        verify_payload(receipt, purpose="render-receipt-v1")
    except SignatureError as exc:
        raise RuntimeError(f"render receipt signature is invalid: {exc}") from exc
    candidate = confined_path(project_dir, Path(receipt["review"]["path"]), require="file", allow_missing=False)
    candidate_hash = sha256_file(candidate)
    if candidate_hash != receipt["review"]["sha256"]:
        raise RuntimeError("candidate hash no longer matches its render receipt")
    technical = _technical_qc(reference, candidate, reference_lock)
    structure = _structure_qc(
        candidate,
        blueprint,
        selection,
        project.get("mode", "reference-locked"),
        reference_lock,
        receipt,
    )
    qc_dir = secure_mkdirs(project_dir, "review", f"qc-{revision}")
    board = qc_dir / "reference-candidate-board.jpg"
    _make_comparison_board(reference, candidate, blueprint["picture_blocks"], board)
    reference_relative = reference_relative_visual_qc(
        reference,
        candidate,
        blueprint["picture_blocks"],
        mode=project.get("mode", "reference-locked"),
    )
    caption_contrast = caption_contrast_qc(candidate, assets, project_dir)
    visual = _visual_qc(
        project_dir,
        revision,
        candidate_hash,
        assets,
        board,
        reference_lock,
        render_receipt_path,
        reference_relative,
        caption_contrast,
    )
    human_receipt = project_dir / "review" / f"human-approval-{revision}.json"
    human = "PENDING"
    authorities = combine_authorities(
        technical=technical["status"],
        structure=structure["status"],
        visual=visual["status"],
        human=human,
    )
    report = {
        "schema_version": 1,
        "revision": revision,
        "candidate": str(candidate),
        "candidate_sha256": candidate_hash,
        "reference_sha256": reference_lock["source"]["sha256"],
        "render_receipt_sha256": sha256_file(render_receipt_path),
        "technical": technical,
        "structure": structure,
        "visual": visual,
        "human": {
            "status": human,
            "receipt": None,
            "untrusted_project_receipt_ignored": human_receipt.is_file(),
            "reason": "human approval cannot be inferred from a project-local JSON file",
        },
        "authorities": authorities,
    }
    validate_contract("qc", report)
    output = _next_qc_report_path(project_dir, qc_dir, revision)
    atomic_write_json(output, report, root=project_dir)
    report["report_path"] = str(output)
    return report
