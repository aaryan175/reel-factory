from __future__ import annotations

import sqlite3
from argparse import Namespace
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .cli import (
    _project,
    command_assets_lock,
    command_assets_template,
    command_blueprint_lock,
    command_feasibility_lock,
    command_feasibility_template,
    command_footage_index,
    command_new,
    command_qc,
    command_reference_analyze,
    command_render,
    command_review_record_agent,
    command_run,
    command_selection_template,
    command_selection_validate,
)
from .errors import ReelctlError
from .hashing import atomic_write_json, load_json, sha256_file
from .identifiers import IdentifierError
from .locks import LockError, ProjectBusyError, project_lock
from .paths import PathSafetyError, canonical_root, confined_path
from .state import STAGES, ProjectState, StateError
from .studio.board import project_summaries, project_summary
from .studio.authority import authority_decision
from .studio.calls import answer_call
from .studio.calls import inbox as calls_inbox
from .studio.config import DEFAULT_PORT, StudioConfig
from .studio.db import connect as connect_studio_database
from .studio.db import initialize as initialize_studio_database
from .studio.events import (
    DEFAULT_STREAM_SECONDS,
    MAX_STREAM_SECONDS,
    EventLogError,
    record_event,
    sse_stream,
)
from .studio.health import health_report
from .studio.intake import create_intake, plan_intake
from .studio.publish import publish_card, record_approval
from .studio.review import record_verdict, review_bundle
from .studio.status import project_layers
from .studio.views import (
    board_payload,
    calls_payload,
    intake_payload,
    publish_payload,
    render_board_page,
    render_calls_page,
    render_intake_page,
    render_publish_page,
    render_review_page,
    review_payload,
)

MODES = ["original-montage", "reference-locked"]
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
ACTION_NAMES = [
    "reference_analyze",
    "blueprint_lock",
    "footage_index",
    "feasibility_template",
    "feasibility_lock",
    "selection_template",
    "selection_validate",
    "assets_template",
    "assets_lock",
    "pipeline_run",
    "render",
    "qc",
    "agent_review",
]
ARTIFACT_ROOTS = {"reference", "footage", "edit", "assets", "review", "deliver"}
STUDIO_TABLES = ("jobs", "events", "schedules", "status_cache")


def _missing_studio_tables(path: Path) -> list:
    if not Path(path).is_file():
        return list(STUDIO_TABLES)
    with connect_studio_database(path) as connection:
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return [table for table in STUDIO_TABLES if table not in present]


def _studio_database_report(path: Path) -> Dict[str, Any]:
    """Probe the studio database on this request, never from a startup snapshot.

    Every other field in ``/api/health`` is re-probed per request — mounts, free bytes,
    binaries, the daemon heartbeat. Serving a value captured at startup would leave one
    field in the strip reporting a healthy database indefinitely after the disk filled or
    the volume went away, which is the stale-green shape this whole app exists to refuse.

    Repair is attempted because §8.1 makes the database disposable: a missing schema is a
    thing to recreate and say so, not a thing to fail on. ``repaired`` is reported rather
    than hidden, so a database that keeps needing recreation is visible as a symptom.
    """
    report: Dict[str, Any] = {"path": str(path), "status": "PASS", "error": None, "repaired": False}
    try:
        missing = _missing_studio_tables(path)
        if missing:
            initialize_studio_database(path)
            report["repaired"] = True
            missing = _missing_studio_tables(path)
        if missing:
            report.update(status="FAIL", error=f"studio database at {path} is missing tables: {', '.join(missing)}")
    except (sqlite3.Error, OSError) as exc:
        report.update(status="FAIL", error=f"studio database at {path} is unusable: {exc}")
    return report


RECOMMENDED_ACTIONS = {
    "REFERENCE_LOCKED": "reference_analyze",
    "BLUEPRINT_LOCKED": "blueprint_lock",
    "FOOTAGE_INDEXED": "footage_index",
    "FEASIBILITY_REPORTED": "feasibility_lock",
    "SELECTION_LOCKED": "selection_validate",
    "ASSETS_LOCKED": "assets_lock",
    "RENDERED": "render",
    "TECHNICAL_QC": "qc",
    "STRUCTURE_QC": "qc",
    "VISUAL_QC": "qc",
    "LOCAL_REVIEW_READY": "agent_review",
}


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=80)
    reference: str = Field(min_length=1)
    footage: str = Field(min_length=1)
    mode: Literal["original-montage", "reference-locked"]


class IntakeRequest(BaseModel):
    """The front door. One box, three defaulted fields — and no mode, on purpose."""

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1)
    lane: Literal["VOLUME", "CRAFT"] = "VOLUME"
    project_id: Optional[str] = Field(default=None, min_length=1, max_length=80)
    footage_root: Optional[str] = Field(default=None, min_length=1)
    world: Optional[str] = Field(default=None, min_length=1)


class ApprovalCardRequest(BaseModel):
    """The operator's completed approval card. Recording it performs no outward action.

    The four required fields are the ones the operating-system contract says an approval is
    meaningless without: which account, what caption, when, and in which timezone. The rest
    may legitimately read "unset" — a scheduling tool may expose no cover or expansion control — but
    they are recorded as the operator stated them rather than defaulted.
    """

    model_config = ConfigDict(extra="forbid")

    destination: str = Field(min_length=1, max_length=400)
    caption: str = Field(min_length=1, max_length=2200)
    schedule: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=120)
    internal_note: Optional[str] = Field(default=None, max_length=400)
    audio_label: Optional[str] = Field(default=None, max_length=200)
    cover: Optional[str] = Field(default=None, max_length=400)
    disclosure: Optional[str] = Field(default=None, max_length=400)
    auto_expansion: Optional[bool] = None
    reviewer: Optional[str] = Field(default=None, min_length=1, max_length=120)


class FeedbackEvent(BaseModel):
    """The structured rejection the doctrine requires, and the body of the negative fixture.

    Every field is mandatory because "the clips are not perfect" is the exact vague call the
    onboarding prompt forbids: a fixture without a target, an observation, a requested change
    and a scope teaches the factory nothing it can act on later.
    """

    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, max_length=400)
    observation: str = Field(min_length=1, max_length=4000)
    requested_change: str = Field(min_length=1, max_length=4000)
    scope: str = Field(min_length=1, max_length=200)


class VerdictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["approve", "reject", "changes"]
    feedback: Optional[FeedbackEvent] = None
    notes: Optional[str] = Field(default=None, max_length=4000)
    reviewer: Optional[str] = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def _feedback_is_mandatory_for_a_negative_verdict(self) -> "VerdictRequest":
        if self.verdict in {"reject", "changes"} and self.feedback is None:
            raise ValueError(
                "a reject or changes verdict requires a structured FeedbackEvent "
                "(target, observation, requested_change, scope); it becomes the permanent negative fixture"
            )
        return self


class CallAnswerRequest(BaseModel):
    """An operator answer to one ``CALL_REQUIRED`` card: an option, free text, or both."""

    model_config = ConfigDict(extra="forbid")

    option: Optional[str] = Field(default=None, min_length=1, max_length=8)
    text: Optional[str] = Field(default=None, max_length=4000)
    reviewer: Optional[str] = Field(default=None, min_length=1, max_length=120)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str = Field(default="v001", min_length=1, max_length=80)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "reference_analyze",
        "blueprint_lock",
        "footage_index",
        "feasibility_template",
        "feasibility_lock",
        "selection_template",
        "selection_validate",
        "assets_template",
        "assets_lock",
        "pipeline_run",
        "render",
        "qc",
        "agent_review",
    ]
    payload: Dict[str, Any] = Field(default_factory=dict)


def _namespace(projects_root: Path, **values: Any) -> Namespace:
    return Namespace(projects_root=projects_root, **values)


def _load_index_html() -> str:
    path = Path(__file__).with_name("static") / "index.html"
    if not path.is_file():
        return "<h1>Reel Studio</h1><p>Static dashboard asset is missing.</p>"
    return path.read_text(encoding="utf-8")


def _as_bool(payload: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ReelctlError(f"action field {key!r} must be boolean")
    return value


def _as_string(payload: Dict[str, Any], key: str, default: Optional[str] = None) -> Optional[str]:
    value = payload.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReelctlError(f"action field {key!r} must be a non-empty string")
    return value


def _csv_value(payload: Dict[str, Any], key: str) -> Optional[str]:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(item, int) for item in value):
        return ",".join(str(item) for item in value)
    if isinstance(value, str):
        return value
    raise ReelctlError(f"action field {key!r} must be a comma-separated string or integer array")


def _write_action_json(root: Path, project_id: str, relative: str, value: Any) -> str:
    directory = _project(root, project_id)
    path = directory / relative
    atomic_write_json(path, value, root=directory)
    return str(path)


def _refuse_outward_reference(root: Path, project_id: str, source: Optional[str]) -> None:
    """Close the one path by which a route could reach the network.

    ``reelctl reference analyze`` locks the reference, and locking a ``http``/``https``
    reference means *downloading* it with yt-dlp. Reached through the allowlisted
    ``reference_analyze`` action, that made the control plane able to perform an outward
    call — the rule §5.2 states for every route. The daemon locks URL references (it drives
    reelctl as a subprocess, outside the app); a local path here is unaffected.
    """
    effective = source
    if effective is None:
        try:
            directory = _project(root, project_id)
            effective = load_json(directory / "project.json", root=directory).get("reference_input")
        except (ReelctlError, IdentifierError, PathSafetyError, OSError, ValueError):
            return
    if isinstance(effective, str) and urlparse(effective).scheme in {"http", "https"}:
        raise ReelctlError(
            f"refusing to analyze reference {effective}: locking a URL reference downloads it, and no route in this app performs "
            "an outward network call. The daemon locks URL references by driving reelctl outside the app; pass a local path here."
        )


def _action_args(root: Path, project_id: str, action: str, payload: Dict[str, Any]) -> Namespace:
    base = {"project_id": project_id}
    if action == "reference_analyze":
        source = _as_string(payload, "source")
        _refuse_outward_reference(root, project_id, source)
        return _namespace(root, **base, source=source)
    if action == "blueprint_lock":
        observations = payload.get("observations")
        observation_path = None
        if observations is not None:
            if not isinstance(observations, dict):
                raise ReelctlError("blueprint observations must be a JSON object")
            observation_path = _write_action_json(root, project_id, "reference/observations.api.json", observations)
        return _namespace(
            root,
            **base,
            boundaries=_csv_value(payload, "boundaries"),
            hard_cuts=_csv_value(payload, "hard_cuts"),
            accept_draft=_as_bool(payload, "accept_draft"),
            observations=observation_path,
            all_frames_reviewed=_as_bool(payload, "all_frames_reviewed"),
        )
    if action == "footage_index":
        return _namespace(
            root, **base, footage=_as_string(payload, "footage"), default_profile=_as_string(payload, "default_profile", "log_unknown")
        )
    if action == "feasibility_template":
        return _namespace(root, **base, force=_as_bool(payload, "force"))
    if action == "feasibility_lock":
        manifest = payload.get("manifest")
        manifest_path = None
        if manifest is not None:
            if not isinstance(manifest, dict):
                raise ReelctlError("feasibility manifest must be a JSON object")
            manifest_path = _write_action_json(root, project_id, "edit/feasibility.api.json", manifest)
        return _namespace(root, **base, manifest=manifest_path)
    if action == "selection_template":
        return _namespace(root, **base, force=_as_bool(payload, "force"))
    if action == "selection_validate":
        selection = payload.get("selection")
        selection_path = None
        if selection is not None:
            if not isinstance(selection, dict):
                raise ReelctlError("selection must be a JSON object")
            selection_path = _write_action_json(root, project_id, "edit/selection.api.json", selection)
        return _namespace(root, **base, selection=selection_path)
    if action == "assets_template":
        return _namespace(root, **base, force=_as_bool(payload, "force"))
    if action == "assets_lock":
        manifest = payload.get("manifest")
        manifest_path = None
        if manifest is not None:
            if not isinstance(manifest, dict):
                raise ReelctlError("assets manifest must be a JSON object")
            manifest_path = _write_action_json(root, project_id, "assets/assets.api.json", manifest)
        return _namespace(root, **base, manifest=manifest_path)
    if action == "pipeline_run":
        return _namespace(
            root,
            **base,
            through="local-review",
            revision=_as_string(payload, "revision", "v001"),
            default_profile=_as_string(payload, "default_profile", "log_unknown"),
        )
    if action == "render":
        return _namespace(root, **base, revision=_as_string(payload, "revision", "v001"))
    if action == "qc":
        return _namespace(root, **base, revision=_as_string(payload, "revision", "v001"))
    if action == "agent_review":
        status = _as_string(payload, "status", "FAIL")
        if status not in {"PASS", "FAIL"}:
            raise ReelctlError("agent review status must be PASS or FAIL")
        return _namespace(
            root,
            **base,
            revision=_as_string(payload, "revision", "v001"),
            status=status,
            normal_speed_full_watch=_as_bool(payload, "normal_speed_full_watch"),
            reference_side_by_side_checked=_as_bool(payload, "reference_side_by_side_checked"),
            typography_checked=_as_bool(payload, "typography_checked"),
            color_checked=_as_bool(payload, "color_checked"),
            cut_and_beat_checked=_as_bool(payload, "cut_and_beat_checked"),
            notes=_as_string(payload, "notes"),
        )
    raise ReelctlError(f"action {action!r} is not allowlisted")


def _dispatch_action(root: Path, project_id: str, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    args = _action_args(root, project_id, action, payload)
    commands = {
        "reference_analyze": command_reference_analyze,
        "blueprint_lock": command_blueprint_lock,
        "footage_index": command_footage_index,
        "feasibility_template": command_feasibility_template,
        "feasibility_lock": command_feasibility_lock,
        "selection_template": command_selection_template,
        "selection_validate": command_selection_validate,
        "assets_template": command_assets_template,
        "assets_lock": command_assets_lock,
        "pipeline_run": command_run,
        "render": command_render,
        "qc": command_qc,
        "agent_review": command_review_record_agent,
    }
    return commands[action](args)


def create_app(*, projects_root: Optional[Path] = None, config: Optional[StudioConfig] = None) -> FastAPI:
    settings = config or StudioConfig.from_env()
    if projects_root is not None:
        settings = settings.with_projects_root(projects_root)
    root = canonical_root(settings.projects_root, create=True)
    app = FastAPI(
        title="Reel Studio",
        version=__version__,
        description="Local, fail-closed control plane for reelctl.",
    )
    app.state.projects_root = root
    app.state.config = settings

    @app.middleware("http")
    async def factory_authority_guard(request: Request, call_next: Any) -> Any:
        # Review surfaces remain live; every HTTP mutation is stopped before route code,
        # project locks, receipts, events, or project files can be touched.
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            decision = authority_decision()
            if not decision.allowed:
                return JSONResponse(
                    status_code=423,
                    content={"status": "GLOBAL_HALT", "detail": decision.reason},
                )
        return await call_next(request)

    # A studio database that was opened but never initialised is an empty *file*, not an
    # empty schema, and the first write against it fails with "no such table". Creating the
    # schema once at startup means no caller has to get the ordering right. It is
    # best-effort and its outcome is deliberately NOT cached: /api/health re-probes per
    # request, so a database that breaks after startup is reported rather than remembered.
    try:
        initialize_studio_database(settings.database_path)
    except (sqlite3.Error, OSError):
        pass

    @app.exception_handler(ReelctlError)
    async def reelctl_error(_: Request, exc: ReelctlError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"status": "FAIL", "error": str(exc), "error_type": type(exc).__name__})

    @app.exception_handler(IdentifierError)
    async def identifier_error(_: Request, exc: IdentifierError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"status": "FAIL", "error": str(exc), "error_type": type(exc).__name__})

    @app.exception_handler(PathSafetyError)
    async def path_safety_error(_: Request, exc: PathSafetyError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"status": "FAIL", "error": str(exc), "error_type": type(exc).__name__})

    @app.exception_handler(ProjectBusyError)
    async def project_busy(_: Request, exc: ProjectBusyError) -> JSONResponse:
        # Another operator or the daemon owns this project right now (§9.1). That is an
        # expected condition, not a crash: say so and let the caller retry.
        return JSONResponse(status_code=409, content={"status": "FAIL", "error": str(exc), "error_type": type(exc).__name__})

    @app.exception_handler(LockError)
    async def lock_error(_: Request, exc: LockError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"status": "FAIL", "error": str(exc), "error_type": type(exc).__name__})

    @app.exception_handler(FileNotFoundError)
    async def missing_file(_: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"status": "FAIL", "error": str(exc), "error_type": type(exc).__name__})

    def publish(kind: str, project_id: Optional[str] = None, **payload: Any) -> Optional[str]:
        """Append one event. A broken log never undoes work that already succeeded.

        The stage advance is already on disk with its receipt by the time this runs, so
        raising here would report a completed mutation as a failure. The error is returned
        and surfaced in the response instead of being swallowed.
        """
        try:
            record_event(settings.database_path, kind=kind, project_id=project_id, payload=payload)
        except EventLogError as exc:
            return str(exc)
        return None

    def next_stage_of(project_id: str) -> Optional[str]:
        try:
            return project_summary(root, project_id).get("next_stage")
        except (ReelctlError, StateError, OSError, ValueError):
            return None

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        return _load_index_html()

    @app.get("/board", response_class=HTMLResponse, include_in_schema=False)
    def board_screen() -> str:
        return render_board_page(board_payload(settings))

    @app.get("/intake", response_class=HTMLResponse, include_in_schema=False)
    def intake_screen() -> str:
        return render_intake_page(intake_payload(settings))

    @app.get("/api/intake/plan")
    def intake_plan(
        reference: str,
        lane: Literal["VOLUME", "CRAFT"] = "VOLUME",
        project_id: Optional[str] = None,
        footage_root: Optional[str] = None,
        world: Optional[str] = None,
    ) -> Dict[str, Any]:
        return plan_intake(
            settings, reference=reference, lane=lane, project_id=project_id, footage_root=footage_root, world=world
        )

    @app.post("/api/intake", status_code=201)
    def intake(payload: IntakeRequest) -> Dict[str, Any]:
        return create_intake(
            settings,
            reference=payload.reference,
            lane=payload.lane,
            project_id=payload.project_id,
            footage_root=payload.footage_root,
            world=payload.world,
        )

    @app.get("/calls", response_class=HTMLResponse, include_in_schema=False)
    def calls_screen(include_answered: bool = False) -> str:
        return render_calls_page(calls_payload(settings, include_answered=include_answered))

    @app.get("/api/calls")
    def calls(include_answered: bool = False) -> Dict[str, Any]:
        return calls_inbox(settings, include_answered=include_answered)

    @app.post("/api/calls/{call_id}/answer", status_code=201)
    def answer(call_id: str, payload: CallAnswerRequest) -> Dict[str, Any]:
        return answer_call(settings, call_id, option=payload.option, text=payload.text, reviewer=payload.reviewer)

    @app.get("/api/reels")
    def reels() -> Dict[str, Any]:
        return board_payload(settings)

    @app.get("/api/reels/{project_id}/layers")
    def reel_layers(project_id: str) -> Dict[str, Any]:
        directory = _project(root, project_id)
        payload = board_payload(settings)
        calls = next(
            (row.get("open_calls") or [] for row in payload["projects"] if row["project_id"] == project_id),
            [],
        )
        model = project_layers(directory, open_calls=calls)
        return {"status": "PASS", **model}

    @app.get("/publish/{project_id}", response_class=HTMLResponse, include_in_schema=False)
    def publish_screen(project_id: str) -> str:
        return render_publish_page(publish_payload(settings, project_id))

    @app.get("/api/publish/{project_id}/card")
    def publish_card_route(project_id: str) -> Dict[str, Any]:
        return publish_card(settings, project_id)

    @app.post("/api/publish/{project_id}/approve", status_code=201)
    def publish_approve(project_id: str, payload: ApprovalCardRequest) -> Dict[str, Any]:
        with project_lock(root, project_id):
            return record_approval(
                settings,
                project_id,
                destination=payload.destination,
                caption=payload.caption,
                schedule=payload.schedule,
                timezone_name=payload.timezone,
                internal_note=payload.internal_note,
                audio_label=payload.audio_label,
                cover=payload.cover,
                disclosure=payload.disclosure,
                auto_expansion=payload.auto_expansion,
                reviewer=payload.reviewer,
            )

    @app.get("/review/{project_id}", response_class=HTMLResponse, include_in_schema=False)
    def review_screen(project_id: str) -> str:
        return render_review_page(review_payload(settings, project_id))

    @app.get("/api/reels/{project_id}/review")
    def reel_review(project_id: str) -> Dict[str, Any]:
        return review_bundle(settings, project_id)

    @app.post("/api/reels/{project_id}/verdict", status_code=201)
    def reel_verdict(project_id: str, payload: VerdictRequest) -> Dict[str, Any]:
        with project_lock(root, project_id):
            return record_verdict(
                settings,
                project_id,
                verdict=payload.verdict,
                feedback=payload.feedback.model_dump() if payload.feedback else None,
                notes=payload.notes,
                reviewer=payload.reviewer,
            )

    @app.get("/api/reels/{project_id}/receipt")
    def reel_receipt(project_id: str, stage: str) -> Dict[str, Any]:
        directory = _project(root, project_id)
        if stage not in STAGES:
            raise ReelctlError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")
        try:
            state = ProjectState.load(directory / "state.json")
        except StateError as exc:
            raise ReelctlError(str(exc)) from exc
        record = state.stage(stage)
        answer: Dict[str, Any] = {
            "status": "PASS",
            "project_id": project_id,
            "stage": stage,
            "stage_status": record.get("status"),
            "updated_at_utc": record.get("updated_at_utc"),
            "path": record.get("receipt"),
            "state_sha256": record.get("receipt_sha256"),
            "sha256": None,
            "receipt": None,
            "verified": False,
            "reason": None,
        }
        if not record.get("receipt"):
            answer["reason"] = f"stage {stage} has recorded no receipt (engine status {record.get('status')})"
            return answer
        path = confined_path(directory, str(record["receipt"]), require="file", allow_missing=False)
        answer["sha256"] = sha256_file(path)
        answer["receipt"] = load_json(path, root=directory)
        try:
            state.verify_stage(stage)
            answer["verified"] = True
        except StateError as exc:
            answer["reason"] = str(exc)
        return answer

    @app.get("/api/stream")
    async def stream(
        request: Request,
        after: Optional[int] = Query(default=None, ge=0),
        max_seconds: float = Query(default=DEFAULT_STREAM_SECONDS, gt=0, le=MAX_STREAM_SECONDS),
        poll_ms: int = Query(default=500, ge=10, le=5000),
    ) -> StreamingResponse:
        cursor = after
        if cursor is None:
            header = request.headers.get("last-event-id")
            try:
                cursor = int(header) if header is not None else 0
            except (TypeError, ValueError):
                cursor = 0
        return StreamingResponse(
            sse_stream(
                settings.database_path,
                after_id=max(cursor, 0),
                poll_seconds=poll_ms / 1000,
                max_seconds=max_seconds,
                is_disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        machine = health_report(settings)
        return {
            **machine,
            "studio_database": _studio_database_report(settings.database_path),
            "service": "reel-studio",
            "version": __version__,
            "bind_policy": "loopback_only",
            "publication_api": False,
            "modes": MODES,
            "projects_root": str(root),
        }

    @app.get("/api/projects")
    def projects() -> Dict[str, Any]:
        return {"status": "PASS", "projects": project_summaries(root)}

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate) -> Dict[str, Any]:
        args = _namespace(
            root,
            project_id=payload.project_id,
            reference=payload.reference,
            footage=payload.footage,
            mode=payload.mode,
            defer_analysis=True,
        )
        with project_lock(root, payload.project_id):
            result = command_new(args)
        error = publish("project", payload.project_id, event="created", mode=payload.mode, next_stage=result.get("next_stage"))
        return {**result, "event_error": error}

    @app.get("/api/projects/{project_id}")
    def project_status(project_id: str) -> Dict[str, Any]:
        return project_summary(root, project_id)

    @app.get("/api/projects/{project_id}/artifact")
    def artifact(project_id: str, path: str) -> FileResponse:
        directory = _project(root, project_id)
        candidate = confined_path(directory, path, require="file", allow_missing=False)
        relative = candidate.relative_to(directory)
        if not relative.parts or relative.parts[0] not in ARTIFACT_ROOTS:
            raise ReelctlError("artifact path is outside the reviewable project subtrees")
        return FileResponse(candidate)

    @app.get("/api/projects/{project_id}/workbench")
    def workbench(project_id: str) -> Dict[str, Any]:
        status = project_summary(root, project_id)
        next_stage = status.get("next_stage")
        return {
            "status": "PASS",
            "project_id": project_id,
            "mode": status.get("mode"),
            "next_stage": next_stage,
            "recommended_action": RECOMMENDED_ACTIONS.get(next_stage),
            "actions": ACTION_NAMES,
            "stages": status.get("stages", {}),
        }

    @app.post("/api/projects/{project_id}/actions")
    def action(project_id: str, request: ActionRequest) -> Dict[str, Any]:
        with project_lock(root, project_id):
            result = _dispatch_action(root, project_id, request.action, request.payload)
        error = publish(
            "stage",
            project_id,
            action=request.action,
            status=result.get("status"),
            next_stage=result.get("next_stage") or next_stage_of(project_id),
        )
        return {"status": result.get("status", "PASS"), "action": request.action, "result": result, "event_error": error}

    @app.post("/api/projects/{project_id}/run")
    def run_project(project_id: str, payload: RunRequest) -> Dict[str, Any]:
        args = _namespace(
            root,
            project_id=project_id,
            through="local-review",
            revision=payload.revision,
            default_profile="log_unknown",
        )
        with project_lock(root, project_id):
            result = command_run(args)
        error = publish(
            "stage",
            project_id,
            action="pipeline_run",
            status=result.get("status"),
            next_stage=result.get("next_stage") or next_stage_of(project_id),
        )
        return {**result, "event_error": error}

    return app


def run_server(*, projects_root: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ReelctlError("Reel Studio is local-only; bind to 127.0.0.1, localhost, or ::1")
    import uvicorn

    uvicorn.run(create_app(projects_root=projects_root), host=host, port=port, access_log=False)
