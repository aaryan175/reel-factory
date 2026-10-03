#!/usr/bin/env python3
"""Query the footage library: library_find.py --role traversal --world night-interior --min-energy medium
Prints matching clips ranked by confidence then energy. Standalone (reelctl wiring comes after the
caption engine settles, to avoid concurrent cli.py edits)."""
import json, argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rf_paths  # noqa: E402

LIB = rf_paths.FOOTAGE_LIBRARY_JSON
ENERGY = {"low": 0, "medium": 1, "high": 2}

ap = argparse.ArgumentParser()
ap.add_argument("--role")
ap.add_argument("--world")
ap.add_argument("--action", help="substring match on action_verbs")
ap.add_argument("--min-energy", choices=ENERGY, default="low")
ap.add_argument("--subject")
ap.add_argument("--lighting")
ap.add_argument("--crop-safe", action="store_true", help="only crop_safety_916 == safe")
ap.add_argument("--min-duration", type=float, default=0)
ap.add_argument("--limit", type=int, default=20)
ap.add_argument("--json", action="store_true")
a = ap.parse_args()

if not LIB.exists():
    sys.exit("FOOTAGE_LIBRARY.json not built yet — run build_library.py after indexing completes.")
lib = json.load(open(LIB))

out = []
for c in lib["clips"].values():
    t = c.get("tags") or {}
    if not t: continue
    if a.role and a.role not in (t.get("roles") or []): continue
    if a.world and t.get("world_cluster") != a.world: continue
    if a.action and not any(a.action in v for v in (t.get("action_verbs") or [])): continue
    if ENERGY.get(t.get("energy", "low"), 0) < ENERGY[a.min_energy]: continue
    if a.subject and t.get("subject") != a.subject: continue
    if a.lighting and t.get("lighting") != a.lighting: continue
    if a.crop_safe and t.get("crop_safety_916") != "safe": continue
    if (c.get("duration") or 0) < a.min_duration: continue
    out.append(c)

conf_rank = {"high": 2, "medium": 1, "low": 0}
out.sort(key=lambda c: (conf_rank.get((c["tags"] or {}).get("confidence", "low"), 0),
                        ENERGY.get((c["tags"] or {}).get("energy", "low"), 0)), reverse=True)
out = out[:a.limit]

if a.json:
    print(json.dumps(out, indent=1))
else:
    for c in out:
        t = c["tags"]
        print(f"{c['id']}  {c['path']}  [{t['world_cluster']}] roles={','.join(t.get('roles', []))} "
              f"energy={t.get('energy')} conf={t.get('confidence')} dur={c.get('duration')}s")
    print(f"-- {len(out)} matches")
