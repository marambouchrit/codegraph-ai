"""Shared setup for the analysis tests: real services on fake databases, no server, no model.

A real project is imported (ZIP, temporary workspace) and analyzed by the real
AnalysisService and AnalysisPipeline over a fake Neo4j, an in-memory Qdrant and hashing
embeddings. Jobs run through a test runner instead of a background thread:

    InlineRunner   runs the job at once: POST /analyze returns when it has finished
    ManualRunner   keeps jobs until `run_next()`: to look at the queued state
"""

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.analysis.jobs import Job
from app.core.config import Settings
from app.core.errors import SourceFileError
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.llm.generator import LLMGenerationService
from app.main import app
from app.services.analysis_service import AnalysisService
from app.services.chat_service import ChatService
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graph_service import GraphService
from app.services.graphrag_service import GraphRAGService
from app.services.project_service import ProjectService
from app.services.vector_index_service import VectorIndexService
from app.services.vector_retrieval_service import VectorRetrievalService
from tests.conftest import MakeZip
from tests.graph_fakes import FakeNeo4j, fake_driver_factory
from tests.test_graphrag import FILES
from tests.test_llm import FakeLLM
from tests.vector_helpers import HashingEmbeddings, memory_store

SECRET = "neo4j-password-SECRET-0123"


class InlineRunner:
    def submit(self, job: Job) -> None:
        job()


class ManualRunner:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    def submit(self, job: Job) -> None:
        self.jobs.append(job)

    def run_next(self) -> None:
        self.jobs.pop(0)()


class AnalysisWorld:
    """The API client over real services on fake databases; records what was really done."""

    def __init__(self, settings: Settings, make_zip: MakeZip, files: dict[str, Any] | None = None) -> None:
        self.settings = settings
        self.make_zip = make_zip
        self.projects = ProjectService(settings)
        self.project_id = self.import_project(files or FILES)
        self.database = FakeNeo4j()
        self.neo4j = Neo4jClient("bolt://localhost:7687", "neo4j", SECRET, "neo4j",
                                 driver_factory=fake_driver_factory(self.database))  # fmt: skip
        self.store = memory_store()
        self.embeddings: HashingEmbeddings = HashingEmbeddings()
        self.runner: Any = InlineRunner()
        self.parsed: list[str] = []  # paths given to the parser, in order
        self.unparsable: set[str] = set()  # paths the parser refuses (as for a bad file)
        self.graph_services = 0  # times the graph service was built (a job ran)
        self.client = TestClient(app, raise_server_exceptions=False)

    def import_project(self, files: dict[str, Any]) -> str:
        return self.projects.create_from_zip(io.BytesIO(self.make_zip(files)), "auth.zip").id

    # ----- The service under test -----

    def service(self) -> AnalysisService:
        def graph() -> GraphService:
            self.graph_services += 1
            service = GraphService(self.settings, GraphRepository(self.neo4j), self.projects)
            parser = service.relationship_service.extraction_service.parser_service
            parse_file = parser.parse_file

            def recorded(root: Path, path: str, *args: Any, **kwargs: Any) -> Any:
                self.parsed.append(path)
                if path in self.unparsable:
                    raise SourceFileError(f"'{path}' cannot be parsed.")
                return parse_file(root, path, *args, **kwargs)

            parser.parse_file = recorded  # type: ignore[method-assign]
            return service

        def vectors() -> VectorIndexService:
            return VectorIndexService(self.settings, self.store, self.embeddings, self.projects)

        return AnalysisService(self.projects, graph, vectors, self.runner)

    def chat_service(self) -> ChatService:
        graphrag = GraphRAGService(VectorRetrievalService(self.store, self.embeddings),
                                   GraphRetrievalService(GraphRepository(self.neo4j)), Settings())  # fmt: skip
        llm = LLMGenerationService(FakeLLM("`AuthService.login` checks the password [1]."))
        return ChatService(self.projects, lambda: graphrag, lambda: llm)

    # ----- HTTP -----

    def post(self, project_id: str | None = None, **params: Any) -> Any:
        return self.client.post(f"/projects/{project_id or self.project_id}/analyze", params=params)

    def get(self, project_id: str | None = None) -> Any:
        return self.client.get(f"/projects/{project_id or self.project_id}/analysis")

    def analyze(self, project_id: str | None = None, **params: Any) -> dict[str, Any]:
        """Start an analysis (it runs inline) and return the state it reached."""
        response = self.post(project_id, **params)
        assert response.status_code == 202, response.text
        return self.get(project_id).json()

    def ready(self, project_id: str | None = None, **params: Any) -> dict[str, Any]:
        """Analyze and return the report of a successful analysis."""
        state = self.analyze(project_id, **params)
        assert state["status"] == "ready", state
        return state["analysis"]

    # ----- The project's source code -----

    def source(self, project_id: str | None = None) -> Path:
        return self.projects.workspace.source_dir(project_id or self.project_id)

    def write(self, path: str, text: str, project_id: str | None = None) -> None:
        target = self.source(project_id) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")

    def edit(self, path: str, change: Callable[[str], str]) -> None:
        target = self.source() / path
        target.write_text(change(target.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")

    def delete(self, path: str) -> None:
        (self.source() / path).unlink()

    # ----- What the databases hold -----

    def node_names(self, project_id: str | None = None) -> set[str]:
        prefix = f"{project_id or self.project_id}:"
        return {node_id.removeprefix(prefix) for node_id in self.database.nodes if node_id.startswith(prefix)}

    def edge_names(self) -> set[tuple[str, str, str]]:
        prefix = f"{self.project_id}:"
        return {(s.removeprefix(prefix), t, target.removeprefix(prefix))
                for s, t, target in self.database.edges() if s.startswith(prefix)}  # fmt: skip

    def points(self, project_id: str | None = None) -> int:
        return self.store.count(project_id or self.project_id)
