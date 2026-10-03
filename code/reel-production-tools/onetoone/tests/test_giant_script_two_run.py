"""L0077: a ghost-script word stands ~470 px tall in the reference. Box
fitting stops at size 600 (fit_font_to_height hi) and set it at about 65% of the reference. Its
template fit scored 0.52, under REF_FIT_MIN_SCORE, so the typesetter ignored that too. The way
through is the reference's measured script metrics (ref_script_metrics -> refit.solve_script),
which are not capped. Checkable rule: a measured two-run fit renders at the measured size and
extent, past the box-fit cap."""
from pathlib import Path

import pytest
from PIL import Image

from onetoone import captions_typeset as T
from onetoone.refit import solve_script

SNELL = ("/System/Library/Fonts/Supplemental/SnellRoundhand.ttc", 0)
# measured on one reference state, read off the reference overlay (work/fit_word_ov.jpg)
METRICS = {"capital": {"x0": 575, "x1": 1023, "top": 232, "bottom": 545},
           "tail": {"x0": 915, "x1": 1455, "xh_top": 233, "baseline": 545}}


@pytest.mark.skipif(not Path(SNELL[0]).exists(), reason="Snell Roundhand not on this machine")
def test_giant_script_is_set_from_metrics_past_the_box_fit_cap():
    fit = solve_script(SNELL, "sir", METRICS)
    assert fit["capital"]["size"] > 600 and fit["tail"]["size"] > 600
    part = {"part": "ornate_word", "text": "sir", "words": ["sir"], "bbox_settled": [574, 78, 1450, 548],
            "ink_rgb": [250, 250, 250], "face_file": list(SNELL), "ref_fit": fit}
    layer = Image.new("RGBA", (1916, 1078), (0, 0, 0, 0))
    rep = T.typeset_part(layer, part, {"plain": SNELL, "ornate": SNELL})
    assert rep["fit"] == "ref_script_metrics"
    x0, y0, x1, y1 = layer.getbbox()
    # the word fills the reference's measured extent (i-dot top to the baseline), within 25 px
    assert abs(x0 - 575) <= 25 and abs(x1 - 1450) <= 25
    assert abs(y0 - 78) <= 25 and abs(y1 - 546) <= 25
