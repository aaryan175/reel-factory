from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .paths import PathSafetyError, confined_path, open_confined_parent


def sha256_file(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    path = Path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if path.is_symlink():
            raise PathSafetyError(f"refusing to hash symlink: {path}") from exc
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PathSafetyError(f"refusing to hash non-regular file: {path}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for block in iter(lambda: handle.read(block_size), b""):
                digest.update(block)
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


#: How many digests one process remembers before it forgets all of them. The bound exists
#: because the studio daemon runs for weeks over a project set that only grows; overflow
#: clears the memo outright rather than evicting by some guess at which entry matters least,
#: because the cost of forgetting is one re-read and the cost of a wrong eviction policy is a
#: hard-to-see slowdown. At ~3,000 artifacts per reel this holds about sixteen reels.
SHA256_MEMO_MAX_ENTRIES = 50_000

#: absolute path -> (size, mtime_ns, inode, digest) of the file that digest was read from.
_SHA256_MEMO: Dict[str, Any] = {}


def clear_sha256_memo() -> None:
    _SHA256_MEMO.clear()


def sha256_file_memoized(path: Path) -> str:
    """``sha256_file``, skipping the read while the file is provably the one already read.

    For the caller that verifies immutable artifacts — ``ProjectState.verify_stage``, reached
    from ``next_stage`` on every studio daemon tick — the same bytes are hashed over and
    over: ``verify_stage`` recurses across the predecessor chain, so a project with eleven
    passed stages performs sixty-six verifications, and on a large project one planning pass
    cost ~100k digests over several GB, every twenty seconds, forever.

    The memo is keyed on ``(absolute path, size, mtime_ns, inode)`` and answers only on an
    exact match of all four. Size and mtime catch an edit; the inode catches a different file
    landing on the path with copied timestamps, which ``os.replace`` can do. Anything else —
    a miss, a changed stat, an entry that will not unpack, a path that no longer answers
    ``stat`` — falls through to a real read, so every failure mode of the cache costs time
    rather than truth. That asymmetry is the whole design: this memo sits underneath an
    integrity check, and an integrity check that answers from memory is not one.

    It is deliberately in-memory and per-process. The repetition being paid for happens
    inside one long-lived daemon, so that is where the saving is; a memo persisted to disk
    would additionally have to be trusted by processes that did not do the hashing, which
    would make a poisoned cache file a way to pass verification.
    """
    target = Path(path)
    key = str(target.absolute())
    try:
        info = os.stat(key, follow_symlinks=False)
    except OSError:
        # Deleted, or otherwise no longer answering: forget it and let the reader raise the
        # real error, which is the caller's evidence.
        _SHA256_MEMO.pop(key, None)
        return sha256_file(target)
    if not stat.S_ISREG(info.st_mode):
        # A symlink or a device. ``sha256_file`` refuses these by design and the memo must
        # never become the way around that refusal.
        _SHA256_MEMO.pop(key, None)
        return sha256_file(target)
    try:
        size, mtime_ns, inode, digest = _SHA256_MEMO[key]
        if size == info.st_size and mtime_ns == info.st_mtime_ns and inode == info.st_ino:
            return str(digest)
    except (KeyError, TypeError, ValueError):
        pass  # an entry this code cannot read is a miss, never an answer
    digest = sha256_file(target)
    if len(_SHA256_MEMO) >= SHA256_MEMO_MAX_ENTRIES:
        _SHA256_MEMO.clear()
    _SHA256_MEMO[key] = (info.st_size, info.st_mtime_ns, info.st_ino, digest)
    return digest


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def recipe_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def atomic_write_bytes(path: Path, payload: bytes, *, root: Optional[Path] = None, mode: int = 0o600) -> None:
    """Atomically write bytes.

    With ``root``, every parent is opened with O_NOFOLLOW and the destination is
    replaced by dirfd, preventing symlink redirection outside the trusted tree.
    """
    path = Path(path)
    if root is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise PathSafetyError(f"refusing to replace symlink destination: {path}")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return

    parent_fd, leaf, confined = open_confined_parent(root, path, create=True)
    temporary = f".{leaf}.{secrets.token_hex(12)}.tmp"
    descriptor: Optional[int] = None
    try:
        try:
            info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISLNK(info.st_mode):
                raise PathSafetyError(f"refusing to replace symlink destination: {confined}")
            if not stat.S_ISREG(info.st_mode):
                raise PathSafetyError(f"destination is not a regular file: {confined}")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def atomic_write_json(path: Path, value: Any, *, root: Optional[Path] = None) -> None:
    atomic_write_bytes(path, _json_bytes(value), root=root, mode=0o600)


@contextmanager
def atomic_external_output(
    path: Path,
    *,
    root: Path,
    mode: int = 0o600,
    require_nonempty: bool = True,
) -> Iterator[Path]:
    """Give an external encoder a private temporary path, then atomically promote it.

    The final destination and temporary leaf are inspected without following
    symlinks. External tools must write only to the yielded path.
    """

    parent_fd, leaf, confined = open_confined_parent(root, path, create=True)
    suffix = Path(leaf).suffix
    stem = Path(leaf).name[: -len(suffix)] if suffix else Path(leaf).name
    temporary = f".{stem}.{secrets.token_hex(12)}.tmp{suffix}"
    descriptor: Optional[int] = None
    try:
        try:
            destination_info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            destination_info = None
        if destination_info is not None:
            if stat.S_ISLNK(destination_info.st_mode):
                raise PathSafetyError(f"refusing to replace symlink destination: {confined}")
            if not stat.S_ISREG(destination_info.st_mode):
                raise PathSafetyError(f"destination is not a regular file: {confined}")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        os.close(descriptor)
        descriptor = None
        temporary_path = confined.parent / temporary
        yield temporary_path

        info = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise PathSafetyError(f"external output temporary is not a regular file: {temporary_path}")
        if require_nonempty and info.st_size < 1:
            raise PathSafetyError(f"external output is empty: {temporary_path}")
        descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        try:
            destination_info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            destination_info = None
        if destination_info is not None and stat.S_ISLNK(destination_info.st_mode):
            raise PathSafetyError(f"refusing to replace symlink destination: {confined}")
        if destination_info is not None and not stat.S_ISREG(destination_info.st_mode):
            raise PathSafetyError(f"destination is not a regular file: {confined}")
        os.replace(temporary, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def load_json(path: Path, *, root: Optional[Path] = None) -> Any:
    path = Path(path)
    if root is None:
        if path.is_symlink():
            raise PathSafetyError(f"refusing JSON symlink: {path}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    else:
        confined_path(root, path, require="file", allow_missing=False)
        parent_fd, leaf, _ = open_confined_parent(root, path)
        try:
            descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
