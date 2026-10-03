"""W5c - "was ANYTHING composited over the picture?"

The cheapest decisive answer to "are the captions missing?" does not need OCR, a bbox, or
the reference at all.  A reelctl project renders a caption-free `picture-base.mov` and then
composites captions over it.  If the DELIVERED file and its own picture base are the same
pixels, nothing was composited - the captions are absent, provably, with no threshold
argument to have.

Encode noise is the only thing that separates them otherwise, and it is diffuse and small
(h264 quantisation, a few luma levels everywhere).  A composited glyph is the opposite:
hundreds-to-thousands of CONTIGUOUS pixels at a large delta.  So the measure is not the mean
difference - it is the size of the largest connected component of "really different" pixels.

Typical uncaptioned delivery: over ~9 sampled state-mid frames the largest connected
component of |delta| >= 60 is ~100 px scattered and mean |delta| is a few luma levels across
the whole raster => the delivery IS its picture base, captions never composited.

HONESTY: verdict in {PRESENT, ABSENT, UNMEASURABLE}. If the two files disagree in geometry
or frame count so badly they cannot be aligned, the answer is UNMEASURABLE - never ABSENT,
because "I could not compare them" is not evidence of absence.

CLI:
    python3 basediff.py --base picture-base.mov --delivered review.mp4 \
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

DELTA = 60          # a pixel this far from the base is not encode noise
MIN_GLYPH_PX = 400  # a composited caption's largest blob is far bigger than this


def composite_check(base: str, delivered: str, frames: Sequence[int],
                    out_dir: str) -> Dict:
    os.makedirs(out_dir, exist_ok=True)
    out: Dict = {
        "gate": "composite_presence_W5c",
        "base": base,
        "delivered": delivered,
        "frames_probed_zero_based": list(frames),
        "thresholds": {"per_pixel_delta": DELTA, "min_glyph_component_px": MIN_GLYPH_PX},
        "evidence": {"out_dir": out_dir},
    }
    rows: List[Dict] = []
    for f in frames:
        bp = os.path.join(out_dir, "base-f%d.png" % f)
        dp = os.path.join(out_dir, "del-f%d.png" % f)
        try:
            readback.extract_frame(base, f, bp)
            readback.extract_frame(delivered, f, dp)
        except Exception as exc:
            rows.append({"frame": f, "status": "unreadable", "reason": str(exc)[:200]})
            continue
        b, d = cv2.imread(bp), cv2.imread(dp)
        if b is None or d is None:
            rows.append({"frame": f, "status": "unreadable", "reason": "frame did not decode"})
            continue
        if b.shape != d.shape:
            d = cv2.resize(d, (b.shape[1], b.shape[0]), interpolation=cv2.INTER_AREA)
        diff = np.abs(b.astype(np.float32) - d.astype(np.float32)).max(axis=2)
        hot = (diff >= DELTA).astype(np.uint8)
        n_lab, _, stats, _ = cv2.connectedComponentsWithStats(hot, 8)
        largest = int(max([stats[i, cv2.CC_STAT_AREA] for i in range(1, n_lab)], default=0))
        rows.append({
            "frame": f, "status": "measured",
            "mean_abs_delta_whole_raster": round(float(diff.mean()), 4),
            "max_abs_delta": int(diff.max()),
            "px_over_delta": int(hot.sum()),
            "largest_connected_component_px": largest,
            "evidence": {"base_png": bp, "delivered_png": dp},
        })

    measured = [r for r in rows if r["status"] == "measured"]
    out["frames"] = rows
    out["sample_size"] = len(measured)
    if not measured:
        out.update(verdict="UNMEASURABLE",
                   reason="no probe frame decoded from both files")
        return out
    worst = max(r["largest_connected_component_px"] for r in measured)
    out["largest_component_px_over_all_frames"] = worst
    out["total_px_over_delta"] = sum(r["px_over_delta"] for r in measured)
    out["mean_abs_delta_median"] = round(
        float(np.median([r["mean_abs_delta_whole_raster"] for r in measured])), 4)
    out["verdict"] = "PRESENT" if worst >= MIN_GLYPH_PX else "ABSENT"
    if out["verdict"] == "ABSENT":
        out["reason"] = (
            f"across {len(measured)} sampled frames the largest contiguous region differing "
            f"from the caption-free picture base by >={DELTA} is {worst} px (a composited "
            f"caption is >={MIN_GLYPH_PX} px); whole-raster mean |delta| median "
            f"{out['mean_abs_delta_median']} is encode noise. Nothing was composited over "
            f"the picture: the captions are absent from the delivered file.")
    else:
        out["reason"] = (f"largest contiguous difference from the picture base is {worst} px "
                         f"- something IS composited over the picture")
    return out


def state_mid_frames(contract_path: str, limit: int = 12) -> List[int]:
    doc = json.load(open(contract_path))
    mids = sorted({(int(s["start_frame"]) + int(s["end_frame_exclusive"])) // 2
                   for s in (doc.get("states") or [])
                   if s.get("start_frame") is not None})
    if len(mids) <= limit:
        return mids
    step = len(mids) / float(limit)
    return [mids[int(i * step)] for i in range(limit)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--delivered", required=True)
    ap.add_argument("--contract", default=None)
    ap.add_argument("--frames", default=None, help="comma-separated zero-based indices")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    if a.frames:
        frames = [int(x) for x in a.frames.split(",") if x.strip()]
    elif a.contract:
        frames = state_mid_frames(a.contract)
    else:
        ap.error("give --contract or --frames")
    r = composite_check(a.base, a.delivered, frames, a.out_dir)
    dest = a.json or os.path.join(a.out_dir, "composite-check.json")
    json.dump(r, open(dest, "w"), indent=1, default=str)
    print("verdict   %s   (sample_size=%s)" % (r["verdict"], r["sample_size"]))
    print("largest contiguous diff component: %s px" % r.get(
        "largest_component_px_over_all_frames"))
    print("reason    %s" % r.get("reason"))
    print("json      %s" % dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
