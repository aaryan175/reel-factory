"""The front door: one reference in, one deferred project and one queued job out.

Three rules make this a front door rather than a shortcut, and all three come straight
from the invariants:

**Nothing is analysed here.** The project is created with ``defer_analysis=True``, so
every stage is ``PENDING`` and the reference is not even copied yet. Locking the reference
is the daemon's first job, driven through reelctl like every other stage. Intake that
"just quickly ran the analysis" would be a second path into stage state, which is the one
thing the stage machine exists to prevent.

**The mode is a call, not a dropdown the app fills in.** A lane implies a provisional mode
— the registry defines VOLUME as reference-grammar adaptation and CRAFT as reference-locked
1:1 — but the project records ``mode_confirmed: false`` and a ``CALL_REQUIRED`` card with
``issue_type: mode-ambiguity`` and its two concrete consequences goes out with it. The
provisional value exists because ``project.json`` requires one; it is never presented as a
decision the operator made.

**The registry is not written.** The intake record is registry-*consistent* — it carries a
``registry_entry`` shaped like a reels row — and lives in the project. Folding it into
``REEL_REGISTRY.json`` needs the single-writer module of §8.1, which does not exist yet;
until it does, writing the file that is the operation's status truth from here would be
exactly the second writer the architecture forbids.
"""

from __future__ import annotations

import json
import re
import sqlite3
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..cli import REFERENCE_DOWNLOAD_NOTICE, command_new
from ..errors import ReelctlError
from ..hashing import atomic_write_json
from ..identifiers import validate_identifier
from ..locks import project_lock
from ..paths import canonical_root, secure_mkdirs
from ..state import ProjectState, StateError
from .board import project_ids
from .authority import guarded_mutation
from .config import StudioConfig
from .db import initialize
from .events import EventLogError, record_event
from .jobs import active_job, enqueue
from .library import corpus_note, load_library
from .registry import craft_slot, normalise_lane, registry_report

LANES = ("VOLUME", "CRAFT")
LANE_MODE = {"VOLUME": "original-montage", "CRAFT": "reference-locked"}
LANE_DOCTRINE = {"VOLUME": "reference-grammar adaptation", "CRAFT": "reference-locked 1:1"}

INTAKE_FILENAME = "intake.json"
INTAKE_ARTIFACT_TYPE = "studio-intake-v1"
CALL_CARD_RELATIVE = "brain/07_CALL_PROMPTS.md"

# The daemon's first job on a deferred project: lock the reference, which unblocks
# feasibility. Kept deterministic — the judgment work starts at the blueprint.
BOOTSTRAP_STAGE = "REFERENCE_LOCKED"
BOOTSTRAP_KIND = "deterministic"
BOOTSTRAP_REASON = "intake bootstrap: lock the exact reference, then report feasibility"

# The stage the mode call gates. The daemon's calls gate stops work *before* the stage a
# card names, so this value decides how much of the pipeline an open mode call freezes.
#
# It must not freeze feasibility. The card asks the operator to choose between
# original-montage and reference-locked, and that question is not answerable until
# feasibility has produced the corpus numbers — a figure like "11 of 600 clips in this world" is
# exactly what makes the answer obvious. Gating at BLUEPRINT_LOCKED would stop the daemon
# before the work that makes the question answerable: a deadlock dressed as a gate.
#
# SELECTION_LOCKED is the first stage the mode actually governs, because selection is
# where 1:1 exact-scene matching diverges from grammar adaptation.
# Registry contract: daemon_contract.intake_mode_call_stage.
MODE_CALL_STAGE = "SELECTION_LOCKED"

# CLAUDE.md states the CRAFT quota as doctrine; the registry usually restates it. Used
# only when the registry declares no quota text, and labelled as such.
DOCTRINE_CRAFT_QUOTA = "one active project at a time (factory doctrine; the registry declares no quota text)"

SHORTCODE_PATTERN = re.compile(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --- identity --------------------------------------------------------------


def reference_shortcode(reference: str) -> Optional[str]:
    match = SHORTCODE_PATTERN.search(str(reference))
    return match.group(1) if match else None


def _slug(text: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-").lower()


def derive_project_id(reference: str, *, version: int = 1) -> str:
    """``.../reel/ref-19/`` → ``reel-ref-19-v1``.

    A shortcode is lowercased rather than slugged, because shortcodes legitimately contain
    ``-`` and ``_`` and the registry join is a plain lowercase comparison — slugging
    ``REF-09`` would silently break the tie between project and registry entry.
    """
    shortcode = reference_shortcode(reference)
    if shortcode:
        token = shortcode.lower()
    else:
        parsed = urlparse(str(reference))
        token = _slug(Path(parsed.path or str(reference)).stem)
    if not token:
        raise ReelctlError(f"could not derive a project id from reference {reference!r}; supply project_id explicitly")
    return validate_identifier(f"reel-{token}-v{version}", kind="project")


def read_intake(project_dir: Path) -> Optional[Dict[str, Any]]:
    path = Path(project_dir) / INTAKE_FILENAME
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


# --- lane quota ------------------------------------------------------------


def _is_finished(project_dir: Path) -> bool:
    try:
        return ProjectState.load(Path(project_dir) / "state.json").next_stage() is None
    except (StateError, OSError, ValueError):
        return False


def craft_quota_state(config: StudioConfig, report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Who holds the CRAFT lane right now, across all three places it can be declared.

    ``lanes.CRAFT.active`` is the headline, but it is not the only declaration: a reel
    entry can carry ``lane: CRAFT`` of its own, and a project this app
    created records its lane in ``intake.json``. A quota that consulted one of the three
    would be a quota of two or three. Every holder is named with where it was found.
    """
    report = registry_report(getattr(config, "registry_path", config.projects_root)) if report is None else report
    slot = craft_slot(report)
    holders: List[str] = []
    if slot["source"]:
        holders.append(slot["source"])
    for reel in report.get("reels") or []:
        if normalise_lane(reel.get("lane")) != "CRAFT":
            continue
        shortcode = str(reel.get("reference_shortcode") or "").strip()
        if shortcode and any(shortcode.lower() in holder.lower() for holder in holders):
            continue
        holders.append(f"registry reel {shortcode or '(unnamed)'} declares lane {reel.get('lane')}")
    root = canonical_root(config.projects_root, create=True)
    for project_id in project_ids(root):
        record = read_intake(root / project_id)
        if record and str(record.get("lane")) == "CRAFT" and not _is_finished(root / project_id):
            holders.append(f"local project {project_id} declares lane CRAFT in its intake record")
    return {
        "lane": "CRAFT",
        "quota": slot["quota"] or DOCTRINE_CRAFT_QUOTA,
        "quota_declared_by_registry": bool(slot["quota"]),
        "occupied": bool(holders),
        "holders": holders,
    }


# --- the mode call ---------------------------------------------------------


def mode_call_card(*, project_id: str, reference: str, lane: str, mode: str, preview: Dict[str, Any]) -> Dict[str, Any]:
    other = "CRAFT" if lane == "VOLUME" else "VOLUME"
    return {
        "call_id": f"CALL-MODE-{project_id}",
        "severity": "DECISION",
        "project_id": project_id,
        "revision": "v001",
        "reference_block": "whole reel (mode governs every block)",
        "issue_type": "mode-ambiguity",
        "stage": MODE_CALL_STAGE,
        "state": "AWAITING_OPERATOR",
        "provisional_lane": lane,
        "provisional_mode": mode,
        "what_the_reference_requires": (
            "Production mode decides what this build owes the reference: its grammar, or its exact picture states. "
            "The reference cannot answer that — only you can."
        ),
        "what_i_found": (
            f"Reference {reference} has been recorded but not analysed; intake defers analysis so the reference is locked by the "
            f"pipeline rather than by the front door. Lane {lane} was requested, which implies mode {mode} "
            f"({LANE_DOCTRINE[lane]}), and that is recorded as provisional only. Corpus position: {preview['recommendation']}"
        ),
        "why_it_does_not_match": (
            "Nothing has failed yet. The two modes produce materially different reels from the same reference, and picking one "
            "silently is how a build ends up judged against a standard it was never aimed at."
        ),
        "evidence": [
            f"footage library census: {preview['library_clips_total']} indexed clips at {preview['library_path']}",
            f"reference input as supplied: {reference}",
            "no reference lock exists yet — REFERENCE_LOCKED is PENDING and queued",
        ],
        "options": [
            {
                "key": "A",
                "text": (
                    "Build original-montage (lane VOLUME): the reel borrows the reference's rhythm, shot roles and energy arc "
                    "using authorized footage. Consequence: no literal scene from the reference appears, and the build is never "
                    "judged on exact-scene fidelity."
                ),
            },
            {
                "key": "B",
                "text": (
                    "Build reference-locked (lane CRAFT): the reel reproduces the reference's exact picture states, clock, captions "
                    "and transitions. Consequence: every block needs an exact-scene match in the corpus, the feasibility gate refuses "
                    "the render below its coverage threshold, and the CRAFT lane allows one active project at a time."
                ),
            },
            {
                "key": "C",
                "text": (
                    "Provide new footage, choose another reference, or explicitly loosen the mode — and say which, so the loosening "
                    "is recorded rather than assumed."
                ),
            },
        ],
        "recommendation": (
            f"A — lane {lane} ({mode}). It is the lane that produced every approved reel to date, and it does not stake the build on "
            f"exact-scene coverage the corpus has not been measured against yet. Answer B to move this reel to {other} instead."
        ),
        "reply_with": "A, B, C, or: <exact information needed>",
        "created_at_utc": _now(),
    }


def call_card_markdown(card: Dict[str, Any]) -> str:
    """The card in the onboarding prompt's exact text schema."""
    lines = [
        "CALL_REQUIRED",
        f"call_id: {card['call_id']}",
        f"severity: {card['severity']}",
        f"project_id: {card['project_id']}",
        f"revision: {card['revision']}",
        f"reference_block: {card['reference_block']}",
        f"issue_type: {card['issue_type']}",
        # Appended after the onboarding prompt's header block rather than inserted into it,
        # so the canonical field order is untouched. Without it the card asks for a
        # decision without telling the reader what is frozen until they answer.
        f"stage: {card['stage']}",
        "",
        "WHAT THE REFERENCE REQUIRES:",
        card["what_the_reference_requires"],
        "",
        "WHAT I FOUND:",
        card["what_i_found"],
        "",
        "WHY IT DOES NOT MATCH:",
        card["why_it_does_not_match"],
        "",
        "EVIDENCE:",
    ]
    lines += [f"- {item}" for item in card["evidence"]]
    lines += ["", "OPTIONS:"]
    lines += [f"{option['key']}. {option['text']}" for option in card["options"]]
    lines += ["", "MY RECOMMENDATION:", card["recommendation"], "", "REPLY WITH:", card["reply_with"], ""]
    return "\n".join(lines)


# --- plan and create -------------------------------------------------------


def plan_intake(
    config: StudioConfig,
    *,
    reference: str,
    lane: str = "VOLUME",
    project_id: Optional[str] = None,
    footage_root: Optional[str] = None,
    world: Optional[str] = None,
) -> Dict[str, Any]:
    """Everything intake would do, computed and returned without writing anything."""
    if not str(reference).strip():
        raise ReelctlError("a reference URL or local path is required")
    if lane not in LANES:
        raise ReelctlError(f"unknown lane {lane!r}; expected one of {', '.join(LANES)}")
    mode = LANE_MODE[lane]
    identifier = validate_identifier(project_id, kind="project") if project_id else derive_project_id(reference)

    report = load_library(config.library_path)
    resolved = footage_root or report.get("library_root")
    if not resolved:
        raise ReelctlError(
            f"no authorized footage root: {config.library_path} declares no source.library_root and none was supplied"
        )
    footage = Path(str(resolved)).expanduser()
    preview = corpus_note(report, world=world)
    registry = registry_report(getattr(config, "registry_path", config.projects_root))
    shortcode = reference_shortcode(reference)

    return {
        "status": "PASS",
        "project_id": identifier,
        "reference": reference,
        "reference_shortcode": shortcode,
        "lane": lane,
        "mode": mode,
        "mode_confirmed": False,
        "mode_provenance": f"derived from lane {lane} ({LANE_DOCTRINE[lane]}); provisional until the mode call is answered",
        "footage_root": str(footage),
        "footage_root_is_directory": footage.is_dir(),
        "library": report,
        "feasibility_preview": preview,
        "craft_quota": craft_quota_state(config, registry),
        "registry_status": registry["status"],
        "call": mode_call_card(project_id=identifier, reference=reference, lane=lane, mode=mode, preview=preview),
        "planned_job": {"stage": BOOTSTRAP_STAGE, "kind": BOOTSTRAP_KIND, "reason": BOOTSTRAP_REASON},
    }


def _registry_entry(plan: Dict[str, Any]) -> Dict[str, Any]:
    """A reels-row-shaped record. Consistent with the registry; not written to it."""
    return {
        "sequence": None,
        "reference_shortcode": plan["reference_shortcode"],
        "reference_url": plan["reference"],
        "creative_hook": None,
        "audio": None,
        "version": "v1",
        "lane": plan["lane"],
        "review_state": "INTAKE",
        "status_bucket": "IN_PIPELINE",
        "project_root": plan["project_id"],
        "runtime": "REELCTL_STUDIO",
    }


@guarded_mutation("studio intake creation")
def create_intake(
    config: StudioConfig,
    *,
    reference: str,
    lane: str = "VOLUME",
    project_id: Optional[str] = None,
    footage_root: Optional[str] = None,
    world: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the deferred project, record the intake, and queue the bootstrap job."""
    plan = plan_intake(
        config, reference=reference, lane=lane, project_id=project_id, footage_root=footage_root, world=world
    )
    identifier = plan["project_id"]

    if lane == "CRAFT" and plan["craft_quota"]["occupied"]:
        quota = plan["craft_quota"]
        raise ReelctlError(
            f"lane CRAFT is at its quota ({quota['quota']}); it is held by: {'; '.join(quota['holders'])}"
        )
    if not plan["footage_root_is_directory"]:
        raise ReelctlError(
            f"authorized footage root is not a directory: {plan['footage_root']} — refusing to create a project against a "
            f"footage pool that is absent or unmounted"
        )

    root = canonical_root(config.projects_root, create=True)
    args = Namespace(
        projects_root=root,
        project_id=identifier,
        reference=reference,
        footage=plan["footage_root"],
        mode=plan["mode"],
        defer_analysis=True,
    )
    with project_lock(root, identifier):
        created = command_new(args)
    directory = Path(created["project_dir"])

    record = {
        "schema_version": 1,
        "artifact_type": INTAKE_ARTIFACT_TYPE,
        "project_id": identifier,
        "created_at_utc": _now(),
        "reference": reference,
        # A URL reference is downloaded later by the bootstrap stage: the notice travels with the project.
        "reference_rights_notice": (
            REFERENCE_DOWNLOAD_NOTICE if urlparse(str(reference)).scheme in {"http", "https"} else None
        ),
        "reference_shortcode": plan["reference_shortcode"],
        "lane": plan["lane"],
        "mode": plan["mode"],
        "mode_confirmed": plan["mode_confirmed"],
        "mode_provenance": plan["mode_provenance"],
        "footage_root": plan["footage_root"],
        "library_path": str(config.library_path),
        "library_clips_total": plan["library"]["clips_total"],
        "world": world,
        "feasibility_preview": plan["feasibility_preview"],
        "defer_analysis": True,
        "bootstrap_stage": BOOTSTRAP_STAGE,
        "call": plan["call"],
        "call_card": CALL_CARD_RELATIVE,
        "registry_entry": _registry_entry(plan),
    }
    atomic_write_json(directory / INTAKE_FILENAME, record, root=directory)
    brain = secure_mkdirs(directory, "brain")
    (brain / Path(CALL_CARD_RELATIVE).name).write_text(call_card_markdown(plan["call"]), encoding="utf-8")

    job_id: Optional[int] = None
    job_error: Optional[str] = None
    try:
        initialize(config.database_path)
        job_id = enqueue(
            config.database_path,
            project_id=identifier,
            stage=BOOTSTRAP_STAGE,
            kind=BOOTSTRAP_KIND,
            reason=BOOTSTRAP_REASON,
        )
        if job_id is None:
            # The schema's partial unique index refused a second active job for this
            # project. The project is already created and real; the queue is a scheduler,
            # not an authority (§8.1), so a stale row does not get to veto the reel. Name
            # the conflict and let the daemon's reconcile pass drop what disk disagrees with.
            existing = active_job(config.database_path, identifier)
            job_error = (
                f"bootstrap job not queued: project {identifier} already has an active job "
                f"({existing.get('status')} {existing.get('stage')}, id {existing.get('id')})"
                if existing
                else f"bootstrap job not queued: the queue refused a second active job for {identifier}"
            )
    except (sqlite3.Error, OSError) as exc:
        job_error = f"bootstrap job could not be queued: {exc}"

    event_error: Optional[str] = None
    try:
        record_event(
            config.database_path,
            kind="intake",
            project_id=identifier,
            job_id=job_id,
            payload={"lane": plan["lane"], "mode": plan["mode"], "mode_confirmed": False, "call_id": plan["call"]["call_id"]},
        )
    except EventLogError as exc:
        event_error = str(exc)

    return {
        "status": "PASS",
        "project_id": identifier,
        "project_dir": str(directory),
        "next_stage": created.get("next_stage"),
        "lane": plan["lane"],
        "mode": plan["mode"],
        "mode_confirmed": False,
        "footage_root": plan["footage_root"],
        "feasibility_preview": plan["feasibility_preview"],
        "call": plan["call"],
        "call_card": str(directory / CALL_CARD_RELATIVE),
        "intake_record": str(directory / INTAKE_FILENAME),
        "job_id": job_id,
        "job_error": job_error,
        "event_error": event_error,
    }
