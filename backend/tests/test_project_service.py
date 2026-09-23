import io
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import (
    ContentTooLargeError,
    InvalidArchiveError,
    InvalidGitHubUrlError,
    NoSourceFilesError,
    ProjectNotFoundError,
    RepositoryCloneError,
    UnsafeArchiveError,
)
from app.schemas.project import SourceType
from app.services import project_service
from app.services.project_service import ProjectService
from tests.conftest import MakeZip


def workspace_entries(settings: Settings) -> list[str]:
    if not settings.workspace_dir.exists():
        return []
    return [entry.name for entry in settings.workspace_dir.iterdir()]


# ----- ZIP ingestion -----


def test_create_from_zip(settings: Settings, make_zip: MakeZip) -> None:
    service = ProjectService(settings)
    data = make_zip({"demo-main/app.py": "x = 1", "demo-main/web/index.ts": "let a = 1"})

    project = service.create_from_zip(io.BytesIO(data), "demo.zip")

    assert project.name == "demo"
    assert project.source_type == SourceType.ZIP
    assert project.file_count == 2
    assert project.languages == {"python": 1, "typescript": 1}
    # The single "demo-main/" folder was unwrapped.
    source_dir = settings.workspace_dir / project.id / "source"
    assert (source_dir / "app.py").exists()
    assert (settings.workspace_dir / project.id / "project.json").exists()
    assert not (settings.workspace_dir / project.id / "extract").exists()


def test_created_projects_can_be_listed_and_read_back(settings: Settings, make_zip: MakeZip) -> None:
    service = ProjectService(settings)
    first = service.create_from_zip(io.BytesIO(make_zip({"a.py": "x"})), "first.zip")
    second = service.create_from_zip(io.BytesIO(make_zip({"b.py": "x"})), "second.zip")

    # A new service instance reads the same data from disk.
    reloaded = ProjectService(settings)

    assert [p.id for p in reloaded.list_projects()] == [second.id, first.id]
    assert reloaded.get_project(first.id) == first
    assert [f.path for f in reloaded.list_files(first.id)] == ["a.py"]


def test_rejects_non_zip_filename(settings: Settings) -> None:
    with pytest.raises(InvalidArchiveError, match=".zip"):
        ProjectService(settings).create_from_zip(io.BytesIO(b"data"), "project.tar.gz")


def test_rejects_upload_larger_than_limit(tmp_path: Path, make_zip: MakeZip) -> None:
    settings = Settings(workspace_dir=tmp_path / "ws", max_upload_size_mb=0)

    with pytest.raises(ContentTooLargeError):
        ProjectService(settings).create_from_zip(io.BytesIO(make_zip({"a.py": "x"})), "a.zip")


@pytest.mark.parametrize(
    ("entries", "expected_error"),
    [
        ({"ok.py": "x", "../evil.py": "x"}, UnsafeArchiveError),
        ({"README.md": "# no code here"}, NoSourceFilesError),
        ({}, NoSourceFilesError),
    ],
)
def test_failed_zip_ingestion_leaves_no_files_behind(
    settings: Settings, make_zip: MakeZip, entries: dict, expected_error: type[Exception]
) -> None:
    with pytest.raises(expected_error):
        ProjectService(settings).create_from_zip(io.BytesIO(make_zip(entries)), "bad.zip")

    assert workspace_entries(settings) == []


def test_invalid_zip_content_leaves_no_files_behind(settings: Settings) -> None:
    with pytest.raises(InvalidArchiveError):
        ProjectService(settings).create_from_zip(io.BytesIO(b"not a zip"), "broken.zip")

    assert workspace_entries(settings) == []


# ----- GitHub ingestion (git is replaced by a fake, no network) -----


def fake_clone(files: dict[str, str]):  # type: ignore[no-untyped-def]
    def clone(_url: str, destination: Path, _timeout: int) -> None:
        for relative_path, content in files.items():
            path = destination / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    return clone


def test_create_from_github(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        project_service, "clone_repository", fake_clone({"src/Main.java": "class Main {}"})
    )

    project = ProjectService(settings).create_from_github("https://github.com/octo/demo.git")

    assert project.name == "demo"
    assert project.source == "https://github.com/octo/demo"
    assert project.source_type == SourceType.GITHUB
    assert project.languages == {"java": 1}


def test_invalid_github_url_creates_nothing(settings: Settings) -> None:
    with pytest.raises(InvalidGitHubUrlError):
        ProjectService(settings).create_from_github("https://example.com/a/b")

    assert workspace_entries(settings) == []


def test_failed_clone_is_cleaned_up(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_clone(_url: str, destination: Path, _timeout: int) -> None:
        destination.mkdir(parents=True)
        (destination / "partial.py").write_text("x")
        raise RepositoryCloneError("network down")

    monkeypatch.setattr(project_service, "clone_repository", failing_clone)

    with pytest.raises(RepositoryCloneError):
        ProjectService(settings).create_from_github("https://github.com/octo/demo")

    assert workspace_entries(settings) == []


def test_rejects_repository_larger_than_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(workspace_dir=tmp_path / "ws", max_repository_size_mb=0)
    monkeypatch.setattr(project_service, "clone_repository", fake_clone({"a.py": "x = 1"}))

    with pytest.raises(ContentTooLargeError):
        ProjectService(settings).create_from_github("https://github.com/octo/demo")

    assert workspace_entries(settings) == []


# ----- Reading and deleting -----


def test_delete_project(settings: Settings, make_zip: MakeZip) -> None:
    service = ProjectService(settings)
    project = service.create_from_zip(io.BytesIO(make_zip({"a.py": "x"})), "a.zip")

    service.delete_project(project.id)

    assert workspace_entries(settings) == []
    with pytest.raises(ProjectNotFoundError):
        service.get_project(project.id)


def test_unknown_project_raises_not_found(settings: Settings) -> None:
    with pytest.raises(ProjectNotFoundError):
        ProjectService(settings).get_project("0" * 32)


def test_list_ignores_incomplete_projects(settings: Settings) -> None:
    # A folder without project.json (e.g. the server crashed mid-import) is not listed.
    (settings.workspace_dir / ("a" * 32)).mkdir(parents=True)

    assert ProjectService(settings).list_projects() == []
