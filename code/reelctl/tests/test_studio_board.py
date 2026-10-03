from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Tuple

import pytest
from test_web import request

from reelctl.locks import ProjectBusyError, project_lock
from reelctl.studio.board import board_model, project_ids, project_summaries, project_summary
from reelctl.web import create_app


def _app_with_project(tmp_path: Path, project_id: str = "demo") -> Tuple[Any, Path]:
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    footage = tmp_path / "footage"
    footage.mkdir(exist_ok=True)
    projects = tmp_path / "projects"
    app = create_app(projects_root=projects)
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
    return app, projects


# --- the contention fix ----------------------------------------------------


def test_listing_projects_takes_no_project_lock(tmp_path: Path) -> None:
    app, projects = _app_with_project(tmp_path)

    with project_lock(projects, "demo"):
        listing = request(app, "GET", "/api/projects")

    assert listing.status_code == 200
    assert [item["project_id"] for item in listing.json()["projects"]] == ["demo"]


def test_every_read_endpoint_answers_while_a_render_holds_the_lock(tmp_path: Path) -> None:
    app, projects = _app_with_project(tmp_path)
    candidate = projects / "demo/review/candidate.mp4"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"a-candidate-being-reviewed-mid-render")

    with project_lock(projects, "demo"):
        status = request(app, "GET", "/api/projects/demo")
        workbench = request(app, "GET", "/api/projects/demo/workbench")
        artifact = request(app, "GET", "/api/projects/demo/artifact?path=review%2Fcandidate.mp4")

    assert status.status_code == 200
    assert status.json()["next_stage"] == "REFERENCE_LOCKED"
    assert workbench.status_code == 200
    assert workbench.json()["recommended_action"] == "reference_analyze"
    assert artifact.status_code == 200
    assert artifact.content == b"a-candidate-being-reviewed-mid-render"


def test_the_read_model_functions_take_no_lock_either(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)

    with project_lock(projects, "demo"):
        assert project_ids(projects) == ["demo"]
        assert [item["project_id"] for item in project_summaries(projects)] == ["demo"]
        assert project_summary(projects, "demo")["project_id"] == "demo"
        assert [item["project_id"] for item in board_model(projects)["projects"]] == ["demo"]


def test_a_busy_project_still_refuses_a_mutating_action_with_a_named_reason(tmp_path: Path) -> None:
    app, projects = _app_with_project(tmp_path)

    with project_lock(projects, "demo"):
        response = request(app, "POST", "/api/projects/demo/actions", json_body={"action": "reference_analyze", "payload": {}})

    assert response.status_code == 409
    assert response.json()["status"] == "FAIL"
    assert "locked by another reelctl process" in response.json()["error"]


def test_the_lock_is_still_taken_for_mutations(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)

    with project_lock(projects, "demo"):
        with pytest.raises(ProjectBusyError):
            with project_lock(projects, "demo"):
                pass


# --- the board model -------------------------------------------------------


def test_board_model_reports_headline_and_layers_per_project(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)

    board = board_model(projects)

    assert len(board["projects"]) == 1
    row = board["projects"][0]
    assert row["project_id"] == "demo"
    assert row["mode"] == "original-montage"
    assert row["next_stage"] == "REFERENCE_LOCKED"
    assert row["headline"] == "PENDING_MACHINE"
    assert [layer["key"] for layer in row["layers"]][0] == "reference_lock"
    assert len(row["layers"]) == 14


def test_board_model_is_deterministic(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)

    assert board_model(projects) == board_model(projects)


def test_board_model_skips_directories_that_are_not_projects(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)
    (projects / "not-a-project").mkdir()
    (projects / ".reelctl-locks").mkdir(exist_ok=True)

    assert project_ids(projects) == ["demo"]
    assert [row["project_id"] for row in board_model(projects)["projects"]] == ["demo"]


def _registry(projects: Path, payload: object) -> None:
    (projects / "REEL_REGISTRY.json").write_text(json.dumps(payload), encoding="utf-8")


def test_board_model_reads_open_calls_from_the_registry_by_default(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)
    _registry(
        projects,
        {
            "schema_version": 2,
            "reels": [],
            "open_calls": [
                {"call_id": "CALL-001-missing-roles", "project": "demo", "card": "brain/07.md", "state": "AWAITING_OPERATOR"}
            ],
        },
    )

    row = board_model(projects)["projects"][0]

    assert row["headline"] == "PENDING_HUMAN"
    assert row["blocked_by_calls"] == ["CALL-001-missing-roles"]


def test_an_answered_registry_call_leaves_the_board_alone(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)
    _registry(
        projects,
        {
            "schema_version": 2,
            "reels": [],
            "open_calls": [{"call_id": "CALL-001", "project": "demo", "state": "ANSWERED"}],
        },
    )

    row = board_model(projects)["projects"][0]

    assert row["headline"] == "PENDING_MACHINE"
    assert row["blocked_by_calls"] == []


def test_a_call_against_another_reel_never_blocks_this_one(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)
    _registry(
        projects,
        {
            "schema_version": 2,
            "reels": [],
            "open_calls": [{"call_id": "CALL-001", "project": "some-other-reel", "state": "AWAITING_OPERATOR"}],
        },
    )

    row = board_model(projects)["projects"][0]

    assert row["headline"] == "PENDING_MACHINE"
    assert row["blocked_by_calls"] == []


def test_an_unparseable_registry_does_not_blank_the_board(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)
    (projects / "REEL_REGISTRY.json").write_text("{not json", encoding="utf-8")

    board = board_model(projects)

    assert [row["project_id"] for row in board["projects"]] == ["demo"]
    assert board["projects"][0]["headline"] == "PENDING_MACHINE"
    assert "does not parse" in board["registry"]["error"]


def test_a_missing_registry_is_reported_without_an_error(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)

    board = board_model(projects)

    assert board["registry"]["error"] is None
    assert board["projects"][0]["blocked_by_calls"] == []


def test_explicit_open_calls_take_precedence_over_the_registry(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)
    _registry(
        projects,
        {
            "schema_version": 2,
            "reels": [],
            "open_calls": [{"call_id": "FROM-REGISTRY", "project": "demo", "state": "AWAITING_OPERATOR"}],
        },
    )

    row = board_model(projects, open_calls={"demo": [{"call_id": "EXPLICIT", "state": "AWAITING_OPERATOR"}]})["projects"][0]

    assert row["blocked_by_calls"] == ["EXPLICIT"]


def test_a_studio_born_reels_mode_call_reaches_the_board(tmp_path: Path) -> None:
    # intake.py never writes REEL_REGISTRY.json, so this call exists only at
    # <project>/intake.json. Reading the registry alone showed nothing blocking while the
    # daemon drove the reel through SELECTION_LOCKED with mode_confirmed still false.
    from reelctl.studio.config import StudioConfig
    from reelctl.studio.intake import create_intake

    projects = tmp_path / "projects"
    projects.mkdir()
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    footage = tmp_path / "footage"
    footage.mkdir()
    config = StudioConfig.from_env(
        {"REEL_STUDIO_PROJECTS_ROOT": str(projects), "REEL_STUDIO_DIR": str(tmp_path / "studio")}
    )
    project_id = create_intake(config, reference=str(reference), lane="VOLUME", footage_root=str(footage))["project_id"]

    row = next(item for item in board_model(projects)["projects"] if item["project_id"] == project_id)

    assert row["blocked_by_calls"] == [f"CALL-MODE-{project_id}"]
    layers = {layer["key"]: layer["status"] for layer in row["layers"]}
    # The mode call is scoped to SELECTION_LOCKED: everything the mode does not govern runs.
    assert layers["reference_lock"] == "PENDING_MACHINE"
    assert layers["blueprint"] == "PENDING_MACHINE"
    assert layers["footage_index"] == "PENDING_MACHINE"
    assert layers["feasibility"] == "PENDING_MACHINE"
    assert layers["selection"] == "PENDING_HUMAN"
    assert layers["colour_grade_proof"] == "PENDING_HUMAN"
    assert layers["render"] == "PENDING_HUMAN"


def _ui_row(**overrides: Any) -> dict:
    row = {
        "project_id": "reel-ref-28-v1",
        "headline": "PENDING_HUMAN",
        "blocked_by_calls": ["CALL-001-missing-roles", "CALL-002-craft-quota", "CALL-003-grade-target"],
        "open_calls": [],
        "lane": "VOLUME",
        "lane_source": "registry",
        "mode": None,
        "next_stage": "REFERENCE_LOCKED",
        "job": None,
        "layers": [
            {
                "key": "reference_lock",
                "title": "Reference lock",
                "stage": "REFERENCE_LOCKED",
                "status": "PENDING_HUMAN",
                "reason": "awaiting the operator on CALL-001-missing-roles",
                "stage_status": "PENDING",
                "receipt": None,
                "receipt_sha256": None,
                "updated_at_utc": None,
                "items": [],
            }
        ],
    }
    row.update(overrides)
    return row


def test_the_board_page_names_the_calls_that_block_a_reel() -> None:
    from reelctl.studio.views import render_board_page

    html = render_board_page({"projects": [_ui_row()], "latest_event_id": 0})

    assert "CALL-001-missing-roles" in html
    assert "CALL-002-craft-quota" in html
    assert "CALL-003-grade-target" in html
    assert "awaiting the operator" in html


def test_the_board_page_says_nothing_about_calls_for_an_unblocked_reel() -> None:
    from reelctl.studio.views import render_board_page

    clear = _ui_row(blocked_by_calls=[], headline="PENDING_MACHINE")
    clear["layers"] = [dict(clear["layers"][0], status="PENDING_MACHINE", reason=None)]

    html = render_board_page({"projects": [clear], "latest_event_id": 0})

    assert "awaiting the operator" not in html


def test_the_row_header_does_not_claim_a_stage_scoped_call_stops_the_whole_reel() -> None:
    # A call scoped to SELECTION_LOCKED leaves reference-lock free, so the header must not
    # say the reel is blocked — the cells carry which stages actually wait.
    from reelctl.studio.views import render_board_page

    html = render_board_page({"projects": [_ui_row()], "latest_event_id": 0})

    assert "blocked —" not in html


def test_a_project_with_unreadable_state_appears_as_failed_not_as_a_gap(tmp_path: Path) -> None:
    _, projects = _app_with_project(tmp_path)
    (projects / "demo/state.json").write_text("{not json", encoding="utf-8")

    board = board_model(projects)

    assert [row["project_id"] for row in board["projects"]] == ["demo"]
    assert board["projects"][0]["headline"] == "FAILED"
    assert board["projects"][0]["reason"]


def test_dot_prefixed_scratch_dirs_are_not_projects(tmp_path: Path) -> None:
    # Observed failure: a failed-init scratch copy (`.scratch-…`) sat in the
    # projects root with project.json + state.json intact, carrying the SAME project_id
    # as the real project. Discovery picked it up: /api/projects died on the dot-name
    # (IdentifierError) and rebuild_from_disk crash-looped the daemon on the duplicate
    # status_cache primary key. Dot-prefixed directories are never projects.
    import shutil

    from reelctl.studio.db import rebuild_from_disk

    app, projects = _app_with_project(tmp_path)
    shutil.copytree(projects / "demo", projects / ".scratch-demo-failed-init")

    assert project_ids(projects) == ["demo"]

    listing = request(app, "GET", "/api/projects")
    assert listing.status_code == 200
    assert [item["project_id"] for item in listing.json()["projects"]] == ["demo"]

    report = rebuild_from_disk(projects, database_path=tmp_path / "studio.db")
    assert report["status"] == "PASS"
    assert report["projects"] == 1


def test_a_project_with_a_contract_breaking_project_json_appears_as_failed_not_as_a_500(tmp_path: Path) -> None:
    # Related failure: a lane wrote extra policy keys into a live
    # project's project.json. ContractValidationError is a ValueError, not a
    # ReelctlError, so one bad project killed the whole /api/projects listing.
    # The listing must degrade the way the board does: a FAILED row, never a gap
    # and never a dead endpoint.
    app, projects = _app_with_project(tmp_path)
    config_path = projects / "demo/project.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("policies", {})["typography_method"] = "doctrine text a lane wrote"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    listing = request(app, "GET", "/api/projects")

    assert listing.status_code == 200
    rows = {row["project_id"]: row for row in listing.json()["projects"]}
    assert "demo" in rows
    assert rows["demo"]["status"] == "FAIL"
    assert "typography_method" in rows["demo"]["error"]
