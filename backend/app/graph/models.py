"""Data exchanged by the graph layer.

`GraphNode` and `GraphEdge` are what the builder sends to the repository: plain
data, already in the shape Neo4j stores. `GraphBuildReport` and `GraphStatistics`
are what the graph layer returns to its callers.
"""

from dataclasses import dataclass, field
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
