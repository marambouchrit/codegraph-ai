"""The knowledge graph of a project, for display: GET /projects/{project_id}/graph.

    ProjectGraphService (this module)  the order of the steps, nothing else
        ├── ProjectService                 does the project exist?            (Phase 2)
        └── GraphRetrievalService          bounded, validated graph read      (Phase 7)
                └── GraphRepository            fixed read query, by project_id

No Cypher here. Retrieval is given as a factory, so an unknown project is a 404
before any Neo4j connection is created. Neo4j down raises its own AppError (503).
"""

from collections.abc import Callable

from app.graph.models import ProjectGraph
from app.services.graph_retrieval_service import DEFAULT_GRAPH_NODES, GraphRetrievalService
from app.services.project_service import ProjectService


class ProjectGraphService:
    def __init__(
        self,
        project_service: ProjectService,
        retrieval: Callable[[], GraphRetrievalService],
    ) -> None:
        self.project_service = project_service
        self._retrieval = retrieval

    def get_graph(self, project_id: str, limit: int = DEFAULT_GRAPH_NODES) -> ProjectGraph:
        """Up to `limit` nodes of the project and the edges between them (404 if unknown)."""
        self.project_service.get_project(project_id)  # invalid or unknown ID: 404, first
        return self._retrieval().get_project_graph(project_id, limit)
