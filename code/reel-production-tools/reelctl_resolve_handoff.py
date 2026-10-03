#!/usr/bin/env python3
"""Export a reelctl locked project into a Resolve-friendly timeline handoff spec.

This does not mutate Resolve. It creates:
- resolve-handoff/timeline-spec.json: source/slot/marker contract
- resolve-handoff/resolve-import-plan.py: script skeleton to run via Resolve MCP or Resolve Scripts
- resolve-handoff/README.md: editor notes
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe_duration(path: Path) -> float | None:
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)
        ], text=True)
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project", help="Path to reelctl project dir")
    ap.add_argument("--out", default=None, help="Output dir; default <project>/resolve-handoff")
    args = ap.parse_args()

    project = Path(args.project).expanduser().resolve()
    outdir = Path(args.out).expanduser().resolve() if args.out else project / "resolve-handoff"
    outdir.mkdir(parents=True, exist_ok=True)

    project_json = load(project / "project.json")
    reference_lock = load(project / "reference/reference-lock.json")
    blueprint = load(project / "reference/blueprint.json")
    selection = load(project / "edit/selection.locked.json")

    clock = blueprint.get("clock") or reference_lock.get("clock") or {}
    fps = Fraction(str(clock.get("fps", "30/1")))
    fps_float = float(fps)
    width = int(clock.get("width", 1080))
    height = int(clock.get("height", 1920))
    frame_count = int(clock.get("frame_count", sum(s.get("frames", 0) for s in selection.get("slots", []))))

    reference_path = Path(reference_lock["source"]["path"]).expanduser()
    if not reference_path.is_absolute():
        reference_path = (project / reference_path).resolve()

    slots = []
    cursor = 0
    source_files = {}
    for idx, s in enumerate(selection.get("slots", []), 1):
        frames = int(s["frames"])
        source = Path(s["source_path"]).expanduser().resolve()
        source_files[str(source)] = {
            "path": str(source),
            "sha256": s.get("source_sha256"),
            "bytes": s.get("source_bytes"),
            "fps": s.get("source_fps"),
            "exists": source.exists(),
            "duration": ffprobe_duration(source) if source.exists() else None,
        }
        start = cursor
        end = cursor + frames
        slots.append({
            "slot_index": idx,
            "block_id": s.get("block_id", f"p{idx:03d}"),
            "timeline_start_frame": start,
            "timeline_end_frame_exclusive": end,
            "frames": frames,
            "timeline_start_seconds": start / fps_float,
            "timeline_end_seconds": end / fps_float,
            "source_path": str(source),
            "source_start_frame": int(s.get("source_start_frame", 0)),
            "source_end_frame_exclusive": int(s.get("source_start_frame", 0)) + frames,
            "speed": s.get("speed", 1.0),
            "reverse": bool(s.get("reverse", False)),
            "crop_anchor_xy": s.get("crop_anchor_xy", [0.5, 0.5]),
            "hflip": bool((s.get("transform") or {}).get("hflip", False)),
            "reference_role": s.get("reference_role", ""),
            "candidate_observation": s.get("candidate_observation", ""),
            "lighting_family": s.get("lighting_family", ""),
            "input_profile": s.get("input_profile", ""),
            "marker_name": f"{s.get('block_id', f'p{idx:03d}')} — {s.get('reference_role', '')}".strip(),
            "marker_note": s.get("candidate_observation", ""),
        })
        cursor = end

    spec = {
        "schema_version": 1,
        "kind": "reelctl_resolve_handoff",
        "project_id": project.name,
        "project_path": str(project),
        "resolve_project_name": f"{project.name}__resolve_test",
        "timeline_name": f"{project.name}__remake_v001",
        "reference_timeline_name": f"{project.name}__reference",
        "frame_contract": {
            "width": width,
            "height": height,
            "fps": str(fps),
            "fps_float": fps_float,
            "frame_count": frame_count,
            "duration_seconds": frame_count / fps_float,
        },
        "reference": {
            "path": str(reference_path),
            "exists": reference_path.exists(),
            "sha256": reference_lock.get("source", {}).get("sha256") or (sha256_file(reference_path) if reference_path.exists() else None),
            "bytes": reference_lock.get("source", {}).get("bytes"),
        },
        "source_files": list(source_files.values()),
        "slots": slots,
        "markers": [
            {
                "frame": s["timeline_start_frame"],
                "duration_frames": s["frames"],
                "color": "Blue",
                "name": s["marker_name"],
                "note": s["marker_note"],
            }
            for s in slots
        ],
        "policies": project_json.get("policies", {}),
        "handoff_notes": [
            "Resolve is the creative cockpit only; reelctl remains the source of truth.",
            "Do not silently change source windows under the same version.",
            "After Resolve export, run reelctl QC before user review.",
        ],
    }

    spec_path = outdir / "timeline-spec.json"
    spec_path.write_text(json.dumps(spec, indent=2))

    import_plan = f'''# Resolve import plan generated from reelctl.
# Use this as the implementation target for MCP calls or paste/adapt as a Resolve script.
# Spec: {spec_path}

PROJECT_NAME = {spec["resolve_project_name"]!r}
TIMELINE_NAME = {spec["timeline_name"]!r}
REFERENCE_TIMELINE_NAME = {spec["reference_timeline_name"]!r}
WIDTH = {width}
HEIGHT = {height}
FPS = {fps_float!r}
FRAME_COUNT = {frame_count}

# MCP operation sequence:
# 1. project_manager.create_project(name=PROJECT_NAME) or load if exists
# 2. project_settings set timelineResolutionWidth/Height + timelineFrameRate
# 3. media_storage import reference and all source_files
# 4. media_pool create REFERENCE and SOURCES bins
# 5. timeline create TIMELINE_NAME
# 6. append each slot source as a subclip/range at the exact frame duration
# 7. add timeline markers at all slot starts with role + observation notes
# 8. create REFERENCE_TIMELINE_NAME with locked reference for A/B comparison
# 9. render review export only after visual pass
# 10. feed export back to reelctl QC
'''
    (outdir / "resolve-import-plan.py").write_text(import_plan)

    readme = f'''# Resolve handoff — {project.name}

Generated from locked `reelctl` state.

## Contract

- Raster: `{width}x{height}`
- FPS: `{fps}`
- Frames: `{frame_count}`
- Duration: `{frame_count / fps_float:.3f}s`
- Slots: `{len(slots)}`
- Reference exists: `{reference_path.exists()}`

## Files

- `timeline-spec.json` — canonical handoff spec for Resolve/MCP.
- `resolve-import-plan.py` — operation plan/script skeleton.

## Operating rule

Use Resolve only for the creative cockpit: timeline feel, crop, color, text, polish, and tweak loops. `reelctl` stays the law for reference lock, source windows, frame/audio contract, QC, review, and delivery.

## After Resolve export

Run the exported MP4/MOV back through `reelctl` QC/manual review before showing it as a candidate. Do not upload/replace Drive files without explicit approval.
'''
    (outdir / "README.md").write_text(readme)

    print(json.dumps({
        "ok": True,
        "project": str(project),
        "outdir": str(outdir),
        "spec": str(spec_path),
        "slots": len(slots),
        "frames": frame_count,
        "slot_frames_sum": sum(s["frames"] for s in slots),
        "reference_exists": reference_path.exists(),
        "missing_sources": [s["path"] for s in source_files.values() if not s["exists"]],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
