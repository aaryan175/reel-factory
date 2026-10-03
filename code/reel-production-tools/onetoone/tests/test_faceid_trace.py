"""`faceid trace` (an analysis aid for letterform matching) turns one clean
caption line into a TrueType face whose glyphs sit on the source ink. Proof here: set a line in Helvetica,
trace it, set the same letters in the traced face at the same cap height, and the two inks overlap
(IoU >= 0.9 per glyph); a second call adds glyphs and keeps the first; --need lists what is missing;
a bad box reports the segmentation instead of guessing. No reference footage, no ffmpeg."""
import json
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from onetoone import faceid

HELV = "/System/Library/Fonts/Helvetica.ttc"
pytestmark = pytest.mark.skipif(not os.path.exists(HELV), reason="needs the macOS Helvetica for the source line")


def _line_png(tmp_path, text, name, size=220):
    f = ImageFont.truetype(HELV, size, index=1)        # Helvetica Bold
    bb = f.getbbox(text)
    im = Image.new("RGB", (bb[2] - bb[0] + 120, bb[3] - bb[1] + 120), "white")
    ImageDraw.Draw(im).text((60 - bb[0], 60 - bb[1]), text, font=f, fill="black")
    p = tmp_path / name
    im.save(p)
    return p, (40, 40, im.width - 40, im.height - 40)


def _mask(font_path, ch, cap_px):
    f = ImageFont.truetype(str(font_path), int(round(cap_px * faceid.UPM / faceid.CAP)))
    bb = f.getbbox(ch)
    im = Image.new("L", (bb[2] - bb[0] + 8, bb[3] - bb[1] + 8), 0)
    ImageDraw.Draw(im).text((4 - bb[0], 4 - bb[1]), ch, font=f, fill=255)
    a = np.asarray(im) > 128
    ys, xs = np.where(a)
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _iou(a, b):
    """Shape overlap: b is resampled onto a's ink box, so only the letterform counts (the traced
    line maps its TALLEST glyph to the cap height, so sizes differ by the round-letter overshoot)."""
    B = np.asarray(Image.fromarray((b * 255).astype("uint8")).resize((a.shape[1], a.shape[0]), Image.BILINEAR)) > 128
    return (a & B).sum() / max(1, (a | B).sum())


def test_traced_face_sets_the_source_glyphs(tmp_path):
    png, box = _line_png(tmp_path, "RIGHT HOUSE", "line1.png")
    out = tmp_path / "Traced.ttf"
    rc = faceid.main(["trace", "--frame", str(png), "--box", ",".join(map(str, box)), "--ink", "0,0,0",
                      "--text", "RIGHT HOUSE", "--out", str(out), "--name", "Traced Test"])
    assert rc == 0 and out.exists()
    side = json.loads((out.with_suffix(".ttf.glyphs.json")).read_text())
    assert set(side["glyphs"]) == set("RIGHTHOUSE ") and side["sources"][0]["report"]["segmentation"] == "exact"
    assert out.with_suffix(".ttf.sheet.png").exists()
    # the built face renders each glyph on the source ink
    f_src = ImageFont.truetype(HELV, 220, index=1)
    cap_px = f_src.getbbox("H")[3] - f_src.getbbox("H")[1]
    for ch in "RIGHTOUSE":
        bb = f_src.getbbox(ch)
        im = Image.new("L", (bb[2] - bb[0] + 8, bb[3] - bb[1] + 8), 0)
        ImageDraw.Draw(im).text((4 - bb[0], 4 - bb[1]), ch, font=f_src, fill=255)
        a = np.asarray(im) > 128
        ys, xs = np.where(a)
        src = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        ours = _mask(out, ch, cap_px)
        assert _iou(src, ours) >= 0.9, f"{ch}: traced glyph does not sit on the source ink"
    # the space advance came from the word gap, not a guess
    assert 150 < side["glyphs"][" "]["advance"] < 450


def test_second_line_adds_glyphs_and_need_reports_missing(tmp_path):
    png, box = _line_png(tmp_path, "RIGHT HOUSE", "line1.png")
    out = tmp_path / "Traced.ttf"
    faceid.main(["trace", "--frame", str(png), "--box", ",".join(map(str, box)), "--ink", "0,0,0", "--text", "RIGHT HOUSE", "--out", str(out)])
    assert faceid.main(["trace", "--need", "DAY GOING", "--out", str(out)]) == 3
    png2, box2 = _line_png(tmp_path, "DAY GOING", "line2.png")
    assert faceid.main(["trace", "--frame", str(png2), "--box", ",".join(map(str, box2)), "--ink", "0,0,0", "--text", "DAY GOING",
                        "--out", str(out), "--need", "DAY GOING RIGHT HOUSE"]) == 0
    side = json.loads((out.with_suffix(".ttf.glyphs.json")).read_text())
    assert set("DANYGOUHTSEIR") <= set(side["glyphs"]) and len(side["sources"]) == 2
    assert faceid.main(["trace", "--need", "DAY GOING RIGHT HOUSE", "--out", str(out)]) == 0
    # the face loads as a real TrueType with every glyph mapped
    from fontTools.ttLib import TTFont
    tt = TTFont(str(out))
    assert all(ord(c) in tt.getBestCmap() for c in "DANYGOUHTSEIR ")


def test_wrong_text_reports_segmentation_instead_of_guessing(tmp_path, capsys):
    png, box = _line_png(tmp_path, "RIGHT HOUSE", "line1.png")
    out = tmp_path / "Traced.ttf"
    rc = faceid.main(["trace", "--frame", str(png), "--box", ",".join(map(str, box)), "--ink", "0,0,0", "--text", "RIGHT HOUSE TOO", "--out", str(out)])
    assert rc == 2 and not out.exists()
    assert "segmentation" in capsys.readouterr().out
