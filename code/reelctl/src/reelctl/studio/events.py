"""The append-only event log and its server-sent-events wire format.

``studio.db``'s ``events`` table is the only thing the live UI tails. Writers — the web
app for the mutations it performs, the daemon for the stages it drives — append here and
never talk to connected browsers directly, so a reload replays exactly what a live
listener saw, and a listener that drops reconnects with ``Last-Event-ID`` and misses
nothing. The row id *is* the SSE event id; that equivalence is what makes resume work.

Two framing rules are load-bearing rather than cosmetic:

* an event ``kind`` is a strict token, because it is written unescaped into the SSE
  ``event:`` field — a kind carrying a newline could forge frames;
* the payload is emitted as one JSON line, because JSON escapes newlines and a raw
  newline inside ``data:`` would end the frame early.

Nothing here is an authority. The log records that something happened; whether a layer is
``PROVEN`` is still decided by re-verifying receipts at read time (``studio.status``).
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from ..errors import ReelctlError
from .db import connect, initialize

KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")

DEFAULT_LIMIT = 200
DEFAULT_POLL_SECONDS = 0.5
DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_STREAM_SECONDS = 300.0
MAX_STREAM_SECONDS = 3600.0

# Every frame carries this one name, and the row's real kind travels in the JSON payload.
#
# The first design named each frame after its kind and had the browser subscribe per name.
# EventSource has no wildcard listener, so that made the client's list a second place the
# truth lived — and it went stale immediately: all thirteen kinds the daemon emits were
# absent from it, so none of them ever reached the live stream and the board only appeared
# to update, on its fallback timer. A constant name has no list to go stale.
#
# It also removes the frame-forging vector by construction rather than by sanitising it:
# with the kind confined to JSON, a newline inside it cannot start an SSE field line.
STREAM_EVENT_NAME = "studio"


class EventLogError(ReelctlError):
    """The event log could not be read or written. Never fatal to the work itself."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_kind(kind: Any) -> str:
    if not isinstance(kind, str) or not KIND_PATTERN.match(kind):
        raise EventLogError(f"event kind must match {KIND_PATTERN.pattern} (it is written into the SSE event field); got {kind!r}")
    return kind


def _encode(payload: Optional[Mapping[str, Any]]) -> str:
    try:
        return json.dumps(dict(payload) if payload else {}, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise EventLogError(f"event payload is not JSON-serialisable: {exc}") from exc


def _decode(row: Sequence[Any]) -> Dict[str, Any]:
    identifier, created_at, kind, project_id, job_id, payload_json = row
    try:
        payload = json.loads(payload_json)
    except ValueError:
        payload = {"unparseable_payload": payload_json}
    return {
        "id": int(identifier),
        "created_at_utc": created_at,
        "kind": kind,
        "project_id": project_id,
        "job_id": job_id,
        "payload": payload,
    }


def _has_events_table(connection: Any) -> bool:
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'").fetchone())


def record_event(
    database_path: Path,
    *,
    kind: str,
    project_id: Optional[str] = None,
    job_id: Optional[int] = None,
    payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Append one event, creating the schema if it is not there yet.

    ``initialize`` runs unconditionally rather than only when the file is missing: a
    database another module opened without initialising is an *empty file*, not an empty
    schema, so a file-existence check would skip the schema creation and the insert would
    fail with "no such table". The DDL is ``IF NOT EXISTS`` and idempotent, and events are
    written once per stage advance, so the extra statement costs nothing that matters.
    """
    path = Path(database_path)
    _valid_kind(kind)
    encoded = _encode(payload)
    created_at = _now()
    try:
        initialize(path)
        with connect(path) as connection:
            cursor = connection.execute(
                "INSERT INTO events (created_at_utc, kind, project_id, job_id, payload_json) VALUES (?, ?, ?, ?, ?)",
                (created_at, kind, project_id, job_id, encoded),
            )
            identifier = int(cursor.lastrowid or 0)
    except (sqlite3.Error, OSError) as exc:
        raise EventLogError(f"event log at {path} is unusable: {exc}") from exc
    return {
        "id": identifier,
        "created_at_utc": created_at,
        "kind": kind,
        "project_id": project_id,
        "job_id": job_id,
        "payload": json.loads(encoded),
    }


def events_since(database_path: Path, after_id: int = 0, *, limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    path = Path(database_path)
    if not path.is_file():
        return []
    try:
        with connect(path) as connection:
            if not _has_events_table(connection):
                return []
            rows = connection.execute(
                "SELECT id, created_at_utc, kind, project_id, job_id, payload_json FROM events WHERE id > ? ORDER BY id LIMIT ?",
                (int(after_id), int(limit)),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        raise EventLogError(f"event log at {path} is unreadable: {exc}") from exc
    return [_decode(row) for row in rows]


def latest_event_id(database_path: Path) -> int:
    path = Path(database_path)
    if not path.is_file():
        return 0
    try:
        with connect(path) as connection:
            if not _has_events_table(connection):
                return 0
            value = connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    except (sqlite3.Error, OSError) as exc:
        raise EventLogError(f"event log at {path} is unreadable: {exc}") from exc
    return int(value)


# --- wire format -----------------------------------------------------------


def sse_frame(event: Mapping[str, Any]) -> str:
    """One SSE frame: the row id, the constant stream name, and the row as one JSON line.

    Nothing from the row reaches a field line, so no value in the database — however it
    got there — can forge a frame. ``json.dumps`` escapes newlines, which is the whole of
    the defence.
    """
    raw = event.get("kind")
    data = json.dumps(
        {
            "id": int(event["id"]),
            "created_at_utc": event.get("created_at_utc"),
            "kind": raw if isinstance(raw, str) else str(raw),
            "project_id": event.get("project_id"),
            "job_id": event.get("job_id"),
            "payload": event.get("payload") or {},
        },
        sort_keys=True,
    )
    return f"id: {int(event['id'])}\nevent: {STREAM_EVENT_NAME}\ndata: {data}\n\n"


def sse_comment(text: str) -> str:
    """A comment frame. Whitespace is flattened so the text cannot end the frame early."""
    return f": {' '.join(str(text).split())}\n\n"


async def sse_stream(
    database_path: Path,
    *,
    after_id: int = 0,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    max_seconds: Optional[float] = DEFAULT_STREAM_SECONDS,
    limit: int = DEFAULT_LIMIT,
    is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
) -> AsyncIterator[str]:
    """Tail the log as SSE frames.

    The window is bounded on purpose: EventSource reconnects on its own and sends
    ``Last-Event-ID``, so a closed window costs one reconnect and buys a connection that
    cannot leak for the lifetime of the app. The reads are synchronous sqlite calls —
    sub-millisecond against a local WAL database, and this app serves one operator.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    cursor = max(int(after_id), 0)
    yield sse_comment(f"reel studio event stream, tailing after {cursor}")
    last_beat = loop.time()
    while True:
        if is_disconnected is not None and await is_disconnected():
            yield sse_comment("client disconnected")
            return
        try:
            batch = events_since(database_path, cursor, limit=limit)
        except EventLogError as exc:
            yield sse_comment(f"event log unreadable, closing stream: {exc}")
            return
        for event in batch:
            cursor = int(event["id"])
            yield sse_frame(event)
        now = loop.time()
        if batch:
            last_beat = now
        elif now - last_beat >= heartbeat_seconds:
            last_beat = now
            yield sse_comment(f"heartbeat {_now()}")
        if max_seconds is not None and loop.time() - started >= max_seconds:
            yield sse_comment(f"window of {max_seconds}s elapsed; reconnect with Last-Event-ID {cursor}")
            return
        await asyncio.sleep(poll_seconds)
