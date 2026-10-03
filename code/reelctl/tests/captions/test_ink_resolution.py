"""The ink-resolution stage: the caption engine's legibility adjudication.

Why this exists
---------------
DOCTRINE 18 says the factory reaches the caption engine through one entry point and that no
new per-reel caption script is permitted; where a reel needs behaviour the engine lacks, "the
engine gains it behind a failing test first". Ink resolution was exactly such a gap:
approved caption passes decided their fills with per-reel *work scripts* rather than with the
engine, so a variant that changes the picture had no engine stage to re-run.

This module's job is that stage, generic over any reel: measured reference ink in, a fill that
carries that ink over *this* picture out, decided per lockup by the same rules the approved
passes recorded:

* the target is 80% of the reference's own ring Michelson;
* the move is pure gain — hue held, only luminance moves — and the SMALLEST move reaching it;
* a flip is reachable only when the reference's own polarity cannot get within 90% of the
  target, and a flip must additionally beat the best in-polarity fill by 0.15 Michelson;
* nothing may sit closer than 25 luma levels to its own background ring (DOCTRINE 15.3's
  legibility floor as the approved pass applied it);
* the decision is made once per lockup over the union of the group's own footprints, because
  deciding per state put `sample` near-black on one frame and white on the next.

The real-data test at the bottom is the one that matters: the stage must reproduce the approved
v002 pass's own automatic resolution, layer for layer, from that pass's own inputs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from reelctl.captions import ink as ink_module

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------------------
# synthetic fixtures: a canvas, a plate, and two pictures behind it


CANVAS = (120, 200)  # h, w
BOX_XY = (60, 40)
PLATE_WH = (60, 30)  # w, h


def _plate(tmp_path: Path, name: str = "L01.png") -> Path:
    """A solid block plate: core is the whole block, ring is the halo around it."""
    import cv2

    plates = tmp_path / "plates"
    plates.mkdir(exist_ok=True)
    alpha = np.zeros((PLATE_WH[1], PLATE_WH[0]), dtype=np.uint8)
    alpha[6:-6, 6:-6] = 255
    cv2.imwrite(str(plates / name), alpha)
    return plates


def _frames(tmp_path: Path, name: str, count: int, *, ink_luma: int, bg_luma: int) -> Path:
    """A picture whose caption box carries `ink_luma` and whose surround carries `bg_luma`.

    The reference frames need ink *in the picture* (that is what the reference's own Michelson
    is measured from). A candidate picture is uniform: it carries no caption yet.
    """
    import cv2

    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    for index in range(count):
        frame = np.full((CANVAS[0], CANVAS[1], 3), bg_luma, dtype=np.uint8)
        if ink_luma is not None:
            y, x = BOX_XY[1], BOX_XY[0]
            frame[y + 6 : y + PLATE_WH[1] - 6, x + 6 : x + PLATE_WH[0] - 6] = ink_luma
        cv2.imwrite(str(directory / f"r{index:03d}.png"), frame)
    return directory


def _spec(measured_rgb, *, layers=("L01",), states=None, spans=None) -> dict:
    states = states or {"L01": ["S01"]}
    spans = spans or {"L01": [0, 8]}
    return {
        "schema_version": 1,
        "artifact_type": "caption_ink_lockups",
        "lockups": [
            {
                "id": "G01",
                "measured_rgb": list(measured_rgb),
                "members": [
                    {
                        "layer": layer,
                        "plate": f"{layer}.png",
                        "box_xy": list(BOX_XY),
                        "span": spans[layer],
                        "states": states[layer],
                    }
                    for layer in layers
                ],
            }
        ],
    }


def _resolve(tmp_path, spec, reference, candidate):
    return ink_module.resolve_ink(
        spec,
        plates_dir=tmp_path / "plates",
        reference_frames=reference,
        candidate_frames=candidate,
    )


# ---------------------------------------------------------------------------------------
# the rules


def test_a_fill_that_already_clears_the_target_is_left_alone(tmp_path) -> None:
    """The smallest move that reaches the target is, when the target is already met, none."""
    plates = _plate(tmp_path)
    assert plates.is_dir()
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=20)
    out = _resolve(tmp_path, _spec((250, 250, 250)), reference, candidate)
    assert out["lockups"]["G01"]["disposition"] == "measured"
    assert out["states"]["S01"]["rgb"] == [250, 250, 250]


def test_a_short_fill_is_lifted_by_the_smallest_gain_that_reaches_the_target(tmp_path) -> None:
    # a reference beat of modest contrast, and a candidate background our measured fill sits
    # too close to but can still be lifted clear of without leaving the reference's polarity
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=130)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=100)
    out = _resolve(tmp_path, _spec((130, 130, 130)), reference, candidate)
    row = out["lockups"]["G01"]
    assert row["disposition"] == "lifted"
    # pure gain: the hue is held, so the channels stay equal and the move is upward
    rgb = out["states"]["S01"]["rgb"]
    assert rgb[0] == rgb[1] == rgb[2]
    assert rgb[0] > 130
    # and it is the SMALLEST such move: one step less would not have reached the target
    assert row["michelson_after"] >= row["target_michelson"]
    assert row["michelson_after"] < row["target_michelson"] + 0.02


def test_the_move_is_pure_gain_so_hue_is_held(tmp_path) -> None:
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=240, bg_luma=10)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=90)
    out = _resolve(tmp_path, _spec((120, 60, 30)), reference, candidate)
    rgb = out["states"]["S01"]["rgb"]
    # gain scales all three channels by one factor: the ratios survive
    assert rgb[0] / rgb[1] == pytest.approx(120 / 60, rel=0.05)
    assert rgb[1] / rgb[2] == pytest.approx(60 / 30, rel=0.05)


def test_light_ink_over_a_pale_field_flips_when_its_own_polarity_cannot_reach(tmp_path) -> None:
    """White ink, light background: no in-polarity fill can carry it."""
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=252, bg_luma=8)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=205)
    out = _resolve(tmp_path, _spec((251, 250, 250)), reference, candidate)
    row = out["lockups"]["G01"]
    assert row["disposition"] == "POLARITY FLIP"
    assert ink_module.luma(out["states"]["S01"]["rgb"]) < 205


def test_no_flip_when_the_reference_polarity_gets_within_ninety_percent(tmp_path) -> None:
    """A lift that lands close enough has done its job; the flip is not reachable."""
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=200, bg_luma=120)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=140)
    out = _resolve(tmp_path, _spec((170, 170, 170)), reference, candidate)
    assert out["lockups"]["G01"]["disposition"] != "POLARITY FLIP"


def test_a_lockup_is_decided_once_and_applied_to_every_member(tmp_path) -> None:
    """Deciding per state put `sample` near-black on one frame and white on the next."""
    import cv2

    plates = _plate(tmp_path, "L01.png")
    alpha = np.zeros((PLATE_WH[1], PLATE_WH[0]), dtype=np.uint8)
    alpha[6:-6, 6:-6] = 255
    cv2.imwrite(str(plates / "L02.png"), alpha)
    reference = _frames(tmp_path, "ref", 12, ink_luma=252, bg_luma=8)
    candidate = _frames(tmp_path, "cand", 12, ink_luma=None, bg_luma=205)
    spec = _spec(
        (251, 250, 250),
        layers=("L01", "L02"),
        states={"L01": ["S01"], "L02": ["S02"]},
        spans={"L01": [0, 4], "L02": [5, 12]},
    )
    out = _resolve(tmp_path, spec, reference, candidate)
    assert out["states"]["S01"]["rgb"] == out["states"]["S02"]["rgb"]
    assert out["states"]["S01"]["lockup"] == out["states"]["S02"]["lockup"] == "G01"


def test_the_separation_floor_is_never_crossed_by_a_resolved_fill(tmp_path) -> None:
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=252, bg_luma=8)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=205)
    out = _resolve(tmp_path, _spec((251, 250, 250)), reference, candidate)
    row = out["lockups"]["G01"]
    assert row["separation_after"] >= ink_module.MIN_SEPARATION


def test_michelson_is_not_maximised_by_driving_ink_to_black(tmp_path) -> None:
    """Maximising Michelson turns white words black on dark footage; the stage must not."""
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=30)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=30)
    out = _resolve(tmp_path, _spec((250, 250, 250)), reference, candidate)
    assert out["states"]["S01"]["rgb"] == [250, 250, 250]


# ---------------------------------------------------------------------------------------
# applying the resolution to a contract


def _contract() -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "caption_contract",
        "status": "EXTRACTED",
        "render_allowed": False,
        "reference": {
            "path": "reference/ref.mp4",
            "sha256": "c" * 64,
            "frame_count": 12,
            "fps": "24/1",
            "width": 200,
            "height": 120,
            "sample_aspect_ratio": "1:1",
        },
        "styles": {"T01": {"render_path": "source_contour"}},
        "states": [
            {
                "id": "S01",
                "start_frame": 2,
                "end_frame_exclusive": 6,
                "frames": 4,
                "text": "word",
                "lines": ["word"],
                "style_id": "T01",
                "placement": {
                    "core_bbox_xyxy": [60, 40, 100, 60],
                    "treatment_bbox_xyxy": [60, 40, 100, 60],
                    "anchor": "optical_center",
                },
                "geometry": {"scale": 1.0, "translate_xy": [0.0, 0.0]},
                "ink": {
                    "source": "sampled_reference_pixels",
                    "sample": {
                        "method": "min_channel_threshold",
                        "frames": [3],
                        "roi_xyxy": [60, 40, 100, 60],
                    },
                    "rgb_median": [247, 249, 251],
                },
                "lifecycle": {"kind": "hard_state"},
                "stacking": {"z": 0, "persists": False},
                "evidence": {
                    "tier": "MASK_VERIFIED",
                    "mask_path": "evidence/S01.png",
                    "mask_sha256": "d" * 64,
                },
            }
        ],
        "font_hypotheses": [],
    }


def test_apply_resolution_moves_the_ink_and_nothing_else() -> None:
    contract = _contract()
    patched, diff = ink_module.apply_resolution(contract, {"S01": {"rgb": [10, 9, 6]}})
    assert patched["states"][0]["ink"]["rgb_median"] == [10, 9, 6]
    assert diff == {"S01": {"from": [247, 249, 251], "to": [10, 9, 6]}}
    # text, span, geometry, placement, plate binding: untouched
    before, after = contract["states"][0], patched["states"][0]
    for key in ("text", "lines", "start_frame", "end_frame_exclusive", "frames",
                "placement", "geometry", "style_id", "lifecycle", "stacking", "evidence"):
        assert before[key] == after[key]
    assert before["ink"]["sample"] == after["ink"]["sample"]
    # the source contract is not mutated
    assert contract["states"][0]["ink"]["rgb_median"] == [247, 249, 251]


def test_apply_resolution_refuses_a_state_it_does_not_know() -> None:
    with pytest.raises(ink_module.InkError):
        ink_module.apply_resolution(_contract(), {"NOPE": {"rgb": [1, 2, 3]}})


def test_render_twin_reverses_every_ink_triple_and_nothing_else() -> None:
    contract = _contract()
    twin = ink_module.render_twin(contract)
    assert twin["states"][0]["ink"]["rgb_median"] == [251, 249, 247]
    assert twin["states"][0]["text"] == contract["states"][0]["text"]
    assert ink_module.render_twin(twin)["states"][0]["ink"]["rgb_median"] == [247, 249, 251]


def test_prove_only_ink_moved_catches_a_smuggled_edit() -> None:
    contract = _contract()
    patched, _ = ink_module.apply_resolution(contract, {"S01": {"rgb": [10, 9, 6]}})
    patched["states"][0]["geometry"]["scale"] = 1.5
    with pytest.raises(ink_module.InkError):
        ink_module.prove_only_ink_moved(contract, patched)


def test_prove_only_ink_moved_passes_a_clean_ink_only_patch() -> None:
    contract = _contract()
    patched, _ = ink_module.apply_resolution(contract, {"S01": {"rgb": [10, 9, 6]}})
    moved = ink_module.prove_only_ink_moved(contract, patched)
    assert moved == {"S01": {"from": [247, 249, 251], "to": [10, 9, 6]}}


# ---------------------------------------------------------------------------------------
# the ring, and the real-data reproduction


def test_a_fill_clearing_a_low_reference_contrast_can_still_sit_on_its_background(tmp_path) -> None:
    """The hole in the approved rule, made visible.

    The reference beat is itself low contrast, so the 80%-of-reference target is easy to clear —
    and a fill can clear it while sitting far inside the 25-level legibility floor. A fill can
    ship at a separation of ~15 this way.
    """
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=120, bg_luma=100)  # refM ~= 0.09
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=60)
    out = _resolve(tmp_path, _spec((70, 70, 70)), reference, candidate)
    row = out["lockups"]["G01"]
    assert row["disposition"] == "measured"
    assert row["michelson_after"] >= row["target_michelson"]
    assert row["separation_after"] < ink_module.MIN_SEPARATION  # the hole


def test_enforcing_the_floor_moves_that_fill_clear_by_the_smallest_gain(tmp_path) -> None:
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=120, bg_luma=100)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=60)
    out = ink_module.resolve_ink(
        _spec((70, 70, 70)),
        plates_dir=tmp_path / "plates",
        reference_frames=reference,
        candidate_frames=candidate,
        strict=True,
    )
    row = out["lockups"]["G01"]
    assert row["separation_after"] >= ink_module.MIN_SEPARATION
    # 70 on 60 is inside the floor AND under 3:1, so both of strict mode's entry conditions
    # bite (the separation floor is the amendment this test was written for, and it is still
    # what names the disposition).
    assert row["entered_on"] == "separation and contrast ratio"
    assert row["disposition"] == "moved clear of the separation floor"
    # still pure gain in the reference's own polarity: brighter, hue held
    rgb = out["states"]["S01"]["rgb"]
    assert rgb[0] == rgb[1] == rgb[2] and rgb[0] > 70


def test_the_floor_flag_changes_nothing_where_the_measured_fill_already_clears_it(tmp_path) -> None:
    """Off or on, a fill already 200 levels clear of its background does not move."""
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=20)
    common = dict(
        plates_dir=tmp_path / "plates", reference_frames=reference, candidate_frames=candidate
    )
    off = ink_module.resolve_ink(_spec((250, 250, 250)), **common)
    on = ink_module.resolve_ink(_spec((250, 250, 250)), strict=True, **common)
    assert off["lockups"]["G01"]["rgb"] == on["lockups"]["G01"]["rgb"] == [250, 250, 250]


def test_strict_refuses_a_flip_that_wins_the_ratio_but_loses_the_difference(tmp_path) -> None:
    """A2. A near-black flip scoring 0.71 at 29 levels of separation,
    against an in-polarity white scoring 0.44 at 220. Michelson prefers the flip; a reader does
    not. Strict mode makes the flip win on both or not at all."""
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=24)  # refM ~= 0.82
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=98)

    lax = _resolve(tmp_path, _spec((250, 252, 251)), reference, candidate)
    assert lax["lockups"]["G01"]["disposition"] == "POLARITY FLIP"
    assert ink_module.luma(lax["lockups"]["G01"]["rgb"]) < 98  # near-black on a mid-dark field

    strict = ink_module.resolve_ink(
        _spec((250, 252, 251)),
        plates_dir=tmp_path / "plates",
        reference_frames=reference,
        candidate_frames=candidate,
        strict=True,
    )
    row = strict["lockups"]["G01"]
    assert row["disposition"] != "POLARITY FLIP"
    assert row["separation_after"] > lax["lockups"]["G01"]["separation_after"]


def test_strict_answers_for_every_member_of_a_lockup_not_their_median(tmp_path) -> None:
    """A3. `left` on a dark field and `right` on a bright one: the union median answers neither."""
    import cv2

    plates = _plate(tmp_path, "L01.png")
    alpha = np.zeros((PLATE_WH[1], PLATE_WH[0]), dtype=np.uint8)
    alpha[6:-6, 6:-6] = 255
    cv2.imwrite(str(plates / "L02.png"), alpha)

    reference = _frames(tmp_path, "ref", 12, ink_luma=200, bg_luma=90)
    # one picture, two very different backgrounds under the two members of the lockup
    candidate = tmp_path / "split"
    candidate.mkdir()
    for index in range(12):
        frame = np.full((CANVAS[0], CANVAS[1], 3), 20 if index < 6 else 210, dtype=np.uint8)
        cv2.imwrite(str(candidate / f"r{index:03d}.png"), frame)

    spec = _spec(
        (120, 120, 120),
        layers=("L01", "L02"),
        states={"L01": ["S01"], "L02": ["S02"]},
        spans={"L01": [0, 6], "L02": [6, 12]},
    )
    common = dict(plates_dir=tmp_path / "plates", reference_frames=reference, candidate_frames=candidate)
    lax = ink_module.resolve_ink(spec, **common)
    strict = ink_module.resolve_ink(spec, strict=True, **common)

    # the union median sits between 20 and 210, so the lax answer can look fine while being
    # measured against a background neither member actually has
    assert len(lax["lockups"]["G01"]["per_picture"]) == 1
    per_member = {row["picture"] for row in strict["lockups"]["G01"]["per_picture"]}
    assert per_member == {"candidate:L01", "candidate:L02"}
    for row in strict["lockups"]["G01"]["per_picture"]:
        assert row["background_luma"] in (20.0, 210.0)


def test_strict_optimises_the_composite_not_the_fill_on_paper(tmp_path) -> None:
    """A4. A plate is not opaque, so the fill is not what lands on screen.

    At 60% alpha over a dark room, a fill 26 levels clear of its surround delivers 15. The
    approved passes assumed opacity; strict mode measures the blend, which is the same number
    the delivery QC reports.
    """
    import cv2

    plates = tmp_path / "plates"
    plates.mkdir()
    alpha = np.zeros((PLATE_WH[1], PLATE_WH[0]), dtype=np.uint8)
    alpha[6:-6, 6:-6] = 153  # 60% — a real plate's soft strokes, not a hard mask
    cv2.imwrite(str(plates / "L01.png"), alpha)

    reference = _frames(tmp_path, "ref", 8, ink_luma=60, bg_luma=140)  # dark ink, light field
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=40)

    spec = _spec((70, 70, 70))
    common = dict(plates_dir=plates, reference_frames=reference, candidate_frames=candidate)
    lax = ink_module.resolve_ink(spec, **common)
    strict = ink_module.resolve_ink(spec, strict=True, **common)

    surface = strict["lockups"]["G01"]["per_picture"][0]
    assert surface["plate_alpha"] == pytest.approx(0.6, abs=0.01)
    # the fill on paper is further from the surround than what actually lands
    assert abs(surface["delivered_ink_luma"] - ink_module.luma(strict["lockups"]["G01"]["rgb"])) > 1
    assert surface["separation_after"] >= ink_module.MIN_SEPARATION
    # and the lax answer, judged on the composite it will actually deliver, does not clear
    lax_rgb = ink_module.luma(lax["lockups"]["G01"]["rgb"])
    delivered_lax = 0.6 * lax_rgb + 0.4 * surface["picture_under_ink_luma"]
    assert abs(delivered_lax - surface["background_luma"]) < ink_module.MIN_SEPARATION


def test_one_fill_must_carry_every_delivered_picture(tmp_path) -> None:
    """A reel delivered in two geometries has two pictures behind the same word.

    The 16:9 master and the 9:16 reframe are different crops of different sources, so the
    background under a caption is not the same in both — and the contract carries one ink value.
    The rule therefore sees the worst of them, and the fill it picks clears the floor on both.
    """
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=252, bg_luma=100)
    easy = _frames(tmp_path, "easy", 8, ink_luma=None, bg_luma=20)  # white reads easily here
    hard = _frames(tmp_path, "hard", 8, ink_luma=None, bg_luma=140)  # and only just here

    spec = _spec((251, 250, 250))
    single = ink_module.resolve_ink(
        spec, plates_dir=tmp_path / "plates", reference_frames=reference, candidate_frames=easy
    )
    both = ink_module.resolve_ink(
        spec,
        plates_dir=tmp_path / "plates",
        reference_frames=reference,
        candidate_frames=easy,
        candidate_pictures=[
            {"name": "refgeom", "frames": easy},
            {"name": "vertical", "frames": hard},
        ],
    )
    assert single["lockups"]["G01"]["rgb"] == [251, 250, 250]  # the easy picture alone: no move
    row = both["lockups"]["G01"]
    assert row["rgb"] != [251, 250, 250]
    assert row["separation_after"] >= ink_module.MIN_SEPARATION
    per_picture = {entry["picture"]: entry for entry in row["per_picture"]}
    assert set(per_picture) == {"refgeom", "vertical"}
    for entry in per_picture.values():
        assert entry["separation_after"] >= ink_module.MIN_SEPARATION
    # and the worst case is what the headline number reports
    assert row["separation_after"] == min(e["separation_after"] for e in per_picture.values())


def test_a_picture_no_fill_can_carry_is_reported_not_smoothed_over(tmp_path) -> None:
    """When the two delivered pictures straddle the palette, no ink is the honest answer.

    This is the signal the caster needs: the slot has to change, because the fill cannot.
    """
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=252, bg_luma=8)
    dark = _frames(tmp_path, "dark", 8, ink_luma=None, bg_luma=20)
    bright = _frames(tmp_path, "bright", 8, ink_luma=None, bg_luma=245)
    out = ink_module.resolve_ink(
        _spec((251, 250, 250)),
        plates_dir=tmp_path / "plates",
        reference_frames=reference,
        candidate_frames=dark,
        candidate_pictures=[
            {"name": "refgeom", "frames": dark},
            {"name": "vertical", "frames": bright},
        ],
    )
    row = out["lockups"]["G01"]
    assert row["disposition"] == "NO PALETTE-LEGAL FILL"
    assert row["clears_separation_floor"] is False


def test_the_caption_layer_transform_is_applied_per_picture(tmp_path) -> None:
    """The 9:16 layer is fitted by width and centred; the ring must be measured where it lands."""
    import cv2

    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=252, bg_luma=8)
    refgeom = _frames(tmp_path, "refgeom", 8, ink_luma=None, bg_luma=20)

    # a taller canvas that is bright ONLY where the scaled caption layer lands
    scale, offset = 0.5, (0, 40)
    tall = tmp_path / "tall"
    tall.mkdir()
    box_x = round(BOX_XY[0] * scale) + offset[0]
    box_y = round(BOX_XY[1] * scale) + offset[1]
    for index in range(8):
        frame = np.full((CANVAS[0] * 2, CANVAS[1], 3), 10, dtype=np.uint8)
        frame[box_y - 6 : box_y + round(PLATE_WH[1] * scale) + 6,
              box_x - 6 : box_x + round(PLATE_WH[0] * scale) + 6] = 245
        cv2.imwrite(str(tall / f"r{index:03d}.png"), frame)

    out = ink_module.resolve_ink(
        _spec((251, 250, 250)),
        plates_dir=tmp_path / "plates",
        reference_frames=reference,
        candidate_frames=refgeom,
        candidate_pictures=[
            {"name": "refgeom", "frames": refgeom},
            {"name": "vertical", "frames": tall, "scale": scale, "offset_xy": list(offset)},
        ],
    )
    per_picture = {entry["picture"]: entry for entry in out["lockups"]["G01"]["per_picture"]}
    # the transform found the bright patch: untransformed it would have measured the dark field
    assert per_picture["vertical"]["background_luma"] > 200
    assert per_picture["refgeom"]["background_luma"] < 40


def test_a_one_based_frame_directory_is_read_through_the_declared_origin(tmp_path) -> None:
    """DOCTRINE 12.4: the frame namespace is declared, never inferred."""
    import cv2


    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)

    # a candidate picture that actually changes frame to frame, so a one-frame shift is
    # visible in the measurement instead of hiding inside a flat field
    zero_based = tmp_path / "cand0"
    one_based = tmp_path / "cand1"
    zero_based.mkdir()
    one_based.mkdir()
    for index in range(8):
        frame = np.full((CANVAS[0], CANVAS[1], 3), 150 + index * 8, dtype=np.uint8)
        cv2.imwrite(str(zero_based / f"r{index:03d}.png"), frame)
        cv2.imwrite(str(one_based / f"f{index + 1:03d}.png"), frame)

    spec = _spec((250, 250, 250))
    zero = _resolve(tmp_path, spec, reference, zero_based)
    one = ink_module.resolve_ink(
        spec,
        plates_dir=tmp_path / "plates",
        reference_frames=reference,
        candidate_frames=one_based,
        candidate_pattern="f{index:03d}.png",
        candidate_index_origin=1,
    )
    assert one["lockups"]["G01"]["rgb"] == zero["lockups"]["G01"]["rgb"]
    assert one["lockups"]["G01"]["candidate_background_luma"] == zero["lockups"]["G01"]["candidate_background_luma"]

    # reading the same directory under the wrong origin measures a different picture: this is
    # the silent one-frame shift DOCTRINE 12.4 exists to prevent, shown rather than asserted
    shifted = ink_module.resolve_ink(
        spec, plates_dir=tmp_path / "plates", reference_frames=reference,
        candidate_frames=one_based, candidate_pattern="f{index:03d}.png",
    )
    assert (
        shifted["lockups"]["G01"]["candidate_background_luma"]
        != one["lockups"]["G01"]["candidate_background_luma"]
    )


def test_the_ring_is_an_annulus_between_the_two_dilations() -> None:
    alpha = np.zeros((60, 60), dtype=np.uint8)
    alpha[25:35, 25:35] = 255
    core, ring = ink_module.plate_masks(alpha)
    assert core[27, 27]
    assert not ring[27, 27]  # the ring never overlaps the glyph
    assert ring[25 - 8, 30]  # outside the 7px inner dilation, inside the 17px outer one
    assert not ring[25 - 12, 30]  # beyond the outer dilation
    assert not (core & ring).any()


# Optional real-corpus check. Point these at an approved caption pass (V001: plates/ + refframes/)
# and its re-run (V002: frames-v002/ + work/contrast-v002-auto.json), plus the lockup spec that
# pass used. Skipped when absent; the packaged spec is synthetic and cannot reproduce a real pass.
V001 = Path(os.path.expanduser(os.environ.get("REEL_FACTORY_CAPTION_PASS_V001", "/nonexistent")))
V002 = Path(os.path.expanduser(os.environ.get("REEL_FACTORY_CAPTION_PASS_V002", "/nonexistent")))
REAL_SPEC = os.environ.get("REEL_FACTORY_INK_LOCKUPS", "")
HAVE_REAL = (
    (V002 / "work" / "contrast-v002-auto.json").is_file()
    and (V001 / "refframes").is_dir()
    and Path(REAL_SPEC).is_file()
)


@pytest.mark.skipif(not HAVE_REAL, reason="no approved caption pass configured (REEL_FACTORY_CAPTION_PASS_V001/V002)")
def test_reproduces_the_approved_v002_automatic_resolution() -> None:
    """The gate that makes the rest of this module trustworthy.

    The approved v002 caption pass ran its own adjudication and left the machine half of it on
    disk as `work/contrast-v002-auto.json`. Running THIS engine stage against that pass's own
    inputs — its reference frames, its plates, its boxes, its measured fills, its picture — must
    reproduce it layer for layer: same fill, same disposition, same measured background. If it
    does not, the stage is not the approved rule and no variant may be resolved with it.
    """
    expected = json.loads((V002 / "work" / "contrast-v002-auto.json").read_text())
    spec = json.loads(Path(REAL_SPEC).read_text())
    out = ink_module.resolve_ink(
        spec,
        plates_dir=V001 / "plates",
        reference_frames=V001 / "refframes",
        candidate_frames=V002 / "frames-v002",
    )
    mismatches = []
    for lockup in spec["lockups"]:
        first_layer = lockup["members"][0]["layer"]
        want = expected[first_layer]
        got = out["lockups"][lockup["id"]]
        if got["rgb"] != want["fill_auto_v002"]:
            mismatches.append((first_layer, "fill", got["rgb"], want["fill_auto_v002"]))
        if got["disposition"] != want["disposition_auto"]:
            mismatches.append((first_layer, "disposition", got["disposition"], want["disposition_auto"]))
        if round(got["candidate_background_luma"], 1) != want["our_bg_L"]:
            mismatches.append((first_layer, "our_bg_L", got["candidate_background_luma"], want["our_bg_L"]))
        if round(got["reference_michelson"], 3) != want["ref_michelson"]:
            mismatches.append((first_layer, "refM", got["reference_michelson"], want["ref_michelson"]))
    assert not mismatches, mismatches


@pytest.mark.skipif(not HAVE_REAL, reason="no approved caption pass configured (REEL_FACTORY_CAPTION_PASS_V001/V002)")
def test_the_stage_is_red_capable_on_the_real_fixture() -> None:
    """A green reproduction is worthless until the comparison is shown able to fail (16.1).

    Same inputs, one deliberate corruption: the candidate picture is swapped for the reference's
    own frames, which is a different picture behind every word. At least one lockup must move.
    """
    spec = json.loads(Path(REAL_SPEC).read_text())
    honest = ink_module.resolve_ink(
        spec, plates_dir=V001 / "plates",
        reference_frames=V001 / "refframes", candidate_frames=V002 / "frames-v002",
    )
    corrupted = ink_module.resolve_ink(
        spec, plates_dir=V001 / "plates",
        reference_frames=V001 / "refframes", candidate_frames=V001 / "refframes",
    )
    moved = [
        key for key in honest["lockups"]
        if honest["lockups"][key]["rgb"] != corrupted["lockups"][key]["rgb"]
    ]
    assert moved, "the stage returned the same answer for two different pictures"
