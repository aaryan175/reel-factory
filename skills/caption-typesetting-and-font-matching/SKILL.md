---
name: caption-typesetting-and-font-matching
description: Identify the font in burned-in video captions and typeset new captions that match a reference - multi-word metric fitting, component fingerprints, exact font file and collection index, size/tracking/baseline recovery, ink colour and blend-mode modelling (source-over vs difference/invert), shadows, lifecycle timing, and readability. Use when asked "what font is this?", "match these captions", "the font looks wrong / changed", or when rendering captions for a reference-led reel.
---

# Caption Typesetting and Font Matching

Captions carry two separate contracts: **semantics** (exact words, frame ranges, lifecycle) and **rendering** (face, size, tracking, baseline, ink, blend, shadow). Passing one says nothing about the other.

**Typeset, don't trace.** Render captions from real font files with a text engine. Traced or thresholded masks of someone else's burned-in text are diagnostics, not production assets.

**Licensing.** Identifying a font does not grant a licence to use it. For anything you publish, license the original font (check the desktop/video/social-media terms of the licence) or substitute an open-licensed font (e.g. SIL OFL) and label the result as an adaptation. Fonts bundled with an operating system are often licensed for use on that system only - read the terms before embedding them in distributed video.

## 1. Lock the caption ledger first

From gap-free frames (see `reel-reference-forensics`): exact raster strings (case, punctuation, deliberate spellings), every no-text frame, progressive builds as separate states, role per state (`connector`, `running`, `display`, `hero`), alpha bbox and baseline per state. Compact form:

```text
000-008 text A · 009-012 text B · 013-017 text C · 018-023 none
```

Lifecycle per state: first detectable -> readable blur -> first crisp -> hold -> exit. Determine entry and exit separately; a whole-frame transition after the caption is gone is not a caption exit. Three identical smeared frames are a static smear hold, not a progressive animation - compare overlay hashes.

## 2. Identify the face

1. Collect sharp exemplar words at native resolution (for stacked text, the earliest fully drawn frame before the next row appears). Prefer dark, uncluttered backgrounds.
2. Render a diagnostic specimen (`g a t e i k m`, plus the actual words) for each candidate face. Discriminators: square vs round `i` dot, one- vs two-storey `g`, `a` bowl and terminal, `t` cap and foot, `e` aperture, `w/m/n` proportions, ascender/x-height/descender ratios.
3. For TTC/OTC collections, load by **index** and read back the family/style/PostScript name from the loaded face. A path alone can silently load Regular while the notes say Bold.
4. **Multi-word global fit**: one nominal size and one tracking value across several words. A face that fits one word only after independent X/Y scaling is not a match.
5. **Component fingerprint** for close neighbours: per-glyph x, y, w, h and area left to right. Cumulative x drift within 1-2 px means tracking is zero. Compare counters, dots, apostrophes and loops - edge-template scores alone lock onto background architecture.
6. Overlay review: observed mask cyan, candidate magenta, overlap white.
7. Script/calligraphic faces: match the lowercase run decisively; treat ornate capitals and swashes separately and say which are unresolved.
8. Verdict per face: `exact` (font bytes proven, or contours verified) / `nearest candidate` / `unknown`. For a 1:1 build, "nearest available" is a typography reject - say so rather than shipping it as a match.

## 3. Recover geometry

```python
from PIL import Image, ImageDraw, ImageFont
font = ImageFont.truetype(path, size=SIZE, index=FACE_INDEX)
print(font.getname())                                  # verify the loaded face
def draw_tracked(draw, x, y, text, font, tracking, fill):
    for i, ch in enumerate(text):                       # kerning-aware prefix positions
        xi = x + font.getlength(text[:i]) + i * tracking
        draw.text((xi, y), ch, font=font, fill=fill, anchor="ls")   # left-baseline anchor
```

- Place by **baseline** (`anchor="ls"`). Centring from `textbbox` ignores the bbox offset and has shifted words by 100+ px in practice; alpha-centring x-height words places them visibly too high.
- Recover the baseline from x-height, ascender and descender words together; they must share one baseline. Rounded glyphs overshoot the baseline by a few pixels - that is correct.
- Only one uniform scale plus translation between reference and target canvas (e.g. 1080 -> 1080 is 1.0; a 1916 -> 1920 raster is ~1.002, not a different aspect).
- For complex scripts or ligatures, shape with HarfBuzz (`hb-shape`, `hb-view`) and record shaper and rasterizer versions.

## 4. Ink, blend and shadow

Treat changing caption colour across footage as evidence of a compositing operator. Fit models on the same samples and compare residuals (RMSE/MAE, parameter count, BIC):

```text
flat fill | spatial/temporal ramp | masked copy of footage
SourceOver:  O = B + a(F - B)          alpha solve (white F): a = (O - B) / (1 - B)
Difference:  O = B + a(|B - S| - B)    white S gives 1 - B; alpha solve: a = (O - B) / (1 - 2B), reject near-zero denominators
inverted-luma LUT
```

- Signatures of footage-reactive (difference/invert) ink: negative correlation between ink and underlay, channel-order reversal. Neutral-white source-over cannot reverse channel order.
- White Difference, Exclusion and Invert are pixel-identical at the white endpoint. Report the equation ("difference-family inversion"), not an editor's menu name.
- Channel registration: a cross-correlation peak at (0,0) between channels means no offset RGB ghost copies; colour fringes are then in-glyph gradient, whole-frame effect, or 4:2:0 bleed.
- Colours you report are decoded samples, not design hex.
- Add outline, glow, blur or shadow only if an enlarged edge profile proves it. Where readability needs help, a soft drop shadow (keep the radius modest, e.g. <= 20 px at 1080 wide) is the least intrusive fix; record it as an adaptation.
- An explicit instruction ("make it white") overrides inferred reactive ink. Recolouring never fixes a wrong face.

## 5. Typeset and test

- Compose captions last, over the graded footage. Render per-state RGBA plates; hash each plate.
- Pre-compose board on **real footage**: short x-height word, ascender phrase, the emphasis word, the largest state and the longest line. Check safe margins, optical centring, antialiasing, contrast.
- Readability gate: alpha-aware contrast on the composited glyph (see `reel-visual-qc`).
- Collision gate: pairwise alpha intersection and minimum contour separation per frame when layers overlap.
- Executable tests: font file hash + face index; size and tracking; baseline mode; approved fill and explicit absence of unproven effects; exact frame coverage and hard switches.
- If a reviewer says an earlier caption was right, restore that exact raster byte-for-byte rather than re-deriving it.
- Any change to face, size, tracking, baseline or ink invalidates every downstream render and its QC.

Lessons and worked examples: `references/typography-lessons.md`.
