from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from httpx import ASGITransport, AsyncClient, Response
from test_web import make_reference

from reelctl.locks import project_lock
from reelctl.state import ProjectState
from reelctl.studio import events as ev
from reelctl.studio import jobs, views
from reelctl.studio.config import StudioConfig
from reelctl.studio.db import initialize as initialize_database
from reelctl.studio.status import PENDING_MACHINE, PROVEN, STATUS_VALUES, WITHHELD, headline_status
from reelctl.web import create_app

NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def request(
    app: Any,
    method: str,
    path: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Response:
    """Same shape as ``test_web.request``, with headers so ``Last-Event-ID`` is reachable."""

    async def call() -> Response:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://reel-studio.local") as client:
            return await client.request(method, path, json=json_body, headers=headers)

    return asyncio.run(call())


def studio_config(tmp_path: Path) -> StudioConfig:
    return StudioConfig.from_env(
        {
            "REEL_STUDIO_PROJECTS_ROOT": str(tmp_path / "projects"),
            "REEL_STUDIO_DIR": str(tmp_path / "studio"),
            "REEL_STUDIO_STORAGE_ROOT": str(tmp_path / "storage"),
        }
    )


def studio_app(tmp_path: Path, *, project_id: str = "demo", real_reference: bool = False) -> Tuple[Any, StudioConfig]:
    config = studio_config(tmp_path)
    reference = tmp_path / "reference.mp4"
    if real_reference:
        make_reference(reference)
    else:
        reference.write_bytes(b"reference")
    footage = tmp_path / "footage"
    footage.mkdir(exist_ok=True)
    app = create_app(config=config)
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
    return app, config


def block_stage(config: StudioConfig, project_id: str, stage: str, reason: str) -> None:
    state = ProjectState.load(config.projects_root / project_id / "state.json")
    state.block(stage, f"{stage}-input-hash", reason)


def cell_html(page: str, project_id: str, layer_key: str) -> str:
    pattern = re.compile(
        rf'<td[^>]*data-project="{re.escape(project_id)}"[^>]*data-layer="{re.escape(layer_key)}".*?</td>',
        re.DOTALL,
    )
    match = pattern.search(page)
    assert match, f"no cell for {project_id}/{layer_key}"
    return match.group(0)


def row_header_html(page: str, project_id: str) -> str:
    pattern = re.compile(
        rf'<tr class="reel" data-project="{re.escape(project_id)}".*?<th class="reel-head" scope="row">(.*?)</th>',
        re.DOTALL,
    )
    match = pattern.search(page)
    assert match, f"no row header for {project_id}"
    return match.group(1)


def park_job(config: StudioConfig, project_id: str, *, stage: str, reason: str) -> int:
    """Put a project's queued job into the state ``daemon._withhold`` leaves it in."""
    initialize_database(config.database_path)
    job_id = jobs.enqueue(
        config.database_path, project_id=project_id, stage=stage, kind="judgment", input_hash="f1", now=NOW
    )
    jobs.withhold(config.database_path, int(job_id), reason=reason, retry_after=NOW + timedelta(minutes=30), now=NOW)
    return int(job_id)


# --- a withheld park is visible, and is not mistaken for progress ----------
#
# The park keeps the row QUEUED behind a retry window, which is true and useless on its
# own: a board can say PENDING_MACHINE while the reel is going nowhere. What the daemon declined to start, and why, has to be on the screen.


def test_a_withheld_park_is_shown_as_withheld_not_as_a_queued_job(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    reason = "BLUEPRINT_LOCKED was not started: the footage root is unreadable (EPERM). See CALL-TCC-WORKDRIVE-VOLUME."
    park_job(config, "demo", stage="BLUEPRINT_LOCKED", reason=reason)

    header = row_header_html(request(app, "GET", "/board").text, "demo")

    assert "WITHHELD BLUEPRINT_LOCKED (judgment)" in header
    assert reason in header
    assert "retry after 2025-01-15T12:30:00Z" in header


def test_the_api_carries_the_park_so_a_client_need_not_parse_the_page(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    park_job(config, "demo", stage="BLUEPRINT_LOCKED", reason="the footage root is unreadable (EPERM)")

    row = request(app, "GET", "/api/reels").json()["projects"][0]

    assert row["job"]["disposition"] == "WITHHELD"
    assert row["job"]["status"] == "QUEUED"
    assert row["job"]["retry_after_utc"] == "2025-01-15T12:30:00Z"


def test_an_ordinary_queued_job_is_still_shown_by_its_status(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    initialize_database(config.database_path)
    jobs.enqueue(config.database_path, project_id="demo", stage="REFERENCE_LOCKED", kind="deterministic", now=NOW)

    header = row_header_html(request(app, "GET", "/board").text, "demo")

    assert "QUEUED REFERENCE_LOCKED (deterministic)" in header
    assert "WITHHELD" not in header


def test_a_park_does_not_repaint_a_layer_the_machine_never_attempted(tmp_path: Path) -> None:
    """Queue state and disk evidence stay separate authorities: no worker ran, no verdict."""
    app, config = studio_app(tmp_path)
    park_job(config, "demo", stage="BLUEPRINT_LOCKED", reason="the footage root is unreadable (EPERM)")

    row = request(app, "GET", "/api/reels").json()["projects"][0]

    assert row["layers"][1]["stage"] == "BLUEPRINT_LOCKED"
    assert row["layers"][1]["status"] == PENDING_MACHINE
    assert row["headline"] == PENDING_MACHINE


# --- the honesty vocabulary survives rendering -----------------------------


def test_every_status_value_maps_to_exactly_one_distinct_class() -> None:
    assert set(views.STATUS_CLASS) == set(STATUS_VALUES)
    assert len(set(views.STATUS_CLASS.values())) == len(STATUS_VALUES)


def test_only_proven_is_painted_with_the_success_colour(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    page = request(app, "GET", "/board").text

    green = views.STATUS_CLASS[PROVEN]
    for value, klass in views.STATUS_CLASS.items():
        rule = re.search(rf"\.chip\.{re.escape(klass)}\s*\{{([^}}]*)\}}", page)
        assert rule, f"no CSS rule for {value}"
        assert ("--proven" in rule.group(1)) is (klass == green), f"{value} paints itself with the proven token"


def test_a_withheld_cell_renders_the_engine_reason_verbatim(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    reason = "no candidate performs the reference role for block 03"
    block_stage(config, "demo", "REFERENCE_LOCKED", reason)

    page = request(app, "GET", "/board").text
    cell = cell_html(page, "demo", "reference_lock")

    assert reason in cell
    assert WITHHELD in cell


def test_a_blocked_stage_never_renders_the_proven_class(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    block_stage(config, "demo", "REFERENCE_LOCKED", "the reference download is unverifiable")

    page = request(app, "GET", "/board").text
    cell = cell_html(page, "demo", "reference_lock")

    assert views.STATUS_CLASS[WITHHELD] in cell
    assert views.STATUS_CLASS[PROVEN] not in cell
    assert PROVEN not in cell


def test_a_reason_containing_markup_is_escaped_not_executed(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    block_stage(config, "demo", "REFERENCE_LOCKED", "<script>alert('x')</script> unverified")

    page = request(app, "GET", "/board").text

    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; unverified" in page


def test_the_row_headline_is_the_weakest_layer(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    block_stage(config, "demo", "REFERENCE_LOCKED", "blocked on purpose")

    payload = request(app, "GET", "/api/reels").json()
    row = payload["projects"][0]
    page = request(app, "GET", "/board").text

    assert row["headline"] == headline_status(row["layers"])
    assert f'<tr class="reel" data-project="demo" data-headline="{row["headline"]}"' in page


def test_the_board_renders_one_row_per_project_and_fourteen_layer_columns(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    reference = tmp_path / "reference.mp4"
    request(
        app,
        "POST",
        "/api/projects",
        json_body={
            "project_id": "second",
            "reference": str(reference),
            "footage": str(tmp_path / "footage"),
            "mode": "reference-locked",
        },
    )

    page = request(app, "GET", "/board").text

    assert len(re.findall(r'<tr[^>]*data-project="demo"', page)) == 1
    assert len(re.findall(r'<tr[^>]*data-project="second"', page)) == 1
    assert len(re.findall(r'<td[^>]*data-project="demo"[^>]*data-layer=', page)) == 14
    assert len(re.findall(r"<th[^>]*data-layer=", page)) == 14


def test_a_cell_carries_its_receipt_path_and_hash_when_one_exists(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path, real_reference=True)
    assert request(app, "POST", "/api/projects/demo/actions", json_body={"action": "reference_analyze"}).status_code == 200

    layers = request(app, "GET", "/api/reels/demo/layers").json()["layers"]
    reference_layer = next(layer for layer in layers if layer["key"] == "reference_lock")
    page = request(app, "GET", "/board").text
    cell = cell_html(page, "demo", "reference_lock")

    assert reference_layer["status"] == PROVEN
    assert reference_layer["receipt_sha256"][:16] in cell
    assert "/api/reels/demo/receipt?stage=REFERENCE_LOCKED" in cell


# --- health strip and registry join ----------------------------------------


def test_the_health_strip_shows_degraded_reasons_as_text(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)

    page = request(app, "GET", "/board").text

    assert "DEGRADED" in page
    assert f"storage root {config.storage_root} is not mounted" in page


def test_the_board_names_the_lane_from_the_registry_and_never_invents_one(tmp_path: Path) -> None:
    # A project id is the lowercased reference shortcode: REF-06 → reel-ref-06-v1.
    app, config = studio_app(tmp_path, project_id="reel-ref-06-v1")
    (config.projects_root / "REEL_REGISTRY.json").write_text(
        json.dumps(
            {
                "schema_version": 2.0,
                "reels": [{"reference_shortcode": "REF-06", "reference_url": "https://example.invalid/reel/REF-06/"}],
                "lanes": {"CRAFT": {"quota": "one active project at a time", "active": "REF-06 (v10-r5 source-contours)"}},
                "open_calls": [],
            }
        ),
        encoding="utf-8",
    )

    payload = request(app, "GET", "/api/reels").json()
    row = payload["projects"][0]

    assert payload["registry"]["status"] == "PASS"
    assert row["lane"] == "CRAFT"
    assert "REF-06" in row["lane_source"]


def test_a_pending_mode_call_in_the_lane_string_is_not_normalised_away(tmp_path: Path) -> None:
    # A registry entry can carry the whole sentence as its lane. Showing a
    # bare "VOLUME" would delete an open question the registry recorded (§5.1).
    declared = "VOLUME (mode call pending; 1:1 captions requested — may become CRAFT)"
    app, config = studio_app(tmp_path, project_id="reel-ref-28-v1")
    Path(config.registry_path).write_text(
        json.dumps(
            {
                "schema_version": 2.0,
                "reels": [{"reference_shortcode": "REF-28", "lane": declared, "project_root": "reel-ref-28-v1/"}],
                "lanes": {},
                "open_calls": [],
            }
        ),
        encoding="utf-8",
    )

    row = request(app, "GET", "/api/reels").json()["projects"][0]
    page = request(app, "GET", "/board").text

    assert row["lane"] == "VOLUME"
    assert row["lane_source"] == f"registry reel entry lane: {declared}"
    assert "may become CRAFT" in page


def test_a_project_with_no_declared_lane_reads_as_undeclared_not_as_volume(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)

    row = request(app, "GET", "/api/reels").json()["projects"][0]
    page = request(app, "GET", "/board").text

    assert row["lane"] is None
    assert row["lane_source"] is None
    assert "not declared" in page


def test_a_registry_that_does_not_parse_surfaces_the_error_without_blanking_the_board(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    (config.projects_root / "REEL_REGISTRY.json").write_text("{not json", encoding="utf-8")

    payload = request(app, "GET", "/api/reels").json()
    page = request(app, "GET", "/board").text

    assert payload["registry"]["status"] == "FAIL"
    assert payload["registry"]["error"]
    assert [row["project_id"] for row in payload["projects"]] == ["demo"]
    assert "REEL_REGISTRY.json" in page


def test_an_absent_registry_is_reported_as_absent_not_as_a_failure(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)

    assert request(app, "GET", "/api/reels").json()["registry"]["status"] == "ABSENT"


# --- the read path stays lock-free -----------------------------------------


def test_the_board_answers_while_a_render_holds_the_project_lock(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)

    with project_lock(config.projects_root, "demo"):
        reels = request(app, "GET", "/api/reels")
        layers = request(app, "GET", "/api/reels/demo/layers")
        page = request(app, "GET", "/board")

    assert reels.status_code == 200
    assert layers.status_code == 200
    assert len(layers.json()["layers"]) == 14
    assert page.status_code == 200


# --- the receipt reader ----------------------------------------------------


def test_the_receipt_endpoint_reads_by_stage_name_not_by_caller_supplied_path(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path, real_reference=True)
    assert request(app, "POST", "/api/projects/demo/actions", json_body={"action": "reference_analyze"}).status_code == 200

    response = request(app, "GET", "/api/reels/demo/receipt?stage=REFERENCE_LOCKED")
    payload = response.json()

    assert response.status_code == 200
    assert payload["stage"] == "REFERENCE_LOCKED"
    assert payload["verified"] is True
    assert payload["receipt"]["stage"] == "REFERENCE_LOCKED"
    assert payload["sha256"] == payload["state_sha256"]


def test_the_receipt_endpoint_refuses_an_unknown_stage(tmp_path: Path) -> None:
    app, _ = studio_app(tmp_path)

    assert request(app, "GET", "/api/reels/demo/receipt?stage=../../etc/passwd").status_code == 400
    assert request(app, "GET", "/api/reels/demo/receipt?stage=NOT_A_STAGE").status_code == 400


def test_the_receipt_endpoint_says_so_when_a_stage_has_no_receipt(tmp_path: Path) -> None:
    app, _ = studio_app(tmp_path)

    response = request(app, "GET", "/api/reels/demo/receipt?stage=RENDERED")

    assert response.status_code == 200
    assert response.json()["receipt"] is None
    assert response.json()["reason"]


# --- events and the live stream --------------------------------------------


def test_driving_a_project_one_stage_forward_publishes_an_event(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path, real_reference=True)
    before = ev.latest_event_id(config.database_path)

    result = request(app, "POST", "/api/projects/demo/actions", json_body={"action": "reference_analyze"})

    assert result.status_code == 200
    published = ev.events_since(config.database_path, after_id=before)
    stage_events = [event for event in published if event["kind"] == "stage"]
    assert stage_events, f"no stage event recorded, got {[event['kind'] for event in published]}"
    assert stage_events[-1]["project_id"] == "demo"
    assert stage_events[-1]["payload"]["action"] == "reference_analyze"
    assert stage_events[-1]["payload"]["next_stage"] == "BLUEPRINT_LOCKED"


def test_the_event_arrives_on_the_stream_after_the_stage_advances(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path, real_reference=True)
    before = ev.latest_event_id(config.database_path)
    assert request(app, "POST", "/api/projects/demo/actions", json_body={"action": "reference_analyze"}).status_code == 200

    response = request(app, "GET", f"/api/stream?after={before}&max_seconds=0.2&poll_ms=10")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [json.loads(line[len("data: ") :]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert any(frame["kind"] == "stage" for frame in frames)
    assert any(frame["payload"].get("next_stage") == "BLUEPRINT_LOCKED" for frame in frames)


def test_creating_a_project_publishes_an_event_too(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)

    kinds = [event["kind"] for event in ev.events_since(config.database_path)]

    assert "project" in kinds


def test_the_stream_resumes_from_the_last_event_id_header(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    ev.record_event(config.database_path, kind="one", project_id="demo")
    second = ev.record_event(config.database_path, kind="two", project_id="demo")

    response = request(
        app,
        "GET",
        "/api/stream?max_seconds=0.2&poll_ms=10",
        headers={"Last-Event-ID": str(second["id"] - 1)},
    )

    kinds = [json.loads(line[len("data: ") :])["kind"] for line in response.text.splitlines() if line.startswith("data: ")]
    assert kinds == ["two"]


def test_the_stream_refuses_an_unbounded_window(tmp_path: Path) -> None:
    app, _ = studio_app(tmp_path)

    assert request(app, "GET", "/api/stream?max_seconds=99999").status_code == 422
    assert request(app, "GET", "/api/stream?poll_ms=0").status_code == 422


def test_the_studio_schema_exists_after_the_app_starts(tmp_path: Path) -> None:
    # A database opened but never initialised is an empty file, not an empty schema, and
    # the first write against it fails with "no such table".
    import sqlite3

    config = studio_config(tmp_path)
    assert not config.database_path.exists()

    app = create_app(config=config)

    assert config.database_path.is_file()
    with sqlite3.connect(str(config.database_path)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"jobs", "events", "schedules", "status_cache"} <= tables
    assert request(app, "GET", "/api/health").json()["studio_database"]["error"] is None


def test_a_database_that_breaks_after_startup_is_reported_not_remembered(tmp_path: Path) -> None:
    # Every other field in /api/health is re-probed per request. A startup snapshot here
    # would keep reporting a healthy database after the disk filled or the volume left —
    # the one field in the strip that could go stale green.
    app, config = studio_app(tmp_path)
    assert request(app, "GET", "/api/health").json()["studio_database"]["status"] == "PASS"

    config.database_path.write_text("this is not a sqlite database", encoding="utf-8")
    broken = request(app, "GET", "/api/health").json()["studio_database"]

    assert broken["status"] == "FAIL"
    assert "unusable" in broken["error"]
    assert str(config.database_path) in broken["error"]


def test_a_deleted_database_is_recreated_and_the_repair_is_reported(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)
    config.database_path.unlink()

    report = request(app, "GET", "/api/health").json()["studio_database"]

    assert report["status"] == "PASS"
    assert report["repaired"] is True
    assert config.database_path.is_file()
    assert request(app, "GET", "/api/health").json()["studio_database"]["repaired"] is False


def test_an_empty_database_file_left_by_another_module_is_still_written_to(tmp_path: Path) -> None:
    config = studio_config(tmp_path)
    config.studio_dir.mkdir(parents=True, exist_ok=True)
    config.database_path.touch()

    recorded = ev.record_event(config.database_path, kind="stage", project_id="demo")

    assert recorded["id"] == 1
    assert ev.latest_event_id(config.database_path) == 1


def test_an_unwritable_event_log_never_fails_a_successful_stage_advance(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path, real_reference=True)
    config.studio_dir.mkdir(parents=True, exist_ok=True)
    config.database_path.write_text("this is not a sqlite database", encoding="utf-8")

    result = request(app, "POST", "/api/projects/demo/actions", json_body={"action": "reference_analyze"})

    assert result.status_code == 200
    assert result.json()["result"]["status"] == "PASS"
    assert result.json()["event_error"]
    assert (config.projects_root / "demo/reference/reference-lock.json").is_file()


def test_the_page_subscribes_by_one_constant_name_and_enumerates_no_kinds(tmp_path: Path) -> None:
    # A per-kind subscription list on the page is a second place the set of event kinds
    # lives, and it went stale against the daemon's thirteen immediately. There must be no
    # list here to go stale again.
    app, _ = studio_app(tmp_path)

    page = request(app, "GET", "/board").text

    assert f'var STREAM_EVENT_NAME = "{ev.STREAM_EVENT_NAME}"' in page
    assert "addEventListener(STREAM_EVENT_NAME" in page
    for kind in ("judgment_parked", "stage_settled", "job_deferred", "tick_error", "daemon_started"):
        assert kind not in page


def test_every_screen_is_reachable_from_every_other_screen(tmp_path: Path) -> None:
    app, _ = studio_app(tmp_path)

    dashboard = request(app, "GET", "/").text
    board = request(app, "GET", "/board").text

    assert 'href="/board"' in dashboard and 'href="/intake"' in dashboard
    assert 'href="/intake"' in board and 'href="/"' in board


def test_the_board_page_subscribes_to_the_stream_from_the_latest_event_id(tmp_path: Path) -> None:
    app, config = studio_app(tmp_path)

    payload: Dict[str, Any] = request(app, "GET", "/api/reels").json()
    page = request(app, "GET", "/board").text

    assert payload["latest_event_id"] >= 1
    assert "EventSource" in page
    assert f'data-after="{payload["latest_event_id"]}"' in page
