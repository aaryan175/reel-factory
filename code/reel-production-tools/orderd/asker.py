#!/usr/bin/env python3
"""asker — the factory's open questions (CALLs) are pushed to a chat notification channel, and a one-character
reply comes back as a ruling, so nobody has to open the Deck to find a question.

Every 60 s (launchd com.reelfactory.reel-asker, `asker.py --once`):
  1. PUSH: every OPEN CALL on the bus (CALL head not followed by a close or a ruling) that has not been
     pushed yet goes to Telegram as ONE short message: the question + its numbered options + "reply 50 1".
  2. REPLIES: Telegram getUpdates; a reply like "50 1", "1" (newest question), "48 2 make it brighter" becomes a
     ui-feedback receipt on the bus in the exact shape the lanes read (RULING on <call receipt>: <n> — <option>),
     the reply text verbatim underneath. orderd picks it up like any Deck answer.
  3. AUTO-DEFAULT: a question whose options mark one "(recommended)" and that nobody answered within AUTO_MINUTES
     is answered with the recommended option, and the reviewer is told (reply with the other number to overturn — the lane
     reads the newest ruling). Never for questions about spending, posting, publishing, Drive or buying.
  4. READY: a `finished` line with an artifact after the last push → "Row 14 ready to review" with the Deck link.

State: _control/asker.state.json (pushed calls, telegram offset, auto-answered). Dry-run prints, sends nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import orderd  # noqa: E402  (bus paths, receipt loader, utc helpers)

STATE = orderd.CONTROL / "asker.state.json"
# Telegram bot config JSON {"bot_token": ..., "chat_id": ...}; without it asker only prints what it would send.
TELEGRAM = Path(os.path.expanduser(os.environ.get("ASKER_TELEGRAM", "~/.config/reelfactory/notify.json")))
DECK = os.environ.get("ASKER_DECK_URL", "https://localhost:8443")
AUTO_MINUTES = int(os.environ.get("ASKER_AUTO_MINUTES", 20))
FRESH_S = int(os.environ.get("ASKER_FRESH_HOURS", 48)) * 3600   # older questions are history, never pushed
OPT_MAX = 160
NO_AUTO_RE = re.compile(r"\b(spend|spending|buy|bought|purchase|post|publish|upload|drive|pay|card|\$\d)", re.I)
_OPT_RE = re.compile(r"(?:^|[\s(/])(?:RULING\s*)?([1-9])\s*[=)\.:]\s*(.+?)(?=(?:[\s,;/]+(?:RULING\s*)?[1-9]\s*[=)\.:])|\s*$)", re.S)
_REPLY_RE = re.compile(r"^\s*(?:row\s*)?(\d{1,3})?\s*[,:\-]?\s*([1-9])\b\s*(.*)$", re.S | re.I)


# ----------------------------------------------------------------------------- state / telegram
def load_state() -> dict:
    return orderd.read_json(STATE, {}) or {"pushed": {}, "offset": 0, "auto": {}, "ready": {}}


def save_state(st: dict) -> None:
    orderd.write_json(STATE, st)


def tg_conf() -> dict | None:
    d = orderd.read_json(TELEGRAM, None)
    return d if isinstance(d, dict) and d.get("bot_token") and d.get("chat_id") else None


def tg_send(text: str, dry: bool = False) -> bool:
    conf = tg_conf()
    if not conf:
        print("asker: no telegram config; would send:\n" + text)
        return False
    if dry:
        print("DRY-RUN telegram send:\n" + text)
        return True
    url = f"https://api.telegram.org/bot{conf['bot_token']}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": conf["chat_id"], "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=20) as r:
            return r.status == 200
    except Exception as e:  # the network is not a reason to lose the question; the next tick re-sends
        print(f"asker: telegram send failed: {e}")
        return False


def tg_updates(offset: int) -> list:
    conf = tg_conf()
    if not conf:
        return []
    url = f"https://api.telegram.org/bot{conf['bot_token']}/getUpdates?" + urllib.parse.urlencode({"offset": offset, "timeout": 0})
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d.get("result") or []
    except Exception as e:
        print(f"asker: telegram getUpdates failed: {e}")
        return []


# ----------------------------------------------------------------------------- the bus
def parse_options(text: str) -> list:
    """[(n, option text)] from '... 1 = reframe (recommended), 2 = accept' (also '1.' / '1)' / 'RULING 1 =')."""
    t = " ".join(str(text).split())
    out = []
    for m in _OPT_RE.finditer(t):
        n, opt = int(m.group(1)), m.group(2).strip(" .,;/")
        if len(opt) > OPT_MAX:
            opt = opt[:OPT_MAX - 1].rstrip() + "…"
        if opt and n not in {k for k, _ in out}:
            out.append((n, opt))
    return out


def question_line(text: str, limit: int = 220) -> str:
    t = " ".join(str(text).split())
    m = _OPT_RE.search(t)
    q = t[: m.start()].strip(" .:;,/-") if m else t
    q = re.sub(r"\b(Pick one|Options|Answer in the Deck.*)\b.*$", "", q, flags=re.I).strip(" .:;,")
    return (q[: limit - 1] + "…") if len(q) > limit else q


def open_calls(now: float) -> list:
    """Every CALL on a bus receipt that no close (finished/called-by-runner/blocked) and no later ruling
    for its row has answered. -> [{receipt, row, at, text, why, options, recommended}]"""
    out = []
    try:
        paths = sorted(p for p in orderd.BUS.iterdir() if p.is_file() and p.name.endswith(".md"))
    except OSError:
        return out
    rulings = {}   # row -> newest reviewer receipt time
    for p in paths:
        m = re.search(r"ui-(?:feedback|frame-note|batch-verdict)-row(\d{1,3})-(\d{8})T(\d{6})Z", p.name)
        if m:
            at = orderd.parse_utc(f"{m.group(2)[:4]}-{m.group(2)[4:6]}-{m.group(2)[6:]}T{m.group(3)[:2]}:{m.group(3)[2:4]}:{m.group(3)[4:]}Z")
            if at:
                rulings[int(m.group(1))] = max(rulings.get(int(m.group(1)), 0), at)
    for p in paths:
        if not p.name.startswith("ui-") and not p.name.startswith("reel"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        last_call = None
        for i, ln in enumerate(lines):
            hm = orderd._HEAD_RE.match(ln)
            if not hm:
                continue
            head, lane, at_s = hm.group(1), hm.group(2).strip(), hm.group(3)
            block = {}
            if i + 1 < len(lines):
                sm = orderd._STATUS_RE.match(lines[i + 1])
                if sm:
                    try:
                        block = json.loads(sm.group(1))
                    except ValueError:
                        block = {}
            if head == "CALL":
                rm = orderd._ROW_RE.search(p.name)
                last_call = {"receipt": p.name, "at_s": at_s, "at": orderd.parse_utc(at_s) or 0, "lane": lane,
                             "text": ln.split(" — ", 3)[-1], "why": block.get("why"),
                             "row": block.get("row") if block.get("row") is not None else (int(rm.group(1)) if rm else None)}
            elif last_call and block.get("state") in ("finished", "blocked") or (head == "ACK" and last_call and "picked up" in ln):
                last_call = None     # closed, or a new round started on this receipt
        if not last_call:
            continue
        if last_call["row"] is None:
            m = re.search(r"\brow (\d{1,3})\b", last_call["text"] + " " + str(last_call["why"]), re.I)
            last_call["row"] = int(m.group(1)) if m else None
        if last_call["row"] is not None and rulings.get(last_call["row"], 0) > last_call["at"]:
            continue             # answered after the CALL (Deck / chat / asker)
        if now - last_call["at"] > FRESH_S:
            continue             # older than FRESH_S: history on the Deck, not a question to push
        opts = parse_options(last_call["text"])
        rec = next((n for n, o in opts if "recommend" in o.lower()), None)
        last_call.update({"options": opts, "recommended": rec, "question": question_line(last_call["text"])})
        out.append(last_call)
    out.sort(key=lambda c: (c["at"], c["row"] or 0))
    return out


def write_ruling(call: dict, n: int, raw: str, by: str) -> Path:
    """A ui-feedback receipt in the exact shape the lanes and the Deck read."""
    opt = dict(call["options"]).get(n, "")
    row = call["row"]
    if row is None:
        raise ValueError("call has no row")
    stamp = orderd.stamp()
    p = orderd.BUS / f"ui-feedback-row{row:02d}-{stamp}.md"
    body = (f"# ui-feedback — row {row:02d} — NOTES — {orderd.utc()}\n\n"
            f"Sent by: admin (OPERATOR) — {by}\n\nDisposition: NOTES\n\nOperator text (verbatim):\n\n{raw.strip() or f'{n}'}\n\n"
            f"RULING on {call['receipt']} (CALL of {call['at_s']}): {n} — {opt}\n"
            "---\nFactory: treat exactly like operator chat input (same authority). After acting, append `ACK — <lane> — <utc>` below this line. Append-only.\n")
    p.write_text(body, encoding="utf-8")
    return p


def fmt_push(call: dict) -> str:
    opts = "\n".join(f"{n} = {o}" for n, o in call["options"]) or "(no numbered options — answer on the Deck)"
    row = f"row {call['row']}" if call["row"] is not None else call["receipt"]
    tail = (f"\nReply: {call['row']} <number>  (or just the number for the newest question)" if call["row"] is not None
            else f"\nAnswer on the Deck: {DECK}/")
    auto = ""
    if call["recommended"] and not NO_AUTO_RE.search(call["text"]):
        auto = f"\nNo reply in {AUTO_MINUTES} min = option {call['recommended']} is taken."
    link = f"{DECK}/row/{call['row']}" if call["row"] is not None else DECK
    return f"❓ {row}: {call['question']}\n{opts}{tail}{auto}\n{link}"


# ----------------------------------------------------------------------------- one tick
def tick(dry: bool = False) -> int:
    now = time.time()
    st = load_state()
    st.setdefault("pushed", {}); st.setdefault("auto", {}); st.setdefault("ready", {}); st.setdefault("offset", 0)
    calls = open_calls(now)
    by_row = {c["row"]: c for c in calls if c["row"] is not None}
    key = lambda c: f"{c['receipt']}@{c['at_s']}"

    # 1. push new questions
    for c in calls:
        if key(c) in st["pushed"]:
            continue
        if tg_send(fmt_push(c), dry):
            st["pushed"][key(c)] = now
            print(f"pushed {key(c)} row={c['row']}")

    # 2. replies
    if not dry:
        for u in tg_updates(int(st["offset"]) + 1 if st["offset"] else 0):
            st["offset"] = max(int(st["offset"]), int(u.get("update_id", 0)))
            msg = u.get("message") or u.get("edited_message") or {}
            conf = tg_conf() or {}
            if str((msg.get("chat") or {}).get("id")) != str(conf.get("chat_id")):
                continue
            text = (msg.get("text") or "").strip()
            m = _REPLY_RE.match(text)
            if not m:
                continue
            row = int(m.group(1)) if m.group(1) else (calls[-1]["row"] if calls else None)
            n = int(m.group(2))
            c = by_row.get(row)
            if c is None:
                tg_send(f"No open question for row {row}. Open: " + (", ".join(str(r) for r in by_row) or "none"))
                continue
            if n not in dict(c["options"]):
                tg_send(f"Row {row}: option {n} does not exist. Options: " + ", ".join(str(k) for k, _ in c["options"]))
                continue
            p = write_ruling(c, n, text, "relayed from Telegram by asker, words verbatim")
            tg_send(f"✅ Row {row}: option {n} sent to the factory ({p.name}).")
            print(f"ruling row {row} option {n} -> {p.name}")
            by_row.pop(row, None)

    # 3. auto-default
    for c in list(by_row.values()):
        k = key(c)
        if c["recommended"] and not NO_AUTO_RE.search(c["text"]) and k in st["pushed"] and k not in st["auto"]:
            if now - float(st["pushed"][k]) >= AUTO_MINUTES * 60:
                if dry:
                    print(f"DRY-RUN would auto-answer {k} with option {c['recommended']}")
                    continue
                p = write_ruling(c, c["recommended"], f"(no reply in {AUTO_MINUTES} min — the recommended option is taken automatically; "
                                 f"reply '{c['row']} <other number>' to overturn)", "asker auto-default (recommended option, no reply)")
                st["auto"][k] = now
                tg_send(f"⏱ Row {c['row']}: no reply in {AUTO_MINUTES} min, took option {c['recommended']} (recommended). "
                        f"Reply '{c['row']} <number>' to change it.")
                print(f"auto-answered {k} -> {p.name}")

    # 4. ready to review
    try:
        for p in orderd.BUS.iterdir():
            if not p.name.startswith("ui-") or not p.name.endswith(".md"):
                continue
            rec = orderd.load_receipt(p)
            if not rec or not rec.blocks:
                continue
            b = rec.blocks[-1]
            if b.get("state") == "finished" and b.get("row") is not None:
                k = f"{p.name}@{b.get('at')}"
                if k in st["ready"]:
                    continue
                if (orderd.parse_utc(str(b.get("at"))) or 0) < now - FRESH_S:
                    continue     # old deliveries are history on the Deck, never a push
                head = next((ln for ln in reversed(p.read_text(encoding="utf-8", errors="replace").splitlines()) if ln.startswith("ACK") and "finished" in ln), "")
                what = " ".join(head.split(" — ", 3)[-1].split())[:220] if head else "delivered"
                if tg_send(f"🎬 Row {b['row']} ready to review: {DECK}/row/{b['row']}\n{what}", dry):
                    st["ready"][k] = now
    except OSError:
        pass

    if not dry:
        save_state(st)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true"); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="print the open questions and exit")
    a = ap.parse_args(argv)
    if a.list:
        for c in open_calls(time.time()):
            print(f"row {c['row']} · {c['receipt']} · {c['at_s']}\n  Q: {c['question']}\n  options: {c['options']} recommended={c['recommended']}")
        return 0
    return tick(dry=a.dry_run or not a.once)


if __name__ == "__main__":
    sys.exit(main())
