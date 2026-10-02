"""Remember the last successful analysis of each project, so a reload knows it is ready.

One small JSON file per project, next to its project.json:

    <workspace>/<project_id>/analysis.json

It holds the real Phase 6 and Phase 8 reports (counts, model, warnings...) and the time
of the analysis. No graph or vector data is copied: Neo4j and Qdrant keep those.
Deleting the project deletes its folder, and the file with it.

The file exists only after a complete analysis: AnalysisService clears it before
starting and writes it after both steps succeeded. A missing, unreadable or outdated
file means "not analyzed" (projects analyzed before this file existed, too).
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.graph.models import GraphBuildReport
from app.ingestion.workspace import Workspace
from app.rag.models import VectorIndexReport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalysisResult:
    """Both reports of a successful analysis, and when it finished."""

    graph: GraphBuildReport
    vectors: VectorIndexReport
    duration_seconds: float
    analyzed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AnalysisStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def load(self, project_id: str) -> AnalysisResult | None:
        """The last successful analysis, or None if there is none (or it can't be read)."""
        path = self.workspace.analysis_file(project_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result = AnalysisResult(
                graph=GraphBuildReport(**data["graph"]),
                vectors=VectorIndexReport(**data["vectors"]),
                duration_seconds=float(data["duration_seconds"]),
                analyzed_at=datetime.fromisoformat(data["analyzed_at"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Ignoring the analysis record of project %s: unreadable", project_id)
            return None
        if result.graph.project_id != project_id or result.vectors.project_id != project_id:
            logger.warning("Ignoring the analysis record of project %s: wrong project", project_id)
            return None
        return result

    def save(self, project_id: str, result: AnalysisResult) -> None:
        data = {
            "graph": asdict(result.graph),
            "vectors": asdict(result.vectors),
            "duration_seconds": result.duration_seconds,
            "analyzed_at": result.analyzed_at.isoformat(),
        }
        _write_atomically(self.workspace.analysis_file(project_id), json.dumps(data, indent=2))

    def clear(self, project_id: str) -> None:
        self.workspace.analysis_file(project_id).unlink(missing_ok=True)


def _write_atomically(path: Path, text: str) -> None:
    # Write a temporary file, then rename it: a crash never leaves a half-written record.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
