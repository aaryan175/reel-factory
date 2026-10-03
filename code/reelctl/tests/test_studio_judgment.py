"""The judgment adapter.

Every test here either mocks ``subprocess.run`` or spawns a **stub** ``claude`` script
written into the test's own ``bin`` directory. Nothing in this file can reach the real
``claude`` binary: the adapter resolves its worker through ``StudioConfig.tool_path``,
which every test points at ``tmp_path/bin``.

The properties under test are the ones the architecture calls non-negotiable:

* artifacts-only authority — the worker's prose never advances anything;
* the worker cannot advance state — the adapter makes the ``reelctl`` call itself, and
  only after the artifact validates;
* the frozen contract's sha256 is recorded on every job;
* a capacity exit ("session limit resets 5pm") parks the job, it never fails it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import pytest

from reelctl import __version__
from reelctl.cli import build_parser, command_new
from reelctl.hashing import sha256_file
from reelctl.state import ProjectState
from reelctl.studio import jobs
from reelctl.studio.config import StudioConfig
from reelctl.studio.daemon import Orchestrator
from reelctl.studio.judgment import (
    BRAIN_PACK,
    CAPACITY,
    DISALLOWED_TOOLS,
    FIRST_PROGRESS,
    JUDGMENT_STAGES,
    LIVENESS,
    STALL,
    VERIFIED_CLI_FLAGS,
    VISUAL_WATCH_RELATIVE,
    HeadlessJudgmentAdapter,
    LivenessWatch,
    WorkerNotAlive,
    capacity_park,
    capacity_signal,
    checkpoint,
    clear_capacity,
    encode_cwd,
    freeze_contract,
    judgment_adapter,
    park_capacity,
    supervised_run,
    worker_transcript_path,
)
from reelctl.studio.runner import FAIL, PASS, TIMEOUT, UNAVAILABLE, WITHHELD, StageResult, UnavailableJudgmentAdapter
from reelctl.studio.stages import JUDGMENT, JobSpec, plan_for

NOW = datetime(2030, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
CONTRACT_TEXT = "# REEL-BRAIN / FULL-FORENSIC-ONBOARDING v1.1\n\nwrite_policy: BRAIN_PACK_ONLY\nrender_authorized: false\n"


# --- a hermetic box --------------------------------------------------------


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
    contract = projects / "REEL-BRAIN-FULL-FORENSIC-ONBOARDING-PROMPT.md"
    if not contract.exists():
        contract.write_text(CONTRACT_TEXT, encoding="utf-8")
    (projects / "REEL_REGISTRY.json").write_text(json.dumps({"schema_version": "2.0", "reels": []}), encoding="utf-8")
    env = {
        "REEL_STUDIO_PROJECTS_ROOT": str(projects),
        "REEL_STUDIO_STORAGE_ROOT": str(storage),
        "REEL_STUDIO_DIR": str(tmp_path / "studio"),
        "REEL_STUDIO_PATH": str(tmp_path / "bin"),
        "REEL_STUDIO_MIN_FREE_BYTES": "0",
        "REEL_STUDIO_MIN_FREE_BYTES_INTERNAL": "0",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return StudioConfig.from_env(env)


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


def _job(stage: str = "BLUEPRINT_LOCKED", *, project_id: str = "demo", job_id: Optional[int] = 7, **kwargs: Any) -> JobSpec:
    return JobSpec(project_id=project_id, stage=stage, kind=JUDGMENT, job_id=job_id, **kwargs)


class Worker:
    """Stand-in for ``subprocess.run``: records the spawn, replays a canned worker."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: Optional[str] = None,
        stderr: str = "",
        raises: Optional[Exception] = None,
        writes: Optional[Any] = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout if stdout is not None else json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"})
        self.stderr = stderr
        self.raises = raises
        self.writes = writes
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> Any:
        self.calls.append({"argv": list(argv), **kwargs})
        if self.writes is not None:
            self.writes()
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(args=list(argv), returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)

    @property
    def argv(self) -> List[str]:
        return self.calls[0]["argv"]


class Reelctl:
    """Stand-in for the ``StageRunner`` the adapter uses for its own advancing call."""

    def __init__(self, *, status: str = PASS, reason: Optional[str] = None) -> None:
        self.calls: List[List[str]] = []
        self.status = status
        self.reason = reason

    def run(self, argv: Sequence[str], *, timeout_seconds: Optional[float] = None) -> StageResult:
        self.calls.append([str(item) for item in argv])
        return StageResult(status=self.status, argv=tuple(str(item) for item in argv), returncode=0, payload={"status": self.status}, reason=self.reason)


def _adapter(config: StudioConfig, worker: Optional[Worker] = None, reelctl: Optional[Reelctl] = None, **kwargs: Any) -> HeadlessJudgmentAdapter:
    kwargs.setdefault("now", lambda: NOW)
    return HeadlessJudgmentAdapter(config, runner=reelctl or Reelctl(), run=worker or Worker(), **kwargs)


# --- artifact writers the tests share --------------------------------------


def _write_brain_pack(project_dir: Path, files: Sequence[str] = BRAIN_PACK) -> None:
    brain = project_dir / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    for name in files:
        payload = "{}" if name.endswith(".json") else "# {}\n".format(name)
        (brain / name).write_text(payload, encoding="utf-8")


def _blueprint_input(
    project_dir: Path,
    *,
    boundaries: Sequence[int] = (12,),
    all_frames_reviewed: bool = True,
    observations: Optional[Dict[str, Any]] = None,
    status: str = "AUTHORED",
) -> Path:
    observations = (
        observations
        if observations is not None
        else {
            "p001": {"role": "opening skyline", "description": "wide night skyline, camera static", "evidence_frames": [0, 6], "transition_from_previous": "opening"},
            "p002": {"role": "street level", "description": "handheld push through neon crowd", "evidence_frames": [13], "transition_from_previous": "hard_cut"},
        }
    )
    payload = {
        "schema_version": 1,
        "status": status,
        "project_id": project_dir.name,
        "reference_sha256": "a" * 64,
        "all_frames_reviewed": all_frames_reviewed,
        "boundaries_after": list(boundaries),
        "hard_cuts_after": list(boundaries),
        "observations": observations,
    }
    path = project_dir / "brain" / "BLUEPRINT_INPUT.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _render_receipt(project_dir: Path, *, revision: str = "v001", candidate: str = "b" * 64) -> Path:
    path = project_dir / "edit" / "render-{}".format(revision) / "render-receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"review": {"sha256": candidate}}, sort_keys=True), encoding="utf-8")
    return path


def _visual_watch(
    project_dir: Path,
    *,
    revision: str = "v001",
    status: str = "PASS",
    candidate: str = "b" * 64,
    flags: bool = True,
    notes: str = "watched end to end at normal speed against the reference board",
) -> Path:
    payload = {
        "schema_version": 1,
        "status": status,
        "candidate_sha256": candidate,
        "normal_speed_full_watch": flags,
        "reference_side_by_side_checked": flags,
        "typography_checked": flags,
        "color_checked": flags,
        "cut_and_beat_checked": flags,
        "notes": notes,
    }
    path = project_dir / "review" / "agent-visual-watch-{}.json".format(revision)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _selection_draft(project_dir: Path, *, status: str = "PASS") -> Path:
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "slots": [
            {
                "block_id": "p001",
                "frames": 12,
                "reference_role": "opening skyline",
                "candidate_observation": "night skyline, static tripod, no people",
                "source_path": "/authorized/footage/skyline.mov",
                "source_start_frame": 0,
                "speed": 1.0,
                "reverse": False,
                "input_profile": "sony_slog3_sgamut3cine",
                "input_range": "full",
                "profile_proof": {
                    "schema_version": 1,
                    "status": "AGENT_VERIFIED",
                    "method": "container metadata plus waveform check",
                    "source_sha256": "d" * 64,
                    "input_range": "full",
                    "evidence": "ffprobe reports slog3 primaries; waveform sits at log black",
                    "signature": {},
                },
                "technical_transform": "slog3_to_bt709",
                "creative": {"exposure_stops": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0},
                "grade_proof": {"status": "AGENT_REVIEWED"},
                "lighting_family": "night-exterior",
            }
        ],
    }
    path = project_dir / "edit" / "selection.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


# --- 1. the frozen contract ------------------------------------------------


def test_the_contract_is_frozen_by_its_own_sha256(tmp_path: Path) -> None:
    config = _config(tmp_path)

    frozen = freeze_contract(config.judgment_contract_path)

    assert frozen.sha256 == sha256_file(config.judgment_contract_path)
    assert frozen.text == CONTRACT_TEXT
    assert frozen.path == config.judgment_contract_path


def test_a_changed_contract_is_a_different_contract(tmp_path: Path) -> None:
    config = _config(tmp_path)
    before = freeze_contract(config.judgment_contract_path).sha256
    config.judgment_contract_path.write_text(CONTRACT_TEXT + "one more sentence\n", encoding="utf-8")

    assert freeze_contract(config.judgment_contract_path).sha256 != before


def test_a_missing_contract_withholds_by_name_and_spawns_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.judgment_contract_path.unlink()
    project_dir = _project(config)
    worker = Worker()

    result = _adapter(config, worker).run(_job(), project_dir)

    assert result.status == WITHHELD
    assert "contract" in (result.reason or "")
    assert str(config.judgment_contract_path) in (result.reason or "")
    assert worker.calls == []


def test_the_contract_sha_is_recorded_on_every_job(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)

    result = _adapter(config).run(_job(), project_dir)

    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["contract"]["sha256"] == sha256_file(config.judgment_contract_path)
    assert receipt["contract"]["path"] == str(config.judgment_contract_path)
    assert receipt["reelctl_version"] == __version__


# --- 2. the invocation -----------------------------------------------------


def test_the_invocation_uses_only_flags_this_claude_build_has(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    flags = [item for item in worker.argv if item.startswith("-")]
    assert flags, "the adapter spawned no flags at all"
    for flag in flags:
        assert flag in VERIFIED_CLI_FLAGS, "{} is not a verified flag of claude 2.1.232".format(flag)


def test_the_invocation_invents_neither_max_turns_nor_a_system_prompt_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    assert "--max-turns" not in worker.argv
    assert "--system-prompt-file" not in worker.argv
    assert "--append-system-prompt-file" not in worker.argv


def test_the_worker_is_the_claude_on_the_studio_tool_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    assert worker.argv[0] == str(tmp_path / "bin" / "claude")


def test_a_missing_claude_binary_defers_rather_than_failing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (tmp_path / "bin" / "claude").unlink()
    project_dir = _project(config)
    worker = Worker()

    result = _adapter(config, worker).run(_job(), project_dir)

    assert result.status == UNAVAILABLE
    assert "claude" in (result.reason or "")
    assert worker.calls == []


def test_the_run_is_bounded_by_a_wall_clock_timeout(tmp_path: Path) -> None:
    config = _config(tmp_path, REEL_STUDIO_JUDGMENT_TIMEOUT_SECONDS=1800)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    assert worker.calls[0]["timeout"] == 1800


def test_a_worker_that_outruns_its_timeout_is_killed_and_reported(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker(raises=subprocess.TimeoutExpired(cmd="claude", timeout=1800))

    result = _adapter(config, worker).run(_job(), project_dir)

    assert result.status == TIMEOUT
    assert "timeout" in (result.reason or "").lower()


def test_path_is_explicit_and_never_inherited(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    assert worker.calls[0]["env"]["PATH"] == str(tmp_path / "bin")


def test_cwd_is_the_factory_root_so_the_operator_invariants_load(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    assert worker.calls[0]["cwd"] == str(config.projects_root)


def test_the_frozen_contract_is_appended_as_the_system_prompt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    appended = worker.argv[worker.argv.index("--append-system-prompt") + 1]
    assert appended == CONTRACT_TEXT


def test_the_worker_is_denied_the_tools_that_could_advance_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    denied = worker.argv[worker.argv.index("--disallowedTools") + 1].split(",")
    assert set(denied) == set(DISALLOWED_TOOLS)
    assert any(entry.startswith("Bash(reelctl") for entry in denied)
    assert "WebFetch" in denied and "WebSearch" in denied


def test_the_deny_list_is_one_comma_joined_argument_so_it_cannot_swallow_the_brief(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    index = worker.argv.index("--disallowedTools")
    assert not worker.argv[index + 2].startswith("Bash("), "a second value here would be eaten as another tool"
    assert worker.argv[-1] != worker.argv[index + 1]


def test_the_brief_is_the_last_argument_and_no_variadic_flag_precedes_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    assert worker.argv[-3] == "--session-id"
    uuid.UUID(worker.argv[-2])
    assert "BLUEPRINT_LOCKED" in worker.argv[-1]


def test_the_session_id_is_a_real_uuid_so_the_transcript_can_be_found(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    result = _adapter(config, worker).run(_job(), project_dir)

    session_id = worker.argv[worker.argv.index("--session-id") + 1]
    uuid.UUID(session_id)
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["invocation"]["session_id"] == session_id


def test_the_project_directory_is_added_to_the_workers_reach(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    assert worker.argv[worker.argv.index("--add-dir") + 1] == str(project_dir)


def test_the_brief_names_the_project_the_phase_and_the_exact_output_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    brief = worker.argv[-1]
    assert "demo" in brief
    assert "phase: BOOTSTRAP" in brief
    assert "write_policy: BRAIN_PACK_ONLY" in brief
    assert "render_authorized: false" in brief
    assert "external_actions: NONE" in brief
    assert "brain/BLUEPRINT_INPUT.json" in brief


def test_the_brief_states_the_refusal_conditions_rather_than_demanding_a_pass(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    brief = worker.argv[-1]
    assert "CALL_REQUIRED" in brief
    assert "prose" in brief.lower()


def test_the_brief_carries_the_projects_own_reference_and_footage_roots(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    brief = worker.argv[-1]
    assert str(config.projects_root.parent / "footage") in brief
    assert "reference" in brief


# --- 3. artifacts are the only authority -----------------------------------


def test_a_worker_that_writes_a_valid_artifact_advances_the_stage(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    worker = Worker(writes=lambda: (_write_brain_pack(project_dir), _blueprint_input(project_dir)))

    result = _adapter(config, worker, reelctl).run(_job(), project_dir)

    assert result.status == PASS
    assert reelctl.calls, "the adapter never made the advancing reelctl call"
    argv = reelctl.calls[0]
    assert argv[:2] == ["blueprint", "lock"]
    assert argv[2] == "demo"
    assert "--all-frames-reviewed" in argv
    assert argv[argv.index("--boundaries") + 1] == "12"
    observations = Path(argv[argv.index("--observations") + 1])
    assert json.loads(observations.read_text(encoding="utf-8"))["p001"]["role"] == "opening skyline"


def test_a_worker_that_writes_an_invalid_artifact_is_rejected_and_the_stage_does_not_advance(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()

    def _broken() -> None:
        _write_brain_pack(project_dir)
        (project_dir / "brain" / "BLUEPRINT_INPUT.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    worker = Worker(writes=_broken)

    result = _adapter(config, worker, reelctl).run(_job(), project_dir)

    assert result.status == FAIL
    assert reelctl.calls == []
    assert "judgment-blueprint-input" in (result.reason or "")


def test_a_worker_that_prints_pass_but_writes_nothing_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    worker = Worker(stdout=json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "PASS — blueprint locked, all frames reviewed"}))

    result = _adapter(config, worker, reelctl).run(_job(), project_dir)

    assert result.status == FAIL
    assert reelctl.calls == []
    assert "wrote" in (result.reason or "")


def test_worker_prose_has_no_authority_over_what_is_on_disk(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    _render_receipt(project_dir)
    worker = Worker(
        stdout=json.dumps({"type": "result", "is_error": False, "result": "I watched it end to end and it is a clean PASS"}),
        writes=lambda: _visual_watch(project_dir, status="FAIL", notes="typography drifts at p004; the kerning is not the reference's"),
    )

    result = _adapter(config, worker, reelctl).run(_job("VISUAL_QC"), project_dir)

    assert result.status == WITHHELD
    assert "typography drifts at p004" in (result.reason or "")
    argv = reelctl.calls[0]
    assert argv[:2] == ["review", "record-agent"]
    assert argv[argv.index("--status") + 1] == "FAIL"


def test_a_nonzero_worker_exit_is_a_failure_even_if_a_file_appeared(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    worker = Worker(returncode=1, stderr="worker crashed", writes=lambda: (_write_brain_pack(project_dir), _blueprint_input(project_dir)))

    result = _adapter(config, worker, reelctl).run(_job(), project_dir)

    assert result.status == FAIL
    assert reelctl.calls == []
    assert "worker crashed" in (result.reason or "")


def test_an_unparseable_worker_result_is_never_read_as_a_pass(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker(stdout="not json at all", writes=lambda: (_write_brain_pack(project_dir), _blueprint_input(project_dir)))

    result = _adapter(config, worker).run(_job(), project_dir)

    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["worker"]["result_parsed"] is False
    assert result.status == PASS  # the artifact is the authority, not the prose


def test_the_blueprint_is_not_locked_when_the_worker_will_not_attest_all_frames_reviewed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    worker = Worker(writes=lambda: (_write_brain_pack(project_dir), _blueprint_input(project_dir, all_frames_reviewed=False)))

    result = _adapter(config, worker, reelctl).run(_job(), project_dir)

    assert result.status == WITHHELD
    assert "all frames" in (result.reason or "").lower()
    assert reelctl.calls == []


def test_an_incomplete_brain_pack_is_named_file_by_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    worker = Worker(writes=lambda: (_write_brain_pack(project_dir, BRAIN_PACK[:5]), _blueprint_input(project_dir)))

    result = _adapter(config, worker, reelctl).run(_job(), project_dir)

    assert result.status == FAIL
    assert "05_SELECTION_SHORTLIST.md" in (result.reason or "")
    assert reelctl.calls == []


def test_an_empty_brain_pack_file_does_not_count_as_written(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)

    def _hollow() -> None:
        _write_brain_pack(project_dir)
        (project_dir / "brain" / "08_DECISION_LOG.md").write_text("", encoding="utf-8")
        _blueprint_input(project_dir)

    result = _adapter(config, Worker(writes=_hollow), Reelctl()).run(_job(), project_dir)

    assert result.status == FAIL
    assert "08_DECISION_LOG.md" in (result.reason or "")


def test_the_visual_watch_must_bind_to_the_candidate_that_was_actually_rendered(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    _render_receipt(project_dir, candidate="b" * 64)
    worker = Worker(writes=lambda: _visual_watch(project_dir, candidate="c" * 64))

    result = _adapter(config, worker, reelctl).run(_job("VISUAL_QC"), project_dir)

    assert result.status == FAIL
    assert reelctl.calls == []
    assert "c" * 64 in (result.reason or "")


def test_a_passing_watch_missing_an_inspection_flag_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    _render_receipt(project_dir)

    def _sloppy() -> None:
        path = _visual_watch(project_dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["color_checked"] = False
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    result = _adapter(config, Worker(writes=_sloppy), reelctl).run(_job("VISUAL_QC"), project_dir)

    assert result.status == FAIL
    assert reelctl.calls == []
    assert "color_checked" in (result.reason or "")


def test_a_passing_watch_carries_all_five_inspection_flags_into_the_record(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    _render_receipt(project_dir)
    worker = Worker(writes=lambda: _visual_watch(project_dir))

    result = _adapter(config, worker, reelctl).run(_job("VISUAL_QC"), project_dir)

    assert result.status == PASS
    argv = reelctl.calls[0]
    for flag in (
        "--normal-speed-full-watch",
        "--reference-side-by-side-checked",
        "--typography-checked",
        "--color-checked",
        "--cut-and-beat-checked",
    ):
        assert flag in argv
    assert argv[argv.index("--revision") + 1] == config.revision


def test_a_watch_with_no_render_receipt_withholds_instead_of_guessing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    worker = Worker(writes=lambda: _visual_watch(project_dir))

    result = _adapter(config, worker, reelctl).run(_job("VISUAL_QC"), project_dir)

    assert result.status == WITHHELD
    assert "render receipt" in (result.reason or "").lower()
    assert reelctl.calls == []


def test_a_draft_authoring_stage_is_validated_against_its_own_schema(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    worker = Worker(writes=lambda: _selection_draft(project_dir))
    job = _job("SELECTION_LOCKED", drafts=(("edit/selection.json", "MISSING"),))

    result = _adapter(config, worker, reelctl).run(job, project_dir)

    assert result.status == PASS
    assert reelctl.calls == [], "the deterministic pass owns selection validate, not the adapter"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["artifacts"][0]["schema"] == "selection"
    assert len(receipt["artifacts"][0]["schema_sha256"]) == 64


def test_a_draft_left_at_status_draft_is_not_accepted_as_authored(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker(writes=lambda: _selection_draft(project_dir, status="DRAFT"))
    job = _job("SELECTION_LOCKED", drafts=(("edit/selection.json", "DRAFT"),))

    result = _adapter(config, worker).run(job, project_dir)

    assert result.status == FAIL
    assert "DRAFT" in (result.reason or "")


def test_a_stage_with_no_judgment_contract_withholds_by_name(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    result = _adapter(config, worker).run(_job("RENDERED"), project_dir)

    assert result.status == WITHHELD
    assert "RENDERED" in (result.reason or "")
    assert worker.calls == []


def test_a_failing_advance_call_is_reported_with_the_engines_own_sentence(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl(status=FAIL, reason="every hard cut must also be a picture-state boundary")
    worker = Worker(writes=lambda: (_write_brain_pack(project_dir), _blueprint_input(project_dir)))

    result = _adapter(config, worker, reelctl).run(_job(), project_dir)

    assert result.status == FAIL
    assert "every hard cut must also be a picture-state boundary" in (result.reason or "")


# --- 4. capacity: the usage-limit wall -------------------------------------


def test_the_session_limit_line_is_read_as_capacity_with_its_own_reset_time(tmp_path: Path) -> None:
    reset = int(datetime(2030, 1, 15, 19, 0, tzinfo=timezone.utc).timestamp())

    signal = capacity_signal("Claude AI usage limit reached|{}".format(reset), now=NOW)

    assert signal is not None
    assert signal["class"] == CAPACITY
    assert signal["retry_after_utc"] == "2030-01-15T19:00:00Z"


def test_a_prose_reset_time_is_parsed_into_a_retry_after(tmp_path: Path) -> None:
    signal = capacity_signal("5-hour limit reached ∙ resets 5pm", now=NOW)

    assert signal is not None
    assert signal["class"] == CAPACITY
    assert signal["retry_after_utc"] > "2030-01-15T12:00:00Z"


def test_an_unparseable_limit_message_still_parks_with_a_bounded_retry(tmp_path: Path) -> None:
    signal = capacity_signal("Claude AI usage limit reached", now=NOW)

    assert signal is not None
    assert signal["retry_after_utc"] > "2030-01-15T12:00:00Z"


def test_ordinary_worker_prose_is_not_mistaken_for_a_capacity_wall(tmp_path: Path) -> None:
    assert capacity_signal("the reference has a limit of four hard cuts", now=NOW) is None
    assert capacity_signal("", now=NOW) is None


def test_a_reset_beyond_the_horizon_is_clamped(tmp_path: Path) -> None:
    far = int(datetime(2031, 1, 1, tzinfo=timezone.utc).timestamp())

    signal = capacity_signal("Claude AI usage limit reached|{}".format(far), now=NOW, max_park_seconds=3600)

    assert signal["retry_after_utc"] == "2030-01-15T13:00:00Z"


def test_a_quota_exit_parks_the_job_and_is_never_recorded_as_a_failure(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reset = int(datetime(2030, 1, 15, 19, 0, tzinfo=timezone.utc).timestamp())
    worker = Worker(returncode=1, stderr="Claude AI usage limit reached|{}".format(reset))

    result = _adapter(config, worker).run(_job(), project_dir)

    assert result.status == WITHHELD
    assert result.status != FAIL
    assert result.payload["class"] == CAPACITY
    assert result.payload["retry_after_utc"] == "2030-01-15T19:00:00Z"
    assert "capacity" in (result.reason or "").lower()


def test_a_capacity_exit_is_remembered_so_the_next_job_burns_no_subprocess(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reset = int(datetime(2030, 1, 15, 19, 0, tzinfo=timezone.utc).timestamp())
    first = Worker(returncode=1, stderr="Claude AI usage limit reached|{}".format(reset))
    _adapter(config, first).run(_job(), project_dir)

    second = Worker()
    result = _adapter(config, second).run(_job("SELECTION_LOCKED", drafts=(("edit/selection.json", "MISSING"),)), project_dir)

    assert second.calls == []
    assert result.status == WITHHELD
    assert result.payload["retry_after_utc"] == "2030-01-15T19:00:00Z"


def test_the_park_expires_on_its_own(tmp_path: Path) -> None:
    config = _config(tmp_path)
    park_capacity(config, until=NOW + timedelta(hours=1), reason="session limit", now=NOW)

    assert capacity_park(config, now=NOW) is not None
    assert capacity_park(config, now=NOW + timedelta(hours=2)) is None


def test_a_completed_run_clears_a_stale_park(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    park_capacity(config, until=NOW - timedelta(minutes=1), reason="an old wall", now=NOW - timedelta(hours=2))
    worker = Worker(writes=lambda: (_write_brain_pack(project_dir), _blueprint_input(project_dir)))

    _adapter(config, worker, Reelctl()).run(_job(), project_dir)

    assert capacity_park(config, now=NOW) is None
    assert worker.calls, "an expired park must not stop the next job"


def test_clearing_a_park_that_was_never_set_is_not_an_error(tmp_path: Path) -> None:
    config = _config(tmp_path)

    clear_capacity(config)

    assert capacity_park(config, now=NOW) is None


# --- 5. checkpointing ------------------------------------------------------


def test_a_fresh_bootstrap_reports_no_checkpoint(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)

    state = checkpoint(project_dir)

    assert state["present"] == []
    assert state["missing"] == list(BRAIN_PACK)
    assert state["resume_at"] == BRAIN_PACK[0]


def test_a_half_finished_brain_pack_resumes_at_the_first_missing_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    _write_brain_pack(project_dir, BRAIN_PACK[:7])

    state = checkpoint(project_dir)

    assert state["resume_at"] == BRAIN_PACK[7]
    assert len(state["present"]) == 7
    assert all(len(entry["sha256"]) == 64 for entry in state["present"])


def test_the_brief_tells_a_resumed_worker_what_it_already_has(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    _write_brain_pack(project_dir, BRAIN_PACK[:7])
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    brief = worker.argv[-1]
    assert BRAIN_PACK[7] in brief
    assert "00_PROJECT_BRAIN.md" in brief


def test_a_complete_brain_pack_asks_for_re_verification_not_a_restart(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    _write_brain_pack(project_dir)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    brief = worker.argv[-1]
    assert "the pack is complete" in brief
    assert "start at" not in brief


def test_the_receipt_records_the_checkpoint_on_both_sides_of_the_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    _write_brain_pack(project_dir, BRAIN_PACK[:7])
    worker = Worker(writes=lambda: (_write_brain_pack(project_dir), _blueprint_input(project_dir)))

    result = _adapter(config, worker, Reelctl()).run(_job(), project_dir)

    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["checkpoint"]["before"]["resume_at"] == BRAIN_PACK[7]
    assert receipt["checkpoint"]["after"]["resume_at"] is None


# --- 6. receipts -----------------------------------------------------------


def test_every_job_lands_a_receipt_under_the_studio_jobs_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)

    result = _adapter(config).run(_job(job_id=41), project_dir)

    receipt_path = Path(result.payload["receipt"])
    assert receipt_path == config.job_receipts_dir / "41.json"
    assert receipt_path.parent == config.projects_root / "_receipts" / "studio" / "jobs"
    assert receipt_path.is_file()


def test_a_job_with_no_queue_id_still_gets_a_traceable_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)

    result = _adapter(config).run(_job(job_id=None), project_dir)

    receipt_path = Path(result.payload["receipt"])
    assert receipt_path.is_file()
    assert "demo" in receipt_path.name and "BLUEPRINT_LOCKED" in receipt_path.name


def test_the_receipt_records_the_exact_argv_that_was_spawned(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker()

    result = _adapter(config, worker).run(_job(), project_dir)

    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["invocation"]["argv"] == worker.argv
    assert receipt["invocation"]["cwd"] == str(config.projects_root)
    assert receipt["invocation"]["path"] == str(tmp_path / "bin")
    assert receipt["invocation"]["timeout_seconds"] == config.judgment_timeout_seconds


def test_the_worker_log_is_stored_as_a_log_with_no_authority(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker(stdout=json.dumps({"type": "result", "result": "I am certain this passes"}))

    result = _adapter(config, worker).run(_job(job_id=41), project_dir)

    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    log = Path(receipt["worker"]["log"])
    assert log.is_file()
    assert "I am certain this passes" in log.read_text(encoding="utf-8")
    assert receipt["authority"] == "artifacts"


def test_a_withheld_job_still_leaves_a_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reset = int(datetime(2030, 1, 15, 19, 0, tzinfo=timezone.utc).timestamp())
    worker = Worker(returncode=1, stderr="Claude AI usage limit reached|{}".format(reset))

    result = _adapter(config, worker).run(_job(job_id=41), project_dir)

    receipt = json.loads(Path(config.job_receipts_dir / "41.json").read_text(encoding="utf-8"))
    assert receipt["result"]["status"] == WITHHELD
    assert receipt["result"]["retry_after_utc"] == result.payload["retry_after_utc"]


def test_the_receipt_lists_every_artifact_with_its_hash(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker(writes=lambda: (_write_brain_pack(project_dir), _blueprint_input(project_dir)))

    result = _adapter(config, worker, Reelctl()).run(_job(), project_dir)

    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    listed = {entry["path"]: entry for entry in receipt["artifacts"]}
    assert "brain/BLUEPRINT_INPUT.json" in listed
    assert listed["brain/BLUEPRINT_INPUT.json"]["sha256"] == sha256_file(project_dir / "brain" / "BLUEPRINT_INPUT.json")
    assert listed["brain/00_PROJECT_BRAIN.md"]["sha256"]


def test_the_receipt_records_the_advancing_call_the_daemon_made(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker(writes=lambda: (_write_brain_pack(project_dir), _blueprint_input(project_dir)))

    result = _adapter(config, worker, Reelctl()).run(_job(), project_dir)

    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["advance"]["argv"][:2] == ["blueprint", "lock"]
    assert receipt["advance"]["status"] == PASS


# --- 7. a stubbed claude binary, spawned for real ---------------------------


def _stub_claude(tmp_path: Path, body: str) -> Path:
    """A real executable on the tool path that stands in for ``claude``."""
    writer = tmp_path / "stub_worker.py"
    writer.write_text(body, encoding="utf-8")
    binary = tmp_path / "bin" / "claude"
    binary.write_text('#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, writer), encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_a_stubbed_worker_that_writes_a_valid_artifact_advances_the_stage(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    _stub_claude(
        tmp_path,
        "import json, pathlib, sys\n"
        "brain = pathlib.Path({project!r}) / 'brain'\n"
        "brain.mkdir(parents=True, exist_ok=True)\n"
        "for name in {pack!r}:\n"
        "    (brain / name).write_text('{{}}' if name.endswith('.json') else '# ' + name)\n"
        "(brain / 'BLUEPRINT_INPUT.json').write_text(json.dumps({payload!r}))\n"
        "print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'brain pack written'}}))\n".format(
            project=str(project_dir),
            pack=list(BRAIN_PACK),
            payload={
                "schema_version": 1,
                "status": "AUTHORED",
                "project_id": "demo",
                "reference_sha256": "a" * 64,
                "all_frames_reviewed": True,
                "boundaries_after": [12],
                "hard_cuts_after": [12],
                "observations": {
                    "p001": {"role": "opening", "description": "wide night skyline", "evidence_frames": [0], "transition_from_previous": "opening"},
                    "p002": {"role": "street", "description": "handheld push", "evidence_frames": [13], "transition_from_previous": "hard_cut"},
                },
            },
        ),
    )

    result = HeadlessJudgmentAdapter(config, runner=reelctl, now=lambda: NOW).run(_job(), project_dir)

    assert result.status == PASS
    assert reelctl.calls[0][:2] == ["blueprint", "lock"]
    assert (project_dir / "brain" / "10_EVIDENCE_MANIFEST.json").is_file()


def test_a_stubbed_worker_that_writes_nothing_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    reelctl = Reelctl()
    _stub_claude(tmp_path, "import json\nprint(json.dumps({'type': 'result', 'is_error': False, 'result': 'PASS'}))\n")

    result = HeadlessJudgmentAdapter(config, runner=reelctl, now=lambda: NOW).run(_job(), project_dir)

    assert result.status == FAIL
    assert reelctl.calls == []


def test_a_stubbed_worker_that_hits_the_session_limit_parks(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    _stub_claude(tmp_path, "import sys\nsys.stderr.write('Claude AI usage limit reached|1736953200\\n')\nsys.exit(1)\n")

    result = HeadlessJudgmentAdapter(config, runner=Reelctl(), now=lambda: NOW).run(_job(), project_dir)

    assert result.status == WITHHELD
    assert result.payload["class"] == CAPACITY


# --- 8. under the daemon ---------------------------------------------------


class Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _complete(project_dir: Path, stage: str) -> None:
    """Mark a stage done the way the reelctl subprocess would, without running it."""
    state = ProjectState.load(project_dir / "state.json")
    relative = "artifacts/{}.json".format(stage.lower())
    path = project_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stage": stage}, sort_keys=True), encoding="utf-8")
    state.complete(stage, "input-{}".format(stage.lower()), [relative])


def _daemon(config: StudioConfig, adapter: HeadlessJudgmentAdapter, clock: Clock) -> Orchestrator:
    orchestrator = Orchestrator(config, runner=Reelctl(), judgment=adapter, now=clock)
    orchestrator.start()
    return orchestrator


def test_a_parked_judgment_job_is_requeued_with_its_retry_after_and_spends_no_attempt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    _complete(project_dir, "REFERENCE_LOCKED")
    clock = Clock()
    reset = int(datetime(2030, 1, 15, 19, 0, tzinfo=timezone.utc).timestamp())
    worker = Worker(returncode=1, stderr="Claude AI usage limit reached|{}".format(reset))
    orchestrator = _daemon(config, HeadlessJudgmentAdapter(config, runner=Reelctl(), run=worker, now=clock), clock)

    report = orchestrator.tick()

    assert report["ran"]["stage"] == "BLUEPRINT_LOCKED"
    assert report["ran"]["status"] == WITHHELD
    assert report["ran"]["disposition"] == "PARKED"
    row = jobs.active_job(config.database_path, "demo")
    assert row["status"] == "QUEUED"
    assert row["attempt"] == 0
    assert row["retry_after_utc"] == "2030-01-15T19:00:00Z"
    assert jobs.count_events(config.database_path, kind="judgment_parked") == 1
    assert project_dir.is_dir()


def test_a_parked_job_is_not_retried_until_its_reset(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _complete(_project(config), "REFERENCE_LOCKED")
    clock = Clock()
    reset = int(datetime(2030, 1, 15, 19, 0, tzinfo=timezone.utc).timestamp())
    worker = Worker(returncode=1, stderr="Claude AI usage limit reached|{}".format(reset))
    orchestrator = _daemon(config, HeadlessJudgmentAdapter(config, runner=Reelctl(), run=worker, now=clock), clock)
    orchestrator.tick()

    clock.advance(timedelta(minutes=30))
    orchestrator.tick()

    assert len(worker.calls) == 1

    clock.advance(timedelta(hours=7))
    orchestrator.tick()

    assert len(worker.calls) == 2


def test_a_rejected_worker_still_spends_an_attempt_and_stalls_rather_than_looping(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _complete(_project(config), "REFERENCE_LOCKED")
    clock = Clock()
    worker = Worker()  # exits 0, writes nothing
    orchestrator = _daemon(config, HeadlessJudgmentAdapter(config, runner=Reelctl(), run=worker, now=clock), clock)

    for _ in range(4):
        orchestrator.tick()
        clock.advance(timedelta(minutes=20))

    assert len(worker.calls) == config.max_attempts
    assert jobs.count_events(config.database_path, kind="call_required") == 1


# --- 9. the suite never reaches a real claude ------------------------------


def test_the_adapter_resolves_its_worker_through_the_studio_tool_path_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    (tmp_path / "bin" / "claude").unlink()
    elsewhere = tmp_path / "ambient"
    elsewhere.mkdir()
    decoy = elsewhere / "claude"
    decoy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    decoy.chmod(0o755)
    monkeypatch.setenv("PATH", str(elsewhere))
    project_dir = _project(config)

    result = _adapter(config).run(_job(), project_dir)

    assert result.status == UNAVAILABLE


def test_the_daemon_command_installs_the_headless_adapter(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert isinstance(judgment_adapter(config), HeadlessJudgmentAdapter)


def test_judgment_can_be_switched_off_without_stopping_deterministic_work(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert isinstance(judgment_adapter(config, enabled=False), UnavailableJudgmentAdapter)
    assert isinstance(judgment_adapter(_config(tmp_path, REEL_STUDIO_JUDGMENT_ENABLED="false")), UnavailableJudgmentAdapter)


def test_the_daemon_subcommand_exposes_the_judgment_switch() -> None:
    parsed = build_parser().parse_args(["studio", "daemon", "--once", "--no-judgment"])

    assert parsed.no_judgment is True
    assert build_parser().parse_args(["studio", "daemon", "--once"]).no_judgment is False


def test_the_draft_stages_take_their_paths_from_the_daemons_own_stage_table() -> None:
    for stage in ("FEASIBILITY_REPORTED", "SELECTION_LOCKED", "ASSETS_LOCKED"):
        declared = plan_for(stage).drafts

        assert tuple(artifact.relative for artifact in JUDGMENT_STAGES[stage].artifacts) == declared
        assert all(artifact.schema for artifact in JUDGMENT_STAGES[stage].artifacts)


def test_the_worker_never_writes_the_signed_review_receipt_itself(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    _render_receipt(project_dir)
    reelctl = Reelctl()

    result = _adapter(config, Worker(writes=lambda: _visual_watch(project_dir)), reelctl).run(_job("VISUAL_QC"), project_dir)

    assert result.status == PASS
    assert VISUAL_WATCH_RELATIVE != plan_for("VISUAL_QC").drafts[0]
    signed = project_dir / plan_for("VISUAL_QC").drafts[0].format(revision=config.revision)
    assert not signed.exists(), "only reelctl review record-agent may write the signed receipt"


def test_the_stage_table_covers_every_stage_the_daemon_can_route_to_judgment() -> None:
    assert set(JUDGMENT_STAGES) == {"BLUEPRINT_LOCKED", "SELECTION_LOCKED", "FEASIBILITY_REPORTED", "ASSETS_LOCKED", "VISUAL_QC"}
    assert JUDGMENT_STAGES["BLUEPRINT_LOCKED"].phase == "BOOTSTRAP"
    assert JUDGMENT_STAGES["BLUEPRINT_LOCKED"].write_policy == "BRAIN_PACK_ONLY"
    assert JUDGMENT_STAGES["VISUAL_QC"].phase == "EXECUTE"


# --- 10. the liveness watchdog ---------------------------------------------
#
# A worker can hang before its first model response: the process stays alive and
# the 3h wall clock was the only thing that would ever have ended it. These tests use real
# subprocesses — a stuck ``sleep`` is the failure mode, and a stub that cannot actually
# hang would prove nothing about killing one.

SHELL_ENV = {"PATH": "/usr/bin:/bin"}

#: What the CLI writes once the model itself has answered.
MODEL_TURN = '{"type":"assistant","message":{"role":"assistant"}}'
#: What it writes before that, whether or not the model ever answers: the prompt, the hook
#: attachments, the tool and skill listings. Every observed hang wrote this much
#: within a second of spawn and then never spoke again, which is why it is not a signal.
STARTUP_NOISE = '{"type":"attachment","attachment":{"type":"hook_success"}}'


def _watch(
    tmp_path: Path,
    *,
    first: float = 0.4,
    stall: float = 0.4,
    poll: float = 0.02,
    transcript: Optional[Path] = None,
    project: Optional[Path] = None,
    transcript_root: Optional[Path] = None,
    session_id: Optional[str] = None,
) -> LivenessWatch:
    project_dir = project if project is not None else tmp_path / "watched-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    return LivenessWatch(
        transcript_path=transcript if transcript is not None else tmp_path / "transcripts" / "never-written.jsonl",
        project_dir=project_dir,
        first_progress_timeout_seconds=first,
        stall_timeout_seconds=stall,
        poll_seconds=poll,
        transcript_root=transcript_root,
        session_id=session_id,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive, just not ours
        return True
    return True


def _sh(script: str) -> List[str]:
    return ["/bin/sh", "-c", script]


def test_a_worker_that_never_shows_a_sign_of_life_is_killed_long_before_its_wall_clock(tmp_path: Path) -> None:
    started = time.monotonic()

    with pytest.raises(WorkerNotAlive) as caught:
        supervised_run(_sh("sleep 120"), cwd=str(tmp_path), env=SHELL_ENV, timeout=10800, liveness=_watch(tmp_path, first=0.3))

    assert caught.value.threshold == FIRST_PROGRESS
    assert time.monotonic() - started < 20, "the watchdog waited for the wall clock instead of the deadline"
    assert FIRST_PROGRESS in str(caught.value)


def test_the_kill_takes_the_whole_process_group_because_workers_survive_sigterm(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"

    with pytest.raises(WorkerNotAlive):
        supervised_run(
            _sh("sleep 120 & echo $! > {}; wait".format(pidfile)),
            cwd=str(tmp_path),
            env=SHELL_ENV,
            timeout=10800,
            liveness=_watch(tmp_path, first=0.3),
        )

    pid = int(pidfile.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid), "the worker's own child outlived the kill; SIGKILL did not reach the group"


def test_a_worker_whose_transcript_keeps_growing_is_never_killed(tmp_path: Path) -> None:
    transcript = tmp_path / "transcripts" / "live.jsonl"
    transcript.parent.mkdir(parents=True)

    completed = supervised_run(
        _sh("i=0; while [ $i -lt 20 ]; do echo '{}' >> {}; i=$((i+1)); sleep 0.05; done".format(MODEL_TURN, transcript)),
        cwd=str(tmp_path),
        env=SHELL_ENV,
        timeout=10800,
        liveness=_watch(tmp_path, first=0.35, stall=0.35, transcript=transcript),
    )

    assert completed.returncode == 0


def test_a_worker_that_writes_its_prompt_but_never_answers_trips_first_progress(tmp_path: Path) -> None:
    # The observed hang, exactly: the CLI files the prompt and its hook attachments in the
    # first second, the model never answers, and the process holds itself open. Before this
    # test, those startup writes counted as the first sign of life and demoted the hang to
    # the 30-minute stall deadline — which is why nothing fired on attempt 3.
    transcript = tmp_path / "transcripts" / "unanswered.jsonl"
    transcript.parent.mkdir(parents=True)

    with pytest.raises(WorkerNotAlive) as caught:
        supervised_run(
            _sh("echo '{0}' >> {1}; sleep 0.1; echo '{0}' >> {1}; sleep 120".format(STARTUP_NOISE, transcript)),
            cwd=str(tmp_path),
            env=SHELL_ENV,
            timeout=10800,
            liveness=_watch(tmp_path, first=0.5, stall=30.0, transcript=transcript),
        )

    assert caught.value.threshold == FIRST_PROGRESS
    assert caught.value.seconds < 30, "the kill waited for the stall deadline instead of the first-progress one"


def test_startup_writes_cannot_push_the_first_progress_deadline_out(tmp_path: Path) -> None:
    # The deadline runs from the spawn, not from the last byte written: a worker that keeps
    # emitting entries which are not model turns is not working, however noisy it is.
    transcript = tmp_path / "transcripts" / "noisy.jsonl"
    transcript.parent.mkdir(parents=True)

    with pytest.raises(WorkerNotAlive) as caught:
        supervised_run(
            _sh("while true; do echo '{}' >> {}; sleep 0.05; done".format(STARTUP_NOISE, transcript)),
            cwd=str(tmp_path),
            env=SHELL_ENV,
            # A ceiling far above the deadline, so a build that lets the noise reset the
            # clock ends as a wall-clock timeout instead of running for the real 3 hours.
            timeout=8,
            liveness=_watch(tmp_path, first=0.5, stall=30.0, transcript=transcript),
        )

    assert caught.value.threshold == FIRST_PROGRESS


def test_a_broken_transcript_line_is_not_mistaken_for_a_model_turn(tmp_path: Path) -> None:
    # The entry is read as JSON, not grepped for a word: a truncated line that happens to
    # contain the type is not the model answering.
    transcript = tmp_path / "transcripts" / "truncated.jsonl"
    transcript.parent.mkdir(parents=True)

    with pytest.raises(WorkerNotAlive) as caught:
        supervised_run(
            _sh("""echo '{{"type":"assistant"' >> {}; sleep 120""".format(transcript)),
            cwd=str(tmp_path),
            env=SHELL_ENV,
            timeout=10800,
            liveness=_watch(tmp_path, first=0.5, stall=30.0, transcript=transcript),
        )

    assert caught.value.threshold == FIRST_PROGRESS


def test_a_worker_that_writes_artifacts_but_never_answers_is_still_killed(tmp_path: Path) -> None:
    # Artifacts cannot appear without a model turn behind them, and the project directory is
    # shared with the daemon: it must not be able to vouch for a worker that never spoke.
    project_dir = tmp_path / "watched-project"
    project_dir.mkdir()

    with pytest.raises(WorkerNotAlive) as caught:
        supervised_run(
            _sh("i=0; while [ $i -lt 200 ]; do echo x > {}/brain-$i.md; i=$((i+1)); sleep 0.05; done".format(project_dir)),
            cwd=str(tmp_path),
            env=SHELL_ENV,
            timeout=10800,
            liveness=_watch(tmp_path, first=0.5, stall=30.0, project=project_dir),
        )

    assert caught.value.threshold == FIRST_PROGRESS


def test_artifact_writes_keep_a_worker_alive_once_the_model_has_answered(tmp_path: Path) -> None:
    # The other half: a worker deep in a long tool run writes files before it writes
    # anything else the daemon can see, and that is real work.
    transcript = tmp_path / "transcripts" / "answered.jsonl"
    transcript.parent.mkdir(parents=True)
    project_dir = tmp_path / "watched-project"
    project_dir.mkdir()

    completed = supervised_run(
        _sh(
            "echo '{}' >> {}; i=0; while [ $i -lt 20 ]; do echo x > {}/brain-$i.md; i=$((i+1)); sleep 0.05; done".format(
                MODEL_TURN, transcript, project_dir
            )
        ),
        cwd=str(tmp_path),
        env=SHELL_ENV,
        timeout=10800,
        liveness=_watch(tmp_path, first=30.0, stall=0.35, transcript=transcript, project=project_dir),
    )

    assert completed.returncode == 0


def test_a_transcript_filed_under_a_different_encoding_is_still_found_by_its_session_id(tmp_path: Path) -> None:
    root = tmp_path / "claude-projects"
    filed = root / "-somewhere-the-encoder-put-it"
    filed.mkdir(parents=True)
    transcript = filed / "the-session.jsonl"

    completed = supervised_run(
        _sh("i=0; while [ $i -lt 20 ]; do echo '{}' >> {}; i=$((i+1)); sleep 0.05; done".format(MODEL_TURN, transcript)),
        cwd=str(tmp_path),
        env=SHELL_ENV,
        timeout=10800,
        liveness=_watch(
            tmp_path,
            first=0.35,
            stall=0.35,
            transcript=root / "-a-guess" / "the-session.jsonl",
            transcript_root=root,
            session_id="the-session",
        ),
    )

    assert completed.returncode == 0


def test_a_worker_that_goes_quiet_after_it_started_trips_the_stall_threshold(tmp_path: Path) -> None:
    transcript = tmp_path / "transcripts" / "frozen.jsonl"
    transcript.parent.mkdir(parents=True)

    with pytest.raises(WorkerNotAlive) as caught:
        supervised_run(
            _sh("echo '{0}' >> {1}; sleep 0.2; echo '{0}' >> {1}; sleep 120".format(MODEL_TURN, transcript)),
            cwd=str(tmp_path),
            env=SHELL_ENV,
            timeout=10800,
            liveness=_watch(tmp_path, first=30.0, stall=0.3, transcript=transcript),
        )

    assert caught.value.threshold == STALL
    assert STALL in str(caught.value)


def test_the_wall_clock_ceiling_still_ends_a_worker_the_watchdog_believes_is_alive(tmp_path: Path) -> None:
    transcript = tmp_path / "transcripts" / "busy.jsonl"
    transcript.parent.mkdir(parents=True)

    with pytest.raises(subprocess.TimeoutExpired):
        supervised_run(
            _sh("while true; do echo '{}' >> {}; sleep 0.02; done".format(MODEL_TURN, transcript)),
            cwd=str(tmp_path),
            env=SHELL_ENV,
            timeout=0.4,
            liveness=_watch(tmp_path, first=30.0, stall=30.0, transcript=transcript),
        )


def test_a_worker_that_completes_returns_its_captured_output(tmp_path: Path) -> None:
    completed = supervised_run(
        _sh("echo on-stdout; echo on-stderr 1>&2; exit 3"),
        cwd=str(tmp_path),
        env=SHELL_ENV,
        timeout=30,
        liveness=_watch(tmp_path, first=30.0, stall=30.0),
    )

    assert completed.returncode == 3
    assert "on-stdout" in completed.stdout
    assert "on-stderr" in completed.stderr


def test_the_transcript_path_is_derived_the_way_the_cli_files_a_cwd(tmp_path: Path) -> None:
    home = tmp_path / "home"

    path = worker_transcript_path(cwd=Path("/home/x/reel-production"), session_id="a-session", home=home)

    assert path == home / ".claude" / "projects" / "-home-x-reel-production" / "a-session.jsonl"
    # A hidden directory's dot is encoded like the separator rather than dropped.
    assert encode_cwd(Path("/home/user/.agent/work")) == "-home-user--agent-work"


def test_the_watchdog_is_armed_with_this_jobs_transcript_and_the_configured_thresholds(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        REEL_STUDIO_JUDGMENT_FIRST_PROGRESS_TIMEOUT_SECONDS=61,
        REEL_STUDIO_JUDGMENT_STALL_TIMEOUT_SECONDS=62,
        REEL_STUDIO_JUDGMENT_LIVENESS_POLL_SECONDS=7,
    )
    project_dir = _project(config)
    worker = Worker()

    _adapter(config, worker).run(_job(), project_dir)

    watch = worker.calls[0]["liveness"]
    session_id = worker.argv[worker.argv.index("--session-id") + 1]
    assert watch.transcript_path.name == "{}.jsonl".format(session_id)
    assert watch.transcript_path.parent.name == encode_cwd(config.projects_root)
    assert watch.transcript_path.parent.parent.name == "projects"
    assert watch.project_dir == project_dir
    assert watch.session_id == session_id
    assert watch.first_progress_timeout_seconds == 61
    assert watch.stall_timeout_seconds == 62
    assert watch.poll_seconds == 7
    assert worker.calls[0]["timeout"] == config.judgment_timeout_seconds


def test_a_hung_worker_is_a_named_liveness_failure_not_a_mystery(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    worker = Worker(raises=WorkerNotAlive(FIRST_PROGRESS, seconds=600, watch=_watch(tmp_path)))

    result = _adapter(config, worker).run(_job(), project_dir)

    assert result.status == FAIL
    assert result.payload["class"] == LIVENESS
    assert result.payload["threshold"] == FIRST_PROGRESS
    assert FIRST_PROGRESS in (result.reason or "")
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["worker"]["class"] == LIVENESS
    assert receipt["worker"]["threshold"] == FIRST_PROGRESS
    assert receipt["result"]["class"] == LIVENESS
    assert receipt["invocation"]["liveness"]["first_progress_timeout_seconds"] == config.judgment_first_progress_timeout_seconds
    assert receipt["invocation"]["liveness"]["stall_timeout_seconds"] == config.judgment_stall_timeout_seconds


def test_what_the_hung_worker_printed_before_the_kill_is_kept_as_a_log(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project_dir = _project(config)
    hang = WorkerNotAlive(STALL, seconds=1800, watch=_watch(tmp_path), stdout="half a thought", stderr="a warning")

    result = _adapter(config, Worker(raises=hang)).run(_job(), project_dir)

    log = Path(json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))["worker"]["log"])
    assert "half a thought" in log.read_text(encoding="utf-8")
    assert "a warning" in log.read_text(encoding="utf-8")


def test_a_liveness_kill_spends_an_attempt_and_stalls_rather_than_parking(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _complete(_project(config), "REFERENCE_LOCKED")
    clock = Clock()
    worker = Worker(raises=WorkerNotAlive(FIRST_PROGRESS, seconds=600, watch=_watch(tmp_path)))
    orchestrator = _daemon(config, HeadlessJudgmentAdapter(config, runner=Reelctl(), run=worker, now=clock), clock)

    for _ in range(4):
        orchestrator.tick()
        clock.advance(timedelta(minutes=20))

    assert len(worker.calls) == config.max_attempts
    assert jobs.count_events(config.database_path, kind="call_required") == 1
    assert jobs.count_events(config.database_path, kind="judgment_parked") == 0
