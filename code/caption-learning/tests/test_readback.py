"""Tests for readback.py.

Pins, in order of importance:
  1. UNMEASURABLE is never PASS when zero pixels / zero cells were measured
     (the dead "compared zero pixels on 40/42 states and passed" class).
  2. Garbage vs a declared word scores ~0 and FAILs.
  3. An exact PIL render of the declared word scores high and PASSes.
  4. read_region returns UNRESOLVED_TEXT rather than guessing.
  5. Optional local-corpus calibration: a damaged delivered word FAILs, the
     reference self-comparison PASSes.
"""

import os

import pytest
from PIL import Image

from readback_fixtures import (REFA_DELIVERED, REFA_PLATES, REFA_REFERENCE, WORD_BOX,
                               corpus_expect, make_video, render_blank, render_noise,
                               render_word,
                               require)


@pytest.fixture(scope="session")
def readback():
    import readback as m
    return m


@pytest.fixture
def work(tmp_path):
    return str(tmp_path)


# --------------------------------------------------------------------------
# text normalisation
# --------------------------------------------------------------------------
def test_normalize_strips_case_punctuation_and_whitespace(readback):
    n = readback.normalize_text
    assert n("Garden") == "garden"
    assert n("  GARDEN!  ") == "garden"
    assert n("that's") == "thats"
    assert n("got a") == "gota"
    assert n("here is the sample line:") == "hereisthesampleline"
    assert n('_Garden"') == n("Garden")
    assert n(None) == ""
    assert n("") == ""


# --------------------------------------------------------------------------
# gate-dict contract
# --------------------------------------------------------------------------
def _assert_gate_shape(d):
    assert d["verdict"] in ("PASS", "FAIL", "UNMEASURABLE"), d["verdict"]
    assert "sample_size" in d and isinstance(d["sample_size"], int)
    assert "evidence" in d and isinstance(d["evidence"], dict)


def test_gate_dict_shape_on_every_branch(readback, work):
    good = render_word(os.path.join(work, "good.png"), "GARDEN")
    _assert_gate_shape(readback.readback_image(good, "GARDEN", out_dir=work + "/a"))
    _assert_gate_shape(readback.readback_image(good, "", out_dir=work + "/b"))
    _assert_gate_shape(readback.readback_image(work + "/nope.png", "GARDEN",
                                               out_dir=work + "/c"))
    blank = render_blank(os.path.join(work, "blank.png"))
    _assert_gate_shape(readback.readback_image(blank, "GARDEN", out_dir=work + "/d"))


# --------------------------------------------------------------------------
# 1. UNMEASURABLE, never PASS
# --------------------------------------------------------------------------
def test_zero_ink_plate_is_unmeasurable_not_pass(readback, work):
    blank = render_blank(os.path.join(work, "blank.png"))
    d = readback.readback_image(blank, "GARDEN", out_dir=work + "/cells")
    assert d["verdict"] == "UNMEASURABLE"
    assert d["verdict"] != "PASS"
    assert d["sample_size"] == 0
    assert d["reason"] == "no_ink_pixels"
    assert d["agreement"] is None


def test_missing_source_is_unmeasurable(readback, work):
    d = readback.readback_image(os.path.join(work, "absent.png"), "GARDEN",
                                out_dir=work + "/cells")
    assert d["verdict"] == "UNMEASURABLE"
    assert d["reason"] == "source_png_missing"
    assert d["sample_size"] == 0


def test_empty_declared_text_is_unmeasurable(readback, work):
    good = render_word(os.path.join(work, "good.png"), "GARDEN")
    d = readback.readback_image(good, "   ", out_dir=work + "/cells")
    assert d["verdict"] == "UNMEASURABLE"
    assert d["reason"] == "declared_text_empty"
    assert d["sample_size"] == 0


def test_unmeasurable_can_never_be_reported_as_pass(readback, work):
    """The dead class, pinned: no zero-sample result may carry PASS."""
    cases = [
        render_blank(os.path.join(work, "b0.png"), level=0),
        render_blank(os.path.join(work, "b255.png"), level=255),
    ]
    for p in cases:
        d = readback.readback_image(p, "GARDEN", out_dir=work + "/u")
        assert not (d["sample_size"] == 0 and d["verdict"] == "PASS")
        assert d["verdict"] == "UNMEASURABLE"


# --------------------------------------------------------------------------
# 2 + 3. garbage ~0 / exact render high
# --------------------------------------------------------------------------
def test_garbage_vs_declared_scores_zero_and_fails(readback, work):
    noise = render_noise(os.path.join(work, "noise.png"))
    d = readback.readback_image(noise, "GARDEN", out_dir=work + "/cells")
    assert d["sample_size"] == readback.ENSEMBLE_CELLS
    assert d["agreement"] == 0.0, d["cells"]
    assert d["verdict"] == "FAIL"
    assert d["ink"]["ink_pixels"] > 0          # it measured real pixels


def test_exact_render_vs_declared_scores_high_and_passes(readback, work):
    for wrd in ("GARDEN", "Garden", "sunlight", "market"):
        p = render_word(os.path.join(work, "w_%s.png" % wrd), wrd)
        d = readback.readback_image(p, wrd, out_dir=os.path.join(work, "c_" + wrd))
        assert d["verdict"] == "PASS", (wrd, d["cells"])
        assert d["agreement"] >= 0.5, (wrd, d["agreement"], d["cells"])
        assert d["sample_size"] == readback.ENSEMBLE_CELLS


def test_exact_render_vs_a_DIFFERENT_declared_word_fails(readback, work):
    p = render_word(os.path.join(work, "w.png"), "GARDEN")
    d = readback.readback_image(p, "GOLDEN", out_dir=work + "/cells")
    assert d["verdict"] == "FAIL"
    assert d["agreement"] == 0.0
    assert readback.normalize_text(d["modal_read"]) == "garden"


def test_clipped_render_fails(readback, work):
    """A word whose glyphs run off the plate is a real defect and must FAIL.
    (A 640px canvas clips a long word at 140px and the gate reads a truncated
    fragment — exactly the behaviour wanted.)"""
    p = render_word(os.path.join(work, "clip.png"), "strawberries",
                    size=(640, 240), px=140)
    d = readback.readback_image(p, "strawberries", out_dir=work + "/cells")
    assert d["verdict"] == "FAIL"
    assert d["agreement"] == 0.0
    assert d["ink"]["ink_bbox_xyxy"][0] == 0        # ink touches the left edge


def test_light_ink_on_dark_is_read(readback, work):
    """Polarity handling: white ink on black must read as well as black on white."""
    p = render_word(os.path.join(work, "inv.png"), "GARDEN",
                    ink=(255, 255, 255), bg=(0, 0, 0))
    d = readback.readback_image(p, "GARDEN", out_dir=work + "/cells")
    assert d["verdict"] == "PASS", d["cells"]
    assert d["ink"]["polarity"] == "light"


def test_chromatic_ink_with_no_luma_contrast_is_still_read(readback, work):
    """refA C14 class: crimson on dark brown — luma-blind, chroma-legible."""
    p = render_word(os.path.join(work, "chroma.png"), "your",
                    ink=(150, 20, 55), bg=(40, 44, 42))
    d = readback.readback_image(p, "your", out_dir=work + "/cells")
    assert d["verdict"] == "PASS", (d["projection_used"], d["per_projection"])
    assert d["sample_size"] == readback.ENSEMBLE_CELLS


def test_projection_choice_is_blind_to_declared_text(readback, work):
    """The projection that wins must not depend on what we claim the word is."""
    p = render_word(os.path.join(work, "chroma.png"), "your",
                    ink=(150, 20, 55), bg=(40, 44, 42))
    a = readback.readback_image(p, "your", out_dir=work + "/a")
    b = readback.readback_image(p, "zzzz", out_dir=work + "/b")
    assert a["projection_used"] == b["projection_used"]
    assert a["modal_read"] == b["modal_read"]


def test_confidence_is_never_a_gate_input(readback, work):
    p = render_word(os.path.join(work, "w.png"), "GARDEN")
    d = readback.readback_image(p, "GARDEN", out_dir=work + "/cells")
    assert d["protocol"]["confidence_used_as_gate_input"] is False
    # confidence is still captured for forensics
    obs = [o for c in d["cells_full"] for o in c.get("observations", [])]
    assert obs and all("confidence" in o for o in obs)


# --------------------------------------------------------------------------
# ensemble geometry
# --------------------------------------------------------------------------
def test_ensemble_is_twelve_cells_of_the_declared_geometry(readback, work):
    p = render_word(os.path.join(work, "w.png"), "GARDEN")
    d = readback.readback_image(p, "GARDEN", out_dir=work + "/cells")
    assert readback.ENSEMBLE_CELLS == 12
    assert len(d["cells"]) == 12
    got = {(c["height"], c["pad_factor"]) for c in d["cells"]}
    assert got == {(h, p_) for h in readback.ENSEMBLE_HEIGHTS
                   for p_ in readback.ENSEMBLE_PADS}
    assert all(os.path.exists(c["png"]) for c in d["cells"])


# --------------------------------------------------------------------------
# 4. read_region — RESOLVED / UNRESOLVED_TEXT / UNMEASURABLE
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def synth_video(tmp_path_factory):
    """6 frames: 0-2 carry the word 'GARDEN', 3-5 carry pure noise."""
    d = str(tmp_path_factory.mktemp("synthvid"))
    word = Image.open(render_word(os.path.join(d, "w.png"), "GARDEN",
                                  size=(960, 540), px=180))
    noise = Image.open(render_noise(os.path.join(d, "n.png"), size=(960, 540)))
    path = make_video(os.path.join(d, "v.mp4"), [word] * 3 + [noise] * 3)
    return path, d


def test_read_region_resolves_a_real_word(readback, synth_video, work):
    path, _ = synth_video
    d = readback.read_region(path, 1, [180, 180, 780, 360], work + "/r")
    assert d["verdict"] == "RESOLVED", d
    assert readback.normalize_text(d["modal_read"]) == "garden"
    assert d["stability"] >= readback.STABILITY_FLOOR
    assert d["sample_size"] > 0
    assert os.path.exists(d["evidence"]["crop_png"])


def test_read_region_returns_unresolved_text_instead_of_guessing(readback,
                                                                 synth_video, work):
    path, _ = synth_video
    d = readback.read_region(path, 4, [180, 180, 780, 360], work + "/r")
    assert d["verdict"] == "UNRESOLVED_TEXT", d
    assert d["modal_read"] is None
    assert d["modal_normalized"] is None
    assert d["sample_size"] > 0            # cells WERE measured; the word was not
    assert d["reason"] in ("all_cells_read_empty", "no_modal_read_reached_floor")


def test_read_region_frame_out_of_range_is_unmeasurable(readback, synth_video, work):
    path, _ = synth_video
    d = readback.read_region(path, 9999, [180, 180, 780, 360], work + "/r")
    assert d["verdict"] == "UNMEASURABLE"
    assert d["sample_size"] == 0
    assert d["reason"] == "frame_extract_failed"


def test_read_region_empty_bbox_is_unmeasurable(readback, synth_video, work):
    path, _ = synth_video
    d = readback.read_region(path, 1, [400, 300, 400, 300], work + "/r")
    assert d["verdict"] == "UNMEASURABLE"
    assert d["sample_size"] == 0
    assert d["reason"] in ("empty_bbox", "crop_out_of_frame")


def test_read_region_is_zero_based_decoded_order(readback, synth_video, work):
    """Frames 0-2 are the word, 3-5 are noise: the boundary must land at 3."""
    path, _ = synth_video
    box = [180, 180, 780, 360]
    reads = {}
    for f in range(6):
        d = readback.read_region(path, f, box, os.path.join(work, "f%d" % f))
        reads[f] = (d["verdict"], readback.normalize_text(d.get("modal_read") or ""))
    assert reads[0][1] == "garden" and reads[2][1] == "garden", reads
    assert reads[3][1] != "garden" and reads[5][1] != "garden", reads


# --------------------------------------------------------------------------
# readback_pair (differential)
# --------------------------------------------------------------------------
def test_pair_self_comparison_passes(readback, work):
    p = render_word(os.path.join(work, "w.png"), "GARDEN")
    d = readback.readback_pair(p, p, declared_text="GARDEN", out_dir=work + "/pair")
    assert d["verdict"] == "PASS"
    assert d["paired_agreement"] == 1.0
    assert d["modal_match"] is True
    assert d["sample_size"] == readback.ENSEMBLE_CELLS


def test_pair_different_words_fails(readback, work):
    a = render_word(os.path.join(work, "a.png"), "GARDEN")
    b = render_word(os.path.join(work, "b.png"), "GOLDEN")
    d = readback.readback_pair(a, b, declared_text="GARDEN", out_dir=work + "/pair")
    assert d["verdict"] == "FAIL"
    assert d["modal_match"] is False


def test_pair_with_blank_side_is_unmeasurable(readback, work):
    a = render_word(os.path.join(work, "a.png"), "GARDEN")
    blank = render_blank(os.path.join(work, "blank.png"))
    d = readback.readback_pair(a, blank, declared_text="GARDEN", out_dir=work + "/pair")
    assert d["verdict"] == "UNMEASURABLE"
    assert d["reason"] == "no_ink_on_one_side"


# --------------------------------------------------------------------------
# 5. OPTIONAL LOCAL-CORPUS CALIBRATION PINS (skip unless a corpus is configured)
# --------------------------------------------------------------------------
def test_calibration_rejected_plates_fail_and_accepted_plates_pass(readback, work):
    """Reviewer-rejected plates must sit BELOW the threshold and accepted plates
    of the same reel ABOVE it, with a real margin."""
    exp = corpus_expect("refA")
    require(REFA_PLATES, "refA plates")
    rejected = {}
    for pid in exp["rejected_states"]:
        d = readback.readback_image(os.path.join(REFA_PLATES, "%s.png" % pid),
                                    exp["rejected_word"], out_dir=os.path.join(work, pid))
        rejected[pid] = d
        assert d["sample_size"] == readback.ENSEMBLE_CELLS
        assert d["verdict"] == "FAIL", d
        assert d["agreement"] <= 0.08, d["agreement"]

    accepted = {}
    for pid, txt in exp["plate_words"].items():
        d = readback.readback_image(os.path.join(REFA_PLATES, "%s.png" % pid), txt,
                                    out_dir=os.path.join(work, pid))
        accepted[pid] = d
        assert d["verdict"] == "PASS", (pid, d["agreement"], d["modal_read"])
        assert d["agreement"] >= 0.50, (pid, d["agreement"])

    worst_accepted = min(d["agreement"] for d in accepted.values())
    best_rejected = max(d["agreement"] for d in rejected.values())
    assert best_rejected < readback.AGREEMENT_THRESHOLD <= worst_accepted


@pytest.mark.slow
def test_calibration_damaged_delivered_word_fails_reference_passes(readback, work):
    """The gate MUST fail the damaged delivered word and pass the reference."""
    exp = corpus_expect("refA")
    require(REFA_DELIVERED, "refA delivered")
    require(REFA_REFERENCE, "refA reference")
    word = exp["rejected_word"]
    norm = readback.normalize_text(word)
    for frame in exp["probe_frames"]:
        ref = readback.read_region(REFA_REFERENCE, frame, WORD_BOX,
                                   os.path.join(work, "ref%d" % frame), keep_frame=True)
        dev = readback.read_region(REFA_DELIVERED, frame, WORD_BOX,
                                   os.path.join(work, "del%d" % frame), keep_frame=True)
        assert ref["verdict"] == "RESOLVED"
        assert readback.normalize_text(ref["modal_read"]) == norm, ref["all_reads"]
        assert readback.normalize_text(dev.get("modal_read") or "") != norm, \
            dev["all_reads"]

        # the gate, on the same crops
        rb_ref = readback.readback_image(ref["evidence"]["crop_png"], word,
                                         out_dir=os.path.join(work, "rbref%d" % frame))
        rb_dev = readback.readback_image(dev["evidence"]["crop_png"], word,
                                         out_dir=os.path.join(work, "rbdel%d" % frame))
        assert rb_ref["verdict"] == "PASS", rb_ref["cells"]
        assert rb_dev["verdict"] == "FAIL", rb_dev["cells"]

        # and differentially
        pair = readback.readback_pair(dev["evidence"]["crop_png"],
                                      ref["evidence"]["crop_png"],
                                      declared_text=word,
                                      out_dir=os.path.join(work, "pair%d" % frame))
        assert pair["verdict"] == "FAIL", pair
        self_pair = readback.readback_pair(ref["evidence"]["crop_png"],
                                           ref["evidence"]["crop_png"],
                                           declared_text=word,
                                           out_dir=os.path.join(work, "self%d" % frame))
        assert self_pair["verdict"] == "PASS", self_pair
