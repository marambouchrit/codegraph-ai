"""Semantic search over a project's code: "Where is JWT authentication implemented?"

    VectorRetrievalService   validation, defaults                         (Phase 8)
            ↓
    EmbeddingProvider        question -> vector (same model as the chunks)
            ↓
    QdrantVectorStore        nearest chunks, filtered by project and model
            ↓
    Qdrant

The vector counterpart of GraphRetrievalService (Phase 7), and independent from it:
vector search finds code that is *about* the question; graph retrieval says how
code is *connected*. Phase 9 combines both.

Usage:

    with QdrantVectorStore.from_settings(settings) as store:
        embeddings = embedding_provider_from_settings(settings)
        retrieval = VectorRetrievalService(store, embeddings, settings)
        for hit in retrieval.retrieve(project_id, "How are users authenticated?"):
            print(hit.score, hit.chunk.qualified_name, hit.chunk.file_path, hit.chunk.start_line)
"""

from collections.abc import Sequence

from app.core.config import Settings
from app.core.errors import InvalidVectorQueryError, ProjectNotFoundError
from app.extraction.models import EntityType
from app.ingestion.languages import Language
from app.ingestion.workspace import is_valid_project_id
from app.rag.embeddings import EmbeddingProvider
from app.rag.models import ChunkSearchResult
from app.rag.vector_store import QdrantVectorStore

MAX_QUERY_CHARS = 2000


class VectorRetrievalService:
    def __init__(
        self,
        vector_store: QdrantVectorStore,
        embeddings: EmbeddingProvider,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or Settings()
        if not 1 <= settings.vector_top_k <= settings.vector_max_top_k:
            raise ValueError("VECTOR_TOP_K must be from 1 to VECTOR_MAX_TOP_K")
        min_score = settings.vector_min_score
        if min_score is not None and not -1.0 <= min_score <= 1.0:
            raise ValueError("VECTOR_MIN_SCORE must be a cosine similarity, from -1 to 1")
        self.vector_store = vector_store
        self.embeddings = embeddings
        self.default_top_k = settings.vector_top_k
        self.max_top_k = settings.vector_max_top_k
        self.min_score = min_score

    def retrieve(
        self,
        project_id: str,
        query: str,
        *,
        top_k: int | None = None,
        languages: Sequence[Language | str] | None = None,
        entity_types: Sequence[EntityType | str] | None = None,
    ) -> list[ChunkSearchResult]:
        """The `top_k` chunks of the project most similar to `query`, best first.

        Optional filters keep only some languages or entity types ("method"...). A project
        that was never indexed gives an empty list.
        """
        if not is_valid_project_id(project_id):
            raise ProjectNotFoundError(f"Project '{project_id}' not found.")
        query = _query(query)
        limit = self._top_k(top_k)
        language_values = _values(languages, Language, "language")
        type_values = _values(entity_types, EntityType, "entity type")

        vector = self.embeddings.embed_query(query)
        return self.vector_store.search(
            project_id,
            vector,
            embedding_model=self.embeddings.model_name,
            limit=limit,
            languages=language_values,
            entity_types=type_values,
            score_threshold=self.min_score,
        )

    def _top_k(self, top_k: int | None) -> int:
        if top_k is None:
            return self.default_top_k
        valid = isinstance(top_k, int) and not isinstance(top_k, bool)
        if not valid or not 1 <= top_k <= self.max_top_k:
            raise InvalidVectorQueryError(f"top_k must be an integer from 1 to {self.max_top_k}.")
        return top_k


def _query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise InvalidVectorQueryError("The search query must not be empty.")
    if len(query) > MAX_QUERY_CHARS:
        raise InvalidVectorQueryError(
            f"The search query must be at most {MAX_QUERY_CHARS} characters."
        )
    return query.strip()


def _values(
    values: Sequence[str] | None, enum: type[Language] | type[EntityType], what: str
) -> list[str] | None:
    """Known enum values only (sorted, no duplicates); None means no filter."""
    if values is None:
        return None
    if isinstance(values, str):  # "python" would otherwise be read as p, y, t...
        values = [values]
    try:
        checked = sorted({enum(value).value for value in values})
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        raise InvalidVectorQueryError(f"Unknown {what} (allowed: {allowed}).") from None
    if not checked:
        raise InvalidVectorQueryError(f"The {what} filter must not be empty (use None for all).")
    return checked
