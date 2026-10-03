"""When a reference's caption letters are FOOTAGE-FILLED (a still picture seen through the glyphs),
drawing them as one flat ink per word is wrong. A part whose reference-pixel fit scores below 0.6 is the
tell: a single-ink mask sees only some of its letters. Such a part may not ship as flat ink until
its fill has been measured (`ink_fill`) or the reference frame was read and the flat ink ruled right
(`flat_ink_checked`)."""
import json
from pathlib import Path

import pytest

from _fixtures import fixture

PROJECT_BRAIN = fixture('footage-fill-project', 'brain', 'captions.plaintext.json')


def flat_low_fit_parts(brain):
    bad = []
    for st in brain.get('states', []):
        for p in st.get('parts', []):
            sc = (p.get('ref_fit') or {}).get('score')
            if sc is not None and sc < 0.6 and not p.get('ink_fill') and not p.get('flat_ink_checked'):
                bad.append((st.get('id'), p.get('text'), sc))
    return bad


def test_detector_flags_a_low_fit_flat_part():
    b = {'states': [{'id': 'hard', 'parts': [{'text': 'hard', 'ref_fit': {'score': 0.53}, 'ink_rgb': [111, 34, 46]}]}]}
    assert flat_low_fit_parts(b) == [('hard', 'hard', 0.53)]
    b['states'][0]['parts'][0]['ink_fill'] = {'device': 'footage_fill'}
    assert flat_low_fit_parts(b) == []


def test_a_good_fit_is_not_flagged():
    b = {'states': [{'id': 'yeah', 'parts': [{'text': 'yeah', 'ref_fit': {'score': 0.91}}]}]}
    assert flat_low_fit_parts(b) == []


@pytest.mark.skipif(not PROJECT_BRAIN.exists(), reason='optional project fixture not present (REEL_FACTORY_FIXTURES)')
def test_project_brain_has_no_flat_low_fit_part():
    assert flat_low_fit_parts(json.load(open(PROJECT_BRAIN))) == []
