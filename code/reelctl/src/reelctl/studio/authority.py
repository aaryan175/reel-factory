"""Fail-closed factory authority boundary for every factory mutation.

The snapshot is deliberately a tiny exact contract.  Only an explicit factory-wide OPEN
from the control file authorizes work; row-scoped recovery data, unknown fields, stale formats,
and all I/O/JSON/type failures are GLOBAL_HALT.  Reads never call this guard.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
import os
from typing import Any, Mapping, Optional

from ..errors import ReelctlError


def _default_authority_path() -> Path:
    """REEL_FACTORY_AUTHORITY, else $REEL_FACTORY_HOME/_control/authority.json (default ~/reel-production)."""
    explicit = os.environ.get("REEL_FACTORY_AUTHORITY")
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("REEL_FACTORY_HOME") or "~/reel-production"
    return Path(home).expanduser() / "_control" / "authority.json"


DEFAULT_AUTHORITY_PATH = _default_authority_path()
FACTORY_OPEN = "FACTORY_OPEN"
GLOBAL_HALT = "GLOBAL_HALT"
_REQUIRED_KEYS = frozenset({"schema_version", "authority", "factory_state", "mutations_allowed"})


@dataclass(frozen=True)
class AuthorityDecision:
    allowed: bool
    state: str
    reason: str


def authority_path() -> Path:
    return DEFAULT_AUTHORITY_PATH


def _halt(reason: str) -> AuthorityDecision:
    return AuthorityDecision(False, GLOBAL_HALT, reason)


def authority_decision(path: Optional[Path] = None) -> AuthorityDecision:
    target = Path(path) if path is not None else authority_path()
    try:
        if target.is_symlink() or not target.is_file():
            return _halt(f"Reel Pilot authority snapshot is missing or unsafe: {target}")
        payload: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _halt(f"Reel Pilot authority snapshot is unreadable or malformed: {type(exc).__name__}")
    if not isinstance(payload, Mapping):
        return _halt("Reel Pilot authority snapshot must be a JSON object")
    if frozenset(payload) != _REQUIRED_KEYS:
        return _halt("Reel Pilot authority snapshot does not match the exact factory contract")
    if payload.get("schema_version") != 1 or isinstance(payload.get("schema_version"), bool):
        return _halt("unsupported Reel Pilot authority schema")
    if payload.get("authority") != "REEL_PILOT":
        return _halt("snapshot is not issued by Reel Pilot")
    if payload.get("factory_state") != FACTORY_OPEN:
        return _halt(f"Reel Pilot factory state is {payload.get('factory_state')!r}")
    if payload.get("mutations_allowed") is not True:
        return _halt("Reel Pilot did not explicitly authorize factory mutations")
    return AuthorityDecision(True, FACTORY_OPEN, "Reel Pilot authorized factory mutations")


def require_factory_open(operation: str, *, authority_path: Optional[Path] = None) -> None:
    decision = authority_decision(authority_path)
    if not decision.allowed:
        raise ReelctlError(f"GLOBAL_HALT: blocked {operation}; {decision.reason}")


def guarded_mutation(operation: str):
    """Decorate a low-level mutation boundary without changing its signature."""
    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            require_factory_open(operation)
            return function(*args, **kwargs)
        return guarded
    return decorate
