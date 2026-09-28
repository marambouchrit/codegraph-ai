"""Phase 5 report -> Neo4j graph: mapping, idempotency, project isolation, stale data."""

import pytest
from neo4j.exceptions import ClientError

from app.core.errors import GraphDatabaseError
from app.extraction.models import Entity, EntityType
from app.graph.builder import (
    GraphBuilder,
    contains_edge,
    entity_to_node,
    graph_edges,
    graph_nodes,
    relationship_to_edge,
)
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.ingestion.languages import Language
from app.relationships.models import Relationship, RelationshipType
from tests.graph_fakes import FakeNeo4j, fake_driver_factory
from tests.relationship_helpers import analyze

PROJECT_A = "a" * 32
PROJECT_B = "b" * 32

USER_PY = "class User:\n    def save(self):\n        pass\n"
AUTH_PY = "from models.user import User\n\ndef login():\n    user = User()\n    user.save()\n"
PROJECT_FILES = {"src/models/user.py": USER_PY, "src/services/auth.py": AUTH_PY}


def make_builder(database: FakeNeo4j, batch_size: int = 1000) -> GraphBuilder:
    client = Neo4jClient("bolt://localhost:7687", "neo4j", "secret", "neo4j",
                         driver_factory=fake_driver_factory(database))  # fmt: skip
    return GraphBuilder(GraphRepository(client, batch_size))


def node_id(project_id: str, suffix: str) -> str:
    return f"{project_id}:{suffix}"


# ----- Mapping -----


def test_entity_becomes_a_node_with_its_id_and_properties() -> None:
    entity = Entity(
        id=f"{PROJECT_A}:src/models/user.py:User.save",
        type=EntityType.METHOD,
        name="save",
        qualified_name="User.save",
        file_path="src/models/user.py",
        language=Language.PYTHON,
        start_line=2,
        start_column=5,
        end_line=3,
        end_column=13,
        parent_id=f"{PROJECT_A}:src/models/user.py:User",
    )

    node = entity_to_node(entity, PROJECT_A)

    assert node.id == entity.id  # the entity ID is the node identity: no new ID
    assert node.label == "Method"
    assert node.properties == {
        "id": entity.id,
        "project_id": PROJECT_A,
        "entity_type": "method",
        "name": "save",
        "qualified_name": "User.save",
        "file_path": "src/models/user.py",
        "language": "python",
        "start_line": 2,
        "start_column": 5,
        "end_line": 3,
        "end_column": 13,
        "parent_id": f"{PROJECT_A}:src/models/user.py:User",
    }


def test_every_entity_type_gets_its_label() -> None:
    report = analyze(
        {"a.ts": "interface I {}\nclass C { m(): void {} }\nfunction f() {}\n"}, PROJECT_A
    )

    labels = {node.properties["qualified_name"]: node.label for node in graph_nodes(report)}

    assert labels == {"a.ts": "File", "I": "Interface", "C": "Class", "C.m": "Method", "f": "Function"}


def test_entity_from_another_project_is_refused() -> None:
    report = analyze({"a.py": "x = 1\n"}, PROJECT_B)

    with pytest.raises(ValueError, match="does not belong"):
        entity_to_node(report.entities[0], PROJECT_A)


def test_relationship_becomes_an_edge_with_the_phase_5_id() -> None:
    relationship = Relationship(
        id="CALLS:a->b", type=RelationshipType.CALLS, source_id="a", target_id="b",
        file_path="auth.py", line=5, column=5,
    )  # fmt: skip

    edge = relationship_to_edge(relationship)

    assert (edge.id, edge.type, edge.source_id, edge.target_id) == ("CALLS:a->b", "CALLS", "a", "b")
    assert edge.properties == {"id": "CALLS:a->b", "file_path": "auth.py", "line": 5, "column": 5}


def test_parent_links_become_contains_edges() -> None:
    report = analyze({"user.py": USER_PY}, PROJECT_A)
    file_id, user_id = node_id(PROJECT_A, "user.py"), node_id(PROJECT_A, "user.py:User")

    edges = {(e.source_id, e.type, e.target_id): e for e in graph_edges(report)}

    assert set(edges) == {
        (file_id, "CONTAINS", user_id),
        (user_id, "CONTAINS", node_id(PROJECT_A, "user.py:User.save")),
    }
    assert edges[(file_id, "CONTAINS", user_id)].id == f"CONTAINS:{file_id}->{user_id}"
    assert contains_edge(report.entities[0]) is None  # a file has no parent


def test_unresolved_references_never_become_nodes_or_edges() -> None:
    report = analyze({"main.py": "import requests\n\ndef run():\n    requests.get()\n    print()\n"}, PROJECT_A)

    assert len(report.unresolved) == 3
    assert {node.properties["name"] for node in graph_nodes(report)} == {"main.py", "run"}
    assert {edge.type for edge in graph_edges(report)} == {"CONTAINS"}


# ----- Building -----


def test_build_writes_nodes_and_edges() -> None:
    database = FakeNeo4j()
    report = analyze(PROJECT_FILES, PROJECT_A)

    result = make_builder(database).build(report, build_id="build-1")

    auth, user = "src/services/auth.py", "src/models/user.py"
    assert database.edges() >= {
        (node_id(PROJECT_A, auth), "IMPORTS", node_id(PROJECT_A, user)),
        (node_id(PROJECT_A, f"{auth}:login"), "CALLS", node_id(PROJECT_A, f"{user}:User.save")),
        (node_id(PROJECT_A, auth), "DEPENDS_ON", node_id(PROJECT_A, user)),
    }
    assert database.labels_of(node_id(PROJECT_A, f"{auth}:login")) == {"Entity", "Function"}
    assert result.nodes_written == 5
    assert result.nodes_by_label == {"Class": 1, "File": 2, "Function": 1, "Method": 1}
    assert result.relationships_by_type == {"CALLS": 2, "CONTAINS": 3, "DEPENDS_ON": 1, "IMPORTS": 1}
    assert result.relationships_written == 7
    assert (result.files, result.unresolved_references, result.failed_files) == (2, 0, 0)
    assert result.summary == "Graph built successfully: 2 files, 5 entities, 7 relationships"
    assert len(database.schema) == 2  # the constraint and the index


def test_building_twice_gives_the_same_graph() -> None:
    database = FakeNeo4j()
    builder = make_builder(database)
    report = analyze(PROJECT_FILES, PROJECT_A)

    builder.build(report, build_id="build-1")
    nodes = {i: dict(n.properties, build_id=None) for i, n in database.nodes.items()}
    edges = set(database.relationships)
    second = builder.build(report, build_id="build-2")

    assert {i: dict(n.properties, build_id=None) for i, n in database.nodes.items()} == nodes
    assert set(database.relationships) == edges
    assert (second.stale_nodes_deleted, second.stale_relationships_deleted) == (0, 0)


def test_rebuild_after_code_changes_removes_stale_nodes_and_edges() -> None:
    database = FakeNeo4j()
    builder = make_builder(database)
    builder.build(analyze(PROJECT_FILES, PROJECT_A), build_id="build-1")

    # login no longer calls save, and User.save is deleted.
    changed = {
        "src/models/user.py": "class User:\n    pass\n",
        "src/services/auth.py": "from models.user import User\n\ndef login():\n    User()\n",
    }
    result = builder.build(analyze(changed, PROJECT_A), build_id="build-2")

    assert node_id(PROJECT_A, "src/models/user.py:User.save") not in database.nodes
    assert not any(r.type == "CALLS" and r.target_id.endswith("User.save")
                   for r in database.relationships.values())  # fmt: skip
    assert result.stale_nodes_deleted == 1
    assert result.stale_relationships_deleted == 2  # login CALLS and User CONTAINS User.save
    assert all(n.properties["build_id"] == "build-2" for n in database.nodes.values())


def test_entity_changing_type_keeps_one_type_label() -> None:
    database = FakeNeo4j()
    builder = make_builder(database)
    builder.build(analyze({"a.js": "function User() {}\n"}, PROJECT_A), build_id="1")

    builder.build(analyze({"a.js": "class User {}\n"}, PROJECT_A), build_id="2")

    assert database.labels_of(node_id(PROJECT_A, "a.js:User")) == {"Entity", "Class"}


def test_projects_are_isolated() -> None:
    database = FakeNeo4j()
    builder = make_builder(database)
    builder.build(analyze(PROJECT_FILES, PROJECT_A), build_id="a-1")
    builder.build(analyze(PROJECT_FILES, PROJECT_B), build_id="b-1")
    project_b = {i: dict(n.properties) for i, n in database.nodes.items() if i.startswith(PROJECT_B)}
    b_edges = {k for k in database.relationships if k[0].startswith(PROJECT_B)}

    # Rebuilding A with less code deletes stale data of A only.
    builder.build(analyze({"src/models/user.py": USER_PY}, PROJECT_A), build_id="a-2")
    builder.repository.delete_project(PROJECT_A)

    assert database.project_node_ids(PROJECT_A) == set()
    assert {i: dict(n.properties) for i, n in database.nodes.items()} == project_b
    assert set(database.relationships) == b_edges
    # No relationship ever links the two projects.
    assert all(k[0][:32] == k[3][:32] for k in database.relationships)


def test_empty_project_writes_nothing_and_clears_an_old_graph() -> None:
    database = FakeNeo4j()
    builder = make_builder(database)
    builder.build(analyze(PROJECT_FILES, PROJECT_A), build_id="1")

    result = builder.build(analyze({}, PROJECT_A), build_id="2")

    assert (result.nodes_written, result.relationships_written) == (0, 0)
    assert result.stale_nodes_deleted == 5
    assert database.nodes == {} and database.relationships == {}


def test_nodes_and_edges_are_sent_in_batches() -> None:
    database = FakeNeo4j()
    files = {f"m{i}.py": "def f(): pass\n" for i in range(5)}  # 10 nodes, 5 CONTAINS

    result = make_builder(database, batch_size=4).build(analyze(files, PROJECT_A), build_id="1")

    node_batches = [len(p["rows"]) for q, p in database.queries if "MERGE (n:Entity" in q]
    edge_batches = [len(p["rows"]) for q, p in database.queries if "MERGE (source)" in q]
    assert node_batches == [4, 1, 4, 1]  # 5 File nodes (4 + 1), then 5 Function nodes
    assert edge_batches == [4, 1]
    assert result.nodes_written == 10


def test_failed_transaction_raises_and_keeps_the_previous_graph() -> None:
    database = FakeNeo4j()
    builder = make_builder(database)
    builder.build(analyze(PROJECT_FILES, PROJECT_A), build_id="1")
    before = set(database.nodes)

    def fail_on_relationships(query: str, _parameters: dict) -> None:
        if "MERGE (source)" in query:
            raise ClientError("write failed")

    database.before_query = fail_on_relationships

    with pytest.raises(GraphDatabaseError, match="transaction failed"):
        builder.build(analyze({"src/models/user.py": USER_PY}, PROJECT_A), build_id="2")

    assert set(database.nodes) == before  # no stale cleanup after a failed build
