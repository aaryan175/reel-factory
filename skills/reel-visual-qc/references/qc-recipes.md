# QC Recipes and Lessons

## A. Caption contrast gate (alpha-aware)

Measure on the caption's **core** pixels (alpha >= 192) against the footage directly behind it, using linear relative luminance and WCAG-style ratio `CR = (Lmax + 0.05) / (Lmin + 0.05)`.

```python
import numpy as np
def lin(c):  # sRGB 0..1 -> linear
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
def lum(rgb):
    r, g, b = lin(rgb[..., 0]), lin(rgb[..., 1]), lin(rgb[..., 2])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def caption_contrast(composited, background, alpha):
    """composited/background: HxWx3 float 0..1 (same frame with and without caption); alpha: HxW 0..255."""
    core = alpha >= 192
    lf, lb = lum(composited)[core], lum(background)[core]
    cr = (np.maximum(lf, lb) + 0.05) / (np.minimum(lf, lb) + 0.05)
    return {"C10": float(np.percentile(cr, 10)), "C50": float(np.median(cr)),
            "frac_below_3": float((cr < 3.0).mean())}
```

| Result | Rule |
|---|---|
| PASS | C10 >= 3.0, C50 >= 4.5, and < 10% of core pixels below 3:1 |
| SEVERE | C50 < 3.0, or >= 50% of core pixels below 3:1 |
| Coloured/inverted ink alternative | a pixel passes if luma ratio >= 1.5 **or** CIE76 deltaE >= 20 vs background; frame passes if <= 10% of core pixels fail |

Use a local background estimate (the same frame rendered without the caption). Evaluate every caption state on its real footage, not on a black specimen. White ink over bright footage generally needs a shadow or underlay; record its radius and opacity.

## B. Decoded legal-range guard

Tags do not move pixels. If a high-quality H.264 review encode (e.g. CRF ~12) rings outside 16-235 after decode, add a pre-encode guard and re-measure:

```text
lutyuv=y='clip(val,30,221)':u='clip(val,16,240)':v='clip(val,16,240)'
```

That specific margin is an example from one codec setting; derive your own minimal guard from post-encode measurements, and re-measure after any codec or CRF change. Tighter chroma clamps (e.g. 32-224) visibly break saturated primaries. Clamp luma with `limiter=...:planes=1` rather than applying luma limits to chroma.

## C. RGB composite levels identity

Any YUV -> RGB -> YUV compositing step must be a numerical identity when it does nothing. Probe it before judging the grade:

1. Pick clean frames (no captions/effects) covering low-key, mid and bright scenes.
2. Render: decoded passthrough; current no-op composite; explicit limited->full->limited composite.
3. Decode to `yuv422p10le`, compute Y p05/p50/p95 and `p95 - p05`.

Gates: |shift| of p05/p50/p95 <= 10 code values (10-bit); dynamic-range ratio 0.98-1.02. A broken graph (limited range contracted twice) once lifted p05 by ~44 codes and cut median dynamic range ~15%, making every shot look washed out. Verify on **your** ffmpeg build - filter negotiation differs between versions, so a graph that is correct on one build can contract on another.

## D. Scope proof for partial re-renders

Render masters in an intra-frame codec (ProRes) and compare with `framemd5`:

```bash
ffmpeg -v error -i A.mov -map 0:v:0 -f framemd5 a.md5
ffmpeg -v error -i B.mov -map 0:v:0 -f framemd5 b.md5
diff <(cut -d, -f6 a.md5) <(cut -d, -f6 b.md5) | grep -c '^>'   # changed frames
```

Require `changed_frames == allowed_changed_frames`. Do not use long-GOP H.264 frame hashes for this; an edit can perturb neighbouring frames.

## E. Per-shot triage metrics

Per shot (excluding cut and pulse frames): mean luma; dark % (< 8); clipped-white % (> 247); mean saturation; Laplacian variance (blur); median optical-flow magnitude (motion); duplicate-frame runs; per-cut difference. Treat these as triage that points at frames, never as the verdict.

## F. Lessons

**Q1.** Rule: evaluate contrast on the composited glyph. Why: an ink colour that passes on a swatch can vanish on matching footage. Check: contrast computed from composited vs uncaptioned frames.

**Q2.** Rule: normalize rotation and SAR before crop, then `setsar=1`, and reject any non-square segment before concat. Why: stream-copy concat carries one SAR; a portrait insert forced into a landscape raster gets flattened (9:16 displayed as 16:9 widens ~3.16x). Check: probe every segment's SAR.

**Q3.** Rule: end on `-frames:v N`. Why: `-shortest` dropped the final frame when audio ended a few ms early. Check: decoded count == N.

**Q4.** Rule: an alpha mask from a grayscale `L` image is used as alpha, not converted to RGBA. Why: `convert('RGBA')` makes alpha opaque and blacks out everything outside the glyph. Check: composite a mask over mid-grey and inspect.

**Q5.** Rule: mask streams are put on the source timebase before frame-sync. Why: off-by-one mask frames make captions lead or lag. Check: prove frame N maps to N, not N+/-1.

**Q6.** Rule: recompute every manifest hash before promotion. Why: stale hashes bless a changed file. Check: hash at promotion == hash in receipt.

**Q7.** Rule: tests must be able to fail. Why: a builder and its checker that share a bug (e.g. both cropping the same glyph tail) "agree". Check: keep a positive control and a known-bad holdout in the suite.
