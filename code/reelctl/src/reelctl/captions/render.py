"""The RENDER stage: caption contract -> full-canvas RGBA plate sequence.

The whole sequence is compiled before anything is composited, so a missing plate or an
out-of-canvas placement is a lock failure rather than a surprise at composite time.

Two paths (``DOCTRINE.md`` section 6):

* ``source_contour`` — the 1:1 default. Alpha comes from a plate traced from the reference's
  own pixels; the renderer only places and uniformly scales it. RGB is the contract's
  sampled ink, applied flat through the alpha, so no footage colour is ever baked in.
* ``native_font`` — permitted only for a style carrying a ``HOLDOUT_PROVEN`` or
  ``ORIGINAL_ASSET_PROVEN`` face.

There is exactly one scale number per state. Per-word anisotropic resize — the mechanism
that makes one font read as several — is unrepresentable rather than merely discouraged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import cv2
import numpy as np

from .contract import CaptionContractError, validate_caption_contract

__all__ = ["RenderError", "render_caption_sequence"]


class RenderError(ValueError):
    pass


def _blur_radius(frame: int, window: Optional[Sequence[int]], reverse: bool = False) -> float:
    """Progressive blur across a half-open window, strongest at the far edge.

    Entry windows resolve toward crisp; exit windows depart from crisp, so the ramp is
    reversed.
    """
    if not window:
        return 0.0
    start, end = int(window[0]), int(window[1])
    if end <= start or not (start <= frame < end):
        return 0.0
    span = end - start
    position = (frame - start) / span
    strength = position if reverse else (1.0 - position)
    # Keep the ramp inside a range that softens edges without dissolving the glyph.
    return round(0.5 + 3.5 * strength, 4)


def _soften(alpha: np.ndarray, radius: float) -> Tuple[np.ndarray, int]:
    """Blur a plate, padding first so the blur has somewhere to spread.

    Returns the softened plate and the pad applied, because a blurred glyph physically
    occupies more space than its crisp core — which is the same reason the contract carries a
    treatment box larger than the core ink box. Without the pad, blurring a plate that is
    solid to its own edges is a no-op: OpenCV reflects the border, so a uniformly opaque
    array stays uniformly opaque.
    """
    if radius <= 0:
        return alpha, 0
    pad = max(1, int(np.ceil(radius * 3)))
    padded = cv2.copyMakeBorder(
        alpha, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0
    )
    ksize = int(radius * 2) * 2 + 1
    return cv2.GaussianBlur(padded, (ksize, ksize), radius), pad


def _scaled_plate(
    plate: np.ndarray,
    scale: float,
    named_exception: Optional[Mapping[str, Any]] = None,
) -> np.ndarray:
    """Uniform scale, plus one evidence-backed axis scale when the contract names it.

    The named exception is the only route to anisotropy, and ``contract.py`` has already
    verified it carries multi-frame evidence, both retained boxes, a bounded axis scale and a
    justification that it reproduces the reference's geometry (rule 4.3). Reaching it here
    means that check passed.
    """
    scale_x = scale_y = float(scale)
    if named_exception is not None:
        axis_scale = float(named_exception["axis_scale"])
        if named_exception["axis"] == "y":
            scale_y *= axis_scale
        else:
            scale_x *= axis_scale

    if scale_x == 1.0 and scale_y == 1.0:
        return plate
    height, width = plate.shape[:2]
    new_width = max(1, int(round(width * scale_x)))
    new_height = max(1, int(round(height * scale_y)))
    shrinking = new_width < width or new_height < height
    interpolation = cv2.INTER_AREA if shrinking else cv2.INTER_CUBIC
    return cv2.resize(plate, (new_width, new_height), interpolation=interpolation)


def _lifecycle_radius(frame: int, lifecycle: Mapping[str, Any]) -> float:
    entry = _blur_radius(frame, lifecycle.get("readable_blur"))
    exit_blur = _blur_radius(frame, lifecycle.get("exit_progressive_blur"), reverse=True)
    return max(entry, exit_blur)


def render_caption_sequence(
    contract: Mapping[str, Any],
    *,
    plates: Mapping[str, np.ndarray],
    allow_unsealed_local_review: bool = False,
) -> List[np.ndarray]:
    """Compile a caption contract into one full-canvas RGBA frame per reference frame.

    ``plates`` maps state id to a single-channel alpha plate traced from the reference. The
    renderer never invents a glyph shape; it places, uniformly scales and softens what the
    EXTRACT stage measured.
    """
    try:
        validate_caption_contract(contract)
    except CaptionContractError as error:
        raise RenderError(f"contract is not renderable: {error}") from error

    sealed = contract["status"] == "SEALED" and contract["render_allowed"]
    if not sealed and not allow_unsealed_local_review:
        raise RenderError(
            f"contract status is {contract['status']} with render_allowed="
            f"{contract['render_allowed']}; only a SEALED contract with render_allowed may "
            "be rendered. Pass allow_unsealed_local_review=True for a local review render "
            "(which still never authorises upload or publication)."
        )

    reference = contract["reference"]
    frame_count = int(reference["frame_count"])
    width = int(reference["width"])
    height = int(reference["height"])

    styles = contract["styles"]
    proven = {
        hypothesis["style_id"]
        for hypothesis in contract["font_hypotheses"]
        if hypothesis["identity_status"] in {"HOLDOUT_PROVEN", "ORIGINAL_ASSET_PROVEN"}
    }

    states = list(contract["states"])
    for state in states:
        style_id = state["style_id"]
        render_path = styles[style_id]["render_path"]
        if render_path == "native_font" and style_id not in proven:
            raise RenderError(
                f"state {state['id']} uses style {style_id} on the native_font path without a "
                "holdout-proven or original-asset-proven face; route it to source_contour"
            )
        if state["id"] not in plates:
            raise RenderError(
                f"no traced plate supplied for state {state['id']}; the caption sequence "
                "cannot be compiled with a missing plate"
            )

    # Composite lowest stacking slot first so a higher z lands above it.
    ordered = sorted(states, key=lambda state: (state["stacking"]["z"], state["id"]))

    canvas: List[np.ndarray] = [
        np.zeros((height, width, 4), dtype=np.uint8) for _ in range(frame_count)
    ]

    for state in ordered:
        plate = plates[state["id"]]
        if plate.ndim != 2:
            raise RenderError(
                f"plate for state {state['id']} must be a single-channel alpha array, got "
                f"shape {plate.shape}"
            )
        scaled = _scaled_plate(
            plate,
            float(state["geometry"]["scale"]),
            state["geometry"].get("named_exception"),
        )
        x0, y0, _, _ = state["placement"]["core_bbox_xyxy"]
        translate = state["geometry"]["translate_xy"]
        origin_x = int(round(x0 + float(translate[0])))
        origin_y = int(round(y0 + float(translate[1])))
        plate_h, plate_w = scaled.shape[:2]

        if (
            origin_x < 0
            or origin_y < 0
            or origin_x + plate_w > width
            or origin_y + plate_h > height
        ):
            raise RenderError(
                f"state {state['id']}: plate {plate_w}x{plate_h} at ({origin_x}, {origin_y}) "
                f"falls outside the {width}x{height} canvas; refusing to crop silently"
            )

        rgb = tuple(int(value) for value in state["ink"]["rgb_median"])
        lifecycle = state["lifecycle"]

        for frame in range(state["start_frame"], state["end_frame_exclusive"]):
            radius = _lifecycle_radius(frame, lifecycle)
            alpha, pad = _soften(scaled, radius)
            frame_x = origin_x - pad
            frame_y = origin_y - pad
            frame_h, frame_w = alpha.shape[:2]
            if (
                frame_x < 0
                or frame_y < 0
                or frame_x + frame_w > width
                or frame_y + frame_h > height
            ):
                raise RenderError(
                    f"state {state['id']}: at frame {frame} the blurred plate "
                    f"{frame_w}x{frame_h} at ({frame_x}, {frame_y}) falls outside the "
                    f"{width}x{height} canvas; a blurred glyph occupies more space than its "
                    "crisp core, so the placement must leave room for it"
                )
            target = canvas[frame]
            region = target[frame_y : frame_y + frame_h, frame_x : frame_x + frame_w]
            existing = region[:, :, 3].astype(np.float32) / 255.0
            incoming = alpha.astype(np.float32) / 255.0
            combined = incoming + existing * (1.0 - incoming)

            for channel, value in enumerate(rgb):
                previous = region[:, :, channel].astype(np.float32)
                blended = value * incoming + previous * existing * (1.0 - incoming)
                with np.errstate(invalid="ignore", divide="ignore"):
                    resolved = np.where(combined > 0, blended / np.maximum(combined, 1e-6), 0)
                region[:, :, channel] = np.clip(np.round(resolved), 0, 255).astype(np.uint8)
            region[:, :, 3] = np.clip(np.round(combined * 255.0), 0, 255).astype(np.uint8)

    return canvas


def sequence_summary(frames: Sequence[np.ndarray]) -> Dict[str, Any]:
    """Per-frame ink statistics, for receipts and QC input."""
    rows: List[Tuple[int, int, int]] = []
    for index, frame in enumerate(frames):
        alpha = frame[:, :, 3]
        rows.append((index, int((alpha > 0).sum()), int(alpha.max())))
    return {
        "frame_count": len(frames),
        "frames_with_ink": sum(1 for _, pixels, _ in rows if pixels > 0),
        "empty_frames": [index for index, pixels, _ in rows if pixels == 0],
        "per_frame": [
            {"frame": index, "ink_pixels": pixels, "alpha_max": peak}
            for index, pixels, peak in rows
        ],
    }
