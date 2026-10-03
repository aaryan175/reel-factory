"""Advisory ownership claims and the agent-session courtesy check (§9.3, §9.5).

The flock in ``reelctl.locks`` is the authority — a claim can never grant permission the
lock refuses. What a claim adds is visibility across *time*: a judgment run can take
twenty minutes without holding a lock, and flock alone would happily let a second worker
start on the same project. So the daemon writes ``.studio/claims/<project>.json`` before
it starts and refuses anything claimed by a different owner whose heartbeat is fresh.

Both checks fail open. A corrupt claim file is treated as no claim; an unreadable agent-session
database means "check skipped", recorded and moved past. Neither may stop the machine.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..hashing import atomic_write_json
from ..identifiers import validate_identifier
from .authority import guarded_mutation
from .config import StudioConfig

DEFAULT_AGENT_SESSION_WINDOW_SECONDS = 3600


@dataclass(frozen=True)
class Claim:
    project_id: str
    owner: str
    pid: int
    lane: Optional[str]
    heartbeat_utc: str
    note: Optional[str]
    path: Path


def _stamp(moment: Optional[datetime] = None) -> str:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(stamp: Any) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def claims_dir(config: StudioConfig) -> Path:
    return config.studio_dir / "claims"


def claim_path(config: StudioConfig, project_id: str) -> Path:
    return claims_dir(config) / "{}.json".format(validate_identifier(project_id, kind="project"))


def read_claim(config: StudioConfig, project_id: str) -> Optional[Claim]:
    path = claim_path(config, project_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Claim(
            project_id=project_id,
            owner=str(payload["owner"]),
            pid=int(payload.get("pid", 0)),
            lane=payload.get("lane"),
            heartbeat_utc=str(payload["heartbeat_utc"]),
            note=payload.get("note"),
            path=path,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def acquire_claim(
    config: StudioConfig,
    project_id: str,
    *,
    owner: str,
    lane: Optional[str] = None,
    note: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, Optional[str]]:
    """Claim a project. Returns ``(taken, reason)``; the reason names the other owner."""
    moment = now or datetime.now(timezone.utc)
    ttl = config.claim_ttl_seconds if ttl_seconds is None else ttl_seconds
    existing = read_claim(config, project_id)
    reason: Optional[str] = None
    if existing is not None and existing.owner != owner:
        beat = _parse(existing.heartbeat_utc)
        age = (moment - beat).total_seconds() if beat else None
        if age is not None and age < ttl:
            return False, "{} is claimed by {} (pid {}), heartbeat {}s old".format(project_id, existing.owner, existing.pid, int(age))
        reason = "took over a stale claim from {} (heartbeat {})".format(existing.owner, existing.heartbeat_utc)
    directory = claims_dir(config)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = claim_path(config, project_id)
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "project_id": project_id,
            "owner": owner,
            "pid": os.getpid(),
            "lane": lane,
            "heartbeat_utc": _stamp(moment),
            "note": note,
        },
        root=directory,
    )
    return True, reason


def release_claim(config: StudioConfig, project_id: str, *, owner: str) -> bool:
    existing = read_claim(config, project_id)
    if existing is None or existing.owner != owner:
        return False
    try:
        claim_path(config, project_id).unlink()
    except OSError:
        return False
    return True


def agent_session_activity(
    project_id: str,
    *,
    database_path: Optional[Path],
    within_seconds: int = DEFAULT_AGENT_SESSION_WINDOW_SECONDS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Read-only look at an interactive agent runtime's session log.

    The log is any SQLite file with a ``sessions`` table carrying ``id``,
    ``last_activity_at``, ``cwd``, ``title`` and ``last_activity_description``.
    Unconfigured or unavailable is a skip, never a block.
    """
    moment = now or datetime.now(timezone.utc)
    report: Dict[str, Any] = {"checked": False, "active": False, "reason": None, "sessions": []}
    if database_path is None:
        report["reason"] = "agent session check disabled (REEL_STUDIO_AGENT_DB is not set)"
        return report
    path = Path(database_path)
    if not path.is_file():
        report["reason"] = "agent session database is not present at {}".format(path)
        return report
    needle = "%{}%".format(project_id)
    connection = None
    try:
        connection = sqlite3.connect("file:{}?mode=ro".format(path), uri=True, timeout=1.0)
        rows: List[Any] = list(
            connection.execute(
                "SELECT id, last_activity_at, cwd FROM sessions"
                " WHERE last_activity_at IS NOT NULL AND (cwd LIKE ? OR title LIKE ? OR last_activity_description LIKE ?)"
                " ORDER BY last_activity_at DESC LIMIT 5",
                (needle, needle, needle),
            )
        )
    except (sqlite3.Error, OSError, ValueError) as exc:
        report["reason"] = "agent session check skipped: {}".format(exc)
        return report
    finally:
        if connection is not None:
            connection.close()
    report["checked"] = True
    cutoff = moment.timestamp() - within_seconds
    for session_id, last_activity, cwd in rows:
        try:
            seen = float(last_activity)
        except (TypeError, ValueError):
            continue
        entry = {"session_id": session_id, "last_activity_at": seen, "cwd": cwd, "recent": seen >= cutoff}
        report["sessions"].append(entry)
        if entry["recent"]:
            report["active"] = True
    if report["active"]:
        report["reason"] = "an agent session touched {} within the last {}s".format(project_id, within_seconds)
    return report


# Claim files are a low-level public API, independently guarded from the daemon.
acquire_claim = guarded_mutation("factory claim acquisition")(acquire_claim)
release_claim = guarded_mutation("factory claim release")(release_claim)
