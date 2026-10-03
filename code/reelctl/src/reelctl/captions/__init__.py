"""Unified caption/typography engine.

One engine for every reel: EXTRACT a caption contract from a hash-locked reference,
RENDER it (source-contour by default, native-font only on proof), QC the render against
the reference's own frames. Per-reel caption scripts are not permitted.

The doctrine and its lesson citations live in ``DOCTRINE.md`` beside this file.
"""

from __future__ import annotations

from .contract import CaptionContractError, validate_caption_contract
from .fontproof import (
    FontProofError,
    HoldoutSplit,
    HoldoutVerdict,
    evaluate_font_hypothesis,
    split_words,
)
from .program import (
    Lifecycle,
    ProgramState,
    StateProgram,
    StateProgramError,
    import_sealed_authority,
)
from .render import RenderError, render_caption_sequence, sequence_summary

__all__ = [
    "CaptionContractError",
    "FontProofError",
    "HoldoutSplit",
    "HoldoutVerdict",
    "Lifecycle",
    "ProgramState",
    "RenderError",
    "StateProgram",
    "StateProgramError",
    "evaluate_font_hypothesis",
    "import_sealed_authority",
    "render_caption_sequence",
    "sequence_summary",
    "split_words",
    "validate_caption_contract",
]
