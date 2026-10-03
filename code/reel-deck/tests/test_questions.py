"""Factory questions: option parsing, the open/closed rule, and the Board strip + row-page card
that answer a CALL with one tap."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

from server import board as _board  # noqa: E402

_REAL_OPEN_CALLS = _board.open_calls
FOOT = "---\nFactory: treat exactly like operator chat input.\n"


def _receipt(bus: Path, name: str, body: str, lines: list[str]) -> Path:
    bus.mkdir(parents=True, exist_ok=True)
    path = bus / name
    path.write_text(f"# {name}\n\nSent by: admin (OPERATOR)\n\nOperator text (verbatim):\n\n{body}\n{FOOT}" + "".join(l + "\n" for l in lines))
    return path


@pytest.fixture()
def deck(tmp_path, monkeypatch):
    """The real app on a fake one-row registry and an empty temp bus."""
    from server import auth, board, eta, lanes, receipts
    from server import registry as reg
    bus = tmp_path / "bus"
    bus.mkdir()
    monkeypatch.setattr(auth, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(receipts, "BUS_DIR", bus)
    monkeypatch.setattr(receipts, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(receipts, "FACTORY_STATUS_PATH", tmp_path / "factory_status.json")
    monkeypatch.setattr(board, "BUS_DIR", bus)
    monkeypatch.setattr(board, "FACTORY_STATUS", tmp_path / "factory_status.json")
    rows = [{"sequence": 16, "creative_hook": "synthetic test reel", "review_state": "BUILDING"}]
    monkeypatch.setattr(reg, "load_registry", lambda force=False: {"rows": rows, "updated_at_utc": "x", "error": None})
    monkeypatch.setattr(reg, "index_snapshot", lambda: {"map": {}, "state": "READY", "files": 0, "age_s": 0, "error": None})
    monkeypatch.setattr(reg, "workbench_status", lambda force=False: {"status": "OK"})
    monkeypatch.setattr(board, "open_calls", lambda rows_payload=None: [])
    monkeypatch.setattr(board, "board_config", lambda: {"current": [16], "hide": []})
    monkeypatch.setattr(board, "factory_working_row", lambda: None)
    monkeypatch.setattr(eta, "bus_events", lambda *a, **k: {})
    monkeypatch.setattr(lanes, "live_rows", lambda *a, **k: {})
    auth.set_credentials("op", "test-pass-123")
    from server import main
    client = TestClient(main.app)
    r = client.post("/login", data={"username": "op", "password": "test-pass-123"}, follow_redirects=False)
    assert r.status_code == 303
    return client, bus, r.cookies


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize("text,expect", [
    ("Which grade? 1 = reference colour, 2 = house grade", [("1", "reference colour"), ("2", "house grade")]),
    ("CALL: x.md — RULING 1 = one more round (recommended), RULING 2 = accept with disclosure. Nothing delivered.",
     [("1", "one more round (recommended)"), ("2", "accept with disclosure")]),
    ("Two ways forward. 1. Reframe S4 at 3.0s. 2. Accept with disclosure. Say which.",
     [("1", "Reframe S4 at 3.0s"), ("2", "Accept with disclosure")]),
    ("Pick: (a) keep the red, (b) mute it to pink, (c) drop the word", [("a", "keep the red"), ("b", "mute it to pink"), ("c", "drop the word")]),
])
def test_option_parsing_four_patterns(text, expect):
    from server.receipts import parse_call_options
    _prefix, options = parse_call_options(text)
    assert [(o["n"], o["label"]) for o in options] == expect


def test_no_options_when_there_is_no_clean_run():
    from server.receipts import parse_call_options
    for text in ("audit measured +3.8 mid and -2.8 highlight at v1.0", "stopped at intake: drop the mp4 (A)", "round 2. Then stop."):
        assert parse_call_options(text)[1] == []


def test_question_text_always_ends_with_a_question_mark():
    from server.receipts import parse_call_options, question_sentence
    for text in ("v009 is on disk. The red on <line A> needs a ruling. 1 = keep, 2 = pink",
                 "Status line first. Which treatment for S09? 1 = v011, 2 = v016",
                 "1 = keep, 2 = drop",
                 "audit failed on colour, nothing delivered"):
        prefix, options = parse_call_options(text)
        q = question_sentence(prefix, bool(options))
        assert q.endswith("?"), q
    prefix, _ = parse_call_options("v009 is on disk. The red needs a ruling. 1 = keep, 2 = pink")
    assert question_sentence(prefix, True) == "v009 is on disk — which one?"
    assert question_sentence("Status line first. Which treatment for S09? ", True) == "Which treatment for S09?"


# ------------------------------------------------------------------ open / closed


def test_newest_of_two_open_calls_wins(deck):
    from server import receipts
    _, bus, _ = deck
    _receipt(bus, "ui-feedback-row16-20250128T100000Z.md", "fix the font",
             ["ACK — orderd — 2025-01-28T10:01:00Z — picked up: round 1",
              "CALL — orderd — 2025-01-28T11:00:00Z — older question? 1 = old a, 2 = old b"])
    _receipt(bus, "ui-frame-note-row16-20250128T090000Z.md", "colour",
             ["CALL — orderd — 2025-01-28T12:00:00Z — Keep the red on <line A>? 1 = keep it, 2 = make it pink"])
    q = receipts.open_question(16)
    assert q["call_id"] == "ui-frame-note-row16-20250128T090000Z.md"
    assert q["question"] == "Keep the red on <line A>?"
    assert [o["ruling"] for o in q["options"]] == [
        "RULING on ui-frame-note-row16-20250128T090000Z.md: 1 — keep it",
        "RULING on ui-frame-note-row16-20250128T090000Z.md: 2 — make it pink"]


def test_call_followed_by_finished_or_pickup_is_closed_but_progress_is_not(deck):
    from server import receipts
    _, bus, _ = deck
    path = _receipt(bus, "ui-feedback-row16-20250128T100000Z.md", "fix it",
                    ["CALL — orderd — 2025-01-28T11:00:00Z — which one? 1 = a thing, 2 = b thing"])
    assert receipts.open_question(16)
    with path.open("a") as fh:
        fh.write("ACK — orderd — 2025-01-28T11:10:00Z — progress: still measuring\n")
    assert receipts.open_question(16), "a progress note does not answer the question"
    with path.open("a") as fh:
        fh.write("ACK — orderd — 2025-01-28T12:00:00Z — finished: v010 delivered\n")
    assert receipts.open_question(16) is None
    path.write_text(path.read_text().replace("— finished: v010 delivered", "— picked up: next round"))
    assert receipts.open_question(16) is None


def test_a_later_operator_note_or_a_ruling_closes_the_call(deck):
    from server import receipts
    _, bus, _ = deck
    _receipt(bus, "ui-feedback-row16-20250128T100000Z.md", "fix it",
             ["CALL — orderd — 2025-01-28T11:00:00Z — which one? 1 = a thing, 2 = b thing"])
    later = _receipt(bus, "ui-frame-note-row16-20250128T113000Z.md", "something else", [])
    assert receipts.open_question(16) is None
    later.unlink()
    _receipt(bus, "ui-feedback-row16-20250128T090000Z.md",
             "RULING on ui-feedback-row16-20250128T100000Z.md: 2 — b thing", [])   # stamped BEFORE the call, still names it
    assert receipts.open_question(16) is None


def test_registry_call_with_a_closed_state_never_reaches_the_card(deck, monkeypatch):
    import json
    from server import receipts
    from server import registry as reg
    reg_file = deck[1].parent / "registry.json"
    reg_file.write_text(json.dumps({"open_calls": [
        {"call_id": "CALL-ROW16-a", "state": "RESOLVED", "one_liner": "row 16 old? 1 = x, 2 = y",
         "opened_at_utc": "2025-01-28T09:00:00Z"},
        {"call_id": "CALL-ROW16-b", "state": "AWAITING_OPERATOR", "one_liner": "row 16 S4 framing? 1 = reframe, 2 = accept",
         "opened_at_utc": "2025-01-28T08:00:00Z"}]}))
    monkeypatch.setattr(reg, "REGISTRY_PATH", reg_file)
    calls = _REAL_OPEN_CALLS()                       # the fixture stubs board.open_calls; this is the real reader
    assert [c["call_id"] for c in calls] == ["CALL-ROW16-b"]
    q = receipts.open_question(16, extra_calls=calls)
    assert q["call_id"] == "CALL-ROW16-b" and q["question"] == "row 16 S4 framing?"
    assert q["options"][0]["ruling"] == "RULING on CALL-ROW16-b: 1 — reframe"


# ------------------------------------------------------------------ pages


def test_board_shows_the_strip_with_the_right_buttons_and_ruling(deck):
    client, bus, cookies = deck
    _receipt(bus, "ui-feedback-row16-20250128T100000Z.md", "fix it",
             ["ACK — orderd — 2025-01-28T10:01:00Z — picked up: round 1",
              "CALL — orderd — 2025-01-28T11:00:00Z — S4 is still occluded. CALL: card.md — RULING 1 = one more round reframing S4 (recommended), RULING 2 = accept with disclosure. Nothing delivered."])
    html = client.get("/", cookies=cookies).text
    assert 'class="qstrip"' in html and html.index('class="qstrip"') < html.index("Needs you")
    assert "Row 16</a> · <span class=\"q-text\">S4 is still occluded — which one?</span>" in html
    assert 'data-ruling="RULING on ui-feedback-row16-20250128T100000Z.md: 1 — one more round reframing S4 (recommended)"' in html
    assert 'data-ruling="RULING on ui-feedback-row16-20250128T100000Z.md: 2 — accept with disclosure"' in html
    api = client.get("/api/board", cookies=cookies).json()
    assert [q["row"] for q in api["questions"]] == [16]
    # the same card sits above the comment box on the row page
    row_html = client.get("/row/16", cookies=cookies).text
    assert row_html.count('class="qcard"') == 1 and row_html.index('class="qcard"') < row_html.index('id="composer"')
    # tapping a button = this POST; afterwards the question is closed and the strip is gone
    sent = client.post("/api/verdict", cookies=cookies, json={
        "row": 16, "disposition": "NOTES",
        "text": "RULING on ui-feedback-row16-20250128T100000Z.md: 2 — accept with disclosure — note: ship it"})
    assert sent.status_code == 200
    receipt = (bus / sent.json()["receipt"]).read_text()
    assert "RULING on ui-feedback-row16-20250128T100000Z.md: 2 — accept with disclosure — note: ship it" in receipt
    assert 'class="qstrip"' not in client.get("/", cookies=cookies).text


def test_no_strip_when_the_call_was_closed_by_finished(deck):
    client, bus, cookies = deck
    _receipt(bus, "ui-feedback-row16-20250128T100000Z.md", "fix it",
             ["CALL — orderd — 2025-01-28T11:00:00Z — which one? 1 = a, 2 = b",
              "ACK — orderd — 2025-01-28T12:00:00Z — finished: v010 on the row",
              '<!--status {"v":1,"row":16,"lane":"orderd","at":"2025-01-28T12:00:00Z","state":"finished"} -->'])
    assert 'class="qstrip"' not in client.get("/", cookies=cookies).text
    assert 'class="qcard"' not in client.get("/row/16", cookies=cookies).text


def test_a_hand_written_call_without_lane_or_time_still_reads_as_a_question(deck):
    """A bare `CALL — row NN — needs one ruling…` with no lane/time fields, followed only by an ACK saying
    'left open for the operator', must still show as an open question."""
    from server import receipts
    _, bus, _ = deck
    _receipt(bus, "ui-feedback-row16-20250102T053531Z.md", "REJECT cut C",
             ["ACK — master — 2025-01-02T13:52:58Z — REJECT recorded; parked pending redo-or-drop",
              "", "CALL — row 16 — needs one ruling before the factory can act. Should the factory (a) recast cut C and redeliver, or (b) drop row 16 entirely?",
              "ACK — reel-factory-interactive — 2025-01-05T06:40:28Z — CALL raised above. No build started. Left open for the operator."])
    q = receipts.open_question(16)
    assert q and q["question"] == "row 16 — needs one ruling before the factory can act — which one?"
    assert [o["label"] for o in q["options"]] == ["recast cut C and redeliver", "drop row 16 entirely"]
