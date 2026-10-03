from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from reelctl.footage import index_footage
from reelctl.hashing import sha256_file


def make_raw_avi(path: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=160x90:r=24",
            "-frames:v",
            "4",
            "-c:v",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            str(path),
        ],
        check=True,
    )


def test_inventory_rehashes_same_path_size_mtime_and_groups_duplicates(tmp_path: Path) -> None:
    footage = tmp_path / "footage"
    footage.mkdir()
    clip = footage / "clip.avi"
    replacement = tmp_path / "replacement.avi"
    make_raw_avi(clip, "red")
    make_raw_avi(replacement, "blue")
    assert clip.stat().st_size == replacement.stat().st_size
    output = tmp_path / "index"
    first = index_footage(footage, output, default_profile="rec709")
    first_hash = first["clips"][0]["sha256"]
    original_stat = clip.stat()

    shutil.copyfile(replacement, clip)
    os.utime(clip, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert clip.stat().st_size == original_stat.st_size
    duplicate = footage / "duplicate.avi"
    shutil.copyfile(clip, duplicate)

    second = index_footage(footage, output, default_profile="rec709")
    assert second["clips"][0]["sha256"] != first_hash
    assert second["expected_count"] == second["passed_count"] == 2
    assert second["unique_content_count"] == 1
    assert len(second["duplicate_groups"]) == 1


def test_inventory_rebuilds_a_tampered_thumbnail_before_cache_reuse(tmp_path: Path) -> None:
    footage = tmp_path / "footage"
    footage.mkdir()
    make_raw_avi(footage / "clip.avi", "red")
    output = tmp_path / "index"
    first = index_footage(footage, output, default_profile="rec709")
    receipt = first["clips"][0]["thumbnail_receipts"][0]
    thumbnail = Path(receipt["path"])
    original_hash = receipt["sha256"]
    thumbnail.write_bytes(b"tampered")

    second = index_footage(footage, output, default_profile="rec709")
    rebuilt = second["clips"][0]["thumbnail_receipts"][0]
    assert rebuilt["sha256"] == original_hash
    assert sha256_file(thumbnail) == original_hash
