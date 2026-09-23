from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_project_service
from app.core.config import Settings
from app.main import app
from app.services.project_service import ProjectService
from tests.conftest import MakeZip


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """API client whose projects are stored in a temporary workspace."""
    app.dependency_overrides[get_project_service] = lambda: ProjectService(settings)
    yield TestClient(app)
    app.dependency_overrides.clear()


def upload(client: TestClient, data: bytes, filename: str = "demo.zip"):  # type: ignore[no-untyped-def]
    return client.post("/projects/zip", files={"file": (filename, data, "application/zip")})


def test_zip_project_lifecycle(client: TestClient, make_zip: MakeZip) -> None:
    created = upload(client, make_zip({"app.py": "x = 1", "lib/util.js": "var a = 1"}))
    assert created.status_code == 201
    project = created.json()
    assert project["name"] == "demo"
    assert project["languages"] == {"javascript": 1, "python": 1}

    assert [p["id"] for p in client.get("/projects").json()] == [project["id"]]
    assert client.get(f"/projects/{project['id']}").json() == project

    files = client.get(f"/projects/{project['id']}/files").json()
    assert files["files"] == [
        {"path": "app.py", "language": "python", "size_bytes": 5},
        {"path": "lib/util.js", "language": "javascript", "size_bytes": 9},
    ]

    assert client.delete(f"/projects/{project['id']}").status_code == 204
    assert client.get(f"/projects/{project['id']}").status_code == 404


def test_upload_rejects_path_traversal(client: TestClient, make_zip: MakeZip) -> None:
    response = upload(client, make_zip({"../evil.py": "x"}))

    assert response.status_code == 400
    assert "Unsafe path" in response.json()["detail"]


def test_upload_rejects_invalid_zip(client: TestClient) -> None:
    response = upload(client, b"definitely not a zip")

    assert response.status_code == 400
    assert "not a valid ZIP" in response.json()["detail"]


def test_upload_without_source_files_returns_422(client: TestClient, make_zip: MakeZip) -> None:
    response = upload(client, make_zip({"README.md": "hello"}))

    assert response.status_code == 422


def test_upload_requires_a_file(client: TestClient) -> None:
    assert client.post("/projects/zip").status_code == 422


def test_github_rejects_invalid_url(client: TestClient) -> None:
    response = client.post("/projects/github", json={"url": "https://gitlab.com/a/b"})

    assert response.status_code == 400
    assert "not a valid GitHub repository URL" in response.json()["detail"]


@pytest.mark.parametrize("project_id", ["0" * 32, "not-a-real-id", "A" * 32])
def test_unknown_project_returns_404(client: TestClient, project_id: str) -> None:
    assert client.get(f"/projects/{project_id}").status_code == 404
    assert client.get(f"/projects/{project_id}/files").status_code == 404
    assert client.delete(f"/projects/{project_id}").status_code == 404
