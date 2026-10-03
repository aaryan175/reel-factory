"""Lock-free project reads.

The board is the operator's most-refreshed surface and the daemon holds a project's
flock through renders that run for minutes. Taking that same lock to *read* meant a
refresh could fail on a busy project — so nothing here takes a lock. Every read goes
straight to ``state.json`` plus receipt verification, and reelctl's atomic writes mean a
concurrent mutation is seen either wholly before or wholly after, never half-written.

Locks remain mandatory for mutation; that is unchanged and enforced in ``web.py``.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..cli import command_status
from ..errors import ReelctlError
from ..paths import canonical_root
from ..state import StateError
from .opencalls import open_calls_map
from .registry import registry_report
from .status import FAILED, project_layers


def project_ids(projects_root: Path) -> List[str]:
    root = canonical_root(projects_root, create=True)
    found: List[str] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if directory.is_symlink() or not directory.is_dir():
            continue
        if directory.name.startswith("."):
            # Dot-prefixed dirs are never projects. A failed-init scratch copy with an
            # intact project.json once carried a live project's id here — /api/projects
            # died on the dot-name and the daemon crash-looped on the duplicate
            # status_cache primary key.
            continue
        if not (directory / "project.json").is_file() or not (directory / "state.json").is_file():
            continue
        found.append(directory.name)
    return found


def project_summary(projects_root: Path, project_id: str) -> Dict[str, Any]:
    root = canonical_root(projects_root, create=True)
    return command_status(Namespace(projects_root=root, project_id=project_id))


def project_summaries(projects_root: Path) -> List[Dict[str, Any]]:
    root = canonical_root(projects_root, create=True)
    summaries: List[Dict[str, Any]] = []
    for project_id in project_ids(root):
        try:
            summaries.append(project_summary(root, project_id))
        except (ReelctlError, StateError, ValueError) as error:
            # ContractValidationError and IdentifierError are ValueErrors, not
            # ReelctlErrors — one contract-breaking project.json took the whole
            # listing to a 500. Degrade like the board: a FAILED row,
            # never a gap and never a dead endpoint.
            summaries.append(
                {
                    "project_id": project_id,
                    "status": "FAIL",
                    "error": str(error),
                    "error_type": type(error).__name__,
                }
            )
    return summaries


def board_row(projects_root: Path, project_id: str, *, open_calls: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    root = canonical_root(projects_root, create=True)
    directory = root / project_id
    model = project_layers(directory, open_calls=open_calls)
    row = {
        "project_id": model["project_id"],
        "project_dir": str(directory),
        "mode": None,
        "next_stage": None,
        "updated_at_utc": None,
        "headline": model["headline"],
        "reason": model["reason"],
        "blocked_by_calls": model["blocked_by_calls"],
        "layers": model["layers"],
    }
    try:
        summary = project_summary(root, project_id)
    except (ReelctlError, StateError, OSError, ValueError) as exc:
        reason = row["reason"] or f"project summary is unavailable: {exc}"
        return {**row, "headline": FAILED, "reason": reason}
    return {
        **row,
        "mode": summary.get("mode"),
        "next_stage": summary.get("next_stage"),
        "updated_at_utc": summary.get("updated_at_utc"),
    }


def board_model(
    projects_root: Path,
    *,
    open_calls: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None,
    registry: Optional[Path] = None,
) -> Dict[str, Any]:
    """The pipeline board, projected from disk only. Never reads ``studio.db``.

    Open calls come from ``opencalls.open_calls_map`` — the same assembly
    ``daemon._schedule`` gates on, covering both the registry and each project's intake
    record. Reading the registry alone missed a studio-born reel's mode call entirely, and
    a board that misses a gate tells the operator the machine is about to work on
    something it will not touch.
    """
    root = canonical_root(projects_root, create=True)
    report = registry_report(registry if registry is not None else root)
    identifiers = project_ids(root)
    calls = open_calls if open_calls is not None else open_calls_map(report, root, identifiers)
    return {
        "registry": {"path": report.get("path"), "status": report.get("status"), "error": report.get("error")},
        "projects": [board_row(root, project_id, open_calls=calls.get(project_id, ())) for project_id in identifiers],
    }
