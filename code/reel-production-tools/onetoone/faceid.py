#!/usr/bin/env python3
"""onetoone.faceid — identify a reference's typeface from its own pixels, then prove it.

A caption face is identified, never assumed: setting script words in whatever face happened
to be used last produces captions that are visibly "not the reference face". This module
gives a repeatable path from reference pixels to a proven face.

The steps, each a sub-command (run from the reel-production-tools directory):

  1. ink-crop   a clean black-on-white crop of ONE caption line from a reference frame
       python3 -m onetoone.faceid ink-crop --frame ref/0151.png --box 566,100,1400,490 --ink 15,20,46 -o work/face/line.png
  2. identify   prints guidance: submit the crop to a font-identification service of your
                choice by hand (respecting that service's terms), or shortlist candidates
                yourself; either way the proof is step 4, not the service's ranking.
  3. find       look for a named face among the configured font directories and earlier intakes
       python3 -m onetoone.faceid find "Pinyon"
  4. fit        set the word in the candidate face against the reference frame; writes the
                overlay (our ink in red over the reference) and prints the ref_fit to store
       python3 -m onetoone.faceid fit --frame ref/0151.png --box 566,100,1400,490 --ink 15,20,46 \\
               --text Sample --face ~/Library/Fonts/PinyonScript-Regular.ttf -o work/face/fit_c18.jpg

  5. trace      ANALYSIS AID: build a rough outline face from the reference's own ink so its
                letterforms can be compared glyph-for-glyph when no installed face matches.
                Each call traces the glyphs of one clean caption line into <out>.ttf
                (+ <out>.glyphs.json sidecar, <out>.sheet.png reference-vs-built sheet):
       python3 -m onetoone.faceid trace --frame work/refframes/ref-200.png --box 420,300,1500,560 \\
               --ink 250,250,250 --text "SAMPLE WORDS" --out work/fonts/RefTrace-Medium.ttf --name "RefTrace Medium"
       python3 -m onetoone.faceid trace --need "ALL THE CAPTION TEXT" --out work/fonts/RefTrace-Medium.ttf   # lists what is still missing
                Then `fit` the traced face against a DIFFERENT reference frame than the ones traced:
                that overlay is the proof of the measurement.

Fonts: for anything you publish, use fonts you are licensed to use (open-licence faces such
as those from Google Fonts / SIL OFL, or faces you have purchased). Traced faces are
measurement artefacts for matching letterforms; check the reference face's licence before
distributing any output that reproduces it.

The PROOF is step 4's overlay read with your own eyes: glyph for glyph, the red ink sits on
the reference ink. A score alone is not proof (two formal scripts score alike). The part in
brain/captions.plaintext.json then carries:
    "face_file": ["~/Library/Fonts/<file>", 0], "face_name": "...", "face_evidence": "...",
    "ref_fit": {<what `fit` printed>}
A script word is ONE connected run (this fit), never capital + letter-spaced tail.
A substitute face is a failed match.

Font search directories: the system defaults below plus any extra directories listed in
REEL_FACTORY_FONT_DIRS (os.pathsep-separated).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np
from PIL import Image, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rf_paths import REEL_HOME, WORKDRIVE  # noqa: E402

HOME = Path(os.path.expanduser("~"))
FONT_DIRS = [HOME / "Library" / "Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts"),
             Path("/System/Library/Fonts/Supplemental"), Path("/usr/share/fonts"), HOME / ".fonts"]
FONT_DIRS += [Path(d).expanduser() for d in (os.environ.get("REEL_FACTORY_FONT_DIRS") or "").split(os.pathsep) if d]
# faces that earlier reference intakes already hold
INTAKE_GLOBS = ["reference-intake/*/analysis/*.otf", "reference-intake/*/analysis/*.ttf"]


def _ints(s: str) -> List[int]:
    return [int(v) for v in str(s).split(",")]


def ink_mask(frame: Image.Image, box: Sequence[int], ink: Sequence[int],
             minus: Sequence[Sequence[int]] = (), pad: int = 24) -> np.ndarray:
    """Float mask (0..1) of pixels close to `ink` inside the padded box, minus the boxes of
    neighbouring lines. Dark ink is keyed on luma, light ink on the minimum channel."""
    a = np.asarray(frame.convert("RGB")).astype(np.float32)
    h, w = a.shape[:2]
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
    if sum(ink) / 3.0 < 110:
        m = np.clip((150.0 - a.mean(axis=2)) / 80.0, 0.0, 1.0)
    else:
        m = np.clip((a.min(axis=2) - 170.0) / 60.0, 0.0, 1.0)
    keep = np.zeros((h, w), np.float32)
    keep[y0:y1, x0:x1] = 1.0
    for b in minus:
        bx0, by0, bx1, by1 = [int(v) for v in b]
        keep[max(0, by0 - 4):by1 + 4, max(0, bx0 - 6):bx1 + 6] = 0.0
    return (m * keep)[y0:y1, x0:x1]


def cmd_ink_crop(a) -> int:
    m = ink_mask(Image.open(a.frame), _ints(a.box), _ints(a.ink), [_ints(b) for b in a.minus or []])
    im = Image.fromarray((255 - m * 255).astype("uint8"))
    k = max(1, int(round(1600 / max(1, im.width))))
    if k > 1:
        im = im.resize((im.width * k, im.height * k), Image.LANCZOS)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    im.save(a.out)
    print(json.dumps({"crop": str(a.out), "size": im.size}))
    return 0


def cmd_identify(a) -> int:
    """Guidance only. This tool does not automate third-party font-ID websites."""
    print(json.dumps({
        "ok": False,
        "crop": str(Path(a.crop).resolve()),
        "next": ("submit this crop to a font-identification service by hand (respect its terms of "
                 "service), then `find` and `fit` each candidate; the fit overlay is the proof"),
    }, indent=1))
    return 2


def cmd_find(a) -> int:
    pat = a.name.lower().replace(" ", "")
    hits = []
    for g in INTAKE_GLOBS:
        for root in (REEL_HOME, WORKDRIVE):
            if root.is_dir():
                hits += [str(p) for p in root.glob(g) if pat in p.name.lower().replace(" ", "")]
    for d in FONT_DIRS:
        if d.is_dir():
            hits += [str(p) for p in d.iterdir() if pat in p.name.lower().replace(" ", "").replace("-", "")]
    print(json.dumps({"here": sorted(set(hits)),
                      "next": "install a licensed/open face if none matched, then `fit` it"}, indent=1))
    return 0 if hits else 2


def cmd_fit(a) -> int:
    import cv2
    from onetoone import refit
    face = (os.path.expanduser(a.face.split(",")[0]), int(a.face.split(",")[1]) if "," in a.face else 0)
    frame = Image.open(a.frame).convert("RGB")
    box, ink = _ints(a.box), _ints(a.ink)
    minus = [_ints(b) for b in a.minus or []]
    ref = cv2.GaussianBlur(np.ascontiguousarray(_full_mask(frame, box, ink, minus)), (0, 0), 1.0)
    probe = ImageFont.truetype(face[0], 200, index=face[1]).getbbox(a.text)
    size0 = max(10, int(round(200 * (box[3] - box[1]) / max(1, probe[3] - probe[1]))))
    best = {"score": -1.0}
    for size in sorted({int(round(size0 * k)) for k in np.arange(0.80, 1.45, 0.02)}):
        f = ImageFont.truetype(face[0], size, index=face[1])
        for tr in np.arange(-0.10, 0.021, 0.01):
            g = refit.draw_tracked(frame.size, (0.0, 0.0), a.text, f, (255, 255, 255, 255), float(tr) * size, 1.0)
            bb = g.getbbox()
            if bb is None or bb[2] - bb[0] >= ref.shape[1] or bb[3] - bb[1] >= ref.shape[0]:
                continue
            tpl = cv2.GaussianBlur((np.asarray(g.crop(bb))[..., 3] > 110).astype(np.float32), (0, 0), 1.0)
            res = cv2.matchTemplate(ref, tpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if mx > best["score"]:
                best = {"score": round(float(mx), 3), "size": int(size), "tracking_em": round(float(tr), 3),
                        "stretch": 1.0, "space_k": 1.0, "xy": [float(loc[0] - bb[0]), float(loc[1] - bb[1])],
                        "ink_box": [int(loc[0]), int(loc[1]), int(loc[0] + bb[2] - bb[0]), int(loc[1] + bb[3] - bb[1])]}
    if best["score"] < 0:
        print(json.dumps({"ok": False, "why": "no size of this face fits inside the frame"}))
        return 2
    f = ImageFont.truetype(face[0], best["size"], index=face[1])
    g = refit.draw_tracked(frame.size, tuple(best["xy"]), a.text, f, (255, 0, 0, 150), best["tracking_em"] * best["size"], 1.0)
    ov = frame.convert("RGBA")
    ov.alpha_composite(g)
    pad = 60
    c = ov.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(frame.width, box[2] + pad), min(frame.height, box[3] + pad)))
    c = c.resize((c.width * 2, c.height * 2), Image.LANCZOS).convert("RGB")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    c.save(a.out, quality=90)
    print(json.dumps({"ref_fit": best, "overlay": str(a.out),
                      "read_it": "open the overlay: red = our face. Glyph-for-glyph on the reference ink = the face is right."}, indent=1))
    return 0


# ----------------------------------------------------------------------------- trace
UPM = 1000          # font units per em
CAP = 700           # the tallest traced glyph of a line sits at this cap height
TRACE_UP = 4        # mask upscale before contouring: the polygon error is 1/TRACE_UP px of the frame


def _runs(cols: np.ndarray, min_gap: int) -> List[List[int]]:
    """[x0, x1) runs of ink columns; gaps shorter than min_gap do not split a glyph."""
    runs: List[List[int]] = []
    x = 0
    n = len(cols)
    while x < n:
        if cols[x]:
            x0 = x
            while x < n and cols[x]:
                x += 1
            if runs and x0 - runs[-1][1] < min_gap:
                runs[-1][1] = x
            else:
                runs.append([x0, x])
        else:
            x += 1
    return runs


def segment_glyphs(mask: np.ndarray, text: str) -> tuple[List[List[int]], str]:
    """Split a binary line mask into one column run per non-space character of `text`.
    Returns (runs, how): how = exact | merged N | split N. Merges the narrowest gaps when the mask
    has more runs than characters (a broken stroke), splits the widest runs at their thinnest column
    when it has fewer (touching letters)."""
    chars = [c for c in text if not c.isspace()]
    h = mask.shape[0]
    cols = mask.sum(axis=0) > 0
    runs = _runs(cols, max(1, h // 60))
    merged = split = 0
    if abs(len(runs) - len(chars)) > 2:
        # more than two repairs = the text or the box is wrong, not the ink; never guess letters
        return runs, f"{len(runs)} runs for {len(chars)} characters"
    while len(runs) > len(chars) and len(runs) > 1:
        gaps = [runs[i + 1][0] - runs[i][1] for i in range(len(runs) - 1)]
        i = int(np.argmin(gaps))
        runs[i][1] = runs[i + 1][1]; del runs[i + 1]; merged += 1
    while len(runs) < len(chars) and runs:
        i = int(np.argmax([r[1] - r[0] for r in runs]))
        x0, x1 = runs[i]
        prof = mask[:, x0:x1].sum(axis=0)
        inner = prof[int(0.2 * (x1 - x0)):int(0.8 * (x1 - x0))]
        if len(inner) == 0:
            break
        cut = x0 + int(0.2 * (x1 - x0)) + int(np.argmin(inner))
        runs[i:i + 1] = [[x0, cut], [cut, x1]]; split += 1
    how = "exact" if not (merged or split) else f"merged {merged}" if merged else f"split {split}"
    return runs, how


def _contours_font_units(sub: np.ndarray, scale: float, base_y: int, x_shift: int) -> List[List[tuple]]:
    """Closed polygons in font units from a binary glyph mask: outer contours clockwise, holes
    counter-clockwise (TrueType), y up from the line's baseline."""
    import cv2
    up = cv2.resize(sub.astype(np.float32), (sub.shape[1] * TRACE_UP, sub.shape[0] * TRACE_UP), interpolation=cv2.INTER_CUBIC)
    bw = (up > 0.5).astype(np.uint8)
    found = cv2.findContours(bw, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    cs, hier = found[-2], found[-1]
    out = []
    if hier is None:
        return out
    for i, c in enumerate(cs):
        if len(c) < 3 or cv2.contourArea(c) < 4 * TRACE_UP * TRACE_UP:
            continue
        c = cv2.approxPolyDP(c, 0.35 * TRACE_UP, True)
        pts = []
        for p in c.reshape(-1, 2):
            fx = (p[0] / TRACE_UP + x_shift) * scale
            fy = (base_y - p[1] / TRACE_UP) * scale
            pts.append((int(round(fx)), int(round(fy))))
        if len(pts) < 3:
            continue
        area = sum(pts[k][0] * pts[(k + 1) % len(pts)][1] - pts[(k + 1) % len(pts)][0] * pts[k][1] for k in range(len(pts))) / 2.0
        is_hole = hier[0][i][3] != -1
        if (area > 0) != is_hole:       # outer must be clockwise (negative area, y up); hole the reverse
            pts.reverse()
        out.append(pts)
    return out


def _glyph_name(ch: str) -> str:
    return f"uni{ord(ch):04X}"


def build_ttf(glyphs: dict, out: Path, family: str) -> None:
    """glyphs: {char: {"contours": [[[x, y], ...]], "advance": int, "lsb": int}} -> a TrueType file."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    order = [".notdef", "space"] + [_glyph_name(c) for c in glyphs if c != " "]
    fb = FontBuilder(UPM, isTTF=True)
    fb.setupGlyphOrder(order)
    cmap = {32: "space"}
    glyf, metrics = {".notdef": TTGlyphPen(None).glyph(), "space": TTGlyphPen(None).glyph()}, {".notdef": (500, 0)}
    metrics["space"] = (int(glyphs.get(" ", {}).get("advance", 300)), 0)
    for ch, g in glyphs.items():
        if ch == " ":
            continue
        pen = TTGlyphPen(None)
        for poly in g["contours"]:
            pen.moveTo(tuple(poly[0]))
            for pt in poly[1:]:
                pen.lineTo(tuple(pt))
            pen.closePath()
        name = _glyph_name(ch)
        glyf[name] = pen.glyph()
        metrics[name] = (int(g["advance"]), int(g["lsb"]))
        cmap[ord(ch)] = name
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyf)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=CAP + 100, descent=-(UPM - CAP - 100))
    fb.setupNameTable({"familyName": family, "styleName": "Regular", "uniqueFontIdentifier": f"{family} traced",
                       "fullName": family, "psName": re.sub(r"[^A-Za-z0-9]", "", family) or "Traced"})
    fb.setupOS2(sTypoAscender=CAP + 100, sTypoDescender=-(UPM - CAP - 100), sTypoLineGap=0,
                usWinAscent=CAP + 100, usWinDescent=UPM - CAP - 100, sCapHeight=CAP)
    fb.setupPost()
    out.parent.mkdir(parents=True, exist_ok=True)
    fb.save(str(out))


def trace_line(mask: np.ndarray, text: str) -> tuple[dict, dict]:
    """Trace one caption line: binary mask (ink = True) + the characters it shows, in order.
    Returns ({char: glyph}, report). Cap height = the tallest run of the line; the baseline = the
    lowest ink of the line; side bearings = half the median gap between letters; the space advance
    = the median gap between words (when the text has one)."""
    m = mask > 0.5
    ys = np.where(m.any(axis=1))[0]
    if len(ys) == 0:
        raise ValueError("no ink in the box")
    runs, how = segment_glyphs(m, text)
    chars = [c for c in text if not c.isspace()]
    if len(runs) != len(chars):
        raise ValueError(f"segmentation: {len(runs)} ink runs for {len(chars)} characters ({how}); tighten --box, use --minus on neighbours, or pick a cleaner frame")
    heights = []
    for x0, x1 in runs:
        ry = np.where(m[:, x0:x1].any(axis=1))[0]
        heights.append((int(ry.min()), int(ry.max())))
    cap_px = max(b - a + 1 for a, b in heights)
    base_y = max(b for _, b in heights) + 1
    scale = CAP / float(cap_px)
    # letter gaps inside words / word gaps between words
    letter_gaps, word_gaps = [], []
    k = 0
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        if k > 0:
            gap = runs[k][0] - runs[k - 1][1]
            (word_gaps if (i > 0 and text[i - 1].isspace()) else letter_gaps).append(gap)
        k += 1
    sb = int(round((float(np.median(letter_gaps)) if letter_gaps else 0.08 * cap_px) * scale / 2.0))
    space_adv = int(round(float(np.median(word_gaps)) * scale)) if word_gaps else int(0.3 * UPM)
    glyphs = {}
    for (x0, x1), ch in zip(runs, chars):
        sub = m[:, x0:x1]
        contours = _contours_font_units(sub, scale, base_y, 0)
        if not contours:
            continue
        xs = [p[0] for poly in contours for p in poly]
        xmin = min(xs)
        contours = [[(x - xmin + sb, y) for x, y in poly] for poly in contours]
        width = max(p[0] for poly in contours for p in poly)
        g = {"contours": [[list(p) for p in poly] for poly in contours], "advance": int(width + sb), "lsb": sb,
             "ink_px": [int(x0), int(x1)], "cap_px": cap_px}
        if ch in glyphs:
            continue        # a repeated letter keeps its first instance
        glyphs[ch] = g
    glyphs[" "] = {"contours": [], "advance": space_adv, "lsb": 0}
    rep = {"runs": len(runs), "segmentation": how, "cap_px": cap_px, "baseline_px": base_y, "scale": round(scale, 4),
           "side_bearing_units": sb, "space_advance_units": space_adv, "chars": [c for c in chars]}
    return glyphs, rep


def _sheet(mask: np.ndarray, glyphs: dict, new_chars: List[str], font_path: Path, out: Path) -> None:
    """Reference ink (top) vs the built face set at the same cap height (bottom), one glyph per column."""
    from PIL import ImageDraw
    cells = [c for c in new_chars if c in glyphs and c != " "]
    if not cells:
        return
    cap_px = int(glyphs[cells[0]]["cap_px"])
    H = cap_px + 20
    tiles = []
    for ch in cells:
        x0, x1 = glyphs[ch]["ink_px"]
        ref = Image.fromarray((255 - (mask[:, x0:x1] > 0.5) * 255).astype("uint8")).convert("RGB")
        f = ImageFont.truetype(str(font_path), int(round(cap_px * UPM / CAP)))
        bb = f.getbbox(ch)
        ours = Image.new("RGB", (max(1, bb[2] - bb[0] + 4), max(1, bb[3] - bb[1] + 4)), "white")
        ImageDraw.Draw(ours).text((2 - bb[0], 2 - bb[1]), ch, font=f, fill="black")
        w = max(ref.width, ours.width) + 12
        tile = Image.new("RGB", (w, 2 * H + 30), (200, 200, 200))
        tile.paste(ref, (6, 10)); tile.paste(ours, (6, H + 20))
        tiles.append(tile)
    sheet = Image.new("RGB", (sum(t.width for t in tiles), tiles[0].height), (200, 200, 200))
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0)); x += t.width
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)


def cmd_trace(a) -> int:
    out = Path(os.path.expanduser(a.out))
    side = out.with_suffix(out.suffix + ".glyphs.json")
    have = json.loads(side.read_text()) if side.exists() else {"family": a.name or out.stem, "glyphs": {}, "sources": []}
    family = a.name or have.get("family") or out.stem
    if a.need is not None and not a.text:
        missing = sorted({c for c in a.need if not c.isspace() and c not in have["glyphs"]})
        print(json.dumps({"font": str(out), "glyphs": sorted(c for c in have["glyphs"] if c != " "), "missing": missing}))
        return 0 if not missing else 3
    if not (a.frame and a.box and a.ink and a.text):
        print(json.dumps({"ok": False, "why": "trace needs --frame --box --ink --text (or --need alone to list missing glyphs)"})); return 2
    frame = Image.open(a.frame).convert("RGB")
    box, ink = _ints(a.box), _ints(a.ink)
    mask = ink_mask(frame, box, ink, [_ints(b) for b in a.minus or []], pad=4)
    try:
        glyphs, rep = trace_line(mask, a.text)
    except ValueError as e:
        print(json.dumps({"ok": False, "why": str(e)})); return 2
    new_chars = [c for c in glyphs if c not in have["glyphs"] or (a.replace and c != " ")]
    for c in new_chars:
        have["glyphs"][c] = glyphs[c]
    if " " not in have["glyphs"] or a.replace:
        have["glyphs"][" "] = glyphs[" "]
    have["family"] = family
    have["sources"].append({"frame": str(a.frame), "box": box, "text": a.text, "chars": new_chars, "report": rep, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    build_ttf(have["glyphs"], out, family)
    side.write_text(json.dumps(have, indent=0))
    sheet = out.with_suffix(out.suffix + ".sheet.png")
    try:
        _sheet(mask, {c: have["glyphs"][c] for c in new_chars if c in have["glyphs"]} | {c: glyphs[c] for c in new_chars if c in glyphs}, new_chars, out, sheet)
    except Exception as e:      # the sheet is for eyes; a sheet failure never loses the font
        sheet = f"sheet failed: {e}"
    missing = sorted({c for c in (a.need or "") if not c.isspace() and c not in have["glyphs"]})
    print(json.dumps({"ok": True, "font": str(out), "family": family, "added": new_chars, "report": rep,
                      "glyphs_total": sorted(c for c in have["glyphs"] if c != " "), "missing_for_need": missing, "sheet": str(sheet),
                      "read_it": "open the sheet: top row is the reference ink, bottom row the built face at the same cap height; then `fit` on a frame you did not trace from"}, indent=1))
    return 0


def _full_mask(frame: Image.Image, box, ink, minus) -> np.ndarray:
    """ink_mask on the full canvas (so fitted coordinates are frame coordinates)."""
    a = np.asarray(frame.convert("RGB")).astype(np.float32)
    h, w = a.shape[:2]
    out = np.zeros((h, w), np.float32)
    pad = 24
    x0, y0 = max(0, box[0] - pad), max(0, box[1] - pad)
    m = ink_mask(frame, box, ink, minus, pad=pad)
    out[y0:y0 + m.shape[0], x0:x0 + m.shape[1]] = m
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="onetoone.faceid", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ink-crop"); p.add_argument("--frame", required=True); p.add_argument("--box", required=True)
    p.add_argument("--ink", required=True); p.add_argument("--minus", action="append"); p.add_argument("-o", "--out", required=True)
    p.set_defaults(fn=cmd_ink_crop)
    p = sub.add_parser("identify"); p.add_argument("crop"); p.set_defaults(fn=cmd_identify)
    p = sub.add_parser("find"); p.add_argument("name"); p.set_defaults(fn=cmd_find)
    p = sub.add_parser("fit"); p.add_argument("--frame", required=True); p.add_argument("--box", required=True)
    p.add_argument("--ink", required=True); p.add_argument("--text", required=True); p.add_argument("--face", required=True)
    p.add_argument("--minus", action="append"); p.add_argument("-o", "--out", required=True)
    p.set_defaults(fn=cmd_fit)
    p = sub.add_parser("trace", help="analysis aid: trace a rough face from the reference's own ink")
    p.add_argument("--frame"); p.add_argument("--box"); p.add_argument("--ink"); p.add_argument("--text")
    p.add_argument("--minus", action="append"); p.add_argument("-o", "--out", required=True); p.add_argument("--name", default=None)
    p.add_argument("--need", default=None, help="all caption text this face must set; prints what is still missing")
    p.add_argument("--replace", action="store_true", help="re-trace characters the font already has")
    p.set_defaults(fn=cmd_trace)
    a = ap.parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
