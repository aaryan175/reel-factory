#!/usr/bin/env python3
"""The Reel Factory learning loop — every reviewer comment, verdict, ruling and audit failure becomes
a durable lesson that every later lane must read before it builds.

Files (all under $REEL_FACTORY_HOME, append-only):
  LESSONS.md          human ledger, newest first per section — what the factory must never repeat
  lessons.json        the same lessons machine-readable + the `seen` set of harvested sources
  LESSONS_INBOX.md    raw, untriaged signal the harvester found (reviewer words, audit verdicts, CALLs)
                      — a lane that touches a row must triage that row's inbox items into lessons
                      (or mark them "no lesson") before it may write `finished`.

Commands:
  python3 tools/lessons.py harvest            scan the bus + every audit-*/REPORT.md + WALLS.md for new signal
  python3 tools/lessons.py add --row 11 --source "audit-v014" --symptom "..." --rule "..." [--check test_name] [--tags a,b]
  python3 tools/lessons.py triage <inbox_id> --lesson <lesson_id> | --none "why"
  python3 tools/lessons.py brief [--row 11] [--tags grade,captions]   print the lessons a lane must read
  python3 tools/lessons.py check [--row 11]   exit 1 if that row has untriaged inbox items
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rf_paths  # noqa: E402

ROOT = rf_paths.REEL_HOME
LEDGER_MD = ROOT / "LESSONS.md"
LEDGER_JSON = ROOT / "lessons.json"
INBOX_MD = ROOT / "LESSONS_INBOX.md"
BUS = Path(os.environ.get("REEL_FACTORY_BUS_DIR") or (rf_paths.RECEIPTS / "bus"))
WORKBENCH = rf_paths.WORKBENCH
LOCK = ROOT / ".lessons.lock"

OPERATOR_RE = re.compile(r"^Sent by: .*\(OPERATOR\)", re.M)
ROW_RE = re.compile(r"row\s*(\d+)", re.I)
VERBATIM_RE = re.compile(r"(?:Operator (?:note|text) \(verbatim\):|RULING on [^\n]+:)\s*\n?(.*?)(?:\n---|\Z)", re.S)
CALL_RE = re.compile(r"^CALL — (.+)$", re.M)
VERDICT_RE = re.compile(r"^\*{0,2}VERDICT\*{0,2}.*?\n+(.*?)(?:\n\n|\Z)", re.S | re.M)
HARD_RE = re.compile(r"^(?:\*\*|-\s*\*\*|#+\s*)?(HARD[- ]?\d+[^\n]*)", re.M)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> Dict[str, Any]:
    if LEDGER_JSON.exists():
        return json.loads(LEDGER_JSON.read_text())
    return {"lessons": [], "inbox": [], "seen": []}


def _save(data: Dict[str, Any]) -> None:
    tmp = LEDGER_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, LEDGER_JSON)


class locked:
    def __enter__(self):
        self.fh = open(LOCK, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def _append(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


# ----------------------------------------------------------------------------- harvest --
def _row_of(name: str, text: str) -> int | None:
    m = re.search(r"row(\d+)", name) or re.search(r"reel(\d+)", name) or ROW_RE.search(text[:400])
    return int(m.group(1)) if m else None


PLUMBING_RE = re.compile(r"SELF-TEST|SYSTEM CHECK|smoketest|plumbing|\[CDP WALK\]|take NO other action", re.I)
PASS_RE = re.compile(r"\b(PROVEN|PASS(?:ED)?|fully proven|genuinely fixed|no blocking defect|fit to put in front)\b", re.I)
# Case-insensitive like PASS_RE, so "not proven, still fails" is never filed as "a pass teaches nothing".
NEGATIVE_RE = re.compile(r"\b(FAIL(?:S|ED|URE)?|BLOCKED|NOT|HARD|DEFECT|REMAIN(?:S|ING)?|OWED|STILL|TRUNCAT\w*|CANNOT|MISSING)\b", re.I)
# Optional YYYYMMDD cut-off: sources dated before it are harvested for the record but auto-triaged.
LOOP_EPOCH = os.environ.get("LESSONS_EPOCH", "")


def _source_date(source: str) -> str:
    m = re.search(r"(20\d{6})", source)
    return m.group(1) if m else "99999999"


def _inbox_add(data: Dict[str, Any], source: str, row: int | None, kind: str, text: str) -> bool:
    text = " ".join(text.split())
    if not text:
        return False
    auto = None
    if PLUMBING_RE.search(text):
        auto = "plumbing test, not a signal"
    elif kind == "audit-verdict" and PASS_RE.search(text) and not NEGATIVE_RE.search(text):
        auto = "a pass teaches nothing"
    elif LOOP_EPOCH and _source_date(source) < LOOP_EPOCH:
        auto = f"pre-loop backlog (before {LOOP_EPOCH})"
    key = f"{source}::{kind}::{text[:80]}"
    if key in data["seen"]:
        return False
    iid = f"in{len(data['inbox']) + 1:04d}"
    data["inbox"].append({"id": iid, "at": _now(), "source": source, "row": row, "kind": kind,
                          "text": text[:1200], "triage": ({"lesson": None, "none": auto, "at": _now(), "auto": True} if auto else None)})
    data["seen"].append(key)
    _append(INBOX_MD, f"- **{iid}** · row {row if row is not None else '?'} · {kind} · `{source}`" + (f" · auto: {auto}" if auto else "") + f"\n  > {text[:600]}")
    return True


def harvest() -> int:
    """Pull every untriaged signal into the inbox. Idempotent."""
    new = 0
    with locked():
        data = _load()
        # 1. the bus — EVERY receipt, not just ui-*: reviewer AND editor words, bare dispositions,
        #    batch verdicts, continue presses, factory CALLs, and audit VERDICT/HARD lines that
        #    live in lane receipts rather than REPORT.md.
        for f in sorted(BUS.glob("*.md")):
            if f.name.startswith("ui-smoketest"):
                continue
            text = f.read_text(errors="replace")
            row = _row_of(f.name, text)
            who = "operator" if OPERATOR_RE.search(text) or "Sent by: main-session" in text else (
                  "editor" if re.search(r"^Sent by: .*\(EDITOR\)", text, re.M) else None)
            if f.name.startswith("ui-"):
                disp = re.search(r"^Disposition:\s*(\w+)", text, re.M)
                got_text = False
                for m in VERBATIM_RE.finditer(text):
                    kind = f"{who or 'unknown'}-" + ("ruling" if "RULING" in m.group(0)[:40] else "note")
                    if disp and disp.group(1).upper() == "REJECT":
                        kind = f"{who or 'unknown'}-REJECT"
                    new += _inbox_add(data, f.name, row, kind, m.group(1)); got_text = True
                if disp and not got_text:                       # a bare APPROVE / REJECT / NOTES
                    watched = re.search(r"^Watched file:\s*(.+)$", text, re.M)
                    new += _inbox_add(data, f.name, row, f"{who or 'unknown'}-{disp.group(1).upper()}",
                                      f"{disp.group(1).upper()} on {(watched.group(1).strip() if watched else 'the cut')} (no text)")
                if f.name.startswith("ui-batch-verdict"):
                    for m in re.finditer(r"^##\s*(Variant\s*\S+)\s*[—:-]\s*(KEEP|KILL|NOTES)\b(.*)$", text, re.M | re.I):
                        new += _inbox_add(data, f.name, row, "operator-batch-verdict", f"{m.group(2).upper()} {m.group(1)} {m.group(3).strip()}")
                if f.name.startswith("ui-continue"):
                    new += _inbox_add(data, f.name, row, "operator-continue", "Continue pressed (carry on where you left off)")
            else:
                for m in VERDICT_RE.finditer(text):
                    new += _inbox_add(data, f.name, row, "audit-verdict", m.group(1))
                for m in HARD_RE.finditer(text):
                    new += _inbox_add(data, f.name, row, "audit-HARD", m.group(1))
            for m in CALL_RE.finditer(text):
                new += _inbox_add(data, f.name, row, "factory-CALL", m.group(1))
        # 2. audit reports: verdict line + every HARD finding
        for rep in sorted(set(WORKBENCH.glob("reel*/audit-*/*.md")) | set(WORKBENCH.glob("reel*/*INDEP-AUDIT*/*.md"))):
            text = rep.read_text(errors="replace")
            row = _row_of(rep.parts[-3], text)
            src = str(rep.relative_to(WORKBENCH))
            v = VERDICT_RE.search(text)
            if v:
                new += _inbox_add(data, src, row, "audit-verdict", v.group(1))
            for h in HARD_RE.finditer(text):
                new += _inbox_add(data, src, row, "audit-HARD", h.group(1))
        # 3. WALLS.md — every "Wall:" / "defect" line the lanes wrote about themselves
        for walls in sorted(WORKBENCH.glob("reel*/brain/WALLS.md")):
            text = walls.read_text(errors="replace")
            row = _row_of(walls.parts[-3], text)
            for line in text.splitlines():
                if re.match(r"^\s*(\*\*Wall|- \*\*|## .*(defect|fixed|FAIL))", line, re.I) and len(line) > 40:
                    new += _inbox_add(data, str(walls.relative_to(WORKBENCH)), row, "walls", line.strip("-* "))
        _save(data)
    return new


# --------------------------------------------------------------------------------- add --
def add(row: int | None, source: str, symptom: str, rule: str, check: str | None, tags: List[str]) -> str:
    with locked():
        data = _load()
        lid = f"L{len(data['lessons']) + 1:04d}"
        entry = {"id": lid, "at": _now(), "row": row, "source": source, "symptom": symptom, "rule": rule,
                 "check": check, "tags": tags, "enforced": "test" if check else "rule"}
        data["lessons"].append(entry)
        _save(data)
        rewrite_ledger(data)
    return lid


LEDGER_HEAD_END = "Newest lessons are at the bottom; `brief` prints them newest first.\n"


def rewrite_ledger(data: Dict[str, Any]) -> None:
    """LESSONS.md is a VIEW of lessons.json (never hand-edited, so it cannot drift from the data).
    The header above LEDGER_HEAD_END is kept."""
    head = LEDGER_MD.read_text() if LEDGER_MD.exists() else ""
    i = head.find(LEDGER_HEAD_END)
    head = head[: i + len(LEDGER_HEAD_END)] if i >= 0 else head
    LEDGER_MD.write_text(head + "".join(_md_lesson(e) for e in data["lessons"]))


def _md_lesson(e: Dict[str, Any]) -> str:
    tags = ", ".join(e["tags"]) if e["tags"] else "—"
    chk = f"`{e['check']}`" if e["check"] else "rule only — no test yet"
    return (f"\n### {e['id']} · {e['at'][:10]} · row {e['row'] if e['row'] is not None else '—'} · {tags}\n"
            f"- **What went wrong:** {e['symptom']}\n"
            f"- **Rule:** {e['rule']}\n"
            f"- **Enforced by:** {chk}\n"
            f"- **Source:** {e['source']}\n")


# ------------------------------------------------------------------------------ triage --
def triage(inbox_id: str, lesson: str | None, none: str | None) -> None:
    with locked():
        data = _load()
        items = [i for i in data["inbox"] if i["id"] == inbox_id]
        if not items:
            raise SystemExit(f"{inbox_id}: not in inbox")
        items[0]["triage"] = {"lesson": lesson, "none": none, "at": _now()}
        _save(data)
        _append(INBOX_MD, f"  - triaged {inbox_id} → {lesson or 'no lesson: ' + (none or '')}")


# ------------------------------------------------------------------------------- brief --
def brief(row: int | None, tags: List[str]) -> str:
    data = _load()
    out = ["# LESSONS BRIEF — read before you build (newest first)"]
    les = list(reversed(data["lessons"]))
    if tags:
        les = [l for l in les if set(l["tags"]) & set(tags)] + [l for l in les if not (set(l["tags"]) & set(tags))]
    for l in les:
        star = "★" if (row is not None and l["row"] == row) else " "
        out.append(f"{star} {l['id']} [{', '.join(l['tags'])}] {l['rule']}  (enforced: {l['enforced']}"
                   f"{': ' + l['check'] if l['check'] else ''})")
    open_items = [i for i in data["inbox"] if i["triage"] is None and (row is None or i["row"] == row)]
    if open_items:
        out.append(f"\n## UNTRIAGED INBOX ({len(open_items)}) — triage before `finished`")
        for i in open_items:
            out.append(f"- {i['id']} row {i['row']} {i['kind']}: {i['text'][:200]}")
    return "\n".join(out)


def check(row: int | None) -> int:
    data = _load()
    open_items = [i for i in data["inbox"] if i["triage"] is None and (row is None or i["row"] == row)]
    if open_items:
        print(f"BLOCK: {len(open_items)} untriaged inbox item(s)" + (f" for row {row}" if row else "") +
              ": " + ", ".join(i["id"] for i in open_items))
        return 1
    print("OK: inbox triaged")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest")
    sub.add_parser("rewrite", help="regenerate LESSONS.md from lessons.json")
    sc = sub.add_parser("setcheck"); sc.add_argument("lesson_id"); sc.add_argument("check")
    a = sub.add_parser("add"); a.add_argument("--row", type=int); a.add_argument("--source", required=True)
    a.add_argument("--symptom", required=True); a.add_argument("--rule", required=True); a.add_argument("--check")
    a.add_argument("--tags", default="")
    t = sub.add_parser("triage"); t.add_argument("inbox_id"); t.add_argument("--lesson"); t.add_argument("--none")
    b = sub.add_parser("brief"); b.add_argument("--row", type=int); b.add_argument("--tags", default="")
    c = sub.add_parser("check"); c.add_argument("--row", type=int)
    sw = sub.add_parser("swept", help="list auto-triaged inbox items")
    sw.add_argument("--reason", default="", help="substring of the auto-triage reason, e.g. pre-loop"); sw.add_argument("--row", type=int)
    args = ap.parse_args()
    if args.cmd == "rewrite":
        with locked():
            rewrite_ledger(_load()); print("LESSONS.md rewritten from lessons.json")
        return
    if args.cmd == "setcheck":
        with locked():
            d = _load()
            for l in d["lessons"]:
                if l["id"] == args.lesson_id: l["check"] = args.check; l["enforced"] = "test"
            _save(d); rewrite_ledger(d); print("ok")
        return
    if args.cmd == "harvest":
        n = harvest(); print(f"harvested {n} new inbox item(s) → {INBOX_MD}")
    elif args.cmd == "add":
        print(add(args.row, args.source, args.symptom, args.rule, args.check,
                  [x for x in args.tags.split(",") if x]))
    elif args.cmd == "triage":
        triage(args.inbox_id, args.lesson, args.none); print("ok")
    elif args.cmd == "brief":
        print(brief(args.row, [x for x in args.tags.split(",") if x]))
    elif args.cmd == "check":
        sys.exit(check(args.row))
    elif args.cmd == "swept":
        for it in _load()["inbox"]:
            t = it.get("triage") or {}
            if t.get("auto") and args.reason.lower() in (t.get("none") or "").lower() and (args.row is None or it.get("row") == args.row):
                print(f"{it['id']}  row {it.get('row')}  {it['kind']}  [{t.get('none')}]  {it['source']}\n    {it['text'][:160]}")


if __name__ == "__main__":
    main()
