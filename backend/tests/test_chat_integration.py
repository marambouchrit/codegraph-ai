"""Optional: the chat API end to end, everything real. Skipped by a normal `pytest` run.

    docker compose up -d neo4j qdrant
    pytest -m "neo4j and qdrant and embeddings and llm"      (needs LLM_API_KEY)

HTTP -> ChatService -> GraphRAGService (real Neo4j, Qdrant, embedding model) ->
LLMGenerationService (real LLM). A project is imported from a ZIP, analyzed into Neo4j
and indexed into a temporary Qdrant collection; both are removed afterwards.
"""

import io
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from app.api.dependencies import get_chat_service, get_project_service
from app.core.config import Settings
from app.core.errors import LLMConfigurationError
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.llm.generator import LLMGenerationService
from app.llm.provider import create_llm_provider
from app.main import app
from app.rag.embeddings import embedding_provider_from_settings
from app.rag.vector_store import QdrantVectorStore
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


@pytest.fixture
def client(settings: Settings, make_zip: MakeZip) -> Iterator[tuple[TestClient, str]]:
    try:
        generator = LLMGenerationService(create_llm_provider(settings))
    except LLMConfigurationError as error:
        pytest.skip(error.message)
    projects = ProjectService(settings)
    project = projects.create_from_zip(io.BytesIO(make_zip(FILES)), "auth.zip")
    qdrant = QdrantClient(url=settings.qdrant_url, timeout=30)
    store = QdrantVectorStore(qdrant, f"test_{uuid.uuid4().hex[:12]}", settings.qdrant_url)
    embeddings = embedding_provider_from_settings(settings)
    with Neo4jClient.from_settings(settings) as neo4j:
        repository = GraphRepository(neo4j)
        try:
            GraphService(settings, repository, projects).build_project_graph(project.id)
            VectorIndexService(settings, store, embeddings, projects).index_project(project.id)
            graphrag = GraphRAGService(VectorRetrievalService(store, embeddings, settings),
                                       GraphRetrievalService(repository), settings)  # fmt: skip
            app.dependency_overrides[get_project_service] = lambda: projects
            app.dependency_overrides[get_chat_service] = lambda: ChatService(
                projects, lambda: graphrag, lambda: generator
            )
            yield TestClient(app), project.id
        finally:
            app.dependency_overrides.clear()
            repository.delete_project(project.id)
            qdrant.delete_collection(store.collection)


def test_chat_over_http_with_every_real_service(client: tuple[TestClient, str]) -> None:
    http, project_id = client

    response = http.post(f"/projects/{project_id}/chat",
                         json={"question": "How is authentication implemented?"})  # fmt: skip

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["graph_status"] == "complete" and body["model"]
    assert "login" in body["answer"] and body["cited"]
    by_id = {source["id"]: source for source in body["sources"]}
    assert all(number in by_id and by_id[number]["cited"] for number in body["cited"])
    login = next(s for s in body["sources"] if s["entity"] == "AuthService.login")
    # Paths are relative to the project: the ZIP's single root folder ("app/") is stripped.
    assert (login["file"], login["start_line"], login["end_line"]) == ("auth/service.py", 14, 20)
