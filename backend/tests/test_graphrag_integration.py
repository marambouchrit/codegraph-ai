"""Optional: GraphRAG on a real Neo4j and a real Qdrant. Skipped by a normal `pytest` run.

    docker compose up -d neo4j qdrant
    pytest -m "neo4j and qdrant"

Random project IDs and a temporary Qdrant collection, removed afterwards. The first
test uses the test embedding (tests/vector_helpers.py), so no model is downloaded: the
same context must come out of the real servers and of the in-memory fakes.

    pytest -m "neo4j and qdrant and embeddings"

runs the real embedding model (EMBEDDING_MODEL, BGE-M3 by default) end to end: real
questions must find the right code, and GraphRAG must connect it through the graph.
"""

import uuid
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient

from app.core.config import Settings
from app.graph.builder import GraphBuilder
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.graphrag.models import EntityRole, GraphStatus
from app.rag.embeddings import embedding_provider_from_settings
from app.rag.vector_store import QdrantVectorStore
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graphrag_service import GraphRAGService
from app.services.vector_retrieval_service import VectorRetrievalService
from tests.relationship_helpers import analyze
from tests.test_graphrag import (
    CREATE_TOKEN,
    FILES,
    FIND_USER,
    LOGIN,
    QUERY,
    QUESTION,
    World,
    full,
    project_chunks,
    short,
)
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


# ----- The real embedding model -----

# Each question, and the entities that answer it (one of them in the top 3 vector hits).
REAL_QUESTIONS = {
    "How is authentication implemented?": {LOGIN},
    "Where is the user retrieved during login?": {FIND_USER, LOGIN},
    "Which function creates the authentication token?": {CREATE_TOKEN},
    "How does the application query the database?": {QUERY, FIND_USER},
}


@pytest.mark.embeddings
def test_graphrag_with_the_real_model(
    servers: tuple[GraphRepository, QdrantVectorStore, str],
) -> None:
    repository, store, project_id = servers
    settings = Settings()
    embeddings = embedding_provider_from_settings(settings)
    GraphBuilder(repository).build(analyze(FILES, project_id))
    store.ensure_collection(embeddings.dimension)
    chunks = project_chunks(project_id)
    store.upsert(chunks, embeddings.embed_documents([c.embedding_text for c in chunks]),
                 index_id="1", embedding_model=embeddings.model_name)  # fmt: skip
    vector = VectorRetrievalService(store, embeddings, settings)
    graphrag = GraphRAGService(vector, GraphRetrievalService(repository), settings)
    by_id = {chunk.id: chunk for chunk in chunks}

    for question, expected in REAL_QUESTIONS.items():
        hits = vector.retrieve(project_id, question, top_k=3)
        assert {short(h.chunk.entity_id) for h in hits} & expected, question
        for hit in hits:  # payload intact: same chunk, same source location
            assert hit.chunk == by_id[hit.chunk.id]

        context = graphrag.build_context(project_id, question)
        assert context.graph_status == GraphStatus.COMPLETE, question
        seeds = [seed for seed in context.seeds if seed.in_graph]
        assert seeds and all(seed.entity_id.startswith(f"{project_id}:") for seed in seeds)
        assert all(source.entity_id.startswith(f"{project_id}:") for source in context.sources)

    # The flagship question: login is found by meaning, its calls are added by the graph.
    context = graphrag.build_context(project_id, "How is authentication implemented?")
    roles = {short(e.entity.id): e.role for e in context.entities}
    assert roles[LOGIN] == EntityRole.SEED
    calls = {(short(r.relationship.source_id), short(r.relationship.target_id))
             for r in context.relationships if r.relationship.type == "CALLS"}  # fmt: skip
    assert {(LOGIN, FIND_USER), (LOGIN, CREATE_TOKEN)} <= calls
    assert full(project_id, FIND_USER) in {e.entity.id for e in context.entities}
