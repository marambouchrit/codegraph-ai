"""Advanced analysis of a project's knowledge graph: impact, dependencies, architecture.

    InsightsService (this module)  the order of the steps, nothing else
        ├── ProjectService             does the project exist?                 (Phase 2)
        ├── GraphRetrievalService      bounded graph reads, by project         (Phase 7)
        ├── app.analysis.insights      cycles, hubs, unreferenced: pure functions
        ├── app.analysis.architecture  numbered facts computed from the graph
        └── ArchitectureSummarizer     an LLM summary of those facts only      (Phase 10 provider)

No Cypher and no LLM SDK here. Retrieval and the LLM provider are given as factories:
an unknown project is a 404 before any connection is created, and the dependency and
impact analyses never need an LLM key.

The architecture overview degrades honestly: the facts are always computed; if the
LLM is not configured or fails, they are returned without a summary, with a warning.
An unanalyzed project has no facts, and then no LLM call is made.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.analysis.architecture import Fact, architecture_facts
from app.analysis.insights import DependencyAnalysis, analyze_dependencies
from app.core.errors import LLMError
from app.graph.models import ImpactResult
from app.llm.architecture import ArchitectureSummarizer, ArchitectureSummary
from app.llm.provider import LLMProvider
from app.services.graph_retrieval_service import DEFAULT_IMPACT_DEPTH, GraphRetrievalService
from app.services.project_service import ProjectService

logger = logging.getLogger(__name__)

NO_GRAPH = "This project has no knowledge graph yet: analyze it first."


@dataclass(frozen=True)
class ArchitectureOverview:
    facts: tuple[Fact, ...]
    summary: ArchitectureSummary | None  # None: no facts, or the LLM was not available
    warnings: tuple[str, ...]


class InsightsService:
    def __init__(
        self,
        project_service: ProjectService,
        retrieval: Callable[[], GraphRetrievalService],
        llm: Callable[[], LLMProvider],
    ) -> None:
        self.project_service = project_service
        self._retrieval = retrieval
        self._llm = llm

    def impact(
        self, project_id: str, entity_id: str, max_depth: int = DEFAULT_IMPACT_DEPTH
    ) -> ImpactResult:
        """What may be affected if the entity changes (404 if the project or entity is unknown)."""
        self.project_service.get_project(project_id)  # invalid or unknown ID: 404, first
        return self._retrieval().get_impact(project_id, entity_id, max_depth=max_depth)

    def dependencies(self, project_id: str) -> DependencyAnalysis:
        """File dependencies, circular dependencies, hubs and unreferenced entities."""
        self.project_service.get_project(project_id)
        return analyze_dependencies(self._retrieval().get_analysis_graph(project_id))

    def architecture(self, project_id: str) -> ArchitectureOverview:
        """Facts computed from the graph, and an LLM summary of those facts."""
        self.project_service.get_project(project_id)
        graph = self._retrieval().get_analysis_graph(project_id)
        facts = tuple(architecture_facts(graph, analyze_dependencies(graph)))
        if not facts:
            return ArchitectureOverview((), None, (NO_GRAPH,))
        try:
            summary = ArchitectureSummarizer(self._llm()).summarize(facts)
        except LLMError as error:  # not configured, unavailable, empty answer: facts remain
            logger.warning("Architecture summary of project %s unavailable: %s",
                           project_id, error.message)  # fmt: skip
            return ArchitectureOverview(
                facts, None, (f"No summary could be generated: {error.message}",)
            )
        warnings = []
        if summary.unknown_citations:
            listed = ", ".join(f"[{number}]" for number in summary.unknown_citations)
            warnings.append(f"The summary cites {listed}, which match no fact.")
        if summary.truncated:
            warnings.append("The summary reached the output limit (LLM_MAX_TOKENS) and may be cut.")
        return ArchitectureOverview(facts, summary, tuple(warnings))
