"""Graph endpoint: a bounded, read-only view of a project's knowledge graph, for display.

The route only translates HTTP to a service call and back: the workflow is in
ProjectGraphService, the query in GraphRepository, and every error goes through the
global AppError handler (app/main.py). The client chooses a node limit, nothing else:
no Cypher, labels or relationship types can be passed.
"""

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies import get_project_graph_service
from app.schemas.graph import ProjectGraphResponse
from app.services.graph_retrieval_service import DEFAULT_GRAPH_NODES, MAX_GRAPH_NODES
from app.services.project_graph_service import ProjectGraphService

router = APIRouter(prefix="/projects", tags=["graph"])

ERRORS = {
    404: {"description": "Unknown project, or an invalid project ID"},
    422: {"description": f"Invalid limit (an integer from 1 to {MAX_GRAPH_NODES})"},
    503: {"description": "Neo4j is unavailable or misconfigured"},
}


# A plain `def`: the Neo4j read is blocking, FastAPI runs it in a thread pool.
@router.get(
    "/{project_id}/graph",
    response_model=ProjectGraphResponse,
    summary="Get the knowledge graph of a project",
    responses=ERRORS,  # type: ignore[arg-type]
)
def get_graph(
    project_id: str = Path(description="ID returned when the project was imported"),
    limit: int = Query(
        DEFAULT_GRAPH_NODES, ge=1, le=MAX_GRAPH_NODES, description="Maximum number of nodes"
    ),
    service: ProjectGraphService = Depends(get_project_graph_service),
) -> ProjectGraphResponse:
    """Nodes (files, classes, interfaces, functions, methods) and the relationships between them.

    Bounded: up to `limit` nodes, structure first (files, then classes and interfaces,
    then functions, then methods), and the edges between those nodes only. `truncated`
    is true when the project has more; `total_nodes` and `total_edges` give its size.
    The same project always gives the same graph. An unanalyzed project has an empty
    graph.
    """
    return ProjectGraphResponse.from_graph(service.get_graph(project_id, limit))
