# Caption Engine Doctrine

One caption engine for every reel. Per-reel caption scripts are the failure class this
replaces: repeated rebuilds of a single reference failed on fonts and geometry, and the
review rejections ("the font is off", "the typeface changes after the first second") traced to
per-reel hacks, not to a missing feature.

Every rule below is cited to the internal lesson note that motivated it. The citation keys
name lesson documents (not shipped in this repository); this file is the engine's spec.

## Citation keys

The bracketed keys in this document (`<ref-06>`, `<ref-11>`, `ref-11-SEAL`, `<ref-22>`, `<ref-44>`,
BITF, LEDGER, AFFINE, IMPL, CLOCK, CFR, CIF, EBG, NFS, R4, EXEMPLAR) cite internal case notes and
lesson files from the original deployment. **Those notes are not shipped with this repository**;
the keys are kept so each rule shows that it came from a recorded case. The rules themselves are
complete as written here, and the shipped lessons live in `docs/04_LESSONS.md`.

---|---|
| <ref-06> | `video-content-analysis/references/ref-06-caption-contour-difference-case.md` |
| <ref-11> | `…/ref-11-exemplar-typography-forensics.md` |
| ref-11-SEAL | `…/ref-11-sealed-authority-supersession.md` |
| <ref-22> | `…/ref-22-script-font-affine-blend-case.md` |
| <ref-44> | `…/ref-44-bodoni-font-lock.md` |
| BITF | `…/burned-in-typography-forensics.md` |
| LEDGER | `…/frame-audit-evidence-ledger-and-font-fit-guardrails.md` |
| AFFINE | `…/evidence-scoped-caption-affine-fitting.md` |
| IMPL | `…/exact-font-glyph-fallback-implementation.md` |
| CLOCK | `…/exact-presentation-clock-and-render-locks.md` |
| CFR | `…/ffmpeg-cfr-endpoint-contract.md` |
| CIF | `reference-driven-reel-production/references/caption-ink-treatment-forensics.md` |
| EBG | `…/exemplar-bound-caption-renderer-gap-audits.md` |
| NFS | `…/native-frame-split-authority-typography-audits.md` |
| R4 | `…/v10-r4-caption-regression-recovery.md` |
| EXEMPLAR | a reference-intake directory holding a validated rule card for one exemplar reel |

---

## 0. The three stages

```text
EXTRACT   hash-locked reference video  ->  caption contract (JSON, schema-validated)
RENDER    caption contract             ->  full-canvas RGBA plate sequence
QC        render + reference frames    ->  per-state verdicts, renderer-independent
```

Each stage is separately receipted. A technical pass at any stage is never a creative
pass, and never a human approval (`README.md` QC authorities; NFS Stage-7 criterion).

---

## 1. The contract is schema-validated or it does not exist

Before this engine, `blueprint.schema.json` declared the caption contract as:

```json
"caption_layers": {"type": "array", "items": {"type": "object"}}
```

Any caption payload validated. No caption defect could be caught at the schema layer —
which is why they were all caught by a reviewer's eye instead.

**Rule 1.1** The caption contract validates against `schemas/caption-contract.schema.json`
with `additionalProperties: false` at every level. An undeclared field is a failure, not a
warning. Unknown lifecycle kinds, unknown ink methods and unknown render paths all fail
closed.

**Rule 1.2** Cross-field invariants a JSON schema cannot express are enforced in
`contract.py`: `frames == end_frame_exclusive - start_frame`; every state inside the
reference clock; every `style_id` declared; no duplicate ids; concurrent states carry
distinct stacking `z`; `render_allowed` only on a `SEALED` contract.

**Rule 1.3** Intervals are zero-based half-open `[start_frame, end_frame_exclusive)`, one
notation everywhere, recorded in the contract. The corpus mixes `[x0,y0,x1,y1)` (<ref-06>,
<ref-11>) with boxes labelled half-open but written closed (<ref-44>); the engine fixes one and
never infers (A1 §5.5; PULSE via A2 §1.3b: "Treat exact frame indices as authoritative,
use 0-based numbering, and state interval semantics").

---

## 2. The clock

**Rule 2.1** Caption in/out are integers on the reference's own PTS clock. The engine
stores `(start_pts, pts_step, time_base, frame_count)` and derives time as
`t(f) = (start_pts + pts_step*f) * time_base`. Never from a float seconds value, never
from `23.98`/`29.97`/`59.94` shorthand (CLOCK:31–37 — "Do not round to a familiar
broadcast rate because it is numerically close").

**Rule 2.2** When nominal rate, average rate and frame PTS disagree, **the PTS are the
authority** (CLOCK:29).

**Rule 2.3** Never inherit FPS from a previous revision; re-probe the reference
(CLOCK:5–17).

**Rule 2.4** Renders are terminated by `-frames:v EXPECTED_FRAME_COUNT`, never by
`-shortest`, which silently ate the last frame of a 708-frame contract (CFR:9–33). Post-
render, the decoded frame count is re-probed; source-code assertions are not evidence
(CLOCK:52).

---

## 3. Ink comes from the reference's own pixels

**Rule 3.1** There is no constant-colour branch. `ink.source` is the constant
`sampled_reference_pixels` and the sample evidence (method, frames, ROI) is required. The
pre-existing `assets.schema.json` exposes `fill_rgba` as a bare constant with no
obligation to say where the colour came from; the caption contract does not.

**Rule 3.2** Ink is measured **spatially inside the clean geometry mask** — a low-pass
reference-derived ink field, or a measured reference palette/gradient for a static state
(<ref-06>; A1 §4). Three sampling methods are recognised, each already proven in the corpus
(EXEMPLAR scripts):

| method | use | mechanism |
|---|---|---|
| `min_channel_threshold` | white/near-white ink | `min(B,G,R) > t`; holds up over arbitrary footage |
| `chromatic_difference` | coloured ink | channel-difference predicates, e.g. `(r-g) > k ∧ (b-g) > k` |
| `temporal_difference` | static ink over moving footage | median of state frames vs median of adjacent **blank** frames |

`temporal_difference` is the non-circular workhorse: it recovers text using only the
reference's own neighbouring text-free frames, so no font, palette or renderer constant
enters the measurement.

**Rule 3.3 — forbidden ink sources** (all <ref-06>/CIF unless noted): hard-coded blue/cyan/
pink/mauve fills; a canvas-wide gradient standing in for reference ink; caption chroma
derived from the **substitute/final base footage** rather than the reference; unblurred
RGB plates; flattened-reference RGB plates (they copy "desert, fabric, set-light, and skin
pixels into text"); raw footage-reactive luma fills; baking source-video colours into the
alpha asset; frozen 4:2:0 one/two-pixel colour edge bleed promoted into exterior shadows;
outline/blur/glow/shadow not proved by an enlarged edge profile (BITF §5, §7).

**Rule 3.4** Never name an editor blend mode from a flattened encode. Report
`Difference-family inversion`, not "Difference" (BITF §5, §8). The engine has **no default
compositing operator**: <ref-06>'s reference fits Difference-family inversion, <ref-22>'s fits
near-white SourceOver, and the lesson is that both must be fitted per reference per stack
(A1 §6 C6). Fit `O = B + α(F − B)` against `O = B + α(|B − S| − B)` and compare residual
distributions (BITF §5).

**Rule 3.5** Measured decoded colours are never presented as original design hex values
(LEDGER §5).

---

## 4. Geometry

**Rule 4.1 — uniform scale only, structurally.** `geometry.scale` is a single scalar. A
2-vector, or separate `scale_x`/`scale_y`, is unrepresentable in the schema rather than
merely discouraged. Per-word anisotropic resize is *the* mechanism that makes one font read
as several:

> "**Never** call `raw.resize((target_width, target_height))` for each word. That
> operation makes one font file look like multiple fonts by applying a different X/Y warp
> to every word." — <ref-06>, permanent regression fixture

**Rule 4.2** Within one editorial tier, share the face, nominal size, tracking and
placement mode, plus **at most one** evidence-backed global transform. Per-state *uniform*
size and optical translation are allowed; per-word X/Y stretching is not (IMPL §4;
BITF §3 anatomy #5).

**Rule 4.3 — the named-exception boundary.** A per-lockup anisotropic fit is permitted
**only** with direct multi-frame evidence, an explicit contract flag, retained measured and
clean native bboxes, a written justification that it is geometry reproduction not font
substitution, and a failing test first (AFFINE). Absent all five it is envelope-filling
warp. This reconciles a live contradiction between <ref-06>/BITF ("forbidden") and AFFINE
("conditionally allowed") — see A1 §6 C3; the engine draws the boundary explicitly rather
than inheriting either side silently.

**Rule 4.4 — scale convention must be recorded.** <ref-22>/BITF declare `scaleY = 1` with
nominal pixel size carrying vertical scale; <ref-06> reports `scaleX=1.15, scaleY=1.22` as its
V10 convention; <ref-44> sets `SCALE_X = 1.0` with no `scaleY` while the rollback it points at
uses 1.15× vertical. Numbers authored under one convention and applied under the other
silently double- or half-apply vertical scale (A1 §6 C2). Any imported legacy geometry
records which convention it was authored under.

**Rule 4.5 — two placement boxes, not one.** Report the core/50%-alpha ink box **and** the
low-alpha glow/shadow extent; keep the optical/core centre separate from any asymmetric
treated-alpha centre (BITF §3; LEDGER §3).

**Rule 4.6** A manually drawn `target_bbox` is a placement/line envelope, never measured
ink, unless explicitly proven (LEDGER §3). The decoded raster is the authority, not legacy
bbox JSON — recovered tight bboxes "differ materially from several loose blueprint
envelopes" (<ref-06>).

---

## 5. Font identity: non-circular or nothing

### 5.1 The exactness hierarchy (IMPL §1)

1. `metadata-locked exact font` — original file/face/axes/features known and hash-locked
2. `raster-verified exact outline` — one local face explains several diagnostic words under
   **one global geometry model** with a **decisive score gap**
3. `hybrid glyph source` — locked face for repeatable glyphs, source-derived components for
   unavailable capitals/swashes/ornaments
4. `reference-derived raster matte` — per-state alpha from the authoritative frames
5. `Unresolved` — fail closed (`TYPOGRAPHY_UNRESOLVED`)

A nearest family may appear in a candidate report and **must never** be promoted to
`exact_font` (IMPL §1). The engine's contract statuses map to this: `HYPOTHESIS`,
`HOLDOUT_PROVEN`, `ORIGINAL_ASSET_PROVEN`.

### 5.2 The holdout protocol (<ref-11>, 6 steps, implemented in `fontproof.py`)

1. Shape whole words with HarfBuzz (`ot`, `kern=1`, `liga=1`, `calt=1`) from the exact file
   and TTC index.
2. Fit **one** nominal size/tracking/global affine for the tier. **Do not independently
   resize words.**
3. Train geometry on multiple words.
4. **Freeze geometry** and score unseen multi-word holdouts **without refitting**.
5. Report both fit-corpus and holdout Dice, 1 px F1, symmetric contour Chamfer,
   component/hole topology, and independent width/height error.
6. Inspect native overlays **after** numerical ranking — "a holdout rank is candidate
   evidence, not original-editor metadata."

### 5.3 What is circular (forbidden as proof)

- Instructing the renderer to fill a `target_bbox` then asserting the bbox equals it:
  `bbox_iou = 1` "proves only that the resize instruction ran. It is a required negative
  regression, not an exact-font proof." (LEDGER §3; IMPL §8)
- `resize(candidate, reference_width, reference_height)` before scoring — "can give a
  horizontally or vertically distorted candidate a perfect score." (IMPL §4)
- Any gate composed only of `alpha_bbox == target_envelope`, font hash, frame count and
  decode success (<ref-06>).
- Bbox equality alone: "Bbox equality alone is never font proof." (IMPL §4)
- Whole-word normalised correlation without contour/skeleton distance and explicit
  outer-bound coverage (BITF §3).
- A component table treated as mask acceptance without an enlarged native-frame contour
  overlay (<ref-11>).

### 5.4 Independent metrics (A1 §3.5)

Counting metrics: Dice, IoU, 1 px-tolerance F1, symmetric contour/skeleton Chamfer
distance (px, **lower is better**), outer-bound/loop/terminal/dot coverage,
connected-component count/holes/left-to-right progression, cumulative x-drift, independent
width/height error.

**Supporting only, never counted toward the agreement requirement:** normalised
correlation `r`. One blended scalar is one metric, not two — the historical
`s = .6*dice + .4*iou` never tested agreement at all.

### 5.5 The gates

Two independent counting metrics must **each** prefer the same candidate on **every**
holdout word, by margin ≥ `MIN_HOLDOUT_MARGIN` over the runner-up, with score ≥
`MIN_HOLDOUT_SCORE`. The margin is the primary gate; tier 2 of §5.1 asks for a *decisive
score gap*, not a high absolute number.

The calibration comes from the corpus: the historical false positive was a **0.0129**
margin (mean `0.8424` vs runner-up `0.8295`, EXEMPLAR `reference_blueprint.json`), and real
candidate holdout Dice sits at `0.699–0.921` (<ref-11> smoke table; <ref-06> Helvetica `0.92099`).
So the margin gate sits an order of magnitude above the noise that produced the false
positive, and the absolute floor sits below the real corpus so that a decisive margin is
not vetoed by a merely-good absolute score.

Failing any gate yields `HYPOTHESIS`. A `HYPOTHESIS` face may never authorise the
native-font render path — it routes to source-contour instead. Human rejection supersedes
any automated PASS, and the rejected artifact is preserved as a negative fixture whose hash
is never reusable as a positive template (<ref-06> V10 gates).

---

## 6. Render paths

**Publishing default: a licensed or open font.** Anything you publish is set in a face you
are licensed to use (purchased, or open-licensed such as SIL OFL) and rendered through
`native_font`. Pick the closest licensed/open face to the reference with the matching tools
below; a guessed face is still not claimed as *the reference's* face.

**`source_contour` — analysis and matching aid.** Glyph shapes are traced from the reference's
own pixels, with per-state uniform scaling and ink from sampled fields. Use it to measure the
reference's lettering, compare candidate faces glyph-for-glyph and make local study renders.
A traced contour reproduces someone else's lettering, so it is not a publishing asset: replace
it with a licensed or open font before anything leaves your machine. (BITF §3 veto #7 still
applies to identification: "If no candidate survives multi-word topology and contour review,
set the exact face name to `null`".)

**`native_font` — exactness claims on proof only.** Claiming a face *is* the reference's face
requires a `HOLDOUT_PROVEN` or `ORIGINAL_ASSET_PROVEN` hypothesis for that style. Enforced in
`contract.py`, not by convention. Without that proof the contract routes the style to
`source_contour` for local study only; for publication, set the captions in your licensed/open
face and disclose that it is a close match, not the original.

**Rule 6.1** Masks are not automatically production alpha. Promotion requires the stricter
ref-11-SEAL superset: authority binding, hash sealing, quality classification, numeric
placement, executable per-frame alpha/blur/ink behaviour, runtime consumption or byte
verification, and independent source-relative gates (A1 §6 C5 — <ref-22>'s looser criterion is
superseded).

**Rule 6.2** Mask evidence tiers are `clean | secondary | diagnostic_partial`, and **only
`clean` enters aggregate ranking** (<ref-11>).

**Rule 6.3** Compositing order is explicit. The exemplar's E01 records "invert picture
first, then composite white caption above it" while E02/E03 invert picture only, no caption
(EXEMPLAR `grammar.effect_layers`). Caption/effect interaction is contract data, not
renderer behaviour.

---

## 7. QC is renderer-independent

**Rule 7.1** Gates never read the renderer's own constants. QC compares the render against
the **reference frames**: per-state mask IoU/Dice, ink distance, timing exactness.

**Rule 7.2 — state-class aware.** A uniform threshold is wrong: `threshold_bbox >= 128`
"may legitimately be null" on heavily blurred/low-opacity entry frames (<ref-22> vs IMPL §7 —
A1 §6 C8). Gates key off `lifecycle.kind` (entry/resolving/crisp), never one global rule.

**Rule 7.3 — contour defect vs codec artifact.** One/two-pixel YUV420 chroma bleed and
ordinary antialias changes are **not** authored shadows or anatomy defects. A real contour
defect "should persist at native scale and alter a meaningful structure: counter closure,
missing dot/apostrophe, clipped swash, fused letters, stepped/notched core edge, wrong
terminal, nonuniform scaling, or a stable layout collision" (NFS §7).

**Rule 7.4 — chronological localisation.** Report the first defective zero-based frame,
the first mature/readable frame, the affected interval, the exact authority layer violated,
the native evidence path, **and the evidence limit**. "Entry blur can expose a collision,
stacking error, or premature line before the following crisp frame. Do not report only a
convenient midpoint." (NFS §5)

**Rule 7.5 — dimensions compared separately** (NFS §6): class/case; anatomy/topology;
geometry; layout/layering; treatment; lifecycle. A timing-only match fails automatically
(NFS Stage 7).

**Rule 7.6 — evidence is bound to the candidate hash.** "A stale board can be visually
useful history but cannot prove the current encoded bytes." Every board/report binds to the
current candidate SHA-256, and coverage must include the **final** state, not only midpoint
crops (EBG §4).

---

## 8. The gap checklist the engine must close (EBG §3)

Audited explicitly, each as a test:

- G1 inherited paths still performing per-word width×height envelope fitting
- G2 dispatch changing geometry engine at a frame or layer-start boundary
- G3 adjacent states mixing envelope-fit script, new sans path, micro fallback face and
  extracted contours
- G4 a "native" tier still applying a large unexplained X/Y affine
- G5 tracking existing only as a manifest field while text is shaped as one unmeasured run
- G6 baseline changing through categorical heuristics rather than exemplar measurements
- G7 special size tiers that are word-ID exceptions from an older source
- G8 reference ink sampled from the wrong reference, or including background
- G9 manifest read/hashed but not consumed or byte-verified by the compositor
- G10 metadata reporting a face hash/source inconsistent with the actual runtime branch
- G11 `scale_x / scale_y` calculated and reported for **every** tier — "'no independent
  per-word fit' does not imply 'no warping'"

**Before any font fitting** (EBG §2) a gap-free zero-based caption-state ledger must exist
for exemplar **and** candidate: exact words and punctuation; state ranges and true blank
intervals; line membership and stack accumulation/removal; sans/script/ornament class;
size tier; entry/hold/replacement/exit states; blur/smear/opacity lifecycle; placement mode
and composite behaviour. "If the script, stack grammar, endpoint, or frame domain differs,
no font tuning can produce parity."

---

## 9. Authority lifecycle

A contract moves `DRAFT → EXTRACTED → SEALED`. Only a `SEALED` contract may carry
`render_allowed: true`. Sealing records `contract_sha256` and what it supersedes; a sealed
contract is superseded by an explicit successor, never edited in place (ref-11-SEAL).

Append-only: an approved baseline is never overwritten, and rejections stay on disk as
negative fixtures with receipts (the project's append-only invariant).

Publishing, uploading and spending remain human-gated at every stage. The engine never
self-approves.

---

## 10. Mask tiering: the machine proposes, the authority decides

<ref-11> classifies every recovered mask `clean | secondary | diagnostic_partial` and only
`clean` enters aggregate ranking (rule 6.2). `extract.classify_mask_tier` proposes a tier
from two measurements — interior/ring luma separation, and interior spread (a bimodal
interior means the mask carries background, or the ink is footage-reactive, both of which
<ref-11> downgrades). Components below 2% of total ink are excluded, because dots, commas and
antialias fragments are legitimately low-contrast and would otherwise govern the score.

**The proposal is not authority.** Calibrated against the sealed authority's own 12
classified RefB states, the machine agreed exactly 4 times out of 12, was conservative on 7,
and over-promoted 5. A single-frame contrast scalar does not reproduce a human's whole-word
contour-authority judgment, which is informed by what sits behind the text — architecture,
people, monitor detail. Rather than tune thresholds until a scalar imitates that judgment,
the engine takes the doctrine at its word: "Inspect native overlays **after** numerical
ranking"; a rank is "candidate evidence, not original-editor metadata" (<ref-11>).

So `resolve_mask_tier` grants two asymmetric powers:

* a machine `diagnostic_partial` is a **hard veto** on the pixels' own evidence — neither a
  sealed authority nor a human may promote past it by assertion;
* a machine `clean` or `secondary` is **advisory** — it can never promote a mask on its own,
  but it does not block a declared classification, so a human may resolve an ambiguous
  `secondary` upward after inspecting native overlays.

`promotable` is therefore true only for a `clean` tier whose source is `sealed_authority` or
`human_review`. A machine `clean` alone never becomes production alpha.

The calibration table is written to the project's receipts directory alongside the run.

---

# Part II — taxonomy, authority, contract shapes, evidence

Part I is the engine's rule set. Part II adds the four things a second independent pass through
the same corpus found missing: a named failure taxonomy, the source/authority locks that sit
*above* the caption contract, the explicit stage contracts with proposed thresholds, and the
fixture inventory that makes those thresholds testable.

Additional citation keys used below:

| Key | Source |
|---|---|
| STA | `reference-driven-reel-production/references/sealed-typography-authority-render-contract-reconciliation.md` |
| RED | `…/independent-native-glyph-red-capable-diagnostics.md` |
| ADAPT | `…/typography-exemplar-authority-domain-adaptation.md` |
| REMED | `…/typography-remediation-execution-handoffs.md` |
| NCR | `…/native-caption-regression-recovery.md` |

---

## 11. The failure taxonomy

Eight named failure classes. Every one of them shipped at least once. Each is the reason for a
rule above or below, and each needs a test that goes red on the original defect.

**F1 — per-word anisotropic warp.** Each word resized independently to its own detected
envelope, so one font file renders as several visibly different faces. In review it reads as
"the typeface changes after the first second": the opening state looks right and the typeface
changes word to word a few states later.
*Source:* `ref-06-caption-contour-difference-case.md` (permanent regression fixture);
`native-caption-regression-recovery.md` measured the axis factors — roughly `0.544` for one word,
`0.485` for a later word, against `1.025` for an early state, all from one font file, produced by
a raw raster resize to each `target_envelope_xyxy`.
*Rule:* 4.1, 4.2, 4.3.

**F2 — hard-coded ink.** A named blue/cyan/pink palette or a canvas-wide gradient substituted
for the reference's actual ink, which is frequently underlay-reactive; or the opposite error,
raw per-channel `abs(background - fill)` applied to *substitute* footage, which stained the
letters red/green/magenta in colours absent from the reference.
*Source:* `ref-06-caption-contour-difference-case.md`;
`caption-ink-treatment-forensics.md` (operator families, not colour swatches);
`v10-r4-caption-regression-recovery.md`.
*Rule:* 3.1–3.5.

**F3 — mixed geometry engines.** A single render whose adjacent states are produced by
different geometry paths: envelope-fit script beside a new sans path beside a micro fallback
face beside extracted contours; dispatch switching engine at a frame or layer-start boundary; a
generic substring branch (`"navy"`) swallowing an exact style ID
(`script-sky-blue-to-deep-navy`) and silently disabling its gradient.
*Source:* `exemplar-bound-caption-renderer-gap-audits.md` §3 (the G1–G10 list);
`ref-06-caption-contour-difference-case.md` (exact-branch precedence).
*Rule:* section 8, plus exact-ID dispatch before any substring catchall.

**F4 — circular QC.** Gates that cannot fail independently of the renderer. The canonical form:
force the candidate into a hard-coded envelope, then assert `alpha_bbox == target_envelope` and
report `bbox_iou = 1`. Also: `font_contract()` compared against `geometry_record()` authored from
the same constants; a `geometry_qc_report` returning a literal `PASS` with claimed residuals it
never recomputed; forbidden-string scans over metadata while the renderer still uses the wrong
branch. In the worked V10-R4 case **all 11 typography tests passed** while remaining largely
circular.
*Source:* `exemplar-bound-caption-renderer-gap-audits.md` §5 (ten circular patterns);
`frame-audit-evidence-ledger-and-font-fit-guardrails.md` §3;
`native-caption-regression-recovery.md` ("Why the old QC passed").
*Rule:* 5.3, section 7, and section 16 below.

**F5 — unproven font assertion.** A nearest family promoted to `exact_font` because it ranked
first after bbox normalisation. Two independent demonstrations that ranking ≠ identity: <ref-06>'s
Helvetica Neue Bold won bbox-normalised Dice `0.92099` over Poppins ExtraBold's `0.88277` and was
then *rejected on native side-by-side crops* as visibly narrower/condensed — production returned
to Poppins. On RefB, two non-circular holdout audits picked **different** winners from different
evidence subsets (Helvetica Neue Condensed Black at holdout Dice `0.695749`; Hiragino Sans W9 at
`0.816284` but requiring an anisotropic `sx=0.76, sy=0.92` shared transform and omitting lowercase
`g`), so the sealed contract sets both `sans_exact_face` and `script_exact_face` to `null`.
*Source:* `ref-11-exemplar-typography-forensics.md`;
`ref-11-sealed-authority-supersession.md`;
`ref-06-caption-contour-difference-case.md`.
*Rule:* 5.1, 5.2, 5.5, and the status vocabulary in 11.1.

**F6 — timeline/exemplar mismatch.** The renderer bound to a different reference than the one
the work is being judged against. V10-R4 was a 213-frame <ref-06> implementation — different words,
faces, ink source and endpoint — presented as parity work against a 240-frame RefB exemplar. No
font tuning can close that gap, and the honest classification is "an internally consistent
implementation of its own old contract, not exemplar parity."
*Source:* `exemplar-bound-caption-renderer-gap-audits.md` §1, §8;
`typography-exemplar-authority-domain-adaptation.md` §2.
*Rule:* section 13.

**F7 — control plane mistaken for data plane.** A sealed source hash, a 39-state ledger, two
proven blank frames and a green validator, coexisting with **zero** executable caption layers:
`canonical_full_frame_alpha: 0/240`, `end_to_end_render_ready_states: 0/39`. Decoded RGB source
frames are not caption RGBA frames; a static `L` mask is not a per-frame alpha sequence; a
`/tmp` scratch mask is not a production asset.
*Source:* `sealed-typography-authority-render-contract-reconciliation.md`;
`ref-11-sealed-authority-supersession.md` (the readiness correction).
*Rule:* section 14.

**F8 — self-fit control.** A QC control that shares the implementation's own preprocessing, so a
near-perfect score proves only that the code reproduced its own input. The R5B `<word A>` diagnostic
split the sealed frame-202 support alpha at row 202 and scored `0.965135`; the builder had done
`binary[202:, :] = 0` and the diagnostic `upper[202:, :] = False` — **both amputated the same
lower descender loop at the same boundary.** Against the *complete* connected source word the same
candidate scored Dice `0.902986` with a source→candidate edge p95 of `85.095 px` and 1,754
source-only pixels.
*Source:* `independent-native-glyph-red-capable-diagnostics.md`.
*Rule:* section 16.

### 11.1 Face-identity status vocabulary

Exactly one status per face class per contract, never prose (REMED §1):

| Status | Means |
|---|---|
| `EXACT_FONT_BYTES_VERIFIED` | font bytes, face index, shaping features and a hash-bound reference-relative specimen all proven |
| `EXACT_SOURCE_CONTOUR_VERIFIED` | original face name may be unknown; a complete finite-word contour from the authoritative exemplar passes native overlay and topology checks |
| `SYNTHETIC_GRAMMAR_COMPLETION` | word constructed from the exemplar's measured grammar; explicitly not an exact-font claim |
| `EXACT_FACE_UNRESOLVED` | nothing meets the proof standard; fail closed |

"Installed or visually similar fonts are candidates only." A `HYPOTHESIS` in section 5 maps to
`EXACT_FACE_UNRESOLVED` for gating purposes: it may never authorise the native-font path.

---

## 12. Source identity is three layers, and the frame namespace is frozen

**Rule 12.1 — hash all three layers.** Container bytes, compressed packet payload (video and
audio independently), and decoded essence (decoded-video stream hash plus decoded PCM hash and
sample count). Different container hashes may carry identical packets; different packets may
decode identically (LEDGER §1). A sealed authority carries all three for exactly this reason
(container hash, VP9 packet-essence hash, decoded RGB24 essence hash), and its policy line
states the resolution: **decoded-video essence is the typography authority
across remuxes; the container hash identifies origin.** A second remux in the same directory has
a different container hash and different audio, and identical packets and pixels. Without this
rule the two files silently fork the visual reference.

**Rule 12.2 — never let a stale manifest hash into a new contract.** Old manifest hashes that
disagree with current bytes are stale evidence, not history to copy forward (LEDGER §1).

**Rule 12.3 — freeze the decoded-frame namespace before any visual interpretation.** A frame
directory and its prebuilt contact sheets are mutable scratch evidence, not authority. Fresh-decode
to a separate immutable directory, declare one zero-based convention independent of the
extractor's filenames, prove an exact one-to-one mapping for all `N` frames, require exactly `N`
current frames with no gaps or extras, and bind every sheet to the current input-frame hashes.
This is not hygiene: on RefB the live scratch directory had been renumbered while old sheets kept
their embedded labels, producing "a plausible one-frame shift at boundaries" that made **every
lexical range look internally plausible** while moving isolated cuts and onsets. If the mapping
changes mid-audit, every boundary claim made from the old sheets is invalidated and rechecked
(<ref-11>; LEDGER §1).

**Rule 12.4 — extract zero-based explicitly.** `ffmpeg … -start_number 0 f%03d.png`. A default
one-based sequence maps decoded frame 0 to `f001` and invalidates every board label; a rejected
one-based extraction is marked invalid rather than mixed with correct evidence (<ref-22>).

---

## 13. Authority domains: which video owns which layer

When a typography exemplar and a target reel are different videos, authority is assigned **per
layer, before the timeline is touched** (ADAPT §1; NFS §1; REMED §2):

| Layer | Default authority |
|---|---|
| picture/cut clock, frame count, shot slots | target reel |
| audio | target reel or a separately locked track |
| words, case, punctuation | target reel, or an explicit user/exemplar override |
| caption intervals, accumulation/replacement | target reel |
| placement envelopes, scene-relative composition | target reel |
| glyph anatomy, script/sans family grammar, hierarchy | typography exemplar |
| lifecycle *method* (detectable/readable/crisp/hold/exit) | typography exemplar |
| exemplar's literal frame numbers | **non-authoritative** unless explicitly adopted |
| ink/compositing | declared target or a labelled adaptation; never silently blended |

**Rule 13.1** Do not extend a 213-frame target to 240 frames because the exemplar is 240 frames.
A read-only audit finding that the last source shot has 27 frames of native handle proves
*mechanical picture feasibility only* — it is an audit hypothesis, not the production plan
(ADAPT §2).

**Rule 13.2** An exemplar contract carrying `render_allowed: false` is a stop sign for any
verbatim exemplar-bound render. Cross-reference reuse goes through a **new target-specific
contract** that binds the exact borrowed contour and scope, and claims nothing about reproducing
the exemplar's full state program (ADAPT §1).

**Rule 13.3** Honour explicit exceptions first. An intentional lowercase source-contour override
is not a defect merely because the target displays uppercase (NFS §1).

**Rule 13.4** Where target and exemplar genuinely disagree on face-class role, casing or phrase
treatment, record an `AUTHORITY_CONFLICT`, produce native-resolution A/B variants under identical
placement/timing/ink, and stop for a human choice. Do not silently pick the easier render
(REMED §2).

**Rule 13.5 — `runtime_fonts == []` proves nothing about anatomy.** It proves only that no font
loaded during composition. A fully fontless renderer can still bake the wrong reference's glyph
skeletons or an explicitly rejected fallback face into its PNGs (ADAPT §3).

---

## 14. Control plane and data plane are separate, and readiness is per state

> A sealed text/timing authority is a control plane. It is not, by itself, a caption data plane.
> — STA

**Rule 14.1** Four authorities never promote into one another: **source** (container/packet/
decoded essence, canvas, rational FPS, frame namespace), **state program** (text/case/ranges/
stack/blank frames/phases), **asset+render** (contours, anchors, scale, per-frame alpha/blur,
ink, compositing order, and the code that consumes them), **parity** (independent source-relative
gates). A PASS in one plane is not a PASS in another (STA §1).

**Rule 14.2 — asset census by provenance, not filename.** Every contour, mask, plate, cache item
and frame sequence is exactly one of `canonical_hash_sealed_source_evidence`,
`temporary_unsealed_source_evidence`, `generated_candidate_or_font_cache`,
`legacy_wrong_authority`, `absent` (STA §4).

**Rule 14.3 — a state is `end_to_end_render_ready` only when all seven hold** (STA §5):
production contour or approved exact-font instance; numeric crop origin/anchor with only
evidence-backed uniform scale and translation; executable per-frame alpha/blur; executable
ink/composite behaviour; deterministic layer order and stack persistence; manifest consumption
or byte-identical live-generation proof; independent source-relative acceptance gates.

It is valid — and was the honest answer for RefB — to report `control_plane_ready = true` for
every state while `end_to_end_render_ready = false` for every state. The engine reports the
readiness matrix rather than burying `0/N` coverage behind font forensics (STA §9).

**Rule 14.4** Two acceptable data-plane designs: **per-frame plates** (one alpha/RGBA result per
logical frame, transparent on declared blank frames) or a **deterministic state renderer**
(static sharp contours plus sealed per-frame placement/alpha/blur/ink/order schedules that
reproduce the same sequence). Anything else is not a data plane (STA §6).

**Rule 14.5** Because the reference is flattened, generated plates are labelled
`source-output reconstruction`, never "recovered original editor alpha" (STA §6).

---

## 15. Stage contracts

### 15.1 EXTRACT must produce

A schema-validated caption contract carrying, at minimum:

```text
source_lock       container_sha256, packet_essence_sha256, decoded_essence_sha256,
                  canvas [w,h], fps_num/fps_den, time_base, start_pts, frame_count,
                  frame_namespace ("zero-based decoded order"), full_decode result
state_program     gap-free over [0, frame_count): every state's id, exact text (case and
                  punctuation preserved verbatim), half-open [start_frame, end_frame_exclusive),
                  style_id, face_class (sans|large_sans|script), composition_mode
                  (accumulate|replace), stack slot z, plus the declared blank-frame set
lifecycle         per state: first_detectable, readable_blur, near_crisp, first_crisp, hold,
                  exit (progressive_blur window | hard removal), final-frame residual flag
words/boxes       per state: word/line membership, line breaks, core/50%-alpha ink bbox AND
                  low-alpha treated extent, optical anchor or measured baseline, placement mode
geometry          per state: uniform scale scalar + translate_xy (+ recorded scale convention);
                  no scale_x/scale_y pair is representable
ink evidence      per state: sampling method, source frames, ROI, interior pixel count,
                  rgb median/p05/p95, alpha median, fitted operator family with residuals,
                  and the explicit equivalence-class caveat
mask provenance   per state: extraction method, selected component IDs, tier
                  (clean|secondary|diagnostic_partial), asset path + sha256, source frame,
                  authority_decoded_sha256, topology (components, holes)
font hypotheses   per style: status from 11.1, candidates with holdout metrics, never an
                  exact claim without the section 5.5 gates
authority_domains when a separate exemplar is involved: the section 13 matrix, plus conflicts
status            DRAFT | EXTRACTED | SEALED, render_allowed, contract_sha256
```

Any state that cannot be filled to this standard is recorded as blocking, not smoothed over.

### 15.2 RENDER consumes

The sealed contract and nothing else. Specifically it must **not** read: the reference's RGB
plate as a fill source, a font family resolved by name at runtime, a target envelope as a resize
instruction, the substitute footage as an ink source, or any constant not present in the
contract. Path selection is enforced, not conventional: an exactness claim on `native_font`
requires a `HOLDOUT_PROVEN`/`ORIGINAL_ASSET_PROVEN` hypothesis for that style; everything else
routes to `source_contour`, which is an analysis/study path. Published output uses a licensed or
open font.

Output is a full-canvas RGBA plate per logical frame, transparent on declared blank frames,
composited after grade and before final encode, terminated by `-frames:v <frame_count>` and never
by `-shortest`.

### 15.3 QC measures — proposed gates

Renderer-independent, recomputed from reference pixels. Thresholds below are proposed from the
corpus; each cites the numbers it is drawn from, and each is a calibration to re-derive against
the fixtures in section 17 rather than a universal constant (RED: "do not present one session's
Dice threshold as universal").

| Gate | Proposed threshold | Where the number comes from |
|---|---|---|
| Crisp-frame mask Dice vs reference | `≥ 0.90` **necessary, not sufficient** | current `CRISP_MIN_DICE`; but see 16.2 — a `0.902986` mask was anatomically wrong |
| One-way source→candidate boundary residual, p95, native scale | `≤ 3.0 px` | valid self-fit measured `1.0 px`; real face-fit Chamfer means span `2.329–3.158 px`; the two failures measured `11.788 px` and `85.095 px` |
| Candidate→source residual, p95 | reported separately, same bound | ADAPT §3: report source-only and candidate-only residuals separately "so a clipped swash cannot hide behind a high area-overlap score" |
| Component count / hole count | exact equality with the source word | `<word B>` failed at source holes 2 vs candidate holes 3 |
| Blur/entry-frame Dice | `≥ 0.60`, and on heavily blurred entry frames assert nonzero **support bbox / composite delta** instead of a threshold bbox | current `BLUR_MIN_DICE`; <ref-22> — `threshold_bbox >= 128` "may legitimately be null" on entry frames |
| Codec-artifact rescue band | `≤ 2 px` around the **reference** boundary only | NFS §7; the existing implementation is right to exclude the candidate's own boundary |
| Ink distance (core RGB, 8-bit units) | `≤ 6.0` pass, `> 8.0` fail, between = report ambiguous | <ref-22> fitted MAE: best-supported models `1.50 / 1.54 / 3.45 / 5.01`; rejected models `7.73 / 13.37 / 22.58` |
| Timing | exact — zero tolerance on the rendered-ink frame set and on the blank-frame set | e.g. a blank set `{115,116}`; the gate requires blank frames handled explicitly, never as empty-mask `NaN` comparisons that silently pass |
| Coverage | every state, including the **final** state, at first-detectable / readable / crisp / hold / exit — never midpoint-only | EBG §4; e.g. a final state still visible on the last frame "is not blank" |

Every verdict carries its evidence limit and `creative_approval: PENDING`. A green QC run is
machine parity on the compared frames — not a normal-speed watch, not an enlarged native contour
review, not human approval.

---

## 16. Proving QC can fail

**Rule 16.1 — a green QC is worthless until the gate is shown red-capable.** Run the metric and
preprocessing path against a **positive control** whose candidate mask is known to derive from
the exact *complete* native source component, and a **holdout target** present in both exemplar
and candidate but not used to calibrate or generate that candidate. Report
`RED_CAPABLE_CONFIRMED` only when the complete-source control passes *and* the holdout fails. A
target-only failure is weak evidence — a bad threshold, crop or alignment can make every word red
(RED).

**Rule 16.2 — the current crisp gate cannot fail the defect it exists for.** This is an open
implementation gap, recorded here as doctrine rather than left to be rediscovered. In the R5B
case the complete-source comparison produced:

| comparison | source size | candidate size | Dice | source→candidate edge p95 | verdict |
|---|---:|---:|---:|---:|---|
| clipped builder input → R5B `<word A>` | 323×175 | 531×288 | `0.965135` | `1.0 px` | implementation-self-fit only |
| complete RefB `<word A>` → R5B `<word A>` | 323×271 | 531×288 | `0.902986` | **`85.095 px`** | FAIL complete-source anatomy |
| complete RefB `<word B>` → inherited `<word B>` | 199×86 | 283×125 | `0.745832` | `11.788 px` | FAIL holdout anatomy/topology |

Dice `0.902986` clears `CRISP_MIN_DICE = 0.90`. The mask was missing the entire lower descender loop —
1,754 source-only pixels. A symmetric area-overlap score is structurally blind to an amputation
that removes a thin extremity from a heavy glyph; **the one-way residual and the hole count are
what caught it.** Therefore `qc_state` must gate on Dice *and* the one-way source→candidate
residual *and* topology equality, all three, before a crisp state may pass. Until it does, a
green caption QC is scoped as "no defect found by an area metric", not as parity.

**Rule 16.3 — normalise without erasing anatomy.** Tight-crop each word, apply **one isotropic
scale** (normally common height), optimise translation only. Never independently resize X/Y, fit
each word to its candidate bbox, rotate, repair topology or edit contours before scoring (RED).

**Rule 16.4 — never isolate a stacked word with a horizontal row cutoff.** A descender, entry
stroke or terminal swash crosses that boundary while remaining a separate connected component.
Isolate by component identity plus native overlay proof, and reject any crop that removes a
source component or produces a large one-way edge residual. This rule *is* the R5B `<word A>`
failure, stated as prevention (ADAPT §3; REMED §4).

**Rule 16.5 — prove locality in memory, not across encodes.** To show a child revision changed
only the frames it claims, run both compositors against the same decoded base/reference frames
and compare pre-encode RGB arrays. Two separately encoded ProRes/H.264 files differ across the
whole frame for codec reasons. Build the allowed-change support per frame from the actual alpha
**after** the renderer's Gaussian blur (a sharp-mask bbox is not the lifecycle support), then
assert `changed_frames == allowed_changed_frames` and
`max_changed_pixels_outside_actual_blurred_union_support == 0` (ADAPT §6).

**Rule 16.6 — a rejected revision contributes zero positive pixels.** No contours, transforms,
palettes or style decisions are inherited from a human-rejected candidate unless a reviewer
separately approved a named, hash-bound subset. It may be used only to reproduce the failure,
demonstrate a regression goes red, and prove the defect no longer occurs. Do not seed a new
contour reconstruction from a rejected mask, and do not repair a clipped contour by grafting a
generic font fragment onto it (REMED §3).

---

## 17. Evidence inventory — what each fixture class can test

The fixtures below are described by *class*. Real reference media, their hashes and their
on-disk locations are project data and are not part of this repository; point the tests at your
own corpus through environment variables (tests skip, never silently pass, when absent).

### 17.1 Sealed state-program authority — the primary fixture

A sealed typography-authority contract for one exemplar (e.g. 39 states, gap-free over
`0..239`, an explicit blank set, per-state `entry{first_detectable, readable_blur, near_crisp,
first_crisp}` / `hold` / `exit`, accumulate-vs-replace behaviour, a `treatment_contract.forbidden`
list and `regression_gates.geometry.anisotropic_per_word_scale_forbidden`). Useful companions:

| Artifact | Tests |
|---|---|
| an independently derived frame-state ledger | the disagreement fixture (two ledgers that classify one onset frame differently) |
| an evidence manifest | hash-binding of every evidence file; decoded-essence hash |
| two remuxes of the same reference | **the three-layer identity fixture**: different containers, identical packets and decoded pixels |
| a frozen independent validator script | container/packet/decoded hashes, frame count, raster, fps, state count, blank set, text and case, onset/blur-out windows, `exact_face_name is None`, `render_allowed is False` |

Re-running that validator is the conformance gate for the contract; by design it makes no claim
about any candidate render.

### 17.2 Source-contour manifests — the source-contour path fixture

A source-contour manifest (one record per word: `source`, `method`, `guide_policy`,
`representative_frame`, `support_frames`, `bbox_xyxy`, ink pixel count, mask `path` + `sha256`,
`edge_evidence` {`source_edge_gradient_median`, `nearby_control_gradient_median`,
`edge_to_control_ratio`}, `font_face: null`, `shape_transform: null`, `anisotropic_scale:
false`) plus its mask PNGs. Tests: the manifest shape; the edge-evidence quality signal (a high
ratio predicts a trustworthy contour); and, with a known-amputated mask and a holdout mask, the
**section 16.2 red-capability fixture**.

### 17.3 Approved baselines

- A caption-block blueprint (`{start_frame, end_frame_exclusive, text, style, start_s, end_s}`
  with several style tiers) — the incremental word-build and long-hold fixture.
- A lifecycle-phase blueprint (`phase` ∈ blur-heavy / blur-medium / near-crisp / crisp, with
  `text: null` for `style: "none"`) carrying one hash-locked proven face beside a provisional
  one — the phase-aware QC fixture and the independent-axis mapping fixture (rule 4.4).
- A static script-caption asset authored with a 1.15× vertical scale — the **scale-convention
  fixture** for rule 4.4.

### 17.4 Negative fixtures — required to stay red

Any legacy per-reel renderer that forces a candidate into a hard-coded target envelope and then
asserts the resulting bbox equals that envelope. It "proves only that the resize instruction
ran." Such files are preserved as required negative regressions and their hashes are never
reusable as positive templates.

---

## 18. Per-reel caption scripts are superseded

The factory reaches the engine through one entry point:

```text
reelctl captions program   # reference intake, feeding BLUEPRINT_LOCKED
reelctl captions validate  # contract gate before ASSETS_LOCKED
reelctl captions render    # the caption stage of composition
reelctl captions qc        # the caption half of VISUAL_QC
```

Legacy per-reel caption scripts are **superseded, not deleted**. Deleting them would violate the
append-only invariant: approved baselines are never overwritten and rejections stay on disk as
negative fixtures with receipts. A legacy envelope-fitting renderer in particular is the standing
negative fixture for the circular envelope-warp bug and must remain readable.

Nothing in the pipeline calls them, and no new per-reel caption script is permitted. If a reel
needs behaviour the engine lacks, the engine gains it behind a failing test first — that is how
the named geometry exception (rule 4.3) and the difference-family operator gap were handled.

The CLI exposes no publish or upload command, and a test asserts it. Rendering an
unsealed contract requires an explicit `--allow-unsealed-local-review` flag, and the render
receipt records that the flag was used and that publication is not authorised.
