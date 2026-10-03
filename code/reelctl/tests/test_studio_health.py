from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from test_web import request

from reelctl.studio.config import StudioConfig
from reelctl.studio.health import health_report
from reelctl.web import create_app


def _tools(tmp_path: Path, *names: str) -> Path:
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    for name in names:
        binary = tools / name
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    return tools


def _config(tmp_path: Path, **overrides: str) -> StudioConfig:
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    env = {
        "REEL_STUDIO_PROJECTS_ROOT": str(projects),
        "REEL_STUDIO_STORAGE_ROOT": str(storage),
        "REEL_STUDIO_DIR": str(tmp_path / "studio"),
        "REEL_STUDIO_PATH": str(_tools(tmp_path, "ffmpeg", "ffprobe")),
        "REEL_STUDIO_MIN_FREE_BYTES": "0",
        "REEL_STUDIO_MIN_FREE_BYTES_INTERNAL": "0",
    }
    env.update(overrides)
    return StudioConfig.from_env(env)


def _heartbeat(config: StudioConfig, *, age_seconds: int) -> None:
    config.studio_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    config.daemon_heartbeat_path.write_text(
        json.dumps({"last_tick_utc": stamp.isoformat().replace("+00:00", "Z"), "pid": 4242}),
        encoding="utf-8",
    )


def _reason_containing(report: Dict[str, Any], needle: str) -> Optional[str]:
    return next((reason for reason in report["degraded_reasons"] if needle in reason), None)


def test_a_healthy_machine_reports_pass_with_no_degraded_reasons(tmp_path: Path) -> None:
    report = health_report(_config(tmp_path))

    assert report["status"] == "PASS"
    assert report["degraded_reasons"] == []
    assert report["mounts"]["storage"]["mounted"] is True
    assert report["mounts"]["storage"]["writable"] is True
    assert report["mounts"]["storage"]["free_bytes"] > 0
    assert report["mounts"]["projects"]["free_bytes"] > 0


def test_health_is_degraded_when_the_storage_volume_is_not_mounted(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(tmp_path / "unplugged"))

    report = health_report(config)

    assert report["status"] == "DEGRADED"
    assert report["mounts"]["storage"]["mounted"] is False
    reason = _reason_containing(report, "not mounted")
    assert reason is not None
    assert str(tmp_path / "unplugged") in reason


def test_health_is_degraded_when_storage_is_below_the_free_space_floor(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_MIN_FREE_BYTES=str(2**62))

    report = health_report(config)

    assert report["status"] == "DEGRADED"
    reason = _reason_containing(report, "free")
    assert reason is not None
    assert str(2**62) in reason
    assert report["mounts"]["storage"]["min_free_bytes"] == 2**62


def test_health_is_degraded_when_the_internal_disk_is_below_its_floor(tmp_path: Path) -> None:
    report = health_report(_config(tmp_path, REEL_STUDIO_MIN_FREE_BYTES_INTERNAL=str(2**62)))

    assert report["status"] == "DEGRADED"
    assert _reason_containing(report, "projects root") is not None


def test_health_is_degraded_when_storage_is_not_writable(tmp_path: Path) -> None:
    readonly = tmp_path / "readonly-storage"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        report = health_report(_config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(readonly)))
    finally:
        readonly.chmod(0o700)

    assert report["status"] == "DEGRADED"
    assert report["mounts"]["storage"]["writable"] is False
    assert _reason_containing(report, "not writable") is not None


def test_health_is_degraded_when_a_required_binary_is_missing(tmp_path: Path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()

    report = health_report(_config(tmp_path, REEL_STUDIO_PATH=str(empty)))

    assert report["status"] == "DEGRADED"
    assert report["binaries"]["ffmpeg"] == {"present": False, "path": None, "required": True}
    assert _reason_containing(report, "ffmpeg") is not None
    assert _reason_containing(report, "ffprobe") is not None


def test_health_reports_optional_binaries_without_degrading(tmp_path: Path) -> None:
    report = health_report(_config(tmp_path))

    assert report["binaries"]["ffmpeg"]["present"] is True
    assert report["binaries"]["ffmpeg"]["path"].endswith("/ffmpeg")
    assert report["binaries"]["claude"]["present"] is False
    assert report["binaries"]["claude"]["required"] is False
    assert report["status"] == "PASS"


def test_daemon_liveness_is_read_from_the_heartbeat_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _heartbeat(config, age_seconds=5)

    report = health_report(config)

    assert report["daemon"]["alive"] is True
    assert report["daemon"]["pid"] == 4242
    assert report["daemon"]["last_tick_utc"].endswith("Z")
    assert report["daemon"]["reason"] is None


def test_a_stale_heartbeat_is_not_alive_and_names_the_window(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_HEARTBEAT_MAX_AGE_SECONDS="30")
    _heartbeat(config, age_seconds=600)

    report = health_report(config)

    assert report["daemon"]["alive"] is False
    assert "30" in report["daemon"]["reason"]


def test_an_unreadable_heartbeat_is_reported_verbatim_not_assumed_alive(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.studio_dir.mkdir(parents=True, exist_ok=True)
    config.daemon_heartbeat_path.write_text("{not json", encoding="utf-8")

    report = health_report(config)

    assert report["daemon"]["alive"] is False
    assert report["daemon"]["reason"]


def test_a_missing_daemon_is_reported_but_does_not_gate_the_read_only_app(tmp_path: Path) -> None:
    report = health_report(_config(tmp_path))

    assert report["daemon"]["alive"] is False
    assert str(tmp_path / "studio") in report["daemon"]["reason"]
    assert report["status"] == "PASS"
    assert report["degraded_reasons"] == []


def test_health_endpoint_keeps_the_fail_closed_contract_and_adds_the_machine_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app = create_app(config=config)

    response = request(app, "GET", "/api/health")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "PASS"
    assert payload["bind_policy"] == "loopback_only"
    assert payload["publication_api"] is False
    assert payload["modes"] == ["original-montage", "reference-locked"]
    assert payload["port"] == 7335
    assert payload["projects_root"] == str(config.projects_root)
    assert payload["degraded_reasons"] == []
    assert set(payload["mounts"]) == {"storage", "projects"}
    assert payload["daemon"]["alive"] is False
    assert payload["capacity"]["max_judgment_jobs"] == 1


def test_health_endpoint_surfaces_degradation_over_http(tmp_path: Path) -> None:
    app = create_app(config=_config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(tmp_path / "unplugged")))

    payload = request(app, "GET", "/api/health").json()

    assert payload["status"] == "DEGRADED"
    assert any("not mounted" in reason for reason in payload["degraded_reasons"])


def test_health_on_the_real_default_configuration_is_well_formed(tmp_path: Path) -> None:
    report = health_report(StudioConfig.from_env().with_projects_root(tmp_path / "projects"))

    assert report["status"] in {"PASS", "DEGRADED"}
    assert (report["status"] == "DEGRADED") == bool(report["degraded_reasons"])
    assert all(isinstance(reason, str) and reason for reason in report["degraded_reasons"])


def test_projects_root_argument_still_wins_over_the_configured_root(tmp_path: Path) -> None:
    app = create_app(projects_root=tmp_path / "explicit")

    payload = request(app, "GET", "/api/health").json()

    assert payload["projects_root"] == str(tmp_path / "explicit")
