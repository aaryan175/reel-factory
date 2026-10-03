#!/usr/bin/env python3
"""onetoone.refstrip — eyeball strip of the reference's own frames over a caption's entry window.

Crops one part's measured bbox (padded) out of every reference frame in a range and tiles them
with frame numbers, so the entry device can be READ off the reference before anything is fitted.

    python3 -m onetoone.refstrip <refframes> <brain> <state_id> <part_index> <n0> <n1> <out.png>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw


def strip(refframes: Path, box: Sequence[int], n0: int, n1: int, out: Path,
          *, pad: int = 60, cols: int = 6, scale: float = 0.5, label: str = "") -> Path:
    x0, y0, x1, y1 = [int(v) for v in box]
    x0 = max(0, x0 - pad); y0 = max(0, y0 - pad); x1 += pad; y1 += pad
    crops = []
    for n in range(n0, n1 + 1):
        im = Image.open(refframes / f"ref-{n:03d}.png").convert("RGB")
        c = im.crop((x0, y0, min(x1, im.width), min(y1, im.height)))
        c = c.resize((max(1, int(c.width * scale)), max(1, int(c.height * scale))))
        crops.append((n, c))
    w, h = crops[0][1].size
    lab = 16
    rows = (len(crops) + cols - 1) // cols
    sheet = Image.new("RGB", (w * cols, (h + lab) * rows + 18), (12, 12, 12))
    d = ImageDraw.Draw(sheet)
    d.text((6, 4), label or f"frames {n0}-{n1}", fill=(255, 210, 120))
    for i, (n, c) in enumerate(crops):
        x = (i % cols) * w; y = 18 + (i // cols) * (h + lab)
        sheet.paste(c, (x, y + lab))
        d.text((x + 4, y + 2), f"n={n}", fill=(120, 230, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


if __name__ == "__main__":
    refframes, brain, sid, pidx, n0, n1, out = sys.argv[1:8]
    states = {s["id"]: s for s in json.loads((Path(brain) / "captions.plaintext.json").read_text())["states"]}
    part = states[sid]["parts"][int(pidx)]
    box = part.get("bbox_settled") or part.get("bbox_settled_approx")
    print(strip(Path(refframes), box, int(n0), int(n1), Path(out),
                label=f"{sid} {part.get('part')} {part.get('text')} n={n0}-{n1}"))
