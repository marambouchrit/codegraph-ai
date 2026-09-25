"""Extract the relationships between the entities of a project.

`RelationshipExtractionService` is the entry point of the relationship layer:

    ScannedFile -> ParserService (Phase 3) -> ParseResult -> EntityExtractionService (Phase 4)
                                                  │                 -> FileEntities
                                                  ▼
                        step 1, per file:  language collector -> FileReferences (raw facts)
                                           (the syntax tree is then released)

                        step 2, project:   ReferenceResolver -> Relationships + unresolved

Each file is parsed exactly once. Resolution runs in two stages because finding
`self.save()` may need the parent classes: imports and INHERITS/IMPLEMENTS first,
then CALLS and USES. DEPENDS_ON is derived last from the resolved relationships.

Like the other layers, it knows nothing about FastAPI.
"""

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

from app.core.errors import UnsupportedLanguageError
from app.extraction.models import Entity, ExtractionReport, FileEntities
from app.extraction.service import EntityExtractionService
from app.ingestion.languages import Language
from app.ingestion.scanner import ScannedFile
from app.parsing.base import ParseResult
from app.relationships.base import ReferenceCollector
from app.relationships.java_references import JavaReferenceCollector
from app.relationships.javascript_references import JavaScriptReferenceCollector
from app.relationships.models import (
    Relationship,
    RelationshipReport,
    RelationshipType,
    UnresolvedReason,
    UnresolvedReference,
    relationship_id,
)
from app.relationships.python_references import PythonReferenceCollector
from app.relationships.references import FileReferences, Import, Reference
from app.relationships.resolver import ReferenceResolver
from app.relationships.typescript_references import TypeScriptReferenceCollector

logger = logging.getLogger(__name__)

HIERARCHY = frozenset({RelationshipType.INHERITS, RelationshipType.IMPLEMENTS})
# Relationships showing that code of one file really needs another file.
DEPENDENCY_EVIDENCE = frozenset(
    {
        RelationshipType.CALLS,
        RelationshipType.USES,
        RelationshipType.INHERITS,
        RelationshipType.IMPLEMENTS,
    }
)


def default_collectors() -> list[ReferenceCollector]:
    return [
        PythonReferenceCollector(),
        JavaReferenceCollector(),
        JavaScriptReferenceCollector(),
        TypeScriptReferenceCollector(),
    ]


class RelationshipExtractionService:
    def __init__(
        self,
        extraction_service: EntityExtractionService,
        collectors: Iterable[ReferenceCollector] | None = None,
    ) -> None:
        self.extraction_service = extraction_service
        chosen = default_collectors() if collectors is None else collectors
        self._collectors = {collector.language: collector for collector in chosen}

    def get_collector(self, language: Language | str) -> ReferenceCollector:
        """Return the collector registered for `language` (a Language or a name like "java")."""
        try:
            collector = self._collectors.get(Language(language))
        except ValueError:
            collector = None
        if collector is None:
            raise UnsupportedLanguageError(f"No relationship collector for language '{language}'.")
        return collector

    def collect(
        self, parse_result: ParseResult, file_entities: FileEntities, project_id: str
    ) -> FileReferences:
        """Step 1 for one already-parsed file: record its imports, calls, base classes..."""
        extractor = self.extraction_service.get_extractor(parse_result.language)
        collector = self.get_collector(parse_result.language)
        return collector.collect(file_entities.file, extractor.walk(parse_result, project_id))

    def extract_project(
        self, project_id: str, root: Path, files: Iterable[ScannedFile]
    ) -> RelationshipReport:
        """Parse every file once, extract its entities and references, then resolve them."""
        references: list[FileReferences] = []

        def collect_references(parse_result: ParseResult, file_entities: FileEntities) -> None:
            try:
                references.append(self.collect(parse_result, file_entities, project_id))
            except Exception:
                # The file keeps its entities; it just contributes no relationships.
                logger.exception(
                    "Unexpected error while collecting references of %s", file_entities.path
                )

        extraction = self.extraction_service.extract_project(
            project_id, root, files, on_file=collect_references
        )
        return self.resolve(extraction, references)

    def resolve(
        self, extraction: ExtractionReport, references: Sequence[FileReferences]
    ) -> RelationshipReport:
        """Step 2: turn the references of every file into relationships."""
        builder = _ReportBuilder(ReferenceResolver(extraction.files, references))
        # Stage 1: imports and the class hierarchy (needed to find inherited methods).
        for refs in references:
            for imp in refs.imports:
                builder.add_import(refs, imp)
            for reference in refs.references:
                if reference.type in HIERARCHY:
                    builder.add_reference(refs, reference)
        # Stage 2: calls and type uses.
        for refs in references:
            for reference in refs.references:
                if reference.type not in HIERARCHY:
                    builder.add_reference(refs, reference)
        builder.add_dependencies()
        return RelationshipReport(
            project_id=extraction.project_id,
            extraction=extraction,
            relationships=builder.relationships,
            unresolved=builder.unresolved,
        )


class _ReportBuilder:
    """Collects relationships without duplicates: one per (source, type, target)."""

    def __init__(self, resolver: ReferenceResolver) -> None:
        self.resolver = resolver
        self._relationships: dict[str, Relationship] = {}
        self._unresolved: dict[tuple[str, RelationshipType, str], UnresolvedReference] = {}

    @property
    def relationships(self) -> list[Relationship]:
        return list(self._relationships.values())

    @property
    def unresolved(self) -> list[UnresolvedReference]:
        return list(self._unresolved.values())

    def add_import(self, refs: FileReferences, imp: Import) -> None:
        target = self.resolver.resolve_import(refs, imp)
        if target is None:
            return
        position = (refs.path, imp.line, imp.column)
        if isinstance(target, Entity):
            if target.id != refs.file.id:
                self._add(RelationshipType.IMPORTS, refs.file, target, *position)
        else:
            self._add_unresolved(
                RelationshipType.IMPORTS, refs.file.id, imp.module, target, *position
            )

    def add_reference(self, refs: FileReferences, reference: Reference) -> None:
        source = self.resolver.entity(reference.source_id)
        target = self.resolver.resolve(refs, reference)
        if isinstance(target, Entity) and reference.type in HIERARCHY and target.id == source.id:
            target = UnresolvedReason.NOT_FOUND  # `class User(User)`: the base is another User
        if not isinstance(target, Entity):
            self._add_unresolved(
                reference.type, source.id, reference.text, target, refs.path,
                reference.line, reference.column,
            )  # fmt: skip
            return
        self._add(reference.type, source, target, refs.path, reference.line, reference.column)
        if reference.type in HIERARCHY:
            self.resolver.add_base(source, target)

    def add_dependencies(self) -> None:
        """File A DEPENDS_ON file B when code of A calls, uses or extends code of B."""
        for relationship in self.relationships:
            if relationship.type not in DEPENDENCY_EVIDENCE:
                continue
            source = self.resolver.file_of(self.resolver.entity(relationship.source_id))
            target = self.resolver.file_of(self.resolver.entity(relationship.target_id))
            if source.id != target.id:
                self._add(
                    RelationshipType.DEPENDS_ON, source, target,
                    relationship.file_path, relationship.line, relationship.column,
                )  # fmt: skip

    def _add(
        self,
        type_: RelationshipType,
        source: Entity,
        target: Entity,
        path: str,
        line: int,
        column: int,
    ) -> None:
        key = relationship_id(type_, source.id, target.id)
        # The first occurrence is kept: files and nodes are visited in a fixed order.
        self._relationships.setdefault(
            key, Relationship(key, type_, source.id, target.id, path, line, column)
        )

    def _add_unresolved(
        self,
        type_: RelationshipType,
        source_id: str,
        target_name: str,
        reason: UnresolvedReason,
        path: str,
        line: int,
        column: int,
    ) -> None:
        self._unresolved.setdefault(
            (source_id, type_, target_name),
            UnresolvedReference(type_, source_id, target_name, reason, path, line, column),
        )
