"""The RENDER stage: caption contract -> full-canvas RGBA plate sequence.

Two paths (DOCTRINE.md section 6):

* ``source_contour`` — the 1:1 default. Glyph shapes come from the reference's own pixels,
  per-state uniform scaling only, ink from the contract's sampled fields.
* ``native_font`` — permitted only for a style with a holdout-proven or original-asset-proven
  face, which ``contract.py`` already enforces.

Typography layers are compiled into a complete full-canvas RGBA sequence *before* render, so
a missing frame is a lock failure rather than a surprise at composite time (reelctl README,
"Exact typography fallback").
"""

from __future__ import annotations

import numpy as np
import pytest

from reelctl.captions.render import (
    RenderError,
    render_caption_sequence,
)

FRAME_COUNT = 12
WIDTH = 200
HEIGHT = 120


def _plate(width: int = 40, height: int = 20) -> np.ndarray:
    """A solid alpha plate standing in for a traced reference contour."""
    return np.full((height, width), 255, dtype=np.uint8)


def _state(
    state_id: str = "C01",
    start: int = 2,
    end: int = 6,
    z: int = 0,
    rgb=(247, 249, 251),
    scale: float = 1.0,
    core=(60, 40, 100, 60),
    lifecycle=None,
) -> dict:
    x0, y0, x1, y1 = core
    return {
        "id": state_id,
        "start_frame": start,
        "end_frame_exclusive": end,
        "frames": end - start,
        "text": "word",
        "lines": ["word"],
        "style_id": "T01",
        "placement": {
            "core_bbox_xyxy": [x0, y0, x1, y1],
            "treatment_bbox_xyxy": [x0, y0, x1, y1],
            "anchor": "optical_center",
        },
        "geometry": {"scale": scale, "translate_xy": [0.0, 0.0]},
        "ink": {
            "source": "sampled_reference_pixels",
            "sample": {
                "method": "min_channel_threshold",
                "frames": [start],
                "roi_xyxy": [x0, y0, x1, y1],
            },
            "rgb_median": list(rgb),
        },
        "lifecycle": lifecycle or {"kind": "hard_state"},
        "stacking": {"z": z, "persists": False},
        "evidence": {
            "tier": "MASK_VERIFIED",
            "mask_path": f"evidence/{state_id}.png",
            "mask_sha256": "d" * 64,
        },
    }


def _contract(states=None, styles=None) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "caption_contract",
        "status": "SEALED",
        "render_allowed": True,
        "reference": {
            "path": "reference/ref.mp4",
            "sha256": "c" * 64,
            "frame_count": FRAME_COUNT,
            "fps": "24/1",
            "width": WIDTH,
            "height": HEIGHT,
            "sample_aspect_ratio": "1:1",
        },
        "styles": styles or {"T01": {"render_path": "source_contour"}},
        "states": states if states is not None else [_state()],
        "font_hypotheses": [],
    }


def test_renders_exactly_one_rgba_frame_per_reference_frame() -> None:
    frames = render_caption_sequence(_contract(), plates={"C01": _plate()})
    assert len(frames) == FRAME_COUNT
    assert all(frame.shape == (HEIGHT, WIDTH, 4) for frame in frames)


def test_frames_outside_a_state_span_are_fully_transparent() -> None:
    frames = render_caption_sequence(_contract(), plates={"C01": _plate()})
    for index in (0, 1, 6, 11):
        assert frames[index][:, :, 3].max() == 0, f"frame {index} should be empty"


def test_frames_inside_a_state_span_carry_alpha() -> None:
    frames = render_caption_sequence(_contract(), plates={"C01": _plate()})
    for index in (2, 3, 4, 5):
        assert frames[index][:, :, 3].max() > 0, f"frame {index} should carry ink"


def test_ink_rgb_comes_from_the_contract_not_a_constant() -> None:
    warm = render_caption_sequence(
        _contract([_state(rgb=(247, 249, 251))]), plates={"C01": _plate()}
    )[3]
    burgundy = render_caption_sequence(
        _contract([_state(rgb=(125, 13, 34))]), plates={"C01": _plate()}
    )[3]
    ys, xs = np.where(warm[:, :, 3] > 0)
    assert tuple(warm[ys[0], xs[0], :3]) == (247, 249, 251)
    ys, xs = np.where(burgundy[:, :, 3] > 0)
    assert tuple(burgundy[ys[0], xs[0], :3]) == (125, 13, 34)


def test_rgb_is_uniform_across_the_glyph_so_no_footage_is_baked_in() -> None:
    frame = render_caption_sequence(_contract(), plates={"C01": _plate()})[3]
    inked = frame[:, :, 3] > 0
    for channel in range(3):
        values = frame[:, :, channel][inked]
        assert values.min() == values.max()


def test_plate_is_placed_at_the_core_box_origin() -> None:
    frames = render_caption_sequence(
        _contract([_state(core=(60, 40, 100, 60))]), plates={"C01": _plate(40, 20)}
    )
    alpha = frames[3][:, :, 3]
    ys, xs = np.where(alpha > 0)
    assert (int(xs.min()), int(ys.min())) == (60, 40)
    assert (int(xs.max()) + 1, int(ys.max()) + 1) == (100, 60)


def test_uniform_scale_scales_both_axes_together() -> None:
    frames = render_caption_sequence(
        _contract([_state(scale=2.0, core=(20, 20, 60, 40))]),
        plates={"C01": _plate(40, 20)},
    )
    alpha = frames[3][:, :, 3]
    ys, xs = np.where(alpha > 0)
    width = int(xs.max()) - int(xs.min()) + 1
    height = int(ys.max()) - int(ys.min()) + 1
    assert width == 80
    assert height == 40


def test_higher_z_composites_above_lower_z() -> None:
    below = _state("C01", 2, 6, z=0, rgb=(10, 10, 10), core=(60, 40, 100, 60))
    above = _state("C02", 2, 6, z=1, rgb=(250, 250, 250), core=(60, 40, 100, 60))
    frames = render_caption_sequence(
        _contract([below, above]), plates={"C01": _plate(), "C02": _plate()}
    )
    frame = frames[3]
    ys, xs = np.where(frame[:, :, 3] > 0)
    assert tuple(frame[ys[0], xs[0], :3]) == (250, 250, 250)


def test_stacking_order_is_independent_of_state_declaration_order() -> None:
    below = _state("C01", 2, 6, z=0, rgb=(10, 10, 10))
    above = _state("C02", 2, 6, z=1, rgb=(250, 250, 250))
    forward = render_caption_sequence(
        _contract([below, above]), plates={"C01": _plate(), "C02": _plate()}
    )[3]
    reversed_ = render_caption_sequence(
        _contract([above, below]), plates={"C01": _plate(), "C02": _plate()}
    )[3]
    assert np.array_equal(forward, reversed_)


def test_a_missing_plate_is_a_lock_failure() -> None:
    with pytest.raises(RenderError, match="plate|C01"):
        render_caption_sequence(_contract(), plates={})


def test_unsealed_contract_is_refused_by_default() -> None:
    contract = _contract()
    contract["status"] = "EXTRACTED"
    contract["render_allowed"] = False
    with pytest.raises(RenderError, match="SEALED|render_allowed"):
        render_caption_sequence(contract, plates={"C01": _plate()})


def test_unsealed_contract_may_be_rendered_for_local_review_when_asked() -> None:
    """Local review renders are allowed; publishing is not."""
    contract = _contract()
    contract["status"] = "EXTRACTED"
    contract["render_allowed"] = False
    frames = render_caption_sequence(
        contract, plates={"C01": _plate()}, allow_unsealed_local_review=True
    )
    assert len(frames) == FRAME_COUNT


def test_native_font_style_without_proof_is_refused() -> None:
    contract = _contract(styles={"T01": {"render_path": "native_font"}})
    with pytest.raises(RenderError, match="native_font|proof|holdout"):
        render_caption_sequence(contract, plates={"C01": _plate()})


def test_blur_entry_softens_edges_before_the_crisp_frame() -> None:
    """A progressive entry must actually blur; the crisp frame must not be blurred."""
    lifecycle = {
        "kind": "blur_to_crisp",
        "first_detectable": 2,
        "readable_blur": [2, 5],
        "first_crisp": 5,
    }
    frames = render_caption_sequence(
        _contract([_state(start=2, end=8, lifecycle=lifecycle)]),
        plates={"C01": _plate()},
    )
    entry_alpha = frames[2][:, :, 3]
    crisp_alpha = frames[5][:, :, 3]
    # A blurred plate has intermediate alpha values; a crisp one is binary.
    assert ((entry_alpha > 0) & (entry_alpha < 255)).sum() > 0
    assert ((crisp_alpha > 0) & (crisp_alpha < 255)).sum() == 0


def test_exit_progressive_blur_softens_the_tail() -> None:
    lifecycle = {
        "kind": "blur_to_crisp",
        "first_detectable": 2,
        "first_crisp": 3,
        "hold": [3, 6],
        "exit_progressive_blur": [6, 9],
    }
    frames = render_caption_sequence(
        _contract([_state(start=2, end=9, lifecycle=lifecycle)]),
        plates={"C01": _plate()},
    )
    hold_alpha = frames[4][:, :, 3]
    exit_alpha = frames[7][:, :, 3]
    assert ((hold_alpha > 0) & (hold_alpha < 255)).sum() == 0
    assert ((exit_alpha > 0) & (exit_alpha < 255)).sum() > 0


def test_render_is_deterministic() -> None:
    first = render_caption_sequence(_contract(), plates={"C01": _plate()})
    second = render_caption_sequence(_contract(), plates={"C01": _plate()})
    assert all(np.array_equal(a, b) for a, b in zip(first, second))


def test_plate_larger_than_the_canvas_is_refused_rather_than_silently_cropped() -> None:
    with pytest.raises(RenderError, match="canvas|bounds|larger"):
        render_caption_sequence(
            _contract([_state(core=(180, 100, 220, 130))]),
            plates={"C01": _plate(40, 30)},
        )


def test_contract_violations_surface_as_render_errors() -> None:
    contract = _contract()
    contract["states"][0]["frames"] = 99
    with pytest.raises(RenderError, match="frames|contract"):
        render_caption_sequence(contract, plates={"C01": _plate()})


def _named_exception(start: int, end: int, axis_scale: float = 1.15) -> dict:
    return {
        "flag": "reference_height_fit",
        "axis": "y",
        "axis_scale": axis_scale,
        "evidence_frames": [start, start + 1],
        "measured_bbox_xyxy": [60, 40, 100, 60],
        "clean_native_bbox_xyxy": [60, 42, 100, 58],
        "justification": (
            "Reproduces the reference lockup's own vertical geometry at this state; it is "
            "geometry reproduction of the reference and not a font substitution."
        ),
    }


def test_named_exception_applies_its_axis_scale_on_render() -> None:
    """An approved script baseline ships scale_y 1.15; the renderer must honour it."""
    state = _state(start=2, end=6, core=(60, 40, 100, 60))
    state["geometry"]["named_exception"] = _named_exception(2, 6, axis_scale=1.15)
    frames = render_caption_sequence(_contract([state]), plates={"C01": _plate(40, 20)})
    alpha = frames[3][:, :, 3]
    ys, xs = np.where(alpha > 0)
    width = int(xs.max()) - int(xs.min()) + 1
    height = int(ys.max()) - int(ys.min()) + 1
    assert width == 40  # unchanged axis
    assert height == 23  # round(20 * 1.15)


def test_named_exception_on_x_scales_the_other_axis() -> None:
    state = _state(start=2, end=6, core=(60, 40, 100, 60))
    exception = _named_exception(2, 6, axis_scale=1.2)
    exception["axis"] = "x"
    state["geometry"]["named_exception"] = exception
    frames = render_caption_sequence(_contract([state]), plates={"C01": _plate(40, 20)})
    ys, xs = np.where(frames[3][:, :, 3] > 0)
    assert int(xs.max()) - int(xs.min()) + 1 == 48  # round(40 * 1.2)
    assert int(ys.max()) - int(ys.min()) + 1 == 20


def test_named_exception_composes_with_the_uniform_scale() -> None:
    state = _state(start=2, end=6, core=(20, 20, 60, 40), scale=2.0)
    state["geometry"]["named_exception"] = _named_exception(2, 6, axis_scale=1.15)
    state["geometry"]["named_exception"]["measured_bbox_xyxy"] = [20, 20, 60, 40]
    state["geometry"]["named_exception"]["clean_native_bbox_xyxy"] = [20, 22, 60, 38]
    frames = render_caption_sequence(_contract([state]), plates={"C01": _plate(40, 20)})
    ys, xs = np.where(frames[3][:, :, 3] > 0)
    assert int(xs.max()) - int(xs.min()) + 1 == 80  # 40 * 2.0
    assert int(ys.max()) - int(ys.min()) + 1 == 46  # round(20 * 2.0 * 1.15)
