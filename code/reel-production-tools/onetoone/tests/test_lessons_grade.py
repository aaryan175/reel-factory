"""Grade lessons from an audit chain, each one a build that failed an
independent audit. LESSONS.md L0003 / L0004 / L0005 point here."""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from onetoone.render import (PULL_PASS2_GAMMA_MIN, PULL_SHOULDER, PULL_TOL, PULL_TOL_REL, luma_pull)


def _probe(value: int, patch: int | None = None) -> Path:
    a = np.full((90, 160, 3), value, dtype=np.uint8)
    if patch is not None:
        a[:20, :40] = patch
    p = Path(tempfile.mkdtemp()) / "probe.png"
    Image.fromarray(a).save(p)
    return p


def test_L0003_pull_curve_carries_a_shoulder():
    """y = x**(1/g) maps 1.0 -> 1.0 for every g, so without a shoulder no pull can touch clipping
    (S10: 10.23% of pixels pinned at >=250 through three builds)."""
    r = luma_pull(_probe(200, patch=255), 120.0)
    assert r["filter"].rstrip("'").endswith(f"1.0000/{PULL_SHOULDER:.4f}")
    assert PULL_SHOULDER < 1.0


def test_L0004_tolerance_scales_with_the_target():
    """A flat 6-code tolerance is +-107% of a 5.6 target: the blackout shot shipped at 11.1
    uncorrected. Dark shots get a relative tolerance."""
    r = luma_pull(_probe(11), 5.6)
    assert r["gamma"] < 1.0, "an 11.1 shot against a 5.6 target must be pulled"
    assert r["tol"] == round(min(PULL_TOL, max(1.0, PULL_TOL_REL * 5.6)), 2)
    # ...and a bright shot inside the flat tolerance is still left alone
    assert luma_pull(_probe(120), 118.0)["gamma"] == 1.0


def test_L0005_second_pass_floors_higher_than_the_first():
    """Two 0.45 gammas compose to ~0.20, steeper than one 0.30. That made a black cutout of a
    backlit bright garment. The second pass must not be allowed as deep as the first."""
    assert PULL_PASS2_GAMMA_MIN >= 0.6
    deep = luma_pull(_probe(200), 40.0)                               # first pass: clamps at the floor
    second = luma_pull(_probe(200), 40.0, gamma_min=PULL_PASS2_GAMMA_MIN)
    assert second["gamma"] >= PULL_PASS2_GAMMA_MIN > deep["gamma"]


def test_render_refuses_without_a_fresh_preflight_token(tmp_path, monkeypatch):
    """audit-loop H5: every gate was prose; render() now needs preflight's token, and
    the token must match the cast bytes AND lessons.json (a new lesson invalidates it)."""
    import json, pytest
    from onetoone import render as r
    monkeypatch.setattr(r, "GATE_DIR", tmp_path / "gate")
    monkeypatch.setattr(r, "LESSONS_JSON", tmp_path / "lessons.json")
    r.LESSONS_JSON.write_text('{"lessons": []}')
    cast = tmp_path / "cast.json"; cast.write_text('{"slots": []}')
    with pytest.raises(r.PreflightRequired):
        r.require_preflight(cast)
    r.GATE_DIR.mkdir(parents=True, exist_ok=True)
    r.gate_token(cast).write_text(json.dumps({"cast_sha256": r._sha(cast), "lessons_sha256": r.lessons_sha()}))
    r.require_preflight(cast)                      # fresh, matching token: passes
    r.LESSONS_JSON.write_text('{"lessons": [1]}')  # a new lesson: every token is stale
    with pytest.raises(r.PreflightRequired):
        r.require_preflight(cast)
    cast.write_text('{"slots": [1]}')              # cast edited: token no longer matches
    with pytest.raises(r.PreflightRequired):
        r.require_preflight(cast)


def test_gate_lives_in_the_pixel_producers_not_only_render(tmp_path, monkeypatch):
    """audit-loop2 H11: render(gate=False) from Python, or importing cut_picture /
    rasterise_captions directly, rendered a finished cut with no token."""
    import pytest
    from onetoone import render as r
    monkeypatch.setattr(r, "GATE_DIR", tmp_path / "gate")
    monkeypatch.setattr(r, "LESSONS_JSON", tmp_path / "lessons.json")
    monkeypatch.delenv("REEL_NO_GATE", raising=False)
    cast = tmp_path / "cast.json"; cast.write_text('{"slots": []}')
    with pytest.raises(r.PreflightRequired):
        r.cut_picture(tmp_path, cast, tmp_path / "ref.mp4", tmp_path / "work", size=(2, 2))
    with pytest.raises(r.PreflightRequired):
        r.rasterise_captions(tmp_path, tmp_path / "work2", frames=1, size=(2, 2))
    with pytest.raises(r.PreflightRequired):
        r.render(tmp_path, cast, tmp_path / "ref.mp4", tmp_path / "o.mp4", tmp_path / "work3", gate=False)


def test_lessons_sha_ignores_harvester_bookkeeping(tmp_path):
    """audit-loop2 MEDIUM: the harvester rewrites lessons.json every 600 s (seen/inbox); only a
    change to the lesson set may invalidate a token."""
    from onetoone import render as r
    p = tmp_path / "lessons.json"
    p.write_text('{"lessons": [{"id": "L1"}], "seen": ["a"], "inbox": []}')
    a = r.lessons_sha(p)
    p.write_text('{"lessons": [{"id": "L1"}], "seen": ["a", "b"], "inbox": [{"id": "in1"}]}')
    assert r.lessons_sha(p) == a
    p.write_text('{"lessons": [{"id": "L1"}, {"id": "L2"}], "seen": ["a", "b"], "inbox": []}')
    assert r.lessons_sha(p) != a


def test_readability_gate_counts_the_shadow_the_viewer_sees():
    """audit-v018 HARD: white ink on a near-white sky measured 1.09:1 and the gate could
    only be passed with --allow-low-contrast, because it judged ink against the RAW bed and ignored
    the caption shadow. The gate now measures the surround with the shadow composited, ink masked out."""
    from PIL import Image, ImageDraw
    from onetoone.captions_typeset import readability
    from onetoone.render import with_shadow
    size = (400, 200)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for x in range(150, 250, 16):                                         # thin text-like strokes, 6 px wide
        d.rectangle((x, 80, x + 5, 120), fill=(255, 255, 255, 255))
    rep = {"parts": [{"text": "Word", "ink": [140, 70, 260, 130]}]}
    st = {"parts": [{"text": "Word", "ink_rgb": [255, 255, 255]}]}
    sky = Image.new("RGBA", size, (240, 240, 240, 255))
    raw = readability(sky.copy(), rep, st)
    assert not raw["ok"] and raw["parts"][0]["contrast"] < 25
    shaded = sky.copy(); shaded.alpha_composite(with_shadow(layer, {"radius": 12, "opacity": 1.0, "offset": [4, 5], "grow": 4}))
    got = readability(sky.copy(), rep, st, ink_layer=layer, shaded_bed=shaded)["parts"][0]
    assert got["ok"] and got["halo_ratio"] >= 3.0
    assert got["bed_sd"] == raw["parts"][0]["bed_sd"]                    # clutter = raw footage, never the shadow
    assert got["contrast_box_raw"] == raw["parts"][0]["contrast"]       # the honest shadow-free number is kept
    # audit-v019 MEDIUM-1: the gate must DISCRIMINATE — a faint shadow on the same sky still fails
    faint = sky.copy(); faint.alpha_composite(with_shadow(layer, {"radius": 14, "opacity": 0.3, "offset": [0, 2], "grow": 2}))
    weak = readability(sky.copy(), rep, st, ink_layer=layer, shaded_bed=faint)["parts"][0]
    assert not weak["ok"] and weak["halo_ratio"] < 3.0 < got["halo_ratio"]


def test_effective_centre_reports_a_clamped_crop():
    """audit-v019 MEDIUM-2: a slot declared cx 0.70 on a 2160x3840 portrait master; the renderer
    clamped the window to the right edge (effective 0.615) and nothing said so."""
    from onetoone.framing import effective_centre
    e = effective_centre({"zoom": 1.3, "cx": 0.70, "cy": 0.58}, 2160, 3840)
    assert e["clamped"] and abs(e["cx"] - 0.6154) < 0.002 and e["upscale"] > 1.1
    assert not effective_centre({"zoom": 1.3, "cx": 0.5, "cy": 0.5}, 3840, 2160)["clamped"]
