"""What the last successful analysis knew about a project, kept to analyze it incrementally.

    <workspace>/<project_id>/analysis_index.json

For every source file: its SHA-256, and the result of analyzing it (entities, raw
references, the fingerprints of its chunks). For the whole project: a fingerprint of
every node and relationship written to Neo4j, and the settings the vectors depend on.

With it, the next analysis can:

    - tell which files changed (hash comparison) and parse only those;
    - reuse the entities and references of unchanged files to resolve cross-file
      relationships again (resolution always looks at the whole project);
    - write to Neo4j only the nodes and relationships that differ;
    - embed only the chunks whose content differs.

The index is only trusted when it matches the databases: it is deleted before an
analysis writes anything and saved again when everything succeeded. No index (first
analysis, failure, crash, unreadable file) simply means a full analysis.
"""

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from app.extraction.models import Entity, EntityType, FileEntities
from app.ingestion.languages import Language
from app.ingestion.workspace import Workspace
from app.relationships.models import RelationshipType
from app.relationships.references import FileReferences, Import, Reference, VariableType

logger = logging.getLogger(__name__)

INDEX_VERSION = 1


def fingerprint(value: Any) -> str:
    """A short, stable hash of JSON-like data (keys sorted, so order never matters)."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def chunk_content_hash(embedding_model: str, embedding_text: str) -> str:
    """Identity of an embedding: the same model and text always give the same vector."""
    return hashlib.sha256(f"{embedding_model}\n{embedding_text}".encode()).hexdigest()


@dataclass(frozen=True)
class ChunkRecord:
    content_hash: str  # of what is embedded: unchanged hash, vector reused
    payload_hash: str  # of the metadata stored with the vector (lines, names...)
    entity_type: str


@dataclass
class FileRecord:
    """One analyzed file. `entities` is None when the file could not be parsed."""

    sha256: str
    entities: FileEntities | None = None
    references: FileReferences | None = None  # None: no relationships collected
    failure: str | None = None  # why it could not be parsed
    chunks: dict[str, ChunkRecord] = field(default_factory=dict)  # by chunk ID


@dataclass(frozen=True)
class EdgeRecord:
    hash: str
    source_id: str  # needed to find the relationship again when deleting it


@dataclass
class AnalysisIndex:
    embedding_model: str
    collection: str
    chunk_max_chars: int
    chunk_overlap_lines: int
    files: dict[str, FileRecord] = field(default_factory=dict)  # by relative path
    nodes: dict[str, str] = field(default_factory=dict)  # node ID -> fingerprint
    edges: dict[str, EdgeRecord] = field(default_factory=dict)  # relationship ID -> record

    @property
    def file_hashes(self) -> dict[str, str]:
        return {path: record.sha256 for path, record in self.files.items()}

    @property
    def chunks(self) -> dict[str, ChunkRecord]:
        return {
            chunk_id: chunk
            for record in self.files.values()
            for chunk_id, chunk in record.chunks.items()
        }


class AnalysisIndexStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def load(self, project_id: str) -> AnalysisIndex | None:
        """The saved index, or None if there is none or it cannot be read (full analysis)."""
        path = self.workspace.analysis_index_file(project_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["version"] != INDEX_VERSION or data["project_id"] != project_id:
                return None
            return _index_from_dict(data)
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Ignoring the analysis index of project %s: unreadable", project_id)
            return None

    def save(self, project_id: str, index: AnalysisIndex) -> None:
        data = {"version": INDEX_VERSION, "project_id": project_id, **asdict(index)}
        path = self.workspace.analysis_index_file(project_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)  # never a half-written index

    def clear(self, project_id: str) -> None:
        self.workspace.analysis_index_file(project_id).unlink(missing_ok=True)


# ----- JSON -> dataclasses (asdict() does the other direction) -----


def _index_from_dict(data: dict[str, Any]) -> AnalysisIndex:
    return AnalysisIndex(
        embedding_model=data["embedding_model"],
        collection=data["collection"],
        chunk_max_chars=int(data["chunk_max_chars"]),
        chunk_overlap_lines=int(data["chunk_overlap_lines"]),
        files={path: _file_record(record) for path, record in data["files"].items()},
        nodes=dict(data["nodes"]),
        edges={key: EdgeRecord(**edge) for key, edge in data["edges"].items()},
    )


def _file_record(data: dict[str, Any]) -> FileRecord:
    entities, references = data["entities"], data["references"]
    return FileRecord(
        sha256=data["sha256"],
        entities=None if entities is None else _file_entities(entities),
        references=None if references is None else _file_references(references),
        failure=data["failure"],
        chunks={key: ChunkRecord(**chunk) for key, chunk in data["chunks"].items()},
    )


def _entity(data: dict[str, Any]) -> Entity:
    return Entity(**{**data, "type": EntityType(data["type"]), "language": Language(data["language"])})


def _file_entities(data: dict[str, Any]) -> FileEntities:
    return FileEntities(
        path=data["path"],
        language=Language(data["language"]),
        has_syntax_errors=bool(data["has_syntax_errors"]),
        entities=[_entity(entity) for entity in data["entities"]],
    )


def _file_references(data: dict[str, Any]) -> FileReferences:
    return FileReferences(
        file=_entity(data["file"]),
        language=Language(data["language"]),
        package=data["package"],
        default_export=data["default_export"],
        imports=[Import(**item) for item in data["imports"]],
        references=[
            Reference(**{**item, "type": RelationshipType(item["type"]),
                         "parts": tuple(item["parts"])})
            for item in data["references"]
        ],  # fmt: skip
        variables=[
            VariableType(**{**item, "type_parts": tuple(item["type_parts"])})
            for item in data["variables"]
        ],
    )
