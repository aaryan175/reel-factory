"""asker: open CALLs on the bus become one short push each; a numbered reply becomes a RULING receipt;
a CALL answered later is not open; old CALLs are history; spend/post questions never auto-default."""
import json, time
from pathlib import Path

import pytest

import asker, orderd


def _bus(tmp_path, monkeypatch):
    bus = tmp_path / "bus"; bus.mkdir(); ctl = tmp_path / "ctl"; ctl.mkdir()
    monkeypatch.setattr(orderd, "BUS", bus); monkeypatch.setattr(orderd, "CONTROL", ctl)
    monkeypatch.setattr(asker, "STATE", ctl / "asker.state.json")
    monkeypatch.setattr(asker, "TELEGRAM", tmp_path / "no-telegram.json")
    return bus


def _call(bus, name, row, text, at="2025-01-03T02:00:00Z", why="x"):
    p = bus / name
    p.write_text(f"# {name}\n\nSent by: admin (OPERATOR)\n\nOperator text (verbatim):\n\nhi\n---\n"
                 f"CALL — orderd-lane — {at} — {text}\n"
                 f'<!--status {json.dumps({"v":1,"row":row,"lane":"orderd-lane","at":at,"state":"called","why":why})} -->\n')
    return p


def test_parse_options_and_question():
    t = "S4 hides the caption. Reframe once more? 1 = reframe S4 (recommended), 2 = accept with a disclosure"
    assert asker.parse_options(t) == [(1, "reframe S4 (recommended)"), (2, "accept with a disclosure")]
    assert asker.question_line(t) == "S4 hides the caption. Reframe once more?"
    assert asker.parse_options("x RULING 1 = buy, RULING 2 = substitute")[1] == (2, "substitute")


def test_open_calls_push_and_reply_ruling(tmp_path, monkeypatch):
    bus = _bus(tmp_path, monkeypatch)
    now = orderd.parse_utc("2025-01-03T02:10:00Z")
    _call(bus, "ui-feedback-row16-20250103T010000Z.md", 16, "Two caption defects. 1 = one more round (recommended), 2 = review v006 as is")
    _call(bus, "ui-drop-20250102T135505Z.md", None, "Row 17 v006 unaudited. 1 = run the audit, 2 = reviewer decides")   # row from the text
    _call(bus, "ui-feedback-row11-20241219T100812Z.md", 11, "old question 1 = a, 2 = b", at="2024-12-19T10:58:00Z")     # history
    calls = asker.open_calls(now)
    assert [c["row"] for c in calls] == [16, 17]
    assert calls[0]["recommended"] == 1 and calls[1]["recommended"] is None
    msg = asker.fmt_push(calls[0])
    assert msg.startswith("❓ row 16: Two caption defects") and "1 = one more round (recommended)" in msg and "Reply: 16 <number>" in msg
    p = asker.write_ruling(calls[0], 2, "16 2 keep it", "test")
    body = p.read_text()
    assert p.name.startswith("ui-feedback-row16-") and "Operator text (verbatim):\n\n16 2 keep it" in body
    assert "RULING on ui-feedback-row16-20250103T010000Z.md (CALL of 2025-01-03T02:00:00Z): 2 — review v006 as is" in body
    # the ruling receipt is newer than the CALL -> row 16 is no longer open
    assert [c["row"] for c in asker.open_calls(now)] == [17]


def test_reply_regex_and_no_auto_for_spend():
    m = asker._REPLY_RE.match("16 1"); assert (m.group(1), m.group(2)) == ("16", "1")
    m = asker._REPLY_RE.match("row 14, 2 make it brighter"); assert (m.group(1), m.group(2), m.group(3).strip()) == ("14", "2", "make it brighter")
    m = asker._REPLY_RE.match("2"); assert (m.group(1), m.group(2)) == (None, "2")
    assert asker._REPLY_RE.match("hello there") is None
    assert asker.NO_AUTO_RE.search("Buy the font? 1 = buy (recommended), 2 = no") and not asker.NO_AUTO_RE.search("Reframe S4? 1 = yes (recommended), 2 = no")


def test_dry_tick_sends_nothing_and_writes_nothing(tmp_path, monkeypatch, capsys):
    bus = _bus(tmp_path, monkeypatch)
    _call(bus, "ui-feedback-row16-20250103T010000Z.md", 16, "Q? 1 = a (recommended), 2 = b", at=orderd.utc(time.time() - 60))
    assert asker.tick(dry=True) == 0
    out = capsys.readouterr().out
    assert "would send" in out or "DRY-RUN" in out
    assert not asker.STATE.exists() and len(list(bus.iterdir())) == 1
