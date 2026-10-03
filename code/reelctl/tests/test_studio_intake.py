from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest
from test_studio_board_ui import request, studio_config

from reelctl.errors import ReelctlError
from reelctl.state import ProjectState
from reelctl.studio import intake as intake_module
from reelctl.studio import jobs, library
from reelctl.studio.config import StudioConfig
from reelctl.web import create_app

# A synthetic corpus shaped like a FOOTAGE_LIBRARY.json: 600 indexed clips, of which one
# thin world holds 11 and the largest world holds 150. A thin world is the case the mode
# call exists for, mirrored here so the test is hermetic.
THIN_WORLD_CLIPS = 11
LARGEST_WORLD_CLIPS = 150
CORPUS_TOTAL = 600


def write_library(config: StudioConfig, footage_root: Path) -> Path:
    worlds = {
        "world-a": LARGEST_WORLD_CLIPS,
        "world-b": 110,
        "world-c": 100,
        "world-d": 80,
        "world-e": 65,
        "world-f": 30,
        "world-g": 30,
        "world-h": 15,
        "world-i": 4,
        "world-k": THIN_WORLD_CLIPS,
        "unknown": 5,
    }
    clips: Dict[str, Any] = {}
    index = 0
    for world, count in worlds.items():
        for _ in range(count):
            index += 1
            clips[f"clip-{index:04d}"] = {
                "id": f"clip-{index:04d}",
                "path": f"{world}/C{index:04d}.MP4",
                "duration": 12.5,
                "tags": {"world_cluster": world, "roles": ["establishing"], "energy": "low", "confidence": "high"},
            }
    assert len(clips) == CORPUS_TOTAL
    path = Path(config.library_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "footage-library-v1",
                "built_at_utc": "2025-01-13T10:34:59Z",
                "corpus_total_files": CORPUS_TOTAL,
                "indexed": CORPUS_TOTAL,
                "source": {"drive_folder": "<drive-folder-id>", "library_root": str(footage_root)},
                "clips": clips,
            }
        ),
        encoding="utf-8",
    )
    return path


def intake_app(tmp_path: Path, *, registry: Any = None) -> Tuple[Any, StudioConfig, Path]:
    config = studio_config(tmp_path)
    config.projects_root.mkdir(parents=True, exist_ok=True)
    footage_root = tmp_path / "footage-library"
    footage_root.mkdir(exist_ok=True)
    write_library(config, footage_root)
    if registry is not None:
        Path(config.registry_path).write_text(json.dumps(registry), encoding="utf-8")
    return create_app(config=config), config, footage_root


REFERENCE = "https://video.example/reel/REF-19/"


# --- project id derivation -------------------------------------------------


def test_a_project_id_is_derived_from_the_reference_shortcode() -> None:
    assert intake_module.derive_project_id("https://video.example/reel/REF-25/") == "reel-ref-25-v1"
    assert intake_module.derive_project_id("https://video.example/p/ref-19/?igsh=x") == "reel-ref-19-v1"
    assert intake_module.derive_project_id("/Volumes/WORKDRIVE/refs/My Reference.mp4") == "reel-my-reference-v1"


def test_a_reference_with_no_usable_name_asks_for_an_explicit_project_id() -> None:
    with pytest.raises(ReelctlError) as caught:
        intake_module.derive_project_id("https://video.example/")

    assert "project id" in str(caught.value)


# --- the corpus census, with real numbers ----------------------------------


def test_the_library_census_counts_every_world(tmp_path: Path) -> None:
    _, config, footage_root = intake_app(tmp_path)

    report = library.load_library(config.library_path)
    census = library.world_census(report)

    assert report["status"] == "PASS"
    assert report["clips_total"] == CORPUS_TOTAL
    assert report["library_root"] == str(footage_root)
    assert census[0] == {"world": "world-a", "clips": LARGEST_WORLD_CLIPS}
    assert {"world": "world-k", "clips": THIN_WORLD_CLIPS} in census


def test_a_thin_world_is_reported_with_its_actual_clip_counts(tmp_path: Path) -> None:
    _, config, _ = intake_app(tmp_path)

    note = library.corpus_note(library.load_library(config.library_path), world="world-k")

    assert note["world"] == "world-k"
    assert note["clips"] == THIN_WORLD_CLIPS
    assert note["library_clips_total"] == CORPUS_TOTAL
    assert note["reference_locked_block_ceiling"] == THIN_WORLD_CLIPS
    assert note["largest_world"] == {"world": "world-a", "clips": LARGEST_WORLD_CLIPS}
    assert "11 of 600" in note["recommendation"]
    assert "150" in note["recommendation"]
    # No invented threshold: the ceiling is stated, the verdict is deferred to the gate.
    assert "FEASIBILITY_REPORTED" in note["recommendation"]


def test_a_world_absent_from_the_corpus_reads_as_zero_not_as_unknown(tmp_path: Path) -> None:
    _, config, _ = intake_app(tmp_path)

    note = library.corpus_note(library.load_library(config.library_path), world="arctic-tundra")

    assert note["clips"] == 0
    assert "not present in the indexed corpus" in note["recommendation"]


def test_with_no_world_named_the_census_is_returned_and_no_verdict_is_invented(tmp_path: Path) -> None:
    _, config, _ = intake_app(tmp_path)

    note = library.corpus_note(library.load_library(config.library_path), world=None)

    assert note["world"] is None
    assert note["clips"] is None
    assert len(note["census"]) == 11
    assert "not known until the reference is analysed" in note["recommendation"]


def test_a_missing_library_is_reported_not_guessed(tmp_path: Path) -> None:
    report = library.load_library(tmp_path / "absent.json")

    assert report["status"] == "ABSENT"
    assert report["clips_total"] == 0
    assert library.corpus_note(report, world="world-k")["clips"] == 0


# --- the front door: intake never bypasses the bootstrap contract ----------


def test_intake_creates_a_deferred_project_and_advances_nothing(tmp_path: Path) -> None:
    app, config, footage_root = intake_app(tmp_path)

    response = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE})
    payload = response.json()

    assert response.status_code == 201
    assert payload["project_id"] == "reel-ref-19-v1"
    directory = config.projects_root / "reel-ref-19-v1"
    state = ProjectState.load(directory / "state.json")
    assert {stage["status"] for stage in state.data["stages"].values()} == {"PENDING"}
    assert state.next_stage() == "REFERENCE_LOCKED"
    assert not (directory / "reference/reference-lock.json").exists()
    assert not list((directory / ".reelctl/receipts").rglob("*.json"))
    assert json.loads((directory / "project.json").read_text())["reference_path"] is None


def test_intake_defaults_the_footage_root_to_the_authorized_library_root(tmp_path: Path) -> None:
    app, config, footage_root = intake_app(tmp_path)

    request(app, "POST", "/api/intake", json_body={"reference": REFERENCE})

    config_json = json.loads((config.projects_root / "reel-ref-19-v1/project.json").read_text())
    assert config_json["footage_root"] == str(footage_root.resolve())


def test_intake_refuses_a_footage_root_that_is_not_a_directory(tmp_path: Path) -> None:
    app, config, footage_root = intake_app(tmp_path)

    response = request(
        app,
        "POST",
        "/api/intake",
        json_body={"reference": REFERENCE, "footage_root": str(tmp_path / "unmounted")},
    )

    assert response.status_code == 400
    assert "unmounted" in response.json()["error"]
    assert not (config.projects_root / "reel-ref-19-v1").exists()


def test_intake_refuses_to_reuse_an_existing_project_directory(tmp_path: Path) -> None:
    app, config, _ = intake_app(tmp_path)
    assert request(app, "POST", "/api/intake", json_body={"reference": REFERENCE}).status_code == 201

    again = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE})

    assert again.status_code == 400
    assert "existing project directory" in again.json()["error"]


# --- the mode is a call, not a dropdown the app fills silently -------------


def test_intake_produces_a_mode_ambiguity_call_card_in_the_onboarding_schema(tmp_path: Path) -> None:
    app, config, _ = intake_app(tmp_path)

    payload = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE, "world": "world-k"}).json()
    card = payload["call"]

    assert card["issue_type"] == "mode-ambiguity"
    assert card["severity"] == "DECISION"
    assert card["state"] == "AWAITING_OPERATOR"
    assert card["project_id"] == "reel-ref-19-v1"
    assert [option["key"] for option in card["options"]] == ["A", "B", "C"]
    consequences = " ".join(option["text"] for option in card["options"])
    assert "original-montage" in consequences and "reference-locked" in consequences
    assert card["recommendation"]
    assert card["reply_with"].startswith("A, B, C")


def test_the_mode_call_gates_selection_so_feasibility_can_still_run(tmp_path: Path) -> None:
    # The daemon's calls gate stops work *before* the stage a call names, so this value
    # decides how much of the pipeline an open mode call freezes. It must not freeze
    # feasibility: the operator cannot answer "original-montage or reference-locked?"
    # until feasibility has produced the corpus numbers — the thin-world case, where "11 of
    # 600 clips" is the whole argument. Selection is the first stage the mode
    # actually governs, because that is where 1:1 exact-scene matching diverges from
    # grammar adaptation. Registry contract: daemon_contract.intake_mode_call_stage.
    from reelctl.state import STAGES

    app, config, _ = intake_app(tmp_path)

    card = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE}).json()["call"]

    assert card["stage"] == intake_module.MODE_CALL_STAGE == "SELECTION_LOCKED"
    assert STAGES.index(card["stage"]) > STAGES.index("FEASIBILITY_REPORTED")


def test_the_call_card_is_written_where_the_registry_points_and_carries_the_numbers(tmp_path: Path) -> None:
    app, config, _ = intake_app(tmp_path)

    request(app, "POST", "/api/intake", json_body={"reference": REFERENCE, "world": "world-k"})

    card_path = config.projects_root / "reel-ref-19-v1/brain/07_CALL_PROMPTS.md"
    text = card_path.read_text(encoding="utf-8")
    assert text.startswith("CALL_REQUIRED")
    assert "issue_type: mode-ambiguity" in text
    assert "11 of 600" in text
    assert "OPTIONS:" in text and "MY RECOMMENDATION:" in text and "REPLY WITH:" in text


def test_the_written_card_names_the_stage_it_gates(tmp_path: Path) -> None:
    # The machine knows what the call freezes — the gate reads card["stage"]. The card on
    # disk is what a human reads, and without this line it asks for a decision without
    # saying what is waiting on it. Appended after issue_type so the onboarding prompt's
    # header block keeps its exact order and only gains a field.
    app, config, _ = intake_app(tmp_path)

    request(app, "POST", "/api/intake", json_body={"reference": REFERENCE})

    text = (config.projects_root / "reel-ref-19-v1/brain/07_CALL_PROMPTS.md").read_text(encoding="utf-8")
    header = text.split("\n\n")[0].splitlines()
    assert header[0] == "CALL_REQUIRED"
    assert header[-2] == "issue_type: mode-ambiguity"
    assert header[-1] == f"stage: {intake_module.MODE_CALL_STAGE}"


def test_the_provisional_mode_comes_from_the_lane_and_is_recorded_as_unconfirmed(tmp_path: Path) -> None:
    app, config, _ = intake_app(tmp_path)

    payload = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE}).json()
    record = json.loads((config.projects_root / "reel-ref-19-v1/intake.json").read_text())

    assert payload["lane"] == "VOLUME"
    assert record["mode"] == "original-montage"
    assert record["mode_confirmed"] is False
    assert "lane VOLUME" in record["mode_provenance"]
    assert json.loads((config.projects_root / "reel-ref-19-v1/project.json").read_text())["mode"] == "original-montage"


def test_the_intake_record_is_registry_consistent_without_editing_the_registry(tmp_path: Path) -> None:
    registry = {"schema_version": 2.0, "reels": [], "lanes": {}, "open_calls": []}
    app, config, _ = intake_app(tmp_path, registry=registry)

    request(app, "POST", "/api/intake", json_body={"reference": REFERENCE})

    record = json.loads((config.projects_root / "reel-ref-19-v1/intake.json").read_text())
    entry = record["registry_entry"]
    assert entry["reference_shortcode"] == "REF-19"
    assert entry["reference_url"] == REFERENCE
    assert entry["review_state"] == "INTAKE"
    assert entry["project_root"] == "reel-ref-19-v1"
    assert entry["lane"] == "VOLUME"
    # The registry itself is untouched: it is the single status truth and this is not its writer.
    assert json.loads(Path(config.registry_path).read_text()) == registry


# --- lane quota ------------------------------------------------------------


def test_craft_intake_is_refused_while_the_registry_says_the_slot_is_taken(tmp_path: Path) -> None:
    app, config, _ = intake_app(
        tmp_path,
        registry={
            "schema_version": 2.0,
            "reels": [],
            "lanes": {"CRAFT": {"quota": "one active project at a time", "active": "REEL-09 (v10-r5 source-contours)"}},
            "open_calls": [],
        },
    )

    response = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE, "lane": "CRAFT"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert "CRAFT" in error
    assert "one active project at a time" in error
    assert "REEL-09" in error
    assert not (config.projects_root / "reel-ref-19-v1").exists()


def test_craft_intake_is_accepted_when_the_slot_is_free(tmp_path: Path) -> None:
    app, config, _ = intake_app(
        tmp_path,
        registry={"schema_version": 2.0, "reels": [], "lanes": {"CRAFT": {"quota": "one active project at a time"}}, "open_calls": []},
    )

    payload = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE, "lane": "CRAFT"}).json()

    assert payload["lane"] == "CRAFT"
    assert json.loads((config.projects_root / "reel-ref-19-v1/project.json").read_text())["mode"] == "reference-locked"


def test_a_craft_project_this_app_created_also_occupies_the_slot(tmp_path: Path) -> None:
    app, config, _ = intake_app(
        tmp_path,
        registry={"schema_version": 2.0, "reels": [], "lanes": {"CRAFT": {"quota": "one active project at a time"}}, "open_calls": []},
    )
    first = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE, "lane": "CRAFT"})
    assert first.status_code == 201

    second = request(
        app,
        "POST",
        "/api/intake",
        json_body={"reference": "https://video.example/reel/ref-19/", "lane": "CRAFT"},
    )

    assert second.status_code == 400
    assert "reel-ref-19-v1" in second.json()["error"]


def test_a_registry_reel_declaring_lane_craft_also_occupies_the_slot(tmp_path: Path) -> None:
    # REEL-09 carries lane CRAFT on its own entry. Reading only lanes.CRAFT.active
    # would let a second CRAFT project through whenever that headline field is unset.
    app, config, _ = intake_app(
        tmp_path,
        registry={
            "schema_version": 2.0,
            "reels": [{"reference_shortcode": "REEL-09", "lane": "CRAFT", "version": "v10-r5"}],
            "lanes": {"CRAFT": {"quota": "one active project at a time"}},
            "open_calls": [],
        },
    )

    response = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE, "lane": "CRAFT"})

    assert response.status_code == 400
    assert "REEL-09" in response.json()["error"]
    assert "declares lane CRAFT" in response.json()["error"]


def test_the_craft_slot_holder_is_named_once_not_once_per_source(tmp_path: Path) -> None:
    app, config, _ = intake_app(
        tmp_path,
        registry={
            "schema_version": 2.0,
            "reels": [{"reference_shortcode": "REEL-09", "lane": "CRAFT"}],
            "lanes": {"CRAFT": {"quota": "one active project at a time", "active": "REEL-09 (v10-r5 source-contours)"}},
            "open_calls": [],
        },
    )

    quota = intake_module.craft_quota_state(config)

    assert len(quota["holders"]) == 1
    assert "REEL-09" in quota["holders"][0]


def test_a_stale_queue_row_names_the_conflict_but_never_vetoes_the_reel(tmp_path: Path) -> None:
    # studio.db is a scheduler, not an authority (§8.1): a leftover job row for a project
    # that no longer exists on disk must not be able to refuse a new intake.
    from reelctl.studio.db import initialize

    app, config, _ = intake_app(tmp_path)
    initialize(config.database_path)
    jobs.enqueue(
        config.database_path,
        project_id="reel-ref-19-v1",
        stage="RENDERED",
        kind="deterministic",
        reason="left over from a project that was deleted from disk",
    )

    response = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE})
    payload = response.json()

    assert response.status_code == 201
    assert payload["job_id"] is None
    assert "already has an active job" in payload["job_error"]
    assert "RENDERED" in payload["job_error"]
    assert (config.projects_root / "reel-ref-19-v1/state.json").is_file()


def test_an_unknown_lane_is_refused_by_name(tmp_path: Path) -> None:
    app, _, _ = intake_app(tmp_path)

    assert request(app, "POST", "/api/intake", json_body={"reference": REFERENCE, "lane": "TURBO"}).status_code == 422


def test_a_volume_intake_is_never_blocked_by_the_craft_slot(tmp_path: Path) -> None:
    app, _, _ = intake_app(
        tmp_path,
        registry={
            "schema_version": 2.0,
            "reels": [],
            "lanes": {"CRAFT": {"quota": "one active project at a time", "active": "REEL-09"}},
            "open_calls": [],
        },
    )

    assert request(app, "POST", "/api/intake", json_body={"reference": REFERENCE}).status_code == 201


# --- the bootstrap job -----------------------------------------------------


def test_intake_queues_exactly_one_bootstrap_job_for_the_daemon(tmp_path: Path) -> None:
    app, config, _ = intake_app(tmp_path)

    payload = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE}).json()

    queued = jobs.all_jobs(config.database_path)
    assert len(queued) == 1
    assert queued[0]["id"] == payload["job_id"]
    assert queued[0]["project_id"] == "reel-ref-19-v1"
    assert queued[0]["stage"] == "REFERENCE_LOCKED"
    assert queued[0]["kind"] == "deterministic"
    assert queued[0]["status"] == "QUEUED"
    assert queued[0]["reason"]


def test_intake_publishes_an_intake_event(tmp_path: Path) -> None:
    from reelctl.studio import events as ev

    app, config, _ = intake_app(tmp_path)

    request(app, "POST", "/api/intake", json_body={"reference": REFERENCE})

    recorded = [event for event in ev.events_since(config.database_path) if event["kind"] == "intake"]
    assert len(recorded) == 1
    assert recorded[0]["project_id"] == "reel-ref-19-v1"
    assert recorded[0]["payload"]["lane"] == "VOLUME"


def test_a_broken_job_queue_does_not_hide_that_the_project_was_created(tmp_path: Path) -> None:
    app, config, _ = intake_app(tmp_path)
    config.studio_dir.mkdir(parents=True, exist_ok=True)
    config.database_path.write_text("this is not a sqlite database", encoding="utf-8")

    response = request(app, "POST", "/api/intake", json_body={"reference": REFERENCE})
    payload = response.json()

    assert response.status_code == 201
    assert payload["job_id"] is None
    assert payload["job_error"]
    assert (config.projects_root / "reel-ref-19-v1/state.json").is_file()


# --- the board and the screen ----------------------------------------------


def test_the_new_reel_appears_on_the_board_with_its_lane_and_queued_job(tmp_path: Path) -> None:
    app, config, _ = intake_app(tmp_path)
    request(app, "POST", "/api/intake", json_body={"reference": REFERENCE})

    row = request(app, "GET", "/api/reels").json()["projects"][0]
    page = request(app, "GET", "/board").text

    assert row["project_id"] == "reel-ref-19-v1"
    assert row["lane"] == "VOLUME"
    assert "intake record" in row["lane_source"]
    assert row["job"]["stage"] == "REFERENCE_LOCKED"
    assert row["headline"] == "PENDING_MACHINE"
    assert "QUEUED REFERENCE_LOCKED" in page


def test_the_intake_screen_offers_one_box_and_shows_the_real_corpus_numbers(tmp_path: Path) -> None:
    app, config, footage_root = intake_app(tmp_path)

    page = request(app, "GET", "/intake").text

    assert 'name="reference"' in page
    assert str(footage_root) in page
    assert "world-k" in page and f">{THIN_WORLD_CLIPS}<" in page
    assert "600" in page
    assert "VOLUME" in page and "CRAFT" in page


def test_the_intake_screen_names_the_craft_slot_holder_instead_of_hiding_the_option(tmp_path: Path) -> None:
    app, _, _ = intake_app(
        tmp_path,
        registry={
            "schema_version": 2.0,
            "reels": [],
            "lanes": {"CRAFT": {"quota": "one active project at a time", "active": "REEL-09 (v10-r5 source-contours)"}},
            "open_calls": [],
        },
    )

    page = request(app, "GET", "/intake").text

    assert "REEL-09 (v10-r5 source-contours)" in page


def test_the_plan_endpoint_previews_without_writing_anything(tmp_path: Path) -> None:
    app, config, _ = intake_app(tmp_path)

    plan = request(app, "GET", f"/api/intake/plan?reference={REFERENCE}&world=world-k").json()

    assert plan["project_id"] == "reel-ref-19-v1"
    assert plan["mode_confirmed"] is False
    assert plan["feasibility_preview"]["clips"] == THIN_WORLD_CLIPS
    assert not (config.projects_root / "reel-ref-19-v1").exists()
    assert jobs.all_jobs(config.database_path) == [] if config.database_path.is_file() else True
