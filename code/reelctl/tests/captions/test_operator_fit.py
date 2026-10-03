"""Compositing-operator fitting (DOCTRINE rule 3.4, from BITF section 5).

The engine has **no default operator**. <ref-06>'s reference fits a Difference-family inversion;
<ref-22>'s fits near-white SourceOver. Both must be fitted per reference per stack and compared by
residual distributions:

    SourceOver         O = B + a*(F - B)
    Difference-family  O = B + a*(|B - S| - B)

Two hard rules come with it:

* Never name an editor blend mode from a flattened encode. The verdict is
  ``difference_family_inversion``, never "Difference" or "Exclusion" — a flattened source
  cannot distinguish them (sealed treatment contracts say exactly this).
* A thin margin between the two models is not a decision. A measured case fit
  near-black SourceOver at MAE 5.01 against a best Difference fit of 5.24 — close enough that
  the file warns it "is a useful warning against appearance-only mode naming".
"""

from __future__ import annotations

import numpy as np
import pytest

from reelctl.captions.extract import (
    ExtractError,
    fit_compositing_operator,
    segment_difference_family_ink,
)


def _background(height: int = 120, width: int = 200) -> np.ndarray:
    """A non-uniform background, so an operator fit is actually constrained."""
    column = np.linspace(30, 220, width)
    row = np.linspace(0, 35, height)
    base = (column[None, :] + row[:, None]).clip(0, 255)
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = base
    frame[:, :, 1] = (base * 0.95).clip(0, 255)
    frame[:, :, 2] = (base * 0.90).clip(0, 255)
    return frame


def _mask(height: int = 120, width: int = 200) -> np.ndarray:
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[40:80, 60:140] = 255
    return mask


def _composite_sourceover(background, mask, fill=(250, 248, 246), alpha=1.0):
    out = background.astype(np.float64).copy()
    where = mask > 0
    for channel, value in enumerate(reversed(fill)):  # fill is RGB, array is BGR
        out[:, :, channel][where] = (
            background[:, :, channel][where] + alpha * (value - background[:, :, channel][where])
        )
    return out.round().clip(0, 255).astype(np.uint8)


def _composite_difference(background, mask, source=(255, 255, 255), alpha=1.0):
    out = background.astype(np.float64).copy()
    where = mask > 0
    for channel, value in enumerate(reversed(source)):
        base = background[:, :, channel][where].astype(np.float64)
        inverted = np.abs(base - value)
        out[:, :, channel][where] = base + alpha * (inverted - base)
    return out.round().clip(0, 255).astype(np.uint8)


# --- fitting ------------------------------------------------------------------


def test_sourceover_composite_is_fitted_as_sourceover() -> None:
    background = _background()
    mask = _mask()
    output = _composite_sourceover(background, mask)
    fit = fit_compositing_operator(output, background, mask)
    assert fit.model == "source_over"
    assert fit.mae < 2.0


def test_difference_composite_is_fitted_as_difference_family() -> None:
    background = _background()
    mask = _mask()
    output = _composite_difference(background, mask)
    fit = fit_compositing_operator(output, background, mask)
    assert fit.model == "difference_family_inversion"
    assert fit.mae < 2.0


def test_the_verdict_never_names_an_editor_blend_mode() -> None:
    background = _background()
    mask = _mask()
    fit = fit_compositing_operator(_composite_difference(background, mask), background, mask)
    record = fit.to_dict()
    text = " ".join(str(value).lower() for value in record.values())
    assert "exclusion" not in text
    assert "difference_family_inversion" in record["model"]
    assert record["model"] != "Difference"


def test_fit_reports_both_models_so_the_margin_is_visible() -> None:
    background = _background()
    mask = _mask()
    fit = fit_compositing_operator(_composite_sourceover(background, mask), background, mask)
    record = fit.to_dict()
    assert "mae_source_over" in record
    assert "mae_difference_family" in record
    assert record["mae_source_over"] < record["mae_difference_family"]


def test_a_thin_margin_is_reported_as_undecided() -> None:
    """A measured case: 5.01 vs 5.24 is not a decision."""
    background = _background()
    mask = _mask()
    # Mid-grey ink on this background makes both models fit comparably.
    output = _composite_sourceover(background, mask, fill=(128, 122, 116), alpha=0.5)
    fit = fit_compositing_operator(output, background, mask)
    if fit.margin < fit.undecided_margin:
        assert fit.decided is False
        assert "undecided" in fit.reason.lower() or "margin" in fit.reason.lower()
    else:
        assert fit.decided is True


def test_fit_recovers_the_alpha_it_was_composited_with() -> None:
    background = _background()
    mask = _mask()
    output = _composite_sourceover(background, mask, fill=(250, 248, 246), alpha=0.6)
    fit = fit_compositing_operator(output, background, mask)
    assert abs(fit.alpha - 0.6) < 0.12


def test_fit_needs_a_non_empty_mask() -> None:
    background = _background()
    with pytest.raises(ExtractError, match="empty|mask"):
        fit_compositing_operator(background, background, np.zeros((120, 200), dtype=np.uint8))


def test_fit_rejects_mismatched_shapes() -> None:
    with pytest.raises(ExtractError, match="shape"):
        fit_compositing_operator(_background(), _background(60, 100), _mask())


def test_a_uniform_background_cannot_constrain_the_operator() -> None:
    """With no background variation the two models are indistinguishable; say so."""
    flat = np.full((120, 200, 3), 100, dtype=np.uint8)
    mask = _mask()
    fit = fit_compositing_operator(_composite_difference(flat, mask), flat, mask)
    assert fit.decided is False
    assert "background" in fit.reason.lower()


# --- difference-family segmentation -------------------------------------------


def test_difference_family_ink_is_recovered_by_anticorrelation() -> None:
    """Under inversion the ink reads as 255 - B, so it is anti-correlated with the plate."""
    background = _background()
    mask = _mask()
    output = _composite_difference(background, mask)
    found = segment_difference_family_ink(
        output, background, roi_xyxy=(0, 0, 200, 120), min_delta=24, min_area=12
    )
    from reelctl.captions.extract import union_bbox

    assert union_bbox(found) == (60, 40, 140, 80)


def test_difference_family_segmentation_finds_nothing_without_ink() -> None:
    from reelctl.captions.extract import union_bbox

    background = _background()
    found = segment_difference_family_ink(
        background, background, roi_xyxy=(0, 0, 200, 120), min_delta=24, min_area=12
    )
    assert union_bbox(found) is None


def test_difference_family_segmentation_ignores_a_sourceover_caption() -> None:
    """A near-white SourceOver caption over a bright plate is not an inversion."""
    from reelctl.captions.extract import union_bbox

    background = np.full((120, 200, 3), 240, dtype=np.uint8)
    mask = _mask()
    output = _composite_sourceover(background, mask, fill=(250, 250, 250))
    found = segment_difference_family_ink(
        output, background, roi_xyxy=(0, 0, 200, 120), min_delta=24, min_area=12
    )
    assert union_bbox(found) is None
