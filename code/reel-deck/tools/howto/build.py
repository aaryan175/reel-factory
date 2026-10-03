#!/usr/bin/env python3
"""Turn record.py's shots + narration lines into the narrated walkthrough mp4.

Narration is macOS `say` (any installed voice; set VOICE). Each scene is its screenshot on a black 1080p canvas with
the spoken line written under it, held for as long as the narration takes. ffmpeg runs through the
kit's one-at-a-time wrapper.

    python3 tools/howto/build.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import settings  # noqa: E402

sys.path.insert(0, str(settings.TOOLS_DIR))
from onetoone.ffx import run  # noqa: E402

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

OUT = settings.WORKBENCH / "reel-deck-howto" / "walkthrough"
WORK = OUT / "work"
W, H = 1920, 1080
FONT = "/System/Library/Fonts/Supplemental/Helvetica.ttc"
VOICE, RATE = "Samantha", 172


def narrate(text: str, stem: str) -> tuple[Path, float]:
    aiff, wav = WORK / f"{stem}.aiff", WORK / f"{stem}.wav"
    subprocess.run(["say", "-v", VOICE, "-r", str(RATE), "-o", str(aiff), text], check=True)
    run(["-y", "-loglevel", "error", "-i", str(aiff), "-af", "apad=pad_dur=0.7",
         "-ar", "48000", "-ac", "2", str(wav)])
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(wav)], capture_output=True, text=True)
    return wav, float(out.stdout.strip())


def card(shot: Path, text: str, stem: str) -> Path:
    canvas = Image.new("RGB", (W, H), (10, 10, 11))
    im = Image.open(shot).convert("RGB")
    box_h = 880
    im.thumbnail((W - 80, box_h), Image.LANCZOS)
    canvas.paste(im, ((W - im.width) // 2, 18))
    d = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(FONT, 34)
    lines = textwrap.wrap(text, width=96)[:3]
    y = H - 30 - 42 * len(lines)
    for line in lines:
        d.text((W // 2, y), line, font=font, fill=(236, 233, 226), anchor="ma")
        y += 42
    path = WORK / f"{stem}.png"
    canvas.save(path)
    return path


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    scenes = json.loads((OUT / "scenes.json").read_text())
    clips: list[Path] = []
    for i, sc in enumerate(scenes, 1):
        stem = f"s{i:02d}"
        wav, dur = narrate(sc["say"], stem)
        img = card(Path(sc["shot"]), sc["say"], stem)
        clip = WORK / f"{stem}.mp4"
        run(["-y", "-loglevel", "error", "-loop", "1", "-i", str(img), "-i", str(wav),
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-tune", "stillimage",
             "-pix_fmt", "yuv420p", "-r", "24", "-c:a", "aac", "-b:a", "128k",
             "-t", f"{dur:.3f}", "-shortest", str(clip)])
        clips.append(clip)
        print(f"  {stem}  {dur:5.1f}s  {sc['say'][:60]}…")

    lst = WORK / "concat.txt"
    lst.write_text("".join(f"file '{c}'\n" for c in clips))
    final = OUT / "reel-deck-walkthrough.mp4"
    run(["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(final)])
    total = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", str(final)],
                           capture_output=True, text=True).stdout.strip()
    print(f"\n{final}  ({float(total):.1f}s, {final.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
