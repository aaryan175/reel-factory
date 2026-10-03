# 07 — Working With an AI Operator

This factory is built to be run by an AI coding agent (Claude Code) acting as the **operator**: it analyses references, casts footage, renders, audits, fixes and delivers locally. The human supplies references, answers the few questions only a human can answer, and gives verdicts. This chapter covers how to set that up, the bootstrap prompt, how to give feedback so it turns into lessons, and the rules that keep an autonomous agent honest and safe.

Placeholders: `<REPO>`, `<WORKDRIVE>`, `<FOOTAGE>`, `<REFS>`, `<RENDERS>`, `<DECK_PORT>`.

---

## 1. The division of labour

| The human | The agent |
|---|---|
| Drops references (URL or file) | Locks the reference, measures it, writes the brain pack |
| Answers CALLs (short numbered questions) | Casts footage, grades, typesets, renders, measures |
| Watches candidates and gives verdicts | Runs the independent audit, fixes every finding, proves each fix |
| Approves each outward action, one at a time | Delivers locally, updates the registry, files lessons |

The human should need only four touches: **references in, CALL answers, review verdicts, outward-action approvals.** Everything else is machine work. If the agent asks something it could have measured, that is a defect, and it should become a lesson.

## 2. Setting up Claude Code

### 2.1 Where it runs

Open Claude Code in your production directory (the folder holding the registry, the bus, `LESSONS.md` and the tools). Put a project `CLAUDE.md` there containing the hard invariants from §4 below; the agent reads it on every session start.

### 2.2 Permissions

Two example settings files ship in the repo:

- `config/reel-production.claude-settings.json`: an allow-list for an **interactive** session (ffprobe, ffmpeg, exiftool and the reelctl interpreter run without prompting). Good for a human-supervised session.
- `code/reel-production-tools/orderd/claude-settings.json`: the **unattended lane** settings: `permissions.defaultMode: acceptEdits`, an empty `permissions.allow` list for the commands you approve for lanes, `autoCompactEnabled: true`, `autoCompactWindow: 350000`. orderd launches lanes with `--permission-mode $ORDERD_PERMISSION_MODE` (default `acceptEdits`).

Bypass mode means the agent can run any command. Only use it when:
- the box runs the factory as an **unprivileged user** whose home holds only the factory;
- no credentials for publishing, cloud storage or payment are reachable from that user;
- the kill switch (`_control/authority.json`) is closed until you have run the smoke tests.

### 2.3 Interactive sessions vs unattended lanes

- **Interactive session.** You and the agent in one terminal. Use it to fix the *machine* (kit code, lane prompt, lessons, Deck), to run side projects, and to answer questions.
- **Unattended lane.** The order runner (`orderd.py`) starts one interactive Claude Code session per order inside tmux, gives it the lane prompt, and closes it when the lane writes its final bus line. One order, then exit.

Do not hand-build a reel version in an interactive chat when the factory is running. If you give a fix in chat, the chat session should **file it on the bus as a receipt** and let a lane build it. That keeps every version traceable to an order, a lane log, an audit and a lesson. Fixing the machine itself is still chat work.

Practical notes on lanes:
- Lanes run the **interactive** CLI inside tmux, typed into a shell rather than passed as the tmux command (so the pane's current command reports correctly). Headless `claude -p` launches from a scheduler were less reliable at refreshing logins; keep headless behind a flag.
- Do not put API keys or auth variables in the lane environment. The lane prelude should set only `PATH`, clear any inherited "I am a child session" variables, and `cd` to the production directory.
- With several lanes open, a login-token refresh can race and leave a lane at a "login expired" prompt even though the credential file is fresh. The runner never types into such a lane: it pauses (writes `_control/orderd.PAUSED`) and files a CALL. Log in by hand, then remove the flag.
- An account-side refusal is not something a retry fixes. The runner raises **one** CALL and backs off 6 h. Never build a relaunch loop.
- Do not use a self-rescheduling loop for the factory; an idle loop still consumes resources. Use the on-demand runner.

### 2.4 Subagents

Use the Agent tool for large independent read-only passes (reference forensics, footage audit, history/intent audit) and, always, for the **independent audit** of a delivered file. Rules:
- Give each subagent a narrow scope and an exact output path.
- Subagents are **evidence collectors, not authorities.** Spot-verify their high-impact claims against the actual media before accepting.
- Never let two agents mutate the same project tree, browser profile or registry row.
- Agents never run Whisper, demucs or ffmpeg outside the guards (§6).

## 3. The bootstrap prompt

Paste this as the first message when starting or resuming a reel project. Fill in `PROJECT INPUTS` first. Use `PHASE=BOOTSTRAP` for the first full understanding pass, `PHASE=CONTINUE` to resume from saved state, `PHASE=EXECUTE` only after all calls are answered. Freeze the prompt before a run: record its SHA-256 in the run's evidence manifest. A changed prompt is a new contract.

````text
You are the lead operator for a repeatable, reference-driven short-form reel production
system. Your job is not to make a generic montage. Your job is to understand the reference,
the footage corpus, the creative intent and the current project state well enough to make
correct editorial decisions and explain every important decision in plain English.

PROJECT INPUTS
```yaml
phase: BOOTSTRAP               # BOOTSTRAP | CONTINUE | EXECUTE
project_id: "<stable-project-id>"
current_revision: "<revision-or-UNKNOWN>"
reference_paths_or_urls:
  - "<exact-reference-file-or-url>"
authorized_footage_root: "<FOOTAGE>"
other_authorized_assets: []    # licensed fonts, licensed audio, LUT
feedback: "<paste new feedback here, or say SEARCH PROJECT FILES>"
output_format: "vertical 9:16"
requested_delivery: "local private review only"
write_policy: BRAIN_PACK_ONLY  # READ_ONLY for audit-only runs
render_authorized: false       # immutable during BOOTSTRAP
external_actions: NONE         # NONE unless separately authorized per item
```
BRAIN_PACK_ONLY permits writes only to the project's brain root and isolated bootstrap
evidence directories. It does not permit mutation of stage state, selection locks, assets,
render outputs, delivery state or anything outside this machine. render_authorized stays
false for BOOTSTRAP regardless of stale project state.

If an input is missing but discoverable from the project files, discover it. Ask only when
the ambiguity changes a creative or irreversible decision.

FIRST, READ: the project CLAUDE.md, LESSONS.md (run `python3 tools/lessons.py brief --row <N>`),
the registry entry for this project, and docs/02_CRAFT_PLAYBOOK.md, docs/03_QA_SYSTEM.md and
docs/06_REFERENCE_FORENSICS.md. The project files, not chat memory, are the source of truth.

TRUTH RULE
Do not say "I watched", "I reviewed everything" or "I understand the reel" unless you have
verified complete coverage of the exact source. For every source record: exact path/URL,
hash, duration, dimensions, whether the full runtime decoded, whether it was reviewed with
audio / muted / frame-by-frame / only via metadata or contact sheets, and what remains
unreviewed. A tool failure, a truncated file, a thumbnail or a technical PASS is never proof
of creative understanding. Label evidence with tiers:
  INDEXED            path/hash/probe receipt exists
  STILL_TRIAGED      representative stills exist; composition claims only
  PROXY_REVIEWED     full proxy watched at normal speed; apparent action claims
  MOTION_VERIFIED    contiguous frames cover the selected interval and its boundaries
  RENDER_CROP_VERIFIED  the final interval, grade, frame count and 9:16 crop inspected at output
Never promote a tier silently.

SAFETY AND SCOPE
- Keep all work local and private. Do not upload, share, publish, replace, send or spend.
- Do not expose secrets in reports.
- Do not invent footage, scenes, captions, fonts, camera metadata or preferences.
- No body/face warps, fake blur, speed ramps, reverse, freezes or loops unless the reference
  and an explicit instruction require that exact operation.
- Preserve raw footage and prior revisions immutably. Work append-only.
- References are study material, never footage sources.
- Never silently switch between grammar-adapt and 1:1 (reference-locked).
- LOCAL_REVIEW_READY is a private review handoff, not approval. HUMAN_APPROVED is not
  DELIVERY_APPROVED.
- Respect rights: use licensed or open fonts and music you have rights to; flag, in one line,
  any publish plan that would need a creator's permission or a licence.

DELEGATION
For large passes use up to three read-only subagents, each writing only to its own file under
<brain-root>/bootstrap-agents/:
  A. Reference forensics: identity/hash, full-runtime description, shot/picture-state map with
     frame ranges, action, composition, camera and subject motion, light, colour, captions,
     effects, audio cues, transitions, ending; mode contracts; unresolved questions.
  B. Footage audit: full inventory of authorized_footage_root with hashes and probe status,
     duplicates, geometry/rotation/SAR/fps/decode, usable intervals, role/action/energy/
     composition/world/crop-safety tags, a bad-clip ledger, missing roles.
  C. Intent audit: project files, prior decisions, accepted and rejected versions, lessons;
     only evidence-backed rules; recurring corrections vs one-off notes; open decisions.
Read their raw output and verify high-impact claims against the media yourself. Reconcile
disagreements explicitly.

CLASSIFY the project on: format/visual family (only if the reference proves it), production
mode (grammar-adapt or 1:1), shot role per block (hook, entry, movement, environment, action,
detail, social moment, work context, escalation, reset/transition, payoff, loop return), and
visual world (subject and appearance policy, location family, day/night, wardrobe/props,
source texture, colour family, screen direction). A filename is not a role.

REQUIRED ANALYSIS: answer "what exactly is in the reel?" at five levels: meaning, structure,
picture (every shot/state), layers (captions, typography, effects, transitions, audio, beat
relationship, compositing order), and production decision (which authorized interval performs
each role, why, and what would make it fail). Use one row per block:
  block_id | ref frames/timecode | role | visible_action | action_phase | subject/setting |
  composition/crop | camera_motion | subject_motion | light/colour | text/effect/audio state |
  candidate_source_hash | source_interval | evidence_path | quality_flags | decision | confidence
Judge candidates by the selected interval in the final 9:16 crop at normal speed and frame by
frame, never by filename or thumbnail.

CALLS
Do not interrupt for routine decisions. Raise a call only when a decision needs human
preference, new footage, a mode choice or acceptance of a material compromise. Stop before the
affected render and write:
  CALL_REQUIRED
  call_id / severity (BLOCKER|DECISION|WARNING) / project_id / revision / reference_block
  issue_type: missing-role | bad-quality | excessive-motion | unsafe-crop | wrong-world |
              wrong-action | duplicate | colour | typography | audio | mode-ambiguity |
              appearance-policy
  WHAT THE REFERENCE REQUIRES / WHAT I FOUND (path, hash, interval) / WHY IT DOES NOT MATCH /
  EVIDENCE (paths) / OPTIONS (1 = ..., 2 = ..., 3 = ...) / MY RECOMMENDATION / REPLY WITH
The one-line version shown to the human is at most 320 characters with numbered options.
Never raise a call about something you can measure. Stuck twice on the same blocker: raise a
call and move to other work; do not loop.

DURABLE OUTPUTS under one brain root (<RENDERS>/<project_id>/brain/):
  00_PROJECT_BRAIN.md        01_REFERENCE_CONTRACT.json   02_SHOT_BLUEPRINT.json
  03_FOOTAGE_REGISTRY.json   04_FEASIBILITY_MATRIX.md     05_SELECTION_SHORTLIST.md
  06_MISMATCH_LEDGER.md      07_CALL_PROMPTS.md           08_DECISION_LOG.md
  09_RESUME_STATE.json       10_EVIDENCE_MANIFEST.json
Every revision preserves the previous manifest, render, decision log and review state.

EXECUTE PHASE (only after calls are answered and render_authorized is true):
  1. Write the cast file; run `python3 -m onetoone.identity <cast>`.
  2. Run `python3 -m onetoone.preflight --row <N> --cast <cast>`; it must print PREFLIGHT: OK.
  3. Render through onetoone.render (one ffmpeg at a time, via ffx).
  4. Look at the side-by-side sheets against the reference.
  5. Run ONE independent audit with a separate subagent on the delivered bytes. Its findings
     are the fix list. Fix every finding and prove each fix with a machine check at the cited
     frame. Do not self-certify.
  6. Deliver locally: reel<NN>-<SHORTCODE>-<MODE>-v<NNN>.mp4 plus an approval card.
  7. Triage this row's lesson inbox; run `python3 -m onetoone.preflight --row <N> --finish`.
  8. Update the registry row (locked write + backup) and write one final bus line via
     tools/busline.py.

COMPLETION: report exactly one of READY_TO_EXECUTE | CALL_REQUIRED | BLOCKED | INCOMPLETE,
then: what this reel is; classification; what is locked; what does not match (block IDs);
calls; one exact next action; evidence paths. No generic reassurance.
````

### 3.1 Continuation message

Once the brain pack exists, resume with:

> Continue `PROJECT_ID=<id>` from the durable project brain. Read and hash-verify all eleven brain files (`00_` to `10_`). Re-verify the active revision and every referenced source/output hash against the manifest. Do not re-derive project truth from chat. Resolve any open calls first; otherwise continue only the next executable stage. Keep `render_authorized: false` unless this phase authorizes rendering. Keep all work local and report evidence paths, not claims.

### 3.2 End-of-session report

Ask every session to end with:

```
FACTORY STATUS: <phase> — <one line>
Done this session: <bullets with receipt paths>
Evidence: <paths>
Calls for the human: <cards or "none">
Next action: <one exact executable step>
```

## 4. Hard invariants (put these in the project CLAUDE.md)

1. **No render outside a project** with a locked reference and a declared authorized footage root. "Quick demo" is not an exemption.
2. **Authorized footage only.** Reference downloads and other creators' videos are study material, never sources.
3. **Four separate gates:** technical PASS ≠ agent visual PASS ≠ human approval ≠ publish approval.
4. **Append-only.** Never overwrite an approved baseline. Rejections stay on disk as negative fixtures with receipts.
5. **Big output goes to `<WORKDRIVE>`**, never the small system disk.
6. **Outward actions** (uploads, cloud-drive changes, publishing, spending) need an exact preview and explicit human approval **per action**.
7. **Stall rule:** stuck twice on the same blocker means one CALL, not a loop.
8. **Learning loop:** harvest feedback into the inbox, triage into lessons before `finished`, run preflight before any render.
9. **Bus lines only through `tools/busline.py`**, closed state set: `picked_up · running · progress · queued · blocked · finished · called`; `queued` and `blocked` need `--why`.
10. **Resource law:** Whisper only via `whisper_guard.py`, demucs only via `vocal_guard.py`, ffmpeg only via `onetoone.ffx`.

## 5. Giving feedback so it becomes lessons

The factory is meant to be recursive: every comment feeds back so the same mistake is not made twice. Your feedback is the most valuable input in the system, so make it easy to turn into a rule.

### 5.1 Where feedback goes

- **In the Deck** (preferred): press **Approve**, **Reject** (a reason is required), or pin a **frame note** at the exact moment that is wrong. Each press writes a receipt on the bus; the runner turns it into an order. A burst of notes on one row is bundled into one round after a 120 s debounce.
- **In chat**: the chat session files your words verbatim as a feedback receipt for the right row. It does not hand-build the fix.
- **Answering a CALL**: reply with the row and the option number (`<row> <option>`, optionally followed by extra words). Your words go into a ruling receipt verbatim.

### 5.2 What makes feedback actionable

| Include | Example |
|---|---|
| Which reel and version | "row 12, v004" (or use the Deck, which knows) |
| Where | a frame note, or "at 0:03", or "the second clip" |
| What is wrong, as you see it | "the caption is hard to read on the bright background" |
| What you want instead, if you know | "use a different shot" |
| Whether it is a one-off or a rule | "always", "never", "from now on" turn a note into a standing rule |

You do not need to diagnose the cause. The lane must **measure the complaint first** (find the frame, measure luma, caption contrast, cut timing, whatever applies) before changing anything, and the fix must be proven on the new bytes.

### 5.3 How the agent turns it into a lesson

```
feedback receipt ──► lessons.py harvest ──► LESSONS_INBOX.md (raw, untriaged)
                                                   │ lane touching that row must triage
                                                   ▼
            lessons.py add --row N --source <receipt> --symptom "..." --rule "..." [--check <test>]
            lessons.py triage <inbox_id> --lesson <Lxxxx>   |   --none "why no lesson"
                                                   │
                                                   ▼
                    LESSONS.md / lessons.json  ──►  a test pins the rule where it can be measured
                                                   │
                                                   ▼
            preflight refuses render / finish while the row's inbox has untriaged items
```

- A lesson has a **symptom** (what went wrong), a **rule** (what to do instead) and, where measurable, a **check** (the test that enforces it).
- `lessons.py brief --row N` prints the lessons a lane must read before building.
- **A repeated lesson is a failed audit.** If the same complaint comes back, the lesson was not enforced; add or fix the test.
- APPROVE means mark approved, **no rebuild**. REJECT or NOTES means measure, fix, re-audit, redeliver as a new version.

## 6. Never self-certify

The builder never decides its own work is good.

- **One independent audit per delivery.** A separate subagent reads the delivered file (not the builder's notes) and lists every measurable defect at a cited frame. Its findings are the fix list, not suggestions.
- **Finding → fix → proof.** Each fix is proven by a machine check at the cited frame on the new bytes (luma, contrast ratio, box position, frame count, cut frame, hash). "Looks fixed" is not proof.
- **Evidence over claims.** Never say indexed, watched, passed or fixed without a receipt on disk. Read the actual pane or file, not just a status line.
- **No fake green.** A gate that could not run is WITHHELD, not PASS. A technical PASS never implies a creative PASS, and neither implies human approval.
- **Prove a wall before calling it a wall.** Before declaring something impossible or blocked (a login wall, a missing font, an unavailable shot), demonstrate it with a test; tooling problems (a stale downloader, a missing package) are fixed, not escalated.
- **Workers are not authorities.** Subagent claims are verified against the media before they drive a decision.

## 7. Outward-action gating

The machine may build, audit, fix, deliver **locally**, and ask. It may never approve on the human's behalf, post, upload, buy, or quietly drop a work item.

| Action | Gate |
|---|---|
| Local render, audit, local delivery | Allowed (inside the stage rules) |
| Cloud-drive upload, rename, replace | Exact preview + explicit human approval for **that file** |
| Publishing anywhere | Never done by the factory or its agents: a human posts by hand, outside the factory, after reviewing the **approval card** |
| Spending money | Explicit human approval; never auto-answered |
| Exposing the Deck beyond localhost/private network | Explicit human approval, with authentication on |
| Opening the kill switch on a new machine | Explicit human approval; never two runners on one bus |

The **approval card** for a publish lists: destination, file path and SHA-256, caption text, cover frame, schedule, and platform settings. After an approved upload or post, **read it back** (remote size/hash, the live post) and record the result in the registry.

The question relay (`asker.py`) may auto-take an option marked `(recommended)` after 20 minutes **only** for internal creative choices. It never auto-answers anything about spending, posting, cloud-drive changes or buying.

## 8. Concurrency

- **Kill switch.** `_control/authority.json` `factory_state` must start with `FACTORY_OPEN` for the runner to start lanes. A missing or unreadable file counts as closed.
- **Pause.** `touch _control/orderd.PAUSED` stops new lanes; running lanes continue. Remove it to resume.
- **Lanes.** At most 3 at once (`ORDERD_MAX_LANES`), **never two on one row**. Each lane has a 5 h wall clock.
- **Priority.** Rejections first, then notes/frame notes, then continue requests, then new drops; oldest first within a priority. Orders older than 14 days are not auto-picked.
- **Locks.** Registry writes take a file lock and write a timestamped `.bak` first. Bus writes are appended under `flock`. Any existing project lock directory must be checked before mutating a project.
- **One heavy process at a time where it matters.** One ffmpeg machine-wide (the kit's lock), one ASR process machine-wide, at most 3 heavy lanes. No whole-library PNG frame dumps; delete frame dumps once the receipt lands. Check free disk before building.
- **Interactive vs runner.** If an interactive session and a lane could touch the same row, the interactive session files a receipt and lets the lane do it, or pauses the runner first. Never race another operator on the same project.
- **One runner per bus.** When moving machines, close the switch on the old box before opening it on the new one.

## 9. Runner controls cheat sheet

```bash
cd <REPO>/code/reel-production-tools/orderd
python3 orderd.py --status           # slots, locks, tmux sessions, backoff, pause, kill switch, open orders
python3 orderd.py --once --dry-run   # what the next tick would pick; writes nothing
python3 orderd.py --once             # one tick by hand
python3 orderd.py --once --smoke     # plumbing-only lane on the newest smoke-test receipt
python3 orderd.py --stop             # stop every lane; each order gets "blocked: stopped by hand"
python3 asker.py --list              # open questions
python3 asker.py --dry-run           # print what would be sent; send nothing

python3 ../busline.py --receipt <abs path> --lane <name> --state <state> --text "<one sentence>" [--why ...]
python3 ../lessons.py harvest | brief --row N | check --row N
python3 -m onetoone.preflight --row N --cast <cast.json>   # run from code/reel-production-tools
```

Configuration is by environment variable (`ORDERD_MAX_LANES`, `ORDERD_LANE_WALL_S`, `ORDERD_MODEL`, `ORDERD_DEBOUNCE_S`, `ORDERD_BUS`, `ORDERD_CONTROL`, `ORDERD_SETTINGS`, `ORDERD_TMUX`, `ORDERD_CLAUDE`; `ASKER_DECK_URL`, `ASKER_AUTO_MINUTES`, `ASKER_FRESH_HOURS`). See [01_ARCHITECTURE.md](01_ARCHITECTURE.md) for the full list and the stand-up order.
