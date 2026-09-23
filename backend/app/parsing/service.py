"""Parse source files into Tree-sitter syntax trees.

`ParserService` is the entry point of the parsing layer:

    source file -> read safely -> pick the parser for its language -> ParseResult

It knows nothing about FastAPI, and nothing about how the files were obtained:
it only needs a project root folder and relative file paths (typically the
`ScannedFile`s returned by `scan_directory()` for `workspace/<id>/source/`).
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import AppError, SourceFileError, UnsupportedLanguageError
from app.ingestion.languages import Language, detect_language
from app.ingestion.scanner import ScannedFile
from app.parsing.base import LanguageParser, ParseResult
from app.parsing.java_parser import JavaParser
from app.parsing.javascript_parser import JavaScriptParser
from app.parsing.python_parser import PythonParser
from app.parsing.typescript_parser import TypeScriptParser

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParseFailure:
    """A file that could not be parsed at all (as opposed to one with syntax errors)."""

    path: str
    reason: str


@dataclass
class ParseReport:
    """Outcome of parsing many files. One bad file never stops the others."""

    results: list[ParseResult] = field(default_factory=list)
    failures: list[ParseFailure] = field(default_factory=list)

    @property
    def files_with_syntax_errors(self) -> list[ParseResult]:
        return [result for result in self.results if result.has_syntax_errors]


def default_parsers() -> list[LanguageParser]:
    return [PythonParser(), JavaParser(), JavaScriptParser(), TypeScriptParser()]


class ParserService:
    def __init__(
        self, max_file_bytes: int, parsers: Iterable[LanguageParser] | None = None
    ) -> None:
        self.max_file_bytes = max_file_bytes
        chosen = default_parsers() if parsers is None else parsers
        self._parsers = {parser.language: parser for parser in chosen}

    @property
    def supported_languages(self) -> list[Language]:
        return sorted(self._parsers)

    def get_parser(self, language: Language | str) -> LanguageParser:
        """Return the parser registered for `language` (a Language or a name like "python")."""
        try:
            parser = self._parsers.get(Language(language))
        except ValueError:
            parser = None  # not even a known Language value, e.g. "ruby"
        if parser is None:
            raise UnsupportedLanguageError(f"No parser is available for language '{language}'.")
        return parser

    def parse_source(
        self, source: bytes, path: str, language: Language | str | None = None
    ) -> ParseResult:
        """Parse source code that is already in memory.

        `path` is used for reporting and, if `language` is not given, to detect it.
        """
        if language is None:
            language = detect_language(path)
            if language is None:
                raise UnsupportedLanguageError(f"'{path}' is not a supported source file.")
        return self.get_parser(language).parse(source, path)

    def parse_file(
        self, root: Path, relative_path: str, language: Language | str | None = None
    ) -> ParseResult:
        """Read `root/relative_path` safely and parse it."""
        source = read_source_file(root, relative_path, self.max_file_bytes)
        return self.parse_source(source, relative_path, language)

    def parse_files(self, root: Path, files: Iterable[ScannedFile]) -> ParseReport:
        """Parse every file; files that cannot be parsed are recorded as failures.

        Note: every syntax tree is kept in memory. For very large projects, call
        `parse_file()` in a loop and process each result before parsing the next.
        """
        report = ParseReport()
        for file in files:
            try:
                report.results.append(self.parse_file(root, file.path, file.language))
            except AppError as error:
                report.failures.append(ParseFailure(path=file.path, reason=error.message))
            except Exception:
                # Never let one unexpected problem abort the analysis of a whole project.
                logger.exception("Unexpected error while parsing %s", file.path)
                report.failures.append(
                    ParseFailure(path=file.path, reason="Unexpected parser error.")
                )
        return report


def read_source_file(root: Path, relative_path: str, max_file_bytes: int) -> bytes:
    """Return the bytes of a source file inside `root`, refusing anything unsafe.

    Imported repositories are untrusted, so even though the scanner already filtered
    the files, they are checked again here: this function may be called with other
    paths, and files could change between scanning and parsing.
    """
    root = root.resolve()
    file_path = root / relative_path
    try:
        if file_path.is_symlink():
            raise SourceFileError(f"'{relative_path}' is a symbolic link and was not read.")
        # resolve() also follows symlinked parent folders and "..", so this check
        # guarantees the file is really inside the project.
        if not file_path.resolve().is_relative_to(root):
            raise SourceFileError(f"'{relative_path}' is outside the project folder.")
        if not file_path.is_file():
            raise SourceFileError(f"'{relative_path}' does not exist or is not a file.")

        with file_path.open("rb") as file:
            # Read one byte more than allowed: that is enough to know the file is too large.
            source = file.read(max_file_bytes + 1)
    except OSError as error:
        reason = error.strerror or str(error)
        raise SourceFileError(f"'{relative_path}' could not be read: {reason}") from error

    if len(source) > max_file_bytes:
        raise SourceFileError(f"'{relative_path}' is larger than {max_file_bytes} bytes.")
    if b"\x00" in source:
        raise SourceFileError(f"'{relative_path}' looks like a binary file.")
    return source
