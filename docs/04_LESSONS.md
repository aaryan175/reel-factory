# 04 — Lessons ledger

This is the factory's institutional memory: every rule here exists because a build once failed
without it. The ledger is append-only. A lesson is never silently deleted; when a later lesson
narrows or overturns an earlier one, both stay, and the earlier one says so.

Each entry has the same shape:

- **Rule** — what a build, lane or reviewer must do.
- **Why** — the failure that produced the rule, described generically.
- **Enforced by** — the test that makes the rule mechanical, as a path relative to the repo root,
  or `review checklist` when it is a human/agent judgement that is not yet automated.

How the ledger is used at runtime (see `03_QA_SYSTEM.md` for the full loop):

- `python3 code/reel-production-tools/lessons.py brief --row NN` prints the lessons a lane must
  read before it touches a project, newest first.
- `python3 code/reel-production-tools/lessons.py check --row NN` blocks a lane from writing
  `finished` while that project's feedback inbox still has untriaged items.
- `python3 -m onetoone.preflight --row NN --cast <cast.json>` runs every check that is enforced in
  code.

Terminology used below:

- **Protagonist** — the person a reel is about (the creator whose footage is being cut). Slots
  that must show this person are **identity slots**.
- **Reference** — the reel being studied and remade. **Bed** — the picture behind a caption.
- **Lane** — one agent session working one project (Build → independent Audit → Deliver).
- **House look / house chain** — the single approved colour pipeline (a camera conversion LUT
  applied through a fixed ffmpeg chain), see `02_CRAFT_PLAYBOOK.md`.
- **The kit** — `code/reel-production-tools/onetoone/`.
- **The Deck** — the review web app, `code/reel-deck/`. **orderd** — the order daemon,
  `code/reel-production-tools/orderd/`.
- **CALL** — a lane stopping to ask the human reviewer a question.

Placeholders: `<REPO>`, `<WORKDRIVE>`, `<FOOTAGE>`, `<REFS>`, `<RENDERS>`.

Numbering note: the ledger runs L0001–L0118 with no gaps and no duplicate IDs. A few entries are
marked *Generalised* where the original was specific to one deployment; their technical core is
kept. Two entries about rights (L0062) and fonts (L0064) are *Revised* to the licensing stance in
the README.

---

## Category index

| Category | Lessons |
|---|---|
| Colour, grade, exposure, clipping | L0001, L0003, L0004, L0005, L0006, L0007, L0008, L0014, L0018, L0024, L0037, L0041, L0045, L0058, L0095, L0099, L0102 |
| Caption typography, faces, tracing | L0016, L0028, L0048, L0049, L0055, L0064, L0067, L0068, L0077, L0082, L0085, L0089, L0090, L0097, L0098, L0112, L0116 |
| Caption ink, shadow, readability gate | L0002, L0023, L0025, L0030, L0033, L0038, L0054, L0056, L0057, L0061, L0066, L0078, L0096, L0104, L0106, L0109, L0111, L0117 |
| Caption occlusion (behind subject) | L0069, L0075, L0083, L0093 |
| Casting, identity, freshness, grades | L0015, L0042, L0052, L0058, L0060, L0065, L0071, L0072, L0087, L0088, L0090, L0101, L0105, L0107, L0110, L0115 |
| Privacy / signage screening | L0073, L0080, L0084, L0091, L0103 |
| Framing, crop, motion | L0010, L0041, L0113, L0114 |
| Cuts, concat, render integrity | L0053, L0076, L0100, L0117 |
| Devices (blur-in, match cuts) | L0074, L0084, L0094, L0111 |
| Audit & process discipline | L0009, L0013, L0026, L0027, L0039, L0046, L0050, L0069, L0081, L0092 |
| Factory ops: liveness, healer, hooks | L0017, L0019, L0020, L0031, L0032, L0034, L0044 |
| Factory ops: disk, permissions, tooling | L0011, L0029, L0036, L0051, L0086, L0108 |
| Order daemon, lanes, concurrency | L0022, L0059, L0070 |
| Review Deck, board truth, CALLs | L0012, L0021, L0027, L0035, L0063, L0079, L0118 |
| Reference intake & fetching | L0040, L0043, L0072 |
| Variant batches (alternate footage) | L0059, L0091, L0101, L0104, L0105, L0106, L0107 |
| Rights & licensing | L0047, L0062, L0064 |

---

## Lessons

### L0001 · Every shot is measured against the reference, in every grade mode
- **Rule:** In a reference-matching grade mode, dropping the colour solve must never also drop the
  luma pull/ramp. In the house-look mode there is deliberately no pull and no ramp. In every mode,
  every shot is *measured* against the reference (`lumacheck`) and its clipped share is reported
  per shot on the review card, so a bright or clipping shot is disclosed, never silently shipped.
  (Narrowed by L0018 and L0024.)
- **Why:** Switching to a "no extra grading" mode silently switched off exposure correction as
  well; most shots shipped 30–107 luma codes brighter than the reference with up to 22% of pixels
  clipped.
- **Enforced by:** review checklist (per-shot luma/clip table on the card).

### L0002 · Ink treatments are applied after the entry device, per frame
- **Rule:** Any ink treatment (shadow, glow, outline) is applied *after* the device has drawn the
  frame (`devices.layer_for_frame`), on the composed state layer — never to the settled layer the
  device reads.
- **Why:** A shadow baked into the settled layer filled the alpha gaps the word-splitter keys on,
  so a word-by-word entry collapsed two words into one.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_v011_fixes.py::test_shadow_is_taken_from_the_frame_the_device_drew`

### L0003 · A gamma pull cannot fix clipping
- **Rule:** A pure gamma curve maps 1.0→1.0 for every gamma, so it can never reduce clipping. The
  pull curve carries a shoulder (`PULL_SHOULDER = 0.94`), and clipped share (pixels ≥ 250) is
  measured per shot before and after; the reference's clipped share is the bar.
- **Why:** One shot had ~10% of pixels pinned at ≥ 250 and every pull left it untouched.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_lessons_grade.py::test_L0003_pull_curve_carries_a_shoulder`

### L0004 · Exposure tolerance scales with the target
- **Rule:** Tolerance = `min(PULL_TOL, max(1, 0.25 × target))`. A flat absolute tolerance is
  wrong for dark shots.
- **Why:** A near-black shot (target luma ~5.6) shipped at ~11 because a flat tolerance of 6.0
  was ±107% of its target.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_lessons_grade.py::test_L0004_tolerance_scales_with_the_target`

### L0005 · Stacked curves compose — floor the second pass
- **Rule:** Two 0.45 gammas compose to ~0.20 effective. A second correction pass floors at
  `PULL_PASS2_GAMMA_MIN = 0.62`. Near-black share (pixels ≤ 5 and ≤ 2) is measured per shot on
  every build and compared with the previous version and the reference.
- **Why:** A second 0.45 pass turned a backlit light-coloured garment into a black cutout — 16.7%
  of the frame at ≤ 2.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_lessons_grade.py::test_L0005_second_pass_floors_higher_than_the_first`

### L0006 · Excess in the highlights is fixed in the highlights only
- **Rule:** When the excess brightness lives in the highlights (e.g. a sky), compress only the
  highlights with `highlight_pull()` (identity below a knee of 0.20). Never bend the whole curve
  to fix a bright sky.
- **Why:** A shot facing the sun, against a reference silhouetted at dusk, could only reach target
  with a whole-curve bend that also crushed the midtone subject.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_v011_fixes.py::test_highlight_pull_leaves_the_subject_alone`

### L0007 · Highlight compression is luma-only with chroma-follow
- **Rule:** Highlight compression acts on luma only, with chroma following at a reduced rate
  (`render.luma_only`, `CHROMA_FOLLOW = 0.4`). Superseded as a default by the house look
  (L0018), which uses no pulls at all; it still applies whenever a pull is explicitly requested.
- **Why:** An `curves=all` style compression gives R, G and B the same transfer and drains
  chroma where the sky is brightest (a dusk blue lost ~62% of its saturation).
- **Enforced by:** review checklist.

### L0008 · Distinct-level count in skies is a publish gate
- **Rule:** Count distinct luma levels in each sky/gradient patch; below ~25 levels, back the
  compression off on that shot before publishing. Review quality and publish quality are
  different bars.
- **Why:** After compression a sky had only 18 distinct levels (sd ≈ 2.95): fine in the review
  encode, but it bands after a platform re-encode.
- **Enforced by:** review checklist.

### L0009 · Never self-certify
- **Rule:** Every candidate gets an independent, read-only audit before it is called anything
  ("1:1", "fixed", "done"). The audit's verdict line is what the reviewer is told, with every open
  finding named.
- **Why:** A build was reported as a faithful remake and the independent audit then found four
  hard defects.
- **Enforced by:** review checklist; the lane prompt (`code/reel-production-tools/orderd/lane_prompt.py`)
  separates Build, Audit and Deliver phases.

### L0010 · Push-ins use zoompan; motion is measured with feature tracking
- **Rule:** Push-ins and keyframed moves use `zoompan` on an isolated intermediate (ProRes) plate.
  Camera motion is measured with feature tracking (`onetoone.motion`), never global phase
  correlation.
- **Why:** A crop-based push never zoomed (+0.02% measured) because ffmpeg's `crop` evaluates w/h
  once; phase correlation also reported a zoom as a horizontal pan.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_v011_fixes.py::test_push_and_keys_use_zoompan_not_a_fixed_size_crop`

### L0011 · Disk floors are checked before every render
- **Rule:** Before any render: system disk ≥ 20 GB free and the work drive (`<WORKDRIVE>`)
  ≥ 100 GB free, checked by preflight. Never park editor/agent scratch on the work drive.
- **Why:** A render was killed by a watchdog at 5 GB free on the system disk, and a later round
  stopped because scratch files pushed the work drive under its floor.
- **Enforced by:** `onetoone.preflight` machine line; review checklist.

### L0012 · Verify UI state by computed visibility in a real browser
- **Rule:** UI state is verified by computed visibility in the real browser, not by reading the
  template attribute. Keep `[hidden]{display:none !important}` in the Deck stylesheet; the
  walkthrough recorder refuses to capture a page while the Deck's test-mode flag is on.
- **Why:** A "test mode" banner showed on every page because a CSS `display` rule beat the
  `hidden` attribute, and the reviewer was wrongly told their notes were going to a test lane.
- **Enforced by:** review checklist.

### L0013 · Colour and text complaints are measured, never dismissed
- **Rule:** A fix agent never rules a colour or caption complaint "not a defect". It is measured
  at the cited frame against the reference with the kit, moved, and the before/after numbers go on
  the card.
- **Why:** A fix agent dismissed a colour complaint; the reviewer's next note was that colour and
  font were both clearly wrong.
- **Enforced by:** review checklist (finding → fix → proof table).

### L0014 · Per-block reference matching only for a named shot (narrowed)
- **Rule:** Per-block reference matching applies only to a shot the reviewer names as off, and
  then to *that block's own* reference values — never an average of neighbouring blocks. The
  audit re-measures the cited frame. The default for every reel remains the house look (L0018).
- **Why:** A block was graded to a self-chosen act average instead of its own reference values
  and reported fixed while the gap widened. A later round over-applied this lesson to every block
  and produced a whole-reel look that was rejected (see L0018, L0026).
- **Enforced by:** review checklist.

### L0015 · Clip freshness is a defect class; colour, font and casting are separate defects
- **Rule:** Every cast is checked against a usage histogram (exact-stem match across the registry
  and prior casts) and reuse is disclosed on the card. Colour, font and casting complaints are
  three separate defects, each measured separately. (Extended by L0071: freshness is judged by
  setup, not just stem.)
- **Why:** A version was rejected for wrong colour, wrong font *and* overused clips at once.
- **Enforced by:** `code/reel-production-tools/reuse_map.py` + `code/reel-production-tools/test_reuse_map.py`; review checklist.

### L0016 · Letterforms are fitted to the reference pixels, never re-set by eye
- **Rule:** Caption letterforms are fitted to the reference's own pixels (`onetoone.refit`: size,
  tracking, squeeze, ramp; script metrics for ornate words), with a no-regress rule against the
  previous version's fit scores.
- **Why:** A plain face re-set by eye regressed against the previous version and was rejected.
- **Enforced by:** review checklist; fit scores recorded in the captions brain file.

### L0017 · (Superseded) idle backstop on a pre-work heartbeat
- **Rule:** Superseded by L0020: health is judged on the *completed* tick only. Do not add an idle
  backstop that reads a heartbeat written before the work.
- **Why:** After a prompt-change restart a worker stopped scheduling wakeups and orders sat
  unread; the first fix (a 10-minute idle backstop on the pre-work heartbeat) would have killed
  busy sessions.
- **Enforced by:** see L0032.

### L0018 · The house look is one LUT on every shot
- **Rule:** The house grade is the hash-locked camera conversion LUT and nothing else — one look
  on every shot (`grade_mode = "house"` in `render.py`). No per-shot exposure matching unless the
  reviewer explicitly asks for reference-matched exposure. Before inventing a look, read the
  render contracts of the already-approved reels.
- **Why:** Six rounds chased "our normal grading" with a custom curve (contrast 1.06, saturation
  1.22) plus per-shot reference exposure; every one was rejected as inconsistent. Nobody had
  checked what the approved reels actually shipped with.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_house_pipe_render.py`; review checklist.

### L0019 · Every agent hook has a timeout; rate limits are waited out, not restarted
- **Rule:** Every hook in the agent runtime's settings carries a timeout (pre-tool 15 s,
  prompt-submit/session-start 20 s). The healer writes state `launching` on restart, detects the
  runtime's usage-limit message on the pane and *waits* (state `limited`) instead of restarting.
- **Why:** A worker sat 57 minutes in one tick on a hook with no timeout; a healer restarted a
  rate-limited session repeatedly, discarding the runtime's own auto-resume.
- **Enforced by:** review checklist.

### L0020 · Two heartbeats: started and completed
- **Rule:** Write `alive_at` as a tick's first action and `last_tick_completed_at` as its last.
  The healer and the Deck judge health on the completed tick (idle with no completed tick for
  12 minutes = wedged). Never judge liveness on a pre-work heartbeat.
- **Why:** A scheduled wakeup was lost while the session was alive; nothing saw it because the
  only heartbeat proved a tick had *started*.
- **Enforced by:** review checklist; `code/reelctl/tests/test_studio_health.py` (legacy engine equivalent).

### L0021 · Board truth is parsed from exact receipt lines
- **Rule:** The board's state comes from ACK/CALL lines in an exact format:
  `ACK — <lane> — <UTC> — picked up: …` / `… — finished: …`; a blocked/queued retry pickup
  renders as QUEUED with no timer. Lanes write exactly these strings; any wording change is a Deck
  test change first.
- **Why:** The board showed FIXING for over an hour on finished work because a "finished" variant
  didn't match the parser, and showed a lane parked on a disk floor as actively fixing.
- **Enforced by:** `code/reel-deck/tests/test_app.py::test_blocked_pickup_reads_as_queued_not_fixing`

### L0022 · Retire lane scripts the moment their base version is superseded
- **Rule:** A lane script moves to a `retired/` folder as soon as its base version is superseded;
  briefs never name a lane whose base is not the project's current version. New lanes are written
  fresh against the kit with the current cast as base.
- **Why:** A stale lane script named in a brief would, if run, have rebuilt from an old base,
  deleted a delivered file and overwritten the registry state.
- **Enforced by:** review checklist.

### L0023 · An unreadable-caption gate is a STOP, never a lane override
- **Rule:** `UnreadableCaption` stops a lane. The override (`--allow-low-contrast`) is never a
  lane decision: it is a CALL to the reviewer with the measured contrast numbers attached, and the
  render report's "unreadable" list goes on the card verbatim.
- **Why:** The override flag was used for several versions with no rule for when it is allowed.
- **Enforced by:** review checklist; the gate raises in `onetoone/captions_typeset.py`.

### L0024 · The house look applies to every shot, including copied "creative" treatments
- **Rule:** In house mode no slot carries a per-shot light treatment (e.g. a "lights-off" gain,
  a wash) unless the reviewer asked for that beat by name.
- **Why:** A LUT-only build kept one copied lights-off gain (0.25) from the reference; the shot
  read as inexplicably black.
- **Enforced by:** review checklist.

### L0025 · The readability gate measures what the viewer sees
- **Rule:** Readability is measured on the bed *with* the caption shadow composited and ink pixels
  masked out. Any grade change that brightens beds is a caption change: re-measure every caption
  state. The shadow is directional (offset 4–5 px, radius ≤ 20), never a symmetric glow.
- **Why:** The gate judged ink against the raw bed and ignored the shadow, so a working shadow
  could never pass and a bypass became the only way to build; 9 of 13 states were under 3:1, one
  at 1.09:1 on a white sky.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_lessons_grade.py::test_readability_gate_counts_the_shadow_the_viewer_sees`

### L0026 · A lesson that overturns doctrine edits every contradicting source in the same change
- **Rule:** Before closing a lesson that overturns doctrine, grep the ledger, the agent's standing
  prompt and the lane templates for the old doctrine's wording and edit them all in the same
  sitting.
- **Why:** A new lesson reversed the grading doctrine but an older lesson and the brief still said
  the opposite; a lane launched minutes later cited the old lesson and rebuilt the rejected look.
- **Enforced by:** review checklist.

### L0027 · Test fixes at the surface the reviewer reads
- **Rule:** A board/status fix is tested at the surface the reviewer reads (the card phrase from a
  replayed real receipt), never only at the parser. Same for any flag crossing a module boundary.
- **Why:** A parser fix passed its test, but the card builder rebuilt the dict from four keys and
  dropped the new flag; the board kept showing the wrong state after the fix was "done".
- **Enforced by:** `code/reel-deck/tests/test_app.py::test_queued_pickup_reaches_the_board_card`

### L0028 · A contested face triggers a cross-face search, not a parameter sweep
- **Rule:** When the letterforms themselves are contested (not just weight), run a face-*family*
  search: `onetoone.refit.fit_part` scored against the reference's own pixels across many real
  candidate fonts, reporting every candidate's score. A dilation/tracking/stretch sweep within one
  face is not a substitute.
- **Why:** Two rounds tuned dilation and tracking on a face chosen by eye; that cannot change
  glyph *shape* (o roundness, t tail, apostrophe), only weight, and both were rejected.
- **Enforced by:** review checklist.

### L0029 · Disk floors move with swap; protect the destination before moving data
- **Rule:** Re-run the disk check immediately before every render — swap shares the system disk.
  When moving cold data to another drive, disable indexing on the destination first (on macOS,
  create `.metadata_never_index`), verify by checksum, then symlink. Check both floors after any
  move.
- **Why:** System free space fell 5 GB in 30 minutes from swap alone; moving files to the big
  drive then pushed *it* under its floor because the OS search indexer started a multi-GB merge.
- **Enforced by:** review checklist.

### L0030 · A gate must be shown to fail something real
- **Rule:** Every new or changed gate ships with a test case it rejects (e.g. a faint shadow on a
  bright sky), and its measure must vary across the states of a real build. Caption legibility =
  WCAG contrast ratio of ink vs the band 4–8 px outside the glyphs on the composited frame, floor
  3.0; the raw shadow-free contrast is always reported beside it.
- **Why:** The first shadow-aware gate averaged a ring touching the ink against a shadow-free
  need; every state passed by 1.3×–9×, so it measured "is there an opaque shadow", not "does this
  caption separate from the footage".
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_lessons_grade.py::test_readability_gate_counts_the_shadow_the_viewer_sees`

### L0031 · Healer and brief changes are watched for one live cycle
- **Rule:** A healer or brief change is done only after it has been watched for one full cycle:
  the heartbeat lands in the file the healer reads, and the board matches the pane. Paths in briefs
  are absolute. Any pane-text detector is tested against the brief's own text. Before reporting a
  state, read the pane, not the status file.
- **Why:** A rewritten brief lost a path (worker wrote a heartbeat where nobody read it → false
  "wedged" restarts), and a usage-limit grep matched the brief's own sentence, falsely marking the
  worker rate-limited for hours.
- **Enforced by:** review checklist; see L0032.

### L0032 · The healer is tested against a frozen fake worker
- **Rule:** Healer changes ship only with a sandbox test passing (sandbox tmux session + a fake
  worker that ignores SIGTERM) covering: launch; leave a healthy worker alone; idle wedge →
  SIGKILL + relaunch; mid-build wedge detected by transcript silence (25 min); busy build left
  alone; brief text never read as a usage limit; real limit waits; manual pause survives. Progress
  is judged by the worker's own transcript files — the one signal a frozen process cannot fake.
- **Why:** A wedged agent process keeps its pane and spinner, ignores SIGTERM and survives
  session kill as an orphan; a wedge during a build was exempt from every backstop; nothing tested
  the healer.
- **Enforced by:** not included in this repo (the healer script and its shell test were part of
  the private deployment); review checklist.

### L0033 · A grade change is a caption change
- **Rule:** After any grade or bed change, re-run the readability check (halo, ink layer over the
  shaded bed) on every frame of every caption state of the actual picture before building. Where
  flat white fails, apply the standard directional shadow per frame after the device (radius 12,
  opacity 1.0, offset (4, 5), grow 5, radius ≤ 20) and report halo ratio plus raw contrast per
  state. Never bypass.
- **Why:** A brighter house grade made a flat-white caption fail on 58 of 77 frames (min halo
  1.40); nobody re-ran the gate after the grade change.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_caption_shadow_gate.py`

### L0034 · Safety nets are code too; claims need an acceptance file
- **Rule:** Every fallback path in the healer gets a sandbox case before it ships, and freshness is
  checked before any state is merged. Board state must not depend on magic phrasing: match receipts
  by file *name*, and let newer finished work close an older question. "Works" is claimed only from
  an acceptance file with evidence per line.
- **Why:** A belt-and-braces healer fallback copied a stale heartbeat over a live one and killed
  the worker mid-pickup; a question stayed open on the board after it had been answered.
- **Enforced by:** review checklist.

### L0035 · "It's on the review page" is proven by asking the Deck
- **Rule:** Before any "delivered" claim, query the Deck's own view (`board.row_detail(NN)["current"]`)
  and confirm it names the new file. Registry deliveries name the file in a *value*
  (`"file": "<name>.mp4"`); the Deck harvests file names from keys as well as values.
- **Why:** A file was verified by hash, registry state and board phrase, but the Deck still served
  the old version because the new name was stored as a dict key; the reviewer spent a note on the
  wrong version.
- **Enforced by:** `code/reel-deck/tests/test_app.py::test_a_file_named_only_in_a_dict_key_is_still_declared`

### L0036 · One authority per gate; waiting is not failing
- **Rule:** Disk floors are whatever preflight prints (decimal GB), never `df -g` (binary GiB,
  rounded down). A floor wait is not a build failure and never consumes retry attempts. On the
  board, a progress line never changes whether work is moving: the newest non-progress status
  decides.
- **Why:** 105 GB free read as "97" in GiB; a round sat blocked for an hour and burned two of its
  three retries on a disk wait.
- **Enforced by:** `code/reel-deck/tests/test_app.py::test_structured_status_blocks_beat_prose`

### L0037 · The house grade is a whole pipeline, proven against an approved frame
- **Rule:** The house grade = the LUT run through the approved pipe (`onetoone.housechain.house_vf`:
  `in_range=full`, `in_color_matrix=bt709`, 32-bit float tetrahedral `lut3d`, 16-bit
  intermediate, output colorspace bt709 tv range) — never a bare `lut3d` on the masters.
  "Colours like the other reels" is proven by reproducing an approved reel's frame from the *same*
  master to < 1.5 mean absolute difference. Composite and measure on player-exact planes
  (`onetoone.yuvexact`), never swscale `rgb24` (reads ~2 levels dark). Re-run the caption gate
  after any pipe change.
- **Why:** A bare LUT on untagged full-range camera masters (swscale assumed BT.601, 8-bit
  trilinear) came out cooler and darker than approved reels (R/B 1.96 vs 2.26), and the swscale
  composite baked in a further 2-level darkening.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_house_chain_and_sharpness.py::test_house_chain_reproduces_the_approved_cold_open_and_bare_chain_does_not`

### L0038 · Measure a "soft caption" before touching letterforms
- **Rule:** Measure edge sharpness on the decoded frame at the cited frame (`onetoone.sharpness.edge_width_px`,
  10–90% edge width) against the reference, the previous version and an approved reel; the floor
  is 2.0 px. If every number says the ink edge is ideal (~1.6 px is a hard 1-px step), do not touch
  letterforms, blur or tracking — report the numbers and look at the bed under the caption instead.
- **Why:** A "soft caption" complaint measured 1.61 px edge width (approved reels 1.61–1.80, the
  reference's own text 2.45); the perceived softness came from the grade/bed, not the ink.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_house_chain_and_sharpness.py::test_sharpness_floor_accepts_hard_edges_and_rejects_soft_ones`

### L0039 · Don't change the kit under a running round
- **Rule:** Before editing anything a build imports, check the bus and the factory status: if a
  round is running or an un-ACKed order exists, wait or notify the worker first (a note file plus
  a one-line pointer; long terminal text is truncated). A kit change that alters shipped pixels is
  announced with its measured delta, and the next build of every affected project renders *all*
  shots on the new kit — never mix segments from two pipelines.
- **Why:** The colour pipe was patched minutes before a fix round launched that promised
  "everything else byte-identical"; it would have shipped one shot on the new pipe beside eleven
  on the old.
- **Enforced by:** review checklist.

### L0040 · Diagnose a failed reference fetch with a control
- **Rule:** Before calling a fetch "login-walled", run the same anonymous command on a reference
  that fetched before. If the control fails too, the tool or network is at fault: update the
  downloader and retest. Only a reference that fails while the control passes is a wall; such a
  CALL quotes the control result and the tool version. Never add cookies or credentials without the
  human's explicit say, and respect the platform's terms of service.
- **Why:** An outdated downloader failed on everything; a lane read its generic error text as a
  login wall and asked the human for a file they should never have had to supply.
- **Enforced by:** review checklist.

### L0041 · Crop subsampled video on even integer offsets; prove pipe changes on the decoded build
- **Rule:** Any colour-pipe change is proven on the *decoded* whole build: scan every frame for
  gross colour failure (per-frame mean vs the previous version, magenta/green share) before calling
  it rendered. Compute independent "twin" expectations with the crop applied *after* conversion.
  `framing.filter_for` must emit even integer x/y for chroma-subsampled masters (or convert to RGB
  before cropping).
- **Why:** A fractional crop x (58.6, `exact=1`) on a native 4:2:2 10-bit master cropped chroma at
  an odd offset and turned a shot solid magenta (mean RGB 237/103/252). The old chain converted to
  RGB first and hid it; 120 unit tests could not see it.
- **Enforced by:** review checklist; render report `chroma_sanity`.

### L0042 · A recast of a person shot must show the person
- **Rule:** A recast slot whose reference shot carries a person must show the protagonist with a
  readable face and real action. Screen every candidate at native crop for face, sharpness at the
  exact shipping window, and blacklist membership.
- **Why:** A recast put an empty plate (a curtain onto nothing) where the reference had a person;
  it read as a useless clip.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_identity_pool.py` (identity slots); review checklist.

### L0043 · Check the downloader version before declaring a wall
- **Rule:** Before declaring a fetch blocked, compare the downloader's version with the current
  release, update, and retry anonymously. Only a fetch that fails on a current version is a CALL.
- **Why:** A months-old downloader produced a "rate-limit or login required" message that was
  wrongly escalated.
- **Enforced by:** review checklist.

### L0044 · Park in-flight work before killing a worker session
- **Rule:** Before restarting or killing the worker session for any reason (configuration change, healer
  restart, …), let the worker park its in-flight workflow in its retry file (attempt count
  unchanged, `not_before = now`) so the next tick resumes cleanly.
- **Why:** A worker was killed mid-render; the workflow died with it and the tick loop misread the
  silent kill as a build crash.
- **Enforced by:** review checklist.

### L0045 · Verification twins must use the render module's own filter builder
- **Rule:** A verification script that rebuilds part of the render contract must import the render
  module's filter-chain builder (or be re-synced every time the contract changes). A diverging twin
  is a false-failure risk.
- **Why:** After the magenta-crop fix (L0041), a verification script still built the old chain and
  "expected" the old bug, which would have read as a regression.
- **Enforced by:** review checklist.

### L0046 · Deliver-phase prompts state their narrow scope first
- **Rule:** A Deliver-phase prompt opens by stating that build and audit are already done and this
  phase is bookkeeping only, then gives a bounded checklist. Don't prepend the whole round's law
  block in a way that reads as the phase's task. Use a higher effort setting for Deliver phases that
  must reconcile audit card-text findings.
- **Why:** A low-effort Deliver agent read the round's full law block as its own task and declined
  with "multi-hour rebuild needed" instead of doing a small registry update.
- **Enforced by:** `code/reel-production-tools/orderd/lane_prompt.py`; review checklist.

### L0047 · A recognisable person's voice and words are not copied
- **Rule:** The default audio step muxes a licensed track you supply; the opt-in "mux the reference's exact audio" step applies only to music you are
  entitled to use. When a reference features a recognisable person speaking, never mux their voice
  or reproduce their first-person wording, name, handle or catchphrase: use licensed/library audio
  or your own recorded audio on the same beat grid, and write new wording in the same structural
  position, timing, typeface and colour. A terse "go ahead" on a queued question approves the
  rights-safe path that was proposed, not the declined one.
- **Why:** The bit-exact audio rule and the caption copy rule were about to be applied to a
  reference whose speaker was a public figure delivering their own words.
- **Enforced by:** review checklist. See README "Rights & licensing".

### L0048 · "Make the font like project X" means read X's locked face first
- **Rule:** When the reviewer names another project as the font target, don't run a blind face
  search. Read that project's locked face config (installed family + weight + index, e.g.
  `onetoone.captions_typeset.DEFAULT_FACES`, or its approval card), verify against its delivered
  frames if undocumented, then refit that exact face to *this* project's own caption boxes. The
  blind cross-face search (L0028) is for abstract complaints only.
- **Why:** A blind search would have re-derived a face that was already approved and locked.
- **Enforced by:** review checklist.

### L0049 · Per-candidate output files need collision-proof names
- **Rule:** A sweep that writes one file per named candidate uses a collision-proof slug of the
  *full* name (or an explicit index), and asserts the name→filename mapping is injective before
  running.
- **Why:** Two candidate faces sharing a two-word prefix wrote the same file; one silently
  overwrote the other's measurements. Only an independent script with different naming exposed it.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_face_sweep_naming.py::test_a_realistic_candidate_list_is_collision_free`

### L0050 · Only the orchestrator writes "finished"
- **Rule:** Only the worker orchestrator writes an order's `finished` bus line, and only after
  Deliver succeeds (or the workflow halts). Build/Fix/Audit phases never do.
- **Why:** A fix-phase agent wrote its own `finished` line before audit and delivery, so the board
  briefly showed the order complete with no registry entry or verdict.
- **Enforced by:** `code/reel-production-tools/orderd/lane_prompt.py`; review checklist.

### L0051 · Preflight runs tests under an interpreter that has pytest
- **Rule:** Treat a preflight "kit tests FAILED: ?" with no real failure text as a tooling
  defect; cross-check by running pytest directly. Preflight's `kit_tests()` must invoke an explicit
  interpreter that has pytest installed, not whatever `sys.executable` resolves to.
- **Why:** `sys.executable` resolved to a Python without pytest, so the finish gate blocked even
  though the full suite was green.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_preflight_kit_tests_interpreter.py`

### L0052 · Identity pool and blacklist are code, not prose
- **Rule:** Every cast declares identity per slot (`"protagonist"`/`"none"`). An identity slot may
  only use a stem in `identity_pool.json` → `settled_pool`. No slot may use a `blacklist_full` stem
  (under any identifier) or an in-point inside a `blacklist_spans` window.
  `onetoone.preflight --cast` runs `onetoone.identity.check_cast` and blocks on any violation. The
  lists change only on an explicit human ruling, in the same change as the test.
- **Why:** The pool, the not-the-protagonist list and the blacklist lived only in prose and
  receipts; preflight didn't check them, a caster filtered on an untrusted tag, a loader read the
  wrong key and some bans had no match keys — so a banned clip could be cast.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_identity_pool.py`

### L0053 · Verify cuts on the decoded file; sanitise reports
- **Rule:** After any render, find frame-difference peaks on the decoded file and compare to the
  cut-grid cut frames — every cut must peak, none may sit one frame late. Segments of 2 frames or
  fewer are not safe through a stream-copy concat (see L0076). Strip absolute home paths from any
  report copied into `deliver/`.
- **Why:** A 2-frame segment concatenated one frame late and shifted every later cut; a report
  shipped absolute local paths.
- **Enforced by:** review checklist.

### L0054 · Verify streak cuts frame by frame; never write a None fit score
- **Rule:** Verify each cut in a fast streak by viewing reference frames n−1, n, n+1 before
  locking the cut grid. When a reference ink fails the gate on your bed, follow L0056/L0057 (don't
  bypass, don't silently swap). Never write `score: None` into `ref_fit`; delete `ref_fit` instead.
  Refit scores < 0.6 fall back to box fitting.
- **Why:** Streak-run boundaries mis-measured from a peak list; a `None` score crashed the
  typesetter.
- **Enforced by:** review checklist.

### L0055 · Identify the reference's faces from its pixels before building captions
- **Rule:** Every new reference gets its faces *identified* from its own pixels: clean ink crop →
  font identification (a font-ID service or manual comparison) → overlay the named face on the
  reference frame at the fitted size; glyph-for-glyph overlap is the proof, saved in `work/`. The
  caption part then records `face_file` / `face_name` / `face_evidence`. A script word is *one*
  connected run fitted with `onetoone.refit.fit_part` (size + slight negative tracking), never a
  capital + tail with positive tracking. Search your installed, licensed fonts (system and user font
  folders, configured font directories) before calling a face unavailable; if you don't hold a
  licence, the CALL names the face and its licensing options (see L0064).
- **Why:** A script word was set in a different project's script face, as two runs, so it read as
  separate letters; the substitute was "disclosed" instead of the real face being found.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_named_face_and_state_shadow.py::test_a_part_that_names_its_face_is_set_in_that_face`

### L0056 · Shadow is per caption state, only where flat ink fails
- **Rule:** A state wears the directional shadow only when its flat ink fails the readability gate
  on your bed. Build with `shadow: false` everywhere, run the gate, and add the shadow only on the
  states it names. Dark ink never gets a dark shadow; if dark ink fails flat, recast the bed or
  CALL — don't flip the ink colour.
- **Why:** A cast-level black shadow on every state turned dark-coloured words on a bright sky into
  muddy blobs and made them *fail* the gate (dark ink on a dark ring), leading to inks being flipped
  to white against the reference.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_named_face_and_state_shadow.py::test_state_can_wear_no_shadow`

### L0057 · Reference inks are measured and listed; gate conflicts become a CALL with numbers
- **Rule:** Every caption state's ink is measured from the reference before the build (flat colour =
  its median; footage fill = disclosed as a device), and every non-white reference ink is listed on
  the card. When a reference colour fails the gate even with the standard shadow, neither bypass nor
  swap: CALL with both measurements (kit halo ratio and decoded band ratio) and the options (recast
  on a darker bed, or a heavier shadow by explicit ruling).
- **Why:** The kit gate refused a reference red (halo 2.58) while an audit's band test measured
  4.2; separately a flat-pink reference word had shipped white undisclosed.
- **Enforced by:** review checklist.

### L0058 · Person-free plates are a last resort; check per-shot colour balance
- **Rule:** When the reference shot has a person, cast the protagonist from the settled pool first.
  When a white caption needs a shadow, recast the bed darker before reaching for the shadow. Measure
  per-shot mean B−R on the decoded build against the opening shots; a shot that swings cool against
  a warm house look is recast, not graded.
- **Why:** A window was filled with person-free plates (rejected as empty); white plates read blue
  (B−R +30 against a warm opening at −9); words wore a shadow the opening words did not.
- **Enforced by:** review checklist.

### L0059 · A Deck button is wired only when every hop is wired and tested
- **Rule:** A Deck button counts as wired only when the runner classifies its receipt, the lane
  prompt has a brief for it, and a test proves both. A variant batch keeps the approved version's
  brain byte-for-byte (cut grid, captions, faces, ink, devices, grade); only the footage behind the
  shots changes — N casts, one batch audit, a batch card, keep/kill per variant in the Deck.
- **Why:** A Deck "order variants" button wrote a receipt type no lane brief handled; the runner
  would have treated it as a new project or stalled.
- **Enforced by:** `code/reel-production-tools/orderd/tests/test_variants_order.py::test_prompt_uses_the_variants_brief`

### L0060 · Ink-vs-bed trade-offs are escalated, not decided inside a build
- **Rule:** When a dark reference ink needs a bright bed and the settled pool has no warm,
  face-visible bright shot, stop at casting and CALL with the trade-off (keep the ink and accept a
  cool or faceless shot; switch to white ink on a warm face shot; keep a person-free plate). Before
  any render, measure per-shot B−R against the opening and confirm a visible face on every identity
  slot.
- **Why:** A shot was recast to the only bright footage available (back of head against haze); it
  read cool (B−R +8) and empty, and failed audit twice.
- **Enforced by:** review checklist.

### L0061 · A named bad time window means recast every shot in it
- **Rule:** When the reviewer calls a time window bad, recast *every* shot in it (abstract/streak
  plates count as empty) to face-visible protagonist shots from the settled pool, warm against the
  opening, no same setup back-to-back; probe candidates through the house chain with caption boxes
  overlaid before casting. When the reviewer rules all captions white, set the cast's
  `caption_ink` to `[245, 245, 250]` (the renderer's ink override) so every state renders white with
  no shadow, and cast beds dark enough that white reads flat; the study file keeps the reference
  inks as facts.
- **Why:** Earlier rounds recast only part of a window the reviewer had rejected; coloured reference
  inks read to the viewer as the font "changing colour".
- **Enforced by:** review checklist.

### L0062 · Style study is the purpose; rights questions go to the human (Revised)
- **Rule:** Remaking a reference's *format* — structure, cut rhythm, caption timing, colour approach,
  devices — with your own footage is the purpose of this factory, and lanes should not stall on
  routine style decisions. Rights questions are still real and are decided by the human owner, not
  an agent: music must be licensed or from the platform's audio library, fonts licensed or open
  (L0064), a recognisable person's voice/words are not reused (L0047), and closely recreating a
  specific creator's work for publication needs their permission. A lane that is unsure raises *one*
  short CALL (L0079) and continues other work; it does not silently proceed and it does not loop.
- **Why:** A project sat idle for a long time because a sub-agent repeatedly declined a step and
  no one routed the question to a human. The fix is a fast, well-formed escalation to the human
  owner; the rights questions themselves always stay.
- **Enforced by:** review checklist; see README "Rights & licensing".

### L0063 · Don't remove access endpoints without notice
- **Rule:** Never remove or change the review app's listening addresses as a side effect of a
  hardening pass; announce and agree first. Never expose the Deck to the public internet without
  strong authentication (prefer a private network/VPN).
- **Why:** A reset removed one of the Deck's two addresses; the reviewer, who used it from devices
  off the private network, found the Deck "down".
- **Enforced by:** review checklist.

### L0064 · Fonts: license the original for publishing; tracing is for matching (Revised)
- **Rule:** For anything you publish, use the reference's typeface under a proper licence, or an
  open-licence face. When the face isn't available, the CALL names the face and its licensing
  options — this is a normal, expected question. The kit's letterform recreation tool
  (`python3 -m onetoone.faceid trace`, one call per clean caption line, `--need` listing the
  characters still missing, one traced face per reference weight; then `faceid fit` on a frame that
  was *not* traced from as proof) exists to *identify, measure and match* letterforms: use it to
  find the right licensed face, to choose the closest open face, or for private style study. A
  substitute face shipped with only a disclaimer is a failed build; so is an unlicensed face in a
  published reel.
- **Why:** A project can stall on an unidentified face, and a close-but-wrong substitute reads as
  wrong immediately. Tracing tooling makes identification and matching fast and measurable, but
  a traced face is an analysis aid, not a licence to publish the original design.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_faceid_trace.py` (trace/fit mechanics); review checklist (licensing).

### L0065 · Human clip grades are casting law
- **Rule:** Clip grades from the Deck grader (`onetoone.grades`, read from `grades.jsonl`) are
  binding: NEVER = full ban everywhere; HERO or ME = the clip shows the protagonist, settled for
  identity slots and preferred wherever the reference shows a person; BROLL and NOTME never go on an
  identity slot. Where the *reference* shows a person (`brain/refpeople.json` from
  `python3 -m onetoone.refpeople <project dir>`, run at intake) the slot is an identity slot and gets
  a HERO/settled clip, never an empty bed. `castscan` filters and orders by grade (HERO first) and
  reads `refpeople.json`.
- **Why:** `castscan` scores the bed under the captions, so an empty frame scored best; nothing told
  it which clips were good, and empty beds were cast under person shots.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_grades_law.py`

### L0066 · No two parts in one caption state may share a text
- **Rule:** Within one caption state, part texts are unique. A per-glyph or per-word study splits
  repeats into sub-states (`<line>.<n>`, same in/out); the render is the union and is identical.
  Judge readability per line, and probe the gate before rendering (graded mid-frame bed + kit
  readability) so reframes are chosen on numbers.
- **Why:** With one part per glyph, the gate looked ink up by text and the last same-text part won,
  so white letters were judged as red and four states were wrongly refused.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_part_text_unique.py`

### L0067 · Trace glyphs from whole instances and read the result
- **Rule:** Trace each glyph from an instance that is whole in the reference (no subject or bright
  background over it); box exactly one glyph run. After every trace, render every glyph the captions
  use at large size and read it, and run the counters test (closed letters keep their counters).
  Re-trace with `--replace` from a clean frame; keep the old face as a backup and keep the re-trace
  only if it matches the reference ink better.
- **Why:** Traced glyphs inherited damage from occluded instances (a D and O set as an open n; a B
  that lost its lower bowl set as R). Fit scores missed it; a side-by-side look sheet caught it.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_built_face_counters.py`

### L0068 · A score never vetoes reading; visibility gates are reference-relative
- **Rule:** Render every glyph used at large size next to the reference ink and keep the version that
  *reads* as the letter, whatever its score. Visibility gates are reference-relative:
  ours ≥ `min(0.60, reference − 0.05)` on the same glyphs — never a flat number the reference itself
  fails.
- **Why:** A higher-scoring glyph whose bowl had collapsed to a slit set "BY" as "RY"; a flat 0.60
  visibility gate failed a line where the reference itself showed only 0.58.
- **Enforced by:** review checklist (look sheet).

### L0069 · An audit finding is a defect class, not a frame
- **Rule:** After fixing a finding on the named shot, re-measure that whole class on every shot
  (e.g. per-glyph visible ink ours vs reference on every frame, not only mid-frames) before
  re-audit. Check the worst frame, not just the state's mid-frame.
- **Why:** A fix round corrected caption occlusion on the one named shot; the final audit found the
  identical defect on the neighbouring shot.
- **Enforced by:** review checklist.

### L0070 · Parallel lanes, never two on one project; resource caps hold
- **Rule:** orderd runs up to `ORDERD_MAX_LANES` lanes side by side (default 2; slot 1 =
  `orderd.lock`, slot 2 = `orderd.lane2.lock`), never two on the same project — a new note for a
  project with a running lane waits. Machine caps still hold: at most 3 heavy ffmpeg processes and
  exactly one transcription process (`whisper_guard`) machine-wide; a lane never fans out build
  agents. The status file carries `lanes[]` and `max_lanes`.
- **Why:** With one lane at a time, a ruling sat unpicked for 45 minutes behind an unrelated lane.
- **Enforced by:** `code/reel-production-tools/orderd/tests/test_orderd.py::test_two_lanes_run_side_by_side_never_on_one_row`

### L0071 · Freshness is judged by setup, not by file stem
- **Rule:** Two adjacent shots from the same set/wardrobe/angle read as a repeat even when they are
  different files. Before rendering, put the cast's first frames side by side and recast any shot
  whose setup repeats a neighbour (unless a jump cut was asked for). A cut the reviewer adds where the
  reference has none goes into `brain/cutgrid.json` as its own shot with an `operator_cuts` note
  (back up first), then is cast and probed like any slot.
- **Why:** Two different clips of the same setup passed the stem-level check and read as a
  repeat.
- **Enforced by:** review checklist (first-frame contact sheet).

### L0072 · Never declare footage absent from a tag search alone
- **Rule:** Also search by shoot date / clip-number series and look at the contact boards of
  untagged clips. Footage that exists but is ungraded still never goes on an identity slot; the card
  names each candidate moment (clip number, what it shows, which slot it would fill) awaiting a
  grade.
- **Why:** Requested footage was reported missing because ~70 library clips carried no tags; the
  series was there all along.
- **Enforced by:** `code/reel-production-tools/library_find.py`; review checklist.

### L0073 · Screen every build for legible signage, credentials and screens
- **Rule:** A HERO grade is about identity, not privacy. Before audit, OCR every 2nd–4th decoded
  frame and view each public-venue shot at native resolution in crops. Any legible sign that reveals
  affiliations or location, any credential, or any readable screen is
  recast or cropped out, then re-rendered. List what was removed on the card.
- **Why:** Well-graded event clips carried readable third-party text and a sharp screen; they
  shipped until a native-resolution look.
- **Enforced by:** `code/reel-production-tools/blackout_scan.py`; review checklist.

### L0074 · Measure devices with the reference's own frame count
- **Rule:** Call `measure(brain, refframes, frames_total=<cutgrid geometry.frames>)`, or have the CLI
  read the frame count from `brain/cutgrid.json` / the reference probe. Check: a state starting after
  the old cap on a longer reference must not come back STATIC.
- **Why:** The `measure_devices` CLI hard-coded one reference's frame count (193); on a 282-frame
  reference every state after f192 measured as static.
- **Enforced by:** review checklist; known bug, see `05_CODE_GUIDE.md`.

### L0075 · Run per-glyph reference-relative checks on every state before audit
- **Rule:** Before any audit, run the per-glyph reference-relative visibility check on every caption
  state, normalising against the reference when your own max never reaches the reference's ink. A
  per-glyph burial the reference doesn't share is a hard defect when the brief says "captions clear".
- **Why:** Buried letters were carried for several versions because line means passed and own-max
  normalisation hid letters buried on every frame.
- **Enforced by:** review checklist.

### L0076 · Never stream-copy concat B-frame segments; check decoded frame count
- **Rule:** After every render, decode the delivered file and check each reference cut c: the frame
  difference at c must exceed the one at c+1, and the picture must decode to exactly the cut-grid
  frame count. Join segments with a decoded concat (concat filter, or re-encode segments with
  `-bf 0`), never `-c copy` of B-frame segments. Until fixed in the kit, re-join the kit's segments
  with a decoded concat and disclose it.
- **Why:** Stream-copy concat of x264 segments gave one packet a wrong duration and dropped a 1-frame
  segment; the picture decoded one frame long and every cut after the first landed one frame late.
- **Enforced by:** not yet automated (proposed test: concat keeps one-frame shots); known bug, see
  `05_CODE_GUIDE.md`.

### L0077 · Giant script words are set from measured metrics
- **Rule:** A script word taller than ~400 px in the reference gets `ref_script_metrics` read off an
  outline overlay of the identified face (the main-run box plus the tail's x-height and baseline), so
  the uncapped two-run solve sets it. Never raise a sub-threshold template score to force it through;
  check the overlay by eye.
- **Why:** Box fitting caps at size 600 and the template fit scored 0.52 (under `REF_FIT_MIN_SCORE`),
  so a 470 px word was set at ~65% size.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_giant_script_two_run.py::test_giant_script_is_set_from_metrics_past_the_box_fit_cap`

### L0078 · Measure footage-filled ink per letter; list every weak fit
- **Rule:** For footage-filled states, measure ink per letter and take the median of letters that are
  *not* footage-filled (or the solid component), never the whole-mask median; confirm the colour
  family on the side-by-side. Every part with `ref_fit < REF_FIT_MIN_SCORE` is listed on the card
  with its rendered width vs the reference ink box; anything outside ~90–110% of reference width is
  refit or escalated before audit.
- **Why:** A whole-mask median dragged a silver word to dark teal; a weak fit drew a word at 77% of
  reference width, and the card listed 3 of 17 weak fits.
- **Enforced by:** review checklist.

### L0079 · A CALL is one short question with numbered options
- **Rule:** A CALL is one question of ≤ 320 characters with numbered options (`1 = …, 2 = …`); the
  bus tool refuses an essay or an option-less CALL. The Deck shows only the newest *open* CALL as one
  card with option buttons directly above the comment box. An answered or retired CALL is closed on
  the bus *and* in the registry's open calls, or it haunts the project as "waiting on you".
- **Why:** A project page showed four stale, multi-paragraph questions far from the comment box; the
  reviewer couldn't tell what was being asked.
- **Enforced by:** `code/reel-production-tools/orderd/tests/test_busline_call_shape.py`

### L0080 · Read every shot at native resolution before render
- **Rule:** Before any render, view every identity and bed shot at native resolution, cropping in on
  any background that holds text. Venues with dense signage, posted credentials or street frontage
  need a native-resolution sweep of the exact span before casting; shop interiors carry readable
  advertising — crop or recast. Prefer clips from the protagonist's own controlled sets.
- **Why:** An eye check on thumbnails and side-by-side halves missed several pieces of readable
  third-party text.
- **Enforced by:** review checklist; `code/reel-production-tools/blackout_scan.py`.

### L0081 · Budget-spent is not a reason to stop (superseded in part by L0092)
- **Rule:** Measurable defects are fixed, not escalated. A lane CALLs only for a real human decision
  (missing footage, a rights question, spending money). An unaudited build is never delivered and
  never handed to the human to audit by eye. (The original multi-round audit budget is replaced by
  L0092's one-audit protocol.)
- **Why:** Three lanes ended with a CALL saying "fix round spent" / "audit budget spent" on defects
  they could have fixed.
- **Enforced by:** `code/reel-production-tools/orderd/lane_prompt.py`; review checklist.

### L0082 · Fix the measurement box before blaming the fit
- **Rule:** For a plain part with `ref_fit < 0.6`, check `bbox_settled` against the reference's real
  ink extent including descenders (g j p q y) before rendering. For footage-filled words, run
  `faceid fit` on a *union* mask (solid ink OR fill colour), read the overlay and store that fit with
  its source. Never raise a score by hand.
- **Why:** A settled box stopped above a y-descender and half the word was footage-filled, so the fit
  scored 0.52, fell back to a box fit and drew the word at 117 of 152 px.
- **Enforced by:** review checklist.

### L0083 · Occlusion probes use the reference's ink mask; recast when there's no headroom
- **Rule:** A pre-render burial probe takes each glyph's ink mask from the *reference* at its best
  frame (never from your own rendered caption layer) and gates per glyph on every frame against the
  reference's visibility. When a full-width caption band sits where a master pins the head (no
  headroom, head wider than the word gaps), recast to a clip with headroom instead of another
  reframe round.
- **Why:** Three rounds reframed a master that could never clear the word; a probe that used the
  builder's own matte silently skipped a glyph buried on every frame.
- **Enforced by:** review checklist.

### L0084 · Check low-confidence device frames by eye; avoid crowded venues
- **Rule:** Any device-curve frame with fit < 0.7 or blur sigma > 3 is compared to the reference
  frame by eye before render; if the reference word is sharp, set that frame to settled values and
  note the override. Avoid casting venues with strangers in sharp focus or readable third-party
  notices; prefer protagonist-alone HERO/settled clips.
- **Why:** `measure_devices` fitted a blur-in at fit 0.59–0.64 on footage-filled letters and shipped
  25 px blurs on words that are sharp in the reference.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_low_fit_blur_overridden.py`

### L0085 · Pad glyph masks before tracing
- **Rule:** Before tracing a glyph mask into an outline, pad it with a zero margin of ≥ 8 px on
  every side and offset `base_y` by the pad; never trace or blur a crop whose ink touches the border.
  Proof is a 500 px render of `i n h l k` compared with the previous face.
- **Why:** Glyphs cropped tight to their ink put the stem on the image border; the blur reflected it
  and the contour followed column 0 row by row, producing ledges and notches on stems. The kit's
  `faceid.trace` has the same tight crop (known bug).
- **Enforced by:** review checklist; known bug, see `05_CODE_GUIDE.md`.

### L0086 · Removable-volume permissions follow the executing binary (macOS)
- **Rule:** On macOS, a lane that must read an external work drive needs its runner's interpreter
  (and the agent CLI it starts) to hold the disk permissions it needs — grant them Full Disk Access
  or Removable Volumes access. Never blame the CLI version for `EPERM` on the drive: test `/bin/ls`
  from the same runner first. A lane hitting `EPERM` writes BLOCKED with the exact path and stops —
  it's a machine fault.
- **Why:** Every lane lost the drive at once because the process running them had no grant of its
  own and had only inherited access that later went away.
- **Enforced by:** setup checklist (`01_ARCHITECTURE.md` 8.4); the studio's input preflight refuses
  a job whose inputs are unreadable instead of hanging.

### L0087 · Never resume a cast on a stale identity check
- **Rule:** Re-run `onetoone.identity` against the *live* grades before every render and recast any
  slot the new grades reject. Kit tests that check pool rules monkeypatch `grades.GRADES_PATH` to an
  empty file so live grading can't flip them.
- **Why:** Grades changed between cast and render; preflight failed 40 minutes later, and a kit test
  that read the live grades file broke.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_identity_pool.py::test_identity_slot_outside_pool_and_unruled_are_rejected`

### L0088 · Shortlist before castscan; clean up its output
- **Rule:** Run `castscan` with `--stems` (a shortlist of HERO/settled stems that fit the role,
  picked from a contact sheet) and delete the `--out` PNG dump as soon as the pick is made. A resumed
  lane re-runs identity + preflight on the earlier cast before rendering.
- **Why:** A scan over every master overran the lane time limit and left 4 GB of PNGs, pushing the
  work drive under its floor.
- **Enforced by:** review checklist.

### L0089 · Detect footage-filled letters before setting any ink
- **Rule:** Look at the reference's settled letters at native size before choosing ink: if colour
  varies inside the glyphs (`ref_fit < 0.6` is the tell), measure the in-glyph low-frequency colour
  field and rebuild it with detail from your own footage, place the word by luma-edge alignment
  instead of a flat-ink fit, and set the face from the reference ink. Gate per letter, not only the
  median.
- **Why:** Every word was drawn in flat white where the reference's letters were filled with images;
  flat-ink fits also misplaced those words by up to ~40 px.
- **Enforced by:** `code/reel-production-tools/onetoone/tests/test_footage_filled_ink_not_flat.py::test_project_brain_has_no_flat_low_fit_part`

### L0090 · Check the whole played window; measure ghost beds; smooth traced outlines
- **Rule:** (1) Check grades over the whole played window `[in_s, in_s + frames/fps]` for every slot,
  not just the in-point. (2) A ghost/translucent word needs a measured bed: mean RGB in the word box
  on the cast frame within ~30 of the reference's bed. (3) Traced outlines are smoothed along their
  length (circular Gaussian ~1 source px), long near-vertical runs are snapped to a line, then
  simplified; refit with a glyph-gap check (each glyph its own component at the fit size) instead of
  free negative tracking.
- **Why:** A slot started on a good clip but played 3 frames into a banned segment; ghost words passed
  the gate yet read grey on a near-black bed; traced faces kept compression jitter and fused letters.
- **Enforced by:** review checklist.

### L0091 · Screen picks at ≥ 960×540 before render
- **Rule:** Screen every pick at 960×540 or larger (the house-graded frame at the slot's mid time)
  before rendering; a 480×270 contact sheet is for shortlisting only. Logos, brand labels, signage,
  plates, strangers or a second person's hand anywhere in frame = reject, logged in
  `eye_rejects.json` with the reason; recast and re-screen new picks until a pass adds no rejects.
- **Why:** Low-resolution screening passed picks carrying a stranger's hand, a branded garment and
  plates; a 960 re-screen then rejected 25 more.
- **Enforced by:** review checklist.

### L0092 · One independent audit; its findings are the fix list
- **Rule:** One independent audit per lane. Fix every finding, prove each fix with a targeted machine
  check on the delivered bytes, put finding → fix → proof on the approval card, deliver. No second
  full audit loop, no fix-round counting, no CALL on audit findings.
- **Why:** Lanes spent hours on repeated audits and round counting instead of fixing what the first
  audit found.
- **Enforced by:** `code/reel-production-tools/orderd/lane_prompt.py` (standing phase/audit lines);
  the card's finding/fix/proof table.

### L0093 · Praised shots are locked; fix occlusion in the caption layer first
- **Rule:** A clip or framing the reviewer has praised (or not complained about) is locked. Fix
  "captions buried" in the caption layer first: draw only the glyphs your subject hides (where the
  reference shows them) in front of the subject for the whole state, keep every other glyph behind.
  Recast or reframe a praised shot only when the reviewer names it.
- **Why:** A fix recast a praised opening clip to clear captions; the reviewer rejected it twice and
  asked for the original back.
- **Enforced by:** review checklist.

### L0094 · Adding match cuts to a remake
- **Rule:** Keep the reference cut grid and caption timing; recast only the two sides of chosen cuts
  with a pair from the match-cut candidate list whose *both* sides pass identity
  (`onetoone.grades.identity_slot_reason`), settled and blacklist checks at their in-points. Use crop
  zoom 1.0 on both sides so the setup stays registered; prefer reference blocks that already repeat a
  setup. Prove each match cut on the decoded file with an edge-map correlation far above the
  project's ordinary cuts.
- **Why:** A mined list of match-cut pairs had never been filtered for identity; most pairs sat on
  ungraded or B-roll clips.
- **Enforced by:** review checklist (edge-map correlation report).

### L0095 · Pinned-frame colour fixes: own percentiles, iterate on graded output, protect faces
- **Rule:** Measure the pinned frame vs the reference frame numerically before and after (mean RGB
  outside the caption band). The cited block gets its own reference balance: split points at that
  block's *own* luma percentiles, white balance iterated on the *graded* output, shadows left at the
  act balance when the reference shadows are near-black (their ratios are noise). Then look at faces:
  if a full match pinks skin, use partial strength and say so. (Applies only when reference-matched
  colour is explicitly requested — see L0102.)
- **Why:** A block read ~5× too bright and orange against a near-black red-magenta reference; a
  split-tone weight band (luma 0.1–0.6) never reached a dark block's highlights, and a full match
  turned a lit face pink.
- **Enforced by:** review checklist.

### L0096 · If the shadow is rejected, fix the bed
- **Rule:** When the caption shadow/outline has been rejected and flat ink fails the gate, recast
  the shot to a gated window where the caption zone is darker. The shadow is the last resort only when
  no clean bed exists, and the card says which.
- **Why:** A flat-white build failed the gate where the caption sat over a light garment, after the
  shadow had been rejected twice.
- **Enforced by:** review checklist.

### L0097 · Build a traced face at one pixel scale; keep small marks
- **Rule:** Build one face from every caption state at *one* pixel scale (cap height measured once,
  from the line with the tallest ascender); segment glyphs as connected components and keep small
  marks (apostrophes, periods, asterisks); prove with an IoU fit on frames that were not traced.
- **Why:** Normalising each line to its own tallest glyph made some glyphs oversized; touching
  letters were mis-segmented and the apostrophe dropped.
- **Enforced by:** review checklist.

### L0098 · Trace from a low temporal percentile at half contrast; 0-based caption frames
- **Rule:** Trace faces from a per-pixel low temporal percentile (8th) of the settled frames,
  normalised `(M − bed) / (peak − bed)`, cut at half contrast (0.5). Place each glyph 1:1 on its
  reference glyph centre (no single tracking value). Choose softening by edge width / profile MAE
  against the reference, not a fixed floor. Caption state frames are 0-based and come from the
  reference's own ink-per-frame curve; prove with ink pixels > 0 at the first and switch frames.
- **Why:** A single-frame trace at a 50% threshold eroded strokes (10 px vs 14), made edges too crisp
  (1.59 px vs 2.43), and 1-based frame numbers placed captions one frame late.
- **Enforced by:** acceptance numbers — IoU ≥ 0.83 vs reference half-contrast on untraced frames;
  stroke px and median edge width equal to reference within 0.1; ink > 0 at f0 and at switch frames.
  Review checklist.

### L0099 · Per-block chroma matching protects skin and hair
- **Rule:** When reference colour matching is requested, match per block to that reference block's
  own mean (Cb, Cr), with R/B gains weighted to shadows/mids by a luminance smoothstep so skin, hair
  and practical lights keep their colour (tune the protect band per block: tungsten mids need a higher
  band, magenta ambient a lower one). No highlight cast on a block with faces. Mean matching on a
  different scene can produce hues the reference never shows (red + blue averages to violet): look at
  the side-by-side and disclose.
- **Why:** Global per-act chroma gains turned one block violet and hair pastel pink; adjacent blocks
  swung 39.7° in hue where the reference swings 13.6°.
- **Enforced by:** acceptance numbers — pastel-pink share equals reference on the pinned frame;
  0 magenta frames per block; adjacent chroma swings within ~2 of the reference. Review checklist.

### L0100 · Measurement tools refuse to overwrite deliverables; build review sheets in code
- **Rule:** Any measure tool refuses an output path that isn't `.json`, is under `/deliver/`, or is
  an existing file > 5 MB; deliverables are `chmod 444` as soon as written. Build side-by-side sheets
  by decoding each input separately and indexing frames in Python, labelling every tile with its
  frame number; don't trust a one-liner ffmpeg `select` after `hstack` through layers of escaping.
- **Why:** A measurement script run with swapped arguments wrote `{}` over a delivered mp4; a sheet
  silently paired the wrong frames.
- **Enforced by:** acceptance checks — tool prints "refusing output path"; deliverables mode 0o444;
  tiles labelled `fNNN`. Review checklist.

### L0101 · A variant slot must not reuse the approved slot's setup
- **Rule:** Reject any variant stem whose clip number is within 3 of the approved stem in that slot
  (same prefix), and after render require per-slot mean absolute difference vs the approved decode
  ≥ 10 on every non-scarce slot before calling the footage different.
- **Why:** Neighbouring takes of the same setup differed from the approved shot by only 4.7 mean abs
  and read as the same shot.
- **Enforced by:** `code/reel-production-tools/test_variant_caster.py`; review checklist.

### L0102 · Colour complaints on a house-graded reel don't trigger a new look
- **Rule:** A colour complaint on a house-graded reel is never answered with a reference-matched or
  per-block look. Keep the house pipe, change only what's named. If asked to go back, rebuild the
  previous version's exact chain (prove string-equal and with a same-clip colour match) and change
  only the named item. A slot newly banned by grades is recast and disclosed, never a reason to
  change the grade.
- **Why:** Brightness/colour notes were answered by grading every block to the reference; the result
  was rejected and the real issue had been the font.
- **Enforced by:** review checklist.

### L0103 · Every shot carries the protagonist; screen recasts for logos and faces under words
- **Rule:** Every shot carries the protagonist (visible face preferred); empty scenery or hands-only
  shots are rejected unless asked for. Once ink is ruled all white, every bed chosen for dark ink is
  recast dark and proved with an ink-minus-plate scan on every frame. A recast is done only after its
  frames are screened at native resolution for brand marks (blur with a redact patch, prove with OCR)
  and for a face under a word box (face boxes vs `bbox_settled`).
- **Why:** Shots with no person read as empty; recasts then introduced readable logos and an opaque
  word across a face.
- **Enforced by:** acceptance checks — OCR brand words = 0 on the delivered window; face boxes ×
  word boxes = 0 overlaps. Review checklist.

### L0104 · Predict entry-frame contrast for devices; sample every in-point
- **Rule:** For captions with an entry device (blur/fade), predict the *entry* frame's contrast on the
  candidate bed with the auditor's metric (ink mask from the approved decode, ring dilate10 minus
  dilate3, device opacity + blur) before casting; cast only beds that clear the approved version's
  measured value. Sample every in-point of a window, not one frame. Variant hooks differ by camera
  setup (clip numbers > 3 apart) across the batch. Automated scans honour `eye_rejects.json` and
  require a person count of 1; every new pick is eye-checked at ≥ 960 px.
- **Why:** The gate passed settled frames but entry frames of fading/blurring words fell under 3.0 in
  most variants because the caster sampled one frame and judged beds by mean luma.
- **Enforced by:** review checklist.

### L0105 · Screen the whole cast, with crop applied, before the first render
- **Rule:** Before the first render, screen the *whole* cast at 960×540 at each shot's mid frame with
  the cast crop applied; reject picks where the head is not in frame (back of head is fine) or any
  logo/label/signage/second person is visible. After that, re-screen only new picks.
- **Why:** Legs-only, headless and crop-cut picks survived three screening rounds because each round
  only looked at new picks without the crop.
- **Enforced by:** review checklist.

### L0106 · Per-decoded-frame halo is the gate, not the prescreen
- **Rule:** Treat per-decoded-frame halo (floor 3.0, compared with the approved version's own values)
  as the gate; a one-frame-per-state prescreen can miss bright or moving neighbours. When a slot
  fails, recast to a darker/plainer bed (L0096), never add shadow.
- **Why:** A variant passed the prescreen but dipped to 2.52 on three frames over a bright bed.
- **Enforced by:** `code/reel-production-tools/variant_render.py` verification; review checklist.

### L0107 · Size the scarce slot first
- **Rule:** For an N-variant batch, gate-search the hardest slot across the whole settled pool before
  casting others; if clean stems are scarce, cast disclosed in-point repeats (different in-point and
  reframe) rather than a weaker stem, and list every repeat on the batch card.
- **Why:** One slot needing a plain darkish bed with the protagonist had only 6 clean stems for 10
  variants.
- **Enforced by:** review checklist.

### L0108 · Preflight is the disk authority; split words explicitly in zsh
- **Rule:** The preflight machine line is the only disk-floor authority (decimal GB). When it blocks,
  clear only your own lane's scratch and re-check — never another lane's folders. In zsh scripts,
  split with `${=var}`, and sanity-check a search driver's first output before trusting "none found".
- **Why:** GiB vs GB readings disagreed; a zsh loop using `set -- $job` silently ran 81 empty
  searches because zsh doesn't word-split by default.
- **Enforced by:** review checklist.

### L0109 · Gate captions per glyph on the decoded delivery
- **Rule:** Gate captions *per glyph* on the decoded file (glyph = connected component of caption
  alpha; band 4–8 px outside the full alpha). When a footage-filled word's glyphs straddle light and
  dark, set that word's ink to the reference's measured median colour as a solid field (your detail
  kept at a fraction), then choose beds by worst glyph against that ink. Never a shadow, never a
  flipped ink.
- **Why:** A fill swinging from L 0.013 to 0.598 inside one word cannot reach 3.0 per glyph on any
  bed; a box-median prescreen predicted passes the decoded file failed (1.06–1.48).
- **Enforced by:** acceptance check — every state's worst glyph ≥ 3.0 on the decoded file. Review checklist.

### L0110 · A silhouette is a person; pair grades aren't identity grades
- **Rule:** Face-only detection misses backs, profiles and small figures. Before declaring any slot
  identity "none", eye-check the reference frames of that window *and* your clip; if either shows a
  person, the slot needs a HERO/ME window. A match-cut pair grade is not an identity grade; an
  ungraded or B-roll clip with a person can't carry the slot. Ask for the grade rather than casting
  around it.
- **Why:** Slots were declared "none" because the face detector saw nothing, while the reference
  showed a person from behind; the cast showed other people.
- **Enforced by:** review checklist.

### L0111 · Tune blur devices with an offline per-glyph sweep
- **Rule:** Before a full build, tune a blur-in device with an offline per-glyph simulation (blur the
  settled layer, composite on the caption-free frame, run the per-glyph gate over a sigma sweep) and
  pick sigmas from the passing set closest to the reference fit; disclose any frame left early.
- **Why:** Gate response to sigma is not monotonic: at σ 2.0–2.5 a blurred script splits into a thin
  component reading 1.25–1.39, while σ 1.5 and 3.0–3.5 pass.
- **Enforced by:** review checklist.

### L0112 · Check traced faces before reuse; spacing comes from reference glyph centres
- **Rule:** Inspect a traced face at 300 px for stair-step edges and bites before reusing it on
  another project; choose a borrowed face by per-line IoU against *this* reference's ink among smooth
  candidates. Spacing comes from the reference's own per-glyph centres with a symmetric 2 px de-weld,
  never a uniform tracking that forces gaps.
- **Why:** A traced face reused from another reference carried stair-steps and bites; a forced 3 px
  ink gap made the line gappier than the reference (per-line IoU 0.62/0.64 vs 0.77 for a smooth
  licensed neo-grotesque bold with measured stroke growth).
- **Enforced by:** review checklist.

### L0113 · Every crop on a person shot carries a headroom constraint
- **Rule:** Every crop on a person shot keeps the whole head with ≥ 6% headroom (and the whole body
  when the reference shows it), checked by a face/head-top sweep over every decoded frame (face box
  top minus 0.45 × face height; CUT < 0, TOUCH < 0.02). A caption-bed crop search without a subject
  constraint will cut heads. When framing, a privacy exclusion and a caption bed conflict, keep the
  framing and the exclusion and give the state the standard directional shadow, reporting the raw
  shadow-free figure.
- **Why:** Crops chosen only for caption beds cut the subject's head on several shots.
- **Enforced by:** review checklist.

### L0114 · Audit crop suggestions are hypotheses; work out the direction
- **Rule:** Before rendering a proposed crop move, work out its direction (lower `cy` = crop window
  moves up = picture moves *down*; lower `cx` = picture moves right) and the predicted face-to-caption
  gap; prove it on the decoded bytes. To widen a face-to-caption gap without cutting the head, lower
  the zoom.
- **Why:** An audit suggested lowering `cy` to move words off a chin; it moved the face onto the
  caption and per-glyph contrast fell to 2.34/2.15.
- **Enforced by:** review checklist.

### L0115 · Resolve "the second clip" against the cut list, not the pinned frame
- **Rule:** When feedback names a clip by order or by content, resolve it against the cut list (shot
  order after every cut, content checked on a decoded still), not against wherever playback was
  paused. A recast requested for a personal-preference reason is a different HERO-graded take framed so no
  caption word is drawn over the face.
- **Why:** A note pinned to a late frame referred to the second shot; the earlier identical note
  never reached a lane.
- **Enforced by:** review checklist.

### L0116 · "Copy project X's font" means face *and* spacing model
- **Rule:** Take that project's face and its tracking/stretch model exactly (from its captions file's
  `ref_fit`), fitting only size and position to this reference. Before swapping in another project's
  traced face, run the cross-face fit (L0028) including the house plain face at negative tracking: a
  tight-tracked grotesque reference is usually a standard bold grotesque, not a traced face at
  positive tracking.
- **Why:** Two traced faces were shipped in a row with positive tracking; the cross-face fit found a
  standard bold face at −0.06 em tracking matched best.
- **Enforced by:** review checklist.

### L0117 · Gate with an encode margin; respect the delivery size limit
- **Rule:** A block wears the standard shadow if any flat-ink frame reads under 3.6 pre-encode (3.0
  floor + 0.35 measured encode drift + margin), and the gate is re-run on the *decoded* file before
  audit. Encode web deliverables at CRF 20–21 (not 18) and check size ≤ 10,000,000 bytes before audit.
- **Why:** A frame measured 3.33 pre-encode and 2.98 after encoding; a CRF 18 encode came out over the
  configured upload size cap.
- **Enforced by:** review checklist.

### L0118 · Deliverables carry their project in the filename; the Deck never borrows files
- **Rule:** Name deliverables `reel<NN>-<REFID>-<MODE>-v<NNN>.mp4` and register them under that name,
  never a bare `vNNN.mp4`. The Deck resolves a project's files only from that project's own lane folder
  (`deliver/` first) and never from another project's folder.
- **Why:** A project's review page played another project's video because both had a `v008.mp4` and
  the Deck resolved bare names globally.
- **Enforced by:** `code/reel-deck/tests/test_app.py::test_a_row_never_plays_another_rows_file`

---

*Adding a lesson:* append `L0119` onwards in the same shape; write the test first where a test can
exist; if it overturns an older lesson, edit that lesson in the same change (L0026).
