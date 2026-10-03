"""readback.py — the plate/crop OCR ensemble gate for the caption-learning toolkit.

WHAT THIS IS
------------
Self-consistency caption gates (timing, plate topology, Dice-vs-own-plan) never
read a delivered word.  This module asks the reviewer's actual question: *is
this the declared word, legibly?*

PROTOCOL (fixed)
----------------
* macOS Vision `VNRecognizeTextRequest` via `VNImageRequestHandler` (requires
  pyobjc's Vision bindings plus numpy and Pillow).
* OCR runs on **isolated caption plates or tight upscaled region crops, NEVER on
  full composite frames** — full-frame Vision OCR returns garbage even on a
  pristine reference.
* Vision's per-read confidence is **BANNED as a gate input** — it was measured
  anti-correlated with correctness.  We record it in evidence for forensics only
  and never branch on it.
* The signal is *ensemble agreement*: 12 independent renderings of the same ink
  (4 cap-height normalisations x 3 pad factors), each OCR'd separately.  A word
  whose glyph anatomy is intact reads the same at every scale; a word with fused
  or eroded strokes ("Garden" -> "Gorden"/"Gerden") disagrees with itself.

HONESTY VOCABULARY
------------------
`readback_image()` is a GATE: verdict is PASS / FAIL / UNMEASURABLE.
  UNMEASURABLE is returned whenever the ensemble measured **zero cells** or the
  image carried **zero ink pixels** — never PASS.  (A probe that compares zero
  pixels and reports PASS is the failure class this rule exists to kill.)
`read_region()` is a RECOVERY primitive, not a gate: verdict is
  RESOLVED / UNRESOLVED_TEXT / UNMEASURABLE.  It never guesses — if no single
  read reaches 1/3 of the cells it says UNRESOLVED_TEXT.

THRESHOLD PROVENANCE
--------------------
AGREEMENT_THRESHOLD = 0.25 is *derived* from review verdicts, not chosen: in
calibration, reviewer-rejected states measured <= 0.08 and accepted states
>= 0.50.  Moving it requires a motivating fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Fixed ensemble geometry (12 cells).  Do not change without a fixture.
# ---------------------------------------------------------------------------
ENSEMBLE_HEIGHTS = (60, 120, 240, 480)   # px, cap-height normalisation targets
ENSEMBLE_PADS = (0.5, 1.5, 3.0)          # pad = factor * normalised height
ENSEMBLE_CELLS = len(ENSEMBLE_HEIGHTS) * len(ENSEMBLE_PADS)   # == 12

AGREEMENT_THRESHOLD = 0.25   # see THRESHOLD PROVENANCE above
STABILITY_FLOOR = 1.0 / 3.0  # read_region: modal read must reach >= 1/3 of cells

from cl_paths import FFMPEG  # noqa: E402

_MAX_CELL_PIXELS = 40_000_000   # guard: refuse to build absurd canvases


# ---------------------------------------------------------------------------
# Vision OCR
# ---------------------------------------------------------------------------
_VISION_LOADED = False


def _load_vision():
    """Load the Vision framework into module globals once."""
    global _VISION_LOADED
    if _VISION_LOADED:
        return
    import objc  # noqa: F401  (pyobjc is present in system python3)

    objc.loadBundle(
        "Vision", globals(), bundle_path="/System/Library/Frameworks/Vision.framework"
    )
    if "VNRecognizeTextRequest" not in globals() or "VNImageRequestHandler" not in globals():
        raise RuntimeError("Vision framework loaded but VNRecognizeTextRequest is absent")
    _VISION_LOADED = True


def ocr_png(path):
    """OCR one PNG with macOS Vision.  Returns (joined_text, observations).

    `observations` is a list of dicts: {text, confidence, bbox}.  `confidence` is
    recorded for forensics ONLY — it is banned as a gate input.
    Reading order is top-to-bottom then left-to-right (Vision's normalised
    bounding boxes have origin bottom-left, so we sort by -y then +x).
    """
    _load_vision()
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(str(path))
    handler = VNImageRequestHandler.alloc().initWithURL_options_(url, None)  # noqa: F821
    req = VNRecognizeTextRequest.alloc().init()  # noqa: F821
    req.setRecognitionLevel_(0)          # 0 = accurate
    req.setUsesLanguageCorrection_(False)  # never let a dictionary invent the word
    handler.performRequests_error_([req], None)

    obs = []
    for o in (req.results() or []):
        cands = o.topCandidates_(1)
        if not cands:
            continue
        bb = o.boundingBox()
        obs.append(
            {
                "text": str(cands[0].string()),
                "confidence": float(cands[0].confidence()),  # forensics only
                "bbox": [float(bb.origin.x), float(bb.origin.y),
                         float(bb.size.width), float(bb.size.height)],
            }
        )
    obs.sort(key=lambda d: (-d["bbox"][1], d["bbox"][0]))
    return " ".join(d["text"] for d in obs), obs


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------
_STRIP_RE = re.compile(r"[^0-9a-z]+")


def normalize_text(s):
    """casefold + NFKC + strip every punctuation/whitespace character.

    "that's" -> "thats" ; "Garden" -> "garden" ;
    "here is the sample line:" -> "hereisthesampleline".
    Whitespace is stripped entirely so a Vision line-split never counts as a
    mismatch.
    """
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    return _STRIP_RE.sub("", s)


# ---------------------------------------------------------------------------
# Ink geometry
# ---------------------------------------------------------------------------
# Fixed, declared projection order.  Caption ink is frequently CHROMATIC against
# its footage (e.g. crimson ink over a dark-brown night plate — luma
# separation is near zero, a* separation is large), so a luma-only read-back
# under-reports legible words.  The order is FIXED and the first projection that
# yields a stable modal read wins; selection never looks at the declared text, so
# it cannot be tuned to manufacture agreement.
PROJECTION_ORDER = ("luma", "lab_a", "lab_b", "pc1", "pc2")


def _norm8(x):
    """Percentile-stretch a float plane to uint8 (0.5/99.5 clip)."""
    lo, hi = np.percentile(x, 0.5), np.percentile(x, 99.5)
    if hi - lo < 1e-6:
        return np.zeros(x.shape, np.uint8)
    return np.clip((x - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)


def _is_achromatic(rgb_arr):
    ch = rgb_arr.astype(np.int16)
    return bool(np.abs(ch[:, :, 0] - ch[:, :, 1]).max() <= 2
                and np.abs(ch[:, :, 1] - ch[:, :, 2]).max() <= 2)


def projections(img):
    """Yield (name, PIL 'L' image) in PROJECTION_ORDER.

    * an informative alpha matte (a plate mask) IS the ink -> single projection
    * an achromatic image (grey mask, grey frame) -> luma only; the chroma and
      PC axes are degenerate and would only add noise
    """
    if img.mode in ("RGBA", "LA"):
        a = np.asarray(img.getchannel("A"))
        if a.min() < 250:
            return [("alpha", Image.fromarray(a).convert("L"))]

    rgb = img.convert("RGB")
    arr = np.asarray(rgb)
    lum = rgb.convert("L")
    if _is_achromatic(arr):
        return [("luma", lum)]

    out = {"luma": lum}
    lab = np.asarray(rgb.convert("LAB")).astype(np.float64)
    out["lab_a"] = Image.fromarray(_norm8(lab[:, :, 1])).convert("L")
    out["lab_b"] = Image.fromarray(_norm8(lab[:, :, 2])).convert("L")

    h, w, _ = arr.shape
    X = arr.reshape(-1, 3).astype(np.float64)
    X = X - X.mean(0)
    sample = X
    if X.shape[0] > 200_000:
        idx = np.random.RandomState(0).choice(X.shape[0], 200_000, replace=False)
        sample = X[idx]
    try:
        # macOS Accelerate raises spurious FP flags from large float64 matmuls;
        # the values are correct, so silence the flags rather than the maths.
        with np.errstate(all="ignore"):
            cov = np.cov(sample.T)
            _, evec = np.linalg.eigh(cov)
            out["pc1"] = Image.fromarray(_norm8((X @ evec[:, -1]).reshape(h, w))).convert("L")
            out["pc2"] = Image.fromarray(_norm8((X @ evec[:, -2]).reshape(h, w))).convert("L")
    except np.linalg.LinAlgError:
        pass

    return [(n, out[n]) for n in PROJECTION_ORDER if n in out]


def _to_gray(img):
    """Back-compat single-projection accessor: first projection in the fixed order."""
    name, g = projections(img)[0]
    return g, name


def _background_level(a):
    """Median of a border ring (outer 10%, min 1px) = the background estimate."""
    h, w = a.shape
    t = max(1, int(round(0.10 * min(h, w))))
    ring = np.concatenate(
        [a[:t, :].ravel(), a[-t:, :].ravel(), a[:, :t].ravel(), a[:, -t:].ravel()]
    )
    return float(np.median(ring))


def ink_polarity(a):
    """'dark' if the ink is darker than its background, else 'light'.

    Works for both hard masks (bimodal 0/255) and composite crops.
    """
    bg = _background_level(a)
    dev = np.abs(a.astype(np.float32) - bg)
    thr = max(16.0, 0.25 * float(dev.max()) if dev.size else 16.0)
    ink = a[dev >= thr]
    if ink.size == 0:
        return ("dark" if bg > 127 else "light"), bg, 0
    return ("dark" if float(ink.mean()) < bg else "light"), bg, int(ink.size)


def ink_bbox(a, bg, polarity):
    """Tight bbox of ink pixels (x0,y0,x1,y1) exclusive, or None if no ink."""
    dev = np.abs(a.astype(np.float32) - bg)
    thr = max(16.0, 0.25 * float(dev.max()) if dev.size else 16.0)
    mask = dev >= thr
    if polarity == "dark":
        mask &= a.astype(np.float32) < bg
    else:
        mask &= a.astype(np.float32) > bg
    ys, xs = np.where(mask)
    if ys.size == 0:
        return None, 0
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1), int(ys.size)


# ---------------------------------------------------------------------------
# The 12-cell ensemble
# ---------------------------------------------------------------------------
def build_cells(gray, out_dir, tag, height_basis="ink_bbox", bg_fill=None,
                polarity=None, bg_level=None):
    """Render the fixed 12-cell ensemble to PNGs.  Returns (cells, meta).

    height_basis:
      "ink_bbox"  — crop to the ink bbox and scale that height to the target
                    (used for isolated plates; the bbox height is a cap-height
                    proxy and is reported as such, never as a measured cap height)
      "crop_box"  — scale the whole crop's height to the target (used for
                    composite region crops where ink cannot be isolated)
    bg_fill: the pad colour.  Default = the estimated background level, which for
    a hard mask is exactly black (light ink) or white (dark ink) — i.e. the
    background is picked by ink polarity, as the protocol requires.
    """
    a = np.asarray(gray)
    if polarity is None or bg_level is None:
        polarity, bg_level, _ = ink_polarity(a)
    box, ink_px = ink_bbox(a, bg_level, polarity)

    meta = {
        "polarity": polarity,
        "background_level": round(float(bg_level), 2),
        "ink_pixels": ink_px,
        "ink_bbox_xyxy": list(box) if box else None,
        "height_basis": height_basis,
        "source_wh": [gray.width, gray.height],
    }

    if height_basis == "ink_bbox":
        if box is None:
            meta["cap_height_proxy_px"] = 0
            return [], meta
        src = gray.crop(box)
    else:
        src = gray
    if src.width == 0 or src.height == 0:
        meta["cap_height_proxy_px"] = 0
        return [], meta
    meta["cap_height_proxy_px"] = src.height

    fill = int(round(bg_level if bg_fill is None else bg_fill))
    fill = max(0, min(255, fill))
    meta["pad_fill"] = fill

    os.makedirs(out_dir, exist_ok=True)
    cells = []
    for H in ENSEMBLE_HEIGHTS:
        scale = float(H) / float(src.height)
        nw = max(1, int(round(src.width * scale)))
        nh = max(1, int(round(src.height * scale)))
        resample = Image.LANCZOS
        scaled = src.resize((nw, nh), resample)
        for pf in ENSEMBLE_PADS:
            pad = int(round(pf * H))
            cw, ch = nw + 2 * pad, nh + 2 * pad
            if cw * ch > _MAX_CELL_PIXELS:
                cells.append(
                    {"height": H, "pad_factor": pf, "png": None, "read": None,
                     "read_normalized": None, "skipped": "canvas_too_large",
                     "canvas_wh": [cw, ch]}
                )
                continue
            canvas = Image.new("L", (cw, ch), fill)
            canvas.paste(scaled, (pad, pad))
            png = os.path.join(out_dir, "%s_h%d_p%s.png" % (tag, H, str(pf).replace(".", "")))
            canvas.save(png)
            text, obs = ocr_png(png)
            cells.append(
                {
                    "height": H,
                    "pad_factor": pf,
                    "png": png,
                    "canvas_wh": [cw, ch],
                    "read": text,
                    "read_normalized": normalize_text(text),
                    "observations": obs,   # includes Vision confidence: forensics only
                }
            )
    return cells, meta


def auto_height_basis(img):
    """Pick the cap-height normalisation basis from the image itself.

    "ink_bbox"  — a hard matte: an informative alpha channel, or a background
                  pinned to pure black/white (a recovered plate mask).  The ink
                  can be isolated exactly, so normalise on the ink's own bbox.
    "crop_box"  — a composite region crop: ink cannot be isolated from footage,
                  so normalise on the crop's height and say so in evidence.
    """
    if img.mode in ("RGBA", "LA"):
        a = np.asarray(img.getchannel("A"))
        if a.min() < 250:
            return "ink_bbox"
    g = np.asarray(img.convert("L"))
    bg = _background_level(g)
    return "ink_bbox" if (bg <= 2.0 or bg >= 253.0) else "crop_box"


def ensemble(img, out_dir, tag, height_basis="auto",
             stability_floor=STABILITY_FLOOR):
    """Run the 12-cell ensemble over projections in the FIXED declared order.

    Stops at the first projection whose modal read is non-empty and reaches
    `stability_floor`.  Selection is blind to any declared text.  Returns
    (cells, meta, projection_used, per_projection, resolved_bool).
    """
    if height_basis == "auto":
        height_basis = auto_height_basis(img)
    projs = projections(img)
    per_projection, adopted = [], None
    for name, gray in projs:
        cells, meta = build_cells(gray, out_dir, "%s_%s" % (tag, name),
                                  height_basis=height_basis)
        meta["projection"] = name
        modal_norm, modal_count, measured_n, _ = _modal(cells)
        stab = (modal_count / float(measured_n)) if measured_n else 0.0
        ok = bool(measured_n) and modal_norm != "" and stab >= stability_floor
        per_projection.append({
            "projection": name, "sample_size": measured_n,
            "modal_normalized": modal_norm, "stability": round(stab, 4),
            "ink_pixels": meta.get("ink_pixels", 0), "resolved": ok,
        })
        if adopted is None and ok:
            adopted = (cells, meta, name)
        if ok:
            break
    if adopted is not None:
        cells, meta, name = adopted
        return cells, meta, name, per_projection, True
    # nothing resolved: report the FIRST projection's cells (the declared default)
    cells, meta = build_cells(projs[0][1], out_dir, "%s_%s" % (tag, projs[0][0]),
                              height_basis=height_basis)
    meta["projection"] = projs[0][0]
    return cells, meta, projs[0][0], per_projection, False


def _modal(cells):
    """Modal normalised read over cells that actually produced an OCR run."""
    measured = [c for c in cells if c.get("skipped") is None]
    norms = [c["read_normalized"] for c in measured]
    if not norms:
        return None, 0, 0, measured
    cnt = Counter(norms)
    # Prefer a non-empty modal read when it ties with the empty read: an empty
    # read is "Vision saw no text", not evidence of a word.
    best = max(cnt.items(), key=lambda kv: (kv[1], kv[0] != ""))
    modal_norm, modal_count = best
    return modal_norm, modal_count, len(measured), measured


def _raw_for(measured, modal_norm):
    for c in measured:
        if c["read_normalized"] == modal_norm:
            return c["read"]
    return ""


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _slug(s, n=40):
    s = re.sub(r"[^0-9A-Za-z]+", "-", str(s)).strip("-")
    return (s[:n] or "x")


# ---------------------------------------------------------------------------
# GATE: readback_image
# ---------------------------------------------------------------------------
def readback_image(png_path, declared_text, out_dir=None, threshold=AGREEMENT_THRESHOLD,
                   height_basis="auto"):
    """Ensemble-OCR an isolated caption plate/crop and score it against `declared_text`.

    Returns a gate dict:
      verdict            "PASS" | "FAIL" | "UNMEASURABLE"
      agreement          fraction of measured cells whose normalised read == declared
      modal_read         the most common raw read (what the ink actually says)
      modal_agreement    fraction of measured cells agreeing with the modal read
      sample_size        number of cells that actually ran OCR (0 => UNMEASURABLE)
      evidence           {cells_dir, cell_pngs, report_json, source_png, source_sha256, ...}
    """
    png_path = str(png_path)
    declared_norm = normalize_text(declared_text)

    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="readback-")
        ephemeral = True
    else:
        ephemeral = False
    os.makedirs(out_dir, exist_ok=True)

    base = {
        "gate": "readback_image",
        "kind": "gate",
        "protocol": {
            "heights": list(ENSEMBLE_HEIGHTS),
            "pad_factors": list(ENSEMBLE_PADS),
            "cells_planned": ENSEMBLE_CELLS,
            "threshold": threshold,
            "ocr": "macOS Vision VNRecognizeTextRequest (accurate, language correction OFF)",
            "confidence_used_as_gate_input": False,
        },
        "declared_text": declared_text,
        "declared_normalized": declared_norm,
        "source_png": png_path,
    }

    if not os.path.exists(png_path):
        base.update(verdict="UNMEASURABLE", reason="source_png_missing",
                    agreement=None, modal_read=None, modal_agreement=None,
                    sample_size=0, cells=[], evidence={"cells_dir": out_dir})
        return base

    base["source_sha256"] = _sha256(png_path)

    if declared_norm == "":
        base.update(verdict="UNMEASURABLE", reason="declared_text_empty",
                    agreement=None, modal_read=None, modal_agreement=None,
                    sample_size=0, cells=[], evidence={"cells_dir": out_dir})
        return base

    img = Image.open(png_path)
    tag = "%s_%s" % (_slug(os.path.splitext(os.path.basename(png_path))[0]),
                     _slug(declared_text, 20))
    cells, meta, proj, per_proj, resolved = ensemble(img, out_dir, tag,
                                                     height_basis=height_basis)
    base["projection_used"] = proj
    base["projection_order"] = list(PROJECTION_ORDER)
    base["per_projection"] = per_proj
    base["projection_resolved"] = resolved
    base["cells_measured_all_projections"] = sum(p["sample_size"] for p in per_proj)

    modal_norm, modal_count, measured_n, measured = _modal(cells)

    if measured_n == 0 or meta.get("ink_pixels", 0) == 0:
        base.update(
            verdict="UNMEASURABLE",
            reason="no_ink_pixels" if meta.get("ink_pixels", 0) == 0 else "no_cells_measured",
            agreement=None, modal_read=None, modal_agreement=None,
            sample_size=0, cells=cells, ink=meta,
            evidence={"cells_dir": out_dir, "cell_pngs": [c["png"] for c in cells if c.get("png")]},
        )
        _write_report(base, out_dir, tag, ephemeral)
        return base

    hits = sum(1 for c in measured if c["read_normalized"] == declared_norm)
    agreement = hits / float(measured_n)

    base.update(
        verdict="PASS" if agreement >= threshold else "FAIL",
        agreement=round(agreement, 4),
        cells_agreeing=hits,
        modal_read=_raw_for(measured, modal_norm),
        modal_normalized=modal_norm,
        modal_agreement=round(modal_count / float(measured_n), 4),
        modal_count=modal_count,
        sample_size=measured_n,
        cells_planned=ENSEMBLE_CELLS,
        distinct_reads=len({c["read_normalized"] for c in measured}),
        no_text_detected=all(c["read_normalized"] == "" for c in measured),
        ink=meta,
        cells=[{k: v for k, v in c.items() if k != "observations"} for c in cells],
        cells_full=cells,
    )
    base["evidence"] = {
        "cells_dir": out_dir,
        "cell_pngs": [c["png"] for c in cells if c.get("png")],
        "source_png": png_path,
        "source_sha256": base["source_sha256"],
    }
    _write_report(base, out_dir, tag, ephemeral)
    return base


def _write_report(d, out_dir, tag, ephemeral):
    try:
        p = os.path.join(out_dir, "%s.readback.json" % tag)
        with open(p, "w") as fh:
            json.dump({k: v for k, v in d.items() if k != "cells_full"}, fh, indent=1)
        d.setdefault("evidence", {})["report_json"] = p
    except OSError:
        pass


# ---------------------------------------------------------------------------
# GATE: readback_pair  (DIFFERENTIAL read-back — candidate vs reference)
# ---------------------------------------------------------------------------
def readback_pair(candidate_png, reference_png, declared_text=None, out_dir=None,
                  threshold=AGREEMENT_THRESHOLD, height_basis="auto"):
    """Compare what the CANDIDATE ink reads against what the REFERENCE ink reads.

    Why this exists: `readback_image` scores against a declared *string*, so it
    inherits Vision's own weaknesses: a reference's OWN ornate-script plate can
    read as a near-miss string in half the cells and score 0.00 against the
    declared string — a false FAIL on approved ink, caused by the reader, not the
    ink.  Running BOTH sides through the identical ensemble cancels that: a
    reader weakness shows up on both sides and the comparison still matches,
    while a real anatomy defect (a delivered word reading "Gorden" where the
    reference reads "Garden") still separates.

    Returns a gate dict; `sample_size` is the number of (height, pad) cells
    measured on BOTH sides.
    """
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="readback-pair-")
    os.makedirs(out_dir, exist_ok=True)

    out = {
        "gate": "readback_pair",
        "kind": "gate",
        "candidate_png": str(candidate_png),
        "reference_png": str(reference_png),
        "declared_text": declared_text,
        "threshold": threshold,
        "protocol": {"heights": list(ENSEMBLE_HEIGHTS), "pad_factors": list(ENSEMBLE_PADS),
                     "cells_planned": ENSEMBLE_CELLS,
                     "confidence_used_as_gate_input": False},
    }
    for label, p in (("candidate", candidate_png), ("reference", reference_png)):
        if not os.path.exists(str(p)):
            out.update(verdict="UNMEASURABLE", reason="%s_png_missing" % label,
                       sample_size=0, evidence={"cells_dir": out_dir})
            return out

    sides = {}
    for label, p in (("candidate", candidate_png), ("reference", reference_png)):
        cells, meta, proj, per_proj, resolved = ensemble(
            Image.open(str(p)), os.path.join(out_dir, label), label,
            height_basis=height_basis)
        modal_norm, modal_count, measured_n, measured = _modal(cells)
        sides[label] = {
            "png": str(p), "projection_used": proj, "per_projection": per_proj,
            "ink_pixels": meta.get("ink_pixels", 0), "sample_size": measured_n,
            "modal_normalized": modal_norm, "modal_read": _raw_for(measured, modal_norm),
            "modal_stability": round(modal_count / float(measured_n), 4) if measured_n else None,
            "cells": {(c["height"], c["pad_factor"]): c["read_normalized"]
                      for c in cells if c.get("skipped") is None},
        }

    common = sorted(set(sides["candidate"]["cells"]) & set(sides["reference"]["cells"]))
    out["sample_size"] = len(common)
    out["cells_planned"] = ENSEMBLE_CELLS
    out["sides"] = {k: {kk: vv for kk, vv in v.items() if kk != "cells"}
                    for k, v in sides.items()}
    out["evidence"] = {"cells_dir": out_dir,
                       "candidate_png": str(candidate_png),
                       "reference_png": str(reference_png)}

    if not common or sides["candidate"]["ink_pixels"] == 0 or sides["reference"]["ink_pixels"] == 0:
        out.update(verdict="UNMEASURABLE",
                   reason=("no_ink_on_one_side"
                           if min(sides["candidate"]["ink_pixels"],
                                  sides["reference"]["ink_pixels"]) == 0
                           else "no_common_cells"),
                   paired_agreement=None, modal_match=None)
        return out

    hits = sum(1 for k in common
               if sides["candidate"]["cells"][k] == sides["reference"]["cells"][k])
    paired = hits / float(len(common))
    modal_match = (sides["candidate"]["modal_normalized"]
                   == sides["reference"]["modal_normalized"]
                   and sides["reference"]["modal_normalized"] != "")

    out["paired_agreement"] = round(paired, 4)
    out["cells_agreeing"] = hits
    out["modal_match"] = modal_match
    out["candidate_modal_read"] = sides["candidate"]["modal_read"]
    out["reference_modal_read"] = sides["reference"]["modal_read"]
    if declared_text is not None:
        dn = normalize_text(declared_text)
        out["declared_normalized"] = dn
        out["reference_matches_declared"] = (sides["reference"]["modal_normalized"] == dn)
        out["candidate_matches_declared"] = (sides["candidate"]["modal_normalized"] == dn)
    out["verdict"] = "PASS" if (modal_match and paired >= threshold) else "FAIL"
    out["reason"] = None if out["verdict"] == "PASS" else (
        "modal_reads_differ" if not modal_match else "paired_cell_agreement_below_threshold")
    return out


# ---------------------------------------------------------------------------
# RECOVERY: read_region
# ---------------------------------------------------------------------------
def extract_frame(video_path, frame_idx, out_png):
    """Decode frame `frame_idx` (ZERO-BASED decoded order) to a PNG.

    Uses `select=eq(n\\,K)` + `-vsync 0` — never an fps shorthand, never a
    timestamp guess.  Returns out_png or None on failure.
    """
    os.makedirs(os.path.dirname(out_png) or ".", exist_ok=True)
    cmd = [
        FFMPEG, "-y", "-v", "error",
        "-i", str(video_path),
        "-vf", "select=eq(n\\,%d)" % int(frame_idx),
        "-vsync", "0", "-frames:v", "1",
        "-start_number", "0",
        str(out_png),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not os.path.exists(out_png) or os.path.getsize(out_png) == 0:
        return None
    return out_png


def read_region(video_path, frame_idx, bbox_xyxy, out_dir, pad_frac=0.20,
                stability_floor=STABILITY_FLOOR, keep_frame=False, tag=None):
    """Recover what a video actually SAYS inside `bbox_xyxy` at `frame_idx`.

    No declared text is supplied — this is how the reference's own word track is
    read back.  The bbox is padded by `pad_frac` (default 20%) on each side, the
    crop is run through the same 12-cell ensemble, and the modal read is returned.

    verdict:
      "RESOLVED"         modal read reaches >= stability_floor of measured cells
      "UNRESOLVED_TEXT"  cells measured but no read reached the floor — NEVER a guess
      "UNMEASURABLE"     frame could not be decoded / crop empty / zero cells

    NOTE: this is a recovery primitive, not a gate; it does not emit PASS/FAIL.
    """
    os.makedirs(out_dir, exist_ok=True)
    x0, y0, x1, y1 = [int(round(v)) for v in bbox_xyxy]
    tag = tag or ("f%06d_%d-%d-%d-%d" % (int(frame_idx), x0, y0, x1, y1))

    out = {
        "gate": "read_region",
        "kind": "recovery",
        "video": str(video_path),
        "frame_idx": int(frame_idx),
        "frame_indexing": "zero-based decoded order (ffmpeg select=eq(n,K))",
        "bbox_xyxy": [x0, y0, x1, y1],
        "pad_frac": pad_frac,
        "protocol": {
            "heights": list(ENSEMBLE_HEIGHTS),
            "pad_factors": list(ENSEMBLE_PADS),
            "cells_planned": ENSEMBLE_CELLS,
            "stability_floor": stability_floor,
            "confidence_used_as_gate_input": False,
        },
    }

    frame_png = os.path.join(out_dir, "frame_%s.png" % tag)
    got = extract_frame(video_path, frame_idx, frame_png)
    if got is None:
        out.update(verdict="UNMEASURABLE", reason="frame_extract_failed",
                   modal_read=None, modal_normalized=None, stability=None,
                   sample_size=0, cells=[], evidence={"cells_dir": out_dir})
        return out

    img = Image.open(frame_png)
    W, H = img.size
    out["frame_wh"] = [W, H]

    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0:
        out.update(verdict="UNMEASURABLE", reason="empty_bbox",
                   modal_read=None, modal_normalized=None, stability=None,
                   sample_size=0, cells=[], evidence={"cells_dir": out_dir,
                                                      "frame_png": frame_png})
        return out
    px, py = int(round(pad_frac * bw)), int(round(pad_frac * bh))
    cx0, cy0 = max(0, x0 - px), max(0, y0 - py)
    cx1, cy1 = min(W, x1 + px), min(H, y1 + py)
    if cx1 - cx0 <= 1 or cy1 - cy0 <= 1:
        out.update(verdict="UNMEASURABLE", reason="crop_out_of_frame",
                   modal_read=None, modal_normalized=None, stability=None,
                   sample_size=0, cells=[], evidence={"cells_dir": out_dir,
                                                      "frame_png": frame_png})
        return out

    out["crop_xyxy"] = [cx0, cy0, cx1, cy1]
    crop_png = os.path.join(out_dir, "crop_%s.png" % tag)
    crop_rgb = img.crop((cx0, cy0, cx1, cy1))
    crop_rgb.save(crop_png)

    cells, meta, proj, per_proj, resolved = ensemble(
        crop_rgb, out_dir, "cell_%s" % tag, height_basis="crop_box",
        stability_floor=stability_floor)  # composite: ink cannot be isolated
    out["projection_used"] = proj
    out["projection_order"] = list(PROJECTION_ORDER)
    out["per_projection"] = per_proj
    out["projection_resolved"] = resolved

    modal_norm, modal_count, measured_n, measured = _modal(cells)
    out["ink"] = meta
    out["cells"] = [{k: v for k, v in c.items() if k != "observations"} for c in cells]
    out["cells_full"] = cells
    out["sample_size"] = measured_n
    out["cells_planned"] = ENSEMBLE_CELLS
    out["evidence"] = {
        "cells_dir": out_dir,
        "frame_png": frame_png,
        "crop_png": crop_png,
        "cell_pngs": [c["png"] for c in cells if c.get("png")],
    }

    if measured_n == 0:
        out.update(verdict="UNMEASURABLE", reason="no_cells_measured",
                   modal_read=None, modal_normalized=None, stability=None)
        return out

    stability = modal_count / float(measured_n)
    out["distinct_reads"] = len({c["read_normalized"] for c in measured})
    out["stability"] = round(stability, 4)
    out["modal_count"] = modal_count
    out["all_reads"] = [c["read"] for c in measured]

    out["modal_is_empty"] = (modal_norm == "")
    if modal_norm == "" or stability < stability_floor:
        out.update(verdict="UNRESOLVED_TEXT",
                   reason=("all_cells_read_empty" if modal_norm == ""
                           else "no_modal_read_reached_floor"),
                   modal_read=None, modal_normalized=None,
                   best_candidate=_raw_for(measured, modal_norm) if modal_norm else None,
                   best_candidate_stability=round(stability, 4))
        if not keep_frame:
            _maybe_rm(frame_png)
        return out

    out.update(verdict="RESOLVED",
               modal_read=_raw_for(measured, modal_norm),
               modal_normalized=modal_norm)
    if not keep_frame:
        _maybe_rm(frame_png)
    return out


def _maybe_rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI (calibration convenience)
# ---------------------------------------------------------------------------
def _main(argv):
    import argparse

    ap = argparse.ArgumentParser(description="caption read-back ensemble gate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("image", help="readback_image on a plate/crop PNG")
    p1.add_argument("png")
    p1.add_argument("declared")
    p1.add_argument("--out-dir", default=None)
    p1.add_argument("--threshold", type=float, default=AGREEMENT_THRESHOLD)

    p2 = sub.add_parser("region", help="read_region on a video frame bbox")
    p2.add_argument("video")
    p2.add_argument("frame", type=int)
    p2.add_argument("bbox", help="x0,y0,x1,y1")
    p2.add_argument("out_dir")

    a = ap.parse_args(argv)
    if a.cmd == "image":
        d = readback_image(a.png, a.declared, out_dir=a.out_dir, threshold=a.threshold)
    else:
        bbox = [int(v) for v in a.bbox.split(",")]
        d = read_region(a.video, a.frame, bbox, a.out_dir)
    d.pop("cells_full", None)
    print(json.dumps(d, indent=1))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
