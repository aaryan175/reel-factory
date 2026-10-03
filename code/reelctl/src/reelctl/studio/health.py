"""Machine state for the studio health strip.

``status`` answers one question: can work proceed on this box. Its inputs are the
preflight gates the daemon refuses to start work under — storage mount, free space,
required binaries. Daemon liveness is reported alongside them but does not gate the
status: the read-only surfaces (board, review room, receipts) are fully usable with the
orchestrator stopped, and a degraded flag there would say nothing about the machine.

Every failure is reported as a sentence naming the path or binary. Nothing is hidden
behind a boolean.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .config import REPORTED_BINARIES, REQUIRED_BINARIES, StudioConfig


def _volume_report(path: Path, *, label: str, min_free_bytes: int) -> Tuple[Dict[str, Any], List[str]]:
    report: Dict[str, Any] = {
        "path": str(path),
        "mounted": False,
        "writable": False,
        "free_bytes": None,
        "total_bytes": None,
        "min_free_bytes": min_free_bytes,
        "error": None,
    }
    reasons: List[str] = []
    try:
        mounted = path.is_dir()
    except OSError as exc:
        report["error"] = str(exc)
        reasons.append(f"{label} {path} could not be inspected: {exc}")
        return report, reasons
    report["mounted"] = mounted
    if not mounted:
        reasons.append(f"{label} {path} is not mounted")
        return report, reasons
    report["writable"] = os.access(path, os.W_OK)
    if not report["writable"]:
        reasons.append(f"{label} {path} is not writable")
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        report["error"] = str(exc)
        reasons.append(f"{label} {path} free space could not be read: {exc}")
        return report, reasons
    report["free_bytes"] = usage.free
    report["total_bytes"] = usage.total
    if usage.free < min_free_bytes:
        reasons.append(f"{label} {path} has {usage.free} bytes free, below the {min_free_bytes} byte floor")
    return report, reasons


def _binary_report(config: StudioConfig) -> Tuple[Dict[str, Any], List[str]]:
    binaries: Dict[str, Any] = {}
    reasons: List[str] = []
    for name in REPORTED_BINARIES:
        resolved = config.which(name)
        required = name in REQUIRED_BINARIES
        binaries[name] = {"present": resolved is not None, "path": resolved, "required": required}
        if required and resolved is None:
            reasons.append(f"required binary {name} is not on the studio tool path {os.pathsep.join(config.tool_path)}")
    return binaries, reasons


def _daemon_report(config: StudioConfig) -> Dict[str, Any]:
    path = config.daemon_heartbeat_path
    report: Dict[str, Any] = {
        "alive": False,
        "last_tick_utc": None,
        "age_seconds": None,
        "pid": None,
        "heartbeat_path": str(path),
        "max_age_seconds": config.heartbeat_max_age_seconds,
        "reason": None,
    }
    if not path.is_file():
        report["reason"] = f"no heartbeat recorded at {path}"
        return report
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(str(beat["last_tick_utc"]).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report["reason"] = f"heartbeat at {path} is unreadable: {exc}"
        return report
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    report["last_tick_utc"] = str(beat["last_tick_utc"])
    report["age_seconds"] = round(age, 3)
    report["pid"] = beat.get("pid")
    if age > config.heartbeat_max_age_seconds:
        report["reason"] = (
            f"last tick was {round(age)}s ago, beyond the {config.heartbeat_max_age_seconds}s liveness window"
        )
        return report
    report["alive"] = True
    return report


def health_report(config: StudioConfig) -> Dict[str, Any]:
    storage, storage_reasons = _volume_report(
        config.storage_root, label="storage root", min_free_bytes=config.min_free_bytes_storage
    )
    projects, projects_reasons = _volume_report(
        config.projects_root, label="projects root", min_free_bytes=config.min_free_bytes_internal
    )
    binaries, binary_reasons = _binary_report(config)
    reasons = storage_reasons + projects_reasons + binary_reasons
    return {
        "status": "DEGRADED" if reasons else "PASS",
        "degraded_reasons": reasons,
        "host": config.host,
        "port": config.port,
        "mounts": {"storage": storage, "projects": projects},
        "binaries": binaries,
        "daemon": _daemon_report(config),
        "capacity": {"max_judgment_jobs": config.max_judgment_jobs},
        "tool_path": list(config.tool_path),
        "library_path": str(config.library_path),
        "workbench_root": str(config.workbench_root),
    }
