# Colour Recipes and Lessons

## A. Determinism proof

```bash
for i in 1 2; do
  ffmpeg -nostdin -v error -threads 1 -filter_threads 1 -i SRC.MP4 -map 0:v:0 -an \
    -vf "$VF" -f framemd5 - | shasum -a 256
done   # both digests must match
```

Define determinism at the decoded-pixel layer; container hashes also depend on muxer metadata.

## B. Levels identity probe (run before judging any grade)

A no-op YUV -> RGB -> YUV path must leave pixels unchanged. Render passthrough vs the no-op composite on clean frames, decode to `yuv422p10le`, and compare Y p05/p50/p95. Gates: |shift| <= 10 codes (10-bit), `(p95-p05)` ratio 0.98-1.02. The classic failure is limited range being contracted twice (`range=tv` on input that is later declared `irange=pc`): shadows lift, highlights drop, everything looks washed out - and no grade fixes it.

One path that passed an identity test on one build:

```text
scale=W:H:in_range=tv:out_range=full:in_color_matrix=bt709:out_color_matrix=bt709,
format=yuv444p10le, format=rgb24,
# ...RGB operations...
scale=W:H:in_range=full:out_range=tv:in_color_matrix=bt709:out_color_matrix=bt709,
format=yuv422p10le
```

The forced full-range 4:4:4 intermediate matters. Treat any filter family as untrusted until the probe passes on your installed ffmpeg.

## C. Strict legal range after dithering

`colorspace=...:range=tv:dither=fsb` can still put an edge sample one code outside nominal. Add `limiter=min=16:max=235:planes=1` (8-bit) or `min=64:max=940:planes=1` (10-bit) **after** the conversion, then re-run `signalstats` on the filtered pre-encode stream and on the decoded delivery.

```bash
ffmpeg -nostdin -v error -i OUT.mov -vf 'signalstats=stat=brng,metadata=mode=print:file=-' -f null - | grep -c 'BRNG=0\.0*[1-9]'
```

Setting tags is not a conversion: a full-range RGB chain must be explicitly converted to limited YUV before an SDR delivery.

## D. Bounded per-shot matching (when matching is used)

Independent per-channel RGB histogram matching can swing a cast badly (one shot went from G/R 0.78 to 1.65 - visibly green). Prefer smooth gamma/saturation adjustments or a bounded monotone curve:

```text
y = x + clip(strength * (target_y - x), -max_delta, +max_delta)
smoke start: strength = 0.4, max_delta = 0.12; keep the curve monotonic; exclude pulse/flash frames from the fit
```

Gates per shot: q10/q50/q90 luma error <= 0.05; saturation error <= 0.06; highlight coverage within 5 percentage points. Measure on caption-masked pixels.

## E. Per-shot cast diagnostics

Trace a bad shot through stages - source -> normalized -> matched -> composited -> encoded - with G/R, B/R, opponent `G-(R+B)/2`, Lab a*, `blue_excess = B-(R+G)/2`. The stage where the metric jumps is the cause. Avoid whole-frame HSV saturation on near-black shots: black noise reports maximum HSV saturation.

## F. Lessons

**C1.** Rule: prove the profile from metadata before transforming. Why: a look applied to the wrong profile clips or crushes irrecoverably. Check: profile tuple recorded per clip with its source (sidecar/embedded/notes).

**C2.** Rule: one technical transform per clip, applied once. Why: input management + CST + LUT stacked double-transforms. Check: bypass all LUTs/CSTs and inspect project colour management first.

**C3.** Rule: a LUT is a rendered look, not a neutral CST, unless documented as technical. Why: creative LUTs bake contrast and saturation choices. Check: LUT provenance and hash recorded.

**C4.** Rule: gate colour per shot. Why: timeline averages hide one blue or green shot. Check: per-shot report attached; any single failing shot fails the gate.

**C5.** Rule: judge local fixes in motion with the output transform on. Why: a key that looks clean paused can chatter, crawl or pump at speed. Check: full-speed playback of each keyed shot.

**C6.** Rule: stop a local correction when its benefit is visible only paused/zoomed, or when each fix needs a narrower key. Why: artefacts grow faster than gains in 8-bit sources. Check: contextual A/B against the previous version and adjacent shots.

**C7.** Rule: a downloaded reference is aesthetic guidance, not colorimetric truth. Why: unknown ICC, gamma and compression. Check: reference-match claims are labelled as appearance only.

**C8.** Rule: deliveries come from originals, not proxies or render cache. Why: proxies can leak into the final export. Check: re-import the export and confirm resolution and bit depth.

**C9.** Rule: when a reviewer rejects a grade, revoke the pass and diagnose transform, balance, density, per-shot WB and continuity before asking for taste notes. Why: most "grade" complaints are technical. Check: diagnosis notes per layer before any new look is tried.
