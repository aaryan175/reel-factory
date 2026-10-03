#!/usr/bin/env python3
"""onetoone.refpeople — where does the REFERENCE show a person?

A cast once put an empty bed under a shot where the reference shows a person, because nothing in the
brain said "a person is on screen here". This measures it from the reference's own frames with the
macOS Vision face detector (the same detector that pre-sorted the footage library for the Deck grader):

    cd reel-production-tools && python3 -m onetoone.refpeople <row dir> [--ref ref/ref24.mp4] [--step 4]

writes <row dir>/brain/refpeople.json:
    {"ref": "...", "fps": 23.976, "frames": 447, "step": 4,
     "faces": {"0": [0.21], "4": [], ...}}            # sampled frame -> largest-face heights (fraction of frame height)

onetoone.identity.refpeople_rules reads it at preflight: a cast slot whose reference window has a face on
at least a third of its sampled frames (largest face >= 6% of frame height) is a person shot and must be
an identity slot cast from HERO/settled clips. No render, no registry write.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

from onetoone.ffx import FFMPEG, FFPROBE  # noqa: E402
MIN_FACE_H = 0.06       # same thresholds as the library pre-sort (the footage intake person scan)
MIN_FRACTION = 1 / 3


def _faces(png: str) -> List[float]:
    import Vision
    from Foundation import NSURL
    h = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(png), None)
    r = Vision.VNDetectFaceRectanglesRequest.alloc().init()
    h.performRequests_error_([r], None)
    return [float(f.boundingBox().size.height) for f in (r.results() or [])]


def probe(ref: Path) -> dict:
    p = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate,nb_frames",
                        "-print_format", "json", str(ref)], capture_output=True, text=True, check=True)
    s = json.loads(p.stdout)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return {"fps": float(num) / float(den), "frames": int(s.get("nb_frames") or 0)}


def scan(ref: Path, step: int = 4, out: Path | None = None) -> dict:
    info = probe(ref)
    faces: Dict[str, List[float]] = {}
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(ref), "-vf", f"select=not(mod(n\\,{step})),scale=640:-2",
                        "-vsync", "0", "-start_number", "0", f"{td}/f%05d.png"], check=True)
        pngs = sorted(Path(td).glob("f*.png"))
        for i, png in enumerate(pngs):
            faces[str(i * step)] = [round(h, 3) for h in _faces(str(png))]
        if not info["frames"]:
            info["frames"] = len(pngs) * step
    data = {"ref": str(ref), "fps": info["fps"], "frames": info["frames"], "step": step,
            "min_face_h": MIN_FACE_H, "min_fraction": MIN_FRACTION, "faces": faces,
            "method": "macOS Vision VNDetectFaceRectanglesRequest on every <step>th frame at 640 px wide"}
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=0))
    return data


def window_verdict(data: dict, f0: int, f1: int) -> dict:
    """Is there a person in reference frames [f0, f1]? Face on >= min_fraction of the sampled frames,
    largest face >= min_face_h of the frame height. Windows shorter than one step borrow the nearest sample."""
    step = int(data.get("step", 4))
    faces = data.get("faces", {})
    minh = float(data.get("min_face_h", MIN_FACE_H)); frac = float(data.get("min_fraction", MIN_FRACTION))
    keys = [k for k in range(f0 - f0 % step, f1 + 1, step) if str(k) in faces]
    if not keys:
        near = min((int(k) for k in faces), key=lambda k: abs(k - (f0 + f1) // 2), default=None)
        keys = [near] if near is not None else []
    with_face = sum(1 for k in keys if any(h >= minh for h in faces[str(k)]))
    n = len(keys)
    return {"sampled": n, "with_face": with_face, "person": bool(n and with_face >= max(1, int(round(n * frac))))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="onetoone.refpeople", description=__doc__.split("\n\n")[0])
    ap.add_argument("row_dir")
    ap.add_argument("--ref", default=None, help="reference video (default: ref/ref24.mp4, else ref/reference-source.mp4, else ref/ref.mp4)")
    ap.add_argument("--step", type=int, default=4)
    a = ap.parse_args(argv)
    row = Path(os.path.expanduser(a.row_dir)).resolve()
    ref = Path(a.ref) if a.ref else next((row / "ref" / n for n in ("ref24.mp4", "reference-source.mp4", "ref.mp4") if (row / "ref" / n).exists()), None)
    if not ref or not ref.exists():
        print(json.dumps({"ok": False, "why": "no reference video found under ref/"})); return 2
    out = row / "brain" / "refpeople.json"
    data = scan(ref, a.step, out)
    n = len(data["faces"]); wf = sum(1 for v in data["faces"].values() if any(h >= MIN_FACE_H for h in v))
    print(json.dumps({"ok": True, "out": str(out), "sampled": n, "with_face": wf, "frames": data["frames"], "fps": data["fps"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
