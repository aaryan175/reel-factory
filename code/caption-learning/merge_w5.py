"""Merge a re-measured W5 pass into the main sweep record.

The full sweep ran with the single-polarity `presence_state`, which reported
opposite-polarity ink as "missing". Rather than re-running the whole battery, W5 alone is re-measured with the corrected
both-polarities probe and merged here, with provenance recorded per target so the file
says which pass produced which number.

    python3 merge_w5.py --sweep sweep.json --shard w5-repass.json
"""

from __future__ import annotations

import argparse
import json
import os


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--shard", required=True)
    a = ap.parse_args(argv)

    main_doc = json.load(open(a.sweep))
    shard = json.load(open(a.shard))
    by_key = {t["key"]: t for t in shard["targets"]}

    merged, missing = 0, []
    for t in main_doc["targets"]:
        s = by_key.get(t["key"])
        if not s or "W5" not in s:
            missing.append(t["key"])
            continue
        t["W5_first_pass_single_polarity"] = {
            "verdict": (t.get("W5") or {}).get("verdict"),
            "states_missing": (t.get("W5") or {}).get("states_missing"),
            "missing_ids": (t.get("W5") or {}).get("missing_ids"),
            "note": ("superseded: this pass measured one polarity only and reported "
                     "opposite-polarity ink as missing"),
        }
        t["W5"] = s["W5"]
        t["W5_provenance"] = {
            "remeasured_utc": shard.get("written_utc"),
            "probe": "presence_state, both polarities on both sides",
            "shard": os.path.abspath(a.shard),
        }
        merged += 1

    main_doc["w5_remeasured"] = {
        "reason": ("the first pass chose a single polarity from the reference and reported "
                   "opposite-polarity ink as missing (a restyled reel carried light ink in a "
                   "box the probe called empty)"),
        "targets_merged": merged,
        "targets_without_a_repass": missing,
    }
    json.dump(main_doc, open(a.sweep, "w"), indent=1, default=str)
    print("merged W5 into %d targets; no repass for: %s" % (merged, missing or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
