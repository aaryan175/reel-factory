#!/usr/bin/env python3
"""onetoone.framing — per-slot reframing of a master before the cover-scale.

A cast slot may carry a "crop" spec so the reviewer's 3840x2160 master matches the
reference's FRAMING and CAMERA TRAVEL (audit: our shots were too tight,
too big, headroom clipped, and static where the reference drifts/pushes):

    {"zoom": 1.25,                 # 1.0 = full frame; 1.6 = 1.6x tighter
     "cx": 0.5, "cy": 0.45,        # crop centre as a fraction of the master
     "drift": {"dx": 1.5, "dy": 0.0},   # crop-window travel in master px per frame (pan)
     "push": 0.0004}               # zoom change per frame (+ pushes in, - pulls out)

filter_for(spec, w, h) returns an ffmpeg `crop=` expression using `n` (frame index) so
the travel is exact per frame and cheap. Applied BEFORE lut/eq/cover in render.cut_picture.
"""
from __future__ import annotations

from typing import Dict, Any, Mapping


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def filter_for(spec: Mapping[str, Any] | None, w: int = 3840, h: int = 2160, *, aspect: float = 1916 / 1078) -> str | None:
    """ffmpeg crop filter for a slot's crop spec on a w x h master, or None for no reframe.

    The crop window keeps the delivery aspect so the later cover-scale never has to crop
    again (which would silently change the framing the caster approved).
    """
    if not spec:
        return None
    if spec.get("push") or spec.get("keys"):
        return _zoompan_for(spec, w, h, aspect=aspect)
    zoom = float(spec.get("zoom", 1.0))
    push = 0.0   # push is handled by _zoompan_for: crop's w/h are fixed at init, so a crop-based push never zoomed
    cx = _clamp(float(spec.get("cx", 0.5)), 0.0, 1.0)
    cy = _clamp(float(spec.get("cy", 0.5)), 0.0, 1.0)
    drift = spec.get("drift") or {}
    dx = float(drift.get("dx", 0.0)); dy = float(drift.get("dy", 0.0))
    # window at zoom z: as wide as fits the aspect inside the master
    base_w = min(w, h * aspect)
    base_h = base_w / aspect
    # zoom as an expression when pushing, else a constant
    z = f"({zoom:.6f}+{push:.8f}*n)" if push else f"{zoom:.6f}"
    cw = f"({base_w:.3f}/{z})"
    ch = f"({base_h:.3f}/{z})"
    x = f"clip({cx:.6f}*{w}-{cw}/2+{dx:.4f}*n,0,{w}-{cw})"
    y = f"clip({cy:.6f}*{h}-{ch}/2+{dy:.4f}*n,0,{h}-{ch})"
    # ffmpeg crop needs even sizes for yuv420p; floor to even via 2*floor(x/2).
    # Each value is quoted: the commas inside clip(a,b,c) would otherwise split the options.
    return f"crop=w='2*floor({cw}/2)':h='2*floor({ch}/2)':x='{x}':y='{y}':exact=1"


def effective_centre(spec: Mapping[str, Any] | None, w: int, h: int, *, aspect: float = 1916 / 1078) -> Dict[str, Any] | None:
    """The centre the renderer can actually honour for a static crop. filter_for clips the window
    to the master, so a declared cx/cy near an edge is silently moved (audit-v019 MEDIUM-2: a reference
    S08 declared cx 0.70 on a 2160-wide portrait master, rendered at 0.615). Reported per shot so the
    cast and the card never record a number the renderer did not use."""
    if not spec or spec.get("push") or spec.get("keys"):
        return None
    zoom = float(spec.get("zoom", 1.0))
    base_w = min(w, h * aspect); base_h = base_w / aspect
    cw, ch = base_w / zoom, base_h / zoom
    cx = _clamp(float(spec.get("cx", 0.5)), 0.0, 1.0); cy = _clamp(float(spec.get("cy", 0.5)), 0.0, 1.0)
    x = min(max(cx * w - cw / 2, 0.0), w - cw); y = min(max(cy * h - ch / 2, 0.0), h - ch)
    ecx, ecy = (x + cw / 2) / w, (y + ch / 2) / h
    return {"cx": round(ecx, 4), "cy": round(ecy, 4), "clamped": abs(ecx - cx) > 0.002 or abs(ecy - cy) > 0.002,
            "declared": {"cx": cx, "cy": cy}, "window_px": [round(cw), round(ch)], "upscale": round(1916 / cw, 3)}


def _piecewise(keys, idx: int) -> str:
    """Linear interpolation of keys [[n, value], ...] as an ffmpeg expression of `on`."""
    keys = sorted(keys)
    expr = f"{keys[-1][1]:.6f}"
    for (n0, v0), (n1, v1) in reversed(list(zip(keys[:-1], keys[1:]))):
        slope = (v1 - v0) / max(1, (n1 - n0))
        expr = f"if(lt(on\\,{n1})\\,{v0:.6f}+{slope:.8f}*(on-{n0})\\,{expr})"
    return f"if(lt(on\\,{keys[0][0]})\\,{keys[0][1]:.6f}\\,{expr})"


def _zoompan_for(spec: Mapping[str, Any], w: int, h: int, *, aspect: float) -> str:
    """Per-frame ZOOM (push / keyframes) via zoompan, which really re-samples every frame.

    v011l audit: `crop` evaluates w/h once at init, so the old crop-based "push" only
    nudged x/y and NEVER zoomed (measured +0.02% for a push that should give +26%). zoompan's
    window is iw/zoom x ih/zoom, i.e. the master's own aspect, so it is used only for masters at the
    delivery aspect (16:9); a portrait master raises rather than silently mis-framing.

    spec: {"zoom", "cx", "cy", "push", "drift": {"dx","dy"}}  or  {"keys": [[n, zoom, cx, cy], ...]}
    (cx, cy = window centre as a fraction of the master; drift in master px per frame).
    """
    if abs(w / h - aspect) > 0.02:
        raise ValueError(f"push/keys need a {aspect:.3f} master; got {w}x{h} (portrait masters: use a static crop)")
    if spec.get("keys"):
        ks = spec["keys"]
        z = _piecewise([[k[0], k[1]] for k in ks], 0)
        cxe = _piecewise([[k[0], k[2]] for k in ks], 0)
        cye = _piecewise([[k[0], k[3]] for k in ks], 0)
    else:
        z0, p = float(spec.get("zoom", 1.0)), float(spec.get("push", 0.0))
        drift = spec.get("drift") or {}
        dx, dy = float(drift.get("dx", 0.0)), float(drift.get("dy", 0.0))
        z = f"({z0:.6f}+{p:.8f}*on)"
        cxe = f"({_clamp(float(spec.get('cx', 0.5)), 0, 1):.6f}+{dx / w:.8f}*on)"
        cye = f"({_clamp(float(spec.get('cy', 0.5)), 0, 1):.6f}+{dy / h:.8f}*on)"
    x = f"max(0\\,min(iw-iw/zoom\\,{cxe}*iw-iw/zoom/2))"
    y = f"max(0\\,min(ih-ih/zoom\\,{cye}*ih-ih/zoom/2))"
    ow, oh = 1916, 1078
    return f"zoompan=z='max(1\\,{z})':x='{x}':y='{y}':d=1:s={ow}x{oh}:fps=24000/1001"


def display_dims(master: str) -> tuple[int, int]:
    """Decoded frame size AFTER ffmpeg's autorotate. Some masters carry a 90° display
    matrix, and a
    crop spec must be evaluated on that frame, not on the stored 3840x2160."""
    import json as _json
    from onetoone.ffx import run
    out = run(["-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=width,height:stream_side_data=rotation", "-of", "json", master], probe=True, capture=True)
    info = _json.loads(out.stdout or "{}")
    stream = (info.get("streams") or [{}])[0]
    w, h = int(stream.get("width", 3840)), int(stream.get("height", 2160))
    rot = 0
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(round(float(sd["rotation"])))
    if rot % 180 != 0:
        w, h = h, w
    return w, h


if __name__ == "__main__":
    import json, sys
    print(filter_for(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"zoom": 1.25, "cx": 0.5, "cy": 0.45, "drift": {"dx": 1.5}}))
