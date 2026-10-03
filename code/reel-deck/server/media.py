"""
Media serving — confined, ranged, never hanging.

Only files under the factory's media roots (workbench, reel-production,
deck uploads) with a whitelisted extension are served. Every request is
realpath-confined (no traversal, no symlink escape) and TCC-guarded via
registry.guard_path so a blocked volume returns a reason, not a hang.
Range requests are honoured — scrubbing video depends on it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from . import registry as reg
from .receipts import UPLOADS_DIR

ALLOWED_ROOTS = [
    str(reg.WORKBENCH_ROOT),
    str(reg.REEL_ROOT),
    str(Path(os.path.realpath(UPLOADS_DIR))),
]
ALLOWED_EXTS = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".png": "image/png",
                ".md": "text/plain; charset=utf-8"}

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK = 1024 * 512


_WB = str(reg.WORKBENCH_ROOT) + os.sep
_RR = str(reg.REEL_ROOT) + os.sep


def _reel_scoped(real: str) -> bool:
    """Only REEL files leave this server: reelNN-* project dirs and the how-to folder on the
    workbench; reference-intake and the bus under the reel root; uploads. The workbench also
    holds unrelated data (backups, other projects) — never those."""
    if real.startswith(_WB):
        head = real[len(_WB):].split(os.sep, 1)[0]
        return bool(re.match(r"^(reel\d{2}-|reel-deck-howto$|footage-library)", head))
    if real.startswith(_RR):
        head = real[len(_RR):].split(os.sep, 1)[0]
        return head in ("reference-intake", "_receipts")
    return True   # uploads root


def confine(raw_path: str) -> str:
    real = os.path.realpath(raw_path)
    ext = os.path.splitext(real)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=403, detail="file type not served")
    if not any(real == root or real.startswith(root + os.sep) for root in ALLOWED_ROOTS):
        raise HTTPException(status_code=403, detail="path outside the media roots")
    if not _reel_scoped(real):
        raise HTTPException(status_code=403, detail="not a reel file")
    reason = reg.guard_path(real)
    if reason:
        raise HTTPException(status_code=503, detail=reason)
    if not os.path.isfile(real):
        raise HTTPException(status_code=404, detail="file not on disk")
    return real


def _stream(path: str, start: int, end: int):
    remaining = end - start + 1
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            data = handle.read(min(CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def serve(request: Request, raw_path: str) -> Response:
    real = confine(raw_path)
    size = os.path.getsize(real)
    mime = ALLOWED_EXTS[os.path.splitext(real)[1].lower()]
    common = {"accept-ranges": "bytes", "cache-control": "private, max-age=60"}

    header = request.headers.get("range")
    if header:
        match = _RANGE_RE.match(header.strip())
        if not match:
            raise HTTPException(status_code=416, detail="bad range")
        start_s, end_s = match.groups()
        if start_s == "" and end_s == "":
            raise HTTPException(status_code=416, detail="bad range")
        if start_s == "":
            length = min(int(end_s), size)
            start, end = size - length, size - 1
        else:
            start = int(start_s)
            end = min(int(end_s), size - 1) if end_s else size - 1
        if start > end or start >= size:
            return Response(status_code=416, headers={"content-range": f"bytes */{size}", **common})
        return StreamingResponse(
            _stream(real, start, end), status_code=206, media_type=mime,
            headers={"content-range": f"bytes {start}-{end}/{size}",
                     "content-length": str(end - start + 1), **common},
        )

    return StreamingResponse(
        _stream(real, 0, size - 1), media_type=mime,
        headers={"content-length": str(size), **common},
    )
