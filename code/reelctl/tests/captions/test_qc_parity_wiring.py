"""The caption QC gate that makes a wrong-word ship impossible.

`reelctl.captions.qc.qc_state` — per-state Dice/IoU of the rendered alpha against the
reference's own ink — has existed since the engine was written and was never reachable from
the CLI: `captions qc` ran `qc_timing` only and reported `visual_parity` as "BLOCKED -
requires a native 1:1 board review". A render can ship nearly every caption word wrong
through that hole, with its own clearance gate green, because the only gate that ran measured
our ink against our picture and had no concept of what the reference said.

Two gates close it, and both are hard:

* **word identity** — the contract's text, state by state, against the sealed authority's.
* **state parity** — the rendered alpha, state by state, against the plate traced from the
  reference's own pixels.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from reelctl.captions import qc as qc_module
from reelctl.captions_cli import command_captions_qc

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


CANVAS_W, CANVAS_H = 200, 120
PLATE_W, PLATE_H = 60, 30
BOX = [70, 40]


def _plate_alpha() -> np.ndarray:
    alpha = np.zeros((PLATE_H, PLATE_W), dtype=np.uint8)
    alpha[6:-6, 6:-6] = 255
    return alpha


def _contract(texts=("Gold", "good")) -> dict:
    states = []
    for index, text in enumerate(texts):
        states.append(
            {
                "id": f"C{index + 1:02d}",
                "text": text,
                "start_frame": index * 4,
                "end_frame_exclusive": index * 4 + 4,
                "style_id": "T_sans",
                "placement": {
                    "core_bbox_xyxy": [BOX[0], BOX[1], BOX[0] + PLATE_W, BOX[1] + PLATE_H],
                    "treatment_bbox_xyxy": [BOX[0], BOX[1], BOX[0] + PLATE_W, BOX[1] + PLATE_H],
                    "anchor": "top_left",
                },
                "geometry": {"scale": 1.0, "translate_xy": [0.0, 0.0]},
                "ink": {"source": "sampled_reference_pixels", "rgb_median": [255, 223, 159], "alpha_median": 255},
                "lifecycle": {"kind": "hard_state"},
                "stacking": {"z": 0, "persists": False},
            }
        )
    return {
        "schema_version": 1,
        "artifact_type": "caption_contract",
        "status": "SEALED",
        "render_allowed": True,
        "reference": {"frame_count": len(texts) * 4, "width": CANVAS_W, "height": CANVAS_H},
        "styles": {"T_sans": {"render_path": "source_contour"}},
        "font_hypotheses": [],
        "states": states,
    }


def _authority(texts=("Gold", "good")) -> dict:
    return {
        "states": [
            {"id": f"C{index + 1:02d}", "text": text} for index, text in enumerate(texts)
        ]
    }


def _write_scene(tmp_path: Path, contract: dict, *, alpha_for_state=None) -> tuple:
    """Plates on disk and a rendered RGBA sequence that places them, as the renderer would."""
    plates = tmp_path / "plates"
    plates.mkdir(exist_ok=True)
    rendered = tmp_path / "rendered"
    rendered.mkdir(exist_ok=True)

    frames = [
        np.zeros((CANVAS_H, CANVAS_W, 4), dtype=np.uint8)
        for _ in range(contract["reference"]["frame_count"])
    ]
    for state in contract["states"]:
        plate = _plate_alpha()
        cv2.imwrite(str(plates / f"{state['id']}.png"), plate)
        drawn = plate if alpha_for_state is None else alpha_for_state(state["id"], plate)
        x0, y0 = state["placement"]["core_bbox_xyxy"][:2]
        for frame in range(state["start_frame"], state["end_frame_exclusive"]):
            region = frames[frame][y0 : y0 + PLATE_H, x0 : x0 + PLATE_W]
            region[:, :, 3] = drawn
            for channel, value in enumerate(state["ink"]["rgb_median"]):
                region[:, :, channel] = np.where(drawn > 0, value, 0)
    for index, frame in enumerate(frames):
        cv2.imwrite(str(rendered / f"caption-{index:03d}.png"), frame)
    return plates, rendered


class _Args:
    def __init__(self, **kwargs):
        self.plates = None
        self.authority = None
        self.output = None
        for key, value in kwargs.items():
            setattr(self, key, value)


# ---------------------------------------------------------------------------------------
# D3 — the words


def test_word_identity_passes_when_the_contract_says_what_the_authority_says() -> None:
    report = qc_module.qc_word_identity(_contract()["states"], _authority()["states"])
    assert report["passed"] is True
    assert report["mismatches"] == []


def test_a_single_rewritten_word_fails_word_identity() -> None:
    """The whole wrong-word defect class, in one assertion."""
    report = qc_module.qc_word_identity(
        _contract(("Gold", "good"))["states"], _authority(("Gold", "great"))["states"]
    )
    assert report["passed"] is False
    assert report["mismatches"][0]["contract_text"] == "good"
    assert report["mismatches"][0]["authority_text"] == "great"


def test_a_dropped_state_fails_word_identity_rather_than_zipping_past_it() -> None:
    """Dropped reference states must be reported; a zip would have hidden them."""
    report = qc_module.qc_word_identity(
        _contract(("Gold",))["states"], _authority(("Gold", "good"))["states"]
    )
    assert report["passed"] is False
    assert report["state_count"] == {"contract": 1, "authority": 2}


def test_punctuation_is_part_of_the_word() -> None:
    """`IM` is not `I'M`. A stripped apostrophe is a wrong word."""
    report = qc_module.qc_word_identity(
        _contract(("IM",))["states"], _authority(("I'M",))["states"]
    )
    assert report["passed"] is False


# ---------------------------------------------------------------------------------------
# D2 — the pixels


def test_state_parity_passes_when_the_rendered_alpha_is_the_reference_plate(tmp_path) -> None:
    contract = _contract()
    plates, rendered = _write_scene(tmp_path, contract)
    args = _Args(contract=tmp_path / "c.json", rendered=rendered, plates=plates)
    (tmp_path / "c.json").write_text(json.dumps(contract))
    report = command_captions_qc(args)
    assert report["state_parity"]["passed"] is True
    assert report["state_parity"]["states"] == 2
    assert report["authorities"]["visual_parity"] == "PASS"
    assert report["status"] == "PASS"


def test_an_eroded_glyph_fails_state_parity(tmp_path) -> None:
    """An eroded glyph: `Gold` reading `Gald` because the
    strokes eroded. A timing gate cannot see it; a Dice gate against the reference can."""
    contract = _contract()

    def erode(state_id, plate):
        if state_id != "C01":
            return plate
        return cv2.erode(plate, np.ones((7, 7), np.uint8))

    plates, rendered = _write_scene(tmp_path, contract, alpha_for_state=erode)
    (tmp_path / "c.json").write_text(json.dumps(contract))
    report = command_captions_qc(_Args(contract=tmp_path / "c.json", rendered=rendered, plates=plates))
    assert report["state_parity"]["passed"] is False
    assert report["state_parity"]["failing_states"] == ["C01"]
    assert report["authorities"]["visual_parity"] == "FAIL"
    assert report["status"] == "FAIL"


def test_the_word_identity_gate_fails_the_whole_qc_run(tmp_path) -> None:
    contract = _contract()
    plates, rendered = _write_scene(tmp_path, contract)
    (tmp_path / "c.json").write_text(json.dumps(contract))
    (tmp_path / "a.json").write_text(json.dumps(_authority(("Gold", "great"))))
    report = command_captions_qc(
        _Args(
            contract=tmp_path / "c.json",
            rendered=rendered,
            plates=plates,
            authority=tmp_path / "a.json",
        )
    )
    assert report["word_identity"]["passed"] is False
    assert report["status"] == "FAIL"


def test_a_qc_run_without_the_plates_says_the_parity_gate_did_not_run(tmp_path) -> None:
    """Silence is not a pass. A run that skipped a gate has to name the gate it skipped."""
    contract = _contract()
    _, rendered = _write_scene(tmp_path, contract)
    (tmp_path / "c.json").write_text(json.dumps(contract))
    report = command_captions_qc(_Args(contract=tmp_path / "c.json", rendered=rendered))
    assert "state_parity" in report["gates_not_run"]
    assert "word_identity" in report["gates_not_run"]
    assert report["authorities"]["visual_parity"].startswith("BLOCKED")


# ---------------------------------------------------------------------------------------
# IDENTITY — the fill is still the fill the reviewer ratified
#
# Failure class: a gold fill re-inked as a pure gain of the reference measurement [95, 83, 59]
# rather than of the ratified [255, 223, 159], landing anywhere from [18, 16, 11] to
# [255, 251, 242]. Without a gate comparing a delivered fill with the ratified fill, "the gold
# word stopped being gold" is invisible to every gate that runs. `ink_distance` and `MAX_INK_DISTANCE` were
# in the library and unreferenced; this is the gate that uses them.


def _ratified(rgb=(255, 223, 159), *, state_ids=("C01", "C02")) -> dict:
    contract = _contract()
    for state, state_id in zip(contract["states"], state_ids):
        state["id"] = state_id
        state["ink"]["rgb_median"] = list(rgb)
    return contract


def test_ink_identity_passes_when_the_fill_is_the_ratified_gold() -> None:
    report = qc_module.qc_ink_identity(_contract()["states"], _ratified()["states"])
    assert report["passed"] is True
    assert [row["ink_distance"] for row in report["rows"]] == [0.0, 0.0]
    assert report["max_ink_distance"] == qc_module.MAX_INK_DISTANCE


def test_a_fill_inside_the_band_is_still_the_ratified_ink() -> None:
    """DOCTRINE 15.3's own band: a couple of levels of rounding is not a new colour."""
    contract = _contract()
    for state in contract["states"]:
        state["ink"]["rgb_median"] = [253, 222, 157]
    report = qc_module.qc_ink_identity(contract["states"], _ratified()["states"])
    assert report["passed"] is True
    assert report["rows"][0]["ink_distance"] < qc_module.MAX_INK_DISTANCE


def test_a_gold_fill_that_stopped_being_gold_fails_ink_identity() -> None:
    """The ratified gold [255, 223, 159] shipped as [18, 16, 11]."""
    contract = _contract()
    contract["states"][0]["ink"]["rgb_median"] = [18, 16, 11]
    report = qc_module.qc_ink_identity(contract["states"], _ratified()["states"])
    assert report["passed"] is False
    assert report["failing_states"] == ["C01"]
    assert report["rows"][0]["ink_distance"] > 300


def test_the_reference_measurement_is_not_the_ratified_fill() -> None:
    """The wrong base, as a gate: [95, 83, 59] is the reference's own ink, not the contract's."""
    contract = _contract()
    for state in contract["states"]:
        state["ink"]["rgb_median"] = [95, 83, 59]
    report = qc_module.qc_ink_identity(contract["states"], _ratified()["states"])
    assert report["passed"] is False
    assert report["failing_states"] == ["C01", "C02"]


def test_ink_identity_refuses_a_ratified_contract_that_does_not_carry_the_state() -> None:
    """A state with no ratified counterpart is unjudged, and unjudged is not a pass."""
    report = qc_module.qc_ink_identity(
        _contract()["states"], _ratified(state_ids=("C01", "C99"))["states"]
    )
    assert report["passed"] is False
    assert report["unmatched_states"] == ["C02"]


def test_the_ink_identity_gate_fails_the_whole_qc_run(tmp_path) -> None:
    contract = _contract()
    contract["states"][0]["ink"]["rgb_median"] = [18, 16, 11]
    plates, rendered = _write_scene(tmp_path, contract)
    (tmp_path / "c.json").write_text(json.dumps(contract))
    (tmp_path / "r.json").write_text(json.dumps(_ratified()))
    report = command_captions_qc(
        _Args(
            contract=tmp_path / "c.json",
            rendered=rendered,
            plates=plates,
            ratified_contract=tmp_path / "r.json",
        )
    )
    assert report["ink_identity"]["passed"] is False
    assert report["authorities"]["ink_identity"] == "FAIL"
    assert report["status"] == "FAIL"


def test_a_qc_run_without_a_ratified_contract_names_the_gate_it_skipped(tmp_path) -> None:
    contract = _contract()
    plates, rendered = _write_scene(tmp_path, contract)
    (tmp_path / "c.json").write_text(json.dumps(contract))
    report = command_captions_qc(
        _Args(contract=tmp_path / "c.json", rendered=rendered, plates=plates)
    )
    assert "ink_identity" in report["gates_not_run"]
    assert report["authorities"]["ink_identity"].startswith("BLOCKED")
    assert report["status"] == "PASS"
