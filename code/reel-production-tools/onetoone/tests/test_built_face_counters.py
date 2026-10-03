"""L0067: a face built with `faceid trace` inherits whatever the subject or a
bright background did to the traced instance. A traced heavy D and O, taken from a frame where the
subject's head cut their ink, set as an open 'n'; a traced medium B lost its lower bowl to a
bokeh light and set as an R. The proof fit and the line scores did not catch it; the LOOK sheet did.
Checkable rule: a closed-counter letter in a built face keeps its counters (O/D one, B two)."""
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

COUNTERS = {"O": 1, "D": 1, "B": 2, "A": 1, "P": 1, "R": 1,
            # lowercase: single-storey g and a two-storey a
            # each keep one closed counter, like o b d e p
            "o": 1, "b": 1, "d": 1, "e": 1, "p": 1, "a": 1, "g": 1}
# every face traced from a reference (Ref*-<Weight>.ttf) in the configured traced-face directory.
# Traced faces are analysis artefacts kept in a work directory; they are never installed as fonts.
_TRACED_ENV = os.environ.get("REEL_FACTORY_TRACED_FONTS")
TRACED_DIR = Path(_TRACED_ENV).expanduser() if _TRACED_ENV else None
FACES = sorted(TRACED_DIR.glob("Ref*-*.ttf")) if TRACED_DIR and TRACED_DIR.is_dir() else []


def _holes(face: Path, ch: str) -> int:
    font = ImageFont.truetype(str(face), 300)
    im = Image.new("L", (400, 500), 0)
    ImageDraw.Draw(im).text((50, 50), ch, font=font, fill=255)
    ink = np.asarray(im) > 128
    lab, n = ndimage.label(~ink)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
    sizes = ndimage.sum(np.ones_like(lab), lab, range(1, n + 1))
    return sum(1 for i, s in enumerate(sizes, 1) if i not in border and s > 150)


def _has(face: Path, ch: str) -> bool:
    from fontTools.ttLib import TTFont
    return ord(ch) in TTFont(str(face)).getBestCmap()


@pytest.mark.parametrize("face", FACES, ids=[f.stem for f in FACES])
def test_closed_letters_keep_their_counters(face):
    if not FACES:
        pytest.skip("no built faces on this machine")
    bad = {ch: _holes(face, ch) for ch, need in COUNTERS.items() if _has(face, ch) and _holes(face, ch) < need}
    assert not bad, f"{face.name}: letters lost their counters (re-trace from a clean instance): {bad}"
