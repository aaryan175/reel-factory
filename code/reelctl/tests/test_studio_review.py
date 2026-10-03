from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from test_studio_board_ui import request, studio_config
from test_studio_calls import PROJECT_JSON

from reelctl.hashing import sha256_file
from reelctl.qc import combine_authorities
from reelctl.signing import verify_payload
from reelctl.state import STAGES, ProjectState, StateError
from reelctl.studio import review as review_module
from reelctl.studio.config import StudioConfig
from reelctl.studio.events import STREAM_EVENT_NAME
from reelctl.studio.status import FAILED, PROVEN, WITHHELD
from reelctl.web import create_app

REVISION = "v001"
QC_DIR = "review/qc-{}".format(REVISION)
QC_REPORT = "{}/qc-report.json".format(QC_DIR)
BOARD = "{}/reference-candidate-board.jpg".format(QC_DIR)
CANDIDATE = "edit/render-{rev}/demo-candidate-review-{rev}.mp4".format(rev=REVISION)
REFERENCE = "reference/reference.mp4"
REFERENCE_LOCK = "reference/reference-lock.json"

FEEDBACK = {
    "target": "block B3, frames 96-118",
    "observation": "the candidate cuts on the beat after the reference does; the shot also collapses to black two seconds in",
    "requested_change": "re-cut B3 against the reference onset and pick a clip that holds exposure through the whole interval",
    "scope": "selection",
}


def _write(project: Path, relative: str, payload: Any) -> str:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload), encoding="utf-8")
    return relative


def _stage_outputs(stage: str) -> Sequence[str]:
    if stage in {"TECHNICAL_QC", "STRUCTURE_QC", "VISUAL_QC"}:
        return [QC_REPORT]
    if stage in {"LOCAL_REVIEW_READY", "RENDERED"}:
        return [CANDIDATE]
    if stage == "REFERENCE_LOCKED":
        # What `reelctl reference analyze` actually binds: the project config it just
        # rewrote and the lock, not the reference bytes themselves.
        return ["project.json", REFERENCE_LOCK]
    return ["edit/{}.json".format(stage.lower())]


def _complete(project: Path, stage: str, *, status: str = "PASS", reason: Optional[str] = None) -> None:
    outputs = list(_stage_outputs(stage))
    for relative in outputs:
        if not (project / relative).exists():
            _write(project, relative, {"stage": stage})
    state = ProjectState.load(project / "state.json")
    state.complete(stage, "input-hash-{}".format(stage.lower()), outputs, status=status, reason=reason)


def _qc_report(project: Path, *, visual: str = "PASS", structure: str = "PASS", technical: str = "PASS") -> Dict[str, Any]:
    candidate = project / CANDIDATE
    return {
        "schema_version": 1,
        "revision": REVISION,
        "candidate": str(candidate),
        "candidate_sha256": sha256_file(candidate),
        "reference_sha256": sha256_file(project / REFERENCE),
        "render_receipt_sha256": "0" * 64,
        "technical": {"status": technical},
        "structure": {
            "status": structure,
            "checks": {
                "reference_audio_timeline_identity": True,
                "presentation_clock_identity": True,
                "hard_cuts_within_two_frames_of_reference_onsets": True,
            },
        },
        "visual": {"status": visual, "reason": None if visual == "PASS" else "reference-relative parity failed on B3"},
        "human": {"status": "PENDING", "receipt": None, "untrusted_project_receipt_ignored": False, "reason": "not recorded"},
        "authorities": combine_authorities(technical=technical, structure=structure, visual=visual, human="PENDING"),
    }


def review_app(
    tmp_path: Path,
    *,
    project_id: str = "reel-demo-v1",
    visual: str = "PASS",
    ready: bool = True,
) -> Tuple[Any, StudioConfig, Path]:
    """A project whose machine work is finished and whose only missing evidence is a human."""
    config = studio_config(tmp_path)
    project = Path(config.projects_root) / project_id
    # The same tree `reelctl new` lays down, including the cache directory `command_status`
    # requires — a fixture that skipped it would pass tests the real project would fail.
    for child in ("reference", "footage", "edit", "assets", "review", "deliver", "brain", ".reelctl/cache"):
        (project / child).mkdir(parents=True, exist_ok=True)
    ProjectState.create(project / "state.json", project_id)

    _write(project, REFERENCE, b"reference-bytes")
    _write(project, CANDIDATE, b"candidate-bytes-for-review")
    _write(project, BOARD, b"\xff\xd8\xff\xe0board-jpeg-bytes")
    # `reelctl reference analyze` rewrites project.json with the copied reference's path.
    (project / "project.json").write_text(
        json.dumps({**PROJECT_JSON, "project_id": project_id, "reference_path": str(project / REFERENCE)}), encoding="utf-8"
    )
    _write(project, REFERENCE_LOCK, {"schema_version": 1, "source": {"sha256": sha256_file(project / REFERENCE)}})
    _write(project, QC_REPORT, _qc_report(project, visual=visual))

    last = "LOCAL_REVIEW_READY" if ready else "VISUAL_QC"
    for stage in STAGES[: STAGES.index(last) + 1]:
        if stage == "VISUAL_QC" and visual != "PASS":
            _complete(project, stage, status="FAIL", reason="reference-relative parity failed on B3")
            break
        _complete(project, stage)
    Path(config.registry_path).write_text(json.dumps({"schema_version": 2.0, "reels": [], "lanes": {}, "open_calls": []}), encoding="utf-8")
    return create_app(config=config), config, project


# --- the bundle -----------------------------------------------------------


def test_the_review_bundle_names_every_artifact_the_machine_compared(tmp_path: Path) -> None:
    app, _, project = review_app(tmp_path)

    payload = request(app, "GET", "/api/reels/reel-demo-v1/review").json()

    assert payload["status"] == "PASS"
    assert payload["revision"] == REVISION
    assert payload["candidate"]["path"] == CANDIDATE
    assert payload["candidate"]["sha256"] == sha256_file(project / CANDIDATE)
    assert payload["reference"]["path"] == REFERENCE
    assert payload["reference"]["sha256"] == sha256_file(project / REFERENCE)
    assert payload["board"]["path"] == BOARD
    assert payload["board"]["sha256"] == sha256_file(project / BOARD)
    assert payload["qc"]["path"] == QC_REPORT
    assert payload["qc"]["sha256"] == sha256_file(project / QC_REPORT)
    assert payload["qc"]["bound_to_stage"] in {"TECHNICAL_QC", "STRUCTURE_QC", "VISUAL_QC"}


def test_every_player_source_is_served_through_the_confined_artifact_reader(tmp_path: Path) -> None:
    app, _, project = review_app(tmp_path)

    payload = request(app, "GET", "/api/reels/reel-demo-v1/review").json()
    for key in ("candidate", "reference", "board"):
        url = payload[key]["artifact_url"]
        assert url.startswith("/api/projects/reel-demo-v1/artifact?path="), key
        assert request(app, "GET", url).status_code == 200, key


def test_the_layer_panel_is_the_task_2_read_model_not_a_second_opinion(tmp_path: Path) -> None:
    app, config, project = review_app(tmp_path)
    from reelctl.studio.status import project_layers

    payload = request(app, "GET", "/api/reels/reel-demo-v1/review").json()

    assert payload["layers"] == project_layers(Path(config.projects_root) / "reel-demo-v1")["layers"]
    assert payload["headline"] == project_layers(Path(config.projects_root) / "reel-demo-v1")["headline"]


def test_the_bundle_reports_pending_human_before_a_verdict_exists(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path)

    payload = request(app, "GET", "/api/reels/reel-demo-v1/review").json()
    human = next(layer for layer in payload["layers"] if layer["key"] == "human_approval")

    assert human["status"] == "PENDING_HUMAN"
    assert payload["can_approve"] is True
    assert payload["approve_refusal"] is None


# --- approve is refused at the API, not merely hidden ---------------------


def test_approve_is_refused_by_the_api_when_local_review_is_not_ready(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path, visual="FAIL", ready=False)

    bundle = request(app, "GET", "/api/reels/reel-demo-v1/review").json()
    response = request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve"})

    assert bundle["can_approve"] is False
    assert "local_review_ready" in bundle["approve_refusal"]
    assert response.status_code == 400
    assert "local_review_ready" in response.json()["error"]


def test_a_refused_approval_writes_nothing(tmp_path: Path) -> None:
    app, config, project = review_app(tmp_path, visual="FAIL", ready=False)
    before = sha256_file(project / "state.json")

    request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve"})

    assert sha256_file(project / "state.json") == before
    assert not (project / "review" / "verdicts").exists()


def test_the_screen_hides_approve_when_the_api_would_refuse_it(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path, visual="FAIL", ready=False)

    page = request(app, "GET", "/review/reel-demo-v1").text

    assert 'value="approve"' not in page
    assert "local_review_ready" in page
    assert 'value="reject"' in page


def test_a_candidate_that_no_longer_matches_the_qc_report_cannot_be_approved(tmp_path: Path) -> None:
    """Approving bytes QC never saw is the exact shape of the operator's false PASS."""
    app, _, project = review_app(tmp_path)
    (project / CANDIDATE).write_bytes(b"a different candidate entirely")

    response = request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve"})

    assert response.status_code == 400
    assert "no longer matches" in response.json()["error"]


# --- approval binds the hashes -------------------------------------------


def test_a_recorded_approval_binds_candidate_qc_and_reference_hashes(tmp_path: Path) -> None:
    app, _, project = review_app(tmp_path)

    response = request(
        app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve", "notes": "watched at normal speed"}
    )

    assert response.status_code == 201, response.text
    receipt = json.loads(Path(response.json()["receipt"]).read_text(encoding="utf-8"))
    assert receipt["verdict"] == "APPROVE"
    assert receipt["candidate"]["sha256"] == sha256_file(project / CANDIDATE)
    assert receipt["qc_report"]["sha256"] == sha256_file(project / QC_REPORT)
    assert receipt["reference"]["sha256"] == sha256_file(project / REFERENCE)
    assert receipt["comparison_board"]["sha256"] == sha256_file(project / BOARD)
    assert receipt["notes"] == "watched at normal speed"
    assert verify_payload(receipt, purpose="studio-human-verdict-v1")["status"] == "PASS"


def test_an_approval_receipt_survives_stage_re_verification(tmp_path: Path) -> None:
    app, config, project = review_app(tmp_path)

    request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve"})
    state = ProjectState.load(project / "state.json")

    assert state.stage("HUMAN_APPROVED")["status"] == "PASS"
    assert state.verify_stage("HUMAN_APPROVED")["status"] == "PASS"
    bound = [item["path"] for item in state.stage("HUMAN_APPROVED")["output_receipts"]]
    assert any(path.startswith("review/verdicts/") for path in bound)


def test_tampering_with_the_approval_receipt_flips_the_layer_to_failed(tmp_path: Path) -> None:
    app, config, project = review_app(tmp_path)
    request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve"})
    verdict_path = next((project / "review" / "verdicts").glob("*.json"))

    payload = json.loads(verdict_path.read_text(encoding="utf-8"))
    payload["verdict"] = "REJECT"
    verdict_path.write_text(json.dumps(payload), encoding="utf-8")

    bundle = request(app, "GET", "/api/reels/reel-demo-v1/review").json()
    human = next(layer for layer in bundle["layers"] if layer["key"] == "human_approval")
    assert human["status"] == FAILED
    try:
        ProjectState.load(project / "state.json").verify_stage("HUMAN_APPROVED")
        raise AssertionError("a tampered verdict must not verify")
    except StateError as exc:
        assert "changed after completion" in str(exc)


def test_approving_the_same_candidate_twice_is_refused_rather_than_overwritten(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path)

    first = request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve"})
    second = request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve", "notes": "again"})

    assert first.status_code == 201
    assert second.status_code == 400
    assert "same input hash" in second.json()["error"]


# --- rejection writes a negative fixture ----------------------------------


def test_a_rejection_writes_both_a_receipt_and_a_negative_fixture(tmp_path: Path) -> None:
    app, _, project = review_app(tmp_path)

    response = request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "reject", "feedback": FEEDBACK})

    assert response.status_code == 201, response.text
    body = response.json()
    receipt = json.loads(Path(body["receipt"]).read_text(encoding="utf-8"))
    fixture = json.loads(Path(body["negative_fixture"]).read_text(encoding="utf-8"))

    assert receipt["verdict"] == "REJECT"
    assert receipt["feedback"] == FEEDBACK
    assert fixture["artifact_type"] == "studio-negative-fixture-v1"
    assert fixture["verdict"] == "REJECT"
    assert fixture["feedback"] == FEEDBACK
    assert fixture["candidate"]["sha256"] == sha256_file(project / CANDIDATE)
    assert fixture["reference"]["sha256"] == sha256_file(project / REFERENCE)
    assert verify_payload(fixture, purpose="studio-negative-fixture-v1")["status"] == "PASS"


def test_a_rejection_without_a_structured_feedback_event_is_refused(tmp_path: Path) -> None:
    app, _, project = review_app(tmp_path)

    response = request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "reject"})

    assert response.status_code == 422
    assert not (project / "review" / "negative-fixtures").exists()


def test_a_feedback_event_missing_a_field_is_refused_by_name(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path)
    partial = {key: value for key, value in FEEDBACK.items() if key != "requested_change"}

    response = request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "reject", "feedback": partial})

    assert response.status_code == 422
    assert "requested_change" in response.text


def test_a_rejection_records_the_stage_as_failed_with_the_operators_reason(tmp_path: Path) -> None:
    app, _, project = review_app(tmp_path)

    request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "reject", "feedback": FEEDBACK})
    stage = ProjectState.load(project / "state.json").stage("HUMAN_APPROVED")
    bundle = request(app, "GET", "/api/reels/reel-demo-v1/review").json()
    human = next(layer for layer in bundle["layers"] if layer["key"] == "human_approval")

    assert stage["status"] == "FAIL"
    assert FEEDBACK["observation"] in stage["reason"]
    assert human["status"] == FAILED
    assert FEEDBACK["observation"] in human["reason"]


def test_request_changes_withholds_rather_than_fails(tmp_path: Path) -> None:
    """A change request is a named refusal to claim, not a verdict that the reel is broken."""
    app, _, project = review_app(tmp_path)

    response = request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "changes", "feedback": FEEDBACK})
    stage = ProjectState.load(project / "state.json").stage("HUMAN_APPROVED")
    bundle = request(app, "GET", "/api/reels/reel-demo-v1/review").json()
    human = next(layer for layer in bundle["layers"] if layer["key"] == "human_approval")

    assert response.status_code == 201
    assert stage["status"] == "BLOCKED"
    assert human["status"] == WITHHELD
    assert FEEDBACK["requested_change"] in human["reason"]
    assert Path(response.json()["negative_fixture"]).is_file()


def test_a_rejection_is_appended_to_the_projects_decision_log(tmp_path: Path) -> None:
    app, _, project = review_app(tmp_path)
    log = project / "brain" / "08_DECISION_LOG.md"
    log.write_text("# 08 — DECISION LOG\n\n**D-existing** — a prior entry.\n", encoding="utf-8")

    request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "reject", "feedback": FEEDBACK})
    text = log.read_text(encoding="utf-8")

    assert "**D-existing** — a prior entry." in text
    assert "REJECT" in text
    assert FEEDBACK["requested_change"] in text
    assert "review/negative-fixtures/" in text


def test_every_verdict_is_listed_in_the_bundle_afterwards(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path)

    request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "reject", "feedback": FEEDBACK})
    request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve", "notes": "re-watched, the cut reads"})

    bundle = request(app, "GET", "/api/reels/reel-demo-v1/review").json()
    assert [item["verdict"] for item in bundle["verdicts"]] == ["REJECT", "APPROVE"]
    assert all(item["candidate_sha256"] for item in bundle["verdicts"])


def test_an_unknown_verdict_is_refused_at_the_schema(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path)

    response = request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "looks-fine"})

    assert response.status_code == 422


# --- the screen -----------------------------------------------------------


def test_the_review_screen_puts_both_players_and_the_board_on_the_page(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path)

    page = request(app, "GET", "/review/reel-demo-v1").text

    assert 'id="reference-player"' in page
    assert 'id="candidate-player"' in page
    assert "reference-candidate-board.jpg" in page
    assert "frame-step" in page
    assert 'value="approve"' in page


def test_the_review_screen_renders_a_withheld_layer_reason_verbatim(tmp_path: Path) -> None:
    app, config, project = review_app(tmp_path)
    reason = "no candidate performs the reference role for block 03"
    ProjectState.load(project / "state.json").block("HUMAN_APPROVED", "human-input-hash", reason)

    page = request(app, "GET", "/review/reel-demo-v1").text

    assert reason in page
    assert WITHHELD in page


def test_the_review_screen_never_paints_a_blocked_layer_with_the_proven_class(tmp_path: Path) -> None:
    app, _, project = review_app(tmp_path)
    ProjectState.load(project / "state.json").block("HUMAN_APPROVED", "human-input-hash", "the operator has not decided")

    page = request(app, "GET", "/review/reel-demo-v1").text
    from reelctl.studio import views

    panel = page.split('id="layer-panel"', 1)[1].split("</section>", 1)[0]
    human = panel.split('data-layer="human_approval"', 1)[1].split("</li>", 1)[0]
    assert views.STATUS_CLASS[WITHHELD] in human
    assert views.STATUS_CLASS[PROVEN] not in human


def test_a_project_with_no_qc_report_says_so_instead_of_rendering_an_empty_room(tmp_path: Path) -> None:
    config = studio_config(tmp_path)
    project = Path(config.projects_root) / "reel-bare-v1"
    for child in ("reference", "edit", "review", "brain"):
        (project / child).mkdir(parents=True, exist_ok=True)
    (project / "project.json").write_text(json.dumps({**PROJECT_JSON, "project_id": "reel-bare-v1"}), encoding="utf-8")
    ProjectState.create(project / "state.json", "reel-bare-v1")
    app = create_app(config=config)

    payload = request(app, "GET", "/api/reels/reel-bare-v1/review").json()
    page = request(app, "GET", "/review/reel-bare-v1").text

    assert payload["qc"]["path"] is None
    assert "no QC report" in payload["qc"]["reason"]
    assert payload["can_approve"] is False
    assert "no QC report" in page


def test_the_module_refuses_a_verdict_on_a_project_that_does_not_exist(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path)

    response = request(app, "POST", "/api/reels/reel-missing-v1/verdict", json_body={"verdict": "approve"})

    assert response.status_code == 400
    assert "reel-missing-v1" in response.json()["error"]


def test_recording_a_verdict_publishes_an_event(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path)

    request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve"})
    events = request(app, "GET", "/api/stream?after=0&max_seconds=0.05&poll_ms=10").text

    # Every frame carries the one constant SSE name; the row's real kind is in the payload.
    assert "event: {}".format(STREAM_EVENT_NAME) in events
    assert '"kind": "verdict"' in events
    assert "APPROVE" in events


def test_the_verdict_helper_is_callable_without_the_web_layer(tmp_path: Path) -> None:
    """The screens are one caller of this module, not its definition."""
    _, config, project = review_app(tmp_path)

    bundle = review_module.review_bundle(config, "reel-demo-v1")
    result = review_module.record_verdict(config, "reel-demo-v1", verdict="approve", reviewer="operator")

    assert bundle["candidate"]["sha256"] == sha256_file(project / CANDIDATE)
    assert result["verdict"] == "APPROVE"
    assert Path(result["receipt"]).is_file()
