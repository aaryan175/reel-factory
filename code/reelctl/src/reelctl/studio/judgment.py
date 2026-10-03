"""Judgment stages as headless ``claude -p`` subprocesses.

Three kinds of judgment reach this module — the bootstrap/forensic pass that produces a
blueprint, the scoring passes that author a draft (feasibility, selection, assets), and the
creative QC watch — and all three obey the same four rules:

* **Artifacts only.** The worker's prose is written to a log and has zero authority. What
  the adapter believes is the file the worker left on disk, validated against a schema.
* **The worker cannot advance state.** Where a stage needs a ``reelctl`` call, *this*
  module makes it, after validation, through the same PATH-explicit runner the daemon uses
  for deterministic stages. The worker is denied the tools that could do it itself.
* **Frozen contract.** The onboarding prompt's sha256 goes into every receipt, so a
  changed prompt is visibly a different contract and old receipts stay comparable only to
  their own.
* **Capacity is not failure.** A session usage-limit wall parks the job with a retry-after and
  is remembered, so the next judgment job does not spend a subprocess rediscovering it. A
  false ``FAIL`` here would pollute the negative-fixture ledger, which is why this is the
  one classification the module is careful about.

Only flags verified present in this ``claude`` build (2.1.232) are used; in particular
there is no ``--max-turns``, so a run is bounded by a wall-clock subprocess timeout.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .. import __version__
from ..contracts import ContractValidationError, schema_sha256, validate_contract
from ..errors import ReelctlError
from ..hashing import atomic_write_json, sha256_file
from .config import StudioConfig
from .runner import (
    FAIL,
    PASS,
    TIMEOUT,
    UNAVAILABLE,
    WITHHELD,
    JudgmentAdapter,
    StageResult,
    StageRunner,
    UnavailableJudgmentAdapter,
)
from .stages import JobSpec, plan_for

#: The classification the architecture reserves for "the box ran out of headless capacity".
CAPACITY = "capacity"

#: The durable brain pack the onboarding contract requires, in the order it is written.
#: Checkpointing is per file: a worker killed at 07 resumes at 07, not at 00.
BRAIN_PACK: Tuple[str, ...] = (
    "00_PROJECT_BRAIN.md",
    "01_REFERENCE_CONTRACT.json",
    "02_SHOT_BLUEPRINT.json",
    "03_FOOTAGE_REGISTRY.json",
    "04_FEASIBILITY_MATRIX.md",
    "05_SELECTION_SHORTLIST.md",
    "06_MISMATCH_LEDGER.md",
    "07_CALL_PROMPTS.md",
    "08_DECISION_LOG.md",
    "09_RESUME_STATE.json",
    "10_EVIDENCE_MANIFEST.json",
)

BLUEPRINT_INPUT_RELATIVE = "brain/BLUEPRINT_INPUT.json"
#: Derived by the daemon from the worker's artifact; `blueprint lock` reads a bare mapping.
BLUEPRINT_OBSERVATIONS_RELATIVE = "brain/BLUEPRINT_OBSERVATIONS.json"
VISUAL_WATCH_RELATIVE = "review/agent-visual-watch-{revision}.json"

#: Flags §6.3 verified exist in this CLI build. Anything outside this set is an invention.
VERIFIED_CLI_FLAGS: Tuple[str, ...] = (
    "-p",
    "--output-format",
    "--model",
    "--fallback-model",
    "--permission-mode",
    "--allowedTools",
    "--disallowedTools",
    "--system-prompt",
    "--append-system-prompt",
    "--add-dir",
    "--settings",
    "--session-id",
    "--strict-mcp-config",
    "--bare",
)

#: Everything that could advance a stage, reach the network, or act outward. Passed as one
#: comma-joined argument: the option is variadic, so separate values would swallow the brief.
DISALLOWED_TOOLS: Tuple[str, ...] = (
    "WebFetch",
    "WebSearch",
    "Bash(reelctl:*)",
    "Bash(reelctl *)",
    "Bash(rclone:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(git push:*)",
    "Bash(launchctl:*)",
    "Bash(osascript:*)",
)

#: The packaged schema each draft must satisfy. The *paths* are not restated here — they
#: come from the daemon's own stage table, so the two cannot drift apart.
DRAFT_SCHEMAS: Dict[str, str] = {
    "edit/feasibility.json": "feasibility",
    "edit/selection.json": "selection",
    "assets/assets.json": "assets",
}

#: How long a park lasts when the worker names no reset time at all.
DEFAULT_PARK_SECONDS = 3600

_LIMIT_MARKERS: Tuple[str, ...] = (
    "usage limit reached",
    "session limit reached",
    "session limit resets",
    "rate limit exceeded",
    "rate_limit_error",
    "too many requests",
    "status 429",
    "http 429",
    "error 429",
    "quota exceeded",
    "out of credits",
    "credit balance is too low",
    "insufficient credit",
)
_EPOCH_PATTERN = re.compile(r"usage limit reached\|(\d{9,13})", re.IGNORECASE)
_RESET_PATTERN = re.compile(r"reset[s]?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)


class ContractUnavailable(ReelctlError):
    """The frozen contract file is not readable — a contract problem, not a work problem."""


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# --- the frozen contract ---------------------------------------------------


@dataclass(frozen=True)
class FrozenContract:
    path: Path
    sha256: str
    text: str

    @property
    def bytes(self) -> int:
        return len(self.text.encode("utf-8"))


def freeze_contract(path: Path) -> FrozenContract:
    """Read the onboarding contract and pin it by hash for this job."""
    path = Path(path)
    if not path.is_file():
        raise ContractUnavailable("the frozen judgment contract is missing at {}".format(path))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractUnavailable("the frozen judgment contract at {} could not be read: {}".format(path, exc)) from exc
    if not text.strip():
        raise ContractUnavailable("the frozen judgment contract at {} is empty".format(path))
    return FrozenContract(path=path, sha256=sha256_file(path), text=text)


# --- checkpointing ---------------------------------------------------------


def checkpoint(project_dir: Path) -> Dict[str, Any]:
    """Which brain-pack files already exist, and therefore where a resume starts.

    Phase 2's lesson: a worker killed by the session reset must not restart at 00.
    """
    brain = Path(project_dir) / "brain"
    present: List[Dict[str, Any]] = []
    missing: List[str] = []
    for name in BRAIN_PACK:
        path = brain / name
        try:
            written = path.is_file() and path.stat().st_size > 0
        except OSError:
            written = False
        if written:
            present.append({"file": name, "sha256": sha256_file(path), "bytes": path.stat().st_size})
        else:
            missing.append(name)
    return {"present": present, "missing": missing, "resume_at": missing[0] if missing else None}


# --- the capacity ledger ---------------------------------------------------


def capacity_signal(
    text: str,
    *,
    now: Optional[datetime] = None,
    max_park_seconds: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Classify worker output as a capacity wall, and say when work can resume.

    Deliberately narrow: the phrases below are the ones the CLI actually prints when a
    session or rate limit is hit. A bare "429" or the word "limit" is not enough — worker
    prose talks about limits all the time, and a false capacity park would stall a reel.
    """
    moment = now or datetime.now(timezone.utc)
    haystack = (text or "").lower()
    named = any(marker in haystack for marker in _LIMIT_MARKERS)
    # "5-hour limit reached ∙ resets 5pm" names no product; a limit *plus* a reset clock is
    # still unambiguously the wall, and prose about editorial limits carries no reset time.
    timed = "limit" in haystack and _RESET_PATTERN.search(text or "") is not None
    if not (named or timed):
        return None
    retry_after = _reset_moment(text, moment)
    if max_park_seconds is not None:
        horizon = moment + timedelta(seconds=max_park_seconds)
        if retry_after > horizon:
            retry_after = horizon
    return {
        "class": CAPACITY,
        "retry_after_utc": _stamp(retry_after),
        "reason": "headless capacity is exhausted; judgment resumes at {}".format(_stamp(retry_after)),
        "observed": _first_marker_line(text),
    }


def _reset_moment(text: str, now: datetime) -> datetime:
    epoch = _EPOCH_PATTERN.search(text or "")
    if epoch:
        value = int(epoch.group(1))
        if value > 10**11:  # milliseconds
            value = value // 1000
        moment = datetime.fromtimestamp(value, tz=timezone.utc)
        if moment > now:
            return moment
    clock = _RESET_PATTERN.search(text or "")
    if clock:
        hour = int(clock.group(1)) % 12
        minute = int(clock.group(2) or 0)
        if clock.group(3).lower() == "pm":
            hour += 12
        local = now.astimezone()
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(timezone.utc)
    return now + timedelta(seconds=DEFAULT_PARK_SECONDS)


def _first_marker_line(text: str) -> str:
    for line in (text or "").splitlines():
        if any(marker in line.lower() for marker in _LIMIT_MARKERS):
            return line.strip()[:400]
    return (text or "").strip()[:400]


def park_capacity(config: StudioConfig, *, until: datetime, reason: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Remember the wall so the next judgment job does not spend a subprocess finding it."""
    payload = {
        "schema_version": 1,
        "class": CAPACITY,
        "observed_at_utc": _stamp(now or datetime.now(timezone.utc)),
        "retry_after_utc": _stamp(until),
        "reason": reason,
    }
    config.studio_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(config.judgment_capacity_path, payload, root=config.studio_dir)
    return payload


def capacity_park(config: StudioConfig, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The live park, or ``None`` when there is none or it has expired."""
    path = config.judgment_capacity_path
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    until = _parse_stamp(payload.get("retry_after_utc"))
    if until is None or until <= (now or datetime.now(timezone.utc)):
        return None
    return payload


def clear_capacity(config: StudioConfig) -> None:
    try:
        config.judgment_capacity_path.unlink()
    except (FileNotFoundError, OSError):
        pass


# --- what each judgment stage owes -----------------------------------------


@dataclass(frozen=True)
class ArtifactSpec:
    relative: str
    schema: Optional[str] = None
    #: ``json`` artifacts are parsed and schema-checked; ``text`` ones only have to exist
    #: with content — the brain pack is prose the operator reads, not a machine contract.
    kind: str = "json"


@dataclass(frozen=True)
class JudgmentStage:
    stage: str
    phase: str
    write_policy: str
    task: str
    artifacts: Tuple[ArtifactSpec, ...]
    checkpointed: bool = False


_BRAIN_PACK_ARTIFACTS = tuple(ArtifactSpec("brain/{}".format(name), None, "text") for name in BRAIN_PACK)


def _draft_artifacts(stage: str) -> Tuple[ArtifactSpec, ...]:
    """The draft this stage's deterministic command reads, as ``stages.py`` declares it."""
    return tuple(ArtifactSpec(relative, DRAFT_SCHEMAS.get(relative)) for relative in plan_for(stage).drafts)

JUDGMENT_STAGES: Dict[str, JudgmentStage] = {
    "BLUEPRINT_LOCKED": JudgmentStage(
        stage="BLUEPRINT_LOCKED",
        phase="BOOTSTRAP",
        write_policy="BRAIN_PACK_ONLY",
        task=(
            "Run the full forensic bootstrap for this project and write the durable brain pack. "
            "Watch the reference at normal speed, then derive every picture-state boundary, every hard cut, "
            "and one independent visible-state observation per ordered picture state. Boundaries and "
            "observations are what the engine cannot invent."
        ),
        artifacts=_BRAIN_PACK_ARTIFACTS + (ArtifactSpec(BLUEPRINT_INPUT_RELATIVE, "judgment-blueprint-input"),),
        checkpointed=True,
    ),
    "FEASIBILITY_REPORTED": JudgmentStage(
        stage="FEASIBILITY_REPORTED",
        phase="EXECUTE",
        write_policy="DRAFT_ONLY",
        task=(
            "Map every locked picture state against the full authorized inventory and author the feasibility "
            "draft: per block, the reference role, the coverage class, and the exact evidence that supports it. "
            "A block you cannot cover is 'missing', not a guess."
        ),
        artifacts=_draft_artifacts("FEASIBILITY_REPORTED"),
    ),
    "SELECTION_LOCKED": JudgmentStage(
        stage="SELECTION_LOCKED",
        phase="EXECUTE",
        write_policy="DRAFT_ONLY",
        task=(
            "Score and choose the source clip for every block — Q-score per REEL_WORKFLOW.md — and author the "
            "selection draft with exact source paths, source windows, input profiles and an independent "
            "candidate observation per slot. Speed stays 1.0 and reverse stays false."
        ),
        artifacts=_draft_artifacts("SELECTION_LOCKED"),
    ),
    "ASSETS_LOCKED": JudgmentStage(
        stage="ASSETS_LOCKED",
        phase="EXECUTE",
        write_policy="DRAFT_ONLY",
        task=(
            "Inventory the reference's typography and effect layers and author the assets draft. Every glyph "
            "claim needs either an exact font hash or a traced reference glyph; an unproven font is a withheld "
            "layer, never an approximation."
        ),
        artifacts=_draft_artifacts("ASSETS_LOCKED"),
    ),
    "VISUAL_QC": JudgmentStage(
        stage="VISUAL_QC",
        phase="EXECUTE",
        write_policy="REVIEW_ONLY",
        task=(
            "Watch the rendered candidate at normal speed, end to end, beside the reference comparison board, "
            "and write the watch verdict. Bind it to the candidate's own sha256. A PASS requires all five "
            "inspections to be true; anything less is a FAIL with named, block-level reasons."
        ),
        artifacts=(ArtifactSpec(VISUAL_WATCH_RELATIVE, "judgment-visual-watch"),),
    ),
}


# --- the liveness watchdog -------------------------------------------------
#
# A wall-clock timeout only ends a worker that is *working*. Judgment workers have been
# observed to hang before their first model response — main thread blocked in an ``open`` that
# never returned — and stayed alive, silent, for hours of a 3h budget. The subprocess is
# therefore supervised: the adapter watches for evidence the worker is doing something and
# kills it when that evidence stops, long before the ceiling.
#
# What counts as evidence is deliberately outside the worker's prose: growth of the CLI's
# own session transcript, or any change under the project directory. Both are things the
# worker cannot fake and cannot forget to emit.


#: The classification for "the process was alive but nothing was happening".
LIVENESS = "liveness"
#: The model never answered at all.
FIRST_PROGRESS = "first_progress"
#: It answered, then stopped.
STALL = "stall"

#: The transcript entry type the CLI writes when the *model* has answered. Everything the
#: CLI writes before that — the prompt, the hook attachments, the tool and skill listings —
#: is the process setting itself up, and an observed class of hangs wrote every bit of it
#: within a second of spawn and then never spoke again. So a model turn is the only thing
#: this watchdog will accept as a first sign of life.
MODEL_TURN_TYPE = "assistant"
#: A cheap pre-filter, so the common line is rejected without parsing it.
MODEL_TURN_MARKER = '"{}"'.format(MODEL_TURN_TYPE)

#: How many files one liveness pass will stat. A project with a full render tree must not
#: turn a 15-second poll into a directory walk that costs more than the poll interval.
LIVENESS_TREE_LIMIT = 4000
#: How long to wait for a killed process group to be reaped before moving on.
KILL_REAP_SECONDS = 5.0
#: What a SIGKILLed worker's log records as its exit code.
KILLED_RETURNCODE = -int(signal.SIGKILL)


class WorkerNotAlive(ReelctlError):
    """The worker held its process open without producing any observable work.

    Carries the threshold that tripped so the daemon's reason names it, and whatever the
    worker had printed before the kill so the forensics are not lost with the process.
    """

    def __init__(
        self,
        threshold: str,
        *,
        seconds: float,
        watch: "LivenessWatch",
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.threshold = threshold
        self.seconds = float(seconds)
        self.watch = watch
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(self.describe())

    def describe(self) -> str:
        amount = "{:g}s".format(round(self.seconds, 1))
        if self.threshold == FIRST_PROGRESS:
            what = "never answered within {} of its spawn".format(amount)
            evidence = "no model turn in {}".format(self.watch.transcript_path)
        else:
            what = "went silent for {} after it had answered".format(amount)
            evidence = "no growth in {} and no change under {}".format(self.watch.transcript_path, self.watch.project_dir)
        return (
            "the judgment worker {} ({}/{}): {}. "
            "Its process group was SIGKILLed; the wall clock was not waited out.".format(
                what, LIVENESS, self.threshold, evidence
            )
        )


def encode_cwd(cwd: Any) -> str:
    """The CLI's own transcript-directory encoding for a working directory.

    Every non-alphanumeric character becomes ``-``: ``/home/user/.agent/work`` is filed as
    ``-home-user--agent-work``, so a hidden directory's dot is encoded like the separator,
    not dropped.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def worker_transcript_path(*, cwd: Any, session_id: str, home: Optional[Path] = None) -> Path:
    """Where the CLI writes the transcript for a ``--session-id`` run started in ``cwd``."""
    root = Path(home) if home is not None else Path.home()
    return root / ".claude" / "projects" / encode_cwd(cwd) / "{}.jsonl".format(session_id)


def _stat(path: Path) -> Optional[Tuple[int, int]]:
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns)


def _tree_fingerprint(root: Path) -> Tuple[int, int, int]:
    """How many files are under ``root``, how new the newest is, and how big they are.

    Aggregates rather than a listing: the question is only "did anything change", and an
    aggregate answers it in one number per pass instead of a snapshot to diff.
    """
    count = 0
    newest = 0
    total = 0
    stack: List[Path] = [Path(root)]
    while stack and count < LIVENESS_TREE_LIMIT:
        current = stack.pop()
        try:
            with os.scandir(current) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if count >= LIVENESS_TREE_LIMIT:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            count += 1
            total += info.st_size
            newest = max(newest, info.st_mtime_ns)
    return (count, newest, total)


def _answered(path: Optional[Path]) -> bool:
    """Whether the transcript shows the model itself has answered at least once.

    Read as JSON rather than grepped: a truncated line that happens to carry the word is
    the CLI writing, not the model speaking.
    """
    if path is None:
        return False
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if MODEL_TURN_MARKER not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("type") == MODEL_TURN_TYPE:
                    return True
    except OSError:
        return False
    return False


class LivenessSample(NamedTuple):
    """One pass of evidence: whether the model has spoken, and whether anything moved."""

    answered: bool
    transcript: Optional[Tuple[int, int]]
    tree: Tuple[int, int, int]


@dataclass(frozen=True)
class LivenessWatch:
    """What the supervisor watches, and how long it will wait for each kind of silence."""

    transcript_path: Path
    project_dir: Path
    first_progress_timeout_seconds: float
    stall_timeout_seconds: float
    poll_seconds: float = 15.0
    #: ``~/.claude/projects``. Only used to find a transcript filed under an encoding this
    #: build does not predict — a mis-derived path must not make a healthy worker look dead.
    transcript_root: Optional[Path] = None
    session_id: Optional[str] = None

    def fingerprint(self) -> LivenessSample:
        transcript = self.transcript()
        return LivenessSample(_answered(transcript), _stat(transcript) if transcript is not None else None, _tree_fingerprint(self.project_dir))

    def transcript(self) -> Optional[Path]:
        """This run's transcript, wherever the CLI actually filed it."""
        if _stat(self.transcript_path) is not None:
            return self.transcript_path
        if self.transcript_root is None or not self.session_id:
            return None
        for candidate in sorted(Path(self.transcript_root).glob("*/{}.jsonl".format(self.session_id))):
            if _stat(candidate) is not None:
                return candidate
        return None


def _probe(watch: LivenessWatch) -> Optional[LivenessSample]:
    """One fingerprint, or ``None`` when the filesystem itself did not answer in time.

    The hang this watchdog exists for was a syscall that never returned, so the probe must
    not be able to inherit it: it runs on a daemon thread, and an unanswered probe is
    simply not a liveness signal — which is what eventually trips the kill.
    """
    box: Dict[str, Any] = {}

    def read() -> None:
        try:
            box["value"] = watch.fingerprint()
        except Exception:  # a probe that cannot read is no evidence, not a crash
            box["value"] = None

    thread = threading.Thread(target=read, daemon=True, name="judgment-liveness-probe")
    thread.start()
    thread.join(max(float(watch.poll_seconds), 0.05))
    return box.get("value") if not thread.is_alive() else None


def _kill_group(process: Any) -> None:
    """SIGKILL the worker's whole session.

    Two things are deliberate. The group, because ``claude`` spawns children that outlive a
    kill aimed at the parent; and SIGKILL rather than SIGTERM, because a worker wedged in an
    uninterruptible syscall does not run a handler — verified on observed hangs.
    """
    pid = getattr(process, "pid", None)
    if pid is None:  # pragma: no cover - defensive
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):  # pragma: no cover - already gone
            pass
    try:
        process.wait(timeout=KILL_REAP_SECONDS)
    except (subprocess.TimeoutExpired, OSError):  # pragma: no cover - reaped by init
        pass


class _Captured:
    """stdout/stderr on disk, so a poll loop never has to drain a pipe to stay alive."""

    def __init__(self, enabled: bool) -> None:
        self.stdout = tempfile.TemporaryFile("w+b") if enabled else None
        self.stderr = tempfile.TemporaryFile("w+b") if enabled else None

    def read(self, text: bool) -> Tuple[Any, Any]:
        return (self._read(self.stdout, text), self._read(self.stderr, text))

    @staticmethod
    def _read(handle: Any, text: bool) -> Any:
        if handle is None:
            return None
        try:
            handle.seek(0)
            raw = handle.read()
        except OSError:  # pragma: no cover - defensive
            raw = b""
        return raw.decode("utf-8", errors="replace") if text else raw

    def close(self) -> None:
        for handle in (self.stdout, self.stderr):
            if handle is not None:
                try:
                    handle.close()
                except OSError:  # pragma: no cover - defensive
                    pass


def supervised_run(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    capture_output: bool = True,
    text: bool = True,
    timeout: Optional[float] = None,
    check: bool = False,
    liveness: Optional[LivenessWatch] = None,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a judgment worker, supervised by a liveness watchdog.

    Same contract as ``subprocess.run`` for a worker that completes, and the same
    ``TimeoutExpired`` at the wall-clock ceiling. The difference is what happens in between:
    a worker that stops producing evidence of work is killed at its liveness deadline with
    ``WorkerNotAlive``, hours before that ceiling.
    """
    command = [str(item) for item in argv]
    captured = _Captured(capture_output)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=captured.stdout,
            stderr=captured.stderr,
            # Its own session, so one killpg reaches the worker's children too.
            start_new_session=True,
        )
        started = time.monotonic()
        baseline = _probe(liveness) if liveness is not None else None
        answered = bool(baseline.answered) if baseline is not None else False
        last_signal = started
        while process.poll() is None:
            moment = time.monotonic()
            if timeout is not None and moment - started >= timeout:
                _kill_group(process)
                stdout, stderr = captured.read(text)
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            if liveness is not None:
                sample = _probe(liveness)
                if sample is not None:
                    if baseline is None:
                        baseline = sample  # the first readable pass is a baseline, not a signal
                    elif sample != baseline:
                        baseline = sample
                        last_signal = moment
                    answered = answered or bool(sample.answered)
                if answered:
                    # It spoke once: any movement since — transcript or artifacts — is work.
                    threshold, limit, silent = STALL, liveness.stall_timeout_seconds, moment - last_signal
                else:
                    # It has not spoken yet, so the deadline runs from the spawn. The CLI's
                    # own startup writes are not the worker working, and letting them reset
                    # this clock is what once left a hang to the stall deadline instead.
                    threshold, limit, silent = FIRST_PROGRESS, liveness.first_progress_timeout_seconds, moment - started
                if silent >= limit:
                    _kill_group(process)
                    stdout, stderr = captured.read(text)
                    raise WorkerNotAlive(
                        threshold,
                        seconds=silent,
                        watch=liveness,
                        stdout=stdout or "",
                        stderr=stderr or "",
                    )
            slice_seconds = float(liveness.poll_seconds) if liveness is not None else 0.5
            if timeout is not None:
                slice_seconds = min(slice_seconds, max(timeout - (time.monotonic() - started), 0.0))
            try:
                process.wait(timeout=max(slice_seconds, 0.01))
            except subprocess.TimeoutExpired:
                continue
        stdout, stderr = captured.read(text)
    finally:
        captured.close()
    returncode = int(process.returncode)
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, command, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


# --- the adapter -----------------------------------------------------------


class HeadlessJudgmentAdapter(JudgmentAdapter):
    """Runs one judgment stage as a headless worker and believes only what it wrote."""

    def __init__(
        self,
        config: StudioConfig,
        *,
        runner: Optional[Any] = None,
        run: Callable[..., Any] = supervised_run,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config
        self.runner = runner if runner is not None else StageRunner(config)
        self._run = run
        self._now = now or (lambda: datetime.now(timezone.utc))

    # --- entry point -------------------------------------------------------

    def run(self, job: JobSpec, project_dir: Path) -> StageResult:
        project_dir = Path(project_dir)
        started = self._now()
        record: Dict[str, Any] = {
            "schema_version": 1,
            "authority": "artifacts",
            "job_id": job.job_id,
            "project_id": job.project_id,
            "stage": job.stage,
            "kind": job.kind,
            "revision": self.config.revision,
            "reelctl_version": __version__,
            "started_at_utc": _stamp(started),
            "contract": {"path": str(self.config.judgment_contract_path), "sha256": None},
            "invocation": None,
            "worker": None,
            "checkpoint": {"before": None, "after": None},
            "artifacts": [],
            "validation": None,
            "advance": None,
        }

        spec = JUDGMENT_STAGES.get(job.stage)
        if spec is None:
            return self._finish(record, WITHHELD, "no judgment contract declares {}; the daemon will not invent one".format(job.stage))

        executable = self.config.which("claude")
        if executable is None:
            return self._finish(
                record,
                UNAVAILABLE,
                "claude is not on the studio tool path {}".format(os.pathsep.join(self.config.tool_path)),
            )
        if not self.config.judgment_enabled:
            return self._finish(record, WITHHELD, "headless judgment is switched off (REEL_STUDIO_JUDGMENT_ENABLED)")

        try:
            contract = freeze_contract(self.config.judgment_contract_path)
        except ContractUnavailable as exc:
            return self._finish(record, WITHHELD, str(exc))
        record["contract"] = {
            "path": str(contract.path),
            "sha256": contract.sha256,
            "bytes": contract.bytes,
            "phase": spec.phase,
            "write_policy": spec.write_policy,
            "render_authorized": False,
        }

        parked = capacity_park(self.config, now=started)
        if parked is not None:
            return self._finish(
                record,
                WITHHELD,
                "headless capacity is still parked until {}: {}".format(parked.get("retry_after_utc"), parked.get("reason")),
                payload={"class": CAPACITY, "retry_after_utc": parked.get("retry_after_utc")},
            )

        before = checkpoint(project_dir)
        record["checkpoint"]["before"] = before

        brief = self.brief(job, project_dir, spec, checkpoint_state=before)
        session_id = self._session_id(job, started)
        argv = self.invocation(job, project_dir, contract=contract, brief=brief, session_id=session_id, executable=executable)
        watch = self.liveness_watch(project_dir, session_id=session_id)
        record["invocation"] = {
            "argv": list(argv),
            "cwd": str(self.config.projects_root),
            "path": os.pathsep.join(self.config.tool_path),
            "timeout_seconds": self.config.judgment_timeout_seconds,
            "model": self.config.judgment_model,
            "fallback_model": self.config.judgment_fallback_model,
            "permission_mode": self.config.judgment_permission_mode,
            "disallowed_tools": list(DISALLOWED_TOOLS),
            "session_id": session_id,
            "liveness": {
                "transcript": str(watch.transcript_path),
                "project_dir": str(watch.project_dir),
                "first_progress_timeout_seconds": watch.first_progress_timeout_seconds,
                "stall_timeout_seconds": watch.stall_timeout_seconds,
                "poll_seconds": watch.poll_seconds,
            },
        }

        elapsed = time.monotonic()
        try:
            completed = self._run(
                list(argv),
                cwd=str(self.config.projects_root),
                env=self.environment(),
                capture_output=True,
                text=True,
                timeout=self.config.judgment_timeout_seconds,
                check=False,
                liveness=watch,
            )
        except WorkerNotAlive as exc:
            # A hang is a failure of this attempt, not of the reel: FAIL puts it back
            # through the daemon's own retry-then-park policy with a reason that names
            # which deadline tripped, instead of burning the 3h ceiling twice.
            self._write_log(job, started, argv=argv, stdout=exc.stdout, stderr=exc.stderr, returncode=KILLED_RETURNCODE)
            record["worker"] = {
                "outcome": FAIL,
                "class": LIVENESS,
                "threshold": exc.threshold,
                "silent_seconds": round(exc.seconds, 3),
                "duration_seconds": round(time.monotonic() - elapsed, 3),
                "log": str(self._log_path(job, started)),
            }
            record["checkpoint"]["after"] = checkpoint(project_dir)
            return self._finish(record, FAIL, str(exc), payload={"class": LIVENESS, "threshold": exc.threshold})
        except subprocess.TimeoutExpired:
            record["worker"] = {"outcome": TIMEOUT, "duration_seconds": time.monotonic() - elapsed}
            record["checkpoint"]["after"] = checkpoint(project_dir)
            return self._finish(
                record,
                TIMEOUT,
                "the judgment worker exceeded its {}s wall-clock timeout and was killed".format(self.config.judgment_timeout_seconds),
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            record["worker"] = {"outcome": UNAVAILABLE, "error": str(exc)}
            return self._finish(record, UNAVAILABLE, "the judgment worker could not be executed: {}".format(exc))

        stdout = getattr(completed, "stdout", "") or ""
        stderr = getattr(completed, "stderr", "") or ""
        returncode = int(getattr(completed, "returncode", 0) or 0)
        parsed = _parse_result(stdout)
        record["worker"] = {
            "exit_code": returncode,
            "duration_seconds": round(time.monotonic() - elapsed, 3),
            "result_parsed": parsed is not None,
            "subtype": (parsed or {}).get("subtype"),
            "is_error": (parsed or {}).get("is_error"),
            "num_turns": (parsed or {}).get("num_turns"),
            "total_cost_usd": (parsed or {}).get("total_cost_usd"),
            "session_id": (parsed or {}).get("session_id"),
            "log": str(self._log_path(job, started)),
        }
        self._write_log(job, started, argv=argv, stdout=stdout, stderr=stderr, returncode=returncode)
        record["checkpoint"]["after"] = checkpoint(project_dir)

        # Artifacts first: they are the only authority, so a clean run is decided by disk.
        if returncode == 0:
            validation = self.validate(job, project_dir, spec)
            record["artifacts"] = validation["artifacts"]
            record["validation"] = {"status": validation["status"], "reasons": validation["reasons"]}
            if validation["status"] == PASS:
                clear_capacity(self.config)
                return self._advance(record, job, project_dir, spec)

        signal = capacity_signal(
            "\n".join([stderr, stdout, str((parsed or {}).get("result") or "")]),
            now=started,
            max_park_seconds=self.config.judgment_max_park_seconds,
        )
        if signal is not None:
            park_capacity(self.config, until=_parse_stamp(signal["retry_after_utc"]), reason=signal["reason"], now=started)
            return self._finish(
                record,
                WITHHELD,
                "{} ({})".format(signal["reason"], signal["observed"] or "no detail printed"),
                payload={"class": CAPACITY, "retry_after_utc": signal["retry_after_utc"]},
            )

        if returncode != 0:
            detail = stderr.strip() or str((parsed or {}).get("result") or "").strip() or "no output"
            return self._finish(record, FAIL, "the judgment worker exited {}: {}".format(returncode, detail[:600]))

        reasons = (record.get("validation") or {}).get("reasons") or ["the worker left nothing this daemon can verify"]
        return self._finish(
            record,
            FAIL,
            "{} reported a result the artifacts do not support: {}".format(job.stage, "; ".join(reasons[:10])),
        )

    # --- invocation --------------------------------------------------------

    def environment(self) -> Dict[str, str]:
        """PATH is replaced, never inherited — a launchd job starts with /usr/bin:/bin."""
        env = {"PATH": os.pathsep.join(self.config.tool_path)}
        for key in ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def invocation(
        self,
        job: JobSpec,
        project_dir: Path,
        *,
        contract: FrozenContract,
        brief: str,
        session_id: str,
        executable: str,
    ) -> List[str]:
        """Build the argv. Every flag here is one §6.3 verified exists in this build.

        Ordering matters: ``--add-dir`` and ``--disallowedTools`` are variadic, so the last
        option before the positional brief is deliberately a single-value one.
        """
        argv: List[str] = [
            executable,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.config.judgment_model,
            "--fallback-model",
            self.config.judgment_fallback_model,
            "--permission-mode",
            self.config.judgment_permission_mode,
            "--add-dir",
            str(project_dir),
            "--disallowedTools",
            ",".join(DISALLOWED_TOOLS),
            "--strict-mcp-config",
            "--append-system-prompt",
            contract.text,
            "--session-id",
            session_id,
            brief,
        ]
        return argv

    def liveness_watch(self, project_dir: Path, *, session_id: str) -> LivenessWatch:
        """What proves this particular worker is still working.

        The transcript is derived from the session id this adapter pinned, so the evidence
        belongs to *this* job and not to whatever else the box is running; the project
        directory is the second signal, because a worker deep in a long read writes
        artifacts before it writes anything else the daemon can see.
        """
        home = Path(self.environment().get("HOME") or Path.home())
        return LivenessWatch(
            transcript_path=worker_transcript_path(cwd=self.config.projects_root, session_id=session_id, home=home),
            project_dir=Path(project_dir),
            first_progress_timeout_seconds=self.config.judgment_first_progress_timeout_seconds,
            stall_timeout_seconds=self.config.judgment_stall_timeout_seconds,
            poll_seconds=self.config.judgment_liveness_poll_seconds,
            transcript_root=home / ".claude" / "projects",
            session_id=session_id,
        )

    def _session_id(self, job: JobSpec, started: datetime) -> str:
        seed = "reel-studio/{}/{}/{}/{}".format(job.project_id, job.stage, job.job_id, _stamp(started))
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))

    def brief(self, job: JobSpec, project_dir: Path, spec: JudgmentStage, *, checkpoint_state: Dict[str, Any]) -> str:
        """The job brief: inputs, the exact output paths, and the refusal conditions."""
        project = _project_config(project_dir)
        revision = self.config.revision
        reference = project.get("reference_path") or project.get("reference_input") or "UNKNOWN"
        footage = project.get("authorized_footage_root") or project.get("footage_root") or "UNKNOWN"
        lines: List[str] = [
            "PROJECT INPUTS for this job. The appended contract governs everything else.",
            "",
            "```yaml",
            "phase: {}".format(spec.phase),
            'project_id: "{}"'.format(job.project_id),
            'current_revision: "{}"'.format(revision),
            "reference_paths_or_urls:",
            '  - "{}"'.format(reference),
            'authorized_footage_root: "{}"'.format(footage),
            'output_format: "vertical 9:16"',
            'requested_delivery: "local private review only"',
            "write_policy: {}".format(spec.write_policy),
            "render_authorized: false",
            "external_actions: NONE",
            "```",
            "",
            "STAGE: {} — {}".format(job.stage, spec.task),
        ]
        if job.reason:
            lines += ["", "WHY THIS STAGE IS HERE: {}".format(job.reason)]
        lines += ["", "WHAT COUNTS AS DONE. Only files count. Write, under {}:".format(project_dir)]
        for artifact in spec.artifacts:
            relative = artifact.relative.format(revision=revision)
            if artifact.schema:
                lines.append("  - {} — must validate against the packaged `{}` contract".format(relative, artifact.schema))
            else:
                lines.append("  - {}".format(relative))
        if spec.checkpointed:
            resume = checkpoint_state.get("resume_at")
            if checkpoint_state.get("present"):
                have = ", ".join(entry["file"] for entry in checkpoint_state["present"])
                lines += ["", "RESUME. These brain-pack files already exist and are hash-recorded: {}.".format(have)]
                if resume:
                    lines.append("Re-verify them, do not rewrite them, and start at {}.".format(resume))
                else:
                    lines.append(
                        "Re-verify them against their own manifest; the pack is complete, so the only thing left to "
                        "write is the artifact above."
                    )
            else:
                lines += ["", "RESUME. No brain-pack file exists yet; start at {}.".format(resume or BRAIN_PACK[0])]
        lines += [
            "",
            "AUTHORITY. Your prose is stored as a log and has no authority: this daemon reads the files above, "
            "validates them against their schemas, and only then makes the advancing `reelctl` call itself. "
            "Do not run reelctl, do not edit state.json, do not touch any stage receipt, and do not render.",
            "",
            "REFUSALS. If you cannot verify something, withhold it and say so in the artifact. Do not claim a watch, "
            "a coverage class or a font you did not evidence. If the decision needs the operator, emit a CALL_REQUIRED "
            "block in the contract's schema and stop — a named refusal is a correct outcome here, an unevidenced PASS is not.",
        ]
        return "\n".join(lines)

    # --- validation --------------------------------------------------------

    def validate(self, job: JobSpec, project_dir: Path, spec: JudgmentStage) -> Dict[str, Any]:
        """Read what the worker wrote and check it against the packaged schemas."""
        revision = self.config.revision
        artifacts: List[Dict[str, Any]] = []
        reasons: List[str] = []
        for artifact in spec.artifacts:
            relative = artifact.relative.format(revision=revision)
            path = Path(project_dir) / relative
            entry: Dict[str, Any] = {"path": relative, "schema": artifact.schema, "status": FAIL}
            if not path.is_file():
                entry["reason"] = "the worker wrote no {}".format(relative)
                reasons.append(entry["reason"])
                artifacts.append(entry)
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                entry["reason"] = "{} could not be read: {}".format(relative, exc)
                reasons.append(entry["reason"])
                artifacts.append(entry)
                continue
            entry["sha256"] = sha256_file(path)
            entry["bytes"] = len(raw.encode("utf-8"))
            if not raw.strip():
                entry["reason"] = "{} is empty".format(relative)
                reasons.append(entry["reason"])
                artifacts.append(entry)
                continue
            if artifact.kind != "json":
                entry["status"] = PASS
                artifacts.append(entry)
                continue
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                entry["reason"] = "{} is not parseable JSON: {}".format(relative, exc)
                reasons.append(entry["reason"])
                artifacts.append(entry)
                continue
            if isinstance(payload, dict) and str(payload.get("status", "")).upper() == "DRAFT":
                entry["reason"] = "{} is still a DRAFT; the worker left the template untouched".format(relative)
                reasons.append(entry["reason"])
                artifacts.append(entry)
                continue
            if artifact.schema:
                try:
                    validate_contract(artifact.schema, payload)
                except ContractValidationError as exc:
                    entry["reason"] = str(exc)
                    reasons.append(str(exc))
                    artifacts.append(entry)
                    continue
                entry["schema_sha256"] = schema_sha256(artifact.schema)
            entry["status"] = PASS
            artifacts.append(entry)
        return {"status": PASS if not reasons else FAIL, "reasons": reasons, "artifacts": artifacts}

    # --- advancing the stage (the daemon's own call, never the worker's) ----

    def _advance(self, record: Dict[str, Any], job: JobSpec, project_dir: Path, spec: JudgmentStage) -> StageResult:
        if job.stage == "BLUEPRINT_LOCKED":
            return self._advance_blueprint(record, job, project_dir)
        if job.stage == "VISUAL_QC":
            return self._advance_visual(record, job, project_dir)
        # The draft-authoring stages need no call: the draft is now authored, so the next
        # tick schedules the same stage as deterministic and reelctl locks it.
        return self._finish(record, PASS, None)

    def _advance_blueprint(self, record: Dict[str, Any], job: JobSpec, project_dir: Path) -> StageResult:
        payload = _read_json(project_dir / BLUEPRINT_INPUT_RELATIVE)
        if not payload.get("all_frames_reviewed"):
            return self._finish(
                record,
                WITHHELD,
                "the worker would not attest that all frames were reviewed, so the blueprint stays unlocked",
            )
        observations_path = Path(project_dir) / BLUEPRINT_OBSERVATIONS_RELATIVE
        atomic_write_json(observations_path, payload["observations"], root=Path(project_dir))
        boundaries = ",".join(str(int(value)) for value in payload.get("boundaries_after", []))
        argv: List[str] = ["blueprint", "lock", job.project_id, "--boundaries", boundaries]
        hard_cuts = payload.get("hard_cuts_after") or []
        if hard_cuts:
            argv += ["--hard-cuts", ",".join(str(int(value)) for value in hard_cuts)]
        argv += ["--observations", str(observations_path), "--all-frames-reviewed"]
        return self._run_advance(record, argv, success=PASS)

    def _advance_visual(self, record: Dict[str, Any], job: JobSpec, project_dir: Path) -> StageResult:
        revision = self.config.revision
        watch = _read_json(Path(project_dir) / VISUAL_WATCH_RELATIVE.format(revision=revision))
        receipt_path = Path(project_dir) / "edit" / "render-{}".format(revision) / "render-receipt.json"
        if not receipt_path.is_file():
            return self._finish(
                record,
                WITHHELD,
                "there is no render receipt for {} to bind a watch to; the candidate must be rendered first".format(revision),
            )
        rendered = str(_read_json(receipt_path).get("review", {}).get("sha256") or "")
        claimed = str(watch.get("candidate_sha256") or "")
        if claimed != rendered:
            return self._finish(
                record,
                FAIL,
                "the watch is bound to {} but the rendered candidate is {}; the worker did not watch this candidate".format(
                    claimed or "nothing", rendered or "unrecorded"
                ),
            )
        status = str(watch.get("status", "FAIL")).upper()
        argv: List[str] = ["review", "record-agent", job.project_id, "--revision", revision, "--status", status]
        for flag, key in (
            ("--normal-speed-full-watch", "normal_speed_full_watch"),
            ("--reference-side-by-side-checked", "reference_side_by_side_checked"),
            ("--typography-checked", "typography_checked"),
            ("--color-checked", "color_checked"),
            ("--cut-and-beat-checked", "cut_and_beat_checked"),
        ):
            if bool(watch.get(key)):
                argv.append(flag)
        argv += ["--notes", str(watch.get("notes", ""))[:2000]]
        if status == "PASS":
            return self._run_advance(record, argv, success=PASS)
        return self._run_advance(record, argv, success=WITHHELD, reason=str(watch.get("notes", "")).strip())

    def _run_advance(self, record: Dict[str, Any], argv: Sequence[str], *, success: str, reason: Optional[str] = None) -> StageResult:
        result = self.runner.run(list(argv), timeout_seconds=self.config.stage_timeout_seconds)
        record["advance"] = {"argv": [str(item) for item in argv], "status": result.status, "reason": result.reason}
        if result.status != PASS:
            return self._finish(
                record,
                result.status if result.status in (WITHHELD, UNAVAILABLE, TIMEOUT) else FAIL,
                result.reason or "the advancing reelctl call reported {}".format(result.status),
            )
        return self._finish(record, success, reason)

    # --- receipts ----------------------------------------------------------

    def _receipt_stem(self, job: JobSpec, started: datetime) -> str:
        if job.job_id is not None:
            return str(int(job.job_id))
        stamp = _stamp(started).replace(":", "").replace("-", "")
        return "{}-{}-{}".format(job.project_id, job.stage, stamp)

    def _log_path(self, job: JobSpec, started: datetime) -> Path:
        return self.config.job_receipts_dir / "{}.worker.log".format(self._receipt_stem(job, started))

    def _write_log(self, job: JobSpec, started: datetime, *, argv: Sequence[str], stdout: str, stderr: str, returncode: int) -> None:
        path = self._log_path(job, started)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = [
            "# judgment worker log — {} {} — exit {}".format(job.project_id, job.stage, returncode),
            "# this log has no authority; the artifacts do",
            "# argv: {}".format(" ".join(str(item) for item in argv[:12])),
            "",
            "--- stdout ---",
            stdout,
            "--- stderr ---",
            stderr,
        ]
        try:
            path.write_text("\n".join(body), encoding="utf-8")
        except OSError:
            pass

    def _finish(
        self,
        record: Dict[str, Any],
        status: str,
        reason: Optional[str],
        *,
        payload: Optional[Dict[str, Any]] = None,
    ) -> StageResult:
        finished = self._now()
        started = _parse_stamp(record.get("started_at_utc")) or finished
        record["finished_at_utc"] = _stamp(finished)
        record["duration_seconds"] = round((finished - started).total_seconds(), 3)
        record["result"] = {"status": status, "reason": reason, **(payload or {})}
        stem = record.get("_stem") or self._receipt_stem(
            JobSpec(
                project_id=record["project_id"],
                stage=record["stage"],
                kind=record["kind"],
                job_id=record.get("job_id"),
            ),
            started,
        )
        receipt_path = self.config.job_receipts_dir / "{}.json".format(stem)
        try:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(receipt_path, record, root=self.config.job_receipts_dir)
        except OSError:
            pass
        return StageResult(
            status=status,
            argv=tuple((record.get("invocation") or {}).get("argv", ())),
            returncode=(record.get("worker") or {}).get("exit_code"),
            payload={"receipt": str(receipt_path), **(payload or {})},
            reason=reason,
        )


def judgment_adapter(config: StudioConfig, *, enabled: bool = True) -> JudgmentAdapter:
    """What the daemon should install. Switched off, judgment withholds by name.

    Deterministic stages keep running either way — that separation is the whole reason
    capacity is a judgment-only gate (§6.5).
    """
    if enabled and config.judgment_enabled:
        return HeadlessJudgmentAdapter(config)
    return UnavailableJudgmentAdapter(
        reason="headless judgment is switched off for this daemon; deterministic stages continue",
        retry_after_seconds=3600,
    )


# --- small readers ---------------------------------------------------------


def _parse_result(text: str) -> Optional[Dict[str, Any]]:
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


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _project_config(project_dir: Path) -> Dict[str, Any]:
    return _read_json(Path(project_dir) / "project.json")
