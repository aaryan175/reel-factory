"""The stat-keyed digest memo behind ``ProjectState.verify_stage``.

Every test here is about the one way a cache can be wrong: answering for bytes it has not
seen. The memo is allowed to skip a read only while the file it read is still the same file
at the same size and the same modification time; every other outcome — changed, replaced,
deleted, unreadable, an entry it cannot make sense of — must fall through to a real read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List

import pytest

from reelctl import hashing
from reelctl.hashing import clear_sha256_memo, sha256_file, sha256_file_memoized
from reelctl.paths import PathSafetyError


@pytest.fixture(autouse=True)
def _clean_memo() -> Any:
    clear_sha256_memo()
    yield
    clear_sha256_memo()


@pytest.fixture()
def reads(monkeypatch: pytest.MonkeyPatch) -> List[Path]:
    seen: List[Path] = []
    real = hashing.sha256_file

    def counting(path: Path, *args: Any, **kwargs: Any) -> str:
        seen.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(hashing, "sha256_file", counting)
    return seen


def test_the_second_call_on_an_unchanged_file_reads_nothing(tmp_path: Path, reads: List[Path]) -> None:
    artifact = tmp_path / "candidate.json"
    artifact.write_text("one\n", encoding="utf-8")

    first = sha256_file_memoized(artifact)
    assert reads == [artifact]
    assert sha256_file_memoized(artifact) == first
    assert reads == [artifact]


def test_a_file_whose_bytes_changed_is_hashed_again(tmp_path: Path, reads: List[Path]) -> None:
    artifact = tmp_path / "candidate.json"
    artifact.write_text("one\n", encoding="utf-8")
    first = sha256_file_memoized(artifact)

    artifact.write_text("two\n", encoding="utf-8")
    os.utime(artifact, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

    assert sha256_file_memoized(artifact) == sha256_file(artifact) != first
    assert len(reads) == 2


def test_a_file_rewritten_to_the_same_size_at_a_new_mtime_is_hashed_again(tmp_path: Path) -> None:
    """Same length, different bytes — the size alone would have said "unchanged"."""
    artifact = tmp_path / "candidate.json"
    artifact.write_text("aaa\n", encoding="utf-8")
    first = sha256_file_memoized(artifact)

    artifact.write_text("bbb\n", encoding="utf-8")
    os.utime(artifact, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

    assert sha256_file_memoized(artifact) != first


def test_a_file_replaced_by_a_different_inode_at_the_same_size_and_mtime_is_hashed_again(tmp_path: Path) -> None:
    """``os.replace`` can land a different file on the path carrying copied timestamps."""
    artifact = tmp_path / "candidate.json"
    artifact.write_text("aaa\n", encoding="utf-8")
    stamp = os.stat(artifact).st_mtime_ns
    first = sha256_file_memoized(artifact)

    replacement = tmp_path / "replacement.json"
    replacement.write_text("bbb\n", encoding="utf-8")
    os.utime(replacement, ns=(stamp, stamp))
    os.replace(replacement, artifact)

    assert sha256_file_memoized(artifact) != first


def test_a_deleted_file_drops_its_entry_and_raises(tmp_path: Path) -> None:
    artifact = tmp_path / "candidate.json"
    artifact.write_text("one\n", encoding="utf-8")
    sha256_file_memoized(artifact)

    artifact.unlink()
    with pytest.raises(OSError):
        sha256_file_memoized(artifact)

    artifact.write_text("three\n", encoding="utf-8")
    assert sha256_file_memoized(artifact) == sha256_file(artifact)


def test_an_entry_the_memo_cannot_read_is_a_miss_not_an_answer(tmp_path: Path, reads: List[Path]) -> None:
    """Fail-safe: a corrupt entry re-derives. It never becomes a verdict about the bytes."""
    artifact = tmp_path / "candidate.json"
    artifact.write_text("one\n", encoding="utf-8")
    expected = sha256_file(artifact)
    sha256_file_memoized(artifact)
    reads.clear()

    hashing._SHA256_MEMO[str(artifact.absolute())] = ("not", "a", "valid", "entry")

    assert sha256_file_memoized(artifact) == expected
    assert reads == [artifact]


def test_a_symlink_is_refused_by_the_memo_exactly_as_it_is_by_the_reader(tmp_path: Path) -> None:
    """The memo must not become a way around ``sha256_file``'s ``O_NOFOLLOW`` refusal."""
    artifact = tmp_path / "candidate.json"
    artifact.write_text("one\n", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(artifact)

    with pytest.raises(PathSafetyError):
        sha256_file_memoized(link)


def test_the_memo_stays_bounded_by_forgetting_rather_than_by_guessing(tmp_path: Path) -> None:
    """A daemon runs for weeks. Overflow clears the memo; it never evicts into a wrong answer."""
    for index in range(hashing.SHA256_MEMO_MAX_ENTRIES + 1):
        artifact = tmp_path / f"artifact-{index}.json"
        artifact.write_text(f"{index}\n", encoding="utf-8")
        sha256_file_memoized(artifact)

    assert len(hashing._SHA256_MEMO) <= hashing.SHA256_MEMO_MAX_ENTRIES
    last = tmp_path / f"artifact-{hashing.SHA256_MEMO_MAX_ENTRIES}.json"
    assert sha256_file_memoized(last) == sha256_file(last)
