"""One source for "what calls are open against this project" (§5.1, §10 Task 8).

The problem: the intake mode call lives in ``<project>/intake.json`` and
never reaches ``REEL_REGISTRY.json``, because ``studio/intake.py`` deliberately does not
write the registry. The daemon's gate and the board both read the registry only, so a
studio-born reel was driven straight through ``SELECTION_LOCKED`` — past the stage its own
mode call declares it governs — while its record still said ``mode_confirmed: false``.

The merge existed already, but only inside ``calls.inbox``, which is why the inbox alone
reported it correctly. Assembling this list is now one function, for the same reason
``call_gate_index`` is: a rule duplicated across consumers diverges invisibly from inside
any one of them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from reelctl.studio.opencalls import INTAKE_RECORD_FILENAME, project_open_calls
from reelctl.studio.registry import registry_report


def _tree(tmp_path: Path, *, open_calls: Optional[List[Dict[str, Any]]] = None) -> Path:
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    (root / "REEL_REGISTRY.json").write_text(
        json.dumps({"schema_version": "2.0", "reels": [], "open_calls": open_calls or []}), encoding="utf-8"
    )
    return root


def _project(root: Path, project_id: str = "reel-demo-v1", *, call: Optional[Dict[str, Any]] = None) -> Path:
    directory = root / project_id
    directory.mkdir(parents=True, exist_ok=True)
    if call is not None:
        (directory / INTAKE_RECORD_FILENAME).write_text(json.dumps({"project_id": project_id, "call": call}), encoding="utf-8")
    return directory


MODE_CALL = {
    "call_id": "CALL-MODE-reel-demo-v1",
    "stage": "SELECTION_LOCKED",
    "state": "AWAITING_OPERATOR",
    "issue_type": "mode-ambiguity",
}


def test_the_intake_records_own_call_is_returned(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    directory = _project(root, call=MODE_CALL)

    calls = project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory)

    assert [call["call_id"] for call in calls] == ["CALL-MODE-reel-demo-v1"]
    assert calls[0]["stage"] == "SELECTION_LOCKED"
    assert calls[0]["source"] == "intake record"


def test_registry_declared_calls_are_returned(tmp_path: Path) -> None:
    root = _tree(tmp_path, open_calls=[{"call_id": "CALL-001", "project": "reel-demo-v1", "state": "AWAITING_OPERATOR"}])
    directory = _project(root)

    calls = project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory)

    assert [call["call_id"] for call in calls] == ["CALL-001"]
    assert calls[0]["source"] == "registry open_calls"


def test_both_sources_are_merged(tmp_path: Path) -> None:
    root = _tree(tmp_path, open_calls=[{"call_id": "CALL-001", "project": "reel-demo-v1", "state": "AWAITING_OPERATOR"}])
    directory = _project(root, call=MODE_CALL)

    calls = project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory)

    assert sorted(call["call_id"] for call in calls) == ["CALL-001", "CALL-MODE-reel-demo-v1"]


def test_the_registry_wins_when_both_declare_the_same_call(tmp_path: Path) -> None:
    """The registry is the operator-facing declaration; an answered call must not reopen."""
    root = _tree(
        tmp_path,
        open_calls=[{"call_id": "CALL-MODE-reel-demo-v1", "project": "reel-demo-v1", "state": "ANSWERED", "stage": "RENDERED"}],
    )
    directory = _project(root, call=MODE_CALL)

    calls = project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory)

    assert len(calls) == 1
    assert calls[0]["state"] == "ANSWERED"
    assert calls[0]["stage"] == "RENDERED"


def test_a_call_on_another_project_is_not_returned(tmp_path: Path) -> None:
    root = _tree(tmp_path, open_calls=[{"call_id": "CALL-001", "project": "somebody-else", "state": "AWAITING_OPERATOR"}])
    directory = _project(root)

    assert project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory) == []


def test_a_project_with_neither_source_has_no_calls(tmp_path: Path) -> None:
    root = _tree(tmp_path)

    assert project_open_calls(registry_report(root / "REEL_REGISTRY.json"), _project(root)) == []


def test_an_unreadable_intake_record_is_ignored_not_fatal(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    directory = _project(root)
    (directory / INTAKE_RECORD_FILENAME).write_text("{not json", encoding="utf-8")

    assert project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory) == []


def test_an_intake_record_without_a_call_is_ignored(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    directory = _project(root)
    (directory / INTAKE_RECORD_FILENAME).write_text(json.dumps({"project_id": "reel-demo-v1"}), encoding="utf-8")

    assert project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory) == []


def test_an_intake_call_without_an_id_is_ignored(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    directory = _project(root, call={"stage": "SELECTION_LOCKED", "state": "AWAITING_OPERATOR"})

    assert project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory) == []


# --- the consumers converge, and stay converged ----------------------------
#
# The team lead's standing requirement is "one implementation, all three surfaces converge".
# The daemon reads the leaf directly; ``calls.inbox`` layers presentation on the same leaf.
# These assert the two paths reach the *same gating decision*, so a future parallel assembly
# fails here rather than silently putting the board and the daemon back into disagreement.
#
# Compared against ``inbox`` rather than a wrapper around it, deliberately: the wrapper was
# only ever a grouping, so testing through it added a layer without adding a guarantee.


def _studio_born(tmp_path: Path):
    from reelctl.studio.config import StudioConfig
    from reelctl.studio.intake import create_intake

    for name in ("projects", "storage", "footage"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "reference.mp4").write_bytes(b"ref")
    (tmp_path / "projects" / "REEL_REGISTRY.json").write_text(
        json.dumps({"schema_version": "2.0", "reels": [], "open_calls": []}), encoding="utf-8"
    )
    config = StudioConfig.from_env(
        {
            "REEL_STUDIO_PROJECTS_ROOT": str(tmp_path / "projects"),
            "REEL_STUDIO_STORAGE_ROOT": str(tmp_path / "storage"),
            "REEL_STUDIO_DIR": str(tmp_path / "studio"),
            "REEL_STUDIO_MIN_FREE_BYTES": "0",
            "REEL_STUDIO_MIN_FREE_BYTES_INTERNAL": "0",
        }
    )
    result = create_intake(config, reference=str(tmp_path / "reference.mp4"), footage_root=str(tmp_path / "footage"), lane="VOLUME")
    project_id = result.get("project_id") or result.get("plan", {}).get("project_id")
    return config, project_id


def test_the_daemon_and_the_inbox_gate_a_studio_born_reel_identically(tmp_path: Path) -> None:
    from reelctl.state import STAGES
    from reelctl.studio.calls import inbox
    from reelctl.studio.daemon import _awaiting_operator

    config, project_id = _studio_born(tmp_path)
    directory = config.projects_root / project_id

    from_leaf = project_open_calls(registry_report(config.registry_path), directory)
    from_inbox = [call for call in inbox(config, include_answered=True)["calls"] if call["project_id"] == project_id]

    assert [call["call_id"] for call in from_leaf] == [call["call_id"] for call in from_inbox]
    for stage in STAGES:
        assert _awaiting_operator(from_leaf, stage=stage) == _awaiting_operator(from_inbox, stage=stage), stage


def test_the_studio_born_reel_is_refused_at_selection_and_free_before_it(tmp_path: Path) -> None:
    """The original reproduction, as a permanent test."""
    from reelctl.studio.daemon import _awaiting_operator

    config, project_id = _studio_born(tmp_path)
    calls = project_open_calls(registry_report(config.registry_path), config.projects_root / project_id)

    proceeds = ("REFERENCE_LOCKED", "BLUEPRINT_LOCKED", "FOOTAGE_INDEXED", "FEASIBILITY_REPORTED")
    waits = ("SELECTION_LOCKED", "ASSETS_LOCKED", "RENDERED")
    assert [stage for stage in proceeds if _awaiting_operator(calls, stage=stage)] == []
    assert [stage for stage in waits if not _awaiting_operator(calls, stage=stage)] == []


def test_an_unparseable_registry_still_yields_the_intake_call(tmp_path: Path) -> None:
    """A broken registry must not silently drop the one gate a fresh reel has."""
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    (root / "REEL_REGISTRY.json").write_text("{not json", encoding="utf-8")
    directory = _project(root, call=MODE_CALL)

    calls = project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory)

    assert [call["call_id"] for call in calls] == ["CALL-MODE-reel-demo-v1"]


# --- an answer receipt is the durable statement a call was answered ---------
#
# Nothing rewrites the intake record's ``call.state`` (the record is ``intake``'s
# artifact; a second writer is forbidden), so the receipt in ``review/calls/`` is the
# only place answered-ness exists. The assembly must apply it, or the daemon parks an
# answered reel forever while the inbox shows it answered.


def _receipt(directory: Path, call_id: str, *, sequence: int = 1, name: Optional[str] = None) -> None:
    answers = directory / "review" / "calls"
    answers.mkdir(parents=True, exist_ok=True)
    payload = {"call_id": call_id, "sequence": sequence, "answered_at_utc": "2025-01-15T00:00:00Z", "option": "A"}
    (answers / (name or "{}.answer.json".format(call_id))).write_text(json.dumps(payload), encoding="utf-8")


def test_an_answer_receipt_marks_an_intake_call_answered(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    directory = _project(root, call=MODE_CALL)
    _receipt(directory, "CALL-MODE-reel-demo-v1")

    calls = project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory)

    assert [call["state"] for call in calls] == ["ANSWERED"]
    assert calls[0]["answer_receipt"] == "CALL-MODE-reel-demo-v1.answer.json"


def test_an_answer_receipt_marks_a_registry_declared_call_answered(tmp_path: Path) -> None:
    declared = {"call_id": "CALL-X", "project": "reel-demo-v1", "state": "AWAITING_OPERATOR"}
    root = _tree(tmp_path, open_calls=[declared])
    directory = _project(root)
    _receipt(directory, "CALL-X")

    calls = project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory)

    assert [call["state"] for call in calls] == ["ANSWERED"]


def test_a_receipt_for_another_call_does_not_answer_this_one(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    directory = _project(root, call=MODE_CALL)
    _receipt(directory, "CALL-SOMETHING-ELSE")

    calls = project_open_calls(registry_report(root / "REEL_REGISTRY.json"), directory)

    assert [call["state"] for call in calls] == ["AWAITING_OPERATOR"]


def test_answering_through_the_real_api_clears_the_daemon_gate(tmp_path: Path) -> None:
    """Substitution-direction: the write side is ``calls.answer_call``, the read side is
    the daemon's assembly. Agreement tests on the unanswered state could never catch the
    two disagreeing about the answered one."""
    from reelctl.state import STAGES
    from reelctl.studio.calls import answer_call, inbox
    from reelctl.studio.daemon import _awaiting_operator

    config, project_id = _studio_born(tmp_path)
    directory = config.projects_root / project_id
    before = project_open_calls(registry_report(config.registry_path), directory)
    assert [call["state"] for call in before] == ["AWAITING_OPERATOR"]
    call_id = str(before[0]["call_id"])

    answer_call(config, call_id, option="A", reviewer="test-operator")

    calls = project_open_calls(registry_report(config.registry_path), directory)
    assert [call["state"] for call in calls] == ["ANSWERED"]
    for stage in STAGES:
        assert _awaiting_operator(calls, stage=stage) == [], stage
    from_inbox = [call for call in inbox(config, include_answered=True)["calls"] if call["project_id"] == project_id]
    assert [call["state"] for call in from_inbox] == ["ANSWERED"]
