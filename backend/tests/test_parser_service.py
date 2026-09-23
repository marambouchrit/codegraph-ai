"""Tests for ParserService: parser selection, safe file reading and batch parsing."""

import os
from pathlib import Path

import pytest

from app.core.errors import SourceFileError, UnsupportedLanguageError
from app.ingestion.languages import Language
from app.ingestion.scanner import ScannedFile, scan_directory
from app.parsing.java_parser import JavaParser
from app.parsing.javascript_parser import JavaScriptParser
from app.parsing.python_parser import PythonParser
from app.parsing.service import ParserService, read_source_file
from app.parsing.typescript_parser import TypeScriptParser

MAX_FILE_BYTES = 1024 * 1024


@pytest.fixture
def service() -> ParserService:
    return ParserService(max_file_bytes=MAX_FILE_BYTES)


def write(root: Path, relative_path: str, content: str | bytes) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


# ----- Parser selection -----


@pytest.mark.parametrize(
    ("language", "parser_class"),
    [
        (Language.PYTHON, PythonParser),
        (Language.JAVA, JavaParser),
        (Language.JAVASCRIPT, JavaScriptParser),
        (Language.TYPESCRIPT, TypeScriptParser),
        ("python", PythonParser),  # plain strings work too
    ],
)
def test_selects_parser_for_language(
    service: ParserService, language: Language | str, parser_class: type
) -> None:
    assert isinstance(service.get_parser(language), parser_class)


def test_supports_the_four_languages(service: ParserService) -> None:
    assert set(service.supported_languages) == set(Language)


@pytest.mark.parametrize("language", ["ruby", "go", "", "PYTHON"])
def test_unknown_language_is_rejected(service: ParserService, language: str) -> None:
    with pytest.raises(UnsupportedLanguageError):
        service.get_parser(language)


def test_language_without_registered_parser_is_rejected() -> None:
    python_only = ParserService(max_file_bytes=MAX_FILE_BYTES, parsers=[PythonParser()])

    with pytest.raises(UnsupportedLanguageError):
        python_only.get_parser(Language.JAVA)


@pytest.mark.parametrize(
    ("path", "language", "root_type"),
    [
        ("main.py", Language.PYTHON, "module"),
        ("Main.java", Language.JAVA, "program"),
        ("main.mjs", Language.JAVASCRIPT, "program"),
        ("main.cts", Language.TYPESCRIPT, "program"),
    ],
)
def test_parse_source_detects_language_from_path(
    service: ParserService, path: str, language: Language, root_type: str
) -> None:
    result = service.parse_source(b"", path)

    assert result.language == language
    assert result.root_node.type == root_type


def test_explicit_language_overrides_detection(service: ParserService) -> None:
    result = service.parse_source(b"x = 1\n", "script.txt", Language.PYTHON)

    assert result.language == Language.PYTHON
    assert result.has_syntax_errors is False


@pytest.mark.parametrize("path", ["README.md", "style.css", "Makefile", "main.rb"])
def test_parse_source_rejects_unsupported_files(service: ParserService, path: str) -> None:
    with pytest.raises(UnsupportedLanguageError):
        service.parse_source(b"anything", path)


# ----- Reading files -----


def test_parse_file_reads_and_parses(service: ParserService, tmp_path: Path) -> None:
    write(tmp_path, "src/app.ts", "export const answer: number = 42;\n")

    result = service.parse_file(tmp_path, "src/app.ts")

    assert result.path == "src/app.ts"
    assert result.language == Language.TYPESCRIPT
    assert result.has_syntax_errors is False


def test_parse_empty_file(service: ParserService, tmp_path: Path) -> None:
    write(tmp_path, "empty.py", "")

    result = service.parse_file(tmp_path, "empty.py")

    assert result.has_syntax_errors is False
    assert result.root_info.child_count == 0


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SourceFileError, match="does not exist"):
        read_source_file(tmp_path, "missing.py", MAX_FILE_BYTES)


def test_directory_is_not_read_as_file(tmp_path: Path) -> None:
    (tmp_path / "package.py").mkdir()

    with pytest.raises(SourceFileError, match="not a file"):
        read_source_file(tmp_path, "package.py", MAX_FILE_BYTES)


@pytest.mark.parametrize("path", ["../outside.py", "sub/../../outside.py"])
def test_paths_outside_the_project_are_refused(tmp_path: Path, path: str) -> None:
    project = tmp_path / "project"
    write(project, "sub/app.py", "x = 1")
    write(tmp_path, "outside.py", "secret = 1")

    with pytest.raises(SourceFileError, match="outside the project"):
        read_source_file(project, path, MAX_FILE_BYTES)


def test_absolute_path_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write(tmp_path, "outside.py", "secret = 1")

    with pytest.raises(SourceFileError, match="outside the project"):
        read_source_file(project, str(tmp_path / "outside.py"), MAX_FILE_BYTES)


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write(tmp_path, "outside.py", "secret = 1")
    project.mkdir()
    try:
        os.symlink(tmp_path / "outside.py", project / "link.py")
        os.symlink(tmp_path, project / "linked_dir", target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this system")

    with pytest.raises(SourceFileError, match="symbolic link"):
        read_source_file(project, "link.py", MAX_FILE_BYTES)
    with pytest.raises(SourceFileError, match="outside the project"):
        read_source_file(project, "linked_dir/outside.py", MAX_FILE_BYTES)


def test_file_larger_than_limit_is_refused(tmp_path: Path) -> None:
    write(tmp_path, "big.py", "x" * 101)
    write(tmp_path, "exact.py", "x" * 100)

    assert len(read_source_file(tmp_path, "exact.py", max_file_bytes=100)) == 100
    with pytest.raises(SourceFileError, match="larger than 100 bytes"):
        read_source_file(tmp_path, "big.py", max_file_bytes=100)


def test_binary_file_is_refused(tmp_path: Path) -> None:
    write(tmp_path, "fake.py", b"x = 1\x00\x01\x02")

    with pytest.raises(SourceFileError, match="binary"):
        read_source_file(tmp_path, "fake.py", MAX_FILE_BYTES)


# ----- Parsing many files -----


def test_parse_files_keeps_going_after_problems(service: ParserService, tmp_path: Path) -> None:
    write(tmp_path, "good.py", "def ok():\n    return 1\n")
    write(tmp_path, "broken.js", "function ( {\n")
    write(tmp_path, "binary.py", b"\x00\x00")
    files = [
        ScannedFile(path="good.py", language=Language.PYTHON, size_bytes=24),
        ScannedFile(path="broken.js", language=Language.JAVASCRIPT, size_bytes=13),
        ScannedFile(path="deleted.java", language=Language.JAVA, size_bytes=10),
        ScannedFile(path="binary.py", language=Language.PYTHON, size_bytes=2),
    ]

    report = service.parse_files(tmp_path, files)

    assert [result.path for result in report.results] == ["good.py", "broken.js"]
    assert [result.path for result in report.files_with_syntax_errors] == ["broken.js"]
    assert [failure.path for failure in report.failures] == ["deleted.java", "binary.py"]
    assert "does not exist" in report.failures[0].reason
    assert "binary" in report.failures[1].reason


def test_parse_files_survives_unexpected_errors(
    service: ParserService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "a.py", "x = 1\n")
    write(tmp_path, "b.py", "y = 2\n")

    original_parse = PythonParser.parse

    def flaky_parse(self: PythonParser, source: bytes, path: str):  # type: ignore[no-untyped-def]
        if path == "a.py":
            raise RuntimeError("boom")
        return original_parse(self, source, path)

    monkeypatch.setattr(PythonParser, "parse", flaky_parse)
    files = scan_directory(tmp_path, MAX_FILE_BYTES).files

    report = service.parse_files(tmp_path, files)

    assert [result.path for result in report.results] == ["b.py"]
    assert report.failures[0].path == "a.py"
    assert report.failures[0].reason == "Unexpected parser error."


def test_parses_a_scanned_multi_language_project(service: ParserService, tmp_path: Path) -> None:
    """End to end with Phase 2: scan a folder, then parse everything it found."""
    write(tmp_path, "backend/app.py", "import os\n\nprint(os.name)\n")
    write(tmp_path, "backend/Main.java", "class Main { public static void main(String[] a) {} }\n")
    write(tmp_path, "web/index.js", "export default function main() {}\n")
    write(tmp_path, "web/App.tsx", "export const App = () => <div />;\n")
    write(tmp_path, "web/types.d.ts", "declare module 'x' { export const y: number; }\n")
    write(tmp_path, "README.md", "# not code")

    report = service.parse_files(tmp_path, scan_directory(tmp_path, MAX_FILE_BYTES).files)

    assert report.failures == []
    assert report.files_with_syntax_errors == []
    assert {result.path: result.language for result in report.results} == {
        "backend/Main.java": Language.JAVA,
        "backend/app.py": Language.PYTHON,
        "web/App.tsx": Language.TYPESCRIPT,
        "web/index.js": Language.JAVASCRIPT,
        "web/types.d.ts": Language.TYPESCRIPT,
    }
