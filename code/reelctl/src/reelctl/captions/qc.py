"""Renderer-independent caption QC.

Every number here is recomputed from the reference frames and the rendered frames. Nothing
reads the renderer's own constants, target boxes or manifest claims — that is the documented
circularity: forcing a candidate into a hard-coded envelope and then asserting the resulting
bbox equals that envelope "proves only that the resize instruction ran" (IMPL section 8).

Gates are state-class aware. A single threshold is wrong because a heavily blurred entry
frame legitimately has no measurable crisp bbox (A1 contradiction C8), so an entry frame is
judged by a relaxed gate and a crisp frame by the strict one.

A technical pass here is never a creative pass and never a human approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import cv2
import numpy as np

__all__ = [
    "BLUR_MIN_DICE",
    "CRISP_MIN_DICE",
    "MAX_INK_DISTANCE",
    "QcError",
    "StateVerdict",
    "TimingReport",
    "dice",
    "ink_distance",
    "iou",
    "is_codec_artifact",
    "qc_ink_identity",
    "qc_state",
    "qc_state_parity",
    "qc_timing",
    "qc_word_identity",
]

# A crisp frame must match the reference closely; a blur entry/exit frame is judged by a
# relaxed gate because its ink is authored soft.
CRISP_MIN_DICE = 0.90
BLUR_MIN_DICE = 0.60

EVIDENCE_LIMIT = (
    "Machine parity on the compared frames only. This is not a full normal-speed watch, not "
    "a native 1:1 enlarged contour review, and not creative approval."
)


class QcError(ValueError):
    pass


def _binary(mask: np.ndarray, name: str) -> np.ndarray:
    binary = (mask > 0)
    if not binary.any():
        raise QcError(f"{name} is empty; an empty mask cannot be scored as a match")
    return binary


def dice(reference_mask: np.ndarray, candidate_mask: np.ndarray) -> float:
    """Sorensen-Dice over set pixels."""
    left = _binary(reference_mask, "reference mask")
    right = _binary(candidate_mask, "candidate mask")
    total = int(left.sum()) + int(right.sum())
    return float(2 * int((left & right).sum()) / total)


def iou(reference_mask: np.ndarray, candidate_mask: np.ndarray) -> float:
    left = _binary(reference_mask, "reference mask")
    right = _binary(candidate_mask, "candidate mask")
    union = int((left | right).sum())
    return float(int((left & right).sum()) / union)


def ink_distance(
    reference_rgb: Sequence[int], candidate_rgb: Sequence[int]
) -> float:
    """Euclidean distance between two RGB triples, in 8-bit channel units."""
    left = np.asarray(reference_rgb, dtype=np.float64)
    right = np.asarray(candidate_rgb, dtype=np.float64)
    if left.shape != (3,) or right.shape != (3,):
        raise QcError("ink distance needs two RGB triples")
    return float(np.linalg.norm(left - right))


def is_codec_artifact(
    reference_mask: np.ndarray,
    candidate_mask: np.ndarray,
    *,
    tolerance_px: int = 2,
) -> bool:
    """Whether the whole difference sits inside a tolerance band of the shared boundary.

    One or two pixels of YUV420 chroma bleed and ordinary antialias change are not authored
    defects. A real contour defect persists at native scale and alters a meaningful
    structure — a closed counter, a missing dot or apostrophe, a clipped swash, fused
    letters, a notched core edge (NFS section 7). Such a difference lies outside the band.
    """
    if tolerance_px < 0:
        raise QcError(f"tolerance_px must be >= 0, got {tolerance_px}")
    left = (reference_mask > 0).astype(np.uint8)
    right = (candidate_mask > 0).astype(np.uint8)
    difference = cv2.bitwise_xor(left, right)
    if not difference.any():
        return True

    # The band is measured around the REFERENCE boundary only. Including the candidate's own
    # boundary band would let a candidate justify its own extension: a glyph grown by 4px
    # would supply the very tolerance that excuses it.
    kernel = np.ones((tolerance_px * 2 + 1,) * 2, np.uint8)
    grown = cv2.dilate(left, kernel)
    shrunk = cv2.erode(left, kernel)
    band = ((grown > 0) & (shrunk == 0)).astype(np.uint8)

    return not bool(((difference > 0) & (band == 0)).any())


@dataclass(frozen=True)
class StateVerdict:
    frame: int
    gate: str
    dice: float
    iou: float
    passed: bool
    codec_artifact: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frame": self.frame,
            "gate": self.gate,
            "dice": round(self.dice, 6),
            "iou": round(self.iou, 6),
            "passed": self.passed,
            "codec_artifact": self.codec_artifact,
            "reason": self.reason,
            "creative_approval": "PENDING",
            "evidence_limit": EVIDENCE_LIMIT,
            "gates": {"crisp_min_dice": CRISP_MIN_DICE, "blur_min_dice": BLUR_MIN_DICE},
        }


def qc_state(
    *,
    reference_mask: np.ndarray,
    rendered_alpha: np.ndarray,
    lifecycle_kind: str,
    is_crisp_frame: bool,
    frame: int,
    tolerance_px: int = 2,
) -> StateVerdict:
    """Score one rendered state frame against the reference's own ink for that frame."""
    if reference_mask.shape[:2] != rendered_alpha.shape[:2]:
        raise QcError(
            f"reference mask shape {reference_mask.shape[:2]} does not match rendered alpha "
            f"shape {rendered_alpha.shape[:2]}"
        )

    gate = "crisp" if is_crisp_frame else "blur"
    minimum = CRISP_MIN_DICE if is_crisp_frame else BLUR_MIN_DICE

    score = dice(reference_mask, rendered_alpha)
    overlap = iou(reference_mask, rendered_alpha)
    artifact = is_codec_artifact(reference_mask, rendered_alpha, tolerance_px=tolerance_px)

    if score >= minimum:
        passed = True
        reason = f"dice {score:.4f} meets the {gate} gate {minimum}"
    elif artifact:
        passed = True
        reason = (
            f"dice {score:.4f} is below the {gate} gate {minimum}, but the entire difference "
            f"lies within {tolerance_px}px of the shared boundary, which is codec/antialias "
            "noise rather than a contour defect"
        )
    else:
        passed = False
        reason = (
            f"dice {score:.4f} is below the {gate} gate {minimum} and the difference extends "
            f"beyond {tolerance_px}px of the boundary, so it alters a real structure "
            f"(lifecycle kind {lifecycle_kind!r})"
        )

    return StateVerdict(
        frame=frame,
        gate=gate,
        dice=score,
        iou=overlap,
        passed=passed,
        codec_artifact=artifact,
        reason=reason,
    )


MAX_INK_DISTANCE = 6.0  # DOCTRINE 15.3's own ink-distance band, in 8-bit channel units


def qc_ink_identity(
    contract_states: Sequence[Mapping[str, Any]],
    ratified_states: Sequence[Mapping[str, Any]],
    *,
    max_distance: float = MAX_INK_DISTANCE,
) -> Dict[str, Any]:
    """Is each state's fill still the fill the reviewer ratified?

    Failure class this guards: a re-ink pass applies a pure gain to the *reference measurement*
    (e.g. [95, 83, 59]) instead of to the ratified fill (e.g. [255, 223, 159]), so a gold word
    lands anywhere from near-black to near-white across variants. Without a gate comparing the
    delivered fill with the ratified fill this is invisible: the ink stage reports PASS, the
    legibility gate measures only separation, and the parity gate scores alpha, not colour.

    The band is DOCTRINE 15.3's own: a few levels of rounding is the same colour, a new hue is
    not. Matching is by state id, not by position, and a state with no ratified counterpart is
    UNJUDGED — which is not a pass, because an unmatched state is exactly how a renamed or
    inserted state would slip the gate.

    A deliberate polarity flip will fail here. That is the intent: DOCTRINE 15.3 sends a flip
    to a native-scale board for adjudication, so it must be visible rather than silent.
    """
    ratified_by_id = {str(state["id"]): state for state in ratified_states}
    rows: List[Dict[str, Any]] = []
    failing: List[str] = []
    unmatched: List[str] = []
    for state in contract_states:
        state_id = str(state["id"])
        ours = [int(value) for value in state["ink"]["rgb_median"]]
        counterpart = ratified_by_id.get(state_id)
        if counterpart is None:
            unmatched.append(state_id)
            rows.append(
                {
                    "state": state_id,
                    "text": state.get("text"),
                    "contract_rgb": ours,
                    "ratified_rgb": None,
                    "ink_distance": None,
                    "within_band": False,
                    "reason": (
                        f"state {state_id} has no counterpart in the ratified contract, so its "
                        "fill is unjudged"
                    ),
                }
            )
            continue
        theirs = [int(value) for value in counterpart["ink"]["rgb_median"]]
        distance = ink_distance(theirs, ours)
        within = distance <= max_distance
        if not within:
            failing.append(state_id)
        rows.append(
            {
                "state": state_id,
                "text": state.get("text"),
                "contract_rgb": ours,
                "ratified_rgb": theirs,
                "ink_distance": round(distance, 4),
                "within_band": within,
                "reason": (
                    f"ink distance {distance:.2f} is inside the {max_distance} band"
                    if within
                    else (
                        f"ink distance {distance:.2f} exceeds the {max_distance} band: "
                        f"{theirs} was ratified, {ours} is what this contract carries"
                    )
                ),
            }
        )
    return {
        "passed": not failing and not unmatched,
        "states": len(rows),
        "failing_states": failing,
        "unmatched_states": unmatched,
        "max_ink_distance": max_distance,
        "rows": rows,
        "method": (
            "euclidean distance in 8-bit RGB between each state's fill and the fill the "
            "reviewer ratified for that state, matched by state id"
        ),
        "adjudication": (
            "a fill outside the band is not automatically wrong; it is unadjudicated. A "
            "polarity flip or a new fill needs a native-scale board and a human review, "
            "which is a HUMAN gate, not this one."
        ),
        "creative_approval": "PENDING",
        "evidence_limit": EVIDENCE_LIMIT,
    }


def qc_word_identity(
    contract_states: Sequence[Mapping[str, Any]],
    authority_states: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Does the contract say, state for state, exactly what the sealed authority says?

    Failure class this guards: a render can ship nearly every caption word wrong with every
    other gate green, because no other gate compares our text with the reference's. The comparison is verbatim — case, spacing
    and punctuation included: `IM` is not `I'M`, and a stripped apostrophe is a wrong word.

    A length mismatch is its own failure rather than something a zip quietly truncates: dropped
    reference states (for example a single-word state like `I`) vanish entirely, and a
    positional walk that stops at the shorter list cannot see that.
    """
    contract_text = [str(state.get("text", "")) for state in contract_states]
    authority_text = [str(state.get("text", "")) for state in authority_states]
    mismatches: List[Dict[str, Any]] = []
    for index in range(max(len(contract_text), len(authority_text))):
        ours = contract_text[index] if index < len(contract_text) else None
        theirs = authority_text[index] if index < len(authority_text) else None
        if ours != theirs:
            mismatches.append(
                {
                    "index": index,
                    "state": (
                        contract_states[index].get("id") if index < len(contract_states) else None
                    ),
                    "contract_text": ours,
                    "authority_text": theirs,
                }
            )
    return {
        "passed": not mismatches and len(contract_text) == len(authority_text),
        "state_count": {"contract": len(contract_text), "authority": len(authority_text)},
        "mismatches": mismatches,
        "comparison": "verbatim: case, spacing and punctuation are part of the word",
        "creative_approval": "PENDING",
        "evidence_limit": EVIDENCE_LIMIT,
    }


def _crisp_frames(state: Mapping[str, Any]) -> Tuple[List[int], List[int]]:
    """(crisp frames, all frames) of one state's span, per its own blur lifecycle."""
    from .render import _lifecycle_radius

    frames = list(range(int(state["start_frame"]), int(state["end_frame_exclusive"])))
    lifecycle = state.get("lifecycle") or {"kind": "hard_state"}
    crisp = [frame for frame in frames if _lifecycle_radius(frame, lifecycle) <= 0]
    return crisp, frames


def qc_state_parity(
    contract: Mapping[str, Any],
    rendered_frames: Sequence[np.ndarray],
    plates: Mapping[str, np.ndarray],
    *,
    tolerance_px: int = 2,
) -> Dict[str, Any]:
    """Per-state Dice/IoU of the DELIVERED alpha against the reference's own traced ink.

    This is the gate that makes a wrong-word ship impossible on any render path: whatever drew
    the frames — the engine, a bespoke PIL script, a re-encode — the alpha that lands where the
    contract places a state has to be the mask that was traced off the reference. An eroded
    stroke, a substituted face, a different word: all of them move Dice, and none of them moves
    a timing gate.

    A state is judged on the last CRISP frame of its span (the word at rest). Where the whole
    span is authored soft the relaxed blur gate applies, exactly as ``qc_state`` defines it.
    """
    from .render import _scaled_plate

    rows: List[Dict[str, Any]] = []
    failing: List[str] = []
    for state in contract["states"]:
        state_id = str(state["id"])
        if state_id not in plates:
            raise QcError(f"no plate supplied for state {state_id}; parity cannot be scored without one")
        crisp, frames = _crisp_frames(state)
        frame_index = (crisp or frames)[-1]
        if frame_index >= len(rendered_frames):
            raise QcError(
                f"state {state_id} names frame {frame_index} but only {len(rendered_frames)} frames "
                "were rendered"
            )
        scaled = _scaled_plate(
            plates[state_id],
            float(state["geometry"]["scale"]),
            state["geometry"].get("named_exception"),
        )
        x0, y0 = state["placement"]["core_bbox_xyxy"][:2]
        translate = state["geometry"]["translate_xy"]
        origin_x = int(round(float(x0) + float(translate[0])))
        origin_y = int(round(float(y0) + float(translate[1])))
        height, width = scaled.shape[:2]
        frame = rendered_frames[frame_index]
        alpha = frame[:, :, 3] if frame.ndim == 3 and frame.shape[2] == 4 else frame
        window = alpha[origin_y : origin_y + height, origin_x : origin_x + width]
        if window.shape[:2] != (height, width):
            raise QcError(
                f"state {state_id}: the rendered frame is too small for the plate placed at "
                f"({origin_x}, {origin_y})"
            )
        verdict = qc_state(
            reference_mask=scaled,
            rendered_alpha=window,
            lifecycle_kind=str((state.get("lifecycle") or {}).get("kind", "hard_state")),
            is_crisp_frame=bool(crisp),
            frame=frame_index,
            tolerance_px=tolerance_px,
        )
        row = verdict.to_dict()
        row["state"] = state_id
        row["text"] = state.get("text")
        rows.append(row)
        if not verdict.passed:
            failing.append(state_id)
    return {
        "passed": not failing,
        "states": len(rows),
        "failing_states": failing,
        "rows": rows,
        "method": (
            "per state, the rendered alpha under the contract's own placement against the plate "
            "traced from the reference's pixels; Dice with the crisp/blur gates and the "
            "codec-artifact tolerance band"
        ),
        "creative_approval": "PENDING",
        "evidence_limit": EVIDENCE_LIMIT,
    }


@dataclass(frozen=True)
class TimingReport:
    passed: bool
    frame_count: int
    expected_frames: Tuple[int, ...]
    rendered_frames: Tuple[int, ...]
    blank_frames: Tuple[int, ...]
    divergent_frames: Tuple[int, ...]

    @property
    def first_divergent_frame(self):
        return self.divergent_frames[0] if self.divergent_frames else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "frame_count": self.frame_count,
            "first_divergent_frame": self.first_divergent_frame,
            "divergent_frames": list(self.divergent_frames),
            "expected_frames": list(self.expected_frames),
            "rendered_frames": list(self.rendered_frames),
            "blank_frames": list(self.blank_frames),
            "creative_approval": "PENDING",
            "evidence_limit": EVIDENCE_LIMIT,
        }


def qc_timing(
    rendered_frames: Sequence[np.ndarray],
    states: Sequence[Mapping[str, Any]],
    *,
    frame_count: int,
) -> TimingReport:
    """Check that rendered ink lands on exactly the frames the contract declares.

    Divergences are listed in frame order and the first one is surfaced, because reporting a
    convenient midpoint hides endpoint failures — the ``-shortest`` hazard drops the final
    frame, and an entry-blur frame can expose a premature line before the next crisp frame
    (NFS section 5, CFR).
    """
    if len(rendered_frames) != frame_count:
        raise QcError(
            f"rendered sequence has {len(rendered_frames)} frames but the contract declares "
            f"frame_count {frame_count}"
        )

    expected = set()
    for state in states:
        start = int(state["start_frame"])
        end = int(state["end_frame_exclusive"])
        if end > frame_count:
            raise QcError(
                f"state {state.get('id')} ends at {end}, past frame_count {frame_count}"
            )
        expected.update(range(start, end))

    rendered: List[int] = []
    for index, frame in enumerate(rendered_frames):
        alpha = frame[:, :, 3] if frame.ndim == 3 and frame.shape[2] == 4 else frame
        if int((alpha > 0).sum()) > 0:
            rendered.append(index)

    rendered_set = set(rendered)
    divergent = tuple(
        sorted(
            (expected - rendered_set) | (rendered_set - expected)
        )
    )

    return TimingReport(
        passed=not divergent,
        frame_count=frame_count,
        expected_frames=tuple(sorted(expected)),
        rendered_frames=tuple(rendered),
        blank_frames=tuple(index for index in range(frame_count) if index not in expected),
        divergent_frames=divergent,
    )
