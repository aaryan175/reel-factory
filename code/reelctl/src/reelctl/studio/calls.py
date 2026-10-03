"""The calls inbox: every open ``CALL_REQUIRED`` card, and the operator's answer to it.

Three rules shape this module, and each is a direct reading of the doctrine.

**The registry is the store of what is open; the brain file is the body.** ``open_calls``
in ``REEL_REGISTRY.json`` says which decisions are outstanding (§5.1(5)); a project's
``brain/07_CALL_PROMPTS.md`` holds the card text in the onboarding prompt's exact schema.
The two are joined here rather than merged: a card block that nothing declares open is
*not* resurrected into the inbox — that file is append-only, so an answered call's card
never leaves it — but it is counted and named in ``unlisted_cards`` so it is not silently
dropped either. The one addition to the registry's list is the intake record's own call:
intake deliberately does not write the registry (§8.1 single writer), so its mode call
would otherwise be invisible.

**An answer is bound to the question that was asked.** The receipt carries the card
file's sha256 *and* a hash of the parsed card body, so an answer cannot be made to look
current by rewriting the card afterwards — the inbox reports ``binds_current_card: false``
with the reason instead.

**Answers are append-only.** A second answer to the same call supersedes the first by
writing a new numbered receipt; nothing on disk is rewritten, in the decision log or in
``review/calls/``. That is the same discipline ``08_DECISION_LOG.md`` states about itself.

Nothing here writes ``REEL_REGISTRY.json``. Releasing a stalled job means deleting the
scheduler rows that recorded the stall — ``studio.db`` is machine scheduling state, never
an authority — after which the daemon's next tick re-derives the project's next stage from
disk and schedules it again.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import __version__
from ..errors import ReelctlError
from ..hashing import atomic_write_json, recipe_hash, sha256_file
from ..paths import PathSafetyError, canonical_root, confined_path, secure_mkdirs
from ..signing import sign_payload
from ..state import STAGES
from . import jobs, opencalls
from .board import project_ids
from .authority import guarded_mutation
from .config import StudioConfig
from .events import EventLogError, record_event
from .registry import registry_report
from .stages import call_gate_index

CALL_CARD_RELATIVE = "brain/07_CALL_PROMPTS.md"
DECISION_LOG_RELATIVE = "brain/08_DECISION_LOG.md"
#: One spelling of the answers directory; ``opencalls`` owns it because the assembly
#: overlays answered-ness (the intake record's filename moved there for the same reason).
ANSWER_DIRECTORY = opencalls.ANSWER_DIRECTORY

AWAITING = "AWAITING_OPERATOR"
ANSWERED = "ANSWERED"

ANSWER_PURPOSE = "studio-call-answer-v1"
DEFAULT_REVIEWER = "operator"

#: Job statuses that mean "this project stopped and is waiting on a human" (§6.1 stall rule).
STALLED_STATUSES = ("FAILED", "WITHHELD")

#: A call id becomes a filename, so it is constrained before it is ever joined to a path.
CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

MARKER = "CALL_REQUIRED"
FENCE = "```"
# ``stage`` and ``layer`` are not in the onboarding prompt's header list, but a card that
# declares which stage its decision governs is read here rather than ignored — that field is
# what the stage-scoped gate keys on (registry ``daemon_contract.calls_gate_design``).
HEADER_KEYS = ("call_id", "severity", "project_id", "revision", "reference_block", "issue_type", "stage", "layer")
HEADER_PATTERN = re.compile(r"^([a-z][a-z_]*):\s*(.*)$")
OPTION_PATTERN = re.compile(r"^([A-Z])\.\s+(.*)$")

SECTION_HEADINGS = {
    "WHAT THE REFERENCE REQUIRES:": "what_the_reference_requires",
    "WHAT I FOUND:": "what_i_found",
    "WHY IT DOES NOT MATCH:": "why_it_does_not_match",
    "EVIDENCE:": "evidence",
    "OPTIONS:": "options",
    "MY RECOMMENDATION:": "recommendation",
    "REPLY WITH:": "reply_with",
}
PROSE_SECTIONS = ("what_the_reference_requires", "what_i_found", "why_it_does_not_match", "recommendation", "reply_with")

#: What an answer is bound to. Volatile fields (source, state, paths) are deliberately out:
#: the question is the card's content, not where it was found.
IDENTITY_FIELDS = (
    "call_id",
    "severity",
    "project_id",
    "revision",
    "reference_block",
    "issue_type",
    # The gate scope is part of the question: moving which stage a call governs changes what
    # the operator is being asked to decide about, so an answer must not survive that change.
    "stage",
    "layer",
    "what_the_reference_requires",
    "what_i_found",
    "why_it_does_not_match",
    "evidence",
    "options",
    "recommendation",
    "reply_with",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --- the card parser -------------------------------------------------------


def _empty_card() -> Dict[str, Any]:
    card: Dict[str, Any] = {key: None for key in HEADER_KEYS}
    card.update({name: None for name in PROSE_SECTIONS})
    card["evidence"] = []
    card["options"] = []
    return card


def _finish_prose(lines: List[str]) -> Optional[str]:
    text = "\n".join(lines).strip("\n").strip()
    return text or None


def _parse_options(lines: Sequence[str]) -> List[Dict[str, str]]:
    options: List[Dict[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = OPTION_PATTERN.match(stripped)
        if match:
            options.append({"key": match.group(1), "text": match.group(2).strip()})
        elif options:
            options[-1]["text"] = "{} {}".format(options[-1]["text"], stripped).strip()
    return options


def _parse_block(lines: Sequence[str]) -> Dict[str, Any]:
    card = _empty_card()
    section: Optional[str] = None
    buffer: List[str] = []
    last_key: Optional[str] = None

    def flush() -> None:
        if section is None:
            return
        if section == "evidence":
            card["evidence"] = [line.strip()[2:].strip() for line in buffer if line.strip().startswith("- ")]
        elif section == "options":
            card["options"] = _parse_options(buffer)
        else:
            card[section] = _finish_prose(buffer)

    for line in lines:
        stripped = line.strip()
        if stripped in SECTION_HEADINGS:
            flush()
            section = SECTION_HEADINGS[stripped]
            buffer = []
            continue
        if section is not None:
            buffer.append(line)
            continue
        match = HEADER_PATTERN.match(stripped)
        if match and match.group(1) in HEADER_KEYS:
            last_key = match.group(1)
            card[last_key] = match.group(2).strip() or None
        elif stripped and last_key:
            # A wrapped header value — ``reference_block`` routinely spans lines. Losing the
            # continuation would silently narrow which blocks the call is about.
            card[last_key] = "{} {}".format(card[last_key] or "", stripped).strip()
    flush()
    return card


def parse_call_cards(text: str) -> List[Dict[str, Any]]:
    """Every ``CALL_REQUIRED`` block in one markdown file, in the order they appear."""
    lines = str(text).splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == MARKER]
    cards: List[Dict[str, Any]] = []
    for position, start in enumerate(starts):
        limit = starts[position + 1] if position + 1 < len(starts) else len(lines)
        body: List[str] = []
        for line in lines[start + 1 : limit]:
            if line.strip() == FENCE:
                break
            body.append(line)
        card = _parse_block(body)
        if card.get("call_id"):
            cards.append(card)
    return cards


def call_identity(card: Dict[str, Any]) -> str:
    """A hash of the question, so an answer cannot be re-pointed at a different one."""
    return recipe_hash({key: card.get(key) for key in IDENTITY_FIELDS})


# --- reading a project -----------------------------------------------------


def _read_text(path: Path) -> Tuple[Optional[str], Optional[str]]:
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, "{} does not exist".format(path)
    except (OSError, ValueError) as exc:
        return None, "{} is unreadable: {}".format(path, exc)


def _cards_in_project(project_dir: Path) -> Tuple[Dict[str, Dict[str, Any]], Optional[str], Optional[str]]:
    """``(cards by id, card sha256, error)``. A missing card file is a named absence."""
    try:
        path = confined_path(project_dir, CALL_CARD_RELATIVE, require="file", allow_missing=True)
    except PathSafetyError as exc:
        return {}, None, str(exc)
    text, error = _read_text(path)
    if text is None:
        return {}, None, error
    cards = {card["call_id"]: card for card in parse_call_cards(text)}
    return cards, sha256_file(path), None


#: Moved into the ``opencalls`` leaf so the daemon's assembly can apply the same overlay
#: the inbox always has; re-exported here because this module's consumers and tests
#: reach it by this name.
call_answers = opencalls.call_answers


def _answer_view(answer: Optional[Dict[str, Any]], current_identity: Optional[str]) -> Optional[Dict[str, Any]]:
    if answer is None:
        return None
    bound = current_identity is not None and answer.get("call_sha256") == current_identity
    note = (
        "this answer binds the card as it read when it was recorded"
        if bound
        else "the card changed after this answer was recorded, so the answer no longer binds the card on disk"
    )
    if current_identity is None:
        note = "the current card body could not be read, so this answer cannot be checked against it"
    return {
        "option": answer.get("option"),
        "option_text": answer.get("option_text"),
        "answer_text": answer.get("answer_text"),
        "reviewer": answer.get("reviewer"),
        "answered_at_utc": answer.get("answered_at_utc"),
        "sequence": answer.get("sequence"),
        "receipt": answer.get("receipt_path"),
        "supersedes": answer.get("supersedes"),
        "binds_current_card": bound,
        "binding_note": note,
    }


def _gate(card: Mapping[str, Any]) -> Dict[str, Any]:
    """What this call actually stops, in the stage-scoped vocabulary the daemon uses.

    ``stages.call_gate_index`` is the single implementation of the rule and both the daemon's
    SCHEDULE gate and the board's layer model already call it; the inbox calls it too rather
    than re-deriving, so the operator is never shown a scope that differs from the one being
    enforced. A call that cannot be placed gates everything — fail closed.
    """
    gate = call_gate_index(card)
    if gate is None:
        return {
            "gates_from_stage": None,
            "gates_everything": True,
            "stages_proceeding": [],
            "stages_waiting": list(STAGES),
            "gate_note": "this call declares no stage, so every stage waits on it; an unrecognised gate is never an open door",
        }
    return {
        "gates_from_stage": STAGES[gate],
        "gates_everything": False,
        "stages_proceeding": list(STAGES[:gate]),
        "stages_waiting": list(STAGES[gate:]),
        "gate_note": "gates {} onward; the {} stage(s) before it keep running while this call is open".format(STAGES[gate], gate),
    }


def _compose(
    *,
    call_id: str,
    project_id: str,
    source: str,
    declared_state: Optional[str],
    card: Optional[Dict[str, Any]],
    card_path: Optional[str],
    card_sha256: Optional[str],
    body_error: Optional[str],
    answer: Optional[Dict[str, Any]],
    declaration: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    body = card or _empty_card()
    identity = call_identity(card) if card else None
    state = ANSWERED if answer is not None else (declared_state or AWAITING)
    # A declaration can carry the gate without an authored card body — a registry row that
    # names its stage but whose card block is missing still gates exactly that stage. Losing
    # it here would fail closed onto "gates everything", which over-blocks a reel silently.
    body = {**body, "stage": body.get("stage") or (declaration or {}).get("stage")}
    body["layer"] = body.get("layer") or (declaration or {}).get("layer")
    return {
        "call_id": call_id,
        "project_id": project_id,
        "source": source,
        "state": state,
        "severity": body.get("severity"),
        "issue_type": body.get("issue_type"),
        "revision": body.get("revision"),
        "reference_block": body.get("reference_block"),
        "stage": body.get("stage"),
        "layer": body.get("layer"),
        **_gate(body),
        "what_the_reference_requires": body.get("what_the_reference_requires"),
        "what_i_found": body.get("what_i_found"),
        "why_it_does_not_match": body.get("why_it_does_not_match"),
        "evidence": list(body.get("evidence") or []),
        "options": [dict(option) for option in body.get("options") or []],
        "recommendation": body.get("recommendation"),
        "reply_with": body.get("reply_with"),
        "card_path": card_path,
        "card_sha256": card_sha256,
        "call_sha256": identity,
        "body_error": body_error,
        "answer": _answer_view(answer, identity),
    }


def _with_gate(card: Optional[Dict[str, Any]], *sources: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Fill ``stage``/``layer`` from the first source that declares one.

    The markdown card is the authored question, but ``intake.call_card_markdown`` writes only
    the onboarding prompt's header fields and ``stage`` is not among them — so a body parsed
    back out of ``07_CALL_PROMPTS.md`` has no gate at all. Preferring that body wholesale
    would turn a call that gates ``SELECTION_LOCKED`` into one that gates everything, and the
    intake mode call would deadlock the reel it was issued for. The registry entry and the
    intake record both carry the field structurally; either fills the gap.
    """
    if card is None:
        return None
    merged = dict(card)
    for key in ("stage", "layer"):
        if merged.get(key):
            continue
        for source in sources:
            value = (source or {}).get(key)
            if value:
                merged[key] = value
                break
    return merged


def registry_record(call: Mapping[str, Any]) -> Dict[str, Any]:
    """One ``open_calls`` row, shaped exactly as the registry stores them.

    Folding a call into ``REEL_REGISTRY.json`` is then a copy rather than a reconstruction —
    and, critically, it carries ``stage`` forward, which is the field the daemon's gate reads.
    Dropping it here would be indistinguishable from a call that gates every stage.
    """
    project = call.get("project_id") or call.get("project")
    card = str(call.get("card_path") or call.get("card") or CALL_CARD_RELATIVE)
    if project and not card.startswith("{}/".format(project)):
        card = "{}/{}".format(project, card)
    return {
        "call_id": str(call.get("call_id")),
        "project": str(project) if project else None,
        "card": card,
        "state": str(call.get("state") or AWAITING),
        "stage": call.get("stage"),
    }


def _project_calls(project_dir: Path, project_id: str, declared: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Presentation on top of the shared assembly — card body, answer state, gate projection.

    ``declared`` arrives already merged, deduped and source-tagged by ``opencalls``. Nothing
    in this module reads ``REEL_REGISTRY.json`` or ``intake.json`` to decide *which* calls
    exist; that source-reading lives in exactly one place, so the daemon, the board and this
    inbox cannot drift apart. What is added here is what only a screen needs: the authored
    card body, the answer receipts, and the gate stated in words.
    """
    cards, card_sha, card_error = _cards_in_project(project_dir)
    answers = call_answers(project_dir)
    composed: List[Dict[str, Any]] = []
    claimed: set = set()

    for entry in declared:
        call_id = str(entry.get("call_id") or "").strip()
        if not call_id:
            continue
        claimed.add(call_id)
        source = str(entry.get("source") or opencalls.REGISTRY_SOURCE)
        authored = cards.get(call_id)
        from_record = source == opencalls.INTAKE_SOURCE
        # The declaration backfills the gate: `intake.call_card_markdown` writes no `stage:`
        # header, so a body parsed out of the card alone carries no gate at all.
        card = _with_gate(authored, entry)
        if card is None and from_record:
            card = {key: entry.get(key) for key in IDENTITY_FIELDS}
        composed.append(
            _compose(
                call_id=call_id,
                project_id=project_id,
                source=source,
                declared_state=str(entry.get("state") or AWAITING),
                card=card,
                card_path=CALL_CARD_RELATIVE
                if authored is not None or not from_record
                else opencalls.INTAKE_RECORD_FILENAME,
                card_sha256=card_sha if authored is not None else None,
                body_error=None
                if (authored is not None or from_record)
                else (card_error or "{} carries no CALL_REQUIRED block with this call_id".format(CALL_CARD_RELATIVE)),
                answer=answers.get(call_id),
                declaration=entry,
            )
        )

    unlisted = [call_id for call_id in cards if call_id not in claimed]
    return composed, unlisted


def inbox(config: StudioConfig, *, include_answered: bool = False) -> Dict[str, Any]:
    """Every open call across every project, plus what was found and deliberately not shown."""
    root = canonical_root(config.projects_root, create=True)
    registry = registry_report(getattr(config, "registry_path", config.projects_root))
    # Which projects to ask about. This is a key set, not an assembly of calls: every actual
    # call row below comes from `opencalls.project_open_calls`.
    named = {str(call.get("project")) for call in registry.get("open_calls") or [] if call.get("project")}
    on_disk = project_ids(root)

    calls: List[Dict[str, Any]] = []
    unlisted_cards: List[Dict[str, Any]] = []
    for project_id in sorted(named | set(on_disk)):
        directory = root / project_id
        entries = opencalls.project_open_calls(registry, directory)
        if not (directory / "state.json").is_file():
            for entry in entries:
                calls.append(
                    _compose(
                        call_id=str(entry.get("call_id") or ""),
                        project_id=project_id,
                        source="registry open_calls",
                        declared_state=str(entry.get("state") or AWAITING),
                        card=None,
                        card_path=str(entry.get("card")),
                        card_sha256=None,
                        body_error="the registry names project {} but no project directory exists at {}".format(project_id, directory),
                        answer=None,
                        declaration=entry,
                    )
                )
            continue
        project_calls, unlisted = _project_calls(directory, project_id, entries)
        calls.extend(project_calls)
        unlisted_cards.extend(
            {
                "project_id": project_id,
                "call_id": call_id,
                "note": "{} holds this card but neither the registry nor an intake record declares it open".format(CALL_CARD_RELATIVE),
            }
            for call_id in unlisted
        )

    visible = [call for call in calls if include_answered or call["state"] != ANSWERED]
    by_project: Dict[str, List[str]] = {}
    for call in visible:
        if call["state"] != ANSWERED:
            by_project.setdefault(call["project_id"], []).append(call["call_id"])
    return {
        "status": "PASS",
        "generated_at_utc": _now(),
        "registry": {key: registry[key] for key in ("status", "path", "error")},
        "calls": visible,
        "by_project": by_project,
        "unlisted_cards": unlisted_cards,
        "open_count": sum(1 for call in calls if call["state"] != ANSWERED),
    }


# A ``merged_open_calls(config)`` wrapper lived here — ``inbox`` grouped by project. It is
# gone, and nothing should reintroduce it. Group ``inbox(config, include_answered=True)
# ["calls"]`` at the call site instead: the wrapper sat between callers and the thing they
# were actually reading, which added a layer without adding a guarantee. Its only consumer
# was ever a test, and a test kept alive by an otherwise-unused production symbol is the tail
# wagging the dog.
#
# Which calls exist is decided in exactly one place, ``opencalls.project_open_calls``. This
# module reads that and adds presentation; ``test_the_inbox_reads_its_calls_through_the_shared_assembly``
# holds the coupling by substitution rather than by convention.


# --- answering -------------------------------------------------------------


def _find(config: StudioConfig, call_id: str) -> Dict[str, Any]:
    model = inbox(config, include_answered=True)
    matches = [call for call in model["calls"] if call["call_id"] == call_id]
    if not matches:
        known = ", ".join(sorted({call["call_id"] for call in model["calls"]})) or "none"
        raise ReelctlError("no open call {!r}; the inbox holds: {}".format(call_id, known))
    if len(matches) > 1:
        owners = ", ".join(sorted(call["project_id"] for call in matches))
        raise ReelctlError("call {!r} is declared by more than one project ({}); answer it per project".format(call_id, owners))
    return matches[0]


def _chosen_option(call: Dict[str, Any], option: Optional[str]) -> Optional[Dict[str, str]]:
    if option is None:
        return None
    key = option.strip().upper()
    offered = {str(item["key"]): item for item in call["options"]}
    if not offered:
        raise ReelctlError(
            "the card body for {} could not be read ({}), so option {!r} cannot be checked against it; answer in free text instead".format(
                call["call_id"], call.get("body_error") or "no options parsed", option
            )
        )
    if key not in offered:
        raise ReelctlError("option {!r} is not offered by {}; it offers: {}".format(option, call["call_id"], ", ".join(sorted(offered))))
    return offered[key]


def _next_receipt_name(project_dir: Path, call_id: str) -> Tuple[str, int, Optional[str]]:
    directory = Path(project_dir) / ANSWER_DIRECTORY
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    existing = sorted(
        path.name for path in paths if path.name == "{}.answer.json".format(call_id) or path.name.startswith("{}.answer-".format(call_id))
    )
    sequence = len(existing) + 1
    name = "{}.answer.json".format(call_id) if sequence == 1 else "{}.answer-{}.json".format(call_id, sequence)
    return name, sequence, existing[-1] if existing else None


@guarded_mutation("operator decision-log append")
def append_decision_log(project_dir: Path, project_id: str, entry: str) -> str:
    """Append-only, ``O_APPEND``, never a read-modify-write of a file the factory relies on.

    Shared with the review room: a call answer and a human verdict are both operator
    decisions and both belong in ``08_DECISION_LOG.md``, written the same way.
    """
    directory = secure_mkdirs(project_dir, "brain")
    path = directory / Path(DECISION_LOG_RELATIVE).name
    header = (
        "# 08 — DECISION LOG — `{}`\n\n"
        "Append-only. Newest entries at the bottom. Never rewrite a prior entry; supersede it with a new one.\n".format(project_id)
    )
    payload = ("" if path.is_file() else header) + entry
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return str(path)


def _log_entry(*, receipt: Dict[str, Any], relative: str, receipt_sha256: str, released: List[int]) -> str:
    option = receipt.get("option")
    headline = "answered: {}".format(option) if option else "answered: free text only"
    if receipt.get("option_text"):
        headline = "{} — {}".format(headline, receipt["option_text"])
    lines = [
        "\n---\n",
        "## {} — operator answer to {} (Reel Studio calls inbox)\n".format(receipt["answered_at_utc"], receipt["call_id"]),
        "**D-{}-{}** — {}\n".format(receipt["answered_at_utc"][:10], receipt["call_id"], headline),
    ]
    if receipt.get("answer_text"):
        lines.append("*Operator text:* {}\n".format(receipt["answer_text"]))
    lines.append("*Receipt:* `{}` (sha256 `{}`)\n".format(relative, receipt_sha256))
    lines.append(
        "*Bound to:* card `{}` sha256 `{}`, question sha256 `{}`\n".format(
            receipt.get("card_path"), receipt.get("card_sha256"), receipt.get("call_sha256")
        )
    )
    lines.append(
        "*Released:* {} stalled scheduler row(s); the daemon re-derives this project's next stage from disk on its next tick.\n".format(
            len(released)
        )
    )
    lines.append("*Recorded by:* reelctl {} on behalf of {}\n".format(receipt.get("reelctl_version"), receipt.get("reviewer")))
    return "\n".join(lines)


def release_stalled_jobs(database_path: Path, project_id: str) -> Dict[str, Any]:
    """Drop the scheduler rows that recorded the stall for exactly one project.

    ``studio.db`` is machine state, not authority (§8.1): deleting a stalled row asserts
    nothing about the reel. It only stops ``is_terminally_failed`` from vetoing the next
    schedule, which is what "the answer released the job" means in practice.
    """
    try:
        rows = [row for row in jobs.all_jobs(database_path) if row.get("project_id") == project_id and row.get("status") in STALLED_STATUSES]
        for row in rows:
            jobs.drop(database_path, int(row["id"]))
    except (sqlite3.Error, OSError) as exc:
        return {"released": [], "error": "stalled jobs for {} could not be released: {}".format(project_id, exc)}
    return {"released": [int(row["id"]) for row in rows], "error": None}


@guarded_mutation("operator call answer")
def answer_call(
    config: StudioConfig,
    call_id: str,
    *,
    option: Optional[str] = None,
    text: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one hash-bound answer, file it in the owning project's decision log, release."""
    identifier = str(call_id).strip()
    if not CALL_ID_PATTERN.match(identifier):
        raise ReelctlError("call id {!r} is not a safe identifier; it becomes a receipt filename".format(call_id))
    answer_text = (text or "").strip() or None
    if option is None and answer_text is None:
        raise ReelctlError("an answer must name an option from the card or say in text what is needed; it carried neither")

    call = _find(config, identifier)
    chosen = _chosen_option(call, option)
    project_id = call["project_id"]
    root = canonical_root(config.projects_root, create=True)
    project_dir = root / project_id
    if not (project_dir / "state.json").is_file():
        raise ReelctlError("call {} names project {}, which has no project directory at {}".format(identifier, project_id, project_dir))

    name, sequence, previous = _next_receipt_name(project_dir, identifier)
    receipt = sign_payload(
        {
            "schema_version": 1,
            "artifact_type": ANSWER_PURPOSE,
            "call_id": identifier,
            "project_id": project_id,
            "sequence": sequence,
            "supersedes": previous,
            "source": call["source"],
            "issue_type": call.get("issue_type"),
            "severity": call.get("severity"),
            "option": chosen["key"] if chosen else None,
            "option_text": chosen["text"] if chosen else None,
            "answer_text": answer_text,
            "reviewer": (reviewer or DEFAULT_REVIEWER).strip() or DEFAULT_REVIEWER,
            "reviewer_provenance": "recorded through the loopback-only studio calls inbox",
            "card_path": call.get("card_path"),
            "card_sha256": call.get("card_sha256"),
            "call_sha256": call.get("call_sha256"),
            "reply_with": call.get("reply_with"),
            "answered_at_utc": _now(),
            "reelctl_version": __version__,
        },
        purpose=ANSWER_PURPOSE,
    )
    directory = secure_mkdirs(project_dir, *Path(ANSWER_DIRECTORY).parts)
    path = directory / name
    atomic_write_json(path, receipt, root=project_dir)
    relative = "{}/{}".format(ANSWER_DIRECTORY, name)

    released = release_stalled_jobs(config.database_path, project_id)
    log_path = append_decision_log(
        project_dir,
        project_id,
        _log_entry(receipt=receipt, relative=relative, receipt_sha256=sha256_file(path), released=released["released"]),
    )

    event_error: Optional[str] = None
    try:
        record_event(
            config.database_path,
            kind="call",
            project_id=project_id,
            payload={
                "call_id": identifier,
                "option": receipt["option"],
                "sequence": sequence,
                "released_jobs": len(released["released"]),
                "receipt": relative,
            },
        )
    except EventLogError as exc:
        event_error = str(exc)

    return {
        "status": "PASS",
        "call_id": identifier,
        "project_id": project_id,
        "option": receipt["option"],
        "sequence": sequence,
        "supersedes": previous,
        "receipt": str(path),
        "receipt_relative": relative,
        "decision_log": log_path,
        "released_jobs": len(released["released"]),
        "release_error": released["error"],
        "event_error": event_error,
    }
