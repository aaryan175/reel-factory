"""Tests for inkcheck.py — per-frame ink cleanliness on a composited file.

Pins:
  1. UNMEASURABLE is never PASS — an empty mask, an empty ring, or a mask that registers
     to no sampled frame all report UNMEASURABLE, never a green.
  2. Ink that separates from its own surround by >= 25 luma PASSes; ink at ~18 luma
     separation FAILs. That is the "muffled, not clean" class, and it is the floor the
     shipped engine's ink.py:519 silently skips.
  3. The WORST sampled frame governs — one bad frame inside a hold fails the state.
  4. The reference-relative ratio is reported when a reference measurement is supplied,
     and is None (never a silent PASS) when it is not.
  5. W3 is BOTH of its specified conditions: >= 25 luma separation AND
     Michelson >= 0.80x the reference's own ring Michelson. Ink that clears the absolute
     floor on a quarter of the reference's contrast FAILs; a reference that cannot be
     measured makes the state UNMEASURABLE, never PASS. A caller measuring the reference
     itself declares ``reference_required=False`` — the three absolute-floor probes below
     were updated to say so when this condition was added, because they had pinned the old
     single-condition behaviour.

Run:  python3 -m pytest caption-learning/tests -v
"""

import os
import shutil
import subprocess
import sys

import cv2
import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import inkcheck  # noqa: E402

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"

FRAMES = 24
SPAN = (8, 17)          # zero-based decoded order, half-open
BBOX = [90, 70, 250, 180]


def _glyph(hw=(240, 320)):
    """A stem plus a ringed counter, so the mask has an interior worth eroding."""
    g = np.zeros(hw, bool)
    g[90:150, 110:126] = True
    yy, xx = np.mgrid[0 : hw[0], 0 : hw[1]]
    r = np.hypot(yy - 120, xx - 190)
    g |= (r <= 42) & (r >= 26)
    return g


def _encode(path, frames):
    src = path + ".src"
    os.makedirs(src, exist_ok=True)
    for i, f in enumerate(frames):
        cv2.imwrite(os.path.join(src, f"{i:05d}.png"), f)
    r = subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-framerate", "24", "-start_number", "0",
         "-i", os.path.join(src, "%05d.png"),
         "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p", path],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    shutil.rmtree(src)
    return path


def _make(path, ink_luma, drift=0):
    """Moving footage with a static glyph composited over ``SPAN``.

    ``drift`` shifts the glyph by that many px per frame, which is how a real caption walks
    away from a single recovered mask.
    """
    rng = np.random.default_rng(7)
    base = rng.integers(24, 56, size=(240, 320), dtype=np.uint8)
    glyph = _glyph()
    out = []
    for i in range(FRAMES):
        bg = np.roll(base, i * 3, axis=1)
        frame = cv2.cvtColor(bg.astype(np.uint8), cv2.COLOR_GRAY2BGR)
        if SPAN[0] <= i < SPAN[1]:
            g = np.roll(glyph, drift * (i - SPAN[0]), axis=1) if drift else glyph
            frame[g] = (ink_luma, ink_luma, ink_luma)
        out.append(frame)
    _encode(path, out)
    return path, glyph


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    d = tmp_path_factory.mktemp("ink")
    return _make(str(d / "clean.mp4"), ink_luma=235)


@pytest.fixture(scope="module")
def muffled(tmp_path_factory):
    d = tmp_path_factory.mktemp("ink")
    # ~18 luma above the footage it sits on: below the 25-luma doctrine floor
    return _make(str(d / "muffled.mp4"), ink_luma=58)


@pytest.fixture(scope="module")
def drifting(tmp_path_factory):
    d = tmp_path_factory.mktemp("ink")
    return _make(str(d / "drift.mp4"), ink_luma=235, drift=12)


STATE = {
    "id": "SYNTH",
    "start_frame": SPAN[0],
    "end_frame_exclusive": SPAN[1],
    "placement": {"core_bbox_xyxy": BBOX},
    "text": "synthetic",
}


def _assert_gate_shape(d):
    assert d["verdict"] in ("PASS", "FAIL", "UNMEASURABLE"), d["verdict"]
    assert "sample_size" in d and isinstance(d["sample_size"], int)
    assert "evidence" in d and isinstance(d["evidence"], dict)


# --------------------------------------------------------------------------
# honesty vocabulary
# --------------------------------------------------------------------------
def test_gate_dict_shape_on_every_branch(clean, muffled, tmp_path):
    video, glyph = clean
    _assert_gate_shape(inkcheck.ink_state(video, STATE, glyph, out_dir=str(tmp_path)))
    _assert_gate_shape(inkcheck.ink_state(video, STATE, np.zeros_like(glyph)))
    _assert_gate_shape(inkcheck.ink_state(muffled[0], STATE, muffled[1]))


def test_empty_mask_is_unmeasurable_not_pass(clean):
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, np.zeros_like(glyph))
    assert r["verdict"] == "UNMEASURABLE"
    assert r["sample_size"] == 0
    assert r["worst_separation"] is None


def test_frames_outside_the_file_are_unmeasurable_not_pass(clean):
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph, frames=[900, 901])
    assert r["verdict"] == "UNMEASURABLE"
    assert r["sample_size"] == 0


def test_sample_size_counts_only_registered_frames(clean):
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph)
    assert r["sample_size"] == len([f for f in r["frames"] if f["status"] == "measured"])
    assert r["frames_sampled"] >= r["sample_size"]


# --------------------------------------------------------------------------
# the 25-luma floor
# --------------------------------------------------------------------------
def test_clean_ink_passes(clean):
    """Absolute-floor probe: declared reference_required=False, because a PASS with no
    reference at all would be half the W3 spec (see the reference-relative section)."""
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph, reference_required=False)
    assert r["verdict"] == "PASS", r
    assert r["worst_separation"] >= inkcheck.MIN_SEPARATION_LUMA
    assert r["sample_size"] >= 3          # entry / mid / exit at minimum


def test_muffled_ink_fails(muffled):
    video, glyph = muffled
    r = inkcheck.ink_state(video, STATE, glyph)
    assert r["verdict"] == "FAIL", r
    assert r["worst_separation"] < inkcheck.MIN_SEPARATION_LUMA
    assert "below the" in r["reason"]


def test_entry_mid_exit_are_sampled_by_default(clean):
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph)
    sampled = [f["frame"] for f in r["frames"]]
    assert SPAN[0] in sampled
    assert SPAN[1] - 1 in sampled
    assert len(sampled) >= 3


def test_worst_frame_governs(clean, tmp_path):
    """One muffled frame inside an otherwise clean hold fails the state."""
    video, glyph = clean
    frames = []
    cap = cv2.VideoCapture(video)
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    frames[12][glyph] = (46, 46, 46)      # one frame's ink sinks into the footage
    bad = _encode(str(tmp_path / "onebad.mp4"), frames)
    r = inkcheck.ink_state(bad, STATE, glyph, frames=[8, 12, 16])
    assert r["verdict"] == "FAIL"
    assert r["worst_frame"] == 12


def test_polarity_agnostic(clean, tmp_path):
    """Dark ink on bright footage must measure the same way as bright on dark."""
    glyph = _glyph()
    rng = np.random.default_rng(3)
    base = rng.integers(200, 236, size=(240, 320), dtype=np.uint8)
    out = []
    for i in range(FRAMES):
        frame = cv2.cvtColor(np.roll(base, i * 3, axis=1).astype(np.uint8), cv2.COLOR_GRAY2BGR)
        if SPAN[0] <= i < SPAN[1]:
            frame[glyph] = (20, 20, 20)
        out.append(frame)
    dark = _encode(str(tmp_path / "dark.mp4"), out)
    r = inkcheck.ink_state(dark, STATE, glyph, reference_required=False)
    assert r["verdict"] == "PASS"
    assert all(f["polarity"] == "ink_darker" for f in r["frames"])


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------
def test_mask_frames_measures_the_frames_the_mask_belongs_to(drifting):
    """The correct API for an animating caption: name the frames the mask came from."""
    video, glyph = drifting
    r = inkcheck.ink_state(video, STATE, glyph, mask_frames=[SPAN[0]],
                           reference_required=False)
    assert r["verdict"] == "PASS"
    assert [f["frame"] for f in r["frames"]] == [SPAN[0]]
    assert r["sample_size"] == 1               # honestly one frame, not three


def test_drift_beyond_the_registration_window_is_visible_and_never_a_silent_pass(drifting):
    """Documented limit, pinned.

    A caption that walks further than ``registration_px`` from its mask reads as low
    separation. The module does not silently green-light it, and the per-frame registration
    offsets make the drift visible instead of hiding it.
    """
    video, glyph = drifting
    r = inkcheck.ink_state(video, STATE, glyph)
    assert r["verdict"] != "PASS"
    offsets = [f["registration_dy_dx"] for f in r["frames"]]
    assert any(o != [0, 0] for o in offsets), offsets


def test_reference_style_animation_reports_unregistered_frames(drifting):
    """With a wide enough drift the optimum leaves the window and the frame is excluded."""
    video, glyph = drifting
    r = inkcheck.ink_state(video, STATE, glyph, registration_px=2)
    statuses = [f["status"] for f in r["frames"]]
    assert "unregistered" in statuses, statuses
    assert r["sample_size"] == statuses.count("measured")


def test_registration_cannot_manufacture_contrast(muffled):
    """Muffled ink stays muffled at every offset in the search window."""
    video, glyph = muffled
    wide = inkcheck.ink_state(video, STATE, glyph, registration_px=8)
    strict = inkcheck.ink_state(video, STATE, glyph, registration_px=0)
    assert wide["verdict"] == strict["verdict"] == "FAIL"
    assert wide["worst_separation"] < inkcheck.MIN_SEPARATION_LUMA
    assert strict["worst_separation"] < inkcheck.MIN_SEPARATION_LUMA


# --------------------------------------------------------------------------
# reference-relative reporting
# --------------------------------------------------------------------------
def test_reference_ratio_is_none_when_no_reference_is_given(clean):
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph)
    assert r["michelson_ratio_vs_reference"] is None
    assert r["separation_ratio_vs_reference"] is None


def test_reference_ratio_is_reported_when_given(clean):
    video, glyph = clean
    base = inkcheck.ink_state(video, STATE, glyph)
    r = inkcheck.ink_state(
        video, STATE, glyph,
        reference_michelson=base["worst_michelson"],
        reference_separation=base["worst_separation"],
    )
    assert r["michelson_ratio_vs_reference"] == pytest.approx(1.0, rel=0.01)
    assert r["separation_ratio_vs_reference"] == pytest.approx(1.0, rel=0.01)


def test_an_unmeasurable_reference_never_becomes_a_silent_pass(muffled):
    """ink.py:519 no-ops to PASS when the reference Michelson is at the floor. This does not:
    the absolute 25-luma floor still runs and still fails."""
    video, glyph = muffled
    r = inkcheck.ink_state(video, STATE, glyph, reference_michelson=0.0, reference_separation=0.0)
    assert r["verdict"] == "FAIL"
    assert r["michelson_ratio_vs_reference"] is None


# --------------------------------------------------------------------------
# the reference-relative floor (W3: BOTH conditions)
# --------------------------------------------------------------------------
def _ratio_call(video, glyph, ratio, **kw):
    """Measure once to learn this state's own Michelson, then re-measure declaring a
    reference whose Michelson is exactly ``worst_michelson / ratio`` — i.e. ask for a
    known reference-relative ratio while the pixels stay identical."""
    base = inkcheck.ink_state(video, STATE, glyph, reference_required=False)
    return base, inkcheck.ink_state(
        video, STATE, glyph,
        reference_michelson=base["worst_michelson"] / ratio,
        reference_separation=base["worst_separation"],
        **kw,
    )


def test_michelson_below_080_of_reference_fails_even_with_ample_separation(clean):
    """W3 = Michelson >= 0.80x the reference's own ring Michelson AND >= 25 luma separation.
    Ink that clears the absolute floor but carries a quarter of the reference's contrast is
    the 'muffled, not clean' class — it must not ship green."""
    video, glyph = clean
    base, r = _ratio_call(video, glyph, 0.2875)
    assert base["worst_separation"] >= inkcheck.MIN_SEPARATION_LUMA
    assert r["worst_separation"] >= inkcheck.MIN_SEPARATION_LUMA   # absolute floor cleared
    assert r["michelson_ratio_vs_reference"] == pytest.approx(0.2875, rel=0.02)
    assert r["verdict"] == "FAIL", r
    assert "0.8" in r["reason"] and "reference" in r["reason"]


def test_michelson_at_the_floor_passes(clean):
    video, glyph = clean
    _, r = _ratio_call(video, glyph, 0.85)
    assert r["verdict"] == "PASS", r


def test_absolute_floor_still_fails_first_even_when_the_ratio_is_fine(muffled):
    """A state can be relatively strong and absolutely unreadable; the 25-luma floor is
    not weakened by adding the second condition."""
    video, glyph = muffled
    r = inkcheck.ink_state(video, STATE, glyph,
                           reference_michelson=0.01, reference_separation=1.0)
    assert r["verdict"] == "FAIL"
    assert "below the" in r["reason"]


def test_unmeasurable_reference_michelson_is_unmeasurable_never_pass(clean):
    """§3G: an unmeasurable reference is an explicit UNMEASURABLE_REFERENCE, not a no-op
    green. Clean ink with a reference we could not measure cannot be called reference-clean."""
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph,
                           reference_michelson=0.0, reference_separation=0.0)
    assert r["verdict"] == "UNMEASURABLE", r
    assert r["reference_relative"] == "UNMEASURABLE_REFERENCE"
    assert r["michelson_ratio_vs_reference"] is None


def test_missing_reference_is_never_a_silent_absolute_only_pass(clean):
    """The default is reference-relative. A caller that supplies no reference gets
    UNMEASURABLE, not a PASS that silently enforced half the spec."""
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph)
    assert r["verdict"] == "UNMEASURABLE", r
    assert r["reference_relative"] == "UNMEASURABLE_REFERENCE"


def test_absolute_only_measurement_must_be_declared(clean):
    """Measuring the REFERENCE itself has nothing to be relative to; that caller declares
    it, and the result says so rather than pretending both conditions ran."""
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph, reference_required=False)
    assert r["verdict"] == "PASS"
    assert r["reference_relative"] == "NOT_APPLICABLE"


def test_ratio_floor_is_reported_in_thresholds(clean):
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph, reference_required=False)
    assert r["thresholds"]["min_michelson_ratio_vs_reference"] == inkcheck.MIN_MICHELSON_RATIO
    assert inkcheck.MIN_MICHELSON_RATIO == 0.80


# --------------------------------------------------------------------------
# ring construction
# --------------------------------------------------------------------------
def test_ring_is_the_annulus_between_the_two_dilations(clean):
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph, inner_dilate=7, outer_dilate=17)
    assert r["ring_px"] > r["interior_px"] * 0.5
    assert r["thresholds"]["inner_dilate_px"] == 7
    assert r["thresholds"]["outer_dilate_px"] == 17


def test_evidence_band_image_is_written(clean, tmp_path):
    video, glyph = clean
    r = inkcheck.ink_state(video, STATE, glyph, out_dir=str(tmp_path), label="bands")
    assert os.path.exists(r["evidence"]["bands_png"])
