"""POST /projects/{id}/analyze and GET /projects/{id}/analysis, with no server and no model.

Phase 11.5 made analysis an endpoint, Phase 13 persisted its state, Phase 14 made it a
background job with real progress. Real services run on fake databases (see
tests/analysis_helpers.py); jobs run inline, or by hand to look at the queued state.
"""

import ast
import json
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable

from app.analysis.jobs import ThreadJobRunner
from app.api import dependencies
from app.api.dependencies import get_analysis_service, get_chat_service, get_project_service
from app.core.config import Settings
from app.core.errors import AnalysisInProgressError, VectorStoreUnavailableError
from app.main import app
from app.schemas.analysis import AnalysisResponse
from app.services import analysis_service
from app.services.analysis_pipeline import PHASES
from app.services.analysis_service import INTERRUPTED, UNEXPECTED
from app.services.analysis_store import AnalysisChanges, AnalysisResult
from app.services.project_service import ProjectService
from tests.analysis_helpers import SECRET, AnalysisWorld, ManualRunner
from tests.conftest import MakeZip
from tests.test_graphrag import FILES, QUESTION
from tests.vector_helpers import FailingEmbeddings, HashingEmbeddings, Vector


@pytest.fixture
def world(settings: Settings, make_zip: MakeZip) -> Iterator[AnalysisWorld]:
    world = AnalysisWorld(settings, make_zip)
    app.dependency_overrides[get_project_service] = lambda: world.projects
    app.dependency_overrides[get_analysis_service] = world.service
    app.dependency_overrides[get_chat_service] = world.chat_service
    yield world
    app.dependency_overrides.clear()
    analysis_service._active.clear()


# ----- Starting an analysis -----


def test_analyze_returns_at_once_with_a_queued_job(world: AnalysisWorld) -> None:
    world.runner = ManualRunner()

    response = world.post()

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"project_id", "status", "job", "analysis"}
    assert (body["status"], body["analysis"]) == ("queued", None)
    job = body["job"]
    assert set(job) == {"job_id", "status", "mode", "phase", "completed", "total", "unit",
                        "queued_at", "started_at", "finished_at", "error"}  # fmt: skip
    assert job["status"] == "queued" and len(job["job_id"]) == 32
    assert (job["started_at"], job["phase"], job["error"]) == (None, None, None)
    # Nothing was analyzed yet: the work is waiting for the worker.
    assert world.graph_services == 0 and world.database.queries == []
    assert world.get().json()["status"] == "queued"


def test_the_worker_then_runs_the_job_to_ready(world: AnalysisWorld) -> None:
    world.runner = ManualRunner()
    job_id = world.post().json()["job"]["job_id"]

    world.runner.run_next()

    body = world.get().json()
    assert body["status"] == "ready" and body["job"]["status"] == "ready"
    assert body["job"]["job_id"] == job_id
    assert body["job"]["started_at"] and body["job"]["finished_at"] and body["job"]["error"] is None
    assert body["job"]["phase"] == "finalizing" and body["job"]["mode"] == "full"
    assert body["analysis"]["status"] == "ready"


def test_analysis_builds_the_graph_and_the_vector_index(world: AnalysisWorld) -> None:
    report = world.ready()

    assert set(report) == {"project_id", "status", "mode", "graph", "vectors", "changes",
                           "failed_files", "warnings", "duration_seconds", "analyzed_at"}  # fmt: skip
    assert report["project_id"] == world.project_id and report["status"] == "ready"
    assert report["failed_files"] == 0 and report["warnings"] == []
    # What the report says is what the databases hold.
    graph, vectors = report["graph"], report["vectors"]
    assert graph["entities"] == len(world.database.nodes) > 0
    assert graph["relationships"] == len(world.database.relationships) > 0
    assert vectors["chunks"] == world.points() > 0
    assert graph["files"] == vectors["files"] == len(FILES)
    assert vectors["embedding_model"] == "test/hashing"
    assert (graph["stale_entities_removed"], vectors["stale_chunks_removed"]) == (0, 0)


def test_the_report_has_no_internal_details(world: AnalysisWorld) -> None:
    report = world.ready()

    assert set(report["graph"]) == {"files", "entities", "relationships", "entities_by_type",
                                    "relationships_by_type", "unresolved_references",
                                    "stale_entities_removed"}  # fmt: skip
    assert set(report["vectors"]) == {"files", "chunks", "chunks_by_type", "embedding_model",
                                      "stale_chunks_removed"}  # fmt: skip
    assert sum(report["graph"]["entities_by_type"].values()) == report["graph"]["entities"]
    assert sum(report["graph"]["relationships_by_type"].values()) == report["graph"]["relationships"]
    assert sum(report["vectors"]["chunks_by_type"].values()) == report["vectors"]["chunks"]
    assert "CALLS" in report["graph"]["relationships_by_type"]
    # No build or index ID, no hash, no vector, no dimension, no credential.
    text = json.dumps(world.get().json())
    for hidden in ("build_id", "index_id", "sha256", "content_hash", "dimension", SECRET):
        assert hidden not in text


def test_no_body_is_needed_and_extra_input_changes_nothing(world: AnalysisWorld) -> None:
    response = world.client.post(
        f"/projects/{world.project_id}/analyze",
        params={"cypher": "MATCH (n) DETACH DELETE n", "model": "x"},
        json={"cypher": "MATCH (n) DETACH DELETE n", "path": "/etc"},
    )

    assert response.status_code == 202 and world.get().json()["status"] == "ready"
    assert all("DETACH DELETE n" != query for query, _ in world.database.queries)


@pytest.mark.parametrize("full", ["maybe", "2"])
def test_an_invalid_full_flag_is_422(world: AnalysisWorld, full: str) -> None:
    assert world.client.post(f"/projects/{world.project_id}/analyze?full={full}").status_code == 422
    assert world.get().json()["status"] == "not_analyzed"


def test_warnings_for_skipped_files_and_an_empty_index() -> None:
    result = AnalysisResult(
        project_id="p", mode="full", files=3, failed_files=1, entities=5, relationships=4,
        entities_by_type={"File": 3}, relationships_by_type={"CONTAINS": 4},
        unresolved_references=0, chunks=0, chunks_by_type={}, embedding_model="test/hashing",
        changes=AnalysisChanges(), duration_seconds=1.0,
    )  # fmt: skip

    response = AnalysisResponse.from_result(result)

    assert response.status == "ready" and response.failed_files == 1
    assert response.warnings == [
        "1 file(s) could not be parsed and were skipped.",
        "No code could be indexed: chat will not find anything to answer from.",
    ]


def test_an_analyzed_project_can_be_chatted_with(world: AnalysisWorld) -> None:
    url = f"/projects/{world.project_id}/chat"
    before = world.client.post(url, json={"question": QUESTION})
    assert "insufficient" in before.json()["answer"]  # not analyzed yet

    world.ready()
    response = world.client.post(url, json={"question": QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "`AuthService.login` checks the password [1]."
    assert body["cited"] == [1] and body["graph_status"] == "complete"
    assert body["sources"][0]["entity"] == "AuthService.login"
    assert any(source["found_by"] == "graph" for source in body["sources"])


# ----- Real progress -----


class WatchingEmbeddings(HashingEmbeddings):
    """Reads the analysis state over HTTP while the job is embedding."""

    def __init__(self, world: AnalysisWorld) -> None:
        super().__init__()
        self.world = world
        self.seen: list[dict[str, Any]] = []

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        self.seen.append(self.world.get().json())
        return super().embed_documents(texts)


def test_the_running_state_reports_real_counts(world: AnalysisWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analysis_service, "PROGRESS_SAVE_SECONDS", 0)  # save every count
    watcher = WatchingEmbeddings(world)
    world.embeddings = watcher
    world.settings.embedding_batch_size = 4  # several batches

    report = world.ready()

    total = report["vectors"]["chunks"]
    assert len(watcher.seen) == -(-total // 4)  # one look per batch
    for index, state in enumerate(watcher.seen):
        assert state["status"] == "running" and state["analysis"] is None
        job = state["job"]
        assert (job["status"], job["phase"], job["mode"]) == ("running", "embedding", "full")
        # Exactly the chunks embedded so far, out of the chunks that need an embedding.
        assert (job["completed"], job["total"], job["unit"]) == (index * 4, total, "chunks")
        assert job["started_at"] and job["finished_at"] is None


def test_phases_come_in_order_and_only_countable_ones_have_counts(
    world: AnalysisWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[dict[str, Any]] = []
    store_save = analysis_service.AnalysisStore.save

    def recording(self: Any, state: Any) -> None:
        store_save(self, state)
        if state.job is not None and state.job.phase:
            saved.append({"phase": state.job.phase, "completed": state.job.completed,
                          "total": state.job.total, "unit": state.job.unit})  # fmt: skip

    monkeypatch.setattr(analysis_service.AnalysisStore, "save", recording)
    world.ready()

    phases = list(dict.fromkeys(entry["phase"] for entry in saved))
    assert phases == list(PHASES)
    for entry in saved:
        if entry["phase"] in ("parsing", "embedding"):
            assert entry["unit"] == ("files" if entry["phase"] == "parsing" else "chunks")
            assert 0 <= entry["completed"] <= entry["total"]
        else:  # nothing to count: no invented numbers
            assert (entry["completed"], entry["total"], entry["unit"]) == (None, None, None)
    parsing = [entry for entry in saved if entry["phase"] == "parsing"]
    assert parsing[-1]["completed"] == parsing[-1]["total"] == len(FILES)


def test_an_incremental_run_counts_only_what_changed(world: AnalysisWorld) -> None:
    world.ready()
    watcher = WatchingEmbeddings(world)
    world.embeddings = watcher
    world.edit("auth/service.py", lambda text: text.replace('"invalid credentials"', '"wrong"'))

    report = world.ready()

    [state] = watcher.seen
    assert state["status"] == "running"
    assert state["analysis"] is not None  # the previous report is still valid while it runs
    job = state["job"]
    assert (job["mode"], job["phase"], job["completed"], job["total"]) == ("incremental", "embedding", 0, 1)
    assert report["changes"]["chunks_embedded"] == 1


# ----- Concurrency -----


def test_a_second_analysis_of_the_same_project_is_refused(world: AnalysisWorld) -> None:
    world.runner = ManualRunner()
    first = world.post().json()

    second = world.post()

    assert second.status_code == 409
    assert "already being analyzed" in second.json()["detail"]
    assert len(world.runner.jobs) == 1  # no second job
    assert world.get().json()["job"]["job_id"] == first["job"]["job_id"]
    # Once it has finished, the project can be analyzed again.
    world.runner.run_next()
    assert world.post().status_code == 202


def test_a_running_analysis_refuses_another_one(world: AnalysisWorld) -> None:
    statuses: list[int] = []

    class Reentrant(HashingEmbeddings):
        def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
            statuses.append(world.post().status_code)  # while the job is running
            return super().embed_documents(texts)

    world.embeddings = Reentrant()
    world.ready()

    assert statuses and set(statuses) == {409}


def test_other_projects_are_queued_not_refused(world: AnalysisWorld) -> None:
    world.runner = ManualRunner()
    other = world.import_project(FILES)

    assert world.post().status_code == 202
    assert world.post(other).status_code == 202

    assert len(world.runner.jobs) == 2
    world.runner.run_next()
    assert (world.get().json()["status"], world.get(other).json()["status"]) == ("ready", "queued")
    world.runner.run_next()
    assert world.get(other).json()["status"] == "ready"
    assert world.points() == world.points(other) > 0


def test_the_service_refuses_a_concurrent_start(world: AnalysisWorld) -> None:
    world.runner = ManualRunner()
    service = world.service()
    service.start(world.project_id)

    with pytest.raises(AnalysisInProgressError) as error:
        service.start(world.project_id)
    assert error.value.status_code == 409


def test_a_real_background_thread_runs_the_job(world: AnalysisWorld) -> None:
    runner = ThreadJobRunner()
    world.runner = runner
    try:
        assert world.post().status_code == 202  # returns while the job runs elsewhere
        deadline = time.monotonic() + 20
        while world.get().json()["status"] in ("queued", "running"):
            assert time.monotonic() < deadline, "the background job did not finish"
            time.sleep(0.05)
    finally:
        runner.shutdown()

    body = world.get().json()
    assert body["status"] == "ready" and body["analysis"]["vectors"]["chunks"] == world.points()


# ----- Unknown projects -----


@pytest.mark.parametrize(
    "project_id", ["f" * 32, "not-a-valid-id", "..%2F..%2Fetc", "A" * 32, "1234"]
)
def test_unknown_or_invalid_project_is_404(world: AnalysisWorld, project_id: str) -> None:
    world.runner = ManualRunner()

    assert world.post(project_id).status_code == 404
    assert world.get(project_id).status_code == 404
    assert world.runner.jobs == [] and world.database.queries == []


def test_nothing_is_connected_for_an_unknown_project(settings: Settings) -> None:
    """The real dependency: a 404 comes before any Neo4j, Qdrant or model is created."""
    dependencies.close_chat_resources()
    app.dependency_overrides[get_project_service] = lambda: ProjectService(settings)
    try:
        response = TestClient(app).post(f"/projects/{'f' * 32}/analyze")
    finally:
        app.dependency_overrides.clear()
        dependencies.close_chat_resources()

    assert response.status_code == 404
    assert dependencies._database_clients.cache_info().currsize == 0


# ----- Failures: a job fails, the request never does -----


def test_neo4j_unavailable_fails_the_job_before_any_work(world: AnalysisWorld) -> None:
    def unavailable(_query: str, _parameters: dict[str, Any]) -> None:
        raise ServiceUnavailable(f"connection refused (password {SECRET})")

    world.database.before_query = unavailable

    state = world.analyze()

    assert state["status"] == "failed" and state["analysis"] is None
    job = state["job"]
    assert (job["status"], job["phase"]) == ("failed", "preparing")
    assert "Neo4j is not reachable" in job["error"] and SECRET not in json.dumps(state)
    assert job["finished_at"] is not None
    assert world.embeddings.documents_embedded == 0 and world.parsed == []


def test_an_embedding_failure_is_failed_not_ready(world: AnalysisWorld) -> None:
    world.embeddings = FailingEmbeddings(fail_after=0)

    state = world.analyze()

    assert state["status"] == "failed" and state["analysis"] is None
    assert state["job"]["error"] == "The embedding model failed to embed the text."
    assert world.points() == 0
    assert len(world.database.nodes) == 0  # the graph is not written before the embeddings exist


def test_qdrant_unavailable_fails_the_job(world: AnalysisWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> None:
        raise VectorStoreUnavailableError("Qdrant is not reachable.")

    monkeypatch.setattr(world.store, "verify_connectivity", unavailable)

    state = world.analyze()

    assert state["status"] == "failed" and state["job"]["error"] == "Qdrant is not reachable."
    assert world.embeddings.documents_embedded == 0


def test_unexpected_errors_leak_nothing(world: AnalysisWorld) -> None:
    class Broken(HashingEmbeddings):
        def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
            raise RuntimeError(f"boom at C:\\secret\\path with {SECRET}")

    world.embeddings = Broken()

    state = world.analyze()

    assert state["status"] == "failed" and state["job"]["error"] == UNEXPECTED
    text = json.dumps(state)
    assert SECRET not in text and "secret\\\\path" not in text and "Traceback" not in text


def test_a_failure_releases_the_project_for_the_next_analysis(world: AnalysisWorld) -> None:
    world.embeddings = FailingEmbeddings(fail_after=0)
    assert world.analyze()["status"] == "failed"

    world.embeddings = HashingEmbeddings()
    assert world.analyze()["status"] == "ready"


def test_an_interrupted_job_is_reported_failed_and_can_be_restarted(world: AnalysisWorld) -> None:
    """A job saved as running, but unknown to this process: the server stopped meanwhile."""
    world.runner = ManualRunner()
    world.post()
    analysis_service._active.clear()  # as after a restart: the queue is gone

    state = world.get().json()

    assert state["status"] == "failed"
    assert (state["job"]["status"], state["job"]["error"]) == ("failed", INTERRUPTED)
    assert world.post().status_code == 202  # not blocked by the dead job


def test_an_interrupted_incremental_job_keeps_the_previous_analysis(world: AnalysisWorld) -> None:
    first = world.ready()
    world.runner = ManualRunner()
    world.post()
    analysis_service._active.clear()

    state = world.get().json()

    assert state["status"] == "failed" and state["analysis"] == first


# ----- The persisted state -----


def test_a_new_project_is_not_analyzed(world: AnalysisWorld) -> None:
    response = world.get()

    assert response.status_code == 200
    assert response.json() == {"project_id": world.project_id, "status": "not_analyzed",
                               "job": None, "analysis": None}  # fmt: skip


def test_the_state_survives_a_restart(world: AnalysisWorld, settings: Settings) -> None:
    """A new service (new request, restarted server) reads the state from the workspace."""
    report = world.ready()
    restarted = AnalysisWorld.__new__(AnalysisWorld)
    restarted.__dict__.update(world.__dict__)
    restarted.projects = ProjectService(settings)

    state = restarted.service().get_state(world.project_id)

    assert state.status == "ready" and state.result is not None
    assert state.result.entities == report["graph"]["entities"]
    assert state.result.analyzed_at.isoformat().replace("+00:00", "Z") == report["analyzed_at"]


def test_reading_the_state_queries_no_database(world: AnalysisWorld) -> None:
    world.ready()
    world.database.queries.clear()
    jobs = world.graph_services

    assert world.get().json()["status"] == "ready"
    assert world.database.queries == [] and world.graph_services == jobs


def test_re_analysis_updates_the_state(world: AnalysisWorld) -> None:
    first = world.ready()
    world.write("extra.py", "def extra():\n    return 1\n")

    second = world.ready()

    assert world.get().json()["analysis"] == second
    assert second["analyzed_at"] > first["analyzed_at"]
    assert second["graph"]["entities"] == first["graph"]["entities"] + 2  # a file + a function
    assert second["vectors"]["files"] == first["vectors"]["files"] + 1


def test_projects_have_separate_states(world: AnalysisWorld) -> None:
    other = world.import_project(FILES)

    world.ready()

    assert world.get().json()["status"] == "ready"
    assert world.get(other).json()["status"] == "not_analyzed"


def test_a_record_written_before_phase_14_is_still_ready(world: AnalysisWorld) -> None:
    """Phase 13 stored the two build reports: the same totals are shown, as a full analysis."""
    graph = {"project_id": world.project_id, "build_id": "b", "files": 4, "nodes_written": 17,
             "relationships_written": 26, "nodes_by_label": {"File": 4, "Method": 13},
             "relationships_by_type": {"CONTAINS": 13, "CALLS": 13}, "stale_nodes_deleted": 0,
             "stale_relationships_deleted": 0, "unresolved_references": 8, "failed_files": 0,
             "duration_seconds": 0.9}  # fmt: skip
    vectors = {"project_id": world.project_id, "index_id": "i", "embedding_model": "BAAI/bge-m3",
               "dimension": 1024, "files": 4, "chunks": 15, "chunks_by_type": {"method": 15},
               "stale_chunks_deleted": 0, "failed_files": 0, "duration_seconds": 5.0}  # fmt: skip
    world.projects.workspace.analysis_file(world.project_id).write_text(json.dumps(
        {"graph": graph, "vectors": vectors, "duration_seconds": 6.0,
         "analyzed_at": "2026-10-02T18:30:00+00:00"}))  # fmt: skip

    body = world.get().json()

    assert body["status"] == "ready" and body["job"] is None
    assert (body["analysis"]["graph"]["entities"], body["analysis"]["vectors"]["chunks"]) == (17, 15)
    # It has no index: its next analysis is a full one.
    assert world.ready()["mode"] == "full"


@pytest.mark.parametrize("content", ["{not json", "{}", '{"version": 2}', '{"version": 99}', "[]"])
def test_an_unreadable_state_is_not_analyzed(world: AnalysisWorld, content: str) -> None:
    world.projects.workspace.analysis_file(world.project_id).write_text(content)

    response = world.get()

    assert response.status_code == 200 and response.json()["status"] == "not_analyzed"


def test_a_state_of_another_project_is_ignored(world: AnalysisWorld) -> None:
    other = world.import_project(FILES)
    world.ready()
    workspace = world.projects.workspace
    workspace.analysis_file(other).write_text(
        workspace.analysis_file(world.project_id).read_text(encoding="utf-8"), encoding="utf-8"
    )

    assert world.get(other).json()["analysis"] is None


def test_deleting_a_project_deletes_its_state(world: AnalysisWorld) -> None:
    world.ready()

    assert world.client.delete(f"/projects/{world.project_id}").status_code == 204
    assert world.get().status_code == 404


# ----- Documentation, architecture -----


def test_the_endpoints_are_documented(world: AnalysisWorld) -> None:
    paths = world.client.get("/openapi.json").json()["paths"]
    start = paths["/projects/{project_id}/analyze"]["post"]
    state = paths["/projects/{project_id}/analysis"]["get"]

    assert start["summary"] == "Start analyzing a project (background job)"
    assert "requestBody" not in start
    assert {"202", "404", "409"} <= set(start["responses"])
    assert [p["name"] for p in start["parameters"] if p["in"] == "query"] == ["full"]
    assert state["summary"] == "Get the analysis state and progress of a project"
    assert "404" in state["responses"]


def test_the_route_and_service_only_orchestrate() -> None:
    """No parser, database client, query, chunker or embedding in the analysis route or service."""
    forbidden = ("neo4j", "qdrant_client", "sentence_transformers", "tree_sitter",
                 "app.parsing", "app.extraction", "app.relationships", "app.graph.client",
                 "app.graph.repository", "app.graph.builder", "app.rag.chunker",
                 "app.rag.embeddings", "app.rag.vector_store", "app.llm")  # fmt: skip
    root = Path(__file__).parent.parent / "app"
    for path in (root / "api" / "routes" / "analysis.py", root / "services" / "analysis_service.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom | ast.Import):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [
                    alias.name for alias in node.names
                ]  # fmt: skip
                for name in names:
                    assert not name.startswith(forbidden), f"{path.name} imports {name}"
