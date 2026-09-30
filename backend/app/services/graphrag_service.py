"""GraphRAG retrieval: semantic starting points, expanded through the knowledge graph.

                         question
                             │
    VectorRetrievalService (Phase 8)     "what code is about this?"   → Qdrant
                             │  hits: chunks + scores, each with an entity_id
                             ▼
    seeds: the distinct entity IDs of the hits, best score first (bounded)
                             │
    GraphRetrievalService (Phase 7)      "how is it connected?"      → Neo4j
        per seed, a few one-hop questions chosen by its type (graphrag/expansion.py),
        then the shortest paths between the top seeds
                             ▼
    GraphRAGContext: vector evidence + graph evidence + sources (deduplicated, bounded)

This service only orchestrates: no Cypher, no Qdrant call, no embedding, no LLM.
The Phase 4 entity ID is the bridge: a chunk's `entity_id` is the ID of a Neo4j node.

Ordering is deterministic and transparent (no combined score): vector hits keep the
Phase 8 order (score, then chunk ID); seeds follow their best hit; neighbors follow
their seed, then the expansion order of the strategy, then Phase 7's order.

Failures:
- invalid project ID or question, Qdrant or the embedding model failing: the error
  is raised (without vector hits there is nothing to build on);
- a seed missing from the graph (indexes built at different times): kept as vector
  evidence, not expanded, with a warning;
- a seed without neighbors: simply no graph evidence for it;
- Neo4j failing (GraphDatabaseError): graph expansion stops and the context keeps
  the vector evidence and any graph evidence already collected, with graph_status
  "unavailable" or "partial" and a warning, unless GRAPHRAG_REQUIRE_GRAPH is set,
  in which case the error is raised.

Usage:

    graphrag = GraphRAGService(vector_retrieval, graph_retrieval, settings)
    context = graphrag.build_context(project_id, "How is authentication implemented?")
    print("\\n".join(context.describe()))
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.errors import EntityNotFoundError, GraphDatabaseError
from app.extraction.models import EntityType
from app.graph.models import EntityResult, GraphPath, RelatedEntity
from app.graph.repository import MAX_TRAVERSAL_DEPTH
from app.graphrag.expansion import Expansion, expansions_for
from app.graphrag.models import (
    ContextEntity,
    ContextRelationship,
    EntityRole,
    GraphRAGContext,
    GraphRAGStatistics,
    GraphStatus,
    Seed,
    Source,
    SourceReason,
)
from app.ingestion.languages import Language
from app.rag.models import ChunkSearchResult
from app.services.graph_retrieval_service import MAX_LIMIT, GraphRetrievalService
from app.services.vector_retrieval_service import VectorRetrievalService

logger = logging.getLogger(__name__)

MAX_SEEDS = 20
MAX_CONTEXT_ENTITIES = 200
MAX_PATH_SEEDS = 5


@dataclass(frozen=True)
class _SeedPlan:
    entity_id: str
    rank: int
    score: float
    chunk_ids: tuple[str, ...]


@dataclass
class _Collected:
    """Graph evidence gathered so far, in discovery order (dicts keep insertion order)."""

    neighbors: dict[str, tuple[EntityResult, list[str]]] = field(default_factory=dict)
    relationships: dict[str, ContextRelationship] = field(default_factory=dict)
    paths: list[GraphPath] = field(default_factory=list)
    queries: int = 0
    failure: GraphDatabaseError | None = None
    warnings: list[str] = field(default_factory=list)


class GraphRAGService:
    def __init__(
        self,
        vector_retrieval: VectorRetrievalService,
        graph_retrieval: GraphRetrievalService,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or Settings()
        _check_range("GRAPHRAG_MAX_SEEDS", settings.graphrag_max_seeds, 1, MAX_SEEDS)
        _check_range("GRAPHRAG_NEIGHBORS_PER_EXPANSION", settings.graphrag_neighbors_per_expansion,
                     1, MAX_LIMIT)  # fmt: skip
        _check_range("GRAPHRAG_MAX_CONTEXT_ENTITIES", settings.graphrag_max_context_entities,
                     settings.graphrag_max_seeds, MAX_CONTEXT_ENTITIES)  # fmt: skip
        _check_range("GRAPHRAG_PATH_SEEDS", settings.graphrag_path_seeds, 0, MAX_PATH_SEEDS)
        _check_range("GRAPHRAG_PATH_MAX_DEPTH", settings.graphrag_path_max_depth, 1,
                     MAX_TRAVERSAL_DEPTH)  # fmt: skip
        self.vector_retrieval = vector_retrieval
        self.graph_retrieval = graph_retrieval
        self.vector_top_k = settings.graphrag_vector_top_k  # checked by the vector service
        self.max_seeds = settings.graphrag_max_seeds
        self.neighbors_per_expansion = settings.graphrag_neighbors_per_expansion
        self.max_context_entities = settings.graphrag_max_context_entities
        self.path_seeds = settings.graphrag_path_seeds
        self.path_max_depth = settings.graphrag_path_max_depth
        self.require_graph = settings.graphrag_require_graph

    def build_context(
        self,
        project_id: str,
        query: str,
        *,
        languages: Sequence[Language | str] | None = None,
        entity_types: Sequence[EntityType | str] | None = None,
    ) -> GraphRAGContext:
        """Vector hits for `query`, expanded in the project's graph. Never calls an LLM.

        `languages` / `entity_types` filter the vector hits (the seeds), not the graph.
        """
        started = time.perf_counter()
        # Validates the project ID, the question and the filters; Qdrant errors propagate.
        hits = self.vector_retrieval.retrieve(
            project_id, query, top_k=self.vector_top_k,
            languages=languages, entity_types=entity_types,
        )  # fmt: skip
        collected = _Collected()
        hits = self._own_project(project_id, hits, collected)
        plans = _seed_plans(hits, self.max_seeds)
        seed_ids = {plan.entity_id for plan in plans}

        seeds = [self._expand_seed(project_id, plan, seed_ids, collected) for plan in plans]
        self._connect_seeds(project_id, [s for s in seeds if s.in_graph], collected)

        context = self._assemble(project_id, query.strip(), hits, seeds, collected, started)
        logger.info(
            "GraphRAG for project %s: %d hits, %d seeds, %d entities, %d relationships (%s)",
            project_id, len(hits), len(seeds), len(context.entities),
            len(context.relationships), context.graph_status,
        )  # fmt: skip
        return context

    # ----- Graph expansion -----

    def _expand_seed(
        self, project_id: str, plan: _SeedPlan, seed_ids: set[str], collected: _Collected
    ) -> Seed:
        def seed(entity: EntityResult | None = None, done: tuple[Expansion, ...] = ()) -> Seed:
            return Seed(plan.entity_id, plan.rank, plan.score, plan.chunk_ids, entity, done)

        if collected.failure is not None:
            return seed()
        try:
            collected.queries += 1
            entity = self.graph_retrieval.get_entity(project_id, plan.entity_id)
        except EntityNotFoundError:
            collected.warnings.append(
                f"{plan.entity_id} is in the vector index but not in the knowledge graph: "
                "rebuild the graph and the index of this project together."
            )
            return seed()
        except GraphDatabaseError as error:
            self._graph_failed(error, collected)
            return seed()
        if entity.project_id != project_id:  # defensive: never mix projects
            return seed()

        done: list[Expansion] = []
        for expansion in expansions_for(entity.entity_type):
            try:
                collected.queries += 1
                related = self._ask(expansion, project_id, entity.id)
            except EntityNotFoundError:
                continue  # deleted between two queries (a rebuild in progress)
            except GraphDatabaseError as error:
                self._graph_failed(error, collected)
                break
            done.append(expansion)
            self._collect(project_id, entity.id, expansion, related, seed_ids, collected)
        return seed(entity, tuple(done))

    def _ask(self, expansion: Expansion, project_id: str, entity_id: str) -> list[RelatedEntity]:
        """One Phase 7 question, with the per-expansion limit."""
        graph, limit = self.graph_retrieval, self.neighbors_per_expansion
        questions: dict[Expansion, Callable[[], list[RelatedEntity]]] = {
            Expansion.CONTAINER: lambda: graph.get_container(project_id, entity_id),
            Expansion.MEMBERS: lambda: graph.get_contained_entities(project_id, entity_id,
                                                                    limit=limit),
            Expansion.CALLEES: lambda: graph.get_callees(project_id, entity_id, limit=limit),
            Expansion.CALLERS: lambda: graph.get_callers(project_id, entity_id, limit=limit),
            Expansion.PARENTS: lambda: graph.get_parents(project_id, entity_id, limit=limit),
            Expansion.SUBCLASSES: lambda: graph.get_subclasses(project_id, entity_id, limit=limit),
            Expansion.INTERFACES: lambda: graph.get_implemented_interfaces(
                project_id, entity_id, limit=limit
            ),
            Expansion.IMPLEMENTATIONS: lambda: graph.get_implementations(project_id, entity_id,
                                                                         limit=limit),
            Expansion.DEPENDENCIES: lambda: graph.get_dependencies(project_id, entity_id,
                                                                   limit=limit),
            Expansion.DEPENDENTS: lambda: graph.get_dependents(project_id, entity_id, limit=limit),
        }  # fmt: skip
        return questions[expansion]()

    def _collect(
        self,
        project_id: str,
        seed_id: str,
        expansion: Expansion,
        related: list[RelatedEntity],
        seed_ids: set[str],
        collected: _Collected,
    ) -> None:
        for item in related:
            neighbor = item.entity
            if neighbor.project_id != project_id or item.relationship is None:
                continue  # defensive: Phase 7 already scopes every query by project
            relationship = item.relationship
            if relationship.id not in collected.relationships:
                collected.relationships[relationship.id] = ContextRelationship(
                    relationship, seed_id, expansion
                )
            if neighbor.id in seed_ids:
                continue  # another seed: it is in the context as a seed
            entry = collected.neighbors.setdefault(neighbor.id, (neighbor, []))
            if seed_id not in entry[1]:
                entry[1].append(seed_id)

    def _connect_seeds(self, project_id: str, seeds: list[Seed], collected: _Collected) -> None:
        """The shortest path between each pair of the first seeds, in either direction."""
        top = seeds[: self.path_seeds]
        for index, first in enumerate(top):
            for second in top[index + 1 :]:
                if collected.failure is not None:
                    return
                for source, target in ((first, second), (second, first)):
                    try:
                        collected.queries += 1
                        paths = self.graph_retrieval.find_paths(
                            project_id, source.entity_id, target.entity_id,
                            max_depth=self.path_max_depth, limit=1,
                        )  # fmt: skip
                    except EntityNotFoundError:
                        break
                    except GraphDatabaseError as error:
                        self._graph_failed(error, collected)
                        return
                    if paths and all(n.project_id == project_id for n in paths[0].nodes):
                        if _adds_evidence(paths[0], collected):
                            collected.paths.append(paths[0])
                        break  # one connection per pair is enough

    def _graph_failed(self, error: GraphDatabaseError, collected: _Collected) -> None:
        if self.require_graph:
            raise error
        logger.warning("GraphRAG: knowledge graph query failed (%s)", type(error).__name__)
        collected.failure = error
        collected.warnings.append(
            f"The knowledge graph could not be queried ({error.message}): "
            "the context holds the graph evidence collected before the failure, if any."
        )

    # ----- Context -----

    def _own_project(
        self, project_id: str, hits: list[ChunkSearchResult], collected: _Collected
    ) -> list[ChunkSearchResult]:
        """Keep hits of this project only (Phase 8 filters already; this never trusts it)."""
        prefix = f"{project_id}:"
        kept = [
            hit for hit in hits
            if hit.chunk.project_id == project_id and hit.chunk.entity_id.startswith(prefix)
        ]  # fmt: skip
        if len(kept) != len(hits):
            collected.warnings.append(f"{len(hits) - len(kept)} vector hits of another project "
                                      "were ignored.")  # fmt: skip
        return kept

    def _assemble(
        self,
        project_id: str,
        query: str,
        hits: list[ChunkSearchResult],
        seeds: list[Seed],
        collected: _Collected,
        started: float,
    ) -> GraphRAGContext:
        best_scores: dict[str, float] = {}
        for hit in hits:  # best first: the first score seen is the best one
            best_scores.setdefault(hit.chunk.entity_id, hit.score)

        entities = [
            ContextEntity(seed.entity, EntityRole.SEED, (seed.entity_id,), seed.score)
            for seed in seeds
            if seed.entity is not None
        ]
        neighbors = [
            ContextEntity(entity, EntityRole.NEIGHBOR, tuple(seed_list),
                          best_scores.get(entity.id))  # fmt: skip
            for entity, seed_list in collected.neighbors.values()
        ]
        room = self.max_context_entities - len(entities)
        dropped = max(0, len(neighbors) - room)
        entities += neighbors[:room]

        kept_ids = {item.entity.id for item in entities}
        relationships = [
            item for item in collected.relationships.values()
            if item.relationship.source_id in kept_ids and item.relationship.target_id in kept_ids
        ]  # fmt: skip
        sources = _sources(hits, entities)

        if collected.failure is None:
            status = GraphStatus.COMPLETE
        elif any(seed.in_graph for seed in seeds):
            status = GraphStatus.PARTIAL
        else:
            status = GraphStatus.UNAVAILABLE
        statistics = GraphRAGStatistics(
            vector_hits=len(hits),
            seeds=len(seeds),
            seeds_expanded=sum(1 for seed in seeds if seed.in_graph),
            seeds_missing=sum(1 for seed in seeds if not seed.in_graph),
            graph_queries=collected.queries,
            entities=len(entities),
            relationships=len(relationships),
            paths=len(collected.paths),
            sources=len(sources),
            entities_dropped=dropped,
            duration_seconds=round(time.perf_counter() - started, 3),
        )
        return GraphRAGContext(
            project_id=project_id,
            query=query,
            vector_results=tuple(hits),
            seeds=tuple(seeds),
            entities=tuple(entities),
            relationships=tuple(relationships),
            paths=tuple(collected.paths),
            sources=tuple(sources),
            graph_status=status,
            warnings=tuple(collected.warnings),
            statistics=statistics,
        )


def _adds_evidence(path: GraphPath, collected: _Collected) -> bool:
    """A chain of several relationships says how two seeds connect, even when each link was
    found separately; a single relationship already in the context says nothing new
    (`AuthService CONTAINS AuthService.login` is already there as login's container)."""
    return path.length > 1 or any(r.id not in collected.relationships for r in path.relationships)


def _seed_plans(hits: list[ChunkSearchResult], max_seeds: int) -> list[_SeedPlan]:
    """One seed per distinct entity, in order of its best hit (hits are best first)."""
    chunks: dict[str, list[str]] = {}
    scores: dict[str, float] = {}
    for hit in hits:
        entity_id = hit.chunk.entity_id
        chunks.setdefault(entity_id, []).append(hit.chunk.id)
        scores.setdefault(entity_id, hit.score)
    return [
        _SeedPlan(entity_id, rank, scores[entity_id], tuple(chunk_ids))
        for rank, (entity_id, chunk_ids) in enumerate(list(chunks.items())[:max_seeds], start=1)
    ]


def _sources(hits: list[ChunkSearchResult], entities: list[ContextEntity]) -> list[Source]:
    """Every vector chunk, then every graph entity not already cited through a chunk."""
    sources: list[Source] = []
    seen: set[tuple[str, int, int, str]] = set()
    cited_entities: set[str] = set()
    for hit in hits:
        chunk = hit.chunk
        key = (chunk.file_path, chunk.start_line, chunk.end_line, chunk.entity_id)
        cited_entities.add(chunk.entity_id)
        if key not in seen:
            seen.add(key)
            sources.append(Source(chunk.file_path, chunk.start_line, chunk.end_line,
                                  chunk.entity_id, chunk.qualified_name, chunk.entity_type,
                                  SourceReason.VECTOR, hit.score, chunk.id))  # fmt: skip
    for item in entities:
        entity = item.entity
        key = (entity.file_path, entity.start_line, entity.end_line, entity.id)
        if entity.id in cited_entities or key in seen:
            continue
        seen.add(key)
        sources.append(Source(entity.file_path, entity.start_line, entity.end_line, entity.id,
                              entity.qualified_name, entity.entity_type,
                              SourceReason.GRAPH))  # fmt: skip
    return sources


def _check_range(name: str, value: int, low: int, high: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
