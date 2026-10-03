#!/usr/bin/env python3
"""RED-CAPABILITY part 2 — realistic corruption models.

Part 1 showed W1 (readback) survives RANDOM-PIXEL erasure of a large share of the ink.
Random speckle is not the defect class reviewers reject; the real "muffled" defect is
MORPHOLOGICAL — strokes thinned until they break, and adjacent letters fused.  This script
corrupts a passing state that way and asks whether W1 goes red, and at what dose.

    python3 verify_red_capability2.py --word WORD --pass-delivered D.png [--out DIR]
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import anatomy  # noqa: E402
import readback  # noqa: E402
from cl_paths import SWEEP_ROOT  # noqa: E402

_ap = argparse.ArgumentParser()
_ap.add_argument("--word", required=True)
_ap.add_argument("--pass-delivered", required=True)
_ap.add_argument("--out", default=os.path.join(SWEEP_ROOT, "verify", "part2"))
_a = _ap.parse_args()
WORD = _a.word
OUT = _a.out
PASS_DEL = _a.pass_delivered
os.makedirs(OUT, exist_ok=True)
res = {}


def load(path):
    a = np.asarray(Image.open(path).convert("L")).astype(np.uint8)
    bg = int(np.median(a))
    dark = a < (bg - 25)
    light = a > (bg + 25)
    m = dark if dark.sum() >= light.sum() else light
    return a, m, bg


def erode(src, iters):
    """Morphological erosion of the ink: thins every stroke uniformly."""
    a, m, bg = load(src)
    m2 = ndimage.binary_erosion(m, structure=np.ones((3, 3)), iterations=iters)
    out = a.copy()
    out[m & ~m2] = bg          # removed stroke edge -> background
    return out, m, m2


def dilate_fuse(src, iters):
    """Dilation: fattens strokes until neighbours FUSE (the fused-letters defect)."""
    a, m, bg = load(src)
    ink_val = int(np.median(a[m]))
    m2 = ndimage.binary_dilation(m, structure=np.ones((3, 3)), iterations=iters)
    out = a.copy()
    out[m2] = ink_val
    return out, m, m2


def run(tag, arr, declared=None):
    p = os.path.join(OUT, tag + ".png")
    Image.fromarray(arr).save(p)
    g = readback.readback_image(p, declared or WORD, out_dir=os.path.join(OUT, tag))
    keep = {k: g.get(k) for k in ("verdict", "agreement", "modal_read",
                                  "modal_agreement", "sample_size")}
    res[tag] = keep
    print("  %-26s %s" % (tag, json.dumps(keep, sort_keys=True)))
    return g


print("=" * 78)
print("MORPHOLOGICAL EROSION (thinning strokes) -> W1 readback")
print("=" * 78)
_, m0, _ = load(PASS_DEL)
for it in (1, 2, 3, 4):
    arr, m, m2 = erode(PASS_DEL, it)
    print("  erode x%d: ink %d -> %d (%.1f%% of strokes removed)"
          % (it, m.sum(), m2.sum(), 100.0 * (1 - m2.sum() / max(1, m.sum()))))
    run("erode_x%d" % it, arr)

print()
print("=" * 78)
print("MORPHOLOGICAL DILATION (fusing neighbours) -> W1 readback")
print("=" * 78)
for it in (1, 2, 3, 4):
    arr, m, m2 = dilate_fuse(PASS_DEL, it)
    print("  dilate x%d: ink %d -> %d" % (it, m.sum(), m2.sum()))
    run("dilate_x%d" % it, arr)

print()
print("=" * 78)
print("SAME corruptions through W2 anatomy (the gate that owns this class)")
print("=" * 78)
for it in (1, 2, 3, 4):
    _, m, m2 = erode(PASS_DEL, it)
    g = anatomy.compare_masks(m, m2, out_dir=os.path.join(OUT, "anat_erode_%d" % it),
                              label="erode_x%d" % it)
    k = {x: g.get(x) for x in ("verdict", "dice", "residual_p95_px",
                               "components_source", "components_candidate")}
    res["anatomy_erode_x%d" % it] = k
    print("  anatomy erode_x%-3d %s" % (it, json.dumps(k, sort_keys=True)))
for it in (1, 2, 3, 4):
    _, m, m2 = dilate_fuse(PASS_DEL, it)
    g = anatomy.compare_masks(m, m2, out_dir=os.path.join(OUT, "anat_dilate_%d" % it),
                              label="dilate_x%d" % it)
    k = {x: g.get(x) for x in ("verdict", "dice", "residual_p95_px",
                               "components_source", "components_candidate")}
    res["anatomy_dilate_x%d" % it] = k
    print("  anatomy dilate_x%-2d %s" % (it, json.dumps(k, sort_keys=True)))

with open(os.path.join(OUT, "red_capability2.json"), "w") as fh:
    json.dump(res, fh, indent=1, sort_keys=True)

w1_red = any(v["verdict"] != "PASS" for k, v in res.items()
             if k.startswith(("erode_", "dilate_")))
w2_red = any(v["verdict"] == "FAIL" for k, v in res.items()
             if k.startswith("anatomy_"))
print()
print("W1 goes red on some morphological dose: %s" % w1_red)
print("W2 goes red on some morphological dose: %s" % w2_red)
print("evidence: %s/red_capability2.json" % OUT)
