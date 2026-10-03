#!/usr/bin/env python3
"""onetoone.devices — replay the reference's measured caption ENTRY DEVICES on our typeset layer.

captions_typeset gives the SETTLED look of a caption state: the right face, the right size, the
right place, sharp. The reference does not cut to that — every caption arrives. It arrives with
a blur that resolves over 2-15 frames, an opacity that ramps with it, a scale that keeps
drifting after the blur has gone, and, on two lines, one word at a time.

onetoone.measure_devices reads those curves off the reference's own frames into
brain/caption_devices.json. This module applies them:

    layer_for_frame(state, part, n, sharp_layer) -> RGBA full-canvas layer for frame n

Per frame, in the order the reference's own renderer must have done it:
  1. GEOMETRY — scale about the part's measured anchor (the settled box's top-left corner),
     then translate by the measured drift. At the settled frame this is the identity, so the
     settled layer is returned untouched and the typeset work is never resampled for nothing.
  2. BLUR — a Gaussian with separate x and y sigmas, so a horizontal smear stays a smear.
     Applied on PREMULTIPLIED alpha, or the blur drags the ink's colour out of the transparent
     pixels around the glyphs and leaves a dark fringe.
  3. OPACITY — a straight multiply on the alpha.
Word-by-word lines carry the same geometry and their own opacity/blur per word; the words are
split off our own layer by its widest interior alpha gaps, never re-typeset.

A part with no measured device renders exactly as it does today: the static settled layer.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np
from PIL import Image
from scipy import ndimage

DEVICES_FILE = "caption_devices.json"
SETTLED_EPS = 1e-3          # |scale-1|, |dx|, |dy|, |1-opacity| under this = nothing to do


def load_devices(brain: Path) -> Dict[str, Any] | None:
    """The measured device table for this brain, or None (every part then renders static)."""
    f = Path(brain) / DEVICES_FILE
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text()).get("states") or None
    except (ValueError, OSError):
        return None


def part_text(part: Mapping[str, Any]) -> str:
    return str(part.get("text") or " ".join(part.get("words") or []))


def device_for(devices: Mapping[str, Any] | None, state: Mapping[str, Any],
               part: Mapping[str, Any]) -> Dict[str, Any] | None:
    if not devices:
        return None
    d = (devices.get(str(state.get("id"))) or {}).get(part_text(part))
    if not d or d.get("first_frame") is None or not d.get("curve"):
        return None
    return d


def frame_row(dev: Mapping[str, Any], n: int) -> Dict[str, Any] | None:
    """The measured row for frame n, or None when n is outside what was measured.

    Before the first measured frame the caption is not up yet; after the last one it holds its
    settled look, which is what the static layer already is.
    """
    curve = dev["curve"]
    if n < int(curve[0]["n"]):
        return None
    if n > int(curve[-1]["n"]):
        return dict(curve[-1])
    for row in curve:
        if int(row["n"]) == n:
            return dict(row)
    return None


def _alpha_columns(layer: Image.Image, n_words: int) -> List[Tuple[int, int]]:
    """Column spans of each word in OUR layer, split at its widest interior alpha gaps."""
    a = np.asarray(layer.split()[-1], dtype=np.float32)
    prof = a.sum(axis=0)
    if prof.max() <= 0 or n_words < 2:
        return []
    low = prof < 0.02 * float(prof.max())
    runs: List[List[int]] = []
    start = None
    for i, v in enumerate(low):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append([start, i - 1]); start = None
    if start is not None:
        runs.append([start, len(low) - 1])
    interior = [r for r in runs if r[0] > 0 and r[1] < len(low) - 1]
    if len(interior) < n_words - 1:
        return []
    interior.sort(key=lambda r: r[1] - r[0], reverse=True)
    cuts = sorted((r[0] + r[1]) // 2 for r in interior[:n_words - 1])
    ink = np.flatnonzero(~low)
    bounds = [int(ink[0])] + cuts + [int(ink[-1]) + 1]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _column_slice(layer: Image.Image, span: Tuple[int, int]) -> Image.Image:
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    x0, x1 = span
    out.paste(layer.crop((x0, 0, x1, layer.height)), (x0, 0))
    return out


def transform(layer: Image.Image, scale: float, dx: float, dy: float,
              anchor: Sequence[float]) -> Image.Image:
    """Scale about `anchor` then translate, as one resample of the settled layer."""
    if abs(scale - 1.0) < SETTLED_EPS and abs(dx) < SETTLED_EPS and abs(dy) < SETTLED_EPS:
        return layer
    ax, ay = float(anchor[0]), float(anchor[1])
    inv = 1.0 / float(scale)
    # output (x, y) <- input (inv*x + c, inv*y + f); c, f put the anchor back where it was
    c = ax * (1.0 - inv) - dx * inv
    f = ay * (1.0 - inv) - dy * inv
    return layer.transform(layer.size, Image.AFFINE, (inv, 0.0, c, 0.0, inv, f),
                           resample=Image.BILINEAR)


def blur(layer: Image.Image, sigma_x: float, sigma_y: float) -> Image.Image:
    """Anisotropic Gaussian on premultiplied alpha, computed on the ink's own crop."""
    if sigma_x <= 0.01 and sigma_y <= 0.01:
        return layer
    bbox = layer.getbbox()
    if bbox is None:
        return layer
    pad = int(np.ceil(3 * max(sigma_x, sigma_y))) + 2
    x0 = max(0, bbox[0] - pad); y0 = max(0, bbox[1] - pad)
    x1 = min(layer.width, bbox[2] + pad); y1 = min(layer.height, bbox[3] + pad)
    a = np.asarray(layer.crop((x0, y0, x1, y1)), dtype=np.float32)
    al = a[..., 3:4] / 255.0
    pre = np.concatenate([a[..., :3] * al, al * 255.0], axis=-1)
    for axis, s in ((1, sigma_x), (0, sigma_y)):
        if s > 0.01:
            pre = ndimage.gaussian_filter1d(pre, s, axis=axis, mode="constant")
    out_a = np.clip(pre[..., 3:4], 0, 255)
    rgb = np.where(out_a > 0.5, pre[..., :3] / np.maximum(out_a / 255.0, 1e-6), 0.0)
    arr = np.concatenate([np.clip(rgb, 0, 255), out_a], axis=-1).astype(np.uint8)
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.paste(Image.fromarray(arr, "RGBA"), (x0, y0))
    return out


def fade(layer: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 1.0 - SETTLED_EPS:
        return layer
    if opacity <= 0.0:
        return Image.new("RGBA", layer.size, (0, 0, 0, 0))
    r, g, b, a = layer.split()
    return Image.merge("RGBA", (r, g, b, a.point(lambda v: int(v * opacity))))


def _apply(layer: Image.Image, row: Mapping[str, Any], anchor: Sequence[float],
           sigma_x: float, sigma_y: float, opacity: float) -> Image.Image:
    out = transform(layer, float(row.get("scale", 1.0)), float(row.get("dx", 0.0)),
                    float(row.get("dy", 0.0)), anchor)
    out = blur(out, sigma_x, sigma_y)
    return fade(out, opacity)


def layer_for_frame(state: Mapping[str, Any], part: Mapping[str, Any], n: int,
                    sharp_layer: Image.Image,
                    devices: Mapping[str, Any] | None = None) -> Image.Image:
    """This part's layer as the reference has it on frame n.

    `sharp_layer` is captions_typeset's settled layer for this part alone. With no measured
    device (or none for this part) the settled layer is returned unchanged, which is exactly
    the static behaviour the renderer had before.
    """
    dev = device_for(devices, state, part)
    if dev is None:
        return sharp_layer
    first = int(dev["first_frame"])
    if n < first:
        return Image.new("RGBA", sharp_layer.size, (0, 0, 0, 0))
    row = frame_row(dev, n)
    # The geometry device scales about the part's top-left. When the part was typeset from the
    # reference's pixels (ref_fit) its true ink corner can differ from the eye-read study box, and
    # scaling about the wrong corner slides the word — so anchor on OUR settled ink instead.
    _bb = sharp_layer.getbbox() if part.get("ref_fit") else None
    if _bb is not None:
        dev = dict(dev, anchor=[_bb[0], _bb[1]])
    if row is None:
        return sharp_layer
    anchor = dev.get("anchor") or [0, 0]
    words = list(part.get("words") or [])
    wrow = row.get("words") if dev.get("word_split") else None
    if wrow and len(words) > 1:
        spans = _alpha_columns(sharp_layer, len(words))
        if len(spans) == len(words):
            out = Image.new("RGBA", sharp_layer.size, (0, 0, 0, 0))
            for w, span in zip(words, spans):
                wd = wrow.get(w) or {}
                op = float(wd.get("opacity", row.get("opacity", 1.0)))
                if op <= 0.0:
                    continue
                piece = _apply(_column_slice(sharp_layer, span), row, anchor,
                               float(wd.get("sigma_x", row.get("sigma_x", 0.0))),
                               float(wd.get("sigma_y", row.get("sigma_y", 0.0))), op)
                out.alpha_composite(piece)
            return out
    return _apply(sharp_layer, row, anchor, float(row.get("sigma_x", 0.0)),
                  float(row.get("sigma_y", 0.0)), float(row.get("opacity", 1.0)))


def summary(devices: Mapping[str, Any] | None) -> Dict[str, Any]:
    """One line per measured unit, for the render report and the card."""
    if not devices:
        return {"devices": "NONE — every caption static"}
    out = {}
    for sid, parts in devices.items():
        for text, d in parts.items():
            if d.get("first_frame") is None:
                out[f"{sid}:{text}"] = "static (%s)" % d.get("why", "not measured")
                continue
            out[f"{sid}:{text}"] = (
                "first=%s sharp=%s axis=%s max_sigma=(%.1f,%.1f) scale %.3f->%.3f src=%s"
                % (d["first_frame"], d["sharp_frame"], d.get("blur_axis"),
                   d.get("max_sigma_x", 0.0), d.get("max_sigma_y", 0.0),
                   d.get("scale_first", 1.0), d.get("scale_last", 1.0),
                   d.get("first_frame_source", "fit")))
    return out
