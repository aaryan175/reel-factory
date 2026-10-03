#!/usr/bin/env python3
"""Variant caster — alternate castings for one LOCKED reel blueprint.

Given a reelctl project whose blueprint and
picture selection are already locked and approved, emit N alternate castings of the
SAME 172-frame clock: every slot keeps its frame count, its editorial role and its
place in the cut; only the background clip behind it changes.

What this tool is and is not
----------------------------
It is a *casting* tool. It chooses which authorized clip and which source window
fills each picture slot, under declared per-slot eligibility rules, and writes a
receipt for each choice. It renders nothing, locks nothing into a project's
state.json, and never touches REEL_REGISTRY.json.

It does NOT claim to have watched the footage. A casting emitted here carries
``agent_visual_review: PENDING_MACHINE``; the factory's independent-observation gate
still has to be walked before any casting is locked into a project and rendered.
The library tag record quoted in ``candidate_observation`` is another agent's
still-triaged observation, transcribed, not this tool's own eyes.

Gates enforced here (all of them refuse rather than warn)
--------------------------------------------------------
front door   a clip is eligible only if it is a PASS member of the parent project's
             own footage-index AND lives under ``masters/`` in the authorized footage
             root. Proxies are indexed but are not render sources; research folders
             and anything outside the index are invisible to this tool.
blacklist    SHOT_BLACKLIST.json exact-clip bans, matched on every identifier the
             entry carries: the schema-v2 ``match_keys`` contract plus the v1
             mirrors (content id, storage file id, sha256, master file name). There
             is deliberately no descriptor rule.
role/world   per-slot allowed library roles, lighting families and subject classes,
             read from a policy file so the reviewer can retune them without editing
             code. An empty pool is reported as a finding, never silently widened.
clock        a clip is eligible for a slot only if it carries enough source frames
             for that slot's window at the output clock, using reelctl's own
             ceil(frames * source_fps / output_fps) conform arithmetic.
distance     any two castings in the family differ in at least ``min_changed_ratio``
             of their slots, and the hook slot's clip is unique per variant and never
             the approved master's hook clip.
reuse        no clip fills two slots inside one casting (reelctl's own selection rule
             for blueprints without a declared repeat_group).

Determinism: identical inputs plus identical ``--seed`` produce byte-identical
castings. Randomness is drawn from a hash of (seed, family, variant, slot, attempt),
never from iteration order or wall clock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

# Source windows are taken at these quantiles of a clip's usable range. Index n is
# used the nth time a clip is cast into the same slot across the family, so a clip
# forced to repeat by a scarce pool at least shows a different moment of itself.
WINDOW_QUANTILES = (0.10, 0.38, 0.66, 0.88)


class CasterError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# deterministic randomness


def _rng(*parts: Any) -> random.Random:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:8], "big"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(payload: Any) -> str:
    return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


# ---------------------------------------------------------------------------
# inputs


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _name_forms(name: str) -> set:
    """Both the master's stored file name and the camera-original name inside it.

    Masters are stored as ``<content-prefix>__<camera-name>``. A blacklist entry may
    quote either form; both must ban the same clip. Library and index basenames were
    both verified unique, so the short form cannot over-ban.
    """
    base = os.path.basename(str(name))
    forms = {base}
    if "__" in base:
        forms.add(base.split("__", 1)[1])
    # Extension-less stems too: schema v2 records the camera stem (`CLIP_0010`) as a
    # first-class identifier, and the two master naming styles on disk
    # (`<16hex>__CAM_0001.MP4` and `<DriveId>__CAM_0001.MP4`) share only that stem.
    for form in list(forms):
        if "." in form:
            forms.add(form.rsplit(".", 1)[0])
    return forms


_ID_KEYS = ("clip_id", "id", "content_id", "drive_file_id")
_FILE_KEYS = ("file", "camera_name", "library_path", "v1_file")


def blacklisted_keys(blacklist: Dict[str, Any]) -> Dict[str, set]:
    """Every identifier form the blacklist file may use, so a ban cannot be dodged.

    SHOT_BLACKLIST schema v2 (doctrine-hygiene) collapsed the file's two
    incompatible key schemes into one entry that carries BOTH the library clip name
    and the Drive clip id, and declares ``match_keys`` as the whole matching
    contract. Reading only ``clip_id`` is what let a ban be dodged twice (a reference
    v001->v002 and 29 v001). Every id-shaped and name-shaped field is read here, and
    the v1 shapes still work unchanged, so a pre-v2 file loads with no behaviour
    change.
    """
    ids, files, hashes = set(), set(), set()

    def absorb(value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        token = value.strip()
        lowered = token.lower()
        if len(token) == 64 and all(c in "0123456789abcdef" for c in lowered):
            hashes.add(token)
        elif "." in os.path.basename(token) or "/" in token:
            files.update(_name_forms(token))
        else:
            # a bare id (content id or Drive file id) or an extension-less camera stem
            ids.add(token)
            files.add(token)

    for entry in blacklist.get("blacklisted_clips", []):
        for key in _ID_KEYS:
            if entry.get(key):
                absorb(str(entry[key]))
        for key in _FILE_KEYS:
            if entry.get(key):
                files |= _name_forms(str(entry[key]))
        for name in entry.get("master_filenames") or []:
            files |= _name_forms(str(name))
        if entry.get("sha256"):
            hashes.add(str(entry["sha256"]))
        for key in entry.get("match_keys") or []:
            absorb(key)
    return {"clip_ids": ids, "files": files, "sha256": hashes}


def library_proxy_root(library: Dict[str, Any]) -> Optional[Path]:
    """Where the library's own proxies live, from the library's own record of it.

    The library declares `source.library_root`; deriving the root from the JSON file's location
    would be a guess, and a wrong guess here silently disables the caption-clearance gate.
    """
    root = ((library.get("source") or {}).get("library_root"))
    return Path(root) if root else None


def build_candidate_table(
    footage_index: Dict[str, Any],
    library: Dict[str, Any],
    blacklist: Dict[str, Any],
    library_root: Optional[Path] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Join the project's authorized inventory to the tagged library.

    The project index is the front door: it is what reelctl will re-check at
    selection-lock time. The library supplies the tags. A clip that is in one but
    not the other is reported, never guessed at.
    """
    if footage_index.get("status") != "PASS":
        raise CasterError("footage index is not PASS; the front door is not open")
    by_basename = {os.path.basename(clip["path"]): clip for clip in library.get("clips", {}).values()}
    bans = blacklisted_keys(blacklist)

    candidates: List[Dict[str, Any]] = []
    stats = {
        "index_clips": len(footage_index.get("clips", [])),
        "index_masters": 0,
        "master_untagged": [],
        "blacklist_rejected": [],
        "library_clips": len(library.get("clips", {})),
    }
    for record in footage_index.get("clips", []):
        if record.get("status", "PASS") != "PASS":
            continue
        relative = str(record.get("relative_path", ""))
        if not relative.startswith("masters/"):
            continue
        stats["index_masters"] += 1
        basename = relative.split("__", 1)[-1]
        if (
            str(record.get("clip_id")) in bans["clip_ids"]
            or str(record.get("sha256")) in bans["sha256"]
            or bool(_name_forms(relative) & bans["files"])
        ):
            stats["blacklist_rejected"].append({"relative_path": relative, "clip_id": record.get("clip_id")})
            continue
        entry = by_basename.get(basename)
        tags = (entry or {}).get("tags") or {}
        if not tags:
            stats["master_untagged"].append(relative)
            continue
        video = record.get("video") or {}
        candidates.append(
            {
                "clip_id": str(record["clip_id"]),
                "source_path": str(record["path"]),
                "relative_path": relative,
                "basename": basename,
                "source_sha256": str(record["sha256"]),
                "source_bytes": int(record["bytes"]),
                "source_frame_count": int(video.get("frame_count") or 0),
                "source_fps": str(video.get("r_frame_rate")),
                "library_id": (entry or {}).get("id"),
                "library_path": (entry or {}).get("path"),
                "duration_s": (entry or {}).get("duration"),
                # The proxy is study/measurement material, never a render source (the front
                # door above already refuses anything outside masters/). The caption-clearance
                # gate reads it; nothing else does.
                "proxy_path": (
                    str(Path(library_root) / (entry or {}).get("proxy"))
                    if library_root and (entry or {}).get("proxy")
                    else None
                ),
                "tags": tags,
            }
        )
    candidates.sort(key=lambda item: item["clip_id"])
    return candidates, stats


# ---------------------------------------------------------------------------
# eligibility


def required_source_frames(frames: int, source_fps: str, output_fps: Fraction) -> int:
    return max(1, math.ceil(frames * float(Fraction(source_fps) / output_fps)))


# ---------------------------------------------------------------------------
# caption clearance
#
# Phase 2 rendered ten variants and every one of them failed caption legibility, because a
# slot's eligibility said nothing about the pixels the reel's own words have to sit on. The
# fill is re-resolved per variant now (`reelctl captions ink`), so the caster no longer has to
# protect a FIXED ink — but a fill is one value per state, and there are windows no single
# value can serve: a word half over a dark region and half over a bright one has no fill
# that clears the floor on both halves, in either polarity. That is a casting question, and
# this is where it belongs.
#
# The band below is not a new threshold. It is the engine's own arithmetic, read backwards.
# A plate of alpha `a` over background `L` delivers `a*F + (1-a)*L` for a fill `F`, so:
#
#   the brightest available fill (F=255) clears the floor iff   a*(255 - L) > MIN_SEPARATION
#   the darkest available fill  (F=0)   clears the floor iff   a*L         > MIN_SEPARATION
#
# which is `L < 255 - MIN_SEPARATION/a` for light ink and `L > MIN_SEPARATION/a` for dark.

CAPTION_MIN_SEPARATION = 25.0  # reelctl.captions.ink.MIN_SEPARATION
CAPTION_LUMA_PERCENTILES = (5, 50, 95)


def caption_servable_band(plate_alpha: float, min_separation: float = CAPTION_MIN_SEPARATION) -> Dict[str, float]:
    """The background luma a palette-legal fill can still be found for, at this plate alpha."""
    if not 0.0 < plate_alpha <= 1.0:
        raise CasterError(f"plate alpha must be within (0, 1]: {plate_alpha}")
    reach = min_separation / plate_alpha
    return {
        "max_for_light_ink": round(255.0 - reach, 2),
        "min_for_dark_ink": round(reach, 2),
        "plate_alpha": round(plate_alpha, 4),
        "min_separation": min_separation,
    }


def caption_clearance_verdict(luma: Dict[str, float], rule: Dict[str, Any]) -> Dict[str, Any]:
    """Can ANY palette-legal fill carry this reel's words over this window?

    The window is judged at its extremes, not at its median. The approved v002 pass learned
    this the hard way on one word: the median said 80 and read healthy while a head at luma 14.8
    sat under the last 38% of the word, and no dark fill could ever clear 25 levels there. So a
    light fill has to clear the BRIGHTEST part of the box and a dark fill the DARKEST part, and
    a window where neither holds is a window this reel's captions cannot be laid over.
    """
    band = caption_servable_band(float(rule.get("plate_alpha", 1.0)))
    high = float(luma["p95"])
    low = float(luma["p05"])
    light_ok = high < band["max_for_light_ink"]
    dark_ok = low > band["min_for_dark_ink"]
    polarities = [name for name, ok in (("light_ink", light_ok), ("dark_ink", dark_ok)) if ok]
    preferred = str(rule.get("reference_polarity") or "")
    return {
        "servable": bool(polarities),
        "polarities_available": polarities,
        "reference_polarity_available": preferred in polarities if preferred else None,
        "band": band,
        "measured": {
            key: (round(float(value), 2) if isinstance(value, (int, float)) else value)
            for key, value in luma.items()
        },
        "reason": (
            "ok"
            if polarities
            else (
                f"no palette-legal fill: the caption box spans luma {low:.1f}..{high:.1f}, so a light "
                f"fill cannot clear the bright end (needs p95 < {band['max_for_light_ink']}) and a dark "
                f"fill cannot clear the dark end (needs p05 > {band['min_for_dark_ink']})"
            )
        ),
    }


def eligible_pool(
    candidates: Sequence[Dict[str, Any]],
    rule: Dict[str, Any],
    frames: int,
    output_fps: Fraction,
    caption_luma: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    roles = set(rule.get("roles") or [])
    lighting = set(rule.get("lighting") or [])
    subjects = set(rule.get("subject") or [])
    worlds = set(rule.get("world_cluster") or [])
    deny_worlds = set(rule.get("deny_world_cluster") or [])
    deny_roles = set(rule.get("deny_roles") or [])
    energies = set(rule.get("energy") or [])
    exclude = set(rule.get("exclude_clip_ids") or [])

    pool = []
    for clip in candidates:
        tags = clip["tags"]
        if clip["clip_id"] in exclude:
            continue
        if roles and not (set(tags.get("roles") or []) & roles):
            continue
        # A denial beats an allow: role tags are coarse (one clip can be tagged both
        # "establishing" and "seated_work"), so the only way to keep a desk wide out of
        # a scale-punch slot is to name the world it may not come from.
        if deny_roles and (set(tags.get("roles") or []) & deny_roles):
            continue
        if lighting and tags.get("lighting") not in lighting:
            continue
        if subjects and tags.get("subject") not in subjects:
            continue
        if worlds and tags.get("world_cluster") not in worlds:
            continue
        if deny_worlds and tags.get("world_cluster") in deny_worlds:
            continue
        if energies and tags.get("energy") not in energies:
            continue
        needed = required_source_frames(frames, clip["source_fps"], output_fps)
        if clip["source_frame_count"] < needed:
            continue
        clearance = rule.get("caption_clearance")
        if clearance and caption_luma is not None:
            verdict = caption_clearance_verdict(caption_luma(clip, clearance), clearance)
            clip["caption_clearance"] = verdict
            if not verdict["servable"]:
                continue
        pool.append(clip)
    return pool


# The two geometries every variant delivers in, and where the caption layer sits on each.
# `variant_render.caption_layer_placement` is the authority for the vertical numbers; they are
# restated here because casting happens before any render exists.
CAPTION_CANVAS = (1916, 1078)
DELIVERY_GEOMETRIES = (
    {"name": "reference_geometry", "canvas": (1916, 1078), "layer_scale": 1.0, "layer_offset": (0, 0)},
    {"name": "vertical_9x16", "canvas": (1080, 1920), "layer_scale": 1080 / 1916, "layer_offset": (0, 656)},
)


def caption_box_on_source(
    box_xyxy: Sequence[int],
    canvas_wh: Sequence[int],
    source_wh: Sequence[int],
    *,
    anchor_x: float = 0.5,
    layer_scale: float = 1.0,
    layer_offset: Sequence[int] = (0, 0),
) -> Tuple[int, int, int, int]:
    """Where a caption box lands on the source frame, through one delivery geometry.

    Two transforms compose. The caption layer is placed on the delivery canvas (identity at
    reference geometry; fitted-by-width and centred on the 9:16 pass). The delivery canvas is
    itself a scale-to-cover crop of the source at some horizontal anchor. Mapping the box back
    through both is what makes this a measurement of the pixels the caption will cover rather
    than of the frame in general.

    `anchor_x` defaults to centre. The 9:16 pass's real anchor is a column-energy measurement
    taken at render time, which does not exist yet when a slot is being cast; the caller is
    responsible for knowing that this is a centre-anchored approximation of that crop.
    """
    canvas_w, canvas_h = int(canvas_wh[0]), int(canvas_wh[1])
    source_w, source_h = int(source_wh[0]), int(source_wh[1])
    if min(canvas_w, canvas_h, source_w, source_h) <= 0:
        raise CasterError("geometry must be positive")
    placed = [
        box_xyxy[0] * layer_scale + layer_offset[0],
        box_xyxy[1] * layer_scale + layer_offset[1],
        box_xyxy[2] * layer_scale + layer_offset[0],
        box_xyxy[3] * layer_scale + layer_offset[1],
    ]
    scale = max(canvas_w / source_w, canvas_h / source_h)
    scaled_w, scaled_h = source_w * scale, source_h * scale
    offset_x = (scaled_w - canvas_w) * float(anchor_x)
    offset_y = (scaled_h - canvas_h) / 2.0
    x0 = int(round((placed[0] + offset_x) / scale))
    y0 = int(round((placed[1] + offset_y) / scale))
    x1 = int(round((placed[2] + offset_x) / scale))
    y1 = int(round((placed[3] + offset_y) / scale))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(source_w, max(x0 + 1, x1)), min(source_h, max(y0 + 1, y1))
    return x0, y0, x1, y1


def _proxy_size(proxy: Path) -> Tuple[int, int]:
    import subprocess

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", str(proxy)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def measure_caption_luma(
    clip: Dict[str, Any],
    clearance: Dict[str, Any],
    *,
    window: Optional[Tuple[int, int]] = None,
    samples: int = 5,
    cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """Luma percentiles inside a slot's caption box, over BOTH delivered geometries.

    Measured on the clip's PROXY, deliberately. The gate asks whether a palette-legal fill can
    be found at all, which is a question about broad luma, and the proxy answers it two orders
    of magnitude cheaper than the 4K master. It is a gate, not the resolver: the resolver
    measures the rendered picture, and the render is what proves the answer.

    `window` restricts the sampling to the source frames this slot will actually use. Omitted,
    the whole clip is sampled — a coarser question, for pool eligibility rather than for a cast.
    """
    import subprocess

    import numpy as np

    key = f"{clip['clip_id']}:{clearance.get('union_bbox_xyxy')}:{window}"
    if cache is not None and key in cache:
        return cache[key]

    proxy = clip.get("proxy_path")
    if not proxy or not Path(proxy).is_file():
        raise CasterError(
            f"no proxy on disk for {clip['basename']}; caption clearance cannot be measured and "
            "is not assumed"
        )
    source_wh = _proxy_size(Path(proxy))
    total = int(clip["source_frame_count"])
    first, last = (window or (0, total))
    first, last = max(0, int(first)), min(total, int(last))
    if last <= first:
        raise CasterError(f"empty sampling window {(first, last)} for {clip['basename']}")
    step = max(1, (last - first) // max(1, samples))
    frames = [first + index * step for index in range(samples) if first + index * step < last] or [first]

    per_geometry: Dict[str, Dict[str, float]] = {}
    for geometry in DELIVERY_GEOMETRIES:
        box = caption_box_on_source(
            clearance["union_bbox_xyxy"], geometry["canvas"], source_wh,
            layer_scale=geometry["layer_scale"], layer_offset=geometry["layer_offset"],
        )
        crop = f"crop={box[2] - box[0]}:{box[3] - box[1]}:{box[0]}:{box[1]}"
        values: List["Any"] = []
        for frame in frames:
            probe = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(proxy),
                 "-vf", f"select=eq(n\\,{frame}),{crop},format=gray", "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                capture_output=True,
            )
            if probe.returncode != 0 or not probe.stdout:
                continue
            values.append(np.frombuffer(probe.stdout, dtype=np.uint8).astype(np.float64))
        if not values:
            raise CasterError(
                f"could not decode any caption-box sample from the proxy for {clip['basename']}"
            )
        stacked = np.concatenate(values)
        per_geometry[geometry["name"]] = {
            f"p{percentile:02d}": round(float(np.percentile(stacked, percentile)), 2)
            for percentile in CAPTION_LUMA_PERCENTILES
        }

    # One fill serves both geometries, so the gate sees the worst of them: the brightest bright
    # end and the darkest dark end across everything the reel delivers.
    measured = {
        "p05": min(row["p05"] for row in per_geometry.values()),
        "p50": float(np.mean([row["p50"] for row in per_geometry.values()])),
        "p95": max(row["p95"] for row in per_geometry.values()),
        "per_geometry": per_geometry,
        "frames_sampled": len(frames),
        "window": [first, last],
        "measured_on": "proxy",
        "vertical_anchor": "centre — the render's column-energy anchor does not exist yet at casting time",
    }
    if cache is not None:
        cache[key] = measured
    return measured


def window_start(clip: Dict[str, Any], frames: int, output_fps: Fraction, repeat_index: int) -> Tuple[int, int, float]:
    needed = required_source_frames(frames, clip["source_fps"], output_fps)
    usable = max(0, clip["source_frame_count"] - needed)
    quantile = WINDOW_QUANTILES[repeat_index % len(WINDOW_QUANTILES)]
    start = int(round(usable * quantile))
    start = max(0, min(start, usable))
    return start, needed, quantile


# ---------------------------------------------------------------------------
# casting


def observation_text(clip: Dict[str, Any], start: int, needed: int) -> str:
    """A transcription, explicitly labelled as one. Never a claim of having watched."""
    tags = clip["tags"]
    notes = (tags.get("notes") or "").strip()
    verbs = ", ".join(tags.get("action_verbs") or []) or "no action verbs tagged"
    roles = ", ".join(tags.get("roles") or []) or "no roles tagged"
    return (
        f"MACHINE-CAST, NOT INDEPENDENTLY WATCHED BY THIS TOOL. Transcribed from the "
        f"FOOTAGE_LIBRARY tag record for {clip['basename']} (library id {clip['library_id']}, "
        f"evidence tier {tags.get('evidence_tier', 'UNKNOWN')}, tagger confidence "
        f"{tags.get('confidence', 'unknown')}): {notes} Tagged world {tags.get('world_cluster')}, "
        f"roles {roles}, lighting {tags.get('lighting')}, subject {tags.get('subject')}, "
        f"energy {tags.get('energy')}, 9:16 crop safety {tags.get('crop_safety_916')}, "
        f"action verbs: {verbs}. Window cast at source frames [{start}, {start + needed}) of "
        f"{clip['source_frame_count']} at {clip['source_fps']}. The window's own content, its "
        f"caption clearance and its grade recoverability are unverified until an agent watches "
        f"this exact window at normal speed."
    )


def cast_slot(
    slot: Dict[str, Any],
    clip: Dict[str, Any],
    output_fps: Fraction,
    repeat_index: int,
    pool_size: int,
    master_clip_id: Optional[str],
) -> Dict[str, Any]:
    start, needed, quantile = window_start(clip, slot["frames"], output_fps, repeat_index)
    return {
        "block_id": slot["block_id"],
        "frames": slot["frames"],
        "reference_role": slot["reference_role"],
        "source_path": clip["source_path"],
        "source_sha256": clip["source_sha256"],
        "source_clip_id": clip["clip_id"],
        "source_bytes": clip["source_bytes"],
        "source_frame_count": clip["source_frame_count"],
        "source_fps": clip["source_fps"],
        "source_start_frame": start,
        "source_end_frame_exclusive": start + needed,
        "required_source_frames": needed,
        "window_quantile": quantile,
        "window_repeat_index": repeat_index,
        "speed": 1.0,
        "reverse": False,
        "crop_anchor_xy": [0.5, 0.5],
        "library_id": clip["library_id"],
        "library_path": clip["library_path"],
        "tags": clip["tags"],
        "candidate_observation": observation_text(clip, start, needed),
        "agent_visual_review": "PENDING_MACHINE",
        "pool_size": pool_size,
        "differs_from_master": master_clip_id is not None and clip["clip_id"] != master_clip_id,
    }


def changed_slot_count(left: Sequence[str], right: Sequence[str]) -> int:
    return sum(1 for a, b in zip(left, right) if a != b)


def cast_family(
    slots: Sequence[Dict[str, Any]],
    pools: Dict[str, List[Dict[str, Any]]],
    master_ids: Dict[str, str],
    *,
    variants: int,
    seed: int,
    family_id: str,
    output_fps: Fraction,
    hook_slot: str,
    min_changed_ratio: float,
    max_attempts: int = 400,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    slot_ids = [slot["block_id"] for slot in slots]
    master_vector = [master_ids[slot_id] for slot_id in slot_ids]
    min_changed = math.ceil(min_changed_ratio * len(slot_ids) - 1e-9)

    # Scarce slots are filled first: the tightest constraint decides the casting.
    order = sorted(slot_ids, key=lambda slot_id: (len(pools[slot_id]), slot_id))
    usage: Dict[str, Dict[str, int]] = {slot_id: {} for slot_id in slot_ids}
    used_hooks = {master_ids[hook_slot]}

    accepted: List[Dict[str, Any]] = []
    accepted_vectors: List[List[str]] = []
    attempts_log: List[Dict[str, Any]] = []

    for index in range(1, variants + 1):
        variant_id = f"var{index:02d}"
        chosen: Optional[Dict[str, str]] = None
        for attempt in range(max_attempts):
            picked: Dict[str, str] = {}
            taken: set = set()
            ok = True
            for slot_id in order:
                pool = pools[slot_id]
                options = [clip for clip in pool if clip["clip_id"] not in taken]
                if slot_id == hook_slot:
                    fresh = [clip for clip in options if clip["clip_id"] not in used_hooks]
                    if fresh:
                        options = fresh
                    else:
                        ok = False
                        break
                if not options:
                    ok = False
                    break
                # least-used-in-this-slot first, so a family spreads over its pool
                # instead of hammering whatever the RNG likes; RNG only breaks ties.
                rng = _rng(seed, family_id, variant_id, slot_id, attempt)
                keyed = sorted(
                    options,
                    key=lambda clip: (usage[slot_id].get(clip["clip_id"], 0), rng.random(), clip["clip_id"]),
                )
                clip = keyed[0]
                picked[slot_id] = clip["clip_id"]
                taken.add(clip["clip_id"])
            if not ok:
                attempts_log.append({"variant_id": variant_id, "attempt": attempt, "result": "POOL_EXHAUSTED"})
                continue
            vector = [picked[slot_id] for slot_id in slot_ids]
            distances = [changed_slot_count(vector, other) for other in [master_vector] + accepted_vectors]
            if min(distances) < min_changed:
                attempts_log.append(
                    {"variant_id": variant_id, "attempt": attempt, "result": "TOO_CLOSE", "min_changed": min(distances)}
                )
                continue
            chosen = picked
            break
        if chosen is None:
            raise CasterError(
                f"{variant_id}: no casting satisfied the distance rule in {max_attempts} deterministic attempts; "
                f"pool sizes {{{', '.join(f'{k}:{len(v)}' for k, v in pools.items())}}}"
            )

        cast_slots = []
        for slot in slots:
            slot_id = slot["block_id"]
            clip_id = chosen[slot_id]
            clip = next(item for item in pools[slot_id] if item["clip_id"] == clip_id)
            repeat_index = usage[slot_id].get(clip_id, 0)
            cast_slots.append(
                cast_slot(slot, clip, output_fps, repeat_index, len(pools[slot_id]), master_ids.get(slot_id))
            )
            usage[slot_id][clip_id] = repeat_index + 1
        used_hooks.add(chosen[hook_slot])
        accepted.append({"variant_id": variant_id, "slots": cast_slots})
        accepted_vectors.append([chosen[slot_id] for slot_id in slot_ids])

    return accepted, {"attempts": attempts_log, "usage": usage, "master_vector": master_vector, "min_changed": min_changed}


def clearance_margin(luma: Dict[str, float], rule: Dict[str, Any]) -> float:
    """How much room the best available polarity has, in luma levels. Higher is safer."""
    band = caption_servable_band(float(rule.get("plate_alpha", 1.0)))
    return max(band["max_for_light_ink"] - float(luma["p95"]), float(luma["p05"]) - band["min_for_dark_ink"])


def window_grid(clip: Dict[str, Any], frames: int, output_fps: Fraction, steps: int = 12) -> List[int]:
    """Start frames spread across a clip's usable range.

    A scarce slot's real lever is not which clip but which MOMENT of it: p010's pool is two
    clips and 1,404 frames long, and eleven of those frames are used. A window is as much a
    re-cast as a clip is, and it changes the pixels behind the caption just as completely.
    """
    needed = required_source_frames(frames, clip["source_fps"], output_fps)
    usable = max(0, int(clip["source_frame_count"]) - needed)
    if usable == 0:
        return [0]
    return sorted({int(round(usable * index / (steps - 1))) for index in range(steps)})


def recast_slot(
    *,
    slot: Dict[str, Any],
    rule: Dict[str, Any],
    pool: Sequence[Dict[str, Any]],
    output_fps: Fraction,
    taken_clip_ids: Sequence[str],
    current_clip_id: str,
    luma_of: Any,
    window_steps: int = 12,
    sibling_windows: Sequence[Tuple[str, int, int]] = (),
) -> Dict[str, Any]:
    """Choose a (clip, window) for one slot of one variant whose caption cannot be served.

    Every rule the family was cast under still holds: the pool is the slot's own eligible pool
    (front door, blacklist, role/lighting/world already applied), no clip already used elsewhere
    in this casting is available, and the clip-level distance vector is unaffected when only the
    window moves. What is added is the caption-clearance measurement, and the winner is the
    option with the most room, not the first that scrapes through.
    """
    clearance = rule.get("caption_clearance")
    if not clearance:
        raise CasterError(f"slot {slot['block_id']} declares no caption_clearance to re-cast against")
    taken = set(taken_clip_ids) - {current_clip_id}
    options = [clip for clip in pool if clip["clip_id"] not in taken]
    if not options:
        raise CasterError(f"slot {slot['block_id']}: every eligible clip is already used in this casting")

    def collides(clip_id: str, start: int, end: int) -> Optional[str]:
        """Would this window show a sibling variant the same pixels at the same beat?

        Two variants sharing a byte-identical block is the one thing a trial family exists to
        avoid — a creative test only means something if the variants actually differ. An
        overlapping window of the same clip is very nearly the same pixels, so it counts as a
        collision.
        """
        for other_id, other_start, other_end in sibling_windows:
            if other_id == clip_id and start < other_end and other_start < end:
                return f"overlaps a sibling's window [{other_start},{other_end}) of the same clip"
        return None

    rows: List[Dict[str, Any]] = []
    for clip in options:
        needed = required_source_frames(slot["frames"], clip["source_fps"], output_fps)
        for start in window_grid(clip, slot["frames"], output_fps, window_steps):
            collision = collides(clip["clip_id"], start, start + needed)
            luma = luma_of(clip, clearance, (start, start + needed))
            verdict = caption_clearance_verdict(luma, clearance)
            rows.append(
                {
                    "clip_id": clip["clip_id"],
                    "basename": clip["basename"],
                    "source_start_frame": start,
                    "source_end_frame_exclusive": start + needed,
                    "required_source_frames": needed,
                    "servable": verdict["servable"],
                    "polarities_available": verdict["polarities_available"],
                    "margin": round(clearance_margin(luma, clearance), 2),
                    "luma": luma,
                    "changes_clip": clip["clip_id"] != current_clip_id,
                    "sibling_collision": collision,
                }
            )
    servable = [row for row in rows if row["servable"] and not row["sibling_collision"]]
    if not servable:
        blocked = sum(1 for row in rows if row["servable"] and row["sibling_collision"])
        raise CasterError(
            f"slot {slot['block_id']}: no (clip, window) in a pool of {len(options)} can carry this "
            f"reel's captions without duplicating a sibling ({blocked} servable options were "
            "blocked as sibling collisions). This is a footage-coverage finding, not a bug — widen "
            "the slot rule or fetch masters, and record the decision."
        )
    # Most room first; a different clip breaks the tie, because a new clip is more content
    # difference than a new moment of the same one.
    servable.sort(key=lambda row: (-row["margin"], not row["changes_clip"], row["clip_id"], row["source_start_frame"]))
    return {"chosen": servable[0], "considered": rows, "servable_options": len(servable)}


def distance_matrix(vectors: Sequence[Sequence[str]], labels: Sequence[str]) -> Dict[str, Any]:
    n = len(vectors)
    total = len(vectors[0]) if n else 0
    rows = []
    worst = None
    for i in range(n):
        row = []
        for j in range(n):
            changed = changed_slot_count(vectors[i], vectors[j])
            row.append(changed)
            if i != j and (worst is None or changed < worst[0]):
                worst = (changed, labels[i], labels[j])
        rows.append(row)
    return {
        "labels": list(labels),
        "slots": total,
        "changed_slots": rows,
        "ratios": [[round(cell / total, 4) for cell in row] for row in rows],
        "min_changed_off_diagonal": worst[0] if worst else None,
        "min_pair": [worst[1], worst[2]] if worst else None,
    }


# ---------------------------------------------------------------------------
# outputs


def build_trial_family_manifest(
    *,
    family_id: str,
    reel_id: str,
    master_slots: Sequence[Dict[str, Any]],
    castings: Sequence[Dict[str, Any]],
    hook_slot: str,
    min_changed_ratio: float,
    min_changed_slots: int,
) -> Dict[str, Any]:
    """The manifest shape `reelctl variants validate` already knows how to check."""

    def family_slot(slot: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "slot_id": slot["block_id"],
            "role": slot["reference_role"],
            "frames": int(slot["frames"]),
            "source_sha256": slot["source_sha256"],
            "source_start_frame": int(slot["source_start_frame"]),
            "source_end_frame_exclusive": int(slot["source_end_frame_exclusive"]),
        }

    return {
        "schema_version": 1,
        "family_id": family_id,
        "family_state": "PLANNED",
        "mode": "trial-variant-family",
        "master": {
            "reel_id": reel_id,
            "selection_sha256": master_slots[0]["_selection_sha256"],
            "slots": [family_slot(slot) for slot in master_slots],
        },
        "policy": {
            "minimum_variants": 5,
            "opening_slot_id": hook_slot,
            "minimum_changed_video_slots": min_changed_slots,
            "minimum_changed_visual_ratio": min_changed_ratio,
            "unique_opening_source_per_variant": True,
            "locked_layers": [
                "frame_clock",
                "cut_clock",
                "audio",
                "caption_timing",
                "typography",
                "effects",
                "ending",
            ],
            "non_counting_changes": [
                "container_metadata",
                "encode_settings",
                "file_name",
                "cover_only",
                "public_caption_only",
            ],
        },
        "variants": [
            {
                "variant_id": casting["variant_id"],
                "primary_test_variable": "footage_package",
                "slots": [family_slot(slot) for slot in casting["slots"]],
                "review": {
                    "status": "PLANNED",
                    "render_sha256": None,
                    "technical_qc": "PENDING",
                    "visual_qc": "PENDING",
                    "normal_speed_full_watch": False,
                },
            }
            for casting in castings
        ],
    }


def recast_main(argv: Sequence[str]) -> int:
    """Re-cast ONE slot of ONE already-cast variant, because its captions cannot be served.

    Everything else in that casting is left exactly where the family put it. The output is a new
    casting document plus a re-cast record naming what moved, what it was measured against, and
    what the distance rule still reads afterwards.
    """
    parser = argparse.ArgumentParser(prog="variant_caster.py recast")
    parser.add_argument("--project", required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--blacklist", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--casting", required=True, help="the casting to re-cast a slot of")
    parser.add_argument("--slot", required=True, help="block id, e.g. p010")
    parser.add_argument("--siblings", required=True, help="directory of the family's castings")
    parser.add_argument("--out", required=True, help="where to write the re-cast casting")
    parser.add_argument("--reason", required=True, help="why this slot is being re-cast")
    parser.add_argument("--window-steps", type=int, default=12)
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args(argv)

    project = Path(args.project).expanduser().resolve()
    casting_path = Path(args.casting).expanduser().resolve()
    casting = load_json(casting_path)
    slot_id = str(args.slot)

    blueprint = load_json(project / "reference/blueprint.json")
    footage_index = load_json(project / "footage/footage-index.json")
    library_path = Path(args.library).expanduser().resolve()
    library = load_json(library_path)
    blacklist = load_json(Path(args.blacklist).expanduser().resolve())
    policy = load_json(Path(args.policy).expanduser().resolve())
    output_fps = Fraction(str(blueprint["clock"]["fps"]))

    candidates, front_door = build_candidate_table(footage_index, library, blacklist, library_proxy_root(library))
    rule = policy["slots"][slot_id]
    slot = next(s for s in casting["slots"] if s["block_id"] == slot_id)
    pool = eligible_pool(candidates, rule, int(slot["frames"]), output_fps)

    cache: Dict[str, Any] = {}

    def luma_of(clip, clearance, window):
        return measure_caption_luma(clip, clearance, window=window, samples=args.samples, cache=cache)

    # every sibling's window at THIS slot, including any sibling that has itself been re-cast,
    # so two variants cannot end up showing the same pixels at the same beat
    sibling_windows: List[Tuple[str, int, int]] = []
    sibling_vectors: Dict[str, List[str]] = {}
    for path in sorted(Path(args.siblings).expanduser().glob("var*-casting*.json")):
        other = load_json(path)
        if other["variant_id"] == casting["variant_id"]:
            continue
        sibling_vectors[f"{other['variant_id']}:{path.name}"] = [s["source_clip_id"] for s in other["slots"]]
        for other_slot in other["slots"]:
            if other_slot["block_id"] == slot_id:
                sibling_windows.append(
                    (
                        str(other_slot["source_clip_id"]),
                        int(other_slot["source_start_frame"]),
                        int(other_slot["source_end_frame_exclusive"]),
                    )
                )

    result = recast_slot(
        slot={"block_id": slot_id, "frames": int(slot["frames"]), "reference_role": slot["reference_role"]},
        rule=rule,
        pool=pool,
        output_fps=output_fps,
        taken_clip_ids=[s["source_clip_id"] for s in casting["slots"]],
        current_clip_id=str(slot["source_clip_id"]),
        luma_of=luma_of,
        window_steps=args.window_steps,
        sibling_windows=sibling_windows,
    )
    chosen = result["chosen"]
    clip = next(item for item in pool if item["clip_id"] == chosen["clip_id"])

    replacement = cast_slot(
        {"block_id": slot_id, "frames": int(slot["frames"]), "reference_role": slot["reference_role"]},
        clip,
        output_fps,
        repeat_index=0,
        pool_size=len(pool),
        master_clip_id=None,
    )
    replacement["source_start_frame"] = chosen["source_start_frame"]
    replacement["source_end_frame_exclusive"] = chosen["source_end_frame_exclusive"]
    replacement["window_quantile"] = None
    replacement["window_repeat_index"] = 0
    replacement["differs_from_master"] = slot.get("differs_from_master", True)
    replacement["candidate_observation"] = observation_text(
        clip, chosen["source_start_frame"], chosen["required_source_frames"]
    )
    replacement["caption_clearance"] = {
        "measured": chosen["luma"],
        "band": caption_servable_band(float(rule["caption_clearance"]["plate_alpha"])),
        "polarities_available": chosen["polarities_available"],
        "margin_luma_levels": chosen["margin"],
        "scope": (
            "a gate on proxies at a centre-anchored 9:16 crop, not the resolver. The render "
            "measures the real picture and is what proves this."
        ),
    }

    # the distance rule, re-read after the move
    vector = [
        (replacement if s["block_id"] == slot_id else s)["source_clip_id"] for s in casting["slots"]
    ]
    distances = {name: changed_slot_count(vector, other) for name, other in sibling_vectors.items()}
    reuse = [cid for cid in vector if vector.count(cid) > 1]

    out = dict(casting)
    out["slots"] = [replacement if s["block_id"] == slot_id else s for s in casting["slots"]]
    out["recast"] = {
        "schema_version": 1,
        "recast_at_utc": time_utc(),
        "slot": slot_id,
        "reason": args.reason,
        "from": {
            "source_clip_id": slot["source_clip_id"],
            "basename": Path(str(slot["source_path"])).name,
            "source_start_frame": slot["source_start_frame"],
            "source_end_frame_exclusive": slot["source_end_frame_exclusive"],
        },
        "to": {
            "source_clip_id": replacement["source_clip_id"],
            "basename": Path(str(replacement["source_path"])).name,
            "source_start_frame": replacement["source_start_frame"],
            "source_end_frame_exclusive": replacement["source_end_frame_exclusive"],
        },
        "clip_changed": chosen["changes_clip"],
        "pool_size": len(pool),
        "options_considered": len(result["considered"]),
        "options_servable": result["servable_options"],
        "sibling_windows_at_this_slot": [list(row) for row in sibling_windows],
        "options": result["considered"],
        "distance_after": {"vs_siblings": distances, "min": min(distances.values()) if distances else None},
        "no_clip_fills_two_slots": not reuse,
        "policy_sha256": sha256_file(Path(args.policy).expanduser().resolve()),
        "supersedes_casting_sha256": sha256_file(casting_path),
        "honesty": (
            "A machine re-cast of one slot. No frame of the new window was watched by this tool. "
            "The caption-clearance measurement is a proxy-level gate at a centre-anchored 9:16 "
            "crop; the render measures the real picture and is what proves it."
        ),
    }
    target = Path(args.out).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "PASS",
        "variant": casting["variant_id"],
        "slot": slot_id,
        "clip_changed": chosen["changes_clip"],
        "from": out["recast"]["from"],
        "to": out["recast"]["to"],
        "margin_luma_levels": chosen["margin"],
        "polarities_available": chosen["polarities_available"],
        "options_servable": result["servable_options"],
        "options_considered": len(result["considered"]),
        "min_distance_vs_siblings": out["recast"]["distance_after"]["min"],
        "written": str(target),
    }, indent=1))
    return 0


def time_utc() -> str:
    import time as _time

    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "recast":
        return recast_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="locked reelctl project directory")
    parser.add_argument("--library", required=True, help="FOOTAGE_LIBRARY.json")
    parser.add_argument("--blacklist", required=True, help="SHOT_BLACKLIST.json")
    parser.add_argument("--policy", required=True, help="per-slot eligibility policy JSON")
    parser.add_argument("--out", required=True, help="output directory for castings and receipts")
    parser.add_argument("--variants", type=int, default=10)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--family-id", default=None)
    args = parser.parse_args(argv)

    project = Path(args.project).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    blueprint = load_json(project / "reference/blueprint.json")
    selection = load_json(project / "edit/selection.locked.json")
    footage_index = load_json(project / "footage/footage-index.json")
    library = load_json(Path(args.library).expanduser().resolve())
    blacklist = load_json(Path(args.blacklist).expanduser().resolve())
    policy = load_json(Path(args.policy).expanduser().resolve())

    output_fps = Fraction(str(blueprint["clock"]["fps"]))
    blocks = {str(block["id"]): block for block in blueprint["picture_blocks"]}
    master_slots = selection["slots"]
    slots = [
        {
            "block_id": str(slot["block_id"]),
            "frames": int(slot["frames"]),
            "reference_role": str(slot["reference_role"]),
        }
        for slot in master_slots
    ]
    for slot in slots:
        if slot["block_id"] not in blocks:
            raise CasterError(f"selection slot {slot['block_id']} is not a blueprint picture block")
        if int(blocks[slot["block_id"]]["frames"]) != slot["frames"]:
            raise CasterError(f"{slot['block_id']} frame count disagrees with the blueprint")

    candidates, front_door = build_candidate_table(footage_index, library, blacklist, library_proxy_root(library))

    rules = policy["slots"]
    missing = [slot["block_id"] for slot in slots if slot["block_id"] not in rules]
    if missing:
        raise CasterError(f"policy has no eligibility rule for {missing}")

    pools: Dict[str, List[Dict[str, Any]]] = {}
    pool_report = []
    empty = []
    for slot in slots:
        slot_id = slot["block_id"]
        pool = eligible_pool(candidates, rules[slot_id], slot["frames"], output_fps)
        pools[slot_id] = pool
        if not pool:
            empty.append(slot_id)
        pool_report.append(
            {
                "block_id": slot_id,
                "frames": slot["frames"],
                "reference_role": slot["reference_role"],
                "rule": rules[slot_id],
                "pool_size": len(pool),
                "pool": [
                    {
                        "clip_id": clip["clip_id"],
                        "basename": clip["basename"],
                        "library_id": clip["library_id"],
                        "world_cluster": clip["tags"].get("world_cluster"),
                        "lighting": clip["tags"].get("lighting"),
                        "roles": clip["tags"].get("roles"),
                        "subject": clip["tags"].get("subject"),
                        "energy": clip["tags"].get("energy"),
                        "source_frame_count": clip["source_frame_count"],
                        "source_fps": clip["source_fps"],
                    }
                    for clip in pool
                ],
            }
        )

    master_ids = {str(slot["block_id"]): str(slot["source_clip_id"]) for slot in master_slots}
    hook_slot = str(policy.get("hook_slot", slots[0]["block_id"]))
    min_changed_ratio = float(policy.get("min_changed_ratio", 0.70))
    family_id = args.family_id or f"{blueprint.get('reference_sha256', 'unknown')[:12]}-trial-family"

    if empty:
        raise CasterError(
            f"empty eligible pool for {empty}; this is a finding about footage coverage, not a bug — "
            "widen the slot rule in the policy file or fetch masters, and record the decision"
        )

    castings, trace = cast_family(
        slots,
        pools,
        master_ids,
        variants=args.variants,
        seed=args.seed,
        family_id=family_id,
        output_fps=output_fps,
        hook_slot=hook_slot,
        min_changed_ratio=min_changed_ratio,
    )

    slot_ids = [slot["block_id"] for slot in slots]
    vectors = [[slot["source_clip_id"] for slot in casting["slots"]] for casting in castings]
    labels = [casting["variant_id"] for casting in castings]
    matrix = distance_matrix([trace["master_vector"]] + vectors, ["v002_master"] + labels)

    selection_sha = sha256_file(project / "edit/selection.locked.json")
    tool_sha = sha256_file(Path(__file__).resolve())
    policy_sha = sha256_file(Path(args.policy).expanduser().resolve())

    scarce = [
        {
            "block_id": slot_id,
            "pool_size": len(pools[slot_id]),
            "variants_requested": args.variants,
            "clips_must_repeat": len(pools[slot_id]) < args.variants,
            "distinct_clips_used": len(trace["usage"][slot_id]),
            "max_reuse_of_one_clip": max(trace["usage"][slot_id].values()) if trace["usage"][slot_id] else 0,
        }
        for slot_id in slot_ids
        if len(pools[slot_id]) < args.variants
    ]

    common = {
        "schema_version": SCHEMA_VERSION,
        "artifact": "VARIANT_CASTING",
        "family_id": family_id,
        "parent_project": str(project),
        "parent_project_id": str(load_json(project / "project.json")["project_id"]),
        "parent_selection_locked_sha256": selection_sha,
        "blueprint_sha256_contract": blueprint.get("sha256_contract"),
        "reference_sha256": blueprint.get("reference_sha256"),
        "output_fps": str(output_fps),
        "frame_count": int(blueprint["clock"]["frame_count"]),
        "seed": args.seed,
        "caster": {"tool": str(Path(__file__).resolve()), "tool_sha256": tool_sha, "policy_sha256": policy_sha},
        "gates": {
            "footage_front_door": {
                "authorized_root": footage_index.get("root"),
                "rule": "PASS member of the parent project's footage-index, under masters/ only",
                "index_clips": front_door["index_clips"],
                "index_masters": front_door["index_masters"],
                "masters_without_library_tags": front_door["master_untagged"],
                "eligible_master_universe": len(candidates),
            },
            "blacklist": {
                "file": str(Path(args.blacklist).expanduser().resolve()),
                "sha256": sha256_file(Path(args.blacklist).expanduser().resolve()),
                "banned_entries": len(blacklist.get("blacklisted_clips", [])),
                "rejected_from_universe": front_door["blacklist_rejected"],
            },
            "distance_rule": {
                "min_changed_ratio": min_changed_ratio,
                "min_changed_slots": trace["min_changed"],
                "hook_slot": hook_slot,
                "hook_unique_per_variant": True,
                "hook_differs_from_master": True,
            },
            "speed_policy": "normal speed only, no reverse, no freeze, no loop",
        },
        "honesty": {
            "agent_visual_review": "PENDING_MACHINE",
            "meaning": (
                "This casting is a machine proposal. No frame of any cast window was watched by this "
                "tool. candidate_observation transcribes another agent's library tag record. The "
                "factory's independent-observation gate, the caption-legibility question over new "
                "picture, and creative approval are all still open."
            ),
            "grade": (
                "The parent's approved casting ships identity creative grading on 11 of 12 slots; a "
                "variant inherits the technical LC-709 normalisation only. Any per-shot creative "
                "correction is a human-reviewed act and is not cast here."
            ),
        },
    }

    written = []
    for casting, vector in zip(castings, vectors):
        distances = {
            "v002_master": changed_slot_count(vector, trace["master_vector"]),
            **{
                other_label: changed_slot_count(vector, other_vector)
                for other_label, other_vector in zip(labels, vectors)
                if other_label != casting["variant_id"]
            },
        }
        payload = {
            **common,
            "variant_id": casting["variant_id"],
            "slots": casting["slots"],
            "slot_order": slot_ids,
            "distance": {
                "slots": len(slot_ids),
                "changed_vs_master": distances["v002_master"],
                "changed_vs_siblings": {k: v for k, v in distances.items() if k != "v002_master"},
                "min_changed_vs_any": min(distances.values()),
                "min_changed_required": trace["min_changed"],
                "pass": min(distances.values()) >= trace["min_changed"],
            },
            "scarce_role_policy": {
                "rule": (
                    "A slot whose eligible pool is smaller than the variant count cannot give every "
                    "variant a fresh clip. Repeats are permitted there and are disclosed per slot "
                    "below; they are never silent, and a repeat is always cast at a different source "
                    "window quantile."
                ),
                "scarce_slots": scarce,
                "this_variant_repeats": [
                    {
                        "block_id": slot["block_id"],
                        "clip_id": slot["source_clip_id"],
                        "window_repeat_index": slot["window_repeat_index"],
                        "pool_size": slot["pool_size"],
                    }
                    for slot in casting["slots"]
                    if slot["window_repeat_index"] > 0
                ],
            },
        }
        path = out / f"{casting['variant_id']}-casting.json"
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append({"variant_id": casting["variant_id"], "path": str(path), "sha256": sha256_file(path)})

    master_family_slots = [
        {
            "block_id": str(slot["block_id"]),
            "reference_role": str(slot["reference_role"]),
            "frames": int(slot["frames"]),
            "source_sha256": str(slot["source_sha256"]),
            "source_start_frame": int(slot["source_start_frame"]),
            "source_end_frame_exclusive": int(slot["source_start_frame"])
            + required_source_frames(int(slot["frames"]), str(slot["source_fps"]), output_fps),
            "_selection_sha256": selection_sha,
        }
        for slot in master_slots
    ]
    manifest = build_trial_family_manifest(
        family_id=family_id,
        reel_id=str(load_json(project / "project.json")["project_id"]),
        master_slots=master_family_slots,
        castings=castings,
        hook_slot=hook_slot,
        min_changed_ratio=min_changed_ratio,
        min_changed_slots=trace["min_changed"],
    )
    manifest_path = out / "trial-family.json"
    manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    pools_path = out / "pools.json"
    pools_path.write_text(
        json.dumps(
            {**common, "artifact": "VARIANT_ELIGIBLE_POOLS", "slots": pool_report},
            indent=1,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    matrix_path = out / "distance-matrix.json"
    matrix_path.write_text(json.dumps({**common, "artifact": "VARIANT_DISTANCE_MATRIX", **matrix}, indent=1) + "\n", encoding="utf-8")

    summary = {
        "status": "PASS",
        "family_id": family_id,
        "variants": len(castings),
        "seed": args.seed,
        "eligible_master_universe": len(candidates),
        "pool_sizes": {slot_id: len(pools[slot_id]) for slot_id in slot_ids},
        "min_changed_required": trace["min_changed"],
        "min_changed_observed": matrix["min_changed_off_diagonal"],
        "min_pair": matrix["min_pair"],
        "scarce_slots": [row["block_id"] for row in scarce],
        "blacklist_rejected": [row["clip_id"] for row in front_door["blacklist_rejected"]],
        "castings": written,
        "trial_family_manifest": str(manifest_path),
        "pools": str(pools_path),
        "distance_matrix": str(matrix_path),
    }
    receipt_path = out / "caster-receipt.json"
    receipt_path.write_text(json.dumps({**common, "artifact": "VARIANT_CASTER_RUN", **summary}, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
