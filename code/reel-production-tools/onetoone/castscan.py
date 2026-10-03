#!/usr/bin/env python3
"""onetoone.castscan — rank candidate masters for a cutgrid slot by what the CAPTION needs.

Why: casting by "world/role" alone shipped a reference c04a with navy ink over dark glass and c05c
across the subject's face. The caption zone is a casting constraint: where the
reference's bed under the ink is pale and plain, ours must be too. This scans candidates at the
frame offsets where the slot's caption states land and scores the bed under every ink box
against the reference's own bed (mean luma distance + texture), so the recast is chosen by the
same measure the readability gate enforces.

Usage:
    python3 -m onetoone.castscan <brain> <cast.json> <reference.mp4> <SLOT> [--stems a,b,c]
                                 [--times 1,3,6] [--top 20] [--out DIR]
Candidates default to every master in the footage library. ONE ffmpeg at a time (sequential).
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image, ImageStat

from onetoone.captions_typeset import _luma, typeset_state
from onetoone.looksheet import COVER, FFMPEG, FOOTAGE, FPS, LUT, resolve_master


def all_master_stems() -> List[str]:
    stems = set()
    for p in glob.glob(str(FOOTAGE / "masters*/*.MP4")) + glob.glob(str(FOOTAGE / "masters*/*.mp4")):
        stems.add(Path(p).stem.split("__")[-1])
    return sorted(stems)


def caption_boxes(brain: Path, slot: Dict[str, Any], *, size: Tuple[int, int]) -> List[Tuple[str, int, List[Tuple[str, Tuple[int, int, int, int], float]]]]:
    """[(state_id, frame_offset_from_slot_in, [(text, ink_box, ink_L)])] for states riding the slot."""
    states = json.loads((brain / "captions.plaintext.json").read_text())["states"]
    out = []
    for st in states:
        mid = (int(st["in"]) + int(st["out"])) // 2
        if not (int(slot["in"]) <= mid <= int(slot["out"])):
            continue
        _, rep = typeset_state(st, canvas=size)
        parts_by_text = {str(p.get("text") or " ".join(p.get("words") or [])): p for p in st["parts"]}
        boxes = []
        for r in rep["parts"]:
            ink = parts_by_text.get(r["text"], {}).get("ink_rgb") or parts_by_text.get(r["text"], {}).get("ink_rgb_approx") or (255, 255, 255)
            x0, y0, x1, y1 = [max(0, int(v)) for v in r["ink"]]
            boxes.append((r["text"], (x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)), _luma(ink)))
        out.append((st["id"], mid - int(slot["in"]), boxes))
    return out


def grab(src: Path, t: float, out: Path, *, size: Tuple[int, int], lut: bool) -> bool:
    vf = (f"lut3d=file={LUT}," if lut else "") + COVER.format(w=size[0], h=size[1])
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.4f}", "-i", str(src), "-vf", vf,
                    "-frames:v", "1", str(out)], capture_output=True)
    return out.exists()


def bed_stats(png: Path, box) -> Tuple[float, float]:
    s = ImageStat.Stat(Image.open(png).convert("L").crop(box))
    return s.mean[0], s.stddev[0]


def allowed_stems(stems: List[str], *, identity: bool, pool: Dict[str, Any] | None = None, grades: Dict[str, Any] | None = None) -> List[str]:
    """The reviewer's grades and the identity pool as a filter: NEVER and blacklisted
    stems never scan; an identity slot (the reference shows a person) scans ONLY HERO/settled stems,
    with HERO first so the bed score breaks ties among the clips the reviewer called good."""
    from onetoone import grades as gr
    from onetoone.identity import is_blacklisted, load_pool
    pool = pool or load_pool()
    grades = gr.load_grades() if grades is None else grades
    never = gr.never_stems(grades)
    out = [s for s in stems if s not in never and not is_blacklisted(s, pool=pool)]
    if identity:
        settled = gr.effective_settled(pool["settled_pool"], grades)
        bad = gr.broll_stems(grades) | gr.not_operator_stems(grades)
        out = [s for s in out if s in settled and s not in bad]
        heroes = gr.hero_stems(grades)
        out.sort(key=lambda s: (s not in heroes, s))
    return out


def scan(brain: Path, cast_json: Path, reference: Path, slot_id: str, *, stems: List[str] | None,
         times: List[float], outdir: Path, size=(1916, 1078), identity: bool | None = None) -> List[Dict[str, Any]]:
    cutgrid = {s["slot"]: s for s in json.loads((brain / "cutgrid.json").read_text())["shots"]}
    slot = cutgrid[slot_id]
    if identity is None:
        # the reference decides: a person on screen in this shot = identity slot
        rp = brain / "refpeople.json"
        if rp.exists():
            from onetoone.refpeople import window_verdict
            identity = window_verdict(json.loads(rp.read_text()), int(slot["in"]), int(slot["out"]))["person"]
        else:
            identity = False
    stems = allowed_stems(list(stems or all_master_stems()), identity=bool(identity))
    needs = caption_boxes(brain, slot, size=size)
    outdir.mkdir(parents=True, exist_ok=True)
    # reference beds at the same frames
    ref_beds = {}
    for sid, off, boxes in needs:
        png = outdir / f"ref_{sid}.png"
        grab(reference, (int(slot["in"]) + off) / FPS, png, size=size, lut=False)
        ref_beds[sid] = [(text, box, ink_L, *bed_stats(png, box)) for text, box, ink_L in boxes]
    from onetoone import grades as gr
    gl = gr.load_grades()
    rows = []
    for stem in stems:
        m = resolve_master(stem)
        if not m:
            continue
        # segment grades (Deck grader): identity slots scan only inside HERO segments; nobody scans a NEVER segment
        hw = gr.hero_windows(stem, gl)
        stem_times = [t for t in times if gr.window_grade(stem, t, None, gl) != "NEVER"]
        if identity and hw:
            stem_times = sorted({t for t in stem_times if any(a <= t < b for a, b in hw)} | {a for a, b in hw})
        for t0 in stem_times:
            score = 0.0; detail = []
            ok = True
            for sid, off, _ in needs:
                png = outdir / f"{stem}_{t0}_{sid}.png"
                if not grab(m, t0 + off / FPS, png, size=size, lut=True):
                    ok = False; break
                for text, box, ink_L, rL, rsd in ref_beds[sid]:
                    L, sd = bed_stats(png, box)
                    ref_c, c = abs(ink_L - rL), abs(ink_L - L)
                    # penalty: lost contrast (relative to the reference) + added clutter
                    score += max(0.0, ref_c - c) / max(ref_c, 1.0) + max(0.0, sd - rsd) / 50.0
                    detail.append({"state": sid, "text": text, "bed_L": round(L, 1), "ref_bed_L": round(rL, 1),
                                   "contrast": round(c, 1), "ref_contrast": round(ref_c, 1), "bed_sd": round(sd, 1), "ref_bed_sd": round(rsd, 1)})
            if ok:
                rows.append({"stem": stem, "in_s": t0, "penalty": round(score, 3), "detail": detail})
    rows.sort(key=lambda r: r["penalty"])
    (outdir / f"castscan_{slot_id}.json").write_text(json.dumps({"slot": slot_id, "identity_slot": bool(identity), "stems_scanned": len(stems),
                                                                 "needs": [(s, o) for s, o, _ in needs], "rows": rows}, indent=1))
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("brain"); ap.add_argument("cast"); ap.add_argument("reference"); ap.add_argument("slot")
    ap.add_argument("--stems", default=None); ap.add_argument("--times", default="1,3,6"); ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--out", default=None)
    ap.add_argument("--identity", choices=["auto", "yes", "no"], default="auto",
                    help="identity slot? auto = brain/refpeople.json decides (person in the reference shot); yes/no overrides")
    a = ap.parse_args()
    out = Path(a.out) if a.out else Path(a.brain).parent / f"castscan-{a.slot}"
    rows = scan(Path(a.brain), Path(a.cast), Path(a.reference), a.slot,
                stems=a.stems.split(",") if a.stems else None, times=[float(t) for t in a.times.split(",")], outdir=out,
                identity={"auto": None, "yes": True, "no": False}[a.identity])
    for r in rows[: a.top]:
        print(f"penalty {r['penalty']:6.3f}  {r['stem']} @ {r['in_s']}s  " +
              " ".join(f"[{d['state']} {d['text']!r} c {d['contrast']}/{d['ref_contrast']} sd {d['bed_sd']}/{d['ref_bed_sd']}]" for d in r["detail"]))
