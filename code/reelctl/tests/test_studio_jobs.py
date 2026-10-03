"""The job queue over ``studio.db``.

The queue is the only place the daemon is allowed to remember anything, and even here the
rows are disposable. What these tests pin is the anti-storm behaviour: one active job per
project, attempts that back off, and a terminal failure that does not re-enqueue itself
until the project's fingerprint moves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from reelctl.studio import jobs
from reelctl.studio.db import initialize

NOW = datetime(2025, 1, 14, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path) -> Path:
    return initialize(tmp_path / ".studio" / "studio.db")


# --- one job per project ---------------------------------------------------


def test_enqueue_returns_the_new_job_id(tmp_path: Path) -> None:
    database = _db(tmp_path)

    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)

    assert isinstance(job_id, int)
    assert jobs.active_job(database, "demo")["stage"] == "RENDERED"


def test_a_second_job_for_the_same_project_is_refused(tmp_path: Path) -> None:
    database = _db(tmp_path)
    jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)

    assert jobs.enqueue(database, project_id="demo", stage="TECHNICAL_QC", kind="deterministic", input_hash="f1", now=NOW) is None
    assert len(jobs.all_jobs(database)) == 1


def test_a_second_project_gets_its_own_job(tmp_path: Path) -> None:
    database = _db(tmp_path)
    jobs.enqueue(database, project_id="one", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)

    assert jobs.enqueue(database, project_id="two", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW) is not None


def test_finishing_a_job_frees_the_project_for_the_next_stage(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)
    jobs.finish(database, job_id, status="DONE", now=NOW)

    assert jobs.active_job(database, "demo") is None
    assert jobs.enqueue(database, project_id="demo", stage="TECHNICAL_QC", kind="deterministic", input_hash="f2", now=NOW) is not None


# --- claiming and running --------------------------------------------------


def test_a_job_moves_queued_to_claimed_to_running(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)

    assert jobs.claim(database, job_id, owner="studio-daemon", now=NOW) is True
    assert jobs.active_job(database, "demo")["status"] == "CLAIMED"
    assert jobs.active_job(database, "demo")["owner"] == "studio-daemon"

    jobs.start(database, job_id, now=NOW)
    assert jobs.active_job(database, "demo")["status"] == "RUNNING"


def test_a_job_already_claimed_cannot_be_claimed_again(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)
    jobs.claim(database, job_id, owner="studio-daemon", now=NOW)

    assert jobs.claim(database, job_id, owner="someone-else", now=NOW) is False


# --- backoff, attempts, and the stall rule ---------------------------------


def test_defer_keeps_the_attempt_count_and_hides_the_job_until_its_retry_time(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)
    jobs.claim(database, job_id, owner="studio-daemon", now=NOW)

    jobs.defer(database, job_id, reason="project is locked by another reelctl process", retry_after=NOW + timedelta(seconds=60), now=NOW)

    row = jobs.active_job(database, "demo")
    assert row["status"] == "QUEUED"
    assert row["attempt"] == 0
    assert "locked by another" in row["reason"]
    assert jobs.due_jobs(database, now=NOW) == []
    assert [item["id"] for item in jobs.due_jobs(database, now=NOW + timedelta(seconds=61))] == [job_id]


def test_a_failed_attempt_backs_off_and_the_second_one_is_terminal(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)

    first = jobs.fail_attempt(database, job_id, reason="ffmpeg exited 1", now=NOW, max_attempts=2, backoff_seconds=(30, 300))
    assert first["status"] == "QUEUED"
    assert first["attempt"] == 1
    assert first["terminal"] is False
    assert jobs.due_jobs(database, now=NOW) == []

    second = jobs.fail_attempt(database, job_id, reason="ffmpeg exited 1", now=NOW, max_attempts=2, backoff_seconds=(30, 300))
    assert second["status"] == "FAILED"
    assert second["attempt"] == 2
    assert second["terminal"] is True
    assert jobs.active_job(database, "demo") is None


def test_a_terminally_failed_fingerprint_is_not_re_enqueued(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)
    jobs.fail_attempt(database, job_id, reason="boom", now=NOW, max_attempts=1, backoff_seconds=(30,))

    assert jobs.is_terminally_failed(database, project_id="demo", stage="RENDERED", input_hash="f1") is True
    assert jobs.is_terminally_failed(database, project_id="demo", stage="RENDERED", input_hash="f2") is False


# --- the withheld park (v1.1) ----------------------------------------------
#
# An input the daemon cannot read is the box's failure, not the job's. Parking it must
# therefore look like contention — back to the queue, retry window, attempt untouched —
# while still *saying* WITHHELD, because "queued" alone would hide an environment wall the
# operator is the only one who can clear.


def test_withholding_a_job_returns_it_to_the_queue_without_spending_an_attempt(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)

    jobs.withhold(
        database,
        job_id,
        reason="the authorized footage root /Volumes/WORKDRIVE/workbench/footage-library is unreadable (EPERM)",
        retry_after=NOW + timedelta(minutes=30),
        now=NOW,
    )

    row = jobs.active_job(database, "demo")
    assert row["attempt"] == 0
    assert row["disposition"] == "WITHHELD"
    assert row["owner"] is None
    assert "footage-library" in row["reason"]


def test_a_withheld_job_is_not_due_until_its_window_passes(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)

    jobs.withhold(database, job_id, reason="unreadable input", retry_after=NOW + timedelta(minutes=30), now=NOW)

    assert jobs.due_jobs(database, now=NOW + timedelta(minutes=29)) == []
    assert [item["id"] for item in jobs.due_jobs(database, now=NOW + timedelta(minutes=31))] == [job_id]


def test_a_withheld_park_is_not_a_terminal_failure(tmp_path: Path) -> None:
    """The whole point: the stage is untried, so nothing may treat it as stalled."""
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)

    jobs.withhold(database, job_id, reason="unreadable input", retry_after=NOW + timedelta(minutes=30), now=NOW)

    assert jobs.is_terminally_failed(database, project_id="demo", stage="RENDERED", input_hash="f1") is False


def test_claiming_a_parked_job_clears_the_withheld_label(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)
    jobs.withhold(database, job_id, reason="unreadable input", retry_after=NOW + timedelta(minutes=30), now=NOW)

    jobs.claim(database, job_id, owner="studio-daemon", now=NOW + timedelta(minutes=31))

    assert jobs.active_job(database, "demo")["disposition"] is None


def test_deferring_for_contention_never_leaves_a_stale_withheld_label(tmp_path: Path) -> None:
    """A busy project is not a withheld one; a label that outlives its reason is a lie."""
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)
    jobs.withhold(database, job_id, reason="unreadable input", retry_after=NOW + timedelta(minutes=30), now=NOW)

    jobs.defer(database, job_id, reason="locked by another reelctl process", retry_after=NOW + timedelta(seconds=60), now=NOW)

    row = jobs.active_job(database, "demo")
    assert row["disposition"] is None
    assert "locked by another" in row["reason"]


def test_a_job_that_was_never_withheld_carries_no_disposition(tmp_path: Path) -> None:
    database = _db(tmp_path)
    jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)

    assert jobs.active_job(database, "demo")["disposition"] is None


def test_the_disposition_column_is_added_to_a_database_that_predates_it(tmp_path: Path) -> None:
    """The live queue is not thrown away for a schema change; it is migrated in place."""
    import sqlite3

    path = tmp_path / ".studio" / "studio.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.executescript(
        """
        CREATE TABLE jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id      TEXT NOT NULL,
            stage           TEXT NOT NULL,
            kind            TEXT NOT NULL,
            status          TEXT NOT NULL,
            input_hash      TEXT,
            attempt         INTEGER NOT NULL DEFAULT 0,
            owner           TEXT,
            reason          TEXT,
            retry_after_utc TEXT,
            created_at_utc  TEXT NOT NULL,
            updated_at_utc  TEXT NOT NULL
        );
        INSERT INTO jobs (project_id, stage, kind, status, created_at_utc, updated_at_utc)
        VALUES ('demo', 'RENDERED', 'deterministic', 'QUEUED', '2025-01-14T12:00:00Z', '2025-01-14T12:00:00Z');
        """
    )
    connection.commit()
    connection.close()

    initialize(path)

    row = jobs.active_job(path, "demo")
    assert row["disposition"] is None
    jobs.withhold(path, int(row["id"]), reason="unreadable input", retry_after=NOW + timedelta(minutes=30), now=NOW)
    assert jobs.active_job(path, "demo")["disposition"] == "WITHHELD"


# --- events ----------------------------------------------------------------


def test_events_are_append_only_and_readable_in_order(tmp_path: Path) -> None:
    database = _db(tmp_path)

    first = jobs.record_event(database, kind="tick", payload={"n": 1})
    jobs.record_event(database, kind="project_busy", project_id="demo", payload={"reason": "locked"})

    stream = jobs.events_since(database, after_id=0)
    assert [item["kind"] for item in stream] == ["tick", "project_busy"]
    assert stream[1]["payload"]["reason"] == "locked"
    assert [item["kind"] for item in jobs.events_since(database, after_id=first)] == ["project_busy"]


def test_counting_events_lets_a_caller_prove_something_was_emitted_once(tmp_path: Path) -> None:
    database = _db(tmp_path)
    jobs.record_event(database, kind="degraded", payload={"reasons": ["a"]})
    jobs.record_event(database, kind="degraded", payload={"reasons": ["a"]})

    assert jobs.count_events(database, kind="degraded") == 2
    assert jobs.count_events(database, kind="tick") == 0


# --- schedules -------------------------------------------------------------


def test_a_schedule_fires_once_and_records_how_late_it_was(tmp_path: Path) -> None:
    database = _db(tmp_path)
    due = NOW + timedelta(hours=24)
    jobs.schedule(database, project_id="demo", kind="metric_capture", due_at=due, payload={"snapshot": "24h"}, now=NOW)

    assert jobs.due_schedules(database, now=NOW) == []

    woke = NOW + timedelta(hours=30)
    pending = jobs.due_schedules(database, now=woke)
    assert len(pending) == 1
    assert pending[0]["payload"]["snapshot"] == "24h"

    fired = jobs.mark_fired(database, pending[0]["id"], now=woke)
    assert fired["late_seconds"] == 6 * 3600
    assert jobs.due_schedules(database, now=woke) == []


# --- resumability ----------------------------------------------------------


def test_orphaned_running_rows_from_a_killed_daemon_are_requeued(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)
    jobs.claim(database, job_id, owner="studio-daemon", now=NOW)
    jobs.start(database, job_id, now=NOW)

    requeued = jobs.requeue_orphans(database, owner="studio-daemon", now=NOW)

    assert requeued == [job_id]
    row = jobs.active_job(database, "demo")
    assert row["status"] == "QUEUED"
    assert "restart" in row["reason"]


def test_dropping_a_job_removes_it_entirely(tmp_path: Path) -> None:
    database = _db(tmp_path)
    job_id = jobs.enqueue(database, project_id="demo", stage="RENDERED", kind="deterministic", input_hash="f1", now=NOW)

    jobs.drop(database, job_id)

    assert jobs.active_job(database, "demo") is None
    assert jobs.all_jobs(database) == []
