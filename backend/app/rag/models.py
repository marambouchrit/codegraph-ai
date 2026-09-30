"""Data exchanged by the vector layer.

A `CodeChunk` is a piece of source code small enough to be embedded, with the
metadata that says where it comes from. Every chunk belongs to one Phase 4
entity (a method, a function, a class, or a whole file for module-level code), so
`entity_id` is also the ID of a node of the Neo4j graph: Phase 9 can go from a
vector search hit to the graph.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CodeChunk:
    # "<entity_id>|<part>": the same code gets the same ID at every indexing (re-indexing
    # overwrites instead of duplicating). part > 1 only when a long entity was split.
    id: str
    project_id: str
    file_path: str
    language: str  # "python", "java", "javascript", "typescript"
    entity_id: str
    entity_type: str  # "file", "class", "interface", "function", "method"
    name: str
    qualified_name: str
    start_line: int  # 1-based, inclusive: the lines of `text` in the file
    end_line: int
    text: str  # the source code (class chunks: method bodies replaced by "...")
    part: int = 1
    part_count: int = 1

    @property
    def embedding_text(self) -> str:
        """What is embedded: a short header, then the code.

        The header puts words the code itself may lack ("auth", "service", the kind of
        entity) next to it: a question about "the authentication service" then also
        matches a chunk of src/auth/service.py. It is derived from the metadata, so it
        is not stored.
        """
        label = self.entity_type if self.entity_type != "file" else "module code"
        part = f" (part {self.part}/{self.part_count})" if self.part_count > 1 else ""
        return (
            f"{self.language} {label} {self.qualified_name}{part}\n"
            f"file: {self.file_path}\n\n{self.text}"
        )

    def to_payload(self) -> dict[str, Any]:
        """The Qdrant payload of this chunk (the point ID is derived from `id`)."""
        return {
            "chunk_id": self.id,
            "project_id": self.project_id,
            "file_path": self.file_path,
            "language": self.language,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
            "part": self.part,
            "part_count": self.part_count,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CodeChunk":
        return cls(
            id=payload["chunk_id"],
            project_id=payload["project_id"],
            file_path=payload["file_path"],
            language=payload["language"],
            entity_id=payload["entity_id"],
            entity_type=payload["entity_type"],
            name=payload["name"],
            qualified_name=payload["qualified_name"],
            start_line=int(payload["start_line"]),
            end_line=int(payload["end_line"]),
            text=payload["text"],
            part=int(payload.get("part", 1)),
            part_count=int(payload.get("part_count", 1)),
        )


@dataclass(frozen=True)
class ChunkSearchResult:
    """One vector search hit: a chunk and its similarity to the query (higher is closer)."""

    chunk: CodeChunk
    score: float


@dataclass(frozen=True)
class VectorIndexReport:
    """What one indexing of a project did. Returned only when it succeeded (errors raise)."""

    project_id: str
    index_id: str
    embedding_model: str
    dimension: int
    files: int  # files chunked
    chunks: int  # chunks embedded and written (created or overwritten)
    chunks_by_type: dict[str, int]
    stale_chunks_deleted: int  # left from an older indexing (code removed since)
    failed_files: int  # files Phase 3/4 could not read or parse
    duration_seconds: float

    @property
    def summary(self) -> str:
        return (
            f"Vector index built successfully: {self.files:,} files, {self.chunks:,} chunks "
            f"({self.embedding_model}, {self.dimension} dimensions)"
        )


@dataclass
class ChunkingResult:
    """Chunks of a whole project, in file order."""

    chunks: list[CodeChunk] = field(default_factory=list)
    files: int = 0

    @property
    def chunks_by_type(self) -> dict[str, int]:
        return dict(sorted(Counter(chunk.entity_type for chunk in self.chunks).items()))
