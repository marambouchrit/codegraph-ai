"""Optional: GraphRAG on a real Neo4j and a real Qdrant. Skipped by a normal `pytest` run.

    docker compose up -d neo4j qdrant
    pytest -m "neo4j and qdrant"

Random project IDs and a temporary Qdrant collection, removed afterwards. The test
embedding (tests/vector_helpers.py) is used, so no model is downloaded. The same
context must come out of the real servers and of the in-memory fakes.
"""

import uuid
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient

from app.core.config import Settings
from app.graph.builder import GraphBuilder
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.graphrag.models import GraphStatus
from app.rag.vector_store import QdrantVectorStore
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graphrag_service import GraphRAGService
from app.services.vector_retrieval_service import VectorRetrievalService
from tests.relationship_helpers import analyze
from tests.test_graphrag import FILES, QUESTION, World, project_chunks
from tests.vector_helpers import HashingEmbeddings

pytestmark = [pytest.mark.neo4j, pytest.mark.qdrant]


@pytest.fixture
def servers() -> Iterator[tuple[GraphRepository, QdrantVectorStore, str]]:
    settings = Settings()
    project_id = uuid.uuid4().hex
    api_key = settings.qdrant_api_key.get_secret_value() or None
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=api_key, timeout=10)
    store = QdrantVectorStore(qdrant, f"test_{uuid.uuid4().hex[:12]}", settings.qdrant_url)
    with Neo4jClient.from_settings(settings) as client:
        repository = GraphRepository(client)
        yield repository, store, project_id
        repository.delete_project(project_id)
    qdrant.delete_collection(store.collection)
    store.close()


def test_graphrag_on_real_servers_matches_the_fakes(
    servers: tuple[GraphRepository, QdrantVectorStore, str],
) -> None:
    repository, store, project_id = servers
    embeddings = HashingEmbeddings()
    GraphBuilder(repository).build(analyze(FILES, project_id))
    store.ensure_collection(embeddings.dimension)
    chunks = project_chunks(project_id)
    store.upsert(chunks, embeddings.embed_documents([c.embedding_text for c in chunks]),
                 index_id="1", embedding_model=embeddings.model_name)  # fmt: skip
    # Every chunk (top_k 30 > ~20 chunks): with the test embedding, several chunks tie at
    # low scores, and which tied chunks make a shorter list is up to each engine.
    settings = Settings(graphrag_vector_top_k=30)
    real = GraphRAGService(
        VectorRetrievalService(store, embeddings, settings), GraphRetrievalService(repository),
        settings,
    ).build_context(project_id, QUESTION)

    fake_world = World.for_project(project_id)
    fake = GraphRAGService(VectorRetrievalService(fake_world.store, embeddings, settings),
                           fake_world.graph, settings).build_context(project_id, QUESTION)

    assert real.graph_status == GraphStatus.COMPLETE and real.relationships
    assert [h.chunk.id for h in real.vector_results] == [h.chunk.id for h in fake.vector_results]
    assert [s.entity_id for s in real.seeds] == [s.entity_id for s in fake.seeds]
    assert real.entities == fake.entities
    assert real.relationships == fake.relationships
    assert real.paths == fake.paths
    assert [(s.location, s.entity_id, s.reason) for s in real.sources] == [
        (s.location, s.entity_id, s.reason) for s in fake.sources
    ]
