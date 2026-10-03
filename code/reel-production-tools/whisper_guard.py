#!/usr/bin/env python3
"""whisper_guard.py — THE ONLY legal way to run Whisper in the reel factory.

Why this exists: several concurrent Whisper processes launched by parallel lanes can exhaust memory
on a small machine and take it down. This wrapper makes that impossible:

  * ONE transcription on the whole machine at a time (exclusive flock on a global lock file;
    a second caller BLOCKS until the first finishes — it never runs alongside).
  * model is forced to `medium` (or smaller). `large*` is refused outright.
  * refuses to start if free memory < 6 GB or if any other whisper/demucs process exists.

Usage (from any lane):
    python3 tools/whisper_guard.py <audio> --out <json> [--lang <code>] [--model medium]
Output: openai-whisper result JSON (segments + word timestamps) written to --out.
"""
import argparse, fcntl, json, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rf_paths  # noqa: E402

LOCK = str(rf_paths.RECEIPTS / ".whisper.lock")
ALLOWED = {"tiny", "base", "small", "medium"}
MIN_FREE_BYTES = 6 * 1024**3


def free_mem_bytes():
    """Use memory_pressure's system-wide free percentage (counts reclaimable pages) x physical RAM."""
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    pct = None
    for line in out.splitlines():
        if "free percentage" in line:
            pct = int(line.split(":")[1].strip().rstrip("%"))
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip())
    if pct is None:
        return 0
    return total * pct // 100


def other_asr_running():
    """Real ASR processes only (not shells whose command text merely mentions whisper)."""
    out = subprocess.run(["ps", "-eo", "pid,ppid,args", "-ww"], capture_output=True, text=True).stdout
    me = os.getpid(); hits = []
    for l in out.splitlines()[1:]:
        parts = l.split(None, 2)
        if len(parts) < 3: continue
        pid, ppid, args = int(parts[0]), int(parts[1]), parts[2]
        if pid == me or "whisper_guard" in args: continue
        if args.startswith(("/bin/zsh", "zsh", "/bin/bash", "bash", "sh ")): continue
        a = args.lower()
        if ("demucs" in a) or ("whisper" in a and ("python" in a or a.split()[0].endswith("whisper"))):
            hits.append(l)
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--out", required=True)
    ap.add_argument("--lang", default=None)
    ap.add_argument("--model", default="medium")
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--extra", default="{}", help="JSON of extra whisper.transcribe kwargs (e.g. no_speech_threshold)")
    a = ap.parse_args()

    if a.model not in ALLOWED:
        sys.exit(f"REFUSED: model '{a.model}' not allowed (large* models are refused: memory). Use medium.")

    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "a+") as lf:
        t0 = time.time()
        fcntl.flock(lf, fcntl.LOCK_EX)   # blocks until the machine is free
        waited = time.time() - t0
        if waited > 1:
            print(f"[guard] waited {waited:.0f}s for the machine-wide transcription slot", file=sys.stderr)
        others = other_asr_running()
        if others:
            sys.exit("REFUSED: another ASR process is running outside the guard:\n" + "\n".join(others))
        fm = free_mem_bytes()
        if fm < MIN_FREE_BYTES:
            sys.exit(f"REFUSED: only {fm/1024**3:.1f} GB free memory (< 6 GB).")
        lf.seek(0); lf.truncate(); lf.write(f"pid={os.getpid()} model={a.model} audio={a.audio} t={time.time()}\n"); lf.flush()

        import whisper  # imported inside the lock so the model never loads twice
        model = whisper.load_model(a.model)
        kw = dict(beam_size=a.beam, word_timestamps=True, fp16=False)
        if a.lang:
            kw["language"] = a.lang
        kw.update(json.loads(a.extra))
        res = model.transcribe(a.audio, **kw)
        with open(a.out, "w") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print(f"[guard] done -> {a.out} ({len(res.get('segments', []))} segments)")


if __name__ == "__main__":
    main()
