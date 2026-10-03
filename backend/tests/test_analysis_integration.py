"""Optional: import -> analyze -> re-analyze -> insights -> chat over HTTP, everything real.

Skipped by a normal `pytest` run.

    docker compose up -d neo4j qdrant
    pytest -m "neo4j and qdrant and embeddings and llm"      (needs LLM_API_KEY)

The project is uploaded as a ZIP and analyzed by POST /projects/{id}/analyze as a real
background job (real Neo4j, embedding model and Qdrant), followed by polling
GET /projects/{id}/analysis. One file is then changed and the project analyzed again:
only that file is parsed and only its changed chunk embedded. The advanced analyses
and the chat (real LLM) then run on the updated project. Vectors go to a temporary
Qdrant collection; the graph and the collection are removed afterwards.
"""

import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from app.analysis.jobs import ThreadJobRunner
from app.api.dependencies import (
    get_analysis_service,
    get_chat_service,
    get_insights_service,
    get_project_service,
)
from app.core.config import Settings
from app.core.errors import LLMConfigurationError
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.llm.generator import LLMGenerationService
from app.llm.provider import create_llm_provider
from app.main import app
from app.rag.embeddings import embedding_provider_from_settings
from app.rag.vector_store import QdrantVectorStore
from app.services.analysis_service import AnalysisService
from app.services.chat_service import ChatService
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graph_service import GraphService
from app.services.graphrag_service import GraphRAGService
from app.services.insights_service import InsightsService
from app.services.project_service import ProjectService
from app.services.vector_index_service import VectorIndexService
from app.services.vector_retrieval_service import VectorRetrievalService
from tests.conftest import MakeZip
from tests.test_graphrag import FILES

pytestmark = [pytest.mark.neo4j, pytest.mark.qdrant, pytest.mark.embeddings, pytest.mark.llm]


class Servers:
    def __init__(self, http: TestClient, repository: GraphRepository, store: QdrantVectorStore,
                 projects: ProjectService) -> None:  # fmt: skip
        self.http = http
        self.repository = repository
        self.store = store
        self.projects = projects
        self.project_ids: list[str] = []
        self.embedding_model = ""

    def analyze(self, project_id: str) -> dict[str, Any]:
        """Start the background job, poll its state, return the report once ready."""
        started = self.http.post(f"/projects/{project_id}/analyze")
        assert started.status_code == 202, started.text
        assert started.json()["status"] in ("queued", "running")
        deadline = time.monotonic() + 1800
        phases: list[str] = []
        while True:
            state = self.http.get(f"/projects/{project_id}/analysis").json()
            if state["job"]["phase"] and state["job"]["phase"] not in phases:
                phases.append(state["job"]["phase"])
            if state["status"] not in ("queued", "running"):
                break
            assert time.monotonic() < deadline, "the analysis did not finish"
            time.sleep(0.2)
        assert state["status"] == "ready", state
        assert phases[-1] == "finalizing"
        return state["analysis"]


@pytest.fixture
def servers(settings: Settings) -> Iterator[Servers]:
    try:
        provider = create_llm_provider(settings)
    except LLMConfigurationError as error:
        pytest.skip(error.message)
    generator = LLMGenerationService(provider)
    projects = ProjectService(settings)
    qdrant = QdrantClient(url=settings.qdrant_url, timeout=30)
    store = QdrantVectorStore(qdrant, f"test_{uuid.uuid4().hex[:12]}", settings.qdrant_url)
    embeddings = embedding_provider_from_settings(settings)
    runner = ThreadJobRunner()
    with Neo4jClient.from_settings(settings) as neo4j:
        repository = GraphRepository(neo4j)
        retrieval = GraphRetrievalService(repository)
        graphrag = GraphRAGService(VectorRetrievalService(store, embeddings, settings),
                                   retrieval, settings)  # fmt: skip
        app.dependency_overrides[get_project_service] = lambda: projects
        app.dependency_overrides[get_analysis_service] = lambda: AnalysisService(
            projects,
            lambda: GraphService(settings, repository, projects),
            lambda: VectorIndexService(settings, store, embeddings, projects),
            runner,
        )
        app.dependency_overrides[get_insights_service] = lambda: InsightsService(
            projects, lambda: retrieval, lambda: provider
        )
        app.dependency_overrides[get_chat_service] = lambda: ChatService(
            projects, lambda: graphrag, lambda: generator
        )
        servers = Servers(TestClient(app), repository, store, projects)
        servers.embedding_model = embeddings.model_name
        try:
            yield servers
        finally:
            runner.shutdown()
            app.dependency_overrides.clear()
            for project_id in servers.project_ids:
                repository.delete_project(project_id)
            qdrant.delete_collection(store.collection)


def test_import_analyze_reanalyze_insights_and_chat_over_http(
    servers: Servers, make_zip: MakeZip
) -> None:
    http = servers.http

    # 1. Import: the source code is stored, nothing is analyzed yet.
    imported = http.post("/projects/zip",
                         files={"file": ("auth.zip", make_zip(FILES), "application/zip")})  # fmt: skip
    assert imported.status_code == 201, imported.text
    project_id = imported.json()["id"]
    servers.project_ids.append(project_id)
    assert not servers.repository.project_exists(project_id)
    assert http.get(f"/projects/{project_id}/analysis").json()["status"] == "not_analyzed"

    # 2. First analysis, in the background: everything is parsed and embedded.
    report = servers.analyze(project_id)
    assert report["mode"] == "full" and report["failed_files"] == 0
    assert report["graph"]["entities"] > 0 and report["graph"]["relationships"] > 0
    chunks = report["vectors"]["chunks"]
    assert chunks == servers.store.count(project_id) > 0
    assert report["changes"]["chunks_embedded"] == chunks
    assert report["vectors"]["embedding_model"] == servers.embedding_model  # EMBEDDING_MODEL
    statistics = servers.repository.statistics(project_id)
    assert sum(statistics.nodes_by_label.values()) == report["graph"]["entities"]

    # 3. Nothing changed: nothing parsed, nothing embedded.
    again = servers.analyze(project_id)
    assert again["mode"] == "incremental"
    assert (again["changes"]["files_parsed"], again["changes"]["chunks_embedded"]) == (0, 0)
    assert again["changes"]["chunks_reused"] == chunks == servers.store.count(project_id)

    # 4. One file changes (a new function, a deleted file): only that is processed.
    source = servers.projects.workspace.source_dir(project_id)
    with (source / "auth" / "service.py").open("a", encoding="utf-8", newline="\n") as file:
        file.write("\n\ndef logout(token):\n    \"\"\"End the session of a token.\"\"\"\n    return None\n")
    (source / "reports" / "charts.py").unlink()
    updated = servers.analyze(project_id)
    changes = updated["changes"]
    assert updated["mode"] == "incremental"
    assert (changes["files_modified"], changes["files_deleted"], changes["files_parsed"]) == (1, 1, 1)
    assert 1 <= changes["chunks_embedded"] < chunks  # far fewer than the whole project
    assert changes["chunks_deleted"] >= 1
    assert servers.store.count(project_id) == updated["vectors"]["chunks"]
    statistics = servers.repository.statistics(project_id)
    assert sum(statistics.nodes_by_label.values()) == updated["graph"]["entities"]
    graph = http.get(f"/projects/{project_id}/graph").json()
    names = {node["qualified_name"] for node in graph["nodes"]}
    assert "logout" in names and "render_bar_chart" not in names

    # 5. Advanced analyses on the updated graph.
    verify = next(n for n in graph["nodes"] if n["qualified_name"] == "verify_password")
    impact = http.get(f"/projects/{project_id}/analysis/impact", params={"entity_id": verify["id"]})
    assert impact.status_code == 200, impact.text
    assert "AuthService.login" in {i["entity"]["qualified_name"] for i in impact.json()["affected"]}
    dependencies = http.get(f"/projects/{project_id}/analysis/dependencies").json()
    assert ("auth/service.py", "repository/user.py") in {
        (d["source"], d["target"]) for d in dependencies["dependencies"]
    }
    architecture = http.get(f"/projects/{project_id}/analysis/architecture").json()
    assert architecture["facts"] and architecture["summary"], architecture["warnings"]
    assert architecture["cited"] and set(architecture["cited"]) <= {f["number"] for f in architecture["facts"]}

    # 6. Chat: a grounded, cited answer from the real LLM, on the updated project.
    response = http.post(f"/projects/{project_id}/chat",
                         json={"question": "How is authentication implemented?"})  # fmt: skip
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"].strip() and "insufficient" not in body["answer"]
    assert body["sources"] and body["cited"]
    assert body["graph_status"] in {"complete", "partial", "unavailable"}
    by_id = {source["id"]: source for source in body["sources"]}
    assert all(number in by_id and by_id[number]["cited"] for number in body["cited"])
    assert any(source["entity"] == "AuthService.login" for source in body["sources"])
