"""``FOOTAGE_LIBRARY.json``, read only — the corpus census behind intake's numbers.

Intake's job is to put the *actual* clip counts in front of the operator before anything
is built, because otherwise a reel can be assembled in twenty minutes from a corpus that
could not carry it, and be rejected on sight. "This world holds eleven clips" is a number
the operator can act on; "the corpus may be thin" is not.

What this module will not do is invent a verdict. It states the ceiling a world imposes
on a 1:1 build — one distinct clip per output block — and defers whether that covers a
particular reference to ``FEASIBILITY_REPORTED``, which is the stage that actually knows.
There is no threshold constant here, because doctrine declares none.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

LIBRARY_SCHEMA = "footage-library-v1"
UNTAGGED = "untagged"

PASS = "PASS"
ABSENT = "ABSENT"
FAIL = "FAIL"


def load_library(path: Path) -> Dict[str, Any]:
    """Read the library without ever raising. Absence and corruption are distinguished."""
    path = Path(path)
    report: Dict[str, Any] = {
        "status": ABSENT,
        "path": str(path),
        "error": None,
        "schema": None,
        "built_at_utc": None,
        "clips_total": 0,
        "indexed": None,
        "library_root": None,
        "drive_folder": None,
        "worlds": {},
    }
    if not path.is_file():
        return report
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {**report, "status": FAIL, "error": f"{path} does not parse: {exc}"}
    if not isinstance(data, dict) or not isinstance(data.get("clips"), dict):
        return {**report, "status": FAIL, "error": f"{path} carries no clips object"}
    counter: Counter = Counter()
    for clip in data["clips"].values():
        tags = clip.get("tags") if isinstance(clip, dict) else None
        world = (tags or {}).get("world_cluster") if isinstance(tags, dict) else None
        counter[world if isinstance(world, str) and world.strip() else UNTAGGED] += 1
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    return {
        **report,
        "status": PASS,
        "schema": data.get("schema"),
        "built_at_utc": data.get("built_at_utc"),
        "clips_total": len(data["clips"]),
        "indexed": data.get("indexed"),
        "library_root": source.get("library_root"),
        "drive_folder": source.get("drive_folder"),
        "worlds": dict(counter),
    }


def world_census(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every indexed world with its clip count, largest first."""
    worlds = report.get("worlds") or {}
    ordered = sorted(worlds.items(), key=lambda item: (-item[1], item[0]))
    return [{"world": world, "clips": clips} for world, clips in ordered]


def corpus_note(report: Dict[str, Any], *, world: Optional[str] = None) -> Dict[str, Any]:
    """A feasibility-backed preview for one world, stated in real clip counts."""
    census = world_census(report)
    total = int(report.get("clips_total") or 0)
    largest = census[0] if census else None
    note: Dict[str, Any] = {
        "world": world,
        "clips": None,
        "library_clips_total": total,
        "library_status": report.get("status"),
        "library_path": report.get("path"),
        "largest_world": largest,
        "share_percent": None,
        "reference_locked_block_ceiling": None,
        "census": census,
        "recommendation": "",
    }
    if world is None:
        note["recommendation"] = (
            f"the reference's visual world is not known until the reference is analysed, so no world-level ceiling applies yet; "
            f"the indexed corpus holds {total} clips across {len(census)} worlds."
        )
        return note
    clips = int((report.get("worlds") or {}).get(world, 0))
    note["clips"] = clips
    note["reference_locked_block_ceiling"] = clips
    note["share_percent"] = round(clips / total * 100, 2) if total else None
    if clips == 0:
        note["recommendation"] = (
            f"world {world!r} is not present in the indexed corpus of {total} clips, so no block of a reference-locked build "
            f"could be sourced from it; either the world label is wrong or the corpus needs new footage before this reference "
            f"is buildable 1:1."
        )
        return note
    largest_text = f" (the largest world, {largest['world']}, holds {largest['clips']})" if largest else ""
    note["recommendation"] = (
        f"the {world} corpus holds {clips} of {total} indexed clips, so a reference-locked 1:1 build can draw at most "
        f"{clips} distinct blocks from it{largest_text}; whether that covers this reference is not known until "
        f"FEASIBILITY_REPORTED measures the reference's own block count."
    )
    return note
