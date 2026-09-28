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
from app.graph.builder import GraphBuilder
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from tests.relationship_helpers import analyze

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
