#!/usr/bin/env python3
"""onetoone.ffx — run ffmpeg/ffprobe under the machine-wide ONE-FFMPEG lock.

Law: one ffmpeg at a time on this machine (parallel encodes have wedged it). Every agent
and every tool in the 1:1 kit must run ffmpeg through this wrapper, never bare.

    python3 -m onetoone.ffx -- -y -i in.mp4 ... out.png          # ffmpeg
    python3 -m onetoone.ffx --probe -- -v error ... in.mp4       # ffprobe (no lock needed, but same path)

Library:  from onetoone.ffx import run;  run(["-y", "-i", src, ...])
Waits (blocking) for the lock; prints nothing extra; returns ffmpeg's exit code.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Sequence

FFMPEG = os.environ.get("REEL_FACTORY_FFMPEG") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = os.environ.get("REEL_FACTORY_FFPROBE") or shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
LOCK = os.path.join(tempfile.gettempdir(), "reel-one-ffmpeg.lock")


def run(args: Sequence[str], *, probe: bool = False, capture: bool = False, timeout: float | None = None) -> subprocess.CompletedProcess:
    cmd = [FFPROBE if probe else FFMPEG, *args]
    if probe:
        return subprocess.run(cmd, capture_output=capture, text=capture, timeout=timeout)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return subprocess.run(cmd, capture_output=capture, text=capture, timeout=timeout)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == "__main__":
    argv = sys.argv[1:]
    probe = False
    if argv and argv[0] == "--probe":
        probe = True; argv = argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    sys.exit(run(argv, probe=probe).returncode)
