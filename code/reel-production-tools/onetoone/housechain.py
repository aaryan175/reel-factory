"""onetoone.housechain — the approved reels' colour PIPE.

'House grade' = the hash-locked Sony LC-709 LUT and nothing else — but it only reproduces the approved
reels' pixels when the LUT is run through the same pipe they used:

    scale (lanczos)  ->  in_range=full, in_color_matrix=bt709   (the camera masters are untagged pc-range; a bare
    lut3d makes swscale assume BT.601 and the picture ships cooler and darker)  ->  gbrpf32le  ->  lut3d tetrahedral
    ->  gbrp16le  ->  colorspace=...range=tv (BT.709, fsb dither)  ->  yuv420p

A bare `lut3d=file=LUT` chain (onetoone.render's house branch used one before it switched to
house_head + HOUSE_TAIL, see tests/test_house_pipe_render.py) measured b* 6.5 / R/B 1.96 on a probe frame;
this chain measures b* 9.4 / R/B 2.26 there, and reproduces an approved cold-open frame to a mean RGB
difference of 0.03 (bare chain: -4.2 R, +3.3 B). L0037."""
from __future__ import annotations

import os
from pathlib import Path

#: The packaged LC-709 LUT shipped with reelctl (override with REEL_FACTORY_LUT).
LUT_PACKAGED = os.environ.get("REEL_FACTORY_LUT") or str(
    Path(__file__).resolve().parents[2] / "reelctl" / "src" / "reelctl" / "data" / "luts" / "Sony-LC-709-official.cube")

REQUIRED_TOKENS = ("in_range=full", "in_color_matrix=bt709", "format=gbrpf32le", "interp=tetrahedral",
                   "format=gbrp16le", "colorspace=", "range=tv", "all=bt709")


HOUSE_TAIL = "colorspace=ispace=gbr:iprimaries=bt709:itrc=bt709:irange=pc:all=bt709:range=tv:format=yuv420p:dither=fsb"


def house_head(w: int, h: int, lut: str = LUT_PACKAGED, cover: bool = True, in_range: str = "full") -> str:
    """The pipe up to the graded 16-bit RGB picture (no trailing comma). `in_range` is "full" for the camera
    masters (untagged pc-range) and "tv" for a range-converted intermediate (onetoone.render's ProRes zoom
    plate measures tv: luma 13389..39206 of the master's 10506..42089). Follow with HOUSE_TAIL to ship."""
    pre = (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}," if cover else
           f"scale={w}:{h}:flags=lanczos,")
    return (f"{pre}scale=iw:ih:flags=lanczos:in_range={in_range}:out_range=full:in_color_matrix=bt709,format=gbrpf32le,"
            f"lut3d=file='{lut}':interp=tetrahedral,format=gbrp16le")


def house_vf(w: int, h: int, lut: str = LUT_PACKAGED, cover: bool = True, fps: str | None = None) -> str:
    """ffmpeg -vf for ONE shot in house mode, ending yuv420p tv BT.709 (tag the encode bt709/tv)."""
    vf = f"{house_head(w, h, lut, cover)},{HOUSE_TAIL}"
    if fps:
        vf += f",fps={fps}"
    return vf + ",setsar=1,format=yuv420p"


def is_approved_chain(vf: str) -> list[str]:
    """Return the contract tokens MISSING from a vf string (empty list = it carries the approved pipe)."""
    return [t for t in REQUIRED_TOKENS if t not in vf]
