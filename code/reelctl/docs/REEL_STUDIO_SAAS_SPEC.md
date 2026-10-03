# Reel Studio — End-to-End SaaS Product Contract

Status: product specification / local-first implementation target
Version: 1.0

## 1. The two job types this product must support

Real-world use produces two different production jobs and an informal review vocabulary.

### A. Reference-led original montage

A user supplies a reference reel and a large pool of original camera footage, then asks for a reel **like the reference** with the same aesthetic.

The reference controls the visual grammar:

- restrained, cinematic short-form pacing;
- coherent visual world and continuity rather than random stock montage;
- native-speed movement and clean hard cuts;
- a recurring real-shot micro-cue at locked time windows;
- composition and natural grade matching the reference;
- shot selection that follows the user's own `subject_preference_rules` (framing and inclusion rules the user configures);
- no synthetic glitch, RGB split, fake blur, geometric warping of people or invented action.

The new footage does **not** need to be the same literal scenes as the reference. It needs to satisfy the same structural roles and aesthetic contract. In `reelctl` terms, this is normally `original-montage`, even when the operator borrows the reference's timing or cue cadence.

Typical shapes of this job:

- a ~16-shot montage with source substitutions, a verified camera-profile conversion and a private review package;
- a six-slot structure with a fixed frame vector, reference audio/cut timing and a recurring real-shot cue, where the successful work is append-only slot replacement rather than an uncontrolled rebuild;
- a same-world six-role sequence with rule-aware source selection.

### B. Reference-locked reconstruction

A stricter job: reproduce a supplied reference's complete cadence, caption states, typography and visual treatment. The failure pattern to design against is an agent repeatedly declaring technical or visual passes while the user still sees a mismatch.

Failure modes the product must prevent:

- the first second uses a close-looking type treatment, then later captions drift into the wrong font and a fallback treatment;
- the rendered candidate's color and typography visibly collapse after a couple of seconds;
- a QC process checks the candidate in isolation rather than proving candidate-versus-reference parity at native geometry;
- a revision mechanically maps ~32 reference picture states into 16 selected slots;
- a signed or written "I watched it, PASS" assertion is treated as stronger than the actual visual evidence;
- a record claims PASS while the user rejects the visible result. Such a record is a negative/unresolved lesson, not a positive template.

In `reelctl` terms, this is `reference-locked`. It must preserve the reference clock and every locked state. If the footage or typography proof is insufficient, the system must block or require an explicit mode switch. It must never silently turn a failed reconstruction into a style match.

### C. "Chat-ready" is a workflow state, not a style

In conversational workflows, "chat-ready" means that a private review candidate and its evidence package were ready for the user to watch. It does **not** mean:

- approved;
- published;
- scheduled;
- publicly shared;
- or visually proven by the machine.

The SaaS state name should be `LOCAL_REVIEW_READY`. The only transition out of that state is an explicit, candidate-hash-bound human decision:

- `APPROVED_FOR_DELIVERY`;
- `REJECTED` with structured reasons;
- or `CHANGES_REQUESTED`.

No chat message, filename containing `final`, or agent self-attestation can substitute for that receipt.

## 2. Product definition

Build **Reel Studio**, a local-first SaaS-like application around the deterministic `reelctl` engine.

The core promise is:

> Upload one reference, add an authorized footage pool, choose whether the job is a literal reconstruction or an original montage in the reference's visual language, and receive a reproducible private review candidate with an evidence-backed explanation of every selected shot and every blocked issue.

The product is not "type a prompt and hope for a reel." It is a project system with durable contracts and resumable jobs.

## 3. The operator flow

### Step 1 — Create project

Inputs:

- stable project ID and semantic title;
- reference URL or local video file;
- authorized footage folder or upload batch;
- output canvas, normally 1080x1920;
- optional audio source and rights note;
- subject preference rules (user-configured);
- chosen production mode.

Do not use `reel 1`, `reel 2` or `final` as the durable identity. Store:

- `project_id` — stable creative identity;
- `revision_id` — editorial revision;
- `export_id` — canvas/codec deliverable;
- `workflow_state` — current gate.

### Step 2 — Analyze and lock the reference

The system extracts, stores and hashes:

- duration, dimensions, FPS, frame count, time base and PTS ledger;
- audio stream and decoded PCM identity of the muxed track (a user-supplied licensed track by default);
- shot/cut candidates;
- picture-state intervals;
- caption intervals, text, typography observations and effect lifecycles;
- color/lighting families;
- composition, crop, subject scale and negative-space observations;
- beat/cue windows.

A human/operator confirms the reference blueprint. Automatic analysis is a draft, never the final authority.

Output: `reference_lock.json` plus review boards.

### Step 3 — Select the mode before selecting footage

The mode is mandatory and immutable for a revision.

#### `reference-locked`

Use when the goal is "recreate this reel."

Controls:

- exact reference clock and block count;
- hard-cut onset within the locked tolerance;
- every picture state and role;
- caption timing and layer geometry;
- exact font proof or hash-locked traced glyph plates;
- effect lifecycle and provenance;
- source-aware color pipeline;
- reference-relative machine QC.

Rules:

- exact-scene feasibility must meet the configured 0.80 minimum;
- every required block needs independently observed evidence;
- missing scenes, guessed typography or unproven effects are hard blockers;
- no speed changes, reverse, fake blur, fake RGB, synthetic rescue frames or unlicensed source substitution;
- a human PASS cannot override a machine reference-relative FAIL.

#### `original-montage`

Use when the goal is "make an original reel in this style."

Controls:

- style profile and visual grammar;
- role coverage and narrative arc;
- pacing distribution and cut rhythm;
- continuity/world consistency;
- native-speed source behavior;
- grade family and bounded saturation/luma policy;
- recurring cue profile when the reference has one;
- rule-aware source selection.

Rules:

- do not claim literal scene or pixel parity;
- source substitutions are allowed when the role is evidenced;
- a reference timing vector may be borrowed, but it must be labeled as a borrowed grammar, not exact reconstruction;
- the engine still blocks incoherent footage, missing required roles, unsafe color, bad crops and unresolved QC.

If the operator asks for "like this" without choosing a mode, the system may recommend a mode, but it must stop at `MODE_CONFIRMATION_REQUIRED`. It must never infer silently from chat.

### Step 4 — Index the footage pool

For every source asset, persist:

- SHA-256, path/object ID, size and decode facts;
- frame thumbnails and contact sheets;
- camera/profile/range metadata and proof status;
- scene, action, subject, location, lighting and camera-motion labels;
- framing labels: subject present/absent, facing camera/away, close/medium/wide;
- continuity cluster and one-world score;
- rights/provenance status.

The model may propose labels. A proposed label is not evidence until tied to a source frame/range and review receipt.

### Step 5 — Feasibility report

Before rendering, show a board with:

- required reference roles;
- candidate source ranges;
- exact/role/uncertain coverage;
- missing or weak slots;
- mode-specific score;
- proof gaps;
- the reason for every block.

For `reference-locked`, below-threshold exact coverage produces `BLOCKED` plus `mode_switch_required: true`. The operator can explicitly create an `original-montage` revision; the system must not mutate the locked revision in place.

### Step 6 — Selection board

Each selected slot records:

- source hash and frame range;
- role/action evidence;
- reason selected;
- crop anchor and scale;
- color/profile proof;
- continuity relationship to adjacent slots;
- repeat group, if reuse is explicitly present in the reference;
- subject-preference-rules result;
- alternate candidates and rejection reasons.

The UI must let the operator replace one slot without rebuilding unrelated slots. Every replacement creates a new append-only revision.

### Step 7 — Render variants

A render job consumes an immutable recipe. It produces:

- master;
- phone review export;
- reference/candidate parity boards;
- motion/cut board;
- manifests and hashes;
- render receipt;
- structured logs.

For framing feedback, the product should generate labeled variants by **selection and crop**, never by warping the picture. Selection follows `subject_preference_rules`, a list the user configures per project (for example: which framings to prefer, which clips or time windows to exclude). The product ships no built-in preferences about how people should look; `preserve_native_geometry` is always true.

The operator can compare five variants, but each variant must disclose exactly which source slots changed.

### Step 8 — Machine QC and human review

Machine gates run independently:

- technical integrity;
- clock/frame/audio parity;
- source and receipt provenance;
- reference-relative visual checks where applicable;
- caption contrast/readability;
- typography/effect proof validity;
- continuity and crop checks;
- no unauthorized transforms;
- normal-speed decode.

Then the UI enters `LOCAL_REVIEW_READY` and presents:

- the video;
- a side-by-side or synchronized reference view;
- slot-by-slot evidence;
- all warnings and unresolved items;
- explicit buttons: Approve, Reject, Request Changes.

The approval receipt is bound to the exact candidate hash, revision, QC report and reviewer identity. A free-text chat response can be captured as feedback, but cannot itself advance the state.

### Step 9 — Delivery/publishing gate

Delivery is a separate state from creative approval. Only after explicit approval may the operator choose a destination. The SaaS must show the exact file, caption, account, destination and schedule before any outward action. Publishing is disabled by default in the MVP.

## 4. Durable data model

Minimum entities:

```text
Workspace
Project
Revision
ReferenceAsset
ReferenceBlueprint
StyleProfile
FootageAsset
FootageObservation
FeasibilityReport
SelectionBlueprint
RenderJob
Artifact
QCReport
HumanApprovalReceipt
DeliveryRequest
DeliveryReceipt
FeedbackEvent
```

Important distinction:

- `StyleProfile` stores reusable grammar learned from approved references.
- `ReferenceBlueprint` stores the exact structure of one reference.
- `SelectionBlueprint` stores the chosen source clips for one revision.
- `FeedbackEvent` stores what the user disliked; it does not silently rewrite the style profile.

A style profile may only be promoted from a reviewed/approved project or an explicitly confirmed operator rule. Rejected candidates remain useful negative examples and must never be promoted as gold templates.

## 5. SaaS architecture

### Local-first MVP (build this first)

```text
React/Next.js operator UI
        │
FastAPI application layer
        │
reelctl domain/service layer
        ├── SQLite project registry
        ├── local artifact store
        ├── ffmpeg/OpenCV workers
        └── append-only receipts + hashes
```

The first usable version should run on the Mac with one command and expose a browser UI at localhost. It should wrap the existing CLI rather than duplicate render/QC logic.

The current local slice implements this boundary: `reelctl serve` provides project creation, status, resumable run, and a JSON workbench over an allowlisted action set. It is intentionally not a hosted multi-tenant service yet; cloud auth, object storage and worker isolation remain a later deployment phase.

### Cloud version after the local flow is proven

```text
Next.js frontend
FastAPI API
Postgres (projects/contracts/state)
S3/R2 (source and immutable artifacts)
Redis + worker queue (analysis/render/QC)
Object-lock/versioning for receipts and outputs
```

Cloud workers must be untrusted execution workers. They may produce artifacts, but only the contract service can advance a gate. Storage permissions must prevent a worker from rewriting a prior receipt or approval.

## 6. MVP screens

1. **Projects** — stable project/revision/status view.
2. **New Reel** — reference, footage, mode and subject preference rules.
3. **Reference Blueprint** — synchronized video, cuts, states, captions and style profile.
4. **Footage Library** — searchable thumbnails, evidence labels and continuity clusters.
5. **Feasibility** — coverage matrix and blockers.
6. **Selection Board** — slots, alternates, replacement and provenance.
7. **Render Queue** — resumable jobs and artifact hashes.
8. **Review Room** — reference/candidate sync, QC, comments and approval controls.
9. **Delivery** — disabled until approval; exact preview before any external action.

## 7. What must not be delegated to chat

Chat is useful for:

- interpreting ambiguous human feedback;
- proposing a mode;
- suggesting style labels;
- explaining a block;
- converting a confirmed preference into a rule.

Chat must not be the source of truth for:

- shot count or timing;
- font identity;
- color/profile proof;
- source authorization;
- approval status;
- publication permission;
- or which file is the current candidate.

Every chat correction should become a structured `FeedbackEvent` with:

```json
{
  "target": "slot|caption|grade|continuity|framing|audio|ending",
  "observation": "what the operator actually saw",
  "requested_change": "what should change",
  "scope": "one_slot|revision|style_profile",
  "confirmed": false,
  "source_candidate_sha256": "..."
}
```

The operator confirms whether the change is one-off or reusable. Only confirmed reusable rules update a style profile.

## 8. Acceptance criteria for the first usable version

A user can:

1. create a project;
2. enter a reference and footage folder;
3. choose `reference-locked` or `original-montage`;
4. receive a feasibility matrix before any expensive render;
5. inspect and replace selected clips;
6. resume an interrupted render;
7. receive a private review candidate with evidence;
8. reject it with structured feedback;
9. generate a new append-only revision;
10. approve the exact hash;
11. export locally;
12. see publishing remain blocked until a separately explicit delivery action.

A clean technical decode alone is not a successful acceptance test. The product is successful only when it prevents the exact historical failures: silent mode confusion, collapsed reference states, typography drift after the first second, unproven color claims, and false PASS records.

## 9. Build order

1. Extract the current CLI into a callable service API without changing its contracts.
2. Add project/revision/feedback registry and explicit mode confirmation.
3. Add reference blueprint and footage/feasibility boards.
4. Add resumable job records and artifact browser.
5. Add Review Room with candidate-hash-bound approval/rejection receipts.
6. Add subject-preference-rule variants and reusable sound-cue profiles.
7. Add optional cloud object storage/workers only after local acceptance passes.
8. Add delivery integrations last, behind a separate approval boundary.
