"""Neo4j operations of the knowledge graph. All the Cypher of the project lives here.

Writing uses batches: instead of one query per node, a list of rows is sent as a
single parameter and expanded by UNWIND, so one query handles `batch_size` rows:

    UNWIND $rows AS row                      -- one row per node
    MERGE (n:Entity {id: row.id})            -- find the node by ID, or create it
    SET n += row.properties                  -- then update its properties

MERGE makes every write idempotent: writing the same node or relationship twice
leaves one node or relationship. Each batch is its own transaction, so a large
project never needs one huge transaction.

Every value (IDs, names, paths, the project ID...) is a query parameter ($rows,
$project_id...), never pasted into the query text. Only labels and relationship
types are part of the text, because Cypher has no parameters for them; they come
from the fixed lists of `schema.py`.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, TypeVar

from app.extraction.models import EntityType
from app.graph.client import Neo4jClient, Transaction
from app.graph.models import GraphEdge, GraphNode, GraphStatistics
from app.graph.schema import (
    ENTITY_LABEL,
    NODE_LABELS,
    SCHEMA_STATEMENTS,
    node_label,
    relationship_type,
)

T = TypeVar("T")
Parameters = dict[str, Any]

ALL_NODE_LABELS = ":".join(NODE_LABELS.values())  # "File:Class:Interface:Function:Method"


# ----- Queries -----


def upsert_nodes_query(label: str) -> str:
    if label not in NODE_LABELS.values():
        raise ValueError(f"Unknown node label: {label!r}")
    # The type label is reset on every write, in case an entity changed type between
    # two builds (e.g. a function moved into a class keeps no stale label).
    return f"""
UNWIND $rows AS row
MERGE (n:{ENTITY_LABEL} {{id: row.id}})
SET n += row.properties, n.build_id = $build_id
REMOVE n:{ALL_NODE_LABELS}
SET n:{label}
RETURN count(n) AS count
"""


def upsert_relationships_query(type_: str) -> str:
    type_ = relationship_type(type_)
    # Both ends must already exist in this project: an edge can never reach another
    # project, and a missing node is never created by accident.
    return f"""
UNWIND $rows AS row
MATCH (source:{ENTITY_LABEL} {{id: row.source_id, project_id: $project_id}})
MATCH (target:{ENTITY_LABEL} {{id: row.target_id, project_id: $project_id}})
MERGE (source)-[r:{type_} {{id: row.id}}]->(target)
SET r += row.properties, r.build_id = $build_id
RETURN count(r) AS count
"""


DELETE_STALE_RELATIONSHIPS = f"""
MATCH (:{ENTITY_LABEL} {{project_id: $project_id}})-[r]->()
WHERE r.build_id IS NULL OR r.build_id <> $build_id
WITH r LIMIT $limit
DELETE r
RETURN count(r) AS count
"""

DELETE_STALE_NODES = f"""
MATCH (n:{ENTITY_LABEL} {{project_id: $project_id}})
WHERE n.build_id IS NULL OR n.build_id <> $build_id
WITH n LIMIT $limit
DETACH DELETE n
RETURN count(n) AS count
"""

DELETE_PROJECT_NODES = f"""
MATCH (n:{ENTITY_LABEL} {{project_id: $project_id}})
WITH n LIMIT $limit
DETACH DELETE n
RETURN count(n) AS count
"""

PROJECT_EXISTS = f"""
MATCH (n:{ENTITY_LABEL} {{project_id: $project_id}})
RETURN n.id AS id
LIMIT 1
"""

COUNT_NODES_BY_TYPE = f"""
MATCH (n:{ENTITY_LABEL} {{project_id: $project_id}})
RETURN n.entity_type AS entity_type, count(*) AS count
"""

COUNT_RELATIONSHIPS_BY_TYPE = f"""
MATCH (:{ENTITY_LABEL} {{project_id: $project_id}})-[r]->()
RETURN type(r) AS type, count(*) AS count
"""


# ----- Repository -----


class GraphRepository:
    def __init__(self, client: Neo4jClient, batch_size: int = 1000) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.client = client
        self.batch_size = batch_size

    def verify_connectivity(self) -> None:
        self.client.verify_connectivity()

    def ensure_schema(self) -> None:
        """Create the constraint and index of `schema.py` if they do not exist yet."""
        for statement in SCHEMA_STATEMENTS:
            # Schema changes cannot share a transaction with other statements.
            self.client.write(lambda tx, statement=statement: tx.run(statement).consume())

    def upsert_nodes(self, nodes: Sequence[GraphNode], build_id: str) -> int:
        """Create or update nodes (grouped by label, sent in batches). Returns the count."""
        written = 0
        for label, group in _group_by(nodes, lambda node: node.label).items():
            query = upsert_nodes_query(label)
            for batch in _batches(group, self.batch_size):
                rows = [{"id": node.id, "properties": node.properties} for node in batch]
                written += self._write_count(query, {"rows": rows, "build_id": build_id})
        return written

    def upsert_relationships(
        self, project_id: str, edges: Sequence[GraphEdge], build_id: str
    ) -> int:
        """Create or update relationships between existing nodes. Returns the count."""
        written = 0
        for type_, group in _group_by(edges, lambda edge: edge.type).items():
            query = upsert_relationships_query(type_)
            for batch in _batches(group, self.batch_size):
                rows = [
                    {
                        "id": edge.id,
                        "source_id": edge.source_id,
                        "target_id": edge.target_id,
                        "properties": edge.properties,
                    }
                    for edge in batch
                ]
                parameters = {"rows": rows, "project_id": project_id, "build_id": build_id}
                written += self._write_count(query, parameters)
        return written

    def delete_stale(self, project_id: str, build_id: str) -> tuple[int, int]:
        """Delete what an older build of this project left: (nodes, relationships) deleted."""
        parameters = {"project_id": project_id, "build_id": build_id}
        relationships = self._delete_in_batches(DELETE_STALE_RELATIONSHIPS, parameters)
        nodes = self._delete_in_batches(DELETE_STALE_NODES, parameters)
        return nodes, relationships

    def delete_project(self, project_id: str) -> int:
        """Delete the nodes (and their relationships) of one project only. Returns the count."""
        return self._delete_in_batches(DELETE_PROJECT_NODES, {"project_id": project_id})

    def project_exists(self, project_id: str) -> bool:
        records = self.client.read(
            lambda tx: tx.run(PROJECT_EXISTS, {"project_id": project_id}).data()
        )
        return bool(records)

    def statistics(self, project_id: str) -> GraphStatistics:
        parameters = {"project_id": project_id}

        def work(tx: Transaction) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            nodes = tx.run(COUNT_NODES_BY_TYPE, parameters).data()
            relationships = tx.run(COUNT_RELATIONSHIPS_BY_TYPE, parameters).data()
            return nodes, relationships

        node_rows, relationship_rows = self.client.read(work)
        return GraphStatistics(
            project_id=project_id,
            nodes_by_label=dict(
                sorted(
                    (node_label(EntityType(row["entity_type"])), int(row["count"]))
                    for row in node_rows
                )
            ),
            relationships_by_type=dict(
                sorted((row["type"], int(row["count"])) for row in relationship_rows)
            ),
        )

    # ----- Helpers -----

    def _write_count(self, query: str, parameters: Parameters) -> int:
        return self.client.write(lambda tx: _count(tx, query, parameters))

    def _delete_in_batches(self, query: str, parameters: Parameters) -> int:
        """Run a `... LIMIT $limit DELETE ...` query until nothing is left to delete.

        Deleting a large project in one transaction could exhaust Neo4j's memory, so
        at most `batch_size` items are deleted per transaction.
        """
        total = 0
        while True:
            deleted = self._write_count(query, {**parameters, "limit": self.batch_size})
            total += deleted
            if deleted < self.batch_size:
                return total


def _count(tx: Transaction, query: str, parameters: Parameters) -> int:
    records = tx.run(query, parameters).data()
    return int(records[0]["count"]) if records else 0


def _group_by(items: Iterable[T], key: Any) -> dict[str, list[T]]:
    """Group items by `key(item)`, keeping the order of first appearance (deterministic)."""
    groups: dict[str, list[T]] = defaultdict(list)
    for item in items:
        groups[key(item)].append(item)
    return dict(groups)


def _batches(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
