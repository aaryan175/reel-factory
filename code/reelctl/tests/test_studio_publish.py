from __future__ import annotations

import json
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from test_studio_board_ui import request, studio_config  # noqa: F401  (studio_config used by review_app)
from test_studio_review import CANDIDATE, QC_REPORT, REFERENCE, review_app

from reelctl.hashing import sha256_file
from reelctl.signing import verify_payload
from reelctl.state import ProjectState
from reelctl.studio import publish as publish_module
from reelctl.studio.config import StudioConfig
from reelctl.studio.jobs import due_schedules

# A neutral offset, on purpose: the fixture proves the +24h/+72h arithmetic crosses a
# non-UTC offset correctly, without carrying anybody's real locale into the repository.
PUBLISHED_AT = "2025-01-10T20:09:00+05:30"
DUE_24H = "2025-01-11T14:39:00Z"
DUE_72H = "2025-01-13T14:39:00Z"

CARD = {
    "destination": "Instagram @example-account via a scheduling tool",
    "caption": "<caption A>",
    "schedule": "2025-01-15T19:30:00+05:30",
    "timezone": "Etc/GMT-5",
    "internal_note": "<song> — trial B",
    "audio_label": "<song>",
    "cover": "unset — the scheduling tool does not expose the cover control",
    "disclosure": "no AI-content disclosure; this is a normal edit of authorized footage",
    "auto_expansion": False,
}

PUBLICATION_RECEIPT = {
    "schema": "reel-publication-receipt-v1",
    "experiment_id": "experiment_002",
    "status": "published",
    "published_at": PUBLISHED_AT,
    "timezone": "Etc/GMT-5",
    "network": "instagram",
    "post_type": "REEL",
    "caption": CARD["caption"],
    "instagram": {"post_id": "POST-0001", "public_url": "https://video.example/reel/POST-0001/", "http_receipt": 200},
    "scheduler": {"account_id": "<scheduler-account-id>", "scheduled_post_id": "<scheduled-post-id>", "provider_status": "PUBLISHED"},
}

METRICS_24H = {
    "views": 100,
    "reach": 80,
    "average_watch_time_seconds": 3.0,
    "three_second_view_rate_percent": 40.0,
    "likes": 4,
    "comments": 0,
    "saves": 0,
    "shares": 0,
}


def _approved(tmp_path: Path) -> Tuple[Any, StudioConfig, Path]:
    """A project whose candidate the operator has already approved in the review room."""
    app, config, project = review_app(tmp_path)
    assert request(app, "POST", "/api/reels/reel-demo-v1/verdict", json_body={"verdict": "approve"}).status_code == 201
    return app, config, project


def _publication_receipt(project: Path, payload: Optional[Dict[str, Any]] = None) -> Path:
    body = dict(PUBLICATION_RECEIPT if payload is None else payload)
    body.setdefault("asset", {"local_path": CANDIDATE, "sha256": sha256_file(project / CANDIDATE)})
    path = project / "deliver" / "publication-receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _schedules(config: StudioConfig) -> List[Dict[str, Any]]:
    return due_schedules(config.database_path, now=datetime(2030, 1, 1, tzinfo=timezone.utc))


# --- the card -------------------------------------------------------------


def test_the_card_carries_every_field_the_operating_system_contract_names(tmp_path: Path) -> None:
    app, _, project = _approved(tmp_path)

    card = request(app, "GET", "/api/publish/reel-demo-v1/card").json()

    for field in (
        "destination",
        "asset",
        "post_type",
        "caption",
        "internal_note",
        "audio_label",
        "schedule",
        "timezone",
        "cover",
        "disclosure",
        "auto_expansion",
        "unresolved",
    ):
        assert field in card, field
    assert card["post_type"] == "REEL"
    assert card["asset"]["path"] == CANDIDATE
    assert card["asset"]["sha256"] == sha256_file(project / CANDIDATE)


def test_a_field_the_app_cannot_know_is_unresolved_rather_than_invented(tmp_path: Path) -> None:
    app, _, _ = _approved(tmp_path)

    card = request(app, "GET", "/api/publish/reel-demo-v1/card").json()

    assert card["destination"]["value"] is None
    assert "no machine-readable destination" in card["destination"]["reason"]
    unresolved = {item["field"] for item in card["unresolved"]}
    assert {"destination", "caption", "schedule", "timezone"} <= unresolved
    assert card["ready_to_approve"] is False


def test_the_card_states_the_platform_limitations_the_app_cannot_clear(tmp_path: Path) -> None:
    app, _, _ = _approved(tmp_path)

    card = request(app, "GET", "/api/publish/reel-demo-v1/card").json()
    limitations = " ".join(card["platform_limitations"])

    assert "no outward call" in limitations
    assert "scheduling tool" in limitations


def test_the_card_refuses_to_compose_for_a_candidate_the_operator_has_not_approved(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path)

    card = request(app, "GET", "/api/publish/reel-demo-v1/card").json()

    assert card["ready_to_approve"] is False
    assert "HUMAN_APPROVED" in card["approval_refusal"]


# --- recording the approval ----------------------------------------------


def test_recording_an_approval_writes_a_hash_bound_receipt_and_nothing_else(tmp_path: Path) -> None:
    app, _, project = _approved(tmp_path)

    response = request(app, "POST", "/api/publish/reel-demo-v1/approve", json_body=CARD)

    assert response.status_code == 201, response.text
    body = response.json()
    receipt = json.loads(Path(body["receipt"]).read_text(encoding="utf-8"))
    assert receipt["intent"] == "DELIVERY_APPROVED"
    assert receipt["asset"]["sha256"] == sha256_file(project / CANDIDATE)
    assert receipt["qc_report"]["sha256"] == sha256_file(project / QC_REPORT)
    assert receipt["reference"]["sha256"] == sha256_file(project / REFERENCE)
    assert receipt["card"]["caption"] == CARD["caption"]
    assert receipt["card"]["destination"] == CARD["destination"]
    assert receipt["outward_action_performed"] is False
    assert verify_payload(receipt, purpose="studio-delivery-approval-v1")["status"] == "PASS"


def test_the_approval_advances_only_the_delivery_approved_stage(tmp_path: Path) -> None:
    app, _, project = _approved(tmp_path)

    request(app, "POST", "/api/publish/reel-demo-v1/approve", json_body=CARD)
    state = ProjectState.load(project / "state.json")

    assert state.stage("DELIVERY_APPROVED")["status"] == "PASS"
    assert state.verify_stage("DELIVERY_APPROVED")["status"] == "PASS"
    assert state.stage("DELIVERED")["status"] == "PENDING"
    assert state.next_stage() == "DELIVERED"


def test_an_approval_is_refused_while_the_candidate_is_not_human_approved(tmp_path: Path) -> None:
    app, _, project = review_app(tmp_path)

    response = request(app, "POST", "/api/publish/reel-demo-v1/approve", json_body=CARD)

    assert response.status_code == 400
    assert "HUMAN_APPROVED" in response.json()["error"]
    assert not (project / "deliver" / "delivery-approval-001.json").exists()


def test_an_approval_missing_a_contract_field_is_refused_by_name(tmp_path: Path) -> None:
    app, _, _ = _approved(tmp_path)
    partial = {key: value for key, value in CARD.items() if key != "schedule"}

    response = request(app, "POST", "/api/publish/reel-demo-v1/approve", json_body=partial)

    assert response.status_code == 422
    assert "schedule" in response.text


# --- metric capture scheduling -------------------------------------------


def test_a_publication_receipt_schedules_exactly_two_captures_at_the_right_times(tmp_path: Path) -> None:
    app, config, project = _approved(tmp_path)
    request(app, "POST", "/api/publish/reel-demo-v1/approve", json_body=CARD)
    _publication_receipt(project)

    result = publish_module.schedule_metric_captures(config, "reel-demo-v1")
    rows = _schedules(config)

    assert len(result["scheduled"]) == 2
    assert len(rows) == 2
    assert [row["kind"] for row in rows] == ["metric_capture", "metric_capture"]
    assert [row["due_at_utc"] for row in rows] == [DUE_24H, DUE_72H]
    assert [row["payload"]["window"] for row in rows] == ["24h", "72h"]
    assert rows[0]["payload"]["published_at"] == PUBLISHED_AT
    assert rows[0]["payload"]["experiment_id"] == "experiment_002"


def test_scheduling_twice_does_not_duplicate_the_captures(tmp_path: Path) -> None:
    app, config, project = _approved(tmp_path)
    request(app, "POST", "/api/publish/reel-demo-v1/approve", json_body=CARD)
    _publication_receipt(project)

    publish_module.schedule_metric_captures(config, "reel-demo-v1")
    again = publish_module.schedule_metric_captures(config, "reel-demo-v1")

    assert again["scheduled"] == []
    assert len(_schedules(config)) == 2
    assert "already scheduled" in again["reason"]


def test_an_approval_with_no_publication_receipt_yet_schedules_nothing_and_says_why(tmp_path: Path) -> None:
    """Publishing is manual in v1, so approval precedes publication and cannot know its time."""
    app, config, _ = _approved(tmp_path)

    body = request(app, "POST", "/api/publish/reel-demo-v1/approve", json_body=CARD).json()

    assert body["captures_scheduled"] == 0
    assert "no publication receipt" in body["capture_reason"]
    assert _schedules(config) == []


def test_a_publication_receipt_without_a_published_at_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    app, config, project = _approved(tmp_path)
    request(app, "POST", "/api/publish/reel-demo-v1/approve", json_body=CARD)
    _publication_receipt(project, {key: value for key, value in PUBLICATION_RECEIPT.items() if key != "published_at"})

    result = publish_module.schedule_metric_captures(config, "reel-demo-v1")

    assert result["scheduled"] == []
    assert "published_at" in result["reason"]
    assert _schedules(config) == []


def test_the_capture_windows_come_from_the_publication_time_not_the_approval_time(tmp_path: Path) -> None:
    app, config, project = _approved(tmp_path)
    request(app, "POST", "/api/publish/reel-demo-v1/approve", json_body=CARD)
    _publication_receipt(project)

    publish_module.schedule_metric_captures(config, "reel-demo-v1")
    rows = _schedules(config)
    published = datetime.fromisoformat(PUBLISHED_AT).astimezone(timezone.utc)

    for row, hours in zip(rows, (24, 72)):
        due = datetime.fromisoformat(row["due_at_utc"].replace("Z", "+00:00"))
        assert due - published == timedelta(hours=hours)


# --- the ledger -----------------------------------------------------------


def _seed_ledger(config: StudioConfig) -> Path:
    path = Path(config.projects_root) / "learning" / "LEDGER.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "trial-learning-ledger-v1",
                "updated_at_utc": "2025-01-13T11:00:00Z",
                "doctrine_rule": "No style-baseline change on fewer than 3 comparable trials.",
                "trials": [
                    {
                        "experiment_id": "experiment_001",
                        "reel": "example — <caption B> v1",
                        "snapshots": {"24h": {"views": 90}, "72h": {"views": 120}},
                        "read": "Front-loaded: most 72h views arrived in the first 24h.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_a_capture_appends_to_the_ledger_without_rewriting_prior_entries(tmp_path: Path) -> None:
    app, config, project = _approved(tmp_path)
    ledger = _seed_ledger(config)
    before = json.loads(ledger.read_text(encoding="utf-8"))
    _publication_receipt(project)

    publish_module.append_ledger_snapshot(config, "reel-demo-v1", window="24h", metrics=METRICS_24H)
    after = json.loads(ledger.read_text(encoding="utf-8"))

    assert after["trials"][0] == before["trials"][0], "the earlier trial must be byte-identical"
    assert after["schema"] == before["schema"]
    assert after["doctrine_rule"] == before["doctrine_rule"]
    new_trial = next(trial for trial in after["trials"] if trial["experiment_id"] == "experiment_002")
    assert new_trial["snapshots"]["24h"]["views"] == 100
    assert new_trial["snapshots"]["24h"]["captured_at_utc"]


def test_a_second_window_leaves_the_first_snapshot_untouched(tmp_path: Path) -> None:
    app, config, project = _approved(tmp_path)
    _seed_ledger(config)
    _publication_receipt(project)
    ledger = Path(config.projects_root) / "learning" / "LEDGER.json"

    publish_module.append_ledger_snapshot(config, "reel-demo-v1", window="24h", metrics=METRICS_24H)
    first = json.loads(ledger.read_text(encoding="utf-8"))["trials"][-1]["snapshots"]["24h"]
    publish_module.append_ledger_snapshot(config, "reel-demo-v1", window="72h", metrics={**METRICS_24H, "views": 150})
    after = json.loads(ledger.read_text(encoding="utf-8"))["trials"][-1]["snapshots"]

    assert after["24h"] == first
    assert after["72h"]["views"] == 150


def test_recapturing_a_window_is_refused_rather_than_silently_overwriting(tmp_path: Path) -> None:
    app, config, project = _approved(tmp_path)
    _seed_ledger(config)
    _publication_receipt(project)

    publish_module.append_ledger_snapshot(config, "reel-demo-v1", window="24h", metrics=METRICS_24H)
    with pytest.raises(Exception) as caught:
        publish_module.append_ledger_snapshot(config, "reel-demo-v1", window="24h", metrics=METRICS_24H)

    assert "24h" in str(caught.value)
    assert "already" in str(caught.value)


def test_a_capture_names_the_metrics_it_did_not_receive(tmp_path: Path) -> None:
    """The minimum list is doctrine; a missing metric is withheld by name, never zero."""
    app, config, project = _approved(tmp_path)
    _seed_ledger(config)
    _publication_receipt(project)

    result = publish_module.append_ledger_snapshot(config, "reel-demo-v1", window="24h", metrics=METRICS_24H)

    assert "retention_percent" in result["withheld_metrics"]
    snapshot = json.loads((Path(config.projects_root) / "learning" / "LEDGER.json").read_text())["trials"][-1]["snapshots"]["24h"]
    assert snapshot["retention_percent"] is None


def test_a_ledger_that_does_not_exist_yet_is_created_with_its_schema(tmp_path: Path) -> None:
    app, config, project = _approved(tmp_path)
    _publication_receipt(project)

    publish_module.append_ledger_snapshot(config, "reel-demo-v1", window="24h", metrics=METRICS_24H)
    ledger = json.loads((Path(config.projects_root) / "learning" / "LEDGER.json").read_text(encoding="utf-8"))

    assert ledger["schema"] == "trial-learning-ledger-v1"
    assert len(ledger["trials"]) == 1


# --- nothing outward ------------------------------------------------------


def test_control_plane_still_has_no_publish_route(tmp_path: Path) -> None:
    """Task 10 adds an approval recorder; it must not add a publisher."""
    app, _, _ = _approved(tmp_path)

    assert request(app, "POST", "/api/projects/reel-demo-v1/publish", json_body={}).status_code in {404, 405}
    assert request(app, "POST", "/api/publish/reel-demo-v1/publish", json_body={}).status_code in {404, 405}
    assert "publish" not in request(app, "GET", "/api/projects/reel-demo-v1/workbench").json()["actions"]


class _OutwardCallAttempted(AssertionError):
    pass


def _arm_network_tripwire(monkeypatch: Any) -> None:
    """Make every path into the network raise, at the lowest layer each one reaches."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise _OutwardCallAttempted("a route reached for the network: {} {}".format(args, kwargs))

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def _exercise(app: Any, path: str, method: str) -> None:
    if path.startswith("/api/stream"):
        path = "/api/stream?after=0&max_seconds=0.05&poll_ms=10"
    request(app, method, path, json_body={} if method == "POST" else None)


def test_no_route_in_the_app_performs_an_outward_network_call(tmp_path: Path, monkeypatch: Any) -> None:
    app, _, _ = _approved(tmp_path)
    paths: List[Tuple[str, str]] = []
    for route in app.routes:
        for method in sorted(getattr(route, "methods", set()) or set()):
            if method not in {"GET", "POST"}:
                continue
            paths.append((route.path.replace("{project_id}", "reel-demo-v1").replace("{call_id}", "CALL-404"), method))
    assert len(paths) >= 15, "the sweep must actually cover the app's routes"

    _arm_network_tripwire(monkeypatch)
    for path, method in paths:
        _exercise(app, path, method)


def test_the_reference_analyze_action_refuses_a_url_source_because_the_download_is_outward(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The one route that could reach the network, closed and named.

    `reelctl reference analyze` downloads a URL reference with yt-dlp. Reached through the
    allowlisted `reference_analyze` action, that made the control plane able to perform an
    outward call, which §5.2 forbids. Local files still work; a URL is the daemon's job.
    """
    app, _, project = _approved(tmp_path)
    config_path = project / "project.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["reference_input"] = "https://video.example/reel/ref-19/"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    _arm_network_tripwire(monkeypatch)
    response = request(app, "POST", "/api/projects/reel-demo-v1/actions", json_body={"action": "reference_analyze"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert "https://video.example/reel/ref-19/" in error
    assert "outward" in error


def test_a_local_reference_source_is_still_accepted_by_that_action(tmp_path: Path) -> None:
    app, _, project = review_app(tmp_path)

    response = request(
        app,
        "POST",
        "/api/projects/reel-demo-v1/actions",
        json_body={"action": "reference_analyze", "payload": {"source": str(project / REFERENCE)}},
    )

    # The action is dispatched rather than refused at the URL gate; whatever the engine then
    # says about these fixture bytes is the engine's business, not the gate's.
    assert "outward" not in response.text


# --- the screen -----------------------------------------------------------


def test_the_publish_screen_shows_the_card_and_one_button(tmp_path: Path) -> None:
    app, _, _ = _approved(tmp_path)

    page = request(app, "GET", "/publish/reel-demo-v1").text

    assert "REEL" in page
    assert "Record approval" in page
    assert "no outward call" in page
    assert page.count("<form") == 1


def test_the_publish_screen_says_what_is_still_unresolved(tmp_path: Path) -> None:
    app, _, _ = _approved(tmp_path)

    page = request(app, "GET", "/publish/reel-demo-v1").text

    assert "Unresolved" in page
    assert "no machine-readable destination" in page


def test_the_publish_screen_refuses_a_reel_the_operator_has_not_approved(tmp_path: Path) -> None:
    app, _, _ = review_app(tmp_path)

    page = request(app, "GET", "/publish/reel-demo-v1").text

    assert "HUMAN_APPROVED" in page
    assert "Record approval" not in page
