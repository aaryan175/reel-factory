#!/usr/bin/env python3
"""THE STRANGER'S WALK — the recursive build's step 1, runnable on demand.

A brand-new Playwright Chromium context (no cookies, no cache — a genuinely clean profile)
at phone width walks the whole Deck as a reviewer who has never seen it:

    login (wrong pw → message) → login → board → open a row → play the reel → play the
    reference → send a TEST verdict → send a TEST frame note → answer a factory question
    (TEST) → sign out → sign in → confirm the sends are listed with their factory state.

Every screen is screenshotted into proof/<date>/. Every failure is a FAIL line with what
was expected and what was seen; the script exits 1 if anything failed. It never sends a
real verdict: sends carry test=true so the Deck files them as ui-smoketest receipts (the
factory ACKs those and does nothing else).

Usage:  .venv/bin/python tests/walk.py [--url URL] [--user reviewer-test] [--pw-file proof/<date>/.reviewer-test.pw]
                                          [--row N] [--width 390] [--no-send]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

APP = Path(__file__).resolve().parent.parent
fails: list[str] = []
passes: list[str] = []


def ok(msg: str) -> None:
    passes.append(msg); print("  PASS", msg)


def fail(msg: str) -> None:
    fails.append(msg); print("  FAIL", msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("REEL_DECK_URL") or "http://127.0.0.1:7355")
    ap.add_argument("--user", default="reviewer-test")
    ap.add_argument("--pw-file", default=None)
    ap.add_argument("--row", type=int, default=None, help="row to review (default: first needs-you row)")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--no-send", action="store_true", help="read-only walk: no verdict/note/answer sends")
    ap.add_argument("--slow", action="store_true", help="also prove typing survives the page's 60s auto-refresh (adds ~70s)")
    a = ap.parse_args()
    today = dt.date.today().isoformat()
    proof = APP / "proof" / today; proof.mkdir(parents=True, exist_ok=True)
    pw_file = Path(a.pw_file) if a.pw_file else APP / "proof" / f".{a.user}.pw"
    password = pw_file.read_text().strip()
    U = a.url.rstrip("/")
    shot_n = [0]

    with sync_playwright() as p:
        # The installed Google Chrome, headless — no Playwright browser download needed,
        # and it IS the engine the stranger's Chrome runs.
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": a.width, "height": a.height}, device_scale_factor=2,
                                  is_mobile=a.width < 700, has_touch=a.width < 700,
                                  user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                                              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1") if a.width < 700 else None,
                                  ignore_https_errors=True)
        page = ctx.new_page()
        console_errors: list[str] = []
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        # Failed responses WITH their URLs — a bare "Failed to load resource" tells nobody anything.
        page.on("response", lambda r: console_errors.append(f"{r.status} {r.url.replace(U, '')}")
                if r.status >= 400 and not (r.status == 401 and "/login" in r.url) else None)

        def shot(name: str) -> None:
            shot_n[0] += 1
            page.screenshot(path=str(proof / f"walk-{shot_n[0]:02d}-{name}.png"), full_page=True)

        def visible_text() -> str:
            return page.inner_text("body")

        # 1. cold open → must land on login, never blank
        page.goto(U + "/", wait_until="networkidle")
        shot("cold-open")
        if "/login" in page.url and page.locator("input[name=password]").count():
            ok("cold open lands on the login page")
        else:
            fail(f"cold open did not land on login (url={page.url})")
        hx = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        if hx: fail("login page scrolls horizontally at phone width")

        # 2. wrong password says so
        page.fill("input[name=username]", a.user); page.fill("input[name=password]", "definitely-wrong")
        page.click("button[type=submit]"); page.wait_for_load_state("networkidle"); shot("wrong-password")
        if "Wrong login" in visible_text(): ok("wrong password shows a message")
        else: fail("wrong password: no visible message")

        # 3. login
        page.fill("input[name=username]", a.user); page.fill("input[name=password]", password)
        t0 = time.time(); page.click("button[type=submit]"); page.wait_for_load_state("networkidle")
        shot("board")
        if page.url.rstrip("/") == U and "REEL DECK" in visible_text():
            ok(f"login lands on the board in {time.time()-t0:.1f}s")
        else:
            fail(f"login did not land on the board (url={page.url})")
        if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
            fail("board scrolls horizontally at phone width")
        # board truth vs API
        board = page.evaluate("() => fetch('/api/board').then(r => r.json())")
        counts = board["counts"]
        body = visible_text()
        for key, label in (("needs_you", "waiting on you"), ("machine", "in the machine"), ("done", "done")):
            if f"{counts[key]} {label}" in body: ok(f"board count '{counts[key]} {label}' matches the API")
            else: fail(f"board header does not show '{counts[key]} {label}'")
        needs = [c for c in board["cards"] if c["bucket"] == "needs_you"]
        row = a.row or (needs[0]["seq"] if needs else board["cards"][0]["seq"])

        # 4. open a row in one tap
        link = page.locator(f"a[href='/row/{row}']").first
        if link.count():
            box = link.bounding_box()
            if box and box["height"] >= 44: ok(f"row {row} card is a tappable target ({int(box['height'])}px tall)")
            else: fail(f"row {row} card too small to tap ({box})")
            link.click(); page.wait_for_load_state("networkidle")
            if "/row/" not in page.url: fail(f"tapping the row {row} card did not open the row (url={page.url})")
        else:
            fail(f"row {row} card not on the board — navigating directly")
        # From here on the row runs in walk (test) mode: every send goes to the factory's
        # ACK-only lane, so the real path is exercised without ordering a build.
        page.goto(U + f"/row/{row}?walk=1", wait_until="networkidle")
        if page.locator("#walk-banner").is_visible(): ok("row shows the TEST MODE banner under ?walk=1")
        else: fail("row does not show the TEST MODE banner under ?walk=1")
        shot(f"row{row}")
        if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
            fail(f"row {row} page scrolls horizontally at phone width")

        # 5. play the reel, then the reference
        def wait_video(label: str) -> None:
            if "isn't on this machine" in visible_text():
                ok(f"{label}: row says plainly the cut isn't on this machine (no dead player)"); return
            try:
                page.wait_for_function("() => { const v=document.getElementById('player'); return v && v.readyState >= 2 && v.duration > 0; }", timeout=20000)
                d = page.evaluate("document.getElementById('player').duration")
                page.evaluate("document.getElementById('player').play().catch(()=>{})"); time.sleep(1.5)
                t = page.evaluate("document.getElementById('player').currentTime")
                if t > 0: ok(f"{label} plays inline (duration {d:.1f}s, currentTime {t:.2f}s)")
                else: fail(f"{label} loaded but does not advance when played")
                page.evaluate("document.getElementById('player').pause()")
            except PWTimeout:
                src = page.evaluate("(document.getElementById('player')||{}).currentSrc")
                fail(f"{label} did not become playable in 20s (src={src})")
        wait_video("current reel")
        shot(f"row{row}-reel-playing")
        ref_btn = page.locator("#btn-reference")
        if ref_btn.count():
            ref_btn.click(); wait_video("reference")
        else:
            fail("no 'Play the reference' control on the row")

        # 6. controls are tappable (one review surface: status pill, comment box, send)
        for cid, label in (("status-pill", "Status"), ("comment-text", "Comment box"), ("btn-send-comment", "Send"), ("tc-chip", "Timestamp chip"), ("btn-play", "Play")):
            el = page.locator("#" + cid)
            if not el.count(): fail(f"control '{label}' missing"); continue
            box = el.bounding_box()
            if box and box["height"] >= 36 and box["x"] >= 0 and box["x"] + box["width"] <= a.width + 1:
                ok(f"control '{label}' on screen and ≥36px")
            else: fail(f"control '{label}' off-screen or too small: {box}")

        if a.slow:
            page.evaluate("window.__walk_nonce = Math.random()")
            page.fill("#comment-text", "[WALK TEST] half-written thought — must survive the refresh")
            page.evaluate("document.getElementById('comment-text').blur()")
            time.sleep(70)
            still = page.evaluate("document.getElementById('comment-text') && document.getElementById('comment-text').value")
            same_load = page.evaluate("typeof window.__walk_nonce === 'number'")
            if same_load and still and "half-written" in still: ok("typed text survived 70s (no reload while text is unsent)")
            else: fail(f"typed text did NOT survive the refresh (same_load={same_load}, value={still!r})")
            page.fill("#comment-text", "")

        if not a.no_send:
            # 7. TEST comment on a frame (timestamp chip on = frame note)
            page.evaluate("const v=document.getElementById('player'); if (v) { v.pause(); v.currentTime = 1.4; }"); time.sleep(0.5)
            page.fill("#comment-text", "[WALK TEST] frame note plumbing")
            page.click("#btn-send-comment"); time.sleep(1.2); shot(f"row{row}-after-comment")
            if "SENT" in page.locator("#btn-send-comment").inner_text(): ok("frame comment: Send flashed SENT ✓")
            else: fail("frame comment did not confirm")
            if page.locator("#thread .comment.mine").count() >= 1: ok("the comment appeared in the thread immediately")
            else: fail("the comment did not appear in the thread")
            # 8. TEST general comment (chip off = notes on the cut)
            page.click("#tc-chip"); page.fill("#comment-text", "[WALK TEST] this is the stranger's walk — plumbing only")
            page.keyboard.press("Enter"); time.sleep(1.2); shot(f"row{row}-after-note")
            if "SENT" in page.locator("#btn-send-comment").inner_text(): ok("general comment: Enter sent it (SENT ✓)")
            else: fail("general comment did not confirm")
            page.click("#tc-chip")
            # 9. status = verdict: Needs changes with a reason
            page.fill("#comment-text", "[WALK TEST] needs changes reason — plumbing only")
            page.click("#status-pill"); page.click("#status-menu [data-status=needs_changes]"); time.sleep(1.2); shot(f"row{row}-after-status")
            if "SENT" in page.locator("#status-pill").inner_text() or "Needs changes" in page.locator("#status-pill").inner_text(): ok("status pill sent the verdict")
            else: fail(f"status pill did not confirm (reads '{page.locator('#status-pill').inner_text()}')")
            # 10. answer a factory question if one is asked (reply in the thread)
            ask = page.locator("#thread .reply-send")
            if ask.count():
                page.locator("#thread .reply-text").first.fill("[WALK TEST] plumbing answer — ignore")
                ask.first.click(); time.sleep(1.2); shot(f"row{row}-after-answer")
                if "SENT" in ask.first.inner_text(): ok("factory question: reply confirmed SENT ✓")
                else: fail("factory question: reply did not confirm")
            else:
                print("  note: no factory question on this row — reply step skipped")
            # 11. the thread shows my sends with a factory state after a reload
            page.reload(wait_until="networkidle"); time.sleep(1.0); shot(f"row{row}-thread")
            body = visible_text()
            if "[WALK TEST]" in body: ok("row lists what I sent after a reload")
            else: fail("row does NOT list what I just sent — the reviewer cannot see their own sends")
            if page.locator("#thread .state").count() >= 1: ok("row shows a factory state for my sends")
            else: fail("row shows no factory state for my sends")
            if page.locator("#markers .marker").count() >= 1: ok("frame comment shows as a marker on the scrubber")
            else: fail("no comment marker on the scrubber")

        # 12. sign out → protected → sign in again
        page.click("form[action='/logout'] button"); page.wait_for_load_state("networkidle")
        if "/login" in page.url: ok("sign out returns to login")
        else: fail(f"sign out did not return to login (url={page.url})")
        page.goto(U + f"/row/{row}", wait_until="networkidle")
        if "/login" in page.url: ok("protected page after sign-out redirects to login")
        else: fail("protected page reachable after sign-out")
        page.fill("input[name=username]", a.user); page.fill("input[name=password]", password)
        page.click("button[type=submit]"); page.wait_for_load_state("networkidle")
        if "REEL DECK" in visible_text() and "/login" not in page.url: ok("second sign-in works")
        else: fail("second sign-in failed")

        # 13. help exists
        page.goto(U + "/help", wait_until="networkidle"); shot("help")
        if page.evaluate("document.title") and "404" not in visible_text() and len(visible_text()) > 200:
            ok("help page exists")
        else: fail("no /help page for the stranger")

        if console_errors:
            fail(f"{len(console_errors)} console error(s): " + " | ".join(console_errors[:3]))
        else: ok("no console errors across the walk")
        ctx.close(); browser.close()

    print(f"\nWALK {'CLEAN' if not fails else 'FAILED'}: {len(passes)} pass, {len(fails)} fail — proof in {proof}")
    (proof / "walk-result.json").write_text(json.dumps({"passes": passes, "fails": fails, "at": dt.datetime.now().isoformat()}, indent=1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
