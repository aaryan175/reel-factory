from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import PIL
from fontTools.ttLib import TTCollection, TTFont
from PIL import Image

from .hashing import atomic_write_bytes, atomic_write_json, load_json, recipe_hash, sha256_file
from .paths import canonical_root, secure_mkdirs
from .signing import SignatureError, sign_payload, verify_payload


class TypographyError(ValueError):
    pass


def _configured_font_dirs() -> List[Path]:
    """Font search roots: REEL_FACTORY_FONT_DIRS (os.pathsep-separated) first, then system defaults.

    Only fonts you are licensed to use (or open-licensed faces such as OFL fonts) belong in these
    directories; the matcher analyses what is installed, it never acquires fonts.
    """

    configured = [Path(p).expanduser() for p in os.environ.get("REEL_FACTORY_FONT_DIRS", "").split(os.pathsep) if p.strip()]
    defaults = [
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path.home() / "Library/Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local/share/fonts",
    ]
    return configured + defaults


FONT_DIRS = _configured_font_dirs()


def _font_name(font: TTFont, name_id: int) -> str:
    for record in font["name"].names:
        if record.nameID == name_id:
            try:
                return record.toUnicode()
            except Exception:
                continue
    return ""


def inspect_font_face(path: Path, face_index: int = 0) -> Dict[str, Any]:
    """Parse and identify one concrete OpenType face without trusting its suffix."""

    path = Path(path)
    if not isinstance(face_index, int) or isinstance(face_index, bool) or face_index < 0:
        raise TypographyError("font face_index must be a non-negative integer")
    if path.is_symlink() or not path.is_file():
        raise TypographyError(f"font is a symlink or missing regular file: {path}")
    try:
        with path.open("rb") as handle:
            is_collection = handle.read(4) == b"ttcf"
        if is_collection:
            collection = TTCollection(str(path), lazy=True)
            face_count = len(collection.fonts)
            for item in collection.fonts:
                item.close()
            if face_index >= face_count:
                raise TypographyError(f"font face_index {face_index} is outside collection range 0..{face_count - 1}")
            font = TTFont(str(path), fontNumber=face_index, lazy=False)
        else:
            if face_index != 0:
                raise TypographyError("font face_index must be 0 for a non-collection font")
            face_count = 1
            font = TTFont(str(path), lazy=False)
    except TypographyError:
        raise
    except Exception as exc:
        raise TypographyError(f"font is not a valid OpenType/TrueType resource: {path}: {exc}") from exc

    try:
        cmap = font.getBestCmap() or {}
        features: set[str] = set()
        for table_name in ("GSUB", "GPOS"):
            if table_name not in font:
                continue
            feature_list = getattr(font[table_name].table, "FeatureList", None)
            if feature_list is not None:
                features.update(record.FeatureTag for record in feature_list.FeatureRecord)
        axes: List[Dict[str, Any]] = []
        if "fvar" in font:
            for axis in font["fvar"].axes:
                axes.append(
                    {
                        "tag": axis.axisTag,
                        "minimum": float(axis.minValue),
                        "default": float(axis.defaultValue),
                        "maximum": float(axis.maxValue),
                    }
                )
        head = font["head"]
        return {
            "face_index": face_index,
            "faces_in_file": face_count,
            "family": _font_name(font, 1),
            "subfamily": _font_name(font, 2),
            "full_name": _font_name(font, 4),
            "postscript_name": _font_name(font, 6),
            "font_revision": f"{float(head.fontRevision):.6f}",
            "units_per_em": int(head.unitsPerEm),
            "glyph_count": int(font["maxp"].numGlyphs),
            "cmap_sha256": recipe_hash([[codepoint, glyph] for codepoint, glyph in sorted(cmap.items())]),
            "variation_axes": axes,
            "available_features": sorted(features),
            "rasterizer": "pillow-freetype",
            "pillow_version": PIL.__version__,
        }
    except Exception as exc:
        raise TypographyError(f"font lacks required OpenType identity tables: {path}: {exc}") from exc
    finally:
        font.close()


def _font_missing_codepoints(path: Path, face_index: int, text: str) -> List[int]:
    try:
        font = TTFont(str(path), fontNumber=face_index, lazy=False)
    except Exception as exc:
        raise TypographyError(f"font is not a valid OpenType/TrueType resource: {path}: {exc}") from exc
    try:
        cmap = font.getBestCmap() or {}
        return sorted({ord(character) for character in text if not character.isspace() and ord(character) not in cmap})
    finally:
        font.close()


def _validate_rgba_plate(path: Path) -> Dict[str, Any]:
    try:
        with Image.open(path) as image:
            image.load()
            if image.format != "PNG" or image.mode != "RGBA" or image.width < 1 or image.height < 1:
                raise TypographyError(f"plate is not a valid RGBA PNG: {path}")
            if image.getchannel("A").getbbox() is None:
                raise TypographyError(f"plate RGBA PNG has no visible pixels: {path}")
            return {"format": image.format, "mode": image.mode, "width": image.width, "height": image.height}
    except TypographyError:
        raise
    except Exception as exc:
        raise TypographyError(f"plate is not a valid RGBA PNG: {path}: {exc}") from exc


def _save_png_atomic(image: Image.Image, output_path: Path) -> None:
    output_path = Path(output_path).absolute()
    parent = secure_mkdirs(canonical_root(output_path.parent))
    buffer = BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    atomic_write_bytes(output_path, buffer.getvalue(), root=parent, mode=0o600)


def _typography_layer_recipe(layer: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "id",
        "text",
        "proof",
        "font_sha256",
        "face_index",
        "font_identity",
        "font_size",
        "tracking_px",
        "layout_mode",
        "scale_xy",
        "center_xy",
        "top_left_xy",
        "fill_rgba",
        "fill_gradient",
        "opacity",
        "stroke_width",
        "stroke_rgba",
        "shadow",
        "start_frame",
        "end_frame_exclusive",
    )
    return {key: layer.get(key) for key in keys if key in layer}


def create_original_font_reference_match(
    layer: Dict[str, Any],
    reference_path: Path,
    *,
    asset_origin: str,
    evidence: str,
) -> Dict[str, Any]:
    reference_path = Path(reference_path).expanduser().resolve()
    font_path = Path(str(layer.get("font_path", ""))).expanduser().resolve()
    if not reference_path.is_file() or not font_path.is_file():
        raise TypographyError("original font provenance requires existing reference and font files")
    if sha256_file(font_path) != layer.get("font_sha256"):
        raise TypographyError("original font provenance font hash does not match the layer")
    asset_origin = str(asset_origin).strip()
    evidence = str(evidence).strip()
    if not asset_origin or not evidence:
        raise TypographyError("original font provenance requires asset_origin and evidence")
    return sign_payload(
        {
            "schema_version": 1,
            "status": "PASS",
            "method": "original_asset_provenance",
            "reference_path": str(reference_path),
            "reference_sha256": sha256_file(reference_path),
            "asset_origin": asset_origin,
            "evidence": evidence,
            "layer_recipe_hash": recipe_hash(_typography_layer_recipe(layer)),
        },
        purpose="typography-reference-match-v1",
    )


def _generated_exact_font_mask(layer: Dict[str, Any], reference_pixels: "Any") -> "Any":
    import cv2
    import numpy as np

    if int(layer.get("stroke_width", 0)) != 0:
        raise TypographyError("silhouette font proof currently requires stroke_width=0")
    text = str(layer.get("text", ""))
    path = Path(str(layer.get("font_path", "")))
    size = int(layer.get("font_size", 0))
    face_index = int(layer.get("face_index", 0))
    tracking = float(layer.get("tracking_px", 0.0))
    layout_mode = str(layer.get("layout_mode", "shaped-run" if abs(tracking) < 1e-9 else "prefix-positioned-isolated"))
    native = _render_mask(text, path, face_index, size, layout_mode=layout_mode, tracking_px=tracking)
    scale = layer.get("scale_xy", [1.0, 1.0])
    target_width = max(1, round(native.shape[1] * float(scale[0])))
    target_height = max(1, round(native.shape[0] * float(scale[1])))
    generated = cv2.resize(native.astype("uint8"), (target_width, target_height), interpolation=cv2.INTER_AREA) > 0
    generated_ys, generated_xs = np.where(generated)
    if not len(generated_xs):
        raise TypographyError("generated font silhouette is empty")
    generated = generated[
        int(generated_ys.min()) : int(generated_ys.max()) + 1,
        int(generated_xs.min()) : int(generated_xs.max()) + 1,
    ]
    ys, xs = np.where(reference_pixels)
    if not len(xs):
        raise TypographyError("reference silhouette mask is empty")
    x1, x2 = int(xs.min()), int(xs.max()) + 1
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    if generated.shape != (y2 - y1, x2 - x1):
        raise TypographyError(
            f"generated font silhouette {generated.shape[::-1]} does not match reference glyph bounds {(x2 - x1, y2 - y1)}"
        )
    canvas = np.zeros(reference_pixels.shape, dtype=bool)
    canvas[y1:y2, x1:x2] = generated
    return canvas


def create_exact_font_reference_match(
    layer: Dict[str, Any],
    reference_path: Path,
    reference_mask_path: Path,
    candidate_mask_path: Path,
    *,
    evidence: str,
) -> Dict[str, Any]:
    import numpy as np

    reference_path = Path(reference_path).expanduser().resolve()
    reference_mask_path = Path(reference_mask_path).expanduser().resolve()
    candidate_mask_path = Path(candidate_mask_path).expanduser().absolute()
    if not reference_path.is_file() or not reference_mask_path.is_file():
        raise TypographyError("silhouette font proof requires existing reference media and mask")
    reference_pixels = np.asarray(Image.open(reference_mask_path).convert("L")) > 127
    generated = _generated_exact_font_mask(layer, reference_pixels)
    _save_png_atomic(Image.fromarray(generated.astype("uint8") * 255), candidate_mask_path)
    intersection = int((reference_pixels & generated).sum())
    union = int((reference_pixels | generated).sum())
    iou = intersection / max(union, 1)
    if iou < 0.995:
        raise TypographyError(f"exact font silhouette IoU {iou:.6f} is below the 0.995 proof threshold")
    evidence = str(evidence).strip()
    if not evidence:
        raise TypographyError("silhouette font proof requires evidence")
    return sign_payload(
        {
            "schema_version": 1,
            "status": "PASS",
            "method": "silhouette_comparison",
            "reference_path": str(reference_path),
            "reference_sha256": sha256_file(reference_path),
            "reference_mask_path": str(reference_mask_path),
            "reference_mask_sha256": sha256_file(reference_mask_path),
            "candidate_mask_path": str(candidate_mask_path),
            "candidate_mask_sha256": sha256_file(candidate_mask_path),
            "silhouette_iou": round(iou, 8),
            "evidence": evidence,
            "layer_recipe_hash": recipe_hash(_typography_layer_recipe(layer)),
        },
        purpose="typography-reference-match-v1",
    )


def _validate_exact_font_reference_match(layer: Dict[str, Any]) -> Dict[str, Any]:
    match = layer.get("reference_match")
    if not isinstance(match, dict) or match.get("status") != "PASS":
        raise TypographyError("exact_font requires a PASS reference_match proof")
    try:
        verify_payload(match, purpose="typography-reference-match-v1")
    except SignatureError as exc:
        raise TypographyError(f"exact_font reference_match signature is invalid: {exc}") from exc
    if match.get("layer_recipe_hash") != recipe_hash(_typography_layer_recipe(layer)):
        raise TypographyError("exact_font reference_match is not bound to the rendered layer recipe")
    method = str(match.get("method", ""))
    reference_sha = str(match.get("reference_sha256", ""))
    reference_path = Path(str(match.get("reference_path", "")))
    evidence = str(match.get("evidence", "")).strip()
    if (
        len(reference_sha) != 64
        or not reference_path.is_file()
        or sha256_file(reference_path) != reference_sha
        or not evidence
        or evidence.upper().startswith(("PENDING_", "REPLACE_"))
    ):
        raise TypographyError("exact_font reference_match lacks reference-bound evidence")
    if method == "original_asset_provenance":
        origin = str(match.get("asset_origin", "")).strip()
        if not origin:
            raise TypographyError("original_asset_provenance requires asset_origin")
        return {"method": method, "reference_sha256": reference_sha, "evidence": evidence}
    if method != "silhouette_comparison":
        raise TypographyError("exact_font reference_match method must be original_asset_provenance or silhouette_comparison")
    import numpy as np
    from PIL import Image

    reference_mask = Path(str(match.get("reference_mask_path", "")))
    candidate_mask = Path(str(match.get("candidate_mask_path", "")))
    if not reference_mask.is_file() or sha256_file(reference_mask) != match.get("reference_mask_sha256"):
        raise TypographyError("reference silhouette mask hash does not match")
    if not candidate_mask.is_file() or sha256_file(candidate_mask) != match.get("candidate_mask_sha256"):
        raise TypographyError("candidate silhouette mask hash does not match")
    reference_pixels = np.asarray(Image.open(reference_mask).convert("L")) > 127
    candidate_pixels = np.asarray(Image.open(candidate_mask).convert("L")) > 127
    if reference_pixels.shape != candidate_pixels.shape:
        raise TypographyError("silhouette comparison masks must share exact canvas dimensions")
    generated_pixels = _generated_exact_font_mask(layer, reference_pixels)
    if not np.array_equal(candidate_pixels, generated_pixels):
        raise TypographyError("candidate silhouette mask was not generated from the locked font layer recipe")
    intersection = int((reference_pixels & candidate_pixels).sum())
    union = int((reference_pixels | candidate_pixels).sum())
    iou = intersection / max(union, 1)
    if iou < 0.995:
        raise TypographyError(f"exact font silhouette IoU {iou:.6f} is below the 0.995 proof threshold")
    return {"method": method, "reference_sha256": reference_sha, "silhouette_iou": round(iou, 8)}


def create_traced_glyph_provenance(
    reference_path: Path,
    reference_mask_path: Path,
    plate_path: Path,
    receipt_path: Path,
    *,
    source_frame: int,
    extraction_roi: Sequence[int],
    extraction_method: str,
) -> Dict[str, Any]:
    import numpy as np

    reference_path = Path(reference_path).resolve()
    reference_mask_path = Path(reference_mask_path).resolve()
    plate_path = Path(plate_path).resolve()
    if not reference_path.is_file() or not reference_mask_path.is_file() or not plate_path.is_file():
        raise TypographyError("traced glyph provenance requires existing reference, mask, and plate files")
    plate_identity = _validate_rgba_plate(plate_path)
    with Image.open(reference_mask_path) as mask_image:
        reference_mask = np.asarray(mask_image.convert("L")) > 127
    with Image.open(plate_path) as plate_image:
        plate_alpha = np.asarray(plate_image.convert("RGBA").getchannel("A")) > 127
    if reference_mask.shape != plate_alpha.shape:
        raise TypographyError("traced glyph mask and plate must use the same canvas")
    intersection = int((reference_mask & plate_alpha).sum())
    union = int((reference_mask | plate_alpha).sum())
    silhouette_iou = intersection / max(union, 1)
    if silhouette_iou < 0.995:
        raise TypographyError(f"traced glyph alpha IoU {silhouette_iou:.6f} is below the 0.995 proof threshold")
    if isinstance(source_frame, bool) or not isinstance(source_frame, int) or source_frame < 0:
        raise TypographyError("traced glyph provenance source_frame must be a non-negative integer")
    roi = [int(value) for value in extraction_roi]
    if len(roi) != 4 or roi[0] < 0 or roi[1] < 0 or roi[2] <= roi[0] or roi[3] <= roi[1]:
        raise TypographyError("traced glyph provenance extraction_roi is invalid")
    extraction_method = str(extraction_method).strip()
    if not extraction_method or extraction_method.upper().startswith(("PENDING_", "REPLACE_")):
        raise TypographyError("traced glyph provenance extraction_method is missing")
    core = {
        "schema_version": 1,
        "proof": "traced_reference_glyph",
        "reference_path": str(reference_path),
        "reference_sha256": sha256_file(reference_path),
        "reference_mask_path": str(reference_mask_path),
        "reference_mask_sha256": sha256_file(reference_mask_path),
        "source_frame": source_frame,
        "extraction_roi": roi,
        "extraction_method": extraction_method,
        "plate_path": str(plate_path),
        "plate_sha256": sha256_file(plate_path),
        "plate_identity": plate_identity,
        "silhouette_iou": round(silhouette_iou, 8),
    }
    receipt = sign_payload({**core, "receipt_id": recipe_hash(core)}, purpose="traced-glyph-provenance-v1")
    atomic_write_json(receipt_path, receipt, root=receipt_path.parent)
    return {**receipt, "receipt_path": str(receipt_path), "receipt_sha256": sha256_file(receipt_path)}


def _validate_traced_glyph_provenance(layer: Dict[str, Any]) -> Dict[str, Any]:
    import numpy as np

    receipt_path = Path(str(layer.get("provenance_receipt_path", "")))
    expected_receipt_hash = str(layer.get("provenance_receipt_sha256", ""))
    if not receipt_path.is_file() or sha256_file(receipt_path) != expected_receipt_hash:
        raise TypographyError("traced reference glyph provenance receipt is missing or changed")
    receipt = load_json(receipt_path)
    try:
        verify_payload(receipt, purpose="traced-glyph-provenance-v1")
    except SignatureError as exc:
        raise TypographyError(f"traced reference glyph provenance signature is invalid: {exc}") from exc
    core = {key: value for key, value in receipt.items() if key not in {"receipt_id", "signature"}}
    if recipe_hash(core) != receipt.get("receipt_id"):
        raise TypographyError("traced reference glyph provenance receipt identity is invalid")
    reference = Path(str(receipt.get("reference_path", "")))
    reference_mask = Path(str(receipt.get("reference_mask_path", "")))
    plate = Path(str(layer.get("plate_path", "")))
    plate_identity = _validate_rgba_plate(plate)
    mask_valid = reference_mask.is_file() and sha256_file(reference_mask) == receipt.get("reference_mask_sha256")
    silhouette_iou = 0.0
    if mask_valid:
        with Image.open(reference_mask) as mask_image:
            mask_pixels = np.asarray(mask_image.convert("L")) > 127
        with Image.open(plate) as plate_image:
            plate_pixels = np.asarray(plate_image.convert("RGBA").getchannel("A")) > 127
        if mask_pixels.shape == plate_pixels.shape:
            intersection = int((mask_pixels & plate_pixels).sum())
            union = int((mask_pixels | plate_pixels).sum())
            silhouette_iou = intersection / max(union, 1)
    checks = {
        "proof": receipt.get("proof") == "traced_reference_glyph",
        "reference": reference.is_file() and sha256_file(reference) == receipt.get("reference_sha256"),
        "reference_mask": mask_valid,
        "plate": str(plate.resolve()) == receipt.get("plate_path") and receipt.get("plate_sha256") == layer.get("plate_sha256"),
        "source_frame": receipt.get("source_frame") == layer.get("source_frame"),
        "extraction_roi": receipt.get("extraction_roi") == layer.get("extraction_roi"),
        "plate_identity": receipt.get("plate_identity") == plate_identity,
        "silhouette": silhouette_iou >= 0.995 and round(silhouette_iou, 8) == receipt.get("silhouette_iou"),
    }
    if not all(checks.values()):
        raise TypographyError("traced reference glyph layer does not match its provenance receipt")
    return {"receipt_id": receipt["receipt_id"], "reference_sha256": receipt["reference_sha256"], "silhouette_iou": silhouette_iou}


def validate_typography_layer(layer: Dict[str, Any]) -> Dict[str, Any]:
    proof = str(layer.get("proof", ""))
    proof_receipt: Dict[str, Any]
    if proof == "exact_font":
        path = Path(str(layer.get("font_path", "")))
        expected = str(layer.get("font_sha256", ""))
        if path.is_symlink() or not path.is_file() or not expected or sha256_file(path) != expected:
            raise TypographyError("exact_font requires an existing font whose SHA-256 matches the locked receipt")
        if "face_index" not in layer:
            raise TypographyError("exact_font requires an explicit face_index")
        face_index = layer.get("face_index")
        identity = inspect_font_face(path, face_index)
        if layer.get("font_identity") != identity:
            raise TypographyError("exact_font font_identity does not match the parsed face")
        missing = _font_missing_codepoints(path, face_index, str(layer.get("text", "")))
        if missing:
            formatted = ", ".join(f"U+{codepoint:04X}" for codepoint in missing)
            raise TypographyError(f"exact_font lacks glyph coverage for: {formatted}")
        if identity["variation_axes"]:
            raise TypographyError("variable exact fonts are unsupported until variation coordinates are applied by the renderer")
        if layer.get("plate_path"):
            plate = Path(str(layer["plate_path"]))
            plate_hash = str(layer.get("plate_sha256", ""))
            if not plate.is_file() or not plate_hash or sha256_file(plate) != plate_hash:
                raise TypographyError("pre-rendered exact-font plate hash does not match")
            _validate_rgba_plate(plate)
        else:
            if int(layer.get("font_size", 0)) <= 0:
                raise TypographyError("exact_font rendering requires font_size")
            if "center_xy" not in layer and "top_left_xy" not in layer:
                raise TypographyError("exact_font rendering requires center_xy or top_left_xy")
            if "fill_rgba" not in layer and "fill_gradient" not in layer:
                raise TypographyError("exact_font rendering requires fill_rgba or fill_gradient")
        proof_receipt = _validate_exact_font_reference_match(layer)
    elif proof == "traced_reference_glyph":
        path = Path(str(layer.get("plate_path", "")))
        expected = str(layer.get("plate_sha256", ""))
        if not path.is_file() or not expected or sha256_file(path) != expected:
            raise TypographyError("traced_reference_glyph requires an exact locked RGBA plate")
        _validate_rgba_plate(path)
        if layer.get("source_frame") is None:
            raise TypographyError("traced_reference_glyph requires source_frame provenance")
        proof_receipt = _validate_traced_glyph_provenance(layer)
    else:
        raise TypographyError(
            "typography is not exact: use a hash-locked exact font or a traced reference glyph plate; nearest/guessed fonts are forbidden"
        )
    return {"status": "PASS", "proof": proof, "text": layer.get("text", ""), "proof_receipt": proof_receipt}


def discover_fonts(extra_dirs: Optional[Sequence[Path]] = None) -> List[Dict[str, Any]]:
    roots = list(FONT_DIRS) + [Path(p) for p in (extra_dirs or [])]
    paths: List[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for suffix in ("*.ttf", "*.otf", "*.ttc"):
            paths.extend(root.rglob(suffix))
    records: List[Dict[str, Any]] = []
    seen = set()
    for path in sorted(set(paths)):
        try:
            with path.open("rb") as handle:
                is_collection = handle.read(4) == b"ttcf"
            if is_collection:
                collection = TTCollection(str(path), lazy=True)
                count = len(collection.fonts)
                for item in collection.fonts:
                    item.close()
            else:
                count = 1
            for face_index in range(count):
                resolved = path.resolve()
                key = (str(resolved), face_index)
                if key in seen:
                    continue
                identity = inspect_font_face(path, face_index)
                seen.add(key)
                records.append(
                    {
                        "path": str(resolved),
                        "sha256": sha256_file(path),
                        **identity,
                    }
                )
        except Exception:
            continue
    return records


def _render_mask(
    text: str,
    font_path: Path,
    face_index: int,
    size: int,
    *,
    layout_mode: str = "shaped-run",
    tracking_px: float = 0.0,
) -> "Any":
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(font_path), size=size, index=face_index)
    bbox = font.getbbox(text, stroke_width=0)
    width = max(1, round(font.getlength(text) + max(0, len(text) - 1) * tracking_px + 32))
    height = max(1, bbox[3] - bbox[1] + 32)
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    if layout_mode == "shaped-run":
        draw.text((16, 16 - bbox[1]), text, font=font, fill=255)
    elif layout_mode == "prefix-positioned-isolated":
        for index, character in enumerate(text):
            x = 16 + float(font.getlength(text[:index])) + index * tracking_px
            draw.text((round(x), 16 - bbox[1]), character, font=font, fill=255)
    else:
        raise TypographyError(f"unsupported typography layout mode: {layout_mode}")
    box = image.getbbox()
    if box:
        image = image.crop(box)
    return np.asarray(image) > 127


def _fit_score(reference_mask: "Any", candidate_mask: "Any") -> float:
    import cv2
    import numpy as np

    refb = reference_mask.astype(bool)
    candb = candidate_mask.astype(bool)
    if not refb.any() or not candb.any():
        return 0.0
    target_h = max(refb.shape[0], candb.shape[0])
    target_w = max(refb.shape[1], candb.shape[1])

    def centered(mask: "Any") -> "Any":
        canvas = np.zeros((target_h, target_w), dtype=bool)
        y = (target_h - mask.shape[0]) // 2
        x = (target_w - mask.shape[1]) // 2
        canvas[y : y + mask.shape[0], x : x + mask.shape[1]] = mask
        return canvas

    refb = centered(refb)
    candb = centered(candb)
    intersection = float((refb & candb).sum())
    union = float((refb | candb).sum())
    iou = intersection / max(union, 1.0)
    ref_edges = cv2.Canny(refb.astype(np.uint8) * 255, 50, 150) > 0
    cand_edges = cv2.Canny(candb.astype(np.uint8) * 255, 50, 150) > 0
    distance_ref = cv2.distanceTransform((~ref_edges).astype(np.uint8), cv2.DIST_L2, 3)
    distance_cand = cv2.distanceTransform((~cand_edges).astype(np.uint8), cv2.DIST_L2, 3)
    chamfer = (float(distance_ref[cand_edges].mean()) if cand_edges.any() else 99.0) + (
        float(distance_cand[ref_edges].mean()) if ref_edges.any() else 99.0
    )
    edge_score = max(0.0, 1.0 - chamfer / max(target_h * 0.12, 1.0))
    return round(0.65 * iou + 0.35 * edge_score, 6)


def _component_topology_score(reference_mask: "Any", candidate_mask: "Any") -> float:
    import cv2
    import numpy as np

    def component_signature(mask: "Any") -> Tuple[int, float]:
        pixels = mask.astype(np.uint8)
        foreground = int(pixels.sum())
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(pixels, connectivity=8)
        minimum_area = max(3, round(foreground * 0.003))
        areas = [int(value) for value in stats[1:, cv2.CC_STAT_AREA] if int(value) >= minimum_area]
        if not areas:
            return 0, 0.0
        return len(areas), max(areas) / max(sum(areas), 1)

    reference_count, reference_largest = component_signature(reference_mask)
    candidate_count, candidate_largest = component_signature(candidate_mask)
    if reference_count == 0 or candidate_count == 0:
        return 0.0
    count_score = 1.0 / (1.0 + abs(reference_count - candidate_count))
    largest_score = max(0.0, 1.0 - abs(reference_largest - candidate_largest))
    return round(0.65 * count_score + 0.35 * largest_score, 6)


def match_fonts(text: str, mask_path: Path, font_inventory: Sequence[Dict[str, Any]], *, limit: int = 20) -> List[Dict[str, Any]]:
    import cv2
    import numpy as np
    from PIL import Image

    ref = np.asarray(Image.open(mask_path).convert("L")) > 127
    ys, xs = np.where(ref)
    if not len(xs):
        raise TypographyError("reference glyph mask is empty")
    ref = ref[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    results: List[Dict[str, Any]] = []
    for record in font_inventory:
        try:
            path = Path(record["path"])
            expected_hash = str(record.get("sha256", ""))
            if path.is_symlink() or not path.is_file() or not expected_hash or sha256_file(path) != expected_hash:
                continue
            face_index = int(record.get("face_index", 0))
            inspect_font_face(path, face_index)
            best: Optional[Dict[str, Any]] = None
            sizes = sorted({max(12, round(ref.shape[0] * factor)) for factor in (0.80, 0.90, 1.0, 1.05, 1.08, 1.10, 1.20, 1.33, 1.40)})
            for size in sizes:
                for layout_mode in ("shaped-run", "prefix-positioned-isolated"):
                    for tracking_px in (-0.04 * size, 0.0, 0.04 * size):
                        native = _render_mask(
                            text,
                            path,
                            face_index,
                            size,
                            layout_mode=layout_mode,
                            tracking_px=tracking_px,
                        )
                        for scale_x in (0.80, 0.84, 0.88, 0.92, 0.96, 1.0, 1.04, 1.08):
                            width = max(1, round(native.shape[1] * scale_x))
                            candidate = cv2.resize(native.astype("uint8"), (width, native.shape[0]), interpolation=cv2.INTER_AREA) > 0
                            contour_score = _fit_score(ref, candidate)
                            topology_score = _component_topology_score(ref, candidate)
                            score = (
                                0.55 * contour_score + 0.45 * topology_score - abs(1.0 - scale_x) * 0.05 - abs(tracking_px / size) * 0.03
                            )
                            attempt = {
                                **record,
                                "silhouette_score": round(score, 6),
                                "contour_score": contour_score,
                                "topology_score": topology_score,
                                "render_size": size,
                                "scale_x": scale_x,
                                "tracking_px": round(tracking_px, 6),
                                "layout_mode": layout_mode,
                            }
                            if best is None or attempt["silhouette_score"] > best["silhouette_score"]:
                                best = attempt
            if best is not None:
                results.append(best)
        except Exception:
            continue
    return sorted(results, key=lambda item: item["silhouette_score"], reverse=True)[:limit]


def extract_ink_mask(
    image_path: Path, output_path: Path, *, roi: Optional[Tuple[int, int, int, int]] = None, ink: str = "bright"
) -> Dict[str, Any]:
    import cv2
    import numpy as np
    from PIL import Image

    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise TypographyError(f"cannot read image: {image_path}")
    full_h, full_w = image.shape[:2]
    x1, y1, x2, y2 = roi or (0, 0, full_w, full_h)
    crop = image[y1:y2, x1:x2]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    if ink == "bright":
        mask = ((hsv[..., 2] >= 205) & (hsv[..., 1] <= 105)).astype(np.uint8) * 255
    elif ink == "blue":
        mask = ((hsv[..., 0] >= 85) & (hsv[..., 0] <= 140) & (hsv[..., 1] >= 70) & (hsv[..., 2] >= 80)).astype(np.uint8) * 255
    elif ink == "dark":
        mask = (hsv[..., 2] <= 55).astype(np.uint8) * 255
    else:
        raise TypographyError(f"unsupported ink mode: {ink}")
    kernel = np.ones((2, 2), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    full = np.zeros((full_h, full_w), dtype=np.uint8)
    full[y1:y2, x1:x2] = mask
    _save_png_atomic(Image.fromarray(full), output_path)
    return {"path": str(output_path), "sha256": sha256_file(output_path), "bbox": list(roi or (0, 0, full_w, full_h)), "ink": ink}


def trace_glyph_plate(mask_path: Path, output_path: Path, *, rgba: Tuple[int, int, int, int] = (255, 255, 255, 255)) -> Dict[str, Any]:
    from PIL import Image

    with Image.open(mask_path) as source:
        mask = source.convert("L")
    canvas = Image.new("RGBA", mask.size, rgba)
    canvas.putalpha(mask)
    _save_png_atomic(canvas, output_path)
    return {"path": str(output_path), "sha256": sha256_file(output_path), "proof": "traced_reference_glyph"}


def render_exact_font_plate(layer: Dict[str, Any], output_path: Path, *, width: int, height: int) -> Dict[str, Any]:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    validate_typography_layer(layer)
    if layer.get("proof") != "exact_font":
        raise TypographyError("render_exact_font_plate requires proof=exact_font")
    text = str(layer.get("text", ""))
    if not text:
        raise TypographyError("exact font layer text cannot be empty")
    font = ImageFont.truetype(str(layer["font_path"]), size=int(layer["font_size"]), index=int(layer["face_index"]))
    tracking = float(layer.get("tracking_px", 0.0))
    stroke_width = max(0, int(layer.get("stroke_width", 0)))
    pad = max(16, stroke_width * 3 + 8)

    bbox = font.getbbox(text, stroke_width=stroke_width)
    natural_width = max(1, bbox[2] - bbox[0])
    natural_height = max(1, bbox[3] - bbox[1])
    tracked_width = natural_width + max(0, len(text) - 1) * tracking
    mask_width = max(1, int(round(tracked_width + pad * 2)))
    mask_height = max(1, natural_height + pad * 2)

    def draw_mask(stroke: int) -> Image.Image:
        mask = Image.new("L", (mask_width, mask_height), 0)
        draw = ImageDraw.Draw(mask)
        origin_y = pad - bbox[1]
        if abs(tracking) < 1e-9:
            draw.text((pad - bbox[0], origin_y), text, font=font, fill=255, stroke_width=stroke, stroke_fill=255)
        else:
            cursor = float(pad - bbox[0])
            for character in text:
                draw.text((round(cursor), origin_y), character, font=font, fill=255, stroke_width=stroke, stroke_fill=255)
                cursor += float(font.getlength(character)) + tracking
        crop = mask.getbbox()
        return mask.crop(crop) if crop else mask

    fill_alpha = draw_mask(0)
    stroke_alpha = draw_mask(stroke_width) if stroke_width else None
    scale = layer.get("scale_xy", [1.0, 1.0])
    scale_x = float(scale[0])
    scale_y = float(scale[1])
    if not 0.25 <= scale_x <= 4.0 or not 0.25 <= scale_y <= 4.0:
        raise TypographyError("scale_xy is outside the safe 0.25..4 range")
    target_size = (max(1, round(fill_alpha.width * scale_x)), max(1, round(fill_alpha.height * scale_y)))
    fill_alpha = fill_alpha.resize(target_size, Image.Resampling.LANCZOS)
    if stroke_alpha is not None:
        stroke_alpha = stroke_alpha.resize(
            (max(1, round(stroke_alpha.width * scale_x)), max(1, round(stroke_alpha.height * scale_y))), Image.Resampling.LANCZOS
        )

    if "fill_gradient" in layer:
        gradient = layer["fill_gradient"]
        colors = gradient.get("colors_rgba", [])
        if len(colors) != 2:
            raise TypographyError("fill_gradient currently requires exactly two colors_rgba")
        first = np.asarray(colors[0], dtype=np.float32)
        second = np.asarray(colors[1], dtype=np.float32)
        axis = gradient.get("axis", "vertical")
        length = fill_alpha.height if axis == "vertical" else fill_alpha.width
        weights = np.linspace(0.0, 1.0, max(length, 1), dtype=np.float32)
        values = first[None, :] * (1.0 - weights[:, None]) + second[None, :] * weights[:, None]
        if axis == "vertical":
            rgba = np.repeat(values[:, None, :], fill_alpha.width, axis=1)
        elif axis == "horizontal":
            rgba = np.repeat(values[None, :, :], fill_alpha.height, axis=0)
        else:
            raise TypographyError("fill_gradient axis must be vertical or horizontal")
        fill_image = Image.fromarray(np.clip(rgba, 0, 255).astype(np.uint8), "RGBA")
    else:
        fill = tuple(int(value) for value in layer.get("fill_rgba", [255, 255, 255, 255]))
        fill_image = Image.new("RGBA", fill_alpha.size, fill)
    fill_image.putalpha(Image.eval(fill_alpha, lambda value: value * int(layer.get("opacity", 255)) // 255))

    if "center_xy" in layer:
        center = layer["center_xy"]
        x = round(float(center[0]) - fill_image.width / 2)
        y = round(float(center[1]) - fill_image.height / 2)
    else:
        top_left = layer["top_left_xy"]
        x, y = round(float(top_left[0])), round(float(top_left[1]))
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    shadow = layer.get("shadow")
    if shadow:
        shadow_mask = fill_alpha.filter(ImageFilter.GaussianBlur(float(shadow.get("blur_radius", 0.0))))
        shadow_color = tuple(int(value) for value in shadow.get("color_rgba", [0, 0, 0, 128]))
        shadow_image = Image.new("RGBA", shadow_mask.size, shadow_color)
        shadow_image.putalpha(shadow_mask.point(lambda value: value * shadow_color[3] // 255))
        offset = shadow.get("offset_xy", [0, 0])
        canvas.alpha_composite(shadow_image, (x + int(offset[0]), y + int(offset[1])))
    if stroke_alpha is not None:
        stroke_color = tuple(int(value) for value in layer.get("stroke_rgba", [0, 0, 0, 255]))
        stroke_image = Image.new("RGBA", stroke_alpha.size, stroke_color)
        stroke_image.putalpha(stroke_alpha)
        stroke_x = x - (stroke_image.width - fill_image.width) // 2
        stroke_y = y - (stroke_image.height - fill_image.height) // 2
        canvas.alpha_composite(stroke_image, (stroke_x, stroke_y))
    canvas.alpha_composite(fill_image, (x, y))
    _save_png_atomic(canvas, output_path)
    return {"path": str(output_path), "sha256": sha256_file(output_path), "proof": "exact_font", "font_sha256": layer["font_sha256"]}
