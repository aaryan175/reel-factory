"""The reference's own face, and a shadow only where the ink needs one.

A build whose script face did not match the reference had two causes, both in the kit:
  1. the script word was set in the default ornate face although this reference's script is a
     different face — a part must be able to name its own face;
  2. every state wore the cast-level shadow, so navy ink on a bright sky got a black halo the
     reference does not have — a state must be able to wear none.
"""
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from onetoone import render as R
from onetoone.captions_typeset import DEFAULT_FACES, typeset_part

CANVAS = (1916, 1078)
PLAIN = list(DEFAULT_FACES["plain"])


def _ink(part):
    layer = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    rep = typeset_part(layer, part, DEFAULT_FACES)
    return np.asarray(layer)[..., 3], rep


@pytest.mark.skipif(not Path(DEFAULT_FACES["ornate"][0]).is_file(),
                    reason="default ornate face not installed (see captions_typeset.DEFAULT_FACES / REEL_FACTORY_ORNATE_FONT)")
def test_a_part_that_names_its_face_is_set_in_that_face():
    base = {"part": "ornate_word", "text": "Sample", "bbox_settled": [566, 100, 1400, 490],
            "ink_rgb": [15, 20, 46]}
    default_ink, _ = _ink(dict(base))
    named_ink, rep = _ink(dict(base, face_file=PLAIN))
    assert named_ink.any(), "the named face drew nothing"
    assert (default_ink != named_ink).mean() > 0.01, "face_file was ignored: same ink as the default face"
    assert rep["text"] == "Sample"


def test_a_part_without_face_file_is_unchanged():
    base = {"part": "plain_line", "text": "stock", "bbox_settled": [1022, 398, 1464, 544], "ink_rgb": [15, 20, 46]}
    a, _ = _ink(dict(base))
    b, _ = _ink(dict(base, face_file=PLAIN))
    assert (a == b).all(), "naming the default face must reproduce the default render exactly"


def test_state_shadow_default_is_the_cast_spec():
    spec = {"radius": 12, "opacity": 1.0, "offset": [4, 5], "grow": 5}
    assert R.state_shadow({"id": "c01"}, spec) is spec
    assert R.state_shadow({"id": "c01"}, None) is None


def test_state_can_wear_no_shadow():
    spec = {"radius": 12, "opacity": 1.0, "offset": [4, 5], "grow": 5}
    assert R.state_shadow({"id": "c07", "shadow": False}, spec) is None
    assert R.state_shadow({"id": "c07", "shadow": None}, spec) is None


def test_state_can_carry_its_own_shadow():
    spec = {"radius": 12, "opacity": 1.0, "offset": [4, 5], "grow": 5}
    own = {"radius": 6, "opacity": 0.5, "offset": [2, 2], "grow": 1}
    assert R.state_shadow({"id": "c05", "shadow": own}, spec) is own
    assert R.state_shadow({"id": "c05", "shadow": True}, spec) is spec
