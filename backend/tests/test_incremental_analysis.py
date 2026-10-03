"""Phase 14: incremental analysis. Only what changed is parsed, embedded and written.

Real services on fake databases (see tests/analysis_helpers.py). Each test analyzes
the project once, changes its source files in the workspace, analyzes again, and
checks both the report and what the databases really hold.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from neo4j.exceptions import ServiceUnavailable

from app.analysis.index import AnalysisIndexStore
from app.api.dependencies import get_analysis_service, get_chat_service, get_project_service
from app.core.config import Settings
from app.main import app
from tests.analysis_helpers import AnalysisWorld
from tests.vector_helpers import FailingEmbeddings, HashingEmbeddings
from tests.conftest import MakeZip
from tests.test_graphrag import FILES, QUESTION

SERVICE = "auth/service.py"
REPOSITORY = "repository/user.py"
DATABASE = "db/database.py"
CHARTS = "reports/charts.py"
LOGIN = f"{SERVICE}:AuthService.login"
FIND_USER = f"{REPOSITORY}:UserRepository.find_user"


@pytest.fixture
def world(settings: Settings, make_zip: MakeZip) -> Iterator[AnalysisWorld]:
    world = AnalysisWorld(settings, make_zip)
    app.dependency_overrides[get_project_service] = lambda: world.projects
    app.dependency_overrides[get_analysis_service] = world.service
    app.dependency_overrides[get_chat_service] = world.chat_service
    yield world
    app.dependency_overrides.clear()


def snapshot(world: AnalysisWorld) -> tuple[Any, ...]:
    """Everything the databases hold for the project, to compare two analyses."""
    nodes = {i: dict(n.properties, build_id=None) for i, n in world.database.nodes.items()}
    edges = {k: dict(r.properties, build_id=None) for k, r in world.database.relationships.items()}
    points, _ = world.store.client.scroll(world.store.collection, limit=10_000,
                                          with_payload=True, with_vectors=True)  # fmt: skip
    # Rounded: Qdrant stores float32, the last digits vary from one write to the next.
    vectors = {
        p.payload["chunk_id"]: (dict(p.payload, index_id=None), [round(v, 5) for v in p.vector])
        for p in points
    }
    return nodes, edges, vectors


# ----- Initial analysis -----


def test_the_first_analysis_is_full(world: AnalysisWorld) -> None:
    report = world.ready()

    assert report["mode"] == "full"
    changes = report["changes"]
    assert (changes["files_added"], changes["files_parsed"]) == (len(FILES), len(FILES))
    assert changes["chunks_embedded"] == report["vectors"]["chunks"] == world.points() > 0
    assert world.embeddings.documents_embedded == report["vectors"]["chunks"]
    assert changes["chunks_reused"] == 0
    assert report["graph"]["entities"] == len(world.database.nodes)
    assert report["graph"]["relationships"] == len(world.database.relationships)
    assert sorted(world.parsed) == sorted(path.removeprefix("app/") for path in FILES)


# ----- Nothing changed -----


def test_an_unchanged_project_is_not_parsed_embedded_or_written_again(world: AnalysisWorld) -> None:
    first = world.ready()
    before = snapshot(world)
    world.parsed.clear()
    embedded = world.embeddings.documents_embedded
    world.database.queries.clear()

    second = world.ready()

    assert second["mode"] == "incremental"
    changes = second["changes"]
    assert (changes["files_unchanged"], changes["files_parsed"]) == (len(FILES), 0)
    assert (changes["files_added"], changes["files_modified"], changes["files_deleted"]) == (0, 0, 0)
    assert world.parsed == []
    assert world.embeddings.documents_embedded == embedded  # no embedding at all
    assert (changes["chunks_embedded"], changes["chunks_reused"]) == (0, first["vectors"]["chunks"])
    assert (changes["nodes_written"], changes["relationships_written"]) == (0, 0)
    assert not any("MERGE" in query or "DELETE" in query for query, _ in world.database.queries)
    assert snapshot(world) == before
    for key in ("graph", "vectors"):
        assert second[key] == first[key]  # same totals


# ----- A modified file -----


def test_only_the_modified_file_is_parsed_and_only_its_changed_chunks_embedded(
    world: AnalysisWorld,
) -> None:
    first = world.ready()
    world.parsed.clear()
    embedded = world.embeddings.documents_embedded
    # Change the body of one function at the END of the file: nothing else moves.
    world.edit(SERVICE, lambda text: text.replace(
        "return hashlib.sha256(password.encode()).hexdigest() == password_hash",
        "digest = hashlib.sha256(password.encode()).hexdigest()\n    return digest == password_hash",
    ))  # fmt: skip

    second = world.ready()

    changes = second["changes"]
    assert second["mode"] == "incremental"
    assert (changes["files_modified"], changes["files_unchanged"]) == (1, len(FILES) - 1)
    assert world.parsed == [SERVICE]  # the three other files were not parsed
    # One chunk changed (verify_password): one embedding, every other vector reused.
    assert changes["chunks_embedded"] == 1
    assert world.embeddings.documents_embedded == embedded + 1
    assert changes["chunks_reused"] == first["vectors"]["chunks"] - 1
    assert second["vectors"]["chunks"] == first["vectors"]["chunks"] == world.points()
    assert changes["chunks_deleted"] == 0


def test_the_modified_chunk_is_replaced_not_duplicated(world: AnalysisWorld) -> None:
    world.ready()
    world.edit(SERVICE, lambda text: text.replace('"invalid credentials"', '"wrong password"'))

    world.ready()

    points, _ = world.store.client.scroll(world.store.collection, limit=1000, with_payload=True)
    texts = [p.payload["text"] for p in points if p.payload["qualified_name"] == "AuthService.login"]
    assert len(texts) == 1 and "wrong password" in texts[0]
    assert not any("invalid credentials" in p.payload["text"] for p in points)


def test_moved_code_keeps_its_vectors_and_gets_new_line_numbers(world: AnalysisWorld) -> None:
    """Blank lines added at the top of a file shift every line: metadata changes, text does not."""
    first = world.ready()
    embedded = world.embeddings.documents_embedded
    world.edit(DATABASE, lambda text: "\n\n" + text)

    second = world.ready()

    changes = second["changes"]
    assert changes["chunks_embedded"] == 0 and world.embeddings.documents_embedded == embedded
    assert changes["chunks_updated"] > 0
    assert changes["chunks_reused"] == first["vectors"]["chunks"]
    points, _ = world.store.client.scroll(world.store.collection, limit=1000, with_payload=True)
    query = next(p for p in points if p.payload["qualified_name"] == "Database.query")
    assert query.payload["start_line"] == 6  # was 4
    # The graph nodes moved too.
    node = world.database.nodes[f"{world.project_id}:{DATABASE}:Database.query"]
    assert node.properties["start_line"] == 6


# ----- An added file -----


def test_an_added_file_is_parsed_and_linked_to_existing_code(world: AnalysisWorld) -> None:
    first = world.ready()
    world.parsed.clear()
    world.write("auth/admin.py", (
        "from auth.service import AuthService\n\n\n"
        "def admin_login(username, password):\n"
        "    service = AuthService()\n"
        "    return service.login(username, password)\n"
    ))  # fmt: skip

    second = world.ready()

    changes = second["changes"]
    assert (changes["files_added"], changes["files_parsed"]) == (1, 1)
    assert world.parsed == ["auth/admin.py"]
    assert second["graph"]["entities"] == first["graph"]["entities"] + 2  # the file, the function
    assert "auth/admin.py:admin_login" in world.node_names()
    # A relationship from the new file to an entity of an UNCHANGED file.
    assert ("auth/admin.py:admin_login", "CALLS", LOGIN) in world.edge_names()
    assert changes["chunks_embedded"] == second["vectors"]["chunks"] - first["vectors"]["chunks"] > 0
    assert world.points() == second["vectors"]["chunks"]


# ----- A deleted file -----


def test_a_deleted_file_is_removed_from_the_graph_and_the_vectors(world: AnalysisWorld) -> None:
    first = world.ready()
    world.parsed.clear()
    embedded = world.embeddings.documents_embedded
    world.delete(CHARTS)

    second = world.ready()

    changes = second["changes"]
    assert (changes["files_deleted"], changes["files_parsed"]) == (1, 0)
    assert world.parsed == [] and world.embeddings.documents_embedded == embedded
    assert not any(name.startswith(CHARTS) for name in world.node_names())
    assert changes["nodes_deleted"] == 2 and second["graph"]["stale_entities_removed"] == 2
    assert changes["chunks_deleted"] == second["vectors"]["stale_chunks_removed"] == 1
    assert world.points() == second["vectors"]["chunks"] == first["vectors"]["chunks"] - 1
    points, _ = world.store.client.scroll(world.store.collection, limit=1000, with_payload=True)
    assert not any(p.payload["file_path"] == CHARTS for p in points)


def test_deleting_a_file_removes_the_relationships_that_pointed_to_it(world: AnalysisWorld) -> None:
    """service.py is unchanged, but its call to the deleted repository must not remain."""
    world.ready()
    assert (LOGIN, "CALLS", FIND_USER) in world.edge_names()
    world.parsed.clear()
    world.delete(REPOSITORY)

    report = world.ready()

    assert world.parsed == []  # service.py is not parsed again: its cached references are resolved
    assert not any(REPOSITORY in source or REPOSITORY in target for source, _, target in world.edge_names())
    assert not any(name.startswith(REPOSITORY) for name in world.node_names())
    assert report["changes"]["relationships_deleted"] > 0
    # Every relationship left has both ends in the graph.
    names = world.node_names()
    assert all(source in names and target in names for source, _, target in world.edge_names())
    # What cannot be resolved any more is counted, not stored.
    assert report["graph"]["unresolved_references"] > 0


def test_a_restored_file_brings_its_relationships_back(world: AnalysisWorld) -> None:
    first = world.ready()
    before = snapshot(world)
    content = (world.source() / REPOSITORY).read_text(encoding="utf-8")
    world.delete(REPOSITORY)
    world.ready()

    world.write(REPOSITORY, content)
    restored = world.ready()

    assert (LOGIN, "CALLS", FIND_USER) in world.edge_names()
    assert restored["graph"] == {**first["graph"], "stale_entities_removed": 0}
    assert snapshot(world) == before  # exactly the graph and vectors of the first analysis


# ----- Cross-file relationships -----


def test_a_renamed_function_updates_callers_in_unchanged_files(world: AnalysisWorld) -> None:
    world.ready()
    world.parsed.clear()
    world.edit(REPOSITORY, lambda text: text.replace("def find_user(", "def load_user("))

    world.ready()

    assert world.parsed == [REPOSITORY]
    names = world.node_names()
    assert f"{REPOSITORY}:UserRepository.load_user" in names and FIND_USER not in names
    # login still calls find_user in its (unchanged) source: that call is no longer resolved.
    assert not any(target == FIND_USER for _, _, target in world.edge_names())
    assert all(source in names and target in names for source, _, target in world.edge_names())


def test_a_new_definition_resolves_a_call_of_an_unchanged_file(world: AnalysisWorld) -> None:
    """charts.py is added later; main.py already called it and is not parsed again."""
    world.write("main.py", "from reports.extra import total\n\n\ndef run():\n    return total([1])\n")
    world.ready()
    assert not any(target.endswith(":total") for _, _, target in world.edge_names())
    world.parsed.clear()
    world.write("reports/extra.py", "def total(values):\n    return sum(values)\n")

    world.ready()

    assert world.parsed == ["reports/extra.py"]
    assert ("main.py:run", "CALLS", "reports/extra.py:total") in world.edge_names()


# ----- Idempotency, isolation -----


def test_incremental_and_full_analyses_give_the_same_databases(world: AnalysisWorld) -> None:
    world.ready()
    world.edit(SERVICE, lambda text: text.replace('"invalid credentials"', '"wrong password"'))
    world.delete(CHARTS)
    world.write("auth/admin.py", "def admin():\n    return 1\n")
    incremental = world.ready()
    after_incremental = snapshot(world)

    full = world.ready(full=True)

    assert (incremental["mode"], full["mode"]) == ("incremental", "full")
    assert snapshot(world) == after_incremental
    for key in ("graph", "vectors"):
        assert full[key] == {**incremental[key], f"stale_{'entities' if key == 'graph' else 'chunks'}_removed": 0}


def test_repeated_incremental_analyses_are_idempotent(world: AnalysisWorld) -> None:
    world.ready()
    world.edit(SERVICE, lambda text: text + "\n\ndef logout():\n    return None\n")
    world.ready()
    before = snapshot(world)

    again = world.ready()

    assert again["changes"]["files_parsed"] == 0 and again["changes"]["chunks_embedded"] == 0
    assert snapshot(world) == before


def test_projects_are_analyzed_separately(world: AnalysisWorld) -> None:
    other = world.import_project(FILES)
    world.ready()
    world.ready(other)
    other_nodes = world.node_names(other)
    other_points = world.points(other)

    world.delete(CHARTS)
    world.edit(SERVICE, lambda text: text.replace('"invalid credentials"', '"wrong password"'))
    report = world.ready()

    assert report["changes"]["files_deleted"] == 1
    assert world.node_names(other) == other_nodes  # the other project is untouched
    assert world.points(other) == other_points
    assert any(name.startswith(CHARTS) for name in world.node_names(other))
    # And it sees no change of its own.
    assert world.ready(other)["changes"]["files_unchanged"] == len(FILES)


# ----- Change detection uses content, not dates -----


def test_a_file_rewritten_with_the_same_content_is_unchanged(world: AnalysisWorld) -> None:
    world.ready()
    world.parsed.clear()
    path = world.source() / SERVICE
    path.write_bytes(path.read_bytes())  # new modification time, same bytes

    report = world.ready()

    assert report["changes"]["files_unchanged"] == len(FILES) and world.parsed == []


def test_a_file_that_cannot_be_parsed_is_remembered_not_retried(world: AnalysisWorld) -> None:
    world.write("broken.py", "def broken():\n    return 1\n")
    world.unparsable.add("broken.py")
    first = world.ready()
    assert first["failed_files"] == 1 and first["warnings"]
    world.parsed.clear()

    second = world.ready()

    assert second["failed_files"] == 1
    assert world.parsed == []  # unchanged: not parsed again
    # Once fixed, it is analyzed.
    world.unparsable.clear()
    world.write("broken.py", "def fixed():\n    return 1\n")
    third = world.ready()
    assert third["failed_files"] == 0 and "broken.py:fixed" in world.node_names()


# ----- When the saved index cannot be trusted: a full analysis -----


def test_without_an_index_the_analysis_is_full(world: AnalysisWorld) -> None:
    world.ready()
    AnalysisIndexStore(world.projects.workspace).clear(world.project_id)

    assert world.ready()["mode"] == "full"


def test_an_unreadable_index_means_a_full_analysis(world: AnalysisWorld) -> None:
    world.ready()
    world.projects.workspace.analysis_index_file(world.project_id).write_text("{broken")

    assert world.ready()["mode"] == "full"


def test_an_emptied_vector_store_is_rebuilt(world: AnalysisWorld) -> None:
    """The index says the vectors exist, Qdrant says otherwise: everything is embedded again."""
    first = world.ready()
    world.store.delete_project(world.project_id)

    second = world.ready()

    assert second["mode"] == "full"
    assert second["changes"]["chunks_embedded"] == first["vectors"]["chunks"] == world.points()


def test_an_emptied_graph_is_rebuilt(world: AnalysisWorld) -> None:
    first = world.ready()
    world.database.nodes.clear()
    world.database.relationships.clear()

    second = world.ready()

    assert second["mode"] == "full"
    assert len(world.database.nodes) == first["graph"]["entities"]


def test_another_embedding_model_means_a_full_analysis(world: AnalysisWorld) -> None:
    world.ready()
    world.embeddings = HashingEmbeddings(model_name="test/other-model")

    report = world.ready()

    assert report["mode"] == "full"
    assert report["changes"]["chunks_embedded"] == report["vectors"]["chunks"]
    assert report["vectors"]["embedding_model"] == "test/other-model"


def test_a_full_analysis_can_be_forced(world: AnalysisWorld) -> None:
    first = world.ready()
    world.parsed.clear()

    forced = world.ready(full=True)

    assert forced["mode"] == "full" and len(world.parsed) == len(FILES)
    assert forced["changes"]["chunks_embedded"] == first["vectors"]["chunks"]
    assert world.points() == first["vectors"]["chunks"]  # overwritten, not duplicated


# ----- Failures -----


def test_a_failure_before_writing_keeps_the_previous_analysis_valid(world: AnalysisWorld) -> None:
    """Embedding fails: nothing was written yet, the previous graph and vectors are intact."""
    first = world.ready()
    before = snapshot(world)
    world.edit(SERVICE, lambda text: text.replace('"invalid credentials"', '"wrong password"'))
    world.embeddings = FailingEmbeddings(fail_after=0)

    state = world.analyze()

    assert state["status"] == "failed"
    assert state["job"]["error"] == "The embedding model failed to embed the text."
    assert state["job"]["phase"] == "embedding"
    assert state["analysis"] == first  # still the valid report of the previous analysis
    assert snapshot(world) == before  # nothing was written
    # The next analysis is still incremental, and completes.
    world.embeddings = HashingEmbeddings()
    world.parsed.clear()
    retry = world.ready()
    assert retry["mode"] == "incremental" and world.parsed == [SERVICE]


def test_a_failure_while_writing_is_failed_with_no_result_then_repaired(world: AnalysisWorld) -> None:
    world.ready()
    world.edit(SERVICE, lambda text: text + "\n\ndef logout():\n    return None\n")

    def fail_on_write(query: str, _parameters: dict[str, Any]) -> None:
        if "MERGE" in query:
            raise ServiceUnavailable("connection lost")

    world.database.before_query = fail_on_write
    state = world.analyze()

    assert state["status"] == "failed" and state["analysis"] is None
    assert state["job"]["phase"] == "graph"
    assert "Neo4j is not reachable" in state["job"]["error"]
    # The databases may be half updated: the next analysis rebuilds everything.
    world.database.before_query = None
    repaired = world.ready()
    assert repaired["mode"] == "full"
    assert f"{SERVICE}:logout" in world.node_names()
    assert world.points() == repaired["vectors"]["chunks"]


def test_neo4j_down_fails_before_any_work(world: AnalysisWorld) -> None:
    first = world.ready()
    world.parsed.clear()

    def unavailable(_query: str, _parameters: dict[str, Any]) -> None:
        raise ServiceUnavailable("connection refused")

    world.database.before_query = unavailable
    state = world.analyze()

    assert state["status"] == "failed" and state["job"]["phase"] == "preparing"
    assert state["analysis"] == first and world.parsed == []


# ----- Chat sees the updated code -----


def test_chat_retrieves_the_updated_code(world: AnalysisWorld) -> None:
    world.ready()
    world.edit(SERVICE, lambda text: text.replace(
        '"""Authentication entry point: check the user\'s password, then create a token."""',
        '"""Authentication entry point: check the password with two-factor codes."""',
    ))  # fmt: skip
    world.ready()

    response = world.client.post(f"/projects/{world.project_id}/chat", json={"question": QUESTION})

    assert response.status_code == 200
    login = next(s for s in response.json()["sources"] if s["entity"] == "AuthService.login")
    assert login["found_by"] == "semantic_search"
    points, _ = world.store.client.scroll(world.store.collection, limit=1000, with_payload=True)
    assert any("two-factor codes" in p.payload["text"] for p in points)
