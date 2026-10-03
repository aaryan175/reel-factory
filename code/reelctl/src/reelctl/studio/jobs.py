"""The job queue, event log and wake schedule over ``studio.db`` (§6.1, §8.1).

Every row here is disposable — delete the database and ``db.rebuild_from_disk`` puts the
world back. What the queue buys is the thing a JSON file is bad at: concurrent-safe
enqueue, due-time queries, and an append-only stream the UI can tail.

Two behaviours are load-bearing rather than incidental:

* **One active job per project.** Enforced by the partial unique index in the schema, not
  by the daemon remembering. A second enqueue returns ``None`` instead of racing.
* **A terminal failure stays terminal for that input.** ``is_terminally_failed`` is keyed
  on ``(project, stage, input_hash)``, so a stuck stage stops being retried until disk
  moves — CLAUDE.md's stall rule, in SQL.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import events
from .authority import guarded_mutation
from .db import connect

ACTIVE_STATUSES: Tuple[str, ...] = ("QUEUED", "CLAIMED", "RUNNING")
DEFAULT_BACKOFF_SECONDS: Tuple[int, ...] = (60, 300, 900)

#: The disposition of a queued row the daemon declined to start (see ``withhold``). The
#: word is deliberately the same one ``runner`` uses for a stage result and ``status`` uses
#: for a layer: to the operator it means one thing — attempted nothing, refused, said why.
#: The three are separate constants because they are separate vocabularies; a queue row has
#: no stage result, and a layer has no retry window.
WITHHELD = "WITHHELD"


def _stamp(moment: Optional[datetime] = None) -> str:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(stamp: Optional[str]) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _rows(connection: sqlite3.Connection, sql: str, parameters: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(sql, tuple(parameters))]


# --- jobs ------------------------------------------------------------------


def enqueue(
    database_path: Path,
    *,
    project_id: str,
    stage: str,
    kind: str,
    input_hash: Optional[str] = None,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[int]:
    """Queue one stage attempt. Returns ``None`` when the project already has one."""
    stamp = _stamp(now)
    with connect(database_path) as connection:
        try:
            cursor = connection.execute(
                "INSERT INTO jobs (project_id, stage, kind, status, input_hash, attempt, reason, created_at_utc, updated_at_utc)"
                " VALUES (?, ?, ?, 'QUEUED', ?, 0, ?, ?, ?)",
                (project_id, stage, kind, input_hash, reason, stamp, stamp),
            )
        except sqlite3.IntegrityError:
            return None
        return int(cursor.lastrowid)


def active_job(database_path: Path, project_id: str) -> Optional[Dict[str, Any]]:
    placeholders = ", ".join("?" * len(ACTIVE_STATUSES))
    with connect(database_path) as connection:
        rows = _rows(
            connection,
            "SELECT * FROM jobs WHERE project_id = ? AND status IN ({}) ORDER BY id LIMIT 1".format(placeholders),
            (project_id, *ACTIVE_STATUSES),
        )
    return rows[0] if rows else None


def get_job(database_path: Path, job_id: int) -> Optional[Dict[str, Any]]:
    with connect(database_path) as connection:
        rows = _rows(connection, "SELECT * FROM jobs WHERE id = ?", (job_id,))
    return rows[0] if rows else None


def all_jobs(database_path: Path) -> List[Dict[str, Any]]:
    with connect(database_path) as connection:
        return _rows(connection, "SELECT * FROM jobs ORDER BY id")


def due_jobs(database_path: Path, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    stamp = _stamp(now)
    with connect(database_path) as connection:
        return _rows(
            connection,
            "SELECT * FROM jobs WHERE status = 'QUEUED' AND (retry_after_utc IS NULL OR retry_after_utc <= ?) ORDER BY id",
            (stamp,),
        )


def claim(database_path: Path, job_id: int, *, owner: str, now: Optional[datetime] = None) -> bool:
    stamp = _stamp(now)
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE jobs SET status = 'CLAIMED', owner = ?, disposition = NULL, updated_at_utc = ?"
            " WHERE id = ? AND status = 'QUEUED'",
            (owner, stamp, job_id),
        )
        return cursor.rowcount == 1


def start(database_path: Path, job_id: int, *, now: Optional[datetime] = None) -> bool:
    stamp = _stamp(now)
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE jobs SET status = 'RUNNING', updated_at_utc = ? WHERE id = ? AND status = 'CLAIMED'",
            (stamp, job_id),
        )
        return cursor.rowcount == 1


def finish(database_path: Path, job_id: int, *, status: str = "DONE", reason: Optional[str] = None, now: Optional[datetime] = None) -> None:
    stamp = _stamp(now)
    with connect(database_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = ?, reason = ?, retry_after_utc = NULL, updated_at_utc = ? WHERE id = ?",
            (status, reason, stamp, job_id),
        )


def defer(
    database_path: Path,
    job_id: int,
    *,
    reason: str,
    retry_after: datetime,
    now: Optional[datetime] = None,
    disposition: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a job to the queue without spending an attempt — contention, not failure.

    ``disposition`` says *why* the row is queued when the status alone would not: ``None``
    is ordinary contention, ``WITHHELD`` is the daemon declining to start the stage at all.
    It is always written, never merged, so a park cannot leave its label on a row that has
    since been deferred for something else.
    """
    stamp = _stamp(now)
    with connect(database_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'QUEUED', owner = NULL, reason = ?, retry_after_utc = ?, disposition = ?,"
            " updated_at_utc = ? WHERE id = ?",
            (reason, _stamp(retry_after), disposition, stamp, job_id),
        )
    return get_job(database_path, job_id) or {}


def withhold(
    database_path: Path,
    job_id: int,
    *,
    reason: str,
    retry_after: datetime,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Park a stage the box cannot serve. No attempt spent, and it says so by name.

    The policy: an unreadable declared
    input is an environment-permission wall, not the job's failure, so spending one of the
    two attempts on it conflates the box with the work. ``FAILED`` stays reserved for genuine worker failures.

    The row goes back to the queue behind ``retry_after`` rather than to the ``WITHHELD``
    *status*, because that status is terminal by construction: ``is_terminally_failed``
    keys on it, and a job that must retry when the wall comes down cannot be terminal. The
    disposition carries the honesty; the status carries the schedule.
    """
    return defer(database_path, job_id, reason=reason, retry_after=retry_after, now=now, disposition=WITHHELD)


def fail_attempt(
    database_path: Path,
    job_id: int,
    *,
    reason: str,
    now: Optional[datetime] = None,
    max_attempts: int = 2,
    backoff_seconds: Sequence[int] = DEFAULT_BACKOFF_SECONDS,
    status: str = "FAILED",
) -> Dict[str, Any]:
    """Spend one attempt. At ``max_attempts`` the job goes terminal instead of looping."""
    moment = now or datetime.now(timezone.utc)
    current = get_job(database_path, job_id) or {}
    attempt = int(current.get("attempt", 0)) + 1
    terminal = attempt >= max_attempts
    if terminal:
        retry_after_utc = None
        next_status = status
    else:
        index = min(attempt - 1, len(backoff_seconds) - 1)
        seconds = backoff_seconds[index] if backoff_seconds else 0
        retry_after_utc = _stamp(datetime.fromtimestamp(moment.timestamp() + seconds, tz=timezone.utc))
        next_status = "QUEUED"
    with connect(database_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = ?, owner = NULL, attempt = ?, reason = ?, retry_after_utc = ?, updated_at_utc = ? WHERE id = ?",
            (next_status, attempt, reason, retry_after_utc, _stamp(moment), job_id),
        )
    row = get_job(database_path, job_id) or {}
    row["terminal"] = terminal
    return row


def is_terminally_failed(database_path: Path, *, project_id: str, stage: str, input_hash: Optional[str]) -> bool:
    with connect(database_path) as connection:
        rows = _rows(
            connection,
            "SELECT id FROM jobs WHERE project_id = ? AND stage = ? AND status IN ('FAILED', 'WITHHELD')"
            " AND ((input_hash IS NULL AND ? IS NULL) OR input_hash = ?) LIMIT 1",
            (project_id, stage, input_hash, input_hash),
        )
    return bool(rows)


def drop(database_path: Path, job_id: int) -> None:
    """Delete a job disk has made irrelevant. Its events survive, unattached."""
    with connect(database_path) as connection:
        connection.execute("UPDATE events SET job_id = NULL WHERE job_id = ?", (job_id,))
        connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def requeue_orphans(database_path: Path, *, owner: str, now: Optional[datetime] = None) -> List[int]:
    """A killed daemon leaves CLAIMED/RUNNING rows behind; the restart owns them again."""
    stamp = _stamp(now)
    reason = "requeued after a daemon restart; disk decides whether this stage still needs running"
    with connect(database_path) as connection:
        rows = _rows(connection, "SELECT id FROM jobs WHERE status IN ('CLAIMED', 'RUNNING')")
        connection.execute(
            "UPDATE jobs SET status = 'QUEUED', owner = ?, reason = ?, retry_after_utc = NULL, updated_at_utc = ?"
            " WHERE status IN ('CLAIMED', 'RUNNING')",
            (owner, reason, stamp),
        )
    return [int(row["id"]) for row in rows]


# --- events ----------------------------------------------------------------


def record_event(
    database_path: Path,
    *,
    kind: str,
    project_id: Optional[str] = None,
    job_id: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> int:
    """Append one event. Delegates to ``studio.events`` so there is one writer's rules.

    There were briefly two writers on this table with different rules: this one stringified
    anything and validated no ``kind``, while ``studio.events`` enforces a strict token.
    That difference is not cosmetic — ``kind`` is written unescaped into the SSE ``event:``
    field, so a kind carrying a newline could forge frames on the wire. Rather than keep a
    second, laxer door onto the same table, this delegates: same validation, same
    serialisation, same timestamp source, one place to change.

    Its timestamp comes from the event log's own clock rather than the daemon's injected
    one; nothing reads event times as a decision input, and a single source is worth more
    than a mockable one here.
    """
    return int(
        events.record_event(database_path, kind=kind, project_id=project_id, job_id=job_id, payload=payload)["id"]
    )


def events_since(database_path: Path, *, after_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
    with connect(database_path) as connection:
        rows = _rows(connection, "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))
    for row in rows:
        try:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        except ValueError:
            row["payload"] = {}
    return rows


def count_events(database_path: Path, *, kind: Optional[str] = None, project_id: Optional[str] = None) -> int:
    clauses: List[str] = []
    parameters: List[Any] = []
    if kind is not None:
        clauses.append("kind = ?")
        parameters.append(kind)
    if project_id is not None:
        clauses.append("project_id = ?")
        parameters.append(project_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect(database_path) as connection:
        cursor = connection.execute("SELECT COUNT(*) FROM events" + where, tuple(parameters))
        return int(cursor.fetchone()[0])


# --- schedules -------------------------------------------------------------


def schedule(
    database_path: Path,
    *,
    project_id: str,
    kind: str,
    due_at: datetime,
    payload: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> int:
    with connect(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO schedules (project_id, kind, due_at_utc, payload_json, created_at_utc) VALUES (?, ?, ?, ?, ?)",
            (project_id, kind, _stamp(due_at), json.dumps(payload or {}, sort_keys=True, default=str), _stamp(now)),
        )
        return int(cursor.lastrowid)


def due_schedules(database_path: Path, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    with connect(database_path) as connection:
        rows = _rows(
            connection,
            "SELECT * FROM schedules WHERE fired_at_utc IS NULL AND due_at_utc <= ? ORDER BY due_at_utc, id",
            (_stamp(now),),
        )
    for row in rows:
        try:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        except ValueError:
            row["payload"] = {}
    return rows


def mark_fired(database_path: Path, schedule_id: int, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Fire a wake and record how late it was. Late is fine; silent is not (§8.4)."""
    moment = now or datetime.now(timezone.utc)
    with connect(database_path) as connection:
        connection.execute("UPDATE schedules SET fired_at_utc = ? WHERE id = ?", (_stamp(moment), schedule_id))
        rows = _rows(connection, "SELECT * FROM schedules WHERE id = ?", (schedule_id,))
    row = rows[0] if rows else {}
    due = _parse(row.get("due_at_utc"))
    row["late_seconds"] = int((moment - due).total_seconds()) if due else 0
    try:
        row["payload"] = json.loads(row.pop("payload_json") or "{}")
    except ValueError:
        row["payload"] = {}
    return row


# Complete low-level queue write API. Direct imports cannot bypass Reel Pilot.
for _name in (
    "enqueue", "claim", "start", "finish", "defer", "withhold", "fail_attempt",
    "drop", "requeue_orphans", "record_event", "schedule", "mark_fired",
):
    globals()[_name] = guarded_mutation(f"studio jobs {_name}")(globals()[_name])
