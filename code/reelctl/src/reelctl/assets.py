from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from .authorization import authorize_asset_manifest
from .hashing import atomic_write_bytes, atomic_write_json, load_json, recipe_hash, sha256_file
from .identifiers import validate_identifier
from .paths import PathSafetyError, canonical_root, confined_path, secure_mkdirs
from .signing import SignatureError, sign_payload, verify_payload
from .typography import render_exact_font_plate, validate_typography_layer


class AssetContractError(ValueError):
    pass


def _validate_interval(layer: Dict[str, Any], frame_count: int) -> None:
    start = int(layer.get("start_frame", -1))
    end = int(layer.get("end_frame_exclusive", -1))
    if start < 0 or end <= start or end > frame_count:
        raise AssetContractError(
            f"layer {layer.get('id', '<unnamed>')} has invalid half-open interval [{start}, {end}) for {frame_count} frames"
        )


def _resolve(path: str, root: Optional[Path] = None, *, allow_external: bool = False, require_file: bool = True) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() and root is not None:
        value = root / value
    value = value.absolute()
    if root is not None:
        trusted = canonical_root(root)
        try:
            return confined_path(trusted, value, require="file" if require_file else None, allow_missing=not require_file)
        except PathSafetyError:
            if not allow_external:
                raise
    if value.is_symlink() or (require_file and not value.is_file()):
        raise AssetContractError(f"asset is a symlink or missing regular file: {value}")
    return value


def _sequence_frame(pattern: str, frame: int, root: Optional[Path]) -> Path:
    if pattern.count("%06d") != 1:
        raise AssetContractError("effect sequence_pattern must contain exactly one %06d placeholder")
    path = Path(pattern.replace("%06d", f"{frame:06d}"))
    if root is not None:
        return confined_path(canonical_root(root), path, require="file", allow_missing=False)
    if path.is_symlink() or not path.is_file():
        raise AssetContractError(f"effect sequence frame is a symlink or missing regular file: {path}")
    return path


def _prepare_output_directory(output_dir: Path, root: Optional[Path]) -> Path:
    output_dir = Path(output_dir).absolute()
    trusted = canonical_root(root) if root is not None else canonical_root(output_dir.parent)
    confined = confined_path(trusted, output_dir, allow_missing=True)
    relative = confined.relative_to(trusted)
    return secure_mkdirs(trusted, *relative.parts)


def _write_png_atomic(image: "Any", target: Path, *, root: Path) -> None:
    buffer = BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    atomic_write_bytes(target, buffer.getvalue(), root=root, mode=0o600)


def _effect_layer_core(layer: Dict[str, Any], *, frame_count: int, root: Optional[Path]) -> Dict[str, Any]:
    _validate_interval(layer, frame_count)
    scalar_keys = ("id", "proof", "start_frame", "end_frame_exclusive", "source_frame", "source_sha256")
    scalar = {key: layer.get(key) for key in scalar_keys if key in layer}
    assets: List[Dict[str, Any]] = []
    if layer.get("sequence_pattern"):
        pattern = str(_resolve(str(layer["sequence_pattern"]), root, require_file=False))
        scalar["sequence_pattern"] = pattern
        for frame in range(int(layer["start_frame"]), int(layer["end_frame_exclusive"])):
            path = _sequence_frame(pattern, frame, root)
            assets.append({"frame": frame, "path": str(path), "sha256": sha256_file(path)})
    else:
        path = _resolve(str(layer.get("plate_path", "")), root)
        scalar["plate_path"] = str(path)
        scalar["plate_sha256"] = sha256_file(path)
        assets.append({"path": str(path), "sha256": sha256_file(path)})
    return {"layer": scalar, "assets": assets}


def create_effect_provenance(
    layer: Dict[str, Any],
    reference_path: Path,
    receipt_path: Path,
    *,
    frame_count: int,
    evidence: str,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    reference_path = Path(reference_path).expanduser().resolve()
    if reference_path.is_symlink() or not reference_path.is_file():
        raise AssetContractError("effect provenance requires existing reference media")
    evidence = str(evidence).strip()
    if not evidence or evidence.upper().startswith(("PENDING_", "REPLACE_")):
        raise AssetContractError("effect provenance requires concrete reference evidence")
    core = _effect_layer_core(layer, frame_count=frame_count, root=root)
    receipt = sign_payload(
        {
            "schema_version": 1,
            "status": "PASS",
            "proof": layer.get("proof"),
            "reference_path": str(reference_path),
            "reference_sha256": sha256_file(reference_path),
            "evidence": evidence,
            "layer_recipe_hash": recipe_hash(core),
            **core,
        },
        purpose="effect-layer-provenance-v1",
    )
    receipt_path = Path(receipt_path).expanduser().absolute()
    atomic_write_json(receipt_path, receipt, root=receipt_path.parent)
    return {**receipt, "receipt_path": str(receipt_path), "receipt_sha256": sha256_file(receipt_path)}


def _verify_effect_provenance(layer: Dict[str, Any], *, frame_count: int, root: Optional[Path]) -> Dict[str, Any]:
    receipt_path = _resolve(str(layer.get("provenance_receipt_path", "")), root)
    expected_hash = str(layer.get("provenance_receipt_sha256", ""))
    if not expected_hash or sha256_file(receipt_path) != expected_hash:
        raise AssetContractError(f"effect layer {layer.get('id')} provenance receipt hash mismatch")
    receipt = load_json(receipt_path, root=root)
    try:
        verify_payload(receipt, purpose="effect-layer-provenance-v1")
    except SignatureError as exc:
        raise AssetContractError(f"effect layer {layer.get('id')} provenance signature is invalid: {exc}") from exc
    reference = Path(str(receipt.get("reference_path", "")))
    core = _effect_layer_core(layer, frame_count=frame_count, root=root)
    checks = {
        "status": receipt.get("status") == "PASS",
        "proof": receipt.get("proof") == layer.get("proof"),
        "reference": reference.is_file() and sha256_file(reference) == receipt.get("reference_sha256"),
        "layer_recipe": receipt.get("layer_recipe_hash") == recipe_hash(core),
        "layer": receipt.get("layer") == core["layer"],
        "assets": receipt.get("assets") == core["assets"],
        "evidence": bool(str(receipt.get("evidence", "")).strip()),
    }
    if not all(checks.values()):
        raise AssetContractError(f"effect layer {layer.get('id')} does not match its signed reference provenance")
    return {"status": "PASS", "reference_sha256": receipt["reference_sha256"], "layer_recipe_hash": receipt["layer_recipe_hash"]}


def validate_assets_manifest(
    manifest: Dict[str, Any],
    *,
    frame_count: int,
    root: Optional[Path] = None,
    footage_root: Optional[Path] = None,
) -> Dict[str, Any]:
    if int(manifest.get("schema_version", 0)) != 1:
        raise AssetContractError("assets manifest schema_version must be 1")
    if footage_root is not None:
        authorize_asset_manifest(manifest, footage_root=footage_root, project_dir=root)
    records: List[Dict[str, Any]] = []
    for layer in manifest.get("typography_layers", []):
        _validate_interval(layer, frame_count)
        normalized = dict(layer)
        if root:
            if normalized.get("font_path"):
                normalized["font_path"] = str(_resolve(str(normalized["font_path"]), root, allow_external=True))
            if normalized.get("plate_path"):
                normalized["plate_path"] = str(_resolve(str(normalized["plate_path"]), root))
            if normalized.get("provenance_receipt_path"):
                normalized["provenance_receipt_path"] = str(_resolve(str(normalized["provenance_receipt_path"]), root))
            if normalized.get("reference_match", {}).get("reference_path"):
                normalized["reference_match"] = dict(normalized["reference_match"])
                normalized["reference_match"]["reference_path"] = str(_resolve(str(normalized["reference_match"]["reference_path"]), root))
        records.append(validate_typography_layer(normalized))
    for layer in manifest.get("effect_layers", []):
        _validate_interval(layer, frame_count)
        proof = layer.get("proof")
        if proof not in {"reference_extracted_plate", "reference_rebuilt_exact", "effect_source_hash_locked"}:
            raise AssetContractError(f"effect layer {layer.get('id')} lacks exact provenance")
        if layer.get("sequence_pattern"):
            pattern = str(_resolve(str(layer["sequence_pattern"]), root, require_file=False))
            for frame in range(int(layer["start_frame"]), int(layer["end_frame_exclusive"])):
                _sequence_frame(pattern, frame, root)
        else:
            path = _resolve(str(layer.get("plate_path", "")), root)
            expected = str(layer.get("plate_sha256", ""))
            if not path.is_file() or not expected or sha256_file(path) != expected:
                raise AssetContractError(f"effect layer {layer.get('id')} plate hash mismatch")
        provenance = _verify_effect_provenance(layer, frame_count=frame_count, root=root)
        records.append({"status": "PASS", "id": layer.get("id"), "proof": proof, "provenance": provenance})
    return {"status": "PASS", "layers": len(records), "records": records}


def _input_asset_receipts(manifest: Dict[str, Any], *, frame_count: int, root: Optional[Path]) -> List[Dict[str, Any]]:
    receipts: List[Dict[str, Any]] = []
    for layer in manifest.get("typography_layers", []):
        if layer.get("font_path"):
            font = _resolve(str(layer["font_path"]), root, allow_external=True)
            receipts.append({"layer": layer.get("id"), "kind": "font", "path": str(font), "sha256": sha256_file(font)})
        if layer.get("plate_path"):
            plate = _resolve(str(layer["plate_path"]), root)
            receipts.append({"layer": layer.get("id"), "kind": "plate", "path": str(plate), "sha256": sha256_file(plate)})
    for layer in manifest.get("effect_layers", []):
        if layer.get("sequence_pattern"):
            pattern = str(_resolve(str(layer["sequence_pattern"]), root, require_file=False))
            for frame in range(int(layer["start_frame"]), int(layer["end_frame_exclusive"])):
                path = _sequence_frame(pattern, frame, root)
                receipts.append(
                    {"layer": layer.get("id"), "kind": "effect-frame", "frame": frame, "path": str(path), "sha256": sha256_file(path)}
                )
        else:
            plate = _resolve(str(layer["plate_path"]), root)
            receipts.append({"layer": layer.get("id"), "kind": "effect-plate", "path": str(plate), "sha256": sha256_file(plate)})
    return receipts


def compose_overlay_sequence(
    manifest: Dict[str, Any],
    output_dir: Path,
    *,
    width: int,
    height: int,
    frame_count: int,
    root: Optional[Path] = None,
    footage_root: Optional[Path] = None,
) -> Dict[str, Any]:
    from PIL import Image

    validate_assets_manifest(manifest, frame_count=frame_count, root=root, footage_root=footage_root)
    input_asset_receipts = _input_asset_receipts(manifest, frame_count=frame_count, root=root)
    current_recipe = recipe_hash(
        {"manifest": manifest, "input_assets": input_asset_receipts, "width": width, "height": height, "frame_count": frame_count}
    )
    output_dir = Path(output_dir).absolute()
    if output_dir.is_symlink():
        raise PathSafetyError(f"overlay output directory is a symlink: {output_dir}")
    if output_dir.exists() and any(output_dir.iterdir()):
        receipt_path = output_dir / "overlay-receipt.json"
        if receipt_path.is_file():
            receipt = load_json(receipt_path, root=output_dir)
            try:
                verify_payload(receipt, purpose="overlay-sequence-receipt-v1")
            except SignatureError as exc:
                raise AssetContractError(f"overlay cache receipt signature is invalid: {exc}") from exc
            expected = receipt.get("recipe_hash")
            hashes = receipt.get("frame_sha256", [])
            cache_valid = expected == current_recipe and len(hashes) == frame_count
            for frame, expected_hash in enumerate(hashes if cache_valid else []):
                target = confined_path(output_dir, f"{frame:06d}.png", require="file", allow_missing=False)
                if sha256_file(target) != expected_hash:
                    cache_valid = False
                    break
            if cache_valid:
                return {**receipt, "cache_hit": True}
        raise AssetContractError(f"refusing non-empty overlay output without matching receipt: {output_dir}")
    output_dir = _prepare_output_directory(output_dir, root)
    layers = []
    for source_layer in list(manifest.get("typography_layers", [])) + list(manifest.get("effect_layers", [])):
        layer = dict(source_layer)
        if layer.get("font_path"):
            layer["font_path"] = str(_resolve(str(layer["font_path"]), root, allow_external=True))
        if layer.get("plate_path"):
            layer["plate_path"] = str(_resolve(str(layer["plate_path"]), root))
        if layer.get("proof") == "exact_font" and not layer.get("plate_path"):
            layer_id = validate_identifier(str(layer.get("id", "font-layer")), kind="asset layer")
            layer_dir = secure_mkdirs(output_dir, ".layers")
            generated = layer_dir / f"{layer_id}.png"
            receipt = render_exact_font_plate(layer, generated, width=width, height=height)
            layer["plate_path"] = receipt["path"]
            layer["plate_sha256"] = receipt["sha256"]
        layers.append(layer)
    frame_hashes: List[str] = []
    for frame in range(frame_count):
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        for layer in layers:
            if frame < int(layer["start_frame"]) or frame >= int(layer["end_frame_exclusive"]):
                continue
            if layer.get("sequence_pattern"):
                pattern = str(_resolve(str(layer["sequence_pattern"]), root, require_file=False))
                path = _sequence_frame(pattern, frame, root)
            else:
                raw_path = layer.get("plate_path")
                path = _resolve(str(raw_path), root)
            with Image.open(path) as source:
                image = source.convert("RGBA")
            if image.size != (width, height):
                raise AssetContractError(f"overlay plate {path} is {image.size}; expected {(width, height)}")
            canvas = Image.alpha_composite(canvas, image)
        target = output_dir / f"{frame:06d}.png"
        _write_png_atomic(canvas, target, root=output_dir)
        frame_hashes.append(sha256_file(target))
    receipt = sign_payload(
        {
            "schema_version": 1,
            "status": "PASS",
            "frame_count": frame_count,
            "width": width,
            "height": height,
            "pattern": str(output_dir / "%06d.png"),
            "frame_sha256": frame_hashes,
            "input_assets": input_asset_receipts,
            "recipe_hash": current_recipe,
        },
        purpose="overlay-sequence-receipt-v1",
    )
    atomic_write_json(output_dir / "overlay-receipt.json", receipt, root=output_dir)
    return {**receipt, "cache_hit": False}
