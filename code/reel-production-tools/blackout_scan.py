"""Scan a deliverable for personal identifiers before it goes outward.

Terms come from REEL_FACTORY_REDACT_TERMS_FILE (one term per line; blank lines and # comments
ignored) — the same list the Review Deck redacts from bus receipts. Keep that file out of the repo.

Usage: blackout_scan.py <file> [<file> ...]   ->  prints per-file hits, exits 1 if any.
"""

import os
import sys
from pathlib import Path


def load_terms() -> list:
    path = os.environ.get("REEL_FACTORY_REDACT_TERMS_FILE")
    if not path:
        return []
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln.strip().lower() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


TERMS = load_terms()

def main(paths):
    if not TERMS:
        print("no terms: set REEL_FACTORY_REDACT_TERMS_FILE to a file with one term per line")
    bad = 0
    for raw in paths:
        path = Path(raw)
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        hits = []
        for term in TERMS:
            # word-ish match so "[abc]" does not fire inside "cuts"/"shortcuts"
            needle = term if len(term) > 3 else f" {term} "
            haystack = text if len(term) > 3 else f" {text} "
            count = haystack.count(needle)
            if count:
                hits.append((term, count))
        bad += bool(hits)
        print(f"{path.name}: {'HITS ' + str(hits) if hits else 'clean'}")
    print(f"files with hits: {bad} of {len(paths)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
