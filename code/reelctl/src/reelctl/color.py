from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .hashing import atomic_write_json, load_json, sha256_file
from .media import probe_media, run_checked
from .signing import SignatureError, sign_payload, verify_payload


class ColorContractError(ValueError):
    pass


DATA_ROOT = Path(__file__).resolve().parent / "data"
PROFILE_REGISTRY = DATA_ROOT / "color-profiles.json"
SUPPORTED_INPUT_PROFILES = {
    "rec709": "identity",
    "sony_slog3_sgamut3cine": "lut_required",
    "custom_log": "lut_required",
}
_PROFILE_METHODS = {
    "rec709": {"camera_metadata_rec709", "scope_verified_display_referred", "source_documentation_rec709"},
    "sony_slog3_sgamut3cine": {"camera_metadata", "source_documentation"},
    "custom_log": {"verified_custom_input_transform"},
}


def _normal_profile_value(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def sony_xml_metadata(path: Path) -> Dict[str, Any]:
    """Read Sony's nested AcquisitionRecord XML pairs from the media file.

    ExifTool does not expose these nested values through a direct
    ``-CaptureGammaEquation`` query; the adjacent name/value rows must be paired.
    """

    path = Path(path).expanduser().resolve()
    output = run_checked(["exiftool", "-G1", "-a", "-s", "-api", "LargeFileSupport=1", str(path)]).stdout
    device_model: Optional[str] = None
    pairs: Dict[str, str] = {}
    pending_name: Optional[str] = None
    for line in output.splitlines():
        if "DeviceModelName" in line and ":" in line:
            device_model = line.split(":", 1)[1].strip()
            continue
        if "AcquisitionRecordGroupItemName" in line and ":" in line:
            pending_name = line.split(":", 1)[1].strip()
            continue
        if pending_name and "AcquisitionRecordGroupItemValue" in line and ":" in line:
            pairs[pending_name] = line.split(":", 1)[1].strip()
            pending_name = None
    return {
        "device_model": device_model,
        "capture_gamma": pairs.get("CaptureGammaEquation"),
        "capture_primaries": pairs.get("CaptureColorPrimaries"),
        "coding_equations": pairs.get("CodingEquations"),
        "raw_pairs": pairs,
    }


def create_camera_profile_proof(source: Path, receipt_path: Optional[Path] = None) -> Dict[str, Any]:
    source = Path(source).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise ColorContractError(f"camera profile source is missing or a symlink: {source}")
    metadata = sony_xml_metadata(source)
    expected = {
        "capture_gamma": "s-log3-cine",
        "capture_primaries": "s-gamut3-cine",
        "coding_equations": "rec709",
    }
    mismatches = [key for key, value in expected.items() if _normal_profile_value(metadata.get(key)) != _normal_profile_value(value)]
    if mismatches:
        raise ColorContractError(
            "unsupported or ambiguous Sony camera profile; refusing to guess: "
            + ", ".join(f"{key}={metadata.get(key)!r}" for key in expected)
        )
    facts = probe_media(source, count_frames=False)
    declared_range = str(facts["video"].get("color_range") or "")
    if declared_range not in {"pc", "tv"}:
        raise ColorContractError(f"source video range is not declared: {declared_range!r}")
    input_range = "full" if declared_range == "pc" else "tv"
    proof = sign_payload(
        {
            "schema_version": 1,
            "status": "AGENT_VERIFIED",
            "method": "camera_metadata",
            "source_path": str(source),
            "source_sha256": sha256_file(source),
            "device_model": metadata["device_model"],
            "capture_gamma": metadata["capture_gamma"],
            "capture_primaries": metadata["capture_primaries"],
            "coding_equations": metadata["coding_equations"],
            "input_range": input_range,
            "container_color_range": declared_range,
            "pixel_format": facts["video"].get("pix_fmt"),
            "evidence": "paired Sony XML AcquisitionRecord name/value rows plus ffprobe stream range",
        },
        purpose="color-profile-proof-v1",
    )
    if receipt_path is not None:
        receipt_path = Path(receipt_path).expanduser().absolute()
        atomic_write_json(receipt_path, proof, root=receipt_path.parent)
    return proof


def _is_default_creative(creative: Dict[str, Any]) -> bool:
    defaults = {"exposure_stops": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0}
    return all(abs(float(creative.get(key, default)) - default) <= 1e-9 for key, default in defaults.items())


def _verify_hashed_file(path_value: Any, digest_value: Any, *, label: str) -> Path:
    path = Path(str(path_value)).expanduser()
    digest = str(digest_value or "")
    if not path.is_file() or len(digest) != 64 or sha256_file(path) != digest:
        raise ColorContractError(f"{label} is missing or its SHA-256 does not match")
    return path


def _validate_profile_proof(slot: Dict[str, Any], profile: str, index: int) -> None:
    proof = slot.get("profile_proof")
    if not isinstance(proof, dict) or proof.get("status") != "AGENT_VERIFIED":
        raise ColorContractError(f"slot {index}: input profile lacks an AGENT_VERIFIED proof")
    try:
        verify_payload(proof, purpose="color-profile-proof-v1")
    except SignatureError as exc:
        raise ColorContractError(f"slot {index}: color profile proof signature is invalid: {exc}") from exc
    method = str(proof.get("method", ""))
    if method not in _PROFILE_METHODS[profile]:
        raise ColorContractError(f"slot {index}: profile proof method is not valid for {profile}")
    source_hash = str(slot.get("source_sha256", ""))
    if not source_hash or proof.get("source_sha256") != source_hash:
        raise ColorContractError(f"slot {index}: profile proof is not bound to the selected source bytes")
    input_range = str(slot.get("input_range", ""))
    if input_range not in {"full", "tv"} or proof.get("input_range") != input_range:
        raise ColorContractError(f"slot {index}: source range is missing or differs from its profile proof")
    evidence = str(proof.get("evidence", "")).strip()
    if not evidence or evidence.upper().startswith(("PENDING_", "REPLACE_")):
        raise ColorContractError(f"slot {index}: profile proof lacks evidence")
    if method == "camera_metadata":
        expected = {
            "capture_gamma": "s-log3-cine",
            "capture_primaries": "s-gamut3-cine",
            "coding_equations": "rec709",
        }
        if any(_normal_profile_value(proof.get(key)) != _normal_profile_value(value) for key, value in expected.items()):
            raise ColorContractError(f"slot {index}: Sony XML metadata does not prove S-Log3/S-Gamut3.Cine")
        source_path = Path(str(proof.get("source_path", "")))
        if not source_path.is_file() or sha256_file(source_path) != source_hash:
            raise ColorContractError(f"slot {index}: camera metadata proof source is missing or changed")
    elif method in {"source_documentation", "source_documentation_rec709", "verified_custom_input_transform"}:
        _verify_hashed_file(
            proof.get("evidence_path"),
            proof.get("evidence_sha256"),
            label=f"slot {index} profile documentation",
        )


def _validate_grade_proof(slot: Dict[str, Any], index: int, *, require_samples: bool) -> None:
    proof = slot.get("grade_proof")
    if not isinstance(proof, dict) or proof.get("status") not in {"PASS", "AGENT_REVIEWED"}:
        raise ColorContractError(f"slot {index}: source-aware grade proof is not reviewed")
    if not require_samples:
        return
    if str(proof.get("block_id")) != str(slot.get("block_id")):
        raise ColorContractError(f"slot {index}: grade proof is not bound to its reference block")
    if int(proof.get("source_frame", -1)) != int(slot.get("source_start_frame", -2)):
        raise ColorContractError(f"slot {index}: grade proof is not bound to its selected source frame")
    _verify_hashed_file(proof.get("source_sample"), proof.get("source_sample_sha256"), label=f"slot {index} source grade sample")
    _verify_hashed_file(
        proof.get("reference_sample"),
        proof.get("reference_sample_sha256"),
        label=f"slot {index} reference grade sample",
    )


def validate_color_contract(slots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not slots:
        raise ColorContractError("color contract has no slots")
    registry = load_json(PROFILE_REGISTRY)
    by_recipe: Dict[str, set] = {}
    for index_zero, slot in enumerate(slots):
        index = index_zero + 1
        profile = str(slot.get("input_profile", "log_unknown"))
        transform = str(slot.get("technical_transform", ""))
        if profile not in SUPPORTED_INPUT_PROFILES:
            raise ColorContractError(
                f"slot {index}: unknown/unsupported input profile {profile!r}; identify the exact camera gamma/gamut and verified input transform"
            )
        _validate_profile_proof(slot, profile, index)
        if profile == "rec709":
            if transform not in {"identity", "rec709_identity"}:
                raise ColorContractError(f"slot {index}: Rec.709 source cannot use {transform!r}")
            if slot.get("technical_lut"):
                raise ColorContractError(f"slot {index}: Rec.709 identity source cannot apply a technical LUT")
        elif profile == "sony_slog3_sgamut3cine":
            record = registry["profiles"][profile]
            if transform != record["technical_transform"]:
                raise ColorContractError(f"slot {index}: {profile} requires {record['technical_transform']}")
            _verify_hashed_file(slot.get("technical_lut"), slot.get("technical_lut_sha256"), label=f"slot {index} technical LUT")
            if slot.get("technical_lut_sha256") != record["lut_sha256"]:
                raise ColorContractError(f"slot {index}: Sony profile requires the hash-locked official LC-709 LUT")
        else:
            if transform != "custom_lut":
                raise ColorContractError(f"slot {index}: custom_log requires technical_transform=custom_lut")
            _verify_hashed_file(slot.get("technical_lut"), slot.get("technical_lut_sha256"), label=f"slot {index} custom technical LUT")

        creative = slot.get("creative", {})
        if not isinstance(creative, dict):
            raise ColorContractError(f"slot {index}: creative grade must be an object")
        values = {
            "exposure_stops": float(creative.get("exposure_stops", 0.0)),
            "contrast": float(creative.get("contrast", 1.0)),
            "saturation": float(creative.get("saturation", 1.0)),
            "gamma": float(creative.get("gamma", 1.0)),
        }
        if not all(math.isfinite(value) for value in values.values()):
            raise ColorContractError(f"slot {index}: creative grade contains a non-finite value")
        if not -1.5 <= values["exposure_stops"] <= 1.0:
            raise ColorContractError("exposure correction exceeds the safe -1.5..+1.0 stop bound")
        if not 0.75 <= values["contrast"] <= 1.3 or not 0.75 <= values["saturation"] <= 1.3 or not 0.8 <= values["gamma"] <= 1.2:
            raise ColorContractError("creative correction exceeds bounded grade limits")
        _validate_grade_proof(slot, index, require_samples=not _is_default_creative(creative) or profile != "rec709")
        lighting = str(slot.get("lighting_family", "")).strip()
        if not lighting or lighting.upper().startswith(("PENDING_", "REPLACE_")):
            raise ColorContractError(f"slot {index}: lighting_family is not independently classified")
        key = repr(sorted((key, round(value, 6)) for key, value in values.items()))
        by_recipe.setdefault(key, set()).add(lighting)
    for recipe, families in by_recipe.items():
        if len(families) > 1 and recipe != repr(sorted({"exposure_stops": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0}.items())):
            raise ColorContractError(
                "one creative grade is reused across different lighting families; technical normalization may be shared, creative grading must be source-aware"
            )
    return {"status": "PASS", "slots": len(slots), "profiles": sorted({str(slot.get("input_profile")) for slot in slots})}


def ffmpeg_color_filter(slot: Dict[str, Any]) -> str:
    profile = str(slot.get("input_profile", "log_unknown"))
    if profile not in SUPPORTED_INPUT_PROFILES:
        raise ColorContractError(f"unsupported input profile: {profile}")
    input_range = str(slot.get("input_range", ""))
    if input_range not in {"full", "tv"}:
        raise ColorContractError("source input_range must be proven as full or tv before rendering")
    filters: List[str]
    if profile == "rec709":
        filters = []
        if input_range == "full":
            filters.append("scale=iw:ih:flags=lanczos:in_range=full:out_range=tv:in_color_matrix=bt709:out_color_matrix=bt709")
    else:
        filters = [
            f"scale=iw:ih:flags=lanczos:in_range={input_range}:out_range=full:in_color_matrix=bt709",
            "format=gbrpf32le",
        ]
        lut = Path(str(slot.get("technical_lut", ""))).resolve()
        if not lut.is_file():
            raise ColorContractError(f"technical LUT missing: {lut}")
        expected = str(slot.get("technical_lut_sha256", ""))
        if not expected or sha256_file(lut) != expected:
            raise ColorContractError("technical LUT changed after color contract validation")
        escaped = str(lut).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        filters.append(f"lut3d=file='{escaped}':interp=tetrahedral")
    creative = slot.get("creative", {})
    exposure = float(creative.get("exposure_stops", 0.0))
    contrast = float(creative.get("contrast", 1.0))
    saturation = float(creative.get("saturation", 1.0))
    gamma = float(creative.get("gamma", 1.0))
    if not all(math.isfinite(value) for value in (exposure, contrast, saturation, gamma)):
        raise ColorContractError("creative correction contains non-finite values")
    if not -1.5 <= exposure <= 1.0:
        raise ColorContractError("exposure correction exceeds the safe -1.5..+1.0 stop bound")
    if not 0.75 <= contrast <= 1.3 or not 0.75 <= saturation <= 1.3 or not 0.8 <= gamma <= 1.2:
        raise ColorContractError("creative correction exceeds bounded grade limits")
    exposure_factor = math.pow(2.0, exposure)
    if abs(exposure_factor - 1.0) > 1e-9:
        filters.append(f"colorchannelmixer=rr={exposure_factor:.8f}:gg={exposure_factor:.8f}:bb={exposure_factor:.8f}")
    if any(abs(value - default) > 1e-9 for value, default in ((contrast, 1.0), (saturation, 1.0), (gamma, 1.0))):
        filters.append(f"eq=contrast={contrast:.8f}:saturation={saturation:.8f}:gamma={gamma:.8f}")
    if profile == "rec709":
        filters += ["format=yuv422p10le", "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"]
    else:
        filters += [
            "format=gbrp16le",
            "colorspace=ispace=gbr:iprimaries=bt709:itrc=bt709:irange=pc:all=bt709:range=tv:format=yuv422p10:dither=fsb",
        ]
    return ",".join(filters)


def image_metrics(image: "Any") -> Dict[str, float]:
    import cv2
    import numpy as np

    array = image if hasattr(image, "shape") else cv2.imread(str(image), cv2.IMREAD_COLOR)
    if array is None:
        raise ColorContractError("could not read image for grade metrics")
    rgb = cv2.cvtColor(array, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    y = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    hsv = cv2.cvtColor((rgb * 255.0).astype(np.uint8), cv2.COLOR_RGB2HSV)
    return {
        "y_q10": float(np.quantile(y, 0.10)),
        "y_q50": float(np.quantile(y, 0.50)),
        "y_q90": float(np.quantile(y, 0.90)),
        "saturation_mean": float(hsv[..., 1].mean() / 255.0),
        "shadow_coverage": float((y <= 0.10).mean()),
        "highlight_coverage": float((y >= 0.90).mean()),
    }


def estimate_bounded_grade(source_metrics: Dict[str, float], target_metrics: Dict[str, float]) -> Dict[str, float]:
    source_mid = max(float(source_metrics["y_q50"]), 1e-4)
    target_mid = max(float(target_metrics["y_q50"]), 1e-4)
    exposure = max(-1.5, min(1.0, math.log(target_mid / source_mid, 2)))
    source_range = max(float(source_metrics["y_q90"]) - float(source_metrics["y_q10"]), 1e-4)
    target_range = max(float(target_metrics["y_q90"]) - float(target_metrics["y_q10"]), 1e-4)
    contrast = max(0.75, min(1.30, target_range / source_range))
    source_sat = max(float(source_metrics.get("saturation_mean", 0.2)), 1e-4)
    target_sat = max(float(target_metrics.get("saturation_mean", source_sat)), 1e-4)
    saturation = max(0.75, min(1.30, target_sat / source_sat))
    return {"exposure_stops": round(exposure, 4), "contrast": round(contrast, 4), "saturation": round(saturation, 4), "gamma": 1.0}


def color_receipt(slot: Dict[str, Any]) -> Dict[str, Any]:
    lut = slot.get("technical_lut")
    return {
        "input_profile": slot.get("input_profile"),
        "input_range": slot.get("input_range"),
        "profile_proof": slot.get("profile_proof"),
        "technical_transform": slot.get("technical_transform"),
        "technical_lut": str(lut) if lut else None,
        "technical_lut_sha256": sha256_file(Path(str(lut))) if lut and Path(str(lut)).is_file() else None,
        "lighting_family": slot.get("lighting_family"),
        "creative": slot.get("creative", {}),
        "grade_proof": slot.get("grade_proof"),
    }
