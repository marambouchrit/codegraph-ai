"""Optional tests with the real pieces. Skipped by a normal `pytest` run.

    pytest -m qdrant        a real Qdrant server (docker compose up -d qdrant), settings
                            from backend/.env; uses a temporary collection, deleted after
    pytest -m embeddings    the real embedding model (EMBEDDING_MODEL, downloaded once to
                            the Hugging Face cache), with an in-memory Qdrant

The `embeddings` end-to-end test checks semantic search without any shared words:
"How does the application authenticate users?" must find the login code.
"""

import io
import math
import uuid
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient

from app.core.config import Settings
from app.rag.embeddings import SentenceTransformerEmbeddings, embedding_provider_from_settings
from app.rag.vector_store import QdrantVectorStore
from app.services.project_service import ProjectService
from app.services.vector_index_service import VectorIndexService
from app.services.vector_retrieval_service import VectorRetrievalService
from tests.conftest import MakeZip
from tests.vector_helpers import AUTH_PROJECT, HashingEmbeddings, memory_store


def import_project(settings: Settings, make_zip: MakeZip) -> str:
    archive = io.BytesIO(make_zip(AUTH_PROJECT))
    return ProjectService(settings).create_from_zip(archive, "auth.zip").id


# ----- Real Qdrant server -----


@pytest.fixture
def qdrant_store() -> Iterator[QdrantVectorStore]:
    settings = Settings()
    api_key = settings.qdrant_api_key.get_secret_value() or None
    client = QdrantClient(url=settings.qdrant_url, api_key=api_key, timeout=10)
    store = QdrantVectorStore(client, f"test_{uuid.uuid4().hex[:12]}", settings.qdrant_url)
    store.verify_connectivity()
    yield store
    client.delete_collection(store.collection)
    store.close()


@pytest.mark.qdrant
def test_index_search_reindex_and_isolation_on_a_real_server(
    settings: Settings, make_zip: MakeZip, qdrant_store: QdrantVectorStore
) -> None:
    embeddings = HashingEmbeddings()
    first, second = import_project(settings, make_zip), import_project(settings, make_zip)
    indexer = VectorIndexService(settings, qdrant_store, embeddings)
    retrieval = VectorRetrievalService(qdrant_store, embeddings, Settings())

    report = indexer.index_project(first)
    again = indexer.index_project(first)
    indexer.index_project(second)

    assert again.chunks == report.chunks and again.stale_chunks_deleted == 0
    assert qdrant_store.count(first) == report.chunks  # re-indexing did not duplicate
    info = qdrant_store.collection_info()
    assert (info.dimension, info.distance) == (embeddings.dimension, "Cosine")

    hits = retrieval.retrieve(first, "check the password of the user, invalid credentials", top_k=5)
    assert hits[0].chunk.qualified_name == "AuthService.login"
    assert {hit.chunk.project_id for hit in retrieval.retrieve(first, "user", top_k=50)} == {first}
    assert [h.chunk.language for h in retrieval.retrieve(first, "cart", languages="typescript")]

    assert indexer.delete_project_index(first) == report.chunks
    assert retrieval.retrieve(first, "password") == []
    assert qdrant_store.count(second) == report.chunks


@pytest.mark.qdrant
def test_the_same_answers_as_the_in_memory_qdrant(
    settings: Settings, make_zip: MakeZip, qdrant_store: QdrantVectorStore
) -> None:
    """The unit tests run on qdrant-client's local mode: the server must agree with it."""
    embeddings = HashingEmbeddings()
    project_id = import_project(settings, make_zip)
    local = memory_store()
    for store in (qdrant_store, local):
        VectorIndexService(settings, store, embeddings).index_project(project_id)

    # Every chunk (top_k=50): with the test embedding, many chunks tie at score 0, and
    # which tied chunks make a shorter list is up to each engine.
    for query in ["password token", "sales chart", "user database", "cart total price"]:
        real = VectorRetrievalService(qdrant_store, embeddings).retrieve(project_id, query, top_k=50)
        fake = VectorRetrievalService(local, embeddings).retrieve(project_id, query, top_k=50)
        assert [h.chunk for h in real] == [h.chunk for h in fake], query
        assert [round(h.score, 4) for h in real] == [round(h.score, 4) for h in fake], query


# ----- Real embedding model -----


# Dimensions published on the model cards, to catch a model that is not the one expected.
KNOWN_DIMENSIONS = {"BAAI/bge-m3": 1024, "BAAI/bge-small-en-v1.5": 384}


@pytest.fixture(scope="module")
def model() -> SentenceTransformerEmbeddings:
    provider = embedding_provider_from_settings(Settings())
    assert isinstance(provider, SentenceTransformerEmbeddings)
    return provider


@pytest.mark.embeddings
def test_real_model_dimension_and_normalized_vectors(model: SentenceTransformerEmbeddings) -> None:
    [document] = model.embed_documents(["def login(username, password): ..."])
    query = model.embed_query("where do users log in?")

    assert model.model_name == Settings().embedding_model
    assert len(document) == len(query) == model.dimension
    if model.model_name in KNOWN_DIMENSIONS:
        assert model.dimension == KNOWN_DIMENSIONS[model.model_name]
    assert all(math.isfinite(v) for v in document + query)
    assert abs(sum(v * v for v in document) - 1.0) < 1e-3
    assert abs(sum(v * v for v in query) - 1.0) < 1e-3
    assert model.embed_documents(["def login(username, password): ..."])[0] == pytest.approx(document)


@pytest.mark.embeddings
def test_real_model_batches_give_the_same_vectors(model: SentenceTransformerEmbeddings) -> None:
    texts = [
        "def login(username, password): ...",
        "class Database:\n    def query(self, table, key): ...",
        "export function cartTotal(items: Item[]): number { return 0 }",
    ]
    batch = model.embed_documents(texts)

    assert len(batch) == len(texts) and model.embed_documents([]) == []
    for text, vector in zip(texts, batch, strict=True):
        # Padding in a batch changes the last float digits only.
        assert model.embed_documents([text])[0] == pytest.approx(vector, abs=1e-3)


@pytest.mark.embeddings
def test_semantic_search_end_to_end(
    settings: Settings, make_zip: MakeZip, model: SentenceTransformerEmbeddings
) -> None:
    project_id = import_project(settings, make_zip)
    store = memory_store()
    VectorIndexService(settings, store, model).index_project(project_id)
    retrieval = VectorRetrievalService(store, model)

    auth = retrieval.retrieve(project_id, "How does the application authenticate users?", top_k=3)
    token = retrieval.retrieve(project_id, "Where is the JWT created?", top_k=3)
    shopping = retrieval.retrieve(project_id, "How is the price of an order computed?", top_k=3)

    assert {hit.chunk.file_path for hit in auth[:2]} == {"app/auth/service.py"}
    assert "AuthService.create_token" in {hit.chunk.qualified_name for hit in token}
    assert shopping[0].chunk.file_path == "web/src/cart.ts"
    unrelated = retrieval.retrieve(project_id, "How is the price of an order computed?", top_k=50)
    chart_score = next(h.score for h in unrelated if h.chunk.name == "render_bar_chart")
    assert shopping[0].score > chart_score
