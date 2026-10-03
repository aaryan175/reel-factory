from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from reelctl.media import legal_luma_range
from reelctl.render import review_encode_filter, review_timing_args

# The regression this pins: a review proxy of a ProRes master landed one frame at Y=235
# (PASS, zero margin) and, on the next revision, at Y=236 (FAIL), while the master itself
# read YMIN=82/YMAX=948 in both. The variable is x264 ringing around hard caption edges,
# which overshoots the clamped encoder input by ~15 code values.
#
# Needs a real master: the overshoot has to land in a narrow band (>14 to go red at
# ceiling 221, <=21 to go green at 214) and no synthetic reproduces that reliably. Point
# REELCTL_LEGALIZATION_MASTER at your own 10-bit ProRes master whose frame
# DEFECT_FRAME (default 174) carries YMAX=948 to run these tests; otherwise they skip.
_MASTER_ENV = os.environ.get("REELCTL_LEGALIZATION_MASTER")
MASTER = Path(_MASTER_ENV).expanduser() if _MASTER_ENV else None
DEFECT_FRAME = int(os.environ.get("REELCTL_LEGALIZATION_DEFECT_FRAME", "174"))

# A 40-frame excerpt ending past the defect frame: enough run-up for x264 to settle into inter coding before
# it reaches the offending frame, short enough to encode in a few seconds.
EXCERPT_START_FRAME = max(0, DEFECT_FRAME - 24)
EXCERPT_FRAMES = 40
EXCERPT_FPS = "2997/125"
DEFECT_INDEX_IN_EXCERPT = DEFECT_FRAME - EXCERPT_START_FRAME
DEFECT_MASTER_YMAX = 948

# Mirrors the review encode in render.py (see _run_external_atomic call for `review`). The
# filter chain itself is imported, not copied, so the constant under test is the live one;
# these are the encoder settings that surround it. Extracting a shared command builder so
# this cannot drift is queued as v1.1 engine work.
REVIEW_ENCODER_ARGS = [
    "-c:v", "libx264",
    "-preset", "slow",
    "-crf", "12",
    "-pix_fmt", "yuv420p",
]


def test_review_timing_args_preserve_locked_pts_without_output_rate_rounding() -> None:
    args = review_timing_args(11988)
    assert args == [
        "-fps_mode", "passthrough",
        "-enc_time_base", "1:11988",
        "-video_track_timescale", "11988",
        "-bf", "0",
    ]
    assert "-r" not in args


def _require_master() -> None:
    if MASTER is None or not MASTER.is_file():
        pytest.skip("set REELCTL_LEGALIZATION_MASTER to a real ProRes master to run this regression")


def _excerpt(destination: Path) -> Path:
    # ProRes is all-intra, so a stream copy cut is frame-accurate and preserves the source
    # luma bytes exactly — the excerpt must carry the real super-legal values, not a re-encode
    # of them.
    start = EXCERPT_START_FRAME * 125 / 2997
    duration = EXCERPT_FRAMES * 125 / 2997
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{start:.6f}",
            "-t", f"{duration:.6f}",
            "-i", str(MASTER),
            "-map", "0:v:0",
            "-an",
            "-c:v", "copy",
            str(destination),
        ],
        check=True,
    )
    return destination


def _review_encode(source: Path, destination: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-i", str(source),
            "-map", "0:v:0",
            "-an",
            "-vf", review_encode_filter(),
            *REVIEW_ENCODER_ARGS,
            "-r", EXCERPT_FPS,
            "-color_range", "tv",
            "-colorspace", "bt709",
            "-color_trc", "bt709",
            "-color_primaries", "bt709",
            str(destination),
        ],
        check=True,
    )
    return destination


@pytest.mark.integration
def test_review_encode_survives_the_caption_edge_ringing_regression(tmp_path: Path) -> None:
    _require_master()
    excerpt = _excerpt(tmp_path / "excerpt.mov")

    # Guard the fixture before trusting the verdict: if the cut ever drifts off the offending
    # frame, this test must fail loudly rather than quietly pass on innocent footage.
    source_range = legal_luma_range(excerpt)
    assert source_range["frame_count"] == EXCERPT_FRAMES
    assert source_range["bit_depth"] == 10
    assert source_range["status"] == "FAIL", "excerpt no longer carries super-legal luma"
    defect = [row for row in source_range["failures"] if row["frame"] == DEFECT_INDEX_IN_EXCERPT]
    assert defect and defect[0]["ymax"] == DEFECT_MASTER_YMAX

    review = _review_encode(excerpt, tmp_path / "review.mp4")

    # The gate QC actually enforces, against the artifact QC actually measures.
    decoded = legal_luma_range(review)
    assert decoded["bit_depth"] == 8
    assert decoded["frame_count"] == EXCERPT_FRAMES
    assert decoded["status"] == "PASS", (
        f"review proxy left the legal band: observed {decoded['observed_min']}..{decoded['observed_max']}, "
        f"offending frames {decoded['failures']}"
    )
    assert decoded["observed_min"] >= 16
    assert decoded["observed_max"] <= 235


@pytest.mark.integration
def test_master_prores_is_never_legalized(tmp_path: Path) -> None:
    # The clamp is a delivery concern of the review proxy. The ProRes master is the archival
    # true render and must keep its super-legal peaks; legalizing it would silently destroy
    # highlight information the grade may depend on. (That the master carries out-of-legal
    # luma on 153/271 frames is a real upstream defect — it is tracked as v1.1 engine work,
    # not something this clamp is allowed to paper over by mutating the master.)
    _require_master()
    excerpt = _excerpt(tmp_path / "excerpt.mov")
    source_range = legal_luma_range(excerpt)
    assert source_range["legal_max"] == 940
    assert source_range["observed_max"] > source_range["legal_max"]
    assert source_range["observed_max"] == 969
