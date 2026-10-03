"""Calibration pin: one reviewer-rejected word state from an optional local corpus.

Ground truth (supplied by the corpus owner in <CORPUS>/refA/expect.json):
  reference  refA/reference.mp4  -> the word rendered crisply
  delivered  refA/delivered.mp4  -> the same word with fused / eroded strokes

REQUIRED, and the reason this file exists:
  * the reference compared against ITSELF over two DISJOINT frame subsets PASSes;
  * the delivered file compared against the reference FAILs on at least one of the three axes.

A gate that cannot do both is not a gate. These tests are marked ``slow`` because they decode
real footage; they SKIP (never silently pass) when no corpus is configured
(see ``readback_fixtures.corpus_expect`` and ``CAPTION_TEST_CORPUS``).

expect.json keys used here: rejected_word, word_span [start, end_exclusive], word_box,
ref_frames [a, b] (two disjoint frames inside the span), del_frame, del_unrecoverable_frame.
"""

import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import anatomy   # noqa: E402
import inkcheck  # noqa: E402
from readback_fixtures import (REFA_CONTRACT, REFA_DELIVERED, REFA_REFERENCE,  # noqa: E402
                               corpus_expect, require)

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def exp():
    return corpus_expect("refA")


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    return str(tmp_path_factory.mktemp("ground-truth"))


@pytest.fixture(scope="module")
def word_state(exp):
    require(REFA_CONTRACT, "contract")
    states = json.load(open(REFA_CONTRACT))["states"]
    word = exp["rejected_word"]
    hits = [s for s in states if (s.get("text") or "").strip() == word]
    assert hits, "contract carries no state whose text is %r" % word
    merged = dict(hits[0])
    merged["id"] = "GT_WORD"
    merged["start_frame"] = min(s["start_frame"] for s in hits)
    merged["end_frame_exclusive"] = max(s["end_frame_exclusive"] for s in hits)
    return merged, states


@pytest.fixture(scope="module")
def masks(exp, word_state, work):
    require(REFA_REFERENCE, "reference")
    require(REFA_DELIVERED, "delivered")
    state, states = word_state
    a, b = exp["ref_frames"]
    d = exp["del_frame"]
    out = {}
    for name, video, frames in (("refA", REFA_REFERENCE, [a]),
                                ("refB", REFA_REFERENCE, [b]),
                                ("del", REFA_DELIVERED, [d])):
        out[name] = anatomy.recover_state_mask(
            video, state, work, all_states=states, frames=frames, label=f"gt-{name}"
        )
    return out


def _span_state(exp):
    s, e = exp["word_span"]
    return {"id": "GT_WORD", "start_frame": s, "end_frame_exclusive": e}


# --------------------------------------------------------------------------
# recovery
# --------------------------------------------------------------------------
def test_word_state_span_and_box_match_the_expectations(exp, word_state):
    state, _ = word_state
    assert [state["start_frame"], state["end_frame_exclusive"]] == list(exp["word_span"])
    assert state["placement"]["core_bbox_xyxy"] == list(exp["word_box"])


def test_reference_and_delivered_masks_recover(masks):
    for name in ("refA", "refB", "del"):
        rec = masks[name]
        assert rec["verdict"] == "PASS", f"{name}: {rec.get('reason')}"
        assert rec["ink_px"] > 5000
        assert rec["blank_frame_used"] is not None


def test_delivered_midspan_frames_have_no_compatible_blank_and_say_so(exp, word_state, work):
    """If the delivered footage moves out from under the caption, that is a REPORTABLE
    fact about the delivery, not a green: UNMEASURABLE, with the measured mismatch."""
    if "del_unrecoverable_frame" not in exp:
        pytest.skip("corpus declares no unrecoverable delivered frame")
    require(REFA_DELIVERED, "delivered")
    state, states = word_state
    rec = anatomy.recover_state_mask(
        REFA_DELIVERED, state, work, all_states=states,
        frames=[exp["del_unrecoverable_frame"]], label="gt-del-mid"
    )
    assert rec["verdict"] == "UNMEASURABLE"
    assert "no compatible blank neighbourhood" in rec["reason"]
    assert rec["blank_scores_best"][0][1] > rec["max_background_mismatch"]


# --------------------------------------------------------------------------
# THE calibration
# --------------------------------------------------------------------------
def test_reference_self_comparison_over_disjoint_subsets_passes(masks, work):
    a, b = masks["refA"], masks["refB"]
    assert not set(a["state_frames_used"]) & set(b["state_frames_used"])  # disjoint
    r = anatomy.compare_masks(a["mask"], b["mask"], out_dir=work, label="gt-ref-self")
    assert r["verdict"] == "PASS", r
    assert r["dice"] >= anatomy.DICE_MIN
    assert r["residual_p95_px"] <= anatomy.RESIDUAL_P95_MAX
    assert r["components_source"] == r["components_candidate"]
    assert r["holes_source"] == r["holes_candidate"]


@pytest.mark.parametrize("ref_key", ["refA", "refB"])
def test_delivered_word_fails_against_the_reference(masks, ref_key, work):
    r = anatomy.compare_masks(masks[ref_key]["mask"], masks["del"]["mask"], out_dir=work,
                              label=f"gt-{ref_key}-vs-del")
    assert r["verdict"] == "FAIL", r
    assert r["failed_axes"], "a FAIL must name the axes it failed on"


def test_refinement_does_not_rescue_the_delivered_word(masks):
    """The bounded translation refinement is a nuisance remover: it must not turn the
    broken delivery into a PASS."""
    broken = anatomy.compare_masks(masks["refB"]["mask"], masks["del"]["mask"])
    assert broken["verdict"] == "FAIL"


def test_the_gate_is_not_trivially_red(masks):
    """Guards against a gate that fails everything: the reference must pass against itself
    on the identical mask."""
    r = anatomy.compare_masks(masks["refB"]["mask"], masks["refB"]["mask"].copy())
    assert r["verdict"] == "PASS"
    assert r["dice"] == 1.0


# --------------------------------------------------------------------------
# ink cleanliness on the same ground truth
# --------------------------------------------------------------------------
def test_reference_ink_passes_on_its_own_mask(exp, masks, work):
    r = inkcheck.ink_state(REFA_REFERENCE, _span_state(exp), masks["refB"]["mask"],
                           out_dir=work, label="gt-ref-ink",
                           reference_required=False)   # this measurement IS the reference
    assert r["verdict"] == "PASS", r
    assert r["sample_size"] >= 1


def test_delivered_ink_is_judged_relative_to_the_reference(exp, masks, work):
    """W3 enforces the reference-relative half of its specification: the delivered ink
    is compared against the reference's own Michelson contrast, not only an absolute floor."""
    ref = inkcheck.ink_state(REFA_REFERENCE, _span_state(exp), masks["refB"]["mask"],
                             reference_required=False)
    dl = inkcheck.ink_state(REFA_DELIVERED, _span_state(exp), masks["del"]["mask"],
                            out_dir=work, label="gt-del-ink",
                            reference_michelson=ref["worst_michelson"],
                            reference_separation=ref["worst_separation"])
    assert dl["michelson_ratio_vs_reference"] is not None
    assert dl["reference_relative"] == "ENFORCED"
