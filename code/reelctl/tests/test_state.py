from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List

import pytest

from reelctl import hashing
from reelctl import state as state_module
from reelctl.state import STAGES, ProjectState, StateError


def test_state_is_atomic_resumable_and_invalidates_downstream(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = ProjectState.create(state_path, "demo")
    (tmp_path / "reference.json").write_text("{}\n")
    state.complete("REFERENCE_LOCKED", "ref-a", ["reference.json"])
    (tmp_path / "blueprint.json").write_text("{}\n")
    state.complete("BLUEPRINT_LOCKED", "blue-a", ["blueprint.json"])
    loaded = ProjectState.load(state_path)
    assert loaded.next_stage() == "FOOTAGE_INDEXED"
    assert not list(tmp_path.glob("*.tmp"))

    (tmp_path / "reference-v2.json").write_text('{"version": 2}\n')
    loaded.complete("REFERENCE_LOCKED", "ref-b", ["reference-v2.json"])
    reloaded = ProjectState.load(state_path)
    assert reloaded.stage("BLUEPRINT_LOCKED")["status"] == "STALE"
    assert reloaded.next_stage() == "BLUEPRINT_LOCKED"


def test_illegal_skipped_transition_is_rejected(tmp_path: Path) -> None:
    state = ProjectState.create(tmp_path / "state.json", "demo")
    with pytest.raises(StateError):
        state.complete("RENDERED", "x", ["candidate.mp4"])


def test_state_allows_hash_bound_qc_refresh_after_new_review_evidence(tmp_path: Path) -> None:
    state = ProjectState.create(tmp_path / "state.json", "demo")
    for stage_name in STAGES[: STAGES.index("TECHNICAL_QC")]:
        artifact = tmp_path / f"{stage_name}.json"
        artifact.write_text(f"{stage_name}\n", encoding="utf-8")
        state.complete(stage_name, stage_name, [str(artifact)])

    first = tmp_path / "qc-first.json"
    first.write_text("first\n", encoding="utf-8")
    state.complete("TECHNICAL_QC", "candidate-a", [str(first)])
    first_receipt = state.stage("TECHNICAL_QC")["receipt_id"]

    second = tmp_path / "qc-after-agent-review.json"
    second.write_text("second\n", encoding="utf-8")
    state.complete("TECHNICAL_QC", "candidate-a", [str(second)])

    stage = state.stage("TECHNICAL_QC")
    assert stage["receipt_id"] != first_receipt
    assert stage["output_receipts"][0]["path"] == second.name
    assert first.read_text(encoding="utf-8") == "first\n"


def test_state_json_has_every_stage_and_one_next_action(tmp_path: Path) -> None:
    state = ProjectState.create(tmp_path / "state.json", "demo")
    raw = json.loads((tmp_path / "state.json").read_text())
    assert list(raw["stages"]) == STAGES
    assert state.next_stage() == "REFERENCE_LOCKED"


def _completed_through(tmp_path: Path, last_stage: str) -> ProjectState:
    state = ProjectState.create(tmp_path / "state.json", "demo")
    for stage_name in STAGES[: STAGES.index(last_stage) + 1]:
        artifact = tmp_path / f"{stage_name}.json"
        artifact.write_text(f"{stage_name}\n", encoding="utf-8")
        state.complete(stage_name, stage_name, [str(artifact)])
    return state


def test_asking_for_the_next_stage_twice_rereads_no_unchanged_artifact(tmp_path: Path) -> None:
    """The studio daemon's tick cost.

    ``next_stage`` verifies every PASS stage and ``verify_stage`` recurses over the whole
    predecessor chain, so a project with eleven passed stages verifies sixty-six of them and
    re-hashes every declared output each time. On a large project that was ~100k hashes
    and gigabytes of reads per planning pass, repeated every twenty seconds forever against
    artifacts that are immutable by construction. Answering the same question about the same
    unchanged bytes must not cost a second read.
    """
    state = _completed_through(tmp_path, "LOCAL_REVIEW_READY")
    reads: List[Path] = []
    real = hashing.sha256_file

    def counting(path: Path, *args: Any, **kwargs: Any) -> str:
        reads.append(Path(path))
        return real(path, *args, **kwargs)

    # Both bindings, because the module under test may reach the reader through either its
    # own imported name or the memo's lookup in ``hashing`` — and a test that watched only
    # one of them would score a change of route as a saved read.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(hashing, "sha256_file", counting)
    monkeypatch.setattr(state_module, "sha256_file", counting)
    try:
        assert state.next_stage() == "HUMAN_APPROVED"
        assert reads, "the first pass must actually read the artifacts it verifies"
        reads.clear()
        assert state.next_stage() == "HUMAN_APPROVED"
    finally:
        monkeypatch.undo()

    assert reads == [], f"the second pass re-read {len(reads)} unchanged artifacts"


def test_deciding_the_next_stage_verifies_each_passed_stage_once_not_once_per_successor(tmp_path: Path) -> None:
    """The other half of the tick cost: ``verify_stage`` recursing over the whole prefix.

    ``next_stage`` verifies every PASS stage and each of those verifications re-walks its
    predecessors, so eleven passed stages cost sixty-six verifications — the earliest stage
    eleven times over. On a large project that meant ~100k path resolutions per planning
    pass even with every digest already known. One walk should verify each stage once; the
    chain-linkage check between neighbours still runs at every link.
    """
    state = _completed_through(tmp_path, "LOCAL_REVIEW_READY")
    passed = [name for name in STAGES if state.stage(name)["status"] == "PASS"]
    assert len(passed) == 11

    verifications: List[str] = []
    real = state_module.confined_path

    def counting(*args: Any, **kwargs: Any) -> Path:
        verifications.append(str(args[1]) if len(args) > 1 else "")
        return real(*args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(state_module, "confined_path", counting)
    try:
        assert state.next_stage() == "HUMAN_APPROVED"
    finally:
        monkeypatch.undo()

    # One receipt and one declared output per passed stage, resolved once each.
    assert len(verifications) == 2 * len(passed)


def test_a_stage_artifact_edited_between_passes_is_still_caught(tmp_path: Path) -> None:
    """The memo may never turn the integrity check into a memory of one.

    ``verify_stage`` exists to notice an artifact that changed after completion. A cache
    that answered from the first pass would report PASS forever, which is worse than the
    cost it saves.
    """
    state = _completed_through(tmp_path, "BLUEPRINT_LOCKED")
    assert state.next_stage() == "FOOTAGE_INDEXED"

    artifact = tmp_path / "REFERENCE_LOCKED.json"
    artifact.write_text("tampered after completion\n", encoding="utf-8")
    os.utime(artifact, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

    assert state.next_stage() == "REFERENCE_LOCKED"
    with pytest.raises(StateError):
        state.verify_stage("REFERENCE_LOCKED")


def test_a_stage_artifact_deleted_between_passes_is_still_caught(tmp_path: Path) -> None:
    state = _completed_through(tmp_path, "BLUEPRINT_LOCKED")
    assert state.next_stage() == "FOOTAGE_INDEXED"

    (tmp_path / "REFERENCE_LOCKED.json").unlink()

    assert state.next_stage() == "REFERENCE_LOCKED"
    with pytest.raises(StateError):
        state.verify_stage("REFERENCE_LOCKED")
