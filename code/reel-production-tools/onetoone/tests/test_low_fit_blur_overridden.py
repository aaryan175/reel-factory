"""L0084: a device-curve frame fit below 0.7 with a blur above 3 px is not a measurement until the
reference frame has been read; an audit found 25 px smears where the reference is sharp."""
import json
from pathlib import Path

import pytest

from _fixtures import fixture

PROJECT_DEVICES = fixture('device-curve-project', 'brain', 'caption_devices.json')


def unchecked_low_fit_blurs(devices):
    bad = []
    for cid, parts in devices.get('states', {}).items():
        for word, st in parts.items():
            for e in st.get('curve') or []:
                sig = max(e.get('sigma_x') or 0, e.get('sigma_y') or 0)
                if (e.get('fit') or 1) < 0.7 and sig > 3 and not any(k.startswith('override') for k in e):
                    bad.append((cid, word, e.get('n'), sig, e.get('fit')))
    return bad


def test_detector_flags_a_low_fit_blur():
    d = {'states': {'c09': {"word": {'curve': [{'n': 61, 'fit': 0.6, 'sigma_x': 1.0, 'sigma_y': 25.0}]}}}}
    assert unchecked_low_fit_blurs(d) == [('c09', "word", 61, 25.0, 0.6)]
    d['states']['c09']["word"]['curve'][0]['override_r007'] = 'read the reference frame'
    assert unchecked_low_fit_blurs(d) == []


@pytest.mark.skipif(not PROJECT_DEVICES.exists(), reason='optional project fixture not present (REEL_FACTORY_FIXTURES)')
def test_project_brain_has_no_unchecked_low_fit_blur():
    assert unchecked_low_fit_blurs(json.load(open(PROJECT_DEVICES))) == []
