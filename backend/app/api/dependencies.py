"""Shared FastAPI dependencies.

Routes ask for services with `Depends(...)`. Tests can replace them through
`app.dependency_overrides`, for example to use a temporary workspace or fake services.

The heavy pieces of analysis and chat are created once per process, on first use,
and shared: the Neo4j driver and the Qdrant client (connection pools), the embedding
model (already one per process, see app/rag/embeddings.py) and the LLM client. Nothing
is connected when the API starts, so the other endpoints work without Neo4j, Qdrant or
an LLM key. A creation that fails (LLM key missing...) is not cached: fixing .env and
restarting is enough. `close_chat_resources()` closes the connections on shutdown.
"""

from functools import lru_cache

from fastapi import Depends

from app.analysis.jobs import ThreadJobRunner
from app.core.config import Settings, get_settings
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.llm.generator import LLMGenerationService
from app.llm.provider import LLMProvider, create_llm_provider
from app.rag.embeddings import embedding_provider_from_settings
from app.rag.vector_store import QdrantVectorStore
from app.services.analysis_service import AnalysisService
from app.services.chat_service import ChatService
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graph_service import GraphService
from app.services.graphrag_service import GraphRAGService
from app.services.insights_service import InsightsService
from app.services.project_graph_service import ProjectGraphService
from app.services.project_service import ProjectService
from app.services.vector_index_service import VectorIndexService
from app.services.vector_retrieval_service import VectorRetrievalService


def get_project_service(settings: Settings = Depends(get_settings)) -> ProjectService:
    return ProjectService(settings)


@lru_cache
def _database_clients() -> tuple[Neo4jClient, QdrantVectorStore]:
    settings = get_settings()
    return Neo4jClient.from_settings(settings), QdrantVectorStore.from_settings(settings)


@lru_cache
def get_graphrag_service() -> GraphRAGService:
    settings = get_settings()
    neo4j, qdrant = _database_clients()
    embeddings = embedding_provider_from_settings(settings)
    return GraphRAGService(
        VectorRetrievalService(qdrant, embeddings, settings),
        GraphRetrievalService(GraphRepository(neo4j, settings.graph_batch_size)),
        settings,
    )


@lru_cache
def get_llm_provider() -> LLMProvider:
    return create_llm_provider(get_settings())


@lru_cache
def get_llm_generation_service() -> LLMGenerationService:
    return LLMGenerationService(get_llm_provider())


def get_chat_service(
    project_service: ProjectService = Depends(get_project_service),
) -> ChatService:
    # Factories, not instances: they are only built when a request needs them.
    return ChatService(project_service, get_graphrag_service, get_llm_generation_service)


@lru_cache
def get_job_runner() -> ThreadJobRunner:
    """The background worker of this process (one thread, started on first use)."""
    return ThreadJobRunner()


def get_analysis_service(
    project_service: ProjectService = Depends(get_project_service),
) -> AnalysisService:
    settings = get_settings()

    def graph() -> GraphService:
        neo4j, _ = _database_clients()
        repository = GraphRepository(neo4j, settings.graph_batch_size)
        return GraphService(settings, repository, project_service)

    def vectors() -> VectorIndexService:
        _, qdrant = _database_clients()
        embeddings = embedding_provider_from_settings(settings)
        return VectorIndexService(settings, qdrant, embeddings, project_service)

    # Factories, as for the chat: nothing is connected before the project is found,
    # and they are called on the worker thread, when the job runs.
    return AnalysisService(project_service, graph, vectors, get_job_runner())


def get_project_graph_service(
    project_service: ProjectService = Depends(get_project_service),
) -> ProjectGraphService:
    settings = get_settings()

    def retrieval() -> GraphRetrievalService:
        neo4j, _ = _database_clients()
        return GraphRetrievalService(GraphRepository(neo4j, settings.graph_batch_size))

    # A factory, as for the chat: nothing is connected before the project is found.
    return ProjectGraphService(project_service, retrieval)


def get_insights_service(
    project_service: ProjectService = Depends(get_project_service),
) -> InsightsService:
    settings = get_settings()

    def retrieval() -> GraphRetrievalService:
        neo4j, _ = _database_clients()
        return GraphRetrievalService(GraphRepository(neo4j, settings.graph_batch_size))

    # Factories: the LLM provider is only built for the architecture summary.
    return InsightsService(project_service, retrieval, get_llm_provider)


def close_chat_resources() -> None:
    """Stop the background worker and close the Neo4j and Qdrant connections, if opened."""
    if get_job_runner.cache_info().currsize:
        get_job_runner().shutdown()
    get_job_runner.cache_clear()
    if _database_clients.cache_info().currsize:
        neo4j, qdrant = _database_clients()
        neo4j.close()
        qdrant.close()
    _database_clients.cache_clear()
    get_graphrag_service.cache_clear()
    get_llm_generation_service.cache_clear()
    get_llm_provider.cache_clear()
