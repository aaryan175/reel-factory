"""Example DITL shot templates (synthetic, neutral).

Each shot: beats (length in music beats), beat codes (in preference order, see beats.py),
on-screen time, on-screen label, night flag, and an optional hook (first shot only).
These are deliberately small examples; add your own templates to TEMPLATES.
"""


def S(beats, codes, time=None, label=None, night=False, hook=None):
    return dict(beats=beats, beat=codes if isinstance(codes, list) else [codes], time=time, label=label, night=night, hook=hook)


STUDIO_DAY = [
    S(4, ['EXTERIOR', 'WIDE']),                              # hook card text filled per variant
    S(2, ['INTERIOR', 'KITCHEN'], '9:00 AM', 'MORNING'),
    S(2, 'WORKSPACE', '9:30 AM', 'WORK BLOCK'),
    S(2, ['FOOD', 'KITCHEN'], '12:30 PM', 'LUNCH'),
    S(2, ['STREET', 'TRANSIT'], '3:00 PM', 'ERRANDS'),
    S(2, ['NIGHT_INTERIOR', 'NIGHT_EXTERIOR'], '7:00 PM', 'EVENING', night=True),
]

MORNING = [
    S(4, ['EXTERIOR', 'WIDE']),
    S(2, 'INTERIOR', '8:00 AM', 'MORNING'),
    S(2, ['KITCHEN', 'FOOD'], '8:15 AM', 'BREAKFAST'),
    S(2, 'WORKSPACE', '9:00 AM', 'WORK BLOCK'),
]

EVENING = [
    S(4, ['NIGHT_EXTERIOR'], night=True),
    S(2, 'NIGHT_INTERIOR', '6:30 PM', 'EVENING', night=True),
    S(2, ['FOOD', 'KITCHEN'], '7:30 PM', 'DINNER', night=True),
    S(2, 'NIGHT_INTERIOR', '10:00 PM', 'WIND DOWN', night=True),
]

TEMPLATES = dict(STUDIO_DAY=STUDIO_DAY, MORNING=MORNING, EVENING=EVENING)
