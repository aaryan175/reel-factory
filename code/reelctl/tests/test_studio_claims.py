"""Advisory ownership claims and the read-only registry/agent-session checks (§9.3, §9.4, §9.5).

The flock is the authority; a claim only stops two long judgment runs landing on one
project, which flock alone would not catch. Nothing here may ever block or raise: an
unreadable agent-session database means "check skipped", never "stop working".
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reelctl.studio.claims import acquire_claim, agent_session_activity, read_claim, release_claim
from reelctl.studio.config import StudioConfig
from reelctl.studio.registry import declared_craft_holder, ownership_index, registry_check

NOW = datetime(2025, 1, 14, 12, 0, 0, tzinfo=timezone.utc)


def _config(tmp_path: Path) -> StudioConfig:
    return StudioConfig.from_env(
        {
            "REEL_STUDIO_PROJECTS_ROOT": str(tmp_path / "projects"),
            "REEL_STUDIO_STORAGE_ROOT": str(tmp_path / "storage"),
            "REEL_STUDIO_PATH": str(tmp_path / "bin"),
        }
    )


# --- claims ----------------------------------------------------------------


def test_a_claim_records_who_owns_the_project_and_when(tmp_path: Path) -> None:
    config = _config(tmp_path)

    taken, reason = acquire_claim(config, "demo", owner="studio-daemon", lane="VOLUME", now=NOW)

    assert taken is True
    assert reason is None
    claim = read_claim(config, "demo")
    assert claim.owner == "studio-daemon"
    assert claim.lane == "VOLUME"
    assert claim.pid > 0
    assert claim.heartbeat_utc == "2025-01-14T12:00:00Z"


def test_a_project_claimed_by_someone_else_is_refused_with_their_name(tmp_path: Path) -> None:
    config = _config(tmp_path)
    acquire_claim(config, "demo", owner="agent-session-42", lane="CRAFT", now=NOW)

    taken, reason = acquire_claim(config, "demo", owner="studio-daemon", lane="VOLUME", now=NOW + timedelta(minutes=5))

    assert taken is False
    assert "agent-session-42" in reason


def test_the_same_owner_refreshes_its_own_heartbeat(tmp_path: Path) -> None:
    config = _config(tmp_path)
    acquire_claim(config, "demo", owner="studio-daemon", lane="VOLUME", now=NOW)

    taken, _ = acquire_claim(config, "demo", owner="studio-daemon", lane="VOLUME", now=NOW + timedelta(minutes=5))

    assert taken is True
    assert read_claim(config, "demo").heartbeat_utc == "2025-01-14T12:05:00Z"


def test_a_claim_older_than_the_ttl_is_taken_over(tmp_path: Path) -> None:
    config = _config(tmp_path)
    acquire_claim(config, "demo", owner="agent-session-42", lane="CRAFT", now=NOW)

    taken, reason = acquire_claim(
        config, "demo", owner="studio-daemon", lane="VOLUME", now=NOW + timedelta(minutes=16), ttl_seconds=900
    )

    assert taken is True
    assert "agent-session-42" in reason
    assert read_claim(config, "demo").owner == "studio-daemon"


def test_a_corrupt_claim_file_does_not_stop_the_daemon(tmp_path: Path) -> None:
    config = _config(tmp_path)
    acquire_claim(config, "demo", owner="studio-daemon", lane="VOLUME", now=NOW)
    path = config.studio_dir / "claims" / "demo.json"
    path.write_text("{not json", encoding="utf-8")

    assert read_claim(config, "demo") is None
    taken, _ = acquire_claim(config, "demo", owner="studio-daemon", lane="VOLUME", now=NOW)
    assert taken is True


def test_releasing_only_removes_your_own_claim(tmp_path: Path) -> None:
    config = _config(tmp_path)
    acquire_claim(config, "demo", owner="agent-session-42", lane="CRAFT", now=NOW)

    release_claim(config, "demo", owner="studio-daemon")
    assert read_claim(config, "demo").owner == "agent-session-42"

    release_claim(config, "demo", owner="agent-session-42")
    assert read_claim(config, "demo") is None


# --- the agent-session concurrency check (§9.5) -----------------------------------


def _agent_db(path: Path, *, cwd: str, last_activity: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, title TEXT, last_activity_at REAL, last_activity_description TEXT)")
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        ("20250114_120000_aaaaaa", cwd, None, last_activity, None),
    )
    connection.commit()
    connection.close()
    return path


def test_a_recent_agent_session_on_the_project_is_reported_as_active(tmp_path: Path) -> None:
    database = _agent_db(tmp_path / "agent/state.db", cwd="/projects/reel-production/demo", last_activity=NOW.timestamp() - 60)

    report = agent_session_activity("demo", database_path=database, now=NOW)

    assert report["checked"] is True
    assert report["active"] is True
    assert report["sessions"]


def test_an_old_agent_session_is_not_active(tmp_path: Path) -> None:
    database = _agent_db(tmp_path / "agent/state.db", cwd="/projects/reel-production/demo", last_activity=NOW.timestamp() - 7200)

    report = agent_session_activity("demo", database_path=database, now=NOW)

    assert report["checked"] is True
    assert report["active"] is False


def test_a_missing_agent_database_skips_the_check_instead_of_blocking(tmp_path: Path) -> None:
    report = agent_session_activity("demo", database_path=tmp_path / "nope/state.db", now=NOW)

    assert report["checked"] is False
    assert report["active"] is False
    assert report["reason"]


def test_a_garbage_agent_database_skips_the_check_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "agent/state.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a database")

    report = agent_session_activity("demo", database_path=path, now=NOW)

    assert report["checked"] is False
    assert report["reason"]


# --- the registry, read only (§9.4) ---------------------------------------


def _registry(root: Path, reels: list) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "REEL_REGISTRY.json"
    path.write_text(json.dumps({"schema_version": "2.0", "reels": reels}), encoding="utf-8")
    return path


def test_a_parseable_registry_passes_preflight(tmp_path: Path) -> None:
    _registry(tmp_path / "projects", [])

    assert registry_check(tmp_path / "projects")["status"] == "PASS"


def test_an_unparseable_registry_halts_and_surfaces_rather_than_being_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    (root / "REEL_REGISTRY.json").write_text("{not json", encoding="utf-8")

    report = registry_check(root)

    assert report["status"] == "FAIL"
    assert "REEL_REGISTRY.json" in report["reason"]
    assert (root / "REEL_REGISTRY.json").read_text(encoding="utf-8") == "{not json"


def test_a_missing_registry_is_a_named_failure_not_a_crash(tmp_path: Path) -> None:
    report = registry_check(tmp_path / "projects")

    assert report["status"] == "FAIL"
    assert report["reason"]


def test_a_quarantined_runtime_is_skipped_with_the_registrys_own_words(tmp_path: Path) -> None:
    _registry(
        tmp_path / "projects",
        [
            {
                "reference_shortcode": "ref-06",
                "project_root": "reel-ref-06-v1/",
                "lane": "CRAFT",
                "runtime": "LEGACY_QUARANTINED_BESPOKE_PIPELINE",
            }
        ],
    )

    index = ownership_index(tmp_path / "projects")

    assert index["reel-ref-06-v1"]["daemon_owned"] is False
    assert "LEGACY_QUARANTINED_BESPOKE_PIPELINE" in index["reel-ref-06-v1"]["reason"]


def test_a_quarantined_review_state_is_skipped_too(tmp_path: Path) -> None:
    _registry(
        tmp_path / "projects",
        [{"reference_shortcode": "ref-27", "project_root": "reel-ref-27-v1/", "lane": "VOLUME", "review_state": "CANDIDATE_QUARANTINED_REVIEW_ONLY"}],
    )

    index = ownership_index(tmp_path / "projects")

    assert index["reel-ref-27-v1"]["daemon_owned"] is False
    assert "QUARANTINED" in index["reel-ref-27-v1"]["reason"]


def test_a_lane_annotated_with_prose_still_reads_as_its_lane(tmp_path: Path) -> None:
    _registry(
        tmp_path / "projects",
        [{"reference_shortcode": "RefA", "project_root": "reel-ref-28-v1/", "lane": "VOLUME (mode call pending; may become CRAFT)"}],
    )

    index = ownership_index(tmp_path / "projects")

    assert index["reel-ref-28-v1"]["lane"] == "VOLUME"
    assert index["reel-ref-28-v1"]["daemon_owned"] is True


def test_a_project_root_outside_the_projects_root_is_not_indexed(tmp_path: Path) -> None:
    _registry(
        tmp_path / "projects",
        [
            {"reference_shortcode": "A", "project_root": "~/elsewhere/project-a/ (outside workspace)", "lane": "VOLUME"},
            {"reference_shortcode": "B", "project_root": "project-a/nested-child/", "lane": "VOLUME"},
            {"reference_shortcode": "C", "lane": "VOLUME"},
        ],
    )

    assert ownership_index(tmp_path / "projects") == {}


def test_a_project_with_no_registry_entry_is_unowned_but_not_forbidden(tmp_path: Path) -> None:
    _registry(tmp_path / "projects", [])

    assert ownership_index(tmp_path / "projects").get("demo") is None


# --- linking a reel to a project when project_root is absent ---------------
#
# The case §9.4 exists for: a CRAFT-lane reel on a LEGACY_QUARANTINED_BESPOKE_PIPELINE
# runtime that carries no project_root at all, so it never entered the index and "absent"
# read the same as "unrestricted".

SAMPLE_REELS = [
    {
        "reference_shortcode": "REEL-09",
        "lane": "CRAFT",
        "runtime": "LEGACY_QUARANTINED_BESPOKE_PIPELINE",
        "review_state": "R5_CAPTION_LAYER_LOCAL_REVIEW_READY__SIDE_BY_SIDE_IN_TODAY_QUEUE",
        "project_root": None,
    },
    {
        "reference_shortcode": "REEL-20",
        "lane": "VOLUME",
        "review_state": "CANDIDATE_QUARANTINED_REVIEW_ONLY",
        "project_root": "reel-ref-27-iphone-v1/",
    },
    {
        "reference_shortcode": "REEL-10",
        "lane": "VOLUME (mode call pending; 1:1 captions requested — may become CRAFT)",
        "review_state": "BOOTSTRAP_COMPLETE__CALL_REQUIRED_X3_AWAITING_OPERATOR",
        "project_root": "reel-ref-28-v1/",
    },
]


def test_a_quarantined_reel_with_no_project_root_is_linked_by_its_shortcode(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _registry(root, SAMPLE_REELS)

    index = ownership_index(root, project_ids=["REEL-09", "reel-ref-28-v1"])

    assert index["REEL-09"]["daemon_owned"] is False
    assert "LEGACY_QUARANTINED_BESPOKE_PIPELINE" in index["REEL-09"]["reason"]


def test_the_reel_project_naming_convention_links_a_reel_without_a_project_root(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _registry(root, [{"reference_shortcode": "REF-06", "runtime": "LEGACY_QUARANTINED_BESPOKE_PIPELINE"}])

    index = ownership_index(root, project_ids=["reel-ref-06-v1"])

    assert index["reel-ref-06-v1"]["daemon_owned"] is False


def test_linking_by_shortcode_never_invents_a_project_that_is_not_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _registry(root, SAMPLE_REELS)

    assert "REEL-09" not in ownership_index(root, project_ids=["reel-ref-28-v1"])


def test_an_explicit_project_root_still_wins_over_shortcode_matching(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _registry(root, SAMPLE_REELS)

    index = ownership_index(root, project_ids=["REEL-09", "reel-ref-27-iphone-v1", "reel-ref-28-v1"])

    assert index["reel-ref-27-iphone-v1"]["daemon_owned"] is False
    assert index["reel-ref-28-v1"]["daemon_owned"] is True
    assert sorted(index) == ["REEL-09", "reel-ref-27-iphone-v1", "reel-ref-28-v1"]


def test_the_verbatim_lane_string_survives_normalisation(tmp_path: Path) -> None:
    """RefA's lane carries a pending mode call; normalising to VOLUME must not destroy it."""
    root = tmp_path / "projects"
    _registry(root, SAMPLE_REELS)

    entry = ownership_index(root, project_ids=["reel-ref-28-v1"])["reel-ref-28-v1"]

    assert entry["lane"] == "VOLUME"
    assert entry["lane_declared"] == "VOLUME (mode call pending; 1:1 captions requested — may become CRAFT)"


def test_the_registry_names_who_holds_the_craft_slot(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    (root / "REEL_REGISTRY.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "reels": SAMPLE_REELS,
                "lanes": {"CRAFT": {"quota": "one active project at a time", "active": "REEL-09 (v10-r5 source-contours)"}},
            }
        ),
        encoding="utf-8",
    )

    assert declared_craft_holder(root) == "REEL-09"


def test_an_empty_craft_slot_holds_nobody(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _registry(root, SAMPLE_REELS)

    assert declared_craft_holder(root) is None


def test_an_unconfigured_agent_session_database_disables_the_check(tmp_path: Path) -> None:
    report = agent_session_activity("demo", database_path=None, now=NOW)

    assert report["checked"] is False
    assert report["active"] is False
    assert "REEL_STUDIO_AGENT_DB" in report["reason"]
