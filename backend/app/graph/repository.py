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

Reading (graph retrieval, Phase 7) follows the same rules. There is no "run this
Cypher" method: each operation is a fixed query shape. Besides labels and types,
only two things are written into a retrieval query's text: the direction of the
arrows (from the `Direction` enum) and the maximum traversal depth of a
variable-length pattern such as [:DEPENDS_ON*1..3], which Cypher cannot take as a
parameter either; `traversal_depth()` only lets through an int from 1 to
MAX_TRAVERSAL_DEPTH. Every retrieval query matches its nodes with
{project_id: $project_id}, so it can never return another project's data.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, TypeVar

from app.extraction.models import EntityType
from app.graph.client import Neo4jClient, Transaction
from app.graph.models import (
    Direction,
    EntityResult,
    GraphEdge,
    GraphNode,
    GraphPath,
    GraphStatistics,
    ProjectGraph,
    RelatedEntity,
    RelationshipResult,
)
from app.graph.schema import (
    CONTAINS,
    ENTITY_LABEL,
    NODE_LABELS,
    RELATIONSHIP_TYPES,
    SCHEMA_STATEMENTS,
    node_label,
    relationship_type,
)
from app.relationships.models import RelationshipType

T = TypeVar("T")
Parameters = dict[str, Any]

ALL_NODE_LABELS = ":".join(NODE_LABELS.values())  # "File:Class:Interface:Function:Method"

# Variable-length patterns ([:DEPENDS_ON*1..N]) can visit a number of paths that grows
# exponentially with N, so N is always bounded, even when the caller asks for more.
MAX_TRAVERSAL_DEPTH = 5


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

# Incremental updates (Phase 14): remove exactly the nodes and relationships that no
# longer exist in the code, found by ID (through the unique-ID index) inside one project.
DELETE_NODES_BY_ID = f"""
UNWIND $ids AS id
MATCH (n:{ENTITY_LABEL} {{id: id, project_id: $project_id}})
DETACH DELETE n
RETURN count(n) AS count
"""

DELETE_RELATIONSHIPS_BY_ID = f"""
UNWIND $rows AS row
MATCH (:{ENTITY_LABEL} {{id: row.source_id, project_id: $project_id}})-[r]->()
WHERE r.id = row.id
DELETE r
RETURN count(r) AS count
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


# ----- Retrieval queries (read only) -----

GET_ENTITY = f"""
MATCH (n:{ENTITY_LABEL} {{id: $entity_id, project_id: $project_id}})
RETURN properties(n) AS entity
"""

# A bounded view of a whole project's graph, for display (GET /projects/{id}/graph).
# Nodes in a fixed order, structure first (files, then classes and interfaces, then
# functions, then methods), so a truncated graph keeps its skeleton and the same
# project always gives the same graph. $limit is the caller's limit + 1: one extra
# row tells whether there was more.
PROJECT_GRAPH_NODES = f"""
MATCH (n:{ENTITY_LABEL} {{project_id: $project_id}})
RETURN properties(n) AS entity
ORDER BY CASE n.entity_type
    WHEN 'file' THEN 0 WHEN 'class' THEN 1 WHEN 'interface' THEN 1 WHEN 'function' THEN 2
    ELSE 3 END, n.file_path, n.start_line, n.id
LIMIT $limit
"""

# The relationships between the selected nodes only (both ends in $ids), of our own types.
PROJECT_GRAPH_EDGES = f"""
MATCH (source:{ENTITY_LABEL} {{project_id: $project_id}})-[r]->(target:{ENTITY_LABEL} {{project_id: $project_id}})
WHERE source.id IN $ids AND target.id IN $ids AND type(r) IN $types
RETURN type(r) AS type, properties(r) AS relationship, source.id AS source_id, target.id AS target_id
ORDER BY type, source_id, target_id, r.id
LIMIT $limit
"""

# Exact matches first (ID, then qualified name, then name), then partial matches if
# asked for. Several entities with the same name are all returned, never one at random.
FIND_ENTITIES = f"""
MATCH (n:{ENTITY_LABEL} {{project_id: $project_id}})
WITH n, CASE
    WHEN n.id = $text THEN 0
    WHEN n.qualified_name = $text THEN 1
    WHEN n.name = $text THEN 2
    WHEN $partial AND toLower(n.qualified_name) CONTAINS toLower($text) THEN 3
  END AS rank
WHERE rank IS NOT NULL
  AND ($entity_types IS NULL OR n.entity_type IN $entity_types)
RETURN properties(n) AS entity
ORDER BY rank, n.qualified_name, n.file_path, n.id
LIMIT $limit
"""


def traversal_depth(max_depth: int) -> int:
    """Check a depth before it is written into a query ([:TYPE*1..<depth>])."""
    if (
        not isinstance(max_depth, int)
        or isinstance(max_depth, bool)
        or not 1 <= max_depth <= MAX_TRAVERSAL_DEPTH
    ):
        raise ValueError(f"max_depth must be an integer from 1 to {MAX_TRAVERSAL_DEPTH}")
    return max_depth


def relationship_types(types: Iterable[str]) -> str:
    """Whitelisted types joined for a pattern ("CALLS|USES"), sorted: one set, one query."""
    checked = sorted({relationship_type(type_) for type_ in types})
    if not checked:
        raise ValueError("At least one relationship type is required")
    return "|".join(checked)


def _arrows(direction: Direction) -> tuple[str, str]:
    """The arrow parts around a relationship pattern, seen from the start node."""
    if direction == Direction.OUTGOING:
        return "-", "->"
    if direction == Direction.INCOMING:
        return "<-", "-"
    raise ValueError(f"Unknown direction: {direction!r}")


def related_query(types: Iterable[str], direction: Direction) -> str:
    """Entities one relationship away from the start entity (both ends in the project)."""
    left, right = _arrows(direction)
    return f"""
MATCH (start:{ENTITY_LABEL} {{id: $entity_id, project_id: $project_id}})\
{left}[r:{relationship_types(types)}]{right}(other:{ENTITY_LABEL} {{project_id: $project_id}})
WHERE $entity_types IS NULL OR other.entity_type IN $entity_types
RETURN properties(other) AS entity, type(r) AS type, properties(r) AS relationship,
       startNode(r).id AS source_id, endNode(r).id AS target_id
ORDER BY type(r), other.file_path, other.start_line, other.id
LIMIT $limit
"""


def reachable_query(types: Iterable[str], direction: Direction, max_depth: int) -> str:
    """Entities 1 to max_depth relationships away, each once, at its shortest distance."""
    left, right = _arrows(direction)
    return f"""
MATCH path = (start:{ENTITY_LABEL} {{id: $entity_id, project_id: $project_id}})\
{left}[:{relationship_types(types)}*1..{traversal_depth(max_depth)}]{right}\
(other:{ENTITY_LABEL} {{project_id: $project_id}})
WHERE other <> start
  AND all(n IN nodes(path) WHERE n.project_id = $project_id)
  AND ($entity_types IS NULL OR other.entity_type IN $entity_types)
WITH other, min(length(path)) AS depth
RETURN properties(other) AS entity, depth
ORDER BY depth, other.file_path, other.start_line, other.id
LIMIT $limit
"""


def paths_query(types: Iterable[str] | None, max_depth: int, directed: bool) -> str:
    """The shortest paths between two entities, sorted by the IDs along them."""
    pattern = f":{relationship_types(types)}" if types is not None else ""
    arrow = "->" if directed else "-"
    return f"""
MATCH (source:{ENTITY_LABEL} {{id: $source_id, project_id: $project_id}})
MATCH (target:{ENTITY_LABEL} {{id: $target_id, project_id: $project_id}})
MATCH path = allShortestPaths(\
(source)-[{pattern}*1..{traversal_depth(max_depth)}]{arrow}(target))
WHERE all(n IN nodes(path) WHERE n.project_id = $project_id)
WITH path, [n IN nodes(path) | n.id] AS node_ids, [r IN relationships(path) | r.id] AS edge_ids
ORDER BY node_ids, edge_ids
LIMIT $limit
RETURN [n IN nodes(path) | properties(n)] AS nodes,
       [r IN relationships(path) | {{type: type(r), properties: properties(r),
                                    source_id: startNode(r).id, target_id: endNode(r).id}}]
         AS relationships
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

    def delete_nodes(self, project_id: str, node_ids: Sequence[str]) -> int:
        """Delete these nodes of the project, with their relationships. Returns the count."""
        deleted = 0
        for batch in _batches(list(node_ids), self.batch_size):
            deleted += self._write_count(
                DELETE_NODES_BY_ID, {"project_id": project_id, "ids": list(batch)}
            )
        return deleted

    def delete_relationships(self, project_id: str, edges: Sequence[tuple[str, str]]) -> int:
        """Delete relationships given as (relationship ID, source node ID). Returns the count."""
        deleted = 0
        for batch in _batches(list(edges), self.batch_size):
            rows = [{"id": edge_id, "source_id": source_id} for edge_id, source_id in batch]
            deleted += self._write_count(
                DELETE_RELATIONSHIPS_BY_ID, {"project_id": project_id, "rows": rows}
            )
        return deleted

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

    # ----- Retrieval (read only, always scoped by project_id) -----
    #
    # These methods trust their arguments to be sensible (the service validates limits
    # and turns "not found" into errors); they only refuse what could change the query
    # text: unknown relationship types, directions or depths.

    def get_project_graph(
        self, project_id: str, *, node_limit: int, edge_limit: int
    ) -> ProjectGraph:
        """Up to `node_limit` nodes of the project and up to `edge_limit` edges between them."""
        rows = self._read(PROJECT_GRAPH_NODES, {"project_id": project_id, "limit": node_limit + 1})
        nodes = [EntityResult.from_properties(row["entity"]) for row in rows[:node_limit]]
        edges: list[RelationshipResult] = []
        edges_truncated = False
        if nodes:
            parameters = {
                "project_id": project_id,
                "ids": [node.id for node in nodes],
                "types": sorted(RELATIONSHIP_TYPES),
                "limit": edge_limit + 1,
            }
            edge_rows = self._read(PROJECT_GRAPH_EDGES, parameters)
            edges = [
                RelationshipResult.from_record(
                    row["type"], row["relationship"], row["source_id"], row["target_id"]
                )
                for row in edge_rows[:edge_limit]
            ]
            edges_truncated = len(edge_rows) > edge_limit
        totals = self.statistics(project_id)
        return ProjectGraph(
            project_id=project_id,
            nodes=tuple(nodes),
            edges=tuple(edges),
            nodes_truncated=len(rows) > node_limit,
            edges_truncated=edges_truncated,
            total_nodes=sum(totals.nodes_by_label.values()),
            total_edges=sum(totals.relationships_by_type.values()),
        )

    def get_entity(self, project_id: str, entity_id: str) -> EntityResult | None:
        rows = self._read(GET_ENTITY, {"project_id": project_id, "entity_id": entity_id})
        return EntityResult.from_properties(rows[0]["entity"]) if rows else None

    def find_entities(
        self,
        project_id: str,
        text: str,
        *,
        limit: int,
        entity_types: Sequence[EntityType] | None = None,
        partial: bool = False,
    ) -> list[EntityResult]:
        """Entities whose ID, qualified name or name is `text` (or contains it, if partial)."""
        parameters = {
            "project_id": project_id,
            "text": text,
            "partial": partial,
            "entity_types": _type_values(entity_types),
            "limit": limit,
        }
        rows = self._read(FIND_ENTITIES, parameters)
        return [EntityResult.from_properties(row["entity"]) for row in rows]

    def get_related(
        self,
        project_id: str,
        entity_id: str,
        types: Iterable[str],
        direction: Direction,
        *,
        limit: int,
        entity_types: Sequence[EntityType] | None = None,
    ) -> list[RelatedEntity]:
        """Entities linked to `entity_id` by one relationship of `types`, with that relationship."""
        rows = self._read(
            related_query(types, direction),
            {
                "project_id": project_id,
                "entity_id": entity_id,
                "entity_types": _type_values(entity_types),
                "limit": limit,
            },
        )
        return [
            RelatedEntity(
                entity=EntityResult.from_properties(row["entity"]),
                direction=direction,
                relationship=RelationshipResult.from_record(
                    row["type"], row["relationship"], row["source_id"], row["target_id"]
                ),
            )
            for row in rows
        ]

    def get_reachable(
        self,
        project_id: str,
        entity_id: str,
        types: Iterable[str],
        direction: Direction,
        max_depth: int,
        *,
        limit: int,
        entity_types: Sequence[EntityType] | None = None,
    ) -> list[RelatedEntity]:
        """Entities 1 to `max_depth` relationships of `types` away, closest first."""
        rows = self._read(
            reachable_query(types, direction, max_depth),
            {
                "project_id": project_id,
                "entity_id": entity_id,
                "entity_types": _type_values(entity_types),
                "limit": limit,
            },
        )
        return [
            RelatedEntity(
                entity=EntityResult.from_properties(row["entity"]),
                direction=direction,
                depth=int(row["depth"]),
            )
            for row in rows
        ]

    def get_neighbors(self, project_id: str, entity_id: str, *, limit: int) -> list[RelatedEntity]:
        """Every direct neighbor: up to `limit` outgoing, then up to `limit` incoming."""
        return [
            neighbor
            for direction in (Direction.OUTGOING, Direction.INCOMING)
            for neighbor in self.get_related(
                project_id, entity_id, RELATIONSHIP_TYPES, direction, limit=limit
            )
        ]

    def get_contained_entities(
        self,
        project_id: str,
        entity_id: str,
        *,
        limit: int,
        max_depth: int = 1,
        entity_types: Sequence[EntityType] | None = None,
    ) -> list[RelatedEntity]:
        """What a file or class defines (CONTAINS); nested definitions too if max_depth > 1."""
        return self._follow(
            project_id, entity_id, CONTAINS, Direction.OUTGOING, max_depth, limit, entity_types
        )

    def get_callers(self, project_id: str, entity_id: str, *, limit: int) -> list[RelatedEntity]:
        return self.get_related(
            project_id, entity_id, [RelationshipType.CALLS], Direction.INCOMING, limit=limit
        )

    def get_callees(self, project_id: str, entity_id: str, *, limit: int) -> list[RelatedEntity]:
        return self.get_related(
            project_id, entity_id, [RelationshipType.CALLS], Direction.OUTGOING, limit=limit
        )

    def get_imports(self, project_id: str, file_id: str, *, limit: int) -> list[RelatedEntity]:
        return self.get_related(
            project_id, file_id, [RelationshipType.IMPORTS], Direction.OUTGOING, limit=limit
        )

    def get_importers(self, project_id: str, file_id: str, *, limit: int) -> list[RelatedEntity]:
        return self.get_related(
            project_id, file_id, [RelationshipType.IMPORTS], Direction.INCOMING, limit=limit
        )

    def get_dependencies(
        self,
        project_id: str,
        file_id: str,
        *,
        limit: int,
        max_depth: int = 1,
        direction: Direction = Direction.OUTGOING,
    ) -> list[RelatedEntity]:
        """DEPENDS_ON: what the file depends on (OUTGOING) or what depends on it (INCOMING)."""
        return self._follow(
            project_id, file_id, RelationshipType.DEPENDS_ON, direction, max_depth, limit, None
        )

    def get_inheritance(
        self, project_id: str, entity_id: str, direction: Direction, *, limit: int
    ) -> list[RelatedEntity]:
        """INHERITS: the parents (OUTGOING) or the direct subclasses (INCOMING)."""
        return self.get_related(
            project_id, entity_id, [RelationshipType.INHERITS], direction, limit=limit
        )

    def get_implementations(
        self, project_id: str, entity_id: str, direction: Direction, *, limit: int
    ) -> list[RelatedEntity]:
        """IMPLEMENTS: a class's interfaces (OUTGOING) or an interface's classes (INCOMING)."""
        return self.get_related(
            project_id, entity_id, [RelationshipType.IMPLEMENTS], direction, limit=limit
        )

    def find_paths(
        self,
        project_id: str,
        source_id: str,
        target_id: str,
        *,
        max_depth: int,
        limit: int,
        types: Iterable[str] | None = None,
        directed: bool = True,
    ) -> list[GraphPath]:
        """The shortest paths from source to target (none: an empty list), at most `limit`.

        `types` restricts the relationships followed (None: all of them). Directed paths
        follow the arrows (login -CALLS-> save); undirected ones ignore them.
        """
        rows = self._read(
            paths_query(types, max_depth, directed),
            {
                "project_id": project_id,
                "source_id": source_id,
                "target_id": target_id,
                "limit": limit,
            },
        )
        return [
            GraphPath(
                nodes=tuple(EntityResult.from_properties(node) for node in row["nodes"]),
                relationships=tuple(
                    RelationshipResult.from_record(
                        edge["type"], edge["properties"], edge["source_id"], edge["target_id"]
                    )
                    for edge in row["relationships"]
                ),
            )
            for row in rows
        ]

    # ----- Helpers -----

    def _read(self, query: str, parameters: Parameters) -> list[dict[str, Any]]:
        return self.client.read(lambda tx: tx.run(query, parameters).data())

    def _follow(
        self,
        project_id: str,
        entity_id: str,
        type_: str,
        direction: Direction,
        max_depth: int,
        limit: int,
        entity_types: Sequence[EntityType] | None,
    ) -> list[RelatedEntity]:
        """One hop keeps the relationship (location of the import...); more hops, the depth."""
        if traversal_depth(max_depth) == 1:
            return self.get_related(
                project_id, entity_id, [type_], direction, limit=limit, entity_types=entity_types
            )
        return self.get_reachable(
            project_id, entity_id, [type_], direction, max_depth,
            limit=limit, entity_types=entity_types,
        )  # fmt: skip

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


def _type_values(entity_types: Sequence[EntityType] | None) -> list[str] | None:
    """["class", "method"] for the $entity_types parameter (None: no filter)."""
    if entity_types is None:
        return None
    return sorted({EntityType(entity_type).value for entity_type in entity_types})


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
