"""wordtruth.py — reference word-track recovery for the caption-learning toolkit.

WHY
---
Caption doctrine: caption WORDS and FONT are 1:1 with the reference.  Without
an OCR pass over the reference caption track, nothing diffs it against what we
declared, and deliveries that ship our own words instead of the reference's
are only caught by a human reviewer.

WHAT THIS DOES
--------------
Given a reel project's caption contract (states carry text, frames, boxes) and
the reel's reference video, for every state it reports:

  declared_text    what OUR contract says the caption is
  reference_read   what the REFERENCE actually says at that state's midframe,
                   recovered with readback.read_region over the state's box
                   (falling back to a wider lower-third crop when the tight box
                   cannot resolve)
  words_match      MATCH | MISMATCH | UNRESOLVED_TEXT

States whose reference read is UNRESOLVED_TEXT are reported as such and are
EXCLUDED from the pass/fail arithmetic — they are never silently counted as
matches and never guessed at.

HONESTY
-------
The project-level result is a gate dict: PASS / FAIL / UNMEASURABLE.
`sample_size` is the number of states where the reference actually resolved to a
word.  If that is zero the verdict is UNMEASURABLE — a contract whose reference
could not be read has NOT been verified, no matter how many states it has.

SCHEMA TOLERANCE
----------------
Two on-disk shapes exist in the corpus and both are accepted:
  * reelctl contract  : states[].{start_frame,end_frame_exclusive,text,
                        placement.core_bbox_xyxy, id}
  * workbench states  : {id: {start,end,text,box}}  (work/states.json)
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cl_paths import FFPROBE, SWEEP_ROOT  # noqa: E402
from readback import (  # noqa: E402
    read_region,
    readback_image,
    normalize_text,
    STABILITY_FLOOR,
)

CONTRACT_CANDIDATES = (
    "captions/contract-v001.json",
    "captions/contract-inked-v001.json",
    "contracts/contract-v004.json",
    "contracts/contract.json",
)
REFERENCE_CANDIDATES = (
    "reference/reference-source.mp4",
    "reference/reference-source.mov",
)


# ---------------------------------------------------------------------------
# Contract loading / normalisation
# ---------------------------------------------------------------------------
def find_contract(project_dir):
    for rel in CONTRACT_CANDIDATES:
        p = os.path.join(project_dir, rel)
        if os.path.exists(p):
            return p
    return None


def find_reference(project_dir):
    for rel in REFERENCE_CANDIDATES:
        p = os.path.join(project_dir, rel)
        if os.path.exists(p):
            return p
    return None


def normalize_states(contract):
    """Return a list of dicts: {id, text, start, end_exclusive, box, style, tier, plate}."""
    out = []
    raw = contract.get("states") if isinstance(contract, dict) else None

    if isinstance(raw, list):                      # reelctl contract shape
        for i, s in enumerate(raw):
            place = s.get("placement") or {}
            box = place.get("core_bbox_xyxy") or place.get("treatment_bbox_xyxy") or s.get("box")
            ev = s.get("evidence") or {}
            out.append(
                {
                    "id": s.get("id") or "S%02d" % i,
                    "text": s.get("text", ""),
                    "start": s.get("start_frame"),
                    "end_exclusive": s.get("end_frame_exclusive"),
                    "box": list(box) if box else None,
                    "style_id": s.get("style_id"),
                    "tier": ev.get("tier"),
                    "plate": ev.get("mask_path"),
                }
            )
        return out

    src = raw if isinstance(raw, dict) else (contract if isinstance(contract, dict) else {})
    for k, s in src.items():                       # workbench states.json shape
        if not isinstance(s, dict) or "text" not in s:
            continue
        box = s.get("box") or ((s.get("placement") or {}).get("core_bbox_xyxy"))
        start = s.get("start", s.get("start_frame"))
        end = s.get("end", s.get("end_frame_exclusive"))
        out.append(
            {
                "id": s.get("id", k),
                "text": s.get("text", ""),
                "start": start,
                "end_exclusive": end,
                "box": list(box) if box else None,
                "style_id": s.get("style") or s.get("style_id"),
                "tier": s.get("tier"),
                "plate": s.get("plate"),
            }
        )
    out.sort(key=lambda d: (d["start"] if d["start"] is not None else 0, d["id"]))
    return out


def midframe(state):
    """Zero-based decoded-order midframe of a state's [start, end_exclusive) span."""
    s, e = state.get("start"), state.get("end_exclusive")
    if s is None:
        return None
    if e is None or e <= s:
        return int(s)
    return int(s) + (int(e) - int(s)) // 2


def probe_frames(state, limit=4):
    """Frames to try inside a state's span, midframe first.

    A single midframe is not enough: a reference can be genuinely BLANK on a
    state's exact midframe (animated captions blink) — probing only the
    midframe would report UNRESOLVED_TEXT for a state the reference states
    perfectly well one frame either side.  Order: mid, mid+1, mid-1, start,
    end_exclusive-1, then the quartiles.
    """
    s, e = state.get("start"), state.get("end_exclusive")
    if s is None:
        return []
    s = int(s)
    e = int(e) if (e is not None and int(e) > s) else s + 1
    m = midframe(state)
    span = e - s
    cands = [m, m + 1, m - 1, s, e - 1, s + span // 4, s + (3 * span) // 4]
    seen, out = set(), []
    for f in cands:
        if f is None or f < s or f >= e or f in seen:
            continue
        seen.add(f)
        out.append(int(f))
        if len(out) >= limit:
            break
    return out


def lower_third_box(width, height):
    """Fallback crop: the lower third, full width — where subtitles live."""
    return [0, int(round(height * 0.60)), int(width), int(height)]


def padded_box(box, pad_frac=0.20):
    x0, y0, x1, y1 = box
    px, py = int(round(pad_frac * (x1 - x0))), int(round(pad_frac * (y1 - y0)))
    return [x0 - px, y0 - py, x1 + px, y1 + py]


def _overlaps(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def neighbours_in_crop(state, all_states, frame, pad_frac=0.20):
    """Other states live on `frame` whose box falls inside this state's padded crop.

    The 20% pad is generous enough to swallow an adjacent caption (a one-word
    state's crop can pick up its neighbour's word).  When that happens the
    reference read legitimately contains extra words and the MISMATCH is an
    artefact of the crop, not a caption defect — so we label it instead of hiding
    it or silently widening the match.
    """
    crop = padded_box(state["box"], pad_frac)
    out = []
    for o in all_states:
        if o["id"] == state["id"] or not o.get("box"):
            continue
        s, e = o.get("start"), o.get("end_exclusive")
        if s is None:
            continue
        e = e if (e is not None and e > s) else s + 1
        if not (s <= frame < e):
            continue
        if _overlaps(crop, o["box"]):
            out.append(o["id"])
    return out


def _video_wh(path):
    import subprocess

    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        w, h = r.stdout.strip().split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Per-state recovery
# ---------------------------------------------------------------------------
def wordtruth_state(state, reference_video, out_dir, frame_offset=0,
                    fallback=True, ref_wh=None, stability_floor=STABILITY_FLOOR,
                    max_frame_probes=4, all_states=None):
    """Recover the reference read for ONE state and diff it against declared text."""
    declared = state.get("text", "")
    mf = midframe(state)
    frames = [f + frame_offset for f in probe_frames(state, limit=max_frame_probes)]
    rec = {
        "state_id": state["id"],
        "declared_text": declared,
        "declared_normalized": normalize_text(declared),
        "start_frame": state.get("start"),
        "end_frame_exclusive": state.get("end_exclusive"),
        "midframe": (mf + frame_offset) if mf is not None else None,
        "frames_probed": frames,
        "box_xyxy": state.get("box"),
        "tier": state.get("tier"),
    }

    if mf is None or not state.get("box") or not frames:
        rec.update(words_match="UNRESOLVED_TEXT", reason="state_missing_frames_or_box",
                   reference_read=None, reference_stability=None,
                   crop_used=None, frame_used=None, evidence={})
        return rec

    sdir = os.path.join(out_dir, str(state["id"]))

    # 1) the state's own box, midframe first, then neighbours inside the span
    r, crop_used, frame_used = None, "core_bbox", frames[0]
    attempts = []
    for f in frames:
        cand = read_region(reference_video, f, state["box"], sdir,
                           stability_floor=stability_floor, tag="core_f%d" % f)
        attempts.append({"frame": f, "box": "core_bbox", "verdict": cand["verdict"],
                         "modal_read": cand.get("modal_read"),
                         "stability": cand.get("stability"),
                         "sample_size": cand.get("sample_size", 0),
                         "reason": cand.get("reason")})
        if r is None or (cand["verdict"] == "RESOLVED" and r["verdict"] != "RESOLVED"):
            r, frame_used = cand, f
        if cand["verdict"] == "RESOLVED":
            break

    # 2) wider lower-third crop, same frame order
    if r["verdict"] != "RESOLVED" and fallback:
        W, H = ref_wh if ref_wh and ref_wh[0] else _video_wh(reference_video)
        if W:
            wide = lower_third_box(W, H)
            for f in frames:
                cand = read_region(reference_video, f, wide, sdir,
                                   stability_floor=stability_floor, tag="lower3_f%d" % f)
                attempts.append({"frame": f, "box": "lower_third", "verdict": cand["verdict"],
                                 "modal_read": cand.get("modal_read"),
                                 "stability": cand.get("stability"),
                                 "sample_size": cand.get("sample_size", 0),
                                 "reason": cand.get("reason")})
                if cand["verdict"] == "RESOLVED":
                    r, crop_used, frame_used = cand, "lower_third_fallback", f
                    break
            rec["fallback_box_xyxy"] = wide

    rec["attempts"] = attempts
    rec["crop_used"] = crop_used
    rec["frame_used"] = frame_used
    rec["reference_verdict"] = r["verdict"]
    rec["reference_read"] = r.get("modal_read")
    rec["reference_normalized"] = r.get("modal_normalized")
    rec["reference_stability"] = r.get("stability")
    rec["reference_sample_size"] = r.get("sample_size", 0)
    rec["reference_all_reads"] = r.get("all_reads")
    rec["evidence"] = r.get("evidence", {})

    if r["verdict"] != "RESOLVED":
        rec["words_match"] = "UNRESOLVED_TEXT"
        rec["reason"] = r.get("reason")
        rec["best_candidate"] = r.get("best_candidate")
    else:
        rec["words_match"] = (
            "MATCH" if r["modal_normalized"] == rec["declared_normalized"] else "MISMATCH"
        )
        if rec["words_match"] == "MISMATCH":
            dn, rn = rec["declared_normalized"], r["modal_normalized"]
            rec["declared_is_substring_of_reference"] = bool(dn and dn in rn)
            if all_states and crop_used == "core_bbox":
                nb = neighbours_in_crop(state, all_states, frame_used - frame_offset)
                rec["neighbours_in_crop"] = nb
                rec["mismatch_explained_by_neighbour_text"] = bool(
                    nb and rec["declared_is_substring_of_reference"])
    return rec


# ---------------------------------------------------------------------------
# GATE: project-level word truth
# ---------------------------------------------------------------------------
def wordtruth_project(project_dir=None, reference_video=None, out_dir=None,
                      contract_path=None, states=None, frame_offset=0,
                      fallback=True, stability_floor=STABILITY_FLOOR,
                      max_frame_probes=4):
    """Recover the reference word track for a whole contract.

    Returns a gate dict:
      verdict      "PASS"  every resolved state matched (>=1 resolved)
                   "FAIL"  at least one resolved state mismatched
                   "UNMEASURABLE"  no state's reference read resolved
      sample_size  number of states whose reference read RESOLVED
      states       per-state records (declared vs reference_read vs words_match)
    """
    result = {
        "gate": "wordtruth_project",
        "kind": "gate",
        "project_dir": project_dir,
        "doctrine": "caption doctrine (words + font 1:1 with the reference)",
        "frame_indexing": "zero-based decoded order",
        "frame_offset": frame_offset,
    }

    contract_path = contract_path or (find_contract(project_dir) if project_dir else None)
    if not contract_path or not os.path.exists(contract_path):
        result.update(verdict="UNMEASURABLE", reason="contract_not_found",
                      sample_size=0, states=[], evidence={})
        return result
    result["contract_path"] = contract_path

    reference_video = reference_video or (find_reference(project_dir) if project_dir else None)
    if not reference_video or not os.path.exists(reference_video):
        result.update(verdict="UNMEASURABLE", reason="reference_video_not_found",
                      sample_size=0, states=[], evidence={"contract_path": contract_path})
        return result
    result["reference_video"] = reference_video

    with open(contract_path) as fh:
        contract = json.load(fh)
    all_states = normalize_states(contract)
    # the neighbour scan must always see the WHOLE contract, even when the caller
    # asked for a subset — otherwise a filtered run reports a crop as clean when
    # an unlisted state is sitting inside it
    full_states = list(all_states)
    if states:
        want = set(states)
        all_states = [s for s in all_states if s["id"] in want]

    if not all_states:
        result.update(verdict="UNMEASURABLE", reason="contract_has_no_states",
                      sample_size=0, states=[],
                      evidence={"contract_path": contract_path})
        return result

    out_dir = out_dir or os.path.join(
        SWEEP_ROOT, "wordtruth",
        os.path.basename(os.path.normpath(project_dir or "contract")),
    )
    os.makedirs(out_dir, exist_ok=True)
    result["out_dir"] = out_dir

    ref_wh = _video_wh(reference_video)
    result["reference_wh"] = list(ref_wh)

    recs = []
    for st in all_states:
        recs.append(
            wordtruth_state(st, reference_video, out_dir, frame_offset=frame_offset,
                            fallback=fallback, ref_wh=ref_wh,
                            stability_floor=stability_floor,
                            max_frame_probes=max_frame_probes,
                            all_states=full_states)
        )

    resolved = [r for r in recs if r["words_match"] in ("MATCH", "MISMATCH")]
    matched = [r for r in resolved if r["words_match"] == "MATCH"]
    mismatched = [r for r in resolved if r["words_match"] == "MISMATCH"]
    unresolved = [r for r in recs if r["words_match"] == "UNRESOLVED_TEXT"]
    neighbour_explained = [r for r in mismatched
                           if r.get("mismatch_explained_by_neighbour_text")]

    if not resolved:
        verdict, reason = "UNMEASURABLE", "no_state_reference_read_resolved"
    elif mismatched:
        verdict, reason = "FAIL", "reference_words_differ_from_declared"
    else:
        verdict, reason = "PASS", None

    result.update(
        verdict=verdict,
        reason=reason,
        sample_size=len(resolved),
        states_total=len(recs),
        states_matched=len(matched),
        states_mismatched=len(mismatched),
        states_unresolved=len(unresolved),
        match_rate=(round(len(matched) / float(len(resolved)), 4) if resolved else None),
        unresolved_ids=[r["state_id"] for r in unresolved],
        mismatched_ids=[r["state_id"] for r in mismatched],
        mismatched_explained_by_neighbour_text=[r["state_id"] for r in neighbour_explained],
        mismatched_unexplained_ids=[r["state_id"] for r in mismatched
                                    if not r.get("mismatch_explained_by_neighbour_text")],
        states=recs,
    )

    report = os.path.join(out_dir, "wordtruth.json")
    try:
        with open(report, "w") as fh:
            json.dump(result, fh, indent=1)
        result["evidence"] = {"report_json": report, "out_dir": out_dir,
                              "contract_path": contract_path,
                              "reference_video": reference_video}
    except OSError:
        result["evidence"] = {"out_dir": out_dir}
    return result


# ---------------------------------------------------------------------------
# Three-way triage: declared vs REFERENCE vs DELIVERED
# ---------------------------------------------------------------------------
def triage_delivered(contract_path, reference_video, delivered_video, out_dir,
                     states=None, frame_offset=0, max_frame_probes=4,
                     stability_floor=STABILITY_FLOOR):
    """Judge a DELIVERED file's words, using the reference as the reader's control.

    The problem this solves: OCR can fail to read a reference's own ornate-script
    state (the REFERENCE crop itself reads as a near-miss string).  Scoring the
    delivered file against the declared string there produces a FAIL on a state a
    reviewer approved.

    So the reference is used as a control on the reader, not just as truth:

      reference reads == declared   -> the reader demonstrably handles this state,
                                       so the delivered read is BLOCKING
                                       (MATCH / MISMATCH)
      reference reads != declared   -> the reader cannot reproduce the declared
                                       word even on known-good ink
                                       -> UNVERIFIABLE_BY_READER (advisory only;
                                          the differential read is still reported)
      neither resolves              -> UNRESOLVED_TEXT

    Nothing here is ever upgraded to PASS on the strength of an unread state.
    """
    with open(contract_path) as fh:
        contract = json.load(fh)
    full_states = normalize_states(contract)
    sel = full_states
    if states:
        want = set(states)
        sel = [s for s in full_states if s["id"] in want]
    os.makedirs(out_dir, exist_ok=True)

    ref_wh, del_wh = _video_wh(reference_video), _video_wh(delivered_video)
    rows = []
    for st in sel:
        rec_ref = wordtruth_state(st, reference_video, os.path.join(out_dir, "reference"),
                                  frame_offset=frame_offset, ref_wh=ref_wh,
                                  stability_floor=stability_floor,
                                  max_frame_probes=max_frame_probes,
                                  all_states=full_states)
        rec_del = wordtruth_state(st, delivered_video, os.path.join(out_dir, "delivered"),
                                  frame_offset=frame_offset, ref_wh=del_wh,
                                  stability_floor=stability_floor,
                                  max_frame_probes=max_frame_probes,
                                  all_states=full_states)
        dn = normalize_text(st.get("text", ""))
        rn, vn = rec_ref.get("reference_normalized"), rec_del.get("reference_normalized")

        nbrs = (rec_ref.get("neighbours_in_crop") or rec_del.get("neighbours_in_crop")
                or neighbours_in_crop(st, full_states,
                                      (rec_del.get("frame_used") or 0) - frame_offset))
        if rn is not None and rn == dn:
            if vn == dn:
                verdict, blocking = "MATCH", True
            elif nbrs and vn and dn and dn in vn:
                # the delivered crop legitimately contains an adjacent caption as
                # well as ours; reported, but not blocking on its own
                verdict, blocking = "MATCH_PLUS_NEIGHBOUR_TEXT", False
            else:
                verdict, blocking = "MISMATCH", True
        elif rn is None and vn is None:
            verdict, blocking = "UNRESOLVED_TEXT", False
        else:
            verdict, blocking = "UNVERIFIABLE_BY_READER", False

        rows.append({
            "state_id": st["id"], "declared_text": st.get("text"),
            "declared_normalized": dn, "style_id": st.get("style_id"),
            "reference_read": rec_ref.get("reference_read"),
            "reference_stability": rec_ref.get("reference_stability"),
            "reference_frame": rec_ref.get("frame_used"),
            "delivered_read": rec_del.get("reference_read"),
            "delivered_stability": rec_del.get("reference_stability"),
            "delivered_frame": rec_del.get("frame_used"),
            "neighbours_in_crop": nbrs,
            "reads_agree": (rn is not None and rn == vn),
            "verdict": verdict, "blocking": blocking,
            "evidence": {"reference": rec_ref.get("evidence", {}),
                         "delivered": rec_del.get("evidence", {})},
        })

    blocking_rows = [r for r in rows if r["blocking"]]
    bad = [r for r in blocking_rows if r["verdict"] == "MISMATCH"]
    out = {
        "gate": "triage_delivered",
        "kind": "gate",
        "contract_path": contract_path,
        "reference_video": reference_video,
        "delivered_video": delivered_video,
        "sample_size": len(blocking_rows),
        "states_total": len(rows),
        "states_blocking_mismatch": len(bad),
        "mismatched_ids": [r["state_id"] for r in bad],
        "unverifiable_by_reader_ids": [r["state_id"] for r in rows
                                       if r["verdict"] == "UNVERIFIABLE_BY_READER"],
        "unresolved_ids": [r["state_id"] for r in rows
                           if r["verdict"] == "UNRESOLVED_TEXT"],
        "match_plus_neighbour_text_ids": [r["state_id"] for r in rows
                                          if r["verdict"] == "MATCH_PLUS_NEIGHBOUR_TEXT"],
        "verdict": ("UNMEASURABLE" if not blocking_rows
                    else ("FAIL" if bad else "PASS")),
        "states": rows,
    }
    if not blocking_rows:
        out["reason"] = "no_state_had_a_reference_read_matching_its_declared_text"
    report = os.path.join(out_dir, "triage.json")
    try:
        with open(report, "w") as fh:
            json.dump(out, fh, indent=1)
        out["evidence"] = {"report_json": report, "out_dir": out_dir}
    except OSError:
        out["evidence"] = {"out_dir": out_dir}
    return out


# ---------------------------------------------------------------------------
# Plate-side companion: declared vs our own plates (no reference needed)
# ---------------------------------------------------------------------------
def platetruth_contract(contract_path, plate_root, out_dir, states=None,
                        threshold=None):
    """Run readback_image over every state's plate mask vs its declared text.

    This is the *self* half of the doctrine (is our own ink legible as the word
    we declared) and needs no reference video.  Returns a gate dict.
    """
    from readback import AGREEMENT_THRESHOLD

    threshold = AGREEMENT_THRESHOLD if threshold is None else threshold
    with open(contract_path) as fh:
        contract = json.load(fh)
    st = normalize_states(contract)
    if states:
        want = set(states)
        st = [s for s in st if s["id"] in want]

    os.makedirs(out_dir, exist_ok=True)
    rows, measured = [], 0
    for s in st:
        plate = s.get("plate")
        p = os.path.join(plate_root, plate) if plate and not os.path.isabs(plate) else plate
        if not p or not os.path.exists(p):
            rows.append({"state_id": s["id"], "declared_text": s["text"],
                         "verdict": "UNMEASURABLE", "reason": "plate_missing",
                         "plate": p, "agreement": None, "sample_size": 0})
            continue
        d = readback_image(p, s["text"], out_dir=os.path.join(out_dir, s["id"]),
                           threshold=threshold)
        if d["sample_size"] > 0:
            measured += 1
        rows.append({
            "state_id": s["id"], "declared_text": s["text"], "plate": p,
            "verdict": d["verdict"], "agreement": d.get("agreement"),
            "modal_read": d.get("modal_read"),
            "modal_agreement": d.get("modal_agreement"),
            "sample_size": d["sample_size"],
            "evidence": d.get("evidence", {}),
        })

    failed = [r for r in rows if r["verdict"] == "FAIL"]
    out = {
        "gate": "platetruth_contract",
        "kind": "gate",
        "contract_path": contract_path,
        "plate_root": plate_root,
        "threshold": threshold,
        "sample_size": measured,
        "states_total": len(rows),
        "states_failed": len(failed),
        "failed_ids": [r["state_id"] for r in failed],
        "verdict": ("UNMEASURABLE" if measured == 0
                    else ("FAIL" if failed else "PASS")),
        "states": rows,
    }
    if measured == 0:
        out["reason"] = "no_plate_produced_a_measurable_cell"
    report = os.path.join(out_dir, "platetruth.json")
    try:
        with open(report, "w") as fh:
            json.dump(out, fh, indent=1)
        out["evidence"] = {"report_json": report, "out_dir": out_dir}
    except OSError:
        out["evidence"] = {"out_dir": out_dir}
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv):
    import argparse

    ap = argparse.ArgumentParser(description="reference word-track recovery")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("project", help="declared vs reference read, per state")
    p1.add_argument("project_dir", nargs="?", default=None)
    p1.add_argument("--contract", default=None)
    p1.add_argument("--reference", default=None)
    p1.add_argument("--out-dir", default=None)
    p1.add_argument("--states", default=None, help="comma-separated state ids")
    p1.add_argument("--frame-offset", type=int, default=0)

    p3 = sub.add_parser("triage", help="declared vs reference vs delivered (3-way)")
    p3.add_argument("contract")
    p3.add_argument("reference")
    p3.add_argument("delivered")
    p3.add_argument("out_dir")
    p3.add_argument("--states", default=None)

    p2 = sub.add_parser("plates", help="declared vs our own plate masks")
    p2.add_argument("contract")
    p2.add_argument("plate_root")
    p2.add_argument("out_dir")
    p2.add_argument("--states", default=None)

    a = ap.parse_args(argv)
    ids = a.states.split(",") if getattr(a, "states", None) else None
    if a.cmd == "project":
        d = wordtruth_project(a.project_dir, reference_video=a.reference,
                              out_dir=a.out_dir, contract_path=a.contract,
                              states=ids, frame_offset=a.frame_offset)
        slim = {k: v for k, v in d.items() if k != "states"}
        print(json.dumps(slim, indent=1))
        for r in d.get("states", []):
            note = r.get("reason") or ""
            if r.get("mismatch_explained_by_neighbour_text"):
                note = "crop also holds %s" % ",".join(r.get("neighbours_in_crop") or [])
            print("  %-9s %-16s declared=%-34r ref=%-30r f=%-5s stab=%-7s %s" % (
                r["state_id"], r["words_match"], r["declared_text"],
                r.get("reference_read"), r.get("frame_used"),
                r.get("reference_stability"), note))
    elif a.cmd == "triage":
        d = triage_delivered(a.contract, a.reference, a.delivered, a.out_dir, states=ids)
        print(json.dumps({k: v for k, v in d.items() if k != "states"}, indent=1))
        for r in d["states"]:
            print("  %-9s %-22s declared=%-22r ref=%-22r del=%-22r blocking=%-5s %s" % (
                r["state_id"], r["verdict"], r["declared_text"], r["reference_read"],
                r["delivered_read"], r["blocking"],
                ("crop also holds %s" % ",".join(r["neighbours_in_crop"]))
                if r["neighbours_in_crop"] else ""))
    else:
        d = platetruth_contract(a.contract, a.plate_root, a.out_dir, states=ids)
        print(json.dumps({k: v for k, v in d.items() if k != "states"}, indent=1))
        for r in d["states"]:
            print("  %-6s %-13s agree=%-6s modal=%-22r n=%d" % (
                r["state_id"], r["verdict"], r.get("agreement"),
                r.get("modal_read"), r.get("sample_size")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
