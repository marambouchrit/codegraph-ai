"""Prepare an imported project for chat: build its knowledge graph, then its vector index.

    AnalysisService (this module)  the order of the steps, nothing else
        ├── ProjectService             does the project exist?                (Phase 2)
        ├── GraphService               parse, extract, write to Neo4j         (Phases 3-6)
        └── VectorIndexService         chunk, embed (BGE-M3), write to Qdrant (Phase 8)

No parsing, no Cypher, no embedding, no Qdrant call here: both services already do
it, and both are idempotent (MERGE + stale cleanup by build ID; deterministic point
IDs + stale cleanup by index ID), so analyzing a project again refreshes it without
duplicates. The two services are given as factories, so an unknown project is a 404
before any connection or model is created.

A result is returned only when both steps succeeded: any failure raises its own
AppError (503 Neo4j/Qdrant/model unavailable...), so a project is never reported
ready after a failed step. If indexing fails after the graph was built, the graph
is kept (it is valid) and the previous vectors, if any, are untouched; analyzing
again completes it.

Two analyses of the same project at the same time are refused (409): each build
removes what the other wrote as "stale". This lock is per process (one API worker).
"""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from app.core.errors import AnalysisInProgressError
from app.graph.models import GraphBuildReport
from app.rag.models import VectorIndexReport
from app.services.project_service import ProjectService

logger = logging.getLogger(__name__)


class ProjectGraphBuilder(Protocol):
    """What the analysis needs from GraphService."""

    def build_project_graph(self, project_id: str) -> GraphBuildReport: ...


class ProjectVectorIndexer(Protocol):
    """What the analysis needs from VectorIndexService."""

    def index_project(self, project_id: str) -> VectorIndexReport: ...


@dataclass(frozen=True)
class AnalysisResult:
    """Both reports of a successful analysis."""

    graph: GraphBuildReport
    vectors: VectorIndexReport
    duration_seconds: float


_running: set[str] = set()
_running_lock = threading.Lock()


@contextmanager
def _exclusive(project_id: str) -> Iterator[None]:
    with _running_lock:
        if project_id in _running:
            raise AnalysisInProgressError(
                "This project is already being analyzed. Try again when it has finished."
            )
        _running.add(project_id)
    try:
        yield
    finally:
        with _running_lock:
            _running.discard(project_id)


class AnalysisService:
    def __init__(
        self,
        project_service: ProjectService,
        graph: Callable[[], ProjectGraphBuilder],
        vectors: Callable[[], ProjectVectorIndexer],
    ) -> None:
        self.project_service = project_service
        self._graph = graph
        self._vectors = vectors

    def analyze(self, project_id: str) -> AnalysisResult:
        """Build the graph, then the vector index (ProjectNotFoundError if unknown)."""
        self.project_service.get_project(project_id)  # invalid or unknown ID: 404, first
        with _exclusive(project_id):
            started = time.perf_counter()
            # Graph first: Neo4j down fails in seconds, before the slow embedding step.
            graph = self._graph().build_project_graph(project_id)
            vectors = self._vectors().index_project(project_id)
            result = AnalysisResult(graph, vectors, round(time.perf_counter() - started, 3))
        logger.info("Project %s analyzed in %.1f s: %s; %s", project_id, result.duration_seconds,
                    graph.summary, vectors.summary)  # fmt: skip
        return result
