"""Turn the result of Phase 5 into the Neo4j knowledge graph.

    RelationshipReport ──> graph_nodes()  Entity       -> GraphNode  (one per entity)
                       ──> graph_edges()  Relationship -> GraphEdge  (Phase 5 edges)
                                          parent_id    -> CONTAINS edge
                       ──> GraphRepository (MERGE in batches, then remove stale data)

The builder never reads or parses source code: everything comes from the report.
Unresolved references are only counted; they never become nodes or edges.

Re-building a project replaces its previous graph without ever deleting it first:
every node and relationship written by a build is stamped with that build's ID,
and once everything is written, the project's nodes and relationships with another
build ID (code deleted since the last analysis) are removed.

An incremental update (Phase 14) writes less: `diff_graph()` compares the nodes and
relationships of the new analysis with the fingerprints saved by the previous one, and
`apply()` writes only what differs and deletes only what disappeared.
"""

import logging
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.analysis.index import fingerprint

from app.extraction.models import Entity
from app.graph.models import GraphBuildReport, GraphEdge, GraphNode
from app.graph.repository import GraphRepository
from app.graph.schema import CONTAINS, contains_id, node_label
from app.relationships.models import Relationship, RelationshipReport

logger = logging.getLogger(__name__)


def entity_to_node(entity: Entity, project_id: str) -> GraphNode:
    if not entity.id.startswith(f"{project_id}:"):
        # Entity IDs always start with their project ID (Phase 4). Anything else would
        # mix two projects in the graph, so it is a bug, not something to store.
        raise ValueError(f"Entity {entity.id!r} does not belong to project {project_id!r}")
    return GraphNode(
        id=entity.id,
        label=node_label(entity.type),
        properties={
            "id": entity.id,
            "project_id": project_id,
            "entity_type": entity.type.value,
            "name": entity.name,
            "qualified_name": entity.qualified_name,
            "file_path": entity.file_path,
            "language": entity.language.value,
            "start_line": entity.start_line,
            "start_column": entity.start_column,
            "end_line": entity.end_line,
            "end_column": entity.end_column,
            # Also stored as a CONTAINS edge; kept as a property for simple lookups.
            # None for files: Neo4j removes a property set to null.
            "parent_id": entity.parent_id,
        },
    )


def relationship_to_edge(relationship: Relationship) -> GraphEdge:
    return GraphEdge(
        id=relationship.id,
        type=relationship.type.value,
        source_id=relationship.source_id,
        target_id=relationship.target_id,
        properties={
            "id": relationship.id,
            "file_path": relationship.file_path,
            "line": relationship.line,
            "column": relationship.column,
        },
    )


def contains_edge(entity: Entity) -> GraphEdge | None:
    if entity.parent_id is None:
        return None
    edge_id = contains_id(entity.parent_id, entity.id)
    return GraphEdge(
        id=edge_id,
        type=CONTAINS,
        source_id=entity.parent_id,
        target_id=entity.id,
        properties={"id": edge_id},
    )


def graph_nodes(report: RelationshipReport) -> list[GraphNode]:
    return [entity_to_node(entity, report.project_id) for entity in report.entities]


def graph_edges(report: RelationshipReport) -> list[GraphEdge]:
    """CONTAINS edges (entity hierarchy) followed by the Phase 5 relationships.

    An edge whose ends are not both entities of the report is dropped (with a warning):
    Phase 5 guarantees this never happens, and the graph must not get dangling edges.
    """
    node_ids = {entity.id for entity in report.entities}
    edges = [edge for entity in report.entities if (edge := contains_edge(entity)) is not None]
    edges += [relationship_to_edge(relationship) for relationship in report.relationships]
    valid = [edge for edge in edges if edge.source_id in node_ids and edge.target_id in node_ids]
    if len(valid) != len(edges):
        logger.warning("Dropped %d edges whose ends are unknown entities", len(edges) - len(valid))
    return valid


def node_fingerprint(node: GraphNode) -> str:
    return fingerprint([node.label, node.properties])


def edge_fingerprint(edge: GraphEdge) -> str:
    return fingerprint([edge.type, edge.source_id, edge.target_id, edge.properties])


@dataclass(frozen=True)
class GraphDiff:
    """What an incremental update must do to turn the previous graph into the new one."""

    nodes_to_write: tuple[GraphNode, ...]  # new, or with changed properties (lines moved...)
    edges_to_write: tuple[GraphEdge, ...]
    nodes_to_delete: tuple[str, ...]  # IDs
    edges_to_delete: tuple[tuple[str, str], ...]  # (relationship ID, source node ID)


def diff_graph(
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    previous_nodes: Mapping[str, str],
    previous_edges: Mapping[str, tuple[str, str]],
) -> GraphDiff:
    """Compare the new graph with the previous one.

    `previous_nodes` maps node IDs to fingerprints; `previous_edges` maps relationship
    IDs to (fingerprint, source node ID). Cross-file relationships need nothing special:
    a relationship that is no longer resolved (its target was deleted) is simply absent
    from `edges`, so it is deleted; a newly resolved one is written.
    """
    node_ids = {node.id for node in nodes}
    edge_ids = {edge.id for edge in edges}
    return GraphDiff(
        nodes_to_write=tuple(
            node for node in nodes if previous_nodes.get(node.id) != node_fingerprint(node)
        ),
        edges_to_write=tuple(
            edge for edge in edges
            if previous_edges.get(edge.id, ("", ""))[0] != edge_fingerprint(edge)
        ),  # fmt: skip
        nodes_to_delete=tuple(sorted(i for i in previous_nodes if i not in node_ids)),
        edges_to_delete=tuple(
            sorted((edge_id, source) for edge_id, (_, source) in previous_edges.items()
                   if edge_id not in edge_ids)
        ),  # fmt: skip
    )


@dataclass(frozen=True)
class GraphUpdateReport:
    nodes_written: int
    relationships_written: int
    nodes_deleted: int
    relationships_deleted: int


class GraphBuilder:
    def __init__(self, repository: GraphRepository) -> None:
        self.repository = repository

    def build(self, report: RelationshipReport, build_id: str | None = None) -> GraphBuildReport:
        """Write the entities and relationships of `report` to Neo4j (idempotent)."""
        started = time.perf_counter()
        build_id = build_id or uuid.uuid4().hex
        project_id = report.project_id
        nodes = graph_nodes(report)
        edges = graph_edges(report)

        self.repository.ensure_schema()
        nodes_written = self.repository.upsert_nodes(nodes, build_id)
        relationships_written = self.repository.upsert_relationships(project_id, edges, build_id)
        # Only once everything is written: if a batch fails, nothing is deleted (the graph
        # holds old and new data until the next successful build cleans it up).
        stale_nodes, stale_relationships = self.repository.delete_stale(project_id, build_id)

        result = GraphBuildReport(
            project_id=project_id,
            build_id=build_id,
            files=len(report.extraction.files),
            nodes_written=nodes_written,
            relationships_written=relationships_written,
            nodes_by_label=dict(sorted(Counter(node.label for node in nodes).items())),
            relationships_by_type=dict(sorted(Counter(edge.type for edge in edges).items())),
            stale_nodes_deleted=stale_nodes,
            stale_relationships_deleted=stale_relationships,
            unresolved_references=len(report.unresolved),
            failed_files=len(report.failures),
            duration_seconds=round(time.perf_counter() - started, 3),
        )
        logger.info("Project %s: %s", project_id, result.summary)
        return result

    def apply(
        self, project_id: str, diff: GraphDiff, build_id: str | None = None
    ) -> GraphUpdateReport:
        """Write an incremental update: new and changed first, then remove what disappeared.

        Untouched nodes and relationships are not sent to Neo4j at all. Writing before
        deleting keeps the rule of `build()`: if a batch fails, nothing was deleted yet.
        """
        build_id = build_id or uuid.uuid4().hex
        self.repository.ensure_schema()
        nodes_written = self.repository.upsert_nodes(diff.nodes_to_write, build_id)
        relationships_written = self.repository.upsert_relationships(
            project_id, diff.edges_to_write, build_id
        )
        # Relationships first: deleting a node also deletes its relationships, which
        # would then no longer be found (and counted).
        relationships_deleted = self.repository.delete_relationships(
            project_id, diff.edges_to_delete
        )
        nodes_deleted = self.repository.delete_nodes(project_id, diff.nodes_to_delete)
        return GraphUpdateReport(
            nodes_written, relationships_written, nodes_deleted, relationships_deleted
        )
