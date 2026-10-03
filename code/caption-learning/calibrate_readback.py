#!/usr/bin/env python3
"""Reproduce the read-back threshold calibration on a local corpus.

    python3 caption-learning/calibrate_readback.py --plates-root DIR --contract C.json \
        --reference ref.mp4 --delivered del.mp4 --word WORD --box x0 y0 x1 y1 \
        --frames 10 13 16 --rejected S1 S2 [--script-plates DIR TEXT=FILE ...]

Prints, with real numbers, the measurements the threshold rests on:
  A. plate-level agreement for every contract state (reviewer-rejected vs accepted)
  B. optional: ornate/script plates a reviewer APPROVED (the false-positive probe)
  C. delivered-vs-reference A/B on the rejected word's frames
  D. differential readback_pair on the same crops
Evidence lands under $CAPTION_SWEEP_ROOT/calibration (or --out).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import readback  # noqa: E402
import wordtruth  # noqa: E402
from cl_paths import SWEEP_ROOT  # noqa: E402


def rule(t):
    print("\n" + t)
    print("-" * len(t))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--plates-root", required=True, help="dir the contract's plate paths are relative to")
    ap.add_argument("--contract", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--delivered", required=True)
    ap.add_argument("--word", required=True, help="declared text of the rejected state(s)")
    ap.add_argument("--box", type=int, nargs=4, required=True)
    ap.add_argument("--frames", type=int, nargs="+", required=True)
    ap.add_argument("--rejected", nargs="+", required=True, help="reviewer-rejected state ids")
    ap.add_argument("--script-plates", default=None)
    ap.add_argument("--script", nargs="*", default=[], help="TEXT=FILE pairs in --script-plates")
    ap.add_argument("--out", default=os.path.join(SWEEP_ROOT, "calibration"))
    a = ap.parse_args(argv)

    OUT = a.out
    os.makedirs(OUT, exist_ok=True)
    rejected = set(a.rejected)
    summary = {"threshold": readback.AGREEMENT_THRESHOLD}

    rule("A. plate readback vs declared text (all states)")
    pt = wordtruth.platetruth_contract(a.contract, a.plates_root,
                                       os.path.join(OUT, "platetruth"))
    print("%-7s %-38s %-6s %-6s %-22s %s" %
          ("state", "declared", "agree", "n", "modal read", "verdict"))
    for r in pt["states"]:
        print("%-7s %-38r %-6s %-6s %-22r %s" %
              (r["state_id"], r["declared_text"], r["agreement"], r["sample_size"],
               r["modal_read"], r["verdict"]))
    rej = [r["agreement"] for r in pt["states"] if r["state_id"] in rejected]
    acc = [r["agreement"] for r in pt["states"]
           if r["state_id"] not in rejected and r["agreement"] is not None]
    if rej and acc:
        print("\nrejected states  : max agreement %.4f  (n=%d)" % (max(rej), len(rej)))
        print("accepted states  : min agreement %.4f  (n=%d)" % (min(acc), len(acc)))
        print("threshold        : %.2f   -> separation margin %.4f"
              % (readback.AGREEMENT_THRESHOLD, min(acc) - max(rej)))
        summary["plate_rejected_max"] = max(rej)
        summary["plate_accepted_min"] = min(acc)

    if a.script_plates and a.script:
        rule("B. script plates a reviewer APPROVED (false-positive probe)")
        print("%-22s %-10s %-6s %-6s %-16s %s" %
              ("plate", "declared", "agree", "n", "modal read", "verdict"))
        b = []
        for pair in a.script:
            t, f = pair.split("=", 1)
            p = os.path.join(a.script_plates, f)
            if not os.path.exists(p):
                print("%-22s MISSING" % f)
                continue
            d = readback.readback_image(p, t, out_dir=os.path.join(OUT, "script", f[:-4]))
            print("%-22s %-10r %-6s %-6s %-16r %s" %
                  (f, t, d["agreement"], d["sample_size"], d["modal_read"], d["verdict"]))
            b.append({"plate": f, "declared": t, "agreement": d["agreement"],
                      "verdict": d["verdict"], "modal_read": d["modal_read"]})
        summary["script_plates"] = b

    rule("C. %r — DELIVERED vs REFERENCE, zero-based frames %s" % (a.word, a.frames))
    print("%-10s %-6s %-16s %-11s %-7s %-4s %-11s %s" %
          ("source", "frame", "read_region", "verdict", "stab", "n",
           "readback", "agree"))
    c = []
    for name, vid in (("reference", a.reference), ("delivered", a.delivered)):
        for f in a.frames:
            d = readback.read_region(vid, f, a.box,
                                     os.path.join(OUT, "word", "%s-f%d" % (name, f)),
                                     keep_frame=True)
            crop = d.get("evidence", {}).get("crop_png")
            rb = readback.readback_image(
                crop, a.word, out_dir=os.path.join(OUT, "word", "%s-f%d-rb" % (name, f)))
            print("%-10s %-6d %-16r %-11s %-7s %-4d %-11s %s" %
                  (name, f, d.get("modal_read"), d["verdict"], d.get("stability"),
                   d.get("sample_size", 0), rb["verdict"], rb["agreement"]))
            c.append({"source": name, "frame": f, "read_region": d["verdict"],
                      "modal_read": d.get("modal_read"),
                      "readback_verdict": rb["verdict"],
                      "readback_agreement": rb["agreement"],
                      "all_reads": d.get("all_reads"), "crop_png": crop})
    summary["word_ab"] = c

    rule("D. differential readback_pair — delivered vs reference, same crops")
    for f in a.frames:
        cr = {r["source"]: r["crop_png"] for r in c if r["frame"] == f}
        p = readback.readback_pair(cr["delivered"], cr["reference"],
                                   declared_text=a.word,
                                   out_dir=os.path.join(OUT, "pair-f%d" % f))
        s = readback.readback_pair(cr["reference"], cr["reference"],
                                   declared_text=a.word,
                                   out_dir=os.path.join(OUT, "self-f%d" % f))
        print("f%-4d delivered-vs-reference %-6s paired=%-7s | reference-vs-itself %-6s paired=%s"
              % (f, p["verdict"], p["paired_agreement"], s["verdict"], s["paired_agreement"]))

    with open(os.path.join(OUT, "calibration.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print("\nevidence: %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
