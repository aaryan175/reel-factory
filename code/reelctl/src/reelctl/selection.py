from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

from .hashing import recipe_hash, sha256_file
from .paths import PathSafetyError, canonical_root, confined_path


class SelectionError(ValueError):
    pass


_PLACEHOLDER_PREFIXES = ("REPLACE_", "PENDING_", "UNOBSERVED")


def _inventory_map(inventory: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if inventory.get("status") != "PASS":
        raise SelectionError("footage inventory is not complete/PASS")
    if int(inventory.get("expected_count", -1)) != int(inventory.get("passed_count", -2)):
        raise SelectionError("footage inventory counts do not prove complete coverage")
    if any(int(inventory.get(key, -1)) != 0 for key in ("failed_count", "missing_count")):
        raise SelectionError("footage inventory has failed or missing entries")
    expected_recipe = recipe_hash({key: value for key, value in inventory.items() if key != "recipe_hash"})
    if inventory.get("recipe_hash") != expected_recipe:
        raise SelectionError("footage inventory recipe hash is invalid")
    try:
        root = canonical_root(Path(str(inventory["root"])))
        result = {}
        for item in inventory.get("clips", []):
            if item.get("status", "PASS") != "PASS":
                continue
            path = confined_path(root, Path(str(item["path"])), require="file", allow_missing=False)
            if str(path.relative_to(root)) != str(item.get("relative_path")):
                raise SelectionError(f"inventory relative path no longer matches authorized root: {path}")
            result[str(path)] = item
        return result
    except (KeyError, PathSafetyError) as exc:
        raise SelectionError(f"footage inventory path confinement failed: {exc}") from exc


def bind_selection_to_inventory(selection: Dict[str, Any], inventory: Dict[str, Any]) -> Dict[str, Any]:
    by_path = _inventory_map(inventory)
    bound = dict(selection)
    slots = []
    hash_cache: Dict[str, str] = {}
    for index, raw in enumerate(selection.get("slots", selection.get("shots", [])), start=1):
        slot = dict(raw)
        source = Path(str(slot.get("source_path", ""))).expanduser().absolute()
        record = by_path.get(str(source))
        if record is None:
            raise SelectionError(f"slot {index} source is not a probed member of the authorized footage inventory: {source}")
        digest = hash_cache.setdefault(str(source), sha256_file(source))
        if digest != record["sha256"]:
            raise SelectionError(f"slot {index} source changed after inventory: {source}")
        slot.update(
            {
                "source_path": str(source),
                "source_sha256": digest,
                "source_bytes": int(record["bytes"]),
                "source_clip_id": record["clip_id"],
                "source_frame_count": int(record["video"]["frame_count"]),
                "source_fps": record["video"]["r_frame_rate"],
            }
        )
        slots.append(slot)
    bound["slots"] = slots
    bound["status"] = "LOCKED_TO_INVENTORY"
    bound["inventory_recipe_hash"] = inventory.get("recipe_hash")
    return bound


def validate_selection(
    blueprint: Dict[str, Any],
    selection: Dict[str, Any],
    *,
    mode: str,
    inventory: Optional[Dict[str, Any]] = None,
    feasibility: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    blocks = blueprint.get("picture_blocks", [])
    slots = selection.get("slots", selection.get("shots", []))
    if not blocks:
        raise SelectionError("blueprint has no picture blocks")
    if not slots:
        raise SelectionError("selection has no slots")
    if mode == "reference-locked" and len(slots) != len(blocks):
        raise SelectionError(f"reference-locked mode requires {len(blocks)} slots; got {len(slots)}")
    by_inventory_path = _inventory_map(inventory) if inventory is not None else None
    feasibility_by_id = None
    if mode == "reference-locked" and inventory is not None:
        if feasibility is None:
            raise SelectionError("reference-locked selection requires the locked feasibility decision map")
        feasibility_by_id = {str(row.get("block_id")): row for row in feasibility.get("blocks", [])}
        if list(feasibility_by_id) != [str(block.get("id")) for block in blocks]:
            raise SelectionError("feasibility map does not match the ordered reference blocks")
    block_by_id = {str(block.get("id")): block for block in blocks}
    observed_ids: List[str] = []
    total = 0
    output_fps = Fraction(str(blueprint.get("clock", {}).get("fps", "1/1")))
    hash_cache: Dict[str, str] = {}
    for index, slot in enumerate(slots):
        block_id = str(slot.get("block_id") or slot.get("slot_id") or slot.get("id") or "")
        if not block_id and index < len(blocks):
            block_id = str(blocks[index].get("id"))
        if block_id not in block_by_id:
            raise SelectionError(f"slot {index + 1} references unknown block {block_id!r}")
        if block_id in observed_ids:
            raise SelectionError(f"duplicate block selection: {block_id}")
        observed_ids.append(block_id)
        expected_frames = int(block_by_id[block_id].get("frames", 0))
        frames = int(slot.get("frames", slot.get("duration_frames", 0)))
        if frames != expected_frames:
            raise SelectionError(f"{block_id} requires {expected_frames} frames; got {frames}")
        total += frames
        reference_role = str(slot.get("reference_role", block_by_id[block_id].get("role", ""))).strip().casefold()
        raw_observation = str(slot.get("candidate_observation", "")).strip()
        observation = raw_observation.casefold()
        if not observation or raw_observation.upper().startswith(_PLACEHOLDER_PREFIXES):
            raise SelectionError(f"{block_id} lacks independent candidate_observation")
        if reference_role and observation == reference_role:
            raise SelectionError(f"{block_id} candidate_observation copies the reference role")
        if "source_path" not in slot or str(slot["source_path"]).upper().startswith(_PLACEHOLDER_PREFIXES):
            raise SelectionError(f"{block_id} lacks source_path")
        source_start = int(slot.get("source_start_frame", 0))
        if source_start < 0:
            raise SelectionError(f"{block_id} source_start_frame cannot be negative")
        if slot.get("reverse") is True or float(slot.get("speed", slot.get("source_rate", 1.0))) != 1.0:
            raise SelectionError(f"{block_id} violates normal-speed/no-reverse policy")
        if by_inventory_path is not None:
            source = Path(str(slot["source_path"])).expanduser().absolute()
            record = by_inventory_path.get(str(source))
            if record is None:
                raise SelectionError(f"{block_id} source is not in the authorized footage inventory")
            digest = hash_cache.setdefault(str(source), sha256_file(source))
            if digest != record["sha256"] or slot.get("source_sha256") != digest:
                raise SelectionError(f"{block_id} source hash is not bound to the current inventory bytes")
            if feasibility_by_id is not None:
                decision = feasibility_by_id[block_id]
                if decision.get("coverage") == "missing":
                    raise SelectionError(f"{block_id} remains missing in the feasibility lock")
                if str(slot.get("source_clip_id")) not in {str(value) for value in decision.get("evidence_clip_ids", [])}:
                    raise SelectionError(f"{block_id} selected clip is not one of its feasibility evidence clips")
            source_fps = Fraction(record["video"]["r_frame_rate"])
            required_source_frames = max(1, math.ceil(frames * float(source_fps / output_fps)))
            source_count = int(record["video"].get("frame_count") or 0)
            if source_count <= 0 or source_start + required_source_frames > source_count:
                raise SelectionError(
                    f"{block_id} source window exceeds clip bounds: start {source_start} + {required_source_frames} > {source_count}"
                )
    expected_total = sum(int(block.get("frames", 0)) for block in blocks)
    expected_ids = [str(block.get("id")) for block in blocks]
    if mode == "reference-locked" and observed_ids != expected_ids:
        raise SelectionError("reference-locked slots must follow the locked picture-state order exactly")
    source_uses: Dict[str, List[str]] = {}
    for slot, block_id in zip(slots, observed_ids):
        source_identity = str(slot.get("source_sha256") or Path(str(slot.get("source_path", ""))).expanduser().absolute())
        source_uses.setdefault(source_identity, []).append(block_id)
    for source_identity, block_ids in source_uses.items():
        if len(block_ids) <= 1:
            continue
        repeat_groups = {str(block_by_id[block_id].get("repeat_group", "")) for block_id in block_ids}
        if len(repeat_groups) != 1 or not next(iter(repeat_groups)):
            raise SelectionError(f"source {source_identity} is reused across {block_ids} without one explicit reference repeat_group")
    if total != expected_total:
        raise SelectionError(f"selection totals {total} frames; blueprint totals {expected_total}")
    return {"status": "PASS", "slots": len(slots), "frames": total, "block_ids": observed_ids}
