"""
Registry engine for Reel Pilot — the pilot's primary source of truth.

The factory's studio projects cover only a subset of the rows, and the build
lanes render OUTSIDE the studio project directories (mostly under
<workbench>/<lane-dir>/deliver/). So the board is built from
REEL_REGISTRY.json and the delivered files are found by a basename index over
the places lanes actually write.

Everything here is READ-ONLY. Nothing in this module ever writes to disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import settings

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

REEL_ROOT = settings.REEL_HOME
REGISTRY_PATH = REEL_ROOT / "REEL_REGISTRY.json"
REFERENCE_INTAKE = REEL_ROOT / "reference-intake"

# realpath is what we scan and what the media route's confinement already allows.
WORKBENCH_ROOT = Path(os.path.realpath(settings.WORKBENCH))

# Workbench subdirs that are factory output, beyond the reel* convention.
WORKBENCH_NAMED = {"reel-delivery", "review-staging"}

# EVERY top-level workbench directory is indexed, not just `reel*`: a delivery
# can sit in an ad-hoc folder such as `final-verify/dl/`, and an unindexed one
# makes the board say "Ready for you to watch" over a player with nothing in it.
#
# What is skipped is the frame-dump class: hundreds of thousands of numbered
# stills that no registry row ever names. They are excluded BY NAME at every
# depth, and every skip is disclosed on /status rather than being silent.
INDEX_SKIP_EXACT = {"footage-library", "masters", "allframes"}
INDEX_SKIP_RE = re.compile(
    r"(frames)|^caption-sweep|^readback-|^haze-repair|-evidence$", re.IGNORECASE
)

# Per-root file budget. Scanning is breadth-first, so a root is complete to
# depth N before depth N+1 is started and the truncation lands on the deepest,
# least valuable level. Deliveries live at depth 1-3; frame dumps live deeper.
INDEX_ROOT_FILE_CAP = 5000


def index_skipped(name: str) -> bool:
    """Is this directory name part of the frame-dump class?"""
    return name in INDEX_SKIP_EXACT or bool(INDEX_SKIP_RE.search(name))

REGISTRY_TTL_SECONDS = 30.0
INDEX_TTL_SECONDS = 60.0
INDEX_MAX_DEPTH = 5

MEDIA_EXTS = {".mp4", ".jpg", ".png", ".md"}
VIDEO_EXT = ".mp4"
IMAGE_EXTS = {".jpg", ".png"}

# A basename that is the reference itself is never a review candidate.
REFERENCE_BASENAMES = {"reference-source.mp4"}

# Names that are comparison/proof artefacts rather than a reel to watch.
COMPARISON_MARKERS = ("side-by-side", "side by side", "proof board", "board", "4up", "4-up")

_VERSION_RE = re.compile(r"\(\s*v(\d{1,3})\b", re.IGNORECASE)
_STATE_VERSION_RE = re.compile(r"\bv(\d{1,3})\b", re.IGNORECASE)

# review_state token classification (matched against the whole state string,
# upper-cased — lane names in this registry are free text, not an enum).
VERDICT_WORDS = (
    "PENDING_HUMAN",
    "AWAITING",
    "IN_TODAY",
    "LOCAL_REVIEW_READY",
    "DELIVERED_TO_TODAY",
    "REVIEW_ONLY",
)
VERDICT_EXCLUSIONS = ("PUBLISHED", "APPROVED", "PARKED", "REJECTED")
DONE_WORDS = ("PUBLISHED", "APPROVED", "USER_REVIEWED_GOOD")
COLD_WORDS = ("PARKED", "REJECTED", "QUARANTINED")

LANE_VERDICT = "verdict"
LANE_DONE = "done"
LANE_MACHINE = "machine"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def utc_iso(ts: float | None = None) -> str:
    when = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            # KEYS too: the factory may record a delivery as
            # {"files": {"deliver/…-v009.mp4": "<sha256>"}} — the file name lives only in a dict key, and
            # without this the Deck would never offer that version.
            if isinstance(key, str):
                yield key
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_strings(value)


def _basename(text: str) -> str:
    return text.replace("\\", "/").rsplit("/", 1)[-1].strip()


def _looks_like_filename(name: str) -> bool:
    """Reject prose that merely ends in '.mp4' (the registry has such sentences)."""
    if not name or len(name) > 200:
        return False
    if "\n" in name or "\t" in name:
        return False
    return True


# --------------------------------------------------------------------------
# Registry read (cached; the file is 1.7 MB)
# --------------------------------------------------------------------------

_registry_lock = threading.Lock()
_registry_state: dict[str, Any] = {"at": 0.0, "payload": None}


def load_registry(force: bool = False) -> dict[str, Any]:
    """Return {'rows', 'error', 'read_at_utc', 'updated_at_utc', 'mtime_utc'}."""
    now = time.monotonic()
    with _registry_lock:
        cached = _registry_state["payload"]
        if cached is not None and not force and now - _registry_state["at"] < REGISTRY_TTL_SECONDS:
            return cached

    payload: dict[str, Any]
    try:
        stat = REGISTRY_PATH.stat()
        with open(REGISTRY_PATH, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
        rows = data.get("reels")
        if not isinstance(rows, list):
            raise ValueError("REEL_REGISTRY.json has no 'reels' list")
        payload = {
            "rows": rows,
            "error": None,
            "read_at_utc": utc_iso(),
            "updated_at_utc": data.get("updated_at_utc"),
            "mtime_utc": utc_iso(stat.st_mtime),
            "bytes": stat.st_size,
            "path": str(REGISTRY_PATH),
        }
    except Exception as exc:  # noqa: BLE001 — surfaced as data, never raised
        payload = {
            "rows": [],
            "error": f"REEL_REGISTRY.json unreadable: {exc}",
            "read_at_utc": utc_iso(),
            "updated_at_utc": None,
            "mtime_utc": None,
            "bytes": 0,
            "path": str(REGISTRY_PATH),
        }

    with _registry_lock:
        _registry_state["at"] = time.monotonic()
        _registry_state["payload"] = payload
    return payload


# --------------------------------------------------------------------------
# Workbench readability probe
#
# macOS TCC gates removable volumes per executing binary, and for a
# prompt-eligible binary launched by launchd the denial presents as an
# INFINITE HANG inside open()/opendir(), not as EPERM — stat still succeeds,
# which is the tell-tale sign. A cockpit that hangs is worse than one that says
# it cannot see the drive, so every volume touch goes through this probe.
# --------------------------------------------------------------------------

PROBE_TIMEOUT_SECONDS = 6.0
PROBE_TTL_SECONDS = 300.0

_probe_lock = threading.Lock()
_probe_state: dict[str, Any] = {
    "status": None,  # OK | BLOCKED | MISSING | ERROR
    "detail": None,
    "checked_mono": 0.0,
    "checked_at_utc": None,
    "thread": None,
}


def _probe_worker(sink: dict[str, Any]) -> None:
    try:
        seen = 0
        with os.scandir(WORKBENCH_ROOT) as entries:
            for _entry in entries:
                seen += 1
                if seen >= 8:
                    break
        sink["status"] = "OK"
        sink["detail"] = f"listed {WORKBENCH_ROOT}"
    except FileNotFoundError:
        sink["status"] = "MISSING"
        sink["detail"] = f"{WORKBENCH_ROOT} does not exist — is the drive mounted?"
    except OSError as exc:
        sink["status"] = "ERROR"
        sink["detail"] = f"{type(exc).__name__}: {exc}"


def workbench_status(force: bool = False) -> dict[str, Any]:
    """OK / BLOCKED / MISSING / ERROR for the workbench volume. Never hangs."""
    with _probe_lock:
        fresh = (
            _probe_state["status"] is not None
            and time.monotonic() - _probe_state["checked_mono"] < PROBE_TTL_SECONDS
        )
        running = _probe_state["thread"]
        if (fresh and not force) or (running is not None and running.is_alive()):
            return {
                "status": _probe_state["status"] or "BLOCKED",
                "detail": _probe_state["detail"]
                or "a probe of the volume has not returned yet — treating it as blocked",
                "checked_at_utc": _probe_state["checked_at_utc"],
            }

    sink: dict[str, Any] = {"status": None, "detail": None}
    # A TCC-blocked open cannot be interrupted; the probe thread is abandoned
    # rather than joined forever, and is never started twice concurrently.
    thread = threading.Thread(target=_probe_worker, args=(sink,), name="reel-pilot-vol-probe", daemon=True)
    with _probe_lock:
        _probe_state["thread"] = thread
    thread.start()
    thread.join(PROBE_TIMEOUT_SECONDS)

    if thread.is_alive() or sink["status"] is None:
        status = "BLOCKED"
        detail = (
            f"listing {WORKBENCH_ROOT} did not return within {PROBE_TIMEOUT_SECONDS:.0f}s. "
            "This is the macOS TCC signature: stat works, opendir/read block forever. "
            "Grant the interpreter this service runs under Full Disk Access / Removable "
            "Volumes, or run it under an interpreter that already has the grant."
        )
    else:
        status, detail = sink["status"], sink["detail"]

    with _probe_lock:
        _probe_state["status"] = status
        _probe_state["detail"] = detail
        _probe_state["checked_mono"] = time.monotonic()
        _probe_state["checked_at_utc"] = utc_iso()
        if not thread.is_alive():
            _probe_state["thread"] = None
    return {"status": status, "detail": detail, "checked_at_utc": utc_iso()}


def guard_path(path: str | Path) -> str | None:
    """None when the path is safe to open; otherwise the reason it is not."""
    text = str(path)
    if not text.startswith(str(WORKBENCH_ROOT)):
        return None
    probe = workbench_status()
    if probe["status"] == "OK":
        return None
    return f"{WORKBENCH_ROOT} is not readable by this process ({probe['status']}): {probe['detail']}"


# --------------------------------------------------------------------------
# Media index — basename -> [absolute paths]
#
# Built on a background thread. A cold external drive can take seconds; no
# request is ever allowed to block on it. Until the first build lands, pages
# say the index is warming rather than claiming files are missing.
# --------------------------------------------------------------------------

_index_lock = threading.Lock()
_index_state: dict[str, Any] = {
    "map": {},
    "built_at_mono": 0.0,
    "built_at_utc": None,
    "building": False,
    "error": None,
    "files": 0,
    "names": 0,
    "roots": 0,
    "duration_s": None,
    "generation": 0,
}


def _project_root_dirnames(rows: list[dict[str, Any]]) -> set[str]:
    """Top-level workbench dir names named by any row's project_root.

    Some rows live in workbench dirs like `batch3-25-v002` that do not start
    with 'reel'; without this the delivered files for those rows are invisible.
    """
    names: set[str] = set()
    for row in rows:
        raw = row.get("project_root")
        if not isinstance(raw, str):
            continue
        text = raw.strip().rstrip("/")
        prefixes = [str(settings.WORKBENCH).rstrip("/") + "/"]
        if settings.WORKDRIVE_ALIAS:
            prefixes.insert(0, settings.WORKDRIVE_ALIAS + "/workbench/")
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix) :]
                names.add(text.split("/", 1)[0])
                break
    return {n for n in names if n and "/" not in n and not n.startswith(".")}


def index_roots(rows: list[dict[str, Any]], include_workbench: bool = True) -> list[str]:
    roots: list[str] = []
    if include_workbench:
        try:
            with os.scandir(WORKBENCH_ROOT) as entries:
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if index_skipped(entry.name):
                        continue
                    roots.append(entry.path)
        except OSError:
            pass

    try:
        with os.scandir(REEL_ROOT) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                for sub in ("edit", "review", "deliver"):
                    candidate = os.path.join(entry.path, sub)
                    if os.path.isdir(candidate):
                        roots.append(candidate)
    except OSError:
        pass
    return roots


def _scan_into(base: str, depth: int, out: dict[str, list[str]]) -> tuple[int, bool]:
    """Index one root breadth-first. Returns (files indexed, was truncated).

    Level by level, so the root is always COMPLETE to some depth: a delivery at
    depth 2 can never be lost to a frame dump at depth 4 that happened to be
    walked first. The budget is checked between levels, never mid-level.
    """
    count = 0
    seen = 0
    level = [base]
    for _ in range(max(0, depth) + 1):
        if not level:
            return count, False
        nxt: list[str] = []
        for directory in level:
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if not index_skipped(entry.name):
                                    nxt.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                seen += 1
                                if os.path.splitext(entry.name)[1].lower() in MEDIA_EXTS:
                                    out.setdefault(entry.name, []).append(entry.path)
                                    count += 1
                        except OSError:
                            continue
            except OSError:
                continue
        if seen > INDEX_ROOT_FILE_CAP and nxt:
            return count, True
        level = nxt
    return count, bool(level)


def _build_index() -> None:
    started = time.time()
    built: dict[str, list[str]] = {}
    error = None
    roots: list[str] = []
    skipped: list[str] = []
    truncated: list[str] = []
    try:
        probe = workbench_status()
        if probe["status"] != "OK":
            error = (
                f"the workbench volume is not readable by this process "
                f"({probe['status']}: {probe['detail']}) — no delivered renders are visible"
            )
        rows = load_registry().get("rows") or []
        roots = index_roots(rows, include_workbench=probe["status"] == "OK")
        if probe["status"] == "OK":
            try:
                with os.scandir(WORKBENCH_ROOT) as entries:
                    skipped = sorted(
                        e.name for e in entries
                        if e.is_dir(follow_symlinks=False) and index_skipped(e.name)
                    )
            except OSError:
                skipped = []
        for root in roots:
            indexed, cut = _scan_into(root, INDEX_MAX_DEPTH, built)
            if cut:
                truncated.append(os.path.basename(root))
    except Exception as exc:  # noqa: BLE001
        error = f"media index scan failed: {exc}"
    finally:
        with _index_lock:
            _index_state["map"] = built
            _index_state["built_at_mono"] = time.monotonic()
            _index_state["built_at_utc"] = utc_iso()
            _save_index_cache(built, _index_state["built_at_utc"])
            _index_state["building"] = False
            _index_state["error"] = error
            _index_state["files"] = sum(len(v) for v in built.values())
            _index_state["names"] = len(built)
            _index_state["roots"] = len(roots)
            _index_state["skipped"] = skipped
            _index_state["truncated"] = sorted(truncated)
            _index_state["duration_s"] = round(time.time() - started, 2)
            _index_state["generation"] += 1


INDEX_CACHE = settings.DECK_HOME / "index-cache.json"


def _save_index_cache(built: dict[str, list[str]], built_at_utc: str) -> None:
    """Persist the last good index so a restart serves it at once (then refreshes)."""
    try:
        tmp = INDEX_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"built_at_utc": built_at_utc, "map": built}))
        os.replace(tmp, INDEX_CACHE)
    except OSError:
        pass


def load_index_cache() -> bool:
    """At startup: the previous index becomes the working map immediately, flagged stale so
    a refresh kicks on the first request. Without this the first ~10-30 s after a restart
    serve an EMPTY index: no versions, no video, "cut isn't on this machine"."""
    try:
        data = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
        built = {str(k): list(v) for k, v in (data.get("map") or {}).items()}
    except (OSError, ValueError, AttributeError):
        return False
    with _index_lock:
        if _index_state["built_at_utc"] is None:
            _index_state["map"] = built
            _index_state["built_at_utc"] = str(data.get("built_at_utc") or "cache")
            _index_state["built_at_mono"] = time.monotonic() - INDEX_TTL_SECONDS - 1   # stale → refresh now
            _index_state["files"] = sum(len(v) for v in built.values())
            _index_state["names"] = len(built)
    return True


def ensure_index(force: bool = False) -> None:
    """Kick a background rebuild when stale. Never blocks the caller."""
    with _index_lock:
        if _index_state["building"]:
            return
        fresh = (
            _index_state["built_at_mono"]
            and time.monotonic() - _index_state["built_at_mono"] < INDEX_TTL_SECONDS
        )
        if fresh and not force:
            return
        _index_state["building"] = True
    threading.Thread(target=_build_index, name="reel-pilot-media-index", daemon=True).start()


COLD_WAIT_SECONDS = 10.0


def index_snapshot() -> dict[str, Any]:
    """A consistent view of the index for one request. Kicks a refresh if stale.

    COLD (no index built yet, e.g. the seconds after a restart): WAIT for the first build
    instead of answering from an empty map — an empty map made the row page say the cut
    "isn't on this machine" and hid every version pill for the first requests after a
    restart."""
    ensure_index()
    deadline = time.monotonic() + COLD_WAIT_SECONDS
    while True:
        with _index_lock:
            cold = _index_state["built_at_utc"] is None and _index_state["building"]
        if not cold or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    with _index_lock:
        built_at = _index_state["built_at_mono"]
        snapshot = {
            "map": _index_state["map"],
            "built_at_utc": _index_state["built_at_utc"],
            "age_s": (time.monotonic() - built_at) if built_at else None,
            "building": _index_state["building"],
            "error": _index_state["error"],
            "files": _index_state["files"],
            "names": _index_state["names"],
            "roots": _index_state["roots"],
            "skipped": list(_index_state.get("skipped") or []),
            "truncated": list(_index_state.get("truncated") or []),
            "duration_s": _index_state["duration_s"],
            "generation": _index_state["generation"],
        }
    snapshot["state"] = (
        "COLD"
        if snapshot["built_at_utc"] is None
        else ("REFRESHING" if snapshot["building"] else "READY")
    )
    return snapshot


def resolve_name(name: str, index: dict[str, list[str]], want_bytes: int | None = None) -> str | None:
    """Absolute path for a basename. Prefers the copy whose size matches the
    registry's recorded byte count; otherwise the newest by mtime."""
    paths = index.get(name)
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    if want_bytes:
        for path in paths:
            try:
                if os.path.getsize(path) == want_bytes:
                    return path
            except OSError:
                continue
    best, best_mtime = None, -1.0
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best or paths[0]


_ROW_DIR_RE = re.compile(r"/reel0?(\d{1,3})-[^/]*", re.IGNORECASE)


def resolve_name_for_row(name: str, index: dict[str, list[str]], want_bytes: int | None,
                         sequence: int | None) -> str | None:
    """resolve_name, but a row never plays another row's file (a lane that delivers a bare `v008.mp4`
    must not resolve to another row's work file of the same name).
    Order: the row's own lane folder(s) — deliver/ first — then paths that belong to no row at all.
    A copy inside a DIFFERENT row's `reelMM-…` folder is never used."""
    if sequence is None:
        return resolve_name(name, index, want_bytes)
    candidates = list(index.get(name) or [])
    for dirname in workbench_dirnames():
        if re.match(r"^reel0?%d-" % sequence, dirname, re.IGNORECASE):
            direct = str(WORKBENCH_ROOT / dirname / "deliver" / name)
            if direct not in candidates and os.path.isfile(direct):
                candidates.append(direct)

    def owner(path: str) -> int | None:
        match = _ROW_DIR_RE.search(path)
        return int(match.group(1)) if match else None

    own = [p for p in candidates if owner(p) == sequence]
    if own:
        delivered = [p for p in own if "/deliver/" in p and "/variants/" not in p]
        pool = delivered or own
        return resolve_name(name, {name: pool}, want_bytes)
    neutral = [p for p in candidates if owner(p) is None]
    return resolve_name(name, {name: neutral}, want_bytes) if neutral else None


# --------------------------------------------------------------------------
# Row accessors — every row is free-form; nothing below assumes a key exists
# --------------------------------------------------------------------------


def row_sequence(row: dict[str, Any]) -> int | None:
    value = row.get("sequence")
    return value if isinstance(value, int) else None


def row_shortcode(row: dict[str, Any]) -> str | None:
    for key in ("reference_shortcode", "shortcode"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def row_reference_url(row: dict[str, Any]) -> str | None:
    for key in ("reference_url", "youtube_url", "reference_url_as_ordered"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def row_review_state(row: dict[str, Any]) -> str:
    value = row.get("review_state")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def row_state_token(row: dict[str, Any]) -> str:
    """The state token: review_state up to the first ' — ' or sentence break.

    Several rows carry a paragraph of narrative in review_state. The token is
    the machine-ish head of it; the full sentence is still shown verbatim on
    the card, it is only lane classification that uses the head.
    """
    state = row_review_state(row)
    if not state:
        return "NO REVIEW STATE"
    token = state.split(" — ", 1)[0]
    token = token.split(". ", 1)[0]
    return token.strip()


def _lane_basis(row: dict[str, Any]) -> str:
    """First three `__` segments of the token — past that it is prose.

    Matching lane words against the whole review_state misfiles rows: a row can
    be `LOCAL_REVIEW_READY__PENDING_HUMAN` followed by paragraphs that happen to
    contain the word APPROVED, or narrate refits that were REJECTED. The head of
    the token is the state; the tail is commentary.
    """
    return "__".join(row_state_token(row).split("__")[:3]).upper()


_WORD_RE = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)*|[A-Z0-9]+")


def _segment_words(segment: str) -> set[str]:
    """Whole words of one `__` segment: both the underscore-joined tokens and their
    parts, upper-cased. Whole-word, so APPROVAL never matches APPROVED."""
    words: set[str] = set()
    # Prose never classifies. Machine state segments are UPPER_SNAKE tokens; a
    # segment with whitespace or lowercase letters is commentary ("no render until
    # the look is approved") and is dropped whole, so its words can't file a row.
    stripped = segment.strip()
    if re.search(r"\s", stripped) or re.search(r"[a-z]", stripped):
        return words
    for token in re.split(r"[^A-Z0-9_]+", segment.upper()):
        if not token:
            continue
        words.add(token)
        parts = [p for p in token.split("_") if p]
        words.update(parts)
        # contiguous joins so HELD_FOR_OPERATOR / PENDING_HUMAN match inside a
        # longer token like APPROVED_HELD_FOR_OPERATOR
        for n in (2, 3, 4):
            for i in range(len(parts) - n + 1):
                words.add("_".join(parts[i:i + n]))
    return words


def basis_word_sets(basis: str) -> tuple[set[str], set[str]]:
    """(head words, all words) for a lane basis — the head segment is the state."""
    segments = (basis or "").split("__")
    head = _segment_words(segments[0]) if segments else set()
    every = set(head)
    for segment in segments[1:]:
        every |= _segment_words(segment)
    return head, every


def _classify_words(words: set[str]) -> tuple[bool, bool, bool]:
    """(verdict, done, cold) hits for a word set. Multi-word classifier terms
    (PENDING_HUMAN, USER_REVIEWED_GOOD…) match as the joined token."""
    verdict = any(w in words for w in VERDICT_WORDS)
    done = any(w in words for w in DONE_WORDS)
    cold = any(w in words for w in COLD_WORDS)
    return verdict, done, cold


def _lane_hits(row: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Lane words for a row. The HEAD segment is the state: if it carries any
    classifier word it decides alone, and the later segments are never consulted.
    Only when the head is silent do the next two segments get a say (older rows
    put the state second, e.g. `V007__LOCAL_REVIEW_READY__…`).

    Regression guard: `AWAITING_OPERATOR_LOOK_APPROVAL__…__no render until the
    look is APPROVED` must not file DONE just because substring matching over all
    three segments finds APPROVED in the prose.
    """
    segments = row_state_token(row).split("__")[:3]
    if not segments:
        return False, False, False
    head = _classify_words(_segment_words(segments[0]))
    if any(head):
        return head
    rest = set()
    for segment in segments[1:]:
        rest |= _segment_words(segment)
    return _classify_words(rest)


def row_lane(row: dict[str, Any]) -> str:
    verdict, done, cold = _lane_hits(row)
    if verdict and not (done or cold):
        return LANE_VERDICT
    if done:
        return LANE_DONE
    return LANE_MACHINE


def row_is_cold(row: dict[str, Any]) -> bool:
    return _lane_hits(row)[2]


def state_basis(row: dict[str, Any]) -> str:
    """Public name for the lane basis — the machine-ish head of review_state.

    review_state up to the first ' — ' or sentence break, then its first three
    `__` segments, upper-cased. Everything past that is commentary.
    """
    return _lane_basis(row)


# --------------------------------------------------------------------------
# Plain-English state translation
#
# The reviewer is the taste layer, not an engineer, and should never have to
# read LOCAL_REVIEW_READY__V001_MAIN_PLUS_CASTING_VARIANT_DELIVERED_TO_TODAY__
# PENDING_HUMAN to learn that a reel is ready to watch. This maps the basis
# (+ lane, for the fallback) onto one short human phrase and one colour.
#
# Nothing is deleted: the caller keeps showing the raw token and the full
# sentence under a collapsed "Details" disclosure. This only decides what
# leads.
# --------------------------------------------------------------------------

BLUE, GREY, AMBER, GREEN = "blue", "grey", "amber", "green"

# `_` is a word character, so \b never fires between `__` and `V006`.
_PLAIN_VERSION_RE = re.compile(r"(?<![A-Za-z0-9])[Vv](\d{1,3})(?![0-9])")

QUESTION_WORDS = (
    "FEASIBILITY_STOP",
    "FEASIBILITY",
    "CALL_REQUIRED",
    "AWAITING_LETTER",
    "OPEN_CALL",
    "CALL_ROW",
)
POSTED_WORDS = ("PUBLISHED", "REPUBLISHED")
PARKED_WORDS = ("QUARANTINED", "PARKED", "REJECTED")
APPROVED_WORDS = ("APPROVED", "USER_REVIEWED_GOOD", "BASELINE", "ACCEPTED")
HELD_WORDS = ("HELD_FOR_OPERATOR", "HELD_FOR_YOU")
NEWCUT_WORDS = ("RECAST", "RE_CAST", "TYPESET", "FIX", "DELIVERED")
NEWCUT_CONTEXT = ("PENDING", "IN_TODAY", "AWAITING", "LOCAL_REVIEW_READY", "READY", "DELIVERED")
WATCH_WORDS = (
    "LOCAL_REVIEW_READY",
    "PENDING_HUMAN",
    "IN_TODAY",
    "AWAITING",
    "REVIEW_ONLY",
)
QUEUED_WORDS = ("REFERENCE_LOCKED", "INTAKEN", "INTAKE_ORDERED", "INTAKE", "QUEUED")
BUILDING_WORDS = ("REFERENCE_MATCHED", "MATCHED_CAPTIONS", "IN_BUILD", "BUILDING", "RENDERING")


def plain_state(basis: str, lane: str = LANE_MACHINE) -> dict[str, Any]:
    """basis + lane -> {'phrase', 'color', 'version'}. Pure; unit-testable.

    Precedence is deliberate: a question the operator has to answer outranks
    everything, then posted, then cold, then approved, then a fresh cut, then
    plain 'ready', then the pre-build states.
    """
    text = (basis or "").upper()
    match = _PLAIN_VERSION_RE.search(text)
    version = f"v{int(match.group(1)):03d}" if match else None

    # Whole-word matching, head segment first (substring matching would read
    # `AWAITING_OPERATOR_LOOK_APPROVAL__…__the look is APPROVED` as "Approved").
    _, scope = basis_word_sets(basis or "")

    def has(words: tuple[str, ...]) -> bool:
        return any(word in scope for word in words)

    if not text or text == "NO REVIEW STATE":
        return {"phrase": "In progress", "color": GREY, "version": version}
    if has(QUESTION_WORDS):
        return {"phrase": "The factory has a question for you", "color": AMBER, "version": version}
    if has(POSTED_WORDS):
        return {"phrase": "Posted", "color": GREEN, "version": version}
    if has(PARKED_WORDS):
        return {"phrase": "Parked", "color": GREY, "version": version}
    if has(APPROVED_WORDS):
        if has(HELD_WORDS):
            return {"phrase": "Approved — held for you", "color": AMBER, "version": version}
        return {"phrase": "Approved", "color": GREEN, "version": version}
    if has(NEWCUT_WORDS) and has(NEWCUT_CONTEXT):
        return {"phrase": "New version ready", "color": BLUE, "version": version}
    if has(WATCH_WORDS):
        return {"phrase": "Ready for you to watch", "color": BLUE, "version": version}
    if has(QUEUED_WORDS):
        return {"phrase": "Queued — being planned", "color": GREY, "version": version}
    if has(BUILDING_WORDS):
        return {"phrase": "Being built", "color": GREY, "version": version}
    if lane == LANE_DONE:
        return {"phrase": "Done", "color": GREEN, "version": version}
    return {"phrase": "In progress", "color": GREY, "version": version}


# The two phrases that promise the operator a video. Saying either of these
# over a player with nothing in it is the one lie this surface must not tell.
PROMISE_PHRASES = ("Ready for you to watch", "New version ready")


def promised_file(row: dict[str, Any]) -> dict[str, Any] | None:
    """The file the state token promises, when the registry names one.

    Prefers the .mp4 whose version tag matches the version in the state token
    (`…V005_SAMPLE_SOLID…` -> the v005 file); otherwise the newest-looking
    review .mp4 the row declares. Carries the Drive id when the row records
    one, because a file that is only on Drive is still watchable — elsewhere.
    """
    declared = row_declared_names(row)["loose"]
    videos = sorted(n for n in declared if os.path.splitext(n)[1].lower() == VIDEO_EXT)
    if not videos:
        return None
    facts = row_hash_facts(row)
    wanted = row_state_version(row)
    pick = None
    if wanted is not None:
        for name in videos:
            if version_of(name) == wanted:
                pick = name
                break
    if pick is None:
        tagged = [n for n in videos if version_of(n) is not None]
        pick = max(tagged, key=lambda n: version_of(n)) if tagged else videos[-1]
    recorded = facts.get(pick) or {}
    return {
        "name": pick,
        "version": version_of(pick),
        "drive_id": recorded.get("drive_id"),
        "sha256": recorded.get("sha256"),
        "bytes": recorded.get("bytes"),
    }


def row_plain_state(row: dict[str, Any], media: dict[str, Any] | None = None,
                    index_ready: bool = True) -> dict[str, Any]:
    """plain_state() for a registry row, with the raw token kept alongside.

    When `media` is supplied the translator is held to the disk: a row whose
    state promises a watchable cut but whose candidate does not resolve says
    so, names the file, and offers Drive when the registry recorded an id.
    Without `media` the behaviour is exactly as before.
    """
    result = plain_state(state_basis(row), row_lane(row))
    result["token"] = row_state_token(row)
    result["state"] = row_review_state(row)

    if media is not None and result["phrase"] in PROMISE_PHRASES and not media.get("versions") and not index_ready:
        # The file index is still being built (COLD past COLD_WAIT_SECONDS — the drive scan is ~76k
        # files). An empty map is not evidence of a missing video: without this every delivered row
        # briefly reads "video not on this machine" after a restart.
        result["phrase"] = "Looking for the video file…"
        result["color"] = AMBER
        result["unplayable"] = True
    elif media is not None and result["phrase"] in PROMISE_PHRASES and not media.get("versions"):
        promised = promised_file(row)
        label = result.get("version")
        if not label and promised and promised.get("version") is not None:
            label = version_label(promised["version"])
        label = label or "a cut"
        result["phrase"] = f"Factory marked {label} ready — video not on this machine"
        result["color"] = AMBER
        result["unplayable"] = True
        result["missing_name"] = promised["name"] if promised else None
        result["drive_id"] = promised.get("drive_id") if promised else None
    return result


def row_state_version(row: dict[str, Any]) -> int | None:
    """A vNNN mentioned in review_state wins the 'current version' choice."""
    match = _STATE_VERSION_RE.search(row_review_state(row))
    return int(match.group(1)) if match else None


def version_of(name: str) -> int | None:
    match = _VERSION_RE.search(name)
    return int(match.group(1)) if match else None


def version_label(number: int | None) -> str:
    return f"v{number:03d}" if number is not None else "untagged"


def is_comparison(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in COMPARISON_MARKERS)


# --------------------------------------------------------------------------
# Variant batches — the ten-at-a-time trial surface
#
# A batch is ten renders of one locked shell with the footage swapped. The
# lanes name them the same way whether they are already delivered
# (`08 v01 — … — TRIAL VARIANT 01 (v10 WHITE INK).mp4`) or still rendering
# (`13 v01 — … — TRIAL VARIANT 01 (BATCH 01).mp4`), so
# one parser reads both. The `(vNNN …)` tail is the VERSION and is never the
# variant number — every pattern below is anchored on the word variant or on
# the `NN vNN —` filename prefix.
# --------------------------------------------------------------------------

_VARIANT_PATTERNS = (
    # New-era batch naming: `NN — TRIAL VARIANTS BATCH 02 — v07 — <code> — review.mp4`
    re.compile(r"variants\s+batch\s+\d+\s*[—-]+\s*v(\d{1,2})(?!\d)", re.IGNORECASE),
    re.compile(r"trial\s+variant\s+(\d{1,2})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<![a-z])variant[\s_-]*(\d{1,2})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<![a-z])var[\s_-]?(\d{1,2})(?!\d)", re.IGNORECASE),
    re.compile(r"^\s*\d{1,3}\s+v(\d{2})(?!\d)\s+[-\u2014]", re.IGNORECASE),
)

BATCH_MARKERS = ("trial variant", "variants batch", "batch 01", "trial variants")


def variant_of(name: str) -> int | None:
    """The variant number a batch file carries, or None if it is not one.

    `TRIAL VARIANTS BATCH 01` (the proof board) is deliberately NOT a variant:
    every pattern demands a number directly after the singular word.
    """
    for pattern in _VARIANT_PATTERNS:
        match = pattern.search(name)
        if match:
            number = int(match.group(1))
            if 1 <= number <= 99:
                return number
    return None


_wb_dirs_lock = threading.Lock()
_wb_dirs_state: dict[str, Any] = {"at": 0.0, "names": [], "error": None}
WB_DIRS_TTL_SECONDS = 60.0


def workbench_dirnames(force: bool = False) -> list[str]:
    """Top-level workbench directory names, cached. Never blocks on a dead drive."""
    now = time.monotonic()
    with _wb_dirs_lock:
        if not force and _wb_dirs_state["names"] and now - _wb_dirs_state["at"] < WB_DIRS_TTL_SECONDS:
            return list(_wb_dirs_state["names"])
    if guard_path(WORKBENCH_ROOT):
        return []
    names: list[str] = []
    try:
        with os.scandir(WORKBENCH_ROOT) as scan:
            for entry in scan:
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return []
    with _wb_dirs_lock:
        _wb_dirs_state["at"] = time.monotonic()
        _wb_dirs_state["names"] = names
    return list(names)


def row_batch(row: dict[str, Any], index: dict[str, list[str]],
              media: dict[str, Any] | None = None) -> dict[str, Any]:
    """This row's variant batch: its lane directory and every delivered variant.

    Two independent sources, merged and deduplicated by path:
      1. `<workbench>/reelNN-batch*/deliver/*.mp4` — the lane writing right now.
      2. any file this row already resolves whose NAME carries a variant number
         — how older batches named their trial variants.
    Intermediate encodes under the lane's `work/` are never variants.
    """
    sequence = row_sequence(row)
    result: dict[str, Any] = {
        "dirs": [], "variants": [], "delivered": False, "sources": [],
        "expected": None, "note": "", "lane_mtime": None, "lane_mtime_utc": None,
    }
    if sequence is None:
        return result

    pattern = re.compile(r"^reel0?%d-batch" % sequence, re.IGNORECASE)
    result["dirs"] = sorted(
        str(WORKBENCH_ROOT / name) for name in workbench_dirnames() if pattern.match(name)
    )

    facts = row_hash_facts(row)
    found: dict[str, dict[str, Any]] = {}

    # The newest thing the lane itself wrote. A batch can be rendering hard
    # while its last BUS receipt is hours old, so this is the second, honest
    # signal that work is happening: a disk mtime, not a status anyone claimed.
    newest = 0.0
    for lane in result["dirs"]:
        try:
            with os.scandir(lane) as scan:
                for entry in scan:
                    try:
                        newest = max(newest, entry.stat().st_mtime)
                    except OSError:
                        continue
        except OSError:
            pass
    if newest:
        result["lane_mtime"] = newest
        result["lane_mtime_utc"] = utc_iso(newest)

    for lane in result["dirs"]:
        # Fix-round lanes deliver into `deliver-FIXn/`; the promoted set is
        # `deliver/`. Scan every deliver* generation except superseded -OLD
        # snapshots. Plain `deliver/` is scanned first and always wins the
        # dedupe; among fix rounds the newest (highest) round wins.
        deliver_dirs: list[str] = []
        try:
            with os.scandir(lane) as scan:
                for entry in scan:
                    if (entry.is_dir(follow_symlinks=False)
                            and entry.name.lower().startswith("deliver")
                            and "old" not in entry.name.lower()):
                        deliver_dirs.append(entry.path)
        except OSError:
            continue
        deliver_dirs.sort(key=lambda p: (os.path.basename(p) != "deliver",
                                         [-ord(c) for c in os.path.basename(p)]))
        for deliver in deliver_dirs:
            try:
                with os.scandir(deliver) as scan:
                    for entry in scan:
                        if not entry.is_file() or not entry.name.lower().endswith(VIDEO_EXT):
                            continue
                        number = variant_of(entry.name)
                        if number is None:
                            continue
                        prior = next((k for k, v in found.items()
                                      if v["variant"] == number and v["name"] == entry.name), None)
                        if prior is not None and os.path.basename(deliver) != "deliver":
                            continue
                        if prior is not None:
                            del found[prior]
                        found[entry.path] = {"variant": number, "name": entry.name,
                                             "path": entry.path,
                                             "source": f"the lane's {os.path.basename(deliver)} folder"}
            except OSError:
                continue
    if found:
        result["sources"].append("deliver folder")

    # 3. The row's OWN lane (orderd lanes):
    #    <workbench>/reelNN-<code>-<date>/deliver/variants/<stamp>/varNN.mp4, with one cast_varNN.json per
    #    planned variant. The newest stamp that holds a video is the batch; older stamps are history.
    own = re.compile(r"^reel0?%d-(?!batch)" % sequence, re.IGNORECASE)
    stamps: list[tuple[str, str]] = []
    for name in workbench_dirnames():
        if not own.match(name):
            continue
        base = WORKBENCH_ROOT / name / "deliver" / "variants"
        try:
            with os.scandir(base) as scan:
                for entry in scan:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        names = os.listdir(entry.path)
                    except OSError:
                        continue
                    if any(n.lower().endswith(VIDEO_EXT) and variant_of(n) is not None for n in names):
                        stamps.append((entry.name, entry.path))
        except OSError:
            continue
    if stamps:
        stamp, batch_dir = max(stamps)
        try:
            names = sorted(os.listdir(batch_dir))
        except OSError:
            names = []
        own_found = 0
        for n in names:
            number = variant_of(n) if n.lower().endswith(VIDEO_EXT) else None
            if number is None:
                continue
            path = os.path.join(batch_dir, n)
            found[path] = {"variant": number, "name": n, "path": path,
                           "source": "the row's deliver/variants folder"}
            own_found += 1
        casts = {int(m.group(1)) for m in (re.match(r"cast_var(\d+)\.json$", n) for n in names) if m}
        result["expected"] = len(casts) or None
        result["batch_stamp"] = stamp
        result["batch_dir"] = batch_dir
        result["older_batches"] = sorted(st for st, _ in stamps if st != stamp)
        card = next((os.path.join(batch_dir, n) for n in names if n.lower().endswith(".md") and "card" in n.lower()), None)
        result["card"] = card
        try:
            mt = os.stat(batch_dir).st_mtime
            if not result["lane_mtime"] or mt > result["lane_mtime"]:
                result["lane_mtime"] = mt
                result["lane_mtime_utc"] = utc_iso(mt)
        except OSError:
            pass
        if own_found:
            result["sources"].append("row deliver/variants")

    if media is None:
        media = row_media(row, index)
    from_index = 0
    for pool in ("versions", "untagged", "comparisons"):
        for entry in media.get(pool) or []:
            if entry.get("kind") not in ("review", "comparison-video"):
                continue
            number = variant_of(entry.get("name") or "")
            if number is None or entry["path"] in found:
                continue
            found[entry["path"]] = {"variant": number, "name": entry["name"],
                                    "path": entry["path"],
                                    "source": "named by the registry, found by the media index"}
            from_index += 1
    if from_index:
        result["sources"].append("registry + media index")

    # One row per delivered NAME: the same artifact resolves at several paths
    # (deliver/, deliver-FIXn/, archives). The deliver-folder copy is the
    # authoritative one; the index copy is only a fallback.
    _batch_re = re.compile(r"batch\s*(\d+)", re.IGNORECASE)
    by_name: dict[str, dict[str, Any]] = {}
    for entry in found.values():
        keep = by_name.get(entry["name"])
        if keep is not None and "deliver" in keep["source"]:
            continue
        by_name[entry["name"]] = entry

    variants = []
    for entry in by_name.values():
        recorded = facts.get(entry["name"]) or {}
        entry.update(_file_facts(entry["path"]))
        entry["sha256"] = recorded.get("sha256")
        entry["md5"] = recorded.get("md5")
        entry["registry_bytes"] = recorded.get("bytes")
        entry["version"] = version_of(entry["name"])
        entry["version_label"] = version_label(entry["version"])
        match = _batch_re.search(entry["name"])
        entry["batch"] = int(match.group(1)) if match else 1
        variants.append(entry)
    variants.sort(key=lambda e: (-e["batch"], e["variant"], e["name"]))

    result["variants"] = variants
    result["delivered"] = bool(variants)
    if variants:
        result["note"] = f"{len(variants)} variant(s) on disk"
        if result.get("expected") and result["expected"] > len(variants):
            result["note"] = f"{len(variants)} of {result['expected']} variants rendered so far"
    elif result["dirs"]:
        result["note"] = "the batch lane exists but nothing has landed in its deliver folder yet"
    return result


def batch_mentioned(row: dict[str, Any]) -> bool:
    """Does the row itself talk about a trial-variant batch?"""
    for text in _walk_strings(row):
        lowered = text.lower()
        if any(marker in lowered for marker in BATCH_MARKERS):
            return True
    return False


# --------------------------------------------------------------------------
# Hash / byte facts the registry already carries, keyed by basename
# --------------------------------------------------------------------------


def row_hash_facts(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """basename -> {'sha256','md5','bytes','drive_id'} harvested from the row."""
    facts: dict[str, dict[str, Any]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and _looks_like_filename(_basename(name)):
                base = _basename(name)
                if any(k in node for k in ("sha256", "md5", "bytes")):
                    record = facts.setdefault(base, {})
                    for key in ("sha256", "md5", "bytes", "drive_id"):
                        if node.get(key) is not None and key not in record:
                            record[key] = node[key]
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(row)
    return facts


def row_declared_names(row: dict[str, Any]) -> dict[str, set[str]]:
    """Basenames this row claims, split by where the claim came from.

    structured — today_files, plus every *_delivery_* -> today_review_mp4.name.
    loose      — any string anywhere in the row that ends in a media extension.
    """
    structured: set[str] = set()
    loose: set[str] = set()

    today = row.get("today_files")
    if isinstance(today, list):
        for item in today:
            if isinstance(item, str):
                base = _basename(item)
                if _looks_like_filename(base) and os.path.splitext(base)[1].lower() in MEDIA_EXTS:
                    structured.add(base)

    for key, value in row.items():
        if "_delivery_" in key and isinstance(value, dict):
            mp4 = value.get("today_review_mp4")
            if isinstance(mp4, dict) and isinstance(mp4.get("name"), str):
                base = _basename(mp4["name"])
                if _looks_like_filename(base):
                    structured.add(base)

    for text in _walk_strings(row):
        stripped = text.strip()
        ext = os.path.splitext(stripped)[1].lower()
        if ext not in MEDIA_EXTS:
            continue
        base = _basename(stripped)
        if _looks_like_filename(base):
            loose.add(base)

    return {"structured": structured, "loose": loose | structured}


# --------------------------------------------------------------------------
# Candidate discovery
# --------------------------------------------------------------------------


def _file_facts(path: str) -> dict[str, Any]:
    try:
        stat = os.stat(path)
        return {"bytes": stat.st_size, "mtime_utc": utc_iso(stat.st_mtime)}
    except OSError:
        return {"bytes": None, "mtime_utc": None}


def row_media(row: dict[str, Any], index: dict[str, list[str]]) -> dict[str, Any]:
    """Everything playable/viewable this row points at, resolved on disk."""
    declared = row_declared_names(row)
    facts = row_hash_facts(row)

    versions: list[dict[str, Any]] = []
    untagged: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    missing: list[str] = []
    seen_paths: set[str] = set()

    for name in sorted(declared["loose"]):
        ext = os.path.splitext(name)[1].lower()
        recorded = facts.get(name) or {}
        path = resolve_name_for_row(name, index, recorded.get("bytes") if isinstance(recorded.get("bytes"), int) else None,
                                    row_sequence(row))
        if path is None:
            if name in declared["structured"] and name not in REFERENCE_BASENAMES:
                missing.append(name)
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)

        entry = {
            "name": name,
            "path": path,
            "sha256": recorded.get("sha256"),
            "md5": recorded.get("md5"),
            "registry_bytes": recorded.get("bytes"),
            "drive_id": recorded.get("drive_id"),
        }
        entry.update(_file_facts(path))

        if ext == VIDEO_EXT:
            if name in REFERENCE_BASENAMES:
                continue
            if is_comparison(name):
                entry["kind"] = "comparison-video"
                comparisons.append(entry)
                continue
            number = version_of(name)
            entry["version"] = number
            entry["version_label"] = version_label(number)
            entry["kind"] = "review"
            (versions if number is not None else untagged).append(entry)
        elif ext in IMAGE_EXTS:
            entry["kind"] = "image"
            entry["version"] = version_of(name)
            comparisons.append(entry)
        else:  # .md
            entry["kind"] = "doc"
            entry["version"] = version_of(name)
            assets.append(entry)

    # A row with no version-tagged review file still deserves a player.
    promoted = False
    if not versions and untagged:
        versions = untagged
        untagged = []
        promoted = True

    versions.sort(key=lambda e: (e.get("version") if e.get("version") is not None else -1, e["name"]))
    untagged.sort(key=lambda e: e["name"])
    comparisons.sort(key=lambda e: e["name"])
    assets.sort(key=lambda e: e["name"])

    current = None
    if versions:
        wanted = row_state_version(row)
        if wanted is not None:
            for entry in versions:
                if entry.get("version") == wanted:
                    current = entry
        if current is None:
            current = max(
                versions,
                key=lambda e: (e.get("version") if e.get("version") is not None else -1, e.get("mtime_utc") or ""),
            )

    # Last resort: the row's own project_root holds renders the registry never
    # names by basename. Clearly labelled, never confused with a bound version.
    unnamed = False
    if not versions:
        fallback = [
            e for e in project_root_renders(row) if e["path"] not in seen_paths
        ]
        if fallback:
            versions = fallback
            unnamed = True
            current = max(
                versions,
                key=lambda e: (e.get("version") if e.get("version") is not None else -1, e.get("mtime_utc") or ""),
            )

    return {
        "versions": versions,
        "untagged": untagged,
        "comparisons": comparisons,
        "assets": assets,
        "missing": sorted(set(missing)),
        "current": current,
        "promoted_untagged": promoted,
        "unnamed_fallback": unnamed,
        "declared_count": len(declared["loose"]),
    }


# --------------------------------------------------------------------------
# Reference resolution
# --------------------------------------------------------------------------


def row_project_dir(row: dict[str, Any]) -> str | None:
    """The row's own project_root as an absolute directory, if it exists."""
    raw = row.get("project_root")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().rstrip("/")
    text = settings.to_workdrive(text)
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        path = REEL_ROOT / text
    if guard_path(path):
        return None
    try:
        return str(path) if path.is_dir() else None
    except OSError:
        return None


def project_root_renders(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Renders sitting in the row's own project_root that the registry never names.

    Some rows deliver to Drive under long display names while the local file
    is `reelNN_v003.mp4`; the registry records only the Drive name. The
    directory still comes from the registry (project_root), so this stays
    registry-anchored — it is labelled as unnamed so it can never be mistaken
    for a registry-bound version.
    """
    directory = row_project_dir(row)
    if directory is None:
        return []
    found: list[dict[str, Any]] = []
    for sub in ("deliver", "review", "edit", ""):
        base = os.path.join(directory, sub) if sub else directory
        if not os.path.isdir(base):
            continue
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if os.path.splitext(entry.name)[1].lower() != VIDEO_EXT:
                            continue
                        if entry.name in REFERENCE_BASENAMES or is_comparison(entry.name):
                            continue
                        stat = entry.stat()
                    except OSError:
                        continue
                    found.append(
                        {
                            "name": entry.name,
                            "path": entry.path,
                            "kind": "review",
                            "version": version_of(entry.name),
                            "version_label": version_label(version_of(entry.name)),
                            "bytes": stat.st_size,
                            "mtime_utc": utc_iso(stat.st_mtime),
                            "sha256": None,
                            "md5": None,
                            "registry_bytes": None,
                            "drive_id": None,
                            "unnamed": True,
                        }
                    )
        except OSError:
            continue
        if found:
            break
    return sorted(found, key=lambda e: (e.get("version") or -1, e["name"]))


def sibling_docs(path: str) -> list[dict[str, Any]]:
    """The .md files sitting beside a delivered render.

    Lanes write the approval card into the same deliver/ directory as the mp4,
    and only some of those names ever make it into the registry. Reading the neighbours is still
    registry-anchored: the directory comes from a file the registry named.
    """
    if guard_path(path):
        return []
    found: list[dict[str, Any]] = []
    directory = os.path.dirname(path)
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if not entry.name.lower().endswith(".md"):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                found.append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "kind": "doc",
                        "version": version_of(entry.name),
                        "bytes": stat.st_size,
                        "mtime_utc": utc_iso(stat.st_mtime),
                        "sha256": None,
                        "md5": None,
                        "registry_bytes": None,
                        "drive_id": None,
                        "beside_render": True,
                    }
                )
    except OSError:
        return []
    return sorted(found, key=lambda d: d["name"])


def row_reference(row: dict[str, Any]) -> dict[str, Any]:
    """Where the locked reference actually is, or an honest note about why not."""
    tried: list[str] = []

    shortcode = row_shortcode(row)
    candidates: list[Path] = []
    if shortcode:
        candidates.append(REFERENCE_INTAKE / shortcode / "reference-source.mp4")

    youtube = row.get("youtube_url")
    if isinstance(youtube, str):
        match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,})", youtube)
        if match:
            candidates.append(REFERENCE_INTAKE / f"yt-{match.group(1)}" / "reference-source.mp4")

    project_root = row.get("project_root")
    if isinstance(project_root, str) and project_root.strip():
        text = project_root.strip().rstrip("/")
        base = Path(os.path.expanduser(settings.to_workdrive(text)))
        if not base.is_absolute():
            base = REEL_ROOT / text
        candidates.append(base / "reference" / "reference-source.mp4")

    # Daemon-era intakes keep the reference INSIDE the workbench project, not
    # under <REEL_FACTORY_HOME>/reference-intake — trying only the old place
    # hides "Play the reference" for those rows.
    seq = row_sequence(row)
    if shortcode and seq is not None:
        try:
            for project in sorted(WORKBENCH_ROOT.glob(f"reel{seq:02d}-{shortcode}-*")):
                candidates.append(project / "reference-intake" / shortcode / "reference-source.mp4")
                candidates.append(project / "reference" / "reference-source.mp4")
        except OSError:
            pass

    for candidate in candidates:
        tried.append(str(candidate))
        try:
            if candidate.is_file():
                stat = candidate.stat()
                return {
                    "path": str(candidate),
                    "bytes": stat.st_size,
                    "mtime_utc": utc_iso(stat.st_mtime),
                    "tried": tried,
                    "note": None,
                }
        except OSError:
            continue

    return {
        "path": None,
        "bytes": None,
        "mtime_utc": None,
        "tried": tried,
        "note": "no reference on disk"
        + (f" — looked at {len(tried)} conventional path(s)" if tried else " — the row names no shortcode, url or project root"),
    }


# --------------------------------------------------------------------------
# Studio mapping — the studio projects are a subset of the registry rows
# --------------------------------------------------------------------------


def studio_project_for(row: dict[str, Any], project_ids: list[str]) -> str | None:
    project_root = row.get("project_root")
    if isinstance(project_root, str) and project_root.strip():
        base = project_root.strip().rstrip("/").rsplit("/", 1)[-1]
        if base in project_ids:
            return base

    shortcode = row_shortcode(row)
    if shortcode:
        needle = shortcode.lower()
        for project_id in project_ids:
            if needle in project_id.lower():
                return project_id
        trimmed = needle.rstrip("-_")
        if trimmed:
            for project_id in project_ids:
                if trimmed in project_id.lower():
                    return project_id
    return None


def sequence_for_project(project_id: str, rows: list[dict[str, Any]], project_ids: list[str]) -> int | None:
    for row in rows:
        if studio_project_for(row, project_ids) == project_id:
            return row_sequence(row)
    return None
