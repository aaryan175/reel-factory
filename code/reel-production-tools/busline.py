#!/usr/bin/env python3
"""busline — append ONE ACK/CALL line to a bus receipt, with a machine-readable status block.

Why: deciding "finished / fixing / queued" by running English regexes over free text misreads lines
(a "finished (final for this note)" can look like a fresh pickup). Every lane writes its line through this
helper; the sentence stays for humans, the block on the next line is what the board reads:

    ACK — reel-factory-interactive — 2025-01-20T14:00:06Z — picked up: building v009 …
    <!--status {"v":1,"row":13,"lane":"reel-factory-interactive","at":"2025-01-20T14:00:06Z","state":"picked_up"} -->

    python3 tools/busline.py --receipt <file.md> --lane <lane> --state <state> --text "…" \
            [--why "disk 97GB < 100GB floor"] [--not-before 2025-01-20T10:35:52Z] [--artifact deliver/x.mp4]

States (closed set): picked_up · running · progress · queued · blocked · finished · called.
`called` writes a CALL line (the factory stopped and needs the reviewer); everything else an ACK line.
Append-only, flock-guarded, never rewrites a receipt.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rf_paths  # noqa: E402

# The bus directory: $REEL_FACTORY_BUS_DIR, default $REEL_FACTORY_HOME/_receipts/bus
BUS = Path(os.environ.get("REEL_FACTORY_BUS_DIR") or (rf_paths.RECEIPTS / "bus"))
STATES = ("picked_up", "running", "progress", "queued", "blocked", "finished", "called")
CALL_MAX_CHARS = 320
_OPT_RE = re.compile(r"(?:^|[\s(])(?:RULING\s*)?([12])\s*[=)\.:]\s*\S")


def call_shape_problem(text: str) -> str | None:
    """A CALL is ONE short question with numbered options, never an essay. The detail lives in the
    receipt file; the line the Deck shows is <= CALL_MAX_CHARS and names options `1 = ...` and `2 = ...`."""
    t = " ".join(str(text).split())
    if len(t) > CALL_MAX_CHARS:
        return (f"{len(t)} chars; a CALL line is at most {CALL_MAX_CHARS}. Put the detail in the receipt/report file "
                "and ask ONE short question here: '<question>? 1 = <option>, 2 = <option>'")
    found = {m.group(1) for m in _OPT_RE.finditer(t)}
    if not {"1", "2"} <= found:
        return "a CALL names its options as '1 = ...' and '2 = ...' (the Deck turns them into buttons)"
    return None
_ROW_RE = re.compile(r"-row(\d{2,3})-")


def append(receipt: Path, lane: str, state: str, text: str, *, why: str | None = None,
           not_before: str | None = None, artifact: str | None = None, now: float | None = None) -> str:
    if state not in STATES:
        raise SystemExit(f"state must be one of {', '.join(STATES)}")
    if state in ("queued", "blocked") and not why:
        raise SystemExit("--why is required for queued/blocked (the board shows it to the reviewer)")
    receipt = receipt if receipt.is_absolute() else BUS / receipt
    if not receipt.exists():
        raise SystemExit(f"no such receipt: {receipt}")
    if state == "called":
        bad = call_shape_problem(text)
        if bad:
            raise SystemExit("CALL refused: " + bad)
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))
    m = _ROW_RE.search(receipt.name)
    block = {"v": 1, "row": int(m.group(1)) if m else None, "lane": lane, "at": at, "state": state}
    if why: block["why"] = why[:160]
    if not_before: block["not_before"] = not_before
    if artifact: block["artifact"] = artifact
    head = "CALL" if state == "called" else "ACK"
    label = {"picked_up": "picked up", "called": "", "finished": "finished", "queued": "queued",
             "blocked": "BLOCKED", "running": "running", "progress": "progress"}[state]
    sentence = " ".join(text.split())
    line = f"{head} — {lane} — {at} — " + (f"{label}: " if label else "") + sentence
    payload = line + "\n<!--status " + json.dumps(block, ensure_ascii=False, separators=(",", ":")) + " -->\n"
    with open(receipt, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0, os.SEEK_END)
        if fh.tell():
            fh.seek(fh.tell() - 1); last = fh.read(1)
            if last != "\n":
                fh.write("\n")
        fh.write(payload); fh.flush(); os.fsync(fh.fileno())
        fcntl.flock(fh, fcntl.LOCK_UN)
    return line


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--receipt", required=True, type=Path); ap.add_argument("--lane", required=True)
    ap.add_argument("--state", required=True, choices=STATES); ap.add_argument("--text", required=True)
    ap.add_argument("--why"); ap.add_argument("--not-before"); ap.add_argument("--artifact")
    a = ap.parse_args()
    print(append(a.receipt, a.lane, a.state, a.text, why=a.why, not_before=a.not_before, artifact=a.artifact))
