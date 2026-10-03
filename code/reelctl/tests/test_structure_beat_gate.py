"""STRUCTURE_QC's beat gate must measure the reel that was built.

An earlier version of `hard_cuts_within_two_frames_of_reference_onsets` iterated the
BLUEPRINT's `hard_cuts_after` against the REFERENCE's audio onsets and never read the
candidate at all. Two consequences:

* a reference whose own cuts sit more than two frames from its own onsets fails its own gate,
  so no build of it can pass;
* every other reel's beat-alignment PASS is vacuous — nothing verified that the cuts actually
  in the delivered file land on the beat.

The candidate's cuts were already being detected at qc.py:426 and thrown away into a
"NON_AUTHORITATIVE supporting signal". This is that measurement, promoted.
"""

from __future__ import annotations

import inspect

from reelctl import qc as qc_module


def test_a_cut_on_the_beat_passes() -> None:
    rows = qc_module.beat_alignment_rows([24, 48], [24, 47])
    assert [row["distance_frames"] for row in rows] == [0, 1]
    assert all(row["within_tolerance"] for row in rows)


def test_a_cut_three_frames_off_the_nearest_onset_fails() -> None:
    rows = qc_module.beat_alignment_rows([24], [21, 60])
    assert rows[0]["distance_frames"] == 3
    assert rows[0]["within_tolerance"] is False


def test_a_reel_with_no_reference_onsets_cannot_claim_alignment() -> None:
    rows = qc_module.beat_alignment_rows([24], [])
    assert rows[0]["nearest_reference_audio_onset_frame"] is None
    assert rows[0]["within_tolerance"] is False


def test_no_measurement_is_not_a_pass() -> None:
    """A build that expects cuts and whose candidate shows none has measured nothing."""
    assert qc_module.beat_alignment_passed([], onsets=[24], expected_cuts=[23]) is False
    # a genuinely uncut reel is a different thing: nothing expected, nothing to align
    assert qc_module.beat_alignment_passed([], onsets=[24], expected_cuts=[]) is True


def test_the_gate_is_computed_from_the_candidates_detected_cuts_not_the_blueprints() -> None:
    """The defect in one assertion: the gate's inputs.

    `expected_hard_cuts` is the blueprint's list. It may inform a reported comparison, but it
    must not be what the ship-blocking check is computed from.
    """
    source = inspect.getsource(qc_module._structure_qc)
    gate_line = next(
        line for line in source.splitlines()
        if "checks[\"hard_cuts_within_two_frames_of_reference_onsets\"]" in line
    )
    body = source.split(gate_line, 1)[1].split("return", 1)[0]
    assert "candidate_cut_frames" in gate_line + body
    assert "expected_hard_cuts" not in gate_line
    assert "detected_hard_cuts" in source
