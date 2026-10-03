"""Tests for wordtruth.py — reference word-track recovery.

Pins:
  * both on-disk contract schemas normalise identically
  * midframe / probe-frame arithmetic is zero-based and stays inside the span
  * a state whose reference cannot be read is UNRESOLVED_TEXT and is EXCLUDED
    from sample_size — never counted as a match
  * a contract with no readable reference is UNMEASURABLE, never PASS
  * declared == reference -> PASS ; declared != reference -> FAIL
  * an optional local corpus (see readback_fixtures.corpus_expect): the same
    contract PASSes against the reference and FAILs against a damaged delivery
"""

import json
import os

import pytest
from PIL import Image

from readback_fixtures import (REFD_CONTRACT, REFC_CONTRACT, REFC_DELIVERED,
                               REFC_REFERENCE, REFA_CONTRACT, REFA_DELIVERED,
                               REFA_REFERENCE, corpus_expect, make_video, render_blank,
                               render_noise, render_word, require)


@pytest.fixture(scope="session")
def wordtruth():
    import wordtruth as m
    return m


@pytest.fixture(scope="session")
def readback():
    import readback as m
    return m


@pytest.fixture
def work(tmp_path):
    return str(tmp_path)


# --------------------------------------------------------------------------
# schema tolerance
# --------------------------------------------------------------------------
REELCTL_SHAPE = {
    "states": [
        {"id": "c01s", "start_frame": 0, "end_frame_exclusive": 13,
         "text": "Every day counts", "style_id": "T_sans",
         "placement": {"core_bbox_xyxy": [433, 508, 1007, 623],
                       "treatment_bbox_xyxy": [433, 508, 1007, 623]},
         "evidence": {"tier": "INDEXED", "mask_path": "plates/c01s.png"}},
    ]
}
WORKBENCH_SHAPE = {
    "C01a": {"id": "C01a", "text": "here is the plan", "start": 0, "end": 2,
             "style": "T_sans", "box": [512, 519, 1406, 572],
             "plate": "plates/C01a.png", "tier": "INDEXED"},
    "C02": {"id": "C02", "text": "Hello", "start": 47, "end": 48,
            "style": "T_script", "box": [769, 432, 1189, 629],
            "plate": "plates/C02.png", "tier": "MASK_VERIFIED"},
}


def test_normalize_states_reelctl_shape(wordtruth):
    st = wordtruth.normalize_states(REELCTL_SHAPE)
    assert len(st) == 1
    s = st[0]
    assert s["id"] == "c01s" and s["text"] == "Every day counts"
    assert s["start"] == 0 and s["end_exclusive"] == 13
    assert s["box"] == [433, 508, 1007, 623]
    assert s["tier"] == "INDEXED" and s["plate"] == "plates/c01s.png"


def test_normalize_states_workbench_shape(wordtruth):
    st = wordtruth.normalize_states(WORKBENCH_SHAPE)
    assert [s["id"] for s in st] == ["C01a", "C02"]      # sorted by start frame
    assert st[1]["start"] == 47 and st[1]["end_exclusive"] == 48
    assert st[1]["box"] == [769, 432, 1189, 629]


def test_real_contracts_on_disk_normalise(wordtruth):
    p = require(REFD_CONTRACT, "refD contract")
    st = wordtruth.normalize_states(json.load(open(p)))
    assert len(st) > 0
    assert all(s["box"] and s["start"] is not None and s["text"] for s in st)


# --------------------------------------------------------------------------
# frame arithmetic
# --------------------------------------------------------------------------
def test_midframe_is_zero_based_and_inside_the_span(wordtruth):
    assert wordtruth.midframe({"start": 0, "end_exclusive": 13}) == 6
    assert wordtruth.midframe({"start": 146, "end_exclusive": 148}) == 147
    assert wordtruth.midframe({"start": 5, "end_exclusive": 6}) == 5
    assert wordtruth.midframe({"start": 9, "end_exclusive": None}) == 9
    assert wordtruth.midframe({"start": None, "end_exclusive": 4}) is None


def test_probe_frames_stay_inside_the_span_and_lead_with_midframe(wordtruth):
    f = wordtruth.probe_frames({"start": 146, "end_exclusive": 148})
    assert f[0] == 147
    assert all(146 <= x < 148 for x in f)
    assert len(f) == len(set(f))
    f2 = wordtruth.probe_frames({"start": 116, "end_exclusive": 127}, limit=4)
    assert f2[0] == 121 and all(116 <= x < 127 for x in f2) and len(f2) == 4
    assert wordtruth.probe_frames({"start": 3, "end_exclusive": 4}) == [3]


def test_lower_third_box(wordtruth):
    assert wordtruth.lower_third_box(1916, 1078) == [0, 647, 1916, 1078]


# --------------------------------------------------------------------------
# UNMEASURABLE paths
# --------------------------------------------------------------------------
def test_missing_contract_is_unmeasurable(wordtruth, work):
    d = wordtruth.wordtruth_project(contract_path=os.path.join(work, "nope.json"),
                                    reference_video=REFA_REFERENCE, out_dir=work)
    assert d["verdict"] == "UNMEASURABLE"
    assert d["reason"] == "contract_not_found"
    assert d["sample_size"] == 0


def test_missing_reference_is_unmeasurable(wordtruth, work):
    c = os.path.join(work, "c.json")
    json.dump(REELCTL_SHAPE, open(c, "w"))
    d = wordtruth.wordtruth_project(contract_path=c,
                                    reference_video=os.path.join(work, "nope.mp4"),
                                    out_dir=work)
    assert d["verdict"] == "UNMEASURABLE"
    assert d["reason"] == "reference_video_not_found"
    assert d["sample_size"] == 0


def test_contract_with_no_states_is_unmeasurable(wordtruth, work):
    c = os.path.join(work, "c.json")
    json.dump({"states": []}, open(c, "w"))
    require(REFA_REFERENCE, "refA reference")
    d = wordtruth.wordtruth_project(contract_path=c, reference_video=REFA_REFERENCE,
                                    out_dir=work)
    assert d["verdict"] == "UNMEASURABLE"
    assert d["reason"] == "contract_has_no_states"


# --------------------------------------------------------------------------
# synthetic end-to-end
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    """A 'reference' video: frames 0-5 say GARDEN, 6-11 say MARKET, 12-17 noise."""
    d = str(tmp_path_factory.mktemp("wtsynth"))
    a = Image.open(render_word(os.path.join(d, "a.png"), "GARDEN",
                               size=(960, 540), px=170))
    b = Image.open(render_word(os.path.join(d, "b.png"), "MARKET",
                               size=(960, 540), px=170))
    n = Image.open(render_noise(os.path.join(d, "n.png"), size=(960, 540)))
    vid = make_video(os.path.join(d, "ref.mp4"), [a] * 6 + [b] * 6 + [n] * 6)
    box = [150, 170, 810, 370]
    contract = {"states": [
        {"id": "S1", "start_frame": 0, "end_frame_exclusive": 6, "text": "GARDEN",
         "placement": {"core_bbox_xyxy": box}, "evidence": {"tier": "INDEXED"}},
        {"id": "S2", "start_frame": 6, "end_frame_exclusive": 12, "text": "MARKET",
         "placement": {"core_bbox_xyxy": box}, "evidence": {"tier": "INDEXED"}},
        {"id": "S3", "start_frame": 12, "end_frame_exclusive": 18, "text": "GHOST",
         "placement": {"core_bbox_xyxy": box}, "evidence": {"tier": "INDEXED"}},
    ]}
    cpath = os.path.join(d, "contract.json")
    json.dump(contract, open(cpath, "w"))
    return d, vid, cpath, box


def test_declared_matching_reference_is_a_MATCH(wordtruth, synth, work):
    d, vid, cpath, _ = synth
    r = wordtruth.wordtruth_project(contract_path=cpath, reference_video=vid,
                                    out_dir=os.path.join(work, "wt"),
                                    states=["S1", "S2"])
    assert r["verdict"] == "PASS", r["states"]
    assert r["sample_size"] == 2
    assert r["states_matched"] == 2 and r["states_mismatched"] == 0
    assert all(s["words_match"] == "MATCH" for s in r["states"])
    assert all(s["frame_used"] is not None for s in r["states"])


def test_declared_differing_from_reference_is_a_MISMATCH(wordtruth, synth, work):
    d, vid, _, box = synth
    c = {"states": [{"id": "W1", "start_frame": 0, "end_frame_exclusive": 6,
                     "text": "GOLDEN", "placement": {"core_bbox_xyxy": box},
                     "evidence": {"tier": "INDEXED"}}]}
    cp = os.path.join(work, "wrong.json")
    json.dump(c, open(cp, "w"))
    r = wordtruth.wordtruth_project(contract_path=cp, reference_video=vid,
                                    out_dir=os.path.join(work, "wt"))
    assert r["verdict"] == "FAIL"
    assert r["reason"] == "reference_words_differ_from_declared"
    assert r["states"][0]["words_match"] == "MISMATCH"
    assert r["states"][0]["reference_normalized"] == "garden"


def test_unreadable_reference_state_is_UNRESOLVED_and_excluded(wordtruth, synth, work):
    """S3 sits over pure noise: it must be UNRESOLVED_TEXT, not a guess, and it
    must NOT inflate sample_size."""
    d, vid, cpath, _ = synth
    r = wordtruth.wordtruth_project(contract_path=cpath, reference_video=vid,
                                    out_dir=os.path.join(work, "wt"))
    by = {s["state_id"]: s for s in r["states"]}
    assert by["S3"]["words_match"] == "UNRESOLVED_TEXT"
    assert by["S3"]["reference_read"] is None
    assert r["states_unresolved"] == 1
    assert r["sample_size"] == 2          # S3 excluded from the arithmetic
    assert "S3" in r["unresolved_ids"]
    assert by["S3"]["attempts"], "an unresolved state must still show its attempts"


def test_all_states_unreadable_is_unmeasurable_not_pass(wordtruth, synth, work):
    d, vid, _, box = synth
    c = {"states": [{"id": "N1", "start_frame": 12, "end_frame_exclusive": 18,
                     "text": "GHOST", "placement": {"core_bbox_xyxy": box},
                     "evidence": {"tier": "INDEXED"}}]}
    cp = os.path.join(work, "noise.json")
    json.dump(c, open(cp, "w"))
    r = wordtruth.wordtruth_project(contract_path=cp, reference_video=vid,
                                    out_dir=os.path.join(work, "wt"))
    assert r["verdict"] == "UNMEASURABLE"
    assert r["verdict"] != "PASS"
    assert r["reason"] == "no_state_reference_read_resolved"
    assert r["sample_size"] == 0


def test_state_without_a_box_is_unresolved(wordtruth, synth, work):
    d, vid, _, _ = synth
    c = {"states": [{"id": "NB", "start_frame": 0, "end_frame_exclusive": 6,
                     "text": "GARDEN", "placement": {}, "evidence": {}}]}
    cp = os.path.join(work, "nobox.json")
    json.dump(c, open(cp, "w"))
    r = wordtruth.wordtruth_project(contract_path=cp, reference_video=vid,
                                    out_dir=os.path.join(work, "wt"))
    assert r["states"][0]["words_match"] == "UNRESOLVED_TEXT"
    assert r["states"][0]["reason"] == "state_missing_frames_or_box"
    assert r["verdict"] == "UNMEASURABLE"


# --------------------------------------------------------------------------
# neighbour contamination
# --------------------------------------------------------------------------
def test_neighbours_in_crop_finds_an_adjacent_live_state(wordtruth):
    a = {"id": "A", "box": [100, 100, 200, 160], "start": 0, "end_exclusive": 10}
    near = {"id": "B", "box": [205, 100, 300, 160], "start": 0, "end_exclusive": 10}
    far = {"id": "C", "box": [900, 900, 950, 950], "start": 0, "end_exclusive": 10}
    dead = {"id": "D", "box": [205, 100, 300, 160], "start": 50, "end_exclusive": 60}
    got = wordtruth.neighbours_in_crop(a, [a, near, far, dead], 5)
    assert got == ["B"]                    # live + overlapping only
    assert wordtruth.neighbours_in_crop(a, [a, far, dead], 5) == []


def test_padded_box_is_twenty_percent(wordtruth):
    assert wordtruth.padded_box([100, 100, 200, 200]) == [80, 80, 220, 220]


# --------------------------------------------------------------------------
# triage_delivered — the reference is the reader's control
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def triage_pair(tmp_path_factory):
    """reference says GARDEN then MARKET; the 'delivered' breaks GARDEN into GOLDEN."""
    d = str(tmp_path_factory.mktemp("triage"))
    good = Image.open(render_word(os.path.join(d, "p.png"), "GARDEN",
                                  size=(960, 540), px=170))
    other = Image.open(render_word(os.path.join(d, "a.png"), "MARKET",
                                   size=(960, 540), px=170))
    broken = Image.open(render_word(os.path.join(d, "b.png"), "GOLDEN",
                                    size=(960, 540), px=170))
    ref = make_video(os.path.join(d, "ref.mp4"), [good] * 6 + [other] * 6)
    dev = make_video(os.path.join(d, "del.mp4"), [broken] * 6 + [other] * 6)
    box = [150, 170, 810, 370]
    c = {"states": [
        {"id": "S1", "start_frame": 0, "end_frame_exclusive": 6, "text": "GARDEN",
         "placement": {"core_bbox_xyxy": box}, "evidence": {}},
        {"id": "S2", "start_frame": 6, "end_frame_exclusive": 12, "text": "MARKET",
         "placement": {"core_bbox_xyxy": box}, "evidence": {}},
    ]}
    cp = os.path.join(d, "c.json")
    json.dump(c, open(cp, "w"))
    return cp, ref, dev


def test_triage_blocks_a_broken_delivered_word(wordtruth, triage_pair, work):
    cp, ref, dev = triage_pair
    r = wordtruth.triage_delivered(cp, ref, dev, os.path.join(work, "t"))
    by = {s["state_id"]: s for s in r["states"]}
    assert by["S1"]["verdict"] == "MISMATCH" and by["S1"]["blocking"] is True
    assert by["S2"]["verdict"] == "MATCH"
    assert r["verdict"] == "FAIL"
    assert r["mismatched_ids"] == ["S1"]
    assert r["sample_size"] == 2


def test_triage_self_comparison_passes(wordtruth, triage_pair, work):
    """reference vs itself must PASS — the false-positive guard."""
    cp, ref, _ = triage_pair
    r = wordtruth.triage_delivered(cp, ref, ref, os.path.join(work, "t"))
    assert r["verdict"] == "PASS", r["states"]
    assert r["states_blocking_mismatch"] == 0
    assert r["sample_size"] == 2


def test_triage_marks_reader_limits_instead_of_failing_them(wordtruth, work):
    """When the reference itself does not read as the declared word, the state is
    UNVERIFIABLE_BY_READER and NON-blocking — never a FAIL (a stylised script
    face the OCR misreads on the reference itself)."""
    d = work
    a = Image.open(render_word(os.path.join(d, "a.png"), "GARDEN",
                               size=(960, 540), px=170))
    ref = make_video(os.path.join(d, "ref.mp4"), [a] * 6)
    c = {"states": [{"id": "S1", "start_frame": 0, "end_frame_exclusive": 6,
                     "text": "TOTALLYDIFFERENT",
                     "placement": {"core_bbox_xyxy": [150, 170, 810, 370]},
                     "evidence": {}}]}
    cp = os.path.join(d, "c.json")
    json.dump(c, open(cp, "w"))
    r = wordtruth.triage_delivered(cp, ref, ref, os.path.join(d, "t"))
    assert r["states"][0]["verdict"] == "UNVERIFIABLE_BY_READER"
    assert r["states"][0]["blocking"] is False
    assert r["sample_size"] == 0
    assert r["verdict"] == "UNMEASURABLE"      # nothing blocking => not a PASS
    assert r["verdict"] != "PASS"


@pytest.mark.slow
def test_triage_real_corpus_rejected_states_fail_accepted_states_pass(wordtruth, work):
    """Corpus pin, both directions: reviewer-rejected states fail, accepted pass."""
    exp = corpus_expect("refA")
    require(REFA_CONTRACT, "refA contract")
    require(REFA_REFERENCE, "refA reference")
    require(REFA_DELIVERED, "refA delivered")
    rejected, accepted = exp["rejected_states"], exp["accepted_states"]
    word = wordtruth.normalize_text(exp["rejected_word"])
    r = wordtruth.triage_delivered(REFA_CONTRACT, REFA_REFERENCE, REFA_DELIVERED,
                                   os.path.join(work, "refA"),
                                   states=rejected + accepted)
    by = {s["state_id"]: s for s in r["states"]}
    assert r["verdict"] == "FAIL"
    assert set(r["mismatched_ids"]) == set(rejected), r["mismatched_ids"]
    for sid in rejected:
        assert by[sid]["reference_read"] and word in by[sid]["reference_read"].lower()
        assert word not in (by[sid]["delivered_read"] or "").lower()
    for sid in accepted:
        assert by[sid]["verdict"] == "MATCH", by[sid]

    # the reference against itself must not fail
    s = wordtruth.triage_delivered(REFA_CONTRACT, REFA_REFERENCE, REFA_REFERENCE,
                                   os.path.join(work, "self"),
                                   states=rejected + accepted)
    assert s["verdict"] == "PASS", s["mismatched_ids"]


@pytest.mark.slow
def test_triage_does_not_fail_accepted_script_states(wordtruth, work):
    """States a reviewer accepted must not fail; a threshold that fails a past
    approval is a bad threshold."""
    exp = corpus_expect("refC")
    c = require(REFC_CONTRACT, "refC contract")
    ref = require(REFC_REFERENCE, "refC reference")
    dev = require(REFC_DELIVERED, "refC delivered")
    r = wordtruth.triage_delivered(c, ref, dev, os.path.join(work, "refC"),
                                   states=exp["accepted_states"])
    assert r["states_blocking_mismatch"] == 0, r["mismatched_ids"]
    assert r["verdict"] == "PASS"
    limited = exp.get("reader_limited_state")
    if limited:
        by = {s["state_id"]: s for s in r["states"]}
        assert by[limited]["verdict"] == "UNVERIFIABLE_BY_READER"
        assert by[limited]["blocking"] is False


# --------------------------------------------------------------------------
# platetruth
# --------------------------------------------------------------------------
def test_platetruth_missing_plate_is_unmeasurable(wordtruth, work):
    c = os.path.join(work, "c.json")
    json.dump({"states": [{"id": "X", "text": "GARDEN", "start_frame": 0,
                           "end_frame_exclusive": 2,
                           "placement": {"core_bbox_xyxy": [0, 0, 10, 10]},
                           "evidence": {"mask_path": "plates/missing.png"}}]},
              open(c, "w"))
    r = wordtruth.platetruth_contract(c, work, os.path.join(work, "pt"))
    assert r["verdict"] == "UNMEASURABLE"
    assert r["sample_size"] == 0
    assert r["states"][0]["reason"] == "plate_missing"


def test_platetruth_scores_good_and_broken_plates(wordtruth, work):
    plates = os.path.join(work, "plates")
    os.makedirs(plates, exist_ok=True)
    render_word(os.path.join(plates, "good.png"), "GARDEN")
    render_noise(os.path.join(plates, "bad.png"))
    c = os.path.join(work, "c.json")
    json.dump({"states": [
        {"id": "G", "text": "GARDEN", "start_frame": 0, "end_frame_exclusive": 2,
         "placement": {"core_bbox_xyxy": [0, 0, 10, 10]},
         "evidence": {"mask_path": "plates/good.png"}},
        {"id": "B", "text": "GARDEN", "start_frame": 2, "end_frame_exclusive": 4,
         "placement": {"core_bbox_xyxy": [0, 0, 10, 10]},
         "evidence": {"mask_path": "plates/bad.png"}},
    ]}, open(c, "w"))
    r = wordtruth.platetruth_contract(c, work, os.path.join(work, "pt"))
    assert r["verdict"] == "FAIL"
    assert r["failed_ids"] == ["B"]
    assert r["sample_size"] == 2


# --------------------------------------------------------------------------
# OPTIONAL LOCAL CORPUS PIN
# --------------------------------------------------------------------------
@pytest.mark.slow
def test_REFA_contract_matches_reference_but_not_damaged_delivery(wordtruth, work):
    exp = corpus_expect("refA")
    require(REFA_CONTRACT, "refA contract")
    require(REFA_REFERENCE, "refA reference")
    require(REFA_DELIVERED, "refA delivered")
    rejected, accepted = exp["rejected_states"], exp["accepted_states"]
    ids = rejected + accepted

    ref = wordtruth.wordtruth_project(contract_path=REFA_CONTRACT,
                                      reference_video=REFA_REFERENCE,
                                      out_dir=os.path.join(work, "ref"), states=ids)
    by = {s["state_id"]: s for s in ref["states"]}
    for sid in rejected:
        assert by[sid]["words_match"] == "MATCH", by[sid]
    assert ref["verdict"] == "PASS", ref["mismatched_ids"]

    dev = wordtruth.wordtruth_project(contract_path=REFA_CONTRACT,
                                      reference_video=REFA_DELIVERED,
                                      out_dir=os.path.join(work, "del"), states=ids)
    byd = {s["state_id"]: s for s in dev["states"]}
    assert dev["verdict"] == "FAIL"
    for sid in rejected:
        assert byd[sid]["words_match"] == "MISMATCH", byd[sid]
    for sid in accepted:
        assert byd[sid]["words_match"] == "MATCH"
