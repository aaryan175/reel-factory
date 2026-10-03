# Reference Forensics Playbook

Commands, a detector recipe, the ledger schema and condensed lessons. Each lesson is **rule / why / check**.

## A. Onset detector (deterministic, numpy + scipy)

```python
import json, subprocess, sys
from fractions import Fraction
import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import find_peaks

src, rate = sys.argv[1], Fraction(sys.argv[2])   # e.g. REF.mp4 30000/1001
SR, N_FFT, HOP = 48000, 2048, 256
pcm = np.frombuffer(subprocess.run(
    ["ffmpeg", "-v", "error", "-i", src, "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
    capture_output=True, check=True).stdout, dtype=np.float32)
frames = np.lib.stride_tricks.sliding_window_view(pcm, N_FFT)[::HOP] * np.hanning(N_FFT)
mag = np.log1p(np.abs(np.fft.rfft(frames, axis=1)))
freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
flux = np.maximum(np.diff(mag, axis=0), 0)             # positive log-spectral flux
fps_env = SR / HOP
out = {}
for name, (lo, hi) in {"low": (20, 200), "mid": (200, 2000), "high": (2000, 12000), "full": (20, 16000)}.items():
    band = flux[:, (freqs >= lo) & (freqs < hi)].sum(axis=1)
    norm = band - median_filter(band, size=int(0.5 * fps_env))   # local-median normalisation
    peaks, _ = find_peaks(norm, distance=int(0.10 * fps_env), prominence=np.percentile(norm, 90) * 0.5)
    t = (peaks + 1) * HOP / SR + N_FFT / (2 * SR)
    out[name] = [{"t": round(float(s), 4), "frame": round(s * rate)} for s in t]
json.dump(out, sys.stdout, indent=1)
```

- Tune `distance` and `prominence` per track and record the values used; they are part of the evidence.
- `librosa.onset.onset_detect` is an acceptable alternative; record its version and parameters.
- Map to frames with the exact rational rate. Keep raw seconds too.

## B. Other commands

```bash
# Per-frame luma/sat stats (one line set per frame)
ffmpeg -v error -i REF.mp4 -vf "signalstats,metadata=print:file=signalstats.txt" -f null -
# Adjacent-frame difference as a second cut signal (frame n vs n-1)
ffmpeg -v error -i REF.mp4 -vf "tblend=all_mode=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=diff.txt" -f null -
# Loudness / peaks
ffmpeg -v error -i REF.mp4 -af ebur128=peak=true -f null - 2> loudness.txt
# Audio identity: packets + data hashes
ffprobe -v error -select_streams a:0 -show_packets -show_data_hash sha256 -of csv REF.mp4 > audio_packets.csv
# Native-resolution PRE/POST pair for a candidate boundary after frame 41
ffmpeg -v error -i REF.mp4 -vf "select='between(n,41,42)'" -fps_mode passthrough pair_%d.png
# Text OCR lead (Tesseract)
tesseract frames/00123.png - --psm 6
```

On macOS, Vision text recognition (accurate mode, language correction off) over every decoded frame finds persistent caption states; convert its bottom-left normalised boxes to top-left pixels. Treat it as a lead, then confirm by eye.

## C. Ledger schema (minimal)

```json
{
  "source": {"sha256": "...", "r_frame_rate": "30/1", "nb_read_frames": 216, "w": 1080, "h": 1920, "sar": "1:1"},
  "picture_blocks": [{"id": "p001", "start": 0, "end": 16, "role": "hook", "verb": "turns", "boundary_after": "hard_cut"}],
  "hard_cuts_after": [15, 29],
  "picture_boundaries_after": [15, 22, 29],
  "captions": [{"id": "c001", "start": 3, "end": 18, "text": "exact raster string", "lifecycle": "blur_in_3f_hold_hard_off", "role": "display"}],
  "effects": [{"frame": 22, "class": "real_frame_recall", "source_frame": 9, "layer": "below_captions"}],
  "onsets": {"low": [], "full": []},
  "endpoint": {"class": "live_motion_cutoff", "audio_end_minus_video_end_ms": 12},
  "evidence_tier": "gap_free_frames+decoded_audio"
}
```

Assert in code: blocks are contiguous, non-overlapping, and cover `0..N-1`; `hard_cuts_after` is a subset of `picture_boundaries_after`.

## D. Lessons

**L1. Lock decoded identity, not filenames.**
Rule: hash bytes, packets and decoded frames. Why: a re-encode can keep the same name and duration but shift every frame. Check: `framemd5` of the working copy equals the lock.

**L2. Never round the frame rate.**
Rule: store `r_frame_rate` as a fraction and do all frame/time maths with it. Why: `2997/125` vs `24` drifts a full frame within seconds. Check: `round(t * rate)` round-trips every cut frame.

**L3. Boundaries are classified by pixels, not scores.**
Rule: inspect PRE/POST at native resolution for every candidate and every suspected miss. Why: dark-to-dark cuts score low, whip pans score high. Check: every entry in `hard_cuts_after` has a pair image on disk.

**L4. Picture states outnumber shots.**
Rule: log held smears, pose jumps, reframes and insert pulses as boundaries. Why: collapsing them makes identical audio feel mistimed. Check: `len(picture_boundaries_after) >= len(hard_cuts_after)`, and the difference is explained.

**L5. Visible text is its own track.**
Rule: extract on-screen text independently of the transcript. Why: references show uncaptioned phrases, partial words, one-frame blinks, or a different hero word than the one spoken. Check: caption ledger built from frames, not from ASR.

**L6. Sampled boards cannot certify boundaries.**
Rule: an 8 fps proxy samples every 125 ms (3.75 frames at 30 fps); emit a search window, not a boundary. Why: the true cut can sit anywhere in that window. Check: every locked boundary cites a native frame pair.

**L7. Loops need hash proof.**
Rule: call an ending `exact_loop` only if the decoded last and first frames hash-match. Why: "looks like it loops" is usually a bookend. Check: compare the two `framemd5` lines.

**L8. Effects are operators, not adjectives.**
Rule: write "frame 22 = real recall of frame 9, below captions" rather than "glitch". Why: a rebuild needs the operator, direction and lifecycle. Check: each effect entry names class, source and layer.

**L9. Persist early.**
Rule: create the report at source lock and checkpoint every few steps; finish the minimum artifact before enriching it. Why: a correct but unwritten analysis is an incomplete task. Check: the report path exists before any heavy analysis starts.

**L10. Say what you did not do.**
Rule: label the evidence tier reached. Why: frame-complete review cannot support rhythm or "feel" claims. Check: the report states whether a normal-speed AV watch happened and by whom.
