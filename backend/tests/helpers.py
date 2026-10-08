"""What the tests share: a small sample project, a fake LLM and a fake retrieval.

The sample project is four Python files. Its code is written so that every kind of
relationship appears once: an import, calls across files, a call in the same file, and a
class that inherits from another.

    auth/service.py      AuthService.login() -> UserRepository.find_user(), verify_password()
    repository/user.py   UserRepository, CachedUserRepository(UserRepository)
    db/database.py       Database
    reports/charts.py    render_bar_chart()   (unrelated to the rest)
"""

from app.extraction.models import ExtractionReport
from app.extraction.service import EntityExtractionService
from app.graphrag.models import GraphRAGContext, GraphRAGStatistics, GraphStatus, Source, SourceReason
from app.llm.models import LLMCompletion
from app.llm.provider import LLMProvider
from app.parsing.service import ParserService
from app.rag.chunker import CodeChunker
from app.rag.models import ChunkSearchResult
from app.relationships.models import RelationshipReport
from app.relationships.service import RelationshipExtractionService

PROJECT_ID = "a" * 32

FILES = {
    "auth/service.py": '''"""Authentication of users: password check and access tokens."""
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
    "repository/user.py": '''from db.database import Database


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
    "db/database.py": '''class Database:
    """A tiny in-memory database."""

    def query(self, table, key):
        return None

    def insert(self, table, row):
        pass
''',
    "reports/charts.py": '''def render_bar_chart(values, width=40):
    """Draw a horizontal bar chart of monthly sales figures as text."""
    peak = max(values) or 1
    return "\\n".join("#" * int(width * value / peak) for value in values)
''',
}

parser_service = ParserService(max_file_bytes=1024 * 1024)
extraction_service = EntityExtractionService(parser_service)
relationship_service = RelationshipExtractionService(extraction_service)


def analyze(files: dict[str, str], project_id: str = PROJECT_ID) -> RelationshipReport:
    """Parse the files, extract their entities and resolve their relationships (no disk)."""
    extraction = ExtractionReport(project_id=project_id)
    references = []
    for path, source in files.items():
        parse_result = parser_service.parse_source(source.encode(), path)
        file_entities = extraction_service.extract(parse_result, project_id)
        extraction.files.append(file_entities)
        references.append(relationship_service.collect(parse_result, file_entities, project_id))
    return relationship_service.resolve(extraction, references)


def short(entity_id: str) -> str:
    """An entity ID without its project prefix: "auth/service.py:AuthService.login"."""
    return entity_id.removeprefix(f"{PROJECT_ID}:")


def edges(report: RelationshipReport) -> set[tuple[str, str, str]]:
    """Every relationship as (source, TYPE, target), with short IDs."""
    return {(short(r.source_id), r.type.value, short(r.target_id)) for r in report.relationships}


class FakeLLM(LLMProvider):
    """An LLM that returns a fixed answer (or raises), and records what it was sent."""

    def __init__(self, answer: str = "An answer.", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, str]] = []

    @property
    def model(self) -> str:
        return "fake/model"

    def generate(self, system_prompt: str, user_prompt: str) -> LLMCompletion:
        self.calls.append((system_prompt, user_prompt))
        if self.error:
            raise self.error
        return LLMCompletion(self.answer, self.model)


def sample_context(question: str = "How is authentication implemented?", hits: int = 2) -> GraphRAGContext:
    """A retrieval result built by hand: the first `hits` chunks of auth/service.py as vector
    hits, each one a numbered source. `hits=0` is "nothing was retrieved"."""
    path = "auth/service.py"
    source = FILES[path].encode()
    entities = extraction_service.extract(parser_service.parse_source(source, path), PROJECT_ID)
    chunks = [c for c in CodeChunker().chunk_file(PROJECT_ID, source, entities) if c.entity_type != "file"]
    results = tuple(ChunkSearchResult(chunk, 0.9 - 0.1 * i) for i, chunk in enumerate(chunks[:hits]))
    sources = tuple(
        Source(r.chunk.file_path, r.chunk.start_line, r.chunk.end_line, r.chunk.entity_id,
               r.chunk.qualified_name, r.chunk.entity_type, SourceReason.VECTOR, r.score, r.chunk.id)
        for r in results
    )  # fmt: skip
    statistics = GraphRAGStatistics(
        vector_hits=len(results), seeds=0, seeds_expanded=0, seeds_missing=0, graph_queries=0,
        entities=0, relationships=0, paths=0, sources=len(sources), entities_dropped=0,
    )  # fmt: skip
    return GraphRAGContext(
        project_id=PROJECT_ID, query=question, vector_results=results, seeds=(), entities=(),
        relationships=(), paths=(), sources=sources, graph_status=GraphStatus.COMPLETE,
        warnings=(), statistics=statistics,
    )  # fmt: skip


class FakeRetrieval:
    """Stands for GraphRAGService: returns a ready context (or raises), and counts its calls."""

    def __init__(self, context: GraphRAGContext | None = None, error: Exception | None = None) -> None:
        self.context = context or sample_context()
        self.error = error
        self.calls = 0

    def build_context(self, project_id: str, query: str) -> GraphRAGContext:
        self.calls += 1
        if self.error:
            raise self.error
        return self.context
