from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reelctl.errors import ReelctlError
from reelctl.studio import authority as authority_module
from reelctl.studio import jobs
from reelctl.studio.authority import FACTORY_OPEN, AuthorityDecision, authority_decision, require_factory_open
from reelctl.studio.config import StudioConfig
from reelctl.studio.daemon import Orchestrator
from reelctl.web import create_app


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _open_snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "authority": "REEL_PILOT",
        "factory_state": FACTORY_OPEN,
        "mutations_allowed": True,
    }


@pytest.mark.parametrize("contents", [None, "{", "[]", "{}"])
def test_authority_snapshot_fails_closed_when_missing_or_malformed(tmp_path: Path, contents: str | None) -> None:
    path = tmp_path / "authority.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    decision = authority_decision(path)

    assert decision.allowed is False
    assert decision.state == "GLOBAL_HALT"
    with pytest.raises(ReelctlError, match="GLOBAL_HALT"):
        require_factory_open("job claim", authority_path=path)


@pytest.mark.parametrize(
    "change",
    [
        {"authority": "NOT_REEL_PILOT"},
        {"schema_version": 2},
        {"factory_state": "GLOBAL_HALT"},
        {"mutations_allowed": False},
        {"mutations_allowed": 1},
        {"authorized_rows": [13]},
    ],
)
def test_only_exact_factory_open_contract_allows_mutation(tmp_path: Path, change: dict[str, object]) -> None:
    path = tmp_path / "authority.json"
    snapshot = _open_snapshot()
    snapshot.update(change)
    _write(path, snapshot)

    decision = authority_decision(path)

    # Row-scoped recovery is intentionally not a factory-open authority and cannot
    # enable Reel Studio or daemon work.
    if change == {"authorized_rows": [13]}:
        assert decision.allowed is False
    else:
        assert decision.allowed is False


def test_exact_factory_open_contract_allows_mutation(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    _write(path, _open_snapshot())

    assert authority_decision(path) == AuthorityDecision(True, FACTORY_OPEN, "Reel Pilot authorized factory mutations")
    require_factory_open("job claim", authority_path=path)


def test_low_level_job_mutation_is_blocked_before_database_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing-authority.json"
    database = tmp_path / "studio.db"
    monkeypatch.setattr(authority_module, "DEFAULT_AUTHORITY_PATH", missing)

    with pytest.raises(ReelctlError, match="GLOBAL_HALT"):
        jobs.enqueue(database, project_id="r001", stage="RENDERED", kind="deterministic")

    assert not database.exists()


def test_daemon_start_is_blocked_before_studio_state_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing-authority.json"
    projects = tmp_path / "projects"
    monkeypatch.setattr(authority_module, "DEFAULT_AUTHORITY_PATH", missing)
    config = StudioConfig.from_env({"REEL_STUDIO_PROJECTS_ROOT": str(projects)})

    with pytest.raises(ReelctlError, match="GLOBAL_HALT"):
        Orchestrator(config).start()

    assert not config.studio_dir.exists()


def test_web_mutation_is_blocked_but_read_surface_remains_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing-authority.json"
    projects = tmp_path / "projects"
    monkeypatch.setattr(authority_module, "DEFAULT_AUTHORITY_PATH", missing)
    client = TestClient(create_app(projects_root=projects))

    response = client.post(
        "/api/projects",
        json={"project_id": "r001", "reference": "/tmp/reference.mp4", "footage": "/tmp/footage"},
    )

    assert response.status_code == 423
    assert response.json()["status"] == "GLOBAL_HALT"
    assert not (projects / "r001").exists()
    assert client.get("/api/projects").status_code == 200
