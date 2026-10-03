# 01 — Architecture

How the reference-driven reel factory is put together, how the pieces talk to each other, how each one is started and stopped, how to stand it up on your own machine, and how it fails.

Related documents:
- [README.md](../README.md): what the system is and the quick start.
- [02_CRAFT_PLAYBOOK.md](02_CRAFT_PLAYBOOK.md): the editing method the lanes apply.
- [03_QA_SYSTEM.md](03_QA_SYSTEM.md): the gates, the independent audit, the learning loop.
- [04_LESSONS.md](04_LESSONS.md): every numbered lesson (`L00NN`) cited below.
- [05_CODE_GUIDE.md](05_CODE_GUIDE.md): module by module.
- [06_REFERENCE_FORENSICS.md](06_REFERENCE_FORENSICS.md): breaking a reference down.
- [07_WORKING_WITH_AN_AI_OPERATOR.md](07_WORKING_WITH_AN_AI_OPERATOR.md): running it with an AI coding agent.

**Status.** This code was extracted from a working private system. Paths, ports and a few platform hooks are still those of the original single macOS machine. Expect to adapt them (section 7 lists every one).

---

## Placeholders used in this document

| Placeholder | Meaning | Code default on the original machine |
|---|---|---|
| `<REPO>` | this repository checkout | — |
| `<STATE>` | the factory's state root: registry, bus, control files, lessons, kit run directory | `~/reel-production` |
| `<WORKDRIVE>` | the big media/work volume (footage library, every reel's work tree, renders) | an external drive mounted at `/Volumes/<name>` (`$REEL_FACTORY_WORKDRIVE`; renders under `$REEL_FACTORY_WORKBENCH`) |
| `<FOOTAGE>` | the footage library on `<WORKDRIVE>` (masters, proxies, boards) | `<WORKDRIVE>/workbench/footage-library` |
| `<REFS>` | where fetched reference videos land (inside each row's tree) | `<WORKDRIVE>/workbench/reel<NN>-<CODE>-<YYYYMMDD>/ref/` |
| `<RENDERS>` | the per-row work and delivery trees | `<WORKDRIVE>/workbench/` |
| `<BUS>` | the order/answer directory (receipts) | `<STATE>/_receipts/<bus-dir>/` |
| `<CONTROL>` | kill switch, locks, pause and backoff files | `<STATE>/_control/` |
| `<DECK_DIR>` | the review Deck checkout + its private config | `~/apps/reel-deck` (in this repo: `code/reel-deck/`) |
| `<DECK_PORT>` | the Deck's loopback port | `7355` |
| `<STUDIO_PORT>` | legacy reelctl studio loopback port | `7335` |
| `<PYTHON>` | the interpreter that has the kit's dependencies **and pytest** | `/usr/bin/python3` (3.9) |
| `<FFMPEG>`, `<FFPROBE>` | ffmpeg binaries | `/opt/homebrew/bin/ffmpeg`, `ffprobe` |
| `<TMUX>` | tmux binary | `/opt/homebrew/bin/tmux` |
| `<AGENT_CLI>` | the AI coding agent CLI (e.g. Claude Code's `claude`) | `~/.local/bin/claude` |
| `<NOTIFY_CONFIG>` | chat-bot config for the asker (bot token + chat id; secret) | `~/.config/reelfactory/<your-notify-config>.json` |
| `<your-host>` | the private hostname you reach the Deck on | — |

---

## Contents

1. What the system does
2. The architecture in one picture
3. Components
4. How the components talk
5. The stage machine
6. The life of one order
7. Standing it up
8. Failure modes and mitigations
9. Cheat sheet and glossary

---

## 1. What the system does

You have a library of your own footage (for example clips shot in a flat log profile such as S-Log3 on a Sony camera, which must be colour-graded before they look normal). You see a short-form reel whose *edit* you want to study: its cut rhythm, its caption typography, its colour, its transitions and camera moves. The factory rebuilds that edit with **your own footage**, as a style study or personal edit.

From the reviewer's side the loop is:

1. **Drop a reference.** Paste a reel link (plus an optional note, e.g. "1:1") into the **Deck**, a small web app. A chat session with the agent can file the same order.
2. **The runner picks it up.** `orderd`, a 60-second scheduler, sees the new order and starts a **lane**: one fresh interactive AI coding agent session (e.g. Claude Code) in its own tmux session, with a long prompt holding every standing rule.
3. **The lane builds it.** It fetches the reference (respecting the platform's terms), measures it frame by frame (cuts, captions, fonts, ink, colour, devices, people per shot), casts matching clips from your library under identity rules, and renders with a fixed toolkit (the **one-to-one kit**: one approved colour pipe, captions typeset in Python, one ffmpeg at a time).
4. **A second agent audits it.** One independent audit agent reads the delivered bytes and lists every defect it can measure. The lane fixes every finding, proves each fix with a machine check, and delivers locally. It never ships on its own say-so.
5. **You review in the Deck.** Approve, Reject (with a reason), leave notes, or pin a **frame note** at the exact frame that is wrong. Each press becomes a new order and the loop repeats.
6. **The machine asks only when it must.** If a lane hits a decision only a human can make, it writes one short question with numbered options (a **CALL**). The Deck shows it; a second small service, `asker`, can push it to your phone through a chat bot, and your one-character reply goes back into the loop.
7. **Publishing stays outside.** Nothing is ever posted or uploaded automatically. Anything leaving the machine needs an explicit, per-file human go.

Two loops sit around this:
- **The learning loop.** Every complaint, rejection and audit finding becomes a numbered **lesson**. Where code can check it, the lesson gets a test, so the same mistake fails a gate next time (see [03_QA_SYSTEM.md](03_QA_SYSTEM.md)).
- **The registry.** One JSON file is the single status record: every row (one reference), every version, every hash, every verdict.

**Who decides what.** The human reviewer is the only gate for creative approval, any upload and any publishing. The machine may build, audit, fix, deliver locally and ask. It may never approve on the human's behalf, post, upload, buy, or silently drop a row. Four gates stay separate:

> technical PASS ≠ agent visual PASS ≠ human approval ≠ publish approval

---

## 2. The architecture in one picture

```
                                   THE REVIEWER
            (browser: Reel Deck · optional phone chat bot · chat with an agent session)
                 |                         |                              |
   drop link / Approve / Reject /     reply "53 1" to a              verdict typed in chat
   frame note / Continue / grade      pushed question                (the chat session files it
                 |                         |                          as a ui-* receipt; it does
                 v                         v                          NOT hand-build the reel)
   +--------------------------------+   +--------------------------------------+    |
   |  REEL DECK (FastAPI/uvicorn)   |   |  ASKER  (asker.py --once, every 60s) |    |
   |  127.0.0.1:<DECK_PORT>         |   |  - pushes each open CALL < 48 h old  |    |
   |  private reverse proxy in front|   |  - turns replies into RULING receipts|    |
   |  writes ONLY receipts + grades |   |  - may auto-take "(recommended)"     |    |
   |  /grader -> grades.jsonl       |   |    after 20 min, never for spend/    |    |
   +---------------+----------------+   |    post/upload/buy questions         |    |
                   |                    +-------------------+------------------+    |
                   | ui-*.md receipts                       | ui-feedback RULING    |
                   v                                        v  receipts             v
   +-----------------------------------------------------------------------------------------+
   |  THE BUS   <BUS>/  (a directory of markdown receipts; append-only)                       |
   |  ui-drop-<ts>.md  ui-feedback-rowNN-<ts>.md  ui-frame-note-rowNN-<ts>.md                    |
   |  ui-batch-verdict-rowNN-<ts>.md  ui-continue-<ts>.md  ui-smoketest-*                        |
   |  answers appended ONLY via busline.py:                                                      |
   |     ACK — <lane> — <utc> — picked up|running|progress|queued|BLOCKED|finished: <text>       |
   |     CALL — <lane> — <utc> — <question ≤320 chars, 1 = …, 2 = …>                             |
   |     <!--status {"v":1,"row":N,"lane":…,"state":…,"why":…,"artifact":…} -->                  |
   +-----------------------------+-----------------------------------------------------------+
                                 | scanned every 60 s
                                 v
   +-----------------------------------------------------------------------------------------+
   |  ORDERD (orderd.py --once, launchd/systemd timer, 60 s)                                  |
   |  gates: kill switch <CONTROL>/authority.json (FACTORY_OPEN*) · orderd.PAUSED · backoff     |
   |  picks ONE open order per free slot: P1 REJECT > P2 notes/frame-notes/batch-verdicts       |
   |         > P3 continue > P4 drop (oldest first; 120 s debounce bundles same-row notes)      |
   |  ≤3 lanes at once (orderd.lock, orderd.lane2.lock, orderd.lane3.lock), never 2 per row     |
   |  writes factory_status.json heartbeat for the Deck; logs every decision                    |
   +-----------------------------+-----------------------------------------------------------+
                                 | tmux new-session reel-lane-<utc> →
                                 | <AGENT_CLI> --settings <lane settings> --model <model> "<prompt>"
                                 v
   +-----------------------------------------------------------------------------------------+
   |  ONE LANE = one interactive agent session, one order, then exit (5 h wall)                |
   |  prompt = lane_prompt.py: order text + STANDING rules + optional LAW template block       |
   |                                                                                           |
   |   INTAKE ──► BUILD ───────────────► ONE INDEPENDENT AUDIT ──► FIX EVERY FINDING ──► DELIVER |
   |   fetch ref    cast (identity pool    separate agent call,     each fix proven by a  local |
   |   measure      + grades), preflight   reads delivered bytes,   machine check on the  only  |
   |   cuts/fonts/  gate, kit render       returns the fix list     new delivered bytes         |
   |   ink/people   (onetoone.*, ffx,                                                            |
   |                housechain)                                                                  |
   |   writes: row work tree {ref,brain,cast,work,audit*,deliver}/ · registry row (flock+.bak)  |
   |           · result JSON · ONE final busline (finished|called|blocked) · lessons · preflight |
   |             --finish                                                                        |
   +--------------------------------------------------------------------------------------+----+
                                                                                          |
                                                                                          v
   +--------------------------------+     +----------------------------------------------------+
   |  REGISTRY  REEL_REGISTRY.json  |<----|  WORKBENCH  <RENDERS> on <WORKDRIVE>                  |
   |  rows · rule keys · versions   |     |  ≥100 GB free floor; footage library masters +      |
   |  review_state head drives the  |     |  proxies; every row's tree; deliver/; variants/;     |
   |  Deck board bucket             |     |  audits                                              |
   +---------------+----------------+     +----------------------------------------------------+
                   | read-only (cached)
                   v
   +--------------------------------------------------------------------------------------+
   |  DECK REVIEW: board (Needs you / In the machine / Done / Parked), row page with      |
   |  version pills, player, REFERENCE pill, frame-pinned comments, thread of ACK/CALLs,  |
   |  question cards with option buttons, Variants section (Watch / Keep / Kill / note)  |
   +--------------------------------------+-----------------------------------------------+
                                          | reviewer presses APPROVE
                                          v
         lane records the approval in the registry (no rebuild);
         any upload / publish stays WITHHELD until an explicit per-file go
                                          |
                                          v  (outside the factory; human-run)
                               publishing, if any, is a manual human step

   SIDE LOOPS
   - Learning loop: lessons.py harvest (run inside preflight) → LESSONS_INBOX.md → lane triages
     into LESSONS.md / lessons.json → preflight refuses render / finish until triaged.
   - Grader: Deck /grader → grades.jsonl (HERO / BROLL / NEVER per 5-s segment; ME / NOTME per
     clip) → onetoone.grades → identity + castscan law.
   - Chat sessions: read the bus, file receipts, and fix the MACHINE (kit, prompt, Deck);
     they do not hand-build a row's version.
```

**How to read it.**
- **Everything flows through files.** The Deck writes receipts and grades, nothing else. orderd reads receipts and writes bus lines through `busline.py`. Lanes write the workbench, the registry and bus lines. The Deck reads the registry, the bus and the heartbeat file to draw the board. No state passes in memory; there is no database or queue server.
- **The bus is the conversation.** An order is *open* while nobody has answered it with an ACK/CALL line. It is *claimed* while its newest status block is `picked_up`, `running`, `progress`, `queued` or `blocked`. It is *closed* once a `finished` or `called` line lands.
- **The registry is the memory of record.** If the registry and a chat transcript disagree, the registry wins. If the registry and the reviewer's own recorded words disagree, the reviewer's words win and the registry head gets a recorded truth fix.
- **Three lanes at most, never two on one row.** The ceiling is a machine resource limit (three heavy ffmpeg lanes is the most a smaller machine holds). Even within that, the kit holds a machine-wide single-ffmpeg lock, so renders queue one at a time across lanes.

---

## 3. Components

| Component | Where in this repo | Runs as |
|---|---|---|
| orderd (runner) | `code/reel-production-tools/orderd/orderd.py` | timer job, `--once` every 60 s |
| lane_prompt | `code/reel-production-tools/orderd/lane_prompt.py` | library + preview CLI |
| lane | (an agent session) | tmux session `reel-lane-<utc>` |
| asker | `code/reel-production-tools/orderd/asker.py` | timer job, `--once` every 60 s |
| busline | `code/reel-production-tools/busline.py` | CLI, called by everyone |
| one-to-one kit | `code/reel-production-tools/onetoone/` | `python -m onetoone.<module>` from the tools dir |
| learning loop | `code/reel-production-tools/lessons.py` | CLI, harvest runs inside preflight |
| machine guards | `code/reel-production-tools/whisper_guard.py`, `vocal_guard.py`, `onetoone/ffx.py` | wrappers |
| Reel Deck | `code/reel-deck/` | long-running uvicorn |
| caption-learning | `code/caption-learning/` | offline measurement/sweep tools |
| legacy reelctl | `code/reelctl/` | CLI + optional local studio (retired from production) |
| launchd plists | `config/launchd/` | macOS service definitions |
| control state | `config/control/` | examples of `authority.json`, `orderd.state.json`, `asker.state.json` |
| agent settings | `code/reel-production-tools/orderd/claude-settings.json`, `config/reel-production.claude-settings.json` | passed to lanes / the state-root project |

### 3.1 orderd — the order runner

A 60-second scheduler. Each tick services every running lane, then picks open orders off the bus and starts at most one new lane per free slot. It replaced an earlier always-on agent loop kept alive by a healer script (see 8.1 for why).

**Commands.**
```bash
cd <REPO>/code/reel-production-tools/orderd
<PYTHON> orderd.py --status            # slots, locks, tmux sessions, backoff, paused, kill switch, open orders
<PYTHON> orderd.py --once --dry-run    # what the next tick WOULD pick; writes nothing
<PYTHON> orderd.py --once              # one tick by hand (the timer does this every 60 s)
<PYTHON> orderd.py --stop              # kill EVERY running lane; each order gets `blocked --why "stopped by hand"`
<PYTHON> orderd.py --once --smoke      # pick the newest ui-smoketest receipt, run a plumbing-only lane
touch <CONTROL>/orderd.PAUSED          # pause new lanes (running lanes continue); rm to resume
<PYTHON> -m pytest -q tests            # orderd + asker tests
```
`--headless` exists (non-interactive `-p` launch) but is off by default; see 7.3.

**Reads:** the bus (`ui-*.md`), `<CONTROL>/authority.json`, `orderd.PAUSED`, `orderd.backoff`, lane lock files, lane result JSON, tmux pane text, the optional LAW template (through lane_prompt), the registry (reference-code lookup only).

**Writes:** bus lines via `busline.py` (lane name `orderd`); `<CONTROL>/orderd.lock`, `orderd.lane2.lock`, `orderd.lane3.lock`; `orderd.state.json` (last idle reason, logged only on change); `orderd.backoff`; the Deck heartbeat `factory_status.json`; `orderd.log` (every decision); per lane `{prompt,lane,result}-<order>-<utc>.{txt,log,json}` plus `.log.exit`.

**One tick, in order** (`tick()`):
1. Take the tick lock `orderd.tick.lock` (non-blocking `flock`). If another tick holds it, exit.
2. **Service every lane slot.** A tmux lane counts as done when any one holds:
   - the lane wrote its own `finished`, `blocked` or `called` bus line after it started;
   - the pane is back at a shell more than 45 s after launch (`ORDERD_SHELL_GRACE_S`);
   - the tmux session is gone;
   - the **5 h wall** passed (`ORDERD_LANE_WALL_S`, default 18000).

   A done lane has its pane captured into the lane log, then `tmux kill-session`, then `finalize()` writes the final bus line(s) and deletes the lock. A lane still working whose pane shows `Login expired` or an account refusal (e.g. subscription access disabled) at an idle prompt is **never typed into**: orderd writes `orderd.PAUSED` (reason `login-expired`/`account-refused`), files one CALL on the order and waits. A human logs in by hand and removes the flag.
3. If all slots are busy (`ORDERD_MAX_LANES`, default 3, clamped to 1..3), write a `running` heartbeat and stop.
4. **Backoff.** If `orderd.backoff` is in the future, do nothing. It is set to 30 min after a usage limit or API error. An account refusal or a "Login expired" stop does not back off on a timer: it writes `orderd.PAUSED` (see 5) and files a CALL, and only a human resumes.
5. **Pause.** If `orderd.PAUSED` exists, do nothing.
6. **Decide** (`decide()`):
   - Scan the bus for open orders: no ACK/CALL head at all, or orderd's own `queued` retry whose `not_before` has passed (at most 3 attempts).
   - Rank: **P1** = `ui-feedback` with `Disposition: REJECT`; **P2** = other feedback, frame notes, batch verdicts; **P3** = continue; **P4** = drop. Oldest first within a priority.
   - Skip rows that already have a running lane; their new notes wait.
   - For P1/P2 on a row, **bundle** every other open P1/P2 on that row into the same round, and wait until the newest is 120 s old (`ORDERD_DEBOUNCE_S`), so a burst of notes becomes one round.
   - Orders older than 14 days with no answer (`ORDERD_LOOKBACK_DAYS`) are stale and never auto-picked.
7. **Kill switch.** If `authority.json` `factory_state` does not start with `FACTORY_OPEN`, log "factory closed; would pick …" and do nothing. A missing or unreadable file counts as closed (fail closed). Smoke tests ignore the switch.
8. **EDITOR receipts.** A receipt authored by an EDITOR role (advisory) is not a build order: orderd writes `picked_up` + `finished` ("advisory receipt, no build started") and starts no lane.
9. **Spawn.** Write `picked_up` on the order (and "folded into the round for …" on bundled ones), build the prompt file, then:
   ```
   tmux new-session -d -s reel-lane-<utc> -c <STATE> -x 220 -y 50
   send-keys: export PATH=…; export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0; unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT; cd <STATE>
   send-keys: <AGENT_CLI> --settings <lane settings> --permission-mode <ORDERD_PERMISSION_MODE, default acceptEdits> --model <model> "$(cat <prompt file>)"; \
              echo $? > <lane log>.exit
   ```
   - **Interactive in tmux, not headless.** A headless `-p` launch under a service manager failed authentication ("OAuth session expired and could not be refreshed") where the interactive launch shape worked; the interactive shape was also more reliable under a service manager. See 7.3.
   - **The command is typed into a shell rather than passed to `new-session`.** A pane started with a command reports its `sh -c` parent as the pane command, which reads as "agent exited" immediately (measured on tmux 3.x).
   - **Model** comes from `ORDERD_MODEL` (default `opus`). Use your most capable model for lanes; a lane built on a smaller model produced weaker work.

**finalize() outcomes** (what lands on the bus when a lane ends):

| Lane ended because | orderd writes | Then |
|---|---|---|
| pane shows an account refusal (subscription access disabled, not logged in, OAuth expired, credit too low) and the lane wrote no state | `called --why account-refused` on every bundled receipt | `orderd.PAUSED` written; nothing relaunched until a human fixes the login and removes the flag |
| pane shows a usage limit / API 5xx / overloaded | `queued --not-before now+30 min` | retried at most 3 times, then closed |
| lane already wrote `finished` / `called` / `blocked` | mirrors that state onto bundled receipts | lock cleared |
| result file says FINISHED and exit 0 | `finished` (+ artifact) | — |
| result file says CALLED | `called` | — |
| 5 h wall | `blocked --why timeout` ("nothing is relaunched") | — |
| `--stop` | `blocked --why "stopped by hand"` | — |
| anything else | `blocked` with the result's `why`, or "lane exited N without a result file" | — |

orderd's own CALL lines are reshaped to the busline CALL contract: at most 320 characters, ending `1 = answer in the comment box / resend the order, 2 = park this row`.

**What orderd never does:** relaunch an order in a loop, run two lanes on one row, start a lane while the switch is closed, or treat an advisory receipt as an order.

### 3.2 lane_prompt — what a lane is told

`lane_prompt.py` builds one prompt per order:
1. **Header:** order path, kind, row, reference code (from the registry, or parsed from the drop's URL).
2. **TASK text per kind:**
   - `drop`: register the next row under flock + `.bak`, `mode: grammar_adapt` unless the order says 1:1; then Intake → Build → Audit → Deliver.
   - `variants` (a drop whose body starts `ORDER: build a 10-variant alternate batch for registry row NN`): keep the approved version's cut grid, captions and grade byte-identical, swap footage only, deliver 10 files to `deliver/variants/<stamp>/varNN.mp4`. `ORDER: FINISH the 10-variant … batch for registry row NN. Do not start a new batch.` resumes a half-built batch.
   - `feedback`: APPROVE = mark approved, no rebuild. REJECT/NOTES = measure the complaint first, then fix.
   - `frame-note`: fix at the pinned frame.
   - `batch-verdict`: KEEP/KILL marks; a note on a variant rebuilds that variant in place, the old file moved to `superseded/`.
   - `continue`: resume the newest owed item, or say nothing is owed.
3. **The verbatim receipt text** (≤12,000 characters), plus any bundled receipts.
4. **STANDING RULES** (the part that changes most often; it reflects the latest rulings). In the shipped version: one order then exit; ONE independent audit whose findings are the fix list (L0092); identity law from the identity pool file and the grades file (L0052, L0065); house look (one approved grade, one approved caption face); caption faces identified from reference pixels, never assumed (L0055); delivered files carry the row in their name (L0118); a CALL is one short question (L0079); caption shadow per state (L0056); caption entries measured, not guessed; look at side-by-side sheets before the audit; preflight before any render and before FINISHED; deliver locally only; bus only through busline; the exact end protocol (result file → one final bus line → exit); no personal identifiers in any deliverable. Edit this text for your own rules; it is plain Python string data.
5. **RESULT FILE spec:** `{"outcome": "FINISHED"|"CALLED"|"BLOCKED", "row", "text", "artifact", "why"}`.
6. **The LAW block (optional):** the text between ``const LAW = ` `` and the closing backtick in the newest `<STATE>/workflows/scripts/reel*-build.js` by mtime, with the template's row number and reference code swapped for this order's. This repository does **not** ship a LAW template. Either write your own (footage, identity, fetch, grade, crop, caption, geometry, disk-floor, learning-loop and delivery-boundary rules) or remove the LAW call from `build_prompt()`. If you keep both, make sure STANDING and LAW never contradict each other; when they did in the original system, lanes followed the newer STANDING rule, which is fragile.

Preview a prompt without running anything:
```bash
<PYTHON> <REPO>/code/reel-production-tools/orderd/lane_prompt.py <BUS>/<order>.md | less
```

### 3.3 A lane (the worker, per order)

One interactive agent session in tmux session `reel-lane-<utc>`, started by orderd, handling one order, then exiting. Inside it the lane may launch sub-agents; it must launch exactly one for the independent audit.

- **Watch:** `tmux ls` · `tmux attach -t reel-lane-<utc>` (detach with Ctrl-b d; do not type into it except to nudge) · `tail -f <logdir>/lane-<order>-<utc>.log` (the pane capture is appended at the end).
- **Writes:** the row's work tree `<RENDERS>/reel<NN>-<CODE>-<YYYYMMDD>/` with subfolders `ref/` (reference), `brain/` (cut grid, `captions.plaintext.json`, `caption_devices.json`, `refpeople.json`, notes), `cast/` or `deliver/cast_vNNN.json`, `work/`, `audit*/`, `deliver/` (mp4 + `APPROVAL-CARD-vNNN.md`), `deliver/variants/<stamp>/`; the registry row (under the registry lock + timestamped `.bak`); lessons via `lessons.py`; the result JSON; bus lines under lane name `orderd-lane`. Fonts rebuilt from reference ink (for analysis, see [02_CRAFT_PLAYBOOK.md](02_CRAFT_PLAYBOOK.md)) land in the user font directory as `Ref<NN>-<Weight>.ttf` with `.glyphs.json` and `.sheet.png`.
- **Settings** (`orderd/claude-settings.json`): auto-compact on with a 350k window, `defaultMode: acceptEdits` and an empty `permissions.allow` list you fill with the commands lanes need (ffmpeg, ffprobe, python, …). The launch passes `--permission-mode` from `ORDERD_PERMISSION_MODE` (default `acceptEdits`; modes that skip permission checks are refused). No hooks. Still run the factory as an **unprivileged user whose home holds only the factory**.
- **Stop:** `orderd.py --stop` (all lanes), or `tmux kill-session -t reel-lane-<utc>`; orderd notices "gone" on the next tick and writes `blocked`.

### 3.4 asker — questions to the reviewer's phone

Pushes every open CALL as one short chat message (question, numbered options, a "reply 50 1" hint, Deck link) and turns the reply into a ruling receipt. Optional: without a `<NOTIFY_CONFIG>` it does nothing and CALLs are answered in the Deck.

- **Behaviour:** pushes only CALLs and deliveries younger than 48 h (`ASKER_FRESH_HOURS`). Auto-answers a `(recommended)` option after 20 min (`ASKER_AUTO_MINUTES`) **unless the question mentions spend, buying, posting, publishing, uploading, payment or cards** (`NO_AUTO_RE`); set the minutes very high to disable auto-answers entirely. Pushes "Row N ready to review" when a `finished` line with a row lands. Any `ui-feedback/frame-note/batch-verdict-rowNN` newer than a CALL counts as the answer.
- **Reply grammar:** `50 1`, `row 50 1`, `1` (the newest question), `48 2 make it brighter` (extra words go in verbatim). Only messages from the configured chat id count.
- **Writes:** `ui-feedback-rowNN-<utc>.md` with `Sent by: <account> (OPERATOR) — relayed by asker, words verbatim`, `Disposition: NOTES`, `Operator text (verbatim):` and `RULING on <call receipt> (CALL of <utc>): <n> — <option>`; state `<CONTROL>/asker.state.json` (`pushed`, `offset`, `auto`, `ready`).
- **Commands:** `asker.py --list` (open questions) · `--dry-run` (prints, sends nothing) · `--once`.
- **Config (env):** `ASKER_DECK_URL` (set it to `https://<your-host>:<port>`), `ASKER_AUTO_MINUTES`, `ASKER_FRESH_HOURS`, `ASKER_TELEGRAM` (path to `<NOTIFY_CONFIG>`, chmod 600, never printed).

### 3.5 busline — the only way to write on the bus

```bash
<PYTHON> <REPO>/code/reel-production-tools/busline.py --receipt <ABSOLUTE receipt path> --lane <lane> \
   --state <picked_up|running|progress|queued|blocked|finished|called> --text "<one plain sentence>" \
   [--why "<what it waits on>"] [--not-before <utc>] [--artifact <file>]
```
- `queued` and `blocked` require `--why`.
- `called` writes a `CALL —` head. CALL text must be ≤320 characters (`CALL_MAX_CHARS`) and name options as `1 = …`, `2 = …`; `call_shape_problem()` refuses anything else (L0079).
- Each line is appended under `fcntl.flock`, followed by `<!--status {"v":1,"row":…,"lane":…,"at":…,"state":…} -->`. The Deck, orderd and asker read the **status block**, not the English. Hand-written lines broke the board in the original system; never hand-append.
- Lane names in use: `orderd` (the runner), `orderd-lane` (inside a lane), `deck-bus` (the Deck acknowledging smoke tests).

### 3.6 The bus (directory)

`<BUS>` holds `ui-*` orders plus lane reports (independent audit copies, call notes). Order receipt shape:

```
# ui-frame-note — row 48 — <utc>

Sent by: <account> (OPERATOR)

File: reel48-<CODE>-GRAMMARADAPT-v007.mp4
Frame: f370 @ 0:15.421

Operator note (verbatim):

swap the second clip for a different close-up; keep everything else
---
Factory: treat exactly like operator chat input (same authority). After acting, append `ACK — <lane> — <utc>` below this line. Append-only.
ACK — orderd — <utc> — picked up: starting one tmux interactive frame-note lane for row 48; priority 2
<!--status {...} -->
```
- A receipt filed from a chat session **must** use the exact headers `Sent by: … (OPERATOR)`, `Disposition: APPROVE|REJECT|NOTES` (feedback only) and `Operator text (verbatim):`. Without the last one the Deck shows "! no answer".
- A ruling on a CALL is a feedback receipt whose text contains `RULING on <receipt>.md: <n> — <option>`.
- The file name carries a UTC stamp `YYYYMMDDTHHMMSSZ`; orderd orders by it.
- Variant batches are always P4 drops, never feedback, so the reviewer's notes always jump ahead of them.

### 3.7 Reel Deck — the review room

A FastAPI app in the style of a frame-accurate review tool. Pages: board (Needs you / In the machine / Done / Parked), row page (version pills, REFERENCE pill, player with frame stepping at 24000/1001, one comment box — timecode chip on = frame note, off = NOTES — status pill = verdict, thread of factory ACK/CALL replies, question cards with option buttons, Variants section), `/row/N/process` (read-only whole-build view), `/make` (drop), `/now`, `/help`, `/grader`.

- **Code:** `code/reel-deck/server/{main,auth,board,receipts,registry,lanes,eta,media,process,grader}.py`, `web/` templates + JS, tests `tests/test_app.py`, `tests/test_grader.py`, `tests/test_questions.py`, a Playwright walk `tests/walk.py`, a CDP walk `tests/cdp_walk.py`, user tool `tools/deck-user`. Docs `HANDOVER.md`, `CONTROLS.md`, `BUGS.md`.
- **Run:** `cd <DECK_DIR> && <deck venv>/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port <DECK_PORT>`. Logs `logs/deck.{log,err}` (copy-truncate at 20 MB, keep 5).
- **Network:** bind to loopback only. Put a private-network reverse proxy (VPN/tailnet, or an authenticated tunnel) in front. **Never expose the Deck to the public internet without strong authentication.** The login is a second wall, not the first. Do not remove a front door people rely on without telling them (L0063).
- **Auth:** users in `<DECK_DIR>/config.json` (scrypt hashes + session secret, mode 0600, per machine, never copied). Roles **OPERATOR** (receipts carry decision authority) and **EDITOR** (verdicts advisory, factory controls hidden; cannot send grades). Cookie `deck_session`, 30-day signed. 8 failed logins per client per 10 min. `tools/deck-user add <name> [--admin]` / `rm` / `list` (password printed once).
- **Reads:** the registry (read-only, ~30 s cache), the bus, `factory_status.json`, `<CONTROL>/orderd*.lock` (slot count for "QUEUED · all 3 lanes busy · place N"), agent sub-agent transcripts (`lanes.py`, to show who is working a row), the workbench media index (persisted `index-cache.json`), `board.json` (era filter: which rows the board shows).
- **Writes:** only bus receipts (`receipts.py`), uploads (frame-note screenshots ≤15 MB), and grades (`grader.py` → `grades.jsonl`). Never the registry. Never posts.
- **Health:** `curl -s http://127.0.0.1:<DECK_PORT>/health` returns build hash, uptime, factory heartbeat age, registry writable, workbench status, index state. `ok` needs a writable, parseable registry. The workbench probe runs in a thread with a 6 s timeout, because a privacy-denied open can hang instead of failing.
- **Restart (macOS):** `launchctl bootout gui/$(id -u)/<deck label>; pkill -f 'uvicorn server.main:app'; launchctl bootstrap gui/$(id -u) <plist>`. `kickstart` alone can leave an old uvicorn child serving old code.

**API** (all behind login): `GET /api/board`, `GET /api/row/{seq}`, `GET /api/row/{seq}/sends` (polled every 20 s), `GET /api/row/{seq}/process`, `GET /api/now`, `POST /api/verdict`, `/api/batch-verdict`, `/api/frame-note` (multipart), `/api/drop` (URLs and/or `build_variants_of`), `/api/continue` (rate-limited to one per 600 s), `GET/POST /api/grader/{items,summary,grade,grade-many}`, `GET /media?p=` (HTTP Range, confined to reel folders). Public: `/login`, `/health`, `/api/client-error`, `/static/*`.

**Board placement rule** (`registry.row_lane`, `board.py`): the head segment of `review_state` decides. Verdict words (`PENDING_HUMAN`, `AWAITING`, `LOCAL_REVIEW_READY`, …) → Needs you. Done words (`PUBLISHED`, `APPROVED`, `USER_REVIEWED_GOOD`) → Done. Cold words (`PARKED`, `REJECTED`, `QUARANTINED`) → Parked. An open CALL always moves a row to Needs you. A row a lane is working moves to In the machine. A stale `open_calls` entry or a `CALL_REQUIRED` head keeps a row in Needs you forever, so retire dead calls in the registry.

**Variants on the row page.** `registry.row_batch` reads the row's own `deliver/variants/<newest stamp>/varNN.mp4`; "N of 10 rendered" is counted from the `cast_varNN.json` files. Each variant gets Watch / Keep / Kill / note plus a batch note; one Send writes `ui-batch-verdict-rowNN`. Files are resolved **within the row's own folders** only (L0118), so a bare `v011.mp4` can never be matched to another row.

**Grader** (`/grader`, `server/grader.py`). The reviewer grades footage one key at a time. A clip plays at 1.5× from its first ungraded **5-second segment** (clips ≤7 s are one piece; otherwise `round(dur/5)` equal pieces). Keys 1/2/3 = **HERO / BROLL / NEVER** for that piece; 7/8/9 = grade the rest of the clip; whole-clip identity **ME / NOTME**. Queue order: new intake → untagged clips → faces. Every tap is appended to `grades.jsonl`. Folding rule: the latest line per (stem, t0, t1) per field wins, and a null never erases. **Never keep grades in browser localStorage**: an earlier static grader lost its grades that way.

### 3.8 The registry

`<STATE>/REEL_REGISTRY.json`: key `reels` (one object per row, `sequence` = row number) plus top-level rule keys (passes, doctrines, calls, intakes, truth fixes). It grows large (megabytes); load it with Python, never `cat` it.

**Write rules:**
- Write only under an exclusive `fcntl.flock` on `REEL_REGISTRY.json.lock`, after copying a timestamped `.bak`. Do a fresh `json.load` inside the lock, write to `.tmp`, then `os.replace`.
- For one-row patches use the helper: `cd <tools dir> && <PYTHON> -m onetoone.registry_row <registry> <seq> <patch.json> --tag <tag>`.
- Build and Audit phases never write the registry. Deliver does, and only after the audit's findings are fixed.
- `review_state` is a `__`-joined string whose first segments are machine-read, e.g. `LOCAL_REVIEW_READY__V017_DELIVERED__PENDING_HUMAN` or `USER_REVIEWED_GOOD__APPROVED__V010_HUMAN_APPROVED_<date>__DRIVE_UPLOAD_WITHHELD__PUBLISH_NOT_APPROVED`. A truth fix prepends `<VERDICT>_BY_OPERATOR__<recorded words>__<old state>`.

```bash
<PYTHON> - <<'EOF'
import json, os
d = json.load(open(os.path.expanduser('<STATE>/REEL_REGISTRY.json')))
for r in sorted(d['reels'], key=lambda r: r['sequence']):
    print(r['sequence'], r.get('reference_shortcode'), str(r.get('review_state'))[:90])
EOF
```

### 3.9 The one-to-one kit (`code/reel-production-tools/onetoone/`)

The production toolkit. Not an installed package: run it as `<PYTHON> -m onetoone.<module>` **from the directory that contains `onetoone/`**, with an interpreter that has pytest (L0051: a second interpreter without pytest made the kit-test gate silently useless). Full module reference: [05_CODE_GUIDE.md](05_CODE_GUIDE.md). In short:

| Module | Job |
|---|---|
| `preflight` | Gate before any render and before `finished`. Internal disk ≥20 GB and work volume ≥100 GB (decimal GB, `shutil.disk_usage(...)/1e9`; never judge with `df`, L0011/L0036/L0108), free memory ≥25 %, kit pytest, cast rules, identity, lessons harvest + brief. With `--cast` it writes a render token `<STATE>/.preflight-ok/<sha16>.json` keyed on the cast sha and lessons sha (valid 6 h). With `--finish` it blocks while the row's lesson inbox is untriaged. |
| `identity` + `identity_pool.json` | Every cast slot declares `"identity": "<protagonist>"` or `"none"`. Identity slots may only use the settled pool or clips graded HERO/ME (inside a HERO segment); never a fully blacklisted stem or an in-point inside a banned span. `python -m onetoone.identity <cast.json>` must print OK. |
| `grades` | Reads `grades.jsonl` live: NEVER = banned, HERO/ME = settled for identity, BROLL/NOTME never on an identity slot. |
| `refpeople` | Face detection over the reference → `brain/refpeople.json`; every shot where the reference shows a person is an identity slot. Face-only, so backs and silhouettes are checked by eye (L0110). |
| `castscan` | Candidate search per slot, filtered and ordered by grades. |
| `render` | The kit render; refuses without a fresh preflight token (bypass only `REEL_NO_GATE=1` for throwaway tests). `--allow-low-contrast` is banned in lanes. |
| `housechain` | The single approved colour pipe (LUT through a fixed float pipeline; see [02_CRAFT_PLAYBOOK.md](02_CRAFT_PLAYBOOK.md)). |
| `ffx` | Every ffmpeg/ffprobe call goes through `python -m onetoone.ffx -- <args>`, which holds the machine-wide lock `/tmp/reel-one-ffmpeg.lock`. |
| `captions_typeset`, `ornate_env`, `measure_devices`, `devices`, `devicesheet`, `bedprobe`, `refit`, `refstrip`, `lumacheck`, `yuvexact`, `sharpness`, `motion`, `framing`, `looksheet`, `grade`, `grades` | Captions rasterised in Python (Pillow + fontTools) on the reference's measured boxes and composited — **ffmpeg drawtext/libass is not used**; measured entry devices; bed readability probe; framing constraints; look sheets. |
| `faceid` | Font workflow: ink-crop → identify → find/fetch → fit (overlay proof) → `trace` (rebuild letterforms from reference ink for matching and analysis). |
| `registry_row` | Flocked single-row registry patch. |

Kit tests: `cd <tools dir> && <PYTHON> -m pytest -q onetoone/tests`.

### 3.10 Learning loop

- Files in `<STATE>`: `LESSONS.md` (human), `lessons.json` (machine + harvester's `seen` set), `LESSONS_INBOX.md` (raw signal).
- Tool: `lessons.py harvest` · `brief [--row N] [--tags …]` · `add --row N --source … --symptom … --rule … [--check <pytest id>] --tags …` · `triage <in_id> --lesson Lnnnn | --none "why"` · `check [--row N]` (exits 1 while untriaged) · `rewrite`, `setcheck`, `swept`.
- **Harvest runs inside preflight**; no separate timer job is needed.
- A lane that repeats a listed lesson has failed by definition. Every audit finding and every reviewer complaint becomes a lesson; where code can check it, the lesson gets a kit test named as its `--check`. Details: [03_QA_SYSTEM.md](03_QA_SYSTEM.md).

### 3.11 Footage library and workbench

- `<STATE>/FOOTAGE_LIBRARY.json` indexes every clip (content-hash ids, probe data, machine-guessed tags). **Tags are guesses; identity comes only from grades and the identity pool.** A query tool (`library_find.py`) answers role/world/energy queries.
- Physical files under `<FOOTAGE>`: `masters*/` (original log masters, the **only** render source), proxies (640 px, for search and review only), `boards/<id>.jpg` contact boards, `person_scores.json`, `usage.json`.
- New masters are named `masters/<sha256(proxy)[:16]>__<Name>` so a proxy can always be traced to its master.
- Plan for around 150 GB of library per ~750 4K log clips, and a 1 TB work volume. **Never put large output on a small system disk.**

### 3.12 Machine-safety guards

- `whisper_guard.py`: the only permitted speech-recognition entry point. Global lock file; models `tiny`/`base`/`small`/`medium` only (`large*` refused); refuses if free memory <6 GiB or another ASR process exists.
- `vocal_guard.py`: the same idea for source separation (demucs), max 40 s per call.
- `/tmp/reel-one-ffmpeg.lock` via `onetoone.ffx`: one ffmpeg at a time, machine-wide.
- If several agent sessions share one machine, give them a shared coordination convention (locks, a heavy-job semaphore, load ≤2× CPU cores, disk floors).

### 3.13 Control files (`<CONTROL>`)

| File | Meaning |
|---|---|
| `authority.json` | **The kill switch.** `{"schema_version":1, "authority":"…", "factory_state":"FACTORY_OPEN_<tag>", "mutations_allowed":true, "opened_by":"<who and why>"}`. To close: set `factory_state` to anything not starting with `FACTORY_OPEN` (e.g. `FACTORY_CLOSED_<tag>`). Keep timestamped backups. Reopening is a human decision. Legacy `reelctl` expects the exact four-key FACTORY_OPEN object, so a non-matching value makes reelctl mutations fail with `GLOBAL_HALT` (intended when reelctl is retired). |
| `orderd.lock`, `orderd.lane2.lock`, `orderd.lane3.lock` | One JSON per running lane: session, receipt, bundle, kind, row, started, log/result/prompt paths, model, permission mode, `login_paused_at`. Deleted by finalize. |
| `orderd.tick.lock` | Tick mutex (empty file). |
| `orderd.PAUSED` | Present = no new lanes. |
| `orderd.backoff` | `{until, until_utc, reason, evidence, receipt}`. |
| `orderd.state.json` | Last idle reason. |
| `asker.state.json` | asker's pushed/offset/auto/ready map. |

Examples are in `config/control/`. Replace the `opened_by` text with your own.

### 3.14 caption-learning (`code/caption-learning/`)

An offline measurement toolkit for caption parity, built after captions shipped that self-consistency checks passed but a human could not read. It is not in the live order path; run it to audit deliverables or to calibrate thresholds.
- `sweep.py`: factory-wide caption parity sweep over current deliverables — W1 words (declared vs the reference's own OCR read-back vs the delivered read-back; the reference doubles as a control on the reader), plus ink, presence and anatomy gates.
- `readback.py`, `wordtruth.py`: plate/crop OCR ensemble — "is this the declared word, legibly?"
- `anatomy.py`: glyph-topology comparison (severed strokes, fused letters, closed counters) that area statistics like Dice miss.
- `inkcheck.py`: per-frame ink cleanliness on the delivered composite (separation ≥25 luma and Michelson contrast ≥0.80× the reference's own for the same state).
- `bandpresence.py`, `basediff.py`: geometry-free "are the captions there at all?" probes (the second compares the delivered file against its own caption-free picture base).
- `verify_red_capability*.py`: proves the gates can actually go red by corrupting copies of passing states (random erasure, then realistic stroke thinning and letter fusion).
- `calibrate_*.py`, `w1_reclass.py`, `w3_rescore.py`, `merge_w5.py`, `finalize.py`, `postfix.py`, `report.py`, `analysis.py`: calibration, re-scoring, provenance-preserving merges and the human work-order report.

### 3.15 Legacy reelctl (`code/reelctl/`)

The earlier pipeline: a signed, deterministic **14-stage** state machine with hash-locked stage receipts, reference-relative QC and an optional local studio web UI (`reelctl serve`, loopback only on `<STUDIO_PORT>`, deliberately no publication endpoint). Stages:

```
REFERENCE_LOCKED → BLUEPRINT_LOCKED → FOOTAGE_INDEXED → FEASIBILITY_REPORTED → SELECTION_LOCKED
→ ASSETS_LOCKED → RENDERED → TECHNICAL_QC → STRUCTURE_QC → VISUAL_QC → LOCAL_REVIEW_READY
→ HUMAN_APPROVED → DELIVERY_APPROVED → DELIVERED
```
Each stage is `deterministic` (a reelctl command completes it from authored inputs), `judgment` (an input is still a `DRAFT` that an agent must author) or `human` (the three approval stages, never run by a daemon). It fails closed when the library cannot supply a required literal role, and refuses any source outside the declared authorized footage root.

In the extracted system reelctl is **retired from production renders** (the kit replaced it) but is still useful as a reference implementation of stage receipts, schemas (`schemas/*.json`), the caption contract, and its test suite. Its colour-profile data and LUT location were reused by the kit. Install: `uv tool install --editable code/reelctl`; health: `reelctl doctor`, `uv run --extra dev pytest -q`.

### 3.16 Retired patterns (do not rebuild)

- **An always-on agent loop kept alive by a healer script.** It cost money while idle and, when the account started refusing launches, the healer relaunched it thousands of times in under two days (8.1). Use orderd's on-demand lanes instead.
- **A self-waking agent loop** (an agent that schedules its own wake-ups) for the factory: same idle-cost problem.
- **Workflow-script lanes run one at a time** by a single long-lived session. Replaced by one interactive session per order.
- **Old variant tools** (`variant_caster.py`, `variant_render.py`, `reuse_map.py`): kept for reference and their tests; new batches use the kit.

---

## 4. How the components talk

### 4.1 Channels

| From → To | Channel | Format |
|---|---|---|
| reviewer → factory | Deck POST → `<BUS>/ui-*.md` | markdown receipt with fixed headers |
| reviewer (phone) → factory | chat bot → asker → `<BUS>/ui-feedback-rowNN-*.md` | RULING receipt |
| chat session → factory | file a `ui-*.md` receipt by hand (exact template) | same as Deck |
| orderd → bus | `busline.py --lane orderd` | ACK/CALL line + status block |
| orderd → lane | prompt file + tmux | text |
| lane → bus | `busline.py --lane orderd-lane` | ACK/CALL line + status block |
| lane → orderd | result JSON file + its own final bus line | `{"outcome",…}` |
| lane → registry | flocked JSON write | row patch |
| orderd → Deck | `factory_status.json` heartbeat; lock files | JSON |
| Deck → reviewer | registry + bus + heartbeat + media index | HTML/JSON |
| grader → kit | `grades.jsonl` (append-only) | one JSON per tap |
| kit → kit | render token in `.preflight-ok/` | JSON keyed on cast sha + lessons sha |

### 4.2 The lane protocol: Build → independent Audit → Deliver

1. **Intake.** Register the row (drops only). Fetch the reference. Measure it into `brain/`.
2. **Build.** Cast, preflight, render with the kit, look at side-by-side sheets.
3. **ONE independent audit.** One sub-agent call. The auditor gets the delivered file paths and the rules, **never the builder's conclusions**. It hashes the delivered file, decodes it with its own tools, and writes `audit*/AUDIT*.md`. Its findings are **the fix list** (L0092). A finding is a defect *class*: re-measure the class on every shot, not just the one named (L0069).
4. **Fix every finding, with proofs.** Each fix gets a targeted machine check on the new delivered bytes (frame compare, readability gate, grades check, colour scan, cut check). The approval card carries a **finding → fix → proof** table. No second full audit round, no CALL about a defect the lane can measure.
5. **Deliver locally.** Correctly named file, approval card, registry entry, lessons triaged, `preflight --finish` passes, result file, one final bus line, exit.

The builder never certifies its own work in prose (see [03_QA_SYSTEM.md](03_QA_SYSTEM.md)).

### 4.3 Exit protocol

A lane ends in exactly this order: write the result JSON → write **one** final bus line (`finished` with `--artifact`, or `called`, or `blocked` with `--why`) → exit the agent. orderd mirrors the state to bundled receipts and frees the slot.

---

## 5. The stage machine

There are three overlapping state machines. Keep them derived from one source each.

### 5.1 Order state (on the bus)

```
            (no ACK/CALL)                 picked_up / running / progress / queued / blocked
  ui-*.md ─────────────────► OPEN ───────────────────────────────────────────► CLAIMED
                               ▲                                                  │
                               │ queued retry whose not_before passed (≤3)        │ finished / called
                               └──────────────────────────────────────────────────┤
                                                                                  ▼
                                                                               CLOSED
  older than 14 days and never answered  ─────────────────────────────────────►  STALE (never auto-picked)
```
The *effective* state is the newest non-`progress` status block. `blocked` stays blocked until a new reviewer receipt arrives; nothing is relaunched automatically.

### 5.2 Lane phases (inside one lane)

```
INTAKE → FEASIBILITY → CAST → PREFLIGHT(--cast) → RENDER → LOOK (side-by-sides) → AUDIT (one)
      → FIX + PROVE → DELIVER (registry, card) → LESSONS TRIAGE → PREFLIGHT(--finish) → RESULT → BUS → EXIT
```
Any phase may end the lane early with `called` (one short question) or `blocked` (with a reason such as a disk floor).

### 5.3 Row state (registry `review_state` head → Deck bucket)

```
REGISTERED__INTAKE_PENDING
   → LOCAL_REVIEW_READY__V<NNN>_DELIVERED__PENDING_HUMAN[__DRIVE_UPLOAD_WITHHELD]      (Needs you)
       → reviewer REJECT/NOTES/frame note → new lane → next version                    (In the machine)
       → reviewer APPROVE → USER_REVIEWED_GOOD__APPROVED__V<NNN>_HUMAN_APPROVED_<date>
                             __DRIVE_UPLOAD_WITHHELD__PUBLISH_NOT_APPROVED              (Done)
           → batch order → VARIANTS_READY__<stamp>__PENDING_HUMAN → Keep/Kill
   → PARKED / REJECTED / QUARANTINED                                                    (Parked)
```
An open CALL always puts the row in Needs you; a running lane always puts it In the machine.

### 5.4 The four gates

| Gate | Who sets it | Recorded as |
|---|---|---|
| technical PASS | machine gates (preflight, render checks, decoded checks) | gate numbers on the approval card |
| agent visual PASS | the independent audit, after every finding is fixed and proven | audit file + finding→fix→proof table |
| human approval | the reviewer, in the Deck | `operator_approval_vNNN_<date>` with words verbatim and the approved file's sha256 |
| publish approval | the reviewer, per file, explicitly | never inferred from approval; default WITHHELD |

---

## 6. The life of one order

### Step 0 — Is the factory allowed to run?
```bash
cat <CONTROL>/authority.json                 # factory_state must start with FACTORY_OPEN
ls <CONTROL>/orderd.PAUSED 2>/dev/null       # must not exist
<PYTHON> <orderd dir>/orderd.py --status
curl -s http://127.0.0.1:<DECK_PORT>/health | python3 -m json.tool
cd <tools dir> && <PYTHON> -m onetoone.preflight --row <NN> --skip-tests   # read the "machine:" line
```
If the switch is closed, the jobs are unloaded or a disk floor is under, tell the human. Do not open the switch, load jobs or delete files on your own.

### Step 1 — Order
- **Deck → Make:** paste one or more reel URLs plus a note (optionally "1:1"); the Deck writes `ui-drop-<utc>.md`.
- **Deck → Order the batch** on an approved row: a `ui-drop` with `ORDER: build a 10-variant alternate batch for registry row NN.`
- **Chat:** the session files the receipt itself with the human's words verbatim and does not build:
```markdown
# ui-drop — input via Reel Deck — <YYYY-MM-DDTHH:MM:SSZ>

Sent by: <account> (OPERATOR) — relayed from chat by <session>

Operator note (verbatim):

<the exact message, including the link>

Relay notes (facts, not orders):
- <mode if 1:1 was asked, footage hints given, row this replaces>
---
Factory: treat exactly like operator chat input (same authority). After acting, append `ACK — <lane> — <utc>` below this line. Append-only.
```
Save as `<BUS>/ui-drop-<YYYYMMDDTHHMMSSZ>.md`, then confirm: `orderd.py --once --dry-run` lists it; within 1–2 min `grep -E '^(ACK|CALL)' <receipt>` shows "picked up".

### Step 2 — Pickup
Within 60 s orderd writes `picked_up`, starts `reel-lane-<utc>`, writes lock and prompt. The Deck card moves to In the machine, or shows "QUEUED · all 3 lanes busy · place N".

### Step 3 — Intake (inside the lane)
1. **Register the row** under the registry lock + `.bak`: `{sequence: max+1, reference_shortcode, reference_url, mode: grammar_adapt|1to1, review_state: 'REGISTERED__INTAKE_PENDING', project_root, intake_<date>: {order_file, authority, note, scope: 'LOCAL ONLY until an independent audit returns PROVEN - no upload, no scheduling, no posting'}}`. Post a `progress` line naming the sequence.
2. **Fetch the reference** with a current `yt-dlp`, without cookies or logins, and only where the platform's terms allow downloading: `yt-dlp --no-cookies --no-config -o '<row dir>/ref/ref.%(ext)s' '<url>'`. A "login required" error is **not** proof of a wall: re-test a known-good control URL with `--simulate`; if the control also fails, upgrade yt-dlp and retest both. Only then may a CALL quote the result with the yt-dlp version (L0040/L0043).
3. **Measure into `brain/`:** cut grid (each cut verified on frames n-1, n, n+1; L0054), caption states (text, box, per-letter ink, timing), entry devices (`onetoone.measure_devices`), people per shot (`onetoone.refpeople`), audio (see [02_CRAFT_PLAYBOOK.md](02_CRAFT_PLAYBOOK.md) for music rights).
4. **Identify every caption face** with `onetoone.faceid` (L0055). Method and licensing: [02_CRAFT_PLAYBOOK.md](02_CRAFT_PLAYBOOK.md).
5. **Feasibility.** Measure what the library can and cannot match. Search by shoot date and clip-number series and look at the boards before declaring footage absent (L0072). Grammar-adapt when 1:1 is infeasible and the order allows it.

### Step 4 — Casting and gates
Every slot declares its identity; identity slots come from the settled pool or HERO/ME-graded windows (the in-point **and** the whole played window inside a HERO segment, L0090); re-run the identity check against **live** grades before every render (L0087); framing keeps the whole head with ≥6 % headroom (L0113); crop offsets even on 10-bit 4:2:2 masters (L0039/L0041/L0042). Then:
```bash
<PYTHON> -m onetoone.identity <cast.json>                      # must print OK
<PYTHON> -m onetoone.preflight --row <NN> --cast <cast.json>   # must print PREFLIGHT: OK; writes the render token
```

### Step 5 — Build
```bash
<PYTHON> -m onetoone.render <brain> <cast.json> <ref.mp4> <row dir>/work/vNNN.mp4 <row dir>/work/rNNN --audio <licensed track> --crf 20
```
House grade, Python-typeset captions, reference geometry, ffmpeg only through `ffx`, ASR only through `whisper_guard`. Look at reference|ours side-by-side sheets for every caption state and every shot before the audit. Decoded checks on the delivered file: every reference cut must peak in frame difference on the right frame (L0053/L0076), per-glyph readability (halo ratio ≥3.0, L0109), a magenta scan, OCR of every 2nd–4th frame for unwanted third-party text (L0073/L0080).

### Steps 6–7 — One independent audit; fix every finding with proofs
See 4.2.

### Step 8 — Deliver (local only)
- File name **`reel<NN>-<CODE>-<MODE>-v<NNN>.mp4`** in the row's `deliver/` (never a bare `vNNN.mp4`, L0118), plus `APPROVAL-CARD-v<NNN>.md` (what changed, the audit file, the finding→fix→proof table, stems used, disclosures, gate numbers). `chmod 444` once delivered (L0100).
- Registry entry `vNNN_delivery_<date>` (files + sha256, local path, base version, order, changes, audit, gates, `drive_upload: WITHHELD`, `outward: none`) and head `LOCAL_REVIEW_READY__V<NNN>_DELIVERED__PENDING_HUMAN__DRIVE_UPLOAD_WITHHELD`.
- `lessons.py harvest`, then `add` + `triage` (or `--none "why"`) for every inbox item of the row; `preflight --row <NN> --finish` must pass.
- Result file → one final `--state finished --artifact <file>` → exit.

### Step 9 — orderd closes the lane
It sees `finished`, captures the pane, kills the session, mirrors state to bundled receipts, frees the slot. asker pushes "Row NN ready to review".

### Step 10 — Review
Approve · Reject (reason required) · notes · frame note · question-card answer. Each becomes a receipt picked at P1/P2 ahead of any new drop.

### Step 11 — Approve
A short lane verifies the approved file's sha256 against the registry and records the approval (disposition, receipt, words verbatim, file, sha, the four gates with **publish approval NOT given**, uploads WITHHELD). No rebuild.

### Step 12 — Anything outward
Uploading to shared storage or publishing anywhere happens only on an explicit, per-file human go, with an approval card first (destination, file + sha256, caption, settings), a read-back afterwards, and a registry record. The factory ships no publishing automation.

### Variant batch lifecycle
1. Order: `ORDER: build a 10-variant alternate batch for registry row NN.` (or `ORDER: FINISH …`).
2. The lane reads the approved cast and writes `cast_var01..10.json` in `deliver/variants/<stamp>/`: every slot keeps frame count, role and identity declaration and is recast from a different stem; a unique hook per variant; ≥70 % of slots differ between any two variants; no stem within 3 clip numbers of the approved stem in that slot (L0101); the scarcest slot is sized first (L0107); every pick screened at 960×540 (L0091/L0105).
3. Render each variant with the kit; per variant: readability per frame, decoded colour scan, captions and audio byte-identical to the approved version; side-by-side against the **approved version**, not the reference.
4. Deliver `varNN.mp4`, `BATCH_CARD.md`, registry key `variants_<stamp>` (files + sha256), head `VARIANTS_READY__<stamp>__PENDING_HUMAN`.
5. Keep / Kill / note per variant in the Deck → `ui-batch-verdict-rowNN`; a note rebuilds that one variant in place (old file → `superseded/varNN.<utc>.mp4`).

---

## 7. Standing it up

**Honest summary:** the extracted code runs on macOS as shipped; Linux is real work but nothing in the design depends on macOS. The macOS-bound parts are: the default caption face (a system font), `refpeople` (Apple Vision face detection via pyobjc), and the privacy (TCC) permissions an external work volume needs (8.4).

**Gate first.** Keep the kill switch closed while you set up. Never run two orderd instances against the same bus.

### 7.1 Dependencies

| Piece | Tested version | Notes |
|---|---|---|
| OS | macOS 15 | Linux: replace the items marked ⚠ |
| AI coding agent CLI | Claude Code 2.x | Log in **interactively** once on the box (`claude` → `/login`). Lanes run the interactive TUI in tmux. |
| tmux | 3.x | orderd reads `pane_current_command`; keep the "type the command into a shell" launch shape. |
| ffmpeg / ffprobe | 8.x | Needed: `lut3d` (tetrahedral), `colorspace`, `scale` (lanczos), libx264, AAC. `zscale` optional. **drawtext/libass/freetype are not needed by the kit.** A different ffmpeg build changes bytes; gates are reference-relative, so that is acceptable, but log it as a version event. |
| Kit Python | 3.9+ | numpy 1.26, Pillow 11, opencv-python (cv2), fontTools 4.60, scipy 1.13, pytest 8; librosa 0.11 (beat tools); ⚠ pyobjc `Vision` + `Foundation` for `refpeople` on macOS. Make one venv and point `<PYTHON>` and preflight's pytest at it. |
| Whisper | openai-whisper | `medium` max, one ASR process machine-wide, ≥6 GiB free RAM. Fix `whisper_guard.py`'s shebang. |
| Deck runtime | Python 3.11 venv | fastapi 0.141, starlette 1.6, uvicorn 0.52, itsdangerous 2.2, Jinja2 3.1, python-multipart 0.0.32; Playwright for `tests/walk.py`. |
| yt-dlp | current | Keep it current; a stale build produced a false "login wall" once (L0040). Anonymous fetch only, within platform terms. |
| Fonts | — | The default caption face is a system font (Helvetica Neue Bold, TTC index 1 on macOS) referenced in `captions_typeset.py` `FACES["plain"]`. ⚠ On Linux use a font you are licensed to use and change that entry. Fonts used for published work must be licensed or open-licensed (see [02_CRAFT_PLAYBOOK.md](02_CRAFT_PLAYBOOK.md)). |
| LUT | an official LC-709-style `.cube` for your camera's log curve | `housechain.LUT_PACKAGED` is an absolute path. The house grade **is** that one file through that one pipe; hash-lock it and never substitute another cube silently. |
| Chat bot | optional | for asker; config chmod 600. |
| Storage | — | System disk ≥20 GB free; work volume ≥100 GB free (decimal). Plan 1 TB for the work volume. On a single-volume Linux box change preflight to one floor. |
| Remote access | — | Private network / VPN / authenticated tunnel to `127.0.0.1:<DECK_PORT>`. Never public without strong auth. |

### 7.2 Environment variables

| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `ORDERD_BUS` | orderd/asker | `<STATE>/_receipts/<bus-dir>` | bus directory |
| `ORDERD_CONTROL` | orderd/asker | `<STATE>/_control` | control directory |
| `ORDERD_LOGDIR` | orderd | `<STATE>/_receipts/orderd` | logs, prompts, results |
| `ORDERD_BUSLINE` | orderd | `<STATE>/tools/busline.py` | bus writer |
| `ORDERD_CLAUDE` | orderd | `~/.local/bin/claude` | agent CLI |
| `ORDERD_SETTINGS` | orderd | `orderd/claude-settings.json` | lane settings |
| `ORDERD_STATUS` | orderd | `<DECK_DIR>/factory_status.json` | Deck heartbeat |
| `ORDERD_AUTHORITY` | orderd | `<CONTROL>/authority.json` | kill switch |
| `ORDERD_TMUX` | orderd | `/opt/homebrew/bin/tmux` | tmux binary |
| `ORDERD_MAX_LANES` | orderd | 3 (clamped 1..3) | parallel lanes |
| `ORDERD_LANE_WALL_S` | orderd | 18000 | lane wall clock |
| `ORDERD_DEBOUNCE_S` | orderd | 120 | note bundling window |
| `ORDERD_LOOKBACK_DAYS` | orderd | 14 | stale cutoff |
| `ORDERD_SHELL_GRACE_S` | orderd | 45 | "back at shell" grace |
| `ORDERD_PERMISSION_MODE` | orderd | acceptEdits | lane `--permission-mode` (default, acceptEdits, plan, auto, dontAsk) |
| `ORDERD_MODEL` | orderd | `opus` | lane model |
| `ORDERD_TMUX_SOCKET`, `ORDERD_TMUX_SHELL` | tests | unset | sandboxed tmux for tests |
| `ASKER_DECK_URL` | asker | `https://<your-host>:8443` | link in pushes |
| `ASKER_AUTO_MINUTES` | asker | 20 | auto-default delay |
| `ASKER_FRESH_HOURS` | asker | 48 | push window |
| `ASKER_TELEGRAM` | asker | `<NOTIFY_CONFIG>` | bot config path |
| `DECK_CONTROL_DIR` | Deck | `<CONTROL>` | lock-file directory |
| `REEL_NO_GATE` | kit render | unset | `1` bypasses the preflight token (throwaway tests only) |

`orderd.py` also hard-codes `PYTHON = "/usr/bin/python3"`; edit it.

### 7.3 The agent CLI: interactive in tmux, not print mode

- Lanes run the interactive TUI in tmux because (a) headless lanes under a service manager failed with "OAuth session expired and could not be refreshed", and (b) the interactive shape ran reliably under the service manager for weeks.
- **Never put API keys or auth variables in the lane environment.** The prelude is only `PATH`, `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0`, and unsetting `CLAUDECODE` / `CLAUDE_CODE_ENTRYPOINT` (so a lane is its own session, not a child of whoever ran the tick).
- **Token rotation:** with several lanes open the OAuth refresh can race; losers stop at "Login expired". orderd types nothing into the lane: it writes `orderd.PAUSED`, files one CALL and waits. Log in by hand, remove the flag, then press Continue in the Deck.
- **Account refusal** (subscription access disabled, logged out, out of credit) is account-side. orderd answers with one CALL and a 6 h backoff. Only the account owner can fix it.
- **Do not use a self-scheduling agent loop for the factory.** On-demand lanes cost nothing while idle.

### 7.4 Copy list

```
<STATE>/                     registry, bus, LESSONS*, lessons.json, _control/, tools/ (= code/reel-production-tools/),
                             optional workflows/scripts/ (LAW template), LUT
<DECK_DIR>/                  = code/reel-deck/, minus config.json secrets, logs/, index-cache.json (rebuilds)
<FOOTAGE>/                   masters*/ (render source), proxies, boards/, person_scores.json, usage.json
<RENDERS>/reel<NN>-*/        only rows you keep working: brain + deliver/ + casts
user font dir                any Ref*.ttf + .glyphs.json analysis builds, plus licensed faces
```
**Never copy:** registry backup snapshots, agent credentials, `config.json` secrets, the notify config values.

### 7.5 Hard-coded paths to change

Find them with:
```bash
grep -rnE "/Users/|/Volumes/|/opt/homebrew|/System/Library|~/reel-production|~/apps/|127\.0\.0\.1|--port|PORT *=" <REPO>/code <REPO>/config
```
About 360 matching lines across ~130 files in the original tree; the ones that matter:

| File(s) | Hard-coded | Change to |
|---|---|---|
| `config/launchd/*.plist` | home paths, `/usr/bin/python3`, PATH with `/opt/homebrew/bin`, port `7355` | your paths, or systemd units (7.6) |
| `orderd/orderd.py` | `PYTHON`, `TMUX`, `CLAUDE`, `STATUS_FILE`, PATH string, bus dir | env vars in 7.2; edit `PYTHON` |
| `orderd/asker.py` | Deck URL default, notify-config path | `ASKER_DECK_URL`, `ASKER_TELEGRAM` |
| `orderd/lane_prompt.py` | STANDING text naming font paths and the state root; `SCRIPTS` (LAW template dir); registry and identity-pool paths | your text and paths |
| `busline.py` | `BUS` directory | `<BUS>` |
| `onetoone/ffx.py` | `FFMPEG`, `FFPROBE`, lock `/tmp/reel-one-ffmpeg.lock` | your ffmpeg; keep the lock |
| `onetoone/housechain.py` | `LUT_PACKAGED` absolute | path to your hash-locked cube |
| `onetoone/captions_typeset.py` | `FACES["plain"]` (system TTC, index 1), ornate script face path | licensed fonts on the box |
| `onetoone/preflight.py` | `ROOT`, the work-volume mount for the 100 GB floor | `<STATE>`, your volume (or a single floor) |
| `onetoone/render.py` | `GATE_DIR` (`.preflight-ok`), `LESSONS_JSON`, PATH prefix | `<STATE>` paths |
| `onetoone/looksheet.py`, `faceid.py`, `refpeople.py`, `grades.py`, `identity_pool.json` | masters glob under the work volume, font search locations, grades path | your paths |
| `whisper_guard.py`, `vocal_guard.py` | interpreter shebang, lock path, separation venv path | yours |
| `lessons.py`, `library_find.py`, `variant_render.py`, `reuse_map.py` | absolute home / workbench paths | `<STATE>` / `<RENDERS>` |
| `reel-deck/server/{main,board,receipts,registry,grader,lanes,eta,auth}.py` | `BUS_DIR`, `UPLOADS_DIR`, `BOARD_CONFIG`, `FACTORY_STATUS`, `LIBRARY_PATH`, `FOOTAGE_ROOT`, `IDENTITY_POOL_PATH`, `GRADES_PATH`, `WORKBENCH_ROOT` (and the optional `REEL_FACTORY_WORKDRIVE_ALIAS` ↔ mount rewriting), `WORKFLOW_GLOB` (the agent's project-transcript dir, which encodes the home path), `AUDIT_DIR`, `HOWTO_VIDEO`, `INDEX_CACHE` | your paths; `DECK_CONTROL_DIR` env exists for `<CONTROL>` |
| `reel-deck/tests/walk.py`, `cdp_walk.py`, `tools/howto/*` | `https://<your-host>:8443`, output dirs | your URL |
| `caption-learning/*` and its tests | absolute fixture paths to old reference/delivery files | your own fixtures (tests that need them will skip or fail until replaced) |
| `reelctl` (`studio/config.py`, deploy plists, `install.sh`) | storage root, tool PATH, port 7335 | yours |
| registry `project_root` values | absolute work-tree paths | leave history; new rows use your `<RENDERS>` |

### 7.6 Services: launchd and systemd

| macOS job (`config/launchd/`) | Shape | Linux equivalent |
|---|---|---|
| `com.reelfactory.reel-orderd` | `StartInterval 60`, `RunAtLoad`, `AbandonProcessGroup true` (lanes outlive the tick), `ProcessType Background` | `reel-orderd.service` (`Type=oneshot`, `ExecStart=<PYTHON> <STATE>/tools/orderd/orderd.py --once`, **`KillMode=process`** so tmux lanes survive) + `reel-orderd.timer` (`OnBootSec=60`, `OnUnitActiveSec=60`) |
| `com.reelfactory.reel-asker` | `StartInterval 60`, `RunAtLoad` | `reel-asker.service` + `.timer`, same shape |
| `com.reelfactory.reel-deck` | `KeepAlive`, `ThrottleInterval 5`, uvicorn run directly | `reel-deck.service` (`ExecStart=<deck venv>/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port <DECK_PORT>`, `WorkingDirectory=<DECK_DIR>`, `Restart=always`) |

On Linux run all three as user services (`systemctl --user`, with `loginctl enable-linger <user>`). The tmux server lanes start in must belong to the same user.

macOS:
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.reelfactory.reel-orderd.plist   # load
launchctl bootout   gui/$(id -u)/com.reelfactory.reel-orderd                                  # unload
launchctl kickstart -k gui/$(id -u)/com.reelfactory.reel-asker                                # run now
launchctl print gui/$(id -u)/com.reelfactory.reel-orderd | head -30                           # state, last exit
```

### 7.7 Bring-up order

1. Install the toolchain. Run the agent CLI by hand once and log in. If the account refuses, stop.
2. Lay out `<STATE>`, `<DECK_DIR>`, `<FOOTAGE>`, `<RENDERS>`; fix the paths in 7.5; keep the kill switch **closed** (`FACTORY_CLOSED_<tag>`).
3. Tests:
   ```bash
   cd <STATE>/tools && <PYTHON> -m pytest -q onetoone/tests
   cd <STATE>/tools/orderd && <PYTHON> -m pytest -q tests
   cd <DECK_DIR> && <deck venv>/bin/python -m pytest -q tests
   cd <REPO>/code/reelctl && uv run --extra dev pytest -q          # optional, legacy
   ```
4. Preflight: `cd <STATE>/tools && <PYTHON> -m onetoone.preflight --row <NN> --skip-tests`, then without `--skip-tests`.
5. Deck on loopback, then behind your private front door. Create accounts with `tools/deck-user add`. Check that the board renders and media play.
6. Smoke the runner:
   ```bash
   printf '# ui-smoketest — orderd\n\nSent by: you (SMOKETEST)\n' > <BUS>/ui-smoketest-orderd-$(date -u +%Y%m%dT%H%M%SZ).md
   <PYTHON> <STATE>/tools/orderd/orderd.py --once --smoke
   ```
   Expect `picked_up` → `progress` → `finished: smoke test complete, nothing was built`, and the tmux session gone.
7. **Acceptance render:** rebuild one previously approved version from its saved cast and compare frame by frame (grade percentiles, caption boxes). Bytes will differ; the reference-relative gates decide.
8. Open the switch. Load the timers. Drop one reference and watch the first lane end to end.

---

## 8. Failure modes and mitigations

General stance:
- Fix the machine rather than asking the human.
- Never hide a failure behind a green tick.
- Never retry blindly in a loop.
- Prove a wall before calling something blocked.
- Never claim a state you have not checked: read the pane and the bus, not just a status file (L0031).
- Anything visible to others, expensive or hard to undo needs an explicit human go.

### 8.1 Runaway relaunch loops

**The failure pattern.** An always-on agent worker supervised by a healer script that checks every 60 s. If the agent service starts refusing launches, each launch prints the refusal and drops to an idle prompt. A healer that measures "age" from the last *completed* tick sees a stale value, concludes "dead on arrival / wedged" on every check and relaunches. The result is thousands of launches, a lagging machine, and usage consumed by sessions that build nothing.

**Why orderd cannot repeat it.**
- There is no healer. A lane starts **only for an order**, and an order is never relaunched in a loop.
- Any refusal string → **one** `called --why account-refused` + `orderd.PAUSED` (human resumes).
- A usage limit → one `queued` retry 30 min later, at most 3 attempts.
- A lane that dies without a result → `blocked`, and stays blocked until a new human receipt arrives.

**Rules for any supervisor you add:** a refusal branch; age measured from *launch*, not from last success; exponential backoff; a launch counter with a hard ceiling; a test for "a launch that never ticks". If you see repeated launches in `orderd.log`, unload orderd and tell the human.

### 8.2 Account refusal and login problems

| Symptom | Cause | Do |
|---|---|---|
| `CALL — orderd — … account refused the lane launch`; `orderd.PAUSED` reason `account-refused` | subscription access disabled, logged out, credit, expired OAuth | Report it to the human. Do not loop launches, switch accounts or paste API keys. After the human fixes the login, they remove `orderd.PAUSED` and press Continue / resend. |
| A lane pane shows "Login expired", idle | token rotated while several lanes refreshed | orderd pauses (`orderd.PAUSED`, reason `login-expired`) and files a CALL; a human logs in and removes the flag. |
| Someone swaps the agent login while lanes run | kills paused work (L0044) | `orderd.py --stop` first. |

### 8.3 Wedged sessions and stuck lanes

| Symptom | Do |
|---|---|
| A lane ran past the 5 h wall | orderd kills it and writes `blocked --why timeout`. Read the log tail and result JSON. If most work is on disk (e.g. 10 of 10 variants rendered but no card), queue a **FINISH** order naming the folder and what is left. Never restart from scratch. |
| Lane pane sits at a question or permission prompt | The prompt forbids asking. Attach, read, nudge briefly (long tmux text truncates). Prefer `--stop` + a clearer order over babysitting. |
| "Lane exited without delivering" | The pane went back to a shell or the session vanished. Read `lane-<order>-<utc>.log` (pane capture at the end). Fix the systemic cause in the kit/prompt, then resend. |
| Two sessions on one row | orderd never runs two lanes on one row. A chat session must not build a row a lane holds, and must not patch the kit under a running lane without telling it (L0039). |
| Machine-wide freeze of agent sessions | Check `ps` for many agent processes, then memory. Do not kill sessions you did not start; report. |
| Deck shows "fixing N min" while the lane is blocked | The effective state is the newest non-`progress` status block. Write only through busline. |

### 8.4 macOS privacy (TCC): "Operation not permitted" on the work volume

On macOS an external volume can be readable only by processes holding the right privacy grant, and the grant is tied to the executing binary. A tmux server or a launchd job does **not** hold Full Disk Access by itself; if the runner's interpreter lacks the grant, every lane fails at once with "Operation not permitted".

**Fix in place:** grant the runner's interpreter (and the agent CLI binary, and the Deck's Python) the disk permissions it needs — Full Disk Access or Removable Volumes in System Settings → Privacy & Security (L0086). A denied open can **hang** rather than fail, hence the Deck's 6 s threaded probe and the studio's killable read probe.

**Handle it:** if a lane reports the volume unreadable, it is a permission problem, not a broken drive. Check which binary is running and whether it holds the grant. Do not retry blindly. On Linux this class disappears.

### 8.5 OOM, crashes and overload

What went wrong in practice: several parallel speech-recognition processes caused kernel panics on a smaller machine; a whole-library frame-mining job crashed the machine twice; a batch lane died mid-batch when the machine went down.

**Laws (all enforced in code where possible):**
- **One heavy ffmpeg at a time** (`onetoone.ffx`'s machine-wide lock).
- **One ASR process at a time**, `medium` model max, ≥6 GiB free (`whisper_guard`).
- **At most 3 heavy lanes**; never raise `ORDERD_MAX_LANES` above what the machine has proven.
- **Free memory ≥25 %** before render (preflight).
- **Guard the memory footprint, not RSS.** On macOS, compressed and wired memory make RSS misleading; read the system's memory-pressure / footprint numbers.
- No whole-library PNG frame scans; work from proxies and boards, extract frames per candidate.
- Delete frame dumps when the receipt lands. Deleting tens of thousands of files spikes load for minutes; do it when no render is due.
- Run big side jobs one at a time with `nice`; give long renders a long tool timeout (a 30 min default killed a batch).
- After a crash, read the lane logs and queue FINISH orders one at a time; do not relaunch everything at once.

### 8.6 Disk floors

- System disk ≥20 GB, work volume ≥100 GB, in **decimal** GB as preflight prints on its `machine:` line. `df -g` / `df -h` round binary GiB and misled a lane once (105 GB read as "97"), costing an hour (L0011/L0036/L0108).
- A floor wait is not a failure: the lane writes `blocked --why "disk floor …"` with preflight's numbers, and the order is resent after space is freed.
- Free only regenerable scratch (frame dumps, superseded intermediates, old `work/` dirs of finished rows). Never masters, deliverables, audits, approved versions or the footage library. Bulk deletions need a human go.
- Large output always goes to the work volume, never the system disk.

### 8.7 Content refusals and identity

- **Real stops are reported honestly.** If an agent declines a step (for example a content filter on a caption word), report it plainly. Never encode, misspell or re-run to get around a filter.
- **No personal identifiers in deliverables.** A caption that would reveal a person's age, location or other identifier is replaced with a neutral line.
- **Rights questions are legitimate.** If a reference is someone else's distinctive work and you intend to publish a close recreation, ask its creator; use music you hold rights to; license fonts for anything published. See the README's "Rights & licensing".
- **Identity:** never cast a clip that is not provably the intended person into a person slot. The identity pool, the grades and refpeople are the only proof; tags are guesses. New footage stays uncastable on identity slots until graded; say so on the card.

### 8.8 Board and bus misreads

| Symptom | Cause | Fix |
|---|---|---|
| Wrong reel plays on a row | bare file names resolved across rows | L0118 names; row-scoped file resolution in the Deck |
| Delivered batch invisible | batches live in `<row>/deliver/variants/<stamp>/` | the row page reads that folder |
| Row stuck in Needs you | stale `open_calls` entry or `CALL_REQUIRED` head | retire the call in the registry (flock + .bak) with a reason |
| "! no answer" under a chat-filed send | missing `Operator text (verbatim):` header | use the exact receipt template |
| Deck serves old code after an edit | service restart hit the hop, not uvicorn | bootout + `pkill -f 'uvicorn server.main:app'` + bootstrap |
| Media index empty after restart | large file scan | wait; `index-cache.json` warms it |

### 8.9 Talking to the human when something breaks

- Lead with what you saw and what you did, in one or two lines; then the one question, if any, flagged clearly with numbered options.
- Do not re-ask what an earlier ruling answered; read the row's feedback receipts first.
- Re-test a rail before calling it dead; a month-old failure is not current evidence.
- Never write "the factory is rate limited" or "the factory is working" without reading the lane pane and the bus.

---

## 9. Cheat sheet and glossary

```bash
# where are we
<PYTHON> <STATE>/tools/orderd/orderd.py --status
tail -50 <STATE>/_receipts/orderd/orderd.log
cat <DECK_DIR>/factory_status.json
tmux ls | grep reel-lane
ls -t <BUS>/ui-*.md | head -20
<PYTHON> <STATE>/tools/orderd/asker.py --list
curl -s http://127.0.0.1:<DECK_PORT>/health

# act
touch <CONTROL>/orderd.PAUSED                        # pause new lanes (rm to resume)
<PYTHON> <STATE>/tools/orderd/orderd.py --stop       # stop all lanes
cd <STATE>/tools && <PYTHON> -m onetoone.preflight --row NN --skip-tests
<PYTHON> <STATE>/tools/lessons.py check
<PYTHON> <STATE>/tools/busline.py --receipt <abs path> --lane <you> --state progress --text "…"
```

**Glossary**
- **Row:** one reference in the registry (`sequence`).
- **Version** `vNNN`: one delivered render of a row.
- **Variant** `varNN`: a footage-swapped copy of an approved version.
- **Reference:** the reel whose edit is being studied (`reference_shortcode` = its platform code).
- **1:1:** same cuts, captions, typography and timing as the reference.
- **Grammar-adapt:** the reference's editing grammar on your footage when 1:1 is infeasible.
- **Lane:** one agent session handling one order.
- **orderd / asker / busline:** the runner / the phone relay / the bus writer.
- **CALL / ACK:** a question to the human / a status line.
- **House grade / house look:** the single approved LUT pipe plus the approved caption face.
- **HERO / BROLL / NEVER / ME / NOTME:** clip grades (per 5-s segment; whole-clip identity).
- **Settled pool:** clips proven to show the intended person.
- **refpeople:** face detection over the reference.
- **Preflight:** the gate before render and before finished.
- **Kill switch:** `<CONTROL>/authority.json`.
- **Bus:** `<BUS>`, the receipts directory.
- **Workbench:** `<RENDERS>` on `<WORKDRIVE>`.
- **Deck:** the review web app.
- **TCC:** macOS privacy permissions.
