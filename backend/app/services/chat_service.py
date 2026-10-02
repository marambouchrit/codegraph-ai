"""The chat workflow: a question about a project -> a grounded, cited answer.

    ChatService (this module)   the order of the steps, nothing else
        ├── ProjectService          does the project exist?          (Phase 2)
        ├── GraphRAGService         retrieval: Qdrant + Neo4j          (Phase 9)
        └── LLMGenerationService    generation: prompt + LLM           (Phase 10)

No Cypher, no Qdrant call, no embedding, no prompt, no provider SDK here: each belongs
to the layer above. The two heavy services are given as factories (called only when
needed), so an unknown project is a 404 before anything is built, and an LLM
misconfiguration fails before the retrieval work is spent.

Nothing is re-indexed: the project must have been analyzed (graph) and indexed
(vectors) before; an unindexed project gets the "insufficient context" answer.
"""

import logging
from collections.abc import Callable
from typing import Protocol

from app.graphrag.models import GraphRAGContext
from app.llm.generator import LLMGenerationService
from app.llm.models import AssistantResponse
from app.services.project_service import ProjectService

logger = logging.getLogger(__name__)


class ContextRetriever(Protocol):
    """What the chat needs from GraphRAGService."""

    def build_context(self, project_id: str, query: str) -> GraphRAGContext: ...


class ChatService:
    def __init__(
        self,
        project_service: ProjectService,
        graphrag: Callable[[], ContextRetriever],
        generator: Callable[[], LLMGenerationService],
    ) -> None:
        self.project_service = project_service
        self._graphrag = graphrag
        self._generator = generator

    def ask(self, project_id: str, question: str) -> AssistantResponse:
        """Answer `question` about the project (ProjectNotFoundError if unknown)."""
        self.project_service.get_project(project_id)  # invalid or unknown ID: 404, first
        generator = self._generator()  # LLM misconfigured: fail before retrieval
        context = self._graphrag().build_context(project_id, question)  # one retrieval
        response = generator.generate(context)
        logger.info("Chat on project %s: %d sources, graph %s, model %s", project_id,
                    len(response.sources), response.graph_status, response.model)  # fmt: skip
        return response
