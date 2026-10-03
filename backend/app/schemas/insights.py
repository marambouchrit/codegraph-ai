"""API models (response bodies) for the advanced analysis endpoints.

Entities are returned as the same `GraphNode` as the graph endpoint, so the frontend
can highlight them in the graph it already displays.
"""

from collections import Counter

from pydantic import BaseModel, Field

from app.analysis.architecture import Fact
from app.analysis.insights import MAX_DEPENDENCIES, DependencyAnalysis, Hub
from app.graph.models import ImpactResult
from app.schemas.graph import GraphNode
from app.services.insights_service import ArchitectureOverview

UNREFERENCED_NOTE = (
    "No call, use or inheritance of these entities was detected in the project. They may "
    "still be used: called dynamically, by a framework (routes, tests, entry points), from "
    "outside the project, or through a call that could not be resolved. This is not proof "
    "of dead code."
)


# ----- Impact -----


class ImpactedEntityResponse(BaseModel):
    entity: GraphNode
    depth: int = Field(description="1: references the changed entity directly; 2: one step further...")
    relationship_type: str = Field(description="How it depends on `via`: CALLS, USES, IMPORTS...")
    via: str = Field(description="ID of the entity it references, one step closer to the change")


class ImpactResponse(BaseModel):
    project_id: str
    entity: GraphNode = Field(description="The entity whose change is analyzed")
    contained: int = Field(description="Entities it defines, which change with it")
    max_depth: int
    affected: list[ImpactedEntityResponse] = Field(description="Closest first, each entity once")
    total: int
    by_depth: dict[int, int] = Field(description="Number of affected entities per distance")
    truncated: bool = Field(description="true when more entities are affected than returned")

    @classmethod
    def from_result(cls, project_id: str, result: ImpactResult) -> "ImpactResponse":
        return cls(
            project_id=project_id,
            entity=GraphNode.from_entity(result.entity),
            contained=len(result.contained),
            max_depth=result.max_depth,
            affected=[
                ImpactedEntityResponse(
                    entity=GraphNode.from_entity(item.entity),
                    depth=item.depth,
                    relationship_type=item.relationship.type,
                    via=item.relationship.target_id,
                )
                for item in result.affected
            ],
            total=len(result.affected),
            by_depth=dict(sorted(Counter(item.depth for item in result.affected).items())),
            truncated=result.truncated,
        )


# ----- Dependencies -----


class FileDependencyResponse(BaseModel):
    source: str = Field(description="Path of the file that depends on `target`")
    target: str
    source_id: str
    target_id: str
    types: list[str] = Field(description="IMPORTS, DEPENDS_ON or both")


class CycleResponse(BaseModel):
    files: list[str] = Field(description="[A, B, C] means A -> B -> C -> A")
    entity_ids: list[str]


class HubResponse(BaseModel):
    entity: GraphNode
    incoming: int = Field(description="Distinct files or entities that depend on it")

    @classmethod
    def from_hub(cls, hub: Hub) -> "HubResponse":
        return cls(entity=GraphNode.from_entity(hub.entity), incoming=hub.incoming)


class DependencyResponse(BaseModel):
    project_id: str
    files: int
    dependency_count: int = Field(description="File-to-file dependencies (IMPORTS or DEPENDS_ON)")
    dependencies: list[FileDependencyResponse]
    dependencies_truncated: bool
    cycles: list[CycleResponse] = Field(description="Circular dependencies between files")
    cycles_truncated: bool
    file_hubs: list[HubResponse] = Field(description="Files most depended on")
    entity_hubs: list[HubResponse] = Field(description="Classes and functions most referenced")
    unreferenced_count: int
    unreferenced: list[GraphNode] = Field(description="Entities with no detected reference")
    unreferenced_note: str
    files_without_dependents: list[GraphNode] = Field(
        description="Files no other file depends on (entry points, scripts, or unused files)"
    )
    partial: bool = Field(description="true when the project is larger than what was analyzed")

    @classmethod
    def from_analysis(cls, project_id: str, analysis: DependencyAnalysis) -> "DependencyResponse":
        return cls(
            project_id=project_id,
            files=analysis.files,
            dependency_count=analysis.dependency_count,
            dependencies=[
                FileDependencyResponse(
                    source=dependency.source.file_path,
                    target=dependency.target.file_path,
                    source_id=dependency.source.id,
                    target_id=dependency.target.id,
                    types=list(dependency.types),
                )
                for dependency in analysis.dependencies[:MAX_DEPENDENCIES]
            ],
            dependencies_truncated=analysis.dependency_count > MAX_DEPENDENCIES,
            cycles=[
                CycleResponse(files=[file.file_path for file in cycle],
                              entity_ids=[file.id for file in cycle])
                for cycle in analysis.cycles
            ],  # fmt: skip
            cycles_truncated=analysis.cycles_truncated,
            file_hubs=[HubResponse.from_hub(hub) for hub in analysis.file_hubs],
            entity_hubs=[HubResponse.from_hub(hub) for hub in analysis.entity_hubs],
            unreferenced_count=analysis.unreferenced_count,
            unreferenced=[GraphNode.from_entity(entity) for entity in analysis.unreferenced],
            unreferenced_note=UNREFERENCED_NOTE,
            files_without_dependents=[
                GraphNode.from_entity(file) for file in analysis.files_without_dependents
            ],
            partial=analysis.partial,
        )


# ----- Architecture -----


class FactResponse(BaseModel):
    number: int = Field(description="The number the summary cites as [number]")
    text: str
    entities: list[GraphNode] = Field(description="The files or entities the fact is about")

    @classmethod
    def from_fact(cls, fact: Fact) -> "FactResponse":
        return cls(
            number=fact.number,
            text=fact.text,
            entities=[GraphNode.from_entity(entity) for entity in fact.entities],
        )


class ArchitectureResponse(BaseModel):
    project_id: str
    facts: list[FactResponse] = Field(description="Computed from the knowledge graph")
    summary: str | None = Field(
        description="Markdown written by the LLM from the facts only, citing them as [number]; "
        "null when there are no facts or the LLM was unavailable"
    )
    cited: list[int] = Field(description="Fact numbers cited by the summary")
    model: str | None
    warnings: list[str]

    @classmethod
    def from_overview(cls, project_id: str, overview: ArchitectureOverview) -> "ArchitectureResponse":
        summary = overview.summary
        return cls(
            project_id=project_id,
            facts=[FactResponse.from_fact(fact) for fact in overview.facts],
            summary=None if summary is None else summary.text,
            cited=[] if summary is None else list(summary.cited),
            model=None if summary is None else summary.model,
            warnings=list(overview.warnings),
        )
