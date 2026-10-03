# Reel Deck — bug classes and open issues

How bugs are worked: every entry = what was done / expected / got / evidence. An entry closes only when the
whole flow re-passes from the top after the fix. Walk proof lives in `proof/<date>/`. Walk =
`python3 tests/walk.py` (headless, phone width by default) or `python3 tests/cdp_walk.py` (a real Chrome over CDP).
Exit rule for a release: one full pass with zero entries, then three consecutive clean passes, one on a real
second device.

## Fixed bug classes (each has a regression test or a walk step)

| # | Area | Symptom | Fix / guard |
|---|------|---------|-------------|
| 1 | board | a row whose state prose contains "APPROVED" filed DONE | head segment decides, whole-word match — `test_lane_head_segment_decides_before_prose` |
| 2 | board/row | same root in the plain-English phrase | `test_plain_phrase_never_reads_prose_as_state` |
| 3 | every page | sideways scroll at 390px | ≤760px single-column CSS; `minmax(0,1fr)` |
| 4 | row | "Play the reference" off-screen | with #3 |
| 5 | row | a send vanished after "SENT ✓" | "Sends on this reel" panel polled every 20s: waiting / received / no answer / done |
| 6 | row | an answered factory question stayed forever | a real RULING receipt hides the call — `test_real_ruling_hides_the_call…` |
| 7 | /help | missing | help.html |
| 8 | any | expired session was a dead-end toast | 401 body + `/login?expired=1` — `test_expired_session_says_so_everywhere` |
| 9 | /health | too little to diagnose | build hash, factory heartbeat age, registry writable |
| 10 | row | frame pin computed at the wrong fps | row.js uses the reel's own rate |
| 11 | any | failures were a 3.5s corner toast | in-page notice with what to do |
| 12 | walk | a test send could order real work | `?walk=1` → `ui-smoketest-*` receipts, ACKed by the Deck itself |
| 13 | rail | editors saw factory-driving controls | role-gated in base.html |
| 14 | ops | logs never rotated | copy-then-truncate at startup + daily |
| 15 | accounts | adding a reviewer was a one-liner | `tools/deck-user` |
| 16 | row | empty player when the cut is not on this machine | plain notice + Drive button |
| 17 | row | same-second sends overwrote each other | `-2`, `-3` suffixes — `test_same_second_sends_never_overwrite_each_other` |
| 18 | row | reference kept inside the workbench project not found | resolver also tries `reelNN-<code>-*/reference*` |
| 19 | row | version pills read "untagged" | row.js version parsing |
| 21 | desktop | buttons below the 40px target | `.btn` min-height |
| 25 | row | typing lost to the 60s refresh | walk `--slow` step |
| 27 | row | auto-refresh wiped "SENT ✓" | no reload within 30s of a send |
| 28 | row | three separate input boxes confused reviewers | ONE comment box + status pill + one thread |
| 29–30 | row | "SENT ✓" flashes raced each other / were wiped by re-render | per-button timers; replied-call set survives renders |
| 31 | header | factory light flickered between worker ticks | 480s awake window |
| 32 | board | a row fell back to NEEDS YOU mid-build | a running receipt counts for 65 min |
| 33 | row | empty media index right after a restart | index persisted to `index-cache.json`; cold builds wait up to 10s |
| 34 | row | no feedback during a slow POST | "sending…" at once |

Further hardening from review passes: status pill seeded from the newest real verdict on the bus; factory CALL
text no longer truncated before the question; thread lists every real send; comment times come from the file-name
stamp, not mtime; /media serves reel folders only; malformed sends → 400 with a plain message; client-error log
capped and rotated; bus CALL lines reach the board; "nothing delivered" is never read as finished; personal
identifiers are scrubbed from receipts on write and on display.

## Open

- **Exposure.** The Deck must only be reachable through a private network / authenticated reverse proxy. Never
  expose it publicly behind the login alone. `/health` discloses paths — keep it private.
- **Device proof.** Inline playback on real iPhone Safari is not provable from an emulated walk.
- **Batch verdicts on device.** Keep/Kill per variant is covered by the API test only until a batch is on disk.
- Two workers could double-pick a receipt if a legacy watcher is re-enabled (keep it disabled).
- Worker heartbeat is model-written; screen-reader announcements; no pause control in the Deck; static assets
  uncached.
