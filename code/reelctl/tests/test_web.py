from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, Optional

from httpx import ASGITransport, AsyncClient, Response

from reelctl.web import create_app


def make_reference(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=24:d=0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def request(app: Any, method: str, path: str, *, json_body: Optional[dict[str, Any]] = None) -> Response:
    async def call() -> Response:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://reel-studio.local") as client:
            return await client.request(method, path, json=json_body)

    return asyncio.run(call())


def test_health_declares_local_fail_closed_contract(tmp_path: Path) -> None:
    response = request(create_app(projects_root=tmp_path / "projects"), "GET", "/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "PASS"
    assert payload["bind_policy"] == "loopback_only"
    assert payload["publication_api"] is False
    assert payload["modes"] == ["original-montage", "reference-locked"]


def test_project_creation_requires_explicit_mode_and_defers_analysis(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"analysis-is-deliberately-deferred")
    footage = tmp_path / "footage"
    footage.mkdir()
    projects = tmp_path / "projects"
    app = create_app(projects_root=projects)

    missing_mode = request(
        app,
        "POST",
        "/api/projects",
        json_body={"project_id": "demo", "reference": str(reference), "footage": str(footage)},
    )
    created = request(
        app,
        "POST",
        "/api/projects",
        json_body={
            "project_id": "demo",
            "reference": str(reference),
            "footage": str(footage),
            "mode": "original-montage",
        },
    )
    status = request(app, "GET", "/api/projects/demo")
    listing = request(app, "GET", "/api/projects")

    assert missing_mode.status_code == 422
    assert created.status_code == 201
    assert created.json()["next_stage"] == "REFERENCE_LOCKED"
    assert status.status_code == 200
    assert status.json()["mode"] == "original-montage"
    assert status.json()["next_stage"] == "REFERENCE_LOCKED"
    assert [item["project_id"] for item in listing.json()["projects"]] == ["demo"]
    assert json.loads((projects / "demo/project.json").read_text())["mode"] == "original-montage"


def test_run_stops_at_blueprint_gate_and_resumes_without_rewriting_reference_lock(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    make_reference(reference)
    footage = tmp_path / "footage"
    footage.mkdir()
    projects = tmp_path / "projects"
    app = create_app(projects_root=projects)

    created = request(
        app,
        "POST",
        "/api/projects",
        json_body={
            "project_id": "demo",
            "reference": str(reference),
            "footage": str(footage),
            "mode": "original-montage",
        },
    )
    assert created.status_code == 201
    first = request(app, "POST", "/api/projects/demo/run", json_body={"revision": "v001"})
    lock_before = (projects / "demo/reference/reference-lock.json").read_bytes()
    second = request(app, "POST", "/api/projects/demo/run", json_body={"revision": "v001"})
    lock_after = (projects / "demo/reference/reference-lock.json").read_bytes()

    assert first.status_code == 200
    assert first.json()["status"] == "BLOCKED"
    assert first.json()["next_stage"] == "BLUEPRINT_LOCKED"
    assert second.json()["status"] == "BLOCKED"
    assert second.json()["next_stage"] == "BLUEPRINT_LOCKED"
    assert lock_before == lock_after


def test_control_plane_has_no_publish_route(tmp_path: Path) -> None:
    response = request(create_app(projects_root=tmp_path / "projects"), "POST", "/api/projects/demo/publish", json_body={})

    assert response.status_code == 404


def test_server_refuses_non_loopback_bind(tmp_path: Path) -> None:
    from reelctl.errors import ReelctlError
    from reelctl.web import run_server

    try:
        run_server(projects_root=tmp_path / "projects", host="0.0.0.0", port=8765)
    except ReelctlError as exc:
        assert "local-only" in str(exc)
    else:
        raise AssertionError("non-loopback bind unexpectedly accepted")


def test_cli_exposes_serve_command(capsys) -> None:
    from reelctl.cli import main

    assert main(["serve", "--help"], exit_on_error=False) == 0
    assert "--host" in capsys.readouterr().out


def test_dashboard_explains_modes_and_review_state(tmp_path: Path) -> None:
    response = request(create_app(projects_root=tmp_path / "projects"), "GET", "/")

    assert response.status_code == 200
    assert "Reel Studio" in response.text
    assert "Original montage" in response.text
    assert "Reference locked" in response.text
    assert "LOCAL_REVIEW_READY" in response.text
    assert "/artifact?path=" in response.text
    assert "Create project" in response.text


def test_invalid_project_id_is_a_client_error_not_a_server_error(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    footage = tmp_path / "footage"
    footage.mkdir()
    response = request(
        create_app(projects_root=tmp_path / "projects"),
        "POST",
        "/api/projects",
        json_body={
            "project_id": "../escape",
            "reference": str(reference),
            "footage": str(footage),
            "mode": "original-montage",
        },
    )

    assert response.status_code == 400
    assert response.json()["status"] == "FAIL"


def test_workbench_exposes_only_engine_actions(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    footage = tmp_path / "footage"
    footage.mkdir()
    app = create_app(projects_root=tmp_path / "projects")
    created = request(
        app,
        "POST",
        "/api/projects",
        json_body={
            "project_id": "workbench-demo",
            "reference": str(reference),
            "footage": str(footage),
            "mode": "original-montage",
        },
    )
    assert created.status_code == 201

    workbench = request(app, "GET", "/api/projects/workbench-demo/workbench")
    assert workbench.status_code == 200
    payload = workbench.json()
    assert payload["next_stage"] == "REFERENCE_LOCKED"
    assert "reference_analyze" in payload["actions"]
    assert "publish" not in payload["actions"]

    unknown = request(
        app,
        "POST",
        "/api/projects/workbench-demo/actions",
        json_body={"action": "publish", "payload": {}},
    )
    assert unknown.status_code == 422


def test_action_endpoint_rejects_unlisted_action_name(tmp_path: Path) -> None:
    response = request(
        create_app(projects_root=tmp_path / "projects"),
        "POST",
        "/api/projects/demo/actions",
        json_body={"action": "shell", "payload": {}},
    )
    assert response.status_code == 422


def test_workbench_reference_action_delegates_to_engine(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    make_reference(reference)
    footage = tmp_path / "footage"
    footage.mkdir()
    projects = tmp_path / "projects"
    app = create_app(projects_root=projects)
    created = request(
        app,
        "POST",
        "/api/projects",
        json_body={"project_id": "action-demo", "reference": str(reference), "footage": str(footage), "mode": "original-montage"},
    )
    assert created.status_code == 201

    result = request(app, "POST", "/api/projects/action-demo/actions", json_body={"action": "reference_analyze", "payload": {}})

    assert result.status_code == 200
    assert result.json()["result"]["status"] == "PASS"
    assert result.json()["result"]["next_stage"] == "BLUEPRINT_LOCKED"
    assert (projects / "action-demo/reference/reference-lock.json").is_file()


def test_artifact_reader_is_confined_to_reviewable_project_tree(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    footage = tmp_path / "footage"
    footage.mkdir()
    projects = tmp_path / "projects"
    app = create_app(projects_root=projects)
    created = request(
        app,
        "POST",
        "/api/projects",
        json_body={"project_id": "artifact-demo", "reference": str(reference), "footage": str(footage), "mode": "original-montage"},
    )
    assert created.status_code == 201
    candidate = projects / "artifact-demo/review/candidate.mp4"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"private-candidate")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret-outside")

    valid = request(app, "GET", "/api/projects/artifact-demo/artifact?path=review%2Fcandidate.mp4")
    escaped = request(app, "GET", "/api/projects/artifact-demo/artifact?path=..%2Foutside.txt")

    assert valid.status_code == 200
    assert valid.content == b"private-candidate"
    assert escaped.status_code == 400
