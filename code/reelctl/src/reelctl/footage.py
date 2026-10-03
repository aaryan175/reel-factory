from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .contracts import validate_contract
from .hashing import atomic_external_output, atomic_write_json, load_json, recipe_hash, sha256_file
from .media import probe_media, run_checked
from .paths import PathSafetyError, canonical_root, confined_path, secure_mkdirs

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mxf", ".avi"}


class FootageError(RuntimeError):
    pass


class FootageIndexError(FootageError):
    pass


def _thumbnail(path: Path, output: Path, time_s: float, *, root: Path) -> None:
    with atomic_external_output(output, root=root) as temporary:
        run_checked(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-ss",
                f"{time_s:.6f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                "-q:v",
                "3",
                str(temporary),
            ]
        )


def index_footage(
    root: Path,
    output_dir: Path,
    *,
    default_profile: str = "log_unknown",
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    try:
        root = canonical_root(root)
    except PathSafetyError as exc:
        raise FootageIndexError(str(exc)) from exc
    if project_root is not None:
        project_root = canonical_root(project_root)
        output_dir = confined_path(project_root, output_dir)
        secure_mkdirs(project_root, output_dir.relative_to(project_root))
        output_root = project_root
    else:
        output_dir = Path(output_dir).absolute()
        if output_dir.is_symlink():
            raise FootageIndexError(f"footage index output is a symlink: {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_root = canonical_root(output_dir)
    cache_path = output_dir / "footage-index.json"
    previous = load_json(cache_path) if cache_path.exists() else {"clips": []}
    previous_recipe = recipe_hash({key: value for key, value in previous.items() if key != "recipe_hash"})
    if previous.get("recipe_hash") != previous_recipe:
        previous = {"clips": []}
    by_identity = {(item.get("path"), item.get("sha256")): item for item in previous.get("clips", [])}
    entries = sorted(root.rglob("*"))
    unsafe_links = [path for path in entries if path.is_symlink()]
    if unsafe_links:
        raise FootageIndexError(f"footage authorization root contains a symlink: {unsafe_links[0]}")
    discovered = sorted(p for p in entries if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
    clips: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for path in discovered:
        stat = path.stat()
        content_hash = sha256_file(path)
        key = (str(path), content_hash)
        if key in by_identity:
            cached = dict(by_identity[key])
            thumbnail_receipts = cached.get("thumbnail_receipts", [])
            try:
                thumbnails_valid = len(thumbnail_receipts) == 4 and all(
                    sha256_file(confined_path(output_root, receipt["path"], require="file", allow_missing=False)) == receipt["sha256"]
                    for receipt in thumbnail_receipts
                )
            except (KeyError, PathSafetyError, OSError):
                thumbnails_valid = False
            if thumbnails_valid:
                cached.update({"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                clips.append(cached)
                continue
        try:
            facts = probe_media(path, precomputed_sha256=content_hash)
            duration = float(facts["format"].get("duration") or 0.0)
            record = {
                "clip_id": facts["sha256"][:16],
                "path": str(path),
                "relative_path": str(path.relative_to(root)),
                "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": facts["sha256"],
                "duration_s": duration,
                "video": facts["video"],
                "audio_present": facts["audio"] is not None,
                "input_profile": default_profile,
                "profile_status": "NEEDS_VERIFICATION" if default_profile == "log_unknown" else "PROJECT_DEFAULT_NEEDS_SPOT_CHECK",
                "independent_observation": "PENDING_AGENT_VISION",
                "status": "PASS",
            }
            thumbs: List[str] = []
            thumbnail_receipts = []
            for index, fraction in enumerate((0.08, 0.32, 0.56, 0.80), start=1):
                target = output_dir / "thumbnails" / f"{record['clip_id']}-{index}.jpg"
                if target.is_symlink():
                    raise FootageIndexError(f"thumbnail destination is a symlink: {target}")
                _thumbnail(path, target, max(0.0, duration * fraction), root=output_root)
                thumbs.append(str(target))
                thumbnail_receipts.append({"path": str(target), "sha256": sha256_file(target), "bytes": target.stat().st_size})
            record["thumbnails"] = thumbs
            record["thumbnail_receipts"] = thumbnail_receipts
            clips.append(record)
        except Exception as exc:
            failures.append(
                {
                    "path": str(path),
                    "relative_path": str(path.relative_to(root)),
                    "bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": content_hash,
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    groups: Dict[str, List[str]] = defaultdict(list)
    for clip in clips:
        groups[clip["sha256"]].append(clip["path"])
    duplicate_groups = [{"sha256": digest, "paths": paths} for digest, paths in sorted(groups.items()) if len(paths) > 1]
    manifest = {
        "schema_version": 1,
        "status": "PASS" if not failures and len(clips) == len(discovered) else "FAIL",
        "root": str(root),
        "expected_count": len(discovered),
        "probed_count": len(clips) + len(failures),
        "passed_count": len(clips),
        "failed_count": len(failures),
        "missing_count": len(discovered) - len(clips) - len(failures),
        "clip_count": len(clips),
        "unique_content_count": len(groups),
        "duplicate_groups": duplicate_groups,
        "clips": clips,
        "failures": failures,
    }
    manifest["recipe_hash"] = recipe_hash(manifest)
    validate_contract("footage-index", manifest)
    atomic_write_json(cache_path, manifest, root=project_root or output_dir)
    if manifest["status"] != "PASS":
        raise FootageIndexError(f"footage inventory incomplete: {len(failures)} failed; inspect {cache_path}")
    return manifest
