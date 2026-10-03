"""Data exchanged by the graph layer.

`GraphNode` and `GraphEdge` are what the builder sends to the repository: plain
data, already in the shape Neo4j stores. `GraphBuildReport` and `GraphStatistics`
are what the graph layer returns to its callers.

The retrieval models (`EntityResult`, `RelationshipResult`, `RelatedEntity`,
`GraphPath`, `GraphContext`) are what graph retrieval (Phase 7) reads back: never
raw Neo4j records.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Values Neo4j can store as properties: strings, numbers, booleans (None removes a property).
Properties = dict[str, Any]


@dataclass(frozen=True)
class GraphNode:
    id: str  # the Phase 4 entity ID: the node's identity in Neo4j
    label: str  # "File", "Class", "Interface", "Function" or "Method"
    properties: Properties  # includes id and project_id


@dataclass(frozen=True)
class GraphEdge:
    id: str  # the Phase 5 relationship ID, e.g. "CALLS:<source_id>-><target_id>"
    type: str  # "CALLS", "IMPORTS"... or "CONTAINS"
    source_id: str
    target_id: str
    properties: Properties  # includes id (and file_path, line, column for Phase 5 edges)


@dataclass(frozen=True)
class GraphBuildReport:
    """What one graph build did. Returned only when the build succeeded (errors raise)."""

    project_id: str
    build_id: str
    files: int
    nodes_written: int  # created or updated (MERGE)
    relationships_written: int  # created or updated (MERGE), CONTAINS included
    nodes_by_label: dict[str, int]
    relationships_by_type: dict[str, int]
    stale_nodes_deleted: int  # left from an older build (code removed since)
    stale_relationships_deleted: int
    unresolved_references: int  # Phase 5 references that did not become edges
    failed_files: int  # files Phase 3/4 could not read or parse
    duration_seconds: float

    @property
    def summary(self) -> str:
        return (
            f"Graph built successfully: {self.files:,} files, "
            f"{self.nodes_written:,} entities, {self.relationships_written:,} relationships"
        )


@dataclass(frozen=True)
class GraphStatistics:
    """What is stored in Neo4j for one project."""

    project_id: str
    nodes_by_label: dict[str, int] = field(default_factory=dict)
    relationships_by_type: dict[str, int] = field(default_factory=dict)

    @property
    def node_count(self) -> int:
        return sum(self.nodes_by_label.values())

    @property
    def relationship_count(self) -> int:
        return sum(self.relationships_by_type.values())


# ----- Retrieval (Phase 7) -----


class Direction(StrEnum):
    """Which way a relationship is followed from the entity asked about."""

    OUTGOING = "outgoing"  # (entity)-[r]->(other): what the entity calls, imports...
    INCOMING = "incoming"  # (other)-[r]->(entity): who calls it, who imports it...


@dataclass(frozen=True)
class EntityResult:
    """One node of the knowledge graph, as stored by Phase 6."""

    id: str
    entity_type: str  # "file", "class", "interface", "function" or "method"
    name: str
    qualified_name: str
    project_id: str
    file_path: str
    language: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    parent_id: str | None = None  # None for files

    @classmethod
    def from_properties(cls, properties: Properties) -> "EntityResult":
        return cls(
            id=properties["id"],
            entity_type=properties["entity_type"],
            name=properties["name"],
            qualified_name=properties["qualified_name"],
            project_id=properties["project_id"],
            file_path=properties["file_path"],
            language=properties["language"],
            start_line=int(properties["start_line"]),
            start_column=int(properties["start_column"]),
            end_line=int(properties["end_line"]),
            end_column=int(properties["end_column"]),
            parent_id=properties.get("parent_id"),
        )


@dataclass(frozen=True)
class RelationshipResult:
    """One edge of the knowledge graph. CONTAINS edges have no source location."""

    id: str
    type: str  # "CALLS", "IMPORTS"... or "CONTAINS"
    source_id: str
    target_id: str
    file_path: str | None = None  # where the relationship was first seen (Phase 5)
    line: int | None = None
    column: int | None = None

    @classmethod
    def from_record(
        cls, type_: str, properties: Properties, source_id: str, target_id: str
    ) -> "RelationshipResult":
        line, column = properties.get("line"), properties.get("column")
        return cls(
            id=properties["id"],
            type=type_,
            source_id=source_id,
            target_id=target_id,
            file_path=properties.get("file_path"),
            line=None if line is None else int(line),
            column=None if column is None else int(column),
        )


@dataclass(frozen=True)
class ProjectGraph:
    """A bounded view of one project's graph: some nodes and the edges between them."""

    project_id: str
    nodes: tuple[EntityResult, ...]
    edges: tuple[RelationshipResult, ...]  # both ends are in `nodes`
    nodes_truncated: bool  # the project has more nodes than returned
    edges_truncated: bool  # the returned nodes have more edges between them than returned
    total_nodes: int  # in the whole project graph
    total_edges: int


@dataclass(frozen=True)
class ImpactedEntity:
    """An entity that may be affected by a change, and how the change reaches it."""

    entity: EntityResult
    depth: int  # 1: it references the changed entity directly; 2: it references a depth-1...
    # The relationship that makes it affected: from this entity to one closer to the change.
    relationship: RelationshipResult


@dataclass(frozen=True)
class ImpactResult:
    """Who may be affected if `entity` changes (reverse references, bounded)."""

    entity: EntityResult
    contained: tuple[EntityResult, ...]  # what the entity defines: changes with it
    affected: tuple[ImpactedEntity, ...]  # closest first, each entity once
    max_depth: int
    truncated: bool  # more entities are affected than returned


@dataclass(frozen=True)
class RelatedEntity:
    """An entity reached from the entity asked about, and how it was reached.

    One hop: `relationship` is the edge followed and `depth` is 1. Several hops
    (transitive dependencies, nested containment): `relationship` is None and `depth`
    is the length of the shortest chain; use find_paths() to see the chain itself.
    """

    entity: EntityResult
    direction: Direction
    depth: int = 1
    relationship: RelationshipResult | None = None


@dataclass(frozen=True)
class GraphPath:
    """A chain of relationships: nodes[i] and nodes[i + 1] are linked by relationships[i]."""

    nodes: tuple[EntityResult, ...]
    relationships: tuple[RelationshipResult, ...]

    @property
    def length(self) -> int:
        return len(self.relationships)


@dataclass(frozen=True)
class GraphContext:
    """An entity with its parent and its direct neighbors, in both directions."""

    entity: EntityResult
    parent: EntityResult | None
    neighbors: tuple[RelatedEntity, ...]

    @property
    def entities(self) -> list[EntityResult]:
        """Every distinct entity of the context, the entity itself first."""
        found = {self.entity.id: self.entity}
        if self.parent is not None:
            found.setdefault(self.parent.id, self.parent)
        for neighbor in self.neighbors:
            found.setdefault(neighbor.entity.id, neighbor.entity)
        return list(found.values())

    @property
    def relationships(self) -> list[RelationshipResult]:
        return [n.relationship for n in self.neighbors if n.relationship is not None]
