# 06 — Reference Forensics: breaking down a reference reel

This document is the method for taking a short-form reference reel apart, frame by frame and
layer by layer, so that it can be studied, rebuilt one-to-one with your own footage, or adapted
at the level of grammar. Run it **before** you choose a single clip of footage.

It is the forensic front half of the pipeline. What happens after the breakdown (casting,
grading, typesetting, rendering) is in [02_CRAFT_PLAYBOOK.md](02_CRAFT_PLAYBOOK.md); the gates
that judge the result are in [03_QA_SYSTEM.md](03_QA_SYSTEM.md); every lesson cited here as
`L00NN` is written out in [04_LESSONS.md](04_LESSONS.md); module details are in
[05_CODE_GUIDE.md](05_CODE_GUIDE.md).

Placeholders used throughout:

| Placeholder | Meaning |
|---|---|
| `<REPO>` | the checkout of this repository |
| `<WORKDRIVE>` | the large working volume where media, frames and renders live |
| `<REFS>` | where locked reference media and their frame dumps live (e.g. `<WORKDRIVE>/refs`) |
| `<RENDERS>` | render outputs (e.g. `<WORKDRIVE>/renders`) |
| `<ROW>` | one production job directory (a "row": one reference → one reel) |

---

## 0. Rights and scope (read first)

Reference forensics is a study technique. Use it responsibly:

- **Downloading references.** Respect the terms of service of the platform you take a reference
  from. Prefer the platform's own save/download features or media you have been given. Keep
  reference media private, in `<REFS>`, and never redistribute it. Never persist signed or
  expiring media URLs in logs or JSON.
- **Remakes.** A one-to-one remake is for style study and personal edits. If you intend to
  publish something that closely recreates another creator's reel (its exact clock, words,
  typography and structure), ask that creator for permission first. Reference-grammar adaptation
  (borrowing rhythm and structure, not the specific creative expression) is the safer default
  for anything public.
- **Fonts.** Identifying a reference's typeface from pixels, and the letterform-recreation
  tooling in this repo (`faceid trace`), exist for **matching and analysis**. A traced
  reconstruction of a commercial typeface is a derivative of that design. For anything you
  publish, license the original face, or choose an open-licensed font (e.g. from the
  OFL-licensed catalogues) that passes the same fit gates.
- **Music.** The reference's audio is analysed for timing (beat map, onsets, word timing). The
  track you publish with must be one you have rights to — the platform's own audio library or a
  licensed track.
- **The reference is never a render source.** No frame of the reference, and no frame of any
  research download, may end up in a render. The reference is the least eligible clip in the
  system: it carries another creator's world, burned-in captions and identity. Make this a
  mechanical gate (renders whose sources fall outside the declared authorized footage root fail
  at the CLI), not a promise.

---

## 1. Ten things to remember

1. Lock the exact source first: hash, rational frame rate, decoded frame count, streams. A
   repost or a transcode is a different reference.
2. A tool "success" is transport, not analysis. An analyser that received no pixels has seen
   nothing.
3. Evidence states advance in order:
   `SOURCE_LOCKED → DECODE_PROVEN → PIXELS_SEEN → AUDIO_HEARD → FULL_WATCH`.
   Only `FULL_WATCH` (normal speed, with audio, to the endpoint) supports claims about rhythm,
   beat feel, smoothness or whether the ending lands.
4. The cut detector proposes; an all-frames board and a normal-speed watch decide. Record
   **picture states**, not just shots.
5. Matching caption words and frame ranges says nothing about typography. Face identity,
   geometry, ink, blend and lifecycle are separate proofs.
6. Identical audio (same packets, same PCM) proves audio identity, not edit-to-music sync.
7. Technical integrity, reference-structure parity, visual parity and human creative approval
   are four separate verdicts. None implies the next.
8. When a human reviewer says it looks wrong, revoke the visual pass immediately. Do not defend
   it with hashes.
9. Everything is append-only: rejected candidates become negative fixtures; approved ones become
   immutable baselines.
10. Local review is the ceiling of autonomous work. Uploading, replacing, sharing and publishing
    each need a separate explicit go.

---

## 2. Before the steps: what kind of job is this?

### 2.1 Modes

| Internal mode | Human-facing name | Contract |
|---|---|---|
| `reference-locked` | exact-reference adaptation (1:1) | Preserve the exact clock, picture blocks, caption/effect timing and transition lifecycle. Missing literal scenes stay **declared gaps**. |
| `original-montage` | reference-grammar adaptation | Copy the reference's rule system (role progression, rhythm, typography family, energy arc, cue logic, ending) using your own footage. "Original" describes only the footage assembly. |
| `multi_reference_explicit` | explicit multi-reference adaptation | Different layers come from different references; every layer names its source. |

Translation from a casual request: "make one like this", "same aesthetic" → grammar adaptation;
"recreate it", "same cadence", "the font changed halfway" → reference-locked. If the request is
ambiguous, stop and ask (a `mode-ambiguity` call, §2.4) with the concrete consequence of each
choice. Never silently switch modes in either direction.

There are no speed exemptions. "Quick demo" and "doesn't have to be perfect" describe polish, not
permission to skip the forensic pass.

### 2.2 Layer lineage

The creative idea of a reference-led reel belongs to the reference. Captions, quotes, typography
states, transitions and hooks in a reference-led reel are presumed to come from the reference;
an unknown origin stays `UNRESOLVED` and is never credited to the editor or the agent.

Every creative layer records `origin`, `source_reference` and an operation:

| Operation | Meaning |
|---|---|
| `LOCK` | preserved exactly from the named reference |
| `ADAPT` | function preserved, implementation changed |
| `SUBSTITUTE` | reference footage replaced by authorized footage in the same role |
| `DROP` | omitted, by explicit decision only |
| `USER_NEW` | genuinely new, supplied or approved by the person you work for |

Required layers: spoken script, lyric/audio words, in-video text, public post caption,
typography, shot-role sequence, cut clock, effects/transitions, audio/cue map, colour/composition
family, ending/loop.

### 2.3 Keep these classifications separate

1. **Reference identity** — the exact source file: bytes, hash, decode, provenance confidence.
2. **Reference grammar** — how it works: timeline driver (spoken quote, lyric, music, action,
   atmosphere); in-video text (none, progressive quote, lyric words, title cards, subtitles);
   typography; cut grammar (phrase cuts, beat cuts, hard spatial cuts, flashes, held smears);
   shot-role sequence; continuity world; audio/cue rule; ending (payoff, hard hold, reset, loop).
3. **Adaptation contract** — which mode, which layers are `LOCK`/`ADAPT`/`SUBSTITUTE`.
4. **Content theme** — what your output communicates. Not the editing grammar.
5. **Continuity world** — `single_world` (one subject/location/wardrobe/light family) or
   `multi_world` (different places unified by quote, lyric or music).
6. **Authority state** — `DRAFT`, `REVIEW`, `APPROVED`, `PUBLISHED`. A technical PASS does not
   make an output a positive exemplar.

Optional format-family labels help routing but never replace the reference card. Examples:
an *observed-access* glimpse into one coherent real world; an *architectural reset loop*
(movement and geometry with a replayable ending); *travel compression* (departure → movement →
arrival → experience → payoff). "Phone B-roll" is a capture label, not a format.

### 2.4 Calls (when a human decision is genuinely needed)

Do not interrupt for routine decisions you can make safely. Stop and issue a structured call only
when the decision needs a preference, new footage, a mode choice, or acceptance of a material
compromise:

```text
CALL_REQUIRED
call_id: <stable-id>
severity: BLOCKER | DECISION | WARNING
project_id: <id>
revision: <revision>
reference_block: <block / timecode / frame range>
issue_type: missing-role | bad-quality | excessive-motion | unsafe-crop | wrong-world |
            wrong-action | duplicate | colour | typography | audio | mode-ambiguity |
            appearance-policy

WHAT THE REFERENCE REQUIRES:   <plain English>
WHAT I FOUND:                  <exact source path, hash, interval, observed problem>
WHY IT DOES NOT MATCH:         <role/action/motion/composition/quality/continuity>
EVIDENCE:                      <boards, proxies, probes>
OPTIONS:                       A. ...  B. ...  C. new footage / another reference / loosen mode
MY RECOMMENDATION:             <one option and why>
REPLY WITH:                    A, B, C, or <exact information needed>
```

Never issue a vague call ("the clips aren't perfect"). Name the block, the frames, the source,
the failure, the alternatives and their consequences.

---

## 3. The thirteen steps

### Step 1 — Acquire and lock the reference

1. **Get the exact file**, within the platform's terms (§0). If video and audio arrive as
   separate representations, keep both originals; a `-c copy` mux is a playback convenience
   only. A screen capture (`captureStream()`/`MediaRecorder`) is a decoded proxy, not the
   reference. Never use a repost.
2. **Lock identity:**

   ```bash
   shasum -a 256 REF.mp4
   ffprobe -v error -show_entries \
     stream=codec_name,width,height,sample_aspect_ratio,display_aspect_ratio,r_frame_rate,avg_frame_rate,time_base,start_pts,duration_ts,nb_frames:stream_tags=rotate \
     -of json REF.mp4
   ffprobe -v error -count_frames -select_streams v:0 \
     -show_entries stream=nb_read_frames -of csv=p=0 REF.mp4
   ffmpeg -v error -i REF.mp4 -f null -        # an empty error log is required
   ```

   Record the **rational** frame rate exactly (`24000/1001`, `30/1`, `2997/125`). Compute seconds
   as `frame / rate`. Never round a close rational rate to a familiar FPS, and never compute frame
   count as decimal fps × duration. Record audio duration separately. The presentation clock is
   `PTS(f) = start_pts + step·f`, with FPS = `time_base_den / step`.
3. **Freeze a zero-based frame namespace.** Decode every frame once into an immutable directory
   (`ffmpeg -i REF.mp4 -start_number 0 <REFS>/<id>/frames/ref-%03d.png`), hash each frame, and
   bind every later board and measurement to those hashes. A one-frame namespace shift (1-based
   numbering, a renumbered directory) moves every cut and caption state while still looking
   plausible (`L0098`). Every tool in this repo uses zero-based decoded order
   (`select=eq(n\,K)` / `-start_number 0`).
4. **Identity has three layers:** container bytes; compressed packet hashes; decoded essence
   (video frame hashes + PCM). A re-download that differs in bytes but matches every decoded
   frame is the same picture evidence, but does not inherit audio or container identity.
5. The lock must be strong enough to detect a repost, a transcode, a different frame count, a
   different clock or different audio.

In `reelctl`, `reelctl new <ID> --reference <FILE> --footage <AUTHORIZED_ROOT> --mode
reference-locked` followed by `reelctl reference analyze` writes this lock (full PTS ledger,
per-frame decoded-pixel ledger, audio packet ledger, PCM hash, toolchain identity) — see
`code/reelctl/src/reelctl/reference.py`.

### Step 2 — Watch it five ways

1. Full speed **with audio**.
2. Full speed **muted** (rhythm of the picture alone).
3. **Frame by frame** at every change.
4. As a **gap-free numbered contact sheet** of all frames.
5. As **boundary boards** for captions, effects and cuts.

Only (1) earns `FULL_WATCH`. If you are a model that cannot play video, say exactly which tiers
you completed (§Step 11). For short reels, numbered all-frame sheets covering `0..N-1` (for
example 24 frame pairs per page) are standard; assert by machine that every sheet's table expands
to exactly `range(N)`.

For long sources, an overview proxy is fine for navigation
(`ffmpeg -i IN -vf 'scale=-2:240,fps=8' -c:v libx264 -preset veryfast -crf 30 -c:a aac -b:a 48k
-movflags +faststart REVIEW.mp4`), but **for frame-exact work never reduce FPS**: scale only, then
prove decoded frame count, FPS, first and last frame match.

### Step 3 — The cut list (picture track)

1. **Generate candidates** — navigation only:

   ```bash
   ffprobe -f lavfi -i "movie=REF.mp4,select=gt(scene\,0.05)" \
     -show_entries frame=pts_time:frame_tags=lavfi.scene_score
   ```

   `0.05` is sensitive; `0.08–0.12` suits reels (e.g. `0.12` found 13 cuts on a ~216-frame
   montage); `0.25` suits talking-head ads. Add adjacent-frame difference as a second signal.
   `reelctl`'s `detect_hard_cuts` does the same internally.
2. **Inspect the PRE/POST pair of every candidate** at native resolution. Background geometry
   outranks the score: dark-to-dark cuts can correlate highly, and camera motion can out-score
   real cuts. For streak runs (several short shots in a row), view frames `n-1, n, n+1` around
   every boundary before locking (`L0054`). For any proof render, search the nominal boundary
   ±4 frames by luma mean-absolute-difference; an ambiguous boundary stays `INCOMPLETE`.
3. **Classify every boundary:**

   | Class | Tell |
   |---|---|
   | distinct-shot hard cut | new geometry, new light |
   | same-camera temporal jump | subject jumps against an unchanged background (homography with many inliers) |
   | crop-only reframe | whole-image scale/translate of the same moment |
   | composite-state switch | a layer appears/disappears |
   | effect-state boundary | an effect starts/ends |
   | ordinary motion / cadence | not a boundary |

   Alternating low/high/low differences usually mean a ~12 fps source doubled into 23.976, not
   cuts.
4. **Record picture states, not just shots:** occupied/empty, pose jumps, held smears, insert
   pulses, brighter reframes. Collapsing them makes the song feel mistimed even when the audio is
   byte-identical (a reference with 28 picture blocks rebuilt as 16 felt off at 0 ms audio lag).
5. **Output:**
   - a shot-frame vector that sums to `N` (e.g. `[16,14,15,16,14,14,13,14,13,12,13,14,13,35]`
     for a 216-frame, 30 fps reference);
   - half-open intervals `[start, end)` in JSON covering every frame exactly once;
   - `picture_boundaries_after` (all visible changes) and `hard_cuts_after` (a strict subset).
6. **Endpoint class:** `live_motion_cutoff`, `held_or_frozen`, `fade_to_black`, `black_frame`,
   `end_card`, `exact_loop` (requires decoded-hash equality of last and first frame), or
   `loop_aware_bookend`. Report audio end in ms relative to video end.
7. **Cadence statistics:** count; median/mean/min/max shot length; sub-0.5 s bursts.
8. **Camera motion per shot.** Global phase correlation cannot see zoom (it reports a push-in as
   a meaningless pan). Use `python3 -m onetoone.motion <video> <frame_a> <frame_b>`
   (Shi-Tomasi features + pyramidal Lucas-Kanade + RANSAC partial affine), which reports scale,
   translation at frame centre, rotation and fit error, so a push-in reads as a push-in.

In `reelctl`: `reelctl blueprint lock <ID> --boundaries ... --hard-cuts ... --observations
obs.json --all-frames-reviewed`. The detector output alone can never lock a blueprint.

### Step 4 — The beat map (audio and timing)

1. **Onsets, two views:** full-band and low-frequency/percussive. In vocal music, full-band
   peaks are often syllables. Recipe: mono PCM at 44.1/48 kHz, Hann STFT, positive log-spectral
   flux in low/mid/high bands, local-median normalisation, peak picking with a minimum distance
   and prominence; `onset_frame = round(onset_seconds × rational_fps)`.

   `code/reel-production-tools/beatmap.py` is a dependency-light implementation (ffmpeg +
   numpy, no librosa): `SR=22050`, `HOP=256` (~11.6 ms), `NFFT=1024`, onset threshold `0.12`,
   minimum onset gap `0.055 s`, tempo search `55–200 BPM` with half/double disambiguation, grid
   phase and per-bar downbeats:

   ```bash
   python3 <REPO>/code/reel-production-tools/beatmap.py REF.wav [--start S] [--dur D] --json beats.json
   ```
2. **Delta per picture event:** `nearest_onset_delta_ms = 1000 × (frame/rate − onset)`. One frame
   is 41.7 ms at 23.976 fps, 33.3 ms at 30 fps.
3. **Fit a grid** `phase + k·step`; report step, BPM and RMS residual; keep half/double/triplet
   ambiguity open until listening resolves it.
4. **Speech-driven edits:** measure cut-to-word alignment at 50 ms and 100 ms windows. Word
   timestamps come from Whisper **only through the guard**:

   ```bash
   python3 <REPO>/code/reel-production-tools/whisper_guard.py REF.wav --out words.json [--lang en] [--model medium]
   ```

   The guard allows models `tiny|base|small|medium` only (`large*` is refused), refuses to start
   under 6 GiB free memory or if any other ASR/separation process is running, and holds one
   machine-wide exclusive lock so a second caller blocks. Parallel large ASR models can exhaust
   RAM and panic smaller machines. Vocal isolation (to hear lyrics under a mix) goes through
   `vocal_guard.py` (same lock, htdemucs only, segments capped at 40 s).
5. **Audio identity is not sync.** Identity check order: elementary-stream hash → decoded PCM
   hash and sample count → packet ledger (PTS/DTS/duration/size/flags/data hash, e.g.
   `ffprobe -show_packets -show_data_hash sha256`). "Beat-synced" needs onset evidence plus a
   listened watch.
6. Optional visual aid: a spectral-flux / tempogram image (e.g. `songsee REF.wav --viz
   flux,tempogram,loudness`). A spectrogram still does not prove sync.

**Rebuild tolerances** (for later): every creative cut lands within **±1 frame** of its onset
(±2 is the explicit maximum; `reelctl`'s `BEAT_TOLERANCE_FRAMES = 2`), and the **internal action
peak** of each slot lands within **±2 frames** of its target. Score action with the median 2-D
optical-flow vector subtracted per frame pair, so a camera pan does not read as action. Retime
with `new_source_start = old_source_start + (current_global_peak − target_global_frame)`.

### Step 5 — Roles and grammar

Per segment, record:

- **visible verb** (turn, type, lift, sip, enter, cross, climb, strike, reveal) and its phase;
- subject scale, depth, movement, palette, title field (where text sits);
- **editorial role**: hook, entry/departure, movement, environment, action, detail,
  access/social, escalation, reset, payoff, finale, loop return.

A filename is not a role; write what the shot does for the viewer.

- **Continuity axis:** single-world, journey-compression, or instructional (an aesthetic reel is
  non-instructional — no tutorial structure or explainer overlays).
- **Slot classes:** physical action, transition lifecycle, establishing hold.
- **Phase structure for short slots:** anticipation → event → consequence → stable endpoint. For a
  16-frame slot: frames 0–2, 3–9, 10–13, 14–15.
- **Peak placement in a substitute:** `source_start = P − (T − S)/F` (P = source peak time,
  T = target frame, S = slot start frame, F = fps).
- **Gate whole worlds before ranking shots.** Do not optimise isolated exciting action nouns
  before proving the reference's world grammar.
- **Where does the reference show a person?** Measure it rather than assume:

  ```bash
  cd <REPO>/code/reel-production-tools
  python3 -m onetoone.refpeople <ROW> [--ref ref/ref.mp4] [--step 4]
  ```

  This samples every 4th reference frame with the macOS Vision face detector and writes
  `<ROW>/brain/refpeople.json`. A slot whose window has a face on at least **1/3** of sampled
  frames, largest face **≥ 6 %** of frame height, is an **identity slot** and must be cast from
  footage of the intended subject, never an empty bed (`L0065`). The detector is face-only:
  a silhouette, back view or small figure is a person too, so eye-check any window before
  declaring it person-free (`L0110`).

Per-block table shape for the brain:

```text
block_id | reference_frames/timecode | role | visible_action | action_phase |
subject/setting | composition/crop | camera_motion | subject_motion | light/colour |
text/effect/audio state | candidate_source_hash | source_interval | evidence_path |
quality_flags | decision | confidence
```

### Step 6 — Captions: semantics, lifecycle, placement

1. **Semantics.** The exact raster string: case, punctuation, deliberate spellings
   (e.g. a dropped final letter marked with an apostrophe), progressive builds as separate intervals, every no-text frame counted. Keep
   `caption_state_start_frames` separate from `word_onset_frames`, and the audible transcript
   separate from the visible text. A reference may hold an uncaptioned opening phrase, show a
   partial word, blink a caption for one frame, or show a different hero word from the audible
   one: **lock what is on screen.** Caption changes are independent of cuts.
2. **State frames come from the reference's own ink curve.** Count ink pixels per frame inside
   the caption region; state starts/ends are where that curve switches. Prove with ink > 0 at
   the first frame and at each switch frame (`L0098`).
3. **Lifecycle.** First detectable → readable blur → near crisp → first crisp → hold → exit.
   Blur categories `extreme → heavy → medium → light/near-sharp → crisp`; never invent Gaussian
   radii by eye — measure them (Step 9). A crisp caption over a smeared picture means the effect
   sits **below** the captions in the compositing order.
4. **Sub-readable onset.** Use at least two caption-free frames (motion-warped) as background,
   score the residual against a glyph template from a crisp frame versus shifted controls. This
   proves overlay onset only.
5. **Hierarchy and placement.** Role per state (`connector`, `running`, `display`, `hero`),
   alpha bbox, optical-centre jitter, baseline. Measure the box against the **real ink extent
   including descenders** (`g j p q y`): a box that stops above a descender is a measurement
   defect that later shrinks the word (`L0082`).
6. **Lock caption geometry before choosing footage.** Text boxes, negative space, contrast and
   occlusion are footage-feasibility gates; a grade cannot rescue a composition that cannot carry
   the caption.
7. **Reading text off frames.** On macOS, Vision OCR (`VNRecognizeTextRequest`, accurate level,
   language correction off) is a lead, then an eyeball. Run OCR on **isolated plates or tight,
   upscaled region crops, never full composite frames** (full-frame OCR returns garbage even on
   clean references). Vision's per-read confidence is **not** a gate input (it was measured
   anti-correlated with correctness); the signal is ensemble agreement.

Caption-learning tools that support this step (`code/caption-learning/`):

| Tool | What it answers |
|---|---|
| `wordtruth.py` | For each caption state: our declared text vs the reference's own read-back at the state's mid-frame (`MATCH / MISMATCH / UNRESOLVED_TEXT`). Unresolved states are excluded from pass/fail, never counted as matches. |
| `readback.py` | 12-cell OCR ensemble per word: cap-height normalisations `60/120/240/480 px` × pad factors `0.5/1.5/3.0`. A word with intact anatomy reads the same at every scale; a fused or eroded one disagrees with itself. Agreement threshold `0.25`; a region read's modal answer must reach ≥ 1/3 of cells. |
| `bandpresence.py` | When a contract has words and timing but no boxes: derive the caption band from pixels that go extreme (luma ≥ 245 light ink / ≤ 15 dark ink) on caption frames but never on blank frames. Components < 120 px are noise; a band < 400 px or covering > 60 % of the frame is `UNMEASURABLE`. Presence rate ≥ 0.80 on caption frames, ≤ 0.25 on blank frames; delivered ink density must reach ≥ 0.25 of the reference's. |

Every gate here returns `PASS | FAIL | UNMEASURABLE` with values, sample size and evidence
paths. **A gate that measured zero pixels returns `UNMEASURABLE`, never PASS.**

### Step 7 — Font identification

Font identity is established from the reference's own pixels, **never assumed and never set by
eye** (`L0016`, `L0055`).

1. **Collect sharp exemplar words** at native resolution: the earliest fully drawn frame before
   the next row appears for stacked text. Choose instances that are **whole** — no subject, no
   bright background light crossing a letter (`L0067`).
2. **Make a clean ink crop** of one caption line:

   ```bash
   cd <REPO>/code/reel-production-tools
   python3 -m onetoone.faceid ink-crop --frame <REFS>/<id>/frames/ref-151.png \
     --box x0,y0,x1,y1 --ink R,G,B -o work/face/line1.png
   ```
3. **Get candidate names.** Upload the crop to a font-identification service yourself
   (respecting its terms; `faceid identify` only prints this instruction). Then look for each
   candidate locally:
   `python3 -m onetoone.faceid find "<name>"` searches system and user font folders and earlier
   reference intakes.
4. **Render candidates.** Set a `g a t e i k m` specimen and the actual words in each candidate
   face. For TTC/OTC collections, read family/style/PostScript name from the **loaded face
   index** — a path alone can silently load Regular while the manifest says Bold.
5. **Score several words together** for one global size and tracking. Compare contours and
   component topology (counters, dots, apostrophes, loops), not edge templates alone (edge fits
   lock onto architecture behind the text).
   - **Component fingerprint:** per-glyph x, y, w, h and area left to right. A cumulative x
     drift within 1–2 px means tracking is zero.
   - Overlay colours: cyan = observed, magenta = candidate, white = overlap.
   - Tracking model: `x_i = font.getlength(text[:i]) + i·tracking` (or between HarfBuzz-shaped
     clusters).
6. **One uniform scale plus translation only.** Never independently resize X and Y to fill a
   detected envelope; when auditing, report `scale_x/scale_y` per tier. A chromatic support box
   is not a glyph bbox.
7. **Place by baseline.** Use Pillow `anchor="ls"` (left-baseline). Centring on `textbbox` ignores
   `bbox[0]/bbox[1]` and has shifted words 100–156 px; alpha-centring puts x-height words
   ~15 px too high. Recover the baseline across x-height, ascender and descender words.
8. **Script / calligraphic text.** A script word is **one connected run** fitted with size and
   slight negative tracking — never a capital plus a letter-spaced tail (which reads as separate
   letters) (`L0055`). Split ornate capitals and punctuation from a decisively matched lowercase
   run; do not let correlation hide missed swashes. Giant script words (taller than ~400 px)
   exceed box-fit caps: read script metrics off an outline overlay of the identified face
   (`L0077`).
9. **Diagnostic masks are not production mattes.** A thresholded or connected-component mask is a
   silhouette for analysis until a native-size topology board proves every loop, dot and swash
   with no background rectangles or ROI clipping. Never select components with a horizontal row
   cutoff (a row cutoff once amputated a descender loop while builder and diagnostic "agreed").
   HSV gate used for warm-white ink: `V ≥ 190, S ≤ 105` (wider `V ≥ 175, S ≤ 140`); no
   morphology before topology is accepted.
10. **Prove it non-circularly.** Fit on some words, test on others:
    - `reelctl/captions/fontproof.py`: fit one uniform transform on train words, freeze it, score
      unseen holdout words; at least **2** independent counting metrics must prefer the same
      candidate by a margin of at least **0.05**. A single blended score, or per-word
      independent x/y sweeps, is circular and has produced "winners" by a 0.013 margin.
    - `caption-learning/anatomy.py` triple gate (ANDed): **Dice ≥ 0.90**; one-way
      source→candidate boundary residual p95 (catches a missing stroke that barely moves Dice);
      **exact equality** of connected-component count and hole count (catches fused letters and
      filled counters). Masks are tight-cropped, one isotropic scale to match height, centroid
      translation.
    - `onetoone.faceid fit --frame ... --box ... --ink ... --text ... --face <font> -o
      overlay.jpg` renders our ink in red over the reference and prints the `ref_fit` score to
      store with its source.
11. **Verdict per face:** `EXACT_FONT_BYTES_VERIFIED` (you hold the font file and it matches),
    `EXACT_SOURCE_CONTOUR_VERIFIED`, `SYNTHETIC_GRAMMAR_COMPLETION`, or `EXACT_FACE_UNRESOLVED`.
    For a one-to-one build, "nearest available" is a typography reject, not a pass.
12. **When the original face is not available.** Options, in order:
    1. license the identified face;
    2. choose an open-licensed face by **per-line IoU against this reference's ink** among smooth
       candidates (a tight-tracked neo-grotesque reference is often matched best by a common
       bold grotesque at slightly negative tracking — test it before anything exotic, `L0116`);
    3. for analysis and matching, build a traced reconstruction from the reference ink
       (`faceid trace`, below) — and license the original before publishing anything set in it.
13. **Reusing a previously locked face.** When told "use the face from job X", read that job's
    locked face **and its tracking/stretch model** and refit only size and position to this
    reference's ink (`L0048`, `L0116`). A blind cross-face search is the rule only when the
    letterforms themselves are contested without a named source (`L0028`); dilation or tracking
    sweeps within one face cannot change glyph shape.

#### Letterform recreation (`faceid trace`) — analysis tooling

`python3 -m onetoone.faceid trace --frame <frame> --box <box> --ink R,G,B --text "LINE" --out
<WORKDRIVE>/fonts/RefFace.ttf --name "RefFace" [--need "<all caption text>"] [--replace]` traces
the glyphs of one clean line into a font (plus a `.glyphs.json` sidecar and a reference-vs-built
sheet). Rules for a trustworthy trace:

- Trace from a per-pixel **low temporal percentile (8th)** of the settled frames, normalised
  `(M − bed)/(peak − bed)`, cut at **half contrast (0.5)**; a single-frame 50 % threshold erodes
  strokes (`L0098`).
- Build one face from every caption state at **one pixel scale** (cap height measured once, from
  the line with the tallest ascender); segment glyphs as connected components and keep small
  marks (apostrophes, periods) (`L0097`).
- **Pad every glyph mask by ≥ 8 px** of zero margin before contouring; a crop whose ink touches
  the border produces ledges and notches on stems (`L0085`; the shipped `faceid.trace` still has
  the tight crop — see known bug 11.2 #16 in [05_CODE_GUIDE.md](05_CODE_GUIDE.md)).
- After every trace, render every needed glyph large (300–500 px) and **read** it; closed letters
  must keep their counters (`test_built_face_counters`); a glyph score never vetoes reading
  (`L0067`, `L0068`). Check for stair-step edges and bites before reusing a traced face
  (`L0112`).
- Prove on frames that were **not** traced: IoU ≥ 0.83 vs the reference half-contrast mask;
  stroke width and median edge width equal to the reference within 0.1 px (`L0098`).

### Step 8 — Ink, colour treatment and blend

Treat changing caption colours as evidence of a compositing operator, not a palette.

1. Build state pairs, every-frame lifecycle boards and mask-alignment boards.
2. **Compare models on the same samples:** flat fill; spatial/temporal ramp; masked copy of
   footage (footage-filled glyphs); source-over; Difference `(1−a)B + a|B−S|`; white invert
   `1−B`; inverted-luma LUT. In sRGB and linear. Report RMSE, MAE, parameter count and BIC.
3. **Signatures:** negative ink/underlay correlation and channel-order reversal → complementary,
   footage-reactive ink (Difference/inversion); nonuniform temporal change; per-state flat-white
   exceptions.
4. **Name the equation, not a menu item.** White Difference, Exclusion and Invert are
   pixel-identical at the white endpoint.

   | Operator | Equation | Alpha solve |
   |---|---|---|
   | SourceOver | `O = B + α(F − B)` | white: `α = (O − B)/(1 − B)` |
   | Difference | `O = B + α(\|B − S\| − B)` (white S gives `1 − B`) | white: `α = (O − B)/(1 − 2B)` — reject near-zero denominators |
5. **Channel registration.** A cross-correlation peak at (0,0) between channels means there are
   no offset RGB ghost copies; the colour is an in-glyph gradient, a whole-frame effect, 4:2:0
   bleed or JPEG.
6. **Reported colours are decoded output samples, not design hex.** A 1–2 px cyan/red fringe in
   `yuv420p` is codec bleed, not design.
7. **Measure every state's ink before building** (`L0057`): a flat colour is its median; a
   footage fill is recorded as a device. Every non-white reference ink is listed on the job
   card. If a reference colour fails the readability gate on your bed, do not silently swap the
   colour or bypass the gate: raise a call with both measurements and the options (recast onto a
   darker bed, or a heavier shadow).
8. **Footage-filled glyphs** (`L0089`): if colour varies inside the glyphs (a low flat-ink
   `ref_fit` < 0.6 is the tell), measure the in-glyph low-frequency colour field, rebuild it with
   detail from your own footage, place the word by luma-edge alignment, and fit the face on a
   **union** mask (solid ink OR fill colour) (`L0082`). Gate per letter, not only the median.
9. `reelctl/captions/extract.py` holds three non-circular segmentation models: minimum-channel
   (`min(B,G,R) > t`, so saturated brights are not mistaken for white), chromatic channel
   difference, and temporal difference (median of the state's frames vs median of adjacent
   **blank** frames — uses only the reference's own text-free neighbours). Mask tiers:
   `clean` (separation ≥ 60), `secondary` (≥ 30), `diagnostic_partial`; only clean/secondary
   are promotable. `fit_compositing_operator` picks between models with an undecided margin of
   0.15.
10. **Edge softness is measured, not guessed** (`L0038`): `onetoone.sharpness` computes 10–90 %
    edge width on decoded frames (`width = 0.8·C/Gpk`, ink = `min(R,G,B) ≥ 235`). A hard 1-px
    step reads ~1.6 px; crisp captions typically 1.6–1.8 px; a soft reference from a lossy
    platform encode can read ~2.4 px. Pass threshold for "sharp": ≤ 2.0 px. Match the
    reference's softness by edge width / profile error, not a fixed floor.

### Step 9 — Effects, pulses and caption entry devices

**Pulses and glitches.** For every pulse frame, inspect PRE / PULSE / POST. Classify:

- clean A/B recall of a real earlier frame (grammar like `A A B A A B B B A`);
- same-moment near/wide reframe;
- inversion (test the residual against `255 − neighbour estimate`);
- slice / RGB split; flash; or a true synthetic glitch.

Record the layer order relative to captions. Describe effects by **operator, vector and
lifecycle** (slit stretch, directional whip, affine whip). When adapting, port the grammar and
density, not absolute frame numbers. Avoid one-frame pulses in very short shots (1 frame of a
6-frame shot is 16.7 %). Every recurrence of the same sound cue gets the same real-shot
micro-edit. Prefer real near/wide recall from your own footage over synthetic glitch overlays.

**Caption entry devices.** Captions rarely cut in; they arrive. Measure the arrival from the
reference's own frames:

```bash
cd <REPO>/code/reel-production-tools
python3 -m onetoone.refstrip <refframes> <brain> <state_id> <part_index> <n0> <n1> strip.png
python3 -m onetoone.measure_devices ...     # writes <ROW>/brain/caption_devices.json
python3 -m onetoone.devicesheet <brain> <refframes> <ourframes> <captions> <outdir> [ids...]
```

- `refstrip` tiles the part's padded bbox across frames `n0..n1` with frame numbers, so the
  entry device can be **read** before anything is fitted.
- `measure_devices` writes per-frame curves: onset frame, sharp frame, blur sigma **x and y
  separately** (a horizontal smear vs an isotropic defocus), opacity, scale and drift. Method:
  **align first, then measure.** References' captions are rarely stationary (a slow 0.90→1.00
  scale ramp plus drift is common); an unaligned fit reads displacement as blur. It computes a
  top-hat residual (window 71 px, wider than the widest stroke), uses the settled frame's
  residual cut to the settled box as template, locates per frame by normalised
  cross-correlation over a scale grid (coarse 0.60–1.36 step 0.04, template blurred 4.0 then
  2.0), requires the located ink to sit ≥ 60 % on the settled box, bounds scale to 0.80–1.30,
  and fits blur over a sigma grid 0–15. Known bug: the CLI's frame cap — pass the reference's
  own frame count or later states come back `STATIC` (`L0074`).
- `devicesheet` puts reference and ours side by side per frame across the entry window. The beds
  differ, so it judges only the device: same first frame, same blur on the way in, same sharp
  frame, same growth.
- When tuning a blur-in for your bed, sweep sigma offline per glyph against the readability rule
  before a full build; the readability response to sigma is not monotonic (`L0111`).
- Ink treatments (shadow, glow, outline) are applied **after** the device, per frame, on the
  composed layer — never baked into the settled layer the device reads (`L0002`).

### Step 10 — Colour of the reference

- `signalstats` time series (`YMIN/YLOW/YAVG/YHIGH/YMAX`, `SAT`, `BRNG`); per shot luma
  q10/q50/q90, highlight coverage, saturation, neutrals and skin through ROIs, lighting families.
- A social delivery is a **creative** reference, not colorimetric truth. Never assume one LUT
  made the reference.
- Measure on **caption-masked** pixels; mark transition, flash and caption-card frames invalid as
  static targets.
- **Per-stage causality** for a bad shot: source → normalised → matched → composited → encoded,
  tracked with G/R, B/R, opponent `G − (R+B)/2`, Lab a*, green occupancy. Independent per-channel
  RGB matching once turned a shot green (G/R 0.78 → 1.65); use smooth gamma/saturation matching
  or bounded monotone curves:
  `y = x + clip(strength·(target_y − x), −max_delta, +max_delta)`, smoke start
  `strength = 0.4, max_delta = 0.12`, monotonic, excluding pulse frames from the fit.
- **Per-shot cast gates:** `blue_excess = B − (R+G)/2`, blue-dominant and cyan-dominant pixel
  fractions with thresholds calibrated per footage. If one shot fails while the timeline average
  improves, reject that shot.
- **Per-shot luma vs the reference:** `python3 -m onetoone.lumacheck <brain> <reference.mp4>
  <ours.mp4> <work_dir>` reports BED (caption rectangles removed from both sides — what the grade
  controls) and WHOLE views, mean difference, worst frame and worst frame-to-frame step.
- **Decode like a player.** ffmpeg's default swscale yuv→rgb reads ~2 levels darker than real
  players on tv-range BT.709; measure and composite on player-exact planes
  (`onetoone.yuvexact`).
- **Camera originals** (your footage, not the reference): prove the log profile from metadata
  (e.g. a Sony camera shooting S-Log3: `exiftool -G1 -a -s -api LargeFileSupport=1` for
  `CaptureGammaEquation`/`CaptureColorPrimaries`). Null container colour tags mean **unknown**;
  "looks flat" never identifies a log curve.

How your own footage is graded toward the reference (house LUT look, exposure/clip/crush gates)
is in [02_CRAFT_PLAYBOOK.md](02_CRAFT_PLAYBOOK.md).

### Step 11 — Evidence tiers (label every claim)

**Review ladder (what you actually did):**

| Tier | Meaning | Supports |
|---|---|---|
| probe | metadata only | identity |
| decode | full decode, empty error log | integrity |
| sampled board | broad coverage | composition |
| gap-free frames | every frame seen | structure, captions (Tier C) |
| native strips | exact in-points and anatomy | boundaries, typography |
| browser transport | playback reached the end | transport only, not perception |
| full AV playback | normal speed, with audio | rhythm, feel, sync (Tier A) |

Review labels: `FULL_AV_CREATIVE_REVIEW`, `FULL_VISUAL_MOTION_REVIEW_AUDIO_IDENTITY_ONLY`,
`PLAYBACK_TRANSPORT_COMPLETE_PLUS_FRAME_COMPLETE_VISUAL_REVIEW`, `FRAME_COMPLETE_VISUAL_REVIEW`.
Only the first two support motion-feel opinions. If a review was impossible and a verdict is
forced, write `FAIL — review/evidence gate (not a creative-quality finding)`.

**Footage tiers:** `still_only` (never shortlist-eligible), `motion_proxy`, `native_window`,
`native_locked`. An 8 fps proxy samples every 125 ms (3.75 frames at 30 fps) and cannot certify a
30 fps boundary — emit a search window instead.

**Corpus tiers:** `INDEXED` → `STILL_TRIAGED` (composition only) → `PROXY_REVIEWED` (apparent
action) → `MOTION_VERIFIED` (contiguous frames over the selected interval plus boundary context
and endpoint) → `RENDER_CROP_VERIFIED` (actual interval, grade, frame count and final crop at
output level).

**Claim labels:** speaker claim, visible evidence, independent evidence, unresolved.

A lower tier never inherits a higher tier's certainty. An analyser "success" with no pixels is
zero evidence. Never silently promote a tier.

### Step 12 — QC thresholds the breakdown hands to the rebuild

| Gate | Threshold |
|---|---|
| Cut placement | within ±2 frames of reference onsets (`reelctl` code); exact boundary match for reference-locked; every cut must peak on the **decoded** file, none one frame late (`L0053`) |
| Action peak | within ±2 frames of target, camera motion subtracted |
| Feasibility (reference-locked) | ≥ 80 % of picture blocks `exact_scene_available`, else BLOCKED with a mode-switch recommendation |
| Caption contrast (mask core alpha ≥ 192, linear luminance, `CR = (max+0.05)/(min+0.05)`) | PASS: C10 ≥ 3.0, C50 ≥ 4.5, < 10 % of core pixels below 3:1. SEVERE: C50 < 3.0 or ≥ 50 % below 3:1 |
| Alpha-aware glyph gate | core alpha ≥ 192; a pixel passes if luma ratio ≥ 1.5 or ΔE76 ≥ 20; frame passes if ≤ 10 % of core pixels fail |
| `reelctl` caption contrast | ≥ 1.8 |
| Kit readability gate | halo ratio ≥ 3.0 (ink vs 4–8 px band outside glyphs on the shadow-composited bed); contrast ≥ 0.6 ratio and ≥ 25 luma absolute; white ink needs a shadow; shadow radius ≤ 20 px |
| Ink cleanliness (`inkcheck.py`, worst sampled frame) | interior-vs-ring separation ≥ 25 luma AND Michelson ≥ 0.80 × the reference's own for that state (ring = dilate 7 → 17 px) |
| Per-glyph visibility | reference-relative: ours ≥ min(0.60, reference − 0.05) on the same glyphs, checked on every frame, not only mid-frames (`L0068`, `L0069`, `L0075`) |
| Typography fit | `ref_fit` ≥ 0.6 to be trusted by the typesetter; anatomy Dice ≥ 0.90 + topology equality; `reelctl` trace acceptance IoU ≥ 0.995; traced face IoU ≥ 0.83 on untraced frames |
| Caption crispness (`reelctl` caption QC) | crisp states Dice ≥ 0.90; blur states Dice ≥ 0.60; ink distance ≤ 6 (8-bit) |
| Edge width | ≤ 2.0 px for "sharp"; match reference within 0.1 px when matching softness |
| Composite levels identity (10-bit) | p05/p50/p95 shift ≤ 10 code values; dynamic-range ratio 0.98–1.02 |
| Colour match (bounded, per shot) | q10/q50/q90 error ≤ 0.05; saturation error ≤ 0.06; highlight coverage ≤ 5 percentage points |
| Legal range | 8-bit 16–235 (10-bit 64–940), measured on the **decoded** review encode; an H.264 CRF 12 review needed a pre-encode `lutyuv=y='clip(val,30,221)':u='clip(val,16,240)':v='clip(val,16,240)'` (tighter chroma 32–224 breaks saturated primaries) |
| Geometry | SAR 1:1 at every segment; normalise rotation/SAR before crop; final SAR/DAR probed |
| Endpoint | `-frames:v N`, never `-shortest` alone (it can drop the last frame) |
| Pulses | exactly one frame; pairs exactly two indices apart; the next frame clean; captions on pulse frames pass contrast |
| Scope proof | `framemd5` on intra-frame masters (ProRes): `changed_frames == allowed_changed_frames`; never H.264 frame hashes for this |
| Delivery | full normal-speed watch with audio; exact last frame inspected at phone size |

### Step 13 — Write it down

Persist from the first minute, never only in a temp directory or terminal scroll-back:

- the source lock JSON;
- the frame-exact ledger: picture, caption, effect and audio tracks over `0..N-1`;
- boards (contact sheets, boundary boards, refstrips, overlays);
- a plain-English description of the reel;
- a **layer matrix** comparing any candidate to the reference: `IDENTICAL / ALIGNED BUT NOT
  IDENTICAL / DIFFERENT / UNPROVEN` per layer.

Write the minimum deliverable first, enrich later. A correct but unwritten analysis is an
incomplete task.

#### The brain pack (one canonical root per job)

```text
<ROW>/brain/
  00_PROJECT_BRAIN.md          complete plain-English understanding
  01_REFERENCE_CONTRACT.json   exact source + grammar/state map
  02_SHOT_BLUEPRINT.json       blocks, timing, roles, layers
  03_FOOTAGE_REGISTRY.json     corpus, hashes, tags, intervals
  04_FEASIBILITY_MATRIX.md     exact / equivalent / missing coverage
  05_SELECTION_SHORTLIST.md    ranked candidates with evidence
  06_MISMATCH_LEDGER.md        every failure and its disposition
  07_CALL_PROMPTS.md           unresolved decisions only
  08_DECISION_LOG.md           append-only feedback and choices
  09_RESUME_STATE.json         stage, revision, hashes, next action
  10_EVIDENCE_MANIFEST.json    paths, sizes, hashes, coverage claims
```

Kit-specific files that live alongside (consumed by the one-to-one kit; see
[05_CODE_GUIDE.md](05_CODE_GUIDE.md)): `cutgrid.json` (frame geometry and cut frames),
`captions.plaintext.json` (states, parts, faces with `face_file/face_name/face_evidence`,
`ref_fit`), `caption_devices.json`, `refpeople.json`.

Never create two competing brain roots. The files on disk, not chat memory, are the source of
truth for continuation.

#### Machine record for a reference rule card

```json
{
  "continuity_world": "single_world|multi_world",
  "lineage": {
    "type": "single_reference|multi_reference_explicit|original_user_directed",
    "references": [{"ref_id": "<id>", "sha256": "<hash>", "rule_card": "brain/01_REFERENCE_CONTRACT.json"}]
  },
  "grammar": {
    "timeline_driver": "spoken_quote|lyrics|music|action|atmosphere",
    "text_grammar": "none|progressive_quote|lyric_lifecycle|title_cards",
    "cut_grammar": [],
    "shot_role_sequence": [],
    "audio_cues": [],
    "ending": "payoff|hold|reset|loop"
  },
  "adaptation": {
    "mode": "exact_reference|reference_grammar",
    "layers": {
      "in_video_text": {"origin": "reference:<id>", "operation": "LOCK"},
      "typography":    {"origin": "reference:<id>", "operation": "ADAPT"},
      "footage":       {"origin": "authorized_pool", "operation": "SUBSTITUTE"},
      "audio":         {"origin": "licensed:<track-id>", "operation": "SUBSTITUTE"}
    }
  },
  "authority": {"technical": "PASS|FAIL", "visual": "PASS|FAIL|PENDING",
                "human": "APPROVED|REJECTED|PENDING", "publication": "PUBLISHED|UNPUBLISHED"}
}
```

---

## 4. From breakdown to feasibility

Once the blueprint is locked, map each reference block to your authorized footage before any
selection:

```json
{"schema_version": 1, "status": "DRAFT",
 "blocks": [{"block_id": "p001", "reference_role": "...",
   "coverage": "exact_scene_available|role_equivalent_substitute|missing",
   "evidence": "INDEPENDENT_EVIDENCE; clip_id=...; clip_sha256=...; thumbnail_paths=...; observed=...",
   "evidence_clip_ids": ["inventory-bound-clip-id"],
   "decision": "EXACT_SCENE_SELECTED|APPROVED_ROLE_EQUIVALENT_SUBSTITUTE|ACQUIRE_NEW_FOOTAGE"}]}
```

```bash
reelctl --projects-root <WORKDRIVE>/projects feasibility template <ID>
# edit edit/feasibility.json
reelctl --projects-root <WORKDRIVE>/projects feasibility lock <ID>
reelctl --projects-root <WORKDRIVE>/projects run <ID> --through local-review --revision v001
```

- Thumbnails prove only role and composition — not motion, timing or caption clearance.
- Literal claims need literal support: a dark laptop is not a neon object; a pool is not a
  sports court.
- Reference-locked needs ≥ 80 % exact or it is `BLOCKED` with `mode_switch_required`. Present
  gaps **before** any render cycle.
- `reelctl` exits 0 on `BLOCKED` (2 only on FAIL): always parse the JSON status.
- Never use the reference file as substitute footage, never silently switch to grammar
  adaptation, never generate empty downstream artifacts.
- A blocked report states: project and mode, lock path and hash, exact/equivalent/missing counts,
  ratio vs threshold (0.8), missing block IDs, recommended mode, that no render exists, and the
  precise decision needed.
- Large libraries: `reelctl footage shortlist --top-n 20` → ~5 coverage boards → ~3 normal-speed
  renderer-exact crop proxies → 1 winner + 1 alternate. Output stays
  `MACHINE_SHORTLIST_REVIEW_REQUIRED` until a human or a verified watch confirms.

---

## 5. Pitfalls specific to forensics

- Audio identity mistaken for musical sync.
- Shot count mistaken for picture-state count.
- Typography checked semantically (right words) instead of as rendered evidence.
- The cut detector used as the picture clock or as structural authority.
- Path, size or mtime used as identity (hash the content).
- A one-frame namespace shift from 1-based numbering or a renumbered frame directory.
- Decoder exit 0 taken as a complete decode; a sampler wrapper's exit 0 taken as a successful
  batch.
- `range=tv` taken as proof of nominal extrema (gate pre-encode with `signalstats`; prove on the
  decoded file).
- Mask streams not normalised to the source timebase before `framesync` (prove frame N maps to N,
  not N±1).
- A grayscale `L` caption mask converted with `convert('RGBA')` gets an opaque alpha and blacks
  out everything outside the glyph — use `L` as the alpha channel.
- Autorotated portrait sources scaled to a fixed raster and stream-copy concatenated: one
  segment's SAR flattens the others. Normalise with cover scale + crop + `setsar=1`; reject
  non-square segments. (A 9:16 frame shown as 16:9 widens by 256/81.)
- Caption contrast evaluated on full-opacity ink instead of the alpha-composited glyph.
- Colour gated on timeline averages instead of per shot.
- Cuts and pulse returns included in within-shot motion metrics.
- One reference turned into a fixed shot-length, transition, caption or LUT preset for every
  future reel.
- A traced or thresholded mask treated as production-ready before a topology board.
- A glyph score allowed to veto what the letter visibly reads as.
- A flat visibility threshold the reference itself fails (use reference-relative gates).
- An audit finding fixed on the named frame only: a finding is a **defect class** — re-measure it
  on every shot and every frame (`L0069`, `L0075`).
- Reviewers majority-voted. An ingestion failure is `REVIEW_INCONCLUSIVE`; a review under the
  wrong contract is `CONTRACT_PREMISE_ERROR`.

---

## 6. Tool index for this document

All kit modules run from `<REPO>/code/reel-production-tools` as `python3 -m onetoone.<name>`.
Several hard-code `ffmpeg`/`ffprobe` locations and font directories; adjust them for your machine
(see the porting checklist in [05_CODE_GUIDE.md](05_CODE_GUIDE.md)).

| Step | Tool | Location |
|---|---|---|
| 1 | reference lock, PTS/frame/audio ledgers, cut scores, all-frames board | `code/reelctl/src/reelctl/reference.py` (`reelctl reference analyze`) |
| 3 | camera move (zoom/pan/rotation) between frames | `onetoone/motion.py` |
| 4 | onsets, BPM, grid phase, downbeats | `beatmap.py` |
| 4 | word timestamps (guarded) | `whisper_guard.py` |
| 4 | vocal isolation (guarded) | `vocal_guard.py` |
| 5 | where the reference shows a person | `onetoone/refpeople.py` |
| 6 | declared vs reference words | `caption-learning/wordtruth.py` |
| 6 | OCR ensemble read-back | `caption-learning/readback.py` |
| 6 | caption band without boxes | `caption-learning/bandpresence.py` |
| 7 | ink crop, identify, find, fit, trace | `onetoone/faceid.py` |
| 7 | typeset-parameter fit to reference pixels (size, tracking, stretch, x, y; `MIN_SCORE 0.45`, ink tolerance 70 RGB) | `onetoone/refit.py` |
| 7 | font inventory, matching, exact-font plates | `code/reelctl/src/reelctl/typography.py` (`reelctl typography inventory|match|extract-mask|trace`) |
| 7 | holdout font-identity proof | `code/reelctl/src/reelctl/captions/fontproof.py` |
| 7 | Dice + boundary residual + topology gate | `caption-learning/anatomy.py` |
| 8 | ink segmentation, mask tiers, operator fit | `code/reelctl/src/reelctl/captions/extract.py` |
| 8 | interior-vs-ring ink cleanliness | `caption-learning/inkcheck.py` |
| 8 | edge width on decoded frames | `onetoone/sharpness.py` |
| 9 | entry-window strip | `onetoone/refstrip.py` |
| 9 | per-frame device curves | `onetoone/measure_devices.py` |
| 9 | reference-vs-ours device sheet | `onetoone/devicesheet.py` |
| 10 | per-shot luma vs reference | `onetoone/lumacheck.py` |
| 10 | player-exact YUV↔RGB | `onetoone/yuvexact.py` |
| all | full caption parity sweep (words, anatomy, ink, presence) | `caption-learning/sweep.py` |
