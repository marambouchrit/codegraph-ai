"""GraphRepository: the Cypher sent to Neo4j (parameters, labels, types, project scope)."""

import pytest

from app.extraction.models import EntityType
from app.graph import schema
from app.graph.client import Neo4jClient
from app.graph.models import GraphEdge, GraphNode
from app.graph.repository import (
    GraphRepository,
    upsert_nodes_query,
    upsert_relationships_query,
)
from app.relationships.models import RelationshipType
from tests.graph_fakes import FakeNeo4j, fake_driver_factory

PROJECT_A = "a" * 32
PROJECT_B = "b" * 32

# A name taken from analyzed code: it must travel as data, never as Cypher.
MALICIOUS = "x'}) DETACH DELETE n //`"


def make_repository(database: FakeNeo4j, batch_size: int = 1000) -> GraphRepository:
    client = Neo4jClient("bolt://localhost:7687", "neo4j", "secret", "neo4j",
                         driver_factory=fake_driver_factory(database))  # fmt: skip
    return GraphRepository(client, batch_size)


def node(project_id: str, name: str, label: str = "Function") -> GraphNode:
    node_id = f"{project_id}:a.py:{name}"
    return GraphNode(
        node_id,
        label,
        {"id": node_id, "project_id": project_id, "name": name, "entity_type": label.lower()},
    )


def edge(source: GraphNode, target: GraphNode, type_: str = "CALLS") -> GraphEdge:
    edge_id = f"{type_}:{source.id}->{target.id}"
    return GraphEdge(edge_id, type_, source.id, target.id, {"id": edge_id, "line": 1})


# ----- Schema and query building -----


def test_schema_has_one_constraint_and_one_index() -> None:
    database = FakeNeo4j()
    make_repository(database).ensure_schema()

    assert database.schema == list(schema.SCHEMA_STATEMENTS)
    assert "REQUIRE n.id IS UNIQUE" in database.schema[0]
    assert "IF NOT EXISTS" in database.schema[0] and "IF NOT EXISTS" in database.schema[1]
    assert database.transactions == 2  # schema changes run in their own transactions


def test_labels_and_types_come_from_fixed_lists() -> None:
    assert schema.node_label(EntityType.CLASS) == "Class"
    assert {schema.node_label(t) for t in EntityType} == {
        "File", "Class", "Interface", "Function", "Method",
    }  # fmt: skip
    assert schema.RELATIONSHIP_TYPES == {t.value for t in RelationshipType} | {"CONTAINS"}
    for bad in ["Hacker", "CALLS]->() DETACH DELETE n //", "calls", ""]:
        with pytest.raises(ValueError):
            schema.relationship_type(bad)
        with pytest.raises(ValueError):
            upsert_nodes_query(bad)
        with pytest.raises(ValueError):
            upsert_relationships_query(bad)
    with pytest.raises(ValueError):
        schema.node_label("module")  # type: ignore[arg-type]


def test_upsert_queries_merge_on_the_id() -> None:
    assert "MERGE (n:Entity {id: row.id})" in upsert_nodes_query("Class")
    assert "SET n:Class" in upsert_nodes_query("Class")
    query = upsert_relationships_query("CALLS")
    assert "MERGE (source)-[r:CALLS {id: row.id}]->(target)" in query
    assert "project_id: $project_id" in query


# ----- Writing -----


def test_values_are_sent_as_parameters_not_in_the_query_text() -> None:
    database = FakeNeo4j()
    repository = make_repository(database)
    a, b = node(PROJECT_A, MALICIOUS), node(PROJECT_A, "b")

    repository.upsert_nodes([a, b], build_id="build-1")
    repository.upsert_relationships(PROJECT_A, [edge(a, b)], build_id="build-1")
    repository.delete_project(PROJECT_A)

    for query, parameters in database.queries:
        assert MALICIOUS not in query
        assert PROJECT_A not in query
        assert "build-1" not in query
    node_rows = database.queries[0][1]["rows"]
    assert node_rows[0]["properties"]["name"] == MALICIOUS
    assert database.queries[1][1]["project_id"] == PROJECT_A


def test_nodes_are_grouped_by_label_and_batched() -> None:
    database = FakeNeo4j()
    nodes = [node(PROJECT_A, f"f{i}") for i in range(5)] + [node(PROJECT_A, "C", "Class")]

    written = make_repository(database, batch_size=2).upsert_nodes(nodes, build_id="1")

    sizes = [(q.split("SET n:")[1].split()[0], len(p["rows"])) for q, p in database.queries]
    assert sizes == [("Function", 2), ("Function", 2), ("Function", 1), ("Class", 1)]
    assert written == 6
    assert database.transactions == 4  # one transaction per batch


def test_writing_the_same_data_twice_keeps_one_copy() -> None:
    database = FakeNeo4j()
    repository = make_repository(database)
    a, b = node(PROJECT_A, "a"), node(PROJECT_A, "b")

    for _ in range(3):
        repository.upsert_nodes([a, b], build_id="1")
        repository.upsert_relationships(PROJECT_A, [edge(a, b), edge(a, b, "USES")], "1")

    assert len(database.nodes) == 2
    assert database.edges() == {(a.id, "CALLS", b.id), (a.id, "USES", b.id)}


def test_relationships_never_link_two_projects() -> None:
    database = FakeNeo4j()
    repository = make_repository(database)
    a, b = node(PROJECT_A, "a"), node(PROJECT_B, "b")
    repository.upsert_nodes([a, b], build_id="1")

    written = repository.upsert_relationships(PROJECT_A, [edge(a, b)], build_id="1")

    assert written == 0
    assert database.relationships == {}


# ----- Deleting and reading -----


def test_delete_project_only_touches_that_project() -> None:
    database = FakeNeo4j()
    repository = make_repository(database, batch_size=2)
    a_nodes = [node(PROJECT_A, f"a{i}") for i in range(5)]
    b_nodes = [node(PROJECT_B, f"b{i}") for i in range(3)]
    repository.upsert_nodes(a_nodes + b_nodes, build_id="1")
    repository.upsert_relationships(PROJECT_B, [edge(b_nodes[0], b_nodes[1])], build_id="1")

    deleted = repository.delete_project(PROJECT_A)

    assert deleted == 5
    assert set(database.nodes) == {n.id for n in b_nodes}
    assert len(database.relationships) == 1
    assert not repository.project_exists(PROJECT_A)
    assert repository.project_exists(PROJECT_B)
    deletes = [p for q, p in database.queries if "DETACH DELETE" in q]
    assert [p["limit"] for p in deletes] == [2, 2, 2]  # 2 + 2 + 1: deleted in batches


def test_delete_stale_keeps_the_current_build() -> None:
    database = FakeNeo4j()
    repository = make_repository(database)
    old, kept = node(PROJECT_A, "old"), node(PROJECT_A, "kept")
    other_project = node(PROJECT_B, "other")
    repository.upsert_nodes([old, kept, other_project], build_id="1")
    repository.upsert_relationships(PROJECT_A, [edge(kept, old)], build_id="1")
    repository.upsert_nodes([kept], build_id="2")

    nodes, relationships = repository.delete_stale(PROJECT_A, build_id="2")

    assert (nodes, relationships) == (1, 1)
    assert set(database.nodes) == {kept.id, other_project.id}  # project B is untouched


def test_statistics_count_nodes_and_relationships_of_one_project() -> None:
    database = FakeNeo4j()
    repository = make_repository(database)
    a, b, c = node(PROJECT_A, "a"), node(PROJECT_A, "b"), node(PROJECT_A, "C", "Class")
    repository.upsert_nodes([a, b, c, node(PROJECT_B, "x")], build_id="1")
    repository.upsert_relationships(PROJECT_A, [edge(a, b), edge(a, c, "USES")], build_id="1")

    statistics = repository.statistics(PROJECT_A)

    assert statistics.nodes_by_label == {"Class": 1, "Function": 2}
    assert statistics.relationships_by_type == {"CALLS": 1, "USES": 1}
    assert (statistics.node_count, statistics.relationship_count) == (3, 2)


def test_batch_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        make_repository(FakeNeo4j(), batch_size=0)
