"""Gate tests for tools/variant_caster.py.

Every test drives the caster's real functions against synthetic fixtures, so a gate
that stops refusing fails here rather than in a render. Run with the reelctl venv:

    _reelctl/.venv/bin/py.test tools/test_variant_caster.py -q
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import variant_caster as vc  # noqa: E402

OUT_FPS = Fraction("2997/125")


def _index_clip(clip_id: str, name: str, *, frames: int = 400, fps: str = "25/1", bucket: str = "masters") -> dict:
    return {
        "clip_id": clip_id,
        "path": f"/root/{bucket}/{clip_id}__{name}",
        "relative_path": f"{bucket}/{clip_id}__{name}",
        "bytes": 1000,
        "sha256": clip_id + "0" * (64 - len(clip_id)),
        "status": "PASS",
        "video": {"frame_count": frames, "r_frame_rate": fps},
    }


def _library_clip(clip_id: str, name: str, *, roles, lighting="night", subject="person-A", world="interior-A") -> dict:
    return {
        "id": f"lib-{clip_id}",
        "path": f"day 1/{name}",
        "duration": 16.0,
        "tags": {
            "world_cluster": world,
            "roles": list(roles),
            "action_verbs": ["typing"],
            "energy": "low",
            "subject": subject,
            "lighting": lighting,
            "crop_safety_916": "safe",
            "confidence": "high",
            "evidence_tier": "STILL_TRIAGED",
            "notes": f"synthetic {name}",
        },
    }


def _corpus(n: int = 12, **kwargs):
    index = {"status": "PASS", "clips": [], "root": "/root"}
    library = {"clips": {}}
    for i in range(n):
        clip_id = f"{i:016x}"
        name = f"C{5000 + i}.MP4"
        index["clips"].append(_index_clip(clip_id, name))
        library["clips"][clip_id] = _library_clip(clip_id, name, roles=["seated_work", "establishing"], **kwargs)
    return index, library


EMPTY_BLACKLIST = {"blacklisted_clips": []}


# --- front door -------------------------------------------------------------


def test_proxies_are_never_render_candidates() -> None:
    index, library = _corpus(3)
    index["clips"].append(_index_clip("ff" * 8, "C9999.MP4", bucket="proxies"))
    library["clips"]["ff" * 8] = _library_clip("ff" * 8, "C9999.MP4", roles=["seated_work"])

    candidates, stats = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)

    assert {clip["basename"] for clip in candidates} == {"C5000.MP4", "C5001.MP4", "C5002.MP4"}
    assert stats["index_masters"] == 3


def test_master_without_library_tags_is_reported_not_guessed() -> None:
    index, library = _corpus(3)
    library["clips"].pop("0" * 15 + "1")

    candidates, stats = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)

    assert len(candidates) == 2
    assert stats["master_untagged"] == ["masters/0000000000000001__C5001.MP4"]


def test_footage_index_that_is_not_pass_closes_the_front_door() -> None:
    index, library = _corpus(3)
    index["status"] = "PARTIAL"

    with pytest.raises(vc.CasterError, match="front door"):
        vc.build_candidate_table(index, library, EMPTY_BLACKLIST)


# --- blacklist --------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        {"clip_id": "0000000000000001"},
        {"file": "0000000000000001__C5001.MP4"},
        {"file": "C5001.MP4"},
        {"file": "/some/other/root/masters/0000000000000001__C5001.MP4"},
        {"sha256": "0000000000000001" + "0" * 48},
    ],
)
def test_blacklist_matches_on_every_identifier_form(entry) -> None:
    index, library = _corpus(3)

    candidates, stats = vc.build_candidate_table(index, library, {"blacklisted_clips": [entry]})

    assert "0000000000000001" not in {clip["clip_id"] for clip in candidates}
    assert len(stats["blacklist_rejected"]) == 1


@pytest.mark.parametrize(
    "entry",
    [
        # v1 shape A — library-content scheme
        {"clip_id": "0000000000000001", "file": "0000000000000001__C5001.MP4"},
        # v1 shape B — Drive scheme. The id is NOT the index's clip_id and
        # the file name is the bare camera name, so a clip_id-only check misses it.
        {"clip_id": "<drive-id>", "file": "C5001.MP4"},
        # v2 canonical — both schemes in one entry plus the match_keys contract
        {
            "camera_name": "C5001.MP4",
            "camera_stem": "C5001",
            "content_id": "0000000000000001",
            "drive_file_id": "<drive-id>",
            "clip_id": "0000000000000001",
            "id": "<drive-id>",
            "file": "0000000000000001__C5001.MP4",
            "master_filenames": [
                "0000000000000001__C5001.MP4",
                "SYNTHETIC-storage-id-placeholder-01__C5001.MP4",
            ],
            "match_keys": [
                "0000000000000001",
                "<drive-id>",
                "C5001",
                "C5001.MP4",
            ],
        },
        # v2 with ONLY the contract field — a future entry written by match_keys alone
        {"match_keys": ["C5001"]},
    ],
)
def test_both_blacklist_key_schemes_bite(entry) -> None:
    """The doctrine-hygiene fix: neither key scheme may be a blind spot.

    SHOT_BLACKLIST.json carried both shapes in one array, and a lane that knew one
    could not see the other — a reference v002 cast the content-scheme entry, a reference v001
    cast the Drive-scheme entry. The caster must ban the clip whichever way it is
    addressed, including a v1 file that will never be rewritten.
    """
    index, library = _corpus(3)

    candidates, stats = vc.build_candidate_table(index, library, {"blacklisted_clips": [entry]})

    assert "0000000000000001" not in {clip["clip_id"] for clip in candidates}
    assert [row["clip_id"] for row in stats["blacklist_rejected"]] == ["0000000000000001"]


def test_a_v2_blacklist_file_bans_every_master_it_names() -> None:
    """Every entry is checked in BOTH naming styles the masters directory uses —
    `<content16hex>__CAMERA.MP4` and `<DriveId>__CAMERA.MP4` — because the file's two
    schemes came from those two styles coexisting on disk.

    The production suite ran this against the real SHOT_BLACKLIST.json; that file is not
    part of the handover, so it runs against a synthetic v2 file of the same shape. Point
    it at your own blacklist and it must still pass, never vacuously.
    """
    blacklist = {"blacklisted_clips": [{
        "camera_name": "C5001.MP4",
        "camera_stem": "C5001",
        "content_id": "0000000000000001",
        "drive_file_id": "SYNTHETIC-storage-id-placeholder-01",
        "sha256": "0000000000000001" + "0" * 48,
        "match_keys": ["0000000000000001", "SYNTHETIC-storage-id-placeholder-01", "C5001", "C5001.MP4"],
    }]}
    bans = vc.blacklisted_keys(blacklist)

    assert blacklist["blacklisted_clips"], "the blacklist is empty — refuse to pass vacuously"
    for entry in blacklist["blacklisted_clips"]:
        stem = entry["camera_stem"]
        for prefix in (entry["content_id"], entry["drive_file_id"]):
            relative = f"masters/{prefix}__{stem}.MP4"
            assert vc._name_forms(relative) & bans["files"], relative
        assert entry["content_id"] in bans["clip_ids"]
        assert entry["drive_file_id"] in bans["clip_ids"]
        assert entry["sha256"] in bans["sha256"]


# --- eligibility ------------------------------------------------------------


def test_role_lighting_and_subject_rules_all_bite() -> None:
    index, library = _corpus(2)
    library["clips"]["0" * 16]["tags"]["lighting"] = "day"
    library["clips"]["0" * 15 + "1"]["tags"]["subject"] = "multiple"
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)

    assert len(vc.eligible_pool(candidates, {"roles": ["payoff"]}, 8, OUT_FPS)) == 0
    assert len(vc.eligible_pool(candidates, {"lighting": ["day"]}, 8, OUT_FPS)) == 1
    assert len(vc.eligible_pool(candidates, {"subject": ["multiple"]}, 8, OUT_FPS)) == 1
    assert len(vc.eligible_pool(candidates, {"roles": ["seated_work"]}, 8, OUT_FPS)) == 2


def test_denials_beat_allows() -> None:
    index, library = _corpus(2)
    library["clips"]["0" * 16]["tags"]["world_cluster"] = "interior-B"
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)

    rule = {"roles": ["seated_work", "establishing"], "deny_world_cluster": ["interior-A"]}
    pool = vc.eligible_pool(candidates, rule, 8, OUT_FPS)
    assert [clip["clip_id"] for clip in pool] == ["0" * 16]

    # A clip carrying a denied role is out even though an allowed role also matches.
    assert vc.eligible_pool(candidates, {"roles": ["establishing"], "deny_roles": ["seated_work"]}, 8, OUT_FPS) == []


def test_clip_too_short_for_the_slot_window_is_ineligible() -> None:
    index, library = _corpus(1)
    index["clips"][0]["video"]["frame_count"] = 40
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)

    # 47 output frames at 25/1 against 2997/125 conform to 50 source frames.
    assert vc.required_source_frames(47, "25/1", OUT_FPS) == 50
    assert vc.eligible_pool(candidates, {}, 47, OUT_FPS) == []
    assert len(vc.eligible_pool(candidates, {}, 30, OUT_FPS)) == 1


# --- casting ----------------------------------------------------------------


def _slots(count: int = 12):
    return [{"block_id": f"p{i:03d}", "frames": 10, "reference_role": f"role {i}"} for i in range(1, count + 1)]


def _pools(candidates, slots, per_slot=None):
    per_slot = per_slot or {}
    return {slot["block_id"]: list(per_slot.get(slot["block_id"], candidates)) for slot in slots}


def test_family_is_deterministic_for_a_seed_and_moves_when_the_seed_moves() -> None:
    index, library = _corpus(30)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slots = _slots()
    masters = {slot["block_id"]: candidates[0]["clip_id"] for slot in slots}

    def run(seed):
        castings, _ = vc.cast_family(
            slots, _pools(candidates, slots), masters,
            variants=5, seed=seed, family_id="f", output_fps=OUT_FPS,
            hook_slot="p001", min_changed_ratio=0.7,
        )
        return [[s["source_clip_id"] for s in c["slots"]] for c in castings]

    assert run(7) == run(7)
    assert run(7) != run(8)


def test_no_clip_fills_two_slots_inside_one_casting() -> None:
    index, library = _corpus(30)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slots = _slots()
    masters = {slot["block_id"]: candidates[0]["clip_id"] for slot in slots}

    castings, _ = vc.cast_family(
        slots, _pools(candidates, slots), masters,
        variants=6, seed=1, family_id="f", output_fps=OUT_FPS, hook_slot="p001", min_changed_ratio=0.7,
    )

    for casting in castings:
        ids = [slot["source_clip_id"] for slot in casting["slots"]]
        assert len(set(ids)) == len(ids)


def test_hook_is_unique_per_variant_and_never_the_master_hook() -> None:
    index, library = _corpus(30)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slots = _slots()
    master_hook = candidates[3]["clip_id"]
    masters = {slot["block_id"]: candidates[0]["clip_id"] for slot in slots}
    masters["p001"] = master_hook

    castings, _ = vc.cast_family(
        slots, _pools(candidates, slots), masters,
        variants=8, seed=3, family_id="f", output_fps=OUT_FPS, hook_slot="p001", min_changed_ratio=0.7,
    )

    hooks = [casting["slots"][0]["source_clip_id"] for casting in castings]
    assert len(set(hooks)) == len(hooks)
    assert master_hook not in hooks


def test_every_pair_meets_the_distance_floor_including_the_master() -> None:
    index, library = _corpus(30)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slots = _slots()
    masters = {slot["block_id"]: candidates[i]["clip_id"] for i, slot in enumerate(slots)}

    castings, trace = vc.cast_family(
        slots, _pools(candidates, slots), masters,
        variants=10, seed=11, family_id="f", output_fps=OUT_FPS, hook_slot="p001", min_changed_ratio=0.7,
    )

    vectors = [[slot["source_clip_id"] for slot in casting["slots"]] for casting in castings]
    matrix = vc.distance_matrix([trace["master_vector"]] + vectors, ["m"] + [c["variant_id"] for c in castings])
    assert trace["min_changed"] == 9  # ceil(0.7 * 12)
    assert matrix["min_changed_off_diagonal"] >= 9


def test_a_scarce_slot_repeats_visibly_and_at_a_different_window() -> None:
    index, library = _corpus(30)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slots = _slots()
    pools = _pools(candidates, slots, {"p010": candidates[:2]})
    masters = {slot["block_id"]: candidates[i]["clip_id"] for i, slot in enumerate(slots)}

    castings, _ = vc.cast_family(
        slots, pools, masters,
        variants=6, seed=5, family_id="f", output_fps=OUT_FPS, hook_slot="p001", min_changed_ratio=0.7,
    )

    p010 = [next(s for s in c["slots"] if s["block_id"] == "p010") for c in castings]
    assert len({slot["source_clip_id"] for slot in p010}) == 2
    repeats = [slot for slot in p010 if slot["window_repeat_index"] > 0]
    assert repeats, "a 2-clip pool across 6 variants must repeat"
    for slot in repeats:
        assert slot["pool_size"] == 2
        siblings = [other for other in p010 if other["source_clip_id"] == slot["source_clip_id"]]
        assert len({other["source_start_frame"] for other in siblings}) > 1


def test_impossible_distance_demand_refuses_instead_of_shipping_near_duplicates() -> None:
    index, library = _corpus(30)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slots = _slots()
    # Eleven of twelve slots pinned to a single clip: at most one slot can ever change.
    pools = _pools(candidates, slots, {slot["block_id"]: [candidates[i]] for i, slot in enumerate(slots[1:], start=1)})
    masters = {slot["block_id"]: candidates[i]["clip_id"] for i, slot in enumerate(slots)}

    with pytest.raises(vc.CasterError, match="distance rule"):
        vc.cast_family(
            slots, pools, masters,
            variants=10, seed=2, family_id="f", output_fps=OUT_FPS, hook_slot="p001", min_changed_ratio=0.7,
        )


def test_observation_is_labelled_as_a_transcription_and_survives_reelctl_placeholder_check() -> None:
    index, library = _corpus(1)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    text = vc.observation_text(candidates[0], 10, 50)

    assert text.startswith("MACHINE-CAST")
    assert "NOT INDEPENDENTLY WATCHED" in text
    # reelctl's selection validator rejects observations starting with these prefixes.
    assert not text.upper().startswith(("REPLACE_", "PENDING_", "UNOBSERVED"))


# --- caption clearance ------------------------------------------------------
#
# Phase 2 rendered ten variants and every one failed caption legibility, because a slot's
# eligibility said nothing about the pixels the reel's own words sit on. These are the tests
# for the constraint that closes it.


CLEARANCE = {
    "states": ["C14", "C15"],
    "union_bbox_xyxy": [191, 247, 682, 612],
    "plate_alpha": 0.9,
    "reference_polarity": "dark_ink",
}


def test_the_servable_band_is_the_ink_engines_own_arithmetic() -> None:
    """Not a new threshold: a fill of 255 or 0 has to clear the engine's 25-level floor."""
    band = vc.caption_servable_band(1.0)
    assert band["min_for_dark_ink"] == 25.0
    assert band["max_for_light_ink"] == 230.0

    # a softer plate delivers less of its fill, so it needs more room on both sides
    softer = vc.caption_servable_band(0.5)
    assert softer["min_for_dark_ink"] == 50.0
    assert softer["max_for_light_ink"] == 205.0
    with pytest.raises(vc.CasterError):
        vc.caption_servable_band(0.0)


def test_a_mid_luma_window_is_servable_in_both_polarities() -> None:
    verdict = vc.caption_clearance_verdict({"p05": 90.0, "p50": 110.0, "p95": 130.0}, CLEARANCE)
    assert verdict["servable"] is True
    assert verdict["polarities_available"] == ["light_ink", "dark_ink"]
    assert verdict["reference_polarity_available"] is True


def test_a_window_that_straddles_the_palette_is_refused() -> None:
    """The var06 p010 case: dark at reference geometry, near-white on the 9:16 crop.

    A light fill cannot clear the bright end and a dark fill cannot clear the dark end, and
    there is one ink value for the state. No fill exists, so the slot has to change.
    """
    verdict = vc.caption_clearance_verdict({"p05": 12.0, "p50": 130.0, "p95": 246.0}, CLEARANCE)
    assert verdict["servable"] is False
    assert verdict["polarities_available"] == []
    assert "no palette-legal fill" in verdict["reason"]


def test_the_extremes_decide_not_the_median() -> None:
    """The extremes lesson: a healthy median describing a word whose last third vanished."""
    healthy_median = {"p05": 14.8, "p50": 80.0, "p95": 248.0}
    assert vc.caption_clearance_verdict(healthy_median, CLEARANCE)["servable"] is False


def test_the_clearance_gate_removes_a_clip_from_the_pool() -> None:
    index, library = _corpus(3)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    rule = {"roles": ["seated_work"], "caption_clearance": CLEARANCE}

    straddles = {clip["clip_id"] for clip in candidates[:1]}

    def luma(clip, clearance):
        if clip["clip_id"] in straddles:
            return {"p05": 8.0, "p50": 120.0, "p95": 250.0}
        return {"p05": 90.0, "p50": 110.0, "p95": 130.0}

    without = vc.eligible_pool(candidates, rule, 11, OUT_FPS)
    with_gate = vc.eligible_pool(candidates, rule, 11, OUT_FPS, caption_luma=luma)
    assert len(without) == 3  # no measurement supplied: the gate does not fire
    assert len(with_gate) == 2
    assert straddles.isdisjoint({clip["clip_id"] for clip in with_gate})
    assert all(clip["caption_clearance"]["servable"] for clip in with_gate)


def test_a_caption_box_maps_through_both_delivery_geometries() -> None:
    """The 16:9 master and the 9:16 reframe cover different parts of the same source."""
    box = [191, 247, 682, 612]
    source = (3840, 2160)
    refgeom = vc.caption_box_on_source(box, (1916, 1078), source)
    vertical = vc.caption_box_on_source(
        box, (1080, 1920), source, layer_scale=1080 / 1916, layer_offset=(0, 656)
    )
    for mapped in (refgeom, vertical):
        assert 0 <= mapped[0] < mapped[2] <= source[0]
        assert 0 <= mapped[1] < mapped[3] <= source[1]
    # the 9:16 pass keeps 31.6% of the width, which is a large zoom in: the same caption box
    # therefore covers far FEWER source pixels there than it does at reference geometry, and
    # those pixels are a different part of the frame entirely
    assert (refgeom[2] - refgeom[0]) > (vertical[2] - vertical[0]) * 2
    assert vertical[0] > refgeom[0] and vertical[2] > refgeom[2]
    # and a non-centre anchor slides the window horizontally, as the render's own anchor does
    left = vc.caption_box_on_source(box, (1080, 1920), source, anchor_x=0.1,
                                    layer_scale=1080 / 1916, layer_offset=(0, 656))
    assert left[0] < vertical[0]


def test_a_scarce_slot_is_recast_by_window_when_the_pool_has_no_other_clip() -> None:
    """p010's pool is two clips and 1,404 frames long. The moment is as much a re-cast as
    the clip, and it changes the pixels behind the caption just as completely."""
    index, library = _corpus(2)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slot = {"block_id": "p010", "frames": 11, "reference_role": "the turn"}
    rule = {"caption_clearance": CLEARANCE}
    current = candidates[0]["clip_id"]

    def luma(clip, clearance, window):
        # only the late window of the current clip clears; the sibling clip is taken
        if clip["clip_id"] == current and window[0] > 200:
            return {"p05": 95.0, "p50": 120.0, "p95": 150.0}
        return {"p05": 6.0, "p50": 128.0, "p95": 250.0}

    result = vc.recast_slot(
        slot=slot, rule=rule, pool=candidates, output_fps=OUT_FPS,
        taken_clip_ids=[candidates[1]["clip_id"]], current_clip_id=current, luma_of=luma,
    )
    chosen = result["chosen"]
    assert chosen["clip_id"] == current
    assert chosen["source_start_frame"] > 200
    assert chosen["servable"] is True
    assert chosen["changes_clip"] is False
    assert result["servable_options"] >= 1


def test_recast_prefers_a_different_clip_when_both_clear_equally() -> None:
    index, library = _corpus(2)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slot = {"block_id": "p010", "frames": 11, "reference_role": "the turn"}
    current = candidates[0]["clip_id"]

    def luma(clip, clearance, window):
        return {"p05": 95.0, "p50": 120.0, "p95": 150.0}

    result = vc.recast_slot(
        slot=slot, rule={"caption_clearance": CLEARANCE}, pool=candidates, output_fps=OUT_FPS,
        taken_clip_ids=[], current_clip_id=current, luma_of=luma,
    )
    assert result["chosen"]["clip_id"] != current
    assert result["chosen"]["changes_clip"] is True


def test_recast_refuses_rather_than_shipping_an_unservable_window() -> None:
    index, library = _corpus(2)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slot = {"block_id": "p010", "frames": 11, "reference_role": "the turn"}

    def luma(clip, clearance, window):
        return {"p05": 6.0, "p50": 128.0, "p95": 250.0}

    with pytest.raises(vc.CasterError, match="footage-coverage finding"):
        vc.recast_slot(
            slot=slot, rule={"caption_clearance": CLEARANCE}, pool=candidates, output_fps=OUT_FPS,
            taken_clip_ids=[], current_clip_id=candidates[0]["clip_id"], luma_of=luma,
        )


def test_recast_never_takes_a_clip_already_used_in_the_same_casting() -> None:
    index, library = _corpus(3)
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slot = {"block_id": "p010", "frames": 11, "reference_role": "the turn"}
    taken = [candidates[1]["clip_id"], candidates[2]["clip_id"]]

    def luma(clip, clearance, window):
        return {"p05": 95.0, "p50": 120.0, "p95": 150.0}

    result = vc.recast_slot(
        slot=slot, rule={"caption_clearance": CLEARANCE}, pool=candidates, output_fps=OUT_FPS,
        taken_clip_ids=taken, current_clip_id=candidates[0]["clip_id"], luma_of=luma,
    )
    assert {row["clip_id"] for row in result["considered"]}.isdisjoint(taken)


def test_recast_never_hands_two_variants_the_same_window_of_the_same_clip() -> None:
    """A trial family exists to test genuinely different creative against each other.

    Two variants sharing a byte-identical block at the same beat makes the comparison meaningless, and a
    scarce slot re-cast twice is exactly where it would happen: both re-casts see the same small
    pool and the same best window.
    """
    index, library = _corpus(2)
    index["clips"][0]["video"]["frame_count"] = 1404
    index["clips"][1]["video"]["frame_count"] = 1644
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slot = {"block_id": "p010", "frames": 11, "reference_role": "the turn"}

    def luma(clip, clearance, window):
        return {"p05": 95.0, "p50": 120.0, "p95": 150.0}

    first = vc.recast_slot(
        slot=slot, rule={"caption_clearance": CLEARANCE}, pool=candidates, output_fps=OUT_FPS,
        taken_clip_ids=[], current_clip_id=candidates[0]["clip_id"], luma_of=luma,
    )["chosen"]

    second = vc.recast_slot(
        slot=slot, rule={"caption_clearance": CLEARANCE}, pool=candidates, output_fps=OUT_FPS,
        taken_clip_ids=[], current_clip_id=candidates[0]["clip_id"], luma_of=luma,
        sibling_windows=[
            (first["clip_id"], first["source_start_frame"], first["source_end_frame_exclusive"])
        ],
    )["chosen"]

    assert (second["clip_id"], second["source_start_frame"]) != (
        first["clip_id"], first["source_start_frame"]
    )
    if second["clip_id"] == first["clip_id"]:
        overlap = (
            second["source_start_frame"] < first["source_end_frame_exclusive"]
            and first["source_start_frame"] < second["source_end_frame_exclusive"]
        )
        assert not overlap


def test_recast_refuses_when_every_servable_option_would_duplicate_a_sibling() -> None:
    index, library = _corpus(1)
    index["clips"][0]["video"]["frame_count"] = 20  # one usable window only
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    slot = {"block_id": "p010", "frames": 11, "reference_role": "the turn"}

    def luma(clip, clearance, window):
        return {"p05": 95.0, "p50": 120.0, "p95": 150.0}

    needed = vc.required_source_frames(11, "25/1", OUT_FPS)
    with pytest.raises(vc.CasterError, match="sibling collisions"):
        vc.recast_slot(
            slot=slot, rule={"caption_clearance": CLEARANCE}, pool=candidates, output_fps=OUT_FPS,
            taken_clip_ids=[], current_clip_id=candidates[0]["clip_id"], luma_of=luma,
            sibling_windows=[(candidates[0]["clip_id"], 0, needed + 20)],
        )


def test_window_grid_spreads_over_the_usable_range_and_stays_inside_it() -> None:
    index, library = _corpus(1)
    index["clips"][0]["video"]["frame_count"] = 1404
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    needed = vc.required_source_frames(11, "25/1", OUT_FPS)
    grid = vc.window_grid(candidates[0], 11, OUT_FPS, steps=12)

    assert len(grid) == 12
    assert grid[0] == 0
    assert grid[-1] + needed <= 1404
    assert len(set(grid)) == len(grid)


def test_window_quantiles_stay_inside_the_clip() -> None:
    index, library = _corpus(1)
    index["clips"][0]["video"]["frame_count"] = 51
    candidates, _ = vc.build_candidate_table(index, library, EMPTY_BLACKLIST)
    for repeat in range(len(vc.WINDOW_QUANTILES)):
        start, needed, _ = vc.window_start(candidates[0], 47, OUT_FPS, repeat)
        assert needed == 50
        assert 0 <= start <= 1
        assert start + needed <= 51
