from __future__ import annotations

import json
from pathlib import Path

from reelctl.cli import main


def test_help_returns_zero() -> None:
    assert main(["--help"], exit_on_error=False) == 0


def test_new_and_status_are_durable(tmp_path: Path, monkeypatch, capsys) -> None:
    reference = tmp_path / "r.mp4"
    reference.write_bytes(b"not-media-yet")
    projects = tmp_path / "projects"
    code = main(
        ["--projects-root", str(projects), "new", "demo", "--reference", str(reference), "--footage", str(tmp_path), "--defer-analysis"],
        exit_on_error=False,
    )
    assert code == 0
    assert (projects / "demo/project.json").is_file()
    assert (projects / "demo/state.json").is_file()
    code = main(["--projects-root", str(projects), "status", "demo", "--json"], exit_on_error=False)
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["project_id"] == "demo"
    assert payload["next_stage"] == "REFERENCE_LOCKED"
