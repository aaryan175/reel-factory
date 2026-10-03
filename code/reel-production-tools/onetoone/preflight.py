#!/usr/bin/env python3
"""onetoone.preflight — the gate every build/fix lane runs BEFORE it renders and AGAIN before it
writes `finished`. It turns the lessons ledger into something that can actually stop a lane.

    python3 -m onetoone.preflight --row 7 [--cast deliver/cast_v016.json] [--finish]

Checks (any failure = exit 1, and the lane must not render / must not finish):
  1. machine floors — internal disk >= 20 GB, $REEL_FACTORY_WORKDRIVE >= 100 GB, free memory >= 25%
  2. the kit's own tests — every lesson that has a test is enforced here (pytest onetoone/tests)
  3. the cast, when given — the shapes the ledger says must never ship again
     + identity and blacklist: identity slots only from settled_pool, no banned stem/span
  4. lessons — prints the brief for the row; with --finish, blocks while the row's inbox is untriaged
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rf_paths import REEL_HOME, TOOLS, WORKDRIVE  # noqa: E402

ROOT = REEL_HOME
INTERNAL_MIN_GB, WORKBENCH_MIN_GB, MEM_MIN_PCT = 20, 100, 25


def _free_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1e9


def _mem_free_pct() -> float | None:
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
        line = [l for l in out.splitlines() if "free percentage" in l.lower()]
        return float(line[-1].split(":")[-1].strip().rstrip("%")) if line else None
    except Exception:
        return None


def machine() -> list[str]:
    fails = []
    i, w = _free_gb("/"), _free_gb(str(WORKDRIVE)) if WORKDRIVE.exists() else 0.0
    if i < INTERNAL_MIN_GB:
        fails.append(f"internal disk {i:.0f} GB free < {INTERNAL_MIN_GB} GB (L0011)")
    if w < WORKBENCH_MIN_GB:
        fails.append(f"{WORKDRIVE} {w:.0f} GB free < {WORKBENCH_MIN_GB} GB floor (L0011)")
    m = _mem_free_pct()
    if m is not None and m < MEM_MIN_PCT:
        fails.append(f"free memory {m:.0f}% < {MEM_MIN_PCT}%")
    print(f"machine: internal {i:.0f} GB · workbench {w:.0f} GB · mem free {m if m is not None else '?'}%")
    return fails


def _has_pytest(py: str) -> bool:
    try:
        return subprocess.run([py, "-c", "import pytest"], capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


def kit_python() -> str:
    """L0051: an interpreter that actually has pytest. A bare interpreter without pytest
    would report a false 'kit tests FAILED: ?' on every --finish gate. Order: the
    REEL_FACTORY_TEST_PYTHON override, this interpreter, then the system python3."""
    for py in (os.environ.get("REEL_FACTORY_TEST_PYTHON"), sys.executable, "/usr/bin/python3"):
        if py and Path(py).exists() and _has_pytest(py):
            return py
    return sys.executable


def kit_tests() -> list[str]:
    py = kit_python()
    r = subprocess.run([py, "-m", "pytest", "-q", "-x", str(TOOLS / "onetoone" / "tests")],
                       capture_output=True, text=True, cwd=str(TOOLS))
    tail = (r.stdout.strip().splitlines() or ["?"])[-1]
    print(f"kit tests: {tail}")
    return [] if r.returncode == 0 else [f"kit tests FAILED: {tail}"]


def cast_rules(cast: Path) -> list[str]:
    fails = []
    d = json.loads(cast.read_text())
    ink = d.get("caption_ink")
    if ink and list(ink) == [255, 255, 255] and not d.get("caption_shadow"):
        fails.append("flat white ink with no caption_shadow: white vanished on 5 of 13 states in v012 (audit HARD-3)")
    sh = d.get("caption_shadow") or {}
    if sh and float(sh.get("radius", 0)) > 20:
        fails.append("caption_shadow radius > 20 reads as a glow, not a shadow (audit MED-4)")
    if d.get("grade_mode") not in (None, "match", "natural", "house"):
        fails.append(f"unknown grade_mode {d.get('grade_mode')!r}")
    print(f"cast: {cast.name} · ink {ink or 'per-state'} · shadow {'yes' if sh else 'no'} · grade {d.get('grade_mode', 'match')}")
    return fails


def identity_rules(cast: Path) -> list[str]:
    """L0052: identity pool and blacklist are code, not prose. Runs only with --cast."""
    from onetoone.identity import check_cast
    fails = check_cast(cast)
    print(f"identity: {cast.name} · {'OK' if not fails else str(len(fails)) + ' violation(s)'}")
    return [f"identity (L0052): {f}" for f in fails]


def lessons(row: int | None, finish: bool) -> list[str]:
    sub = subprocess.run([sys.executable, str(TOOLS / "lessons.py"), "harvest"], capture_output=True, text=True)
    print(sub.stdout.strip())
    brief = subprocess.run([sys.executable, str(TOOLS / "lessons.py"), "brief"] + (["--row", str(row)] if row else []),
                           capture_output=True, text=True).stdout
    print(brief)
    if finish:
        chk = subprocess.run([sys.executable, str(TOOLS / "lessons.py"), "check"] + (["--row", str(row)] if row else []),
                             capture_output=True, text=True)
        print(chk.stdout.strip())
        if chk.returncode != 0:
            return ["untriaged inbox items for this row — triage them into LESSONS.md (tools/lessons.py triage) before `finished`"]
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--row", type=int)
    ap.add_argument("--cast", type=Path)
    ap.add_argument("--finish", action="store_true", help="the pre-`finished` gate: inbox must be triaged")
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    fails = machine()
    if not a.skip_tests:
        fails += kit_tests()
    if a.cast:
        fails += cast_rules(a.cast)
        fails += identity_rules(a.cast)
    fails += lessons(a.row, a.finish)
    if fails:
        print("\nPREFLIGHT: BLOCKED")
        for f in fails:
            print(f"  ✗ {f}")
        sys.exit(1)
    if a.cast:
        # the token render() requires: keyed on the cast bytes, 6 h life
        from onetoone.render import GATE_DIR, gate_token
        GATE_DIR.mkdir(parents=True, exist_ok=True)
        tok = gate_token(a.cast)
        from onetoone.render import _sha, lessons_sha
        tok.write_text(json.dumps({"cast": str(a.cast), "cast_sha256": _sha(a.cast), "lessons_sha256": lessons_sha(),
                                   "row": a.row, "at": __import__("time").time()}))
        print(f"preflight token: {tok.name}")
    print("\nPREFLIGHT: OK")


if __name__ == "__main__":
    main()
