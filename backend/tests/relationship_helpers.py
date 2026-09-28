"""Helpers shared by the relationship tests: analyze in-memory projects, read results.

Entity IDs are shortened by removing the project prefix, so a relationship reads
("src/auth.py:login", "CALLS", "src/models/user.py:User.save").
"""

from app.extraction.models import ExtractionReport
from app.extraction.service import EntityExtractionService
from app.parsing.service import ParserService
from app.relationships.models import RelationshipReport, RelationshipType
from app.relationships.service import RelationshipExtractionService

PROJECT_ID = "p"

parser_service = ParserService(max_file_bytes=1024 * 1024)
extraction_service = EntityExtractionService(parser_service)
relationship_service = RelationshipExtractionService(extraction_service)

Edge = tuple[str, str, str]


def analyze(files: dict[str, str], project_id: str = PROJECT_ID) -> RelationshipReport:
    """Parse, extract and resolve a project given as {path: source}, without any disk access."""
    extraction = ExtractionReport(project_id=project_id)
    references = []
    for path, source in files.items():
        parse_result = parser_service.parse_source(source.encode(), path)
        file_entities = extraction_service.extract(parse_result, project_id)
        extraction.files.append(file_entities)
        references.append(relationship_service.collect(parse_result, file_entities, project_id))
    return relationship_service.resolve(extraction, references)


def short(entity_id: str) -> str:
    return entity_id.removeprefix(f"{PROJECT_ID}:")


def edges(report: RelationshipReport, type_: RelationshipType | None = None) -> set[Edge]:
    return {
        (short(r.source_id), r.type.value, short(r.target_id))
        for r in report.relationships
        if type_ is None or r.type == type_
    }


def unresolved(report: RelationshipReport) -> set[tuple[str, str, str, str]]:
    """(source, type, name as written, reason) of every unresolved reference."""
    return {
        (short(u.source_id), u.type.value, u.target_name, u.reason.value)
        for u in report.unresolved
    }
