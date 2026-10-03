"""The factory's single entry point into the caption engine.

DONE criterion 5: "Factory stages call the engine; per-reel caption scripts are dead."

The engine is reachable as `reelctl captions ...` so no stage ever needs a reel-specific
caption script again. The existing per-reel scripts stay on disk as fixtures — the append-only
invariant forbids deleting them — but they are superseded, and nothing in the pipeline calls
them.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from reelctl.cli import main

MASK_SHA = "d" * 64


def _contract(frame_count: int = 12) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "caption_contract",
        "status": "EXTRACTED",
        "render_allowed": False,
        "reference": {
            "path": "reference/ref.mp4",
            "sha256": "c" * 64,
            "frame_count": frame_count,
            "fps": "24/1",
            "width": 200,
            "height": 120,
            "sample_aspect_ratio": "1:1",
        },
        "styles": {"T01": {"render_path": "source_contour"}},
        "states": [
            {
                "id": "C01",
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
                    "mask_path": "evidence/C01.png",
                    "mask_sha256": MASK_SHA,
                },
            }
        ],
        "font_hypotheses": [],
    }


def test_captions_command_group_exists() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["captions", "--help"])
    assert exit_info.value.code == 0


def test_captions_validate_accepts_a_good_contract(tmp_path, capsys) -> None:
    path = tmp_path / "caption-contract.json"
    path.write_text(json.dumps(_contract()))
    assert main(["captions", "validate", "--contract", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert payload["states"] == 1
    assert len(payload["schema_sha256"]) == 64


def test_captions_validate_fails_closed_on_a_bad_contract(tmp_path, capsys) -> None:
    contract = _contract()
    contract["states"][0]["frames"] = 99
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(contract))
    assert main(["captions", "validate", "--contract", str(path)]) != 0
    assert "frames" in capsys.readouterr().err


def test_captions_program_imports_a_sealed_authority(tmp_path, capsys) -> None:
    authority = {
        "visual_states": [
            {"id": "s1", "text": "a", "start": 0, "end": 4,
             "entry": {"first_detectable": 0, "first_crisp": 2}, "hold": [2, 4],
             "font_class": "sans", "behavior": "hard replacement"},
            {"id": "s2", "text": "b", "start": 6, "end": 9,
             "entry": {"first_detectable": 6, "first_crisp": 7}, "hold": [7, 9],
             "font_class": "sans", "behavior": "hard replacement"},
        ],
        "authority": {
            "shortcode": "TEST",
            "video": {
                "frame_count": 10, "raster": [200, 120], "fps": "24/1",
                "decoded_rgb24_essence_sha256": "a" * 64,
            },
        },
        "regression_gates": {"authority": {"blank_frames": [5]}},
        "next_implementation_gate": {"render_allowed": False},
        "font_forensics": {"sans": {"exact_face_name": None}, "script": {"exact_face_name": None}},
        "treatment_contract": {"forbidden": ["per-word x/y envelope stretching"]},
    }
    authority_path = tmp_path / "authority.json"
    authority_path.write_text(json.dumps(authority))
    out = tmp_path / "program.json"
    assert main(["captions", "program", "--authority", str(authority_path),
                 "--output", str(out)]) == 0
    program = json.loads(out.read_text())
    assert program["frame_count"] == 10
    assert program["blank_frames"] == [5]
    assert len(program["states"]) == 2
    assert program["render_allowed"] is False


def test_captions_program_refuses_a_blank_frame_mismatch(tmp_path, capsys) -> None:
    authority = {
        "visual_states": [
            {"id": "s1", "text": "a", "start": 0, "end": 3, "font_class": "sans",
             "behavior": "hard", "entry": {"first_detectable": 0}},
        ],
        "authority": {"shortcode": "T", "video": {
            "frame_count": 6, "raster": [200, 120], "fps": "24/1",
            "decoded_rgb24_essence_sha256": "a" * 64}},
        "regression_gates": {"authority": {"blank_frames": [5]}},  # derived is 4 and 5, so this mismatches
        "next_implementation_gate": {"render_allowed": False},
    }
    path = tmp_path / "a.json"
    path.write_text(json.dumps(authority))
    assert main(["captions", "program", "--authority", str(path),
                 "--output", str(tmp_path / "p.json")]) != 0
    assert "blank" in capsys.readouterr().err


def test_captions_render_writes_a_full_canvas_sequence(tmp_path, capsys) -> None:
    import cv2

    contract_path = tmp_path / "c.json"
    contract_path.write_text(json.dumps(_contract()))
    plates = tmp_path / "plates"
    plates.mkdir()
    cv2.imwrite(str(plates / "C01.png"), np.full((20, 40), 255, dtype=np.uint8))
    out = tmp_path / "rendered"

    assert main(["captions", "render", "--contract", str(contract_path),
                 "--plates", str(plates), "--output", str(out),
                 "--allow-unsealed-local-review"]) == 0
    written = sorted(p.name for p in out.glob("*.png"))
    assert len(written) == 12
    assert written[0] == "caption-000.png"
    receipt = json.loads((out / "render-receipt.json").read_text())
    assert receipt["frame_count"] == 12
    assert receipt["frames_with_ink"] == 4
    assert receipt["render_allowed_in_contract"] is False
    assert receipt["rendered_under_local_review_flag"] is True


def test_captions_render_refuses_an_unsealed_contract_without_the_flag(tmp_path, capsys) -> None:
    import cv2

    contract_path = tmp_path / "c.json"
    contract_path.write_text(json.dumps(_contract()))
    plates = tmp_path / "plates"
    plates.mkdir()
    cv2.imwrite(str(plates / "C01.png"), np.full((20, 40), 255, dtype=np.uint8))
    assert main(["captions", "render", "--contract", str(contract_path),
                 "--plates", str(plates), "--output", str(tmp_path / "out")]) != 0
    assert "SEALED" in capsys.readouterr().err


def test_captions_ink_resolves_a_fill_and_writes_both_contract_twins(tmp_path, capsys) -> None:
    """The stage a variant re-runs when the picture behind the words changes."""
    import cv2

    contract_path = tmp_path / "c.json"
    contract_path.write_text(json.dumps(_contract()))

    plates = tmp_path / "plates"
    plates.mkdir()
    alpha = np.zeros((20, 40), dtype=np.uint8)
    alpha[4:-4, 4:-4] = 255
    cv2.imwrite(str(plates / "C01.png"), alpha)

    # the reference: white ink on a dark field. our picture: a pale background, which the
    # reference's own white cannot be seen against.
    reference = tmp_path / "ref"
    candidate = tmp_path / "cand"
    for directory, ink in ((reference, 250), (candidate, None)):
        directory.mkdir()
        for index in range(12):
            frame = np.full((120, 200, 3), 12 if ink else 205, dtype=np.uint8)
            if ink:
                frame[44:56, 64:96] = ink
            cv2.imwrite(str(directory / f"r{index:03d}.png"), frame)

    lockups = tmp_path / "lockups.json"
    lockups.write_text(json.dumps({
        "schema_version": 1,
        "artifact_type": "caption_ink_lockups",
        "lockups": [{
            "id": "G01",
            "measured_rgb": [250, 250, 250],
            "members": [{"layer": "C01", "plate": "C01.png", "box_xy": [60, 40],
                         "span": [2, 6], "states": ["C01"]}],
        }],
    }))

    resolution = tmp_path / "resolution.json"
    assert main([
        "captions", "ink",
        "--contract", str(contract_path),
        "--lockups", str(lockups),
        "--plates", str(plates),
        "--reference-frames", str(reference),
        "--candidate-frames", str(candidate),
        "--output", str(resolution),
        "--out-contract", str(tmp_path / "out.json"),
        "--out-contract-render", str(tmp_path / "out-render.json"),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert payload["creative_approval"] == "PENDING"
    assert payload["states_changed"] == ["C01"]

    patched = json.loads((tmp_path / "out.json").read_text())
    twin = json.loads((tmp_path / "out-render.json").read_text())
    assert patched["states"][0]["ink"]["rgb_median"] != [247, 249, 251]
    assert twin["states"][0]["ink"]["rgb_median"] == list(
        reversed(patched["states"][0]["ink"]["rgb_median"])
    )
    # nothing but the fill moved
    original = _contract()
    for key in ("text", "start_frame", "end_frame_exclusive", "placement", "geometry",
                "evidence", "style_id"):
        assert patched["states"][0][key] == original["states"][0][key]


def test_captions_ink_refuses_to_emit_a_contract_that_moved_more_than_ink(tmp_path) -> None:
    """The guard, exercised directly: a patch that touches geometry is not an ink patch."""
    from reelctl.captions.ink import InkError, prove_only_ink_moved

    before = _contract()
    after = json.loads(json.dumps(before))
    after["states"][0]["ink"]["rgb_median"] = [10, 9, 6]
    after["states"][0]["placement"]["core_bbox_xyxy"] = [0, 0, 10, 10]
    with pytest.raises(InkError):
        prove_only_ink_moved(before, after)


def test_captions_render_has_no_publish_or_upload_option() -> None:
    """Publishing stays human-gated; the engine exposes no route to it."""
    from reelctl.cli import build_parser

    parser = build_parser()
    actions = {action.dest for action in parser._actions}  # noqa: SLF001
    text = parser.format_help()
    assert "publish" not in text.lower()
    assert "upload" not in text.lower()
    assert "drive" not in text.lower()
    assert actions  # parser built
