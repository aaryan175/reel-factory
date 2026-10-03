"""Read-only access to ``REEL_REGISTRY.json`` for the daemon (§8.1, §9.4).

The registry is the single status truth and this module never writes it. Two jobs only:

* **Preflight.** If the file will not parse, the daemon halts and surfaces the parse error
  rather than doing anything that could end with the registry being rewritten (§6.5).
* **Ownership.** A reel whose entry names another runtime — e.g.
  ``LEGACY_QUARANTINED_BESPOKE_PIPELINE``, or a quarantined review state — is skipped by
  the daemon entirely, with the registry's own words as the reason.

The write path (flock, schema validation, ``.bak-<utc>``, atomic replace) is deliberately
absent: nothing in the daemon core needs it, and adding an unused second writer to the
file the operation cannot afford to corrupt would be the wrong trade.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REGISTRY_FILENAME = "REEL_REGISTRY.json"

#: Substrings in ``runtime`` or ``review_state`` that hand a reel to something else.
FOREIGN_RUNTIME_MARKERS = ("QUARANTINED", "LEGACY", "BESPOKE")

KNOWN_LANES = ("VOLUME", "CRAFT")


def registry_path(location: Path) -> Path:
    path = Path(location)
    return path / REGISTRY_FILENAME if path.is_dir() else path


def load_registry(location: Path) -> Dict[str, Any]:
    path = registry_path(location)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("{} is not a JSON object".format(path))
    return payload


def registry_check(location: Path) -> Dict[str, Any]:
    """Never raises. A registry that will not parse is a named halt, not a traceback."""
    path = registry_path(location)
    try:
        registry = load_registry(path)
    except FileNotFoundError:
        return {"status": "FAIL", "path": str(path), "reason": "{} does not exist".format(path)}
    except (OSError, ValueError) as exc:
        return {"status": "FAIL", "path": str(path), "reason": "{} failed to parse: {}".format(path, exc)}
    reels = registry.get("reels")
    if not isinstance(reels, list):
        return {"status": "FAIL", "path": str(path), "reason": "{} has no reels list".format(path)}
    return {"status": "PASS", "path": str(path), "reels": len(reels), "schema_version": registry.get("schema_version")}


def _lane(value: Any) -> Optional[str]:
    """``"VOLUME (mode call pending; may become CRAFT)"`` is still the VOLUME lane."""
    if not isinstance(value, str) or not value.strip():
        return None
    head = value.strip().split()[0].upper().strip("(),")
    return head if head in KNOWN_LANES else None


def _local_project_id(value: Any, projects_root: Path) -> Optional[str]:
    """Only a direct child of the projects root is a project this daemon can drive."""
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip().split()[0].rstrip("/")
    if not token or token.startswith("~") or token.startswith("/") or "/" in token:
        return None
    candidate = Path(projects_root) / token
    if candidate.parent != Path(projects_root):
        return None
    return token


def _foreign_reason(reel: Dict[str, Any]) -> Optional[str]:
    for field in ("runtime", "review_state"):
        value = reel.get(field)
        if isinstance(value, str) and any(marker in value.upper() for marker in FOREIGN_RUNTIME_MARKERS):
            return "registry {} is {}".format(field, value)
    return None


def _driver_reason(reel: Dict[str, Any]) -> Optional[str]:
    """``driver: "agent"`` excludes permanently, per the registry's own ``daemon_contract``.

    The contract: *"daemon drives a project iff (a) it was created via studio intake
    (studio.db origin) OR (b) its registry entry has driver=='daemon'. Registry-absent
    projects are studio-born and driven. driver=='agent' excludes permanently."* This is the half that can be decided from the registry alone;
    the studio-intake half needs the project on disk and lives in the daemon.
    """
    driver = reel.get("driver")
    if isinstance(driver, str) and driver.strip().lower() == "agent":
        return "registry driver is agent; this reel is driven by an interactive session, not the daemon"
    return None


def _shortcode_match(reel: Dict[str, Any], project_ids: Sequence[str]) -> Optional[str]:
    """Link a reel with no ``project_root`` to a directory by its shortcode.

    The case §9.4 exists to skip — CRAFT lane, quarantined bespoke runtime — can record
    no ``project_root`` at all. Without this fallback its exclusion is
    invisible, and "absent from the index" reads identically to "no restriction recorded".
    Two spellings are accepted, both of which occur in this tree: the shortcode used
    verbatim as a directory name, and the ``reel-<shortcode>-v<N>`` convention.
    """
    shortcode = str(reel.get("reference_shortcode") or "").strip().lower()
    if not shortcode:
        return None
    for project_id in project_ids:
        if project_id.lower() == shortcode:
            return project_id
        derived = shortcode_for_project(project_id)
        if derived and derived.lower() == shortcode:
            return project_id
    return None


def ownership_index(
    location: Path,
    *,
    projects_root: Optional[Path] = None,
    project_ids: Sequence[str] = (),
) -> Dict[str, Dict[str, Any]]:
    """Map project id → what the registry says the daemon may do with it.

    ``project_ids`` are the directories that actually exist. They are only ever used to
    *link* a reel that named no ``project_root``; nothing here invents a project.
    """
    path = registry_path(location)
    root = Path(projects_root) if projects_root is not None else path.parent
    check = registry_check(path)
    if check["status"] != "PASS":
        return {}
    reels: List[Dict[str, Any]] = load_registry(path).get("reels", [])
    index: Dict[str, Dict[str, Any]] = {}
    for reel in reels:
        if not isinstance(reel, dict):
            continue
        project_id = _local_project_id(reel.get("project_root"), root) or _shortcode_match(reel, project_ids)
        if project_id is None:
            continue
        foreign = _foreign_reason(reel) or _driver_reason(reel)
        declared = reel.get("lane")
        index[project_id] = {
            "project_id": project_id,
            "driver": str(reel.get("driver")).strip().lower() if isinstance(reel.get("driver"), str) else None,
            "lane": _lane(declared),
            # The verbatim string, because a lane can read "VOLUME (mode call pending;
            # 1:1 captions requested — may become CRAFT)" and normalising to "VOLUME"
            # would silently drop a call the operator is owed (§5.1).
            "lane_declared": declared if isinstance(declared, str) else None,
            "reference_shortcode": reel.get("reference_shortcode"),
            "daemon_owned": foreign is None,
            "reason": foreign,
        }
    return index


def declared_craft_holder(location: Path) -> Optional[str]:
    """Who the registry says holds the one CRAFT slot, or ``None``.

    ``lanes.CRAFT.active`` is a display string — ``"REF-06 (v10-r5 source-contours)"`` —
    so the shortcode is its first token and the parenthetical is commentary.
    """
    check = registry_check(location)
    if check["status"] != "PASS":
        return None
    lanes = load_registry(location).get("lanes")
    lane = lanes.get("CRAFT") if isinstance(lanes, dict) else None
    active = lane.get("active") if isinstance(lane, dict) else None
    if not isinstance(active, str) or not active.strip():
        return None
    return active.strip().split()[0]


# --- read model for the studio screens -------------------------------------
#
# The daemon halts on an unparseable registry (above). A read-only screen cannot halt
# usefully, so the UI path reports the parse error as text and keeps rendering the board
# beside it — a broken registry says more than a blank page. Same file, same no-write
# rule, different failure posture.

PASS = "PASS"
ABSENT = "ABSENT"
FAIL = "FAIL"

PROJECT_ID_PATTERN = re.compile(r"^reel-(?P<shortcode>.+)-v\d+$")


def registry_report(location: Path) -> Dict[str, Any]:
    """Read the registry without ever raising. Absence and corruption are distinguished."""
    path = registry_path(location)
    report: Dict[str, Any] = {
        "status": ABSENT,
        "path": str(path),
        "error": None,
        "data": None,
        "reels": [],
        "lanes": {},
        "open_calls": [],
        "factory_mode": None,
    }
    if not path.is_file():
        return report
    try:
        data = load_registry(path)
    except (OSError, ValueError) as exc:
        return {**report, "status": FAIL, "error": "{} does not parse: {}".format(path, exc)}
    mode = data.get("factory_mode")
    return {
        **report,
        "status": PASS,
        "data": data,
        "reels": [reel for reel in data.get("reels") or [] if isinstance(reel, dict)],
        "lanes": data.get("lanes") if isinstance(data.get("lanes"), dict) else {},
        "open_calls": [call for call in data.get("open_calls") or [] if isinstance(call, dict)],
        "factory_mode": mode.get("mode") if isinstance(mode, dict) else None,
    }


def shortcode_for_project(project_id: str) -> Optional[str]:
    """``reel-ref-28-v1`` → ``ref-28``; a project id is the lowercased shortcode."""
    match = PROJECT_ID_PATTERN.match(str(project_id))
    return match.group("shortcode") if match else None


def reel_for_project(report: Dict[str, Any], project_id: str) -> Optional[Dict[str, Any]]:
    shortcode = shortcode_for_project(project_id)
    if not shortcode:
        return None
    for reel in report.get("reels") or []:
        if str(reel.get("reference_shortcode") or "").lower() == shortcode.lower():
            return dict(reel)
    return None


def craft_slot(report: Dict[str, Any]) -> Dict[str, Any]:
    """The CRAFT lane's one-at-a-time quota, exactly as the registry states it."""
    lane = (report.get("lanes") or {}).get("CRAFT")
    lane = lane if isinstance(lane, dict) else {}
    active = lane.get("active")
    active = active if isinstance(active, str) and active.strip() else None
    return {
        "lane": "CRAFT",
        "quota": lane.get("quota"),
        "active": active,
        "occupied": active is not None,
        "source": "registry lanes.CRAFT.active: {}".format(active) if active else None,
    }


def normalise_lane(value: Any) -> Optional[str]:
    """Public name for the lane normaliser, so callers need not reach for ``_lane``."""
    return _lane(value)


def lane_for_project(report: Dict[str, Any], project_id: str) -> Tuple[Optional[str], Optional[str]]:
    """``(lane, source sentence)``. ``(None, None)`` when nothing declares a lane.

    Being listed in the registry is not a declaration that a reel is VOLUME, so absence
    reads as undeclared rather than being filled in with the common case.

    The source sentence carries the registry's **raw** lane string, not the normalised
    token. An entry can read ``"VOLUME (mode call pending; 1:1 captions requested — may
    become CRAFT)"``; normalising that to ``VOLUME`` for display would
    delete a pending mode call that §5.1 says must surface, and the operator would see a
    settled lane where the registry recorded an open question.
    """
    shortcode = shortcode_for_project(project_id)
    slot = craft_slot(report)
    if shortcode and slot["active"] and shortcode.lower() in slot["active"].lower():
        return "CRAFT", slot["source"]
    raw = (reel_for_project(report, project_id) or {}).get("lane")
    declared = _lane(raw)
    return (declared, "registry reel entry lane: {}".format(raw)) if declared else (None, None)


def open_calls_by_project(report: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for call in report.get("open_calls") or []:
        project = call.get("project")
        if isinstance(project, str) and project:
            grouped.setdefault(project, []).append(dict(call))
    return grouped
