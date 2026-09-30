"""Qdrant: store chunk vectors and search them. The only module that talks to Qdrant.

One collection holds every project (Qdrant's recommended multitenancy layout):

    collection "codegraph_chunks"  (vectors of `dimension` floats, cosine distance)
      point  id      = uuid5(model + chunk ID)   deterministic: re-indexing overwrites
             vector  = embedding of the chunk
             payload = chunk metadata and source text, plus
                       project_id       every search, count and delete filters on it
                       embedding_model  searches only compare vectors of the same model
                       index_id         which indexing wrote it (stale cleanup)

Every filter is built with the qdrant-client models (FieldCondition, MatchValue...),
never from text, so a value such as a file path cannot change what a filter means.
Qdrant exceptions never leave this module: `qdrant_errors()` turns them into the
application's VectorStore*Errors, whose messages never contain the API key.
"""

import logging
import re
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Self

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from app.core.config import Settings
from app.core.errors import (
    VectorCollectionError,
    VectorStoreError,
    VectorStoreUnavailableError,
)
from app.core.urls import safe_uri
from app.rag.models import ChunkSearchResult, CodeChunk

logger = logging.getLogger(__name__)

# Collection names come from configuration; still, only simple names are accepted.
COLLECTION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Fixed namespace: uuid5(namespace, chunk ID) is the same on every machine and every run.
POINT_ID_NAMESPACE = uuid.UUID("5b0f6a9e-4c1d-4e0b-9a55-6f1f3c2a7d10")

# The model's vectors are normalized and compared by angle (see embeddings.py).
DISTANCE = models.Distance.COSINE

# Payload fields used in filters get an index. project_id is the tenant key: Qdrant
# then stores each project's points together, which makes per-project search faster.
TENANT_FIELD = "project_id"
KEYWORD_FIELDS = ("embedding_model", "index_id", "language", "entity_type")

SCORE_DECIMALS = 6  # float32 carries about 7 significant digits


def point_id(chunk_id: str, embedding_model: str) -> str:
    """Qdrant IDs must be integers or UUIDs: a UUID derived from the model and chunk ID.

    With the model in the ID, indexing with a new model never overwrites the old model's
    points: until the new indexing succeeds (and removes them as stale), searches with
    the old model still find every chunk.
    """
    return str(uuid.uuid5(POINT_ID_NAMESPACE, f"{embedding_model}\n{chunk_id}"))


@dataclass(frozen=True)
class CollectionInfo:
    name: str
    dimension: int
    distance: str
    points: int


class QdrantVectorStore:
    def __init__(self, client: QdrantClient, collection: str, location: str = "") -> None:
        if not isinstance(collection, str) or not COLLECTION_NAME.fullmatch(collection):
            raise VectorCollectionError(
                "Invalid Qdrant collection name: use 1 to 64 letters, digits, '_' or '-'."
            )
        self.client = client
        self.collection = collection
        self.location = location or "Qdrant"  # shown in error messages, never with a key
        self._checked_dimension: int | None = None  # the collection's, once read or created

    @classmethod
    def from_settings(cls, settings: Settings) -> "QdrantVectorStore":
        api_key = settings.qdrant_api_key.get_secret_value() or None
        client = QdrantClient(
            url=settings.qdrant_url, api_key=api_key, timeout=settings.qdrant_timeout_seconds
        )
        return cls(client, settings.qdrant_collection, safe_uri(settings.qdrant_url))

    def __repr__(self) -> str:
        return f"QdrantVectorStore(location={self.location!r}, collection={self.collection!r})"

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # ----- Collection -----

    def verify_connectivity(self) -> None:
        with self.qdrant_errors():
            self.client.get_collections()

    def collection_exists(self) -> bool:
        with self.qdrant_errors():
            return bool(self.client.collection_exists(self.collection))

    def ensure_collection(self, dimension: int) -> None:
        """Create the collection for `dimension`-float vectors, or check the existing one.

        A collection built with another model (another dimension) cannot be reused:
        its vectors could not be compared with the new model's.
        """
        if not self.collection_exists():
            logger.info("Creating Qdrant collection %s (%d dimensions)", self.collection, dimension)
            with self.qdrant_errors():
                self.client.create_collection(
                    self.collection,
                    vectors_config=models.VectorParams(size=dimension, distance=DISTANCE),
                )
                self.client.create_payload_index(
                    self.collection,
                    field_name=TENANT_FIELD,
                    field_schema=models.KeywordIndexParams(
                        type=models.KeywordIndexType.KEYWORD, is_tenant=True
                    ),
                )
                for field in KEYWORD_FIELDS:
                    self.client.create_payload_index(
                        self.collection, field_name=field,
                        field_schema=models.PayloadSchemaType.KEYWORD,
                    )  # fmt: skip
            self._checked_dimension = dimension
            return
        info = self.collection_info()
        if info.distance != DISTANCE.value:
            raise VectorCollectionError(
                f"The Qdrant collection '{self.collection}' uses {info.distance} distance, not "
                f"{DISTANCE.value}: it was not created by CodeGraph AI."
            )
        self._check_dimension(info.dimension, dimension)

    def collection_info(self) -> CollectionInfo:
        with self.qdrant_errors():
            info = self.client.get_collection(self.collection)
        vectors = info.config.params.vectors
        if not isinstance(vectors, models.VectorParams):  # named vectors: not ours
            raise VectorCollectionError(
                f"The Qdrant collection '{self.collection}' was not created by CodeGraph AI."
            )
        return CollectionInfo(
            name=self.collection,
            dimension=vectors.size,
            distance=models.Distance(vectors.distance).value,
            points=info.points_count or 0,
        )

    # ----- Writing -----

    def upsert(
        self,
        chunks: Sequence[CodeChunk],
        vectors: Sequence[Sequence[float]],
        *,
        index_id: str,
        embedding_model: str,
    ) -> int:
        """Create or overwrite one point per chunk (one chunk ID, one point). Returns the count."""
        if len(chunks) != len(vectors):
            raise ValueError("One vector is needed per chunk")
        if not chunks:
            return 0
        points = [
            models.PointStruct(
                id=point_id(chunk.id, embedding_model),
                vector=list(vector),
                payload={
                    **chunk.to_payload(),
                    "embedding_model": embedding_model,
                    "index_id": index_id,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        with self.qdrant_errors():
            self.client.upsert(self.collection, points=points, wait=True)
        return len(points)

    def delete_stale(self, project_id: str, index_id: str) -> int:
        """Delete the project's points written by another indexing. Returns the count."""
        stale = _filter(project_id, exclude_index_id=index_id)
        return self._delete(stale)

    def delete_project(self, project_id: str) -> int:
        """Delete every point of one project (other projects are untouched)."""
        return self._delete(_filter(project_id))

    # ----- Reading -----

    def count(self, project_id: str) -> int:
        if not self.collection_exists():
            return 0
        with self.qdrant_errors():
            return self.client.count(
                self.collection, count_filter=_filter(project_id), exact=True
            ).count

    def search(
        self,
        project_id: str,
        vector: Sequence[float],
        *,
        embedding_model: str,
        limit: int,
        languages: Sequence[str] | None = None,
        entity_types: Sequence[str] | None = None,
        score_threshold: float | None = None,
    ) -> list[ChunkSearchResult]:
        """The `limit` chunks of this project closest to `vector`, best first.

        Always filtered by project and by embedding model. Nothing indexed yet (no
        collection) is an empty result, not an error.
        """
        if not self.collection_exists():
            return []
        # A query vector of another dimension (the model changed, the collection did not):
        # a clear error rather than Qdrant's HTTP 400.
        if self._checked_dimension is None:
            self._checked_dimension = self.collection_info().dimension
        self._check_dimension(self._checked_dimension, len(vector))
        query_filter = _filter(
            project_id, embedding_model=embedding_model,
            languages=languages, entity_types=entity_types,
        )  # fmt: skip
        with self.qdrant_errors():
            response = self.client.query_points(
                self.collection,
                query=list(vector),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
                score_threshold=score_threshold,
            )
        # Qdrant computes in float32: the last digits of a score vary slightly from one
        # search to the next. Rounded, equal scores are really equal, and sorting them by
        # chunk ID gives the same list for the same search.
        results = [
            ChunkSearchResult(
                chunk=CodeChunk.from_payload(point.payload or {}),
                score=round(point.score, SCORE_DECIMALS),
            )
            for point in response.points
        ]
        return sorted(results, key=lambda result: (-result.score, result.chunk.id))

    # ----- Helpers -----

    def _check_dimension(self, collection_dimension: int, dimension: int) -> None:
        if collection_dimension != dimension:
            raise VectorCollectionError(
                f"The Qdrant collection '{self.collection}' holds {collection_dimension}-dimension "
                f"vectors, but the embedding model produces {dimension}-dimension vectors: set "
                "another QDRANT_COLLECTION and re-index the projects (see docs/architecture.md)."
            )
        self._checked_dimension = collection_dimension

    def _delete(self, points_filter: models.Filter) -> int:
        if not self.collection_exists():
            return 0
        with self.qdrant_errors():
            count = self.client.count(self.collection, count_filter=points_filter, exact=True).count
            if count:
                self.client.delete(
                    self.collection,
                    points_selector=models.FilterSelector(filter=points_filter),
                    wait=True,
                )
        return count

    @contextmanager
    def qdrant_errors(self) -> Iterator[None]:
        """Translate qdrant-client exceptions into application errors."""
        try:
            yield
        except ResponseHandlingException as error:  # connection refused, timeout, DNS...
            raise VectorStoreUnavailableError(
                f"Qdrant is not reachable at {self.location}. Is it running? "
                "Start it with: docker compose up -d qdrant"
            ) from error
        except UnexpectedResponse as error:
            if error.status_code in (401, 403):
                raise VectorStoreUnavailableError(
                    "Qdrant rejected the request: check QDRANT_API_KEY."
                ) from error
            if error.status_code == 404:
                raise VectorCollectionError(
                    f"The Qdrant collection '{self.collection}' does not exist."
                ) from error
            logger.error("Qdrant error: HTTP %s", error.status_code)
            raise VectorStoreError("A Qdrant operation failed.") from error


def _filter(
    project_id: str,
    *,
    embedding_model: str | None = None,
    languages: Sequence[str] | None = None,
    entity_types: Sequence[str] | None = None,
    exclude_index_id: str | None = None,
) -> models.Filter:
    """A filter that always keeps one project only, plus the optional conditions."""
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("A project ID is required: a filter must never cover every project")
    must: list[Any] = [_equals(TENANT_FIELD, project_id)]
    if embedding_model is not None:
        must.append(_equals("embedding_model", embedding_model))
    if languages:
        must.append(_any("language", languages))
    if entity_types:
        must.append(_any("entity_type", entity_types))
    must_not: list[Any] = []
    if exclude_index_id is not None:
        must_not.append(_equals("index_id", exclude_index_id))
    return models.Filter(must=must, must_not=must_not or None)


def _equals(key: str, value: str) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def _any(key: str, values: Sequence[str]) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchAny(any=list(values)))
