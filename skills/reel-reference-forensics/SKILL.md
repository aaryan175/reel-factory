---
name: reel-reference-forensics
description: Break down a reference short-form video (reel) frame-exactly before rebuilding or adapting it - source lock, cut list, picture-state ledger, beat/onset map, shot roles, caption lifecycle, font identification, ink/blend modes, effects and pulses, reference colour, and evidence tiers. Use when asked to "analyse this reel", "break down this reference", "what makes this edit work", "how was this cut/captioned", or before any reference-led reel build.
---

# Reel Reference Forensics

Turn one reference video into a frame-exact, written contract that a rebuild can be checked against. The output is evidence, not opinion: every claim carries an evidence tier, and nothing is locked from a detector alone.

## Ground rules

1. **Lock the exact source first.** A repost, re-download or transcode is a different reference until proven identical at the decoded-pixel layer.
2. **A tool "success" is transport, not analysis.** An analyzer that returned no pixels has seen nothing.
3. **Detectors propose, eyes decide.** Scene-score and OCR output are navigation aids. Every boundary and caption state is confirmed on native-resolution frames.
4. **Record picture states, not just shots.** Pose jumps, held smears, insert pulses, reframes and effect states are boundaries too.
5. **Typography is not semantics.** Matching words and frame ranges says nothing about face, size, tracking, ink or blend. Those are separate proofs.
6. **Identical audio is audio identity, not musical sync.** Sync claims need onset evidence plus a normal-speed watch.
7. **Write as you go.** Create the report file right after the source lock. Never leave the only result in terminal output or a temp directory.

Rights: analyse references you are allowed to study. Close recreations of someone else's edit need their permission; respect each platform's terms when acquiring media; never persist signed media URLs.

## Evidence states (advance in order)

`SOURCE_LOCKED -> DECODE_PROVEN -> PIXELS_SEEN -> AUDIO_HEARD -> FULL_WATCH`

Only `FULL_WATCH` (normal speed, with audio, to the last frame) supports claims about rhythm, beat feel, smoothness or whether the ending lands. A model that cannot play video must say which tiers it actually completed (usually gap-free frames + decoded audio analysis).

## Workflow

### 1. Lock the source

```bash
shasum -a 256 REF.mp4
ffprobe -v error -show_entries stream=index,codec_type,codec_name,width,height,pix_fmt,sample_aspect_ratio,display_aspect_ratio,r_frame_rate,avg_frame_rate,time_base,start_pts,duration_ts,nb_frames,sample_rate,channels:stream_tags=rotate:stream_side_data=rotation -of json REF.mp4
ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of csv=p=0 REF.mp4
ffmpeg -v error -i REF.mp4 -f null - 2> decode.log   # decode.log must be empty
ffmpeg -v error -i REF.mp4 -map 0:v:0 -f framemd5 ref.video.framemd5
ffmpeg -v error -i REF.mp4 -map 0:a:0 -f framemd5 ref.audio.framemd5
```

- Record the **rational** frame rate exactly (`30/1`, `24000/1001`, `2997/125`). Seconds = `frame / rate`. Never compute frame count as decimal fps x duration, and never round a close rational rate to a familiar one.
- Identity has three layers: container bytes, compressed packets, decoded essence (frame hashes + PCM). A re-download that differs in bytes but matches every decoded frame is the same picture evidence; it does not inherit audio or container identity.
- If video and audio arrive as separate streams, lock both; a convenience mux is playback evidence only.

### 2. Freeze a frame namespace

Decode every frame once, zero-based, into an immutable directory and hash it. Every later board cites these indices.

```bash
mkdir -p frames && ffmpeg -v error -i REF.mp4 -map 0:v:0 -fps_mode passthrough -start_number 0 frames/%05d.png
ls frames | wc -l    # must equal nb_read_frames
```

A one-frame namespace shift (a renumbered directory, `-start_number 1`) moves every cut while still looking plausible.

### 3. Watch it five ways

Full speed with audio; full speed muted; frame by frame at every change; as a gap-free numbered contact sheet; as caption/effect/cut boundary boards. For short reels, numbered all-frame sheets (e.g. 24 frames per page) are standard; assert by machine that the pages expand to exactly `range(N)`.

```bash
ffmpeg -v error -i REF.mp4 -vf "scale=240:-2,drawtext=text='%{frame_num}':x=6:y=6:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.6,tile=6x4" -fps_mode passthrough -start_number 0 sheets/sheet_%03d.png
```

If the ffmpeg build lacks `drawtext`, number the tiles with Pillow instead. A contact sheet is coverage, not proof that small text was read.

### 4. Cut list (picture track)

1. Candidates: `ffmpeg -i REF.mp4 -vf "select='gt(scene,0.08)',metadata=print:file=scenes.txt" -an -f null -`. Thresholds: 0.05 sensitive, 0.08-0.12 typical for reels, 0.25 conservative. Add adjacent-frame luma difference as a second signal. Convert `pts_time` to frame with the rational rate.
2. Inspect the PRE/POST pair of every candidate at native resolution. Background geometry outranks the score: dark-to-dark cuts can score low; fast camera motion can out-score real cuts.
3. Classify every boundary: distinct-shot hard cut, same-camera temporal jump, crop-only reframe, composite-state switch, effect-state boundary, ordinary motion. An alternating low/high/low difference pattern usually means a ~12 fps source frame-doubled into ~24, not cuts.
4. Output half-open intervals `[start, end)` covering `0..N-1` exactly once, plus `picture_boundaries_after` (every visible change) and `hard_cuts_after` (strict subset). Example shot-frame vector for a 216-frame reference: `[16,14,15,16,14,14,13,14,13,12,13,14,13,35]` (sums to 216).
5. Endpoint class: `live_motion_cutoff`, `held_or_frozen`, `fade_to_black`, `black_frame`, `end_card`, `exact_loop` (requires decoded-hash equality of last and first frames), `loop_aware_bookend`. Report audio end in ms relative to video end.
6. Cadence stats: count, median/mean/min/max shot length, number of sub-0.5 s bursts.

### 5. Beat map

Two onset views: full-band and low/percussive (in vocal music full-band peaks are often syllables). Map `onset_frame = round(onset_s * rational_fps)`. For each picture event report `delta_ms = 1000 * (frame/rate - onset)`; one frame is 41.7 ms at 23.976 and 33.3 ms at 30. Fit a beat grid `phase + k*step` (report step, BPM, RMS residual; keep half/double/triplet ambiguity open). For speech-led edits, check cut-to-word alignment within 50 and 100 ms. Detector recipe: `references/forensics-playbook.md`.

### 6. Roles and grammar

Per segment: visible verb (turn, lift, enter, cross, reveal...), subject scale, depth, camera movement, palette, title field, and editorial role (hook, entry, movement, environment, action, detail, escalation, reset, payoff, finale, loop return). Continuity axis: single-world, journey-compression or instructional. For short slots, phase structure: anticipation -> event -> consequence -> stable endpoint (a 16-frame slot might be 0-2 / 3-9 / 10-13 / 14-15). Judge the world grammar before ranking individual exciting shots.

### 7. Captions, fonts, ink

- Semantics: exact on-screen string (case, punctuation, deliberate spellings), progressive builds as separate intervals, every no-text frame counted. Keep caption-state starts separate from word onsets and visible text separate from the audible transcript; caption changes are independent of cuts.
- Lifecycle: first detectable -> readable blur -> near crisp -> first crisp -> hold -> exit. Use blur categories (extreme / heavy / medium / light / crisp); never invent Gaussian radii. A crisp caption over a smeared picture means the effect sits below the captions.
- Font identification, ink and blend modelling: use the `caption-typesetting-and-font-matching` skill.

### 8. Effects and pulses

For every pulse frame inspect PRE / PULSE / POST. Classify: clean A/B recall of a real earlier frame (grammar like `A A B A A B B B A`), same-moment near/wide reframe, inversion (test residual against `255 - neighbour estimate`), slice/RGB split, flash, or true synthetic glitch. Record layer order relative to captions. Describe effects by operator, direction vector and lifecycle. When adapting, port grammar and density, not absolute frame numbers.

### 9. Reference colour

`signalstats` time series (YMIN/YLOW/YAVG/YHIGH/YMAX, SATAVG, BRNG); per-shot luma q10/q50/q90, highlight coverage, saturation, neutral and skin ROIs, lighting families. A social delivery is a creative reference, not colorimetric truth; never assume one LUT made it. Measure on caption-masked pixels and exclude transition, flash and caption-card frames.

### 10. Evidence tiers - label every claim

probe (metadata) < decode (integrity) < sampled board < gap-free frames < native strips < browser transport (playback reached the end; not perception) < full AV playback. A lower tier never inherits a higher tier's certainty.

### 11. Write it down

Deliver: source-lock JSON; a frame-exact ledger with picture, caption, effect and audio tracks over `0..N-1`; boards; a plain-language description of what the edit does; and a layer matrix with each layer marked `IDENTICAL / ALIGNED_NOT_IDENTICAL / DIFFERENT / UNPROVEN` when comparing to a candidate. Template and commands: `references/forensics-playbook.md`.

## Common mistakes

- Using the cut detector as the picture clock.
- Shot count reported as picture-state count (one reference had 28 picture blocks; a rebuild with 16 "felt off" even at 0 ms audio lag).
- Trusting `range=tv` tags or decoder exit 0 as proof.
- OCR at low resolution treated as evidence (it is a lead; mark small text illegible).
- Turning one reference into a fixed preset (shot length, transition, LUT, caption style) for everything else.
