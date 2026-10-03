"""Ground-truth tests for tools/reuse_map.py, on a synthetic fixture.

The production suite asserted facts an independent audit had established from
bytes against a real registry. That registry is not part of the handover, so
every rule those assertions protected is re-proved here on a small synthetic
repo built in a temp dir. If the tool ever stops reproducing one of them, the
tool is wrong, not the fixture.

The failure modes covered (each one shipped a wrong reuse claim once):
  1. reading the EARLIEST draft instead of the delivered cut;
  2. a source-key list missing `remote_path` / `local_path`;
  3. reading master ids out of prose / runner-ups / rejected alternates;
  4. a recast that leaves the replaced master on the map;
  5. a second delivered cut (variant) or a superseded-but-delivered cut that the
     map could not see, so a "first ship" claim was false;
  6. a declared trial-variant batch that silently reads as empty;
  7. rows with delivered cuts that are not in the tool's universe at all.

    python3 -m pytest tools/test_reuse_map.py -q
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reuse_map  # noqa: E402

RS = reuse_map.RowSpec


def _w(root, rel, doc):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc), encoding="utf-8")
    return rel


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("repo")
    # a reference -----------------------------------------------------------------
    _w(root, "reel-01/edit/selection-v1.json", {"slots": [  # an old DRAFT: must never be read
        {"slot": "p001", "source": "CLIP_0098.MP4"}]})
    _w(root, "reel-01/edit/selection-v2.json", {"slots": [
        {"slot": "p001", "source_path": "/m/0123456789abcdef__CLIP_0001.MP4",
         "runner_ups": [{"clip": "CLIP_0097.MP4"}],
         "rejected_on_doctrine": ["CLIP_0096 off-doctrine take"],
         "candidate_observation": "not the CLIP_0095 take"},
        {"slot": "p002", "remote_path": "day 1/CLIP_0002.MP4", "local_path": "/cache/day 1/CLIP_0002.MP4"},
        {"slot": "p003", "source": "CLIP_0003.MP4"},
        {"slot": "p004", "note": "2-frame black tail, no master by design"},
    ]})
    _w(root, "reel-01/edit/recast-p003.json", {"recast": {"p003": {
        "was": {"source": "CLIP_0003.MP4"}, "now": {"source": "CLIP_0004.MP4"}}}})
    _w(root, "reel-01/work/contract-variant.json", {"slots": [{"block_id": "p001", "source": "CLIP_0005.MP4"}]})
    _w(root, "reel-01/edit/selection-v1-delivered.json", {"slots": [{"slot": "p001", "source": "CLIP_0006.MP4"}]})
    # a reference -----------------------------------------------------------------
    _w(root, "reel-02/edit/selection.locked.json", {"slots": [{"slot": "p010", "source": "CLIP_0001.MP4"}]})
    for i, clip in ((1, "CLIP_0007"), (2, "CLIP_0008")):
        _w(root, f"reel-02/variants/var{i:02d}/selection.locked.json",
           {"status": "LOCKED", "slots": [{"block_id": "p001", "source": clip + ".MP4"}]})
    _w(root, "reel-02/variants/superseded/var03/selection.locked.json",
       {"slots": [{"block_id": "p001", "source": "CLIP_0094.MP4"}]})
    # a reference: an override DELTA wrongly declared as a base ------------------
    _w(root, "reel-04/picture-plan.json", {"blocks": [
        {"block_id": "b1", "source": "CLIP_0009.MP4"}, {"block_id": "b2"}, {"block_id": "b3"}]})
    # registry ----------------------------------------------------------------
    _w(root, "REEL_REGISTRY.json", {"reels": [
        {"sequence": 1, "version": "v2"},
        {"sequence": 2, "version": "v13"},
        {"sequence": 3, "version": "v1"},
        {"sequence": 4, "version": "v1"},
        {"sequence": 5, "version": "v1", "today_files": ["<reel-05>.mp4"]},  # delivered, not in ROWS
    ]})
    return root


@pytest.fixture(scope="module")
def rows(repo):
    return {
        1: RS(version="v2", base="reel-01/edit/selection-v2.json",
              reason="v2 is the delivered cut; selection-v1.json is a draft",
              supersedes=(reuse_map.Superseder(path="reel-01/edit/recast-p003.json", note="p003 recast"),),
              also=(reuse_map.AlsoPlan(path="reel-01/work/contract-variant.json", prefix="var:"),),
              historic=(reuse_map.HistoricShip(path="reel-01/edit/selection-v1-delivered.json", prefix="v1:"),)),
        2: RS(version="v13", base="reel-02/edit/selection.locked.json", reason="main cut",
              variants=(reuse_map.VariantBatch(
                  glob_pattern="reel-02/variants/*/selection.locked.json", note="trial variants",
                  exclude=("/superseded/",), label_index=-2, expect_at_least=2),)),
        3: RS(version="v1", base=None, reason="mp4 only", unresolved="no structured plan on disk"),
        4: RS(version="v1", base="reel-04/picture-plan.json", reason="fixture delta"),
    }


@pytest.fixture(scope="module")
def data(repo, rows):
    mp = pytest.MonkeyPatch()
    mp.setattr(reuse_map, "REPO", str(repo))
    mp.setattr(reuse_map, "REGISTRY", str(repo / "REEL_REGISTRY.json"))
    mp.setattr(reuse_map, "ROWS", rows)
    try:
        yield reuse_map.build()
    finally:
        mp.undo()


def slots_of(data, master):
    return [(e["row"], e["slot"]) for e in data["masters"].get(master, [])]


def rows_of(data, master):
    return sorted({r for r, _ in slots_of(data, master)})


# --- delivered cut only --------------------------------------------------------


def test_the_earliest_draft_is_never_read(data):
    assert "CLIP_0098" not in data["masters"]
    assert data["rows"]["1"]["provenance"]["base_file"] == "reel-01/edit/selection-v2.json"


def test_prose_runner_ups_and_rejects_never_enter_the_map(data):
    for m in ("CLIP_0097", "CLIP_0096", "CLIP_0095"):
        assert m not in data["masters"]


def test_remote_and_local_path_are_source_keys(data):
    assert slots_of(data, "CLIP_0002") == [(1, "p002")]


def test_reuse_across_rows_is_visible(data):
    assert slots_of(data, "CLIP_0001") == [(1, "p001"), (2, "p010")]


def test_recast_removes_the_master_it_replaced(data):
    assert "CLIP_0003" not in data["masters"]
    assert slots_of(data, "CLIP_0004") == [(1, "p003")]
    sup = data["rows"]["1"]["provenance"]["supersessions"][0]
    assert sup["replaced"]["p003"] == {"was": ["CLIP_0003"], "now": ["CLIP_0004"]}


def test_a_sourceless_record_is_listed_but_not_a_delta_warning(data):
    prov = data["rows"]["1"]["provenance"]
    assert prov["base_records_without_a_source"] == ["p004"]
    assert "delta_warning" not in prov


def test_a_mostly_sourceless_base_is_flagged_as_an_override_delta(data):
    assert "delta_warning" in data["rows"]["4"]["provenance"]


# --- second / historic / variant cuts ------------------------------------------


def test_also_delivered_cut_is_namespaced_and_never_overwrites_main(data):
    assert slots_of(data, "CLIP_0005") == [(1, "var:p001")]
    assert data["rows"]["1"]["slots"]["p001"] == ["CLIP_0001"]


def test_superseded_but_delivered_cut_still_counts_for_first_ship(data):
    assert slots_of(data, "CLIP_0006") == [(1, "v1:p001")]


def test_variant_batch_reads_live_contracts_and_skips_superseded(data):
    assert slots_of(data, "CLIP_0007") == [(2, "var01:p001")]
    assert slots_of(data, "CLIP_0008") == [(2, "var02:p001")]
    assert "CLIP_0094" not in data["masters"]
    assert data["variant_contracts"]["contracts_read"] == 2


def test_a_declared_variant_batch_that_reads_empty_raises(tmp_path):
    """An empty path must FAIL LOUDLY: it is otherwise indistinguishable from a row with no variants."""
    spec = RS(version="x", base=None, reason="fixture", unresolved="",
              variants=(reuse_map.VariantBatch(glob_pattern=str(tmp_path / "var*" / "s.json"), note="nothing"),))
    res = reuse_map.RowResult(row=8, version="x")
    with pytest.raises(reuse_map.ReuseMapError, match="ZERO live contracts"):
        reuse_map.walk_variant_batch(8, spec.variants[0], res)


def test_a_variant_batch_short_of_its_declared_count_raises(tmp_path):
    for i in (1, 2):
        d = tmp_path / f"var{i:02d}"
        d.mkdir()
        (d / "s.json").write_text(json.dumps({"slots": [{"block_id": "p001", "source": "CLIP_0011.MP4"}]}))
    batch = reuse_map.VariantBatch(glob_pattern=str(tmp_path / "var*" / "s.json"), note="short",
                                   label_index=-2, expect_at_least=10)
    with pytest.raises(reuse_map.ReuseMapError, match="expected at least 10"):
        reuse_map.walk_variant_batch(8, batch, reuse_map.RowResult(row=8, version="x"))


# --- declared blind spots --------------------------------------------------------


def test_unresolved_rows_are_declared_not_guessed(data):
    assert data["rows"]["3"]["provenance"]["status"] == "UNRESOLVED"
    assert data["rows"]["3"]["slots"] == {}
    assert {"row": "3", "why": "no structured plan on disk"} in data["rows_unresolved"]


def test_rows_with_deliveries_outside_the_tool_are_reported(data):
    assert [o["row"] for o in data["rows_outside_the_tool"]] == ["5"]


def test_missing_base_is_unresolved_not_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(reuse_map, "REPO", str(tmp_path))
    res = reuse_map.build_row(9, RS(version="v1", base="nope.json", reason="x"), "v1")
    assert res.provenance["status"] == "UNRESOLVED"


# --- normalisation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("CLIP_0013", "CLIP_0013"),
        ("day 3/CLIP_0055.MP4", "CLIP_0055"),
        ("<drive-id>__CLIP_0013.MP4", "CLIP_0013"),
        ("0123456789abcdef__CAM_0002.MP4", "CAM_0002"),
        ("/media/work/masters/0123456789abcdef__C0001.MP4", "C0001"),
        ("/home/x/workspaces/reel2/sources/IMG_0001.MOV", "IMG_0001"),
        ("ab" * 32, None),
        ("day 3", None),
        ("", None),
        (None, None),
        (1234, None),
    ],
)
def test_master_id_normalisation(value, expected):
    assert reuse_map.master_id(value) == expected


def test_match_masters_accepts_numeric_tail(data):
    assert reuse_map.match_masters(data, "0001") == ["CLIP_0001"]
    assert reuse_map.match_masters(data, "clip_0004") == ["CLIP_0004"]


def test_shipped_rows_table_is_empty_in_the_handover():
    """The production registry is not shipped; EXAMPLE_ROWS documents the shape."""
    src = open(reuse_map.__file__, encoding="utf-8").read()
    assert "ROWS: dict[int, RowSpec] = {\n    # Declare one RowSpec" in src
    assert set(reuse_map.EXAMPLE_ROWS) == {1, 2, 3}
