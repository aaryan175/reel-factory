"""Server-rendered studio screens.

Rendering happens in Python, not in the browser, for one reason: the assertion "no cell
renders green for a blocked stage" has to be testable without a browser. A client-side
renderer would put the only code that decides what the operator actually sees outside the
suite, which is precisely where the factory's worst failures have hidden before. So the
page is HTML the tests can read, and SSE is used only to tell the page to fetch a fresh
render — the rendering logic exists once.

Every status string, reason and receipt path reaching this module comes from an engine,
so everything is escaped on the way out. The reasons are rendered as visible text rather
than tooltips: §7 is only satisfied if the withheld reason is on screen, not one hover
away.
"""

from __future__ import annotations

import html
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..paths import canonical_root
from .board import board_model, project_ids
from .calls import inbox
from .config import StudioConfig
from .events import STREAM_EVENT_NAME, EventLogError, latest_event_id
from .health import health_report
from .intake import LANE_DOCTRINE, LANE_MODE, LANES, craft_quota_state, read_intake
from .jobs import ACTIVE_STATUSES, all_jobs
from .library import corpus_note, load_library, world_census
from .opencalls import open_calls_map
from .publish import publish_card
from .registry import lane_for_project, reel_for_project, registry_report
from .review import review_bundle
from .status import FAILED, LAYERS, PENDING_HUMAN, PENDING_MACHINE, PROVEN, WITHHELD

STATUS_CLASS = {
    PROVEN: "s-proven",
    WITHHELD: "s-withheld",
    PENDING_HUMAN: "s-pending-human",
    PENDING_MACHINE: "s-pending-machine",
    FAILED: "s-failed",
}

UNDECLARED = "not declared"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# --- view model ------------------------------------------------------------


def _active_jobs(config: StudioConfig) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """The one in-flight job per project. Absent database is normal, not an error.

    The schema's partial unique index already guarantees at most one active job per
    project, so keying by project id here cannot lose a row.
    """
    path = Path(config.database_path)
    if not path.is_file():
        return {}, None
    try:
        rows = all_jobs(path)
    except (sqlite3.Error, OSError) as exc:
        return {}, "job queue at {} is unreadable: {}".format(path, exc)
    active: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if row.get("status") in ACTIVE_STATUSES:
            active.setdefault(str(row.get("project_id")), row)
    return active, None


def board_payload(config: StudioConfig) -> Dict[str, Any]:
    """The board model: disk-derived status, joined with registry lane and queue state."""
    root = canonical_root(config.projects_root, create=True)
    registry = registry_report(getattr(config, "registry_path", config.projects_root))
    # The same assembly daemon._schedule gates on: registry calls plus each project's own
    # intake record. The registry alone misses a studio-born reel's mode call entirely.
    calls = open_calls_map(registry, root, project_ids(root))
    board = board_model(root, open_calls=calls)
    queue, queue_error = _active_jobs(config)

    latest = 0
    event_error: Optional[str] = None
    try:
        latest = latest_event_id(config.database_path)
    except EventLogError as exc:
        event_error = str(exc)

    projects: List[Dict[str, Any]] = []
    for row in board["projects"]:
        project_id = row["project_id"]
        lane, lane_source = lane_for_project(registry, project_id)
        if lane is None:
            # The registry knows the reels the factory was already running; only the intake
            # record knows the ones this app created since. Neither is guessed from the other.
            record = read_intake(root / project_id) or {}
            if record.get("lane"):
                lane = str(record["lane"])
                lane_source = f"intake record {project_id}/{'intake.json'}"
        projects.append(
            {
                **row,
                "lane": lane,
                "lane_source": lane_source,
                "registry_entry": reel_for_project(registry, project_id),
                "open_calls": calls.get(project_id, []),
                "job": queue.get(project_id),
            }
        )

    return {
        "status": "PASS",
        "generated_at_utc": _now(),
        "latest_event_id": latest,
        "event_log_error": event_error,
        "job_queue_error": queue_error,
        "health": health_report(config),
        "registry": {key: registry[key] for key in ("status", "path", "error", "lanes", "factory_mode")},
        "projects": projects,
    }


# --- rendering -------------------------------------------------------------

STYLE = """
:root {
  color-scheme: dark;
  --bg:#08090a; --panel:#0f1011; --border:rgba(255,255,255,.08); --border-soft:rgba(255,255,255,.05);
  --text:#f7f8f8; --muted:#8a8f98; --subtle:#62666d; --accent:#7170ff;
  --proven:#10b981; --withheld:#f59e0b; --pending-human:#a5b4fc; --pending-machine:#8a8f98; --failed:#f87171;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:13px; }
a { color:var(--accent); }
header.top { position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:16px;
  padding:0 20px; height:56px; border-bottom:1px solid var(--border-soft); background:rgba(8,9,10,.9); backdrop-filter:blur(14px); }
header.top h1 { margin:0; font-size:15px; font-weight:510; letter-spacing:-.18px; }
nav a { margin-right:14px; color:var(--muted); text-decoration:none; font-size:12px; }
nav a.active { color:var(--text); }
.strip { padding:10px 20px; border-bottom:1px solid var(--border-soft); background:var(--panel);
  display:flex; flex-wrap:wrap; gap:8px 20px; align-items:baseline; font-size:12px; color:var(--muted); }
.strip ul { margin:0; padding-left:18px; }
.strip li { color:var(--withheld); }
.wrap { padding:18px 20px 60px; }
.scroller { overflow-x:auto; border:1px solid var(--border); border-radius:10px; }
table.board { border-collapse:collapse; width:max-content; min-width:100%; }
table.board th, table.board td { border-bottom:1px solid var(--border-soft); border-right:1px solid var(--border-soft);
  padding:9px 10px; vertical-align:top; text-align:left; }
table.board thead th { position:sticky; top:0; background:var(--panel); font-size:11px; font-weight:590;
  letter-spacing:.02em; white-space:nowrap; z-index:2; }
table.board thead th span { display:block; color:var(--subtle); font:500 9px ui-monospace,Menlo,monospace; }
th.reel-head { position:sticky; left:0; background:var(--panel); min-width:230px; z-index:3; }
td.cell { min-width:180px; max-width:260px; }
.chip { display:inline-block; padding:3px 7px; border-radius:999px; border:1px solid var(--border);
  font:600 10px ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.03em; }
.chip.s-proven { color:var(--proven); border-color:rgba(16,185,129,.35); background:rgba(16,185,129,.10); }
.chip.s-withheld { color:var(--withheld); border-color:rgba(245,158,11,.35); background:rgba(245,158,11,.10); }
.chip.s-pending-human { color:var(--pending-human); border-color:rgba(165,180,252,.35); background:rgba(165,180,252,.10); }
.chip.s-pending-machine { color:var(--pending-machine); border-color:var(--border); background:rgba(255,255,255,.03); }
.chip.s-failed { color:var(--failed); border-color:rgba(248,113,113,.35); background:rgba(248,113,113,.10); }
p.reason { margin:7px 0 0; color:var(--muted); line-height:1.45; font-size:11px; overflow-wrap:anywhere; }
details.detail { margin-top:6px; }
details.detail summary { cursor:pointer; color:var(--subtle); font-size:10px; text-transform:uppercase; letter-spacing:.07em; }
.meta { margin:6px 0 0; color:var(--subtle); font-size:11px; overflow-wrap:anywhere; }
.items { margin:6px 0 0; padding-left:15px; color:var(--muted); font-size:11px; line-height:1.5; }
.reel-id { font-weight:590; }
.reel-meta { margin:5px 0 0; color:var(--muted); font-size:11px; line-height:1.55; }
.empty { padding:36px 18px; color:var(--muted); text-align:center; }
.note { margin:14px 0 0; color:var(--subtle); font-size:11px; line-height:1.6; max-width:780px; }
.intake-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,460px); gap:26px; align-items:start; }
@media (max-width:860px) { .intake-grid { grid-template-columns:1fr; } }
form { display:grid; gap:14px; max-width:560px; }
label.field, fieldset.field { display:grid; gap:6px; border:0; margin:0; padding:0; color:#d0d6e0; font-size:12px; font-weight:510; }
fieldset.field legend { padding:0; margin-bottom:6px; }
label.field input { width:100%; border:1px solid var(--border); background:rgba(255,255,255,.025); color:var(--text);
  padding:10px 11px; border-radius:6px; outline:none; font:inherit; }
label.field input:focus { border-color:rgba(113,112,255,.8); box-shadow:0 0 0 3px rgba(113,112,255,.12); }
label.field input::placeholder { color:#555961; }
label.lane { display:block; color:var(--muted); font-weight:400; line-height:1.6; }
label.lane strong { color:var(--text); }
label.lane code { color:var(--accent); }
button.go { justify-self:start; border:1px solid #7170ff; background:#5e6ad2; color:#fff; font:510 13px inherit;
  padding:9px 14px; border-radius:6px; cursor:pointer; }
button.go:hover { background:#6d72e0; }
pre#result { margin:16px 0 0; padding:12px; border:1px solid var(--border); border-radius:8px; background:rgba(255,255,255,.02);
  color:var(--muted); font:11px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; overflow-wrap:anywhere; max-height:340px; overflow:auto; }
td.count { text-align:right; font:500 12px ui-monospace,SFMono-Regular,Menlo,monospace; }
article.call { border:1px solid var(--border); border-radius:10px; padding:16px 18px; margin:0 0 16px; background:var(--panel); max-width:900px; }
article.call h4 { margin:14px 0 4px; font-size:11px; font-weight:590; letter-spacing:.06em; text-transform:uppercase; color:var(--subtle); }
p.card-prose { margin:0; color:#d0d6e0; font-size:12px; line-height:1.6; overflow-wrap:anywhere; }
.call-head { display:block; }
.call-head .reel-id { margin-left:8px; font:590 13px ui-monospace,SFMono-Regular,Menlo,monospace; }
.answered { margin-top:12px; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:rgba(255,255,255,.02); }
form.answer { margin-top:14px; max-width:100%; }
pre.answer-result { margin:10px 0 0; padding:10px; border:1px solid var(--border); border-radius:8px; background:rgba(255,255,255,.02);
  color:var(--muted); font:11px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; overflow-wrap:anywhere; max-height:220px; overflow:auto; }
h3 { margin:22px 0 10px; font-size:12px; font-weight:590; letter-spacing:.05em; text-transform:uppercase; color:var(--subtle); }
.players-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:18px; align-items:start; }
.player video { width:100%; max-height:62vh; background:#000; border:1px solid var(--border); border-radius:8px; }
.player h3 { margin-top:0; }
.transport { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin:14px 0 0; }
.transport button { border:1px solid var(--border); background:rgba(255,255,255,.03); color:var(--text); font:inherit;
  padding:6px 11px; border-radius:6px; cursor:pointer; }
.transport button:hover { border-color:var(--accent); }
.transport label.lane { display:inline-flex; gap:6px; align-items:center; margin:0; }
.board-block img { max-width:100%; border:1px solid var(--border); border-radius:8px; display:block; }
ul.layers { list-style:none; margin:0; padding:0; display:grid; gap:8px; max-width:900px; }
ul.layers li { border:1px solid var(--border-soft); border-radius:8px; padding:9px 11px; background:var(--panel); }
.sub-layers { display:grid; gap:4px; margin-top:7px; }
.sub-layers .sub { color:var(--muted); font-size:11px; line-height:1.5; overflow-wrap:anywhere; }
.verdict-block form { max-width:640px; }
pre#verdict-result { margin:12px 0 0; padding:11px; border:1px solid var(--border); border-radius:8px; background:rgba(255,255,255,.02);
  color:var(--muted); font:11px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; overflow-wrap:anywhere; max-height:260px; overflow:auto; }
"""

SCRIPT = """
(function () {
  var host = document.querySelector('[data-after]');
  if (!host) return;
  var status = document.getElementById('live');
  function paint(text) { if (status) status.textContent = text; }
  function refresh() {
    fetch(window.location.pathname, {headers: {'Accept': 'text/html'}})
      .then(function (r) { return r.text(); })
      .then(function (text) {
        var next = new DOMParser().parseFromString(text, 'text/html');
        var fresh = next.querySelector('[data-after]');
        var strip = next.querySelector('.strip');
        if (fresh) { host.innerHTML = fresh.innerHTML; host.dataset.after = fresh.dataset.after; }
        if (strip && document.querySelector('.strip')) document.querySelector('.strip').innerHTML = strip.innerHTML;
      })
      .catch(function (error) { paint('refresh failed: ' + error.message); });
  }
  var source = new EventSource('/api/stream?after=' + (host.dataset.after || 0));
  // One listener, one constant frame name. Subscribing per kind made this page a second
  // place the set of event kinds lived, and it went stale against the daemon immediately.
  source.addEventListener(STREAM_EVENT_NAME, function (event) {
    host.dataset.after = event.lastEventId || host.dataset.after;
    var kind = '';
    try { kind = JSON.parse(event.data).kind || ''; } catch (error) { kind = '(unreadable)'; }
    paint('live · ' + kind + ' at ' + new Date().toLocaleTimeString());
    refresh();
  });
  source.onopen = function () { paint('live'); };
  source.onerror = function () { paint('stream dropped — reconnecting; falling back to polling'); };
  // Backstop for a dropped connection, not for unknown kinds — those now arrive live.
  window.setInterval(refresh, 15000);
})();
"""


def _page(*, title: str, active: str, strip: str, body: str, after: Optional[int] = None) -> str:
    script = ""
    if after is not None:
        script = f'<script>var STREAM_EVENT_NAME = "{STREAM_EVENT_NAME}";{SCRIPT}</script>'
    links = (
        ("board", "/board", "Pipeline board"),
        ("intake", "/intake", "Intake"),
        ("calls", "/calls", "Calls"),
        ("legacy", "/", "Workbench"),
    )
    nav = "".join(
        '<a href="{href}" class="{klass}">{label}</a>'.format(href=href, klass="active" if key == active else "", label=label)
        for key, href, label in links
    )
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n"
        f'<header class="top"><h1>{_e(title)}</h1><nav>{nav}</nav>'
        '<span id="live" style="margin-left:auto;color:var(--muted);font-size:11px">connecting…</span></header>\n'
        f"{strip}\n<div class=\"wrap\">{body}</div>\n{script}\n</body>\n</html>\n"
    )


def _health_strip(payload: Mapping[str, Any]) -> str:
    health = payload.get("health") or {}
    registry = payload.get("registry") or {}
    daemon = health.get("daemon") or {}
    reasons = "".join(f"<li>{_e(reason)}</li>" for reason in health.get("degraded_reasons") or [])
    parts = [
        f'<span><strong>{_e(health.get("status"))}</strong></span>',
        "<span>daemon: {}</span>".format(
            "alive, last tick " + _e(daemon.get("last_tick_utc")) if daemon.get("alive") else _e(daemon.get("reason") or "not running")
        ),
        f'<span>registry: {_e(registry.get("status"))} · {_e(registry.get("path"))}</span>',
    ]
    if registry.get("error"):
        parts.append(f'<span style="color:var(--failed)">{_e(registry["error"])}</span>')
    for key in ("event_log_error", "job_queue_error"):
        if payload.get(key):
            parts.append(f'<span style="color:var(--failed)">{_e(payload[key])}</span>')
    if reasons:
        parts.append(f"<ul>{reasons}</ul>")
    return f'<div class="strip" data-status="{_e(health.get("status"))}">{"".join(parts)}</div>'


def _items(items: Any) -> str:
    rows = []
    for item in items or []:
        chip = STATUS_CLASS.get(item.get("status"), "s-failed")
        reason = f" — {_e(item.get('reason'))}" if item.get("reason") else ""
        rows.append(f'<li><span class="chip {chip}">{_e(item.get("status"))}</span> {_e(item.get("id"))}{reason}</li>')
    return f'<ul class="items">{"".join(rows)}</ul>' if rows else ""


def _cell(project_id: str, layer: Mapping[str, Any]) -> str:
    status = str(layer.get("status"))
    chip = STATUS_CLASS.get(status, "s-failed")
    reason = f'<p class="reason">{_e(layer.get("reason"))}</p>' if layer.get("reason") else ""
    meta = [f'stage {_e(layer.get("stage"))} · engine status {_e(layer.get("stage_status"))}']
    if layer.get("updated_at_utc"):
        meta.append(f'updated {_e(layer.get("updated_at_utc"))}')
    receipt = ""
    if layer.get("receipt"):
        digest = str(layer.get("receipt_sha256") or "")
        receipt = (
            f'<p class="meta"><a href="/api/reels/{_e(project_id)}/receipt?stage={_e(layer.get("stage"))}">'
            f'{_e(layer.get("receipt"))}</a> · <code title="{_e(digest)}">{_e(digest[:16])}</code></p>'
        )
    detail = f'<details class="detail"><summary>evidence</summary><p class="meta">{" · ".join(meta)}</p>{receipt}{_items(layer.get("items"))}</details>'
    return (
        f'<td class="cell" data-project="{_e(project_id)}" data-layer="{_e(layer.get("key"))}" data-status="{_e(status)}">'
        f'<span class="chip {chip}">{_e(status)}</span>{reason}{detail}</td>'
    )


def _job_text(job: Optional[Mapping[str, Any]]) -> str:
    if not job:
        return "no job queued"
    # A job the daemon declined to start sits in the queue behind a retry window, so its
    # status reads QUEUED — true, and on its own it hides an environment wall only the
    # operator can clear. ``disposition`` is the queue's word for that (jobs.withhold), and
    # it leads here; the retry time below still says the machine has not given up.
    label = job.get("disposition") or job.get("status")
    text = f'{_e(label)} {_e(job.get("stage"))} ({_e(job.get("kind"))})'
    if job.get("reason"):
        text += f' — {_e(job.get("reason"))}'
    if job.get("retry_after_utc"):
        text += f' · retry after {_e(job.get("retry_after_utc"))}'
    return text


def _row_header(row: Mapping[str, Any]) -> str:
    job_text = _job_text(row.get("job"))
    lane = f'{_e(row.get("lane"))} — {_e(row.get("lane_source"))}' if row.get("lane") else UNDECLARED
    calls = row.get("open_calls") or []
    blocked = [str(call_id) for call_id in row.get("blocked_by_calls") or []]
    if blocked:
        # Calls are stage-scoped, so this does not claim the whole reel is stopped — the
        # per-layer cells carry which stages actually wait. It names who the machine is
        # waiting on, which is the part the row header can state honestly.
        call_text = f'<br><span class="blocked">awaiting the operator on {_e(", ".join(blocked))}</span>'
    elif calls:
        call_text = f'<br>calls on record: {_e(", ".join(str(call.get("call_id")) for call in calls))}'
    else:
        call_text = ""
    return (
        f'<th class="reel-head" scope="row">'
        f'<span class="reel-id">{_e(row.get("project_id"))}</span>'
        f'<p class="reel-meta">lane: {lane}<br>mode: {_e(row.get("mode") or UNDECLARED)}'
        f'<br>next stage: {_e(row.get("next_stage") or "none pending")}'
        f"<br>job: {job_text}{call_text}</p></th>"
    )


def render_board_page(payload: Mapping[str, Any]) -> str:
    heads = "".join(
        f'<th data-layer="{_e(spec.key)}" scope="col">{_e(spec.title)}<span>{_e(spec.stage)}</span></th>' for spec in LAYERS
    )
    rows = []
    for row in payload.get("projects") or []:
        cells = "".join(_cell(str(row.get("project_id")), layer) for layer in row.get("layers") or [])
        rows.append(
            f'<tr class="reel" data-project="{_e(row.get("project_id"))}" data-headline="{_e(row.get("headline"))}">'
            f"{_row_header(row)}{cells}</tr>"
        )
    table = (
        '<div class="scroller"><table class="board"><thead><tr>'
        f'<th class="reel-head" scope="col">Reel<span>headline = weakest layer</span></th>{heads}'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        if rows
        else '<div class="scroller"><div class="empty">No projects yet. Start one on the intake screen.</div></div>'
    )
    body = (
        f'<div id="board" data-after="{int(payload.get("latest_event_id") or 0)}">{table}</div>'
        '<p class="note">Every cell is recomputed from receipts on this request — a status is never read from a cache. '
        "A reel&#8217;s headline is the weakest of its layers, computed rather than stored, so one proven layer can never "
        "stand in for an unproven one.</p>"
    )
    return _page(
        title="Pipeline board",
        active="board",
        strip=_health_strip(payload),
        body=body,
        after=int(payload.get("latest_event_id") or 0),
    )


def render_board(config: StudioConfig) -> str:
    return render_board_page(board_payload(config))


# --- intake ----------------------------------------------------------------


def intake_payload(config: StudioConfig) -> Dict[str, Any]:
    """Everything the intake screen shows before the operator types anything."""
    library = load_library(config.library_path)
    registry = registry_report(getattr(config, "registry_path", config.projects_root))
    latest = 0
    event_error: Optional[str] = None
    try:
        latest = latest_event_id(config.database_path)
    except EventLogError as exc:
        event_error = str(exc)
    return {
        "status": "PASS",
        "generated_at_utc": _now(),
        "latest_event_id": latest,
        "event_log_error": event_error,
        "job_queue_error": None,
        "health": health_report(config),
        "registry": {key: registry[key] for key in ("status", "path", "error", "lanes", "factory_mode")},
        "library": library,
        "census": world_census(library),
        "preview": corpus_note(library, world=None),
        "footage_root": library.get("library_root"),
        "craft_quota": craft_quota_state(config, registry),
        "lanes": [{"lane": lane, "mode": LANE_MODE[lane], "doctrine": LANE_DOCTRINE[lane]} for lane in LANES],
    }


INTAKE_SCRIPT = """
(function () {
  var form = document.getElementById('intake');
  var out = document.getElementById('result');
  if (!form) return;
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var data = new FormData(form);
    var body = {reference: (data.get('reference') || '').trim(), lane: data.get('lane')};
    ['project_id', 'footage_root', 'world'].forEach(function (key) {
      var value = (data.get(key) || '').trim();
      if (value) body[key] = value;
    });
    out.textContent = 'Creating the project and queueing the bootstrap job\\u2026';
    fetch('/api/intake', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function (response) { return response.json().then(function (payload) { return {ok: response.ok, payload: payload}; }); })
      .then(function (result) {
        if (!result.ok) { out.textContent = 'Refused: ' + (result.payload.error || JSON.stringify(result.payload)); return; }
        out.textContent = JSON.stringify(result.payload, null, 2);
      })
      .catch(function (error) { out.textContent = 'Request failed: ' + error.message; });
  });
})();
"""


def render_intake_page(payload: Mapping[str, Any]) -> str:
    library = payload.get("library") or {}
    quota = payload.get("craft_quota") or {}
    census = payload.get("census") or []
    rows = "".join(
        f'<tr><td>{_e(item["world"])}</td><td class="count">{_e(item["clips"])}</td></tr>' for item in census
    )
    lanes = "".join(
        '<label class="lane"><input type="radio" name="lane" value="{lane}"{checked}> '
        "<strong>{lane}</strong> — {doctrine} <code>{mode}</code></label>".format(
            lane=_e(item["lane"]),
            doctrine=_e(item["doctrine"]),
            mode=_e(item["mode"]),
            checked=" checked" if item["lane"] == "VOLUME" else "",
        )
        for item in payload.get("lanes") or []
    )
    quota_note = (
        '<p class="note">CRAFT is at its quota ({quota}) — held by: {holders}. Choosing it will be refused with that reason '
        "rather than silently downgraded.</p>".format(
            quota=_e(quota.get("quota")), holders=_e("; ".join(quota.get("holders") or []))
        )
        if quota.get("occupied")
        else '<p class="note">CRAFT is free ({}).</p>'.format(_e(quota.get("quota")))
    )
    census_block = (
        f'<div id="corpus" data-after="{int(payload.get("latest_event_id") or 0)}">'
        f'<p class="note">Authorized corpus: <strong>{_e(library.get("clips_total"))}</strong> indexed clips at '
        f'<code>{_e(library.get("library_root"))}</code> (library status {_e(library.get("status"))}, '
        f'built {_e(library.get("built_at_utc"))}).</p>'
        f'<div class="scroller" style="max-width:420px"><table class="board"><thead><tr><th scope="col">Visual world</th>'
        f'<th scope="col">Clips</th></tr></thead><tbody>{rows}</tbody></table></div>'
        f"{quota_note}</div>"
    )
    form = (
        '<form id="intake">'
        '<label class="field"><span>Reference URL or local path</span>'
        '<input name="reference" type="text" required placeholder="https://video.example/reel/…"></label>'
        f'<fieldset class="field"><legend>Lane</legend>{lanes}</fieldset>'
        '<label class="field"><span>Authorized footage root</span>'
        f'<input name="footage_root" type="text" placeholder="{_e(payload.get("footage_root"))}"></label>'
        '<label class="field"><span>Project id</span>'
        '<input name="project_id" type="text" placeholder="derived from the reference shortcode"></label>'
        '<label class="field"><span>Expected visual world (optional)</span>'
        '<input name="world" type="text" list="worlds" placeholder="leave blank until the reference is analysed"></label>'
        '<datalist id="worlds">'
        + "".join(f'<option value="{_e(item["world"])}"></option>' for item in census)
        + "</datalist>"
        '<button class="go" type="submit">Create project and queue bootstrap</button>'
        "</form>"
    )
    body = (
        '<div class="intake-grid">'
        f"<section>{form}"
        '<p class="note">Intake records the reference and queues the bootstrap job. It does not download, analyse or advance '
        "anything — the daemon locks the reference through reelctl like every other stage. The mode is recorded as "
        "<em>provisional</em> and issued as a <code>CALL_REQUIRED</code> card with both consequences; it is never a choice the "
        "app made on your behalf.</p>"
        f'<pre id="result">Nothing submitted yet.</pre></section>'
        f"<section>{census_block}</section></div>"
    )
    page = _page(
        title="Intake",
        active="intake",
        strip=_health_strip(payload),
        body=body,
        after=int(payload.get("latest_event_id") or 0),
    )
    return page.replace("</body>", f"<script>{INTAKE_SCRIPT}</script>\n</body>")


def render_intake(config: StudioConfig) -> str:
    return render_intake_page(intake_payload(config))


# --- calls inbox -----------------------------------------------------------


def calls_payload(config: StudioConfig, *, include_answered: bool = False) -> Dict[str, Any]:
    """The inbox model plus the strip's health, so the screen renders from one payload."""
    model = inbox(config, include_answered=include_answered)
    latest = 0
    event_error: Optional[str] = None
    try:
        latest = latest_event_id(config.database_path)
    except EventLogError as exc:
        event_error = str(exc)
    return {
        **model,
        "latest_event_id": latest,
        "event_log_error": event_error,
        "job_queue_error": None,
        "health": health_report(config),
        "registry": {**model["registry"], "lanes": {}, "factory_mode": None},
    }


CALLS_SCRIPT = """
(function () {
  document.querySelectorAll('form.answer').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var out = form.parentNode.querySelector('pre.answer-result');
      var data = new FormData(form);
      var body = {};
      var option = data.get('option');
      var text = (data.get('text') || '').trim();
      if (option) body.option = option;
      if (text) body.text = text;
      out.textContent = 'Recording the answer\\u2026';
      fetch('/api/calls/' + encodeURIComponent(form.dataset.call) + '/answer',
            {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
        .then(function (response) { return response.json().then(function (payload) { return {ok: response.ok, payload: payload}; }); })
        .then(function (result) {
          if (!result.ok) { out.textContent = 'Refused: ' + (result.payload.error || JSON.stringify(result.payload)); return; }
          out.textContent = JSON.stringify(result.payload, null, 2);
        })
        .catch(function (error) { out.textContent = 'Request failed: ' + error.message; });
    });
  });
})();
"""


def _call_section(name: str, value: Any) -> str:
    if not value:
        return ""
    return '<h4>{}</h4><p class="card-prose">{}</p>'.format(_e(name), _e(value).replace("\n", "<br>"))


def _call_answer_block(call: Mapping[str, Any]) -> str:
    answer = call.get("answer")
    if not answer:
        return ""
    warning = "" if answer.get("binds_current_card") else ' style="color:var(--withheld)"'
    return (
        '<div class="answered"><p class="meta">answered {answered} by {who} — option {option}</p>'
        "<p class=\"reason\">{text}</p>"
        '<p class="meta"{warning}>{note}</p>'
        '<p class="meta">receipt: {receipt}</p></div>'
    ).format(
        answered=_e(answer.get("answered_at_utc")),
        who=_e(answer.get("reviewer")),
        option=_e(answer.get("option") or "free text"),
        text=_e(answer.get("answer_text") or answer.get("option_text") or ""),
        warning=warning,
        note=_e(answer.get("binding_note")),
        receipt=_e(answer.get("receipt")),
    )


def _call_gate_line(call: Mapping[str, Any]) -> str:
    """Both facts at once: what this call stops, and what keeps running while it is open.

    A stage-scoped call means a reel can be legitimately mid-pipeline *and* awaiting the
    operator. Showing only the second reads as a parked reel; showing only the first hides a
    decision that is owed. §7's honesty rule is not satisfied by either half alone.
    """
    if call.get("gates_everything"):
        return "gate: {}".format(_e(call.get("gate_note")))
    proceeding = call.get("stages_proceeding") or []
    running = " · still running meanwhile: {}".format(_e(", ".join(proceeding))) if proceeding else ""
    return "gate: gates {} onward{}".format(_e(call.get("gates_from_stage")), running)


def _call_card(call: Mapping[str, Any]) -> str:
    options = "".join(
        '<label class="lane"><input type="radio" name="option" value="{key}"> <strong>{key}</strong> — {text}</label>'.format(
            key=_e(option.get("key")), text=_e(option.get("text"))
        )
        for option in call.get("options") or []
    )
    evidence = "".join('<li>{}</li>'.format(_e(item)) for item in call.get("evidence") or [])
    body_error = '<p class="reason" style="color:var(--failed)">{}</p>'.format(_e(call["body_error"])) if call.get("body_error") else ""
    form = (
        '<form class="answer" data-call="{call_id}">'
        "{options}"
        '<label class="field"><span>Or say exactly what is needed</span>'
        '<input name="text" type="text" placeholder="{reply_with}"></label>'
        '<button class="go" type="submit">Record answer</button>'
        "</form>"
        '<pre class="answer-result">Nothing recorded yet.</pre>'
    ).format(call_id=_e(call.get("call_id")), options=options, reply_with=_e(call.get("reply_with") or "A, B, C, or the information needed"))
    return (
        '<article class="call" data-call="{call_id}" data-state="{state}" data-project="{project}">'
        '<header class="call-head"><span class="chip {chip}">{severity}</span> '
        '<span class="reel-id">{call_id}</span>'
        '<p class="reel-meta">project: {project} · issue: {issue} · block: {block}<br>'
        "source: {source} · card: {card} · question sha256: {identity}<br>{gate}</p></header>"
        "{body_error}{requires}{found}{mismatch}{evidence}{recommendation}{answer}{form}</article>"
    ).format(
        call_id=_e(call.get("call_id")),
        state=_e(call.get("state")),
        project=_e(call.get("project_id")),
        chip=STATUS_CLASS[PENDING_HUMAN] if call.get("state") != "ANSWERED" else STATUS_CLASS[PROVEN],
        severity=_e(call.get("severity") or "UNDECLARED"),
        issue=_e(call.get("issue_type") or UNDECLARED),
        block=_e(call.get("reference_block") or UNDECLARED),
        source=_e(call.get("source")),
        card=_e(call.get("card_path")),
        identity=_e((call.get("call_sha256") or "")[:16] or "not computed"),
        gate=_call_gate_line(call),
        body_error=body_error,
        requires=_call_section("What the reference requires", call.get("what_the_reference_requires")),
        found=_call_section("What I found", call.get("what_i_found")),
        mismatch=_call_section("Why it does not match", call.get("why_it_does_not_match")),
        evidence='<h4>Evidence</h4><ul class="items">{}</ul>'.format(evidence) if evidence else "",
        recommendation=_call_section("My recommendation", call.get("recommendation")),
        answer=_call_answer_block(call),
        form=form,
    )


def render_calls_page(payload: Mapping[str, Any]) -> str:
    calls = list(payload.get("calls") or [])
    cards = "".join(_call_card(call) for call in calls)
    unlisted = payload.get("unlisted_cards") or []
    unlisted_block = (
        '<p class="note">Cards found in project brains that nothing declares open, so they are not shown as decisions: {}.</p>'.format(
            _e("; ".join("{} ({})".format(item["call_id"], item["project_id"]) for item in unlisted))
        )
        if unlisted
        else ""
    )
    body = (
        '<div id="calls" data-after="{after}">{cards}</div>{unlisted}'
        '<p class="note">Registry <code>open_calls</code> is the store of what is open; a project&#8217;s '
        "<code>brain/07_CALL_PROMPTS.md</code> supplies the card body. An answer is written as a signed receipt bound to the "
        "card&#8217;s sha256 and to a hash of the question itself, appended to that project&#8217;s "
        "<code>brain/08_DECISION_LOG.md</code>, and it releases only that project&#8217;s stalled scheduler rows.</p>"
    ).format(
        after=int(payload.get("latest_event_id") or 0),
        cards=cards or '<div class="empty">No open calls. Nothing is waiting on you.</div>',
        unlisted=unlisted_block,
    )
    page = _page(
        title="Calls inbox",
        active="calls",
        strip=_health_strip(payload),
        body=body,
        after=int(payload.get("latest_event_id") or 0),
    )
    return page.replace("</body>", "<script>{}</script>\n</body>".format(CALLS_SCRIPT))


def render_calls(config: StudioConfig, *, include_answered: bool = False) -> str:
    return render_calls_page(calls_payload(config, include_answered=include_answered))


# --- review room -----------------------------------------------------------


def review_payload(config: StudioConfig, project_id: str) -> Dict[str, Any]:
    """The review bundle plus the strip's health, so the screen renders from one payload."""
    bundle = review_bundle(config, project_id)
    latest = 0
    event_error: Optional[str] = None
    try:
        latest = latest_event_id(config.database_path)
    except EventLogError as exc:
        event_error = str(exc)
    registry = registry_report(getattr(config, "registry_path", config.projects_root))
    return {
        **bundle,
        "latest_event_id": latest,
        "event_log_error": event_error,
        "job_queue_error": None,
        "health": health_report(config),
        "registry": {key: registry[key] for key in ("status", "path", "error", "lanes", "factory_mode")},
    }


REVIEW_SCRIPT = """
(function () {
  var reference = document.getElementById('reference-player');
  var candidate = document.getElementById('candidate-player');
  var link = document.getElementById('scrub-link');
  var rate = Number(document.body.dataset.frameRate || 25);
  var syncing = false;
  function mirror(from, to) {
    return function () {
      if (!link || !link.checked || syncing) return;
      syncing = true;
      if (Math.abs(to.currentTime - from.currentTime) > 0.005) to.currentTime = from.currentTime;
      syncing = false;
    };
  }
  if (reference && candidate) {
    ['seeked', 'timeupdate', 'play', 'pause'].forEach(function (name) {
      reference.addEventListener(name, mirror(reference, candidate));
      candidate.addEventListener(name, mirror(candidate, reference));
    });
    reference.addEventListener('play', function () { if (link && link.checked) candidate.play(); });
    reference.addEventListener('pause', function () { if (link && link.checked) candidate.pause(); });
    document.querySelectorAll('[data-frame-step]').forEach(function (button) {
      button.addEventListener('click', function () {
        var delta = Number(button.dataset.frameStep) / rate;
        [reference, candidate].forEach(function (player) { player.pause(); player.currentTime = Math.max(0, player.currentTime + delta); });
      });
    });
  }
  var form = document.getElementById('verdict');
  if (!form) return;
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var out = document.getElementById('verdict-result');
    var data = new FormData(form);
    var body = {verdict: data.get('verdict')};
    var feedback = {};
    ['target', 'observation', 'requested_change', 'scope'].forEach(function (key) {
      var value = (data.get(key) || '').trim();
      if (value) feedback[key] = value;
    });
    if (Object.keys(feedback).length) body.feedback = feedback;
    var notes = (data.get('notes') || '').trim();
    if (notes) body.notes = notes;
    out.textContent = 'Recording the verdict\\u2026';
    fetch(window.location.pathname.replace('/review/', '/api/reels/') + '/verdict',
          {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function (response) { return response.json().then(function (payload) { return {ok: response.ok, payload: payload}; }); })
      .then(function (result) {
        if (!result.ok) { out.textContent = 'Refused: ' + (result.payload.error || JSON.stringify(result.payload)); return; }
        out.textContent = JSON.stringify(result.payload, null, 2);
      })
      .catch(function (error) { out.textContent = 'Request failed: ' + error.message; });
  });
})();
"""


def _player(role: str, title: str, facts: Mapping[str, Any]) -> str:
    if not facts.get("path"):
        return '<div class="player"><h3>{}</h3><p class="reason">{}</p></div>'.format(_e(title), _e(facts.get("reason")))
    mismatches = "".join('<p class="reason" style="color:var(--failed)">{}</p>'.format(_e(item)) for item in facts.get("mismatches") or [])
    return (
        '<div class="player"><h3>{title}</h3>'
        '<video id="{role}-player" src="{url}" controls preload="metadata" playsinline></video>'
        '<p class="meta">{path}</p><p class="meta">sha256 <code>{sha}</code></p>{mismatches}</div>'
    ).format(
        title=_e(title),
        role=_e(role),
        url=_e(facts.get("artifact_url")),
        path=_e(facts.get("path")),
        sha=_e(facts.get("sha256")),
        mismatches=mismatches,
    )


def _layer_items(items: Any) -> str:
    """Sub-layer rows as spans, not a nested list: one ``<li>`` per layer, always."""
    rows = []
    for item in items or []:
        chip = STATUS_CLASS.get(item.get("status"), "s-failed")
        reason = " — {}".format(_e(item.get("reason"))) if item.get("reason") else ""
        rows.append(
            '<span class="sub"><span class="chip {}">{}</span> {}{}</span>'.format(chip, _e(item.get("status")), _e(item.get("id")), reason)
        )
    return '<div class="sub-layers">{}</div>'.format("".join(rows)) if rows else ""


def _layer_row(project_id: str, layer: Mapping[str, Any]) -> str:
    status = str(layer.get("status"))
    chip = STATUS_CLASS.get(status, "s-failed")
    reason = '<p class="reason">{}</p>'.format(_e(layer.get("reason"))) if layer.get("reason") else ""
    receipt = ""
    if layer.get("receipt"):
        receipt = '<p class="meta"><a href="/api/reels/{}/receipt?stage={}">{}</a></p>'.format(
            _e(project_id), _e(layer.get("stage")), _e(layer.get("receipt"))
        )
    return (
        '<li data-layer="{key}" data-status="{status}"><span class="chip {chip}">{status}</span> '
        '<strong>{title}</strong> <span class="meta">{stage} · engine {engine}</span>{reason}{receipt}{items}</li>'
    ).format(
        key=_e(layer.get("key")),
        status=_e(status),
        chip=chip,
        title=_e(layer.get("title")),
        stage=_e(layer.get("stage")),
        engine=_e(layer.get("stage_status")),
        reason=reason,
        receipt=receipt,
        items=_layer_items(layer.get("items")),
    )


def _verdict_form(payload: Mapping[str, Any]) -> str:
    options = []
    if payload.get("can_approve"):
        options.append('<label class="lane"><input type="radio" name="verdict" value="approve" required> <strong>Approve</strong></label>')
    else:
        options.append(
            '<p class="reason" style="color:var(--withheld)">Approve is not offered, and the API refuses it too: {}</p>'.format(
                _e(payload.get("approve_refusal"))
            )
        )
    options.append('<label class="lane"><input type="radio" name="verdict" value="reject" required> <strong>Reject</strong></label>')
    options.append('<label class="lane"><input type="radio" name="verdict" value="changes" required> <strong>Request changes</strong></label>')
    fields = "".join(
        '<label class="field"><span>{label}</span><input name="{name}" type="text" placeholder="{hint}"></label>'.format(
            label=_e(label), name=_e(name), hint=_e(hint)
        )
        for name, label, hint in (
            ("target", "Target", "the exact block, timecode or frame range"),
            ("observation", "Observation", "what you saw, not what you infer"),
            ("requested_change", "Requested change", "the specific change that would fix it"),
            ("scope", "Scope", "selection · grade · captions · audio · render"),
        )
    )
    return (
        '<form id="verdict"><fieldset class="field"><legend>Verdict</legend>{options}</fieldset>'
        '<p class="note">Reject and Request changes both require every feedback field — that structured event is what becomes the '
        "permanent negative fixture.</p>{fields}"
        '<label class="field"><span>Notes (optional)</span><input name="notes" type="text"></label>'
        '<button class="go" type="submit">Record verdict</button></form>'
        '<pre id="verdict-result">Nothing recorded yet.</pre>'
    ).format(options="".join(options), fields=fields)


def render_review_page(payload: Mapping[str, Any]) -> str:
    project_id = str(payload.get("project_id"))
    board = payload.get("board") or {}
    board_block = (
        '<img src="{url}" alt="the reference/candidate comparison board QC built">'
        '<p class="meta">{path} · sha256 <code>{sha}</code></p>'.format(
            url=_e(board.get("artifact_url")), path=_e(board.get("path")), sha=_e(board.get("sha256"))
        )
        if board.get("path")
        else '<p class="reason">{}</p>'.format(_e(board.get("reason")))
    )
    qc = payload.get("qc") or {}
    qc_line = (
        '<p class="meta">QC report {path} · sha256 <code>{sha}</code> · bound to {stage}</p>'.format(
            path=_e(qc.get("path")), sha=_e(qc.get("sha256")), stage=_e(qc.get("bound_to_stage"))
        )
        if qc.get("path")
        else '<p class="reason" style="color:var(--withheld)">{}</p>'.format(_e(qc.get("reason")))
    )
    rows = "".join(_layer_row(project_id, layer) for layer in payload.get("layers") or [])
    history = "".join(
        '<li><span class="chip {chip}">{verdict}</span> #{sequence} · {when} · {who} · candidate <code>{sha}</code>'
        '<p class="meta">{receipt}</p></li>'.format(
            chip=STATUS_CLASS[PROVEN] if item.get("verdict") == "APPROVE" else STATUS_CLASS[FAILED],
            verdict=_e(item.get("verdict")),
            sequence=_e(item.get("sequence")),
            when=_e(item.get("recorded_at_utc")),
            who=_e(item.get("reviewer")),
            sha=_e(str(item.get("candidate_sha256") or "")[:16]),
            receipt=_e(item.get("receipt")),
        )
        for item in payload.get("verdicts") or []
    )
    body = (
        '<div id="review" data-after="{after}">'
        '<section class="players-grid">{reference}{candidate}</section>'
        '<div class="transport"><button type="button" data-frame-step="-1">&#9664; frame-step</button>'
        '<button type="button" data-frame-step="1">frame-step &#9654;</button>'
        '<label class="lane"><input type="checkbox" id="scrub-link" checked> scrub-linked</label>'
        '<span class="meta">revision {revision} · headline <span class="chip {headline_chip}">{headline}</span></span></div>'
        '<section class="board-block"><h3>What the machine actually compared</h3>{board}{qc_line}</section>'
        '<section id="layer-panel"><h3>Layers</h3><ul class="layers">{rows}</ul></section>'
        '<section class="verdict-block"><h3>Verdict</h3>{form}</section>'
        '<section class="history"><h3>Recorded verdicts</h3><ul class="layers">{history}</ul></section>'
        "</div>"
        '<p class="note">Every hash on this page is recomputed from the file on this request. A verdict binds the candidate, the '
        "reference, the QC report and the comparison board by sha256, and a reject or change request also writes a signed negative "
        "fixture that stays on disk.</p>"
    ).format(
        after=int(payload.get("latest_event_id") or 0),
        reference=_player("reference", "Reference", payload.get("reference") or {}),
        candidate=_player("candidate", "Candidate", payload.get("candidate") or {}),
        revision=_e(payload.get("revision") or UNDECLARED),
        headline=_e(payload.get("headline")),
        headline_chip=STATUS_CLASS.get(str(payload.get("headline")), "s-failed"),
        board=board_block,
        qc_line=qc_line,
        rows=rows,
        form=_verdict_form(payload),
        history=history or '<li class="meta">none yet</li>',
    )
    page = _page(
        title="Review — {}".format(project_id),
        active="board",
        strip=_health_strip(payload),
        body=body,
        after=int(payload.get("latest_event_id") or 0),
    )
    return page.replace("</body>", "<script>{}</script>\n</body>".format(REVIEW_SCRIPT))


def render_review(config: StudioConfig, project_id: str) -> str:
    return render_review_page(review_payload(config, project_id))


# --- publish card ----------------------------------------------------------


def publish_payload(config: StudioConfig, project_id: str) -> Dict[str, Any]:
    """The approval card plus the strip's health, so the screen renders from one payload."""
    card = publish_card(config, project_id)
    latest = 0
    event_error: Optional[str] = None
    try:
        latest = latest_event_id(config.database_path)
    except EventLogError as exc:
        event_error = str(exc)
    registry = registry_report(getattr(config, "registry_path", config.projects_root))
    return {
        **card,
        "latest_event_id": latest,
        "event_log_error": event_error,
        "job_queue_error": None,
        "health": health_report(config),
        "registry": {key: registry[key] for key in ("status", "path", "error", "lanes", "factory_mode")},
    }


PUBLISH_SCRIPT = """
(function () {
  var form = document.getElementById('approval');
  if (!form) return;
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var out = document.getElementById('approval-result');
    var data = new FormData(form);
    var body = {};
    ['destination', 'caption', 'schedule', 'timezone', 'internal_note', 'audio_label', 'cover', 'disclosure'].forEach(function (key) {
      var value = (data.get(key) || '').trim();
      if (value) body[key] = value;
    });
    body.auto_expansion = data.get('auto_expansion') === 'on';
    out.textContent = 'Recording the approval\\u2026 no outward call is made by this button.';
    fetch(window.location.pathname.replace('/publish/', '/api/publish/') + '/approve',
          {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function (response) { return response.json().then(function (payload) { return {ok: response.ok, payload: payload}; }); })
      .then(function (result) {
        if (!result.ok) { out.textContent = 'Refused: ' + (result.payload.error || JSON.stringify(result.payload)); return; }
        out.textContent = JSON.stringify(result.payload, null, 2);
      })
      .catch(function (error) { out.textContent = 'Request failed: ' + error.message; });
  });
})();
"""

_CARD_ROWS = (
    ("destination", "Destination account"),
    ("caption", "Public caption"),
    ("internal_note", "Internal note"),
    ("audio_label", "Original-audio label"),
    ("schedule", "Schedule"),
    ("timezone", "Timezone"),
    ("cover", "Cover"),
    ("disclosure", "Disclosure"),
    ("auto_expansion", "Automatic expansion to followers"),
)


def _card_table(payload: Mapping[str, Any]) -> str:
    rows = []
    for name, label in _CARD_ROWS:
        entry = payload.get(name) or {}
        value = entry.get("value")
        state = "stated" if value is not None else ("unresolved — required" if entry.get("required") else "unresolved")
        rows.append(
            "<tr><td>{label}</td><td>{value}</td><td>{state}</td><td>{reason}</td></tr>".format(
                label=_e(label),
                value=_e(value if value is not None else "—"),
                state=_e(state),
                reason=_e(entry.get("reason") or ""),
            )
        )
    return (
        '<div class="scroller"><table class="board"><thead><tr><th scope="col">Field</th><th scope="col">Value</th>'
        '<th scope="col">State</th><th scope="col">Why it is not filled in</th></tr></thead>'
        "<tbody>{}</tbody></table></div>".format("".join(rows))
    )


def _approval_form() -> str:
    fields = "".join(
        '<label class="field"><span>{label}</span><input name="{name}" type="text"></label>'.format(label=_e(label), name=_e(name))
        for name, label in _CARD_ROWS
        if name != "auto_expansion"
    )
    return (
        '<form id="approval">{fields}'
        '<label class="lane"><input type="checkbox" name="auto_expansion"> automatic expansion to followers is ON</label>'
        '<button class="go" type="submit">Record approval</button></form>'
        '<pre id="approval-result">Nothing recorded yet.</pre>'
    ).format(fields=fields)


def render_publish_page(payload: Mapping[str, Any]) -> str:
    asset = payload.get("asset") or {}
    unresolved = "".join(
        "<li><strong>{field}</strong>{required} — {reason}</li>".format(
            field=_e(item["field"]), required=" (required)" if item.get("required") else "", reason=_e(item.get("reason"))
        )
        for item in payload.get("unresolved") or []
    )
    limitations = "".join("<li>{}</li>".format(_e(item)) for item in payload.get("platform_limitations") or [])
    approval = payload.get("human_approval") or {}
    proven = (
        '<ul class="items"><li>asset <code>{path}</code></li><li>sha256 <code>{sha}</code></li>'
        "<li>bytes {bytes} · duration {duration} · geometry {geometry} · fps {fps} · frames {frames}</li>"
        "<li>post type {post_type}</li>"
        "<li>human approval receipt <code>{receipt}</code> sha256 <code>{receipt_sha}</code></li></ul>"
    ).format(
        path=_e(asset.get("path")),
        sha=_e(asset.get("sha256")),
        bytes=_e(asset.get("bytes")),
        duration=_e(asset.get("duration_seconds") if asset.get("duration_seconds") is not None else "not recorded by QC"),
        geometry=_e(asset.get("geometry") or "not recorded by QC"),
        fps=_e(asset.get("fps") or "not recorded by QC"),
        frames=_e(asset.get("frame_count") if asset.get("frame_count") is not None else "not recorded by QC"),
        post_type=_e(payload.get("post_type")),
        receipt=_e(approval.get("receipt") or "none"),
        receipt_sha=_e(approval.get("receipt_sha256") or "none"),
    )
    if payload.get("can_record"):
        action = '<section class="verdict-block"><h3>Record approval</h3>{}</section>'.format(_approval_form())
    else:
        action = '<section class="verdict-block"><h3>Not approvable yet</h3><p class="reason" style="color:var(--withheld)">{}</p></section>'.format(
            _e(payload.get("approval_refusal"))
        )
    body = (
        '<div id="publish" data-after="{after}">'
        "<section><h3>What the machine can prove</h3>{proven}</section>"
        "<section><h3>The approval card</h3>{table}</section>"
        '<section><h3>Unresolved</h3><ul class="items">{unresolved}</ul></section>'
        '<section><h3>Platform limitations this app cannot clear</h3><ul class="items">{limitations}</ul></section>'
        "{action}</div>"
        '<p class="note">This screen composes the card and records the operator&#8217;s approval as a hash-bound receipt. It '
        "uploads nothing, schedules nothing with any provider and spends nothing. On a publication receipt appearing on disk, "
        "the +24h and +72h metric captures are queued as rows in <code>studio.db</code>; executing them stays with the operator.</p>"
    ).format(
        after=int(payload.get("latest_event_id") or 0),
        proven=proven,
        table=_card_table(payload),
        unresolved=unresolved or "<li>nothing — every field on the card carries a value</li>",
        limitations=limitations,
        action=action,
    )
    page = _page(
        title="Publish card — {}".format(payload.get("project_id")),
        active="board",
        strip=_health_strip(payload),
        body=body,
        after=int(payload.get("latest_event_id") or 0),
    )
    return page.replace("</body>", "<script>{}</script>\n</body>".format(PUBLISH_SCRIPT))


def render_publish(config: StudioConfig, project_id: str) -> str:
    return render_publish_page(publish_payload(config, project_id))


def project_dir(config: StudioConfig, project_id: str) -> Path:
    return canonical_root(config.projects_root, create=True) / project_id
