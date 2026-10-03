# reelctl

`reelctl` is a deterministic media engine designed to be driven by an AI agent skill (e.g. a `reference-driven-reel-production` skill). It turns a locked short-form reference plus an authorized footage pool into an append-only local review candidate with exact clock/audio contracts, block-complete editing, hash-locked typography/effects, source-aware log handling, and separate QC authorities.

## Install

```bash
uv tool install --editable .     # puts `reelctl` on ~/.local/bin
```

Installed editable, tested source changes are immediately visible to the executable. Locations are environment-driven: `REEL_FACTORY_HOME` (projects root, default `~/reel-production`), `REEL_FACTORY_WORKDRIVE` (external work volume, default `/Volumes/WORKDRIVE`), `REEL_FACTORY_TZ` (display timezone, default `UTC`).

## Health

```bash
reelctl doctor
reelctl fixtures audit --manifest path/to/your-regressions.json
uv run --extra dev pytest -q
```

The fixture audit replays a regression manifest against your own media (the shipped `fixtures/real-regressions.json` is a template with placeholder hashes). Each entry pins a candidate by SHA-256 and states its expected verdict:

- an accepted candidate with its acceptance evidence;
- an unresolved candidate whose QC passed but human approval is still pending;
- rejected candidates, including the "32 reference states versus 16 selected slots" false-PASS mechanism and a human rejection that reference-relative QC must reproduce.

## Exactness contract

In `reference-locked` mode, `reelctl` hard-locks controllable properties:

- exact reference bytes and SHA-256;
- full decoded frame count and PTS ledger;
- rational frame rate/time base/PTS step;
- audio payload and decoded PCM identity of the muxed track (a licensed track by default);
- every locked picture-state interval and hard-cut boundary;
- normal-speed source windows and isotropic crop geometry;
- exact-font hashes or traced reference glyph plates;
- exact static/per-frame effect plates;
- verified camera/log transform followed by per-shot creative grade;
- master/review codec, geometry, SAR, Rec.709 tags, decode status, hashes, and recipe identity.

Different source footage cannot be pixel-identical to a reference scene. The engine therefore requires independently observed role/action/composition evidence and fails closed when the authorized footage library cannot supply a required literal role. It does not hide a missing scene with grading, speed warps, fake blur, fake RGB, or synthetic rescue effects.

## Quick start

```bash
reelctl new <PROJECT_ID> \
  --reference '/path/to/reference.mp4' \
  --footage '<AUTHORIZED_FOOTAGE_ROOT>' \
  --audio-track '/path/to/licensed-track.m4a' \
  --mode reference-locked

reelctl run <PROJECT_ID> \
  --through local-review \
  --revision v001 \
  --default-profile sony_slog3_sgamut3cine
```

`--reference` takes a local file path (recommended). A URL is also accepted and downloaded with yt-dlp; downloads must respect the platform's terms of service and the creator's rights, and the CLI prints that notice before any download. `--audio-track` is the licensed track the render muxes (default audio policy `licensed_track_required`).

`run` advances deterministic stages and stops with structured `BLOCKED` instructions at visual reasoning gates. The driving agent skill inspects the referenced video/boards, writes the evidence contract, and runs again. Autonomous work stops at `LOCAL_REVIEW_READY`; the engine never self-approves publication.

## Local Reel Studio dashboard

Start the local-only web control plane over the same state machine:

```bash
reelctl serve
```

Then open `http://127.0.0.1:7335`. The dashboard supports explicit-mode project creation, durable status, resumable advancement, and a Stage workbench that exposes only the allowlisted engine actions (`/api/projects/{id}/actions`). Payloads are structured JSON; invalid order/proof is still blocked by reelctl. It binds only to loopback and deliberately has no publication endpoint. The full SaaS contract is in `docs/REEL_STUDIO_SAAS_SPEC.md`.

## Main commands

```text
reelctl doctor
reelctl fixtures audit
reelctl serve [--host 127.0.0.1] [--port 7335]
reelctl new ...
reelctl status <ID> --json
reelctl run <ID> --through local-review --revision v001

reelctl reference analyze <ID> [--source FILE]
reelctl blueprint lock <ID> --boundaries '...' --observations observations.json
reelctl footage index <ID> --default-profile ...
reelctl footage shortlist --blueprint BLUEPRINT.json --catalog CATALOG.jsonl \
  [--features FEATURES.json] --output SHORTLIST.json [--top-n 20]
reelctl variants validate --manifest FAMILY.json --output FAMILY_VALIDATION.json
reelctl selection template <ID>
reelctl selection validate <ID>

reelctl color setup <ID> --profile sony_slog3_sgamut3cine
reelctl color propose <ID>
reelctl color validate --selection FILE

reelctl typography inventory <ID>
reelctl typography match <ID> --text TEXT --mask MASK.png
reelctl typography extract-mask --image FRAME.png --output MASK.png --roi X1 Y1 X2 Y2
reelctl typography trace --mask MASK.png --output PLATE.png

reelctl assets template <ID>
reelctl assets lock <ID>
reelctl render <ID> --revision v001
reelctl qc <ID> --revision v001
reelctl review record-agent <ID> --revision v001 --status PASS ...
```

Run any command with `--help` for live flags.

## Project layout

```text
<PROJECT_ID>/
├── project.json
├── state.json
├── reference/
│   ├── reference-source.mp4
│   ├── reference-lock.json
│   ├── blueprint-draft-board.jpg
│   └── blueprint.json
├── footage/
│   ├── footage-index.json
│   └── thumbnails/
├── edit/
│   ├── selection.json
│   ├── selection.locked.json
│   └── render-v001/
├── assets/
│   ├── luts/
│   ├── assets.json
│   ├── assets.locked.json
│   └── overlay-locked/
├── review/
│   ├── color-grade-proposal/
│   ├── qc-v001/
│   └── agent-visual-review-v001.json
├── deliver/
└── .reelctl/cache/
```

## State model

The ordered stages are:

```text
REFERENCE_LOCKED
BLUEPRINT_LOCKED
FOOTAGE_INDEXED
FEASIBILITY_REPORTED
SELECTION_LOCKED
ASSETS_LOCKED
RENDERED
TECHNICAL_QC
STRUCTURE_QC
VISUAL_QC
LOCAL_REVIEW_READY
HUMAN_APPROVED
DELIVERY_APPROVED
DELIVERED
```

Changing an upstream recipe marks completed downstream stages stale. Writes are atomic. Render revisions are append-only. Cache reuse requires the complete source/selection/color/asset/clock recipe hash.

## Reference analysis

Reference analysis records:

- full source hash/size;
- FFprobe video/audio facts;
- every decoded PTS;
- exact clock and frame count;
- audio payload and decoded PCM hashes;
- audio onset evidence;
- draft hard-cut scores;
- a draft picture-block board.

Automatic cut detection is deliberately not final authority. The reviewing agent must inspect the full reference and lock all hard cuts, held transitions, insert pulses, pose jumps, caption states, and effects.

All new runtime intervals are zero-based and half-open:

```text
[start_frame, end_frame_exclusive)
```

## Footage selection

For a large pre-indexed corpus, build a deterministic machine-only retrieval funnel before visual selection:

```bash
reelctl footage shortlist \
  --blueprint reference_blueprint.json \
  --catalog clip_editorial_catalog.jsonl \
  --features thumbnail_features.json \
  --output role-shortlist.json \
  --top-n 20
```

The artifact is hash-bound to the blueprint, catalog, and optional feature file and always remains `MACHINE_SHORTLIST_REVIEW_REQUIRED`. Review broad coverage boards, then normal-speed renderer-exact crop proxies. A score cannot grant role fit, feasibility, selection lock, visual PASS, or approval.

After one master passes local review, a separate variant family can reuse its clock, audio, captions, effects, and renderer while substituting real source clips. `reelctl variants validate` enforces a conservative local production-diversity baseline: at least five siblings; a new, sibling-unique opening source; at least two and 30% changed visual slots; unique clip packages; and independent hash/QC/full-watch evidence before `LOCAL_REVIEW_READY`. This is a creative-variety check only: it makes no claim about how any platform treats similar content, never authorizes posting, and does not replace following each platform's rules.

A reference-locked selection requires one slot per locked picture state. Every slot records:

- exact block ID/frame count;
- exact source path/start frame;
- independently observed candidate action;
- normal speed/no reverse;
- crop anchor;
- input profile, verified LUT, lighting family;
- shot-scoped creative grade and evidence.

The validator rejects merged blocks, copied reference prose, wrong frame totals, missing source identity, reverse, and speed changes.

## Log color

The packaged Sony path uses a byte-locked official LC-709 LUT:

```text
SHA-256 d41aeebbca5c4df100f1e6b53b739bd1e7bc38e49890812a14ba7275ebfbbf94
```

It is valid only when the source is positively identified as Sony S-Log3/S-Gamut3.Cine. “Flat-looking” is not profile evidence.

Color is two separate stages:

1. shared technical normalization for the same verified camera profile;
2. bounded source-aware creative matching per shot.

`color propose` produces technical-normalization/reference boards and bounded luma/contrast/saturation diagnostics. A proposal is not accepted until the reviewing agent inspects skin, neutrals, highlights, shadows, and continuity and marks the slot proof `AGENT_REVIEWED`.

One creative recipe reused across unrelated lighting families fails the contract.

## Exact typography fallback

The exact route is:

1. original font file SHA-256 + face index; or
2. a hash-locked traced reference glyph RGBA plate when the wording is unchanged.

Font similarity ranking helps discovery but can never satisfy exact proof on its own. The traced route is an analysis and matching aid: it preserves the reference's visible glyph silhouette so a local study render can be compared against it. It is not a publishing asset. For anything you publish, the default is a licensed or open font (route 1 with a font you hold a licence for, or an open-licensed face such as SIL OFL); disclose when it is a close match rather than the original. The traced route does not support changing the wording while claiming the unavailable font is exact.

Typography/effect layers are compiled into a complete full-canvas RGBA sequence before render. Missing frames, wrong hashes, guessed fonts, and unproven effect plates fail the asset lock.

## Render and audio

Each selected source segment is rendered at native motion to its exact target frame count, normalized/cropped/graded, and cached as ProRes. Segments are concatenated on the reference clock. Locked overlays are composited. The audio track is stream-copied into master and review outputs, then its payload, packet ledger and decoded PCM are compared with the source track. The default audio policy is `licensed_track_required`: supply a track you hold rights to with `reelctl new ... --audio-track PATH` (cut to the reel's length). The reference's own audio is used only under the explicit opt-in `--reference-audio-rights-held`, and only if you hold the rights to it; either way the reference is analysed for timing.

Outputs:

- ProRes 422 HQ, 10-bit `yuv422p10le` master;
- H.264 `yuv420p` review;
- square pixels;
- limited-range Rec.709 tags;
- complete hashes and decode receipts.

## QC authorities

QC never collapses these states:

```json
{
  "technical_integrity": "PASS|FAIL",
  "reference_structure_parity": "PASS|FAIL",
  "visual_parity": "PASS|FAIL|BLOCKED",
  "human_creative_approval": "PENDING|APPROVED|REJECTED"
}
```

A technical/audio pass cannot hide missing picture states, wrong typography, bad color, or a rejected creative result.

Visual PASS requires a candidate-hash-bound agent review receipt after:

- complete normal-speed watch with audio;
- exact reference/candidate board inspection;
- typography check;
- color check;
- cut/beat check.

Human approval remains pending at local handoff.

## Development

```bash
cd code/reelctl
uv run --extra dev pytest -q
python3 -m compileall -q src tests
uv tool install --editable . --force
```

Add a failing test before changing contracts or rendering behavior. Keep old accepted/rejected media immutable. Do not add reel-specific renderer forks.
