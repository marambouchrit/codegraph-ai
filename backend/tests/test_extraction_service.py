"""Tests for EntityExtractionService: extractor selection, IDs and project extraction."""

from pathlib import Path

import pytest

from app.core.errors import UnsupportedLanguageError
from app.extraction.java_extractor import JavaExtractor
from app.extraction.javascript_extractor import JavaScriptExtractor
from app.extraction.models import Entity, EntityType
from app.extraction.python_extractor import PythonExtractor
from app.extraction.service import EntityExtractionService
from app.extraction.typescript_extractor import TypeScriptExtractor
from app.ingestion.languages import Language
from app.ingestion.scanner import ScannedFile, scan_directory
from app.parsing.base import ParseResult
from app.parsing.service import ParserService

MAX_FILE_BYTES = 1024 * 1024
PROJECT_ID = "a" * 32


@pytest.fixture
def parser_service() -> ParserService:
    return ParserService(max_file_bytes=MAX_FILE_BYTES)


@pytest.fixture
def service(parser_service: ParserService) -> EntityExtractionService:
    return EntityExtractionService(parser_service)


def write(root: Path, relative_path: str, content: str | bytes) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


# ----- Extractor selection -----


@pytest.mark.parametrize(
    ("language", "extractor_class"),
    [
        (Language.PYTHON, PythonExtractor),
        (Language.JAVA, JavaExtractor),
        (Language.JAVASCRIPT, JavaScriptExtractor),
        (Language.TYPESCRIPT, TypeScriptExtractor),
        ("java", JavaExtractor),
    ],
)
def test_selects_extractor_for_language(
    service: EntityExtractionService, language: Language | str, extractor_class: type
) -> None:
    assert type(service.get_extractor(language)) is extractor_class


@pytest.mark.parametrize("language", ["ruby", "go", ""])
def test_unknown_language_is_rejected(service: EntityExtractionService, language: str) -> None:
    with pytest.raises(UnsupportedLanguageError):
        service.get_extractor(language)


def test_parsed_language_without_extractor_is_rejected(parser_service: ParserService) -> None:
    python_only = EntityExtractionService(parser_service, extractors=[PythonExtractor()])
    parse_result = parser_service.parse_source(b"class A {}", "A.java")

    with pytest.raises(UnsupportedLanguageError):
        python_only.extract(parse_result, PROJECT_ID)


def test_unsupported_file_is_rejected_by_phase_3_before_extraction(
    parser_service: ParserService,
) -> None:
    # Language detection stays in Phase 3: extraction only ever receives a ParseResult.
    with pytest.raises(UnsupportedLanguageError):
        parser_service.parse_source(b"class A; end", "a.rb")


# ----- Reusing Phase 3 -----


def test_extract_uses_the_given_tree_without_parsing_again(
    service: EntityExtractionService,
    parser_service: ParserService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_result = parser_service.parse_source(b"def f():\n    pass\n", "a.py")

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("extraction must not parse the file again")

    monkeypatch.setattr(ParserService, "parse_source", fail)
    monkeypatch.setattr(ParserService, "parse_file", fail)

    result = service.extract(parse_result, PROJECT_ID)

    assert [entity.name for entity in result.entities] == ["a.py", "f"]


def test_extract_reports_metadata_from_the_parse_result(
    service: EntityExtractionService, parser_service: ParserService
) -> None:
    parse_result: ParseResult = parser_service.parse_source(b"class A:\n  def (\n", "src/a.py")

    result = service.extract(parse_result, PROJECT_ID)

    assert result.path == "src/a.py"
    assert result.language == Language.PYTHON
    assert result.has_syntax_errors is True


# ----- IDs -----


def test_ids_are_readable_and_include_project_and_path(
    service: EntityExtractionService, parser_service: ParserService
) -> None:
    source = b"class User:\n    def login(self):\n        pass\n"

    result = service.extract(parser_service.parse_source(source, "src/user.py"), PROJECT_ID)

    assert [entity.id for entity in result.entities] == [
        f"{PROJECT_ID}:src/user.py",
        f"{PROJECT_ID}:src/user.py:User",
        f"{PROJECT_ID}:src/user.py:User.login",
    ]


def test_ids_are_deterministic(
    service: EntityExtractionService, parser_service: ParserService
) -> None:
    source = b"class User {\n  login() {}\n}\nfunction createUser() {}\n"

    first = service.extract(parser_service.parse_source(source, "user.js"), PROJECT_ID)
    second = service.extract(parser_service.parse_source(source, "user.js"), PROJECT_ID)

    assert [entity.id for entity in first.entities] == [entity.id for entity in second.entities]
    assert first.entities == second.entities


def test_ids_do_not_change_when_code_moves(
    service: EntityExtractionService, parser_service: ParserService
) -> None:
    before = service.extract(parser_service.parse_source(b"def f(): pass\n", "a.py"), PROJECT_ID)
    after = service.extract(
        parser_service.parse_source(b"import os\n\n\ndef f(): pass\n", "a.py"), PROJECT_ID
    )

    assert before.entities[1].id == after.entities[1].id
    assert before.entities[1].start_line != after.entities[1].start_line


def test_same_qualified_name_gets_numbered_ids(
    service: EntityExtractionService, parser_service: ParserService
) -> None:
    source = b"""\
class User {
    void login() {}
    void login(String password) {}
    void login(String user, String password) {}
}
"""
    result = service.extract(parser_service.parse_source(source, "User.java"), PROJECT_ID)

    login_ids = [entity.id for entity in result.entities if entity.name == "login"]
    assert login_ids == [
        f"{PROJECT_ID}:User.java:User.login",
        f"{PROJECT_ID}:User.java:User.login#2",
        f"{PROJECT_ID}:User.java:User.login#3",
    ]


def test_same_name_in_different_files_or_projects_gets_different_ids(
    service: EntityExtractionService, parser_service: ParserService
) -> None:
    source = b"def main():\n    pass\n"

    ids = {
        service.extract(parser_service.parse_source(source, path), project_id).entities[1].id
        for path in ("a.py", "b.py")
        for project_id in (PROJECT_ID, "b" * 32)
    }

    assert len(ids) == 4


# ----- Whole projects -----


def test_extract_project_combines_scanner_parser_and_extractors(
    service: EntityExtractionService, tmp_path: Path
) -> None:
    write(tmp_path, "backend/models.py", "class User:\n    def login(self):\n        pass\n")
    write(tmp_path, "backend/Auth.java", "interface Auth { void login(); }\n")
    write(tmp_path, "web/api.js", "export function fetchUsers() {}\n")
    write(tmp_path, "web/App.tsx", "export const App = () => <div />;\n")
    write(tmp_path, "README.md", "# docs")
    files = scan_directory(tmp_path, MAX_FILE_BYTES).files

    report = service.extract_project(PROJECT_ID, tmp_path, files)

    assert report.project_id == PROJECT_ID
    assert report.failures == []
    assert [file.path for file in report.files] == [
        "backend/Auth.java",
        "backend/models.py",
        "web/App.tsx",
        "web/api.js",
    ]
    assert report.entity_counts == {
        "class": 1,
        "file": 4,
        "function": 2,
        "interface": 1,
        "method": 2,
    }
    all_ids = [entity.id for entity in report.entities]
    assert len(all_ids) == len(set(all_ids)), "IDs must be unique within a project"


def test_extract_project_keeps_going_after_problems(
    service: EntityExtractionService, tmp_path: Path
) -> None:
    write(tmp_path, "good.py", "def ok():\n    pass\n")
    write(tmp_path, "broken.py", "class User:\n    def login(self):\n        pass\nx = = 1\n")
    write(tmp_path, "binary.js", b"\x00\x01")
    files = [
        ScannedFile(path="good.py", language=Language.PYTHON, size_bytes=20),
        ScannedFile(path="broken.py", language=Language.PYTHON, size_bytes=50),
        ScannedFile(path="binary.js", language=Language.JAVASCRIPT, size_bytes=2),
        ScannedFile(path="deleted.ts", language=Language.TYPESCRIPT, size_bytes=10),
    ]

    report = service.extract_project(PROJECT_ID, tmp_path, files)

    assert [file.path for file in report.files] == ["good.py", "broken.py"]
    assert report.files[1].has_syntax_errors is True
    assert [entity.name for entity in report.files[1].entities] == ["broken.py", "User", "login"]
    assert [failure.path for failure in report.failures] == ["binary.js", "deleted.ts"]
    assert "binary" in report.failures[0].reason
    assert "does not exist" in report.failures[1].reason


def test_extract_project_survives_unexpected_errors(
    service: EntityExtractionService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "a.py", "def a():\n    pass\n")
    write(tmp_path, "b.py", "def b():\n    pass\n")
    original_extract = PythonExtractor.extract

    def flaky_extract(
        self: PythonExtractor, parse_result: ParseResult, project_id: str
    ) -> list[Entity]:
        if parse_result.path == "a.py":
            raise RuntimeError("boom")
        return original_extract(self, parse_result, project_id)

    monkeypatch.setattr(PythonExtractor, "extract", flaky_extract)

    report = service.extract_project(
        PROJECT_ID, tmp_path, scan_directory(tmp_path, MAX_FILE_BYTES).files
    )

    assert [file.path for file in report.files] == ["b.py"]
    assert report.failures[0].path == "a.py"
    assert report.failures[0].reason == "Unexpected extraction error."


def test_empty_project(service: EntityExtractionService, tmp_path: Path) -> None:
    report = service.extract_project(PROJECT_ID, tmp_path, [])

    assert report.files == []
    assert report.entities == []
    assert report.entity_counts == {}


def test_file_entities_helpers(
    service: EntityExtractionService, parser_service: ParserService
) -> None:
    source = b"class A:\n    def x(self): pass\n    def y(self): pass\n\ndef f(): pass\n"
    result = service.extract(parser_service.parse_source(source, "a.py"), PROJECT_ID)
    class_a = result.of_type(EntityType.CLASS)[0]

    assert [entity.name for entity in result.children_of(class_a)] == ["x", "y"]
    assert [entity.name for entity in result.children_of(result.file)] == ["A", "f"]
    assert [entity.name for entity in result.of_type(EntityType.FUNCTION)] == ["f"]
