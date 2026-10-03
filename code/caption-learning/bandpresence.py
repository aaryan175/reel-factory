"""GEOMETRY-FREE CAPTION PRESENCE PROBE  (W5b)

Some caption contracts record words and timing but NO placement geometry at all (every
state is text + frame span, `proven_face: null`, `render_allowed: false`).
`sweep.presence_state` needs a bbox, so those reels would come back UNMEASURABLE on the
simplest review question - "are the captions there at all?"

This module answers that question WITHOUT a contract bbox, by deriving the caption band
from the REFERENCE's own pixels:

  1. the contract declares which frames are BLANK (no caption showing);
  2. for a sample of caption-bearing frames, mark pixels at the extreme of the luma range
     (near-255 for light ink, near-0 for dark ink);
  3. a pixel belongs to the caption band if it goes extreme while a caption is up and
     NEVER goes extreme on any blank frame - that difference removes the footage, which is
     the only thing both sets have in common;
  4. the union bbox of the surviving components IS the reference's caption band;
  5. measure ink fraction inside that band, on the reference and on the delivered file,
     at the same zero-based frame indices.

HONESTY RULES (same as every other gate here):
  * verdict in {PASS, FAIL, UNMEASURABLE}, always with value(s), sample_size, evidence;
  * if step 4 yields no band, or the band is degenerate, the answer is UNMEASURABLE -
    never PASS.  A probe that found nothing to look at has not verified anything;
  * if the REFERENCE itself carries no ink in its own band at a state's frames, that state
    is UNMEASURABLE, not FAIL - there is nothing for the delivery to be missing;
  * frame indices are ZERO-BASED DECODED ORDER (readback.extract_frame -> ffmpeg
    select eq(n,K)); no fps shorthand anywhere.

CLI:
    python3 bandpresence.py --reference REF.mp4 --delivered DEL.mp4 \
        --contract contract.json --out-dir /path/to/evidence
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Sequence

import cv2
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import readback  # noqa: E402

# --- thresholds ---------------------------------------------------------------------------
LIGHT_LUMA = 245          # "this pixel is at the top of the range" - caption ink, not footage
DARK_LUMA = 15
MIN_COMPONENT_PX = 120    # a band component smaller than this is noise, not a letterform
MIN_BAND_PX = 400         # below this there is no band to speak of -> UNMEASURABLE
MAX_BAND_AREA_FRACTION = 0.60   # a "band" covering most of the raster is a failed derivation
MIN_REFERENCE_FRACTION = 0.002  # reference ink floor inside its own band
MIN_RATIO = 0.25          # delivered must reach this share of the reference's ink density


def _luma(bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def _extreme(gray: np.ndarray, polarity: str) -> np.ndarray:
    return (gray >= LIGHT_LUMA) if polarity == "light" else (gray <= DARK_LUMA)


def _state_probe_frames(states: Sequence[Dict], limit: int) -> List[int]:
    """One mid-span frame per state, spread evenly across the whole program."""
    mids = []
    for s in states:
        a = int(s.get("start_frame", 0))
        b = int(s.get("end_frame_exclusive", a + 1))
        if b > a:
            mids.append((a + b) // 2)
    mids = sorted(set(mids))
    if len(mids) <= limit:
        return mids
    step = len(mids) / float(limit)
    return [mids[int(i * step)] for i in range(limit)]


def _peak_run(profile: np.ndarray, frac_of_peak: float) -> (int, int):
    """The contiguous run around the profile's peak that stays above frac_of_peak*peak."""
    if profile.size == 0:
        return 0, 0
    peak = float(profile.max())
    if peak <= 0:
        return 0, 0
    thr = frac_of_peak * peak
    k = int(np.argmax(profile))
    lo = k
    while lo - 1 >= 0 and profile[lo - 1] >= thr:
        lo -= 1
    hi = k
    while hi + 1 < profile.size and profile[hi + 1] >= thr:
        hi += 1
    return lo, hi + 1


def _band_from_profile(score: np.ndarray, polarity: str, n_cap: int, n_blank: int,
                       out_dir: str, frac_of_peak: float = 0.20) -> Dict:
    """Locate the caption band as the concentration of repeat-firing pixels.

    A union bbox over every surviving component does NOT work: one specular highlight in a
    corner stretches the box across the whole raster (observed: a box covering ~98% of the
    raster, correctly refused as a failed derivation).  A caption band is instead the place
    where the SAME pixels fire on many caption frames, so the row/column mass profile of
    `score` has a clear peak there.  Rows first (captions are horizontal bands), then the
    column extent is measured inside those rows only.
    """
    if score is None or score.size == 0:
        return {"polarity": polarity, "band_px": 0, "components": 0,
                "n_caption_frames": n_cap, "n_blank_frames": n_blank, "box": None}
    # a pixel must fire on at least two sampled caption frames to count as band evidence
    firm = (score >= 2.0)
    row_mass = firm.sum(axis=1).astype(np.float32)
    y0, y1 = _peak_run(row_mass, frac_of_peak)
    if y1 <= y0:
        return {"polarity": polarity, "band_px": 0, "components": 0,
                "n_caption_frames": n_cap, "n_blank_frames": n_blank, "box": None}
    col_mass = firm[y0:y1].sum(axis=0).astype(np.float32)
    x0, x1 = _peak_run(col_mass, frac_of_peak)
    if x1 <= x0:
        return {"polarity": polarity, "band_px": 0, "components": 0,
                "n_caption_frames": n_cap, "n_blank_frames": n_blank, "box": None}
    sub = firm[y0:y1, x0:x1].astype(np.uint8)
    n_lab, _, stats, _ = cv2.connectedComponentsWithStats(sub, 8)
    keep = [i for i in range(1, n_lab) if stats[i, cv2.CC_STAT_AREA] >= MIN_COMPONENT_PX]
    area = int(sub.sum())
    cand = {"polarity": polarity, "band_px": area, "components": len(keep),
            "n_caption_frames": n_cap, "n_blank_frames": n_blank,
            "box": [int(x0), int(y0), int(x1), int(y1)],
            "band_row_run": [int(y0), int(y1)], "band_col_run": [int(x0), int(x1)],
            "min_caption_frames_per_pixel": 2, "profile_frac_of_peak": frac_of_peak}
    png = os.path.join(out_dir, f"band-{polarity}.png")
    cv2.imwrite(png, (firm.astype(np.uint8) * 255))
    cand["mask_png"] = png
    return cand


MIN_PRESENCE_RATE = 0.80   # a caption band shows ink on ~every caption frame
MAX_BLANK_RATE = 0.25      # ...and on ~none of the caption-free frames


def _band_behaviour(box: Sequence[int], polarity: str,
                    cap_grays: Dict[int, np.ndarray],
                    blank_grays: Dict[int, np.ndarray]) -> Dict:
    """How often does this box carry ink when a caption IS up, vs when none is?"""
    x0, y0, x1, y1 = box

    def rate(grays):
        if not grays:
            return None, []
        vals = []
        for f, g in sorted(grays.items()):
            sub = g[y0:y1, x0:x1]
            vals.append((f, float(_extreme(sub, polarity).mean()) if sub.size else 0.0))
        hits = sum(1 for _, v in vals if v >= MIN_REFERENCE_FRACTION)
        return hits / float(len(vals)), vals

    pr, cap_vals = rate(cap_grays)
    br, blank_vals = rate(blank_grays)
    return {
        "presence_rate": None if pr is None else round(pr, 4),
        "blank_rate": None if br is None else round(br, 4),
        "caption_frame_ink_fractions": [round(v, 5) for _, v in cap_vals],
        "blank_frame_ink_fractions": [round(v, 5) for _, v in blank_vals],
    }


def derive_reference_band(reference: str, states: Sequence[Dict],
                          blank_frames: Sequence[int], out_dir: str,
                          state_samples: int = 20) -> Dict:
    """Derive the reference's caption band from its own pixels. Returns a gate-shaped dict."""
    os.makedirs(out_dir, exist_ok=True)
    out: Dict = {
        "probe": "derive_reference_band",
        "thresholds": {"light_luma_at_or_above": LIGHT_LUMA,
                       "dark_luma_at_or_below": DARK_LUMA,
                       "min_component_px": MIN_COMPONENT_PX,
                       "min_band_px": MIN_BAND_PX,
                       "max_band_area_fraction": MAX_BAND_AREA_FRACTION},
        "evidence": {"out_dir": out_dir},
    }
    blanks = sorted({int(f) for f in (blank_frames or [])})
    probes = _state_probe_frames(states, state_samples)
    out["blank_frames_used"] = blanks
    out["caption_frames_sampled"] = probes
    if not blanks:
        out.update(verdict="UNMEASURABLE", sample_size=0, band_xyxy=None,
                   reason=("the contract declares no blank frames, so there is no "
                           "caption-free control to subtract the footage with"))
        return out
    if not probes:
        out.update(verdict="UNMEASURABLE", sample_size=0, band_xyxy=None,
                   reason="no state carries a usable frame span")
        return out

    def _load(idx: int, tag: str) -> Optional[np.ndarray]:
        png = os.path.join(out_dir, f"_{tag}-f{idx}.png")
        try:
            readback.extract_frame(reference, idx, png)
        except Exception:
            return None
        img = cv2.imread(png)
        return None if img is None else _luma(img)

    # cache every decoded gray once - both polarity passes and the behaviour check use them
    cap_grays = {f: _load(f, "cap") for f in probes}
    blank_grays = {f: _load(f, "blank") for f in blanks}
    cap_grays = {k: v for k, v in cap_grays.items() if v is not None}
    blank_grays = {k: v for k, v in blank_grays.items() if v is not None}

    candidates: List[Dict] = []
    for polarity in ("light", "dark"):
        cap_count = None
        for g in cap_grays.values():
            e = _extreme(g, polarity).astype(np.int32)
            cap_count = e if cap_count is None else (cap_count + e)
        blank_hit = None
        for g in blank_grays.values():
            e = _extreme(g, polarity)
            blank_hit = e if blank_hit is None else (blank_hit | e)
        if cap_count is None or blank_hit is None:
            continue
        # Pixels that go extreme while a caption is up, minus every pixel that ever goes
        # extreme on a caption-free frame.  `score` counts HOW MANY caption frames each
        # surviving pixel fired on - a caption band fires repeatedly in the same place,
        # a passing specular highlight fires once.
        score = (cap_count * (~blank_hit)).astype(np.float32)
        cand = _band_from_profile(score, polarity, len(cap_grays), len(blank_grays), out_dir)
        if cand.get("box"):
            cand.update(_band_behaviour(cand["box"], polarity, cap_grays, blank_grays))
        candidates.append(cand)

    # SELECTION.  Size is the wrong criterion - on an approved control the largest
    # candidate can be a dark vignette in a corner, which would make the probe FAIL an
    # approved reel.  A caption band is instead defined
    # by its BEHAVIOUR: the reference shows ink there on essentially every frame where a
    # caption is up, and shows none there when no caption is up.  Select on that, and refuse
    # to answer at all when no candidate behaves like a caption band.
    scored = [c for c in candidates if c.get("box") and c.get("presence_rate") is not None]
    best: Dict = {}
    if scored:
        best = max(scored, key=lambda c: (c["presence_rate"] - c["blank_rate"]))
    out["candidates"] = [{k: v for k, v in c.items() if k != "mask_png"} for c in candidates]
    out["candidates_considered"] = ["light", "dark"]
    out["selection_rule"] = ("highest (presence_rate - blank_rate); NOT band size - "
                             "size selected a corner vignette on the approved control")
    out.update({k: v for k, v in (best or {}).items() if k != "box"})
    box = (best or {}).get("box")
    out["band_xyxy"] = box
    out["sample_size"] = int((best or {}).get("n_caption_frames", 0))
    if not box or (best or {}).get("band_px", 0) < MIN_BAND_PX:
        out.update(verdict="UNMEASURABLE",
                   reason=(f"no caption band survived the blank-frame subtraction "
                           f"(kept {(best or {}).get('band_px', 0)} px, floor {MIN_BAND_PX}); "
                           f"the reference's caption ink is not separable from its footage "
                           f"by extreme-luma alone at these frames"))
        return out
    frac = ((box[2] - box[0]) * (box[3] - box[1]))
    g0 = _load(probes[0], "cap")
    total = float(g0.size) if g0 is not None else None
    out["band_area_fraction_of_raster"] = (round(frac / total, 4) if total else None)
    if total and frac / total > MAX_BAND_AREA_FRACTION:
        out.update(verdict="UNMEASURABLE",
                   reason=(f"the derived band covers {frac/total:.2%} of the raster "
                           f"(ceiling {MAX_BAND_AREA_FRACTION:.0%}); that is a failed "
                           f"derivation, not a caption band"))
        return out
    pr = best.get("presence_rate")
    br = best.get("blank_rate")
    out["acceptance"] = {"presence_rate": pr, "blank_rate": br,
                         "min_presence_rate": MIN_PRESENCE_RATE,
                         "max_blank_rate": MAX_BLANK_RATE}
    if pr is None or br is None:
        out.update(verdict="UNMEASURABLE",
                   reason="the candidate band's behaviour could not be measured")
        return out
    if pr < MIN_PRESENCE_RATE or br > MAX_BLANK_RATE:
        out.update(verdict="UNMEASURABLE",
                   reason=(f"the best candidate box does not behave like a caption band: it "
                           f"carries reference ink on {pr:.0%} of caption frames (floor "
                           f"{MIN_PRESENCE_RATE:.0%}) and on {br:.0%} of caption-free frames "
                           f"(ceiling {MAX_BLANK_RATE:.0%}). Refusing to measure presence "
                           f"against a box that is not the caption band."))
        return out
    out["verdict"] = "RESOLVED"
    return out


def band_presence(reference: str, delivered: str, states: Sequence[Dict],
                  blank_frames: Sequence[int], out_dir: str,
                  state_samples: int = 20, per_state_limit: int = 2) -> Dict:
    """W5b: does the DELIVERED file carry caption ink inside the reference's own band?"""
    os.makedirs(out_dir, exist_ok=True)
    band = derive_reference_band(reference, states, blank_frames,
                                 os.path.join(out_dir, "band"), state_samples)
    res: Dict = {
        "gate": "band_presence_W5b",
        "band_derivation": band,
        "thresholds": {"min_reference_ink_fraction": MIN_REFERENCE_FRACTION,
                       "min_delivered_over_reference_ratio": MIN_RATIO},
        "evidence": {"out_dir": out_dir},
    }
    if band["verdict"] != "RESOLVED":
        res.update(verdict="UNMEASURABLE", sample_size=0,
                   reason="the reference's caption band could not be derived: "
                          + str(band.get("reason")))
        return res

    x0, y0, x1, y1 = band["band_xyxy"]
    pol = band["polarity"]
    rows: List[Dict] = []
    for s in states:
        a = int(s.get("start_frame", 0))
        b = int(s.get("end_frame_exclusive", a + 1))
        if b <= a:
            rows.append({"state_id": s.get("id"), "verdict": "UNMEASURABLE",
                         "sample_size": 0, "reason": "empty frame span"})
            continue
        picks = sorted({(a + b) // 2, a})[:per_state_limit]
        meas = []
        for f in picks:
            rp = os.path.join(out_dir, f"{s.get('id')}-f{f}-ref.png")
            dp = os.path.join(out_dir, f"{s.get('id')}-f{f}-del.png")
            try:
                readback.extract_frame(reference, f, rp)
                readback.extract_frame(delivered, f, dp)
            except Exception:
                continue
            ri, di = cv2.imread(rp), cv2.imread(dp)
            if ri is None or di is None:
                continue
            rg, dg = _luma(ri)[y0:y1, x0:x1], _luma(di)[y0:y1, x0:x1]
            if rg.size == 0 or dg.size == 0:
                continue
            meas.append({
                "frame": f,
                "reference_ink_fraction": round(float(_extreme(rg, pol).mean()), 6),
                "delivered_ink_fraction": round(float(_extreme(dg, pol).mean()), 6),
                "band_px": int(rg.size),
                "evidence": {"reference_png": rp, "delivered_png": dp},
            })
        if not meas:
            rows.append({"state_id": s.get("id"), "verdict": "UNMEASURABLE",
                         "sample_size": 0,
                         "reason": "no probe frame decoded on both sides"})
            continue
        rmax = max(m["reference_ink_fraction"] for m in meas)
        dmax = max(m["delivered_ink_fraction"] for m in meas)
        row = {"state_id": s.get("id"), "text": s.get("text"),
               "sample_size": len(meas), "polarity": pol,
               "reference_ink_fraction": rmax, "delivered_ink_fraction": dmax,
               "ratio_delivered_over_reference": (round(dmax / rmax, 4) if rmax else None),
               "frames": meas}
        if rmax < MIN_REFERENCE_FRACTION:
            row.update(verdict="UNMEASURABLE",
                       reason=(f"the reference itself shows only {rmax:.5f} ink fraction in "
                               f"its own band here (floor {MIN_REFERENCE_FRACTION}); nothing "
                               f"to be missing from"))
        else:
            row["verdict"] = "PASS" if dmax >= MIN_RATIO * rmax else "FAIL"
        rows.append(row)

    measured = [r for r in rows if r["verdict"] in ("PASS", "FAIL")]
    fails = [r for r in rows if r["verdict"] == "FAIL"]
    res.update({
        "band_xyxy": [x0, y0, x1, y1],
        "polarity": pol,
        "states_total": len(rows),
        "sample_size": len(measured),
        "states_missing": len(fails),
        "missing_ids": [r["state_id"] for r in fails],
        "unmeasurable_ids": [r["state_id"] for r in rows if r["verdict"] == "UNMEASURABLE"],
        "states": rows,
        "verdict": ("UNMEASURABLE" if not measured else ("FAIL" if fails else "PASS")),
    })
    if not measured:
        res["reason"] = "no state could be measured on both sides inside the derived band"
    return res


def _states_from_contract(doc: Dict) -> List[Dict]:
    out = []
    for s in doc.get("states") or []:
        out.append({"id": s.get("id"), "text": s.get("text"),
                    "start_frame": s.get("start_frame"),
                    "end_frame_exclusive": s.get("end_frame_exclusive")})
    return out


def blanks_from_gaps(states: Sequence[Dict], frame_count: Optional[int] = None,
                     limit: int = 12) -> List[int]:
    """Frames no state covers.  Used only when the contract declares no blank_frames.

    This is weaker evidence than a declared blank list - it assumes the contract's spans
    are complete - so callers must record which of the two they used.
    """
    covered = set()
    hi = 0
    for s in states:
        a = int(s.get("start_frame") or 0)
        b = int(s.get("end_frame_exclusive") or a + 1)
        covered.update(range(a, b))
        hi = max(hi, b)
    n = int(frame_count) if frame_count else hi
    gaps = [f for f in range(0, n) if f not in covered]
    if len(gaps) <= limit:
        return gaps
    step = len(gaps) / float(limit)
    return [gaps[int(i * step)] for i in range(limit)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True)
    ap.add_argument("--delivered", required=True)
    ap.add_argument("--contract", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--json", default=None)
    ap.add_argument("--state-samples", type=int, default=20)
    a = ap.parse_args(argv)

    doc = json.load(open(a.contract))
    states = _states_from_contract(doc)
    blanks = doc.get("blank_frames") or []
    blank_source = "contract.blank_frames"
    if not blanks:
        blanks = blanks_from_gaps(states, doc.get("frame_count"))
        blank_source = ("DERIVED: frames no state span covers (the contract declares no "
                        "blank_frames; weaker evidence, assumes the spans are complete)")
    r = band_presence(a.reference, a.delivered, states, blanks, a.out_dir,
                      state_samples=a.state_samples)
    r["blank_frame_source"] = blank_source
    dest = a.json or os.path.join(a.out_dir, "band-presence.json")
    json.dump(r, open(dest, "w"), indent=1, default=str)
    print("verdict          %s" % r["verdict"])
    print("band             %s  polarity=%s" % (r.get("band_xyxy"), r.get("polarity")))
    print("states measured  %s of %s   missing=%s" % (
        r.get("sample_size"), r.get("states_total"), r.get("states_missing")))
    if r.get("reason"):
        print("reason           %s" % r["reason"])
    if r.get("missing_ids"):
        print("missing ids      %s" % r["missing_ids"][:25])
    print("json             %s" % dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
