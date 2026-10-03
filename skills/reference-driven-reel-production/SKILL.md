---
name: reference-driven-reel-production
description: Produce or revise a short-form reel that follows a reference reel - either an exact 1:1 adaptation (same clock, blocks, captions, audio) or a grammar adaptation (same rule system, your own footage). Use for any request to make, remake, fix or revise a reel "like this one", including casual "quick demo" or "just a test" requests; runs the stage machine from source lock through feasibility, casting from a footage library, build, audit and local delivery.
---

# Reference-Driven Reel Production

A reel built from a reference is a contract with that reference. This skill is the front door and stage machine; the detailed methods live in sibling skills (`reel-reference-forensics`, `reel-feasibility-gates`, `log-footage-colour-pipeline`, `caption-typesetting-and-font-matching`, `reel-visual-qc`, `reel-review-loop`).

## Front door (no speed exemptions)

1. Every reel - including "quick demo", "proof of capability", "doesn't have to be perfect" - goes through the same stages. Those phrases describe polish, not permission to skip gates.
2. Work inside a project directory with durable state on disk (project manifest, stage locks, append-only receipts) and a declared **authorized footage root**. A scratch folder plus a one-off script is a tool, not a project.
3. Rendering stays disabled until the reference is locked and the footage root is declared. Missing either -> stop and ask.
4. **The reference and any research downloads are never render sources.** They carry another creator's footage and burned-in captions. Enforce mechanically: a selection or render whose source path resolves outside the authorized root fails.
5. A bypassed render is not repaired forward. Patching each complaint on a reel that skipped the front door (re-crop, then whitelist, then...) never fixes the cause. Redo from the stage that was skipped.

Rights: use footage you own or have licensed, licensed audio, licensed or open fonts, and get permission for close recreations of another creator's edit. Respect platform terms.

## Choose the mode

| Mode | Contract | Typical phrasing |
|---|---|---|
| **exact adaptation (1:1)** | Preserve the reference clock, picture blocks, audio, caption/effect timing and transition lifecycle. Missing literal scenes stay declared gaps. | "recreate this", "same cadence", "the font changed mid-reel, fix it" |
| **grammar adaptation** | Copy the rule system - role progression, rhythm, typography family, energy arc, cue logic, ending - using your own footage. "Original" refers to the footage assembly only. | "make one like this", "same vibe", "use my clips" |

If ambiguous, ask before building. Record lineage per creative layer (script, on-screen text, typography, role sequence, cut clock, effects, audio, colour, ending): `origin`, `source_reference`, `operation = LOCK | ADAPT | SUBSTITUTE | DROP | USER_NEW`. Unknown origin stays `UNRESOLVED`; never credit it to the builder.

## Four separate verdicts

```json
{"technical_integrity": "PASS|FAIL",
 "reference_structure_parity": "PASS|FAIL|NOT_APPLICABLE",
 "visual_parity": "PASS|FAIL|PENDING_REVIEW",
 "human_creative_approval": "PENDING|APPROVED|REJECTED"}
```

None implies the next. The builder can set the first three; only the human sets the fourth. `LOCAL_REVIEW_READY` is the maximum autonomous outcome.

## Stage machine

| # | Stage | Complete when |
|---|---|---|
| 0 | Resolve state | One active revision, one next action, prior artifacts named and protected, no second process mutating the project (per-project lock). |
| 1 | Lock reference | Hash, raster/rotation/SAR/DAR, rational fps, time base, decoded frame count, per-frame hashes, audio packet + PCM ledger. The lock would detect a repost or transcode. |
| 2 | Blueprint | Full forensic breakdown (`reel-reference-forensics`): picture blocks as half-open intervals covering every frame once, hard cuts as a subset, caption/effect ledger, onsets. Detector output alone never locks. |
| 3 | Feasibility | Every block mapped to `exact_scene_available`, `role_equivalent_substitute` or `missing` (`reel-feasibility-gates`). Exact mode needs >= 80% exact or it is blocked. Gaps are presented before any render. |
| 4 | Footage index | Every library file content-hashed (path/size/mtime are hints only), probed, duplicates grouped, expected = probed = passed. |
| 5 | Casting / selection | Picture-state complete selection; see "Casting" below. Selection rehashes sources; render rehashes again. |
| 6 | Colour | Source-aware normalization then look (`log-footage-colour-pipeline`). |
| 7 | Typography | Captions typeset to the reference (`caption-typesetting-and-font-matching`). |
| 8 | Render | Native motion, exact frame counts, order: source normalization -> shot grade -> reference effects -> captions. One intra-frame master (e.g. ProRes) plus a separate review encode. Locked audio. |
| 9 | QC | Technical, structural and visual gates (`reel-visual-qc`), plus pre-compose caption collision and on-footage contrast gates. |
| 10 | Stage locally | One playable file + receipts. Upload, replace, share and publish are separate actions that each need an explicit go bound to the exact file hash and destination. |

A stage runner that stops at a step needing visual judgement is working correctly: do the named visual work, lock it, continue. Parse the runner's structured status - a "blocked" result may still exit 0.

## Casting from a footage library

For large libraries, funnel rather than browse:

1. Shortlist ~20 candidates per block from the index (role, verb, scale, direction, lighting family, motion energy).
2. ~5 coverage boards (full-take contact sheets at ~8 fps, labelled with source time and native frame).
3. ~3 normal-speed, renderer-exact crop proxies with the real caption composited.
4. 1 winner + 1 alternate. Output remains "machine shortlist, review required".

Gate whole worlds before individual shots: a montage of unrelated locations rarely reads as one piece. Judge substitutes on observed evidence - role, visible verb and its phase, subject scale, crop safety, screen direction, motion energy, lighting family. Reject at selection: fewer blocks than the blueprint, action that starts after the slot ends, warped geometry, unapproved reuse of a clip, reverse, speed warp, freeze, loop, fake blur, colour used to disguise a missing role. Peak placement for a substitute: `source_start = P - (T - S) / F` (P = source peak time, T = target frame, S = slot start frame, F = rational fps). Lock caption geometry (text boxes, negative space) **before** casting - a grade cannot rescue a composition that cannot carry the caption.

## Render essentials

- Normalize rotation and SAR before cropping; isotropic cover-scale + crop + `setsar=1`. Never scale X and Y independently.
- End with `-frames:v N`, never `-shortest` alone (it can drop the last frame when audio ends fractionally early).
- Keep effect frames exact (a one-frame pulse is one frame) and compose captions last.
- Append-only outputs: `v001`, `v002`... never overwrite a reviewed render.

## Handoff behaviour

- "Just give me the reel": hand over the one playable file, naming the exact revision first. No audit dump.
- "What do you think?": lead with APPROVE / REVISE / REJECT, then strengths, blockers and the smallest correct intervention.
- "What reels exist?": reconcile registry, live state and receipts; dedupe revisions and re-encodes into concepts; report rendered, QC-passed, approved, uploaded and published as separate counts.

More: `references/production-lessons.md` (pitfalls, revision recovery, cycle-time planning).
