"""API model (response body) for the analysis endpoint.

Built from the Phase 6 GraphBuildReport and the Phase 8 VectorIndexReport; internal
details (build and index IDs, vector dimension, Neo4j queries) are not exposed.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.analysis_service import AnalysisResult


class GraphSummary(BaseModel):
    files: int = Field(description="Files analyzed")
    entities: int = Field(description="Nodes written to the knowledge graph")
    relationships: int = Field(description="Relationships written (CONTAINS, CALLS, IMPORTS...)")
    entities_by_type: dict[str, int]
    relationships_by_type: dict[str, int]
    unresolved_references: int = Field(
        description="Calls or imports whose target is not in the project (e.g. libraries)"
    )
    stale_entities_removed: int = Field(description="Left from a previous analysis, removed")


class VectorSummary(BaseModel):
    files: int = Field(description="Files chunked")
    chunks: int = Field(description="Code chunks embedded and stored for semantic search")
    chunks_by_type: dict[str, int]
    embedding_model: str
    stale_chunks_removed: int = Field(description="Left from a previous analysis, removed")


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "project_id": "3f2b9c0e1a4d4e5f8a7b6c5d4e3f2a1b",
                    "status": "ready",
                    "graph": {
                        "files": 4, "entities": 17, "relationships": 26,
                        "entities_by_type": {"Class": 4, "File": 4, "Function": 2, "Method": 7},
                        "relationships_by_type": {"CALLS": 8, "CONTAINS": 13, "DEPENDS_ON": 2,
                                                  "IMPORTS": 2, "INHERITS": 1},
                        "unresolved_references": 8, "stale_entities_removed": 0,
                    },
                    "vectors": {
                        "files": 4, "chunks": 15,
                        "chunks_by_type": {"class": 4, "file": 2, "function": 2, "method": 7},
                        "embedding_model": "BAAI/bge-m3", "stale_chunks_removed": 0,
                    },
                    "failed_files": 0,
                    "warnings": [],
                    "duration_seconds": 14.2,
                }
            ]
        }
    )  # fmt: skip

    project_id: str
    status: Literal["ready"] = Field(
        description="Always 'ready': a failed analysis returns an error, never a result"
    )
    graph: GraphSummary
    vectors: VectorSummary
    failed_files: int = Field(description="Files that could not be read or parsed (skipped)")
    warnings: list[str]
    duration_seconds: float

    @classmethod
    def from_result(cls, project_id: str, result: AnalysisResult) -> "AnalysisResponse":
        graph, vectors = result.graph, result.vectors
        failed_files = max(graph.failed_files, vectors.failed_files)  # same files, both steps
        warnings = []
        if failed_files:
            warnings.append(f"{failed_files} file(s) could not be parsed and were skipped.")
        if vectors.chunks == 0:
            warnings.append("No code could be indexed: chat will not find anything to answer from.")
        return cls(
            project_id=project_id,
            status="ready",
            graph=GraphSummary(
                files=graph.files,
                entities=graph.nodes_written,
                relationships=graph.relationships_written,
                entities_by_type=graph.nodes_by_label,
                relationships_by_type=graph.relationships_by_type,
                unresolved_references=graph.unresolved_references,
                stale_entities_removed=graph.stale_nodes_deleted,
            ),
            vectors=VectorSummary(
                files=vectors.files,
                chunks=vectors.chunks,
                chunks_by_type=vectors.chunks_by_type,
                embedding_model=vectors.embedding_model,
                stale_chunks_removed=vectors.stale_chunks_deleted,
            ),
            failed_files=failed_files,
            warnings=warnings,
            duration_seconds=result.duration_seconds,
        )
