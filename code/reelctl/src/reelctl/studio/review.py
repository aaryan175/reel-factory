"""The review room: the screen that replaces the Drive folder).

The room shows exactly the artifacts the machine compared — the candidate, the reference,
and `review/qc-<rev>/reference-candidate-board.jpg`, which is the board QC actually built —
beside the Task 2 per-layer read model. It adds no opinion of its own: ``review_bundle``
calls ``status.project_layers`` rather than recomputing anything, so there is no second
place a layer could be called ``PROVEN``.

Four rules make the verdict trustworthy, and each exists because of a specific failure the
factory has already had.

**Approve has two locks.** The API refuses when the QC report's
``authorities.local_review_ready`` is false, and ``ProjectState.complete`` refuses again
because its predecessor ``LOCAL_REVIEW_READY`` must be ``PASS`` and re-verify. The history
here includes a signed "I watched it, PASS" that was false, so one lock was not enough.

**The verdict binds the bytes, not the claim.** Every hash in the receipt is recomputed
from the file at verdict time. If the candidate on disk no longer hashes to what the QC
report recorded, the approval is refused outright — approving bytes QC never saw is the
same failure wearing a different hat.

**A rejection is evidence, not a deletion.** Reject and request-changes both require a
structured ``FeedbackEvent`` (target / observation / requested_change / scope) and write a
signed negative fixture next to the receipt. "Every rejection is a permanent negative
fixture" stops depending on someone remembering to write one.

**Reject and request-changes are different words on purpose.** A rejection records the
stage ``FAIL`` — the honesty vocabulary's ``FAILED``. A change request records ``BLOCKED``
— ``WITHHELD``, a named refusal to claim. Collapsing them would throw away the distinction
§7.1 is built on.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from .. import __version__
from ..errors import ReelctlError
from ..hashing import atomic_write_json, sha256_file
from ..paths import PathSafetyError, canonical_root, confined_path, secure_mkdirs
from ..signing import sign_payload
from ..state import ProjectState, StateError
from .calls import append_decision_log
from .authority import guarded_mutation
from .config import StudioConfig
from .events import EventLogError, record_event
from .registry import open_calls_by_project, registry_report
from .status import project_layers

APPROVE = "APPROVE"
REJECT = "REJECT"
CHANGES = "CHANGES"
VERDICTS = {"approve": APPROVE, "reject": REJECT, "changes": CHANGES}

VERDICT_DIRECTORY = "review/verdicts"
FIXTURE_DIRECTORY = "review/negative-fixtures"
REFERENCE_LOCK = "reference/reference-lock.json"

VERDICT_PURPOSE = "studio-human-verdict-v1"
FIXTURE_PURPOSE = "studio-negative-fixture-v1"
DEFAULT_REVIEWER = "operator"

#: The stages whose receipt binds a QC report. Read in this order: the latest QC authority
#: first, so the report shown is the one the strongest completed check was bound to.
QC_STAGES = ("VISUAL_QC", "STRUCTURE_QC", "TECHNICAL_QC")

FEEDBACK_FIELDS = ("target", "observation", "requested_change", "scope")

WHY_FIXTURES_EXIST = (
    "Append-only doctrine: a rejected candidate stays on disk as a negative fixture with its receipt, so the factory cannot "
    "repeat a settled mistake or quietly rebuild from scratch."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _artifact_url(project_id: str, relative: str) -> str:
    return "/api/projects/{}/artifact?path={}".format(quote(str(project_id), safe=""), quote(str(relative), safe=""))


def _project_dir(config: StudioConfig, project_id: str) -> Path:
    root = canonical_root(config.projects_root, create=True)
    directory = root / project_id
    if not (directory / "state.json").is_file():
        raise ReelctlError("no reelctl project {!r} at {}".format(project_id, directory))
    return directory


def _relative(project_dir: Path, value: Any) -> Optional[str]:
    """A project-relative path for anything inside the project, else ``None``."""
    if not value:
        return None
    try:
        path = confined_path(project_dir, str(value), require="file", allow_missing=False)
    except PathSafetyError:
        return None
    return str(path.relative_to(project_dir))


def _file_facts(project_dir: Path, relative: Optional[str], *, reason: Optional[str] = None) -> Dict[str, Any]:
    if relative is None:
        return {"path": None, "sha256": None, "bytes": None, "artifact_url": None, "reason": reason}
    path = project_dir / relative
    return {
        "path": relative,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "artifact_url": _artifact_url(project_dir.name, relative),
        "reason": None,
    }


# --- evidence discovery ----------------------------------------------------


def _qc_evidence(project_dir: Path, state: ProjectState) -> Dict[str, Any]:
    """The QC report bound to a stage receipt — never one found by globbing a directory.

    A directory can hold several reports (``qc-report.json`` plus token-suffixed rerun
    reports). Picking one by name would let the room show a report no stage ever accepted;
    the stage receipt is the only thing that says which report the engine stood behind.
    """
    absent = {"path": None, "sha256": None, "bound_to_stage": None, "report": None, "authorities": None, "reason": None}
    for stage_name in QC_STAGES:
        stage = state.stage(stage_name)
        for receipt in stage.get("output_receipts") or []:
            relative = str(receipt.get("path") or "")
            if not relative.startswith("review/") or not relative.endswith(".json"):
                continue
            path = project_dir / relative
            if not path.is_file():
                continue
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return {**absent, "path": relative, "reason": "{} is unreadable: {}".format(relative, exc)}
            if not isinstance(report, dict):
                return {**absent, "path": relative, "reason": "{} is not a QC report object".format(relative)}
            return {
                "path": relative,
                "sha256": sha256_file(path),
                "bound_to_stage": stage_name,
                "report": report,
                "authorities": report.get("authorities") if isinstance(report.get("authorities"), dict) else None,
                "reason": None,
            }
    return {**absent, "reason": "no QC report is bound to any of {} on this project".format(", ".join(QC_STAGES))}


def _reference_facts(project_dir: Path, qc: Mapping[str, Any]) -> Dict[str, Any]:
    """The locked reference, as ``project.json`` records it after ``reference analyze``."""
    try:
        config = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"path": None, "sha256": None, "bytes": None, "artifact_url": None, "reason": "project.json is unreadable: {}".format(exc)}
    relative = _relative(project_dir, config.get("reference_path"))
    if relative is None:
        return {
            "path": None,
            "sha256": None,
            "bytes": None,
            "artifact_url": None,
            "reason": "project.json records no locked reference inside this project ({!r})".format(config.get("reference_path")),
        }
    facts = _file_facts(project_dir, relative)
    facts["mismatches"] = _reference_mismatches(project_dir, facts["sha256"], qc)
    return facts


def _reference_mismatches(project_dir: Path, digest: Optional[str], qc: Mapping[str, Any]) -> List[str]:
    """Everything that claims to know the reference's hash, and whether they agree."""
    mismatches: List[str] = []
    report = qc.get("report") or {}
    claimed = report.get("reference_sha256") if isinstance(report, dict) else None
    if claimed and digest and claimed != digest:
        mismatches.append("the QC report was run against reference {} but the locked reference now hashes to {}".format(claimed, digest))
    lock_path = project_dir / REFERENCE_LOCK
    if lock_path.is_file():
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return mismatches
        locked = (lock.get("source") or {}).get("sha256") if isinstance(lock, dict) else None
        if locked and digest and locked != digest:
            mismatches.append("{} records reference {} but the file now hashes to {}".format(REFERENCE_LOCK, locked, digest))
    return mismatches


def _board_relative(project_dir: Path, revision: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not revision:
        return None, "no revision is known, so the comparison board cannot be located"
    relative = "review/qc-{}/reference-candidate-board.jpg".format(revision)
    if not (project_dir / relative).is_file():
        return None, "{} does not exist; run reelctl qc once to build the comparison board".format(relative)
    return relative, None


def _candidate_facts(project_dir: Path, qc: Mapping[str, Any]) -> Dict[str, Any]:
    report = qc.get("report") or {}
    relative = _relative(project_dir, report.get("candidate")) if isinstance(report, dict) else None
    if relative is None:
        return {
            "path": None,
            "sha256": None,
            "bytes": None,
            "artifact_url": None,
            "reason": qc.get("reason") or "the QC report names no candidate inside this project",
            "matches_qc_report": False,
        }
    facts = _file_facts(project_dir, relative)
    claimed = report.get("candidate_sha256")
    facts["qc_report_sha256"] = claimed
    facts["matches_qc_report"] = bool(claimed) and claimed == facts["sha256"]
    if not facts["matches_qc_report"]:
        facts["reason"] = "the candidate on disk hashes to {} but the QC report recorded {}".format(facts["sha256"], claimed)
    return facts


def recorded_verdicts(project_dir: Path) -> List[Dict[str, Any]]:
    """Every verdict this project has ever recorded, oldest first. Nothing is removed."""
    directory = Path(project_dir) / VERDICT_DIRECTORY
    if not directory.is_dir():
        return []
    found: List[Dict[str, Any]] = []
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        found.append(
            {
                "sequence": int(payload.get("sequence") or 0),
                "verdict": payload.get("verdict"),
                "reviewer": payload.get("reviewer"),
                "recorded_at_utc": payload.get("recorded_at_utc"),
                "candidate_sha256": (payload.get("candidate") or {}).get("sha256"),
                "notes": payload.get("notes"),
                "feedback": payload.get("feedback"),
                "receipt": "{}/{}".format(VERDICT_DIRECTORY, path.name),
                "negative_fixture": payload.get("negative_fixture"),
            }
        )
    return sorted(found, key=lambda item: (item["sequence"], item["receipt"]))


# --- the bundle ------------------------------------------------------------


def _approval_gate(qc: Mapping[str, Any], candidate: Mapping[str, Any], state: ProjectState) -> Optional[str]:
    """The API-level lock. Returns the refusal sentence, or ``None`` when approve is open."""
    if qc.get("path") is None:
        return qc.get("reason") or "there is no QC report to approve against"
    authorities = qc.get("authorities")
    if not isinstance(authorities, dict):
        return "{} records no authorities block, so local_review_ready cannot be read".format(qc.get("path"))
    if authorities.get("local_review_ready") is not True:
        return "the QC report records authorities.local_review_ready = {!r}; approval is refused until the machine finishes".format(
            authorities.get("local_review_ready")
        )
    if candidate.get("path") is None:
        return candidate.get("reason") or "no candidate file was found to approve"
    if not candidate.get("matches_qc_report"):
        return "the candidate no longer matches the QC report: {}".format(candidate.get("reason"))
    try:
        state.verify_stage("LOCAL_REVIEW_READY")
    except StateError as exc:
        return "LOCAL_REVIEW_READY does not verify: {}".format(exc)
    return None


def review_bundle(config: StudioConfig, project_id: str) -> Dict[str, Any]:
    """Candidate, reference, board, QC and the per-layer read model, in one payload."""
    project_dir = _project_dir(config, project_id)
    state = ProjectState.load(project_dir / "state.json")
    qc = _qc_evidence(project_dir, state)
    report = qc.get("report") or {}
    revision = str(report.get("revision")) if isinstance(report, dict) and report.get("revision") else None

    candidate = _candidate_facts(project_dir, qc)
    reference = _reference_facts(project_dir, qc)
    board_relative, board_reason = _board_relative(project_dir, revision)
    board = _file_facts(project_dir, board_relative, reason=board_reason)

    registry = registry_report(getattr(config, "registry_path", config.projects_root))
    calls = open_calls_by_project(registry).get(project_id, [])
    model = project_layers(project_dir, open_calls=calls)
    refusal = _approval_gate(qc, candidate, state)

    return {
        "status": "PASS",
        "project_id": project_id,
        "project_dir": str(project_dir),
        "revision": revision,
        "candidate": candidate,
        "reference": reference,
        "board": board,
        "qc": {
            "path": qc["path"],
            "sha256": qc["sha256"],
            "bound_to_stage": qc["bound_to_stage"],
            "authorities": qc["authorities"],
            "reason": qc["reason"],
            "render_receipt_sha256": report.get("render_receipt_sha256") if isinstance(report, dict) else None,
            # ffprobe's own facts about the candidate, as QC recorded them. Carried so the
            # publish card can state the asset's duration and geometry without re-probing.
            "technical_facts": (report.get("technical") or {}).get("facts") if isinstance(report, dict) else None,
            "artifact_url": _artifact_url(project_id, qc["path"]) if qc["path"] else None,
        },
        "headline": model["headline"],
        "layers": model["layers"],
        "open_calls": calls,
        "verdicts": recorded_verdicts(project_dir),
        "can_approve": refusal is None,
        "approve_refusal": refusal,
        "generated_at_utc": _now(),
    }


# --- recording a verdict ---------------------------------------------------


def _validate_feedback(verdict: str, feedback: Optional[Mapping[str, Any]]) -> Optional[Dict[str, str]]:
    if verdict == APPROVE:
        return dict(feedback) if feedback else None
    if not feedback:
        raise ReelctlError(
            "a {} verdict requires a structured FeedbackEvent with {}; it is what becomes the negative fixture".format(
                verdict.lower(), ", ".join(FEEDBACK_FIELDS)
            )
        )
    missing = [field for field in FEEDBACK_FIELDS if not str(feedback.get(field) or "").strip()]
    if missing:
        raise ReelctlError("the FeedbackEvent is missing {}".format(", ".join(missing)))
    return {field: str(feedback[field]).strip() for field in FEEDBACK_FIELDS}


def _next_sequence(project_dir: Path) -> int:
    directory = project_dir / VERDICT_DIRECTORY
    return (len(list(directory.glob("*.json"))) if directory.is_dir() else 0) + 1


def _stage_outcome(verdict: str) -> Tuple[str, str]:
    if verdict == APPROVE:
        return "PASS", "PROVEN"
    if verdict == REJECT:
        return "FAIL", "FAILED"
    return "BLOCKED", "WITHHELD"


def _stage_reason(verdict: str, feedback: Optional[Mapping[str, str]], fixture_relative: Optional[str]) -> Optional[str]:
    if verdict == APPROVE or not feedback:
        return None
    verb = "rejected the candidate" if verdict == REJECT else "requested changes"
    return (
        "operator {verb}: {observation} — requested: {requested_change} (scope {scope}, target {target}); "
        "negative fixture {fixture}"
    ).format(verb=verb, fixture=fixture_relative, **feedback)


def _baseline_guard(state: ProjectState, candidate_sha256: str, project_id: str) -> None:
    stage = state.stage("HUMAN_APPROVED")
    if stage.get("status") == "PASS" and stage.get("input_hash") == candidate_sha256:
        raise ReelctlError(
            "{} is already approved on this exact candidate (same input hash {}); an approved baseline is never overwritten. "
            "Render a new candidate, or supersede this decision on a new revision.".format(project_id, candidate_sha256)
        )


@guarded_mutation("review verdict recording")
def record_verdict(
    config: StudioConfig,
    project_id: str,
    *,
    verdict: str,
    feedback: Optional[Mapping[str, Any]] = None,
    notes: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> Dict[str, Any]:
    """Write one hash-bound verdict, its negative fixture when there is one, and the stage."""
    name = VERDICTS.get(str(verdict).strip().lower())
    if name is None:
        raise ReelctlError("unknown verdict {!r}; expected one of {}".format(verdict, ", ".join(sorted(VERDICTS))))
    event = _validate_feedback(name, feedback)

    bundle = review_bundle(config, project_id)
    project_dir = Path(bundle["project_dir"])
    state = ProjectState.load(project_dir / "state.json")

    if name == APPROVE and not bundle["can_approve"]:
        raise ReelctlError("approval refused for {}: {}".format(project_id, bundle["approve_refusal"]))
    candidate = bundle["candidate"]
    if candidate["path"] is None:
        raise ReelctlError("no candidate to record a verdict against for {}: {}".format(project_id, candidate.get("reason")))
    if not candidate.get("matches_qc_report"):
        raise ReelctlError(
            "the candidate for {} no longer matches the QC report, so no verdict can bind it: {}".format(project_id, candidate.get("reason"))
        )
    _baseline_guard(state, candidate["sha256"], project_id)

    sequence = _next_sequence(project_dir)
    stem = "verdict-{:03d}-{}".format(sequence, name.lower())
    fixture_relative = "{}/{}.json".format(FIXTURE_DIRECTORY, stem) if event and name != APPROVE else None

    core = {
        "schema_version": 1,
        "artifact_type": VERDICT_PURPOSE,
        "project_id": project_id,
        "revision": bundle["revision"],
        "sequence": sequence,
        "verdict": name,
        "reviewer": (reviewer or DEFAULT_REVIEWER).strip() or DEFAULT_REVIEWER,
        "reviewer_provenance": "recorded through the loopback-only studio review room",
        "candidate": {"path": candidate["path"], "sha256": candidate["sha256"], "bytes": candidate["bytes"]},
        "reference": {"path": bundle["reference"]["path"], "sha256": bundle["reference"]["sha256"]},
        "qc_report": {"path": bundle["qc"]["path"], "sha256": bundle["qc"]["sha256"], "bound_to_stage": bundle["qc"]["bound_to_stage"]},
        "comparison_board": {"path": bundle["board"]["path"], "sha256": bundle["board"]["sha256"]},
        "render_receipt_sha256": bundle["qc"]["render_receipt_sha256"],
        "authorities_at_verdict": bundle["qc"]["authorities"],
        "headline_at_verdict": bundle["headline"],
        "layers_at_verdict": {layer["key"]: layer["status"] for layer in bundle["layers"]},
        "feedback": event,
        "negative_fixture": fixture_relative,
        "notes": (notes or "").strip() or None,
        "recorded_at_utc": _now(),
        "reelctl_version": __version__,
    }
    receipt = sign_payload(core, purpose=VERDICT_PURPOSE)

    verdict_dir = secure_mkdirs(project_dir, *Path(VERDICT_DIRECTORY).parts)
    verdict_relative = "{}/{}.json".format(VERDICT_DIRECTORY, stem)
    atomic_write_json(verdict_dir / "{}.json".format(stem), receipt, root=project_dir)

    outputs = [verdict_relative]
    if fixture_relative is not None:
        fixture = sign_payload(
            {
                "schema_version": 1,
                "artifact_type": FIXTURE_PURPOSE,
                "project_id": project_id,
                "revision": bundle["revision"],
                "sequence": sequence,
                "verdict": name,
                "reviewer": receipt["reviewer"],
                "candidate": receipt["candidate"],
                "reference": receipt["reference"],
                "qc_report": receipt["qc_report"],
                "comparison_board": receipt["comparison_board"],
                "feedback": event,
                "verdict_receipt": verdict_relative,
                "recorded_at_utc": receipt["recorded_at_utc"],
                "reelctl_version": __version__,
                "why_this_exists": WHY_FIXTURES_EXIST,
            },
            purpose=FIXTURE_PURPOSE,
        )
        fixture_dir = secure_mkdirs(project_dir, *Path(FIXTURE_DIRECTORY).parts)
        atomic_write_json(fixture_dir / "{}.json".format(stem), fixture, root=project_dir)
        outputs.append(fixture_relative)

    status, layer_word = _stage_outcome(name)
    reason = _stage_reason(name, event, fixture_relative)
    try:
        state.complete("HUMAN_APPROVED", candidate["sha256"], outputs, status=status, reason=reason)
    except StateError as exc:
        raise ReelctlError("the verdict for {} is on disk but the stage refused it: {}".format(project_id, exc)) from exc

    append_decision_log(project_dir, project_id, _log_entry(receipt=receipt, relative=verdict_relative, layer_word=layer_word))

    event_error: Optional[str] = None
    try:
        record_event(
            config.database_path,
            kind="verdict",
            project_id=project_id,
            payload={
                "verdict": name,
                "sequence": sequence,
                "candidate_sha256": candidate["sha256"],
                "receipt": verdict_relative,
                "negative_fixture": fixture_relative,
            },
        )
    except EventLogError as exc:
        event_error = str(exc)

    return {
        "status": "PASS",
        "project_id": project_id,
        "verdict": name,
        "sequence": sequence,
        "stage_status": status,
        "layer": layer_word,
        "receipt": str(project_dir / verdict_relative),
        "negative_fixture": str(project_dir / fixture_relative) if fixture_relative else None,
        "candidate_sha256": candidate["sha256"],
        "next_stage": state.next_stage(),
        "event_error": event_error,
    }


def _log_entry(*, receipt: Mapping[str, Any], relative: str, layer_word: str) -> str:
    feedback = receipt.get("feedback") or {}
    lines = [
        "\n---\n",
        "## {} — human verdict {} (Reel Studio review room)\n".format(receipt["recorded_at_utc"], receipt["verdict"]),
        "**D-{}-verdict-{:03d}** — {} on revision {}; the human approval layer now reads {}.\n".format(
            receipt["recorded_at_utc"][:10], receipt["sequence"], receipt["verdict"], receipt.get("revision"), layer_word
        ),
        "*Bound to:* candidate `{}` sha256 `{}`; reference sha256 `{}`; QC report `{}` sha256 `{}`; board sha256 `{}`\n".format(
            (receipt.get("candidate") or {}).get("path"),
            (receipt.get("candidate") or {}).get("sha256"),
            (receipt.get("reference") or {}).get("sha256"),
            (receipt.get("qc_report") or {}).get("path"),
            (receipt.get("qc_report") or {}).get("sha256"),
            (receipt.get("comparison_board") or {}).get("sha256"),
        ),
    ]
    if feedback:
        lines.append(
            "*Feedback:* target {target} — {observation} — requested: {requested_change} (scope {scope})\n".format(**feedback)
        )
    if receipt.get("negative_fixture"):
        lines.append("*Negative fixture:* `{}`\n".format(receipt["negative_fixture"]))
    if receipt.get("notes"):
        lines.append("*Notes:* {}\n".format(receipt["notes"]))
    lines.append("*Receipt:* `{}`\n".format(relative))
    lines.append("*Recorded by:* reelctl {} on behalf of {}\n".format(receipt.get("reelctl_version"), receipt.get("reviewer")))
    return "\n".join(lines)
