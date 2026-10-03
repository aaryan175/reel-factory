from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from reelctl.errors import ReelctlError
from reelctl.studio import events as ev
from reelctl.studio.db import initialize


def _drain(database: Path, **kwargs: Any) -> List[str]:
    """Run the SSE generator to completion and return its chunks."""

    async def scenario() -> List[str]:
        chunks: List[str] = []
        stream = ev.sse_stream(database, **kwargs)
        try:
            async for chunk in stream:
                chunks.append(chunk)
        finally:
            await stream.aclose()
        return chunks

    return asyncio.run(scenario())


def _frames(chunks: List[str]) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                payloads.append(json.loads(line[len("data: ") :]))
    return payloads


# --- the append-only log ---------------------------------------------------


def test_recording_an_event_returns_a_row_id_and_a_utc_stamp(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"

    recorded = ev.record_event(database, kind="stage", project_id="demo", payload={"stage": "RENDERED"})

    assert recorded["id"] == 1
    assert recorded["kind"] == "stage"
    assert recorded["project_id"] == "demo"
    assert recorded["payload"] == {"stage": "RENDERED"}
    assert recorded["created_at_utc"].endswith("Z")


def test_recording_creates_the_database_when_it_is_absent(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "studio.db"
    assert not database.exists()

    ev.record_event(database, kind="intake", project_id="demo")

    assert database.is_file()
    assert [event["kind"] for event in ev.events_since(database)] == ["intake"]


def test_event_ids_are_monotonic_and_events_since_returns_only_newer_rows(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    first = ev.record_event(database, kind="one")
    second = ev.record_event(database, kind="two")
    third = ev.record_event(database, kind="three")

    assert [first["id"], second["id"], third["id"]] == [1, 2, 3]
    assert [event["kind"] for event in ev.events_since(database)] == ["one", "two", "three"]
    assert [event["kind"] for event in ev.events_since(database, after_id=1)] == ["two", "three"]
    assert ev.events_since(database, after_id=3) == []


def test_events_since_respects_its_limit_and_stays_in_order(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    for index in range(10):
        ev.record_event(database, kind="tick", payload={"index": index})

    page = ev.events_since(database, after_id=2, limit=3)

    assert [event["id"] for event in page] == [3, 4, 5]


def test_latest_event_id_is_zero_on_an_empty_log(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    initialize(database)

    assert ev.latest_event_id(database) == 0

    ev.record_event(database, kind="stage")
    assert ev.latest_event_id(database) == 1


def test_latest_event_id_of_a_database_that_does_not_exist_is_zero(tmp_path: Path) -> None:
    assert ev.latest_event_id(tmp_path / "absent.db") == 0
    assert ev.events_since(tmp_path / "absent.db") == []


def test_a_payload_round_trips_through_json(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    payload = {"stage": "VISUAL_QC", "reason": "candidate collapses at 00:02", "counts": [1, 2, 3]}

    ev.record_event(database, kind="stage", project_id="demo", payload=payload)

    assert ev.events_since(database)[0]["payload"] == payload


def test_an_unusable_payload_is_refused_rather_than_stored_lossily(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"

    with pytest.raises(ReelctlError):
        ev.record_event(database, kind="stage", payload={"path": Path("/tmp/x")})


# --- the SSE wire format ---------------------------------------------------


def test_a_frame_carries_the_row_id_the_kind_and_one_data_line(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    recorded = ev.record_event(database, kind="stage", project_id="demo", payload={"stage": "RENDERED"})

    frame = ev.sse_frame(recorded)

    assert frame.startswith(f"id: 1\nevent: {ev.STREAM_EVENT_NAME}\ndata: ")
    assert frame.endswith("\n\n")
    assert len([line for line in frame.splitlines() if line.startswith("data: ")]) == 1
    assert json.loads(frame.splitlines()[2][len("data: ") :])["project_id"] == "demo"


def test_a_reason_containing_newlines_cannot_forge_extra_frames(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    recorded = ev.record_event(
        database,
        kind="stage",
        project_id="demo",
        payload={"reason": "line one\nid: 99\nevent: forged\ndata: {}\n\n"},
    )

    frame = ev.sse_frame(recorded)

    # The forged text survives inside the JSON string — that is fine and lossless. What
    # must not happen is it starting a line, which is the only way SSE reads a field.
    lines = frame.splitlines()
    assert frame.count("\n\n") == 1
    assert [line for line in lines if line.startswith("event:")] == [f"event: {ev.STREAM_EVENT_NAME}"]
    assert [line for line in lines if line.startswith("id:")] == ["id: 1"]
    assert [line for line in lines if line.startswith("data:")] == [lines[2]]
    assert json.loads(lines[2][len("data: ") :])["payload"]["reason"].startswith("line one\n")


def test_an_event_kind_may_not_smuggle_a_newline_into_the_event_field(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"

    for bad in ["stage\nevent: forged", "", "   ", "Stage Advance", "a" * 41]:
        with pytest.raises(ReelctlError):
            ev.record_event(database, kind=bad)


def test_a_row_this_module_did_not_write_cannot_forge_a_frame(tmp_path: Path) -> None:
    # The row is inserted straight into the table rather than through a writer, because
    # that is the only thing the reader can actually assume: a row predating validation, a
    # future writer, or a hand-edited database. `studio.jobs.record_event` used to be an
    # unvalidated second writer and was the original motivation here; it now delegates to
    # `record_event`, so the reader's defence is tested against raw SQL instead of against
    # a writer that happens to be well-behaved today.
    from reelctl.studio.db import connect

    database = tmp_path / "studio.db"
    initialize(database)
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO events (created_at_utc, kind, project_id, payload_json) VALUES (?, ?, ?, ?)",
            ("2025-01-14T00:00:00Z", "stage\nevent: forged\ndata: {}\n", "demo", "{}"),
        )

    frame = ev.sse_frame(ev.events_since(database)[0])

    assert [line for line in frame.splitlines() if line.startswith("event:")] == [f"event: {ev.STREAM_EVENT_NAME}"]
    assert frame.count("\n\n") == 1
    assert json.loads(frame.splitlines()[2][len("data: ") :])["kind"].startswith("stage\n")


def test_a_kind_this_build_never_heard_of_still_reaches_the_stream(tmp_path: Path) -> None:
    # The original design named each frame after its kind, and the browser subscribed by
    # name — so every one of the daemon's thirteen kinds silently missed the live stream
    # and the board only appeared to update, on a fallback timer. The frame name is now a
    # constant, so no client-side list can go stale against a new writer.
    database = tmp_path / "studio.db"
    for kind in ("judgment_parked", "stage_settled", "tick_error", "intake"):
        ev.record_event(database, kind=kind, project_id="demo")

    chunks = _drain(database, after_id=0, poll_seconds=0.01, max_seconds=0.05, heartbeat_seconds=99)

    assert [frame["kind"] for frame in _frames(chunks)] == ["judgment_parked", "stage_settled", "tick_error", "intake"]
    # The reachability half: a browser bound to the constant name receives all four, and
    # asserting only on the payload would pass even with the broken per-kind naming.
    delivered = [chunk.splitlines()[1] for chunk in chunks if chunk.startswith("id:")]
    assert delivered == [f"event: {ev.STREAM_EVENT_NAME}"] * 4


def test_every_frame_carries_the_same_constant_event_name(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    ev.record_event(database, kind="job_deferred", project_id="demo")
    ev.record_event(database, kind="stage", project_id="demo")

    names = [ev.sse_frame(event).splitlines()[1] for event in ev.events_since(database)]

    assert names == [f"event: {ev.STREAM_EVENT_NAME}"] * 2
    assert ev.STREAM_EVENT_NAME.isidentifier()


def test_a_comment_frame_is_a_colon_line(tmp_path: Path) -> None:
    assert ev.sse_comment("heartbeat") == ": heartbeat\n\n"
    assert ev.sse_comment("two\nlines") == ": two lines\n\n"


# --- the stream ------------------------------------------------------------


def test_the_stream_replays_the_backlog_after_the_given_id(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    ev.record_event(database, kind="one")
    ev.record_event(database, kind="two")
    ev.record_event(database, kind="three")

    chunks = _drain(database, after_id=1, poll_seconds=0.01, max_seconds=0.05, heartbeat_seconds=99)

    assert [frame["kind"] for frame in _frames(chunks)] == ["two", "three"]


def test_the_stream_opens_with_a_comment_naming_where_it_is_tailing_from(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    ev.record_event(database, kind="one")

    chunks = _drain(database, after_id=1, poll_seconds=0.01, max_seconds=0.05, heartbeat_seconds=99)

    assert chunks[0].startswith(": ")
    assert "after 1" in chunks[0]


def test_the_stream_delivers_an_event_recorded_while_it_is_open(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    ev.record_event(database, kind="one")

    async def scenario() -> List[str]:
        chunks: List[str] = []

        async def writer() -> None:
            await asyncio.sleep(0.05)
            ev.record_event(database, kind="stage", project_id="demo", payload={"stage": "REFERENCE_LOCKED"})

        task = asyncio.ensure_future(writer())
        stream = ev.sse_stream(database, after_id=1, poll_seconds=0.01, max_seconds=5.0, heartbeat_seconds=99)
        try:
            async for chunk in stream:
                chunks.append(chunk)
                if '"kind": "stage"' in chunk:
                    break
        finally:
            await stream.aclose()
            await task
        return chunks

    chunks = asyncio.run(scenario())

    assert _frames(chunks)[-1]["payload"]["stage"] == "REFERENCE_LOCKED"


def test_the_stream_heartbeats_while_nothing_happens(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"

    chunks = _drain(database, after_id=0, poll_seconds=0.01, max_seconds=0.2, heartbeat_seconds=0.02)

    comments = [chunk for chunk in chunks if chunk.startswith(": ")]
    assert len(comments) >= 2
    assert any("heartbeat" in chunk for chunk in comments)


def test_the_stream_stops_at_max_seconds(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"

    async def scenario() -> float:
        started = time.monotonic()
        stream = ev.sse_stream(database, poll_seconds=0.01, max_seconds=0.15, heartbeat_seconds=99)
        try:
            async for _ in stream:
                pass
        finally:
            await stream.aclose()
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())

    assert 0.1 <= elapsed < 2.0


def test_the_stream_stops_when_the_client_has_gone(tmp_path: Path) -> None:
    database = tmp_path / "studio.db"
    ev.record_event(database, kind="one")

    async def scenario() -> List[str]:
        async def gone() -> bool:
            return True

        chunks: List[str] = []
        stream = ev.sse_stream(database, poll_seconds=0.01, max_seconds=30.0, heartbeat_seconds=99, is_disconnected=gone)
        try:
            async for chunk in stream:
                chunks.append(chunk)
        finally:
            await stream.aclose()
        return chunks

    chunks = asyncio.run(scenario())

    assert len(chunks) < 5
