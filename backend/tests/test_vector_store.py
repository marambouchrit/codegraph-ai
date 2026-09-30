"""QdrantVectorStore on qdrant-client's in-memory Qdrant: collections, points, filters."""

import uuid
from typing import Any

import httpx
import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse

from app.core.errors import (
    VectorCollectionError,
    VectorStoreError,
    VectorStoreUnavailableError,
)
from app.rag.models import CodeChunk
from app.rag.vector_store import QdrantVectorStore, point_id
from tests.vector_helpers import HashingEmbeddings, memory_store

PROJECT_A = "a" * 32
PROJECT_B = "b" * 32
MODEL = "test/hashing"
MALICIOUS = "x\" OR project_id != '' //"


def make_chunk(project_id: str, name: str, text: str, **overrides: Any) -> CodeChunk:
    entity_id = f"{project_id}:src/{name}.py:{name}"
    values: dict[str, Any] = {
        "id": f"{entity_id}|1", "project_id": project_id, "file_path": f"src/{name}.py",
        "language": "python", "entity_id": entity_id, "entity_type": "function",
        "name": name, "qualified_name": name, "start_line": 1, "end_line": 3, "text": text,
    }  # fmt: skip
    values.update(overrides)
    return CodeChunk(**values)


def index(
    store: QdrantVectorStore,
    chunks: list[CodeChunk],
    embeddings: HashingEmbeddings,
    index_id: str = "index-1",
    model: str = MODEL,
) -> int:
    store.ensure_collection(embeddings.dimension)
    vectors = embeddings.embed_documents([c.embedding_text for c in chunks])
    return store.upsert(chunks, vectors, index_id=index_id, embedding_model=model)


def search(store: QdrantVectorStore, embeddings: HashingEmbeddings, project_id: str,
           query: str, limit: int = 10, **filters: Any) -> list[str]:  # fmt: skip
    vector = embeddings.embed_query(query)
    results = store.search(project_id, vector, embedding_model=MODEL, limit=limit, **filters)
    return [r.chunk.name for r in results]


AUTH = [
    ("login", "def login(username, password): check the password and create a token"),
    ("create_token", "def create_token(user): sign a jwt token for the user"),
    ("render_chart", "def render_chart(values): draw a bar chart of sales"),
]


@pytest.fixture
def embeddings() -> HashingEmbeddings:
    return HashingEmbeddings()


@pytest.fixture
def store() -> QdrantVectorStore:
    return memory_store()


# ----- Collection -----


def test_collection_is_created_once_with_the_model_dimension(store: QdrantVectorStore) -> None:
    assert not store.collection_exists()

    store.ensure_collection(384)
    store.ensure_collection(384)  # already there, same dimension: nothing to do

    info = store.collection_info()
    assert (info.name, info.dimension, info.distance, info.points) == ("test_chunks", 384, "Cosine", 0)


def test_a_collection_of_another_dimension_is_refused(store: QdrantVectorStore) -> None:
    store.ensure_collection(384)

    with pytest.raises(VectorCollectionError) as error:
        store.ensure_collection(1024)
    assert "384" in error.value.message and "QDRANT_COLLECTION" in error.value.message


@pytest.mark.parametrize("name", ["", "a b", "../x", "x" * 65, "chunks;drop", None])
def test_collection_names_are_validated(name: Any) -> None:
    with pytest.raises(VectorCollectionError):
        QdrantVectorStore(QdrantClient(":memory:"), name)


# ----- Writing -----


def test_upsert_and_search(store: QdrantVectorStore, embeddings: HashingEmbeddings) -> None:
    chunks = [make_chunk(PROJECT_A, name, text) for name, text in AUTH]

    assert index(store, chunks, embeddings) == 3
    assert store.count(PROJECT_A) == 3
    assert search(store, embeddings, PROJECT_A, "check the user password", limit=1) == ["login"]
    assert search(store, embeddings, PROJECT_A, "jwt token")[0] == "create_token"


def test_results_carry_the_whole_chunk_and_a_score(
    store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    login = make_chunk(PROJECT_A, "login", AUTH[0][1], start_line=10, end_line=24)
    index(store, [login], embeddings)

    [hit] = store.search(PROJECT_A, embeddings.embed_query("password"), embedding_model=MODEL, limit=5)

    assert hit.chunk == login
    assert 0 < hit.score <= 1.0


def test_same_chunk_twice_is_one_point(store: QdrantVectorStore, embeddings: HashingEmbeddings) -> None:
    chunks = [make_chunk(PROJECT_A, name, text) for name, text in AUTH]

    for index_id in ("index-1", "index-2", "index-3"):
        index(store, chunks, embeddings, index_id=index_id)

    assert store.count(PROJECT_A) == 3


def test_point_ids_are_deterministic_uuids() -> None:
    chunk_id = f"{PROJECT_A}:src/a.py:login|1"

    assert point_id(chunk_id) == point_id(chunk_id)
    assert point_id(chunk_id) != point_id(chunk_id.replace("|1", "|2"))
    assert uuid.UUID(point_id(chunk_id)).version == 5


def test_delete_stale_keeps_the_current_indexing(
    store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    login, token, chart = (make_chunk(PROJECT_A, name, text) for name, text in AUTH)
    other_project = make_chunk(PROJECT_B, "chart", AUTH[2][1])
    index(store, [login, token, chart], embeddings, index_id="old")
    index(store, [other_project], embeddings, index_id="old")
    index(store, [login, token], embeddings, index_id="new")  # chart was deleted since

    assert store.delete_stale(PROJECT_A, "new") == 1
    assert search(store, embeddings, PROJECT_A, "sales chart") != ["render_chart"]
    assert store.count(PROJECT_A) == 2
    assert store.count(PROJECT_B) == 1  # another project's "old" points are not stale


def test_delete_project_only_touches_that_project(
    store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    index(store, [make_chunk(PROJECT_A, n, t) for n, t in AUTH], embeddings)
    index(store, [make_chunk(PROJECT_B, n, t) for n, t in AUTH], embeddings)

    assert store.delete_project(PROJECT_A) == 3
    assert store.count(PROJECT_A) == 0 and store.count(PROJECT_B) == 3


def test_operations_on_a_missing_collection_are_harmless(store: QdrantVectorStore) -> None:
    assert store.count(PROJECT_A) == 0
    assert store.delete_project(PROJECT_A) == 0
    assert store.delete_stale(PROJECT_A, "x") == 0
    assert store.search(PROJECT_A, [1.0, 0.0], embedding_model=MODEL, limit=3) == []


def test_upsert_needs_one_vector_per_chunk(store: QdrantVectorStore) -> None:
    store.ensure_collection(4)
    with pytest.raises(ValueError):
        store.upsert([make_chunk(PROJECT_A, "a", "x")], [], index_id="1", embedding_model=MODEL)
    assert store.upsert([], [], index_id="1", embedding_model=MODEL) == 0


# ----- Search filters and isolation -----


def test_search_never_leaves_the_project(store: QdrantVectorStore, embeddings: HashingEmbeddings) -> None:
    index(store, [make_chunk(PROJECT_A, "login", AUTH[0][1])], embeddings)
    index(store, [make_chunk(PROJECT_B, name, text) for name, text in AUTH], embeddings)

    vector = embeddings.embed_query("password token")
    results = store.search(PROJECT_A, vector, embedding_model=MODEL, limit=50)

    assert [r.chunk.project_id for r in results] == [PROJECT_A]
    assert store.search("c" * 32, vector, embedding_model=MODEL, limit=50) == []


def test_a_filter_can_never_cover_every_project(store: QdrantVectorStore) -> None:
    store.ensure_collection(4)
    for project_id in ("", None):
        with pytest.raises(ValueError):
            store.search(project_id, [1.0, 0, 0, 0], embedding_model=MODEL, limit=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            store.delete_project(project_id)  # type: ignore[arg-type]


def test_only_vectors_of_the_same_model_are_compared(
    store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    index(store, [make_chunk(PROJECT_A, "login", AUTH[0][1])], embeddings, model="other/model")

    assert search(store, embeddings, PROJECT_A, "password") == []


def test_language_and_entity_type_filters(store: QdrantVectorStore, embeddings: HashingEmbeddings) -> None:
    chunks = [
        make_chunk(PROJECT_A, "login", "login password", language="python", entity_type="method"),
        make_chunk(PROJECT_A, "signIn", "login password", language="typescript"),
        make_chunk(PROJECT_A, "Auth", "login password", language="java", entity_type="class"),
    ]
    index(store, chunks, embeddings)

    assert search(store, embeddings, PROJECT_A, "login", languages=["typescript"]) == ["signIn"]
    assert sorted(search(store, embeddings, PROJECT_A, "login", entity_types=["method", "class"])) == [
        "Auth", "login",
    ]  # fmt: skip
    assert search(store, embeddings, PROJECT_A, "login", languages=["java"], entity_types=["method"]) == []


def test_limit_and_score_threshold(store: QdrantVectorStore, embeddings: HashingEmbeddings) -> None:
    index(store, [make_chunk(PROJECT_A, n, t) for n, t in AUTH], embeddings)
    vector = embeddings.embed_query("password token user")

    assert len(store.search(PROJECT_A, vector, embedding_model=MODEL, limit=2)) == 2
    everything = store.search(PROJECT_A, vector, embedding_model=MODEL, limit=10)
    cut = (everything[0].score + everything[-1].score) / 2
    kept = store.search(PROJECT_A, vector, embedding_model=MODEL, limit=10, score_threshold=cut)
    assert 0 < len(kept) < len(everything)
    assert all(r.score >= cut for r in kept)


def test_equal_scores_are_ordered_by_chunk_id(
    store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    # Same metadata and text, so the same vector and score; only the chunk IDs differ.
    twins = [make_chunk(PROJECT_A, "x", "same words", id=f"{PROJECT_A}:src/x.py:x|{part}")
             for part in (3, 1, 2)]  # fmt: skip
    index(store, twins, embeddings)

    results = store.search(PROJECT_A, embeddings.embed_query("same words"), embedding_model=MODEL,
                           limit=10)  # fmt: skip
    assert len({r.score for r in results}) == 1
    assert [r.chunk.id[-1] for r in results] == ["1", "2", "3"]


def test_malicious_values_are_only_data(store: QdrantVectorStore, embeddings: HashingEmbeddings) -> None:
    evil = make_chunk(PROJECT_A, "evil", MALICIOUS, file_path=MALICIOUS, qualified_name=MALICIOUS)
    index(store, [evil, make_chunk(PROJECT_B, "login", AUTH[0][1])], embeddings)

    vector = embeddings.embed_query(MALICIOUS)
    [hit] = store.search(PROJECT_A, vector, embedding_model=MODEL, limit=10)
    assert hit.chunk.file_path == MALICIOUS
    assert store.search(MALICIOUS, vector, embedding_model=MODEL, limit=10) == []
    assert store.search(PROJECT_A, vector, embedding_model=MODEL, limit=10,
                        languages=[MALICIOUS]) == []  # fmt: skip
    assert store.count(PROJECT_B) == 1


# ----- Errors -----


def test_qdrant_down_is_translated() -> None:
    # Nothing listens there. check_compatibility=False: no version check at construction.
    client = QdrantClient(url="http://127.0.0.1:9", timeout=1, check_compatibility=False)
    store = QdrantVectorStore(client, "chunks", "http://127.0.0.1:9")

    with pytest.raises(VectorStoreUnavailableError) as error:
        store.verify_connectivity()
    assert "docker compose up -d qdrant" in error.value.message
    assert error.value.status_code == 503


class RejectingClient:
    """Answers every call with the given HTTP status, like a Qdrant server would."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __getattr__(self, _name: str) -> Any:
        def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise UnexpectedResponse(self.status, "error", b"{}", httpx.Headers())

        return fail


@pytest.mark.parametrize(
    ("status", "error_type", "text"),
    [
        (401, VectorStoreUnavailableError, "QDRANT_API_KEY"),
        (403, VectorStoreUnavailableError, "QDRANT_API_KEY"),
        (404, VectorCollectionError, "does not exist"),
        (500, VectorStoreError, "A Qdrant operation failed."),
    ],
)
def test_http_errors_are_translated(status: int, error_type: type, text: str) -> None:
    store = QdrantVectorStore(RejectingClient(status), "chunks")  # type: ignore[arg-type]

    with pytest.raises(error_type) as error:
        store.verify_connectivity()
    assert text in error.value.message


@pytest.mark.filterwarnings("ignore::UserWarning")  # http + API key, no server to check
def test_api_key_never_appears_in_the_store_description() -> None:
    from app.core.config import Settings
    from pydantic import SecretStr

    settings = Settings(qdrant_url="http://user:pw@localhost:6333",
                        qdrant_api_key=SecretStr("top-secret-key"))  # fmt: skip
    store = QdrantVectorStore.from_settings(settings)

    assert "top-secret-key" not in repr(store) and "pw" not in repr(store)
    assert store.location == "http://localhost:6333"
    store.close()
