#!/usr/bin/env python3
"""onetoone.lumacheck — per-shot luma of a delivered cut vs the reference, from the bytes.

Two views, both BT.709 full-range RGB (the deliverable's tagging):
  BED   — caption rectangles cut out of BOTH sides (the picture itself; what the grade controls)
  WHOLE — every pixel

Per shot: mean difference, worst frame, and the worst frame-to-frame step ours takes that the
reference doesn't. Writes <work>/luma_per_frame.json and prints a table the ledger can quote.

    python3 -m onetoone.lumacheck <brain> <reference.mp4> <ours.mp4> <work_dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from onetoone.render import _frame_means, caption_masks


def per_shot(ours: List[float], ref: List[float], shots: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for s in shots:
        a, b = int(s["in"]), int(s["out"])
        d = [ours[i] - ref[i] for i in range(a, b + 1)]
        steps = [(abs((ours[i + 1] - ours[i]) - (ref[i + 1] - ref[i])), i + 1) for i in range(a, b)]
        wi = max(range(len(d)), key=lambda k: abs(d[k]))
        st = max(steps) if steps else (0.0, a)
        out[s["slot"]] = {"mean": round(sum(d) / len(d), 2), "worst": round(d[wi], 2), "worst_f": a + wi,
                          "step": round(st[0], 2), "step_f": st[1]}
    return out


def check(brain: Path, reference: Path, ours: Path, work: Path, frames: Optional[int] = None) -> Dict[str, Any]:
    shots = json.loads((brain / "cutgrid.json").read_text())["shots"]
    n = frames or (int(shots[-1]["out"]) + 1)
    masks = caption_masks(brain, range(0, n))
    scratch = work / "lumacheck"; scratch.mkdir(parents=True, exist_ok=True)
    res: Dict[str, Any] = {"weights": "BT.709 full-range RGB", "frames": n}
    for tag, mk in (("bed", masks), ("whole", None)):
        o = _frame_means(ours, scratch / f"{tag}_ours", start=0, count=n, masks=mk)
        r = _frame_means(reference, scratch / f"{tag}_ref", start=0, count=n, masks=mk)
        res[tag] = {"ours": o, "ref": r, "per_shot": per_shot(o, r, shots)}
    (work / "luma_per_frame.json").write_text(json.dumps(res))
    return res


def table(res: Dict[str, Any]) -> str:
    lines = ["slot   BED mean  worst (frame)   step (frame)  |  WHOLE mean  worst (frame)   step (frame)"]
    for k, b in res["bed"]["per_shot"].items():
        w = res["whole"]["per_shot"][k]
        lines.append(f"{k}   {b['mean']:+6.2f}  {b['worst']:+6.2f} (f{b['worst_f']:<3})  {b['step']:5.2f} (f{b['step_f']:<3})"
                     f"  |  {w['mean']:+6.2f}  {w['worst']:+6.2f} (f{w['worst_f']:<3})  {w['step']:5.2f} (f{w['step_f']:<3})")
    return "\n".join(lines)


if __name__ == "__main__":
    brain, ref, ours, work = (Path(a) for a in sys.argv[1:5])
    print(table(check(brain, ref, ours, work)))
