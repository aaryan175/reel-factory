#!/usr/bin/env python3
"""cdp_walk — drive a REAL Chrome over CDP through the Deck's URL, end to end, in TEST MODE
(?walk=1 → ACK-only lane). Not headless, not a Playwright-bundled engine: a new tab in an
already-running Chrome. Needs a Playwright-over-CDP helper module (`cdp_client.CDPClient`)
on REEL_DECK_CDP_LIB. Screenshots + a PASS/FAIL ledger under proof/cdp-<date>/.

Manual script (not collected by pytest):  python3 tests/cdp_walk.py [ROW] [WIDTH]
Environment: REEL_DECK_URL, REEL_DECK_HOME (proof/ + proof/.reviewer-test.pw live there)."""
import json, os, sys, time, datetime as dt
from pathlib import Path
if os.environ.get("REEL_DECK_CDP_LIB"):
    sys.path.insert(0, os.path.expanduser(os.environ["REEL_DECK_CDP_LIB"]))
from cdp_client import CDPClient

U = (os.environ.get("REEL_DECK_URL") or "http://127.0.0.1:7355").rstrip("/")
APP = Path(os.environ.get("REEL_DECK_HOME") or "~/apps/reel-deck").expanduser()
OUT = APP / "proof" / ("cdp-" + dt.date.today().isoformat()); OUT.mkdir(parents=True, exist_ok=True)
PW = (APP / "proof" / ".reviewer-test.pw").read_text().strip()
ROW = int(sys.argv[1]) if len(sys.argv) > 1 else 1
WIDTH = int(sys.argv[2]) if len(sys.argv) > 2 else 1280
ledger = []
def ok(m): ledger.append(("PASS", m)); print("  PASS", m, flush=True)
def fail(m): ledger.append(("FAIL", m)); print("  FAIL", m, flush=True)
n = [0]
def shot(page, name):
    n[0] += 1; p = OUT / f"{WIDTH}-{n[0]:02d}-{name}.png"; page.screenshot(path=str(p)); return p
def text(page): return page.evaluate("document.body.innerText")

with CDPClient(auto_start=False) as cdp:
    page = cdp.new_page()
    page.set_viewport_size({"width": WIDTH, "height": 900 if WIDTH > 700 else 844})
    errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append("console.error: " + m.text) if m.type == "error" and "placard" not in m.text else None)
    try:
        # 1 login
        cdp.goto(page, U + "/login"); page.wait_for_load_state("networkidle"); shot(page, "login")
        page.fill("input[name=username]", "reviewer-test"); page.fill("input[name=password]", PW); page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        if "REEL DECK" in text(page) and "/login" not in page.url: ok("real Chrome: login lands on the board over the public URL")
        else: fail(f"login did not land on the board (url={page.url})")
        shot(page, "board")
        board = page.evaluate("fetch('/api/board').then(r=>r.json())")
        body = text(page)
        for key, label in (("needs_you", "waiting on you"), ("machine", "in the machine")):
            if f"{board['counts'][key]} {label}" in body: ok(f"board header '{board['counts'][key]} {label}' matches the API")
            else: fail(f"board header does not show '{board['counts'][key]} {label}'")
        card = next((c for c in board["cards"] if c["seq"] == ROW), None)
        if card: ok(f"row {ROW} card: bucket={card['bucket']} phrase='{card['phrase']}'")
        # 2 row in test mode
        cdp.goto(page, U + f"/row/{ROW}?walk=1"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(800)
        if page.locator("#walk-banner").is_visible(): ok("TEST MODE banner shown under ?walk=1")
        else: fail("no TEST MODE banner under ?walk=1")
        if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"): fail("row page scrolls horizontally")
        else: ok("row page fits the viewport (no horizontal scroll)")
        shot(page, f"row{ROW}")
        # 3 video plays
        try:
            page.wait_for_function("() => { const v=document.getElementById('player'); return v && v.readyState >= 2 && v.duration > 0; }", timeout=20000)
            page.click("#btn-play"); page.wait_for_timeout(1500)
            t = page.evaluate("document.getElementById('player').currentTime")
            if t > 0: ok(f"video plays from the ▶ control (t={t:.2f}s)")
            else: fail("video did not advance after ▶")
            page.evaluate("const v=document.getElementById('player'); v.pause(); v.currentTime=1.4;")
        except Exception as e: fail(f"video not playable in 20s: {str(e)[:80]}")
        shot(page, f"row{ROW}-paused")
        # 4 versions + reference
        if page.locator("#version-pills .pill").count() >= 1: ok("version stack has pills")
        else: fail("no version pills")
        if page.locator("#btn-reference").count():
            page.click("#btn-reference"); page.wait_for_timeout(1200)
            if "reference-source" in page.evaluate("document.getElementById('fname').textContent"): ok("REFERENCE pill loads the original")
            else: fail("REFERENCE pill did not load the original")
            page.locator("#version-pills .pill").first.click(); page.wait_for_timeout(1000)
        # 5 frame comment
        page.evaluate("const v=document.getElementById('player'); v.pause(); v.currentTime=1.4;"); page.wait_for_timeout(400)
        before = page.locator("#thread .comment.mine").count()
        page.click("#comment-text"); page.type("#comment-text", "[CDP WALK] frame comment via the real Chrome", delay=20)
        page.click("#btn-send-comment")
        try:
            page.wait_for_function("() => /SENT/.test(document.getElementById('btn-send-comment').textContent)", timeout=8000); ok("Send confirmed SENT ✓ (frame comment)")
        except Exception: fail(f"Send did not confirm within 8s (reads '{page.locator('#btn-send-comment').inner_text()}')")
        shot(page, "after-frame-comment")
        if page.locator("#thread .comment.mine").count() > before: ok("comment appeared in the thread at once")
        else: fail("comment did not appear in the thread")
        if page.locator("#markers .marker").count() >= 1: ok("comment marker on the scrub bar")
        else: fail("no marker on the scrub bar")
        # 6 general comment via Enter
        page.click("#tc-chip"); page.click("#comment-text"); page.type("#comment-text", "[CDP WALK] general comment via Enter", delay=20)
        page.keyboard.press("Enter")
        try:
            page.wait_for_function("() => /SENT/.test(document.getElementById('btn-send-comment').textContent)", timeout=8000); ok("Enter sent the general comment")
        except Exception: fail("Enter did not send the general comment within 8s")
        page.click("#tc-chip")
        # 7 status = needs changes
        page.click("#status-pill"); page.wait_for_timeout(300)
        if not page.evaluate("document.getElementById('status-menu').hidden"): ok("status menu opens")
        else: fail("status menu did not open")
        shot(page, "status-menu")
        page.click("#status-menu [data-status=needs_changes]"); page.wait_for_timeout(1300)
        if "SENT" in page.locator("#status-pill").inner_text() or "NEEDS CHANGES" in page.locator("#status-pill").inner_text().upper(): ok("status pill sent NEEDS CHANGES")
        else: fail(f"status pill did not confirm ('{page.locator('#status-pill').inner_text()}')")
        # 7b the pill must survive a verdict: change it again after the flash (UI audit HARD 1)
        page.wait_for_timeout(2600)
        page.click("#status-pill"); page.wait_for_timeout(250); page.click("#status-menu [data-status=in_progress]"); page.wait_for_timeout(400)
        lab = page.evaluate("(document.getElementById('status-label')||{}).textContent")
        if lab and "progress" in lab.lower() and page.locator("#status-pill .dot").count() == 1: ok("status pill survives a verdict and changes again (label + dot intact)")
        else: fail(f"status pill broke after a verdict (label={lab!r}, dots={page.locator('#status-pill .dot').count()})")
        # 8 reply to a factory question, if any
        if page.locator("#thread .reply-send").count():
            page.locator("#thread .reply-text").first.fill("[CDP WALK] half-typed — must survive the poll")
            page.evaluate("window.refreshSends && window.refreshSends()"); page.wait_for_timeout(1500)
            kept = page.locator("#thread .reply-text").first.input_value()
            if "half-typed" in kept: ok("half-typed reply survives a thread refresh")
            else: fail("half-typed reply was wiped by the thread refresh")
            page.locator("#thread .reply-text").first.fill("[CDP WALK] reply via the real Chrome"); page.locator("#thread .reply-send").first.click(); page.wait_for_timeout(1300)
            if "SENT" in page.locator("#thread .reply-send").first.inner_text(): ok("reply to the factory question confirmed")
            else: fail("reply did not confirm")
        else: print("  note: no factory question on this row (a fix round may be running)")
        # 9 the bus answers: test sends turn 'bus round-trip ✓' within 90s (the Deck ACKs smoketests)
        t0 = time.time(); got = False
        while time.time() - t0 < 90:
            page.wait_for_timeout(5000)
            if page.evaluate("Array.from(document.querySelectorAll('#thread .comment.mine .state')).some(e => /test/.test(e.textContent) && e.classList.contains('received'))"): got = True; break
        shot(page, "thread-after-ack")
        if got: ok(f"test sends came back received (bus round-trip) in {int(time.time()-t0)}s")
        else: fail("test sends never turned received within 90s")
        # 10 process page + help video
        cdp.goto(page, U + f"/row/{ROW}/process"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(800); shot(page, "process")
        if "THE PIPELINE" in text(page) and page.locator("details.block").count() >= 1: ok("PROCESS page renders casts/renders")
        else: fail("PROCESS page did not render")
        cdp.goto(page, U + "/help#howto-video"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(800)
        page.evaluate("document.querySelector('#howto-video video').play().catch(()=>{})"); page.wait_for_timeout(2000)
        vt = page.evaluate("document.querySelector('#howto-video video').currentTime")
        if vt > 0: ok(f"help video plays (t={vt:.1f}s)")
        else: fail("help video did not play")
        shot(page, "help")
        # 11 sign out (errors after this point are the dying page's own fetches → 401, expected)
        errors_before_logout = list(errors)
        page.click("form[action='/logout'] button"); page.wait_for_load_state("networkidle")
        if "/login" in page.url: ok("sign out returns to login")
        else: fail("sign out did not return to login")
        errors = errors_before_logout
        if errors: fail(f"{len(errors)} browser error(s): " + " | ".join(errors[:3]))
        else: ok("no JS errors in the real Chrome across the walk")
    finally:
        page.close()
fails = [m for s, m in ledger if s == "FAIL"]
(OUT / f"ledger-{WIDTH}.json").write_text(json.dumps({"at": dt.datetime.now().isoformat(), "row": ROW, "width": WIDTH, "ledger": ledger}, indent=1))
print(f"\nCDP WALK {'CLEAN' if not fails else 'FAILED'}: {len(ledger)-len(fails)} pass, {len(fails)} fail — proof in {OUT}")
sys.exit(1 if fails else 0)
