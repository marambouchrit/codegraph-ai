"""Optional: import -> analyze -> chat over HTTP, everything real. Skipped by a normal `pytest` run.

    docker compose up -d neo4j qdrant
    pytest -m "neo4j and qdrant and embeddings and llm"      (needs LLM_API_KEY)

The project is uploaded as a ZIP, analyzed by POST /projects/{id}/analyze (real Neo4j,
BGE-M3, Qdrant), then questioned by POST /projects/{id}/chat (real LLM). No README
snippet: the API alone prepares the project. Vectors go to a temporary Qdrant
collection; the graph and the collection are removed afterwards.
"""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from app.api.dependencies import get_analysis_service, get_chat_service, get_project_service
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
from app.services.project_service import ProjectService
from app.services.vector_index_service import VectorIndexService
from app.services.vector_retrieval_service import VectorRetrievalService
from tests.conftest import MakeZip
from tests.test_graphrag import FILES

pytestmark = [pytest.mark.neo4j, pytest.mark.qdrant, pytest.mark.embeddings, pytest.mark.llm]


class Servers:
    def __init__(self, http: TestClient, repository: GraphRepository, store: QdrantVectorStore):
        self.http = http
        self.repository = repository
        self.store = store
        self.project_ids: list[str] = []
        self.embedding_model = ""


@pytest.fixture
def servers(settings: Settings) -> Iterator[Servers]:
    try:
        generator = LLMGenerationService(create_llm_provider(settings))
    except LLMConfigurationError as error:
        pytest.skip(error.message)
    projects = ProjectService(settings)
    qdrant = QdrantClient(url=settings.qdrant_url, timeout=30)
    store = QdrantVectorStore(qdrant, f"test_{uuid.uuid4().hex[:12]}", settings.qdrant_url)
    embeddings = embedding_provider_from_settings(settings)
    with Neo4jClient.from_settings(settings) as neo4j:
        repository = GraphRepository(neo4j)
        graphrag = GraphRAGService(VectorRetrievalService(store, embeddings, settings),
                                   GraphRetrievalService(repository), settings)  # fmt: skip
        app.dependency_overrides[get_project_service] = lambda: projects
        app.dependency_overrides[get_analysis_service] = lambda: AnalysisService(
            projects,
            lambda: GraphService(settings, repository, projects),
            lambda: VectorIndexService(settings, store, embeddings, projects),
        )
        app.dependency_overrides[get_chat_service] = lambda: ChatService(
            projects, lambda: graphrag, lambda: generator
        )
        servers = Servers(TestClient(app), repository, store)
        servers.embedding_model = embeddings.model_name
        try:
            yield servers
        finally:
            app.dependency_overrides.clear()
            for project_id in servers.project_ids:
                repository.delete_project(project_id)
            qdrant.delete_collection(store.collection)


def test_import_analyze_and_chat_over_http(servers: Servers, make_zip: MakeZip) -> None:
    http = servers.http

    # 1. Import: the source code is stored, nothing is analyzed yet.
    imported = http.post("/projects/zip",
                         files={"file": ("auth.zip", make_zip(FILES), "application/zip")})  # fmt: skip
    assert imported.status_code == 201, imported.text
    project_id = imported.json()["id"]
    servers.project_ids.append(project_id)
    assert not servers.repository.project_exists(project_id)

    # 2. Analyze: the graph is in Neo4j, the vectors in Qdrant.
    analyzed = http.post(f"/projects/{project_id}/analyze")
    assert analyzed.status_code == 200, analyzed.text
    report = analyzed.json()
    assert report["status"] == "ready" and report["failed_files"] == 0
    assert report["graph"]["entities"] > 0 and report["graph"]["relationships"] > 0
    assert report["vectors"]["chunks"] == servers.store.count(project_id) > 0
    assert report["vectors"]["embedding_model"] == servers.embedding_model  # EMBEDDING_MODEL
    assert servers.repository.project_exists(project_id)

    # Analyzing again changes nothing: no duplicate nodes or points.
    again = http.post(f"/projects/{project_id}/analyze").json()
    assert again["graph"]["entities"] == report["graph"]["entities"]
    assert again["graph"]["stale_entities_removed"] == 0
    assert servers.store.count(project_id) == report["vectors"]["chunks"]

    # 3. Chat: a grounded, cited answer from the real LLM.
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
