"""Extract code entities (classes, interfaces, functions, methods) from syntax trees.

`EntityExtractionService` is the entry point of the extraction layer:

    ScannedFile -> ParserService (Phase 3) -> ParseResult -> language extractor -> entities

It never reads or parses files itself: that is ParserService's job. Like the
other layers, it knows nothing about FastAPI.
"""

import logging
from collections.abc import Iterable
from pathlib import Path

from app.core.errors import AppError, UnsupportedLanguageError
from app.extraction.base import EntityExtractor
from app.extraction.java_extractor import JavaExtractor
from app.extraction.javascript_extractor import JavaScriptExtractor
from app.extraction.models import ExtractionReport, FileEntities
from app.extraction.python_extractor import PythonExtractor
from app.extraction.typescript_extractor import TypeScriptExtractor
from app.ingestion.languages import Language
from app.ingestion.scanner import ScannedFile
from app.parsing.base import ParseResult
from app.parsing.service import ParseFailure, ParserService

logger = logging.getLogger(__name__)


def default_extractors() -> list[EntityExtractor]:
    return [PythonExtractor(), JavaExtractor(), JavaScriptExtractor(), TypeScriptExtractor()]


class EntityExtractionService:
    def __init__(
        self,
        parser_service: ParserService,
        extractors: Iterable[EntityExtractor] | None = None,
    ) -> None:
        self.parser_service = parser_service
        chosen = default_extractors() if extractors is None else extractors
        self._extractors = {extractor.language: extractor for extractor in chosen}

    def get_extractor(self, language: Language | str) -> EntityExtractor:
        """Return the extractor registered for `language` (a Language or a name like "java")."""
        try:
            extractor = self._extractors.get(Language(language))
        except ValueError:
            extractor = None  # not even a known Language value, e.g. "ruby"
        if extractor is None:
            raise UnsupportedLanguageError(f"No entity extractor for language '{language}'.")
        return extractor

    def extract(self, parse_result: ParseResult, project_id: str) -> FileEntities:
        """Extract the entities of one already-parsed file.

        Files with syntax errors are not rejected: Tree-sitter still builds a tree,
        and every well-formed definition in it is extracted.
        """
        extractor = self.get_extractor(parse_result.language)
        return FileEntities(
            path=parse_result.path,
            language=parse_result.language,
            has_syntax_errors=parse_result.has_syntax_errors,
            entities=extractor.extract(parse_result, project_id),
        )

    def extract_project(
        self, project_id: str, root: Path, files: Iterable[ScannedFile]
    ) -> ExtractionReport:
        """Parse and extract every file, one at a time.

        Each syntax tree is released as soon as its entities are extracted, so memory
        use does not grow with the number of files (only the small entities are kept).
        """
        report = ExtractionReport(project_id=project_id)
        for file in files:
            try:
                parse_result = self.parser_service.parse_file(root, file.path, file.language)
                report.files.append(self.extract(parse_result, project_id))
            except AppError as error:
                report.failures.append(ParseFailure(path=file.path, reason=error.message))
            except Exception:
                # Never let one unexpected problem abort the analysis of a whole project.
                logger.exception("Unexpected error while extracting entities from %s", file.path)
                report.failures.append(
                    ParseFailure(path=file.path, reason="Unexpected extraction error.")
                )
        return report
