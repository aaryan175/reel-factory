# 03 — The QA System

How the factory decides that a reel is ready to show a human, and how every mistake it makes
becomes a rule that a machine enforces next time.

This document covers:

1. [The four gates](#1-the-four-gates-never-merged) — and why they are never merged
2. [The one path a reel takes](#2-the-one-path)
3. [The independent audit protocol](#3-the-independent-audit-protocol)
4. [Measure at the cited frame](#4-measure-at-the-cited-frame)
5. [Preflight](#5-preflight-the-gate-before-every-render-and-before-finished)
6. [The decoded-bytes checklist](#6-the-decoded-bytes-checklist)
7. [Tests that enforce lessons](#7-tests-that-enforce-lessons)
8. [How to design a gate](#8-how-to-design-a-gate-meta-rules)
9. [Delivery and the approval card](#9-delivery-and-the-approval-card)
10. [The learning loop](#10-the-learning-loop)
11. [Questions to the human (CALLs)](#11-questions-to-the-human-calls)
12. [Checklists](#12-checklists)
13. [Top mistakes and the rule that fixes each](#13-top-mistakes-and-the-rule-that-fixes-each)

Related documents: the craft rules being checked live in [`02_CRAFT_PLAYBOOK.md`](02_CRAFT_PLAYBOOK.md);
every lesson cited here as `L00NN` is written out in [`04_LESSONS.md`](04_LESSONS.md); module and
CLI details are in [`05_CODE_GUIDE.md`](05_CODE_GUIDE.md); the runtime (order daemon, lanes, bus,
Deck) is in [`01_ARCHITECTURE.md`](01_ARCHITECTURE.md); running it with an AI agent is in
[`07_WORKING_WITH_AN_AI_OPERATOR.md`](07_WORKING_WITH_AN_AI_OPERATOR.md).

Placeholders used below: `<REPO>` (this repository checkout), `<WORKDRIVE>` (the large work volume
holding row work trees and renders), `<FOOTAGE>` (your authorized footage library), `<REFS>`
(downloaded reference reels), `<RENDERS>` (render and delivery output). "The kit" means the
one-to-one kit at `code/reel-production-tools/onetoone/`. "The reviewer" is the human who owns the
footage and approves reels. "A row" is one reel project (one reference, its versions, its notes).

---

## 1. The four gates, never merged

```
technical PASS  ≠  agent visual PASS  ≠  human approval  ≠  publish approval
```

| Gate | Who sets it | What it means | What it does NOT mean |
|---|---|---|---|
| **Technical PASS** | Machine checks on the decoded delivered file (geometry, frame count, audio integrity, cuts, colour sanity, readability, size) | The file is structurally what the brain says it is | That it looks right, or that anyone likes it |
| **Agent visual PASS** | One independent audit agent, comparing the delivered file to the reference | An agent that did not build it found no open defect, or every finding has been fixed and proven | Creative approval |
| **Human approval** | The reviewer only (an APPROVE in the Deck, or their explicit words) | The reviewer has watched it and accepts it | Permission to upload or post it anywhere |
| **Publish approval** | The reviewer only, per file, per destination | This exact file may go to this exact destination | Permission for any other file or destination |

The rule is enforced in code: `reelctl.qc.combine_authorities()` (in `code/reelctl/src/reelctl/qc.py`)
takes four separate inputs — `technical`, `structure`, `visual`, `human` — and only computes
`publishable = machine_pass and human == "APPROVED"`. A machine verdict can make a reel
`LOCAL_REVIEW_READY`; it can never make it `APPROVED`. Any `FAIL` among the machine authorities, or
a human `REJECTED`, makes the whole thing `REJECT`.

Rules that keep the gates apart:

- **A passing test never sets creative approval.** Every audit report ends with a line saying so
  explicitly ("technical verification only; confers no creative or publish approval").
- **Only the reviewer approves.** Review-tool logins can carry different authority (for example an
  "editor" role whose notes are advisory vs a "reviewer" role whose verdicts are binding). Decide
  this once, write it down, and do not let two templates disagree about it.
- **Never turn an ambiguous message into an approval.** "It's good" can be a complaint about one
  part ("it was good before"). A question like "are you ready to send this?" is not an approval to
  send. When in doubt, ask.
- **Delegated approval is not self-approval.** Even if the reviewer once said "use your judgment",
  an agent never marks its own work approved.

---

## 2. The one path

Exactly one path may render a reel. Anything that skips a step is out of process, however small
the change ("quick demo" is not an exemption).

1. **Order.** A drop in the Deck, a reference link from the reviewer, or a frame note. Nothing else
   starts a reel.
2. **Intake.** Register the row, fetch the reference (respecting the platform's terms; see §8
   "fetch diagnosis"), lock its hash/raster/rate/frame count, write the intake receipt.
3. **Feasibility.** Measure what the footage library can and cannot match. If a 1:1 remake is
   infeasible, ask (CALL) and wait; switch to grammar-adapt only on the reviewer's word.
4. **Cast** from the identity pool (`python3 -m onetoone.identity <cast.json>` prints OK).
5. **Build** on the house look with captions. `python3 -m onetoone.preflight --row NN --cast <cast.json>`
   must print `PREFLIGHT: OK` before any render. ffmpeg only through `onetoone.ffx`; transcription
   only through `whisper_guard.py`.
6. **One independent audit** reads the delivered bytes (§3).
7. **Fix every finding, with proofs** on the delivered bytes.
8. **Local delivery** into the row's `deliver/` with its approval card.
9. **Human review** in the Deck. The verdict is recorded in the registry in the reviewer's own words.
10. **Lessons harvest**: `lessons.py harvest`, triage the row's inbox into `LESSONS.md`,
    `preflight --finish`, then — and only then — the orchestrator writes `finished`.
11. **Outward moves** (cloud folder upload, posting) happen only on the reviewer's explicit go for
    that file. Never as part of the lane.

A kill switch (`config/control/authority.json`) can close the factory entirely; nothing renders
while it is closed. Check the file, don't assume its state.

---

## 3. The independent audit protocol

### 3.1 Never self-certify

The builder never tells the reviewer a reel is "1:1" or "done". Every candidate gets an
independent, read-only audit before it is called anything, and **the audit's verdict line is what
the reviewer is told**, with every open finding named (L0009).

Why: a builder that checks its own work checks what it intended, not what it shipped. The first
time this was skipped, a version described as "1:1 by eye" turned out to have four hard defects
(a loosely matched script face, a plain word set inside a script word, the wrong number of rooms,
a legible background plate).

### 3.2 Exactly one audit per lane

- The lane makes **one** audit agent call (L0092). The auditor receives the delivered file paths and
  the law (the rules it audits against) — **never the builder's conclusions**. It hashes the
  delivered file and verifies the bytes with its own decoders.
- **Its findings are the fix list.** Fix every finding (hard and soft), prove each fix with a
  targeted machine check on the new delivered bytes, put a **finding → fix → proof** table on the
  approval card, and deliver.
- No second full audit. No "audit 2 of 4". No fix-round counting. No question to the reviewer about
  a defect the lane can measure and fix.
- **Never stop on a budget.** An earlier version of the rules capped lanes at three fix rounds and
  four audits (L0081); lanes then stopped on "budget spent" with fixable defects open. The counting
  was removed entirely. Equally: an unaudited build is never delivered, and never handed to the
  reviewer to audit by eye.
- For a **variant batch** (one approved brain, many footage casts): machine checks per variant on
  the delivered bytes, one batch audit, no audit rounds.

### 3.3 What the auditor does

- **Read-only.** It never edits the build, never uploads, never posts.
- **Every number comes from decoded bytes.** No card text, build script or receipt is taken on
  trust. If the card says "readability 3.4", the auditor decodes the file and measures it.
- **An audit finding is a defect CLASS, not a frame** (L0069, L0075). If burial is found on one
  shot, the fixer re-measures burial on every shot, every caption state, every frame.
- **An audit's proposed fix is a hypothesis** (L0114). "Move the crop centre to cy 0.45" must be
  worked out (cy down = picture moves down; cx down = picture moves right), its predicted effect
  stated, and the result proven on decoded bytes. Applying an audit's crop blindly once moved a
  face straight under a caption.
- **Before the audit, the builder LOOKS.** Build reference | ours side-by-side sheets for every
  caption state's settled frame and every shot, and read them. If a person would say "different
  font", "different colour" or "that shadow is not in the reference", it is not ready for the
  auditor.

### 3.4 Finding → fix → proof

Every row of the table on the card has three parts:

| Finding | Fix | Proof |
|---|---|---|
| What the audit measured, where (shot, state, frame) | What changed in the brain/cast/kit | The machine check on the NEW delivered file that would have caught it, with its number (e.g. "worst glyph 3.21 ≥ 3.0 on all 77 frames of state c04") |

A fix without a machine proof is not a fix; it is prose (L0009).

### 3.5 When an audit agent refuses

Content filters can stop an agent describing footage. Do not reword the material to get past a
filter. Raise a question to the reviewer with the filter's text.

---

## 4. Measure at the cited frame

Human notes are the most valuable signal and the easiest to mishandle.

- **A colour or text complaint is never ruled "not a defect"** (L0013). Measure ours vs the
  reference at the reviewer's cited frame with the kit, move it, and put before/after numbers on
  the card. (Mean RGB outside the caption band is the usual measure, L0095.)
- **A "soft caption" complaint is measured before it is acted on** (L0038):
  `onetoone.sharpness.edge_width_px` on the decoded frame at the cited time, vs the reference, the
  previous version and an approved reel. If every number says the ink edge is ideal, do not touch
  the letterforms; look at the bed (grade, exposure) under the caption instead.
- **Convert playback times to frames** at the reel's rate before looking anything up.
- **Resolve "the second clip" against the cut list** (shot order after every cut, content checked
  on a decoded still), not against the pinned frame (L0115).
- **Classify the note.** Colour, font, casting and caption complaints are separate defects, each
  measured separately (L0015).
- **Change only what was named.** A clip or framing the reviewer praised, or did not complain about,
  is locked (L0093). "Go back" means rebuild the previous version's exact chain — prove it
  string-equal — and change only the named item (L0102).

---

## 5. Preflight: the gate before every render and before `finished`

```bash
cd <REPO>/code/reel-production-tools
python3 -m onetoone.preflight --row NN --cast <cast.json>          # before ANY render
python3 -m onetoone.preflight --row NN --cast <cast.json> --finish # before writing `finished`
```

Source: `code/reel-production-tools/onetoone/preflight.py`. Any failure exits 1 and prints
`PREFLIGHT: BLOCKED` with one line per failure.

### 5.1 What it checks

| # | Check | Thresholds / behaviour | Lessons |
|---|---|---|---|
| 1 | **Machine floors** | internal disk ≥ 20 GB free, `<WORKDRIVE>` ≥ 100 GB free (decimal GB, `shutil.disk_usage / 1e9`), free memory ≥ 25 % | L0011, L0036 |
| 2 | **Kit tests** | `pytest -q -x onetoone/tests` must pass, run with an interpreter that actually has pytest | L0051 |
| 3 | **Cast rules** | pure white ink `[255,255,255]` with no `caption_shadow` is refused (white vanished on bright beds; ruled all-white casts use `[245,245,250]` on beds proven dark enough by the per-frame gate); shadow radius > 20 is refused (reads as a glow); unknown `grade_mode` refused | L0025, L0033 |
| 4 | **Identity** | `onetoone.identity.check_cast`: every slot declares identity (`operator`/`none`), identity slots only from the settled pool, no blacklisted stem or in-point inside a banned span, live clip grades applied | L0052, L0065, L0087 |
| 5 | **Lessons** | runs `lessons.py harvest`, prints the row's brief; with `--finish`, blocks while the row's inbox has untriaged items | L0026, §10 |

### 5.2 The preflight token

On success with `--cast`, preflight writes a token keyed on the cast's sha256 into a gate
directory. `onetoone.render.require_preflight()` refuses to render unless that token:

- exists,
- is younger than `GATE_MAX_AGE_S` (6 h),
- matches the cast file's bytes, **and**
- matches the sha256 of the current **lesson set** (not the file bytes — bookkeeping rewrites must
  not invalidate tokens; adding or changing a lesson must).

So **adding a lesson invalidates every outstanding preflight**. That is the recursion: a new
lesson forces every lane to re-pass the gate under the new rules. The only bypass is an explicit
environment variable (`REEL_NO_GATE=1`) for throwaway local tests — never a casual CLI flag.

### 5.3 What preflight does NOT check (still lane duties, with receipts)

Freshness (setup repeats), privacy/third-party text, headroom, per-glyph burial, cut alignment and
readability on the decoded file. These are in the decoded-bytes checklist (§6) and the audit.

### 5.4 Preflight traps

- **Run it immediately before every render.** Disk floors move with swap, not only with files;
  a reading from earlier in the round is stale (L0029).
- **The preflight `machine:` line is the only disk authority.** `df -g` reports binary GiB rounded
  down (105 GB reads as "97") and blocked an allowed round for an hour (L0036, L0108).
- **A floor wait is not a build failure** and never consumes a retry attempt; park and re-check
  (L0036).
- **"kit tests FAILED: ?" with an empty tail** is a tooling defect (the interpreter has no pytest),
  not a real failure (L0051, enforced by `test_preflight_kit_tests_interpreter.py`).

---

## 6. The decoded-bytes checklist

Run on the **delivered** file, decoded with player-exact conversion (`onetoone.yuvexact`; a naive
swscale rgb24 decode reads about 2 levels dark). Every row reports its sample size.

| Check | Pass bar | Lessons |
|---|---|---|
| Geometry / rate / frame count | equals the reference (e.g. 1916×1078, 24000/1001, exact frame count); yuv420p, tv range, bt709 | — |
| Size | under the configured upload size cap (default 10 MiB); encode at crf 20–21 (never 18) | L0117 |
| Audio integrity | the muxed track is bit-exact to the licensed source you chose: packet md5, per-packet framemd5, extradata and decoded PCM md5 identical | — |
| Stream durations | video and audio stream durations agree; never `-shortest` (it truncated picture by 2 frames) | — |
| Cuts | at every reference cut c, frame diff(c) > diff(c+1); picture decodes to exactly the cut-grid frame count; no stray cut; reviewer-added cuts land | L0053, L0076 |
| Colour sanity | 0 magenta/green-dominant frames; per-shot magenta/green share gain ≤ 0.40 vs its own master (`CHROMA_JUMP_MAX`) | L0041 |
| House grade | each shot equals master + `house_vf` within roughly 0.1–1.1 mean RGB | L0037 |
| Warmth consistency | per-shot mean B−R vs the opening shots; no cool swing against a warm look | L0058, L0060 |
| Luma / clipping / crush | clipped share and near-black share reported per shot; on good builds Y < 16 ≤ ~0.05 %, Y > 235 ≤ ~0.18 % | L0001, L0005 |
| Readability | per glyph, per decoded frame, WCAG ratio of ink vs the band 4–8 px outside the glyph ≥ 3.0, **entry frames included**; raw shadow-free figure reported beside it | L0030, L0104, L0106, L0109, L0117 |
| Burial | per-glyph visible ink vs the reference on every state and frame, reference-relative: ours ≥ min(0.60, reference − 0.05) | L0068, L0069, L0075, L0083 |
| Ink sharpness | 10–90 % edge width ≤ 2.0 px; traced faces within 0.1 px of the reference | L0038, L0098 |
| Face vs words | face-detector boxes × caption word boxes = 0 overlaps | L0103, L0115 |
| Headroom | whole head in frame with ≥ 6 % headroom on every frame (head top = face box top + 0.45 × face height; CUT < 0, TOUCH < 0.02) | L0113 |
| Privacy screen | OCR every 2nd–4th decoded frame + native-resolution crops of busy backgrounds: 0 legible third-party signage, logos, screens, credentials, strangers | L0073, L0080, L0103 |
| Identity | identity check OK against live clip grades over each slot's whole played window | L0087, L0090 |
| Freshness | no repeated camera setup between neighbours; reuse disclosed | L0015, L0071 |
| Publish-only: banding | sky / flat gradient patches keep ≥ ~25 distinct luma levels | L0008 |

---

## 7. Tests that enforce lessons

Every lesson that code can check gets a kit test named as its `--check` in the ledger. The test
suite is part of preflight, so a test-backed lesson **physically blocks rendering** when violated.

Run the suites:

```bash
cd <REPO>/code/reel-production-tools && python3 -m pytest -q onetoone/tests
cd <REPO>/code/reel-production-tools && python3 -m pytest -q orderd/tests
cd <REPO>/code/reel-deck            && python3 -m pytest -q tests
cd <REPO>/code/caption-learning     && python3 -m pytest -q tests
cd <REPO>/code/reelctl              && python3 -m pytest -q
```

### 7.1 The lesson → test map

Paths are relative to `code/`. Some test files carry the internal project number they were written
against in their names; the names are kept so the ledger's references resolve.

| Lesson | What it enforces | Test |
|---|---|---|
| L0002 | Shadow/outline is applied after the entry device, per frame | `reel-production-tools/onetoone/tests/test_v011_fixes.py::test_shadow_is_taken_from_the_frame_the_device_drew` |
| L0003 | Exposure pull curve carries a shoulder (can reach clipping) | `onetoone/tests/test_lessons_grade.py::test_L0003_pull_curve_carries_a_shoulder` |
| L0004 | Tolerance scales with the target | `onetoone/tests/test_lessons_grade.py::test_L0004_tolerance_scales_with_the_target` |
| L0005 | Second gamma pass floors higher than the first | `onetoone/tests/test_lessons_grade.py::test_L0005_second_pass_floors_higher_than_the_first` |
| L0006 | Highlight pull leaves the midtone subject alone | `onetoone/tests/test_v011_fixes.py::test_highlight_pull_leaves_the_subject_alone` |
| L0010 | Push-ins use zoompan, not a fixed-size crop | `onetoone/tests/test_v011_fixes.py::test_push_and_keys_use_zoompan_not_a_fixed_size_crop` |
| L0025, L0030 | The readability gate counts the shadow the viewer sees, and fails real cases | `onetoone/tests/test_lessons_grade.py::test_readability_gate_counts_the_shadow_the_viewer_sees` |
| L0033 | Flat white on a bright bed fails; ruled directional shadow passes; weak symmetric glow fails | `onetoone/tests/test_caption_shadow_gate.py` |
| L0037 | House chain reproduces an approved frame; bare LUT does not | `onetoone/tests/test_house_chain_and_sharpness.py::test_house_chain_reproduces_the_approved_cold_open_and_bare_chain_does_not`, `test_house_pipe_render.py` |
| L0038 | Sharpness floor accepts hard edges and rejects soft ones | `onetoone/tests/test_house_chain_and_sharpness.py::test_sharpness_floor_accepts_hard_edges_and_rejects_soft_ones` |
| L0049 | Face-sweep output names are collision-free | `onetoone/tests/test_face_sweep_naming.py` |
| L0051 | Preflight runs kit tests with an interpreter that has pytest | `onetoone/tests/test_preflight_kit_tests_interpreter.py` |
| L0052, L0087 | Identity slots only from the settled pool; unruled stems rejected; tests isolate live grades | `onetoone/tests/test_identity_pool.py` |
| L0055 | A part that names its face is set in that face | `onetoone/tests/test_named_face_and_state_shadow.py::test_a_part_that_names_its_face_is_set_in_that_face` |
| L0056 | A caption state can wear no shadow | `onetoone/tests/test_named_face_and_state_shadow.py::test_state_can_wear_no_shadow` |
| L0064 | A traced face sets the source glyphs; missing glyphs reported, not guessed | `onetoone/tests/test_faceid_trace.py` |
| L0065 | Clip grades are identity law; reference-people windows drive identity slots | `onetoone/tests/test_grades_law.py` |
| L0066 | No caption state repeats a part text | `onetoone/tests/test_part_text_unique.py` |
| L0067 | Traced closed letters keep their counters | `onetoone/tests/test_built_face_counters.py` |
| L0074 | Device measurement uses the reference's own frame count | `onetoone/tests/test_grades_law.py::test_measure_devices_counts_the_reference_frames` |
| L0077 | Giant script words are set from metrics past the box-fit cap | `onetoone/tests/test_giant_script_two_run.py` |
| L0084 | No unchecked low-fit blur frame in a device curve | `onetoone/tests/test_low_fit_blur_overridden.py` |
| L0089 | No flat ink on a low-fit (footage-filled) part | `onetoone/tests/test_footage_filled_ink_not_flat.py` |
| L0021 | A blocked pickup reads as queued, not "fixing" | `reel-deck/tests/test_app.py::test_blocked_pickup_reads_as_queued_not_fixing` |
| L0027 | A queued pickup reaches the board card (tested at the surface) | `reel-deck/tests/test_app.py::test_queued_pickup_reaches_the_board_card` |
| L0035 | A file named only in a dict key is still declared | `reel-deck/tests/test_app.py::test_a_file_named_only_in_a_dict_key_is_still_declared` |
| L0036 | Structured status blocks beat prose on the board | `reel-deck/tests/test_app.py::test_structured_status_blocks_beat_prose` |
| L0118 | A row never plays another row's file | `reel-deck/tests/test_app.py::test_a_row_never_plays_another_rows_file` |
| L0059 | A Deck button is wired only when the runner and lane prompt handle it | `reel-production-tools/orderd/tests/test_variants_order.py::test_prompt_uses_the_variants_brief` |
| L0070 | Two lanes run side by side, never on one row | `orderd/tests/test_orderd.py::test_two_lanes_run_side_by_side_never_on_one_row` |
| L0079 | A question to the reviewer is short with numbered options | `orderd/tests/test_busline_call_shape.py` |
| L0086 | The runner's interpreter holds the disk permissions it needs | *setup checklist (01_ARCHITECTURE.md 8.4); no kit test* |
| L0076 | One-frame shots survive the concat | *proposed: `test_concat_keeps_one_frame_shots` — not yet written* |

Some lessons are enforced by a per-row machine check rather than a kit test (for example L0098's
"IoU ≥ 0.83 on untraced frames", L0100's "measure tools refuse non-JSON output paths", L0103's
"OCR brand words = 0, face × word boxes = 0", L0109's per-glyph legibility JSON). Promote those
into kit tests when you port the system.

**Everything else in the ledger is "rule only — no test yet".** Those are the rules most likely to
be broken again; lanes must read them in the brief, and a porting effort should turn as many as
possible into tests.

### 7.2 The pattern

```
failure observed  →  lesson written (symptom, rule, tags)
                  →  a test that FAILS on the old behaviour and passes on the fix
                  →  lessons.py setcheck L00NN <pytest id>
                  →  the test runs inside preflight  →  render refuses until it passes
```

A test that was never seen to fail on the real bad case does not count (see §8).

---

## 8. How to design a gate (meta-rules)

- **A gate must be shown to FAIL something real before it is trusted**, and its measure must vary
  across the states of a real build (L0030). A shadow-aware readability gate once passed everything
  because it measured the opaque shadow instead of the footage.
- **One authority per gate.** Disk floors are what preflight prints, never `df` (L0036, L0108).
- **Test at the surface the human reads**, from a replayed real receipt, never only at the parser.
  The same applies to any flag that crosses a module boundary (L0027).
- **Every check reports its sample size.**
- **A checker that reimplements a render chain must import the renderer's builder**, or it drifts
  and produces false failures (L0045).
- **Prove "works" with an acceptance file**, one line of evidence per claim (L0034).
- **Never bypass a gate to build.** Never re-run a lane to defeat a readability refusal; the
  low-contrast override (`--allow-low-contrast`) is a question to the reviewer with the numbers,
  never a lane decision (L0023).
- **Measure tools must be safe.** Any measure tool refuses an output path that is not `.json`, is
  under `deliver/`, or is an existing file > 5 MB; deliverables are `chmod 444` the moment they are
  written (L0100). A measure script with swapped arguments once overwrote a delivered video with
  two bytes.
- **Review sheets are built by decoding each input separately and indexing frames in Python**, each
  tile labelled with its frame number; never trust a one-liner `select` after `hstack` (L0100).
- **Verify UI state by computed visibility in a real browser**, not by the template attribute
  (L0012). A "test mode" banner once showed on every page because a CSS `[hidden]` rule was missing.
- **Watch a change to the supervisor for one full cycle** on the live process before calling it
  done; read the live session output, not the status file, before telling the human a state (L0031).
- **Gates are reference-relative where the reference itself would fail a flat number** (L0068):
  ours ≥ min(0.60, reference − 0.05).
- **Fetch diagnosis uses a control** (L0040, L0043). A failed reference download is checked by
  running the same anonymous command on a reference that fetched before. If the control fails too,
  it is the tool or network (update the downloader and retry). Only a reel that fails while the
  control passes is a real wall. Never use cookies or credentials without explicit permission, and
  respect the platform's terms of service for downloading.

---

## 9. Delivery and the approval card

### 9.1 Naming and registration

- File name: `reel<NN>-<SHORTCODE>-<MODE>-v<NNN>.mp4`, registered under that exact name. Never a
  bare `vNNN.mp4` — the review app once resolved bare names across rows and played one row's video
  on another's page (L0118).
- The review app resolves a row's files only from that row's own folder (`deliver/` first).
- **"It's on the row" is proven by asking the review app what it serves**
  (`board.row_detail(NN)['current']`) before any finished line claims it (L0035). Registry deliveries
  name the review file in a value (`'file': '<name>.mp4'`).
- Version labels match files (no v003 inside a "v002" folder).

### 9.2 What the card carries

The card is written in plain words a human can read in a minute — not a dump of internal notes.

- the audit's verdict line and the path of the audit report;
- the **finding → fix → proof** table (L0092);
- every disclosure:
  - clip reuse across reels (L0015) and in-point repeats within a batch (L0107);
  - substitutions, and any recast forced by updated clip grades (L0102);
  - caption parts with a reference fit below the trust threshold, with rendered width vs the
    reference ink box (L0078);
  - every non-white reference ink (L0057);
  - shadow usage per caption state, with the raw shadow-free contrast figure (L0096, L0113);
  - third-party text, signage or logos removed (L0073);
  - device frames overridden to settled values (L0084, L0111);
  - the cut-check JSON (L0076);
  - clipped and near-black share per shot (L0001, L0005);
- gate numbers (readability per state, size, crf).

Sanitize absolute home paths in any report copied into `deliver/` (L0053).

### 9.3 Who ends the lane

- **Only the orchestrator writes `finished`**, and only after Deliver succeeds and
  `preflight --finish` passes (L0050). A Fix-phase or Build-phase agent writing `finished` early makes
  the board lie.
- A Deliver-phase prompt opens by stating that build and audit are ALREADY DONE, then gives a bounded
  bookkeeping checklist. Prepending the full build law made a low-effort Deliver agent read it as a
  multi-hour rebuild and bail (L0046).

### 9.4 Local first, append-only

- **No upload and no posting without the reviewer's explicit go for that file.** Delivery is local:
  the row's `deliver/`, the registry entry, the review app row. Record cloud upload as `WITHHELD`
  and outward actions as `none`.
- **Append-only.** Never overwrite an approved baseline. Rejected versions stay on disk as negative
  fixtures with their receipts.
- **Retire a lane script the moment its base version is superseded** (L0022). A stale template once
  rebuilt a new version from an old base and would have overwritten registry state.

---

## 10. The learning loop

The system is meant to be recursive: every comment, verdict, ruling, rejection, question and audit
finding becomes a durable lesson, and a lesson that code can check becomes a test that blocks the
next render. A repeated lesson is a failed audit.

### 10.1 Files

| File | Role |
|---|---|
| `LESSONS.md` | Human-readable ledger, append-only |
| `lessons.json` | Same lessons, machine-readable, plus the harvester's `seen` set |
| `LESSONS_INBOX.md` | Raw, untriaged signal (reviewer text, audit verdicts, HARD findings, questions) |

### 10.2 The tool

`code/reel-production-tools/lessons.py`:

```bash
python3 lessons.py harvest                                   # scan the bus + audit reports for new signal
python3 lessons.py brief [--row NN] [--tags grade,captions]  # what a lane must read, newest first
python3 lessons.py add --row NN --source "<audit file>" --symptom "..." --rule "..." [--check <pytest id>] [--tags a,b]
python3 lessons.py triage <inbox_id> --lesson L00NN          # or: --none "why no lesson"
python3 lessons.py check [--row NN]                          # exit 1 while the row has untriaged items
python3 lessons.py setcheck L00NN <pytest id>                # attach a test to an existing lesson
python3 lessons.py rewrite                                   # regenerate LESSONS.md from lessons.json
python3 lessons.py swept [--reason ...] [--row NN]           # list auto-triaged items
```

The harvester recognises reviewer text by a fixed receipt header (`Operator text (verbatim):` or
`RULING on …:` in the shipped code — keep whatever header you choose stable), `CALL —` lines,
`VERDICT` blocks and `HARD-n` findings. Writes are under a file lock.

### 10.3 The loop

```
reviewer note / audit HARD finding / question / verdict
        │  (filed verbatim as a receipt on the bus)
        ▼
lessons.py harvest  ──►  LESSONS_INBOX.md
        │
        ▼  (the lane that touches the row)
triage: add a lesson (symptom → rule → enforced-by)  or  --none "why"
        │
        ├──► code can check it?  write a test that fails on the old behaviour
        │                         lessons.py setcheck L00NN <test>
        ▼
lesson set changes  ──►  every outstanding preflight token is invalid
        │
        ▼
next render must re-pass preflight (kit tests incl. the new one)
        │
        ▼
preflight --finish blocks `finished` until the row's inbox is triaged
```

Harvest runs inside preflight, so a lane cannot render without pulling in new signal.

### 10.4 Rules for the loop itself

- **A lesson that overturns doctrine is not filed until every older lesson, the agent's standing
  prompt and the lane templates that say the opposite are edited in the same sitting** (L0026). grep
  the ledger and the brief for the old doctrine's words before closing it. Otherwise the next lane
  cites the old lesson to justify the old mistake — this happened within minutes once.
- **Mark superseded lessons in place** ("NARROWED by …", "SUPERSEDED by …") rather than deleting them.
- **Stale re-briefs are doctrine too.** A context-restore note that still names a retired rule will
  re-inject it on every compaction. Audit those notes when doctrine changes.
- **State is re-derived from the bus, never carried in a self-written summary.** A summary paragraph
  once silently dropped an open question for hundreds of wake-ups.
- Every lesson names: what went wrong, the rule that prevents it, and how it is enforced (a test
  path, a machine check, or "rule only — no test yet").

---

## 11. Questions to the human (CALLs)

A CALL is the only way a lane asks the reviewer something.

- **Shape** (L0079, enforced by `test_busline_call_shape.py`): ONE short question, ≤ 320 characters,
  with numbered options (`1 = …, 2 = …`). The bus writer refuses an essay or an optionless question.
  The review app shows only the newest open question, as one card with option buttons next to the
  comment box. Flag it unmistakably ("QUESTION FOR YOU:").
- **Close answered or retired questions** on the bus AND in the registry, or they haunt the row as
  "waiting on you".
- **When to ask:** a real decision only — footage the library does not hold; an ungraded clip a
  slot needs (L0110); a dark-ink-vs-bed trade-off (L0060); a reference colour that fails the
  readability gate (L0057); the low-contrast override (L0023); a content-filter block; a genuine
  fetch wall after the control test (L0040); rights questions you cannot resolve yourself (see
  [`README.md`](../README.md#rights--licensing)); anything that spends money or acts outwardly.
  Stuck twice on the same blocker → ask, don't loop.
- **Never ask about** a measurable defect the lane can fix, an audit budget (there is none), or
  research the agent can do itself.
- **Timed auto-answers** (if you push questions to a phone and take the recommended option after a
  timeout) must never apply to spending, posting, buying, or uploading.

---

## 12. Checklists

### 12.1 Pre-render

- [ ] The order is real (Deck drop / reference link / frame note filed on the bus). You are a lane,
      not an ad-hoc chat session.
- [ ] Bus and factory status checked; no kit edit while a round is running (L0039).
- [ ] Reference locked: hash, raster, rate, frame count; cut grid from decoded frames, streak cuts
      checked at n−1 / n / n+1 (L0054); caption state frames 0-based from the reference's own
      ink-per-frame curve (L0098).
- [ ] Reference-people pass run; person slots eye-checked in the reference and in our clip (L0065,
      L0110).
- [ ] Faces identified from the reference pixels (or built), overlay proofs saved, tracking model
      recorded (L0055, L0064, L0116). Fonts you publish with are licensed or open.
- [ ] Reference inks measured per state; footage-filled ink identified (L0057, L0089).
- [ ] Cast: settled-pool clips for identity slots, live grades, whole played windows, no repeated
      setup, fresh, warm against the opening, face visible, headroom (L0052, L0071, L0087, L0090,
      L0113).
- [ ] Whole cast eye-screened at ≥ 960×540 with the crop applied: logos, signage, screens,
      strangers, stray hands (L0091, L0105).
- [ ] Bed probe + readability gate on every frame of every caption state, entry frames included,
      after the final grade (L0033, L0104).
- [ ] Shadow only where the gate requires it, applied after the device; bed fixed first (L0002,
      L0056, L0096).
- [ ] `python3 -m onetoone.identity <cast>` OK and `python3 -m onetoone.preflight --row NN --cast <cast>`
      `PREFLIGHT: OK`, run immediately before the render (L0029, L0036).
- [ ] One ffmpeg (`onetoone.ffx`), one ASR process (`whisper_guard.py`), memory-footprint guard on
      any big job.

### 12.2 Pre-delivery

- [ ] Decoded-bytes checklist (§6) green on the DELIVERED file.
- [ ] Side-by-side sheets read by the builder before the audit.
- [ ] One independent audit; every finding fixed and proven; finding → fix → proof on the card
      (L0092).
- [ ] Card disclosures complete (§9.2); home paths sanitized (L0053).
- [ ] File named `reel<NN>-<SHORTCODE>-<MODE>-v<NNN>.mp4`, ≤ 10,000,000 bytes, crf 20–21,
      `chmod 444` (L0100, L0117, L0118).
- [ ] Registry names the file in a value; the review app serves it as `current` (L0035).
- [ ] Inbox triaged into `LESSONS.md`; `preflight --finish` OK; only then the orchestrator writes
      `finished` through the bus writer (L0050).
- [ ] Nothing uploaded, nothing posted, without the reviewer's explicit go for that file.

### 12.3 On feedback

1. File the note verbatim on the bus with the stable receipt header. Don't build from a chat.
2. Work out which clip/frame is meant: cut list first, playback time → frame (L0115).
3. Change ONLY what was named; everything praised or not mentioned is locked (L0093, L0102).
4. Classify colour / font / casting / caption separately (L0015).
5. Measure at the cited frame before and after (L0013, L0095).
6. Font note naming another row → copy that row's face and tracking model exactly (L0048, L0116).
   "Go back" → rebuild the previous chain exactly, proven string-equal (L0102).
7. Fix the whole defect class, not the frame (L0069).
8. If you need the reviewer: one ≤ 320-char question with numbered options (L0079).
9. Harvest and triage the note into a lesson before `finished`.

---

## 13. Top mistakes and the rule that fixes each

Each of these actually happened while the system was being built. Ordered roughly by cost.

| # | The mistake | The rule now | Lessons |
|---|---|---|---|
| 1 | A version was reported as "1:1 by eye"; an independent audit then found four hard defects | Never self-certify; one independent audit; its verdict line is what the human hears | L0009, L0092 |
| 2 | Several rounds chased "our normal grade" with curves, contrast, saturation and per-shot exposure; every one read as random colours | House look = the hash-locked LUT only, one look on every shot. Read approved reels' render contracts before inventing a look | L0018 |
| 3 | The LUT ran as a bare `lut3d` on untagged full-range masters → cooler and darker than approved reels | Run it through `housechain.house_vf`; prove by reproducing an approved frame of the same master to < 1.5 mean abs | L0037 |
| 4 | A brightness note was answered with a per-block reference grade; numerically perfect, rejected — the real complaint was the font | On a house-graded reel never answer a colour note with a reference look; change only the named item; a request to go back = rebuild the previous chain exactly | L0102 |
| 5 | A "lights-off" gain copied from the reference turned a shot black | House look covers every shot; no creative light state unless the reviewer names that beat | L0024 |
| 6 | A crop at a fractional x on a 4:2:2 master rendered a shot solid magenta; the test suite didn't see it | Convert to 4:4:4 before crop (or even crop offsets); chroma sanity per shot; decoded whole-build colour scan after any pipe change | L0041 |
| 7 | A fix agent ruled a colour complaint "not a defect" | Measure at the cited frame vs the reference, move it, before/after numbers on the card | L0013 |
| 8 | A block was graded toward an average of its neighbours; the gap widened | Per-block matching only on a named shot, to that block's own reference | L0014, L0026 |
| 9 | Two stacked 0.45 gammas (≈ 0.20 effective) turned a backlit bright subject into a black cutout | Second pass floors at 0.62; near-black share measured per shot | L0005 |
| 10 | The readability gate was bypassed with the low-contrast flag; most states were unreadable, one word at ~1.1:1 | Unreadable caption is a STOP; the override is a question with the numbers | L0023, L0025 |
| 11 | A shadow-aware gate passed everything because it measured the opaque shadow | A gate must fail something real first; WCAG ink vs the 4–8 px band, floor 3.0, raw figure beside it | L0030 |
| 12 | The grade changed and the caption gate wasn't re-run; one state failed on most of its frames | A grade change is a caption change: re-gate every frame of every state | L0033 |
| 13 | Gated pre-encode at 3.33; the decoded file read 2.98 | Shadow when any pre-encode frame < 3.6 (3.0 + 0.35 drift + margin); re-gate on the decoded file | L0117 |
| 14 | Only settled frames were gated; blur/fade entry frames fell under 3.0 across most variants | Predict and gate the ENTRY frame on the candidate bed before casting | L0104, L0111 |
| 15 | A cast-wide black shadow muddied dark ink | Shadow per state, only where flat ink fails; dark ink never gets a dark shadow | L0056, L0058 |
| 16 | A rejected outline was added back to make white ink pass | Fix the BED first (recast a darker window); shadow is the disclosed last resort | L0096 |
| 17 | The shadow was baked before the entry device; a two-word caption arrived as one word | Ink treatments go after the device, per frame | L0002 |
| 18 | A caption face was re-set by eye and got worse | Refit to the reference's pixels with a no-regress rule | L0016 |
| 19 | Dilation/tracking was swept on one face when the letterforms themselves were wrong | Face-family search across real candidates, every score reported | L0028 |
| 20 | Asked to match another row's font, the lane ran a blind search; later copied the face but not its tracking | Named row = that row's locked face AND tracking/stretch model; fit only size/position | L0048, L0116 |
| 21 | A substitute script face shipped with a disclosure when the real face was available | Identify every face from reference pixels; glyph-for-glyph overlay proof; a disclosed substitute is a failed build | L0055 |
| 22 | Glyphs were traced from damaged instances (a head over a letter, bokeh eating a stroke) | Trace from whole instances; render every used glyph large and read it; counter test | L0067 |
| 23 | A lower glyph score vetoed a re-trace that actually read correctly | A score never vetoes reading | L0068 |
| 24 | A stair-stepped traced face with forced gaps was reused | Check traced faces at 300 px; pick borrowed faces by per-line IoU; spacing from reference glyph centres + 2 px de-weld | L0112 |
| 25 | Caption states were placed one frame late (1-based) | Caption frames are 0-based from the reference's ink-per-frame curve | L0098 |
| 26 | Footage-filled reference letters were drawn as flat white | Rebuild the in-glyph colour field, place by luma edges, gate per letter | L0089, L0078 |
| 27 | Burial was fixed only on the shot the audit named; the same defect sat on another shot | A finding is a class: re-measure every shot, state and frame | L0069, L0075 |
| 28 | A praised opening clip was recast to clear a buried word | Praised clips are locked; fix burial in the caption layer (draw hidden glyphs in front) | L0093 |
| 29 | A caption word ran across the protagonist's face | Frame so no word covers a face; face boxes × word boxes = 0 | L0115, L0103 |
| 30 | Empty plates were cast where the reference shows a person | Reference-people pass at intake; a person in the reference = an identity slot | L0065, L0042 |
| 31 | Face detection said "no person" on a silhouette slot and a stranger-like figure was cast | Reference-people is face-only; eye-check the reference and our clip before declaring "none" | L0110 |
| 32 | Shots with no human at all (water, an empty bed, hands only) shipped | Every shot carries the protagonist, face visible preferred | L0103 |
| 33 | Someone else was cast as the protagonist | Identity pool + blacklist as code; identity check OK; face-verify at zoom | L0052 |
| 34 | An old identity OK was reused after clip grades changed | Re-run identity against live grades before every render, over the whole played window | L0087, L0088, L0090 |
| 35 | Two different files from the same setup and angle read as a repeat | Freshness by SETUP; side-by-side first frames; variants ≥ 4 clip numbers apart and mean abs diff ≥ 10 | L0071, L0101 |
| 36 | The same clips were reused across reels until they were stale | Usage histogram per cast; prefer least-used; disclose reuse | L0015 |
| 37 | A scene series was declared missing from a tag search | Also search by shoot date / clip-number series and contact sheets | L0072 |
| 38 | Readable third-party text and readable screens shipped | OCR every 2nd–4th decoded frame + native-res crops; never cast a ruled-out venue | L0073, L0080, L0084 |
| 39 | Casts were eye-screened at 480×270, new picks only | Screen the whole cast at ≥ 960×540 with the crop applied; log rejects | L0091, L0105 |
| 40 | A caption-bed crop search cut the protagonist's head off | Every person crop carries ≥ 6 % headroom, swept on every decoded frame | L0113 |
| 41 | An audit's crop move was applied blindly and put the face under the caption | Crop proposals are hypotheses; work out direction; lower zoom to open a gap | L0114 |
| 42 | A crop "push" never zoomed and phase correlation called it a pan | zoompan on an isolated ProRes plate; measure motion with feature tracking | L0010 |
| 43 | Stream-copy concat of B-frame segments dropped a 1-frame shot; every later cut was a frame late | Decoded concat; cut-check every reference cut on the delivered file; exact frame count | L0076, L0053 |
| 44 | Lanes stopped on "fix round spent" and then ran audits 2, 3, 4 | One audit; fix every finding; finding → fix → proof; no question on findings | L0081, L0092 |
| 45 | Several parallel transcription processes exhausted RAM and crashed the machine | One ASR process machine-wide through `whisper_guard.py`, `medium` max, refuses under 6 GB free | — (machine law) |
| 46 | A big analysis job hit ~20 GB while an RSS guard never fired (memory was compressed) | Guard on physical footprint, self-kill at 4 GB, subset-test first, block the N×N work | — (machine law) |
| 47 | Disk floors were read from `df -g` (GiB) and an allowed round sat blocked | Preflight's decimal-GB line is the only authority; re-check right before render | L0036, L0029, L0108 |
| 48 | Bare `v008.mp4` names let one row's page play another row's video | `reel<NN>-<SHORTCODE>-<MODE>-v<NNN>.mp4`; the review app resolves only from the row's own folder | L0118 |
| 49 | A reel was hand-built in a chat session instead of through an order on the bus | Every change goes through an order and a lane | — |
| 50 | "It's on the row" was claimed without asking the review app | Ask the app what it serves before claiming it | L0035 |
| 51 | A Fix-phase agent wrote `finished` before Audit/Deliver ran | Only the orchestrator writes `finished`, after `preflight --finish` | L0050 |
| 52 | A doctrine-overturning lesson was filed without editing older lessons and the brief | Edit every contradicting lesson and template in the same sitting | L0026 |
| 53 | A reference was called login-walled because the downloader was months out of date | Control test, update, retry anonymously before calling it a wall | L0040, L0043 |
| 54 | Encoded at crf 18 and went over the upload size cap | crf 20–21; check bytes before the audit | L0117 |
| 55 | A measure tool with swapped arguments overwrote a delivered video | Measure tools refuse non-JSON / deliver/ / large existing paths; deliverables `chmod 444` | L0100 |
| 56 | A one-liner `select` after `hstack` silently paired the wrong frames on a review sheet | Decode inputs separately, index in Python, label every tile | L0100 |
| 57 | Caption devices were measured with another row's frame count, so late states read static | Measure devices with the reference's own frame count | L0074 |
| 58 | A face sweep wrote two candidates to one filename and overwrote the true score | Candidate → filename mapping must be injective; assert it | L0049 |
| 59 | The board said "rate-limited" for hours because a detector matched the brief's own text | Test pane-text detectors against the brief; read the live pane before reporting state | L0031 |
| 60 | A kit change shipped while a build was mid-round, mixing two pipes in one reel | Check the bus before editing the kit; announce pixel-changing kit changes; re-render all shots on the new kit | L0039 |

---

*Status: extracted from a working private system. Paths, interpreters and thresholds reflect that
system; expect to adapt them (see [`05_CODE_GUIDE.md`](05_CODE_GUIDE.md) porting checklist).*
