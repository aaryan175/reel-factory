"""Beat taxonomy over the footage library (FOOTAGE_LIBRARY.json): a first-pass classifier from clip tags.

Beat codes are neutral shot categories; the result (beats_auto.json) is meant to be verified by eye and saved as
beats_verified.json before the renderer uses it.
"""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import LIB_JSON, WB  # noqa: E402

BEATS = [  # (beat, regex over notes+verbs) - order = priority
 ('WORKSPACE', r'desk|keyboard|monitor|computer|screen|typing|notebook'),
 ('KITCHEN',   r'kitchen|counter|stove|kettle|\bcup\b|mug|coffee|tea'),
 ('FOOD',      r'\beats?\b|eating|plate|bowl|meal|restaurant|menu'),
 ('TRANSIT',   r'\bcar\b|\bbus\b|train|bicycle|driving|riding'),
 ('STREET',    r'street|market|shop|crossing|road|sidewalk'),
 ('ACTIVITY',  r'walks?\b|walking|runs?\b|running|stretch|exercise'),
 ('EXTERIOR',  r'outside|outdoor|park|garden|sky|exterior'),
 ('INTERIOR',  r'room|interior|hallway|stairs|lounge|living'),
 ('WIDE',      r'\bwide\b|establishing|landscape'),
]


def load():
    return json.load(open(LIB_JSON))['clips']


def classify(c):
    t = c.get('tags') or {}
    txt = (t.get('notes', '') + ' ' + ' '.join(t.get('action_verbs', []))).lower()
    hits = [b for b, rx in BEATS if re.search(rx, txt)]
    night = bool(re.search(r'night|dusk|dark|lamp-lit|candle|neon', txt)) or 'night' in str(t.get('world_cluster') or '')
    return hits, night


if __name__ == '__main__':
    import collections
    d = load(); cnt = collections.Counter(); out = {}
    for cid, c in d.items():
        h, n = classify(c); out[cid] = {'beats': h, 'night': n}
        cnt[(h[0] if h else 'NONE') + ('/N' if n else '')] += 1
    os.makedirs(WB, exist_ok=True)
    json.dump(out, open(os.path.join(WB, 'beats_auto.json'), 'w'), indent=0)
    for k, v in sorted(cnt.items()): print(f'{k:14s}{v}')
