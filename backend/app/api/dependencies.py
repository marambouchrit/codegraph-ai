"""Shared FastAPI dependencies.

Routes ask for services with `Depends(...)`. Tests can replace them through
`app.dependency_overrides`, for example to use a temporary workspace or fake services.

The chat's heavy pieces are created once per process, on first use, and reused:
the Neo4j driver and the Qdrant client (connection pools), the embedding model
(already one per process, see app/rag/embeddings.py) and the LLM client. Nothing is
connected when the API starts, so the other endpoints work without Neo4j, Qdrant or
an LLM key. A creation that fails (LLM key missing...) is not cached: fixing .env and
restarting is enough. `close_chat_resources()` closes the connections on shutdown.
"""

from functools import lru_cache

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.graph.client import Neo4jClient
from app.graph.repository import GraphRepository
from app.llm.generator import LLMGenerationService
from app.llm.provider import create_llm_provider
from app.rag.embeddings import embedding_provider_from_settings
from app.rag.vector_store import QdrantVectorStore
from app.services.chat_service import ChatService
from app.services.graph_retrieval_service import GraphRetrievalService
from app.services.graphrag_service import GraphRAGService
from app.services.project_service import ProjectService
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
def get_llm_generation_service() -> LLMGenerationService:
    return LLMGenerationService(create_llm_provider(get_settings()))


def get_chat_service(
    project_service: ProjectService = Depends(get_project_service),
) -> ChatService:
    # Factories, not instances: they are only built when a request needs them.
    return ChatService(project_service, get_graphrag_service, get_llm_generation_service)


def close_chat_resources() -> None:
    """Close the Neo4j and Qdrant connections, if they were ever opened."""
    if _database_clients.cache_info().currsize:
        neo4j, qdrant = _database_clients()
        neo4j.close()
        qdrant.close()
    _database_clients.cache_clear()
    get_graphrag_service.cache_clear()
    get_llm_generation_service.cache_clear()
