"""Per-layer status, computed at read time from receipts. The honesty core.

Five values, no sixth, and no aggregate that can stand in for them.
``PROVEN`` is reachable through exactly one path: ``ProjectState.verify_stage`` returning
without raising, right now, on this call. There is no cached flag anywhere in this
module, which is why a tampered receipt byte turns a layer red on the next page load
rather than staying green until someone reruns QC.

Layer-to-stage mapping: eleven layers are a stage each. Three are sub-layers with their
own evidence file on top of a backing stage — colour/grade proof reads the locked
selection's per-slot proofs, captions & typography reads the caption contract's per-state
font identity, audio/beat parity reads the structure QC report's beat section. The
``DELIVERED`` stage has no layer: v1 publishing is manual and outside the app.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..hashing import sha256_file
from ..paths import PathSafetyError, confined_path
from ..state import STAGES, ProjectState, StateError
from .stages import call_gate_index

PROVEN = "PROVEN"
WITHHELD = "WITHHELD"
PENDING_HUMAN = "PENDING_HUMAN"
PENDING_MACHINE = "PENDING_MACHINE"
FAILED = "FAILED"
STATUS_VALUES = (PROVEN, WITHHELD, PENDING_HUMAN, PENDING_MACHINE, FAILED)

# Weakest first. FAILED leads because it is the alarm. PENDING_MACHINE is weaker than
# WITHHELD because "not attempted" carries less evidence than "attempted, refused to
# claim, named why" — so a reel that has not started headlines as unstarted rather than
# as a refusal it never made.
SEVERITY = (FAILED, PENDING_MACHINE, WITHHELD, PENDING_HUMAN, PROVEN)

NO_EVIDENCE = "no evidence recorded"
AWAITING_OPERATOR = "AWAITING_OPERATOR"
PROVEN_FACE_STATUSES = frozenset({"HOLDOUT_PROVEN", "ORIGINAL_ASSET_PROVEN"})
REVIEWED_GRADE_STATUSES = frozenset({"PASS", "AGENT_REVIEWED"})
VERIFIED_PROFILE_STATUS = "AGENT_VERIFIED"
SELECTION_EVIDENCE = "edit/selection.locked.json"
CAPTION_EVIDENCE_GLOB = "captions-*.json"
CAPTION_ARTIFACT_TYPE = "caption_contract"
AUDIO_CHECKS = (
    "reference_audio_timeline_identity",
    "presentation_clock_identity",
    "hard_cuts_within_two_frames_of_reference_onsets",
)


@dataclass(frozen=True)
class LayerSpec:
    key: str
    title: str
    stage: str
    evidence: Optional[str] = None


LAYERS: Tuple[LayerSpec, ...] = (
    LayerSpec("reference_lock", "Reference lock", "REFERENCE_LOCKED"),
    LayerSpec("blueprint", "Blueprint", "BLUEPRINT_LOCKED"),
    LayerSpec("footage_index", "Footage index", "FOOTAGE_INDEXED"),
    LayerSpec("feasibility", "Feasibility", "FEASIBILITY_REPORTED"),
    LayerSpec("selection", "Selection", "SELECTION_LOCKED"),
    LayerSpec("colour_grade_proof", "Colour/grade proof", "SELECTION_LOCKED", evidence="colour"),
    LayerSpec("captions_typography", "Captions & typography", "ASSETS_LOCKED", evidence="captions"),
    LayerSpec("audio_beat_parity", "Audio/beat parity", "STRUCTURE_QC", evidence="audio"),
    LayerSpec("render", "Render", "RENDERED"),
    LayerSpec("technical_qc", "Technical QC", "TECHNICAL_QC"),
    LayerSpec("structure_qc", "Structure QC", "STRUCTURE_QC"),
    LayerSpec("visual_qc", "Visual QC (reference-relative)", "VISUAL_QC"),
    LayerSpec("human_approval", "Human approval", "HUMAN_APPROVED"),
    LayerSpec("delivery_approval", "Delivery approval", "DELIVERY_APPROVED"),
)


def weakest(values: Iterable[str]) -> str:
    ranked = [value for value in values if value in SEVERITY]
    if not ranked:
        return PENDING_MACHINE
    return min(ranked, key=SEVERITY.index)


def headline_status(layers: Sequence[Mapping[str, Any]]) -> str:
    return weakest(layer["status"] for layer in layers)


def _reason(*parts: Optional[str]) -> Optional[str]:
    seen: List[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return "; ".join(seen) or None


def _read_json(project_dir: Path, relative: str) -> Tuple[Optional[Any], Optional[Path], Optional[str]]:
    try:
        path = confined_path(project_dir, relative, require="file", allow_missing=False)
    except PathSafetyError:
        return None, None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), path, None
    except (OSError, ValueError) as exc:
        return None, path, str(exc)


def _bound_outputs(stage: Mapping[str, Any]) -> List[str]:
    return [str(receipt.get("path")) for receipt in stage.get("output_receipts") or []]


def _evidence(relative: str, path: Optional[Path], stage_name: str, stage: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "path": relative,
        "sha256": sha256_file(path) if path is not None and path.is_file() else None,
        "receipt_bound": relative in _bound_outputs(stage),
        "stage": stage_name,
    }


def _verified(state: ProjectState, stage_name: str) -> bool:
    try:
        state.verify_stage(stage_name)
    except StateError:
        return False
    return True


def _stage_status(state: ProjectState, stage_name: str) -> Tuple[str, Optional[str]]:
    stage = state.stage(stage_name)
    status = stage.get("status")
    if status == "PASS":
        try:
            state.verify_stage(stage_name)
        except StateError as exc:
            return FAILED, f"stage receipt no longer verifies: {exc}"
        return PROVEN, None
    if status == "BLOCKED":
        return WITHHELD, stage.get("reason") or f"stage {stage_name} is blocked without a recorded reason"
    if status in {"FAIL", "STALE"}:
        return FAILED, stage.get("reason") or f"stage {stage_name} is {status}"
    if status == "PENDING":
        return PENDING_MACHINE, stage.get("reason")
    return FAILED, f"stage {stage_name} has an unrecognised status {status!r}"


# --- sub-layer evidence readers --------------------------------------------


def _colour_evidence(project_dir: Path, state: ProjectState, spec: LayerSpec) -> Tuple[str, Optional[str], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    stage = state.stage(spec.stage)
    payload, path, error = _read_json(project_dir, SELECTION_EVIDENCE)
    evidence = _evidence(SELECTION_EVIDENCE, path, spec.stage, stage) if path is not None else None
    if error is not None:
        return FAILED, f"{SELECTION_EVIDENCE} is unreadable: {error}", [], evidence
    slots = (payload or {}).get("slots") if isinstance(payload, dict) else None
    if not slots:
        return WITHHELD, NO_EVIDENCE, [], evidence
    items: List[Dict[str, Any]] = []
    unproven: List[str] = []
    for index, slot in enumerate(slots, start=1):
        block = str(slot.get("block_id") or f"slot-{index}")
        profile_status = str((slot.get("profile_proof") or {}).get("status"))
        grade_status = str((slot.get("grade_proof") or {}).get("status"))
        missing = []
        if profile_status != VERIFIED_PROFILE_STATUS:
            missing.append(f"input profile proof is {profile_status}")
        if grade_status not in REVIEWED_GRADE_STATUSES:
            missing.append(f"source-aware grade proof is {grade_status}")
        if missing:
            unproven.append(block)
        items.append(
            {
                "id": block,
                "status": WITHHELD if missing else PROVEN,
                "reason": "; ".join(missing) or None,
                "input_profile": slot.get("input_profile"),
            }
        )
    if evidence is not None and not evidence["receipt_bound"]:
        return WITHHELD, f"{SELECTION_EVIDENCE} is present but not bound to the {spec.stage} receipt", items, evidence
    if unproven:
        return WITHHELD, f"colour proof is unproven for {', '.join(unproven)}", items, evidence
    return PROVEN, None, items, evidence


def _caption_contracts(project_dir: Path) -> List[Tuple[str, Any, Path]]:
    assets = project_dir / "assets"
    if not assets.is_dir():
        return []
    found: List[Tuple[str, Any, Path]] = []
    for candidate in sorted(assets.glob(CAPTION_EVIDENCE_GLOB)):
        relative = f"assets/{candidate.name}"
        payload, path, error = _read_json(project_dir, relative)
        if error is not None or not isinstance(payload, dict):
            continue
        if payload.get("artifact_type") == CAPTION_ARTIFACT_TYPE:
            found.append((relative, payload, path))
    return found


def _caption_evidence(project_dir: Path, state: ProjectState, spec: LayerSpec) -> Tuple[str, Optional[str], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    stage = state.stage(spec.stage)
    contracts = _caption_contracts(project_dir)
    if not contracts:
        return WITHHELD, NO_EVIDENCE, [], None
    if len(contracts) > 1:
        names = ", ".join(relative for relative, _, _ in contracts)
        return WITHHELD, f"{len(contracts)} caption contracts are present ({names}); the studio will not guess which one governs", [], None
    relative, payload, path = contracts[0]
    evidence = _evidence(relative, path, spec.stage, stage)
    states = payload.get("states") or []
    if not states:
        return WITHHELD, NO_EVIDENCE, [], evidence
    hypotheses: Dict[str, List[Mapping[str, Any]]] = {}
    for hypothesis in payload.get("font_hypotheses") or []:
        hypotheses.setdefault(str(hypothesis.get("style_id")), []).append(hypothesis)
    items: List[Dict[str, Any]] = []
    unproven: List[str] = []
    for caption in states:
        caption_id = str(caption.get("id"))
        style_id = str(caption.get("style_id"))
        candidates = hypotheses.get(style_id, [])
        proven = [item for item in candidates if item.get("identity_status") in PROVEN_FACE_STATUSES]
        if proven:
            items.append(
                {
                    "id": caption_id,
                    "style_id": style_id,
                    "status": PROVEN,
                    "reason": None,
                    "identity_status": str(proven[0].get("identity_status")),
                }
            )
            continue
        statuses = sorted({str(item.get("identity_status")) for item in candidates})
        reason = (
            f"font identity for style {style_id} is {', '.join(statuses)}"
            if statuses
            else f"no font hypothesis recorded for style {style_id}"
        )
        unproven.append(caption_id)
        items.append(
            {
                "id": caption_id,
                "style_id": style_id,
                "status": WITHHELD,
                "reason": reason,
                "identity_status": statuses[0] if len(statuses) == 1 else None,
            }
        )
    if unproven:
        return WITHHELD, f"caption identity is unproven for {', '.join(unproven)}", items, evidence
    return PROVEN, None, items, evidence


def _audio_evidence(project_dir: Path, state: ProjectState, spec: LayerSpec) -> Tuple[str, Optional[str], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    stage = state.stage(spec.stage)
    relative = next((path for path in _bound_outputs(stage) if path.endswith(".json")), None)
    if relative is None:
        return WITHHELD, NO_EVIDENCE, [], None
    payload, path, error = _read_json(project_dir, relative)
    evidence = _evidence(relative, path, spec.stage, stage) if path is not None else None
    if error is not None:
        return FAILED, f"{relative} is unreadable: {error}", [], evidence
    structure = (payload or {}).get("structure") if isinstance(payload, dict) else None
    checks = structure.get("checks") if isinstance(structure, dict) else None
    if not isinstance(checks, dict):
        return WITHHELD, NO_EVIDENCE, [], evidence
    items: List[Dict[str, Any]] = []
    failing: List[str] = []
    unrecorded: List[str] = []
    for name in AUDIO_CHECKS:
        if name not in checks:
            unrecorded.append(name)
            items.append({"id": name, "status": WITHHELD, "reason": "not recorded in the structure QC report"})
        elif checks[name] is True:
            items.append({"id": name, "status": PROVEN, "reason": None})
        else:
            failing.append(name)
            items.append({"id": name, "status": FAILED, "reason": "structure QC recorded this check as false"})
    reason = _reason(
        f"structure QC reports {', '.join(failing)} as false" if failing else None,
        f"{', '.join(unrecorded)} not recorded" if unrecorded else None,
    )
    return weakest(item["status"] for item in items), reason, items, evidence


EVIDENCE_READERS = {"colour": _colour_evidence, "captions": _caption_evidence, "audio": _audio_evidence}


# --- assembly --------------------------------------------------------------


def _is_open(call: Mapping[str, Any]) -> bool:
    """Fail closed: a call blocks unless it says, in the registry's own word, that it doesn't.

    The daemon gates on ``state == "AWAITING_OPERATOR"``. A record with no state at all is
    treated as open here rather than ignored, so a schema that gains states later cannot
    quietly un-block a reel.
    """
    state = call.get("state")
    if state is None:
        return True
    return str(state).strip().upper() == AWAITING_OPERATOR


def _call_ids(calls: Sequence[Mapping[str, Any]]) -> List[str]:
    return [str(call.get("call_id") or "unnamed call") for call in calls]


def _calls_for(spec: LayerSpec, open_calls: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Calls that block this layer, mirroring the daemon's gate stage for stage.

    A call naming a ``layer`` blocks only that one. Otherwise the comparison is by stage
    position: the named stage and everything after it wait, so an open intake mode call
    (``SELECTION_LOCKED``) still lets the reference lock and feasibility run, exactly as
    §5.1 requires.

    The gate position comes from ``stages.call_gate_index``, the same function the daemon's
    SCHEDULE gate calls. It is shared rather than mirrored because this rule was once
    implemented twice and the copies diverged over a ``.strip()`` — invisibly, since each
    copy's own tests passed.
    """
    matched: List[Mapping[str, Any]] = []
    position = STAGES.index(spec.stage)
    for call in open_calls:
        if not _is_open(call):
            continue
        layer_target = call.get("layer")
        if layer_target:
            if str(layer_target) in {spec.key, spec.stage}:
                matched.append(call)
            continue
        gate = call_gate_index(call)
        if gate is None or position >= gate:
            matched.append(call)
    return matched


def layer_statuses(
    project_dir: Path,
    *,
    open_calls: Sequence[Mapping[str, Any]] = (),
) -> List[Dict[str, Any]]:
    project_dir = Path(project_dir)
    state = ProjectState.load(project_dir / "state.json")
    layers: List[Dict[str, Any]] = []
    for spec in LAYERS:
        stage = state.stage(spec.stage)
        status, reason = _stage_status(state, spec.stage)
        items: List[Dict[str, Any]] = []
        evidence: Optional[Dict[str, Any]] = None
        if spec.evidence is not None:
            evidence_status, evidence_reason, items, evidence = EVIDENCE_READERS[spec.evidence](project_dir, state, spec)
            status = weakest([status, evidence_status])
            reason = _reason(reason, evidence_reason)
        if status == PENDING_MACHINE:
            if spec.key == "human_approval" and _verified(state, "LOCAL_REVIEW_READY"):
                status, reason = PENDING_HUMAN, "local review is ready; a hash-bound human verdict is the only missing evidence"
            elif spec.key == "delivery_approval" and _verified(state, "HUMAN_APPROVED"):
                status, reason = PENDING_HUMAN, "human approval is recorded; the delivery decision is the only missing evidence"
        if status == PENDING_MACHINE:
            blocking = _calls_for(spec, open_calls)
            if blocking:
                status = PENDING_HUMAN
                named = ", ".join(_call_ids(blocking))
                issue = blocking[0].get("issue_type")
                detail = f"{named}: {issue}" if len(blocking) == 1 and issue else named
                reason = _reason(reason, f"awaiting the operator on {detail}")
        layers.append(
            {
                "key": spec.key,
                "title": spec.title,
                "stage": spec.stage,
                "status": status,
                "reason": reason,
                "stage_status": stage.get("status"),
                "receipt": stage.get("receipt"),
                "receipt_sha256": stage.get("receipt_sha256"),
                "updated_at_utc": stage.get("updated_at_utc"),
                "items": items,
                "evidence": evidence,
            }
        )
    return layers


def _unreadable_layers(reason: str) -> List[Dict[str, Any]]:
    return [
        {
            "key": spec.key,
            "title": spec.title,
            "stage": spec.stage,
            "status": FAILED,
            "reason": reason,
            "stage_status": None,
            "receipt": None,
            "receipt_sha256": None,
            "updated_at_utc": None,
            "items": [],
            "evidence": None,
        }
        for spec in LAYERS
    ]


def project_layers(project_dir: Path, *, open_calls: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    """Per-layer status for one project, fail-visible rather than fail-loud.

    A project whose state cannot be read is a board row of FAILED layers naming the
    error, not an exception that blanks the whole board.
    """
    project_dir = Path(project_dir)
    blocked = _call_ids([call for call in open_calls if _is_open(call)])
    try:
        state = ProjectState.load(project_dir / "state.json")
        project_id = str(state.data.get("project_id") or project_dir.name)
        layers = layer_statuses(project_dir, open_calls=open_calls)
    except (StateError, PathSafetyError, OSError, ValueError) as exc:
        reason = f"project state is unreadable: {exc}"
        return {
            "project_id": project_dir.name,
            "headline": FAILED,
            "reason": reason,
            "blocked_by_calls": blocked,
            "layers": _unreadable_layers(reason),
        }
    return {
        "project_id": project_id,
        "headline": headline_status(layers),
        "reason": None,
        "blocked_by_calls": blocked,
        "layers": layers,
    }
