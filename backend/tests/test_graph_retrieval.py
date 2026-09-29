"""Graph retrieval (Phase 7): GraphRetrievalService -> GraphRepository -> (fake) Neo4j.

The graph is written by the real Phase 6 GraphBuilder from the hand-made analysis
of a small Java project described in `retrieval_helpers.py`; every expected answer
below can be read off the map at the top of that file.
"""

from typing import Any

import pytest
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from app.core.errors import (
    EntityNotFoundError,
    GraphDatabaseError,
    GraphDatabaseUnavailableError,
    InvalidGraphQueryError,
    ProjectNotFoundError,
)
from app.extraction.models import EntityType
from app.graph.builder import GraphBuilder
from app.graph.client import Neo4jClient
from app.graph.models import Direction, EntityResult, RelatedEntity
from app.graph.repository import GraphRepository
from app.services.graph_retrieval_service import GraphRetrievalService
from tests.graph_fakes import FakeNeo4j, fake_driver_factory
from tests.relationship_helpers import analyze
from tests.retrieval_helpers import (
    ADMIN,
    ADMIN_FILE,
    AUDIT,
    BAN,
    CONFIG_FILE,
    DELETE,
    ISERVICE,
    ISERVICE_FILE,
    LOG_FILE,
    LOGIN,
    SAVE,
    USER,
    USER_FILE,
    USER_SERVICE,
    USER_SERVICE_FILE,
    VALIDATE,
    WRITE,
    full,
    java_report,
    short,
)

PROJECT_A = "a" * 32
PROJECT_B = "b" * 32
PASSWORD = "secret-password"
MALICIOUS = "x'}) DETACH DELETE n //`"


def make_repository(database: FakeNeo4j) -> GraphRepository:
    client = Neo4jClient("bolt://localhost:7687", "neo4j", PASSWORD, "neo4j",
                         driver_factory=fake_driver_factory(database))  # fmt: skip
    return GraphRepository(client)


@pytest.fixture
def database() -> FakeNeo4j:
    """Projects A and B hold the same code: any leak between them would show."""
    database = FakeNeo4j()
    builder = GraphBuilder(make_repository(database))
    builder.build(java_report(PROJECT_A))
    builder.build(java_report(PROJECT_B))
    database.queries.clear()
    return database


@pytest.fixture
def service(database: FakeNeo4j) -> GraphRetrievalService:
    return GraphRetrievalService(make_repository(database))


def ids(results: list[RelatedEntity]) -> list[str]:
    return [short(result.entity.id) for result in results]


def a(short_id: str) -> str:
    return full(PROJECT_A, short_id)


# ----- Entity search -----


def test_find_entity_by_exact_id(service: GraphRetrievalService) -> None:
    [found] = service.find_entities(PROJECT_A, a(SAVE))

    assert found.id == a(SAVE)


def test_find_entity_by_qualified_name(service: GraphRetrievalService) -> None:
    [found] = service.find_entities(PROJECT_A, "User.save")

    assert found.id == a(SAVE)


def test_find_entity_by_name(service: GraphRetrievalService) -> None:
    [found] = service.find_entities(PROJECT_A, "save")

    assert found.qualified_name == "User.save"


def test_ambiguous_names_return_every_match(service: GraphRetrievalService) -> None:
    found = service.find_entities(PROJECT_A, "run")

    assert [e.qualified_name for e in found] == [
        "AuditService.run", "IService.run", "UserService.run",
    ]  # fmt: skip
    assert {e.project_id for e in found} == {PROJECT_A}


def test_exact_matches_come_before_partial_ones(service: GraphRetrievalService) -> None:
    found = service.find_entities(PROJECT_A, "User", partial=True)
    names = [e.qualified_name for e in found]

    assert names[0] == "User"  # the exact match, then the partial ones in name order
    assert names[1:] == sorted(names[1:])
    assert {"UserService.login", "src/model/User.java"} <= set(names)
    assert service.find_entities(PROJECT_A, "user") == []  # exact search is case-sensitive
    assert len(service.find_entities(PROJECT_A, "user", partial=True)) == len(found)


def test_search_can_filter_by_entity_type(service: GraphRetrievalService) -> None:
    found = service.find_entities(PROJECT_A, "run", entity_types=[EntityType.INTERFACE])
    assert found == []
    interfaces = service.find_entities(
        PROJECT_A, "Service", partial=True, entity_types=["interface"]
    )
    assert [e.id for e in interfaces] == [a(ISERVICE)]


def test_entity_metadata_is_complete(service: GraphRetrievalService) -> None:
    assert service.get_entity(PROJECT_A, a(SAVE)) == EntityResult(
        id=a(SAVE),
        entity_type="method",
        name="save",
        qualified_name="User.save",
        project_id=PROJECT_A,
        file_path=USER_FILE,
        language="java",
        start_line=15,
        start_column=5,
        end_line=18,
        end_column=6,
        parent_id=a(USER),
    )
    file = service.get_entity(PROJECT_A, a(USER_FILE))
    assert (file.entity_type, file.name, file.parent_id) == ("file", "User.java", None)


def test_entity_context_has_parent_and_neighbors(service: GraphRetrievalService) -> None:
    context = service.get_entity_context(PROJECT_A, a(SAVE))

    assert context.entity.id == a(SAVE)
    assert context.parent is not None and context.parent.id == a(USER)
    neighbors = {(n.direction, n.relationship.type, short(n.entity.id))
                 for n in context.neighbors if n.relationship}  # fmt: skip
    assert neighbors == {
        (Direction.OUTGOING, "CALLS", WRITE),
        (Direction.INCOMING, "CALLS", LOGIN),
        (Direction.INCOMING, "CALLS", DELETE),
        (Direction.INCOMING, "CONTAINS", USER),
    }
    assert [short(e.id) for e in context.entities][:2] == [SAVE, USER]
    assert len(context.relationships) == 4


# ----- Containment -----


def test_class_methods_through_contains(service: GraphRetrievalService) -> None:
    methods = service.get_contained_entities(PROJECT_A, a(USER))

    assert ids(methods) == [SAVE, DELETE, VALIDATE]  # in source order
    assert {m.relationship.type for m in methods if m.relationship} == {"CONTAINS"}


def test_nested_containment_with_depth(service: GraphRetrievalService) -> None:
    direct = service.get_contained_entities(PROJECT_A, a(USER_FILE))
    nested = service.get_contained_entities(PROJECT_A, a(USER_FILE), max_depth=2)
    methods_only = service.get_contained_entities(
        PROJECT_A, a(USER_FILE), max_depth=2, entity_types=[EntityType.METHOD]
    )

    assert ids(direct) == [USER]
    assert [(short(n.entity.id), n.depth) for n in nested] == [
        (USER, 1), (SAVE, 2), (DELETE, 2), (VALIDATE, 2),
    ]  # fmt: skip
    assert ids(methods_only) == [SAVE, DELETE, VALIDATE]


# ----- Calls -----


def test_callers(service: GraphRetrievalService) -> None:
    callers = service.get_callers(PROJECT_A, a(SAVE))

    assert ids(callers) == [DELETE, LOGIN]
    login_call = callers[1].relationship
    assert login_call is not None
    assert (login_call.type, login_call.source_id, login_call.target_id) == (
        "CALLS", a(LOGIN), a(SAVE),
    )  # fmt: skip
    assert (login_call.file_path, login_call.column) == (USER_SERVICE_FILE, 9)
    assert all(c.direction == Direction.INCOMING for c in callers)


def test_callees(service: GraphRetrievalService) -> None:
    callees = service.get_callees(PROJECT_A, a(LOGIN))

    assert ids(callees) == [SAVE, VALIDATE]
    assert all(c.direction == Direction.OUTGOING for c in callees)


# ----- Imports and dependencies -----


def test_imports(service: GraphRetrievalService) -> None:
    assert ids(service.get_imports(PROJECT_A, a(USER_SERVICE_FILE))) == [USER_FILE, ISERVICE_FILE]


def test_importers(service: GraphRetrievalService) -> None:
    assert ids(service.get_importers(PROJECT_A, a(USER_FILE))) == [ADMIN_FILE, USER_SERVICE_FILE]
    assert service.get_importers(PROJECT_A, a(CONFIG_FILE)) == []  # depended on, not imported


def test_direct_dependencies(service: GraphRetrievalService) -> None:
    dependencies = service.get_dependencies(PROJECT_A, a(USER_SERVICE_FILE))

    assert ids(dependencies) == [USER_FILE, ISERVICE_FILE]
    assert all(d.relationship and d.relationship.type == "DEPENDS_ON" for d in dependencies)


def test_transitive_dependencies(service: GraphRetrievalService) -> None:
    found = service.get_transitive_dependencies(PROJECT_A, a(USER_SERVICE_FILE))

    assert [(short(d.entity.id), d.depth) for d in found] == [
        (USER_FILE, 1), (ISERVICE_FILE, 1), (LOG_FILE, 2), (CONFIG_FILE, 3),
    ]  # fmt: skip


def test_max_depth_is_respected(service: GraphRetrievalService, database: FakeNeo4j) -> None:
    found = service.get_transitive_dependencies(PROJECT_A, a(USER_SERVICE_FILE), max_depth=2)

    assert ids(found) == [USER_FILE, ISERVICE_FILE, LOG_FILE]  # Config.java is 3 steps away
    assert "[:DEPENDS_ON*1..2]" in database.queries[-1][0]


def test_dependents(service: GraphRetrievalService) -> None:
    assert ids(service.get_dependents(PROJECT_A, a(USER_FILE))) == [ADMIN_FILE, USER_SERVICE_FILE]
    assert ids(service.get_dependents(PROJECT_A, a(CONFIG_FILE), max_depth=5)) == [
        LOG_FILE, USER_FILE, ADMIN_FILE, USER_SERVICE_FILE,
    ]  # fmt: skip


# ----- Inheritance and implementation -----


def test_inheritance_parents(service: GraphRetrievalService) -> None:
    assert ids(service.get_parents(PROJECT_A, a(ADMIN))) == [USER]
    assert service.get_parents(PROJECT_A, a(USER)) == []


def test_inheritance_children(service: GraphRetrievalService) -> None:
    assert ids(service.get_subclasses(PROJECT_A, a(USER))) == [ADMIN]


def test_implemented_interfaces(service: GraphRetrievalService) -> None:
    assert ids(service.get_implemented_interfaces(PROJECT_A, a(USER_SERVICE))) == [ISERVICE]


def test_classes_implementing_an_interface(service: GraphRetrievalService) -> None:
    implementations = service.get_implementations(PROJECT_A, a(ISERVICE))

    assert ids(implementations) == [AUDIT, USER_SERVICE]
    assert {i.relationship.type for i in implementations if i.relationship} == {"IMPLEMENTS"}


# ----- Paths -----


def test_path_between_two_entities(service: GraphRetrievalService) -> None:
    [direct] = service.find_paths(PROJECT_A, a(LOGIN), a(SAVE))
    [chain] = service.find_paths(PROJECT_A, a(BAN), a(WRITE))

    assert [short(n.id) for n in direct.nodes] == [LOGIN, SAVE]
    assert [r.type for r in direct.relationships] == ["CALLS"]
    assert [short(n.id) for n in chain.nodes] == [BAN, DELETE, SAVE, WRITE]
    assert chain.length == 3
    for index, relationship in enumerate(chain.relationships):
        assert relationship.source_id == chain.nodes[index].id
        assert relationship.target_id == chain.nodes[index + 1].id


def test_several_paths_are_ordered_and_limited(service: GraphRetrievalService) -> None:
    paths = service.find_paths(PROJECT_A, a(LOGIN), a(WRITE))
    [first] = service.find_paths(PROJECT_A, a(LOGIN), a(WRITE), limit=1)

    assert [[short(n.id) for n in p.nodes] for p in paths] == [
        [LOGIN, SAVE, WRITE], [LOGIN, VALIDATE, WRITE],
    ]  # fmt: skip
    assert first == paths[0]
    assert service.find_paths(PROJECT_A, a(LOGIN), a(WRITE)) == paths  # deterministic


def test_undirected_and_typed_paths(service: GraphRetrievalService) -> None:
    assert service.find_paths(PROJECT_A, a(SAVE), a(VALIDATE)) == []  # no directed path
    paths = service.find_paths(
        PROJECT_A, a(SAVE), a(VALIDATE), directed=False, relationship_types=["CALLS"]
    )

    # Both callers-in-common and callees-in-common connect them; sorted by node IDs.
    assert [[short(n.id) for n in p.nodes] for p in paths] == [
        [SAVE, LOGIN, VALIDATE], [SAVE, WRITE, VALIDATE],
    ]  # fmt: skip
    assert {r.type for p in paths for r in p.relationships} == {"CALLS"}
    login_path = paths[0]
    assert login_path.relationships[0].source_id == a(LOGIN)  # the real direction is kept


def test_path_depth_is_respected(service: GraphRetrievalService) -> None:
    assert service.find_paths(PROJECT_A, a(BAN), a(WRITE), max_depth=2) == []


def test_no_path_returns_an_empty_list(service: GraphRetrievalService) -> None:
    assert service.find_paths(PROJECT_A, a(WRITE), a(LOGIN)) == []  # against the arrows
    assert service.find_paths(PROJECT_A, a(CONFIG_FILE), a(AUDIT), directed=False,
                              relationship_types=["CALLS"]) == []  # fmt: skip


def test_path_from_an_entity_to_itself(service: GraphRetrievalService) -> None:
    [path] = service.find_paths(PROJECT_A, a(SAVE), a(SAVE))

    assert [n.id for n in path.nodes] == [a(SAVE)] and path.length == 0


def test_path_to_a_missing_entity_is_an_error(service: GraphRetrievalService) -> None:
    with pytest.raises(EntityNotFoundError):
        service.find_paths(PROJECT_A, a(SAVE), a("src/Nope.java:Nope"))


# ----- Project isolation -----


def test_results_never_leave_the_project(
    service: GraphRetrievalService, database: FakeNeo4j
) -> None:
    results: list[Any] = [
        service.find_entities(PROJECT_A, "User", partial=True),
        service.get_callers(PROJECT_A, a(SAVE)),
        service.get_neighbors(PROJECT_A, a(USER)),
        service.get_transitive_dependencies(PROJECT_A, a(USER_SERVICE_FILE), max_depth=5),
        service.get_implementations(PROJECT_A, a(ISERVICE)),
    ]
    paths = service.find_paths(PROJECT_A, a(LOGIN), a(WRITE), directed=False)

    entities = [e for group in results for e in group]
    entities = [e.entity if isinstance(e, RelatedEntity) else e for e in entities]
    entities += [n for path in paths for n in path.nodes]
    assert entities and {e.project_id for e in entities} == {PROJECT_A}
    assert all(p["project_id"] == PROJECT_A for _, p in database.queries)


def test_an_entity_of_another_project_is_not_found(
    service: GraphRetrievalService, database: FakeNeo4j
) -> None:
    other = full(PROJECT_B, SAVE)  # exists, but in project B

    for call in [
        lambda: service.get_entity(PROJECT_A, other),
        lambda: service.get_callers(PROJECT_A, other),
        lambda: service.find_paths(PROJECT_A, a(LOGIN), other),
        lambda: service.get_entity_context(PROJECT_A, other),
    ]:
        with pytest.raises(EntityNotFoundError):
            call()
    assert database.queries == []  # refused before reaching Neo4j


def test_a_forged_id_with_the_right_prefix_finds_nothing(
    service: GraphRetrievalService,
) -> None:
    # Starts with project A's ID but is project B's entity with a prefix glued on.
    forged = f"{PROJECT_A}:{full(PROJECT_B, SAVE)}"

    with pytest.raises(EntityNotFoundError):
        service.get_callers(PROJECT_A, forged)
    assert service.find_entities(PROJECT_A, full(PROJECT_B, SAVE)) == []


def test_invalid_project_ids_are_rejected(
    service: GraphRetrievalService, database: FakeNeo4j
) -> None:
    for project_id in ["", "../etc", "A" * 32, "a" * 31, f"{PROJECT_A}' OR 1=1"]:
        with pytest.raises(ProjectNotFoundError):
            service.find_entities(project_id, "User")
    assert database.queries == []


def test_empty_project(service: GraphRetrievalService) -> None:
    empty = "c" * 32

    assert service.find_entities(empty, "User", partial=True) == []
    with pytest.raises(EntityNotFoundError):
        service.get_entity(empty, f"{empty}:{SAVE}")


def test_empty_database() -> None:
    service = GraphRetrievalService(make_repository(FakeNeo4j()))

    assert service.find_entities(PROJECT_A, "User") == []
    with pytest.raises(EntityNotFoundError):
        service.get_callees(PROJECT_A, a(LOGIN))


# ----- Missing entities, limits and validation -----


def test_missing_entity_is_an_error_but_no_result_is_not(
    service: GraphRetrievalService,
) -> None:
    missing = a("src/Nope.java:Nope.run")
    for call in [
        service.get_entity,
        service.get_callers,
        service.get_callees,
        service.get_imports,
        service.get_contained_entities,
        service.get_transitive_dependencies,
        service.get_implementations,
        service.get_neighbors,
    ]:
        with pytest.raises(EntityNotFoundError):
            call(PROJECT_A, missing)  # type: ignore[operator]

    assert service.get_callers(PROJECT_A, a(LOGIN)) == []  # exists, nobody calls it


def test_limits_are_respected(service: GraphRetrievalService, database: FakeNeo4j) -> None:
    assert len(service.find_entities(PROJECT_A, "run", limit=2)) == 2
    assert len(service.get_contained_entities(PROJECT_A, a(USER), limit=1)) == 1
    assert len(service.get_transitive_dependencies(PROJECT_A, a(USER_SERVICE_FILE), limit=3)) == 3
    assert len(service.find_paths(PROJECT_A, a(LOGIN), a(WRITE), limit=1)) == 1

    limits = [p["limit"] for q, p in database.queries if "LIMIT $limit" in q]
    assert limits == [2, 1, 3, 1]


def test_default_limits_are_always_sent(
    service: GraphRetrievalService, database: FakeNeo4j
) -> None:
    service.get_callers(PROJECT_A, a(SAVE))
    service.find_paths(PROJECT_A, a(LOGIN), a(SAVE))

    assert [p["limit"] for _, p in database.queries] == [50, 5]


@pytest.mark.parametrize("max_depth", [0, -1, 6, 100, True, "3", 2.5, None])
def test_invalid_max_depth_is_rejected(
    service: GraphRetrievalService, database: FakeNeo4j, max_depth: Any
) -> None:
    with pytest.raises(InvalidGraphQueryError):
        service.get_transitive_dependencies(PROJECT_A, a(USER_FILE), max_depth=max_depth)
    with pytest.raises(InvalidGraphQueryError):
        service.find_paths(PROJECT_A, a(LOGIN), a(SAVE), max_depth=max_depth)
    with pytest.raises(InvalidGraphQueryError):
        service.get_contained_entities(PROJECT_A, a(USER_FILE), max_depth=max_depth)
    assert database.queries == []


@pytest.mark.parametrize("limit", [0, -5, 201, True, "10", 1.5, None])
def test_invalid_limit_is_rejected(
    service: GraphRetrievalService, database: FakeNeo4j, limit: Any
) -> None:
    with pytest.raises(InvalidGraphQueryError):
        service.find_entities(PROJECT_A, "User", limit=limit)
    with pytest.raises(InvalidGraphQueryError):
        service.get_callers(PROJECT_A, a(SAVE), limit=limit)
    assert database.queries == []


def test_path_limit_has_its_own_maximum(service: GraphRetrievalService) -> None:
    with pytest.raises(InvalidGraphQueryError):
        service.find_paths(PROJECT_A, a(LOGIN), a(SAVE), limit=21)


def test_invalid_search_and_filters_are_rejected(service: GraphRetrievalService) -> None:
    for text in ["", "   ", "x" * 501]:
        with pytest.raises(InvalidGraphQueryError):
            service.find_entities(PROJECT_A, text)
    with pytest.raises(InvalidGraphQueryError):
        service.find_entities(PROJECT_A, "User", entity_types=["module"])
    with pytest.raises(InvalidGraphQueryError):
        service.find_entities(PROJECT_A, "User", entity_types=[])
    for types in [["CALLS]->() DETACH DELETE n //"], ["calls"], []]:
        with pytest.raises(InvalidGraphQueryError):
            service.find_paths(PROJECT_A, a(LOGIN), a(SAVE), relationship_types=types)


def test_a_single_entity_type_string_is_accepted(service: GraphRetrievalService) -> None:
    found = service.find_entities(PROJECT_A, "User", partial=True, entity_types="class")

    assert [e.qualified_name for e in found] == ["User", "UserService"]


# ----- Security: values stay parameters -----


def test_malicious_values_remain_query_parameters(
    service: GraphRetrievalService, database: FakeNeo4j
) -> None:
    malicious_id = a(MALICIOUS)
    node_count = len(database.nodes)

    assert service.find_entities(PROJECT_A, MALICIOUS, partial=True) == []
    with pytest.raises(EntityNotFoundError):
        service.get_callers(PROJECT_A, malicious_id)
    with pytest.raises(EntityNotFoundError):
        service.find_paths(PROJECT_A, malicious_id, a(SAVE))
    with pytest.raises(EntityNotFoundError):
        service.get_transitive_dependencies(PROJECT_A, malicious_id)

    assert database.queries
    for query, parameters in database.queries:
        assert MALICIOUS not in query
        assert PROJECT_A not in query
        assert parameters["project_id"] == PROJECT_A
    assert database.queries[0][1]["text"] == MALICIOUS
    assert len(database.nodes) == node_count  # nothing was deleted


# ----- Neo4j errors -----


def test_neo4j_down_is_translated(database: FakeNeo4j) -> None:
    def unavailable(_query: str, _parameters: dict[str, Any]) -> None:
        raise ServiceUnavailable("connection refused")

    database.before_query = unavailable
    service = GraphRetrievalService(make_repository(database))

    with pytest.raises(GraphDatabaseUnavailableError) as error:
        service.get_callers(PROJECT_A, a(SAVE))
    assert PASSWORD not in str(error.value)


def test_failed_query_is_translated(database: FakeNeo4j) -> None:
    def broken(_query: str, _parameters: dict[str, Any]) -> None:
        raise Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Statement.SyntaxError", message="Invalid input"
        )

    database.before_query = broken
    service = GraphRetrievalService(make_repository(database))

    with pytest.raises(GraphDatabaseError) as error:
        service.find_entities(PROJECT_A, "User")
    assert error.value.message == "A Neo4j transaction failed."
    assert PASSWORD not in error.value.message


# ----- End to end: real source code, analyzed by Phases 3-5 -----


def test_retrieval_on_an_analyzed_python_project() -> None:
    database = FakeNeo4j()
    repository = make_repository(database)
    report = analyze(
        {
            "src/models/user.py": "class User:\n    def save(self):\n        pass\n",
            "src/models/admin.py": (
                "from models.user import User\n\nclass Admin(User):\n    pass\n"
            ),
            "src/services/auth.py": (
                "from models.user import User\n\n"
                "def login():\n    user = User()\n    user.save()\n"
            ),
        },
        PROJECT_A,
    )
    GraphBuilder(repository).build(report)
    service = GraphRetrievalService(repository)

    [save] = service.find_entities(PROJECT_A, "User.save")
    [user] = service.find_entities(PROJECT_A, "User", entity_types=[EntityType.CLASS])
    [auth] = service.find_entities(PROJECT_A, "auth.py")

    assert ids(service.get_callers(PROJECT_A, save.id)) == ["src/services/auth.py:login"]
    assert ids(service.get_contained_entities(PROJECT_A, user.id)) == [
        "src/models/user.py:User.save"
    ]
    assert ids(service.get_subclasses(PROJECT_A, user.id)) == ["src/models/admin.py:Admin"]
    assert ids(service.get_imports(PROJECT_A, auth.id)) == ["src/models/user.py"]
    [path] = service.find_paths(PROJECT_A, auth.id, save.id)
    assert [n.name for n in path.nodes] == ["auth.py", "login", "save"]
    assert [r.type for r in path.relationships] == ["CONTAINS", "CALLS"]
