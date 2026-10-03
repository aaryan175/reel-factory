from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from .errors import ContractError
from .paths import PathSafetyError, canonical_root

PathPart = Union[str, os.PathLike[str]]

# `authorized_footage_root` is the doctrine name; `footage_root` is the key `reelctl new`
# has always written. Either declares the same authorization; both must agree.
ROOT_KEYS = ("authorized_footage_root", "footage_root")

REFUSAL_SUFFIX = "research pools and reference downloads are study material, never sources"


class FootageAuthorizationError(ContractError):
    """Raised when media is sourced from outside the project's authorized footage root."""


def _resolved(path: PathPart) -> Path:
    """Resolve every symlink so an escaping link is compared by its real target."""
    return Path(os.path.realpath(os.path.expanduser(os.fspath(path))))


def _within(candidate: Path, root: Path) -> bool:
    return candidate != root and root in candidate.parents


def authorized_footage_root(project: Mapping[str, Any]) -> Path:
    """Return the resolved authorized footage root a project declares."""
    declared = {key: str(project.get(key) or "").strip() for key in ROOT_KEYS}
    declared = {key: value for key, value in declared.items() if value}
    if not declared:
        raise FootageAuthorizationError(
            "project.json declares no authorized_footage_root; reelctl refuses to source media without one — "
            + REFUSAL_SUFFIX
        )
    roots = {}
    for key, value in declared.items():
        try:
            roots[key] = _resolved(canonical_root(value))
        except PathSafetyError as exc:
            raise FootageAuthorizationError(f"declared {key} is unusable: {exc}") from exc
    if len(set(roots.values())) > 1:
        details = "; ".join(f"{key}={root}" for key, root in sorted(roots.items()))
        raise FootageAuthorizationError(f"project.json declares conflicting footage roots: {details}")
    return roots[next(key for key in ROOT_KEYS if key in roots)]


def authorize_source(path: PathPart, footage_root: PathPart, *, kind: str = "source media") -> Path:
    """Return the resolved path, refusing anything that lives outside the authorized root."""
    resolved = _resolved(path)
    root = _resolved(footage_root)
    if not _within(resolved, root):
        raise FootageAuthorizationError(
            f"{kind} is outside the authorized footage root: {resolved} is not beneath "
            f"the declared authorized_footage_root {root} — {REFUSAL_SUFFIX}"
        )
    return resolved


def authorize_media(
    path: PathPart,
    *,
    footage_root: PathPart,
    project_dir: Optional[PathPart] = None,
    kind: str = "source media",
) -> Path:
    """Authorize media that may also legitimately be one of the project's own locked artifacts."""
    candidate = Path(os.path.expanduser(os.fspath(path)))
    if not candidate.is_absolute() and project_dir is not None:
        candidate = Path(project_dir) / candidate
    resolved = _resolved(candidate)
    if project_dir is not None and _within(resolved, _resolved(project_dir)):
        return resolved
    return authorize_source(resolved, footage_root, kind=kind)


def authorize_asset_manifest(
    manifest: Mapping[str, Any],
    *,
    footage_root: Path,
    project_dir: Optional[PathPart] = None,
) -> None:
    """Refuse an assets manifest whose plates or effect frames come from an unauthorized pool.

    Font files are deliberately exempt: they are hash-locked typeface bytes, not footage.
    """
    layers = list(manifest.get("typography_layers", [])) + list(manifest.get("effect_layers", []))
    for layer in layers:
        identifier = layer.get("id", "<unnamed>")
        for key in ("plate_path", "sequence_pattern"):
            declared = str(layer.get(key) or "").strip()
            if declared:
                authorize_media(
                    declared,
                    footage_root=footage_root,
                    project_dir=project_dir,
                    kind=f"asset layer {identifier} {key}",
                )
