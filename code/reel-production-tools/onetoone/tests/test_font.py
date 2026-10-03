#!/usr/bin/env python3
"""Regression guards for the default ornate caption face.

Two things are being held:
  1. the chosen face is actually on this machine and loadable, so a build never silently
     falls back to a different script face;
  2. the c02 collision fix stays fixed — the ornate word sits inside its measured box and
     never grows into a slab that swallows the plain line drawn on top of it.

The overlap ceiling is calibrated to the REFERENCE, not to zero. Measured on
reference-source.mp4 by differencing the ink mask at n=48 (script landed) against n=35
(plain line only, script has not blurred in yet): 21.6% of the reference's own script ink
falls inside the "<line A>" ink box. The reference deliberately threads the plain line through
the script's top loop, so a near-zero ceiling would reject the reference's own layout.
Ours: Pinyon 18.0%, Snell (the previous face) 3.9%.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onetoone.captions_typeset import DEFAULT_FACES, typeset_state  # noqa: E402

from _fixtures import fixture  # noqa: E402

BRAIN = fixture("ornate-script-project", "brain", "captions.plaintext.json")

# The reference threads "<line A>" through the script's loop; 21.6% of its script ink lands in
# the plain line's ink box. Anything past 30% means our script has outgrown that grammar.
MAX_ORNATE_INTRUSION = 0.30
# A script fills a small fraction of its block. The v004 reject filled the ornate box with a
# solid slab; the reference measures 13.5% coverage inside the c02 ornate box.
ORNATE_COVERAGE_RANGE = (0.05, 0.25)


def _mask(layer: Image.Image) -> np.ndarray:
    return np.array(layer)[:, :, 3] > 0


@pytest.fixture(scope="module")
def c02():
    if not BRAIN.exists():
        pytest.skip(f"optional project fixture not present: {BRAIN}")
    states = json.loads(BRAIN.read_text())["states"]
    return [s for s in states if s["id"] == "c02"][0]


def test_ornate_face_file_exists_and_loads():
    path, index = DEFAULT_FACES["ornate"]
    if not Path(path).is_file():
        pytest.skip(f"ornate face not installed at {path}: install Pinyon Script (SIL OFL, google/fonts) "
                    "or set REEL_FACTORY_ORNATE_FONT")
    font = ImageFont.truetype(path, 100, index=index)
    assert font.size == 100
    # a real outline, not a notdef box
    x0, y0, x1, y1 = font.getbbox("Sample")
    assert x1 - x0 > 100 and y1 - y0 > 40


def test_c02_ornate_ink_stays_inside_its_measured_box(c02):
    ornate = [p for p in c02["parts"] if p["part"] == "ornate_word"][0]
    layer, _ = typeset_state({"id": "c02-ornate-only", "parts": [ornate]})
    m = _mask(layer)
    total = int(m.sum())
    assert total > 0, "ornate word rendered no ink"
    bx0, by0, bx1, by1 = [int(v) for v in ornate["bbox_settled"]]
    inside = int(m[by0:by1, bx0:bx1].sum())
    assert inside / total >= 0.99, f"only {100 * inside / total:.1f}% of 'Sample' ink is inside its bbox"
    coverage = inside / ((bx1 - bx0) * (by1 - by0))
    lo, hi = ORNATE_COVERAGE_RANGE
    assert lo <= coverage <= hi, f"ornate coverage {coverage:.3f} outside {ORNATE_COVERAGE_RANGE} (slab or empty?)"


def test_c02_ornate_does_not_swallow_the_plain_line(c02):
    """The collision guard: '<Script A>' must not grow into the '<line A>' ink box past the
    reference's own 21.6%, and '<line A>' must still be fully drawn on top of it."""
    ornate = [p for p in c02["parts"] if p["part"] == "ornate_word"][0]
    plain = [p for p in c02["parts"] if p["part"] == "plain_line"][0]

    orn_layer, _ = typeset_state({"id": "o", "parts": [ornate]})
    plain_layer, plain_rep = typeset_state({"id": "p", "parts": [plain]})
    full_layer, _ = typeset_state(c02)

    om = _mask(orn_layer)
    px0, py0, px1, py1 = [int(v) for v in plain_rep["parts"][0]["ink"]]
    intrusion = int(om[py0:py1, px0:px1].sum()) / int(om.sum())
    assert intrusion <= MAX_ORNATE_INTRUSION, (
        f"ornate ink fills {100 * intrusion:.1f}% of the plain line's ink box "
        f"(reference 21.6%, ceiling {100 * MAX_ORNATE_INTRUSION:.0f}%)"
    )

    # z-order: every pixel of "<line A>" survives into the composited state layer.
    pm = _mask(plain_layer)
    assert int(pm.sum()) > 0
    survived = int((pm & _mask(full_layer)).sum()) / int(pm.sum())
    assert survived == 1.0, f"the ornate word erased {100 * (1 - survived):.2f}% of the plain line"
