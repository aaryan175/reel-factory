"""
Reel Deck — desktop control room for the reel factory.

Reads: REEL_REGISTRY.json + the workbench media index (server.registry).
Writes: ONLY bus receipts (server.receipts). Never posts, never touches
the registry, never schedules.

Run: .venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 7355
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, board, grader, media, receipts
from . import registry as reg
from . import settings

APP_DIR = Path(__file__).resolve().parent.parent  # the Deck checkout
# shot-by-shot row audit (static, behind the login); its proxies/ is a symlink into the footage library
AUDIT_DIR = Path(os.environ.get("REEL_DECK_AUDIT_DIR") or settings.WORKBENCH / "footage-intake" / "audit").expanduser()

app = FastAPI(title="Reel Deck", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "web" / "static"), name="static")
# check_dir=False: an unmounted drive must not stop the Deck booting; /audit just 404s until it is back
app.mount("/audit", StaticFiles(directory=AUDIT_DIR, follow_symlink=True, check_dir=False), name="audit")
templates = Jinja2Templates(directory=APP_DIR / "web" / "templates")

PUBLIC_PATHS = {"/login", "/health", "/api/client-error", "/favicon.ico"}


@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    cookie = request.cookies.get(auth.SESSION_COOKIE)
    request.state.user = auth.session_user(cookie)
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if request.state.user is None:
        # A cookie that no longer verifies = the session expired or the account
        # changed. Say so, in words, wherever the person is.
        expired = bool(cookie)
        if path.startswith("/api/") or path.startswith("/media"):
            return JSONResponse({"error": "Your session expired — sign in again. Nothing you already sent is lost.",
                                 "expired": expired, "login": "/login?expired=1"}, status_code=401)
        return RedirectResponse("/login?expired=1" if expired else "/login", status_code=303)
    return await call_next(request)


def is_editor(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user) and str(user.get("role") or "").upper() == "EDITOR"


def author_of(request: Request) -> dict[str, str] | None:
    """Who is sending this — stamped onto every receipt the request writes."""
    return getattr(request.state, "user", None)


# ---------------------------------------------------------------- auth pages

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    expired = request.query_params.get("expired") == "1"
    return templates.TemplateResponse(request, "login.html", {
        "error": "Your session expired — sign in again. Nothing you already sent is lost." if expired else None,
        "ready": auth.credentials_set(),
    })


@app.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    client = request.client.host if request.client else "?"
    user, message = auth.check_login(username, password, client)
    if user is None:
        return templates.TemplateResponse(request, "login.html", {
            "error": message, "ready": auth.credentials_set(),
        }, status_code=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(auth.SESSION_COOKIE, auth.issue_session(user),
                        max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
                        secure=(request.headers.get("x-forwarded-proto", request.url.scheme) == "https"))
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


# --------------------------------------------------------------------- pages

def page(request: Request, template: str, context: dict[str, Any], **kwargs):
    """Render a page with the signed-in user always in scope (the nav rail
    shows who is sending, and role decides how receipts read)."""
    return templates.TemplateResponse(
        request, template, {**context, "user": author_of(request), "editor": is_editor(request),
                            "build": _BUILD}, **kwargs
    )


@app.get("/favicon.ico")
async def favicon():
    return RedirectResponse("/static/favicon.svg", status_code=308)


@app.get("/", response_class=HTMLResponse)
async def board_page(request: Request):
    return page(request, "board.html", {"board": board.build_board()})


@app.get("/row/{seq}", response_class=HTMLResponse)
async def row_page(request: Request, seq: int):
    detail = board.row_detail(seq)
    if detail is None:
        return page(request, "missing.html", {"seq": seq}, status_code=404)
    return page(request, "row.html", {"d": detail})


@app.post("/api/client-error")
async def client_error(request: Request):
    """The reviewer's browser reports its own JS errors here (window.onerror in base.html),
    with the user agent — the only way to see what a phone we do not hold is choking on."""
    try:
        raw = await request.body()
        body = json.loads(raw[:4096].decode("utf-8", "replace")) if raw else {}
        if not isinstance(body, dict):
            body = {"message": str(body)[:600]}
    except Exception:
        body = {}
    ua = request.headers.get("user-agent", "?")[:200]
    # the caller's fields live under "report" so they can never overwrite at/ua/ip
    line = json.dumps({"at": datetime.datetime.utcnow().isoformat() + "Z", "ua": ua, "ip": request.client.host if request.client else "?",
                       "report": {str(k)[:40]: str(v)[:600] for k, v in list((body or {}).items())[:12]}}, ensure_ascii=False)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "client-errors.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return {"ok": True}


@app.get("/row/{seq}/process", response_class=HTMLResponse)
async def process_page(request: Request, seq: int):
    """The whole build of one row, end to end, read from disk (read-only)."""
    from . import process
    data = process.build_process(seq)
    if data is None:
        return page(request, "missing.html", {"seq": seq}, status_code=404)
    return page(request, "process.html", {"p": data})


@app.get("/api/row/{seq}/process")
async def process_api(request: Request, seq: int):
    from . import process
    data = process.build_process(seq)
    if data is None:
        raise HTTPException(status_code=404, detail="no such row")
    return data


@app.get("/make", response_class=HTMLResponse)
async def make_page(request: Request):
    data = board.build_board()
    done = [c for c in data["cards"] + data["archive"] if c["bucket"] == "done"]
    return page(request, "make.html", {
        "done": done, "receipts": receipts.list_ui_receipts(20),
    })


@app.get("/now", response_class=HTMLResponse)
async def now_page(request: Request):
    return page(request, "now.html", {"now": now_payload()})


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    return page(request, "help.html", {"howto_video": HOWTO_VIDEO})


@app.get("/grader", response_class=HTMLResponse)
async def grader_page(request: Request):
    return page(request, "grader.html", {})


# --------------------------------------------------------------- clip grader
# Every tap is one line in grades.jsonl on this machine, written before the reply says saved.

@app.get("/api/grader/items")
async def api_grader_items():
    return grader.list_items()


@app.get("/api/grader/summary")
async def api_grader_summary():
    return grader.summary()


async def _grader_body(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError:
        return None


EDITOR_GRADE_ERROR = "Grades are the operator's own rulings on this footage — an editor login cannot send them."


@app.post("/api/grader/grade")
async def api_grader_grade(request: Request):
    if is_editor(request):
        return JSONResponse({"error": EDITOR_GRADE_ERROR}, status_code=403)
    entry, error = grader.validate(await _grader_body(request), grader.library_stems(),
                                   grader.library_durations())
    if error:
        return JSONResponse({"error": error}, status_code=422)
    return {"ok": True, "line": grader.append([entry], author_of(request))[0]}


@app.post("/api/grader/grade-many")
async def api_grader_grade_many(request: Request):
    if is_editor(request):
        return JSONResponse({"error": EDITOR_GRADE_ERROR}, status_code=403)
    body = await _grader_body(request)
    raw = body.get("items") if isinstance(body, dict) else None
    if not isinstance(raw, list) or not raw:
        return JSONResponse({"error": "no grades given"}, status_code=422)
    known, durations = grader.library_stems(), grader.library_durations()
    entries = []
    for item in raw:   # all or nothing: one bad item and no line is written
        entry, error = grader.validate(item, known, durations)
        if error:
            return JSONResponse({"error": error}, status_code=422)
        entries.append(entry)
    lines = grader.append(entries, author_of(request))
    return {"ok": True, "written": len(lines)}


# ----------------------------------------------------------------------- api

@app.get("/api/board")
async def api_board():
    return board.build_board()


@app.get("/api/row/{seq}")
async def api_row(seq: int):
    detail = board.row_detail(seq)
    if detail is None:
        return JSONResponse({"error": f"no registry row with sequence {seq}"}, status_code=404)
    return detail


def _disk(path: str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        return {"free_gb": round(usage.free / 1e9, 1), "total_gb": round(usage.total / 1e9, 1)}
    except OSError as exc:
        return {"error": str(exc)}


def now_payload() -> dict[str, Any]:
    data = board.build_board()
    waiting = [c for c in data["cards"] if c["bucket"] == "needs_you"]
    building = [c for c in data["cards"] if c["bucket"] == "machine"]
    return {
        "waiting": waiting,
        "building": building,
        "next_review": data["next_review"],
        "calls_unmatched": data["calls_unmatched"],
        "bus": receipts.recent_bus_activity(30),
        "ui_receipts": receipts.list_ui_receipts(15),
        "workbench": data["workbench"],
        "index": data["index"],
        "disks": {"workbench": _disk(str(reg.WORKBENCH_ROOT)), "internal": _disk("/")},
        "registry": data["registry"],
    }


@app.get("/api/now")
async def api_now():
    return now_payload()


@app.post("/api/verdict")
async def api_verdict(request: Request):
    # a malformed body used to 500 and the reviewer's words were lost
    try:
        body = await request.json()
        row = int(body["row"])
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "That did not send: the page sent a malformed request. Reload the page and send it again — your text is still in the box."}, status_code=400)
    disposition = str(body.get("disposition", "NOTES"))
    if disposition.upper() not in ("APPROVE", "REJECT", "NOTES"):
        return JSONResponse({"error": "disposition must be APPROVE, REJECT or NOTES"}, status_code=422)
    path = receipts.write_feedback(row, disposition, str(body.get("text", "")),
                                   body.get("watched"), author=author_of(request),
                                   test=bool(body.get("test")))
    return {"ok": True, "receipt": os.path.basename(path), "test": bool(body.get("test"))}


@app.post("/api/batch-verdict")
async def api_batch_verdict(request: Request):
    body = await request.json()
    items = body.get("items") or []
    if not items:
        return JSONResponse({"error": "no verdicts given"}, status_code=422)
    for item in items:
        if str(item.get("verdict", "")).upper() not in ("KEEP", "KILL", "NOTES"):
            return JSONResponse({"error": "each verdict must be KEEP, KILL or NOTES"}, status_code=422)
    path = receipts.write_batch_verdict(int(body["row"]), items, str(body.get("note", "")),
                                        author=author_of(request))
    return {"ok": True, "receipt": os.path.basename(path)}


@app.post("/api/frame-note")
async def api_frame_note(request: Request):
    form = await request.form()
    images: list[str] = []
    for upload in form.getlist("images"):
        if isinstance(upload, UploadFile) and upload.filename:
            saved, error = receipts.save_upload(upload.filename, await upload.read())
            if error:
                return JSONResponse({"error": error}, status_code=422)
            images.append(saved)
    frame_raw = str(form.get("frame") or "").strip()
    path = receipts.write_frame_note(
        row=int(str(form.get("row"))),
        file_name=str(form.get("file") or "?"),
        sha256=(str(form.get("sha256")) or None) if form.get("sha256") else None,
        frame=int(frame_raw) if frame_raw.isdigit() else None,
        timecode=str(form.get("timecode") or ""),
        text=str(form.get("text") or ""),
        images=images,
        author=author_of(request),
        test=str(form.get("test") or "") in ("1", "true", "on"),
    )
    return {"ok": True, "receipt": os.path.basename(path)}


@app.get("/api/row/{seq}/sends")
async def api_row_sends(seq: int):
    """The reviewer's sends on a row with the factory's state — polled by the row
    page so 'waiting' turns into 'received' without a reload."""
    return {"sends": receipts.row_sends(seq), "factory": board.factory_status()}


@app.post("/api/drop")
async def api_drop(request: Request):
    body = await request.json()
    urls = [u for u in (body.get("urls") or []) if isinstance(u, str) and u.strip()]
    build_of = body.get("build_variants_of")
    if build_of is not None:
        try:
            build_of = int(build_of)
        except (TypeError, ValueError):
            return JSONResponse({"error": "build_variants_of must be a reel number"}, status_code=422)
    if not urls and build_of is None:
        return JSONResponse({"error": "give at least one URL or pick a reel"}, status_code=422)
    path = receipts.write_drop(urls, str(body.get("note", "")), build_of,
                               author=author_of(request))
    return {"ok": True, "receipt": os.path.basename(path)}


@app.post("/api/continue")
async def api_continue(request: Request):
    path, error = receipts.write_continue(author=author_of(request))
    if error:
        return JSONResponse({"error": error}, status_code=429)
    return {"ok": True, "receipt": os.path.basename(path)}


@app.get("/media")
async def media_route(request: Request, p: str) -> Response:
    return media.serve(request, p)


# narrated walkthrough of the whole system, built by tools/howto/ (optional)
HOWTO_VIDEO = str(settings.WORKBENCH / "reel-deck-howto" / "walkthrough" / "reel-deck-walkthrough.mp4")

_STARTED_AT = __import__("time").time()


def build_hash() -> str:
    """Identify what is running: sha1 over the server/templates/static sources.
    Changes on every edit; two machines showing the same hash run the same code."""
    import hashlib
    h = hashlib.sha1()
    for folder in (APP_DIR / "server", APP_DIR / "web"):
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".html", ".js", ".css"):
                h.update(path.name.encode()); h.update(path.read_bytes())
    return h.hexdigest()[:12]


_BUILD = build_hash()

LOG_DIR = settings.DECK_HOME / "logs"
LOG_ROTATE_BYTES = 20 * 1024 * 1024
LOG_KEEP = 5


def rotate_logs() -> list[str]:
    """launchd holds deck.log/deck.err open (append mode), so rotation is
    copy-then-truncate, never rename. Runs at startup and once a day."""
    rotated = []
    for name in ("deck.log", "deck.err", "client-errors.log"):
        path = LOG_DIR / name
        try:
            if not path.exists() or path.stat().st_size < LOG_ROTATE_BYTES:
                continue
            for n in range(LOG_KEEP - 1, 0, -1):
                older, newer = LOG_DIR / f"{name}.{n + 1}", LOG_DIR / f"{name}.{n}"
                if newer.exists():
                    os.replace(newer, older)
            shutil.copyfile(path, LOG_DIR / f"{name}.1")
            with open(path, "r+b") as handle:
                handle.truncate(0)
            rotated.append(name)
        except OSError:
            continue
    return rotated


def ack_smoketests() -> list[str]:
    """The plumbing lane's other half. ui-smoketest-* receipts exist so the
    stranger's walk can prove receipt → bus → pickup → row state without ordering
    a build. The executor's scan skips smoketests entirely (its bus scan is
    `grep -v smoketest`), so the Deck itself picks them up: one
    ACK line, honestly labelled, nothing else. Real orders are never touched here."""
    acked = []
    try:
        names = sorted(n for n in os.listdir(receipts.BUS_DIR) if n.startswith("ui-smoketest-") and n.endswith(".md"))
    except OSError:
        return acked
    for name in names:
        path = receipts.BUS_DIR / name
        try:
            if receipts.receipt_status(str(path))["acks"]:
                continue
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"\nACK — deck-bus — {receipts.utc_iso()} — smoketest: bus round-trip OK, the Deck saw this receipt; nothing was built and no verdict was recorded\n")
            acked.append(name)
        except OSError:
            continue
    return acked


@app.on_event("startup")
async def _startup_tasks():
    import asyncio
    rotate_logs()
    ack_smoketests()
    reg.load_index_cache()         # last good index serves immediately…
    reg.ensure_index(force=True)   # …while a fresh build runs in the background

    async def daily():
        while True:
            await asyncio.sleep(24 * 3600)
            rotate_logs()

    async def bus_ack():
        while True:
            await asyncio.sleep(60)
            await asyncio.get_event_loop().run_in_executor(None, ack_smoketests)
    asyncio.get_event_loop().create_task(daily())
    asyncio.get_event_loop().create_task(bus_ack())


@app.get("/health")
async def health():
    snapshot = reg.index_snapshot()
    factory = board.factory_status()
    registry_path = Path(reg.REGISTRY_PATH)
    registry_writable = os.access(registry_path, os.W_OK) if registry_path.exists() else False
    payload = {
        "ok": True,
        "build": _BUILD,
        "uptime_s": round(__import__("time").time() - _STARTED_AT, 1),
        "factory": {"awake": factory["awake"], "state": factory["state"], "heartbeat_age_s": factory["age_s"]},
        "registry": {"path": str(registry_path), "writable": registry_writable,
                     "error": reg.load_registry().get("error")},
        "workbench": reg.workbench_status(),
        "index_state": snapshot["state"],
        "index_files": snapshot["files"],
    }
    payload["ok"] = bool(registry_writable and payload["registry"]["error"] is None)
    return payload
