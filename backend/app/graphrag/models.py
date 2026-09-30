"""The GraphRAG context: vector evidence + graph evidence + sources, all typed.

    GraphRAGContext
    ├── vector_results   Phase 8 hits (chunk + score), best first: *why* code was found
    ├── seeds            the distinct entities of those hits, expanded in the graph
    ├── entities         seeds, then their graph neighbors (bounded), each with its role
    ├── relationships    Phase 7 edges found by the expansions: *how* it is connected
    ├── paths            shortest connections between the top seeds
    ├── sources          file + line range of everything above, for citations
    ├── graph_status     complete / partial / unavailable (Neo4j failures)
    └── statistics

Every entity, relationship and source carries the entity ID, file path and lines of
the original code, so Phase 10 can cite "app/auth/service.py, lines 14-20".
Nothing here is text for an LLM: `describe()` is a readable outline for people.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from app.graph.models import EntityResult, GraphPath, RelationshipResult
from app.graphrag.expansion import Expansion
from app.rag.models import ChunkSearchResult


class GraphStatus(StrEnum):
    COMPLETE = "complete"  # every planned graph query ran
    PARTIAL = "partial"  # Neo4j failed after some results were collected
    UNAVAILABLE = "unavailable"  # Neo4j failed before any: vector evidence only


class EntityRole(StrEnum):
    SEED = "seed"  # found by vector search, then expanded
    NEIGHBOR = "neighbor"  # reached from a seed through one relationship


class SourceReason(StrEnum):
    VECTOR = "vector"  # a chunk found by vector search
    GRAPH = "graph"  # an entity reached through the graph


@dataclass(frozen=True)
class Seed:
    """A distinct entity of the vector hits: where graph expansion starts."""

    entity_id: str
    rank: int  # 1 = the best vector score
    score: float  # best score among its chunks
    chunk_ids: tuple[str, ...]  # its chunks among the vector hits (a long entity has parts)
    entity: EntityResult | None  # None: in the vector index, not (or no longer) in the graph
    expansions: tuple[Expansion, ...]  # the graph questions asked about it

    @property
    def in_graph(self) -> bool:
        return self.entity is not None


@dataclass(frozen=True)
class ContextEntity:
    entity: EntityResult
    role: EntityRole
    seed_ids: tuple[str, ...]  # the seed itself, or the seeds that reached this neighbor
    vector_score: float | None = None  # when the entity also has a vector hit


@dataclass(frozen=True)
class ContextRelationship:
    relationship: RelationshipResult
    seed_id: str  # the seed whose expansion found it (the first one, in seed order)
    expansion: Expansion


@dataclass(frozen=True)
class Source:
    """A piece of code to cite: where it is, and why it is in the context."""

    file_path: str
    start_line: int
    end_line: int
    entity_id: str
    qualified_name: str
    entity_type: str
    reason: SourceReason
    score: float | None = None  # vector similarity, for vector sources
    chunk_id: str | None = None  # the vector chunk, for vector sources

    @property
    def location(self) -> str:
        return f"{self.file_path}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class GraphRAGStatistics:
    vector_hits: int
    seeds: int
    seeds_expanded: int
    seeds_missing: int  # vector hits whose entity is not in the graph
    graph_queries: int
    entities: int
    relationships: int
    paths: int
    sources: int
    entities_dropped: int  # neighbors left out by the context limit
    duration_seconds: float = field(default=0.0, compare=False)  # not part of the result


@dataclass(frozen=True)
class GraphRAGContext:
    project_id: str
    query: str
    vector_results: tuple[ChunkSearchResult, ...]
    seeds: tuple[Seed, ...]
    entities: tuple[ContextEntity, ...]
    relationships: tuple[ContextRelationship, ...]
    paths: tuple[GraphPath, ...]
    sources: tuple[Source, ...]
    graph_status: GraphStatus
    warnings: tuple[str, ...]
    statistics: GraphRAGStatistics

    def entity(self, entity_id: str) -> EntityResult | None:
        for item in self.entities:
            if item.entity.id == entity_id:
                return item.entity
        return None

    def describe(self) -> list[str]:
        """A readable outline, for logs, debugging and documentation (not an LLM prompt)."""
        names = {item.entity.id: item.entity.qualified_name for item in self.entities}
        for path in self.paths:
            names.update((node.id, node.qualified_name) for node in path.nodes)
        lines = [f"Question: {self.query}", "Vector evidence:"]
        lines += [
            f"  {hit.score:.3f}  {hit.chunk.qualified_name}  "
            f"{hit.chunk.file_path}:{hit.chunk.start_line}-{hit.chunk.end_line}"
            for hit in self.vector_results
        ]
        lines.append(f"Graph evidence ({self.graph_status}):")
        for item in self.relationships:
            r = item.relationship
            source = names.get(r.source_id, r.source_id)
            target = names.get(r.target_id, r.target_id)
            lines.append(f"  {source} {r.type} {target}")
        for path in self.paths:
            chain = " -> ".join(node.qualified_name for node in path.nodes)
            lines.append(f"  path: {chain}")
        lines.append("Sources:")
        lines += [f"  {s.location}  {s.qualified_name} ({s.reason})" for s in self.sources]
        lines += [f"Warning: {warning}" for warning in self.warnings]
        return lines
