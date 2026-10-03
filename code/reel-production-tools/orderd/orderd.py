#!/usr/bin/env python3
"""orderd — the on-demand ORDER RUNNER for the reel factory.

One tick (launchd runs `orderd.py --once` every 60 s):
  1. If a lane is running (lock file): leave it alone until it is DONE, then finalize it. A tmux lane (the
     default) is done when (a) the order receipt carries an `orderd-lane` finished/blocked/called busline written
     after the lane started, (b) its tmux pane is back at a shell (claude exited), (c) its tmux session is gone,
     or (d) the 3 h wall hits (-> `blocked --why timeout`). Then orderd captures the pane into the lane log,
     runs `tmux kill-session`, and writes the final bus line (finished / called / blocked / queued-for-retry)
     from the lane's own busline, the result file and the pane text, then clears the lock.
  2. Backoff (usage limit), PAUSED flag (set by a human, or by orderd itself on a login / account problem),
     or the factory kill switch -> do nothing.
  3. Otherwise scan the bus for OPEN orders (a Deck `ui-*` receipt nobody has answered yet), pick ONE by the
     priority (REJECT feedback > other feedback / frame-notes / batch-verdicts > continue > drop,
     oldest first), write `picked_up` through tools/busline.py and spawn ONE lane.

LAUNCH SHAPE: a lane is the INTERACTIVE `claude` in its own tmux session:
`tmux new-session -d -s reel-lane-<utc> -c $REEL_FACTORY_HOME`, then send-keys
`claude --settings <settings> --permission-mode <mode> --model opus "$(/bin/cat <prompt file>)"`, where <mode>
is ORDERD_PERMISSION_MODE (default `acceptEdits`; never a mode that skips permission checks). Commands a lane
needs beyond file edits go in the settings file's `permissions.allow` list.
A headless `claude -p` launch under launchd can fail to refresh its login, so the interactive launch is the
default; the headless path stays behind `--headless` (default OFF). The claude line is sent into a shell (not
passed to new-session) because a pane started with a command reports its `sh -c` parent as pane_current_command
while claude runs, which would read as "claude exited" at once. Smoke receipts named
`ui-smoketest-orderd-*` are picked by the normal tick too (as smoke lanes), so a smoke proves the launchd context.

Hard limits: one lane at a time; 3 h wall per lane; an account refusal or a "Login expired" stop PAUSES the
runner (writes the PAUSED flag) and notifies the human with a CALL — orderd never types into a lane to get
past a login or subscription-access problem, and nothing launches until a human removes the flag; an order is never relaunched in a loop (a usage-limit/API-error death is retried at most
MAX_ATTEMPTS times, 30 min apart, then blocked). Every decision is logged to _receipts/orderd/orderd.log.

    orderd.py --once              one tick (tmux interactive lane)
    orderd.py --once --headless   one tick, lane launched as headless `claude -p` (old shape, off by default)
    orderd.py --once --dry-run    print the decision, spawn nothing, write nothing
    orderd.py --once --smoke      pick the newest un-handled ui-smoketest receipt and run the smoke lane
    orderd.py --status            print lock / backoff / open orders
    orderd.py --stop              kill the running lane (tmux session or process group), write `blocked --why stopped`
"""
from __future__ import annotations

import argparse
import calendar
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(HERE))
if str(TOOLS) not in sys.path:
    sys.path.insert(1, str(TOOLS))
import lane_prompt  # noqa: E402
import rf_paths  # noqa: E402

HOME = Path(os.path.expanduser("~"))
REEL = rf_paths.REEL_HOME


def _p(env: str, default: Path) -> Path:
    return Path(os.environ.get(env) or default)


BUS = _p("ORDERD_BUS", Path(os.environ.get("REEL_FACTORY_BUS_DIR") or (rf_paths.RECEIPTS / "bus")))
CONTROL = _p("ORDERD_CONTROL", REEL / "_control")
LOGDIR = _p("ORDERD_LOGDIR", REEL / "_receipts" / "orderd")
BUSLINE = _p("ORDERD_BUSLINE", TOOLS / "busline.py")
CLAUDE = _p("ORDERD_CLAUDE", HOME / ".local" / "bin" / "claude")
SETTINGS = _p("ORDERD_SETTINGS", HERE / "claude-settings.json")
# Permission mode for lanes. Default acceptEdits; modes that skip permission checks are not accepted.
PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk")
PERMISSION_MODE = os.environ.get("ORDERD_PERMISSION_MODE") or "acceptEdits"
if PERMISSION_MODE not in PERMISSION_MODES:
    raise SystemExit(f"ORDERD_PERMISSION_MODE must be one of {PERMISSION_MODES}, got {PERMISSION_MODE!r}")
STATUS_FILE = _p("ORDERD_STATUS", Path(os.environ.get("REEL_DECK_HOME") or (HOME / "apps" / "reel-deck")) / "factory_status.json")
AUTHORITY = _p("ORDERD_AUTHORITY", CONTROL / "authority.json")
PYTHON = os.environ.get("ORDERD_PYTHON") or sys.executable
TMUX = str(_p("ORDERD_TMUX", Path(shutil.which("tmux") or "/opt/homebrew/bin/tmux")))
TMUX_SOCKET = os.environ.get("ORDERD_TMUX_SOCKET") or None       # tests only (-L sandbox); production = default server
TMUX_SHELL = os.environ.get("ORDERD_TMUX_SHELL") or None         # tests only; production = tmux default-shell
SESSION_PREFIX = "reel-lane-"
SHELLS = ("zsh", "-zsh", "bash", "-bash", "sh", "-sh", "login")
SHELL_GRACE_S = int(os.environ.get("ORDERD_SHELL_GRACE_S", 45))  # a pane at a shell this soon after launch = not started yet

LOCK = CONTROL / "orderd.lock"
# Up to MAX_LANES lanes run side by side, never two on the same row. Slot 1 uses orderd.lock; slots 2.. are
# orderd.lane<N>.lock.
MAX_LANES = max(1, min(3, int(os.environ.get("ORDERD_MAX_LANES", 3))))   # hard ceiling 3 (heavy ffmpeg lanes)
TICK_LOCK = CONTROL / "orderd.tick.lock"
BACKOFF = CONTROL / "orderd.backoff"
PAUSED = CONTROL / "orderd.PAUSED"
STATE = CONTROL / "orderd.state.json"
LOG = LOGDIR / "orderd.log"

RUNNER_LANE = "orderd"
LANE_WALL_S = int(os.environ.get("ORDERD_LANE_WALL_S", 5 * 3600))   # 5 h wall per lane
LIMIT_RETRY_S = 30 * 60
MAX_ATTEMPTS = 3
DEBOUNCE_S = int(os.environ.get("ORDERD_DEBOUNCE_S", 120))       # let a burst of notes land as one round
LOOKBACK_S = int(os.environ.get("ORDERD_LOOKBACK_DAYS", 14)) * 86400
CLAIM_TRUST_S = 3 * 3600                                           # the Deck's PICKUP_TRUST_S
MODEL = os.environ.get("ORDERD_MODEL", "opus")

KINDS = ("drop", "continue", "feedback", "frame-note", "batch-verdict", "smoketest")
_NAME_RE = re.compile(r"^ui-(drop|continue|feedback|frame-note|batch-verdict|smoketest)[-.]")
_ROW_RE = re.compile(r"-row(\d{2,3})-")
_TS_RE = re.compile(r"(\d{8})T(\d{6})Z")
_STATUS_RE = re.compile(r"^<!--status (\{.*\}) -->\s*$")
_HEAD_RE = re.compile(r"^(ACK|CALL)\s+[—–-]+\s+(.+?)\s+[—–-]+\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
_DISP_RE = re.compile(r"^Disposition:\s*(\w+)", re.MULTILINE)
OPEN_STATES = ("picked_up", "running", "progress", "queued", "blocked")

REFUSAL_RE = re.compile(
    r"disabled Claude subscription access|Invalid API key|Please run /login|Not logged in|"
    r"OAuth token (?:has )?(?:expired|revoked)|OAuth session expired|Failed to authenticate|authentication_error|"
    r"Credit balance is too low", re.IGNORECASE)
LIMIT_RE = re.compile(r"usage limit reached|hit your (?:weekly|session|usage|5-hour) limit|"
                      r"API Error: 5\d\d|overloaded_error", re.IGNORECASE)


# ------------------------------------------------------------------ small utils
def utc(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def stamp(ts: Optional[float] = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts if ts is not None else time.time()))


def parse_utc(s: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def log(msg: str) -> None:
    LOGDIR.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{utc()} {msg}\n")


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


# ------------------------------------------------------------------ receipts
@dataclass
class Receipt:
    path: Path
    kind: str
    row: Optional[int]
    at: float                      # when the Deck wrote it (name stamp, mtime fallback)
    disposition: Optional[str]
    editor: bool
    heads: list = field(default_factory=list)     # [(head, lane, at_str, line)]
    blocks: list = field(default_factory=list)    # status blocks, in order

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def priority(self) -> int:
        if self.kind == "feedback" and (self.disposition or "").upper() == "REJECT":
            return 1
        if self.kind in ("feedback", "frame-note", "batch-verdict"):
            return 2
        if self.kind == "continue":
            return 3
        if self.kind == "drop":
            return 4
        return 9                                   # smoketest

    def orderd_heads(self) -> list:
        return [h for h in self.heads if h[1] == RUNNER_LANE or h[1].startswith(RUNNER_LANE + "-")]


def load_receipt(path: Path) -> Optional[Receipt]:
    m = _NAME_RE.match(path.name)
    if not m or not path.name.endswith(".md"):
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ts = _TS_RE.search(path.name)
    at = None
    if ts:
        at = parse_utc(f"{ts.group(1)[:4]}-{ts.group(1)[4:6]}-{ts.group(1)[6:]}T"
                       f"{ts.group(2)[:2]}:{ts.group(2)[2:4]}:{ts.group(2)[4:]}Z")
    if at is None:
        at = path.stat().st_mtime
    r = _ROW_RE.search(path.name)
    d = _DISP_RE.search(text)
    row = int(r.group(1)) if r else None
    if row is None and m.group(1) == "drop":
        # Deck "Order the batch" (build_variants_of): a drop that targets an existing row, so it bundles
        # with that row's other open orders and the lane gets the variants brief (lane_prompt.variants_of).
        vm = re.search(r"^ORDER: build a 10-variant alternate batch for registry row (\d{1,3})\.", text, re.M)
        row = int(vm.group(1)) if vm else None
    rec = Receipt(path=path, kind=m.group(1), row=row, at=at,
                  disposition=d.group(1) if d else None,
                  editor=("AUTHORITY: EDITOR" in text) or bool(re.search(r"^Sent by:\s*\S+\s*\(EDITOR\)", text, re.M)))
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        hm = _HEAD_RE.match(ln)
        if hm:
            rec.heads.append((hm.group(1), hm.group(2).strip(), hm.group(3), ln))
            continue
        sm = _STATUS_RE.match(ln)
        if sm and i > 0 and lines[i - 1].startswith(("ACK", "CALL")):
            try:
                b = json.loads(sm.group(1))
                if isinstance(b, dict) and b.get("state"):
                    rec.blocks.append(b)
            except ValueError:
                pass
    return rec


def classify(rec: Receipt, now: float, smoke: bool = False) -> tuple[str, str]:
    """-> (verdict, reason). verdict: actionable | retry | closed | claimed | stale | skip.

    OPEN = nobody has answered it. A receipt another lane answered is theirs: finished/CALL = closed, an
    open pickup = claimed (never race another lane on a row).
    Receipts orderd already touched are closed, except a usage-limit/API-error `queued` whose not_before
    has passed (bounded retry)."""
    if rec.kind == "smoketest" and not smoke and not rec.name.startswith("ui-smoketest-orderd-"):
        return "skip", "smoketest (the Deck ACKs these itself)"
    if rec.kind != "smoketest" and smoke:
        return "skip", "smoke mode takes smoketests only"
    if rec.kind == "smoketest":
        # the Deck's bus watcher ACKs every smoketest with a `deck-bus` round-trip line within a minute; that is
        # not an answer to an orderd smoke, so it never closes one
        rec.heads = [h for h in rec.heads if h[1] != "deck-bus"]
        rec.blocks = [b for b in rec.blocks if b.get("lane") != "deck-bus"]
    mine = rec.orderd_heads()
    if mine:
        last = [b for b in rec.blocks if str(b.get("lane", "")).startswith(RUNNER_LANE)]
        if last and last[-1].get("state") == "queued" and last[-1].get("lane") == RUNNER_LANE:
            nb = parse_utc(str(last[-1].get("not_before") or ""))
            attempts = sum(1 for b in last if b.get("state") == "queued" and b.get("lane") == RUNNER_LANE)
            if attempts >= MAX_ATTEMPTS:
                return "closed", f"orderd retries exhausted ({attempts})"
            if nb is not None and now >= nb:
                return "retry", f"retry {attempts + 1}/{MAX_ATTEMPTS} (not_before {last[-1].get('not_before')} passed)"
            return "closed", f"orderd retry waits until {last[-1].get('not_before')}"
        return "closed", "orderd already handled it"
    if not rec.heads:
        if now - rec.at > LOOKBACK_S:
            return "stale", f"unanswered but older than {LOOKBACK_S // 86400} days"
        return "actionable", "no ACK/CALL yet"
    # answered by another lane
    if rec.blocks:
        # structured truth only when the newest head carries a block (same rule as the Deck)
        last_head = rec.heads[-1]
        if rec.blocks[-1].get("at") == last_head[2] and rec.blocks[-1].get("lane") == last_head[1]:
            st = rec.blocks[-1].get("state")
            if st in ("finished", "called"):
                return "closed", f"{st} by {last_head[1]}"
            at = parse_utc(str(rec.blocks[-1].get("at")))
            return "claimed", f"{st} by {last_head[1]} at {rec.blocks[-1].get('at')}" + (
                "" if at and now - at < CLAIM_TRUST_S else " (stale claim, left alone)")
    head, lane, at_s, line = rec.heads[-1]
    if head == "CALL":
        return "closed", f"CALL by {lane} (waits on the reviewer)"
    if re.search(r"—\s*picked up\b", line, re.I) and not re.search(r"—\s*finished\b", line, re.I):
        return "claimed", f"picked up by {lane} at {at_s}"
    return "closed", f"answered by {lane} at {at_s}"


def scan(now: float, smoke: bool = False) -> tuple[list, list]:
    """-> (candidates [(rec, verdict, reason)], everything else [(rec, verdict, reason)])."""
    cands, rest = [], []
    try:
        paths = sorted(p for p in BUS.iterdir() if p.is_file() and p.name.startswith("ui-") and p.name.endswith(".md"))
    except OSError as e:
        log(f"scan failed: {e}")
        return [], []
    for p in paths:
        rec = load_receipt(p)
        if rec is None:
            continue
        v, why = classify(rec, now, smoke)
        (cands if v in ("actionable", "retry") else rest).append((rec, v, why))
    cands.sort(key=lambda t: (t[0].priority, t[0].at, t[0].name))
    return cands, rest


@dataclass
class Decision:
    action: str                    # spawn | wait | idle
    reason: str
    order: Optional[Receipt] = None
    verdict: str = ""
    bundle: list = field(default_factory=list)


def decide(now: float, smoke: bool = False, busy_rows: tuple = ()) -> Decision:
    cands, _ = scan(now, smoke)
    held = [c for c in cands if c[0].row is not None and c[0].row in busy_rows]
    cands = [c for c in cands if c not in held]       # never two lanes on one row: those wait for their lane
    if not cands:
        if held:
            return Decision("wait", f"row {held[0][0].row} already has a lane; its new note waits for it")
        return Decision("idle", "no open orders")
    rec, verdict, why = cands[0]
    bundle = []
    if rec.priority in (1, 2) and rec.row is not None:
        bundle = [c[0] for c in cands if c[0] is not rec and c[0].row == rec.row and c[0].priority in (1, 2)]
        newest = max([rec.at] + [b.at for b in bundle])
        if now - newest < DEBOUNCE_S:
            return Decision("wait", f"debounce: row {rec.row} got a note {int(now - newest)}s ago (<{DEBOUNCE_S}s)",
                            rec, verdict, bundle)
    return Decision("spawn", why, rec, verdict, bundle)


# ------------------------------------------------------------------ bus writes (always via busline.py)
def _call_text(text: str) -> str:
    """A CALL line is one short question with '1 = ..., 2 = ...' options (busline refuses an essay).
    orderd's own CALLs (account refused, a lane that called without options) are shaped here."""
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("busline", BUSLINE)
        bl = importlib.util.module_from_spec(spec); spec.loader.exec_module(bl)
        if bl.call_shape_problem(text) is None:
            return text
        limit = int(bl.CALL_MAX_CHARS)
    except Exception:
        limit = 320
    tail = " 1 = answer in the comment box / resend the order, 2 = park this row"
    head = " ".join(str(text).split())
    room = limit - len(tail)
    if len(head) > room:
        head = head[:room - 1].rstrip() + "…"
    return head + tail


def bus(receipt: Path, state: str, text: str, *, why: Optional[str] = None, not_before: Optional[str] = None,
        artifact: Optional[str] = None, lane: str = RUNNER_LANE) -> str:
    if state == "called":
        text = _call_text(text)
    cmd = [PYTHON, str(BUSLINE), "--receipt", str(receipt), "--lane", lane, "--state", state, "--text", text]
    if why:
        cmd += ["--why", why]
    if not_before:
        cmd += ["--not-before", not_before]
    if artifact:
        cmd += ["--artifact", artifact]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = (r.stdout or r.stderr).strip()
    log(f"busline {state} {receipt.name}: rc={r.returncode} {out[:300]}")
    return out


# ------------------------------------------------------------------ factory light for the Deck
def heartbeat(state: str, receipt: Optional[str] = None, last_error: Optional[str] = None,
              limit_until: Optional[str] = None, lanes: Optional[list] = None) -> None:
    now = time.time()
    cands = 0
    data = {"alive_at": now, "alive_at_utc": utc(now), "state": state, "receipt": receipt,
            "lanes": lanes or [], "max_lanes": MAX_LANES,
            "paused": PAUSED.exists(), "retry_queue": cands, "last_error": last_error,
            "last_tick_completed_at": now, "last_tick_completed_at_utc": utc(now), "launched_by": "orderd"}
    if limit_until:
        data["limit_until_text"] = limit_until
    try:
        write_json(STATUS_FILE, data)
    except OSError as e:
        log(f"heartbeat write failed: {e}")


def note_idle(reason: str) -> None:
    """Log an idle reason only when it changes (a 60 s tick would otherwise write 1,440 identical lines a day)."""
    st = read_json(STATE, {}) or {}
    if st.get("idle_reason") != reason:
        log(f"idle: {reason}")
        st["idle_reason"] = reason
        write_json(STATE, st)


def clear_idle() -> None:
    st = read_json(STATE, {}) or {}
    if st.pop("idle_reason", None) is not None:
        write_json(STATE, st)


# ------------------------------------------------------------------ lane process
def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:   # a zombie or a recycled pid is not our lane
        out = subprocess.run(["/bin/ps", "-o", "stat=,command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    if not out or out.startswith("Z"):
        return False
    return "orderd.py" in out and "--run-lane" in out


def lane_locks() -> list:
    """Every lane slot's lock path, slot 1 first."""
    return [LOCK] + [CONTROL / f"orderd.lane{i}.lock" for i in range(2, MAX_LANES + 1)]


def free_lock_slot() -> Optional[Path]:
    for lp in lane_locks():
        if not lp.exists():
            return lp
    return None


def _lane_files(order: Receipt, bundle: list, smoke: bool, mode: str) -> dict:
    t = stamp()
    lock_path = free_lock_slot() or LOCK
    base = order.path.stem
    lane_log = LOGDIR / f"lane-{base}-{t}.log"
    result = LOGDIR / f"result-{base}-{t}.json"
    prompt_file = LOGDIR / f"prompt-{base}-{t}.txt"
    prompt = (lane_prompt.smoke_prompt(order.path, result) if smoke
              else lane_prompt.build_prompt(order.path, [b.path for b in bundle], result))
    prompt_file.write_text(prompt, encoding="utf-8")
    return {"pid": None, "mode": mode, "receipt": str(order.path), "bundle": [str(b.path) for b in bundle],
            "kind": order.kind, "row": order.row, "smoke": smoke, "started": time.time(), "started_utc": utc(),
            "log": str(lane_log), "result": str(result), "prompt": str(prompt_file), "exit_file": str(lane_log) + ".exit",
            "claude": str(CLAUDE), "settings": str(SETTINGS), "permission_mode": PERMISSION_MODE, "model": MODEL, "stamp": t, "lock_path": str(lock_path)}


def spawn(order: Receipt, bundle: list, smoke: bool, headless: bool = False) -> dict:
    return spawn_headless(order, bundle, smoke) if headless else spawn_tmux(order, bundle, smoke)


# ---------------------------------------------------------------- tmux lane (default)
def tmux_cmd(*args: str) -> list:
    return [TMUX] + (["-L", TMUX_SOCKET] if TMUX_SOCKET else []) + list(args)


def tmux(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(tmux_cmd(*args), capture_output=True, text=True, timeout=timeout, env=_lane_env())


def session_exists(session: str) -> bool:
    return tmux("has-session", "-t", f"={session}").returncode == 0


def pane_command(session: str) -> Optional[str]:
    r = tmux("display", "-p", "-t", f"={session}:", "#{pane_current_command}")
    return r.stdout.strip() if r.returncode == 0 else None


def capture_pane(session: str, lines: int = 3000) -> str:
    r = tmux("capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", f"={session}:")
    return r.stdout if r.returncode == 0 else ""


def kill_session(session: str) -> None:
    tmux("kill-session", "-t", f"={session}")


# A lane can stop at "Login expired · Please run /login" (or on a subscription-access refusal). orderd never
# types anything into the lane to get past it: logging in is the human's job. It pauses the runner (PAUSED
# flag, so no new lane starts), leaves the stopped lane's session alive for the human to inspect, and files
# ONE CALL on the order. The human fixes the login, then removes the PAUSED flag.
LOGIN_EXPIRED_MARK = "Login expired"


def pause_for_human(reason: str, evidence: str, receipt: Optional[str]) -> None:
    """Write the PAUSED flag with the reason. Only a human removes it."""
    write_json(PAUSED, {"paused_at_utc": utc(), "reason": reason, "evidence": evidence, "receipt": receipt,
                        "resume": f"fix the login / account access by hand, then remove {PAUSED}"})


def _stopped_on(pane: str, matches) -> Optional[str]:
    """The matching stop text when the lane's last turn ended on it and the prompt is idle, else None."""
    lines = [ln.rstrip() for ln in pane.splitlines() if ln.strip() and not ln.strip().startswith("─")]
    tail = lines[-12:]
    hits = [i for i, ln in enumerate(tail) if matches(ln)]
    if not hits:
        return None
    after = tail[hits[-1] + 1:]
    busy = any(("tokens" in ln and "↓" in ln) or "esc to interrupt" in ln for ln in after)
    if busy or not any(ln.strip().startswith("❯") for ln in after):
        return None
    return tail[hits[-1]].strip()


def login_expired(pane: str) -> bool:
    """True when the lane's last claude turn ended in the login-expired stop and the prompt is idle."""
    return _stopped_on(pane, lambda ln: LOGIN_EXPIRED_MARK in ln) is not None


def account_stop(pane: str) -> Optional[str]:
    """A login-expired or account-refusal stop (e.g. subscription access disabled) with an idle prompt."""
    return _stopped_on(pane, lambda ln: LOGIN_EXPIRED_MARK in ln or REFUSAL_RE.search(ln) is not None)


def pause_on_login_stop(lock: dict, now: float) -> bool:
    """A lane stopped on 'Login expired': pause the runner and notify the human, once per lane.
    Sends NOTHING to the lane. Returns True when it paused."""
    session = lock.get("session") or ""
    if not session or lock.get("login_paused_at"):
        return False
    stop = account_stop(capture_pane(session, 60))
    if stop is None:
        return False
    pause_for_human("login-expired" if LOGIN_EXPIRED_MARK in stop else "account-refused", stop, lock.get("receipt"))
    lock["login_paused_at"] = now
    write_json(Path(lock["lock_path"]), lock)
    if lock.get("receipt"):
        bus(Path(lock["receipt"]), "called",
            f"lane {session} stopped on a login/account problem ('{stop[:80]}'). orderd paused and typed nothing. "
            "Fix it by hand, then remove the PAUSED flag", why="login-expired")
    try:
        with open(lock["log"], "a", encoding="utf-8") as fh:
            fh.write(f"[orderd] {utc()} lane {session} stopped on '{stop}' -> runner paused, human notified\n")
    except OSError:
        pass
    log(f"tmux lane {session} stopped on login-expired; runner paused, CALL filed, nothing typed")
    return True


def launch_line(lock: dict) -> str:
    """The claude launch line; the prompt goes in as ONE argv via $(/bin/cat file).
    `; echo $? > <exit file>` records claude's exit code once the pane is back at the shell."""
    import shlex
    q = shlex.quote
    mode = lock.get("permission_mode") or PERMISSION_MODE
    claude = (f"{q(lock['claude'])} --settings {q(lock['settings'])} --permission-mode {q(mode)} --model "
              f"{q(lock['model'])} \"$(/bin/cat {q(lock['prompt'])})\"")
    return f"{claude}; echo $? > {q(lock['exit_file'])}"


def spawn_tmux(order: Receipt, bundle: list, smoke: bool) -> dict:
    lock = _lane_files(order, bundle, smoke, "tmux")
    session = f"{SESSION_PREFIX}{lock['stamp']}"
    lock.update({"session": session, "tmux": TMUX, "tmux_socket": TMUX_SOCKET})
    write_json(Path(lock["lock_path"]), lock)
    new = ["new-session", "-d", "-s", session, "-c", str(REEL), "-x", "220", "-y", "50"]
    if TMUX_SHELL:
        new.append(TMUX_SHELL)
    r = tmux(*new)
    if r.returncode != 0:
        log(f"tmux new-session failed rc={r.returncode}: {(r.stderr or r.stdout).strip()[:300]}")
        Path(lock["log"]).write_text(f"[orderd] {utc()} tmux new-session failed: {(r.stderr or r.stdout).strip()}\n")
        return lock                                   # next tick: session missing -> finalize (blocked)
    # prelude: PATH + bg-wait ceiling + cwd; no auth variables, ever
    tmux("send-keys", "-t", f"={session}:", f"export PATH={HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:"
         f"/bin:/usr/sbin:/sbin; export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0; unset CLAUDECODE "
         f"CLAUDE_CODE_ENTRYPOINT; cd {REEL}", "Enter")
    time.sleep(1)
    line = launch_line(lock)
    tmux("send-keys", "-t", f"={session}:", line, "Enter")
    with open(lock["log"], "a", encoding="utf-8") as fh:
        fh.write(f"[orderd] {utc()} tmux session {session}: {line.replace(lock['prompt'], '<prompt file>')} "
                 f"(prompt {Path(lock['prompt']).stat().st_size} bytes)\n")
    log(f"spawned tmux lane session={session} order={order.name} bundle={[b.name for b in bundle]} smoke={smoke} "
        f"log={Path(lock['log']).name}")
    return lock


def tmux_lane_done(lock: dict, now: float) -> Optional[str]:
    """None while the lane works; else why it is done: busline-<state> | shell | gone | timeout."""
    session = lock.get("session") or ""
    started = float(lock.get("started") or now)
    st = _state_after(Path(lock["receipt"]), started)
    if st in ("finished", "blocked", "called"):
        return f"busline-{st}"
    if now - started > LANE_WALL_S:
        return "timeout"
    if not session_exists(session):
        return "gone"
    cmd = pane_command(session)
    if cmd is not None and cmd in SHELLS and now - started > SHELL_GRACE_S:
        return "shell"
    return None


def close_tmux_lane(lock: dict, why: str) -> None:
    session = lock.get("session") or ""
    pane = capture_pane(session) if session else ""
    try:
        with open(lock["log"], "a", encoding="utf-8") as fh:
            fh.write(f"[orderd] {utc()} lane done ({why}); pane capture of {session} follows\n")
            fh.write(pane.rstrip() + "\n")
            fh.write(f"[orderd] {utc()} tmux kill-session {session}\n")
    except OSError as e:
        log(f"pane capture write failed: {e}")
    kill_session(session)
    log(f"tmux lane {session} done ({why}); session killed")


# ---------------------------------------------------------------- headless lane (--headless only)
def spawn_headless(order: Receipt, bundle: list, smoke: bool) -> dict:
    lock = _lane_files(order, bundle, smoke, "headless")
    write_json(Path(lock["lock_path"]), lock)
    lane_log = Path(lock["log"])
    # the wrapper (this file, --run-lane) is the detached session leader: it runs claude, waits, and records
    # the exit code, so the next tick can read it; killpg(wrapper pid) takes claude down with it.
    with open(os.devnull, "rb") as dn, open(str(lane_log) + ".wrapper", "ab") as wl:
        p = subprocess.Popen([PYTHON, str(Path(__file__).resolve()), "--run-lane", lock["lock_path"]],
                             stdin=dn, stdout=wl, stderr=wl, cwd=str(REEL), start_new_session=True,
                             env=_lane_env())
    lock["pid"] = p.pid
    write_json(Path(lock["lock_path"]), lock)
    log(f"spawned lane pid={p.pid} order={order.name} bundle={[b.name for b in bundle]} smoke={smoke} log={lane_log.name}")
    return lock


def _lane_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env["HOME"] = str(HOME)
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
    env.pop("CLAUDECODE", None)            # a lane is its own session, not a child of whoever ran --once
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    return env


def run_lane(lock_path: Path) -> int:
    """--run-lane: the detached wrapper. Runs claude -p once, writes the exit code, exits. Never retries."""
    lock = read_json(lock_path, {}) or {}
    prompt = Path(lock["prompt"]).read_text(encoding="utf-8")
    claude, settings, model = lock.get("claude") or str(CLAUDE), lock.get("settings") or str(SETTINGS), lock.get("model") or MODEL
    mode = lock.get("permission_mode") or PERMISSION_MODE
    cmd = [claude, "--settings", settings, "--permission-mode", mode, "--model", model,
           "-p", prompt, "--output-format", "text"]
    with open(lock["log"], "ab") as out:
        out.write(f"[orderd] {utc()} launch: {claude} --settings {settings} --permission-mode {mode} "
                  f"--model {model} -p <prompt {len(prompt)} chars> --output-format text\n".encode())
        out.flush()
        try:
            rc = subprocess.call(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                 cwd=str(REEL) if REEL.is_dir() else None)
        except OSError as e:
            out.write(f"[orderd] launch failed: {e}\n".encode())
            rc = 127
        out.write(f"\n[orderd] {utc()} exit {rc}\n".encode())
    Path(lock["exit_file"]).write_text(str(rc), encoding="utf-8")
    return rc


def kill_lane(lock: dict) -> None:
    pid = lock.get("pid")
    if not pid:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(int(pid), sig)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(20):
            time.sleep(0.25)
            if not pid_alive(int(pid)):
                return


def _state_after(receipt: Path, since: float) -> Optional[str]:
    """Newest status state the LANE itself wrote on the receipt after `since`, if any."""
    rec = load_receipt(receipt)
    if rec is None:
        return None
    for b in reversed(rec.blocks):
        at = parse_utc(str(b.get("at")))
        if b.get("lane") == lane_prompt.LANE_NAME and at is not None and at >= since - 1:
            return b.get("state")
    return None


def finalize(lock: dict, timed_out: bool = False, stopped: bool = False) -> str:
    """Write the final bus line(s) for a lane that has exited (or was killed). Returns the outcome word."""
    receipt = Path(lock["receipt"])
    targets = [receipt] + [Path(b) for b in lock.get("bundle") or []]
    started = float(lock.get("started") or 0)
    rc_txt = ""
    try:
        rc_txt = Path(lock["exit_file"]).read_text(encoding="utf-8").strip()
    except OSError:
        pass
    rc = int(rc_txt) if rc_txt.lstrip("-").isdigit() else None
    try:
        tail = Path(lock["log"]).read_text(encoding="utf-8", errors="replace")[-20000:]
    except OSError:
        tail = ""
    if lock.get("mode") == "tmux":                  # the TUI echoes the prompt; judge only the pane's last lines
        tail = "\n".join(tail.splitlines()[-60:])
    result = read_json(Path(lock["result"]), None)
    lane_state = _state_after(receipt, started)
    outcome = "blocked"

    if stopped:
        for t in targets:
            bus(t, "blocked", "orderd lane stopped by hand; nothing further runs on this order until a new send",
                why="stopped by hand")
    elif timed_out:
        for t in targets:
            bus(t, "blocked", f"orderd lane hit the {LANE_WALL_S // 3600} h wall and was killed; "
                "nothing is relaunched", why="timeout")
    elif REFUSAL_RE.search(tail) and lane_state is None and not (result and result.get("outcome") == "FINISHED"):
        m = REFUSAL_RE.search(tail)
        pause_for_human("account-refused", m.group(0), receipt.name)
        for t in targets:
            bus(t, "called", f"the Claude account refused the lane launch ('{m.group(0)}'). orderd is PAUSED and "
                "relaunches nothing. Fix the login or subscription access by hand, then remove the PAUSED flag",
                why="account-refused")
        outcome = "called"
        log(f"account refused ({m.group(0)}); runner paused until a human removes {PAUSED}")
    elif LIMIT_RE.search(tail) and not (result and result.get("outcome") in ("FINISHED", "CALLED")):
        m = LIMIT_RE.search(tail)
        nb = utc(time.time() + LIMIT_RETRY_S)
        write_json(BACKOFF, {"until": time.time() + LIMIT_RETRY_S, "until_utc": nb, "reason": "usage-limit",
                             "evidence": m.group(0), "receipt": receipt.name})
        bus(receipt, "queued", f"lane stopped on '{m.group(0)}'; orderd retries this order once after {nb} "
            f"(max {MAX_ATTEMPTS} attempts)", why="usage limit / API error", not_before=nb)
        outcome = "queued"
    elif lane_state in ("finished", "called", "blocked"):
        outcome = lane_state                     # the lane already closed it on the bus
        for t in targets[1:]:
            bus(t, lane_state, f"handled in the same round as {receipt.name}: " + ((result or {}).get("text") or lane_state),
                why=(str((result or {}).get("why") or lane_state)[:160]) if lane_state != "finished" else None)
    elif result and result.get("outcome") == "FINISHED" and rc == 0:
        for t in targets:
            bus(t, "finished", str(result.get("text") or "done"), artifact=result.get("artifact") or None)
        outcome = "finished"
    elif result and result.get("outcome") == "CALLED":
        for t in targets:
            bus(t, "called", str(result.get("text") or "the lane needs a reviewer decision"),
                why=str(result.get("why") or "reviewer decision needed")[:160])
        outcome = "called"
    else:
        why = (result or {}).get("why") or (f"lane exited {rc} without a result file" if not result
                                            else f"lane reported {result.get('outcome')} (exit {rc})")
        text = (result or {}).get("text") or f"orderd lane ended without delivering (exit {rc}); see {Path(lock['log']).name}"
        for t in targets:
            bus(t, "blocked", str(text), why=str(why)[:160])
        outcome = "blocked"
    log(f"finalized {receipt.name}: outcome={outcome} rc={rc} lane_state={lane_state} "
        f"timed_out={timed_out} stopped={stopped} result={'yes' if result else 'no'}")
    try:
        Path(lock.get("lock_path") or LOCK).unlink()
    except FileNotFoundError:
        pass
    return outcome


# ------------------------------------------------------------------ gates
def factory_closed() -> Optional[str]:
    """None when the kill switch is open. Open = factory_state starting with FACTORY_OPEN (the kit path does
    not check the rest of the object; reelctl wants its exact four-key FACTORY_OPEN form, which is not ours to
    write). Anything else, including a missing or unreadable file, is closed (fail closed)."""
    a = read_json(AUTHORITY, None)
    st = str(a.get("factory_state") or "") if isinstance(a, dict) else ""
    if st.startswith("FACTORY_OPEN"):
        return None
    return f"kill switch {AUTHORITY.name}: factory_state={st or '(missing/unreadable)'}"


def switch_state() -> str:
    a = read_json(AUTHORITY, None)
    return str(a.get("factory_state") or "(no factory_state)") if isinstance(a, dict) else "(missing/unreadable)"


def backoff_active(now: float) -> Optional[dict]:
    b = read_json(BACKOFF, None)
    if isinstance(b, dict) and float(b.get("until") or 0) > now:
        return b
    return None


# ------------------------------------------------------------------ one tick
def _service_lock(lock: dict, now: float, dry_run: bool) -> tuple:
    """Look after ONE lane slot: -> (still_running, timed_out). A finished lane is captured, killed and
    finalized here (never in dry-run, which only reports)."""
    if lock.get("mode") == "tmux":
        age = now - float(lock.get("started") or now)
        why = tmux_lane_done(lock, now)
        if why is None:
            if dry_run:
                print(f"DRY-RUN: tmux lane {lock.get('session')} working, {int(age)}s, order "
                      f"{Path(lock['receipt']).name}")
            else:
                pause_on_login_stop(lock, now)
            return True, False
        if dry_run:
            print(f"DRY-RUN: tmux lane {lock.get('session')} is done ({why}) -> WOULD capture, kill-session, finalize")
            return False, False
        close_tmux_lane(lock, why)
        finalize(lock, timed_out=(why == "timeout"))
        heartbeat("idle", last_error="lane timeout" if why == "timeout" else None)
        return False, why == "timeout"
    pid = int(lock.get("pid") or 0)
    age = now - float(lock.get("started") or now)
    if pid and pid_alive(pid):
        if age > LANE_WALL_S:
            if dry_run:
                print(f"DRY-RUN: lane pid {pid} is {int(age)}s old (> {LANE_WALL_S}s) -> WOULD kill + blocked timeout")
                return False, False
            log(f"lane pid {pid} past the wall ({int(age)}s); killing")
            kill_lane(lock)
            finalize(lock, timed_out=True)
            heartbeat("idle", last_error="lane timeout")
            return False, True
        if dry_run:
            print(f"DRY-RUN: lane running: pid {pid}, {int(age)}s, order {Path(lock['receipt']).name}")
        return True, False
    if dry_run:
        print(f"DRY-RUN: lane for {Path(lock['receipt']).name} has exited -> WOULD finalize its bus line and clear the lock")
        return False, False
    finalize(lock)
    heartbeat("idle")
    return False, False


def tick(dry_run: bool = False, smoke: bool = False, headless: bool = False) -> int:
    now = time.time()
    CONTROL.mkdir(parents=True, exist_ok=True)
    LOGDIR.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        log(f"tick: kill switch factory_state={switch_state()}{' (smoke)' if smoke else ''}")

    running = []          # locks still working after this tick's service pass
    for lp in lane_locks():
        lock = read_json(lp, None)
        if not lock:
            continue
        lock.setdefault("lock_path", str(lp))
        alive, timed_out = _service_lock(lock, now, dry_run)
        if alive:
            running.append(lock)
        elif dry_run:
            return 0
        elif timed_out:
            return 0
    now = time.time()
    busy_rows = tuple(l.get("row") for l in running if l.get("row") is not None)
    if len(running) >= MAX_LANES:
        if dry_run:
            print(f"DRY-RUN: {len(running)} lane(s) working (max {MAX_LANES}): "
                  + ", ".join(f"{l.get('session') or l.get('pid')} on {Path(l['receipt']).name}" for l in running)
                  + " -> nothing picked")
            return 0
        heartbeat("running", receipt=Path(running[0]["receipt"]).name, lanes=[Path(l["receipt"]).name for l in running])
        return 0
    lanes_now = [Path(l["receipt"]).name for l in running]
    if False:
        pass
    lock = None
    if lock and lock.get("mode") == "tmux":
        age = now - float(lock.get("started") or now)
        why = tmux_lane_done(lock, now)
        if why is None:
            if dry_run:
                print(f"DRY-RUN: tmux lane {lock.get('session')} working, {int(age)}s, order "
                      f"{Path(lock['receipt']).name} -> exit 0, nothing picked")
                return 0
            heartbeat("running", receipt=Path(lock["receipt"]).name)
            return 0
        if dry_run:
            print(f"DRY-RUN: tmux lane {lock.get('session')} is done ({why}) -> WOULD capture, kill-session, finalize")
            return 0
        close_tmux_lane(lock, why)
        finalize(lock, timed_out=(why == "timeout"))
        heartbeat("idle", last_error="lane timeout" if why == "timeout" else None)
        if why == "timeout":
            return 0
        now = time.time()
        lock = None
    if lock:
        pid = int(lock.get("pid") or 0)
        age = now - float(lock.get("started") or now)
        if pid and pid_alive(pid):
            if age > LANE_WALL_S:
                if dry_run:
                    print(f"DRY-RUN: lane pid {pid} is {int(age)}s old (> {LANE_WALL_S}s) -> WOULD kill + blocked timeout")
                    return 0
                log(f"lane pid {pid} past the wall ({int(age)}s); killing")
                kill_lane(lock)
                finalize(lock, timed_out=True)
                heartbeat("idle", last_error="lane timeout")
                return 0
            msg = f"lane running: pid {pid}, {int(age)}s, order {Path(lock['receipt']).name}"
            if dry_run:
                print(f"DRY-RUN: {msg} -> exit 0, nothing picked")
                return 0
            heartbeat("running", receipt=Path(lock["receipt"]).name)
            return 0
        if dry_run:
            print(f"DRY-RUN: lane for {Path(lock['receipt']).name} has exited -> WOULD finalize its bus line and clear the lock")
            return 0
        finalize(lock)
        heartbeat("idle")
        now = time.time()

    b = backoff_active(now)
    if b and not smoke:
        msg = f"backoff ({b.get('reason')}) until {b.get('until_utc')}"
        if dry_run:
            print(f"DRY-RUN: {msg} -> nothing picked")
            return 0
        note_idle(msg)
        heartbeat("limited", last_error=b.get("reason"), limit_until=b.get("until_utc"))
        return 0
    if PAUSED.exists() and not smoke:
        if dry_run:
            print(f"DRY-RUN: paused ({PAUSED}) -> nothing picked")
            return 0
        note_idle("paused")
        heartbeat("paused")
        return 0

    d = decide(now, smoke, busy_rows)
    closed = None if (smoke or (d.order is not None and d.order.kind == "smoketest")) else factory_closed()
    idle_state = "running" if lanes_now else "idle"

    if dry_run:
        if lanes_now:
            print(f"DRY-RUN: {len(lanes_now)} lane(s) working ({', '.join(lanes_now)}); a free slot remains")
        _print_dry(d, now, smoke, closed)
        return 0
    if d.action == "idle":
        note_idle(d.reason)
        heartbeat(idle_state, receipt=lanes_now[0] if lanes_now else None, lanes=lanes_now)
        return 0
    if d.action == "wait":
        note_idle(d.reason)
        heartbeat(idle_state, receipt=lanes_now[0] if lanes_now else None, lanes=lanes_now)
        return 0
    if closed:
        note_idle(f"factory closed; would pick {d.order.name} ({d.verdict}: {d.reason}) — {closed}")
        heartbeat(idle_state, receipt=lanes_now[0] if lanes_now else None, last_error="factory closed (kill switch)", lanes=lanes_now)
        return 0

    clear_idle()
    order = d.order
    smoke = smoke or order.kind == "smoketest"      # ui-smoketest-orderd-* picked by the launchd tick
    what = "smoke lane" if smoke else f"{order.kind} lane" + (f" for row {order.row}" if order.row else "")
    if order.editor and order.kind in ("feedback", "frame-note", "batch-verdict") and not smoke:
        # advisory input: ACK without building, no lane
        bus(order.path, "picked_up", "EDITOR advisory receipt read by orderd")
        bus(order.path, "finished", "EDITOR advisory receipt: not reviewer authority, no build started; "
            "the reviewer can confirm it from the Deck")
        heartbeat("idle")
        return 0
    extra = f" (+{len(d.bundle)} same-round notes)" if d.bundle else ""
    bus(order.path, "picked_up", f"orderd {utc()[:16]}Z starting one {'headless' if headless else 'tmux interactive'} {what}{extra}; "
        f"{'retry after ' + d.reason if d.verdict == 'retry' else 'priority ' + str(order.priority)}")
    for bb in d.bundle:
        bus(bb.path, "picked_up", f"folded into the round for {order.name}")
    lock = spawn(order, d.bundle, smoke, headless)
    heartbeat("running", receipt=order.name, lanes=lanes_now + [order.name])
    return 0


def _print_dry(d: Decision, now: float, smoke: bool, closed: Optional[str]) -> None:
    cands, rest = scan(now, smoke)
    print(f"DRY-RUN {utc(now)}  bus={BUS}  smoke={smoke}")
    print(f"open orders: {len(cands)}")
    for rec, v, why in cands:
        print(f"  P{rec.priority} {rec.name}  [{v}] {why}")
    interesting = [(r, v, w) for r, v, w in rest
                   if (v == "claimed" or (v == "closed" and ("CALL" in w or "called" in w))) and now - r.at < LOOKBACK_S]
    if interesting:
        print("parked or claimed elsewhere (not picked; they wait on the reviewer or another lane):")
        for rec, v, why in interesting:
            print(f"  {rec.name}  [{v}] {why}")
    stale = [r for r, v, _ in rest if v == "stale"]
    if stale:
        print(f"stale unanswered (> {LOOKBACK_S // 86400} d, never auto-picked): {len(stale)}")
    if d.action == "spawn":
        print(f"DECISION: WOULD pick {d.order.name} (P{d.order.priority}, {d.verdict}: {d.reason})"
              + (f" + bundle {[b.name for b in d.bundle]}" if d.bundle else ""))
        if closed:
            print(f"  ...but the live tick would NOT spawn: {closed}")
    else:
        print(f"DECISION: {d.action} — {d.reason}")
    print("spawned: nothing (dry run)")


def status() -> int:
    now = time.time()
    print(f"lane slots: {MAX_LANES}")
    for lp in lane_locks():
        lock = read_json(lp, None)
        print(f"lock {lp.name}:", json.dumps(lock) if lock else "none")
        if lock and lock.get("mode") == "tmux":
            s = lock.get("session") or ""
            print("  tmux session:", s, "exists:", session_exists(s), "pane:", pane_command(s), "row:", lock.get("row"))
        elif lock and lock.get("pid"):
            print("  lane alive:", pid_alive(int(lock["pid"])))
    print("backoff:", json.dumps(backoff_active(now)) if backoff_active(now) else "none")
    print("paused:", PAUSED.exists())
    print("kill switch:", factory_closed() or "open")
    _print_dry(decide(now), now, False, factory_closed())
    return 0


def stop() -> int:
    """--stop: stop EVERY running lane (all slots)."""
    any_lane = False
    for lp in lane_locks():
        lock = read_json(lp, None)
        if not lock:
            continue
        any_lane = True
        lock.setdefault("lock_path", str(lp))
        log(f"stop requested for {lock.get('session') or 'pid ' + str(lock.get('pid'))}")
        if lock.get("mode") == "tmux":
            close_tmux_lane(lock, "stopped by hand")
        else:
            kill_lane(lock)
        print("outcome:", finalize(lock, stopped=True))
    if not any_lane:
        print("no lane running")
        return 0
    heartbeat("idle", last_error="lane stopped by hand")
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one tick")
    ap.add_argument("--dry-run", action="store_true", help="print the decision; spawn and write nothing")
    ap.add_argument("--smoke", action="store_true", help="smoketest receipts only, plumbing lane")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--headless", action="store_true", default=False,
                    help="launch the lane as headless `claude -p` (old shape); default OFF = tmux interactive")
    ap.add_argument("--run-lane", metavar="LOCK", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.run_lane:
        return run_lane(Path(a.run_lane))
    if a.status:
        return status()
    CONTROL.mkdir(parents=True, exist_ok=True)
    with open(TICK_LOCK, "a") as tl:
        try:
            fcntl.flock(tl, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another orderd tick is running")
            return 0
        if a.stop:
            return stop()
        if a.once or a.dry_run:
            return tick(dry_run=a.dry_run, smoke=a.smoke, headless=a.headless)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
