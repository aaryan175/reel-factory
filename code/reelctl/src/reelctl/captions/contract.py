"""Caption contract validation.

Schema validation catches shape; this module adds the cross-field checks a JSON schema
cannot express: span arithmetic, clock containment, style resolution, stacking order for
concurrent states, and the rule that only a holdout-proven face may unlock the
native-font render path.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Dict, Mapping

from ..contracts import ContractValidationError, schema_sha256, validate_contract

__all__ = ["CaptionContractError", "SCHEMA_NAME", "validate_caption_contract"]

SCHEMA_NAME = "caption-contract"


class CaptionContractError(ValueError):
    pass


def _fail(message: str) -> None:
    raise CaptionContractError(message)


def _check_spans(states: list[Mapping[str, Any]], frame_count: int) -> None:
    for state in states:
        state_id = state["id"]
        start = state["start_frame"]
        end = state["end_frame_exclusive"]
        frames = state["frames"]
        if end <= start:
            _fail(
                f"state {state_id}: end_frame_exclusive {end} must be greater than "
                f"start_frame {start}"
            )
        if frames != end - start:
            _fail(
                f"state {state_id}: frames {frames} does not equal the declared span "
                f"{end} - {start} = {end - start}"
            )
        if end > frame_count:
            _fail(
                f"state {state_id}: end_frame_exclusive {end} runs past the reference "
                f"clock frame_count {frame_count}"
            )


def _check_placement(states: list[Mapping[str, Any]]) -> None:
    """The treatment box must contain the core ink box (DOCTRINE.md rule 4.5)."""
    for state in states:
        placement = state["placement"]
        cx0, cy0, cx1, cy1 = placement["core_bbox_xyxy"]
        tx0, ty0, tx1, ty1 = placement["treatment_bbox_xyxy"]
        if cx1 <= cx0 or cy1 <= cy0:
            _fail(
                f"state {state['id']}: core_bbox_xyxy {placement['core_bbox_xyxy']} is empty"
            )
        if tx0 > cx0 or ty0 > cy0 or tx1 < cx1 or ty1 < cy1:
            _fail(
                f"state {state['id']}: treatment_bbox_xyxy "
                f"{placement['treatment_bbox_xyxy']} does not contain the core ink box "
                f"{placement['core_bbox_xyxy']}; the glow/shadow extent can never be "
                "smaller than the ink it surrounds"
            )


# AFFINE warns against "using a global unbounded vertical stretch to fix one opening word",
# so the exception is bounded. Approved reference-height fits typically land around a scale_y of
# 1.15-1.22, which sets the scale of a legitimate reference-height fit; anything materially past that is envelope filling, not geometry reproduction.
MAX_NAMED_EXCEPTION_AXIS_SCALE = 1.30
MIN_NAMED_EXCEPTION_AXIS_SCALE = 0.77

# The justification has to say what it is. These are the words that distinguish reproducing
# the reference's own geometry from filling a box someone drew.
_REPRODUCTION_MARKERS = ("geometry", "reproduc", "reference")
_ENVELOPE_MARKERS = ("envelope", "fills the target", "fill the target", "looks correct")


def _check_named_exceptions(states: list[Mapping[str, Any]]) -> None:
    """Enforce all five things an evidence-backed anisotropic fit must carry (rule 4.3)."""
    for state in states:
        exception = state["geometry"].get("named_exception")
        if exception is None:
            continue
        state_id = state["id"]

        scale = float(exception["axis_scale"])
        if scale == 1.0:
            _fail(
                f"state {state_id}: named_exception axis_scale is 1.0, which is not an "
                "exception at all; remove it"
            )
        if not MIN_NAMED_EXCEPTION_AXIS_SCALE <= scale <= MAX_NAMED_EXCEPTION_AXIS_SCALE:
            _fail(
                f"state {state_id}: named_exception axis_scale {scale} is outside the bounded "
                f"range [{MIN_NAMED_EXCEPTION_AXIS_SCALE}, "
                f"{MAX_NAMED_EXCEPTION_AXIS_SCALE}]; an unbounded stretch to make one word "
                "fit is envelope filling, not geometry reproduction"
            )

        frames = [int(frame) for frame in exception["evidence_frames"]]
        if len(frames) < 2:
            _fail(
                f"state {state_id}: named_exception needs direct multi-frame evidence, got "
                f"{len(frames)} evidence_frames"
            )
        outside = [
            frame
            for frame in frames
            if not state["start_frame"] <= frame < state["end_frame_exclusive"]
        ]
        if outside:
            _fail(
                f"state {state_id}: named_exception evidence_frames {outside} lie outside the "
                f"state's own span [{state['start_frame']}, {state['end_frame_exclusive']}); "
                "an exception is named per state and carries its own evidence"
            )

        justification = exception["justification"].lower()
        if not any(marker in justification for marker in _REPRODUCTION_MARKERS):
            _fail(
                f"state {state_id}: named_exception justification must state that this "
                "reproduces the reference's own geometry rather than substituting a font"
            )
        if any(marker in justification for marker in _ENVELOPE_MARKERS):
            _fail(
                f"state {state_id}: named_exception justification describes filling a target "
                "envelope, which is the forbidden warp, not geometry reproduction"
            )


def _check_ids(states: list[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for state in states:
        state_id = state["id"]
        if state_id in seen:
            _fail(f"duplicate state id {state_id}")
        seen.add(state_id)


def _check_styles(
    states: list[Mapping[str, Any]], styles: Mapping[str, Any]
) -> None:
    for state in states:
        style_id = state["style_id"]
        if style_id not in styles:
            _fail(
                f"state {state['id']} references undeclared style {style_id}; "
                f"declared styles are {sorted(styles)}"
            )
    for style_id, style in styles.items():
        inherits = style.get("inherits")
        if inherits is not None and inherits not in styles:
            _fail(f"style {style_id} inherits undeclared style {inherits}")


def _check_stacking(states: list[Mapping[str, Any]]) -> None:
    for left, right in combinations(states, 2):
        overlaps = (
            left["start_frame"] < right["end_frame_exclusive"]
            and right["start_frame"] < left["end_frame_exclusive"]
        )
        if not overlaps:
            continue
        if left["stacking"]["z"] == right["stacking"]["z"]:
            _fail(
                f"states {left['id']} and {right['id']} overlap on frames "
                f"[{max(left['start_frame'], right['start_frame'])}, "
                f"{min(left['end_frame_exclusive'], right['end_frame_exclusive'])}) "
                f"but share stacking z={left['stacking']['z']}; concurrent captions must "
                "declare an explicit stacking order"
            )


def _check_font_proofs(
    styles: Mapping[str, Any], hypotheses: list[Mapping[str, Any]]
) -> None:
    by_style: Dict[str, list[Mapping[str, Any]]] = {}
    for hypothesis in hypotheses:
        by_style.setdefault(hypothesis["style_id"], []).append(hypothesis)
        status = hypothesis["identity_status"]
        if status == "HOLDOUT_PROVEN":
            holdout = hypothesis.get("holdout")
            if holdout is None:
                _fail(
                    f"font hypothesis for style {hypothesis['style_id']} claims "
                    "HOLDOUT_PROVEN without a holdout record"
                )
            if holdout["identity_status"] != "HOLDOUT_PROVEN":
                _fail(
                    f"font hypothesis for style {hypothesis['style_id']} claims "
                    "HOLDOUT_PROVEN but its holdout record says "
                    f"{holdout['identity_status']}"
                )
            if len(holdout["metrics_agreeing"]) < 2:
                _fail(
                    f"font hypothesis for style {hypothesis['style_id']} claims "
                    "HOLDOUT_PROVEN but fewer than two metrics agree: "
                    f"{holdout['metrics_agreeing']}"
                )
            overlap = sorted(set(holdout["train_words"]) & set(holdout["holdout_words"]))
            if overlap:
                _fail(
                    f"font hypothesis for style {hypothesis['style_id']} has a circular "
                    "holdout: train and holdout words overlap on " + ", ".join(overlap)
                )
        if status == "ORIGINAL_ASSET_PROVEN" and "original_asset_provenance" not in hypothesis:
            _fail(
                f"font hypothesis for style {hypothesis['style_id']} claims "
                "ORIGINAL_ASSET_PROVEN without original_asset_provenance evidence"
            )

    for style_id, style in styles.items():
        if style["render_path"] != "native_font":
            continue
        candidates = by_style.get(style_id, [])
        proven = [
            candidate
            for candidate in candidates
            if candidate["identity_status"] in {"HOLDOUT_PROVEN", "ORIGINAL_ASSET_PROVEN"}
        ]
        if not proven:
            statuses = sorted({candidate["identity_status"] for candidate in candidates})
            _fail(
                f"style {style_id} selects the native_font render path but has no "
                "holdout-proven or original-asset-proven font hypothesis "
                f"(hypothesis statuses: {statuses or 'none'}); use source_contour instead"
            )


def validate_caption_contract(payload: Any) -> Dict[str, Any]:
    """Validate a caption contract, failing closed.

    Returns a receipt on success; raises :class:`CaptionContractError` otherwise. The
    payload is never mutated.
    """
    try:
        validate_contract(SCHEMA_NAME, payload)
    except ContractValidationError as error:
        raise CaptionContractError(str(error)) from error

    states = list(payload["states"])
    styles = payload["styles"]
    frame_count = payload["reference"]["frame_count"]

    _check_ids(states)
    _check_spans(states, frame_count)
    _check_placement(states)
    _check_named_exceptions(states)
    _check_styles(states, styles)
    _check_stacking(states)
    _check_font_proofs(styles, list(payload["font_hypotheses"]))

    if payload["render_allowed"]:
        if payload["status"] != "SEALED":
            _fail(
                "render_allowed is true but the contract status is "
                f"{payload['status']}; only a SEALED contract may authorise a render"
            )

    return {
        "status": "PASS",
        "schema": SCHEMA_NAME,
        "schema_sha256": schema_sha256(SCHEMA_NAME),
        "states": len(states),
        "styles": len(styles),
        "frame_count": frame_count,
    }
