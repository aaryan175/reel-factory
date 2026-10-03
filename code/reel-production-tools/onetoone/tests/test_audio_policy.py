"""Audio policy: the default is a licensed track the user supplies.

The reference is analysed for timing either way; muxing the reference's own audio stream is an
explicit opt-in (`reference_audio_rights_held=True`) for users who hold the rights to it.
"""
from __future__ import annotations

import pytest


def test_render_refuses_without_a_licensed_track(tmp_path, monkeypatch):
    from onetoone import render as r
    monkeypatch.setenv("REEL_NO_GATE", "1")
    cast = tmp_path / "cast.json"
    cast.write_text('{"slots": []}')
    with pytest.raises(r.LicensedAudioRequired) as err:
        r.render(tmp_path, cast, tmp_path / "ref.mp4", tmp_path / "o.mp4", tmp_path / "work", gate=False)
    assert "--audio" in str(err.value) and "rights" in str(err.value)


def test_render_refuses_a_missing_track_file(tmp_path, monkeypatch):
    from onetoone import render as r
    monkeypatch.setenv("REEL_NO_GATE", "1")
    cast = tmp_path / "cast.json"
    cast.write_text('{"slots": []}')
    with pytest.raises(r.LicensedAudioRequired):
        r.render(tmp_path, cast, tmp_path / "ref.mp4", tmp_path / "o.mp4", tmp_path / "work", gate=False,
                 audio=tmp_path / "missing.m4a")


def test_cli_exposes_the_licensed_track_and_the_explicit_opt_in():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "render.py").read_text()
    assert '"--audio"' in src
    assert '"--reference-audio-rights-held"' in src
