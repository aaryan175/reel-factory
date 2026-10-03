"""Non-circular font-identity proof.

Anti-pattern this replaces: a font-family check that scores a handful of candidate faces over
a handful of reference words. For **every word** it swept ``sx`` and ``sy`` independently across
``np.linspace(.96, 1.04, 17)`` and took the best-scoring combination, then reported
``mean`` and ``min`` of those per-word best scores. Two things make that circular:

1. no holdout — every word used to fit the transform is also a word being scored, so the
   number reports "can this face be squeezed onto this ink" not "is this the face";
2. one blended metric — ``s = .6*dice + .4*iou`` is a single scalar, so "two metrics
   agreeing" was never actually tested.

The resulting margin was ``mean=0.8424`` for the winner against ``runner_up_mean=0.8295``
(recorded in ``reference_blueprint.json``): a 0.0129 gap, which the blueprint itself
honestly labels ``STRONG_MULTI_WORD_SHAPE_MATCH_NOT_ORIGINAL_ASSET_PROVEN``.

The engine therefore requires: one transform fitted on train words only, frozen, then
applied to unseen holdout words, scored by >=2 independent metrics that must independently
rank the same candidate first.
"""

from __future__ import annotations

import pytest

from reelctl.captions.fontproof import (
    FontProofError,
    HoldoutSplit,
    evaluate_font_hypothesis,
    split_words,
)


def test_split_is_disjoint_and_covers_every_word() -> None:
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    split = split_words(words, holdout_fraction=0.5)
    assert isinstance(split, HoldoutSplit)
    assert set(split.train) | set(split.holdout) == set(words)
    assert not set(split.train) & set(split.holdout)


def test_split_is_deterministic_for_the_same_word_list() -> None:
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    assert split_words(words, holdout_fraction=0.5) == split_words(words, holdout_fraction=0.5)


def test_split_refuses_when_too_few_words_to_hold_any_out() -> None:
    with pytest.raises(FontProofError, match="holdout|words"):
        split_words(["Gold"], holdout_fraction=0.5)


def test_overlapping_train_and_holdout_words_are_rejected() -> None:
    with pytest.raises(FontProofError, match="overlap|disjoint"):
        evaluate_font_hypothesis(
            train_words=["alpha", "echo"],
            holdout_words=["echo"],
            transform={"scale": 1.0},
            metrics={
                "dice": {"alpha": 0.9, "echo": 0.9},
                "iou": {"alpha": 0.85, "echo": 0.85},
            },
            runner_up_metrics={
                "dice": {"alpha": 0.7, "echo": 0.7},
                "iou": {"alpha": 0.6, "echo": 0.6},
            },
        )


def test_a_single_metric_cannot_prove_identity() -> None:
    with pytest.raises(FontProofError, match="two|independent|metric"):
        evaluate_font_hypothesis(
            train_words=["alpha"],
            holdout_words=["echo"],
            transform={"scale": 1.0},
            metrics={"dice": {"echo": 0.95}},
            runner_up_metrics={"dice": {"echo": 0.60}},
        )


def test_anisotropic_transform_is_rejected_outright() -> None:
    with pytest.raises(FontProofError, match="scale|anisotropic|uniform"):
        evaluate_font_hypothesis(
            train_words=["alpha"],
            holdout_words=["echo"],
            transform={"scale_x": 1.0, "scale_y": 1.04},
            metrics={
                "dice": {"echo": 0.95},
                "iou": {"echo": 0.91},
            },
            runner_up_metrics={
                "dice": {"echo": 0.60},
                "iou": {"echo": 0.55},
            },
        )


def test_metrics_must_cover_every_holdout_word() -> None:
    with pytest.raises(FontProofError, match="down|coverage|missing"):
        evaluate_font_hypothesis(
            train_words=["alpha"],
            holdout_words=["echo", "foxtrot"],
            transform={"scale": 1.0},
            metrics={
                "dice": {"echo": 0.95},
                "iou": {"echo": 0.91},
            },
            runner_up_metrics={
                "dice": {"echo": 0.60, "foxtrot": 0.61},
                "iou": {"echo": 0.55, "foxtrot": 0.56},
            },
        )


def test_disagreeing_metrics_do_not_prove_identity() -> None:
    """dice prefers the candidate, iou prefers the runner-up -> unproven."""
    verdict = evaluate_font_hypothesis(
        train_words=["alpha", "bravo"],
        holdout_words=["echo", "foxtrot"],
        transform={"scale": 1.0},
        metrics={
            "dice": {"echo": 0.95, "foxtrot": 0.94},
            "iou": {"echo": 0.50, "foxtrot": 0.51},
        },
        runner_up_metrics={
            "dice": {"echo": 0.60, "foxtrot": 0.61},
            "iou": {"echo": 0.88, "foxtrot": 0.87},
        },
    )
    assert verdict.identity_status == "HYPOTHESIS"
    assert verdict.proven is False
    assert "disagree" in verdict.reason.lower()


def test_thin_margin_over_runner_up_does_not_prove_identity() -> None:
    """The historical 0.8424 vs 0.8295 case must come back UNPROVEN."""
    verdict = evaluate_font_hypothesis(
        train_words=["alpha", "bravo", "charlie", "delta", "echo"],
        holdout_words=["foxtrot", "golf", "hotel", "india"],
        transform={"scale": 1.0},
        metrics={
            "dice": {"foxtrot": 0.8424, "golf": 0.8424, "hotel": 0.8424, "india": 0.8424},
            "iou": {"foxtrot": 0.7577, "golf": 0.7577, "hotel": 0.7577, "india": 0.7577},
        },
        runner_up_metrics={
            "dice": {"foxtrot": 0.8295, "golf": 0.8295, "hotel": 0.8295, "india": 0.8295},
            "iou": {"foxtrot": 0.7450, "golf": 0.7450, "hotel": 0.7450, "india": 0.7450},
        },
    )
    assert verdict.proven is False
    assert verdict.identity_status == "HYPOTHESIS"
    assert "margin" in verdict.reason.lower()


def test_clear_agreeing_margin_on_unseen_words_proves_identity() -> None:
    verdict = evaluate_font_hypothesis(
        train_words=["alpha", "bravo", "charlie"],
        holdout_words=["echo", "foxtrot", "india"],
        transform={"scale": 1.0},
        metrics={
            "dice": {"echo": 0.97, "foxtrot": 0.96, "india": 0.965},
            "iou": {"echo": 0.94, "foxtrot": 0.93, "india": 0.935},
        },
        runner_up_metrics={
            "dice": {"echo": 0.71, "foxtrot": 0.70, "india": 0.72},
            "iou": {"echo": 0.58, "foxtrot": 0.57, "india": 0.59},
        },
    )
    assert verdict.proven is True
    assert verdict.identity_status == "HOLDOUT_PROVEN"
    assert verdict.metrics_agreeing == ("dice", "iou")


def test_one_weak_holdout_word_blocks_the_proof() -> None:
    """min over holdout words gates the claim, not the mean."""
    verdict = evaluate_font_hypothesis(
        train_words=["alpha", "bravo", "charlie"],
        holdout_words=["echo", "foxtrot", "india"],
        transform={"scale": 1.0},
        metrics={
            "dice": {"echo": 0.97, "foxtrot": 0.96, "india": 0.42},
            "iou": {"echo": 0.94, "foxtrot": 0.93, "india": 0.31},
        },
        runner_up_metrics={
            "dice": {"echo": 0.71, "foxtrot": 0.70, "india": 0.72},
            "iou": {"echo": 0.58, "foxtrot": 0.57, "india": 0.59},
        },
    )
    assert verdict.proven is False
    assert "india" in verdict.reason


def test_verdict_records_the_full_audit_trail() -> None:
    verdict = evaluate_font_hypothesis(
        train_words=["alpha", "bravo", "charlie"],
        holdout_words=["echo", "foxtrot", "india"],
        transform={"scale": 1.0},
        metrics={
            "dice": {"echo": 0.97, "foxtrot": 0.96, "india": 0.965},
            "iou": {"echo": 0.94, "foxtrot": 0.93, "india": 0.935},
        },
        runner_up_metrics={
            "dice": {"echo": 0.71, "foxtrot": 0.70, "india": 0.72},
            "iou": {"echo": 0.58, "foxtrot": 0.57, "india": 0.59},
        },
    )
    record = verdict.to_dict()
    assert record["train_words"] == ["alpha", "bravo", "charlie"]
    assert record["holdout_words"] == ["echo", "foxtrot", "india"]
    assert record["identity_status"] == "HOLDOUT_PROVEN"
    # The audit trail must never silently drop which words were scored.
    assert set(record["per_metric"]["dice"]) == {"echo", "foxtrot", "india"}
