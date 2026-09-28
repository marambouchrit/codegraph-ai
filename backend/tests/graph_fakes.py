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
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.graph import repository as queries

Row = dict[str, Any]


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
