"""Prepare an imported project for chat, in the background: graph + vector index.

    AnalysisService (this module)  jobs and their state, nothing else
        ├── ProjectService             does the project exist?                 (Phase 2)
        ├── JobRunner                  runs the job on a background thread     (Phase 14)
        ├── AnalysisPipeline           the analysis itself, full or incremental (Phase 14)
        └── AnalysisStore              the persisted state: job, progress, result

    start()      validate the project, refuse a second job for it (409), save a
                 "queued" job and hand it to the runner: the HTTP request returns at once
    _run()       on the worker thread: "running", real progress saved as it happens,
                 then "ready" with the result, or "failed" with a safe message
    get_state()  what GET /projects/{id}/analysis returns, read from the state file

No parsing, no Cypher, no embedding, no Qdrant call here. The graph and vector
services are given as factories, so an unknown project is a 404 before any connection
or model is created.

Consistency (Neo4j and Qdrant are two systems, with no common transaction):

    - "ready" is only ever written after both were updated successfully;
    - the pipeline does all the slow, failure-prone work (parsing, embedding) before it
      writes anything: a failure there leaves the previous analysis untouched and valid,
      so the previous result is kept and chat keeps working on it;
    - once writing starts, the previous result is removed from the state. A failure
      after that reads "failed" with no result, and the next analysis is a full one,
      which rewrites everything and removes leftovers.

One analysis per project at a time: two would each remove what the other wrote. The
registry of active jobs is per process, like the runner (one API worker).
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable

from app.analysis.jobs import JobRunner
from app.core.errors import AnalysisInProgressError, AppError
from app.services.analysis_pipeline import AnalysisPipeline
from app.services.analysis_store import (
    ACTIVE,
    AnalysisJob,
    AnalysisResult,
    AnalysisState,
    AnalysisStore,
    JobStatus,
    now,
)
from app.services.graph_service import GraphService
from app.services.project_service import ProjectService
from app.services.vector_index_service import VectorIndexService

__all__ = ["AnalysisResult", "AnalysisService", "AnalysisState"]

logger = logging.getLogger(__name__)

INTERRUPTED = "The analysis was interrupted (the server stopped). Run it again."
UNEXPECTED = "The analysis failed unexpectedly. Run it again."

# Projects with a queued or running job in THIS process: project ID -> job ID.
_active: dict[str, str] = {}
_active_lock = threading.Lock()

PROGRESS_SAVE_SECONDS = 0.5  # counts are saved at most this often (and always at the end)


class AnalysisService:
    def __init__(
        self,
        project_service: ProjectService,
        graph: Callable[[], GraphService],
        vectors: Callable[[], VectorIndexService],
        runner: JobRunner,
    ) -> None:
        self.project_service = project_service
        self.store = AnalysisStore(project_service.workspace)
        self._graph = graph
        self._vectors = vectors
        self._runner = runner

    def get_state(self, project_id: str) -> AnalysisState:
        """The analysis state of the project (ProjectNotFoundError if unknown)."""
        self.project_service.get_project(project_id)
        state = self.store.load(project_id)
        job = state.job
        if job is not None and job.status in ACTIVE and _active.get(project_id) != job.job_id:
            # Saved as queued or running, but no job of this process: the server stopped.
            job.status, job.error, job.finished_at = JobStatus.FAILED, INTERRUPTED, now()
            self.store.save(state)
        return state

    def start(self, project_id: str, full: bool = False) -> AnalysisState:
        """Queue an analysis and return at once. `full` forces a complete re-analysis."""
        self.project_service.get_project(project_id)  # invalid or unknown ID: 404, first
        job = AnalysisJob(job_id=uuid.uuid4().hex, status=JobStatus.QUEUED, queued_at=now())
        with _active_lock:
            if project_id in _active:
                raise AnalysisInProgressError(
                    "This project is already being analyzed. Wait for it to finish."
                )
            _active[project_id] = job.job_id
        try:
            state = self.store.load(project_id)
            state.job = job  # the previous result stays: it is still what chat uses
            self.store.save(state)
            self._runner.submit(lambda: self._run(project_id, job.job_id, full))
        except Exception:
            _release(project_id, job.job_id)
            raise
        return state

    def _run(self, project_id: str, job_id: str, full: bool) -> None:
        """The job, on the worker thread. Never raises: the outcome goes to the state."""
        state = self.store.load(project_id)
        if state.job is None or state.job.job_id != job_id:
            _release(project_id, job_id)
            return  # the project or its state was removed meanwhile
        job = state.job
        job.status, job.started_at = JobStatus.RUNNING, now()
        self.store.save(state)
        progress = _JobProgress(self.store, state)
        try:
            pipeline = AnalysisPipeline(self.project_service, self._graph(), self._vectors())
            state.result = pipeline.run(project_id, progress, force_full=full)
            job.status = JobStatus.READY
        except AppError as error:  # expected: Neo4j or Qdrant down, model missing...
            logger.warning("Analysis of project %s failed: %s", project_id, error.message)
            job.status, job.error = JobStatus.FAILED, error.message
        except Exception:
            logger.exception("Analysis of project %s failed unexpectedly", project_id)
            job.status, job.error = JobStatus.FAILED, UNEXPECTED  # never the exception's text
        finally:
            job.finished_at = now()
            try:
                self.store.save(state)
            except OSError:  # e.g. the project was deleted during its analysis
                logger.warning("Could not save the analysis state of project %s", project_id)
            _release(project_id, job_id)


def _release(project_id: str, job_id: str) -> None:
    with _active_lock:
        if _active.get(project_id) == job_id:
            del _active[project_id]


class _JobProgress:
    """Saves what the pipeline reports into the job's state (see ProgressListener)."""

    def __init__(self, store: AnalysisStore, state: AnalysisState) -> None:
        self.store = store
        self.state = state
        self._saved_at = 0.0

    @property
    def job(self) -> AnalysisJob:
        assert self.state.job is not None
        return self.state.job

    def phase(self, name: str, total: int | None = None, unit: str | None = None) -> None:
        job = self.job
        job.phase, job.total, job.unit = name, total, unit
        job.completed = 0 if total is not None else None
        self._save()

    def advance(self, completed: int) -> None:
        self.job.completed = completed
        # Counts can change many times per second: save them now and then, and at the end.
        if completed == self.job.total or time.monotonic() - self._saved_at >= PROGRESS_SAVE_SECONDS:
            self._save()

    def mode(self, mode: str) -> None:
        self.job.mode = mode
        self._save()

    def writing(self) -> None:
        self.state.result = None  # the databases no longer match the previous result
        self._save()

    def _save(self) -> None:
        self.store.save(self.state)
        self._saved_at = time.monotonic()
