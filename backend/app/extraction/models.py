"""Data returned by entity extraction.

An entity is a named piece of code (a file, a class, a function...) with its
location. Entities form a tree through `parent_id`:

    File src/models/user.py
    ├── Class User
    │   └── Method login        (parent_id = id of User)
    └── Function create_user    (parent_id = id of the file)

Each entity is designed to become one node of the knowledge graph (Phase 6).
"""

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from app.ingestion.languages import Language
from app.parsing.service import ParseFailure


class EntityType(StrEnum):
    FILE = "file"
    CLASS = "class"
    INTERFACE = "interface"
    FUNCTION = "function"
    METHOD = "method"


@dataclass(frozen=True)
class Entity:
    id: str
    type: EntityType
    name: str  # as written in the source, e.g. "login"
    qualified_name: str  # names of the enclosing entities joined by ".", e.g. "User.login"
    file_path: str  # relative to the project root, with "/" separators
    language: Language
    # Human-friendly positions: lines and columns start at 1 (like SyntaxErrorInfo).
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    parent_id: str | None = None  # None only for FILE entities


@dataclass
class FileEntities:
    """All entities found in one source file. The first entity is always the FILE."""

    path: str
    language: Language
    has_syntax_errors: bool
    entities: list[Entity]

    @property
    def file(self) -> Entity:
        return self.entities[0]

    def of_type(self, entity_type: EntityType) -> list[Entity]:
        return [entity for entity in self.entities if entity.type == entity_type]

    def children_of(self, parent: Entity) -> list[Entity]:
        return [entity for entity in self.entities if entity.parent_id == parent.id]


@dataclass
class ExtractionReport:
    """Outcome of extracting a whole project. One bad file never stops the others."""

    project_id: str
    files: list[FileEntities] = field(default_factory=list)
    failures: list[ParseFailure] = field(default_factory=list)  # files that were skipped

    @property
    def entities(self) -> list[Entity]:
        return [entity for file in self.files for entity in file.entities]

    @property
    def entity_counts(self) -> dict[str, int]:
        counts = Counter(entity.type.value for entity in self.entities)
        return dict(sorted(counts.items()))
