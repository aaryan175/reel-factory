"""v011 fixes for the v010 independent audit: own-ink collision gate, reference-pixel
fits, declared light states, plate redaction, text behind the subject."""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from onetoone.captions_typeset import MAX_OWN_INK_OVERLAP, own_ink_collision, typeset_state
from onetoone.refit import draw_ramped, draw_tracked, tracked_layout
from onetoone.render import behind_subject, light_state_filter, redact_filter


def _layer(box, size=(400, 200)):
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle(box, fill=(255, 255, 255, 255))
    return im


def test_own_ink_collision_flags_plain_word_inside_script_word():
    """HARD-1: the plain line typeset ON the script word must refuse, whatever the bed."""
    script = ({"part": "ornate_word"}, _layer((50, 50, 350, 150)))
    plain = ({"part": "plain_line"}, _layer((150, 90, 250, 120)))
    res = own_ink_collision([script, plain])
    assert not res["ok"] and res["overlap"] > MAX_OWN_INK_OVERLAP


def test_own_ink_collision_passes_plain_word_beneath_script():
    script = ({"part": "ornate_word"}, _layer((50, 20, 350, 90)))
    plain = ({"part": "plain_line"}, _layer((150, 120, 250, 160)))
    assert own_ink_collision([script, plain])["ok"]


def test_typeset_state_reports_own_ink():
    st = {"id": "t", "parts": [{"part": "plain_line", "text": "go", "bbox_settled": [10, 10, 110, 60]}]}
    _, rep = typeset_state(st, canvas=(200, 100))
    assert rep["own_ink"]["ok"]


def test_tracking_tightens_and_space_factor_narrows_words():
    from onetoone.captions_typeset import DEFAULT_FACES, _font
    f = _font(DEFAULT_FACES["plain"], 60)
    loose = tracked_layout(f, "a b", 0.0)
    tight = tracked_layout(f, "a b", -6.0, space_k=0.5)
    assert tight[2] < loose[2]


def test_ramped_line_grows_left_to_right():
    """A reference may set the first words of a line small and the last words large on one baseline."""
    from onetoone.captions_typeset import DEFAULT_FACES
    lay = draw_ramped((900, 200), (10, 150), "oooooooo", DEFAULT_FACES["plain"], (255, 255, 255, 255), 30, 90, 0.0)
    a = np.asarray(lay)[:, :, 3] > 40
    cols = np.nonzero(a.any(0))[0]
    left, right = a[:, cols[0]:cols[0] + 40], a[:, cols[-1] - 40:cols[-1]]
    h = lambda m: np.ptp(np.nonzero(m.any(1))[0])
    assert h(right) > 1.8 * h(left)
    # one shared baseline: the bottoms of the o's line up within a few px
    assert abs(np.nonzero(left.any(1))[0].max() - np.nonzero(right.any(1))[0].max()) <= 4


def test_light_state_whitelists_and_clamps():
    f = light_state_filter({"colorbalance": {"rm": 2.0, "bh": -0.3, "evil": "x;rm -rf"}})
    assert f == "colorbalance=rm=0.600:bh=-0.300"
    assert light_state_filter(None) == "" and light_state_filter({"colorbalance": {}}) == ""


def test_redact_filter_moves_patch_across_the_shot():
    f = redact_filter({"w": 100, "h": 50, "from": [200, 100], "to": [260, 160]}, 19)
    assert "crop=100:50" in f and "boxblur" in f and "overlay" in f
    assert "150.0+(60.0)*n/18" in f and "75.0+(60.0)*n/18" in f
    assert redact_filter(None, 19) == ""


def test_behind_subject_removes_ink_over_dark_subject_only():
    ink = Image.new("RGBA", (100, 40), (140, 125, 95, 255))
    bed = Image.new("RGB", (100, 40), (240, 230, 210))
    ImageDraw.Draw(bed).rectangle((40, 0, 60, 40), fill=(60, 50, 40))      # the subject
    out = np.asarray(behind_subject(ink, bed, {"luma_lo": 140, "luma_hi": 178}))[:, :, 3]
    assert out[20, 50] < 10          # behind the subject: gone
    assert out[20, 10] > 245         # on the sky: intact


def test_light_state_gain_is_darken_only_and_clamped():
    assert light_state_filter({"gain": 0.4}) == "colorchannelmixer=rr=0.400:gg=0.400:bb=0.400"
    assert light_state_filter({"gain": 3.0}) == ""
    assert light_state_filter({"gain": 0.01}).startswith("colorchannelmixer=rr=0.150")


def test_push_and_keys_use_zoompan_not_a_fixed_size_crop():
    """v011l audit: crop evaluates w/h once, so a crop-based push never zoomed (+0.02% measured)."""
    import pytest
    from onetoone.framing import filter_for
    f = filter_for({"zoom": 1.15, "cx": 0.5, "cy": 0.43, "push": 0.002}, 3840, 2160, aspect=1916 / 1078)
    assert f.startswith("zoompan=") and "on" in f and "d=1" in f
    k = filter_for({"keys": [[0, 1.0, 0.5, 0.5], [22, 1.8, 0.49, 0.42]]}, 3840, 2160, aspect=1916 / 1078)
    assert k.startswith("zoompan=") and "lt(on" in k
    static = filter_for({"zoom": 1.2, "cx": 0.5, "cy": 0.5}, 3840, 2160, aspect=1916 / 1078)
    assert static.startswith("crop=")
    with pytest.raises(ValueError):
        filter_for({"zoom": 1.1, "push": 0.01}, 2160, 3840, aspect=1916 / 1078)


def test_shadow_is_taken_from_the_frame_the_device_drew():
    """v012b audit: pre-baking the shadow into the settled layer changed c03's entry (two words
    arrived together instead of one at a time). The shadow must come off the live frame's alpha."""
    import numpy as np
    from onetoone.render import with_shadow
    ink = _layer((150, 80, 250, 120))
    half = ink.copy()
    half.putalpha(half.getchannel("A").point(lambda v: v // 2))   # mid-entry opacity ramp
    full_a = np.asarray(with_shadow(ink, {"radius": 8, "opacity": 0.6}))[:, :, 3]
    half_a = np.asarray(with_shadow(half, {"radius": 8, "opacity": 0.6}))[:, :, 3]
    assert half_a.max() < full_a.max()          # a fading caption fades its shadow with it
    assert half_a.sum() < full_a.sum()


def test_highlight_pull_leaves_the_subject_alone():
    """A gamma steep enough to close S09's 106-code gap took a backlit bright garment
    (a midtone) to black. The highlight pull must move the sky and not the subject."""
    import numpy as np
    from PIL import Image
    from onetoone.render import highlight_pull, luma_pull, HL_KNEE
    sky = np.full((90, 160, 3), 235, dtype=np.uint8)
    sky[60:, :] = 90                      # the subject: a midtone, ~0.35
    p = Path(tempfile.mkdtemp()) / "probe.png"; Image.fromarray(sky).save(p)
    hl = highlight_pull(p, 120.0)
    g = luma_pull(p, 120.0)
    assert hl["after"] <= 130.0                       # it does the work
    assert f"{HL_KNEE:.4f}/{HL_KNEE:.4f}" in hl["filter"]   # identity at the knee
    assert hl["kind"] == "highlight" and g.get("gamma", 1.0) < 1.0
