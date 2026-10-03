from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from test_studio_board_ui import request, studio_config

from reelctl.hashing import sha256_file
from reelctl.signing import verify_payload
from reelctl.state import STAGES, ProjectState
from reelctl.studio import calls as calls_module
from reelctl.studio import intake as intake_module
from reelctl.studio import jobs
from reelctl.studio import opencalls as opencalls_module
from reelctl.studio.board import project_ids
from reelctl.studio.config import StudioConfig
from reelctl.studio.daemon import _awaiting_operator
from reelctl.studio.events import STREAM_EVENT_NAME
from reelctl.studio.opencalls import open_calls_map
from reelctl.studio.registry import open_calls_by_project, registry_report
from reelctl.studio.stages import call_gate_index
from reelctl.web import create_app

# A card in the onboarding prompt's exact text schema, with two shapes real
# `brain/07_CALL_PROMPTS.md` files have and a naive parser would
# lose: a `reference_block` continued across lines, and options wrapped over several.
CARD_MARKDOWN = """# 07 — CALL PROMPTS — `reel-demo-v1`

Two unresolved operator decisions.

---

```text
CALL_REQUIRED
call_id: CALL-001-missing-roles
severity: BLOCKER
project_id: reel-demo-v1
revision: bootstrap-v1
reference_block: S02 (F047,F049-F054), S09 (F098-F106),
                 S11 (F116-F126)
issue_type: missing-role

WHAT THE REFERENCE REQUIRES:
Six of the reference's thirteen footage roles need scenes that do not exist in the
authorized library.

WHAT I FOUND:
Authorized corpus: FOOTAGE_LIBRARY.json (600 clips). "kayak" 0 hits, "desert" 0 hits.
Role coverage result: 0 EXACT / 7 EQUIVALENT / 6 MISSING.

WHY IT DOES NOT MATCH:
The gap is a world gap, not a grading gap.

EVIDENCE:
- brain/04_FEASIBILITY_MATRIX.md (the 13-role result)
- brain/06_MISMATCH_LEDGER.md sections A and C

OPTIONS:
A. RE-INTERPRET the six roles into the home world, keeping the clock and cuts exactly.
   Consequence: the picture becomes a disclosed adaptation.
B. BUILD ONLY the covered spine and stop. Consequence: no deliverable this cycle.
C. SUPPLY NEW AUTHORIZED FOOTAGE for the six roles. Consequence: the build waits.

MY RECOMMENDATION:
Option A. It honours the part specified as one-to-one while being honest.

REPLY WITH:
A, B, C, or: the specific roles you want preserved literally
```

---

```text
CALL_REQUIRED
call_id: CALL-002-craft-quota
severity: DECISION
project_id: reel-demo-v1
revision: bootstrap-v1
reference_block: whole reel
issue_type: mode-ambiguity

WHAT THE REFERENCE REQUIRES:
A lane decision.

WHAT I FOUND:
The CRAFT slot is held by REEL-09.

WHY IT DOES NOT MATCH:
Nothing has failed; the quota is a policy, not a defect.

EVIDENCE:
- REEL_REGISTRY.json lanes.CRAFT.active

OPTIONS:
A. Wait for the CRAFT slot. Consequence: this reel does not start today.
B. Run it as VOLUME. Consequence: no exact-scene fidelity claim.

MY RECOMMENDATION:
B, because the corpus has not been measured against exact-scene coverage.

REPLY WITH:
A, B, or: the lane you want
```
"""


def _registry(config: StudioConfig, payload: Dict[str, Any]) -> None:
    Path(config.registry_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config.registry_path).write_text(json.dumps(payload), encoding="utf-8")


PROJECT_JSON = {
    "schema_version": 1,
    "project_id": "reel-demo-v1",
    "mode": "reference-locked",
    "reference_input": "/reference-intake/reference_video.mp4",
    "reference_path": None,
    "footage_root": "/footage-library",
    "output": {"master_codec": "prores_ks", "master_pix_fmt": "yuv422p10le", "review_codec": "libx264", "color": "bt709"},
    "policies": {
        "audio": "licensed_track_required",
        "timing": "exact_reference_pts_and_picture_blocks",
        "typography": "exact_font_hash_or_traced_reference_glyph",
        "speed": "normal_only_no_reverse",
        "color": "identified_input_profile_then_per_shot_grade",
        "publication": "human_approval_required",
    },
}


def _project(config: StudioConfig, project_id: str, *, card: Optional[str] = CARD_MARKDOWN) -> Path:
    directory = Path(config.projects_root) / project_id
    for child in ("reference", "footage", "edit", "assets", "review", "deliver", "brain"):
        (directory / child).mkdir(parents=True, exist_ok=True)
    (directory / "project.json").write_text(json.dumps({**PROJECT_JSON, "project_id": project_id}), encoding="utf-8")
    ProjectState.create(directory / "state.json", project_id)
    if card is not None:
        (directory / "brain" / "07_CALL_PROMPTS.md").write_text(card.replace("reel-demo-v1", project_id), encoding="utf-8")
    return directory


def _open_call_entries(project_id: str, *call_ids: str) -> List[Dict[str, Any]]:
    return [
        {
            "call_id": call_id,
            "project": project_id,
            "card": "{}/brain/07_CALL_PROMPTS.md".format(project_id),
            "state": "AWAITING_OPERATOR",
        }
        for call_id in call_ids
    ]


def calls_app(
    tmp_path: Path,
    *,
    projects: Tuple[str, ...] = ("reel-demo-v1",),
    open_calls: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Any, StudioConfig]:
    config = studio_config(tmp_path)
    Path(config.projects_root).mkdir(parents=True, exist_ok=True)
    for project_id in projects:
        _project(config, project_id)
    entries = _open_call_entries(projects[0], "CALL-001-missing-roles", "CALL-002-craft-quota") if open_calls is None else open_calls
    _registry(config, {"schema_version": 2.0, "reels": [], "lanes": {}, "open_calls": entries})
    return create_app(config=config), config


# --- the card parser reads the onboarding schema, not a convenient subset ---


def test_every_field_of_the_onboarding_call_schema_survives_parsing() -> None:
    cards = calls_module.parse_call_cards(CARD_MARKDOWN)

    assert [card["call_id"] for card in cards] == ["CALL-001-missing-roles", "CALL-002-craft-quota"]
    first = cards[0]
    assert first["severity"] == "BLOCKER"
    assert first["project_id"] == "reel-demo-v1"
    assert first["revision"] == "bootstrap-v1"
    assert first["issue_type"] == "missing-role"
    # A wrapped reference_block is one value, not a truncated first line.
    assert "S02 (F047,F049-F054), S09 (F098-F106), S11 (F116-F126)" == first["reference_block"]
    assert first["what_the_reference_requires"].startswith("Six of the reference's thirteen")
    assert "0 EXACT / 7 EQUIVALENT / 6 MISSING" in first["what_i_found"]
    assert first["why_it_does_not_match"] == "The gap is a world gap, not a grading gap."
    assert first["evidence"] == [
        "brain/04_FEASIBILITY_MATRIX.md (the 13-role result)",
        "brain/06_MISMATCH_LEDGER.md sections A and C",
    ]
    assert [option["key"] for option in first["options"]] == ["A", "B", "C"]
    # A wrapped option keeps its consequence — the half that makes it a decision.
    assert "Consequence: the picture becomes a disclosed adaptation." in first["options"][0]["text"]
    assert first["recommendation"].startswith("Option A.")
    assert first["reply_with"].startswith("A, B, C, or:")


def test_the_parser_reads_a_real_call_card_if_one_is_provided() -> None:
    """Optional: point REEL_STUDIO_SAMPLE_CALL_CARD at a real 07_CALL_PROMPTS.md. Skipped otherwise."""
    configured = os.environ.get("REEL_STUDIO_SAMPLE_CALL_CARD")
    if not configured or not Path(configured).expanduser().is_file():
        pytest.skip("set REEL_STUDIO_SAMPLE_CALL_CARD to a real call-prompts file to run this check")

    cards = calls_module.parse_call_cards(Path(configured).expanduser().read_text(encoding="utf-8"))

    assert cards
    assert all(card["call_id"] for card in cards)
    assert all(card["severity"] in {"BLOCKER", "DECISION", "WARNING"} for card in cards)
    assert all(card["options"] for card in cards)
    assert all(card["reply_with"] for card in cards)


# --- the inbox ------------------------------------------------------------


def test_the_inbox_surfaces_registry_open_calls_in_the_onboarding_schema(tmp_path: Path) -> None:
    app, _ = calls_app(tmp_path)

    payload = request(app, "GET", "/api/calls").json()

    assert payload["status"] == "PASS"
    assert [call["call_id"] for call in payload["calls"]] == ["CALL-001-missing-roles", "CALL-002-craft-quota"]
    call = payload["calls"][0]
    assert call["project_id"] == "reel-demo-v1"
    assert call["state"] == "AWAITING_OPERATOR"
    assert call["issue_type"] == "missing-role"
    assert call["severity"] == "BLOCKER"
    assert [option["key"] for option in call["options"]] == ["A", "B", "C"]
    assert call["card_path"] == "brain/07_CALL_PROMPTS.md"
    assert call["source"] == "registry open_calls"


def test_a_registry_call_whose_card_body_is_missing_is_listed_and_says_so(tmp_path: Path) -> None:
    """A pointer with no card is still an open decision; hiding it would be the lie."""
    config = studio_config(tmp_path)
    Path(config.projects_root).mkdir(parents=True, exist_ok=True)
    _project(config, "reel-demo-v1", card=None)
    _registry(config, {"schema_version": 2.0, "reels": [], "lanes": {}, "open_calls": _open_call_entries("reel-demo-v1", "CALL-009")})
    app = create_app(config=config)

    payload = request(app, "GET", "/api/calls").json()

    call = payload["calls"][0]
    assert call["call_id"] == "CALL-009"
    assert call["options"] == []
    assert "brain/07_CALL_PROMPTS.md" in call["body_error"]
    assert call["card_sha256"] is None


def test_the_intake_mode_call_is_surfaced_without_a_registry_entry(tmp_path: Path) -> None:
    config = studio_config(tmp_path)
    Path(config.projects_root).mkdir(parents=True, exist_ok=True)
    directory = _project(config, "reel-demo-v1", card=None)
    card = {
        "call_id": "CALL-MODE-reel-demo-v1",
        "severity": "DECISION",
        "project_id": "reel-demo-v1",
        "issue_type": "mode-ambiguity",
        "state": "AWAITING_OPERATOR",
        "options": [{"key": "A", "text": "Build original-montage."}, {"key": "B", "text": "Build reference-locked."}],
        "reply_with": "A, B, C",
    }
    (directory / "intake.json").write_text(json.dumps({"project_id": "reel-demo-v1", "call": card}), encoding="utf-8")
    _registry(config, {"schema_version": 2.0, "reels": [], "lanes": {}, "open_calls": []})
    app = create_app(config=config)

    payload = request(app, "GET", "/api/calls").json()

    assert [call["call_id"] for call in payload["calls"]] == ["CALL-MODE-reel-demo-v1"]
    assert payload["calls"][0]["source"] == "intake record"
    assert payload["calls"][0]["issue_type"] == "mode-ambiguity"


def test_a_card_nothing_declares_open_is_named_rather_than_resurrected(tmp_path: Path) -> None:
    """``07_CALL_PROMPTS.md`` is append-only, so an answered call's card never leaves it."""
    app, config = calls_app(tmp_path)
    _registry(
        config,
        {"schema_version": 2.0, "reels": [], "lanes": {}, "open_calls": _open_call_entries("reel-demo-v1", "CALL-001-missing-roles")},
    )

    payload = request(app, "GET", "/api/calls").json()

    assert [call["call_id"] for call in payload["calls"]] == ["CALL-001-missing-roles"]
    assert payload["unlisted_cards"] == [
        {
            "project_id": "reel-demo-v1",
            "call_id": "CALL-002-craft-quota",
            "note": "brain/07_CALL_PROMPTS.md holds this card but neither the registry nor an intake record declares it open",
        }
    ]
    assert "CALL-002-craft-quota" in request(app, "GET", "/calls").text


def test_calls_are_scoped_to_the_project_that_owns_them(tmp_path: Path) -> None:
    app, config = calls_app(tmp_path, projects=("reel-a-v1", "reel-b-v1"))
    _registry(
        config,
        {
            "schema_version": 2.0,
            "reels": [],
            "lanes": {},
            "open_calls": _open_call_entries("reel-a-v1", "CALL-001-missing-roles") + _open_call_entries("reel-b-v1", "CALL-002-craft-quota"),
        },
    )

    payload = request(app, "GET", "/api/calls").json()
    owners = {call["call_id"]: call["project_id"] for call in payload["calls"]}

    assert owners == {"CALL-001-missing-roles": "reel-a-v1", "CALL-002-craft-quota": "reel-b-v1"}
    assert payload["by_project"]["reel-a-v1"] == ["CALL-001-missing-roles"]
    assert payload["by_project"]["reel-b-v1"] == ["CALL-002-craft-quota"]


# --- the gate stage travels with the call ---------------------------------


def test_a_card_header_declaring_its_gate_stage_is_parsed(tmp_path: Path) -> None:
    text = CARD_MARKDOWN.replace("issue_type: missing-role", "issue_type: missing-role\nstage: SELECTION_LOCKED")

    card = calls_module.parse_call_cards(text)[0]

    assert card["stage"] == "SELECTION_LOCKED"
    assert call_gate_index(card) == STAGES.index("SELECTION_LOCKED")


def test_the_intake_mode_calls_gate_stage_survives_into_the_inbox(tmp_path: Path) -> None:
    """The markdown card carries no `stage:` line, so the record must supply it.

    `intake.call_card_markdown` writes the onboarding prompt's header fields only, and
    `stage` is not one of them — so a body parsed back out of `07_CALL_PROMPTS.md` has no
    gate. Preferring that body over the intake record would hand the daemon a call that
    gates everything, which is the intake deadlock in one line.
    """
    config = studio_config(tmp_path)
    Path(config.projects_root).mkdir(parents=True, exist_ok=True)
    directory = _project(config, "reel-demo-v1", card=None)
    preview = {"recommendation": "corpus note", "library_clips_total": 600, "library_path": "/lib"}
    card = intake_module.mode_call_card(
        project_id="reel-demo-v1", reference="https://video.example/reel/ref-19/", lane="VOLUME", mode="original-montage", preview=preview
    )
    (directory / "brain" / "07_CALL_PROMPTS.md").write_text(intake_module.call_card_markdown(card), encoding="utf-8")
    (directory / "intake.json").write_text(json.dumps({"project_id": "reel-demo-v1", "call": card}), encoding="utf-8")
    _registry(config, {"schema_version": 2.0, "reels": [], "lanes": {}, "open_calls": []})

    entry = request(create_app(config=config), "GET", "/api/calls").json()["calls"][0]

    assert entry["stage"] == intake_module.MODE_CALL_STAGE == "SELECTION_LOCKED"
    assert call_gate_index(entry) == STAGES.index("SELECTION_LOCKED")


def test_the_registry_record_projection_preserves_the_gate_stage(tmp_path: Path) -> None:
    """Folding a call into the registry must be a copy, not a lossy reconstruction."""
    app, _ = calls_app(tmp_path)
    call = request(app, "GET", "/api/calls").json()["calls"][0]

    row = calls_module.registry_record(call)

    assert set(row) == {"call_id", "project", "card", "state", "stage"}
    assert row["call_id"] == "CALL-001-missing-roles"
    assert row["project"] == "reel-demo-v1"
    assert row["state"] == "AWAITING_OPERATOR"
    assert row["stage"] == call["stage"]


def test_intake_does_not_deadlock_on_its_own_mode_call(tmp_path: Path) -> None:
    """The whole point of the stage-scoped gate, proved against the real daemon function.

    Reference-lock, blueprint, footage-index and feasibility must all run while the mode
    call is open — the mode question is only answerable once feasibility has produced its
    numbers. Selection is the first stage the mode governs, so selection onward waits.
    """
    preview = {"recommendation": "corpus note", "library_clips_total": 600, "library_path": "/lib"}
    card = intake_module.mode_call_card(
        project_id="reel-demo-v1", reference="https://video.example/reel/ref-19/", lane="VOLUME", mode="original-montage", preview=preview
    )
    open_calls = [calls_module.registry_record({**card, "project_id": "reel-demo-v1", "card_path": calls_module.CALL_CARD_RELATIVE})]

    proceeding = ["REFERENCE_LOCKED", "BLUEPRINT_LOCKED", "FOOTAGE_INDEXED", "FEASIBILITY_REPORTED"]
    waiting = ["SELECTION_LOCKED", "ASSETS_LOCKED", "RENDERED", "LOCAL_REVIEW_READY"]

    for stage in proceeding:
        assert _awaiting_operator(open_calls, stage=stage) == [], "{} must run while the mode call is open".format(stage)
    for stage in waiting:
        assert _awaiting_operator(open_calls, stage=stage) == [card["call_id"]], "{} must wait".format(stage)


def test_the_inbox_states_which_stages_proceed_and_which_wait(tmp_path: Path) -> None:
    app, config = calls_app(tmp_path)
    _registry(
        config,
        {
            "schema_version": 2.0,
            "reels": [],
            "lanes": {},
            "open_calls": [
                {
                    "call_id": "CALL-001-missing-roles",
                    "project": "reel-demo-v1",
                    "card": "reel-demo-v1/brain/07_CALL_PROMPTS.md",
                    "state": "AWAITING_OPERATOR",
                    "stage": "SELECTION_LOCKED",
                }
            ],
        },
    )

    call = request(app, "GET", "/api/calls").json()["calls"][0]

    assert call["stage"] == "SELECTION_LOCKED"
    assert call["gates_from_stage"] == "SELECTION_LOCKED"
    assert call["stages_proceeding"] == ["REFERENCE_LOCKED", "BLUEPRINT_LOCKED", "FOOTAGE_INDEXED", "FEASIBILITY_REPORTED"]
    assert call["stages_waiting"][0] == "SELECTION_LOCKED"
    assert call["gates_everything"] is False


def test_a_call_with_no_declared_stage_gates_everything_and_says_so(tmp_path: Path) -> None:
    """Fails closed: an unrecognised gate must never read as an open door."""
    app, _ = calls_app(tmp_path)

    call = request(app, "GET", "/api/calls").json()["calls"][0]

    assert call["stage"] is None
    assert call["gates_everything"] is True
    assert call["stages_proceeding"] == []
    assert call["stages_waiting"] == list(STAGES)
    assert _awaiting_operator([calls_module.registry_record(call)], stage="REFERENCE_LOCKED") == ["CALL-001-missing-roles"]


def test_the_screen_states_the_gate_and_what_still_runs_under_it(tmp_path: Path) -> None:
    app, config = calls_app(tmp_path)
    _registry(
        config,
        {
            "schema_version": 2.0,
            "reels": [],
            "lanes": {},
            "open_calls": [
                {
                    "call_id": "CALL-001-missing-roles",
                    "project": "reel-demo-v1",
                    "card": "reel-demo-v1/brain/07_CALL_PROMPTS.md",
                    "state": "AWAITING_OPERATOR",
                    "stage": "SELECTION_LOCKED",
                }
            ],
        },
    )

    page = request(app, "GET", "/calls").text

    # Both facts, honestly: what is waiting, and what keeps running meanwhile.
    assert "gates SELECTION_LOCKED onward" in page
    assert "FEASIBILITY_REPORTED" in page


# --- the merged source the daemon and board need to gate on ---------------


def _studio_born(tmp_path: Path) -> Tuple[Any, StudioConfig, str]:
    """A project created the way the intake screen creates one: no registry entry."""
    from test_studio_intake import intake_app

    app, config, _ = intake_app(tmp_path)
    made = intake_module.create_intake(config, reference="https://video.example/reel/ref-19/", lane="VOLUME")
    return app, config, made["project_id"]


def test_a_studio_born_mode_call_is_invisible_to_the_registry_only_grouping(tmp_path: Path) -> None:
    """The defect this merge exists to fix, pinned so it cannot come back silently.

    `intake.py` deliberately never writes `REEL_REGISTRY.json` (§8.1 single writer), so a
    studio-born reel's mode call lives only in `<project>/intake.json`. Anything reading the
    registry alone sees no call at all.
    """
    _, config, project_id = _studio_born(tmp_path)

    registry_only = open_calls_by_project(registry_report(config.registry_path))

    assert registry_only.get(project_id, []) == []
    assert _awaiting_operator(registry_only.get(project_id, ()), stage="SELECTION_LOCKED") == []


def _inbox_by_project(config: StudioConfig) -> Dict[str, List[Dict[str, Any]]]:
    """Group the inbox by project, at the call site.

    These assertions are about `inbox`, so they read it directly. A production wrapper that
    did this grouping used to exist and was deleted: it sat between callers and the thing
    they were actually reading, and its only consumer was a test.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for call in calls_module.inbox(config, include_answered=True)["calls"]:
        grouped.setdefault(str(call["project_id"]), []).append(call)
    return grouped


def test_the_inbox_agrees_with_the_registry_grouping_on_a_declared_call(tmp_path: Path) -> None:
    """A drop-in: for calls the registry does declare, the gating decision is unchanged."""
    _, config = calls_app(tmp_path)
    _registry(
        config,
        {
            "schema_version": 2.0,
            "reels": [],
            "lanes": {},
            "open_calls": _open_call_entries("reel-demo-v1", "CALL-001-missing-roles"),
        },
    )

    registry_only = open_calls_by_project(registry_report(config.registry_path)).get("reel-demo-v1", [])
    from_inbox = _inbox_by_project(config).get("reel-demo-v1", [])

    assert [call["call_id"] for call in from_inbox] == [call["call_id"] for call in registry_only]
    for stage in STAGES:
        assert _awaiting_operator(from_inbox, stage=stage) == _awaiting_operator(registry_only, stage=stage)


def test_answering_stops_the_call_gating_the_reel(tmp_path: Path) -> None:
    """The answer path is this module's, so its effect on the gate is pinned here."""
    app, config, project_id = _studio_born(tmp_path)
    call_id = "CALL-MODE-{}".format(project_id)

    before = _inbox_by_project(config)[project_id]
    assert _awaiting_operator(before, stage="SELECTION_LOCKED") == [call_id]
    assert request(app, "POST", "/api/calls/{}/answer".format(call_id), json_body={"option": "A"}).status_code == 201

    after = _inbox_by_project(config)[project_id]
    assert after[0]["state"] == "ANSWERED"
    assert _awaiting_operator(after, stage="SELECTION_LOCKED") == []


def test_the_inbox_and_the_shared_assembly_agree_on_every_open_call(tmp_path: Path) -> None:
    """Cross-assembly tripwire. Two assemblies of the same fact is the failure we keep hitting.

    `opencalls` is THE assembly; `calls.inbox` adds presentation on top. They agree here by
    construction, and this test exists so that stops being a claim: if anyone reintroduces a
    second source-reading path in `calls.py`, the ids or the gating decision will drift and
    this fails rather than the board and daemon silently disagreeing in production.
    """
    _, config, studio_born = _studio_born(tmp_path)
    _project(config, "reel-declared-v1")
    _registry(
        config,
        {
            "schema_version": 2.0,
            "reels": [],
            "lanes": {},
            "open_calls": _open_call_entries("reel-declared-v1", "CALL-001-missing-roles", "CALL-002-craft-quota"),
        },
    )

    mine = _inbox_by_project(config)
    theirs = open_calls_map(registry_report(config.registry_path), config.projects_root, project_ids(config.projects_root))

    assert set(mine) == {project_id for project_id, rows in theirs.items() if rows}
    for project_id, rows in mine.items():
        assert [call["call_id"] for call in rows] == [call["call_id"] for call in theirs[project_id]], project_id
        for stage in STAGES:
            assert _awaiting_operator(rows, stage=stage) == _awaiting_operator(theirs[project_id], stage=stage), (project_id, stage)
    assert studio_born in mine


def test_the_inbox_reads_its_calls_through_the_shared_assembly(tmp_path: Path, monkeypatch: Any) -> None:
    """Delegation, proved by substitution: patch the assembly and the inbox must follow.

    If `calls.py` still read the registry or `intake.json` itself, a replaced assembly would
    leave the inbox unchanged and this would fail — which is exactly the duplication the
    consolidation removes.
    """
    app, config = calls_app(tmp_path)
    sentinel = [{"call_id": "CALL-FROM-THE-ASSEMBLY", "state": "AWAITING_OPERATOR", "stage": "RENDERED", "source": "registry open_calls"}]
    monkeypatch.setattr(opencalls_module, "project_open_calls", lambda report, project_dir: list(sentinel))

    listed = request(app, "GET", "/api/calls").json()["calls"]

    assert [call["call_id"] for call in listed] == ["CALL-FROM-THE-ASSEMBLY"]
    assert listed[0]["gates_from_stage"] == "RENDERED"


# --- answering ------------------------------------------------------------


def test_an_answer_is_a_hash_bound_signed_receipt(tmp_path: Path) -> None:
    app, config = calls_app(tmp_path)
    card_path = Path(config.projects_root) / "reel-demo-v1" / "brain" / "07_CALL_PROMPTS.md"

    response = request(
        app,
        "POST",
        "/api/calls/CALL-001-missing-roles/answer",
        json_body={"option": "A", "text": "keep the clock, re-interpret the picture", "reviewer": "operator"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    receipt = json.loads(Path(body["receipt"]).read_text(encoding="utf-8"))
    assert receipt["call_id"] == "CALL-001-missing-roles"
    assert receipt["project_id"] == "reel-demo-v1"
    assert receipt["option"] == "A"
    assert receipt["option_text"].startswith("RE-INTERPRET the six roles")
    assert receipt["answer_text"] == "keep the clock, re-interpret the picture"
    assert receipt["reviewer"] == "operator"
    assert receipt["card_sha256"] == sha256_file(card_path)
    assert receipt["call_sha256"] and receipt["call_sha256"] != receipt["card_sha256"]
    assert verify_payload(receipt, purpose="studio-call-answer-v1")["status"] == "PASS"


def test_an_answer_is_bound_to_the_question_that_was_asked(tmp_path: Path) -> None:
    """Rewriting the card after the answer must not leave the answer looking current."""
    app, config = calls_app(tmp_path)
    card_path = Path(config.projects_root) / "reel-demo-v1" / "brain" / "07_CALL_PROMPTS.md"

    request(app, "POST", "/api/calls/CALL-001-missing-roles/answer", json_body={"option": "A"})
    before = calls_module.call_answers(Path(config.projects_root) / "reel-demo-v1")["CALL-001-missing-roles"]
    card_path.write_text(CARD_MARKDOWN.replace("0 EXACT / 7 EQUIVALENT / 6 MISSING", "13 EXACT / 0 EQUIVALENT / 0 MISSING"), encoding="utf-8")

    payload = request(app, "GET", "/api/calls?include_answered=true").json()
    answered = next(call for call in payload["calls"] if call["call_id"] == "CALL-001-missing-roles")

    assert before["call_sha256"] != answered["call_sha256"]
    assert answered["answer"]["binds_current_card"] is False
    assert "the card changed after this answer" in answered["answer"]["binding_note"]


def test_an_answer_is_appended_to_the_owning_projects_decision_log(tmp_path: Path) -> None:
    app, config = calls_app(tmp_path)
    log = Path(config.projects_root) / "reel-demo-v1" / "brain" / "08_DECISION_LOG.md"
    log.write_text("# 08 — DECISION LOG\n\nAppend-only.\n\n**D-existing** — a prior entry.\n", encoding="utf-8")

    request(app, "POST", "/api/calls/CALL-001-missing-roles/answer", json_body={"option": "B"})
    text = log.read_text(encoding="utf-8")

    assert "**D-existing** — a prior entry." in text  # append-only: nothing rewritten
    assert "CALL-001-missing-roles" in text
    assert "answered: B" in text
    assert "review/calls/CALL-001-missing-roles.answer.json" in text


def test_the_decision_log_is_created_when_the_project_has_none(tmp_path: Path) -> None:
    app, config = calls_app(tmp_path)
    log = Path(config.projects_root) / "reel-demo-v1" / "brain" / "08_DECISION_LOG.md"
    assert not log.is_file()

    request(app, "POST", "/api/calls/CALL-001-missing-roles/answer", json_body={"option": "B"})

    assert log.read_text(encoding="utf-8").startswith("# 08 — DECISION LOG")


def test_an_answered_call_leaves_the_open_inbox(tmp_path: Path) -> None:
    app, _ = calls_app(tmp_path)

    request(app, "POST", "/api/calls/CALL-001-missing-roles/answer", json_body={"option": "A"})
    open_now = request(app, "GET", "/api/calls").json()
    everything = request(app, "GET", "/api/calls?include_answered=true").json()

    assert [call["call_id"] for call in open_now["calls"]] == ["CALL-002-craft-quota"]
    assert [call["call_id"] for call in everything["calls"]] == ["CALL-001-missing-roles", "CALL-002-craft-quota"]
    answered = everything["calls"][0]
    assert answered["state"] == "ANSWERED"
    assert answered["answer"]["option"] == "A"


def test_a_second_answer_supersedes_rather_than_overwrites(tmp_path: Path) -> None:
    app, config = calls_app(tmp_path)

    first = request(app, "POST", "/api/calls/CALL-001-missing-roles/answer", json_body={"option": "A"}).json()
    second = request(app, "POST", "/api/calls/CALL-001-missing-roles/answer", json_body={"option": "C", "text": "new footage is coming"})

    assert second.status_code == 201
    body = second.json()
    assert Path(first["receipt"]).is_file(), "the first answer must survive on disk"
    assert body["receipt"] != first["receipt"]
    receipt = json.loads(Path(body["receipt"]).read_text(encoding="utf-8"))
    assert receipt["supersedes"] == Path(first["receipt"]).name
    assert receipt["sequence"] == 2
    latest = request(app, "GET", "/api/calls?include_answered=true").json()["calls"][0]
    assert latest["answer"]["option"] == "C"


def test_an_option_the_card_does_not_offer_is_refused_with_the_valid_keys(tmp_path: Path) -> None:
    app, _ = calls_app(tmp_path)

    response = request(app, "POST", "/api/calls/CALL-002-craft-quota/answer", json_body={"option": "C"})

    assert response.status_code == 400
    assert "C" in response.json()["error"]
    assert "A, B" in response.json()["error"]


def test_an_empty_answer_is_refused(tmp_path: Path) -> None:
    app, _ = calls_app(tmp_path)

    response = request(app, "POST", "/api/calls/CALL-001-missing-roles/answer", json_body={})

    assert response.status_code == 400
    assert "option" in response.json()["error"]


def test_an_unknown_call_is_refused_by_name(tmp_path: Path) -> None:
    app, _ = calls_app(tmp_path)

    response = request(app, "POST", "/api/calls/CALL-404/answer", json_body={"option": "A"})

    assert response.status_code == 400
    assert "CALL-404" in response.json()["error"]


def test_free_text_alone_is_a_valid_answer(tmp_path: Path) -> None:
    app, _ = calls_app(tmp_path)

    response = request(
        app,
        "POST",
        "/api/calls/CALL-001-missing-roles/answer",
        json_body={"text": "none of these; I will shoot the missing roles next week"},
    )

    assert response.status_code == 201
    receipt = json.loads(Path(response.json()["receipt"]).read_text(encoding="utf-8"))
    assert receipt["option"] is None
    assert receipt["option_text"] is None
    assert receipt["answer_text"].startswith("none of these")


# --- job release ----------------------------------------------------------


def _stall(config: StudioConfig, project_id: str, *, stage: str = "BLUEPRINT_LOCKED") -> int:
    job_id = jobs.enqueue(config.database_path, project_id=project_id, stage=stage, kind="judgment", input_hash="fingerprint-1")
    assert job_id is not None
    jobs.finish(config.database_path, job_id, status="FAILED", reason="stalled twice on the same input")
    assert jobs.is_terminally_failed(config.database_path, project_id=project_id, stage=stage, input_hash="fingerprint-1")
    return job_id


def test_answering_releases_the_stalled_job_so_the_daemon_resumes_that_project(tmp_path: Path) -> None:
    app, config = calls_app(tmp_path)
    _stall(config, "reel-demo-v1")

    body = request(app, "POST", "/api/calls/CALL-001-missing-roles/answer", json_body={"option": "A"}).json()

    assert body["released_jobs"] == 1
    assert not jobs.is_terminally_failed(
        config.database_path, project_id="reel-demo-v1", stage="BLUEPRINT_LOCKED", input_hash="fingerprint-1"
    )


def test_an_unanswered_call_never_blocks_an_unrelated_reel(tmp_path: Path) -> None:
    app, config = calls_app(tmp_path, projects=("reel-a-v1", "reel-b-v1"))
    _registry(
        config,
        {
            "schema_version": 2.0,
            "reels": [],
            "lanes": {},
            "open_calls": _open_call_entries("reel-a-v1", "CALL-001-missing-roles"),
        },
    )
    _stall(config, "reel-a-v1")
    stalled_b = _stall(config, "reel-b-v1")

    calls = request(app, "GET", "/api/calls").json()
    body = request(app, "POST", "/api/calls/CALL-001-missing-roles/answer", json_body={"option": "A"}).json()

    # reel-b-v1 has no open call at all, and reel-a-v1's answer touches only reel-a-v1.
    assert [call["project_id"] for call in calls["calls"]] == ["reel-a-v1"]
    assert calls["by_project"].get("reel-b-v1") is None
    assert body["released_jobs"] == 1
    assert jobs.get_job(config.database_path, stalled_b)["status"] == "FAILED"
    assert jobs.is_terminally_failed(
        config.database_path, project_id="reel-b-v1", stage="BLUEPRINT_LOCKED", input_hash="fingerprint-1"
    )


def test_answering_records_a_call_event_for_the_live_board(tmp_path: Path) -> None:
    app, config = calls_app(tmp_path)

    request(app, "POST", "/api/calls/CALL-001-missing-roles/answer", json_body={"option": "A"})
    events = request(app, "GET", "/api/stream?after=0&max_seconds=0.05&poll_ms=10").text

    # Every frame carries the one constant SSE name; the row's real kind is in the payload.
    assert "event: {}".format(STREAM_EVENT_NAME) in events
    assert '"kind": "call"' in events
    assert "CALL-001-missing-roles" in events


# --- the screen -----------------------------------------------------------


def test_the_inbox_screen_renders_the_card_body_and_its_options(tmp_path: Path) -> None:
    app, _ = calls_app(tmp_path)

    page = request(app, "GET", "/calls").text

    assert "CALL-001-missing-roles" in page
    assert "0 EXACT / 7 EQUIVALENT / 6 MISSING" in page
    assert "RE-INTERPRET the six roles" in page
    assert 'value="A"' in page
    assert "BLOCKER" in page


def test_the_inbox_screen_escapes_a_card_that_carries_markup(tmp_path: Path) -> None:
    config = studio_config(tmp_path)
    Path(config.projects_root).mkdir(parents=True, exist_ok=True)
    _project(config, "reel-demo-v1", card=CARD_MARKDOWN.replace("a world gap", "<script>alert('x')</script>"))
    _registry(
        config,
        {"schema_version": 2.0, "reels": [], "lanes": {}, "open_calls": _open_call_entries("reel-demo-v1", "CALL-001-missing-roles")},
    )
    app = create_app(config=config)

    page = request(app, "GET", "/calls").text

    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in page


def test_the_inbox_screen_says_so_when_nothing_is_waiting(tmp_path: Path) -> None:
    config = studio_config(tmp_path)
    Path(config.projects_root).mkdir(parents=True, exist_ok=True)
    _registry(config, {"schema_version": 2.0, "reels": [], "lanes": {}, "open_calls": []})
    app = create_app(config=config)

    page = request(app, "GET", "/calls").text

    assert "No open calls" in page
