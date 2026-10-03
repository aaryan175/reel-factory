# Feasibility Lessons

Format: **rule / why / check**.

**F1. Map picture states, not shots.**
Rule: feasibility runs over the blueprint's picture-block ledger. Why: a held smear or pulse state may need its own source moment. Check: `len(feasibility.blocks) == len(blueprint.blocks)` and IDs match.

**F2. Caption geometry is a feasibility constraint.**
Rule: lock where text sits before judging a clip. Why: a busy or bright region under the caption fails contrast no matter the grade. Check: each `exact`/`equivalent` block was inspected with the caption box overlaid.

**F3. Role equivalence needs named attributes.**
Rule: an equivalent must match the role's verb, subject scale, screen direction, motion energy and lighting family, and the evidence string must name them. Why: "similar vibe" is not reviewable. Check: evidence lists the attributes compared.

**F4. Action must happen inside the slot.**
Rule: confirm the verb is readable from the first frame of the intended window. Why: a clip whose action starts after the slot ends reads as dead air. Check: native-frame strip of the window is attached.

**F5. Machine rank is a lead.**
Rule: the shortlist score orders inspection, it never decides. Why: embeddings and colour stats match backgrounds, not actions. Check: every accepted ID has a human/visual observation in evidence.

**F6. Blocked is a valid, useful result.**
Rule: report the gap list and stop. Why: rendering around gaps produces a reel that technically passes and visibly misses. Check: no selection or render files exist after a blocked lock.

**F7. Never borrow the reference.**
Rule: the reference video and research downloads are ineligible as sources. Why: they contain someone else's footage and burned-in text; using them is both a rights problem and a parity fake. Check: every evidence clip resolves inside the authorized root.

**F8. Re-hash on every run.**
Rule: rehash candidate clips when re-running feasibility. Why: a library can change between runs. Check: evidence hashes match the current inventory.

**F9. Coverage changes need a new lock.**
Rule: indexing new footage creates a new feasibility revision; the old lock is kept. Why: append-only history explains why a block moved from missing to exact. Check: lock files are versioned, not overwritten.

## Example blocked summary

```text
Mode: exact adaptation. Blocks: 24. Exact 15, equivalent 5, missing 4.
Exact ratio 0.625 < 0.80 -> BLOCKED (mode_switch_required).
Missing: p006 (establishing wide, dusk), p011 (hands detail, close), p017 (crowd pass), p022 (final reveal).
No render exists. Decision needed: approve grammar adaptation, or supply footage for the 4 missing blocks.
```
