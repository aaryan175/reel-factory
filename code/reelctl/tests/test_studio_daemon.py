"""The orchestrator daemon core.

Every test drives real project state on disk through a fake ``reelctl`` that performs the
same mutations the subprocess would, under the same project flock. Nothing here spawns a
process, and nothing here touches the live factory tree.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from reelctl.cli import build_parser, command_new
from reelctl.locks import ProjectBusyError, project_lock
from reelctl.state import STAGES, ProjectState
from reelctl.studio import jobs
from reelctl.studio.claims import acquire_claim
from reelctl.studio.config import StudioConfig
from reelctl.studio.daemon import Orchestrator, _awaiting_operator, preflight
from reelctl.studio.runner import BLOCKED, FAIL, PASS, WITHHELD, StageResult
from reelctl.studio.stages import JUDGMENT

NOW = datetime(2025, 1, 14, 12, 0, 0, tzinfo=timezone.utc)


# --- hermetic box ----------------------------------------------------------


def _binaries(tmp_path: Path, names: Sequence[str] = ("ffmpeg", "ffprobe", "reelctl", "claude")) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        binary = directory / name
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    return directory


def _config(tmp_path: Path, **overrides: Any) -> StudioConfig:
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    _binaries(tmp_path)
    env = {
        "REEL_STUDIO_PROJECTS_ROOT": str(projects),
        "REEL_STUDIO_STORAGE_ROOT": str(storage),
        "REEL_STUDIO_DIR": str(tmp_path / "studio"),
        "REEL_STUDIO_PATH": str(tmp_path / "bin"),
        "REEL_STUDIO_MIN_FREE_BYTES": "0",
        "REEL_STUDIO_MIN_FREE_BYTES_INTERNAL": "0",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    _registry(projects)
    return StudioConfig.from_env(env)


def _registry(projects_root: Path, reels: Optional[List[Dict[str, Any]]] = None) -> Path:
    path = projects_root / "REEL_REGISTRY.json"
    path.write_text(json.dumps({"schema_version": "2.0", "reels": reels or []}), encoding="utf-8")
    return path


def _project(config: StudioConfig, project_id: str = "demo") -> Path:
    footage = config.projects_root.parent / "footage"
    footage.mkdir(parents=True, exist_ok=True)
    reference = config.projects_root.parent / "reference.mp4"
    if not reference.exists():
        reference.write_bytes(b"a reference the daemon must never guess at")
    command_new(
        argparse.Namespace(
            projects_root=config.projects_root,
            project_id=project_id,
            reference=str(reference),
            footage=str(footage),
            mode="original-montage",
            defer_analysis=True,
        )
    )
    return config.projects_root / project_id


def _author_drafts(project_dir: Path, *, revision: str = "v001") -> None:
    """Stand in for the judgment worker: write authored (non-DRAFT) inputs."""
    for relative in ("edit/feasibility.json", "edit/selection.json", "assets/assets.json"):
        path = project_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "status": "READY"}), encoding="utf-8")


def _receipt_count(project_dir: Path) -> int:
    receipts = project_dir / ".reelctl" / "receipts"
    return len(list(receipts.rglob("*.json"))) if receipts.is_dir() else 0


def _next_stage(project_dir: Path) -> Optional[str]:
    return ProjectState.load(project_dir / "state.json").next_stage()


# --- a fake reelctl that mutates real state, under the real lock -----------


class FakeReelctl:
    """Replays what the ``reelctl`` subprocess does, including taking the project lock."""

    def __init__(self, config: StudioConfig, *, fail: Sequence[str] = (), block: Sequence[str] = ()) -> None:
        self.config = config
        self.calls: List[Tuple[str, ...]] = []
        self.fail = set(fail)
        self.block = set(block)
        self.lock_was_free: List[bool] = []

    def _complete(self, project_dir: Path, stage: str, *, status: str = "PASS", reason: Optional[str] = None) -> None:
        state = ProjectState.load(project_dir / "state.json")
        relative = "artifacts/{}.json".format(stage.lower())
        path = project_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stage": stage}, sort_keys=True), encoding="utf-8")
        state.complete(stage, "input-{}".format(stage.lower()), [relative], status=status, reason=reason)

    def run(self, argv: Sequence[str], *, timeout_seconds: Optional[float] = None) -> StageResult:
        argv = tuple(argv)
        self.calls.append(argv)
        project_id = argv[2] if argv[0] in {"reference", "footage", "feasibility", "selection", "assets"} else argv[1]
        project_dir = self.config.projects_root / project_id
        try:
            with project_lock(self.config.projects_root, project_id):
                self.lock_was_free.append(True)
                return self._mutate(argv, project_dir)
        except ProjectBusyError as exc:
            self.lock_was_free.append(False)
            return StageResult(status="BUSY", argv=argv, returncode=2, payload={}, reason=str(exc))

    def _mutate(self, argv: Tuple[str, ...], project_dir: Path) -> StageResult:
        stage = {
            ("reference", "analyze"): "REFERENCE_LOCKED",
            ("footage", "index"): "FOOTAGE_INDEXED",
            ("feasibility", "lock"): "FEASIBILITY_REPORTED",
            ("selection", "validate"): "SELECTION_LOCKED",
            ("assets", "lock"): "ASSETS_LOCKED",
        }.get(argv[:2])
        if stage is None and argv[0] == "render":
            stage = "RENDERED"
        if stage is not None:
            if stage in self.fail:
                return StageResult(status=FAIL, argv=argv, returncode=2, payload={}, reason="{} engine failure".format(stage.lower()))
            if stage in self.block:
                return StageResult(status=BLOCKED, argv=argv, returncode=0, payload={}, reason="{} is blocked".format(stage.lower()))
            self._complete(project_dir, stage)
            return StageResult(status=PASS, argv=argv, returncode=0, payload={"status": "PASS"}, reason=None)
        if argv[0] == "qc":
            revision = argv[argv.index("--revision") + 1]
            self._complete(project_dir, "TECHNICAL_QC")
            self._complete(project_dir, "STRUCTURE_QC")
            watch = project_dir / "review" / "agent-visual-review-{}.json".format(revision)
            if not watch.is_file():
                self._complete(
                    project_dir,
                    "VISUAL_QC",
                    status="BLOCKED",
                    reason="no hash-bound agent visual review is recorded for this candidate",
                )
                return StageResult(
                    status=BLOCKED,
                    argv=argv,
                    returncode=0,
                    payload={"status": "BLOCKED"},
                    reason="no hash-bound agent visual review is recorded for this candidate",
                )
            self._complete(project_dir, "VISUAL_QC")
            self._complete(project_dir, "LOCAL_REVIEW_READY")
            return StageResult(status=PASS, argv=argv, returncode=0, payload={"status": "LOCAL_REVIEW_READY"}, reason=None)
        raise AssertionError("the daemon ran a command no stage plan declares: {}".format(argv))


class WatchingJudgment:
    """A judgment adapter that writes the artifact but never advances the stage itself."""

    def __init__(self, config: StudioConfig) -> None:
        self.config = config
        self.calls: List[str] = []

    def run(self, job: Any, project_dir: Path) -> StageResult:
        self.calls.append(job.stage)
        if job.stage == "BLUEPRINT_LOCKED":
            state = ProjectState.load(project_dir / "state.json")
            path = project_dir / "reference/blueprint.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"picture_blocks": []}, sort_keys=True), encoding="utf-8")
            state.complete("BLUEPRINT_LOCKED", "input-blueprint_locked", ["reference/blueprint.json"])
        elif job.stage == "VISUAL_QC":
            path = project_dir / "review/agent-visual-review-v001.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"status": "PASS"}, sort_keys=True), encoding="utf-8")
        return StageResult(status=PASS, argv=(), returncode=0, payload={}, reason=None)


class SilentJudgment:
    """A worker that claims PASS and writes nothing — the failure mode Task 7 must reject."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, job: Any, project_dir: Path) -> StageResult:
        self.calls += 1
        return StageResult(status=PASS, argv=(), returncode=0, payload={}, reason=None)


class Clock:
    """A hand-wound clock. Backoff is real time, so retries need the hands to move."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _orchestrator(config: StudioConfig, **kwargs: Any) -> Orchestrator:
    kwargs.setdefault("now", lambda: NOW)
    orchestrator = Orchestrator(config, **kwargs)
    orchestrator.start()
    return orchestrator


# --- preflight and degraded mode (§6.5) -----------------------------------


def test_preflight_passes_on_a_healthy_box(tmp_path: Path) -> None:
    report = preflight(_config(tmp_path))

    assert report["status"] == "PASS"
    assert report["degraded_reasons"] == []


def test_preflight_is_degraded_when_the_storage_volume_is_not_mounted(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(tmp_path / "not-mounted"))

    report = preflight(config)

    assert report["status"] == "DEGRADED"
    assert any(str(tmp_path / "not-mounted") in reason for reason in report["degraded_reasons"])


def test_preflight_is_degraded_when_ffmpeg_is_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (tmp_path / "bin" / "ffmpeg").unlink()

    report = preflight(config)

    assert report["status"] == "DEGRADED"
    assert any("ffmpeg" in reason for reason in report["degraded_reasons"])


def test_preflight_is_degraded_when_reelctl_itself_is_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (tmp_path / "bin" / "reelctl").unlink()

    report = preflight(config)

    assert report["status"] == "DEGRADED"
    assert any("reelctl" in reason for reason in report["degraded_reasons"])


def test_preflight_halts_on_an_unparseable_registry_and_never_rewrites_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = config.projects_root / "REEL_REGISTRY.json"
    registry.write_text("{not json", encoding="utf-8")

    report = preflight(config)

    assert report["status"] == "DEGRADED"
    assert any("REEL_REGISTRY.json" in reason for reason in report["degraded_reasons"])
    assert registry.read_text(encoding="utf-8") == "{not json"


def test_a_degraded_tick_starts_no_work_but_still_beats(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(tmp_path / "not-mounted"))
    _project(config)
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner)

    report = orchestrator.tick()

    assert report["status"] == "DEGRADED"
    assert report["scheduled"] == []
    assert report["ran"] is None
    assert runner.calls == []
    beat = json.loads(config.daemon_heartbeat_path.read_text(encoding="utf-8"))
    assert beat["status"] == "DEGRADED"
    assert beat["degraded_reasons"]
    assert beat["last_tick_utc"] == "2025-01-14T12:00:00Z"


def test_the_degraded_reason_is_announced_once_not_on_every_tick(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(tmp_path / "not-mounted"))
    orchestrator = _orchestrator(config, runner=FakeReelctl(config))

    for _ in range(5):
        orchestrator.tick()

    assert jobs.count_events(config.database_path, kind="degraded") == 1


def test_the_daemon_resumes_work_when_the_mount_comes_back(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(tmp_path / "late-mount"))
    _project(config)
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner)
    assert orchestrator.tick()["status"] == "DEGRADED"

    (tmp_path / "late-mount").mkdir()
    report = orchestrator.tick()

    assert report["status"] == "PASS"
    assert jobs.count_events(config.database_path, kind="recovered") == 1


def test_a_degraded_box_never_crashes_the_loop(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(tmp_path / "not-mounted"))
    orchestrator = _orchestrator(config, runner=FakeReelctl(config))

    report = orchestrator.run_forever(interval=0, max_ticks=3)

    assert report["ticks"] == 3
    assert report["errors"] == 0


# --- the launchd log (§10 Task 11) -----------------------------------------


def test_the_loop_logs_its_verdict_so_a_launchd_log_is_not_empty(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(tmp_path / "not-mounted"))
    lines: List[str] = []
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), log=lines.append)

    orchestrator.run_forever(interval=0, max_ticks=1)

    events = [json.loads(line) for line in lines]
    assert [event["event"] for event in events] == ["daemon_started", "tick"]
    assert events[0]["pid"] == os.getpid()
    assert events[1]["status"] == "DEGRADED"
    assert any("not mounted" in reason or "mount" in reason for reason in events[1]["degraded_reasons"])


def test_an_unchanged_verdict_is_not_repeated_on_every_tick(tmp_path: Path) -> None:
    """4,320 identical lines a day into a file nothing rotates is not a log."""
    config = _config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(tmp_path / "not-mounted"))
    lines: List[str] = []
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), log=lines.append)

    orchestrator.run_forever(interval=0, max_ticks=5)

    assert [json.loads(line)["event"] for line in lines] == ["daemon_started", "tick"]


def test_a_changed_verdict_is_logged_again(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_STORAGE_ROOT=str(tmp_path / "late-mount"))
    lines: List[str] = []
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), log=lines.append)
    orchestrator.run_forever(interval=0, max_ticks=2)

    (tmp_path / "late-mount").mkdir()
    orchestrator.run_forever(interval=0, max_ticks=2)

    statuses = [json.loads(line)["status"] for line in lines if json.loads(line)["event"] == "tick"]
    assert statuses == ["DEGRADED", "PASS"]


def test_the_log_names_the_project_it_skipped_and_why(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config, "demo")
    _registry(
        config.projects_root,
        [{"reference_shortcode": "DEMO", "project_root": "demo", "driver": "agent", "lane": "VOLUME"}],
    )
    lines: List[str] = []
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), log=lines.append)

    orchestrator.run_forever(interval=0, max_ticks=1)

    tick = [json.loads(line) for line in lines if json.loads(line)["event"] == "tick"][0]
    assert tick["scheduled"] == []
    assert [skip["project_id"] for skip in tick["skipped"]] == ["demo"]
    assert "driver is agent" in tick["skipped"][0]["reason"]


# --- the acceptance run ----------------------------------------------------


def test_a_fake_project_advances_end_to_end_through_the_deterministic_stages(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _author_drafts(project)
    runner = FakeReelctl(config)
    judgment = WatchingJudgment(config)
    orchestrator = _orchestrator(config, runner=runner, judgment=judgment)

    for _ in range(30):
        orchestrator.tick()

    assert _next_stage(project) == "HUMAN_APPROVED"
    assert [call[0] for call in runner.calls] == [
        "reference",
        "footage",
        "feasibility",
        "selection",
        "assets",
        "render",
        "qc",
        "qc",
    ]
    assert judgment.calls == ["BLUEPRINT_LOCKED", "VISUAL_QC"]
    assert all(runner.lock_was_free)


def test_the_daemon_runs_at_most_one_job_per_tick(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _author_drafts(project)
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner, judgment=WatchingJudgment(config))

    orchestrator.tick()
    assert len(runner.calls) == 1
    orchestrator.tick()
    assert len(runner.calls) == 1  # tick two is the BLUEPRINT_LOCKED judgment job


def test_the_daemon_stops_at_the_human_gate_and_never_runs_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _author_drafts(project)
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner, judgment=WatchingJudgment(config))
    for _ in range(30):
        orchestrator.tick()
    before = len(runner.calls)

    report = orchestrator.tick()

    assert len(runner.calls) == before
    assert report["ran"] is None
    assert any(item["reason"] and "HUMAN_APPROVED" in item["reason"] for item in report["skipped"])
    assert jobs.active_job(config.database_path, "demo") is None


# --- resumability (§6.4) ---------------------------------------------------


def test_killing_the_daemon_mid_stage_and_restarting_reproduces_next_stage_with_no_duplicate_receipts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _author_drafts(project)
    runner = FakeReelctl(config)
    first = _orchestrator(config, runner=runner, judgment=WatchingJudgment(config))
    first.tick()
    first.tick()
    first.tick()
    stage_before = _next_stage(project)
    receipts_before = _receipt_count(project)

    # a killed daemon leaves its claimed row behind, mid-flight
    job_id = jobs.enqueue(config.database_path, project_id="demo", stage=stage_before, kind="deterministic", input_hash="stale", now=NOW)
    jobs.claim(config.database_path, job_id, owner=first.owner, now=NOW)
    jobs.start(config.database_path, job_id, now=NOW)

    _orchestrator(config, runner=FakeReelctl(config), judgment=WatchingJudgment(config))

    assert _next_stage(project) == stage_before
    assert _receipt_count(project) == receipts_before
    assert jobs.active_job(config.database_path, "demo")["status"] == "QUEUED"


def test_a_queued_job_that_disk_says_is_already_done_is_dropped(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _author_drafts(project)
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=WatchingJudgment(config))
    jobs.enqueue(config.database_path, project_id="demo", stage="DELIVERED", kind="deterministic", input_hash="bogus", now=NOW)

    report = orchestrator.tick()

    assert any("DELIVERED" in str(item) for item in report["reconciled"])
    assert [item["stage"] for item in report["scheduled"]] == ["REFERENCE_LOCKED"]


def test_rerunning_a_completed_stage_produces_no_second_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner, judgment=WatchingJudgment(config))
    orchestrator.tick()
    receipts = _receipt_count(project)

    runner.run(("reference", "analyze", "demo"))

    assert _receipt_count(project) == receipts


# --- concurrency (§9) ------------------------------------------------------


def test_a_project_locked_by_another_process_is_skipped_with_a_named_event(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner)

    with project_lock(config.projects_root, "demo"):
        report = orchestrator.tick()

    assert runner.calls == []
    assert any("locked by another reelctl process" in str(item["reason"]) for item in report["skipped"])
    assert jobs.count_events(config.database_path, kind="project_busy") == 1


def test_a_busy_project_is_backed_off_rather_than_retried_in_a_tight_loop(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner)

    with project_lock(config.projects_root, "demo"):
        for _ in range(5):
            orchestrator.tick()

    assert runner.calls == []
    assert jobs.count_events(config.database_path, kind="project_busy") == 1
    assert jobs.active_job(config.database_path, "demo")["retry_after_utc"] > "2025-01-14T12:00:00Z"


def test_the_daemon_holds_no_lock_while_a_stage_runs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    observed: List[bool] = []

    class Probe:
        def run(self, argv: Sequence[str], *, timeout_seconds: Optional[float] = None) -> StageResult:
            try:
                with project_lock(config.projects_root, "demo"):
                    observed.append(True)
            except ProjectBusyError:
                observed.append(False)
            return StageResult(status=PASS, argv=tuple(argv), returncode=0, payload={}, reason=None)

    _orchestrator(config, runner=Probe()).tick()

    assert observed == [True]


def test_a_project_claimed_by_another_owner_is_skipped(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    acquire_claim(config, "demo", owner="agent-session-42", lane="CRAFT", now=NOW)
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert runner.calls == []
    assert any("agent-session-42" in str(item["reason"]) for item in report["skipped"])


def test_a_registry_quarantined_project_is_never_scheduled(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    _registry(
        config.projects_root,
        [{"reference_shortcode": "ref-06", "project_root": "demo/", "lane": "CRAFT", "runtime": "LEGACY_QUARANTINED_BESPOKE_PIPELINE"}],
    )
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert runner.calls == []
    assert report["scheduled"] == []
    assert any("LEGACY_QUARANTINED_BESPOKE_PIPELINE" in str(item["reason"]) for item in report["skipped"])


def test_a_quarantined_reel_with_no_project_root_is_still_skipped(tmp_path: Path) -> None:
    """ref-06 carries no project_root, so only its shortcode can link it to a directory."""
    config = _config(tmp_path)
    _project(config, "REEL-09")
    _registry(
        config.projects_root,
        [
            {
                "reference_shortcode": "REEL-09",
                "lane": "CRAFT",
                "runtime": "LEGACY_QUARANTINED_BESPOKE_PIPELINE",
                "project_root": None,
            }
        ],
    )
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert runner.calls == []
    assert report["scheduled"] == []
    assert any("LEGACY_QUARANTINED_BESPOKE_PIPELINE" in str(item["reason"]) for item in report["skipped"])


def test_the_registry_declared_craft_holder_occupies_the_lane_quota(tmp_path: Path) -> None:
    """The CRAFT slot is taken by a reel the daemon does not even run; the quota still counts it."""
    config = _config(tmp_path)
    _project(config, "craft-newcomer")
    registry = config.projects_root / "REEL_REGISTRY.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "reels": [{"reference_shortcode": "NEW", "project_root": "craft-newcomer/", "lane": "CRAFT", "driver": "daemon"}],
                "lanes": {"CRAFT": {"quota": "one active project at a time", "active": "REEL-09 (v10-r5 source-contours)"}},
            }
        ),
        encoding="utf-8",
    )
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert runner.calls == []
    assert report["scheduled"] == []
    assert any("REEL-09" in str(item["reason"]) for item in report["skipped"])


# --- the registry's daemon_contract ------------------------------------------------
#
#   "daemon drives a project iff (a) it was created via studio intake (studio.db origin)
#    OR (b) its registry entry has driver=='daemon'. Registry-absent projects are
#    studio-born and driven. driver=='agent' excludes permanently."


def test_a_reel_the_registry_marks_driver_agent_is_never_driven(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config, "reel-ref-28-v1")
    _registry(
        config.projects_root,
        [{"reference_shortcode": "REEL-10", "project_root": "reel-ref-28-v1/", "lane": "VOLUME", "driver": "agent"}],
    )
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert runner.calls == []
    assert report["scheduled"] == []
    assert any("driver" in str(item["reason"]) and "agent" in str(item["reason"]) for item in report["skipped"])


def test_driver_agent_still_excludes_after_every_call_is_answered(tmp_path: Path) -> None:
    """The exclusion is permanent; it must not evaporate when the calls clear."""
    config = _config(tmp_path)
    _project(config, "reel-ref-28-v1")
    path = config.projects_root / "REEL_REGISTRY.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "reels": [{"reference_shortcode": "REEL-10", "project_root": "reel-ref-28-v1/", "driver": "agent"}],
                "open_calls": [{"call_id": "CALL-001", "project": "reel-ref-28-v1", "state": "ANSWERED"}],
            }
        ),
        encoding="utf-8",
    )
    runner = FakeReelctl(config)

    for _ in range(5):
        _orchestrator(config, runner=runner).tick()

    assert runner.calls == []


def test_a_reel_the_registry_marks_driver_daemon_is_driven(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config, "adopted")
    _registry(config.projects_root, [{"reference_shortcode": "AD", "project_root": "adopted/", "driver": "daemon"}])
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert [item["project_id"] for item in report["scheduled"]] == ["adopted"]
    assert runner.calls == [("reference", "analyze", "adopted")]


def test_a_registry_listed_reel_with_no_driver_is_left_alone_unless_studio_born(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config, "legacy-listed")
    _registry(config.projects_root, [{"reference_shortcode": "LL", "project_root": "legacy-listed/"}])
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert runner.calls == []
    assert any("studio intake" in str(item["reason"]) for item in report["skipped"])


def test_the_same_reel_is_driven_once_studio_intake_owns_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config, "legacy-listed")
    _registry(config.projects_root, [{"reference_shortcode": "LL", "project_root": "legacy-listed/"}])
    (project / "intake.json").write_text(json.dumps({"project_id": "legacy-listed"}), encoding="utf-8")
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert [item["project_id"] for item in report["scheduled"]] == ["legacy-listed"]


# --- calls the registry declares against a project (§10 Task 8's "job release") --------
#
# A schedulable project can have three AWAITING_OPERATOR calls declared against it in the
# registry, CALL-001 marked BLOCKER. Nothing in the daemon read them, so the first
# tick would have started building the very artifacts those calls exist to shape.


def _registry_with_calls(projects_root: Path, calls: List[Dict[str, Any]], reels: Optional[List[Dict[str, Any]]] = None) -> Path:
    path = projects_root / "REEL_REGISTRY.json"
    path.write_text(json.dumps({"schema_version": "2.0", "reels": reels or [], "open_calls": calls}), encoding="utf-8")
    return path


def test_a_project_the_registry_says_is_awaiting_the_operator_is_not_driven(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    _registry_with_calls(
        config.projects_root,
        [{"call_id": "CALL-001-missing-roles", "project": "demo", "card": "demo/brain/07_CALL_PROMPTS.md", "state": "AWAITING_OPERATOR"}],
    )
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert runner.calls == []
    assert report["scheduled"] == []
    assert any("CALL-001-missing-roles" in str(item["reason"]) for item in report["skipped"])


def test_an_answered_call_releases_the_project(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    _registry_with_calls(config.projects_root, [{"call_id": "CALL-001", "project": "demo", "state": "ANSWERED"}])
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert [item["project_id"] for item in report["scheduled"]] == ["demo"]
    assert runner.calls == [("reference", "analyze", "demo")]


def test_an_unanswered_call_never_blocks_an_unrelated_reel(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config, "demo")
    _registry_with_calls(config.projects_root, [{"call_id": "CALL-001", "project": "somebody-else", "state": "AWAITING_OPERATOR"}])
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert [item["project_id"] for item in report["scheduled"]] == ["demo"]


def test_every_open_call_is_named_in_the_reason_not_just_counted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    _registry_with_calls(
        config.projects_root,
        [
            {"call_id": "CALL-001-missing-roles", "project": "demo", "state": "AWAITING_OPERATOR"},
            {"call_id": "CALL-002-craft-quota", "project": "demo", "state": "AWAITING_OPERATOR"},
            {"call_id": "CALL-003-grade-target", "project": "demo", "state": "AWAITING_OPERATOR"},
        ],
    )

    report = _orchestrator(config, runner=FakeReelctl(config)).tick()

    reason = str([item["reason"] for item in report["skipped"]])
    for call_id in ("CALL-001-missing-roles", "CALL-002-craft-quota", "CALL-003-grade-target"):
        assert call_id in reason


# --- stage-scoped gating (registry daemon_contract.calls_gate_design) -------------------
#
# "call cards carry a 'stage' field naming the first stage the decision governs; the daemon
#  gate compares call.stage vs job.stage — stages before it proceed, the named stage onward
#  waits." Supersedes the blanket gate and the issue_type exemption that preceded it.


def test_a_call_that_names_a_later_stage_lets_earlier_work_proceed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    _registry_with_calls(
        config.projects_root,
        [{"call_id": "CALL-MODE-demo", "project": "demo", "stage": "BLUEPRINT_LOCKED", "state": "AWAITING_OPERATOR"}],
    )
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert [item["project_id"] for item in report["scheduled"]] == ["demo"]
    assert runner.calls == [("reference", "analyze", "demo")]


def test_a_call_bites_from_the_stage_it_names_onward(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _author_drafts(project)
    _registry_with_calls(
        config.projects_root,
        [{"call_id": "CALL-SEL-demo", "project": "demo", "stage": "SELECTION_LOCKED", "state": "AWAITING_OPERATOR"}],
    )
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner, judgment=WatchingJudgment(config))

    for _ in range(20):
        orchestrator.tick()

    assert _next_stage(project) == "SELECTION_LOCKED"
    assert [call[0] for call in runner.calls] == ["reference", "footage", "feasibility"]


def test_a_call_naming_the_very_next_stage_stops_it_immediately(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    _registry_with_calls(
        config.projects_root,
        [{"call_id": "CALL-REF-demo", "project": "demo", "stage": "REFERENCE_LOCKED", "state": "AWAITING_OPERATOR"}],
    )
    runner = FakeReelctl(config)

    _orchestrator(config, runner=runner).tick()

    assert runner.calls == []


def test_a_call_naming_a_stage_this_build_does_not_know_gates_everything(tmp_path: Path) -> None:
    """An unrecognised gate must never read as an open door."""
    config = _config(tmp_path)
    _project(config)
    _registry_with_calls(
        config.projects_root,
        [{"call_id": "CALL-X", "project": "demo", "stage": "SOME_FUTURE_STAGE", "state": "AWAITING_OPERATOR"}],
    )
    runner = FakeReelctl(config)

    _orchestrator(config, runner=runner).tick()

    assert runner.calls == []


def test_the_gate_is_callable_without_a_stage_and_then_blocks_everything() -> None:
    """The default `stage=None` is a public surface; unknown position must fail closed.

    Unreachable from SCHEDULE (a project with no next stage is skipped earlier), so it is
    pinned directly rather than through the daemon — a mutation of the branch would
    otherwise pass the whole suite.
    """
    scoped = [{"call_id": "CALL-LATE", "stage": "DELIVERED", "state": "AWAITING_OPERATOR"}]

    assert _awaiting_operator(scoped, stage="REFERENCE_LOCKED") == []
    assert _awaiting_operator(scoped, stage=None) == ["CALL-LATE"]
    assert _awaiting_operator(scoped) == ["CALL-LATE"]
    assert _awaiting_operator(scoped, stage="NOT_A_STAGE") == ["CALL-LATE"]


def test_the_gate_compares_position_across_the_whole_stage_list() -> None:
    call = [{"call_id": "CALL-SEL", "stage": "SELECTION_LOCKED", "state": "AWAITING_OPERATOR"}]
    before = [stage for stage in STAGES if not _awaiting_operator(call, stage=stage)]
    blocked = [stage for stage in STAGES if _awaiting_operator(call, stage=stage)]

    assert before == STAGES[: STAGES.index("SELECTION_LOCKED")]
    assert blocked == STAGES[STAGES.index("SELECTION_LOCKED") :]


def test_a_scoped_call_that_is_answered_releases_the_stage_it_governed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _author_drafts(project)
    _registry_with_calls(
        config.projects_root,
        [{"call_id": "CALL-SEL-demo", "project": "demo", "stage": "SELECTION_LOCKED", "state": "ANSWERED"}],
    )
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=WatchingJudgment(config))

    for _ in range(30):
        orchestrator.tick()

    assert _next_stage(project) == "HUMAN_APPROVED"


def test_a_project_with_open_blocker_calls_is_refused(tmp_path: Path) -> None:
    """Synthetic registry records shaped like a production registry, against a matching project directory."""
    config = _config(tmp_path)
    _project(config, "reel-ref-28-v1")
    _registry_with_calls(
        config.projects_root,
        [
            {"call_id": cid, "project": "reel-ref-28-v1", "card": "reel-ref-28-v1/brain/07_CALL_PROMPTS.md", "state": "AWAITING_OPERATOR"}
            for cid in ("CALL-001-missing-roles", "CALL-002-craft-quota", "CALL-003-grade-target")
        ],
        reels=[
            {
                "reference_shortcode": "REEL-10",
                "project_root": "reel-ref-28-v1/",
                "lane": "VOLUME (mode call pending)",
                "review_state": "BOOTSTRAP_COMPLETE__CALL_REQUIRED_X3_AWAITING_OPERATOR",
            }
        ],
    )
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert runner.calls == []
    assert report["scheduled"] == []
    assert jobs.active_job(config.database_path, "reel-ref-28-v1") is None


# --- the intake mode call gates too -------------------------------------------------
#
# A studio-born reel's mode call never reaches the registry, so a registry-only gate saw
# nothing and drove it through SELECTION_LOCKED — past the stage its own card governs —
# while its record still read mode_confirmed: false.


def _intake_record(project_dir: Path, *, stage: str = "SELECTION_LOCKED", state: str = "AWAITING_OPERATOR") -> None:
    (project_dir / "intake.json").write_text(
        json.dumps(
            {
                "project_id": project_dir.name,
                "mode_confirmed": state != "ANSWERED",
                "call": {
                    "call_id": "CALL-MODE-{}".format(project_dir.name),
                    "stage": stage,
                    "state": state,
                    "issue_type": "mode-ambiguity",
                },
            }
        ),
        encoding="utf-8",
    )


def test_an_intake_mode_call_stops_the_daemon_at_the_stage_it_governs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _author_drafts(project)
    _intake_record(project)
    _registry(config.projects_root, [])
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner, judgment=WatchingJudgment(config))

    for _ in range(20):
        orchestrator.tick()

    assert _next_stage(project) == "SELECTION_LOCKED"
    assert [call[0] for call in runner.calls] == ["reference", "footage", "feasibility"]


def test_an_intake_mode_call_still_lets_the_early_stages_run(tmp_path: Path) -> None:
    """§5.1: lock the reference and run feasibility *while* the mode call is open."""
    config = _config(tmp_path)
    project = _project(config)
    _intake_record(project)
    _registry(config.projects_root, [])
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert [item["project_id"] for item in report["scheduled"]] == ["demo"]
    assert runner.calls == [("reference", "analyze", "demo")]


def test_a_confirmed_mode_releases_selection(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _author_drafts(project)
    _intake_record(project, state="ANSWERED")
    _registry(config.projects_root, [])
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=WatchingJudgment(config))

    for _ in range(30):
        orchestrator.tick()

    assert _next_stage(project) == "HUMAN_APPROVED"


def test_the_gate_reads_both_sources_not_just_the_registry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _intake_record(project, stage="REFERENCE_LOCKED")
    _registry_with_calls(
        config.projects_root,
        [{"call_id": "CALL-REG", "project": "demo", "stage": "RENDERED", "state": "AWAITING_OPERATOR"}],
    )
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert runner.calls == []
    reason = str([item["reason"] for item in report["skipped"]])
    assert "CALL-MODE-demo" in reason  # the intake card, which only the merged source sees


def test_a_project_the_registry_never_mentions_is_still_driven(tmp_path: Path) -> None:
    """A freshly intaken reel has no registry entry yet; refusing it would break Task 5."""
    config = _config(tmp_path)
    _project(config, "brand-new")
    _registry(config.projects_root, [])
    runner = FakeReelctl(config)

    report = _orchestrator(config, runner=runner).tick()

    assert [item["project_id"] for item in report["scheduled"]] == ["brand-new"]
    assert runner.calls == [("reference", "analyze", "brand-new")]


def test_the_craft_lane_quota_of_one_is_respected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config, "craft-one")
    _project(config, "craft-two")
    _registry(
        config.projects_root,
        [
            {"reference_shortcode": "A", "project_root": "craft-one/", "lane": "CRAFT", "driver": "daemon"},
            {"reference_shortcode": "B", "project_root": "craft-two/", "lane": "CRAFT", "driver": "daemon"},
        ],
    )
    orchestrator = _orchestrator(config, runner=FakeReelctl(config))

    report = orchestrator.tick()

    assert len(report["scheduled"]) == 1
    assert any("CRAFT" in str(item["reason"]) for item in report["skipped"])


# --- judgment seam ---------------------------------------------------------


def test_a_judgment_stage_withholds_when_no_adapter_is_installed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner)

    orchestrator.tick()  # REFERENCE_LOCKED, deterministic
    report = orchestrator.tick()  # BLUEPRINT_LOCKED, judgment

    assert _next_stage(project) == "BLUEPRINT_LOCKED"
    assert report["ran"]["kind"] == JUDGMENT
    assert report["ran"]["status"] == WITHHELD
    assert report["ran"]["reason"]
    assert jobs.active_job(config.database_path, "demo")["retry_after_utc"] > "2025-01-14T12:00:00Z"


def test_a_judgment_worker_that_writes_nothing_is_not_believed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    judgment = SilentJudgment()
    clock = Clock()
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=judgment, now=clock)

    for _ in range(6):
        orchestrator.tick()
        clock.advance(timedelta(minutes=20))

    assert _next_stage(project) == "BLUEPRINT_LOCKED"
    assert judgment.calls == 2
    assert jobs.count_events(config.database_path, kind="call_required") == 1


# --- the stall rule --------------------------------------------------------


def test_two_failures_on_the_same_input_raise_a_call_instead_of_looping(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    runner = FakeReelctl(config, fail=["REFERENCE_LOCKED"])
    clock = Clock()
    orchestrator = _orchestrator(config, runner=runner, now=clock)

    for _ in range(8):
        orchestrator.tick()
        clock.advance(timedelta(minutes=20))

    assert len(runner.calls) == 2
    assert jobs.count_events(config.database_path, kind="call_required") == 1
    assert jobs.active_job(config.database_path, "demo") is None


def test_a_changed_input_re_enqueues_a_stage_that_previously_failed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    runner = FakeReelctl(config, fail=["REFERENCE_LOCKED"])
    clock = Clock()
    orchestrator = _orchestrator(config, runner=runner, now=clock)
    for _ in range(4):
        orchestrator.tick()
        clock.advance(timedelta(minutes=20))
    assert len(runner.calls) == 2

    runner.fail.clear()
    (project / "edit").mkdir(parents=True, exist_ok=True)
    (project / "edit/feasibility.json").write_text(json.dumps({"status": "READY"}), encoding="utf-8")
    orchestrator.tick()

    assert len(runner.calls) == 3
    assert _next_stage(project) == "BLUEPRINT_LOCKED"


def test_a_blocked_stage_is_recorded_as_the_engines_withholding_not_a_crash(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    runner = FakeReelctl(config, block=["REFERENCE_LOCKED"])
    orchestrator = _orchestrator(config, runner=runner)

    report = orchestrator.tick()

    assert report["ran"]["status"] == BLOCKED
    assert report["ran"]["reason"] == "reference_locked is blocked"


# --- the input preflight (v1.1) --------------------------------------------
#
# Judgment workers can be spawned against a footage root the daemon's
# children have no TCC grant for. The read does not fail — it blocks in ``openat`` waiting
# for a consent prompt — and the job burns both its attempts and goes terminal on an
# environment wall. The daemon's own context gets a clean error on the same path in 30ms,
# so it now finds out *before* spawning anything, and parks WITHHELD with no attempt spent.


def _declare_footage(project_dir: Path, path: Path) -> None:
    """Repoint the project at a footage root, the way an unmounted volume would look."""
    config_path = project_dir / "project.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["footage_root"] = str(path)
    config_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def test_an_unreachable_declared_input_parks_the_job_before_any_worker_is_spawned(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    judgment = WatchingJudgment(config)
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=judgment)
    orchestrator.tick()  # REFERENCE_LOCKED, which declares no footage
    _declare_footage(project, tmp_path / "not-mounted" / "footage-library")

    report = orchestrator.tick()  # BLUEPRINT_LOCKED, judgment, footage root in its brief

    assert judgment.calls == [], "a worker was spawned against an input the daemon cannot read"
    assert report["ran"] is None
    reason = next(item["reason"] for item in report["skipped"] if item["project_id"] == "demo")
    assert str(tmp_path / "not-mounted" / "footage-library") in reason
    assert "ENOENT" in reason


def test_a_parked_job_spends_no_attempt_and_stays_out_of_the_stall_ledger(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=WatchingJudgment(config))
    orchestrator.tick()
    _declare_footage(project, tmp_path / "not-mounted" / "footage-library")

    orchestrator.tick()

    row = jobs.active_job(config.database_path, "demo")
    assert row["attempt"] == 0
    assert row["disposition"] == "WITHHELD"
    assert row["status"] == "QUEUED"
    assert row["retry_after_utc"] == "2025-01-14T12:30:00Z"
    assert jobs.count_events(config.database_path, kind="call_required") == 0


def test_a_park_repeats_without_spending_an_attempt_or_repeating_itself_in_the_log(tmp_path: Path) -> None:
    """A wall can last many hours; 48 identical events a day is not news."""
    config = _config(tmp_path)
    project = _project(config)
    judgment = WatchingJudgment(config)
    clock = Clock()
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=judgment, now=clock)
    orchestrator.tick()
    _declare_footage(project, tmp_path / "not-mounted" / "footage-library")

    for _ in range(6):
        orchestrator.tick()
        clock.advance(timedelta(minutes=31))

    assert judgment.calls == []
    assert jobs.active_job(config.database_path, "demo")["attempt"] == 0
    assert jobs.count_events(config.database_path, kind="job_withheld") == 1


def test_a_parked_job_runs_once_the_input_answers_and_the_window_passes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    judgment = WatchingJudgment(config)
    clock = Clock()
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=judgment, now=clock)
    orchestrator.tick()
    _declare_footage(project, tmp_path / "not-mounted" / "footage-library")
    orchestrator.tick()
    assert judgment.calls == []

    (tmp_path / "not-mounted" / "footage-library").mkdir(parents=True)
    clock.advance(timedelta(minutes=31))
    orchestrator.tick()

    assert judgment.calls == ["BLUEPRINT_LOCKED"]
    assert _next_stage(project) == "FOOTAGE_INDEXED"


def test_the_window_is_honoured_rather_than_retried_on_the_next_tick(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    judgment = WatchingJudgment(config)
    clock = Clock()
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=judgment, now=clock)
    orchestrator.tick()
    _declare_footage(project, tmp_path / "not-mounted" / "footage-library")
    orchestrator.tick()

    (tmp_path / "not-mounted" / "footage-library").mkdir(parents=True)
    clock.advance(timedelta(minutes=5))
    orchestrator.tick()

    assert judgment.calls == []


def test_a_deterministic_stage_is_preflighted_too(tmp_path: Path) -> None:
    """The footage indexer walks the volume itself; ``reelctl`` is no more granted than a worker."""
    config = _config(tmp_path)
    project = _project(config)
    runner = FakeReelctl(config)
    orchestrator = _orchestrator(config, runner=runner, judgment=WatchingJudgment(config))
    orchestrator.tick()  # REFERENCE_LOCKED
    orchestrator.tick()  # BLUEPRINT_LOCKED
    assert _next_stage(project) == "FOOTAGE_INDEXED"
    _declare_footage(project, tmp_path / "not-mounted" / "footage-library")

    orchestrator.tick()

    assert [call[0] for call in runner.calls] == ["reference"]
    assert jobs.active_job(config.database_path, "demo")["disposition"] == "WITHHELD"


def test_a_stage_that_never_reads_the_volume_is_not_parked_by_it(tmp_path: Path) -> None:
    """An invented blocker is worse than a missed one: the reference lock reads no footage."""
    config = _config(tmp_path)
    project = _project(config)
    runner = FakeReelctl(config)
    _declare_footage(project, tmp_path / "not-mounted" / "footage-library")

    _orchestrator(config, runner=runner, judgment=WatchingJudgment(config)).tick()

    assert [call[0] for call in runner.calls] == ["reference"]
    assert _next_stage(project) == "BLUEPRINT_LOCKED"


def test_a_readable_box_parks_nothing_and_the_reel_advances_as_before(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    _author_drafts(project)
    runner = FakeReelctl(config)
    judgment = WatchingJudgment(config)
    orchestrator = _orchestrator(config, runner=runner, judgment=judgment)

    for _ in range(30):
        orchestrator.tick()

    assert _next_stage(project) == "HUMAN_APPROVED"
    assert jobs.count_events(config.database_path, kind="job_withheld") == 0
    assert judgment.calls == ["BLUEPRINT_LOCKED", "VISUAL_QC"]


def test_the_park_event_carries_the_path_the_errno_and_the_retry_window(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=WatchingJudgment(config))
    orchestrator.tick()
    _declare_footage(project, tmp_path / "not-mounted" / "footage-library")

    orchestrator.tick()

    parked = [item for item in jobs.events_since(config.database_path) if item["kind"] == "job_withheld"]
    assert len(parked) == 1
    payload = parked[0]["payload"]
    assert payload["path"] == str(tmp_path / "not-mounted" / "footage-library")
    assert payload["code"] == "ENOENT"
    assert payload["stage"] == "BLUEPRINT_LOCKED"
    assert payload["retry_after_utc"] == "2025-01-14T12:30:00Z"
    assert payload["class"] == "input_preflight"


def test_an_open_call_already_covering_the_wall_is_named_in_the_park_reason(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), judgment=WatchingJudgment(config))
    orchestrator.tick()
    _declare_footage(project, tmp_path / "not-mounted" / "footage-library")
    _registry_with_calls(
        config.projects_root,
        [
            {
                "call_id": "CALL-TCC-WORKDRIVE-VOLUME",
                "project": "demo",
                "state": "AWAITING_OPERATOR",
                "issue_type": "environment-permission",
                # The card governs the stages that read footage, so it does not gate the
                # blueprint by itself — the park has to name it anyway, or the operator
                # sees two separate problems where there is one.
                "stage": "RENDERED",
            }
        ],
    )

    report = orchestrator.tick()

    reason = next(item["reason"] for item in report["skipped"] if item["project_id"] == "demo")
    assert "CALL-TCC-WORKDRIVE-VOLUME" in reason


# --- wakes (§8.4) ----------------------------------------------------------


def test_a_due_schedule_fires_late_but_never_silently(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _project(config)
    clock = Clock()
    orchestrator = _orchestrator(config, runner=FakeReelctl(config), now=clock)
    jobs.schedule(
        config.database_path,
        project_id="demo",
        kind="metric_capture",
        due_at=NOW + timedelta(hours=24),
        payload={"snapshot": "24h"},
        now=NOW,
    )

    assert orchestrator.tick()["woke"] == []

    clock.advance(timedelta(hours=30))
    report = orchestrator.tick()

    assert len(report["woke"]) == 1
    assert report["woke"][0]["late_seconds"] == 6 * 3600
    assert report["woke"][0]["kind"] == "metric_capture"
    assert jobs.count_events(config.database_path, kind="schedule_fired") == 1


# --- the loop never crash-loops -------------------------------------------


def test_a_tick_that_raises_is_recorded_and_the_loop_carries_on(tmp_path: Path) -> None:
    config = _config(tmp_path)
    orchestrator = _orchestrator(config, runner=FakeReelctl(config))
    ticks = {"n": 0}

    def exploding() -> Dict[str, Any]:
        ticks["n"] += 1
        raise RuntimeError("the disk went away mid-tick")

    orchestrator.tick = exploding  # type: ignore[method-assign]

    report = orchestrator.run_forever(interval=0, max_ticks=3)

    assert ticks["n"] == 3
    assert report["errors"] == 3
    assert jobs.count_events(config.database_path, kind="tick_error") == 3


def test_run_forever_stops_when_asked(tmp_path: Path) -> None:
    config = _config(tmp_path)
    orchestrator = _orchestrator(config, runner=FakeReelctl(config))
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    report = orchestrator.run_forever(interval=0, stop=stop)

    assert report["ticks"] == 2


# --- CLI wiring ------------------------------------------------------------


def test_the_cli_exposes_reelctl_studio_daemon(tmp_path: Path) -> None:
    args = build_parser().parse_args(["--projects-root", str(tmp_path), "studio", "daemon", "--once"])

    assert args.once is True
    assert not hasattr(args, "project_id")  # a project lock must never wrap the daemon
    assert callable(args.func)


def test_the_daemon_command_runs_a_single_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    for key, value in {
        "REEL_STUDIO_PROJECTS_ROOT": str(config.projects_root),
        "REEL_STUDIO_STORAGE_ROOT": str(config.storage_root),
        "REEL_STUDIO_DIR": str(config.studio_dir),
        "REEL_STUDIO_PATH": str(tmp_path / "bin"),
        "REEL_STUDIO_MIN_FREE_BYTES": "0",
        "REEL_STUDIO_MIN_FREE_BYTES_INTERNAL": "0",
    }.items():
        monkeypatch.setenv(key, value)

    args = build_parser().parse_args(["--projects-root", str(config.projects_root), "studio", "daemon", "--once"])
    result = args.func(args)

    assert result["status"] in {"PASS", "DEGRADED"}
    assert result["tick"] == 1
