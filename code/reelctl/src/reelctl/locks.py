from __future__ import annotations

import errno
import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .identifiers import validate_identifier
from .paths import PathSafetyError, open_confined_parent, secure_mkdirs


class LockError(RuntimeError):
    pass


class ProjectBusyError(LockError):
    pass


@contextmanager
def project_lock(projects_root: Path, project_id: str) -> Iterator[Path]:
    safe_id = validate_identifier(project_id, kind="project")
    try:
        lock_dir = secure_mkdirs(projects_root, ".reelctl-locks", mode=0o700)
        path = lock_dir / f"{safe_id}.lock"
        parent_fd, leaf, confined = open_confined_parent(projects_root, path)
    except PathSafetyError as exc:
        raise LockError(str(exc)) from exc
    descriptor = None
    try:
        try:
            try:
                info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)):
                raise LockError(f"lock destination is a symlink or not a regular file: {confined}")
            descriptor = os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise LockError(f"lock path contains a symlink: {confined}") from exc
            raise
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProjectBusyError(f"project {safe_id} is locked by another reelctl process: {confined}") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        yield confined
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        os.close(parent_fd)
