"""Regression-manifest audit, exercised on synthetic files.

The real regression media never ship with the repository.  These tests build a
small synthetic manifest in a temp dir and check that ``audit_real_fixtures``
verifies hashes and classifies verdicts.  The shipped template manifest is only
checked for shape.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from reelctl.fixtures import DEFAULT_MANIFEST, audit_real_fixtures


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload))
    return path


def _synthetic_manifest(tmp_path: Path, *, corrupt: bool = False) -> Path:
    accepted = _write(tmp_path / "accepted/candidate.mp4", b"accepted-candidate")
    _write(tmp_path / "accepted/render-contract.json", {"ok": True})
    unresolved = _write(tmp_path / "unresolved/candidate.mp4", b"unresolved-candidate")
    evidence = _write(tmp_path / "unresolved/qc.json", {"status": "PASS_FOR_USER_REVIEW", "human_approval": "PENDING"})
    collapsed = _write(tmp_path / "collapsed/candidate.mp4", b"collapsed-candidate")
    _write(tmp_path / "collapsed/blueprint.json", {"hard_cuts_after": list(range(5)), "picture_blocks": [{}] * 6})
    _write(tmp_path / "collapsed/selection.json", {"shots": [{}] * 3})
    manifest = {
        "schema_version": 1,
        "fixtures": [
            {
                "id": "accepted",
                "expected_verdict": "ACCEPTED",
                "candidate": "accepted/candidate.mp4",
                "candidate_sha256": "0" * 64 if corrupt else _sha(accepted),
                "evidence": "accepted/render-contract.json",
            },
            {
                "id": "unresolved",
                "expected_verdict": "UNRESOLVED",
                "candidate": "unresolved/candidate.mp4",
                "candidate_sha256": _sha(unresolved),
                "evidence": "unresolved/qc.json",
                "evidence_sha256": _sha(evidence),
                "required_evidence": {"status": "PASS_FOR_USER_REVIEW", "human_approval": "PENDING"},
            },
            {
                "id": "collapsed",
                "expected_verdict": "REJECTED",
                "candidate": "collapsed/candidate.mp4",
                "candidate_sha256": _sha(collapsed),
                "blueprint": "collapsed/blueprint.json",
                "selection": "collapsed/selection.json",
                "required_failure": {"reference_picture_blocks": 6, "reference_hard_cuts": 5, "selected_picture_slots": 3},
            },
        ],
    }
    return _write(tmp_path / "manifest.json", manifest)


def test_synthetic_manifest_audits_clean(tmp_path: Path) -> None:
    report = audit_real_fixtures(_synthetic_manifest(tmp_path))
    assert report["status"] == "PASS", report
    assert {row["id"]: row["observed_verdict"] for row in report["fixtures"]} == {
        "accepted": "ACCEPTED",
        "unresolved": "UNRESOLVED",
        "collapsed": "REJECTED",
    }


def test_a_hash_mismatch_fails_the_audit(tmp_path: Path) -> None:
    report = audit_real_fixtures(_synthetic_manifest(tmp_path, corrupt=True))
    assert report["status"] == "FAIL"
    accepted = next(row for row in report["fixtures"] if row["id"] == "accepted")
    assert accepted["checks"]["candidate_hash"] is False


def test_the_shipped_manifest_is_a_template_with_no_real_hashes() -> None:
    manifest = json.loads(DEFAULT_MANIFEST.read_text())
    for fixture in manifest["fixtures"]:
        for key, value in fixture.items():
            if key.endswith("sha256"):
                assert value.startswith("<"), f"{fixture['id']}.{key} must be a placeholder"


@pytest.mark.skip(reason="needs your own regression media; point `reelctl fixtures audit --manifest` at a filled-in manifest")
def test_real_regression_media_audit() -> None:
    assert audit_real_fixtures()["status"] == "PASS"
