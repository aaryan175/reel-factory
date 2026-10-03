"""What calls are open against a project — from every source that declares one.

Calls reach a project by two routes, and until now only one consumer read both:

* ``REEL_REGISTRY.json``'s ``open_calls``, the operator-facing declaration;
* ``<project>/intake.json``'s own ``call``, because ``studio/intake.py`` deliberately does
  not write the registry — folding a record in needs §8.1's single-writer module, which
  does not exist yet.

The daemon's SCHEDULE gate and the board's layer model both read the registry only, so a
studio-born reel was driven straight past ``SELECTION_LOCKED`` — the stage its own mode
call declares it governs — while its record still said ``mode_confirmed: false``. §5.1 is
explicit that mode is not something the app fills in silently; committing to one the
operator never confirmed is exactly that, on the path Task 12 validates.

``calls.inbox`` had the merge all along, which is why it alone reported the gate correctly.
This module is that merge, extracted so there is one implementation rather than one per
consumer — the same reason ``stages.call_gate_index`` exists. A rule duplicated across
consumers diverges invisibly from inside any one of them.

**Why a separate module:** ``calls.py`` imports ``board``, which imports ``status``, so the
merge cannot live in ``calls.py`` without a cycle the moment ``status`` needs it. This
module imports only ``registry`` (itself a leaf), so every consumer can reach it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .registry import open_calls_by_project

#: Written by ``studio/intake.py`` (its ``INTAKE_FILENAME``). Declared here as well because
#: this module must stay a leaf — ``intake`` imports ``registry``, so importing ``intake``
#: from anything ``registry`` can reach would cycle.
INTAKE_RECORD_FILENAME = "intake.json"

#: Where ``calls.answer_call`` files hash-bound answer receipts. Declared here — not in
#: ``calls`` — because answered-ness is part of the assembly, not the presentation: the
#: daemon gates on ``state``, and a call whose latest receipt exists is no longer awaiting
#: anyone. ``calls`` imports this constant so there is one spelling of the directory.
ANSWER_DIRECTORY = "review/calls"

ANSWERED_STATE = "ANSWERED"

REGISTRY_SOURCE = "registry open_calls"
INTAKE_SOURCE = "intake record"


def call_answers(project_dir: Path) -> Dict[str, Dict[str, Any]]:
    """The latest recorded answer per call id. Superseded receipts stay on disk.

    Lives in this leaf (moved from ``calls``) because the assembly must apply it: neither
    ``answer_call`` nor anything else rewrites the intake record's ``call.state`` — the
    record is ``intake``'s artifact and a second writer is exactly what the architecture
    forbids — so the *receipt* is the only durable statement that a call was answered.
    An assembly that ignores receipts returns the intake record's stale
    ``AWAITING_OPERATOR`` forever, and the daemon parks an answered reel indefinitely
    (an observed failure mode: a reel skipped on its already-answered mode call).
    """
    directory = Path(project_dir) / ANSWER_DIRECTORY
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    latest: Dict[str, Dict[str, Any]] = {}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str):
            continue
        payload["receipt_path"] = str(path)
        payload["receipt_name"] = path.name
        current = latest.get(call_id)
        if current is None or int(payload.get("sequence", 1)) >= int(current.get("sequence", 1)):
            latest[call_id] = payload
    return latest


def intake_call(project_dir: Path) -> Optional[Dict[str, Any]]:
    """The intake record's own call, or ``None``. Never raises on a damaged record."""
    path = Path(project_dir) / INTAKE_RECORD_FILENAME
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    call = record.get("call")
    if not isinstance(call, dict) or not call.get("call_id"):
        return None
    return dict(call)


def project_open_calls(report: Mapping[str, Any], project_dir: Path) -> List[Dict[str, Any]]:
    """Every call declared against this project, from both sources, deduped by id.

    The registry wins a tie: it is the operator-facing record, so a call answered there
    must not be reopened by a stale copy in the project's intake record. Each entry carries
    a ``source`` so a consumer can say where a gate came from.

    An answer receipt in ``review/calls/`` overrides the declared ``state`` with
    ``ANSWERED`` — matching ``calls.inbox``, which has always shown answered calls that
    way. Receipts are the durable statement of answered-ness (no writer updates the intake
    record's copy), so without this overlay the daemon and the inbox disagree about the
    one thing the gate exists to decide.
    """
    directory = Path(project_dir)
    declared = open_calls_by_project(report).get(directory.name, [])
    merged: List[Dict[str, Any]] = [dict(call, source=call.get("source") or REGISTRY_SOURCE) for call in declared]
    known = {str(call.get("call_id")) for call in merged if call.get("call_id")}
    local = intake_call(directory)
    if local is not None and str(local["call_id"]) not in known:
        merged.append(dict(local, source=INTAKE_SOURCE))
    answers = call_answers(directory)
    for call in merged:
        answer = answers.get(str(call.get("call_id")))
        if answer is not None:
            call["state"] = ANSWERED_STATE
            call["answered_at_utc"] = answer.get("answered_at_utc")
            call["answer_receipt"] = answer.get("receipt_name")
    return merged


def open_calls_map(report: Mapping[str, Any], projects_root: Path, project_ids: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    """``project_open_calls`` for many projects, shaped for the board's row loop."""
    root = Path(projects_root)
    return {project_id: project_open_calls(report, root / project_id) for project_id in project_ids}
