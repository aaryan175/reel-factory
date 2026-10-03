"""What the daemon is allowed to do about each of the fourteen stages.

Three kinds, and the boundary between them is evidence on disk rather than a hard-coded
list. A stage is ``deterministic`` when a ``reelctl`` command can complete it from inputs
that already exist; it becomes ``judgment`` when one of those inputs is still an
unauthored draft, because writing that draft is exactly the work a headless worker does
. The three approval stages are ``human`` and the daemon never
runs them at all.

The draft test is deliberately blunt: ``reelctl``'s own templates write
``{"status": "DRAFT", ...}``, so a top-level ``DRAFT`` status means "nobody has looked at
this yet". No path is invented here — every relative path below is one the CLI reads or
writes today.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..hashing import recipe_hash, sha256_file
from ..state import STAGES

DETERMINISTIC = "deterministic"
JUDGMENT = "judgment"
CAPTURE = "capture"
HUMAN = "human"

AUTHORED = "AUTHORED"
DRAFT = "DRAFT"
MISSING = "MISSING"
UNREADABLE = "UNREADABLE"


@dataclass(frozen=True)
class StagePlan:
    stage: str
    kind: str
    argv: Tuple[str, ...] = ()
    drafts: Tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class JobSpec:
    project_id: str
    stage: str
    kind: str
    argv: Tuple[str, ...] = ()
    reason: Optional[str] = None
    note: str = ""
    drafts: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    #: The queue row this attempt belongs to, attached by the daemon when it runs the job.
    #: A spec built for scheduling has none yet, so receipts fall back to a derived name.
    job_id: Optional[int] = None


def _plan(stage: str, kind: str, argv: Tuple[str, ...] = (), drafts: Tuple[str, ...] = (), note: str = "") -> Tuple[str, StagePlan]:
    return stage, StagePlan(stage=stage, kind=kind, argv=argv, drafts=drafts, note=note)


_QC = ("qc", "{project_id}", "--revision", "{revision}")

STAGE_PLANS: Dict[str, StagePlan] = dict(
    [
        _plan("REFERENCE_LOCKED", DETERMINISTIC, ("reference", "analyze", "{project_id}")),
        _plan(
            "BLUEPRINT_LOCKED",
            JUDGMENT,
            note=(
                "Boundaries, hard cuts and per-block observations come from watching the reference at "
                "normal speed; reelctl blueprint lock cannot invent them."
            ),
        ),
        _plan("FOOTAGE_INDEXED", DETERMINISTIC, ("footage", "index", "{project_id}")),
        _plan(
            "FEASIBILITY_REPORTED",
            DETERMINISTIC,
            ("feasibility", "lock", "{project_id}"),
            drafts=("edit/feasibility.json",),
            note="Every locked picture state must be mapped against the full inventory before it can be locked.",
        ),
        _plan(
            "SELECTION_LOCKED",
            DETERMINISTIC,
            ("selection", "validate", "{project_id}"),
            drafts=("edit/selection.json",),
            note="Clip choice, source windows and profile proofs are judgment; validation is not.",
        ),
        _plan(
            "ASSETS_LOCKED",
            DETERMINISTIC,
            ("assets", "lock", "{project_id}"),
            drafts=("assets/assets.json",),
            note="Typography and effect layers must be inventoried from the reference before they can be locked.",
        ),
        _plan("RENDERED", DETERMINISTIC, ("render", "{project_id}", "--revision", "{revision}")),
        _plan("TECHNICAL_QC", DETERMINISTIC, _QC),
        _plan("STRUCTURE_QC", DETERMINISTIC, _QC),
        _plan(
            "VISUAL_QC",
            DETERMINISTIC,
            _QC,
            drafts=("review/agent-visual-review-{revision}.json",),
            note="Visual QC stays BLOCKED until a hash-bound agent watch receipt exists for this revision.",
        ),
        _plan("LOCAL_REVIEW_READY", DETERMINISTIC, _QC),
        _plan("HUMAN_APPROVED", HUMAN, note="Only the operator can approve a candidate; the daemon composes evidence and waits."),
        _plan("DELIVERY_APPROVED", HUMAN, note="Delivery is an outward action and stays with the operator."),
        _plan("DELIVERED", HUMAN, note="Publication is never performed by this runtime."),
    ]
)

if list(STAGE_PLANS) != STAGES:
    raise RuntimeError("the studio stage plan table has drifted from reelctl.state.STAGES")

DRAFT_PATHS: Tuple[str, ...] = tuple(sorted({relative for plan in STAGE_PLANS.values() for relative in plan.drafts}))


def plan_for(stage: str) -> StagePlan:
    try:
        return STAGE_PLANS[stage]
    except KeyError as exc:
        raise KeyError("unknown stage: {}".format(stage)) from exc


def argv_for(plan: StagePlan, *, project_id: str, revision: str) -> Tuple[str, ...]:
    return tuple(item.format(project_id=project_id, revision=revision) for item in plan.argv)


def draft_state(project_dir: Path, relative: str) -> str:
    path = Path(project_dir) / relative
    if not path.is_file():
        return MISSING
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return UNREADABLE
    if isinstance(payload, dict) and str(payload.get("status", "")).upper() == DRAFT:
        return DRAFT
    return AUTHORED


def unauthored_drafts(project_dir: Path, plan: StagePlan, *, revision: str) -> List[Tuple[str, str]]:
    unauthored: List[Tuple[str, str]] = []
    for template in plan.drafts:
        relative = template.format(revision=revision)
        state = draft_state(project_dir, relative)
        if state != AUTHORED:
            unauthored.append((relative, state))
    return unauthored


def job_for(project_dir: Path, stage: str, *, revision: str) -> JobSpec:
    directory = Path(project_dir)
    project_id = directory.name
    plan = plan_for(stage)
    if plan.kind == HUMAN:
        return JobSpec(
            project_id=project_id,
            stage=stage,
            kind=HUMAN,
            reason="{} is waiting on the operator, not on the machine".format(stage),
            note=plan.note,
        )
    if plan.kind == JUDGMENT:
        return JobSpec(project_id=project_id, stage=stage, kind=JUDGMENT, reason=plan.note or None, note=plan.note)
    pending = unauthored_drafts(directory, plan, revision=revision)
    if pending:
        described = ", ".join("{} is {}".format(relative, state) for relative, state in pending)
        return JobSpec(
            project_id=project_id,
            stage=stage,
            kind=JUDGMENT,
            reason="{} needs authored input first: {}".format(stage, described),
            note=plan.note,
            drafts=tuple(pending),
        )
    return JobSpec(
        project_id=project_id,
        stage=stage,
        kind=DETERMINISTIC,
        argv=argv_for(plan, project_id=project_id, revision=revision),
        note=plan.note,
    )


def call_gate_index(call: Mapping[str, Any]) -> Optional[int]:
    """Where in ``STAGES`` an open call starts gating work. ``None`` gates everything.

    The registry's ``daemon_contract.calls_gate_design`` makes calls stage-scoped: a card
    names the first stage its decision governs, stages before it proceed, that stage onward
    waits. Two consumers read that field — the daemon's SCHEDULE gate and the board's layer
    model — and they must agree exactly, because any difference is invisible: the board
    would show a reel parked while the daemon drove it, or the reverse.

    They did diverge once. This rule was implemented twice and the second copy omitted the
    ``.strip()``, so a card reading ``"  SELECTION_LOCKED  "`` would have gated the whole
    board while the daemon ran four stages. A comment saying "mirrors the daemon exactly" is
    not a mechanism; one function is. Both callers use this.

    Fails closed on everything it cannot place — missing, blank, non-string, wrong case, or
    a stage this build does not know. An unrecognised gate must never read as an open door.
    """
    stage = call.get("stage")
    if not isinstance(stage, str):
        return None
    try:
        return STAGES.index(stage.strip())
    except ValueError:
        return None


def project_fingerprint(project_dir: Path, *, revision: str) -> str:
    """A cheap version marker: the state file plus the authored-ness of every draft.

    It is the optimistic-concurrency check of §9.2 and the anti-storm key of §6.1 — a job
    that failed at fingerprint F is not tried again until disk moves off F.
    """
    directory = Path(project_dir)
    state = directory / "state.json"
    payload: Dict[str, Any] = {"state": None, "drafts": {}}
    try:
        payload["state"] = sha256_file(state) if state.is_file() else None
    except OSError as exc:
        payload["state"] = "unreadable: {}".format(exc)
    for template in DRAFT_PATHS:
        relative = template.format(revision=revision)
        payload["drafts"][relative] = draft_state(directory, relative)
    return recipe_hash(payload)
