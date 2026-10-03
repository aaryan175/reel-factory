"""
Auth — multi-user login for Reel Deck.

Credentials live in config.json next to the app (scrypt hash, never the
password) as a `users` list, each entry carrying a role:

    OPERATOR — full authority; receipts read as operator chat input.
    EDITOR   — may look and may write, but verdicts are advisory only
               (see server.receipts: editor verdicts carry an AUTHORITY line).

Configs written by the single-user era (top-level username/salt/hash) are
migrated to the list shape on first load and treated as one OPERATOR.

Sessions are HMAC-signed cookies (itsdangerous TimestampSigner) carrying
`username|role|nonce`, 30-day life. Login failures are rate-limited in
memory. The app only listens on loopback and should sit behind a private
reverse proxy / VPN, so the login is a second wall, not the first. Do not
expose the Deck to the public internet without strong auth in front of it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from . import settings

CONFIG_PATH = settings.DECK_HOME / "config.json"
SESSION_COOKIE = "deck_session"
SESSION_MAX_AGE = 30 * 24 * 3600

ROLE_OPERATOR = "OPERATOR"
ROLE_EDITOR = "EDITOR"
ROLES = (ROLE_OPERATOR, ROLE_EDITOR)

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1

_fail_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
FAIL_WINDOW = 600.0
FAIL_LIMIT = 8


def _read() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(config: dict[str, Any]) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    CONFIG_PATH.chmod(0o600)


def _migrate(config: dict[str, Any]) -> bool:
    """Fold a single-user config into the users list. True if it changed."""
    if isinstance(config.get("users"), list):
        return False
    if config.get("username") and config.get("hash") and config.get("salt"):
        config["users"] = [{
            "username": config["username"],
            "salt": config["salt"],
            "hash": config["hash"],
            "role": ROLE_OPERATOR,
        }]
        for key in ("username", "salt", "hash"):
            config.pop(key, None)
        return True
    config["users"] = []
    return False


def _load() -> dict[str, Any]:
    """Config in the current shape. Rewrites a legacy config on first read."""
    config = _read()
    if _migrate(config) and config.get("secret"):
        _save(config)
    return config


def _users(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [u for u in config.get("users") or [] if isinstance(u, dict) and u.get("username")]


def _find(config: dict[str, Any], username: str) -> dict[str, Any] | None:
    wanted = username.strip()
    return next((u for u in _users(config) if u["username"] == wanted), None)


def _role_of(user: dict[str, Any]) -> str:
    role = str(user.get("role", "")).upper()
    return role if role in ROLES else ROLE_OPERATOR


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024)


def set_credentials(username: str, password: str, role: str = ROLE_OPERATOR) -> None:
    """Upsert one user, leaving every other user in place."""
    role = role.upper()
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    config = _read()
    _migrate(config)
    salt = secrets.token_bytes(16)
    entry = {
        "username": username.strip(),
        "salt": salt.hex(),
        "hash": _hash(password, salt).hex(),
        "role": role,
    }
    users = _users(config)
    for i, existing in enumerate(users):
        if existing["username"] == entry["username"]:
            users[i] = entry
            break
    else:
        users.append(entry)
    config["users"] = users
    config.setdefault("secret", secrets.token_hex(32))
    _save(config)


def list_users() -> list[dict[str, str]]:
    return [{"username": u["username"], "role": _role_of(u)} for u in _users(_load())]


def credentials_set() -> bool:
    return any(u.get("hash") and u.get("salt") for u in _users(_load()))


def _signer() -> TimestampSigner:
    config = _load()
    secret = config.get("secret")
    if not secret:
        config["secret"] = secrets.token_hex(32)
        _save(config)
        secret = config["secret"]
    return TimestampSigner(secret, salt="deck-session")


def check_login(username: str, password: str, client: str) -> tuple[dict[str, str] | None, str]:
    """Returns (user, message). Rate-limited per client address."""
    now = time.monotonic()
    with _fail_lock:
        recent = [t for t in _failures.get(client, []) if now - t < FAIL_WINDOW]
        _failures[client] = recent
        if len(recent) >= FAIL_LIMIT:
            return None, "Too many attempts. Wait a few minutes and try again."

    matched: dict[str, str] | None = None
    for user in _users(_load()):
        if not (user.get("hash") and user.get("salt")):
            continue
        try:
            expected = bytes.fromhex(user["hash"])
            got = _hash(password, bytes.fromhex(user["salt"]))
        except (ValueError, TypeError):
            continue
        if (hmac.compare_digest(expected, got)
                and hmac.compare_digest(user["username"].encode(), username.strip().encode())):
            matched = {"username": user["username"], "role": _role_of(user)}
            break

    if matched is None:
        with _fail_lock:
            _failures.setdefault(client, []).append(now)
        return None, "Wrong login. Check the details and try again."
    return matched, ""


def issue_session(user: dict[str, str]) -> str:
    payload = f"{user['username']}|{user['role']}|{secrets.token_hex(16)}"
    return _signer().sign(payload).decode("utf-8")


def session_user(cookie: str | None) -> dict[str, str] | None:
    """The signed-in user, or None. The account must still exist in config."""
    if not cookie:
        return None
    try:
        raw = _signer().unsign(cookie, max_age=SESSION_MAX_AGE).decode("utf-8")
    except (BadSignature, SignatureExpired, UnicodeDecodeError):
        return None
    username, _, rest = raw.partition("|")
    signed_role, _, _ = rest.partition("|")
    if not username or signed_role.upper() not in ROLES:
        return None
    account = _find(_load(), username)
    if account is None:
        return None
    # Role comes from config, so a role change lands without a re-login.
    return {"username": username, "role": _role_of(account)}


def session_valid(cookie: str | None) -> bool:
    return session_user(cookie) is not None
