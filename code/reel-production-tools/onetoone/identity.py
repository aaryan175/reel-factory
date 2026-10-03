#!/usr/bin/env python3
"""onetoone.identity — the identity pool and the shot blacklist as code.

    from onetoone.identity import load_pool, is_blacklisted, assert_cast_identity
    python3 -m onetoone.identity <cast.json>          # exit 1 with the reasons if the cast breaks the law

Data: onetoone/identity_pool.json (settled_pool, operator_confirmed, not_operator, blacklist_full,
blacklist_spans, unruled, sources). Every entry cites its source and the human ruling that set it.
Shipped EMPTY in this handover: fill it from your own rulings by clip number. Entry shapes:
  blacklist_full : {"stem": "CLIP_0061", "kind": "FULL_BAN", "match_keys": [<sha256>, <file name>, <drive id>], "ruling": "..."}
  blacklist_spans: {"stem": "CLIP_0074", "banned_windows_s": [[0.0, 8.0]], "reason": "..."}
  unruled        : {"stem": "CLIP_0003", "why": "..."}

The law, enforced by assert_cast_identity():
  * An IDENTITY slot is a slot whose `identity` is "subject" (or listed in the cast's top-level
    `identity_slots`). It may only use a stem in `settled_pool`.
  * EVERY slot, identity or not, is checked against `blacklist_full` (any identifier: stem, file name,
    content id, sha256, Drive id) and against `blacklist_spans` (the in-point window).
  * Every slot must DECLARE its identity: `"identity": "subject"` or `"identity": "none"` (no person,
    or a person-free window). An undeclared slot fails: an unchecked identity is how prose gates leaked.
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

POOL_PATH = Path(__file__).resolve().with_name("identity_pool.json")
#: Slot identity value meaning "the primary on-camera subject". "operator" is accepted as a
#: legacy alias for casts written before the rename.
IDENTITY_SUBJECT = "subject"
IDENTITY_ALIASES = {"subject", "operator"}
IDENTITY_OPERATOR = IDENTITY_SUBJECT  # backward-compatible name
IDENTITY_NONE = {"none", "no-person", "person-free", "nobody"}


class IdentityViolation(ValueError):
    """Raised when a cast breaks the identity or blacklist law."""


@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def load_pool(path: str | Path | None = None) -> dict:
    """The parsed identity_pool.json (cached per path)."""
    return _load(str(path or POOL_PATH))


def normalise(ident: str) -> str:
    """'…/masters/0123456789abcdef__CLIP_0061.MP4' -> 'CLIP_0061'. Hashes and Drive ids pass through."""
    s = str(ident).strip()
    name = s.replace("\\", "/").rsplit("/", 1)[-1]
    if "__" in name:
        name = name.split("__")[-1]
    if "." in name and name.rsplit(".", 1)[-1].lower() in ("mp4", "mov", "mxf", "m4v"):
        name = name.rsplit(".", 1)[0]
    return name


def _keys(entry: dict) -> set[str]:
    return {str(k) for k in entry.get("match_keys") or []} | {entry["stem"]}


def _candidates(ident: str) -> set[str]:
    s = str(ident).strip()
    return {s, normalise(s), s.replace("\\", "/").rsplit("/", 1)[-1]}


def _full_ban(ident: str, pool: dict) -> dict | None:
    c = _candidates(ident)
    for e in pool["blacklist_full"]:
        if c & _keys(e):
            return e
    return None


def _span(ident: str, pool: dict) -> dict | None:
    c = _candidates(ident)
    for e in pool["blacklist_spans"]:
        if c & _keys(e):
            return e
    return None


def is_blacklisted(stem: str, t0: float | None = None, t1: float | None = None, pool: dict | None = None) -> bool:
    """True if `stem` (or any identifier of it) is a full ban, or if [t0, t1) touches a banned window
    of a span-restricted master. A span-restricted master with no time given counts as banned
    (nothing proves the use sits in the allowed range). t1=None checks the in-point t0 alone."""
    return blacklist_reason(stem, t0, t1, pool) is not None


def blacklist_reason(stem: str, t0: float | None = None, t1: float | None = None, pool: dict | None = None) -> str | None:
    pool = pool or load_pool()
    e = _full_ban(stem, pool)
    if e:
        return f"{e['stem']} is a FULL ban: {(e.get('ruling') or e.get('reason') or '')[:160]}"
    s = _span(stem, pool)
    if not s:
        return None
    if t0 is None:
        return f"{s['stem']} is span-restricted and the slot gives no in-point (banned windows {s['banned_windows_s']})"
    a = float(t0)
    b = float(t1) if t1 is not None else None
    for lo, hi in s["banned_windows_s"]:
        hit = (lo <= a < hi) if b is None or b <= a else (a < hi and b > lo)
        if hit:
            span = f"{a:g}s" if b is None or b <= a else f"{a:g}-{b:g}s"
            return f"{s['stem']} used at {span}, inside the banned window {lo:g}-{hi:g}s ({(s.get('reason') or '')[:120]})"
    return None


def _slot_stem(slot: dict) -> str | None:
    for k in ("stem", "master", "path", "file", "source"):
        if slot.get(k):
            return str(slot[k])
    return None


def _slot_window(slot: dict) -> tuple[float | None, float | None]:
    t0 = next((slot[k] for k in ("in_s", "inpoint", "inpt", "t0") if isinstance(slot.get(k), (int, float))), None)
    if t0 is None:
        return None, None
    dur = next((slot[k] for k in ("dur_s", "len", "slot_seconds", "duration") if isinstance(slot.get(k), (int, float))), None)
    if dur is None and isinstance(slot.get("out_s"), (int, float)):
        return float(t0), float(slot["out_s"])
    return float(t0), (float(t0) + float(dur)) if dur else None


def _slots(cast: dict | list) -> list[dict]:
    sl = cast.get("slots", []) if isinstance(cast, dict) else cast
    if isinstance(sl, dict):
        sl = [dict(v, slot=v.get("slot", k)) if isinstance(v, dict) else v for k, v in sl.items()]
    return [s for s in sl if isinstance(s, dict)]


def check_cast(cast_json_path: str | Path, pool: dict | None = None, grades: dict | None = None) -> list[str]:
    """Every violation as a readable line; [] means the cast is clean.
    The reviewer's clip grades are part of the law: NEVER = full ban, HERO/ME =
    settled for identity slots, BROLL/NOTME = never on an identity slot."""
    from onetoone import grades as gr
    pool = pool or load_pool()
    grades = gr.load_grades() if grades is None else grades
    p = Path(cast_json_path)
    cast = json.loads(p.read_text())
    settled = gr.effective_settled(pool["settled_pool"], grades)
    unruled = {u["stem"] for u in pool.get("unruled", [])}
    declared = set(cast.get("identity_slots") or []) if isinstance(cast, dict) else set()
    slots = _slots(cast)
    if not slots:
        return [f"{p.name}: no slots found, nothing can be checked"]
    fails = []
    for i, s in enumerate(slots):
        name = s.get("slot", f"#{i}")
        raw = _slot_stem(s)
        if not raw:
            fails.append(f"{name}: slot names no stem/master")
            continue
        stem = normalise(raw)
        ident = str(s.get("identity", "")).strip().lower() or (IDENTITY_SUBJECT if name in declared else "")
        if ident in IDENTITY_ALIASES:
            ident = IDENTITY_SUBJECT
        t0, t1 = _slot_window(s)
        why = blacklist_reason(raw, t0, t1, pool) or (blacklist_reason(stem, t0, t1, pool) if stem != raw else None)
        if why:
            fails.append(f"{name}: BLACKLISTED — {why}")
        else:
            nwhy = gr.never_reason(stem, t0, t1, grades)
            if nwhy:
                fails.append(f"{name}: BLACKLISTED — {nwhy} (Deck grader, L0065)")
        if ident == IDENTITY_SUBJECT:
            gwhy = gr.identity_slot_reason(stem, grades, t0, t1)
            if gwhy:
                fails.append(f"{name}: identity slot — {gwhy} (L0065)")
            elif stem not in settled:
                extra = f" ({stem} is unruled: no human ruling on it by number yet)" if stem in unruled else ""
                fails.append(f"{name}: identity slot uses {stem}, which is not in settled_pool and not graded HERO/ME in the Deck grader{extra}")
        elif ident in IDENTITY_NONE:
            pass
        else:
            fails.append(f"{name}: identity not declared — set \"identity\": \"subject\" (stem must be in settled_pool) "
                         f"or \"identity\": \"none\" (no person / person-free window)")
    fails += refpeople_rules(p, slots, grades)
    return fails


def _slot_ref_window(slot: dict, cutgrid: dict | None) -> tuple[int, int] | None:
    """The reference frame window a cast slot stands in: the slot's own ref_in/ref_out (or in/out)
    frames, else the cutgrid shot of the same id (cutgrid schema `shots`), else the picture block
    (block schema `picture_blocks`, P1.. ids). None when nothing resolves."""
    for a, b in (("ref_in", "ref_out"), ("in", "out")):
        if isinstance(slot.get(a), int) and isinstance(slot.get(b), int):
            return int(slot[a]), int(slot[b])
    if not cutgrid:
        return None
    sid = str(slot.get("slot", ""))
    for sh in cutgrid.get("shots") or []:
        if str(sh.get("slot")) == sid and isinstance(sh.get("in"), int) and isinstance(sh.get("out"), int):
            return int(sh["in"]), int(sh["out"])
    for pb in cutgrid.get("picture_blocks") or []:
        fr = pb.get("frames")
        if str(pb.get("id")) == sid and isinstance(fr, list) and len(fr) == 2:
            return int(fr[0]), int(fr[1])
    return None


def refpeople_rules(cast_path: Path, slots: list[dict], grades: dict | None = None) -> list[str]:
    """L0065: where the reference shows a person, the slot is an
    identity slot and takes a HERO/settled clip, never b-roll. Needs <row>/brain/refpeople.json
    (python3 -m onetoone.refpeople <row dir>); without it the rule prints a warning and passes, so a
    row measured before is not blocked by a file it never had."""
    from onetoone import grades as gr
    from onetoone import refpeople as rp
    row_dir = cast_path.resolve().parent.parent
    rp_path = row_dir / "brain" / "refpeople.json"
    if not rp_path.exists():
        print(f"refpeople: {rp_path} missing — run `python3 -m onetoone.refpeople {row_dir}` so the person rule can run (L0065)")
        return []
    data = json.loads(rp_path.read_text())
    cg_path = row_dir / "brain" / "cutgrid.json"
    cutgrid = json.loads(cg_path.read_text()) if cg_path.exists() else None
    grades = gr.load_grades() if grades is None else grades
    heroes = gr.hero_stems(grades)
    fails = []
    for i, s in enumerate(slots):
        name = s.get("slot", f"#{i}")
        win = _slot_ref_window(s, cutgrid)
        if win is None:
            continue
        verdict = rp.window_verdict(data, win[0], win[1])
        if verdict["person"] and str(s.get("identity", "")).strip().lower() not in IDENTITY_ALIASES:
            fails.append(f"{name}: the reference shows a person in f{win[0]}–f{win[1]} (face on {verdict['with_face']}/{verdict['sampled']} sampled frames) "
                         f"but the slot is identity \"{s.get('identity')}\" — declare identity subject and cast a HERO/settled clip, never an empty bed (L0065)")
        if verdict["person"] and heroes and normalise(_slot_stem(s) or "") not in heroes:
            print(f"refpeople: {name} shows a person in the reference; {len(heroes)} HERO clips are graded and this slot uses "
                  f"{normalise(_slot_stem(s) or '')} (not HERO) — prefer a HERO clip (L0065)")
    return fails


def assert_cast_identity(cast_json_path: str | Path, pool: dict | None = None) -> None:
    """Raise IdentityViolation listing every slot that breaks the identity or blacklist law."""
    fails = check_cast(cast_json_path, pool)
    if fails:
        raise IdentityViolation(f"{Path(cast_json_path).name}: {len(fails)} identity/blacklist violation(s):\n  " + "\n  ".join(fails))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 -m onetoone.identity <cast.json>")
    try:
        assert_cast_identity(sys.argv[1])
    except IdentityViolation as e:
        print(e)
        sys.exit(1)
    print("identity: OK")
