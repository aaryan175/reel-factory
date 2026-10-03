"""POST-FIX VERIFICATION PASS

Re-runs the calibrated caption gate battery over NEW delivered bytes produced by fix
lanes, independently of those lanes' own audits, and reports the DELTA against a baseline
sweep record (sweep.py's output).

Gates are sweep.py's, unmodified and imported, so the numbers are produced by exactly the
same code that produced the baseline:

    W1  words     wordtruth.triage_delivered  (+ w1_reclass.reclassify -> W1_corrected,
                  the verdict to act on: reel-level agreement rate, threshold 0.50)
    W2  anatomy   DOCTRINE 16.2 triple gate over recovered ink masks
    W3  ink       per-frame separation AND reference-relative Michelson (>= 0.80x), both
                  conditions enforced inside inkcheck.py
    W5  presence  does the delivered file carry ink where the contract says a caption is

DELTA VOCABULARY per (target, gate):
    FIXED          baseline FAIL          -> now PASS
    STILL-BROKEN   baseline FAIL          -> now FAIL
    REGRESSED      baseline PASS          -> now FAIL
    HELD           baseline PASS          -> now PASS
    NOW-MEASURABLE baseline UNMEASURABLE  -> now PASS/FAIL
    UNMEASURABLE   now UNMEASURABLE (reason carried verbatim)

Targets: a JSON list (--targets, default $CAPTION_SWEEP_ROOT/postfix-targets.json) in the
sweep.py target schema plus `baseline_key` (the sweep target this one supersedes) and
optional `delivery_state` / `words_rule`.  `contract` may additionally be
{"adapt": "word_ink_qc", "source": <per-word QC contract>} (see adapt_word_ink_qc).

Big outputs -> $CAPTION_SWEEP_ROOT/postfix/<shard>/
Machine record -> $CAPTION_SWEEP_ROOT/postfix.json  (merged from the shards)

This module sets no approval state anywhere: technical + measurement only.

Usage:
    python3 caption-learning/postfix.py --list
    python3 caption-learning/postfix.py --only refB-v2 --root shardA --out /path/shard-A.json
    python3 caption-learning/postfix.py --merge shard-*.json --out postfix.json
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import sweep          # noqa: E402
import w1_reclass     # noqa: E402
from cl_paths import SWEEP_ROOT as _SWEEP_BASE  # noqa: E402
from readback import normalize_text as _norm   # noqa: E402

POSTFIX_ROOT = os.path.join(_SWEEP_BASE, "postfix")
BASELINE = os.path.join(_SWEEP_BASE, "sweep.json")
OUT_JSON = os.path.join(_SWEEP_BASE, "postfix.json")
DEFAULT_TARGETS = os.path.join(_SWEEP_BASE, "postfix-targets.json")


# ==========================================================================================
# contract adapters — written to disk with provenance, never authored silently
# ==========================================================================================
def adapt_word_ink_qc(dest: str, src: str) -> dict:
    """A per-word QC contract whose geometry keys are `word_ink_bbox_xyxy` /
    `line_ink_bbox_xyxy`, not reelctl's `placement.core_bbox_xyxy`.  The gates address
    states through `wordtruth.normalize_states`, which only reads the reelctl shape, so the
    SAME numbers are restated in that shape here.  No geometry is invented: every box is the
    source document's own `word_ink_bbox_xyxy`."""
    src = os.path.expanduser(src)
    doc = json.load(open(src))
    states = []
    for s in doc["states"]:
        box = [int(v) for v in s["word_ink_bbox_xyxy"]]
        states.append({
            "id": s["id"].replace("-", "_"),
            "text": s["text"],
            "start_frame": int(s["delivered_onset_frame"]),
            "end_frame_exclusive": int(s["delivered_end_frame_exclusive"]),
            "frames": int(s["delivered_end_frame_exclusive"]) - int(s["delivered_onset_frame"]),
            "style_id": s.get("tier", "T_display"),
            "placement": {"core_bbox_xyxy": box, "treatment_bbox_xyxy": box,
                          "anchor": "word_ink_bbox"},
            "ink": {"rgb_median": [int(v) for v in s["ink_rgb"]]},
        })
    out = {
        "schema_version": "ADAPTED-postfix",
        "artifact_type": "caption_contract",
        "provenance": {
            "adapted_by": "caption-learning/postfix.py::adapt_word_ink_qc",
            "source": src,
            "transform": ("key rename only: word_ink_bbox_xyxy -> placement.core_bbox_xyxy, "
                          "delivered_onset_frame -> start_frame, "
                          "delivered_end_frame_exclusive -> end_frame_exclusive, "
                          "ink_rgb -> ink.rgb_median.  No value was changed, added or "
                          "estimated."),
        },
        "states": states,
    }
    json.dump(out, open(dest, "w"), indent=1)
    return out


TARGETS: list = []
BY_KEY: dict = {}


def load_targets(path: str) -> list:
    targets = sweep.load_targets(path)
    TARGETS[:] = targets
    BY_KEY.clear()
    BY_KEY.update({t["key"]: t for t in targets})
    return targets


# ==========================================================================================
# driver
# ==========================================================================================
def _verdict(block):
    return (block or {}).get("verdict")


def delta(before, after):
    if after in (None, "UNMEASURABLE"):
        return "UNMEASURABLE"
    if before in (None, "UNMEASURABLE"):
        return "NOW-MEASURABLE"
    if before == "FAIL":
        return "FIXED" if after == "PASS" else "STILL-BROKEN"
    return "HELD" if after == "PASS" else "REGRESSED"


def load_baseline():
    d = json.load(open(BASELINE))
    return {t["key"]: t for t in d["targets"]}


def override_words_check(rec, declared):
    """For a target whose declared words deliberately differ from the reference's
    (words_rule DECLARED_OVERRIDE), the words question is 'does the DELIVERED file read as
    the declared text', not 'does it read as the reference'.  Answered from the SAME triage
    reads, no new OCR."""
    tri_path = os.path.join(rec["evidence_dir"], "w1", "triage.json")
    if not os.path.exists(tri_path):
        return {"verdict": "UNMEASURABLE", "sample_size": 0,
                "reason": "no triage.json: W1 never ran on this target"}
    tri = json.load(open(tri_path))
    rows, agree = [], 0
    for s in tri.get("states") or []:
        d_read, d_stab = s.get("delivered_read"), s.get("delivered_stability")
        resolved = w1_reclass._resolved(d_read, d_stab, w1_reclass.STABILITY_FLOOR)
        ok = bool(resolved and _norm(d_read) == _norm(declared))
        agree += 1 if ok else 0
        rows.append({"state_id": s.get("state_id"), "declared_override": declared,
                     "delivered_read": d_read, "delivered_stability": d_stab,
                     "resolved": resolved, "matches_override": ok})
    meas = [r for r in rows if r["resolved"]]
    if not meas:
        return {"probe": "override_words", "verdict": "UNMEASURABLE", "sample_size": 0,
                "reason": "the delivered crop never resolved to text at or above the "
                          "stability floor, so it could not be compared to the override",
                "states": rows}
    return {"probe": "override_words", "verdict": ("PASS" if agree == len(meas) else "FAIL"),
            "sample_size": len(meas), "states_total": len(rows),
            "agreeing": agree,
            "reason": ("the delivered caption reads as the declared override text on "
                       "%d/%d resolved state(s)" % (agree, len(meas))),
            "states": rows}


def run(keys, root_name, out_path):
    root = os.path.join(POSTFIX_ROOT, root_name, "sweep")
    os.makedirs(root, exist_ok=True)
    sweep.SWEEP_ROOT = root                       # isolate this shard's frame cache
    base = load_baseline()

    results = []
    for key in keys:
        t = dict(BY_KEY[key])
        out_dir = os.path.join(root, key)
        os.makedirs(out_dir, exist_ok=True)
        c = t["contract"]
        if isinstance(c, dict) and c.get("adapt") == "word_ink_qc":
            cpath = os.path.join(out_dir, "contract-ADAPTED-word-ink-qc.json")
            adapt_word_ink_qc(cpath, c["source"])
            t["contract"] = cpath

        print("=" * 92)
        print("TARGET %s  [%s]" % (key, t.get("delivery_state")))
        print("  delivered: %s" % t["delivered"])
        print("  reference: %s" % t["reference"])
        print("  contract : %s" % t["contract"])
        t0 = time.time()
        rec = sweep.run_target(t, True, True, True, keep_cache=False)
        rec["elapsed_s"] = round(time.time() - t0, 1)
        rec["baseline_key"] = t["baseline_key"]
        rec["delivery_state"] = t.get("delivery_state")
        rec["words_rule"] = t.get("words_rule", "REFERENCE_1TO1")

        # --- W1_corrected, from the same reads ------------------------------------------
        tri = os.path.join(out_dir, "w1", "triage.json")
        if os.path.exists(tri):
            try:
                rec["W1_corrected"] = w1_reclass.reclassify(json.load(open(tri)))
            except Exception as exc:
                rec["W1_corrected"] = {"verdict": "UNMEASURABLE",
                                       "sample_size": 0, "reason": str(exc)[:300]}
        if rec.get("words_rule") == "DECLARED_OVERRIDE":
            declared = json.load(open(rec["contract_path"]))["states"][0]["text"]
            rec["override_words"] = override_words_check(rec, declared)

        # --- delta vs the baseline -------------------------------------------------------
        b = base.get(t["baseline_key"], {})
        bw3 = _verdict(b.get("W3_corrected")) or _verdict(b.get("W3"))
        rec["baseline"] = {
            "key": t["baseline_key"],
            "W1": _verdict(b.get("W1")),
            "W1_corrected": _verdict(b.get("W1_corrected")),
            "W2": _verdict(b.get("W2")),
            "W3_corrected": bw3,
            "W5": _verdict(b.get("W5")),
            "delivered": b.get("delivered"),
        }
        rec["delta"] = {
            "W1_corrected": delta(_verdict(b.get("W1_corrected")),
                                  _verdict(rec.get("W1_corrected"))),
            "W2": delta(_verdict(b.get("W2")), _verdict(rec.get("W2"))),
            "W3": delta(bw3, _verdict(rec.get("W3"))),
            "W5": delta(_verdict(b.get("W5")), _verdict(rec.get("W5"))),
        }
        results.append(rec)

        for g in ("W1", "W1_corrected", "W2", "W3", "W5"):
            v = rec.get(g)
            if v:
                print("  %-13s %-13s n=%-4s %s" % (
                    g, v.get("verdict"), v.get("sample_size"), (v.get("reason") or "")[:110]))
        print("  DELTA %s" % rec["delta"])
        print("  elapsed %.1fs  evidence %s" % (rec["elapsed_s"], out_dir))

        json.dump({"schema": "caption-postfix",
                   "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "targets": results}, open(out_path, "w"), indent=1, default=str)
    print("\nwrote %s" % out_path)
    return 0


def merge(patterns, out_path):
    seen, targets = set(), []
    for pat in patterns:
        for p in sorted(_glob.glob(pat)):
            for t in json.load(open(p))["targets"]:
                if t["key"] in seen:
                    continue
                seen.add(t["key"])
                targets.append(t)
    order = [t["key"] for t in TARGETS]
    targets.sort(key=lambda t: order.index(t["key"]) if t["key"] in order else 999)
    json.dump({
        "schema": "caption-postfix",
        "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "purpose": ("independent post-fix verification of the NEW delivered caption bytes; "
                    "delta against the baseline sweep record"),
        "baseline": BASELINE,
        "scope": ("technical + measurement pass only. This document sets NO creative "
                  "approval and NO publish approval anywhere."),
        "delta_vocabulary": {
            "FIXED": "baseline FAIL -> now PASS",
            "STILL-BROKEN": "baseline FAIL -> now FAIL",
            "REGRESSED": "baseline PASS -> now FAIL",
            "HELD": "baseline PASS -> now PASS",
            "NOW-MEASURABLE": "baseline UNMEASURABLE -> now PASS/FAIL",
            "UNMEASURABLE": "now UNMEASURABLE; the reason is carried on the gate block",
        },
        "w1_note": ("W1 is the raw triage verdict; W1_corrected (w1_reclass.py, reel-level "
                    "agreement, threshold 0.50) is the one to act on."),
        "w3_note": ("W3 here is a FRESH measurement under the corrected two-condition rule "
                    "(separation >= 25 luma AND Michelson >= 0.80x the reference's own), so it "
                    "is compared against the baseline's W3_corrected, never its raw W3."),
        "targets": targets,
    }, open(out_path, "w"), indent=1, default=str)
    print("merged %d targets -> %s" % (len(targets), out_path))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=DEFAULT_TARGETS)
    ap.add_argument("--baseline", default=None, help="baseline sweep JSON")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default=None)
    ap.add_argument("--root", default="shard")
    ap.add_argument("--out", default=None)
    ap.add_argument("--merge", nargs="*", default=None)
    a = ap.parse_args(argv)
    global BASELINE
    if a.baseline:
        BASELINE = a.baseline
    load_targets(a.targets)

    if a.list:
        for t in TARGETS:
            print("%-16s %-12s %s" % (t["key"], t.get("delivery_state"), t["label"]))
        return 0
    if a.merge is not None:
        return merge(a.merge, a.out or OUT_JSON)

    keys = ([k.strip() for k in a.only.split(",") if k.strip()] if a.only
            else [t["key"] for t in TARGETS])
    bad = [k for k in keys if k not in BY_KEY]
    if bad:
        print("unknown target keys: %s" % bad, file=sys.stderr)
        return 2
    return run(keys, a.root, a.out or OUT_JSON)


if __name__ == "__main__":
    raise SystemExit(main())
