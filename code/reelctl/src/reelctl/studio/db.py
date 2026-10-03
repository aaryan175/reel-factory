"""``studio.db`` — machine scheduling state only, never an authority.

The split of authority is the point. ``REEL_REGISTRY.json``, each
project's ``state.json`` and ``_receipts/**`` remain the only truth about creative status.
This database holds what a JSON file is bad at: a concurrent-safe job queue, due-time
scheduling, an append-only event log for the SSE stream, and a status cache.

Consequences that hold by construction:

* the file is deletable at any time — ``rebuild_from_disk`` reconstructs every row;
* the board never reads it (``studio.board`` imports nothing from here), so a stale or
  poisoned ``status_cache`` row cannot turn a layer green in the UI.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

from ..hashing import sha256_file
from ..paths import canonical_root
from .board import board_model

SCHEMA_VERSION = 2
TABLES: Tuple[str, ...] = ("jobs", "events", "schedules", "status_cache")

#: Columns added after v1. ``initialize`` adds any that a live database is missing rather
#: than dropping the queue for a schema change: the rows are disposable in principle, but
#: throwing away a parked job to gain a column would be a self-inflicted outage.
MIGRATIONS: Tuple[Tuple[str, str, str], ...] = (("jobs", "disposition", "TEXT"),)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL,
    stage           TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('deterministic', 'judgment', 'capture')),
    status          TEXT NOT NULL CHECK (status IN ('QUEUED', 'CLAIMED', 'RUNNING', 'DONE', 'FAILED', 'WITHHELD')),
    input_hash      TEXT,
    attempt         INTEGER NOT NULL DEFAULT 0,
    owner           TEXT,
    reason          TEXT,
    retry_after_utc TEXT,
    -- Why a queued row is waiting, when ``status`` alone would not say. ``WITHHELD`` means
    -- the daemon declined to start the stage because the box could not serve it (an
    -- unreadable declared input), so the row is queued behind its retry window with no
    -- attempt spent. Every other transition clears it — a label that outlives its reason
    -- is worse than no label.
    disposition     TEXT,
    created_at_utc  TEXT NOT NULL,
    updated_at_utc  TEXT NOT NULL
);

-- §6.1 schedules at most one job per project; the schema enforces it rather than trusting
-- the daemon to remember.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_project
    ON jobs (project_id) WHERE status IN ('QUEUED', 'CLAIMED', 'RUNNING');
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status, retry_after_utc);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at_utc TEXT NOT NULL,
    kind           TEXT NOT NULL,
    project_id     TEXT,
    -- The log is append-only history and outlives the queue: discarding an orphaned job
    -- (§6.4) must drop the pointer, never the event.
    job_id         INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    payload_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_by_project ON events (project_id, id);

CREATE TABLE IF NOT EXISTS schedules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    due_at_utc     TEXT NOT NULL,
    payload_json   TEXT NOT NULL DEFAULT '{}',
    fired_at_utc   TEXT,
    created_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules (due_at_utc) WHERE fired_at_utc IS NULL;

CREATE TABLE IF NOT EXISTS status_cache (
    project_id      TEXT PRIMARY KEY,
    state_sha256    TEXT NOT NULL,
    headline        TEXT NOT NULL,
    layers_json     TEXT NOT NULL,
    computed_at_utc TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(str(Path(database_path)))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        connection.commit()
    finally:
        connection.close()


def _migrate(connection: sqlite3.Connection) -> None:
    """Add columns a database created by an older build does not have yet.

    ``CREATE TABLE IF NOT EXISTS`` is silent about a table that exists with the wrong shape,
    so a new column has to be added explicitly. Each addition is nullable with no default,
    which SQLite applies without rewriting the table.
    """
    for table, column, declaration in MIGRATIONS:
        present = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if not present or column in present:
            continue
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def initialize(database_path: Path) -> Path:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with connect(path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        _migrate(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return path


def rebuild_from_disk(projects_root: Path, *, database_path: Path) -> Dict[str, Any]:
    """Rebuild every derived row from disk, discarding anything disk disagrees with."""
    root = canonical_root(projects_root, create=True)
    path = initialize(database_path)
    board = board_model(root)
    known = [row["project_id"] for row in board["projects"]]
    computed_at = _now()
    with connect(path) as connection:
        connection.execute("DELETE FROM status_cache")
        for row in board["projects"]:
            state_path = root / row["project_id"] / "state.json"
            connection.execute(
                "INSERT INTO status_cache (project_id, state_sha256, headline, layers_json, computed_at_utc) VALUES (?, ?, ?, ?, ?)",
                (
                    row["project_id"],
                    sha256_file(state_path) if state_path.is_file() else "",
                    row["headline"],
                    json.dumps(row["layers"], sort_keys=True),
                    computed_at,
                ),
            )
        if known:
            placeholders = ", ".join("?" * len(known))
            connection.execute(f"DELETE FROM jobs WHERE project_id NOT IN ({placeholders})", known)
            connection.execute(f"DELETE FROM schedules WHERE project_id NOT IN ({placeholders})", known)
        else:
            connection.execute("DELETE FROM jobs")
            connection.execute("DELETE FROM schedules")
    return {"status": "PASS", "projects": len(known), "database": str(path)}
