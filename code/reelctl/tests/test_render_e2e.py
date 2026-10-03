from __future__ import annotations

import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from reelctl import __version__
from reelctl.assets import compose_overlay_sequence
from reelctl.errors import ContractError
from reelctl.footage import index_footage
from reelctl.hashing import atomic_write_json, sha256_file
from reelctl.media import audio_payload_sha256, decode_pcm_sha256, legal_luma_range, probe_media
from reelctl.qc import run_qc
from reelctl.reference import analyze_reference, lock_blueprint
from reelctl.render import REVIEW_LUMA_HEADROOM, render_project
from reelctl.selection import bind_selection_to_inventory
from reelctl.signing import sign_payload
from reelctl.typography import create_traced_glyph_provenance


def make_clip(path: Path, color: str, frames: int = 30) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x180:r=24",
            "-frames:v",
            str(frames),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            "-video_track_timescale",
            "24000",
            str(path),
        ],
        check=True,
    )


def make_reference(path: Path) -> None:
    filters = (
        "color=c=red:s=320x180:r=24:d=0.5[a];"
        "color=c=green:s=320x180:r=24:d=0.5[b];"
        "color=c=blue:s=320x180:r=24:d=0.5[c];"
        "[a][b][c]concat=n=3:v=1:a=0[v]"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=1.5",
            "-filter_complex",
            filters,
            "-map",
            "[v]",
            "-map",
            "0:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            "-video_track_timescale",
            "24000",
            str(path),
        ],
        check=True,
    )


def prepare_project(tmp_path: Path) -> Path:
    project = tmp_path / "demo"
    for name in ("reference", "footage", "edit", "assets", "review", "deliver", ".reelctl/cache"):
        (project / name).mkdir(parents=True, exist_ok=True)
    reference = project / "reference/reference-source.mp4"
    make_reference(reference)
    lock = analyze_reference(reference, project / "reference")
    blueprint = lock_blueprint(
        lock,
        [11, 23],
        project / "reference/blueprint.json",
        observations={
            "p001": {
                "description": "red field",
                "role": "red field",
                "transition_from_previous": "opening",
                "evidence_frames": [0, 11],
            },
            "p002": {
                "description": "green field",
                "role": "green field",
                "transition_from_previous": "hard_cut",
                "evidence_frames": [12, 23],
            },
            "p003": {
                "description": "blue field",
                "role": "blue field",
                "transition_from_previous": "hard_cut",
                "evidence_frames": [24, 35],
            },
        },
        all_frames_reviewed=True,
    )
    source_root = tmp_path / "source-footage"
    source_root.mkdir()
    sources = []
    for color in ("red", "green", "blue"):
        source = source_root / f"{color}.mp4"
        make_clip(source, color)
        sources.append(source)
    inventory = index_footage(source_root, project / "footage", default_profile="rec709", project_root=project)
    slots = []
    for block, source, color in zip(blueprint["picture_blocks"], sources, ("red", "green", "blue")):
        slots.append(
            {
                "block_id": block["id"],
                "frames": block["frames"],
                "reference_role": block["role"],
                "candidate_observation": f"solid {color} candidate frame",
                "source_path": str(source),
                "source_sha256": sha256_file(source),
                "source_bytes": source.stat().st_size,
                "source_start_frame": 0,
                "speed": 1.0,
                "reverse": False,
                "lighting_family": color,
                "input_profile": "rec709",
                "input_range": "tv",
                "profile_proof": sign_payload(
                    {
                        "schema_version": 1,
                        "status": "AGENT_VERIFIED",
                        "method": "scope_verified_display_referred",
                        "source_sha256": sha256_file(source),
                        "input_range": "tv",
                        "evidence": "synthetic ffmpeg source is explicitly generated as display-referred Rec.709",
                    },
                    purpose="color-profile-proof-v1",
                ),
                "technical_transform": "identity",
                "creative": {},
                "grade_proof": {"status": "AGENT_REVIEWED"},
                "crop_anchor_xy": [0.5, 0.5],
            }
        )
    selection = bind_selection_to_inventory({"schema_version": 1, "status": "DRAFT", "slots": slots}, inventory)
    atomic_write_json(project / "edit/selection.locked.json", selection)
    feasibility_rows = []
    for block, slot in zip(blueprint["picture_blocks"], selection["slots"]):
        feasibility_rows.append(
            {
                "block_id": block["id"],
                "reference_role": block["role"],
                "coverage": "exact_scene_available",
                "evidence": f"{slot['source_clip_id']} contains the exact solid-color scene",
                "evidence_clip_ids": [slot["source_clip_id"]],
                "decision": "EXACT_SCENE_SELECTED",
            }
        )
    atomic_write_json(
        project / "edit/feasibility.locked.json",
        {"schema_version": 1, "status": "PASS", "blocks": feasibility_rows},
    )
    atomic_write_json(
        project / "assets/assets.locked.json",
        {"schema_version": 1, "typography_layers": [], "effect_layers": [], "overlay_sequence": None, "status": "PASS"},
    )
    atomic_write_json(
        project / "project.json",
        {
            "schema_version": 1,
            "project_id": "demo",
            "mode": "reference-locked",
            "reference_input": str(reference),
            "reference_path": str(reference),
            "footage_root": str(source_root),
            "output": {"master_codec": "prores_ks", "master_pix_fmt": "yuv422p10le", "review_codec": "libx264", "color": "bt709"},
            "policies": {
                # explicit opt-in: the synthetic reference is ours, so we hold its rights
                "audio": "reference_audio_rights_held",
                "timing": "exact_reference_pts_and_picture_blocks",
                "typography": "exact_font_hash_or_traced_reference_glyph",
                "speed": "normal_only_no_reverse",
                "color": "identified_input_profile_then_per_shot_grade",
                "publication": "human_approval_required",
            },
        },
    )
    return project


def test_render_is_deterministic_and_preserves_reference_audio_clock(tmp_path: Path) -> None:
    project = prepare_project(tmp_path)
    first = render_project(project, revision="v001")
    second = render_project(project, revision="v001")
    assert second["cache_hit"] is True
    assert first["review_sha256"] == second["review_sha256"]
    review = Path(first["review_path"])
    reference = project / "reference/reference-source.mp4"
    facts = probe_media(review)
    assert facts["video"]["frame_count"] == 36
    assert facts["video"]["r_frame_rate"] == "24/1"
    assert facts["video"]["time_base"] == "1/24000"
    assert facts["video"]["sample_aspect_ratio"] == "1:1"
    assert facts["video"]["color_primaries"] == "bt709"
    assert decode_pcm_sha256(review) == decode_pcm_sha256(reference)
    assert audio_payload_sha256(review) == audio_payload_sha256(reference)
    assert facts["audio"]["start_pts"] == 0
    assert facts["audio"]["time_base"] == "1/48000"
    assert legal_luma_range(review)["status"] == "PASS"


def make_licensed_track(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
            "-c:a", "aac", str(path),
        ],
        check=True,
    )


def _set_audio_policy(project: Path, policy: str, track: "Path | None") -> None:
    config = json.loads((project / "project.json").read_text())
    config["policies"]["audio"] = policy
    config["audio_track"] = str(track) if track else None
    atomic_write_json(project / "project.json", config)


def test_default_policy_refuses_to_render_without_a_licensed_track(tmp_path: Path) -> None:
    project = prepare_project(tmp_path)
    _set_audio_policy(project, "licensed_track_required", None)
    with pytest.raises(ContractError, match="licensed_track_required"):
        render_project(project, revision="v001")


def test_licensed_track_is_muxed_and_verified_instead_of_the_reference_audio(tmp_path: Path) -> None:
    project = prepare_project(tmp_path)
    track = project / "assets/licensed-audio.m4a"
    make_licensed_track(track)
    _set_audio_policy(project, "licensed_track_required", track)
    result = render_project(project, revision="v001")
    review = Path(result["review_path"])
    reference = project / "reference/reference-source.mp4"
    assert decode_pcm_sha256(review) == decode_pcm_sha256(track)
    assert decode_pcm_sha256(review) != decode_pcm_sha256(reference)
    assert audio_payload_sha256(review) == audio_payload_sha256(track)
    receipt = json.loads(Path(result["render_receipt_path"]).read_text())
    assert receipt["audio_policy"] == "licensed_track_required"
    assert receipt["audio_source"]["sha256"] == sha256_file(track)


def test_render_rejects_tampered_render_and_segment_cache_receipts(tmp_path: Path) -> None:
    project = prepare_project(tmp_path)
    first = render_project(project, revision="v001")
    render_receipt = Path(first["render_receipt_path"])
    payload = json.loads(render_receipt.read_text(encoding="utf-8"))
    payload["publication"]["status"] = "FORGED"
    atomic_write_json(render_receipt, payload, root=project)
    with pytest.raises(ContractError, match="signature"):
        render_project(project, revision="v001")

    # Restore a valid render receipt by using a fresh project, then forge one
    # segment sidecar while keeping its unkeyed recipe/hash fields plausible.
    second_project = prepare_project(tmp_path / "second")
    render_project(second_project, revision="v001")
    segment_receipt = next((second_project / ".reelctl/cache/segments").glob("*.json"))
    segment_payload = json.loads(segment_receipt.read_text(encoding="utf-8"))
    segment_payload["bytes"] = int(segment_payload["bytes"]) + 1
    atomic_write_json(segment_receipt, segment_payload, root=second_project)
    with pytest.raises(ContractError, match="signature"):
        render_project(second_project, revision="v002")


def test_qc_separates_machine_and_human_authority(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REELCTL_AGENT_REVIEW_KEY", str(tmp_path / "agent-review.key"))
    project = prepare_project(tmp_path)
    render = render_project(project, revision="v001")
    candidate_hash = render["review_sha256"]
    blocked = run_qc(project, revision="v001")
    first_report_path = Path(blocked["report_path"])
    assert blocked["visual"]["status"] == "BLOCKED"
    board = project / "review/qc-v001/reference-candidate-board.jpg"
    render_receipt = project / "edit/render-v001/render-receipt.json"
    reference = project / "reference/reference-source.mp4"
    unsigned = {
        "schema_version": 1,
        "candidate_sha256": candidate_hash,
        "status": "PASS",
        "normal_speed_full_watch": True,
        "reference_side_by_side_checked": True,
        "typography_checked": True,
        "color_checked": True,
        "cut_and_beat_checked": True,
        "reviewer": "agent-vision",
        "reelctl_version": __version__,
        "reference_sha256": sha256_file(reference),
        "comparison_board_sha256": sha256_file(board),
        "render_receipt_sha256": sha256_file(render_receipt),
        "notes": "synthetic fixture visually inspected",
    }
    atomic_write_json(project / "review/agent-visual-review-v001.json", unsigned)
    forged = run_qc(project, revision="v001")
    assert forged["visual"]["status"] == "FAIL"
    assert Path(forged["report_path"]) != first_report_path
    assert first_report_path.is_file()
    signed = sign_payload(
        unsigned,
        purpose="agent-visual-review-v1",
    )
    atomic_write_json(
        project / "review/agent-visual-review-v001.json",
        signed,
    )
    atomic_write_json(
        project / "review/human-approval-v001.json",
        {"candidate_sha256": candidate_hash, "status": "APPROVED"},
    )
    report = run_qc(project, revision="v001")
    assert report["technical"]["status"] == "PASS"
    assert report["structure"]["status"] == "PASS"
    assert report["visual"]["status"] == "PASS"
    assert report["authorities"]["overall"] == "LOCAL_REVIEW_READY"
    assert report["authorities"]["publishable"] is False
    assert report["human"]["status"] == "PENDING"
    assert report["human"]["untrusted_project_receipt_ignored"] is True


def test_render_composites_hash_locked_exact_overlay_frames(tmp_path: Path) -> None:
    project = prepare_project(tmp_path)
    plate = project / "assets/exact-glyph.png"
    rgba = np.zeros((180, 320, 4), dtype=np.uint8)
    rgba[70:110, 130:190, :3] = 255
    rgba[70:110, 130:190, 3] = 255
    Image.fromarray(rgba).save(plate)
    reference_mask = project / "assets/exact-glyph-mask.png"
    Image.fromarray(rgba[..., 3]).save(reference_mask)
    provenance = create_traced_glyph_provenance(
        project / "reference/reference-source.mp4",
        reference_mask,
        plate,
        project / "assets/exact-glyph-provenance.json",
        source_frame=5,
        extraction_roi=[0, 0, 320, 180],
        extraction_method="synthetic-white-glyph-mask-v1",
    )
    manifest = {
        "schema_version": 1,
        "status": "PASS",
        "typography_layers": [
            {
                "id": "exact-glyph",
                "text": "EXACT",
                "proof": "traced_reference_glyph",
                "plate_path": str(plate),
                "plate_sha256": sha256_file(plate),
                "source_frame": 5,
                "extraction_roi": [0, 0, 320, 180],
                "provenance_receipt_path": provenance["receipt_path"],
                "provenance_receipt_sha256": provenance["receipt_sha256"],
                "start_frame": 5,
                "end_frame_exclusive": 6,
            }
        ],
        "effect_layers": [],
    }
    overlay = compose_overlay_sequence(manifest, project / "assets/overlay-locked", width=320, height=180, frame_count=36)
    manifest["overlay_sequence"] = overlay["pattern"]
    manifest["overlay_recipe_hash"] = overlay["recipe_hash"]
    manifest["overlay_receipt_sha256"] = sha256_file(project / "assets/overlay-locked/overlay-receipt.json")
    atomic_write_json(project / "assets/assets.locked.json", manifest)
    result = render_project(project, revision="v-overlay")
    capture = cv2.VideoCapture(result["review_path"])
    capture.set(cv2.CAP_PROP_POS_FRAMES, 5)
    ok, frame = capture.read()
    capture.release()
    assert ok
    center = frame[80:100, 145:175]
    # The review proxy clamps luma to REVIEW_LUMA_HEADROOM before the lossy encode, so a pure
    # white glyph cannot read 255 here — it reads the ceiling converted out of tv range, less a
    # little codec slack. Derive the floor from the constant so this stays pinned to "the glyph
    # is composited and white" instead of re-breaking every time the headroom moves. The base
    # clips are pure red/green/blue (mean ~85), so this still fails loudly if the overlay is
    # missing.
    white_ceiling = (REVIEW_LUMA_HEADROOM[1] - 16) * 255.0 / 219.0
    assert float(center.mean()) > white_ceiling - 6.0
