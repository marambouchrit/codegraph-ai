"""One analysis of a project, full or incremental: source files -> Neo4j graph + Qdrant vectors.

It reuses the existing layers and adds no parser, resolver, graph writer or embedder:

    ProjectService                 the project's files                          (Phase 2)
    EntityExtractionService        parse + entities, one parse per file         (Phases 3-4)
    RelationshipExtractionService  collect() per file, resolve() for the project (Phase 5)
    GraphBuilder                   build() everything, or apply() a difference  (Phase 6)
    CodeChunker, VectorIndexService  chunks, embed(), store()                   (Phase 8)

The steps (each one is a `phase` reported to the caller):

    preparing          Neo4j and Qdrant answer, the embedding model is loaded
    detecting_changes  SHA-256 of every source file, compared with the saved index
    parsing            only added and modified files are parsed (entities, references, chunks)
    resolving          relationships are resolved again over ALL files, from the cached
                       entities and references of the unchanged ones: a call from an
                       unchanged file to a function that was just deleted disappears
    embedding          only chunks whose embedded text changed get a new vector
    ----- nothing was written until here: a failure leaves the previous analysis valid -----
    graph              Neo4j: write new and changed nodes and relationships, delete the gone
    vector_index       Qdrant: write new vectors, refresh moved metadata, delete the gone
    finalizing         save the index for the next incremental analysis

The first analysis is "full": everything is parsed, embedded and written (with the
stale cleanup of Phases 6 and 8, which also repairs leftovers). Later ones are
"incremental" when the saved index can be trusted: it exists, was made with the same
embedding model and chunking, and its counts match Neo4j and Qdrant. Otherwise the
analysis falls back to a full one.
"""

import logging
import time
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Protocol

from app.analysis.changes import FileChanges, detect_changes, hash_files
from app.analysis.index import (
    AnalysisIndex,
    AnalysisIndexStore,
    ChunkRecord,
    EdgeRecord,
    FileRecord,
    chunk_content_hash,
    fingerprint,
)
from app.extraction.models import ExtractionReport, FileEntities
from app.graph.builder import (
    diff_graph,
    edge_fingerprint,
    graph_edges,
    graph_nodes,
    node_fingerprint,
)
from app.ingestion.scanner import ScannedFile
from app.parsing.base import ParseResult
from app.parsing.service import ParseFailure
from app.rag.models import CodeChunk
from app.relationships.models import RelationshipReport
from app.services.analysis_store import AnalysisChanges, AnalysisResult
from app.services.graph_service import GraphService
from app.services.project_service import ProjectService
from app.services.vector_index_service import VectorIndexService

logger = logging.getLogger(__name__)

PHASES = (
    "preparing", "detecting_changes", "parsing", "resolving",
    "embedding", "graph", "vector_index", "finalizing",
)  # fmt: skip

FULL, INCREMENTAL = "full", "incremental"


class ProgressListener(Protocol):
    """How the pipeline reports what it is really doing."""

    def phase(self, name: str, total: int | None = None, unit: str | None = None) -> None:
        """A new phase starts. `total` is given only when the work can be counted."""

    def advance(self, completed: int) -> None:
        """`completed` of the phase's `total` are done."""

    def mode(self, mode: str) -> None:
        """Whether this analysis is "full" or "incremental"."""

    def writing(self) -> None:
        """The databases are about to change: the previous result no longer describes them."""


class AnalysisPipeline:
    def __init__(
        self,
        project_service: ProjectService,
        graph: GraphService,
        vectors: VectorIndexService,
    ) -> None:
        self.projects = project_service
        self.graph = graph
        self.vectors = vectors
        self.index_store = AnalysisIndexStore(project_service.workspace)

    def run(
        self, project_id: str, progress: ProgressListener, force_full: bool = False
    ) -> AnalysisResult:
        started = time.perf_counter()
        self.projects.get_project(project_id)  # unknown project: fail before anything

        progress.phase("preparing")
        self.graph.repository.verify_connectivity()
        self.vectors.prepare()  # Qdrant, the model, the collection
        model = self.vectors.embeddings.model_name

        progress.phase("detecting_changes")
        files = self.projects.list_files(project_id)
        source_dir = self.projects.workspace.source_dir(project_id)
        hashes = hash_files(source_dir, files)
        index = None if force_full else self._trusted_index(project_id)
        mode = INCREMENTAL if index is not None else FULL
        progress.mode(mode)
        previous = index or self._empty_index()
        changes = detect_changes(previous.file_hashes, hashes)

        to_parse = [file for file in files if file.path in changes.to_analyze]
        progress.phase("parsing", total=len(to_parse), unit="files")
        parsed, new_chunks = self._analyze_files(
            project_id, source_dir, to_parse, hashes, model, progress
        )
        # Every current file, in scan order: just parsed, or as the last analysis left it.
        records = {file.path: parsed.get(file.path) or previous.files[file.path] for file in files}

        progress.phase("resolving")
        report = self._resolve(project_id, records)
        nodes, edges = graph_nodes(report), graph_edges(report)
        diff = diff_graph(
            nodes, edges, previous.nodes,
            {key: (edge.hash, edge.source_id) for key, edge in previous.edges.items()},
        )  # fmt: skip
        current_chunks = {
            chunk_id: chunk for record in records.values() for chunk_id, chunk in record.chunks.items()
        }
        plan = _plan_chunks(previous.chunks, current_chunks, new_chunks)

        progress.phase("embedding", total=len(plan.to_embed), unit="chunks")
        vectors = self.vectors.embed(plan.to_embed, progress.advance)

        # ----- From here the databases change -----
        progress.writing()
        self.index_store.clear(project_id)  # a failure below means: next analysis is full

        progress.phase("graph")
        if mode == FULL:
            built = self.graph.builder.build(report)  # MERGE everything, remove stale data
            graph_counts = (built.nodes_written, built.relationships_written,
                            built.stale_nodes_deleted, built.stale_relationships_deleted)  # fmt: skip
        else:
            updated = self.graph.builder.apply(project_id, diff)
            graph_counts = (updated.nodes_written, updated.relationships_written,
                            updated.nodes_deleted, updated.relationships_deleted)  # fmt: skip

        progress.phase("vector_index")
        index_id = uuid.uuid4().hex
        store = self.vectors.vector_store
        self.vectors.store(plan.to_embed, vectors, index_id)
        store.update_payloads(plan.to_update, index_id=index_id, embedding_model=model)
        if mode == FULL:
            chunks_deleted = store.delete_stale(project_id, index_id)  # anything left over
        else:
            chunks_deleted = store.delete_chunks(project_id, plan.deleted_ids, model)

        progress.phase("finalizing")
        self.index_store.save(
            project_id,
            AnalysisIndex(
                embedding_model=model,
                collection=store.collection,
                chunk_max_chars=self.vectors.chunker.max_chars,
                chunk_overlap_lines=self.vectors.chunker.overlap_lines,
                files=records,
                nodes={node.id: node_fingerprint(node) for node in nodes},
                edges={e.id: EdgeRecord(edge_fingerprint(e), e.source_id) for e in edges},
            ),
        )
        result = AnalysisResult(
            project_id=project_id,
            mode=mode,
            files=len(report.extraction.files),
            failed_files=len(report.failures),
            entities=len(nodes),
            relationships=len(edges),
            entities_by_type=dict(sorted(Counter(node.label for node in nodes).items())),
            relationships_by_type=dict(sorted(Counter(edge.type for edge in edges).items())),
            unresolved_references=len(report.unresolved),
            chunks=len(current_chunks),
            chunks_by_type=dict(
                sorted(Counter(chunk.entity_type for chunk in current_chunks.values()).items())
            ),
            embedding_model=model,
            changes=_changes(changes, len(to_parse), graph_counts, plan, chunks_deleted),
            duration_seconds=round(time.perf_counter() - started, 3),
        )
        logger.info(
            "Project %s analyzed (%s) in %.1f s: %d files parsed, %d chunks embedded, %d reused",
            project_id, mode, result.duration_seconds, len(to_parse),
            len(plan.to_embed), result.changes.chunks_reused,
        )  # fmt: skip
        return result

    # ----- Steps -----

    def _empty_index(self) -> AnalysisIndex:
        return AnalysisIndex(
            embedding_model=self.vectors.embeddings.model_name,
            collection=self.vectors.vector_store.collection,
            chunk_max_chars=self.vectors.chunker.max_chars,
            chunk_overlap_lines=self.vectors.chunker.overlap_lines,
        )

    def _trusted_index(self, project_id: str) -> AnalysisIndex | None:
        """The saved index if incremental analysis is safe, otherwise None (full analysis).

        Safe means: made with the same embedding model, collection and chunking, and
        describing exactly what Neo4j and Qdrant hold (same counts). If a database was
        emptied or changed outside the application, everything is rebuilt.
        """
        index = self.index_store.load(project_id)
        if index is None:
            return None
        settings = self._empty_index()
        if (index.embedding_model, index.collection, index.chunk_max_chars, index.chunk_overlap_lines) != (
            settings.embedding_model, settings.collection, settings.chunk_max_chars, settings.chunk_overlap_lines
        ):  # fmt: skip
            logger.info("Project %s: embedding or chunking settings changed, full analysis", project_id)
            return None
        statistics = self.graph.repository.statistics(project_id)
        graph_matches = (
            sum(statistics.nodes_by_label.values()) == len(index.nodes)
            and sum(statistics.relationships_by_type.values()) == len(index.edges)
        )
        if not graph_matches or self.vectors.vector_store.count(project_id) != len(index.chunks):
            logger.warning("Project %s: the saved index does not match the databases, "
                           "full analysis", project_id)  # fmt: skip
            return None
        return index

    def _analyze_files(
        self,
        project_id: str,
        source_dir: Path,
        files: Sequence[ScannedFile],
        hashes: dict[str, str],
        model: str,
        progress: ProgressListener,
    ) -> tuple[dict[str, FileRecord], dict[str, CodeChunk]]:
        """Parse each file once: its entities, its raw references and its chunks."""
        relationships = self.graph.relationship_service
        records: dict[str, FileRecord] = {}
        chunks: dict[str, CodeChunk] = {}

        def on_file(parse_result: ParseResult, file_entities: FileEntities) -> None:
            # Called while the file's syntax tree and source are in memory.
            try:
                references = relationships.collect(parse_result, file_entities, project_id)
            except Exception:
                # The file keeps its entities; it just contributes no relationships.
                logger.exception("Unexpected error while collecting references of %s",
                                 file_entities.path)  # fmt: skip
                references = None
            file_chunks = self.vectors.chunker.chunk_file(
                project_id, parse_result.source, file_entities
            )
            records[file_entities.path] = FileRecord(
                sha256=hashes[file_entities.path],
                entities=file_entities,
                references=references,
                chunks={
                    chunk.id: ChunkRecord(
                        content_hash=chunk_content_hash(model, chunk.embedding_text),
                        payload_hash=fingerprint(chunk.to_payload()),
                        entity_type=chunk.entity_type,
                    )
                    for chunk in file_chunks
                },
            )
            chunks.update({chunk.id: chunk for chunk in file_chunks})

        def counted() -> Iterator[ScannedFile]:
            for done, file in enumerate(files):
                progress.advance(done)  # the previous files are finished
                yield file
            progress.advance(len(files))

        extraction = relationships.extraction_service.extract_project(
            project_id, source_dir, counted(), on_file=on_file
        )
        for failure in extraction.failures:  # unreadable or unparsable: remembered as such
            records[failure.path] = FileRecord(sha256=hashes[failure.path], failure=failure.reason)
            # A failure after chunking started must not leave chunks of that file behind.
            for chunk_id in [c for c, chunk in chunks.items() if chunk.file_path == failure.path]:
                del chunks[chunk_id]
        return records, chunks

    def _resolve(self, project_id: str, records: dict[str, FileRecord]) -> RelationshipReport:
        """Resolve the relationships of the whole project from every file's record."""
        extraction = ExtractionReport(
            project_id=project_id,
            files=[r.entities for r in records.values() if r.entities is not None],
            failures=[
                ParseFailure(path=path, reason=record.failure or "")
                for path, record in records.items() if record.entities is None
            ],  # fmt: skip
        )
        references = [r.references for r in records.values() if r.references is not None]
        return self.graph.relationship_service.resolve(extraction, references)


class _ChunkPlan:
    """What to do with the project's chunks, compared with the previous analysis."""

    def __init__(self) -> None:
        self.to_embed: list[CodeChunk] = []  # new, or their embedded text changed
        self.to_update: list[CodeChunk] = []  # same text, other metadata: vector kept
        self.deleted_ids: list[str] = []
        self.reused = 0  # untouched in Qdrant


def _plan_chunks(
    previous: dict[str, ChunkRecord],
    current: dict[str, ChunkRecord],
    new_chunks: dict[str, CodeChunk],
) -> _ChunkPlan:
    """Chunks of unchanged files are not in `new_chunks`: they are reused as they are."""
    plan = _ChunkPlan()
    for chunk_id, record in current.items():
        before = previous.get(chunk_id)
        chunk = new_chunks.get(chunk_id)
        if before is None or before.content_hash != record.content_hash:
            if chunk is None:  # cannot happen: a changed chunk comes from a parsed file
                raise RuntimeError(f"Chunk {chunk_id} changed but its file was not parsed")
            plan.to_embed.append(chunk)
        elif before.payload_hash != record.payload_hash and chunk is not None:
            plan.to_update.append(chunk)
            plan.reused += 1
        else:
            plan.reused += 1
    plan.deleted_ids = sorted(chunk_id for chunk_id in previous if chunk_id not in current)
    return plan


def _changes(
    files: FileChanges,
    files_parsed: int,
    graph_counts: tuple[int, int, int, int],
    plan: _ChunkPlan,
    chunks_deleted: int,
) -> AnalysisChanges:
    nodes_written, relationships_written, nodes_deleted, relationships_deleted = graph_counts
    return AnalysisChanges(
        files_added=len(files.added),
        files_modified=len(files.modified),
        files_unchanged=len(files.unchanged),
        files_deleted=len(files.deleted),
        files_parsed=files_parsed,
        nodes_written=nodes_written,
        nodes_deleted=nodes_deleted,
        relationships_written=relationships_written,
        relationships_deleted=relationships_deleted,
        chunks_embedded=len(plan.to_embed),
        chunks_reused=plan.reused,
        chunks_updated=len(plan.to_update),
        chunks_deleted=chunks_deleted,
    )
