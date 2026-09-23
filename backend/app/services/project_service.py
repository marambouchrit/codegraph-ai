"""Project ingestion: create a workspace, fetch the source, scan it, save metadata.

This is the only module that combines the ingestion building blocks. Routes call
it; it never imports FastAPI.
"""

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import (
    ContentTooLargeError,
    InvalidArchiveError,
    NoSourceFilesError,
    ProjectNotFoundError,
)
from app.ingestion.github import clone_repository, parse_github_url
from app.ingestion.scanner import ScannedFile, scan_directory
from app.ingestion.workspace import Workspace, directory_size, new_project_id, remove_tree
from app.ingestion.zip_handler import extract_zip_safely, find_project_root
from app.schemas.project import Project, SourceType

logger = logging.getLogger(__name__)

MB = 1024 * 1024


class ProjectService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workspace = Workspace(settings.workspace_dir)

    # ----- Creating projects -----

    def create_from_github(self, url: str) -> Project:
        repository = parse_github_url(url)
        max_bytes = self.settings.max_repository_size_mb * MB

        def fetch_source(source_dir: Path) -> None:
            clone_repository(
                repository.clone_url, source_dir, self.settings.git_clone_timeout_seconds
            )
            if directory_size(source_dir) > max_bytes:
                raise ContentTooLargeError(
                    f"The repository is larger than {self.settings.max_repository_size_mb} MB."
                )

        return self._ingest(repository.name, SourceType.GITHUB, repository.url, fetch_source)

    def create_from_zip(self, archive: BinaryIO, filename: str) -> Project:
        if not filename.lower().endswith(".zip"):
            raise InvalidArchiveError("Only .zip files are supported.")
        if _stream_size(archive) > self.settings.max_upload_size_mb * MB:
            raise ContentTooLargeError(
                f"The upload is larger than {self.settings.max_upload_size_mb} MB."
            )

        def fetch_source(source_dir: Path) -> None:
            extract_dir = source_dir.parent / "extract"
            extract_zip_safely(
                archive,
                extract_dir,
                max_total_bytes=self.settings.max_extracted_size_mb * MB,
                max_files=self.settings.max_archive_files,
            )
            find_project_root(extract_dir).rename(source_dir)
            if extract_dir.exists():
                remove_tree(extract_dir)

        name = Path(filename).stem
        return self._ingest(name, SourceType.ZIP, filename, fetch_source)

    def _ingest(
        self,
        name: str,
        source_type: SourceType,
        source: str,
        fetch_source: Callable[[Path], None],
    ) -> Project:
        """Shared ingestion steps. On any failure the project folder is removed."""
        project_id = new_project_id()
        self.workspace.create_project_dir(project_id)
        try:
            source_dir = self.workspace.source_dir(project_id)
            fetch_source(source_dir)

            scan = scan_directory(source_dir, self._max_source_file_bytes)
            if not scan.files:
                raise NoSourceFilesError(
                    "No Python, Java, JavaScript or TypeScript source files were found."
                )

            project = Project(
                id=project_id,
                name=name,
                source_type=source_type,
                source=source,
                created_at=datetime.now(UTC),
                file_count=len(scan.files),
                total_size_bytes=scan.total_size_bytes,
                languages=scan.language_counts,
            )
            # Metadata is written last: a project without project.json is incomplete.
            self.workspace.metadata_file(project_id).write_text(
                project.model_dump_json(indent=2), encoding="utf-8"
            )
        except Exception:
            self._cleanup(project_id)
            raise

        logger.info("Ingested project %s (%s, %d files)", project_id, name, project.file_count)
        return project

    # ----- Reading and deleting projects -----

    def list_projects(self) -> list[Project]:
        projects = [self._load(project_id) for project_id in self.workspace.project_ids()]
        complete = [project for project in projects if project is not None]
        return sorted(complete, key=lambda project: project.created_at, reverse=True)

    def get_project(self, project_id: str) -> Project:
        project = self._load(project_id)
        if project is None:
            raise ProjectNotFoundError(f"Project '{project_id}' not found.")
        return project

    def list_files(self, project_id: str) -> list[ScannedFile]:
        self.get_project(project_id)
        source_dir = self.workspace.source_dir(project_id)
        return scan_directory(source_dir, self._max_source_file_bytes).files

    def delete_project(self, project_id: str) -> None:
        self.get_project(project_id)
        self.workspace.delete_project_dir(project_id)
        logger.info("Deleted project %s", project_id)

    # ----- Helpers -----

    @property
    def _max_source_file_bytes(self) -> int:
        return self.settings.max_source_file_kb * 1024

    def _load(self, project_id: str) -> Project | None:
        metadata_file = self.workspace.metadata_file(project_id)
        if not metadata_file.is_file():
            return None
        try:
            return Project.model_validate_json(metadata_file.read_text(encoding="utf-8"))
        except ValidationError:
            logger.warning("Ignoring project %s: invalid project.json", project_id)
            return None

    def _cleanup(self, project_id: str) -> None:
        try:
            self.workspace.delete_project_dir(project_id)
        except OSError:
            # Do not hide the original ingestion error behind a cleanup error.
            logger.exception("Could not clean up the workspace of project %s", project_id)


def _stream_size(stream: BinaryIO) -> int:
    size = stream.seek(0, os.SEEK_END)
    stream.seek(0)
    return size
