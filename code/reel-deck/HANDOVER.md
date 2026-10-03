# Reel Deck — handover for a reviewer

## The link
https://<your-deck-host>  (serve it only behind a private network or an authenticated reverse proxy)
Works on a phone or a computer. Sign in with the account you were given.

## How to review (the whole job)
1. Open the link, sign in. You land on the **Board**.
2. Under **Needs you**, tap a card.
3. Press play. Tap **Play the reference** to watch the reference it studies. Flip between them.
4. Pick one:
   - **Approve** — it's good.
   - **Reject** — not good. Say *why* in the box first (the machine rebuilds from your reason).
   - **Just notes** — thoughts, no verdict.
   Something wrong at one moment? Pause there → **Pin this frame** → write it → **Send frame note**.
5. The button turns green **SENT ✓**. Under **Sends on this reel** you'll see it with a state:
   *waiting for the factory* → *received by factory* → *done*. If it says *no answer*, tell whoever runs the factory.
6. If a reel shows **THE FACTORY IS ASKING YOU**, type an answer and tap **Answer**.

The **Help** link (top of the page) says the same thing in more words.

## If something looks wrong
- A red bar tells you what to do. Follow it.
- Sent back to the sign-in page? Your session expired. Sign in again — nothing you sent is lost.
- Video won't play? The row will say the cut isn't on this machine (and offer Drive if it can).
- Anything else: tell whoever gave you your login.

## For whoever runs it
- Configuration: environment variables, see `server/settings.py` (`REEL_DECK_HOME`, `REEL_FACTORY_HOME`, `REEL_FACTORY_WORKDRIVE`, `REEL_FACTORY_TZ`, …). Copy `config.json` to `$REEL_DECK_HOME/config.json` and generate a secret.
- Accounts: `deck-user add <name>` (password printed once) · `deck-user rm <name>` · `deck-user list`
- Health: `curl -s http://127.0.0.1:7355/health` — build, factory heartbeat age, registry writable.
- Service: LaunchAgent `com.reelfactory.reel-deck` (KeepAlive; back in ~1s after a crash). Logs in `logs/`, rotated at 20MB.
- Prove it still works: `python3 tests/walk.py --row <N>` (phone width) · `--width 1280` (desk) · `--slow` (typing survives refresh).
  Sends made by the walk use `?walk=1` test mode: they land on the bus as `ui-smoketest-*` and are ACKed by the Deck — nothing is built.
- Unit tests: `.venv/bin/python -m pytest -q tests` (tests/conftest.py isolates every path to a temp dir)
- State of the build: `BUGS.md` (every bug, every pass) · `CONTROLS.md` (every control and its proof).
