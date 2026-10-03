"""Studio runtime configuration: environment-sourced, defaulted, fail-closed.

Binary lookup deliberately ignores the inherited ``PATH``. A launchd job starts with
``/usr/bin:/bin``, so resolving ffmpeg through the ambient environment would report it
missing on a machine where it is installed. ``tool_path`` is the resolution order for
every binary the studio shells out to.

Locations are environment-driven so nothing is tied to one machine:

  REEL_STUDIO_PROJECTS_ROOT (or REEL_FACTORY_HOME)   projects root, default ~/reel-production
  REEL_STUDIO_STORAGE_ROOT (or REEL_FACTORY_WORKDRIVE) external work volume, default /Volumes/WORKDRIVE
  REEL_STUDIO_AGENT_DB      optional read-only SQLite session log of an interactive agent
                            runtime; when unset the concurrency courtesy check is disabled
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional, Tuple

from ..errors import ReelctlError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7335
DEFAULT_PROJECTS_ROOT = Path.home() / "reel-production"
DEFAULT_STORAGE_ROOT = Path("/Volumes/WORKDRIVE")  # documented default; override via env
DEFAULT_TOOL_PATH = ("~/.local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin")
DEFAULT_MIN_FREE_BYTES = 20 * 1024**3
DEFAULT_MAX_JUDGMENT_JOBS = 1
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 90
# Daemon-only keys (Task 6). The architecture's tick is 20s; the heartbeat window above is
# already sized for roughly three of them.
DEFAULT_TICK_SECONDS = 20.0
DEFAULT_STAGE_TIMEOUT_SECONDS = 3 * 60 * 60
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_BACKOFF_SECONDS = (60, 300, 900)
DEFAULT_BUSY_BACKOFF_SECONDS = 60
# How long a stage waits after the daemon declines to start it because a declared input was
# unreadable (v1.1 input preflight). Long enough that a wall needing an operator — a TCC
# grant, a remounted volume — is not re-probed every tick; short enough that work resumes
# soon after it is cleared.
DEFAULT_WITHHELD_BACKOFF_SECONDS = 30 * 60
DEFAULT_REVISION = "v001"
DEFAULT_OWNER = "studio-daemon"
DEFAULT_CRAFT_QUOTA = 1
DEFAULT_AGENT_SESSION_DATABASE: Optional[Path] = None
# Judgment-only keys (Task 7). The contract file is the project's onboarding prompt;
# its sha256 is recorded per job, so changing it is changing the contract.
DEFAULT_JUDGMENT_CONTRACT_FILENAME = "REEL-BRAIN-FULL-FORENSIC-ONBOARDING-PROMPT.md"
DEFAULT_JUDGMENT_MODEL = "opus"
DEFAULT_JUDGMENT_FALLBACK_MODEL = "sonnet"
DEFAULT_JUDGMENT_TIMEOUT_SECONDS = 3 * 60 * 60
# Liveness. The wall clock above is the outer bound and nothing else: two
# workers hung before their first model response and held the process open for hours
# without writing a byte. These two deadlines are what actually ends that failure —
# time from spawn to the first observable sign of life, and time between signs after.
DEFAULT_JUDGMENT_FIRST_PROGRESS_TIMEOUT_SECONDS = 600
DEFAULT_JUDGMENT_STALL_TIMEOUT_SECONDS = 30 * 60
DEFAULT_JUDGMENT_LIVENESS_POLL_SECONDS = 15.0
DEFAULT_JUDGMENT_PERMISSION_MODE = "acceptEdits"
DEFAULT_JUDGMENT_MAX_PARK_SECONDS = 12 * 60 * 60
DEFAULT_JUDGMENT_ENABLED = True
# The modes this `claude` build accepts. Anything else would be an invented flag value.
PERMISSION_MODES = ("acceptEdits", "auto", "manual", "dontAsk", "plan")

# Missing these stops every render and index, so they gate the machine's health.
REQUIRED_BINARIES = ("ffmpeg", "ffprobe")
# Reported so the health strip can name what is absent without calling the box unhealthy.
REPORTED_BINARIES = ("ffmpeg", "ffprobe", "claude", "reelctl", "rclone", "yt-dlp")


def _absolute(value: str) -> Path:
    return Path(str(value)).expanduser().absolute()


def _path(env: Mapping[str, str], key: str, default: Path) -> Path:
    raw = env.get(key)
    if raw is None:
        return default
    if not raw.strip():
        raise ReelctlError(f"{key} is set but empty")
    return _absolute(raw)


def _int(env: Mapping[str, str], key: str, default: int, *, minimum: int, maximum: Optional[int] = None) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ReelctlError(f"{key} must be an integer, got {raw!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ReelctlError(f"{key} must be {bound}, got {value}")
    return value


def _float(env: Mapping[str, str], key: str, default: float, *, minimum: float) -> float:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ReelctlError(f"{key} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ReelctlError(f"{key} must be >= {minimum}, got {value}")
    return value


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ReelctlError(f"{key} must be a boolean (true/false), got {raw!r}")


def _choice(env: Mapping[str, str], key: str, default: str, *, allowed: Tuple[str, ...]) -> str:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip()
    if value not in allowed:
        raise ReelctlError(f"{key} must be one of {', '.join(allowed)}, got {raw!r}")
    return value


def _identifier(env: Mapping[str, str], key: str, default: str) -> str:
    raw = env.get(key)
    if raw is None:
        return default
    if not raw.strip():
        raise ReelctlError(f"{key} is set but empty")
    return raw.strip()


def _tool_path(env: Mapping[str, str]) -> Tuple[str, ...]:
    raw = env.get("REEL_STUDIO_PATH")
    entries = DEFAULT_TOOL_PATH if raw is None else tuple(item for item in raw.split(os.pathsep) if item.strip())
    if not entries:
        raise ReelctlError("REEL_STUDIO_PATH is set but contains no directories")
    return tuple(str(_absolute(entry)) for entry in entries)


@dataclass(frozen=True)
class StudioConfig:
    projects_root: Path
    library_path: Path
    registry_path: Path
    storage_root: Path
    studio_dir: Path
    host: str
    port: int
    tool_path: Tuple[str, ...]
    min_free_bytes_storage: int
    min_free_bytes_internal: int
    max_judgment_jobs: int
    heartbeat_max_age_seconds: int
    # Daemon-only (Task 6). Defaulted so this dataclass stays constructible by every
    # caller that predates the orchestrator.
    tick_seconds: float = DEFAULT_TICK_SECONDS
    stage_timeout_seconds: int = DEFAULT_STAGE_TIMEOUT_SECONDS
    claim_ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    busy_backoff_seconds: int = DEFAULT_BUSY_BACKOFF_SECONDS
    withheld_backoff_seconds: int = DEFAULT_WITHHELD_BACKOFF_SECONDS
    craft_quota: int = DEFAULT_CRAFT_QUOTA
    revision: str = DEFAULT_REVISION
    owner: str = DEFAULT_OWNER
    agent_session_database: Optional[Path] = DEFAULT_AGENT_SESSION_DATABASE
    # Judgment-only (Task 7), defaulted for every caller that predates the adapter.
    judgment_contract_path: Path = DEFAULT_PROJECTS_ROOT / DEFAULT_JUDGMENT_CONTRACT_FILENAME
    judgment_model: str = DEFAULT_JUDGMENT_MODEL
    judgment_fallback_model: str = DEFAULT_JUDGMENT_FALLBACK_MODEL
    judgment_timeout_seconds: int = DEFAULT_JUDGMENT_TIMEOUT_SECONDS
    judgment_first_progress_timeout_seconds: int = DEFAULT_JUDGMENT_FIRST_PROGRESS_TIMEOUT_SECONDS
    judgment_stall_timeout_seconds: int = DEFAULT_JUDGMENT_STALL_TIMEOUT_SECONDS
    judgment_liveness_poll_seconds: float = DEFAULT_JUDGMENT_LIVENESS_POLL_SECONDS
    judgment_permission_mode: str = DEFAULT_JUDGMENT_PERMISSION_MODE
    judgment_max_park_seconds: int = DEFAULT_JUDGMENT_MAX_PARK_SECONDS
    judgment_enabled: bool = DEFAULT_JUDGMENT_ENABLED

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "StudioConfig":
        source = os.environ if env is None else env
        projects_root = _path(
            source, "REEL_STUDIO_PROJECTS_ROOT", _path(source, "REEL_FACTORY_HOME", DEFAULT_PROJECTS_ROOT)
        )
        host = source.get("REEL_STUDIO_HOST", DEFAULT_HOST)
        if not host.strip():
            raise ReelctlError("REEL_STUDIO_HOST is set but empty")
        return cls(
            projects_root=projects_root,
            library_path=_path(source, "REEL_STUDIO_LIBRARY", projects_root / "FOOTAGE_LIBRARY.json"),
            registry_path=_path(source, "REEL_STUDIO_REGISTRY", projects_root / "REEL_REGISTRY.json"),
            storage_root=_path(
                source, "REEL_STUDIO_STORAGE_ROOT", _path(source, "REEL_FACTORY_WORKDRIVE", DEFAULT_STORAGE_ROOT)
            ),
            studio_dir=_path(source, "REEL_STUDIO_DIR", projects_root / ".studio"),
            host=host,
            port=_int(source, "REEL_STUDIO_PORT", DEFAULT_PORT, minimum=1, maximum=65535),
            tool_path=_tool_path(source),
            min_free_bytes_storage=_int(source, "REEL_STUDIO_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES, minimum=0),
            min_free_bytes_internal=_int(source, "REEL_STUDIO_MIN_FREE_BYTES_INTERNAL", DEFAULT_MIN_FREE_BYTES, minimum=0),
            max_judgment_jobs=_int(source, "REEL_STUDIO_MAX_JUDGMENT_JOBS", DEFAULT_MAX_JUDGMENT_JOBS, minimum=0),
            heartbeat_max_age_seconds=_int(
                source, "REEL_STUDIO_HEARTBEAT_MAX_AGE_SECONDS", DEFAULT_HEARTBEAT_MAX_AGE_SECONDS, minimum=1
            ),
            tick_seconds=_float(source, "REEL_STUDIO_TICK_SECONDS", DEFAULT_TICK_SECONDS, minimum=0.0),
            stage_timeout_seconds=_int(source, "REEL_STUDIO_STAGE_TIMEOUT_SECONDS", DEFAULT_STAGE_TIMEOUT_SECONDS, minimum=1),
            claim_ttl_seconds=_int(source, "REEL_STUDIO_CLAIM_TTL_SECONDS", DEFAULT_CLAIM_TTL_SECONDS, minimum=1),
            max_attempts=_int(source, "REEL_STUDIO_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, minimum=1),
            busy_backoff_seconds=_int(source, "REEL_STUDIO_BUSY_BACKOFF_SECONDS", DEFAULT_BUSY_BACKOFF_SECONDS, minimum=1),
            withheld_backoff_seconds=_int(
                source, "REEL_STUDIO_WITHHELD_BACKOFF_SECONDS", DEFAULT_WITHHELD_BACKOFF_SECONDS, minimum=1
            ),
            craft_quota=_int(source, "REEL_STUDIO_CRAFT_QUOTA", DEFAULT_CRAFT_QUOTA, minimum=0),
            revision=_identifier(source, "REEL_STUDIO_REVISION", DEFAULT_REVISION),
            owner=_identifier(source, "REEL_STUDIO_OWNER", DEFAULT_OWNER),
            agent_session_database=(
                _path(source, "REEL_STUDIO_AGENT_DB", Path()) if source.get("REEL_STUDIO_AGENT_DB") else None
            ),
            judgment_contract_path=_path(
                source, "REEL_STUDIO_JUDGMENT_CONTRACT", projects_root / DEFAULT_JUDGMENT_CONTRACT_FILENAME
            ),
            judgment_model=_identifier(source, "REEL_STUDIO_JUDGMENT_MODEL", DEFAULT_JUDGMENT_MODEL),
            judgment_fallback_model=_identifier(
                source, "REEL_STUDIO_JUDGMENT_FALLBACK_MODEL", DEFAULT_JUDGMENT_FALLBACK_MODEL
            ),
            judgment_timeout_seconds=_int(
                source, "REEL_STUDIO_JUDGMENT_TIMEOUT_SECONDS", DEFAULT_JUDGMENT_TIMEOUT_SECONDS, minimum=1
            ),
            judgment_first_progress_timeout_seconds=_int(
                source,
                "REEL_STUDIO_JUDGMENT_FIRST_PROGRESS_TIMEOUT_SECONDS",
                DEFAULT_JUDGMENT_FIRST_PROGRESS_TIMEOUT_SECONDS,
                minimum=1,
            ),
            judgment_stall_timeout_seconds=_int(
                source, "REEL_STUDIO_JUDGMENT_STALL_TIMEOUT_SECONDS", DEFAULT_JUDGMENT_STALL_TIMEOUT_SECONDS, minimum=1
            ),
            judgment_liveness_poll_seconds=_float(
                source, "REEL_STUDIO_JUDGMENT_LIVENESS_POLL_SECONDS", DEFAULT_JUDGMENT_LIVENESS_POLL_SECONDS, minimum=0.01
            ),
            judgment_permission_mode=_choice(
                source, "REEL_STUDIO_JUDGMENT_PERMISSION_MODE", DEFAULT_JUDGMENT_PERMISSION_MODE, allowed=PERMISSION_MODES
            ),
            judgment_max_park_seconds=_int(
                source, "REEL_STUDIO_JUDGMENT_MAX_PARK_SECONDS", DEFAULT_JUDGMENT_MAX_PARK_SECONDS, minimum=1
            ),
            judgment_enabled=_bool(source, "REEL_STUDIO_JUDGMENT_ENABLED", DEFAULT_JUDGMENT_ENABLED),
        )

    def with_projects_root(self, root: Path) -> "StudioConfig":
        """Move the projects root, carrying any path that was derived from the old one."""
        moved = _absolute(str(root))
        updates = {"projects_root": moved}
        if self.library_path == self.projects_root / "FOOTAGE_LIBRARY.json":
            updates["library_path"] = moved / "FOOTAGE_LIBRARY.json"
        if self.registry_path == self.projects_root / "REEL_REGISTRY.json":
            updates["registry_path"] = moved / "REEL_REGISTRY.json"
        if self.studio_dir == self.projects_root / ".studio":
            updates["studio_dir"] = moved / ".studio"
        if self.judgment_contract_path == self.projects_root / DEFAULT_JUDGMENT_CONTRACT_FILENAME:
            updates["judgment_contract_path"] = moved / DEFAULT_JUDGMENT_CONTRACT_FILENAME
        return replace(self, **updates)

    @property
    def workbench_root(self) -> Path:
        return self.storage_root / "workbench"

    @property
    def daemon_heartbeat_path(self) -> Path:
        return self.studio_dir / "daemon-heartbeat.json"

    @property
    def database_path(self) -> Path:
        return self.studio_dir / "studio.db"

    @property
    def log_dir(self) -> Path:
        return self.studio_dir / "logs"

    @property
    def job_receipts_dir(self) -> Path:
        """Where a judgment job's receipt and worker log land."""
        return self.projects_root / "_receipts" / "studio" / "jobs"

    @property
    def judgment_capacity_path(self) -> Path:
        """The one place a headless capacity wall is remembered between jobs."""
        return self.studio_dir / "judgment-capacity.json"

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name, path=os.pathsep.join(self.tool_path))
