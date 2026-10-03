from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from reelctl.state import STAGES, ProjectState, StateError
from reelctl.studio.status import (
    FAILED,
    LAYERS,
    PENDING_HUMAN,
    PENDING_MACHINE,
    PROVEN,
    STATUS_VALUES,
    WITHHELD,
    headline_status,
    layer_statuses,
    project_layers,
    weakest,
)

PROJECT_DIRECTORIES = ("reference", "footage", "edit", "assets", "review", "deliver", ".reelctl")

# The paths a real reelctl run binds to each stage receipt (see cli.py). Everything the
# read model treats as receipt-bound evidence has to arrive this way in the fixtures too,
# or the fixtures would prove something the engine never proves.
QC_REPORT = "review/qc-v001/qc-report.json"
STAGE_OUTPUTS = {
    "SELECTION_LOCKED": ["edit/selection.locked.json"],
    "ASSETS_LOCKED": ["assets/assets.locked.json"],
    "TECHNICAL_QC": [QC_REPORT],
    "STRUCTURE_QC": [QC_REPORT],
    "VISUAL_QC": [QC_REPORT],
}


def _stage_outputs(stage: str) -> List[str]:
    return list(STAGE_OUTPUTS.get(stage, [f"edit/{stage.lower()}.json"]))


def _write(project: Path, relative: str, payload: Any) -> str:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload), encoding="utf-8")
    return relative


def _new_project(tmp_path: Path, name: str = "demo") -> Path:
    project = tmp_path / name
    for child in PROJECT_DIRECTORIES:
        (project / child).mkdir(parents=True, exist_ok=True)
    ProjectState.create(project / "state.json", name)
    return project


def _complete(
    project: Path,
    stage: str,
    *,
    outputs: Optional[Sequence[str]] = None,
    status: str = "PASS",
    reason: Optional[str] = None,
    input_hash: Optional[str] = None,
) -> None:
    chosen = _stage_outputs(stage) if outputs is None else list(outputs)
    for relative in chosen:
        if not (project / relative).exists():
            _write(project, relative, {"stage": stage})
    state = ProjectState.load(project / "state.json")
    state.complete(stage, input_hash or f"input-hash-{stage.lower()}", chosen, status=status, reason=reason)


def _advance_through(project: Path, last_stage: str) -> None:
    for stage in STAGES[: STAGES.index(last_stage) + 1]:
        _complete(project, stage)


def _by_key(layers: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {layer["key"]: layer for layer in layers}


def _statuses(project: Path) -> Dict[str, str]:
    return {layer["key"]: layer["status"] for layer in layer_statuses(project)}


def _colour_evidence(project: Path, *, proven: bool) -> None:
    slot = {
        "block_id": "B1",
        "frames": 12,
        "source_path": "/Volumes/WORKDRIVE/workbench/footage-library/clip.mp4",
        "source_start_frame": 0,
        "input_profile": "sony_slog3_sgamut3cine" if proven else "PENDING_PROFILE_IDENTIFICATION",
        "profile_proof": {
            "status": "AGENT_VERIFIED" if proven else "PENDING_PROFILE_IDENTIFICATION",
            "method": "camera_metadata",
        },
        "grade_proof": {"status": "AGENT_REVIEWED" if proven else "PENDING_SOURCE_AWARE_GRADE_REVIEW"},
    }
    _write(project, "edit/selection.locked.json", {"schema_version": 1, "slots": [slot]})


def _caption_contract(project: Path, *, proven: bool, relative: str = "assets/captions-v001.json") -> None:
    _write(
        project,
        relative,
        {
            "schema_version": 1,
            "artifact_type": "caption_contract",
            "status": "SEALED",
            "render_allowed": proven,
            "reference": {"frame_count": 90},
            "styles": {
                "T_sans": {"render_path": "native_font" if proven else "source_contour"},
                "T_script": {"render_path": "native_font" if proven else "source_contour"},
            },
            "states": [
                {"id": "have", "style_id": "T_sans", "text": "have", "start_frame": 41, "end_frame_exclusive": 45},
                {"id": "focus", "style_id": "T_script", "text": "focus", "start_frame": 50, "end_frame_exclusive": 60},
            ],
            "font_hypotheses": (
                [
                    {"style_id": "T_sans", "identity_status": "HOLDOUT_PROVEN", "family": "Example Sans Wide"},
                    {"style_id": "T_script", "identity_status": "ORIGINAL_ASSET_PROVEN", "family": "Example Script"},
                ]
                if proven
                else [{"style_id": "T_sans", "identity_status": "CANDIDATE", "family": "Example Sans Wide"}]
            ),
        },
    )


def _qc_report(project: Path, *, beats_aligned: bool) -> None:
    _write(
        project,
        QC_REPORT,
        {
            "schema_version": 1,
            "revision": "v001",
            "structure": {
                "status": "PASS" if beats_aligned else "FAIL",
                "checks": {
                    "presentation_clock_identity": True,
                    "reference_audio_timeline_identity": True,
                    "hard_cuts_within_two_frames_of_reference_onsets": beats_aligned,
                },
                "beat_alignment": [{"cut_frame": 24, "nearest_reference_audio_onset_frame": 24, "distance_frames": 0}],
            },
        },
    )


# --- vocabulary and shape --------------------------------------------------


def test_the_vocabulary_has_exactly_five_values_and_no_aggregate() -> None:
    assert set(STATUS_VALUES) == {PROVEN, WITHHELD, PENDING_HUMAN, PENDING_MACHINE, FAILED}
    assert len(STATUS_VALUES) == 5


def test_every_layer_in_the_architecture_is_present_and_ordered() -> None:
    assert [layer.key for layer in LAYERS] == [
        "reference_lock",
        "blueprint",
        "footage_index",
        "feasibility",
        "selection",
        "colour_grade_proof",
        "captions_typography",
        "audio_beat_parity",
        "render",
        "technical_qc",
        "structure_qc",
        "visual_qc",
        "human_approval",
        "delivery_approval",
    ]
    assert len(LAYERS) == 14


@pytest.mark.parametrize("layer", LAYERS, ids=lambda layer: layer.key)
def test_every_layer_declares_a_real_backing_stage(layer) -> None:
    assert layer.stage in STAGES
    assert layer.title


# --- the five values -------------------------------------------------------


def test_a_fresh_project_reads_pending_machine_never_blank_never_green(tmp_path: Path) -> None:
    project = _new_project(tmp_path)

    layers = layer_statuses(project)

    assert all(layer["status"] in STATUS_VALUES for layer in layers)
    assert not any(layer["status"] == PROVEN for layer in layers)
    assert _statuses(project)["reference_lock"] == PENDING_MACHINE
    assert headline_status(layers) == PENDING_MACHINE


def test_a_verified_stage_reads_proven(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "BLUEPRINT_LOCKED")

    layers = _by_key(layer_statuses(project))

    assert layers["reference_lock"]["status"] == PROVEN
    assert layers["blueprint"]["status"] == PROVEN
    assert layers["blueprint"]["reason"] is None
    assert layers["blueprint"]["receipt"].startswith(".reelctl/receipts/")
    assert layers["blueprint"]["receipt_sha256"]
    assert layers["footage_index"]["status"] == PENDING_MACHINE


def test_a_blocked_stage_reads_withheld_with_the_engine_reason_verbatim(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _complete(project, "REFERENCE_LOCKED")
    _complete(
        project,
        "BLUEPRINT_LOCKED",
        status="BLOCKED",
        reason="The reviewing agent must review all 90 reference frames before the blueprint can lock",
    )

    layer = _by_key(layer_statuses(project))["blueprint"]

    assert layer["status"] == WITHHELD
    assert layer["reason"] == "The reviewing agent must review all 90 reference frames before the blueprint can lock"


def test_a_blocked_stage_without_a_reason_still_says_so(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _complete(project, "REFERENCE_LOCKED", status="BLOCKED", reason=None)

    layer = _by_key(layer_statuses(project))["reference_lock"]

    assert layer["status"] == WITHHELD
    assert layer["reason"]


def test_a_failed_stage_reads_failed_with_its_reason(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _complete(project, "REFERENCE_LOCKED")
    _complete(project, "BLUEPRINT_LOCKED", status="FAIL", reason="boundary count disagrees with the reference")

    layer = _by_key(layer_statuses(project))["blueprint"]

    assert layer["status"] == FAILED
    assert layer["reason"] == "boundary count disagrees with the reference"


def test_a_stale_downstream_stage_reads_failed(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "FOOTAGE_INDEXED")

    _complete(project, "REFERENCE_LOCKED", outputs=["reference/reference-lock.json"], input_hash="a-new-reference-hash")

    layers = _by_key(layer_statuses(project))
    assert layers["reference_lock"]["status"] == PROVEN
    assert layers["blueprint"]["status"] == FAILED
    assert "upstream" in layers["blueprint"]["reason"]


def test_human_approval_reads_pending_human_once_local_review_is_ready(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "LOCAL_REVIEW_READY")

    layers = _by_key(layer_statuses(project))

    assert layers["human_approval"]["status"] == PENDING_HUMAN
    assert "human" in layers["human_approval"]["reason"].lower()
    assert layers["delivery_approval"]["status"] == PENDING_MACHINE


def test_human_approval_stays_pending_machine_before_local_review_is_ready(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "VISUAL_QC")

    assert _statuses(project)["human_approval"] == PENDING_MACHINE


def test_delivery_approval_waits_for_the_human_verdict_then_reads_pending_human(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "HUMAN_APPROVED")

    layers = _by_key(layer_statuses(project))

    assert layers["human_approval"]["status"] == PROVEN
    assert layers["delivery_approval"]["status"] == PENDING_HUMAN


def test_an_open_call_makes_its_layer_pending_human(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _complete(project, "REFERENCE_LOCKED")
    call = {"call_id": "call-0001", "stage": "BLUEPRINT_LOCKED", "issue_type": "mode-ambiguity"}

    layer = _by_key(layer_statuses(project, open_calls=[call]))["blueprint"]

    assert layer["status"] == PENDING_HUMAN
    assert "call-0001" in layer["reason"]
    assert "mode-ambiguity" in layer["reason"]


# Three registry call records shaped like real ones for one project.
# The daemon refuses to schedule this project; the board has to say the same thing.
SAMPLE_CALLS = [
    {
        "call_id": "CALL-001-missing-roles",
        "project": "reel-ref-28-v1",
        "card": "reel-ref-28-v1/brain/07_CALL_PROMPTS.md",
        "state": "AWAITING_OPERATOR",
    },
    {
        "call_id": "CALL-002-craft-quota",
        "project": "reel-ref-28-v1",
        "card": "reel-ref-28-v1/brain/07_CALL_PROMPTS.md",
        "state": "AWAITING_OPERATOR",
    },
    {
        "call_id": "CALL-003-grade-target",
        "project": "reel-ref-28-v1",
        "card": "reel-ref-28-v1/brain/07_CALL_PROMPTS.md",
        "state": "AWAITING_OPERATOR",
    },
]


def test_a_registry_call_with_no_stage_blocks_every_unstarted_layer(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "REFERENCE_LOCKED")

    layers = _by_key(layer_statuses(project, open_calls=SAMPLE_CALLS))

    assert layers["reference_lock"]["status"] == PROVEN
    assert layers["blueprint"]["status"] == PENDING_HUMAN
    assert layers["render"]["status"] == PENDING_HUMAN
    assert "CALL-001-missing-roles" in layers["blueprint"]["reason"]
    assert "CALL-003-grade-target" in layers["blueprint"]["reason"]


def test_a_project_with_open_calls_reads_pending_human_not_pending_machine(tmp_path: Path) -> None:
    project = _new_project(tmp_path, name="reel-ref-28-v1")

    model = project_layers(project, open_calls=SAMPLE_CALLS)

    assert model["headline"] == PENDING_HUMAN
    assert model["blocked_by_calls"] == [
        "CALL-001-missing-roles",
        "CALL-002-craft-quota",
        "CALL-003-grade-target",
    ]


def test_a_project_with_no_open_calls_reports_none_blocking(tmp_path: Path) -> None:
    project = _new_project(tmp_path)

    model = project_layers(project)

    assert model["blocked_by_calls"] == []
    assert model["headline"] == PENDING_MACHINE


def test_an_answered_call_stops_blocking(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    answered = [dict(call, state="ANSWERED") for call in SAMPLE_CALLS]

    model = project_layers(project, open_calls=answered)

    assert model["blocked_by_calls"] == []
    assert model["headline"] == PENDING_MACHINE


def test_a_call_with_no_state_is_treated_as_open(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    stateless = [{"call_id": "CALL-009", "project": project.name}]

    model = project_layers(project, open_calls=stateless)

    assert model["blocked_by_calls"] == ["CALL-009"]


@pytest.mark.parametrize("gate_stage", STAGES)
def test_a_stage_scoped_call_gates_that_stage_onward_and_nothing_before_it(tmp_path: Path, gate_stage: str) -> None:
    # registry daemon_contract.calls_gate_design: a card names the first stage its decision
    # governs; stages before it proceed, the named stage onward waits. Walking all fourteen
    # stages is the only way an off-by-one at the boundary shows up.
    project = _new_project(tmp_path)
    call = {"call_id": "CALL-STAGE", "project": project.name, "state": "AWAITING_OPERATOR", "stage": gate_stage}
    gate = STAGES.index(gate_stage)

    layers = layer_statuses(project, open_calls=[call])

    for layer in layers:
        expected = PENDING_HUMAN if STAGES.index(layer["stage"]) >= gate else PENDING_MACHINE
        assert layer["status"] == expected, f"{layer['key']} ({layer['stage']}) with the gate at {gate_stage}"


def test_the_intake_mode_call_leaves_reference_blueprint_footage_and_feasibility_free(tmp_path: Path) -> None:
    # registry daemon_contract.intake_mode_call_stage = SELECTION_LOCKED, because the mode
    # decision needs feasibility numbers to be answerable.
    project = _new_project(tmp_path)
    call = {"call_id": "CALL-MODE", "project": project.name, "state": "AWAITING_OPERATOR", "stage": "SELECTION_LOCKED"}

    layers = _by_key(layer_statuses(project, open_calls=[call]))

    assert layers["reference_lock"]["status"] == PENDING_MACHINE
    assert layers["blueprint"]["status"] == PENDING_MACHINE
    assert layers["footage_index"]["status"] == PENDING_MACHINE
    assert layers["feasibility"]["status"] == PENDING_MACHINE
    assert layers["selection"]["status"] == PENDING_HUMAN
    assert layers["colour_grade_proof"]["status"] == PENDING_HUMAN
    assert layers["render"]["status"] == PENDING_HUMAN


def test_a_call_naming_a_stage_this_build_does_not_know_gates_everything(tmp_path: Path) -> None:
    # An unrecognised gate must never read as an open door.
    project = _new_project(tmp_path)
    call = {"call_id": "CALL-FUTURE", "project": project.name, "state": "AWAITING_OPERATOR", "stage": "SOME_FUTURE_STAGE"}

    statuses = {layer["status"] for layer in layer_statuses(project, open_calls=[call])}

    assert statuses == {PENDING_HUMAN}


def test_a_call_with_no_stage_still_gates_everything(tmp_path: Path) -> None:
    project = _new_project(tmp_path)

    statuses = {layer["status"] for layer in layer_statuses(project, open_calls=SAMPLE_CALLS)}

    assert statuses == {PENDING_HUMAN}


def test_the_shared_call_gate_contract_the_board_depends_on() -> None:
    # The board and the daemon's SCHEDULE gate call this same function — it is shared
    # rather than mirrored, because the mirrored version diverged over a .strip() and each
    # copy's own tests still passed. This asserts the contract from the consumer side, so a
    # semantic change to the shared rule fails here as well as in the daemon's suite.
    from reelctl.studio.stages import call_gate_index

    assert call_gate_index({"stage": "SELECTION_LOCKED"}) == STAGES.index("SELECTION_LOCKED")
    assert call_gate_index({"stage": STAGES[0]}) == 0
    assert call_gate_index({"stage": "  SELECTION_LOCKED  "}) == STAGES.index("SELECTION_LOCKED")
    # Everything it cannot place gates the whole reel: an unrecognised gate is never an open door.
    assert call_gate_index({}) is None
    assert call_gate_index({"stage": None}) is None
    assert call_gate_index({"stage": ""}) is None
    assert call_gate_index({"stage": "NOT_A_STAGE"}) is None
    assert call_gate_index({"stage": "selection_locked"}) is None
    assert call_gate_index({"stage": 7}) is None


def test_a_project_wide_call_never_overrides_harder_evidence(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _complete(project, "REFERENCE_LOCKED")
    _complete(project, "BLUEPRINT_LOCKED", status="FAIL", reason="boundary count disagrees")

    layers = _by_key(layer_statuses(project, open_calls=SAMPLE_CALLS))

    assert layers["reference_lock"]["status"] == PROVEN
    assert layers["blueprint"]["status"] == FAILED
    assert headline_status(list(layers.values())) == FAILED


def test_an_open_call_never_overrides_harder_evidence(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _complete(project, "REFERENCE_LOCKED")
    _complete(project, "BLUEPRINT_LOCKED", status="FAIL", reason="boundary count disagrees")
    call = {"call_id": "call-0002", "stage": "BLUEPRINT_LOCKED", "issue_type": "mode-ambiguity"}

    layer = _by_key(layer_statuses(project, open_calls=[call]))["blueprint"]

    assert layer["status"] == FAILED


# --- the tamper gate -------------------------------------------------------


def test_tampering_with_one_receipt_byte_flips_the_layer_from_proven_to_failed(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "FOOTAGE_INDEXED")
    assert _statuses(project)["blueprint"] == PROVEN
    state = ProjectState.load(project / "state.json")
    receipt = project / str(state.stage("BLUEPRINT_LOCKED")["receipt"])
    raw = bytearray(receipt.read_bytes())
    raw[-3] = raw[-3] ^ 0x01

    receipt.write_bytes(bytes(raw))

    layers = _by_key(layer_statuses(project))
    assert layers["blueprint"]["status"] == FAILED
    assert "receipt" in layers["blueprint"]["reason"]
    assert layers["footage_index"]["status"] == FAILED
    assert layers["reference_lock"]["status"] == PROVEN


def test_tampering_with_a_stage_artifact_flips_the_layer_to_failed(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "BLUEPRINT_LOCKED")
    assert _statuses(project)["blueprint"] == PROVEN

    (project / "edit/blueprint_locked.json").write_text("rewritten after the receipt was signed", encoding="utf-8")

    assert _statuses(project)["blueprint"] == FAILED


# --- the spy gate: PROVEN may never come from a cached flag -----------------


def test_every_proven_layer_called_verify_stage_for_its_backing_stage(tmp_path: Path, monkeypatch) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "SELECTION_LOCKED")
    seen: List[str] = []
    original = ProjectState.verify_stage

    def spy(self: ProjectState, name: str, **kwargs: Any) -> Dict[str, Any]:
        seen.append(name)
        return original(self, name, **kwargs)

    monkeypatch.setattr(ProjectState, "verify_stage", spy)

    proven = [layer for layer in layer_statuses(project) if layer["status"] == PROVEN]

    assert proven, "the fixture must prove at least one layer or this test proves nothing"
    for layer in proven:
        assert layer["stage"] in seen


def test_no_layer_can_report_proven_when_verification_is_refused(tmp_path: Path, monkeypatch) -> None:
    project = _new_project(tmp_path)
    _colour_evidence(project, proven=True)
    _caption_contract(project, proven=True)
    _advance_through(project, "ASSETS_LOCKED")
    assert PROVEN in _statuses(project).values()

    def refuse(self: ProjectState, name: str) -> Dict[str, Any]:
        raise StateError(f"stage {name} receipt changed after completion")

    monkeypatch.setattr(ProjectState, "verify_stage", refuse)
    after = _statuses(project)

    assert PROVEN not in after.values()
    assert after["reference_lock"] == FAILED
    assert after["colour_grade_proof"] == FAILED
    assert after["captions_typography"] == FAILED


# --- headline --------------------------------------------------------------


def test_headline_is_the_weakest_layer(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "LOCAL_REVIEW_READY")
    assert headline_status(layer_statuses(project)) == PENDING_MACHINE

    _complete(project, "REFERENCE_LOCKED", status="FAIL", reason="reference bytes changed", input_hash="rehashed")

    assert headline_status(layer_statuses(project)) == FAILED


def test_weakest_orders_failed_worst_and_proven_best() -> None:
    assert weakest([PROVEN, PENDING_HUMAN, WITHHELD, PENDING_MACHINE, FAILED]) == FAILED
    assert weakest([PROVEN, PENDING_HUMAN, WITHHELD, PENDING_MACHINE]) == PENDING_MACHINE
    assert weakest([PROVEN, PENDING_HUMAN, WITHHELD]) == WITHHELD
    assert weakest([PROVEN, PENDING_HUMAN]) == PENDING_HUMAN
    assert weakest([PROVEN, PROVEN]) == PROVEN
    assert weakest([]) == PENDING_MACHINE


def test_headline_of_a_fully_proven_reel_is_proven(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _colour_evidence(project, proven=True)
    _caption_contract(project, proven=True)
    _qc_report(project, beats_aligned=True)
    _advance_through(project, "DELIVERY_APPROVED")

    layers = layer_statuses(project)

    assert headline_status(layers) == PROVEN, {layer["key"]: (layer["status"], layer["reason"]) for layer in layers}


# --- sub-layers: colour, captions, audio -----------------------------------


def test_a_sub_layer_without_evidence_is_withheld_by_name_never_blank(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _advance_through(project, "STRUCTURE_QC")

    layers = _by_key(layer_statuses(project))

    for key in ("colour_grade_proof", "captions_typography", "audio_beat_parity"):
        assert layers[key]["status"] == WITHHELD
        assert layers[key]["reason"] == "no evidence recorded"


def test_colour_proof_reads_proven_only_when_every_slot_carries_both_proofs(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _colour_evidence(project, proven=True)
    _advance_through(project, "SELECTION_LOCKED")

    layer = _by_key(layer_statuses(project))["colour_grade_proof"]

    assert layer["status"] == PROVEN
    assert layer["items"] == [{"id": "B1", "status": PROVEN, "reason": None, "input_profile": "sony_slog3_sgamut3cine"}]
    assert layer["evidence"]["path"] == "edit/selection.locked.json"
    assert layer["evidence"]["receipt_bound"] is True


def test_colour_proof_is_withheld_and_names_the_unproven_slot(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _colour_evidence(project, proven=False)
    _advance_through(project, "SELECTION_LOCKED")

    layer = _by_key(layer_statuses(project))["colour_grade_proof"]

    assert layer["status"] == WITHHELD
    assert "B1" in layer["reason"]
    assert layer["items"][0]["status"] == WITHHELD
    assert "profile" in layer["items"][0]["reason"]


def test_colour_proof_is_withheld_when_its_evidence_is_not_bound_to_the_stage_receipt(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _colour_evidence(project, proven=True)
    _advance_through(project, "FEASIBILITY_REPORTED")
    _complete(project, "SELECTION_LOCKED", outputs=["edit/selection-somewhere-else.json"])

    layer = _by_key(layer_statuses(project))["colour_grade_proof"]

    assert layer["status"] == WITHHELD
    assert "not bound" in layer["reason"]
    assert layer["evidence"]["receipt_bound"] is False


def test_caption_states_are_proven_or_withheld_one_row_each(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _caption_contract(project, proven=False)
    _advance_through(project, "ASSETS_LOCKED")

    layer = _by_key(layer_statuses(project))["captions_typography"]

    assert layer["status"] == WITHHELD
    assert [item["id"] for item in layer["items"]] == ["have", "focus"]
    assert all(item["status"] == WITHHELD for item in layer["items"])
    assert "CANDIDATE" in layer["items"][0]["reason"]
    assert "no font hypothesis" in layer["items"][1]["reason"]
    assert "focus" in layer["reason"]


def test_caption_layer_is_proven_when_every_state_has_a_proven_face(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _caption_contract(project, proven=True)
    _advance_through(project, "ASSETS_LOCKED")

    layer = _by_key(layer_statuses(project))["captions_typography"]

    assert layer["status"] == PROVEN
    assert [item["identity_status"] for item in layer["items"]] == ["HOLDOUT_PROVEN", "ORIGINAL_ASSET_PROVEN"]
    assert layer["evidence"]["receipt_bound"] is False
    assert layer["evidence"]["sha256"]


def test_two_caption_contracts_are_refused_rather_than_guessed(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _caption_contract(project, proven=True)
    _caption_contract(project, proven=True, relative="assets/captions-v002.json")
    _advance_through(project, "ASSETS_LOCKED")

    layer = _by_key(layer_statuses(project))["captions_typography"]

    assert layer["status"] == WITHHELD
    assert "captions-v001.json" in layer["reason"]
    assert "captions-v002.json" in layer["reason"]


def test_audio_beat_parity_reads_the_structure_qc_report(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _qc_report(project, beats_aligned=True)
    _advance_through(project, "STRUCTURE_QC")

    layer = _by_key(layer_statuses(project))["audio_beat_parity"]

    assert layer["status"] == PROVEN
    assert {item["id"] for item in layer["items"]} == {
        "reference_audio_timeline_identity",
        "presentation_clock_identity",
        "hard_cuts_within_two_frames_of_reference_onsets",
    }
    assert layer["evidence"]["path"] == QC_REPORT
    assert layer["evidence"]["receipt_bound"] is True


def test_audio_beat_parity_fails_and_names_the_failing_check(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _qc_report(project, beats_aligned=False)
    _advance_through(project, "TECHNICAL_QC")
    _complete(project, "STRUCTURE_QC", status="FAIL", reason="beat alignment is outside two frames")

    layer = _by_key(layer_statuses(project))["audio_beat_parity"]

    assert layer["status"] == FAILED
    assert "hard_cuts_within_two_frames_of_reference_onsets" in layer["reason"]


def test_a_sub_layer_cannot_outrank_its_unverified_backing_stage(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _colour_evidence(project, proven=True)
    _caption_contract(project, proven=True)

    layers = _by_key(layer_statuses(project))

    assert layers["colour_grade_proof"]["status"] == PENDING_MACHINE
    assert layers["captions_typography"]["status"] == PENDING_MACHINE


# --- project-level model ---------------------------------------------------


def test_project_layers_reports_id_headline_and_layers(tmp_path: Path) -> None:
    project = _new_project(tmp_path, name="reel-demo-v1")
    _advance_through(project, "BLUEPRINT_LOCKED")

    model = project_layers(project)

    assert model["project_id"] == "reel-demo-v1"
    assert len(model["layers"]) == 14
    assert model["headline"] == headline_status(model["layers"]) == PENDING_MACHINE


def test_an_unreadable_state_file_reads_failed_rather_than_raising(tmp_path: Path) -> None:
    project = _new_project(tmp_path, name="broken")
    (project / "state.json").write_text("{not json", encoding="utf-8")

    model = project_layers(project)

    assert model["headline"] == FAILED
    assert all(layer["status"] == FAILED for layer in model["layers"])
    assert model["reason"]


def test_the_read_model_is_deterministic_across_calls(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _colour_evidence(project, proven=True)
    _advance_through(project, "SELECTION_LOCKED")

    assert project_layers(project) == project_layers(project)
