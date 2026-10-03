"""Advanced analysis endpoints: impact, dependencies and architecture of a project.

The routes only translate HTTP to a service call and back: the analyses are in
InsightsService and app/analysis, every error goes through the global AppError handler.
All three are read-only and bounded, and the client can pass an entity ID and a depth,
nothing else: no Cypher, labels or relationship types.
"""

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies import get_insights_service
from app.schemas.insights import ArchitectureResponse, DependencyResponse, ImpactResponse
from app.services.graph_retrieval_service import (
    DEFAULT_IMPACT_DEPTH,
    MAX_ID_LENGTH,
    MAX_TRAVERSAL_DEPTH,
)
from app.services.insights_service import InsightsService

router = APIRouter(prefix="/projects", tags=["advanced analysis"])

NOT_FOUND = {"description": "Unknown project, or an invalid project ID"}
UNAVAILABLE = {"description": "Neo4j is unavailable or misconfigured"}
PROJECT_ID = Path(description="ID returned when the project was imported")


# Plain `def` routes: the Neo4j reads (and the LLM call) block, FastAPI uses its thread pool.
@router.get(
    "/{project_id}/analysis/impact",
    response_model=ImpactResponse,
    summary="What may be affected if an entity changes",
    responses={404: {"description": "Unknown project or entity"}, 503: UNAVAILABLE},
)
def impact(
    project_id: str = PROJECT_ID,
    entity_id: str = Query(
        min_length=1, max_length=MAX_ID_LENGTH,
        description="ID of a node of the project's graph (as returned by GET .../graph)",
    ),
    depth: int = Query(
        DEFAULT_IMPACT_DEPTH, ge=1, le=MAX_TRAVERSAL_DEPTH,
        description="How many steps of references to follow",
    ),
    service: InsightsService = Depends(get_insights_service),
) -> ImpactResponse:  # fmt: skip
    """Entities that reference the given one, directly or through others, grouped by distance.

    Follows incoming CALLS, USES, INHERITS, IMPLEMENTS, IMPORTS and DEPENDS_ON
    relationships, starting from the entity and what it defines. Each affected entity
    appears once, at its smallest distance, with the relationship that reaches it.
    These are the references the analysis detected: dynamic calls are not seen.
    """
    return ImpactResponse.from_result(project_id, service.impact(project_id, entity_id, depth))


@router.get(
    "/{project_id}/analysis/dependencies",
    response_model=DependencyResponse,
    summary="File dependencies, circular dependencies and hubs",
    responses={404: NOT_FOUND, 503: UNAVAILABLE},
)
def dependencies(
    project_id: str = PROJECT_ID,
    service: InsightsService = Depends(get_insights_service),
) -> DependencyResponse:
    """How files depend on each other, computed from the graph (no LLM).

    Circular dependencies as readable paths, the files and entities most depended on
    (real counts), and the entities with no detected reference, which is not a proof
    that they are unused.
    """
    return DependencyResponse.from_analysis(project_id, service.dependencies(project_id))


@router.get(
    "/{project_id}/analysis/architecture",
    response_model=ArchitectureResponse,
    summary="Architecture facts and an LLM summary of them",
    responses={404: NOT_FOUND, 503: UNAVAILABLE},
)
def architecture(
    project_id: str = PROJECT_ID,
    service: InsightsService = Depends(get_insights_service),
) -> ArchitectureResponse:
    """Numbered facts computed from the graph, and a short LLM overview citing them.

    The LLM only receives the facts, never the repository, and is told not to name a
    pattern the facts do not state. If it is unavailable, the facts are returned with
    `summary: null` and a warning. This endpoint makes one LLM call.
    """
    return ArchitectureResponse.from_overview(project_id, service.architecture(project_id))
