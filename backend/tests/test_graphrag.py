"""GraphRAGService: vector hits (in-memory Qdrant) expanded in the graph (fake Neo4j).

The project below is analyzed by the real Phases 3-5 and written by the real Phase 6
builder; its chunks come from the real Phase 8 chunker. Relationships (resolved by
Phase 5):

    AuthService.login        CALLS  UserRepository, UserRepository.find_user,
                                    AuthService.create_token, verify_password
    UserRepository.find_user CALLS  Database, Database.query
    UserRepository.save_user CALLS  Database, Database.insert
    CachedUserRepository     INHERITS UserRepository
    auth/service.py -> repository/user.py -> db/database.py   (IMPORTS, DEPENDS_ON)
"""

import io
import sys
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest
from neo4j.exceptions import ServiceUnavailable
from qdrant_client import QdrantClient

from app.core.config import Settings
from app.core.errors import (
    GraphDatabaseUnavailableError,
    InvalidVectorQueryError,
    ProjectNotFoundError,
    VectorStoreUnavailableError,
)
from app.graph.builder import GraphBuilder
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.graphrag.expansion import Expansion
from app.graphrag.models import EntityRole, GraphStatus, SourceReason
from app.rag.chunker import CodeChunker
from app.rag.models import ChunkSearchResult, CodeChunk
from app.rag.vector_store import QdrantVectorStore
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graph_service import GraphService
from app.services.graphrag_service import GraphRAGService
from app.services.project_service import ProjectService
from app.services.vector_index_service import VectorIndexService
from app.services.vector_retrieval_service import VectorRetrievalService
from tests.conftest import MakeZip
from tests.graph_fakes import FakeNeo4j, fake_driver_factory
from tests.relationship_helpers import analyze, extraction_service, parser_service
from tests.vector_helpers import HashingEmbeddings, memory_store

PROJECT_A = "a" * 32
PROJECT_B = "b" * 32
QUESTION = "How is authentication implemented?"

FILES = {
    "app/auth/service.py": '''"""Authentication of users: password check and access tokens."""
import hashlib

import jwt

from repository.user import UserRepository

SECRET_KEY = "change-me"


class AuthService:
    """Authentication service: logs users in and creates their access tokens."""

    def login(self, username, password):
        """Authentication entry point: check the user's password, then create a token."""
        repository = UserRepository()
        user = repository.find_user(username)
        if user is None or not verify_password(password, user.password_hash):
            raise PermissionError("invalid credentials")
        return self.create_token(user)

    def create_token(self, user):
        """Create a signed JWT access token for an authenticated user."""
        return jwt.encode({"sub": user.id}, SECRET_KEY, algorithm="HS256")


def verify_password(password, password_hash):
    """Compare a password with its stored hash (authentication helper)."""
    return hashlib.sha256(password.encode()).hexdigest() == password_hash
''',
    "app/repository/user.py": '''from db.database import Database


class UserRepository:
    """Loads and stores users."""

    def find_user(self, username):
        database = Database()
        return database.query("users", username)

    def save_user(self, user):
        database = Database()
        database.insert("users", user)


class CachedUserRepository(UserRepository):
    """Keeps recently loaded users in memory."""

    def clear(self):
        self.cache = {}
''',
    "app/db/database.py": '''class Database:
    """A tiny in-memory database."""

    def query(self, table, key):
        return None

    def insert(self, table, row):
        pass
''',
    "app/reports/charts.py": '''def render_bar_chart(values, width=40):
    """Draw a horizontal bar chart of monthly sales figures as text."""
    peak = max(values) or 1
    return "\\n".join("#" * int(width * value / peak) for value in values)
''',
}

AUTH = "app/auth/service.py"
REPO = "app/repository/user.py"
DB = "app/db/database.py"
LOGIN = f"{AUTH}:AuthService.login"
CREATE_TOKEN = f"{AUTH}:AuthService.create_token"
VERIFY = f"{AUTH}:verify_password"
AUTH_SERVICE = f"{AUTH}:AuthService"
USER_REPO = f"{REPO}:UserRepository"
FIND_USER = f"{REPO}:UserRepository.find_user"
SAVE_USER = f"{REPO}:UserRepository.save_user"
CACHED_REPO = f"{REPO}:CachedUserRepository"
DATABASE = f"{DB}:Database"
QUERY = f"{DB}:Database.query"


def full(project_id: str, short_id: str) -> str:
    return f"{project_id}:{short_id}"


def short(entity_id: str) -> str:
    return entity_id.split(":", 1)[1]


# ----- Building the two stores -----


def project_chunks(project_id: str, chunker: CodeChunker | None = None) -> list[CodeChunk]:
    chunker = chunker or CodeChunker()
    chunks: list[CodeChunk] = []
    for path, source in FILES.items():
        parse_result = parser_service.parse_source(source.encode(), path)
        file_entities = extraction_service.extract(parse_result, project_id)
        chunks += chunker.chunk_file(project_id, parse_result.source, file_entities)
    return chunks


class World:
    """Projects A and B, with the same code, in a fake Neo4j and an in-memory Qdrant."""

    def __init__(self, project_ids: Sequence[str] = (PROJECT_A, PROJECT_B)) -> None:
        self.database = FakeNeo4j()
        client = Neo4jClient("bolt://localhost:7687", "neo4j", "secret", "neo4j",
                             driver_factory=fake_driver_factory(self.database))  # fmt: skip
        self.graph = GraphRetrievalService(GraphRepository(client))
        self.store = memory_store()
        self.embeddings = HashingEmbeddings()
        self.store.ensure_collection(self.embeddings.dimension)
        for project_id in project_ids:
            GraphBuilder(GraphRepository(client)).build(analyze(FILES, project_id))
            chunks = project_chunks(project_id)
            vectors = self.embeddings.embed_documents([c.embedding_text for c in chunks])
            self.store.upsert(chunks, vectors, index_id="1", embedding_model="test/hashing")
        self.database.queries.clear()
        self.vector = VectorRetrievalService(self.store, self.embeddings)

    @classmethod
    def for_project(cls, project_id: str) -> "World":
        return cls([project_id])

    def service(self, vector: Any = None, **settings: Any) -> GraphRAGService:
        return GraphRAGService(vector or self.vector, self.graph, Settings(**settings))

    def graph_parameters(self) -> list[dict[str, Any]]:
        return [parameters for _, parameters in self.database.queries]


@pytest.fixture
def world() -> World:
    return World()


class FixedHits:
    """A vector retrieval returning chosen hits: precise seeds for expansion tests."""

    def __init__(self, hits: Sequence[ChunkSearchResult]) -> None:
        self.hits = list(hits)
        self.calls: list[tuple[str, str]] = []

    def retrieve(self, project_id: str, query: str, **_options: Any) -> list[ChunkSearchResult]:
        self.calls.append((project_id, query))
        return self.hits


def hit(project_id: str, qualified_name: str, score: float, part: int = 1) -> ChunkSearchResult:
    [chunk] = [c for c in project_chunks(project_id) if c.qualified_name == qualified_name]
    if part != 1:  # a second part of the same entity (as if it had been split)
        chunk = replace(chunk, id=f"{chunk.entity_id}|{part}", part=part, part_count=part,
                        start_line=chunk.start_line + 3)  # later lines of the same entity
    return ChunkSearchResult(chunk, score)


def with_seeds(world: World, *names: str, **settings: Any) -> Any:
    hits = [hit(PROJECT_A, name, round(0.9 - i * 0.1, 6)) for i, name in enumerate(names)]
    return world.service(FixedHits(hits), **settings).build_context(PROJECT_A, QUESTION)


def neighbor_ids(context: Any) -> list[str]:
    return [short(e.entity.id) for e in context.entities if e.role == EntityRole.NEIGHBOR]


def edges(context: Any) -> set[tuple[str, str, str]]:
    return {(short(r.relationship.source_id), r.relationship.type, short(r.relationship.target_id))
            for r in context.relationships}  # fmt: skip


# ----- End to end -----


def test_end_to_end_how_is_authentication_implemented(settings: Settings, make_zip: MakeZip) -> None:
    project = ProjectService(settings).create_from_zip(io.BytesIO(make_zip(FILES)), "app.zip")
    database = FakeNeo4j()
    client = Neo4jClient("bolt://localhost:7687", "neo4j", "secret", "neo4j",
                         driver_factory=fake_driver_factory(database))  # fmt: skip
    GraphService(settings, GraphRepository(client)).build_project_graph(project.id)
    store, embeddings = memory_store(), HashingEmbeddings()
    VectorIndexService(settings, store, embeddings).index_project(project.id)
    graphrag = GraphRAGService(
        VectorRetrievalService(store, embeddings, settings),
        GraphRetrievalService(GraphRepository(client)),
        settings,
    )

    context = graphrag.build_context(project.id, QUESTION)

    # The ZIP's single top folder ("app/") is unwrapped at import (Phase 2).
    login, find_user, create_token = (name.removeprefix("app/") for name in (LOGIN, FIND_USER,
                                                                          CREATE_TOKEN))  # fmt: skip
    assert context.graph_status == GraphStatus.COMPLETE and context.warnings == ()
    seeds = [short(seed.entity_id) for seed in context.seeds]
    assert login in seeds and all(seed.in_graph for seed in context.seeds)
    found = {short(e.entity.id) for e in context.entities}
    assert {login, find_user, create_token} <= found
    assert (login, "CALLS", find_user) in edges(context)
    assert (login, "CALLS", create_token) in edges(context)
    [login_source] = [s for s in context.sources if short(s.entity_id) == login]
    assert login_source.location == "auth/service.py:14-20"
    assert login_source.reason == SourceReason.VECTOR and login_source.score is not None
    outline = context.describe()
    assert "  AuthService.login CALLS UserRepository.find_user" in outline
    assert "  AuthService.login CALLS AuthService.create_token" in outline
    assert any(line.startswith("  auth/service.py:14-20  AuthService.login") for line in outline)


# ----- Seeds -----


def test_vector_seeds_come_from_the_vector_retrieval(world: World) -> None:
    context = world.service().build_context(PROJECT_A, "check the user password and create a token")

    assert context.vector_results and len(context.vector_results) <= 10
    assert [s.entity_id for s in context.seeds] == list(
        dict.fromkeys(h.chunk.entity_id for h in context.vector_results)
    )[:5]
    assert [s.rank for s in context.seeds] == list(range(1, len(context.seeds) + 1))
    scores = [s.score for s in context.seeds]
    assert scores == sorted(scores, reverse=True)


def test_duplicate_chunks_of_one_entity_make_one_seed(world: World) -> None:
    hits = [hit(PROJECT_A, "AuthService.login", 0.9), hit(PROJECT_A, "AuthService.login", 0.8, part=2),
            hit(PROJECT_A, "AuthService.create_token", 0.7)]  # fmt: skip
    context = world.service(FixedHits(hits)).build_context(PROJECT_A, QUESTION)

    [login, token] = context.seeds
    assert (short(login.entity_id), login.score, len(login.chunk_ids)) == (LOGIN, 0.9, 2)
    assert (short(token.entity_id), token.rank) == (CREATE_TOKEN, 2)
    # login is expanded once: one query per expansion (container, callees, callers).
    expansions = [q for q, p in world.database.queries
                  if p.get("entity_id") == full(PROJECT_A, LOGIN) and "(start:Entity" in q]  # fmt: skip
    assert len(expansions) == 3
    assert len(context.vector_results) == 3  # every chunk stays as vector evidence
    assert [s.chunk_id for s in context.sources][:2] == [f"{full(PROJECT_A, LOGIN)}|1",
                                                         f"{full(PROJECT_A, LOGIN)}|2"]


def test_multiple_seeds_share_relationships_once(world: World) -> None:
    context = with_seeds(world, "AuthService.login", "AuthService.create_token")

    calls = [r for r in context.relationships
             if (short(r.relationship.source_id), short(r.relationship.target_id)) == (LOGIN, CREATE_TOKEN)]
    assert len(calls) == 1  # found as login's callee and create_token's caller: kept once
    assert calls[0].seed_id == full(PROJECT_A, LOGIN) and calls[0].expansion == Expansion.CALLEES
    assert CREATE_TOKEN not in neighbor_ids(context)  # a seed, not a neighbor
    [auth_service] = [e for e in context.entities if short(e.entity.id) == AUTH_SERVICE]
    assert [short(s) for s in auth_service.seed_ids] == [LOGIN, CREATE_TOKEN]


# ----- Expansion strategy -----


def test_method_seed_expansion(world: World) -> None:
    context = with_seeds(world, "AuthService.login")

    [seed] = context.seeds
    assert seed.expansions == (Expansion.CONTAINER, Expansion.CALLEES, Expansion.CALLERS)
    assert neighbor_ids(context) == [AUTH_SERVICE, CREATE_TOKEN, VERIFY, USER_REPO, FIND_USER]
    assert edges(context) == {
        (AUTH_SERVICE, "CONTAINS", LOGIN),
        (LOGIN, "CALLS", CREATE_TOKEN), (LOGIN, "CALLS", VERIFY),
        (LOGIN, "CALLS", USER_REPO), (LOGIN, "CALLS", FIND_USER),
    }  # fmt: skip


def test_class_seed_expansion(world: World) -> None:
    context = with_seeds(world, "UserRepository")

    [seed] = context.seeds
    assert seed.expansions == (Expansion.CONTAINER, Expansion.MEMBERS, Expansion.PARENTS,
                               Expansion.SUBCLASSES, Expansion.INTERFACES, Expansion.CALLERS)
    assert neighbor_ids(context) == [REPO, FIND_USER, SAVE_USER, CACHED_REPO, LOGIN]
    assert (CACHED_REPO, "INHERITS", USER_REPO) in edges(context)
    assert (LOGIN, "CALLS", USER_REPO) in edges(context)  # who creates it


def test_file_seed_expansion(world: World) -> None:
    context = with_seeds(world, REPO)

    [seed] = context.seeds
    assert seed.expansions == (Expansion.MEMBERS, Expansion.DEPENDENCIES, Expansion.DEPENDENTS)
    assert neighbor_ids(context) == [USER_REPO, CACHED_REPO, DB, AUTH]
    assert (REPO, "DEPENDS_ON", DB) in edges(context)
    assert (AUTH, "DEPENDS_ON", REPO) in edges(context)
    assert "IMPORTS" not in {r.relationship.type for r in context.relationships}


def test_seed_without_graph_neighbors(world: World) -> None:
    context = with_seeds(world, "render_bar_chart")

    assert context.graph_status == GraphStatus.COMPLETE and context.warnings == ()
    assert edges(context) == {("app/reports/charts.py", "CONTAINS", "app/reports/charts.py:render_bar_chart")}
    assert len(context.sources) == 2  # the chunk, and its file reached through CONTAINS


def test_paths_connect_the_top_seeds(world: World) -> None:
    context = with_seeds(world, "AuthService.login", "Database.query")

    [path] = context.paths
    assert [short(n.id) for n in path.nodes] == [LOGIN, FIND_USER, QUERY]
    assert [r.type for r in path.relationships] == ["CALLS", "CALLS"]
    assert "  path: AuthService.login -> UserRepository.find_user -> Database.query" in context.describe()
    assert with_seeds(world, "AuthService.login", "Database.query", graphrag_path_seeds=0).paths == ()


# ----- Missing entities -----


def test_seed_missing_from_the_graph_is_kept_as_vector_evidence(world: World) -> None:
    # Indexed before a rename: the vector index still has it, the rebuilt graph does not.
    removed = full(PROJECT_A, f"{AUTH}:Removed.old")
    ghost = hit(PROJECT_A, "AuthService.login", 0.9)
    ghost = ChunkSearchResult(replace(ghost.chunk, entity_id=removed, id=f"{removed}|1"), 0.9)
    hits = FixedHits([ghost, hit(PROJECT_A, "AuthService.create_token", 0.5)])
    context = world.service(hits).build_context(PROJECT_A, QUESTION)

    missing, token = context.seeds
    assert not missing.in_graph and missing.expansions == ()
    assert token.in_graph
    assert context.graph_status == GraphStatus.COMPLETE
    assert "not in the knowledge graph" in context.warnings[0]
    assert context.sources[0].entity_id == missing.entity_id  # still cited
    assert context.statistics.seeds_missing == 1


# ----- Project isolation -----


def test_projects_never_mix(world: World) -> None:
    context = world.service().build_context(PROJECT_A, "user password token database")

    ids = [h.chunk.project_id for h in context.vector_results]
    ids += [e.entity.project_id for e in context.entities]
    ids += [node.project_id for path in context.paths for node in path.nodes]
    assert ids and set(ids) == {PROJECT_A}
    assert all(s.entity_id.startswith(PROJECT_A) for s in context.sources)
    assert all(p["project_id"] == PROJECT_A for p in world.graph_parameters())


def test_a_hit_of_another_project_is_ignored(world: World) -> None:
    hits = [hit(PROJECT_B, "AuthService.login", 0.95), hit(PROJECT_A, "AuthService.create_token", 0.5)]
    context = world.service(FixedHits(hits)).build_context(PROJECT_A, QUESTION)

    assert [short(s.entity_id) for s in context.seeds] == [CREATE_TOKEN]
    assert {h.chunk.project_id for h in context.vector_results} == {PROJECT_A}
    assert "another project" in context.warnings[0]
    assert all(p["project_id"] == PROJECT_A for p in world.graph_parameters())


def test_invalid_input_fails_before_the_graph(world: World) -> None:
    with pytest.raises(ProjectNotFoundError):
        world.service().build_context("../etc", QUESTION)
    with pytest.raises(InvalidVectorQueryError):
        world.service().build_context(PROJECT_A, "   ")
    assert world.database.queries == []


# ----- Failures -----


def test_neo4j_unavailable_keeps_the_vector_evidence(world: World) -> None:
    def down(_query: str, _parameters: dict[str, Any]) -> None:
        raise ServiceUnavailable("connection refused")

    world.database.before_query = down
    context = world.service().build_context(PROJECT_A, "user password token")

    assert context.graph_status == GraphStatus.UNAVAILABLE
    assert context.vector_results and context.entities == () and context.relationships == ()
    assert all(not seed.in_graph for seed in context.seeds)
    assert len(context.sources) == len(context.vector_results)
    assert "could not be queried" in context.warnings[0]
    assert context.statistics.graph_queries == 1  # stopped at the first failure

    with pytest.raises(GraphDatabaseUnavailableError):
        world.service(graphrag_require_graph=True).build_context(PROJECT_A, "user password token")


def test_neo4j_failing_midway_gives_a_partial_context(world: World) -> None:
    calls = {"count": 0}

    def fail_later(_query: str, _parameters: dict[str, Any]) -> None:
        calls["count"] += 1
        if calls["count"] > 3:
            raise ServiceUnavailable("connection lost")

    world.database.before_query = fail_later
    context = with_seeds(world, "AuthService.login", "UserRepository")

    assert context.graph_status == GraphStatus.PARTIAL
    login, repository = context.seeds
    assert login.in_graph and login.expansions == (Expansion.CONTAINER, Expansion.CALLEES)
    assert not repository.in_graph
    assert (LOGIN, "CALLS", FIND_USER) in edges(context)  # collected before the failure
    assert len(context.warnings) == 1


def test_qdrant_unavailable_fails_before_the_graph(world: World) -> None:
    client = QdrantClient(url="http://127.0.0.1:9", timeout=1, check_compatibility=False)
    down = VectorRetrievalService(QdrantVectorStore(client, "chunks"), world.embeddings)

    with pytest.raises(VectorStoreUnavailableError):
        world.service(down).build_context(PROJECT_A, QUESTION)
    assert world.database.queries == []


# ----- Limits -----


def test_context_entity_limit_keeps_seeds_and_consistent_relationships(world: World) -> None:
    context = with_seeds(world, "AuthService.login", "UserRepository",
                         graphrag_max_seeds=2, graphrag_max_context_entities=4)  # fmt: skip

    roles = [e.role for e in context.entities]
    assert len(context.entities) == 4 and roles[:2] == [EntityRole.SEED, EntityRole.SEED]
    kept = {e.entity.id for e in context.entities}
    for item in context.relationships:
        assert {item.relationship.source_id, item.relationship.target_id} <= kept
    assert context.statistics.entities_dropped > 0
    assert {s.entity_id for s in context.sources if s.reason == SourceReason.GRAPH} <= kept


def test_neighbors_per_expansion_and_seed_limits(world: World) -> None:
    context = with_seeds(world, "AuthService.login", "UserRepository", "Database",
                         graphrag_neighbors_per_expansion=1, graphrag_max_seeds=2)  # fmt: skip

    assert len(context.seeds) == 2 and len(context.vector_results) == 3
    limits = [p["limit"] for q, p in world.database.queries if "LIMIT $limit" in q
              and "allShortestPaths" not in q]  # fmt: skip
    assert set(limits) == {1}
    callees = [r for r in context.relationships
               if r.expansion == Expansion.CALLEES]  # fmt: skip
    assert len(callees) == 1


def test_graph_expansion_is_bounded_to_one_hop_and_limited_paths(world: World) -> None:
    with_seeds(world, "AuthService.login", "UserRepository", "Database.query")

    queries = [q for q, _ in world.database.queries]
    assert not any("*1.." in q and "allShortestPaths" not in q for q in queries)  # no multi-hop
    path_queries = [p for q, p in world.database.queries if "allShortestPaths" in q]
    assert path_queries and all(p["limit"] == 1 for p in path_queries)
    assert len(path_queries) <= 6  # 3 pairs, at most both directions each


@pytest.mark.parametrize(
    "settings",
    [{"graphrag_max_seeds": 0}, {"graphrag_max_seeds": 21}, {"graphrag_neighbors_per_expansion": 0},
     {"graphrag_max_context_entities": 3, "graphrag_max_seeds": 5}, {"graphrag_path_seeds": 6},
     {"graphrag_path_max_depth": 6}],
)  # fmt: skip
def test_limits_are_validated(world: World, settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        world.service(**settings)


def test_vector_top_k_is_passed_to_the_vector_retrieval(world: World) -> None:
    context = world.service(graphrag_vector_top_k=3).build_context(PROJECT_A, "user password")
    assert len(context.vector_results) == 3
    with pytest.raises(InvalidVectorQueryError):
        world.service(graphrag_vector_top_k=500).build_context(PROJECT_A, "user")


# ----- Determinism and metadata -----


def test_same_question_same_context(world: World) -> None:
    first = world.service().build_context(PROJECT_A, "user password token database")
    again = world.service().build_context(PROJECT_A, "user password token database")

    assert [h.chunk.id for h in again.vector_results] == [h.chunk.id for h in first.vector_results]
    assert [(s.entity_id, s.chunk_ids, s.expansions) for s in again.seeds] == [
        (s.entity_id, s.chunk_ids, s.expansions) for s in first.seeds
    ]
    assert again.entities == first.entities
    assert again.relationships == first.relationships
    assert again.paths == first.paths
    assert [(s.location, s.entity_id) for s in again.sources] == [
        (s.location, s.entity_id) for s in first.sources
    ]


def test_source_metadata_is_preserved(world: World) -> None:
    context = with_seeds(world, "AuthService.login")

    vector_source, *graph_sources = context.sources
    assert (vector_source.reason, vector_source.score, vector_source.chunk_id) == (
        SourceReason.VECTOR, 0.9, f"{full(PROJECT_A, LOGIN)}|1",
    )  # fmt: skip
    assert (vector_source.location, vector_source.qualified_name, vector_source.entity_type) == (
        f"{AUTH}:14-20", "AuthService.login", "method",
    )  # fmt: skip
    find_user = next(s for s in graph_sources if short(s.entity_id) == FIND_USER)
    assert (find_user.reason, find_user.location, find_user.score) == (SourceReason.GRAPH, f"{REPO}:7-9", None)
    assert len({(s.location, s.entity_id) for s in context.sources}) == len(context.sources)


def test_relationship_metadata_is_preserved(world: World) -> None:
    context = with_seeds(world, "AuthService.login")

    [call] = [r.relationship for r in context.relationships
              if (short(r.relationship.source_id), short(r.relationship.target_id)) == (LOGIN, FIND_USER)]
    assert call.id == f"CALLS:{full(PROJECT_A, LOGIN)}->{full(PROJECT_A, FIND_USER)}"
    assert (call.type, call.file_path, call.line, call.column) == ("CALLS", AUTH, 17, 16)
    [found] = [e for e in context.entities if short(e.entity.id) == FIND_USER]
    assert (found.entity.file_path, found.entity.start_line, found.entity.end_line) == (REPO, 7, 9)
    assert found.role == EntityRole.NEIGHBOR and [short(s) for s in found.seed_ids] == [LOGIN]


# ----- No LLM -----


def test_only_the_two_retrieval_services_are_used(world: World) -> None:
    vector = FixedHits([hit(PROJECT_A, "AuthService.login", 0.9)])

    context = world.service(vector).build_context(PROJECT_A, QUESTION)

    assert vector.calls == [(PROJECT_A, QUESTION)]  # one vector search, then graph queries only
    assert context.statistics.graph_queries > 0 and world.database.queries
    assert not {"openai", "anthropic", "groq", "langchain"} & {m.split(".")[0] for m in sys.modules}


def test_a_single_relationship_already_in_the_context_is_not_a_path(world: World) -> None:
    # AuthService CONTAINS login: found as login's container, so no one-step "path" repeats it.
    context = with_seeds(world, "AuthService", "AuthService.login")

    assert (AUTH_SERVICE, "CONTAINS", LOGIN) in edges(context)
    assert context.paths == ()
