from __future__ import annotations

import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# The media tests shell out to these by bare name. Non-interactive shells (agent
# sessions, launchd jobs) frequently run without Homebrew's bin on PATH, which turns
# every media test into a FileNotFoundError that reads like a product failure.
MEDIA_TOOLS = ("ffmpeg", "ffprobe", "exiftool")
FALLBACK_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _ensure_media_toolchain_on_path() -> None:
    for directory in FALLBACK_BIN_DIRS:
        if all(shutil.which(tool) for tool in MEDIA_TOOLS):
            return
        if any((Path(directory) / tool).is_file() for tool in MEDIA_TOOLS):
            os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"


_ensure_media_toolchain_on_path()


# --- the unit suite never reaches the network ------------------------------
#
# Not a hypothetical: during mutation testing, flipping `defer_analysis` to False sent a
# pytest run out to the network and downloaded a reference reel. A front-door
# regression should fail loudly in the suite, not quietly succeed by doing the outward
# thing. So the network is closed by default and a test that legitimately needs it must
# say so with `@pytest.mark.network` — which makes every such test greppable.
#
# Loopback stays open. The studio binds 127.0.0.1 and talking to yourself is not the
# network; blocking it would only push future tests toward mocking their own transport.

_NETWORK_FAMILIES = (socket.AF_INET, socket.AF_INET6)
_LOOPBACK_HOSTS = frozenset({"localhost", "localhost.localdomain", "127.0.0.1", "::1", "ip6-localhost"})
_STATE = {"blocked": False}

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection
_real_getaddrinfo = socket.getaddrinfo


class NetworkBlocked(RuntimeError):
    """A test reached for the network without declaring `@pytest.mark.network`."""


def network_is_blocked() -> bool:
    return _STATE["blocked"]


def _host_of(address: Any) -> str:
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    return str(address)


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _refuse(host: str) -> NetworkBlocked:
    return NetworkBlocked(
        f"the unit suite may not reach the network; refused a connection to {host!r}. "
        f"If this test genuinely needs the network, mark it @pytest.mark.network — and say why in the test."
    )


def _permitted(family: Any, address: Any) -> Tuple[bool, str]:
    host = _host_of(address)
    if family not in _NETWORK_FAMILIES:
        # AF_UNIX and friends are local IPC, not the network.
        return True, host
    return _is_loopback(host), host


def _guard_connect(self: socket.socket, address: Any) -> Any:
    allowed, host = _permitted(self.family, address)
    if not _STATE["blocked"] or allowed:
        return _real_connect(self, address)
    raise _refuse(host)


def _guard_connect_ex(self: socket.socket, address: Any) -> Any:
    allowed, host = _permitted(self.family, address)
    if not _STATE["blocked"] or allowed:
        return _real_connect_ex(self, address)
    raise _refuse(host)


def _guard_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    host = _host_of(address)
    if not _STATE["blocked"] or _is_loopback(host):
        return _real_create_connection(address, *args, **kwargs)
    raise _refuse(host)


def _guard_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    # Resolution is blocked as well as connection: a DNS lookup is itself a network round
    # trip, and leaving it open would leak the hostname being fetched.
    if not _STATE["blocked"] or host is None or _is_loopback(str(host)):
        return _real_getaddrinfo(host, *args, **kwargs)
    raise _refuse(str(host))


def pytest_configure(config: Any) -> None:
    config.addinivalue_line("markers", "network: test legitimately requires real network access")
    config.addinivalue_line("markers", "integration: test drives the real toolchain against real project media")


@pytest.fixture(autouse=True)
def _factory_authority_for_tests(tmp_path: Path, monkeypatch: Any) -> None:
    """Legacy mutation tests run only against an explicit temp Reel Pilot OPEN.

    Production's fixed snapshot is never created or touched. Guard-specific tests pass
    their own path and therefore still exercise missing/malformed fail-closed behavior.
    """
    from reelctl.studio import authority as authority_module

    authority = tmp_path / "test-control" / "authority.json"
    authority.parent.mkdir()
    authority.write_text(
        '{"schema_version":1,"authority":"REEL_PILOT","factory_state":"FACTORY_OPEN","mutations_allowed":true}',
        encoding="utf-8",
    )
    monkeypatch.setattr(authority_module, "DEFAULT_AUTHORITY_PATH", authority)


@pytest.fixture(autouse=True)
def _block_network(request: Any, monkeypatch: Any) -> None:
    if request.node.get_closest_marker("network"):
        return
    monkeypatch.setattr(socket.socket, "connect", _guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guard_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _guard_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _guard_getaddrinfo)
    monkeypatch.setitem(_STATE, "blocked", True)
