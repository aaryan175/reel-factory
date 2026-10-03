#!/usr/bin/env python3
"""onetoone.devicesheet — reference | ours contact sheet over a caption's ENTRY WINDOW.

One row per frame across the entry, reference crop on the left and ours on the right, same
frame number, same bbox. The beds are different footage, so this is not a picture comparison —
it is the only way to judge the DEVICE: does the ink appear on the same frame, is it as blurred
on the way in, does it land sharp on the same frame, does it grow or shrink the same way.

    python3 -m onetoone.devicesheet <brain> <refframes> <ourframes> <captions> <outdir> [ids...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List, Mapping, Sequence, Tuple

from PIL import Image, ImageDraw

DEFAULT_IDS = ("c01", "c02", "c03", "c04a", "c04b", "c06")


def _crop(img: Image.Image, box: Sequence[int], pad: int) -> Image.Image:
    x0 = max(0, box[0] - pad); y0 = max(0, box[1] - pad)
    x1 = min(img.width, box[2] + pad); y1 = min(img.height, box[3] + pad)
    return img.crop((x0, y0, x1, y1))


def _union(parts: Sequence[Mapping[str, Any]]) -> List[int]:
    boxes = [p.get("bbox_settled") or p.get("bbox_settled_approx") for p in parts]
    boxes = [[int(v) for v in b] for b in boxes if b]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def sheet(brain: Path, refframes: Path, ourframes: Path, captions: Path, out: Path,
          state_id: str, *, pad: int = 70, scale: float = 0.42) -> Path:
    states = {s["id"]: s for s in json.loads((brain / "captions.plaintext.json").read_text())["states"]}
    devs = json.loads((brain / "caption_devices.json").read_text())["states"]
    st = states[state_id]
    box = _union(st["parts"])
    d = devs.get(state_id, {})
    firsts = [v["first_frame"] for v in d.values() if v.get("first_frame") is not None]
    sharps = [v["sharp_frame"] for v in d.values() if v.get("sharp_frame") is not None]
    lo = max(int(st["in"]), (min(firsts) if firsts else int(st["in"])) - 1)
    hi = min(int(st["out"]), (max(sharps) if sharps else int(st["in"]) + 8) + 2)
    if hi - lo > 15:                       # keep the sheet readable: sample the tail
        frames = list(range(lo, lo + 11)) + list(range(lo + 11, hi + 1, 3))
    else:
        frames = list(range(lo, hi + 1))
    rows = []
    for n in frames:
        ref = Image.open(refframes / f"ref-{n:03d}.png").convert("RGB")
        bed = Image.open(ourframes / f"our-{n:03d}.png").convert("RGBA")
        cap = Image.open(captions / f"caption-{n:03d}.png").convert("RGBA")
        bed.alpha_composite(cap)
        a = _crop(ref, box, pad)
        b = _crop(bed.convert("RGB"), box, pad)
        rows.append((n, a, b))
    w = int(rows[0][1].width * scale); h = int(rows[0][1].height * scale)
    lab = 18
    sheet_img = Image.new("RGB", (w * 2 + 12, (h + lab) * len(rows) + 22), (12, 12, 12))
    dr = ImageDraw.Draw(sheet_img)
    label = "%s  %s   REFERENCE | OURS   device: %s" % (
        state_id, " + ".join(str(p.get("text")) for p in st["parts"]),
        "; ".join("%s first=%s sharp=%s blur≤%.1f" % (t, v.get("first_frame"), v.get("sharp_frame"),
                                                      max(v.get("max_sigma_x", 0), v.get("max_sigma_y", 0)))
                  for t, v in d.items() if v.get("first_frame") is not None))
    dr.text((6, 5), label, fill=(255, 210, 120))
    for i, (n, a, b) in enumerate(rows):
        y = 22 + i * (h + lab)
        dr.text((6, y + 2), f"n={n}", fill=(120, 230, 255))
        sheet_img.paste(a.resize((w, h)), (0, y + lab))
        sheet_img.paste(b.resize((w, h)), (w + 12, y + lab))
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{state_id}.png"
    sheet_img.save(p)
    return p


if __name__ == "__main__":
    brain, refframes, ourframes, captions, outdir = (Path(a) for a in sys.argv[1:6])
    ids = sys.argv[6:] or list(DEFAULT_IDS)
    for sid in ids:
        print(sheet(brain, refframes, ourframes, captions, outdir, sid))
