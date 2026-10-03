#!/usr/bin/env python3
"""RED-CAPABILITY audit (independent verifier).

Question: can the sweep's gates actually go red, or are they decorative?

Method: take a state the sweep PASSED (W1 MATCH at full stability on both sides), corrupt
COPIES of its delivered pixels several ways, and re-run the same gate functions the sweep
ran.  Inputs are crops the sweep already wrote under <target>/w1/{delivered,reference}/.

    python3 verify_red_capability.py --word WORD \
        --pass-delivered D.png --pass-reference R.png \
        --other-delivered O.png --other-reference OR.png \
        --broken-delivered BD.png --broken-reference BR.png [--out DIR]

  pass-*    : a state the sweep passed, declared as --word
  other-*   : a DIFFERENT word from the same reel, same font/treatment (word-swap probe)
  broken-*  : a state with pixel-verified broken glyphs (anatomy must FAIL)
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import anatomy  # noqa: E402
import readback  # noqa: E402
from cl_paths import SWEEP_ROOT  # noqa: E402

_ap = argparse.ArgumentParser()
_ap.add_argument("--word", required=True)
for _k in ("pass-delivered", "pass-reference", "other-delivered", "other-reference",
           "broken-delivered", "broken-reference"):
    _ap.add_argument("--" + _k, required=True)
_ap.add_argument("--out", default=os.path.join(SWEEP_ROOT, "verify"))
_a = _ap.parse_args()

WORD = _a.word
OUT = _a.out
PASS_DEL, PASS_REF = _a.pass_delivered, _a.pass_reference
OTHER_DEL, OTHER_REF = _a.other_delivered, _a.other_reference
BROKEN_DEL, BROKEN_REF = _a.broken_delivered, _a.broken_reference

os.makedirs(OUT, exist_ok=True)
results = {}


def gray(path):
    return np.asarray(Image.open(path).convert("L")).astype(np.float64)


def ink_mask(path):
    """Binary ink mask, polarity chosen the way readback does: minority class."""
    a = gray(path)
    bg = np.median(a)
    dark = a < (bg - 25)
    light = a > (bg + 25)
    return dark if dark.sum() >= light.sum() else light


def erase_strokes(src_png, dst_png, fraction=0.30, seed=1234):
    """Erase `fraction` of the ink pixels by painting them to local background."""
    img = Image.open(src_png).convert("L")
    a = np.asarray(img).astype(np.uint8).copy()
    m = ink_mask(src_png)
    bg = int(np.median(a))
    idx = np.flatnonzero(m.ravel())
    rng = np.random.RandomState(seed)
    kill = rng.choice(idx, size=int(len(idx) * fraction), replace=False)
    flat = a.ravel()
    flat[kill] = bg
    a = flat.reshape(a.shape)
    Image.fromarray(a).save(dst_png)
    return {"ink_px_before": int(m.sum()), "ink_px_erased": int(len(kill)),
            "fraction": fraction}


def summarize(tag, g):
    keep = {k: g.get(k) for k in
            ("verdict", "agreement", "modal_read", "modal_agreement", "sample_size",
             "dice", "residual_p95_px", "components_source", "components_candidate",
             "holes_source", "holes_candidate", "reason")
            if k in g}
    results[tag] = keep
    print("  %-34s %s" % (tag, json.dumps(keep, sort_keys=True)))


print("=" * 78)
print("PART A — W1 readback: baseline (the state the sweep PASSED)")
print("=" * 78)
summarize("A1_baseline_delivered",
          readback.readback_image(PASS_DEL, WORD,
                                  out_dir=os.path.join(OUT, "A1")))
summarize("A2_baseline_reference",
          readback.readback_image(PASS_REF, WORD,
                                  out_dir=os.path.join(OUT, "A2")))

print()
print("=" * 78)
print("PART B — W1 readback: CORRUPTED copies of that same passing state")
print("=" * 78)

# B1: erase 30% of stroke pixels
b1_png = os.path.join(OUT, "pass_delivered_erased30.png")
b1_meta = erase_strokes(PASS_DEL, b1_png, 0.30)
print("  erase-30%% meta: %s" % json.dumps(b1_meta))
summarize("B1_erased30_declared",
          readback.readback_image(b1_png, WORD,
                                  out_dir=os.path.join(OUT, "B1")))

# B2: erase 60% of stroke pixels
b2_png = os.path.join(OUT, "pass_delivered_erased60.png")
b2_meta = erase_strokes(PASS_DEL, b2_png, 0.60, seed=7)
print("  erase-60%% meta: %s" % json.dumps(b2_meta))
summarize("B2_erased60_declared",
          readback.readback_image(b2_png, WORD,
                                  out_dir=os.path.join(OUT, "B2")))

# B3: WORD SWAP — a different rendered word from the same reel, same font,
#     same treatment, declared as WORD. This is the wrong-words class.
summarize("B3_wordswap_declared",
          readback.readback_image(OTHER_DEL, WORD,
                                  out_dir=os.path.join(OUT, "B3")))

print()
print("=" * 78)
print("PART C — W2 anatomy triple gate: self-comparison vs corrupted")
print("=" * 78)
ref_broken = ink_mask(BROKEN_REF)
del_broken = ink_mask(BROKEN_DEL)
ref_pass = ink_mask(PASS_REF)
print("  ink px: BROKENref=%d BROKENdel=%d PASSref=%d"
      % (ref_broken.sum(), del_broken.sum(), ref_pass.sum()))

# C1: reference vs ITSELF must PASS (false-positive guard)
summarize("C1_reference_self_comparison",
          anatomy.compare_masks(ref_broken, ref_broken, out_dir=os.path.join(OUT, "C1"),
                                label="broken_ref_self"))

# C2: reference vs the pixel-verified BROKEN delivered state must FAIL
summarize("C2_reference_vs_broken_delivered",
          anatomy.compare_masks(ref_broken, del_broken, out_dir=os.path.join(OUT, "C2"),
                                label="broken_ref_vs_del"))

# C3: a clean state vs itself with 30% of strokes erased must FAIL
c3 = ref_pass.copy()
idx = np.flatnonzero(c3.ravel())
rng = np.random.RandomState(99)
kill = rng.choice(idx, size=int(len(idx) * 0.30), replace=False)
flat = c3.ravel()
flat[kill] = False
c3 = flat.reshape(c3.shape)
summarize("C3_clean_vs_self_erased30",
          anatomy.compare_masks(ref_pass, c3, out_dir=os.path.join(OUT, "C3"),
                                label="pass_erode30"))

# C4: clean state vs a DIFFERENT word (word swap at the anatomy layer)
other_ref = ink_mask(OTHER_REF)
summarize("C4_pass_vs_other_wordswap",
          anatomy.compare_masks(ref_pass, other_ref, out_dir=os.path.join(OUT, "C4"),
                                label="pass_vs_other"))

with open(os.path.join(OUT, "red_capability.json"), "w") as fh:
    json.dump(results, fh, indent=1, sort_keys=True)

print()
print("=" * 78)
print("SCORECARD")
print("=" * 78)
expect = [
    ("A1_baseline_delivered", "PASS", "baseline must be green"),
    ("A2_baseline_reference", "PASS", "reference self must be green"),
    ("B1_erased30_declared", "not-PASS", "30% strokes erased"),
    ("B2_erased60_declared", "not-PASS", "60% strokes erased"),
    ("B3_wordswap_declared", "not-PASS", "wrong word"),
    ("C1_reference_self_comparison", "PASS", "anatomy false-positive guard"),
    ("C2_reference_vs_broken_delivered", "FAIL", "pixel-verified broken glyphs"),
    ("C3_clean_vs_self_erased30", "FAIL", "30% ink erased"),
    ("C4_pass_vs_other_wordswap", "FAIL", "different word entirely"),
]
ok = True
for tag, want, why in expect:
    got = results[tag]["verdict"]
    if want == "not-PASS":
        good = got != "PASS"
    else:
        good = got == want
    ok = ok and good
    print("  [%s] %-38s want=%-9s got=%-13s (%s)"
          % ("OK " if good else "BAD", tag, want, got, why))
print()
print("RED-CAPABILITY: %s" % ("PROVEN" if ok else "NOT PROVEN"))
print("evidence: %s/red_capability.json" % OUT)
