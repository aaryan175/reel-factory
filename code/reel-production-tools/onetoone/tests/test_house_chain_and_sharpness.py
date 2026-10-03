"""House colour chain, caption sharpness floor and exact YUV round-trip.

Every check has a case it must REJECT: the bare-lut3d chain fails the approved-pipe test, a blurred caption
fails the sharpness floor, a swscale-style -2 offset fails the round-trip."""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from onetoone.housechain import LUT_PACKAGED, house_vf, is_approved_chain
from onetoone.sharpness import EDGE_WIDTH_MAX_PX, edge_width_px
from onetoone.yuvexact import rgb_to_yuv420, yuv420_to_rgb

TOOLS = str(Path(__file__).resolve().parents[2])
from _fixtures import fixture

APPROVED_REEL = str(fixture("approved-house-grade", "approved_reel.mp4"))
from onetoone.looksheet import resolve_master
_m = resolve_master("CLIP_0071")
CLIP_0071 = str(_m) if _m else ""
BARE = f"lut3d=file={LUT_PACKAGED},scale=1920:1080,setsar=1,format=yuv420p"


def test_house_chain_carries_the_approved_contract_and_bare_lut3d_does_not():
    assert is_approved_chain(house_vf(1916, 1078)) == []
    missing = is_approved_chain(BARE)
    assert "in_color_matrix=bt709" in missing and "interp=tetrahedral" in missing and "range=tv" in missing


def _frame(vf, src, ss, w=1920, h=1080):
    cmd = [sys.executable, "-m", "onetoone.ffx", "--", "-v", "error", "-ss", ss, "-i", src, "-an", "-vf", vf,
           "-frames:v", "1", "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709", "-f", "rawvideo", "-"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, check=True, cwd=TOOLS, env={**os.environ, "PYTHONPATH": TOOLS})
    return yuv420_to_rgb(p.stdout, w, h).astype(np.float64)


@pytest.mark.skipif(not (os.path.exists(APPROVED_REEL) and CLIP_0071 and os.path.exists(CLIP_0071)), reason="approved reel fixture / its master not on disk (REEL_FACTORY_FIXTURES)")
def test_house_chain_reproduces_the_approved_cold_open_and_bare_chain_does_not():
    cmd = [sys.executable, "-m", "onetoone.ffx", "--", "-v", "error", "-i", APPROVED_REEL, "-frames:v", "1", "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, check=True, cwd=TOOLS, env={**os.environ, "PYTHONPATH": TOOLS})
    ref = yuv420_to_rgb(p.stdout, 1920, 1080).astype(np.float64)
    good = _frame(house_vf(1920, 1080), CLIP_0071, "4.6")
    bare = _frame(BARE, CLIP_0071, "4.6")
    d_good = np.abs(good - ref).reshape(-1, 3).mean(0)
    d_bare = np.abs(bare - ref).reshape(-1, 3).mean(0)
    assert d_good.max() < 1.5, d_good      # same pixels as the approved reel (measured 0.96 mean abs)
    assert d_bare.max() > 3.0, d_bare      # the bare chain is visibly off (measured R -4.2 / B +3.3 mean)
    # warmth: approved pipe has the higher R/B on this shot
    assert good[..., 0].mean() / good[..., 2].mean() > bare[..., 0].mean() / bare[..., 2].mean() + 0.03


def _glyph_frame(blur=0.0):
    im = Image.new("RGB", (400, 160), (30, 24, 20))
    d = ImageDraw.Draw(im)
    for i in range(5):
        d.rounded_rectangle((30 + i * 70, 40, 60 + i * 70, 120), 4, fill=(255, 255, 255))
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    return np.asarray(im)


def test_sharpness_floor_accepts_hard_edges_and_rejects_soft_ones():
    hard = edge_width_px(_glyph_frame(), (0, 0, 400, 160))
    # the mask is thresholded at 235 so a blur shrinks the ink, but its edges spread: width must exceed the floor
    soft_frame = _glyph_frame(1.6)
    soft_mask = soft_frame.min(-1) >= 200
    soft = edge_width_px(soft_frame, (0, 0, 400, 160), mask=soft_mask)
    assert hard is not None and hard <= EDGE_WIDTH_MAX_PX, hard
    assert soft is not None and soft > EDGE_WIDTH_MAX_PX, soft


def test_yuv_roundtrip_is_exact_and_a_swscale_style_offset_would_fail_it():
    rng = np.random.default_rng(3)
    base = np.repeat(np.repeat(rng.integers(20, 235, (18, 32, 3), dtype=np.uint8), 2, 0), 2, 1)   # chroma-flat 2x2 blocks
    back = yuv420_to_rgb(rgb_to_yuv420(base), 64, 36).astype(int)
    assert np.abs(back - base.astype(int)).max() <= 2
    assert np.abs(back - base.astype(int)).mean() < 0.6
    dark = np.clip(base.astype(int) - 2, 0, 255)          # what swscale's decode did to every level
    assert np.abs(dark - base.astype(int)).mean() > 1.5
