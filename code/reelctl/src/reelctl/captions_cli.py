"""CLI commands for the caption engine.

The factory's single entry point into ``reelctl.captions``. Kept in its own module so the
caption engine's surface in ``cli.py`` is a dozen lines of parser wiring.

Stage mapping (DONE criterion 5):

* ``captions program``  — reference intake, feeding BLUEPRINT_LOCKED
* ``captions validate`` — contract gate, called before ASSETS_LOCKED
* ``captions ink``      — resolve the fill against the picture it will sit on
* ``captions render``   — the caption stage of composition
* ``captions qc``       — the caption half of VISUAL_QC

There is deliberately no publish or upload command here. Local review renders are allowed;
anything outward stays behind a human approval.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .captions.contract import validate_caption_contract
from .captions.ink import (
    apply_resolution,
    prove_only_ink_moved,
    render_twin,
    resolve_ink,
)
from .captions.program import import_sealed_authority
from .captions.qc import (
    qc_ink_identity,
    qc_state_parity,
    qc_timing,
    qc_word_identity,
)
from .captions.render import render_caption_sequence, sequence_summary

__all__ = [
    "command_captions_ink",
    "command_captions_program",
    "command_captions_qc",
    "command_captions_render",
    "command_captions_validate",
]


def _load(path: Path) -> Any:
    return json.loads(Path(path).read_text())


def command_captions_validate(args) -> Dict[str, Any]:
    contract = _load(args.contract)
    receipt = validate_caption_contract(contract)
    return {
        **receipt,
        "contract": str(args.contract),
        "render_allowed": contract["render_allowed"],
        "contract_status": contract["status"],
    }


def command_captions_program(args) -> Dict[str, Any]:
    authority = _load(args.authority)
    program = import_sealed_authority(authority)
    payload = program.to_dict()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    return {
        "status": "PASS",
        "output": str(output),
        "states": len(program.states),
        "frame_count": program.frame_count,
        "blank_frames": list(program.blank_frames),
        "render_allowed": program.render_allowed,
        "sans_exact_face": program.sans_exact_face,
        "script_exact_face": program.script_exact_face,
    }


def command_captions_ink(args) -> Dict[str, Any]:
    """Re-resolve the contract's fills against the picture they will actually sit on.

    Ink is measured from the reference's own pixels; that measurement is the truth about the
    reference, not automatically a legible fill over a different picture. This command runs the
    engine's legibility rules over one candidate picture and, optionally, writes the patched
    contract twins. It moves ``states[].ink.rgb_median`` and refuses if anything else differs.
    """
    contract = _load(args.contract)
    spec = _load(args.lockups)
    resolution = resolve_ink(
        spec,
        plates_dir=Path(args.plates),
        reference_frames=Path(args.reference_frames),
        candidate_frames=Path(args.candidate_frames),
        reference_pattern=args.reference_pattern,
        candidate_pattern=args.candidate_pattern,
        reference_index_origin=args.reference_index_origin,
        candidate_index_origin=args.candidate_index_origin,
        strict=bool(args.strict),
        base_from_contract=contract if getattr(args, "base_from_contract", False) else None,
    )
    patched, diff = apply_resolution(contract, resolution["states"])
    moved = prove_only_ink_moved(contract, patched)
    resolution["contract"] = str(args.contract)
    resolution["ink_changes"] = moved
    resolution["states_changed"] = sorted(moved)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(resolution, indent=2) + "\n")

    written = {"resolution": str(output)}
    if args.out_contract:
        target = Path(args.out_contract)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(patched, indent=2) + "\n")
        written["contract"] = str(target)
    if args.out_contract_render:
        target = Path(args.out_contract_render)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(render_twin(patched), indent=2) + "\n")
        written["contract_render"] = str(target)

    # The status is the resolution's own, never an unconditional PASS. Five dispositions ship a
    # state short of target and one ships no legible fill at all; reporting
    # an unconditional "PASS" here would let a large share of lockup decisions in a variant batch
    # reach review with no warning attached. `cli.main()` maps FAIL to exit 2.
    return {
        "status": resolution["status"],
        "lockups": len(resolution["lockups"]),
        "states": len(resolution["states"]),
        "states_changed": resolution["states_changed"],
        "ink_changes": diff,
        "dispositions": {
            key: row["disposition"] for key, row in resolution["lockups"].items()
        },
        "verdicts": resolution["verdicts"],
        "lockups_below_contrast_floor": resolution["lockups_below_contrast_floor"],
        "lockups_short_of_target": resolution["lockups_short_of_target"],
        "lockups_skipped_low_reference_contrast": resolution["lockups_skipped_low_reference_contrast"],
        "verdict_reasons": {
            key: row["verdict_reasons"]
            for key, row in resolution["lockups"].items()
            if row["verdict_reasons"]
        },
        "base_source": {key: row["base_source"] for key, row in resolution["lockups"].items()},
        "gates": resolution["gates"],
        "written": written,
        "creative_approval": "PENDING",
        "scope": resolution["scope"],
    }


def command_captions_render(args) -> Dict[str, Any]:
    import cv2

    contract = _load(args.contract)
    plate_dir = Path(args.plates)
    plates = {}
    for state in contract["states"]:
        path = plate_dir / f"{state['id']}.png"
        if not path.is_file():
            raise FileNotFoundError(
                f"no plate for state {state['id']} at {path}; the caption sequence cannot be "
                "compiled with a missing plate"
            )
        plate = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if plate is None:
            raise ValueError(f"could not read plate {path}")
        if plate.ndim == 3:
            plate = plate[:, :, 3] if plate.shape[2] == 4 else cv2.cvtColor(
                plate, cv2.COLOR_BGR2GRAY
            )
        plates[state["id"]] = plate

    frames = render_caption_sequence(
        contract,
        plates=plates,
        allow_unsealed_local_review=bool(args.allow_unsealed_local_review),
    )

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    for index, frame in enumerate(frames):
        cv2.imwrite(
            str(output / f"caption-{index:03d}.png"),
            frame,
            [cv2.IMWRITE_PNG_COMPRESSION, 4],
        )

    summary = sequence_summary(frames)
    receipt = {
        "status": "PASS",
        "output": str(output),
        "frame_count": summary["frame_count"],
        "frames_with_ink": summary["frames_with_ink"],
        "empty_frames": summary["empty_frames"],
        "states": len(contract["states"]),
        "render_allowed_in_contract": contract["render_allowed"],
        "contract_status": contract["status"],
        "rendered_under_local_review_flag": bool(args.allow_unsealed_local_review),
        "publication": "NOT AUTHORISED - local review only; upload and publishing are operator-gated",
    }
    (output / "render-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def command_captions_qc(args) -> Dict[str, Any]:
    import cv2

    contract = _load(args.contract)
    rendered_dir = Path(args.rendered)
    paths = sorted(rendered_dir.glob("caption-*.png"))
    if not paths:
        raise FileNotFoundError(f"no caption-*.png frames found in {rendered_dir}")
    frames = []
    for path in paths:
        frame = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if frame is None or frame.ndim != 3 or frame.shape[2] != 4:
            raise ValueError(f"{path} is not an RGBA frame")
        frames.append(frame)

    timing = qc_timing(
        frames, contract["states"], frame_count=contract["reference"]["frame_count"]
    )

    # D2/D3. `qc_state` — per-state Dice against the reference's own ink — has been in the
    # library since the engine was written and was unreachable from here, so the compliant path
    # shipped a TIMING-ONLY gate and word fidelity rested on a human looking at a board, which
    # lets a render with nearly every word wrong through. Both gates below are hard when they run,
    # and a run that skips one has to name it: silence is not a pass.
    gates_not_run = []
    parity = None
    if getattr(args, "plates", None):
        plate_dir = Path(args.plates)
        plates = {}
        for state in contract["states"]:
            path = plate_dir / f"{state['id']}.png"
            if not path.is_file():
                raise FileNotFoundError(f"no plate for state {state['id']} at {path}")
            plate = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if plate is None:
                raise ValueError(f"could not read plate {path}")
            if plate.ndim == 3:
                plate = plate[:, :, 3] if plate.shape[2] == 4 else cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY)
            plates[state["id"]] = plate
        parity = qc_state_parity(contract, frames, plates)
    else:
        gates_not_run.append("state_parity")

    identity = None
    if getattr(args, "authority", None):
        authority = _load(args.authority)
        identity = qc_word_identity(contract["states"], authority["states"])
    else:
        gates_not_run.append("word_identity")

    # The third identity: the fill. A re-ink pass can apply a gain to the reference measurement
    # instead of the ratified fill, and without this gate nothing compares a delivered fill
    # with the fill the reviewer chose.
    ink = None
    if getattr(args, "ratified_contract", None):
        ratified = _load(args.ratified_contract)
        ink = qc_ink_identity(contract["states"], ratified["states"])
    else:
        gates_not_run.append("ink_identity")

    passed = (
        timing.passed
        and (parity is None or parity["passed"])
        and (identity is None or identity["passed"])
        and (ink is None or ink["passed"])
    )
    report = {
        "status": "PASS" if passed else "FAIL",
        "timing": timing.to_dict(),
        "rendered_frames": len(frames),
        "gates_not_run": gates_not_run,
        "authorities": {
            "technical_integrity": "PASS" if timing.passed else "FAIL",
            "visual_parity": (
                ("PASS" if parity["passed"] else "FAIL")
                if parity is not None
                else "BLOCKED - the per-state parity gate did not run (no --plates given); a "
                "native 1:1 board review is the only remaining evidence"
            ),
            "word_identity": (
                ("PASS" if identity["passed"] else "FAIL")
                if identity is not None
                else "BLOCKED - no sealed authority supplied (--authority)"
            ),
            "ink_identity": (
                ("PASS" if ink["passed"] else "FAIL")
                if ink is not None
                else "BLOCKED - no ratified contract supplied (--ratified-contract); nothing "
                "checked that the fills are still the fills the reviewer approved"
            ),
            "human_creative_approval": "PENDING",
        },
    }
    if parity is not None:
        report["state_parity"] = parity
    if identity is not None:
        report["word_identity"] = identity
    if ink is not None:
        report["ink_identity"] = ink
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        report["output"] = str(output)
    return report
