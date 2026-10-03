"""L0066: captions_typeset.readability looks a part's ink up BY ITS TEXT,
so two parts in one state with the same text (a white L and a red L in a per-glyph line) are
both judged with the last one's ink, and the gate fails a white letter as if it were red.
Any study that stores repeated texts in one state must split them into sub-states."""
import json
from pathlib import Path

import pytest

from onetoone.captions_typeset import readability, typeset_state
from _fixtures import fixture


def _states(brain: Path):
    return json.loads((brain / "captions.plaintext.json").read_text())["states"]


def test_readability_keys_parts_by_text_so_repeated_texts_collide():
    from PIL import Image
    white = {"part": "plain_line", "text": "L", "words": ["L"], "bbox_settled": [100, 100, 160, 200],
             "ink_rgb": [253, 253, 253]}
    red = dict(white, bbox_settled=[300, 100, 360, 200], ink_rgb=[237, 25, 26])
    st = {"id": "t", "in": 0, "out": 1, "parts": [white, red], "shadow": False}
    layer, rep = typeset_state(st, canvas=(480, 270))
    bed = Image.new("RGBA", (480, 270), (20, 20, 20, 255))
    r = readability(bed, rep, st)
    # both parts are judged with the RED ink (the last part with that text) - the collision
    assert len({round(p["contrast"], 1) for p in r["parts"]}) == 1


@pytest.mark.parametrize("brain", [fixture("per-glyph-project", "brain")])
def test_no_state_repeats_a_part_text(brain):
    if not (brain / "captions.plaintext.json").exists():
        pytest.skip(f"optional project fixture not present: {brain}")
    for st in _states(brain):
        texts = [p.get("text") for p in st["parts"]]
        assert len(texts) == len(set(texts)), f"{st['id']} repeats a part text: {texts}"
