from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .hashing import load_json, sha256_file
from .qc import reference_relative_visual_qc

DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "fixtures/real-regressions.json"


def _resolver(manifest_dir: Path):
    """Manifest paths may be absolute, ~-relative, or relative to the manifest file."""

    def resolve(value: Any) -> Path:
        candidate = Path(str(value)).expanduser()
        return candidate if candidate.is_absolute() else manifest_dir / candidate

    return resolve


def audit_real_fixtures(manifest_path: Optional[Path] = None) -> Dict[str, Any]:
    """Audit a regression manifest (see fixtures/real-regressions.json for the template).

    The shipped manifest is a template with placeholder hashes; point
    ``manifest_path`` at your own manifest describing your own regression media.
    """
    path = Path(manifest_path or DEFAULT_MANIFEST).resolve()
    manifest = load_json(path)
    resolve = _resolver(path.parent)
    results = []
    harness_pass = True
    for fixture in manifest["fixtures"]:
        candidate = resolve(fixture["candidate"])
        checks: Dict[str, Any] = {
            "candidate_exists": candidate.is_file(),
            "candidate_hash": candidate.is_file() and sha256_file(candidate) == fixture["candidate_sha256"],
        }
        observed_verdict = "UNKNOWN"
        if fixture["expected_verdict"] == "REJECTED" and fixture.get("reference"):
            reference = resolve(fixture["reference"])
            blueprint_path = resolve(fixture["blueprint"])
            human_verdict_path = resolve(fixture["human_verdict_receipt"])
            checks.update(
                {
                    "reference_exists": reference.is_file(),
                    "reference_hash": reference.is_file() and sha256_file(reference) == fixture["reference_sha256"],
                    "blueprint_hash": blueprint_path.is_file() and sha256_file(blueprint_path) == fixture["blueprint_sha256"],
                    "human_verdict_hash": human_verdict_path.is_file()
                    and sha256_file(human_verdict_path) == fixture["human_verdict_receipt_sha256"],
                }
            )
            if all(checks.values()):
                blueprint = load_json(blueprint_path)
                human_verdict = load_json(human_verdict_path)
                machine = reference_relative_visual_qc(
                    reference,
                    candidate,
                    blueprint["picture_blocks"],
                    mode=str(fixture.get("mode", "reference-locked")),
                )
                boundary = int(fixture["required_failure"]["visual_quality_degrades_after_frame"])
                checks.update(
                    {
                        "human_rejection": human_verdict.get("human_creative_approval") == "REJECTED",
                        "human_verdict_candidate_hash": human_verdict.get("artifact_sha256") == fixture["candidate_sha256"],
                        "machine_reference_relative_rejection": machine["status"] == "FAIL",
                        "machine_failure_after_reported_boundary": any(
                            row["status"] == "FAIL" and int(row["start_frame"]) >= boundary for row in machine["blocks"]
                        ),
                    }
                )
            observed_verdict = "REJECTED" if all(checks.values()) else "FALSE_PASS"
        elif fixture["expected_verdict"] == "REJECTED":
            blueprint = load_json(resolve(fixture["blueprint"]))
            selection = load_json(resolve(fixture["selection"]))
            required = fixture["required_failure"]
            checks.update(
                {
                    "reference_picture_blocks": len(blueprint["picture_blocks"]) == int(required["reference_picture_blocks"]),
                    "reference_hard_cuts": len(blueprint["hard_cuts_after"]) == int(required["reference_hard_cuts"]),
                    "selected_picture_slots": len(selection.get("slots", selection.get("shots", [])))
                    == int(required["selected_picture_slots"]),
                    "collapsed_structure_is_detected": len(selection.get("slots", selection.get("shots", [])))
                    != len(blueprint["picture_blocks"]),
                }
            )
            observed_verdict = "REJECTED" if checks["collapsed_structure_is_detected"] else "FALSE_PASS"
        elif fixture["expected_verdict"] == "UNRESOLVED":
            evidence = resolve(fixture["evidence"])
            checks["evidence_hash"] = evidence.is_file() and sha256_file(evidence) == fixture["evidence_sha256"]
            if checks["evidence_hash"]:
                evidence_payload = load_json(evidence)
                checks["pending_human_approval"] = all(
                    evidence_payload.get(key) == value for key, value in fixture["required_evidence"].items()
                )
            observed_verdict = "UNRESOLVED" if all(checks.values()) else "UNVERIFIED"
        else:
            evidence = resolve(fixture["evidence"])
            checks["acceptance_evidence_exists"] = evidence.is_file()
            observed_verdict = "ACCEPTED" if checks["acceptance_evidence_exists"] else "UNVERIFIED"
        fixture_pass = all(checks.values()) and observed_verdict == fixture["expected_verdict"]
        harness_pass = harness_pass and fixture_pass
        results.append(
            {
                "id": fixture["id"],
                "expected_verdict": fixture["expected_verdict"],
                "observed_verdict": observed_verdict,
                "status": "PASS" if fixture_pass else "FAIL",
                "checks": checks,
            }
        )
    return {"status": "PASS" if harness_pass else "FAIL", "manifest": str(path), "fixtures": results}
