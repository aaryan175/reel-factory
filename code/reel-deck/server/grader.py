"""
Clip grader — the reviewer grades the footage library one key at a time.

Reads (never writes): FOOTAGE_LIBRARY.json, person_scores.json, usage.json,
identity_pool.json. Writes ONLY grades.jsonl, append-only, one line per tap,
the moment the tap lands. The kit reads that file as law. (The grader before
this one kept grades in the phone's localStorage and lost them — never again.)

Grades are per SEGMENT: a clip is cut into ~5 s pieces by `segments()` (the
kit reproduces the same grid from the duration alone). A line with t0/t1 set
grades that piece; a line with t0 = t1 = null grades the whole clip (and is the
only place an ident ME/NOTME may sit). Lines written before segments existed
have no t0/t1 at all and read as whole-clip.

Folding: the latest line per (stem, t0, t1) wins per field. A later null never
erases an earlier value; only set fields count. A whole-clip line never
overwrites a segment line — the segment's own grade outranks the whole-clip one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import settings
from .receipts import _write_lock, utc_iso

LIBRARY_PATH = settings.REEL_HOME / "FOOTAGE_LIBRARY.json"
FOOTAGE_ROOT = settings.FOOTAGE_LIBRARY_ROOT
PERSON_SCORES_PATH = FOOTAGE_ROOT / "person_scores.json"
USAGE_PATH = FOOTAGE_ROOT / "usage.json"
IDENTITY_POOL_PATH = settings.TOOLS_DIR / "onetoone" / "identity_pool.json"
GRADES_PATH = settings.RECEIPTS / "clip-grades" / "grades.jsonl"

# Library clips whose `intake` field equals this tag are flagged "new" in the grader.
NEW_INTAKE = os.environ.get("REEL_DECK_NEW_INTAKE_TAG") or "new_footage_intake"
GRADES = ("HERO", "BROLL", "NEVER")
IDENTS = ("ME", "NOTME")

SEG = 5.0          # target piece length, seconds
ONE_PIECE_MAX = 7.0  # a clip this short or shorter is one piece


def segments(duration: Any) -> list[list[float]]:
    """The segment grid for a clip of `duration` seconds: [[t0, t1], ...].

    dur <= 7.0 s → one piece [0, dur]. Otherwise n = max(2, round(dur / 5.0))
    (Python's round: halves go to the even number) equal pieces of dur / n,
    inner boundaries rounded to 0.01 s, the last piece ending exactly at dur.
    No usable duration → [] (the clip can only be graded whole).
    """
    try:
        dur = float(duration)
    except (TypeError, ValueError):
        return []
    if not dur > 0 or dur != dur or dur == float("inf"):
        return []
    if dur <= ONE_PIECE_MAX:
        return [[0.0, dur]]
    n = max(2, round(dur / SEG))
    bounds = [0.0] + [round(i * dur / n, 2) for i in range(1, n)] + [dur]
    return [[bounds[i], bounds[i + 1]] for i in range(n)]


def seg_key(t0: float, t1: float) -> str:
    """The key a segment grade is filed under in the items JSON: "0.00-5.00"."""
    return f"{t0:.2f}-{t1:.2f}"


def _match_segment(t0: Any, t1: Any, grid: list[list[float]]) -> list[float] | None:
    if isinstance(t0, bool) or isinstance(t1, bool):
        return None
    try:
        a, b = float(t0), float(t1)
    except (TypeError, ValueError):
        return None
    for piece in grid:
        if abs(piece[0] - a) < 0.001 and abs(piece[1] - b) < 0.001:
            return piece
    return None


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _identity_sets() -> dict[str, set[str]]:
    pool = _dict(_json(IDENTITY_POOL_PATH))
    blacklist = {str(b.get("stem")) for b in pool.get("blacklist_full") or [] if isinstance(b, dict) and b.get("stem")}
    return {
        "settled": set(pool.get("settled_pool") or []),
        "not_operator": set(pool.get("not_operator") or []),
        "blacklisted": blacklist,
    }


def _identity(stem: str, sets: dict[str, set[str]]) -> str:
    # a ban outranks a ruling, a "not me" outranks the settled pool
    for name in ("blacklisted", "not_operator", "settled"):
        if stem in sets[name]:
            return name
    return "unruled"


def read_grades() -> dict[str, dict[str, Any]]:
    """stem → {grade, ident, clip_id, at, segments} folded latest-wins per field.

    grade/ident = the whole-clip line(s); segments = {seg_key: grade} from the
    lines that carry t0/t1."""
    folded: dict[str, dict[str, Any]] = {}
    try:
        handle = open(GRADES_PATH, encoding="utf-8")
    except OSError:
        return folded
    with handle:
        for raw in handle:
            try:
                line = json.loads(raw)
            except ValueError:
                continue   # a torn line never takes the rest of the file with it
            if not isinstance(line, dict) or not line.get("stem"):
                continue
            entry = folded.setdefault(str(line["stem"]), {"grade": None, "ident": None, "segments": {}})
            t0, t1 = line.get("t0"), line.get("t1")
            if t0 is not None and t1 is not None:
                try:
                    key = seg_key(float(t0), float(t1))
                except (TypeError, ValueError):
                    continue
                if line.get("grade") is not None:
                    entry["segments"][key] = line["grade"]
            elif t0 is None and t1 is None:
                for field in ("grade", "ident"):
                    if line.get(field) is not None:
                        entry[field] = line[field]
            else:
                continue   # half a window is not a window
            entry["clip_id"] = line.get("clip_id") or entry.get("clip_id")
            entry["at"] = line.get("at")
    return folded


def _group(item: dict[str, Any]) -> int:
    if item["new"]:
        return 0
    verdict = item["verdict"]
    if verdict == "NO_PROXY":
        return 6   # nothing to play, so last even when untagged
    if item["untagged"]:
        return 1   # no tag notes: footage nobody has looked at yet
    if verdict == "PERSON":
        return 2 if item["identity"] == "unruled" else 3
    if verdict == "EMPTY":
        return 5
    return 4   # FAINT and anything not scanned yet


def list_items() -> list[dict[str, Any]]:
    """Every clip in the library, in queue order, with its grade folded in.
    The library is read fresh on every call (it is rewritten by other agents)."""
    clips = _dict(_dict(_json(LIBRARY_PATH)).get("clips"))
    scores = _dict(_json(PERSON_SCORES_PATH))
    usage = _dict(_json(USAGE_PATH))
    sets = _identity_sets()
    grades = read_grades()
    items = []
    for clip_id, clip in clips.items():
        if not isinstance(clip, dict):
            continue
        stem = Path(str(clip.get("name") or clip_id)).stem
        tags = _dict(clip.get("tags"))
        score = _dict(scores.get(clip_id))
        verdict = score.get("verdict") or "UNSCANNED"
        proxy = clip.get("proxy")
        if not proxy:
            verdict = "NO_PROXY"
        frames = score.get("frames")
        graded = grades.get(stem, {})
        grid = segments(clip.get("duration"))
        keys = {seg_key(a, b) for a, b in grid}
        items.append({
            "clip_id": clip_id,
            "stem": stem,
            "dur": clip.get("duration"),
            "verdict": verdict,
            "face_frames": f"{score.get('frames_with_face', 0)}/{frames}" if frames is not None else "",
            "identity": _identity(stem, sets),
            "uses": int(_dict(usage.get(stem)).get("uses") or 0),
            "note": tags.get("notes") or "",
            "subject": tags.get("subject") or "",
            "new": clip.get("intake") == NEW_INTAKE,
            "untagged": not tags.get("notes"),
            "proxy_url": "/media?p=" + quote(str(FOOTAGE_ROOT / proxy)) if proxy else None,
            "segments": grid,
            "segment_keys": [seg_key(a, b) for a, b in grid],   # the keys `grades` is filed under, same order
            "grades": {k: v for k, v in (graded.get("segments") or {}).items() if k in keys},
            "whole": graded.get("grade"),
            "grade": graded.get("grade"),   # = whole, kept for older readers
            "ident": graded.get("ident"),
        })
    items.sort(key=lambda item: (_group(item), item["stem"]))
    return items


def _pieces(item: dict[str, Any]) -> list[str | None]:
    """The queue entries of one clip: its segment keys, or [None] (= whole) if it has no grid."""
    return [seg_key(a, b) for a, b in item.get("segments") or []] or [None]


def piece_grade(item: dict[str, Any], key: str | None) -> str | None:
    """The grade that stands for this piece: its own segment grade, else the whole-clip grade."""
    own = (item.get("grades") or {}).get(key) if key else None
    return own or item.get("whole")


def resume(items: list[dict[str, Any]]) -> dict[str, int] | None:
    """The first (clip, segment) with no segment grade and no whole-clip grade covering it."""
    queue_index = 0
    for clip_index, item in enumerate(items):
        for segment_index, key in enumerate(_pieces(item)):
            if not piece_grade(item, key):
                return {"queue_index": queue_index, "clip_index": clip_index, "segment_index": segment_index}
            queue_index += 1
    return None


def summary(items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = list_items() if items is None else items
    counts = {g: 0 for g in GRADES + IDENTS}          # per clip: whole-clip grades + idents
    segment_counts = {g: 0 for g in GRADES}           # per piece: the grade that stands for it
    seg_total = seg_graded = 0
    clips_done = []
    for item in items:
        for value in (item["whole"], item["ident"]):
            if value in counts:
                counts[value] += 1
        pieces = _pieces(item)
        done = 0
        for key in pieces:
            grade = piece_grade(item, key)
            if grade in segment_counts:
                segment_counts[grade] += 1
                done += 1
        seg_total += len(pieces)
        seg_graded += done
        clips_done.append(done == len(pieces))
    first = next((i for i, done in enumerate(clips_done) if not done), None)
    graded = sum(clips_done)
    return {
        "total": len(items), "graded": graded, "remaining": len(items) - graded,   # clips fully covered
        "counts": counts,
        "segments_total": seg_total, "segments_graded": seg_graded,
        "segments_remaining": seg_total - seg_graded,
        "segment_counts": segment_counts,
        "first_ungraded_index": first,                                   # 0-based clip, into /api/grader/items
        "first_ungraded_position": first + 1 if first is not None else None,
        "resume": resume(items),                                         # first uncovered (clip, segment)
    }


def validate(entry: Any, known: dict[str, str],
             durations: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """(clean entry, None) or (None, reason). `known` = stem → clip_id from the library,
    `durations` = stem → duration (for the segment grid)."""
    if not isinstance(entry, dict):
        return None, "each grade must be an object"
    stem, clip_id = str(entry.get("stem") or ""), str(entry.get("clip_id") or "")
    if known.get(stem) != clip_id or not clip_id:
        return None, f"{stem or '?'} / {clip_id or '?'} is not a clip in the library"
    grade, ident = entry.get("grade"), entry.get("ident")
    if grade is not None and grade not in GRADES:
        return None, "grade must be HERO, BROLL or NEVER"
    if ident is not None and ident not in IDENTS:
        return None, "ident must be ME or NOTME"
    if grade is None and ident is None:
        return None, "send a grade, an ident, or both"
    t0, t1 = entry.get("t0"), entry.get("t1")
    if t0 is None and t1 is None:
        return {"stem": stem, "clip_id": clip_id, "grade": grade, "ident": ident, "t0": None, "t1": None}, None
    if ident is not None:
        return None, "ME / NOT ME is for the whole clip: send it with t0 and t1 null"
    grid = segments((durations or {}).get(stem))
    piece = _match_segment(t0, t1, grid)
    if piece is None:
        return None, f"{t0}–{t1} is not one of {stem}'s segments"
    return {"stem": stem, "clip_id": clip_id, "grade": grade, "ident": None, "t0": piece[0], "t1": piece[1]}, None


def library_stems() -> dict[str, str]:
    clips = _dict(_dict(_json(LIBRARY_PATH)).get("clips"))
    return {Path(str(c.get("name") or k)).stem: k for k, c in clips.items() if isinstance(c, dict)}


def library_durations() -> dict[str, Any]:
    clips = _dict(_dict(_json(LIBRARY_PATH)).get("clips"))
    return {Path(str(c.get("name") or k)).stem: c.get("duration") for k, c in clips.items() if isinstance(c, dict)}


def append(entries: list[dict[str, Any]], author: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One line per entry, appended and flushed to disk before we say saved."""
    by = str((author or {}).get("username") or "?")
    at = utc_iso()
    lines = [{"at": at, "stem": e["stem"], "clip_id": e["clip_id"], "t0": e.get("t0"), "t1": e.get("t1"),
              "grade": e["grade"], "ident": e["ident"], "by": by, "via": "deck-grader"} for e in entries]
    GRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with open(GRADES_PATH, "a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return lines
