from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reelctl.color import create_camera_profile_proof, ffmpeg_color_filter, validate_color_contract
from reelctl.grading import install_color_profile
from reelctl.hashing import sha256_file
from reelctl.signing import sign_payload


_PACKAGED_LUT = Path(__file__).resolve().parents[1] / "src/reelctl/data/luts/Sony-LC-709-official.cube"


@pytest.mark.skipif(
    not _PACKAGED_LUT.is_file(),
    reason="the vendor LUT is not redistributed with this repository; download Sony's official LC-709 LUT into src/reelctl/data/luts/",
)
def test_packaged_sony_lut_installs_and_executes_in_ffmpeg(tmp_path: Path) -> None:
    project = tmp_path / "project"
    receipt = install_color_profile(project, "sony_slog3_sgamut3cine")
    lut = Path(receipt["lut"])
    assert lut.is_file()
    assert sha256_file(lut) == "d41aeebbca5c4df100f1e6b53b739bd1e7bc38e49890812a14ba7275ebfbbf94"
    source_sample = tmp_path / "source-sample.bin"
    reference_sample = tmp_path / "reference-sample.bin"
    source_sample.write_bytes(b"source-grade-sample")
    reference_sample.write_bytes(b"reference-grade-sample")
    source_hash = "a" * 64
    profile_evidence = tmp_path / "camera-profile.txt"
    profile_evidence.write_text("Sony source documentation: S-Log3/S-Gamut3.Cine, full-range recording", encoding="utf-8")
    profile_proof = sign_payload(
        {
            "schema_version": 1,
            "status": "AGENT_VERIFIED",
            "method": "source_documentation",
            "source_sha256": source_hash,
            "input_range": "full",
            "evidence": "camera source documentation identifies S-Log3/S-Gamut3.Cine",
            "evidence_path": str(profile_evidence),
            "evidence_sha256": sha256_file(profile_evidence),
        },
        purpose="color-profile-proof-v1",
    )
    slot = {
        "block_id": "p001",
        "source_start_frame": 0,
        "source_sha256": source_hash,
        "lighting_family": "day-interior",
        "input_profile": "sony_slog3_sgamut3cine",
        "input_range": "full",
        "profile_proof": profile_proof,
        "technical_transform": "sony_lc709",
        "technical_lut": str(lut),
        "technical_lut_sha256": sha256_file(lut),
        "creative": {"exposure_stops": 0.1, "contrast": 1.02, "saturation": 1.01, "gamma": 1.0},
        "grade_proof": {
            "status": "AGENT_REVIEWED",
            "block_id": "p001",
            "source_frame": 0,
            "source_sample": str(source_sample),
            "source_sample_sha256": sha256_file(source_sample),
            "reference_sample": str(reference_sample),
            "reference_sample_sha256": sha256_file(reference_sample),
        },
    }
    assert validate_color_contract([slot])["status"] == "PASS"
    output = tmp_path / "normalized.png"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=320x180:r=24:d=0.1",
            "-vf",
            ffmpeg_color_filter(slot),
            "-frames:v",
            "1",
            str(output),
        ],
        check=True,
    )
    assert output.is_file() and output.stat().st_size > 0
    filtergraph = ffmpeg_color_filter(slot)
    assert "in_range=full:out_range=full" in filtergraph
    assert "lut3d=" in filtergraph
    assert "colorspace=ispace=gbr" in filtergraph
    assert "range=tv:format=yuv422p10:dither=fsb" in filtergraph


def test_camera_profile_proof_reads_nested_sony_xml_metadata(tmp_path: Path) -> None:
    import os

    import pytest

    configured = os.environ.get("REELCTL_SONY_XML_CLIP")
    source = Path(configured).expanduser() if configured else None
    if source is None or not source.is_file():
        pytest.skip("set REELCTL_SONY_XML_CLIP to a Sony S-Log3 clip with its XML sidecar to run this check")
    proof = create_camera_profile_proof(source, tmp_path / "profile-proof.json")
    assert proof["status"] == "AGENT_VERIFIED"
    assert proof["method"] == "camera_metadata"
    assert proof["device_model"]  # whatever body wrote the sidecar
    assert proof["capture_gamma"] == "s-log3-cine"
    assert proof["capture_primaries"] == "s-gamut3-cine"
    assert proof["coding_equations"] == "rec709"
    assert proof["input_range"] in {"full", "tv"}
