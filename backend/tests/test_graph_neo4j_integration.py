"""Optional tests against a real Neo4j server. Skipped by a normal `pytest` run.

Run them with Neo4j started (`docker compose up -d neo4j`) and the connection set in
backend/.env or in the environment:

    pytest -m neo4j

They only create and delete projects with random IDs, so they never touch the
graph of a real project stored in the same database.
"""

import uuid
from collections.abc import Iterator

import pytest

from app.core.config import Settings
from app.core.errors import EntityNotFoundError
from app.graph.builder import (
    GraphBuilder,
    diff_graph,
    edge_fingerprint,
    graph_edges,
    graph_nodes,
    node_fingerprint,
)
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.services.graph_retrieval_service import GraphRetrievalService
from tests.graph_fakes import FakeNeo4j, fake_driver_factory
from tests.relationship_helpers import analyze
from tests.retrieval_helpers import QUESTIONS, SAVE, full, java_report

pytestmark = pytest.mark.neo4j

FILES = {
    "src/models/user.py": "class User:\n    def save(self):\n        pass\n",
    "src/services/auth.py": (
        "from models.user import User\n\ndef login():\n    user = User()\n    user.save()\n"
    ),
}


@pytest.fixture
def client() -> Iterator[Neo4jClient]:
    with Neo4jClient.from_settings(Settings()) as client:
        client.verify_connectivity()
        yield client


@pytest.fixture
def repository(client: Neo4jClient) -> GraphRepository:
    return GraphRepository(client, batch_size=2)  # small batches: several per build


@pytest.fixture
def project_ids(repository: GraphRepository) -> Iterator[list[str]]:
    ids = [uuid.uuid4().hex, uuid.uuid4().hex]
    yield ids
    for project_id in ids:
        repository.delete_project(project_id)


def count(client: Neo4jClient, query: str, project_id: str) -> int:
    return client.read(lambda tx: tx.run(query, {"project_id": project_id}).data()[0]["count"])


NODES = "MATCH (n:Entity {project_id: $project_id}) RETURN count(n) AS count"
RELATIONSHIPS = "MATCH (:Entity {project_id: $project_id})-[r]->() RETURN count(r) AS count"


def test_build_is_idempotent_and_queryable(
    client: Neo4jClient, repository: GraphRepository, project_ids: list[str]
) -> None:
    project_id = project_ids[0]
    builder = GraphBuilder(repository)
    report = analyze(FILES, project_id)

    first = builder.build(report)
    nodes, relationships = count(client, NODES, project_id), count(client, RELATIONSHIPS, project_id)
    builder.build(report)
    builder.build(report)

    assert (nodes, relationships) == (first.nodes_written, first.relationships_written)
    assert count(client, NODES, project_id) == nodes
    assert count(client, RELATIONSHIPS, project_id) == relationships

    calls = client.read(
        lambda tx: tx.run(
            "MATCH (f:Function {project_id: $project_id})-[:CALLS]->(m) "
            "RETURN f.qualified_name AS caller, m.qualified_name AS callee ORDER BY callee",
            {"project_id": project_id},
        ).data()
    )
    assert calls == [
        {"caller": "login", "callee": "User"},
        {"caller": "login", "callee": "User.save"},
    ]


def test_projects_are_isolated(
    client: Neo4jClient, repository: GraphRepository, project_ids: list[str]
) -> None:
    project_a, project_b = project_ids
    builder = GraphBuilder(repository)
    builder.build(analyze(FILES, project_a))
    builder.build(analyze(FILES, project_b))
    nodes_b = count(client, NODES, project_b)

    repository.delete_project(project_a)

    assert count(client, NODES, project_a) == 0
    assert count(client, NODES, project_b) == nodes_b
    assert repository.statistics(project_b).node_count == nodes_b


# ----- Graph retrieval (Phase 7) -----


def test_retrieval_gives_the_same_answers_as_the_fake(
    repository: GraphRepository, project_ids: list[str]
) -> None:
    """Every retrieval question, asked to a real Neo4j and to the test fake.

    The fake runs the unit tests; this proves its answers (content, order, limits)
    are the ones the real Cypher gives.
    """
    project_a, project_b = project_ids
    fake_client = Neo4jClient("bolt://localhost:7687", "neo4j", "", "neo4j",
                              driver_factory=fake_driver_factory(FakeNeo4j()))  # fmt: skip
    fake_repository = GraphRepository(fake_client)
    for target in (repository, fake_repository):
        GraphBuilder(target).build(java_report(project_a))
    GraphBuilder(repository).build(java_report(project_b))  # a neighbour that must not leak

    real, fake = GraphRetrievalService(repository), GraphRetrievalService(fake_repository)
    for name, question in QUESTIONS.items():
        assert question(real, project_a) == question(fake, project_a), name


def test_retrieval_is_isolated_on_a_real_server(
    repository: GraphRepository, project_ids: list[str]
) -> None:
    project_a, project_b = project_ids
    GraphBuilder(repository).build(java_report(project_a))
    GraphBuilder(repository).build(java_report(project_b))
    service = GraphRetrievalService(repository)

    found = service.find_entities(project_a, "User", partial=True, limit=200)
    callers = service.get_callers(project_a, full(project_a, SAVE))

    assert found and {e.project_id for e in found} == {project_a}
    assert callers and {c.entity.project_id for c in callers} == {project_a}
    with pytest.raises(EntityNotFoundError):
        service.get_callers(project_a, full(project_b, SAVE))


def test_an_incremental_update_gives_the_same_graph_as_the_fake(
    repository: GraphRepository, project_ids: list[str]
) -> None:
    """Phase 14: write a difference (changed nodes, removed nodes and relationships) to a
    real Neo4j and to the fake; both end with the same graph, and the neighbour project
    is untouched."""
    project_a, project_b = project_ids
    database = FakeNeo4j()
    fake_repository = GraphRepository(
        Neo4jClient("bolt://localhost:7687", "neo4j", "", "neo4j",
                    driver_factory=fake_driver_factory(database))
    )  # fmt: skip
    before = analyze(FILES, project_a)
    for target in (repository, fake_repository):
        GraphBuilder(target).build(before)
    GraphBuilder(repository).build(analyze(FILES, project_b))
    neighbour = repository.statistics(project_b)

    # auth.py no longer calls User.save, and gains a function; user.py loses `save`.
    after = analyze({
        "src/models/user.py": "class User:\n    pass\n",
        "src/services/auth.py": (
            "from models.user import User\n\ndef login():\n    return User()\n\n"
            "def logout():\n    return None\n"
        ),
    }, project_a)  # fmt: skip
    old_nodes, old_edges = graph_nodes(before), graph_edges(before)
    diff = diff_graph(
        graph_nodes(after), graph_edges(after),
        {node.id: node_fingerprint(node) for node in old_nodes},
        {edge.id: (edge_fingerprint(edge), edge.source_id) for edge in old_edges},
    )  # fmt: skip
    assert diff.nodes_to_delete and diff.edges_to_delete and diff.nodes_to_write

    real = GraphBuilder(repository).apply(project_a, diff)
    fake = GraphBuilder(fake_repository).apply(project_a, diff)

    assert real == fake
    assert real.nodes_deleted == len(diff.nodes_to_delete)
    assert repository.statistics(project_a) == fake_repository.statistics(project_a)
    retrieval, fake_retrieval = GraphRetrievalService(repository), GraphRetrievalService(fake_repository)
    assert retrieval.get_project_graph(project_a) == fake_retrieval.get_project_graph(project_a)
    # Exactly the graph a full build of the new code gives.
    assert sum(repository.statistics(project_a).nodes_by_label.values()) == len(graph_nodes(after))
    assert repository.statistics(project_b) == neighbour  # the other project is untouched
