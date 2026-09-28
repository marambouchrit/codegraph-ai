"""GraphService: an imported project, analyzed by Phases 3-5, written to (a fake) Neo4j."""

import io
from typing import Any

import pytest
from neo4j.exceptions import ServiceUnavailable

from app.core.config import Settings
from app.core.errors import GraphDatabaseUnavailableError, ProjectNotFoundError
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.schemas.project import Project
from app.services.graph_service import GraphService
from app.services.project_service import ProjectService
from tests.conftest import MakeZip
from tests.graph_fakes import FakeNeo4j, fake_driver_factory

SOURCES = {
    "shop/models/user.py": "class User:\n    def save(self):\n        pass\n",
    "shop/services/auth.py": (
        "from models.user import User\n\ndef login():\n    user = User()\n    user.save()\n"
    ),
    "shop/web/api.ts": 'import { get } from "./http";\nexport function load() { get(); }\n',
    "shop/web/http.ts": "export function get() {}\n",
}


def make_service(settings: Settings, database: FakeNeo4j) -> GraphService:
    client = Neo4jClient("bolt://localhost:7687", "neo4j", "secret", "neo4j",
                         driver_factory=fake_driver_factory(database))  # fmt: skip
    return GraphService(settings, GraphRepository(client, settings.graph_batch_size))


def import_project(settings: Settings, make_zip: MakeZip, sources: dict[str, Any]) -> Project:
    return ProjectService(settings).create_from_zip(io.BytesIO(make_zip(sources)), "shop.zip")


def test_build_project_graph_from_an_imported_project(settings: Settings, make_zip: MakeZip) -> None:
    project = import_project(settings, make_zip, SOURCES)
    database = FakeNeo4j()
    service = make_service(settings, database)

    report = service.build_project_graph(project.id)

    assert report.project_id == project.id
    assert report.files == 4
    assert report.nodes_by_label == {"Class": 1, "File": 4, "Function": 3, "Method": 1}
    assert report.summary == "Graph built successfully: 4 files, 9 entities, 12 relationships"
    assert database.project_node_ids(project.id) == {n for n in database.nodes}
    assert (f"{project.id}:services/auth.py:login", "CALLS",
            f"{project.id}:models/user.py:User.save") in database.edges()  # fmt: skip

    statistics = service.get_statistics(project.id)
    assert statistics.node_count == 9
    assert statistics.relationship_count == 12
    assert service.project_graph_exists(project.id)


def test_rebuilding_a_project_is_idempotent(settings: Settings, make_zip: MakeZip) -> None:
    project = import_project(settings, make_zip, SOURCES)
    database = FakeNeo4j()
    service = make_service(settings, database)

    first = service.build_project_graph(project.id)
    second = service.build_project_graph(project.id)

    assert second.nodes_written == first.nodes_written
    assert second.relationships_written == first.relationships_written
    assert (second.stale_nodes_deleted, second.stale_relationships_deleted) == (0, 0)
    assert len(database.nodes) == 9
    assert len(database.relationships) == 12  # 5 CONTAINS, 3 CALLS, 2 IMPORTS, 2 DEPENDS_ON


def test_delete_project_graph_keeps_other_projects(settings: Settings, make_zip: MakeZip) -> None:
    first = import_project(settings, make_zip, SOURCES)
    second = import_project(settings, make_zip, SOURCES)
    database = FakeNeo4j()
    service = make_service(settings, database)
    service.build_project_graph(first.id)
    service.build_project_graph(second.id)

    deleted = service.delete_project_graph(first.id)

    assert deleted == 9
    assert not service.project_graph_exists(first.id)
    assert service.get_statistics(second.id).node_count == 9


def test_unknown_project_is_rejected_before_touching_neo4j(settings: Settings) -> None:
    database = FakeNeo4j()

    with pytest.raises(ProjectNotFoundError):
        make_service(settings, database).build_project_graph("f" * 32)

    assert database.queries == []


def test_neo4j_down_fails_before_the_analysis(
    settings: Settings, make_zip: MakeZip, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = import_project(settings, make_zip, SOURCES)
    database = FakeNeo4j()

    def unavailable(_query: str, _parameters: dict[str, Any]) -> None:
        raise ServiceUnavailable("connection refused")

    database.before_query = unavailable
    service = make_service(settings, database)
    analyzed: list[str] = []
    monkeypatch.setattr(service, "analyze_project", lambda project_id: analyzed.append(project_id))

    with pytest.raises(GraphDatabaseUnavailableError):
        service.build_project_graph(project.id)

    assert analyzed == []


def test_unresolved_references_are_reported_not_stored(settings: Settings, make_zip: MakeZip) -> None:
    project = import_project(
        settings, make_zip, {"app/main.py": "import requests\n\ndef run():\n    requests.get()\n"}
    )
    database = FakeNeo4j()

    report = make_service(settings, database).build_project_graph(project.id)

    assert report.unresolved_references == 2  # the import and the call
    assert {n.properties["name"] for n in database.nodes.values()} == {"main.py", "run"}
