"""Hand-written analysis appended to the generated work order by report.py (--analysis).

Kept in code rather than in a loose markdown file so the prose ships with the tool that
emits it.  This is a TEMPLATE: replace the bracketed prompts with the findings of your own
sweep.  The section headings encode the reading order that has proven useful: gate defects
before reel defects, and the fix order by what actually blocks a delivery.
"""

ANALYSIS = r"""
## 3 · Cross-cutting findings

### 3.1 · Check the gates before the reels

Before trusting any per-reel verdict, look for a gate that is wrong *as wired*. The classic
case: `wordtruth.triage_delivered` marks a state `UNVERIFIABLE_BY_READER` when the reference's
own crop does not read as the declared text. If the declared text was itself wrong (we wrote
different words from the reference), that control fires and absolves the defect. The fix —
implemented in `w1_reclass.py` — keys the control on whether the **reference read resolved**,
not on whether it matched the declaration: reference resolves to X, delivery resolves to Y,
X ≠ Y is a words defect whatever the contract declared.

### 3.2 · Calibrate thresholds against review verdicts, not taste

Per-state string equality is too brittle to block on (OCR stability is not correctness — a
read can be perfectly stable and still wrong). Block on the **reel-level agreement rate** over
states where both sides resolved, and set the threshold inside the empty band between the
lowest-scoring approved reel and the highest-scoring words-rejected reel.

| reel | review verdict | agree | differ | agreement |
|---|---|---|---|---|
| [key] | [APPROVED / REJECTED] | [n] | [n] | [rate] |

### 3.3 · Independent gates landing on the same states

When W1 (words) and W2 (anatomy) flag the *same* state IDs, that is strong corroboration that
the defect is damaged glyphs being misread rather than different words. Record those overlaps.

### 3.4 · Captions missing entirely

A contract with text and timing but no geometry cannot be addressed by W1/W2/W5. Difference the
delivered file against its own caption-free picture base (`basediff.py`, W5c) instead: no OCR,
no bbox, no reference required.

### 3.5 · Variant families

When many variants share one recovered plate, tabulate W2 per state across the family. A
state broken in every variant points upstream of compositing (re-recover the plate; do not
re-render).

### 3.6 · Where the gates could not see (honest register)

| limitation | evidence | consequence |
|---|---|---|
| W2 needs a text-free frame inside the state's own shot | [states UNMEASURABLE] | anatomy is blind on fast per-word captions |
| W5's box-luma proxy cannot tell glyphs from a flat field | [field-ceiling hits] | W5 "missing" needs W1/W2 corroboration |
| Font identity is not measured by this toolkit | — | name it as blind-spot debt on every card |

---

## 4 · What the fix lanes should do first

1. Fix any gate defect found in §3.1 before re-captioning anything.
2. Re-recover plates that are broken across a whole family; do not re-render them.
3. Build captions for any reel W5c reports as ABSENT.
4. Re-caption words-rejected reels from the reference's words.
5. Give W2 something to measure on per-word reels (adjacent-shot background or picture base).
6. Then, and only then, font identity.
"""
