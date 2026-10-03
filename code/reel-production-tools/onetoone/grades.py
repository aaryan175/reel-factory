#!/usr/bin/env python3
"""onetoone.grades — the reviewer's clip grades as casting law.

Why: a reference kept casting an empty bed where the reference shows a person, because castscan scores
the bed under the captions (an empty shot reads best) and nobody had told the machine which clips are
good. The reviewer now grades clips one key at a time in the Deck (/grader); every tap lands in

    $REEL_FACTORY_HOME/_receipts/clip-grades/grades.jsonl

one JSON object per line: {"at", "stem", "clip_id", "grade": HERO|BROLL|NEVER|null,
"ident": ME|NOTME|null, "by", "via"}. The LATEST line per stem wins per field; a null never erases.

The law (enforced by onetoone.identity.check_cast and onetoone.castscan):
  * NEVER      = a full ban everywhere, same as blacklist_full.
  * HERO or ME = the reviewer ruled this clip shows the on-camera subject, by clip number: it counts as
                 settled_pool for identity slots. HERO is also the preferred pick for every shot where
                 the reference shows a person.
  * BROLL      = never on an identity slot (a shot where the reference shows a person); fine behind a
                 caption where the reference shows nobody.
  * NOTME      = not the on-camera subject: never on an identity slot.
Nothing here writes. identity_pool.json is untouched; the effective pool = pool + grades.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Iterable

from rf_paths import RECEIPTS  # noqa: E402

GRADES_PATH = RECEIPTS / "clip-grades" / "grades.jsonl"
GRADE_VALUES = {"HERO", "BROLL", "NEVER"}
IDENT_VALUES = {"ME", "NOTME"}


def normalise(ident: str) -> str:
    """Same rule as onetoone.identity.normalise (kept local so grades has no import cycle)."""
    s = str(ident).strip()
    name = s.replace("\\", "/").rsplit("/", 1)[-1]
    if "__" in name:
        name = name.split("__")[-1]
    if "." in name and name.rsplit(".", 1)[-1].lower() in ("mp4", "mov", "mxf", "m4v"):
        name = name.rsplit(".", 1)[0]
    return name


def load_grades(path: str | Path | None = None) -> Dict[str, dict]:
    """{stem: {"grade": ..., "ident": ..., "at": ..., "clip_id": ..., "windows": {(t0, t1): grade}}}
    — latest line per field wins; the whole-clip grade is `grade` (lines with t0/t1 null), the per-segment
    grades live in `windows`.
    A missing file is an empty law, never an error (a fresh machine has no grades yet)."""
    p = Path(path or GRADES_PATH)
    out: Dict[str, dict] = {}
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(d, dict) or not d.get("stem"):
            continue
        stem = normalise(d["stem"])
        cur = out.setdefault(stem, {"grade": None, "ident": None, "at": None, "clip_id": d.get("clip_id"), "windows": {}})
        g, i = d.get("grade"), d.get("ident")
        t0, t1 = d.get("t0"), d.get("t1")
        windowed = isinstance(t0, (int, float)) and isinstance(t1, (int, float)) and float(t1) > float(t0)
        if g in GRADE_VALUES:
            if windowed:
                cur["windows"][(float(t0), float(t1))] = g
            else:
                cur["grade"] = g
        if i in IDENT_VALUES:
            cur["ident"] = i
        if d.get("at"):
            cur["at"] = d["at"]
        if d.get("clip_id"):
            cur["clip_id"] = d["clip_id"]
    return out


def _has(v: dict, grade: str) -> bool:
    """The clip carries `grade` as its whole-clip grade or on any segment."""
    return v.get("grade") == grade or grade in set((v.get("windows") or {}).values())


def never_stems(grades: Dict[str, dict] | None = None) -> set:
    """Whole-clip NEVER only; a NEVER segment bans just its window (see never_reason)."""
    g = grades if grades is not None else load_grades()
    return {s for s, v in g.items() if v.get("grade") == "NEVER"}


def hero_stems(grades: Dict[str, dict] | None = None) -> set:
    """Clips with a HERO grade on the whole clip or on any segment."""
    g = grades if grades is not None else load_grades()
    return {s for s, v in g.items() if _has(v, "HERO")}


def hero_windows(stem: str, grades: Dict[str, dict] | None = None) -> list:
    """[(t0, t1), ...] segments graded HERO for `stem` (empty when the HERO grade is whole-clip or absent)."""
    g = grades if grades is not None else load_grades()
    v = g.get(normalise(stem)) or {}
    return sorted(w for w, gr in (v.get("windows") or {}).items() if gr == "HERO")


def window_grade(stem: str, t0: float, t1: float | None = None, grades: Dict[str, dict] | None = None) -> str | None:
    """The grade that covers the in-point window [t0, t1): a NEVER segment overlapping it wins, else the
    segment holding t0, else the whole-clip grade, else None."""
    g = grades if grades is not None else load_grades()
    v = g.get(normalise(stem)) or {}
    wins = v.get("windows") or {}
    a = float(t0); b = float(t1) if t1 is not None and float(t1) > a else a
    for (w0, w1), gr in wins.items():
        if gr == "NEVER" and (w0 <= a < w1 or (b > a and a < w1 and b > w0)):
            return "NEVER"
    for (w0, w1), gr in wins.items():
        if w0 <= a < w1:
            return gr
    return v.get("grade")


def never_reason(stem: str, t0: float | None = None, t1: float | None = None, grades: Dict[str, dict] | None = None) -> str | None:
    """Why this use of `stem` is banned by the grades: whole-clip NEVER, or a NEVER segment the window touches.
    A segment-graded clip used with no in-point counts as banned (nothing proves the use avoids the NEVER piece)."""
    g = grades if grades is not None else load_grades()
    st = normalise(stem)
    v = g.get(st)
    if not v:
        return None
    if v.get("grade") == "NEVER":
        return f"{st} is graded NEVER by the reviewer (whole clip)"
    nev = [w for w, gr in (v.get("windows") or {}).items() if gr == "NEVER"]
    if not nev:
        return None
    if t0 is None:
        return f"{st} has NEVER segments {[list(w) for w in nev]} and the slot gives no in-point"
    if window_grade(st, t0, t1, g) == "NEVER":
        return f"{st} used at {float(t0):g}s touches a NEVER segment {[list(w) for w in nev]}"
    return None


def operator_stems(grades: Dict[str, dict] | None = None) -> set:
    """Stems the reviewer ruled show the on-camera subject: HERO (whole or any segment) or ME, and not NOTME on a later line."""
    g = grades if grades is not None else load_grades()
    return {s for s, v in g.items() if (_has(v, "HERO") or v.get("ident") == "ME") and v.get("ident") != "NOTME"}


def not_operator_stems(grades: Dict[str, dict] | None = None) -> set:
    g = grades if grades is not None else load_grades()
    return {s for s, v in g.items() if v.get("ident") == "NOTME"}


def broll_stems(grades: Dict[str, dict] | None = None) -> set:
    """Whole-clip B-ROLL (a B-ROLL segment only rules its own window: see identity_slot_reason)."""
    g = grades if grades is not None else load_grades()
    return {s for s, v in g.items() if v.get("grade") == "BROLL"}


def effective_settled(pool_settled: Iterable[str], grades: Dict[str, dict] | None = None) -> set:
    """settled_pool + HERO/ME grades, minus anything later graded NEVER or NOTME."""
    g = grades if grades is not None else load_grades()
    return (set(pool_settled) | operator_stems(g)) - never_stems(g) - not_operator_stems(g)


def identity_slot_reason(stem: str, grades: Dict[str, dict] | None = None, t0: float | None = None, t1: float | None = None) -> str | None:
    """Why `stem` (at in-point t0) may not sit on an identity slot under the grades, or None if allowed.
    With segment grades: the segment holding t0 must be HERO (or the clip settled by ME / whole-clip HERO and
    that segment not B-ROLL/NEVER); a segment-graded clip with no in-point cannot be checked = refused."""
    g = grades if grades is not None else load_grades()
    st = normalise(stem)
    v = g.get(st)
    if not v:
        return None
    if v.get("grade") == "NEVER":
        return f"{st} is graded NEVER by the reviewer (never cast)"
    if v.get("ident") == "NOTME":
        return f"{st} is graded NOT ME by the reviewer (not the subject's identity)"
    if v.get("grade") == "BROLL":
        return f"{st} is graded B-ROLL by the reviewer (never where the reference shows a person)"
    wins = v.get("windows") or {}
    if wins:
        if t0 is None:
            return f"{st} is graded per segment and the slot gives no in-point (segments {sorted(list(w) for w in wins)})"
        wg = window_grade(st, t0, t1, g)
        if wg == "NEVER":
            return f"{st} at {float(t0):g}s is inside a NEVER segment"
        if wg == "BROLL":
            return f"{st} at {float(t0):g}s is inside a B-ROLL segment (never where the reference shows a person)"
        if wg != "HERO" and v.get("ident") != "ME" and v.get("grade") != "HERO":
            return f"{st} at {float(t0):g}s is not inside a HERO segment (HERO segments: {[list(w) for w in hero_windows(st, g)]})"
    return None


def summary(grades: Dict[str, dict] | None = None) -> dict:
    g = grades if grades is not None else load_grades()
    return {"graded": len(g), "hero": len(hero_stems(g)), "broll": len(broll_stems(g)), "never": len(never_stems(g)),
            "segments_graded": sum(len(v.get("windows") or {}) for v in g.values()),
            "me": len({s for s, v in g.items() if v.get("ident") == "ME"}), "notme": len(not_operator_stems(g)),
            "path": str(GRADES_PATH)}


if __name__ == "__main__":
    print(json.dumps(summary(), indent=1))
