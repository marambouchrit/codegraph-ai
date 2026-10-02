"""Phase 13: GET /projects/{project_id}/graph, with no server.

Two real projects (same code) are imported (ZIP, temporary workspace) and built by the
real GraphService into a fake Neo4j; the endpoint reads them through the real
GraphRetrievalService and GraphRepository.
"""

import ast
import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable

from app.api import dependencies
from app.api.dependencies import get_project_graph_service, get_project_service
from app.core.config import Settings
from app.core.errors import InvalidGraphQueryError, ProjectNotFoundError
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.graph.schema import RELATIONSHIP_TYPES
from app.main import app
from app.services import graph_retrieval_service
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graph_service import GraphService
from app.services.project_graph_service import ProjectGraphService
from app.services.project_service import ProjectService
from tests.conftest import MakeZip
from tests.graph_fakes import FakeNeo4j, fake_driver_factory
from tests.test_graphrag import FILES

SECRET = "neo4j-password-SECRET-0123"
TYPE_ORDER = {"file": 0, "class": 1, "interface": 1, "function": 2, "method": 3}


class Graphs:
    """Projects A and B analyzed into a fake Neo4j, C imported only; the API client."""

    def __init__(self, settings: Settings, make_zip: MakeZip) -> None:
        self.projects = ProjectService(settings)
        self.a, self.b, self.c = (
            self.projects.create_from_zip(io.BytesIO(make_zip(FILES)), "auth.zip").id
            for _ in range(3)
        )
        self.database = FakeNeo4j()
        self.neo4j = Neo4jClient("bolt://localhost:7687", "neo4j", SECRET, "neo4j",
                                 driver_factory=fake_driver_factory(self.database))  # fmt: skip
        builder = GraphService(settings, GraphRepository(self.neo4j), self.projects)
        builder.build_project_graph(self.a)
        builder.build_project_graph(self.b)
        self.database.queries.clear()
        self.retrievals = 0
        self.client = TestClient(app, raise_server_exceptions=False)

    def service(self) -> ProjectGraphService:
        def retrieval() -> GraphRetrievalService:
            self.retrievals += 1
            return GraphRetrievalService(GraphRepository(self.neo4j))

        return ProjectGraphService(self.projects, retrieval)

    def get(self, project_id: str, **params: Any) -> Any:
        return self.client.get(f"/projects/{project_id}/graph", params=params)


@pytest.fixture
def graphs(settings: Settings, make_zip: MakeZip) -> Iterator[Graphs]:
    graphs = Graphs(settings, make_zip)
    app.dependency_overrides[get_project_service] = lambda: graphs.projects
    app.dependency_overrides[get_project_graph_service] = graphs.service
    yield graphs
    app.dependency_overrides.clear()


# ----- Success -----


def test_the_graph_is_the_real_project_graph(graphs: Graphs) -> None:
    response = graphs.get(graphs.a)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"project_id", "nodes", "edges", "truncated", "total_nodes", "total_edges"}
    stored_nodes = graphs.database.project_node_ids(graphs.a)
    stored_edges = {(s, t, target) for s, t, target in graphs.database.edges() if s in stored_nodes}
    assert {node["id"] for node in body["nodes"]} == stored_nodes
    assert {(e["source"], e["relationship_type"], e["target"]) for e in body["edges"]} == stored_edges
    assert body["truncated"] is False
    assert body["total_nodes"] == len(stored_nodes) == len(body["nodes"])
    assert body["total_edges"] == len(stored_edges) == len(body["edges"])


def test_nodes_and_edges_carry_only_display_metadata(graphs: Graphs) -> None:
    body = graphs.get(graphs.a).json()

    login = next(n for n in body["nodes"] if n["qualified_name"] == "AuthService.login")
    assert login == {
        "id": login["id"], "entity_type": "method", "name": "login",
        "qualified_name": "AuthService.login", "file_path": "auth/service.py",
        "language": "python", "start_line": 14, "end_line": 20,
    }  # fmt: skip
    assert all(set(e) == {"id", "source", "target", "relationship_type"} for e in body["edges"])
    assert {e["relationship_type"] for e in body["edges"]} <= RELATIONSHIP_TYPES
    assert {"CONTAINS", "CALLS"} <= {e["relationship_type"] for e in body["edges"]}
    assert "build_id" not in str(body) and SECRET not in str(body)


def test_the_same_project_always_gives_the_same_graph(graphs: Graphs) -> None:
    first, second = graphs.get(graphs.a).json(), graphs.get(graphs.a).json()

    assert first == second
    order = [(TYPE_ORDER[n["entity_type"]], n["file_path"], n["start_line"], n["id"])
             for n in first["nodes"]]  # fmt: skip
    assert order == sorted(order)  # structure first: files, classes, functions, methods


# ----- Isolation -----


def test_projects_never_mix(graphs: Graphs) -> None:
    a, b = graphs.get(graphs.a).json(), graphs.get(graphs.b).json()

    assert all(n["id"].startswith(f"{graphs.a}:") for n in a["nodes"])
    assert all(n["id"].startswith(f"{graphs.b}:") for n in b["nodes"])
    for edge in a["edges"]:
        assert edge["source"].startswith(f"{graphs.a}:") and edge["target"].startswith(f"{graphs.a}:")
    assert len(a["nodes"]) == len(b["nodes"])  # same code, separate graphs
    # Every query was scoped to the project asked for, with parameters.
    assert {params["project_id"] for _, params in graphs.database.queries} == {graphs.a, graphs.b}


# ----- Limits and truncation -----


def test_a_bounded_graph_keeps_the_structure_and_says_it_is_truncated(graphs: Graphs) -> None:
    full = graphs.get(graphs.a).json()

    body = graphs.get(graphs.a, limit=5).json()

    assert len(body["nodes"]) == 5 and body["truncated"] is True
    assert {n["entity_type"] for n in body["nodes"]} <= {"file", "class"}  # 4 files, then a class
    assert body["nodes"] == full["nodes"][:5]
    ids = {n["id"] for n in body["nodes"]}
    assert body["edges"] and all(e["source"] in ids and e["target"] in ids for e in body["edges"])
    # The totals are the whole project's.
    assert (body["total_nodes"], body["total_edges"]) == (full["total_nodes"], full["total_edges"])


def test_too_many_edges_are_truncated_too(graphs: Graphs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_retrieval_service, "MAX_GRAPH_EDGES", 3)

    body = graphs.get(graphs.a).json()

    assert len(body["edges"]) == 3 and body["truncated"] is True
    assert len(body["nodes"]) == body["total_nodes"]  # all nodes: only the edges were cut


def test_the_limit_is_exact(graphs: Graphs) -> None:
    total = graphs.get(graphs.a).json()["total_nodes"]

    exact = graphs.get(graphs.a, limit=total).json()

    assert len(exact["nodes"]) == total and exact["truncated"] is False


@pytest.mark.parametrize("limit", ["0", "-1", "501", "abc", "1.5", ""])
def test_invalid_limits_are_422_without_any_query(graphs: Graphs, limit: str) -> None:
    response = graphs.client.get(f"/projects/{graphs.a}/graph?limit={limit}")

    assert response.status_code == 422
    assert graphs.database.queries == [] and graphs.retrievals == 0


def test_the_largest_limit_is_allowed(graphs: Graphs) -> None:
    assert graphs.get(graphs.a, limit=500).status_code == 200


def test_unknown_query_parameters_change_nothing(graphs: Graphs) -> None:
    plain = graphs.get(graphs.a).json()

    injected = graphs.client.get(f"/projects/{graphs.a}/graph", params={
        "cypher": "MATCH (n) DETACH DELETE n", "label": "Secret",
        "relationship_type": "X", "project_id": graphs.b,
    }).json()  # fmt: skip

    assert injected == plain
    assert all("DETACH DELETE n" not in query for query, _ in graphs.database.queries)


# ----- Empty, unknown, errors -----


def test_an_unanalyzed_project_has_an_empty_graph(graphs: Graphs) -> None:
    body = graphs.get(graphs.c).json()

    assert body == {"project_id": graphs.c, "nodes": [], "edges": [], "truncated": False,
                    "total_nodes": 0, "total_edges": 0}  # fmt: skip


@pytest.mark.parametrize("project_id", ["f" * 32, "not-a-valid-id", "..%2F..%2Fetc", "A" * 32])
def test_unknown_or_invalid_project_is_404(graphs: Graphs, project_id: str) -> None:
    response = graphs.get(project_id)

    assert response.status_code == 404
    assert graphs.database.queries == [] and graphs.retrievals == 0


def test_neo4j_unavailable_is_503(graphs: Graphs) -> None:
    def unavailable(_query: str, _parameters: dict[str, Any]) -> None:
        raise ServiceUnavailable(f"connection refused (password {SECRET})")

    graphs.database.before_query = unavailable

    response = graphs.get(graphs.a)

    assert response.status_code == 503
    assert "Neo4j is not reachable" in response.json()["detail"]
    assert SECRET not in response.text


def test_unexpected_errors_leak_nothing(graphs: Graphs) -> None:
    def broken(_query: str, _parameters: dict[str, Any]) -> None:
        raise RuntimeError(f"boom at C:\\secret\\path with {SECRET}")

    graphs.database.before_query = broken

    response = graphs.get(graphs.a)

    assert response.status_code == 500
    assert SECRET not in response.text and "secret\\path" not in response.text


# ----- The retrieval method itself -----


@pytest.mark.parametrize("limit", [0, 501, -3, True, "10"])
def test_the_service_validates_the_limit(limit: Any) -> None:
    service = GraphRetrievalService(GraphRepository(Neo4jClient(
        "bolt://localhost:7687", "neo4j", "", "neo4j", driver_factory=fake_driver_factory(FakeNeo4j())
    )))  # fmt: skip
    with pytest.raises(InvalidGraphQueryError):
        service.get_project_graph("a" * 32, limit)


def test_the_service_refuses_an_invalid_project_id() -> None:
    database = FakeNeo4j()
    service = GraphRetrievalService(GraphRepository(Neo4jClient(
        "bolt://localhost:7687", "neo4j", "", "neo4j", driver_factory=fake_driver_factory(database)
    )))  # fmt: skip
    with pytest.raises(ProjectNotFoundError):
        service.get_project_graph("../etc")
    assert database.queries == []


# ----- Dependency injection, documentation, architecture -----


def test_nothing_is_connected_for_an_unknown_project(settings: Settings) -> None:
    """The real dependency: a 404 comes before any Neo4j client is created."""
    dependencies.close_chat_resources()
    app.dependency_overrides[get_project_service] = lambda: ProjectService(settings)
    try:
        response = TestClient(app).get(f"/projects/{'f' * 32}/graph")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert dependencies._database_clients.cache_info().currsize == 0


def test_the_endpoint_is_documented(graphs: Graphs) -> None:
    operation = graphs.client.get("/openapi.json").json()["paths"]["/projects/{project_id}/graph"]["get"]

    assert operation["summary"] == "Get the knowledge graph of a project"
    [limit] = [p for p in operation["parameters"] if p["name"] == "limit"]
    assert (limit["schema"]["minimum"], limit["schema"]["maximum"], limit["schema"]["default"]) == (1, 500, 150)
    assert {"200", "404", "422", "503"} <= set(operation["responses"])


def test_the_route_and_service_only_orchestrate() -> None:
    """No database client or query in the graph route or service: they go through retrieval."""
    forbidden = ("neo4j", "qdrant_client", "app.graph.client", "app.graph.repository",
                 "app.graph.builder", "app.rag", "app.llm")  # fmt: skip
    root = Path(__file__).parent.parent / "app"
    for path in (root / "api" / "routes" / "graph.py", root / "services" / "project_graph_service.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom | ast.Import):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [
                    alias.name for alias in node.names
                ]  # fmt: skip
                for name in names:
                    assert not name.startswith(forbidden), f"{path.name} imports {name}"
