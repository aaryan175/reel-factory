"""Tests for the P2 variant render loop's decision logic.

Everything that decides something — which sources may be rendered, what the selection handed
to the engine contains, where the caption layer lands on the 9:16 canvas, which crop the
vertical pass takes, which encode ships — is tested here on fixtures. The ffmpeg and PIL
plumbing is deliberately not mocked into a fake pass: it is proven by rendering a real
variant and pasting the real probe output into the phase receipt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_reelctl" / "src"))

import variant_render as vr  # noqa: E402
from reelctl.contracts import load_schema  # noqa: E402

# ---------------------------------------------------------------------------------------
# geometry


def test_cover_geometry_matches_ffmpeg_scale_to_cover_for_the_vertical_pass():
    cover = vr.cover_geometry(3840, 2160, 1080, 1920)
    assert cover["scaled_h"] == 1920
    assert cover["scaled_w"] == 3413
    assert cover["slack_y"] == 0
    assert cover["slack_x"] == 2333


def test_cover_geometry_leaves_no_slack_when_source_and_target_share_an_aspect():
    cover = vr.cover_geometry(3840, 2160, 1916, 1078)
    assert (cover["scaled_w"], cover["scaled_h"]) == (1916, 1078)
    assert cover["slack_x"] == cover["slack_y"] == 0


def test_cover_geometry_refuses_nonsense():
    with pytest.raises(ValueError):
        vr.cover_geometry(0, 100, 10, 10)


def test_caption_layer_placement_reproduces_the_approved_letterboxed_view():
    place = vr.caption_layer_placement(1916, 1078, 1080, 1920)
    # fitted by width, so a caption that spanned 47% of the 16:9 frame still spans 47% of
    # the 9:16 frame — same apparent size, same apparent position, no black bars
    assert place["scaled_w"] == 1080
    assert place["scaled_h"] == 608  # 1078 * 1080 / 1916 = 607.63
    assert place["x"] == 0
    assert place["y"] == (1920 - 608) // 2 == 656
    assert place["scale"] == pytest.approx(1080 / 1916)


def test_caption_layer_placement_is_identity_on_the_reference_canvas():
    place = vr.caption_layer_placement(1916, 1078, 1916, 1078)
    assert (place["scaled_w"], place["scaled_h"], place["x"], place["y"]) == (1916, 1078, 0, 0)


# ---------------------------------------------------------------------------------------
# reframe anchor


def test_flat_energy_gives_a_centred_crop():
    assert vr.anchor_from_column_energy([1.0] * 100, 30) == 0.5


def test_energy_on_the_left_pulls_the_crop_left_but_not_to_the_edge():
    energy = [5.0] * 20 + [0.1] * 80
    anchor = vr.anchor_from_column_energy(energy, 20)
    assert anchor < 0.5
    # the centre prior keeps it off the extreme edge even when all the energy is at frame left
    assert anchor >= vr.ANCHOR_CENTRE_PRIOR * 0.5


def test_energy_on_the_right_pulls_the_crop_right():
    energy = [0.1] * 80 + [5.0] * 20
    assert vr.anchor_from_column_energy(energy, 20) > 0.5


def test_a_window_as_wide_as_the_frame_has_nothing_to_choose():
    assert vr.anchor_from_column_energy([1.0, 9.0, 1.0], 3) == 0.5
    assert vr.anchor_from_column_energy([1.0, 9.0, 1.0], 99) == 0.5


def test_anchor_refuses_an_empty_profile_or_an_out_of_range_prior():
    with pytest.raises(ValueError):
        vr.anchor_from_column_energy([], 10)
    with pytest.raises(ValueError):
        vr.anchor_from_column_energy([1.0, 2.0], 1, centre_prior=1.5)


# ---------------------------------------------------------------------------------------
# delivery encode


def test_the_first_crf_under_the_rail_cap_wins():
    assert vr.web_crf_for_size([(21, 11_000_000), (23, 9_500_000)]) == 23


def test_crf21_wins_when_it_already_fits():
    assert vr.web_crf_for_size([(21, 6_000_000)]) == 21


def test_a_ladder_that_never_fits_returns_no_choice_instead_of_shipping_an_oversize_file():
    assert vr.web_crf_for_size([(21, 20_000_000), (23, 18_000_000)]) is None


def test_the_cap_is_the_ig_web_rail_cap():
    assert vr.WEB_SIZE_CAP_BYTES == 10 * 1024 * 1024
    assert vr.WEB_CRF_LADDER[0] == 21


# ---------------------------------------------------------------------------------------
# clock


CLOCK = {
    "frame_count": 172,
    "fps": "2997/125",
    "time_base": "1/11988",
    "duration_ts": 86000,
    "width": 1916,
    "height": 1078,
}


def _facts(**overrides):
    video = {
        "frame_count": 172,
        "r_frame_rate": "2997/125",
        "time_base": "1/11988",
        "duration_ts": 86000,
        "width": 1080,
        "height": 1920,
        "rotation": 0,
        "sample_aspect_ratio": "1:1",
        "color_space": "bt709",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
    }
    video.update(overrides)
    return {"video": video}


def test_the_vertical_encode_is_measured_against_the_blueprint_clock_not_its_own_geometry():
    checks = vr.clock_checks(_facts(), CLOCK, geometry=(1080, 1920))
    assert all(checks.values()), checks


def test_a_dropped_frame_fails_the_clock():
    checks = vr.clock_checks(_facts(frame_count=171), CLOCK, geometry=(1080, 1920))
    assert checks["frame_count"] is False


def test_a_letterboxed_or_wrong_size_encode_fails_the_geometry_check():
    checks = vr.clock_checks(_facts(width=1916, height=1078), CLOCK, geometry=(1080, 1920))
    assert checks["geometry"] is False


def test_untagged_colour_fails():
    checks = vr.clock_checks(_facts(color_transfer="smpte170m"), CLOCK, geometry=(1080, 1920))
    assert checks["rec709_tags"] is False


# ---------------------------------------------------------------------------------------
# front door


def _casting(source="/root/masters/a.MP4", digest="a" * 64):
    return {
        "slots": [
            {
                "block_id": "p001",
                "frames": 47,
                "reference_role": "hook",
                "candidate_observation": "MACHINE-CAST, NOT INDEPENDENTLY WATCHED",
                "source_path": source,
                "source_sha256": digest,
                "source_clip_id": "clip1",
                "source_start_frame": 12,
                "crop_anchor_xy": [0.5, 0.5],
                "tags": {"lighting": "day", "world_cluster": "cluster-a"},
            }
        ]
    }


def _index(relative="masters/a.MP4", digest="a" * 64, path="/root/masters/a.MP4", status="PASS"):
    return {
        "status": "PASS",
        "clips": [{"path": path, "relative_path": relative, "sha256": digest, "clip_id": "clip1", "status": status}],
    }


def test_front_door_admits_an_indexed_master():
    report = vr.front_door_report(_casting(), _index())
    assert report["status"] == "PASS"
    assert report["authorized"][0]["clip_id"] == "clip1"


def test_front_door_refuses_a_source_that_is_not_in_the_project_index():
    report = vr.front_door_report(_casting(source="/elsewhere/nice-clip.MP4"), _index())
    assert report["status"] == "FAIL"
    assert "not a PASS member" in report["violations"][0]["reason"]


def test_front_door_refuses_a_proxy_even_when_it_is_indexed():
    report = vr.front_door_report(
        _casting(source="/root/proxies/a.MP4"),
        _index(relative="proxies/a.MP4", path="/root/proxies/a.MP4"),
    )
    assert report["status"] == "FAIL"
    assert "masters/" in report["violations"][0]["reason"]


def test_front_door_refuses_a_source_whose_bytes_moved_since_the_index():
    report = vr.front_door_report(_casting(digest="b" * 64), _index())
    assert report["status"] == "FAIL"
    assert "hash differs" in report["violations"][0]["reason"]


def test_a_footage_index_that_is_not_pass_closes_the_door_entirely():
    index = _index()
    index["status"] = "FAIL"
    with pytest.raises(vr.VariantRenderError):
        vr.front_door_report(_casting(), index)


# ---------------------------------------------------------------------------------------
# selection


PROOF = {
    "schema_version": 1,
    "status": "AGENT_VERIFIED",
    "method": "camera_metadata",
    "source_sha256": "a" * 64,
    "input_range": "full",
    "evidence": "paired Sony XML AcquisitionRecord rows",
    "signature": {"algorithm": "HMAC-SHA256"},
}


def test_selection_slots_ship_identity_creative_grading():
    slots = vr.build_selection_slots(_casting(), {"clip1": PROOF}, lut_path="/lut.cube", lut_sha256="c" * 64)
    assert slots[0]["creative"] == {"exposure_stops": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0}
    assert slots[0]["technical_transform"] == "sony_lc709"
    assert slots[0]["speed"] == 1.0 and slots[0]["reverse"] is False


def test_a_profile_proof_bound_to_other_bytes_is_refused_rather_than_reused():
    proof = dict(PROOF, source_sha256="b" * 64)
    with pytest.raises(vr.VariantRenderError):
        vr.build_selection_slots(_casting(), {"clip1": proof}, lut_path="/lut.cube", lut_sha256="c" * 64)


def test_selection_slots_carry_only_keys_the_engine_schema_allows():
    schema = load_schema("selection")
    allowed = set(schema["properties"]["slots"]["items"]["properties"])
    slots = vr.build_selection_slots(_casting(), {"clip1": PROOF}, lut_path="/lut.cube", lut_sha256="c" * 64)
    assert set(slots[0]) <= allowed, set(slots[0]) - allowed


def test_finishing_a_selection_declines_the_estimator_in_writing_and_keeps_identity():
    slots = vr.build_selection_slots(_casting(), {"clip1": PROOF}, lut_path="/lut.cube", lut_sha256="c" * 64)
    proposals = {
        "p001": {
            "status": "PENDING_AGENT_REVIEW",
            "block_id": "p001",
            "source_frame": 12,
            "source_metrics": {"y_q50": 0.42},
            "bounded_proposal": {"exposure_stops": 0.6, "contrast": 1.1, "saturation": 1.0, "gamma": 1.0},
        }
    }
    finished = vr.finish_selection_slots(slots, proposals, {"p001": {"lighting": "day", "world_cluster": "cluster-a"}})
    row = finished[0]
    assert row["creative"] == {"exposure_stops": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0}
    assert row["grade_proof"]["status"] == "AGENT_REVIEWED"
    assert "DECLINED" in row["grade_proof"]["review_notes"]
    assert json.dumps(proposals["p001"]["bounded_proposal"]) in row["grade_proof"]["review_notes"]
    assert row["lighting_family"] == "mid-key-day-cluster-a-measured-y50-0.420"


def test_a_grade_proof_bound_to_a_different_source_frame_is_refused():
    slots = vr.build_selection_slots(_casting(), {"clip1": PROOF}, lut_path="/lut.cube", lut_sha256="c" * 64)
    proposals = {
        "p001": {
            "block_id": "p001",
            "source_frame": 999,
            "source_metrics": {"y_q50": 0.42},
            "bounded_proposal": {},
        }
    }
    with pytest.raises(vr.VariantRenderError):
        vr.finish_selection_slots(slots, proposals, {})


def test_lighting_key_bands_are_measured_not_guessed():
    assert vr.key_band(0.10) == "low-key"
    assert vr.key_band(0.42) == "mid-key"
    assert vr.key_band(0.80) == "high-key"


# ---------------------------------------------------------------------------------------
# caption reproduction


def test_caption_reproduction_fails_loudly_when_the_engine_wrote_fewer_frames(tmp_path):
    rendered, baseline = tmp_path / "r", tmp_path / "b"
    rendered.mkdir()
    baseline.mkdir()
    for index in range(3):
        (baseline / f"caption-{index:03d}.png").write_bytes(b"ink")
    for index in range(2):
        (rendered / f"caption-{index:03d}.png").write_bytes(b"ink")
    report = vr.compare_caption_frames(rendered, baseline, 3)
    assert report["pass"] is False
    assert report["missing_frames"] == [2]


def test_caption_reproduction_passes_only_on_byte_identity_with_the_approved_program(tmp_path):
    rendered, baseline = tmp_path / "r", tmp_path / "b"
    rendered.mkdir()
    baseline.mkdir()
    for index in range(3):
        (baseline / f"caption-{index:03d}.png").write_bytes(b"ink")
        (rendered / f"caption-{index:03d}.png").write_bytes(b"ink")
    assert vr.compare_caption_frames(rendered, baseline, 3)["pass"] is True
    (rendered / "caption-001.png").write_bytes(b"different ink")
    report = vr.compare_caption_frames(rendered, baseline, 3)
    assert report["pass"] is False
    assert report["byte_identical_to_approved_program"] == 2


# ---------------------------------------------------------------------------------------
# caption legibility on the delivered picture


def _legibility_scene(tmp_path, *, picture_luma_by_frame, ink_luma=255, span=(0, 8)):
    """A caption layer over a picture whose luma changes frame by frame."""
    import cv2
    import numpy as np

    caption_dir = tmp_path / "captions"
    frames_dir = tmp_path / "frames"
    caption_dir.mkdir()
    frames_dir.mkdir()
    height, width = 120, 200
    for index in range(span[0], span[1]):
        layer = np.zeros((height, width, 4), dtype=np.uint8)
        layer[50:70, 80:120, 3] = 255
        for channel in range(3):
            layer[50:70, 80:120, channel] = ink_luma
        cv2.imwrite(str(caption_dir / f"caption-{index:03d}.png"), layer)

        picture = np.full((height, width, 3), picture_luma_by_frame[index], dtype=np.uint8)
        # the caption is burned in, as it is on any delivered frame
        picture[50:70, 80:120] = ink_luma
        cv2.imwrite(str(frames_dir / f"f{index + 1:03d}.png"), picture[:, :, ::-1])
    return caption_dir, frames_dir


def test_measure_legibility_reads_every_frame_of_the_span_not_the_midpoint(tmp_path):
    """DOCTRINE 15.3: never midpoint-only.

    The old gate read frame `(start + end) // 2` and reported that one frame as the state. Here
    the midpoint is the easiest frame in the span and frame 6 is a ghost; a midpoint reader
    calls this state clean.
    """
    luma_by_frame = {0: 20, 1: 20, 2: 20, 3: 20, 4: 20, 5: 20, 6: 250, 7: 20}
    caption_dir, frames_dir = _legibility_scene(tmp_path, picture_luma_by_frame=luma_by_frame)
    states = [{"id": "C01", "text": "Sample", "start_frame": 0, "end_frame_exclusive": 8}]

    rows = vr.measure_legibility(states, caption_dir, frames_dir)
    row = rows["C01"]
    assert row["frames_measured"] == 8
    assert row["worst_frame"] == 6, "the worst frame is reported, not the midpoint"
    assert row["separation"] < vr.SEPARATION_FLOOR
    assert row["contrast_ratio"] < 3.0


def test_measure_legibility_reports_the_wcag_ratio_and_the_weak_ink_fraction(tmp_path):
    luma_by_frame = {index: 20 for index in range(8)}
    caption_dir, frames_dir = _legibility_scene(tmp_path, picture_luma_by_frame=luma_by_frame)
    states = [{"id": "C01", "text": "Sample", "start_frame": 0, "end_frame_exclusive": 8}]
    row = vr.measure_legibility(states, caption_dir, frames_dir)["C01"]
    assert row["contrast_ratio"] > 4.5
    assert row["contrast_ratio_median"] > 4.5
    assert row["weak_ink_fraction"] <= 0.10


def test_the_per_state_grandfather_floor_is_gone(tmp_path):
    """`min` is the clause that let the
    approved reel ship `your` at 15.0 levels and 1.01:1, and let every variant inherit that
    allowance. caption_doctrine supersedes it: ink must be clean, full stop."""
    assert not hasattr(vr, "effective_floors")
    assert not hasattr(vr, "EFFECTIVE_FLOOR_RULE")
