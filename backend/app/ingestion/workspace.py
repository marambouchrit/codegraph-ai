"""On-disk workspace where imported projects are stored.

Layout:
    <workspace_dir>/
        <project_id>/
            project.json   <- project metadata, written only when ingestion succeeds
            analysis.json  <- analysis state: current job, progress, last successful report
            analysis_index.json  <- file hashes and per-file results, for incremental analysis
            source/        <- the repository files
"""

import os
import re
import shutil
import stat
import sys
import uuid
from pathlib import Path

from app.core.errors import ProjectNotFoundError

_PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def new_project_id() -> str:
    return uuid.uuid4().hex


def is_valid_project_id(project_id: object) -> bool:
    return isinstance(project_id, str) and _PROJECT_ID_PATTERN.fullmatch(project_id) is not None


class Workspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir.resolve()

    def project_dir(self, project_id: str) -> Path:
        # Project IDs come from URLs, so validate them before building a path.
        # This makes values such as "../../etc" impossible.
        if not is_valid_project_id(project_id):
            raise ProjectNotFoundError(f"Project '{project_id}' not found.")
        return self.base_dir / project_id

    def source_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "source"

    def metadata_file(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def analysis_file(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "analysis.json"

    def analysis_index_file(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "analysis_index.json"

    def create_project_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id)
        path.mkdir(parents=True, exist_ok=False)
        return path

    def project_ids(self) -> list[str]:
        if not self.base_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.base_dir.iterdir()
            if entry.is_dir() and _PROJECT_ID_PATTERN.fullmatch(entry.name)
        )

    def delete_project_dir(self, project_id: str) -> None:
        path = self.project_dir(project_id)
        if path.exists():
            remove_tree(path)


def remove_tree(path: Path) -> None:
    """Delete a directory tree, including read-only files (Git creates some on Windows)."""

    def make_writable_and_retry(func, failed_path, _error) -> None:  # type: ignore[no-untyped-def]
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)


def directory_size(path: Path) -> int:
    """Total size in bytes of all regular files under `path` (symlinks are not followed)."""
    total = 0
    for current_dir, _dir_names, file_names in os.walk(path):
        for file_name in file_names:
            file_path = os.path.join(current_dir, file_name)
            if not os.path.islink(file_path):
                total += os.path.getsize(file_path)
    return total
