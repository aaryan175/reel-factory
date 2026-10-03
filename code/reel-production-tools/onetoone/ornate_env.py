#!/usr/bin/env python3
"""onetoone.ornate_env — measure the reference's ornate INK ENVELOPE, so the script word sits
where the reference's does (an eye check found one script word rendered 26px low with its
lowercase landing on the plain line, and another filling its box where the reference's sits high).

Box-fitting is wrong for a script face: the study box includes the swash tails, and a different
face (Pinyon vs the reference's) puts a different share of its ink in tails. What the eye reads
is the body. So, in the SAME mask for both sides — the ornate box minus the plain line's box —
take the 5th/95th percentile rows of ink, and typeset ours to match that envelope (size by
span, position by centre). Stored on the part as `ref_ink_env` and `env_mask`.

    python3 -m onetoone.ornate_env <brain> <look_dir>   # look_dir holds {sid}_{frame}_ref.png
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from onetoone.captions_typeset import _PART_FACE, _part_box

P_LO, P_HI = 0.05, 0.95
INK_TOL = 70.0


def env_mask(ornate_box: Sequence[int], plain_box: Optional[Sequence[int]], size: Tuple[int, int]) -> np.ndarray:
    m = np.zeros((size[1], size[0]), dtype=bool)
    x0, y0, x1, y1 = ornate_box
    m[y0:y1, x0:x1] = True
    if plain_box:
        px0, py0, px1, py1 = plain_box
        m[py0:py1, px0:px1] = False
    return m


def row_envelope(weights: np.ndarray) -> Optional[Tuple[int, int]]:
    """(y05, y95) of a per-row ink weight profile."""
    tot = float(weights.sum())
    if tot <= 0:
        return None
    c = np.cumsum(weights) / tot
    return int(np.searchsorted(c, P_LO)), int(np.searchsorted(c, P_HI))


def ref_ink_rows(frame: Image.Image, ink_rgb: Sequence[int], mask: np.ndarray) -> np.ndarray:
    a = np.asarray(frame.convert("RGB"), dtype=np.float32)
    d = np.sqrt(((a - np.asarray(ink_rgb, dtype=np.float32)) ** 2).sum(axis=2))
    w = np.clip(1.0 - d / INK_TOL, 0.0, 1.0) * mask
    return w.sum(axis=1)


def layer_ink_rows(layer: Image.Image, mask: np.ndarray) -> np.ndarray:
    a = np.asarray(layer.split()[-1], dtype=np.float32) / 255.0
    return (a * mask).sum(axis=1)


def measure(brain: Path, look: Path) -> Dict[str, Any]:
    cap = brain / "captions.plaintext.json"
    data = json.loads(cap.read_text())
    out: Dict[str, Any] = {}
    for st in data["states"]:
        parts = st["parts"]
        orn = [p for p in parts if _PART_FACE.get(str(p.get("part"))) == "ornate"]
        if not orn:
            continue
        pl = [_part_box(p) for p in parts if _PART_FACE.get(str(p.get("part"))) == "plain"]
        mid = (int(st["in"]) + int(st["out"])) // 2
        png = look / f"{st['id']}_{mid}_ref.png"
        if not png.exists():
            out[st["id"]] = {"error": f"no {png.name}"}; continue
        frame = Image.open(png)
        for p in orn:
            box = _part_box(p)
            mask = env_mask(box, pl[0] if pl else None, frame.size)
            ink = p.get("ink_rgb") or p.get("ink_rgb_approx") or (255, 255, 255)
            env = row_envelope(ref_ink_rows(frame, ink, mask))
            p["ref_ink_env"] = list(env) if env else None
            p["env_mask"] = {"ornate_box": box, "minus_plain_box": pl[0] if pl else None, "frame": mid}
            out[st["id"]] = {"text": p["text"], "ref_ink_env": p["ref_ink_env"], "box": box}
    cap.write_text(json.dumps(data, indent=2))
    return out


if __name__ == "__main__":
    res = measure(Path(sys.argv[1]), Path(sys.argv[2]))
    for k, v in res.items():
        print(k, v)
