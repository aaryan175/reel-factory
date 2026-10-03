#!/usr/bin/env python3
"""Tests for onetoone.grade — the per-shot grade match.

What these pin down, in the order the reviewer cares about:
  * the curve can never invert, crush or reverse tone (monotonic, by construction);
  * a reference DARKER than our footage can never come back as a brightening;
  * what this module computes in numpy is what ffmpeg actually renders, and the filter
    string it emits parses (one real 1-frame ffmpeg run, through the machine-wide lock);
  * the cast stays on a plausible illuminant axis and preserves luma;
  * an unusable measurement falls back to the proven mood_eq rather than guessing.

ffmpeg is only ever invoked through `onetoone.ffx`, never directly.
"""
from __future__ import annotations

import re

import numpy as np
import pytest
from PIL import Image

from onetoone import grade as G

# --------------------------------------------------------------------- fixtures --


def _frame(path, *, base=0.45, spread=0.35, tint=(1.0, 1.0, 1.0), size=(640, 360)):
    """A synthetic frame with a smooth luma ramp, a colour wash and a skin-tone patch."""
    w, h = size
    x = np.linspace(0.0, 1.0, w, dtype=np.float64)[None, :]
    y = np.linspace(0.0, 1.0, h, dtype=np.float64)[:, None]
    ramp = np.clip(base + spread * (x - 0.5) + 0.06 * (y - 0.5), 0.0, 1.0)
    a = np.repeat(ramp[..., None], 3, axis=2) * np.asarray(tint, dtype=np.float64)
    a[h // 3:2 * h // 3, w // 3:2 * w // 3] = np.asarray([0.62, 0.45, 0.35])   # skin patch
    img = np.clip(a, 0.0, 1.0)
    Image.fromarray(np.rint(img * 255).astype(np.uint8)).save(path)
    return path


def _stats(tmp_path, name, **kw):
    p = _frame(tmp_path / f"{name}.png", **kw)
    return G.shot_stats([p], native_probe=p)


@pytest.fixture
def bright_cand(tmp_path):
    return _stats(tmp_path, "cand", base=0.60, spread=0.34, tint=(1.0, 1.0, 1.02))


@pytest.fixture
def dark_ref(tmp_path):
    return _stats(tmp_path, "dark_ref", base=0.18, spread=0.22, tint=(1.05, 1.0, 0.9))


@pytest.fixture
def warm_ref(tmp_path):
    return _stats(tmp_path, "warm_ref", base=0.55, spread=0.34, tint=(1.22, 0.99, 0.66))


@pytest.fixture
def cool_ref(tmp_path):
    return _stats(tmp_path, "cool_ref", base=0.40, spread=0.30, tint=(0.80, 1.0, 1.25))


# ------------------------------------------------------------- monotonic curves --

ALL_STRENGTHS = (0.0, 0.25, 0.4, 0.55, 0.7, 0.85, 1.0)


@pytest.mark.parametrize("ref_name", ["dark_ref", "warm_ref", "cool_ref"])
@pytest.mark.parametrize("strength", ALL_STRENGTHS)
def test_control_points_are_monotonic(request, bright_cand, ref_name, strength):
    ref = request.getfixturevalue(ref_name)
    pts = G.tone_points(bright_cand, ref, strength)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    assert xs == sorted(xs) and len(set(xs)) == len(xs), "x must be strictly increasing"
    assert all(b >= a for a, b in zip(ys, ys[1:])), "y must never decrease"
    assert 0.0 <= min(ys) and max(ys) <= 1.0
    assert pts[0] == (0.0, 0.0) and pts[-1][0] == 1.0


@pytest.mark.parametrize("ref_name", ["dark_ref", "warm_ref", "cool_ref"])
@pytest.mark.parametrize("strength", ALL_STRENGTHS)
def test_rendered_curve_is_monotonic(request, bright_cand, ref_name, strength):
    """Not just the control points: the interpolated curve itself never turns back."""
    ref = request.getfixturevalue(ref_name)
    pts = G.tone_points(bright_cand, ref, strength)
    y = G.pchip(pts, np.linspace(0.0, 1.0, 2048))
    assert float(np.min(np.diff(y))) >= -1e-9, "pchip output must be non-decreasing"


def test_curve_never_exceeds_the_segment_slope_cap(bright_cand, dark_ref):
    """The cap that stops a stretch from striping a gradient."""
    pts = G.tone_points(bright_cand, dark_ref, 1.0)
    secants = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(pts, pts[1:])]
    assert max(secants) <= G.MAX_SEG_SLOPE + 1e-6


def test_zero_strength_is_the_identity_curve(bright_cand, dark_ref):
    pts = G.tone_points(bright_cand, dark_ref, 0.0)
    x = np.linspace(0.0, 1.0, 512)
    assert float(np.max(np.abs(G.pchip(pts, x) - x))) < 0.01


# --------------------------------------------- a dark reference never brightens --

@pytest.mark.parametrize("strength", ALL_STRENGTHS)
def test_dark_reference_never_lifts_a_control_point(bright_cand, dark_ref, strength):
    for x, y in G.tone_points(bright_cand, dark_ref, strength):
        assert y <= x + 1e-9, f"point {x:.4f} was lifted to {y:.4f} against a darker reference"


def test_dark_reference_darkens_the_rendered_frame(tmp_path, bright_cand, dark_ref):
    src = G._load(bright_cand["probe"])
    sol = G.solve(bright_cand, dark_ref)
    assert sol["mode"] == "curves"
    pts = sol["points"] if sol["strength"] > 0 else [(0.0, 0.0), (1.0, 1.0)]
    out = G.simulate(src.astype(np.float64), pts, sol["gains"], sol["sat"])
    assert G._analyse(out.astype(np.float32))["lum_mean"] <= G._analyse(src)["lum_mean"] + 1e-6


def test_mood_eq_fallback_never_brightens_against_a_dark_reference():
    """The documented fallback path carries the same promise the old grade did."""
    flat = {"lum_mean": 0.50, "sat_mean": 0.30}
    dark = {"lum_mean": 0.10, "sat_mean": 0.30}
    s = G.mood_eq((flat["lum_mean"] * 255, flat["sat_mean"] * 255, 0.0),
                  (dark["lum_mean"] * 255, dark["sat_mean"] * 255, 0.0))
    brightness = float(re.search(r"brightness=(-?[\d.]+)", s).group(1))
    assert brightness <= 0.0


def test_unreliable_measurement_falls_back_to_mood_eq(tmp_path):
    """A flat frame carries no tone to match; keep the proven behaviour, do not guess."""
    flat = _stats(tmp_path, "flat", base=0.5, spread=0.0)
    ref = _stats(tmp_path, "ref_for_flat", base=0.2, spread=0.3)
    ok, why = G.reliable(flat, ref)
    assert not ok and "flat" in why
    out = G.shot_filter(flat, ref)
    assert out.startswith("eq=") and "curves" not in out
    assert float(re.search(r"brightness=(-?[\d.]+)", out).group(1)) <= 0.0


# ------------------------------------------------------------------- cast rules --

@pytest.mark.parametrize("ref_name", ["dark_ref", "warm_ref", "cool_ref"])
@pytest.mark.parametrize("strength", ALL_STRENGTHS)
def test_cast_stays_on_a_plausible_illuminant_axis(request, bright_cand, ref_name, strength):
    ref = request.getfixturevalue(ref_name)
    gr, gg, gb = G.cast_gains(bright_cand, ref, strength)
    lr, lg, lb = (np.log(v) for v in (gr, gg, gb))
    tint = lg - (lr + lb) / 2.0
    assert abs(tint) <= G.CAST_TINT_MAX + 1e-4, "green-magenta tint must stay bounded"


@pytest.mark.parametrize("ref_name", ["dark_ref", "warm_ref", "cool_ref"])
def test_cast_preserves_luma(request, bright_cand, ref_name):
    ref = request.getfixturevalue(ref_name)
    g = G.cast_gains(bright_cand, ref, 1.0)
    assert abs(sum(G.LUMA[i] * g[i] for i in range(3)) - 1.0) < 1e-3


def test_warm_reference_moves_the_cast_warm(bright_cand, warm_ref, cool_ref):
    gw = G.cast_gains(bright_cand, warm_ref, 1.0)
    gc = G.cast_gains(bright_cand, cool_ref, 1.0)
    assert gw[0] / gw[2] > 1.0, "a warm reference must raise red against blue"
    assert gc[0] / gc[2] < 1.0, "a cool reference must raise blue against red"


def test_saturation_multiplier_stays_inside_its_caps(bright_cand, warm_ref):
    for s in ALL_STRENGTHS:
        f = G.saturation_factor(bright_cand, warm_ref, s, after_curve=0.05)
        assert G.SAT_MIN - 1e-9 <= f <= G.SAT_MAX + 1e-9


# ---------------------------------------------------------- ffmpeg, for real --

def _run_filter(src, dst, vf):
    from onetoone.ffx import run
    return run(["-y", "-loglevel", "error", "-i", str(src), "-vf", vf + ",format=rgb24",
                "-frames:v", "1", str(dst)], capture=True)


def test_emitted_filter_parses_and_renders_in_ffmpeg(tmp_path, bright_cand, warm_ref):
    """One real 1-frame ffmpeg run, through the machine-wide one-ffmpeg lock."""
    vf = G.shot_filter(bright_cand, warm_ref)
    assert "curves=interp=pchip" in vf and "colorchannelmixer=" in vf
    src = _frame(tmp_path / "ff_in.png", base=0.5, spread=0.4)
    dst = tmp_path / "ff_out.png"
    cp = _run_filter(src, dst, vf)
    assert cp.returncode == 0, cp.stderr
    assert dst.exists() and Image.open(dst).size == (640, 360)


def test_ffmpeg_render_matches_the_simulation(tmp_path, bright_cand, warm_ref):
    """If these drift, every guard in the module is judging a picture nobody will see."""
    sol = G.solve(bright_cand, warm_ref)
    assert sol["mode"] == "curves"
    src = _frame(tmp_path / "sim_in.png", base=0.5, spread=0.4)
    dst = tmp_path / "sim_out.png"
    assert _run_filter(src, dst, G.filter_from_solution(sol, bright_cand, warm_ref)).returncode == 0
    pts = sol["points"] if sol["strength"] > 0 else [(0.0, 0.0), (1.0, 1.0)]
    sim = G.simulate(G._load(src).astype(np.float64), pts, sol["gains"], sol["sat"])
    got = np.asarray(Image.open(dst).convert("RGB"), dtype=np.float64) / 255.0
    diff = np.abs(sim - got) * 255.0
    assert float(diff.mean()) < 2.0, f"mean disagreement {diff.mean():.2f} codes"
    assert float(diff.max()) < 12.0, f"worst disagreement {diff.max():.1f} codes"


def test_ffmpeg_curve_is_monotonic_on_a_ramp(tmp_path, bright_cand, dark_ref):
    """ffmpeg's own pchip, on every 8-bit input code, must never step backwards."""
    pts = G.tone_points(bright_cand, dark_ref, 1.0)
    ramp = np.tile(np.arange(256, dtype=np.uint8), (8, 1))
    src = tmp_path / "ramp.png"
    Image.fromarray(np.dstack([ramp] * 3)).save(src)
    dst = tmp_path / "ramp_out.png"
    vf = f"format=gbrp10le,curves=interp=pchip:all='{G._fmt(pts)}'"
    assert _run_filter(src, dst, vf).returncode == 0
    out = np.asarray(Image.open(dst).convert("RGB"))[0, :, 0].astype(int)
    assert np.all(np.diff(out) >= 0), "ffmpeg's rendered curve inverted somewhere"
    assert out[0] <= 1 and out[-1] >= 250


# ------------------------------------------------------------------ guard wiring --

def test_solver_records_why_it_backed_off(bright_cand, dark_ref):
    sol = G.solve(bright_cand, dark_ref)
    assert sol["mode"] == "curves"
    assert 0.0 <= sol["strength"] <= 1.0 and 0.0 < sol["cast_strength"] <= 1.0
    assert sol["tried"], "every attempt must be recorded for the report"
    assert all(t["passed"] or t["fails"] for t in sol["tried"])


def test_banding_delta_flags_a_violent_stretch(tmp_path):
    """The banding guard must score a crushing S-curve worse than leaving the frame alone."""
    src = _frame(tmp_path / "band_src.png", base=0.5, spread=0.45)
    flat = [(0.0, 0.0), (1.0, 1.0)]
    stretch = [(0.0, 0.0), (0.40, 0.05), (0.60, 0.95), (1.0, 1.0)]
    mild = G.banding_delta(src, flat, (1.0, 1.0, 1.0), 1.0)
    harsh = G.banding_delta(src, stretch, (1.0, 1.0, 1.0), 1.0)
    assert harsh["stepped_after"] > mild["stepped_after"]


def test_banding_report_sees_a_posterised_gradient(tmp_path):
    w, h = 480, 240
    ramp = np.linspace(40, 200, w)[None, :].repeat(h, axis=0)
    clean = tmp_path / "clean.png"
    steps = tmp_path / "steps.png"
    Image.fromarray(np.dstack([np.rint(ramp).astype(np.uint8)] * 3)).save(clean)
    posterised = np.rint(np.rint(ramp / 12.0) * 12.0).astype(np.uint8)
    Image.fromarray(np.dstack([posterised] * 3)).save(steps)
    assert G.banding_report(steps)["worst_run"] > G.banding_report(clean)["worst_run"]
