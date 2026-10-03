"""Re-score every W3 verdict in a sweep record under the corrected two-condition rule.

W3 requires BOTH per-frame separation >= 25 luma AND per-frame Michelson >= 0.80x the
reference's own ring Michelson for the same state. An older ``inkcheck.ink_state``
computed and stored the reference-relative ratio but gated only on the absolute floor; this
script upgrades records produced by that version.

This re-scores FROM THE STORED NUMBERS - no video is decoded, no measurement is repeated, and
nothing already in the file is modified. Each per-state block gains ``W3_corrected`` beside its
untouched ``W3``; each target gains a ``W3_corrected`` summary carrying the flip list.

Re-scoring rule, applied to the stored per-state W3:

* stored FAIL        -> FAIL         (the absolute floor already failed it)
* stored UNMEASURABLE-> UNMEASURABLE (nothing measured; the second condition cannot rescue it)
* stored PASS and ``michelson_ratio_vs_reference`` < 0.80        -> FAIL
* stored PASS and ``michelson_ratio_vs_reference`` is None       -> UNMEASURABLE
  (the reference's own ink was never measured for that state, so the relative condition
  cannot be evaluated - half the specification is not a PASS)
* stored PASS and ratio >= 0.80                                  -> PASS

Run:  python3 w3_rescore.py [--sweep PATH] [--dry-run]
"""

from __future__ import annotations

import json
import os
import sys
import time

MIN_MICHELSON_RATIO = 0.80
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from cl_paths import SWEEP_ROOT  # noqa: E402

SWEEP = os.path.join(SWEEP_ROOT, "sweep.json")

RULE = ("W3: per-frame separation >= 25 luma AND per-frame Michelson >= 0.80x "
        "the reference's own ring Michelson for the same state")


def rescore_state(w3: dict) -> dict:
    """Corrected verdict for one stored per-state W3 block. Never mutates ``w3``."""
    old = w3.get("verdict")
    ratio = w3.get("michelson_ratio_vs_reference")
    out = {
        "rule": RULE,
        "min_michelson_ratio_vs_reference": MIN_MICHELSON_RATIO,
        "verdict_original": old,
        "worst_frame": w3.get("worst_frame"),
        "worst_separation": w3.get("worst_separation"),
        "worst_michelson": w3.get("worst_michelson"),
        "reference_michelson": w3.get("reference_michelson"),
        "michelson_ratio_vs_reference": ratio,
        "separation_ratio_vs_reference": w3.get("separation_ratio_vs_reference"),
        "sample_size": w3.get("sample_size", 0),
        "rescored_from": "stored sweep numbers (no re-measurement)",
    }
    if old != "PASS":
        out["verdict"] = old
        out["reference_relative"] = "NOT_REACHED"
        out["reason"] = (f"unchanged: the original verdict was {old} "
                         f"({(w3.get('reason') or 'no reason recorded')[:160]})")
        return out
    if ratio is None:
        out["verdict"] = "UNMEASURABLE"
        out["reference_relative"] = "UNMEASURABLE_REFERENCE"
        out["reason"] = (
            f"the absolute 25-luma floor is cleared ({w3.get('worst_separation')} on "
            f"f{w3.get('worst_frame')}), but the reference's own ring Michelson for this state "
            f"was never measured (reference_michelson={w3.get('reference_michelson')}), so the "
            "reference-relative condition cannot be evaluated. Half the W3 specification is "
            "not a PASS")
        return out
    if ratio < MIN_MICHELSON_RATIO:
        out["verdict"] = "FAIL"
        out["reference_relative"] = "ENFORCED"
        out["reason"] = (
            f"clears the 25-luma floor ({w3.get('worst_separation')} on "
            f"f{w3.get('worst_frame')}) but carries Michelson {w3.get('worst_michelson')} "
            f"against the reference's own {w3.get('reference_michelson')} - ratio {ratio}, "
            f"below the {MIN_MICHELSON_RATIO} reference-relative floor: muffled next to the "
            "reference, not clean")
        return out
    out["verdict"] = "PASS"
    out["reference_relative"] = "ENFORCED"
    out["reason"] = (f"both conditions hold: separation {w3.get('worst_separation')} >= 25 and "
                     f"Michelson ratio {ratio} >= {MIN_MICHELSON_RATIO}")
    return out


def aggregate(verdicts):
    if "PASS" not in verdicts and "FAIL" not in verdicts:
        return "UNMEASURABLE"
    return "FAIL" if "FAIL" in verdicts else "PASS"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry-run" in argv
    global SWEEP
    if "--sweep" in argv:
        SWEEP = argv[argv.index("--sweep") + 1]

    doc = json.load(open(SWEEP))
    flips = []
    for t in doc["targets"]:
        per = t.get("W2W3_states") or []
        corrected = []
        for st in per:
            w3 = st.get("W3") or {}
            c = rescore_state(w3)
            st["W3_corrected"] = c            # append-only: st["W3"] is untouched
            corrected.append((st.get("state_id"), c))
            if c["verdict"] != w3.get("verdict"):
                flips.append({
                    "target": t["key"],
                    "state": st.get("state_id"),
                    "verdict_old": w3.get("verdict"),
                    "verdict_new": c["verdict"],
                    "michelson_ratio_vs_reference": c["michelson_ratio_vs_reference"],
                    "worst_michelson": c["worst_michelson"],
                    "reference_michelson": c["reference_michelson"],
                    "worst_separation": c["worst_separation"],
                    "worst_frame": c["worst_frame"],
                    "reason": c["reason"],
                })
        vs = [c["verdict"] for _, c in corrected]
        old_block = t.get("W3") or {}
        t["W3_corrected"] = {
            "gate": "ink_cleanliness",
            "rule": RULE,
            "source": "re-scored from stored sweep numbers; no video decoded, no state "
                      "re-measured; the original W3 blocks are unmodified",
            "sample_size": vs.count("PASS") + vs.count("FAIL"),
            "states_measured": [s for s, c in corrected if c["verdict"] != "UNMEASURABLE"],
            "states_failed": [s for s, c in corrected if c["verdict"] == "FAIL"],
            "states_unmeasurable": [s for s, c in corrected if c["verdict"] == "UNMEASURABLE"],
            "verdict": aggregate(vs) if corrected else (old_block.get("verdict")
                                                        or "UNMEASURABLE"),
            "verdict_original": old_block.get("verdict"),
            "flipped_states": [f["state"] for f in flips if f["target"] == t["key"]],
        }
        if not corrected:
            # No per-state blocks: either the whole W2/W3 stage was UNMEASURABLE (its reason
            # lives in W2W3) or the target carries only a summary W3.
            why = (old_block.get("reason")
                   or (t.get("W2W3") or {}).get("reason")
                   or "no reason recorded")
            t["W3_corrected"]["verdict_original"] = (
                old_block.get("verdict") or (t.get("W2W3") or {}).get("verdict"))
            t["W3_corrected"]["reason"] = (
                "no per-state W3 blocks exist for this target, so there is nothing to "
                "re-score; the original verdict stands: " + str(why))

    doc["w3_corrected_note"] = {
        "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rule": RULE,
        "why": ("the sweep's inkcheck.ink_state computed and stored "
                "michelson_ratio_vs_reference but gated only on the absolute 25-luma floor "
                "inkcheck.py now enforces both conditions; these fields re-score the "
                "already-collected numbers under the corrected rule."),
        "append_only": ("every original W3 block - per target and per state - is byte-for-byte "
                        "unchanged; corrections live in W3_corrected keys beside them"),
        "flips": flips,
        "flip_count": len(flips),
    }

    if not dry:
        json.dump(doc, open(SWEEP, "w"), indent=1, default=str)
    print(json.dumps({"flip_count": len(flips),
                      "targets": len(doc["targets"]),
                      "dry_run": dry}, indent=1))
    for f in flips:
        print("  %-11s %-6s %s -> %-12s ratio=%s" % (
            f["target"], f["state"], f["verdict_old"], f["verdict_new"],
            f["michelson_ratio_vs_reference"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
