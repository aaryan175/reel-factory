#!/usr/bin/env python3
"""onetoone.captions_typeset — TYPESET (never trace) captions to the reference's measured geometry.

Doctrine: typeset-never-trace. Every part of a caption state is set in a real
font, sized so its ink fills the reference's measured bbox_settled, and placed so the ink
aligns to that box. Ink is solid (no halo/glow/bloom). Ornate script is drawn FIRST (behind);
the plain line is drawn LAST (on top) so it always stays readable — that is how the
reference reads ("<line A>" over the "<Script A>" swash), and it is the collision the v004 build
introduced by filling the ornate box with a solid block on top of the plain line.

Usage (library):
    layer = typeset_state(state, faces, canvas=(1916, 1078))   # RGBA PIL image
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

# Reference faces: plain = heavy regular-width neo-grotesque;
# ornate = formal roundhand. Fonts must be installed locally, e.g. Helvetica Neue Bold (REGULAR
# width, TTC idx 1 — not idx 9 Condensed Black, which reads too narrow) and a formal roundhand
# such as Snell Roundhand Black (commercial copperplate faces are not assumed to be installed).
DEFAULT_FACES: Dict[str, Tuple[str, int]] = {
    "plain": ("/System/Library/Fonts/HelveticaNeue.ttc", 1),
    # ORNATE = Pinyon Script Regular.
    #   Licence: SIL Open Font License 1.1 (OFL) — free for this use, redistributable.
    #   Source:  https://github.com/google/fonts/tree/main/ofl/pinyonscript
    #            raw TTF https://raw.githubusercontent.com/google/fonts/main/ofl/pinyonscript/PinyonScript-Regular.ttf
    #            keep the licence copy beside the font (PinyonScript-OFL.txt). Override the path
    #            with REEL_FACTORY_ORNATE_FONT.
    # WHY: the reference's script is a formal copperplate. 26 free script faces were
    # typeset to the reference's own bboxes and compared against the reference crops (sheets in
    # <project>/render/font/). Pinyon is the only candidate that carries ALL of the
    # reference's discriminating traits at once: a giant open capital whose entry stroke is a THIN
    # crossing loop over the lowercase, extreme thick/thin contrast (hairline joins into thick
    # downstrokes), a small lowercase under a tall capital, and a left-sweeping under-swash on D.
    # Numbers vs the reference ink masks (mean of c02/c03/c04b/c06; c04a excluded, its mask is
    # contaminated by the building bed and the brain already flags that bbox LOW confidence):
    #   ink coverage  Pinyon  mean |err| 13.3%, signed +1.6%   |  Snell 17.6%, signed -16.9%
    #   stroke width  Pinyon  mean |err|  5.7%, signed +1.3%   |  Snell  7.5%, signed  -7.5%
    #   body-band ratio (lowercase height / block height) Pinyon matches to <=0.045, Snell <=0.060
    # Snell was systematically ~17% UNDER-inked and its capitals lack the crossing loop entirely —
    # that is why the capitals looked visibly wrong. No erode/dilate is
    # applied: Pinyon's signed stroke error is +1.3%, i.e. already at the reference's weight.
    # Runners-up: ImperialScript (right capital construction, but body ratio 0.49-0.56 vs the
    # reference's 0.32-0.43 — lowercase far too big), GreatVibes (capital shape close, strokes
    # +30% fat), Allura/Ephesis (+18-23% fat, capitals too soft), LoversQuarrel (too upright,
    # slant -15deg). Rejected outright: DrSugiyama/StyleScript/Carattere/Qwigley (brush-heavy),
    # MonsieurLaDoulaise/MissFajardose/Tangerine/LavishlyYours (35-50% under-inked).
    # KNOWN GAP: the reference face carries ball/teardrop terminals and an internal spiral in its
    # L and D, and tracks the lowercase noticeably wider than any installed face sets it. Those
    # are swash alternates; this PIL build has no Raqm, so OpenType `swsh`/`salt` is unreachable.
    "ornate": (os.path.expanduser(os.environ.get("REEL_FACTORY_ORNATE_FONT") or "~/Library/Fonts/PinyonScript-Regular.ttf"), 0),
}

_PART_FACE = {"plain_line": "plain", "plain": "plain", "ornate_word": "ornate", "ornate": "ornate"}


def _font(face: Tuple[str, int], size: int) -> ImageFont.FreeTypeFont:
    path, index = face
    return ImageFont.truetype(path, size, index=index)


def _ink_bbox(font: ImageFont.FreeTypeFont, text: str) -> Tuple[int, int, int, int]:
    """Tight ink bbox (x0, y0, x1, y1) of `text` at origin, from the glyph outlines."""
    return font.getbbox(text)


def fit_font_to_box(face: Tuple[str, int], text: str, box_w: int, box_h: int,
                    *, lo: int = 8, hi: int = 600) -> Tuple[ImageFont.FreeTypeFont, Tuple[int, int, int, int]]:
    """Largest size whose INK height <= box_h and ink width <= box_w (binary search).

    The reference boxes are measured ink extents, so we fit ink-to-ink, never em-to-box.
    """
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _font(face, mid)
        x0, y0, x1, y1 = _ink_bbox(f, text)
        w, h = x1 - x0, y1 - y0
        if w <= box_w and h <= box_h:
            best = (f, (x0, y0, x1, y1))
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        f = _font(face, lo)
        best = (f, _ink_bbox(f, text))
    return best


SCRIPT_SQUEEZE_MIN = 0.90
SCRIPT_STRETCH_MAX = 1.15
SCRIPT_OVERFLOW = 1.08


def fit_font_to_height(face: Tuple[str, int], text: str, box_h: int, *, lo: int = 8, hi: int = 600):
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _font(face, mid)
        bb = _ink_bbox(f, text)
        if bb[3] - bb[1] <= box_h:
            best = (f, bb); lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        f = _font(face, lo); best = (f, _ink_bbox(f, text))
    return best


def fit_script(face: Tuple[str, int], text: str, box_w: int, box_h: int):
    """(font, ink bbox at that size, horizontal factor). Height first, then a bounded
    horizontal factor toward the box width; if even the max squeeze overflows the box by
    more than SCRIPT_OVERFLOW, shrink the size until it fits at the max squeeze."""
    font, bb = fit_font_to_height(face, text, box_h)
    natural_w = bb[2] - bb[0]
    factor = max(SCRIPT_SQUEEZE_MIN, min(SCRIPT_STRETCH_MAX, box_w / max(natural_w, 1)))
    if natural_w * factor > box_w * SCRIPT_OVERFLOW:
        factor = SCRIPT_SQUEEZE_MIN
        limit = box_w * SCRIPT_OVERFLOW / factor
        lo, hi, best = 8, font.size, None
        while lo <= hi:
            mid = (lo + hi) // 2
            f = _font(face, mid); b = _ink_bbox(f, text)
            if b[2] - b[0] <= limit:
                best = (f, b); lo = mid + 1
            else:
                hi = mid - 1
        if best:
            font, bb = best
    return font, bb, factor


def _draw_text(layer: Image.Image, xy: Tuple[int, int], text: str, font: ImageFont.FreeTypeFont,
               fill: Tuple[int, int, int, int], stretch: float, ink: Tuple[int, int, int, int]) -> None:
    """Draw text, optionally scaled horizontally by `stretch` about its ink origin."""
    if abs(stretch - 1.0) < 0.005:
        ImageDraw.Draw(layer).text(xy, text, font=font, fill=fill); return
    ix0, iy0, ix1, iy1 = ink
    w, h = ix1 - ix0, iy1 - iy0
    tmp = Image.new("RGBA", (w + 4, h + 4), (0, 0, 0, 0))
    ImageDraw.Draw(tmp).text((2 - ix0, 2 - iy0), text, font=font, fill=fill)
    new_w = max(1, int(round((w + 4) * stretch)))
    tmp = tmp.resize((new_w, h + 4), Image.LANCZOS)
    layer.alpha_composite(tmp, (int(round(xy[0] + ix0 - 2 * stretch)), xy[1] + iy0 - 2))


def _part_box(part: Mapping[str, Any]):
    box = part.get("bbox_settled") or part.get("bbox_settled_approx")
    return [int(v) for v in box] if box else None


REF_FIT_MIN_SCORE = 0.6


def _paste_ink(layer: Image.Image, glyphs: Image.Image, ink_xy: Sequence[float], stretch: float = 1.0):
    """Composite `glyphs` (any canvas) so its tight ink bbox lands with its top-left at ink_xy."""
    bb = glyphs.getbbox()
    if bb is None:
        return None
    crop = glyphs.crop(bb)
    if abs(stretch - 1.0) > 0.004:
        crop = crop.resize((max(1, int(round(crop.size[0] * stretch))), crop.size[1]), Image.LANCZOS)
    x, y = int(round(ink_xy[0])), int(round(ink_xy[1]))
    tmp = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    tmp.paste(crop, (x, y))
    layer.alpha_composite(tmp)
    return [x, y, x + crop.size[0], y + crop.size[1]]


def _typeset_ref_fit(layer: Image.Image, part: Mapping[str, Any], kind: str, face: Tuple[str, int],
                     text: str, fill: Tuple[int, int, int, int]) -> Dict[str, Any] | None:
    """REFERENCE-PIXEL FIT. When the study
    carries `ref_fit` (onetoone.refit), the part is typeset with exactly those parameters:
      plain   size + tracking_em + space_k at draw origin xy   (tight heavy grotesque), or a
              RAMP line;
      script  TWO runs: the giant capital and the lowercase tail, each with its own size, from
              the reference's measured capital box and x-height/baseline (`ref_script_metrics`).
    Box fitting stays as the fallback for studies that have no fit."""
    fit = part.get("ref_fit")
    if not fit:
        return None
    from onetoone.refit import draw_ramped, draw_tracked
    big = (layer.size[0] * 2, layer.size[1] * 2)
    off = (layer.size[0] // 2, layer.size[1] // 2)
    if fit.get("two_run"):
        boxes = []
        for run, run_text in (("capital", text[0]), ("tail", text[1:])):
            r = fit[run]
            g = draw_tracked(big, off, run_text, _font(face, int(r["size"])), fill, float(r.get("tracking_em", 0.0)) * int(r["size"]))
            b = _paste_ink(layer, g, r["ink_xy"], float(r.get("stretch", 1.0)))
            if b:
                boxes.append(b)
        if not boxes:
            return None
        ink_box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        return {"part": part.get("part"), "text": text, "face": kind, "size": int(fit["tail"]["size"]),
                "stretch": float(fit["capital"].get("stretch", 1.0)), "box": list(_part_box(part)), "ink": ink_box,
                "fit": "ref_script_metrics"}
    if float(fit.get("score", 0.0)) < REF_FIT_MIN_SCORE:
        return None
    if fit.get("ramp"):
        g = draw_ramped(layer.size, tuple(fit["xy"]), text, face, fill, int(fit["size_l"]), int(fit["size_r"]),
                        float(fit["tracking_em"]), float(fit.get("space_k", 1.0)))
        size = int(fit["size_r"])
    else:
        size = int(fit["size"])
        g = draw_tracked(layer.size, tuple(fit["xy"]), text, _font(face, size), fill,
                         float(fit["tracking_em"]) * size, float(fit.get("space_k", 1.0)))
    stretch = float(fit.get("stretch", 1.0))
    if abs(stretch - 1.0) > 0.004:
        # horizontal squeeze about the ink's LEFT edge (the fit's ink_box[0]): the reference's
        # grotesque is narrower for its height than ours
        bb = _paste_ink(layer, g, fit["ink_box"][:2], stretch)
    else:
        layer.alpha_composite(g)
        bb = g.getbbox()
    return {"part": part.get("part"), "text": text, "face": kind, "size": size, "stretch": stretch,
            "box": list(_part_box(part)), "ink": list(bb) if bb else list(fit["ink_box"]),
            "fit": "ref_pixels", "tracking_em": fit.get("tracking_em"), "score": fit.get("score")}


def typeset_part(layer: Image.Image, part: Mapping[str, Any],
                 faces: Mapping[str, Tuple[str, int]],
                 plain_box: Sequence[int] | None = None) -> Dict[str, Any]:
    """Draw one part onto `layer` (RGBA canvas). Returns the placement report."""
    kind = _PART_FACE.get(str(part.get("part")), "plain")
    face = faces[kind]
    # REFERENCE-IDENTIFIED FACE. The default ornate face is the winner of one reference's
    # bake-off; a different reference sets its script in a different face (identified from its
    # own pixels) and no amount of fitting turns one face into another. When the study names the face for a part it wins: [path, ttc index].
    if part.get("face_file"):
        import os
        ff = part["face_file"]
        face = (os.path.expanduser(str(ff[0])), int(ff[1]) if len(ff) > 1 else 0)
    text = str(part.get("text") or " ".join(part.get("words") or []))
    # Geometry: the study records an exact box where the mask was clean, and an *_approx box
    # (eye-measured off a gridded crop) where the bed contaminated the mask (e.g. c04a "<Script D>"
    # over shot S08 (bright exterior)). Approx is still the reference's geometry — use it, never skip it.
    box = part.get("bbox_settled") or part.get("bbox_settled_approx")
    if not box:
        raise KeyError(f"{part.get('part')} {text!r}: no bbox_settled / bbox_settled_approx")
    bx0, by0, bx1, by1 = [int(v) for v in box]
    box_w, box_h = bx1 - bx0, by1 - by0
    stretch = 1.0
    ink = part.get("ink_rgb") or part.get("ink_rgb_approx") or (255, 255, 255)
    fitted = _typeset_ref_fit(layer, part, kind, face, text, tuple(int(c) for c in ink) + (255,))
    if fitted is not None:
        return fitted
    if kind == "ornate":
        # SCRIPT FIT: the reference's signature is the giant capital. A plain
        # ink-to-box fit is width-bound for <Script B>/<Script E>/<Script C> (Pinyon sets the word 12–23% wider
        # than the reference at capital height) and shrinks the capital. So: fit the HEIGHT
        # (capital matches the box), then squeeze horizontally down to 0.90 and allow ≤8% box
        # overflow before backing the size off. A connected script survives a mild squeeze;
        # letter-spacing would break its joins. c04a "<Script D>" wants a stretch — capped at 1.15.
        font, (ix0, iy0, ix1, iy1), stretch = fit_script(face, text, box_w, box_h)
    else:
        font, (ix0, iy0, ix1, iy1) = fit_font_to_box(face, text, box_w, box_h)
    ink_w, ink_h = int(round((ix1 - ix0) * stretch)), iy1 - iy0
    # Align ink to the measured box. Plain lines: left-anchored (the reference's D5 scale-down
    # holds a fixed left edge), vertically centred. Ornate words: centred horizontally but
    # BOTTOM-anchored — the reference's script sits low in its swash block (baseline at the box
    # bottom, giant capital reaching up). Centering the script vertically pushed its lowercase
    # up into the plain line: that was the "<Script A>"-over-"<line A>" collision.
    if kind == "plain":
        x = bx0 - ix0
        y = by0 + (box_h - ink_h) // 2 - iy0
    else:
        x = bx0 + (box_w - ink_w) // 2 - ix0
        # Anchor the script AWAY from the plain line. The relation flips per state: c02's
        # "<line A>" sits ABOVE "<Script A>" (so the script hugs its box bottom), c06's "<line C>"
        # sits BELOW "<Script C>" (so the script must hug its box top or it lands on the line —
        # the c06 collision). No plain partner → bottom (the common case).
        plain_above = plain_box is not None and (plain_box[1] + plain_box[3]) / 2 < (by0 + by1) / 2
        if plain_box is None or plain_above:
            y = by1 - ink_h - iy0
        else:
            y = by0 - iy0
        # ENVELOPE MATCH: when the study carries the reference's
        # measured ink envelope for this word (onetoone.ornate_env: 5th/95th percentile ink rows
        # inside the ornate box minus the plain line's box), size and place OUR ink to the same
        # envelope in the same mask. Box-fitting put Pinyon's body 26px low on "<Script C>" (its tails
        # are shorter than the reference face's, so the height fit inflated the body) and filled
        # the "<Script B>" box where the reference sits high. Two passes: span → size, centre → y.
        env = part.get("ref_ink_env")
        if env and env[1] - env[0] >= 20:
            from onetoone.ornate_env import env_mask, layer_ink_rows, row_envelope
            mask = env_mask((bx0, by0, bx1, by1), plain_box, layer.size)
            rgb_probe = (255, 255, 255, 255)
            for _pass in range(2):
                tmp = Image.new("RGBA", layer.size, (0, 0, 0, 0))
                _draw_text(tmp, (x, y), text, font, rgb_probe, stretch, (ix0, iy0, ix1, iy1))
                ours = row_envelope(layer_ink_rows(tmp, mask))
                if not ours or ours[1] - ours[0] < 4:
                    break
                scale = (env[1] - env[0]) / (ours[1] - ours[0])
                if abs(scale - 1.0) > 0.02:
                    font = _font(face, max(8, int(round(font.size * scale))))
                    ix0, iy0, ix1, iy1 = _ink_bbox(font, text)
                    natural_w = ix1 - ix0
                    stretch = max(SCRIPT_SQUEEZE_MIN, min(SCRIPT_STRETCH_MAX, box_w / max(natural_w, 1)))
                    ink_w, ink_h = int(round(natural_w * stretch)), iy1 - iy0
                    x = bx0 + (box_w - ink_w) // 2 - ix0
                    tmp = Image.new("RGBA", layer.size, (0, 0, 0, 0))
                    _draw_text(tmp, (x, y), text, font, rgb_probe, stretch, (ix0, iy0, ix1, iy1))
                    ours = row_envelope(layer_ink_rows(tmp, mask)) or ours
                y += int(round((env[0] + env[1]) / 2 - (ours[0] + ours[1]) / 2))
    # Ink: exact where measured, *_approx where eye-read. Some states are DARK ink on a light
    # bed (c04a/c04b "<Script D>"/"<line D>" navy over the building) — defaulting to white would render
    # them invisible, which is exactly a "captions are off" reject. Only fall back to white
    # when the study recorded no ink at all.
    ink = part.get("ink_rgb") or part.get("ink_rgb_approx") or (255, 255, 255)
    rgb = tuple(int(c) for c in ink)
    _draw_text(layer, (x, y), text, font, rgb + (255,), stretch, (ix0, iy0, ix1, iy1))
    return {"part": part.get("part"), "text": text, "face": kind, "size": font.size, "stretch": round(stretch, 3),
            "box": [bx0, by0, bx1, by1], "ink": [x + ix0, y + iy0, x + ix0 + ink_w, y + iy1]}


def typeset_state(state: Mapping[str, Any], faces: Mapping[str, Tuple[str, int]] | None = None,
                  *, canvas: Tuple[int, int] = (1916, 1078)) -> Tuple[Image.Image, Dict[str, Any]]:
    """Render one caption state to a full-canvas RGBA layer. Returns (layer, report)."""
    faces = dict(DEFAULT_FACES, **(faces or {}))
    layer = Image.new("RGBA", canvas, (0, 0, 0, 0))

    parts = list(state.get("parts", []))
    # z-order: ornate first (behind), plain last (on top) — keeps the plain line readable.
    ordered = sorted(parts, key=lambda p: 0 if _PART_FACE.get(str(p.get("part"))) == "ornate" else 1)
    plain_boxes = [_part_box(p) for p in parts if _PART_FACE.get(str(p.get("part"))) == "plain"]
    plain_box = plain_boxes[0] if plain_boxes else None
    report: Dict[str, Any] = {"id": state.get("id"), "parts": []}
    part_layers = []
    for p in ordered:
        pl = Image.new("RGBA", canvas, (0, 0, 0, 0))
        report["parts"].append(typeset_part(pl, p, faces, plain_box=plain_box))
        part_layers.append((p, pl))
        layer.alpha_composite(pl)
    report["own_ink"] = own_ink_collision(part_layers)
    return layer, report


# OWN-INK COLLISION: the readability gate compared ink with the BED only, so
# the plain word "<line D>" typeset inside the script word "<Script D>" passed while being unreadable.
# Share of the plain line's ink that lies on (or within 2 px of) the script word's ink. The
# reference itself lets a hairline cross a plain word, so a small share is normal.
MAX_OWN_INK_OVERLAP = 0.22


def own_ink_collision(part_layers) -> Dict[str, Any]:
    import numpy as np
    from PIL import ImageFilter
    plain = [l for p, l in part_layers if _PART_FACE.get(str(p.get("part"))) == "plain"]
    ornate = [l for p, l in part_layers if _PART_FACE.get(str(p.get("part"))) == "ornate"]
    if not plain or not ornate:
        return {"ok": True, "overlap": 0.0}
    pa = np.asarray(plain[0].split()[3]) > 96
    oa = np.asarray(ornate[0].split()[3].filter(ImageFilter.MaxFilter(5))) > 96
    share = float((pa & oa).sum()) / max(1, int(pa.sum()))
    return {"ok": share <= MAX_OWN_INK_OVERLAP, "overlap": round(share, 3), "max": MAX_OWN_INK_OVERLAP}


# Readability is judged RELATIVE to the reference, never to a fixed number. Found on
# a reference: c04a sets navy "<Script D>" on pale sky (reference contrast ≈173) and our shot put
# the same box over a dark background (ours 31) → invisible. But c05c "<word E>" is tan on peach sky
# in the reference itself (contrast ≈58) — a fixed floor of 60 would wrongly flag the reference's
# own style. So: ours must keep ≥ MIN_CONTRAST_RATIO of the reference's contrast (from the
# study's local_bed_rgb, else the reference frame), and never drop under MIN_CONTRAST_ABS.
# The ink colour is the study's; the BED under it is a casting fact — check it on OUR frame.
MIN_CONTRAST_RATIO = 0.6
MIN_CONTRAST_ABS = 25.0
HALO_MIN_RATIO = 3.0   # WCAG ratio, ink vs the 4-8 px band outside the glyphs on the shadow-composited bed
# Clutter: the reference keeps clear sky under "<word E>"; ours ran the word across the
# subject's face. Texture (luma stddev) under the ink box, ours vs the reference frame.
MAX_CLUTTER_RATIO = 2.0
MIN_CLUTTER_ABS = 25.0


def _luma(rgb: Sequence[float]) -> float:
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def readability(bed: Image.Image, report: Mapping[str, Any], state: Mapping[str, Any],
                *, ref_bed: Image.Image | None = None, ink_layer: Image.Image | None = None,
                shaded_bed: Image.Image | None = None) -> Dict[str, Any]:
    """Contrast + clutter of every typeset part against OUR bed, relative to the reference.

    `report` is typeset_state()'s report (ink boxes in canvas pixels); `bed` is the frame the
    layer sits on; `ref_bed` (optional) is the reference's frame at the same index, used for the
    clutter comparison and as the contrast baseline when the study recorded no local_bed_rgb.
    """
    from PIL import ImageStat
    parts_by_text = {str(p.get("text") or " ".join(p.get("words") or [])): p for p in state.get("parts", [])}
    out = []
    for rep in report["parts"]:
        p = parts_by_text.get(rep["text"], {})
        ink = p.get("ink_rgb") or p.get("ink_rgb_approx") or (255, 255, 255)
        ink_L = _luma(ink)
        x0, y0, x1, y1 = [max(0, int(v)) for v in rep["ink"]]
        box = (x0, y0, max(x0 + 1, x1), max(y0 + 1, y1))
        # CLUTTER is a property of the footage: always the RAW bed under the word box.
        raw = ImageStat.Stat(bed.convert("L").crop(box))
        bed_L, bed_sd = raw.mean[0], raw.stddev[0]
        box_L = bed_L
        # CONTRAST, when the caption carries a shadow (`ink_layer` + `shaded_bed` = our frame with
        # this caption's shadow composited): the WCAG relative-luminance ratio of the ink against a
        # BAND 4-8 px outside the glyphs. History: the old box mean ignored the shadow, so
        # white on a white sky (1.09:1) could only ship by bypass flag. The first fix
        # took the mean of a ring touching the ink, where an opaque shadow is near-black whatever the
        # footage does, and compared it with a shadow-free reference need: every state cleared by
        # 1.3x-9x and the gate stopped discriminating. The 4-8 px band is the
        # independent auditors' method: it fell 8.19 -> 3.43 between the best and the weakest settled
        # state of v019 and failed 9 states of v018. HALO_MIN_RATIO is the must-pass floor.
        halo = None
        if ink_layer is not None and shaded_bed is not None:
            import numpy as np
            from PIL import ImageFilter, ImageChops
            pad = 12
            rbox = (max(0, box[0] - pad), max(0, box[1] - pad), min(bed.size[0], box[2] + pad), min(bed.size[1], box[3] + pad))
            core = ink_layer.getchannel("A").crop(rbox).point(lambda v: 255 if v >= 64 else 0)
            band = ImageChops.subtract(core.filter(ImageFilter.MaxFilter(17)), core.filter(ImageFilter.MaxFilter(9)))
            m = np.asarray(band) > 0
            if m.any():
                def _lin(u):
                    u = u / 255.0
                    return np.where(u <= 0.04045, u / 12.92, ((u + 0.055) / 1.055) ** 2.4)
                rgb = np.asarray(shaded_bed.convert("RGB").crop(rbox), dtype=np.float64)[m]
                Lb = float((0.2126 * _lin(rgb[:, 0]) + 0.7152 * _lin(rgb[:, 1]) + 0.0722 * _lin(rgb[:, 2])).mean())
                ik = np.asarray(ink[:3], dtype=np.float64)
                Li = float(0.2126 * _lin(ik[0]) + 0.7152 * _lin(ik[1]) + 0.0722 * _lin(ik[2]))
                hi, lo = max(Li, Lb), min(Li, Lb)
                halo = (hi + 0.05) / (lo + 0.05)
        c = abs(ink_L - bed_L)
        ref_L = ref_sd = None
        if p.get("local_bed_rgb"):
            ref_L = _luma(p["local_bed_rgb"])
        if ref_bed is not None:
            rs = ImageStat.Stat(ref_bed.convert("L").crop(box))
            ref_sd = rs.stddev[0]
            if ref_L is None:
                ref_L = rs.mean[0]
        ref_c = abs(ink_L - ref_L) if ref_L is not None else None
        need = max(MIN_CONTRAST_ABS, MIN_CONTRAST_RATIO * ref_c) if ref_c is not None else MIN_CONTRAST_ABS
        contrast_ok = (halo >= HALO_MIN_RATIO) if halo is not None else (c >= need)
        clutter_ok = True if ref_sd is None else not (bed_sd > MAX_CLUTTER_RATIO * ref_sd and bed_sd > MIN_CLUTTER_ABS)
        out.append({"text": rep["text"], "ink_L": round(ink_L, 1), "bed_L": round(bed_L, 1), "contrast": round(c, 1),
                    "contrast_box_raw": round(abs(ink_L - box_L), 1),
                    "halo_ratio": None if halo is None else round(halo, 2), "halo_min": HALO_MIN_RATIO,
                    "ref_contrast": None if ref_c is None else round(ref_c, 1), "need": round(need, 1),
                    "contrast_ok": contrast_ok, "bed_sd": round(bed_sd, 1),
                    "ref_bed_sd": None if ref_sd is None else round(ref_sd, 1), "clutter_ok": clutter_ok,
                    "ok": contrast_ok and clutter_ok})
    return {"ok": all(o["ok"] for o in out), "parts": out}


def load_states(captions_plaintext: Path) -> Sequence[Mapping[str, Any]]:
    data = json.loads(Path(captions_plaintext).read_text())
    return data["states"]


if __name__ == "__main__":  # smoke: typeset every state of a brain to PNGs
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("captions_plaintext")
    ap.add_argument("outdir")
    a = ap.parse_args()
    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    for st in load_states(Path(a.captions_plaintext)):
        layer, rep = typeset_state(st)
        layer.save(out / f"{st['id']}.png")
        print(json.dumps(rep))
