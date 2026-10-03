"""Flat white ink on a bright bed must FAIL the halo gate; the ruled
directional shadow (radius 12, opacity 1.0, offset [4,5], grow 5) must PASS it; a weak symmetric glow
must still fail (the gate discriminates). Synthetic, no footage needed."""
import numpy as np
from PIL import Image, ImageDraw
from onetoone.render import with_shadow
from onetoone.captions_typeset import readability, HALO_MIN_RATIO

RULED = {"radius": 12, "opacity": 1.0, "offset": [4, 5], "grow": 5}
SIZE = (600, 260)


def _layer():
    im = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i in range(6):
        d.rounded_rectangle((60 + i * 80, 90, 100 + i * 80, 170), 12, fill=(255, 255, 255, 255))
    return im


def _halo(bed_rgb, spec):
    layer = _layer()
    bed = Image.new("RGBA", SIZE, bed_rgb + (255,))
    shaded = bed.copy()
    if spec:
        shaded.alpha_composite(with_shadow(layer, spec))
    a = np.asarray(layer.getchannel("A")); ys, xs = np.nonzero(a >= 64)
    box = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
    rep = {"parts": [{"text": "t", "ink": box}]}
    st = {"parts": [{"text": "t", "ink_rgb": [255, 255, 255]}]}
    return readability(bed, rep, st, ink_layer=layer, shaded_bed=shaded)["parts"][0]


def test_flat_white_on_bright_bed_fails():
    r = _halo((235, 225, 200), None)
    assert not r["contrast_ok"] and r["halo_ratio"] < HALO_MIN_RATIO


def test_ruled_directional_shadow_passes_and_radius_within_cast_rule():
    assert RULED["radius"] <= 20
    r = _halo((235, 225, 200), RULED)
    assert r["contrast_ok"] and r["halo_ratio"] >= HALO_MIN_RATIO


def test_weak_symmetric_glow_still_fails():
    r = _halo((250, 248, 240), {"radius": 12, "opacity": 0.3, "offset": [0, 0], "grow": 2})
    assert not r["contrast_ok"]
