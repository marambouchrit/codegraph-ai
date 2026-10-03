"""The analysis state of each project: its current job, its progress, its last result.

One small JSON file per project, next to its project.json:

    <workspace>/<project_id>/analysis.json

    job     the latest analysis job: queued, running (with its phase and real
            completed/total counts), ready or failed (with a safe error message)
    result  the report of the last analysis whose data is still in Neo4j and Qdrant

The status of a project is read from these two:

    queued / running   a job is waiting or working
    failed             the latest job failed (`result` is kept when the previous
                       analysis is still valid: the failure happened before any write)
    ready              the latest job succeeded
    not_analyzed       no job and no result

`result` is removed the moment an analysis starts writing to the databases and set
again only when it finished: the file never says "ready" for data that is half
written. No graph or vector data is copied here: Neo4j and Qdrant keep those.
A missing or unreadable file means "not analyzed".
"""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.ingestion.workspace import Workspace

logger = logging.getLogger(__name__)

STATE_VERSION = 2


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


ACTIVE = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class AnalysisJob:
    job_id: str
    status: JobStatus
    queued_at: datetime
    mode: str | None = None  # "full" or "incremental", known once changes are detected
    phase: str | None = None  # see analysis_pipeline.PHASES
    # Real counts of the current phase, or None when the phase has nothing to count.
    completed: int | None = None
    total: int | None = None
    unit: str | None = None  # "files" or "chunks"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None  # a message that is safe to show


@dataclass(frozen=True)
class AnalysisChanges:
    """What one analysis actually did (a full analysis processes everything)."""

    files_added: int = 0
    files_modified: int = 0
    files_unchanged: int = 0
    files_deleted: int = 0
    files_parsed: int = 0
    nodes_written: int = 0
    nodes_deleted: int = 0
    relationships_written: int = 0
    relationships_deleted: int = 0
    chunks_embedded: int = 0  # embeddings generated
    chunks_reused: int = 0  # vectors kept as they were
    chunks_updated: int = 0  # same vector, new metadata (e.g. the lines moved)
    chunks_deleted: int = 0


@dataclass(frozen=True)
class AnalysisResult:
    """The project after a successful analysis (totals), and what that analysis changed."""

    project_id: str
    mode: str  # "full" or "incremental"
    files: int
    failed_files: int
    entities: int
    relationships: int  # CONTAINS included
    entities_by_type: dict[str, int]
    relationships_by_type: dict[str, int]
    unresolved_references: int
    chunks: int
    chunks_by_type: dict[str, int]
    embedding_model: str
    changes: AnalysisChanges
    duration_seconds: float
    analyzed_at: datetime = field(default_factory=now)


@dataclass
class AnalysisState:
    project_id: str
    job: AnalysisJob | None = None
    result: AnalysisResult | None = None

    @property
    def status(self) -> str:
        if self.job is not None and self.job.status in (*ACTIVE, JobStatus.FAILED):
            return self.job.status.value
        return "ready" if self.result is not None else "not_analyzed"


# One process, several threads (requests and the analysis worker): reads and writes of
# the state files take turns, so a reader never opens a file being replaced.
_file_lock = threading.Lock()


class AnalysisStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def load(self, project_id: str) -> AnalysisState:
        """The saved state; an empty one if there is none or it cannot be read."""
        path = self.workspace.analysis_file(project_id)
        with _file_lock:
            if not path.is_file():
                return AnalysisState(project_id)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                state = _state_from_dict(project_id, data)
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("Ignoring the analysis state of project %s: unreadable", project_id)
                return AnalysisState(project_id)
        if state.result is not None and state.result.project_id != project_id:
            logger.warning("Ignoring the analysis state of project %s: wrong project", project_id)
            return AnalysisState(project_id)
        return state

    def save(self, state: AnalysisState) -> None:
        data = {
            "version": STATE_VERSION,
            "job": None if state.job is None else asdict(state.job),
            "result": None if state.result is None else asdict(state.result),
        }
        text = json.dumps(data, indent=2, default=_json_default)
        with _file_lock:
            _write_atomically(self.workspace.analysis_file(state.project_id), text)

    def clear(self, project_id: str) -> None:
        with _file_lock:
            self.workspace.analysis_file(project_id).unlink(missing_ok=True)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot store {type(value).__name__} in the analysis state")


def _write_atomically(path: Path, text: str) -> None:
    # Write a temporary file, then rename it: a crash never leaves a half-written state.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:  # Windows: another program (an antivirus...) holds the file
            if attempt == 4:
                raise
            time.sleep(0.05)


# ----- JSON -> dataclasses -----


def _datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _state_from_dict(project_id: str, data: dict[str, Any]) -> AnalysisState:
    if "version" not in data:  # written by Phase 13: only the reports of a finished analysis
        return AnalysisState(project_id, result=_legacy_result(data))
    if data["version"] != STATE_VERSION:
        raise ValueError("unknown analysis state version")
    job, result = data["job"], data["result"]
    return AnalysisState(
        project_id,
        job=None if job is None else _job(job),
        result=None if result is None else _result(result),
    )


def _job(data: dict[str, Any]) -> AnalysisJob:
    return AnalysisJob(
        **{
            **data,
            "status": JobStatus(data["status"]),
            "queued_at": datetime.fromisoformat(data["queued_at"]),
            "started_at": _datetime(data["started_at"]),
            "finished_at": _datetime(data["finished_at"]),
        }
    )


def _result(data: dict[str, Any]) -> AnalysisResult:
    return AnalysisResult(
        **{
            **data,
            "changes": AnalysisChanges(**data["changes"]),
            "analyzed_at": datetime.fromisoformat(data["analyzed_at"]),
        }
    )


def _legacy_result(data: dict[str, Any]) -> AnalysisResult:
    """A Phase 13 record (the two build reports): the same totals, as a full analysis."""
    graph, vectors = data["graph"], data["vectors"]
    return AnalysisResult(
        project_id=graph["project_id"],
        mode="full",
        files=int(graph["files"]),
        failed_files=max(int(graph["failed_files"]), int(vectors["failed_files"])),
        entities=int(graph["nodes_written"]),
        relationships=int(graph["relationships_written"]),
        entities_by_type=dict(graph["nodes_by_label"]),
        relationships_by_type=dict(graph["relationships_by_type"]),
        unresolved_references=int(graph["unresolved_references"]),
        chunks=int(vectors["chunks"]),
        chunks_by_type=dict(vectors["chunks_by_type"]),
        embedding_model=vectors["embedding_model"],
        changes=AnalysisChanges(),
        duration_seconds=float(data["duration_seconds"]),
        analyzed_at=datetime.fromisoformat(data["analyzed_at"]),
    )
