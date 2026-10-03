"""Non-circular font-identity proof.

A font claim is only ever as good as the words it was *not* fitted on. This module
implements the holdout protocol from ``DOCTRINE.md`` section 5.2: fit one uniform transform
on train words, freeze it, score unseen holdout words, and require at least two independent
*counting* metrics to independently prefer the same candidate by a decisive margin.

The anti-pattern this replaces: scoring each candidate face by sweeping ``sx`` and ``sy``
independently *per word* (e.g. across ``np.linspace(.96, 1.04, 17)``), keeping each word's best
combination and reporting the mean of those best scores. That is circular twice over: no word
is ever held out, and a single blended scalar such as ``.6*dice + .4*iou`` cannot show "two
metrics agreeing". Such a procedure routinely produces winning margins around 0.01, which is
noise, not identity.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Tuple

__all__ = [
    "COUNTING_METRICS",
    "SUPPORTING_METRICS",
    "FontProofError",
    "HoldoutSplit",
    "HoldoutVerdict",
    "MIN_HOLDOUT_MARGIN",
    "MetricSpec",
    "evaluate_font_hypothesis",
    "split_words",
]


@dataclass(frozen=True)
class MetricSpec:
    """One admissible metric.

    ``direction`` is ``"higher"`` when a larger number is better and ``"lower"`` for
    distances reported in pixels. ``bound`` is an absolute gate in the metric's own units
    (a floor for ``higher``, a ceiling for ``lower``); ``None`` means the metric is judged
    on its margin over the runner-up alone.
    """

    name: str
    direction: str
    bound: Optional[float]
    note: str = ""


# Counting metrics may satisfy the "two independent metrics agree" requirement.
# DOCTRINE.md section 5.4; sourced from A1 section 3.5.
COUNTING_METRICS: Dict[str, MetricSpec] = {
    "dice": MetricSpec(
        "dice",
        "higher",
        0.70,
        "real candidate holdout Dice sits at 0.699-0.921, so the floor sits below the "
        "corpus and the margin gate does the discriminating",
    ),
    "iou": MetricSpec("iou", "higher", 0.55, "systematically lower than Dice"),
    "f1_1px": MetricSpec("f1_1px", "higher", None, "one-pixel-tolerance F1"),
    "chamfer_px": MetricSpec(
        "chamfer_px",
        "lower",
        3.0,
        "symmetric contour/skeleton Chamfer distance in pixels; worked corpus 2.329-3.158",
    ),
    "outer_bound_coverage": MetricSpec("outer_bound_coverage", "higher", None, ""),
    "component_count_error": MetricSpec("component_count_error", "lower", None, ""),
    "hole_count_error": MetricSpec("hole_count_error", "lower", None, ""),
    "width_error_px": MetricSpec("width_error_px", "lower", None, ""),
    "height_error_px": MetricSpec("height_error_px", "lower", None, ""),
    "x_drift_px": MetricSpec("x_drift_px", "lower", None, "cumulative x-position drift"),
}

# Reportable, but never one of the two agreeing metrics (A1 section 3.5:
# normalised correlation r is "supporting only").
SUPPORTING_METRICS: Dict[str, MetricSpec] = {
    "correlation_r": MetricSpec("correlation_r", "higher", None, "supporting only"),
}

# Relative improvement over the runner-up required on every holdout word, for every
# counting metric. The historical false positive was a 1.56% relative margin
# (0.0129 / 0.8295), so this gate sits roughly an order of magnitude above that noise.
MIN_HOLDOUT_MARGIN = 0.05

# "Two independent metrics must agree" - one blended scalar is one metric, not two.
MIN_INDEPENDENT_METRICS = 2

_EPS = 1e-9


class FontProofError(ValueError):
    """Raised when a proof attempt is malformed, i.e. cannot be judged at all."""


@dataclass(frozen=True)
class HoldoutSplit:
    train: Tuple[str, ...]
    holdout: Tuple[str, ...]


@dataclass(frozen=True)
class HoldoutVerdict:
    proven: bool
    identity_status: str
    reason: str
    train_words: Tuple[str, ...]
    holdout_words: Tuple[str, ...]
    metrics_agreeing: Tuple[str, ...]
    per_metric: Mapping[str, Mapping[str, float]]
    runner_up: Mapping[str, Mapping[str, float]]

    def to_dict(self) -> Dict[str, object]:
        return {
            "identity_status": self.identity_status,
            "proven": self.proven,
            "reason": self.reason,
            "train_words": list(self.train_words),
            "holdout_words": list(self.holdout_words),
            "metrics_agreeing": list(self.metrics_agreeing),
            "per_metric": {
                metric: dict(scores) for metric, scores in self.per_metric.items()
            },
            "runner_up": {
                metric: dict(scores) for metric, scores in self.runner_up.items()
            },
            "gates": {
                "min_holdout_margin": MIN_HOLDOUT_MARGIN,
                "min_independent_metrics": MIN_INDEPENDENT_METRICS,
                "bounds": {
                    name: spec.bound
                    for name, spec in COUNTING_METRICS.items()
                    if spec.bound is not None
                },
            },
        }


def _digest_rank(word: str) -> str:
    return hashlib.sha256(word.encode("utf-8")).hexdigest()


def split_words(words: Sequence[str], *, holdout_fraction: float = 0.5) -> HoldoutSplit:
    """Deterministically split words into train/holdout sets.

    Ordering is by SHA-256 of the word rather than alphabetical, so the split does not
    systematically sort short words onto one side.
    """
    unique = list(dict.fromkeys(words))
    if len(unique) < 2:
        raise FontProofError(
            f"cannot hold any words out: need at least 2 distinct words, got {len(unique)}"
        )
    if not 0 < holdout_fraction < 1:
        raise FontProofError(f"holdout_fraction must be in (0, 1), got {holdout_fraction}")

    ordered = sorted(unique, key=_digest_rank)
    n_holdout = max(1, min(len(ordered) - 1, round(len(ordered) * holdout_fraction)))
    return HoldoutSplit(train=tuple(ordered[n_holdout:]), holdout=tuple(ordered[:n_holdout]))


def _check_transform(transform: Mapping[str, object]) -> None:
    forbidden = sorted(key for key in transform if key in {"scale_x", "scale_y", "scale_xy"})
    if forbidden:
        raise FontProofError(
            "anisotropic transform rejected: "
            f"{', '.join(forbidden)} present; only a single uniform 'scale' is allowed"
        )
    if "scale" not in transform:
        raise FontProofError("transform must declare a uniform 'scale'")
    scale = transform["scale"]
    if isinstance(scale, (list, tuple)):
        raise FontProofError(
            "anisotropic transform rejected: 'scale' must be a single uniform number, "
            f"got {scale!r}"
        )
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
        raise FontProofError(f"transform 'scale' must be a positive number, got {scale!r}")


def _resolve_metrics(metrics: Mapping[str, Mapping[str, float]]) -> Tuple[str, ...]:
    """Return the counting metric names, rejecting anything unrecognised."""
    unknown = sorted(
        name
        for name in metrics
        if name not in COUNTING_METRICS and name not in SUPPORTING_METRICS
    )
    if unknown:
        raise FontProofError(
            "unknown metric(s): "
            + ", ".join(unknown)
            + f"; admissible counting metrics are {sorted(COUNTING_METRICS)} and "
            f"supporting-only metrics are {sorted(SUPPORTING_METRICS)}. A blended score "
            "is one metric, not two."
        )
    counting = tuple(sorted(name for name in metrics if name in COUNTING_METRICS))
    if len(counting) < MIN_INDEPENDENT_METRICS:
        supporting = sorted(name for name in metrics if name in SUPPORTING_METRICS)
        raise FontProofError(
            f"need at least {MIN_INDEPENDENT_METRICS} independent counting metrics that "
            f"agree, got {len(counting)}: {list(counting)}"
            + (
                f"; {supporting} are supporting-only and cannot be counted"
                if supporting
                else ""
            )
        )
    return counting


def _improvement(spec: MetricSpec, score: float, rival: float) -> float:
    """Relative improvement over the runner-up, in the metric's favourable direction."""
    denominator = max(abs(rival), _EPS)
    if spec.direction == "higher":
        return (score - rival) / denominator
    return (rival - score) / denominator


def _within_bound(spec: MetricSpec, score: float) -> bool:
    if spec.bound is None:
        return True
    if spec.direction == "higher":
        return score >= spec.bound
    return score <= spec.bound


def evaluate_font_hypothesis(
    *,
    train_words: Sequence[str],
    holdout_words: Sequence[str],
    transform: Mapping[str, object],
    metrics: Mapping[str, Mapping[str, float]],
    runner_up_metrics: Mapping[str, Mapping[str, float]],
) -> HoldoutVerdict:
    """Judge one candidate face against the runner-up on unseen words."""
    train = tuple(train_words)
    holdout = tuple(holdout_words)

    if not train:
        raise FontProofError("no train words supplied")
    if not holdout:
        raise FontProofError("no holdout words supplied")

    overlap = sorted(set(train) & set(holdout))
    if overlap:
        raise FontProofError(
            "train and holdout words must be disjoint; overlap on: " + ", ".join(overlap)
        )

    _check_transform(transform)
    counting = _resolve_metrics(metrics)

    missing_runner_up = sorted(set(metrics) - set(runner_up_metrics))
    if missing_runner_up:
        raise FontProofError(
            "runner-up scores missing for metric(s): " + ", ".join(missing_runner_up)
        )

    for metric, scores in metrics.items():
        absent = [word for word in holdout if word not in scores]
        if absent:
            raise FontProofError(
                f"metric {metric!r} has no coverage for holdout word(s): " + ", ".join(absent)
            )
        runner_absent = [word for word in holdout if word not in runner_up_metrics[metric]]
        if runner_absent:
            raise FontProofError(
                f"runner-up metric {metric!r} has no coverage for holdout word(s): "
                + ", ".join(runner_absent)
            )

    accepting: list[str] = []
    rejecting: list[str] = []
    failures: Dict[str, list[str]] = {}

    for metric in counting:
        spec = COUNTING_METRICS[metric]
        bad_words: list[str] = []
        for word in holdout:
            score = float(metrics[metric][word])
            rival = float(runner_up_metrics[metric][word])
            if (
                _improvement(spec, score, rival) < MIN_HOLDOUT_MARGIN
                or not _within_bound(spec, score)
            ):
                bad_words.append(word)
        if bad_words:
            rejecting.append(metric)
            failures[metric] = bad_words
        else:
            accepting.append(metric)

    frozen_metrics = {metric: dict(scores) for metric, scores in metrics.items()}
    frozen_runner_up = {metric: dict(scores) for metric, scores in runner_up_metrics.items()}

    if not rejecting:
        return HoldoutVerdict(
            proven=True,
            identity_status="HOLDOUT_PROVEN",
            reason=(
                f"{len(counting)} independent counting metrics ({', '.join(counting)}) "
                f"each prefer this face on all {len(holdout)} unseen words by at least "
                f"{MIN_HOLDOUT_MARGIN:.0%} relative margin"
            ),
            train_words=train,
            holdout_words=holdout,
            metrics_agreeing=counting,
            per_metric=frozen_metrics,
            runner_up=frozen_runner_up,
        )

    if accepting:
        reason = (
            "metrics disagree: "
            f"{', '.join(sorted(accepting))} accept but {', '.join(sorted(rejecting))} "
            "reject this face on unseen words; "
            + "; ".join(
                f"{metric} fails on {', '.join(failures[metric])}"
                for metric in sorted(rejecting)
            )
        )
    else:
        weakest = sorted({word for words in failures.values() for word in words})
        reason = (
            "insufficient margin over the runner-up on unseen word(s): "
            + ", ".join(weakest)
            + f" (every counting metric must beat the runner-up by >= {MIN_HOLDOUT_MARGIN:.0%} "
            "relative on every holdout word and stay within its absolute bound); failing "
            "metrics: "
            + "; ".join(
                f"{metric} on {', '.join(failures[metric])}" for metric in sorted(failures)
            )
        )

    return HoldoutVerdict(
        proven=False,
        identity_status="HYPOTHESIS",
        reason=reason,
        train_words=train,
        holdout_words=holdout,
        metrics_agreeing=tuple(sorted(accepting)),
        per_metric=frozen_metrics,
        runner_up=frozen_runner_up,
    )
