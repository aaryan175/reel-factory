"""Turn a sweep record (+ verdicts.jsonl + the side probes) into the human work order.

The mechanical half of the report - scoreboards, per-state tables, the UNMEASURABLE
register - is generated here so the numbers in the markdown are the numbers in the JSON and
cannot drift apart.  The judgement half ("what the fix lanes should do first") is written by
hand and appended from a separate file.

    python3 report.py --sweep $CAPTION_SWEEP_ROOT/sweep.json --out /path/to/report.md
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from cl_paths import SWEEP_ROOT  # noqa: E402

W5C_ROOT = os.path.join(SWEEP_ROOT, "w5c")
W5B_ROOT = os.path.join(SWEEP_ROOT, "w5b")
RECLASS = os.path.join(SWEEP_ROOT, "w1-reclass.json")


def load_reclass(path: str = RECLASS) -> Dict[str, Dict]:
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            return {}
    return {}


def load_verdicts(path: str) -> Dict[str, Dict]:
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path):
        line = line.strip()
        if line:
            d = json.loads(line)
            out[d["reel"]] = d
    return out


def side(root: str, key: str, fname: str) -> Optional[Dict]:
    p = os.path.join(root, key, fname)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            return None
    return None


def sev(t: Dict) -> tuple:
    """Sort key: worst offenders first."""
    if t.get("status") == "SKIPPED":
        return (9, 0, 0)
    fails = sum(1 for g in ("W1", "W2", "W3", "W5")
                if t.get(g, {}).get("verdict") == "FAIL")
    w1 = t.get("W1", {}).get("states_blocking_mismatch") or 0
    w5 = t.get("W5", {}).get("states_missing") or 0
    return (0, -(fails * 100 + w1 + w5), 0)


def g(t: Dict, name: str) -> Dict:
    return t.get(name) or {}


def fmt_verdict(v: Optional[str]) -> str:
    """Table cell form (carries its own emphasis)."""
    if v is None:
        return "–"
    return {"PASS": "**PASS**", "FAIL": "**FAIL**",
            "UNMEASURABLE": "*UNMEASURABLE*"}.get(v, v)


def plain(v: Optional[str]) -> str:
    """Heading form - no emphasis markers, because it is already inside bold."""
    return "–" if v is None else str(v)


def build(sweep_path: str, verdicts_path: str) -> str:
    doc = json.load(open(sweep_path))
    targets = doc["targets"]
    verdicts = load_verdicts(verdicts_path)
    reclass = load_reclass()
    L: List[str] = []
    a = L.append

    a("# CAPTION PARITY SWEEP — factory-wide work order")
    a("")
    a(f"**Written:** {doc.get('written_utc')} · **schema:** `{doc.get('schema')}`  ")
    a(f"**Machine record:** `{os.path.abspath(sweep_path)}`  ")
    a(f"**Evidence root:** `{SWEEP_ROOT}`  ")
    a("")
    a("This file is the work order for the re-caption fix lanes. It is a measurement report, "
      "not a plan: every number below came out of a gate that also had to be able to say "
      "UNMEASURABLE, and several of them did.")
    a("")
    a("## How to read a verdict")
    a("")
    a("| verdict | means |")
    a("|---|---|")
    a("| **PASS** | the gate measured something and it met the threshold |")
    a("| **FAIL** | the gate measured something and it did not |")
    a("| *UNMEASURABLE* | the gate could not measure — **not** a pass. Reason always given. |")
    a("")
    a("The gates:")
    a("")
    a("| gate | question | module |")
    a("|---|---|---|")
    a("| **W1 words** | are the delivered words the *reference's* words? | `wordtruth.triage_delivered` |")
    a("| **W2 anatomy** | are the letterforms intact? (DOCTRINE §16.2 triple: Dice **and** one-way residual p95 **and** component+hole equality) | `anatomy.compare_masks` |")
    a("| **W3 ink** | is the ink clean/separated against the footage behind it, relative to the reference's own? | `inkcheck.ink_state` |")
    a("| **W5 presence** | does the delivered file carry ink where the contract says a caption is? | `sweep.presence_state` |")
    a("| **W5c composite** | was *anything at all* composited over the picture base? | `basediff.composite_check` |")
    a("")

    # ---- scoreboard --------------------------------------------------------------------
    a("## 1 · Scoreboard — review verdict vs what the gates said")
    a("")
    a("| reel | review | W1 words (corrected) | agreement | W2 anatomy | W3 ink | W5 presence | gates agree? |")
    a("|---|---|---|---|---|---|---|---|")
    order = sorted(targets, key=sev)
    for t in order:
        key = t["key"]
        v = verdicts.get(key, {})
        opv = v.get("verdict") or t.get("review_verdict") or "—"
        if t.get("status") == "SKIPPED":
            a(f"| `{key}` | {opv} | SKIPPED | — | SKIPPED | SKIPPED | SKIPPED | — |")
            continue
        rc = reclass.get(key) or {}
        gv = {n: g(t, n).get("verdict") for n in ("W1", "W2", "W3", "W5")}
        # a target whose contract carries no bbox never enters the W2/W3 stage; the sweep
        # records why under W2W3.  Show that reason rather than an empty cell.
        if gv["W2"] is None and g(t, "W2W3"):
            gv["W2"] = gv["W3"] = g(t, "W2W3").get("verdict")
        if rc:
            gv["W1"] = rc.get("verdict")
        anyfail = "FAIL" in gv.values()
        if opv.startswith("REJECTED"):
            agree = "yes — caught" if anyfail else "**NO — ESCAPE**"
        elif opv.startswith("APPROVED"):
            agree = "yes — clean" if not anyfail else "flagged (see notes)"
        else:
            agree = "n/a (unreviewed)"
        a("| `%s` | %s | %s | %s | %s | %s | %s | %s |" % (
            key, opv, fmt_verdict(gv["W1"]),
            rc.get("track_agreement_rate", "–"), fmt_verdict(gv["W2"]),
            fmt_verdict(gv["W3"]), fmt_verdict(gv["W5"]), agree))
    a("")

    # ---- per reel ----------------------------------------------------------------------
    a("## 2 · Per reel, per state")
    a("")
    for t in order:
        key = t["key"]
        a(f"### `{key}` — {t.get('label')}")
        a("")
        v = verdicts.get(key, {})
        if v:
            a(f"> **Review ({v['verdict']}):** “{v['quote']}”")
            a("")
        if t.get("note"):
            a(f"*Lane note:* {t['note']}")
            a("")
        if t.get("status") == "SKIPPED":
            a(f"**SKIPPED** — {t.get('reason')}")
            a("")
            continue
        a(f"- delivered: `{t['delivered']}`")
        a(f"- reference: `{t['reference']}`")
        a(f"- contract: `{t.get('contract_path')}`"
          + ("  ⚠️ **SYNTHESIZED by the sweep** (provenance inside the file)"
             if "SYNTHESIZED" in str(t.get("contract_path")) else ""))
        geo = t.get("geometry") or {}
        a(f"- geometry: delivered {geo.get('delivered')} vs reference {geo.get('reference')}"
          f" — raster match **{geo.get('raster_match')}**, frame-count match "
          f"**{geo.get('frame_count_match')}**")
        idx = t.get("zero_based_index_check")
        if idx:
            a(f"- zero-based frame indexing re-proved this run: **{idx.get('verdict')}** "
              f"(frame {idx.get('frame_idx')}: |Δ| to same index "
              f"{idx.get('mean_abs_diff_same_index')} vs next index "
              f"{idx.get('mean_abs_diff_next_index')})")
        a(f"- states in contract: {t.get('states_total')} "
          f"({t.get('states_with_bbox')} carry placement geometry)")
        a("")

        # W1
        w1 = g(t, "W1")
        if w1:
            a(f"**W1 words — {plain(w1.get('verdict'))}** "
              f"(measured {w1.get('sample_size')} of {w1.get('states_total', '?')} states)")
            a("")
            if w1.get("reason"):
                a(f"- UNMEASURABLE because: {w1['reason']}")
            if w1.get("mismatched_ids"):
                a(f"- blocking word mismatches: **{w1.get('states_blocking_mismatch')}** — "
                  f"`{'`, `'.join(map(str, w1['mismatched_ids']))}`")
            if w1.get("unverifiable_by_reader_ids"):
                a(f"- `UNVERIFIABLE_BY_READER` (the reference's *own* crop does not read "
                  f"either — the reader is the limit, not our ink; advisory, non-blocking): "
                  f"`{'`, `'.join(map(str, w1['unverifiable_by_reader_ids']))}`")
            if w1.get("unresolved_ids"):
                a(f"- unresolved (below the stability floor, no guess made): "
                  f"`{'`, `'.join(map(str, w1['unresolved_ids']))}`")
            a("")
            # NB `blocking: True` means "this state counts toward the verdict" (i.e. the
            # reader could measure it), NOT "this state failed".  MATCHes carry
            # blocking=True too.  The failures are the MISMATCHes.
            rows = [s for s in (w1.get("states") or []) if s.get("verdict") == "MISMATCH"]
            if rows:
                a("| state | declared | reference reads | delivered reads | ref stab | del stab | frame |")
                a("|---|---|---|---|---|---|---|")
                for s in rows:
                    a("| `%s` | %s | **%s** | **%s** | %s | %s | %s |" % (
                        s.get("state_id"), s.get("declared_text"),
                        s.get("reference_read"), s.get("delivered_read"),
                        s.get("reference_stability"), s.get("delivered_stability"),
                        s.get("delivered_frame")))
                a("")

        # W1 corrected (w1_reclass)
        rc = reclass.get(key) or {}
        if rc and rc.get("states_total"):
            a(f"**W1 words, CORRECTED — {plain(rc.get('verdict'))}** "
              f"(track agreement {rc.get('track_agreement_rate')} vs threshold "
              f"{rc.get('track_agreement_threshold')}; "
              f"{rc.get('sample_size')} of {rc.get('states_total')} states had BOTH sides "
              f"resolve to text)")
            a("")
            a(f"- re-derived by `w1_reclass.py` from the same reads, because "
              f"`UNVERIFIABLE_BY_READER` keys on \"reference read != declared text\" and so "
              f"absolves the wrong-words class (see §3.1).")
            if rc.get("reason"):
                a(f"- {rc['reason']}")
            a(f"- words vs the reference: **{rc.get('wrong_words')} differ**, "
              f"{rc.get('words_ok')} agree, {rc.get('unreadable_delivered')} unreadable in the "
              f"delivery, {rc.get('unverifiable')} unverifiable (reference did not resolve)")
            a(f"- CONTRACT check (did we even declare the reference's words?): "
              f"**{plain(rc.get('contract_verdict'))}** — agreement "
              f"{rc.get('contract_agreement_rate')}, "
              f"{rc.get('contract_words_wrong')} declared states differ from the reference")
            a("")
            bad = [r for r in (rc.get("states") or []) if r.get("class") == "WRONG_WORDS"]
            if bad:
                a("| state | declared | reference reads | delivered reads | ref stab | del stab |")
                a("|---|---|---|---|---|---|")
                for r in bad[:24]:
                    a("| `%s` | %s | **%s** | **%s** | %s | %s |" % (
                        r.get("state_id"), r.get("declared_text"), r.get("reference_read"),
                        r.get("delivered_read"), r.get("reference_stability"),
                        r.get("delivered_stability")))
                if len(bad) > 24:
                    a(f"| … | *{len(bad)-24} more in the JSON* | | | | |")
                a("")

        # W2 / W3
        w2, w3 = g(t, "W2"), g(t, "W3")
        if not w2 and g(t, "W2W3"):
            w2 = w3 = g(t, "W2W3")
        if w2:
            a(f"**W2 anatomy — {plain(w2.get('verdict'))}** "
              f"(measured {w2.get('sample_size')} states)  ·  "
              f"**W3 ink — {plain(w3.get('verdict'))}** "
              f"(measured {w3.get('sample_size')} states)")
            a("")
            if w2.get("reason"):
                a(f"- W2 UNMEASURABLE because: {w2['reason']}")
            if w3.get("reason"):
                a(f"- W3 UNMEASURABLE because: {w3['reason']}")
            per = t.get("W2W3_states") or []
            if per:
                a("| state | W2 | Dice | residual p95 px | comps ref→del | holes ref→del | boundary px | W3 | worst sep | worst michelson |")
                a("|---|---|---|---|---|---|---|---|---|---|")
                for p in per:
                    x, y = p.get("W2", {}), p.get("W3", {})
                    a("| `%s` | %s | %s | %s | %s→%s | %s→%s | %s | %s | %s | %s |" % (
                        p.get("state_id"), fmt_verdict(x.get("verdict")),
                        x.get("dice"), x.get("residual_p95_px"),
                        x.get("components_source"), x.get("components_candidate"),
                        x.get("holes_source"), x.get("holes_candidate"),
                        x.get("sample_size"), fmt_verdict(y.get("verdict")),
                        y.get("worst_separation"), y.get("worst_michelson")))
                a("")
                for p in per:
                    x = p.get("W2", {})
                    if x.get("verdict") == "UNMEASURABLE" and x.get("reason"):
                        a(f"- `{p.get('state_id')}` W2 UNMEASURABLE: {x['reason']}")
                a("")

        # W5
        w5 = g(t, "W5")
        if w5:
            a(f"**W5 presence — {plain(w5.get('verdict'))}** "
              f"(measured {w5.get('sample_size')} of {w5.get('states_total')} states)")
            a("")
            if w5.get("reason"):
                a(f"- UNMEASURABLE because: {w5['reason']}")
            if w5.get("missing_ids"):
                a(f"- states whose ink is missing/too faint in the DELIVERED file: "
                  f"**{w5.get('states_missing')}** — `{'`, `'.join(map(str, w5['missing_ids']))}`")
                a("")
                a("| state | reference ink frac | delivered ink frac | delivered/reference | polarity | frames |")
                a("|---|---|---|---|---|---|")
                for s in (w5.get("states") or []):
                    if s.get("verdict") == "FAIL":
                        a("| `%s` | %s | %s | **%s** | %s | %s |" % (
                            s.get("state_id"), s.get("reference_ink_fraction"),
                            s.get("delivered_ink_fraction"),
                            s.get("ratio_delivered_over_reference"),
                            s.get("polarity"), s.get("sample_size")))
                a("")
            if w5.get("unmeasurable_ids"):
                a(f"- UNMEASURABLE states: `{'`, `'.join(map(str, w5['unmeasurable_ids']))}`")
                for s in (w5.get("states") or []):
                    if s.get("verdict") == "UNMEASURABLE":
                        a(f"  - `{s.get('state_id')}`: {s.get('reason')}")
                a("")

        # side probes
        w5c = side(W5C_ROOT, key, "composite-check.json")
        if w5c:
            a(f"**W5c composite presence — {w5c.get('verdict')}** "
              f"(sample_size {w5c.get('sample_size')}): largest contiguous difference from "
              f"the caption-free picture base = **{w5c.get('largest_component_px_over_all_frames')} px** "
              f"(a composited caption is ≥400 px). {w5c.get('reason')}")
            a("")
        w5b = side(W5B_ROOT, key, "band-presence.json")
        if w5b:
            a(f"**W5b band presence — {w5b.get('verdict')}**: {w5b.get('reason') or ''}")
            a("")
        a("")

    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", default=os.path.join(SWEEP_ROOT, "sweep.json"))
    ap.add_argument("--verdicts", default=os.path.join(HERE, "verdicts.jsonl"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--append", default=None, help="extra markdown to append")
    ap.add_argument("--analysis", action="store_true",
                    help="append the hand-written analysis from analysis.py")
    a = ap.parse_args(argv)
    text = build(a.sweep, a.verdicts)
    if a.analysis:
        from analysis import ANALYSIS
        text += "\n" + ANALYSIS
    if a.append and os.path.exists(a.append):
        text += "\n" + open(a.append).read()
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    open(a.out, "w").write(text)
    print("wrote %s (%d bytes)" % (a.out, len(text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
