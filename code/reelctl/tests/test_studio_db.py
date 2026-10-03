from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_web import request

from reelctl.studio.board import board_model
from reelctl.studio.db import SCHEMA_VERSION, TABLES, connect, initialize, rebuild_from_disk
from reelctl.web import create_app


def _projects_with(tmp_path: Path, *project_ids: str) -> Path:
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    footage = tmp_path / "footage"
    footage.mkdir(exist_ok=True)
    projects = tmp_path / "projects"
    app = create_app(projects_root=projects)
    for project_id in project_ids:
        created = request(
            app,
            "POST",
            "/api/projects",
            json_body={
                "project_id": project_id,
                "reference": str(reference),
                "footage": str(footage),
                "mode": "original-montage",
            },
        )
        assert created.status_code == 201
    return projects


def _db(tmp_path: Path) -> Path:
    return tmp_path / "studio" / "studio.db"


def _rows(database: Path, sql: str, *args: Any) -> list:
    with connect(database) as connection:
        return [tuple(row) for row in connection.execute(sql, args).fetchall()]


# --- schema ----------------------------------------------------------------


def test_schema_creates_exactly_the_declared_tables(tmp_path: Path) -> None:
    database = _db(tmp_path)

    initialize(database)

    names = {row[0] for row in _rows(database, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) == {"jobs", "events", "schedules", "status_cache"}
    assert set(TABLES) <= names
    assert _rows(database, "PRAGMA user_version")[0][0] == SCHEMA_VERSION


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    database = _db(tmp_path)

    initialize(database)
    initialize(database)

    assert _rows(database, "PRAGMA user_version")[0][0] == SCHEMA_VERSION


def test_at_most_one_active_job_per_project(tmp_path: Path) -> None:
    database = _db(tmp_path)
    initialize(database)
    insert = (
        "INSERT INTO jobs (project_id, stage, kind, status, created_at_utc, updated_at_utc) "
        "VALUES (?, ?, ?, ?, '2025-01-14T00:00:00Z', '2025-01-14T00:00:00Z')"
    )

    with connect(database) as connection:
        connection.execute(insert, ("demo", "RENDERED", "deterministic", "QUEUED"))
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, ("demo", "TECHNICAL_QC", "deterministic", "QUEUED"))

    with connect(database) as connection:
        connection.execute("UPDATE jobs SET status='DONE' WHERE project_id='demo'")
        connection.execute(insert, ("demo", "TECHNICAL_QC", "deterministic", "QUEUED"))
        connection.commit()

    assert len(_rows(database, "SELECT id FROM jobs WHERE project_id='demo'")) == 2


def test_a_job_kind_outside_the_three_is_refused(tmp_path: Path) -> None:
    database = _db(tmp_path)
    initialize(database)

    with connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs (project_id, stage, kind, status, created_at_utc, updated_at_utc) "
                "VALUES ('demo', 'RENDERED', 'publish', 'QUEUED', 'x', 'x')"
            )


def test_events_are_append_only_and_tail_in_insertion_order(tmp_path: Path) -> None:
    database = _db(tmp_path)
    initialize(database)

    with connect(database) as connection:
        for kind in ("stage", "job", "health"):
            connection.execute(
                "INSERT INTO events (created_at_utc, kind, project_id, payload_json) VALUES ('2025-01-14T00:00:00Z', ?, 'demo', '{}')",
                (kind,),
            )
        connection.commit()

    assert [row[1] for row in _rows(database, "SELECT id, kind FROM events ORDER BY id")] == ["stage", "job", "health"]


def test_schedules_can_be_queried_by_due_time(tmp_path: Path) -> None:
    database = _db(tmp_path)
    initialize(database)

    with connect(database) as connection:
        connection.executemany(
            "INSERT INTO schedules (project_id, kind, due_at_utc, created_at_utc) VALUES (?, ?, ?, '2025-01-14T00:00:00Z')",
            [("demo", "metric_capture_24h", "2025-01-15T00:00:00Z"), ("demo", "metric_capture_72h", "2025-01-17T00:00:00Z")],
        )
        connection.commit()

    due = _rows(database, "SELECT kind FROM schedules WHERE fired_at_utc IS NULL AND due_at_utc <= ? ORDER BY due_at_utc", "2025-01-16T00:00:00Z")
    assert [row[0] for row in due] == ["metric_capture_24h"]


# --- rebuild from disk -----------------------------------------------------


def test_rebuild_creates_the_database_and_caches_one_row_per_project(tmp_path: Path) -> None:
    projects = _projects_with(tmp_path, "alpha", "beta")
    database = _db(tmp_path)

    result = rebuild_from_disk(projects, database_path=database)

    assert result["status"] == "PASS"
    assert result["projects"] == 2
    assert database.is_file()
    cached = _rows(database, "SELECT project_id, headline FROM status_cache ORDER BY project_id")
    assert cached == [("alpha", "PENDING_MACHINE"), ("beta", "PENDING_MACHINE")]


def test_deleting_the_database_and_rebuilding_reproduces_identical_board_output(tmp_path: Path) -> None:
    projects = _projects_with(tmp_path, "alpha", "beta")
    database = _db(tmp_path)
    rebuild_from_disk(projects, database_path=database)
    before_board = board_model(projects)
    before_cache = _rows(database, "SELECT project_id, state_sha256, headline, layers_json FROM status_cache ORDER BY project_id")

    database.unlink()
    rebuild_from_disk(projects, database_path=database)

    after_board = board_model(projects)
    after_cache = _rows(database, "SELECT project_id, state_sha256, headline, layers_json FROM status_cache ORDER BY project_id")
    assert after_board == before_board
    assert after_cache == before_cache


def test_the_board_never_reads_the_status_cache(tmp_path: Path) -> None:
    projects = _projects_with(tmp_path, "alpha")
    database = _db(tmp_path)
    rebuild_from_disk(projects, database_path=database)
    honest = board_model(projects)

    with connect(database) as connection:
        connection.execute("UPDATE status_cache SET headline='PROVEN', layers_json='[]'")
        connection.commit()

    assert board_model(projects) == honest
    assert board_model(projects)["projects"][0]["headline"] == "PENDING_MACHINE"


def test_the_board_answers_with_no_database_at_all(tmp_path: Path) -> None:
    projects = _projects_with(tmp_path, "alpha")

    assert not _db(tmp_path).exists()
    assert [row["project_id"] for row in board_model(projects)["projects"]] == ["alpha"]


def test_rebuild_discards_cache_rows_for_projects_that_left_the_disk(tmp_path: Path) -> None:
    projects = _projects_with(tmp_path, "alpha")
    database = _db(tmp_path)
    rebuild_from_disk(projects, database_path=database)
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO status_cache (project_id, state_sha256, headline, layers_json, computed_at_utc) "
            "VALUES ('ghost', 'x', 'PROVEN', '[]', '2025-01-14T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO jobs (project_id, stage, kind, status, created_at_utc, updated_at_utc) "
            "VALUES ('ghost', 'RENDERED', 'deterministic', 'QUEUED', 'x', 'x')"
        )
        connection.commit()

    rebuild_from_disk(projects, database_path=database)

    assert [row[0] for row in _rows(database, "SELECT project_id FROM status_cache")] == ["alpha"]
    assert _rows(database, "SELECT project_id FROM jobs") == []


def test_discarding_an_orphan_job_keeps_its_events_and_drops_the_pointer(tmp_path: Path) -> None:
    # The event log is history and stays; the job row is disposable scheduling state.
    projects = _projects_with(tmp_path, "alpha")
    database = _db(tmp_path)
    initialize(database)
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO jobs (project_id, stage, kind, status, created_at_utc, updated_at_utc) "
            "VALUES ('ghost', 'REFERENCE_LOCKED', 'deterministic', 'QUEUED', 'x', 'x')"
        )
        job_id = connection.execute("SELECT id FROM jobs WHERE project_id='ghost'").fetchone()[0]
        connection.execute(
            "INSERT INTO events (created_at_utc, kind, project_id, job_id, payload_json) "
            "VALUES ('2025-01-14T00:00:00Z', 'job_enqueued', 'ghost', ?, '{}')",
            (job_id,),
        )

    rebuild_from_disk(projects, database_path=database)

    assert _rows(database, "SELECT project_id FROM jobs") == []
    assert _rows(database, "SELECT kind, job_id FROM events") == [("job_enqueued", None)]


def test_the_cache_is_keyed_by_the_state_json_hash(tmp_path: Path) -> None:
    projects = _projects_with(tmp_path, "alpha")
    database = _db(tmp_path)
    rebuild_from_disk(projects, database_path=database)
    first = _rows(database, "SELECT state_sha256 FROM status_cache WHERE project_id='alpha'")[0][0]

    state = projects / "alpha/state.json"
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["updated_at_utc"] = "2025-01-14T23:59:59Z"
    state.write_text(json.dumps(payload), encoding="utf-8")
    rebuild_from_disk(projects, database_path=database)

    assert _rows(database, "SELECT state_sha256 FROM status_cache WHERE project_id='alpha'")[0][0] != first


def test_the_cached_layers_are_the_board_layers(tmp_path: Path) -> None:
    projects = _projects_with(tmp_path, "alpha")
    database = _db(tmp_path)

    rebuild_from_disk(projects, database_path=database)

    cached = json.loads(_rows(database, "SELECT layers_json FROM status_cache WHERE project_id='alpha'")[0][0])
    assert cached == board_model(projects)["projects"][0]["layers"]
