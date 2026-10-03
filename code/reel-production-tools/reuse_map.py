#!/usr/bin/env python3
"""Canonical cross-row clip-reuse map for the reel factory.

Every reuse claim on an approval card MUST cite this tool's output. Re-implementing
the extraction per lane (an ad-hoc overlap script, a hand-written list, a per-project
reuse table) produces inconsistent, wrong enumerations. The failure modes this tool
exists to end:

  1. Reading the EARLIEST draft (`selection-v1.json`) instead of the version the
     row actually shipped, which produces false entries.
  2. A source-key list that omits fields such as `remote_path` / `local_path`,
     which makes rows with parsable delivered plans look unreadable (omissions).
  3. Reading master ids out of free prose. A record may NAME a clip in a
     rejected-alternate note without shipping it.

The rules here, in order of importance:

  * A row resolves to its DELIVERED cut and nothing else. The delivered plan
    file for each row is declared below with the reason it is the delivered one,
    checked against the registry's own `version` string.
  * Master ids come only from declared source-key fields of slot records. Never
    from prose, notes, runner-up lists or rejected-alternate blocks.
  * A row whose delivered cut is NOT on disk in structured form is UNRESOLVED
    and says so. It is never silently approximated from a draft.

Usage:
    reuse_map.py --rebuild          rebuild REUSE_MAP.json from disk
    reuse_map.py --row 2            per-slot reuse table for one reel
    reuse_map.py --master 0029      every delivered slot a master ships in
    reuse_map.py                    summary + unresolved rows
"""

from __future__ import annotations

import argparse
import glob as globlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

REPO = os.environ.get("REEL_FACTORY_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(REPO, "REEL_REGISTRY.json")
OUT = os.path.join(REPO, "REUSE_MAP.json")


class ReuseMapError(RuntimeError):
    """A declared source of delivered slots did not resolve. Never downgraded to a warning.

    The whole point of this tool is that a reuse claim can be trusted. A blind spot
    that reports itself as an empty result is worse than a crash: it lets a card claim
    "zero collision" while clips are shipping in trial variants the map cannot see.
    """

# ---------------------------------------------------------------------------
# master id extraction
# ---------------------------------------------------------------------------

# Every key that has ever carried a slot's source on disk, across the six
# schemas in use. `remote_path` and `local_path` are the two ad-hoc
# extractors miss; `master` and `clip_file` come from older schemas.
SOURCE_KEYS = (
    "source",
    "source_path",
    "source_file",
    "master",
    "master_path",
    "remote_path",
    "local_path",
    "path",
    "file",
    "filename",
    "basename",
    "clip",
    "clip_file",
)

# Keys that identify which slot a record is. Ordered by specificity.
SLOT_KEYS = ("block_id", "block", "slot", "shot", "id", "state", "i")

# CLIP_0013 · CAM_0001 · C0001 · IMG_0001 — optionally Drive-id-prefixed
# (<drive-id>__CAM_0001.MP4) or content-hash
# prefixed (0123456789abcdef__CAM_0002.MP4), optionally under a
# "day 3/" corpus path, with or without the extension.
MASTER_RE = re.compile(r"(?:^|[/_])((?:CLIP_\d{4})|(?:CAM_\d{4})|(?:C\d{4,5})|(?:IMG_\d{3,5}))(?:\.[A-Za-z0-9]+)?$")


def master_id(value: Any) -> str | None:
    """Canonical master id from one source-key value, or None.

    Accepts a bare id, a basename, a Drive/hash-prefixed basename, or a full
    path. Returns None for anything that is not a master reference, so that a
    key carrying a note, a directory or a hash never enters the map.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    stem = os.path.basename(value.strip().replace("\\", "/"))
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    m = MASTER_RE.search(stem) or MASTER_RE.search(value.strip())
    return m.group(1) if m else None


def slot_id(rec: dict, fallback_index: int) -> str:
    for k in SLOT_KEYS:
        if k in rec and isinstance(rec[k], (str, int)) and str(rec[k]).strip():
            v = str(rec[k]).strip()
            return v if not v.isdigit() else f"{k}{int(v):03d}"
    return f"idx{fallback_index:03d}"


def slot_records(doc: Any) -> list[dict]:
    """The slot list of a plan document, whichever of the six schemas it is."""
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    if not isinstance(doc, dict):
        return []
    for key in ("slots", "shots", "states", "segments", "blocks", "picture_slots"):
        v = doc.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def sources_of(rec: dict) -> list[str]:
    """Master ids named by this slot record's declared source keys only.

    Deliberately shallow: it does not walk into `runner_ups`, `rejected_*`,
    `disclosures`, `candidate_observation` or any other nested prose, because
    those name masters the slot did NOT ship.
    """
    out: list[str] = []
    for k in SOURCE_KEYS:
        mid = master_id(rec.get(k))
        if mid and mid not in out:
            out.append(mid)
    return out


# ---------------------------------------------------------------------------
# delivered-cut resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Superseder:
    """An append-only record that replaces one or more slots after the base."""

    path: str
    slot: str | None = None  # explicit slot when the file does not name its own
    note: str = ""


@dataclass(frozen=True)
class AlsoPlan:
    """A SECOND delivered cut of the same row, shipped alongside the base.

    A row may ship TWO labelled mp4s (a main and a labelled cut-density variant) with
    DIFFERENT slot tables. Without this the map could only name one of them, so a
    master that shipped only in the variant would be invisible to every reuse claim
    — a silent under-report.

    A supersession is the wrong shape here: it REPLACES a slot. Both cuts were
    delivered, so both slot tables are true at once; the variant's slots are
    namespaced with `prefix` so neither overwrites the other.
    """

    path: str
    prefix: str  # e.g. "var:" -> slots land as var:p001 …
    note: str = ""


@dataclass(frozen=True)
class HistoricShip:
    """A SUPERSEDED but genuinely DELIVERED cut of the same row.

    The failure it fixes: this tool resolves a row to its NEWEST delivered cut and
    nothing else, so a master that shipped in an EARLIER delivered version of that
    row is invisible, and a card can wrongly call it a "first ship" even though
    earlier delivered versions carried it. A version being superseded does not un-ship it: `first_ship` means "no delivered
    cut anywhere has ever carried this master", so every delivered cut has to be in
    the universe, not just the current one.

    A supersession is the wrong shape (it REPLACES a slot in the current cut) and
    `also` is the wrong shape (it is a second cut shipped ALONGSIDE the base, both
    current). This is a PAST cut: its slots are true history, they are namespaced
    with `prefix` so they never overwrite the live table, and they count for
    first-ship questions and for nothing else.

    Only versions whose delivery the REGISTRY records (a delivery id) belong here. A
    draft that never left the workbench is not a ship — docstring rule 1 still holds.
    """

    path: str
    prefix: str  # e.g. "v005:" -> slots land as v005:p019 …
    note: str = ""


@dataclass(frozen=True)
class VariantBatch:
    """A batch of trial-variant films of one row, each with its own locked contract.

    A row may ship several trial variants alongside its main cut; when every
    re-render is captions-only, each variant's PICTURE is its locked selection.
    Naming those slots in a prose string (or pointing at a draft castings folder
    whose entries were later replaced) leaves the map blind to them, so variant
    contracts are walked explicitly.

    `glob_pattern` is walked at rebuild time. Zero matches raises rather than
    silently emitting nothing.
    """

    glob_pattern: str  # absolute or repo-relative; may contain *
    note: str
    exclude: tuple[str, ...] = ()  # substrings that mark a superseded contract dir
    label_index: int = -4  # path part that names the variant, counting from the file
    expect_at_least: int = 1


@dataclass(frozen=True)
class RowSpec:
    version: str  # the version this row shipped, per REEL_REGISTRY
    base: str | None  # delivered base plan, repo- or home-relative
    reason: str  # why THIS file is the delivered cut
    supersedes: tuple[Superseder, ...] = ()
    also: tuple[AlsoPlan, ...] = ()  # further cuts of the SAME row, both delivered
    variants: tuple[VariantBatch, ...] = ()  # trial-variant films of the SAME row
    historic: tuple[HistoricShip, ...] = ()  # SUPERSEDED but delivered cuts of the SAME row
    unresolved: str = ""  # non-empty => declared blind spot, no slots emitted


# The delivered cut of every row. Each entry is checked against the registry's
# own `version` string at rebuild time; a mismatch is reported, not swallowed.
#
# Precedence used to fill this in, highest first:
#   1. a single file that IS the delivered slate for the shipped version
#
#   2. the project's locked selection for the shipped version, plus every
#      append-only recast/reseat record up to that version
#   3. the last picture selection before a captions-only chain of versions
#
#   4. UNRESOLVED
ROWS: dict[int, RowSpec] = {
    # Declare one RowSpec per delivered reel. Left empty in this handover: the
    # production registry it described is not part of the method. A synthetic
    # example of every field follows; copy its shape for real rows.
}

# Illustrative only (never read by build()). Paths are repo- or home-relative.
EXAMPLE_ROWS: dict[int, RowSpec] = {
    1: RowSpec(
        version="v2",
        base="<reel-01>/edit/selection-v2.json",
        reason="registry version is v2 and selection-v2.json is its locked selection. "
        "selection-v1.json is a draft one revision back and is NOT read.",
        supersedes=(
            Superseder(
                path="<reel-01>/edit/recast-p004-v2.json",
                note="p004 re-cast CLIP_0003 -> CLIP_0004 per review pass 2",
            ),
        ),
        also=(
            AlsoPlan(
                path="<reel-01>/work/picture-contract-variant-v2.json",
                prefix="var:",
                note="cut-density variant, second delivered mp4 of the same row",
            ),
        ),
        historic=(
            HistoricShip(
                path="<reel-01>/edit/selection-v1-delivered.json",
                prefix="v1:",
                note="v1 was delivered (registry records its delivery id) before v2 superseded it",
            ),
        ),
    ),
    2: RowSpec(
        version="v13",
        base="<reel-02>/edit/selection.locked.json",
        reason="main cut; ten trial variants ship alongside it, captions-only re-renders",
        variants=(
            VariantBatch(
                glob_pattern="<reel-02>/variants/var*/edit/selection.locked.json",
                note="each trial variant's PICTURE is its own locked selection",
                exclude=("/superseded/",),
                expect_at_least=10,
            ),
        ),
    ),
    3: RowSpec(
        version="v1",
        base=None,
        reason="no structured plan on disk",
        unresolved="delivered cut exists only as an mp4; no slot table was ever written",
    ),
}


def resolve(path: str) -> str:
    p = os.path.expanduser(path)
    return p if os.path.isabs(p) else os.path.join(REPO, p)


def load(path: str) -> Any:
    with open(resolve(path), "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# supersession records
# ---------------------------------------------------------------------------


def superseding_slots(doc: Any, forced_slot: str | None) -> list[tuple[str, str]]:
    """(slot, master_id) pairs a recast/reseat record installs.

    Five on-disk shapes:
      recast:   {"recast":   {slot: {"was": {...}, "now": {"source": ...}}}}
      reseat:   {"reseated": {slot: {"source": ...}}}
      install:  {"block_id": slot, "installed": {"source_path": ...}}
      slots:    {"slots": [{"slot": slot, "clip_file": ...}]}
      contract: {"block": slot, "source_path": ...}
    Only the INSTALLED side is ever read; `was`/`removed`/`runner_ups` are the
    masters the slot stopped shipping.
    """
    out: list[tuple[str, str]] = []
    if not isinstance(doc, dict):
        for i, rec in enumerate(slot_records(doc)):
            for mid in sources_of(rec):
                out.append((forced_slot or slot_id(rec, i), mid))
        return out

    for container in ("recast", "reseated", "reseat"):
        block = doc.get(container)
        if isinstance(block, dict):
            for slot, rec in block.items():
                if not isinstance(rec, dict):
                    continue
                installed = rec.get("now") if isinstance(rec.get("now"), dict) else rec
                for mid in sources_of(installed):
                    out.append((slot, mid))
    if out:
        return out

    installed = doc.get("installed")
    if isinstance(installed, dict):
        slot = forced_slot or doc.get("block_id") or doc.get("block") or doc.get("slot")
        for mid in sources_of(installed):
            out.append((str(slot), mid))
        if out:
            return out

    recs = slot_records(doc)
    if recs:
        for i, rec in enumerate(recs):
            for mid in sources_of(rec):
                out.append((forced_slot or slot_id(rec, i), mid))
        return out

    # A flat single-slot contract: the document itself is the slot record.
    slot = forced_slot or doc.get("block_id") or doc.get("block") or doc.get("slot")
    if slot is not None:
        for mid in sources_of(doc):
            out.append((str(slot), mid))
    return out


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


@dataclass
class RowResult:
    row: int
    version: str
    slots: dict[str, list[str]] = field(default_factory=dict)  # slot -> master ids
    slot_file: dict[str, str] = field(default_factory=dict)  # slot -> file it came from
    provenance: dict[str, Any] = field(default_factory=dict)


def build_row(seq: int, spec: RowSpec, registry_version: str) -> RowResult:
    res = RowResult(row=seq, version=spec.version)
    prov: dict[str, Any] = {
        "shipped_version": spec.version,
        "registry_version": registry_version,
        "reason": spec.reason,
    }
    if registry_version and spec.version.split()[0] not in registry_version:
        prov["version_string_check"] = (
            f"NOTE: registry `version` does not literally contain '{spec.version.split()[0]}'; "
            "resolution reason above states why this is still the delivered cut"
        )

    if spec.unresolved:
        prov["status"] = "UNRESOLVED"
        prov["unresolved"] = spec.unresolved
        prov["base_file"] = None
        res.provenance = prov
        return res

    assert spec.base is not None
    base_path = resolve(spec.base)
    if not os.path.exists(base_path):
        prov["status"] = "UNRESOLVED"
        prov["unresolved"] = f"declared base plan is missing on disk: {spec.base}"
        res.provenance = prov
        return res

    doc = load(spec.base)
    recs = slot_records(doc)
    sourceless: list[str] = []
    for i, rec in enumerate(recs):
        sid = slot_id(rec, i)
        mids = sources_of(rec)
        if mids:
            res.slots[sid] = mids
            res.slot_file[sid] = spec.base
        else:
            sourceless.append(sid)

    prov["status"] = "RESOLVED"
    prov["base_file"] = spec.base
    prov["base_records"] = len(recs)
    prov["base_slots"] = len(res.slots)
    if sourceless:
        # A base whose records MOSTLY lack a source key is an OVERRIDE DELTA, not
        # a full cut — one row's picture plans listed all 49 blocks but sourced only
        # the 6 that changed. Reading one as a base silently drops the rest.
        #
        # A handful of sourceless records is not that: one row's contract has one,
        # a 2-frame BLACK TAIL that has no master by design. Firing the delta
        # warning on it made the guard permanently red, and a permanently red
        # guard is not a guard — hence the majority test the docstring always
        # described. The record list itself is emitted either way, so nothing is
        # hidden by the threshold.
        prov["base_records_without_a_source"] = sourceless
        if len(sourceless) * 2 > len(recs):
            prov["delta_warning"] = (
                f"{len(sourceless)}/{len(recs)} base records name no source. If this base is an "
                "override delta rather than a full cut, the row is under-reported — resolve it to "
                "the full selection and list the delta as a supersession instead."
            )
    prov["supersessions"] = []

    for sup in spec.supersedes:
        sup_path = resolve(sup.path)
        entry: dict[str, Any] = {"file": sup.path, "note": sup.note, "replaced": {}}
        if not os.path.exists(sup_path):
            entry["error"] = "MISSING ON DISK — this row's map is incomplete"
            prov["supersessions"].append(entry)
            prov["status"] = "PARTIAL"
            continue
        for slot, mid in superseding_slots(load(sup.path), sup.slot):
            entry["replaced"][slot] = {"was": res.slots.get(slot), "now": [mid]}
            res.slots[slot] = [mid]
            res.slot_file[slot] = sup.path
        prov["supersessions"].append(entry)

    prov["also_delivered"] = []
    for alt in spec.also:
        alt_path = resolve(alt.path)
        aentry: dict[str, Any] = {"file": alt.path, "prefix": alt.prefix, "note": alt.note}
        if not os.path.exists(alt_path):
            aentry["error"] = "MISSING ON DISK — this row's map is incomplete"
            prov["also_delivered"].append(aentry)
            prov["status"] = "PARTIAL"
            continue
        arecs = slot_records(load(alt.path))
        added = 0
        for i, rec in enumerate(arecs):
            mids = sources_of(rec)
            if not mids:
                continue
            sid = alt.prefix + slot_id(rec, i)
            res.slots[sid] = mids
            res.slot_file[sid] = alt.path
            added += 1
        aentry["slots_added"] = added
        prov["also_delivered"].append(aentry)

    prov["variant_contracts"] = []
    for batch in spec.variants:
        prov["variant_contracts"].append(walk_variant_batch(seq, batch, res))

    # SUPERSEDED-BUT-DELIVERED cuts of this row. See HistoricShip: a version being
    # replaced does not un-ship it, and first_ship means "no delivered cut anywhere".
    prov["historic_ships"] = []
    for hist in spec.historic:
        hpath = resolve(hist.path)
        hentry: dict[str, Any] = {"file": hist.path, "prefix": hist.prefix, "note": hist.note}
        if not os.path.exists(hpath):
            hentry["error"] = "MISSING ON DISK — this row's delivered history is incomplete"
            prov["historic_ships"].append(hentry)
            prov["status"] = "PARTIAL"
            continue
        hrecs = slot_records(load(hist.path))
        hadded = 0
        for i, rec in enumerate(hrecs):
            mids = sources_of(rec)
            if not mids:
                continue
            sid = hist.prefix + slot_id(rec, i)
            res.slots[sid] = mids
            res.slot_file[sid] = hist.path
            hadded += 1
        hentry["slots_added"] = hadded
        prov["historic_ships"].append(hentry)

    prov["delivered_slots"] = len(res.slots)
    res.provenance = prov
    return res


def variant_label(path: str, batch: VariantBatch) -> str:
    """The variant's own name, from the path part the batch declares.

    Taken from the contract's own location rather than from anything inside the
    file, because these contracts do not name themselves — var08's selection has
    no `variant` key, which is why a hand-walk was the only way to find `CLIP_0011`.
    """
    parts = path.split(os.sep)
    return parts[batch.label_index] if len(parts) >= abs(batch.label_index) else os.path.dirname(path)


def walk_variant_batch(seq: int, batch: VariantBatch, res: RowResult) -> dict[str, Any]:
    """Read every live contract in a trial-variant batch into namespaced slots.

    Raises rather than returning an empty result: an unreadable batch used to look
    exactly like a row with no variants, and that is the failure being fixed.
    """
    pattern = batch.glob_pattern
    if not os.path.isabs(os.path.expanduser(pattern)):
        pattern = os.path.join(REPO, pattern)
    pattern = os.path.expanduser(pattern)

    found = sorted(globlib.glob(pattern))
    live = [p for p in found if not any(x in p for x in batch.exclude)]

    if not live:
        raise ReuseMapError(
            f"row {seq}: variant batch resolved to ZERO live contracts.\n"
            f"  glob     : {pattern}\n"
            f"  matched  : {len(found)} paths, {len(found) - len(live)} excluded as superseded\n"
            f"  note     : {batch.note}\n"
            "  A declared variant batch that reads as empty is a silent under-report of every "
            "reuse claim on this row. Fix the path or delete the batch — do not let it pass. "
            "If the contracts live on the workbench volume, mount it and rebuild."
        )
    if len(live) < batch.expect_at_least:
        raise ReuseMapError(
            f"row {seq}: variant batch resolved {len(live)} live contracts, "
            f"expected at least {batch.expect_at_least}.\n  glob: {pattern}\n"
            f"  found: {[variant_label(p, batch) for p in live]}"
        )

    entry: dict[str, Any] = {
        "glob": pattern,
        "note": batch.note,
        "contracts_matched": len(found),
        "contracts_excluded_as_superseded": [
            os.path.relpath(p, os.path.dirname(os.path.dirname(pattern.split("*")[0])))
            for p in found
            if p not in live
        ],
        "contracts_read": [],
        "slots_added": 0,
    }

    for path in live:
        label = variant_label(path, batch)
        doc = load(path)
        recs = slot_records(doc)
        added = 0
        for i, rec in enumerate(recs):
            mids = sources_of(rec)
            if not mids:
                continue
            sid = f"{label}:{slot_id(rec, i)}"
            res.slots[sid] = mids
            res.slot_file[sid] = path
            added += 1
        entry["contracts_read"].append(
            {
                "variant": label,
                "file": path,
                "status": doc.get("status") if isinstance(doc, dict) else None,
                "records": len(recs),
                "slots_added": added,
            }
        )
        entry["slots_added"] += added

    if entry["slots_added"] == 0:
        raise ReuseMapError(
            f"row {seq}: {len(live)} variant contracts read but NOT ONE named a source. "
            "The schema changed under the tool; fix sources_of/SOURCE_KEYS rather than "
            "shipping a map that under-reports this row."
        )
    return entry


def rows_outside_the_tool(reg: dict[str, Any]) -> list[dict[str, Any]]:
    """Registry rows that HAVE a delivered cut but are not in this tool's ROWS universe.

    The tool's blind spot can be WIDER than the UNRESOLVED caveat it prints: rows that are
    not in ROWS at all but have delivered cuts are invisible to every FIRST-SHIP claim
    built on this tool, so they are listed explicitly.

    A row counts as having a delivered cut if the registry says so: a non-empty `today_files`, or
    any build/version record carrying a `delivery` (or `*_delivery_*`) block.
    """
    def has_drive_object(node: Any, depth: int = 0) -> bool:
        """A delivered object leaves a delivery-id fingerprint somewhere in the row record."""
        if depth > 6:
            return False
        if isinstance(node, dict):
            keys = {k.lower() for k in node}
            if ("drive_file_id" in keys or "drive_id" in keys
                    or ({"md5", "bytes"} <= keys) or ({"id", "md5"} <= keys)):
                return True
            return any(has_drive_object(v, depth + 1) for v in node.values())
        if isinstance(node, list):
            return any(has_drive_object(v, depth + 1) for v in node)
        return False

    out: list[dict[str, Any]] = []
    for r in reg.get("reels", []):
        seq = r.get("sequence")
        if seq is None or seq in ROWS:
            continue
        evidence = []
        tf = r.get("today_files")
        if isinstance(tf, (list, dict)) and tf:
            evidence.append(f"today_files={len(tf)}")
        for k, v in r.items():
            if not isinstance(v, dict):
                continue
            if "delivery" in k.lower() or "delivery" in {kk.lower() for kk in v}:
                evidence.append(k)
            elif k.lower().startswith(("build", "v00")) and has_drive_object(v):
                evidence.append(k)
        if evidence:
            out.append({"row": str(seq), "version": str(r.get("version", "")),
                        "evidence": sorted(set(evidence))})
    return out


def build() -> dict[str, Any]:
    reg = load(REGISTRY)
    outside = rows_outside_the_tool(reg)
    reg_versions = {r.get("sequence"): str(r.get("version", "")) for r in reg["reels"]}

    rows: dict[str, Any] = {}
    masters: dict[str, list[dict[str, Any]]] = {}
    unresolved: list[dict[str, str]] = []
    variant_contracts: dict[str, list[dict[str, Any]]] = {}
    historic_ships: dict[str, list[dict[str, Any]]] = {}

    for seq in sorted(ROWS):
        spec = ROWS[seq]
        res = build_row(seq, spec, reg_versions.get(seq, ""))
        if res.provenance.get("variant_contracts"):
            variant_contracts[str(seq)] = res.provenance["variant_contracts"]
        if res.provenance.get("historic_ships"):
            historic_ships[str(seq)] = res.provenance["historic_ships"]
        rows[str(seq)] = {
            "version": res.version,
            "provenance": res.provenance,
            "slots": {s: res.slots[s] for s in sorted(res.slots)},
        }
        if res.provenance.get("status") == "UNRESOLVED":
            unresolved.append({"row": str(seq), "why": res.provenance.get("unresolved", "")})
        for slot in sorted(res.slots):
            for mid in res.slots[slot]:
                masters.setdefault(mid, []).append(
                    {
                        "row": seq,
                        "slot": slot,
                        "version": res.version,
                        "file": res.slot_file[slot],
                    }
                )

    for mid in masters:
        masters[mid].sort(key=lambda e: (e["row"], e["slot"]))

    return {
        "schema_version": 1,
        "tool": "tools/reuse_map.py",
        "rule": "lanes MUST cite tools/reuse_map.py output for any reuse claim",
        "what_this_is": (
            "Every master id that ships in a DELIVERED cut, and every delivered slot it ships in. "
            "Draft selections, runner-up lists, rejected alternates and prose mentions are excluded "
            "by construction."
        ),
        "rows_total": len(ROWS),
        "rows_outside_the_tool": outside,
        "rows_outside_the_tool_note": (
            "Rows with a DELIVERED cut that are NOT in this tool's ROWS universe. Any FIRST-SHIP "
            "or zero-collision claim is UNPROVEN against these rows."
        ),
        "historic_ships": {
            "what_this_is": (
                "SUPERSEDED but genuinely DELIVERED cuts of a row (see HistoricShip). This tool "
                "otherwise resolves each row to its NEWEST delivered cut only, so a master that "
                "shipped in an earlier delivered version was invisible and got reported as a "
                "FIRST SHIP."
            ),
            "rows_enumerated": sorted(historic_ships, key=int),
            "rows": historic_ships,
            "slots_added": sum(
                h.get("slots_added", 0) for row in historic_ships.values() for h in row
            ),
            "REMAINING_LIMIT": (
                "Only the rows listed in rows_enumerated have their superseded delivered cuts in "
                "this map. Every OTHER multi-version row is still resolved to its newest cut "
                "alone, so a FIRST-SHIP claim is UNPROVEN against their superseded ships. Do not "
                "publish a first-ship claim on this tool's silence."
            ),
        },
        "rows_resolved": sum(1 for r in rows.values() if r["provenance"]["status"] == "RESOLVED"),
        "rows_unresolved": unresolved,
        "variant_contracts": {
            "what_changed": (
                "This key replaces `variant_castings_excluded`, a prose string that walked "
                "nothing. Trial variants ARE delivered cuts, and a draft castings folder is not "
                "their delivered contract, so each variant's locked contract is walked. "
                "Variant slots are namespaced `varNN:pXXX` so they never overwrite a main cut."
            ),
            "rows": variant_contracts,
            "contracts_read": sum(
                len(b["contracts_read"]) for row in variant_contracts.values() for b in row
            ),
            "slots_added": sum(
                b["slots_added"] for row in variant_contracts.values() for b in row
            ),
        },
        "masters": dict(sorted(masters.items())),
        "rows": rows,
    }


def read_map() -> dict[str, Any]:
    if not os.path.exists(OUT):
        return build()
    with open(OUT, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------


def match_masters(data: dict[str, Any], needle: str) -> list[str]:
    """Masters matching a query: exact id, or the numeric tail (`0029`, `CLIP_0014`)."""
    n = needle.strip().upper()
    exact = [m for m in data["masters"] if m.upper() == n]
    if exact:
        return exact
    return sorted(
        m
        for m in data["masters"]
        if m.upper().endswith("_" + n) or m.upper() == n or m.upper().endswith(n) and n.startswith("C")
    )


def print_row(data: dict[str, Any], row: int) -> int:
    key = str(row)
    if key not in data["rows"]:
        print(f"row {row}: not in the map", file=sys.stderr)
        return 1
    r = data["rows"][key]
    prov = r["provenance"]
    print(f"ROW {row} — {r['version']} — {prov['status']}")
    print(f"  delivered plan : {prov.get('base_file')}")
    print(f"  why            : {prov['reason']}")
    for sup in prov.get("supersessions", []):
        print(f"  superseded by  : {sup['file']}")
        print(f"                   {sup['note']}")
        for slot, ch in sorted(sup["replaced"].items()):
            was = ",".join(ch["was"] or []) or "(none)"
            print(f"                   {slot}: {was} -> {','.join(ch['now'])}")
    if prov["status"] == "UNRESOLVED":
        print(f"  UNRESOLVED     : {prov['unresolved']}")
        return 0
    print()
    print(f"  {'slot':<8} {'master':<22} also delivered in")
    print(f"  {'-'*8} {'-'*22} {'-'*46}")
    for slot in sorted(r["slots"]):
        for mid in r["slots"][slot]:
            others = [
                f"row {e['row']} {e['slot']}"
                for e in data["masters"].get(mid, [])
                if not (e["row"] == row and e["slot"] == slot)
            ]
            print(f"  {slot:<8} {mid:<22} {', '.join(others) if others else '— nowhere (zero reuse)'}")
    reused = sum(
        1 for s in r["slots"] for m in r["slots"][s] if len(data["masters"].get(m, [])) > 1
    )
    print()
    print(f"  {len(r['slots'])} delivered slots · {reused} carry a master that ships elsewhere")
    if data["rows_unresolved"]:
        rows = ", ".join(u["row"] for u in data["rows_unresolved"])
        print(f"  BLIND SPOT: rows {rows} are UNRESOLVED — reuse against them is unproven")
    if data.get("rows_outside_the_tool"):
        rows = ", ".join(o["row"] for o in data["rows_outside_the_tool"])
        print(f"  BLIND SPOT: rows {rows} have delivered cuts and are NOT IN THE TOOL — any "
              "first-ship claim is unproven against them")
    return 0


def print_master(data: dict[str, Any], needle: str) -> int:
    hits = match_masters(data, needle)
    if not hits:
        print(f"{needle}: ZERO delivered slots in any resolved row")
        if data["rows_unresolved"]:
            rows = ", ".join(u["row"] for u in data["rows_unresolved"])
            print(f"  (rows {rows} are UNRESOLVED and were not searched)")
        if data.get("rows_outside_the_tool"):
            rows = ", ".join(o["row"] for o in data["rows_outside_the_tool"])
            print(f"  (rows {rows} have delivered cuts and are NOT IN THE TOOL'S UNIVERSE — "
                  "they were not searched either)")
        return 0
    for mid in hits:
        entries = data["masters"][mid]
        rows = sorted({e["row"] for e in entries})
        print(f"{mid} — {len(entries)} delivered slot(s) across rows {rows}")
        for e in entries:
            print(f"  row {e['row']:>2}  {e['slot']:<8} {e['version']:<26} {e['file']}")
    h = data.get("historic_ships") or {}
    if h:
        print(f"  superseded-but-delivered cuts enumerated for rows: "
              f"{', '.join(h.get('rows_enumerated', [])) or 'none'}")
        print("  every OTHER multi-version row is resolved to its NEWEST cut only — a first-ship "
              "claim is unproven against their superseded ships")
    return 0


def print_summary(data: dict[str, Any]) -> int:
    print(f"REUSE MAP — {data['rows_resolved']}/{data['rows_total']} rows resolved")
    print()
    ranked = sorted(data["masters"].items(), key=lambda kv: (-len(kv[1]), kv[0]))
    print("most-reused masters:")
    for mid, entries in ranked[:10]:
        rows = sorted({e["row"] for e in entries})
        print(f"  {mid:<22} {len(entries):>2} slots  rows {rows}")
    print()
    print(f"masters in delivered cuts: {len(data['masters'])}")
    print(f"delivered slots total    : {sum(len(v) for v in data['masters'].values())}")
    if data["rows_unresolved"]:
        print()
        print("UNRESOLVED rows — any reuse claim touching them is unproven:")
        for u in data["rows_unresolved"]:
            print(f"  row {u['row']}: {u['why']}")
    if data.get("rows_outside_the_tool"):
        print()
        print("ROWS OUTSIDE THIS TOOL'S UNIVERSE — they have delivered cuts the map cannot see;")
        print("any FIRST-SHIP or zero-collision claim is UNPROVEN against them:")
        for o in data["rows_outside_the_tool"]:
            print(f"  row {o['row']:>2}  {o['version']:<28} {', '.join(o['evidence'])}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild", action="store_true", help="rebuild REUSE_MAP.json from disk")
    ap.add_argument("--row", type=int, help="per-slot reuse table for one reel")
    ap.add_argument("--master", help="every delivered slot a master ships in")
    args = ap.parse_args(argv)

    if args.rebuild:
        data = build()
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"wrote {OUT}")
        print(f"  {data['rows_resolved']}/{data['rows_total']} rows resolved, "
              f"{len(data['masters'])} masters, "
              f"{sum(len(v) for v in data['masters'].values())} delivered slots")
        for u in data["rows_unresolved"]:
            print(f"  UNRESOLVED row {u['row']}")
        if data.get("rows_outside_the_tool"):
            print("  NOT IN THIS TOOL'S UNIVERSE (delivered cuts the map cannot see): rows "
                  + ", ".join(o["row"] for o in data["rows_outside_the_tool"]))
            print("  -> any FIRST-SHIP or zero-collision claim is UNPROVEN against them")
        return 0

    data = read_map()
    if args.row is not None:
        return print_row(data, args.row)
    if args.master:
        return print_master(data, args.master)
    return print_summary(data)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
