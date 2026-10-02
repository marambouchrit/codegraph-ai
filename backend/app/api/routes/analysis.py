"""Analysis endpoint: prepare an imported project for chat (knowledge graph + vector index).

The route only translates HTTP to a service call and back: the workflow is in
AnalysisService, every error goes through the global AppError handler (app/main.py).
"""

from fastapi import APIRouter, Depends, Path

from app.api.dependencies import get_analysis_service
from app.schemas.analysis import AnalysisResponse
from app.services.analysis_service import AnalysisService

router = APIRouter(prefix="/projects", tags=["analysis"])

ERRORS = {
    404: {"description": "Unknown project, or an invalid project ID"},
    409: {"description": "The project is already being analyzed"},
    503: {"description": "Neo4j, Qdrant or the embedding model is unavailable or misconfigured"},
}


# A plain `def`: parsing, Neo4j writes and embedding are blocking, run in the thread pool.
@router.post(
    "/{project_id}/analyze",
    response_model=AnalysisResponse,
    summary="Analyze and index a project for chat",
    responses=ERRORS,  # type: ignore[arg-type]
)
def analyze(
    project_id: str = Path(description="ID returned when the project was imported"),
    service: AnalysisService = Depends(get_analysis_service),
) -> AnalysisResponse:
    """Build the project's knowledge graph (Neo4j) and semantic index (Qdrant, BGE-M3).

    Run it once after importing a project, before `POST /projects/{id}/chat`, and again
    after the code changes: re-analysis refreshes both without duplicates. No body.

    It is synchronous: expect seconds for a small project, minutes for a large one on a
    CPU (the first call also loads the embedding model). `status` is `ready` only when
    both steps succeeded; otherwise an error is returned.
    """
    return AnalysisResponse.from_result(project_id, service.analyze(project_id))
