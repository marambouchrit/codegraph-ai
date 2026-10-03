"""Detect which source files changed since the last analysis, by content.

Each file gets the SHA-256 of its bytes. Comparing today's hashes with the ones saved
by the last successful analysis tells, for every file, whether it was added, modified,
left unchanged or deleted. Modification times are never used: a file rewritten with
the same content is unchanged, and a file whose date was preserved but whose content
changed is modified.
"""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.ingestion.scanner import ScannedFile

_BLOCK = 1024 * 1024


def file_sha256(path: Path) -> str:
    """SHA-256 of a file's content, read in blocks (large files never fill the memory)."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def hash_files(root: Path, files: Iterable[ScannedFile]) -> dict[str, str]:
    """{relative path: SHA-256} of the scanned source files, in scan order."""
    return {file.path: file_sha256(root / file.path) for file in files}


@dataclass(frozen=True)
class FileChanges:
    """Relative paths, each list sorted: the same project state gives the same changes."""

    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def to_analyze(self) -> frozenset[str]:
        """Files whose content must be parsed again."""
        return frozenset(self.added) | frozenset(self.modified)

    @property
    def any(self) -> bool:
        return bool(self.added or self.modified or self.deleted)


def detect_changes(previous: Mapping[str, str], current: Mapping[str, str]) -> FileChanges:
    """Compare the hashes of the last analysis (`previous`) with today's (`current`)."""
    return FileChanges(
        added=tuple(sorted(path for path in current if path not in previous)),
        modified=tuple(
            sorted(path for path in current if path in previous and previous[path] != current[path])
        ),
        unchanged=tuple(
            sorted(path for path in current if path in previous and previous[path] == current[path])
        ),
        deleted=tuple(sorted(path for path in previous if path not in current)),
    )
