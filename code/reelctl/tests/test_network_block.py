"""The unit suite must never reach the network.

This exists because it already happened. During mutation testing, flipping
``defer_analysis`` to ``False`` sent a unit-test run out to the network and downloaded a
real reference reel — an outward action from inside pytest, triggered by a one-word change
to a front-door flag. The block below is the machine
answer; ``test_the_remote_download_path_is_refused_inside_the_suite`` is the specific one.
"""

from __future__ import annotations

import socket
from argparse import Namespace
from pathlib import Path

import pytest
from conftest import NetworkBlocked, network_is_blocked

from reelctl.errors import ReelctlError

REMOTE_REFERENCE = "https://video.example/reel/ref-19/"


# --- the block itself ------------------------------------------------------


def test_the_block_is_on_by_default() -> None:
    assert network_is_blocked() is True


def test_resolving_a_public_hostname_is_refused() -> None:
    with pytest.raises(NetworkBlocked) as caught:
        socket.getaddrinfo("www.example.com", 443)

    assert "www.example.com" in str(caught.value)
    assert "network" in str(caught.value)


def test_opening_a_connection_to_a_public_address_is_refused() -> None:
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


def test_a_raw_socket_connect_is_refused() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlocked):
            sock.connect(("93.184.216.34", 80))
        with pytest.raises(NetworkBlocked):
            sock.connect_ex(("93.184.216.34", 80))
    finally:
        sock.close()


# --- what must keep working ------------------------------------------------


def test_loopback_still_works_end_to_end() -> None:
    # The studio binds 127.0.0.1 and a future test may want a real local server. Blocking
    # the network must not block the machine talking to itself.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=1)
        accepted, _ = server.accept()
        try:
            client.sendall(b"loopback")
            assert accepted.recv(8) == b"loopback"
        finally:
            client.close()
            accepted.close()
    finally:
        server.close()


def test_resolving_localhost_still_works() -> None:
    assert socket.getaddrinfo("127.0.0.1", 7335, socket.AF_INET)


# --- the opt-in ------------------------------------------------------------


@pytest.mark.network
def test_the_network_marker_opts_back_in() -> None:
    # Asserted through the flag rather than by making a real request: proving the opt-in
    # works must not itself be a test that reaches the network.
    assert network_is_blocked() is False
    assert socket.getaddrinfo("127.0.0.1", 7335, socket.AF_INET)


def test_the_block_is_restored_after_a_marked_test() -> None:
    assert network_is_blocked() is True


# --- the specific regression ----------------------------------------------


def test_the_remote_download_path_is_refused_inside_the_suite(tmp_path: Path) -> None:
    from reelctl.cli import _copy_reference

    with pytest.raises(ReelctlError) as caught:
        _copy_reference(REMOTE_REFERENCE, tmp_path / "reference")

    assert "Do not guess or substitute" in str(caught.value)
    assert not list((tmp_path / "reference").glob("reference-source*"))


def test_a_project_created_without_deferral_cannot_reach_the_network(tmp_path: Path) -> None:
    # The exact mutation that leaked: intake creating a project with defer_analysis=False.
    from reelctl.cli import command_new

    footage = tmp_path / "footage"
    footage.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    args = Namespace(
        projects_root=projects,
        project_id="reel-ref-19-v1",
        reference=REMOTE_REFERENCE,
        footage=str(footage),
        mode="original-montage",
        defer_analysis=False,
    )

    with pytest.raises(ReelctlError):
        command_new(args)

    assert not (tmp_path / "projects/reel-ref-19-v1/reference/reference-source.mp4").exists()
