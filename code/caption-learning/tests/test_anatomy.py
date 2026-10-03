"""Tests for anatomy.py — the DOCTRINE 16.2 triple gate.

Pins, in order of importance:
  1. UNMEASURABLE is never PASS when there were no pixels to compare (the dead
     "compared zero pixels on 40/42 states and passed" class).
  2. An amputated stroke FAILS via residual + topology **while Dice is still above
     0.90** — the amputated-`they` false pass (Dice 0.9030, residual 85px) cannot recur.
  3. An anisotropically stretched mask FAILS — the normalisation must never apply an
     independent x/y resize, which is the historical false-pass mechanism.
  4. A uniformly scaled / translated copy PASSes — those are nuisance parameters, not
     anatomy, and the gate must not fire on them.

Run:  python3 -m pytest caption-learning/tests -v
"""

import os
import sys

import cv2
import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import anatomy  # noqa: E402


# --------------------------------------------------------------------------
# synthetic letterforms
# --------------------------------------------------------------------------
# A "word": two ringed counters flanking a central stem. Deliberately symmetric about its own
# centroid so the amputation fixture can remove mass WITHOUT moving the centroid — otherwise
# the test would be measuring alignment rather than anatomy.
SYNTH_HW = (520, 760)


def synthetic_word():
    mask = np.zeros(SYNTH_HW, dtype=bool)
    mask[100:400, 370:388] = True                      # the stem
    yy, xx = np.mgrid[0 : SYNTH_HW[0], 0 : SYNTH_HW[1]]
    for cx in (180, 580):                              # two counters
        r = np.hypot(yy - 250, xx - cx)
        mask |= (r <= 90) & (r >= 62)
    return mask


def amputated_word():
    """Sever the stem and open both counters — centroid-neutral by construction."""
    mask = synthetic_word()
    mask[200:300, 365:393] = False   # sever the stem -> one component becomes two
    mask[235:265, 85:125] = False    # open the left counter  -> one hole gone
    mask[235:265, 635:675] = False   # open the right counter -> the other hole gone
    return mask


@pytest.fixture(scope="module")
def src():
    return synthetic_word()


def _assert_gate_shape(d):
    assert d["verdict"] in ("PASS", "FAIL", "UNMEASURABLE"), d["verdict"]
    assert "sample_size" in d and isinstance(d["sample_size"], int)
    assert "evidence" in d and isinstance(d["evidence"], dict)


# --------------------------------------------------------------------------
# gate-dict contract + honesty vocabulary
# --------------------------------------------------------------------------
def test_gate_dict_shape_on_every_branch(src, tmp_path):
    for candidate in (src.copy(), amputated_word(), np.zeros_like(src)):
        _assert_gate_shape(anatomy.compare_masks(src, candidate, out_dir=str(tmp_path)))


def test_empty_candidate_is_unmeasurable_not_pass(src):
    r = anatomy.compare_masks(src, np.zeros_like(src))
    assert r["verdict"] == "UNMEASURABLE"
    assert r["dice"] is None
    assert r["sample_size"] == 0
    assert "nothing to compare" in r["reason"]


def test_empty_source_is_unmeasurable_not_pass(src):
    r = anatomy.compare_masks(np.zeros_like(src), src)
    assert r["verdict"] == "UNMEASURABLE"
    assert r["sample_size"] == 0


def test_both_empty_is_unmeasurable_not_pass():
    z = np.zeros((64, 64), bool)
    r = anatomy.compare_masks(z, z)
    assert r["verdict"] == "UNMEASURABLE"
    assert r["verdict"] != "PASS"


def test_tiny_speck_is_unmeasurable_not_pass():
    """Two identical 5x5 specks have Dice 1.0 — and are still not a measurement."""
    a = np.zeros((64, 64), bool)
    a[10:15, 10:15] = True
    r = anatomy.compare_masks(a, a.copy())
    assert r["verdict"] == "UNMEASURABLE"
    assert r["sample_size"] < anatomy.MIN_BOUNDARY_PIXELS or r["dice"] is None


def test_sample_size_is_the_source_boundary_population(src):
    r = anatomy.compare_masks(src, src.copy())
    assert r["sample_size"] > 1000
    assert r["boundary_px"]["source"] == r["sample_size"]


# --------------------------------------------------------------------------
# 1. identical masks PASS
# --------------------------------------------------------------------------
def test_identical_masks_pass(src):
    r = anatomy.compare_masks(src, src.copy())
    assert r["verdict"] == "PASS"
    assert r["dice"] == 1.0
    assert r["residual_p95_px"] == 0.0
    assert r["components_source"] == r["components_candidate"]
    assert r["holes_source"] == r["holes_candidate"]
    assert r["failed_axes"] == []


def test_pure_translation_passes(src):
    """Translation is a nuisance parameter — tight-crop + centroid absorbs it exactly."""
    shifted = np.roll(np.roll(src, 7, axis=0), -11, axis=1)
    r = anatomy.compare_masks(src, shifted)
    assert r["verdict"] == "PASS"
    assert r["dice"] == 1.0


def test_uniform_scale_passes(src):
    """ONE isotropic scale is the sanctioned normalisation, so a 1.6x copy must pass."""
    big = cv2.resize(src.astype(np.uint8) * 255, None, fx=1.6, fy=1.6,
                     interpolation=cv2.INTER_NEAREST) > 0
    r = anatomy.compare_masks(src, big)
    assert r["verdict"] == "PASS", r
    assert r["dice"] > 0.99
    assert r["normalisation"]["isotropic_scale"] == pytest.approx(1 / 1.6, rel=0.02)


# --------------------------------------------------------------------------
# 2. amputated stroke FAILS via residual + holes even though Dice > 0.90
# --------------------------------------------------------------------------
def test_amputated_stroke_fails_via_residual_and_topology_despite_high_dice(src, tmp_path):
    amp = amputated_word()
    r = anatomy.compare_masks(src, amp, out_dir=str(tmp_path), label="amputated")

    # Dice alone would have PASSED this candidate — that is the whole point of the fixture.
    assert r["dice"] > anatomy.DICE_MIN, f"fixture invalid: dice {r['dice']} <= {anatomy.DICE_MIN}"
    assert "dice" not in r["failed_axes"]

    assert r["verdict"] == "FAIL"
    assert r["residual_p95_px"] > anatomy.RESIDUAL_P95_MAX
    assert "residual_p95" in r["failed_axes"]
    assert r["components_candidate"] == r["components_source"] + 1  # the stem was severed
    assert "components" in r["failed_axes"]
    assert r["holes_source"] == 2 and r["holes_candidate"] == 0     # both counters opened
    assert "holes" in r["failed_axes"]


def test_amputation_is_centroid_neutral(src):
    """Guards the fixture: if the erasure moved the centroid the test would be about
    alignment, not anatomy."""
    amp = amputated_word()
    sy, sx = np.nonzero(src)
    ay, ax = np.nonzero(amp)
    assert abs(sy.mean() - ay.mean()) < 1.0
    assert abs(sx.mean() - ax.mean()) < 1.0


def test_single_severed_stroke_fails_on_residual_alone():
    """A long bar with one segment removed: the amputated-stroke geometry in its simplest form."""
    a = np.zeros((520, 200), bool)
    a[60:460, 90:110] = True
    b = a.copy()
    b[240:280, :] = False
    r = anatomy.compare_masks(a, b)
    assert r["dice"] > anatomy.DICE_MIN, r["dice"]
    assert r["residual_p95_px"] > anatomy.RESIDUAL_P95_MAX
    assert r["verdict"] == "FAIL"


# --------------------------------------------------------------------------
# 3. anisotropic stretch FAILS
# --------------------------------------------------------------------------
@pytest.mark.parametrize("fx", [1.15, 1.25, 1.40])
def test_anisotropic_x_stretch_fails(src, fx):
    stretched = cv2.resize(src.astype(np.uint8) * 255, None, fx=fx, fy=1.0,
                           interpolation=cv2.INTER_NEAREST) > 0
    r = anatomy.compare_masks(src, stretched)
    assert r["verdict"] == "FAIL"
    assert "dice" in r["failed_axes"]
    assert "residual_p95" in r["failed_axes"]


@pytest.mark.parametrize("fy", [1.15, 1.30])
def test_anisotropic_y_stretch_fails(src, fy):
    stretched = cv2.resize(src.astype(np.uint8) * 255, None, fx=1.0, fy=fy,
                           interpolation=cv2.INTER_NEAREST) > 0
    r = anatomy.compare_masks(src, stretched)
    assert r["verdict"] == "FAIL"


def test_normalisation_never_resizes_the_axes_independently(src):
    """The scale recorded is a single number, applied to both axes."""
    stretched = cv2.resize(src.astype(np.uint8) * 255, None, fx=1.25, fy=1.0,
                           interpolation=cv2.INTER_NEAREST) > 0
    r = anatomy.compare_masks(src, stretched)
    scale = r["normalisation"]["isotropic_scale"]
    assert isinstance(scale, float)
    assert "single isotropic scale" in r["normalisation"]["method"]
    # a y-only stretch changes the height, so the single scale corrects height and the
    # width mismatch survives — which is exactly why the gate can see it
    assert r["dice"] < anatomy.DICE_MIN


# --------------------------------------------------------------------------
# bounded refinement is a nuisance remover, not a repair
# --------------------------------------------------------------------------
def test_refinement_cannot_repair_an_amputation(src):
    amp = amputated_word()
    with_refine = anatomy.compare_masks(src, amp, refine_px=6)
    without = anatomy.compare_masks(src, amp, refine_px=0)
    assert with_refine["verdict"] == without["verdict"] == "FAIL"
    assert with_refine["residual_p95_px"] > anatomy.RESIDUAL_P95_MAX
    assert without["residual_p95_px"] > anatomy.RESIDUAL_P95_MAX


def test_refinement_is_reported(src):
    shifted = np.roll(src, 3, axis=1)
    r = anatomy.compare_masks(src, shifted)
    n = r["normalisation"]
    assert "refine_dy_dx" in n and "dice_centroid_only" in n and "refinement_gain_dice" in n


# --------------------------------------------------------------------------
# topology floors are reported, not hidden
# --------------------------------------------------------------------------
def test_topology_floors_and_raw_counts_are_reported(src):
    r = anatomy.compare_masks(src, src.copy())
    raw = r["topology_raw"]
    for key in ("components_source_unfloored", "holes_source_unfloored",
                "component_area_floor_px", "hole_area_floor_px"):
        assert key in raw


def test_speck_below_the_component_floor_does_not_change_the_count(src):
    """A 3x3 recovery speck is noise, not a letterform component.

    Placed INSIDE the word's own bounding box: a speck outside it would move the tight crop
    and therefore the isotropic scale, which is a different (real) sensitivity — see
    ``test_outlier_speck_outside_the_bbox_moves_the_crop``.
    """
    speckled = src.copy()
    speckled[300:303, 300:303] = True
    r = anatomy.compare_masks(src, speckled)
    assert r["components_source"] == r["components_candidate"]
    assert r["topology_raw"]["components_candidate_unfloored"] == \
        r["topology_raw"]["components_source_unfloored"] + 1


def test_outlier_speck_outside_the_bbox_moves_the_crop(src):
    """Documented sensitivity, pinned so it cannot change silently.

    Tight-crop normalisation is set by the mask's extremes, so a stray component OUTSIDE the
    letterform's own box rescales the whole comparison. That is why
    ``recover_state_mask`` drops components touching the padded-ROI border before a mask ever
    reaches this gate — footage structures entering the box are removed at recovery time, not
    tolerated here.
    """
    speckled = src.copy()
    speckled[500:503, 700:703] = True
    r = anatomy.compare_masks(src, speckled)
    assert r["normalisation"]["isotropic_scale"] < 0.9   # the crop grew, the scale shrank
    assert r["verdict"] == "FAIL"


# --------------------------------------------------------------------------
# I/O tolerance
# --------------------------------------------------------------------------
def test_accepts_png_paths_and_rgba_plates(src, tmp_path):
    png = str(tmp_path / "src.png")
    cv2.imwrite(png, src.astype(np.uint8) * 255)
    rgba = np.zeros(src.shape + (4,), np.uint8)
    rgba[..., 3] = src.astype(np.uint8) * 255
    assert anatomy.compare_masks(png, rgba)["verdict"] == "PASS"


def test_evidence_files_are_written(src, tmp_path):
    r = anatomy.compare_masks(src, amputated_word(), out_dir=str(tmp_path), label="ev")
    for key in ("source_png", "candidate_png", "overlay_png"):
        assert os.path.exists(r["evidence"][key])
