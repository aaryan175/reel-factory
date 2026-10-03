#!/usr/bin/env python3
"""onetoone.bedprobe — will THIS clip window carry THESE captions? Measured before a render.

The render's readability gate refuses a state whose ink vanishes into our bed (v007: six
states). Fixing that by re-rendering the whole reel per guess is slow; this probes one slot's
candidate window (stem, in_s, crop spec, optional darkening target) the same way the render
judges it: crop → LUT → cover → (luma_pull to target) → the state's typeset layer →
captions_typeset.readability against the reference bed.

    python3 -m onetoone.bedprobe <brain> <work_with_beds> S12 CLIP_0060 1.92 \
        --crop '{"zoom":1.3,"cx":0.62,"cy":0.4}' --target 66 [--states c07a,c07d] [--png out.png]

Prints one line per part: contrast/need and bed sd/ref sd, OK or FAIL. Exit 0 iff every part ok.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from PIL import Image

from onetoone.captions_typeset import readability, typeset_state
from onetoone.ffx import FFMPEG, run
from onetoone.framing import display_dims, filter_for
from onetoone.looksheet import COVER, FPS, LUT, resolve_master


def probe(brain: Path, beds_dir: Path, slot: str, stem: str, in_s: float, crop: Mapping[str, Any] | None,
          *, target: float | None = None, states: List[str] | None = None, size: Tuple[int, int] = (1916, 1078),
          outdir: Path | None = None, keep_png: Path | None = None) -> Dict[str, Any]:
    from onetoone.render import luma_pull  # local import: render imports this module's siblings
    cutgrid = {s["slot"]: s for s in json.loads((brain / "cutgrid.json").read_text())["shots"]}
    shot = cutgrid[slot]
    all_states = json.loads((brain / "captions.plaintext.json").read_text())["states"]
    live = [st for st in all_states if int(st["in"]) <= int(shot["out"]) and int(st["out"]) >= int(shot["in"])]
    if states:
        live = [st for st in live if st["id"] in states]
    master = resolve_master(stem)
    if master is None:
        raise FileNotFoundError(stem)
    mw, mh = display_dims(str(master))
    cf = filter_for(crop, mw, mh, aspect=size[0] / size[1])
    pre = (f"{cf}," if cf else "") + f"lut3d=file={LUT}"
    outdir = outdir or (beds_dir.parent / "bedprobe"); outdir.mkdir(parents=True, exist_ok=True)
    w, h = size
    # darkening curve from the shot's mid frame (same rule as the render)
    mid_f = (int(shot["in"]) + int(shot["out"])) // 2
    pull_filter = ""
    pull: Dict[str, Any] = {}
    if target is not None:
        probe_png = outdir / f"{slot}_{stem}_{in_s}_probe.png"
        run(["-y", "-loglevel", "error", "-ss", f"{in_s + (mid_f - int(shot['in'])) / FPS:.4f}", "-i", str(master), "-vf",
             f"{pre},{COVER.format(w=w, h=h)},scale=640:360,format=rgb24", "-frames:v", "1", str(probe_png)])
        pull = luma_pull(probe_png, float(target))
        if pull["filter"]:
            pull_filter = "," + pull["filter"]
    result: Dict[str, Any] = {"slot": slot, "stem": stem, "in_s": in_s, "crop": crop, "pull": pull, "states": [], "ok": True}
    for st in live:
        mid = (int(st["in"]) + int(st["out"])) // 2
        # the state's mid frame may lie outside this shot: clamp to the shot's span
        f = min(max(mid, int(shot["in"])), int(shot["out"]))
        t = in_s + (f - int(shot["in"])) / FPS
        bed_png = outdir / f"{slot}_{stem}_{in_s}_{st['id']}.png"
        run(["-y", "-loglevel", "error", "-ss", f"{t:.4f}", "-i", str(master), "-vf",
             f"{pre}{pull_filter},{COVER.format(w=w, h=h)},format=rgb24", "-frames:v", "1", str(bed_png)])
        layer, rep = typeset_state(st, canvas=size)
        ref_png = beds_dir / f"{st['id']}_ref.png"
        rd = readability(Image.open(bed_png).convert("RGBA"), rep, st,
                         ref_bed=Image.open(ref_png).convert("RGB") if ref_png.exists() else None)
        result["states"].append({"id": st["id"], "ok": rd["ok"], "parts": rd["parts"], "bed": str(bed_png)})
        result["ok"] = result["ok"] and rd["ok"]
        if keep_png is not None:
            comp = Image.open(bed_png).convert("RGBA"); comp.alpha_composite(layer)
            comp.convert("RGB").save(keep_png.with_name(keep_png.stem + f"_{st['id']}_COMP.png"))
    return result


def fmt(res: Mapping[str, Any]) -> str:
    lines = [f"{res['slot']} {res['stem']} @{res['in_s']} crop={json.dumps(res['crop'])} "
             + (f"pull g={res['pull'].get('gamma')} {res['pull'].get('ours')}->{res['pull'].get('after', res['pull'].get('ours'))}" if res.get("pull") else "")]
    for st in res["states"]:
        for p in st["parts"]:
            lines.append(f"  {st['id']:5} {p['text']!r:14} contrast {p['contrast']:6.1f}/{p['need']:5.1f}  bed sd {p['bed_sd']:5.1f}/ref {p['ref_bed_sd']}  "
                         + ("OK" if p["ok"] else "FAIL"))
    lines.append("  => " + ("PASS" if res["ok"] else "FAIL"))
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("brain"); ap.add_argument("work"); ap.add_argument("slot"); ap.add_argument("stem"); ap.add_argument("in_s", type=float)
    ap.add_argument("--crop", default=None); ap.add_argument("--target", type=float, default=None)
    ap.add_argument("--states", default=None); ap.add_argument("--png", default=None)
    a = ap.parse_args()
    res = probe(Path(a.brain), Path(a.work) / "beds", a.slot, a.stem, a.in_s, json.loads(a.crop) if a.crop else None,
                target=a.target, states=a.states.split(",") if a.states else None,
                keep_png=Path(a.png) if a.png else None)
    print(fmt(res))
    raise SystemExit(0 if res["ok"] else 1)
