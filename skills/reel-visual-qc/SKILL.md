---
name: reel-visual-qc
description: Quality-check a rendered reel or short video - technical integrity (decode, frame count, PTS, SAR, legal range, audio), structural parity against a reference (blocks, cuts, caption states), and visual checks measured at cited frames (caption readability/contrast, luma, clipping, colour casts, collisions, contact-sheet audits). Use before handing over any render, when asked "is this ready?", "does it match the reference?", or when a review says something looks off.
---

# Reel Visual QC

QC produces separate verdicts. A file that decodes cleanly can still look wrong; a pleasing file can still be structurally off the reference. Label each verdict and its evidence tier.

| Verdict | Owner | Can be automated |
|---|---|---|
| Technical integrity | builder | yes |
| Reference-structure parity | builder | mostly |
| Visual parity | builder, then reviewer | partly - needs eyes on cited frames |
| Creative approval | human | never |

If QC only checked frames, dimensions, decode and audio, call it **"technical-integrity PASS only"**.

## 1. Technical gates

```bash
ffmpeg -v error -i OUT.mov -f null - 2> decode.log          # must be empty
ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames,r_frame_rate,sample_aspect_ratio,display_aspect_ratio,pix_fmt,color_range,color_primaries,color_transfer,color_space -of json OUT.mov
ffmpeg -v error -i REVIEW.mp4 -vf "signalstats,metadata=print:file=-" -f null - | grep -E "YMIN|YMAX|BRNG"
```

| Gate | Threshold |
|---|---|
| Frame count | decoded count == planned N (end renders with `-frames:v N`, not `-shortest`) |
| Geometry | SAR 1:1 on every segment and the output; DAR probed |
| Legal range (decoded) | 8-bit Y 16-235, C 16-240; 10-bit Y 64-940. Measured on the **decoded** file, not inferred from tags |
| Audio | packet + PCM identity against the locked audio; start offset recorded |
| Endpoint | the exact last frame inspected at phone size |

Decoder exit 0 is not a complete decode - check the positive frame count. `BRNG=0` proves range, not a good look.

## 2. Structural gates (vs reference)

- Picture-block count and boundaries match the blueprint (exact mode); hard cuts within 2 frames of their targets (exact boundary for 1:1).
- Caption states appear on exactly their ledger frames; effect frames exact (a one-frame pulse is one frame, and the next frame is clean).
- No merged transitions; no repeated source unless approved.
- Validate any A->B boundary by searching the nominal frame +/-4 for the max luma MAD and inspecting a six-frame strip A-3..B+2. Ambiguous stays INCOMPLETE.

## 3. Visual gates at cited frames

Every visual claim cites frame numbers and has a board on disk.

- **Paired boards**: reference and candidate at the same frame indices, gap-free, 24-30 pairs per page. Normalize timebases before `hstack`.
- **Caption readability** on the alpha-composited glyph over the actual footage (not the ink colour alone). See the contrast gate in `references/qc-recipes.md`.
- **Luma/clipping per shot**: mean luma, dark % (Y < 8 in 0-255 RGB), clipped-white % (> 247). Gate per shot, not on timeline averages - one bad shot hides inside a good average.
- **Colour casts per shot**: `blue_excess = B - (R+G)/2`, plus blue/cyan-dominant pixel fractions with thresholds calibrated on your footage. If one shot fails while the timeline average improves, reject that shot.
- **Collisions**: when two text layers or text and graphics coexist, check pairwise alpha intersection and minimum contour separation per frame before composing.
- **Codec artefacts**: 1-2 px cyan/red fringes around text in `yuv420p` are chroma subsampling, not a design defect.

## 4. Contact-sheet audits

- Every tile in a sheet must be accounted for exactly once in the written audit (machine-check the tile list against the frame range).
- Still-only language: describe what a still can show ("seated on a bike") not motion ("riding").
- Exclude cut frames and pulse returns when measuring within-shot motion.
- A sheet is coverage; small text still needs a native crop.

## 5. Review tiers and the honest label

| Tier | What happened | Supports |
|---|---|---|
| A | full playback with audio at 1x | rhythm, sync, feel |
| B | local playback + frame inspection | motion quality, most visual claims |
| C | exhaustive frames only | layout, typography, colour; **no** rhythm/sync claims |

Browser playback that reached `ended` with frame callbacks proves transport, not perception. If a review was impossible, write "FAIL - review/evidence gate (not a creative-quality finding)".

## 6. When a reviewer says it looks wrong

Revoke the visual pass immediately. Do not defend it with hashes or passing tests. Re-audit reference vs candidate at the cited frames and at normal speed, then follow `reel-review-loop`.

Recipes (contrast, range guard, levels identity, scope proof): `references/qc-recipes.md`.
