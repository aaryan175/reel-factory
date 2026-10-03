"""Mask evidence tiering.

<ref-11> classifies every recovered mask as ``clean | secondary | diagnostic_partial`` and only
``clean`` enters aggregate ranking (DOCTRINE rule 6.2). Without tiering, a polluted
measurement is silently accepted — e.g. a state over bright footage measured ink median RGB (215, 225, 224) where authored
white should read near 249-252, because background inside the core box entered the sample.

The discriminating signal is separation, not variance. Scene-reactive ink legitimately
varies inside the glyph (the sealed treatment contract records states carrying
"cyan/pink/cream/navy or scene-reactive internal color"), so variance alone must not
disqualify a mask.
"""

from __future__ import annotations

import numpy as np
import pytest

from reelctl.captions.extract import (
    CLEAN_SEPARATION,
    ExtractError,
    classify_mask_tier,
    is_promotable_tier,
    resolve_mask_tier,
)


def _frame(value: int = 20) -> np.ndarray:
    return np.full((160, 240, 3), value, dtype=np.uint8)


def test_only_clean_is_promotable_to_production_alpha() -> None:
    assert is_promotable_tier("clean") is True
    assert is_promotable_tier("secondary") is False
    assert is_promotable_tier("diagnostic_partial") is False


def test_unknown_tier_is_rejected() -> None:
    with pytest.raises(ExtractError, match="tier"):
        is_promotable_tier("probably_fine")


def test_white_ink_on_dark_ground_is_clean() -> None:
    frame = _frame(18)
    frame[60:100, 80:160] = (250, 250, 250)
    mask = np.zeros((160, 240), dtype=np.uint8)
    mask[60:100, 80:160] = 255
    verdict = classify_mask_tier(frame, mask)
    assert verdict.tier == "clean"
    assert verdict.separation > 60


def test_mask_that_swallows_background_is_not_clean() -> None:
    """A mask twice the size of the ink it covers has no separation from its surround."""
    frame = _frame(200)  # bright footage
    frame[60:100, 80:160] = (215, 218, 216)  # barely brighter "ink"
    mask = np.zeros((160, 240), dtype=np.uint8)
    mask[50:110, 70:170] = 255  # mask is larger than the ink
    verdict = classify_mask_tier(frame, mask)
    assert verdict.tier != "clean"
    assert verdict.separation < 30


def test_footage_reactive_ink_is_downgraded_even_when_well_separated() -> None:
    """<ref-11> downgrades footage-reactive ink: it prevents clean whole-word authority.

    This is not a contradiction of the sealed treatment contract, which records that
    several states legitimately carry "cyan/pink/cream/navy or scene-reactive internal
    color". The ink is authored that way *and* the mask cannot be called clean for ranking
    purposes, because the glyph's own interior no longer reads as one ink field.
    """
    frame = _frame(15)
    patch = np.zeros((40, 80, 3), dtype=np.uint8)
    patch[:, :40] = (240, 120, 200)  # one internal colour
    patch[:, 40:] = (120, 240, 220)  # another
    frame[60:100, 80:160] = patch
    mask = np.zeros((160, 240), dtype=np.uint8)
    mask[60:100, 80:160] = 255
    verdict = classify_mask_tier(frame, mask)
    assert verdict.separation >= CLEAN_SEPARATION
    assert verdict.tier != "clean"
    assert "spread" in verdict.reason


def test_uniform_ink_with_good_separation_is_clean() -> None:
    frame = _frame(15)
    frame[60:100, 80:160] = (249, 250, 251)
    mask = np.zeros((160, 240), dtype=np.uint8)
    mask[60:100, 80:160] = 255
    verdict = classify_mask_tier(frame, mask)
    assert verdict.tier == "clean"
    assert verdict.interior_spread < 12


def test_dim_ink_with_a_bimodal_interior_is_not_clean() -> None:
    """Separation passes but the interior is ink plus background.

    A representative measurement: a state with separation 82 (over the
    clean gate) while its ink median was RGB (215, 225, 224) with p95 (247, 242, 235) -
    authored white should read near 249-252. <ref-11> classifies that state as `secondary`.
    """
    frame = _frame(15)
    patch = np.full((40, 80, 3), 250, dtype=np.uint8)
    patch[:, 40:] = 190  # background bleeding inside the mask
    frame[60:100, 80:160] = patch
    mask = np.zeros((160, 240), dtype=np.uint8)
    mask[60:100, 80:160] = 255
    verdict = classify_mask_tier(frame, mask)
    assert verdict.tier != "clean"


def test_marginal_separation_is_secondary_not_clean() -> None:
    frame = _frame(140)
    frame[60:100, 80:160] = (185, 185, 185)
    mask = np.zeros((160, 240), dtype=np.uint8)
    mask[60:100, 80:160] = 255
    verdict = classify_mask_tier(frame, mask)
    assert verdict.tier == "secondary"


def test_verdict_records_why_so_it_can_be_audited(caplog) -> None:
    frame = _frame(18)
    frame[60:100, 80:160] = (250, 250, 250)
    mask = np.zeros((160, 240), dtype=np.uint8)
    mask[60:100, 80:160] = 255
    record = classify_mask_tier(frame, mask).to_dict()
    assert record["tier"] == "clean"
    assert "separation" in record
    assert "interior_pixels" in record
    assert "ring_pixels" in record
    assert record["promotable"] is True


def test_empty_mask_cannot_be_tiered() -> None:
    with pytest.raises(ExtractError, match="empty|mask"):
        classify_mask_tier(_frame(), np.zeros((160, 240), dtype=np.uint8))


def test_mask_touching_the_frame_edge_still_tiers() -> None:
    """A caption cropped by the frame edge has a truncated ring but is still measurable."""
    frame = _frame(18)
    frame[0:40, 0:80] = (250, 250, 250)
    mask = np.zeros((160, 240), dtype=np.uint8)
    mask[0:40, 0:80] = 255
    verdict = classify_mask_tier(frame, mask)
    assert verdict.tier == "clean"
    assert verdict.ring_pixels > 0


def test_one_badly_separated_letter_downgrades_the_whole_word() -> None:
    """<ref-11> tiers *whole-word* authority, so the worst component governs.

    Calibrating against the sealed authority's own classification exposed this: a global
    median over the whole mask hides a single letter sitting over bright architecture, so
    states <ref-11> calls `diagnostic_partial` scored as clean. A word is clean only when every
    component separates cleanly.
    """
    frame = _frame(15)
    # Two letters: the first cleanly separated, the second sitting on a bright plate.
    frame[60:100, 40:90] = (250, 250, 250)
    frame[55:105, 120:190] = (232, 232, 232)  # bright surround
    frame[60:100, 130:180] = (250, 250, 250)  # letter on top of it
    mask = np.zeros((160, 240), dtype=np.uint8)
    mask[60:100, 40:90] = 255
    mask[60:100, 130:180] = 255

    verdict = classify_mask_tier(frame, mask)
    assert verdict.components == 2
    assert verdict.tier != "clean"
    assert verdict.worst_component_separation < CLEAN_SEPARATION


def test_every_component_cleanly_separated_stays_clean() -> None:
    frame = _frame(15)
    frame[60:100, 40:90] = (250, 250, 250)
    frame[60:100, 130:180] = (250, 250, 250)
    mask = np.zeros((160, 240), dtype=np.uint8)
    mask[60:100, 40:90] = 255
    mask[60:100, 130:180] = 255
    verdict = classify_mask_tier(frame, mask)
    assert verdict.components == 2
    assert verdict.tier == "clean"


# --- the machine proposes, the authority decides ------------------------------
#
# Calibrating against the sealed authority's own 12 classified states gave 4/12 exact
# agreement and 7/12 conservative. A single-frame contrast scalar does not reproduce a
# human's whole-word contour-authority judgment, which is informed by what sits behind the
# text (architecture, people, monitor detail). <ref-11> already states the governing rule for
# machine scores: "Inspect native overlays AFTER numerical ranking" and a rank is
# "candidate evidence, not original-editor metadata".
#
# So the classifier is a fail-closed gate: it may DOWNGRADE a tier, never promote one.


def test_machine_proposal_cannot_promote_an_authority_tier() -> None:
    resolved = resolve_mask_tier(machine_proposal="clean", authority_tier="diagnostic_partial")
    assert resolved.tier == "diagnostic_partial"
    assert resolved.tier_source == "sealed_authority"
    assert resolved.promotable is False


def test_machine_proposal_may_downgrade_an_authority_tier() -> None:
    """If the pixels look worse than the document claims, the pixels win."""
    resolved = resolve_mask_tier(machine_proposal="diagnostic_partial", authority_tier="clean")
    assert resolved.tier == "diagnostic_partial"
    assert resolved.tier_source == "machine_downgrade"
    assert resolved.promotable is False


def test_agreeing_tiers_keep_authority_provenance() -> None:
    resolved = resolve_mask_tier(machine_proposal="clean", authority_tier="clean")
    assert resolved.tier == "clean"
    assert resolved.tier_source == "sealed_authority"
    assert resolved.promotable is True


def test_machine_clean_alone_is_never_promotable() -> None:
    """With no authority and no human review, a machine 'clean' cannot bless a mask."""
    resolved = resolve_mask_tier(machine_proposal="clean", authority_tier=None)
    assert resolved.tier == "clean"
    assert resolved.tier_source == "machine_proposal"
    assert resolved.promotable is False


def test_human_review_can_promote_where_the_machine_is_unsure() -> None:
    resolved = resolve_mask_tier(
        machine_proposal="secondary", authority_tier=None, human_tier="clean"
    )
    assert resolved.tier == "clean"
    assert resolved.tier_source == "human_review"
    assert resolved.promotable is True


def test_human_review_cannot_override_a_machine_hard_failure() -> None:
    """A mask the pixels show as diagnostic_partial is not promotable by assertion."""
    resolved = resolve_mask_tier(
        machine_proposal="diagnostic_partial", authority_tier=None, human_tier="clean"
    )
    assert resolved.tier == "diagnostic_partial"
    assert resolved.promotable is False
