from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import __version__
from .assets import validate_assets_manifest
from .authorization import authorize_source, authorized_footage_root
from .color import color_receipt, ffmpeg_color_filter, validate_color_contract
from .errors import ContractError
from .hashing import atomic_external_output, atomic_write_bytes, atomic_write_json, load_json, recipe_hash, sha256_file
from .identifiers import validate_identifier
from .media import audio_packet_ledger, audio_payload_sha256, decode_pcm_sha256, frame_pts, full_decode, probe_media, run_checked
from .paths import canonical_root, confined_path, secure_mkdirs
from .reference import verify_blueprint_identity, verify_reference_identity
from .selection import validate_selection
from .signing import SignatureError, sign_payload, verify_payload
from .typography import validate_typography_layer


def _ffconcat_escape(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def _toolchain_identity() -> Dict[str, str]:
    ffmpeg = Path(shutil.which("ffmpeg") or "").resolve()
    ffprobe = Path(shutil.which("ffprobe") or "").resolve()
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise ContractError("ffmpeg and ffprobe must resolve to regular executable files")
    return {
        "reelctl": __version__,
        "ffmpeg_path": str(ffmpeg),
        "ffmpeg_version": run_checked([str(ffmpeg), "-version"]).stdout.splitlines()[0],
        "ffmpeg_sha256": sha256_file(ffmpeg),
        "ffprobe_path": str(ffprobe),
        "ffprobe_version": run_checked([str(ffprobe), "-version"]).stdout.splitlines()[0],
        "ffprobe_sha256": sha256_file(ffprobe),
    }


def _run_external_atomic(command: List[str], *, output: Path, root: Path) -> None:
    if not command or Path(command[-1]) != output:
        raise ContractError("atomic external command must end with its declared output path")
    with atomic_external_output(output, root=root) as temporary:
        run_checked([*command[:-1], str(temporary)])


def _authorized_source(project_dir: Path, slot: Dict[str, Any], footage_root: Path) -> Path:
    raw = Path(str(slot["source_path"])).expanduser()
    path = raw if raw.is_absolute() else project_dir / raw
    return authorize_source(path, footage_root, kind=f"selected source for block {slot.get('block_id', '<unnamed>')}")


def _source_path(project_dir: Path, slot: Dict[str, Any], footage_root: Path) -> Path:
    path = _authorized_source(project_dir, slot, footage_root)
    if not path.is_file():
        raise ContractError(f"selected source is missing: {path}")
    return path


# H.264's lossy transform can ring a nominally legal edge back outside
# 16..235 after decode. Keep a small headroom margin before the review encode;
# the QC gate measures the decoded review file, not just the encoder metadata.
#
# Measured overshoot is ~15 code values against hard caption edges: at a 221 ceiling the
# encoder input is provably 30..221 yet one measured frame decoded at 236 and
# failed QC, having decoded at 235 — zero margin — on the previous revision. 214 lands that
# frame at 230. Removing the clamp entirely, or replacing it with a plain limiter at 16..235,
# puts 189 of 271 frames out of range; pre-legalizing the master to 64..940 changes nothing,
# because the excursions are manufactured by the codec after the filter chain runs.
#
# This headroom is an OPEN-LOOP MITIGATION, not a guarantee — it buys margin, it does not
# bound the codec. The deterministic fixes are (c) a closed-loop encode/measure/tighten pass
# recorded in the render receipt, and (d) gating legal range on the master, where the pipeline
# actually has control, while allowing the lossy proxy a ringing tolerance. Both are queued as
# v1.1 engine work along with the upstream master-legality defect.
REVIEW_LUMA_HEADROOM = (30, 214)
REVIEW_CHROMA_HEADROOM = (16, 240)


def review_encode_filter() -> str:
    y_low, y_high = REVIEW_LUMA_HEADROOM
    c_low, c_high = REVIEW_CHROMA_HEADROOM
    return (
        "format=yuv420p,"
        f"lutyuv=y='clip(val,{y_low},{y_high})':"
        f"u='clip(val,{c_low},{c_high})':v='clip(val,{c_low},{c_high})',"
        "setparams=range=tv"
    )


def review_timing_args(time_base_denominator: int) -> List[str]:
    return [
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        f"1:{time_base_denominator}",
        "-video_track_timescale",
        str(time_base_denominator),
        "-bf",
        "0",
    ]


def _render_segment(
    source: Path,
    slot: Dict[str, Any],
    output: Path,
    *,
    width: int,
    height: int,
    fps: str,
    time_base_denominator: int,
    root: Path,
) -> None:
    start_frame = int(slot.get("source_start_frame", 0))
    target_frames = int(slot.get("frames", slot.get("duration_frames", 0)))
    anchor = slot.get("crop_anchor_xy", slot.get("display_crop_anchor_xy", [0.5, 0.5]))
    anchor_x = max(0.0, min(1.0, float(anchor[0])))
    anchor_y = max(0.0, min(1.0, float(anchor[1])))
    transform = slot.get("transform", {})
    filters: List[str] = []
    if transform.get("hflip"):
        filters.append("hflip")
    filters += [
        f"trim=start_frame={start_frame}",
        "setpts=PTS-STARTPTS",
        f"fps=fps={fps}:round=near",
        f"trim=end_frame={target_frames}",
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos",
        f"crop={width}:{height}:x=(in_w-out_w)*{anchor_x:.8f}:y=(in_h-out_h)*{anchor_y:.8f}",
        "setsar=1",
        ffmpeg_color_filter(slot),
    ]
    with atomic_external_output(output, root=root) as temporary:
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            ",".join(filters),
            "-frames:v",
            str(target_frames),
            "-fps_mode",
            "cfr",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "3",
            "-pix_fmt",
            "yuv422p10le",
            "-r",
            fps,
            "-video_track_timescale",
            str(time_base_denominator),
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            str(temporary),
        ]
        run_checked(command)
        facts = probe_media(temporary)
        if int(facts["video"]["frame_count"] or 0) != target_frames:
            raise ContractError(f"segment {output.name} rendered {facts['video']['frame_count']} frames; expected {target_frames}")


def _overlay_sequence(
    base: Path,
    pattern: Path,
    output: Path,
    *,
    fps: str,
    frame_count: int,
    time_base_denominator: int,
    root: Path,
) -> None:
    with atomic_external_output(output, root=root) as temporary:
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(base),
            "-framerate",
            fps,
            "-start_number",
            "0",
            "-i",
            str(pattern),
            "-filter_complex",
            "[0:v][1:v]overlay=0:0:format=auto:shortest=1,format=yuv422p10le,setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709[v]",
            "-map",
            "[v]",
            "-frames:v",
            str(frame_count),
            "-an",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "3",
            "-pix_fmt",
            "yuv422p10le",
            "-r",
            fps,
            "-video_track_timescale",
            str(time_base_denominator),
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            str(temporary),
        ]
        run_checked(command)


LICENSED_AUDIO_POLICY = "licensed_track_required"
REFERENCE_AUDIO_POLICY = "reference_audio_rights_held"


def _audio_source(project_dir: Path, project: Dict[str, Any], reference_path: Path) -> Tuple[str, Path]:
    """Resolve what audio gets muxed under the project's audio policy.

    Default `licensed_track_required`: a track the user holds rights to (`audio_track`, locked into
    the project by `reelctl new --audio-track`). `reference_audio_rights_held` is an explicit opt-in
    for users who hold the rights to the reference's own audio. The reference is analysed for
    timing (clock, beat grid) under either policy; only the muxed stream differs.
    """
    policy = str((project.get("policies") or {}).get("audio") or LICENSED_AUDIO_POLICY)
    if policy == REFERENCE_AUDIO_POLICY:
        return policy, reference_path
    if policy != LICENSED_AUDIO_POLICY:
        raise ContractError(f"unknown audio policy: {policy}")
    track = project.get("audio_track")
    if not track:
        raise ContractError(
            "audio policy licensed_track_required: no audio_track in project.json. Supply a track you hold rights to "
            "(`reelctl new ... --audio-track PATH`); use policy reference_audio_rights_held only if you hold the rights "
            "to the reference's own audio."
        )
    return policy, confined_path(project_dir, Path(str(track)), require="file", allow_missing=False)


def render_project(project_dir: Path, *, revision: str) -> Dict[str, Any]:
    validate_identifier(revision, kind="revision")
    project_dir = canonical_root(project_dir)
    project = load_json(project_dir / "project.json", root=project_dir)
    validate_identifier(str(project["project_id"]), kind="project")
    footage_root = authorized_footage_root(project)
    reference_path = confined_path(project_dir, Path(str(project["reference_path"])), require="file", allow_missing=False)
    reference_lock = load_json(project_dir / "reference/reference-lock.json", root=project_dir)
    verify_reference_identity(reference_path, reference_lock)
    audio_policy, audio_path = _audio_source(project_dir, project, reference_path)
    blueprint = load_json(project_dir / "reference/blueprint.json", root=project_dir)
    verify_blueprint_identity(blueprint, reference_lock)
    selection_path = project_dir / "edit/selection.locked.json"
    assets_path = project_dir / "assets/assets.locked.json"
    inventory = load_json(project_dir / "footage/footage-index.json", root=project_dir)
    feasibility = load_json(project_dir / "edit/feasibility.locked.json", root=project_dir)
    selection = load_json(selection_path, root=project_dir)
    assets = load_json(assets_path, root=project_dir)
    raw_slots = selection.get("slots", selection.get("shots", []))
    slots = []
    for raw_slot in raw_slots:
        slot = dict(raw_slot)
        _authorized_source(project_dir, slot, footage_root)
        if slot.get("technical_lut"):
            lut = Path(str(slot["technical_lut"])).expanduser()
            if not lut.is_absolute():
                lut = project_dir / lut
            slot["technical_lut"] = str(confined_path(project_dir, lut, require="file", allow_missing=False))
        slots.append(slot)
    normalized_selection = dict(selection)
    normalized_selection["slots"] = slots
    selection_receipt = validate_selection(
        blueprint,
        normalized_selection,
        mode=project.get("mode", "reference-locked"),
        inventory=inventory,
        feasibility=feasibility,
    )
    validate_color_contract(slots)
    for layer in assets.get("typography_layers", []):
        validate_typography_layer(layer)
    if assets.get("status") != "PASS":
        raise ContractError("assets.locked.json is not PASS")

    clock = reference_lock["clock"]
    width, height = int(clock["width"]), int(clock["height"])
    fps = str(clock["fps"])
    time_base = str(clock["time_base"])
    if not time_base.startswith("1/"):
        raise ContractError(f"unsupported reference time base: {time_base}")
    time_base_denominator = int(time_base.split("/", 1)[1])
    frame_count = int(clock["frame_count"])

    validate_assets_manifest(assets, frame_count=frame_count, root=project_dir, footage_root=footage_root)

    source_receipts = []
    for slot in slots:
        source = _source_path(project_dir, slot, footage_root)
        digest = sha256_file(source)
        if slot.get("source_sha256") != digest:
            raise ContractError(f"selected source is not hash-bound or changed after selection lock: {source}")
        source_receipts.append({"path": str(source), "sha256": digest, "bytes": source.stat().st_size})
    asset_receipts: List[Dict[str, Any]] = []
    overlay_pattern = assets.get("overlay_sequence")
    if overlay_pattern:
        pattern_path = Path(str(overlay_pattern))
        pattern_path = pattern_path if pattern_path.is_absolute() else project_dir / pattern_path
        pattern_path = confined_path(project_dir, pattern_path)
        overlay_receipt_path = pattern_path.parent / "overlay-receipt.json"
        overlay_receipt_path = confined_path(project_dir, overlay_receipt_path, require="file", allow_missing=False)
        if sha256_file(overlay_receipt_path) != assets.get("overlay_receipt_sha256"):
            raise ContractError("overlay sequence receipt hash does not match assets.locked.json")
        overlay_receipt = load_json(overlay_receipt_path, root=project_dir)
        try:
            verify_payload(overlay_receipt, purpose="overlay-sequence-receipt-v1")
        except SignatureError as exc:
            raise ContractError(f"overlay sequence receipt signature is invalid: {exc}") from exc
        if overlay_receipt.get("recipe_hash") != assets.get("overlay_recipe_hash"):
            raise ContractError("overlay sequence recipe does not match assets.locked.json")
        existing = [
            confined_path(
                project_dir,
                Path(str(pattern_path).replace("%06d", f"{index:06d}")),
                require="file",
                allow_missing=False,
            )
            for index in range(frame_count)
        ]
        expected_frame_hashes = overlay_receipt.get("frame_sha256", [])
        if len(expected_frame_hashes) != frame_count:
            raise ContractError("overlay sequence receipt does not cover every output frame")
        for index, (path, expected_hash) in enumerate(zip(existing, expected_frame_hashes)):
            if sha256_file(path) != expected_hash:
                raise ContractError(f"overlay sequence frame {index} changed after assets lock")
        asset_receipts = [{"path": str(path), "sha256": sha256_file(path)} for path in existing]

    recipe = {
        "schema_version": 1,
        "revision": revision,
        "project": project,
        "reference_sha256": reference_lock["source"]["sha256"],
        "audio_policy": audio_policy,
        "audio_sha256": sha256_file(audio_path),
        "blueprint_contract": blueprint.get("sha256_contract"),
        "selection": normalized_selection,
        "assets": assets,
        "sources": source_receipts,
        "asset_receipts": asset_receipts,
        "toolchain": _toolchain_identity(),
        "reelctl_render_schema": 1,
    }
    digest = recipe_hash(recipe)
    output_dir = confined_path(project_dir, project_dir / "edit" / f"render-{revision}")
    receipt_path = output_dir / "render-receipt.json"
    if receipt_path.is_file():
        receipt = load_json(receipt_path, root=project_dir)
        try:
            verify_payload(receipt, purpose="render-receipt-v1")
        except SignatureError as exc:
            raise ContractError(f"cached render receipt signature is invalid: {exc}") from exc
        if receipt.get("recipe_hash") != digest:
            raise ContractError(f"append-only revision {revision} already exists with a different recipe")
        for key in ("master", "review"):
            path = confined_path(project_dir, Path(receipt[key]["path"]), require="file", allow_missing=False)
            if not path.is_file() or sha256_file(path) != receipt[key]["sha256"]:
                raise ContractError(f"cached {key} receipt is stale or corrupted: {path}")
        return {
            "status": "PASS",
            "cache_hit": True,
            "recipe_hash": digest,
            "master_path": receipt["master"]["path"],
            "master_sha256": receipt["master"]["sha256"],
            "review_path": receipt["review"]["path"],
            "review_sha256": receipt["review"]["sha256"],
            "render_receipt_path": str(receipt_path),
        }
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ContractError(f"refusing non-empty render directory without matching receipt: {output_dir}")
    secure_mkdirs(project_dir, output_dir.relative_to(project_dir))

    cache_root = secure_mkdirs(project_dir, ".reelctl/cache/segments")
    segment_paths: List[Path] = []
    segment_receipts: List[Dict[str, Any]] = []
    output_start_frame = 0
    for index, (slot, source_receipt) in enumerate(zip(slots, source_receipts), start=1):
        segment_recipe = {
            "source": source_receipt,
            "slot": slot,
            "clock": {"width": width, "height": height, "fps": fps, "time_base": time_base},
            "color": color_receipt(slot),
            "toolchain": recipe["toolchain"],
            "renderer": 1,
        }
        segment_hash = recipe_hash(segment_recipe)
        segment = confined_path(project_dir, cache_root / f"{segment_hash}.mov")
        cache_receipt_path = confined_path(project_dir, cache_root / f"{segment_hash}.json")
        if segment.exists() or cache_receipt_path.exists():
            segment = confined_path(project_dir, segment, require="file", allow_missing=False)
            cache_receipt_path = confined_path(project_dir, cache_receipt_path, require="file", allow_missing=False)
            cache_receipt = load_json(cache_receipt_path, root=project_dir)
            try:
                verify_payload(cache_receipt, purpose="segment-cache-receipt-v1")
            except SignatureError as exc:
                raise ContractError(f"segment cache receipt signature is invalid: {segment}: {exc}") from exc
            if cache_receipt.get("recipe_hash") != segment_hash or cache_receipt.get("sha256") != sha256_file(segment):
                raise ContractError(f"segment cache receipt is stale or corrupted: {segment}")
        else:
            _render_segment(
                Path(source_receipt["path"]),
                slot,
                segment,
                width=width,
                height=height,
                fps=fps,
                time_base_denominator=time_base_denominator,
                root=project_dir,
            )
            cache_receipt = sign_payload(
                {"schema_version": 1, "recipe_hash": segment_hash, "sha256": sha256_file(segment), "bytes": segment.stat().st_size},
                purpose="segment-cache-receipt-v1",
            )
            atomic_write_json(
                cache_receipt_path,
                cache_receipt,
                root=project_dir,
            )
        segment_paths.append(segment)
        segment_receipts.append(
            {
                "slot": index,
                "block_id": slot["block_id"],
                "output_start_frame": output_start_frame,
                "output_end_frame_exclusive": output_start_frame + int(slot["frames"]),
                "frames": int(slot["frames"]),
                "path": str(segment),
                "sha256": sha256_file(segment),
                "recipe_hash": segment_hash,
            }
        )
        output_start_frame += int(slot["frames"])

    concat_list = output_dir / "segments.ffconcat"
    atomic_write_bytes(
        concat_list,
        ("ffconcat version 1.0\n" + "".join(f"file '{_ffconcat_escape(path)}'\n" for path in segment_paths)).encode("utf-8"),
        root=project_dir,
    )
    base = output_dir / "picture-base.mov"
    _run_external_atomic(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-safe",
            "0",
            "-f",
            "concat",
            "-i",
            str(concat_list),
            "-map",
            "0:v:0",
            "-an",
            "-c",
            "copy",
            "-video_track_timescale",
            str(time_base_denominator),
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            str(base),
        ],
        output=base,
        root=project_dir,
    )
    picture = base
    if overlay_pattern:
        overlaid = output_dir / "picture-overlays.mov"
        _overlay_sequence(
            base,
            pattern_path,
            overlaid,
            fps=fps,
            frame_count=frame_count,
            time_base_denominator=time_base_denominator,
            root=project_dir,
        )
        picture = overlaid

    master = output_dir / f"{project['project_id']}-candidate-master-{revision}.mov"
    _run_external_atomic(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(picture),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0?",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-video_track_timescale",
            str(time_base_denominator),
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            str(master),
        ],
        output=master,
        root=project_dir,
    )
    review = output_dir / f"{project['project_id']}-candidate-review-{revision}.mp4"
    _run_external_atomic(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(master),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            review_encode_filter(),
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "12",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            *review_timing_args(time_base_denominator),
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            str(review),
        ],
        output=review,
        root=project_dir,
    )

    master_facts = probe_media(master)
    review_facts = probe_media(review)
    for label, facts in (("master", master_facts), ("review", review_facts)):
        if int(facts["video"]["frame_count"] or 0) != frame_count:
            raise ContractError(f"{label} frame count changed: {facts['video']['frame_count']} != {frame_count}")
    for label, path in (("master", master), ("review", review)):
        if frame_pts(path) != clock["pts"]:
            raise ContractError(f"{label} presentation timestamp ledger differs from the reference clock")
    # Audio verification is the same for both policies: the muxed track's decoded PCM, compressed
    # payload and packet ledger must survive the stream-copy mux and the review encode unchanged.
    source_pcm = decode_pcm_sha256(audio_path)
    if decode_pcm_sha256(master) != source_pcm or decode_pcm_sha256(review) != source_pcm:
        raise ContractError(f"{audio_policy}: audio PCM identity was lost during mux/review encode")
    source_audio_payload = audio_payload_sha256(audio_path)
    if audio_payload_sha256(master) != source_audio_payload or audio_payload_sha256(review) != source_audio_payload:
        raise ContractError(f"{audio_policy}: compressed audio payload identity was lost during stream-copy mux")
    if audio_policy == REFERENCE_AUDIO_POLICY:
        source_audio_packets = reference_lock["audio"].get("packet_ledger")
    else:
        source_audio_packets = audio_packet_ledger(audio_path)
    if audio_packet_ledger(master) != source_audio_packets or audio_packet_ledger(review) != source_audio_packets:
        raise ContractError(f"{audio_policy}: audio packet bytes/timestamps/durations were lost during stream-copy mux")
    if audio_policy == REFERENCE_AUDIO_POLICY and reference_lock["audio"]["present"]:
        for label, facts in (("master", master_facts), ("review", review_facts)):
            audio = facts["audio"] or {}
            if int(audio.get("start_pts") or 0) != int(reference_lock["audio"]["start_pts"]):
                raise ContractError(f"{label} audio start PTS differs from the reference")
            if audio.get("time_base") != reference_lock["audio"]["time_base"]:
                raise ContractError(f"{label} audio time base differs from the reference")

    receipt = sign_payload(
        {
            "schema_version": 1,
            "status": "PASS",
            "revision": revision,
            "recipe_hash": digest,
            "clock": clock,
            "selection": selection_receipt,
            "sources": source_receipts,
            "color": [color_receipt(slot) for slot in slots],
            "segments": segment_receipts,
            "assets": asset_receipts,
            "audio_policy": audio_policy,
            "audio_source": {"path": str(audio_path), "sha256": sha256_file(audio_path)},
            "audio_pcm_sha256": source_pcm,
            "audio_payload_sha256": source_audio_payload,
            "audio_packet_ledger": source_audio_packets,
            "master": {
                "path": str(master),
                "bytes": master.stat().st_size,
                "sha256": sha256_file(master),
                "facts": master_facts,
                "decode": full_decode(master),
            },
            "review": {
                "path": str(review),
                "bytes": review.stat().st_size,
                "sha256": sha256_file(review),
                "facts": review_facts,
                "decode": full_decode(review),
            },
            "publication": {"status": "NOT_APPROVED", "human_approval": "PENDING"},
        },
        purpose="render-receipt-v1",
    )
    atomic_write_json(receipt_path, receipt, root=project_dir)
    return {
        "status": "PASS",
        "cache_hit": False,
        "recipe_hash": digest,
        "master_path": str(master),
        "master_sha256": receipt["master"]["sha256"],
        "review_path": str(review),
        "review_sha256": receipt["review"]["sha256"],
        "render_receipt_path": str(receipt_path),
    }
