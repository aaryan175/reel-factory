"""The ink stage's contrast units, its local measurement, and its ability to say no.

Why this module exists
----------------------
An ink stage that is not built carefully can ship a caption nobody can read and report PASS
while doing it. Four mechanisms, each of which gets a test here:

1. **Wrong units.** Michelson is a ratio of sums and the 25-level floor is an absolute
   difference; both are blind at the dark end. A dark fill can measure 27.0 levels of
   separation and 0.415 Michelson — clearing both — at an actual contrast ratio of 1.37:1.
2. **No way to say no.** Five dispositions ship a state short of target and none of them set
   any failure signal; `command_captions_ink` returned an unconditional `"PASS"` above them.
3. **Wrong base.** Every lockup restarted from the spec's `measured_rgb` (the reference's own
   ink) rather than from the fill the reviewer ratified, so a gold word is reset from
   [255, 223, 159] to [95, 83, 59] on every variant and gain-scaled from there.
4. **Wrong measurement geometry.** One ring mean per word answers for a word half on sky and
   half on dark timber with a number neither half has: a word can score 74.0 levels of
   separation globally while 57% of its ink sits under 3:1 against its own local background.

The numbers in the docstrings below are representative measurements of each failure class.
"""

from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pytest

from reelctl.captions import ink as ink_module

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


CANVAS = (120, 200)  # h, w
BOX_XY = (60, 40)
PLATE_WH = (60, 30)  # w, h


def _plate(tmp_path: Path, name: str = "L01.png") -> Path:
    import cv2

    plates = tmp_path / "plates"
    plates.mkdir(exist_ok=True)
    alpha = np.zeros((PLATE_WH[1], PLATE_WH[0]), dtype=np.uint8)
    alpha[6:-6, 6:-6] = 255
    cv2.imwrite(str(plates / name), alpha)
    return plates


def _frames(tmp_path: Path, name: str, count: int, *, ink_luma, bg_luma) -> Path:
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


def _split_frames(tmp_path: Path, name: str, count: int, *, left: int, right: int, split_x: int) -> Path:
    """A picture whose left half is sky and whose right half is dark timber."""
    import cv2

    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    for index in range(count):
        frame = np.full((CANVAS[0], CANVAS[1], 3), right, dtype=np.uint8)
        frame[:, :split_x] = left
        cv2.imwrite(str(directory / f"r{index:03d}.png"), frame)
    return directory


def _spec(measured_rgb, *, span=(0, 8), states=("S01",)) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "caption_ink_lockups",
        "lockups": [
            {
                "id": "G01",
                "measured_rgb": list(measured_rgb),
                "members": [
                    {
                        "layer": "L01",
                        "plate": "L01.png",
                        "box_xy": list(BOX_XY),
                        "span": list(span),
                        "states": list(states),
                    }
                ],
            }
        ],
    }


def _contract(ink_rgb, *, state_ids=("S01",)) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "caption_contract",
        "status": "SEALED",
        "render_allowed": True,
        "reference": {"frame_count": 8, "width": CANVAS[1], "height": CANVAS[0]},
        "styles": {"T_sans": {"render_path": "source_contour"}},
        "font_hypotheses": [],
        "states": [
            {
                "id": state_id,
                "text": "Gold",
                "start_frame": 0,
                "end_frame_exclusive": 8,
                "style_id": "T_sans",
                "ink": {"source": "sampled_reference_pixels", "rgb_median": list(ink_rgb), "alpha_median": 255},
            }
            for state_id in state_ids
        ],
    }


def _resolve(tmp_path, spec, reference, candidate, **kwargs):
    return ink_module.resolve_ink(
        spec,
        plates_dir=tmp_path / "plates",
        reference_frames=reference,
        candidate_frames=candidate,
        **kwargs,
    )


# ---------------------------------------------------------------------------------------
# A2 — the units


def test_the_contrast_ratio_is_the_wcag_ratio_not_a_luma_difference() -> None:
    """White on black is 21:1; a colour against itself is 1:1. Both are definitional."""
    assert round(ink_module.contrast_ratio((255, 255, 255), (0, 0, 0)), 2) == 21.0
    assert round(ink_module.contrast_ratio((138, 29, 36), (138, 29, 36)), 3) == 1.0
    # symmetric: the ratio does not care which argument is the ink
    assert ink_module.contrast_ratio((255, 223, 159), (20, 20, 20)) == ink_module.contrast_ratio(
        (20, 20, 20), (255, 223, 159)
    )


def test_a_fill_that_clears_michelson_but_not_the_contrast_ratio_is_not_clean(tmp_path) -> None:
    """A dark fill that clears the old units, in representative numbers.

    Shipped fill delivering luma 19.0 on a surround of 46.0: 27.0 levels of separation (over
    the 25 floor), Michelson 0.415 (over its 0.353 target) — and 1.37:1, which is a ghost.
    Both of the stage's old units pass it. The ratio does not, and the verdict must not.
    """
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=120, bg_luma=47)  # refM ~ 0.437 -> target ~0.35
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=46)
    out = _resolve(tmp_path, _spec((19, 19, 19)), reference, candidate, strict=True)
    row = out["lockups"]["G01"]

    # what the OLD rule saw, on the fill it shipped: both of its units are clear
    assert row["michelson_before"] >= row["target_michelson"]
    assert row["separation_before"] >= ink_module.MIN_SEPARATION
    # and the number a reader follows says it is a ghost
    assert row["contrast_ratio_before"] < ink_module.MIN_CONTRAST_RATIO
    assert row["entered_on"] == "contrast ratio", "the ratio, alone, is what puts this fill in play"
    # nothing on the ray from a near-black base reaches 3:1 against a surround of 46, so the
    # honest end state is a reported failure, not a quiet ship
    assert row["contrast_ratio_after"] < ink_module.MIN_CONTRAST_RATIO
    assert row["verdict"] == "UNRESOLVED"
    assert out["status"] == "FAIL"
    assert "G01" in out["lockups_below_contrast_floor"]


# ---------------------------------------------------------------------------------------
# A3 — the fail branch


def test_the_stage_reports_fail_when_a_lockup_cannot_be_made_readable(tmp_path) -> None:
    """No fill in either polarity clears the floor against every delivered picture.

    The stage already names this outcome `NO PALETTE-LEGAL FILL`. What it never did was carry
    that name upward: the resolution had no status, so a caller could only find it by reading
    every disposition string.
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
        strict=True,
        candidate_pictures=[
            {"name": "refgeom", "frames": dark},
            {"name": "vertical", "frames": bright},
        ],
    )
    row = out["lockups"]["G01"]
    assert row["disposition"] == "NO PALETTE-LEGAL FILL"
    assert row["verdict"] == "UNRESOLVED"
    assert out["status"] == "FAIL"
    assert out["verdicts"]["G01"] == "UNRESOLVED"


def test_the_low_reference_contrast_branch_is_reported_not_silent(tmp_path) -> None:
    """registry TOOL-DEFECT-captions-ink-silent-noop.

    `reference_michelson > MIN_REFERENCE_MICHELSON` guards the whole move with no else, so a
    reference beat carrying no contrast of its own left the fill untouched and reported
    nothing at all. Real lockups rarely sit under 0.02, so this reference has to be
    constructed by hand — which is the point: the branch is unreachable on typical data, and
    therefore untested without this.
    """
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=100, bg_luma=99)  # refM ~ 0.005
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=60)
    out = _resolve(tmp_path, _spec((55, 55, 55)), reference, candidate, strict=True)
    row = out["lockups"]["G01"]

    assert row["reference_michelson"] <= ink_module.MIN_REFERENCE_MICHELSON
    assert row["skipped_low_reference_contrast"] is True
    assert row["disposition"] == "low reference contrast, left measured"
    assert row["verdict"] == "UNRESOLVED"
    assert out["lockups_skipped_low_reference_contrast"] == ["G01"]
    assert out["status"] == "FAIL"


def test_a_disposition_that_ships_short_of_the_reference_is_never_clean(tmp_path) -> None:
    """The four shipping-short dispositions must set verdict != CLEAN ."""
    for disposition in (
        "lifted, short of reference",
        "low, left measured",
        "flipped, short of reference",
        "NO PALETTE-LEGAL FILL",
    ):
        assert disposition in ink_module.SHIPS_SHORT_DISPOSITIONS


# ---------------------------------------------------------------------------------------
# A1 — the base


def test_the_approved_contract_fill_is_the_base_when_one_is_supplied(tmp_path) -> None:
    """The ratified word is gold. The spec's `measured_rgb` is the reference's own ink, [95, 83, 59].

    Whenever a spec base differs from the approved contract it is because a review round
    adjudicated that fill. Starting from the spec discards that adjudication on every re-ink.
    """
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=20)
    out = _resolve(
        tmp_path,
        _spec((95, 83, 59)),
        reference,
        candidate,
        base_from_contract=_contract((255, 223, 159)),
    )
    row = out["lockups"]["G01"]
    assert row["base_rgb"] == [255, 223, 159]
    assert row["base_source"] == "approved_contract"
    assert row["reference_measured_rgb"] == [95, 83, 59]
    # the fill that ships is on the GOLD ray, not on the reference-measurement ray
    fill = out["states"]["S01"]["rgb"]
    assert fill[0] > fill[1] > fill[2]
    assert abs(fill[1] / fill[0] - 223 / 255) < 0.02
    assert abs(fill[2] / fill[0] - 159 / 255) < 0.02


def test_without_a_contract_base_the_reference_measurement_is_still_the_base(tmp_path) -> None:
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=20)
    out = _resolve(tmp_path, _spec((250, 250, 250)), reference, candidate)
    row = out["lockups"]["G01"]
    assert row["base_rgb"] == [250, 250, 250]
    assert row["base_source"] == "reference_measurement"
    assert row["reference_measured_rgb"] == [250, 250, 250]


def test_a_lockup_whose_members_disagree_about_the_ratified_fill_is_refused(tmp_path) -> None:
    """`left` and `right` are one lockup and must carry one fill; if the contract says two,
    the stage has no base and must say so rather than pick one."""
    _plate(tmp_path)
    _plate(tmp_path, "L02.png")
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=20)
    spec = _spec((250, 250, 250))
    spec["lockups"][0]["members"].append(
        {"layer": "L02", "plate": "L02.png", "box_xy": list(BOX_XY), "span": [0, 8], "states": ["S02"]}
    )
    contract = _contract((138, 29, 36), state_ids=("S01", "S02"))
    contract["states"][1]["ink"]["rgb_median"] = [255, 174, 180]
    with pytest.raises(ink_module.InkError, match="one fill"):
        _resolve(tmp_path, spec, reference, candidate, base_from_contract=contract)


def test_a_contract_that_does_not_carry_a_lockups_states_is_refused(tmp_path) -> None:
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=20)
    contract = _contract((255, 223, 159))
    contract["states"][0]["id"] = "SOMETHING_ELSE"
    with pytest.raises(ink_module.InkError):
        _resolve(tmp_path, _spec((95, 83, 59)), reference, candidate, base_from_contract=contract)


# ---------------------------------------------------------------------------------------
# A5 — the measurement geometry


def test_a_word_half_on_sky_and_half_on_timber_fails_on_weak_ink_fraction(tmp_path) -> None:
    """74.0 levels of separation globally, 57% of the ink under 3:1 locally.

    One ring mean per word answers for a word whose halves sit on different worlds with a
    number neither half has. The fill here clears every global gate — Michelson, the 25-level
    floor, and the 3:1 ratio against the ring mean — while half its strokes are invisible.
    """
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=130, bg_luma=100)  # a low-contrast beat
    candidate = _split_frames(tmp_path, "cand", 8, left=220, right=20, split_x=90)
    out = _resolve(tmp_path, _spec((255, 255, 255)), reference, candidate, strict=True)
    row = out["lockups"]["G01"]

    assert row["disposition"] == "measured"
    assert row["separation_after"] >= ink_module.MIN_SEPARATION
    assert row["contrast_ratio_after"] >= ink_module.MIN_CONTRAST_RATIO, "globally it looks fine"
    assert row["weak_ink_fraction"] > ink_module.MAX_WEAK_INK_FRACTION
    assert row["verdict"] == "UNRESOLVED"
    assert out["status"] == "FAIL"


def test_the_span_is_measured_on_every_frame_not_on_its_last_four(tmp_path) -> None:
    """DOCTRINE 15.3 forbids sampling a span at one convenient point.

    The hold median hides a frame the word is unreadable on: here frames 0-6 are dark and
    frame 7 — the only one the 4-frame hold median is dominated by — is not.
    """
    import cv2

    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=20)
    # frame 1 is a flash the hold median never sees: the ink's own colour, wall to wall
    flash = np.full((CANVAS[0], CANVAS[1], 3), 250, dtype=np.uint8)
    cv2.imwrite(str(candidate / "r001.png"), flash)

    out = _resolve(tmp_path, _spec((250, 250, 250)), reference, candidate, strict=True)
    row = out["lockups"]["G01"]
    assert row["frames_measured"] == 8
    assert row["worst_frame"] == 1
    assert row["verdict"] == "UNRESOLVED"


# ---------------------------------------------------------------------------------------
# the local-background primitive itself


def test_the_local_background_reads_the_picture_under_each_stroke() -> None:
    picture = np.zeros((60, 60, 3), dtype=np.uint8)
    picture[:, :30] = 220
    local = ink_module.local_background_luma(picture)
    assert local.shape == (60, 60)
    assert local[30, 5] > 200  # deep in the bright half
    assert local[30, 55] < 20  # deep in the dark half


def test_the_local_background_inpaints_an_occluding_caption_when_one_is_given() -> None:
    """On a DELIVERED frame the ink is already burned in, so the pixels under a stroke are the
    stroke. The envelope is inpainted so a word cannot supply its own background."""
    picture = np.full((60, 60, 3), 200, dtype=np.uint8)
    picture[25:35, 25:35] = 0  # burned-in black ink
    envelope = np.zeros((60, 60), dtype=bool)
    envelope[25:35, 25:35] = True
    naive = ink_module.local_background_luma(picture)
    honest = ink_module.local_background_luma(picture, occlusion_mask=envelope)
    assert naive[30, 30] < 120, "without the mask the ink drags its own background down"
    assert honest[30, 30] > 150, "with the mask the background is the wall the word sits on"


# ---------------------------------------------------------------------------------------
# the resolution's own roll-up


def test_the_resolution_carries_a_status_and_the_three_lists(tmp_path) -> None:
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=20)
    out = _resolve(tmp_path, _spec((250, 250, 250)), reference, candidate, strict=True)
    assert out["status"] in ink_module.STATUSES
    for key in (
        "lockups_below_contrast_floor",
        "lockups_short_of_target",
        "lockups_skipped_low_reference_contrast",
        "verdicts",
    ):
        assert key in out
    assert out["lockups"]["G01"]["verdict"] == "CLEAN"
    assert out["status"] == "PASS"


def test_the_resolution_is_json_serialisable_with_the_per_pixel_measurement_on(tmp_path) -> None:
    """The local measurement carries numpy arrays; none of them may reach the receipt."""
    import json

    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=20)
    out = _resolve(tmp_path, _spec((250, 250, 250)), reference, candidate, strict=True)
    json.dumps(out)


def test_apply_resolution_is_unchanged_by_the_new_fields(tmp_path) -> None:
    _plate(tmp_path)
    reference = _frames(tmp_path, "ref", 8, ink_luma=250, bg_luma=20)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=20)
    out = _resolve(tmp_path, _spec((250, 250, 250)), reference, candidate)
    contract = _contract((10, 10, 10))
    patched, diff = ink_module.apply_resolution(copy.deepcopy(contract), out["states"])
    assert patched["states"][0]["ink"]["rgb_median"] == [250, 250, 250]
    assert diff["S01"] == {"from": [10, 10, 10], "to": [250, 250, 250]}


def test_a_search_that_returns_the_base_is_not_reported_as_a_lift(tmp_path) -> None:
    """A disposition is a claim about what the stage DID. It has to be true.

    Representative case: a shipped [18, 16, 11] over a dark picture enters the search on the
    contrast ratio, finds no gain in polarity that reaches 3:1, and the nearest-to-1.0 pick
    among the fills that merely clear the Michelson target is the base itself. Without this
    check the row ships `disposition: lifted for contrast ratio` with `rgb` equal to the fill it
    started from — a receipt claiming a move that never happened.
    """
    _plate(tmp_path)
    # dark ink on a bright background, which is the word's own polarity in the reference
    reference = _frames(tmp_path, "ref", 8, ink_luma=60, bg_luma=200)
    candidate = _frames(tmp_path, "cand", 8, ink_luma=None, bg_luma=60)
    out = _resolve(tmp_path, _spec((19, 19, 19)), reference, candidate, strict=True)
    row = out["lockups"]["G01"]
    assert row["contrast_ratio_before"] < ink_module.MIN_CONTRAST_RATIO
    assert row["michelson_before"] >= row["target_michelson"]
    assert row["separation_before"] >= ink_module.MIN_SEPARATION
    assert row["rgb"] == [19, 19, 19], "this fixture is only interesting while nothing moves"
    assert "lifted" not in row["disposition"]
    assert row["disposition"] == "no readable fill in polarity, left measured"
    assert row["verdict"] == "UNRESOLVED"
    # it reached the reference's Michelson target, so it is not "short of target"; what it
    # never reached is the readability floor, and that is the list it belongs on
    assert out["lockups_short_of_target"] == []
    assert "G01" in out["lockups_below_contrast_floor"]
