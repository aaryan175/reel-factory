from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from reelctl.hashing import sha256_file
from reelctl.typography import (
    _fit_score,
    _render_mask,
    create_exact_font_reference_match,
    inspect_font_face,
    match_fonts,
    validate_typography_layer,
)


def test_silhouette_match_ranks_the_exact_font_face_first(tmp_path: Path) -> None:
    helvetica = Path("/System/Library/Fonts/Helvetica.ttc")
    menlo = Path("/System/Library/Fonts/Menlo.ttc")
    mask = _render_mask("the exact font", helvetica, 0, 96)
    mask_path = tmp_path / "mask.png"
    Image.fromarray(mask.astype("uint8") * 255).save(mask_path)
    inventory = [
        {"path": str(menlo), "sha256": sha256_file(menlo), "face_index": 0, "family": "Menlo"},
        {"path": str(helvetica), "sha256": sha256_file(helvetica), "face_index": 0, "family": "Helvetica"},
    ]
    results = match_fonts("the exact font", mask_path, inventory, limit=2)
    assert results[0]["path"] == str(helvetica)
    assert results[0]["silhouette_score"] > results[1]["silhouette_score"]


def test_exact_font_silhouette_receipt_is_generated_from_locked_layer(tmp_path: Path) -> None:
    font = Path("/System/Library/Fonts/Helvetica.ttc")
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(b"reference")
    layer = {
        "id": "exact",
        "text": "exact font",
        "proof": "exact_font",
        "font_path": str(font),
        "font_sha256": sha256_file(font),
        "face_index": 0,
        "font_identity": inspect_font_face(font, 0),
        "font_size": 64,
        "tracking_px": 0.0,
        "scale_xy": [1.0, 1.0],
        "center_xy": [100, 100],
        "fill_rgba": [255, 255, 255, 255],
        "start_frame": 0,
        "end_frame_exclusive": 12,
    }
    reference_mask = _render_mask(layer["text"], font, 0, layer["font_size"])
    reference_mask_path = tmp_path / "reference-mask.png"
    Image.fromarray(reference_mask.astype("uint8") * 255).save(reference_mask_path)
    layer["reference_match"] = create_exact_font_reference_match(
        layer,
        reference,
        reference_mask_path,
        tmp_path / "candidate-mask.png",
        evidence="all reference glyph contours extracted from the locked caption state",
    )
    result = validate_typography_layer(layer)
    assert result["status"] == "PASS"
    assert result["proof_receipt"]["silhouette_iou"] == pytest.approx(1.0)


def test_silhouette_score_penalizes_aspect_ratio_distortion() -> None:
    source = np.zeros((40, 80), dtype=bool)
    source[8:32, 10:70] = True
    stretched = cv2.resize(source.astype("uint8"), (160, 20), interpolation=cv2.INTER_NEAREST) > 0
    assert _fit_score(source, source) == pytest.approx(1.0)
    assert _fit_score(source, stretched) < 0.8


def test_accepted_script_fixture_prefers_pinyon_over_snell() -> None:
    # Optional real fixture: REEL_FACTORY_TYPOGRAPHY_FIXTURE points at a project directory holding
    # a script word mask and the (OFL-licensed) Pinyon Script font file.
    project = Path(os.path.expanduser(os.environ.get("REEL_FACTORY_TYPOGRAPHY_FIXTURE", "/nonexistent")))
    word = os.environ.get("REEL_FACTORY_TYPOGRAPHY_FIXTURE_WORD", "sample")
    mask = project / f"assets/source-script-masks/{word}-source-mask.png"
    pinyon = project / "assets/fonts/PinyonScript-Regular.ttf"
    snell = Path("/System/Library/Fonts/Supplemental/SnellRoundhand.ttc")
    if not (mask.is_file() and pinyon.is_file() and snell.is_file()):
        pytest.skip("accepted local typography fixture is unavailable")
    inventory = [
        {"path": str(snell), "sha256": sha256_file(snell), "face_index": 1, "family": "Snell Roundhand"},
        {"path": str(pinyon), "sha256": sha256_file(pinyon), "face_index": 0, "family": "Pinyon Script"},
    ]
    results = match_fonts(word, mask, inventory, limit=2)
    assert results[0]["path"] == str(pinyon)
