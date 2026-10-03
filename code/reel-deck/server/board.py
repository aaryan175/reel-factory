"""
Board model — turns the registry into the desk the reviewer actually reads.

Four lanes: NEEDS YOU / IN THE MACHINE / DONE / PARKED. Era filter comes from
board.json ("current" sequences lead; everything else is the archive shelf).
All reads go through server.registry (read-only, cached, TCC-guarded).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import eta as eta_engine
from . import lanes
from . import registry as reg
from . import receipts as receipts_mod
from . import settings

BOARD_CONFIG = settings.DECK_HOME / "board.json"
FACTORY_STATUS = settings.DECK_HOME / "factory_status.json"


CONTROL_DIR = settings.CONTROL_DIR


def runner_slots() -> tuple[int, int]:
    """(busy, total) lane slots of the order runner, from its lock files in _control/
    (orderd.lock = slot 1, orderd.lane2.lock, orderd.lane3.lock). A lock that parses with a session
    is a lane at work; the runner deletes a lock when its lane is finalized."""
    control = CONTROL_DIR
    names = ["orderd.lock", "orderd.lane2.lock", "orderd.lane3.lock"]
    busy = 0
    for name in names:
        try:
            data = json.loads((control / name).read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("session"):
                busy += 1
        except (OSError, ValueError):
            continue
    return busy, len(names)


_BATCH_ORDER_RE = re.compile(r"alternate batch for registry row 0?(\d+)", re.IGNORECASE)


def batch_building_rows() -> set[int]:
    """Rows whose 10-variant batch a runner lane is building RIGHT NOW: a live lane lock in _control/ whose
    receipt carries a batch ORDER ("... alternate batch for registry row NN"). A batch drop has no row number in
    its file name, so nothing else on the board can tell which row it belongs to."""
    rows: set[int] = set()
    for name in ("orderd.lock", "orderd.lane2.lock", "orderd.lane3.lock"):
        try:
            data = json.loads((CONTROL_DIR / name).read_text(encoding="utf-8"))
            if not (isinstance(data, dict) and data.get("session") and data.get("receipt")):
                continue
            head = Path(data["receipt"]).read_text(encoding="utf-8", errors="replace")[:1500]
        except (OSError, ValueError):
            continue
        match = _BATCH_ORDER_RE.search(head)
        if match:
            rows.add(int(match.group(1)))
    return rows


def variant_batch_phrase(seq: int | None, batch: dict[str, Any], building: set[int],
                         now: float | None = None) -> tuple[str, str, str] | None:
    """(bucket, phrase, color) for a row whose new-style variant batch (deliver/variants/<stamp>/) needs saying:
    BUILDING while a lane renders it, READY once every planned variant is on disk and no keep/kill verdict has
    been sent since. None = say nothing (stale batches, judged batches, no batch)."""
    if seq is None or not batch.get("batch_stamp"):
        return None
    done = len(batch.get("variants") or [])
    expected = int(batch.get("expected") or 0) or done
    now = now or time.time()
    fresh = bool(batch.get("lane_mtime") and now - float(batch["lane_mtime"]) < 20 * 60)
    if seq in building or (done < expected and fresh):
        return "machine", f"BUILDING VARIANTS · {done} of {expected} done · watch them as they land", "amber"
    if done < expected:
        return "machine", f"VARIANTS STOPPED · {done} of {expected} done · nothing is building", "red"
    newest = max((os.path.getmtime(v["path"]) for v in batch["variants"] if os.path.exists(v["path"])), default=0)
    if now - newest > 7 * 86400:
        return None
    verdicts = sorted(BUS_DIR.glob(f"ui-batch-verdict-row{seq:02d}-*.md"))
    if verdicts and os.path.getmtime(verdicts[-1]) > newest:
        return None
    return "needs_you", f"{done} VARIANTS READY · watch, keep / kill, leave notes", "green"


def factory_status() -> dict[str, Any]:
    """The daemon's heartbeat — is the chat-free factory awake?"""
    try:
        data = json.loads(FACTORY_STATUS.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("factory status is not an object")
        # health = the last COMPLETED tick when the worker reports one (a
        # heartbeat written before the work proves nothing); alive_at is the fallback
        age = time.time() - float(data.get("last_tick_completed_at") or data.get("alive_at") or 0)
        state = data.get("state") or "unknown"
        if state == "limited":
            return {"awake": False, "state": "limited", "age_s": round(age, 1), "receipt": None,
                    "paused": bool(data.get("paused")), "retry_queue": data.get("retry_queue"), "last_error": None,
                    "limit_until": data.get("limit_until_text"), "launched_by": data.get("launched_by")}
        if state == "launching":
            # heal gives a launch 900 s before it restarts (LAUNCH_GRACE); the 8-min rule below read
            # a slow launch as "asleep". Past the grace it is a failed start.
            n = int(data.get("launch_count") or 1)
            failed = age >= 900.0
            return {"awake": not failed, "state": "launch-failed" if failed else "launching",
                    "age_s": round(age, 1), "receipt": None, "paused": bool(data.get("paused")),
                    "retry_queue": int(data.get("retry_queue") or 0), "last_error": _norm_error(data.get("last_error")),
                    "launch_count": n, "launched_by": data.get("launched_by")}
        # the interactive worker idles on a ~3.2 min tick and takes 20-40s per tick: a 180s
        # window flickers to 'asleep' between ticks. Awake = heard from inside 8 min.
        awake = age < 480.0
        # mid-build the worker deliberately goes quiet (no self-wake during a build); a
        # "running" heartbeat younger than its own 65-min fallback is a live build, not sleep
        if state == "running" and data.get("receipt") and age < 65 * 60 and worker_alive():
            awake = True
        return {"awake": awake, "state": state if awake else "asleep",
                "age_s": round(age, 1), "receipt": data.get("receipt"),
                "paused": bool(data.get("paused")),
                "retry_queue": int(data.get("retry_queue") or 0),
                "last_error": _norm_error(data.get("last_error"))}
    except (OSError, ValueError):
        return {"awake": False, "state": "asleep", "age_s": None,
                "receipt": None, "paused": False,
                "retry_queue": 0, "last_error": None}

# Sequences shown on the main board when board.json has no "current" list (empty = all rows
# at or above current_min_seq).
DEFAULT_CURRENT: list[int] = []

_ROWCALL_RE = re.compile(r"ROW[\s_-]?(\d{1,2})", re.IGNORECASE)

BUS_DIR = settings.BUS_DIR
_DROP_CODE_RE = re.compile(r"instagram\.com/(?:reel|reels|p)/([A-Za-z0-9_-]+)")
_DROP_ACK_RE = re.compile(r"^ACK\s+—\s+", re.MULTILINE)
_DROP_NOTE_RE = re.compile(r"Operator note \(verbatim\):\s*\n+\s*(.+)")


def pending_drops(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drops the reviewer submitted that have no registry row yet — shown as
    QUEUED cards the moment the receipt lands, so a submit is never invisible
    while the factory daemon walks over to pick it up."""
    known = {str(r.get("reference_shortcode") or "") for r in rows}
    out: list[dict[str, Any]] = []
    now = time.time()
    try:
        receipts = sorted(BUS_DIR.glob("ui-drop-*.md"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return out
    for path in receipts:
        try:
            stat = path.stat()
            if now - stat.st_mtime > 48 * 3600:
                break
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fresh = [c for c in _DROP_CODE_RE.findall(text) if c not in known]
        if not fresh:
            continue
        note = _DROP_NOTE_RE.search(text)
        state, say = _drop_state(text)
        out.append({
            "receipt": path.name,
            "shortcodes": fresh,
            "picked_up": bool(_DROP_ACK_RE.search(text)),
            # A drop that stops and raises a CALL must not keep saying "PICKED UP — lane starting".
            # A drop has no row yet, so its receipt is the only place the factory's question can be
            # seen: carry the receipt's effective state onto the card.
            "state": state,
            "say": say,
            "age_s": round(now - stat.st_mtime, 1),
            "note": note.group(1).strip()[:90] if note else "",
        })
    return out


_DROP_LINE_RE = re.compile(r"^(ACK|CALL)\s+—\s+\S+\s+—\s+\S+\s+—\s+(.*)$")


def _drop_state(text: str) -> tuple[str | None, str]:
    """(state, sentence) for a drop receipt: the newest NON-progress status block (tools/busline.py),
    else the newest ACK/CALL line's own words. state is one of the busline states or None."""
    lines = [ln for ln in text.splitlines() if ln.startswith(("ACK", "CALL"))]
    if not lines:
        return None, ""
    blocks = receipts_mod._status_blocks(text) or []
    real = [b for b in blocks if b.get("state") != "progress"]
    if real:
        b = real[-1]
        at = str(b.get("at") or "")
        line = next((ln for ln in reversed(lines) if at and at in ln), lines[-1])
        m = _DROP_LINE_RE.match(line)
        return str(b.get("state")), (str(b.get("why") or "") or (m.group(2) if m else ""))[:900]
    m = _DROP_LINE_RE.match(lines[-1])
    return ("called" if lines[-1].startswith("CALL") else "picked_up"), (m.group(2) if m else "")[:900]


def _norm_error(err: Any) -> dict[str, Any] | None:
    """The old watcher wrote {reason, receipt, detail, at_utc}; the interactive worker writes a sentence.
    A sentence is a NOTE (amber), never the red failure stamp, and words like queued/fixed/waiting are not errors."""
    if not err:
        return None
    if isinstance(err, dict):
        return {**err, "level": "red"}
    return {"reason": str(err)[:300], "receipt": "", "detail": str(err), "at_utc": "", "level": "amber"}


def board_config() -> dict[str, Any]:
    try:
        data = json.loads(BOARD_CONFIG.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"current": DEFAULT_CURRENT, "hide": []}


def open_calls(rows_payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Open operator calls from the registry's own open_calls array."""
    try:
        data = json.loads(Path(reg.REGISTRY_PATH).read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    calls = data.get("open_calls")
    if not isinstance(calls, list):
        return []
    out = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        state = str(call.get("state", "")).upper()
        if "RESOLVED" in state or "CLOSED" in state or "SUPERSEDED" in state:
            continue
        row = None
        blob = " ".join(str(call.get(k, "")) for k in ("call_id", "project", "one_liner"))
        match = _ROWCALL_RE.search(blob)
        if match:
            row = int(match.group(1))
        out.append({
            "call_id": call.get("call_id"),
            "severity": call.get("severity"),
            "state": state,
            "one_liner": call.get("one_liner"),
            "row": row,
            "opened_at_utc": call.get("opened_at_utc"),
        })
    return out


def _row_title(row: dict[str, Any]) -> str:
    hook = row.get("creative_hook")
    if isinstance(hook, str) and hook.strip():
        return hook.strip()
    return reg.row_shortcode(row) or "untitled"


def _phase_of_role(role: str | None) -> str | None:
    if not role:
        return None
    upper = role.upper()
    if "AUDIT" in upper:
        return "audit"
    if "FIX" in upper:
        return "fix_round"
    if "BUILD" in upper:
        return "build"
    if "STUDY" in upper:
        return "study"
    if "DELIVER" in upper:
        return "cardfix_deliver"
    return None


_WORKER_CHECK: dict[str, Any] = {"at": 0.0, "alive": False}


def worker_alive() -> bool:
    """Is a factory worker process alive? Workers are the agent sessions orderd launches with
    its lane settings file (`orderd/claude-settings.json`); override the process pattern with
    REEL_DECK_WORKER_PATTERN. The pid in factory_status.json is the short-lived snippet that
    wrote the file, so it proves nothing. Cached 15 s."""
    import subprocess
    now = time.time()
    if now - _WORKER_CHECK["at"] < 15:
        return bool(_WORKER_CHECK["alive"])
    alive = False
    try:
        pattern = os.environ.get("REEL_DECK_WORKER_PATTERN") or "orderd/claude-settings.json"
        out = subprocess.run(["/usr/bin/pgrep", "-f", pattern],
                             capture_output=True, text=True, timeout=3).stdout.split()
        alive = any(p.isdigit() for p in out)
    except (OSError, subprocess.SubprocessError):
        alive = False
    _WORKER_CHECK.update(at=now, alive=alive)
    return alive


def factory_working_row() -> dict[str, Any] | None:
    """{seq, receipt, since_s} when the factory heartbeat says state=running on a rowNN receipt."""
    # Read the raw heartbeat: while a build runs the worker deliberately does NOT re-tick
    # (a self-wake mid-build kills builds), so "awake" (8 min) goes false and the card would
    # fall back to NEEDS YOU in the middle of a real fix round.
    # A running receipt stays "working" for up to 65 min — the worker's own long fallback.
    try:
        raw = json.loads(FACTORY_STATUS.read_text(encoding="utf-8"))
        age = time.time() - float(raw.get("alive_at") or 0)
    except (OSError, ValueError, TypeError):
        return None
    f = {"state": raw.get("state"), "receipt": raw.get("receipt")}
    if not (f.get("state") == "running" and f.get("receipt") and age < 65 * 60 and worker_alive()):
        return None
    if "smoketest" in str(f["receipt"]):
        return None            # a plumbing test is never a build
    m = re.search(r"row(\d{2,3})", str(f["receipt"]))
    if not m:
        return None
    # "N min in" counts from the heartbeat that declared the build running (alive_at), not the
    # receipt's mtime (which moves whenever anyone appends to it).
    return {"seq": int(m.group(1)), "receipt": f["receipt"], "since_s": age}


def row_card(row: dict[str, Any], index: dict[str, list[str]],
             calls_by_row: dict[int, list[dict[str, Any]]],
             bus: dict[int, list[dict[str, Any]]] | None = None,
             fitted: dict[str, Any] | None = None,
             live_map: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    seq = reg.row_sequence(row)
    media = reg.row_media(row, index)
    plain = reg.row_plain_state(row, media, index_ready=bool(index))
    batch = reg.row_batch(row, index, media)
    lane = reg.row_lane(row)
    cold = reg.row_is_cold(row)
    calls = list(calls_by_row.get(seq or -1, []))
    if seq is not None:
        # a registry CALL the reviewer has already ruled on is not an open question any more
        ruled = receipts_mod.answered_calls(seq)
        calls = [c for c in calls if c.get("call_id") not in ruled]
        seen = {c.get("call_id") for c in calls}
        calls += [c for c in receipts_mod.bus_calls(seq) if c["call_id"] not in seen]
        # a CALL written on the DROP receipt that created this row (no row number in its name); only while
        # the registry still says the row is stopped on a call, so an answered one cannot linger
        if str(row.get("review_state") or "").upper().startswith("CALL_REQUIRED"):
            calls += receipts_mod.drop_calls(str(row.get("reference_shortcode") or ""))
    live = (live_map or {}).get(seq or -1)

    working = factory_working_row()
    working_here = bool(working and working["seq"] == seq)
    if not working_here and seq is not None:
        # any lane that ACKed "picked up" on this row's note (and has not finished) = in the machine
        pick = receipts_mod.picked_up(seq)
        if pick:
            # carry queued/queued_why through: rebuilding the dict from four keys drops them, the
            # QUEUED phrase below becomes unreachable, and a lane parked on the disk floor reads
            # "FIXING · N min in".
            working = {"seq": seq, "receipt": pick["receipt"], "since_s": pick.get("work_since_s", pick["since_s"]),
                       "lane": pick["lane"],
                       "queued": bool(pick.get("queued")), "queued_why": pick.get("queued_why")}
            working_here = True
    elif working_here:
        # the heartbeat says running on this row, but if the newest pickup ACK on that same
        # receipt says BLOCKED/queued, the receipt is the fresher truth
        pick = receipts_mod.picked_up(seq)
        if pick and pick.get("receipt") == working.get("receipt") and pick.get("queued"):
            working = dict(working, lane=pick.get("lane"), queued=True, queued_why=pick.get("queued_why"))
        elif pick and pick.get("receipt") == working.get("receipt"):
            # "N min in" = since the round's first pickup line on the receipt. alive_at moves every time the
            # worker refreshes its heartbeat, so a long build would read "3 min in".
            working = dict(working, since_s=max(float(working.get("since_s") or 0),
                                                 float(pick.get("work_since_s", pick.get("since_s")) or 0)))
    if working_here:
        # The factory is on this row's receipt right now: it is in the machine, and the
        # note that put it there answers any open CALL (otherwise the board says NEEDS YOU · CALL
        # while a fix round is already running).
        bucket = "machine"
        calls = []
    elif calls:
        bucket = "needs_you"
    elif cold:
        bucket = "parked"
    elif lane == reg.LANE_VERDICT:
        bucket = "needs_you"
    elif lane == reg.LANE_DONE:
        bucket = "done"
    else:
        bucket = "machine"

    # Only rows actually in the machine get a "review ready ~" estimate —
    # everything else is either already on the desk or not moving.
    estimate = None
    if bucket == "machine" and seq is not None:
        events = eta_engine.merge_events((bus or {}).get(seq) or [],
                                         eta_engine.registry_events(row))
        lane_mtime = batch.get("lane_mtime")
        if live and live.get("alive"):
            # A lane agent is appending to its transcript right now — its
            # heartbeat is a far better anchor than yesterday's bus receipt.
            heartbeat = live.get("started_at") or (time.time() - live["age_s"])
            phase = _phase_of_role(live.get("role"))
            if phase:
                events = eta_engine.merge_events(
                    events, [{"at": heartbeat, "phase": phase,
                              "source": f"live:{live['run_id']}", "fix_round": None}])
            lane_mtime = max(lane_mtime or 0.0, heartbeat)
        estimate = eta_engine.estimate(
            seq, row=row, lane_mtime=lane_mtime, events=events, fitted=fitted,
        )

    current = media.get("current")
    phrase, color = plain["phrase"], plain["color"]
    if any(c.get("source") == "bus" for c in calls):
        phrase, color = "FACTORY NEEDS YOUR CALL", "amber"
    if seq is not None and not working_here:
        # an order on this row that NOBODY has picked up outranks every "ready" phrase: the card must
        # not say "new version ready" while the note that rejects that version sits unread
        waiting = receipts_mod.unanswered()
        mine = [w for w in waiting if w["row"] == seq]
        if mine:
            mins = int(mine[0]['age_s'] // 60)
            busy, slots = runner_slots()
            if slots and busy >= slots:
                ahead = sum(1 for w in waiting if w["age_s"] > mine[0]["age_s"])
                place = "next in line" if ahead == 0 else f"{ahead} ahead of it"
                phrase, color = f"QUEUED · all {slots} lanes busy · {place} · {mins} min", "amber"
            else:
                phrase, color = f"SENT · NOT PICKED UP YET · {mins} min", "red"
    if working_here:
        mins = int((working.get("since_s") or 0) // 60)
        who = working.get("lane")
        who = "" if not who or who.startswith("reel-factory") else f" · {who}"
        if working.get("queued"):
            why = working.get("queued_why")
            phrase = f"QUEUED{who} — waiting" + (f" · {why}" if why else "") + " · nothing is building yet"
        else:
            # a row with no version yet is a FIRST build from a dropped reference, not a fix
            verb = "FIXING FROM YOUR NOTE" if (media.get("versions") or media.get("current")) else "BUILDING YOUR NEW REEL"
            phrase = (f"{verb}{who} · {mins} min in" if mins else f"{verb}{who}")
            fs = factory_status()
            if fs.get("state") == "limited":
                # a usage limit can pause the build mid-way; the card must not keep counting minutes
                # as if it were working. Say so, with the resume time the harness printed.
                until = fs.get("limit_until") or "the next window"
                phrase = f"PAUSED · usage limit, resumes {until} · {verb.lower()} keeps its place, {mins} min done"
        color = "amber"
    said = variant_batch_phrase(seq, batch, batch_building_rows())
    if said and not calls and not (working_here and said[0] == "needs_you"):
        bucket, phrase, color = said
    return {
        "working": working if working_here else None,
        "eta": estimate,
        "live": live,
        "seq": seq,
        "title": _row_title(row),
        "shortcode": reg.row_shortcode(row),
        "phrase": phrase,
        "color": color,
        "token": plain["token"],
        "state_sentence": plain["state"],
        "unplayable": bool(plain.get("unplayable")),
        "bucket": bucket,
        "versions": len(media.get("versions") or []),
        "current_name": current.get("name") if current else None,
        "current_version": (current or {}).get("version_label"),
        "variants_on_disk": len(batch.get("variants") or []),
        "variants_expected": batch.get("expected"),
        "lane_mtime_utc": batch.get("lane_mtime_utc"),
        "calls": calls,
    }


def build_board() -> dict[str, Any]:
    payload = reg.load_registry()
    rows = payload.get("rows") or []
    snapshot = reg.index_snapshot()
    index = snapshot["map"]
    config = board_config()
    current_set = set(config.get("current") or [])
    hide_set = set(config.get("hide") or [])
    # Any row newer than the configured era is automatically current — a fresh
    # reviewer drop must appear on the board the moment it is registered.
    current_min = config.get("current_min_seq", (max(current_set) + 1) if current_set else 0)

    calls = open_calls()
    calls_by_row: dict[int, list[dict[str, Any]]] = {}
    for call in calls:
        if call["row"] is not None:
            calls_by_row.setdefault(call["row"], []).append(call)

    bus = eta_engine.bus_events()
    fitted = eta_engine.model()
    live_map = lanes.live_rows()

    cards, archive = [], []
    for row in rows:
        seq = reg.row_sequence(row)
        if seq is None:
            # A row without a usable sequence can't be addressed, linked, or
            # formatted ('%02d' % None is a template 500) — skip it whole.
            continue
        if seq in hide_set:
            continue
        card = row_card(row, index, calls_by_row, bus=bus, fitted=fitted,
                        live_map=live_map)
        is_current = seq in current_set or (seq is not None and seq >= current_min)
        (cards if is_current else archive).append(card)

    def order(card: dict[str, Any]) -> tuple:
        rank = {"needs_you": 0, "machine": 1, "done": 2, "parked": 3}[card["bucket"]]
        return (rank, -(card["seq"] or 0))

    cards.sort(key=order)
    archive.sort(key=order)

    counts = {"needs_you": 0, "machine": 0, "done": 0, "parked": 0}
    for card in cards:
        counts[card["bucket"]] += 1

    pending = pending_drops(rows)
    counts["machine"] += len(pending)

    # The soonest thing coming back to the desk, so the board can say it once
    # at the top instead of making the reviewer read every card.
    estimates = [c["eta"] for c in cards if c.get("eta")]
    next_review = min(estimates, key=lambda e: e["eta_epoch"]) if estimates else None

    waiting = receipts_mod.unanswered()
    return {
        "questions": board_questions(cards + archive),
        "factory": factory_status(),
        "unanswered": {"count": len(waiting), "oldest_min": int(waiting[0]["age_s"] // 60) if waiting else 0,
                       "rows": sorted({w["row"] for w in waiting})},
        "pending": pending,
        "next_review": next_review,
        "cards": cards,
        "archive": archive,
        "counts": counts,
        "calls_unmatched": [c for c in calls if c["row"] is None],
        "index": {
            "state": snapshot["state"],
            "files": snapshot["files"],
            "age_s": snapshot["age_s"],
            "error": snapshot["error"],
        },
        "workbench": reg.workbench_status(),
        "registry": {
            "updated_at_utc": payload.get("updated_at_utc"),
            "error": payload.get("error"),
        },
    }


def card_question(card: dict[str, Any]) -> dict[str, Any] | None:
    """The newest open factory question for one board card (None while the factory is working the row:
    the note it is working from already answered it). One rule for the board strip and the row page."""
    if card.get("seq") is None or card.get("working"):
        return None
    q = receipts_mod.open_question(int(card["seq"]), extra_calls=card.get("calls") or [])
    if q:
        q["title"] = card.get("title")
    return q


def board_questions(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """QUESTIONS strip: one card per row with an open question, newest question first."""
    out = [q for q in (card_question(c) for c in cards) if q]
    return sorted(out, key=lambda q: (q.get("raised_at_utc") or "", q["row"]), reverse=True)


def row_detail(seq: int) -> dict[str, Any] | None:
    payload = reg.load_registry()
    rows = payload.get("rows") or []
    row = next((r for r in rows if reg.row_sequence(r) == seq), None)
    if row is None:
        return None
    snapshot = reg.index_snapshot()
    index = snapshot["map"]
    media = reg.row_media(row, index)
    plain = reg.row_plain_state(row, media, index_ready=snapshot["state"] != "COLD")
    batch = reg.row_batch(row, index, media)
    reference = reg.row_reference(row)

    docs: list[dict[str, Any]] = list(media.get("assets") or [])
    current = media.get("current")
    if current:
        for doc in reg.sibling_docs(current["path"]):
            if all(doc["path"] != d.get("path") for d in docs):
                docs.append(doc)

    # Newest batch-delivery Drive folder the row records — the place the final
    # films live even when this machine only holds pre-fix backups.
    batch_drive: dict[str, Any] | None = None
    for key in sorted(row.keys(), reverse=True):
        value = row.get(key)
        if "delivery" in key and isinstance(value, dict) and value.get("drive_folder_id"):
            # Linked only when REEL_DECK_DRIVE_FOLDER_URL (e.g. ".../folders/{folder_id}") is set.
            template = settings.DRIVE_FOLDER_URL_TEMPLATE
            if template:
                batch_drive = {
                    "key": key,
                    "folder_id": value["drive_folder_id"],
                    "folder_name": value.get("drive_folder_name"),
                    "url": template.replace("{folder_id}", str(value["drive_folder_id"])),
                }
            break

    # A lane that stopped on CALL_REQUIRED wrote its question at the foot of
    # its newest bus receipt under "## The call" — surface it verbatim.
    question = None
    token = str(row.get("review_state") or "")
    if "CALL" in token.upper():
        try:
            receipts = sorted(BUS_DIR.glob(f"reel{seq}-*.md"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
            for path in receipts[:4]:
                text = path.read_text(encoding="utf-8", errors="replace")
                match = re.search(r"##\s*The call\s*\n+(.*?)(?:\n##|\Z)", text, re.DOTALL)
                if match:
                    question = {"text": match.group(1).strip(), "receipt": path.name}
                    break
        except OSError:
            pass

    # A question the reviewer already answered (a real RULING receipt names its
    # call id) leaves the row; it comes back only if the factory asks again
    # under a new id. Test-mode answers never hide a real question.
    from . import receipts as receipts_mod
    answered = receipts_mod.answered_calls(seq)
    calls = [c for c in open_calls() if c["row"] == seq and str(c.get("call_id")) not in answered]
    if question and question["receipt"] in set(answered.values()) | set(answered):
        question = None
    live_card = row_card(row, index, {seq: calls} if calls else {},
                         bus=eta_engine.bus_events(), fitted=eta_engine.model(),
                         live_map=lanes.live_rows())
    return {
        "open_question": card_question(live_card),
        "sends": receipts_mod.row_sends(seq),
        "factory": factory_status(),
        "answered": answered,
        "question": question,
        "bucket": live_card["bucket"],
        "eta": live_card["eta"],
        "live": live_card["live"],
        "batch_drive": batch_drive,
        "seq": seq,
        "title": _row_title(row),
        "shortcode": reg.row_shortcode(row),
        "reference_url": reg.row_reference_url(row),
        "phrase": live_card["phrase"],
        "color": live_card["color"],
        "working": live_card.get("working"),
        "token": plain["token"],
        "state_sentence": plain["state"],
        "reference": reference,
        "versions": media.get("versions") or [],
        "comparisons": media.get("comparisons") or [],
        "docs": docs,
        "missing": media.get("missing") or [],
        "current": current,
        "batch": batch,
        "calls": [] if live_card.get("working") else (calls + [c for c in receipts_mod.bus_calls(seq) if c["call_id"] not in {x.get("call_id") for x in calls}]),   # the note being worked on answered it
        "index_state": snapshot["state"],
    }
