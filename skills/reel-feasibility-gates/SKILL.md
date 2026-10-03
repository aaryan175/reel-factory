---
name: reel-feasibility-gates
description: Prove, block by block, that an authorized footage library can actually support a reference-led reel before any footage is selected or rendered. Use after a reference blueprint is locked and before casting, whenever a build might be promoted on thumbnail resemblance, or when someone asks "can we make this 1:1 with my footage?".
---

# Reel Feasibility Gates

Feasibility is an evidence gate between the locked blueprint and footage selection. It stops attractive thumbnails, filenames, colour similarity or machine rank from being promoted into matches the footage cannot support.

## Inputs (all must exist)

- A locked blueprint: the complete picture-block ledger with roles (not a guessed shot count).
- A complete, content-hashed inventory of the authorized footage root (expected = probed = passed).
- The declared mode: exact adaptation (1:1) or grammar adaptation.

## Classify every block independently

| Coverage | Meaning |
|---|---|
| `exact_scene_available` | The same scene, action and composition exist in the library and were observed. |
| `role_equivalent_substitute` | A different scene that performs the same editorial role (verb, scale, direction, energy, lighting family) and can carry the caption geometry. |
| `missing` | Neither exists. |

Decision per block: `USE_EXACT`, `USE_ROLE_EQUIVALENT`, or `ACQUIRE_NEW_FOOTAGE`.

## Thresholds

- **Exact mode requires >= 80% of blocks `exact_scene_available`.** Below that the result is `BLOCKED` with `mode_switch_required`.
- Grammar mode requires every block to be exact or role-equivalent; `missing` blocks need new footage or an explicit, recorded decision to drop or merge the block.
- Never silently switch modes. Never use the reference video as substitute footage. Never generate empty downstream artifacts (placeholder selections, renders or QC) to make the pipeline look complete.

## Evidence rules

1. Thumbnails prove role and composition only - not motion, timing, action phase, or caption clearance. Mark evidence scope honestly.
2. Literal claims need literal support: a dark screen is not a lit sign; a pool is not a court. Weak semantic resemblance stays a lead.
3. A candidate ID goes into `evidence_clip_ids` only when the required scene/action/composition was actually observed in frames from that exact hashed clip.
4. Rejected candidates are kept as negative evidence with the reason.
5. Footage tiers: `still_only` (never shortlist-eligible), `motion_proxy`, `native_window`, `native_locked`. A lower tier never inherits a higher tier's certainty. An 8 fps proxy cannot certify a 30 fps boundary.

## Manifest (keep the canonical file minimal)

```json
{"schema_version": 1, "status": "DRAFT", "mode": "exact",
 "blocks": [{"block_id": "p001", "reference_role": "hook: subject turns to camera",
   "coverage": "exact_scene_available|role_equivalent_substitute|missing",
   "evidence": "frames inspected=...; clip_sha256=...; observed=...; scope=thumbnail|proxy|native",
   "evidence_clip_ids": ["inventory-clip-id"],
   "decision": "USE_EXACT|USE_ROLE_EQUIVALENT|ACQUIRE_NEW_FOOTAGE"}]}
```

Put rich evidence (boards, notes) in a separate evidence file referenced from the `evidence` string. Lock = write once, hash, record the hash; any change is a new revision.

## Safe sequence

```text
exact workspace -> live status -> locked blueprint + complete inventory
-> thumbnail/frame/metadata inspection -> feasibility draft
-> lock + hash -> verify status
-> only then: selection -> assets -> render -> QC -> local review
```

Downstream prerequisite failures after a blocked lock are expected, fail-closed evidence. Retry only after the named upstream gate is genuinely repaired (new footage indexed) or the owner authorizes a mode change.

## Blocked report (what to tell the owner)

State: project and mode; lock path and hash; exact / equivalent / missing counts; ratio vs the 0.8 threshold; missing block IDs with their roles; recommended mode; the fact that no render exists; and the precise decision needed ("approve grammar mode", or "shoot blocks p004, p009, p013").

## Checklist

- [ ] Every blueprint block has its own coverage record.
- [ ] No thumbnail-only resemblance promoted to exact.
- [ ] Ratio computed against the full block count.
- [ ] Blocked stays blocked until an upstream change.
- [ ] No downstream artifacts were created on a blocked lock.
- [ ] Any "MP4 exists" claim points to a produced candidate, not the reference input.
- [ ] Nothing was uploaded, shared or published.

Worked patterns: `references/feasibility-lessons.md`.
