"""Analysis endpoints: start the analysis of a project in the background, and follow it.

The routes only translate HTTP to a service call and back: jobs and their state are in
AnalysisService, every error goes through the global AppError handler (app/main.py).
"""

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies import get_analysis_service
from app.schemas.analysis import AnalysisStatusResponse
from app.services.analysis_service import AnalysisService

router = APIRouter(prefix="/projects", tags=["analysis"])

ERRORS = {
    404: {"description": "Unknown project, or an invalid project ID"},
    409: {"description": "The project is already being analyzed"},
}


@router.post(
    "/{project_id}/analyze",
    response_model=AnalysisStatusResponse,
    status_code=202,
    summary="Start analyzing a project (background job)",
    responses=ERRORS,  # type: ignore[arg-type]
)
def analyze(
    project_id: str = Path(description="ID returned when the project was imported"),
    full: bool = Query(
        False, description="Analyze everything again instead of only what changed"
    ),
    service: AnalysisService = Depends(get_analysis_service),
) -> AnalysisStatusResponse:
    """Queue the analysis and return at once (202): follow it with `GET .../analysis`.

    The job builds or updates the knowledge graph (Neo4j) and the semantic index
    (Qdrant). The first analysis processes everything. Later ones are incremental:
    only added and modified files are parsed, only changed code is embedded, and what
    was deleted is removed. No body. A project can have one analysis at a time (409).
    """
    return AnalysisStatusResponse.from_state(service.start(project_id, full=full))


@router.get(
    "/{project_id}/analysis",
    response_model=AnalysisStatusResponse,
    summary="Get the analysis state and progress of a project",
    responses={404: ERRORS[404]},  # type: ignore[dict-item]
)
def get_analysis(
    project_id: str = Path(description="ID returned when the project was imported"),
    service: AnalysisService = Depends(get_analysis_service),
) -> AnalysisStatusResponse:
    """The project's status, its current or last job with real progress, and its last report.

    `job.completed` / `job.total` count real work of the current phase (files parsed,
    chunks embedded) and are null for phases with nothing to count. Read from the
    project's workspace: no database is queried.
    """
    return AnalysisStatusResponse.from_state(service.get_state(project_id))
