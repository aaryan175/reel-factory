# 05 — Code Guide

Module-by-module reference for the code under `code/`: what each module is for, how to call it, the constants that matter, and which lesson (`L00NN`, see [04_LESSONS.md](04_LESSONS.md)) it enforces. Every constant, flag and threshold here was checked against the source in this repository. Where older design notes disagree with the code, the code wins and the difference is called out.

Related docs: [01_ARCHITECTURE.md](01_ARCHITECTURE.md) (how the parts talk and how to stand them up), [02_CRAFT_PLAYBOOK.md](02_CRAFT_PLAYBOOK.md) (why the numbers are what they are), [03_QA_SYSTEM.md](03_QA_SYSTEM.md) (gates and the learning loop), [06_REFERENCE_FORENSICS.md](06_REFERENCE_FORENSICS.md) (how a reference is studied before the kit runs), [07_WORKING_WITH_AN_AI_OPERATOR.md](07_WORKING_WITH_AN_AI_OPERATOR.md) (running lanes with Claude Code).

---

## 0. Conventions

| Placeholder | Meaning | Default written in the code |
|---|---|---|
| `<REPO>` | this repository's checkout | — |
| `~/reel-production` | the **factory root** at runtime: `tools/`, `_receipts/`, `_control/`, `lessons.json`, `REEL_REGISTRY.json`, `FOOTAGE_LIBRARY.json` | hard-coded as `~/reel-production` in most modules |
| `<WORKDRIVE>` | the big work disk (renders, row work trees, footage library) | `/Volumes/WORKDRIVE` |
| `<FOOTAGE>` | the footage library root (camera masters + proxies) | `<WORKDRIVE>/workbench/footage-library` |
| `<REFS>` | reference reels and their study folders | per-row `ref/` folders, plus `~/reel-production/reference-intake/` |
| `<RENDERS>` | per-row work trees (`ref/`, `brain/`, `work/`, `deliver/`) | `$REEL_FACTORY_WORKBENCH/reel<NN>-<REFID>-<date>/` (default `<WORKDRIVE>/workbench/...`) |
| `<DECK_PORT>` | Reel Deck loopback port | `7355` |
| `<PYTHON>` | the interpreter that runs `tools/` (must have pytest) | `/usr/bin/python3` |

- "Row" means one entry in the reel registry (one reel being built, numbered `NN`). "Lane" means one Claude Code session started by the order daemon to work one order. "Reviewer" means the human who orders reels, answers questions and approves deliveries.
- Commands are shown as they run after deployment. The deployed layout is:

| In this repo | Deployed at | Notes |
|---|---|---|
| `code/reel-production-tools/` | `~/reel-production/tools/` | the kit (`onetoone/`), plumbing, `orderd/`, `ditl/` |
| `code/caption-learning/` | `~/reel-production/caption-learning/` | caption forensic battery |
| `code/reelctl/` | `~/reel-production/_reelctl/` | legacy signed stage machine (package `reelctl`) |
| `code/reel-deck/` | `~/apps/reel-deck/` | review web app |
| `config/launchd/*.plist` | `~/Library/LaunchAgents/` | Deck, order daemon, asker |
| `config/control/*.json` | `~/reel-production/_control/` | kill switch + daemon state (examples) |
| `config/reel-production.claude-settings.json` | `~/reel-production/.claude/settings.json` | project permissions allow-list for interactive sessions |

### 0.1 The four code bodies

| Body | Size | What it does | Status |
|---|---|---|---|
| **One-to-one kit** `tools/onetoone/` | 25 modules, ~6.5k lines, 18 test files | Rebuilds a reference reel shot for shot from your own footage: preflight gate, casting, house grade, typeset captions, measured entry devices, one-ffmpeg render, measurement | **The live render path.** Nothing else should render. |
| **Factory plumbing** `tools/*.py`, `tools/orderd/`, `tools/ditl/` | ~12k lines | Bus writer, lessons loop, ASR/stem-separation guards, footage queries, order daemon that launches lanes, a "day in the life" generator | Plumbing live; the older variant/Resolve tools are dormant |
| **reelctl** `_reelctl/` | ~21.5k lines src, ~1,060 tests | Signed 14-stage machine with receipts, locks, kill switch and a web studio | **Halted by design** (see §6.1). Kept as a library of contracts and as the home of the house LUT path |
| **Review and forensics** `caption-learning/`, `apps/reel-deck/` | ~6k + ~4.7k lines | Caption readback/anatomy/ink audit battery; the Deck (FastAPI review app) | Deck live; caption-learning is a library the audit calls |

### 0.2 The four "only legal way" rules

Most hard gates exist because something once bypassed one of these:

1. **ffmpeg only through `onetoone.ffx`** (one encode on the machine at a time).
2. **Whisper only through `tools/whisper_guard.py`**, stem separation only through `tools/vocal_guard.py` (one ASR process machine-wide).
3. **Bus writes only through `tools/busline.py`** (structured status, never hand-written ACK lines).
4. **Registry writes only through `onetoone.registry_row`** (flock + timestamped backup).

---

## 1. Environments and how the kit is invoked

- **The kit is not an installed package.** Run it from the tools directory so `onetoone` and the sibling `variant_render.py` import:
  ```bash
  cd ~/reel-production/tools && <PYTHON> -m onetoone.<module> ...
  ```
  From any other directory you get `No module named onetoone`. `render.py` additionally puts `tools/` on `sys.path` so `import variant_render` works.
- **Interpreter.** The original deployment ran the kit on one Python and tests on another (only one had pytest). `preflight` runs the kit tests itself, so the interpreter that runs `preflight` must have pytest (L0051). On a new machine install one interpreter with every dependency in §10 and use it everywhere.
- **Canvas and clock defaults.** `render.render(size=(1916,1078))`, `framing.filter_for(aspect=1916/1078)`, `framing._zoompan_for` (hard-codes `s=1916x1078`), `castscan.scan(size=(1916,1078))`; output clock `24000/1001` fps with mp4 timescale `24000`. These are the geometry of the references the kit was built on (16:9 masters, cover-cropped). A different canvas needs these parameterised (§11).
- **The preflight gate is technical, not advisory.** `render.render()` and `render.cut_picture()` refuse to run unless `onetoone.preflight --cast <cast>` passed in the last 6 hours on byte-identical cast JSON and an unchanged lesson set. The only bypass is `REEL_NO_GATE=1` in the environment (throwaway local tests only).

### 1.1 Data files a row carries

A row lives in a work tree under `<RENDERS>`. The kit reads and writes:

| File | Written by | Read by | Content |
|---|---|---|---|
| `ref/ref24.mp4` (or `ref/reference-source.mp4`, `ref/ref.mp4`) | intake | everything | the reference, conformed to 24000/1001 |
| `brain/cutgrid.json` | intake study | render, grade, lumacheck, castscan, bedprobe, looksheet, identity | `{"shots": [{"slot": "S01", "in": 0, "out": 23}, ...]}` (frame numbers, **inclusive**). An alternative `picture_blocks` form (`{"id": "P1", "frames": [a, b]}`) is understood only by `identity._slot_ref_window` |
| `brain/captions.plaintext.json` | intake study, then mutated in place by `refit`, `ornate_env`, and pasted `faceid fit` output | captions_typeset, render, castscan, bedprobe, looksheet, measure_devices, refstrip, devicesheet | `{"states": [{"id": "c02", "in": 32, "out": 64, "shadow": false?, "behind_subject": {...}?, "parts": [{"part": "plain_line"/"ornate_word", "text", "words", "bbox_settled"/"bbox_settled_approx", "bbox_frame", "ink_rgb"/"ink_rgb_approx", "local_bed_rgb", "face_file", "ref_fit", "ref_script_metrics", "ref_ink_env", "word_reveal", ...}]}]}` |
| `brain/caption_devices.json` | `measure_devices` | `devices`, `render`, `devicesheet` | per-state, per-part entry curves, schema `reel-caption-devices-v3` |
| `brain/refpeople.json` | `refpeople` | `identity.refpeople_rules`, `castscan` | sampled reference frame → face heights |
| `deliver/cast_vNNN.json` | the lane | preflight, identity, render, grade, castscan, looksheet | `{"grade_mode": "house"/"natural"/"match", "caption_ink": [r,g,b]?, "caption_shadow": {...}?, "identity_slots": [...]?, "slots": [{"slot": "S01", "stem": "CLIP_0086", "in_s": 3.2, "identity": "operator"/"none", "crop": {...}?, "light_state": {...}?, "redact": {...}?, "luma_target": {"target_mean_luma": 66}?}]}` |
| `~/reel-production/.preflight-ok/<sha16>.json` | preflight | render | the gate token |
| `<work>/...` | render | lumacheck, devicesheet, ornate_env | segments, caption PNGs, beds, eye sheet, `render_report.json` |

Footage masters are resolved under `<FOOTAGE>/masters*/` (`<16-hex-hash>__<stem>.MP4` in `masters/`, plain `<stem>.MP4` in later `masters-*/` folders). The stem is the camera clip name. The kit expects camera-original log masters (e.g. log-profile camera originals; some may carry a display rotation matrix; often untagged full range; 8-bit or 10-bit). **Always cast masters, never proxies.**

---

## 2. The one-to-one kit (`tools/onetoone/`)

The kit takes a **reference reel**, a **brain** (the measured study of that reference: cut grid, caption geometry, caption entry devices — see [06_REFERENCE_FORENSICS.md](06_REFERENCE_FORENSICS.md)), and a **cast** (which of your own masters stands in for each reference shot, at what in-point and crop), and produces an MP4 whose cuts, caption timing, typography and entry animation match the reference frame for frame, on your footage, in the house grade.

### 2.1 `preflight.py` — the gate before every render and before `finished`

Turns the lessons ledger and machine floors into something that can stop a lane. Any failure prints `PREFLIGHT: BLOCKED` with one `✗` line per reason and exits 1; success prints `PREFLIGHT: OK` and, with a cast, writes the gate token `render` requires.

```bash
cd ~/reel-production/tools
<PYTHON> -m onetoone.preflight --row 7 --cast <RENDERS>/reel07-<REFID>/deliver/cast_v003.json
<PYTHON> -m onetoone.preflight --row 7 --finish        # before the lane writes `finished`
<PYTHON> -m onetoone.preflight --row 7 --skip-tests    # debug only
```
Flags: `--row INT`, `--cast PATH`, `--finish`, `--skip-tests`.

Checks, in order:
1. `machine()` — `INTERNAL_MIN_GB, WORKBENCH_MIN_GB, MEM_MIN_PCT = 20, 100, 25`. Internal `/` free ≥ 20 GB; `<WORKDRIVE>` free ≥ 100 GB (decimal GB; **0 if the path does not exist, so a machine without it always blocks**); free memory ≥ 25 % from macOS `memory_pressure` (skipped if the command is missing). Prints `machine: internal N GB · workbench N GB · mem free N%`. This printout is the only disk-floor authority — never `df` (L0011, L0036, L0108).
2. `kit_tests()` — runs `/usr/bin/python3 -m pytest -q -x <tools>/onetoone/tests` (falls back to `sys.executable` if `/usr/bin/python3` is absent), cwd `tools/` (L0051).
3. `cast_rules(cast)` (only with `--cast`): `caption_ink == [255,255,255]` with no `caption_shadow` fails (flat white vanishes on bright beds); `caption_shadow.radius > 20` fails (reads as a glow, not a shadow); `grade_mode` not in `{None, "match", "natural", "house"}` fails.
4. `identity_rules(cast)` (only with `--cast`) — `onetoone.identity.check_cast`; every violation becomes `identity (L0052): ...`.
5. `lessons(row, finish)` — runs `tools/lessons.py harvest`, prints `lessons.py brief [--row]`; with `--finish` runs `lessons.py check [--row]` and blocks while the row has untriaged inbox items.

Output: with `--cast` and no failures, writes `~/reel-production/.preflight-ok/<first 16 hex of sha256(cast bytes)>.json` = `{"cast", "cast_sha256", "lessons_sha256", "row", "at"}`.

Enforces: L0011/L0036/L0108 (floors), L0051 (interpreter), L0052/L0065/L0087 (identity), every lesson that has a kit test (they run in step 2), and the learning loop itself.

Gotchas: not read-only (harvests lessons, writes the token). The token is keyed on cast **bytes** — re-saving with different whitespace invalidates it. It also dies whenever the **lesson set** changes (`render.lessons_sha`); harvester bookkeeping (`seen`, `inbox`) does not count.

### 2.2 `render.py` — the durable 1:1 render path

Cut your masters on the reference's cut grid, grade, rasterise typeset captions with their measured entry devices, composite, encode to ≤ 10 MB, write a full report and an eye sheet. Never uploads, never writes the registry.

```bash
cd ~/reel-production/tools
<PYTHON> -m onetoone.render <brain> <cast.json> <reference.mp4> <out.mp4> <work_dir> --audio <licensed_track.m4a> [--crf 20] [--allow-low-contrast] [--no-gate]
```
`--audio` is required by default (policy `licensed_track_required`; `render()` raises `LicensedAudioRequired` without it). `--reference-audio-rights-held` replaces it with the reference's own audio stream; use it only if you hold the rights to that audio.
`--no-gate` is honoured only if `REEL_NO_GATE=1` is also exported. `--allow-low-contrast` ships even when a caption fails readability; **using it is never the lane's decision — it is a question for the reviewer** (L0023), and it is recorded in `render_report.json.invocation`.

Library entry: `render(brain, cast_json, reference, out_mp4, work, *, size=(1916,1078), crf=20, allow_low_contrast=False, gate=True) -> dict`.

| Constant | Value | Meaning |
|---|---|---|
| `FPS_STR` / `TIMESCALE` | `"24000/1001"` / `24000` | output clock |
| `WEB_CAP` | `10_000_000` bytes | delivery size cap; re-encode ladder crf 22/24/26/28 |
| `CHROMA_JUMP_MAX` | 0.40 | a segment may not gain > 40 % magenta- or green-dominant pixels vs its own master (L0041) |
| `PULL_TOL` / `PULL_TOL_REL` | 6.0 codes / 0.25 | luma-pull tolerance = `min(6, max(1, 0.25·target))` (L0004) |
| `PULL_GAMMA_MIN` | 0.45 | steepest first-pass gamma |
| `PULL_SHOULDER` | 0.94 | 255 maps to ~240 so clipped highlights come off the ceiling (L0003) |
| `PULL_PASS2_GAMMA_MIN` | 0.62 | second pass floors higher (two 0.45 curves compose to ~0.20) (L0005) |
| `PULL_PASS2_OVER` | 5.0 codes | second pass only if the first leaves the shot this far over |
| `HL_KNEE` / `HL_E_MAX` | 0.20 / 14.0 | highlight-only compression knee and max exponent (L0006) |
| `CHROMA_FOLLOW` | 0.4 | natural mode: chroma follows 40 % of the luma drop (L0007) |
| `RAMP_TOL` / `RAMP_TOL_PASS2` / `RAMP_MAX` | 2.5 / 1.5 / 40.0 codes | per-frame shape-correction thresholds and clamp |
| `GATE_DIR` / `GATE_MAX_AGE_S` | `~/reel-production/.preflight-ok` / 21,600 s (6 h) | token dir and life |
| `LESSONS_JSON` | `~/reel-production/lessons.json` | lesson-set hash source |

Pipeline:
1. **Gate** — `require_preflight(cast)`: token exists, < 6 h by mtime, `cast_sha256` matches, `lessons_sha256 == lessons_sha()`; else `PreflightRequired` with the exact command to run. Repeated inside `cut_picture` (importing `cut_picture` directly used to be a bypass), which writes `<work>/.preflight-ok`; `rasterise_captions` refuses without that marker.
2. **`cut_picture(brain, cast, reference, work, size)`** per shot of `cutgrid.json`:
   - `looksheet.resolve_master(stem)` → master (FileNotFoundError if missing).
   - **Framing**: `framing.display_dims` (rotation-aware), `framing.filter_for(slot.crop, mw, mh, aspect)`; `framing.effective_centre` records where a clamped static crop really landed. A moving crop (`zoompan=`) is first rendered alone to a ProRes 422 HQ plate `work/plates/<slot>.mov` (a zoompan inside the full chain exhausted memory; L0010), then treated as a static master.
   - **Grade by `grade_mode`** (default `"match"`):
     - `house` (the approved look; L0018, L0024, L0037, L0102): `format=yuv444p16le,<crop>,` (chroma upsampled **before** any crop — a crop on a subsampled 10-bit master once corrupted chroma to solid magenta) + `housechain.house_head(w, h, LUT, in_range="tv" if plate else "full")` … `HOUSE_TAIL`. No pull, no ramp, no colour wash; only a declared `light_state.gain` (lights-off) survives.
     - `natural`: `lut3d` + `looksheet.NATURAL_GRADE`; exposure pulls kept but re-expressed luma-only via `luma_only()`.
     - `match`: per-shot measured solve, `grade.solve` → `grade.filter_from_solution`, plus pulls and ramp.
   - **Probes** (match/natural): reference and candidate frames at in+1 / mid / out−1 (`grade.sample_frames`), 640×360 (`grade.STAT_SIZE`).
   - **Exposure** (not house): `luma_pull(probe, target)` toward `slot.luma_target.target_mean_luma` or the reference mid-frame mean; a second pass with `gamma_min=0.62` if still > 5 codes over; if that fails, `highlight_pull` (identity below knee 0.20).
   - **Light state**: `light_state_filter(spec)` — whitelisted `colorbalance` keys (`rs gs bs rm gm bm rh gh bh`, each clamped ±0.6) plus `gain` (darken-only `colorchannelmixer`, clamped 0.15–1.0). House/natural drop everything but `gain`.
   - **Redaction**: `redact_filter(spec, n)` — a feathered `boxblur=12:3` patch `{w, h, from:[x,y], to:[x,y], feather:16}` moving linearly across the shot, so no readable identifier (plate, screen, address) ships.
   - **Encode** each segment: `libx264 -preset medium -crf 10 -pix_fmt yuv420p`, tagged bt709/tv, timescale 24000, exactly `n = out − in + 1` frames → `work/segments/<slot>.mp4`.
   - **`chroma_sanity(seg, master, ...)`**: decodes the segment's middle frame and the master's same instant on a plain path, compares magenta/green-dominant pixel fractions (G 50+ codes below both R and B, or above both); raises `RuntimeError("... CHROMA CORRUPTION ...")` on a jump > 0.40 (L0041).
   - **Shape ramp** (match/natural, not with a lights-off gain): per-frame BT.709 mean luma of ours vs reference at 480×270 with caption rectangles cut out of both (`caption_masks`), `luma_ramp` (median-of-5 smoothed per-frame delta, clamped ±40), then a closed loop of up to 4 re-cuts scored on mean |ref−ours| + worst unmatched step; the best candidate (possibly uncorrected) ships.
   - Concat by stream copy → `work/picture_silent.mp4`; mux the **audio track by stream copy, never `-shortest`** (a track that ends before the picture otherwise costs frames). The track is the licensed one passed with `--audio` (default policy `licensed_track_required`); the reference's own audio is muxed only with the explicit opt-in `--reference-audio-rights-held`, and only if you hold the rights to it → `work/picture.mp4`.
3. **`rasterise_captions(...)`**:
   - If the cast has `caption_ink`, every part's ink is overridden with that one colour.
   - Per state: `captions_typeset.typeset_state` → layer + report; `own_ink` collision failures become unreadable entries.
   - Per state, at its mid frame: our bed and the reference frame → `work/beds/<id>_{ours,ref}.png`; the state's shadow from `state_shadow(state, cast_shadow)` (a state may say `"shadow": false` or carry its own spec — L0056); `readability(...)` judged on the shadow-composited bed.
   - Any failure → `UnreadableCaption("caption(s) vanish into our bed — recast the shot: ...")` unless `allow_low_contrast`.
   - Per frame: each live part goes through `devices.layer_for_frame` (measured entry blur/opacity/scale), then `with_shadow` **after** the device (L0002), then `behind_subject` (a luma matte keeping ink only where our bed is brighter than `luma_lo..luma_hi`) → `work/captions/caption-NNN.png` (full-canvas RGBA, one per frame, empty frames included).
   - `with_shadow(layer, spec)` defaults: `radius 12, opacity 0.55, offset (0,2), grow 2`. The directional shadow used in practice is `radius 12, opacity 1.0, offset [4,5], grow 5` (pinned by `test_caption_shadow_gate.py`).
4. **Composite**: `variant_render.composite(picture, work/composite.mp4, width, height, fps, frames, time_base_denominator=24000, captions=<dir>, codec="libx264", crf)` (tv→full, alpha composite, full→tv BT.709, exact frame count, audio copied). If > 10 MB, `variant_render.encode_delivery` at crf 22, 24, 26, 28 until it fits; else copy.
5. **Report**: `work/render_report.json` (invocation, per-shot facts, grade solution, pulls, ramp, chroma sanity, mid-frame luma ours/ref/target, caption device summary, per-part readability) and `eye_sheet(...)` → `work/eye/EYE_SHEET.png` (hook frame + five caption states, reference | ours, half scale).

Enforces in code: L0002, L0003, L0004, L0005, L0006, L0007, L0010, L0018, L0023 (by refusing), L0024, L0025, L0037, L0041, L0056; the gate enforces the whole learning loop.

Gotchas: `render()` prepends `/opt/homebrew/bin` to `PATH` because `variant_render` calls bare `ffmpeg`. `eye_sheet` default state ids are hard-coded from the first reel the kit was built on; on other rows only ids that exist are used, so the sheet may show just the hook. The module docstring still says captions are STATIC — stale; entry devices replay whenever `brain/caption_devices.json` exists. In house mode the LUT used is `looksheet.LUT`, not `housechain.LUT_PACKAGED`; point both at the same file. Readability is judged once per state at its mid frame on the settled layer — entry frames and per-glyph burial are lane-side checks (L0104, L0106, L0109).

### 2.3 `ffx.py` — ONE ffmpeg at a time

The machine-wide ffmpeg mutex.

```bash
<PYTHON> -m onetoone.ffx -- -y -i in.mp4 -vf "select=eq(n\,40)" -vsync 0 -frames:v 1 out.png
<PYTHON> -m onetoone.ffx --probe -- -v error -show_entries stream=width,height -of json in.mp4
```
Library: `from onetoone.ffx import run; run(args, probe=False, capture=False, timeout=None) -> CompletedProcess`.

Constants: `FFMPEG = /opt/homebrew/bin/ffmpeg`, `FFPROBE = /opt/homebrew/bin/ffprobe`, `LOCK = /tmp/reel-one-ffmpeg.lock`. ffmpeg takes `fcntl.LOCK_EX` (blocking, no timeout); ffprobe runs unlocked. Returns ffmpeg's exit code; does not raise.

Gotchas: the lock is advisory — only callers that use `ffx` (or `render._run`) respect it. A hung ffmpeg holds the lock until killed (no timeout unless the caller passes one).

### 2.4 `housechain.py` — the approved colour pipe (L0037)

"House grade" is the hash-locked LC-709 LUT and nothing else (L0018), but it only reproduces approved pixels when run through the same pipe.

- `house_head(w, h, lut=LUT_PACKAGED, cover=True, in_range="full")` → `scale=w:h:force_original_aspect_ratio=increase:flags=lanczos,crop=w:h,scale=iw:ih:flags=lanczos:in_range=<full|tv>:out_range=full:in_color_matrix=bt709,format=gbrpf32le,lut3d=file='<lut>':interp=tetrahedral,format=gbrp16le`.
- `HOUSE_TAIL = "colorspace=ispace=gbr:iprimaries=bt709:itrc=bt709:irange=pc:all=bt709:range=tv:format=yuv420p:dither=fsb"`.
- `house_vf(w, h, lut, cover, fps)` = head + tail (+ `fps=`) + `setsar=1,format=yuv420p` — the one-shot `-vf` outside `render`.
- `is_approved_chain(vf) -> list[str]` returns the missing tokens from `REQUIRED_TOKENS = ("in_range=full", "in_color_matrix=bt709", "format=gbrpf32le", "interp=tetrahedral", "format=gbrp16le", "colorspace=", "range=tv", "all=bt709")`; empty list = approved.
- `LUT_PACKAGED = ~/reel-production/_reelctl/src/reelctl/data/luts/Sony-LC-709-official.cube`.

Measured facts recorded in the docstring: the house chain reproduces an approved frame to a mean RGB difference of 0.03; a bare `lut3d` chain is −4.2 R / +3.3 B off (swscale assumes BT.601 on untagged masters). A ProRes zoom plate measures tv range, hence `in_range="tv"` for plates.

**The LUT file is not shipped in this repository.** Obtain the LC-709 conversion LUT from your camera vendor (or use the LUT matching your camera's log profile), place it, and point both `housechain.LUT_PACKAGED` and `looksheet.LUT` at it. Record its sha256 — the house look is defined by that hash.

### 2.5 `looksheet.py` — look sheet before a full render, plus shared constants

For every caption state, reference frame beside our candidate frame (cast master, LUT + per-shot mood eq, typeset caption composited, readability-checked), plus grade-only tiles. Built before a full render so the look is approved on stills.

```bash
<PYTHON> -m onetoone.looksheet <brain> <cast.json> <reference.mp4> <outdir>
```
Writes `<outdir>/LOOKSHEET.png`, `looksheet_report.json`, `cap_<id>_{ref,cand,ours}.png`, `grade_<slot>_*.png`. `build(..., grade_slots=("S01","S03","S06"))`.

Shared constants other modules import from here: `FFMPEG = /opt/homebrew/bin/ffmpeg`; `FPS = 24000/1001`; `FOOTAGE = <WORKDRIVE>/workbench/footage-library`; `LUT = $REEL_FACTORY_LUT` (default: the packaged path `reelctl/src/reelctl/data/luts/Sony-LC-709-official.cube`, which you download yourself); `NATURAL_GRADE = "curves=all='0/0.045 0.25/0.31 0.75/0.80 1/1',eq=contrast=1.06:saturation=1.22:gamma=1.05"`; `COVER = "scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"`.
- `resolve_master(stem)` tries `masters/*__<stem>.MP4|mp4`, `masters-*/<stem>.MP4|mp4`, `masters-*/*__<stem>.MP4`; first sorted hit wins; `None` if absent.
- `frame_stats(png) -> (mean luma, mean saturation, R−B warmth)` on a 320×180 downsample.
- `mood_eq(cand, ref)` — clamped `eq`: brightness `0.70·(ref−cand)/255` clamped −0.22…+0.18; saturation `1 + 0.70·(ref/cand − 1)` clamped 0.80…1.50; contrast 1.03; gamma 0.95 only when the reference is > 25 codes darker. Also `grade.py`'s safe fallback.

Gotchas: bare `subprocess.run([FFMPEG ...])` (bypasses the ffx lock). The look sheet is a natural-grade preview; for a house-mode row its colours are not what ships.

### 2.6 `grade.py` — per-shot measured grade for `grade_mode: "match"`

Matches the reference's tone and colour cast per shot without going garish. Separates **tone** (one pchip luma curve, `tone_points`), **cast** (per-channel gains from the chromaticity of lit pixels, `cast_gains` — warm/cool free, green/magenta tightly bounded, luma-preserving) and **saturation** (one multiplier after curve and gain, `saturation_factor`). `solve()` simulates each candidate in numpy (verified to ~1 code vs ffmpeg), checks guards, and walks a strength grid down until everything passes; if nothing passes it returns `mode: "fallback"` and `filter_from_solution` emits `looksheet.mood_eq`.

```bash
<PYTHON> -m onetoone.grade <brain> <cast.json> <reference.mp4> <workdir> <out.json>   # measure only
```

| Group | Values |
|---|---|
| Measurement | `LEVELS = (1,10,50,90,99)` percentiles; `STAT_SIZE = (640,360)`; `HUE_BINS = 12`; `V_FLOOR = 0.10` |
| Aim | `ALPHA_TONE 0.75`; `ALPHA_LEVEL {1:0.85, 10:0.75, 50:0.70, 90:0.45, 99:0.35}`; `ALPHA_CAST 0.70`; `ALPHA_SAT 0.65` |
| Clamps | `CAST_CLAMP 0.34`; `CAST_TINT_MAX 0.06`; `TONE_CLAMP {1:0.060, 10:0.160, 50:0.220, 90:0.120, 99:0.050}`; `MIN_STEP 0.004` |
| Skin guard | `SKIN_HUE_RANGE (5°, 50°)`; `SKIN_HUE_SHIFT_MAX 18°`; `SKIN_SAT_HARD 0.65`; `SKIN_SAT_RISE_MAX 0.22`; `SKIN_MIN_FRACTION 0.02` |
| Clip/crush | `SPECULAR_V 0.90`; `SHADOW_V 0.10`; `CLIP_RISE_MAX 0.020`; `CRUSH_RISE_MAX 0.060` |
| Banding | `MIN_SLOPE 0.12` inside p10..p90; `MAX_CODE_STEP 2`; `BAND_STEPPED_RISE 0.02`; `BAND_STEPPED_FLOOR 0.04`; `MAX_SEG_SLOPE 2.2` |
| Search | `STRENGTHS (1.0, 0.85, 0.7, 0.55, 0.4, 0.25)`; tone also tries 0.0; strongest first, ties prefer tone |

Key functions: `sample_frames`, `extract_reference`, `extract_candidate` (LUT + cover + 640×360, via ffx), `shot_stats`, `solve`, `filter_from_solution` (→ `format=gbrp10le,curves=interp=pchip:all='...',colorchannelmixer=...`), `shot_filter`, `explain`, `banding_report`, `banding_delta`, `check_guards`.

Lessons: L0001/L0014 narrow its scope; L0018/L0024/L0102 make `house` (not `match`) the default look. Use `match` only when a reference measurably needs a named block move. Applied after `lut3d`, before the cover crop. Guards measure probe frames, not every frame.

### 2.7 `grades.py` — clip grades as casting law (L0065)

Reads the append-only ledger the Deck grader writes: `~/reel-production/_receipts/clip-grades/grades.jsonl`, one JSON per line `{"at", "stem", "clip_id", "grade": HERO|BROLL|NEVER|null, "ident": ME|NOTME|null, "t0"?, "t1"?, "by", "via"}`. Latest line per field wins; a null never erases. Lines with numeric `t0 < t1` are **segment** grades (the grader works in 5-second segments).

Law: NEVER = full ban (whole clip) or ban of that window (segment). HERO or ME = the reviewer ruled the clip shows the on-camera subject → counts as settled for identity slots; HERO preferred wherever the reference shows a person. BROLL or NOTME = never on an identity slot.

Functions: `load_grades(path=None)` (missing file = empty law), `never_stems`, `hero_stems`, `hero_windows(stem)`, `window_grade(stem, t0, t1)`, `never_reason`, `operator_stems`, `not_operator_stems`, `broll_stems`, `effective_settled(pool_settled, grades)` = settled ∪ HERO/ME − NEVER − NOTME, `identity_slot_reason(stem, grades, t0, t1)`, `summary()`.

```bash
<PYTHON> -m onetoone.grades   # prints {"graded","hero","broll","never","segments_graded","me","notme","path"}
```
Constants: `GRADES_PATH`, `GRADE_VALUES = {"HERO","BROLL","NEVER"}`, `IDENT_VALUES = {"ME","NOTME"}`. Nothing here writes. The effective pool is pool + grades evaluated live, which is why a cast is re-checked before every render (L0087). A segment-graded clip used with no in-point counts as banned. Only the in-point (and optional out-point) is checked; L0090 asks lanes to check the whole played window.

### 2.8 `identity.py` + `identity_pool.json` — identity and blacklist as code (L0052)

Stops the factory casting the wrong person or a banned clip. `identity_pool.json` (schema 1) ships **empty** in this repo; fill it for your own library:

| Key | Meaning |
|---|---|
| `settled_pool` / `settled_pool_sets` | stems confirmed to show the on-camera subject, optionally grouped into look sets |
| `operator_confirmed` | confirmed but not yet castable |
| `not_operator` | shows someone else |
| `blacklist_full` | banned clips, each with `match_keys` (stem, file names, content ids, sha256, cloud ids) |
| `blacklist_spans` | span-restricted masters with `banned_windows_s` / `allowed_in_point_ranges_s` |
| `unruled` | awaiting a ruling |
| `sources` | where each ruling came from |

Law (`check_cast`):
- Every slot declares `"identity": "operator"` or one of `{"none", "no-person", "person-free", "nobody"}` (or is listed in top-level `identity_slots`). Undeclared fails.
- Every slot is checked against `blacklist_full` (any identifier form, via `normalise()` which strips the `<hash>__` prefix and `.MP4/.MOV/.MXF/.M4V`) and `blacklist_spans` (a span-restricted master with no in-point counts as banned), then against grades NEVER.
- An identity slot must use a stem in `effective_settled` and pass `grades.identity_slot_reason`.
- `refpeople_rules`: if `<row>/brain/refpeople.json` exists, a slot whose reference window shows a person must be declared identity operator (L0058, L0065); a non-HERO stem there only warns. **If the file is missing it warns and passes.**

Accepted slot aliases: stem from `stem|master|path|file|source`; in-point from `in_s|inpoint|inpt|t0`; duration from `dur_s|len|slot_seconds|duration` or `out_s`.

```bash
cd ~/reel-production/tools && <PYTHON> -m onetoone.identity <cast.json>   # "identity: OK" or violations, exit 1
```
Library: `load_pool`, `is_blacklisted(stem, t0, t1)`, `blacklist_reason`, `check_cast`, `assert_cast_identity` (raises `IdentityViolation`). The pool path is resolved next to the module. Change any list only on an explicit human ruling by clip, and update `tests/test_identity_pool.py` in the same change.

### 2.9 `refpeople.py` — where does the reference show a person (L0065)

Runs the macOS Vision face detector over the reference so casting knows which shots need the on-camera subject.

```bash
cd ~/reel-production/tools && <PYTHON> -m onetoone.refpeople <row dir> [--ref ref/ref24.mp4] [--step 4]
```
Extracts every 4th frame at 640 px wide, runs `VNDetectFaceRectanglesRequest`, writes `<row>/brain/refpeople.json` = `{"ref","fps","frames","step","min_face_h","min_fraction","faces":{"0":[0.21],"4":[],...},"method"}`. `window_verdict(data, f0, f1)` → person when a face ≥ `MIN_FACE_H = 0.06` of frame height is on ≥ `MIN_FRACTION = 1/3` of sampled frames; windows shorter than one step borrow the nearest sample.

Gotchas: macOS only (pyobjc Vision). Face-only — a silhouette, a back or a small figure is missed, so an eye check is mandatory before declaring a slot `none` (L0110). Bare ffmpeg/ffprobe (no lock).

### 2.10 `castscan.py` — rank masters by the caption bed they give

For one slot, scores candidate master windows by how well the bed under each caption ink box matches the reference's bed (contrast lost + clutter added), filtered and ordered by identity law and grades.

```bash
<PYTHON> -m onetoone.castscan <brain> <cast.json> <reference.mp4> <SLOT> [--stems a,b,c] [--times 1,3,6] [--top 20] [--out DIR] [--identity auto|yes|no]
```
Default `--out` = `<brain>/../castscan-<SLOT>`; default candidates = **every** master (slow and disk-hungry — always pass `--stems` with a shortlist and delete the PNG dump afterwards, L0088). `--identity auto` reads `brain/refpeople.json`. Score per candidate/in-point: `Σ max(0, ref_c − c)/max(ref_c,1) + max(0, sd − ref_sd)/50` over all ink boxes; lower is better. NEVER-segment times are skipped; identity slots scan only inside HERO segments. Writes `<out>/castscan_<SLOT>.json`.

Gotchas: `cast.json` is accepted but not used. Scans with plain `lut3d`, not the house pipe (beds read slightly cooler/darker than what ships). Bare ffmpeg (no lock).

### 2.11 `bedprobe.py` — will this window carry these captions?

Probe one candidate window with the render's own readability gate before re-rendering.

```bash
<PYTHON> -m onetoone.bedprobe <brain> <work_with_beds> S12 CLIP_0060 1.92 --crop '{"zoom":1.3,"cx":0.62,"cy":0.4}' --target 66 [--states c07a,c07d] [--png out.png]
```
Needs `<work>/beds/<id>_ref.png` from an earlier render. Pipeline: crop → `lut3d` → cover → optional `render.luma_pull` to `--target` → typeset layer → `readability`. Prints `contrast x/need  bed sd y/ref z  OK|FAIL` per part and `=> PASS|FAIL`; exit 0 iff all pass. Uses ffx. Gotcha: judges flat ink with **no shadow**, through plain LUT, not the house pipe.

### 2.12 `framing.py` — per-slot crop, drift and push

Turns a slot's `crop` spec into an ffmpeg filter on the decoded (rotation-applied) master, before the LUT. Spec: `{"zoom": 1.25, "cx": 0.5, "cy": 0.45, "drift": {"dx": 1.5, "dy": 0.0}, "push": 0.0004}` or `{"keys": [[n, zoom, cx, cy], ...]}`.
- Static/drift → `crop=w='2*floor(cw/2)':h='2*floor(ch/2)':x='clip(...)':y='clip(...)':exact=1` (drift in master px per frame).
- `push` or `keys` → `_zoompan_for` → `zoompan=z='max(1,...)':x=...:y=...:d=1:s=1916x1078:fps=24000/1001` (a crop evaluates w/h once, so a crop-based push never zooms — L0010). **Raises `ValueError` on a portrait master** (aspect mismatch > 0.02).
- `effective_centre(spec, w, h)` → `clamped`, `window_px`, `upscale`. `display_dims(master)` reads rotation side data via `ffx --probe`.

```bash
<PYTHON> -m onetoone.framing '{"zoom":1.25,"cx":0.5,"cy":0.45,"drift":{"dx":1.5}}'   # prints the filter
```
L0113/L0114 (headroom ≥ 6 %; `cy` larger = picture moves down) are lane rules, not enforced here.

### 2.13 `motion.py` — camera motion as a similarity transform (L0010)

```bash
<PYTHON> -m onetoone.motion <video> <frame_a> <frame_b>
```
Shi-Tomasi corners (800, quality 0.01, min distance 12) + pyramidal Lucas-Kanade (31×31, 4 levels) + `estimateAffinePartial2D` RANSAC (2.0 px) → `{"scale_pct","rot_deg","centre_shift_px","inliers","tracked","fit_err_px"}`. Exists because phase correlation read a +2.6 % push-in as a −15.9 px pan. `track_roi(video, n_frames, roi, scratch)` integrates a subject's scale and centre frame to frame (to hold a subject steady with crop keys); it is defined after the `__main__` block. Scratch: `/tmp/onetoone-motion/<stem>/`.

### 2.14 `captions_typeset.py` — typeset, never trace

Draws each caption part in a real font so its ink fills the reference's measured box. Ornate script drawn first (behind), plain line last (on top). Solid ink, no glow.

Faces: `DEFAULT_FACES` = plain `("/System/Library/Fonts/HelveticaNeue.ttc", 1)` (Bold — TTC index matters; a condensed index once shipped and read too narrow) and ornate `("~/Library/Fonts/PinyonScript-Regular.ttf", 0)` (an open SIL OFL script face that won a 26-face coverage bake-off: mean ink-coverage error 13.3 % vs 17.6 % for the runner-up). A part may name its own face: `"face_file": ["~/Library/Fonts/X.ttf", 0]` wins (L0055). `_PART_FACE` maps `plain_line|plain → plain`, `ornate_word|ornate → ornate`.

Fitting order in `typeset_part`:
1. `_typeset_ref_fit` if the part carries `ref_fit`: a two-run script fit (`capital` + `tail`, from `refit.solve_script`) is always used; a single-run fit only if `score ≥ REF_FIT_MIN_SCORE = 0.6`; ramp lines use `refit.draw_ramped`; `stretch ≠ 1` squeezes about the ink's left edge.
2. Otherwise box fitting: plain → `fit_font_to_box` (largest size whose ink fits, binary search 8–600 px), left-anchored, vertically centred; ornate → `fit_script` (height first, horizontal factor clamped `SCRIPT_SQUEEZE_MIN 0.90`…`SCRIPT_STRETCH_MAX 1.15`, overflow ≤ `SCRIPT_OVERFLOW 1.08`), horizontally centred, bottom-anchored when the plain line is above, nudged by `ref_ink_env` when present.
3. Ink: `ink_rgb`, else `ink_rgb_approx`, else white.

Gates in this module:
- `own_ink_collision(part_layers)`: share of plain-line ink within 2 px of script ink ≤ `MAX_OWN_INK_OVERLAP = 0.22`.
- `readability(bed, report, state, ref_bed=, ink_layer=, shaded_bed=)` per part:
  - contrast (no shadow): Rec.601 luma `|ink − bed mean in ink box| ≥ max(MIN_CONTRAST_ABS 25, MIN_CONTRAST_RATIO 0.6 × reference contrast)`;
  - contrast (with shadow): WCAG ratio of ink vs the 4–8 px band outside the glyphs on the shadow-composited bed ≥ `HALO_MIN_RATIO = 3.0` (L0025, L0030, L0033);
  - clutter: fails when bed luma stddev > `MAX_CLUTTER_RATIO 2.0 ×` the reference's and > `MIN_CLUTTER_ABS 25`.

```bash
<PYTHON> -m onetoone.captions_typeset <brain>/captions.plaintext.json <outdir>
```
Gotchas: parts are looked up **by text** in `readability` and `castscan` — two parts with the same text in one state are judged with the last one's ink (L0066; pinned by `test_part_text_unique.py`). Pillow without Raqm cannot reach OpenType swash alternates (`swsh`, `salt`). Box fitting caps at 600 px; giant script words need `ref_script_metrics` (L0077). The ornate default path is absolute.

### 2.15 `refit.py` — fit typeset ink to the reference's pixels (L0016)

Chooses typesetting parameters (size, tracking, horizontal stretch, word-space factor, position, or a ramp) so our ink mask correlates best with the reference's ink mask at the settled frame. Nothing here traces.

```bash
<PYTHON> -m onetoone.refit <brain> <ref_frames_dir>   # frames named NNNN.png (4 digits)
```
Reference mask = pixels within `INK_TOL = 70` RGB of the ink in a window padded by `PAD = 60` (+ box/4 horizontally, + box/2 vertically), Gaussian σ 1.2. Candidate grid — plain: size ×0.62–1.60 step 0.03, tracking −0.20…+0.04 em, stretch 0.88/0.94/1.0, space_k 1.0/0.85/0.7/0.55 when the text has spaces; ornate: stretch 0.84–1.30. Scored with `cv2.matchTemplate(TM_CCOEFF_NORMED)`. Plain lines scoring < 0.7 with ≥ 2 spaces also try `fit_ramp`. Ornate parts fit only from `ref_script_metrics` via `solve_script` (two runs: capital sized by its own height, stretch 0.9–1.35; tail sized by x-height, tracking −0.03…+0.30 em, stretch 0.88–1.0). **Never regress**: a re-fit scoring lower than the stored fit keeps the stored one. Writes `ref_fit` back into `captions.plaintext.json` **in place, no backup**.

Also: `face_sweep_slugs(names)` (L0049: collision-proof filenames for face-family sweeps; raises on collision), `tracked_layout`, `draw_tracked`, `draw_ramped`, `render_mask`, `ref_mask`, `fit_part`.

Gotchas: `MIN_SCORE = 0.45` is declared but unused (the live threshold is `REF_FIT_MIN_SCORE = 0.6`). Frame naming `NNNN.png` differs from `measure_devices`/`refstrip`/`devicesheet` (`ref-NNN.png`). Always fits with `DEFAULT_FACES`, ignoring `face_file` — use `faceid fit` for identified faces. Lessons: L0016, L0028, L0048, L0049, L0077, L0082, L0116.

### 2.16 `faceid.py` — identify, locate, fit, or recreate a reference typeface (L0055, L0064, L0067)

A face is identified from the reference's pixels, never assumed.

```bash
<PYTHON> -m onetoone.faceid ink-crop --frame ref/0151.png --box 566,100,1400,490 --ink 15,20,46 [--minus x0,y0,x1,y1] -o work/face/line.png
<PYTHON> -m onetoone.faceid identify work/face/line.png        # prints a manual instruction: upload the crop to a font-ID service yourself
<PYTHON> -m onetoone.faceid find "Edwardian"                   # search your installed, licensed fonts
<PYTHON> -m onetoone.faceid fit --frame ref/0151.png --box ... --ink ... --text "<word A>" --face <licensed-font.ttf>[,idx] -o work/face/fit.jpg
<PYTHON> -m onetoone.faceid trace --frame <ref frame> --box ... --ink ... --text "<line A>" --out work/fonts/RefTrace-Medium.ttf [--name "RefTrace Medium"] [--minus ...] [--replace]
<PYTHON> -m onetoone.faceid trace --need "ALL THE CAPTION TEXT" --out work/fonts/RefTrace-Medium.ttf   # lists missing glyphs, exit 3 if any
```
- `ink_mask`: dark ink (mean < 110) keyed on luma `(150 − mean)/80`; light ink on min channel `(min − 170)/60`; neighbouring line boxes subtracted (`--minus`).
- `fit`: size ×0.80–1.44 step 0.02, tracking −0.10…+0.02 em; best `TM_CCOEFF_NORMED`; writes a 2× overlay (our ink in red over the reference) and prints the `ref_fit` JSON to paste onto the part. **The proof is reading the overlay**, not the score.
- `trace`: segments glyphs by column gaps, contours at `TRACE_UP = 4` upscale, builds a TrueType with fontTools (`UPM 1000`, `CAP 700`), keeps a `<out>.glyphs.json` sidecar (accumulates across calls) and writes `<out>.sheet.png`. Method refinements: pad masks ≥ 8 px, one pixel scale per face, trace from an 8th-percentile temporal stack at half contrast, check at 300 px (L0085, L0097, L0098, L0112). A traced closed letter must keep its counters (O/D one, B two) — `tests/test_built_face_counters.py` checks every traced `Ref*-*.ttf` in `$REEL_FACTORY_TRACED_FONTS` (skips when unset) (L0067).

**Licensing.** `trace` produces a letterform approximation for matching and analysis. For anything you publish, identify the original face and license it, or choose an open-licensed face that matches; do not redistribute traced recreations of commercial typefaces. See the Rights section of the [README](../README.md).

No web automation: `identify` never drives a website; it prints the manual step (upload a crop to a font-ID service yourself, under that service's terms). `find` searches local font directories (system, user, `REEL_FACTORY_FONT_DIRS`) and earlier intakes under `REEL_HOME`/`$REEL_FACTORY_WORKBENCH`.

### 2.17 `ornate_env.py` — reference ornate ink envelope

Measures the 5th/95th-percentile ink rows (`P_LO 0.05`, `P_HI 0.95`, ink within `INK_TOL 70`) of each ornate word inside its box minus the plain line's box, so `captions_typeset` sizes and places the script body where the reference's sits.

```bash
<PYTHON> -m onetoone.ornate_env <brain> <look_dir>   # look_dir holds {sid}_{mid_frame}_ref.png (e.g. render's work/eye/)
```
Writes `ref_ink_env` and `env_mask` onto each ornate part **in place** (indent 2; `refit` writes indent 1 — expect whitespace churn in diffs). Helpers `env_mask`, `row_envelope`, `ref_ink_rows`, `layer_ink_rows` are imported by `captions_typeset`.

### 2.18 `measure_devices.py` — measure caption entry devices from the reference

Reads the reference's frames and writes per-frame entry curves (onset, sharp frame, blur σx/σy, opacity, scale, drift, per-word reveal) to `brain/caption_devices.json`.

```bash
<PYTHON> -m onetoone.measure_devices <brain> <refframes_dir> [-o caption_devices.json] [--frames N]
```
`refframes_dir` holds `ref-NNN.png` (3-digit, zero-based). `--frames` defaults to counting those files (a fixed default once left later states unmeasured; L0074). Write to a candidate file first, sanity-check, then save.

Method: normalised stroke-scale residual (grey opening/closing, `TOPHAT = 71`) → template from the settled frame cut to the study box → per-frame `matchTemplate` over a scale grid (`COARSE 0.60…1.36 step 0.04`, fine ±0.06 step 0.005, template blurred `COARSE_BLUR 4.0` / `FINE_BLUR 2.0`), accepted only if the ink sits on the box (`MIN_CONTAIN 0.60`) and scale is in `SCALE_BAND (0.80, 1.30)` → blur fitted over `SIGMA_GRID 0…30 px` separately in x and y → opacity from coverage → onset by changepoint (`ONSET_ALPHA 0.22`, `ONSET_FIT 0.55`, gain ≥ 0.10), sharp when σ ≤ `SHARP_SIGMA 1.2` and α ≥ `SHARP_ALPHA 0.60` → 3-frame median on scale/dx/dy. Each part gets `confidence` high (median fit ≥ 0.6) / medium (≥ 0.4) / low; parts that never clear the floors are written static with a reason.

Gotchas: `EYE_ONSETS` and `swaps()` hard-code eye-read onsets and in-place replacement pairs keyed by state id and text from the first reel the kit was built on — they silently select nothing elsewhere, or apply if a later reel reuses the same id and text; clear them for your project. Any curve frame with fit < 0.7 and σ > 3 must be eye-checked against the reference and, if the reference is sharp, overridden with a key starting `override` (L0084; `tests/test_low_fit_blur_overridden.py`). Tune devices with an offline per-glyph simulation before rendering (L0111).

### 2.19 `devices.py` — replay the measured entry devices

`layer_for_frame(state, part, n, sharp_layer, devices) -> RGBA` returns the part's layer as the reference has it on frame n: geometry (scale about the anchor, then drift) → anisotropic Gaussian blur on **premultiplied** alpha (no dark fringe) → opacity multiply. Before `first_frame` the part is invisible; after the last measured frame it holds the settled layer; no device = static settled layer. Word-reveal lines are split at the widest interior alpha gaps (`_alpha_columns`, low = < 2 % of the column-sum peak) and each word gets its own opacity/blur. When a part was typeset from `ref_fit`, the anchor is our settled ink's top-left, not the study box. Constants `DEVICES_FILE = "caption_devices.json"`, `SETTLED_EPS = 1e-3`. Also `load_devices`, `device_for`, `frame_row`, `transform`, `blur`, `fade`, `summary`. Library only. Enforces L0002 (device before shadow).

### 2.20 `devicesheet.py`, `refstrip.py` — eyeball sheets for devices

```bash
<PYTHON> -m onetoone.devicesheet <brain> <refframes> <ourframes> <captions_dir> <outdir> [c01 c02 ...]
<PYTHON> -m onetoone.refstrip <refframes> <brain> <state_id> <part_index> <n0> <n1> <out.png>
```
`devicesheet` tiles reference | ours (our frame + caption PNG) over a state's entry window, one row per frame (first 11 frames, then every 3rd if longer than 15). Expects `ref-NNN.png`, `our-NNN.png`, `caption-NNN.png`. `DEFAULT_IDS` are from the first reel; pass ids explicitly. `refstrip` tiles the padded (60 px) settled bbox of one part across frames n0..n1 (6 columns, half scale) so the device can be read by eye before anything is fitted.

### 2.21 `sharpness.py`, `yuvexact.py` — measure before acting on "soft" (L0038)

- `sharpness.edge_width_px(frame, roi, mask=None)`: ink = `min(RGB) ≥ INK_T 235`; boundary pixels with contrast > 25; median `0.8·C/Gpk` (C = ink luma − 7×7 min bed, Gpk = 5×5 max Sobel). Needs ≥ 200 ink and ≥ 50 boundary pixels, else `None`. `is_sharp` = width ≤ `EDGE_WIDTH_MAX_PX = 2.0`. Calibration: a hard 1-px step reads 1.6 px; approved deliveries 1.6–1.8; VP9-encoded reference text ~2.45.
- `yuvexact.yuv420_to_rgb(buf, w, h)` / `rgb_to_yuv420(rgb)`: exact BT.709 limited-range conversion (`KR 0.2126`, `KB 0.0722`). swscale's `rgb24` reads ~2 levels dark; compositing on swscale RGB and re-encoding bakes that −2 in.

Libraries only.

### 2.22 `lumacheck.py` — delivered cut vs reference, per shot

```bash
<PYTHON> -m onetoone.lumacheck <brain> <reference.mp4> <ours.mp4> <work_dir>
```
Two views — BED (caption rectangles cut from both via `render.caption_masks`) and WHOLE — of BT.709 mean luma per frame; per shot: mean difference, worst frame, worst unmatched frame-to-frame step. Writes `<work>/luma_per_frame.json`. Used in audits and fix proofs.

### 2.23 `registry_row.py` — one safe registry write

```bash
<PYTHON> -m onetoone.registry_row ~/reel-production/REEL_REGISTRY.json 7 patch.json --tag row07v003
# patch.json = {"set": {"version": "v011", "review_state": "awaiting_review"}, "add": {"v011": {...}}}
```
Takes `flock` on `<registry>.lock`, copies a backup `<stem>.backup-<UTCstamp>-<tag>.json`, applies `set` (overwrite) and `add` (append-only: refuses an existing key), stamps `updated_at_utc`, writes via `.tmp` + `os.replace`. Exactly one row with that `sequence` must exist. Every call leaves a full backup copy — prune periodically.

### 2.24 `tests/` — what each kit test pins

```bash
cd ~/reel-production/tools && <PYTHON> -m pytest -q onetoone/tests     # preflight runs the same with -x
```
`conftest.py` only puts `tools/` on `sys.path`. Several tests read real rows or masters and **skip** when absent; a skip is not proof.

| File | Pins | Lessons | Needs real data? |
|---|---|---|---|
| `test_lessons_grade.py` | pull curve shoulder 0.94; tolerance scales with target; pass-2 floor ≥ 0.6; render refuses without a fresh token; gate inside `cut_picture`/`rasterise_captions`; `lessons_sha` ignores bookkeeping; readability counts the shadow the viewer sees; `effective_centre` reports a clamped crop | L0003, L0004, L0005, L0025, L0030 | no |
| `test_v011_fixes.py` | own-ink collision; tracking/space factor; ramped line; `light_state` whitelist/clamp; moving redaction; `behind_subject`; darken-only gain; push/keys use zoompan; shadow from the device-drawn frame; highlight pull spares the subject | L0002, L0006, L0010 | no |
| `test_caption_shadow_gate.py` | flat white on a bright bed fails the halo gate; the directional shadow (radius 12, opacity 1.0, offset [4,5], grow 5) passes and stays inside the cast rule; a weak symmetric glow still fails | L0025, L0030, L0033 | no |
| `test_house_chain_and_sharpness.py` | house chain carries the contract, bare `lut3d` does not; house chain reproduces an approved frame; sharpness floor; exact YUV round trip, a −2 offset fails | L0030, L0037, L0038 | one approved delivery + master (skips) |
| `test_house_pipe_render.py` | head+tail = approved chain; ProRes plate enters as tv; render's house branch uses the pipe; tail precedes redaction and final format; `chroma_fractions` flags a magenta flood; `cut_picture` guards every segment; a real crop on a subsampled 10-bit master renders sane RGB | L0037, L0041 | one master (skips) |
| `test_grade.py` | monotonic curves; slope cap; zero strength = identity; dark reference never lifts; `mood_eq` fallback never brightens against a dark reference; unreliable measurement → fallback; cast on an illuminant axis, luma-preserving; saturation caps; emitted filter parses and matches the numpy simulation; banding probes | grade doctrine | no (runs ffmpeg) |
| `test_grades_law.py` | latest-wins, null never erases; grades as identity law; refpeople verdict + preflight rule; castscan `allowed_stems`; segment grades; `measure_devices` counts frames | L0065, L0074 | no |
| `test_identity_pool.py` | pool loads; banned stems rejected in every identifier form; settled stem accepted; out-of-pool and unruled stems rejected; undeclared identity fails; span bans; preflight runs identity only with a cast | L0052, L0087 | **asserts specific stems — rewrite for your pool** |
| `test_font.py` | script face exists and loads; ornate stays in its box and does not swallow the plain line | — | a reel brain (skips) |
| `test_face_sweep_naming.py` | slug collisions rejected | L0049 | no |
| `test_preflight_kit_tests_interpreter.py` | `kit_tests()` uses an interpreter with pytest, not hard-coded `sys.executable` | L0051 | `/usr/bin/python3` with pytest |
| `test_named_face_and_state_shadow.py` | `face_file` honoured; state shadow default = cast spec; `"shadow": false` wears none; per-state spec | L0055, L0056 | no |
| `test_faceid_trace.py` | traced face sets the source glyphs; `--need` reports missing; wrong text reports a segmentation error instead of guessing | L0064 | macOS Helvetica (skips) |
| `test_built_face_counters.py` | every built `Ref*` face keeps closed counters | L0067 | built faces (skips if none) |
| `test_part_text_unique.py` | documents the by-text collision; a brain has no repeated part text in a state | L0066 | a reel brain (skips) |
| `test_giant_script_two_run.py` | a two-run script fit renders past the 600 px cap | L0077 | a system script face (skips) |
| `test_footage_filled_ink_not_flat.py` | a part with `ref_fit.score < 0.6` may not ship as flat ink without `ink_fill` or `flat_ink_checked` | L0089 | a reel brain (skips) |
| `test_low_fit_blur_overridden.py` | a device frame with fit < 0.7 and σ > 3 needs an `override*` key | L0084 | a reel brain (skips) |

Note: the shipped `identity_pool.json` is empty, so `test_identity_pool.py` fails until you populate the pool and rewrite its fixtures for your library. Data-dependent tests carry historical file names; repoint or skip them.

### 2.25 Lesson ↔ module index

| Module | Lessons enforced in code |
|---|---|
| preflight | L0011, L0036, L0108 (floors); L0051; runs L0052/L0065/L0087 via identity; runs every tested lesson via pytest; learning-loop gate |
| render | L0002, L0003, L0004, L0005, L0006, L0007, L0010, L0018, L0023 (refusal), L0024, L0025, L0037, L0041, L0056 |
| housechain | L0018, L0037, L0102 |
| captions_typeset | L0016 (uses `ref_fit`), L0025, L0030, L0033, L0055, L0077; L0066 is a known limitation |
| refit | L0016, L0028, L0048, L0049, L0077 |
| faceid | L0055, L0064, L0067 (via test), L0085, L0097, L0098 (method) |
| identity / grades / refpeople / castscan | L0052, L0058, L0065, L0087, L0088 (usage), L0110 (limit) |
| measure_devices / devices | L0002, L0074, L0084 (via test), L0111 (method) |
| framing / motion | L0010; L0113, L0114 lane-side |
| sharpness / yuvexact | L0038 |
| ffx | the one-ffmpeg rule (L0045/L0100 lane-side) |
| registry_row | flock + backup registry writes |

---

## 3. End-to-end call graph of a one-to-one build

The lane driving this is a Claude Code session started by `orderd` (§5) with the standing rules from `orderd/lane_prompt.py`: INTAKE → BUILD → ONE independent AUDIT → FIX every finding → DELIVER. The kit supplies the steps; the lane writes the study and the cast between them.

```
INTAKE (row dir <RENDERS>/reel<NN>-<REFID>-<date>/)
  ref/ref24.mp4 ── frames ──▶ work/refframes24/ref-NNN.png   (onetoone.ffx -- -i ref24.mp4 ... ref-%03d.png)
  brain/cutgrid.json, brain/captions.plaintext.json           (written by the lane: cuts, states, bbox_settled, ink_rgb ...)
  │
  ├─ onetoone.refpeople <row>                      → brain/refpeople.json          (Vision faces; L0065)
  ├─ onetoone.faceid ink-crop/identify/find/fit | trace
  │                                                → install your licensed font; part.face_file + ref_fit (pasted)
  ├─ onetoone.refit <brain> <NNNN.png frames>      → captions.plaintext.json:ref_fit (in place)
  ├─ onetoone.ornate_env <brain> <look_dir>        → captions.plaintext.json:ref_ink_env (in place)
  └─ onetoone.measure_devices <brain> work/refframes24 -o cand.json
                                                   → (checked, L0084) brain/caption_devices.json
CAST
  ├─ onetoone.grades (live ledger _receipts/clip-grades/grades.jsonl)
  ├─ onetoone.castscan <brain> <cast> <ref> <SLOT> --stems a,b,c --identity auto
  │       allowed_stems() ─▶ identity.load_pool + grades.effective_settled/hero_windows
  │       grab() lut3d+cover frames ─▶ bed stats vs reference beds  → castscan-<SLOT>/castscan_<SLOT>.json
  ├─ onetoone.bedprobe <brain> <work> <SLOT> <STEM> <in_s> --crop ... --target ...
  │       framing.filter_for ─▶ render.luma_pull ─▶ captions_typeset.typeset_state + readability
  ├─ onetoone.looksheet <brain> <cast> <ref> <outdir>  → LOOKSHEET.png (eye check before render)
  └─ lane writes deliver/cast_vNNN.json {grade_mode:"house", slots:[{slot, stem, in_s, identity, crop?...}], caption_ink?, caption_shadow?}
GATE
  └─ <PYTHON> -m onetoone.preflight --row NN --cast deliver/cast_vNNN.json
          machine() ─ kit_tests() ─ cast_rules() ─ identity.check_cast() ─ lessons.py harvest/brief
          → ~/reel-production/.preflight-ok/<sha16>.json
RENDER  <PYTHON> -m onetoone.render <brain> <cast> <ref> deliver/reel<NN>-<REFID>-<MODE>-vNNN.mp4 work/vNNN --audio <track>
  render()
   ├─ require_preflight(cast)  (token, 6 h, cast sha, lessons sha)
   ├─ cut_picture()                                   ─ every ffmpeg via render._run → ffx.run (lock)
   │    for shot in cutgrid.shots:
   │      looksheet.resolve_master(stem)
   │      framing.display_dims / filter_for / effective_centre   [zoompan → work/plates/<slot>.mov]
   │      house  : housechain.house_head(... LUT ...) + HOUSE_TAIL
   │      natural: lut3d + NATURAL_GRADE + luma_pull[/pass2/highlight_pull] → luma_only()
   │      match  : grade.sample_frames/extract_reference/shot_stats/solve/filter_from_solution + pulls
   │      light_state_filter, redact_filter
   │      encode → work/segments/<slot>.mp4 ; chroma_sanity() (raises on > 0.40 jump)
   │      caption_masks + per-frame means + luma_ramp loop (not house)
   │    concat → work/picture_silent.mp4 ; mux licensed audio track (copy, no -shortest) → work/picture.mp4
   ├─ rasterise_captions()
   │    devices.load_devices(brain)
   │    per state: typeset_state → own_ink_collision ; beds/<id>_{ours,ref}.png ; state_shadow ; with_shadow ; readability() → UnreadableCaption?
   │    per frame: devices.layer_for_frame → with_shadow → behind_subject → work/captions/caption-NNN.png
   ├─ variant_render.composite(picture, captions) → work/composite.mp4
   ├─ variant_render.encode_delivery (crf 22/24/26/28 until ≤ 10 MB) or copy → deliver/...mp4
   ├─ eye_sheet() → work/eye/EYE_SHEET.png
   └─ work/render_report.json
MEASURE (lane, on the delivered bytes)
  ├─ onetoone.lumacheck <brain> <ref> <ours> <work>     → work/luma_per_frame.json
  ├─ onetoone.sharpness.edge_width_px on decoded frames  (L0038)
  ├─ onetoone.devicesheet / refstrip                      (entry devices by eye)
  ├─ onetoone.motion                                      (camera moves)
  ├─ caption-learning readback / anatomy / inkcheck       (§7)
  └─ lane-side checks with no module: cut peaks at every cutgrid frame on the decoded file (L0053, L0076);
       per-glyph halo on the decoded delivery (L0075, L0083, L0109); OCR for signage (L0073, L0080);
       colour scan per block (L0095, L0099)
AUDIT  one independent agent (read-only), given the delivered paths + the rules, never the builder's conclusions
FIX    every finding, each proven by a targeted machine check → finding / fix / proof table on the card
DELIVER
  ├─ <PYTHON> -m onetoone.preflight --row NN --finish   (inbox triaged)
  ├─ <PYTHON> -m onetoone.registry_row ~/reel-production/REEL_REGISTRY.json NN patch.json --tag rowNNvNNN
  └─ tools/busline.py --state finished   (the lane's last action)
```

Narrative:
1. **Intake.** Conform the reference to `ref/ref24.mp4`, dump `ref-NNN.png`, write `cutgrid.json` (one shot per reference cut, inclusive ranges) and `captions.plaintext.json` (one state per caption appearance; each part with settled box, ink colour, measured frame). `refpeople` marks person shots; `faceid` identifies each face (its `fit` output is stored as `ref_fit` with `face_file`); `refit` fits plain lines; `ornate_env` measures script envelopes; `measure_devices` writes entry curves after low-fit frames are checked.
2. **Cast.** Read live grades and the identity pool. `castscan` ranks windows by caption bed; `bedprobe` confirms a window carries its states; `looksheet` shows stills early. Write the cast with `grade_mode: "house"`, an `identity` on every slot, optional crop/redaction/light-state.
3. **Gate.** `preflight --cast` checks floors, runs the kit tests, checks cast shape and identity, harvests lessons, writes the token. Any later lesson or cast edit means preflight again.
4. **Render.** Cut, grade, chroma-check, ramp (non-house), mux the licensed audio track, typeset and gate captions, replay devices, composite, size for delivery, report. Name outputs `reel<NN>-<REFID>-<MODE>-v<NNN>.mp4` so a bare `v008.mp4` can never be matched to the wrong row (L0118).
5. **Measure and audit.** Measure the delivered bytes; hand the delivered paths to one independent auditor.
6. **Fix and deliver.** Fix and prove every finding; `preflight --finish`; `registry_row`; final busline.

---

## 4. Factory plumbing (`tools/*.py`)

| Tool | Status |
|---|---|
| `busline.py`, `lessons.py`, `whisper_guard.py`, `vocal_guard.py`, `orderd/` | **Live.** Called by every lane, by preflight and by launchd |
| `beatmap.py`, `library_find.py`, `reuse_map.py`, `blackout_scan.py` | Standalone analysis helpers |
| `variant_caster.py`, `variant_render.py` | Retired as a pipeline (L0059). `variant_render.composite()` and `encode_delivery()` are still imported by `onetoone.render` |
| `resolve_ready.py`, `reelctl_resolve_handoff.py` | Dormant DaVinci Resolve hand-off helpers (official scripting API only) |
| `ditl/` | Experimental generator built on the kit's colour and ffmpeg plumbing |

### 4.1 `busline.py` — the only legal way to write an ACK/CALL line

Appends one human line plus one machine-readable status block to a bus receipt in `~/reel-production/_receipts/bus/` (`REEL_FACTORY_BUS_DIR`). Before this existed, readers ran English regexes over free text and misread "finished (final for this note)" as a fresh pickup and a blocked pickup as "fixing".

```
ACK — reel-factory-interactive — <ISO-UTC> — picked up: building v009 …
<!--status {"v":1,"row":47,"lane":"reel-factory-interactive","at":"<ISO-UTC>","state":"picked_up"} -->
```
```bash
<PYTHON> ~/reel-production/tools/busline.py --receipt <file.md> --lane <lane> --state <state> --text "…" \
        [--why "disk 97GB < 100GB floor"] [--not-before <ISO-UTC>] [--artifact deliver/x.mp4]
```
- `STATES = ("picked_up","running","progress","queued","blocked","finished","called")` — closed set. `called` writes a `CALL` head, everything else `ACK`.
- `--why` is **required** for `queued` and `blocked` (truncated to 160 chars in the block).
- `CALL_MAX_CHARS = 320`. `call_shape_problem()` refuses a CALL longer than 320 characters (whitespace-collapsed) or one that does not name both `1 = …` and `2 = …` (`_OPT_RE`). The Deck and `asker.py` turn options into buttons (L0071; `orderd/tests/test_busline_call_shape.py`).
- Row comes from the receipt name (`-row(\d{2,3})-`), else `null`.
- Append-only, `fcntl.flock(LOCK_EX)`, adds a missing trailing newline first, `fsync`s. A receipt is never rewritten.
- Library: `append(receipt, lane, state, text, *, why, not_before, artifact, now)`. `orderd.py` loads it by file path.
- Lessons: L0050 (only the orchestrator writes `finished`, and only after Deliver), L0071.
- Gotchas: the receipt must exist. Readers trust the status block **only when it sits on the line directly after the newest ACK/CALL head and its `lane`/`at` match**; a hand-written ACK without a block downgrades the row to prose parsing.

### 4.2 `lessons.py` — the learning loop

Turns every reviewer comment, verdict, ruling and audit failure into a durable lesson every later lane must read (see [03_QA_SYSTEM.md](03_QA_SYSTEM.md)). Files under `~/reel-production`: `lessons.json` (source of truth `{"lessons","inbox","seen"}`), `LESSONS.md` (a **view** regenerated by `rewrite_ledger()`; the header above the line ``Newest lessons are at the bottom; `brief` prints them newest first.`` is preserved), `LESSONS_INBOX.md` (raw untriaged signal). Lock `~/reel-production/.lessons.lock`.

```bash
<PYTHON> tools/lessons.py harvest                         # scan bus + audit reports + WALLS.md, idempotent
<PYTHON> tools/lessons.py add --row 7 --source "audit-v014" --symptom "…" --rule "…" [--check test_name] [--tags a,b]
<PYTHON> tools/lessons.py triage in0123 --lesson L0042     # or: --none "why no lesson"
<PYTHON> tools/lessons.py brief [--row 7] [--tags grade,captions]   # ★ marks this row
<PYTHON> tools/lessons.py check [--row 7]                 # exit 1 if the row has untriaged inbox items
<PYTHON> tools/lessons.py rewrite                          # regenerate LESSONS.md
<PYTHON> tools/lessons.py setcheck L0042 test_foo          # attach a test, flips enforced -> "test"
<PYTHON> tools/lessons.py swept [--reason pre-loop] [--row N]
```
What `harvest` reads:
1. Every `*.md` on the bus except `ui-smoketest*`. For `ui-*` receipts: `Operator note (verbatim):` and `RULING on …:` blocks, bare `Disposition: APPROVE|REJECT|NOTES`, batch-verdict lines (`## Variant NN — KEEP|KILL|NOTES`), continue presses. For other receipts: `VERDICT` paragraphs and `HARD-N` lines. Also every `CALL — …` line.
2. `WORKBENCH.glob("reel*/audit-*/*.md")` and `reel*/*INDEP-AUDIT*/*.md`: verdict + every HARD finding.
3. `reel*/brain/WALLS.md`: lines starting `**Wall`, `- **` or `## … defect|fixed|FAIL`, longer than 40 characters.

Auto-triage (`triage.auto = true`): `PLUMBING_RE` (SELF-TEST, SYSTEM CHECK, smoketest, plumbing, `[CDP WALK]`, "take NO other action") → plumbing; an audit verdict matching `PASS_RE` **and not** `NEGATIVE_RE` (case-insensitive — "not proven, still fails" must not read as a pass) → "a pass teaches nothing"; a source date older than `LOOP_EPOCH` → backlog.

IDs: lessons `L%04d`, inbox `in%04d`, both `len(list)+1` — **never delete an entry**. Inbox text stored up to 1,200 chars; MD line cut at 600.

Where it bites: `preflight` runs `harvest` + `brief` before every render and `check` on `--finish`; `render` hashes the `lessons` list, so **adding or changing any lesson invalidates every outstanding preflight token** (intended).

Gotchas: `WORKBENCH = /Volumes/WORKDRIVE/workbench` is hard-coded — elsewhere `harvest` silently finds no audit reports. The `seen` key is `source::kind::first 80 chars` (distinct signals sharing 80 chars dedupe). `_row_of` falls back to the first `row N` in the first 400 chars and can mis-attribute.

### 4.3 `whisper_guard.py` — the only legal Whisper

Concurrent Whisper processes launched by parallel lanes once drove a smaller machine out of memory and into kernel panics. Rule: one transcription on the machine at a time (L0070).

```bash
whisper_guard.py <audio> --out <json> [--lang <code>] [--model medium] [--beam 5] [--extra '{"no_speech_threshold":0.6}']
```
Output: openai-whisper result JSON (segments + word timestamps, `word_timestamps=True`, `fp16=False`). Gates: `ALLOWED = {tiny, base, small, medium}` (`large*` refused); exclusive `flock` on `~/reel-production/_receipts/.whisper.lock` (second caller blocks and prints the wait; lock body records `pid model audio t`); refuse if `other_asr_running()` finds a whisper/demucs process outside the guard; refuse if free memory < `MIN_FREE_BYTES = 6 GiB` (computed from `memory_pressure` × `sysctl hw.memsize` — **macOS only**; on Linux it reads 0 and always refuses). `import whisper` happens inside the lock. Shebang points at a Homebrew Python 3.14 that has `openai-whisper`; it also needs `/opt/homebrew/bin` on PATH for ffmpeg.

### 4.4 `vocal_guard.py` — the only legal demucs

`vocal_guard.py <audio.wav|m4a> --out-dir <dir> [--start S --dur D]` → `<dir>/vocals.wav`, `<dir>/no_vocals.wav` (44.1 kHz stereo), `<dir>/segment.wav`. Loads `whisper_guard.py` by path and **shares its lock, process check and 6 GiB floor**. Model `htdemucs --two-stems vocals`; `MAX_DUR = 40.0` s (refused above 40.5 s after decode). Hard-coded `VENV_PY` (a venv with torch + demucs) and `/opt/homebrew/bin/ffmpeg`; a missing venv prints `REFUSED`. Use it only on audio you have rights to process.

### 4.5 `beatmap.py` — onset/tempo/downbeat mapper

```bash
<PYTHON> tools/beatmap.py song.m4a [--start 85.11] [--dur 30] [--json beats.json]
```
ffmpeg decode + numpy, no librosa. `SR = 22050`, `HOP = 256` (~11.6 ms), `NFFT = 1024`; spectral flux `log1p(1000·|S|)` with a 31-frame moving-average detrend; onsets threshold `0.12`, min gap `0.055` s; tempo from autocorrelation peaks in `55–200` BPM (top 8); grid fit for {bpm, ×2, ÷2, ×4, ÷4} inside 60–200, phase scanned in 200 steps, on-grid within ±10 % of the period. JSON: `onsets`, `tempo_candidates`, `grid_fits[]` (`bpm`, `phase_s`, `on_grid_onsets`, `beat_hit_pct`), `primary_bpm`, `primary_phase_s`, `downbeats_4_4`. "Downbeats" are every 4th beat from the best phase — no bar detection.

### 4.6 `library_find.py` — footage library query

```bash
<PYTHON> tools/library_find.py --role traversal --world <world> --min-energy medium [--action walk] \
        [--subject S] [--lighting L] [--crop-safe] [--min-duration 3] [--limit 20] [--json]
```
Reads `LIB = ~/reel-production/FOOTAGE_LIBRARY.json` (`clips[*].tags`), ranks by `tags.confidence` (high > medium > low) then energy (`ENERGY = {"low":0,"medium":1,"high":2}`). `--crop-safe` = `crop_safety_916 == "safe"`. The library's `subject` tag is **untrusted for identity** — never use `--subject` to pick identity shots; use `identity_pool.json` and the clip grades.

### 4.7 `reuse_map.py` — canonical cross-row clip reuse

The single source for "which master clips shipped in which delivered reel"; every reuse claim on an approval card must cite its output. It exists because hand enumerations read drafts instead of shipped versions, missed source keys, or read clip ids out of prose.

```bash
<PYTHON> tools/reuse_map.py --rebuild          # -> ~/reel-production/REUSE_MAP.json
<PYTHON> tools/reuse_map.py --row 7           # per-slot reuse table for one reel
<PYTHON> tools/reuse_map.py --master CLIP_0001 # every delivered slot a master ships in
<PYTHON> tools/reuse_map.py                    # summary
```
- `SOURCE_KEYS`: every key that ever carried a slot source (`source, source_path, source_file, master, master_path, remote_path, local_path, path, file, filename, basename, clip, clip_file, …`). `sources_of()` is deliberately shallow — never walks into `runner_ups`, `rejected_*`, `disclosures`, `candidate_observation`.
- `MASTER_RE` recognises `CLIP_NNNN`, `20YYMMDD_E?_NNN[N]`, `CNNNN[N]`, `IMG_NNNN`, optionally prefixed by an id/hash ending in `__`.
- `ROWS: dict[int, RowSpec]` is a **hand-maintained declaration** of each delivered plan file. It ships **empty**; `EXAMPLE_ROWS` shows every field (`version`, `base`, `reason`, `supersedes`, `also`, `variants`, `historic`, `unresolved`).
- `ReuseMapError`: a declared source that does not resolve is a crash, never a warning.
- `rows_outside_the_tool(reg)`: registry rows with delivered cuts that are not in `ROWS` — any "first ship" or "zero collision" claim is unproven against them.

Tests: `tools/test_reuse_map.py` pins facts from a specific registry and will fail without that data; treat it as a pattern. Known gap: the kit's `cast_vNNN.json` schema is not one of the shapes it reads.

### 4.8 `blackout_scan.py` — identifier scan for outgoing text

`<PYTHON> tools/blackout_scan.py <file> [...]` prints `name: clean` or `HITS [(term, count)]`, then `files with hits: N of M`, exit 1 on any hit. `TERMS` is read from `REEL_FACTORY_REDACT_TERMS_FILE` (one term per line, `#` comments; empty when unset) — **list the identifiers you never want in outgoing cards, captions or filenames, and keep the file out of the repo**. Terms ≤ 3 characters match space-delimited. Reads files as UTF-8; scan the text side of deliverables (cards, caption JSON, filenames) — burned-in pixels are not scanned. Optional; the Deck's `receipts._scrub()` reads the same file at runtime and replaces each term with `REEL_FACTORY_REDACT_REPLACEMENT` (default `[redacted]`).

### 4.9 `variant_caster.py` / `variant_render.py` — alternate castings (retired pipeline)

`variant_caster.py` emits N alternate castings of one locked reelctl project: every slot keeps its frame count and role, only the source clip changes. Gates (all refuse rather than warn): clip must be a PASS member of the footage index and live under `masters/`; exact-clip blacklist on every identifier (`_ID_KEYS = clip_id, id, content_id, drive_file_id`; `_FILE_KEYS = file, camera_name, library_path, v1_file`); per-slot allowed roles/lighting/subject from a policy file (an empty pool is a finding, never silently widened); clock `required_source_frames = max(1, ceil(frames × src_fps / out_fps))`; distance `min_changed_ratio` (default 0.70) between any two castings and vs the master; no clip twice in one casting; **caption clearance** `CAPTION_MIN_SEPARATION = 25.0` with `caption_servable_band(alpha)` = light-ink max `255 − 25/α`, dark-ink min `25/α`, judged on **p95 for light ink and p05 for dark ink** (`CAPTION_LUMA_PERCENTILES = (5, 50, 95)`) — the median hides a dark head sitting under a word. Deterministic (`_rng` seeded from sha256; scarce slots first; least-used clip wins; `max_attempts = 400`; windows at `WINDOW_QUANTILES = (0.10, 0.38, 0.66, 0.88)`). `CAPTION_CANVAS = (1916, 1078)`.

```bash
<PYTHON> tools/variant_caster.py --project <locked dir> --library FOOTAGE_LIBRARY.json --blacklist SHOT_BLACKLIST.json \
        --policy policy.json --out <dir> --seed 17 [--variants 10] [--family-id ID]
<PYTHON> tools/variant_caster.py recast --project … --casting var03-casting.json --slot p010 --siblings <dir> --out <new.json> --reason "…"
```

`variant_render.py` rendered a casting through the reelctl engine (sandboxed project copy, signed colour proofs, identity creative grade, QC, caption re-render byte-compared to approved frames, a 1080×1920 vertical render, a ≤ 10 MiB delivery encode). Constants: `VERTICAL_SIZE (1080,1920)`, `WEB_SIZE_CAP_BYTES 10 MiB`, `WEB_CRF_LADDER (21,23,25,27)`, `DELIVERY_CRF 17`, `IDENTITY_CREATIVE` (exposure 0, contrast/saturation/gamma 1.0), `SEPARATION_FLOOR 25`, `RING_RADIUS 6`, `ANCHOR_CENTRE_PRIOR 0.35`, `ANCHOR_SAMPLE_FRAMES 5`. `CAPTION_FLOOR_RULE`: 25 luma levels of separation, 3.0:1 minimum contrast ratio and at most 10 % weak ink, per state, on the **worst** frame of its span.

**Still load-bearing:** `onetoone.render` imports `variant_render.composite()` (tv→full, alpha composite, full→tv BT.709, exact frame count) and `encode_delivery()`. Importing it requires `_reelctl/src` importable. It calls bare `ffmpeg`. Porting tip: lift those two functions into the kit and drop the import.

### 4.10 Resolve helpers (dormant)

- `resolve_ready.py` — read-only readiness check (ffmpeg, ffprobe, reelctl, a Resolve MCP server, the app and scripting paths, a live call through Resolve's official external scripting API); PASS/FAIL + `JSON_SUMMARY`; exit 0 ready, 2 blocked. Contains machine-specific paths and an agent-runtime check; rewrite or delete.
- `reelctl_resolve_handoff.py <project> [--out DIR]` — writes `resolve-handoff/timeline-spec.json` (frame contract, slots with timeline/source frames, a marker per slot), a `resolve-import-plan.py` skeleton and a README. Never touches Resolve. Defaults 1080×1920 at `30/1` if the clock is missing.

### 4.11 `ditl/` — "day in the life" generator (experimental)

Builds many day-in-the-life reels from the footage library using the kit's plumbing (`looksheet`, `housechain`, `grades`, `identity`, `ffx`). Working root `WB = $DITL_WORKDIR` (default `$REEL_FACTORY_WORKBENCH/ditl`). Required under `WB` (not in the repo): `beats_verified.json` (eye-verified beat code per clip), `look/DAY_POST.txt`, `look/DAY_POST_DRL.txt`, `look/NIGHT_POST_DRL.txt` (ffmpeg filter strings appended after the house chain — **read at import time**, a missing file crashes the import), optional `exclude.json`, `capture_times.json`, `songs/`, `story_specs.json`.

| Module | Role |
|---|---|
| `beats.py` | first-pass beat taxonomy from library tags (an ordered list of activity regexes — rewrite for your footage); writes `beats_auto.json` for a human to verify |
| `templates.py` | shot templates `S(beats, codes, time, label, night, hook)` |
| `make_specs.py` | writes `specs/V*.json` + `BATCH_PLAN.md`; one song per variant; if beat < 0.38 s the grid doubles; song window chosen so the biggest RMS lift (energy now − 2 s ago) lands on the day→night hinge |
| `ditl.py spec.json` | renderer: 1080×1920 at **24 fps**, hard cuts; `CUT_LEAD = 2/24` s (a cut lands 2 frames before the beat; cuts snap to absolute frames because per-shot rounding drifted 0.4 s over 30 s); captions in Helvetica Neue (time line Bold 65 px cap-top y=901, label Light 36 px caps y=980, hook Bold 71 px, faint offset shadow α 70); librosa `beat_track`; Vision person centring clipped 0.16–0.84 (falls back to 0.5); shot score rejects luma < 34 (night) / < 45 (day) and Laplacian variance < 15, +2 for a face with a bonus for face area 0.004–0.12 and a penalty above 0.2, −1.5 body-part close-up, −0.6 for > 2 people; encode libx264 crf 16 bt709 tv, AAC 256k with 0.6 s fade; writes `<name>.edl.json`; in-points obey clip grades (L0065) |
| `batch.py` | renders every spec serially; skips existing; avoids clips used ≥ 2×; non-blocking lock `WB/.batch.lock` |
| `review.py` | contact sheet, one mid-frame per shot |
| `page.py` | builds `WB/out/index.html` |
| `serve.py [port]` | read-only HTTP with Range support (`CHUNK = 2 MiB`); **binds 0.0.0.0 with no auth — change to 127.0.0.1** |
| `story.py S01 …` | chronology-true variant: shots sorted by real capture time (`DITL_UTC_OFFSET_HOURS` env, default 0), day 05:00–18:44 / night 18:45–04:59, monotonic display times, fixed cut list, song from `DITL_SONG` at a fixed `SONG_START`, 25 fps, captions in the open variable font at `DITL_STORY_FONT`; `audit(edl)` prints `LAWFAIL` on violations |

---

## 5. The order daemon (`tools/orderd/`)

The engine that turns a Deck button press into a Claude Code lane. It replaced an always-on worker + healer (whose relaunch loop is documented in [01_ARCHITECTURE.md](01_ARCHITECTURE.md)).

### 5.1 `orderd.py`

launchd `com.reelfactory.reel-orderd` runs `orderd.py --once` every 60 s.

One tick:
1. **Service running lanes.** Up to `MAX_LANES` lock slots (`_control/orderd.lock`, then `orderd.lane<N>.lock`). A tmux lane is done when the receipt carries an `orderd-lane` finished/blocked/called line written after start, the pane is back at a shell (`SHELLS`, after `SHELL_GRACE_S = 45` s), the session is gone, or the wall clock hits. Then orderd captures the pane into the lane log, kills the session, writes the final bus line from the lane's busline / result file / pane text, and clears the lock. A pane showing `Login expired` (or an account refusal) pauses the daemon (`orderd.PAUSED`) and files one CALL to the human; orderd never types into a login prompt.
2. **Gates.** Do nothing if `_control/orderd.backoff` is in the future, `_control/orderd.PAUSED` exists, or the kill switch `_control/authority.json` is not open (`factory_state` must start with `FACTORY_OPEN`; **fail-closed** when missing or unreadable).
3. **Scan.** Read the bus for `ui-*.md` receipts and `classify()` each (actionable, retry, closed, claimed, stale, skip). Priority: (1) feedback with `Disposition: REJECT`, (2) other feedback / frame-note / batch-verdict, (3) continue, (4) drop, smoketest last. Ties oldest first. A row with a running lane is held — two lanes never work one row (L0070). Feedback and frame-notes for one row are **bundled** and **debounced** by `DEBOUNCE_S = 120` s. Receipts older than `LOOKBACK_DAYS = 14` are stale.
4. **Spawn.** Write `picked_up` via `busline.py`, then spawn.

Launch shape (`launch_line`): `tmux new-session -d -s reel-lane-<UTCstamp> -c ~/reel-production`, then `send-keys` of the claude line into the shell (sending it into a shell avoids tmux reporting the `sh -c` parent as the pane command). Command: `claude --settings orderd/claude-settings.json --permission-mode <ORDERD_PERMISSION_MODE> --model <MODEL> "$(/bin/cat <prompt file>)"; echo $? > <exit file>`. `--headless` keeps an older `claude -p` shape (off by default; it lost its login under launchd).

**`claude-settings.json` ships with `permissions.defaultMode: "acceptEdits"`** and an empty `permissions.allow` list (plus `autoCompactEnabled: true`, `autoCompactWindow: 350000`). Lanes launch with `--permission-mode $ORDERD_PERMISSION_MODE` (default `acceptEdits`; orderd refuses any mode that skips permission checks). Add the commands lanes need to the allow-list (see `config/reel-production.claude-settings.json` for the ffmpeg/ffprobe/exiftool allow-list pattern). A lane that stops on "Login expired" or an account refusal is never typed into: orderd writes `_control/orderd.PAUSED`, files one CALL and waits for a human. See [07_WORKING_WITH_AN_AI_OPERATOR.md](07_WORKING_WITH_AN_AI_OPERATOR.md).

| Constant | Value / env override |
|---|---|
| `BUS` | `~/reel-production/_receipts/bus` (`ORDERD_BUS`, else `REEL_FACTORY_BUS_DIR`) |
| `CONTROL` | `~/reel-production/_control` (`ORDERD_CONTROL`) |
| `LOGDIR` | `~/reel-production/_receipts/orderd` (`ORDERD_LOGDIR`) |
| `BUSLINE` / `SETTINGS` / `STATUS_FILE` / `AUTHORITY` | `ORDERD_BUSLINE` / `ORDERD_SETTINGS` / `ORDERD_STATUS` (Deck heartbeat `~/apps/reel-deck/factory_status.json`) / `ORDERD_AUTHORITY` |
| `CLAUDE` | `~/.local/bin/claude` (`ORDERD_CLAUDE`) |
| `PYTHON` | `/usr/bin/python3` (hard-coded) |
| `TMUX` | `/opt/homebrew/bin/tmux` (`ORDERD_TMUX`); tests use `ORDERD_TMUX_SOCKET` / `ORDERD_TMUX_SHELL` |
| `MAX_LANES` | `max(1, min(3, ORDERD_MAX_LANES or 3))` — 3 is the ceiling for heavy ffmpeg lanes |
| `LANE_WALL_S` | 5 h (`ORDERD_LANE_WALL_S`) |
| `PAUSED` on refusal | an account refusal (`REFUSAL_RE`: invalid key, expired login, low credit…) or a "Login expired" stop writes `called --why account-refused`/`login-expired` and the `orderd.PAUSED` flag; only a human removes it |
| `LIMIT_RETRY_S` / `MAX_ATTEMPTS` | 30 min / 3; a usage-limit or 5xx death (`LIMIT_RE`) is re-queued with `not_before`, then blocked |
| `CLAIM_TRUST_S` | 3 h (matches the Deck's `PICKUP_TRUST_S`) |
| `MODEL` | `opus` (`ORDERD_MODEL`) |
| `DEBOUNCE_S`, `LOOKBACK_DAYS`, `SHELL_GRACE_S`, `PERMISSION_MODE` | `ORDERD_*` of the same name |

```bash
<PYTHON> ~/reel-production/tools/orderd/orderd.py --once            # one tick
<PYTHON> …/orderd.py --once --dry-run                               # print the decision only
<PYTHON> …/orderd.py --once --smoke                                 # newest ui-smoketest-orderd-* receipt, plumbing lane
<PYTHON> …/orderd.py --status                                       # locks, backoff, open orders
<PYTHON> …/orderd.py --stop                                         # kill every lane, write blocked --why stopped
touch ~/reel-production/_control/orderd.PAUSED                     # pause without unloading launchd
```
A tick takes `_control/orderd.tick.lock` non-blocking. `--run-lane LOCK` is internal. A variants order is a `ui-drop` whose body says `ORDER: build a 10-variant alternate batch for registry row NN.`; it gets that row number and routes to the variants brief (L0059). Docstring drift: the header still says "one lane at a time" and "3 h wall"; the code (`MAX_LANES = 3`, 5 h) wins.

### 5.2 `lane_prompt.py`

Builds the full prompt for one lane; reads files, writes nothing.

```bash
<PYTHON> tools/orderd/lane_prompt.py <order-receipt.md> [--bundle other.md ...] [--result result.json]
<PYTHON> tools/orderd/lane_prompt.py --smoke <receipt.md> --result result.json
```
Composition:
- **LAW block** — copied verbatim from the newest `~/reel-production/workflows/scripts/reel<NN>-build.js` (text between ``const LAW = ` `` and the closing backtick); only the template's row number and reference id are substituted. A missing LAW raises `ValueError`: **no lane can launch without at least one `reel*-build.js` carrying a `const LAW` block** (none is shipped; write your own house rules there).
- **`KIND_TASK`** — a brief per order kind: drop (register the next registry row under the registry lock with a backup, `review_state: REGISTERED__INTAKE_PENDING`), variants (approved brain locked byte for byte, 10 casts, ≥ 70 % slot distance, unique hook, one batch audit, `BATCH_CARD.md`), feedback (APPROVE = mark approved, no rebuild; REJECT/NOTES = measure first (L0038), then fix), frame-note, batch-verdict, continue.
- **`STANDING`** — the house rules: four phases with **one** independent audit then fix everything it found (L0092); identity declarations; house look (L0018/L0037); caption faces identified, never assumed (L0055); delivered-file naming (L0118); CALL shape (L0071); clip grades as casting law (L0065); per-state caption shadow (L0056); measured caption devices; preflight before render and `--finish` before finished; **local delivery only, never post, publish, schedule or upload**; busline for every state change; no names or secrets in files; ffmpeg only via `ffx`, transcription only via `whisper_guard`.
- **`RESULT_SPEC`** — before its final busline the lane writes `{"outcome": "FINISHED"|"CALLED"|"BLOCKED", "row", "text", "artifact", "why"}`.
- **`identity_rule()`** — reads `tools/onetoone/identity_pool.json`.

**Rewrite `STANDING` before use.** It was written for one deployment: it names a specific approved baseline, specific fonts and file paths, and carries deployment-specific rulings. In a public or shared deployment, replace any font rule with "identify the face; license it or use an open-licensed match; traced recreations are for matching only", and add an explicit rule that the lane raises a question when a reference's music, typeface or a close recreation of another creator's work needs permission. See the README's Rights section.

### 5.3 `asker.py`

Relays the factory's questions to the reviewer's phone through a Telegram bot. launchd `com.reelfactory.reel-asker` runs `asker.py --once` every 60 s.

1. **PUSH** — every unpushed open CALL younger than `FRESH_S = 48 h` (`ASKER_FRESH_HOURS`) is sent as one short message: question, options (each ≤ `OPT_MAX = 160` chars), and "reply 50 1".
2. **REPLIES** — `getUpdates` filtered to the configured `chat_id`; `_REPLY_RE` accepts `50 1`, `1` (newest question) or `48 2 make it brighter`. A reply is written as a `ui-feedback` receipt `RULING on <call receipt>: <n> — <option>` with the words verbatim; orderd picks it up like any Deck answer.
3. **AUTO-DEFAULT** — an option marked "(recommended)" is taken after `AUTO_MINUTES = 20` (`ASKER_AUTO_MINUTES`) with no reply — **never** when the question matches `NO_AUTO_RE` (spend, buy, purchase, post, publish, upload, drive, pay, card, `$N`).
4. **READY** — a `finished` line with an artifact sends "Row NN ready to review" plus the Deck link (`ASKER_DECK_URL`, default `https://<your-host>:8443`).

State `_control/asker.state.json`. Config `ASKER_TELEGRAM` (default `~/.config/reelfactory/<your-notify-config>.json` with `bot_token` and `chat_id` — a secret; never commit it). Flags `--once`, `--dry-run` (prints, sends nothing), `--list` (print open questions and exit).

### 5.4 orderd tests

Run from inside `orderd/` (from `tools/` collection fails on `import asker`): `cd ~/reel-production/tools/orderd && <PYTHON> -m pytest tests -q`. Needs tmux (or `ORDERD_TMUX`); tests set their own `ORDERD_*` sandbox env.

| File | Pins |
|---|---|
| `test_orderd.py` | classify / priority / debounce / bundle, retries, kill switch, tmux lane lifecycle on a sandbox socket, two lanes side by side never on one row (L0070), direct launch line |
| `test_asker.py` | option parsing, reply parsing, ruling receipt shape |
| `test_busline_call_shape.py` | the CALL shape (L0071) |
| `test_variants_order.py` | variants orders use the variants brief (L0059) |

---

## 6. reelctl — the signed stage machine (`_reelctl/`)

### 6.1 Status: halted by design

`reelctl` is a deterministic reference-reel reconstruction runtime: a Python package + CLI that turns a locked reference and an authorized footage pool into an append-only, hash-bound, HMAC-signed local review candidate through 14 ordered stages. It is **not** the live render path. Every mutating command checks the kill switch `~/reel-production/_control/authority.json` and accepts only this exact four-key object:

```json
{"schema_version": 1, "authority": "REEL_PILOT", "factory_state": "FACTORY_OPEN", "mutations_allowed": true}
```
orderd accepts any `factory_state` that *starts with* `FACTORY_OPEN` and tolerates extra keys, so an authority file written for orderd (as in `config/control/authority.json`, which adds an `opened_by` note and a suffixed state) keeps reelctl closed while the kit path is open. Writing the exact four-key form re-opens every reelctl mutation and the studio daemon — do it deliberately.

Treat reelctl as: (1) a library of hard-won contracts and QC gates (reference clock lock, audio identity, typography proofs, reference-relative QC, caption QC — their lessons still hold); (2) the package that historically hosted the house LUT path; (3) a dormant control plane (Reel Studio daemon + FastAPI board on port 7335).

### 6.2 Package, install, dependencies

| Item | Value |
|---|---|
| Package | `reelctl` 0.1.0, `requires-python >=3.9`, setuptools, `package-dir = src` |
| Entry point | `reelctl = reelctl.cli:main` |
| Install | `uv tool install --editable . --force` (editable; source edits take effect immediately) |
| Dev | `uv run --extra dev pytest -q` (creates `.venv`) |
| Package data | `data/*.json` (colour profiles, ink lockup specs), `data/luts/*.cube` (**not shipped — add your LUT**), `schemas/*.json`, `static/*` |
| Lint | ruff, `target-version py39`, `line-length 140`, rules `E4 E7 E9 F I B` |

| Package | pyproject range | Known-good version |
|---|---|---|
| fastapi | `>=0.115,<1` | 0.141.1 |
| uvicorn | `>=0.30,<1` | 0.52.1 |
| fonttools | `>=4.50` | 4.63.0 |
| jsonschema | `>=4.18,<5` | 4.26.0 |
| numpy | `>=1.24` | 2.5.2 |
| opencv-python-headless | `>=4.8` | 5.0.0.93 |
| Pillow | `>=10` | 12.3.0 |
| yt-dlp | `>=2025.1.1` | a recent release |
| dev: httpx / pytest / ruff | `>=0.27,<1` / `>=8,<9` / `>=0.8,<1` | 0.28.1 / 8.4.2 / 0.16.2 |

System binaries: `reelctl doctor` checks `ffmpeg`/`ffprobe` (FAIL if missing) and reports `yt-dlp` and `tesseract`; `color identify` needs `exiftool`; the judgment worker needs `claude` on PATH. Fonts are discovered from macOS font dirs plus `--extra-dir`. Errors: `ReelctlError`, `ContractError`, `ExternalToolError`; ids must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$` with no `..`. Every command prints one JSON line; exit `0`, or `2` on `status == "FAIL"` or an exception.

### 6.3 Module map (`src/reelctl/`)

| Module | Purpose |
|---|---|
| `cli.py` | argparse front end, `run` auto-advancer, `doctor`, `serve`, `studio daemon`; wraps every mutating command in the authority guard and project flock |
| `state.py` | 14-stage machine, signed immutable stage receipts, recursive verification, STALE propagation |
| `signing.py` | HMAC-SHA256 with purpose strings |
| `hashing.py` | sha256 (symlink-refusing, memoised), canonical-JSON `recipe_hash`, atomic writes, `atomic_external_output` for ffmpeg |
| `paths.py`, `locks.py`, `identifiers.py`, `errors.py`, `contracts.py` | confinement, per-project flock, id rules, error classes, JSON-Schema (Draft 2020-12) validation (≤ 20 errors reported) |
| `media.py` | ffprobe/ffmpeg wrappers: PTS ledgers, framemd5 decoded hashes, PCM hashes, `signalstats` legal-range probe, toolchain identity |
| `reference.py` | reference analysis (clock/audio/cut-draft/onsets/boards) and blueprint lock |
| `footage.py`, `feasibility.py`, `selection.py`, `authorization.py`, `retrieval.py`, `variants.py` | inventory; per-block coverage; slot validation; authorized-root gate; role shortlist; alternate-family validator |
| `color.py`, `grading.py` | input-profile proofs, bounded creative grade, colour filter, LUT install, grade proposals |
| `typography.py` | font identity, exact-font and traced-glyph proofs, discovery, silhouette match, mask extraction, plate rendering |
| `assets.py` | assets manifest validation, overlay sequence composition |
| `render.py`, `qc.py`, `fixtures.py` | render + signed receipt; four QC authorities; regression fixture audit |
| `captions/`, `captions_cli.py` | caption contract / program / render / ink / extract / fontproof / QC engine |
| `studio/`, `web.py` | Reel Studio daemon, judgment worker, board/review/publish/calls/intake; FastAPI app (loopback only) |

The top-level `_reelctl/schemas/` is an older duplicate; the loaded schemas are `src/reelctl/schemas/`.

### 6.4 Stage machine and receipts

```
REFERENCE_LOCKED → BLUEPRINT_LOCKED → FOOTAGE_INDEXED → FEASIBILITY_REPORTED → SELECTION_LOCKED
→ ASSETS_LOCKED → RENDERED → TECHNICAL_QC → STRUCTURE_QC → VISUAL_QC → LOCAL_REVIEW_READY
→ HUMAN_APPROVED → DELIVERY_APPROVED → DELIVERED
```
Statuses `PENDING`, `PASS`, `FAIL`, `BLOCKED`, `STALE`; `state.json` must have `schema_version` 3 and exactly this list.

`complete(name, input_hash, outputs, status, reason)`: predecessor must be PASS and re-verify; outputs confined under the project root (no symlinks), hashed and sized; same stage + same input + same outputs is a no-op, same input with different outputs is an error (except the three QC stages, which legitimately refresh); builds an immutable receipt, `receipt_id = recipe_hash(core)`, HMAC-signs with purpose `stage-receipt-v1`, writes `.reelctl/receipts/<stage>/<receipt_id>.json`; a changed input hash marks every downstream non-PENDING stage `STALE`.

`verify_stage(name)` re-checks status, receipt sha256, HMAC, stage/input match, recomputed `receipt_id`, recursive predecessor linkage, and every output's size and sha256 (a per-walk `_verified` set keeps it linear). `next_stage()` returns the first stage not PASS **or** failing verification — hand-edit any artifact and the machine points back at it.

Signing: HMAC-SHA256 over `"reelctl:<purpose>:" + canonical_json(payload minus signature)`. Key from `$REELCTL_AGENT_REVIEW_KEY` or `~/.config/reelctl/agent-review.key` (auto-created, 32 random bytes, mode 0600, regular file). **The key is machine-local**: receipts signed elsewhere fail verification. Locks: `<root>/.reelctl-locks/<id>.lock`, `flock(LOCK_EX|LOCK_NB)`, `ProjectBusyError` if held. Atomic writes via dir-fd `O_EXCL|O_NOFOLLOW`, fsync, `os.replace`, directory fsync. `canonical_root` refuses a symlinked root — pass `--projects-root` pointing at a real directory.

Project layout: `project.json`, `state.json`, `reference/`, `footage/`, `edit/`, `assets/`, `review/`, `deliver/`, `.reelctl/` (receipts, segment cache). All intervals zero-based, half-open `[start, end)`.

### 6.5 CLI

```bash
reelctl doctor | fixtures audit | status <ID> --json | serve --host 127.0.0.1 --port 7335      # unguarded
reelctl new <ID> --reference <reference-url-or-file> --footage /path/to/authorized/pool --mode reference-locked|original-montage [--defer-analysis]
reelctl run <ID> --through local-review --revision v001 --default-profile sony_slog3_sgamut3cine
reelctl reference analyze <ID> [--source FILE]
reelctl blueprint lock <ID> --boundaries 12,40,77 --hard-cuts 12,77 --observations obs.json --all-frames-reviewed   # or --accept-draft
reelctl footage index <ID> [--footage ROOT] --default-profile log_unknown
reelctl footage shortlist --blueprint B.json --catalog CATALOG.jsonl [--features F.json] --output S.json --top-n 20
reelctl feasibility template|lock <ID> ;  reelctl selection template|validate <ID>
reelctl color identify --source CLIP.MP4 [--output proof.json] ; color setup <ID> --profile sony_slog3_sgamut3cine|rec709 ; color propose|validate
reelctl typography inventory <ID> [--extra-dir D] ; typography extract-mask --image F.png --output M.png --roi X1 Y1 X2 Y2 --ink bright|blue|dark
reelctl typography match <ID> --text "WORDS" --mask M.png [--limit 20] ; typography trace --mask M.png --output PLATE.png
reelctl assets template|lock <ID> ; render <ID> --revision v001 ; qc <ID> --revision v001
reelctl review record-agent <ID> --revision v001 --status PASS --normal-speed-full-watch --reference-side-by-side-checked \
    --typography-checked --color-checked --cut-and-beat-checked --notes "…"
reelctl variants validate --manifest FAMILY.json --output OUT.json
reelctl captions program|validate|ink|render|qc …
reelctl studio daemon [--once] [--interval 20] [--no-judgment]
```
`run` advances deterministic stages and stops with structured `BLOCKED` + `next_action` at each judgment gate (blueprint lock needs an agent to watch the reference at normal speed; feasibility/selection/assets write templates and block; "guessed fonts are forbidden"; human approval and delivery always block — reelctl never self-publishes). There is no publish, upload or cloud command anywhere (pinned by tests). `new --reference <url>` downloads through yt-dlp: only fetch references you are permitted to download under the platform's terms; prefer supplying a local file.

### 6.6 Reference, footage, selection, colour, typography

- **`analyze_reference`** fails closed if decoded frame count ≠ PTS-ledger length, `pts[0] != 0`, non-uniform PTS step, OpenCV and ffprobe disagree, the decoded hash ledger is incomplete, or audio start ≠ 0. Writes a signed `reference-lock.json` (bytes + sha256, full clock with SAR/rotation, audio codec/payload/PCM s16le 48 kHz mono sha/packet ledger, per-frame `framemd5` at `gbrp16le`, **toolchain identity** (ffmpeg path, sha256, version), draft cuts, onsets, a 4-column draft board and a 12-column all-frames board). Because the toolchain hash is part of identity, **upgrading ffmpeg invalidates every reference lock**.
- **Cut-draft detector**: per adjacent pair `0.55·mean|ΔLab|/255 + 0.45·Bhattacharyya(16×16 H-S hist)` at ≤ 320 px; threshold `max(0.115, median + 8·max(MAD, 0.002), min(0.35, 0.68·q95))`; peaks within 2 frames merged; result `DRAFT_REQUIRES_AGENT_VISION_LOCK`, never authoritative.
- **Audio onsets**: 48 kHz mono RMS per 480-sample hop, positive novelty, threshold `median + 5·max(MAD, 1e-5)`, peaks ≥ 5 hops apart, `round(t·fps)`.
- **`lock_blueprint`** requires a valid reference lock, `--all-frames-reviewed`, hard cuts ⊆ boundaries, exactly one observation per block (non-placeholder `description` and `role`, `evidence_frames` inside the block, `transition_from_previous` = `opening` for the first block, `hard_cut` at a hard cut, else `held_transition | effect_state | reframe | other_picture_state`); optional `repeat_group` is the only licence to reuse a source across blocks.
- **Footage inventory**: `.mp4 .mov .m4v .mxf .avi`; **any symlink under the root aborts the scan**; full sha256, ffprobe facts, `clip_id = sha256[:16]`, four thumbnails at 8/32/56/80 %.
- **Feasibility**: per block `exact_scene_available | role_equivalent_substitute | missing` with required decisions; BLOCKED if any block is missing or reference-locked exact coverage < **0.80** (recommends `original-montage`).
- **Selection** (reference-locked): one slot per block; frames equal block frames; a `candidate_observation` that is not a copy of the reference role; `reverse: false`, `speed == source_rate == 1.0`; source sha-bound to the inventory and among the block's `evidence_clip_ids`; window fits `start + ceil(frames·src_fps/out_fps) ≤ frame_count`; reuse only within one `repeat_group`; total frames equal the blueprint.
- **Authorization**: `authorized_footage_root` / `footage_root` from `project.json`; refuses if neither (research pools and reference downloads are study material, never sources) or if they conflict; `realpath` must sit strictly beneath the root. Runs for every render slot and every asset plate.
- **Input profiles** (`data/color-profiles.json`): `rec709` (identity), `sony_slog3_sgamut3cine` (packaged LC-709 cube, hash-locked; proof from camera metadata or documentation), `custom_log` (hash-locked custom LUT). `log_unknown` fails ("a file looking flat does not identify its log curve or gamut"). `color identify` runs `exiftool -G1 -a -s -api LargeFileSupport=1` and requires the camera XML to declare S-Log3 / S-Gamut3.Cine / rec709 coding.
- **Bounded creative grade**: exposure −1.5…+1.0 stops, contrast 0.75…1.3, saturation 0.75…1.3, gamma 0.8…1.2; `color propose` never applies anything; one non-identity recipe may not be reused across different lighting families. Chain: `scale in_range→full`, `gbrpf32le`, `lut3d interp=tetrahedral` (LUT hash rechecked), `colorchannelmixer` exposure, `eq`, back to bt709 limited `yuv422p10`.
- **Typography proofs** (only two are legal): `exact_font` (file sha256 + explicit `face_index`, parsed identity: family, subfamily, PostScript name, revision, UPM, glyph count, cmap hash, axes, features, rasteriser and Pillow version; full codepoint coverage; **variable fonts rejected**; signed reference match IoU ≥ **0.995**) or `traced_reference_glyph` (hash-locked plate + signed provenance, IoU ≥ 0.995). Discovery helpers are aids, never proof: `match_fonts` sweeps 9 sizes × 2 layouts × 3 trackings × 8 x-scales scoring `0.55·contour + 0.45·topology − penalties`; `extract_ink_mask` HSV rules bright V≥205 & S≤105, blue H 85–140 S≥70 V≥80, dark V≤55. The live caption rule is **typeset, never traced** (kit `captions_typeset`); the traced route here is legacy.
- **Assets**: every plate passes the authorization check (fonts exempt); effect layers need a provenance proof; `compose_overlay_sequence` writes one full-canvas RGBA PNG per frame + a signed receipt of every frame hash.
- **Alternate families** (`variants.py`): ≥ 5 variants; same slot ids/roles/frames; clip identity = `source_sha256` (a new window of the same clip is not new); opening slot uses a new clip unique across siblings; ≥ 2 slots and ≥ 30 % of visual slots change; no duplicate packages; `publication_allowed: False` always.

### 6.7 Render and QC

`render_project(project_dir, revision)`: re-verifies everything upstream and authorizes every slot; recipe hash over project, reference, blueprint, selection, assets, sources, receipts, toolchain — **revisions are append-only** (different recipe for an existing revision refused; same recipe = cache hit); renders each slot once to `.reelctl/cache/segments/<hash>.mov` (`hflip?`, `trim`, `setpts`, `fps=<ref>:round=near`, `trim`, Lanczos cover-scale, crop at `crop_anchor_xy`, `setsar=1`, colour filter; `prores_ks -profile:v 3` `yuv422p10le` bt709 tv, single-threaded, frame count asserted); concat → optional overlay → the policy's audio track (licensed `audio_track` by default; the reference's audio only under the `reference_audio_rights_held` opt-in) **stream-copied** into a master `.mov` and a review `.mp4` (libx264 `-preset slow -crf 12`, `yuv420p`, `+faststart`, `-fps_mode passthrough -enc_time_base 1:N -video_track_timescale N -bf 0`); post-checks frames, PTS, PCM, payload, packets, audio start; signs `render-receipt.json` with `publication: {status: NOT_APPROVED, human_approval: PENDING}`. Known limitation: the review encode clamps luma to `REVIEW_LUMA_HEADROOM = (30, 214)` and chroma to `(16, 240)` because H.264 ringing pushed a legal edge to 236 — an open-loop mitigation, not a guarantee; the master is never legalised.

`run_qc` keeps **four authorities that never collapse into one**:
- **Technical**: full decode; frame count, PTS ledger, r_frame_rate, time_base, duration equal the reference; rotation 0, SAR 1:1, bt709×3 tags; legal luma (8-bit 16–235 via `signalstats`); PCM / payload / packet identity; audio start and time base.
- **Reference structure**: block ids and lengths; rendered boundaries = blueprint boundaries; clock and audio timeline identity; hard cuts within `BEAT_TOLERANCE_FRAMES = 2` of reference onsets, computed on the **candidate's own detected cuts**.
- **Visual**: reference-relative per-block medians — luma q50 |Δ| ≤ 0.10, saturation ratio 0.75–1.25 (or |Δ| ≤ 0.08 if reference saturation < 0.05), Lab neutral-axis Δ ≤ 12.0, no unexpected black (y_q95 < 0.03) or white (y_q05 > 0.97) frames (ship-blocking in reference-locked mode, diagnostic in original-montage); caption contrast `(hi+0.05)/(lo+0.05)` vs a 9×9 dilated ring ≥ `CAPTION_CONTRAST_MIN_RATIO = 1.8`; typography re-validated; a signed agent-review receipt (bound to candidate, reference, board, render receipt and reelctl version, with a fixed reviewer id and five booleans) is required — **a machine FAIL is never overridden by a receipt**.
- **Human approval**: always `PENDING` from `run_qc`; only the studio review room sets it.

`combine_authorities`: `REJECT` if the human rejected or any machine authority FAILed; else `BLOCKED` if any machine authority is BLOCKED/PENDING; else `APPROVED` if the human approved; else `LOCAL_REVIEW_READY`. QC reports are append-only (`qc-report-<sha12>.json`).

### 6.8 Caption engine (`captions/`)

| File | Role |
|---|---|
| `contract.py` + `caption-contract.schema.json` | strict schema (`additionalProperties:false`) + cross-field checks: unique ids, `frames == end−start`, treatment bbox ⊇ core bbox, distinct `stacking.z` on overlap, resolvable `inherits`; native-font styles need a holdout- or asset-proven hypothesis; `render_allowed` only when `SEALED`; named geometry exception `axis_scale` in [0.77, 1.30] with ≥ 2 evidence frames |
| `program.py` | converts inclusive ranges to half-open once, greedy stack slots, cross-checks blank frames |
| `render.py` | one uniform scale per state; progressive blur radius `0.5 + 3.5·strength`; refuses off-canvas placement; flat ink `rgb_median` |
| `fontproof.py` | holdout proof: disjoint train/holdout (split by sha256 of each word), ≥ 2 metrics each beating the runner-up by `MIN_HOLDOUT_MARGIN = 0.05` on every holdout word, within Dice ≥ 0.70, IoU ≥ 0.55, Chamfer ≤ 3.0 px |
| `ink.py` | moves only `states[].ink.rgb_median`; target 80 % of the reference ring Michelson contrast; `MIN_SEPARATION = 25`; flip rules `FLIP_SHORTFALL = 0.90`, `FLIP_MARGIN = 0.15`; WCAG `MIN_CONTRAST_RATIO = 3.0`, `TARGET_CONTRAST_RATIO = 4.5`; real PASS/WARN/FAIL; use `--base-from-contract` or ratified fills revert |
| `extract.py` | segmentation models (min-channel, chromatic difference, temporal difference, static ink), bbox tiers (`CORE_ALPHA 128`, `TREATMENT_ALPHA 8`), compositing-operator fit, mask tiering (`CLEAN_SEPARATION 60`, `SECONDARY_SEPARATION 30`) |
| `qc.py` | `qc_timing` (ink frames equal state spans exactly); `qc_state_parity` (Dice ≥ 0.90 crisp / ≥ 0.60 blur, 2 px codec band); `qc_word_identity` (verbatim text); `qc_ink_identity` (RGB distance ≤ 6.0 from the ratified fill); skipped gates listed in `gates_not_run` |

Known gap: the crisp-state gate should use Dice **and** one-way residual **and** topology (as caption-learning's `anatomy.py` does); the code gates on Dice + codec band only.

### 6.9 Reel Studio (dormant)

Config is environment-driven (`REEL_STUDIO_*`: `PROJECTS_ROOT, LIBRARY, REGISTRY, STORAGE_ROOT, DIR, HOST, PORT, PATH, MIN_FREE_BYTES(_INTERNAL), MAX_JUDGMENT_JOBS, HEARTBEAT_MAX_AGE_SECONDS, TICK_SECONDS, STAGE_TIMEOUT_SECONDS, CLAIM_TTL_SECONDS, MAX_ATTEMPTS, BUSY_BACKOFF_SECONDS, WITHHELD_BACKOFF_SECONDS, CRAFT_QUOTA, REVISION, OWNER, AGENT_DB, JUDGMENT_*`). Defaults: `127.0.0.1:7335`; storage root `/Volumes/WORKDRIVE`; min free 20 GiB on both volumes; tick 20 s; stage timeout 3 h; claim TTL 15 min; max attempts 2; backoff 60/300/900 s; withheld backoff 30 min; judgment model `opus` with `sonnet` fallback, permission mode `acceptEdits`, 1 concurrent job, 3 h wall, first-progress 600 s, stall 30 min, max park 12 h. `REEL_STUDIO_AGENT_DB` points at an optional agent-runtime activity database (read-only; leave unset).

| Module | Role |
|---|---|
| `daemon.py` | `Orchestrator.tick()`: preflight → reconcile → schedule → claim → run → wake; never crash-loops (a degraded box says why once and keeps beating); two attempts per project fingerprint, then terminal + one `call_required` |
| `stages.py` | classifies stages deterministic / judgment / conditionally deterministic / human-only |
| `runner.py` | deterministic stages as `reelctl` subprocesses (explicit PATH, cwd, timeout) |
| `judgment.py` | judgment stages as headless `claude -p` workers that may write artifacts but cannot advance state; capacity walls park the job; liveness watchdog |
| `preflight.py`, `_readprobe.py` | probes input paths in a killable child (a TCC-blocked read on an external volume **hangs** under launchd instead of failing); exit 3 = refused |
| `db.py`, `jobs.py`, `events.py` | `studio.db` SQLite (schema v2), disposable and rebuildable; SSE |
| `registry.py`, `library.py`, `opencalls.py`, `calls.py`, `intake.py` | read-only registry/library; open calls; hash-bound answers appended to a decision log; deferred project creation with a mode-ambiguity CALL |
| `status.py`, `board.py`, `views.py`, `health.py`, `claims.py`, `review.py`, `publish.py` | layer statuses from receipts; server-rendered board; two-lock approve with signed verdicts; the publish card is **manual-only** (no outward call) |

`web.py` refuses any non-loopback bind; actions are an allowlist (`ACTION_NAMES`). Deploy: `deploy/install.sh` copies `com.reelfactory.reel-studio` and `…-reel-studio-daemon` plists into `~/Library/LaunchAgents`, lints, `bootout`/`bootstrap`s only those two labels, polls `http://127.0.0.1:7335/api/health`; `uninstall.sh` reverses. Known defect: the daemon's status-cache upsert raised `UNIQUE constraint failed: status_cache.project_id`; rebuild `studio.db` and fix the upsert before reviving.

Other: `retrieval.shortlist_reference_roles` scores role terms ×5 + requirement ×3 + observation ×1.5 with motion/pose/duration/luma/saturation/sharpness tiebreaks and penalties (−12 static vs walking, −8 static vs sport, −20 duration shortfall); `top_n` 1–100; output always `MACHINE_SHORTLIST_REVIEW_REQUIRED`. `fixtures audit` reads `fixtures/real-regressions.json` (absolute media paths; replace with your own regressions).

---

## 7. Caption-learning — forensic caption battery (`caption-learning/`)

Every caption gate that predated it was a self-consistency check (timing vs contract, Dice vs our own plan). None read a delivered word or compared our ink to the reference's. Caption-learning answers four questions about a delivered reel:

| Gate | Question | Module |
|---|---|---|
| **W1 words** | Is it the right word, and does it read as that word? | `readback.py` + `wordtruth.py` (+ `w1_reclass.py`) |
| **W2 anatomy** | Are letterforms intact (no severed strokes, fused letters, filled counters)? | `anatomy.py` |
| **W3 ink** | Is the ink at least as clean against its bed as the reference's? | `inkcheck.py` (+ `w3_rescore.py`) |
| **W5 presence** | Is there caption ink where the contract says a caption is? | `sweep.presence_state`, `bandpresence.py` (W5b), `basediff.py` (W5c) |

Standalone: imports nothing from reelctl, sets no approval state, is not wired into the kit's render path — run it as the audit layer.

**Honesty vocabulary** (any change that breaks it is a regression): every gate returns `verdict` ∈ `{PASS, FAIL, UNMEASURABLE}` + measured values + `sample_size` + evidence paths; **a gate that measured zero pixels, frames or cells returns UNMEASURABLE, never PASS**; frame indices are zero-based decoded order everywhere (`select=eq(n\,K)`), no `t*fps` arithmetic; an unmeasurable reel is SKIPPED with a reason; Vision OCR confidence is **banned as a gate input** (it was anti-correlated with correctness).

Runtime: macOS Python with `pyobjc-framework-Vision` (+ Quartz/Cocoa), numpy, Pillow, opencv, scipy, pytest; `/opt/homebrew/bin/ffmpeg|ffprobe` hard-coded. `readback`/`wordtruth` are macOS-only; `anatomy`/`inkcheck`/`basediff`/`bandpresence` are pure OpenCV/NumPy.

### 7.1 `readback.py` — OCR ensemble gate

Runs Apple Vision `VNRecognizeTextRequest` on **isolated plates or tight upscaled crops, never full composite frames** (full-frame OCR returns garbage even on a pristine reference). The signal is **ensemble agreement**: the same ink re-rendered 12 ways and OCR'd separately; an intact word reads the same at every scale, fused/eroded strokes make it disagree with itself.

| Constant | Value |
|---|---|
| `ENSEMBLE_HEIGHTS` | `(60, 120, 240, 480)` px cap-height normalisations |
| `ENSEMBLE_PADS` | `(0.5, 1.5, 3.0)` × normalised height |
| `ENSEMBLE_CELLS` | 12 |
| `AGREEMENT_THRESHOLD` | **0.25** — derived: rejected states measured ≤ 0.08, accepted ≥ 0.50 |
| `STABILITY_FLOOR` | 1/3 (modal read share for `read_region`, else `UNRESOLVED_TEXT`) |
| `_MAX_CELL_PIXELS` | 40,000,000 |

Functions: `ocr_png`, `normalize_text`, `projections` (5: `luma, lab_a, lab_b, pc1, pc2`, so coloured ink on coloured ground separates), `build_cells`/`ensemble`, `readback_image(png, declared, out_dir=None, threshold=0.25)` (**gate**), `readback_pair(candidate, reference, ...)` (candidate must agree at least as well as the reference), `extract_frame`, `read_region(video, frame, bbox, out_dir, pad_frac=0.20)` (recovery primitive: RESOLVED / UNRESOLVED_TEXT / UNMEASURABLE, never guesses).

```bash
<PYTHON> caption-learning/readback.py image plate.png "<word A>" --out-dir /tmp/rb
<PYTHON> caption-learning/readback.py region reference.mp4 146 371,142,910,361 /tmp/rb
```
Soft spot: erasing 30–60 % of a word's strokes can still read back PASS — readback proves the *word*, not the *anatomy*; always run W2 alongside.

### 7.2 `wordtruth.py` and `w1_reclass.py`

`wordtruth` recovers the reference's own caption words and diffs them against what we declared (`declared_text`, `reference_read` via `read_region` over the state box, falling back to the lower-third crop `[0, 0.60·H, W, H]`; `words_match` ∈ `MATCH | MISMATCH | UNRESOLVED_TEXT`; unresolved excluded from arithmetic). Accepts both reelctl contracts and workbench `states.json`. `probe_frames(state, limit=4)` tries mid, mid±1, start, end−1, quartiles (a reference can be blank on a state's exact mid frame). `neighbours_in_crop` labels mismatches caused by a padded crop catching an adjacent caption.

```bash
<PYTHON> caption-learning/wordtruth.py project <project_dir> --out-dir /tmp/wt
<PYTHON> caption-learning/wordtruth.py triage contract.json reference.mp4 delivered.mp4 /tmp/wt [--states C19,C20]
<PYTHON> caption-learning/wordtruth.py plates contract.json plates/ /tmp/wt
```
**Known critical bug, corrected downstream:** `triage_delivered` marks a state `UNVERIFIABLE_BY_READER` when the reference crop does not read as the *declared* text, so a wrong-words state absolves itself. **Always act on `w1_reclass` output (`W1_corrected`), never the raw triage verdict.** `w1_reclass.reclassify(triage)` re-derives from the same reads: `WRONG_WORDS` (both resolved and differ → **blocking**), `WORDS_OK`, `UNREADABLE_DELIVERED` (advisory), `UNVERIFIABLE` (reader limit), and independently `CONTRACT_WORDS_WRONG`. Constants `STABILITY_FLOOR = 1/3`, `TRACK_AGREEMENT_THRESHOLD = 0.50`, `MIN_STATES_FOR_TRACK_VERDICT = 4`.

### 7.3 `anatomy.py` — the triple gate (W2)

Dice alone passes an amputated word. Three measurements, ANDed (`compare_masks(source_mask, candidate_mask)`): **Dice ≥ `DICE_MIN = 0.90`**; **one-way source→candidate boundary residual p95 ≤ `RESIDUAL_P95_MAX = 3.0` px** (a missing stroke explodes this while Dice barely moves); **topology equality** (significant component count and hole count match exactly). Normalisation: tight-crop, **one isotropic scale** to equal crop heights, centroid translate, bounded integer refinement `REFINE_PX = 6` — never independent x/y resize (that silently corrects the aspect/tracking error the gate exists to catch). Topology floors: `COMPONENT_AREA_FLOOR_FRACTION = 0.01`, `MIN_COMPONENT_AREA = 64`, `HOLE_AREA_FLOOR_FRACTION = 0.002`, `MIN_HOLE_AREA = 16`; below `MIN_INK_PIXELS = 64` / `MIN_BOUNDARY_PIXELS = 32` → UNMEASURABLE. `recover_state_mask(video, state, out_dir)` recovers a state's ink mask from a composited video (shot-bound detection, `cut_threshold=25.0`).

### 7.4 `inkcheck.py` — ink cleanliness (W3)

Per sampled frame: interior = mask eroded 1 px; ring = dilate(mask, 17) − dilate(mask, 7); separation = |median(interior luma) − median(ring luma)|; Michelson over those medians. Judged on the **worst** sampled frame. `MIN_SEPARATION_LUMA = 25.0`; `MIN_MICHELSON_RATIO = 0.80` × the reference's own ring Michelson; `MIN_INTERIOR_PIXELS 32`, `MIN_RING_PIXELS 64`; defaults `inner_dilate=7`, `outer_dilate=17`, `erode_px=1`, `registration_px=8`, `registration_step=2`. Verdict: separation < 25 → FAIL; ≥ 25 and ratio < 0.80 → FAIL ("muffled next to the reference"); reference Michelson missing → UNMEASURABLE; else PASS. Per-frame registration over ±8 px at step 2; an optimum on the window edge = unregistered, excluded. For animated captions pass per-frame masks. `w3_rescore.py` re-scores stored records under the corrected reference-relative rule (an earlier version computed it but never consulted it).

### 7.5 Presence probes (W5, W5b, W5c)

- **`sweep.presence_state`** (W5): ink fraction at the luma extremes inside the box, delivered vs reference: `W5_LIGHT_LUMA = 205`, `W5_DARK_LUMA = 50`, `W5_MIN_REFERENCE_FRACTION = 0.003` (below → UNMEASURABLE), `W5_MIN_RATIO = 0.25`, `W5_MAX_REFERENCE_FRACTION = 0.55` (a white field is not glyphs). Probe **both polarities**; a single-polarity probe reports opposite-polarity ink as missing (`merge_w5.py` merges a corrected pass).
- **`bandpresence.py`** (W5b, experimental): for contracts with no geometry, derives the caption band from pixels that go extreme (`LIGHT_LUMA 245` / `DARK_LUMA 15`) while a caption is up and never on blank frames; `MIN_COMPONENT_PX 120`, `MIN_BAND_PX 400`, `MAX_BAND_AREA_FRACTION 0.60`, `MIN_REFERENCE_FRACTION 0.002`, `MIN_RATIO 0.25`, `MIN_PRESENCE_RATE 0.80`, `MAX_BLANK_RATE 0.25`. It tends to return UNMEASURABLE; treat as experimental.
- **`basediff.py`** (W5c): if delivered == caption-free picture base, nothing was composited. Largest connected component of |Δ| ≥ `DELTA = 60`; `MIN_GLYPH_PX = 400`; PRESENT / ABSENT / UNMEASURABLE (misaligned geometry → UNMEASURABLE, never ABSENT).
  ```bash
  <PYTHON> caption-learning/basediff.py --base picture-base.mov --delivered review.mp4 --contract contract.json --out-dir /path
  <PYTHON> caption-learning/bandpresence.py --reference REF.mp4 --delivered DEL.mp4 --contract contract.json --out-dir /path
  ```

### 7.6 Drivers and calibration scripts

`sweep.py` (factory-wide parity sweep over a hard-coded `TARGETS` list, `FrameStore` decodes once and `verify_zero_based_indexing` proves the index against ffmpeg), `postfix.py` (re-runs the same gates over fixed bytes, reports `FIXED / STILL-BROKEN / REGRESSED / HELD / NOW-MEASURABLE / UNMEASURABLE`), `report.py` + `analysis.py` (generate the human work order from the JSON so tables cannot drift), `finalize.py`, `merge_w5.py`, `calibrate_readback.py`, `calibrate_word.py`, `verify_red_capability*.py` (corrupt a passing state — erase 30 %/60 % of strokes, erode, dilate-fuse — and confirm the battery turns red). The `TARGETS` lists and calibration paths are historical; on a new project call the gate functions directly (`wordtruth triage`, `anatomy.compare_masks`, `inkcheck`, `basediff`).

Tests: `cd ~/reel-production && <PYTHON> -m pytest caption-learning/tests -q -p no:cacheprovider` — `test_readback.py`, `test_wordtruth.py`, `test_anatomy.py`, `test_inkcheck.py`, `test_ground_truth_word.py`; `conftest.render_word` renders exact word plates in Arial Bold. Corpus-dependent tests **skip, never silently pass**; without pyobjc the readback/wordtruth tests fail at import.

---

## 8. Reel Deck (`apps/reel-deck/`)

The review web UI: watch a cut next to its reference, approve/reject/comment (whole-reel or frame-pinned), answer factory questions, drop new references, grade the footage library clip by clip. It **reads** the registry and a media index and **writes only** markdown receipts onto the bus (plus uploads and the grader's append-only grades file). It never posts, never writes `REEL_REGISTRY.json`, never schedules.

Stack: FastAPI + Starlette + Jinja2 + itsdangerous on uvicorn at `127.0.0.1:<DECK_PORT>` (default 7355), venv `.venv311` (Python 3.11). Known-good: fastapi 0.141.1, starlette 1.6.0, uvicorn 0.52.4, Jinja2 3.1.6, itsdangerous 2.2.0, python-multipart 0.0.32, httpx 0.28.1, pytest 9.1.1. No requirements file — recreate from that list. Browser walks need Playwright (`tests/walk.py`) or a CDP Chrome (`tests/cdp_walk.py`). Docs beside the code: `HANDOVER.md`, `CONTROLS.md`, `BUGS.md`.

### 8.1 Service and security

- launchd `com.reelfactory.reel-deck` (`config/launchd/`): KeepAlive, RunAtLoad, ThrottleInterval 5, logs `logs/deck.{log,err}`. The shipped plist runs `<deck venv>/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 7355` directly. With an external work volume on macOS, grant that interpreter the disk permissions it needs (L0086).
- **Exposure:** loopback only. Put it behind a private-network reverse proxy if you need phone access. **Never expose the Deck publicly without strong auth**; `/health` is public and discloses filesystem paths.
- `curl -s http://127.0.0.1:<DECK_PORT>/health` → build hash, uptime, factory light, registry path/writable, workbench status, index state.

### 8.2 `config.json` (secrets)

Keys `secret` (itsdangerous signing key) and `users` (`{username, salt, hash, role}`); mode 0600. The repo ships a template: generate a secret with `python3 -c "import secrets;print(secrets.token_hex(32))"`. Passwords are **scrypt** (N=2^14, r=8, p=1, 16-byte salt). Manage with `tools/deck-user add <name> [--admin]` (prints the password once) / `rm` / `list`; `tools/set_login.py` is the older single-user setter. Legacy single-user configs migrate on first load.

### 8.3 Module map

| Module | Role | Key constants |
|---|---|---|
| `main.py` | app, routes, session middleware, startup tasks, `/health`, log rotation | `PUBLIC_PATHS = {/login, /health, /api/client-error, /favicon.ico}` (+ `/static/*`); `LOG_ROTATE_BYTES 20 MB` copy-then-truncate, keep 5; `AUDIT_DIR`, `HOWTO_VIDEO` (absolute, deployment-specific) |
| `auth.py` | users, scrypt, roles, signed cookies, rate limit | roles `OPERATOR` (receipts carry full reviewer authority) and `EDITOR` (advisory verdicts; factory controls hidden); cookie `deck_session` = `TimestampSigner(secret, salt="deck-session")` over `username\|role\|nonce`; `SESSION_MAX_AGE` 30 days, httponly, samesite lax, Secure when proxied; role re-read every request; `FAIL_LIMIT = 8` per `FAIL_WINDOW = 600 s` (in memory); `CONFIG_PATH` absolute |
| `registry.py` | read-only registry + media index + TCC guard + lane classifier | `REGISTRY_TTL_SECONDS 30`; index `INDEX_MAX_DEPTH 5`, `INDEX_ROOT_FILE_CAP 5000`, skips `footage-library, masters, allframes` and frame dumps, `INDEX_TTL_SECONDS 60`, persisted to `index-cache.json` and loaded at startup, cold wait `COLD_WAIT_SECONDS 10`; `MEDIA_EXTS {.mp4,.jpg,.png,.md}`; TCC probe in a thread (`PROBE_TIMEOUT_SECONDS 6`, re-probe `PROBE_TTL_SECONDS 300`) because a denied read hangs; lane classifier keys on the **head** `__` segment of `review_state` (prose later in the string must not move a row) |
| `board.py` | four lanes NEEDS YOU / IN THE MACHINE / DONE / PARKED; factory light; open questions | era filter from `board.json` (`DEFAULT_CURRENT` if missing); `CONTROL_DIR = $DECK_CONTROL_DIR or ~/reel-production/_control`; factory awake if heartbeat age < **480 s**, or `running` on a receipt < 65 min with the worker process alive; `launching` → awake until 900 s then "launch-failed"; CALL ids with a real RULING are hidden |
| `receipts.py` | **the only writer**: bus receipts, send status, CALL parsing | kinds `ui-drop-<ts>`, `ui-feedback-rowNN-<ts>`, `ui-frame-note-rowNN-<ts>` (hash-bound to file + frame), `ui-batch-verdict-rowNN-<ts>`, `ui-continue-<ts>` (`CONTINUE_MIN_INTERVAL 600 s`), `ui-rules-<scope>-<ts>`, `ui-smoketest-rowNN-*`; each carries `Sent by: <user> (<ROLE>)`; same-second sends get `-2`, `-3` suffixes; `_scrub()` redacts the terms in `REEL_FACTORY_REDACT_TERMS_FILE`; structured `<!--status {…} -->` blocks win over prose (L0036); `WAIT_S 300`, `PICKUP_TRUST_S 3 h`, `UNANSWERED_LOOKBACK_S 48 h`; `QUESTION_SHORT 220` chars on the card (full text kept to 4,000) |
| `eta.py` | "review ready ~X" from bus history | `MIN_SAMPLES 3` (else `PRIORS`), `MAX_GAP_HOURS 48`, `DEDUPE_MINUTES 90`, `DEFAULT_FIX_ROUNDS 3.0`, `MIN_REMAINING_HOURS 0.25`, `MODEL_TTL_SECONDS 3600`; always labelled an estimate |
| `lanes.py` | live lane activity from agent transcripts | `WORKFLOW_GLOB = ~/.claude/projects/-Users-operator/*/subagents/workflows/wf_*` (**rewrite the user slug**); `ALIVE_S 900`, `FRESH_RUN_S 6 h`, 64 KB tail; only role line, tool names and basenames reach the payload |
| `media.py` | confined, ranged file serving | `.mp4 .jpg .png .md` only; realpath-confined; **reel-scoped**; TCC-guarded; HTTP Range; `CHUNK 512 KB` |
| `process.py` | read-only whole-build PROCESS view per row | lists study files, casts, renders and gate outcomes, look sheets, receipts |
| `grader.py` | footage-library clip grader | reads `FOOTAGE_LIBRARY.json`, `<FOOTAGE>/{person_scores,usage}.json`, `identity_pool.json`; **writes only** `_receipts/clip-grades/grades.jsonl`, one line per tap; `GRADES = (HERO, BROLL, NEVER)`, `IDENTS = (ME, NOTME)` (whole-clip); `SEG = 5.0` s pieces, clips ≤ `ONE_PIECE_MAX = 7.0` s are one piece; latest line per `(stem, t0, t1)` wins per field; a whole-clip line never overrides a segment line |

Routes: pages `/`, `/row/{seq}`, `/row/{seq}/process`, `/make`, `/now`, `/help`, `/grader`, `POST /logout`. API: `GET /api/board`, `/api/row/{seq}`, `/api/row/{seq}/process`, `/api/now`, `/api/row/{seq}/sends` (polled every 20 s); `POST /api/verdict` (APPROVE | REJECT | NOTES; REJECT requires a reason), `/api/batch-verdict` (KEEP | KILL | NOTES per variant), `/api/frame-note` (multipart, images ≤ 15 MB), `/api/drop`, `/api/continue` (rate-limited); `GET /media?p=` (ranged, confined); `POST /api/client-error` (public, 4 KB cap); grader `GET /api/grader/items|summary`, `POST /api/grader/grade|grade-many` (OPERATOR only). Startup: rotate logs, ACK every `ui-smoketest-*` receipt itself (the order daemon skips smoketests by design), load the index cache, rebuild in the background. Any send with `?walk=1` becomes a smoketest receipt with a TEST banner — the plumbing walk never orders a build.

Lessons enforced by Deck tests: L0021 (blocked pickup reads as queued, not fixing), L0027 (queued pickup reaches the board card), L0035 (file names recorded only as dict keys are still declared), L0036 (structured status beats prose). L0012 (verify UI state by computed visibility; `[hidden]{display:none !important}` stays in `deck.css`) is rule-only.

```bash
cd ~/apps/reel-deck && .venv311/bin/python -m pytest -q tests -p no:cacheprovider   # test_app.py, test_grader.py, test_questions.py
python3 tests/walk.py --row 7 [--width 1280] [--slow]                             # Playwright walk (posts test-mode receipts)
```
The unit suite is isolated (fixtures monkeypatch `auth.CONFIG_PATH`, `receipts.BUS_DIR`, `receipts.UPLOADS_DIR`, grader paths into `tmp_path`). Walks post test-mode receipts onto the live bus; the Deck ACKs them itself; nothing builds.

---

## 9. Dependencies and versions

There were four Python environments in the original deployment; the version spreads are real (numpy 1.26 vs 2.x, pydantic 1 vs 2). On a fresh machine you can merge the tools and caption-learning environments into one.

**Tools + kit + caption-learning + orderd** (`<PYTHON>`; known-good on Python 3.9):

| Package | Known-good | Used by |
|---|---|---|
| numpy | 1.26.4 | everything numeric |
| opencv-python-headless | 5.0.0.93 | kit measures, caption-learning |
| Pillow | 11.3.0 | typesetting, sheets, readback |
| scipy | 1.13.1 | grade solves, sharpness, anatomy |
| fontTools | 4.60.2 | `captions_typeset`, `faceid trace` |
| pyobjc-framework-Vision / -Quartz | 12.0 | `faceid`, `refpeople`, `castscan`, `identity`, `ditl`, `readback` (macOS only) |
| librosa | 0.11.0 | `ditl` beat tracking |
| pydantic | 1.10.26 | some top-level tools (v1 API) |
| jsonschema | 4.25.1 | contract checks |
| httpx | 0.28.1 | asker |
| pytest | 8.4.2 | all tools-side tests |
| playwright | 1.60.0 | Deck walk tests |

```bash
<PYTHON> -m pip install --user "numpy==1.26.4" "opencv-python-headless==5.0.0.93" "Pillow==11.3.0" \
  "scipy==1.13.1" "fonttools==4.60.2" "pyobjc-framework-Vision==12.0" "pyobjc-framework-Quartz==12.0" \
  "librosa==0.11.0" "pydantic==1.10.26" "jsonschema==4.25.1" "httpx==0.28.1" "pytest==8.4.2" "playwright==1.60.0"
```

**ASR environment** (`whisper_guard`, `vocal_guard`): Python 3.14 with openai-whisper 20250625, torch 2.11.0, numpy 2.4.4; demucs in a separate venv (`vocal_guard.VENV_PY`).

**Deck venv** (Python 3.11): see §8. **reelctl venv** (Python 3.13, uv): `uv sync` in `_reelctl/`.

**System binaries:** ffmpeg/ffprobe 8.1.x with `lut3d`, `colorspace`, `zoompan`, `libx264`, `prores_ks` (the kit rasterises captions itself, so `drawtext`/`libass` are not needed); exiftool 13.x (reelctl colour proofs); tmux 3.x (orderd); uv 0.11.x (reelctl); Claude Code CLI `claude` (lanes); yt-dlp (optional intake — respect platform terms); DaVinci Resolve (optional hand-off only).

**Fonts:** the kit defaults to macOS Helvetica Neue (system) and Pinyon Script (SIL OFL — download it and keep its licence file). Any identified commercial face must be licensed for publishing; traced `Ref*` faces built by `faceid trace` are local matching aids, not for redistribution.

**LUT:** the house look needs your camera's log→display LUT (e.g. the vendor's LC-709 cube for S-Log3/S-Gamut3.Cine). Not shipped.

---

## 10. Running the test suites

| Suite | Command | Notes |
|---|---|---|
| one-to-one kit | `cd ~/reel-production/tools && <PYTHON> -m pytest -p no:cacheprovider -q onetoone/tests` | the real code-health signal; data-gated tests skip; `test_identity_pool.py` fails until you populate your pool and rewrite its fixtures |
| top-level tools | `cd ~/reel-production/tools && <PYTHON> -m pytest -p no:cacheprovider -q test_reuse_map.py test_variant_caster.py test_variant_render.py` | `test_reuse_map.py` and parts of `test_variant_caster.py` read a live registry/blacklist — treat failures as "data not present"; `test_variant_render.py` needs `_reelctl/src` importable |
| orderd + asker | `cd ~/reel-production/tools/orderd && <PYTHON> -m pytest -p no:cacheprovider -q tests` | run from inside `orderd/`; needs tmux |
| caption-learning | `cd ~/reel-production && <PYTHON> -m pytest -p no:cacheprovider -q caption-learning/tests` | corpus tests skip; macOS + pyobjc for readback |
| reelctl | `cd ~/reel-production/_reelctl && uv run --extra dev pytest -q` (read-only variant: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/reelctl-pytest`) | `conftest.py` blocks all non-loopback network unless a test is marked `@pytest.mark.network`; `tests/captions/test_ink_resolution.py` needs fixture plates that are not shipped; one colour test skips without a real camera clip |
| Reel Deck | `cd ~/apps/reel-deck && .venv311/bin/python -m pytest -p no:cacheprovider -q tests` | isolated; FastAPI `on_event` deprecation warnings are expected |

Expect skips wherever a test needs a real reel brain, master, or system font. **A skip is not a pass** — before trusting a gate on new hardware, give each data-gated test a fixture from your own project.

---

## 11. Known bugs, consolidated

### 11.1 Blockers on a new machine (fix before the first render)

| # | Where | Symptom | Fix |
|---|---|---|---|
| 1 | `onetoone/preflight.py` `machine()` | measures `<WORKDRIVE>` against `WORKBENCH_MIN_GB = 100`; with no such path it reads 0 GB and **always blocks** | point it at your work disk; keep a floor |
| 2 | `onetoone/looksheet.py` `LUT`, `onetoone/housechain.py` `LUT_PACKAGED` | two absolute paths to the same LUT; the LUT is not shipped | supply the LUT once; point both at it; record its hash (L0018) |
| 3 | `onetoone/ffx.py` `FFMPEG` (+ direct calls in `castscan`, `refpeople`, `looksheet`, `vocal_guard`, `ditl`, caption-learning) | `/opt/homebrew/bin/ffmpeg` hard-coded | one constant via `shutil.which` |
| 4 | `/Volumes/WORKDRIVE/workbench` literals (`lessons.WORKBENCH`, `ditl.LIBROOT`, `looksheet.FOOTAGE`, `grader.FOOTAGE_ROOT`, `registry.WORKBENCH_ROOT`, `variant_render` defaults) | empty globs; `lessons harvest` silently finds nothing | rewrite to `<WORKDRIVE>` |
| 5 | macOS-only pieces: Vision via pyobjc, `memory_pressure`/`sysctl`, system `.ttc` fonts | import errors / always-refuse on Linux | stay on macOS or write replacements (no fallback exists) |
| 6 | macOS privacy (TCC) on an external work volume | lanes and the Deck hit `EPERM` or hang on reads | grant the runner's and the Deck's interpreters the disk permissions they need (L0086) |
| 7 | `orderd/lane_prompt.py` | refuses to build a prompt without a `workflows/scripts/reel*-build.js` LAW block; `STANDING` is deployment-specific | write your LAW file; rewrite `STANDING` (§5.2) |
| 8 | `orderd/claude-settings.json` | `acceptEdits` + empty allow-list | add the commands your lanes need |
| 9 | `identity_pool.json` empty | every identity slot fails `check_cast` (correctly) | populate from human rulings |

### 11.2 Correctness bugs in live code

1. **One-ffmpeg lock bypassed** by `castscan.grab`, `looksheet.extract_frame`/`build.ref_frame`, `refpeople.scan`/`probe` (bare `subprocess.run`). The lock has **no timeout**; a wedged ffmpeg blocks every kit call until killed.
2. **Caption readability looks parts up by text** (`captions_typeset.readability`, `castscan.caption_boxes`) — repeated text in one state is judged with the last part's ink. Worked around by study rule L0066.
3. **The kit's readability gate samples one frame per state** (settled mid frame); entry frames and per-glyph burial are lane-side (L0104, L0106, L0109).
4. **`identity`'s person rule passes with a warning** when `brain/refpeople.json` is missing.
5. **`refit` ignores `face_file`**; `refit.MIN_SCORE = 0.45` is dead (live threshold `REF_FIT_MIN_SCORE = 0.6`).
6. **First-reel constants leak**: `render.eye_sheet` picks, `devicesheet.DEFAULT_IDS`, `looksheet.build(grade_slots=…)`, `measure_devices.EYE_ONSETS` and `swaps`.
7. **Fixed 1916×1078** in `framing._zoompan_for` (and default aspect in `filter_for`/`effective_centre`, `upscale` computed against 1916).
8. **In-place brain rewrites without backup** in `refit.measure` (indent 1) and `ornate_env.measure` (indent 2).
9. **Frame-name conventions differ**: `refit` `NNNN.png`; `measure_devices`/`refstrip`/`devicesheet` `ref-NNN.png` (+ `our-NNN.png`, `caption-NNN.png`); `ornate_env` `{sid}_{frame}_ref.png`. Keep two frame dumps or normalise.
10. **`bedprobe` ignores shadows**; `castscan`/`bedprobe`/`looksheet` scan through bare `lut3d`, not the house pipe.
11. **`castscan` takes a `cast.json` it never uses**; its default (all masters) can write thousands of PNGs.
12. **`wordtruth.triage_delivered` can pass wrong words** — act on `w1_reclass`.
13. **`lessons.py` dedupe key** (`source::kind::first 80 chars`) can merge distinct signals; `_row_of` can mis-attribute.
14. **`reuse_map`** does not read the kit's `cast_vNNN.json` schema.
15. **reelctl**: status-cache `IntegrityError` in the studio daemon; caption crisp gate area-only; engine defaults to trace-first captions while the live rule is typeset; review-encode luma headroom open-loop; ffmpeg upgrades invalidate reference locks; signing key machine-local.
16. **`faceid trace` crops tight**: `cmd_trace` builds its mask with `pad=4`, under the ≥ 8 px margin L0085 asks for, so glyphs touching the box edge can trace with ledges. Pass a box with generous margin (or raise the pad) until fixed.

### 11.3 Operational and stale state

- Docstring drift: `orderd.py` header ("one lane at a time", "3 h wall") vs code (`MAX_LANES = 3`, 5 h); `render.py` says captions are static.
- Deck: `@app.on_event("startup")` deprecated (migrate to a lifespan handler); `/health` discloses paths; no pause control in the UI (use `_control/orderd.PAUSED`); inline playback on real iOS Safari unproven; batch Keep/Kill on device unproven.
- Never run two always-on workers against one bus — they double-pick orders. If you revive an always-on worker, make sure its relaunch counter resets after a failed launch (see [01_ARCHITECTURE.md](01_ARCHITECTURE.md), failure modes).
- Servers that bind `0.0.0.0` (`ditl/serve.py`): change to `127.0.0.1` on any machine with a public interface.
- Machine-local secrets never travel: reelctl signing key, Deck `config.json`, asker bot file.

---

## 12. Porting checklist

1. **Layout.** Create `~/reel-production/` (copy `code/reel-production-tools/` to `tools/`, `code/caption-learning/` to `caption-learning/`, `code/reelctl/` to `_reelctl/`), `~/apps/reel-deck/` (from `code/reel-deck/`), and your work disk `<WORKDRIVE>` with `workbench/footage-library/`. Create `~/reel-production/_receipts/bus/` (the bus), `_receipts/clip-grades/`, `_control/`.
2. **Paths.** Replace `/Volumes/WORKDRIVE` across `tools/`, `caption-learning/` and the Deck (`grep -rl '/Volumes/WORKDRIVE' … | xargs sed -i '' 's#/Volumes/WORKDRIVE#<WORKDRIVE>#g'`, then review the diff). Fix `preflight.machine()`. Rewrite `lanes.WORKFLOW_GLOB`'s user slug. Rewrite `faceid` helper paths or accept that `identify`/remote `find` are unavailable.
3. **Binaries.** Install ffmpeg 8.1.x; set `ffx.FFMPEG`/`FFPROBE` and the bare calls in `castscan`, `refpeople`, `looksheet`, `vocal_guard`, `ditl`, caption-learning. Install tmux, exiftool, uv, the `claude` CLI.
4. **Colour.** Obtain your camera's log→display LUT; point `housechain.LUT_PACKAGED` and `looksheet.LUT` at it; record the sha256; update `data/color-profiles.json` in reelctl if you use it.
5. **Fonts.** Install the open default faces (Pinyon Script, OFL) or set `DEFAULT_FACES` to faces you license. Identify and license each reference face you publish with.
6. **Identity.** Fill `identity_pool.json` from explicit human rulings; rewrite `tests/test_identity_pool.py` for your stems; write your redaction list and point `REEL_FACTORY_REDACT_TERMS_FILE` at it.
7. **Python.** Install §9 packages into `<PYTHON>`; build the Deck venv and the reelctl venv; optionally the ASR environments.
8. **Agent lanes.** Write `~/reel-production/workflows/scripts/reel01-build.js` with a `const LAW = \`…\`` block holding your house rules; rewrite `lane_prompt.STANDING`; fill the `permissions.allow` list in `orderd/claude-settings.json` (ships as `acceptEdits` with an empty list); on macOS with an external work volume, grant the runner's interpreter the disk permissions it needs (L0086).
9. **Kill switch.** Write `_control/authority.json` with a `factory_state` starting `FACTORY_OPEN` to let orderd run (keep reelctl closed unless you deliberately want it).
10. **Deck.** Generate `config.json` (`secret` + users via `tools/deck-user`); edit the plist to call uvicorn directly; keep it on loopback or a private network.
11. **Asker (optional).** Create a bot config outside the repo and set `ASKER_TELEGRAM`, `ASKER_DECK_URL`.
12. **launchd.** Edit absolute paths inside `config/launchd/*.plist` (they use `~`, which launchd does not expand — write full paths), copy to `~/Library/LaunchAgents/`, `launchctl bootstrap gui/$(id -u) <plist>`. On Linux use systemd user units with the same commands (see [01_ARCHITECTURE.md](01_ARCHITECTURE.md)).
13. **Verify.** Run every suite in §10; run `preflight --row 1 --skip-tests` to see the floors print; do one dry `orderd.py --once --dry-run` against a smoketest receipt before letting a real lane start.

---

## 13. Not included in this repository

These existed alongside the original system and are referenced in some docstrings, but are not shipped:

- the house LUT `.cube` and any font files (licensing — supply your own);
- an always-on worker + healer (superseded by `orderd`);
- a match-cut mining research pipeline (Apple Vision feature prints, pose and silhouette matching over a whole library);
- the reel registry, footage library, lessons ledger data, reel brains, receipts and fixtures of the original deployment;
- any posting or scheduling automation. This system delivers locally and **does not post**: uploading and publishing are always done by a human, outside the factory.
