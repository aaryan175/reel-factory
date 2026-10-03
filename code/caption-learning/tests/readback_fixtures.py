"""Helpers for the read-back / word-truth tests.

Deliberately NOT conftest.py: `tests/conftest.py` is shared with the other
caption-learning modules being built in parallel, so these helpers live in their
own importable module and the pytest fixtures are declared inside each test file.

Requires numpy, Pillow, pytest and ffmpeg on PATH (or $FFMPEG).
"""

from __future__ import annotations

import os
import subprocess
import sys

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cl_paths import FFMPEG  # noqa: E402
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

# Real corpus artefacts used for calibration-pinning tests.  Tests that need them
# SKIP (never silently pass) when the workbench volume is not mounted.
from cl_paths import WB  # noqa: E402

# Optional local corpus: <CAPTION_TEST_CORPUS>/<ref>/{plates/,contract.json,
# delivered.mp4,reference.mp4}.  Not shipped with the repo; tests that need it skip.
CORPUS = os.path.expanduser(os.environ.get("CAPTION_TEST_CORPUS")
                            or os.path.join(WB, "caption-corpus"))
REFA_PLATES = os.path.join(CORPUS, "refA", "plates")
REFA_CONTRACT = os.path.join(CORPUS, "refA", "contract.json")
REFA_DELIVERED = os.path.join(CORPUS, "refA", "delivered.mp4")
REFA_REFERENCE = os.path.join(CORPUS, "refA", "reference.mp4")
REFC_CONTRACT = os.path.join(CORPUS, "refC", "contract.json")
REFC_REFERENCE = os.path.join(CORPUS, "refC", "reference.mp4")
REFC_DELIVERED = os.path.join(CORPUS, "refC", "delivered.mp4")
REFD_CONTRACT = os.path.join(CORPUS, "refD", "contract.json")
# bbox of the measured ground-truth word state in the refA corpus (if present)
WORD_BOX = [371, 142, 910, 361]


def render_word(path, text, size=None, px=140, ink=(0, 0, 0),
                bg=(255, 255, 255), font_path=FONT_PATH, margin=0.35):
    """Render `text` with PIL as an exact, uncorrupted word plate.

    The canvas is sized to the glyphs plus a margin unless `size` is forced —
    a clipped render is a DIFFERENT defect (and readback correctly fails it),
    so the "exact render" fixture must not accidentally produce one.
    """
    f = ImageFont.truetype(font_path, px)
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    box = probe.textbbox((0, 0), text, font=f)
    tw, th = box[2] - box[0], box[3] - box[1]
    if size is None:
        size = (int(tw + 2 * margin * px) or 1, int(th + 2 * margin * px) or 1)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text(((size[0] - tw) / 2 - box[0], (size[1] - th) / 2 - box[1]),
           text, font=f, fill=ink)
    img.save(path)
    return path


def render_noise(path, size=(640, 240), seed=7):
    """Structured garbage: no glyphs, plenty of 'ink' pixels."""
    rs = np.random.RandomState(seed)
    a = rs.randint(0, 256, (size[1], size[0]), dtype=np.uint8)
    a = np.repeat(np.repeat(a[::8, ::8], 8, 0), 8, 1)[:size[1], :size[0]]
    Image.fromarray(a).convert("RGB").save(path)
    return path


def render_blank(path, size=(640, 240), level=0):
    """A plate with ZERO ink — the UNMEASURABLE case."""
    Image.new("L", size, level).save(path)
    return path


def make_video(path, frames, fps=25):
    """Encode PIL images into an mp4 in zero-based decoded order."""
    d = os.path.join(os.path.dirname(path), "_vsrc_" + os.path.basename(path))
    os.makedirs(d, exist_ok=True)
    for i, im in enumerate(frames):
        im.convert("RGB").save(os.path.join(d, "%05d.png" % i))
    r = subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-framerate", str(fps),
         "-start_number", "0", "-i", os.path.join(d, "%05d.png"),
         "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", path],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return path


def corpus_expect(ref):
    """Load <CORPUS>/<ref>/expect.json (the corpus owner's ground truth), or skip.

    Keys used by the corpus-pinned tests (all optional per test):
      rejected_states   list of state ids a reviewer rejected for damaged words
      accepted_states   list of state ids a reviewer accepted
      rejected_word     the declared word of the rejected states
      plate_words       {state_id: declared text} for accepted plates
      probe_frames      frame indices inside the rejected word's span
      reader_limited_state  a state whose reference crop the OCR cannot read
    """
    p = os.path.join(CORPUS, ref, "expect.json")
    if not os.path.exists(p):
        pytest.skip("corpus expectations missing (%s): %s" % (ref, p))
    import json
    return json.load(open(p))


def require(path, what):
    if not os.path.exists(path):
        pytest.skip("corpus artefact missing (%s): %s" % (what, path))
    return path
