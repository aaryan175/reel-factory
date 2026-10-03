"""The daemon-side input preflight (v1.1).

The failure this exists for: a judgment brief names a footage root on an external volume;
a daemon-spawned ``claude`` has no TCC grant for that volume; and because the CLI is a
prompt-eligible TCC identity the read does not fail — it blocks in ``openat`` waiting for a
consent prompt an unattended service cannot answer. Workers hang for a long time.

The daemon's own interpreter gets a clean ``PermissionError`` on the same path in 30ms.
So it can know, before it spawns anything, that the worker cannot possibly succeed. These
tests pin what it does with that knowledge: probe content rather than metadata, stay
bounded, name the path and the errno, and cite the operator's call when the path is on the
external volume.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from reelctl.studio.config import StudioConfig
from reelctl.studio.preflight import (
    TCC_CALL_ID,
    BlockedInput,
    DeclaredInput,
    InputProbe,
    declared_inputs,
    is_external,
    preflight_inputs,
    probe_input,
)
from reelctl.studio.stages import DETERMINISTIC, JUDGMENT, JobSpec


def _config(tmp_path: Path, **overrides: Any) -> StudioConfig:
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    env = {
        "REEL_STUDIO_PROJECTS_ROOT": str(projects),
        "REEL_STUDIO_STORAGE_ROOT": str(storage),
        "REEL_STUDIO_DIR": str(tmp_path / "studio"),
        "REEL_STUDIO_MIN_FREE_BYTES": "0",
        "REEL_STUDIO_MIN_FREE_BYTES_INTERNAL": "0",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return StudioConfig.from_env(env)


def _project(config: StudioConfig, project_id: str = "demo", **fields: Any) -> Path:
    directory = config.projects_root / project_id
    directory.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {"schema_version": 1, "project_id": project_id, "mode": "original-montage"}
    payload.update(fields)
    (directory / "project.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return directory


def _job(stage: str = "BLUEPRINT_LOCKED", kind: str = JUDGMENT, project_id: str = "demo") -> JobSpec:
    return JobSpec(project_id=project_id, stage=stage, kind=kind)


@pytest.fixture()
def unreadable() -> Iterator[Any]:
    """A directory whose contents macOS refuses this process, restored on the way out.

    ``chmod 000`` is the closest thing an ordinary test can get to the TCC wall: ``stat``
    keeps answering, the listing does not. Permissions are always restored — a 0o000
    directory would otherwise defeat pytest's own tmp cleanup.
    """
    restored: list = []

    def deny(path: Path) -> Path:
        restored.append((path, stat.S_IMODE(path.stat().st_mode)))
        path.chmod(0o000)
        return path

    try:
        yield deny
    finally:
        for path, mode in restored:
            try:
                path.chmod(mode)
            except OSError:  # pragma: no cover - the test already failed if this bites
                pass


def _declared(path: Path, label: str = "a declared input", source: str = "a test") -> DeclaredInput:
    return DeclaredInput(label=label, path=path, source=source)


# --- 1. the probe reads content, because metadata is not what is gated ------


def test_a_readable_directory_answers_the_probe(tmp_path: Path) -> None:
    (tmp_path / "footage").mkdir()
    (tmp_path / "footage" / "clip.mp4").write_bytes(b"0")

    probe = probe_input(_declared(tmp_path / "footage"))

    assert probe.readable is True
    assert probe.code is None
    assert probe.error is None


def test_an_empty_directory_is_readable_rather_than_suspicious(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()

    assert probe_input(_declared(tmp_path / "empty")).readable is True


def test_a_readable_file_answers_the_probe(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"a reference")

    assert probe_input(_declared(reference)).readable is True


def test_a_directory_that_stats_but_cannot_be_listed_is_unreadable(tmp_path: Path, unreadable: Any) -> None:
    """The TCC fingerprint exactly: metadata is free, content is gated."""
    footage = tmp_path / "footage"
    footage.mkdir()
    denied = unreadable(footage)

    probe = probe_input(_declared(denied))

    assert denied.is_dir() is True, "stat still answers, which is why a stat-only check would pass"
    assert probe.readable is False
    assert probe.code == "EACCES"
    assert "Permission denied" in str(probe.error)


def test_a_missing_path_is_unreadable_and_says_so_by_errno(tmp_path: Path) -> None:
    probe = probe_input(_declared(tmp_path / "not-mounted" / "footage-library"))

    assert probe.readable is False
    assert probe.code == "ENOENT"


def test_a_probe_that_never_answers_is_bounded_instead_of_hanging_the_tick(tmp_path: Path) -> None:
    """A wedged mount must cost the daemon its timeout, not its loop."""
    blocker = tmp_path / "blocks-forever"
    # ``exec`` so the sleep replaces the shell rather than becoming its child: the probe's
    # own kill has to be enough, and a grandchild would let a leak hide behind it.
    blocker.write_text("#!/bin/sh\nexec sleep 31\n", encoding="utf-8")
    blocker.chmod(0o755)

    started = time.monotonic()
    probe = probe_input(_declared(tmp_path), timeout_seconds=0.5, executable=str(blocker))
    elapsed = time.monotonic() - started

    assert probe.readable is False
    assert probe.code == "TIMEOUT"
    assert elapsed < 10, "the probe waited for the read instead of bounding it"
    assert str(tmp_path) in probe.describe()


def test_the_blocked_probe_leaves_no_process_behind(tmp_path: Path) -> None:
    """A thread stuck in openat cannot be killed; the child it replaced can, and is."""
    blocker = tmp_path / "blocks-forever"
    blocker.write_text("#!/bin/sh\nexec sleep 37\n", encoding="utf-8")
    blocker.chmod(0o755)

    probe_input(_declared(tmp_path), timeout_seconds=0.5, executable=str(blocker))

    survivors = subprocess.run(["/usr/bin/pgrep", "-f", "sleep 37"], capture_output=True, text=True, check=False)
    assert survivors.stdout.strip() == "", "the timed-out probe left a live process behind"


def test_a_probe_that_cannot_be_run_is_not_reported_as_a_closed_path(tmp_path: Path) -> None:
    """"I could not find out" and "the path is closed to me" are different sentences."""
    probe = probe_input(_declared(tmp_path), executable=str(tmp_path / "no-such-interpreter"))

    assert probe.readable is False
    assert probe.code == "PROBE_FAILED"
    assert "probe" in str(probe.error)


def test_the_child_probe_reports_the_same_refusal_the_direct_read_does(tmp_path: Path, unreadable: Any) -> None:
    footage = tmp_path / "footage"
    footage.mkdir()
    denied = unreadable(footage)

    assert probe_input(_declared(denied), bounded=True).code == "EACCES"
    assert probe_input(_declared(denied), bounded=False).code == "EACCES"


def test_the_child_probe_agrees_with_the_direct_read_on_a_healthy_path(tmp_path: Path) -> None:
    (tmp_path / "footage").mkdir()

    assert probe_input(_declared(tmp_path / "footage"), bounded=True).readable is True
    assert probe_input(_declared(tmp_path / "footage"), bounded=False).readable is True


def test_only_a_path_that_can_block_pays_for_a_child_process(tmp_path: Path) -> None:
    """The bounded form buys nothing on internal storage and is not spent there."""
    config = _config(tmp_path)
    external = config.storage_root / "workbench" / "footage-library"
    external.mkdir(parents=True)

    assert is_external(config, external) is True
    assert is_external(config, tmp_path / "internal") is False
    assert is_external(config, config.projects_root / "demo") is False


def test_a_path_on_the_external_volume_is_read_in_the_child(tmp_path: Path, unreadable: Any) -> None:
    """Where a read can block, the mechanism has to be the killable one — not just the verdict.

    A path that merely *fails* answers identically either way, so the choice of mechanism is
    invisible in the outcome and has to be asserted on its own. On the real volume it is the
    whole difference between a 5s park and a thread parked for the life of the daemon.
    """
    config = _config(tmp_path)
    footage = config.storage_root / "workbench" / "footage-library"
    footage.mkdir(parents=True)
    project = _project(config, footage_root=str(footage))
    unreadable(footage)

    blocked = preflight_inputs(config, _job(), project)

    assert blocked.probe.bounded is True
    assert blocked.payload()["bounded"] is True


def test_a_path_on_internal_storage_is_read_here(tmp_path: Path, unreadable: Any) -> None:
    config = _config(tmp_path)
    footage = tmp_path / "internal-footage"
    footage.mkdir()
    project = _project(config, footage_root=str(footage))
    unreadable(footage)

    blocked = preflight_inputs(config, _job(), project)

    assert blocked.probe.bounded is False


# --- 2. what a stage declares ----------------------------------------------


def test_every_stage_declares_the_project_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)

    declared = declared_inputs(config, _job(), project)

    assert [item.path for item in declared][0] == project


def test_a_judgment_stage_declares_the_footage_root_its_brief_names(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config, footage_root=str(tmp_path / "footage-library"))

    declared = declared_inputs(config, _job(), project)

    assert tmp_path / "footage-library" in [item.path for item in declared]
    assert any("footage_root" in item.source for item in declared)


def test_the_doctrine_key_declares_the_same_footage_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config, authorized_footage_root=str(tmp_path / "footage-library"))

    declared = declared_inputs(config, _job(), project)

    assert tmp_path / "footage-library" in [item.path for item in declared]


def test_the_stage_that_walks_the_footage_root_declares_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config, footage_root=str(tmp_path / "footage-library"))

    declared = declared_inputs(config, _job("FOOTAGE_INDEXED", DETERMINISTIC), project)

    assert tmp_path / "footage-library" in [item.path for item in declared]


def test_a_deterministic_stage_that_never_reads_footage_does_not_declare_it(tmp_path: Path) -> None:
    """Over-declaring would park stages the wall cannot reach — an invented blocker."""
    config = _config(tmp_path)
    project = _project(config, footage_root=str(tmp_path / "footage-library"))

    declared = declared_inputs(config, _job("TECHNICAL_QC", DETERMINISTIC), project)

    assert [item.path for item in declared] == [project]


def test_a_project_declaring_no_footage_root_declares_no_footage_input(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)

    declared = declared_inputs(config, _job(), project)

    assert [item.path for item in declared] == [project]


def test_the_reference_is_declared_when_it_is_a_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config, reference_path=str(tmp_path / "reference.mp4"))

    declared = declared_inputs(config, _job(), project)

    assert tmp_path / "reference.mp4" in [item.path for item in declared]


def test_a_reference_url_is_not_probed_as_a_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config, reference_input="https://video.example/reel/ref-19/")

    declared = declared_inputs(config, _job(), project)

    assert [item.path for item in declared] == [project]


def test_an_unreadable_project_json_declares_the_project_directory_and_nothing_invented(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config)
    (project / "project.json").write_text("{not json", encoding="utf-8")

    declared = declared_inputs(config, _job(), project)

    assert [item.path for item in declared] == [project]


# --- 3. the verdict ---------------------------------------------------------


def test_a_readable_box_blocks_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    footage = tmp_path / "footage-library"
    footage.mkdir()
    project = _project(config, footage_root=str(footage))

    assert preflight_inputs(config, _job(), project) is None


def test_an_unreadable_footage_root_blocks_the_stage_and_names_path_and_errno(tmp_path: Path, unreadable: Any) -> None:
    config = _config(tmp_path)
    footage = tmp_path / "footage-library"
    footage.mkdir()
    project = _project(config, footage_root=str(footage))
    unreadable(footage)

    blocked = preflight_inputs(config, _job(), project)

    assert blocked is not None
    reason = blocked.describe()
    assert str(footage) in reason
    assert "EACCES" in reason
    assert "BLUEPRINT_LOCKED" in reason


def test_a_path_on_the_external_volume_cites_the_operator_call(tmp_path: Path, unreadable: Any) -> None:
    config = _config(tmp_path)
    footage = config.storage_root / "workbench" / "footage-library"
    footage.mkdir(parents=True)
    project = _project(config, footage_root=str(footage))
    unreadable(footage)

    blocked = preflight_inputs(config, _job(), project)

    assert blocked is not None
    assert TCC_CALL_ID in blocked.describe()


def test_a_path_on_internal_storage_does_not_cite_the_volume_call(tmp_path: Path, unreadable: Any) -> None:
    """Citing a TCC grant for a path the grant does not cover would send the operator nowhere."""
    config = _config(tmp_path)
    footage = tmp_path / "internal-footage"
    footage.mkdir()
    project = _project(config, footage_root=str(footage))
    unreadable(footage)

    blocked = preflight_inputs(config, _job(), project)

    assert blocked is not None
    assert TCC_CALL_ID not in blocked.describe()


def test_a_read_that_blocked_names_the_interpreter_that_needs_the_grant(tmp_path: Path) -> None:
    """Which binary the operator has to grant is the next action; a block answers it."""
    blocked = BlockedInput(
        stage="BLUEPRINT_LOCKED",
        kind=JUDGMENT,
        probe=InputProbe(
            declared=_declared(Path("/Volumes/WORKDRIVE/workbench/footage-library")),
            readable=False,
            seconds=5.0,
            code="TIMEOUT",
        ),
        external=True,
    )

    reason = blocked.describe()

    assert sys.executable in reason
    assert TCC_CALL_ID in reason


def test_a_read_that_failed_outright_does_not_lecture_about_grants(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config, footage_root=str(tmp_path / "not-mounted"))

    reason = preflight_inputs(config, _job(), project).describe()

    assert sys.executable not in reason
    assert "ENOENT" in reason


def test_an_open_call_that_already_covers_the_path_is_named(tmp_path: Path, unreadable: Any) -> None:
    config = _config(tmp_path)
    footage = tmp_path / "footage-library"
    footage.mkdir()
    project = _project(config, footage_root=str(footage))
    unreadable(footage)
    calls = [
        {"call_id": "CALL-SOMETHING-ELSE", "state": "AWAITING_OPERATOR", "issue_type": "mode-confirmation"},
        {"call_id": "CALL-THE-ONE", "state": "AWAITING_OPERATOR", "issue_type": "environment-permission"},
    ]

    reason = preflight_inputs(config, _job(), project).describe(open_calls=calls)

    assert "CALL-THE-ONE" in reason
    assert "CALL-SOMETHING-ELSE" not in reason


def test_an_answered_call_is_not_offered_as_cover(tmp_path: Path, unreadable: Any) -> None:
    config = _config(tmp_path)
    footage = tmp_path / "footage-library"
    footage.mkdir()
    project = _project(config, footage_root=str(footage))
    unreadable(footage)
    answered = [{"call_id": "CALL-ANSWERED", "state": "ANSWERED", "issue_type": "environment-permission"}]

    reason = preflight_inputs(config, _job(), project).describe(open_calls=answered)

    assert "CALL-ANSWERED" not in reason


def test_one_blocker_is_reported_and_it_is_the_footage_root(tmp_path: Path, unreadable: Any) -> None:
    """One blocker, named exactly — a list of everything wrong is not a next action."""
    config = _config(tmp_path)
    footage = tmp_path / "footage-library"
    footage.mkdir()
    project = _project(config, footage_root=str(footage), reference_path=str(tmp_path / "gone.mp4"))
    unreadable(footage)

    blocked = preflight_inputs(config, _job(), project)

    assert blocked.probe.declared.path == footage
    assert blocked.payload()["path"] == str(footage)
    assert blocked.payload()["code"] == "EACCES"


def test_the_probe_order_puts_the_project_directory_first(tmp_path: Path, unreadable: Any) -> None:
    config = _config(tmp_path)
    footage = tmp_path / "footage-library"
    footage.mkdir()
    project = _project(config, footage_root=str(footage))
    unreadable(project)

    blocked = preflight_inputs(config, _job(), project)

    assert blocked is not None
    assert blocked.probe.declared.path == project


def test_a_blocked_verdict_carries_the_probe_duration_for_the_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    project = _project(config, footage_root=str(tmp_path / "not-mounted"))

    blocked = preflight_inputs(config, _job(), project)

    assert blocked is not None
    assert blocked.payload()["seconds"] >= 0.0
    assert blocked.payload()["stage"] == "BLUEPRINT_LOCKED"


def test_the_probe_never_writes_to_the_path_it_tests(tmp_path: Path) -> None:
    config = _config(tmp_path)
    footage = tmp_path / "footage-library"
    footage.mkdir()
    (footage / "clip.mp4").write_bytes(b"0")
    project = _project(config, footage_root=str(footage))
    before = {path.name: path.stat().st_mtime_ns for path in footage.iterdir()}

    preflight_inputs(config, _job(), project)

    assert {path.name: path.stat().st_mtime_ns for path in footage.iterdir()} == before
    assert sorted(item.name for item in footage.iterdir()) == ["clip.mp4"]
