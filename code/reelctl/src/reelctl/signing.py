from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from .hashing import canonical_json
from .paths import PathSafetyError, canonical_root


class SignatureError(RuntimeError):
    pass


def _default_key_path() -> Path:
    configured = os.environ.get("REELCTL_AGENT_REVIEW_KEY")
    return Path(configured).expanduser().absolute() if configured else Path.home() / ".config/reelctl/agent-review.key"


def _read_key(path: Path) -> bytes:
    if path.is_symlink():
        raise SignatureError(f"review signing key is a symlink: {path}")
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise SignatureError(f"review signing key does not exist: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise SignatureError(f"review signing key must be a private regular file (0600): {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        value = os.read(descriptor, 4096)
    finally:
        os.close(descriptor)
    if len(value) < 32:
        raise SignatureError("review signing key is too short")
    return value


def _get_or_create_key(path: Optional[Path] = None) -> tuple[Path, bytes]:
    target = Path(path or _default_key_path()).expanduser().absolute()
    try:
        parent = canonical_root(target.parent, create=True)
    except PathSafetyError as exc:
        raise SignatureError(str(exc)) from exc
    target = parent / target.name
    if not target.exists():
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            key = secrets.token_bytes(32)
            os.write(descriptor, key)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return target, _read_key(target)


def _message(payload: Dict[str, Any], purpose: str) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return f"reelctl:{purpose}:".encode("utf-8") + canonical_json(unsigned).encode("utf-8")


def sign_payload(payload: Dict[str, Any], *, purpose: str, key_path: Optional[Path] = None) -> Dict[str, Any]:
    path, key = _get_or_create_key(key_path)
    unsigned = {key_name: value for key_name, value in payload.items() if key_name != "signature"}
    signature = hmac.new(key, _message(unsigned, purpose), hashlib.sha256).hexdigest()
    return {
        **unsigned,
        "signature": {
            "algorithm": "HMAC-SHA256",
            "purpose": purpose,
            "key_id": hashlib.sha256(key).hexdigest()[:16],
            "value": signature,
            "key_location": str(path),
        },
    }


def verify_payload(payload: Dict[str, Any], *, purpose: str, key_path: Optional[Path] = None) -> Dict[str, Any]:
    signature = payload.get("signature")
    if not isinstance(signature, dict):
        raise SignatureError("signed receipt lacks a signature object")
    target = Path(key_path or _default_key_path()).expanduser().absolute()
    key = _read_key(target)
    checks = {
        "algorithm": signature.get("algorithm") == "HMAC-SHA256",
        "purpose": signature.get("purpose") == purpose,
        "key_id": signature.get("key_id") == hashlib.sha256(key).hexdigest()[:16],
        "key_location": signature.get("key_location") == str(target),
    }
    expected = hmac.new(key, _message(payload, purpose), hashlib.sha256).hexdigest()
    checks["value"] = hmac.compare_digest(str(signature.get("value", "")), expected)
    if not all(checks.values()):
        raise SignatureError(f"receipt signature verification failed: {[name for name, passed in checks.items() if not passed]}")
    return {"status": "PASS", "purpose": purpose, "key_id": signature["key_id"]}
