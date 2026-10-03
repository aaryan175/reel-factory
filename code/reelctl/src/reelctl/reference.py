from __future__ import annotations

from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .contracts import validate_contract
from .hashing import atomic_write_bytes, atomic_write_json, recipe_hash, sha256_file
from .media import (
    audio_packet_ledger,
    audio_payload_sha256,
    decode_pcm_sha256,
    frame_pts,
    media_toolchain_identity,
    probe_media,
    run_checked,
    video_frame_ledger,
)
from .paths import canonical_root
from .signing import SignatureError, sign_payload, verify_payload


class ReferenceError(RuntimeError):
    pass


def _reference_lock_core(reference_lock: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in reference_lock.items() if key not in {"recipe_hash", "signature"}}


def verify_reference_lock_receipt(reference_lock: Dict[str, Any]) -> None:
    if reference_lock.get("recipe_hash") != recipe_hash(_reference_lock_core(reference_lock)):
        raise ReferenceError("reference lock recipe hash is invalid")
    try:
        verify_payload(reference_lock, purpose="reference-lock-v1")
    except SignatureError as exc:
        raise ReferenceError(f"reference lock signature is invalid: {exc}") from exc


def verify_blueprint_identity(blueprint: Dict[str, Any], reference_lock: Dict[str, Any]) -> None:
    core = {key: value for key, value in blueprint.items() if key not in {"sha256_contract", "signature"}}
    if blueprint.get("sha256_contract") != recipe_hash(core):
        raise ReferenceError("blueprint content no longer matches its locked contract hash")
    try:
        verify_payload(blueprint, purpose="blueprint-lock-v1")
    except SignatureError as exc:
        raise ReferenceError(f"blueprint signature is invalid: {exc}") from exc
    if (
        blueprint.get("reference_sha256") != reference_lock["source"]["sha256"]
        or blueprint.get("reference_lock_recipe_hash") != reference_lock["recipe_hash"]
        or blueprint.get("all_frames_board_sha256") != reference_lock["analysis_artifacts"]["all_frames_board_sha256"]
    ):
        raise ReferenceError("blueprint is not bound to the current reference lock and all-frames review board")


def _save_image_atomic(image: "Any", output: Path, *, quality: int) -> None:
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    atomic_write_bytes(output, buffer.getvalue(), root=output.parent, mode=0o600)


def verify_reference_identity(path: Path, reference_lock: Dict[str, Any]) -> Dict[str, Any]:
    verify_reference_lock_receipt(reference_lock)
    path = Path(path).resolve()
    source = reference_lock.get("source", {})
    facts = probe_media(path)
    clock = reference_lock.get("clock", {})
    audio = reference_lock.get("audio", {})
    artifacts = reference_lock.get("analysis_artifacts", {})
    draft_board = Path(str(artifacts.get("blueprint_draft_board_path", "")))
    all_frames_board = Path(str(artifacts.get("all_frames_board_path", "")))
    checks = {
        "lock_recipe": reference_lock.get("recipe_hash") == recipe_hash(_reference_lock_core(reference_lock)),
        "source_path": str(path) == str(Path(str(source.get("path", ""))).resolve()),
        "source_bytes": int(facts["bytes"]) == int(source.get("bytes", -1)),
        "source_sha256": facts["sha256"] == source.get("sha256"),
        "video_pts": frame_pts(path) == clock.get("pts"),
        "decoded_video_frames": video_frame_ledger(path) == reference_lock.get("decoded_video"),
        "decoder_toolchain": media_toolchain_identity() == reference_lock.get("toolchain"),
        "video_clock": facts["video"].get("r_frame_rate") == clock.get("fps")
        and facts["video"].get("time_base") == clock.get("time_base")
        and int(facts["video"].get("frame_count") or 0) == int(clock.get("frame_count", -1)),
        "audio_payload": audio_payload_sha256(path) == audio.get("payload_sha256"),
        "audio_pcm": decode_pcm_sha256(path) == audio.get("pcm_s16le_48k_mono_sha256"),
        "audio_packets": audio_packet_ledger(path) == audio.get("packet_ledger"),
        "draft_board": not draft_board.is_symlink()
        and draft_board.is_file()
        and sha256_file(draft_board) == artifacts.get("blueprint_draft_board_sha256"),
        "all_frames_board": not all_frames_board.is_symlink()
        and all_frames_board.is_file()
        and sha256_file(all_frames_board) == artifacts.get("all_frames_board_sha256"),
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise ReferenceError(f"reference bytes/clock/audio changed after lock: {', '.join(failed)}")
    return {"status": "PASS", "checks": checks, "source_sha256": facts["sha256"]}


def _decode_analysis_frames(path: Path) -> List["Any"]:
    import cv2

    frames: List[Any] = []
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ReferenceError(f"OpenCV cannot decode reference: {path}")
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            height, width = frame.shape[:2]
            scale = min(1.0, 320.0 / max(width, 1))
            resized = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
            frames.append(resized)
    finally:
        capture.release()
    if not frames:
        raise ReferenceError("reference decoded zero frames")
    return frames


def _cut_scores(frames: Sequence["Any"]) -> List[float]:
    import cv2
    import numpy as np

    scores: List[float] = []
    for left, right in zip(frames, frames[1:]):
        l_lab = cv2.cvtColor(left, cv2.COLOR_BGR2LAB).astype(np.float32)
        r_lab = cv2.cvtColor(right, cv2.COLOR_BGR2LAB).astype(np.float32)
        pixel = float(np.mean(np.abs(l_lab - r_lab)) / 255.0)
        l_hist = cv2.calcHist([left], [0, 1], None, [16, 16], [0, 256, 0, 256])
        r_hist = cv2.calcHist([right], [0, 1], None, [16, 16], [0, 256, 0, 256])
        cv2.normalize(l_hist, l_hist)
        cv2.normalize(r_hist, r_hist)
        hist = float(cv2.compareHist(l_hist, r_hist, cv2.HISTCMP_BHATTACHARYYA))
        scores.append(0.55 * pixel + 0.45 * hist)
    return scores


def detect_hard_cuts(frames: Sequence["Any"]) -> Dict[str, Any]:
    import numpy as np

    scores = _cut_scores(frames)
    if not scores:
        return {"boundaries_after": [], "scores": [], "threshold": 1.0}
    array = np.asarray(scores, dtype=np.float64)
    median = float(np.median(array))
    mad = float(np.median(np.abs(array - median)))
    q95 = float(np.quantile(array, 0.95))
    threshold = max(0.115, median + 8.0 * max(mad, 0.002), min(0.35, q95 * 0.68))
    raw = [index for index, score in enumerate(scores) if score >= threshold]
    boundaries: List[int] = []
    for index in raw:
        if boundaries and index - boundaries[-1] <= 2:
            if scores[index] > scores[boundaries[-1]]:
                boundaries[-1] = index
        else:
            boundaries.append(index)
    return {"boundaries_after": boundaries, "scores": [round(float(v), 7) for v in scores], "threshold": round(threshold, 7)}


def picture_blocks(frame_count: int, boundaries_after: Sequence[int]) -> List[Dict[str, Any]]:
    boundaries = sorted(set(int(v) for v in boundaries_after))
    if any(v < 0 or v >= frame_count - 1 for v in boundaries):
        raise ReferenceError("boundary lies outside the reference clock")
    blocks: List[Dict[str, Any]] = []
    start = 0
    for index, boundary in enumerate(boundaries + [frame_count - 1], start=1):
        blocks.append(
            {
                "id": f"p{index:03d}",
                "start_frame": start,
                "end_frame_exclusive": boundary + 1,
                "frames": boundary - start + 1,
                "role": "UNOBSERVED",
                "reference_observation": "PENDING_AGENT_VISION",
            }
        )
        start = boundary + 1
    return blocks


def _write_reference_board(frames: Sequence["Any"], blocks: Sequence[Dict[str, Any]], output: Path) -> None:
    import cv2
    from PIL import Image, ImageDraw

    columns = 4
    tile_width = 280
    label_height = 34
    tiles = []
    for block in blocks:
        frame_index = (int(block["start_frame"]) + int(block["end_frame_exclusive"]) - 1) // 2
        frame = frames[frame_index]
        height, width = frame.shape[:2]
        tile_height = max(1, round(height * tile_width / width))
        resized = cv2.resize(frame, (tile_width, tile_height), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        tile = Image.new("RGB", (tile_width, tile_height + label_height), "white")
        tile.paste(Image.fromarray(rgb), (0, label_height))
        ImageDraw.Draw(tile).text((7, 7), f"{block['id']}  frame {frame_index}", fill="black")
        tiles.append(tile)
    if not tiles:
        return
    rows = (len(tiles) + columns - 1) // columns
    board = Image.new("RGB", (columns * tiles[0].width, rows * tiles[0].height), (238, 238, 238))
    for index, tile in enumerate(tiles):
        board.paste(tile, ((index % columns) * tile.width, (index // columns) * tile.height))
    canonical_root(output.parent, create=True)
    _save_image_atomic(board, output, quality=92)


def _write_all_frames_board(frames: Sequence["Any"], output: Path) -> None:
    import cv2
    from PIL import Image, ImageDraw

    columns = 12
    tile_width = 150
    label_height = 20
    tiles = []
    for frame_index, frame in enumerate(frames):
        frame_height, frame_width = frame.shape[:2]
        tile_height = max(1, round(frame_height * tile_width / frame_width))
        resized = cv2.resize(frame, (tile_width, tile_height), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        tile = Image.new("RGB", (tile_width, tile_height + label_height), "white")
        tile.paste(Image.fromarray(rgb), (0, label_height))
        ImageDraw.Draw(tile).text((5, 3), f"f{frame_index:04d}", fill="black")
        tiles.append(tile)
    rows = (len(tiles) + columns - 1) // columns
    board = Image.new("RGB", (columns * tiles[0].width, rows * tiles[0].height), (225, 225, 225))
    for index, tile in enumerate(tiles):
        board.paste(tile, ((index % columns) * tile.width, (index // columns) * tile.height))
    canonical_root(output.parent, create=True)
    _save_image_atomic(board, output, quality=90)


def _audio_onsets(path: Path, fps: Fraction) -> List[Dict[str, Any]]:
    import numpy as np

    result = run_checked(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0?", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
        capture_binary=True,
    )
    audio = np.frombuffer(result.stdout, dtype=np.float32)
    if not len(audio):
        return []
    hop = 480
    usable = (len(audio) // hop) * hop
    energy = np.sqrt(np.mean(audio[:usable].reshape(-1, hop) ** 2, axis=1) + 1e-12)
    novelty = np.maximum(0.0, np.diff(energy, prepend=energy[0]))
    median = float(np.median(novelty))
    mad = float(np.median(np.abs(novelty - median)))
    threshold = median + 5.0 * max(mad, 1e-5)
    candidates = [
        i for i in range(1, len(novelty) - 1) if novelty[i] >= threshold and novelty[i] >= novelty[i - 1] and novelty[i] >= novelty[i + 1]
    ]
    retained: List[int] = []
    for index in candidates:
        if not retained or index - retained[-1] >= 5:
            retained.append(index)
        elif novelty[index] > novelty[retained[-1]]:
            retained[-1] = index
    return [
        {
            "time_s": round(index * hop / 48000.0, 6),
            "frame": int(round(index * hop / 48000.0 * float(fps))),
            "strength": round(float(novelty[index]), 8),
        }
        for index in retained
    ]


def analyze_reference(path: Path, output_dir: Path) -> Dict[str, Any]:
    path = Path(path).resolve()
    output_dir = canonical_root(output_dir, create=True)
    facts = probe_media(path)
    video = facts["video"]
    pts = frame_pts(path)
    frame_count = int(video.get("frame_count") or len(pts))
    if len(pts) != frame_count:
        raise ReferenceError(f"decoded frame count {frame_count} differs from PTS ledger {len(pts)}")
    if frame_count < 1:
        raise ReferenceError("reference has no frames")
    if pts[0] != 0:
        raise ReferenceError("reference video starts at non-zero PTS; an explicit timestamp-preserving normalization stage is required")
    step_values = [b - a for a, b in zip(pts, pts[1:])]
    pts_step = step_values[0] if step_values and all(v == step_values[0] for v in step_values) else None
    if pts_step is None and frame_count > 1:
        raise ReferenceError("reference uses a non-uniform presentation clock; normalize explicitly before reconstruction")
    fps = Fraction(video["r_frame_rate"])
    frames = _decode_analysis_frames(path)
    if len(frames) != frame_count:
        raise ReferenceError(f"OpenCV decoded {len(frames)} frames; ffprobe counted {frame_count}")
    decoded_video = video_frame_ledger(path)
    if int(decoded_video["frame_count"]) != frame_count:
        raise ReferenceError("decoded video hash ledger does not cover every reference frame")
    cuts = detect_hard_cuts(frames)
    audio_stream = facts["audio"]
    if audio_stream is not None and int(audio_stream.get("start_pts") or 0) != 0:
        raise ReferenceError(
            "reference audio starts at non-zero PTS; normalize the exact reference without changing decoded PCM before locking"
        )
    sar_text = str(video["sample_aspect_ratio"])
    sar_numerator, sar_denominator = (int(value) for value in sar_text.split(":"))
    display_width = max(2, round(int(video["width"]) * sar_numerator / sar_denominator))
    display_height = int(video["height"])
    if int(video.get("rotation", 0)) in {90, 270}:
        display_width, display_height = display_height, display_width
    display_width += display_width % 2
    display_height += display_height % 2
    lock: Dict[str, Any] = {
        "schema_version": 1,
        "source": {"path": str(path), "bytes": facts["bytes"], "sha256": facts["sha256"]},
        "clock": {
            "coded_width": int(video["width"]),
            "coded_height": int(video["height"]),
            "width": display_width,
            "height": display_height,
            "rotation": int(video.get("rotation", 0)),
            "sample_aspect_ratio": video["sample_aspect_ratio"],
            "fps": video["r_frame_rate"],
            "time_base": video["time_base"],
            "frame_count": frame_count,
            "pts_start": pts[0],
            "pts_step": pts_step,
            "pts": pts,
            "duration_ts": int(video.get("duration_ts") or (pts_step or 0) * frame_count),
        },
        "audio": {
            "present": audio_stream is not None,
            "codec": audio_stream.get("codec_name") if audio_stream else None,
            "time_base": audio_stream.get("time_base") if audio_stream else None,
            "start_pts": int(audio_stream.get("start_pts") or 0) if audio_stream else None,
            "duration_ts": int(audio_stream.get("duration_ts") or 0) if audio_stream else None,
            "sample_rate": int(audio_stream.get("sample_rate") or 0) if audio_stream else None,
            "channels": int(audio_stream.get("channels") or 0) if audio_stream else None,
            "payload_sha256": audio_payload_sha256(path),
            "pcm_s16le_48k_mono_sha256": decode_pcm_sha256(path),
            "packet_ledger": audio_packet_ledger(path),
        },
        "decoded_video": decoded_video,
        "toolchain": media_toolchain_identity(),
        "draft_boundaries_after": cuts["boundaries_after"],
        "cut_detection": {"threshold": cuts["threshold"], "scores": cuts["scores"], "status": "DRAFT_REQUIRES_AGENT_VISION_LOCK"},
        "picture_blocks": picture_blocks(frame_count, cuts["boundaries_after"]),
        "audio_onsets": _audio_onsets(path, fps) if facts["audio"] is not None else [],
        "lock_status": "REFERENCE_CLOCK_LOCKED_BLUEPRINT_DRAFT",
    }
    _write_reference_board(frames, lock["picture_blocks"], output_dir / "blueprint-draft-board.jpg")
    _write_all_frames_board(frames, output_dir / "reference-all-frames-board.jpg")
    lock["analysis_artifacts"] = {
        "blueprint_draft_board_path": str(output_dir / "blueprint-draft-board.jpg"),
        "blueprint_draft_board_sha256": sha256_file(output_dir / "blueprint-draft-board.jpg"),
        "all_frames_board_path": str(output_dir / "reference-all-frames-board.jpg"),
        "all_frames_board_sha256": sha256_file(output_dir / "reference-all-frames-board.jpg"),
    }
    lock["recipe_hash"] = recipe_hash(lock)
    lock = sign_payload(lock, purpose="reference-lock-v1")
    atomic_write_json(output_dir / "reference-lock.json", lock, root=output_dir)
    return lock


def lock_blueprint(
    reference_lock: Dict[str, Any],
    boundaries_after: Sequence[int],
    output_path: Path,
    *,
    hard_cuts_after: Optional[Sequence[int]] = None,
    observations: Optional[Dict[str, Any]] = None,
    all_frames_reviewed: bool = False,
) -> Dict[str, Any]:
    verify_reference_lock_receipt(reference_lock)
    frame_count = int(reference_lock["clock"]["frame_count"])
    picture_boundaries = sorted(set(int(value) for value in boundaries_after))
    hard_cuts = picture_boundaries if hard_cuts_after is None else sorted(set(int(value) for value in hard_cuts_after))
    if not set(hard_cuts).issubset(set(picture_boundaries)):
        raise ReferenceError("every hard cut must also be a picture-state boundary")
    if not all_frames_reviewed:
        raise ReferenceError("blueprint lock requires an explicit all-frames-reviewed attestation")
    blocks = picture_blocks(frame_count, picture_boundaries)
    observations = observations or {}
    if set(observations) != {block["id"] for block in blocks}:
        raise ReferenceError("blueprint lock requires one structured observation for every ordered picture state")
    boundary_provenance = []
    valid_non_hard = {"held_transition", "effect_state", "reframe", "other_picture_state"}
    for index, block in enumerate(blocks):
        observation = observations[block["id"]]
        if not isinstance(observation, dict):
            raise ReferenceError(f"{block['id']} observation must be a structured object")
        description = str(observation.get("description", "")).strip()
        role = str(observation.get("role", "")).strip()
        evidence_frames = observation.get("evidence_frames", [])
        if not description or description.upper().startswith(("PENDING_", "REPLACE_", "UNOBSERVED")):
            raise ReferenceError(f"{block['id']} lacks an independent visible-state description")
        if not role or role.upper().startswith(("PENDING_", "REPLACE_", "UNOBSERVED")):
            raise ReferenceError(f"{block['id']} lacks a reference role")
        if not isinstance(evidence_frames, list) or not evidence_frames:
            raise ReferenceError(f"{block['id']} lacks evidence frame indices")
        if any(
            isinstance(frame, bool)
            or not isinstance(frame, int)
            or frame < int(block["start_frame"])
            or frame >= int(block["end_frame_exclusive"])
            for frame in evidence_frames
        ):
            raise ReferenceError(f"{block['id']} evidence frame lies outside its locked half-open interval")
        transition = str(observation.get("transition_from_previous", ""))
        if index == 0:
            if transition != "opening":
                raise ReferenceError("p001 transition_from_previous must be opening")
        else:
            boundary = int(block["start_frame"]) - 1
            if boundary in hard_cuts and transition != "hard_cut":
                raise ReferenceError(f"{block['id']} must identify its locked hard cut")
            if boundary not in hard_cuts and transition not in valid_non_hard:
                raise ReferenceError(f"{block['id']} must classify its non-hard picture-state transition")
            boundary_provenance.append(
                {
                    "after_frame": boundary,
                    "kind": transition,
                    "evidence": str(observation.get("boundary_evidence", description)).strip(),
                }
            )
        block.update(
            {
                "reference_observation": description,
                "role": role,
                "transition_from_previous": transition,
                "evidence_frames": evidence_frames,
            }
        )
        # Derived picture states (smears, polarity inversions, unbroken finales) share one
        # source; ``validate_selection`` only permits that reuse when the blueprint names
        # the run. The schema has always declared the field — this is the writer for it.
        repeat_group = observation.get("repeat_group")
        if repeat_group is not None:
            if not isinstance(repeat_group, str) or not repeat_group.strip():
                raise ReferenceError(f"{block['id']} repeat_group must be a non-empty string when given")
            block["repeat_group"] = repeat_group.strip()
    blueprint = {
        "schema_version": 1,
        "reference_sha256": reference_lock["source"]["sha256"],
        "reference_lock_recipe_hash": reference_lock["recipe_hash"],
        "all_frames_board_sha256": reference_lock["analysis_artifacts"]["all_frames_board_sha256"],
        "clock": reference_lock["clock"],
        "picture_boundaries_after": picture_boundaries,
        "hard_cuts_after": hard_cuts,
        "boundary_provenance": boundary_provenance,
        "picture_blocks": blocks,
        "caption_layers": [],
        "effect_layers": [],
        "all_frames_reviewed": True,
        "status": "LOCKED",
    }
    blueprint["sha256_contract"] = recipe_hash(blueprint)
    blueprint = sign_payload(blueprint, purpose="blueprint-lock-v1")
    validate_contract("blueprint", blueprint)
    atomic_write_json(output_path, blueprint, root=output_path.parent)
    return blueprint
