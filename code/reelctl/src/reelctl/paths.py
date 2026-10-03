from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence, Union


class PathSafetyError(RuntimeError):
    pass


PathPart = Union[str, os.PathLike[str]]


def _absolute(path: PathPart) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def canonical_root(root: PathPart, *, create: bool = False, mode: int = 0o700) -> Path:
    """Return a non-symlink directory root without resolving through symlinks."""
    value = _absolute(root)
    if value.is_symlink():
        raise PathSafetyError(f"trusted root is a symlink: {value}")
    if create:
        value.mkdir(parents=True, exist_ok=True, mode=mode)
    try:
        info = value.lstat()
    except FileNotFoundError as exc:
        raise PathSafetyError(f"trusted root does not exist: {value}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise PathSafetyError(f"trusted root is a symlink: {value}")
    if not stat.S_ISDIR(info.st_mode):
        raise PathSafetyError(f"trusted root is not a directory: {value}")
    return value


def _relative_parts(root: Path, candidate: PathPart) -> tuple[str, ...]:
    value = _absolute(candidate)
    try:
        relative = value.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"path escapes trusted root {root}: {value}") from exc
    parts = relative.parts
    if any(part in {"", ".", ".."} or "/" in part or "\x00" in part for part in parts):
        raise PathSafetyError(f"unsafe relative path beneath {root}: {relative}")
    return parts


def _join_candidate(root: Path, parts: Sequence[PathPart]) -> Path:
    if len(parts) == 1:
        only = Path(os.fspath(parts[0])).expanduser()
        candidate = only if only.is_absolute() else root / only
    else:
        candidate = root.joinpath(*(os.fspath(part) for part in parts))
    return _absolute(candidate)


def _walk_nofollow(root: Path, parts: Iterable[str], *, create: bool = False, mode: int = 0o700) -> Iterator[int]:
    """Yield directory fds while traversing beneath root with O_NOFOLLOW."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        yield descriptor
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=mode, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PathSafetyError(f"path component is a symlink or not a directory: {part}") from exc
                raise
            os.close(descriptor)
            descriptor = child
            yield descriptor
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def open_confined_parent(root: PathPart, candidate: PathPart, *, create: bool = False) -> tuple[int, str, Path]:
    """Open a candidate's parent via no-follow directory descriptors.

    The caller owns the returned fd.
    """
    trusted = canonical_root(root, create=create)
    parts = _relative_parts(trusted, candidate)
    if not parts:
        raise PathSafetyError("the trusted root itself cannot be used as a file")
    parent_parts = parts[:-1]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(trusted, flags)
    try:
        for part in parent_parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise PathSafetyError(f"parent directory does not exist beneath trusted root: {part}") from None
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PathSafetyError(f"path component is a symlink or not a directory: {part}") from exc
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1], trusted.joinpath(*parts)
    except Exception:
        os.close(descriptor)
        raise


def secure_mkdirs(root: PathPart, *parts: PathPart, mode: int = 0o700) -> Path:
    trusted = canonical_root(root, create=True, mode=mode)
    candidate = _join_candidate(trusted, parts)
    relative = _relative_parts(trusted, candidate)
    descriptor = os.open(trusted, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, mode=mode, dir_fd=descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PathSafetyError(f"path component is a symlink or not a directory: {part}") from exc
                raise
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    return candidate


def confined_path(
    root: PathPart,
    *parts: PathPart,
    require: Optional[str] = None,
    allow_missing: bool = True,
) -> Path:
    """Return a lexical path beneath root after rejecting every existing symlink."""
    trusted = canonical_root(root)
    candidate = _join_candidate(trusted, parts)
    relative = _relative_parts(trusted, candidate)
    current = trusted
    missing = False
    for index, part in enumerate(relative):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing = True
            if not allow_missing:
                raise PathSafetyError(f"required path does not exist: {current}") from None
            continue
        if stat.S_ISLNK(info.st_mode):
            raise PathSafetyError(f"symlink is forbidden in trusted path: {current}")
        if index < len(relative) - 1 and not stat.S_ISDIR(info.st_mode):
            raise PathSafetyError(f"non-directory path component beneath trusted root: {current}")
    if require is not None:
        if missing:
            raise PathSafetyError(f"required {require} does not exist: {candidate}")
        info = candidate.lstat()
        if require == "directory" and not stat.S_ISDIR(info.st_mode):
            raise PathSafetyError(f"required directory is not a directory: {candidate}")
        if require == "file" and not stat.S_ISREG(info.st_mode):
            raise PathSafetyError(f"required file is not a regular file: {candidate}")
    return candidate


def validate_project_tree(project_dir: PathPart, *, fixed_directories: Sequence[str]) -> Path:
    project = _absolute(project_dir)
    parent = canonical_root(project.parent)
    confined_path(parent, project.name, require="directory", allow_missing=False)
    for child in fixed_directories:
        confined_path(project, child, require="directory", allow_missing=False)
    for child in ("project.json", "state.json"):
        confined_path(project, child, require="file", allow_missing=False)
    return project
