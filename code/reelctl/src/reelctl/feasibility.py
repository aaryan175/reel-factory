from __future__ import annotations

from typing import Any, Dict, List, Optional


class FeasibilityError(ValueError):
    pass


COVERAGE_STATES = {"exact_scene_available", "role_equivalent_substitute", "missing"}


def feasibility_template(blueprint: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "DRAFT",
        "blocks": [
            {
                "block_id": block["id"],
                "reference_role": block.get("role", block.get("reference_observation", "UNOBSERVED")),
                "coverage": "PENDING_FULL_INVENTORY_REVIEW",
                "evidence": "PENDING_INDEPENDENT_EVIDENCE",
                "evidence_clip_ids": [],
                "decision": "PENDING_USER_OR_AGENT_DECISION",
            }
            for block in blueprint.get("picture_blocks", [])
        ],
    }


def validate_feasibility(
    blueprint: Dict[str, Any],
    manifest: Dict[str, Any],
    *,
    inventory: Optional[Dict[str, Any]] = None,
    mode: str = "reference-locked",
) -> Dict[str, Any]:
    expected = [str(block["id"]) for block in blueprint.get("picture_blocks", [])]
    rows = manifest.get("blocks", [])
    observed = [str(row.get("block_id", "")) for row in rows]
    if mode not in {"reference-locked", "original-montage"}:
        raise FeasibilityError(f"unsupported reel mode: {mode}")
    if observed != expected:
        raise FeasibilityError("feasibility rows must match the locked picture-state order exactly")
    inventory_clips = None
    if inventory is not None:
        if inventory.get("status") != "PASS":
            raise FeasibilityError("feasibility cannot be locked against an incomplete footage inventory")
        inventory_clips = {str(clip.get("clip_id")): clip for clip in inventory.get("clips", [])}
    missing: List[str] = []
    substitutions: List[str] = []
    exact: List[str] = []
    blueprint_by_id = {str(block["id"]): block for block in blueprint.get("picture_blocks", [])}
    for row in rows:
        block_id = str(row.get("block_id"))
        expected_role = str(blueprint_by_id[block_id].get("role", blueprint_by_id[block_id].get("reference_observation", "")))
        if str(row.get("reference_role", "")) != expected_role:
            raise FeasibilityError(f"{block_id} reference_role differs from the locked blueprint")
        coverage = str(row.get("coverage", ""))
        evidence = str(row.get("evidence", "")).strip()
        if coverage not in COVERAGE_STATES:
            raise FeasibilityError(f"{row.get('block_id')} has invalid coverage state {coverage!r}")
        if not evidence or evidence.upper().startswith("PENDING_") or evidence.upper().startswith("REPLACE_"):
            raise FeasibilityError(f"{row.get('block_id')} lacks independent feasibility evidence")
        clip_ids = [str(value) for value in row.get("evidence_clip_ids", [])]
        if coverage != "missing":
            if not clip_ids:
                raise FeasibilityError(f"{block_id} lacks clip-bound feasibility evidence")
            if inventory_clips is None:
                raise FeasibilityError("an exact footage inventory is required to validate non-missing feasibility coverage")
            unknown = [clip_id for clip_id in clip_ids if clip_id not in inventory_clips]
            if unknown:
                raise FeasibilityError(f"{block_id} cites clips outside the locked footage inventory: {unknown}")
        decision = str(row.get("decision", ""))
        if coverage == "role_equivalent_substitute":
            if decision != "APPROVED_ROLE_EQUIVALENT_SUBSTITUTE":
                raise FeasibilityError(f"{block_id} role-equivalent substitution lacks an explicit approval decision")
            substitutions.append(block_id)
        if coverage == "exact_scene_available":
            if decision != "EXACT_SCENE_SELECTED":
                raise FeasibilityError(f"{block_id} exact-scene coverage lacks an exact selection decision")
            exact.append(block_id)
        if coverage == "missing":
            if decision not in {"ACQUIRE_NEW_FOOTAGE", "CHANGE_REFERENCE_WITH_EXPLICIT_USER_APPROVAL"}:
                raise FeasibilityError(f"{block_id} missing coverage lacks a fail-closed next-step decision")
            missing.append(str(row["block_id"]))
    exact_coverage_ratio = len(exact) / max(len(rows), 1)
    minimum_exact_coverage = 0.80 if mode == "reference-locked" else 0.0
    mode_switch_required = mode == "reference-locked" and exact_coverage_ratio < minimum_exact_coverage
    blocked = bool(missing) or mode_switch_required
    return {
        "status": "BLOCKED" if blocked else "PASS",
        "mode": mode,
        "blocks": len(rows),
        "exact_blocks": exact,
        "exact_coverage_ratio": round(exact_coverage_ratio, 8),
        "minimum_exact_coverage": minimum_exact_coverage,
        "missing_blocks": missing,
        "substitution_blocks": substitutions,
        "decision_required": blocked,
        "mode_switch_required": mode_switch_required,
        "recommended_mode": "original-montage" if mode_switch_required else mode,
    }
