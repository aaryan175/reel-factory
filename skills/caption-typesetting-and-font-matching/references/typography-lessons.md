# Typography Lessons

Format: **rule / why / check**. Examples use neutral, widely known faces.

**T1. Several words, one size.**
Rule: fit size and tracking globally across 3+ words with mixed x-height/ascender/descender content. Why: a single word can be matched by the wrong face at a fudged size. Check: residual reported per word at the shared parameters.

**T2. Never stretch to fit.**
Rule: forbid independent X/Y resize of a candidate to the observed envelope. Why: it manufactures a perfect bbox match (IoU 1.0) for the wrong face. Check: code has one scale variable; audits report `scale_x/scale_y` and they agree.

**T3. Load the face you claim.**
Rule: specify the collection index and assert the loaded name. Why: a `.ttc` opened without an index silently returned Regular while the record said Bold. Check: `font.getname()` logged next to every render.

**T4. Close serif neighbours need components.**
Rule: when two display serifs (e.g. a Bodoni and a Didot) produce near-identical word bboxes, compare per-glyph geometry and stroke contrast. Why: outer bbox hides different bowls, terminals and advances. Check: component table plus a cyan/magenta overlay at native size.

**T5. A bank ranking is not a verdict.**
Rule: verify the top-ranked face at native scale before accepting it. Why: in a large font-bank search, a common grotesque ranked first by Dice score while a different geometric sans was correct at native size. Check: native-scale overlay of the top 3 candidates.

**T6. Silhouettes are diagnostics.**
Rule: a thresholded or connected-component mask is not a production matte until a native-size topology board proves every loop, dot and swash with no background rectangles or ROI clipping. Never select components with a horizontal row cutoff. Why: a row cutoff amputated a descender loop and both builder and checker "agreed". Check: topology board per word; a positive control from a complete source plus a failing holdout.

**T7. Colour gates for light ink.**
Rule: when isolating warm-white ink, start from an HSV gate such as `V >= 190, S <= 105` (wider `V >= 175, S <= 140`) and do no morphology until topology is accepted. Why: morphology fills counters and merges dots. Check: counters and `i` dots intact on the board.

**T8. Baked strokes darken thin scripts.**
Rule: check the share of near-black pixels inside a script caption's raster. Why: a raster with over half its visible pixels near-black came from a baked stroke, reading as "black text" over footage. Check: histogram of glyph pixels per state.

**T9. Caption timing is independent of cuts.**
Rule: lock caption state frames separately from picture boundaries. Why: captions often switch a frame before or after a cut on purpose. Check: ledger has separate caption and picture tracks.

**T10. Hierarchy ratios carry the style.**
Rule: record size ratios between roles (connector : running : hero) and keep them when adapting to new words. Why: the look comes from relationships, not absolute sizes. Check: ratios within a few percent of the reference.

**T11. Lifecycle is evidence-based.**
Rule: name blur stages by category and frame span; never invent radii or mirrored exits. Why: a hard-off exit rebuilt as a smear-out reads wrong at speed. Check: entry and exit strips per state.

**T12. Contrast is measured on the composite.**
Rule: gate readability with the alpha-aware composite test, per state, on its own footage. Why: the same white passes on shade and fails on sky. Check: contrast table per caption state.

**T13. Licence before publish.**
Rule: confirm the licence of every font in a published render, or switch to an open-licensed face. Why: a font ID is not a usage right. Check: font manifest lists file hash, licence and source for each face.
