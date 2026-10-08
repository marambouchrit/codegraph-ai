"""Incremental analysis: which files changed since the last analysis (SHA-256 of the content)."""

import hashlib
from pathlib import Path

from app.analysis.changes import FileChanges, detect_changes, file_sha256


def test_the_hash_of_a_file_is_the_sha256_of_its_content(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_bytes(b"def a():\n    return 1\n")

    assert file_sha256(path) == hashlib.sha256(b"def a():\n    return 1\n").hexdigest()


def test_the_hash_depends_on_the_content_not_on_the_name_or_the_date(tmp_path: Path) -> None:
    first, copy, edited = tmp_path / "a.py", tmp_path / "b.py", tmp_path / "c.py"
    first.write_bytes(b"x = 1\n")
    copy.write_bytes(b"x = 1\n")
    edited.write_bytes(b"x = 2\n")

    assert file_sha256(first) == file_sha256(copy) != file_sha256(edited)


def test_every_file_is_added_modified_unchanged_or_deleted() -> None:
    previous = {"same.py": "hash1", "edited.py": "hash2", "removed.py": "hash3"}
    current = {"same.py": "hash1", "edited.py": "other", "new.py": "hash4"}

    changes = detect_changes(previous, current)

    assert changes == FileChanges(
        added=("new.py",), modified=("edited.py",), unchanged=("same.py",), deleted=("removed.py",)
    )
    # Only the added and modified files are parsed and embedded again.
    assert changes.to_analyze == {"new.py", "edited.py"}


def test_nothing_changed_means_nothing_to_analyze() -> None:
    hashes = {"a.py": "hash1", "b.py": "hash2"}

    changes = detect_changes(hashes, dict(hashes))

    assert not changes.any and changes.to_analyze == frozenset()
