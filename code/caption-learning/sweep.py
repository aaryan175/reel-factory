"""CAPTION PARITY SWEEP

Runs the caption-learning gate battery over every current deliverable and writes the
machine record the fix lanes work from.

    W1  words     declared vs the REFERENCE's own read-back vs the DELIVERED read-back
                  (wordtruth.triage_delivered - the reference doubles as a control on
                   the reader, so a state Vision cannot read even on known-good ink is
                   UNVERIFIABLE_BY_READER, never a false FAIL)
    W2  anatomy   DOCTRINE 16.2 triple gate over recovered ink masks
                  (Dice AND one-way source->candidate residual p95 AND component+hole
                   equality), reference mask = source, delivered mask = candidate
    W3  ink       per-frame separation / Michelson on the DELIVERED composite, reported
                  relative to the reference's own measurement
    W5  presence  does the DELIVERED file actually carry ink where the contract says a
                  caption is?  (the "captions are missing" gate)

HONESTY RULES (non-negotiable, inherited from the modules):
  * every gate returns verdict in {PASS, FAIL, UNMEASURABLE} plus value(s), sample_size
    and evidence paths;
  * a gate that measured zero pixels / zero frames returns UNMEASURABLE, NEVER PASS;
  * frame indices are ZERO-BASED DECODED ORDER everywhere (ffmpeg -start_number 0 /
    select eq(n,K)); no fps shorthand is used anywhere in this file;
  * a reel that cannot be measured is SKIPPED with a reason, never silently dropped.

Targets are read from a JSON list (--targets, default $CAPTION_SWEEP_ROOT/targets.json;
see targets.example.json for the schema).

Big outputs -> $CAPTION_SWEEP_ROOT/sweep/<target>/
Machine record -> $CAPTION_SWEEP_ROOT/sweep.json

Usage:
    python3 caption-learning/sweep.py --targets targets.example.json --list
    python3 caption-learning/sweep.py --only refB-main,refB-var01
    python3 caption-learning/sweep.py --only refB-var01 --no-w1
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from typing import Dict, List, Optional, Sequence

import cv2
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import anatomy      # noqa: E402
import inkcheck     # noqa: E402
import readback     # noqa: E402
import wordtruth    # noqa: E402

from cl_paths import FFPROBE  # noqa: E402

from cl_paths import REEL_HOME as REELPROD, SWEEP_ROOT as _SWEEP_BASE, WB  # noqa: E402,F401

SWEEP_ROOT = os.path.join(_SWEEP_BASE, "sweep")
OUT_JSON = os.path.join(_SWEEP_BASE, "sweep.json")
DEFAULT_TARGETS = os.path.join(_SWEEP_BASE, "targets.json")

# --- W5 thresholds ------------------------------------------------------------------------
# Ink is detected as "pixels at the extreme of the luma range inside the declared box".
# 205 is the threshold the caption tooling uses for light ink elsewhere in the pipeline.
W5_LIGHT_LUMA = 205
W5_DARK_LUMA = 50
# The reference must itself carry this much ink in the box for the comparison to mean
# anything.  Below it there is nothing to be missing FROM -> UNMEASURABLE.
W5_MIN_REFERENCE_FRACTION = 0.003
# The delivered file must reach this fraction of the reference's own ink density.
W5_MIN_RATIO = 0.25
# ...and a CEILING, because this probe's "ink" is really "pixels at the extreme of the luma
# range inside the box", which cannot tell glyphs from a flat bright/dark field.
#
# Observed in practice: a state box on a frame where the reference is KNOWN to be blank can
# read ~70% LIGHT. That is a white field behind an absent caption, not letterforms.
# Treating that as "the reference's ink" made the delivered file look like it
# had lost a caption it never had to lose. Glyph ink in a state box runs roughly 5-45% of
# the box; above this ceiling the probe is looking at a field and must say so.
W5_MAX_REFERENCE_FRACTION = 0.55


# ==========================================================================================
# frame access
# ==========================================================================================
class FrameStore:
    """Random access to every decoded frame of a video, O(1) RAM.

    A full in-memory decode of a 1036-frame 1440x1080 file is 4.8 GB and the shot-bounds
    pass used to cost another 6.4 GB of float32 grays; that does not fit on smaller
    machines.  So the video is streamed once into a uint8 memmap on the workbench drive and
    read back by index.  Frames and indices are identical to anatomy._decode() -
    zero-based decoded order - which `verify_zero_based_indexing` re-proves against ffmpeg
    on every run.
    """

    def __init__(self, video_path: str, cache_dir: str):
        self.video_path = str(video_path)
        os.makedirs(cache_dir, exist_ok=True)
        stem = os.path.basename(self.video_path).replace(" ", "_")
        self.npy = os.path.join(cache_dir, stem + ".frames.npy")
        self.meta = os.path.join(cache_dir, stem + ".frames.json")
        self._mm: Optional[np.ndarray] = None
        self.diffs: List[float] = []
        self._build()

    def _build(self) -> None:
        if os.path.exists(self.npy) and os.path.exists(self.meta):
            m = json.load(open(self.meta))
            self._mm = np.load(self.npy, mmap_mode="r")
            self.diffs = m["diffs"]
            return
        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open video {self.video_path!r}")
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        # CAP_PROP_FRAME_COUNT can overcount; allocate generously then truncate.
        mm = np.lib.format.open_memmap(
            self.npy + ".part", mode="w+", dtype=np.uint8, shape=(max(n + 8, 8), h, w, 3)
        )
        diffs: List[float] = [0.0]
        prev = None
        i = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if i >= mm.shape[0]:  # pragma: no cover - defensive
                break
            mm[i] = frame
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
            if prev is not None:
                diffs.append(float(np.abs(gray - prev).mean()))
            prev = gray
            i += 1
        cap.release()
        if i == 0:
            raise RuntimeError(f"decoded zero frames from {self.video_path!r}")
        mm.flush()
        del mm
        full = np.load(self.npy + ".part", mmap_mode="r")
        out = np.lib.format.open_memmap(
            self.npy, mode="w+", dtype=np.uint8, shape=(i, h, w, 3)
        )
        out[:] = full[:i]
        out.flush()
        del out, full
        os.remove(self.npy + ".part")
        json.dump({"frames": i, "height": h, "width": w, "diffs": diffs},
                  open(self.meta, "w"))
        self._mm = np.load(self.npy, mmap_mode="r")
        self.diffs = diffs

    def __len__(self) -> int:
        return int(self._mm.shape[0])

    def __getitem__(self, i):
        return np.asarray(self._mm[i])

    def close(self, delete: bool = False) -> None:
        self._mm = None
        if delete:
            for p in (self.npy, self.meta):
                try:
                    os.remove(p)
                except OSError:
                    pass


def verify_zero_based_indexing(store: "FrameStore", frame_idx: int, tmp_dir: str) -> Dict:
    """Re-prove that store[K] is ffmpeg's `select eq(n,K)` frame, on this file, this run."""
    os.makedirs(tmp_dir, exist_ok=True)
    png = os.path.join(tmp_dir, f"_idxcheck_{frame_idx}.png")
    try:
        readback.extract_frame(store.video_path, frame_idx, png)
        via_ffmpeg = cv2.imread(png)
        if via_ffmpeg is None:
            return {"verdict": "UNMEASURABLE", "reason": "ffmpeg produced no frame"}
        a = via_ffmpeg.astype(np.float32)
        b = store[frame_idx].astype(np.float32)
        if a.shape != b.shape:
            return {"verdict": "UNMEASURABLE", "reason": f"shape {a.shape} vs {b.shape}"}
        d_same = float(np.abs(a - b).mean())
        n = len(store)
        d_neigh = None
        if frame_idx + 1 < n:
            d_neigh = float(np.abs(a - store[frame_idx + 1].astype(np.float32)).mean())
        return {
            "verdict": "PASS" if (d_neigh is None or d_same <= d_neigh) else "FAIL",
            "frame_idx": frame_idx,
            "mean_abs_diff_same_index": round(d_same, 4),
            "mean_abs_diff_next_index": None if d_neigh is None else round(d_neigh, 4),
            "sample_size": 1,
            "evidence": {"ffmpeg_png": png},
        }
    except Exception as exc:  # pragma: no cover - diagnostic only
        return {"verdict": "UNMEASURABLE", "reason": f"{type(exc).__name__}: {exc}"}


def probe_wh_frames(path: str):
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=width,height,nb_read_frames", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=900)
        w, h, n = r.stdout.strip().split(",")[:3]
        return int(w), int(h), int(n)
    except Exception:
        return None, None, None


# ==========================================================================================
# W5 - presence
# ==========================================================================================
def _crop(img: np.ndarray, box: Sequence[int]) -> np.ndarray:
    h, w = img.shape[:2]
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((0, 0), np.uint8)
    return img[y0:y1, x0:x1]


def _ink_fraction(bgr: np.ndarray, polarity: str) -> (float, int, int):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if gray.size == 0:
        return 0.0, 0, 0
    hit = (gray >= W5_LIGHT_LUMA) if polarity == "light" else (gray <= W5_DARK_LUMA)
    n = int(hit.sum())
    return n / float(gray.size), n, int(gray.size)


def presence_state(delivered: str, reference: str, state: Dict, out_dir: str,
                   declared_polarity: Optional[str] = None,
                   probe_limit: int = 3) -> Dict:
    """W5: does the DELIVERED file carry caption ink where the contract says it does?

    Polarity is chosen from the REFERENCE alone (never from the delivered file), so the
    probe cannot pick whichever sign makes our own delivery look better.
    """
    os.makedirs(out_dir, exist_ok=True)
    box = state.get("box")
    sid = state.get("id")
    out: Dict = {
        "gate": "presence_W5",
        "state_id": sid,
        "box_xyxy": list(box) if box else None,
        "thresholds": {
            "light_luma_at_or_above": W5_LIGHT_LUMA,
            "dark_luma_at_or_below": W5_DARK_LUMA,
            "min_reference_ink_fraction": W5_MIN_REFERENCE_FRACTION,
            "min_delivered_over_reference_ratio": W5_MIN_RATIO,
        },
        "sample_size": 0,
        "frames": [],
        "evidence": {"out_dir": out_dir},
    }
    if not box:
        out.update(verdict="UNMEASURABLE", reason="state carries no bbox in its contract",
                   reference_ink_fraction=None, delivered_ink_fraction=None)
        return out

    frames = wordtruth.probe_frames(state, limit=probe_limit)
    if not frames:
        out.update(verdict="UNMEASURABLE", reason="state has no usable frame span",
                   reference_ink_fraction=None, delivered_ink_fraction=None)
        return out

    rows = []
    for f in frames:
        rp = os.path.join(out_dir, f"{sid}-f{f}-ref.png")
        dp = os.path.join(out_dir, f"{sid}-f{f}-del.png")
        try:
            readback.extract_frame(reference, f, rp)
            readback.extract_frame(delivered, f, dp)
        except Exception as exc:
            rows.append({"frame": f, "status": "unreadable", "reason": str(exc)[:200]})
            continue
        rimg, dimg = cv2.imread(rp), cv2.imread(dp)
        if rimg is None or dimg is None:
            rows.append({"frame": f, "status": "unreadable",
                         "reason": "a frame did not decode"})
            continue
        rc, dc = _crop(rimg, box), _crop(dimg, box)
        if rc.size == 0 or dc.size == 0:
            rows.append({"frame": f, "status": "unreadable",
                         "reason": "bbox falls outside the raster"})
            continue
        # Measure BOTH polarities on BOTH sides, every frame.
        #
        # The single-polarity version of this probe was wrong and the measurement caught it:
        # on a deliberately restyled reel it reported a state's delivered ink as 0.0 /
        # "missing" while the delivered frame actually carried ~0.41 LIGHT ink in that exact
        # box - the restyled ink sits at the opposite end of the luma range from the
        # reference's.  Choosing one polarity and calling the other end "missing" turns a
        # style difference into a false "caption absent" alarm, which is the single most
        # expensive kind of wrong answer this work order could contain.
        rl, rln, rtot = _ink_fraction(rc, "light")
        rd, rdn, _ = _ink_fraction(rc, "dark")
        dl, dln, _ = _ink_fraction(dc, "light")
        dd, ddn, _ = _ink_fraction(dc, "dark")
        if declared_polarity in ("light", "dark"):
            pol = declared_polarity
        else:
            pol = "light" if rl >= rd else "dark"
        rf, rn = (rl, rln) if pol == "light" else (rd, rdn)
        df, dn = (dl, dln) if pol == "light" else (dd, ddn)
        opp = "dark" if pol == "light" else "light"
        rf_o, df_o = ((rd, dd) if pol == "light" else (rl, dl))
        rows.append({
            "frame": f, "status": "measured", "polarity": pol,
            "reference_ink_fraction": round(rf, 6), "reference_ink_px": rn,
            "delivered_ink_fraction": round(df, 6), "delivered_ink_px": dn,
            "opposite_polarity": opp,
            "reference_ink_fraction_opposite": round(rf_o, 6),
            "delivered_ink_fraction_opposite": round(df_o, 6),
            "box_px": rtot,
            "evidence": {"reference_png": rp, "delivered_png": dp},
        })

    measured = [r for r in rows if r["status"] == "measured"]
    out["frames"] = rows
    out["sample_size"] = len(measured)
    if not measured:
        out.update(verdict="UNMEASURABLE", reason="no probe frame decoded on both sides",
                   reference_ink_fraction=None, delivered_ink_fraction=None)
        return out

    ref_max = max(r["reference_ink_fraction"] for r in measured)
    del_max = max(r["delivered_ink_fraction"] for r in measured)
    out["reference_ink_fraction"] = round(ref_max, 6)
    out["delivered_ink_fraction"] = round(del_max, 6)
    out["ratio_delivered_over_reference"] = (
        round(del_max / ref_max, 4) if ref_max > 0 else None)
    out["polarity"] = measured[0]["polarity"]
    if ref_max < W5_MIN_REFERENCE_FRACTION:
        out.update(
            verdict="UNMEASURABLE",
            reason=(f"the REFERENCE itself carries only {ref_max:.5f} ink fraction in this "
                    f"box (floor {W5_MIN_REFERENCE_FRACTION}); there is nothing to be "
                    f"missing from, so presence cannot be judged here"))
        return out
    if ref_max > W5_MAX_REFERENCE_FRACTION:
        out.update(
            verdict="UNMEASURABLE",
            reason=(f"the REFERENCE's box is {ref_max:.1%} extreme-luma pixels (ceiling "
                    f"{W5_MAX_REFERENCE_FRACTION:.0%}) - that is a flat bright/dark FIELD, "
                    f"not letterforms. This probe measures extreme luma inside a box and "
                    f"cannot separate caption ink from a field behind it, so it refuses to "
                    f"judge presence here. Use W2 (recovered ink masks) or W5c (composite "
                    f"difference against the picture base) for this state."))
        return out
    # Before calling anything "missing", ask whether the ink is simply at the other end of
    # the luma range.  Presence is a question about INK EXISTING, not about its polarity;
    # polarity divergence is a real finding but it belongs to W2/W3, and for a deliberately
    # restyled reel it is not a defect at all.
    del_max_opp = max(r["delivered_ink_fraction_opposite"] for r in measured)
    out["opposite_polarity"] = measured[0]["opposite_polarity"]
    out["delivered_ink_fraction_opposite"] = round(del_max_opp, 6)
    present_same = del_max >= W5_MIN_RATIO * ref_max
    present_opp = del_max_opp >= W5_MIN_RATIO * ref_max
    out["polarity_divergent"] = bool(present_opp and not present_same)
    if present_same:
        out["verdict"] = "PASS"
    elif present_opp:
        out.update(
            verdict="PASS",
            reason=(f"the delivered file carries {del_max_opp:.5f} ink fraction in this box "
                    f"at the OPPOSITE polarity ({out['opposite_polarity']}) while the "
                    f"reference's ink is {out['polarity']}. The caption IS present - its ink "
                    f"sits at the other end of the luma range. Reported as PRESENT with "
                    f"polarity_divergent=true; whether that divergence is a defect is a "
                    f"W2/W3 question, not a presence question."))
    else:
        out.update(
            verdict="FAIL",
            reason=(f"delivered ink fraction {del_max:.5f} is "
                    f"{out['ratio_delivered_over_reference']}x the reference's {ref_max:.5f}, "
                    f"below the {W5_MIN_RATIO} floor - and the opposite polarity carries only "
                    f"{del_max_opp:.5f}, so this is not a polarity artefact"))
    return out


# ==========================================================================================
# W2 / W3
# ==========================================================================================
def _recover(video: str, raw_state: Dict, all_raw: List[Dict], out_dir: str, label: str,
             store: FrameStore) -> Dict:
    """recover_state_mask over the whole span; if that is UNMEASURABLE, walk the span
    frame by frame and take the first frame that IS measurable, recording which."""
    rec = anatomy.recover_state_mask(
        video, raw_state, out_dir, all_states=all_raw, label=label,
        frames_all=store, shot_diffs=store.diffs)
    rec["recovery_frames_strategy"] = "whole_span"
    if rec["verdict"] != "UNMEASURABLE":
        return rec
    start = int(raw_state.get("start_frame", 0))
    end = int(raw_state.get("end_frame_exclusive", start + 1))
    tried = []
    for f in range(start, min(end, len(store))):
        r2 = anatomy.recover_state_mask(
            video, raw_state, out_dir, all_states=all_raw, frames=[f],
            label=f"{label}-f{f}", frames_all=store, shot_diffs=store.diffs)
        tried.append(f)
        if r2["verdict"] != "UNMEASURABLE":
            r2["recovery_frames_strategy"] = "single_frame_fallback"
            r2["frames_tried_before_success"] = tried
            return r2
    rec["frames_tried"] = tried
    rec["recovery_frames_strategy"] = "whole_span_then_every_frame"
    return rec


def anatomy_and_ink(delivered: str, reference: str, raw_state: Dict, all_raw: List[Dict],
                    out_dir: str, del_store: FrameStore, ref_store: FrameStore) -> Dict:
    sid = raw_state.get("id", "state")
    os.makedirs(out_dir, exist_ok=True)
    res: Dict = {"state_id": sid}

    ref_rec = _recover(reference, raw_state, all_raw, out_dir, f"REF-{sid}", ref_store)
    del_rec = _recover(delivered, raw_state, all_raw, out_dir, f"DEL-{sid}", del_store)
    res["reference_recovery"] = anatomy._json_safe(ref_rec)
    res["delivered_recovery"] = anatomy._json_safe(del_rec)

    if ref_rec["verdict"] == "UNMEASURABLE" or del_rec["verdict"] == "UNMEASURABLE":
        which = []
        if ref_rec["verdict"] == "UNMEASURABLE":
            which.append("reference: " + str(ref_rec.get("reason")))
        if del_rec["verdict"] == "UNMEASURABLE":
            which.append("delivered: " + str(del_rec.get("reason")))
        res["W2"] = {"gate": "anatomy_triple_16.2", "verdict": "UNMEASURABLE",
                     "sample_size": 0, "reason": " | ".join(which),
                     "dice": None, "residual_p95_px": None,
                     "evidence": {"out_dir": out_dir}}
    else:
        res["W2"] = anatomy._json_safe(anatomy.compare_masks(
            ref_rec["mask"], del_rec["mask"], out_dir=out_dir,
            label=f"{sid}-REFvsDEL"))

    # W3 - ink cleanliness on the DELIVERED composite, reference-relative
    ref_ink = None
    if ref_rec["verdict"] != "UNMEASURABLE":
        try:
            # This measurement IS the reference: there is nothing for it to be relative to,
            # so it runs the absolute floor only and says so.
            ref_ink = inkcheck.ink_state(
                reference, raw_state, ref_rec["mask"], out_dir=out_dir,
                label=f"REFINK-{sid}", frames_all=ref_store,
                reference_required=False,
                mask_frames=ref_rec.get("state_frames_used"))
        except Exception as exc:
            ref_ink = {"verdict": "UNMEASURABLE",
                       "reason": f"{type(exc).__name__}: {exc}", "sample_size": 0}
    res["reference_ink"] = ref_ink
    if del_rec["verdict"] == "UNMEASURABLE":
        res["W3"] = {"gate": "ink_cleanliness", "verdict": "UNMEASURABLE",
                     "sample_size": 0,
                     "reason": "no delivered ink mask to measure against: "
                               + str(del_rec.get("reason")),
                     "evidence": {"out_dir": out_dir}}
    else:
        try:
            res["W3"] = inkcheck.ink_state(
                delivered, raw_state, del_rec["mask"], out_dir=out_dir,
                label=f"DELINK-{sid}", frames_all=del_store,
                reference_michelson=(ref_ink or {}).get("worst_michelson"),
                reference_separation=(ref_ink or {}).get("worst_separation"))
        except Exception as exc:
            res["W3"] = {"gate": "ink_cleanliness", "verdict": "UNMEASURABLE",
                         "sample_size": 0,
                         "reason": f"{type(exc).__name__}: {exc}"}
    return res


# ==========================================================================================
# contract adapters
# ==========================================================================================
def synth_contract_from_build_plan(dest: str, build_plan: str, caption_states: str) -> Dict:
    """For a project that ships no caption contract: its declared words live in a
    build_plan.json and the reference's caption band was measured into a
    caption-states.json; this stitches them into a reelctl-shaped contract so the gates can
    address the states.  Provenance is recorded in the artefact - SYNTHESIZED, not authored."""
    bp = json.load(open(os.path.expanduser(build_plan)))
    ev = json.load(open(os.path.expanduser(caption_states)))
    band = [int(v) for v in ev["band_xyxy"]]
    lock = bp["captions"].get("reference_lockup", {})
    states = []
    for s in bp["captions"]["states"]:
        states.append({
            "id": "c%02d" % int(s["i"]),
            "text": s["text"],
            "start_frame": int(s["start"]),
            "end_frame_exclusive": int(s["end"]),
            "frames": int(s["end"]) - int(s["start"]),
            "style_id": "T_sans",
            "placement": {"core_bbox_xyxy": band,
                          "treatment_bbox_xyxy": band,
                          "anchor": "band"},
            "ink": {"rgb_median": lock.get("ink_rgb", [252, 251, 250])},
        })
    doc = {
        "schema_version": "SYNTHESIZED-sweep",
        "artifact_type": "caption_contract",
        "provenance": {
            "synthesized_by": "caption-learning/sweep.py",
            "declared_words_from": build_plan + " :: captions.states",
            "bbox_from": caption_states + " :: band_xyxy",
            "caveat": "ONE band box is shared by all states (no per-state geometry was "
                      "recorded). The box is therefore wider than any single word; read-back "
                      "crops tolerate that, mask recovery is noisier for it.",
        },
        "states": states,
    }
    json.dump(doc, open(dest, "w"), indent=1)
    return doc


def synth_contract_from_program(dest: str, program: str, band: Sequence[int],
                                declared_text: Optional[str] = None) -> Dict:
    """For a project that carries a one-state caption program instead of a contract.
    `declared_text` overrides the program's text (e.g. to declare the words a build actually
    rendered); `band` must contain every lockup being compared."""
    prog = json.load(open(os.path.expanduser(program)))
    st = prog["states"][0]
    band = [int(v) for v in band]
    text = declared_text if declared_text is not None else st["text"]
    doc = {
        "schema_version": "SYNTHESIZED-sweep",
        "artifact_type": "caption_contract",
        "provenance": {
            "synthesized_by": "caption-learning/sweep.py",
            "source": program,
            "declared_text_rule": ("declared_text override" if declared_text is not None
                                   else "the program's own text"),
            "bbox_rule": "union band covering every lockup being compared",
        },
        "states": [{
            "id": "c001",
            "text": text,
            "start_frame": int(st["start_frame"]),
            "end_frame_exclusive": int(st["end_frame_exclusive"]),
            "frames": int(st["frames"]),
            "style_id": "T_title",
            "placement": {"core_bbox_xyxy": band, "treatment_bbox_xyxy": band,
                          "anchor": "band"},
            "ink": {"rgb_median": st["ink"]["mean_rgb"]},
        }],
    }
    json.dump(doc, open(dest, "w"), indent=1)
    return doc


def resolve_contract(csrc, out_dir: str):
    """`contract` in a target is a path, or {"synth": "build_plan"|"program", ...}.
    Returns (path, doc) or (None, reason)."""
    if isinstance(csrc, dict):
        kind = csrc.get("synth")
        cpath = os.path.join(out_dir, "contract-SYNTHESIZED-%s.json" % kind)
        if kind == "build_plan":
            return cpath, synth_contract_from_build_plan(
                cpath, csrc["build_plan"], csrc["caption_states"])
        if kind == "program":
            return cpath, synth_contract_from_program(
                cpath, csrc["program"], csrc["band"], csrc.get("declared_text"))
        return None, f"unknown contract synth kind: {kind!r}"
    path = os.path.expanduser(str(csrc))
    if not os.path.exists(path):
        return None, f"contract not on disk: {path}"
    return path, json.load(open(path))


# ==========================================================================================
# targets
# ==========================================================================================
def load_targets(path: str) -> List[Dict]:
    """A JSON list of targets.  Each target:
        key, label, delivered, reference, contract (path or synth spec),
        optional: w1_states, w2_states, review_verdict, note.
    Paths may use ~ and are expanded; a missing file SKIPs that target with a reason."""
    if not os.path.exists(path):
        return []
    doc = json.load(open(path))
    targets = doc["targets"] if isinstance(doc, dict) else doc
    for t in targets:
        for k in ("delivered", "reference"):
            t[k] = os.path.expanduser(t[k])
    return targets


TARGETS: List[Dict] = []


# ==========================================================================================
# driver
# ==========================================================================================
def pick(states: List[Dict], want: Optional[Sequence[str]], n: int = 4) -> List[Dict]:
    if want:
        by_id = {s["id"]: s for s in states}
        got = [by_id[i] for i in want if i in by_id]
        if got:
            return got
    if len(states) <= n:
        return list(states)
    step = max(1, len(states) // n)
    return [states[i] for i in range(0, len(states), step)][:n]


def polarity_of(raw_state: Dict) -> Optional[str]:
    rgb = ((raw_state.get("ink") or {}).get("rgb_median")
           or (raw_state.get("ink") or {}).get("mean_rgb"))
    if not rgb:
        return None
    lum = 0.114 * rgb[0] + 0.587 * rgb[1] + 0.299 * rgb[2]   # BGR-ish order tolerant
    lum2 = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    m = max(lum, lum2)
    if m >= 160:
        return "light"
    if m <= 90:
        return "dark"
    return None


def run_target(t: Dict, do_w1: bool, do_w2: bool, do_w5: bool,
               keep_cache: bool) -> Dict:
    key = t["key"]
    out_dir = os.path.join(SWEEP_ROOT, key)
    os.makedirs(out_dir, exist_ok=True)
    rec: Dict = {
        "key": key, "label": t["label"],
        "review_verdict": t.get("review_verdict"),
        "note": t.get("note"),
        "delivered": t["delivered"], "reference": t["reference"],
        "contract_source": t["contract"],
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "evidence_dir": out_dir,
    }

    for name, p in (("delivered", t["delivered"]), ("reference", t["reference"])):
        if not os.path.exists(p):
            rec.update(status="SKIPPED",
                       reason=f"{name} file not on disk: {p}")
            return rec

    # ---- contract -----------------------------------------------------------------------
    cpath, contract = resolve_contract(t["contract"], out_dir)
    if cpath is None:
        rec.update(status="SKIPPED", reason=contract)
        return rec
    rec["contract_path"] = cpath

    raw_states = contract.get("states") or []
    states = wordtruth.normalize_states(contract)
    rec["states_total"] = len(states)
    rec["states_with_bbox"] = sum(1 for s in states if s.get("box"))
    if not states:
        rec.update(status="SKIPPED", reason="contract carries no states")
        return rec

    # ---- geometry -----------------------------------------------------------------------
    dw, dh, dn = probe_wh_frames(t["delivered"])
    rw, rh, rn = probe_wh_frames(t["reference"])
    rec["geometry"] = {
        "delivered": [dw, dh, dn], "reference": [rw, rh, rn],
        "raster_match": (dw == rw and dh == rh),
        "frame_count_match": (dn == rn),
    }

    # ---- W1 -----------------------------------------------------------------------------
    if do_w1:
        w1_ids = t.get("w1_states")
        if rec["states_with_bbox"] == 0:
            rec["W1"] = {"gate": "triage_delivered", "verdict": "UNMEASURABLE",
                         "sample_size": 0,
                         "reason": ("this contract records no placement geometry for any "
                                    "state, so no state can be localised for read-back")}
        else:
            try:
                t0 = time.time()
                rec["W1"] = wordtruth.triage_delivered(
                    cpath, t["reference"], t["delivered"],
                    os.path.join(out_dir, "w1"), states=w1_ids)
                rec["W1"]["elapsed_s"] = round(time.time() - t0, 1)
            except Exception as exc:
                rec["W1"] = {"gate": "triage_delivered", "verdict": "UNMEASURABLE",
                             "sample_size": 0,
                             "reason": f"{type(exc).__name__}: {exc}",
                             "traceback": traceback.format_exc()[-2000:]}

    # ---- W5 -----------------------------------------------------------------------------
    if do_w5:
        raw_by_id = {s.get("id"): s for s in raw_states}
        rows = []
        for s in states:
            pol = polarity_of(raw_by_id.get(s["id"], {}))
            try:
                rows.append(presence_state(t["delivered"], t["reference"], s,
                                           os.path.join(out_dir, "w5"),
                                           declared_polarity=pol))
            except Exception as exc:
                rows.append({"gate": "presence_W5", "state_id": s["id"],
                             "verdict": "UNMEASURABLE", "sample_size": 0,
                             "reason": f"{type(exc).__name__}: {exc}"})
        fails = [r for r in rows if r["verdict"] == "FAIL"]
        meas = [r for r in rows if r["verdict"] in ("PASS", "FAIL")]
        rec["W5"] = {
            "gate": "presence_W5",
            "verdict": ("UNMEASURABLE" if not meas else ("FAIL" if fails else "PASS")),
            "sample_size": len(meas),
            "states_total": len(rows),
            "states_missing": len(fails),
            "missing_ids": [r["state_id"] for r in fails],
            "polarity_divergent_ids": [r["state_id"] for r in rows
                                       if r.get("polarity_divergent")],
            "unmeasurable_ids": [r["state_id"] for r in rows
                                 if r["verdict"] == "UNMEASURABLE"],
            "states": rows,
            "evidence": {"out_dir": os.path.join(out_dir, "w5")},
        }
        if not meas:
            rec["W5"]["reason"] = "no state could be measured on both sides"

    # ---- W2 / W3 -------------------------------------------------------------------------
    if do_w2:
        raw_with_box = [s for s in raw_states
                        if (s.get("placement") or {}).get("core_bbox_xyxy")]
        if not raw_with_box:
            rec["W2W3"] = {"verdict": "UNMEASURABLE", "sample_size": 0,
                           "reason": "no state carries core_bbox_xyxy; mask recovery "
                                     "cannot be localised"}
        else:
            norm_by_id = {s["id"]: s for s in states}
            chosen_norm = pick([norm_by_id[s["id"]] for s in raw_with_box
                                if s.get("id") in norm_by_id],
                               t.get("w2_states"))
            chosen_ids = [c["id"] for c in chosen_norm]
            chosen_raw = [s for s in raw_with_box if s.get("id") in chosen_ids]
            cache = os.path.join(SWEEP_ROOT, "_framecache")
            del_store = ref_store = None
            try:
                del_store = FrameStore(t["delivered"], cache)
                ref_store = FrameStore(t["reference"], cache)
                mid = min(len(del_store) - 1, max(0, len(del_store) // 2))
                rec["zero_based_index_check"] = verify_zero_based_indexing(
                    del_store, mid, os.path.join(out_dir, "idxcheck"))
                per = []
                for raw in chosen_raw:
                    per.append(anatomy_and_ink(
                        t["delivered"], t["reference"], raw, raw_states,
                        os.path.join(out_dir, "w2w3"), del_store, ref_store))
                rec["W2W3_states"] = per
                w2v = [p["W2"]["verdict"] for p in per]
                w3v = [p["W3"]["verdict"] for p in per]
                rec["W2"] = {
                    "gate": "anatomy_triple_16.2", "sample_size": w2v.count("PASS") + w2v.count("FAIL"),
                    "states_measured": [p["state_id"] for p in per
                                        if p["W2"]["verdict"] != "UNMEASURABLE"],
                    "states_failed": [p["state_id"] for p in per
                                      if p["W2"]["verdict"] == "FAIL"],
                    "states_unmeasurable": [p["state_id"] for p in per
                                            if p["W2"]["verdict"] == "UNMEASURABLE"],
                    "verdict": ("UNMEASURABLE" if "PASS" not in w2v and "FAIL" not in w2v
                                else ("FAIL" if "FAIL" in w2v else "PASS")),
                }
                rec["W3"] = {
                    "gate": "ink_cleanliness", "sample_size": w3v.count("PASS") + w3v.count("FAIL"),
                    "states_failed": [p["state_id"] for p in per
                                      if p["W3"]["verdict"] == "FAIL"],
                    "states_unmeasurable": [p["state_id"] for p in per
                                            if p["W3"]["verdict"] == "UNMEASURABLE"],
                    "verdict": ("UNMEASURABLE" if "PASS" not in w3v and "FAIL" not in w3v
                                else ("FAIL" if "FAIL" in w3v else "PASS")),
                }
            except Exception as exc:
                rec["W2"] = {"gate": "anatomy_triple_16.2", "verdict": "UNMEASURABLE",
                             "sample_size": 0,
                             "reason": f"{type(exc).__name__}: {exc}",
                             "traceback": traceback.format_exc()[-2000:]}
                rec["W3"] = {"gate": "ink_cleanliness", "verdict": "UNMEASURABLE",
                             "sample_size": 0, "reason": "W2 stage raised before W3 ran"}
            finally:
                for st in (del_store, ref_store):
                    if st is not None:
                        # the reference cache is reused across a variant family; the delivered
                        # cache is not
                        st.close(delete=(not keep_cache and st is del_store))

    rec["status"] = "MEASURED"
    rec["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=DEFAULT_TARGETS, help="targets JSON list")
    ap.add_argument("--only", default=None, help="comma-separated target keys")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-w1", action="store_true")
    ap.add_argument("--no-w2", action="store_true")
    ap.add_argument("--no-w5", action="store_true")
    ap.add_argument("--keep-cache", action="store_true")
    ap.add_argument("--out", default=None, help="write this shard instead of the full JSON")
    a = ap.parse_args(argv)
    TARGETS[:] = load_targets(a.targets)
    if not TARGETS:
        print("no targets: %s missing or empty (see targets.example.json)" % a.targets,
              file=sys.stderr)
        return 2

    if a.list:
        for t in TARGETS:
            print("%-14s %s" % (t["key"], t["label"]))
        return 0

    sel = TARGETS
    if a.only:
        want = [x.strip() for x in a.only.split(",") if x.strip()]
        sel = [t for t in TARGETS if t["key"] in want]
        missing = [w for w in want if w not in {t["key"] for t in TARGETS}]
        if missing:
            print("unknown target keys: %s" % missing, file=sys.stderr)
            return 2

    os.makedirs(SWEEP_ROOT, exist_ok=True)
    results = []
    for t in sel:
        print("=" * 88)
        print("TARGET %s" % t["key"])
        print("  delivered: %s" % t["delivered"])
        print("  reference: %s" % t["reference"])
        t0 = time.time()
        r = run_target(t, not a.no_w1, not a.no_w2, not a.no_w5, a.keep_cache)
        r["elapsed_s"] = round(time.time() - t0, 1)
        results.append(r)
        if r.get("status") == "SKIPPED":
            print("  -> SKIPPED: %s" % r["reason"])
        else:
            for g in ("W1", "W2", "W3", "W5"):
                if g in r:
                    v = r[g]
                    print("  %s %-13s n=%-4s %s" % (
                        g, v.get("verdict"), v.get("sample_size"),
                        (v.get("reason") or "")[:120]))
        print("  elapsed %.1fs   evidence %s" % (r["elapsed_s"], r["evidence_dir"]))
        dest = a.out or OUT_JSON
        json.dump({"schema": "caption-parity-sweep",
                   "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "targets": results},
                  open(dest, "w"), indent=1, default=str)
    print("\nwrote %s" % (a.out or OUT_JSON))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
