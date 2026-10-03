from __future__ import annotations

import json
import math
import os
import re
import stat
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .contracts import validate_contract


class RetrievalError(RuntimeError):
    pass


def load_jsonl_catalog(path: Path) -> List[Dict[str, Any]]:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise RetrievalError(f"refusing catalog symlink: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RetrievalError(f"catalog is not a regular file: {path}")
        rows: List[Dict[str, Any]] = []
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise RetrievalError(f"catalog line {line_number} is not a JSON object")
                rows.append(value)
        return rows
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_feature_map(path: Optional[Path]) -> Dict[str, Mapping[str, Any]]:
    if path is None:
        return {}
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise RetrievalError(f"refusing feature-file symlink: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    rows = value.get("features", []) if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise RetrievalError("feature file must be an array or an object with a features array")
    return {str(row["path"]): row for row in rows if isinstance(row, dict) and row.get("path")}


_STOPWORDS = {
    "a",
    "an",
    "and",
    "against",
    "at",
    "be",
    "both",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "must",
    "of",
    "on",
    "or",
    "over",
    "same",
    "should",
    "the",
    "to",
    "use",
    "with",
}

_CONCEPT_ALIASES = {
    "architecture": {
        "architecture",
        "architectural",
        "building",
        "facade",
        "interior",
        "room",
        "structure",
        "tower",
    },
    "sport": {"athlete", "athletic", "discipline", "exercise", "sport", "training", "workout"},
    "screen": {"computer", "desk", "display", "interface", "laptop", "monitor", "monitors", "screen", "screens", "work"},
    "walking": {"arrival", "corridor", "entrance", "rear", "walk", "walking"},
    "static": {"hold", "locked", "nearly", "seated", "standing", "static"},
    "night": {"dark", "evening", "night"},
    "warm": {"amber", "cream", "orange", "red", "sunset", "warm"},
    "outdoor": {"court", "environment", "exterior", "garden", "outdoor", "park", "pool", "road", "sky", "street"},
    "human": {"adult", "athlete", "face", "human", "people", "person", "portrait", "subject"},
    "pair": {"dance", "interaction", "pair", "paired", "together", "two"},
    "motion": {"action", "energetic", "movement", "moving", "motion", "peak", "walk", "walking"},
    "silhouette": {"backlit", "shadow", "silhouette"},
    "vehicle": {"bicycle", "car", "motorcycle", "road", "transport", "vehicle"},
    "hospitality": {"cafe", "hospitality", "hotel", "lobby", "restaurant", "venue"},
    "water": {"court", "pool", "water"},
    "profile": {"profile", "side"},
    "rear": {"back", "rear"},
    "wide": {"environment", "establishing", "fullbody", "wide"},
    "bright": {"bright", "cream", "day", "daylight", "pale", "white"},
}
_ALIAS_LOOKUP = {alias: concept for concept, aliases in _CONCEPT_ALIASES.items() for alias in aliases}


def _tokens(value: Any) -> Set[str]:
    raw = re.findall(r"[a-z0-9]+", str(value).lower())
    result: Set[str] = set()
    for token in raw:
        if token in _STOPWORDS or len(token) < 2:
            continue
        result.add(token)
        concept = _ALIAS_LOOKUP.get(token)
        if concept:
            result.add(concept)
    return result


def _role_parts(role: Mapping[str, Any]) -> Tuple[Set[str], Set[str], Set[str]]:
    identity = _tokens(role.get("role", ""))
    observations = _tokens(" ".join(str(role.get(key, "")) for key in ("reference_observation", "composition", "description")))
    requirements = _tokens(" ".join(str(value) for value in role.get("substitute_requirements", [])))
    return identity, observations, requirements


def _catalog_tokens(row: Mapping[str, Any]) -> Set[str]:
    values: List[str] = [
        str(row.get("path", "")),
        " ".join(str(value) for value in row.get("semantic_groups", [])),
    ]
    for label in row.get("top_visual_labels", []):
        values.append(str(label.get("label", "")) if isinstance(label, Mapping) else str(label))
    for family in row.get("duplicate_or_setup_families", []):
        if isinstance(family, Mapping):
            values.extend((str(family.get("family_id", "")), str(family.get("description", ""))))
        else:
            values.append(str(family))
    return _tokens(" ".join(values))


def _target_duration_s(blueprint: Mapping[str, Any], role: Mapping[str, Any]) -> Optional[float]:
    start = role.get("start_frame")
    end = role.get("end_frame_exclusive")
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return None
    fps_value = ((blueprint.get("source_lock") or {}).get("video") or {}).get("fps")
    if fps_value is None:
        return None
    try:
        fps = float(Fraction(str(fps_value)))
    except (ValueError, ZeroDivisionError):
        return None
    return (end - start) / fps if fps > 0 else None


def _feature_values(row: Mapping[str, Any], features: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    return features.get(str(row.get("path", "")), {})


def _score_candidate(
    blueprint: Mapping[str, Any],
    role: Mapping[str, Any],
    row: Mapping[str, Any],
    features: Mapping[str, Mapping[str, Any]],
) -> Tuple[float, List[str]]:
    identity, observations, requirements = _role_parts(role)
    candidate = _catalog_tokens(row)
    evidence: List[str] = []
    score = 0.0

    for label, terms, weight in (
        ("role", identity, 5.0),
        ("requirement", requirements, 3.0),
        ("observation", observations, 1.5),
    ):
        overlap = sorted(terms & candidate)
        if overlap:
            score += weight * len(overlap)
            evidence.append(f"{label}_terms:" + ",".join(overlap[:12]))

    text = _tokens(
        " ".join(
            [
                str(role.get("role", "")),
                str(role.get("reference_observation", "")),
                str(role.get("composition", "")),
                *[str(value) for value in role.get("substitute_requirements", [])],
            ]
        )
    )
    motion = row.get("camera_motion") or {}
    speed = float(motion.get("translation_speed_p90") or 0.0)
    local_peaks = int((row.get("editorial_role_counts") or {}).get("subject_or_local_motion_peak") or 0)
    if "motion" in text or "sport" in text or "walking" in text:
        motion_score = min(3.0, speed * 20.0) + min(2.0, local_peaks * 0.5)
        score += motion_score
        evidence.append(f"native_motion:{motion_score:.3f}")
    if "walking" in text and "static" in candidate:
        score -= 12.0
        evidence.append("action_contradiction:static_vs_walking")
    if "sport" in text and "static" in candidate:
        score -= 8.0
        evidence.append("action_contradiction:static_vs_sport")
    if {"stable", "hold", "locked"} & text and motion.get("label") == "locked_or_nearly_locked":
        score += 2.0
        evidence.append("stable_camera")

    poses = int(row.get("pose_count_in_representative_frame") or 0)
    faces = int(row.get("face_count_in_representative_frame") or 0)
    if "human" in text and (poses or faces or "human" in candidate):
        score += 1.5
        evidence.append("human_evidence")
    if "pair" in text:
        pair_score = 3.0 if poses >= 2 else (1.0 if "pair" in candidate else 0.0)
        score += pair_score
        if pair_score:
            evidence.append("paired_action_evidence")

    target_duration = _target_duration_s(blueprint, role)
    duration = float(row.get("duration_s") or 0.0)
    if target_duration is not None:
        if duration + 1e-6 < target_duration:
            score -= 20.0
            evidence.append("duration_shortfall")
        else:
            score += 1.0
            evidence.append("duration_covered")

    metrics = role.get("grade_metrics") or {}
    feature = _feature_values(row, features)
    target_luma = metrics.get("luma_p50_8bit")
    source_luma = feature.get("luma_p50")
    if isinstance(target_luma, (int, float)) and isinstance(source_luma, (int, float)):
        luma_score = max(0.0, 1.5 - abs(float(target_luma) - float(source_luma)) / 64.0)
        score += luma_score
        evidence.append(f"luma_tiebreak:{luma_score:.3f}")
    target_sat = metrics.get("saturation_mean_8bit")
    source_sat = feature.get("sat_p50")
    if isinstance(target_sat, (int, float)) and isinstance(source_sat, (int, float)):
        sat_score = max(0.0, 1.0 - abs(float(target_sat) - float(source_sat)) / 128.0)
        score += sat_score
        evidence.append(f"saturation_tiebreak:{sat_score:.3f}")

    sharpness = feature.get("laplacian_var")
    if isinstance(sharpness, (int, float)) and math.isfinite(float(sharpness)):
        sharpness_score = min(1.0, math.log1p(max(0.0, float(sharpness))) / 8.0)
        score += sharpness_score
        evidence.append(f"sharpness_tiebreak:{sharpness_score:.3f}")

    return round(score, 6), evidence


def _role_records(blueprint: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    roles = blueprint.get("shot_roles")
    if not isinstance(roles, list) or not roles:
        raise RetrievalError("blueprint must contain a non-empty shot_roles array")
    if any(not isinstance(role, Mapping) or not role.get("id") for role in roles):
        raise RetrievalError("every shot role must be an object with an id")
    return roles


def shortlist_reference_roles(
    blueprint: Mapping[str, Any],
    catalog: Sequence[Mapping[str, Any]],
    *,
    features: Optional[Mapping[str, Mapping[str, Any]]] = None,
    top_n: int = 20,
) -> Dict[str, Any]:
    """Rank a complete editorial catalog against reference shot roles.

    This is a retrieval funnel only. It deliberately leaves every candidate in
    ``NOT_REVIEWED`` state and preserves the reference's manual reject checks.
    Full-motion and renderer-exact crop review remain mandatory before selection.
    """

    if top_n < 1 or top_n > 100:
        raise RetrievalError("top_n must be between 1 and 100")
    roles = _role_records(blueprint)
    catalog_rows = list(catalog)
    if not catalog_rows:
        raise RetrievalError("catalog must contain at least one source row")
    features = features or {}

    output_roles: Dict[str, Any] = {}
    for role in roles:
        ranked = []
        for row in catalog_rows:
            score, evidence = _score_candidate(blueprint, role, row, features)
            ranked.append((score, str(row.get("path", "")), row, evidence))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        candidates = []
        for rank, (score, _path, row, evidence) in enumerate(ranked[:top_n], start=1):
            candidates.append(
                {
                    "rank": rank,
                    "clip_id": row.get("clip_id"),
                    "source_path": row.get("path"),
                    "source_sha256": row.get("source_sha256") or row.get("sha256"),
                    "duration_s": row.get("duration_s"),
                    "orientation": row.get("orientation"),
                    "coverage_board": row.get("coverage_board"),
                    "families": [
                        family.get("family_id") if isinstance(family, Mapping) else family
                        for family in row.get("duplicate_or_setup_families", [])
                    ],
                    "labels": [
                        label.get("label") if isinstance(label, Mapping) else label for label in row.get("top_visual_labels", [])
                    ],
                    "machine_score": score,
                    "score_evidence": evidence,
                    "manual_review_status": "NOT_REVIEWED",
                }
            )
        role_id = str(role["id"])
        output_roles[role_id] = {
            "role": role.get("role"),
            "target_frames": [role.get("start_frame"), role.get("end_frame_exclusive")],
            "substitute_requirements": list(role.get("substitute_requirements", [])),
            "manual_reject_checks": list(role.get("reject_if", [])),
            "candidates": candidates,
        }

    result = {
        "schema_version": 1,
        "status": "MACHINE_SHORTLIST_REVIEW_REQUIRED",
        "method": "deterministic semantic, motion, duration, source-quality and grade-recoverability retrieval; no machine acceptance",
        "warning": "Inspect coverage boards, then normal-speed renderer-exact crop proxies. A machine rank cannot prove role/action/composition or creative acceptance.",
        "source_count": len(catalog_rows),
        "role_count": len(roles),
        "top_n": top_n,
        "roles": output_roles,
    }
    validate_contract("role-shortlist", result)
    return result
