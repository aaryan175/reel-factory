#!/usr/bin/env python3
"""onetoone.registry_row — update ONE reel row in REEL_REGISTRY.json safely.

Law: registry writes take a flock and leave a timestamped .bak.
    python3 -m onetoone.registry_row <registry.json> <sequence> <patch.json> [--tag row7v009]
patch.json = {"set": {...top-level fields...}, "add": {"<key>": {...}}}
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import shutil
from pathlib import Path


def update(registry: Path, sequence: int, patch: dict, tag: str) -> dict:
    lock = registry.with_suffix(".lock")
    with open(lock, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            bak = registry.with_name(f"{registry.stem}.backup-{ts}-{tag}.json")
            shutil.copy2(registry, bak)
            data = json.loads(registry.read_text())
            rows = [r for r in data["reels"] if r.get("sequence") == sequence]
            if len(rows) != 1:
                raise SystemExit(f"row {sequence}: found {len(rows)}")
            row = rows[0]
            for k, v in (patch.get("set") or {}).items():
                row[k] = v
            for k, v in (patch.get("add") or {}).items():
                if k in row:
                    raise SystemExit(f"row {sequence}: key {k} exists (append-only registry)")
                row[k] = v
            row["updated_at_utc"] = ts[:4] + "-" + ts[4:6] + "-" + ts[6:8] + "T" + ts[9:11] + ":" + ts[11:13] + ":" + ts[13:15] + "Z"
            data["updated_at_utc"] = row["updated_at_utc"]
            tmp = registry.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            os.replace(tmp, registry)
            return {"backup": str(bak), "row": sequence, "review_state": row.get("review_state"), "version": row.get("version")}
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("registry"); ap.add_argument("sequence", type=int); ap.add_argument("patch"); ap.add_argument("--tag", default="row")
    a = ap.parse_args()
    print(json.dumps(update(Path(a.registry), a.sequence, json.loads(Path(a.patch).read_text()), a.tag), indent=1))
