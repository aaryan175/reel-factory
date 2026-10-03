#!/usr/bin/env python3
"""onetoone.render — the durable 1:1 render path (v9 goal, step 1).

One repeatable pipeline, no per-run rewrites:
  1. PICTURE  — cut our own masters on brain/cutgrid.json (cast slot in_s → shot len frames),
                per shot: official LC-709 LUT (masters are S-Log3) + the same per-shot mood eq the
                approved look sheet used, cover-crop to the reference geometry, resample to the
                reference clock. Concat. Mux the AUDIO TRACK by stream copy (bit-exact). By default
                that is a licensed track you supply (`--audio`, cut to the reel's length); the
                reference's own audio is used only with the explicit opt-in
                `--reference-audio-rights-held`, and only if you hold the rights to it.
  2. CAPTIONS — rasterise the typeset layer of every caption state (captions_typeset) into one
                full-canvas RGBA PNG per frame over the state's in..out. STATIC this pass: the
                reference's entry blur / scale-down devices are NOT reproduced (disclosed on the card).
  3. COMPOSITE — variant_render.composite(): tv→full, alpha-composite, full→tv bt709, exact frame
                count, audio copied. Then a ≤10MB delivery encode if needed.
Footage law: masters only (authorized library). ONE ffmpeg at a time. Never uploads, never registry.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/
from onetoone.captions_typeset import (_PART_FACE, _part_box, DEFAULT_FACES,  # noqa: E402
                                       readability, typeset_part, typeset_state)
from onetoone import devices as dv  # noqa: E402  (measured entry blur / fade / scale ramp)
from onetoone import grade  # noqa: E402
from onetoone.ffx import FFMPEG, run as ffx_run  # noqa: E402  (ONE ffmpeg law — every call goes through the lock)
from onetoone.housechain import HOUSE_TAIL, house_head  # noqa: E402
from onetoone.framing import display_dims, filter_for  # noqa: E402  (per-slot crop/drift/push before the cover)
from onetoone.looksheet import COVER, FPS, LUT, NATURAL_GRADE, frame_stats, resolve_master  # noqa: E402
import variant_render as vr  # noqa: E402  (tools/variant_render.py — composite / encode_delivery)

FPS_STR = "24000/1001"
TIMESCALE = 24000
WEB_CAP = 10_000_000


def _run(cmd: List[str]) -> None:
    """ffmpeg under the machine-wide lock. Accepts the historical [FFMPEG, ...] shape."""
    args = cmd[1:] if cmd and cmd[0] == FFMPEG else cmd
    proc = ffx_run(args)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, [FFMPEG, *args])


CHROMA_JUMP_MAX = 0.40   # a shot may not gain >40% magenta- or green-dominant pixels over its own master


def chroma_fractions(png: Path) -> tuple[float, float]:
    """(magenta, green) fraction of a frame: pixels whose G sits 50+ codes below BOTH R and B, or above both.
    A chroma-plane corruption floods one of the two; real footage, even
    under a red or purple practical, does not move by CHROMA_JUMP_MAX against its own master."""
    import numpy as np
    a = np.asarray(Image.open(png).convert("RGB"), dtype=np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return float((g < np.minimum(r, b) - 50).mean()), float((g > np.maximum(r, b) + 50).mean())


def chroma_sanity(seg: Path, master: Path, t_mid: float, n: int, stat_dir: Path, slot: str) -> dict:
    """DECODED check of the segment that ships: its middle frame against the same instant of the
    master decoded on a plain path with no crop and no LUT. Raises, so a corrupted shot stops the render
    instead of reaching an audit or the reviewer. The kit's unit tests cannot see this class: only pixels can."""
    sp = stat_dir / f"{slot}_sanity_seg.png"; mp_ = stat_dir / f"{slot}_sanity_master.png"
    _run([FFMPEG, "-y", "-loglevel", "error", "-i", str(seg), "-vf",
          f"select=eq(n\\,{n // 2}),scale=480:-2:in_color_matrix=bt709:in_range=tv:out_range=full,format=rgb24",
          "-vsync", "0", "-frames:v", "1", str(sp)])
    _run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t_mid:.4f}", "-i", str(master), "-vf",
          "scale=480:-2:in_color_matrix=bt709,format=rgb24", "-frames:v", "1", str(mp_)])
    (sm, sg), (mm, mg) = chroma_fractions(sp), chroma_fractions(mp_)
    out = {"magenta": round(sm, 3), "green": round(sg, 3), "master_magenta": round(mm, 3), "master_green": round(mg, 3)}
    if sm - mm > CHROMA_JUMP_MAX or sg - mg > CHROMA_JUMP_MAX:
        raise RuntimeError(f"{slot}: CHROMA CORRUPTION in the rendered segment {out} - the shot is not the master's "
                           f"colours (see {sp}). Fix the filter chain; do not ship, do not nudge the cast to hide it.")
    return out


PULL_TOL = 6.0        # mean-luma codes: closer than this and no pull is applied
PULL_TOL_REL = 0.25   # ...but never looser than a quarter of the target: a flat 6 codes is +-107%
                      # of a 5.6 target, which is how a reference's blackout shot (S05) shipped at 11.1
                      # with no correction at all
PULL_GAMMA_MIN = 0.45  # never darker than this exponent's curve (keeps shadows from crushing)
PULL_SHOULDER = 0.94  # the curve's top point: 255 lands at ~240 so a clipped set comes off the
                      # ceiling. y = x**(1/g) maps 1.0 -> 1.0 for EVERY g, so without this the pull
                      # cannot touch clipping at all.
PULL_PASS2_GAMMA_MIN = 0.62  # the SECOND pass floors higher than the first: two 0.45 curves compose
                             # to ~0.2025 effective, STEEPER than one 0.30, and that is what turned
                             # a reference's S09 into a black cutout (16.7% of the frame at <=2). 0.62 on
                             # top of 0.45 composes to ~0.28. A few codes over target beats losing
                             # the subject.
PULL_PASS2_OVER = 5.0  # a second 0.45 pass only where the first leaves the shot this far over.
                       # Two 0.45 curves compose to ~0.20 effective and are far kinder to the
                       # shadows than one deep curve.


def luma_pull(probe_png: Path, target: float, gamma_min: float | None = None) -> Dict[str, Any]:
    """A measured darkening curve so the shot's MEAN LUMA meets `target` (0-255).

    Searches the exponent g of y = x**(1/g) on the graded probe frame (numpy, the same maths
    ffmpeg's curves will run) and returns an ffmpeg `curves` fragment sampled from it — pchip,
    monotone, no brightness offset (a flat offset crushes shadows; castC's S12 warning). Darken
    only: brightening an under-exposed master lifts noise, and no reel-11 shot needs it.
    """
    import numpy as np
    im = np.asarray(Image.open(probe_png).convert("RGB"), dtype=np.float64) / 255.0
    lum = 0.2126 * im[..., 0] + 0.7152 * im[..., 1] + 0.0722 * im[..., 2]
    ours = float(lum.mean() * 255.0)
    tol = min(PULL_TOL, max(1.0, PULL_TOL_REL * target))
    if ours <= target + tol:
        return {"filter": None, "ours": round(ours, 1), "target": round(target, 1), "gamma": 1.0,
                "tol": round(tol, 2)}
    lo, hi = (PULL_GAMMA_MIN if gamma_min is None else gamma_min), 1.0
    for _ in range(30):
        g = (lo + hi) / 2
        m = float((np.power(lum, 1.0 / g)).mean() * 255.0)
        if m > target:
            hi = g
        else:
            lo = g
    g = (lo + hi) / 2
    xs = [0.0, 0.05, 0.12, 0.25, 0.4, 0.6, 0.8, 1.0]
    ys = [x ** (1.0 / g) for x in xs]
    ys[-1] = min(ys[-1], PULL_SHOULDER)          # shoulder: take a clipped set off the ceiling
    ys[-2] = min(ys[-2], (ys[-2] + PULL_SHOULDER) / 2 if ys[-2] > PULL_SHOULDER else ys[-2])
    pts = " ".join(f"{x:.4f}/{y:.4f}" for x, y in zip(xs, ys))
    got = float((np.power(lum, 1.0 / g)).mean() * 255.0)
    return {"filter": f"curves=interp=pchip:all='{pts}'", "ours": round(ours, 1), "target": round(target, 1),
            "gamma": round(g, 4), "after": round(got, 1), "tol": round(tol, 2), "shoulder": PULL_SHOULDER}


HL_KNEE = 0.20        # below this the highlight pull is identity: the subject is not touched
HL_E_MAX = 14.0       # how hard the highlights may be compressed before we stop and stay over


def highlight_pull(probe_png: Path, target: float, knee: float = HL_KNEE) -> Dict[str, Any]:
    """Bring a shot's mean luma down by compressing ONLY what is above `knee`, leaving everything
    below it untouched.

    Why this exists: a gamma pull is the wrong instrument for a shot whose excess is all sky. In one
    test shot (S09) our plate sat 106 codes over the reference because it faces the sun where the
    reference is a dusk silhouette; a gamma steep enough to close that gap took a backlit bright
    garment (a MIDTONE, around 0.35) to black — 16.7% of the frame at <=2 on the first attempt,
    still 7.65% with the second pass floored on the next. On the same probe frame this curve reaches within ~7 codes of
    target with ZERO pixels under 5. Highlights are where the error is, so highlights are what moves.
    """
    import numpy as np
    im = np.asarray(Image.open(probe_png).convert("RGB"), dtype=np.float64) / 255.0
    lum = 0.2126 * im[..., 0] + 0.7152 * im[..., 1] + 0.0722 * im[..., 2]
    ours = float(lum.mean() * 255.0)
    top = PULL_SHOULDER

    def shaped(e: float):
        out = lum.copy()
        m = lum > knee
        out[m] = knee + (top - knee) * np.power((lum[m] - knee) / (1.0 - knee), e)
        return out

    lo, hi = 1.0, HL_E_MAX
    for _ in range(40):
        e = (lo + hi) / 2
        if float(shaped(e).mean() * 255.0) > target:
            lo = e
        else:
            hi = e
    e = (lo + hi) / 2
    got = float(shaped(e).mean() * 255.0)
    xs = [0.0, knee * 0.5, knee, knee + (1 - knee) * 0.25, knee + (1 - knee) * 0.5, knee + (1 - knee) * 0.75, 1.0]
    ys = [x if x <= knee else knee + (top - knee) * ((x - knee) / (1 - knee)) ** e for x in xs]
    pts = " ".join(f"{x:.4f}/{y:.4f}" for x, y in zip(xs, ys))
    return {"filter": f"curves=interp=pchip:all='{pts}'", "ours": round(ours, 1), "target": round(target, 1),
            "exponent": round(e, 3), "knee": knee, "after": round(got, 1), "kind": "highlight"}


CHROMA_FOLLOW = 0.4   # how much of a luma drop the chroma follows (auditor: 0.5-0.7 keeps orange from
                      # going lurid and keeps a dark subject inside gamut)


def luma_only(rgb_curves: str, ours: float, after: float) -> str:
    """Wrap RGB `curves` fragments so they move LUMA ONLY and leave the house grade's colour alone.

    Why: `curves=all` gives R, G and B the same transfer, so every exposure
    pull drained chroma where the picture was brightest — S09's orange sky went olive (R-B +66 →
    +6.8), S12 lost 62% of its blue. The intended grade is LC-709 + NATURAL_GRADE and
    nothing else; the pulls exist to match exposure, not to recolour. So: run the curves on a copy,
    keep only its Y, and take U/V from the ungraded side, scaled toward neutral by CHROMA_FOLLOW of
    the luma drop (chroma sized for the old Y would read over-saturated and can leave gamut).
    """
    k = 1.0
    if ours and after is not None and ours > 0:
        k = max(0.4, 1.0 - CHROMA_FOLLOW * (1.0 - min(1.0, after / ours)))
    uv = f"lutyuv=u='128+(val-128)*{k:.4f}':v='128+(val-128)*{k:.4f}'"
    return (f"format=yuv444p,split[__o][__g];[__g]format=rgb24,{rgb_curves},format=yuv444p[__gy];"
            f"[__o]{uv}[__oc];[__gy][__oc]mergeplanes=0x001112:yuv444p")


_LIGHT_KEYS = ("rs", "gs", "bs", "rm", "gm", "bm", "rh", "gh", "bh")


def light_state_filter(spec: Any) -> str:
    """A slot's LIGHT STATE: the reference changes the scene's practical light between cuts on a
    locked-off frame. Where the shot
    has no colour-changing practical, the state is a declared colour-balance wash on the SAME
    shot, applied before the measured grade so the solver sees the picture that ships.
    Whitelisted ffmpeg colorbalance keys only, each clamped to ±0.6; always logged as a residual."""
    if not isinstance(spec, dict):
        return ""
    cb = spec.get("colorbalance") or {}
    parts = [f"{k}={max(-0.6, min(0.6, float(cb[k]))):.3f}" for k in _LIGHT_KEYS if k in cb]
    out = ["colorbalance=" + ":".join(parts)] if parts else []
    # "gain": the practical switched OFF (a reference S05 — the reference drops to ~5 mean luma while
    # the frame stays locked off). Darken-only multiply, clamped 0.15..1.0.
    if spec.get("gain") is not None:
        g = max(0.15, min(1.0, float(spec["gain"])))
        if g < 0.999:
            out.append(f"colorchannelmixer=rr={g:.3f}:gg={g:.3f}:bb={g:.3f}")
    return ",".join(out)


def redact_filter(spec: Any, n_frames: int) -> str:
    """PRIVACY REDACTION.
    A soft blur patch of fixed size travelling linearly from `from` to `to` (patch CENTRES, in
    delivered-frame pixels) across the shot. Blackout law: no readable identifier ships."""
    if not isinstance(spec, dict):
        return ""
    w, h = int(spec["w"]), int(spec["h"])
    (x0, y0), (x1, y1) = spec["from"], spec["to"]
    N = max(1, n_frames - 1)
    xe = f"{x0 - w / 2:.1f}+({x1 - x0:.1f})*n/{N}"
    ye = f"{y0 - h / 2:.1f}+({y1 - y0:.1f})*n/{N}"
    fe = int(spec.get("feather", 16))
    alpha = f"255*min(1\\,min(X\\,W-X)/{fe})*min(1\\,min(Y\\,H-Y)/{fe})"
    return (f"split[rm][rp];[rp]crop={w}:{h}:x='{xe}':y='{ye}',boxblur=12:3,format=rgba,"
            f"geq=r='r(X\\,Y)':g='g(X\\,Y)':b='b(X\\,Y)':a='{alpha}'[rb];"
            f"[rm][rb]overlay=x='{xe}':y='{ye}'")


RAMP_TOL = 2.5          # mean-luma codes; below this a shot already matches
RAMP_TOL_PASS2 = 1.5    # the second pass cleans up what the first under-delivered (offsets land ~70%)
RAMP_MAX = 40.0         # never move a single frame more than this (it is a shape fix, not a grade)


def caption_masks(brain: Path, frames: range, size: Tuple[int, int] = (1916, 1078), pad: int = 40):
    """Per-frame caption rectangles (canvas px) to EXCLUDE from luma measurement.

    v011j: the correction loop compared our caption-free segment with reference frames that carry
    captions; white ink adds 2-3 luma to a dark frame, so S04 was pushed +2.4 bright once our own
    caption went on. Bed-vs-bed is the honest comparison: the same rectangles (study box and the
    fitted ink box of every part live on that frame, padded) are cut out of both sides."""
    states = json.loads((brain / "captions.plaintext.json").read_text())["states"]
    out = []
    for f in frames:
        rects = []
        for st in states:
            if int(st["in"]) <= f <= int(st["out"]):
                for p in st["parts"]:
                    for b in (p.get("bbox_settled") or p.get("bbox_settled_approx"),
                              (p.get("ref_fit") or {}).get("ink_box")):
                        if b:
                            rects.append((max(0, b[0] - pad), max(0, b[1] - pad),
                                          min(size[0], b[2] + pad), min(size[1], b[3] + pad)))
                    # script words: their MEASURED capital/tail boxes (v011k: a blanket 700x400 rect per
                    # run masked most of S09 and the loop corrected from a sliver of bed -> +17)
                    sm = p.get("ref_script_metrics") or {}
                    c, t = sm.get("capital"), sm.get("tail")
                    if c:
                        rects.append((max(0, c["x0"] - pad), max(0, c["top"] - pad),
                                      min(size[0], c["x1"] + pad), min(size[1], c["bottom"] + pad)))
                    if t:
                        xh = t["baseline"] - t["xh_top"]
                        rects.append((max(0, t["x0"] - pad), max(0, int(t["xh_top"] - 1.3 * xh) - pad),
                                      min(size[0], t["x1"] + pad), min(size[1], int(t["baseline"] + 1.3 * xh) + pad)))
        out.append(rects)
    return out


def _frame_means(src: Path, out_dir: Path, *, start: int | None = None, count: int | None = None,
                 masks: List[List[Tuple[int, int, int, int]]] | None = None,
                 size: Tuple[int, int] = (1916, 1078)) -> List[float]:
    """Mean BT.709 luma (0-255) of each frame of `src` (or frames start..start+count-1).

    v011g audit: this used to read ffmpeg's gray plane, which matched BT.601 weights and understated
    the difference on saturated shots (S11 read +1.2 where BT.709 says +3.9). The deliverable is tagged
    BT.709, so decode to full-range RGB with the 709 matrix and weight 0.2126/0.7152/0.0722."""
    import numpy as np
    out_dir.mkdir(parents=True, exist_ok=True)
    vf = "scale=480:270:in_color_matrix=bt709:out_range=full,format=rgb24"   # 480x270: masks no longer round out coarsely
    if start is not None:
        vf = f"select='between(n\\,{start}\\,{start + count - 1})'," + vf
    _run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-vf", vf, "-vsync", "0",
          "-start_number", "0", str(out_dir / "%03d.png")])
    files = sorted(out_dir.glob("*.png"))
    if count is not None:
        files = files[:count]
    w = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
    vals = []
    for k, f in enumerate(files):
        L = np.asarray(Image.open(f).convert("RGB"), dtype=np.float64) @ w
        if masks is not None and k < len(masks) and masks[k]:
            keep = np.ones(L.shape, dtype=bool)
            sx, sy = L.shape[1] / size[0], L.shape[0] / size[1]
            for x0, y0, x1, y1 in masks[k]:
                keep[int(y0 * sy):int(np.ceil(y1 * sy)), int(x0 * sx):int(np.ceil(x1 * sx))] = False
            vals.append(float(L[keep].mean()) if keep.mean() >= 0.4 else float(L.mean()))
        else:
            vals.append(float(L.mean()))
    return vals


def ramp_filter(delta: List[float]) -> str:
    """Per-frame brightness offsets (mean-luma codes) as one eq expression, frame n → delta[n]."""
    expr = "0"
    for i in reversed(range(len(delta))):
        expr = f"if(eq(n\\,{i})\\,{delta[i] / 256.0:.5f}\\,{expr})"
    return f"eq=eval=frame:brightness='{expr}'"


def luma_ramp(ours: List[float], ref: List[float], tol: float = RAMP_TOL, smooth: bool = True) -> Dict[str, Any]:
    """Per-frame SHAPE correction (v010 audit MEDIUM-7: S06 climbs 56→70 out of the blackout step
    in the reference while ours sat flat at 74; S10 settles from 140 while ours rose from 128).
    Both series are taken relative to their own middle frame (the constant grade/pull already
    matched the middle), and the difference becomes a per-frame brightness offset for eq.
    Returns {} when every frame is already within RAMP_TOL."""
    n = min(len(ours), len(ref))
    if n < 3:
        return {}
    # Full per-frame match (the shot's level AND its shape), SMOOTHED over a 5-frame window:
    # v011c audit M6 — following raw per-frame deltas copied frame noise into the picture and
    # made S10 flicker, and anchoring on the middle frame left the shot 5 under overall.
    raw = [ref[i] - ours[i] for i in range(n)]
    # MEDIAN of 5 (v011d: a mean smeared a real step in the footage at S10 n=113 and left -4.4
    # after it; a median keeps steps and still drops single-frame spikes).
    half = 2 if smooth else 0     # cleanup passes follow the (smooth) reference frame by frame, so
    delta = []                    # single-frame pops in OUR footage (S10 n=130) are removed too
    for i in range(n):
        w = sorted(raw[max(0, i - half):min(n, i + half + 1)])
        delta.append(max(-RAMP_MAX, min(RAMP_MAX, w[len(w) // 2])))
    if max(abs(d) for d in delta) < tol:
        return {}
    return {"filter": ramp_filter(delta), "delta": [round(d, 1) for d in delta],
            "max_abs": round(max(abs(d) for d in delta), 1)}


def cut_picture(brain: Path, cast_json: Path, reference: Path, work: Path,
                *, size: Tuple[int, int], audio: "Path | None" = None) -> Dict[str, Any]:
    """Per-shot LUT+mood segments → concat → audio track muxed. Returns facts + per-shot eq.

    `audio` is the track to mux (a licensed track the user supplied). `render()` enforces the
    audio policy; a direct caller that passes no `audio` gets the reference's stream, which is
    only legitimate when the caller holds the rights to it."""
    # the gate lives HERE, not only in render(): importing cut_picture directly was an open bypass
    # A passed gate leaves a marker the caption pass requires.
    if not gate_bypassed():
        require_preflight(cast_json)
    work.mkdir(parents=True, exist_ok=True)
    (work / ".preflight-ok").write_text(json.dumps({"cast": str(cast_json), "cast_sha256": _sha(cast_json), "at": time.time()}))
    cutgrid = json.loads((brain / "cutgrid.json").read_text())
    cast_doc = json.loads(cast_json.read_text())
    cast = {s["slot"]: s for s in cast_doc["slots"]}
    # GRADE MODE: "natural" = LC-709 + the house NATURAL_GRADE on every
    # shot — no reference cast/tone matching, no luma forcing, no colour washes. "match" = the 1:1 path.
    grade_mode = str(cast_doc.get("grade_mode", "match"))
    shots = cutgrid["shots"]
    seg_dir = work / "segments"; seg_dir.mkdir(parents=True, exist_ok=True)
    w, h = size
    facts: Dict[str, Any] = {"shots": [], "total_frames": 0}
    stat_dir = work / "grade"; stat_dir.mkdir(parents=True, exist_ok=True)
    for shot in shots:
        slot = cast[shot["slot"]]
        master = resolve_master(str(slot["stem"]))
        if master is None:
            raise FileNotFoundError(f"{shot['slot']}: master {slot['stem']} not on disk")
        n = int(shot["out"]) - int(shot["in"]) + 1
        t0 = float(slot["in_s"])
        # 1. FRAMING — the slot's crop spec, evaluated on the DECODED frame (portrait masters
        #    carry a 90° display matrix), before the LUT so the grade sees exactly the picture that ships.
        mw, mh = display_dims(str(master))
        crop = filter_for(slot.get("crop"), mw, mh, aspect=w / h)
        from onetoone.framing import effective_centre
        crop_eff = effective_centre(slot.get("crop"), mw, mh, aspect=w / h)   # audit-v019 MEDIUM-2
        if crop and crop.startswith("zoompan="):
            # MOVING CROP -> ISOLATED PLATE: a zoompan inside the full grade/ramp chain
            # drove the machine to 14% free memory and 1.1 GB internal disk (S12 ffmpeg SIGKILLed, the
            # panic zone). The zoom alone is 0.9 s / 0.8 GB, so it is rendered FIRST to a ProRes plate
            # of exactly this slot, and the plate then goes through the normal static path.
            plates = work / "plates"; plates.mkdir(parents=True, exist_ok=True)
            plate = plates / f"{shot['slot']}.mov"
            _run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t0:.4f}", "-i", str(master), "-an",
                  "-vf", crop, "-frames:v", str(n), "-c:v", "prores_ks", "-profile:v", "3", str(plate)])
            master, t0, crop = plate, 0.0, None
            mw, mh = display_dims(str(master))
            is_plate = True
        else:
            is_plate = False
        pre = (f"{crop}," if crop else "") + f"lut3d=file={LUT}"
        ship_pre, tail = None, ""
        if grade_mode == "house":
            # L0037: the LUT alone is not the house grade. The masters are untagged
            # pc-range, so a bare lut3d lets swscale assume BT.601 and the picture ships cooler and darker.
            # House mode runs the approved reels' pipe:
            # cover -> in_range/bt709 -> f32 tetrahedral LUT -> 16-bit RGB ... colorspace bt709 tv at the end.
            # The ProRes zoom plate is already range-converted (measured tv), so it enters as tv.
            # The old bare chain went to RGB before
            # anything else, which hid that ffmpeg's crop corrupts chroma on a SUBSAMPLED 10-bit master for some
            # windows (S09: 3338x1878 at x=58.8 on yuv422p10le -> mean RGB 241/102/254). The crop must see 4:4:4.
            # Upsampling chroma first matches an RGB-first render to 0.02 mean RGB on S01/S06/S09.
            ship_pre = (f"format=yuv444p16le,{crop}," if crop else "") + house_head(w, h, LUT, in_range="tv" if is_plate else "full")
            pre = f"{ship_pre},format=rgb24"      # stat probes: 8-bit PNGs of the picture that ships
            tail = f",{HOUSE_TAIL}"
        ls = slot.get("light_state")
        if grade_mode in ("natural", "house") and isinstance(ls, dict):
            ls = {k: v for k, v in ls.items() if k == "gain"}   # keep a declared lights-off; drop colour washes
        light = light_state_filter(ls)
        if light:
            pre = f"{pre},{light}"
            ship_pre = f"{ship_pre},{light}" if ship_pre else None
        # 2. GRADE — measured, not nominal: reference vs OUR cropped+LUT'd frames at in/mid/out,
        #    solved per shot (tone curve + white-balance gain + saturation, guarded) by onetoone.grade.
        #    brain/grade_match.json is NOT used: it was solved on cast_v006's clips.
        ref_paths, cand_paths = [], []
        for tag, f in grade.sample_frames(shot):
            rp = stat_dir / f"{shot['slot']}_{tag}_ref.png"; cp = stat_dir / f"{shot['slot']}_{tag}_cand.png"
            grade.extract_reference(reference, f, rp)
            t = t0 + (f - int(shot["in"])) / FPS
            _run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.4f}", "-i", str(master), "-vf",
                  f"{pre},{COVER.format(w=w, h=h)},scale={grade.STAT_SIZE[0]}:{grade.STAT_SIZE[1]}",
                  "-frames:v", "1", str(cp)])
            ref_paths.append(rp); cand_paths.append(cp)
        mid_f = grade.sample_frames(shot)[1][1]
        nat_c = stat_dir / f"{shot['slot']}_mid_cand_native.png"; nat_r = stat_dir / f"{shot['slot']}_mid_ref_native.png"
        _run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t0 + (mid_f - int(shot['in'])) / FPS:.4f}", "-i", str(master),
              "-vf", f"{pre},{COVER.format(w=w, h=h)}", "-frames:v", "1", str(nat_c)])
        grade.extract_reference(reference, mid_f, nat_r, size=None)
        if grade_mode == "house":
            # The house grade as approved reels actually shipped it: the hash-locked LC-709 LUT and NOTHING else — no curves,
            # no eq, no per-shot solve, no luma pull, no ramp. One look on every shot. Builds that
            # carried NATURAL_GRADE (contrast 1.06, saturation 1.22, a global shadow lift) plus per-shot
            # reference-matched exposure read as inconsistent; none of that exists in an approved reel.
            sol = {"mode": "house", "why": "LC-709 LUT only, creative at defaults (approved-reel recipe); no pull, no ramp"}
            gf = "null"
        elif grade_mode == "natural":
            sol = {"mode": "natural", "why": "house NATURAL_GRADE; colour solve off, luma pull + ramp kept"}
            gf = NATURAL_GRADE
        else:
            cand_stats = grade.shot_stats(cand_paths, native_probe=nat_c)
            ref_stats = grade.shot_stats(ref_paths, native_probe=nat_r)
            sol = grade.solve(cand_stats, ref_stats)
            gf = grade.filter_from_solution(sol, cand_stats, ref_stats)
        # 3. EXPOSURE — the solver's tone clamp stops well short on shots that are far brighter
        #    than the reference. Mean luma is the most basic
        #    1:1 property, so pull the rest of the way with a measured gamma curve (darken only).
        tgt = slot.get("luma_target")
        target = float(tgt["target_mean_luma"]) if isinstance(tgt, dict) and tgt.get("target_mean_luma") else None
        probe_png = stat_dir / f"{shot['slot']}_mid_probe.png"
        _run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t0 + (mid_f - int(shot['in'])) / FPS:.4f}", "-i", str(master),
              "-vf", f"{pre},{gf},{COVER.format(w=w, h=h)},scale={grade.STAT_SIZE[0]}:{grade.STAT_SIZE[1]},format=rgb24",
              "-frames:v", "1", str(probe_png)])
        # Exposure is NOT colour. The grade complaint was about the colour work — casts,
        # washes, per-shot white balance. Dropping the luma pull with it left 7 of 12 shots +30 to
        # +107 codes over the reference with 8-22% of pixels clipped. The pull is a
        # monotone gamma toward the reference's own mean and carries no colour, so natural mode keeps it.
        tgt_luma = target if target is not None else frame_stats(nat_r)[0]
        if grade_mode == "house":
            pull = {"filter": None, "ours": round(frame_stats(probe_png)[0], 1) if probe_png.exists() else None,
                    "target": round(tgt_luma, 1), "gamma": 1.0, "skipped": "house mode: no exposure matching"}
        else:
            pull = luma_pull(probe_png, tgt_luma)
        pull_filters = [pull["filter"]] if pull["filter"] else []
        if pull["filter"]:
            gf = f"{gf},{pull['filter']}"          # RGB for the probes; swapped for luma-only below
        # A SECOND gated pass where one clamped curve could not reach the target (v013 audit:
        # PULL_GAMMA_MIN bound on S06/S08/S09/S11/S12, leaving S09 70 codes short and its ramp
        # saturated at RAMP_MAX with no headroom left for shape). Two 0.45 curves compose to ~0.20
        # effective and hold the shadows far better than one deep curve.
        if pull.get("after") is not None and pull["after"] > tgt_luma + PULL_PASS2_OVER:
            probe2 = stat_dir / f"{shot['slot']}_mid_probe2.png"
            _run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t0 + (mid_f - int(shot['in'])) / FPS:.4f}",
                  "-i", str(master), "-vf",
                  f"{pre},{gf},{COVER.format(w=w, h=h)},scale={grade.STAT_SIZE[0]}:{grade.STAT_SIZE[1]},format=rgb24",
                  "-frames:v", "1", str(probe2)])
            pull2 = luma_pull(probe2, tgt_luma, gamma_min=PULL_PASS2_GAMMA_MIN)
            # If even the floored second gamma cannot reach the target, the excess is not spread
            # through the picture — it is in the highlights. Compress those instead of bending the
            # whole curve: it is the difference between a dusk sky and a black subject (S09).
            if pull2.get("after") is not None and pull2["after"] > tgt_luma + PULL_PASS2_OVER:
                pull2 = highlight_pull(probe2, tgt_luma)
            if pull2["filter"]:
                gf = f"{gf},{pull2['filter']}"
                pull_filters.append(pull2["filter"])
            pull = {**pull, "pass2": pull2}
        if pull_filters and grade_mode == "natural":
            # Natural mode: the house grade's colour is sacred. Re-express every pull as luma-only.
            base = NATURAL_GRADE
            last = (pull.get("pass2") or pull)
            gf = f"{base},{luma_only(','.join(pull_filters), pull.get('ours') or 0.0, last.get('after'))}"
            pull = {**pull, "luma_only": True, "chroma_follow": CHROMA_FOLLOW}
        seg = seg_dir / f"{shot['slot']}.mp4"
        red = redact_filter(slot.get("redact"), n)
        # house: the 16-bit RGB picture goes through the colorspace tail BEFORE the redaction patch and the
        # final format, so nothing auto-converts it to YUV with a guessed matrix on the way out.
        vf = f"{ship_pre or pre},{gf},{COVER.format(w=w, h=h)},fps={FPS_STR}{tail}" + (f",{red}" if red else "") + ",setsar=1,format=yuv420p"
        _run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t0:.4f}", "-i", str(master), "-an",
              "-vf", vf, "-frames:v", str(n), "-c:v", "libx264", "-preset", "medium", "-crf", "10",
              "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709", "-color_trc", "bt709",
              "-color_primaries", "bt709", "-video_track_timescale", str(TIMESCALE), str(seg)])
        sanity = chroma_sanity(seg, master, t0 + (n // 2) / FPS, n, stat_dir, shot["slot"])
        # 4. SHAPE — the constant grade matches the middle frame; the reference can still ramp
        #    inside the shot (a climb out of a blackout, a settle after a cut). Measure both per
        #    frame and, when they diverge by more than RAMP_TOL anywhere, re-cut with a per-frame
        #    brightness offset. The shift is logged so the ledger can quote it.
        ramp: Dict[str, Any] = {}
        cmask = caption_masks(brain, range(int(shot["in"]), int(shot["in"]) + n))
        ours_pf = _frame_means(seg, stat_dir / f"{shot['slot']}_pf_ours", masks=cmask)
        ref_pf = _frame_means(reference, stat_dir / f"{shot['slot']}_pf_ref", start=int(shot["in"]), count=n, masks=cmask)
        declared_exposure = isinstance(slot.get("light_state"), dict) and slot["light_state"].get("gain") is not None
        ramp = {} if (declared_exposure or grade_mode == "house") else luma_ramp(ours_pf, ref_pf)   # house: no per-frame matching either
        if ramp:
            # Closed loop, KEEP THE BEST (v011f: blindly taking the last pass overshot S01/S05/S07
            # and worsened steps). Every candidate — including the uncorrected cut — is scored on
            # mean |ref-ours| plus half the worst unmatched frame-to-frame step; the best is shipped.
            def score(pf):
                m = min(len(pf), len(ref_pf))
                err = sum(abs(ref_pf[i] - pf[i]) for i in range(m)) / m
                step = max((abs((pf[i + 1] - pf[i]) - (ref_pf[i + 1] - ref_pf[i])) for i in range(m - 1)), default=0.0)
                return err + 1.0 * step    # v011h audit: steps visible side by side (S11 f157, S07 f54) — weigh them fully
            def cut_with(delta):
                vf2 = vf if delta is None else vf.replace(f",fps={FPS_STR}", f",fps={FPS_STR},{ramp_filter(delta)}", 1)
                _run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t0:.4f}", "-i", str(master), "-an",
                      "-vf", vf2, "-frames:v", str(n), "-c:v", "libx264", "-preset", "medium", "-crf", "10",
                      "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709", "-color_trc", "bt709",
                      "-color_primaries", "bt709", "-video_track_timescale", str(TIMESCALE), str(seg)])
            cands = [(score(ours_pf), None, ours_pf)]
            total = list(ramp["delta"])
            prev_pf, prev_total = ours_pf, [0.0] * len(total)
            for rpass in range(4):
                cut_with(total)
                after = _frame_means(seg, stat_dir / f"{shot['slot']}_pf_ours_ramped{rpass}", masks=cmask)
                cands.append((score(after), list(total), after))
                more = luma_ramp(after, ref_pf, tol=RAMP_TOL_PASS2, smooth=False)
                if not more:
                    break
                # MEASURED GAIN (v011i: offsets land ~70%, so a fixed damping left S11's f155 spike
                # and f157 step in place): how much of the last change actually arrived, per frame.
                ratios = sorted((after[i] - prev_pf[i]) / (total[i] - prev_total[i])
                                for i in range(min(len(after), len(total)))
                                if abs(total[i] - prev_total[i]) > 0.5)
                gain = max(0.4, min(1.2, ratios[len(ratios) // 2])) if ratios else 0.7
                prev_pf, prev_total = after, list(total)
                total = [max(-RAMP_MAX, min(RAMP_MAX, a + b / gain)) for a, b in zip(total, more["delta"])]
            best = min(cands, key=lambda c: c[0])
            if best is not cands[-1]:
                cut_with(best[1])
            after = best[2]
            ramp = {"delta": None if best[1] is None else [round(x, 1) for x in best[1]],
                    "max_abs": None if best[1] is None else round(max(abs(x) for x in best[1]), 1),
                    "candidates": [round(c[0], 2) for c in cands], "chosen": cands.index(best),
                    "residual_after": round(max(abs(ref_pf[i] - after[i]) for i in range(min(len(after), len(ref_pf)))), 1),
                    "mean_after": round(sum(after) / len(after) - sum(ref_pf[:len(after)]) / len(after), 1)}
        elif declared_exposure:
            ramp = {"skipped": "declared light_state exposure (e.g. lights off) — not auto-lifted"}
        # what shipped vs the reference, mid-frame, for the residual log (mean luma 0-255)
        graded_png = stat_dir / f"{shot['slot']}_mid_graded.png"
        _run([FFMPEG, "-y", "-loglevel", "error", "-i", str(seg), "-vf", f"select=eq(n\\,{n // 2})", "-vsync", "0",
              "-frames:v", "1", str(graded_png)])
        luma_ours = round(frame_stats(graded_png)[0], 1); luma_ref = round(frame_stats(nat_r)[0], 1)
        facts["shots"].append({"slot": shot["slot"], "stem": slot["stem"], "in": shot["in"], "out": shot["out"],
                               "frames": n, "master_in_s": t0, "display_wh": [mw, mh], "crop": crop, "crop_effective": crop_eff, "light_state": light or None, "redact": slot.get("redact"), "luma_ramp": ramp or None,
                               "grade": {"mode": sol["mode"], "why": sol.get("why"), "strength": sol.get("strength"),
                                         "cast_strength": sol.get("cast_strength"), "gains": sol.get("gains"),
                                         "sat": sol.get("sat"), "filter": gf, "luma_pull": pull},
                               "chroma_sanity": sanity, "luma_mid": {"ours": luma_ours, "ref": luma_ref, "target": slot.get("luma_target", {}).get("target_mean_luma")
                                            if isinstance(slot.get("luma_target"), dict) else None}})
        facts["total_frames"] += n
    # concat (demuxer, stream copy — all segments share codec/geometry/clock)
    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{seg_dir / (s['slot'] + '.mp4')}'\n" for s in facts["shots"]))
    silent = work / "picture_silent.mp4"
    _run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(silent)])
    # audio track, bit-exact stream copy. NO -shortest: a track that ends before the picture
    # (8.011s audio vs 8.050s video) had -shortest trim the picture to 191/193 frames.
    picture = work / "picture.mp4"
    audio_source = Path(audio) if audio is not None else reference
    facts["audio_source"] = {"path": str(audio_source), "kind": "licensed_track" if audio is not None else "reference_rights_held"}
    _run([FFMPEG, "-y", "-loglevel", "error", "-i", str(silent), "-i", str(audio_source),
          "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
          "-movflags", "+faststart", str(picture)])
    facts["picture"] = str(picture)
    return facts


class UnreadableCaption(RuntimeError):
    """A caption's ink has too little contrast against OUR bed frame — recast, don't ship."""


def _part_layers(state: Mapping[str, Any],
                 size: Tuple[int, int]) -> List[Tuple[Mapping[str, Any], Image.Image]]:
    """One settled layer PER PART, in the same z-order and with the same anchoring decisions
    typeset_state makes for the whole state (ornate behind, plain on top; the ornate word is
    anchored away from the plain line). Compositing these in order reproduces typeset_state's
    layer exactly — they are split only so each part can carry its own entry device.
    """
    parts = list(state.get("parts", []))
    ordered = sorted(parts, key=lambda p: 0 if _PART_FACE.get(str(p.get("part"))) == "ornate" else 1)
    plain_boxes = [_part_box(p) for p in parts if _PART_FACE.get(str(p.get("part"))) == "plain"]
    plain_box = plain_boxes[0] if plain_boxes else None
    out = []
    for p in ordered:
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        typeset_part(layer, p, DEFAULT_FACES, plain_box=plain_box)
        out.append((p, layer))
    return out


def _picture_frame(picture: Path, work: Path, f: int) -> Image.Image:
    """Frame f of OUR picture (extracted once, all frames, on first use)."""
    d = work / "picture_frames"
    if not (d / "000.png").exists():
        d.mkdir(parents=True, exist_ok=True)
        _run([FFMPEG, "-y", "-loglevel", "error", "-i", str(picture), "-vsync", "0", "-start_number", "0", str(d / "%03d.png")])
    return Image.open(d / f"{f:03d}.png").convert("RGB")


def behind_subject(layer: Image.Image, bed: Image.Image, spec: Mapping[str, Any]) -> Image.Image:
    """TEXT BEHIND THE SUBJECT (e.g. a walking figure's head passes IN FRONT of a word in the
    reference; drawing the word on top would put it across the subject's head). The caption sits on a bright sky and the subject is darker than it, so the
    matte is the bed's own luma: ink is kept where the bed is sky-bright and removed where it is
    subject-dark, with a soft ramp lo→hi (mean-luma codes, 0–255) and a 2 px feather."""
    import numpy as np
    from PIL import ImageFilter
    lo, hi = float(spec["luma_lo"]), float(spec["luma_hi"])
    L = np.asarray(bed.convert("L").filter(ImageFilter.GaussianBlur(2)), dtype=np.float32)
    keep = np.clip((L - lo) / max(1.0, hi - lo), 0.0, 1.0)
    r, g, b, a = layer.split()
    a2 = Image.fromarray((np.asarray(a, dtype=np.float32) * keep).astype("uint8"))
    return Image.merge("RGBA", (r, g, b, a2))


def with_shadow(layer: Image.Image, spec: Mapping[str, Any]) -> Image.Image:
    """A soft dark halo UNDER the ink, from the ink's own alpha (blurred, offset, darkened).

    Why: one flat caption colour is the house style, but some
    beds are bright sun/glass where white ink measures 9-27 against a need of 25-106 and simply
    vanishes. A shadow keeps the ink one colour and plain — it changes nothing about the letters,
    only what is behind them — where a bed fix would mean recasting the shot. Invisible on the dark
    beds, load-bearing on the bright ones.
    """
    radius = float(spec.get("radius", 12))
    opacity = max(0.0, min(1.0, float(spec.get("opacity", 0.55))))
    dx, dy = (int(v) for v in spec.get("offset", (0, 2)))
    grow = int(spec.get("grow", 2))
    alpha = layer.getchannel("A")
    if grow:
        alpha = alpha.filter(ImageFilter.MaxFilter(2 * grow + 1))
    alpha = alpha.filter(ImageFilter.GaussianBlur(radius)).point(lambda v: int(v * opacity))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.putalpha(alpha)
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.alpha_composite(shadow, (max(0, dx), max(0, dy)))
    out.alpha_composite(layer)
    return out


def state_shadow(state: Mapping[str, Any], cast_shadow: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The shadow THIS caption state wears.

    The ruling is "where flat ink fails on our bed, apply the directional shadow" — not "shadow
    everything". Shadowing every state from the cast-level spec gave navy ink on a
    bright sky got a black halo the reference does not have, and the halo then made the navy
    FAIL the gate (dark ink on a dark ring), so two states were flipped to white. A state may
    now say `"shadow": false` (wear none: it reads flat, as the reference sets it) or carry its
    own spec dict; a state that says nothing keeps the cast-level spec exactly as before."""
    if "shadow" not in state:
        return cast_shadow
    own = state.get("shadow")
    if not own:
        return None
    return own if isinstance(own, Mapping) else cast_shadow


def rasterise_captions(brain: Path, work: Path, *, frames: int, size: Tuple[int, int],
                       picture: Path | None = None, reference: Path | None = None,
                       allow_low_contrast: bool = False,
                       ink_override: Tuple[int, int, int] | None = None,
                       ink_shadow: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    """One RGBA PNG per frame: the union of every part live on that frame, with the reference's
    measured entry device applied to each (onetoone.devices + brain/caption_devices.json).

    A part with no measured device renders exactly as before: its static settled layer. With
    `picture`, every state is readability-checked against OUR mid-frame bed and the render
    REFUSES on a failure unless allow_low_contrast. The gate is
    judged on the SETTLED layer, which is the caption at its most legible and the only frame
    whose geometry the study measured.
    """
    if not gate_bypassed() and not (work / ".preflight-ok").exists():
        raise PreflightRequired("rasterise_captions needs a gated cut_picture in the same work dir first (audit-loop2 H11)")
    states = json.loads((brain / "captions.plaintext.json").read_text())["states"]
    cast_shadow = ink_shadow
    if ink_override:
        # ONE caption colour (house style: every state in the same ink). The study's per-state inks stay in the brain as reference facts; only the render
        # changes. Readability is still judged against the reference with the SAME ink.
        for st in states:
            for p in st["parts"]:
                p["ink_rgb"] = [int(c) for c in ink_override]; p.pop("ink_rgb_approx", None)
    cap_dir = work / "captions"; cap_dir.mkdir(parents=True, exist_ok=True)
    device_table = dv.load_devices(brain)
    layers: Dict[str, Image.Image] = {}
    parts_z: Dict[str, List[Tuple[Mapping[str, Any], Image.Image]]] = {}
    reports = []
    unreadable = []
    for st in states:
        layer, rep = typeset_state(st, canvas=size)
        layers[st["id"]] = layer
        if not rep.get("own_ink", {}).get("ok", True):
            # v010 audit HARD-1: a plain word typeset inside the script word is unreadable no
            # matter how good the bed is. This is a typesetting fault, never a casting one.
            unreadable.append(f"{st['id']}: plain line sits on our own script ink "
                              f"({rep['own_ink']['overlap']:.0%} of its ink, max {rep['own_ink']['max']:.0%})")
        parts_z[st["id"]] = _part_layers(st, size)
        if picture is not None:
            mid = (int(st["in"]) + int(st["out"])) // 2
            beds = work / "beds"; beds.mkdir(exist_ok=True)
            bed_png, ref_png = beds / f"{st['id']}_ours.png", beds / f"{st['id']}_ref.png"
            for src, png in ((picture, bed_png), (reference, ref_png)):
                _run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-vf", f"select=eq(n\\,{mid})",
                      "-vsync", "0", "-frames:v", "1", str(png)])
            bed_img = Image.open(bed_png).convert("RGBA")
            shaded = None
            ink_shadow = state_shadow(st, cast_shadow)
            if ink_shadow:
                # contrast is judged on the bed the viewer gets (our frame WITH this caption's shadow),
                # in a ring around the letters; clutter stays on the raw footage
                if bed_img.size != layer.size:
                    bed_img = bed_img.resize(layer.size)
                shaded = bed_img.copy(); shaded.alpha_composite(with_shadow(layer, ink_shadow))
            rep["readability"] = readability(bed_img, rep, st, ref_bed=Image.open(ref_png).convert("RGB"),
                                             ink_layer=layer if ink_shadow else None, shaded_bed=shaded)
            if not rep["readability"]["ok"]:
                unreadable.append(f"{st['id']}: " + ", ".join(
                    f"{p['text']!r} " + ("contrast %s/%s" % (p["contrast"], p["need"]) if not p["contrast_ok"]
                                         else "clutter sd %s vs ref %s" % (p["bed_sd"], p["ref_bed_sd"]))
                    for p in rep["readability"]["parts"] if not p["ok"]))
        reports.append(rep)
    if unreadable and not allow_low_contrast:
        raise UnreadableCaption("caption(s) vanish into our bed — recast the shot: " + " | ".join(unreadable))
    empty = Image.new("RGBA", size, (0, 0, 0, 0))
    live_frames = 0
    for f in range(frames):
        active = [st for st in states if int(st["in"]) <= f <= int(st["out"])]
        if not active:
            empty.save(cap_dir / f"caption-{f:03d}.png"); continue
        frame = empty.copy()
        for st in active:
            st_layer = empty.copy()
            for part, sharp in parts_z[st["id"]]:
                st_layer.alpha_composite(dv.layer_for_frame(st, part, f, sharp, device_table))
            ink_shadow = state_shadow(st, cast_shadow)
            if ink_shadow:
                # AFTER the entry device, never before: shadowing the
                # settled layer first fed the device a different alpha and merged c03's two words
                # into one arrival. Taking it from what is actually on screen this frame keeps every
                # entry exactly as v011 played it, and the shadow ramps in with the ink.
                st_layer = with_shadow(st_layer, ink_shadow)
            if st.get("behind_subject") and picture is not None:
                st_layer = behind_subject(st_layer, _picture_frame(picture, work, f), st["behind_subject"])
            frame.alpha_composite(st_layer)
        frame.save(cap_dir / f"caption-{f:03d}.png"); live_frames += 1
    return {"dir": str(cap_dir), "states": len(states), "frames_with_captions": live_frames,
            "devices": ("STATIC — no brain/caption_devices.json" if not device_table else
                        "MEASURED — entry blur / opacity ramp / scale drift replayed per part"),
            "device_summary": dv.summary(device_table),
            "unreadable": unreadable, "typeset": reports}


from rf_paths import REEL_HOME  # noqa: E402

GATE_DIR = REEL_HOME / ".preflight-ok"
GATE_MAX_AGE_S = 6 * 3600


class PreflightRequired(RuntimeError):
    pass


def gate_token(cast_json: Path) -> Path:
    import hashlib
    return GATE_DIR / (hashlib.sha256(cast_json.read_bytes()).hexdigest()[:16] + ".json")


LESSONS_JSON = REEL_HOME / "lessons.json"


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def lessons_sha(path: Path | None = None) -> str:
    """sha256 of the LESSON SET (the `lessons` list), not the file bytes: the harvester rewrites
    lessons.json every 600 s for its `seen`/inbox bookkeeping, and
    a token must only die when a lesson is added or changed."""
    import hashlib
    path = path or LESSONS_JSON
    if not path.exists():
        return ""
    try:
        doc = json.loads(path.read_text())
        body = doc.get("lessons", doc) if isinstance(doc, dict) else doc
    except Exception:
        return _sha(path)
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def gate_bypassed() -> bool:
    """The ONLY bypass: REEL_NO_GATE=1 in the environment (throwaway local tests)."""
    return os.environ.get("REEL_NO_GATE") == "1"


def require_preflight(cast_json: Path) -> None:
    """The learning loop's technical tooth. preflight.py
    writes a token {cast_sha256, lessons_sha256, row, at} when it passes; render() refuses unless the
    token exists, is younger than GATE_MAX_AGE_S, matches the cast's bytes AND matches lessons.json —
    so a NEW LESSON invalidates every outstanding token (that is the intended
    recursion). Bypass only via REEL_NO_GATE=1 in the environment, never a casual flag."""
    tok = gate_token(cast_json)
    why = None
    if not tok.exists():
        why = "no token"
    elif (time.time() - tok.stat().st_mtime) > GATE_MAX_AGE_S:
        why = f"token older than {GATE_MAX_AGE_S // 3600} h"
    else:
        try:
            d = json.loads(tok.read_text())
        except Exception:
            d = {}
        if d.get("cast_sha256") != _sha(cast_json):
            why = "token does not match the cast bytes"
        elif d.get("lessons_sha256") != lessons_sha():
            why = "lessons.json changed since preflight (a new lesson invalidates every token)"
    if why:
        raise PreflightRequired(
            f"{why} for {cast_json.name}: run `cd reel-production-tools && python3 -m onetoone.preflight --row <NN> --cast {cast_json}`")


class LicensedAudioRequired(RuntimeError):
    """No audio track you hold rights to was named. Supply one; never default to the reference's."""


def render(brain: Path, cast_json: Path, reference: Path, out_mp4: Path, work: Path,
           *, size: Tuple[int, int] = (1916, 1078), crf: int = 20, allow_low_contrast: bool = False,
           gate: bool = True, audio: "Path | None" = None,
           reference_audio_rights_held: bool = False) -> Dict[str, Any]:
    if not gate and not gate_bypassed():
        raise PreflightRequired("render(gate=False) is only honoured with REEL_NO_GATE=1 in the environment (audit-loop2 H11)")
    if gate:
        require_preflight(cast_json)
    # Audio policy (default: licensed_track_required). The reference is analysed for timing
    # (beat map, cut grid) either way; what is MUXED is a track the user holds rights to.
    if audio is None and not reference_audio_rights_held:
        raise LicensedAudioRequired(
            "no audio track: pass audio=<licensed track> (CLI --audio PATH), cut to the reel's length. "
            "Only if you hold the rights to the reference's own audio, opt in with "
            "reference_audio_rights_held=True (CLI --reference-audio-rights-held).")
    if audio is not None and not Path(audio).is_file():
        raise LicensedAudioRequired(f"audio track not found: {audio}")
    work.mkdir(parents=True, exist_ok=True)
    ffdir = os.path.dirname(FFMPEG)  # variant_render calls bare `ffmpeg`
    if ffdir and ffdir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = ffdir + os.pathsep + os.environ.get("PATH", "")
    pic = cut_picture(brain, cast_json, reference, work, size=size, audio=audio)
    frames = pic["total_frames"]
    cast_doc = json.loads(cast_json.read_text())
    ink = cast_doc.get("caption_ink")
    caps = rasterise_captions(brain, work, frames=frames, size=size, picture=Path(pic["picture"]),
                              reference=reference, allow_low_contrast=allow_low_contrast,
                              ink_override=tuple(ink) if ink else None,
                              ink_shadow=cast_doc.get("caption_shadow"))
    composite_out = work / "composite.mp4"
    vr.composite(Path(pic["picture"]), composite_out, width=size[0], height=size[1], fps=FPS_STR,
                 frames=frames, time_base_denominator=TIMESCALE, captions=Path(caps["dir"]),
                 codec="libx264", crf=crf)
    final_crf = crf
    if composite_out.stat().st_size > WEB_CAP:
        for c in (22, 24, 26, 28):
            vr.encode_delivery(composite_out, out_mp4, crf=c, fps=FPS_STR, frames=frames, time_base_denominator=TIMESCALE)
            final_crf = c
            if out_mp4.stat().st_size <= WEB_CAP:
                break
    else:
        shutil.copy2(composite_out, out_mp4)
    facts = {"output": str(out_mp4), "bytes": out_mp4.stat().st_size, "frames": frames, "crf": final_crf,
             # audit-v019: "rendered WITHOUT --allow-low-contrast" could not be verified from any
             # artifact. Every report now records how it was invoked.
             "invocation": {"argv": list(sys.argv), "allow_low_contrast": bool(allow_low_contrast), "gate": bool(gate),
                            "audio_policy": "licensed_track_required" if audio is not None else "reference_audio_rights_held",
                            "audio": str(audio) if audio is not None else None,
                            "REEL_NO_GATE": os.environ.get("REEL_NO_GATE"), "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
             "picture": pic, "captions": {k: v for k, v in caps.items() if k != "typeset"},
             # per-part readability numbers incl. the raw (shadow-free) contrast and the halo ratio:
             # the card needs them and audit-v019 found the report dropped them
             "caption_readability": [{"state": r.get("id") or r.get("state"), "parts": (r.get("readability") or {}).get("parts")}
                                     for r in caps.get("typeset", [])]}
    facts["eye_sheet"] = str(eye_sheet(brain, reference, out_mp4, work / "eye", size=size))
    (work / "render_report.json").write_text(json.dumps(facts, indent=1))
    return facts


def eye_sheet(brain: Path, reference: Path, ours: Path, outdir: Path, *, size: Tuple[int, int],
              picks: Tuple[str, ...] = ("c02", "c04a", "c05c", "c06", "c07c")) -> Path:
    """Every render ends with this: the hook + five caption states, reference | ours, same frame
    numbers, half-scale (judge detail on the native *_ref/_ours.png, not the sheet)."""
    from PIL import ImageDraw
    outdir.mkdir(parents=True, exist_ok=True)
    states = {s["id"]: s for s in json.loads((brain / "captions.plaintext.json").read_text())["states"]}
    frames = [("hook", 5)] + [(sid, (int(states[sid]["in"]) + int(states[sid]["out"])) // 2) for sid in picks if sid in states]
    W, H = size; w, h = W // 2, H // 2; lab = 22; per_row = 2
    rows = (len(frames) + per_row - 1) // per_row
    sheet = Image.new("RGB", (w * 2 * per_row, (h + lab) * rows), (16, 16, 16)); d = ImageDraw.Draw(sheet)
    for i, (name, f) in enumerate(frames):
        pair = []
        for tag, src in (("ref", reference), ("ours", ours)):
            png = outdir / f"{name}_{f}_{tag}.png"
            _run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-vf", f"select=eq(n\\,{f})", "-vsync", "0",
                  "-frames:v", "1", str(png)])
            pair.append(Image.open(png).convert("RGB").resize((w, h)))
        x = (i % per_row) * w * 2; y = (i // per_row) * (h + lab)
        sheet.paste(pair[0], (x, y + lab)); sheet.paste(pair[1], (x + w, y + lab))
        d.text((x + 6, y + 4), f"{name} f{f}   ref | ours", fill=(255, 210, 120))
    out = outdir / "EYE_SHEET.png"; sheet.save(out)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("brain"); ap.add_argument("cast"); ap.add_argument("reference"); ap.add_argument("out_mp4"); ap.add_argument("work")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--allow-low-contrast", action="store_true", help="ship even if a caption vanishes into our bed (disclose!)")
    ap.add_argument("--audio", type=Path, default=None,
                    help="licensed audio track to mux (default policy: licensed_track_required); cut it to the reel's length")
    ap.add_argument("--reference-audio-rights-held", action="store_true",
                    help="opt-in: mux the reference's own audio stream. Use ONLY if you hold the rights to it")

    ap.add_argument("--no-gate", action="store_true", help="ignored unless REEL_NO_GATE=1 is set in the environment (throwaway local test only)")
    a = ap.parse_args()
    facts = render(Path(a.brain), Path(a.cast), Path(a.reference), Path(a.out_mp4), Path(a.work), crf=a.crf,
                   allow_low_contrast=a.allow_low_contrast, gate=not (a.no_gate and os.environ.get("REEL_NO_GATE") == "1"),
                   audio=a.audio, reference_audio_rights_held=a.reference_audio_rights_held)
    print(json.dumps({k: v for k, v in facts.items() if k != "picture"}, indent=1))
    print("shots:", [(s["slot"], s["frames"]) for s in facts["picture"]["shots"]], "total", facts["frames"])
