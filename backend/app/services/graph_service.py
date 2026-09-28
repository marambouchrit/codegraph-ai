"""Build and manage the Neo4j knowledge graph of an imported project.

This service only orchestrates existing pieces, it contains no Cypher:

    ProjectService         the project's source folder and files      (Phase 2)
    RelationshipExtraction parse + entities + relationships, once     (Phases 3-5)
    GraphBuilder           entities -> nodes, relationships -> edges  (Phase 6)
    GraphRepository        Cypher, batches, MERGE                     (Phase 6)

Usage (the client owns the Neo4j connection, so close it when done):

    with Neo4jClient.from_settings(settings) as client:
        service = GraphService(settings, GraphRepository(client, settings.graph_batch_size))
        report = service.build_project_graph(project_id)
        print(report.summary)
"""

from app.core.config import Settings
from app.extraction.service import EntityExtractionService
from app.graph.builder import GraphBuilder
from app.graph.models import GraphBuildReport, GraphStatistics
from app.graph.repository import GraphRepository
from app.parsing.service import ParserService
from app.relationships.models import RelationshipReport
from app.relationships.service import RelationshipExtractionService
from app.services.project_service import ProjectService


class GraphService:
    def __init__(
        self,
        settings: Settings,
        repository: GraphRepository,
        project_service: ProjectService | None = None,
        relationship_service: RelationshipExtractionService | None = None,
    ) -> None:
        self.repository = repository
        self.builder = GraphBuilder(repository)
        self.project_service = project_service or ProjectService(settings)
        if relationship_service is None:
            parser_service = ParserService(max_file_bytes=settings.max_source_file_kb * 1024)
            relationship_service = RelationshipExtractionService(
                EntityExtractionService(parser_service)
            )
        self.relationship_service = relationship_service

    def analyze_project(self, project_id: str) -> RelationshipReport:
        """Run Phases 3-5 on an imported project (raises ProjectNotFoundError if unknown)."""
        files = self.project_service.list_files(project_id)
        source_dir = self.project_service.workspace.source_dir(project_id)
        return self.relationship_service.extract_project(project_id, source_dir, files)

    def build_project_graph(self, project_id: str) -> GraphBuildReport:
        """Analyze the project and write (or refresh) its knowledge graph in Neo4j."""
        self.project_service.get_project(project_id)  # unknown project: fail before anything
        # Fail fast if Neo4j is down, before spending time on the analysis.
        self.repository.verify_connectivity()
        return self.builder.build(self.analyze_project(project_id))

    def delete_project_graph(self, project_id: str) -> int:
        """Remove one project's graph (other projects are untouched). Returns the nodes deleted."""
        return self.repository.delete_project(project_id)

    def project_graph_exists(self, project_id: str) -> bool:
        return self.repository.project_exists(project_id)

    def get_statistics(self, project_id: str) -> GraphStatistics:
        return self.repository.statistics(project_id)
