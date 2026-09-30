"""SentenceTransformerEmbeddings, with a fake model: loading, prefixes, shapes, errors.

The real model is tested by the optional `pytest -m embeddings` tests
(test_vector_integration.py): these tests never download anything.
"""

import threading
from typing import Any

import pytest

from app.core.errors import EmbeddingModelError
from app.rag.embeddings import (
    QUERY_PREFIXES,
    SentenceTransformerEmbeddings,
    get_embedding_provider,
)
from tests.vector_helpers import HashingEmbeddings


class FakeModel:
    """Mimics SentenceTransformer.encode(): records its calls, returns unit vectors."""

    def __init__(self, dimension: int = 4, broken: bool = False, wrong_shape: bool = False):
        self.dimension = dimension
        self.broken = broken
        self.wrong_shape = wrong_shape
        self.calls: list[dict[str, Any]] = []

    def get_sentence_embedding_dimension(self) -> int:
        return self.dimension

    def encode(self, texts: list[str], **options: Any) -> list[list[float]]:
        self.calls.append({"texts": texts, **options})
        if self.broken:
            raise RuntimeError("CUDA out of memory: SECRET SOURCE CODE")
        size = self.dimension + (1 if self.wrong_shape else 0)
        return [[1.0] + [0.0] * (size - 1) for _ in texts]


def provider(model: FakeModel, name: str = "BAAI/bge-small-en-v1.5", **kwargs: Any):
    loads: list[str] = []

    def loader(model_name: str) -> FakeModel:
        loads.append(model_name)
        return model

    return SentenceTransformerEmbeddings(name, loader=loader, **kwargs), loads


def test_model_is_loaded_once_and_lazily() -> None:
    embeddings, loads = provider(FakeModel())
    assert loads == []  # creating the provider loads nothing

    embeddings.embed_query("a")
    embeddings.embed_documents(["b", "c"])
    assert embeddings.dimension == 4
    assert loads == ["BAAI/bge-small-en-v1.5"]


def test_concurrent_first_use_loads_the_model_once() -> None:
    embeddings, loads = provider(FakeModel())
    threads = [threading.Thread(target=embeddings.embed_query, args=("q",)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert loads == ["BAAI/bge-small-en-v1.5"]


def test_dimension_comes_from_the_model() -> None:
    embeddings, _ = provider(FakeModel(dimension=384))

    assert embeddings.dimension == 384
    assert len(embeddings.embed_query("x")) == 384


def test_documents_and_queries_use_the_same_model_and_options() -> None:
    model = FakeModel()
    embeddings, _ = provider(model, batch_size=7)

    documents = embeddings.embed_documents(["def login(): ...", "class User: ..."])
    query = embeddings.embed_query("where is login?")

    assert len(documents) == 2 and len(query) == 4
    for call in model.calls:
        assert call["normalize_embeddings"] is True
        assert call["batch_size"] == 7
        assert call["show_progress_bar"] is False
    # The model's query instruction goes before queries only, never before documents.
    prefix = QUERY_PREFIXES["BAAI/bge-small-en-v1.5"]
    assert model.calls[0]["texts"] == ["def login(): ...", "class User: ..."]
    assert model.calls[1]["texts"] == [prefix + "where is login?"]


def test_models_without_a_known_instruction_get_no_prefix() -> None:
    model = FakeModel()
    embeddings, _ = provider(model, name="BAAI/bge-m3")
    embeddings.embed_query("q")
    assert model.calls[0]["texts"] == ["q"]

    explicit, _ = provider(FakeModel(), name="BAAI/bge-small-en-v1.5", query_prefix="")
    assert explicit.query_prefix == ""


def test_no_documents_means_no_model_call() -> None:
    model = FakeModel()
    embeddings, loads = provider(model)

    assert embeddings.embed_documents([]) == []
    assert model.calls == [] and loads == []


def test_loading_failure_is_translated() -> None:
    def loader(_name: str) -> Any:
        raise OSError("401 Client Error for url https://huggingface.co/...")

    embeddings = SentenceTransformerEmbeddings("unknown/model", loader=loader)

    with pytest.raises(EmbeddingModelError) as error:
        embeddings.embed_query("q")
    assert "unknown/model" in error.value.message and "EMBEDDING_MODEL" in error.value.message
    assert error.value.status_code == 503


def test_embedding_failure_is_translated_without_leaking_the_text() -> None:
    embeddings, _ = provider(FakeModel(broken=True))

    with pytest.raises(EmbeddingModelError) as error:
        embeddings.embed_documents(["password = 'hunter2'"])
    assert "hunter2" not in error.value.message and "SECRET" not in error.value.message


def test_vectors_of_the_wrong_shape_are_rejected() -> None:
    embeddings, _ = provider(FakeModel(wrong_shape=True))

    with pytest.raises(EmbeddingModelError):
        embeddings.embed_query("q")


def test_batch_size_is_validated() -> None:
    with pytest.raises(ValueError):
        SentenceTransformerEmbeddings("m", batch_size=0)


def test_one_provider_per_model_for_the_whole_process() -> None:
    first = get_embedding_provider("some/model-for-cache-test")
    assert get_embedding_provider("some/model-for-cache-test") is first
    assert get_embedding_provider("other/model-for-cache-test") is not first


def test_test_embeddings_are_consistent_and_normalized() -> None:
    embeddings = HashingEmbeddings(dimension=32)
    text = "class AuthService: def login(self, username, password)"

    [document] = embeddings.embed_documents([text])
    assert embeddings.embed_query(text) == document  # same text, same vector
    assert abs(sum(v * v for v in document) - 1.0) < 1e-9
    assert len(document) == embeddings.dimension == 32
