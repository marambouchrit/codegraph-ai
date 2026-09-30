"""VectorIndexService and VectorRetrievalService: an imported project, end to end.

Real Phases 2-4 (ZIP import, parsing, entities), real chunker and QdrantVectorStore
(in-memory Qdrant), and the test hashing embeddings instead of a downloaded model.
"""

import io
from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import (
    EmbeddingModelError,
    InvalidVectorQueryError,
    ProjectNotFoundError,
    VectorCollectionError,
    VectorStoreUnavailableError,
)
from app.ingestion.languages import Language
from app.ingestion.scanner import ScannedFile
from app.rag.vector_store import QdrantVectorStore
from app.schemas.project import Project
from app.services.project_service import ProjectService
from app.services.vector_index_service import VectorIndexService
from app.services.vector_retrieval_service import VectorRetrievalService
from tests.conftest import MakeZip
from tests.vector_helpers import AUTH_PROJECT, FailingEmbeddings, HashingEmbeddings, memory_store

MALICIOUS = "') OR 1=1; DROP COLLECTION codegraph_chunks; --"


def import_project(settings: Settings, make_zip: MakeZip, files: dict[str, Any]) -> Project:
    return ProjectService(settings).create_from_zip(io.BytesIO(make_zip(files)), "shop.zip")


def rebuild_zip_project(settings: Settings, project: Project, files: dict[str, str]) -> None:
    """Replace the project's source files on disk, as if the repository had changed."""
    source_dir = ProjectService(settings).workspace.source_dir(project.id)
    for path in source_dir.rglob("*"):
        if path.is_file():
            path.unlink()
    for path, content in files.items():
        target = source_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def store() -> QdrantVectorStore:
    return memory_store()


@pytest.fixture
def embeddings() -> HashingEmbeddings:
    return HashingEmbeddings()


def indexer(settings: Settings, store: QdrantVectorStore, embeddings: Any) -> VectorIndexService:
    return VectorIndexService(settings, store, embeddings)


def retriever(store: QdrantVectorStore, embeddings: Any, **overrides: Any) -> VectorRetrievalService:
    return VectorRetrievalService(store, embeddings, Settings(**overrides))


# ----- Indexing -----


def test_index_a_whole_project(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    project = import_project(settings, make_zip, AUTH_PROJECT)

    report = indexer(settings, store, embeddings).index_project(project.id)

    assert report.project_id == project.id
    assert report.files == 4 and report.failed_files == 0
    # One file chunk only: service.py has module code (imports, SECRET_KEY); the other
    # files only hold definitions, each in its own chunk.
    assert report.chunks_by_type == {"class": 3, "file": 1, "function": 3, "interface": 1, "method": 8}
    assert report.chunks == sum(report.chunks_by_type.values()) == store.count(project.id)
    assert (report.embedding_model, report.dimension) == ("test/hashing", 256)
    assert report.stale_chunks_deleted == 0
    assert report.summary.startswith("Vector index built successfully: 4 files, 16 chunks")
    assert embeddings.documents_embedded == 16


def test_search_finds_the_relevant_code(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    project = import_project(settings, make_zip, AUTH_PROJECT)
    indexer(settings, store, embeddings).index_project(project.id)
    retrieval = retriever(store, embeddings)

    hits = retrieval.retrieve(project.id, "check the password of the user, invalid credentials", top_k=2)
    chart = retrieval.retrieve(project.id, "bar chart of monthly sales", top_k=1)

    assert hits[0].chunk.qualified_name == "AuthService.login"
    assert {h.chunk.file_path for h in hits} == {"app/auth/service.py"}
    assert chart[0].chunk.qualified_name == "render_bar_chart"
    login = hits[0].chunk
    assert (login.entity_type, login.language, login.start_line, login.end_line) == (
        "method", "python", 15, 20,
    )  # fmt: skip
    assert login.entity_id == f"{project.id}:app/auth/service.py:AuthService.login"
    assert "verify_password(password" in login.text


def test_reindexing_is_idempotent(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    project = import_project(settings, make_zip, AUTH_PROJECT)
    service = indexer(settings, store, embeddings)

    first = service.index_project(project.id)
    second = service.index_project(project.id)

    assert second.chunks == first.chunks
    assert second.stale_chunks_deleted == 0
    assert second.index_id != first.index_id
    assert store.count(project.id) == first.chunks  # overwritten, not duplicated


def test_deleted_and_renamed_code_is_removed_on_reindexing(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    project = import_project(settings, make_zip, AUTH_PROJECT)
    service = indexer(settings, store, embeddings)
    service.index_project(project.id)

    remaining = {path: code for path, code in AUTH_PROJECT.items() if "reports" not in path}
    remaining["app/db/database.py"] = remaining["app/db/database.py"].replace("find_user", "get_user")
    rebuild_zip_project(settings, project, remaining)
    report = service.index_project(project.id)

    # charts.py: its 2 functions (only definitions, so no file chunk); database.py:
    # find_user renamed to get_user (the Database skeleton keeps its ID: overwritten).
    assert report.stale_chunks_deleted == 3
    assert store.count(project.id) == report.chunks
    names = {h.chunk.qualified_name
             for h in retriever(store, embeddings).retrieve(project.id, "chart user", top_k=50)}
    assert "render_bar_chart" not in names and "Database.find_user" not in names
    assert "Database.get_user" in names


def test_projects_are_isolated(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    first = import_project(settings, make_zip, AUTH_PROJECT)
    second = import_project(settings, make_zip, AUTH_PROJECT)
    service = indexer(settings, store, embeddings)
    service.index_project(first.id)
    service.index_project(second.id)
    retrieval = retriever(store, embeddings)

    hits = retrieval.retrieve(first.id, "password token login", top_k=50)
    assert hits and {h.chunk.project_id for h in hits} == {first.id}

    deleted = service.delete_project_index(first.id)
    assert deleted > 0 and service.count_chunks(first.id) == 0
    assert service.count_chunks(second.id) == deleted  # same code, untouched
    assert retrieval.retrieve(first.id, "password token login") == []


def test_unknown_project_is_rejected_before_qdrant_and_the_model(
    settings: Settings, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    with pytest.raises(ProjectNotFoundError):
        indexer(settings, store, embeddings).index_project("f" * 32)
    with pytest.raises(ProjectNotFoundError):
        indexer(settings, store, embeddings).index_project("../../etc")

    assert not store.collection_exists()
    assert embeddings.documents_embedded == 0


def test_qdrant_down_fails_before_the_analysis(
    settings: Settings, make_zip: MakeZip, embeddings: HashingEmbeddings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    from qdrant_client import QdrantClient

    project = import_project(settings, make_zip, AUTH_PROJECT)
    client = QdrantClient(url="http://127.0.0.1:9", timeout=1, check_compatibility=False)
    down = QdrantVectorStore(client, "chunks")
    service = indexer(settings, down, embeddings)
    analyzed: list[str] = []
    monkeypatch.setattr(service, "chunk_project", lambda project_id: analyzed.append(project_id))

    with pytest.raises(VectorStoreUnavailableError):
        service.index_project(project.id)
    assert analyzed == []


def test_embedding_failure_deletes_nothing(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore
) -> None:
    project = import_project(settings, make_zip, AUTH_PROJECT)
    indexer(settings, store, HashingEmbeddings()).index_project(project.id)
    before = store.count(project.id)

    settings.embedding_batch_size = 4
    with pytest.raises(EmbeddingModelError):
        indexer(settings, store, FailingEmbeddings(fail_after=8)).index_project(project.id)

    # Two batches were rewritten, the rest keeps the previous indexing: nothing is lost.
    assert store.count(project.id) == before


def test_another_model_dimension_is_refused(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore
) -> None:
    project = import_project(settings, make_zip, AUTH_PROJECT)
    indexer(settings, store, HashingEmbeddings(dimension=256)).index_project(project.id)

    with pytest.raises(VectorCollectionError):
        indexer(settings, store, HashingEmbeddings(dimension=128)).index_project(project.id)


# ----- Changing the embedding model -----


def chunk_ids(store: QdrantVectorStore, embeddings: Any, project_id: str) -> set[str]:
    hits = retriever(store, embeddings).retrieve(project_id, "user password token", top_k=50)
    return {hit.chunk.id for hit in hits}


def test_reindexing_with_a_new_model_replaces_the_old_vectors(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore
) -> None:
    old, new = HashingEmbeddings(model_name="old/model"), HashingEmbeddings(model_name="new/model")
    project = import_project(settings, make_zip, AUTH_PROJECT)
    first = indexer(settings, store, old).index_project(project.id)
    before = chunk_ids(store, old, project.id)

    assert retriever(store, new).retrieve(project.id, "password") == []  # never mixed
    report = indexer(settings, store, new).index_project(project.id)

    # Same chunks (IDs, metadata) under the new model; the old model's points are gone.
    assert report.chunks == first.chunks and report.stale_chunks_deleted == first.chunks
    assert store.count(project.id) == report.chunks
    assert chunk_ids(store, new, project.id) == before
    assert retriever(store, old).retrieve(project.id, "password") == []
    [hit] = retriever(store, new).retrieve(project.id, "verify password hash", top_k=1)
    assert (hit.chunk.file_path, hit.chunk.start_line) != ("", 0)
    assert hit.chunk.entity_id.startswith(f"{project.id}:")


def test_a_failed_reindexing_with_a_new_model_keeps_the_old_index(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore
) -> None:
    old = HashingEmbeddings(model_name="old/model")
    project = import_project(settings, make_zip, AUTH_PROJECT)
    indexer(settings, store, old).index_project(project.id)
    before = chunk_ids(store, old, project.id)

    settings.embedding_batch_size = 4
    failing = FailingEmbeddings(fail_after=8, model_name="new/model")
    with pytest.raises(EmbeddingModelError):
        indexer(settings, store, failing).index_project(project.id)

    # Two batches were written for the new model, next to (not over) the old points.
    assert chunk_ids(store, old, project.id) == before


def test_index_all_projects(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    first = import_project(settings, make_zip, AUTH_PROJECT)
    second = import_project(settings, make_zip, AUTH_PROJECT)
    service = indexer(settings, store, embeddings)
    chunk_project = service.chunk_project

    def broken_first(project_id: str) -> Any:
        if project_id == first.id:
            raise ProjectNotFoundError("Project files are missing.")
        return chunk_project(project_id)

    service.chunk_project = broken_first  # type: ignore[method-assign]
    reports, failures = service.index_all_projects()

    # One project failing does not stop the others.
    assert [r.project_id for r in reports] == [second.id]
    assert failures == {first.id: "Project files are missing."}
    assert store.count(second.id) == reports[0].chunks and store.count(first.id) == 0


def test_index_all_projects_stops_when_the_model_is_missing(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore
) -> None:
    import_project(settings, make_zip, AUTH_PROJECT)
    import_project(settings, make_zip, AUTH_PROJECT)

    with pytest.raises(EmbeddingModelError):  # would fail the same way for every project
        indexer(settings, store, FailingEmbeddings()).index_all_projects()


def test_files_that_cannot_be_parsed_are_counted_not_fatal(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    files = {**AUTH_PROJECT, "app/broken.py": "def broken(:\n    pass\n\ndef fine():\n    return 1\n"}
    project = import_project(settings, make_zip, files)
    service = indexer(settings, store, embeddings)
    scanned = service.project_service.list_files(project.id)
    # Listed by the scanner, gone when it is read: Phase 3 reports it as a failure.
    vanished = ScannedFile("app/vanished.py", Language.PYTHON, 10)
    service.project_service.list_files = lambda _id: [*scanned, vanished]  # type: ignore[method-assign]

    report = service.index_project(project.id)

    assert report.failed_files == 1
    assert report.files == 5  # the file with syntax errors is still chunked
    names = {h.chunk.qualified_name for h in retriever(store, embeddings).retrieve(project.id, "fine", top_k=50)}
    assert "fine" in names


def test_empty_project_indexes_nothing(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    project = import_project(settings, make_zip, {"src/empty.py": "", "src/blank.ts": "\n\n"})

    report = indexer(settings, store, embeddings).index_project(project.id)

    assert (report.files, report.chunks) == (2, 0)
    assert retriever(store, embeddings).retrieve(project.id, "anything") == []


# ----- Retrieval validation -----


@pytest.fixture
def indexed(
    settings: Settings, make_zip: MakeZip, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> Project:
    project = import_project(settings, make_zip, AUTH_PROJECT)
    indexer(settings, store, embeddings).index_project(project.id)
    return project


def test_top_k_default_and_maximum(
    indexed: Project, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    retrieval = retriever(store, embeddings, vector_top_k=3, vector_max_top_k=5)

    assert len(retrieval.retrieve(indexed.id, "user")) == 3
    assert len(retrieval.retrieve(indexed.id, "user", top_k=5)) == 5
    for bad in [0, -1, 6, True, "3", 2.5]:
        with pytest.raises(InvalidVectorQueryError):
            retrieval.retrieve(indexed.id, "user", top_k=bad)  # type: ignore[arg-type]


def test_retrieval_settings_are_validated(store: QdrantVectorStore, embeddings: HashingEmbeddings) -> None:
    with pytest.raises(ValueError):
        retriever(store, embeddings, vector_top_k=60, vector_max_top_k=50)
    with pytest.raises(ValueError):
        retriever(store, embeddings, vector_min_score=1.5)


@pytest.mark.parametrize("project_id", ["", "../etc", "A" * 32, "a" * 31, MALICIOUS])
def test_invalid_project_ids_are_rejected(
    project_id: str, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    with pytest.raises(ProjectNotFoundError):
        retriever(store, embeddings).retrieve(project_id, "user")
    assert embeddings.queries_embedded == 0


def test_invalid_queries_and_filters_are_rejected(
    indexed: Project, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    retrieval = retriever(store, embeddings)
    for query in ["", "   ", "x" * 2001, None]:
        with pytest.raises(InvalidVectorQueryError):
            retrieval.retrieve(indexed.id, query)  # type: ignore[arg-type]
    for filters in [{"languages": ["ruby"]}, {"languages": []}, {"entity_types": ["module"]},
                    {"entity_types": [MALICIOUS]}]:  # fmt: skip
        with pytest.raises(InvalidVectorQueryError):
            retrieval.retrieve(indexed.id, "user", **filters)
    assert embeddings.queries_embedded == 0  # rejected before the model is used


def test_filters_by_language_and_entity_type(
    indexed: Project, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    retrieval = retriever(store, embeddings)

    typescript = retrieval.retrieve(indexed.id, "total price item", languages="typescript", top_k=50)
    methods = retrieval.retrieve(indexed.id, "user", entity_types=["method"], top_k=50)

    assert typescript and {h.chunk.language for h in typescript} == {"typescript"}
    assert methods and {h.chunk.entity_type for h in methods} == {"method"}


def test_malicious_query_is_only_text(
    indexed: Project, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    before = store.count(indexed.id)

    results = retriever(store, embeddings).retrieve(indexed.id, MALICIOUS)

    assert {h.chunk.project_id for h in results} <= {indexed.id}
    assert store.count(indexed.id) == before


def test_min_score_threshold_drops_weak_matches(
    indexed: Project, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    everything = retriever(store, embeddings).retrieve(indexed.id, "password token", top_k=50)
    assert everything[2].score > everything[3].score
    cut = (everything[2].score + everything[3].score) / 2  # keeps exactly the first 3
    strict = retriever(store, embeddings, vector_min_score=cut).retrieve(
        indexed.id, "password token", top_k=50
    )

    assert [hit.chunk for hit in strict] == [hit.chunk for hit in everything[:3]]


def test_results_are_deterministic(
    indexed: Project, store: QdrantVectorStore, embeddings: HashingEmbeddings
) -> None:
    retrieval = retriever(store, embeddings)
    first = retrieval.retrieve(indexed.id, "user password", top_k=10)
    again = retrieval.retrieve(indexed.id, "user password", top_k=10)

    # Same chunks in the same order; scores equal up to float32 noise (Qdrant computes
    # in float32, and the last digits can vary from one search to the next).
    assert [hit.chunk.id for hit in again] == [hit.chunk.id for hit in first]
    assert [hit.score for hit in again] == pytest.approx([hit.score for hit in first], abs=1e-6)
    scores = [hit.score for hit in first]
    assert scores == sorted(scores, reverse=True)


def test_never_indexed_project_returns_nothing(store: QdrantVectorStore, embeddings: HashingEmbeddings) -> None:
    assert retriever(store, embeddings).retrieve("c" * 32, "user") == []


def test_query_embedding_failure_is_translated(indexed: Project, store: QdrantVectorStore) -> None:
    class BrokenQueries(HashingEmbeddings):
        def embed_query(self, text: str) -> list[float]:
            raise EmbeddingModelError("The embedding model failed to embed the text.")

    with pytest.raises(EmbeddingModelError):
        retriever(store, BrokenQueries()).retrieve(indexed.id, "user")
