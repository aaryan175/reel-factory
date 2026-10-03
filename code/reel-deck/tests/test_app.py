"""Smoke tests: auth gate, roles, login flow, receipts writes, media
confinement, and the ETA model."""

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from server import auth, receipts

    monkeypatch.setattr(auth, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(receipts, "BUS_DIR", tmp_path / "bus")
    monkeypatch.setattr(receipts, "UPLOADS_DIR", tmp_path / "uploads")
    auth.set_credentials("op", "test-pass-123")
    auth.set_credentials("ed", "editor-pass-123", auth.ROLE_EDITOR)

    from server import main
    importlib.reload  # keep module identity; main uses auth/receipts at call time
    return TestClient(main.app), tmp_path


def login(test_client, username="op", password="test-pass-123"):
    response = test_client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303
    return response.cookies


def test_everything_requires_login(client):
    test_client, _ = client
    assert test_client.get("/", follow_redirects=False).status_code == 303
    assert test_client.get("/api/board").status_code == 401
    assert test_client.get("/media", params={"p": "/etc/hosts"}).status_code == 401
    assert test_client.get("/health").status_code == 200


def test_wrong_password_rejected(client):
    test_client, _ = client
    response = test_client.post(
        "/login", data={"username": "op", "password": "nope"}, follow_redirects=False
    )
    assert response.status_code == 401


def test_login_and_board(client):
    test_client, _ = client
    cookies = login(test_client)
    response = test_client.get("/", cookies=cookies)
    assert response.status_code == 200
    assert "REEL DECK" in response.text
    api = test_client.get("/api/board", cookies=cookies)
    assert api.status_code == 200
    assert "cards" in api.json()


def test_media_confinement(client):
    test_client, _ = client
    cookies = login(test_client)
    for bad in ("/etc/hosts", "~/.ssh/config",
                "~/reel-production/REEL_REGISTRY.json"):
        response = test_client.get("/media", params={"p": bad}, cookies=cookies)
        assert response.status_code == 403, bad


def test_receipt_writes(client):
    test_client, tmp = client
    cookies = login(test_client)

    response = test_client.post("/api/verdict", cookies=cookies, json={
        "row": 3, "disposition": "APPROVE", "text": "looks good",
        "watched": {"name": "x.mp4", "sha256": "ab" * 32, "version_label": "v002"},
    })
    assert response.status_code == 200
    receipt = tmp / "bus" / response.json()["receipt"]
    text = receipt.read_text()
    assert "APPROVE" in text and "looks good" in text and "ab" * 32 in text

    response = test_client.post("/api/batch-verdict", cookies=cookies, json={
        "row": 22, "items": [{"variant": 1, "name": "v01.mp4", "verdict": "KEEP"},
                              {"variant": 2, "name": "v02.mp4", "verdict": "KILL", "note": "bad hook"}],
    })
    assert response.status_code == 200
    text = (tmp / "bus" / response.json()["receipt"]).read_text()
    assert "variant 01: KEEP" in text and "variant 02: KILL" in text and "bad hook" in text

    response = test_client.post("/api/drop", cookies=cookies, json={
        "urls": ["https://www.instagram.com/reel/EXAMPLE01/"], "note": "recreate 1:1",
    })
    assert response.status_code == 200
    text = (tmp / "bus" / response.json()["receipt"]).read_text()
    assert "instagram.com/reel/EXAMPLE01" in text

    response = test_client.post("/api/drop", cookies=cookies, json={"urls": []})
    assert response.status_code == 422


def test_bad_disposition_rejected(client):
    test_client, _ = client
    cookies = login(test_client)
    response = test_client.post("/api/verdict", cookies=cookies,
                                json={"row": 1, "disposition": "MAYBE", "text": ""})
    assert response.status_code == 422


# ----------------------------------------------------------------- multi-user


def test_both_users_can_sign_in_and_see_their_role(client):
    test_client, _ = client
    for username, password, role in (("op", "test-pass-123", "OPERATOR"),
                                     ("ed", "editor-pass-123", "EDITOR")):
        cookies = login(test_client, username, password)
        page = test_client.get("/", cookies=cookies)
        assert page.status_code == 200
        assert username in page.text and role in page.text


def test_users_cannot_borrow_each_others_passwords(client):
    test_client, _ = client
    response = test_client.post(
        "/login", data={"username": "ed", "password": "test-pass-123"}, follow_redirects=False
    )
    assert response.status_code == 401


def test_operator_receipt_is_attributed_and_authoritative(client):
    test_client, tmp = client
    cookies = login(test_client)
    response = test_client.post("/api/verdict", cookies=cookies, json={
        "row": 5, "disposition": "APPROVE", "text": "ship it",
    })
    text = (tmp / "bus" / response.json()["receipt"]).read_text()
    assert "Sent by: op (OPERATOR)" in text
    assert "AUTHORITY: EDITOR" not in text
    assert "same authority" in text


def test_editor_verdicts_carry_the_authority_stamp(client):
    test_client, tmp = client
    cookies = login(test_client, "ed", "editor-pass-123")

    response = test_client.post("/api/verdict", cookies=cookies, json={
        "row": 5, "disposition": "REJECT", "text": "hook is soft",
    })
    text = (tmp / "bus" / response.json()["receipt"]).read_text()
    assert "Sent by: ed (EDITOR)" in text
    assert "AUTHORITY: EDITOR — advisory only, awaiting operator confirmation." in text
    assert "do NOT treat as an operator verdict" in text
    # The stamp must not sit above a footer claiming operator authority.
    assert "same authority" not in text

    response = test_client.post("/api/batch-verdict", cookies=cookies, json={
        "row": 8, "items": [{"variant": 1, "verdict": "KILL"}],
    })
    text = (tmp / "bus" / response.json()["receipt"]).read_text()
    assert "Sent by: ed (EDITOR)" in text
    assert "AUTHORITY: EDITOR" in text


def test_editor_drops_are_attributed_but_not_downgraded(client):
    test_client, tmp = client
    cookies = login(test_client, "ed", "editor-pass-123")
    response = test_client.post("/api/drop", cookies=cookies, json={
        "urls": ["https://www.instagram.com/reel/EXAMPLE01/"], "note": "reference",
    })
    text = (tmp / "bus" / response.json()["receipt"]).read_text()
    assert "Sent by: ed (EDITOR)" in text
    assert "AUTHORITY: EDITOR" not in text
    assert "same authority" in text


def test_legacy_single_user_config_migrates_to_operator(tmp_path, monkeypatch):
    from server import auth

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(auth, "CONFIG_PATH", config_path)
    auth.set_credentials("legacy", "legacy-pass-123")

    # Rewrite it in the pre-roles shape, exactly as the old app left it.
    saved = json.loads(config_path.read_text())
    only = saved["users"][0]
    config_path.write_text(json.dumps({
        "username": only["username"], "salt": only["salt"],
        "hash": only["hash"], "secret": saved["secret"],
    }))

    assert auth.credentials_set()
    user, message = auth.check_login("legacy", "legacy-pass-123", "test")
    assert user == {"username": "legacy", "role": "OPERATOR"}, message

    # The migration is persisted, not just applied in memory.
    on_disk = json.loads(config_path.read_text())
    assert on_disk["users"] == [{
        "username": "legacy", "salt": only["salt"], "hash": only["hash"], "role": "OPERATOR",
    }]
    assert "username" not in on_disk and "hash" not in on_disk


def test_set_credentials_upserts_without_dropping_others(tmp_path, monkeypatch):
    from server import auth

    monkeypatch.setattr(auth, "CONFIG_PATH", tmp_path / "config.json")
    auth.set_credentials("one", "pass-one-123")
    auth.set_credentials("two", "pass-two-123", auth.ROLE_EDITOR)
    auth.set_credentials("one", "pass-one-rotated", auth.ROLE_EDITOR)

    assert auth.list_users() == [{"username": "one", "role": "EDITOR"},
                                 {"username": "two", "role": "EDITOR"}]
    assert auth.check_login("one", "pass-one-123", "test")[0] is None
    assert auth.check_login("one", "pass-one-rotated", "test")[0] is not None
    assert auth.check_login("two", "pass-two-123", "test")[0] is not None


def test_session_cookie_carries_user_and_dies_with_the_account(tmp_path, monkeypatch):
    from server import auth

    monkeypatch.setattr(auth, "CONFIG_PATH", tmp_path / "config.json")
    auth.set_credentials("ghost", "ghost-pass-123", auth.ROLE_EDITOR)
    cookie = auth.issue_session({"username": "ghost", "role": "EDITOR"})
    assert auth.session_user(cookie) == {"username": "ghost", "role": "EDITOR"}

    # Role change in config lands without a re-login.
    auth.set_credentials("ghost", "ghost-pass-123", auth.ROLE_OPERATOR)
    assert auth.session_user(cookie) == {"username": "ghost", "role": "OPERATOR"}

    # Removing the account invalidates the still-signed cookie.
    config = json.loads((tmp_path / "config.json").read_text())
    config["users"] = []
    (tmp_path / "config.json").write_text(json.dumps(config))
    assert auth.session_user(cookie) is None
    assert auth.session_user("not-a-real-cookie") is None
    assert auth.session_user(None) is None


# ------------------------------------------------------------------------ eta


def _bus_fixture(directory: Path) -> None:
    """A synthetic bus: one full batch run per history row, on a clean clock.

    Phase offsets in hours from the run start: build +2, audit +1, then three
    fix rounds each followed by an audit, then delivery.
    """
    directory.mkdir(parents=True, exist_ok=True)
    start = time.time() - 40 * 24 * 3600
    for index, row in enumerate((1, 2, 3, 4, 6, 9, 11, 16, 18, 22, 33)):
        base = start + index * 5 * 24 * 3600
        timeline = [
            ("feasibility", 0.0),
            ("build", 2.0),
            ("INDEP-AUDIT", 3.0),
            ("fix1", 6.0),
            ("fix1-INDEP-AUDIT", 7.0),
            ("fix2", 10.0),
            ("fix2-INDEP-AUDIT", 11.0),
            ("fix3", 14.0),
            ("fix3-INDEP-AUDIT", 15.0),
            ("delivery", 15.5),
        ]
        for tag, offset in timeline:
            path = directory / f"reel{row:02d}-batch01-{tag}-20250102.md"
            path.write_text(f"# {tag}\n", encoding="utf-8")
            when = base + offset * 3600
            os.utime(path, (when, when))


def test_eta_model_learns_phase_durations_from_a_synthetic_bus(tmp_path, monkeypatch):
    from server import eta

    bus = tmp_path / "bus"
    _bus_fixture(bus)
    monkeypatch.setattr(eta, "BUS_DIR", bus)

    model = eta.build_model()
    assert model["rows_seen"] == [1, 2, 3, 4, 6, 9, 11, 16, 18, 22, 33]
    assert model["events"] == 11 * 10

    phases = model["phases"]
    assert phases["build"]["source"] == "history"
    assert phases["build"]["median_h"] == pytest.approx(2.0, abs=0.01)
    assert phases["audit"]["median_h"] == pytest.approx(1.0, abs=0.01)
    assert phases["fix_round"]["median_h"] == pytest.approx(3.0, abs=0.01)
    assert phases["cardfix_deliver"]["median_h"] == pytest.approx(0.5, abs=0.01)
    # Feasibility opens a run, so it never gets a measured gap charged to it.
    assert phases["feasibility"]["source"] == "prior"
    assert model["fix_rounds"]["median"] == 3.0
    assert model["total_hours"]["median_h"] == pytest.approx(15.5, abs=0.05)


def test_eta_estimate_counts_only_the_phases_still_to_come(tmp_path, monkeypatch):
    from server import eta

    bus = tmp_path / "bus"
    _bus_fixture(bus)
    monkeypatch.setattr(eta, "BUS_DIR", bus)
    model = eta.build_model()

    now = time.time()
    # A row that just passed its second fix audit: one fix + audit + delivery
    # left = 3.0 + 1.0 + 0.5 hours.
    events = [
        {"at": now - 7200, "phase": "build", "source": "x", "fix_round": None},
        {"at": now - 3600, "phase": "fix_round", "source": "x", "fix_round": 1},
        {"at": now - 1800, "phase": "fix_round", "source": "x", "fix_round": 2},
        {"at": now, "phase": "audit", "source": "reel17-fix2-INDEP-AUDIT.md", "fix_round": 2},
    ]
    estimate = eta.estimate(17, events=events, now=now, fitted=model)
    assert estimate["phase"] == "audit"
    assert estimate["rounds_done"] == 2
    assert estimate["remaining_phases"] == ["fix_round", "audit", "cardfix_deliver"]
    assert estimate["remaining_hours"] == pytest.approx(4.5, abs=0.05)
    assert estimate["line"].startswith("review ready ~")
    assert estimate["eta_local_label"].endswith(("am", "pm"))
    assert estimate["spread_hours"] > 0


def test_eta_says_nothing_when_it_cannot_read_the_phase(tmp_path, monkeypatch):
    from server import eta

    bus = tmp_path / "bus"
    _bus_fixture(bus)
    monkeypatch.setattr(eta, "BUS_DIR", bus)
    model = eta.build_model()
    now = time.time()

    assert eta.estimate(99, events=[], now=now, fitted=model) is None
    delivered = [{"at": now, "phase": "cardfix_deliver", "source": "d.md", "fix_round": None}]
    assert eta.estimate(22, events=delivered, now=now, fitted=model) is None


def test_eta_phase_reading_is_specific_before_generic():
    from server import eta

    assert eta.phase_of("reel22-batch01-fix1-INDEP-AUDIT-20250106.md") == "audit"
    assert eta.phase_of("reel22-batch01-fix4-delivery-20250107.md") == "cardfix_deliver"
    assert eta.phase_of("reel22-batch01-fix3-20250107.md") == "fix_round"
    assert eta.phase_of("reel17-batch01-feasibility-20250107.md") == "feasibility"
    assert eta.phase_of("reel06-batch01-cast-20250101.md") == "cast"
    assert eta.phase_of("reel09-batch01-build-20250102.md") == "build"
    assert eta.phase_of("trial_batch01_fix2_audit_20250107") == "audit"
    assert eta.phase_of("reel01-batch01-feasibility-20250101.SUPERSEDED-CALL-x.md") is None
    assert eta.phase_of("ui-drop-20250107T010101Z.md") is None

    assert eta.sequence_of("reel02-batch01-build-20250101.md") == 2
    assert eta.sequence_of("reel22-batch01-fix1-20250106.md") == 22
    assert eta.sequence_of("reel9-ref-19-postready-20250101.md") == 9
    assert eta.sequence_of("audit-reel14v003-reel17v003-v04v3-20250103.md") is None
    assert eta.fix_round_of("reel02-batch01-fix7-20250103.md") == 7


def test_eta_merge_folds_the_registry_key_into_its_bus_receipt():
    from server import eta

    now = time.time()
    bus = [{"at": now, "phase": "audit", "source": "reel17-fix2-INDEP-AUDIT.md", "fix_round": 2}]
    registry = [{"at": now + 600, "phase": "audit",
                 "source": "registry:trial_batch01_fix2_audit_20250107", "fix_round": 2}]
    assert len(eta.merge_events(bus, registry)) == 1

    later = [{"at": now + 6 * 3600, "phase": "audit", "source": "r3.md", "fix_round": 3}]
    assert len(eta.merge_events(bus, later)) == 2


def test_board_attaches_eta_only_to_machine_cards(client):
    test_client, _ = client
    cookies = login(test_client)
    payload = test_client.get("/api/board", cookies=cookies).json()
    for card in payload["cards"] + payload["archive"]:
        if card["bucket"] != "machine":
            assert card["eta"] is None, card["seq"]
        if card["eta"]:
            assert card["eta"]["line"].startswith("review ready ~")
            assert card["eta"]["phase"] in ("feasibility", "cast", "build", "audit", "fix_round", "intake", "study")
    if payload["next_review"]:
        assert payload["next_review"]["eta_epoch"] == min(
            c["eta"]["eta_epoch"] for c in payload["cards"] if c["eta"]
        )


# ------------------------------------------------------------- lane truth


def test_lane_head_segment_decides_before_prose():
    """`AWAITING_OPERATOR_LOOK_APPROVAL__...__no render until the look is APPROVED` must not
    file DONE/Approved because the prose in segment 3 contains the word APPROVED. The head
    segment is the state; prose never outranks it, and words match whole (APPROVAL is not
    APPROVED)."""
    from server import registry as reg

    def lane(state):
        return reg.row_lane({"review_state": state})

    assert lane("AWAITING_OPERATOR_LOOK_APPROVAL__LOOK_SHEET_V4_IN_DRIVE__no render until the look is approved__old") == reg.LANE_VERDICT
    assert lane("APPROVED_BY_OPERATOR__gold, ship it__LOCAL_REVIEW_READY") == reg.LANE_DONE
    assert lane("LOCAL_REVIEW_READY__PENDING_HUMAN__three paragraphs mention APPROVED") == reg.LANE_VERDICT
    assert lane("PUBLISHED__2025-01-06__posted via scheduler") == reg.LANE_DONE
    assert lane("BUILDING__fix2 running") == reg.LANE_MACHINE
    assert reg.row_is_cold({"review_state": "PARKED_KILLED_BY_OPERATOR__not it__was AWAITING"})
    assert not reg.row_is_cold({"review_state": "AWAITING_OPERATOR__the last one was REJECTED__x"})


def test_plain_phrase_never_reads_prose_as_state():
    from server import registry as reg
    ps = lambda s: reg.plain_state(reg.state_basis({"review_state": s}), reg.row_lane({"review_state": s}))["phrase"]
    assert ps("AWAITING_OPERATOR_LOOK_APPROVAL__LOOK_SHEET_V4_IN_DRIVE__no render until the look is approved__old") == "Ready for you to watch"
    assert ps("APPROVED_BY_OPERATOR__gold__LOCAL_REVIEW_READY") == "Approved"
    assert ps("APPROVED_HELD_FOR_OPERATOR__v3") == "Approved — held for you"
    assert ps("PARKED_KILLED_BY_OPERATOR__was AWAITING") == "Parked"
    assert ps("LOCAL_REVIEW_READY__V005_RECAST_DELIVERED__PENDING_HUMAN") == "New version ready"


# ------------------------------------------------------------ sends + test mode


def test_test_mode_sends_file_as_smoketest_and_list_with_state(client):
    from server import receipts
    test_client, tmp = client
    cookies = login(test_client, "ed", "editor-pass-123")
    r = test_client.post("/api/verdict", cookies=cookies, json={
        "row": 11, "disposition": "NOTES", "text": "[WALK TEST] plumbing", "test": True})
    name = r.json()["receipt"]
    assert name.startswith("ui-smoketest-row11-feedback-"), name
    text = (tmp / "bus" / name).read_text()
    assert "TEST — Reel Deck plumbing walk" in text and "Disposition: NOTES" in text
    # listed under the row as waiting (no ACK yet)
    sends = receipts.row_sends(11)
    assert sends and sends[0]["name"] == name and sends[0]["state"] == "waiting" and sends[0]["test"]
    assert sends[0]["sent_by"] == "ed" and sends[0]["text"].startswith("[WALK TEST]")
    # the factory ACKs → received
    (tmp / "bus" / name).open("a").write("\nACK — factory-daemon — 2025-01-18T00:00:00Z — smoketest\n")
    assert receipts.row_sends(11)[0]["state"] == "received"
    # old and silent → no_answer
    old = test_client.post("/api/verdict", cookies=cookies, json={"row": 11, "disposition": "NOTES", "text": "x", "test": True}).json()["receipt"]
    # "sent" is the stamp in the file NAME (mtime moves whenever the factory appends), so age it
    # by evaluating 15 minutes in the future rather than by touching the file.
    import time
    states = {s["name"]: s["state"] for s in receipts.row_sends(11, now=time.time() + 900)}
    assert states[old] == "no_answer"
    # the api endpoint the row polls
    api = test_client.get("/api/row/11/sends", cookies=cookies)
    assert api.status_code == 200 and "factory" in api.json() and len(api.json()["sends"]) == 2


def test_real_ruling_hides_the_call_but_a_test_ruling_does_not(client):
    from server import receipts
    test_client, _ = client
    cookies = login(test_client)
    test_client.post("/api/verdict", cookies=cookies, json={
        "row": 13, "disposition": "NOTES", "text": "RULING on CALL-ROW13-NEXT-STEP-20250115: go with A", "test": True})
    assert receipts.answered_calls(13) == {}
    test_client.post("/api/verdict", cookies=cookies, json={
        "row": 13, "disposition": "NOTES", "text": "RULING on CALL-ROW13-NEXT-STEP-20250115: go with A"})
    assert list(receipts.answered_calls(13)) == ["CALL-ROW13-NEXT-STEP-20250115"]


def test_expired_session_says_so_everywhere(client):
    test_client, _ = client
    bad = {"deck_session": "not-a-real-cookie"}
    r = test_client.post("/api/verdict", cookies=bad, json={"row": 1, "disposition": "NOTES", "text": "x"})
    assert r.status_code == 401 and "expired" in r.json()["error"] and r.json()["login"] == "/login?expired=1"
    r = test_client.get("/row/11", cookies=bad, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?expired=1"
    page = test_client.get("/login?expired=1")
    assert "session expired" in page.text


def test_health_reports_build_factory_and_registry(client):
    test_client, _ = client
    body = test_client.get("/health").json()
    assert len(body["build"]) == 12 and "heartbeat_age_s" in body["factory"] and "writable" in body["registry"]


def test_editor_does_not_see_factory_controls(client):
    test_client, _ = client
    ed = login(test_client, "ed", "editor-pass-123")
    page = test_client.get("/", cookies=ed).text
    assert "btn-continue" not in page and 'href="/make"' not in page and 'href="/help"' in page
    op = login(test_client)
    page = test_client.get("/", cookies=op).text
    assert "btn-continue" in page and 'href="/make"' in page


def test_same_second_sends_never_overwrite_each_other(client):
    test_client, tmp = client
    cookies = login(test_client)
    names = {test_client.post("/api/verdict", cookies=cookies, json={"row": 9, "disposition": "NOTES", "text": f"n{i}"}).json()["receipt"]
             for i in range(3)}
    assert len(names) == 3
    assert all((tmp / "bus" / n).exists() for n in names)


def test_log_rotation_copies_then_truncates(tmp_path, monkeypatch):
    from server import main
    monkeypatch.setattr(main, "LOG_DIR", tmp_path)
    monkeypatch.setattr(main, "LOG_ROTATE_BYTES", 10)
    (tmp_path / "deck.log").write_bytes(b"x" * 50)
    assert main.rotate_logs() == ["deck.log"]
    assert (tmp_path / "deck.log").stat().st_size == 0 and (tmp_path / "deck.log.1").stat().st_size == 50


def test_deck_acks_smoketests_but_never_real_orders(client):
    from server import main, receipts
    test_client, tmp = client
    cookies = login(test_client)
    smoke = test_client.post("/api/verdict", cookies=cookies, json={"row": 11, "disposition": "NOTES", "text": "t", "test": True}).json()["receipt"]
    real = test_client.post("/api/verdict", cookies=cookies, json={"row": 11, "disposition": "NOTES", "text": "r"}).json()["receipt"]
    assert main.ack_smoketests() == [smoke]
    assert receipts.receipt_status(str(tmp / "bus" / smoke))["acks"][0]["lane"] == "deck-bus"
    assert receipts.receipt_status(str(tmp / "bus" / real))["acks"] == []
    assert main.ack_smoketests() == []  # idempotent
    states = {s["name"]: s["state"] for s in receipts.row_sends(11)}
    assert states[smoke] == "received" and states[real] == "waiting"


def test_process_page_and_api(client):
    test_client, _ = client
    assert test_client.get("/row/11/process", follow_redirects=False).status_code in (401, 303)
    cookies = login(test_client)
    response = test_client.get("/row/11/process", cookies=cookies)
    assert response.status_code in (200, 404)  # 404 only if the registry has no row 11 on this machine
    if response.status_code == 200:
        assert "PROCESS" in response.text and "THE PIPELINE" in response.text
        api = test_client.get("/api/row/11/process", cookies=cookies)
        assert api.status_code == 200
        body = api.json()
        assert body["seq"] == 11 and "renders" in body and "casts" in body and "timeline" in body
    assert test_client.get("/row/9999/process", cookies=cookies).status_code == 404


def test_factory_running_receipt_moves_row_into_the_machine(client, tmp_path, monkeypatch):
    """The board must show the truth on its own: when the factory heartbeat says it is
    running on a row's receipt, that row is IN THE MACHINE, its open CALL is hidden, and
    the reviewer's send reads 'working' — not 'waiting'."""
    import json, time
    from server import board, receipts
    status = tmp_path / "factory_status.json"
    bus = tmp_path / "bus"; bus.mkdir(exist_ok=True)
    name = "ui-frame-note-row13-20250119T065029Z.md"
    (bus / name).write_text("# ui-frame-note — row 13\n\nSent by: admin (OPERATOR)\n\nFrame: f50 @ 0:02.084\n\nOperator note (verbatim):\n\nfix it\n---\n")
    status.write_text(json.dumps({"alive_at": time.time(), "state": "running", "receipt": name}))
    monkeypatch.setattr(board, "FACTORY_STATUS", status)
    monkeypatch.setattr(board, "BUS_DIR", bus)
    monkeypatch.setattr(receipts, "FACTORY_STATUS_PATH", status)
    monkeypatch.setattr(receipts, "BUS_DIR", bus)
    monkeypatch.setattr(board, "worker_alive", lambda: True)   # the process gate is tested by hand
    w = board.factory_working_row()
    assert w and w["seq"] == 13 and w["receipt"] == name
    sends = receipts.row_sends(13)
    assert sends and sends[0]["state"] == "working"
    test_client, _ = client
    cookies = login(test_client)
    data = test_client.get("/api/board", cookies=cookies).json()
    card = next((c for c in data["cards"] if c["seq"] == 13), None)
    if card:  # only when this machine's registry has row 13
        assert card["bucket"] == "machine" and "FIXING" in card["phrase"] and card["calls"] == []


def test_any_lane_pickup_puts_the_row_in_the_machine(tmp_path, monkeypatch):
    """A lane other than the factory worker picking up a note while the factory sleeps must still
    move the row into the machine (not READY FOR YOU TO WATCH / 0 in the machine)."""
    import time as _t
    from server import receipts
    monkeypatch.setattr(receipts, "BUS_DIR", tmp_path)
    now = _t.time()
    stamp = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now - 120))
    body = ("# ui-frame-note — row 11\n\nSent by: admin (OPERATOR)\n\nOperator note (verbatim):\n\nfix the grade\n---\n"
            f"Factory: ...\n\nACK — main-session — {stamp} — picked up: working it now\n")
    (tmp_path / "ui-frame-note-row11-20250120T052454Z.md").write_text(body)
    pick = receipts.picked_up(11, now=now)
    assert pick and pick["lane"] == "main-session" and 100 < pick["since_s"] < 200
    assert receipts.row_sends(11, now=now)[0]["state"] == "working"
    done = body + f"ACK — main-session — {stamp} — finished: v012 delivered\n"
    (tmp_path / "ui-frame-note-row11-20250120T052454Z.md").write_text(done)
    assert receipts.picked_up(11, now=now) is None
    assert receipts.row_sends(11, now=now)[0]["state"] == "received"


def test_finished_line_with_a_parenthetical_is_finished_not_a_pickup():
    """'ACK — lane — <utc> — finished (final for this note): …' is a finish, not a fresh pickup
    (otherwise the board says FIXING while the work sits done)."""
    from server import receipts as r
    text = ("ACK — main-session — 2025-01-20T05:26:10Z — picked up: working it now\n"
            "ACK — main-session — 2025-01-20T08:52:00Z — finished (final for this note): v016 delivered\n")
    assert r._is_finished(text) if hasattr(r, "_is_finished") else r._FINISHED_RE.search(text)
    assert r._pickup_state(text) is None


def test_blocked_pickup_reads_as_queued_not_fixing():
    """'picked up … BLOCKED at pre-flight … Queued to retry' must read as queued, not FIXING · N min in."""
    from server import receipts as r
    text = ("ACK — reel-factory-interactive — 2025-01-20T10:05:52Z — picked up: round 4. BLOCKED at pre-flight: "
            "disk 97GB < 100GB floor. No render attempted. Queued to retry.json for 10:35:52Z\n")
    st = r._pickup_state(text)
    assert st and st["queued"] and "disk" in (st["queued_why"] or "").lower()
    assert not r._is_finished("ACK — lane — 2025-01-20T10:05:52Z — picked up: REJECT on v001 (delivered ~25 min ago)\n")


def test_queued_pickup_reaches_the_board_card(tmp_path, monkeypatch):
    """receipts.py sets queued/queued_why; board.row_card must carry them through so the QUEUED
    phrase is reachable and a lane parked on the disk floor never reads 'FIXING FROM YOUR NOTE'."""
    import time as _t
    from server import board, receipts
    monkeypatch.setattr(receipts, "BUS_DIR", tmp_path)
    monkeypatch.setattr(board, "factory_working_row", lambda: None)
    stamp = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - 300))
    (tmp_path / "ui-feedback-row13-20250120T100020Z.md").write_text(
        "# ui-feedback — row 13 — NOTES\n\nSent by: admin (OPERATOR)\n\nOperator text (verbatim):\n\nfix the grade\n---\n"
        f"ACK — reel-factory-interactive — {stamp} — picked up: round 4. BLOCKED at pre-flight: "
        "disk 97GB < 100GB floor. No render attempted. Queued to retry.json for later\n")
    card = board.row_card({"sequence": 13}, {}, {})
    assert card["bucket"] == "machine"
    assert card["phrase"].startswith("QUEUED"), card["phrase"]
    assert "min in" not in card["phrase"]
    assert "disk" in card["phrase"].lower()


def test_factory_light_shows_launching_then_launch_failed_not_asleep(tmp_path, monkeypatch):
    """The 480 s awake rule must not turn a slow launch or a usage-limited factory into 'asleep';
    heal's grace is 900 s and 'limited' is alive and self-resuming."""
    import json, time
    from server import board
    status = tmp_path / "factory_status.json"
    monkeypatch.setattr(board, "FACTORY_STATUS", status)
    status.write_text(json.dumps({"alive_at": time.time() - 600, "state": "launching", "launch_count": 1, "paused": True}))
    f = board.factory_status()
    assert f["awake"] and f["state"] == "launching" and f["paused"] is True
    status.write_text(json.dumps({"alive_at": time.time() - 1000, "state": "launching", "launch_count": 3}))
    f = board.factory_status()
    assert not f["awake"] and f["state"] == "launch-failed" and f["launch_count"] == 3
    status.write_text(json.dumps({"alive_at": time.time() - 3000, "state": "limited", "limit_until_text": "8pm", "paused": True}))
    f = board.factory_status()
    assert f["state"] == "limited" and f["limit_until"] == "8pm" and f["paused"] is True


def test_unanswered_order_is_loud_on_the_card_and_the_header(tmp_path, monkeypatch):
    """An order nobody picked up for > WAIT_S must say so on the card (not 'new version
    ready') and in the board header; an ACK clears it."""
    import time as _t
    from server import board, receipts
    monkeypatch.setattr(receipts, "BUS_DIR", tmp_path)
    monkeypatch.setattr(board, "factory_working_row", lambda: None)
    monkeypatch.setattr(board, "CONTROL_DIR", tmp_path / "control")      # no runner locks = slots free
    stamp = _t.strftime("%Y%m%dT%H%M%SZ", _t.gmtime(_t.time() - 900))
    f = tmp_path / f"ui-feedback-row13-{stamp}.md"
    f.write_text("# ui-feedback — row 13 — NOTES\n\nSent by: admin (OPERATOR)\n\nOperator text (verbatim):\n\nfix it\n---\nFactory: ...\n")
    w = receipts.unanswered()
    assert len(w) == 1 and w[0]["row"] == 13 and 800 < w[0]["age_s"] < 1000
    card = board.row_card({"sequence": 13}, {}, {})
    assert card["phrase"].startswith("SENT · NOT PICKED UP YET · 1") and card["color"] == "red", card["phrase"]
    f.write_text(f.read_text() + "ACK — reel-factory-interactive — 2025-01-21T00:00:00Z — picked up: on it\n")
    assert receipts.unanswered() == []
    fresh = tmp_path / f"ui-feedback-row11-{_t.strftime('%Y%m%dT%H%M%SZ', _t.gmtime())}.md"
    fresh.write_text("# ui-feedback — row 11\n\nOperator text (verbatim):\n\nhello\n---\n")
    assert receipts.unanswered() == []          # younger than WAIT_S: still just 'waiting'


def test_help_page_is_role_aware(client):
    """Help must not describe Make/Now/Nudge to editors who cannot see them, must say an editor's
    approval is advisory, and must explain the archive and that 'in the machine' can mean waiting."""
    from server import auth
    test_client, _ = client
    html = test_client.get("/help", cookies=login(test_client)).text
    assert "Adding a new reel" in html and "Your role: editor" not in html
    assert "NOT PICKED UP YET" in html and "QUEUED" in html and "archive" in html


def test_help_page_for_an_editor(client):
    from server import auth
    test_client, _ = client
    auth.set_credentials("ed", "editor-pass-123", role="EDITOR")
    html = test_client.get("/help", cookies=login(test_client, "ed", "editor-pass-123")).text
    assert "Your role: editor" in html and "approval is advice" in html
    assert "Adding a new reel" not in html and "The Nudge button" not in html


def test_structured_status_blocks_beat_prose(tmp_path, monkeypatch):
    """busline.py writes a status block under every ACK/CALL; the Deck reads the block and only
    falls back to regexes when the newest line has none. Uses the repo's own busline.py
    (code/reel-production-tools/busline.py)."""
    import importlib.util, time as _t
    from server import receipts as r
    src = APP_ROOT.parent / "reel-production-tools" / "busline.py"
    if not src.is_file():
        pytest.skip("busline.py is not checked out next to the Deck")
    spec = importlib.util.spec_from_file_location("busline", str(src))
    busline = importlib.util.module_from_spec(spec); spec.loader.exec_module(busline)
    f = tmp_path / "ui-feedback-row13-20250121T135754Z.md"
    f.write_text("# ui-feedback — row 13\n\nOperator text (verbatim):\n\nfix it\n---\nFactory: ...\n")
    t0 = _t.time() - 1800
    busline.append(f, "reel-factory-interactive", "picked_up", "round 5, delivered ~25 min ago was v008", now=t0)
    st = r._pickup_state(f.read_text())
    assert st and st["structured"] and not st["queued"] and not r._is_finished(f.read_text())   # 'delivered' in prose no longer fools it
    busline.append(f, "reel-factory-interactive", "blocked", "pre-flight refused", why="workbench 97GB below the 100GB floor",
                   not_before="2025-01-21T15:00:00Z", now=t0 + 60)
    st = r._pickup_state(f.read_text())
    assert st["queued"] and st["queued_why"] == "workbench 97GB below the 100GB floor" and abs(st["at_epoch"] - int(t0)) < 2
    busline.append(f, "reel-factory-interactive", "progress", "scope note while still blocked", now=t0 + 120)
    st = r._pickup_state(f.read_text())
    assert st["queued"] and "97GB" in st["queued_why"]                         # progress after blocked is STILL blocked
    busline.append(f, "reel-factory-interactive", "running", "floor cleared, render started", now=t0 + 600)
    busline.append(f, "reel-factory-interactive", "progress", "finished the render, audit running", now=t0 + 900)
    st = r._pickup_state(f.read_text())
    assert st and not st["queued"] and abs(st["at_epoch"] - int(t0)) < 2       # clock NOT restarted by a progress line; 'finished' in prose ignored
    assert not r._is_finished(f.read_text())
    busline.append(f, "reel-factory-interactive", "called", "audit FAIL, keep v009? 1 = keep it, 2 = one more round", now=t0 + 1200)
    assert r._pickup_state(f.read_text()) is None and not r._is_finished(f.read_text())
    busline.append(f, "reel-factory-interactive", "finished", "v009 on the row", artifact="deliver/v009.mp4", now=t0 + 1500)
    assert r._is_finished(f.read_text()) and r._pickup_state(f.read_text()) is None
    # a hand-written line AFTER the last block: prose is the newest truth again
    f.write_text(f.read_text() + "ACK — main-session — 2025-01-21T15:10:00Z — picked up: reopening this\n")
    st = r._pickup_state(f.read_text())
    assert st and st["lane"] == "main-session" and not st.get("structured")


def test_a_lane_typed_ruling_and_newer_finished_work_close_a_call(tmp_path, monkeypatch):
    """A ruling phrased 'RULING on the v008 CALL (ui-feedback-…md, CALL …): …', or newer finished
    work on the row, closes the CALL (the card must not keep saying FACTORY NEEDS YOUR CALL)."""
    from server import receipts as r
    monkeypatch.setattr(r, "BUS_DIR", tmp_path)
    old = tmp_path / "ui-feedback-row13-20250120T100020Z.md"
    old.write_text("# ui-feedback — row 13\n\nSent by: admin (OPERATOR)\n\nOperator text (verbatim):\n\nfix the font\n---\nFactory: ...\n"
                   "ACK — reel-factory-interactive — 2025-01-20T10:05:52Z — picked up: round 4\n"
                   "CALL — reel-factory-interactive — 2025-01-20T12:03:37Z — v008 failed audit, need your ruling\n")
    assert [c["call_id"] for c in r.bus_calls(13)] == [old.name]
    new = tmp_path / "ui-feedback-row13-20250121T135754Z.md"
    new.write_text("# ui-feedback — row 13\n\nSent by: main-session (SYSTEM)\n\nOperator text (verbatim):\n\n"
                   "RULING on the v008 CALL (ui-feedback-row13-20250120T100020Z.md, CALL 2025-01-20T12:03:37Z): build v009\n---\nFactory: ...\n")
    assert r._ruling_target(new.read_text()) == old.name
    assert r.bus_calls(13) == []                                   # answered by name, however it was phrased
    new.write_text("# ui-feedback — row 13\n\nOperator text (verbatim):\n\nmake the captions readable\n---\n"
                   "ACK — reel-factory-interactive — 2025-01-21T14:27:51Z — finished: v009 on the row\n")
    assert r.bus_calls(13) == []                                   # no ruling words at all: newer finished work closes it


def test_a_file_named_only_in_a_dict_key_is_still_declared():
    """A delivery registered as {"files": {"deliver/x-v009.mp4": "<sha>"}} names its file only in a
    dict key; walking values only would never show v009."""
    from server import registry as reg
    row = {"sequence": 13, "v009_delivery_20250121": {"files": {"deliver/reel13-ABC-SAMPLE-v009.mp4": "3735c6fb", "deliver/APPROVAL-CARD-v009.md": "5d31"}}}
    assert "reel13-ABC-SAMPLE-v009.mp4" in reg.row_declared_names(row)["loose"]


def test_drop_card_shows_a_factory_call_not_picked_up(tmp_path, monkeypatch):
    """A drop that raises a CALL must say so on its card, not PICKED UP."""
    from server import board
    monkeypatch.setattr(board, "BUS_DIR", tmp_path)
    f = tmp_path / "ui-drop-20250122T035515Z.md"
    f.write_text("# ui-drop\n\nhttps://www.instagram.com/reel/EXAMPLE01/\n---\n"
                 "ACK — reel-factory-interactive — 2025-01-22T05:51:11Z — picked up: taking the new reference\n"
                 '<!--status {"v":1,"row":null,"lane":"reel-factory-interactive","at":"2025-01-22T05:51:11Z","state":"picked_up"} -->\n'
                 "CALL — reel-factory-interactive — 2025-01-22T05:53:14Z — Row 14 stopped at intake: drop the mp4 (A)\n"
                 '<!--status {"v":1,"row":null,"lane":"reel-factory-interactive","at":"2025-01-22T05:53:14Z","state":"called"} -->\n')
    (card,) = board.pending_drops([])
    assert card["state"] == "called" and "drop the mp4" in card["say"]
    # a later progress line must not hide the question
    with f.open("a") as fh:
        fh.write("ACK — reel-factory-interactive — 2025-01-22T06:10:00Z — progress: still waiting\n"
                 '<!--status {"v":1,"row":null,"lane":"reel-factory-interactive","at":"2025-01-22T06:10:00Z","state":"progress"} -->\n')
    (card,) = board.pending_drops([])
    assert card["state"] == "called" and "drop the mp4" in card["say"]
    # hand-written prose (no status block) still reads as a call
    g = tmp_path / "ui-drop-20250122T040000Z.md"
    g.write_text("# ui-drop\n\nhttps://www.instagram.com/reel/EXAMPLE01/\n---\nCALL — lane — 2025-01-22T06:00:00Z — need the file\n")
    states = {c["receipt"]: c["state"] for c in board.pending_drops([])}
    assert states[g.name] == "called"


def test_call_on_a_drop_receipt_reaches_the_row_card(tmp_path, monkeypatch):
    """A row registered CALL_REQUIRED whose question lives only on the ui-drop receipt."""
    from server import receipts
    monkeypatch.setattr(receipts, "BUS_DIR", tmp_path)
    f = tmp_path / "ui-drop-20250122T035515Z.md"
    f.write_text("# ui-drop\n\nhttps://www.instagram.com/reel/EXAMPLE01/\n---\n"
                 "CALL — reel-factory-interactive — 2025-01-22T05:53:14Z — reference is login-walled: drop the mp4 (A)\n"
                 '<!--status {"v":1,"row":null,"lane":"reel-factory-interactive","at":"2025-01-22T05:53:14Z","state":"called"} -->\n')
    (call,) = receipts.drop_calls("EXAMPLE01")
    assert call["call_id"] == f.name and "drop the mp4" in call["one_liner"]
    assert receipts.drop_calls("SomethingElse") == [] and receipts.drop_calls("") == []
    with f.open("a") as fh:   # the factory moves on: the question is closed
        fh.write("ACK — reel-factory-interactive — 2025-01-22T07:00:00Z — running: reference received, intake started\n"
                 '<!--status {"v":1,"row":14,"lane":"reel-factory-interactive","at":"2025-01-22T07:00:00Z","state":"running"} -->\n')
    assert receipts.drop_calls("EXAMPLE01") == []


def test_worker_sentence_in_last_error_is_a_note_not_an_empty_red_warning():
    """A plain worker sentence in last_error is an amber note, not a red stamp reading only ': '."""
    from server import board
    n = board._norm_error("row 11 v020 queued behind another lane")
    assert n["level"] == "amber" and n["reason"].startswith("row 11") and n["receipt"] == ""
    assert board._norm_error(None) is None and board._norm_error("") is None
    d = board._norm_error({"reason": "usage limit", "receipt": "ui-x.md", "detail": "d", "at_utc": "t"})
    assert d["level"] == "red" and d["receipt"] == "ui-x.md"


def test_minutes_in_count_the_working_stretch_not_the_blocked_wait(tmp_path, monkeypatch):
    """Picked up, blocked ~3 h on the disk floor, relaunched: "N min in" counts only the working
    stretch, not the blocked wait."""
    import time
    from server import receipts
    monkeypatch.setattr(receipts, "BUS_DIR", tmp_path)
    f = tmp_path / "ui-feedback-row14-20250122T061505Z.md"
    def blk(state, at, extra=""):
        return (f"ACK - lane - {at} - {state}: x\n"
                '<!--status {"v":1,"row":14,"lane":"lane","at":"%s","state":"%s"%s} -->\n' % (at, state, extra))
    f.write_text("# ui-feedback - row 14\n---\n"
                 + blk("picked_up", "2025-01-22T06:15:00Z")
                 + blk("blocked", "2025-01-22T06:25:00Z", ',"why":"disk floor"')
                 + blk("queued", "2025-01-22T07:59:00Z", ',"why":"disk floor"')
                 + blk("running", "2025-01-22T09:15:00Z"))
    import calendar
    now = calendar.timegm(time.strptime("2025-01-22T09:25:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    pick = receipts.picked_up(14, now=now)
    assert pick is not None and not pick["queued"]
    assert 9 * 60 <= pick["work_since_s"] <= 11 * 60, pick["work_since_s"]       # 10 min of work
    assert pick["since_s"] > 3 * 3600                                            # the whole span, for trust only


def test_empty_index_says_looking_not_missing(monkeypatch):
    """While the drive scan is still cold past COLD_WAIT_SECONDS, a delivered row says it is looking,
    not "video not on this machine"."""
    from server import registry as reg
    row = {"sequence": 13, "review_state": "LOCAL_REVIEW_READY__V010__PENDING_HUMAN"}
    media = {"versions": [], "current": None, "assets": [], "missing": []}
    building = reg.row_plain_state(row, media, index_ready=False)
    assert "Looking for the video" in building["phrase"] and building["unplayable"]
    ready = reg.row_plain_state(row, media, index_ready=True)
    assert "not on this machine" in ready["phrase"]


def test_queued_phrase_when_every_runner_slot_is_busy(tmp_path, monkeypatch):
    """All 3 lane slots busy -> the card says QUEUED with its place in line, never a red 'not picked up'."""
    import json, time
    from server import receipts, board
    monkeypatch.setattr(receipts, "BUS_DIR", tmp_path)
    monkeypatch.setattr(board, "factory_working_row", lambda: None)
    control = tmp_path / "control"; control.mkdir()
    for name in ("orderd.lock", "orderd.lane2.lock", "orderd.lane3.lock"):
        (control / name).write_text(json.dumps({"session": "reel-lane-x", "started": time.time()}))
    monkeypatch.setattr(board, "CONTROL_DIR", control)
    assert board.runner_slots() == (3, 3)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - 900))
    (tmp_path / f"ui-feedback-row13-{stamp}.md").write_text(
        "# ui-feedback — row 13 — NOTES\n\nOperator text (verbatim):\n\nfix it\n---\nFactory: append ACK below.\n")
    card = board.row_card({"sequence": 13}, {}, {})
    assert card["phrase"].startswith("QUEUED · all 3 lanes busy · next in line"), card["phrase"]
    assert card["color"] == "amber"


def test_row_batch_finds_variants_in_the_rows_own_deliver_folder(tmp_path, monkeypatch):
    """orderd lanes deliver a batch into <row lane>/deliver/variants/<stamp>/varNN.mp4 (+ cast_varNN.json per planned
    variant). The newest stamp with a video is the batch; the Deck must list it and say how many are still to render."""
    from server import registry as reg
    lane = tmp_path / "reel16-ref-19-20250202" / "deliver" / "variants"
    old, new = lane / "20250202T010101Z", lane / "20250203T122414Z"
    old.mkdir(parents=True); new.mkdir(parents=True)
    (old / "var01.mp4").write_bytes(b"old")
    for n in (1, 2, 3):
        (new / f"var{n:02d}.mp4").write_bytes(b"x" * n)
    for n in range(1, 11):
        (new / f"cast_var{n:02d}.json").write_text("{}")
    (new / "BATCH_CARD.md").write_text("# card")
    (lane / "brain").mkdir()
    monkeypatch.setattr(reg, "WORKBENCH_ROOT", tmp_path)
    monkeypatch.setattr(reg, "workbench_dirnames", lambda force=False: ["reel16-ref-19-20250202", "reel5-other"])
    batch = reg.row_batch({"sequence": 16}, {}, {"versions": [], "untagged": [], "comparisons": []})
    assert [v["variant"] for v in batch["variants"]] == [1, 2, 3]
    assert all(str(new) in v["path"] for v in batch["variants"])
    assert batch["expected"] == 10 and batch["batch_stamp"] == "20250203T122414Z"
    assert batch["note"] == "3 of 10 variants rendered so far"
    assert batch["card"].endswith("BATCH_CARD.md") and batch["older_batches"] == ["20250202T010101Z"]


def test_variant_batch_phrase_says_building_stopped_and_ready(tmp_path, monkeypatch):
    import os, time
    from server import board
    monkeypatch.setattr(board, "BUS_DIR", tmp_path)
    files = []
    for n in (1, 2, 3):
        f = tmp_path / f"var{n:02d}.mp4"; f.write_bytes(b"x"); files.append({"variant": n, "name": f.name, "path": str(f)})
    now = time.time()
    batch = {"batch_stamp": "20250203T122414Z", "variants": files, "expected": 10, "lane_mtime": now - 3600}
    assert board.variant_batch_phrase(16, batch, {16}, now)[1].startswith("BUILDING VARIANTS · 3 of 10")
    assert board.variant_batch_phrase(16, batch, set(), now)[1].startswith("VARIANTS STOPPED · 3 of 10")
    assert board.variant_batch_phrase(16, dict(batch, lane_mtime=now - 60), set(), now)[0] == "machine"
    full = dict(batch, expected=3)
    assert board.variant_batch_phrase(16, full, set(), now) == ("needs_you", "3 VARIANTS READY · watch, keep / kill, leave notes", "green")
    verdict = tmp_path / "ui-batch-verdict-row16-20250204T000000Z.md"; verdict.write_text("x")
    os.utime(verdict, (now + 5, now + 5))
    assert board.variant_batch_phrase(16, full, set(), now) is None
    assert board.variant_batch_phrase(16, {"variants": files}, {16}, now) is None      # old-style batch: say nothing


def test_a_row_never_plays_another_rows_file(tmp_path, monkeypatch):
    """Row 19's lane delivers a bare `v008.mp4`; the index also knows a `v008.mp4` inside row 14's work folder.
    The row's own deliver/ copy wins, and with no own copy the other row's file is refused."""
    from server import registry as reg
    other_dir = tmp_path / "reel14-ref-58-20250122" / "work" / "v008-first"; other_dir.mkdir(parents=True)
    own_dir = tmp_path / "reel19-ref-24-20250203" / "deliver"; own_dir.mkdir(parents=True)
    (other_dir / "v008.mp4").write_bytes(b"row14"); (own_dir / "v008.mp4").write_bytes(b"row19-own")
    monkeypatch.setattr(reg, "WORKBENCH_ROOT", tmp_path)
    monkeypatch.setattr(reg, "workbench_dirnames", lambda force=False: [p.name for p in tmp_path.iterdir()])
    index = {"v008.mp4": [str(other_dir / "v008.mp4")]}                       # the index only knows row 14's copy
    assert reg.resolve_name_for_row("v008.mp4", index, None, 19) == str(own_dir / "v008.mp4")
    assert reg.resolve_name_for_row("v008.mp4", index, None, 14) == str(other_dir / "v008.mp4")
    assert reg.resolve_name_for_row("v008.mp4", index, None, 17) is None  # another row's file is never borrowed
    loose = tmp_path / "exports"; loose.mkdir(); (loose / "x.mp4").write_bytes(b"n")
    assert reg.resolve_name_for_row("x.mp4", {"x.mp4": [str(loose / "x.mp4")]}, None, 17) == str(loose / "x.mp4")
