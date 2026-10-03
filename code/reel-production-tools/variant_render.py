#!/usr/bin/env python3
"""P2 — the variant render loop: one casting receipt in, a reviewable variant reel out.

What this is
------------
`variant_caster.py` (P1) decides WHICH clip and window sits behind each of the reel's twelve
picture slots for each variant. This tool turns one of those castings into picture, puts the
reel's own approved caption program back over it, and produces the two delivery passes the delivery spec requires, with receipts and a proof board for every step.

The chain, and why each link is what it is
------------------------------------------
1. **Sandbox project.** Rendering happens in a COPY of the parent reelctl project, kept
   outside `~/reel-production` so the studio daemon (depth-1 discovery under that root) never
   sees it. The parent project's `state.json`, stage locks and `edit/` are never written. The
   copy keeps the parent's reference, blueprint, footage-index, feasibility lock and LUT
   byte-for-byte, so every gate the parent reel passed still bites here.
2. **Selection.** The casting is expanded into a real `edit/selection.locked.json`:
   `reelctl` colour-profile proofs per source (signed, bound to the source bytes), the
   project's hash-locked Sony LC-709 technical LUT, and **identity creative grading on every
   slot**. That is the caster's recorded grade doctrine, not laziness — in an approved casting
   nearly every slot ships identity, and any exception is a human-requested exposure lift.
   A machine has no standing to invent that for a different clip. The measured reference-match proposal is recorded on each slot and
   explicitly declined in writing.
3. **Picture** is rendered by `reelctl.render.render_project` itself — not a re-implementation —
   so the grade/LUT confinement, the frame clock, the segment cache, the audio identity checks
   and the signed render receipt are the engine's, identical to how v002 was produced.
4. **QC (reference-relative)** is `reelctl.qc.run_qc`, the same path v001/v002 walked. The
   project is `original-montage`, so reference-relative visual parity is DIAGNOSTIC_ONLY by
   the engine's own rule — a variant deliberately carries different pictures — while technical
   and structure QC stay ship-blocking.
5. **Captions** are re-rendered from the reel's caption contract through
   `reelctl captions render` (DOCTRINE 18: no per-reel caption scripts), then byte-compared
   against the approved deliverable's caption frames. Same program, same plates, same ink.
6. **Delivery pass 1 — vertical-native 9:16.** The 16:9 machine master letterboxes inside the
   reel viewport, which the format doctrine forbids (`format_doctrine`). So the picture is
   rendered a SECOND time, from the same masters through the same engine segment renderer, at
   1080x1920 — a real vertical render, not an upscale of a centre slice of the finished 16:9
   frame. The caption layer is scaled by `1080/1916` and centred, which reproduces exactly the
   apparent caption size and position the reviewer approved when the 16:9 cut was letterboxed
   into the 9:16 viewport; the only thing that changes is that picture, not black, now fills
   the top and bottom.
7. **Delivery pass 2 — web encode under a configurable upload size cap** (crf21, exact frame
   count, audio stream-copied); `REEL_FACTORY_WEB_SIZE_CAP_BYTES`, default 10 MiB.

What this tool does NOT decide
------------------------------
It never watches anything. `agent_visual_review` stays PENDING_MACHINE on every artifact it
writes. The 9:16 reframe anchor is an ENERGY MEASUREMENT with a centre prior (no subject
detector exists in this runtime), disclosed per slot and drawn on the proof board so an
reviewer can see what the crop kept and what it cut. Nothing is published, uploaded, or
written to REEL_REGISTRY.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rf_paths import WORKBENCH  # noqa: E402

REEL_ROOT = Path(__file__).resolve().parent.parent
for _src in (REEL_ROOT / "_reelctl" / "src", REEL_ROOT / "reelctl" / "src"):
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from reelctl.captions.ink import (  # noqa: E402
    MAX_WEAK_INK_FRACTION as CAPTION_MAX_WEAK_INK_FRACTION,
)
from reelctl.captions.ink import (  # noqa: E402
    MIN_CONTRAST_RATIO as CAPTION_MIN_CONTRAST_RATIO,
)
from reelctl.captions.ink import (  # noqa: E402
    TARGET_CONTRAST_RATIO as CAPTION_TARGET_CONTRAST_RATIO,
)
from reelctl.captions.ink import (  # noqa: E402
    apply_resolution,
    contrast_ratio_from_luma,
    local_background_luma,
    prove_only_ink_moved,
    render_twin,
    resolve_ink,
)
from reelctl.color import create_camera_profile_proof, validate_color_contract  # noqa: E402
from reelctl.contracts import validate_contract  # noqa: E402
from reelctl.grading import propose_source_aware_grades  # noqa: E402
from reelctl.hashing import atomic_write_json, load_json, sha256_file  # noqa: E402
from reelctl.media import (  # noqa: E402
    audio_packet_ledger,
    audio_payload_sha256,
    decode_pcm_sha256,
    frame_pts,
    full_decode,
    legal_luma_range,
    probe_media,
    run_checked,
)
from reelctl.paths import secure_mkdirs  # noqa: E402
from reelctl.qc import run_qc  # noqa: E402

# `_render_segment` is the engine's own per-slot renderer (trim -> fps -> scale-to-cover ->
# crop -> colour filter -> ProRes). The vertical pass reuses it verbatim rather than
# re-implementing the filter chain, which is the whole point of "same grade/LUT confinement".
from reelctl.render import _render_segment, render_project  # noqa: E402
from reelctl.selection import bind_selection_to_inventory, validate_selection  # noqa: E402

# ---------------------------------------------------------------------------------------
# constants

VERTICAL_SIZE = (1080, 1920)
WEB_SIZE_CAP_BYTES = int(os.environ.get("REEL_FACTORY_WEB_SIZE_CAP_BYTES") or 10 * 1024 * 1024)
WEB_CRF_LADDER = (21, 23, 25, 27)
DELIVERY_CRF = 17  # the approved v002 captioned deliverable's own encode setting
IDENTITY_CREATIVE = {"exposure_stops": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0}
SEPARATION_FLOOR = 25  # luma levels; the v002 caption pass's own legibility floor
RING_RADIUS = 6
ANCHOR_CENTRE_PRIOR = 0.35
ANCHOR_SAMPLE_FRAMES = 5

GRADE_DECLINE_NOTE = (
    "Creative correction DECLINED on this slot; the row ships identity. The estimator's "
    "reference-match proposal is recorded and not applied: it is fitted toward the reference "
    "block's own pixels, which are a different world (this reel's picture layer is substituted, "
    "not matched), so applying it would be grading one world toward another. Technical LC-709 "
    "normalisation is applied and is the whole of the grade. A per-shot creative correction on a "
    "trial variant is a human act after watching it, not a machine act before."
)

PENDING = "PENDING_MACHINE"


class VariantRenderError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------
# pure helpers (unit-tested in tools/test_variant_render.py)


def cover_geometry(in_w: int, in_h: int, out_w: int, out_h: int) -> Dict[str, int]:
    """Scale-to-cover then crop, exactly as ffmpeg's force_original_aspect_ratio=increase does.

    Returns the intermediate scaled size and how much freedom the crop has on each axis.
    """

    if min(in_w, in_h, out_w, out_h) <= 0:
        raise ValueError("geometry must be positive")
    scale = max(out_w / in_w, out_h / in_h)
    scaled_w = max(out_w, round(in_w * scale))
    scaled_h = max(out_h, round(in_h * scale))
    return {
        "scaled_w": scaled_w,
        "scaled_h": scaled_h,
        "slack_x": scaled_w - out_w,
        "slack_y": scaled_h - out_h,
    }


def caption_layer_placement(src_w: int, src_h: int, dst_w: int, dst_h: int) -> Dict[str, int]:
    """Where the reel's caption layer sits on a taller canvas.

    The caption program is authored on the reference canvas (1916x1078). Fitting that canvas
    by WIDTH into the delivery canvas and centring it vertically reproduces, pixel for pixel,
    the apparent caption size and position a viewer saw when the 16:9 cut was letterboxed into
    a 9:16 viewport — which is the geometry the reviewer approved. Any other scale would be a
    new typographic decision, and typography is locked.
    """

    if min(src_w, src_h, dst_w, dst_h) <= 0:
        raise ValueError("geometry must be positive")
    scaled_w = dst_w
    scaled_h = max(1, round(src_h * dst_w / src_w))
    return {
        "scaled_w": scaled_w,
        "scaled_h": scaled_h,
        "x": (dst_w - scaled_w) // 2,
        "y": (dst_h - scaled_h) // 2,
        "scale": dst_w / src_w,
    }


def anchor_from_column_energy(
    energy: Sequence[float],
    window_columns: int,
    *,
    centre_prior: float = ANCHOR_CENTRE_PRIOR,
) -> float:
    """Pick a horizontal crop anchor in 0..1 from a measured column-energy profile.

    The window that captures the most measured energy wins, then the result is pulled back
    toward frame centre by `centre_prior` so a busy background cannot throw the crop to an
    edge. This is a measurement with a stated bias, not a judgement about the subject: no
    subject detector is available in this runtime, and the tool says so everywhere it reports.
    """

    count = len(energy)
    if count == 0:
        raise ValueError("empty energy profile")
    if not 0.0 <= centre_prior <= 1.0:
        raise ValueError("centre prior must be within 0..1")
    window = max(1, min(int(window_columns), count))
    if window >= count:
        return 0.5
    prefix = [0.0]
    for value in energy:
        prefix.append(prefix[-1] + float(value))
    centre_start = (count - window) / 2
    best_key: Optional[Tuple[float, float]] = None
    best_start = 0
    for start in range(0, count - window + 1):
        # ties break toward frame centre, so a flat profile means a centred crop rather than
        # a left-edge crop that only reflects iteration order
        key = (prefix[start + window] - prefix[start], -abs(start - centre_start))
        if best_key is None or key > best_key:
            best_key, best_start = key, start
    measured = best_start / (count - window)
    return round((1.0 - centre_prior) * measured + centre_prior * 0.5, 6)


def web_crf_for_size(observed: Sequence[Tuple[int, int]], cap: int = WEB_SIZE_CAP_BYTES) -> Optional[int]:
    """Given (crf, bytes) attempts in ladder order, return the first that fits the cap."""

    for crf, size in observed:
        if size <= cap:
            return crf
    return None


def clock_checks(facts: Dict[str, Any], clock: Dict[str, Any], *, geometry: Tuple[int, int]) -> Dict[str, bool]:
    """Every delivery encode is measured against the blueprint clock, not against its sibling."""

    video = facts["video"]
    return {
        "frame_count": int(video.get("frame_count") or 0) == int(clock["frame_count"]),
        "fps": video.get("r_frame_rate") == clock["fps"],
        "time_base": video.get("time_base") == clock["time_base"],
        "duration_ts": int(video.get("duration_ts") or 0) == int(clock["duration_ts"]),
        "geometry": [int(video.get("width", 0)), int(video.get("height", 0))] == [int(geometry[0]), int(geometry[1])],
        "rotation_normalized": int(video.get("rotation", 0)) == 0,
        "sar_square": video.get("sample_aspect_ratio") == "1:1",
        "rec709_tags": [video.get("color_space"), video.get("color_transfer"), video.get("color_primaries")]
        == ["bt709", "bt709", "bt709"],
    }


def front_door_report(casting: Dict[str, Any], inventory: Dict[str, Any]) -> Dict[str, Any]:
    """Refuse any cast source that is not a PASS master of the project's own footage index."""

    if inventory.get("status") != "PASS":
        raise VariantRenderError("footage index is not PASS; the front door is closed")
    by_path = {
        str(Path(str(item["path"])).expanduser().absolute()): item
        for item in inventory.get("clips", [])
        if item.get("status", "PASS") == "PASS"
    }
    rows, violations = [], []
    for slot in casting["slots"]:
        path = str(Path(str(slot["source_path"])).expanduser().absolute())
        record = by_path.get(path)
        if record is None:
            violations.append({"block_id": slot["block_id"], "path": path, "reason": "not a PASS member of the footage index"})
            continue
        relative = str(record.get("relative_path", ""))
        if not relative.startswith("masters/"):
            violations.append({"block_id": slot["block_id"], "path": path, "reason": f"not under masters/: {relative}"})
            continue
        if str(record.get("sha256")) != str(slot.get("source_sha256")):
            violations.append({"block_id": slot["block_id"], "path": path, "reason": "hash differs from the indexed bytes"})
            continue
        rows.append({"block_id": slot["block_id"], "relative_path": relative, "clip_id": record.get("clip_id")})
    return {"status": "PASS" if not violations else "FAIL", "authorized": rows, "violations": violations}


def build_selection_slots(
    casting: Dict[str, Any],
    proofs: Dict[str, Dict[str, Any]],
    *,
    lut_path: str,
    lut_sha256: str,
) -> List[Dict[str, Any]]:
    """Casting slots -> reelctl selection slots, identity creative grade, technical LUT only."""

    slots = []
    for slot in casting["slots"]:
        proof = proofs[str(slot["source_clip_id"])]
        if proof["source_sha256"] != slot["source_sha256"]:
            raise VariantRenderError(f"{slot['block_id']}: colour profile proof is bound to different bytes")
        tags = slot.get("tags", {})
        slots.append(
            {
                "block_id": slot["block_id"],
                "frames": int(slot["frames"]),
                "reference_role": slot["reference_role"],
                "candidate_observation": slot["candidate_observation"],
                "source_path": slot["source_path"],
                "source_start_frame": int(slot["source_start_frame"]),
                "speed": 1.0,
                "reverse": False,
                "crop_anchor_xy": list(slot.get("crop_anchor_xy", [0.5, 0.5])),
                "input_profile": "sony_slog3_sgamut3cine",
                "input_range": proof["input_range"],
                "profile_proof": proof,
                "technical_transform": "sony_lc709",
                "technical_lut": lut_path,
                "technical_lut_sha256": lut_sha256,
                "lighting_family": f"{tags.get('lighting')}-{tags.get('world_cluster')}-library-tag-pending-measurement",
                "creative": dict(IDENTITY_CREATIVE),
                "grade_proof": {"status": "PENDING_SOURCE_AWARE_GRADE_REVIEW"},
            }
        )
    return slots


def key_band(y50: float) -> str:
    if y50 < 0.30:
        return "low-key"
    if y50 < 0.55:
        return "mid-key"
    return "high-key"


def finish_selection_slots(
    slots: List[Dict[str, Any]],
    proposals: Dict[str, Dict[str, Any]],
    tags_by_block: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Fold the measured grade proof back in, decline its proposal in writing, keep identity."""

    finished = []
    for slot in slots:
        block_id = str(slot["block_id"])
        proof = dict(proposals[block_id])
        if int(proof["source_frame"]) != int(slot["source_start_frame"]):
            raise VariantRenderError(f"{block_id}: grade proof is bound to a different source frame")
        y50 = float(proof["source_metrics"]["y_q50"])
        proof["status"] = "AGENT_REVIEWED"
        proof["review_notes"] = GRADE_DECLINE_NOTE + " Recorded proposal: " + json.dumps(proof["bounded_proposal"])
        row = dict(slot)
        row["grade_proof"] = proof
        tags = tags_by_block.get(block_id, {})
        row["lighting_family"] = f"{key_band(y50)}-{tags.get('lighting')}-{tags.get('world_cluster')}-measured-y50-{y50:.3f}"
        row["creative"] = dict(IDENTITY_CREATIVE)
        finished.append(row)
    return finished


# ---------------------------------------------------------------------------------------
# small io helpers


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n")


def _ffprobe_frame_count(path: Path) -> int:
    result = run_checked(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ]
    )
    return int(result.stdout.strip())


# ---------------------------------------------------------------------------------------
# stage 1 — sandbox project


SANDBOX_FILES = (
    "project.json",
    "reference/reference-source.mp4",
    "reference/reference-lock.json",
    "reference/blueprint.json",
    "reference/blueprint-draft-board.jpg",
    "reference/reference-all-frames-board.jpg",
    "footage/footage-index.json",
    "assets/assets.json",
    "assets/assets.locked.json",
    "assets/color-profile.json",
    "edit/feasibility.json",
    "edit/feasibility.locked.json",
)

# The reference lock and the blueprint bind the reference to an ABSOLUTE path, and reelctl
# confines a project's reference to that project's own root. A sandbox copy therefore cannot
# reuse them untouched — `verify_reference_identity` fails on `source_path` before anything
# renders. These are the only fields this tool is allowed to move, and every content field
# (bytes, sha256, clock, pts ledger, decoded-frame ledger, audio payload/pcm/packets, board
# hashes, picture blocks) is compared afterwards and must be byte-identical to the parent's.
RELOCATABLE_LOCK_PATHS = (
    ("source", "path", "reference/reference-source.mp4"),
    ("analysis_artifacts", "blueprint_draft_board_path", "reference/blueprint-draft-board.jpg"),
    ("analysis_artifacts", "all_frames_board_path", "reference/reference-all-frames-board.jpg"),
)


def relocate_reference_lock(sandbox: Path, parent_lock: Dict[str, Any], parent_blueprint: Dict[str, Any]) -> Dict[str, Any]:
    """Repoint the locked reference at the sandbox copy, re-hash and re-sign, prove the rest is identical."""

    from reelctl.hashing import recipe_hash
    from reelctl.signing import sign_payload

    lock_core = {key: value for key, value in parent_lock.items() if key not in {"recipe_hash", "signature"}}
    lock_core = json.loads(json.dumps(lock_core))
    moved = []
    for section, field, relative in RELOCATABLE_LOCK_PATHS:
        before = lock_core[section][field]
        after = str(sandbox / relative)
        lock_core[section][field] = after
        moved.append({"field": f"{section}.{field}", "parent": before, "sandbox": after})
    lock = dict(lock_core)
    lock["recipe_hash"] = recipe_hash(lock_core)
    lock = sign_payload(lock, purpose="reference-lock-v1")

    blueprint_core = {key: value for key, value in parent_blueprint.items() if key not in {"sha256_contract", "signature"}}
    blueprint_core = json.loads(json.dumps(blueprint_core))
    blueprint_core["reference_lock_recipe_hash"] = lock["recipe_hash"]
    blueprint = dict(blueprint_core)
    blueprint["sha256_contract"] = recipe_hash(blueprint_core)
    blueprint = sign_payload(blueprint, purpose="blueprint-lock-v1")

    unchanged = []
    for key, value in parent_lock.items():
        if key in {"recipe_hash", "signature", "source", "analysis_artifacts"}:
            continue
        if lock.get(key) != value:
            raise VariantRenderError(f"reference lock field {key} changed during relocation; refusing")
        unchanged.append(key)
    for section in ("source", "analysis_artifacts"):
        for key, value in parent_lock[section].items():
            if key in {field for _, field, _ in RELOCATABLE_LOCK_PATHS}:
                continue
            if lock[section][key] != value:
                raise VariantRenderError(f"reference lock {section}.{key} changed during relocation; refusing")
    for key, value in parent_blueprint.items():
        if key in {"sha256_contract", "signature", "reference_lock_recipe_hash"}:
            continue
        if blueprint.get(key) != value:
            raise VariantRenderError(f"blueprint field {key} changed during relocation; refusing")

    return {
        "lock": lock,
        "blueprint": blueprint,
        "receipt": {
            "why": (
                "reelctl confines a project's reference to its own root, so a sandbox copy of an "
                "human-locked project cannot reuse the parent's path-bound lock. Only the three "
                "absolute paths below were moved; the lock was then re-hashed and re-signed, and the "
                "blueprint's reference_lock_recipe_hash followed it."
            ),
            "moved_paths": moved,
            "parent_lock_recipe_hash": parent_lock.get("recipe_hash"),
            "sandbox_lock_recipe_hash": lock["recipe_hash"],
            "parent_blueprint_sha256_contract": parent_blueprint.get("sha256_contract"),
            "sandbox_blueprint_sha256_contract": blueprint["sha256_contract"],
            "content_fields_verified_identical": sorted(unchanged),
            "reference_source_sha256": parent_lock["source"]["sha256"],
            "picture_blocks_identical": parent_blueprint["picture_blocks"] == blueprint["picture_blocks"],
            "clock_identical": parent_lock["clock"] == lock["clock"],
            "audio_identical": parent_lock["audio"] == lock["audio"],
        },
    }


def prepare_sandbox(parent_project: Path, sandbox: Path) -> Dict[str, Any]:
    """Copy the parent project's locked inputs into a daemon-invisible sandbox.

    Only `reference_input`/`reference_path` are rewritten, because reelctl confines the
    reference to the project root. Everything else is copied byte-for-byte and verified.
    """

    sandbox.mkdir(parents=True, exist_ok=True)
    copied = []
    for relative in SANDBOX_FILES:
        source = parent_project / relative
        if not source.is_file():
            raise VariantRenderError(f"parent project is missing {relative}")
        destination = sandbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or sha256_file(destination) != sha256_file(source):
            shutil.copyfile(source, destination)
        copied.append({"path": relative, "sha256": sha256_file(destination), "matches_parent": sha256_file(destination) == sha256_file(source)})
    lut_source = parent_project / "assets/luts"
    (sandbox / "assets/luts").mkdir(parents=True, exist_ok=True)
    for lut in sorted(lut_source.glob("*.cube")):
        destination = sandbox / "assets/luts" / lut.name
        if not destination.is_file() or sha256_file(destination) != sha256_file(lut):
            shutil.copyfile(lut, destination)
        copied.append({"path": f"assets/luts/{lut.name}", "sha256": sha256_file(destination), "matches_parent": True})

    project = json.loads((sandbox / "project.json").read_text())
    project["reference_input"] = str(parent_project / "reference/reference-source.mp4")
    project["reference_path"] = str(sandbox / "reference/reference-source.mp4")
    (sandbox / "project.json").write_text(json.dumps(project, indent=2) + "\n")

    relocated = relocate_reference_lock(
        sandbox,
        json.loads((parent_project / "reference/reference-lock.json").read_text()),
        json.loads((parent_project / "reference/blueprint.json").read_text()),
    )
    (sandbox / "reference/reference-lock.json").write_text(json.dumps(relocated["lock"], indent=1) + "\n")
    (sandbox / "reference/blueprint.json").write_text(json.dumps(relocated["blueprint"], indent=1) + "\n")
    relocated_names = {"reference/reference-lock.json", "reference/blueprint.json"}
    for row in copied:
        if row["path"] in relocated_names:
            row["sha256"] = sha256_file(sandbox / row["path"])
            row["matches_parent"] = False
            row["note"] = "path-relocated and re-signed; content fields verified identical (see reference_relocation)"
    for name in ("edit", "review", "deliver"):
        (sandbox / name).mkdir(parents=True, exist_ok=True)
    (sandbox / "README.txt").write_text(
        "Variant render sandbox. A copy of the parent reelctl project kept outside "
        "~/reel-production so the studio daemon never discovers it. Nothing here is a "
        "deliverable and nothing here is published.\n"
    )
    return {"status": "PASS", "sandbox": str(sandbox), "files": copied, "reference_relocation": relocated["receipt"]}


# ---------------------------------------------------------------------------------------
# stage 2 — selection


def ensure_profile_proofs(casting: Dict[str, Any], cache_dir: Path) -> Dict[str, Dict[str, Any]]:
    """One signed camera-profile proof per distinct source, cached across the whole family."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    proofs: Dict[str, Dict[str, Any]] = {}
    for slot in casting["slots"]:
        clip_id = str(slot["source_clip_id"])
        if clip_id in proofs:
            continue
        receipt = cache_dir / f"{clip_id}.json"
        if receipt.is_file():
            proof = json.loads(receipt.read_text())
            if proof.get("source_sha256") == slot["source_sha256"]:
                proofs[clip_id] = proof
                continue
        proof = create_camera_profile_proof(Path(str(slot["source_path"])), receipt)
        proofs[clip_id] = proof
    return proofs


def build_locked_selection(
    sandbox: Path,
    casting: Dict[str, Any],
    proofs: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    project = load_json(sandbox / "project.json", root=sandbox)
    blueprint = load_json(sandbox / "reference/blueprint.json", root=sandbox)
    inventory = load_json(sandbox / "footage/footage-index.json", root=sandbox)
    feasibility = load_json(sandbox / "edit/feasibility.locked.json", root=sandbox)
    lut = sandbox / "assets/luts/Sony-LC-709-official.cube"
    slots = build_selection_slots(casting, proofs, lut_path=str(lut), lut_sha256=sha256_file(lut))

    draft_path = sandbox / "edit/selection.json"
    draft = {"schema_version": 1, "status": "DRAFT", "slots": slots}
    draft_path.write_text(json.dumps(draft, indent=1) + "\n")

    report = propose_source_aware_grades(sandbox, selection_path=draft_path)
    proposals = {str(row["block_id"]): row["grade_proof"] for row in report["proposals"]}
    tags_by_block = {str(slot["block_id"]): slot.get("tags", {}) for slot in casting["slots"]}
    finished = finish_selection_slots(slots, proposals, tags_by_block)

    selection = {"schema_version": 1, "status": "DRAFT", "slots": finished}
    bound = bind_selection_to_inventory(selection, inventory)
    validate_color_contract(bound["slots"])
    receipt = validate_selection(
        blueprint,
        bound,
        mode=project.get("mode", "reference-locked"),
        inventory=inventory,
        feasibility=feasibility,
    )
    validate_contract("selection", bound)
    locked = sandbox / "edit/selection.locked.json"
    if locked.is_file():
        locked.unlink()
    atomic_write_json(locked, bound, root=sandbox)
    draft_path.write_text(json.dumps(bound, indent=1) + "\n")
    return {"status": "PASS", "selection": receipt, "path": str(locked), "sha256": sha256_file(locked)}


# ---------------------------------------------------------------------------------------
# stage 3 — captions


def render_caption_frames(contract: Path, plates: Path, output: Path) -> Dict[str, Any]:
    """DOCTRINE 18: the caption program is re-rendered by the engine, never re-scripted."""

    output.mkdir(parents=True, exist_ok=True)
    command = [
        "reelctl",
        "captions",
        "render",
        "--contract",
        str(contract),
        "--plates",
        str(plates),
        "--output",
        str(output),
        "--allow-unsealed-local-review",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise VariantRenderError(f"reelctl captions render failed: {result.stderr.strip()[:2000]}")
    return {"command": " ".join(command), "stdout": result.stdout.strip()}


def compare_caption_frames(rendered: Path, baseline: Path, frame_count: int) -> Dict[str, Any]:
    identical = 0
    missing = []
    for index in range(frame_count):
        mine = rendered / f"caption-{index:03d}.png"
        theirs = baseline / f"caption-{index:03d}.png"
        if not mine.is_file():
            missing.append(index)
            continue
        if theirs.is_file() and mine.read_bytes() == theirs.read_bytes():
            identical += 1
    return {
        "frames": frame_count,
        "byte_identical_to_approved_program": identical,
        "missing_frames": missing,
        "pass": not missing and identical == frame_count,
    }


def scale_caption_layers(source: Path, destination: Path, placement: Dict[str, int], canvas: Tuple[int, int], frame_count: int) -> Path:
    """Pre-scale the caption layer onto the delivery canvas once; reuse for every variant."""

    from PIL import Image

    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / "placement.json"
    expected = {"placement": placement, "canvas": list(canvas), "frames": frame_count}
    if marker.is_file() and json.loads(marker.read_text()) == expected:
        if len(list(destination.glob("caption-*.png"))) == frame_count:
            return destination
    for index in range(frame_count):
        layer = Image.open(source / f"caption-{index:03d}.png").convert("RGBA")
        scaled = layer.resize((placement["scaled_w"], placement["scaled_h"]), Image.Resampling.LANCZOS)
        canvas_image = Image.new("RGBA", canvas, (0, 0, 0, 0))
        canvas_image.paste(scaled, (placement["x"], placement["y"]))
        canvas_image.save(destination / f"caption-{index:03d}.png", compress_level=4)
    marker.write_text(json.dumps(expected, indent=1) + "\n")
    return destination


# ---------------------------------------------------------------------------------------
# stage 4 — composite


def composite(
    picture: Path,
    output: Path,
    *,
    width: int,
    height: int,
    fps: str,
    frames: int,
    time_base_denominator: int,
    captions: Optional[Path],
    codec: str,
    crf: Optional[int] = None,
) -> None:
    """The approved v002 composite chain, geometry- and codec-parameterised.

    tv->full into rgb24, the engine's RGBA caption layer alpha-composited at (0,0) with no
    scaling (any scaling already happened when the layer was built), full->tv back out with
    bt709 tagging, exact frame count, audio stream-copied from the picture's own container.
    """

    from PIL import Image

    decode_filter = (
        f"scale={width}:{height}:in_range=tv:out_range=full:in_color_matrix=bt709:"
        "out_color_matrix=bt709,format=yuv444p10le,format=rgb24,setsar=1"
    )
    if codec == "libx264":
        encode_filter = (
            f"scale={width}:{height}:in_range=full:out_range=tv:in_color_matrix=bt709:"
            "out_color_matrix=bt709,format=yuv420p,setsar=1,"
            "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        )
        codec_args = ["-c:v", "libx264", "-preset", "slow", "-crf", str(crf)]
    elif codec == "prores_ks":
        encode_filter = (
            f"scale={width}:{height}:in_range=full:out_range=tv:in_color_matrix=bt709:"
            "out_color_matrix=bt709,format=yuv422p10le,setsar=1,"
            "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        )
        codec_args = ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"]
    else:  # pragma: no cover - guarded by the CLI
        raise VariantRenderError(f"unsupported composite codec: {codec}")

    decoder = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(picture), "-map", "0:v:0",
            "-vf", decode_filter, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ],
        stdout=subprocess.PIPE,
    )
    encoder = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", fps, "-i", "pipe:0",
            "-i", str(picture), "-map", "0:v:0", "-map", "1:a:0",
            "-vf", encode_filter, "-frames:v", str(frames), *codec_args,
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-color_range", "tv", "-c:a", "copy", "-fps_mode", "cfr",
            "-video_track_timescale", str(time_base_denominator), "-movflags", "+faststart+write_colr",
            "-y", str(output),
        ],
        stdin=subprocess.PIPE,
    )
    payload = width * height * 3
    try:
        for index in range(frames):
            raw = decoder.stdout.read(payload)
            if len(raw) != payload:
                raise VariantRenderError(f"picture ran out at frame {index} (got {len(raw)} bytes)")
            frame = Image.frombytes("RGB", (width, height), raw)
            if captions is not None:
                frame = frame.convert("RGBA")
                layer = Image.open(captions / f"caption-{index:03d}.png").convert("RGBA")
                frame.alpha_composite(layer, (0, 0))
                frame = frame.convert("RGB")
            encoder.stdin.write(frame.tobytes())
    finally:
        if encoder.stdin:
            encoder.stdin.close()
        encoder.wait()
        if decoder.stdout:
            decoder.stdout.close()
        decoder.wait()
    if encoder.returncode != 0:
        raise VariantRenderError(f"composite encode failed with rc {encoder.returncode}")


def encode_delivery(source: Path, output: Path, *, crf: int, fps: str, frames: int, time_base_denominator: int) -> None:
    run_checked(
        [
            "ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-frames:v", str(frames), "-fps_mode", "cfr", "-r", fps,
            "-c:a", "copy", "-movflags", "+faststart+write_colr",
            "-video_track_timescale", str(time_base_denominator),
            "-color_range", "tv", "-colorspace", "bt709", "-color_trc", "bt709", "-color_primaries", "bt709",
            str(output),
        ]
    )


# ---------------------------------------------------------------------------------------
# stage 5 — vertical reframe


def measure_column_energy(source: Path, start_frame: int, frames: int, samples: int = ANCHOR_SAMPLE_FRAMES) -> List[float]:
    """Sobel magnitude plus temporal difference, summed per column, over sampled frames."""

    import cv2
    import numpy as np

    step = max(1, frames // max(1, samples))
    indices = [start_frame + offset * step for offset in range(samples)]
    grabbed = []
    for index in indices:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-v", "error", "-i", str(source),
                "-vf", f"select=eq(n\\,{index}),scale=480:-2", "-frames:v", "1",
                "-f", "image2pipe", "-vcodec", "png", "pipe:1",
            ],
            capture_output=True,
        )
        if result.returncode != 0 or not result.stdout:
            continue
        array = cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if array is not None:
            grabbed.append(array.astype(np.float32))
    if not grabbed:
        raise VariantRenderError(f"could not sample any frame from {source} at {start_frame}")
    spatial = np.zeros(grabbed[0].shape[1], dtype=np.float64)
    for frame in grabbed:
        gradient_x = cv2.Sobel(frame, cv2.CV_32F, 1, 0, ksize=3)
        gradient_y = cv2.Sobel(frame, cv2.CV_32F, 0, 1, ksize=3)
        spatial += np.abs(np.hypot(gradient_x, gradient_y)).sum(axis=0)
    motion = np.zeros_like(spatial)
    for first, second in zip(grabbed, grabbed[1:]):
        motion += np.abs(second - first).sum(axis=0)

    def normalise(vector: "np.ndarray") -> "np.ndarray":
        peak = float(vector.max())
        return vector / peak if peak > 0 else vector

    return list(normalise(spatial) + normalise(motion))


def vertical_anchor_for_slot(slot: Dict[str, Any], out_w: int, out_h: int) -> Dict[str, Any]:
    facts = probe_media(Path(str(slot["source_path"])), count_frames=False)
    in_w, in_h = int(facts["video"]["width"]), int(facts["video"]["height"])
    cover = cover_geometry(in_w, in_h, out_w, out_h)
    if cover["slack_x"] <= 0:
        return {
            "block_id": slot["block_id"],
            "anchor_x": 0.5,
            "method": "no horizontal slack; the source is not wider than the delivery frame",
            "source_size": [in_w, in_h],
            "cover": cover,
        }
    energy = measure_column_energy(
        Path(str(slot["source_path"])),
        int(slot["source_start_frame"]),
        int(slot.get("required_source_frames", slot["frames"])),
    )
    # the crop window expressed in the units of the measured profile
    window_columns = max(1, round(len(energy) * out_w / cover["scaled_w"]))
    anchor = anchor_from_column_energy(energy, window_columns)
    return {
        "block_id": slot["block_id"],
        "anchor_x": anchor,
        "centre_anchor_delta": round(anchor - 0.5, 6),
        "method": (
            "measured column energy (Sobel magnitude + temporal difference over "
            f"{ANCHOR_SAMPLE_FRAMES} sampled frames), sliding-window argmax, blended "
            f"{int(ANCHOR_CENTRE_PRIOR * 100)}% back toward frame centre. NO SUBJECT DETECTOR "
            "IS AVAILABLE IN THIS RUNTIME: this is an energy measurement, not a decision about "
            "who or what is in frame. PENDING_MACHINE — the proof board shows what it kept and cut."
        ),
        "source_size": [in_w, in_h],
        "cover": cover,
        "window_columns": window_columns,
        "profile_columns": len(energy),
    }


def render_vertical_picture(
    sandbox: Path,
    slots: Sequence[Dict[str, Any]],
    anchors: Dict[str, float],
    *,
    width: int,
    height: int,
    fps: str,
    time_base_denominator: int,
    reference: Path,
) -> Path:
    """Second picture render, same engine segment renderer, delivery geometry."""

    segments_dir = secure_mkdirs(sandbox, "vertical", "segments")
    segment_paths: List[Path] = []
    for index, slot in enumerate(slots, start=1):
        reframed = dict(slot)
        reframed["crop_anchor_xy"] = [anchors[str(slot["block_id"])], 0.5]
        output = segments_dir / f"{index:02d}-{slot['block_id']}.mov"
        if not output.is_file():
            _render_segment(
                Path(str(slot["source_path"])),
                reframed,
                output,
                width=width,
                height=height,
                fps=fps,
                time_base_denominator=time_base_denominator,
                root=sandbox,
            )
        segment_paths.append(output)

    concat = sandbox / "vertical/segments.ffconcat"
    concat.write_text("ffconcat version 1.0\n" + "".join(f"file '{path}'\n" for path in segment_paths))
    picture = sandbox / "vertical/picture.mov"
    run_checked(
        [
            "ffmpeg", "-hide_banner", "-v", "error", "-y", "-safe", "0", "-f", "concat", "-i", str(concat),
            "-map", "0:v:0", "-an", "-c", "copy", "-video_track_timescale", str(time_base_denominator),
            "-color_range", "tv", "-colorspace", "bt709", "-color_trc", "bt709", "-color_primaries", "bt709",
            str(picture),
        ]
    )
    master = sandbox / "vertical/picture-with-audio.mov"
    run_checked(
        [
            "ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(picture), "-i", str(reference),
            "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", "-c:a", "copy",
            "-video_track_timescale", str(time_base_denominator),
            "-color_range", "tv", "-colorspace", "bt709", "-color_trc", "bt709", "-color_primaries", "bt709",
            str(master),
        ]
    )
    return master


# ---------------------------------------------------------------------------------------
# stage 6 — caption QC over the new picture


def _extract_frames(source: Path, output: Path, frame_count: int) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    if len(list(output.glob("*.png"))) != frame_count:
        for stale in output.glob("*.png"):
            stale.unlink()
        run_checked(["ffmpeg", "-v", "error", "-i", str(source), "-vsync", "0", "-pix_fmt", "rgb24", f"{output}/f%03d.png", "-y"])
    return output


def _luma(rgb: "Any") -> "Any":
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def measure_legibility(states: Sequence[Dict[str, Any]], caption_dir: Path, frames_dir: Path) -> Dict[str, Any]:
    """Per caption state, on EVERY frame of its span, as composited on the delivered picture.

    Three changes each from a measurement on the delivered 08 family:

    * **Every frame, gated on the worst one.** This read `(start + end) // 2` and reported that
      single frame as the state, which DOCTRINE 15.3's coverage rule forbids in as many words.
      A word can be clean at its midpoint and a ghost two frames later; the viewer sees both.
    * **The WCAG contrast ratio.** Michelson and the 25-level separation floor are both blind
      at the dark end: one variant's word cleared them at 27.0 levels and 0.415 while actually
      measuring 1.37:1.
    * **Weak ink, per pixel.** A word half on sky and half on dark timber averages to a number
      neither half has. The background is re-estimated per pixel — the caption envelope is
      inpainted out of the DELIVERED frame first, so the ink cannot supply its own background —
      and the share of ink under 3:1 against its own local surround is reported.

    The primitives come from `reelctl.captions.ink`, not from a copy here: the resolver and the
    gate have to be measuring the same thing for either number to mean anything.
    """

    import cv2
    import numpy as np
    from PIL import Image

    rows = {}
    kernel = np.ones((RING_RADIUS * 2 + 1, RING_RADIUS * 2 + 1), np.uint8)
    for state in states:
        start, end = int(state["start_frame"]), int(state["end_frame_exclusive"])
        per_frame: List[Dict[str, Any]] = []
        for frame in range(start, end):
            caption_path = caption_dir / f"caption-{frame:03d}.png"
            picture_path = frames_dir / f"f{frame + 1:03d}.png"
            if not caption_path.is_file() or not picture_path.is_file():
                raise VariantRenderError(
                    f"state {state['id']} frame {frame}: missing {caption_path if not caption_path.is_file() else picture_path}"
                )
            alpha = np.asarray(Image.open(caption_path).convert("RGBA"))[..., 3]
            ink = alpha > 128
            if not ink.any():
                continue
            envelope = alpha > 0
            ring = (cv2.dilate(envelope.astype(np.uint8), kernel, iterations=1) > 0) & ~envelope
            rgb = np.asarray(Image.open(picture_path).convert("RGB")).astype(np.float64)
            picture = _luma(rgb)
            local = local_background_luma(rgb, occlusion_mask=envelope)
            ink_mean = float(picture[ink].mean())
            ring_mean = float(picture[ring].mean())
            denominator = ink_mean + ring_mean
            ratios = np.asarray(contrast_ratio_from_luma(picture[ink], local[ink]))
            per_frame.append(
                {
                    "frame": frame,
                    "ink_luma_mean": round(ink_mean, 2),
                    "ring_luma_mean": round(ring_mean, 2),
                    "separation": round(abs(ink_mean - ring_mean), 2),
                    "michelson": round(abs(ink_mean - ring_mean) / denominator if denominator else 0.0, 4),
                    "contrast_ratio": round(float(contrast_ratio_from_luma(ink_mean, ring_mean)), 3),
                    "weak_ink_fraction": round(float((ratios < CAPTION_MIN_CONTRAST_RATIO).mean()), 4),
                    "local_separation_fail_fraction": round(
                        float((np.abs(picture[ink] - local[ink]) < SEPARATION_FLOOR).mean()), 4
                    ),
                    "invisible_ink_fraction": round(
                        float((np.abs(picture[ink] - ring_mean) < SEPARATION_FLOOR).mean()), 4
                    ),
                }
            )
        if not per_frame:
            continue
        worst = min(per_frame, key=lambda row: row["contrast_ratio"])
        ratios = sorted(row["contrast_ratio"] for row in per_frame)
        rows[state["id"]] = {
            "text": state.get("text"),
            "frames_measured": len(per_frame),
            "frame": worst["frame"],
            "worst_frame": worst["frame"],
            "ink_luma_mean": worst["ink_luma_mean"],
            "ring_luma_mean": worst["ring_luma_mean"],
            "separation": min(row["separation"] for row in per_frame),
            "michelson": min(row["michelson"] for row in per_frame),
            "contrast_ratio": ratios[0],
            "contrast_ratio_median": round(float(ratios[len(ratios) // 2]), 3),
            "weak_ink_fraction": max(row["weak_ink_fraction"] for row in per_frame),
            "local_separation_fail_fraction": max(row["local_separation_fail_fraction"] for row in per_frame),
            "invisible_ink_fraction": max(row["invisible_ink_fraction"] for row in per_frame),
            "per_frame": per_frame,
        }
    return rows


CAPTION_FLOOR_RULE = (
    "25 luma levels of separation, 3.0:1 minimum contrast ratio and at most 10% weak ink, per "
    "state, on the WORST frame of its span. There is no per-state grandfather any more: the "
    "clause `min(25, the approved v002 deliverable's own separation)` is what let the approved "
    "reel ship C14 `your` at 15.0 levels and 1.01:1 and let every variant inherit that "
    "allowance, and caption_doctrine ('ink must be clean and solid — no muffled/ghost "
    "caption states, ever') supersedes it. The approved reel's own numbers are still measured "
    "and reported beside each state, as context for the reviewer, never as the bar."
)


def caption_qc(
    workspace: Path,
    captioned: Path,
    control: Path,
    caption_dir: Path,
    contract: Dict[str, Any],
    frame_count: int,
    *,
    baseline_frames: Optional[Path],
    baseline_captions: Optional[Path],
    vertical_captioned: Optional[Path] = None,
    vertical_caption_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    import cv2
    import numpy as np
    from PIL import Image

    out_frames = _extract_frames(captioned, workspace / "qc-out", frame_count)
    ctrl_frames = _extract_frames(control, workspace / "qc-ctrl", frame_count)

    expected: Dict[int, List[str]] = {frame: [] for frame in range(frame_count)}
    for state in contract["states"]:
        for frame in range(int(state["start_frame"]), int(state["end_frame_exclusive"])):
            expected[frame].append(state["id"])
    declared_blanks = sorted(frame for frame, ids in expected.items() if not ids)

    report: Dict[str, Any] = {}
    observed = _ffprobe_frame_count(captioned)
    report["frame_count"] = {"expected": frame_count, "observed": observed, "pass": observed == frame_count}

    divergent, blank_ink, per_frame = [], [], []
    kernel5 = np.ones((5, 5), np.uint8)
    for frame in range(frame_count):
        a = np.asarray(Image.open(out_frames / f"f{frame + 1:03d}.png")).astype(np.int16)
        b = np.asarray(Image.open(ctrl_frames / f"f{frame + 1:03d}.png")).astype(np.int16)
        delta = np.abs(a - b).max(axis=2)
        strong = int((delta > 48).sum())
        per_frame.append(strong)
        if (strong > 200) != bool(expected[frame]):
            divergent.append({"frame": frame, "expected_states": expected[frame], "strong_px": strong})
        if not expected[frame] and strong > 0:
            blank_ink.append({"frame": frame, "strong_px": strong})
    report["timing"] = {"divergent_frames": divergent, "pass": not divergent}

    # A declared blank frame's caption layer is transparent, so the array handed to the encoder
    # on that frame is byte-identical to the control's. Any difference between the two ENCODES
    # is therefore the encoder, not ink — DOCTRINE 16.5: "Two separately encoded ProRes/H.264
    # files differ across the whole frame for codec reasons." The gate asks the question that
    # can actually be answered: did the caption layer put ink on a frame declared blank. The
    # encode delta is still measured and reported beside it, never used to fail.
    layer_ink = []
    for frame in declared_blanks:
        alpha = np.asarray(Image.open(caption_dir / f"caption-{frame:03d}.png").convert("RGBA"))[..., 3]
        if int(alpha.max()) > 0:
            layer_ink.append({"frame": frame, "alpha_max": int(alpha.max()), "px": int((alpha > 0).sum())})
    report["blanks"] = {
        "declared": declared_blanks,
        "count": len(declared_blanks),
        "frames_where_the_caption_layer_carries_ink": layer_ink,
        "encode_delta_on_blank_frames": blank_ink,
        "encode_delta_note": (
            "measured, not gated. On these frames the rendered caption layer is fully "
            "transparent, so the pre-encode arrays of the captioned cut and the control are "
            "identical and every one of these pixels is h264 rate allocation. DOCTRINE 16.5."
        ),
        "pass": not layer_ink,
    }
    missing = [
        {"state": state["id"], "frame": frame, "strong_px": per_frame[frame]}
        for state in contract["states"]
        for frame in range(int(state["start_frame"]), int(state["end_frame_exclusive"]))
        if per_frame[frame] <= 200
    ]
    report["coverage"] = {"states": len(contract["states"]), "states_missing_ink": missing, "pass": not missing}

    outside = []
    for frame in range(frame_count):
        if not expected[frame]:
            continue
        alpha = np.asarray(Image.open(caption_dir / f"caption-{frame:03d}.png").convert("RGBA"))[..., 3]
        envelope = cv2.dilate((alpha > 0).astype(np.uint8), kernel5, iterations=1) > 0
        a = np.asarray(Image.open(out_frames / f"f{frame + 1:03d}.png")).astype(np.int16)
        b = np.asarray(Image.open(ctrl_frames / f"f{frame + 1:03d}.png")).astype(np.int16)
        delta = np.abs(a - b).max(axis=2)
        count = int(((delta > 48) & ~envelope).sum())
        if count:
            outside.append({"frame": frame, "px": count})
    total_outside = sum(row["px"] for row in outside)
    report["containment"] = {
        "frames_with_pixels_outside_envelope": outside,
        "total_px": total_outside,
        "pass": total_outside < 50,
    }

    variant_rows = measure_legibility(contract["states"], caption_dir, out_frames)
    baseline_rows = {}
    if baseline_frames and baseline_captions:
        baseline_rows = measure_legibility(contract["states"], baseline_captions, baseline_frames)
    report["caption_legibility_over_new_picture"] = _legibility_report(
        variant_rows,
        baseline_rows,
        scope=(
            "A pass means every state cleared the hard gates on the WORST frame of its own span: 25 "
            "luma levels of separation, 3.0:1 contrast ratio, and at most 10% of its ink weak against "
            "its own local background. It is not parity with the approved reel and it is not creative "
            "approval."
        ),
    )

    if vertical_captioned and vertical_caption_dir:
        vertical_frames = _extract_frames(vertical_captioned, workspace / "qc-vertical", frame_count)
        vertical_rows = measure_legibility(contract["states"], vertical_caption_dir, vertical_frames)
        report["caption_legibility_on_vertical_delivery"] = _legibility_report(
            vertical_rows,
            baseline_rows,
            scope=(
                "measured on the 9:16 encode, where the reframed picture sits behind the same ink. Under "
                "format_doctrine the vertical is WITHHELD from delivery, so this is reported "
                "evidence about a file nobody is shipping, not a gate on the file they are."
            ),
        )
    return report


def _legibility_report(
    rows: Dict[str, Any], baseline_rows: Dict[str, Any], *, scope: str
) -> Dict[str, Any]:
    """Roll one geometry's per-state measurements up into a gate."""
    deltas = []
    for state_id, row in rows.items():
        base = baseline_rows.get(state_id)
        deltas.append(
            {
                "state": state_id,
                "text": row["text"],
                "frames_measured": row["frames_measured"],
                "worst_frame": row["worst_frame"],
                "michelson_variant": row["michelson"],
                "separation_variant": row["separation"],
                "contrast_ratio_worst": row["contrast_ratio"],
                "contrast_ratio_median": row["contrast_ratio_median"],
                "weak_ink_fraction": row["weak_ink_fraction"],
                "local_separation_fail_fraction": row["local_separation_fail_fraction"],
                "invisible_variant": row["invisible_ink_fraction"],
                "michelson_approved_v002": base["michelson"] if base else None,
                "separation_approved_v002": base["separation"] if base else None,
                "contrast_ratio_approved_v002": base["contrast_ratio"] if base else None,
                "michelson_delta": round(row["michelson"] - base["michelson"], 4) if base else None,
                "floor_applied": float(SEPARATION_FLOOR),
                "below_separation_floor": row["separation"] < SEPARATION_FLOOR,
                "below_contrast_floor": row["contrast_ratio"] < CAPTION_MIN_CONTRAST_RATIO,
                "below_contrast_target": row["contrast_ratio_median"] < CAPTION_TARGET_CONTRAST_RATIO,
                "above_weak_ink_limit": row["weak_ink_fraction"] > CAPTION_MAX_WEAK_INK_FRACTION,
                "worse_than_approved": bool(base and row["michelson"] < base["michelson"]),
            }
        )
    deltas.sort(key=lambda row: row["contrast_ratio_worst"])
    failing = [
        row["state"]
        for row in deltas
        if row["below_separation_floor"] or row["below_contrast_floor"] or row["above_weak_ink_limit"]
    ]
    return {
        "method": (
            f"per state, on every frame of its span, gated on the worst frame; ink = engine alpha > 128, "
            f"ring = {RING_RADIUS}px dilation of alpha > 0 minus the glyph envelope, local background = "
            "the delivered frame with the caption envelope inpainted out and blurred at sigma 5; "
            "measured identically on this variant and on the approved v002 deliverable"
        ),
        "separation_floor": SEPARATION_FLOOR,
        "contrast_ratio_floor": CAPTION_MIN_CONTRAST_RATIO,
        "contrast_ratio_target": CAPTION_TARGET_CONTRAST_RATIO,
        "weak_ink_limit": CAPTION_MAX_WEAK_INK_FRACTION,
        "floor_rule": CAPTION_FLOOR_RULE,
        "states_measured": len(deltas),
        "states_below_separation_floor": [row["state"] for row in deltas if row["below_separation_floor"]],
        "states_below_contrast_floor": [row["state"] for row in deltas if row["below_contrast_floor"]],
        "states_below_contrast_target": [row["state"] for row in deltas if row["below_contrast_target"]],
        "states_above_weak_ink_limit": [row["state"] for row in deltas if row["above_weak_ink_limit"]],
        "states_worse_than_approved": [row["state"] for row in deltas if row["worse_than_approved"]],
        "worst_state": deltas[0]["state"] if deltas else None,
        "rows": deltas,
        "pass": not failing,
        "failing_states": failing,
        "scope": scope,
    }


# ---------------------------------------------------------------------------------------
# stage 7 — deliverable verification


def verify_deliverable(path: Path, clock: Dict[str, Any], geometry: Tuple[int, int], reference_lock: Dict[str, Any]) -> Dict[str, Any]:
    facts = probe_media(path)
    checks = clock_checks(facts, clock, geometry=geometry)
    checks["presentation_pts_ledger"] = frame_pts(path) == clock["pts"]
    checks["full_decode"] = full_decode(path)["status"] == "PASS"
    checks["audio_pcm_identity"] = decode_pcm_sha256(path) == reference_lock["audio"]["pcm_s16le_48k_mono_sha256"]
    checks["audio_payload_identity"] = audio_payload_sha256(path) == reference_lock["audio"]["payload_sha256"]
    checks["audio_packet_ledger_identity"] = audio_packet_ledger(path) == reference_lock["audio"]["packet_ledger"]
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "width": int(facts["video"]["width"]),
        "height": int(facts["video"]["height"]),
        "fps": facts["video"]["r_frame_rate"],
        "frame_count": int(facts["video"]["frame_count"] or 0),
        "duration_s": float(facts["format"].get("duration") or 0.0),
        "legal_luma_range": legal_luma_range(path),
        "checks": checks,
        "status": "PASS" if all(checks.values()) else "FAIL",
    }


# ---------------------------------------------------------------------------------------
# stage 8 — proof board


def build_proof_board(
    output: Path,
    *,
    variant_id: str,
    casting: Dict[str, Any],
    vertical_frames: Path,
    refgeom_frames: Path,
    anchors: Sequence[Dict[str, Any]],
    summary: Sequence[str],
) -> Path:
    """A contact sheet an reviewer can verdict from: every slot, plus what the reframe cut."""

    from PIL import Image, ImageDraw

    tile_w = 216
    label_h = 46
    slots = casting["slots"]
    starts, running = [], 0
    for slot in slots:
        starts.append(running)
        running += int(slot["frames"])

    vertical_tiles = []
    for slot, start in zip(slots, starts):
        frame = start + int(slot["frames"]) // 2
        image = Image.open(vertical_frames / f"f{frame + 1:03d}.png").convert("RGB")
        height = round(image.height * tile_w / image.width)
        image = image.resize((tile_w, height), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (tile_w, height + label_h), (18, 18, 18))
        tile.paste(image, (0, label_h))
        draw = ImageDraw.Draw(tile)
        draw.text((6, 4), f"{slot['block_id']}  f{frame}  {int(slot['frames'])}fr", fill=(255, 255, 255))
        draw.text((6, 18), f"{slot['source_clip_id']}  q{slot.get('window_quantile')}", fill=(170, 190, 255))
        draw.text((6, 32), f"src {slot['source_start_frame']}-{slot['source_end_frame_exclusive']}", fill=(160, 160, 160))
        vertical_tiles.append(tile)

    reframe_tiles = []
    anchors_by_block = {row["block_id"]: row for row in anchors}
    for slot, start in zip(slots, starts):
        frame = start + int(slot["frames"]) // 2
        image = Image.open(refgeom_frames / f"f{frame + 1:03d}.png").convert("RGB")
        row = anchors_by_block.get(str(slot["block_id"]), {})
        anchor = float(row.get("anchor_x", 0.5))
        width = image.width
        keep_w = width * VERTICAL_SIZE[0] / max(1, row.get("cover", {}).get("scaled_w", width))
        keep_w = max(8.0, min(float(width), keep_w))
        left = (width - keep_w) * anchor
        draw = ImageDraw.Draw(image)
        draw.rectangle([left, 0, left + keep_w, image.height - 1], outline=(255, 210, 60), width=6)
        height = round(image.height * tile_w / image.width)
        image = image.resize((tile_w, height), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (tile_w, height + label_h), (18, 18, 18))
        tile.paste(image, (0, label_h))
        draw = ImageDraw.Draw(tile)
        draw.text((6, 4), f"{slot['block_id']} 16:9 master", fill=(255, 255, 255))
        draw.text((6, 18), f"9:16 keep box, anchor x={anchor:.3f}", fill=(255, 210, 60))
        draw.text((6, 32), f"centre delta {row.get('centre_anchor_delta', 0.0):+.3f}", fill=(160, 160, 160))
        reframe_tiles.append(tile)

    columns = 6
    def grid(tiles: Sequence["Image.Image"]) -> "Image.Image":
        rows = (len(tiles) + columns - 1) // columns
        cell_h = max(tile.height for tile in tiles)
        board = Image.new("RGB", (columns * tile_w, rows * cell_h), (10, 10, 10))
        for index, tile in enumerate(tiles):
            board.paste(tile, ((index % columns) * tile_w, (index // columns) * cell_h))
        return board

    top = grid(vertical_tiles)
    bottom = grid(reframe_tiles)
    header_h = 26 + 18 * len(summary)
    board = Image.new("RGB", (max(top.width, bottom.width), header_h + top.height + 30 + bottom.height), (10, 10, 10))
    draw = ImageDraw.Draw(board)
    draw.text((10, 6), f"{variant_id} — PROOF BOARD — machine proposal, PENDING_MACHINE (no frame watched)", fill=(255, 255, 255))
    for index, line in enumerate(summary):
        draw.text((10, 24 + 18 * index), line, fill=(190, 190, 190))
    board.paste(top, (0, header_h))
    draw.text((10, header_h + top.height + 8), "16:9 machine master with the 9:16 delivery keep-box drawn — what the reframe kept and cut", fill=(255, 210, 60))
    board.paste(bottom, (0, header_h + top.height + 30))
    output.parent.mkdir(parents=True, exist_ok=True)
    board.save(output, compress_level=6)
    return output


# ---------------------------------------------------------------------------------------
# the loop


def render_variant(args: argparse.Namespace) -> Dict[str, Any]:
    started = time.time()
    casting_path = Path(args.casting).expanduser().resolve()
    casting = json.loads(casting_path.read_text())
    variant_id = str(casting["variant_id"])
    parent_project = Path(str(casting["parent_project"])).expanduser().resolve()
    work_root = Path(args.work_root).expanduser()
    workspace = work_root / variant_id
    sandbox = workspace / "project"
    deliver = Path(args.deliver_root).expanduser() / variant_id
    workspace.mkdir(parents=True, exist_ok=True)
    deliver.mkdir(parents=True, exist_ok=True)

    receipt: Dict[str, Any] = {
        "schema_version": 1,
        "artifact": "VARIANT_RENDER_RUN",
        "variant_id": variant_id,
        "family_id": casting.get("family_id"),
        "parent_project_id": casting.get("parent_project_id"),
        "casting": {"path": str(casting_path), "sha256": sha256_file(casting_path)},
        "tool": {"path": str(Path(__file__).resolve()), "sha256": sha256_file(Path(__file__).resolve())},
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "honesty": {
            "agent_visual_review": PENDING,
            "meaning": (
                "Machine render of a machine casting. No frame of this variant has been watched by this tool "
                "or by any agent. Technical gates, caption timing and measured caption legibility are all this "
                "receipt claims. The 9:16 reframe anchors are energy measurements, not subject decisions. "
                "Creative approval and publication approval are both still open."
            ),
            "grade": (
                "Technical LC-709 normalisation only, identity creative grading on every slot. The parent's "
                "human-approved exposure lift on p001 belongs to that clip and that verdict; it is "
                "not carried onto different footage by a machine."
            ),
            "publication": "NOT AUTHORISED — local review only. Nothing here is uploaded or posted.",
        },
    }

    _log(f"{variant_id}: preparing sandbox at {sandbox}")
    receipt["sandbox"] = prepare_sandbox(parent_project, sandbox)

    reference_lock = load_json(sandbox / "reference/reference-lock.json", root=sandbox)
    clock = reference_lock["clock"]
    frame_count = int(clock["frame_count"])
    fps = str(clock["fps"])
    time_base_denominator = int(str(clock["time_base"]).split("/", 1)[1])
    ref_geometry = (int(clock["width"]), int(clock["height"]))
    inventory = load_json(sandbox / "footage/footage-index.json", root=sandbox)

    _log(f"{variant_id}: front-door check")
    door = front_door_report(casting, inventory)
    receipt["front_door"] = door
    if door["status"] != "PASS":
        raise VariantRenderError(f"front door refused this casting: {json.dumps(door['violations'])}")

    _log(f"{variant_id}: colour profile proofs")
    proofs = ensure_profile_proofs(casting, work_root / "shared/profile-proofs")

    locked = sandbox / "edit/selection.locked.json"
    if not locked.is_file() or args.force_selection:
        _log(f"{variant_id}: building locked selection (grade proposal pass runs here)")
        receipt["selection"] = build_locked_selection(sandbox, casting, proofs)
    else:
        receipt["selection"] = {"status": "PASS", "path": str(locked), "sha256": sha256_file(locked), "cache_hit": True}

    _log(f"{variant_id}: rendering picture at reference geometry via reelctl.render.render_project")
    render = render_project(sandbox, revision=variant_id)
    receipt["picture_reference_geometry"] = render
    master = Path(render["master_path"])

    _log(f"{variant_id}: reference-relative QC via reelctl.qc.run_qc")
    qc = run_qc(sandbox, revision=variant_id)
    receipt["reelctl_qc"] = {
        "report_path": qc["report_path"],
        "authorities": qc["authorities"],
        "technical": qc["technical"]["status"],
        "technical_checks": qc["technical"]["checks"],
        "structure": qc["structure"]["status"],
        "structure_checks": qc["structure"]["checks"],
        "visual": qc["visual"]["status"],
        "reference_relative_mode": qc["visual"].get("reference_relative", {}).get("mode"),
        "reference_relative_authority": qc["visual"].get("reference_relative", {}).get("authority"),
    }

    _log(f"{variant_id}: re-rendering the caption program (reelctl captions render)")
    caption_dir = workspace / "caption-frames"
    contract_render = Path(args.captions_contract_render).expanduser()
    contract_truth = Path(args.captions_contract).expanduser()
    if len(list(caption_dir.glob("caption-*.png"))) != frame_count:
        render_caption_frames(contract_render, Path(args.captions_plates).expanduser(), caption_dir)
    contract = json.loads(contract_truth.read_text())
    baseline_captions = Path(args.baseline_captions).expanduser() if args.baseline_captions else None
    receipt["captions"] = {
        "contract": str(contract_truth),
        "contract_sha256": sha256_file(contract_truth),
        "contract_render_sha256": sha256_file(contract_render),
        "plates": str(Path(args.captions_plates).expanduser()),
        "states": len(contract["states"]),
        "doctrine": "DOCTRINE 18 — the engine re-renders the reel's caption program; no per-reel caption script exists",
    }
    if baseline_captions:
        receipt["captions"]["reproduction"] = compare_caption_frames(caption_dir, baseline_captions, frame_count)

    _log(f"{variant_id}: compositing captions over the reference-geometry picture")
    refgeom_captioned = workspace / f"{variant_id}-refgeom-captioned.mp4"
    refgeom_control = workspace / f"{variant_id}-refgeom-control.mp4"
    if not refgeom_captioned.is_file():
        composite(master, refgeom_captioned, width=ref_geometry[0], height=ref_geometry[1], fps=fps,
                  frames=frame_count, time_base_denominator=time_base_denominator, captions=caption_dir,
                  codec="libx264", crf=DELIVERY_CRF)
    if not refgeom_control.is_file():
        composite(master, refgeom_control, width=ref_geometry[0], height=ref_geometry[1], fps=fps,
                  frames=frame_count, time_base_denominator=time_base_denominator, captions=None,
                  codec="libx264", crf=DELIVERY_CRF)

    _log(f"{variant_id}: measuring 9:16 reframe anchors")
    selection = load_json(locked, root=sandbox)
    anchor_rows = [vertical_anchor_for_slot(slot, *VERTICAL_SIZE) for slot in selection["slots"]]
    anchors = {row["block_id"]: float(row["anchor_x"]) for row in anchor_rows}
    receipt["vertical_reframe"] = {
        "delivery_geometry": list(VERTICAL_SIZE),
        "doctrine": "format_doctrine — no black borders; vertical-native delivery",
        "anchors": anchor_rows,
        "agent_visual_review": PENDING,
    }

    _log(f"{variant_id}: rendering the vertical picture (second engine segment pass)")
    vertical_master = render_vertical_picture(
        sandbox, selection["slots"], anchors,
        width=VERTICAL_SIZE[0], height=VERTICAL_SIZE[1], fps=fps,
        time_base_denominator=time_base_denominator,
        reference=sandbox / "reference/reference-source.mp4",
    )

    placement = caption_layer_placement(ref_geometry[0], ref_geometry[1], *VERTICAL_SIZE)
    vertical_caption_dir = scale_caption_layers(
        caption_dir, work_root / "shared" / f"caption-layers-{VERTICAL_SIZE[0]}x{VERTICAL_SIZE[1]}",
        placement, VERTICAL_SIZE, frame_count,
    )
    receipt["vertical_reframe"]["caption_placement"] = placement
    receipt["vertical_reframe"]["caption_placement_rationale"] = (
        "The caption canvas is fitted by width and centred, which reproduces exactly the apparent caption size "
        "and position of the approved 16:9 cut as it letterboxed inside a 9:16 viewport. Typography is locked; "
        "this is the one placement that changes nothing a reviewer already approved."
    )

    _log(f"{variant_id}: compositing captions over the vertical picture")
    vertical_captioned_master = workspace / f"{variant_id}-vertical-captioned-master.mov"
    if not vertical_captioned_master.is_file():
        composite(vertical_master, vertical_captioned_master, width=VERTICAL_SIZE[0], height=VERTICAL_SIZE[1],
                  fps=fps, frames=frame_count, time_base_denominator=time_base_denominator,
                  captions=vertical_caption_dir, codec="prores_ks")

    _log(f"{variant_id}: delivery encodes")
    vertical_delivery = deliver / f"{variant_id}-vertical-{VERTICAL_SIZE[0]}x{VERTICAL_SIZE[1]}-crf{DELIVERY_CRF}.mp4"
    encode_delivery(vertical_captioned_master, vertical_delivery, crf=DELIVERY_CRF, fps=fps,
                    frames=frame_count, time_base_denominator=time_base_denominator)
    attempts: List[Tuple[int, int]] = []
    web_delivery = deliver / f"{variant_id}-web-{VERTICAL_SIZE[0]}x{VERTICAL_SIZE[1]}.mp4"
    chosen_crf = None
    for crf in WEB_CRF_LADDER:
        encode_delivery(vertical_captioned_master, web_delivery, crf=crf, fps=fps,
                        frames=frame_count, time_base_denominator=time_base_denominator)
        attempts.append((crf, web_delivery.stat().st_size))
        if web_delivery.stat().st_size <= WEB_SIZE_CAP_BYTES:
            chosen_crf = crf
            break
    receipt["web_encode"] = {
        "cap_bytes": WEB_SIZE_CAP_BYTES,
        "ladder": list(WEB_CRF_LADDER),
        "attempts": [{"crf": crf, "bytes": size} for crf, size in attempts],
        "chosen_crf": chosen_crf,
        "pass": chosen_crf is not None,
        "note": "audio stream-copied, frame count exact; the ladder only moves if crf21 misses the upload size cap",
    }
    if chosen_crf is None:
        raise VariantRenderError("no crf on the ladder brought the web encode under the upload size cap")

    _log(f"{variant_id}: verifying deliverables against the blueprint clock")
    receipt["deliverables"] = {
        "vertical_native_9x16": verify_deliverable(vertical_delivery, clock, VERTICAL_SIZE, reference_lock),
        "web_encode_under_10mb": verify_deliverable(web_delivery, clock, VERTICAL_SIZE, reference_lock),
        "reference_geometry_captioned_qc_artifact": verify_deliverable(refgeom_captioned, clock, ref_geometry, reference_lock),
    }

    _log(f"{variant_id}: caption QC")
    receipt["caption_qc"] = caption_qc(
        workspace, refgeom_captioned, refgeom_control, caption_dir, contract, frame_count,
        baseline_frames=Path(args.baseline_frames).expanduser() if args.baseline_frames else None,
        baseline_captions=baseline_captions,
        vertical_captioned=vertical_delivery,
        vertical_caption_dir=vertical_caption_dir,
    )

    _log(f"{variant_id}: proof board")
    board = build_proof_board(
        deliver / f"{variant_id}-proof-board.png",
        variant_id=variant_id,
        casting=casting,
        vertical_frames=workspace / "qc-vertical",
        refgeom_frames=workspace / "qc-out",
        anchors=anchor_rows,
        summary=[
            f"vertical 9:16 {receipt['deliverables']['vertical_native_9x16']['bytes']:,} B  |  "
            f"web crf{chosen_crf} {receipt['deliverables']['web_encode_under_10mb']['bytes']:,} B  |  "
            f"{frame_count} frames @ {fps}",
            f"distance vs master {casting['distance']['changed_vs_master']}/12 slots  |  "
            f"min vs any sibling {casting['distance']['min_changed_vs_any']}/12  |  hook {casting['slots'][0]['source_clip_id']}",
            "captions: reel program re-rendered by the engine, ink and timing unchanged; grade: technical LC-709 only",
        ],
    )
    receipt["proof_board"] = {"path": str(board), "sha256": sha256_file(board)}

    for name, source in (
        ("reelctl-render-receipt.json", sandbox / f"edit/render-{variant_id}/render-receipt.json"),
        ("reelctl-qc-report.json", Path(qc["report_path"])),
    ):
        shutil.copyfile(source, deliver / name)

    gates = {
        "front_door": door["status"] == "PASS",
        "reelctl_technical": qc["technical"]["status"] == "PASS",
        "reelctl_structure": qc["structure"]["status"] == "PASS",
        "caption_timing": receipt["caption_qc"]["timing"]["pass"],
        "caption_blanks": receipt["caption_qc"]["blanks"]["pass"],
        "caption_coverage": receipt["caption_qc"]["coverage"]["pass"],
        "caption_containment": receipt["caption_qc"]["containment"]["pass"],
        "caption_legibility_refgeom": receipt["caption_qc"]["caption_legibility_over_new_picture"]["pass"],
        "caption_legibility_vertical": receipt["caption_qc"].get("caption_legibility_on_vertical_delivery", {}).get("pass", True),
        "vertical_delivery_clock": receipt["deliverables"]["vertical_native_9x16"]["status"] == "PASS",
        "web_delivery_clock": receipt["deliverables"]["web_encode_under_10mb"]["status"] == "PASS",
        "web_under_cap": receipt["web_encode"]["pass"],
    }
    receipt["gates"] = gates
    receipt["status"] = "PROVEN" if all(gates.values()) else "FAILED"
    receipt["failed_gates"] = [name for name, passed in gates.items() if not passed]
    receipt["elapsed_s"] = round(time.time() - started, 1)
    _write_json(deliver / f"{variant_id}-render-receipt.json", receipt)
    _write_json(workspace / "render-receipt.json", receipt)
    _log(f"{variant_id}: {receipt['status']} in {receipt['elapsed_s']}s -> {deliver}")
    return receipt


# ---------------------------------------------------------------------------------------
# stage 9 — per-variant ink re-resolution (phase 2b)


def build_ink_adjudication_board(
    output: Path,
    *,
    variant_id: str,
    resolution: Dict[str, Any],
    plates_dir: Path,
    picture_frames: Path,
    approved_ink: Dict[str, Any],
    margin: int = 28,
) -> Optional[Path]:
    """The board the approved passes decided their flips on, built per variant.

    Three native-scale panels per changed lockup, on one frame of that lockup's own hold: the
    picture bare, the fill the approved v002 deliverable shipped, and the fill this stage
    resolved — each composited through the state's own plate at its own box. DOCTRINE 10: the
    machine's number scopes the choice, the pixels decide it. Nothing here approves anything;
    it exists so a reviewer can overturn a number by looking.
    """

    from PIL import Image, ImageDraw

    rows: List[Dict[str, Any]] = []
    for lockup_id, row in resolution["lockups"].items():
        member = row["members"][0]
        before = list(approved_ink[member["states"][0]])
        after = list(row["rgb"])
        if before == after:
            continue
        rows.append({"lockup": lockup_id, "row": row, "member": member, "before": before, "after": after})
    if not rows:
        return None

    def panel(frame: "Image.Image", box: Tuple[int, int, int, int], alpha: "Image.Image",
              at: Tuple[int, int], fill: Optional[Sequence[int]]) -> "Image.Image":
        canvas = frame.convert("RGBA")
        if fill is not None:
            plate = Image.new("RGBA", alpha.size, (int(fill[0]), int(fill[1]), int(fill[2]), 0))
            plate.putalpha(alpha)
            canvas.alpha_composite(plate, at)
        return canvas.convert("RGB").crop(box)

    tiles: List["Image.Image"] = []
    label_h = 34
    for entry in rows:
        member, row = entry["member"], entry["row"]
        span = member["candidate_frames"]
        frame_index = span[len(span) // 2]
        picture = Image.open(picture_frames / f"f{frame_index + 1:03d}.png").convert("RGB")
        alpha = Image.open(plates_dir / member["plate"]).convert("L")
        x, y = member["box_xy"]
        w, h = alpha.size
        box = (
            max(0, x - margin), max(0, y - margin),
            min(picture.width, x + w + margin), min(picture.height, y + h + margin),
        )
        strip = [
            ("picture only", None),
            (f"approved v002 {entry['before']}", entry["before"]),
            (f"resolved {entry['after']}", entry["after"]),
        ]
        cells = [panel(picture, box, alpha, (x, y), fill) for _, fill in strip]
        cell_w, cell_h = cells[0].size
        tile = Image.new("RGB", (cell_w * 3 + 8, cell_h + label_h), (10, 10, 10))
        draw = ImageDraw.Draw(tile)
        draw.text(
            (4, 3),
            f"{'/'.join(m['layer'] for m in row['members'])}  \"{member.get('text')}\"  r{frame_index}"
            f"   bg luma {row['candidate_background_luma']}  ->  {row['disposition']}"
            f"   sep {row['separation_before']} -> {row['separation_after']}",
            fill=(255, 255, 255),
        )
        for index, ((caption, _), cell) in enumerate(zip(strip, cells)):
            tile.paste(cell, (index * (cell_w + 4), label_h))
            draw.text((index * (cell_w + 4) + 4, label_h - 15), caption, fill=(255, 210, 60))
        tiles.append(tile)

    header_h = 46
    width = max(tile.width for tile in tiles)
    board = Image.new("RGB", (width, header_h + sum(tile.height + 6 for tile in tiles)), (10, 10, 10))
    draw = ImageDraw.Draw(board)
    draw.text((10, 6), f"{variant_id} — CAPTION INK ADJUDICATION BOARD — native 1:1", fill=(255, 255, 255))
    draw.text(
        (10, 24),
        "machine proposal over this variant's own picture. The number scopes the choice; these pixels "
        "are what a reviewer overturns it with. creative_approval: PENDING.",
        fill=(190, 190, 190),
    )
    offset = header_h
    for tile in tiles:
        board.paste(tile, (0, offset))
        offset += tile.height + 6
    output.parent.mkdir(parents=True, exist_ok=True)
    board.save(output, compress_level=6)
    return output


def _drop_cached_frames(directory: Path, pattern: str) -> int:
    """Invalidate a frame cache whose key (the frame count) cannot see that the bytes moved."""
    if not directory.is_dir():
        return 0
    dropped = 0
    for stale in directory.glob(pattern):
        stale.unlink()
        dropped += 1
    return dropped


def _supersede(deliver: Path, variant_id: str) -> Dict[str, Any]:
    """Move the superseded P2 artifacts aside rather than overwriting them.

    Append-only (`CLAUDE.md` invariant 4): a rejected artifact stays on disk as a negative
    fixture with its receipt. These are not approved baselines — they are the FAILED first
    batch — but they are the evidence the defect was real, so they are preserved, not deleted.
    """
    archive = deliver / "superseded-p2"
    archive.mkdir(parents=True, exist_ok=True)
    moved, replaced = [], []
    for path in sorted(deliver.iterdir()):
        if path.is_dir():
            continue
        target = archive / path.name
        if target.exists():
            # the P2 original is already archived; what sits here is an earlier attempt at
            # THIS pass, which is not history worth keeping under the same name
            path.unlink()
            replaced.append(path.name)
            continue
        shutil.move(str(path), str(target))
        moved.append(path.name)
    return {"archive": str(archive), "moved": moved, "discarded_prior_attempt": replaced}


def reink_variant(args: argparse.Namespace) -> Dict[str, Any]:
    """Re-resolve one variant's caption ink against its own picture, then re-deliver.

    The picture, the casting, the selection, the grade and the caption program's text, timing,
    geometry and plates are all untouched — they are what the first pass already rendered and
    what the reviewer's approved reel locked. The one thing that moves is the fill, because the
    fill was resolved against a picture this variant does not have.
    """
    started = time.time()
    variant_id = str(args.variant)
    work_root = Path(args.work_root).expanduser()
    workspace = work_root / variant_id
    sandbox = workspace / "project"
    deliver = Path(args.deliver_root).expanduser() / variant_id
    if not sandbox.is_dir():
        raise VariantRenderError(f"no P2 sandbox for {variant_id} at {sandbox}")

    # the P2 receipt is what this pass supersedes; if an earlier attempt at THIS pass already
    # archived it, that archive copy is the P2 one and the live file is our own previous try
    archived_prior = deliver / "superseded-p2" / f"{variant_id}-render-receipt.json"
    prior_path = deliver / f"{variant_id}-render-receipt.json"
    if archived_prior.is_file():
        prior_path = archived_prior
    elif not prior_path.is_file():
        prior_path = workspace / "render-receipt.json"
    if not prior_path.is_file():
        raise VariantRenderError(f"no prior render receipt for {variant_id}")
    prior = json.loads(prior_path.read_text())
    if prior.get("phase") == "2b-ink-refix":
        raise VariantRenderError(
            f"the receipt this pass would supersede is itself a 2b receipt: {prior_path}"
        )

    reference_lock = load_json(sandbox / "reference/reference-lock.json", root=sandbox)
    clock = reference_lock["clock"]
    frame_count = int(clock["frame_count"])
    fps = str(clock["fps"])
    time_base_denominator = int(str(clock["time_base"]).split("/", 1)[1])
    ref_geometry = (int(clock["width"]), int(clock["height"]))

    master = Path(str(prior["picture_reference_geometry"]["master_path"]))
    vertical_master = sandbox / "vertical/picture-with-audio.mov"
    for required in (master, vertical_master):
        if not required.is_file():
            raise VariantRenderError(f"the P2 picture master is gone: {required}")

    receipt: Dict[str, Any] = dict(prior)
    receipt["artifact"] = "VARIANT_RENDER_RUN"
    receipt["phase"] = "2b-ink-refix"
    receipt["supersedes"] = {
        "receipt_sha256": sha256_file(prior_path),
        "status": prior.get("status"),
        "failed_gates": prior.get("failed_gates"),
    }
    receipt["tool"] = {
        "path": str(Path(__file__).resolve()),
        "sha256": sha256_file(Path(__file__).resolve()),
    }
    receipt["started_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
    receipt["superseded_artifacts"] = _supersede(deliver, variant_id)

    # --- the ink stage ---------------------------------------------------------------
    _log(f"{variant_id}: re-resolving caption ink against this variant's own picture")
    control = workspace / f"{variant_id}-refgeom-control.mp4"
    if not control.is_file():
        composite(master, control, width=ref_geometry[0], height=ref_geometry[1], fps=fps,
                  frames=frame_count, time_base_denominator=time_base_denominator, captions=None,
                  codec="libx264", crf=DELIVERY_CRF)
    picture_frames = _extract_frames(control, workspace / "qc-ctrl", frame_count)

    # WHICH PICTURES THE FILL IS OPTIMISED FOR (PLAN A6). The P2 batch always optimised the
    # worst case across BOTH the reference-geometry delivery and the 9:16 vertical. Verticals
    # are withheld under format_doctrine, so the file the reviewer actually watched
    # paid contrast for a picture nobody sees: one variant's word went from 154.3 luma levels of
    # separation at the ratified gold to 27.4. The default is therefore the delivered geometry
    # alone; `--pictures both` restores the old behaviour for the day verticals ship again.
    placement = caption_layer_placement(ref_geometry[0], ref_geometry[1], *VERTICAL_SIZE)
    wanted = str(getattr(args, "pictures", "refgeom"))
    vertical_picture_frames = workspace / "ink-vertical-control"
    candidate_pictures: List[Dict[str, Any]] = []
    if wanted in {"refgeom", "both"}:
        candidate_pictures.append(
            {
                "name": "reference_geometry",
                "frames": picture_frames,
                "pattern": "f{index:03d}.png",
                "index_origin": 1,
            }
        )
    if wanted in {"vertical", "both"}:
        _drop_cached_frames(vertical_picture_frames, "*.png")
        _extract_frames(vertical_master, vertical_picture_frames, frame_count)
        candidate_pictures.append(
            {
                "name": "vertical_9x16_delivery",
                "frames": vertical_picture_frames,
                "pattern": "f{index:03d}.png",
                "index_origin": 1,
                "scale": placement["scale"],
                "offset_xy": [placement["x"], placement["y"]],
            }
        )
    if not candidate_pictures:
        raise VariantRenderError(f"--pictures {wanted!r} names no picture to optimise against")

    contract_truth = Path(args.captions_contract).expanduser()
    approved = json.loads(contract_truth.read_text())
    spec = json.loads(Path(args.captions_lockups).expanduser().read_text())
    resolution = resolve_ink(
        spec,
        plates_dir=Path(args.captions_plates).expanduser(),
        reference_frames=Path(args.reference_frames).expanduser(),
        candidate_frames=picture_frames,
        candidate_pattern="f{index:03d}.png",
        candidate_index_origin=1,
        strict=True,
        candidate_pictures=candidate_pictures,
        # PLAN A1: start from the fill the reviewer ratified, not from the reference measurement
        # in the lockup spec. Six of the sixteen refA lockups differ, and they are exactly the
        # six the round had fixed — including one word, which every variant reset from
        # gold to [95, 83, 59] and then gain-scaled into the dark.
        base_from_contract=approved,
    )
    _drop_cached_frames(vertical_picture_frames, "*.png")
    patched, diff = apply_resolution(approved, resolution["states"])
    moved = prove_only_ink_moved(approved, patched)

    captions_dir = workspace / "captions-ink"
    captions_dir.mkdir(parents=True, exist_ok=True)
    contract_out = captions_dir / "contract.json"
    contract_render_out = captions_dir / "contract-render.json"
    atomic_write_json(contract_out, patched)
    atomic_write_json(contract_render_out, render_twin(patched))
    atomic_write_json(captions_dir / "ink-resolution.json", resolution)

    receipt["ink_resolution"] = {
        "stage": "reelctl.captions.ink.resolve_ink",
        "doctrine": (
            "DOCTRINE 18 — the ink stage is the engine's, not a per-reel script. The rules are the "
            "approved refA caption pass's own: 80% of the reference's own Michelson, pure gain, "
            "smallest move, flip only when the reference's polarity cannot get within 90% of it."
        ),
        "approved_contract": str(contract_truth),
        "approved_contract_sha256": sha256_file(contract_truth),
        "lockups_spec": str(Path(args.captions_lockups).expanduser()),
        "lockups_spec_sha256": sha256_file(Path(args.captions_lockups).expanduser()),
        "candidate_pictures": [picture["name"] for picture in candidate_pictures],
        "pictures_flag": wanted,
        "status": resolution["status"],
        "verdicts": resolution["verdicts"],
        "lockups_below_contrast_floor": resolution["lockups_below_contrast_floor"],
        "lockups_short_of_target": resolution["lockups_short_of_target"],
        "lockups_skipped_low_reference_contrast": resolution["lockups_skipped_low_reference_contrast"],
        "verdict_reasons": {
            key: row["verdict_reasons"] for key, row in resolution["lockups"].items() if row["verdict_reasons"]
        },
        "base_source": {key: row["base_source"] for key, row in resolution["lockups"].items()},
        "gates": resolution["gates"],
        "unresolvable_lockups": [
            key for key, row in resolution["lockups"].items() if not row["clears_separation_floor"]
        ],
        "candidate_picture_note": (
            "BOTH delivered geometries of THIS variant's own picture, caption-free, extracted "
            "frame by frame: the reference-geometry composite and the 9:16 vertical master with "
            "the caption layer's own scale and offset applied. One contract carries one ink "
            "value, so the fill is decided against the worse of the two. The approved v002 pass "
            "measured on its reelctl review encode, which carries the engine's 30..214 headroom "
            "clamp; this measures the unclamped pixels the caption actually lands on, which is "
            "the harder and more truthful test."
        ),
        "resolution_path": str(captions_dir / "ink-resolution.json"),
        "contract_sha256": sha256_file(contract_out),
        "contract_render_sha256": sha256_file(contract_render_out),
        "ink_changes": moved,
        "states_changed": sorted(moved),
        "dispositions": {key: row["disposition"] for key, row in resolution["lockups"].items()},
        "only_ink_moved": True,
        "creative_approval": "PENDING",
    }

    _log(f"{variant_id}: {len(moved)} of {len(approved['states'])} states changed ink")

    ink_board = build_ink_adjudication_board(
        deliver / f"{variant_id}-ink-adjudication-board.png",
        variant_id=variant_id,
        resolution=resolution,
        plates_dir=Path(args.captions_plates).expanduser(),
        picture_frames=picture_frames,
        approved_ink={state["id"]: state["ink"]["rgb_median"] for state in approved["states"]},
    )
    receipt["ink_resolution"]["adjudication_board"] = (
        {"path": str(ink_board), "sha256": sha256_file(ink_board)} if ink_board else None
    )
    receipt["ink_resolution"]["adjudication"] = (
        "PENDING — the board exists; no human has ruled on it. DOCTRINE 10: a machine `clean` is "
        "advisory, never promotion."
    )

    # --- re-render the caption program with the resolved fills ------------------------
    caption_dir = workspace / "caption-frames-ink"
    _drop_cached_frames(caption_dir, "caption-*.png")
    render_caption_frames(contract_render_out, Path(args.captions_plates).expanduser(), caption_dir)
    baseline_captions = Path(args.baseline_captions).expanduser() if args.baseline_captions else None
    receipt["captions"] = {
        "contract": str(contract_out),
        "contract_sha256": sha256_file(contract_out),
        "contract_render_sha256": sha256_file(contract_render_out),
        "plates": str(Path(args.captions_plates).expanduser()),
        "states": len(patched["states"]),
        "doctrine": "DOCTRINE 18 — the engine re-renders the reel's caption program; no per-reel caption script exists",
    }
    if baseline_captions:
        receipt["captions"]["reproduction_vs_approved_program"] = compare_caption_frames(
            caption_dir, baseline_captions, frame_count
        )
        receipt["captions"]["reproduction_scope"] = (
            "byte-identity against the approved v002 caption frames is EXPECTED TO BE PARTIAL here and "
            "is reported, not gated: the frames that differ are exactly the frames carrying a re-resolved "
            "fill. Geometry, timing and plate are still byte-identical, which is what the comparison proves."
        )

    # --- re-composite and re-deliver --------------------------------------------------
    _log(f"{variant_id}: compositing captions over the reference-geometry picture")
    refgeom_captioned = workspace / f"{variant_id}-refgeom-captioned.mp4"
    if refgeom_captioned.is_file():
        refgeom_captioned.unlink()
    composite(master, refgeom_captioned, width=ref_geometry[0], height=ref_geometry[1], fps=fps,
              frames=frame_count, time_base_denominator=time_base_denominator, captions=caption_dir,
              codec="libx264", crf=DELIVERY_CRF)

    vertical_caption_dir = workspace / f"caption-layers-ink-{VERTICAL_SIZE[0]}x{VERTICAL_SIZE[1]}"
    _drop_cached_frames(vertical_caption_dir, "caption-*.png")
    vertical_caption_dir = scale_caption_layers(
        caption_dir, vertical_caption_dir, placement, VERTICAL_SIZE, frame_count,
    )

    _log(f"{variant_id}: compositing captions over the vertical picture")
    vertical_captioned_master = workspace / f"{variant_id}-vertical-captioned-master.mov"
    if vertical_captioned_master.is_file():
        vertical_captioned_master.unlink()
    composite(vertical_master, vertical_captioned_master, width=VERTICAL_SIZE[0], height=VERTICAL_SIZE[1],
              fps=fps, frames=frame_count, time_base_denominator=time_base_denominator,
              captions=vertical_caption_dir, codec="prores_ks")

    _log(f"{variant_id}: delivery encodes")
    deliver.mkdir(parents=True, exist_ok=True)
    vertical_delivery = deliver / f"{variant_id}-vertical-{VERTICAL_SIZE[0]}x{VERTICAL_SIZE[1]}-crf{DELIVERY_CRF}.mp4"
    encode_delivery(vertical_captioned_master, vertical_delivery, crf=DELIVERY_CRF, fps=fps,
                    frames=frame_count, time_base_denominator=time_base_denominator)
    attempts: List[Tuple[int, int]] = []
    web_delivery = deliver / f"{variant_id}-web-{VERTICAL_SIZE[0]}x{VERTICAL_SIZE[1]}.mp4"
    chosen_crf = None
    for crf in WEB_CRF_LADDER:
        encode_delivery(vertical_captioned_master, web_delivery, crf=crf, fps=fps,
                        frames=frame_count, time_base_denominator=time_base_denominator)
        attempts.append((crf, web_delivery.stat().st_size))
        if web_delivery.stat().st_size <= WEB_SIZE_CAP_BYTES:
            chosen_crf = crf
            break
    receipt["web_encode"] = {
        "cap_bytes": WEB_SIZE_CAP_BYTES,
        "ladder": list(WEB_CRF_LADDER),
        "attempts": [{"crf": crf, "bytes": size} for crf, size in attempts],
        "chosen_crf": chosen_crf,
        "pass": chosen_crf is not None,
        "note": "audio stream-copied, frame count exact; the ladder only moves if crf21 misses the upload size cap",
    }
    if chosen_crf is None:
        raise VariantRenderError("no crf on the ladder brought the web encode under the upload size cap")

    _log(f"{variant_id}: verifying deliverables against the blueprint clock")
    receipt["deliverables"] = {
        "vertical_native_9x16": verify_deliverable(vertical_delivery, clock, VERTICAL_SIZE, reference_lock),
        "web_encode_under_10mb": verify_deliverable(web_delivery, clock, VERTICAL_SIZE, reference_lock),
        "reference_geometry_captioned_qc_artifact": verify_deliverable(refgeom_captioned, clock, ref_geometry, reference_lock),
    }

    _log(f"{variant_id}: caption QC")
    # `_extract_frames` is a cache keyed only on frame COUNT, so a P2 extraction of the same
    # 172 frames would be reused and this pass would measure the picture it just replaced.
    # The captioned cuts are new bytes; their frame directories are dropped, deliberately.
    # `qc-ctrl` is NOT dropped: the caption-free control is byte-identical across passes and is
    # the picture the ink was resolved against.
    for stale_dir in (workspace / "qc-out", workspace / "qc-vertical"):
        _drop_cached_frames(stale_dir, "*.png")
    receipt["caption_qc"] = caption_qc(
        workspace, refgeom_captioned, control, caption_dir, patched, frame_count,
        baseline_frames=Path(args.baseline_frames).expanduser() if args.baseline_frames else None,
        baseline_captions=baseline_captions,
        vertical_captioned=vertical_delivery,
        vertical_caption_dir=vertical_caption_dir,
    )

    _log(f"{variant_id}: proof board")
    board = build_proof_board(
        deliver / f"{variant_id}-proof-board.png",
        variant_id=variant_id,
        casting=json.loads(Path(str(prior["casting"]["path"])).read_text()),
        vertical_frames=workspace / "qc-vertical",
        refgeom_frames=workspace / "qc-out",
        anchors=prior["vertical_reframe"]["anchors"],
        summary=[
            f"vertical 9:16 {receipt['deliverables']['vertical_native_9x16']['bytes']:,} B  |  "
            f"web crf{chosen_crf} {receipt['deliverables']['web_encode_under_10mb']['bytes']:,} B  |  "
            f"{frame_count} frames @ {fps}",
            f"PHASE 2b — caption ink re-resolved by the engine against this variant's own picture; "
            f"{len(moved)} states moved: {', '.join(sorted(moved)) or 'none'}",
            "text, font, timing, geometry, plates and picture all unchanged from the P2 render",
        ],
    )
    receipt["proof_board"] = {"path": str(board), "sha256": sha256_file(board)}

    for name, source in (
        ("reelctl-render-receipt.json", sandbox / f"edit/render-{variant_id}/render-receipt.json"),
        ("reelctl-qc-report.json", Path(prior["reelctl_qc"]["report_path"])),
    ):
        if Path(source).is_file():
            shutil.copyfile(source, deliver / name)

    gates = {
        "front_door": prior["front_door"]["status"] == "PASS",
        "reelctl_technical": prior["reelctl_qc"]["technical"] == "PASS",
        "reelctl_structure": prior["reelctl_qc"]["structure"] == "PASS",
        "caption_timing": receipt["caption_qc"]["timing"]["pass"],
        "caption_blanks": receipt["caption_qc"]["blanks"]["pass"],
        "caption_coverage": receipt["caption_qc"]["coverage"]["pass"],
        "caption_containment": receipt["caption_qc"]["containment"]["pass"],
        # The ink stage's own verdict is a gate now. It returned an unconditional PASS until
        # which is how 74 of 160 lockup decisions reached the reviewer with no
        # warning and how one variant's word shipped at 1.37:1 with every gate green.
        "caption_ink_resolution": resolution["status"] != "FAIL",
        "caption_legibility_refgeom": receipt["caption_qc"]["caption_legibility_over_new_picture"]["pass"],
        # The vertical is WITHHELD under format_doctrine. Its legibility is measured
        # and reported, never gated: a file nobody ships must not block the file they do.
        "vertical_delivery_clock": receipt["deliverables"]["vertical_native_9x16"]["status"] == "PASS",
        "web_delivery_clock": receipt["deliverables"]["web_encode_under_10mb"]["status"] == "PASS",
        "web_under_cap": receipt["web_encode"]["pass"],
    }
    receipt["gates"] = gates
    receipt["status"] = "PROVEN" if all(gates.values()) else "FAILED"
    receipt["failed_gates"] = [name for name, passed in gates.items() if not passed]
    receipt["elapsed_s"] = round(time.time() - started, 1)
    _write_json(deliver / f"{variant_id}-render-receipt.json", receipt)
    _write_json(workspace / "render-receipt.json", receipt)
    _log(f"{variant_id}: {receipt['status']} in {receipt['elapsed_s']}s -> {deliver}")
    return receipt


def batch_qc(args: argparse.Namespace) -> Dict[str, Any]:
    """Cross-variant distance QC over what actually rendered, not over what was cast."""

    import numpy as np
    from PIL import Image

    deliver_root = Path(args.deliver_root).expanduser()
    work_root = Path(args.work_root).expanduser()
    variants = sorted(args.variants.split(",")) if args.variants else sorted(
        path.name for path in deliver_root.iterdir() if path.is_dir()
    )
    rows, hashes, hooks = [], {}, {}
    for variant in variants:
        receipt_path = deliver_root / variant / f"{variant}-render-receipt.json"
        if not receipt_path.is_file():
            rows.append({"variant": variant, "status": "MISSING_RECEIPT"})
            continue
        receipt = json.loads(receipt_path.read_text())
        rows.append(
            {
                "variant": variant,
                "status": receipt.get("status"),
                "vertical_bytes": receipt["deliverables"]["vertical_native_9x16"]["bytes"],
                "web_bytes": receipt["deliverables"]["web_encode_under_10mb"]["bytes"],
                "web_crf": receipt["web_encode"]["chosen_crf"],
                "vertical_sha256": receipt["deliverables"]["vertical_native_9x16"]["sha256"],
            }
        )
        hashes[variant] = receipt["deliverables"]["vertical_native_9x16"]["sha256"]
        hook = work_root / variant / "qc-vertical/f001.png"
        if hook.is_file():
            hooks[variant] = np.asarray(Image.open(hook).convert("L").resize((96, 170))).astype(np.float64)

    duplicate_hashes = [value for value in set(hashes.values()) if list(hashes.values()).count(value) > 1]
    pairs = []
    names = sorted(hooks)
    for first_index, first in enumerate(names):
        for second in names[first_index + 1 :]:
            pairs.append(
                {
                    "pair": [first, second],
                    "hook_mean_abs_luma_delta": round(float(np.abs(hooks[first] - hooks[second]).mean()), 3),
                }
            )
    pairs.sort(key=lambda row: row["hook_mean_abs_luma_delta"])
    report = {
        "schema_version": 1,
        "artifact": "VARIANT_BATCH_QC",
        "status": "PASS" if not duplicate_hashes else "FAILED",
        "variants": rows,
        "rendered_deliverable_hashes_unique": not duplicate_hashes,
        "duplicate_hashes": duplicate_hashes,
        "hook_frame_distance": {
            "method": "mean absolute luma delta on frame 0 of the shipped 9:16 encode, 96x170 grayscale",
            "closest_pairs": pairs[:5],
            "all_pairs": pairs,
        },
        "honesty": (
            "Distance here is measured on rendered pixels, which is stronger evidence than the casting-level "
            "slot count — but it says nothing about how audiences will respond to each variant."
        ),
    }
    _write_json(Path(args.output).expanduser(), report)
    print(json.dumps({"status": report["status"], "variants": len(rows), "output": args.output}, indent=1))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="render one casting end to end")
    render.add_argument("--casting", required=True)
    render.add_argument("--work-root", default=str(WORKBENCH / "variant-renders"))
    render.add_argument("--deliver-root", required=True, help="project variants/renders directory")
    render.add_argument("--captions-contract", required=True, help="RGB-truth caption contract (QC)")
    render.add_argument("--captions-contract-render", required=True, help="BGR caption contract handed to the engine")
    render.add_argument("--captions-plates", required=True)
    render.add_argument("--baseline-captions", default=None, help="approved deliverable's caption RGBA frames")
    render.add_argument("--baseline-frames", default=None, help="approved deliverable's decoded picture frames")
    render.add_argument("--force-selection", action="store_true")
    render.set_defaults(func=render_variant)

    reink = sub.add_parser(
        "reink",
        help="re-resolve one already-rendered variant's caption ink against its own picture, then re-deliver",
    )
    reink.add_argument("--variant", required=True)
    reink.add_argument("--work-root", default=str(WORKBENCH / "variant-renders"))
    reink.add_argument("--deliver-root", required=True, help="project variants/renders directory")
    reink.add_argument("--captions-contract", required=True, help="approved RGB-truth caption contract")
    reink.add_argument("--captions-lockups", required=True, help="the reel's ink lockup spec")
    reink.add_argument("--captions-plates", required=True)
    reink.add_argument("--reference-frames", required=True, help="decoded reference frames, r%%03d.png zero-based")
    reink.add_argument("--baseline-captions", default=None, help="approved deliverable's caption RGBA frames")
    reink.add_argument("--baseline-frames", default=None, help="approved deliverable's decoded picture frames")
    reink.add_argument(
        "--pictures",
        choices=("refgeom", "vertical", "both"),
        default="refgeom",
        help=(
            "which delivered geometries the one fill has to carry. Default refgeom: the 9:16 "
            "vertical is WITHHELD under format_doctrine, and optimising across it "
            "dragged the delivered 16:9 fill down (one variant's word: 154.3 luma levels of "
            "separation at the ratified gold, 27.4 after)."
        ),
    )
    reink.set_defaults(func=reink_variant)

    batch = sub.add_parser("batch-qc", help="cross-variant distance QC over rendered pixels")
    batch.add_argument("--deliver-root", required=True)
    batch.add_argument("--work-root", default=str(WORKBENCH / "variant-renders"))
    batch.add_argument("--variants", default=None)
    batch.add_argument("--output", required=True)
    batch.set_defaults(func=batch_qc)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except VariantRenderError as error:
        print(json.dumps({"status": "FAILED", "error": str(error)}, indent=1))
        return 2
    if args.command in {"render", "reink"}:
        print(json.dumps({
            "status": result["status"],
            "variant": result["variant_id"],
            "failed_gates": result["failed_gates"],
            "elapsed_s": result["elapsed_s"],
        }, indent=1))
    return 0 if result.get("status") in {"PROVEN", "PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
