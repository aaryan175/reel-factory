"""L0037 closed in the kit: onetoone.render's house branch must ship the approved reels' colour
pipe, not a bare lut3d. Measured on a reference masters: patched chain vs housechain.house_vf = 0.01 mean RGB;
the old bare chain was off by up to 4 levels per channel, in a different direction per master format."""
import re
from pathlib import Path

from onetoone.housechain import HOUSE_TAIL, house_head, house_vf, is_approved_chain

RENDER = (Path(__file__).resolve().parent.parent / "render.py").read_text()


def test_head_plus_tail_is_the_approved_chain():
    assert is_approved_chain(f"{house_head(1080, 1920)},{HOUSE_TAIL}") == []
    assert house_vf(1080, 1920, fps="24000/1001") == f"{house_head(1080, 1920)},{HOUSE_TAIL},fps=24000/1001,setsar=1,format=yuv420p"


def test_range_converted_plate_enters_as_tv():
    head = house_head(1080, 1920, in_range="tv")
    assert "in_range=tv" in head and "in_range=full" not in head and "in_color_matrix=bt709" in head


def test_render_house_branch_uses_the_pipe():
    branch = RENDER[RENDER.index('if grade_mode == "house":\n            # L0037'):]
    branch = branch[:branch.index("ls = slot.get")]
    assert "house_head(w, h, LUT" in branch and "HOUSE_TAIL" in branch
    # S09 magenta: in house mode the crop must see 4:4:4, never the subsampled master
    assert 'f"format=yuv444p16le,{crop},"' in branch
    assert 'in_range="tv" if is_plate else "full"' in branch


def test_shipping_vf_puts_the_tail_before_redaction_and_final_format():
    m = re.search(r'vf = f"\{ship_pre or pre\},\{gf\},.*?fps=\{FPS_STR\}\{tail\}" \+ \(f",\{red\}" if red else ""\)', RENDER)
    assert m, "house tail must sit between fps and the redaction patch"


def test_chroma_fractions_flag_a_magenta_flood_and_pass_normal_pixels(tmp_path):
    import numpy as np
    from PIL import Image
    from onetoone.render import CHROMA_JUMP_MAX, chroma_fractions
    bad = tmp_path / "bad.png"; ok = tmp_path / "ok.png"; red = tmp_path / "red.png"
    Image.fromarray(np.full((40, 60, 3), (241, 102, 254), np.uint8)).save(bad)      # a measured chroma-corrupted frame
    Image.fromarray(np.full((40, 60, 3), (176, 170, 153), np.uint8)).save(ok)       # the same shot, fixed
    Image.fromarray(np.full((40, 60, 3), (220, 40, 30), np.uint8)).save(red)        # a red practical is NOT corruption
    assert chroma_fractions(bad)[0] > 0.9 and chroma_fractions(bad)[0] - chroma_fractions(ok)[0] > CHROMA_JUMP_MAX
    assert chroma_fractions(ok) == (0.0, 0.0) and chroma_fractions(red) == (0.0, 0.0)


def test_cut_picture_runs_the_decoded_guard_on_every_shipping_segment():
    i = RENDER.index('sanity = chroma_sanity(seg, master')
    assert RENDER.rindex('str(seg)])', 0, i) > 0 and '"chroma_sanity": sanity' in RENDER


def test_real_pixels_crop_on_a_subsampled_10bit_master_is_not_corrupted(tmp_path):
    """The exact failing window: CLIP_0068 @5.1 s, crop 3338x1878 at x=58.8, delivered 1916x1078."""
    import numpy as np
    import pytest
    from PIL import Image
    from onetoone.ffx import run
    from onetoone.looksheet import LUT, resolve_master
    m = resolve_master("CLIP_0068")
    if m is None:
        pytest.skip("master CLIP_0068 not mounted")
    crop = ("crop=w='2*floor((3839.109/1.150000)/2)':h='2*floor((2160.000/1.150000)/2)':"
            "x='clip(0.450000*3840-(3839.109/1.150000)/2+0.0000*n,0,3840-(3839.109/1.150000))':"
            "y='clip(0.300000*2160-(2160.000/1.150000)/2+0.0000*n,0,2160-(2160.000/1.150000))':exact=1")
    out = tmp_path / "s09.png"
    vf = (f"format=yuv444p16le,{crop},{house_head(1916, 1078, LUT)},{HOUSE_TAIL},"
          "scale=in_color_matrix=bt709:in_range=tv:out_range=full,format=rgb24")
    r = run(["-y", "-loglevel", "error", "-ss", "5.1", "-i", str(m), "-an", "-vf", vf, "-frames:v", "1", str(out)])
    assert r.returncode == 0
    mean = np.asarray(Image.open(out)).astype(float).mean((0, 1))
    assert abs(mean[0] - 176) < 6 and abs(mean[1] - 170) < 6 and abs(mean[2] - 153) < 6, mean   # bug gave 241/102/254
