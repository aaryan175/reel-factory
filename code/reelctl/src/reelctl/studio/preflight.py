"""Does this stage's declared input actually answer, before a worker is spawned for it?

The failure class this retires: a judgment brief names an authorized footage root on an
external volume; a daemon-spawned ``claude`` holds no TCC grant for that volume; and because
the CLI is a prompt-eligible TCC identity, the read does not fail — it blocks in ``openat``
waiting for a consent prompt that an unattended service cannot answer. Workers hang, and the kernel names
the path only at the instant each is killed.

The asymmetry that makes this fixable: the daemon's *own* interpreter is not
prompt-eligible, so the identical read comes back ``PermissionError(1, 'Operation not
permitted')`` in 30 milliseconds. The daemon can therefore know in advance that a worker
handed this path cannot possibly succeed, and decline to spend one on it.

Three properties are load-bearing:

* **Content, not metadata.** TCC gates the bytes, not the inode: on the wedged volume
  ``stat`` answered instantly while ``listdir`` was denied. A probe that stats would have
  passed every observed hang, so the probe here lists a directory or reads a byte
  of a file, and nothing else counts as an answer.
* **Bounded, and killable.** A path that can block is probed in a child interpreter with a
  deadline, because a read stuck in ``openat`` cannot be interrupted from inside the process
  that issued it — a thread there leaks for the life of the daemon, where a process can be
  killed. This is not hypothetical: measured from launchd, the daemon's own
  interpreter blocked on the footage root for the full timeout rather than returning the
  30ms ``PermissionError`` the earlier probe saw from a granted shell. A mount that has
  stopped answering therefore costs this daemon one interpreter start and its timeout,
  never its tick loop — which is the exact failure the module exists to prevent, and would
  be absurd to reintroduce here.
* **Declared, not guessed.** An input is preflighted because a stage genuinely reads it:
  the judgment brief names the footage root and the reference for every judgment stage, and
  ``footage index``, ``assets lock`` and ``render`` are the deterministic commands that
  reach under the footage root. Over-declaring would park stages the wall cannot reach,
  which is an invented blocker — the one thing worse than a missed one.

What the daemon does with a blocked verdict is policy, implemented in ``daemon._run_one``: park the job WITHHELD with a retry window and **no
attempt spent**. An environment-permission wall is not the job's failure, and burning the
two-attempt ceiling on it conflates the box with the work.
"""

from __future__ import annotations

import errno as errno_module
import json
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..authorization import ROOT_KEYS
from ._readprobe import read_test
from .config import StudioConfig
from .stages import JUDGMENT, JobSpec

#: The child program that performs the read, run by path so it costs a bare interpreter
#: start rather than a package import.
PROBE_PROGRAM = Path(__file__).with_name("_readprobe.py")

#: ``-I`` isolates the child from the environment and the user site directory; ``-S`` skips
#: site processing entirely, which is most of what an interpreter start costs.
PROBE_FLAGS: Tuple[str, ...] = ("-I", "-S")

#: What ``_readprobe`` exits with when the filesystem refused the read.
REFUSED_RETURNCODE = 3

#: The operator card that clears the known instance of this wall (hang-debug-receipt.md).
TCC_CALL_ID = "CALL-TCC-WORKDRIVE-VOLUME"

#: How long one input gets to answer. Generous against a cold spinning mount, and still two
#: orders of magnitude under the 600s a hung worker used to cost before the watchdog saw it.
READ_TIMEOUT_SECONDS = 5.0

#: What a probe reports when the read never came back at all.
TIMEOUT = "TIMEOUT"

#: What a probe reports when the probe itself could not be run. Distinct from a refusal on
#: purpose: "I could not find out" and "the path is closed to me" are different sentences,
#: and only one of them is about the path.
PROBE_FAILED = "PROBE_FAILED"

#: Deterministic stages whose ``reelctl`` command reads under the authorized footage root:
#: ``footage index`` walks it, ``assets lock`` authorizes manifest sources against it, and
#: ``render`` opens the selected clips. The rest work from project-local JSON — feasibility
#: and selection read ``footage/footage-index.json``, QC reads the candidate — so declaring
#: the volume for them would park work the wall never touches.
FOOTAGE_READING_STAGES = frozenset({"FOOTAGE_INDEXED", "ASSETS_LOCKED", "RENDERED"})

#: The stage whose command reads the reference media itself.
REFERENCE_READING_STAGES = frozenset({"REFERENCE_LOCKED"})

#: Where the brief takes the reference from, in the order ``HeadlessJudgmentAdapter.brief``
#: resolves it. A ``reference_input`` is frequently a URL, which is not this module's
#: business — only values that name a filesystem path are probed.
REFERENCE_KEYS: Tuple[str, ...] = ("reference_path", "reference_input")

#: An open call is offered as cover for a blocked path when it names the path outright or
#: when it is already about the environment rather than the reel.
ENVIRONMENT_ISSUE_TYPES = frozenset({"environment-permission"})

AWAITING_OPERATOR = "AWAITING_OPERATOR"


@dataclass(frozen=True)
class DeclaredInput:
    """One filesystem path a stage is going to be handed, and who declared it."""

    label: str
    path: Path
    source: str


@dataclass(frozen=True)
class InputProbe:
    declared: DeclaredInput
    readable: bool
    seconds: float
    error: Optional[str] = None
    #: The symbolic errno (``EPERM``, ``EACCES``, ``ENOENT``) or ``TIMEOUT``.
    code: Optional[str] = None
    #: Whether the read ran in the killable child. Recorded rather than inferred: on a path
    #: that merely fails, both mechanisms give the same answer, so nothing else distinguishes
    #: a probe that could have survived a block from one that would have hung.
    bounded: bool = False

    def describe(self) -> str:
        if self.readable:
            return "{} {} answered a read in {:.3f}s".format(self.declared.label, self.declared.path, self.seconds)
        if self.code == TIMEOUT:
            return "{} {} did not answer a read within {:.3f}s — the read is still blocked".format(
                self.declared.label, self.declared.path, self.seconds
            )
        return "{} {} is unreadable from this daemon's own context: {} ({})".format(
            self.declared.label, self.declared.path, self.error, self.code
        )


@dataclass(frozen=True)
class BlockedInput:
    """A stage the daemon refuses to start, and everything needed to say why."""

    stage: str
    kind: str
    probe: InputProbe
    external: bool

    def describe(self, *, open_calls: Sequence[Mapping[str, Any]] = ()) -> str:
        parts = [
            "{} was not started: {}, so a worker handed it cannot succeed.".format(self.stage, self.probe.describe()),
            "The path was declared by {}.".format(self.probe.declared.source),
        ]
        if self.external:
            parts.append(
                "It is on the external volume, where a daemon-spawned worker's read blocks on a consent prompt "
                "nobody can answer instead of failing — the operator grant that clears it is {}.".format(TCC_CALL_ID)
            )
        if self.probe.code == TIMEOUT:
            # Which binary needs the grant is the operator's actual next action, and a
            # blocked probe answers it precisely: this interpreter is the process that
            # waited, so granting the worker alone would leave the stage parked here.
            parts.append(
                "The read that blocked was this daemon's own interpreter ({}), so the grant has to cover it and "
                "not only the worker binary.".format(sys.executable)
            )
        covering = covering_calls(open_calls, self.probe.declared.path)
        if covering:
            parts.append("An open call already covers this: {}.".format(", ".join(covering)))
        parts.append("No attempt was spent; this is the box, not the work.")
        return " ".join(parts)

    def payload(self, *, open_calls: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
        return {
            "class": "input_preflight",
            "stage": self.stage,
            "kind": self.kind,
            "label": self.probe.declared.label,
            "path": str(self.probe.declared.path),
            "source": self.probe.declared.source,
            "code": self.probe.code,
            "error": self.probe.error,
            "bounded": self.probe.bounded,
            "seconds": round(self.probe.seconds, 3),
            "external": self.external,
            "calls": covering_calls(open_calls, self.probe.declared.path),
        }


# --- the probe --------------------------------------------------------------


def _symbolic(number: Optional[int]) -> Optional[str]:
    if number is None:
        return None
    return errno_module.errorcode.get(number, str(number))


def _direct(declared: DeclaredInput, started: float) -> InputProbe:
    """Read here, with no deadline. For paths that fail rather than block."""
    try:
        read_test(str(declared.path))
    except OSError as exc:
        return InputProbe(
            declared=declared,
            readable=False,
            seconds=time.monotonic() - started,
            error=str(exc),
            code=_symbolic(exc.errno),
        )
    return InputProbe(declared=declared, readable=True, seconds=time.monotonic() - started)


def _bounded(declared: DeclaredInput, started: float, *, timeout_seconds: float, executable: str) -> InputProbe:
    """Read in a child interpreter that can be killed when the read never comes back.

    The child is a lone interpreter with no children of its own, so the standard library's
    own kill-and-reap on timeout is enough; no process group is needed. A probe that cannot
    even be launched is reported as such rather than as a verdict about the path — refusing
    to run a stage because the *probe* is broken would be a fabricated blocker.
    """
    argv = [executable, *PROBE_FLAGS, str(PROBE_PROGRAM), str(declared.path)]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return InputProbe(declared=declared, readable=False, seconds=time.monotonic() - started, code=TIMEOUT)
    except (OSError, ValueError) as exc:  # pragma: no cover - a broken interpreter path
        return InputProbe(
            declared=declared,
            readable=False,
            seconds=time.monotonic() - started,
            error="the read probe could not be run: {}".format(exc),
            code=PROBE_FAILED,
        )
    seconds = time.monotonic() - started
    if completed.returncode == 0:
        return InputProbe(declared=declared, readable=True, seconds=seconds)
    if completed.returncode == REFUSED_RETURNCODE:
        try:
            refusal = json.loads(completed.stdout or "{}")
        except ValueError:  # pragma: no cover - the child writes json or nothing
            refusal = {}
        return InputProbe(
            declared=declared,
            readable=False,
            seconds=seconds,
            error=str(refusal.get("error") or "the filesystem refused the read"),
            code=_symbolic(refusal.get("errno")),
        )
    detail = (completed.stderr or completed.stdout or "").strip() or "exit {}".format(completed.returncode)
    return InputProbe(
        declared=declared,
        readable=False,
        seconds=seconds,
        error="the read probe exited {}: {}".format(completed.returncode, detail[:300]),
        code=PROBE_FAILED,
    )


def probe_input(
    declared: DeclaredInput,
    *,
    bounded: bool = True,
    timeout_seconds: float = READ_TIMEOUT_SECONDS,
    executable: Optional[str] = None,
) -> InputProbe:
    """Read the declared path and report what happened, one way or the other.

    ``bounded`` chooses the mechanism, and the choice is about blocking, not about trust:
    a path that can only *fail* is read here, while a path that can *block* — the external
    volume, where a read waits on a consent prompt nobody can answer — is read in a child
    that can be killed. The bounded form costs an interpreter start, so it is spent exactly
    where it buys something. Internal paths are read directly, which is no more exposure
    than the health strip's own unbounded ``stat`` of the projects root every tick.
    """
    started = time.monotonic()
    if not bounded:
        return _direct(declared, started)
    probe = _bounded(declared, started, timeout_seconds=timeout_seconds, executable=executable or sys.executable)
    return replace(probe, bounded=True)


# --- what a stage declares --------------------------------------------------


def _project_config(project_dir: Path) -> Dict[str, Any]:
    try:
        payload = json.loads((Path(project_dir) / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _declared_path(project: Mapping[str, Any], keys: Sequence[str]) -> Optional[Tuple[str, Path]]:
    """The first of ``keys`` the project declares as a filesystem path, with its key name."""
    for key in keys:
        raw = str(project.get(key) or "").strip()
        if not raw or "://" in raw:
            continue
        return key, Path(raw).expanduser().absolute()
    return None


def declared_inputs(config: StudioConfig, job: JobSpec, project_dir: Path) -> Tuple[DeclaredInput, ...]:
    """Every filesystem path this stage is going to be handed, in probe order.

    The project directory leads because everything else is read through the job that lives
    in it. The footage root comes next: it is the one that has actually hung a worker, and
    naming it first keeps the operator's next action unambiguous when several inputs are
    unreachable at once.
    """
    directory = Path(project_dir)
    declared: List[DeclaredInput] = [
        DeclaredInput(label="the project directory", path=directory, source="the daemon's projects root")
    ]
    project = _project_config(directory)

    if job.kind == JUDGMENT or job.stage in FOOTAGE_READING_STAGES:
        # ``ROOT_KEYS`` is reelctl's own resolution order for this declaration; the keys are
        # not restated here, so the preflight and the engine cannot disagree about which one
        # governs. Existence is deliberately not required — an unmounted volume is exactly
        # the case worth parking on, and the canonical resolver refuses to return a path
        # for it.
        footage = _declared_path(project, ROOT_KEYS)
        if footage is not None:
            key, path = footage
            declared.append(
                DeclaredInput(label="the authorized footage root", path=path, source="project.json {}".format(key))
            )

    if job.kind == JUDGMENT or job.stage in REFERENCE_READING_STAGES:
        reference = _declared_path(project, REFERENCE_KEYS)
        if reference is not None:
            key, path = reference
            declared.append(
                DeclaredInput(label="the locked reference", path=path, source="project.json {}".format(key))
            )

    seen: Dict[Path, None] = {}
    ordered: List[DeclaredInput] = []
    for item in declared:
        if item.path in seen:
            continue
        seen[item.path] = None
        ordered.append(item)
    return tuple(ordered)


# --- the verdict ------------------------------------------------------------


def _within(candidate: Path, root: Path) -> bool:
    try:
        return candidate == root or root in candidate.parents
    except (OSError, ValueError):  # pragma: no cover - parents is pure path arithmetic
        return False


def is_external(config: StudioConfig, path: Path) -> bool:
    """Whether this path lives where a TCC (removable-volume) wall can apply.

    The configured storage root is the studio's own external volume; ``/Volumes`` covers
    every other removable mount, which is gated by the same privacy setting. Anything on
    internal storage is deliberately excluded: citing a Removable-Volumes grant for a path
    the grant does not cover would send the operator to a setting that changes nothing.
    """
    return _within(path, Path(config.storage_root)) or _within(path, Path("/Volumes"))


def covering_calls(open_calls: Sequence[Mapping[str, Any]], path: Path) -> List[str]:
    """Open call ids already about this blockage — the path by name, or the environment."""
    named: List[str] = []
    for call in open_calls or ():
        if str(call.get("state", AWAITING_OPERATOR)).strip().upper() != AWAITING_OPERATOR:
            continue
        issue = str(call.get("issue_type") or "").strip().lower()
        try:
            serialized = json.dumps(call, default=str)
        except (TypeError, ValueError):  # pragma: no cover - default=str covers the realistic cases
            serialized = ""
        if issue in ENVIRONMENT_ISSUE_TYPES or str(path) in serialized:
            named.append(str(call.get("call_id") or call.get("card") or "an unnamed call"))
    return named


def preflight_inputs(
    config: StudioConfig,
    job: JobSpec,
    project_dir: Path,
    *,
    timeout_seconds: float = READ_TIMEOUT_SECONDS,
) -> Optional[BlockedInput]:
    """``None`` when every declared input answers; otherwise the first one that does not.

    Probing stops at the first blockage on purpose. The daemon's park reason has to name a
    next action, and one unreachable path with its errno is a next action where a list of
    everything currently wrong with the box is not.
    """
    for declared in declared_inputs(config, job, project_dir):
        external = is_external(config, declared.path)
        probe = probe_input(declared, bounded=external, timeout_seconds=timeout_seconds)
        if probe.readable:
            continue
        return BlockedInput(stage=job.stage, kind=job.kind, probe=probe, external=external)
    return None
