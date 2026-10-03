"""CALIBRATION: the DOCTRINE 16.2 triple gate against verified ground truth.

Ground truth (reviewer-verified) for one word state:
  reference   frames inside the state's span -> the word is crisp, every letter articulated.
  delivered   same frames -> fused / eroded strokes (reads as a different word).

REQUIRED OUTCOME
  reference vs itself (two DISJOINT frame subsets)  -> PASS
  delivered vs reference                            -> FAIL on >=1 of the three axes

Run:
  python3 caption-learning/calibrate_word.py --reference ref.mp4 --delivered del.mp4 \
      --contract contract.json --word WORD --ref-frames 151 152 153 --del-frames 153 150
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anatomy  # noqa: E402
import inkcheck  # noqa: E402
from cl_paths import SWEEP_ROOT  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True)
    ap.add_argument("--delivered", required=True)
    ap.add_argument("--contract", required=True)
    ap.add_argument("--word", required=True, help="declared text of the state(s) to merge")
    ap.add_argument("--ref-frames", type=int, nargs=3, required=True,
                    help="three frames inside the span; the last two are the disjoint pair")
    ap.add_argument("--del-frames", type=int, nargs="+", required=True,
                    help="delivered frames; the first is compared against the reference")
    ap.add_argument("--out", default=os.path.join(SWEEP_ROOT, "calibration"))
    a = ap.parse_args(argv)
    REF, DELIVERED, OUT = a.reference, a.delivered, a.out

    os.makedirs(OUT, exist_ok=True)
    contract = json.load(open(a.contract))
    states = contract["states"]
    hits = [s for s in states if (s.get("text") or "").strip() == a.word]
    if not hits:
        print("contract carries no state whose text is %r" % a.word)
        return 2
    print(f"contract states carrying the word {a.word!r}: {[s['id'] for s in hits]}")
    for s in hits:
        print(f"  {s['id']}: frames [{s['start_frame']},{s['end_frame_exclusive']}) "
              f"bbox={s['placement']['core_bbox_xyxy']}")

    # A word split across several states is treated as ONE state spanning all of them,
    # which is what a viewer reads on screen.
    state = dict(hits[0])
    state["id"] = "GT_WORD"
    state["start_frame"] = min(s["start_frame"] for s in hits)
    state["end_frame_exclusive"] = max(s["end_frame_exclusive"] for s in hits)
    print(f"\nmerged state: frames [{state['start_frame']},{state['end_frame_exclusive']}) "
          f"bbox={state['placement']['core_bbox_xyxy']}\n")

    # ---- recover masks -------------------------------------------------------------------
    recoveries = {}
    ra, rb, rc = a.ref_frames
    plan = [("REF-subsetA", REF, [ra]), ("REF-subsetB", REF, [rb]), ("REF-subsetC", REF, [rc])]
    plan += [("DELIVERED-f%d" % f, DELIVERED, [f]) for f in a.del_frames]
    DEL_KEY = "DELIVERED-f%d" % a.del_frames[0]
    for name, video, frames in plan:
        rec = anatomy.recover_state_mask(
            video, state, OUT, all_states=states, frames=frames, label=name
        )
        recoveries[name] = rec
        print(
            f"[recover] {name:16s} verdict={rec['verdict']:13s} frames={frames} "
            f"blank=f{rec.get('blank_frame_used')} mismatch={rec.get('background_mismatch_luma')} "
            f"otsu={rec.get('otsu_threshold')} ink={rec.get('ink_px')} comps={rec.get('components')}"
        )
        if rec["verdict"] == "UNMEASURABLE":
            print(f"                  reason: {rec['reason']}")

    print()
    # ---- the triple gate -----------------------------------------------------------------
    comparisons = [
        # REQUIRED outcomes
        ("REFERENCE self-comparison (disjoint subsets f%d vs f%d)" % (rb, rc),
         "REF-subsetB", "REF-subsetC", "PASS"),
        ("DELIVERED vs REFERENCE (same frame)", "REF-subsetC", DEL_KEY, "FAIL"),
        ("DELIVERED vs REFERENCE (adjacent frame)", "REF-subsetB", DEL_KEY, "FAIL"),
        # INFORMATIONAL: recovery repeatability probe, no required verdict
        ("[informational] REFERENCE f%d vs f%d - recovery repeatability" % (ra, rb),
         "REF-subsetA", "REF-subsetB", None),
    ]
    rows = []
    ok = True
    for title, src_key, cand_key, expected in comparisons:
        src, cand = recoveries[src_key], recoveries[cand_key]
        if src["verdict"] == "UNMEASURABLE" or cand["verdict"] == "UNMEASURABLE":
            print(f"### {title}\n    SKIPPED - a mask was UNMEASURABLE\n")
            ok = False
            continue
        res = anatomy.compare_masks(
            src["mask"], cand["mask"], out_dir=OUT, label=f"{src_key}__vs__{cand_key}"
        )
        rows.append((title, expected, res))
        got = res["verdict"]
        if expected is None:
            print(f"### {title}")
            print(f"    (no required verdict)  ->  got {got}")
        else:
            mark = "OK " if got == expected else "!! "
            ok = ok and got == expected
            print(f"### {title}")
            print(f"    expected {expected}  ->  got {got}   {mark}")
        print(
            f"    dice={res['dice']}  residual_p95={res['residual_p95_px']}px  "
            f"residual_max={res['residual_max_px']}px"
        )
        print(
            f"    components src/cand = {res['components_source']}/{res['components_candidate']}   "
            f"holes src/cand = {res['holes_source']}/{res['holes_candidate']}"
        )
        print(
            f"    sample_size (source boundary px) = {res['sample_size']}   "
            f"isotropic_scale={res['normalisation']['isotropic_scale']}  "
            f"translate={res['normalisation']['translate_dy_dx']}"
        )
        if res.get("failed_axes"):
            print(f"    failed axes: {res['failed_axes']}")
        print()

    # ---- ink cleanliness on the delivered composite --------------------------------------
    print("=" * 78)
    print("INK CLEANLINESS (inkcheck.ink_state) on the DELIVERED composite vs the REFERENCE")
    print("=" * 78)
    ref_ink = None
    if recoveries["REF-subsetC"]["verdict"] != "UNMEASURABLE":
        ref_ink = inkcheck.ink_state(
            REF, state, recoveries["REF-subsetC"]["mask"], out_dir=OUT, label="REFERENCE",
            reference_required=False,      # this measurement IS the reference
        )
        print(f"[reference] verdict={ref_ink['verdict']} worst_frame=f{ref_ink['worst_frame']} "
              f"separation={ref_ink['worst_separation']} michelson={ref_ink['worst_michelson']} "
              f"sample_size={ref_ink['sample_size']}")
        for f in ref_ink["frames"]:
            print(f"    f{f['frame']}: [{f['status']}] reg={f['registration_dy_dx']} "
                  f"interior={f['interior_median']} ring={f['ring_median']} "
                  f"sep={f['separation']} michelson={f['michelson']} "
                  f"interior_px={f['interior_px']} ring_px={f['ring_px']}")
    if recoveries[DEL_KEY]["verdict"] != "UNMEASURABLE":
        dl_ink = inkcheck.ink_state(
            DELIVERED,
            state,
            recoveries[DEL_KEY]["mask"],
            out_dir=OUT,
            label="DELIVERED",
            reference_michelson=(ref_ink or {}).get("worst_michelson"),
            reference_separation=(ref_ink or {}).get("worst_separation"),
        )
        print(f"[delivered] verdict={dl_ink['verdict']} worst_frame=f{dl_ink['worst_frame']} "
              f"separation={dl_ink['worst_separation']} michelson={dl_ink['worst_michelson']} "
              f"sample_size={dl_ink['sample_size']} "
              f"michelson_ratio_vs_reference={dl_ink.get('michelson_ratio_vs_reference')}")
        for f in dl_ink["frames"]:
            print(f"    f{f['frame']}: [{f['status']}] reg={f['registration_dy_dx']} "
                  f"interior={f['interior_median']} ring={f['ring_median']} "
                  f"sep={f['separation']} michelson={f['michelson']} "
                  f"interior_px={f['interior_px']} ring_px={f['ring_px']}")

    with open(os.path.join(OUT, "calibration.json"), "w") as fh:
        json.dump(
            anatomy._json_safe(
                {
                    "recoveries": recoveries,
                    "comparisons": [
                        {"title": t, "expected": e, "result": r} for t, e, r in rows
                    ],
                }
            ),
            fh,
            indent=2,
        )
    print(f"\ncalibration json -> {os.path.join(OUT, 'calibration.json')}")
    print("\nCALIBRATION", "SATISFIED" if ok else "NOT SATISFIED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
