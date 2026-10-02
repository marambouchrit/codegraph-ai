"""A fake Neo4j driver for tests, so that `pytest` never needs a real Neo4j server.

`FakeNeo4j` stores nodes and relationships in dictionaries and understands the few
Cypher queries of `app/graph/repository.py` (and nothing else). The real
Neo4jClient, GraphRepository and GraphBuilder run on top of it unchanged, so the
tests check what the code really sends: queries, parameters, batches.

It reproduces the meaning of the queries that matters for the tests:
- MERGE by `id`: writing a node or relationship twice keeps one;
- relationships only link nodes of the given project (MATCH ... {project_id});
- deletions are limited to one project and to `$limit` items per query.
Real MERGE semantics are checked against a real server by the optional
`test_graph_neo4j_integration.py` tests.

It also answers the read-only retrieval queries (Phase 7): entity lookup and
search, one-hop neighbors, bounded multi-hop reachability and shortest paths,
with the same filters (project, entity types), order and limits as the Cypher.
A generated query is only accepted if it is exactly the text the repository's
query builder produces for the types, direction and depth read back from it.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.graph import repository as queries
from app.graph.models import Direction

Row = dict[str, Any]

_RELATED = re.compile(r"\(start:Entity \{[^}]*\}\)(<-|-)\[r:([A-Z_|]+)\](->|-)\(other")
_REACHABLE = re.compile(
    r"MATCH path = \(start:Entity \{[^}]*\}\)(<-|-)\[:([A-Z_|]+)\*1\.\.(\d+)\](->|-)\(other"
)
_PATHS = re.compile(
    r"allShortestPaths\(\(source\)-\[(?::([A-Z_|]+))?\*1\.\.(\d+)\](->|-)\(target\)\)"
)


@dataclass
class StoredNode:
    labels: set[str]
    properties: dict[str, Any]


@dataclass
class StoredRelationship:
    type: str
    source_id: str
    target_id: str
    properties: dict[str, Any]


@dataclass
class FakeNeo4j:
    nodes: dict[str, StoredNode] = field(default_factory=dict)
    # Key: (source_id, type, id, target_id), the pattern MERGE matches on.
    relationships: dict[tuple[str, str, str, str], StoredRelationship] = field(
        default_factory=dict
    )
    schema: list[str] = field(default_factory=list)
    queries: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # every query run
    transactions: int = 0
    # Called before each query; raise an exception from it to simulate a failure.
    before_query: Callable[[str, dict[str, Any]], None] | None = None

    def run(self, query: str, parameters: dict[str, Any] | None) -> list[Row]:
        parameters = parameters or {}
        self.queries.append((query, parameters))
        if self.before_query is not None:
            self.before_query(query, parameters)
        if query.startswith(("CREATE CONSTRAINT", "CREATE INDEX")):
            self.schema.append(query)
            return []
        if query == "RETURN 1 AS ok":
            return [{"ok": 1}]
        if "MERGE (n:Entity" in query:
            return self._merge_nodes(query, parameters)
        if "MERGE (source)-[r:" in query:
            return self._merge_relationships(query, parameters)
        if query == queries.DELETE_STALE_RELATIONSHIPS:
            return self._delete_relationships(parameters)
        if query in (queries.DELETE_STALE_NODES, queries.DELETE_PROJECT_NODES):
            return self._delete_nodes(query, parameters)
        if query == queries.PROJECT_EXISTS:
            ids = [node_id for node_id in self.nodes if self._in_project(node_id, parameters)]
            return [{"id": ids[0]}] if ids else []
        if query == queries.COUNT_NODES_BY_TYPE:
            return self._count_nodes(parameters)
        if query == queries.COUNT_RELATIONSHIPS_BY_TYPE:
            return self._count_relationships(parameters)
        if query == queries.GET_ENTITY:
            node = self._project_node(parameters["entity_id"], parameters)
            return [{"entity": dict(node.properties)}] if node is not None else []
        if query == queries.FIND_ENTITIES:
            return self._find_entities(parameters)
        if query == queries.PROJECT_GRAPH_NODES:
            return self._project_graph_nodes(parameters)
        if query == queries.PROJECT_GRAPH_EDGES:
            return self._project_graph_edges(parameters)
        if match := _REACHABLE.search(query):
            direction = _direction(match.group(1), match.group(4))
            types, depth = match.group(2).split("|"), int(match.group(3))
            assert query == queries.reachable_query(types, direction, depth)
            return self._reachable(types, direction, depth, parameters)
        if match := _RELATED.search(query):
            direction, types = _direction(match.group(1), match.group(3)), match.group(2).split("|")
            assert query == queries.related_query(types, direction)
            return self._related(types, direction, parameters)
        if match := _PATHS.search(query):
            path_types = match.group(1).split("|") if match.group(1) else None
            depth, directed = int(match.group(2)), match.group(3) == "->"
            assert query == queries.paths_query(path_types, depth, directed)
            return self._paths(path_types, depth, directed, parameters)
        raise AssertionError(f"FakeNeo4j does not understand this query:\n{query}")

    # ----- Helpers for assertions -----

    def labels_of(self, node_id: str) -> set[str]:
        return self.nodes[node_id].labels

    def project_node_ids(self, project_id: str) -> set[str]:
        return {i for i, n in self.nodes.items() if n.properties.get("project_id") == project_id}

    def edges(self) -> set[tuple[str, str, str]]:
        return {(r.source_id, r.type, r.target_id) for r in self.relationships.values()}

    # ----- Query implementations -----

    def _merge_nodes(self, query: str, parameters: dict[str, Any]) -> list[Row]:
        label = re.search(r"SET n:(\w+)\s+RETURN", query)
        assert label is not None
        for row in parameters["rows"]:
            node = self.nodes.setdefault(row["id"], StoredNode(set(), {"id": row["id"]}))
            _set_properties(node.properties, row["properties"])
            node.properties["build_id"] = parameters["build_id"]
            node.labels = {"Entity", label.group(1)}  # REMOVE all type labels, SET the new one
        return [{"count": len(parameters["rows"])}]

    def _merge_relationships(self, query: str, parameters: dict[str, Any]) -> list[Row]:
        type_ = re.search(r"-\[r:(\w+) ", query)
        assert type_ is not None
        count = 0
        for row in parameters["rows"]:
            ends = (row["source_id"], row["target_id"])
            if not all(self._in_project(node_id, parameters) for node_id in ends):
                continue  # MATCH found nothing: no relationship, not counted
            key = (row["source_id"], type_.group(1), row["id"], row["target_id"])
            relationship = self.relationships.setdefault(
                key, StoredRelationship(type_.group(1), *ends, {})
            )
            _set_properties(relationship.properties, row["properties"])
            relationship.properties["build_id"] = parameters["build_id"]
            count += 1
        return [{"count": count}]

    def _delete_relationships(self, parameters: dict[str, Any]) -> list[Row]:
        stale = [
            key
            for key, r in self.relationships.items()
            if self._in_project(r.source_id, parameters)
            and r.properties.get("build_id") != parameters["build_id"]
        ][: parameters["limit"]]
        for key in stale:
            del self.relationships[key]
        return [{"count": len(stale)}]

    def _delete_nodes(self, query: str, parameters: dict[str, Any]) -> list[Row]:
        only_stale = query == queries.DELETE_STALE_NODES
        doomed = [
            node_id
            for node_id, node in self.nodes.items()
            if self._in_project(node_id, parameters)
            and (not only_stale or node.properties.get("build_id") != parameters["build_id"])
        ][: parameters["limit"]]
        for node_id in doomed:  # DETACH DELETE: the node and all its relationships
            del self.nodes[node_id]
            for key in [k for k in self.relationships if node_id in (k[0], k[3])]:
                del self.relationships[key]
        return [{"count": len(doomed)}]

    def _count_nodes(self, parameters: dict[str, Any]) -> list[Row]:
        counts: dict[str, int] = {}
        for node_id, node in self.nodes.items():
            if self._in_project(node_id, parameters):
                entity_type = node.properties["entity_type"]
                counts[entity_type] = counts.get(entity_type, 0) + 1
        return [{"entity_type": t, "count": c} for t, c in counts.items()]

    def _count_relationships(self, parameters: dict[str, Any]) -> list[Row]:
        counts: dict[str, int] = {}
        for relationship in self.relationships.values():
            if self._in_project(relationship.source_id, parameters):
                counts[relationship.type] = counts.get(relationship.type, 0) + 1
        return [{"type": t, "count": c} for t, c in counts.items()]

    def _in_project(self, node_id: str, parameters: dict[str, Any]) -> bool:
        node = self.nodes.get(node_id)
        return node is not None and node.properties.get("project_id") == parameters["project_id"]

    # ----- Retrieval query implementations -----

    def _project_node(self, node_id: str, parameters: dict[str, Any]) -> StoredNode | None:
        return self.nodes[node_id] if self._in_project(node_id, parameters) else None

    def _type_allowed(self, node_id: str, parameters: dict[str, Any]) -> bool:
        allowed = parameters["entity_types"]
        return allowed is None or self.nodes[node_id].properties["entity_type"] in allowed

    def _project_graph_nodes(self, parameters: dict[str, Any]) -> list[Row]:
        rank = {"file": 0, "class": 1, "interface": 1, "function": 2}

        def order(node: StoredNode) -> tuple[Any, ...]:
            p = node.properties
            return rank.get(p["entity_type"], 3), p["file_path"], p["start_line"], p["id"]

        nodes = [n for i, n in self.nodes.items() if self._in_project(i, parameters)]
        return [{"entity": dict(n.properties)} for n in sorted(nodes, key=order)][: parameters["limit"]]

    def _project_graph_edges(self, parameters: dict[str, Any]) -> list[Row]:
        ids, types = set(parameters["ids"]), set(parameters["types"])
        edges = [
            r for r in self.relationships.values()
            if self._in_project(r.source_id, parameters) and self._in_project(r.target_id, parameters)
            and r.source_id in ids and r.target_id in ids and r.type in types
        ]  # fmt: skip
        edges.sort(key=lambda r: (r.type, r.source_id, r.target_id, r.properties["id"]))
        return [
            {"type": r.type, "relationship": dict(r.properties),
             "source_id": r.source_id, "target_id": r.target_id}
            for r in edges[: parameters["limit"]]
        ]  # fmt: skip

    def _find_entities(self, parameters: dict[str, Any]) -> list[Row]:
        text = parameters["text"]

        def rank(properties: dict[str, Any]) -> int | None:
            if properties["id"] == text:
                return 0
            if properties["qualified_name"] == text:
                return 1
            if properties["name"] == text:
                return 2
            if parameters["partial"] and text.lower() in properties["qualified_name"].lower():
                return 3
            return None

        found = []
        for node_id, node in self.nodes.items():
            if not self._in_project(node_id, parameters):
                continue
            node_rank = rank(node.properties)
            if node_rank is not None and self._type_allowed(node_id, parameters):
                p = node.properties
                found.append(((node_rank, p["qualified_name"], p["file_path"], p["id"]), p))
        found.sort(key=lambda item: item[0])
        return [{"entity": dict(p)} for _, p in found[: parameters["limit"]]]

    def _steps(
        self, node_id: str, types: list[str] | None, directions: tuple[Direction, ...]
    ) -> list[tuple[StoredRelationship, str]]:
        """(relationship, node at its other end) for each relationship followed from node_id."""
        steps = []
        for relationship in self.relationships.values():
            if types is not None and relationship.type not in types:
                continue
            if Direction.OUTGOING in directions and relationship.source_id == node_id:
                steps.append((relationship, relationship.target_id))
            elif Direction.INCOMING in directions and relationship.target_id == node_id:
                steps.append((relationship, relationship.source_id))
        return steps

    def _related(
        self, types: list[str], direction: Direction, parameters: dict[str, Any]
    ) -> list[Row]:
        start = parameters["entity_id"]
        if self._project_node(start, parameters) is None:
            return []
        found = []
        for relationship, other_id in self._steps(start, types, (direction,)):
            if not self._in_project(other_id, parameters):
                continue
            if not self._type_allowed(other_id, parameters):
                continue
            other = self.nodes[other_id].properties
            key = (relationship.type, other["file_path"], other["start_line"], other_id)
            row = {
                "entity": dict(other),
                "type": relationship.type,
                "relationship": dict(relationship.properties),
                "source_id": relationship.source_id,
                "target_id": relationship.target_id,
            }
            found.append((key, row))
        found.sort(key=lambda item: item[0])
        return [row for _, row in found[: parameters["limit"]]]

    def _reachable(
        self, types: list[str], direction: Direction, max_depth: int, parameters: dict[str, Any]
    ) -> list[Row]:
        """Breadth-first search: the first time a node is reached is its shortest depth."""
        start = parameters["entity_id"]
        if self._project_node(start, parameters) is None:
            return []
        depths: dict[str, int] = {start: 0}
        frontier = [start]
        for depth in range(1, max_depth + 1):
            next_frontier = []
            for node_id in frontier:
                for _, other_id in self._steps(node_id, types, (direction,)):
                    if other_id not in depths and self._in_project(other_id, parameters):
                        depths[other_id] = depth
                        next_frontier.append(other_id)
            frontier = next_frontier
        found = []
        for node_id, depth in depths.items():
            if node_id == start or not self._type_allowed(node_id, parameters):
                continue
            p = self.nodes[node_id].properties
            found.append(((depth, p["file_path"], p["start_line"], node_id), p, depth))
        found.sort(key=lambda item: item[0])
        return [{"entity": dict(p), "depth": d} for _, p, d in found[: parameters["limit"]]]

    def _paths(
        self, types: list[str] | None, max_depth: int, directed: bool, parameters: dict[str, Any]
    ) -> list[Row]:
        """All the shortest paths (allShortestPaths) from source to target."""
        source, target = parameters["source_id"], parameters["target_id"]
        if self._project_node(source, parameters) is None:
            return []
        if self._project_node(target, parameters) is None:
            return []
        directions = (
            (Direction.OUTGOING,) if directed else (Direction.OUTGOING, Direction.INCOMING)
        )
        paths: list[tuple[list[str], list[StoredRelationship]]] = []
        layer: list[tuple[list[str], list[StoredRelationship]]] = [([source], [])]
        for _ in range(max_depth):
            next_layer = []
            for node_ids, edges in layer:
                for relationship, other_id in self._steps(node_ids[-1], types, directions):
                    if other_id in node_ids or not self._in_project(other_id, parameters):
                        continue
                    next_layer.append((node_ids + [other_id], edges + [relationship]))
            paths = [path for path in next_layer if path[0][-1] == target]
            if paths:
                break  # shortest length found: longer paths are not shortest paths
            layer = next_layer
        paths.sort(key=lambda path: (path[0], [edge.properties["id"] for edge in path[1]]))
        return [
            {
                "nodes": [dict(self.nodes[node_id].properties) for node_id in node_ids],
                "relationships": [
                    {
                        "type": edge.type,
                        "properties": dict(edge.properties),
                        "source_id": edge.source_id,
                        "target_id": edge.target_id,
                    }
                    for edge in edges
                ],
            }
            for node_ids, edges in paths[: parameters["limit"]]
        ]


def _direction(left: str, right: str) -> Direction:
    if (left, right) == ("-", "->"):
        return Direction.OUTGOING
    assert (left, right) == ("<-", "-"), (left, right)
    return Direction.INCOMING


def _set_properties(properties: dict[str, Any], new: dict[str, Any]) -> None:
    """Cypher `SET x += map`: a null value removes the property."""
    for key, value in new.items():
        if value is None:
            properties.pop(key, None)
        else:
            properties[key] = value


# ----- The driver objects the client uses -----


class FakeResult:
    def __init__(self, records: list[Row]) -> None:
        self._records = records

    def data(self) -> list[Row]:
        return self._records

    def consume(self) -> None:
        return None


class FakeTransaction:
    def __init__(self, database: FakeNeo4j) -> None:
        self._database = database

    def run(self, query: str, parameters: dict[str, Any] | None = None) -> FakeResult:
        return FakeResult(self._database.run(query, parameters))


class FakeSession:
    def __init__(self, database: FakeNeo4j) -> None:
        self._database = database

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def execute_write(self, work: Callable[[FakeTransaction], Any]) -> Any:
        self._database.transactions += 1
        return work(FakeTransaction(self._database))

    execute_read = execute_write


class FakeDriver:
    def __init__(self, database: FakeNeo4j) -> None:
        self.database = database
        self.sessions: list[str | None] = []  # the database name of every session opened
        self.closed = False

    def verify_connectivity(self) -> None:
        self.database.run("RETURN 1 AS ok", None)

    def session(self, database: str | None = None) -> FakeSession:
        self.sessions.append(database)
        return FakeSession(self.database)

    def close(self) -> None:
        self.closed = True


def fake_driver_factory(database: FakeNeo4j) -> Callable[..., FakeDriver]:
    """A replacement for neo4j.GraphDatabase.driver that returns a FakeDriver."""

    def create(uri: str, auth: tuple[str, str]) -> FakeDriver:
        return FakeDriver(database)

    return create
