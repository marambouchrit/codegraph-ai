"""Index an imported project for semantic search: source code -> chunks -> vectors -> Qdrant.

This service only orchestrates existing pieces:

    ProjectService           the project's source folder and files         (Phase 2)
    EntityExtractionService  parse + entities, one file at a time          (Phases 3-4)
    CodeChunker              entities + source -> code-aware chunks        (Phase 8)
    EmbeddingProvider        chunks -> vectors, with a local model         (Phase 8)
    QdrantVectorStore        upsert, stale cleanup                         (Phase 8)

The source files are the source of truth: nothing is read from Neo4j.

Re-indexing a project is safe and idempotent. A chunk's point ID is derived from
its chunk ID, so indexing again overwrites the same points. Every point written
by an indexing carries that indexing's `index_id`; once all chunks are written,
the project's points with another `index_id` (code deleted or renamed since) are
removed. If indexing fails half-way, nothing is deleted: search keeps working
on old and new points until the next successful indexing.

Usage (the store owns the Qdrant connection, so close it when done):

    with QdrantVectorStore.from_settings(settings) as store:
        embeddings = get_embedding_provider(settings.embedding_model)
        report = VectorIndexService(settings, store, embeddings).index_project(project_id)
        print(report.summary)
"""

import logging
import time
import uuid
from collections.abc import Iterator, Sequence
from typing import TypeVar

from app.core.config import Settings
from app.extraction.models import ExtractionReport, FileEntities
from app.extraction.service import EntityExtractionService
from app.parsing.base import ParseResult
from app.parsing.service import ParserService
from app.rag.chunker import CodeChunker
from app.rag.embeddings import EmbeddingProvider
from app.rag.models import ChunkingResult, VectorIndexReport
from app.rag.vector_store import QdrantVectorStore
from app.services.project_service import ProjectService

logger = logging.getLogger(__name__)

T = TypeVar("T")


class VectorIndexService:
    def __init__(
        self,
        settings: Settings,
        vector_store: QdrantVectorStore,
        embeddings: EmbeddingProvider,
        project_service: ProjectService | None = None,
        extraction_service: EntityExtractionService | None = None,
    ) -> None:
        self.vector_store = vector_store
        self.embeddings = embeddings
        self.batch_size = max(1, settings.embedding_batch_size)
        self.chunker = CodeChunker(
            max_chars=settings.vector_chunk_max_chars,
            overlap_lines=settings.vector_chunk_overlap_lines,
        )
        self.project_service = project_service or ProjectService(settings)
        self.extraction_service = extraction_service or EntityExtractionService(
            ParserService(max_file_bytes=settings.max_source_file_kb * 1024)
        )

    def chunk_project(self, project_id: str) -> tuple[ChunkingResult, ExtractionReport]:
        """Parse, extract and chunk every file (raises ProjectNotFoundError if unknown)."""
        files = self.project_service.list_files(project_id)
        source_dir = self.project_service.workspace.source_dir(project_id)
        result = ChunkingResult()

        def on_file(parse_result: ParseResult, file_entities: FileEntities) -> None:
            # Called while the file's source is in memory: no second read, no second parse.
            result.chunks.extend(
                self.chunker.chunk_file(project_id, parse_result.source, file_entities)
            )
            result.files += 1

        extraction = self.extraction_service.extract_project(
            project_id, source_dir, files, on_file=on_file
        )
        return result, extraction

    def index_project(self, project_id: str) -> VectorIndexReport:
        """Chunk, embed and store the project in Qdrant (idempotent). Returns a report."""
        started = time.perf_counter()
        self.project_service.get_project(project_id)  # unknown project: fail before anything
        # Fail fast, before the analysis: Qdrant down, model missing, wrong collection.
        self.vector_store.verify_connectivity()
        dimension = self.embeddings.dimension  # loads the model once
        self.vector_store.ensure_collection(dimension)

        chunking, extraction = self.chunk_project(project_id)
        index_id = uuid.uuid4().hex
        model = self.embeddings.model_name
        written = 0
        for batch in _batches(chunking.chunks, self.batch_size):
            vectors = self.embeddings.embed_documents([chunk.embedding_text for chunk in batch])
            written += self.vector_store.upsert(
                batch, vectors, index_id=index_id, embedding_model=model
            )
        # Only once everything is written: a failure above deletes nothing.
        stale = self.vector_store.delete_stale(project_id, index_id)

        report = VectorIndexReport(
            project_id=project_id,
            index_id=index_id,
            embedding_model=model,
            dimension=dimension,
            files=chunking.files,
            chunks=written,
            chunks_by_type=chunking.chunks_by_type,
            stale_chunks_deleted=stale,
            failed_files=len(extraction.failures),
            duration_seconds=round(time.perf_counter() - started, 3),
        )
        logger.info("Project %s: %s", project_id, report.summary)
        return report

    def delete_project_index(self, project_id: str) -> int:
        """Remove one project's vectors (other projects are untouched). Returns the count."""
        return self.vector_store.delete_project(project_id)

    def count_chunks(self, project_id: str) -> int:
        return self.vector_store.count(project_id)


def _batches(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
