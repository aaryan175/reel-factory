# 02 — Craft Playbook

The editing method behind the reference-driven reel factory: how a reference reel is read, how its edit signature is rebuilt with your own footage, and the measured number that decides each call. Every rule here exists because breaking it produced a rejected build or a failed audit. Lesson IDs (`L00NN`) point to [04_LESSONS.md](04_LESSONS.md), where each lesson's failure and enforcement are written out.

Read sections 1–3 before touching a reel. Use the rest as reference while you work. The full numeric reference is section 12.

Related documents:

- [README.md](../README.md): what the system is, quick start, rights and licensing.
- [01_ARCHITECTURE.md](01_ARCHITECTURE.md): components, lanes, stage machine, machine limits.
- [03_QA_SYSTEM.md](03_QA_SYSTEM.md): the four gates, independent audit, preflight, checklists.
- [05_CODE_GUIDE.md](05_CODE_GUIDE.md): module-by-module reference for every tool named here.
- [06_REFERENCE_FORENSICS.md](06_REFERENCE_FORENSICS.md): the 13-step reference breakdown.
- [07_WORKING_WITH_AN_AI_OPERATOR.md](07_WORKING_WITH_AN_AI_OPERATOR.md): running this with an AI agent.

Paths use placeholders: `<REPO>` (this repository), `<WORKDRIVE>` (the big scratch/work disk), `<FOOTAGE>` (your footage library masters), `<REFS>` (downloaded references), `<RENDERS>` (build output). Module names like `onetoone.castscan` refer to `code/reel-production-tools/onetoone/` ("the one-to-one kit"); run them from `code/reel-production-tools/`.

---

## 0. How to read this playbook

### 0.1 Status words

- **PROVEN**: an independent audit read the delivered bytes and every finding is closed.
- **Approved**: the human reviewer said so. Nothing else counts as approval, not a passing test, not an agent's opinion (see [03_QA_SYSTEM.md](03_QA_SYSTEM.md), "four gates").
- **Rule only, no test yet**: the lesson is enforced by discipline, not code. These are the rules most likely to be broken again.

### 0.2 Vocabulary

| Term | Meaning |
|---|---|
| Reference | The existing reel whose edit you are studying and rebuilding |
| Row | One reel project in the registry (one reference, many versions) |
| Brain | A row's machine-readable study of its reference: `brain/cutgrid.json`, `brain/captions.plaintext.json`, `brain/caption_devices.json`, `brain/refpeople.json`, … |
| Cast | The JSON mapping every shot slot of the cut grid to a footage master + in-point + crop |
| Slot / shot | One entry of the cut grid (`S01`, `S02`, …) |
| Caption state | One stretch of frames during which the on-screen text is constant (`c01`, `c02`, …); a state has one or more parts (lines/words) |
| Bed | The picture underneath a caption |
| Master | The full-resolution original camera file. A proxy is a small study copy and is never rendered |
| Protagonist / person A | The person the reel is about. Identity slots must show them |
| House look | The single fixed colour pipeline every shot goes through (section 5) |
| Device | An editing figure: entry animation of a caption, glitch interleave, match cut, flash, whip |
| CALL | A short question to the human with numbered options (only for real decisions) |

### 0.3 Rights and responsible use

Remaking a reference reel is a style study and a personal edit. The playbook is written so the *technique* is reusable; what you are allowed to publish is a separate question:

- **Music:** use audio you have rights to: the platform's own audio library (which attaches the licensed track at posting time) or a track you have licensed. The technique in section 10 (the soundtrack is the master clock, muxed by stream copy) is the same whichever licensed track you use.
- **Fonts:** license the original face, or use an open-licensed face. The tracing/letterform-recreation tooling in section 6.5 exists for *matching and analysis*; for anything you publish, license the original or choose an open font that fits.
- **Close recreations:** get the creator's permission before closely recreating and publishing another creator's work. Study freely; publish thoughtfully.
- **Downloading references:** respect each platform's terms of service when fetching references.
- **People on screen:** cast only footage of people who have consented to appear (section 7), and screen out bystanders, readable screens, credentials and third-party signage (section 7.7).

---

## 1. Principles

### 1.1 The reference is the thing you copy

The creative idea belongs to the reference. The factory's job is to:

1. freeze the exact reference;
2. extract its rule system;
3. decide which layers are copied exactly and which are adapted;
4. map your own authorized footage into its roles;
5. rebuild the rule system and keep its lineage recorded.

Never "invent" a quote, caption treatment, cut pattern or flash that the reference already had, and never claim authorship of the reference's grammar. If a layer's origin is unknown it is recorded as `UNRESOLVED`, never credited to the factory by default.

What a reference is made of (the layer model, `R = {F, T, A, C, M, G, X}`):

| Layer | What it is | Default operation |
|---|---|---|
| F frame contract | raster, frame rate, frame count, duration | LOCK |
| T timeline grammar | shot slots, cut frames, holds, transitions | LOCK |
| A audio | soundtrack, beats, phrases (a licensed track; section 10) | LOCK the timing |
| C captions | words, onset/exit, line breaks, faces, placement, treatment | LOCK (words and letterforms 1:1) |
| M motion grammar | subject action, camera direction, action peak | ADAPT to your footage |
| G grade | lighting family | HOUSE LOOK, not the reference's (section 5) |
| X story | hook → struggle → movement → payoff | LOCK the order |

Only the footage is yours (SUBSTITUTE). The one large exception: **colour is not copied**. Every reel wears the house look (L0018). The rest of the edit signature is the reference's.

### 1.2 Layer operations

Every creative layer gets an origin and one operation:

- `LOCK`: preserve exactly from the named reference;
- `ADAPT`: preserve the function while changing the implementation;
- `SUBSTITUTE`: replace reference footage with authorized footage that fills the same role;
- `DROP`: omit, only by explicit decision;
- `USER_NEW`: genuinely new material supplied or approved by the human.

Required layers in the provenance map: spoken quote/script, lyric/audio words, in-video text, the post's caption, typography, shot-role sequence, cut clock, effects/transitions, audio/cue map, colour/composition family, ending/loop.

### 1.3 Selection philosophy

Prefer a slightly less glamorous clip with a clear, intentional action that peaks on the reference beat over a beautiful but static or generic lookalike. Every pick has intent: is the protagonist doing something, looking somewhere on purpose, framed on purpose? Random camera wander and "a whole lot of nothing" are defects (section 7.5).

### 1.4 Edits are invisible and made from the footage

A device is made from the footage itself: a same-setup interleave, a day→night pass, a clone from one locked-off take, a walk-through time shift, a match cut. The transition boundary is the person or the scene, never a visible line, a shape wipe or a preset. No VHS, noise, strobe, stop-motion, rewind or geometric "time sweep" transitions; those read as template-app effects. A recurring sound cue is answered by repeating the same real-shot micro-edit, never by adding a fake digital glitch and a glitch sound. Slow-motion, stacked filters and unmotivated zoom-ins also read as amateur. See section 8.

### 1.5 The protagonist law

Every shot carries the protagonist, face visible preferred. Where the reference shows a person, the slot is an identity slot. Even where the reference shot is empty (water, a bed, hands), an empty plate of yours is usually rejected; recast to the protagonist doing something (L0042, L0058, L0103). Back-facing or standing is fine as long as they read as the main character.

### 1.6 Captions are the soundtrack's text, not decoration

On-screen text is the reference's words, or, for lyric reels, the sung lyric at that instant, timed to the vocal, in the reference's grammar. Never random theme lines, never "auto-subtitle" styling. Only an explicit instruction from the human changes a reference word. For anything you publish, use your own words or get the creator's permission; copying another creator's on-screen text or lyrics is for private study renders only.

### 1.7 Change only what was named

A note on a version changes only the item it names. Everything the reviewer praised or did not mention is locked. "Go back to how it was" means rebuild the previous version's exact chain (prove it string-equal) and change only the named item (L0093, L0102).

### 1.8 Never self-certify

The builder never declares its own build 1:1. Every candidate gets one independent read-only audit, and the audit's verdict line is what the reviewer hears (L0009, L0092). See [03_QA_SYSTEM.md](03_QA_SYSTEM.md).

### 1.9 The machine builds versions, not the chat

When the reviewer gives a verdict in a chat with an agent, it is filed as a receipt (verbatim) and a lane does Build → Audit → Deliver. Fixing the machine (kit, lane prompt, lessons) is chat work. Hand-building a version in the chat session is not. See [07_WORKING_WITH_AN_AI_OPERATOR.md](07_WORKING_WITH_AN_AI_OPERATOR.md).

---

## 2. Analysing a reference

The full 13-step method is [06_REFERENCE_FORENSICS.md](06_REFERENCE_FORENSICS.md). What the craft depends on:

### 2.1 Freeze the identity

- Save the URL/identifier, the exact media hash and a complete decode (`ffmpeg -v error -i REF.mp4 -f null -` exits clean).
- Hash three layers: container bytes, compressed packet payload (video and audio separately), decoded essence (decoded video hash plus decoded PCM hash and sample count). Different containers may carry identical packets; different packets may decode identically. **Decoded essence is the authority across remuxes; the container hash identifies origin.**
- Never copy a stale manifest hash into a new contract.

### 2.2 Freeze the frame namespace

- Extract zero-based explicitly: `ffmpeg -i REF.mp4 -start_number 0 f%03d.png`. A default one-based sequence maps decoded frame 0 to `f001` and invalidates every board label.
- Fresh-decode into an immutable directory, prove exactly N frames with no gaps or extras, bind every contact sheet to the current frame hashes. A renumbered scratch folder with old sheets produces a plausible one-frame shift at every boundary.
- Caption and cut intervals are zero-based half-open `[start_frame, end_frame_exclusive)` everywhere.
- Time comes from the reference's own PTS clock: store `(start_pts, pts_step, time_base, frame_count)` and derive `t(f) = (start_pts + pts_step*f) * time_base`. Never use `23.98`/`29.97` shorthand. When nominal rate, average rate and PTS disagree, PTS win. Never inherit FPS from a previous revision; re-probe.

### 2.3 Extract before naming

Inspect every frame, text state, cut, effect, cue and ending before you name a font or a grade. Write one **reference rule card** (no footage selected yet), containing:

- timeline driver: spoken quote, lyric, music, action, atmosphere;
- in-video text grammar: none, progressive quote, lyric lifecycle, title cards;
- typography: faces, hierarchy, placement, lifecycle, ink/compositing;
- cut grammar: phrase cuts, beat cuts, hard spatial cuts, flashes, held smears;
- shot-role sequence: the functional order of images/actions;
- continuity world: `single_world` (one subject/location/wardrobe/light family) or `multi_world` (different places unified by quote, lyric or music);
- audio/cue rule: onsets, repeated motifs;
- ending: payoff, hard hold, reset, loop.

Routing labels (e.g. `QUOTE_CAPTION_HERO_WORD`, `LYRIC_TYPOGRAPHY_LIFECYCLE`, `TITLE_CARD_ACTION_ONSET`, `KINETIC_QUOTE_SMEAR_MATTE`, `SINGLE_WORLD_REAL_CUE_MOTIF`, `ARCHITECTURAL_RESET_LOOP`) make searching easier but never replace the rule card.

### 2.4 Machine record shape

```json
{
  "continuity_world": "single_world|multi_world",
  "lineage": {
    "type": "single_reference|multi_reference_explicit|original_user_directed",
    "references": [{"id": "<reference-id>", "rule_card": "reference/<id>.json"}]
  },
  "grammar": {
    "timeline_driver": "spoken_quote|lyrics|music|action|atmosphere",
    "text_grammar": "none|progressive_quote|lyric_lifecycle|title_cards",
    "cut_grammar": [], "shot_role_sequence": [], "audio_cues": [],
    "ending": "payoff|hold|reset|loop"
  },
  "adaptation": {
    "mode": "exact_reference|reference_grammar",
    "layers": {
      "in_video_text": {"origin": "reference:<id>", "operation": "LOCK"},
      "typography":    {"origin": "reference:<id>", "operation": "ADAPT"},
      "footage":       {"origin": "authorized_pool", "operation": "SUBSTITUTE"}
    }
  },
  "authority": {"technical": "PASS|FAIL", "visual": "PASS|FAIL|PENDING",
                "human": "APPROVED|REJECTED|PENDING", "publication": "PUBLISHED|UNPUBLISHED"}
}
```

---

## 3. 1:1 versus grammar-adapt

### 3.1 The lanes

- **Exact-reference adaptation ("1:1", `reference-locked`)**: exact clock, picture states, audio timing, caption and effect timing, transition lifecycle. This is the default.
- **Reference-grammar adaptation (`original-montage` internally)**: keep the reference's functional grammar (cut grid, beat grid, captions, effects) while substituting footage. Used **only** when the reference's world does not exist in your library (for example, most of the reference is a night city skyline and your library has none) **and** the human says go.
- **Explicit multi-reference adaptation**: different layers come from different references; each layer names its source. Never record a multi-reference build as a pure reconstruction of one of them.

### 3.2 Decision procedure

1. Run feasibility: for each slot, does the library hold an exact role, an equivalent role, or nothing?
2. If 1:1 passes, build 1:1.
3. If 1:1 is infeasible, CALL with the measured gap and wait. Grammar-adapt only on the human's word.
4. Never invent unreferenced caption or effect behaviour to hide missing footage. Missing footage = adapt the grammar and disclose each substitution.

### 3.3 Captions stay 1:1 in both lanes

In study renders, words and letterforms match the reference 1:1, in both lanes. A historic attempt to let a batch use "our own words" was reversed hard; the only legitimate change to a reference word is an explicit instruction for that word.

### 3.4 Authority domains when two videos are involved

When a typography exemplar and the target reel are different videos, assign authority per layer before touching the timeline:

| Layer | Default authority |
|---|---|
| picture/cut clock, frame count, shot slots | target reel |
| audio | target reel or a separately locked (licensed) track |
| words, case, punctuation | target reel, or an explicit override |
| caption intervals, accumulation/replacement | target reel |
| placement envelopes, scene-relative composition | target reel |
| glyph anatomy, script/sans grammar, hierarchy | typography exemplar |
| lifecycle *method* (detectable/readable/crisp/hold/exit) | typography exemplar |
| exemplar's literal frame numbers | non-authoritative unless explicitly adopted |
| ink/compositing | declared target or a labelled adaptation; never silently blended |

- Do not stretch a target to the exemplar's length because the exemplar is longer. Spare native handle proves mechanical feasibility only.
- Where target and exemplar disagree on face-class role, casing or phrase treatment, record an `AUTHORITY_CONFLICT`, render A/B variants under identical placement/timing/ink, and stop for a human choice.

### 3.5 Alternate versions of an approved reel

An alternate keeps the approved version's brain locked byte for byte (cut grid, captions, faces, ink, devices, grade); only the footage changes. Alternates are whole new casts, not caption swaps. Footage-difference rules are in section 7.6.

---

## 4. Cut grid and beat sync

### 4.1 The soundtrack is the master clock

Visual timing is derived from the reference, never from a guessed BPM. Every cut lands on the music grid; align **action peaks** to the audio, not merely the cut boundaries. In device-heavy reels every cut is also a caption change.

Cut density matters: a remake that matches the first half's cuts but collapses density in the back half reads as "off the beat" even though each landed cut is correct. Compare cut counts per second across the whole timeline. Beat drops get emphasis: hold or accelerate where the reference does.

### 4.2 Lock the cut grid from decoded frames

- Generate candidates with scene detection (see [06_REFERENCE_FORENSICS.md](06_REFERENCE_FORENSICS.md) for thresholds), then **verify every cut frame by viewing reference frames n−1, n, n+1** before locking `brain/cutgrid.json`. A peak list once put a streak cut at f120 when it was f122 (L0054).
- A cut detector is a candidate generator, never the picture clock.
- A cut the human adds where the reference has none goes into `brain/cutgrid.json` as its own shot with an `operator_cuts` note (back the file up first), and is cast and probed like any other slot (L0071).

### 4.3 Prove the cuts on the delivered file

- Decode the delivered file and check each reference cut `c`: the frame difference at `c` must exceed the difference at `c+1`. Put the cut-check JSON on the review card (L0076).
- The picture must decode to exactly the cut-grid frame count. Every cut must peak; none may sit one frame late (L0053).
- Clean-cut evidence from a passing build: cut differences 10.3–208 against every intra-shot difference ≤ 5.9; no stray cut; no byte-identical consecutive frames except where the reference itself holds on twos.

### 4.4 The concat trap (B-frame segments)

Joining x264 B-frame shot segments with the concat demuxer (`-c copy`) breaks very short shots: at 2-frame and 1-frame shots the previous segment's last packet took a long duration and the 1-frame segment was dropped, so the picture decoded one frame short and every later cut landed +1 (L0076). Rules:

- Join with a **decoded** concat (concat filter), or encode segments with `-bf 0`. Never stream-copy B-frame segments.
- Treat segments of ≤ 2 frames as unsafe through any stream-copy concat (L0053).
- Segments whose in-point falls early inside a master frame can start at a non-zero PTS; check first-frame PTS per segment.

### 4.5 Converting human time references

The reviewer's time stamps ("the clip at 3.5 s") are playback positions: convert to frames at the reel's rate. When a note names a clip by order ("the second clip") or by content, resolve it against the cut list first (shot order, content checked on a decoded still), not against the pinned frame, which is just where playback stood (L0115).

---

## 5. Colour

### 5.1 The house look

The grade is a single fixed **house look**: one camera-maker Rec.709 conversion LUT (for example, a Sony LC-709-style LUT for footage shot in S-Log3), the same on every shot, run through one hash-locked pipeline. No per-shot exposure or colour matching unless the reviewer explicitly asks for reference-matched exposure on a named shot (L0018, L0024, L0037, L0102). Supply the LUT file yourself (camera makers publish their official LUTs).

How this was learned: six rounds chased "normal colour" with a hand curve (`curves=all='0/0.045 0.25/0.31 0.75/0.80 1/1'`, `eq=contrast=1.06:saturation=1.22:gamma=1.05`) plus per-shot reference-matched exposure, and every round was rejected as "random colours". Nobody had read what the *approved* reels shipped with. **Before inventing a look, read the approved reels' render contracts** (L0018). The answer was on disk the whole time.

House look means **every** shot, including "creative" treatments copied from the reference: no `light_state` (for example a lights-off gain of 0.25) on any slot unless the reviewer asked for that beat by name. Unintended black frames are a defect (L0024).

### 5.2 The house pipe (and the bare-LUT trap)

```
scale (lanczos)
 -> in_range=full, in_color_matrix=bt709
 -> format=gbrpf32le
 -> lut3d (interp=tetrahedral)
 -> format=gbrp16le
 -> colorspace=ispace=gbr:iprimaries=bt709:itrc=bt709:irange=pc:all=bt709:range=tv:format=yuv420p:dither=fsb
```

Implemented in `onetoone/housechain.py` (`house_vf`, `house_head`, `HOUSE_TAIL`). `REQUIRED_TOKENS`: `in_range=full`, `in_color_matrix=bt709`, `format=gbrpf32le`, `interp=tetrahedral`, `format=gbrp16le`, `colorspace=`, `range=tv`, `all=bt709`.

Why: log camera masters are often **untagged full (pc) range**. A bare `lut3d` makes swscale assume BT.601 at 8-bit trilinear and the picture ships cooler and darker. Measured on one frame: bare chain R/B 1.96, b* 6.5, L 10.9; house pipe 2.26 / 9.4 / 13.1. The house pipe reproduced an approved reel's frame of the same master to 0.03 mean RGB; the bare chain was off by R −4.2 / B +3.3 (L0037).

- **Proof standard**: "colours like the other reels" is proven by reproducing an approved reel's frame of the SAME master to < 1.5 mean absolute difference. Never by eye, never by a hand tint.
- **Input range**: an intermediate ProRes plate that is genuinely tv-range must enter `house_head(..., in_range="tv")`. Feeding it as full range was 6 levels wrong.
- **Tag your outputs**: deliverables carry bt709 primaries/transfer/matrix and tv range in the stream metadata. Untagged files get guessed differently by different players and measuring tools.
- **Decode for measurement with player-exact planes** (`onetoone.yuvexact`), never swscale `rgb24`, which reads about 2 levels dark (ink 252.7 instead of 254.9). A composite decoded via swscale bakes that error into the delivery (L0037, L0038).

### 5.3 Never mix pipes in one build

When the colour pipe changes, the next build of every affected reel renders **all** shots on the new pipe. Never ship one recast shot on the new pipe beside the rest on the old one. A kit change that alters shipped pixels is announced with the measured delta (L0039).

### 5.4 Crop before colour conversion on subsampled masters

A fractional crop offset (x = 58.6, `exact=1`) applied to a native `yuv422p10le` master cropped chroma at an odd offset and rendered a whole shot solid magenta (mean RGB 237/103/252 instead of about 182/178/164). The old chain converted to RGB first and hid it; 120 unit tests did not see it (L0041).

- Insert `format=yuv444p16le,{crop},` before `house_head`, or use even-integer x/y offsets on subsampled masters.
- `render.chroma_sanity` runs on every shipping segment: decoded mid-frame vs its own master; a shot may not gain more than 0.40 magenta- or green-dominant pixel share (`CHROMA_JUMP_MAX = 0.40`). The broken shot scored 0.934.
- Any colour-pipe change is proven on the **decoded whole build**: per-frame mean vs the previous version and magenta/green share on every frame. The broken build had 15 of 193 frames over 85% magenta; the fix had 0.
- **Test at the reel's real raster.** A repro at 1080x1920 was clean; the reel was landscape 1916x1078.
- A verification twin that rebuilds the render chain by hand must import the renderer's own builder. A stale twin produced a false colour failure (L0045).

### 5.5 Exposure is not colour (reference-matched mode only)

These rules govern the `match`/`natural` grade modes, which you use only when the reviewer asks for reference-matched exposure on a named shot. In house mode there is no pull and no ramp by design.

| Lesson | Failure | Rule | Constant |
|---|---|---|---|
| L0001 | Dropping the colour solve also dropped the luma pull/ramp; 7 of 12 shots ran +30..+107 luma codes over, up to 22% clipped | Dropping colour never drops the luma pull/ramp. In every mode each shot is measured (`lumacheck`) and its clipped share reported | — |
| L0003 | 10.23% of a shot's pixels ≥ 250; `y = x**(1/g)` maps 1.0 → 1.0 for every gamma, so a gamma pull never touches clipping | The pull curve carries a shoulder | `PULL_SHOULDER = 0.94` (255 → ~240); reference at 0.00% clipped is the bar |
| L0004 | A blackout shot (target luma 5.6) shipped at 11.1 because a flat 6.0 tolerance is ±107% of the target | Tolerance scales with target: `min(PULL_TOL, max(1, PULL_TOL_REL × target))` | `PULL_TOL = 6.0`, `PULL_TOL_REL = 0.25` |
| L0005 | Two 0.45 gamma passes composed to ~0.20 and turned a backlit bright subject into a black cutout (16.7% of frame ≤ 2) | Stacked curves compose; the second pass floors higher. Near-black share (≤ 5 and ≤ 2) measured per shot vs previous version and reference | `PULL_GAMMA_MIN = 0.45`, `PULL_PASS2_GAMMA_MIN = 0.62`, `PULL_PASS2_OVER = 5.0` |
| L0006 | A shot's excess luma was all sky; any whole-curve bend steep enough took the midtone subject with it | Compress highlights only: `highlight_pull()` is identity below the knee | `HL_KNEE = 0.20`, `HL_E_MAX = 14.0` |
| L0007 | `curves=all` gives R, G, B the same transfer and drains chroma where brightest: a sunset's R−B fell +66 → +6.8, a dusk blue lost 62% | Highlight compression is luma-only with chroma-follow | `CHROMA_FOLLOW = 0.4` (auditor suggested 0.5–0.7) |
| L0008 | A sky fell to 18 distinct luma levels (sd 2.95); fine for review, bands after platform re-encode | Distinct-level count per sky patch is a **publish** gate: below ~25, back the compression off. Review and publish are different bars | ~25 levels |

Luma ramp (render.py): `RAMP_TOL = 2.5` codes, second pass `RAMP_TOL_PASS2 = 1.5`, never move one frame more than `RAMP_MAX = 40`.

Measurement law:

- Luma = BT.709 full-range RGB weights (0.2126, 0.7152, 0.0722), never ffmpeg `format=gray` (BT.601-ish; it hid a +3.9 error).
- Compare **bed to bed**: mask the caption rectangles on both sides (`onetoone.lumacheck` BED view). Comparing a caption-free segment to captioned reference frames biased dark shots +2.4.
- Ledger numbers come only from `lumacheck` output, never from a hand calculation.

### 5.6 The reference-matched grade engine (`onetoone.grade`)

When a per-shot reference match is explicitly requested, `onetoone.grade` separates three things because they compare across different footage to very different degrees:

- **Tone**: one monotone curve on luma (`interp=pchip`), blended part of the way toward the reference's percentile ladder, clamped per level (hardest at shadows, softest at highlights, since a top percentile is mostly composition), segment slope capped so a stretch cannot stripe a gradient.
- **Cast**: a white-balance-style per-channel gain from the chromaticity of lit pixels, log-blended, clamped, split into a free warm-cool part and a tightly bounded green-magenta part, re-normalised to preserve luma.
- **Saturation**: one multiplier chosen after the curve and the gain, so the three do not compound.

A per-channel percentile match was tried first and must not come back: the two shots' channels describe different objects, so it turned a sunset pink and banded a dusk. The solver runs its own filter on a probe frame in numpy (matching ffmpeg to ~1 code), then walks strength down through `STRENGTHS = (1.0, 0.85, 0.7, 0.55, 0.4, 0.25)` until every guard passes. Guards and blend constants are in section 12.2. If a shot's measurement is unusable, it falls back to the safe global `mood_eq` rather than guessing.

### 5.7 When the reviewer names a shot as off

- Per-block reference matching applies **only** to a shot the reviewer names, and to **that block's own** reference values, never an average of neighbours. Grading one block toward a self-chosen act average widened the gap (L0014).
- A colour or text complaint is **never** ruled "not a defect" by a fix agent. Measure ours vs the reference at the cited frame, move it, put before/after numbers on the card (L0013).
- Per-block matching craft (L0095, L0099): measure the pinned frame vs the reference by numbers before and after (mean RGB outside the caption band); put split points at that block's own luma percentiles; iterate white balance on the **graded** output; leave shadows at the act balance when the reference shadows are near-black (their ratios are noise); match chroma per block to the reference block's own mean (cb, cr) with R/B gains weighted to shadows/mids by a luminance smoothstep so skin, hair and lamps keep their own colour; no highlight cast at all on a block with faces. Look at faces: if a full match pinks the skin, keep partial strength and say so. A red+blue reference averages to violet; check the side-by-side and disclose.
- Enforced numbers there: pastel-pink share equals the reference's (0.0) on the pinned frame; 0 magenta frames per block; adjacent-block chroma swings within ~2 of the reference's swing.

### 5.8 The override: on a house-graded reel, revert, don't chase

A reel graded block-by-block to the reference, numerically perfect (L 5.0 vs 5.2 at the pinned frame), was rejected: the reviewer preferred the previous version's colour, and the real complaint was the font.

Rule (L0102): a colour complaint on a house-graded reel is never answered with a reference-matched or per-block look. Keep the house pipe; change only what is named. If told to go back, rebuild the previous version's exact chain (prove it string-equal and with a same-clip colour match) and change only the named item. A slot that new clip grades ban is recast and disclosed, never a reason to change the grade.

Resolution of 5.7 vs 5.8: default is house. Per-block reference work is permitted only when a specific shot/frame is named **and** the rest of the reel stays house. If whole-reel look drift appears, revert. Ask whether the complaint was really about colour.

### 5.9 Cool shots against a warm house look

Measure per-shot mean B−R on the decoded build against the opening shots. A shot that swings cool against a warm look (for example B−R +30 against a hook at −9) is a defect to **recast**, not to grade (L0058). Check the replacement too: a recast to a pool shot still read B−R +8 (L0060).

### 5.10 Source-type thinking

- Log footage needs a real conversion (the house LUT is that conversion). Reference points for S-Log3 on a 10-bit scale: black ≈ code 95, 18% grey ≈ 420, 90% white ≈ 598. Identify the encoded state first; use a colour-managed pipeline or an explicit colour-space transform, never both.
- Phone or already-graded footage stays close to source. Practical light sources (fire, lamps) keep their saturation.
- "Good" is not "colourful". A close histogram is not proof of a good grade; never histogram-match to a dark reference. A dark grade is a defect, not a look.
- No random colour shifts inside a reel, no grade drift inside a clip, no green casts, no blown-white clips.

---

## 6. Captions

Captions are where remakes fail most often and where a viewer's eye is sharpest. In one diagnosis of the corpus, every caption defect had been caught by human eyes, never by QC. Treat every caption claim as unproven until measured on the delivered bytes.

### 6.1 Four different things "the captions are bad" can mean

Diagnose which before touching anything:

| Complaint | Real cause | Fix class |
|---|---|---|
| Wrong words | built under a wrong policy | words 1:1; diff declared text vs the reference read-back |
| Wrong font | reference face never identified | identify/fit the face from the reference pixels (6.4–6.6) |
| "Muffled, not clean" ink | glyph anatomy damage + weak ink (an "o" that reads as an "a") | contour/topology check, ink separation (6.8, 6.12) |
| Captions missing | QC compared plan vs plan, never read the finished video | presence gate on the delivered file (6.14) |

Colour, font and casting complaints are three separate defects; measure each separately (L0015).

### 6.2 Words and language

- Words are the reference's, 1:1. For lyric reels the text is the sung lyric at that instant, timed to the vocal. Non-English lyrics are allowed in the reference's face.
- Captions are plain text, written exactly as they appear in the final video. Never encode or spell around a content filter (codepoint arrays, state ids). If a filter blocks a build, escalate to the human with the filter text; never reword or re-run to defeat it.

### 6.3 Ink colour

- **Measure every caption state's ink from the reference before the build**: a flat colour = its median; a footage fill (a picture visible through the glyphs) = disclosed as a device (section 6.10). List every non-white reference ink on the card (L0057).
- **A ruling of "all white" for a reel**: set the cast's `caption_ink` to `[245,245,250]` (the renderer's `ink_override`) so every state renders white with no shadow, and cast beds dark enough that white reads flat; the study keeps the reference inks as facts (L0061). Every bed chosen *for* dark ink must then be recast dark and proved with an ink-minus-plate scan on every frame (L0103).
- **When a reference colour fails the readability gate even with the ruled shadow**: neither bypass the gate nor swap the colour. CALL with both measurements (kit halo and decoded band ratio) and the options: recast on a darker bed, or a heavier shadow by ruling (L0057).
- Dark ink never gets a dark shadow. If dark ink fails flat, recast the bed or CALL; do not flip the ink colour (L0056).
- When a dark reference ink needs a bright bed and the pool has no warm, face-visible bright shot, stop at casting and CALL the trade-off (keep the ink and accept a cool or faceless shot / flip to white on a warm face shot / keep a person-free plate). Never pick a side silently (L0060).

Ink comes from the reference's own pixels (caption engine, `code/reelctl/src/reelctl/captions/`):

| Sampling method | Use | Mechanism |
|---|---|---|
| `min_channel_threshold` | white/near-white ink | `min(B,G,R) > t`; robust over arbitrary footage |
| `chromatic_difference` | coloured ink | channel-difference predicates, e.g. `(r−g) > k ∧ (b−g) > k` |
| `temporal_difference` | static ink over moving footage | median of state frames vs median of adjacent **blank** frames; no font, palette or renderer constant enters the measurement |

Forbidden ink sources: hard-coded palettes; a canvas-wide gradient standing in for ink; caption chroma taken from **your** footage instead of the reference; flattened-reference RGB plates (they copy scenery into the letters); raw footage-reactive luma fills; 4:2:0 one/two-pixel colour bleed promoted into a "shadow"; any outline/blur/glow/shadow not proved by an enlarged edge profile. Measured decoded colours are never presented as original design hex values.

**Compositing mode**: never name an editor blend mode from a flattened encode. Fit `O = B + α(F − B)` (source-over) against `O = B + α(|B − S| − B)` (difference family) and compare residual distributions; report "Difference-family inversion", not "Difference". White Difference, Exclusion and Invert are pixel-identical at the white endpoint. Negative ink/underlay correlation and channel-order reversal identify footage-reactive difference/inversion. There is no default compositing operator: fit it per reference, per stack. An explicit "make it white" request supersedes inferred reactive ink, and recolouring cannot fix a wrong face.

### 6.4 Typography: defaults and hero-script words

- A sensible default plain caption face is a Helvetica-class bold (for example Helvetica Neue Bold, TTC index 1 on macOS; `onetoone.captions_typeset.DEFAULT_FACES`). A default is a starting point, not an answer: every new reference gets its faces identified from its own pixels (L0055).
- Script (cursive) faces apply **only** to the reference's hero words. Setting a whole line in script because one word is script is a classic misreading.

### 6.5 Identify, license, or recreate the face

Procedure (L0055):

1. Take a clean ink crop of the reference caption.
2. Get candidate names: upload the crop to a font-ID service yourself (respecting its terms), or compare by eye, and list candidates. `faceid identify` only prints this instruction.
3. Overlay the named face on the reference frame at the fitted size. Glyph-for-glyph overlap is the proof; save the overlay in `work/`.
4. The caption part carries `face_file` / `face_name` / `face_evidence` in `brain/captions.plaintext.json`.
5. Look in your installed, licensed fonts before calling a face unavailable: the system and user font folders and any directories listed in `REEL_FACTORY_FONT_DIRS` (`faceid.py` `FONT_DIRS`). Only use faces you are licensed to use.
6. Never ship a substitute face silently. Enforced by `test_named_face_and_state_shadow.py::test_a_part_that_names_its_face_is_set_in_that_face`.

When the face is not installed, the options are, in order:

1. **License the original face.** This is the right answer for anything you publish.
2. **An open-licensed face** that passes the fit (6.6): `refit.fit_part` scored against the reference's pixels across real candidates.
3. **Letterform recreation for matching and analysis**: `python3 -m onetoone.faceid trace` builds a face from the reference's own ink (one call per clean caption line → `<out>.ttf` + `.glyphs.json` + `.sheet.png`; `--need` lists missing characters; one traced face per reference weight; `--replace` re-traces), then `faceid fit` on a frame that was **not** traced from is the proof. Use this to understand and match the letterforms; license the original before publishing work that depends on it. Enforced by `test_faceid_trace` (L0064).

The caption engine's exactness hierarchy for a face:

1. `metadata-locked exact font`: original face/axes/features known and hash-locked;
2. `raster-verified exact outline`: one local face explains several diagnostic words under **one** global geometry model with a decisive score gap;
3. `hybrid glyph source`: locked face for repeatable glyphs, source-derived components for unavailable capitals/swashes;
4. `reference-derived raster matte`: per-state alpha from the authoritative frames;
5. `Unresolved`: fail closed.

Status vocabulary (exactly one per face class): `EXACT_FONT_BYTES_VERIFIED`, `EXACT_SOURCE_CONTOUR_VERIFIED`, `SYNTHETIC_GRAMMAR_COMPLETION`, `EXACT_FACE_UNRESOLVED`. Installed or visually similar fonts are candidates only. A nearest family is never promoted to "exact font" because it ranked first: one bbox-normalised winner (Dice 0.92099 vs 0.88277) was visibly too condensed on native crops; two independent holdout audits of one reference picked **different** winners.

Non-circular holdout protocol (`fontproof.py`): shape whole words with HarfBuzz (`kern`, `liga`, `calt` on) from the exact file and TTC index; fit **one** size/tracking/global transform per tier; train on several words; **freeze** geometry and score unseen holdout words without refitting; report fit and holdout Dice, 1 px F1, symmetric contour Chamfer, component/hole topology and width/height error; inspect native overlays **after** ranking. Two independent counting metrics must each prefer the same candidate on every holdout word by a margin (the historical false positive had a 0.0129 margin; real holdout Dice sits 0.699–0.921). A blended score (`0.6·dice + 0.4·iou`) is one metric, not two. Normalised correlation is supporting evidence only.

Circular "proofs" that are forbidden: telling the renderer to fill a target bbox and then asserting the bbox matches (`bbox_iou = 1` proves only that the resize ran); resizing a candidate to the reference's width and height before scoring; any gate made only of bbox equality, font hash, frame count and decode success.

### 6.6 Fitting a face to the reference

- Letterforms are fitted to the reference's own pixels (`onetoone.refit`: size, tracking, squeeze, ramp; script metrics for ornate words) with a **no-regress rule** against the previous version's fit scores. Never re-set a face by eye (L0016). Box-fitting from eye-read study boxes is not enough: one such fit came out 16–33% short with loose tracking.
- **Uniform scale only**: geometry has a single scale scalar per state. Per-word anisotropic resize makes one font file look like several fonts (one regression measured axis factors 0.544, 0.485 and 1.025 for different words of one face). Within one editorial tier share face, size, tracking and placement mode, plus at most one evidence-backed global transform. A per-lockup anisotropic fit is allowed only with direct multi-frame evidence, an explicit contract flag, retained measured and clean boxes, a written justification and a failing test first.
- Record the scale convention (is vertical scale carried by nominal pixel size, or by an explicit `scaleY`?). Mixing conventions silently doubles or halves vertical scale.
- Report two boxes per part: the core/50%-alpha ink box **and** the low-alpha glow/shadow extent. A hand-drawn target box is a placement envelope, never measured ink.
- A script word is **one** connected run fitted with `refit.fit_part` (size + slight negative tracking), never a capital plus a positively tracked tail (L0055). Script bounds: `SCRIPT_SQUEEZE_MIN = 0.90`, `SCRIPT_STRETCH_MAX = 1.15`, `SCRIPT_OVERFLOW = 1.08`. Place the script word so its **body** matches the reference (`onetoone.ornate_env`: 5th/95th percentile ink rows in the same mask on both sides), since tails differ between faces.
- Fit thresholds: `REF_FIT_MIN_SCORE = 0.6` in the typesetter (below it the part falls back to box fit); `refit.MIN_SCORE = 0.45`; `refit.INK_TOL = 70` RGB distance; search pad 60 px. **Never raise a sub-threshold score by hand** (L0077, L0082). Never write `None` into `ref_fit`; delete the key instead (a None crashes the typesetter).
- Every part with fit < 0.6 is listed on the card with its rendered width vs the reference ink box. A part rendered under ~90% or over ~110% of the reference width is refit or escalated before audit (L0078).
- A box that cuts off a descender (g j p q y) is a measurement defect. Check `bbox_settled` against the real ink extent including descenders (L0082).
- Giant script words (more than ~400 px tall) exceed the box-fit cap (size 600): read `ref_script_metrics` off an outline overlay of the identified face (a-run box plus tail x-height and baseline) and let the uncapped two-run solve set them. Check the overlay by eye (L0077; `test_giant_script_two_run.py`).
- Plain ink must never land on your own script ink: `own_ink_collision`, `MAX_OWN_INK_OVERLAP = 0.22`.

### 6.7 Which face, when a face is contested

1. **The reviewer names another reel's font** ("make it like reel X") → do not run a blind face search. Read that reel's locked face config (family, weight, TTC index) and take its face **and its tracking/stretch model** exactly (from its `captions.plaintext.json` `ref_fit`). Fit only size and position to this reference's ink boxes (L0048, L0116). Example tracking model: Helvetica Neue Bold at −0.06 em.
2. **The letterforms are contested in the abstract** ("nothing like the reference") → run a face-**family** search: `refit.fit_part` across several real candidates, report every score. Dilation/tracking sweeps within one face cannot change glyph *shape* (o roundness, t tail, apostrophe) (L0028). Include the house plain face at negative tracking: a tight-tracked Helvetica-class reference is usually Helvetica-class, not a traced face at positive tracking (L0116).
3. **No candidate fits** → license, choose an open face that fits, or recreate for matching (6.5).

A pixel-fit winner the reviewer dislikes is still wrong; when they name a target reel, that reel is the target.

### 6.8 Letterform recreation craft (every way a traced face went wrong)

| Lesson | Defect | Rule |
|---|---|---|
| L0067 | Glyphs traced where the subject's head cut them set "D" and "O" as an open "n"; a bokeh light ate a "B" bowl so it set as "R" | Trace each glyph from an instance that is **whole** in the reference (no subject or bright background over it); box exactly one glyph run. After every trace render every used glyph large and **read** it; run the counter test (`test_built_face_counters`: closed letters keep their counters). Keep a re-trace only if it matches the reference ink better |
| L0068 | A correct re-trace was rejected because its glyph score was lower (0.983 → 0.933); the kept "B" had a collapsed bowl | A glyph score never vetoes reading. Render every used glyph large next to the reference ink and keep the version that reads as the letter |
| L0085 | 1–2 px ledges and notches on i/n/h stems: glyphs cropped tight to their ink, so the stem sat on the image border and the blur reflected it | Pad the glyph mask with ≥ 8 px zero margin on every side and offset the baseline by the pad. Proof: a 500 px render of `i n h l k` vs the previous face |
| L0090 | The trace kept the reference's compression jitter (notched stems, wavy legs); free negative tracking fused letters | Smooth each outline along its length (circular Gaussian ~1 source px), snap long near-vertical runs to a line, then simplify; refit with a glyph-gap check (every glyph its own connected component at fit size) |
| L0097 | Scale normalised to one line's tallest glyph oversized other lines; touching letters mis-segmented; an apostrophe dropped | Build ONE face from every caption state at ONE pixel scale (cap height measured once, from the line with the tallest ascender); segment glyphs as connected components and **keep** small marks (apostrophes, periods, asterisks); prove with IoU on frames that were not traced |
| L0098 | A face traced at a 50% threshold on one frame: strokes eroded to 10 px (reference 14), lumpy, edges too crisp (1.59 px vs reference 2.43) | Trace from a per-pixel **low temporal percentile (8th)** of the settled frames, normalised `(M − bed)/(peak − bed)`, cut at half contrast (0.5); place each glyph 1:1 on its reference glyph centre; choose softening by edge width / profile MAE against the reference. Pass: IoU ≥ 0.83 vs the reference's half-contrast mask on untraced frames; stroke px and median edge width within 0.1 of the reference |
| L0112 | A traced face reused on another reference showed stair-step edges and bites, spread with a forced 3 px gap + uniform tracking; per-line IoU 0.62/0.64 | Check a traced face at 300 px for stair-steps and bites before reuse. Choose a borrowed face by per-line IoU against THIS reference among **smooth** candidates (a Helvetica-class bold with measured stroke growth won at 0.770/0.768). Spacing from the reference's own per-glyph centres with a symmetric 2 px de-weld |

Tracer internals (`faceid.py`): `UPM = 1000`, the tallest traced glyph of a line sits at cap height `CAP = 700`, mask upscaled `TRACE_UP = 4` before contouring.

Tooling trap (L0049): a face-sweep helper derived output filenames from the first two words of the candidate name, so two different "Helvetica Neue …" candidates wrote the same file and the true score (0.833) was overwritten (0.639). Output names must be a collision-proof slug of the full name; assert the name → file mapping is injective before the sweep (`test_face_sweep_naming.py`).

### 6.9 Spacing and tracking

- Spacing comes from the reference's own per-glyph centres with a symmetric 2 px de-weld, never a uniform tracking value that forces gaps (L0112). Place each glyph on its reference centre (L0098).
- No free negative tracking that fuses letters; use the glyph-gap check (L0090).
- Copying another reel's face means copying its tracking model too (L0116).
- Script words: one connected run, slight negative tracking (L0055).

### 6.10 Footage-filled ink

When the reference's letters show a picture through the glyphs (colour varies inside a glyph; a fit below 0.6 is the tell):

- Measure the in-glyph low-frequency colour field and rebuild it with detail from your own footage; place the word by luma-edge alignment instead of a flat-ink fit; set the letterforms from the reference ink; gate per letter (L0089; `test_footage_filled_ink_not_flat.py`).
- Measure ink per letter and take the median of the letters that are **not** footage-filled (or the pale/solid component), never the whole-mask median. A whole-mask median once shipped a dark teal (69/118/111) where the reference read silver (145/145/143) (L0078).
- Fit footage-filled words with `faceid fit` on a **union** mask (solid ink OR the fill colour), read the overlay, store the fit with its source (L0082).
- When a footage-filled word's glyphs straddle light and dark (glyph luma 0.013–0.598), no bed reaches 3.0 per glyph: set that word's ink to the reference's measured median colour as a solid field (your detail kept at a fraction), then choose beds by worst glyph against that ink. Never a shadow, never a flipped ink (L0109).

### 6.11 Shadow, halo and outlines

Reviewers consistently reject black outlines, baked strokes and soft halos ("hazy" captions). The default is flat ink, crisp antialiasing, no halo. A reference that is flat white with a hard edge and no halo is reproduced that way.

Order of remedies when white ink fails on your bed:

1. **Fix the bed first.** Recast to a cleared window where the caption zone is darker or plainer so the word reads flat like the reference (L0058, L0096, L0106).
2. Only if no cleared bed exists: the **ruled directional shadow** on exactly the states the gate names (L0056). Parameters: radius 12, opacity 1.0, offset [4,5], grow 5; radius ≤ 20; never a symmetric glow (L0025, L0033). A grow of 5 lifted one word from 2.96 to 3.42.
3. The card says which remedy was used and reports the raw shadow-free figure beside the gated one (L0030, L0096, L0113).
4. Exception, framing wins: when framing, a third-party-mark exclusion and a caption bed conflict, keep the framing and the exclusion and give the state the ruled shadow (L0113).

Shadow is **per state**, not per cast: build the study with `shadow:false` on every state, run the gate, then add the shadow only to the states the gate names (L0056; `test_named_face_and_state_shadow.py::test_state_can_wear_no_shadow`). A cast-wide shadow turned navy words on bright sky into muddy blobs.

Shadow is applied **after** the entry device. Baking the shadow into the settled layer filled the alpha gap the word-splitter keys on, so a two-word caption arrived as one word. Every ink treatment (shadow, glow, outline) is applied after `devices.layer_for_frame`, per frame, on the composed layer (L0002; `test_shadow_is_taken_from_the_frame_the_device_drew`).

### 6.12 The readability gate

`UnreadableCaption` is a **stop**. `--allow-low-contrast` is never a lane decision; it is a CALL with the measured numbers, and the render report's `unreadable` list goes on the card verbatim (L0023). One build only rendered because someone passed the flag: 9 of 13 states were under 3:1, one at 1.09:1 on a white sky (L0025).

Gate definition (`captions_typeset.py`; L0025, L0030, L0109):

- Measure what the viewer sees: the bed **with** the caption shadow composited, ink pixels masked out (`ink_layer` + `shaded_bed`).
- Legibility = WCAG relative-luminance contrast ratio `(Lmax + 0.05)/(Lmin + 0.05)` of ink vs the **band 4–8 px outside the glyphs** on the composited frame. Floor **3.0** (`HALO_MIN_RATIO`). Always report the raw shadow-free contrast beside it.
- Per **glyph** on the decoded delivery: glyph = connected component of caption alpha; band 4–8 px outside the full alpha; every state's `worst_glyph ≥ 3.0` (L0109).
- Other kit gates: `MIN_CONTRAST_ABS = 25.0` luma, `MIN_CONTRAST_RATIO = 0.6`, `MAX_CLUTTER_RATIO = 2.0`, `MIN_CLUTTER_ABS = 25.0`.
- A stricter core-pixel gate from forensics work, useful for audits: within the mask core (alpha ≥ 192), linear-luminance contrast ratio PASS when C10 ≥ 3.0, C50 ≥ 4.5 and < 10% of core pixels are below 3:1; SEVERE when C50 < 3.0 or ≥ 50% of core pixels are below 3:1. Alpha-aware glyph variant: a pixel passes if luma ratio ≥ 1.5 or ΔE76 ≥ 20; a frame passes if ≤ 10% of core pixels fail.

History worth remembering: the first shadow-aware gate took the mean of a ring touching the ink on the composited bed. An opaque shadow is near-black whatever the footage does, so every state cleared by 1.3×–9×. It answered "is there a shadow", not "does this caption separate". **A gate must be shown to fail something real before it is trusted**, and its measure must vary across the states of a real build (L0030).

When to run it:

- **A grade change is a caption change.** After any grade or bed change, re-run `onetoone.captions_typeset.readability` on every frame of every caption state of the actual picture. A brighter grade once made a flat-white state fail on 58 of 77 frames (shadow-free minimum 1.40) and nobody re-ran the gate (L0025, L0033).
- **Probe before render.** `onetoone.bedprobe` runs one slot's candidate window through crop → LUT → cover → typeset layer → readability, so a reframe is chosen on numbers, not by re-rendering (L0066).
- **Every frame, not one.** A one-frame-per-state prescreen missed a per-frame dip to 2.52 on three frames (L0106).
- **Entry frames too.** Settled frames passed while blur-in/fade-in entry frames fell under 3.0. For any caption with an entry device, predict the entry frame's contrast on the candidate bed with the auditor's metric (ink mask from the approved decode, ring = dilate10 − dilate3, device opacity + blur) before casting; sample every in-point of a window (L0104).
- **After encode, again.** A pre-encode 3.33 decoded at 2.98 (encode drift up to 0.35). A state wears the ruled shadow if any flat-ink frame reads under **3.6** pre-encode (3.0 floor + 0.35 drift + margin), and the gate is re-run on the decoded file before audit (L0117).

Ghost/translucent words need a measured bed: mean RGB in the word box within ~30 of the reference's bed. A bed-ranking score is not a bed check (L0090).

Visibility gates are reference-relative: ours ≥ min(0.60, reference − 0.05) visible ink on the same glyphs; never a flat number the reference itself fails (L0068).

When the reference itself is illegible in places (for example difference-blend captions failing the floor on some states), a human may rule "build it 1:1, the illegibility is the look" for exactly those states. That waiver is per reel, per state, by explicit ruling. Never extend it yourself.

### 6.13 Occlusion: captions in front of or behind the subject

- A caption state can be `behind_subject` (a luma/person matte so the subject passes in front of the word), copied from the reference.
- **Captions clear, not buried.** A per-glyph burial the reference does not share is a hard defect (L0075).
- **Fix burial in the caption layer first.** Draw only the glyphs your subject hides (where the reference shows them) in front of the subject for the whole state; keep every other glyph behind. Recast or reframe a praised shot only when the reviewer names that shot (L0093).
- **A finding is a class, not a frame.** After fixing burial on the named shot, re-measure per-glyph visible ink (yours vs the reference) on every shot and every frame (L0069). Normalise yours against the reference when your own maximum never reaches the reference's ink; own-max normalisation hid letters buried on every frame (L0075).
- **Probe with the reference's ink mask.** Take each glyph's mask from the reference at its best frame, never from your rendered caption layer (which silently skips a glyph you bury on every frame) (L0083).
- Where a full-width caption band sits on a master that pins the head to the top edge, no static framing clears it: recast to a clip with headroom instead of another reframe round (L0083).
- **Never a word across the protagonist's face.** Frame recasts so no caption word overlaps the face (0 per-glyph fails, so the in-front rule never has to fire). Proof: face boxes × word boxes = 0 overlaps on the decoded build (L0103, L0115).
- Within one caption state no two parts may share the same text (the readability lookup is by text, so the last same-text part wins). Split repeats into sub-states `<line>.<n>` with the same in/out (L0066; `test_part_text_unique`).

### 6.14 Entry devices and caption lifecycle

- Entry and exit behaviour (blur-in, fade, scale drift, word-by-word reveal) is part of the 1:1 contract. Abrupt pop-ons and bland entries are named defects. Slow fades suit slow songs; fast songs get harder hits.
- **Measure, don't read prose.** `onetoone.measure_devices` aligns first, then measures, per frame: (1) residual `polarity × (L − grey_open/close(L, 71))` over the padded crop; (2) template = the settled frame's residual cut to `bbox_settled`; (3) geometry per frame by `matchTemplate` (normalised correlation) over a scale grid (coarse 0.60–1.36 in 0.04 steps, template blur 4.0, then fine with blur 2.0), giving scale s(n) and offset; (4) blur at that alignment over a sigma grid (isotropic, then y, then x), relative to the reference's own settled sharpness; (5) opacity by least squares; (6) word reveal per word for lines that have one. An unaligned fit reads a caption's slow scale ramp as blur. References' captions are rarely stationary; one grew from 0.90× to 1.00× across 30 frames.
- **Replay in order** (`onetoone.devices.layer_for_frame`): geometry (scale about the settled box's top-left, then drift) → blur (separate x/y sigmas, on **premultiplied** alpha to avoid dark fringes) → opacity. Word-by-word lines are split off your own layer at its widest interior alpha gaps, never re-typeset.
- Measure with the reference's **own** frame count. A tool hard-coded to another reel's length read every state after that frame as static (L0074).
- Low-confidence device fits lie. Any device-curve frame with fit < 0.7 or sigma > 3 is checked against the reference frame by eye before render; if the reference word is sharp, set that frame to settled values and note the override (L0084; `test_low_fit_blur_overridden.py`).
- Blur response is not monotonic: at sigma 2.0–2.5 a blurred script split into a thin component reading 1.25–1.39 while 1.5 and 3.0–3.5 passed. Tune blur-in with an offline per-glyph simulation (blur the settled layer, composite on the caption-free frame, run the per-glyph rule over a sigma sweep) and pick the passing sigma closest to the reference fit; disclose any frame left early (L0111).
- Kit constants: `SIGMA_GRID` 0–15 (0, 0.5, 1, 1.5, 2, 3, 4, 5, 6.5, 8, 10, 12, 15), `SCALE_BAND = (0.80, 1.30)`, `MIN_CONTAIN = 0.60`, `TOPHAT = 71`, `SETTLED_EPS = 1e-3`.
- Review tools: `onetoone.refstrip` tiles one part's bbox over the entry window of the reference with frame numbers, so the device can be read before fitting; `onetoone.devicesheet` puts reference and yours side by side per frame across the entry.
- Lifecycle phases to record per state: first detectable, readable blur, near crisp, first crisp, hold, exit (progressive blur window or hard removal), final-frame residual. QC gates key off lifecycle kind; a heavily blurred entry frame may legitimately have no thresholded bbox, so assert non-zero support/composite delta instead.

### 6.15 Caption timing

- Caption state frames are **zero-based** and come from the reference's own ink-per-frame curve. Prove it with ink pixel counts at the first frame and at each switch frame. A one-based placement left frame 0 empty and the previous state showing one frame too long (L0098).
- Lyric/voice captions sit on the voice and on the beat. Hold the line while it is sung; no random flashes; no abrupt stop at the end.
- Blank gaps between captions are part of the rhythm: check exact frame-set identity and the blank-run distribution against the reference.
- **Speech-recognition word timestamps are not a caption clock.** Whole-file transcription drifts up to ~1.3 s inside a 30 s chunk; single short slices have edge artefacts; word jitter is ±0.3–0.6 s. What works: transcribe the **built** audio in 12 s windows at a 3 s hop, discard words within 1.5 s of a window edge, take the median of the remaining votes (~3 per word), interpolate the rest, force monotonic order. Longer windows are worse (20 s left a word 1.0 s out). Judge per caption card against energy-based speech onsets (6 s sliding 90th-percentile reference): target median 0.00 s, p90 ≤ ~0.37 s; a card up to ~0.5 s early is correct when the previous line holds through a pause. For 1:1 reels the reference's ink curve is the clock; transcription is only for voice-led builds with no reference caption track. Run all transcription through `whisper_guard.py` (one process machine-wide; see [01_ARCHITECTURE.md](01_ARCHITECTURE.md)).

### 6.16 Softness and sharpness

"The text is soft" is one of the most frequent notes. Measure before acting.

- `onetoone.sharpness.edge_width_px`: ink = `min(R,G,B) ≥ 235` inside the ROI; width = `0.8 × C / Gpk` (C = ink luma minus local bed via 7×7 min, Gpk = 5×5-max Sobel magnitude), on the **decoded** frame at the cited frame vs the reference, the previous version and an approved reel. Floor **2.0 px** (`EDGE_WIDTH_MAX_PX`). A hard 1-px step reads 1.6; approved reels read 1.61–1.80; one reference's own encoded text read 2.45 (L0038).
- If every number says the ink is at the ideal step, do not touch letterforms, blur or tracking: report the numbers and check the picture **under** the caption (grade, exposure, bed), since the bed changes the perceived edge.
- "Soft" can also mean "wrong edge profile vs the reference": a traced face at 1.59 px against a reference at 2.43 was still called soft and wrong. Choose softening by edge width / profile MAE against the reference. "Too crisp" and "too soft" are both measured against the reference (L0098).
- Composite and measure on player-exact planes (`onetoone.yuvexact`), never swscale `rgb24`.

### 6.17 Caption read-back QA

The design behind the caption gates (details in [03_QA_SYSTEM.md](03_QA_SYSTEM.md)):

- **Read the delivered word.** OCR over isolated, upscaled caption plates in a 12-cell ensemble (cap heights 60/120/240/480 × padding 0.5/1.5/3.0), never full frames (full-frame OCR fails even on the reference) and never the OCR engine's own confidence (it anti-correlated with correctness). Calibration: rejected states scored ≤ 0.08, accepted ≥ 0.50 → threshold 0.25 (single measurement; replicate).
- **Parity battery on the delivered file**, per state, per frame, worst frame governs: W1 words (read-back equals the reference track); W2 contour (Dice ≥ 0.90 crisp / ≥ 0.60 blur AND one-way source→candidate residual p95 ≤ 3.0 px AND component + hole equality; Dice alone passed a 0.9030 amputated word with an 85 px residual); W3 ink (per-frame Michelson contrast ≥ 0.80× the reference's ring Michelson AND ≥ 25 luma separation, both sides of any cut inside a hold); W4 rhythm (frame-set identity + blank-run distribution); W5 presence (every contracted state exists in the delivered file); W6 font metrics (cap height ±1.5%, stroke width, tracking).
- Every check reports its sample size (a probe once compared zero pixels on 40 of 42 states and passed).
- **Mutation proof**: re-run parity on deliberately corrupted copies (words swapped / font scaled 3% / one hold frame dropped). All must fail, or the gate is not trusted.
- Contour defect vs codec artefact: 1–2 px yuv420p chroma fringes and ordinary antialias changes are not defects. A real contour defect persists at native scale and alters a meaningful structure (counter closure, missing dot/apostrophe, clipped swash, fused letters, stepped edge, wrong terminal, non-uniform scaling, stable collision).
- Ink distance (core RGB, 8-bit): ≤ 6.0 pass, > 8.0 fail, between = ambiguous. Timing: exact, zero tolerance on the rendered-ink and blank frame sets. Coverage includes the final state, not only midpoints.

### 6.18 Look sheets

Scores and counter tests passed broken glyphs three times; only the side-by-side **look sheet** showed them (L0067, L0068). Build every review sheet by decoding each input separately and indexing frames in Python, labelling each tile with its frame number (`fNNN`). Never trust a one-liner ffmpeg `select` after `hstack` through shell escaping; it silently paired the wrong frames (L0100). `onetoone.looksheet` builds the pre-render version (reference frame beside your candidate frame with the typeset caption) so the look can be approved on stills before a full render.

---

## 7. Casting

Casting means choosing, for every slot, a master, an in-point and a crop. The footage is your own, of people who agreed to appear. Most casting rules exist because a clip made the protagonist look bad, showed someone else, showed nothing, or repeated itself.

### 7.1 Authorized footage, masters only

- Sources must sit under the project's declared `authorized_footage_root`. Reference downloads, research pools and other creators' reels are study material, never sources.
- Render from **masters**, never proxies (e.g. 640x360 study copies). `onetoone.looksheet.resolve_master(stem)` finds a master; a clip with no master is unavailable, not a "fix it in the grade" problem.
- Resolution defects (stretched, squished, low-res clips) are a named rejection. Check SAR and rotation (section 9.2).

### 7.2 Identity pools as code

Who may appear in an identity slot is **data enforced by code**, not prose (L0052):

- Every cast slot declares `"identity": "operator"` (the protagonist; person A) or `"identity": "none"` (no person, or a person-free window). An undeclared slot fails.
- An identity slot may only use a stem in `onetoone/identity_pool.json` `settled_pool` (proven masters of person A, possibly from several identity sets for different looks; unruled clips excluded).
- No slot, identity or not, may use a `blacklist_full` stem (matched by any identifier: stem, file name, content hash) or an in-point inside a `blacklist_spans` window.
- Other keys: `not_operator` (clips where the visible person is someone else, never in a person-bearing window), `operator_confirmed`, `unruled`, `sources`.
- `python3 -m onetoone.identity <cast.json>` must print OK; `onetoone.preflight --cast` runs it and blocks. The lists change only on a human ruling by clip number, in the same change as the test (`test_identity_pool.py`).

Entry shapes:

```json
{"blacklist_full":  [{"stem": "CLIP_0061", "kind": "FULL_BAN", "match_keys": ["<sha256>", "<file name>"], "ruling": "..."}],
 "blacklist_spans": [{"stem": "CLIP_0074", "banned_windows_s": [[0.0, 8.0]], "reason": "..."}],
 "unruled":         [{"stem": "CLIP_0003", "why": "..."}]}
```

The pool ships empty; fill it from your own rulings. Why it became code: when the pool and blacklist lived in prose and receipts, a caster filtered on a library `subject` tag that was wrong on hundreds of clips, a loader read the wrong JSON key, and two bans carried no match keys. Library subject tags are unreliable; always face-verify at zoom. After any new identity ruling, re-check every delivered set, not only new builds.

### 7.3 Clip grades are casting law

A grader UI (the review Deck's `/grader`) writes every tap to an append-only ledger `_receipts/clip-grades/grades.jsonl` under the factory root (latest line per stem per field wins; a null never erases). `onetoone.grades` reads it live (L0065):

| Grade | Meaning |
|---|---|
| NEVER | full ban everywhere (a NEVER piece of a segment-graded clip bans only that window) |
| HERO | person A at their best; settled for identity slots and preferred wherever the reference shows a person |
| ME | it is person A; settled for identity slots |
| BROLL | never on an identity slot; fine behind a caption where the reference shows nobody |
| NOTME | not person A; never on an identity slot |

(`GRADE_VALUES = {HERO, BROLL, NEVER}`, `IDENT_VALUES = {ME, NOTME}`.) The effective pool = `identity_pool.json` + grades. Never build a grader that stores grades only in browser localStorage; they get lost.

Segment grades: lines may carry `t0`/`t1` on a 5 s grid (clips ≤ 7 s are one piece; otherwise n = round(duration/5) equal pieces). An identity in-point must sit in a HERO piece; `castscan` scans identity slots only inside HERO pieces.

Details bought with failures:

- **Check the whole played window**, not just the in-point: a HERO in-point played three frames into a NEVER segment (L0090).
- **Grades change under you.** Never resume a cast on an earlier identity OK: re-run `onetoone.identity` + preflight against the live grades before **every** render and recast any slot the new grades reject (L0087, L0088). Tests that check pool rules monkeypatch `grades.GRADES_PATH` to an empty file so live grading cannot flip them.
- **Ungraded footage never goes on an identity slot**, even when it fits. The card names each moment (clip + what it shows + which slot) that waits on a grade; ask for the grade with a CALL rather than casting around it (L0072, L0110).
- **A match-cut pair grade is not an identity grade** (L0110).

### 7.4 Identity slots come from the reference

- Where the reference shows a person, the slot is an identity slot. Run `python3 -m onetoone.refpeople <row dir>` at intake (face detection → `brain/refpeople.json`; a face counts when its height ≥ 0.06 of the frame in ≥ 1/3 of the slot's frames: `MIN_FACE_H = 0.06`, `MIN_FRACTION = 1/3`). `castscan --identity auto` reads it and orders by grades (L0065).
- **refpeople is face-only.** A silhouette, a back or a small figure is a person too. Before declaring any slot "none", eye-check the reference frames of that window **and** your clip (L0110).
- A person-free plate is a last resort, never a default fill (L0058), and is often rejected even where the reference is empty (L0103). Streaks and abstract plates count as empty (L0061).
- `castscan` scores the bed under the captions, so an empty frame "reads best". Never let a bed score pick an empty plate for a person slot (L0065).

### 7.5 Taste law: what makes a clip good

- **Main character, intent, action.** Person A visibly doing something with intent, the camera placed on purpose. No "whole lot of nothing", no aimless camera wander, no excessive motion. A recast slot whose reference shot carries a person must show person A with a readable face and real action (L0042).
- **Hook**: open on person A with a visible face. No blank openers; no dead or passive closers. End shots need motivation and action.
- **Flattering beats glamorous**: prefer flattering angles (partial, profile, back-facing, architectural). Never reshape a body; select a different take instead. Avoid unflattering expressions (yawns, slack moments).
- **Per-project taste bans** (wardrobe, specific clips, whole categories) are the human's call, recorded per clip in the blacklist and grades, and checked by code.
- When the reviewer says a take looks bad, recast to a different take they graded HERO (L0115).
- Check every candidate's sharpness at the shipping in-point (one candidate opened slightly soft) and its blacklist status before shortlisting (L0042).
- Darker, active clips usually read cleaner than bright, passive ones.

### 7.6 Freshness

- **Clip freshness is a defect class.** Check every cast against a usage histogram (exact-stem match across the registry and prior casts; `python3 code/reel-production-tools/reuse_map.py --master <stem>`) and disclose reuse on the card. Prefer least-used masters (L0015). Keep a human-verified shortlist of least-used masters per identity set for when a reviewer says the clips are overused.
- **No repeated master inside a reel**. The one allowed exception is a quick whip back-and-forth between the same two shots. If the reference intro is one locked-off room across several cuts, one master across those slots is correct; disclose it.
- **Freshness is judged by setup, not stem.** Two different files with the same set, props, lighting and angle read as a repeat. Before render, put the cast's first frames side by side and recast any shot whose setup repeats a neighbour, unless a jump cut was requested (L0071).
- **Alternates of an approved reel**: reject any stem within 3 clip numbers of the approved slot's stem (same prefix; adjacent clip numbers are usually the same setup); after render, per-slot mean absolute difference vs the approved decode must be ≥ 10 on every non-scarce slot (one "new" slot differed by only 4.7) (L0101). Hooks must differ by camera setup across the whole batch (L0104).
- **Size the scarce slot first**: if one slot needs a rare bed (plain, darkish, person A in frame), gate-search it across the whole pool before casting the others, then use disclosed in-point repeats of clean stems (different in-point and reframe) rather than a weaker stem (L0107).

### 7.7 Privacy screen: what must not be on screen

What appears in frame matters as much as what you write. Screen for third-party and private information:

- A HERO grade is about identity, not about what else is in frame. Clips from public venues have shipped readable third-party text and readable screens because the eye check was done on thumbnails (L0073).
- Before audit, OCR every 2nd–4th decoded frame of the build and view every public-venue shot at native resolution in crops. Any legible sign tying the people on screen to a location or affiliation, any credential, any readable screen is recast or cropped out (use `castscan` for the replacement), then re-rendered. List removals on the card (L0073, L0080).
- Strangers in focus, a second person's hand, vehicle plates and readable third-party notices are rejects. Venues ruled out are not cast on any slot (L0084).
- **Third-party logos and brand marks**: a recast is not done until its frames are screened at native resolution for marks; blur with the kit's redaction patch and prove with OCR (0 brand words in the delivered window) (L0103). Clothing wordmarks on the subject are a disclose-only class unless the project rules otherwise.
- **Screen at ≥ 960x540.** A 480x270 contact sheet passed a second person's hand, a branded garment and street signage; the 960x540 re-screen rejected 25 more windows. Screen every pick (graded frame at the slot's mid time) before any render; a reject is logged at stem level in `eye_rejects.json` with the reason (L0091).
- **Screen the whole cast with the crop applied**, not only new picks: five picks (legs only, shoulder close-up, headless silhouette, head cropped by the reframe) passed three rounds because each round re-screened only new picks. Reject any pick where the head is not in frame (the back of the head is fine) (L0105).
- Any automated scan honours `eye_rejects.json` (stem and frame keys) and a human count of exactly 1; a cut-off face can pass a face detector as "one face" (L0104).
- Waivers (for example background bystanders as disclose-only) are explicit, written and scoped to one reel.

### 7.8 Finding footage

- **Never declare footage absent from a tag search alone.** Untagged clips exist in every library; also search by shoot date / clip-number series and read the contact boards (L0072).
- Tag precision can be poor: one library measured 39.3% precision on an "action peak" tag. Prefer a human-verified overlay (ACTIVE / PASSIVE / DEAD / NOT_PROTAGONIST) over automatic tags; PASSIVE/DEAD/NOT_PROTAGONIST clips are out of protagonist and action slots.
- **castscan with a shortlist.** An all-library castscan for one slot ran past a 30-minute limit and left 4.0 GB of frame dumps. Run `castscan --stems <shortlist read off a contact sheet>` and delete the `--out` dump as soon as the pick is made (L0088).
- **The caption bed is a casting constraint.** Where the reference's bed under the ink is pale and plain, yours must be too. Rank candidates with `python3 -m onetoone.castscan <brain> <cast.json> <reference.mp4> <SLOT> [--stems a,b,c] [--times 1,3,6] [--top 20]`, which scores the bed under every ink box against the reference's own bed (mean luma distance + texture). Probe finalists through the house chain with caption boxes overlaid (L0061).
- **Known supply gaps are adapted, not faked.** Missing footage = adapt the grammar and disclose each substitution.

### 7.9 Casting procedure

1. Intake: `refpeople`, reference faces and inks measured, cut grid locked (section 4).
2. Per slot: role, person yes/no (eye-checked), caption-bed constraint, entry-frame contrast need.
3. Shortlist ≥ 3 candidates per slot from HERO/settled clips; contact sheet → `castscan --stems`.
4. Probe each finalist through the house chain with caption boxes overlaid; watch at normal speed (a candidate not watched at normal speed is a hard reject).
5. Score (Q-score): 0.25 semantic role + 0.25 visible action/beat peak + 0.15 composition/crop readability + 0.10 motion direction + 0.10 lighting/palette + 0.08 native transition readiness + 0.07 narrative escalation, minus penalties: duplicate source = hard reject; static lookalike in an action slot −0.30; no readable action −0.25; crop hides subject −0.20; synthetic warp = hard reject; climax weaker than the preceding block = hard reject.
6. Screen the whole cast at ≥ 960x540 with the crop applied: face, head in frame, marks, signage, screens, strangers, hands.
7. Side-by-side first frames: no repeated setup; per-shot B−R vs the opening shots; freshness.
8. `python3 -m onetoone.identity <cast.json>` → OK, against live grades, whole played windows.
9. `python3 -m onetoone.preflight --row NN --cast <cast.json>` → `PREFLIGHT: OK`, immediately before render.

---

## 8. Devices

### 8.1 Doctrine

A device is made from real footage and reads as a glitch in time, not a filter. A recurring sound cue = repeat the same real-shot micro-edit. Hooks benefit from devices: match cuts and glitch interleaves in the first seconds keep viewers watching. A useful standing format: every edit gets at least one glitch beat on its strongest musical hit. Keep working glitches as built unless something is actually broken; make them clean.

### 8.2 Presence interleave (the "glitch" opener)

Anatomy of the strongest version, in three escalating instances:

1. **Subliminal**: 1-frame ABABAB × 12 between two windows of **one locked-off take** (A = empty frame, B = subject present). The background is pixel-registered because both states come from the same tripod take. Neighbour mean absolute difference during the flicker measured 7.9–8.7, against 47–95 for a real cut.
2. **Readable**: 3–4-frame runs (A3 B4 × 9) from another locked take; the slower cadence reads as a repeated match cut. The discriminator there was colour temperature (warm empty vs cool occupied), not luma.
3. **Rhythmic**: four 3-frame holds of the same locked setup at four timestamps = four postures, on the beat grid.

Why it works: same-take harvesting gives perfect registration, so the brain reads a presence change as a time glitch, not an edit. Cadence hierarchy: 1 frame (subliminal) → 3–4-frame runs (readable) → single flashes (rhythmic). Every cut lands on the music grid and every cut is a caption change.

Build: `build_plan.json` blocks with `"kind": "interleave"`, `pattern: [[state, frames], ...]`, `states: {A: {clip, in_s}, B: {clip, in_s}}`. Supply: long locked-off takes where the subject enters and leaves. Keep a shelf (`GLITCH_SHELF.json`) of glitch-capable takes with beats capped at 6 per take so no single take is overused, face-verified.

Traps:

- Median-frame presence detection lies when the subject stays in frame for most of the take. TRUE-PRESENCE rule: the take is empty for ≥ 35% of its length **and** the longest empty stretch is ≥ 3 s.
- Numeric windows always need eyeball rounds; face-verify at zoom.
- Ink is bed-relative: fix dead caption states by re-seating picture windows, never by adding ink.
- For a looping device, last frame = first frame.

### 8.3 Match cuts

- A match cut is a cut where the composition (shape, position, motion) carries across two different moments. A first attempt that simply cut between two unrelated shots was not one; study the term before building.
- Day→night pairs from the same camera position are excellent raw material: they add feeling instead of random clips stitched together.
- **Adding match cuts to a 1:1 reel (L0094)**: keep the reference cut grid and caption timing; recast only the two sides of chosen cuts with a mined pair whose **both** sides pass the identity rules (`grades.identity_slot_reason`, effective settled pool, blacklist) at their in-points; crop zoom 1.0 on both sides so the setup stays registered; prefer reference blocks that already repeat one setup; prove each match cut on the decoded file with an edge-map correlation far above the reel's ordinary cuts.
- Mining pairs across a whole library is memory-heavy; guard the miner's physical footprint, not RSS (see [01_ARCHITECTURE.md](01_ARCHITECTURE.md), failure modes).

### 8.4 Day/night split, walk-through, clone

- **Split day|night** works only if the subject passes through the boundary: walk in from the other side so they phase from day into night with no visible line.
- **Clone** (two of the same person in one frame from one locked take): matte against the take's **median** background, never a two-frame difference (which masks both silhouettes). For video clones use full-resolution person segmentation; a smudged head is a fail.
- **Twin pairs**: same camera, different time of day/light, held longer.
- Rejected as template-looking: rewind + VHS, stop-motion/frame-rate strobe, circular time-sweep wipes.

### 8.5 Flashes and pulses

- One-frame flash pulses (inversion or exposure flashes) are a strong grammar. Missing reference flashes are a defect; copy the reference by default.
- Flashes must be synced and organised. No accidental black dips; a flash with lag is a defect (one fix removed 12 frozen frames).
- The effect sits exactly on its sound cue.
- Pulse QC: exactly one frame; pairs exactly two indices apart; the next frame clean; captions on pulse frames pass contrast.
- Restraint: stacked filters, slashes and zooms together are "too much going on". Fast songs get hard hits and cuts on the beat with restrained flashes.

### 8.6 Whips and direction

- Horizontal whips are a strong format; keep a whips version and add a flashes version as an alternate.
- Horizontal, not vertical, unless the reference does otherwise.
- A back-and-forth whip of the same two shots is the one allowed clip repeat when quick.

### 8.7 Never devices

- Synthetic warps to fake missing action: hard reject. Fake push-ins / manufactured movement: forbidden. Unintended speed change, reverse, freeze, loop or frame interpolation: technical QC fail.
- Slow motion used as decoration.

---

## 9. Framing, crop and camera moves

### 9.1 16:9 masters at the reference raster

- The house format is **16:9 masters**, matching the reference's raster, rate and frame count exactly: for example 1916x1078 at `24000/1001` (`render.FPS_STR`, `TIMESCALE = 24000`) or the reference's own rational rate (e.g. `2997/125`). Geometry, rate and frame count are part of technical QC.
- A vertical deliverable is a separate derivative with its own framing pass, never a stretched or letterboxed master.
- Terminate renders with `-frames:v <expected count>`, never `-shortest`; re-probe the decoded frame count after render. Source-code assertions are not evidence.

### 9.2 Crop law and geometry

- Probe the master, not the library index: read the signed rotation (0 / 90 / −180) with ffprobe before any crop. Rotate upright **before** scaling. Crop law: `scale=W:H:force_original_aspect_ratio=increase,crop=W:H`. A portrait source flattened into landscape without honouring SAR/rotation came out squashed 3.16×.
- SAR 1:1 at every segment; probe the final SAR/DAR.
- Even-integer crop x/y on subsampled masters, or convert to 4:4:4 first (section 5.4).
- Per-slot reframing (`onetoone.framing`): a cast slot may carry `{"zoom": 1.25, "cx": 0.5, "cy": 0.45, "drift": {"dx": 1.5, "dy": 0.0}, "push": 0.0004}` (zoom 1.0 = full frame; `cx`/`cy` crop centre as a fraction of the master; `drift` = crop-window travel in master px per frame; `push` = zoom change per frame). Applied before LUT/cover.
- On portrait masters the kit clamps `cx` and refuses a push; the render reports `crop_effective`. Framing drift can be silently clipped at the frame edge (e.g. `cy 0.38` at zoom 1.15 pinned y = 0); verify moves and match the reference's **direction** (a pan is not a rise).

### 9.3 Headroom

Every crop on a person shot carries a framing constraint: the whole head with **≥ 6% headroom** (and the whole body when the reference shows the whole body), checked by a face/head-top sweep of **every** decoded frame. Head top = face box top + 0.45 × face height; CUT if the margin < 0, TOUCH if < 0.02. A caption-bed crop search without the subject/headroom constraint picks crops that cut heads (L0113). Too-low framing on the first and last shots is a common miss: centre the subject properly.

### 9.4 Crop direction: audit proposals are hypotheses

An audit proposed lowering `cy` to move words off a chin. Lowering `cy` moves the crop window **up**, so the picture (and the face) moves **down** onto the caption; per-glyph legibility fell to 2.34/2.15 (L0114).

- **cy down = picture down; cx down = picture right.**
- Work out the direction and the predicted face-to-caption gap before rendering; prove it on decoded bytes (face box vs caption alpha).
- To widen a face-to-caption gap without cutting the head, lower the zoom.

### 9.5 Camera moves

- A crop-based "push" never zooms (ffmpeg `crop` evaluates w/h once; measured +0.02%). Push-ins and keyframed moves use `zoompan` on an **isolated ProRes plate** (`framing._zoompan_for`, keys `[[n, zoom, cx, cy], ...]`, 16:9 masters only) (L0010; `test_push_and_keys_use_zoompan_not_a_fixed_size_crop`).
- Measure camera motion with feature tracking (`onetoone.motion`: Shi-Tomasi corners + pyramidal Lucas-Kanade + RANSAC partial affine → scale, translation at frame centre, rotation, fit error), never global phase correlation, which reported a +2.6% push-in as a −15.9 px pan (L0010).
- Render moving crops **first** to an isolated ProRes plate (about 0.9 s of footage per 0.8 GB), then grade it as a static clip, under a memory/disk watchdog (free memory < 25% or system disk < 6 GB → kill that ffmpeg). `zoompan` inside the full grade chain nearly exhausted memory and disk.
- Subject hold: `motion.track_roi` on the master → per-frame keys (zoom = 1/scale) cancels a camera pull-back (a head that shrank to 53% was held within 0.4%).

---

## 10. Audio

### 10.1 The soundtrack is the master clock

- Use a track you have rights to: the platform's audio library (attach it when posting) or a licensed track. For a local study render, use the same licensed track the post will carry so the cut grid is built against it.
- The cuts match the soundtrack's beat grid; the reference shows you where.

### 10.2 Mux by stream copy

Concatenate the picture segments, then mux the audio by stream copy, never by re-encode:

```
ffmpeg -i picture_silent.mp4 -i soundtrack.m4a -map 0:v:0 -map 1:a:0 \
       -c:v copy -c:a copy -movflags +faststart picture.mp4
```

Gotchas, each one real:

- **No `-shortest`.** A soundtrack 8.011 s long against video 8.050 s trimmed the picture to 191 of 193 frames.
- **Proof of a bit-exact audio stream**: packet md5 identical; per-packet `framemd5` identical; stream parameters and extradata identical; decoded PCM md5 identical. Audio identity is never one hash; compare every packet. Audio identity is also not musical sync; check sync separately.
- **The video is never bit-reproducible; the audio is.** Re-rendering an identical selection gave 0 of 204 frames bit-identical (mean MAE 1.233, worst 1.92) while audio stayed bit-exact. Never claim encode reproducibility; state provenance against that measured floor.
- **AAC profile on viewing derivatives.** `codec_name=aac` is not enough: a file can be valid H.264/yuv420p and still fail in an inline player because its audio is HE-AAC. For any viewing derivative request `-profile:a aac_low` and verify ffprobe reports `profile=LC`. A broad-compatibility review proxy: H.264 baseline/main, yuv420p, AAC-LC.
- **Encoder tags and colour tags.** Re-encoded files carry encoder tags (`Lavf`/`Lavc`) and odd dimensions; in forensics these mark a file as a re-encode, not a camera original, so record provenance from the file, not from its name. On your own deliverables set the colour tags explicitly (bt709 primaries/transfer/matrix, tv range) so players and measuring tools agree.
- **Check stream durations, not the container.** `ffprobe -select_streams v:0 -show_entries stream=duration` vs `a:0`. Chained crossfades once put the picture 2.8 s ahead of the voice over 157 s; the gap was exactly the sum of the dissolves. Related traps: an `xfade` whose duration hits zero silently truncates the chain (exit code 0); pad segments with `tpad=stop_mode=clone`; never stack two dissolves; verify sync errors have no trend.

### 10.3 Song choice and energy

- Pick the strongest section of the track; a dead or noisy section kills a reel.
- More energy than the reference is welcome when requested: faster, harder cuts on the beat.

### 10.4 Detecting unwanted room sound under speech

When a build carries speech plus room sound: general audio-event classifiers scored audience laughter at only 0.04–0.09 (useless as a gate). Detect by the "murmur band" (sustained ≥ 0.4 s stretches between −32 dB and −12 dB relative to a 6 s sliding 90th-percentile speech reference), confirm by re-transcribing that stretch alone (speech recognition hallucinates tags on windows under ~2 s; re-check with 2.5–3.5 s windows), and duck to the room floor (~−34 dB) rather than by a fixed amount. Never denoise a reaction under speech. Classify first and ask which sounds are wanted.

---

## 11. Order of craft operations

1. Freeze the reference (identity, frame namespace, rule card, provenance map).
2. Feasibility: 1:1 or CALL.
3. Lock the cut grid from decoded frames; caption states zero-based from the ink curve.
4. Identify faces (license/open face/recreate for matching), measure inks and entry devices.
5. `refpeople`; cast from the identity pool and live grades; screen; probe beds.
6. House look on every shot; reframe with headroom; moving crops on isolated plates.
7. Typeset → device replay → per-state shadow only where the gate requires it.
8. Readability gate on every frame of every state, entry frames included; re-gate after any grade change.
9. Render with `-frames:v N`, decoded concat, audio by stream copy.
10. Decoded-bytes checks, then one independent audit (see [03_QA_SYSTEM.md](03_QA_SYSTEM.md)).

---

## 12. Thresholds

Every number used above, in one place. "Kit" constants live in `code/reel-production-tools/onetoone/`.

### 12.1 Captions and typography

| Quantity | Value | Source |
|---|---|---|
| Readability floor: WCAG ratio ink vs band 4–8 px outside glyphs, composited bed, per glyph, per decoded frame | ≥ 3.0 (`HALO_MIN_RATIO`) | L0030, L0109 |
| Pre-encode trigger for the ruled shadow | any flat-ink frame < 3.6 | L0117 |
| Measured encode drift of the gate | up to 0.35 | L0117 |
| Ruled directional shadow | radius 12, opacity 1.0, offset [4,5], grow 5; radius ≤ 20; never symmetric | L0025, L0033 |
| Minimum ink/bed separation | 25 luma (`MIN_CONTRAST_ABS`); ratio 0.6 (`MIN_CONTRAST_RATIO`) | captions_typeset.py |
| Clutter limits | ratio 2.0 (`MAX_CLUTTER_RATIO`), abs 25 (`MIN_CLUTTER_ABS`) | captions_typeset.py |
| Core-pixel contrast (audit) | PASS C10 ≥ 3.0, C50 ≥ 4.5, < 10% core px below 3:1; SEVERE C50 < 3.0 or ≥ 50% below 3:1; core alpha ≥ 192 | forensics |
| Alpha-aware glyph gate (audit) | pixel passes at luma ratio ≥ 1.5 or ΔE76 ≥ 20; frame passes at ≤ 10% core px failing | forensics |
| All-white ink override | `caption_ink [245,245,250]` | L0061 |
| Fit trust threshold | typesetter 0.6 (`REF_FIT_MIN_SCORE`); refit 0.45 (`MIN_SCORE`) | captions_typeset.py, refit.py |
| refit ink tolerance / search pad | 70 RGB distance (`INK_TOL`) / 60 px (`PAD`) | refit.py |
| Rendered width vs reference ink box | 90–110%, else refit or escalate | L0078 |
| Plain-on-script ink overlap | ≤ 0.22 (`MAX_OWN_INK_OVERLAP`) | captions_typeset.py |
| Script squeeze / stretch / overflow | 0.90 / 1.15 / 1.08 | captions_typeset.py |
| Giant script word | > ~400 px tall → metrics two-run solve (box-fit cap size 600) | L0077 |
| Device frame needing eye check | fit < 0.7 or sigma > 3 | L0084 |
| Device measurement | sigma grid 0–15; scale band 0.80–1.30; coarse scale grid 0.60–1.36 step 0.04; template blur 4.0 coarse / 2.0 fine; min contain 0.60; top-hat 71 px; settled eps 1e-3 | measure_devices.py, devices.py |
| Visible-ink gate | ours ≥ min(0.60, reference − 0.05) | L0068 |
| Ghost-word bed | mean RGB in word box within ~30 of the reference bed | L0090 |
| Caption edge width (10–90) | ≤ 2.0 px (`EDGE_WIDTH_MAX_PX`); ink = min(R,G,B) ≥ 235; hard 1-px step reads 1.6; approved 1.61–1.80; one reference 2.45 | L0038 |
| Recreated face: edge width and stroke | within 0.1 of the reference | L0098 |
| Recreated face IoU on untraced frames | ≥ 0.83 | L0098 |
| Trace settings | 8th temporal percentile; half contrast 0.5; pad ≥ 8 px; outline Gaussian ~1 source px; UPM 1000, cap 700, upscale 4 | L0098, L0085, L0090, faceid.py |
| Trace review sizes | 300 px stair-step check; 500 px `i n h l k` proof | L0112, L0085 |
| Spacing de-weld | symmetric 2 px | L0112 |
| Example tracking model | Helvetica Neue Bold at −0.06 em | L0116 |
| Borrowed-face per-line IoU (example) | smooth face 0.770/0.768 vs stair-stepped traced face 0.62/0.64 | L0112 |
| Holdout font proof | decisive margin (false positive was 0.0129); real holdout Dice 0.699–0.921 | caption engine |
| Speech timing | 12 s windows, 3 s hop, drop words within 1.5 s of edges, median of ~3 votes; jitter ±0.3–0.6 s; whole-file drift ≤ ~1.3 s; per-card median 0.00 s, p90 ≤ ~0.37 s; ≤ 0.5 s early acceptable on a held line | section 6.15 |
| OCR read-back | 12-cell ensemble (cap 60/120/240/480 × pad 0.5/1.5/3.0); threshold 0.25 (rejected ≤ 0.08, accepted ≥ 0.50) | section 6.17 |
| Contour parity | Dice ≥ 0.90 crisp / ≥ 0.60 blur; residual p95 ≤ 3.0 px both directions; exact component + hole equality; codec rescue band ≤ 2 px around the reference boundary | section 6.17 |
| Ink parity | Michelson ≥ 0.80× reference ring and ≥ 25 luma; core RGB distance ≤ 6.0 pass, > 8.0 fail | section 6.17 |
| Font metrics | cap height ±1.5% | section 6.17 |
| Mutation proof | swapped words / font +3% / one hold frame dropped → all must fail | section 6.17 |

### 12.2 Grade and picture

| Quantity | Value | Source |
|---|---|---|
| House look | camera Rec.709 LUT via `house_vf`: in_range=full, in_color_matrix=bt709, gbrpf32le, tetrahedral, gbrp16le, colorspace bt709 range=tv, fsb dither, yuv420p | L0037; housechain.py |
| "Same as approved reels" proof | same master frame within < 1.5 mean abs (house pipe reproduced to 0.03) | L0037 |
| Bare-chain error (example) | R −4.2 / B +3.3; R/B 1.96 vs 2.26, b* 6.5 vs 9.4, L 10.9 vs 13.1 | L0037 |
| tv-range plate fed as full | 6 levels wrong | section 5.2 |
| swscale rgb24 decode error | ~2 levels dark | L0037, L0038 |
| Chroma sanity | ≤ 0.40 gain in magenta/green-dominant share per shot (`CHROMA_JUMP_MAX`); broken shot 0.934 | L0041 |
| Pull tolerance | min(6.0, max(1, 0.25 × target)) (`PULL_TOL`, `PULL_TOL_REL`) | L0004 |
| Pull gamma floor / pass-2 floor / pass-2 trigger | 0.45 / 0.62 / > target + 5.0 | L0005 |
| Pull shoulder | 0.94 (255 → ~240) | L0003 |
| Highlight pull | identity below knee 0.20; exponent cap 14 | L0006 |
| Chroma follow | 0.4 (auditor 0.5–0.7) | L0007 |
| Luma ramp | tolerance 2.5 codes; pass-2 1.5; max 40 per frame | render.py |
| Sky banding (publish gate) | ≥ ~25 distinct luma levels per sky patch | L0008 |
| Clipping bar | reference 0.00% at ≥ 250 | L0003 |
| Range observed on passing builds | Y < 16 ≤ ~0.05%; Y > 235 ≤ ~0.18% | audits |
| Legal range | 8-bit 16–235 (10-bit 64–940) on the decoded file; a very-high-quality H.264 review encode needed pre-encode Y clipped to 30..221 (chroma 16..240; tighter chroma breaks saturated primaries) | forensics |
| Composite levels identity (10-bit) | p05/p50/p95 shift ≤ 10 code values; dynamic-range ratio 0.98–1.02 | forensics |
| Bounded per-shot colour match | q10/q50/q90 error ≤ 0.05; saturation error ≤ 0.06; highlight coverage ≤ 5 percentage points | forensics |
| Pastel-pink / magenta per block | equal to reference (0.0) on the pinned frame; 0 magenta frames | L0099 |
| Adjacent-block chroma swing | within ~2 of the reference's swing | L0099 |
| Warmth consistency | per-shot mean B−R vs the opening shots; a cool swing is a recast | L0058, L0060 |
| Reference-match engine blends | tone α 0.75; per-level α {p1 0.85, p10 0.75, p50 0.70, p90 0.45, p99 0.35}; cast α 0.70; sat α 0.65; cast clamp 0.34; tint max 0.06; tone clamp {p1 0.060, p10 0.160, p50 0.220, p90 0.120, p99 0.050}; min step 0.004 | grade.py |
| Reference-match engine guards | skin hue 5–50°, skin hue shift ≤ 18°, skin sat ≤ 0.65 and rise ≤ 0.22 (guard active when skin ≥ 2% of frame); specular V 0.90, shadow V 0.10; clip rise ≤ 0.020, crush rise ≤ 0.060 of midtones; curve slope ≥ 0.12 inside p10..p90; segment slope ≤ 2.2; stepped gradient when a code gap > 2, allowed rise 0.02 above source, always allowed up to 0.04; strength ladder 1.0 → 0.25 | grade.py |
| S-Log3 reference points (10-bit) | black ≈ 95, 18% grey ≈ 420, 90% white ≈ 598 | section 5.10 |
| Headroom | whole head, ≥ 6%; head top = face box + 0.45 × face height; CUT < 0, TOUCH < 0.02 | L0113 |
| Moving-crop plate watchdog | free memory < 25% or system disk < 6 GB → kill | section 9.5 |
| Push-in measured by a fixed crop | +0.02% (i.e. no zoom) | L0010 |

### 12.3 Casting and devices

| Quantity | Value | Source |
|---|---|---|
| refpeople | face height ≥ 0.06 of frame in ≥ 1/3 of frames | refpeople.py |
| Grade segments | 5 s grid; clips ≤ 7 s = one piece; n = round(duration/5) | grades |
| Eye-screen resolution | ≥ 960x540 (480x270 = shortlist only) | L0091 |
| Privacy OCR sampling | every 2nd–4th decoded frame | L0073 |
| Shortlist per slot | ≥ 3 candidates | section 7.9 |
| Q-score weights | 0.25 role, 0.25 action/beat, 0.15 crop, 0.10 motion, 0.10 lighting, 0.08 transition, 0.07 narrative; −0.30 static lookalike, −0.25 no action, −0.20 crop hides subject; hard rejects: duplicate source, synthetic warp, weaker climax | section 7.9 |
| Alternate stem distance | reject within 3 clip numbers of the approved slot (same prefix) | L0101, L0104 |
| Alternate footage difference | per-slot mean abs diff vs approved decode ≥ 10 | L0101 |
| castscan wall | use `--stems` shortlists (an all-library scan exceeded 30 min and 4.0 GB of dumps) | L0088 |
| Presence-interleave registration | neighbour MAD 7.9–8.7 vs 47–95 for a real cut | section 8.2 |
| TRUE-PRESENCE | empty ≥ 35% of the take and longest empty ≥ 3 s | section 8.2 |
| Glitch beats per take | capped at 6 | section 8.2 |
| Cadence hierarchy | 1 f subliminal → 3–4 f readable → single flashes rhythmic | section 8.2 |
| Pulses | exactly one frame; pairs two indices apart; next frame clean | section 8.5 |
| Tag precision warning | an "action peak" tag measured 39.3% precise | section 7.8 |

### 12.4 Audio, cuts, delivery

| Quantity | Value | Source |
|---|---|---|
| Audio mux | stream copy; packet md5, per-packet framemd5, extradata and decoded PCM md5 identical to the source track | section 10.2 |
| `-shortest` | never (8.011 s audio vs 8.050 s video → 191/193 frames); use `-frames:v N` | section 10.2 |
| Viewing-derivative audio | AAC-LC (`-profile:a aac_low`, ffprobe `profile=LC`), never HE-AAC | section 10.2 |
| Encode reproducibility floor | 0/204 frames bit-identical, mean MAE 1.233, worst 1.92 | section 10.2 |
| Cut check | diff(c) > diff(c+1) at every reference cut; exact cut-grid frame count | L0076 |
| Clean-cut evidence | cut diffs 10.3–208 vs intra-shot ≤ 5.9 | section 4.3 |
| Unsafe segment length through stream-copy concat | ≤ 2 frames | L0053, L0076 |
| Reference cut placement (grammar-adapt) | within 2 frames of reference onsets; exact for reference-locked | forensics |
| Web deliverable size | ≤ 10,000,000 bytes (`WEB_CAP`) | L0117 |
| Encode quality | crf 20–21 for web deliverables (kit default 20; crf 18 overshot the cap) | L0117 |
| Delivery raster | reference raster and rational rate, e.g. 1916x1078 @ 24000/1001; SAR 1:1 | render.py |
| Preflight OK validity | 6 h (`GATE_MAX_AGE_S`) | render.py |
| Murmur band | ≥ 0.4 s between −32 and −12 dB vs 6 s p90 reference; confirm on 2.5–3.5 s windows; duck to ~−34 dB | section 10.4 |

Machine, disk and concurrency limits (one ffmpeg, one speech-recognition process, disk floors, footprint guards) are in [01_ARCHITECTURE.md](01_ARCHITECTURE.md).

---

## 13. Superseded approaches: do not follow

Older text in any fork of this system may still say these. They are dead:

| Old approach | Replaced by |
|---|---|
| Grade every block to its own reference | House look; per-block only on a named shot (L0014 narrowed, L0018, L0102) |
| Hand curve + contrast 1.06 + saturation 1.22 + per-shot exposure "natural grade" | House look (L0018) |
| Luma pull/ramp always on | None in house mode (L0001 narrowed) |
| Luma-only highlight compression as the general fix | Superseded for house-graded reels (L0007, L0018) |
| Declared creative light states copied from the reference | Not in house mode unless requested by name (L0024) |
| Bare `lut3d` chain | `housechain.house_vf` (L0037) |
| "When a reference ink fails the gate, ship white and disclose" | Neither bypass nor swap: escalate with both measurements, unless "all white" was ruled (L0054 → L0056, L0057, L0061) |
| Cast-level shadow on every caption | Per-state shadow only where the gate requires it (L0056) |
| A per-word envelope fit (independent X/Y per word) | Uniform scale per state (caption engine rule 4.1) |
| "Captions may use our own words" | Words 1:1 |
| Encoding caption text to slip past a content filter | Plain text; escalate a block (section 6.2) |
| Capped fix rounds / multiple audits per build | One audit, then fix every finding with proof (L0081 → L0092) |
| Speech-recognition timestamps as the caption clock for 1:1 reels | The reference's ink-per-frame curve (section 6.15) |
