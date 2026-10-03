"""The publish card and metric-capture scheduling, §8.4).

**Publishing is fully manual in v1** — a deliberate design decision. This module composes the approval
card from `reel_system/REEL_OPERATING_SYSTEM.md` §"Exact approval card", records the
operator's approval as a hash-bound receipt, and schedules the two metric captures. It
makes **no outward call of any kind**, and neither does any route that reaches it: the
scheduler call, the upload and the post all stay with the operator.

Three rules follow from that, and each is tested:

**The card never invents a field.** Everything the machine can prove — the asset's path,
its sha256, its geometry and duration, the QC report it was judged by, the human approval
receipt — is read from disk and hash-bound. Everything else is the operator's to state, and
until they state it the card lists it under `unresolved` with the reason. A card that filled
in a plausible destination account would be the same class of error as a green light
standing in for an unproven claim.

**The capture windows come from the publication, not the approval.** §8.4 keys the +24h and
+72h captures off the publication receipt's `published_at`. An approval is recorded *before*
the operator posts, so at approval time there is usually no publication receipt and nothing
to schedule — the response says so rather than scheduling from the wrong clock. When the
receipt lands, `schedule_metric_captures` inserts exactly two rows, once.

**The captures are rows, not calls.** Scheduling writes `studio.db` rows of kind
`metric_capture`. Executing them — reading platform/scheduler analytics — stays with the
operator and the factory loop; `append_ledger_snapshot` takes the numbers as data and files
them. A metric the capture did not receive is written `null` and named in
`withheld_metrics`, never defaulted to zero, because a zero and an unmeasured value read
identically in a ledger and only one of them is true.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import __version__
from ..errors import ReelctlError
from ..hashing import atomic_write_json
from ..paths import canonical_root, secure_mkdirs
from ..signing import sign_payload
from ..state import ProjectState, StateError
from . import jobs
from .calls import append_decision_log
from .authority import guarded_mutation
from .config import StudioConfig
from .db import connect
from .events import EventLogError, record_event
from .review import review_bundle

POST_TYPE = "REEL"
APPROVAL_PURPOSE = "studio-delivery-approval-v1"
APPROVAL_DIRECTORY = "deliver"
PUBLICATION_SCHEMA = "reel-publication-receipt-v1"

CAPTURE_KIND = "metric_capture"
CAPTURE_WINDOWS: Tuple[Tuple[str, int], ...] = (("24h", 24), ("72h", 72))

LEDGER_RELATIVE = "learning/LEDGER.json"
LEDGER_SCHEMA = "trial-learning-ledger-v1"
DOCTRINE_RULE = "No style-baseline change on fewer than 3 comparable trials."

#: The operating system's minimum capture list. Anything absent is written null and named.
MINIMUM_METRICS: Tuple[str, ...] = (
    "views",
    "reach",
    "average_watch_time_seconds",
    "retention_percent",
    "three_second_view_rate_percent",
    "likes",
    "comments",
    "saves",
    "shares",
    "reposts",
    "follows_when_attributable",
)

#: Fields the contract requires before an approval means anything.
REQUIRED_CARD_FIELDS: Tuple[str, ...] = ("destination", "caption", "schedule", "timezone")
#: Fields the contract lists but that may legitimately read "unset" with a reason.
OPTIONAL_CARD_FIELDS: Tuple[str, ...] = ("internal_note", "audio_label", "cover", "disclosure", "auto_expansion")

FIELD_REASONS = {
    "destination": (
        "no machine-readable destination account is declared anywhere in the factory — not in REEL_REGISTRY.json, not in the "
        "project — and this app makes no outward call it could ask; the operator states the connected account"
    ),
    "caption": "the public caption is a creative decision and is never drafted into an approval card unattended",
    "schedule": "the exact date and time of the post is the operator's; an unset schedule authorizes nothing",
    "timezone": "the timezone the schedule is expressed in, stated rather than assumed from this machine's locale",
    "internal_note": "the private note carried alongside the post, if the operator wants one",
    "audio_label": "the original-audio display name, if the post uses original audio",
    "cover": "the cover frame or image, if the publishing surface exposes that control",
    "disclosure": "any AI-content or paid-partnership disclosure the post requires",
    "auto_expansion": "whether automatic expansion to followers is on or off",
}

PLATFORM_LIMITATIONS = (
    "This app performs no outward call, so it cannot read the destination account's feature entitlements, follower count or "
    "posting eligibility; none of those are asserted here.",
    "The scheduling tool may not expose cover selection, licensed-audio selection or the automatic-expansion control, so those "
    "fields cannot be set by any automation and must be handled in the platform's own composer if they matter.",
    "Recording this approval uploads nothing, schedules nothing with any provider and spends nothing. The outward action remains "
    "the operator's, performed outside this app.",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _project_dir(config: StudioConfig, project_id: str) -> Path:
    return canonical_root(config.projects_root, create=True) / project_id


def ledger_path(config: StudioConfig) -> Path:
    return Path(config.projects_root) / LEDGER_RELATIVE


# --- the card --------------------------------------------------------------


def _asset_facts(bundle: Mapping[str, Any]) -> Dict[str, Any]:
    """The asset line of the card: path, hash, duration — proven, or named as unknown."""
    candidate = bundle.get("candidate") or {}
    facts: Dict[str, Any] = {
        "path": candidate.get("path"),
        "sha256": candidate.get("sha256"),
        "bytes": candidate.get("bytes"),
        "duration_seconds": None,
        "frame_count": None,
        "geometry": None,
        "fps": None,
        "reason": candidate.get("reason"),
    }
    report = bundle.get("qc") or {}
    technical = (report.get("technical_facts") or {}) if isinstance(report, dict) else {}
    video = technical.get("video") or {}
    container = technical.get("format") or {}
    if video:
        width, height = video.get("width"), video.get("height")
        facts["geometry"] = "{}x{}".format(width, height) if width and height else None
        facts["frame_count"] = video.get("frame_count")
        facts["fps"] = video.get("r_frame_rate")
    if container.get("duration") is not None:
        try:
            facts["duration_seconds"] = float(container["duration"])
        except (TypeError, ValueError):
            facts["duration_seconds"] = None
    if facts["duration_seconds"] is None and facts["reason"] is None:
        facts["reason"] = "the QC report records no container duration for this candidate"
    return facts


def _field(name: str, supplied: Mapping[str, Any]) -> Dict[str, Any]:
    value = supplied.get(name)
    if isinstance(value, str):
        value = value.strip() or None
    return {
        "field": name,
        "value": value,
        "required": name in REQUIRED_CARD_FIELDS,
        "reason": None if value is not None else FIELD_REASONS[name],
        "provenance": "stated by the operator on this card" if value is not None else None,
    }


def _human_approval(project_dir: Path) -> Tuple[Optional[str], Dict[str, Any]]:
    """``(refusal, evidence)``. Delivery cannot be approved above an unapproved candidate."""
    try:
        state = ProjectState.load(project_dir / "state.json")
    except (StateError, OSError, ValueError) as exc:
        return "project state is unreadable: {}".format(exc), {}
    stage = state.stage("HUMAN_APPROVED")
    try:
        state.verify_stage("HUMAN_APPROVED")
    except StateError as exc:
        return (
            "HUMAN_APPROVED does not verify ({}); the operator approves the candidate in the review room before any delivery "
            "decision exists to record".format(exc)
        ), {}
    return None, {
        "receipt": stage.get("receipt"),
        "receipt_sha256": stage.get("receipt_sha256"),
        "input_hash": stage.get("input_hash"),
        "updated_at_utc": stage.get("updated_at_utc"),
        "outputs": [item.get("path") for item in stage.get("output_receipts") or []],
    }


def publish_card(config: StudioConfig, project_id: str, *, supplied: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Compose the approval card. Read-only: this function writes nothing, ever."""
    given = dict(supplied or {})
    bundle = review_bundle(config, project_id)
    project_dir = Path(bundle["project_dir"])
    refusal, approval = _human_approval(project_dir)

    fields = {name: _field(name, given) for name in REQUIRED_CARD_FIELDS + OPTIONAL_CARD_FIELDS}
    unresolved = [
        {"field": name, "required": entry["required"], "reason": entry["reason"]}
        for name, entry in fields.items()
        if entry["value"] is None
    ]
    missing_required = [item["field"] for item in unresolved if item["required"]]
    publication = _publication_receipt(project_dir)

    return {
        "status": "PASS",
        "project_id": project_id,
        "project_dir": str(project_dir),
        "revision": bundle["revision"],
        "post_type": POST_TYPE,
        "asset": _asset_facts(bundle),
        "reference": bundle["reference"],
        "qc_report": bundle["qc"],
        "comparison_board": bundle["board"],
        "human_approval": approval,
        "headline": bundle["headline"],
        "layers": bundle["layers"],
        **fields,
        "unresolved": unresolved,
        "missing_required": missing_required,
        "platform_limitations": list(PLATFORM_LIMITATIONS),
        "approval_refusal": refusal,
        "can_record": refusal is None,
        "ready_to_approve": refusal is None and not missing_required,
        "publication_receipt": publication.get("path"),
        "publication_reason": publication.get("reason"),
        "generated_at_utc": _now(),
    }


# --- recording the approval ------------------------------------------------


def _next_approval(project_dir: Path) -> Tuple[str, int]:
    directory = project_dir / APPROVAL_DIRECTORY
    existing = sorted(directory.glob("delivery-approval-*.json")) if directory.is_dir() else []
    sequence = len(existing) + 1
    return "delivery-approval-{:03d}.json".format(sequence), sequence


@guarded_mutation("publish approval recording")
def record_approval(
    config: StudioConfig,
    project_id: str,
    *,
    destination: str,
    caption: str,
    schedule: str,
    timezone_name: str,
    internal_note: Optional[str] = None,
    audio_label: Optional[str] = None,
    cover: Optional[str] = None,
    disclosure: Optional[str] = None,
    auto_expansion: Optional[bool] = None,
    reviewer: Optional[str] = None,
) -> Dict[str, Any]:
    """Record ``DELIVERY_APPROVED`` intent as a signed receipt. Performs no outward action."""
    supplied = {
        "destination": destination,
        "caption": caption,
        "schedule": schedule,
        "timezone": timezone_name,
        "internal_note": internal_note,
        "audio_label": audio_label,
        "cover": cover,
        "disclosure": disclosure,
        "auto_expansion": auto_expansion,
    }
    card = publish_card(config, project_id, supplied=supplied)
    if card["approval_refusal"] is not None:
        raise ReelctlError("delivery approval refused for {}: {}".format(project_id, card["approval_refusal"]))
    if card["missing_required"]:
        raise ReelctlError("the approval card is incomplete: {} carry no value".format(", ".join(card["missing_required"])))
    asset = card["asset"]
    if not asset.get("sha256"):
        raise ReelctlError("no approvable asset for {}: {}".format(project_id, asset.get("reason")))

    project_dir = Path(card["project_dir"])
    name, sequence = _next_approval(project_dir)
    receipt = sign_payload(
        {
            "schema_version": 1,
            "artifact_type": APPROVAL_PURPOSE,
            "intent": "DELIVERY_APPROVED",
            "project_id": project_id,
            "revision": card["revision"],
            "sequence": sequence,
            "post_type": POST_TYPE,
            "asset": {"path": asset["path"], "sha256": asset["sha256"], "bytes": asset["bytes"], "duration_seconds": asset["duration_seconds"]},
            "reference": {"path": card["reference"].get("path"), "sha256": card["reference"].get("sha256")},
            "qc_report": {"path": card["qc_report"].get("path"), "sha256": card["qc_report"].get("sha256")},
            "comparison_board": {"path": card["comparison_board"].get("path"), "sha256": card["comparison_board"].get("sha256")},
            "human_approval": card["human_approval"],
            "card": {name: card[name]["value"] for name in REQUIRED_CARD_FIELDS + OPTIONAL_CARD_FIELDS},
            "unresolved": card["unresolved"],
            "platform_limitations": card["platform_limitations"],
            "layers_at_approval": {layer["key"]: layer["status"] for layer in card["layers"]},
            "reviewer": (reviewer or "operator").strip() or "operator",
            "reviewer_provenance": "recorded through the loopback-only studio publish card",
            "outward_action_performed": False,
            "outward_action_owner": "the operator, outside this app; publishing is fully manual in v1",
            "recorded_at_utc": _now(),
            "reelctl_version": __version__,
        },
        purpose=APPROVAL_PURPOSE,
    )
    directory = secure_mkdirs(project_dir, APPROVAL_DIRECTORY)
    atomic_write_json(directory / name, receipt, root=project_dir)
    relative = "{}/{}".format(APPROVAL_DIRECTORY, name)

    state = ProjectState.load(project_dir / "state.json")
    try:
        state.complete("DELIVERY_APPROVED", asset["sha256"], [relative])
    except StateError as exc:
        raise ReelctlError("the delivery approval for {} is on disk but the stage refused it: {}".format(project_id, exc)) from exc

    append_decision_log(project_dir, project_id, _log_entry(receipt=receipt, relative=relative))
    captures = schedule_metric_captures(config, project_id)

    event_error: Optional[str] = None
    try:
        record_event(
            config.database_path,
            kind="delivery",
            project_id=project_id,
            payload={"intent": "DELIVERY_APPROVED", "sequence": sequence, "receipt": relative, "captures_scheduled": len(captures["scheduled"])},
        )
    except EventLogError as exc:
        event_error = str(exc)

    return {
        "status": "PASS",
        "project_id": project_id,
        "intent": "DELIVERY_APPROVED",
        "sequence": sequence,
        "receipt": str(project_dir / relative),
        "receipt_relative": relative,
        "asset_sha256": asset["sha256"],
        "outward_action_performed": False,
        "captures_scheduled": len(captures["scheduled"]),
        "capture_reason": captures["reason"],
        "next_stage": state.next_stage(),
        "event_error": event_error,
    }


def _log_entry(*, receipt: Mapping[str, Any], relative: str) -> str:
    card = receipt.get("card") or {}
    return "\n".join(
        [
            "\n---\n",
            "## {} — delivery approval recorded (Reel Studio publish card)\n".format(receipt["recorded_at_utc"]),
            "**D-{}-delivery-{:03d}** — DELIVERY_APPROVED intent recorded for revision {}. **No outward action was performed.**\n".format(
                receipt["recorded_at_utc"][:10], receipt["sequence"], receipt.get("revision")
            ),
            "*Asset:* `{}` sha256 `{}`\n".format((receipt.get("asset") or {}).get("path"), (receipt.get("asset") or {}).get("sha256")),
            "*Card:* destination {} · post type {} · schedule {} ({}) · caption {!r}\n".format(
                card.get("destination"), receipt.get("post_type"), card.get("schedule"), card.get("timezone"), card.get("caption")
            ),
            "*Receipt:* `{}`\n".format(relative),
            "*The scheduler call, the upload and the post remain the operator's, performed outside this app.*\n",
        ]
    )


# --- metric capture scheduling ---------------------------------------------


def _publication_receipt(project_dir: Path) -> Dict[str, Any]:
    """Find the publication receipt the operator dropped in after posting, if any."""
    directory = Path(project_dir) / APPROVAL_DIRECTORY
    if not directory.is_dir():
        return {"path": None, "payload": None, "reason": "no publication receipt: {} does not exist".format(directory)}
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("schema") == PUBLICATION_SCHEMA:
            return {"path": str(path), "payload": payload, "reason": None}
    return {
        "path": None,
        "payload": None,
        "reason": "no publication receipt ({}) is on disk yet; publishing is manual in v1, so the capture clock does not "
        "start until the operator files one".format(PUBLICATION_SCHEMA),
    }


def _existing_captures(database_path: Path, project_id: str) -> List[Dict[str, Any]]:
    with connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, due_at_utc, payload_json FROM schedules WHERE project_id = ? AND kind = ?", (project_id, CAPTURE_KIND)
        ).fetchall()
    return [dict(row) for row in rows]


@guarded_mutation("metric capture scheduling")
def schedule_metric_captures(config: StudioConfig, project_id: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Insert the +24h and +72h capture rows, once, keyed off the publication time (§8.4)."""
    project_dir = _project_dir(config, project_id)
    found = _publication_receipt(project_dir)
    if found["payload"] is None:
        return {"scheduled": [], "reason": found["reason"], "publication_receipt": None}

    payload = found["payload"]
    published_raw = payload.get("published_at")
    if not published_raw:
        return {
            "scheduled": [],
            "reason": "{} records no published_at, so the capture windows cannot be computed; late is fine, guessed is not".format(
                found["path"]
            ),
            "publication_receipt": found["path"],
        }
    try:
        published = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
    except ValueError:
        return {
            "scheduled": [],
            "reason": "{} records published_at {!r}, which is not an ISO-8601 instant".format(found["path"], published_raw),
            "publication_receipt": found["path"],
        }
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)

    try:
        existing = _existing_captures(config.database_path, project_id)
    except (sqlite3.Error, OSError) as exc:
        return {"scheduled": [], "reason": "the schedule table is unreadable: {}".format(exc), "publication_receipt": found["path"]}
    if existing:
        return {
            "scheduled": [],
            "reason": "the {} capture windows for {} are already scheduled ({} rows); scheduling is idempotent so a re-run never "
            "duplicates a wake".format(CAPTURE_KIND, project_id, len(existing)),
            "publication_receipt": found["path"],
        }

    scheduled: List[Dict[str, Any]] = []
    for window, hours in CAPTURE_WINDOWS:
        due = published + timedelta(hours=hours)
        schedule_id = jobs.schedule(
            config.database_path,
            project_id=project_id,
            kind=CAPTURE_KIND,
            due_at=due,
            payload={
                "window": window,
                "hours_after_publication": hours,
                "published_at": str(published_raw),
                "experiment_id": payload.get("experiment_id"),
                "instagram_url": (payload.get("instagram") or {}).get("public_url"),
                "publication_receipt": found["path"],
                "minimum_metrics": list(MINIMUM_METRICS),
                "execution": "operator/factory loop; this app makes no provider call",
            },
            now=now,
        )
        scheduled.append({"schedule_id": schedule_id, "window": window, "due_at_utc": _stamp(due)})
    return {
        "scheduled": scheduled,
        "reason": "scheduled {} captures from published_at {}".format(len(scheduled), published_raw),
        "publication_receipt": found["path"],
    }


# --- the learning ledger ---------------------------------------------------


def _load_ledger(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"schema": LEDGER_SCHEMA, "updated_at_utc": _now(), "doctrine_rule": DOCTRINE_RULE, "trials": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReelctlError("{} will not parse ({}); refusing to overwrite the learning ledger".format(path, exc)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("trials"), list):
        raise ReelctlError("{} is not a {} document; refusing to overwrite it".format(path, LEDGER_SCHEMA))
    return payload


@guarded_mutation("publish ledger append")
def append_ledger_snapshot(
    config: StudioConfig,
    project_id: str,
    *,
    window: str,
    metrics: Mapping[str, Any],
    captured_at: Optional[str] = None,
    experiment_id: Optional[str] = None,
) -> Dict[str, Any]:
    """File one capture into ``learning/LEDGER.json``. Appends; never rewrites a prior entry."""
    known = {name for name, _ in CAPTURE_WINDOWS}
    if window not in known:
        raise ReelctlError("unknown capture window {!r}; expected one of {}".format(window, ", ".join(sorted(known))))

    project_dir = _project_dir(config, project_id)
    found = _publication_receipt(project_dir)
    payload = found["payload"] or {}
    trial_id = experiment_id or payload.get("experiment_id")
    if not trial_id:
        raise ReelctlError(
            "no experiment id for {}: {}; a snapshot cannot be filed against an unidentified trial".format(project_id, found["reason"])
        )

    path = ledger_path(config)
    ledger = _load_ledger(path)
    trials: List[Dict[str, Any]] = ledger["trials"]
    index = next((position for position, trial in enumerate(trials) if trial.get("experiment_id") == trial_id), None)
    if index is None:
        trials.append(
            {
                "experiment_id": trial_id,
                "project_id": project_id,
                "published_at": payload.get("published_at"),
                "instagram_url": (payload.get("instagram") or {}).get("public_url"),
                "snapshots": {},
            }
        )
        index = len(trials) - 1
    trial = trials[index]
    snapshots = trial.setdefault("snapshots", {})
    if window in snapshots:
        raise ReelctlError(
            "the {} snapshot for {} is already recorded (captured {}); the ledger is append-only, so a re-capture supersedes it "
            "under a new trial rather than overwriting this one".format(window, trial_id, snapshots[window].get("captured_at_utc"))
        )

    withheld = [name for name in MINIMUM_METRICS if metrics.get(name) is None]
    snapshot: Dict[str, Any] = {"captured_at_utc": captured_at or _now()}
    snapshot.update({name: metrics.get(name) for name in MINIMUM_METRICS})
    snapshot.update({name: value for name, value in metrics.items() if name not in MINIMUM_METRICS})
    if withheld:
        snapshot["withheld_metrics"] = withheld
        snapshot["withheld_reason"] = "not supplied by the capture; recorded null rather than zero, which would read as measured"
    snapshots[window] = snapshot
    ledger["updated_at_utc"] = _now()
    ledger.setdefault("doctrine_rule", DOCTRINE_RULE)

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, ledger, root=path.parent)

    event_error: Optional[str] = None
    try:
        record_event(
            config.database_path,
            kind="capture",
            project_id=project_id,
            payload={"window": window, "experiment_id": trial_id, "withheld_metrics": withheld, "ledger": str(path)},
        )
    except EventLogError as exc:
        event_error = str(exc)

    return {
        "status": "PASS",
        "project_id": project_id,
        "experiment_id": trial_id,
        "window": window,
        "ledger": str(path),
        "withheld_metrics": withheld,
        "trial_index": index,
        "event_error": event_error,
    }


def due_captures(config: StudioConfig, *, now: Optional[datetime] = None) -> Sequence[Dict[str, Any]]:
    """Capture wakes that are due. Reading them is all this app does with them."""
    return [row for row in jobs.due_schedules(config.database_path, now=now) if row.get("kind") == CAPTURE_KIND]
