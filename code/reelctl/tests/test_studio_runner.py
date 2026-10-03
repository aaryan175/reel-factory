"""Stage plans and the deterministic subprocess runner.

The runner is the boundary where PATH, cwd and the wall-clock timeout are enforced, so
every test here mocks ``subprocess.run`` and asserts on what would have been spawned.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from reelctl.state import STAGES
from reelctl.studio.config import StudioConfig
from reelctl.studio.runner import (
    BLOCKED,
    BUSY,
    FAIL,
    PASS,
    TIMEOUT,
    UNAVAILABLE,
    WITHHELD,
    StageRunner,
    UnavailableJudgmentAdapter,
)
from reelctl.studio.stages import (
    DETERMINISTIC,
    HUMAN,
    JUDGMENT,
    STAGE_PLANS,
    argv_for,
    call_gate_index,
    draft_state,
    job_for,
    plan_for,
    project_fingerprint,
    unauthored_drafts,
)


def _config(tmp_path: Path, **overrides: Any) -> StudioConfig:
    env = {
        "REEL_STUDIO_PROJECTS_ROOT": str(tmp_path / "projects"),
        "REEL_STUDIO_STORAGE_ROOT": str(tmp_path / "storage"),
        "REEL_STUDIO_PATH": str(tmp_path / "bin"),
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return StudioConfig.from_env(env)


def _fake_reelctl(tmp_path: Path) -> Path:
    binary = tmp_path / "bin" / "reelctl"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


class _Recorder:
    """Stand-in for ``subprocess.run`` that records the call and replays a result."""

    def __init__(self, *, returncode: int = 0, stdout: str = "{}", stderr: str = "", raises: Exception = None) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, argv: List[str], **kwargs: Any) -> Any:
        self.calls.append({"argv": list(argv), **kwargs})
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(args=list(argv), returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


# --- the stage plan table --------------------------------------------------


def test_every_stage_has_exactly_one_plan() -> None:
    assert list(STAGE_PLANS) == STAGES
    for stage in STAGES:
        assert plan_for(stage).stage == stage


def test_plans_declare_only_the_three_kinds_the_daemon_can_act_on() -> None:
    kinds = {plan.kind for plan in STAGE_PLANS.values()}

    assert kinds <= {DETERMINISTIC, JUDGMENT, HUMAN}


def test_the_deterministic_set_matches_the_architecture() -> None:
    deterministic = {stage for stage, plan in STAGE_PLANS.items() if plan.kind == DETERMINISTIC}

    assert deterministic == {
        "REFERENCE_LOCKED",
        "FOOTAGE_INDEXED",
        "FEASIBILITY_REPORTED",
        "SELECTION_LOCKED",
        "ASSETS_LOCKED",
        "RENDERED",
        "TECHNICAL_QC",
        "STRUCTURE_QC",
        "VISUAL_QC",
        "LOCAL_REVIEW_READY",
    }


def test_the_three_approval_stages_are_human_and_carry_no_command() -> None:
    for stage in ("HUMAN_APPROVED", "DELIVERY_APPROVED", "DELIVERED"):
        plan = plan_for(stage)
        assert plan.kind == HUMAN
        assert plan.argv == ()


def test_a_judgment_stage_carries_no_command_for_the_daemon_to_run() -> None:
    plan = plan_for("BLUEPRINT_LOCKED")

    assert plan.kind == JUDGMENT
    assert plan.argv == ()
    assert plan.note


def test_argv_binds_the_project_and_revision() -> None:
    assert argv_for(plan_for("RENDERED"), project_id="demo", revision="v007") == ("render", "demo", "--revision", "v007")
    assert argv_for(plan_for("TECHNICAL_QC"), project_id="demo", revision="v001") == ("qc", "demo", "--revision", "v001")
    assert argv_for(plan_for("FOOTAGE_INDEXED"), project_id="demo", revision="v001") == ("footage", "index", "demo")


def test_the_four_qc_stages_share_one_command_because_one_qc_run_completes_them() -> None:
    commands = {argv_for(plan_for(stage), project_id="d", revision="v001") for stage in ("TECHNICAL_QC", "STRUCTURE_QC", "LOCAL_REVIEW_READY")}

    assert commands == {("qc", "d", "--revision", "v001")}


# --- drafts: the judgment/deterministic seam -------------------------------


def test_a_missing_draft_reads_missing_and_a_template_reads_draft(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    (project / "edit").mkdir(parents=True)

    assert draft_state(project, "edit/feasibility.json") == "MISSING"

    (project / "edit/feasibility.json").write_text(json.dumps({"status": "DRAFT", "blocks": []}), encoding="utf-8")
    assert draft_state(project, "edit/feasibility.json") == "DRAFT"

    (project / "edit/feasibility.json").write_text(json.dumps({"status": "PASS", "blocks": []}), encoding="utf-8")
    assert draft_state(project, "edit/feasibility.json") == "AUTHORED"


def test_an_unreadable_draft_is_named_not_guessed(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    (project / "edit").mkdir(parents=True)
    (project / "edit/feasibility.json").write_text("{not json", encoding="utf-8")

    assert draft_state(project, "edit/feasibility.json") == "UNREADABLE"


def test_a_deterministic_stage_with_an_unauthored_draft_becomes_a_judgment_job(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    (project / "edit").mkdir(parents=True)
    (project / "edit/selection.json").write_text(json.dumps({"status": "DRAFT", "slots": []}), encoding="utf-8")

    job = job_for(project, "SELECTION_LOCKED", revision="v001")

    assert job.kind == JUDGMENT
    assert job.argv == ()
    assert "edit/selection.json" in job.reason
    assert unauthored_drafts(project, plan_for("SELECTION_LOCKED"), revision="v001") == [("edit/selection.json", "DRAFT")]


def test_the_same_stage_becomes_deterministic_once_the_draft_is_authored(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    (project / "edit").mkdir(parents=True)
    (project / "edit/selection.json").write_text(json.dumps({"status": "READY", "slots": []}), encoding="utf-8")

    job = job_for(project, "SELECTION_LOCKED", revision="v001")

    assert job.kind == DETERMINISTIC
    assert job.argv == ("selection", "validate", "demo")


def test_visual_qc_waits_for_the_agent_watch_receipt_of_its_own_revision(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    (project / "review").mkdir(parents=True)

    assert job_for(project, "VISUAL_QC", revision="v001").kind == JUDGMENT

    (project / "review/agent-visual-review-v001.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")

    later = job_for(project, "VISUAL_QC", revision="v001")
    assert later.kind == DETERMINISTIC
    assert later.argv == ("qc", "demo", "--revision", "v001")
    # a different revision has its own receipt requirement
    assert job_for(project, "VISUAL_QC", revision="v002").kind == JUDGMENT


def test_a_human_stage_produces_a_job_the_daemon_will_not_run(tmp_path: Path) -> None:
    job = job_for(tmp_path / "demo", "HUMAN_APPROVED", revision="v001")

    assert job.kind == HUMAN
    assert job.argv == ()
    assert job.reason


# --- the one place a call's declared stage is interpreted -------------------
#
# An earlier mirror of this rule in status.py omitted the
# .strip(), so a card reading "  SELECTION_LOCKED  " would have gated the whole board while
# the daemon drove four stages — the exact board/daemon divergence the stage gate exists to
# close, reintroduced over whitespace. One shared function now, so there is nothing to keep
# in sync by hand.


def test_a_calls_declared_stage_resolves_to_its_position_in_the_stage_order() -> None:
    assert call_gate_index({"stage": "SELECTION_LOCKED"}) == STAGES.index("SELECTION_LOCKED")
    assert call_gate_index({"stage": STAGES[0]}) == 0
    assert call_gate_index({"stage": STAGES[-1]}) == len(STAGES) - 1


def test_surrounding_whitespace_in_a_card_does_not_change_the_gate() -> None:
    assert call_gate_index({"stage": "  SELECTION_LOCKED  "}) == STAGES.index("SELECTION_LOCKED")
    assert call_gate_index({"stage": "\tRENDERED\n"}) == STAGES.index("RENDERED")


def test_anything_the_build_cannot_place_gates_everything() -> None:
    """``None`` means "gates everything" — an unrecognised gate is never an open door."""
    for value in ({}, {"stage": None}, {"stage": ""}, {"stage": "   "}, {"stage": 7}, {"stage": ["RENDERED"]}):
        assert call_gate_index(value) is None
    assert call_gate_index({"stage": "SOME_FUTURE_STAGE"}) is None
    # case is not normalised: STAGES are uppercase, so a lowercased card fails closed
    assert call_gate_index({"stage": "selection_locked"}) is None


def test_the_project_fingerprint_moves_when_state_or_a_draft_moves(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    (project / "edit").mkdir(parents=True)
    (project / "state.json").write_text(json.dumps({"a": 1}), encoding="utf-8")

    first = project_fingerprint(project, revision="v001")
    assert first == project_fingerprint(project, revision="v001")

    (project / "state.json").write_text(json.dumps({"a": 2}), encoding="utf-8")
    second = project_fingerprint(project, revision="v001")
    assert second != first

    (project / "edit/selection.json").write_text(json.dumps({"status": "READY"}), encoding="utf-8")
    assert project_fingerprint(project, revision="v001") != second


# --- the subprocess boundary ----------------------------------------------


def test_the_command_is_the_resolved_binary_with_an_explicit_projects_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    binary = _fake_reelctl(tmp_path)
    runner = StageRunner(config, run=_Recorder())

    assert runner.command(("render", "demo", "--revision", "v001")) == [
        str(binary),
        "--projects-root",
        str(config.projects_root),
        "render",
        "demo",
        "--revision",
        "v001",
    ]


def test_the_subprocess_gets_the_studio_tool_path_not_the_inherited_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nowhere")
    config = _config(tmp_path)
    _fake_reelctl(tmp_path)
    recorder = _Recorder(stdout=json.dumps({"status": "PASS"}))

    StageRunner(config, run=recorder).run(("footage", "index", "demo"))

    assert recorder.calls[0]["env"]["PATH"] == str(tmp_path / "bin")
    assert "/nowhere" not in recorder.calls[0]["env"]["PATH"]


def test_the_subprocess_runs_in_the_projects_root_so_claude_md_invariants_apply(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _fake_reelctl(tmp_path)
    recorder = _Recorder(stdout=json.dumps({"status": "PASS"}))

    StageRunner(config, run=recorder).run(("footage", "index", "demo"))

    assert recorder.calls[0]["cwd"] == str(config.projects_root)


def test_a_wall_clock_timeout_is_always_passed_and_reported_by_name(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _fake_reelctl(tmp_path)
    recorder = _Recorder(raises=subprocess.TimeoutExpired(cmd="reelctl", timeout=12))

    result = StageRunner(config, timeout_seconds=12, run=recorder).run(("render", "demo", "--revision", "v001"))

    assert recorder.calls[0]["timeout"] == 12
    assert result.status == TIMEOUT
    assert "12" in result.reason


def test_a_clean_run_carries_the_engines_own_payload(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _fake_reelctl(tmp_path)
    payload = {"status": "PASS", "next_stage": "TECHNICAL_QC", "clip_count": 3}
    recorder = _Recorder(stdout=json.dumps(payload))

    result = StageRunner(config, run=recorder).run(("footage", "index", "demo"))

    assert result.status == PASS
    assert result.payload == payload
    assert result.returncode == 0


def test_a_blocked_stage_is_a_withholding_with_the_engines_reason_verbatim(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _fake_reelctl(tmp_path)
    reason = "The reviewing agent must map every locked picture state against the complete footage inventory."
    recorder = _Recorder(stdout=json.dumps({"status": "BLOCKED", "reason": reason, "next_stage": "FEASIBILITY_REPORTED"}))

    result = StageRunner(config, run=recorder).run(("feasibility", "lock", "demo"))

    assert result.status == BLOCKED
    assert result.reason == reason


def test_a_busy_project_is_classified_as_contention_not_as_a_stage_failure(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _fake_reelctl(tmp_path)
    error = "project demo is locked by another reelctl process: /x/.reelctl-locks/demo.lock"
    recorder = _Recorder(
        returncode=2,
        stdout="",
        stderr=json.dumps({"status": "FAIL", "error": error, "error_type": "ProjectBusyError"}),
    )

    result = StageRunner(config, run=recorder).run(("render", "demo", "--revision", "v001"))

    assert result.status == BUSY
    assert result.reason == error


def test_a_real_failure_keeps_the_engines_error_string(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _fake_reelctl(tmp_path)
    error = "feasibility remains blocked by missing roles"
    recorder = _Recorder(returncode=2, stdout="", stderr=json.dumps({"status": "FAIL", "error": error, "error_type": "ReelctlError"}))

    result = StageRunner(config, run=recorder).run(("selection", "validate", "demo"))

    assert result.status == FAIL
    assert result.reason == error


def test_a_zero_exit_without_parseable_json_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _fake_reelctl(tmp_path)
    recorder = _Recorder(stdout="Traceback (most recent call last):\n")

    result = StageRunner(config, run=recorder).run(("qc", "demo", "--revision", "v001"))

    assert result.status == FAIL
    assert "parseable JSON" in result.reason


def test_a_missing_reelctl_binary_is_unavailable_and_spawns_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    recorder = _Recorder()

    result = StageRunner(config, run=recorder).run(("footage", "index", "demo"))

    assert result.status == UNAVAILABLE
    assert "reelctl" in result.reason
    assert recorder.calls == []


def test_a_binary_that_vanishes_between_resolution_and_spawn_does_not_raise(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _fake_reelctl(tmp_path)
    recorder = _Recorder(raises=FileNotFoundError("No such file or directory: reelctl"))

    result = StageRunner(config, run=recorder).run(("footage", "index", "demo"))

    assert result.status == UNAVAILABLE
    assert result.reason


# --- the judgment seam Task 7 fills ---------------------------------------


def test_the_default_judgment_adapter_withholds_with_a_named_reason_and_a_retry(tmp_path: Path) -> None:
    adapter = UnavailableJudgmentAdapter()

    result = adapter.run(job_for(tmp_path / "demo", "BLUEPRINT_LOCKED", revision="v001"), tmp_path / "demo")

    assert result.status == WITHHELD
    assert result.reason
    assert adapter.retry_after_seconds > 0


def test_the_default_judgment_adapter_never_claims_a_pass(tmp_path: Path) -> None:
    adapter = UnavailableJudgmentAdapter()

    for stage in ("BLUEPRINT_LOCKED", "SELECTION_LOCKED", "VISUAL_QC"):
        assert adapter.run(job_for(tmp_path / "demo", stage, revision="v001"), tmp_path / "demo").status != PASS
