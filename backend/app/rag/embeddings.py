"""Turn text into vectors (embeddings) with a local, open-source model.

The rest of the application only sees `EmbeddingProvider`: a dimension, a model
name, and two methods. Which library or model runs behind it is an implementation
detail of this module.

    embed_documents(chunks) -> vectors stored in Qdrant      (indexing)
    embed_query(question)   -> vector compared with them     (search)

Both must come from the same model: two different models place texts in two
unrelated vector spaces, and comparing a vector of one with a vector of the other
gives meaningless scores. Some models are trained with a short instruction before
*queries* (not documents); it is part of how that same model is used.

The model runs on this machine (sentence-transformers). It is downloaded once from
Hugging Face into the local cache; no code is ever sent to an external service,
and no model code is executed (trust_remote_code=False).
"""

import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Any

from app.core.config import Settings
from app.core.errors import EmbeddingModelError

logger = logging.getLogger(__name__)

Vector = list[float]

# Instructions some models expect before a search query (from their model cards).
QUERY_PREFIXES: dict[str, str] = {
    f"BAAI/bge-{size}-en-v1.5": "Represent this sentence for searching relevant passages: "
    for size in ("small", "base", "large")
}


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Length of every vector produced (the Qdrant collection is created with it)."""

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        """One vector per text, in order."""

    @abstractmethod
    def embed_query(self, text: str) -> Vector: ...


ModelLoader = Callable[[str], Any]


def load_sentence_transformer(model_name: str, device: str = "cpu") -> Any:
    # Imported here: it loads PyTorch, which takes seconds, and only indexing and
    # search need it (the API and the other tests start without it).
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name, device=device, trust_remote_code=False)


class SentenceTransformerEmbeddings(EmbeddingProvider):
    """A sentence-transformers model, loaded on first use and then reused."""

    def __init__(
        self,
        model_name: str,
        batch_size: int = 32,
        query_prefix: str | None = None,
        loader: ModelLoader | None = None,
        device: str = "cpu",
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._model_name = model_name
        self.batch_size = batch_size
        if query_prefix is None:
            query_prefix = QUERY_PREFIXES.get(model_name, "")
        self.query_prefix = query_prefix
        self.device = device
        self._loader = loader or (lambda name: load_sentence_transformer(name, device))
        self._model: Any = None
        self._lock = threading.Lock()  # two first requests at once must not load it twice

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._model = self._load()
        return self._model

    @property
    def dimension(self) -> int:
        get_dimension = getattr(self.model, "get_embedding_dimension", None) or getattr(
            self.model, "get_sentence_embedding_dimension"
        )
        dimension = get_dimension()
        if not isinstance(dimension, int) or dimension < 1:
            raise EmbeddingModelError(
                f"The embedding model '{self._model_name}' does not report its dimension."
            )
        return dimension

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        return self._encode(list(texts)) if texts else []

    def embed_query(self, text: str) -> Vector:
        return self._encode([self.query_prefix + text])[0]

    def _load(self) -> Any:
        logger.info("Loading embedding model %s on %s", self._model_name, self.device)
        started = time.perf_counter()
        try:
            model = self._loader(self._model_name)
        except Exception as error:
            # The library's message may be long (HTTP details...): log its type only.
            logger.error("Could not load embedding model %s: %s", self._model_name,
                         type(error).__name__)  # fmt: skip
            raise EmbeddingModelError(
                f"The embedding model '{self._model_name}' could not be loaded on "
                f"'{self.device}': check EMBEDDING_MODEL and EMBEDDING_DEVICE, and that the "
                "model could be downloaded once (network, disk space, Hugging Face cache)."
            ) from error
        logger.info("Embedding model %s loaded in %.1f s", self._model_name,
                    time.perf_counter() - started)  # fmt: skip
        return model

    def _encode(self, texts: list[str]) -> list[Vector]:
        model = self.model
        try:
            # Normalized vectors (length 1): cosine similarity is then a plain dot product,
            # and scores are comparable from one text to another.
            vectors = model.encode(
                texts,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            result = [[float(value) for value in vector] for vector in vectors]
        except Exception as error:
            logger.error("Embedding failed: %s", type(error).__name__)  # never log the code
            raise EmbeddingModelError("The embedding model failed to embed the text.") from error
        dimension = self.dimension
        if len(result) != len(texts) or any(len(vector) != dimension for vector in result):
            raise EmbeddingModelError("The embedding model returned vectors of the wrong shape.")
        # NaN or infinity would be stored in Qdrant and silently break every score.
        if not all(math.isfinite(value) for vector in result for value in vector):
            raise EmbeddingModelError("The embedding model returned non-finite values.")
        return result


@lru_cache(maxsize=4)
def get_embedding_provider(
    model_name: str, batch_size: int = 32, device: str = "cpu"
) -> EmbeddingProvider:
    """One provider (so one loaded model) per model and device for the whole process."""
    return SentenceTransformerEmbeddings(model_name, batch_size=batch_size, device=device)


def embedding_provider_from_settings(settings: Settings) -> EmbeddingProvider:
    """The provider for EMBEDDING_MODEL / EMBEDDING_BATCH_SIZE / EMBEDDING_DEVICE."""
    return get_embedding_provider(
        settings.embedding_model, settings.embedding_batch_size, settings.embedding_device
    )
