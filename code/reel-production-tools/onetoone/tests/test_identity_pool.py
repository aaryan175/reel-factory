"""L0052: the identity pool and the shot blacklist are code, not prose.
The pool must load and be internally consistent; a "not the protagonist" clip and a full-ban clip must be
rejected by every identifier; a settled stem must pass; a span inside a banned window must fail; and
preflight must run the check only when a cast is given. Synthetic pool, synthetic casts, no footage, no ffmpeg.

onetoone/identity_pool.json ships empty. These tests point POOL_PATH at a
synthetic pool with the same shape, so the law is proven without any real clip numbers."""
import json

import pytest

from onetoone import identity as idn
from onetoone import preflight

SHA_A = "a" * 64
SHA_B = "b" * 64

SYNTH_POOL = {
    "schema_version": 1,
    "law": "synthetic test pool",
    "settled_pool": ["CLIP_0065", "CLIP_0066", "CLIP_0067", "CLIP_0069"],
    "settled_pool_sets": {"identity_set_a": ["CLIP_0065", "CLIP_0066"], "identity_set_b": ["CLIP_0067", "CLIP_0069"]},
    "operator_confirmed": ["CLIP_0065", "CLIP_0066", "CLIP_0067", "CLIP_0069"],
    "not_operator": ["CLIP_0072"],
    "blacklist_full": [
        {"stem": "CLIP_0072", "kind": "FULL_BAN", "match_keys": [SHA_A], "ruling": "not the protagonist"},
        {"stem": "CLIP_0061", "kind": "FULL_BAN", "match_keys": [SHA_B], "ruling": "review: never use this clip"},
    ],
    "blacklist_spans": [
        {"stem": "CLIP_0074", "banned_windows_s": [[0.0, 8.0]], "reason": "excluded window: first 8 s"},
        {"stem": "CLIP_0075", "banned_windows_s": [[0.0, 10.5], [11.5, 12.0], [14.3, 16.2], [17.5, 99.0]],
         "reason": "allowed only 10.5-11.5, 12.0-14.3, 16.2-17.5"},
    ],
    "unruled": [{"stem": "CLIP_0003", "why": "never ruled on by number"}],
    "sources": {k: "synthetic" for k in ("settled_pool", "operator_confirmed", "blacklist_full", "blacklist_spans")},
}


@pytest.fixture(autouse=True)
def synth_pool(tmp_path, monkeypatch):
    p = tmp_path / "identity_pool.json"
    p.write_text(json.dumps(SYNTH_POOL))
    monkeypatch.setattr(idn, "POOL_PATH", p)
    idn._load.cache_clear()
    # hermetic: live Deck grades must not reject stems for a different reason
    from onetoone import grades as _g
    empty = tmp_path / "grades.jsonl"
    empty.write_text("")
    monkeypatch.setattr(_g, "GRADES_PATH", empty)
    yield p
    idn._load.cache_clear()


def _cast(tmp_path, slots, **top):
    p = tmp_path / "cast_test.json"
    p.write_text(json.dumps({"version": "vtest", "grade_mode": "house", "slots": slots, **top}))
    return p


def test_shipped_pool_is_empty_and_well_formed():
    shipped = json.loads(open(idn.Path(idn.__file__).with_name("identity_pool.json")).read())
    for key in ("settled_pool", "blacklist_full", "blacklist_spans"):
        assert shipped[key] == []


def test_pool_loads_and_is_consistent():
    pool = idn.load_pool()
    assert len(pool["settled_pool"]) == 4 == len(set(pool["settled_pool"]))
    assert sum(len(v) for v in pool["settled_pool_sets"].values()) == 4
    assert len(pool["blacklist_full"]) == 2 and len(pool["blacklist_spans"]) == 2
    assert [u["stem"] for u in pool["unruled"]] == ["CLIP_0003"]
    assert "CLIP_0003" not in pool["settled_pool"]
    assert not set(pool["settled_pool"]) & {e["stem"] for e in pool["blacklist_full"]}
    assert not set(pool["settled_pool"]) & set(pool["not_operator"])
    for key in ("settled_pool", "operator_confirmed", "blacklist_full", "blacklist_spans"):
        assert key in pool["sources"]


def test_banned_clips_are_rejected_by_every_identifier():
    for stem in ("CLIP_0072", "CLIP_0061"):
        assert idn.is_blacklisted(stem)
        assert idn.is_blacklisted(stem + ".MP4")
        assert idn.is_blacklisted("/media/x/masters/0123456789abcdef__" + stem + ".MP4")
        entry = next(e for e in idn.load_pool()["blacklist_full"] if e["stem"] == stem)
        assert entry["kind"] == "FULL_BAN"
        sha = next(k for k in entry["match_keys"] if len(k) == 64)
        assert idn.is_blacklisted(sha)                     # a ban entry with no match_keys misses renamed copies
    assert not idn.is_blacklisted("0061")                  # no substring matching (hash false-positive trap)


def test_cast_using_a_banned_clip_raises(tmp_path):
    for stem in ("CLIP_0072", "CLIP_0061"):
        cast = _cast(tmp_path, [{"slot": "S01", "stem": stem, "in_s": 1.0, "identity": "subject"}])
        with pytest.raises(idn.IdentityViolation, match=stem):
            idn.assert_cast_identity(cast)
    # a banned stem fails even in a non-identity slot
    cast = _cast(tmp_path, [{"slot": "S01", "stem": "CLIP_0061", "in_s": 1.0, "identity": "none"}])
    with pytest.raises(idn.IdentityViolation, match="FULL ban"):
        idn.assert_cast_identity(cast)


def test_settled_stem_is_accepted(tmp_path):
    cast = _cast(tmp_path, [
        {"slot": "S01", "stem": "CLIP_0065", "in_s": 2.0, "identity": "subject"},
        {"slot": "S02", "stem": "CLIP_0067", "in_s": 0.5, "identity": "subject"},
        {"slot": "S03", "stem": "CLIP_0068", "in_s": 5.1, "identity": "none"},
    ])
    idn.assert_cast_identity(cast)                         # no raise
    assert idn.check_cast(cast) == []


def test_identity_slot_outside_pool_and_unruled_are_rejected(tmp_path):
    cast = _cast(tmp_path, [{"slot": "S01", "stem": "CLIP_0003", "in_s": 1.0, "identity": "subject"}])
    with pytest.raises(idn.IdentityViolation, match="unruled"):
        idn.assert_cast_identity(cast)
    cast = _cast(tmp_path, [{"slot": "S01", "stem": "CLIP_0073", "in_s": 1.0}], identity_slots=["S01"])
    with pytest.raises(idn.IdentityViolation, match="not in settled_pool"):
        idn.assert_cast_identity(cast)


def test_undeclared_identity_fails(tmp_path):
    cast = _cast(tmp_path, [{"slot": "S01", "stem": "CLIP_0065", "in_s": 2.0}])
    with pytest.raises(idn.IdentityViolation, match="identity not declared"):
        idn.assert_cast_identity(cast)


def test_span_inside_banned_window_is_rejected_and_outside_passes(tmp_path):
    # CLIP_0074: in-points 0.0-8.0 banned, 8.0+ allowed
    assert idn.is_blacklisted("CLIP_0074", 3.0)
    assert idn.is_blacklisted("CLIP_0074", 7.5, 8.5)     # runs into the window from inside
    assert not idn.is_blacklisted("CLIP_0074", 8.0, 9.0)
    assert idn.is_blacklisted("CLIP_0074")               # no time given: cannot prove it is outside
    # CLIP_0075: allowed only 10.5-11.5, 12.0-14.3, 16.2-17.5
    assert idn.is_blacklisted("CLIP_0075", 11.6)
    assert not idn.is_blacklisted("CLIP_0075", 12.5, 13.5)
    bad = _cast(tmp_path, [{"slot": "S01", "stem": "CLIP_0074", "in_s": 4.0, "len": 1.0, "identity": "none"}])
    with pytest.raises(idn.IdentityViolation, match="banned window"):
        idn.assert_cast_identity(bad)
    ok = _cast(tmp_path, [{"slot": "S01", "stem": "CLIP_0074", "in_s": 9.0, "len": 1.0, "identity": "none"}])
    idn.assert_cast_identity(ok)


def test_preflight_runs_identity_only_with_a_cast(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(preflight, "identity_rules", lambda c: calls.append(c) or [])
    monkeypatch.setattr(preflight, "machine", lambda: [])
    monkeypatch.setattr(preflight, "lessons", lambda r, f: [])
    monkeypatch.setattr("sys.argv", ["preflight", "--row", "99", "--skip-tests"])
    preflight.main()                                      # no --cast: identity check is a no-op
    assert calls == []
    src = open(preflight.__file__).read()
    assert "fails += identity_rules(a.cast)" in src and src.index("identity_rules(a.cast)") > src.index("if a.cast:")
    bad = _cast(tmp_path, [{"slot": "S01", "stem": "CLIP_0072", "in_s": 1.0, "identity": "subject"}])
    monkeypatch.undo()                                    # also undoes the autouse patches: re-apply them
    from onetoone import grades as _g
    monkeypatch.setattr(idn, "POOL_PATH", tmp_path / "identity_pool.json")
    monkeypatch.setattr(_g, "GRADES_PATH", tmp_path / "grades.jsonl")
    idn._load.cache_clear()
    fails = preflight.identity_rules(bad)                 # the real one, on a bad cast
    assert fails and all(f.startswith("identity (L0052):") for f in fails)
