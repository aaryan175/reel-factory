#!/usr/bin/env python3
"""beatmap.py — dependency-light onset/tempo/downbeat mapper for the reel factory.

No librosa. Uses ffmpeg (decode) + numpy (STFT, spectral flux, tempo autocorrelation).

Usage:
  beatmap.py <media> [--start S] [--dur D] [--json out.json]

Outputs: BPM estimate (with half/double disambiguation), beat grid phase (first
downbeat offset), onset times, and per-bar downbeats over the analysed window.
"""
import argparse, json, subprocess, sys
import numpy as np

SR = 22050
HOP = 256          # ~11.6 ms
NFFT = 1024


def decode(path, start=None, dur=None):
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", path]
    if dur is not None:
        cmd += ["-t", str(dur)]
    cmd += ["-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def spectral_flux(x):
    win = np.hanning(NFFT)
    n = 1 + (len(x) - NFFT) // HOP
    if n < 4:
        raise SystemExit("clip too short")
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n)[:, None]
    frames = x[idx] * win
    S = np.abs(np.fft.rfft(frames, axis=1))
    S = np.log1p(1000.0 * S)
    d = np.diff(S, axis=0)
    d[d < 0] = 0.0
    flux = d.sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    # normalise / remove slow drift
    k = 31
    pad = np.pad(flux, (k // 2, k // 2), mode="edge")
    loc = np.convolve(pad, np.ones(k) / k, mode="valid")[: len(flux)]
    env = flux - loc
    env[env < 0] = 0.0
    if env.max() > 0:
        env = env / env.max()
    return env


def pick_onsets(env, thresh=0.12, min_gap_s=0.055):
    min_gap = max(1, int(min_gap_s * SR / HOP))
    peaks = []
    for i in range(1, len(env) - 1):
        if env[i] >= env[i - 1] and env[i] > env[i + 1] and env[i] > thresh:
            if not peaks or i - peaks[-1] >= min_gap:
                peaks.append(i)
            elif env[i] > env[peaks[-1]]:
                peaks[-1] = i
    return np.array(peaks) * HOP / SR


def tempo(env, bpm_lo=55.0, bpm_hi=200.0):
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    ac[0] = 0
    fps = SR / HOP
    lags = np.arange(len(ac))
    with np.errstate(divide="ignore"):
        bpms = 60.0 * fps / np.maximum(lags, 1e-9)
    mask = (bpms >= bpm_lo) & (bpms <= bpm_hi)
    cand = []
    for lag in np.where(mask)[0]:
        if 1 <= lag < len(ac) - 1 and ac[lag] >= ac[lag - 1] and ac[lag] >= ac[lag + 1]:
            cand.append((ac[lag], 60.0 * fps / lag, lag))
    cand.sort(reverse=True)
    return cand[:8]


def grid_phase(onsets, bpm, window_end):
    """Best phase for a beat grid at bpm: maximise onset energy landing on grid."""
    period = 60.0 / bpm
    best = (None, -1)
    for ph in np.arange(0, period, period / 200.0):
        grid = np.arange(ph, window_end, period)
        if len(grid) < 2:
            continue
        d = np.abs(onsets[:, None] - grid[None, :]).min(axis=1) if len(onsets) else np.array([9])
        score = float((d < period * 0.10).sum())
        if score > best[1]:
            best = (float(ph), score)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--dur", type=float, default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    x = decode(a.media, a.start, a.dur)
    if len(x) == 0:
        raise SystemExit("no audio decoded")
    dur = len(x) / SR
    env = spectral_flux(x)
    onsets = pick_onsets(env)
    cands = tempo(env)

    print(f"media       : {a.media}")
    print(f"window      : start={a.start} dur={dur:.3f}s")
    print(f"onsets      : {len(onsets)}  ({len(onsets)/dur:.2f}/s)")
    print("tempo candidates (autocorr strength, BPM):")
    for s, b, lag in cands:
        print(f"   {s:10.1f}   {b:7.2f} BPM   (lag {lag})")

    out = {"media": a.media, "start": a.start, "dur": dur,
           "onsets": [round(float(t), 4) for t in onsets],
           "tempo_candidates": [{"strength": float(s), "bpm": round(float(b), 3)} for s, b, _ in cands]}

    if cands:
        # prefer the strongest candidate in 70-180; report its half/double too
        bpm = cands[0][1]
        for fam in sorted({bpm, bpm * 2, bpm / 2, bpm * 4, bpm / 4}):
            if 60 <= fam <= 200:
                ph, sc = grid_phase(onsets, fam, dur)
                hits = sc / max(1, len(np.arange(ph, dur, 60.0 / fam)))
                print(f"grid fit @ {fam:7.2f} BPM : phase={ph:.4f}s  on-grid onsets={int(sc)}  ({hits*100:.0f}% of beats)")
                out.setdefault("grid_fits", []).append(
                    {"bpm": round(float(fam), 3), "phase_s": round(float(ph), 4),
                     "on_grid_onsets": int(sc), "beat_hit_pct": round(float(hits * 100), 1)})
        ph, _ = grid_phase(onsets, bpm, dur)
        out["primary_bpm"] = round(float(bpm), 3)
        out["primary_phase_s"] = round(float(ph), 4)
        period = 60.0 / bpm
        downbeats = [round(float(t), 4) for t in np.arange(ph, dur, period * 4)]
        out["downbeats_4_4"] = downbeats
        print(f"downbeats(4/4) : {downbeats[:12]}")

    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=2)
        print(f"wrote {a.json}")


if __name__ == "__main__":
    main()
