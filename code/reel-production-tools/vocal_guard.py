#!/usr/bin/env python3
"""vocal_guard.py — the ONLY legal way to run demucs vocal isolation in the reel factory.

Shares the machine-wide ASR/ML lock with whisper_guard.py (one heavy ML process on the box at a
time). htdemucs only, segment capped at 40 s, refuses under 6 GB free.
Runs demucs with $DEMUCS_PYTHON (an interpreter with torch + demucs installed; default: this interpreter).

Usage: vocal_guard.py <audio.wav|m4a> --out-dir <dir> [--start S --dur D]
Writes <dir>/vocals.wav and <dir>/no_vocals.wav (44.1 kHz stereo).
"""
import argparse, fcntl, os, shutil, subprocess, sys, time, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("wg", os.path.join(HERE, "whisper_guard.py"))
wg = importlib.util.module_from_spec(spec); spec.loader.exec_module(wg)
VENV_PY = os.path.expanduser(os.environ.get("DEMUCS_PYTHON") or sys.executable)
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE") or shutil.which("ffprobe") or "ffprobe"
MAX_DUR = 40.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio"); ap.add_argument("--out-dir", required=True)
    ap.add_argument("--start", type=float, default=None); ap.add_argument("--dur", type=float, default=None)
    a = ap.parse_args()
    if not os.path.exists(VENV_PY):
        sys.exit("REFUSED: demucs interpreter missing at " + VENV_PY)
    os.makedirs(a.out_dir, exist_ok=True)
    seg = os.path.join(a.out_dir, "segment.wav")
    cmd = [FFMPEG, "-v", "error", "-y"]
    if a.start is not None: cmd += ["-ss", str(a.start)]
    cmd += ["-i", a.audio]
    if a.dur is not None:
        if a.dur > MAX_DUR: sys.exit(f"REFUSED: segment {a.dur}s > {MAX_DUR}s cap")
        cmd += ["-t", str(a.dur)]
    cmd += ["-ac", "2", "-ar", "44100", seg]
    subprocess.run(cmd, check=True)
    d = float(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", seg], capture_output=True, text=True).stdout.strip())
    if d > MAX_DUR + 0.5: sys.exit(f"REFUSED: segment {d:.1f}s > {MAX_DUR}s cap (pass --start/--dur)")

    with open(wg.LOCK, "a+") as lf:
        t0 = time.time(); fcntl.flock(lf, fcntl.LOCK_EX)
        if time.time() - t0 > 1: print(f"[guard] waited {time.time()-t0:.0f}s for the machine-wide ML slot", file=sys.stderr)
        if wg.other_asr_running(): sys.exit("REFUSED: another ASR/ML process is running outside the guard")
        fm = wg.free_mem_bytes()
        if fm < wg.MIN_FREE_BYTES: sys.exit(f"REFUSED: only {fm/1024**3:.1f} GB free memory")
        lf.seek(0); lf.truncate(); lf.write(f"pid={os.getpid()} model=htdemucs audio={seg} t={time.time()}\n"); lf.flush()
        r = subprocess.run([VENV_PY, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals", "-o", a.out_dir, seg], capture_output=True, text=True)
        if r.returncode != 0: sys.exit("demucs failed:\n" + r.stderr[-2000:])
    stem = os.path.join(a.out_dir, "htdemucs", "segment")
    for n in ("vocals.wav", "no_vocals.wav"):
        os.replace(os.path.join(stem, n), os.path.join(a.out_dir, n))
    print(f"[guard] done -> {a.out_dir}/vocals.wav ({d:.2f}s)")


if __name__ == "__main__":
    main()
