from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reelctl.hashing import recipe_hash
from reelctl.media import probe_media
from reelctl.reference import (
    ReferenceError,
    analyze_reference,
    lock_blueprint,
    verify_blueprint_identity,
    verify_reference_lock_receipt,
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
            "sine=frequency=440:sample_rate=48000:duration=1.5",
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
            "-video_track_timescale",
            "24000",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


def test_reference_lock_records_exact_clock_audio_and_boundaries(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    make_reference(reference)
    lock = analyze_reference(reference, tmp_path / "analysis")
    assert lock["clock"]["frame_count"] == 36
    assert lock["clock"]["fps"] == "24/1"
    assert lock["clock"]["time_base"] == "1/24000"
    assert lock["clock"]["pts_step"] == 1000
    assert lock["audio"]["present"] is True
    assert lock["audio"]["start_pts"] == 0
    assert lock["audio"]["time_base"] == "1/48000"
    assert lock["audio"]["packet_ledger"]["packet_count"] > 0
    assert lock["decoded_video"]["frame_count"] == 36
    assert len(lock["decoded_video"]["frames"]) == 36
    assert lock["signature"]["purpose"] == "reference-lock-v1"
    assert lock["draft_boundaries_after"] == [11, 23]
    assert [block["frames"] for block in lock["picture_blocks"]] == [12, 12, 12]
    assert (tmp_path / "analysis/reference-lock.json").is_file()
    assert (tmp_path / "analysis/reference-all-frames-board.jpg").is_file()


def test_probe_normalizes_missing_sar_to_one(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    make_reference(reference)
    facts = probe_media(reference)
    assert facts["video"]["sample_aspect_ratio"] == "1:1"


def test_picture_states_can_include_held_transitions_without_claiming_hard_cuts(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    make_reference(reference)
    lock = analyze_reference(reference, tmp_path / "analysis")
    observations = {
        "p001": {
            "description": "red field opening",
            "role": "red opening",
            "transition_from_previous": "opening",
            "evidence_frames": [0, 5],
        },
        "p002": {
            "description": "red held transition state",
            "role": "held red state",
            "transition_from_previous": "held_transition",
            "evidence_frames": [6, 11],
        },
        "p003": {
            "description": "green field after cut",
            "role": "green field",
            "transition_from_previous": "hard_cut",
            "evidence_frames": [12, 23],
        },
        "p004": {
            "description": "blue field after cut",
            "role": "blue field",
            "transition_from_previous": "hard_cut",
            "evidence_frames": [24, 35],
        },
    }
    blueprint = lock_blueprint(
        lock,
        [5, 11, 23],
        tmp_path / "analysis/blueprint.json",
        hard_cuts_after=[11, 23],
        observations=observations,
        all_frames_reviewed=True,
    )
    assert blueprint["picture_boundaries_after"] == [5, 11, 23]
    assert blueprint["hard_cuts_after"] == [11, 23]
    assert len(blueprint["picture_blocks"]) == 4
    assert [block["frames"] for block in blueprint["picture_blocks"]] == [6, 6, 12, 12]
    assert blueprint["signature"]["purpose"] == "blueprint-lock-v1"


def test_reference_and_blueprint_reject_self_consistent_unkeyed_forgery(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    make_reference(reference)
    lock = analyze_reference(reference, tmp_path / "analysis")
    forged_lock = dict(lock)
    forged_lock["audio_onsets"] = [{"time_s": 0.1, "frame": 2, "strength": 999.0}]
    forged_lock["recipe_hash"] = recipe_hash({key: value for key, value in forged_lock.items() if key not in {"recipe_hash", "signature"}})
    with pytest.raises(ReferenceError, match="signature"):
        verify_reference_lock_receipt(forged_lock)

    observations = {
        "p001": {"description": "red", "role": "red", "transition_from_previous": "opening", "evidence_frames": [0]},
        "p002": {"description": "green", "role": "green", "transition_from_previous": "hard_cut", "evidence_frames": [12]},
        "p003": {"description": "blue", "role": "blue", "transition_from_previous": "hard_cut", "evidence_frames": [24]},
    }
    blueprint = lock_blueprint(
        lock,
        [11, 23],
        tmp_path / "analysis/blueprint.json",
        observations=observations,
        all_frames_reviewed=True,
    )
    forged_blueprint = dict(blueprint)
    forged_blueprint["picture_blocks"] = [dict(item) for item in blueprint["picture_blocks"]]
    forged_blueprint["picture_blocks"][0]["role"] = "forged role"
    forged_blueprint["sha256_contract"] = recipe_hash(
        {key: value for key, value in forged_blueprint.items() if key not in {"sha256_contract", "signature"}}
    )
    with pytest.raises(ReferenceError, match="signature"):
        verify_blueprint_identity(forged_blueprint, lock)


def test_blueprint_lock_carries_repeat_group_through_to_locked_blocks(tmp_path: Path) -> None:
    """The schema declares ``repeat_group`` and ``validate_selection`` requires it for any
    source reuse — a writer that cannot emit it makes reference-continuity reels
    (derived states: smears, inversions, unbroken finales) structurally unlockable."""
    reference = tmp_path / "reference.mp4"
    make_reference(reference)
    lock = analyze_reference(reference, tmp_path / "analysis")
    observations = {
        "p001": {"description": "red", "role": "red", "transition_from_previous": "opening",
                 "evidence_frames": [0], "repeat_group": "run-p001"},
        "p002": {"description": "red smear", "role": "smear off red", "transition_from_previous": "held_transition",
                 "evidence_frames": [12], "repeat_group": "run-p001"},
        "p003": {"description": "blue", "role": "blue", "transition_from_previous": "hard_cut",
                 "evidence_frames": [24]},
    }
    blueprint = lock_blueprint(
        lock,
        [11, 23],
        tmp_path / "analysis/blueprint.json",
        hard_cuts_after=[23],
        observations=observations,
        all_frames_reviewed=True,
    )
    groups = [block.get("repeat_group") for block in blueprint["picture_blocks"]]
    assert groups == ["run-p001", "run-p001", None]


def test_blueprint_lock_rejects_a_blank_repeat_group(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    make_reference(reference)
    lock = analyze_reference(reference, tmp_path / "analysis")
    observations = {
        "p001": {"description": "red", "role": "red", "transition_from_previous": "opening",
                 "evidence_frames": [0], "repeat_group": "   "},
        "p002": {"description": "green", "role": "green", "transition_from_previous": "hard_cut",
                 "evidence_frames": [12]},
        "p003": {"description": "blue", "role": "blue", "transition_from_previous": "hard_cut",
                 "evidence_frames": [24]},
    }
    with pytest.raises(ReferenceError, match="repeat_group"):
        lock_blueprint(
            lock,
            [11, 23],
            tmp_path / "analysis/blueprint.json",
            observations=observations,
            all_frames_reviewed=True,
        )
