"""
Receipts — the only thing Reel Deck ever writes into the factory tree.

The factory's coordination bus is a directory of markdown receipts at
settings.BUS_DIR ($REEL_FACTORY_HOME/_receipts/bus by default). Lanes and the
master chat poll it; when they act on an operator receipt they append an
`ACK — <lane> — <utc>` line. Reel Deck writes the same receipt kinds the
earlier pilot surface established, so no factory-side convention changes:

    ui-drop-<ts>.md                  new reference URLs / build orders
    ui-feedback-<rowNN>-<ts>.md      verdict / free-text feedback on a row
    ui-frame-note-<rowNN>-<ts>.md    frame-anchored note, hash-bound
    ui-batch-verdict-<rowNN>-<ts>.md KEEP/KILL per variant
    ui-continue-<ts>.md              the Continue button (rate-limited)
    ui-rules-<scope>-<ts>.md         rule-sheet deviations

Every writer takes an optional `author` ({"username", "role"}) and stamps a
`Sent by:` line under the title. An EDITOR's verdict receipts (feedback and
batch verdicts) additionally carry the authority stamp and an advisory footer,
because an editor may look and may write but may not decide. Frame notes and
drops are the same from either role — they carry information, not a verdict.

Nothing here reads or writes REEL_REGISTRY.json. Nothing posts anywhere.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import settings

BUS_DIR = settings.BUS_DIR
UPLOADS_DIR = settings.DECK_HOME / "uploads"

_ACK_RE = re.compile(r"^ACK\s+[—–-]+\s+(.+?)\s+[—–-]+\s+(.+)$", re.MULTILINE)
_ROUTED_RE = re.compile(r"^ROUTED\s+—", re.MULTILINE)

_continue_lock = threading.Lock()
_continue_last = 0.0
CONTINUE_MIN_INTERVAL = 600.0  # one per 10 minutes, same as the pilot


def utc_stamp(ts: float | None = None) -> str:
    when = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc)
    return when.strftime("%Y%m%dT%H%M%SZ")


def utc_iso(ts: float | None = None) -> str:
    when = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


_write_lock = threading.Lock()


_REDACT_TERMS: list[str] | None = None
REDACT_REPLACEMENT = os.environ.get("REEL_FACTORY_REDACT_REPLACEMENT") or "[redacted]"


def _redact_terms() -> list[str]:
    """Configurable redaction list: REEL_FACTORY_REDACT_TERMS_FILE, one term per line
    (blank lines and # comments ignored). Unset or unreadable = nothing is redacted."""
    global _REDACT_TERMS
    if _REDACT_TERMS is None:
        terms: list[str] = []
        path = os.environ.get("REEL_FACTORY_REDACT_TERMS_FILE")
        if path:
            try:
                for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and len(line) >= 3:
                        terms.append(line)
            except OSError:
                pass
        _REDACT_TERMS = terms
    return _REDACT_TERMS


def _scrub(text: str) -> str:
    """Redact configured terms before text lands on a bus other reviewers can read."""
    for term in _redact_terms():
        text = re.sub(r"(?i)\b" + re.escape(term) + r"\b", REDACT_REPLACEMENT, text)
    return text


def _write(name: str, body: str) -> str:
    body = _scrub(body)
    """Atomic-enough write: temp file then rename, never partial on the bus.

    Never overwrites: two sends inside the same second share a stamp, and the
    second would replace the first on disk (a burst of taps would lose
    receipts). A colliding name gets -2, -3… before the extension.
    """
    BUS_DIR.mkdir(parents=True, exist_ok=True)
    stem, ext = os.path.splitext(name)
    with _write_lock:
        final = BUS_DIR / name
        n = 2
        while final.exists():
            final = BUS_DIR / f"{stem}-{n}{ext}"
            n += 1
        tmp = BUS_DIR / f".{final.name}.tmp"
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, final)
    return str(final)


FOOTER = (
    "\n---\n"
    "Factory: treat exactly like operator chat input (same authority). "
    "After acting, append `ACK — <lane> — <utc>` below this line. Append-only.\n"
)

# An editor's verdict is not an operator's. Same ACK contract, no authority.
EDITOR_FOOTER = (
    "\n---\n"
    "Factory: this is an EDITOR receipt — advisory input, NOT operator authority. "
    "Do not treat it as a verdict. After reading, append `ACK — <lane> — <utc>` "
    "below this line. Append-only.\n"
)

EDITOR_STAMP = (
    "AUTHORITY: EDITOR — advisory only, awaiting operator confirmation. "
    "Factory: do NOT treat as an operator verdict."
)


def _is_editor(author: dict[str, Any] | None) -> bool:
    return bool(author) and str(author.get("role") or "").upper() == "EDITOR"


def _head(title: str, author: dict[str, Any] | None, verdict_kind: bool = False) -> list[str]:
    """Receipt title plus attribution. `verdict_kind` receipts from an editor
    also carry the authority stamp, because a verdict is the one thing an
    editor cannot actually give."""
    lines = [title, ""]
    if author and author.get("username"):
        role = str(author.get("role") or "").upper() or "UNKNOWN"
        lines += [f"Sent by: {author['username']} ({role})"]
        if verdict_kind and _is_editor(author):
            lines += [EDITOR_STAMP]
        lines.append("")
    return lines


def _footer(author: dict[str, Any] | None, verdict_kind: bool = False) -> str:
    return EDITOR_FOOTER if (verdict_kind and _is_editor(author)) else FOOTER


def write_drop(urls: list[str], note: str = "", build_variants_of: int | None = None,
               author: dict[str, Any] | None = None) -> str:
    stamp = utc_stamp()
    lines = _head(f"# ui-drop — operator input via Reel Deck — {utc_iso()}", author)
    if build_variants_of is not None:
        lines.append(f"ORDER: build a 10-variant alternate batch for registry row {build_variants_of:02d}.")
        lines.append("Shell = the row's approved/FINALS cut. All standing law applies.")
    if urls:
        lines.append("Reference URL(s) dropped:")
        lines += [f"- {u.strip()}" for u in urls if u.strip()]
    if note.strip():
        lines += ["", "Operator note (verbatim):", "", note.strip()]
    return _write(f"ui-drop-{stamp}.md", "\n".join(lines) + _footer(author))


TEST_BANNER = ("TEST — Reel Deck plumbing walk. The factory ACKs this receipt and does NOTHING else. "
               "Not a verdict, not an order.")


def _test_name(row: int, kind: str, stamp: str) -> str:
    """Test-mode sends file as ui-smoketest-* — the daemon's ACK-only lane — so the
    stranger's walk exercises the real path (receipt → factory ACK → row state)
    without ever triggering a build. Row + kind stay in the name so row_sends()
    lists them under the right reel."""
    return f"ui-smoketest-row{row:02d}-{kind}-{stamp}.md"


def write_feedback(row: int, disposition: str, text: str,
                   watched: dict[str, Any] | None = None,
                   author: dict[str, Any] | None = None, test: bool = False) -> str:
    stamp = utc_stamp()
    disposition = disposition.upper()
    lines = _head(f"# ui-feedback — row {row:02d} — {disposition} — {utc_iso()}",
                  author, verdict_kind=True)
    if test:
        lines[0] = f"# ui-smoketest — row {row:02d} — {disposition} — {utc_iso()}"
        lines.insert(1, TEST_BANNER)
    lines.append(f"Disposition: {disposition}")
    if watched:
        lines.append(f"Watched file: {watched.get('name')}")
        if watched.get("sha256"):
            lines.append(f"sha256 (registry-recorded): {watched['sha256']}")
        if watched.get("version_label"):
            lines.append(f"Version: {watched['version_label']}")
    if text.strip():
        lines += ["", "Operator text (verbatim):", "", text.strip()]
    name = _test_name(row, "feedback", stamp) if test else f"ui-feedback-row{row:02d}-{stamp}.md"
    return _write(name, "\n".join(lines) + _footer(author, verdict_kind=True))


def write_frame_note(row: int, file_name: str, sha256: str | None, frame: int | None,
                     timecode: str, text: str, images: list[str],
                     author: dict[str, Any] | None = None, test: bool = False) -> str:
    stamp = utc_stamp()
    lines = _head(f"# ui-frame-note — row {row:02d} — {utc_iso()}", author)
    if test:
        lines[0] = f"# ui-smoketest — row {row:02d} — frame note — {utc_iso()}"
        lines.insert(1, TEST_BANNER)
    lines.append(f"File: {file_name}")
    if sha256:
        lines.append(f"sha256 (registry-recorded): {sha256}")
    if frame is not None:
        lines.append(f"Frame: f{frame} @ {timecode}")
    elif timecode:
        lines.append(f"Timecode: {timecode}")
    if text.strip():
        lines += ["", "Operator note (verbatim):", "", text.strip()]
    if images:
        lines += ["", "Attached images (open these — they carry the operator's visual reference):"]
        lines += [f"- {p}" for p in images]
    name = _test_name(row, "frame-note", stamp) if test else f"ui-frame-note-row{row:02d}-{stamp}.md"
    return _write(name, "\n".join(lines) + _footer(author))


def write_batch_verdict(row: int, items: list[dict[str, Any]], note: str = "",
                        author: dict[str, Any] | None = None) -> str:
    stamp = utc_stamp()
    lines = _head(f"# ui-batch-verdict — row {row:02d} — {utc_iso()}", author, verdict_kind=True)
    for item in items:
        verdict = str(item.get("verdict", "")).upper()
        line = f"- variant {int(item['variant']):02d}: {verdict}"
        if item.get("name"):
            line += f" — {item['name']}"
        lines.append(line)
        if item.get("path"):
            lines.append(f"  file: {item['path']}")
        if item.get("sha256"):
            lines.append(f"  sha256: {item['sha256']}")
        if str(item.get("note") or "").strip():
            lines.append(f"  note: {str(item['note']).strip()}")
    if note.strip():
        lines += ["", "Batch note (verbatim):", "", note.strip()]
    return _write(f"ui-batch-verdict-row{row:02d}-{stamp}.md",
                  "\n".join(lines) + _footer(author, verdict_kind=True))


def write_continue(author: dict[str, Any] | None = None) -> tuple[str | None, str | None]:
    """Returns (path, error). Rate-limited: one per 10 minutes."""
    global _continue_last
    with _continue_lock:
        now = time.monotonic()
        if now - _continue_last < CONTINUE_MIN_INTERVAL:
            wait = int(CONTINUE_MIN_INTERVAL - (now - _continue_last))
            return None, f"Continue was already sent — next one allowed in {wait}s"
        _continue_last = now
    lines = _head(f"# ui-continue — {utc_iso()}", author)
    lines += [
        "Operator pressed Continue in Reel Deck. Equivalent to typing "
        '"continue from where you stopped" in chat.',
    ]
    return _write(f"ui-continue-{utc_stamp()}.md", "\n".join(lines) + _footer(author)), None


def save_upload(filename: str, data: bytes) -> tuple[str | None, str | None]:
    """Store a frame-note image attachment. Returns (path, error).

    Magic-byte sniffed (jpg/png only), 15 MB cap — same contract the pilot ran.
    """
    if len(data) > 15 * 1024 * 1024:
        return None, "image larger than 15 MB"
    if data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    else:
        return None, "not a JPEG or PNG (magic bytes)"
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(filename or "img")[0])[:60]
    path = UPLOADS_DIR / f"{utc_stamp()}-{safe}{ext}"
    path.write_bytes(data)
    return str(path), None


# ---------------------------------------------------------------------------
# Reading the bus back (status: was my receipt picked up? what just happened?)
# ---------------------------------------------------------------------------


def receipt_status(path: str) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"exists": False, "acks": [], "routed": False}
    acks = [{"lane": m.group(1), "at": m.group(2)} for m in _ACK_RE.finditer(text)]
    return {"exists": True, "acks": acks, "routed": bool(_ROUTED_RE.search(text))}


# The three states a send can be in, from the reviewer's chair. "received" needs
# an ACK line from a factory lane; "waiting" is younger than WAIT_S with no ACK;
# "no_answer" is older than WAIT_S with no ACK — the factory has gone quiet.
WAIT_S = 300.0
_SENT_BY_RE = re.compile(r"^Sent by:\s*(\S+)\s*\((\w+)\)", re.MULTILINE)
_DISP_RE = re.compile(r"^Disposition:\s*(\w+)", re.MULTILINE)
_FRAME_RE = re.compile(r"^Frame:\s*(.+)$", re.MULTILINE)
_TEXT_RE = re.compile(r"(?:Operator text \(verbatim\)|Operator note \(verbatim\)):\s*\n+\s*(.+?)(?:\n---|\Z)", re.DOTALL)
_RULING_RE = re.compile(r"RULING on (.+?):", re.MULTILINE)
_FINISHED_RE = re.compile(r"—\s*finished\b|finished clean|^DELIVERED|\bdelivered\b(?! *: *nothing)|\bshipped\b|marked approved|approved in the registry", re.IGNORECASE | re.MULTILINE)
_NOT_FINISHED_RE = re.compile(r"nothing delivered|not delivered|BLOCKED|failed audit|CALL —", re.IGNORECASE)
_ROW_NAME_RE = re.compile(r"^ui-(feedback|frame-note|batch-verdict|smoketest)-row(\d{2})-", re.IGNORECASE)


FACTORY_STATUS_PATH = settings.DECK_HOME / "factory_status.json"


def _factory_working_receipt() -> str | None:
    """The receipt the factory says it is working on right now (state=running), if fresh."""
    try:
        data = json.loads(FACTORY_STATUS_PATH.read_text(encoding="utf-8"))
        if data.get("state") == "running" and data.get("receipt") and (time.time() - float(data.get("alive_at") or 0)) < 65 * 60:
            from . import board as _board   # local import: board imports receipts
            if _board.worker_alive():
                return str(data["receipt"])
    except (OSError, ValueError, TypeError):
        pass
    return None


_NAME_TS_RE = re.compile(r"(\d{8})T(\d{6})Z")


def _name_time_iso(name: str) -> str | None:
    """When the reviewer SENT it = the stamp in the file name. The file's mtime moves every
    time the factory appends an ACK, which would make an old note read "1 min ago" and
    break newest-first sorting."""
    m = _NAME_TS_RE.search(name)
    if not m:
        return None
    d, t = m.group(1), m.group(2)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}Z"


_STATUS_RE = re.compile(r"^<!--status (\{.*\}) -->\s*$")
_STATUS_OPEN = ("picked_up", "running", "progress", "queued", "blocked")


def _status_blocks(text: str) -> list[dict[str, Any]] | None:
    """The machine-readable blocks tools/busline.py writes under each ACK/CALL line.
    Returned ONLY when the newest ACK/CALL line carries one — a hand-written line after the last
    block means prose is the newest truth and the regex fallback below decides (that is also how
    older receipts written without status blocks keep working, with no backfill)."""
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    last_ack = last_block = -1
    for i, ln in enumerate(lines):
        if ln.startswith(("ACK", "CALL")):
            last_ack = i
        else:
            m = _STATUS_RE.match(ln)
            if m and i == last_ack + 1:
                try:
                    b = json.loads(m.group(1))
                except ValueError:
                    continue
                if isinstance(b, dict) and b.get("state"):
                    blocks.append(b); last_block = i
    if last_ack < 0 or last_block != last_ack + 1:
        return None
    return blocks


def _is_finished(text: str) -> bool:
    blocks = _status_blocks(text)
    if blocks:
        return blocks[-1]["state"] == "finished"
    return _is_finished_prose(text)


def _is_finished_prose(text: str) -> bool:
    """Only the LAST ACK/CALL line decides: a 'BLOCKED … nothing delivered' ACK is not done
    (otherwise a reject can sit for days behind a green tick)."""
    lines = [ln for ln in text.splitlines() if ln.startswith(("ACK", "CALL"))]
    if not lines:
        return False
    last = lines[-1]
    if _NOT_FINISHED_RE.search(last):
        return False
    if re.search(r"—\s*picked up\b", last, re.IGNORECASE) and not re.search(r"—\s*finished\b", last, re.IGNORECASE):
        return False   # "picked up … (delivered 25 min ago)" is a start, not an end
    return bool(_FINISHED_RE.search(last))


_PICKUP_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
_QUEUED_RE = re.compile(r"\bBLOCKED\b|\bqueued\b|\bretry\b|not_before|waiting on disk|under the .* floor", re.IGNORECASE)
_QUEUED_WHY_RE = re.compile(r"(disk[^.;,]*|floor[^.;,]*|usage limit[^.;,]*|API error[^.;,]*|retry[^.;,]*)", re.IGNORECASE)
PICKUP_TRUST_S = 3 * 3600


def _pickup_state(text: str) -> dict[str, Any] | None:
    """{lane, at_epoch} when the LAST ACK/CALL line on a receipt is a pickup that has not been
    finished (and is not a CALL). Any lane counts, not just the factory worker: if another session
    picks up a note while the factory sleeps, the board must not still say READY FOR YOU TO WATCH /
    0 in the machine."""
    import calendar as _cal
    blocks = _status_blocks(text)
    if blocks:
        cur = blocks[-1]
        if cur["state"] not in _STATUS_OPEN:
            return None                      # finished or called: nobody is working it
        # "N min in" counts from the START of this run of open states, so a progress line no longer
        # restarts the clock, and a queued/blocked state reports its own reason verbatim
        start = cur
        for b in reversed(blocks):
            if b["state"] not in _STATUS_OPEN:
                break
            start = b
        try:
            at = _cal.timegm(time.strptime(str(start.get("at")), "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            return None
        # A "progress" line is commentary: it never changes whether the work is moving. A worker that
        # writes blocked (disk floor) and then a progress note must not read "FIXING · N min in" for a
        # round that has not started. The effective state is the newest NON-progress block in this run.
        eff = cur
        for b in reversed(blocks):
            if b["state"] not in _STATUS_OPEN:
                break
            if b["state"] != "progress":
                eff = b
                break
        queued = eff["state"] in ("queued", "blocked")
        # "N min in" must measure the WORK, not the wait: a round that spent hours blocked on the disk
        # floor and then relaunched must not count the blocked time.
        # work_at = start of the current unbroken stretch of actually-working states.
        work = None
        for b in reversed(blocks):
            if b["state"] not in _STATUS_OPEN or b["state"] in ("queued", "blocked"):
                break
            work = b
        try:
            work_at = _cal.timegm(time.strptime(str(work.get("at")), "%Y-%m-%dT%H:%M:%SZ")) if work else at
        except ValueError:
            work_at = at
        return {"lane": str(cur.get("lane") or "").strip(), "at_epoch": at, "work_at_epoch": work_at, "queued": queued,
                "queued_why": (str(eff.get("why"))[:120] if queued and eff.get("why") else None),
                "not_before": eff.get("not_before"), "structured": True}
    lines = [ln for ln in text.splitlines() if ln.startswith(("ACK", "CALL"))]
    if not lines or lines[-1].startswith("CALL") or _is_finished(text):
        return None
    last = lines[-1]
    if "smoketest" in last or "deck-bus" in last:
        return None
    m = _ACK_RE.match(last)
    ts = _PICKUP_TS_RE.search(last)
    # a pickup that says BLOCKED / queued / retry is not "fixing" — a lane parked on the disk floor
    # must not read "FIXING · N min in". Report it as queued.
    queued = bool(_QUEUED_RE.search(last))
    if not m or not ts:
        return None
    import calendar
    try:
        at = calendar.timegm(time.strptime(ts.group(1), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None
    return {"lane": m.group(1).strip(), "at_epoch": at, "queued": queued,
            "queued_why": (_QUEUED_WHY_RE.search(last).group(0)[:80] if queued and _QUEUED_WHY_RE.search(last) else None)}


def picked_up(row: int, now: float | None = None) -> dict[str, Any] | None:
    """The newest operator receipt for `row` that some lane has picked up and not finished,
    within PICKUP_TRUST_S of the pickup. None otherwise."""
    now = now or time.time()
    best = None
    try:
        entries = [e for e in os.scandir(BUS_DIR) if e.is_file() and e.name.endswith(".md")]
    except OSError:
        return None
    for e in entries:
        m = _ROW_NAME_RE.match(e.name)
        if not m or int(m.group(2)) != row or m.group(1).lower() == "smoketest":
            continue
        try:
            text = Path(e.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        st = _pickup_state(text)
        # trust is judged on the CURRENT working stretch: a round that sat 3 h on the disk floor and then
        # relaunched is freshly working, and judging it by the original pickup would drop it out of
        # trust entirely.
        if st and now - st.get("work_at_epoch", st["at_epoch"]) < PICKUP_TRUST_S:
            if best is None or st["at_epoch"] > best["at_epoch"]:
                best = dict(st, receipt=e.name, since_s=now - st["at_epoch"],
                            work_since_s=now - st.get("work_at_epoch", st["at_epoch"]))
    return best


def row_sends(row: int, limit: int = 200, now: float | None = None) -> list[dict[str, Any]]:
    working = _factory_working_receipt()
    """What this reviewer (any reviewer) sent about a row, newest first, with the
    factory's state for each — the panel that makes a send visible after SENT ✓."""
    now = now or time.time()
    out: list[dict[str, Any]] = []
    try:
        entries = [e for e in os.scandir(BUS_DIR) if e.is_file() and e.name.endswith(".md")]
    except OSError:
        return out
    mine = []
    for e in entries:
        m = _ROW_NAME_RE.match(e.name)
        if m and int(m.group(2)) == row:
            mine.append((e, m.group(1).lower()))
    # newest SENT first (file-name stamp, not mtime — mtime moves when the factory appends).
    mine.sort(key=lambda t: (_name_time_iso(t[0].name) or "", t[0].name), reverse=True)
    # Every real send shows (up to `limit`); plumbing tests are capped so 50 walk receipts
    # cannot push a reviewer's own words off the thread.
    real = [t for t in mine if t[1] != "smoketest"][:limit]
    tests = [t for t in mine if t[1] == "smoketest"][:8]
    chosen = sorted(real + tests, key=lambda t: (_name_time_iso(t[0].name) or "", t[0].name), reverse=True)
    for e, kind in chosen:
        try:
            text = Path(e.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        status = receipt_status(e.path)
        sent_iso = _name_time_iso(e.name)
        try:
            import calendar
            sent_epoch = calendar.timegm(time.strptime(sent_iso, "%Y-%m-%dT%H:%M:%SZ")) if sent_iso else e.stat().st_mtime
        except (ValueError, OSError):
            sent_epoch = e.stat().st_mtime
        age = now - sent_epoch
        last_line = next((ln for ln in reversed(text.splitlines()) if ln.startswith(("ACK", "CALL"))), "")
        if last_line.startswith("CALL"):
            state = "call"             # the factory stopped and needs the reviewer's ruling on THIS
        elif status["acks"] and _pickup_state(text) and (now - _pickup_state(text)["at_epoch"]) < PICKUP_TRUST_S:
            state = "working"          # a lane ACKed "picked up" and has not finished yet
        elif status["acks"]:
            state = "received"
        elif e.name == working:
            state = "working"          # picked up: the factory's heartbeat names this receipt
        elif age < WAIT_S:
            state = "waiting"
        else:
            state = "no_answer"
        who = _SENT_BY_RE.search(text)
        disp = _DISP_RE.search(text)
        body = _TEXT_RE.search(text)
        ruling = _RULING_RE.search(text)
        frame = _FRAME_RE.search(text)
        is_test = kind == "smoketest"
        if is_test:
            kind = "frame-note" if "frame note" in text[:200].lower() else "feedback"
        out.append({
            "name": e.name,
            "kind": kind,
            "test": is_test,
            "disposition": disp.group(1).upper() if disp else None,
            "answers_call": _ruling_target(text),
            "frame": frame.group(1).strip() if frame else None,
            "text": _scrub(body.group(1).strip()[:220] if body else ""),
            "sent_by": who.group(1) if who else None,
            "role": who.group(2).upper() if who else None,
            "sent_at_utc": _name_time_iso(e.name) or utc_iso(e.stat().st_mtime),
            "age_s": round(age, 1),
            "state": state,
            "finished": _is_finished(text) if status["acks"] else False,
            "acks": status["acks"],
            # the factory's own words back (ACK / CALL lines, newest last) — shown as replies
            "ack_lines": [ln.strip()[:4000] for ln in text.splitlines() if ln.startswith(("ACK", "CALL"))][-6:],
            "timecode_s": _frame_seconds(frame.group(1)) if frame else None,
            "text_full": _scrub(body.group(1).strip()[:1200] if body else ""),
        })
    return out


_TC_RE = re.compile(r"@\s*(\d+):(\d{2}(?:\.\d+)?)")


def _frame_seconds(frame_line: str) -> float | None:
    """'f34 @ 0:01.435 — file.mp4' -> 1.435 (seconds on the reference clock)."""
    m = _TC_RE.search(frame_line)
    if not m:
        return None
    try:
        return int(m.group(1)) * 60 + float(m.group(2))
    except ValueError:
        return None


_RECEIPT_NAME_RE = re.compile(r"ui-[a-z-]+-row\d{2,3}-\d{8}T\d{6}Z\.md", re.IGNORECASE)


def _ruling_target(text: str) -> str | None:
    """Which receipt a ruling answers. The Deck writes `RULING on <receipt>.md: …`; a ruling typed
    by a lane may read `RULING on the v008 CALL (ui-feedback-row07-….md, CALL …): …`, where a naive
    capture-up-to-the-first-colon returns a sentence and leaves the answered CALL open. Take the
    first receipt NAME after the words instead."""
    m = re.search(r"RULING on\b(.{0,300})", text, re.DOTALL)
    if not m:
        return None
    name = _RECEIPT_NAME_RE.search(m.group(1))
    if name:
        return name.group(0)
    first = _RULING_RE.search(text)
    return first.group(1).strip() if first else None


def _call_epoch(line: str) -> float:
    import calendar
    m = _PICKUP_TS_RE.search(line)
    try:
        return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ"))) if m else 0.0
    except ValueError:
        return 0.0


def bus_calls(row: int) -> list[dict[str, Any]]:
    """Open questions the factory wrote INTO receipts (a `CALL —` line last, with no later
    `RULING on <receipt>`), so they reach the board and the thread — the registry's open_calls
    array alone misses most of them."""
    sends = row_sends(row, limit=100000)
    answered = {s_["answers_call"] for s_ in sends if s_["answers_call"] and not s_["test"]}
    # a CALL is also moot once a lane has FINISHED newer work on the same row: the question was
    # overtaken whether or not anyone typed the magic words
    finished_after = 0.0
    for s_ in sends:
        if not s_["test"]:
            for ln in s_["ack_lines"]:
                if ln.startswith("ACK") and re.search(r"—\s*finished\b", ln, re.IGNORECASE):
                    finished_after = max(finished_after, _call_epoch(ln))
    out = []
    for s_ in sends:
        if s_["test"] or s_["state"] != "call" or s_["name"] in answered:
            continue
        last = next((ln for ln in reversed(s_["ack_lines"]) if ln.startswith("CALL")), "")
        if finished_after and _call_epoch(last) and finished_after > _call_epoch(last):
            continue
        m = re.match(r"^CALL\s+—\s+(.+?)\s+—\s+(\S+)\s+—\s+(.*)$", last)
        out.append({"call_id": s_["name"], "severity": "DECISION", "state": "AWAITING_OPERATOR",
                    "one_liner": (m.group(3) if m else last)[:1500], "raised_at_utc": m.group(2) if m else None,
                    "about": s_["text"], "source": "bus"})
    return out


def drop_calls(shortcode: str) -> list[dict[str, Any]]:
    """Open CALLs the factory wrote on a ui-DROP receipt (e.g. the reference could not be fetched).
    A drop has no row number in its file name, so bus_calls(row) can never see it and the card would
    say "the factory has a question" with no question on it. Matched by the reference shortcode;
    open while the receipt's newest non-progress state is `called` (prose fallback: last line is a CALL)."""
    if not shortcode:
        return []
    out: list[dict[str, Any]] = []
    try:
        paths = sorted(BUS_DIR.glob("ui-drop-*.md"), reverse=True)
    except OSError:
        return out
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f"/{shortcode}" not in text:
            continue
        lines = [ln for ln in text.splitlines() if ln.startswith(("ACK", "CALL"))]
        if not lines:
            continue
        real = [b for b in (_status_blocks(text) or []) if b.get("state") != "progress"]
        is_call = (real[-1].get("state") == "called") if real else lines[-1].startswith("CALL")
        if not is_call:
            continue
        last = next((ln for ln in reversed(lines) if ln.startswith("CALL")), "")
        m = re.match(r"^CALL\s+—\s+(.+?)\s+—\s+(\S+)\s+—\s+(.*)$", last)
        out.append({"call_id": path.name, "severity": "DECISION", "state": "AWAITING_OPERATOR",
                    "one_liner": (m.group(3) if m else last)[:1500], "raised_at_utc": m.group(2) if m else None,
                    "about": "your dropped reference", "source": "bus"})
    return out


def answered_calls(row: int) -> dict[str, str]:
    """call_id -> receipt name for every REAL (non-test) ruling sent on this row."""
    out: dict[str, str] = {}
    for send in row_sends(row, limit=100000):   # every REAL ruling ever; smoketests are skipped below
        if send["answers_call"] and not send["test"]:
            out.setdefault(send["answers_call"], send["name"])
    return out


def list_ui_receipts(limit: int = 40) -> list[dict[str, Any]]:
    """Newest-first ui-* receipts with their ACK state."""
    out: list[dict[str, Any]] = []
    try:
        entries = [
            e for e in os.scandir(BUS_DIR)
            if e.is_file() and e.name.startswith("ui-") and e.name.endswith(".md")
        ]
    except OSError:
        return out
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    for entry in entries[:limit]:
        status = receipt_status(entry.path)
        out.append({
            "name": entry.name,
            "mtime_utc": utc_iso(entry.stat().st_mtime),
            "acked": bool(status["acks"]),
            "acks": status["acks"],
            "routed": status["routed"],
        })
    return out


def recent_bus_activity(limit: int = 60) -> list[dict[str, Any]]:
    """Newest-first receipts of ANY kind — the 'what is the factory doing' feed."""
    out: list[dict[str, Any]] = []
    try:
        entries = [e for e in os.scandir(BUS_DIR) if e.is_file() and e.name.endswith(".md")]
    except OSError:
        return out
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    for entry in entries[:limit]:
        out.append({"name": entry.name, "mtime_utc": utc_iso(entry.stat().st_mtime)})
    return out


UNANSWERED_LOOKBACK_S = 48 * 3600.0


def unanswered(now: float | None = None) -> list[dict[str, Any]]:
    """Orders nobody has picked up: a row receipt older than WAIT_S with no ACK and no CALL under
    it (so an unread order is loud, not a small badge on the row page). Oldest first. Plumbing tests and
    anything older than UNANSWERED_LOOKBACK_S are left out."""
    import calendar
    now = now or time.time()
    out: list[dict[str, Any]] = []
    try:
        entries = [e for e in os.scandir(BUS_DIR) if e.is_file() and e.name.endswith(".md")]
    except OSError:
        return out
    for e in entries:
        m = _ROW_NAME_RE.match(e.name)
        if not m or m.group(1).lower() == "smoketest":
            continue
        iso = _name_time_iso(e.name)
        try:
            sent = calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) if iso else e.stat().st_mtime
        except (ValueError, OSError):
            continue
        age = now - sent
        if age < WAIT_S or age > UNANSWERED_LOOKBACK_S:
            continue
        try:
            text = Path(e.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(ln.startswith(("ACK", "CALL")) for ln in text.splitlines()):
            continue
        out.append({"row": int(m.group(2)), "receipt": e.name, "age_s": age})
    return sorted(out, key=lambda d: -d["age_s"])


# ---------------------------------------------------------------------------
# Open questions — the factory stopped and asked something (board strip + row page).
# ONE parser and ONE "is it still open" rule, used by both pages.
# ---------------------------------------------------------------------------

# Option markers the lanes actually write, tried in this order; the first family that yields a clean
# 1,2,3… (or a,b,c…) run of two or more non-empty options wins.
_OPTION_FAMILIES: list[tuple[str, re.Pattern[str]]] = [
    ("eq", re.compile(r"(?<![\w.])(?:RULING\s+)?(\d{1,2})\s*=\s*", re.IGNORECASE)),       # 1 = … / RULING 1 = …
    ("dot", re.compile(r"(?:(?<=\s)|^)(\d{1,2})\.\s+(?=\S)")),                               # 1. …
    ("paren", re.compile(r"\(([a-hA-H])\)\s*")),                                             # (a) …
]
_LABEL_STOP_RE = re.compile(r"(?<=[^\s\d])[.;!?](?=\s|$)|\n")
_SENTENCE_RE = re.compile(r"(?<=[.!])\s+(?=[A-Z0-9(\"'])")
QUESTION_SHORT = 220


def _seq_value(raw: str) -> int:
    return int(raw) if raw.isdigit() else ord(raw.lower()) - ord("a") + 1


def _clean_label(text: str) -> str:
    stop = _LABEL_STOP_RE.search(text)
    if stop:
        text = text[:stop.start()]
    text = re.sub(r"[\s,;:—–-]+(?:or|and)?[\s,;:—–-]*$", "", text.strip(), flags=re.IGNORECASE)
    text = text.strip(" \t,;:—–-")
    return text[:140].rstrip() + ("…" if len(text) > 140 else "")


def parse_call_options(text: str) -> tuple[str, list[dict[str, str]]]:
    """(text before the first option marker, [{n, label}]) for a CALL line's words. No clean run of
    two or more options → (whole text, [])."""
    text = str(text or "")
    for _family, pattern in _OPTION_FAMILIES:
        run: list[re.Match[str]] = []
        for m in pattern.finditer(text):
            want = len(run) + 1
            if _seq_value(m.group(1)) == want:
                run.append(m)
            elif not run:
                continue
        if len(run) < 2:
            continue
        options = []
        for i, m in enumerate(run):
            end = run[i + 1].start() if i + 1 < len(run) else len(text)
            label = _clean_label(text[m.end():end])
            if not label:
                options = []
                break
            options.append({"n": m.group(1), "label": label})
        if len(options) >= 2:
            return text[:run[0].start()], options
    return text, []


def question_sentence(prefix: str, has_options: bool = True) -> str:
    """The CALL's words up to the first option, said as ONE question that ends with '?'."""
    p = re.sub(r"\s+", " ", str(prefix or "")).strip(" \t—–-:;,(")
    if not p:
        return "Which one?" if has_options else "What's your call?"
    if "?" in p:
        i = p.index("?")
        starts = [m.end() for m in _SENTENCE_RE.finditer(p[:i])]
        return p[(starts[-1] if starts else 0):i + 1].strip()
    first = _SENTENCE_RE.split(p, maxsplit=1)[0].rstrip(" .!;:,—–-")
    return f"{first} — {'which one?' if has_options else 'what is your call?'}"


def _short(text: str, limit: int = QUESTION_SHORT) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 10].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:—–-") + " …"


_CLOSE_LINE_RE = re.compile(r"—\s*(?:finished|blocked|picked up|running)\b|\bBLOCKED\b", re.IGNORECASE)
_CLOSE_STATES = ("finished", "blocked", "picked_up", "running", "queued")
_CALL_LINE_RE = re.compile(r"^CALL\s+[—–-]+\s+(.+?)\s+[—–-]+\s+(\S+)\s+[—–-]+\s+(.*)$")


def _iso_epoch(iso: str | None) -> float | None:
    import calendar
    if not iso:
        return None
    m = _PICKUP_TS_RE.search(str(iso))
    if not m:
        return None
    try:
        return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _open_call_on(text: str) -> dict[str, Any] | None:
    """The newest CALL line on one receipt when nothing after it closes it: a later finished / blocked /
    picked-up (or running) ACK line or status block. A progress note does not close a question."""
    lines = text.splitlines()
    idx = max((i for i, ln in enumerate(lines) if ln.startswith("CALL")), default=-1)
    if idx < 0:
        return None
    for ln in lines[idx + 1:]:
        if ln.startswith(("ACK", "CALL")) and _CLOSE_LINE_RE.search(ln):
            return None
        st = _STATUS_RE.match(ln)
        if st:
            try:
                if json.loads(st.group(1)).get("state") in _CLOSE_STATES:
                    return None
            except ValueError:
                pass
    m = _CALL_LINE_RE.match(lines[idx].strip())
    return {"lane": m.group(1) if m else "", "raised_at_utc": m.group(2) if m else None,
            "words": (m.group(3) if m else re.sub(r"^CALL\s+[—–-]+\s*", "", lines[idx].strip())).strip()}


def open_question(row: int, extra_calls: list[dict[str, Any]] | None = None,
                  now: float | None = None) -> dict[str, Any] | None:
    """The newest OPEN factory question on a row, ready to render, or None.

    Candidates: a CALL line on this row's bus receipts with nothing closing it after it, plus
    `extra_calls` (registry open_calls already filtered of RESOLVED/CLOSED/SUPERSEDED, drop-receipt
    calls). A candidate is closed by a REAL ruling naming it, by newer finished work on the row, or by
    any later non-test operator ui-feedback / ui-frame-note receipt for the row (the operator already
    spoke after the question was raised)."""
    import calendar
    try:
        entries = [e for e in os.scandir(BUS_DIR) if e.is_file() and e.name.endswith(".md")]
    except OSError:
        entries = []
    candidates: list[dict[str, Any]] = []
    answered: set[str] = set()
    operator_sends: list[tuple[float, str]] = []
    finished_after = 0.0
    own_names: set[str] = set()
    for e in entries:
        m = _ROW_NAME_RE.match(e.name)
        if not m or int(m.group(2)) != row or m.group(1).lower() == "smoketest":
            continue
        own_names.add(e.name)
        try:
            text = Path(e.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        target = _ruling_target(text)
        if target:
            answered.add(target)
        for ln in text.splitlines():
            if ln.startswith("ACK") and re.search(r"—\s*finished\b", ln, re.IGNORECASE):
                finished_after = max(finished_after, _call_epoch(ln))
        who = _SENT_BY_RE.search(text)
        role = who.group(2).upper() if who else None
        if m.group(1).lower() in ("feedback", "frame-note") and role in (None, "OPERATOR"):
            iso = _name_time_iso(e.name)
            try:
                sent = float(calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))) if iso else e.stat().st_mtime
            except (ValueError, OSError):
                sent = 0.0
            operator_sends.append((sent, e.name))
        call = _open_call_on(text)
        if call:
            candidates.append({"call_id": e.name, "words": call["words"], "lane": call["lane"],
                               "raised_at_utc": call["raised_at_utc"],
                               "epoch": _iso_epoch(call["raised_at_utc"]) or _iso_epoch(_name_time_iso(e.name)),
                               "source": "bus"})
    for c in extra_calls or []:
        cid = str(c.get("call_id") or "")
        if not cid or cid in own_names:
            continue          # this row's own receipts were judged above, by the stricter bus rule
        raised = c.get("raised_at_utc") or c.get("opened_at_utc")
        candidates.append({"call_id": cid, "words": str(c.get("one_liner") or ""), "lane": "",
                           "raised_at_utc": raised, "epoch": _iso_epoch(raised),
                           "source": c.get("source") or "registry"})
    open_ones = []
    for c in candidates:
        if c["call_id"] in answered:
            continue
        ep = c["epoch"]
        if ep is not None:
            if finished_after and finished_after > ep:
                continue
            if any(sent > ep and name != c["call_id"] for sent, name in operator_sends):
                continue
        open_ones.append(c)
    if not open_ones:
        return None
    best = max(open_ones, key=lambda c: (c["epoch"] or 0.0, c["call_id"]))
    words = _scrub(re.sub(r"\s+", " ", best["words"]).strip())
    prefix, options = parse_call_options(words)
    question = question_sentence(prefix, bool(options))
    return {
        "row": row,
        "call_id": best["call_id"],
        "receipt": best["call_id"],
        "source": best["source"],
        "raised_at_utc": best["raised_at_utc"],
        "question": question,
        "question_short": _short(question),
        "full": words[:4000],
        "options": [dict(o, ruling=f"RULING on {best['call_id']}: {o['n']} — {o['label']}") for o in options],
        "ruling_prefix": f"RULING on {best['call_id']}: ",
    }
