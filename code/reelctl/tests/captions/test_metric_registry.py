"""Metric polarity and metric admissibility.

Two defects the absorbed doctrine (A1 section 3.5) exposes in a naive scorer:

1. **Polarity.** Symmetric contour Chamfer distance is reported in pixels and is
   *lower is better*. A scorer that assumes every metric is higher-is-better will read a
   good Chamfer as a failure and a bad one as a pass.
2. **Admissibility.** Normalised correlation ``r`` is explicitly "supporting only". It may
   appear in a report but may never be one of the two independent metrics that agree.
   Likewise a blended scalar (the historical ``.6*dice + .4*iou``) is one metric, not two.

Unknown metric names fail closed rather than being silently treated as higher-is-better.
"""

from __future__ import annotations

import pytest

from reelctl.captions.fontproof import (
    COUNTING_METRICS,
    SUPPORTING_METRICS,
    FontProofError,
    evaluate_font_hypothesis,
)


def test_chamfer_is_registered_as_lower_is_better() -> None:
    assert COUNTING_METRICS["chamfer_px"].direction == "lower"
    assert COUNTING_METRICS["dice"].direction == "higher"
    assert COUNTING_METRICS["iou"].direction == "higher"


def test_correlation_r_is_supporting_only() -> None:
    assert "correlation_r" in SUPPORTING_METRICS
    assert "correlation_r" not in COUNTING_METRICS


def test_unknown_metric_name_fails_closed() -> None:
    with pytest.raises(FontProofError, match="unknown metric|blended_score"):
        evaluate_font_hypothesis(
            train_words=["alpha"],
            holdout_words=["echo"],
            transform={"scale": 1.0},
            metrics={
                "dice": {"echo": 0.97},
                "blended_score": {"echo": 0.95},
            },
            runner_up_metrics={
                "dice": {"echo": 0.71},
                "blended_score": {"echo": 0.60},
            },
        )


def test_supporting_metric_does_not_satisfy_the_two_metric_requirement() -> None:
    with pytest.raises(FontProofError, match="counting|independent|supporting"):
        evaluate_font_hypothesis(
            train_words=["alpha"],
            holdout_words=["echo"],
            transform={"scale": 1.0},
            metrics={
                "dice": {"echo": 0.97},
                "correlation_r": {"echo": 0.99},
            },
            runner_up_metrics={
                "dice": {"echo": 0.71},
                "correlation_r": {"echo": 0.60},
            },
        )


def test_good_chamfer_is_read_as_a_win_not_a_loss() -> None:
    """Candidate 2.329 px vs runner-up 2.562 px is a win (<ref-11> smoke values)."""
    verdict = evaluate_font_hypothesis(
        train_words=["have", "time"],
        holdout_words=["countless", "hours"],
        transform={"scale": 1.0},
        metrics={
            "dice": {"countless": 0.95, "hours": 0.96},
            "chamfer_px": {"countless": 2.329, "hours": 2.301},
        },
        runner_up_metrics={
            "dice": {"countless": 0.71, "hours": 0.72},
            "chamfer_px": {"countless": 3.158, "hours": 3.200},
        },
    )
    assert verdict.proven is True
    assert verdict.metrics_agreeing == ("chamfer_px", "dice")


def test_worse_chamfer_is_not_silently_accepted() -> None:
    """Candidate Chamfer larger than the runner-up must fail, not pass."""
    verdict = evaluate_font_hypothesis(
        train_words=["have", "time"],
        holdout_words=["countless", "hours"],
        transform={"scale": 1.0},
        metrics={
            "dice": {"countless": 0.95, "hours": 0.96},
            "chamfer_px": {"countless": 3.900, "hours": 4.100},
        },
        runner_up_metrics={
            "dice": {"countless": 0.71, "hours": 0.72},
            "chamfer_px": {"countless": 2.329, "hours": 2.301},
        },
    )
    assert verdict.proven is False
    assert "chamfer_px" in verdict.reason


def test_supporting_metric_may_ride_along_when_two_counting_metrics_agree() -> None:
    verdict = evaluate_font_hypothesis(
        train_words=["have", "time"],
        holdout_words=["countless", "hours"],
        transform={"scale": 1.0},
        metrics={
            "dice": {"countless": 0.95, "hours": 0.96},
            "iou": {"countless": 0.91, "hours": 0.92},
            "correlation_r": {"countless": 0.99, "hours": 0.99},
        },
        runner_up_metrics={
            "dice": {"countless": 0.71, "hours": 0.72},
            "iou": {"countless": 0.58, "hours": 0.59},
            "correlation_r": {"countless": 0.60, "hours": 0.61},
        },
    )
    assert verdict.proven is True
    # correlation_r is reported but is not counted as one of the agreeing metrics.
    assert "correlation_r" not in verdict.metrics_agreeing
    assert "correlation_r" in verdict.to_dict()["per_metric"]


def test_dice_absolute_floor_is_calibrated_to_the_real_corpus() -> None:
    """Real candidate holdout Dice sits at 0.699-0.921; the floor must sit below it."""
    assert COUNTING_METRICS["dice"].bound is not None
    assert COUNTING_METRICS["dice"].bound <= 0.70
