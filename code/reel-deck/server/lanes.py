"""
Live lane activity — reads the workflow agents' own transcripts so the board
can say WHO is working on a row and what they just did, not only an ETA.

Read-only over agent workflow transcript dirs named by REEL_DECK_WORKFLOW_GLOB
(unset = this feature is off and no lane is ever reported live).
An agent transcript is a .jsonl the runtime appends to on every turn, so the
file's mtime IS the heartbeat. Nothing here ever shows caption words: only the
agent's role line, tool names and file basenames make it to the payload.
"""

from __future__ import annotations

import calendar
import glob
import json
import os
import re
import time
from typing import Any

from . import settings

# e.g. REEL_DECK_WORKFLOW_GLOB="~/.agent-runs/*/workflows/wf_*"
WORKFLOW_GLOB = settings.WORKFLOW_GLOB

ALIVE_S = 900.0        # no append in 15 min = the lane is not working
FRESH_RUN_S = 6 * 3600.0  # ignore run dirs older than this entirely
_CACHE_TTL = 5.0
_TAIL_BYTES = 65536

_ROW_RE = re.compile(r"\b(?:row|reel)\s*#?\s*(\d{1,3})\b", re.IGNORECASE)
_ROLE_RE = re.compile(r"You are the ([A-Z][A-Z ]{2,28}?) agent", re.IGNORECASE)

_cache: dict[str, Any] = {"at": 0.0, "rows": {}}


def _safe_detail(tool: str, tool_input: Any) -> str:
    """A short human line about a tool call with nothing sensitive in it —
    basenames only, never full paths or free text."""
    if not isinstance(tool_input, dict):
        return ""
    text = ""
    for key in ("command", "file_path", "path", "pattern", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = value
            break
    if not text:
        return ""
    names = re.findall(r"[\w][\w.-]*\.(?:mp4|py|json|md|jpg|png|txt|sh)\b", text)
    seen: list[str] = []
    for name in names:
        base = os.path.basename(name)
        if base not in seen:
            seen.append(base)
    if seen:
        return " · ".join(seen[:3])
    first = text.strip().split()[0] if text.strip() else ""
    return os.path.basename(first)[:40]


def _head_lines(path: str, count: int = 4) -> list[str]:
    lines: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(count):
                line = f.readline()
                if not line:
                    break
                lines.append(line)
    except OSError:
        pass
    return lines


def _agent_identity(path: str) -> tuple[int | None, str | None, float | None]:
    """(row sequence, role label, started epoch) from the transcript's first lines."""
    seq: int | None = None
    role: str | None = None
    started: float | None = None
    for line in _head_lines(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if started is None:
            ts = entry.get("timestamp")
            if isinstance(ts, str):
                try:
                    started = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    started = None
        message = entry.get("message")
        blob = ""
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                blob = content
            elif isinstance(content, list):
                blob = " ".join(b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text")
        if blob:
            if seq is None:
                m = _ROW_RE.search(blob)
                if m:
                    seq = int(m.group(1))
            if role is None:
                m = _ROLE_RE.search(blob)
                if m:
                    role = m.group(1).strip().upper()
        if seq is not None and role is not None:
            break
    return seq, role, started


def _last_action(path: str) -> dict[str, Any]:
    """Newest tool call in the transcript tail."""
    out: dict[str, Any] = {"tool": None, "detail": "", "at": None}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - _TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return out
    for line in reversed(tail.split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                out["tool"] = block.get("name")
                out["detail"] = _safe_detail(block.get("name") or "", block.get("input"))
                ts = entry.get("timestamp")
                if isinstance(ts, str):
                    try:
                        out["at"] = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
                    except ValueError:
                        pass
                return out
    return out


def _model_of(jsonl_path: str) -> str | None:
    meta_path = jsonl_path[:-len(".jsonl")] + ".meta.json"
    try:
        meta = json.loads(open(meta_path, encoding="utf-8").read())
        return meta.get("model")
    except (OSError, ValueError):
        return None


def live_rows() -> dict[int, dict[str, Any]]:
    """Map row sequence -> the freshest live agent working on it."""
    now = time.time()
    if now - _cache["at"] < _CACHE_TTL:
        return _cache["rows"]
    rows: dict[int, dict[str, Any]] = {}
    for run_dir in (glob.glob(os.path.expanduser(WORKFLOW_GLOB)) if WORKFLOW_GLOB else []):
        try:
            transcripts = sorted(
                (p for p in glob.glob(os.path.join(run_dir, "agent-*.jsonl"))),
                key=lambda p: os.path.getmtime(p), reverse=True)
        except OSError:
            continue
        if not transcripts:
            continue
        newest = transcripts[0]
        try:
            mtime = os.path.getmtime(newest)
        except OSError:
            continue
        if now - mtime > FRESH_RUN_S:
            continue
        seq, role, started = _agent_identity(newest)
        if seq is None:
            continue
        age = now - mtime
        entry = {
            "seq": seq,
            "role": role or "LANE",
            "model": _model_of(newest),
            "alive": age <= ALIVE_S,
            "age_s": round(age, 1),
            "started_at": started,
            "run_id": os.path.basename(run_dir),
            "restarts": max(0, len(transcripts) - 1),
            "action": _last_action(newest),
        }
        current = rows.get(seq)
        if current is None or mtime > (now - current["age_s"]):
            rows[seq] = entry
    _cache["at"] = now
    _cache["rows"] = rows
    return rows
