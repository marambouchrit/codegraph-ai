"""Data returned by relationship extraction.

A relationship connects two entities found in Phase 4:

    File src/services/auth.py ──IMPORTS──> File src/models/user.py
    Function login            ──CALLS────> Class User
    Function login            ──CALLS────> Method User.save
    Class Admin               ──INHERITS─> Class User

Each relationship is designed to become one edge of the knowledge graph (Phase 6).
Its `target_id` is always the ID of a real entity of the project: references that
cannot be resolved confidently (a library call, an ambiguous name...) are never
turned into relationships. They are listed separately as `UnresolvedReference`s.
"""

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from app.extraction.models import Entity, ExtractionReport
from app.parsing.service import ParseFailure


class RelationshipType(StrEnum):
    # Upper case, like the relationship types of Neo4j.
    IMPORTS = "IMPORTS"  # file -> file
    INHERITS = "INHERITS"  # class -> class, interface -> interface
    IMPLEMENTS = "IMPLEMENTS"  # class -> interface
    CALLS = "CALLS"  # function/method/file -> function/method, or -> class (object creation)
    USES = "USES"  # entity -> class/interface used as a type, or a JSX component
    DEPENDS_ON = "DEPENDS_ON"  # file -> file, derived from the relationships above


class UnresolvedReason(StrEnum):
    EXTERNAL = "external"  # comes from a module outside the project (a library)
    NOT_FOUND = "not_found"  # no matching definition (built-ins, globals, dynamic code...)
    AMBIGUOUS = "ambiguous"  # several entities match and nothing tells them apart
    UNKNOWN_RECEIVER = "unknown_receiver"  # obj.method() where the type of obj is unknown


@dataclass(frozen=True)
class Relationship:
    id: str  # "<TYPE>:<source_id>-><target_id>": one relationship per (source, type, target)
    type: RelationshipType
    source_id: str
    target_id: str
    # Where the relationship was first seen (1-based, like entities). The same call made
    # twice in a function is still one relationship: the graph only needs to know it exists.
    file_path: str
    line: int
    column: int


def relationship_id(type_: RelationshipType, source_id: str, target_id: str) -> str:
    return f"{type_}:{source_id}->{target_id}"


@dataclass(frozen=True)
class UnresolvedReference:
    """A reference whose target could not be found confidently in the project."""

    type: RelationshipType
    source_id: str
    target_name: str  # as written in the source, e.g. "requests.get" or "user.save"
    reason: UnresolvedReason
    file_path: str
    line: int
    column: int


@dataclass
class RelationshipReport:
    """Everything Phase 6 needs: the entities (nodes) and the relationships (edges)."""

    project_id: str
    extraction: ExtractionReport
    relationships: list[Relationship] = field(default_factory=list)
    unresolved: list[UnresolvedReference] = field(default_factory=list)

    @property
    def entities(self) -> list[Entity]:
        return self.extraction.entities

    @property
    def failures(self) -> list[ParseFailure]:
        return self.extraction.failures

    def of_type(self, type_: RelationshipType) -> list[Relationship]:
        return [relationship for relationship in self.relationships if relationship.type == type_]

    @property
    def relationship_counts(self) -> dict[str, int]:
        counts = Counter(relationship.type.value for relationship in self.relationships)
        return dict(sorted(counts.items()))

    @property
    def unresolved_counts(self) -> dict[str, int]:
        counts = Counter(reference.reason.value for reference in self.unresolved)
        return dict(sorted(counts.items()))
