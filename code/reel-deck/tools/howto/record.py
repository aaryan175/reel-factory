#!/usr/bin/env python3
"""Record the Reel Deck walkthrough: every screen an employee touches, in order.

Drives the real Deck in a Chrome reachable over CDP, through a small Playwright-over-CDP helper
module (`cdp_client.CDPClient`) found on REEL_DECK_CDP_LIB. Takes one screenshot per scene and writes
scenes.json for build.py to turn into a narrated mp4.

Environment: REEL_DECK_URL (the Deck's URL), REEL_DECK_WALK_ROW (row to walk through),
REEL_DECK_WALK_USER / REEL_DECK_WALK_PASSWORD_FILE (a throwaway OPERATOR login).

It NEVER sends anything to the factory: it types into boxes and clears them, never presses Send,
and never touches a status pill's options. Read-only by construction.

    python3 tools/howto/record.py            # shots + scenes.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if os.environ.get("REEL_DECK_CDP_LIB"):
    sys.path.insert(0, os.path.expanduser(os.environ["REEL_DECK_CDP_LIB"]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import settings  # noqa: E402

BASE = os.environ.get("REEL_DECK_URL") or "http://127.0.0.1:7355"
OUT = settings.WORKBENCH / "reel-deck-howto" / "walkthrough"
SHOTS = OUT / "shots"
ROW = int(os.environ.get("REEL_DECK_WALK_ROW") or 1)          # the row the video walks through
W, H = 1600, 1000


def creds() -> tuple[str, str]:
    """A throwaway OPERATOR login made for the recording (deleted after), so the video shows every
    button. A reviewer account is EDITOR and sees Board + Help only."""
    user = os.environ.get("REEL_DECK_WALK_USER") or "walkthrough"
    pw_file = os.environ.get("REEL_DECK_WALK_PASSWORD_FILE")
    if not pw_file:
        raise SystemExit("set REEL_DECK_WALK_PASSWORD_FILE to a file holding the walkthrough password")
    return user, Path(pw_file).expanduser().read_text().strip()


def shoot(page, name: str, zoom: str | None = None, pad: int = 40) -> str:
    """One scene's picture. With `zoom` (a CSS selector) the shot is cropped to that element plus a
    margin, so the part being talked about is actually readable at 1080p."""
    if page.evaluate("window.DECK_TEST === true"):
        raise SystemExit(f"{name}: page is in TEST MODE ({page.url}) — the video must show the real thing")
    path = SHOTS / f"{name}.png"
    clip = None
    if zoom:
        b = page.locator(zoom).first.bounding_box()
        if b:
            x, y = max(0, b["x"] - pad), max(0, b["y"] - pad)
            clip = {"x": x, "y": y, "width": min(W - x, b["width"] + 2 * pad),
                    "height": min(H - y, b["height"] + 2 * pad)}
    page.screenshot(path=str(path), clip=clip)
    return str(path)


def main() -> None:
    from cdp_client import CDPClient
    SHOTS.mkdir(parents=True, exist_ok=True)
    scenes: list[dict] = []
    user, pw = creds()

    with CDPClient() as cdp:
        page = cdp.context.new_page()
        client = cdp.context.new_cdp_session(page)
        client.send("Emulation.setDeviceMetricsOverride",
                    {"width": W, "height": H, "deviceScaleFactor": 2, "mobile": False})

        def go(url: str, wait: float = 1.6):
            cdp.goto(page, BASE + url, wait_until="load")
            time.sleep(wait)
            if page.url != BASE + url:
                print(f"  ! asked {url}, landed {page.url}")

        # 1 — sign in
        go("/logout"); go("/login")
        page.fill("input[name=username]", user)
        page.fill("input[type=password]", "••••••••••")
        scenes.append({"shot": shoot(page, "01-login"), "say":
            "This is the Reel Deck. You get a username and a password from whoever runs the factory. "
            "Sign in once and the browser remembers you."})
        page.fill("input[type=password]", pw)
        page.click("button[type=submit]")
        time.sleep(2.5)

        # 2 — the board
        go("/")
        scenes.append({"shot": shoot(page, "02-board"), "say":
            "This is the board. Every reel the machine is working on sits in one of four columns. "
            "Needs you, means it is waiting on your eyes. In the machine, means the factory is building or fixing it, "
            "and there is nothing for you to do. Done is approved. Parked is set aside on purpose."})
        scenes.append({"shot": shoot(page, "03-board-again"), "say":
            "Start at the left. If the Needs you count is zero, you are finished for now. "
            "The light at the top tells you whether the factory is awake."})

        # 3 — the row
        go(f"/row/{ROW}", wait=2.5)
        scenes.append({"shot": shoot(page, "04-row"), "say":
            "Tap a card and you get one screen. The cut on the left, the conversation on the right. "
            "That is the whole job: watch the cut, say what is wrong, or say it is good."})

        # 4 — the player
        page.keyboard.press("k") if False else None
        scenes.append({"shot": shoot(page, "05-player"), "say":
            "The pills above the player switch between our cut and the reference it is studying. "
            "Flick between them, that is how you spot what is off. "
            "Space plays and pauses. The left and right arrow keys step one frame at a time."})

        # 5 — the comment box
        box = page.locator("#comment-text")
        box.click()
        box.type("the colours here look off, please match the reference", delay=18)
        time.sleep(0.6)
        scenes.append({"shot": shoot(page, "06-comment", zoom="#composer"), "say":
            "See something wrong? Write it in the box under the video, the way you would say it in a text message. "
            "Press Enter to send. The little clock chip pins your comment to the exact moment on screen, "
            "so the factory knows which frame you meant. The paperclip attaches a screenshot."})
        box.fill("")

        # 6 — the factory's question (the amber card the composer is answering)
        page.evaluate("(document.querySelector('.comment.factory') || document.querySelector('.comment.call') || document.querySelector('.stamp.amber')?.closest('.comment'))?.scrollIntoView({block:'center'})")
        time.sleep(0.8)
        scenes.append({"shot": shoot(page, "07-question"), "say":
            "Sometimes the factory asks you something. It shows up as a card on the right, and the comment box turns amber "
            "and says, answering the factory's question. Anything you type then goes back as your answer. "
            "Answer these first. Until you do, that reel sits still."})

        # 7 — the verdict (menu opened so the four options are readable; no option is clicked)
        page.evaluate("window.scrollTo(0,0)")
        time.sleep(0.5)
        page.click("#status-pill")
        time.sleep(0.5)
        scenes.append({"shot": shoot(page, "08-verdict", zoom="#status-menu", pad=120), "say":
            "When you have decided, use the status button at the top right. Approved means this cut is the one. "
            "Needs changes sends it back to be redone from your comments. "
            "The other two are just notes to yourself and send nothing."})

        page.keyboard.press("Escape")
        time.sleep(0.3)

        # 8 — what happens next
        page.evaluate("document.querySelector('#thread')?.scrollIntoView({block:'start'})")
        time.sleep(0.8)
        scenes.append({"shot": shoot(page, "09-thread"), "say":
            "Everything you send appears in the thread with its timecode. Waiting means the factory has not picked it up yet, "
            "which is normal for a few minutes. Received means it has your note and has replied. "
            "Two ticks mean it finished that work. Click any timecode and the video jumps there."})

        # 9 — adding a reel
        go("/make")
        page.fill("#drop-note", "recreate 1:1")
        scenes.append({"shot": shoot(page, "10-make", zoom=".panelbox"), "say":
            "To add a new reel, go to Make. Paste the link of the reference reel you want to study and recreate, with permission, "
            "add a note if there is one, and press send to the factory. "
            "It registers the reel, studies the original, and builds a first cut. "
            "That takes a while, so you do not need to wait on the page."})
        page.fill("#drop-note", "")

        # 10 — help
        go("/help")
        scenes.append({"shot": shoot(page, "11-help"), "say":
            "Everything in this video is written down on the Help page, including this video. "
            "If the factory goes quiet for a long time, the nudge button at the top right tells it to carry on. "
            "That is the whole system. Watch, comment, approve, or send it back."})

        client.send("Emulation.clearDeviceMetricsOverride")
        page.close()

    (OUT / "scenes.json").write_text(json.dumps(scenes, indent=2))
    print(f"{len(scenes)} scenes → {OUT/'scenes.json'}")


if __name__ == "__main__":
    main()
