"""orderd tests — fake bus, fake control dir, fake claude. Run: /usr/bin/python3 -m pytest -q (from tools/orderd)."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
REAL_BUSLINE = Path(__file__).resolve().parents[2] / "busline.py"
TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
_N = [0]


def _fake_claude(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def od(tmp_path, monkeypatch):
    _N[0] += 1
    sock = f"orderd-test-{os.getpid()}-{_N[0]}"                 # sandbox tmux server, never the default one
    bus = tmp_path / "bus"; bus.mkdir()
    ctl = tmp_path / "control"; ctl.mkdir()
    logs = tmp_path / "logs"; logs.mkdir()
    scripts = tmp_path / "scripts"; scripts.mkdir()
    (scripts / "reel14-build.js").write_text(
        "const LAW = `\nTHE LAW:\n- ROW 14, reference SYNTH0SHORT, L0048 stays, preflight --row 14\n`\n", encoding="utf-8")
    env = {"ORDERD_BUS": bus, "ORDERD_CONTROL": ctl, "ORDERD_LOGDIR": logs, "ORDERD_BUSLINE": REAL_BUSLINE,
           "ORDERD_CLAUDE": _fake_claude(tmp_path / "claude", "echo ok; exit 0"),
           "ORDERD_STATUS": tmp_path / "factory_status.json", "ORDERD_AUTHORITY": ctl / "authority.json",
           "ORDERD_DEBOUNCE_S": "0", "ORDERD_TMUX_SOCKET": sock, "ORDERD_TMUX_SHELL": "/bin/zsh -f",
           "ORDERD_SHELL_GRACE_S": "0"}
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    sys.path.insert(0, str(HERE))
    import lane_prompt
    import orderd
    importlib.reload(lane_prompt)
    mod = importlib.reload(orderd)
    monkeypatch.setattr(mod, "REEL", tmp_path)                 # lanes start in a temp project root
    monkeypatch.setattr(lane_prompt, "SCRIPTS", scripts)
    monkeypatch.setattr(lane_prompt, "REGISTRY", tmp_path / "REEL_REGISTRY.json")
    (tmp_path / "REEL_REGISTRY.json").write_text(json.dumps({"reels": [{"sequence": 13, "reference_shortcode": "<ref-17>"}]}))
    (ctl / "authority.json").write_text(json.dumps({"factory_state": "FACTORY_OPEN", "mutations_allowed": True}))
    mod._t = tmp_path
    mod._scripts = scripts
    mod._sock = sock
    yield mod
    subprocess.run([TMUX, "-L", sock, "kill-server"], capture_output=True)
    Path(f"/private/tmp/tmux-{os.getuid()}/{sock}").unlink(missing_ok=True)


def receipt(bus: Path, name: str, body: str = "", age_s: int = 3600) -> Path:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - age_s))
    p = bus / name.replace("TS", ts)
    p.write_text(f"# {name}\n\nSent by: admin (OPERATOR)\n{body}\n---\nFactory: treat exactly like operator chat input.\n",
                 encoding="utf-8")
    return p


def test_priority_order(od):
    b = od.BUS
    receipt(b, "ui-drop-TS.md", "Reference URL(s) dropped:\n- <instagram-link>", age_s=9000)
    receipt(b, "ui-continue-TS.md", age_s=8000)
    receipt(b, "ui-frame-note-row13-TS.md", "Frame: f10 @ 0:00.417", age_s=7000)
    receipt(b, "ui-feedback-row13-TS.md", "Disposition: NOTES", age_s=6000)
    rej = receipt(b, "ui-feedback-row12-TS.md", "Disposition: REJECT", age_s=100)
    receipt(b, "ui-smoketest-row13-feedback-TS.md", age_s=50000)
    cands, _ = od.scan(time.time())
    order = [(c[0].kind, c[0].priority) for c in cands]
    assert order[0] == ("feedback", 1), order                       # newest REJECT still beats older notes
    assert [p for _, p in order] == sorted(p for _, p in order)
    assert order[-1] == ("drop", 4)
    assert all(k != "smoketest" for k, _ in order)
    d = od.decide(time.time())
    assert d.action == "spawn" and d.order.path == rej


def test_answered_receipts_are_not_open(od):
    b = od.BUS
    fin = receipt(b, "ui-feedback-row13-TS.md", "Disposition: NOTES")
    od.bus(fin, "finished", "done elsewhere", lane="reel-factory-interactive")
    call = receipt(b, "ui-feedback-row14-TS.md", "Disposition: NOTES", age_s=5000)
    call.write_text(call.read_text() + "CALL — main-session — 2025-01-21T12:00:00Z — which one?\n")
    claimed = receipt(b, "ui-frame-note-row11-TS.md", age_s=200)
    od.bus(claimed, "picked_up", "someone else is on it", lane="main-session")
    old = receipt(b, "ui-drop-TS.md", age_s=40 * 86400)
    cands, rest = od.scan(time.time())
    assert cands == []
    v = {r.name: (verdict, why) for r, verdict, why in rest}
    assert v[fin.name][0] == "closed"
    assert v[call.name][0] == "closed"
    assert v[claimed.name][0] == "claimed"
    assert v[old.name][0] == "stale"


def _running_lock(od, row=None, name="x.md"):
    od.write_json(od.LOCK, {"pid": os.getpid(), "receipt": str(od.BUS / name), "started": time.time(), "row": row,
                            "log": "l", "result": "r", "exit_file": "e", "bundle": []})


def test_lock_respected(od, monkeypatch):
    """With one lane slot (ORDERD_MAX_LANES=1) a running lane holds everything else back."""
    monkeypatch.setattr(od, "MAX_LANES", 1)
    receipt(od.BUS, "ui-drop-TS.md")
    _running_lock(od)
    monkeypatch.setattr(od, "pid_alive", lambda pid: True)
    spawned = []
    monkeypatch.setattr(od, "spawn", lambda *a, **k: spawned.append(a))
    assert od.tick() == 0
    assert spawned == []
    assert od.LOCK.exists()
    assert json.loads(od.STATUS_FILE.read_text())["state"] == "running"


def test_two_lanes_run_side_by_side_never_on_one_row(od, monkeypatch):
    """A second lane starts while the first works, into the second lock slot; a note for the row the first lane holds waits for it."""
    monkeypatch.setattr(od, "MAX_LANES", 2)
    monkeypatch.setattr(od, "pid_alive", lambda pid: True)
    monkeypatch.setattr(od, "factory_closed", lambda: None)
    _running_lock(od, row=16, name="ui-drop-20250104T095541Z.md")
    same = receipt(od.BUS, "ui-feedback-row16-TS.md", "Disposition: NOTES")
    spawned = []
    monkeypatch.setattr(od, "spawn", lambda order, *a, **k: spawned.append(order.name) or {"receipt": str(order.path), "lock_path": str(od.lane_locks()[1])})
    assert od.tick() == 0
    assert spawned == []                                        # row 16 already has a lane: its note waits
    other = receipt(od.BUS, "ui-feedback-row14-TS.md", "Disposition: NOTES")
    import os as _os
    _os.utime(other, (time.time() - 400, time.time() - 400))    # past the debounce
    od.DEBOUNCE_S = 0
    assert od.tick() == 0
    assert spawned == [other.name]                              # a different row goes into slot 2
    assert od.lane_locks()[1].name == "orderd.lane2.lock"
    st = json.loads(od.STATUS_FILE.read_text())
    assert st["state"] == "running" and st["max_lanes"] == 2 and len(st["lanes"]) == 2
    # both slots full -> nothing more is picked
    od.write_json(od.lane_locks()[1], {"pid": os.getpid(), "receipt": str(other), "started": time.time(), "row": 14,
                                       "log": "l", "result": "r", "exit_file": "e", "bundle": []})
    receipt(od.BUS, "ui-drop-TS2.md")
    assert od.tick() == 0
    assert spawned == [other.name]


def test_dry_run_spawns_nothing(od, monkeypatch, capsys):
    r = receipt(od.BUS, "ui-feedback-row13-TS.md", "Disposition: REJECT")
    before = r.read_text()
    monkeypatch.setattr(od, "spawn", lambda *a, **k: pytest.fail("dry run spawned"))
    assert od.tick(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "WOULD pick " + r.name in out and "spawned: nothing" in out
    assert r.read_text() == before                     # no ACK written
    assert not od.LOCK.exists() and not od.STATUS_FILE.exists()


def test_kill_switch_blocks_real_spawn(od, monkeypatch):
    receipt(od.BUS, "ui-drop-TS.md")
    od.AUTHORITY.write_text(json.dumps({"factory_state": "FACTORY_CLOSED_RESET_20250102", "mutations_allowed": False}))
    monkeypatch.setattr(od, "spawn", lambda *a, **k: pytest.fail("spawned while the factory is closed"))
    assert od.tick() == 0
    assert "factory closed" in od.LOG.read_text()


def _run_until_done(od, timeout=30):
    lock = json.loads(od.LOCK.read_text())
    t0 = time.time()
    while od.pid_alive(int(lock["pid"])) and time.time() - t0 < timeout:
        time.sleep(0.2)
    return lock


def test_refusal_text_pauses_and_calls(od, monkeypatch):
    monkeypatch.setattr(od, "CLAUDE", _fake_claude(od._t / "claude-refuse",
        'echo "Your organization has disabled Claude subscription access for Claude Code"; exit 1'))
    r = receipt(od.BUS, "ui-drop-TS.md", "Reference URL(s) dropped:\n- <instagram-link>")
    od.tick(headless=True)                     # picks up + spawns
    assert "picked up" in r.read_text()
    _run_until_done(od)
    od.tick(headless=True)                     # finalizes
    txt = r.read_text()
    assert "CALL — orderd" in txt and '"why":"account-refused"' in txt
    p = json.loads(od.PAUSED.read_text())
    assert p["reason"] == "account-refused" and "remove" in p["resume"]
    assert not od.BACKOFF.exists()
    assert not od.LOCK.exists()
    # paused until a human removes the flag: nothing relaunches, however long it waits
    r2 = receipt(od.BUS, "ui-drop-TS.md".replace("TS", "20990101T000000Z"))
    monkeypatch.setattr(od, "spawn", lambda *a, **k: pytest.fail("relaunched while paused"))
    od.tick(); od.tick()
    assert "ACK" not in r2.read_text()
    assert json.loads(od.STATUS_FILE.read_text())["state"] == "paused"


def test_timeout_kills_and_blocks(od, monkeypatch):
    monkeypatch.setattr(od, "CLAUDE", _fake_claude(od._t / "claude-hang", "sleep 300"))
    r = receipt(od.BUS, "ui-feedback-row13-TS.md", "Disposition: NOTES")
    od.tick(headless=True)
    lock = json.loads(od.LOCK.read_text())
    pid = int(lock["pid"])
    time.sleep(0.5)
    assert od.pid_alive(pid)
    lock["started"] = time.time() - od.LANE_WALL_S - 5          # pretend 3 h passed
    od.write_json(od.LOCK, lock)
    od.tick(headless=True)
    assert not od.pid_alive(pid)
    txt = r.read_text()
    assert '"state":"blocked"' in txt and '"why":"timeout"' in txt
    assert not od.LOCK.exists()
    # no relaunch of the same order
    monkeypatch.setattr(od, "spawn", lambda *a, **k: pytest.fail("relaunched a timed-out order"))
    od.tick()


def test_success_writes_finished_from_result(od, monkeypatch):
    # the fake lane writes the result file named in its prompt, like a real lane is told to
    body = ('R=$(printf "%s" "$*" | grep -o "/[^ ]*result-[^ ]*\\.json" | head -1); '
            'echo \'{"outcome":"FINISHED","row":13,"text":"v012 delivered locally","artifact":"deliver/x.mp4"}\' > "$R"; exit 0')
    monkeypatch.setattr(od, "CLAUDE", _fake_claude(od._t / "claude-ok", body))
    r = receipt(od.BUS, "ui-frame-note-row13-TS.md", "Frame: f1")
    r2 = receipt(od.BUS, "ui-feedback-row13-TS.md", "Disposition: NOTES", age_s=3000)
    od.tick(headless=True)
    assert "picked up" in r2.read_text()                       # bundled into the same round
    _run_until_done(od)
    od.tick(headless=True)
    for p in (r, r2):
        t = p.read_text()
        assert '"state":"finished"' in t and "v012 delivered locally" in t, t
    # closed now: never picked again
    assert od.scan(time.time())[0] == []


def test_debounce_waits_for_a_burst(od, monkeypatch):
    monkeypatch.setattr(od, "DEBOUNCE_S", 120)
    receipt(od.BUS, "ui-frame-note-row13-TS.md", age_s=10)
    d = od.decide(time.time())
    assert d.action == "wait"


def test_editor_receipt_is_acked_without_a_lane(od, monkeypatch):
    p = receipt(od.BUS, "ui-feedback-row13-TS.md", "AUTHORITY: EDITOR — advisory only\nDisposition: REJECT")
    monkeypatch.setattr(od, "spawn", lambda *a, **k: pytest.fail("spawned for an editor note"))
    od.tick()
    assert '"state":"finished"' in p.read_text()


def test_law_is_verbatim_with_row_substituted(od):
    import lane_prompt
    law, src = lane_prompt.law_block(18, "ZZZZZZZZ", od._scripts)
    assert "ROW 18, reference ZZZZZZZZ" in law and "--row 18" in law
    assert "L0048" in law                                       # lesson ids untouched
    law2, _ = lane_prompt.law_block(None, None, od._scripts)
    assert "ROW <NN>, reference <SHORTCODE>" in law2


def test_switch_open_only_on_factory_open_prefix(od):
    for st, closed in (("FACTORY_OPEN", False), ("FACTORY_OPEN_20250103", False),
                       ("FACTORY_CLOSED_RESET_20250102", True), ("", True)):
        od.AUTHORITY.write_text(json.dumps({"factory_state": st}))
        assert (od.factory_closed() is not None) == closed, st
    od.AUTHORITY.unlink()
    assert od.factory_closed() is not None                       # missing file fails closed


def test_every_tick_logs_the_switch_state(od):
    od.AUTHORITY.write_text(json.dumps({"factory_state": "FACTORY_CLOSED_RESET_20250102"}))
    od.tick(); od.tick()
    assert od.LOG.read_text().count("factory_state=FACTORY_CLOSED_RESET_20250102") >= 2


def test_smoke_runs_while_closed(od, monkeypatch):
    od.AUTHORITY.write_text(json.dumps({"factory_state": "FACTORY_CLOSED_RESET_20250102"}))
    receipt(od.BUS, "ui-smoketest-orderd-TS.md")
    spawned = []
    monkeypatch.setattr(od, "spawn", lambda *a, **k: spawned.append(a) or {})
    od.tick(smoke=True)
    assert len(spawned) == 1


# ------------------------------------------------------------------ tmux interactive lane (default launch shape)
def _wait(cond, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.2)
    return False


# the fake claude finds the receipt + result paths inside its ONE prompt argv, like a real lane reads them
_FIND = ('RC=$(printf "%s" "$*" | grep -o "/[^ ]*/ui-[^ ]*\\.md" | head -1); '
         'R=$(printf "%s" "$*" | grep -o "/[^ ]*result-[^ ]*\\.json" | head -1); ')


def _tmux(od, *a):
    return subprocess.run([TMUX, "-L", od._sock] + list(a), capture_output=True, text=True)


def test_headless_flag_off_by_default(od, monkeypatch):
    seen = []
    monkeypatch.setattr(od, "tick", lambda dry_run=False, smoke=False, headless=False: seen.append(headless) or 0)
    od.main(["--once"])
    od.main(["--once", "--headless"])
    assert seen == [False, True]
    import inspect
    assert inspect.signature(od.spawn).parameters["headless"].default is False


def test_tmux_lane_sandbox_session_and_one_argv_prompt(od, monkeypatch):
    argv = od._t / "argv.txt"
    monkeypatch.setattr(od, "CLAUDE", _fake_claude(od._t / "claude-argv",
        f'printf "%s|%s|%s\\n" "$#" "$1" "${{#7}}" > {argv}; exec sleep 300'))
    r = receipt(od.BUS, "ui-drop-TS.md", "Reference URL(s) dropped:\n- <instagram-link>")
    od.tick()
    lock = json.loads(od.LOCK.read_text())
    assert lock["mode"] == "tmux" and lock["session"].startswith("reel-lane-") and lock["pid"] is None
    assert _tmux(od, "has-session", "-t", lock["session"]).returncode == 0            # on the sandbox socket
    assert subprocess.run([TMUX, "has-session", "-t", lock["session"]], capture_output=True).returncode != 0
    assert _wait(argv.exists)
    n, first, plen = argv.read_text().strip().split("|")
    assert n == "7" and first == "--settings"                  # --settings S --permission-mode P --model M <prompt>
    assert int(plen) == len(Path(lock["prompt"]).read_text())    # the whole prompt file arrived as ONE argv
    assert "tmux interactive" in r.read_text()
    od.tick()                                                    # still working: nothing finalized
    assert od.LOCK.exists()


def test_tmux_finalizes_on_lane_busline_and_kills_session(od, monkeypatch):
    body = (_FIND + 'echo \'{"outcome":"FINISHED","row":null,"text":"smoke ok","artifact":null}\' > "$R"; '
            f'{od.PYTHON} {REAL_BUSLINE} --receipt "$RC" --lane orderd-lane --state finished --text "smoke ok"; '
            'exec sleep 300')                                   # an interactive claude that just sits there
    monkeypatch.setattr(od, "CLAUDE", _fake_claude(od._t / "claude-fin", body))
    r = receipt(od.BUS, "ui-smoketest-orderd-TS.md", "TEST")
    od.tick()                                                   # the normal (launchd) tick picks orderd smoketests
    lock = json.loads(od.LOCK.read_text())
    assert lock["smoke"] is True
    assert _wait(lambda: '"lane":"orderd-lane"' in r.read_text() and '"state":"finished"' in r.read_text())
    assert _tmux(od, "has-session", "-t", lock["session"]).returncode == 0
    od.tick()
    assert _tmux(od, "has-session", "-t", lock["session"]).returncode != 0            # killed
    assert not od.LOCK.exists()
    assert "done (busline-finished)" in Path(lock["log"]).read_text()
    assert "outcome=finished" in od.LOG.read_text()
    assert od.scan(time.time())[0] == []


def test_tmux_pane_back_at_shell_finalizes_with_pane_text(od, monkeypatch):
    monkeypatch.setattr(od, "CLAUDE", _fake_claude(od._t / "claude-auth",
        'echo "Failed to authenticate: OAuth session expired and could not be refreshed"; exit 1'))
    r = receipt(od.BUS, "ui-drop-TS.md", "Reference URL(s) dropped:\n- <instagram-link>")
    od.tick()
    lock = json.loads(od.LOCK.read_text())
    assert _wait(lambda: Path(lock["exit_file"]).exists())
    assert _wait(lambda: od.pane_command(lock["session"]) in od.SHELLS)
    od.tick()
    assert not od.LOCK.exists()
    assert _tmux(od, "has-session", "-t", lock["session"]).returncode != 0
    assert "OAuth session expired" in Path(lock["log"]).read_text()       # pane captured into the lane log
    txt = r.read_text()
    assert '"why":"account-refused"' in txt
    assert json.loads(od.PAUSED.read_text())["reason"] == "account-refused"


def test_tmux_kill_on_timeout(od, monkeypatch):
    monkeypatch.setattr(od, "CLAUDE", _fake_claude(od._t / "claude-hang", "exec sleep 300"))
    r = receipt(od.BUS, "ui-feedback-row13-TS.md", "Disposition: NOTES")
    od.tick()
    lock = json.loads(od.LOCK.read_text())
    assert _wait(lambda: od.pane_command(lock["session"]) == "sleep")
    od.tick()
    assert od.LOCK.exists()                                     # working, under the wall
    lock["started"] = time.time() - od.LANE_WALL_S - 5          # pretend 3 h passed
    od.write_json(od.LOCK, lock)
    od.tick()
    assert _tmux(od, "has-session", "-t", lock["session"]).returncode != 0
    txt = r.read_text()
    assert '"state":"blocked"' in txt and '"why":"timeout"' in txt
    assert not od.LOCK.exists()
    monkeypatch.setattr(od, "spawn", lambda *a, **k: pytest.fail("relaunched a timed-out order"))
    od.tick()


def test_deck_smoketests_still_skipped(od):
    receipt(od.BUS, "ui-smoketest-row13-feedback-TS.md")
    assert od.scan(time.time())[0] == []


def test_orderd_smoke_ignores_the_deck_roundtrip_ack(od):
    p = receipt(od.BUS, "ui-smoketest-orderd-TS.md", "TEST")
    p.write_text(p.read_text() + "\nACK — deck-bus — 2025-01-29T12:13:48Z — smoketest: bus round-trip OK, the Deck saw "
                 "this receipt; nothing was built and no verdict was recorded\n")
    cands, _ = od.scan(time.time())
    assert [c[0].name for c in cands] == [p.name]


def test_launch_line_runs_claude_directly():
    """A lane's claude line runs directly in the pane's shell and records its exit code."""
    import orderd as mod
    lock = {"claude": "/x/claude", "settings": "/s.json", "model": "opus", "prompt": "/p.txt", "exit_file": "/e"}
    line = mod.launch_line(lock)
    assert line.startswith("/x/claude --settings /s.json --permission-mode acceptEdits --model opus")
    assert line.endswith("; echo $? > /e")


def test_login_expired_detector_reads_the_pane_tail():
    import orderd as mod
    stopped = "⏺ Bash(ls)\n  ⎿  ok\n⏺ Login expired · Please run /login\n✻ Worked for 3m · done 2:08 pm\n────\n❯ \n────\n  Opus 5.5 · tools\n"
    assert mod.login_expired(stopped)
    working = stopped + "❯ continue\n· Beboppin'… (23s · ↓ 332 tokens)\n❯ \n"
    assert not mod.login_expired(working)
    assert not mod.login_expired("⏺ Bash(ls)\n  ⎿  ok\n❯ \n")
    refused = "⏺ Your organization has disabled Claude subscription access for Claude Code\n────\n❯ \n"
    assert mod.account_stop(refused) and not mod.login_expired(refused)


def test_login_stop_pauses_and_types_nothing(od, monkeypatch):
    """A lane stopped on 'Login expired' is never nudged: orderd pauses and files one CALL."""
    r = receipt(od.BUS, "ui-feedback-row13-TS.md", "Disposition: NOTES")
    sent = []
    monkeypatch.setattr(od, "tmux", lambda *a: sent.append(a) or "")
    monkeypatch.setattr(od, "capture_pane", lambda *a, **k:
                        "⏺ Login expired · Please run /login\n────\n❯ \n")
    lock_path = od.CONTROL / "orderd.lock"
    lock = {"session": "reel-lane-x", "receipt": str(r), "log": str(od._t / "lane.log"), "lock_path": str(lock_path)}
    assert od.pause_on_login_stop(lock, time.time())
    assert not [a for a in sent if a and a[0] == "send-keys"]
    assert json.loads(od.PAUSED.read_text())["reason"] == "login-expired"
    assert '"why":"login-expired"' in r.read_text()
    assert not od.pause_on_login_stop(lock, time.time())           # once per lane


def test_permission_mode_defaults_to_accept_edits_and_refuses_skip_modes(monkeypatch):
    import importlib, orderd as mod
    monkeypatch.delenv("ORDERD_PERMISSION_MODE", raising=False)
    importlib.reload(mod)
    assert mod.PERMISSION_MODE == "acceptEdits"
    settings = json.loads((HERE / "claude-settings.json").read_text())
    assert settings["permissions"]["defaultMode"] == "acceptEdits"
    monkeypatch.setenv("ORDERD_PERMISSION_MODE", "bypass" + "Permissions")
    with pytest.raises(SystemExit):
        importlib.reload(mod)
    monkeypatch.delenv("ORDERD_PERMISSION_MODE")
    importlib.reload(mod)
