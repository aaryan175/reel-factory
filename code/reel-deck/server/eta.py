"""
ETA — "review ready ~X", learned from the factory's own history.

Nobody in the factory declares how long a phase takes. But every phase leaves
a receipt on the bus, and the bus's file mtimes are the whole training set:

    reelNN-batch01-feasibility-<date>.md      the lane says the batch is possible
    reelNN-batch01-cast-<date>.md             clips chosen
    reelNN-batch01-build-<date>.md            first cut rendered
    reelNN-batch01-INDEP-AUDIT-<date>.md      audited
    reelNN-batch01-fix2-<date>.md             fix round
    reelNN-batch01-fix2-INDEP-AUDIT-<date>.md that round audited
    reelNN-batch01-cardfix/-delivery-<date>.md handed over -> NEEDS YOU

Sorting one row's receipts by mtime and taking consecutive gaps gives a
duration for each phase (the gap is charged to the phase that ENDS it). The
registry rows carry the same skeleton in their key names, so keys with a
parseable timestamp inside are folded in as extra events.

From those samples: median / p25 / p75 per phase, plus the median number of
fix rounds a batch actually needs. A row in the machine is then placed on that
skeleton by its newest event, and the remaining phase medians are summed.

Everything here is an estimate and is labelled as one. Where the phase cannot
be read off the evidence, this module returns None and the UI shows nothing —
a wrong time is worse than no time.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import settings

BUS_DIR = settings.BUS_DIR

MODEL_TTL_SECONDS = 3600.0

FEASIBILITY = "feasibility"
CAST = "cast"
BUILD = "build"
AUDIT = "audit"
FIX = "fix_round"
DELIVER = "cardfix_deliver"
# New-reel intake lanes (a fresh operator drop) run intake -> study -> build
# -> audit -> deliver. They share build/audit/fix/deliver with the batches.
INTAKE = "intake"
STUDY = "study"

PHASE_ORDER = (FEASIBILITY, CAST, BUILD, AUDIT, FIX, DELIVER, INTAKE, STUDY)

# Hours. Used when history is too thin for a phase to speak for itself.
PRIORS = {
    FEASIBILITY: 0.75,
    CAST: 1.5,
    BUILD: 3.5,
    AUDIT: 1.5,
    FIX: 4.0,
    DELIVER: 0.75,
    INTAKE: 0.15,
    STUDY: 1.0,
}

PHASE_LABELS = {
    FEASIBILITY: "feasibility",
    CAST: "casting",
    BUILD: "build",
    AUDIT: "audit",
    FIX: "fix round",
    DELIVER: "delivery",
    INTAKE: "intake",
    STUDY: "reference study",
}

# A fresh drop is a single-candidate lane; the clean-pipeline standard is one
# fix round, not the batches' three.
INTAKE_FIX_ROUNDS = 1.0

# The rows the model learns from: those with a full receipt trail. Configure them with
# REEL_DECK_ETA_HISTORY_ROWS="1,2,3"; unset = every row that has receipts on the bus.
HISTORY_ROWS: tuple[int, ...] | None = tuple(
    int(v) for v in (os.environ.get("REEL_DECK_ETA_HISTORY_ROWS") or "").split(",") if v.strip().isdigit()
) or None


def _history_rows(by_row: dict) -> list[int]:
    return list(HISTORY_ROWS) if HISTORY_ROWS else sorted(by_row)

MIN_SAMPLES = 3         # below this a phase falls back to its prior
MAX_GAP_HOURS = 48.0    # a longer gap is the factory idle, not a phase running
DEDUPE_MINUTES = 90.0   # same phase twice inside this window = one event
DEFAULT_FIX_ROUNDS = 3.0
MIN_REMAINING_HOURS = 0.25

_REEL_RE = re.compile(r"^reel[-_ ]?0?(\d{1,2})(?![0-9])")
_FIXNUM_RE = re.compile(r"fix[-_ ]?(\d{1,2})(?![0-9])")
_CAST_RE = re.compile(r"(?<![a-z])cast(?![a-z])")
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")

# Registry keys whose value dict may carry the moment the phase landed.
_TIME_FIELDS = (
    "at_utc", "ran_utc", "written_at_utc", "registered_at_utc", "audited_at_utc",
    "built_at_utc", "date_utc", "at", "when", "timestamp",
)

_model_lock = threading.Lock()
_model: dict[str, Any] | None = None
_model_at = 0.0


# --------------------------------------------------------------------- parsing


def phase_of(name: str) -> str | None:
    """Which phase a receipt filename or registry key belongs to, if any.

    Order matters: `fix1-INDEP-AUDIT` is an audit, `fix4-delivery` is a
    delivery. The most specific reading wins.
    """
    low = name.lower().replace("_", "-")
    if "superseded" in low:
        return None
    if "audit" in low:
        return AUDIT
    if "cardfix" in low or "deliver" in low:
        return DELIVER
    if _FIXNUM_RE.search(low):
        return FIX
    if "study" in low:
        return STUDY
    if "intake" in low:
        return INTAKE
    if "feasibility" in low:
        return FEASIBILITY
    if _CAST_RE.search(low):
        return CAST
    if "build" in low:
        return BUILD
    return None


def fix_round_of(name: str) -> int | None:
    match = _FIXNUM_RE.search(name.lower().replace("_", "-"))
    return int(match.group(1)) if match else None


def sequence_of(name: str) -> int | None:
    match = _REEL_RE.match(name.lower())
    return int(match.group(1)) if match else None


def _parse_iso(value: Any) -> float | None:
    """Epoch seconds from an ISO-ish timestamp string, or None."""
    if not isinstance(value, str):
        return None
    match = _ISO_RE.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute = (int(match.group(i)) for i in range(1, 6))
    second = int(match.group(6) or 0)
    try:
        return datetime(year, month, day, hour, minute, second,
                        tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _registry_key_time(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    for field in _TIME_FIELDS:
        stamp = _parse_iso(value.get(field))
        if stamp is not None:
            return stamp
    return None


# ---------------------------------------------------------------- event stream


def bus_events() -> dict[int, list[dict[str, Any]]]:
    """Every phase-bearing bus receipt, grouped by registry row."""
    out: dict[int, list[dict[str, Any]]] = {}
    try:
        entries = [e for e in os.scandir(BUS_DIR) if e.is_file() and e.name.endswith(".md")]
    except OSError:
        return out
    for entry in entries:
        sequence = sequence_of(entry.name)
        if sequence is None:
            continue
        phase = phase_of(entry.name)
        if phase is None:
            continue
        try:
            when = entry.stat().st_mtime
        except OSError:
            continue
        out.setdefault(sequence, []).append({
            "at": when, "phase": phase, "source": entry.name,
            "fix_round": fix_round_of(entry.name),
        })
    for events in out.values():
        events.sort(key=lambda e: e["at"])
    return out


def registry_events(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Phase events a registry row records itself, when a key carries a time."""
    events: list[dict[str, Any]] = []
    for key, value in row.items():
        phase = phase_of(key)
        if phase is None:
            continue
        when = _registry_key_time(value)
        if when is None:
            continue
        events.append({"at": when, "phase": phase, "source": f"registry:{key}",
                       "fix_round": fix_round_of(key)})
    events.sort(key=lambda e: e["at"])
    return events


def merge_events(*streams: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One timeline. The same phase landing twice inside DEDUPE_MINUTES is the
    bus receipt and the registry key describing one event, not two."""
    merged: list[dict[str, Any]] = []
    for stream in streams:
        merged.extend(stream)
    merged.sort(key=lambda e: e["at"])
    kept: list[dict[str, Any]] = []
    window = DEDUPE_MINUTES * 60.0
    for event in merged:
        twin = next((k for k in reversed(kept)
                     if k["phase"] == event["phase"]
                     and k.get("fix_round") == event.get("fix_round")
                     and event["at"] - k["at"] <= window), None)
        if twin is None:
            kept.append(event)
    return kept


# ---------------------------------------------------------------------- stats


def _quantile(values: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = q * (len(values) - 1)
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def _stats(samples: list[float], phase: str) -> dict[str, Any]:
    prior = PRIORS[phase]
    if len(samples) < MIN_SAMPLES:
        return {"median_h": prior, "p25_h": prior * 0.6, "p75_h": prior * 1.6,
                "n": len(samples), "source": "prior"}
    ordered = sorted(samples)
    return {
        "median_h": round(_quantile(ordered, 0.5), 3),
        "p25_h": round(_quantile(ordered, 0.25), 3),
        "p75_h": round(_quantile(ordered, 0.75), 3),
        "n": len(ordered),
        "source": "history",
    }


def _fit(by_row: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    samples: dict[str, list[float]] = {phase: [] for phase in PHASE_ORDER}
    fix_round_counts: list[float] = []
    totals: list[float] = []

    for sequence in _history_rows(by_row):
        events = by_row.get(sequence) or []
        if len(events) < 2:
            continue
        for previous, current in zip(events, events[1:]):
            # A gap only measures work when both ends sit inside one run.
            # Feasibility opens a run and delivery closes one, so a gap into
            # feasibility or out of delivery is the factory idle between
            # batches — charging that to a phase would read as days of work.
            if current["phase"] == FEASIBILITY or previous["phase"] == DELIVER:
                continue
            gap = (current["at"] - previous["at"]) / 3600.0
            if 0 < gap <= MAX_GAP_HOURS:
                samples[current["phase"]].append(gap)

        rounds = {e["fix_round"] for e in events if e["phase"] == FIX and e["fix_round"]}
        if rounds:
            fix_round_counts.append(float(len(rounds)))

        # Wall time of the last complete run: its first ordering event through
        # the delivery that handed it back to the operator.
        last_delivery = next((e for e in reversed(events) if e["phase"] == DELIVER), None)
        if last_delivery is not None:
            openers = [e for e in events
                       if e["phase"] in (FEASIBILITY, CAST, BUILD) and e["at"] < last_delivery["at"]]
            if openers:
                start = max(openers, key=lambda e: last_delivery["at"] - e["at"])
                total = (last_delivery["at"] - start["at"]) / 3600.0
                if 0 < total <= MAX_GAP_HOURS * 4:
                    totals.append(total)

    ordered_totals = sorted(totals)
    return {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows_seen": sorted(s for s in _history_rows(by_row) if by_row.get(s)),
        "events": sum(len(by_row.get(s) or []) for s in _history_rows(by_row)),
        "phases": {phase: _stats(samples[phase], phase) for phase in PHASE_ORDER},
        "fix_rounds": {
            "median": (round(_quantile(sorted(fix_round_counts), 0.5), 2)
                       if len(fix_round_counts) >= 2 else DEFAULT_FIX_ROUNDS),
            "n": len(fix_round_counts),
            "source": "history" if len(fix_round_counts) >= 2 else "prior",
        },
        "total_hours": {
            "median_h": round(_quantile(ordered_totals, 0.5), 2) if ordered_totals else None,
            "p25_h": round(_quantile(ordered_totals, 0.25), 2) if ordered_totals else None,
            "p75_h": round(_quantile(ordered_totals, 0.75), 2) if ordered_totals else None,
            "n": len(ordered_totals),
        },
    }


def build_model() -> dict[str, Any]:
    """Fit the duration model. Scans the bus — call through model()."""
    return _fit(bus_events())


def model(force: bool = False) -> dict[str, Any]:
    """The fitted model, cached for MODEL_TTL_SECONDS."""
    global _model, _model_at
    with _model_lock:
        now = datetime.now(timezone.utc).timestamp()
        if force or _model is None or now - _model_at > MODEL_TTL_SECONDS:
            _model = build_model()
            _model_at = now
        return _model


# ------------------------------------------------------------------- estimate


def _remaining_phases(phase: str, rounds_done: int, fix_rounds_median: float) -> list[str]:
    """The phases still between this row and the operator's desk.

    Each remaining fix round drags its own audit along with it — that is how
    every completed batch on the bus actually ran.
    """
    rounds_left = max(0, int(round(fix_rounds_median)) - rounds_done)
    if phase == INTAKE:
        tail = [STUDY, BUILD, AUDIT]
    elif phase == STUDY:
        tail = [BUILD, AUDIT]
    elif phase == FEASIBILITY:
        tail = [CAST, BUILD, AUDIT]
    elif phase == CAST:
        tail = [BUILD, AUDIT]
    elif phase == BUILD:
        tail = [AUDIT]
    elif phase == AUDIT:
        tail = []
    elif phase == FIX:
        tail = [AUDIT]
    else:
        return []
    return tail + [FIX, AUDIT] * rounds_left + [DELIVER]


def local_label(when: datetime) -> str:
    """`~9:40 pm` in the configured display timezone (REEL_FACTORY_TZ, default UTC)."""
    local = when.astimezone(settings.local_tz())
    hour = local.hour % 12 or 12
    return f"~{hour}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"


def estimate(sequence: int, row: dict[str, Any] | None = None,
             lane_mtime: float | None = None, events: list[dict[str, Any]] | None = None,
             now: float | None = None, fitted: dict[str, Any] | None = None
             ) -> dict[str, Any] | None:
    """When this row should land on the operator's desk, or None if unreadable."""
    fitted = fitted or model()
    if events is None:
        events = merge_events(bus_events().get(sequence) or [],
                              registry_events(row) if row else [])
    if not events:
        return None

    newest = events[-1]
    phase = newest["phase"]
    if phase == DELIVER:
        return None  # already handed over; nothing left to predict

    rounds_done = len({e["fix_round"] for e in events if e["phase"] == FIX and e["fix_round"]})
    is_intake_lane = any(e["phase"] in (INTAKE, STUDY) for e in events)
    fix_target = INTAKE_FIX_ROUNDS if is_intake_lane else fitted["fix_rounds"]["median"]
    remaining = _remaining_phases(phase, rounds_done, fix_target)
    if not remaining:
        return None

    stats = fitted["phases"]
    totals = {
        key: sum(stats[p][f"{key}_h"] for p in remaining)
        for key in ("median", "p25", "p75")
    }

    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    anchor = max(newest["at"], lane_mtime or 0.0)
    elapsed = max(0.0, (now - anchor) / 3600.0)

    left = max(MIN_REMAINING_HOURS, totals["median"] - elapsed)
    low = max(MIN_REMAINING_HOURS, totals["p25"] - elapsed)
    high = max(left, totals["p75"] - elapsed)

    eta = datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=left)
    spread = max(MIN_REMAINING_HOURS, (high - low) / 2.0)
    label = local_label(eta)

    return {
        "phase": phase,
        "phase_label": (f"{PHASE_LABELS[FIX]} {rounds_done}"
                        if phase == FIX and rounds_done else PHASE_LABELS[phase]),
        "rounds_done": rounds_done,
        "remaining_phases": remaining,
        "remaining_hours": round(left, 2),
        "spread_hours": round(spread, 1),
        "eta_utc": eta.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "eta_epoch": eta.timestamp(),
        "eta_local_label": label,
        "line": f"review ready {label} (±{spread:.1f}h)",
        "anchor_utc": datetime.fromtimestamp(anchor, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "anchor_source": newest["source"],
    }
