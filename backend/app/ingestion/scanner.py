"""Recursively find the supported source files of a repository.

The scanner only reads the first few kilobytes of candidate files (to detect
binary content). It never executes anything.
"""

import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.ingestion.languages import Language, detect_language

IGNORED_DIRECTORIES = frozenset(
    {
        # Version control
        ".git", ".hg", ".svn",
        # Python environments and caches
        "venv", ".venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", ".ruff_cache",
        # JavaScript / TypeScript
        "node_modules", ".next", ".nuxt", "bower_components", "coverage",
        # Build output
        "dist", "build", "out", "target", ".gradle",
        # Editors and OS
        ".idea", ".vscode", "__MACOSX",
    }
)  # fmt: skip

MINIFIED_SUFFIXES = (".min.js", ".min.mjs", ".min.cjs")
BINARY_CHECK_BYTES = 8192


@dataclass(frozen=True)
class ScannedFile:
    path: str  # relative to the repository root, always with "/" separators
    language: Language
    size_bytes: int


@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    skipped_files: int = 0

    @property
    def total_size_bytes(self) -> int:
        return sum(file.size_bytes for file in self.files)

    @property
    def language_counts(self) -> dict[str, int]:
        counts = Counter(file.language.value for file in self.files)
        return dict(sorted(counts.items()))


def scan_directory(root: Path, max_file_bytes: int) -> ScanResult:
    result = ScanResult()
    for current_dir, dir_names, file_names in os.walk(root, followlinks=False):
        # Editing dir_names in place tells os.walk not to descend into ignored folders.
        dir_names[:] = sorted(name for name in dir_names if name not in IGNORED_DIRECTORIES)

        for file_name in sorted(file_names):
            scanned = _inspect_file(root, Path(current_dir) / file_name, max_file_bytes)
            if scanned is None:
                result.skipped_files += 1
            else:
                result.files.append(scanned)
    return result


def is_binary_file(path: Path) -> bool:
    """Text source files never contain NUL bytes, so one NUL byte means binary."""
    with path.open("rb") as file:
        return b"\x00" in file.read(BINARY_CHECK_BYTES)


def _inspect_file(root: Path, file_path: Path, max_file_bytes: int) -> ScannedFile | None:
    """Return a ScannedFile if this is a supported source file, otherwise None."""
    language = detect_language(file_path)
    if language is None or file_path.name.lower().endswith(MINIFIED_SUFFIXES):
        return None

    try:
        if file_path.is_symlink():
            return None
        size = file_path.stat().st_size
        # Very large files are usually generated code, not something worth analyzing.
        if size > max_file_bytes or is_binary_file(file_path):
            return None
    except OSError:
        return None  # unreadable file (permissions, broken path...)

    relative_path = file_path.relative_to(root).as_posix()
    return ScannedFile(path=relative_path, language=language, size_bytes=size)
