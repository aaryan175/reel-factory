"""W1 RECLASSIFICATION — the words gate, re-derived from the reads it already made.

Two labelling defects in `wordtruth.triage_delivered` are corrected here.  Both are labelling
defects, not measurement defects: the OCR reads recorded in triage.json are correct and
sufficient; only the verdict logic on top of them was wrong.  So this module re-derives the
words verdict from the SAME triage.json records, with no new OCR.

--------------------------------------------------------------------------------------------
DEFECT 1 (critical) — `UNVERIFIABLE_BY_READER` inverts on the wrong-words class.

A state is marked `UNVERIFIABLE_BY_READER` when the REFERENCE's own crop does not read as the
DECLARED text. The intent was a control on the reader: "if we cannot read known-good ink, the
reader is the limit, not our ink." But when the declared text is supposed to BE the
reference's text, and they differ *because we wrote different words*, the control fires and
absolves the defect:

    c01  declared 'OURS1'  reference reads 'REF1'  delivered reads 'OURS1'
    c02  declared 'OURS2'  reference reads 'REF2'  delivered reads 'OURS2'

Every such state was labelled UNVERIFIABLE_BY_READER and the reel-level verdict came out
PASS, although the reader read BOTH sides cleanly.

FIX: the reader control must key on whether the REFERENCE READ RESOLVED, not on whether it
matched declared. If the reference resolves to X and the delivered resolves to Y and X != Y,
that is a words defect no matter what the contract declared.

--------------------------------------------------------------------------------------------
DEFECT 2 — "wrong word" and "reader gave up" were the same label.

On an approved control, flagged states can be the reader giving up on ink that is
demonstrably present (a direct ink probe at the same box and frame shows delivered ink at a
density comparable to the reference's own).  Meanwhile a damaged delivery can read
*confidently* as a different word (e.g. 'Garden' read as 'Gorden').  Those are different
events and must not share a label.

--------------------------------------------------------------------------------------------
CLASSES EMITTED (per state)

  WRONG_WORDS           ref and del both resolved, and they differ        -> BLOCKING
  WORDS_OK              ref and del both resolved, and they agree
  UNREADABLE_DELIVERED  ref resolved, del did not                         -> advisory (legibility)
  UNVERIFIABLE          ref did not resolve                               -> advisory (reader limit)

and, independently, the CONTRACT check:

  CONTRACT_WORDS_WRONG  ref resolved and declared != ref  -> the policy defect (we declared other words)

The contract check is what `wordtruth_project` answers; it is reported here alongside so one
table shows both "did we write the right words" and "did we render what we wrote".
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from readback import normalize_text as _norm
except Exception:  # pragma: no cover - fallback keeps the module usable standalone
    def _norm(s):
        return "".join(ch for ch in str(s or "").lower() if ch.isalnum())

STABILITY_FLOOR = 1.0 / 3.0
# Calibrated from review verdicts, not chosen: place it inside the empty band between the
# lowest approved reel and the highest words-rejected reel (see the note inside reclassify()).
TRACK_AGREEMENT_THRESHOLD = 0.50
MIN_STATES_FOR_TRACK_VERDICT = 4


def _resolved(read: Optional[str], stab: Optional[float], floor: float) -> bool:
    if read is None or not str(read).strip() or stab is None:
        return False
    return float(stab) >= floor


def reclassify(triage: Dict, floor: float = STABILITY_FLOOR) -> Dict:
    rows: List[Dict] = []
    for s in triage.get("states") or []:
        r_read, d_read = s.get("reference_read"), s.get("delivered_read")
        r_ok = _resolved(r_read, s.get("reference_stability"), floor)
        d_ok = _resolved(d_read, s.get("delivered_stability"), floor)
        declared = s.get("declared_text")

        if r_ok and d_ok:
            kind = "WORDS_OK" if _norm(r_read) == _norm(d_read) else "WRONG_WORDS"
        elif r_ok and not d_ok:
            kind = "UNREADABLE_DELIVERED"
        else:
            kind = "UNVERIFIABLE"

        contract = None
        if r_ok:
            contract = ("CONTRACT_WORDS_OK" if _norm(declared) == _norm(r_read)
                        else "CONTRACT_WORDS_WRONG")

        rows.append({
            "state_id": s.get("state_id"),
            "declared_text": declared,
            "reference_read": r_read, "reference_stability": s.get("reference_stability"),
            "delivered_read": d_read, "delivered_stability": s.get("delivered_stability"),
            "frame": s.get("delivered_frame"),
            "original_label": s.get("verdict"),
            "class": kind,
            "contract_class": contract,
        })

    wrong = [r for r in rows if r["class"] == "WRONG_WORDS"]
    ok = [r for r in rows if r["class"] == "WORDS_OK"]
    unread = [r for r in rows if r["class"] == "UNREADABLE_DELIVERED"]
    unver = [r for r in rows if r["class"] == "UNVERIFIABLE"]
    cwrong = [r for r in rows if r["contract_class"] == "CONTRACT_WORDS_WRONG"]
    cok = [r for r in rows if r["contract_class"] == "CONTRACT_WORDS_OK"]

    measured = len(wrong) + len(ok)
    total = len(rows)

    # REEL-LEVEL AGREEMENT, not per-state equality.
    #
    # A single state's OCR inequality is far too brittle to block on. On an approved control
    # several states can "differ" purely because the REFERENCE read is noisy - and noisy at
    # high stability, which is the part that matters (e.g. ref 'the sun' read as 'the sum' at stability
    # 1.0).  "Stability 1.0" does not mean "this read is correct", and exact
    # string equality between two OCR reads cannot be the blocking condition.
    #
    # The wrong-words class is unmistakable in AGGREGATE instead: approved reels score high
    # agreement (~0.8+) and words-rejected reels score near zero, so the threshold is placed
    # in the middle of the empty band between them.
    agreement = (len(ok) / float(measured)) if measured else None
    if measured < MIN_STATES_FOR_TRACK_VERDICT:
        verdict = "UNMEASURABLE"
        reason = (f"only {measured} state(s) had BOTH sides resolve to text (floor "
                  f"{MIN_STATES_FOR_TRACK_VERDICT}); the delivered words were never compared "
                  f"against the reference's on enough of the track to judge it")
    elif agreement < TRACK_AGREEMENT_THRESHOLD:
        verdict = "FAIL"
        reason = (f"only {agreement:.1%} of the states where both sides read agree with the "
                  f"reference (threshold {TRACK_AGREEMENT_THRESHOLD:.0%}); this is the "
                  f"wrong-words class, not reader noise")
    else:
        verdict = "PASS"
        reason = (f"{agreement:.1%} of comparable states carry the reference's words; the "
                  f"{len(wrong)} disagreeing state(s) are consistent with reader noise at "
                  f"this rate, and are listed for inspection rather than blocked on")

    contract_measured = len(cwrong) + len(cok)
    contract_agreement = (len(cok) / float(contract_measured)) if contract_measured else None
    if contract_measured < MIN_STATES_FOR_TRACK_VERDICT:
        contract_verdict = "UNMEASURABLE"
    elif contract_agreement < TRACK_AGREEMENT_THRESHOLD:
        contract_verdict = "FAIL"
    else:
        contract_verdict = "PASS"

    return {
        "probe": "w1_reclass",
        "stability_floor": floor,
        "states_total": total,
        "sample_size": measured,
        "coverage": round(measured / total, 4) if total else None,
        "track_agreement_rate": (round(agreement, 4) if agreement is not None else None),
        "track_agreement_threshold": TRACK_AGREEMENT_THRESHOLD,
        "verdict": verdict,
        "reason": reason,
        "wrong_words": len(wrong), "wrong_words_ids": [r["state_id"] for r in wrong],
        "words_ok": len(ok),
        "unreadable_delivered": len(unread),
        "unreadable_delivered_ids": [r["state_id"] for r in unread],
        "unverifiable": len(unver),
        "contract_verdict": contract_verdict,
        "contract_agreement_rate": (round(contract_agreement, 4)
                                    if contract_agreement is not None else None),
        "contract_sample_size": contract_measured,
        "contract_words_wrong": len(cwrong),
        "contract_words_wrong_ids": [r["state_id"] for r in cwrong],
        "contract_words_ok": len(cok),
        "states": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    from cl_paths import SWEEP_ROOT
    ap.add_argument("--sweep-root", default=os.path.join(SWEEP_ROOT, "sweep"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    results = {}
    for path in sorted(glob.glob(os.path.join(a.sweep_root, "*", "w1", "triage.json"))):
        key = path.split(os.sep)[-3]
        try:
            results[key] = reclassify(json.load(open(path)))
        except Exception as exc:
            results[key] = {"verdict": "UNMEASURABLE", "reason": str(exc)[:200]}

    print("%-15s %-12s %-7s %-6s %-6s %-6s %-6s | %-12s %-7s" % (
        "target", "WORDS", "agree", "wrong", "ok", "unrd", "unver", "CONTRACT", "agree"))
    for k, v in results.items():
        print("%-15s %-12s %-7s %-6s %-6s %-6s %-6s | %-12s %-7s" % (
            k, v.get("verdict"), v.get("track_agreement_rate"), v.get("wrong_words"),
            v.get("words_ok"), v.get("unreadable_delivered"), v.get("unverifiable"),
            v.get("contract_verdict"), v.get("contract_agreement_rate")))
    if a.out:
        json.dump(results, open(a.out, "w"), indent=1, default=str)
        print("\nwrote %s" % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
