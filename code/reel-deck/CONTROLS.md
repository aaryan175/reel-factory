# Reel Deck — CONTROLS (control → request → what the user sees → what changed → proof)

Every control on every screen a reviewer (EDITOR role) can reach. "Proof" = a test name in tests/test_app.py, a
walk step in tests/walk.py (proof/<date>/walk-*.png), or a receipt path. A control with no proof is not done.
Walk = `python3 tests/walk.py --row N [--width 390|1280]`. Suite = `.venv/bin/python -m pytest -q tests`.

| Screen | Control | Request | User sees | System change | Proof |
|--------|---------|---------|-----------|---------------|-------|
| /login | Sign in (good) | POST /login | board in ~1s, name + role in rail | session cookie (HMAC, 30d) | walk "login lands on the board"; test_login_and_board |
| /login | Sign in (wrong pw) | POST /login → 401 | "Wrong login. Check the details and try again." | nothing | walk "wrong password shows a message"; test_wrong_password_rejected |
| /login?expired=1 | (arrival after expiry) | GET | "Your session expired — sign in again. Nothing you already sent is lost." | nothing | test_expired_session_says_so_everywhere |
| any | expired cookie on a page | GET → 303 /login?expired=1 | login page with the message | — | test_expired_session_says_so_everywhere |
| any | expired cookie on an action | POST /api/* → 401 JSON | in-page red notice + "Sign in" button, auto-redirect 2.5s | — | deck.js api(); test_expired_session_says_so_everywhere |
| board | header counts | GET / | "N waiting on you · N in the machine · N done" | — | walk "board count … matches the API" |
| board | lanes Needs you / In the machine / Done / Parked | GET / | cards by registry state (head segment, whole words, prose ignored) | — | test_lane_head_segment_decides_before_prose; test_plain_phrase_never_reads_prose_as_state |
| board | tap a card | GET /row/N | the row, one tap, ≥44px target | — | walk "row N card is a tappable target" |
| board | first-login banner (editor) | — | plain-words "New here?" + How to review; ✕ remembers per browser | localStorage deck.banner | base.html |
| board | auto-refresh | reload every 45s | never while typing / a field is focused / text unsent | — | deck.js (guard); OPEN: timed proof in walk |
| rail | Help | GET /help | six plain-words sections, no jargon | — | walk "help page exists" |
| rail | Sign out | POST /logout | login page; protected pages redirect | cookie deleted | walk "sign out returns to login", "protected page after sign-out redirects" |
| rail | Make / Now / ▶ Continue | — | HIDDEN for editors (factory-driving controls) | — | test_editor_does_not_see_factory_controls |
| row | player | GET /media?p= (206 ranges) | current cut plays inline (iPhone UA emulation + desktop Chrome) | — | walk "current reel plays inline" (390 + 1280) |
| row | Play the reference | GET /media?p= | the original plays in the same player | — | walk "reference plays inline" |
| row | cut missing on this machine | — | red notice "This cut isn't on this machine" (+ Drive button when known); never a dead player | — | row.html; walk wait_video accepts the notice |
| row | version pills | — | v004 / v006 test … (never "untagged") | — | row.js |
| row | Pin this frame | — | "pinned ≈fN @ m:ss.mmm" at 23.976fps | — | row.js FPS = 24000/1001 |
| row | Send frame note (+ images) | POST /api/frame-note (multipart) | SENT ✓ on the button; send appears under Sends with state | ui-frame-note-rowNN-<ts>.md on the bus (jpg/png ≤15MB) | walk "frame note button flashed SENT ✓"; test_receipt_writes |
| row | Approve / Reject / Just notes | POST /api/verdict | SENT ✓; Reject without a reason is refused with a notice | ui-feedback-rowNN-<ts>.md; editor receipts carry the advisory stamp | walk "verdict button flashed SENT ✓"; test_editor_verdicts_carry_the_authority_stamp |
| row | same-second burst | 3 POSTs | three receipts, none lost | unique names (-2, -3 suffix) | test_same_second_sends_never_overwrite_each_other |
| row | THE FACTORY IS ASKING YOU → Answer | POST /api/verdict text "RULING on <call_id>: …" | ANSWERED ✓ card; question gone until the factory asks again | receipt; board hides answered call ids | walk "factory question: answer confirmed"; test_real_ruling_hides_the_call… |
| row | Sends on this reel | GET /api/row/N/sends every 20s | each send: waiting for the factory / received by factory / no answer (+done), who, when; factory heartbeat line | — | walk "row lists what I sent", "row shows a factory state"; test_test_mode_sends… |
| row | ?walk=1 TEST MODE | any send with test=true | banner; sends stamped test; "bus round-trip ✓" once the Deck's poller ACKs | ui-smoketest-rowNN-*.md; ACK — deck-bus within 60s; executor never builds | test_deck_acks_smoketests_but_never_real_orders |
| row | Keep / Kill per variant + Send keep/kill | POST /api/batch-verdict | per-variant marks, SENT ✓ | ui-batch-verdict-rowNN | test_receipt_writes — OPEN on device: no row currently has variants on disk |
| any | in-page notice | — | red/blue bar with what to do, ✕ to dismiss; toasts only for success | — | deck.js notice() |
| /health | — | GET | ok, build hash, uptime, factory heartbeat age, registry writable, workbench, index | — | test_health_reports_build_factory_and_registry |
| ops | crash | kill -9 | back in 1s (launchd KeepAlive), same build | — | BUGS #14 |
| ops | logs | — | deck.log/deck.err copy-truncate at 20MB, keep 5, daily | — | test_log_rotation_copies_then_truncates |
| ops | accounts | `deck-user add <name>` / `rm` / `list` | password shown once | scrypt hash in config.json | tools/deck-user |

## PROCESS view
`/row/<N>/process` (link on every row page: "PROCESS — the whole build, every step") shows the row's entire build from disk, read-only:
the pipeline stages, a timeline of every cast / render / receipt, the reference film, the study (cut grid, caption states, brain files, WALLS.md),
every cast_vNNN.json as a slot table, every render with its gate outcome (CLEAN / REFUSED + why), per-shot grade and luma numbers, all look sheets
(SHOTS, CAPTIONS, NATIVE_ORNATE, DEVICES, EYE SHEET, GRADE, GATE beds), the deliverable, the bus receipts and the registry row's own entries.
JSON: `/api/row/<N>/process`. Source: `server/process.py`, template `web/templates/process.html`. Test: `test_process_page_and_api`.

## REVIEW SURFACE — the Frame.io model
The row page is ONE surface now (`web/templates/row.html`, `web/static/js/row.js`):
- **Version stack** (V004 · REFERENCE pills) top-left; **status pill** top-right = the verdict: Needs review / In progress (yours, local) · Needs changes (= REJECT receipt; needs a reason in the box or an earlier comment) · Approved (= APPROVE receipt).
- **Player** with a scrubber that carries a **marker per timed comment** (click = seek), frame step ‹ ›, play/pause (space), loop; ← → step frames, C focuses the comment box.
- **One comment box** under the player. Timestamp chip ON (default) = the comment is a **frame note** at the paused frame; chip OFF = a general **NOTES** receipt. Enter sends; typing pauses the video. 📎 attaches screenshots.
- **Thread** on the right: every send as a comment (avatar, who, when, ⏱ timecode chip, state ○ waiting / ✓ received / ✓✓ done / ! no answer), the factory's ACK/CALL lines nested as replies, and the factory's open questions as amber cards with an inline **Reply** box (= RULING receipt). Sort: by timecode / newest first.
- Receipts on the bus are unchanged (ui-frame-note / ui-feedback / RULING); `row_sends` now also returns `ack_lines`, `timecode_s`, `text_full`.
- Walk (`tests/walk.py`) drives the new controls: status pill, comment box, tc chip, Send, thread reply, markers.
