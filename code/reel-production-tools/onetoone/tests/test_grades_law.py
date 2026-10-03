"""L0065: the Deck grader's grades.jsonl is casting law.
NEVER bans everywhere; HERO/ME settle a stem for identity slots; BROLL/NOTME never sit on an identity slot;
latest line per field wins and a null never erases; refpeople turns a person in the reference into an
identity slot. Synthetic files only: no footage, no ffmpeg, no Vision."""
import json

import pytest

from onetoone import castscan, grades as gr, identity as idn, refpeople as rp


# The shipped identity_pool.json ships empty; these tests run on a synthetic pool.
SYNTH_POOL = {
    "schema_version": 1, "law": "synthetic test pool",
    "settled_pool": ["CLIP_0065", "CLIP_0067"], "settled_pool_sets": {},
    "blacklist_full": [{"stem": "CLIP_0061", "kind": "FULL_BAN", "match_keys": ["b" * 64], "ruling": "never use"}],
    "blacklist_spans": [], "unruled": [],
}


@pytest.fixture(autouse=True)
def synth_pool(tmp_path, monkeypatch):
    p = tmp_path / "identity_pool.json"
    p.write_text(json.dumps(SYNTH_POOL))
    monkeypatch.setattr(idn, "POOL_PATH", p)
    idn._load.cache_clear()
    yield
    idn._load.cache_clear()


def _lines(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_latest_wins_per_field_and_null_never_erases(tmp_path):
    p = _lines(tmp_path / "g.jsonl", [
        {"at": "1", "stem": "CLIP_0062", "grade": "BROLL", "ident": None},
        {"at": "2", "stem": "/x/masters/abcd__CLIP_0062.MP4", "grade": None, "ident": "ME"},
        {"at": "3", "stem": "CLIP_0062", "grade": "HERO", "ident": None},
        {"at": "4", "stem": "CLIP_0063", "grade": "NEVER"},
        {"at": "5", "stem": "CLIP_0064", "ident": "NOTME"},
        "not json at all",
    ][:5] + [{"stem": ""}])
    g = gr.load_grades(p)
    assert g["CLIP_0062"] == {"grade": "HERO", "ident": "ME", "at": "3", "clip_id": None, "windows": {}}
    assert gr.hero_stems(g) == {"CLIP_0062"} and gr.never_stems(g) == {"CLIP_0063"}
    assert gr.operator_stems(g) == {"CLIP_0062"} and gr.not_operator_stems(g) == {"CLIP_0064"}
    assert gr.effective_settled(["CLIP_0065", "CLIP_0063"], g) == {"CLIP_0065", "CLIP_0062"}
    assert gr.load_grades(tmp_path / "missing.jsonl") == {}


def _cast(tmp_path, slots):
    row = tmp_path / "reel-test"
    (row / "deliver").mkdir(parents=True); (row / "brain").mkdir()
    p = row / "deliver" / "cast_vtest.json"
    p.write_text(json.dumps({"version": "vtest", "grade_mode": "house", "slots": slots}))
    return p


def test_grades_are_identity_law(tmp_path):
    g = gr.load_grades(_lines(tmp_path / "g.jsonl", [
        {"stem": "CLIP_0062", "grade": "HERO"},
        {"stem": "CLIP_0063", "grade": "NEVER"},
        {"stem": "CLIP_0064", "grade": "BROLL"},
        {"stem": "CLIP_0066", "ident": "NOTME"},
        {"stem": "CLIP_0065", "grade": "BROLL"},        # a settled-pool stem graded b-roll
    ]))
    cast = _cast(tmp_path, [
        {"slot": "S00", "stem": "CLIP_0062", "in_s": 1.0, "identity": "subject"},   # HERO: settled by grade
        {"slot": "S01", "stem": "CLIP_0063", "in_s": 1.0, "identity": "none"},       # NEVER: banned anywhere
        {"slot": "S02", "stem": "CLIP_0064", "in_s": 1.0, "identity": "subject"},   # BROLL on an identity slot
        {"slot": "S03", "stem": "CLIP_0066", "in_s": 1.0, "identity": "subject"},   # NOTME on an identity slot
        {"slot": "S04", "stem": "CLIP_0065", "in_s": 1.0, "identity": "subject"},   # settled but graded BROLL
        {"slot": "S05", "stem": "CLIP_0064", "in_s": 1.0, "identity": "none"},       # BROLL where nobody is: fine
    ])
    fails = idn.check_cast(cast, grades=g)
    names = [f.split(":")[0] for f in fails]
    assert "S00" not in names and "S05" not in names
    assert any(f.startswith("S01:") and "NEVER" in f for f in fails)
    assert any(f.startswith("S02:") and "B-ROLL" in f for f in fails)
    assert any(f.startswith("S03:") and "NOT ME" in f for f in fails)
    assert any(f.startswith("S04:") and "B-ROLL" in f for f in fails)


def test_refpeople_window_verdict_and_preflight_rule(tmp_path):
    data = {"step": 4, "min_face_h": 0.06, "min_fraction": 1 / 3,
            "faces": {"0": [0.2], "4": [0.21], "8": [], "12": [0.3], "16": [], "20": [], "24": [0.02], "28": []}}
    assert rp.window_verdict(data, 0, 12)["person"] is True
    assert rp.window_verdict(data, 16, 28)["person"] is False          # one tiny face only
    assert rp.window_verdict(data, 13, 15)["sampled"] == 1               # shorter than a step: nearest sample
    cast = _cast(tmp_path, [
        {"slot": "S00", "stem": "CLIP_0065", "in_s": 1.0, "identity": "none"},        # person shot cast as nobody
        {"slot": "S01", "stem": "CLIP_0067", "in_s": 1.0, "identity": "subject"},    # person shot, settled stem
        {"slot": "S02", "stem": "CLIP_0068", "in_s": 1.0, "identity": "none"},                   # empty shot
    ])
    brain = cast.parent.parent / "brain"
    (brain / "refpeople.json").write_text(json.dumps(data))
    (brain / "cutgrid.json").write_text(json.dumps({"shots": [{"slot": "S00", "in": 0, "out": 12}, {"slot": "S01", "in": 0, "out": 12},
                                                              {"slot": "S02", "in": 16, "out": 28}]}))
    fails = idn.check_cast(cast, grades={})
    assert [f.split(":")[0] for f in fails] == ["S00"] and "declare identity subject" in fails[0]
    # block schema: picture_blocks with P ids, and a slot that carries its own frames
    (brain / "cutgrid.json").write_text(json.dumps({"picture_blocks": [{"id": "P1", "frames": [0, 12]}]}))
    cast2 = _cast(tmp_path / "b", [{"slot": "P1", "stem": "CLIP_0068", "in_s": 1.0, "identity": "none"},
                                   {"slot": "X", "stem": "CLIP_0068", "ref_in": 16, "ref_out": 28, "in_s": 1.0, "identity": "none"}])
    b2 = cast2.parent.parent / "brain"
    (b2 / "refpeople.json").write_text(json.dumps(data)); (b2 / "cutgrid.json").write_text(json.dumps({"picture_blocks": [{"id": "P1", "frames": [0, 12]}]}))
    fails = idn.check_cast(cast2, grades={})
    assert [f.split(":")[0] for f in fails] == ["P1"]
    # no refpeople.json = warning only, never a block
    (b2 / "refpeople.json").unlink()
    assert idn.check_cast(cast2, grades={}) == []


def test_castscan_allowed_stems_filters_and_orders(tmp_path):
    g = gr.load_grades(_lines(tmp_path / "g.jsonl", [
        {"stem": "CLIP_0062", "grade": "HERO"}, {"stem": "CLIP_0063", "grade": "NEVER"},
        {"stem": "CLIP_0064", "grade": "BROLL"}, {"stem": "CLIP_0065", "grade": "BROLL"},
    ]))
    stems = ["CLIP_0064", "CLIP_0063", "CLIP_0065", "CLIP_0067", "CLIP_0062", "CLIP_0061"]
    free = castscan.allowed_stems(stems, identity=False, grades=g)
    assert "CLIP_0063" not in free and "CLIP_0061" not in free and "CLIP_0064" in free
    ident = castscan.allowed_stems(stems, identity=True, grades=g)
    assert ident[0] == "CLIP_0062"                       # HERO first
    assert "CLIP_0067" in ident                           # settled pool still allowed
    assert "CLIP_0065" not in ident and "CLIP_0064" not in ident


def test_segment_grades_rule_their_windows(tmp_path):
    """Deck grader segments (t0/t1 on the line): a NEVER piece bans only its window, a HERO piece settles only
    its window for identity slots, a whole-clip line and a segment line coexist, castscan keeps identity
    in-points inside HERO pieces."""
    g = gr.load_grades(_lines(tmp_path / "g.jsonl", [
        {"stem": "CLIP_0069", "grade": "HERO", "t0": 0.0, "t1": 5.0},
        {"stem": "CLIP_0069", "grade": "NEVER", "t0": 60.0, "t1": 65.0},
        {"stem": "CLIP_0069", "grade": "BROLL", "t0": 5.0, "t1": 10.0},
        {"stem": "CLIP_0070", "grade": "HERO", "t0": None, "t1": None},
        {"stem": "CLIP_0070", "grade": "NEVER", "t0": 10.0, "t1": 15.0},
    ]))
    v = g["CLIP_0069"]
    assert v["grade"] is None and v["windows"][(0.0, 5.0)] == "HERO" and v["windows"][(60.0, 65.0)] == "NEVER"
    assert "CLIP_0069" in gr.hero_stems(g) and "CLIP_0069" in gr.operator_stems(g)
    assert "CLIP_0069" not in gr.never_stems(g)                     # whole clip is not banned
    assert gr.window_grade("CLIP_0069", 2.0, 4.0, g) == "HERO"
    assert gr.window_grade("CLIP_0069", 58.0, 62.0, g) == "NEVER"    # touches the NEVER piece
    assert gr.window_grade("CLIP_0069", 30.0, None, g) is None
    assert gr.never_reason("CLIP_0069", 61.0, None, g) and gr.never_reason("CLIP_0069", None, None, g)
    assert gr.never_reason("CLIP_0069", 2.0, 4.0, g) is None
    assert gr.identity_slot_reason("CLIP_0069", g, 2.0, 4.0) is None
    assert "B-ROLL" in gr.identity_slot_reason("CLIP_0069", g, 6.0, 8.0)
    assert "not inside a HERO" in gr.identity_slot_reason("CLIP_0069", g, 30.0, 32.0)
    assert "no in-point" in gr.identity_slot_reason("CLIP_0069", g)
    # whole-clip HERO + one NEVER piece: allowed everywhere except that piece
    assert gr.identity_slot_reason("CLIP_0070", g, 30.0, 32.0) is None
    assert "NEVER" in gr.identity_slot_reason("CLIP_0070", g, 12.0, 14.0)
    assert gr.hero_windows("CLIP_0069", g) == [(0.0, 5.0)] and gr.hero_windows("CLIP_0070", g) == []
    # the identity law reads the slot window
    cast = _cast(tmp_path, [
        {"slot": "S00", "stem": "CLIP_0069", "in_s": 1.0, "dur_s": 2.0, "identity": "subject"},
        {"slot": "S01", "stem": "CLIP_0069", "in_s": 61.0, "dur_s": 2.0, "identity": "none"},
        {"slot": "S02", "stem": "CLIP_0069", "in_s": 30.0, "dur_s": 2.0, "identity": "subject"},
        {"slot": "S03", "stem": "CLIP_0069", "in_s": 30.0, "dur_s": 2.0, "identity": "none"},
    ])
    fails = idn.check_cast(cast, grades=g)
    assert sorted(f.split(":")[0] for f in fails) == ["S01", "S02"]
    assert gr.summary(g)["segments_graded"] == 4


def test_measure_devices_counts_the_reference_frames(tmp_path):
    """The measurer once defaulted to a fixed 193 frames and left later states unmeasured.
    The frame count comes from the ref-NNN.png files on disk."""
    from onetoone import measure_devices as md
    for i in range(282):
        (tmp_path / f"ref-{i:03d}.png").write_bytes(b"")
    assert md.count_refframes(tmp_path) == 282
    assert md.count_refframes(tmp_path / "nowhere") == 0
