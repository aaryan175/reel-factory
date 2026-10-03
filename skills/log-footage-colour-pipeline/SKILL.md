---
name: log-footage-colour-pipeline
description: Build and verify a colour pipeline for camera-log and mixed footage in reels - identify the encoded profile from metadata (not appearance), apply a range-correct technical transform (e.g. Sony S-Log3/S-Gamut3.Cine to Rec.709 via the official LC-709 LUT), measure exposure, keep one consistent house look across shots, and prove the result with scopes and decoded-range checks. Use when grading or normalizing log footage, when shots look washed out/flat/green/too saturated, or when designing a repeatable look.
---

# Log Footage Colour Pipeline

Classify the pixels first, then transform, then look, then trim. Keep technical normalization and creative intent in separate, named layers.

## 1. Identify the encoded state (never from "it looks flat")

Evidence, strongest first:

1. Camera-vendor metadata (sidecar XML, or metadata embedded in the file).
2. NLE clip attributes.
3. Shoot notes / camera settings.
4. Container tags (`ffprobe` colour fields) - often empty or generic.
5. Chart or grey-card waveform behaviour.
6. Visual impression - label as low confidence.

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt,color_range,color_space,color_transfer,color_primaries,bits_per_raw_sample -of json CLIP.MP4
exiftool -G1 -a -s -api LargeFileSupport=1 CLIP.MP4 | grep -iE 'gamma|primaries|model|picture|iso|white'
exiftool -ee3 -G3 -a -s CLIP.MP4 | grep -iE 'CaptureGammaEquation|CaptureColorPrimaries'   # embedded timed metadata
```

Rules:

- Empty container colour tags mean **unknown**, not Rec.709.
- A flat-looking picture may be a log profile, a flat picture style, HLG, or a baked look. Profiles to distinguish: camera log (e.g. S-Log3), cinema-style display profiles (e.g. S-Cinetone), HLG/BT.2020, clips recorded with a user LUT baked in, and native BT.709. Each needs a different input handling.
- A monitoring/display LUT on the camera does not mean the recording was transformed; an "embed LUT" flag stores metadata, not baked pixels.
- Only transform a clip when the profile tuple is proven (e.g. exactly `S-Log3 + S-Gamut3.Cine`). Otherwise mark it `needs-review` and do not apply the transform.

## 2. Technical transform (range-correct)

For Sony S-Log3/S-Gamut3.Cine, the vendor's LC-709 technical LUT is a reasonable normalization to Rec.709. (LC-709 "Type A" and stronger looks such as Cine+709 are creative options - do not substitute them silently.) Hash-lock the LUT file you use.

A tested ffmpeg route for a full-range-in/full-range-out LUT:

```bash
VF="scale=iw:ih:flags=lanczos:in_range=full:out_range=full:in_color_matrix=bt709,\
format=gbrpf32le,\
lut3d=file=LC709.cube:interp=tetrahedral,\
format=gbrp16le,\
colorspace=ispace=gbr:iprimaries=bt709:itrc=bt709:irange=pc:all=bt709:range=tv:format=yuv422p10:dither=fsb,\
limiter=min=64:max=940:planes=1"
ffmpeg -nostdin -v error -i SRC.MP4 -map 0:v:0 -an -vf "$VF" \
  -c:v prores_ks -profile:v 3 -pix_fmt yuv422p10le \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv NORM.mov
```

Check the LUT's documented input/output range before choosing `in_range`. Capability-gate the binary (`ffmpeg -h filter=lut3d`, `-h filter=colorspace`) and **prove the chain on your build** with the levels-identity probe and range checks in `references/colour-lessons.md` - filter negotiation differs across builds, and a chain that is correct on one can double-contract range on another. Make one 10-bit 4:2:2 normalized mezzanine; derive 8-bit 4:2:0 deliveries from it once. No auto-levels, auto-WB or adaptive tone mapping in the technical stage.

In an NLE: either colour-managed project with explicit per-clip input assignment, **or** manual input CST -> working space -> output CST. Never both, and never a creative LUT that expects log after the pixels already left log. Double transforms are the first thing to rule out when footage looks wrong.

## 3. Exposure measurement

- Reference points for Sony S-Log3 (vendor technical summary, 10-bit code values): black ~95, 18% grey ~420, 90% white ~598. Use them only with a correctly decoded source and a known target.
- Measure, don't eyeball: `signalstats` Y percentiles per shot, a grey/skin ROI after normalization, and the clipped-highlight fraction.
- Log in 8-bit 4:2:0 has limited code values: broad exposure/WB/contrast moves first; expect skies, walls, skin gradients and saturated LEDs to band or break first under narrow keys or steep curves. A higher-bit intermediate prevents further loss but cannot restore missing data.

## 4. Consistent house look

Two valid policies - pick one per project and write it down:

- **House look (default for a series):** one proven technical transform + one look applied identically to every shot. Per-shot trims only on shots a reviewer names as off. Gives a consistent identity and is easy to audit.
- **Matched look:** technical transform -> per-clip exposure/WB balance -> shared look per lighting family -> small per-clip finishing trims. Use when sources vary widely.

Either way: a pilot that worked on one clip is a starting recipe for that profile and lighting family, not a universal LUT. Grade and approve the exact interval used in the edit, after inspecting the whole take through the transform.

Match order: exposure/offset -> WB/tint -> black/mid/high distribution -> saturation -> specific hues/skin. "Less dull" does not mean "more saturation" - check density, white balance and contrast first. Preserve natural saturation in practical-light scenes.

## 5. Verify

- Scopes on the output transform: waveform for clipping/crush, RGB parade for casts, vectorscope for gamut excursions. The skin line is a guide, not a target for every complexion or coloured light.
- Decoded legal range on the delivered file (8-bit 16-235 / 10-bit 64-940).
- Determinism: run the same transform twice single-threaded and compare `framemd5` (see references).
- Re-import the export and compare to the timeline; separate source, grade, monitor and export artefacts before changing the grade.
- Viewer differences between players are often colour-management/tag interpretation, not changed pixels. Keep a Rec.709/Gamma 2.4 master; do not "fix" a viewer mismatch by corrupting the master.

Delivery starting point for vertical social: 1080x1920 H.264 High, 4:2:0, intended frame rate, AAC 48 kHz, explicit Rec.709 tags. Platform limits change - check current documentation.

Recipes and lessons: `references/colour-lessons.md`.
