"""The caption state program (EBG section 2's gap-free ledger).

A sealed typography authority carries the *state program* — exact words, ranges, lifecycle,
font class, stack behaviour — but **no placement boxes and no ink**. Those are pixel
measurements and must come from the reference frames, never from the authority document.
So the engine has two layers:

    sealed authority / reference video  ->  StateProgram  ->  caption contract
                                            (text+timing)     (+ measured geometry+ink)

Fixture: a small synthetic authority built below (inclusive ranges, 60 frames, blank frames
{28, 29}, max overlap depth 3). A real sealed authority can additionally be checked by setting
``REEL_FACTORY_CAPTION_AUTHORITY`` to its ``typography-authority-contract.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reelctl.captions.program import (
    StateProgram,
    StateProgramError,
    import_sealed_authority,
)

SYNTHETIC_SHA = "0" * 64


def _synthetic_authority() -> dict:
    return {
        "authority": {
            "shortcode": "example",
            "video": {
                "frame_count": 60,
                "raster": [1080, 1920],
                "fps": "30/1",
                "decoded_rgb24_essence_sha256": SYNTHETIC_SHA,
            },
        },
        "visual_states": [
            {
                "id": "open-a", "text": "Hello", "start": 0, "end": 19,
                "font_class": "script, warm white", "behavior": "accumulates line 1",
                "entry": {"first_detectable": 0, "readable_blur": [1, 3], "first_crisp": 4},
                "hold": [5, 19],
            },
            {"id": "open-b", "text": "there", "start": 2, "end": 19, "behavior": "accumulates line 2"},
            {"id": "open-c", "text": "World", "start": 4, "end": 19, "behavior": "accumulates line 3"},
            {
                "id": "mid", "text": "simple", "start": 20, "end": 27,
                "exit": {"progressive_blur": [25, 27]},
            },
            {"id": "late", "text": "sample", "start": 30, "end": 44},
            {
                "id": "end", "text": "Text!", "start": 45, "end": 59,
                "hold": [45, 52], "exit": {"progressive_blur": [53, 59]},
            },
        ],
        "regression_gates": {"authority": {"blank_frames": [28, 29]}},
        "font_forensics": {"sans": {"exact_face_name": None}, "script": {"exact_face_name": None}},
        "treatment_contract": {
            "forbidden": ["per-word x/y envelope stretching", "Poppins fallback face"],
        },
        "next_implementation_gate": {"render_allowed": False},
    }


@pytest.fixture(scope="module")
def authority() -> dict:
    return _synthetic_authority()


@pytest.fixture(scope="module")
def program(authority: dict) -> StateProgram:
    return import_sealed_authority(authority)


def test_imports_all_states(program: StateProgram) -> None:
    assert len(program.states) == 6


def test_inclusive_ranges_are_converted_to_half_open(program: StateProgram) -> None:
    """Source 'start': 0, 'end': 19 is inclusive -> [0, 20)."""
    opening = program.state("open-a")
    assert opening.start_frame == 0
    assert opening.end_frame_exclusive == 20
    assert opening.frames == 20


def test_conversion_is_recorded_not_assumed(program: StateProgram) -> None:
    """Rule 1.3: interval semantics are stated, so the conversion stays auditable."""
    assert program.source_interval_semantics == "inclusive"
    assert program.interval_semantics == "half_open"


def test_blank_frames_are_derived_from_coverage_not_asserted(program: StateProgram) -> None:
    assert program.blank_frames == (28, 29)


def test_derived_blanks_must_match_the_sealed_gate_table(authority: dict) -> None:
    """If the derivation disagrees with the sealed gate table, that is a hard failure."""
    tampered = json.loads(json.dumps(authority))
    tampered["regression_gates"]["authority"]["blank_frames"] = [28]
    with pytest.raises(StateProgramError, match="blank"):
        import_sealed_authority(tampered)


def test_exact_case_and_punctuation_are_preserved(program: StateProgram) -> None:
    """regression_gates.state_program.exact_script_text - case is load-bearing."""
    assert program.state("open-a").text == "Hello"
    assert program.state("open-b").text == "there"
    assert program.state("end").text == "Text!"


def test_concurrent_states_get_distinct_stacking_z(program: StateProgram) -> None:
    """The three accumulating opening lines are visible together -> three distinct z."""
    zs = [program.state(state_id).z for state_id in ("open-a", "open-b", "open-c")]
    assert sorted(zs) == [0, 1, 2]


def test_stacking_z_follows_accumulation_order(program: StateProgram) -> None:
    """Earlier-entering lines sit lower in the stack."""
    assert program.state("open-a").z == 0
    assert program.state("open-b").z == 1
    assert program.state("open-c").z == 2


def test_non_overlapping_states_may_reuse_z(program: StateProgram) -> None:
    """z is a stack slot, not a global counter."""
    assert program.state("mid").z == 0
    assert program.state("late").z == 0


def test_every_overlapping_pair_has_distinct_z(program: StateProgram) -> None:
    states = program.states
    for left in states:
        for right in states:
            if left.id >= right.id:
                continue
            overlaps = (
                left.start_frame < right.end_frame_exclusive
                and right.start_frame < left.end_frame_exclusive
            )
            if overlaps:
                assert left.z != right.z, f"{left.id} and {right.id} share z={left.z}"


def test_lifecycle_windows_are_carried_as_half_open(program: StateProgram) -> None:
    """entry readable_blur [1, 3] inclusive -> [1, 4); first_crisp is a frame index."""
    opening = program.state("open-a")
    assert opening.lifecycle.first_detectable == 0
    assert opening.lifecycle.readable_blur == (1, 4)
    assert opening.lifecycle.first_crisp == 4
    assert opening.lifecycle.hold == (5, 20)


def test_exit_blur_is_carried_when_present(program: StateProgram) -> None:
    """exit progressive_blur [53, 59] inclusive -> [53, 60)."""
    final = program.state("end")
    assert final.lifecycle.exit_progressive_blur == (53, 60)
    assert final.lifecycle.hold == (45, 53)


def test_mid_blur_out_is_half_open(program: StateProgram) -> None:
    """progressive_blur [25, 27] inclusive -> [25, 28)."""
    assert program.state("mid").lifecycle.exit_progressive_blur == (25, 28)


def test_font_class_prose_is_never_promoted_to_a_face(program: StateProgram) -> None:
    """font_class is a description; the sealed authority sets both exact faces to null."""
    opening = program.state("open-a")
    assert "script" in opening.font_class
    assert opening.proven_face is None
    assert program.sans_exact_face is None
    assert program.script_exact_face is None


def test_program_records_the_authority_hash_it_came_from(program: StateProgram) -> None:
    assert program.authority_decoded_rgb24_sha256 == SYNTHETIC_SHA
    assert program.frame_count == 60
    assert program.raster == (1080, 1920)
    assert program.fps == "30/1"


def test_state_running_past_the_clock_is_rejected(authority: dict) -> None:
    tampered = json.loads(json.dumps(authority))
    tampered["visual_states"][-1]["end"] = 60
    with pytest.raises(StateProgramError, match="clock|60|frame_count"):
        import_sealed_authority(tampered)


def test_render_is_not_authorised_by_importing_a_program(program: StateProgram) -> None:
    assert program.render_allowed is False


def test_forbidden_treatments_are_carried_forward(program: StateProgram) -> None:
    """The sealed treatment_contract.forbidden list must survive into the program."""
    assert "per-word x/y envelope stretching" in program.forbidden_treatments
    assert any("Poppins" in item for item in program.forbidden_treatments)


def test_program_is_json_round_trippable(program: StateProgram) -> None:
    payload = program.to_dict()
    assert payload["frame_count"] == 60
    assert len(payload["states"]) == 6
    assert payload["blank_frames"] == [28, 29]
    assert json.loads(json.dumps(payload)) == payload


REAL_AUTHORITY = os.environ.get("REEL_FACTORY_CAPTION_AUTHORITY", "")


@pytest.mark.skipif(
    not (REAL_AUTHORITY and Path(REAL_AUTHORITY).is_file()),
    reason="no real sealed authority configured (REEL_FACTORY_CAPTION_AUTHORITY)",
)
def test_a_real_sealed_authority_imports_gap_consistently() -> None:
    """Structural invariants only; real words and hashes are project data."""
    program = import_sealed_authority(json.loads(Path(REAL_AUTHORITY).read_text()))
    assert program.states
    assert program.render_allowed is False
    for state in program.states:
        assert 0 <= state.start_frame < state.end_frame_exclusive <= program.frame_count
