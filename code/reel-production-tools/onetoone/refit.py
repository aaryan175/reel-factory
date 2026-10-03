#!/usr/bin/env python3
"""onetoone.refit — fit OUR typeset ink to the REFERENCE's pixels (size, tracking, stretch, x, y).

Why: box fitting was width-bound, so the plain face came
out 16–33% short and loosely tracked against the reference's tight heavy grotesque, and the
script's placement let a plain word land inside the script word. The study boxes are
eye-read in places (c04a). The reference frame is the truth, so: build the reference's ink
mask (pixels near the part's ink colour) at the part's settled frame, render our text over a
grid of (size, tracking | stretch), and keep the candidate whose mask correlates best with the
reference mask (cv2.matchTemplate, TM_CCOEFF_NORMED, free translation near the study box).
The winner is written back into captions.plaintext.json as `ref_fit` and the typesetter uses
it verbatim. Nothing here traces the reference: it only chooses typesetting parameters.

    python3 -m onetoone.refit <brain> <ref_frames_dir>     # dir holds NNNN.png reference frames
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

MIN_SCORE = 0.45          # below this the fit is not trusted and the typesetter falls back
INK_TOL = 70.0            # max RGB distance to count a reference pixel as ink
PAD = 60                  # search window = study box grown by this many px


def face_sweep_slugs(names: Sequence[str]) -> Dict[str, str]:
    """Collision-free output-filename slugs for a face-family sweep (L0049: a per-row scratch
    script once derived its output filename from a candidate name's first two words, so
    'Helvetica Neue Bold' and 'Helvetica Neue Condensed Bold' both hashed to
    'fit_Helvetica_Neue.json' and the second run silently overwrote the first's measurement --
    no error, no warning, just a wrong number consumed downstream). Every full candidate name
    maps to a distinct slug (built from every word, not a truncated prefix); call this before a
    sweep writes anything and fail loudly if it can't, rather than silently colliding."""
    slugs: Dict[str, str] = {}
    for name in names:
        slug = "_".join(name.split())
        if slug in slugs.values():
            raise ValueError(f"face_sweep_slugs: collision on {slug!r} from candidate {name!r}")
        slugs[name] = slug
    return slugs


def tracked_layout(font: ImageFont.FreeTypeFont, text: str, tracking_px: float, space_k: float = 1.0):
    """x offset of each glyph: the font's own advances + kerning, plus tracking per gap.
    `space_k` scales the word space (the reference sets its spaces narrower than the face's)."""
    sp = font.getlength(" ") * (space_k - 1.0)
    return [font.getlength(text[:i]) + i * tracking_px + text[:i].count(" ") * sp for i in range(len(text))]


def draw_tracked(size_wh: Tuple[int, int], xy: Tuple[float, float], text: str, font, fill, tracking_px: float,
                 space_k: float = 1.0) -> Image.Image:
    layer = Image.new("RGBA", size_wh, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if abs(tracking_px) < 0.01 and abs(space_k - 1.0) < 0.01:
        d.text(xy, text, font=font, fill=fill); return layer
    for ch, off in zip(text, tracked_layout(font, text, tracking_px, space_k)):
        if ch != " ":
            d.text((xy[0] + off, xy[1]), ch, font=font, fill=fill)
    return layer


def draw_ramped(size_wh: Tuple[int, int], xy: Tuple[float, float], text: str, face: Tuple[str, int], fill,
                size_l: int, size_r: int, tracking_em: float, space_k: float = 1.0) -> Image.Image:
    """A line whose glyph size ramps linearly from `size_l` (first glyph) to `size_r` (last),
    all on ONE baseline — e.g. a reference that sets the first words of a line small and the last
    large on a shared baseline. `xy` = (left edge of the first glyph's origin, BASELINE y)."""
    layer = Image.new("RGBA", size_wh, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    n = max(1, len(text) - 1)
    x = float(xy[0])
    cache: Dict[int, ImageFont.FreeTypeFont] = {}
    for i, ch in enumerate(text):
        sz = int(round(size_l + (size_r - size_l) * i / n))
        f = cache.setdefault(sz, ImageFont.truetype(face[0], sz, index=face[1]))
        if ch != " ":
            d.text((x, xy[1]), ch, font=f, fill=fill, anchor="ls")
        adv = f.getlength(ch)
        if i + 1 < len(text):
            adv = f.getlength(text[i:i + 2]) - f.getlength(text[i + 1])      # kerned advance
        if ch == " ":
            adv *= space_k
        x += adv + tracking_em * sz
    return layer


def fit_ramp(frame: Image.Image, face: Tuple[str, int], text: str, box: Sequence[int], ink_rgb: Sequence[int]) -> Dict[str, Any]:
    W, H = frame.size
    bx0, by0, bx1, by1 = [int(v) for v in box]
    win = (max(0, bx0 - 60), max(0, by0 - 50), min(W, bx1 + 80), min(H, by1 + 50))
    ref = cv2.GaussianBlur(ref_mask(frame, ink_rgb, win), (0, 0), 1.2).astype(np.float32)
    bh = by1 - by0
    best: Dict[str, Any] = {"score": -1.0}
    for size_r in range(int(bh * 0.95), int(bh * 1.55), 3):
        for k in (0.62, 0.68, 0.74, 0.80, 0.88, 1.0):   # one measured ramp: x-height 35 (first word) -> 55 (last word)
            size_l = int(round(size_r * k))
            for tr in (-0.12, -0.10, -0.08, -0.06, -0.04):
              for sk in (1.0, 0.8, 0.6):
                for st in (0.92, 1.0):
                    lay = draw_ramped((2600, size_r * 3), (size_r, size_r * 2), text, face, (255, 255, 255, 255), size_l, size_r, tr, sk)
                    a = np.asarray(lay)[:, :, 3]
                    ys, xs = np.nonzero(a > 40)
                    if len(xs) == 0:
                        continue
                    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
                    m = a[y0:y1, x0:x1]
                    if st < 0.999:
                        m = cv2.resize(m, (max(2, int(round(m.shape[1] * st))), m.shape[0]), interpolation=cv2.INTER_AREA)
                    if m.shape[0] >= ref.shape[0] or m.shape[1] >= ref.shape[1]:
                        continue
                    res = cv2.matchTemplate(ref, cv2.GaussianBlur(m, (0, 0), 1.2).astype(np.float32), cv2.TM_CCOEFF_NORMED)
                    _, mx, _, loc = cv2.minMaxLoc(res)
                    if mx > best["score"]:
                        best = {"score": float(mx), "ramp": True, "size_l": size_l, "size_r": size_r, "tracking_em": tr, "space_k": sk, "stretch": st,
                                "xy": [float(win[0] + loc[0] + size_r - x0), float(win[1] + loc[1] + size_r * 2 - y0)],
                                "ink_box": [int(win[0] + loc[0]), int(win[1] + loc[1]), int(win[0] + loc[0] + m.shape[1]), int(win[1] + loc[1] + m.shape[0])]}
    best["score"] = round(best["score"], 3)
    return best


def render_mask(face: Tuple[str, int], text: str, size: int, *, tracking_em: float = 0.0, stretch: float = 1.0,
                space_k: float = 1.0):
    """(tight ink mask uint8 0/255, (ox, oy)) where (ox, oy) is the draw origin relative to the
    mask's top-left — so a mask found at (mx, my) means: draw the text at (mx+ox, my+oy)."""
    font = ImageFont.truetype(face[0], size, index=face[1])
    w = int(font.getlength(text) + abs(tracking_em) * size * len(text)) + size * 2
    h = size * 3
    org = (size, size)
    lay = draw_tracked((w, h), org, text, font, (255, 255, 255, 255), tracking_em * size, space_k)
    a = np.asarray(lay)[:, :, 3]
    ys, xs = np.nonzero(a > 40)
    if len(xs) == 0:
        return None, (0, 0)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    m = a[y0:y1, x0:x1]
    ox, oy = org[0] - x0, org[1] - y0
    if abs(stretch - 1.0) > 0.004:
        nw = max(2, int(round(m.shape[1] * stretch)))
        m = cv2.resize(m, (nw, m.shape[0]), interpolation=cv2.INTER_AREA)
        ox = ox * stretch
    return m, (ox, oy)


def ref_mask(frame: Image.Image, ink_rgb: Sequence[int], window: Sequence[int], minus: Optional[Sequence[int]] = None) -> np.ndarray:
    x0, y0, x1, y1 = window
    a = np.asarray(frame.convert("RGB"), dtype=np.float32)[y0:y1, x0:x1]
    d = np.sqrt(((a - np.asarray(ink_rgb, dtype=np.float32)) ** 2).sum(axis=2))
    m = np.clip((INK_TOL - d) / INK_TOL, 0, 1) * 255.0
    if minus:
        mx0, my0, mx1, my1 = [int(v) for v in minus]
        m[max(0, my0 - y0):max(0, my1 - y0), max(0, mx0 - x0):max(0, mx1 - x0)] = 0
    return m.astype(np.uint8)


def fit_part(frame: Image.Image, face: Tuple[str, int], text: str, box: Sequence[int], ink_rgb: Sequence[int],
             *, kind: str, minus: Optional[Sequence[int]] = None, window: Optional[Sequence[int]] = None,
             sizes: Optional[Sequence[int]] = None, params: Optional[Sequence[Tuple[float, float]]] = None) -> Dict[str, Any]:
    W, H = frame.size
    bx0, by0, bx1, by1 = [int(v) for v in box]
    pad_x = PAD + (bx1 - bx0) // 4
    pad_y = PAD + (by1 - by0) // 2
    win = tuple(window) if window else (max(0, bx0 - pad_x), max(0, by0 - pad_y), min(W, bx1 + pad_x), min(H, by1 + pad_y))
    ref = ref_mask(frame, ink_rgb, win, minus)
    ref_f = cv2.GaussianBlur(ref, (0, 0), 1.2).astype(np.float32)
    bh = by1 - by0
    # size seeds: the height fit gives the scale of the search
    probe = ImageFont.truetype(face[0], 200, index=face[1]).getbbox(text)
    size0 = max(10, int(round(200 * bh / max(1, probe[3] - probe[1]))))
    if sizes is not None and params is not None:
        pass
    elif kind == "plain":
        # v011c audit M1: widths matched but heights ran 5-23% short — the reference's grotesque
        # is narrower for its height than Helvetica Neue Bold. So the search also squeezes
        # horizontally (0.84-1.0) and lets the size rise to compensate.
        sizes = sorted({int(round(size0 * k)) for k in np.arange(0.62, 1.60, 0.03)})
        params = [(round(-0.20 + 0.01 * i, 2), st) for i in range(25) for st in (0.88, 0.94, 1.0)]
    else:
        sizes = sorted({int(round(size0 * k)) for k in np.arange(0.60, 1.31, 0.03)})
        params = [(0.0, s) for s in np.arange(0.84, 1.305, 0.03)]       # horizontal stretch
    best: Dict[str, Any] = {"score": -1.0}
    space_ks = (1.0, 0.85, 0.7, 0.55) if (kind == "plain" and " " in text) else (1.0,)
    for size, (tr, stv), sk in ((a, b, c) for a in sizes for b in params for c in space_ks):
        if True:
            m, (ox, oy) = render_mask(face, text, size, tracking_em=tr, stretch=float(stv), space_k=sk)
            if m is None or m.shape[0] >= ref_f.shape[0] or m.shape[1] >= ref_f.shape[1]:
                continue
            t = cv2.GaussianBlur(m, (0, 0), 1.2).astype(np.float32)
            res = cv2.matchTemplate(ref_f, t, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if mx > best["score"]:
                best = {"score": float(mx), "size": int(size), "tracking_em": round(float(tr), 3),
                        "stretch": round(float(stv), 3), "space_k": sk,
                        "xy": [float(win[0] + loc[0] + ox), float(win[1] + loc[1] + oy)],
                        "ink_box": [int(win[0] + loc[0]), int(win[1] + loc[1]),
                                    int(win[0] + loc[0] + m.shape[1]), int(win[1] + loc[1] + m.shape[0])]}
    best["score"] = round(best["score"], 3)
    return best


def solve_script(face: Tuple[str, int], text: str, metrics: Mapping[str, Any]) -> Dict[str, Any]:
    """Script word from the reference's MEASURED metrics (brain: part.ref_script_metrics =
    {capital:{x0,x1,top,bottom}, tail:{x0,x1,xh_top,baseline}}, read off a gridded reference
    crop). The reference sets a GIANT capital over a small lowercase — a ratio no single size of
    our face reproduces — so the word is set in two runs: the tail sized by
    x-height and seated on the baseline, the capital sized by its own height. Template matching
    a different script face against the reference proved unreliable (: it locked the
    capital onto sub-strokes), which is why the metrics are read, not searched."""
    def F(sz: int): return ImageFont.truetype(face[0], sz, index=face[1])
    t, c, tail = metrics["tail"], metrics["capital"], text[1:]
    ob = F(1000).getbbox("o")
    ts = int(round(1000 * (t["baseline"] - t["xh_top"]) / (ob[3] - ob[1])))
    f = F(ts); tb = f.getbbox(tail)
    tr_em = ((t["x1"] - t["x0"]) - (tb[2] - tb[0])) / max(1, len(tail) - 1) / ts
    tr_em = max(-0.03, min(0.30, tr_em))
    # Tracking floor hit (the reference's lowercase is narrower than ours can be set without
    # the joins colliding): take the rest as a bounded horizontal squeeze, as the capital does.
    tracked_w = (tb[2] - tb[0]) + tr_em * ts * max(0, len(tail) - 1)
    t_stretch = max(0.88, min(1.0, (t["x1"] - t["x0"]) / max(1.0, tracked_w)))
    base_off = f.getbbox("o")[3] - tb[1]                     # tail ink top → baseline
    cb = F(1000).getbbox(text[0])
    cs = int(round(1000 * (c["bottom"] - c["top"]) / (cb[3] - cb[1])))
    st = max(0.9, min(1.35, (c["x1"] - c["x0"]) / ((cb[2] - cb[0]) * cs / 1000)))
    return {"two_run": True, "score": 1.0, "source": "ref_script_metrics",
            "capital": {"size": cs, "stretch": round(st, 3), "ink_xy": [c["x0"], c["top"]]},
            "tail": {"size": ts, "tracking_em": round(tr_em, 3), "stretch": round(t_stretch, 3), "ink_xy": [t["x0"], int(round(t["baseline"] - base_off))]}}


def measure(brain: Path, frames: Path) -> Dict[str, Any]:
    from onetoone.captions_typeset import DEFAULT_FACES, _PART_FACE
    path = brain / "captions.plaintext.json"
    data = json.loads(path.read_text())
    out: Dict[str, Any] = {}
    for st in data["states"]:
        plain = [p for p in st["parts"] if _PART_FACE.get(str(p.get("part"))) == "plain"]
        for p in st["parts"]:
            kind = _PART_FACE.get(str(p.get("part")), "plain")
            box = p.get("bbox_settled") or p.get("bbox_settled_approx")
            ink = p.get("ink_rgb") or p.get("ink_rgb_approx")
            if not box or not ink:
                continue
            n = int(p.get("bbox_frame") or st["out"])
            png = frames / f"{n:04d}.png"
            if not png.exists():
                continue
            text = str(p.get("text") or " ".join(p.get("words") or []))
            minus = None
            if kind == "ornate" and plain:
                minus = plain[0].get("bbox_settled") or plain[0].get("bbox_settled_approx")
            if kind == "ornate":
                if not p.get("ref_script_metrics"):
                    p.pop("ref_fit", None)
                    continue
                fit = solve_script(DEFAULT_FACES[kind], text, p["ref_script_metrics"])
            else:
                fit = fit_part(Image.open(png), DEFAULT_FACES[kind], text, box, ink, kind=kind, minus=minus)
                if fit["score"] < 0.7 and text.count(" ") >= 2:
                    ramp = fit_ramp(Image.open(png), DEFAULT_FACES[kind], text, box, ink)
                    if ramp["score"] > fit["score"] + 0.05:
                        fit = ramp
            fit["frame"] = n
            prev = p.get("ref_fit")
            # NEVER REGRESS: a re-fit with a different search can score lower than the
            # fit already stored (it happened: one line went 0.83 -> 0.66). Keep the better one.
            if (kind == "plain" and isinstance(prev, dict) and not prev.get("two_run")
                    and float(prev.get("score", -1)) > float(fit.get("score", -1))):
                fit = prev
            p["ref_fit"] = fit
            out[f"{st['id']}:{text}"] = fit
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    return out


if __name__ == "__main__":
    res = measure(Path(sys.argv[1]), Path(sys.argv[2]))
    for k, v in res.items():
        if v.get("two_run"):
            for r in ("capital", "tail"):
                w = v[r]; print(f"{k:30} {r:7} size {w['size']} track {w.get('tracking_em')} stretch {w.get('stretch')} ink_xy {w['ink_xy']}")
            continue
        print(f"{k:34} score {v['score']:.3f} size {v.get('size')} track {v.get('tracking_em')} stretch {v.get('stretch')} ink {v.get('ink_box')}")
