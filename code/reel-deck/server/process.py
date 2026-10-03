"""The PROCESS view — one page that shows a row's entire build, end to end, from
what is on disk: the reference, the study, every cast, every render (and what its gate
said), every look sheet, every receipt, and the registry's own entries for the row.

Read-only. Nothing here writes. Paths are served through /media (confined to the
workbench, the reel root and uploads; png/jpg/mp4/md only)."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import registry as reg
from . import settings

RECEIPT_DIR = settings.BUS_DIR

PIPELINE = [
    ("Reference", "reference-intake/<code>/reference-source.mp4 — the film being remade, probed frame by frame"),
    ("Study (brain/)", "STUDY.md · cutgrid.json (shot in/out) · captions.plaintext.json (every caption state: text, box, ink, entry device) · caption_devices.json (measured blur/opacity/scale/word-reveal curves) · WALLS.md (what the footage cannot give)"),
    ("Cast", "deliver/cast_vNNN.json — per shot: which master, in-point, crop/drift/push, why, residual. Candidates screened by castscan + bedprobe (will THIS window carry THESE captions?)"),
    ("Render", "tools/onetoone/render.py — framing → LC-709 LUT → measured per-shot grade (tone curve + white-balance gain + saturation, guarded) → luma pull to the reference mean → cover → concat → licensed audio track bit-exact"),
    ("Captions", "captions_typeset (plain lines box-fit; script words on the reference's measured ink envelope) → devices.py replays the measured entry devices per part"),
    ("Gate", "readability gate: every state checked against OUR bed at its mid frame; a state whose ink vanishes REFUSES the render (never disabled — the window is re-probed instead)"),
    ("Look", "eye sheets: SHOTS (12 shots ref|ours), CAPTIONS (13 states ref|ours), NATIVE_ORNATE (script words at full size), DEVICES (entry strips)"),
    ("Deliver", "deliver/reelNN-<code>-vNNN.mp4 ≤10MB · registry row → LOCAL_REVIEW_READY__VNNN__PENDING_HUMAN (flock + .bak) · bus receipt · the reviewer's eye is the next gate"),
]

_VER_RE = re.compile(r"v(\d{3})")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _stamp(ts: float) -> str:
    if not ts:
        return ""
    from datetime import datetime
    return datetime.fromtimestamp(ts, settings.local_tz()).strftime("%Y-%m-%d %H:%M")


def _rel(project: Path, path: Path) -> str:
    try:
        return str(path.relative_to(project))
    except ValueError:
        return str(path)


def _sheet(project: Path, path: Path, label: str) -> dict[str, Any] | None:
    if path.is_file():
        return {"label": label, "path": str(path), "rel": _rel(project, path), "mtime": _stamp(_mtime(path))}
    return None


def _study(project: Path) -> dict[str, Any]:
    brain = project / "brain"
    out: dict[str, Any] = {"dir": str(brain), "files": [], "shots": None, "states": None, "devices": None, "walls": None}
    if not brain.is_dir():
        return out
    for p in sorted(brain.iterdir()):
        if p.is_file():
            out["files"].append({"name": p.name, "size": p.stat().st_size, "mtime": _stamp(_mtime(p)),
                                 "media": str(p) if p.suffix.lower() in (".png", ".jpg", ".md") else None})
    cut = _read_json(brain / "cutgrid.json")
    if cut and isinstance(cut.get("shots"), list):
        out["shots"] = [{"slot": s.get("slot"), "in": s.get("in"), "out": s.get("out"),
                         "frames": int(s.get("out", 0)) - int(s.get("in", 0)) + 1,
                         "character": s.get("grade_character") or s.get("character") or ""} for s in cut["shots"]]
    caps = _read_json(brain / "captions.plaintext.json")
    if caps and isinstance(caps.get("states"), list):
        out["states"] = [{"id": st.get("id"), "in": st.get("in"), "out": st.get("out"),
                          "text": " / ".join(str(p.get("text") or "") for p in st.get("parts", [])),
                          "parts": [{"text": p.get("text"), "part": p.get("part"), "box": p.get("bbox_settled") or p.get("bbox_settled_approx"),
                                     "ink": p.get("ink_rgb") or p.get("ink_rgb_approx"), "env": p.get("ref_ink_env")}
                                    for p in st.get("parts", [])]} for st in caps["states"]]
    dev = _read_json(brain / "caption_devices.json")
    if isinstance(dev, dict):
        parts = dev.get("parts") or dev.get("states") or dev
        out["devices"] = {"measured": len(parts) if hasattr(parts, "__len__") else None}
    walls = brain / "WALLS.md"
    if walls.is_file():
        out["walls"] = walls.read_text()
    return out


def _casts(project: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted((project / "deliver").glob("cast_v*.json")):
        data = _read_json(p)
        if not isinstance(data, dict):
            continue
        m = _VER_RE.search(p.name)
        out.append({
            "name": p.name, "version": data.get("version") or (m.group(0) if m else p.stem),
            "mtime": _stamp(_mtime(p)), "ts": _mtime(p),
            "change": data.get("change") or data.get("cure") or data.get("scope") or "",
            "derived_from": data.get("derived_from"),
            "partial": "slots" in data and isinstance(data["slots"], dict),
            "slots": [{"slot": s.get("slot"), "stem": s.get("stem"), "in_s": s.get("in_s"), "crop": s.get("crop"),
                       "owner": s.get("owner"), "why": s.get("why"), "residual": s.get("residual")}
                      for s in (data["slots"] if isinstance(data.get("slots"), list) else
                                [dict(v, slot=k) for k, v in (data.get("slots") or {}).items() if isinstance(v, dict)])],
        })
    return out


def _renders(project: Path) -> list[dict[str, Any]]:
    out = []
    for d in sorted(project.glob("render-v*")):
        if not d.is_dir():
            continue
        m = _VER_RE.search(d.name)
        version = m.group(0) if m else d.name
        report = _read_json(d / "render_report.json")
        log = d / "render.log"
        refusal = None
        if log.is_file():
            text = log.read_text(errors="replace")
            hit = re.search(r"UnreadableCaption: (.*)", text)
            if hit:
                refusal = hit.group(1).strip()
            elif "Traceback" in text and not report:
                refusal = text.strip().splitlines()[-1][:400]
        shots = []
        if report:
            for s in (report.get("picture") or {}).get("shots") or []:
                g = s.get("grade") or {}
                lp = g.get("luma_pull") or {}
                lm = s.get("luma_mid") or {}
                shots.append({"slot": s.get("slot"), "stem": s.get("stem"), "frames": s.get("frames"),
                              "in_s": s.get("master_in_s"), "crop": bool(s.get("crop")),
                              "grade_mode": g.get("mode"), "tone": g.get("strength"), "pull": lp.get("gamma"),
                              "luma_ours": lm.get("ours"), "luma_ref": lm.get("ref"),
                              "mood_eq": s.get("mood_eq")})
        sheets = [x for x in (
            _sheet(project, d / "look" / "SHOTS.png", "SHOTS — 12 shots, reference | ours"),
            _sheet(project, d / "look" / "CAPTIONS.png", "CAPTIONS — 13 states, reference | ours"),
            _sheet(project, d / "look" / "NATIVE_ORNATE.png", "NATIVE ORNATE — script words at full size"),
            _sheet(project, d / "look" / "DEVICES.png", "DEVICES — caption entry strips"),
            _sheet(project, d / "eye" / "EYE_SHEET.png", "EYE SHEET (auto, every render)"),
            _sheet(project, d / "grade" / "SHOTS_v007.png", "GRADE — per-shot reference | ours"),
            _sheet(project, d / "beds" / "FAILS.png", "GATE — the beds that refused"),
        ) if x]
        deliverables = sorted(str(p) for p in (project / "deliver").glob(f"*{version}*.mp4"))
        out.append({
            "dir": str(d), "name": d.name, "version": version, "ts": _mtime(d), "mtime": _stamp(_mtime(d)),
            "ok": bool(report), "refusal": refusal,
            "bytes": report.get("bytes") if report else None, "frames": report.get("frames") if report else None,
            "crf": report.get("crf") if report else None,
            "devices": ((report.get("captions") or {}).get("devices") if report else None),
            "unreadable": ((report.get("captions") or {}).get("unreadable") if report else None),
            "shots": shots, "sheets": sheets, "deliverables": deliverables,
            "log": str(log) if log.is_file() else None,
        })
    return out


def _receipts(seq: int, project: Path) -> list[dict[str, Any]]:
    out = []
    if not RECEIPT_DIR.is_dir():
        return out
    # exact row only: a bare prefix "row4"/"reel4" would also match rows 40-49
    pat = re.compile(rf"(?:row|reel)0*{seq}(?!\d)")
    for p in sorted(RECEIPT_DIR.iterdir(), key=_mtime):
        if p.suffix != ".md" or not pat.search(p.name):
            continue
        try:
            head = p.read_text(errors="replace").strip().splitlines()
        except OSError:
            head = []
        acks = [ln for ln in head if ln.startswith(("ACK", "CALL"))]
        out.append({"name": p.name, "path": str(p), "ts": _mtime(p), "mtime": _stamp(_mtime(p)),
                    "title": head[0].lstrip("# ").strip() if head else p.name,
                    "test": "smoketest" in p.name, "acks": acks[-3:]})
    # plus the project's own md receipts
    for p in sorted(project.glob("*.md")):
        out.append({"name": p.name, "path": str(p), "ts": _mtime(p), "mtime": _stamp(_mtime(p)),
                    "title": ((p.read_text(errors="replace").strip().splitlines() or [p.name])[0].lstrip("# ")) if p.stat().st_size else p.name,
                    "test": False, "acks": [], "project": True})
    return out


def _registry_entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key, value in row.items():
        if not isinstance(value, dict):
            continue
        if not re.search(r"(intake|build|audit|delivery|looksheet|fix|render|verdict|review)", key):
            continue
        summary = {k: v for k, v in value.items() if not isinstance(v, (dict, list))}
        nested = {k: v for k, v in value.items() if isinstance(v, (dict, list))}
        out.append({"key": key, "summary": summary, "nested": json.dumps(nested, indent=1, ensure_ascii=False) if nested else None,
                    "ts": value.get("delivered_at_utc") or value.get("date_utc") or value.get("utc") or ""})
    out.sort(key=lambda e: e["ts"] or "")
    return out


def build_process(seq: int) -> dict[str, Any] | None:
    payload = reg.load_registry()
    rows = payload.get("rows") or []
    row = next((r for r in rows if reg.row_sequence(r) == seq), None)
    if row is None:
        return None
    project_dir = reg.row_project_dir(row)
    project = Path(project_dir) if project_dir else None
    reference = reg.row_reference(row)
    out: dict[str, Any] = {
        "seq": seq, "shortcode": row.get("reference_shortcode"), "title": row.get("creative_hook") or "",
        "review_state": row.get("review_state"), "version": row.get("version"), "status_bucket": row.get("status_bucket"),
        "updated": row.get("updated_at_utc"), "project": project_dir, "reference": reference,
        "pipeline": PIPELINE, "study": None, "casts": [], "renders": [], "receipts": [], "registry": _registry_entries(row),
        "timeline": [],
    }
    if project and project.is_dir():
        out["study"] = _study(project)
        out["casts"] = _casts(project)
        out["renders"] = _renders(project)
        out["receipts"] = _receipts(seq, project)
        # everything in time order, one ribbon
        tl: list[dict[str, Any]] = []
        for c in out["casts"]:
            tl.append({"ts": c["ts"], "when": c["mtime"], "kind": "cast", "label": c["name"], "note": (c["change"] or "")[:220]})
        for r in out["renders"]:
            tl.append({"ts": r["ts"], "when": r["mtime"], "kind": "render", "label": r["name"],
                       "note": ("REFUSED by the readability gate: " + r["refusal"]) if r["refusal"] else
                               (f"clean — {r['frames']} frames, {(r['bytes'] or 0)/1e6:.1f} MB, crf {r['crf']}" if r["ok"] else "no report")})
        for rc in out["receipts"]:
            if not rc["test"]:
                tl.append({"ts": rc["ts"], "when": rc["mtime"], "kind": "receipt", "label": rc["name"], "note": rc["title"][:220]})
        tl.sort(key=lambda e: e["ts"])
        out["timeline"] = tl
    return out
