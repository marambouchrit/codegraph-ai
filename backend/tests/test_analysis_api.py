"""Phase 11.5: POST /projects/{project_id}/analyze, with no server and no model.

A real project is imported (ZIP, temporary workspace) and analyzed by the real
GraphService (Phases 3-6) and VectorIndexService (Phase 8), over a fake Neo4j, an
in-memory Qdrant and hashing embeddings. The last tests chat on the analyzed project.
"""

import ast
import io
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable

from app.api import dependencies
from app.api.dependencies import get_analysis_service, get_chat_service, get_project_service
from app.core.config import Settings
from app.core.errors import AnalysisInProgressError, VectorStoreUnavailableError
from app.graph.client import Neo4jClient
from app.graph.models import GraphBuildReport
from app.graph.repository import GraphRepository
from app.llm.generator import LLMGenerationService
from app.main import app
from app.rag.models import VectorIndexReport
from app.schemas.analysis import AnalysisResponse
from app.services.analysis_service import AnalysisResult, AnalysisService
from app.services.chat_service import ChatService
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graph_service import GraphService
from app.services.graphrag_service import GraphRAGService
from app.services.project_service import ProjectService
from app.services.vector_index_service import VectorIndexService
from app.services.vector_retrieval_service import VectorRetrievalService
from tests.conftest import MakeZip
from tests.graph_fakes import FakeNeo4j, fake_driver_factory
from tests.test_graphrag import FILES, QUESTION
from tests.test_llm import FakeLLM
from tests.vector_helpers import FailingEmbeddings, HashingEmbeddings, memory_store

SECRET = "neo4j-password-SECRET-0123"


class Analysis:
    """The API client over real services on fake databases; counts what was built."""

    def __init__(self, settings: Settings, project_id: str) -> None:
        self.settings = settings
        self.project_id = project_id
        self.projects = ProjectService(settings)
        self.database = FakeNeo4j()
        self.neo4j = Neo4jClient("bolt://localhost:7687", "neo4j", SECRET, "neo4j",
                                 driver_factory=fake_driver_factory(self.database))  # fmt: skip
        self.store = memory_store()
        self.embeddings: HashingEmbeddings = HashingEmbeddings()
        self.graph_builds: list[str] = []
        self.vector_builds: list[str] = []
        self.graph_error: Exception | None = None  # raised by build_project_graph
        self.client = TestClient(app, raise_server_exceptions=False)

    def service(self) -> AnalysisService:
        def graph() -> GraphService:
            service = GraphService(self.settings, GraphRepository(self.neo4j), self.projects)
            build = service.build_project_graph

            def counted(project_id: str) -> Any:
                self.graph_builds.append(project_id)
                if self.graph_error:
                    raise self.graph_error
                return build(project_id)

            service.build_project_graph = counted  # type: ignore[method-assign]
            return service

        def vectors() -> VectorIndexService:
            service = VectorIndexService(self.settings, self.store, self.embeddings, self.projects)
            index = service.index_project

            def counted(project_id: str) -> Any:
                self.vector_builds.append(project_id)
                return index(project_id)

            service.index_project = counted  # type: ignore[method-assign]
            return service

        return AnalysisService(self.projects, graph, vectors)

    def analyze(self, project_id: str | None = None, **kwargs: Any) -> Any:
        return self.client.post(f"/projects/{project_id or self.project_id}/analyze", **kwargs)

    def chat_service(self) -> ChatService:
        graphrag = GraphRAGService(VectorRetrievalService(self.store, self.embeddings),
                                   GraphRetrievalService(GraphRepository(self.neo4j)), Settings())  # fmt: skip
        llm = LLMGenerationService(FakeLLM("`AuthService.login` checks the password [1]."))
        return ChatService(self.projects, lambda: graphrag, lambda: llm)


@pytest.fixture
def analysis(settings: Settings, make_zip: MakeZip) -> Iterator[Analysis]:
    project = ProjectService(settings).create_from_zip(io.BytesIO(make_zip(FILES)), "auth.zip")
    analysis = Analysis(settings, project.id)
    app.dependency_overrides[get_project_service] = lambda: ProjectService(settings)
    app.dependency_overrides[get_analysis_service] = analysis.service
    app.dependency_overrides[get_chat_service] = analysis.chat_service
    yield analysis
    app.dependency_overrides.clear()


# ----- Success -----


def test_analysis_builds_the_graph_and_the_vector_index(analysis: Analysis) -> None:
    response = analysis.analyze()

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"project_id", "status", "graph", "vectors", "failed_files",
                         "warnings", "duration_seconds", "analyzed_at"}  # fmt: skip
    assert body["project_id"] == analysis.project_id and body["status"] == "ready"
    assert body["failed_files"] == 0 and body["warnings"] == []
    # What the response says is what the databases hold.
    graph, vectors = body["graph"], body["vectors"]
    assert graph["entities"] == len(analysis.database.nodes) > 0
    assert graph["relationships"] == len(analysis.database.relationships) > 0
    assert vectors["chunks"] == analysis.store.count(analysis.project_id) > 0
    assert graph["files"] == vectors["files"] == len(FILES)
    assert vectors["embedding_model"] == "test/hashing"
    assert (graph["stale_entities_removed"], vectors["stale_chunks_removed"]) == (0, 0)
    assert analysis.graph_builds == analysis.vector_builds == [analysis.project_id]


def test_the_reports_are_translated_without_internal_details(analysis: Analysis) -> None:
    body = analysis.analyze().json()

    assert set(body["graph"]) == {"files", "entities", "relationships", "entities_by_type",
                                  "relationships_by_type", "unresolved_references",
                                  "stale_entities_removed"}  # fmt: skip
    assert set(body["vectors"]) == {"files", "chunks", "chunks_by_type", "embedding_model",
                                    "stale_chunks_removed"}  # fmt: skip
    assert sum(body["graph"]["entities_by_type"].values()) == body["graph"]["entities"]
    assert sum(body["graph"]["relationships_by_type"].values()) == body["graph"]["relationships"]
    assert sum(body["vectors"]["chunks_by_type"].values()) == body["vectors"]["chunks"]
    assert "CALLS" in body["graph"]["relationships_by_type"]
    # No build or index ID, no vector, no dimension, no credential.
    text = str(body)
    assert "build_id" not in text and "index_id" not in text and "dimension" not in text
    assert SECRET not in text


def test_analyzing_again_refreshes_without_duplicates(analysis: Analysis) -> None:
    first = analysis.analyze().json()
    nodes, relationships = len(analysis.database.nodes), len(analysis.database.relationships)
    points = analysis.store.count(analysis.project_id)

    second = analysis.analyze()

    assert second.status_code == 200
    body = second.json()
    assert body["graph"]["entities"] == first["graph"]["entities"]
    assert body["vectors"]["chunks"] == first["vectors"]["chunks"]
    assert (body["graph"]["stale_entities_removed"], body["vectors"]["stale_chunks_removed"]) == (0, 0)
    assert len(analysis.database.nodes) == nodes
    assert len(analysis.database.relationships) == relationships
    assert analysis.store.count(analysis.project_id) == points
    # One graph build and one indexing per request, nothing more.
    assert analysis.graph_builds == analysis.vector_builds == [analysis.project_id] * 2


def test_no_body_is_needed_and_a_body_changes_nothing(analysis: Analysis) -> None:
    ignored = analysis.analyze(json={"cypher": "MATCH (n) DETACH DELETE n", "path": "/etc"})

    assert ignored.status_code == 200 and ignored.json()["status"] == "ready"
    assert all("DETACH DELETE n" != query for query, _ in analysis.database.queries)


def test_warnings_for_skipped_files_and_an_empty_index() -> None:
    graph = GraphBuildReport("p", "b", 3, 5, 4, {"File": 3, "Project": 1, "Class": 1}, {"CONTAINS": 4},
                             0, 0, 0, 1, 0.5)  # fmt: skip
    vectors = VectorIndexReport("p", "i", "test/hashing", 256, 3, 0, {}, 0, 1, 0.5)

    response = AnalysisResponse.from_result("p", AnalysisResult(graph, vectors, 1.0))

    assert response.status == "ready" and response.failed_files == 1
    assert response.warnings == [
        "1 file(s) could not be parsed and were skipped.",
        "No code could be indexed: chat will not find anything to answer from.",
    ]


# ----- Import -> analyze -> chat -----


def test_an_analyzed_project_can_be_chatted_with(analysis: Analysis) -> None:
    before = analysis.client.post(f"/projects/{analysis.project_id}/chat", json={"question": QUESTION})
    assert "insufficient" in before.json()["answer"]  # not analyzed yet

    assert analysis.analyze().status_code == 200
    response = analysis.client.post(f"/projects/{analysis.project_id}/chat",
                                    json={"question": QUESTION})  # fmt: skip

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "`AuthService.login` checks the password [1]."
    assert body["cited"] == [1] and body["graph_status"] == "complete"
    assert body["sources"][0]["entity"] == "AuthService.login"
    assert any(source["found_by"] == "graph" for source in body["sources"])


# ----- Errors -----


@pytest.mark.parametrize(
    "project_id", ["f" * 32, "not-a-valid-id", "..%2F..%2Fetc", "A" * 32, "1234"]
)
def test_unknown_or_invalid_project_is_404(analysis: Analysis, project_id: str) -> None:
    response = analysis.analyze(project_id)

    assert response.status_code == 404
    assert analysis.graph_builds == analysis.vector_builds == []
    assert analysis.database.queries == []


def test_neo4j_unavailable_is_503_before_any_indexing(analysis: Analysis) -> None:
    def unavailable(_query: str, _parameters: dict[str, Any]) -> None:
        raise ServiceUnavailable(f"connection refused (password {SECRET})")

    analysis.database.before_query = unavailable

    response = analysis.analyze()

    assert response.status_code == 503
    assert "status" not in response.json() and SECRET not in response.text
    assert analysis.vector_builds == []  # the slow step is not started
    assert analysis.embeddings.documents_embedded == 0


def test_vector_indexing_failure_is_an_error_not_ready(analysis: Analysis) -> None:
    analysis.embeddings = FailingEmbeddings(fail_after=0)

    response = analysis.analyze()

    assert response.status_code == 503
    assert response.json() == {"detail": "The embedding model failed to embed the text."}
    assert analysis.graph_builds == analysis.vector_builds == [analysis.project_id]
    assert analysis.store.count(analysis.project_id) == 0


def test_qdrant_unavailable_is_503(analysis: Analysis, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> None:
        raise VectorStoreUnavailableError("Qdrant is not reachable.")

    monkeypatch.setattr(analysis.store, "verify_connectivity", unavailable)

    response = analysis.analyze()

    assert response.status_code == 503 and "status" not in response.json()
    assert analysis.embeddings.documents_embedded == 0


def test_a_failed_indexing_keeps_the_previous_vectors(analysis: Analysis) -> None:
    assert analysis.analyze().status_code == 200
    points = analysis.store.count(analysis.project_id)
    analysis.embeddings = FailingEmbeddings(fail_after=3)

    assert analysis.analyze().status_code == 503
    # Nothing was deleted: chat keeps working on the previous index.
    assert analysis.store.count(analysis.project_id) >= points


def test_unexpected_errors_leak_nothing(analysis: Analysis) -> None:
    analysis.graph_error = RuntimeError(f"boom at C:\\secret\\path with {SECRET}")

    response = analysis.analyze()

    assert response.status_code == 500
    assert SECRET not in response.text and "secret\\path" not in response.text
    assert "Traceback" not in response.text


def test_a_failure_releases_the_project_for_the_next_analysis(analysis: Analysis) -> None:
    analysis.graph_error = RuntimeError("boom")
    assert analysis.analyze().status_code == 500

    analysis.graph_error = None
    assert analysis.analyze().status_code == 200


def test_the_same_project_is_not_analyzed_twice_at_the_same_time(
    settings: Settings, make_zip: MakeZip
) -> None:
    projects = ProjectService(settings)
    project = projects.create_from_zip(io.BytesIO(make_zip(FILES)), "auth.zip")
    other = projects.create_from_zip(io.BytesIO(make_zip(FILES)), "auth.zip")
    inside, release = threading.Event(), threading.Event()
    results: dict[str, Any] = {}

    class SlowGraph:
        def build_project_graph(self, project_id: str) -> Any:
            if project_id == other.id:
                raise RuntimeError("other project analyzed")
            inside.set()
            release.wait(5)
            raise RuntimeError("stop here")

    class NoVectors:
        def index_project(self, project_id: str) -> Any:
            raise AssertionError("not reached")

    service = AnalysisService(projects, SlowGraph, NoVectors)
    worker = threading.Thread(target=lambda: results.setdefault("first", _run(service, project.id)))
    worker.start()
    assert inside.wait(5)
    try:
        with pytest.raises(AnalysisInProgressError) as error:
            service.analyze(project.id)
        assert error.value.status_code == 409
        # Another project is not blocked.
        assert str(_run(service, other.id)) == "other project analyzed"
    finally:
        release.set()
        worker.join(5)
    assert str(results["first"]) == "stop here"


def _run(service: AnalysisService, project_id: str) -> Any:
    try:
        return service.analyze(project_id)
    except Exception as error:  # noqa: BLE001 - the test inspects it
        return error


# ----- Dependency injection, documentation, architecture -----


def test_nothing_is_connected_for_an_unknown_project(settings: Settings) -> None:
    """The real dependency: a 404 comes before any Neo4j, Qdrant or model is created."""
    dependencies.close_chat_resources()
    app.dependency_overrides[get_project_service] = lambda: ProjectService(settings)
    try:
        response = TestClient(app).post(f"/projects/{'f' * 32}/analyze")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert dependencies._database_clients.cache_info().currsize == 0


def test_the_endpoint_is_documented(analysis: Analysis) -> None:
    operation = analysis.client.get("/openapi.json").json()["paths"]["/projects/{project_id}/analyze"]["post"]

    assert operation["summary"] == "Analyze and index a project for chat"
    assert "requestBody" not in operation
    assert {"200", "404", "409", "503"} <= set(operation["responses"])
    assert "/projects/{project_id}/chat" in analysis.client.get("/openapi.json").json()["paths"]


def test_the_route_and_service_only_orchestrate() -> None:
    """No parser, database client, query, chunker or embedding in the analysis route or service."""
    forbidden = ("neo4j", "qdrant_client", "sentence_transformers", "tree_sitter",
                 "app.parsing", "app.extraction", "app.relationships", "app.graph.client",
                 "app.graph.repository", "app.graph.builder", "app.rag.chunker",
                 "app.rag.embeddings", "app.rag.vector_store", "app.llm")  # fmt: skip
    root = Path(__file__).parent.parent / "app"
    for path in (root / "api" / "routes" / "analysis.py", root / "services" / "analysis_service.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom | ast.Import):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [
                    alias.name for alias in node.names
                ]  # fmt: skip
                for name in names:
                    assert not name.startswith(forbidden), f"{path.name} imports {name}"


# ----- Persisted analysis state: GET /projects/{id}/analysis (Phase 13) -----


def state(analysis: Analysis, project_id: str | None = None) -> Any:
    return analysis.client.get(f"/projects/{project_id or analysis.project_id}/analysis")


def test_a_new_project_is_not_analyzed(analysis: Analysis) -> None:
    response = state(analysis)

    assert response.status_code == 200
    assert response.json() == {"project_id": analysis.project_id, "status": "not_analyzed",
                               "analysis": None}  # fmt: skip


def test_an_analyzed_project_is_ready_with_the_real_report(analysis: Analysis) -> None:
    analyzed = analysis.analyze().json()

    body = state(analysis).json()

    assert body["status"] == "ready"
    assert body["analysis"] == analyzed  # the same report, counts included
    assert body["analysis"]["graph"]["entities"] == len(analysis.database.nodes)
    assert body["analysis"]["vectors"]["chunks"] == analysis.store.count(analysis.project_id)


def test_the_state_survives_a_reload(analysis: Analysis, settings: Settings) -> None:
    """A new service (new request, restarted server) reads the state from the workspace."""
    analyzed = analysis.analyze().json()
    restarted = AnalysisService(ProjectService(settings), _unused, _unused)

    result = restarted.get_analysis(analysis.project_id)

    assert result is not None
    assert result.graph.nodes_written == analyzed["graph"]["entities"]
    assert result.analyzed_at.isoformat().replace("+00:00", "Z") == analyzed["analyzed_at"]


def test_reading_the_state_queries_no_database(analysis: Analysis) -> None:
    analysis.analyze()
    analysis.database.queries.clear()
    builds = (len(analysis.graph_builds), len(analysis.vector_builds))

    assert state(analysis).json()["status"] == "ready"
    assert analysis.database.queries == []
    assert (len(analysis.graph_builds), len(analysis.vector_builds)) == builds


def test_re_analysis_updates_the_state(analysis: Analysis, settings: Settings) -> None:
    first = analysis.analyze().json()
    source = ProjectService(settings).workspace.source_dir(analysis.project_id)
    (source / "extra.py").write_text("def extra():\n    return 1\n", encoding="utf-8")

    second = analysis.analyze().json()

    body = state(analysis).json()
    assert body["analysis"] == second
    assert second["analyzed_at"] > first["analyzed_at"]
    assert second["graph"]["entities"] == first["graph"]["entities"] + 2  # a file + a function
    assert second["vectors"]["files"] == first["vectors"]["files"] + 1


def test_a_failed_first_analysis_is_not_ready(analysis: Analysis) -> None:
    analysis.embeddings = FailingEmbeddings(fail_after=0)

    assert analysis.analyze().status_code == 503
    assert state(analysis).json()["status"] == "not_analyzed"


def test_a_failed_re_analysis_is_not_ready(analysis: Analysis) -> None:
    assert analysis.analyze().status_code == 200
    analysis.graph_error = RuntimeError("boom")

    assert analysis.analyze().status_code == 500
    # The databases may hold a mix of old and new data: never report "ready" for it.
    assert state(analysis).json() == {"project_id": analysis.project_id,
                                      "status": "not_analyzed", "analysis": None}  # fmt: skip


def test_a_refused_concurrent_analysis_keeps_the_state(analysis: Analysis, settings: Settings) -> None:
    assert analysis.analyze().status_code == 200
    service = analysis.service()
    with _exclusive_for_test(analysis.project_id):
        with pytest.raises(AnalysisInProgressError):
            service.analyze(analysis.project_id)

    assert state(analysis).json()["status"] == "ready"  # the running analysis decides


def test_projects_have_separate_states(analysis: Analysis, settings: Settings, make_zip: MakeZip) -> None:
    other = ProjectService(settings).create_from_zip(io.BytesIO(make_zip(FILES)), "b.zip")

    analysis.analyze()

    assert state(analysis).json()["status"] == "ready"
    assert state(analysis, other.id).json()["status"] == "not_analyzed"


@pytest.mark.parametrize("project_id", ["f" * 32, "not-a-valid-id", "..%2F..%2Fetc", "A" * 32])
def test_the_state_of_an_unknown_or_invalid_project_is_404(analysis: Analysis, project_id: str) -> None:
    assert state(analysis, project_id).status_code == 404


def test_a_project_analyzed_before_the_state_existed_is_not_analyzed(
    analysis: Analysis, settings: Settings
) -> None:
    """No analysis.json (analyzed before Phase 13): honest "not_analyzed", never a 500."""
    analysis.analyze()
    ProjectService(settings).workspace.analysis_file(analysis.project_id).unlink()

    assert state(analysis).json()["status"] == "not_analyzed"


@pytest.mark.parametrize("content", ["{not json", "{}", '{"graph": {"unknown": 1}}', "[]"])
def test_an_unreadable_record_is_not_analyzed(analysis: Analysis, settings: Settings, content: str) -> None:
    ProjectService(settings).workspace.analysis_file(analysis.project_id).write_text(content)

    response = state(analysis)

    assert response.status_code == 200 and response.json()["status"] == "not_analyzed"


def test_a_record_of_another_project_is_ignored(
    analysis: Analysis, settings: Settings, make_zip: MakeZip
) -> None:
    other = ProjectService(settings).create_from_zip(io.BytesIO(make_zip(FILES)), "b.zip")
    analysis.analyze()
    workspace = ProjectService(settings).workspace
    workspace.analysis_file(other.id).write_text(
        workspace.analysis_file(analysis.project_id).read_text(encoding="utf-8"), encoding="utf-8"
    )

    assert state(analysis, other.id).json()["status"] == "not_analyzed"


def test_deleting_a_project_deletes_its_state(analysis: Analysis) -> None:
    analysis.analyze()

    assert analysis.client.delete(f"/projects/{analysis.project_id}").status_code == 204
    assert state(analysis).status_code == 404


def test_the_state_endpoint_is_documented(analysis: Analysis) -> None:
    paths = analysis.client.get("/openapi.json").json()["paths"]
    operation = paths["/projects/{project_id}/analysis"]["get"]

    assert operation["summary"] == "Get the analysis state of a project"
    assert "404" in operation["responses"]


def _unused() -> Any:
    raise AssertionError("reading the state must not build a service")


@contextmanager
def _exclusive_for_test(project_id: str) -> Iterator[None]:
    """Hold the analysis lock of a project, as a running analysis does."""
    from app.services import analysis_service

    with analysis_service._exclusive(project_id):
        yield
