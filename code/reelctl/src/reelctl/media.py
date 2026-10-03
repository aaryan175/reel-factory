from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ExternalToolError
from .hashing import recipe_hash, sha256_file


def run_checked(command: List[str], *, capture_binary: bool = False) -> Any:
    try:
        return subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not capture_binary,
        )
    except FileNotFoundError as exc:
        raise ExternalToolError(f"required executable not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr
        raise ExternalToolError(f"command failed ({exc.returncode}): {' '.join(command)}\n{stderr}") from exc


def _ratio(value: Optional[str], fallback: str = "0/1") -> str:
    if not value or value in {"0/0", "N/A"}:
        return fallback
    if "/" not in value:
        return f"{value}/1"
    return value


def probe_media(path: Path, *, count_frames: bool = True, precomputed_sha256: Optional[str] = None) -> Dict[str, Any]:
    path = Path(path).resolve()
    entries = (
        "stream=index,codec_type,codec_name,profile,pix_fmt,width,height,sample_aspect_ratio,"
        "display_aspect_ratio,r_frame_rate,avg_frame_rate,time_base,start_pts,duration_ts,"
        "nb_frames,nb_read_frames,sample_rate,channels,color_range,color_space,color_transfer,"
        "color_primaries:stream_tags=rotate,creation_time,com.apple.quicktime.make,"
        "com.apple.quicktime.model,com.apple.quicktime.software:stream_side_data=rotation:format=duration,size,format_name"
    )
    command = ["ffprobe", "-v", "error"]
    if count_frames:
        command.append("-count_frames")
    command += ["-show_entries", entries, "-of", "json", str(path)]
    payload = json.loads(run_checked(command).stdout)
    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ExternalToolError(f"no video stream: {path}")
    video = dict(video)
    video["r_frame_rate"] = _ratio(video.get("r_frame_rate"))
    video["avg_frame_rate"] = _ratio(video.get("avg_frame_rate"), video["r_frame_rate"])
    video["sample_aspect_ratio"] = video.get("sample_aspect_ratio") or "1:1"
    if video["sample_aspect_ratio"] in {"0:1", "N/A"}:
        video["sample_aspect_ratio"] = "1:1"
    tags = video.get("tags") or {}
    try:
        side_rotation = next(
            (item.get("rotation") for item in video.get("side_data_list", []) if item.get("rotation") is not None),
            None,
        )
        video["rotation"] = int(side_rotation if side_rotation is not None else tags.get("rotate") or 0) % 360
    except (TypeError, ValueError):
        video["rotation"] = 0
    raw_count = video.get("nb_read_frames") or video.get("nb_frames")
    video["frame_count"] = int(raw_count) if raw_count not in {None, "N/A"} else None
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": precomputed_sha256 or sha256_file(path),
        "format": payload.get("format", {}),
        "video": video,
        "audio": audio,
    }


def frame_pts(path: Path) -> List[int]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=best_effort_timestamp",
        "-of",
        "json",
        str(Path(path).resolve()),
    ]
    payload = json.loads(run_checked(command).stdout)
    values: List[int] = []
    for frame in payload.get("frames", []):
        value = frame.get("best_effort_timestamp")
        if value is not None:
            values.append(int(value))
    return values


def media_toolchain_identity() -> Dict[str, Any]:
    identity: Dict[str, Any] = {}
    for name in ("ffmpeg", "ffprobe"):
        resolved = Path(shutil.which(name) or "").resolve()
        if not resolved.is_file():
            raise ExternalToolError(f"required executable not found: {name}")
        identity[name] = {
            "path": str(resolved),
            "sha256": sha256_file(resolved),
            "version": run_checked([str(resolved), "-version"]).stdout.splitlines()[0],
        }
    return identity


def video_frame_ledger(path: Path) -> Dict[str, Any]:
    result = run_checked(
        [
            "ffmpeg",
            "-v",
            "error",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-i",
            str(Path(path).resolve()),
            "-map",
            "0:v:0",
            "-an",
            "-pix_fmt",
            "gbrp16le",
            "-f",
            "framemd5",
            "-hash",
            "sha256",
            "-",
        ]
    )
    frames: List[Dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split(",", 5)]
        if len(fields) != 6:
            raise ExternalToolError(f"unexpected framemd5 row: {line}")
        frames.append(
            {
                "stream": int(fields[0]),
                "dts": int(fields[1]),
                "pts": int(fields[2]),
                "duration": int(fields[3]),
                "size": int(fields[4]),
                "sha256": fields[5],
            }
        )
    if not frames:
        raise ExternalToolError("framemd5 emitted no decoded video frames")
    ledger: Dict[str, Any] = {"pixel_format": "gbrp16le", "frame_count": len(frames), "frames": frames}
    ledger["ledger_sha256"] = recipe_hash(ledger)
    return ledger


def _has_audio_stream(path: Path) -> bool:
    """True when the file carries an audio stream. Works on audio-only files (a licensed track)
    as well as on video containers, unlike `probe_media`, which requires a video stream."""
    command = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", str(Path(path).resolve())]
    return bool(json.loads(run_checked(command).stdout).get("streams"))


def audio_payload_sha256(path: Path) -> Optional[str]:
    if not _has_audio_stream(path):
        return None
    result = run_checked(
        ["ffmpeg", "-v", "error", "-i", str(Path(path).resolve()), "-map", "0:a:0", "-c", "copy", "-f", "data", "-"],
        capture_binary=True,
    )
    import hashlib

    return hashlib.sha256(result.stdout).hexdigest()


def audio_packet_ledger(path: Path) -> Optional[Dict[str, Any]]:
    if not _has_audio_stream(path):
        return None
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_packets",
        "-show_data_hash",
        "sha256",
        "-show_entries",
        "packet=pts,dts,duration,size,flags,data_hash",
        "-of",
        "json",
        str(Path(path).resolve()),
    ]
    payload = json.loads(run_checked(command).stdout)
    packets = []
    for raw in payload.get("packets", []):
        packets.append(
            {
                "pts": int(raw["pts"]) if raw.get("pts") not in {None, "N/A"} else None,
                "dts": int(raw["dts"]) if raw.get("dts") not in {None, "N/A"} else None,
                "duration": int(raw["duration"]) if raw.get("duration") not in {None, "N/A"} else None,
                "size": int(raw["size"]),
                "flags": str(raw.get("flags", "")),
                "data_hash": str(raw.get("data_hash", "")),
            }
        )
    ledger: Dict[str, Any] = {"packet_count": len(packets), "packets": packets}
    ledger["ledger_sha256"] = recipe_hash(ledger)
    return ledger


def decode_pcm_sha256(path: Path, sample_rate: int = 48000) -> Optional[str]:
    if not _has_audio_stream(path):
        return None
    result = run_checked(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(Path(path).resolve()),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-",
        ],
        capture_binary=True,
    )
    import hashlib

    return hashlib.sha256(result.stdout).hexdigest()


def full_decode(path: Path) -> Dict[str, Any]:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(Path(path).resolve()),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-f",
        "null",
        "-",
    ]
    result = run_checked(command)
    return {"status": "PASS", "exit_code": result.returncode}


def legal_luma_range(path: Path) -> Dict[str, Any]:
    facts = probe_media(path, count_frames=False)
    pix_fmt = str(facts["video"].get("pix_fmt") or "")
    bit_depth = 12 if "12" in pix_fmt else 10 if "10" in pix_fmt else 8
    scale = 1 << (bit_depth - 8)
    legal_min, legal_max = 16 * scale, 235 * scale
    result = run_checked(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(Path(path).resolve()),
            "-vf",
            "signalstats,metadata=print:file=-",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    minimums = [int(value) for value in re.findall(r"lavfi\.signalstats\.YMIN=(\d+)", result.stdout)]
    maximums = [int(value) for value in re.findall(r"lavfi\.signalstats\.YMAX=(\d+)", result.stdout)]
    if not minimums or len(minimums) != len(maximums):
        raise ExternalToolError("signalstats did not emit a complete luma ledger")
    failures = [
        {"frame": index, "ymin": low, "ymax": high}
        for index, (low, high) in enumerate(zip(minimums, maximums))
        if low < legal_min or high > legal_max
    ]
    return {
        "status": "PASS" if not failures else "FAIL",
        "bit_depth": bit_depth,
        "legal_min": legal_min,
        "legal_max": legal_max,
        "observed_min": min(minimums),
        "observed_max": max(maximums),
        "frame_count": len(minimums),
        "failures": failures,
    }
