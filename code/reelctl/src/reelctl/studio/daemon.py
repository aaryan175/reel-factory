"""The orchestrator daemon core.

Six phases, twenty seconds apart: preflight, reconcile, schedule, claim, run, wake. The
whole of the daemon's memory lives in ``studio.db`` and every row in it is disposable —
state is derived from ``REEL_REGISTRY.json``, each project's ``state.json`` and its
receipts, never remembered across a restart (§6.4).

Three properties are the point of this module, and each has a test that bites:

* **It never crash-loops.** A degraded box (no mount, no ffmpeg, an unparseable registry)
  refuses to start work, says why once, keeps beating, and picks the work back up when the
  reason clears. An exception inside a tick is recorded and the loop continues.
* **It never holds a lock it does not need.** Deterministic stages run as subprocesses and
  ``reelctl.cli.main`` takes the project flock around exactly its own mutation. The daemon
  only *probes* the lock, so a twenty-minute job never blocks ``reelctl status`` (§9.2).
* **It stops instead of looping.** A job is keyed on the project's fingerprint; two
  attempts on the same fingerprint is the ceiling, after which the job goes terminal and a
  ``call_required`` event is raised once — CLAUDE.md's stall rule.

Judgment stages are a seam here, not an implementation: ``runner.JudgmentAdapter`` is the
interface and ``UnavailableJudgmentAdapter`` withholds by name until Task 7 lands.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..hashing import atomic_write_json
from ..locks import LockError, ProjectBusyError, project_lock
from ..state import STAGES, ProjectState, StateError
from . import jobs
from .authority import require_factory_open
from .board import project_ids as disk_project_ids
from .claims import acquire_claim, agent_session_activity
from .config import DEFAULT_BACKOFF_SECONDS, StudioConfig
from .db import initialize, rebuild_from_disk
from .health import health_report
from .judgment import CAPACITY
from .opencalls import INTAKE_RECORD_FILENAME, project_open_calls
from .preflight import preflight_inputs
from .registry import declared_craft_holder, ownership_index, registry_check, registry_report
from .runner import BLOCKED, BUSY, PASS, TIMEOUT, UNAVAILABLE, WITHHELD, JudgmentAdapter, StageRunner, UnavailableJudgmentAdapter
from .stages import DETERMINISTIC, HUMAN, JUDGMENT, JobSpec, call_gate_index, job_for, project_fingerprint

#: Ticks after which ``run_forever`` repeats an unchanged line, so a quiet log still proves
#: liveness. At the 20s default tick that is one line an hour when nothing is happening —
#: the log stays readable and stays bounded, and launchd rotates nothing for us.
LOG_REPEAT_TICKS = 180

#: Statuses the engine reports for work it declined to do rather than failed at.
DECLINED = (BLOCKED, WITHHELD)
#: Statuses that mean "the box, not the work" — deferred without spending an attempt.
CONTENTION = (BUSY, UNAVAILABLE)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stdout(line: str) -> None:
    """The launchd log. Flushed per line, because a buffered log of a long-lived service
    is a log you only get to read after it dies."""
    print(line, file=sys.stdout, flush=True)


def _capacity_retry(result: Any) -> Optional[datetime]:
    """When a judgment result is a capacity park, the moment work may resume."""
    if getattr(result, "status", None) != WITHHELD:
        return None
    payload = getattr(result, "payload", None) or {}
    if payload.get("class") != CAPACITY:
        return None
    stamp = payload.get("retry_after_utc")
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def preflight(config: StudioConfig) -> Dict[str, Any]:
    """§6.5. Every gate that must hold before the daemon may start new work."""
    health = health_report(config)
    reasons: List[str] = list(health["degraded_reasons"])
    if config.which("reelctl") is None:
        reasons.append(
            "the daemon drives stages through reelctl, which is not on the studio tool path {}".format(os.pathsep.join(config.tool_path))
        )
    registry = registry_check(config.registry_path)
    if registry["status"] != "PASS":
        reasons.append(registry["reason"])
    return {
        "status": "DEGRADED" if reasons else "PASS",
        "degraded_reasons": reasons,
        "health": health,
        "registry": registry,
    }


def _drive_refusal(entry: Mapping[str, Any], project_dir: Path) -> Optional[str]:
    """The registry's ``daemon_contract``, applied. ``None`` means the daemon may drive.

    The contract (registry ``daemon_contract``): drive iff
    the project was created via studio intake, or its registry entry says
    ``driver == "daemon"``; a registry-absent project is studio-born and driven;
    ``driver == "agent"`` excludes permanently.

    ``registry.ownership_index`` already folds ``driver == "agent"`` into ``daemon_owned``,
    so what is left here is the studio-intake half, which needs the project on disk.
    """
    if not entry:
        return None  # registry-absent: studio-born, driven
    if entry.get("driver") == "daemon":
        return None
    if (Path(project_dir) / INTAKE_RECORD_FILENAME).is_file():
        return None
    return (
        "the registry lists this reel but does not set driver=daemon, and it was not created via studio intake"
    )


def _awaiting_operator(calls: Sequence[Mapping[str, Any]], *, stage: Optional[str] = None) -> List[str]:
    """Call ids the registry declares still awaiting the operator on a project.

    §10 Task 8 states the rule from the other side — "answering writes a receipt and the
    daemon resumes that project on the next tick; an unanswered call never blocks an
    unrelated reel" — so an unanswered call blocks the reel it names, and the pause has to
    exist before Task 8 can release it.

    Both sources of open calls gate, assembled by ``opencalls.project_open_calls``: the
    registry's ``open_calls`` and the project's own intake record. An earlier version of
    this docstring said project-local cards did *not* gate, and that was correct under the
    blanket design — a card gating everything would have deadlocked intake on its own card.
    Stage scoping plus ``daemon_contract.intake_mode_call_stage = SELECTION_LOCKED`` is the
    mechanism that makes it safe, so the exclusion became a stale premise that let a
    studio-born reel run past its own unanswered mode call.

    The scoping is **stage-scoped**, per the registry's ``daemon_contract.calls_gate_design``
    (which supersedes both the blanket gate and the ``issue_type`` exemption idea that
    preceded it): a call card carries a ``stage`` field naming the first stage its decision
    governs, and the gate compares that against the job's stage. Stages before it proceed;
    the named stage onward waits. That matches the onboarding contract — a call stops work
    before the affected render, not before everything.

    The card's ``stage`` is resolved by ``stages.call_gate_index``, which is the single
    implementation of that rule — the board's layer model calls the same function. It was
    briefly implemented twice and the copies diverged over whitespace, which is invisible
    from either side: the board showed a reel parked while the daemon drove it. Everything
    that cannot be placed gates every stage, so an unrecognised gate is never an open door.
    """
    position = STAGES.index(stage) if stage in STAGES else None
    blocking: List[str] = []
    for call in calls or ():
        if str(call.get("state", "")).strip().upper() != "AWAITING_OPERATOR":
            continue
        gate = call_gate_index(call)
        if gate is not None and position is not None and position < gate:
            continue
        blocking.append(str(call.get("call_id") or call.get("card") or "an unnamed call"))
    return blocking


def project_is_free(projects_root: Path, project_id: str) -> Optional[str]:
    """Probe the project flock. Returns ``None`` when free, else the engine's sentence.

    The probe is taken and released immediately: the daemon must not be holding this lock
    when the stage subprocess reaches for it.
    """
    try:
        with project_lock(projects_root, project_id):
            return None
    except ProjectBusyError as exc:
        return str(exc)
    except LockError as exc:
        return str(exc)


class Orchestrator:
    def __init__(
        self,
        config: StudioConfig,
        *,
        runner: Optional[Any] = None,
        judgment: Optional[JudgmentAdapter] = None,
        owner: Optional[str] = None,
        now: Optional[Callable[[], datetime]] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self.runner = runner if runner is not None else StageRunner(config)
        self.judgment = judgment if judgment is not None else UnavailableJudgmentAdapter()
        self.owner = owner or config.owner
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._log = log if log is not None else _stdout
        self.tick_count = 0
        self._last_reasons: List[str] = []
        self._last_line: Optional[str] = None
        self._last_line_tick = 0

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> Dict[str, Any]:
        """Rebuild everything from disk; discard whatever the database disagrees about."""
        require_factory_open("daemon start")
        self.config.studio_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        initialize(self.config.database_path)
        rebuilt = rebuild_from_disk(self.config.projects_root, database_path=self.config.database_path)
        requeued = jobs.requeue_orphans(self.config.database_path, owner=self.owner, now=self._now())
        self._event("daemon_started", payload={"owner": self.owner, "pid": os.getpid(), "requeued": requeued})
        return {"status": "PASS", "owner": self.owner, "database": rebuilt["database"], "requeued": requeued}

    def run_forever(
        self,
        *,
        interval: Optional[float] = None,
        max_ticks: Optional[int] = None,
        stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        delay = self.config.tick_seconds if interval is None else interval
        ticks = 0
        errors = 0
        self._write(
            {
                "event": "daemon_started",
                "at_utc": _stamp(self._now()),
                "owner": self.owner,
                "pid": os.getpid(),
                "projects_root": str(self.config.projects_root),
                "interval_seconds": delay,
                "judgment": type(self.judgment).__name__,
            }
        )
        while True:
            if stop is not None and stop():
                break
            if max_ticks is not None and ticks >= max_ticks:
                break
            try:
                self._log_tick(self.tick())
            except Exception as exc:  # never let one bad tick end the service
                errors += 1
                self._event("tick_error", payload={"error": str(exc), "error_type": type(exc).__name__})
                self._write(
                    {
                        "event": "tick_error",
                        "at_utc": _stamp(self._now()),
                        "tick": self.tick_count,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                )
            ticks += 1
            if delay:
                time.sleep(delay)
        return {"status": "PASS", "ticks": ticks, "errors": errors}

    # --- the launchd log ---------------------------------------------------

    def _write(self, payload: Mapping[str, Any]) -> None:
        try:
            self._log(json.dumps(payload, default=str, sort_keys=True))
        except Exception:  # a log that cannot be written is not a reason to stop working
            pass

    def _log_tick(self, report: Mapping[str, Any]) -> None:
        """One line per *decision*, not one line per tick.

        A 20-second loop over an idle factory would otherwise write 4,300 identical lines a
        day into a file nothing rotates. The verdict — degraded reasons, what was scheduled,
        what ran, what was skipped and why — is the content; the timestamp is not. So the
        line is suppressed while that content is unchanged, and repeated every
        ``LOG_REPEAT_TICKS`` regardless so a silent log still distinguishes "nothing to do"
        from "dead".
        """
        line = {
            "event": "tick",
            "at_utc": report.get("at_utc"),
            "tick": report.get("tick"),
            "status": report.get("status"),
            "degraded_reasons": list(report.get("degraded_reasons") or ()),
            "scheduled": list(report.get("scheduled") or ()),
            "ran": report.get("ran"),
            "skipped": list(report.get("skipped") or ()),
            "woke": list(report.get("woke") or ()),
        }
        verdict = json.dumps({k: v for k, v in line.items() if k not in ("at_utc", "tick")}, default=str, sort_keys=True)
        repeated = self.tick_count - self._last_line_tick >= LOG_REPEAT_TICKS
        if verdict == self._last_line and not repeated:
            return
        self._last_line = verdict
        self._last_line_tick = self.tick_count
        self._write(line)

    # --- the tick ----------------------------------------------------------

    def tick(self) -> Dict[str, Any]:
        # First operation: a hold permits no heartbeat/event/schedule/claim side effect.
        require_factory_open("daemon tick")
        self.tick_count += 1
        now = self._now()
        report: Dict[str, Any] = {
            "tick": self.tick_count,
            "at_utc": _stamp(now),
            "status": "PASS",
            "degraded_reasons": [],
            "projects": [],
            "reconciled": [],
            "scheduled": [],
            "skipped": [],
            "ran": None,
            "woke": [],
        }

        flight = preflight(self.config)
        report["status"] = flight["status"]
        report["degraded_reasons"] = flight["degraded_reasons"]
        self._announce(flight["degraded_reasons"])
        self._heartbeat(now, flight)
        if flight["status"] != "PASS":
            return report

        report["projects"] = disk_project_ids(self.config.projects_root)
        self._reconcile(report, now)
        self._schedule(report, now)
        self._run_one(report, now)
        self._wake(report, now)
        return report

    # --- 2. reconcile ------------------------------------------------------

    def _reconcile(self, report: Dict[str, Any], now: datetime) -> None:
        """Disk decides. A queued job disk says is done, or aimed at a stage that moved, goes."""
        known = set(report["projects"])
        for row in jobs.all_jobs(self.config.database_path):
            if row["status"] not in jobs.ACTIVE_STATUSES:
                continue
            project_id = row["project_id"]
            reason: Optional[str] = None
            if project_id not in known:
                reason = "project {} is no longer on disk".format(project_id)
            else:
                stage = self._next_stage(project_id)
                if stage is None:
                    reason = "project {} has no stage left to run".format(project_id)
                elif stage != row["stage"]:
                    reason = "disk says {} is past {}; next stage is {}".format(project_id, row["stage"], stage)
                else:
                    fingerprint = self._fingerprint(project_id)
                    if row["input_hash"] is not None and row["input_hash"] != fingerprint:
                        reason = "{} moved on disk since this job was queued".format(project_id)
            if reason is not None:
                jobs.drop(self.config.database_path, int(row["id"]))
                report["reconciled"].append({"job_id": int(row["id"]), "project_id": project_id, "stage": row["stage"], "reason": reason})
                self._event("job_dropped", project_id=project_id, payload={"stage": row["stage"], "reason": reason})

    # --- 3. schedule -------------------------------------------------------

    def _schedule(self, report: Dict[str, Any], now: datetime) -> None:
        database = self.config.database_path
        ownership = ownership_index(
            self.config.registry_path,
            projects_root=self.config.projects_root,
            project_ids=report["projects"],
        )
        lane_load = self._lane_load(report["projects"], ownership)
        craft_holder = declared_craft_holder(self.config.registry_path)
        registry = registry_report(self.config.registry_path)
        for project_id in report["projects"]:
            if jobs.active_job(database, project_id) is not None:
                continue
            entry = ownership.get(project_id) or {}
            if entry and not entry.get("daemon_owned", True):
                self._skip(report, project_id, entry.get("reason") or "the registry hands this reel to another runtime")
                continue
            project_dir = self.config.projects_root / project_id
            refusal = _drive_refusal(entry, project_dir)
            if refusal is not None:
                self._skip(report, project_id, refusal)
                continue
            stage = self._next_stage(project_id)
            if stage is None:
                self._skip(report, project_id, "every stage is complete; nothing left for the machine to do")
                continue
            awaiting = _awaiting_operator(project_open_calls(registry, project_dir), stage=stage)
            if awaiting:
                self._skip(report, project_id, "the registry declares {} awaiting the operator: {}".format(
                    "a call" if len(awaiting) == 1 else "{} calls".format(len(awaiting)), ", ".join(awaiting)
                ))
                continue
            lane = entry.get("lane")
            if lane == "CRAFT":
                # The registry's declared holder counts against the quota even though the
                # daemon never runs it — a quarantined bespoke pipeline can hold the slot,
                # and a slot held by something this daemon cannot see is still held.
                held_by_registry = craft_holder is not None and craft_holder.lower() != project_id.lower()
                if held_by_registry:
                    self._skip(report, project_id, "the registry gives the CRAFT slot to {}".format(craft_holder))
                    continue
                if lane_load.get("CRAFT", 0) >= self.config.craft_quota:
                    self._skip(report, project_id, "the CRAFT lane quota of {} is already taken".format(self.config.craft_quota))
                    continue
            try:
                spec = job_for(project_dir, stage, revision=self.config.revision)
            except KeyError as exc:
                self._skip(report, project_id, str(exc))
                continue
            if spec.kind == HUMAN:
                self._skip(report, project_id, spec.reason)
                continue
            fingerprint = self._fingerprint(project_id)
            if jobs.is_terminally_failed(database, project_id=project_id, stage=stage, input_hash=fingerprint):
                self._skip(report, project_id, "{} already stalled at {} on this input; waiting on an answer".format(project_id, stage))
                continue
            agent = agent_session_activity(project_id, database_path=self.config.agent_session_database, now=now)
            if agent["active"]:
                self._skip(report, project_id, agent["reason"])
                continue
            taken, claim_reason = acquire_claim(self.config, project_id, owner=self.owner, lane=lane, note=stage, now=now)
            if not taken:
                self._skip(report, project_id, claim_reason)
                continue
            job_id = jobs.enqueue(
                database,
                project_id=project_id,
                stage=stage,
                kind=spec.kind,
                input_hash=fingerprint,
                reason=spec.reason,
                now=now,
            )
            if job_id is None:
                continue
            if lane:
                lane_load[lane] = lane_load.get(lane, 0) + 1
            report["scheduled"].append({"job_id": job_id, "project_id": project_id, "stage": stage, "kind": spec.kind})
            self._event("job_queued", project_id=project_id, job_id=job_id, payload={"stage": stage, "kind": spec.kind})

    def _lane_load(self, project_ids: List[str], ownership: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
        load: Dict[str, int] = {}
        for project_id in project_ids:
            lane = (ownership.get(project_id) or {}).get("lane")
            if lane and jobs.active_job(self.config.database_path, project_id) is not None:
                load[lane] = load.get(lane, 0) + 1
        return load

    # --- 4/5. claim and run one job ---------------------------------------

    def _run_one(self, report: Dict[str, Any], now: datetime) -> None:
        for row in jobs.due_jobs(self.config.database_path, now=now):
            job_id = int(row["id"])
            project_id = row["project_id"]
            project_dir = self.config.projects_root / project_id

            busy = project_is_free(self.config.projects_root, project_id)
            if busy is not None:
                jobs.defer(
                    self.config.database_path,
                    job_id,
                    reason=busy,
                    retry_after=now + timedelta(seconds=self.config.busy_backoff_seconds),
                    now=now,
                )
                self._skip(report, project_id, busy)
                self._event("project_busy", project_id=project_id, job_id=job_id, payload={"reason": busy})
                continue

            try:
                spec = job_for(project_dir, row["stage"], revision=self.config.revision)
            except KeyError as exc:
                jobs.drop(self.config.database_path, job_id)
                report["reconciled"].append({"job_id": job_id, "project_id": project_id, "reason": str(exc)})
                continue
            spec = replace(spec, job_id=job_id)

            blocked = preflight_inputs(self.config, spec, project_dir)
            if blocked is not None:
                self._withhold(report, spec, job_id, blocked, project_dir, now)
                continue

            if not jobs.claim(self.config.database_path, job_id, owner=self.owner, now=now):
                continue
            jobs.start(self.config.database_path, job_id, now=now)

            before = row["input_hash"] or self._fingerprint(project_id)
            result = self._execute(spec, project_dir)
            after = self._fingerprint(project_id)
            report["ran"] = {
                "job_id": job_id,
                "project_id": project_id,
                "stage": spec.stage,
                "kind": spec.kind,
                "status": result.status,
                "reason": result.reason,
                "progress": after != before,
            }
            report["ran"]["disposition"] = self._settle(spec, job_id, result, progressed=after != before, now=now)
            return

    def _withhold(
        self,
        report: Dict[str, Any],
        spec: JobSpec,
        job_id: int,
        blocked: Any,
        project_dir: Path,
        now: datetime,
    ) -> None:
        """Park a stage whose declared input the box will not serve (§6.5, v1.1).

        No attempt is spent. An environment-permission wall is a fact about the box, not a
        failure of the reel, and spending attempts on one costs workers, hours of wall clock,
        and a job terminally FAILED for a reason no rerun could ever change. ``FAILED`` stays for
        genuine worker failures.

        The reason names the path, the errno and — when the path is on the external volume —
        the operator card that clears it, plus any open call already covering it. The open
        calls are assembled only on the failing branch: a healthy box must not pay a
        registry read per due job for a sentence it will never print.
        """
        calls = project_open_calls(registry_report(self.config.registry_path), Path(project_dir))
        reason = blocked.describe(open_calls=calls)
        retry_after = now + timedelta(seconds=self.config.withheld_backoff_seconds)
        before = jobs.get_job(self.config.database_path, job_id) or {}
        jobs.withhold(self.config.database_path, job_id, reason=reason, retry_after=retry_after, now=now)
        self._skip(report, spec.project_id, reason)
        # Said once, like every other standing condition in this loop: a wall that lasts
        # for hours would otherwise write the same sentence into the event log
        # every retry window until it is cleared.
        if str(before.get("reason") or "") != reason or str(before.get("disposition") or "") != jobs.WITHHELD:
            self._event(
                "job_withheld",
                project_id=spec.project_id,
                job_id=job_id,
                payload={**blocked.payload(open_calls=calls), "reason": reason, "retry_after_utc": _stamp(retry_after)},
            )

    def _execute(self, spec: JobSpec, project_dir: Path) -> Any:
        if spec.kind == DETERMINISTIC:
            return self.runner.run(spec.argv, timeout_seconds=self.config.stage_timeout_seconds)
        if spec.kind == JUDGMENT:
            return self.judgment.run(spec, project_dir)
        raise AssertionError("the daemon does not execute {} stages".format(spec.kind))

    def _settle(self, spec: JobSpec, job_id: int, result: Any, *, progressed: bool, now: datetime) -> str:
        """Decide what a result means for the queue. Contention defers; no progress counts."""
        database = self.config.database_path
        parked = _capacity_retry(result)
        if parked is not None and not progressed:
            # A usage-limit wall is a fact about the box, not about the work: park until
            # the reset without spending an attempt, so a quota day cannot burn a stage's
            # retries and land it in the negative-fixture ledger as a failure (§11).
            jobs.defer(database, job_id, reason=result.reason or "headless capacity exhausted", retry_after=parked, now=now)
            self._event(
                "judgment_parked",
                project_id=spec.project_id,
                job_id=job_id,
                payload={"stage": spec.stage, "reason": result.reason, "retry_after_utc": _stamp(parked), "class": CAPACITY},
            )
            return "PARKED"
        if result.status in CONTENTION:
            jobs.defer(
                database,
                job_id,
                reason=result.reason or result.status,
                retry_after=now + timedelta(seconds=self.config.busy_backoff_seconds),
                now=now,
            )
            self._event("job_deferred", project_id=spec.project_id, job_id=job_id, payload={"stage": spec.stage, "reason": result.reason})
            return "QUEUED"
        if progressed and result.status in (PASS,) + DECLINED:
            jobs.finish(database, job_id, status="DONE", reason=result.reason, now=now)
            self._event(
                "stage_settled",
                project_id=spec.project_id,
                job_id=job_id,
                payload={"stage": spec.stage, "status": result.status, "reason": result.reason},
            )
            return "DONE"
        reason = result.reason or "{} reported {} and nothing on disk moved".format(spec.stage, result.status)
        if result.status in (PASS,) and not progressed:
            reason = "{} reported PASS but wrote nothing the daemon can verify".format(spec.stage)
        elif result.status == TIMEOUT:
            reason = result.reason or "{} exceeded its wall-clock timeout".format(spec.stage)
        outcome = jobs.fail_attempt(
            database,
            job_id,
            reason=reason,
            now=now,
            max_attempts=self.config.max_attempts,
            backoff_seconds=DEFAULT_BACKOFF_SECONDS,
            status=WITHHELD if result.status in DECLINED else "FAILED",
        )
        if outcome.get("terminal"):
            self._event(
                "call_required",
                project_id=spec.project_id,
                job_id=job_id,
                payload={
                    "stage": spec.stage,
                    "kind": spec.kind,
                    "attempts": outcome.get("attempt"),
                    "reason": reason,
                    "issue_type": "stage-stalled",
                },
            )
            return str(outcome.get("status"))
        self._event("job_retry", project_id=spec.project_id, job_id=job_id, payload={"stage": spec.stage, "reason": reason})
        return "QUEUED"

    # --- 6. wake -----------------------------------------------------------

    def _wake(self, report: Dict[str, Any], now: datetime) -> None:
        for row in jobs.due_schedules(self.config.database_path, now=now):
            fired = jobs.mark_fired(self.config.database_path, int(row["id"]), now=now)
            entry = {
                "schedule_id": int(row["id"]),
                "project_id": row["project_id"],
                "kind": row["kind"],
                "due_at_utc": row["due_at_utc"],
                "fired_at_utc": fired.get("fired_at_utc"),
                "late_seconds": fired.get("late_seconds", 0),
                "handled": False,
                "note": "capture execution lands in Task 10; the wake is recorded either way",
            }
            report["woke"].append(entry)
            self._event("schedule_fired", project_id=row["project_id"], payload=entry)

    # --- helpers -----------------------------------------------------------

    def _next_stage(self, project_id: str) -> Optional[str]:
        try:
            return ProjectState.load(self.config.projects_root / project_id / "state.json").next_stage()
        except (StateError, OSError, ValueError):
            return None

    def _fingerprint(self, project_id: str) -> str:
        return project_fingerprint(self.config.projects_root / project_id, revision=self.config.revision)

    def _skip(self, report: Dict[str, Any], project_id: str, reason: Optional[str]) -> None:
        report["skipped"].append({"project_id": project_id, "reason": reason})

    def _announce(self, reasons: List[str]) -> None:
        """Say it once. A degraded box must not fill the event log with the same sentence."""
        if reasons == self._last_reasons:
            return
        if reasons:
            self._event("degraded", payload={"reasons": reasons})
        else:
            self._event("recovered", payload={"cleared": self._last_reasons})
        self._last_reasons = list(reasons)

    def _heartbeat(self, now: datetime, flight: Dict[str, Any]) -> None:
        self.config.studio_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(
            self.config.daemon_heartbeat_path,
            {
                "schema_version": 1,
                "last_tick_utc": _stamp(now),
                "pid": os.getpid(),
                "owner": self.owner,
                "tick": self.tick_count,
                "status": flight["status"],
                "degraded_reasons": flight["degraded_reasons"],
            },
            root=self.config.studio_dir,
        )

    def _event(
        self,
        kind: str,
        *,
        project_id: Optional[str] = None,
        job_id: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            jobs.record_event(self.config.database_path, kind=kind, project_id=project_id, job_id=job_id, payload=payload)
        except Exception:  # the event log is observability, never a reason to stop working
            pass
