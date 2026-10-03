#!/usr/bin/env python3
"""onetoone.looksheet — the REVIEW-EARLY artifact.

Before any full render, build a LOOK SHEET: for every caption state, the reference's own frame
beside OUR candidate frame — our own footage (from the cast), natural-graded, with
the typeset caption composited — plus the hook and grade-only shots. A reviewer approves the LOOK on
stills; only then does a full render run. This is what replaces 5–7 blind rebuild rounds.

Frame source law: candidate frames come ONLY from the authorized footage library masters named
in the cast (never the reference's pixels, never research folders). ONE ffmpeg at a time.
"""
from __future__ import annotations

import glob
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from PIL import Image, ImageDraw

from onetoone.captions_typeset import readability, typeset_state

from onetoone.ffx import FFMPEG  # noqa: E402
from onetoone.housechain import LUT_PACKAGED  # noqa: E402
from rf_paths import FOOTAGE_LIBRARY_ROOT  # noqa: E402
FPS = 24000 / 1001
FOOTAGE = FOOTAGE_LIBRARY_ROOT
# Natural/vivid grade: lift crushed shadows,
# restore saturation, gentle contrast — never force the reference's raw percentiles onto
# different footage. Per-shot refinement is layered on top later; this is the honest base.
NATURAL_GRADE = "curves=all='0/0.045 0.25/0.31 0.75/0.80 1/1',eq=contrast=1.06:saturation=1.22:gamma=1.05"
# Crop law: cover-scale then crop, rotation upright before scale.
COVER = "scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def resolve_master(stem: str) -> Optional[Path]:
    """masters/ uses <hash>__<stem>.MP4; later pulls live in masters-row*-pull-*/<stem>.MP4."""
    pats = [f"{FOOTAGE}/masters/*__{stem}.MP4", f"{FOOTAGE}/masters/*__{stem}.mp4",
            f"{FOOTAGE}/masters-*/{stem}.MP4", f"{FOOTAGE}/masters-*/{stem}.mp4",
            f"{FOOTAGE}/masters-*/*__{stem}.MP4"]
    for p in pats:
        hits = sorted(glob.glob(p))
        if hits:
            return Path(hits[0])
    return None


# The masters are Sony S-Log3 Cine / S-Gamut3.Cine (camera model sidecar-verified). Raw log frames
# are flat, bright-mids and desaturated by nature —: every ungraded
# candidate sat 18–61 vs reference 56–221. The hash-pinned official LC-709 LUT is the
# technical conversion (reelctl color contract); mood matching layers on top of it.
LUT = LUT_PACKAGED


def frame_stats(path: Path) -> Tuple[float, float, float]:
    """(mean luma, mean saturation, warmth R-B) on a 320x180 downsample — the mood numbers."""
    from PIL import ImageStat
    im = Image.open(path).convert("RGB").resize((320, 180))
    L = ImageStat.Stat(im.convert("L")).mean[0]
    S = ImageStat.Stat(im.convert("HSV")).mean[1]
    r, g, b = ImageStat.Stat(im).mean
    return L, S, r - b


def mood_eq(cand: Tuple[float, float, float], ref: Tuple[float, float, float]) -> str:
    """Gentle, clamped eq that moves a LUT'd candidate TOWARD its reference shot's mood.

    Not a percentile force-match — a bounded push on brightness
    and saturation so a dark reference shot reads dark and a saturated one reads saturated,
    while our own footage stays itself. Clamps keep it from ever going garish.
    """
    cL, cS, _ = cand; rL, rS, _ = ref
    # NATURAL beats matched. Sheets v2/v3 proved that chasing the reference's
    # stylised saturation (150–220) 2x over turns our own footage garish/posterised —
    # the same artifact class that sank v006/v007. Land ~70% of the way and stay natural:
    # that is the "natural" look. Brightness gently, saturation capped at 1.5,
    # gamma pull only for genuinely dark references and never below 0.95.
    brightness = max(-0.22, min(0.18, (rL - cL) / 255.0 * 0.70))
    saturation = max(0.80, min(1.50, 1.0 + ((rS / cS) - 1.0) * 0.70 if cS > 1 else 1.0))
    gamma = 0.95 if (rL - cL) < -25 else 1.0
    return f"eq=brightness={brightness:.3f}:saturation={saturation:.3f}:contrast=1.03:gamma={gamma:.2f}"


def extract_frame(src: Path, t: float, out: Path, *, size: Tuple[int, int], grade: bool,
                  mood_ref: Optional[Path] = None) -> Optional[str]:
    """Extract one frame. grade=True applies LC-709; with mood_ref, a second pass adds the
    per-shot mood eq derived from the reference frame. Returns the eq string used (or None)."""
    cover = COVER.format(w=size[0], h=size[1])
    def run(vf: str) -> None:
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.4f}", "-i", str(src),
                        "-vf", vf, "-frames:v", "1", str(out)], check=True)
    if not grade:
        run(cover); return None
    run(f"lut3d=file={LUT},{cover}")
    if mood_ref is None:
        return "lut3d"
    eq = mood_eq(frame_stats(out), frame_stats(mood_ref))
    run(f"lut3d=file={LUT},{eq},{cover}")
    return eq


def shot_for_frame(shots: List[Mapping[str, Any]], frame: int) -> Optional[Mapping[str, Any]]:
    for s in shots:
        if int(s["in"]) <= frame <= int(s["out"]):
            return s
    return None


def build(brain: Path, cast_json: Path, reference: Path, outdir: Path,
          *, size: Tuple[int, int] = (1916, 1078), grade_slots: Tuple[str, ...] = ("S01", "S03", "S06")) -> Dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)
    cutgrid = json.loads((brain / "cutgrid.json").read_text())
    captions = json.loads((brain / "captions.plaintext.json").read_text())
    cast = {s["slot"]: s for s in json.loads(cast_json.read_text())["slots"]}
    shots = cutgrid["shots"]
    report: Dict[str, Any] = {"states": [], "grade_shots": [], "problems": []}

    def candidate_frame(frame: int, name: str, *, grade: bool = True, mood_ref: Optional[Path] = None) -> Optional[Path]:
        shot = shot_for_frame(shots, frame)
        if not shot:
            report["problems"].append(f"{name}: no cutgrid shot covers frame {frame}"); return None
        slot = cast.get(shot["slot"])
        if not slot:
            report["problems"].append(f"{name}: slot {shot['slot']} not in cast"); return None
        master = resolve_master(str(slot["stem"]))
        if not master:
            report["problems"].append(f"{name}: master {slot['stem']} not on disk"); return None
        t = float(slot["in_s"]) + (frame - int(shot["in"])) / FPS
        out = outdir / f"{name}_cand.png"
        eq = extract_frame(master, t, out, size=size, grade=grade, mood_ref=mood_ref)
        report.setdefault("grade_eq", {})[name] = eq
        return out

    def ref_frame(frame: int, name: str) -> Path:
        out = outdir / f"{name}_ref.png"
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{frame / FPS:.4f}", "-i", str(reference),
                        "-frames:v", "1", str(out)], check=True)
        return out

    tiles: List[Tuple[str, Image.Image, Image.Image]] = []
    # 1) hook + grade-only shots (no captions): does the footage + grade LOOK like the reference?
    for shot in shots:
        if shot["slot"] in grade_slots:
            mid = (int(shot["in"]) + int(shot["out"])) // 2
            name = f"grade_{shot['slot']}"
            r = ref_frame(mid, name); c = candidate_frame(mid, name, mood_ref=r)
            if c:
                tiles.append((f"{shot['slot']} GRADE  ref | ours", Image.open(r).convert("RGB"), Image.open(c).convert("RGB")))
                report["grade_shots"].append({"slot": shot["slot"], "frame": mid, "stem": cast[shot["slot"]]["stem"]})
    # 2) every caption state: reference frame | our footage + natural grade + typeset caption
    for st in captions["states"]:
        mid = (int(st["in"]) + int(st["out"])) // 2
        name = f"cap_{st['id']}"
        r = ref_frame(mid, name); c = candidate_frame(mid, name, mood_ref=r)
        if not c:
            continue
        bed = Image.open(c).convert("RGBA")
        layer, rep = typeset_state(st, canvas=bed.size)
        read = readability(bed, rep, st, ref_bed=Image.open(r).convert("RGB"))
        comp = Image.alpha_composite(bed, layer).convert("RGB")
        comp.save(outdir / f"{name}_ours.png")
        bad = [p for p in read["parts"] if not p["ok"]]
        why = "; ".join(f"{p['text']!r} " + ("contrast" if not p["contrast_ok"] else "clutter") for p in bad)
        label = f"{st['id']} CAPTION  ref | ours" + ("" if read["ok"] else f"   !! {why} — recast this shot")
        if bad:
            report["problems"].append(f"{st['id']}: " + ", ".join(
                f"{p['text']!r} contrast {p['contrast']}/{p['need']} clutter sd {p['bed_sd']} vs ref {p['ref_bed_sd']}" for p in bad))
        tiles.append((label, Image.open(r).convert("RGB"), comp))
        report["states"].append({"id": st["id"], "frame": mid, "parts": rep["parts"], "readability": read})

    # 3) the sheet: two states per row, each = [reference | ours]
    W = 470
    scaled = [(n, a.resize((W, int(a.size[1] * W / a.size[0]))), b.resize((W, int(b.size[1] * W / b.size[0])))) for n, a, b in tiles]
    h = scaled[0][1].size[1]; lab = 24; per_row = 2
    rows = (len(scaled) + per_row - 1) // per_row
    sheet = Image.new("RGB", (W * 2 * per_row, (h + lab) * rows), (18, 18, 18)); d = ImageDraw.Draw(sheet)
    for n, (label, a, b) in enumerate(scaled):
        x = (n % per_row) * W * 2; y = (n // per_row) * (h + lab)
        sheet.paste(a, (x, y + lab)); sheet.paste(b, (x + W, y + lab)); d.text((x + 6, y + 5), label, fill=(255, 210, 120))
    sheet_path = outdir / "LOOKSHEET.png"; sheet.save(sheet_path)
    report["sheet"] = str(sheet_path); report["tiles"] = len(tiles)
    (outdir / "looksheet_report.json").write_text(json.dumps(report, indent=1))
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("brain"); ap.add_argument("cast"); ap.add_argument("reference"); ap.add_argument("outdir")
    a = ap.parse_args()
    rep = build(Path(a.brain), Path(a.cast), Path(a.reference), Path(a.outdir))
    print(json.dumps({k: v for k, v in rep.items() if k != "states"}, indent=1))
