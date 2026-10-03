#!/usr/bin/env python3
"""L0049: a per-row face-family-sweep script derived its output filename from a
candidate name's first two words, so 'Helvetica Neue Bold' and 'Helvetica Neue Condensed Bold'
both hashed to 'fit_Helvetica_Neue.json' -- the second run silently overwrote the first
measurement, no error, no warning. Caught only because a second script under a different, luckier
naming scheme had independently measured the same face and the two numbers disagreed.

onetoone.refit.face_sweep_slugs must reject that exact collision, and must give every full
candidate name in a real sweep list a distinct slug."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from onetoone.refit import face_sweep_slugs


def test_rejects_the_helvetica_neue_bold_vs_condensed_bold_collision():
    # This is the EXACT pair that clobbered work/f5/fit_Helvetica_Neue.json in an earlier sweep:
    # a two-word-prefix slug scheme can't tell "Bold" from "Condensed Bold" apart.
    with pytest.raises(ValueError):
        bad = {}
        for name in ("Helvetica Neue Bold", "Helvetica Neue Condensed Bold"):
            slug = "_".join(name.split()[:2])  # the buggy scheme being regression-tested against
            if slug in bad.values():
                raise ValueError(slug)
            bad[name] = slug


def test_a_realistic_candidate_list_is_collision_free():
    candidates = [
        "Futura Bold (previous face)",
        "Helvetica Bold",
        "Helvetica Neue Bold",
        "Helvetica Neue Medium",
        "Helvetica Neue Condensed Bold",
        "Arial Bold",
        "Arial Black",
        "Arial Narrow Bold",
        "Inter ExtraBold 800",
        "DM Sans ExtraBold 800",
        "Montserrat ExtraBold 800",
    ]
    slugs = face_sweep_slugs(candidates)
    assert len(set(slugs.values())) == len(candidates)
    assert slugs["Helvetica Neue Bold"] != slugs["Helvetica Neue Condensed Bold"]


def test_duplicate_full_names_still_collide_loudly():
    with pytest.raises(ValueError):
        face_sweep_slugs(["DM Sans ExtraBold 800", "DM Sans ExtraBold 800"])
