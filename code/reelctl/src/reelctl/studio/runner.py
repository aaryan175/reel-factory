"""The execution boundary: deterministic stages as ``reelctl`` subprocesses (§6.2).

In-process calls were rejected for three reasons the architecture spells out: an
ffmpeg-heavy render must not be able to wedge the daemon's loop, a crashed render must
leave the daemon alive to record the failure, and the subprocess boundary is the only
place PATH, cwd and a wall-clock timeout can actually be enforced.

The daemon does **not** hold the project flock while a command runs. ``reelctl.cli.main``
takes that lock itself around exactly the mutation, which is §9.2's "lock granularity is
one mutation, not one job" made literal — a twenty-minute job never holds a lock the
operator's own ``reelctl status`` needs.

This module also owns the seam Task 7 plugs into. ``JudgmentAdapter`` is the interface;
``UnavailableJudgmentAdapter`` is what the daemon uses until then, and it withholds by
name rather than pretending a judgment stage failed.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .config import StudioConfig
from .stages import JobSpec

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
BUSY = "BUSY"
TIMEOUT = "TIMEOUT"
UNAVAILABLE = "UNAVAILABLE"
WITHHELD = "WITHHELD"

#: Environment keys forwarded to the subprocess. PATH is replaced, never inherited.
FORWARDED_ENVIRONMENT = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "LOCALAPPDATA")


@dataclass(frozen=True)
class StageResult:
    status: str
    argv: Tuple[str, ...] = ()
    returncode: Optional[int] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


def _parse(text: str) -> Optional[Dict[str, Any]]:
    stripped = (text or "").strip()
    if not stripped:
        return None
    for candidate in (stripped, stripped.splitlines()[-1]):
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


class StageRunner:
    def __init__(
        self,
        config: StudioConfig,
        *,
        timeout_seconds: Optional[float] = None,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else config.stage_timeout_seconds
        self._run = run

    @property
    def executable(self) -> Optional[str]:
        return self.config.which("reelctl")

    def environment(self) -> Dict[str, str]:
        env = {"PATH": os.pathsep.join(self.config.tool_path)}
        for key in FORWARDED_ENVIRONMENT:
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def command(self, argv: Sequence[str]) -> list:
        executable = self.executable
        if executable is None:
            raise FileNotFoundError("reelctl is not on the studio tool path")
        return [executable, "--projects-root", str(self.config.projects_root), *[str(item) for item in argv]]

    def run(self, argv: Sequence[str], *, timeout_seconds: Optional[float] = None) -> StageResult:
        argv = tuple(str(item) for item in argv)
        if self.executable is None:
            return StageResult(
                status=UNAVAILABLE,
                argv=argv,
                reason="reelctl is not on the studio tool path {}".format(os.pathsep.join(self.config.tool_path)),
            )
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        started = time.monotonic()
        try:
            completed = self._run(
                self.command(argv),
                cwd=str(self.config.projects_root),
                env=self.environment(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return StageResult(
                status=TIMEOUT,
                argv=argv,
                reason="reelctl {} exceeded its {}s wall-clock timeout and was killed".format(" ".join(argv), timeout),
                duration_seconds=time.monotonic() - started,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return StageResult(
                status=UNAVAILABLE,
                argv=argv,
                reason="reelctl could not be executed: {}".format(exc),
                duration_seconds=time.monotonic() - started,
            )
        return self._classify(argv, completed, time.monotonic() - started)

    def _classify(self, argv: Tuple[str, ...], completed: Any, duration: float) -> StageResult:
        stdout = getattr(completed, "stdout", "") or ""
        stderr = getattr(completed, "stderr", "") or ""
        returncode = int(getattr(completed, "returncode", 0) or 0)
        payload = _parse(stdout) or {}
        common = {"argv": argv, "returncode": returncode, "stdout": stdout, "stderr": stderr, "duration_seconds": duration}
        if returncode != 0:
            error = _parse(stderr) or {}
            reason = str(error.get("error") or stderr.strip() or "reelctl exited {}".format(returncode))
            status = BUSY if error.get("error_type") == "ProjectBusyError" else FAIL
            return StageResult(status=status, payload=error, reason=reason, **common)
        if not payload:
            return StageResult(
                status=FAIL,
                payload={},
                reason="reelctl exited 0 but printed no parseable JSON; refusing to read that as a pass",
                **common
            )
        declared = str(payload.get("status", PASS)).upper()
        if declared == BLOCKED:
            return StageResult(status=BLOCKED, payload=payload, reason=payload.get("reason"), **common)
        if declared == FAIL:
            return StageResult(status=FAIL, payload=payload, reason=payload.get("reason") or payload.get("error"), **common)
        return StageResult(status=PASS, payload=payload, reason=None, **common)


class JudgmentAdapter:
    """The Task 7 seam.

    An adapter receives the job and the project directory, invokes a headless worker
    against the frozen contract, validates whatever the worker wrote, and reports. It must
    never advance a stage itself — stage advancement is always a subsequent ``reelctl``
    call made by the daemon after validation (§6.3).
    """

    def run(self, job: JobSpec, project_dir: Path) -> StageResult:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass(frozen=True)
class UnavailableJudgmentAdapter(JudgmentAdapter):
    """The default: withhold by name. A judgment stage is unproven, not failed."""

    reason: str = "no judgment adapter is installed; headless judgment lands in Task 7"
    retry_after_seconds: int = 3600

    def run(self, job: JobSpec, project_dir: Path) -> StageResult:
        wanted = job.reason or job.note or "this stage needs a judgment worker"
        return StageResult(status=WITHHELD, reason="{} withheld — {}. {}".format(job.stage, wanted, self.reason))
