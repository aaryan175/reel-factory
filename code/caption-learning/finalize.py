"""Assemble the final sweep record from the passes that produced it.

The sweep ran in three pieces and this makes one honest file out of them, recording which
pass produced which number rather than silently overwriting:

  1. the main pass          -> the first targets
  2. the tail pass          -> the remaining targets (when the main pass was stopped
                               partway and the tail was re-launched for the remainder)
  3. the W5 re-pass         -> W5 only, re-measured for the targets in (1) with the corrected
                               both-polarities + field-ceiling probe. Targets in (2) already
                               ran the corrected probe.

    python3 finalize.py [--sweep sweep.json --tail shard-tail.json --w5 shard-w5repass.json]
"""

from __future__ import annotations

import argparse
import json
import os

import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from cl_paths import SWEEP_ROOT as WB  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", default=os.path.join(WB, "sweep.json"))
    ap.add_argument("--tail", default=os.path.join(WB, "shard-tail.json"))
    ap.add_argument("--w5", default=os.path.join(WB, "shard-w5repass.json"))
    a = ap.parse_args(argv)

    doc = json.load(open(a.sweep))
    targets = {t["key"]: t for t in doc["targets"]}
    order = [t["key"] for t in doc["targets"]]

    added = []
    if os.path.exists(a.tail):
        tail = json.load(open(a.tail))
        for t in tail["targets"]:
            if t["key"] not in targets:
                order.append(t["key"])
                added.append(t["key"])
            t["_pass"] = "tail (corrected W5 probe)"
            targets[t["key"]] = t

    w5_merged, w5_missing = [], []
    if os.path.exists(a.w5):
        shard = json.load(open(a.w5))
        by_key = {t["key"]: t for t in shard["targets"]}
        for key, t in targets.items():
            if t.get("_pass", "").startswith("tail"):
                continue
            s = by_key.get(key)
            if not s or "W5" not in s:
                if "W5" in t:
                    w5_missing.append(key)
                continue
            t["W5_first_pass_single_polarity"] = {
                "verdict": (t.get("W5") or {}).get("verdict"),
                "states_missing": (t.get("W5") or {}).get("states_missing"),
                "missing_ids": (t.get("W5") or {}).get("missing_ids"),
                "superseded_because": (
                    "measured one polarity only and had no field ceiling, so it reported "
                    "opposite-polarity ink as missing and treated a flat bright field as ink"),
            }
            t["W5"] = s["W5"]
            t["W5_provenance"] = {
                "probe": "presence_state — both polarities both sides, reference field ceiling 0.55",
                "remeasured_utc": shard.get("written_utc"),
                "shard": os.path.abspath(a.w5),
            }
            w5_merged.append(key)

    doc["targets"] = [targets[k] for k in order]
    doc["assembly"] = {
        "targets_total": len(order),
        "added_by_tail_pass": added,
        "w5_remeasured": sorted(w5_merged),
        "w5_not_remeasured": sorted(w5_missing),
        "w5_remeasure_reason": (
            "the first pass chose a single polarity from the reference and had no ceiling on "
            "what counts as ink: restyled light ink was called empty, and a flat bright field "
            "read as 'ink' on a frame the reference is known blank on"),
    }
    json.dump(doc, open(a.sweep, "w"), indent=1, default=str)
    print("targets: %d (tail added: %s)" % (len(order), added or "none"))
    print("W5 re-measured: %d  %s" % (len(w5_merged), sorted(w5_merged)))
    print("W5 NOT re-measured: %s" % (sorted(w5_missing) or "none"))
    print("wrote %s" % a.sweep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
