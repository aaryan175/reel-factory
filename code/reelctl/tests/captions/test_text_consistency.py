"""Does a recovered mask plausibly contain the text the authority says is there?

The mask tier is a contrast proxy: it asks whether ink separates from its surround. It cannot
ask the question that actually matters before rendering — is this mask the glyph, or is it
background architecture that happens to be bright and static?

The sealed authority declares the exact string for every state, so that question is directly
checkable, and it is not circular: the text comes from the authority document, the mask comes
from pixels, and neither informed the other. NFS section 6 lists exactly these dimensions as
ones to compare separately — class/case, and anatomy/topology including component counts.

The motivating failure: `opening-ive` (text "I've") recovered a 1291x757 region. Four glyphs do
not fill 977k pixels, and the region's ink is dense where text is sparse.
"""

from __future__ import annotations

import numpy as np
import pytest

from reelctl.captions.extract import (
    ExtractError,
    text_consistency,
)


def _glyph(mask: np.ndarray, top: int, left: int, w: int, h: int, stroke: int = 8) -> None:
    """One glyph-shaped blob: a closed ring, so it has a counter like real type.

    Solid rectangles are the wrong fixture here. Real caption ink is sparse inside its own
    box — measured on the approved baselines, a caption fills 0.21-0.45 of its bbox
    (a two-word uppercase end card reads ~0.21-0.45 across its hold; a short lowercase
    phrase ~0.38). Solid blocks fill 0.78-1.00, which is background-region territory,
    so a fixture built from them cannot be separated from the dense-background case the gate
    exists to reject.
    """
    mask[top : top + h, left : left + w] = 255
    mask[top + stroke : top + h - stroke, left + stroke : left + w - stroke] = 0


def _word_mask(letters: int, letter_w=40, letter_h=60, gap=12, top=100, left=200,
               shape=(1078, 1916)) -> np.ndarray:
    mask = np.zeros(shape, dtype=np.uint8)
    x = left
    for _ in range(letters):
        _glyph(mask, top, x, letter_w, letter_h)
        x += letter_w + gap
    return mask


def test_a_plausible_word_mask_is_consistent() -> None:
    verdict = text_consistency(_word_mask(4), "have")
    assert verdict.consistent is True
    assert verdict.components == 4


def test_a_dense_background_region_is_not_consistent() -> None:
    """The opening-ive failure: a solid 1291x757 block is not four glyphs."""
    mask = np.zeros((1078, 1916), dtype=np.uint8)
    mask[206:963, 71:1362] = 255
    verdict = text_consistency(mask, "I've")
    assert verdict.consistent is False
    assert "fill" in verdict.reason.lower() or "dense" in verdict.reason.lower()


def test_ink_far_too_small_for_the_string_is_not_consistent() -> None:
    """The `hours` failure: a 23x9 region cannot hold five glyphs."""
    mask = np.zeros((1078, 1916), dtype=np.uint8)
    mask[500:509, 800:823] = 255
    verdict = text_consistency(mask, "hours")
    assert verdict.consistent is False


def test_merged_letters_are_tolerated() -> None:
    """Script and tight kerning merge glyphs; fewer components than letters is normal."""
    mask = np.zeros((1078, 1916), dtype=np.uint8)
    x = 200
    for _ in range(10):  # ten glyphs whose rings touch, so they fuse into few components
        _glyph(mask, 100, x, 32, 60, stroke=6)
        x += 32
    verdict = text_consistency(mask, "playground")
    assert verdict.components < len("playground")
    assert verdict.consistent is True


def test_far_more_components_than_letters_is_not_consistent() -> None:
    """Speckle: 200 tiny components cannot be a four-letter word."""
    rng = np.random.default_rng(20250113)
    mask = np.zeros((1078, 1916), dtype=np.uint8)
    for _ in range(200):
        y, x = rng.integers(100, 900), rng.integers(200, 1700)
        mask[y : y + 6, x : x + 6] = 255
    verdict = text_consistency(mask, "have")
    assert verdict.consistent is False
    assert "component" in verdict.reason.lower()


def test_a_wildly_wrong_aspect_ratio_is_not_consistent() -> None:
    """A tall narrow column is not a horizontal word."""
    mask = np.zeros((1078, 1916), dtype=np.uint8)
    mask[100:900, 400:460] = 255
    verdict = text_consistency(mask, "have")
    assert verdict.consistent is False
    assert "aspect" in verdict.reason.lower() or "fill" in verdict.reason.lower()


def test_multi_line_text_is_measured_per_line() -> None:
    """Stacked lines: the aspect expectation must use the widest line, not the whole string."""
    mask = np.zeros((1078, 1916), dtype=np.uint8)
    for index in range(4):  # top line, 4 glyphs
        _glyph(mask, 100, 200 + index * 80, 60, 60)
    for index in range(4):  # second line, narrower
        _glyph(mask, 180, 200 + index * 65, 50, 60)
    verdict = text_consistency(mask, "i've\nbeen")
    assert verdict.lines == 2
    assert verdict.consistent is True


def test_empty_text_is_rejected() -> None:
    with pytest.raises(ExtractError, match="text|empty"):
        text_consistency(_word_mask(4), "   ")


def test_empty_mask_is_not_consistent() -> None:
    verdict = text_consistency(np.zeros((1078, 1916), dtype=np.uint8), "have")
    assert verdict.consistent is False


def test_verdict_is_auditable() -> None:
    record = text_consistency(_word_mask(4), "have").to_dict()
    for key in ("consistent", "reason", "components", "glyphs", "fill_ratio",
                "aspect_ratio", "expected_aspect_range", "lines"):
        assert key in record
