"""The caption state program — the gap-free zero-based caption-state ledger.

EBG section 2 requires this ledger to exist *before* any font fitting: exact words and
punctuation, state ranges and true blank intervals, line membership and stack
accumulation, class, size tier, entry/hold/replacement/exit states, blur lifecycle,
placement mode and composite behaviour. "If the script, stack grammar, endpoint, or frame
domain differs, no font tuning can produce parity."

A sealed authority document supplies the *program*. It does not supply placement boxes or
ink — those are pixel measurements that the EXTRACT stage takes from the reference frames.
Keeping the two apart is what stops a document from being mistaken for a measurement.

Interval semantics: sealed authorities in this corpus use **inclusive** `start`/`end`
ranges. The engine stores **half-open** `[start, end_exclusive)` everywhere
(``DOCTRINE.md`` rule 1.3), so the conversion happens once, here, and is recorded on the
program so it stays auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "Lifecycle",
    "ProgramState",
    "StateProgram",
    "StateProgramError",
    "import_sealed_authority",
]


class StateProgramError(ValueError):
    pass


def _half_open(window: Sequence[int]) -> Tuple[int, int]:
    """Convert an inclusive [a, b] window to half-open [a, b+1)."""
    if len(window) != 2:
        raise StateProgramError(f"expected a 2-element inclusive window, got {window!r}")
    start, end_inclusive = int(window[0]), int(window[1])
    if end_inclusive < start:
        raise StateProgramError(f"inclusive window {window!r} ends before it starts")
    return start, end_inclusive + 1


@dataclass(frozen=True)
class Lifecycle:
    """Entry/hold/exit windows, all half-open. Frame indices are plain ints."""

    kind: str
    first_detectable: int
    first_crisp: Optional[int] = None
    readable_blur: Optional[Tuple[int, int]] = None
    near_crisp: Optional[Tuple[int, int]] = None
    hold: Optional[Tuple[int, int]] = None
    exit_progressive_blur: Optional[Tuple[int, int]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "kind": self.kind,
            "first_detectable": self.first_detectable,
        }
        if self.first_crisp is not None:
            payload["first_crisp"] = self.first_crisp
        for name in ("readable_blur", "near_crisp", "hold", "exit_progressive_blur"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = list(value)
        return payload


@dataclass(frozen=True)
class ProgramState:
    id: str
    text: str
    start_frame: int
    end_frame_exclusive: int
    font_class: str
    behavior: str
    lifecycle: Lifecycle
    z: int
    proven_face: Optional[str] = None

    @property
    def frames(self) -> int:
        return self.end_frame_exclusive - self.start_frame

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "start_frame": self.start_frame,
            "end_frame_exclusive": self.end_frame_exclusive,
            "frames": self.frames,
            "font_class": self.font_class,
            "behavior": self.behavior,
            "lifecycle": self.lifecycle.to_dict(),
            "z": self.z,
            "proven_face": self.proven_face,
        }


@dataclass(frozen=True)
class StateProgram:
    states: Tuple[ProgramState, ...]
    frame_count: int
    raster: Tuple[int, int]
    fps: str
    blank_frames: Tuple[int, ...]
    authority_decoded_rgb24_sha256: str
    authority_shortcode: str
    render_allowed: bool
    sans_exact_face: Optional[str]
    script_exact_face: Optional[str]
    forbidden_treatments: Tuple[str, ...]
    source_interval_semantics: str = "inclusive"
    interval_semantics: str = "half_open"
    _by_id: Mapping[str, ProgramState] = field(default_factory=dict, repr=False)

    def state(self, state_id: str) -> ProgramState:
        try:
            return self._by_id[state_id]
        except KeyError:
            raise StateProgramError(f"no such state: {state_id!r}") from None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frame_count": self.frame_count,
            "raster": list(self.raster),
            "fps": self.fps,
            "blank_frames": list(self.blank_frames),
            "authority_decoded_rgb24_sha256": self.authority_decoded_rgb24_sha256,
            "authority_shortcode": self.authority_shortcode,
            "render_allowed": self.render_allowed,
            "sans_exact_face": self.sans_exact_face,
            "script_exact_face": self.script_exact_face,
            "forbidden_treatments": list(self.forbidden_treatments),
            "source_interval_semantics": self.source_interval_semantics,
            "interval_semantics": self.interval_semantics,
            "states": [state.to_dict() for state in self.states],
        }


def _assign_stack_slots(
    raw: Sequence[Tuple[str, int, int]]
) -> Dict[str, int]:
    """Greedy interval colouring: the lowest free stack slot among live neighbours.

    Concurrent captions must carry an explicit, distinct stacking order; non-overlapping
    states may reuse a slot because z is a stack position, not a global counter.
    Processing in (start, id) order makes earlier-entering lines sit lower in the stack.
    """
    assigned: Dict[str, int] = {}
    placed: List[Tuple[str, int, int]] = []
    for state_id, start, end in sorted(raw, key=lambda item: (item[1], item[0])):
        taken = {
            assigned[other_id]
            for other_id, other_start, other_end in placed
            if start < other_end and other_start < end
        }
        slot = 0
        while slot in taken:
            slot += 1
        assigned[state_id] = slot
        placed.append((state_id, start, end))
    return assigned


def _lifecycle_kind(state: Mapping[str, Any]) -> str:
    if "exit" in state and state["exit"].get("progressive_blur"):
        return "blur_to_crisp"
    behavior = state.get("behavior", "")
    if "accumulates" in behavior:
        return "build"
    if state["end"] == state["start"]:
        return "blink"
    return "hard_state"


def import_sealed_authority(authority: Mapping[str, Any]) -> StateProgram:
    """Build a state program from a sealed typography-authority contract.

    Importing a program never authorises a render; ``render_allowed`` is carried through
    from the authority's own gate and is not raised here under any circumstance.
    """
    for key in ("visual_states", "authority", "regression_gates"):
        if key not in authority:
            raise StateProgramError(f"sealed authority is missing {key!r}")

    video = authority["authority"].get("video")
    if not video:
        raise StateProgramError("sealed authority has no authority.video block")

    frame_count = int(video["frame_count"])
    raster = tuple(int(value) for value in video["raster"])
    if len(raster) != 2:
        raise StateProgramError(f"authority raster must be [w, h], got {video['raster']!r}")

    raw_states = authority["visual_states"]
    if not raw_states:
        raise StateProgramError("sealed authority declares no visual states")

    spans: List[Tuple[str, int, int]] = []
    for state in raw_states:
        for key in ("id", "text", "start", "end"):
            if key not in state:
                raise StateProgramError(f"visual state is missing {key!r}: {state!r}")
        start = int(state["start"])
        end_exclusive = int(state["end"]) + 1
        if start < 0:
            raise StateProgramError(f"state {state['id']}: negative start frame {start}")
        if end_exclusive <= start:
            raise StateProgramError(
                f"state {state['id']}: inclusive end {state['end']} precedes start {start}"
            )
        if end_exclusive > frame_count:
            raise StateProgramError(
                f"state {state['id']}: inclusive end {state['end']} runs past the "
                f"authority clock frame_count {frame_count} "
                f"(half-open end {end_exclusive} > {frame_count})"
            )
        spans.append((str(state["id"]), start, end_exclusive))

    seen: set[str] = set()
    for state_id, _, _ in spans:
        if state_id in seen:
            raise StateProgramError(f"duplicate state id {state_id}")
        seen.add(state_id)

    slots = _assign_stack_slots(spans)

    coverage = [0] * frame_count
    for _, start, end in spans:
        for frame in range(start, end):
            coverage[frame] += 1
    derived_blanks = tuple(
        index for index, count in enumerate(coverage) if count == 0
    )

    declared = authority["regression_gates"].get("authority", {}).get("blank_frames")
    if declared is not None:
        declared_blanks = tuple(int(frame) for frame in declared)
        if declared_blanks != derived_blanks:
            raise StateProgramError(
                "derived blank frames do not match the sealed gate table: derived "
                f"{list(derived_blanks)} vs declared {list(declared_blanks)}. The blank "
                "set is computed from state coverage, never asserted."
            )

    states: List[ProgramState] = []
    for state in raw_states:
        state_id = str(state["id"])
        start = int(state["start"])
        end_exclusive = int(state["end"]) + 1
        entry = state.get("entry", {})
        exit_block = state.get("exit", {})
        lifecycle = Lifecycle(
            kind=_lifecycle_kind(state),
            first_detectable=int(entry.get("first_detectable", start)),
            first_crisp=(
                int(entry["first_crisp"]) if "first_crisp" in entry else None
            ),
            readable_blur=(
                _half_open(entry["readable_blur"]) if "readable_blur" in entry else None
            ),
            near_crisp=(
                _half_open(entry["near_crisp"]) if "near_crisp" in entry else None
            ),
            hold=_half_open(state["hold"]) if "hold" in state else None,
            exit_progressive_blur=(
                _half_open(exit_block["progressive_blur"])
                if exit_block.get("progressive_blur")
                else None
            ),
        )
        states.append(
            ProgramState(
                id=state_id,
                text=str(state["text"]),
                start_frame=start,
                end_frame_exclusive=end_exclusive,
                font_class=str(state.get("font_class", "")),
                behavior=str(state.get("behavior", "")),
                lifecycle=lifecycle,
                z=slots[state_id],
                proven_face=None,
            )
        )

    forensics = authority.get("font_forensics", {})
    treatment = authority.get("treatment_contract", {})
    gate = authority.get("next_implementation_gate", {})

    program = StateProgram(
        states=tuple(states),
        frame_count=frame_count,
        raster=(raster[0], raster[1]),
        fps=str(video["fps"]),
        blank_frames=derived_blanks,
        authority_decoded_rgb24_sha256=str(video.get("decoded_rgb24_essence_sha256", "")),
        authority_shortcode=str(authority["authority"].get("shortcode", "")),
        render_allowed=bool(gate.get("render_allowed", False)),
        sans_exact_face=forensics.get("sans", {}).get("exact_face_name"),
        script_exact_face=forensics.get("script", {}).get("exact_face_name"),
        forbidden_treatments=tuple(treatment.get("forbidden", ())),
        _by_id={state.id: state for state in states},
    )
    return program
