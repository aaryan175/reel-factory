from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from reelctl.errors import ReelctlError
from reelctl.studio.config import DEFAULT_MIN_FREE_BYTES, DEFAULT_PORT, REQUIRED_BINARIES, StudioConfig


def test_default_port_is_7335_wherever_it_is_declared() -> None:
    from reelctl.cli import build_parser
    from reelctl.web import run_server

    assert DEFAULT_PORT == 7335
    assert StudioConfig.from_env({}).port == 7335
    assert inspect.signature(run_server).parameters["port"].default == 7335
    assert build_parser().parse_args(["serve"]).port == 7335


def test_config_defaults_match_the_storage_and_layout_doctrine() -> None:
    from reelctl.cli import DEFAULT_PROJECTS_ROOT as CLI_PROJECTS_ROOT

    config = StudioConfig.from_env({})

    assert config.host == "127.0.0.1"
    assert config.projects_root == CLI_PROJECTS_ROOT
    assert config.storage_root == Path("/Volumes/WORKDRIVE")
    assert config.workbench_root == Path("/Volumes/WORKDRIVE/workbench")
    assert config.library_path == config.projects_root / "FOOTAGE_LIBRARY.json"
    assert config.registry_path == config.projects_root / "REEL_REGISTRY.json"
    assert config.studio_dir == config.projects_root / ".studio"
    assert config.database_path == config.studio_dir / "studio.db"
    assert config.log_dir == config.studio_dir / "logs"
    assert config.min_free_bytes_storage == DEFAULT_MIN_FREE_BYTES == 20 * 1024**3
    assert config.min_free_bytes_internal == DEFAULT_MIN_FREE_BYTES
    assert config.max_judgment_jobs == 1
    assert REQUIRED_BINARIES == ("ffmpeg", "ffprobe")


def test_tool_path_default_carries_the_launchd_directories_expanded() -> None:
    config = StudioConfig.from_env({})

    assert str(Path.home() / ".local/bin") in config.tool_path
    assert "/opt/homebrew/bin" in config.tool_path
    assert all(Path(entry).is_absolute() for entry in config.tool_path)


def test_env_overrides_every_configurable_value(tmp_path: Path) -> None:
    config = StudioConfig.from_env(
        {
            "REEL_STUDIO_PROJECTS_ROOT": str(tmp_path / "projects"),
            "REEL_STUDIO_LIBRARY": str(tmp_path / "library.json"),
            "REEL_STUDIO_REGISTRY": str(tmp_path / "registry.json"),
            "REEL_STUDIO_STORAGE_ROOT": str(tmp_path / "storage"),
            "REEL_STUDIO_DIR": str(tmp_path / "studio"),
            "REEL_STUDIO_HOST": "localhost",
            "REEL_STUDIO_PORT": "7999",
            "REEL_STUDIO_PATH": "/a/bin:/b/bin",
            "REEL_STUDIO_MIN_FREE_BYTES": "1024",
            "REEL_STUDIO_MIN_FREE_BYTES_INTERNAL": "2048",
            "REEL_STUDIO_MAX_JUDGMENT_JOBS": "3",
            "REEL_STUDIO_HEARTBEAT_MAX_AGE_SECONDS": "45",
        }
    )

    assert config.projects_root == tmp_path / "projects"
    assert config.library_path == tmp_path / "library.json"
    assert config.registry_path == tmp_path / "registry.json"
    assert config.storage_root == tmp_path / "storage"
    assert config.studio_dir == tmp_path / "studio"
    assert config.host == "localhost"
    assert config.port == 7999
    assert config.tool_path == ("/a/bin", "/b/bin")
    assert config.min_free_bytes_storage == 1024
    assert config.min_free_bytes_internal == 2048
    assert config.max_judgment_jobs == 3
    assert config.heartbeat_max_age_seconds == 45


def test_projects_root_override_carries_its_derived_paths(tmp_path: Path) -> None:
    moved = StudioConfig.from_env({}).with_projects_root(tmp_path / "elsewhere")

    assert moved.projects_root == tmp_path / "elsewhere"
    assert moved.library_path == tmp_path / "elsewhere/FOOTAGE_LIBRARY.json"
    assert moved.registry_path == tmp_path / "elsewhere/REEL_REGISTRY.json"
    assert moved.studio_dir == tmp_path / "elsewhere/.studio"


def test_explicitly_configured_paths_survive_a_projects_root_override(tmp_path: Path) -> None:
    pinned = StudioConfig.from_env({"REEL_STUDIO_LIBRARY": str(tmp_path / "pinned.json")})

    moved = pinned.with_projects_root(tmp_path / "elsewhere")

    assert moved.library_path == tmp_path / "pinned.json"


def test_the_judgment_liveness_thresholds_carry_the_operators_defaults() -> None:
    config = StudioConfig.from_env({})

    assert config.judgment_first_progress_timeout_seconds == 600
    assert config.judgment_stall_timeout_seconds == 1800
    assert config.judgment_liveness_poll_seconds == 15.0
    # The wall clock stays the outer bound; the watchdog only ends hangs sooner.
    assert config.judgment_timeout_seconds == 3 * 60 * 60


def test_the_judgment_liveness_thresholds_are_configurable() -> None:
    config = StudioConfig.from_env(
        {
            "REEL_STUDIO_JUDGMENT_FIRST_PROGRESS_TIMEOUT_SECONDS": "300",
            "REEL_STUDIO_JUDGMENT_STALL_TIMEOUT_SECONDS": "900",
            "REEL_STUDIO_JUDGMENT_LIVENESS_POLL_SECONDS": "5",
        }
    )

    assert config.judgment_first_progress_timeout_seconds == 300
    assert config.judgment_stall_timeout_seconds == 900
    assert config.judgment_liveness_poll_seconds == 5.0


@pytest.mark.parametrize(
    "env",
    [
        {"REEL_STUDIO_PORT": "not-a-port"},
        {"REEL_STUDIO_PORT": "0"},
        {"REEL_STUDIO_PORT": "70000"},
        {"REEL_STUDIO_MIN_FREE_BYTES": "-1"},
        {"REEL_STUDIO_MAX_JUDGMENT_JOBS": "-1"},
        {"REEL_STUDIO_HEARTBEAT_MAX_AGE_SECONDS": "0"},
        {"REEL_STUDIO_PATH": ""},
        {"REEL_STUDIO_JUDGMENT_FIRST_PROGRESS_TIMEOUT_SECONDS": "0"},
        {"REEL_STUDIO_JUDGMENT_FIRST_PROGRESS_TIMEOUT_SECONDS": "soon"},
        {"REEL_STUDIO_JUDGMENT_STALL_TIMEOUT_SECONDS": "0"},
        {"REEL_STUDIO_JUDGMENT_LIVENESS_POLL_SECONDS": "0"},
    ],
)
def test_invalid_configuration_fails_closed(env: dict) -> None:
    with pytest.raises(ReelctlError):
        StudioConfig.from_env(env)


def test_binary_resolution_uses_the_configured_tool_path_not_the_inherited_one(tmp_path: Path, monkeypatch) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    ffmpeg = tools / "ffmpeg"
    ffmpeg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ffmpeg.chmod(0o755)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    ffprobe = decoy / "ffprobe"
    ffprobe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ffprobe.chmod(0o755)
    monkeypatch.setenv("PATH", str(decoy))

    config = StudioConfig.from_env({"REEL_STUDIO_PATH": str(tools)})

    assert config.which("ffmpeg") == str(ffmpeg)
    assert config.which("ffprobe") is None


def test_the_factory_env_vars_set_the_roots_and_the_agent_db_is_opt_in(tmp_path: Path) -> None:
    config = StudioConfig.from_env(
        {"REEL_FACTORY_HOME": str(tmp_path / "home"), "REEL_FACTORY_WORKDRIVE": str(tmp_path / "volume")}
    )
    assert config.projects_root == tmp_path / "home"
    assert config.storage_root == tmp_path / "volume"
    assert config.agent_session_database is None
    configured = StudioConfig.from_env({"REEL_STUDIO_AGENT_DB": str(tmp_path / "sessions.db")})
    assert configured.agent_session_database == tmp_path / "sessions.db"
