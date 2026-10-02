"""API model (response body) for the project graph endpoint.

Built from the Phase 7 ProjectGraph. Nodes keep the metadata Neo4j already stores
(Phase 6); columns, parent IDs and the internal build ID are left out.
"""

from pydantic import BaseModel, Field

from app.graph.models import EntityResult, ProjectGraph, RelationshipResult


class GraphNode(BaseModel):
    id: str = Field(description="Entity ID, referenced by the edges")
    entity_type: str = Field(description="file, class, interface, function or method")
    name: str
    qualified_name: str
    file_path: str = Field(description="Path in the repository")
    language: str
    start_line: int
    end_line: int

    @classmethod
    def from_entity(cls, entity: EntityResult) -> "GraphNode":
        return cls(
            id=entity.id,
            entity_type=entity.entity_type,
            name=entity.name,
            qualified_name=entity.qualified_name,
            file_path=entity.file_path,
            language=entity.language,
            start_line=entity.start_line,
            end_line=entity.end_line,
        )


class GraphEdge(BaseModel):
    id: str
    source: str = Field(description="ID of the source node")
    target: str = Field(description="ID of the target node")
    relationship_type: str = Field(
        description="CONTAINS, CALLS, IMPORTS, INHERITS, IMPLEMENTS, USES or DEPENDS_ON"
    )

    @classmethod
    def from_relationship(cls, relationship: RelationshipResult) -> "GraphEdge":
        return cls(
            id=relationship.id,
            source=relationship.source_id,
            target=relationship.target_id,
            relationship_type=relationship.type,
        )


class ProjectGraphResponse(BaseModel):
    project_id: str
    nodes: list[GraphNode]
    edges: list[GraphEdge] = Field(description="Only edges whose two ends are in `nodes`")
    truncated: bool = Field(
        description="true when the project has more nodes or edges than returned"
    )
    total_nodes: int = Field(description="Nodes in the whole project graph")
    total_edges: int = Field(description="Relationships in the whole project graph")

    @classmethod
    def from_graph(cls, graph: ProjectGraph) -> "ProjectGraphResponse":
        return cls(
            project_id=graph.project_id,
            nodes=[GraphNode.from_entity(node) for node in graph.nodes],
            edges=[GraphEdge.from_relationship(edge) for edge in graph.edges],
            truncated=graph.nodes_truncated or graph.edges_truncated,
            total_nodes=graph.total_nodes,
            total_edges=graph.total_edges,
        )
