"""The shipped launchd plists.

These files are the deployment. Nothing else in the suite would notice if one of them
lost its PATH, pointed at the wrong port, or grew a `kickstart` of somebody else's
service, so the checks that matter live here:

* the explicit PATH, because a launchd job starts with ``/usr/bin:/bin`` and the whole
  engine shells out to ffmpeg;
* the port, because the app fails loudly on a bind collision rather than picking another;
* and the installer's blast radius, because no other service on the machine may be
  restarted by anything of ours.
"""

from __future__ import annotations

import plistlib
import re
import stat
from pathlib import Path
from typing import Dict

import pytest

from reelctl.studio.config import DEFAULT_PORT, DEFAULT_TOOL_PATH

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
APP_LABEL = "com.reelfactory.reel-studio"
DAEMON_LABEL = "com.reelfactory.reel-studio-daemon"
LABELS = (APP_LABEL, DAEMON_LABEL)


def _plist(label: str) -> Dict[str, object]:
    """Load a shipped plist rendered the way install.sh renders it (placeholders → paths)."""
    text = (DEPLOY / f"{label}.plist").read_text(encoding="utf-8")
    text = text.replace("__REEL_FACTORY_HOME__", str(Path.home() / "reel-production")).replace("__HOME__", str(Path.home()))
    return plistlib.loads(text.encode("utf-8"))


@pytest.mark.parametrize("label", LABELS)
def test_plist_parses_and_owns_its_label(label: str) -> None:
    assert _plist(label)["Label"] == label


@pytest.mark.parametrize("label", LABELS)
def test_plist_sets_the_path_launchd_does_not_give_it(label: str) -> None:
    env = _plist(label)["EnvironmentVariables"]
    assert isinstance(env, dict)
    entries = str(env["PATH"]).split(":")
    expected = [str(Path(item).expanduser()) for item in DEFAULT_TOOL_PATH]
    assert entries == expected, "the plist PATH must match the studio tool path, in order"


@pytest.mark.parametrize("label", LABELS)
def test_plist_runs_at_load_keeps_alive_and_throttles(label: str) -> None:
    data = _plist(label)
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    throttle = data["ThrottleInterval"]
    assert isinstance(throttle, int) and throttle > 0


@pytest.mark.parametrize("label", LABELS)
def test_plist_logs_under_the_studio_directory(label: str) -> None:
    data = _plist(label)
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert "/reel-production/.studio/logs/" in str(data[key])
    assert data["StandardOutPath"] != data["StandardErrorPath"]


@pytest.mark.parametrize("label", LABELS)
def test_plist_invokes_the_installed_reelctl_from_the_factory_root(label: str) -> None:
    data = _plist(label)
    argv = [str(item) for item in data["ProgramArguments"]]
    assert argv[0].endswith("/.local/bin/reelctl")
    assert argv[1:3] == ["--projects-root", str(data["WorkingDirectory"])]
    assert str(data["WorkingDirectory"]).endswith("/reel-production")


def test_app_serves_the_studio_port_on_loopback() -> None:
    argv = [str(item) for item in _plist(APP_LABEL)["ProgramArguments"]]
    assert "serve" in argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == str(DEFAULT_PORT)


def test_daemon_runs_the_orchestrator_loop_not_a_single_tick() -> None:
    argv = [str(item) for item in _plist(DAEMON_LABEL)["ProgramArguments"]]
    assert argv[-2:] == ["studio", "daemon"]
    assert "--once" not in argv


def test_the_two_services_do_not_share_a_log_file() -> None:
    app, daemon = _plist(APP_LABEL), _plist(DAEMON_LABEL)
    paths = {app["StandardOutPath"], app["StandardErrorPath"], daemon["StandardOutPath"], daemon["StandardErrorPath"]}
    assert len(paths) == 4


@pytest.mark.parametrize("script", ("install.sh", "uninstall.sh"))
def test_scripts_are_executable(script: str) -> None:
    assert (DEPLOY / script).stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("script", ("install.sh", "uninstall.sh"))
def test_scripts_name_only_our_own_services(script: str) -> None:
    """The blast-radius test. Every launchctl verb must be scoped to one of our labels."""
    body = (DEPLOY / script).read_text()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "launchctl" not in stripped:
            continue
        assert '"$DOMAIN/$label"' in stripped or '"$DOMAIN/$APP_LABEL"' in stripped or '"$DOMAIN/$DAEMON_LABEL"' in stripped or '"$DOMAIN" "$dst"' in stripped, (
            f"unscoped launchctl call in {script}: {stripped}"
        )


@pytest.mark.parametrize("script", ("install.sh", "uninstall.sh"))
def test_scripts_never_kickstart_anything(script: str) -> None:
    """`kickstart` restarts a service that already exists — which, for anything that is not
    ours, is the one action this project must never take."""
    body = (DEPLOY / script).read_text()
    assert not re.search(r"^\s*[^#]*launchctl\s+kickstart", body, re.MULTILINE)


def test_install_creates_the_log_directory_before_bootstrapping() -> None:
    body = (DEPLOY / "install.sh").read_text()
    assert body.index("mkdir -p") < body.index("launchctl bootstrap")
