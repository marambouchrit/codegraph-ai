"""Tests for RelationshipExtractionService: whole projects, IDs, duplicates, broken code."""

from pathlib import Path

import pytest

from app.core.errors import UnsupportedLanguageError
from app.extraction.service import EntityExtractionService
from app.ingestion.languages import Language
from app.ingestion.scanner import scan_directory
from app.parsing.service import ParserService
from app.relationships.java_references import JavaReferenceCollector
from app.relationships.javascript_references import JavaScriptReferenceCollector
from app.relationships.models import RelationshipType
from app.relationships.python_references import PythonReferenceCollector
from app.relationships.service import RelationshipExtractionService
from app.relationships.typescript_references import TypeScriptReferenceCollector
from tests.relationship_helpers import PROJECT_ID, analyze, edges, unresolved

MAX_FILE_BYTES = 1024 * 1024
CALLS, IMPORTS = RelationshipType.CALLS, RelationshipType.IMPORTS
DEPENDS_ON = RelationshipType.DEPENDS_ON


@pytest.fixture
def parser_service() -> ParserService:
    return ParserService(max_file_bytes=MAX_FILE_BYTES)


@pytest.fixture
def service(parser_service: ParserService) -> RelationshipExtractionService:
    return RelationshipExtractionService(EntityExtractionService(parser_service))


def write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ----- Collector selection -----


@pytest.mark.parametrize(
    ("language", "collector_class"),
    [
        (Language.PYTHON, PythonReferenceCollector),
        (Language.JAVA, JavaReferenceCollector),
        (Language.JAVASCRIPT, JavaScriptReferenceCollector),
        (Language.TYPESCRIPT, TypeScriptReferenceCollector),
        ("java", JavaReferenceCollector),
    ],
)
def test_selects_collector_for_language(
    service: RelationshipExtractionService, language: Language | str, collector_class: type
) -> None:
    assert type(service.get_collector(language)) is collector_class


@pytest.mark.parametrize("language", ["ruby", ""])
def test_unknown_language_is_rejected(service: RelationshipExtractionService, language: str) -> None:
    with pytest.raises(UnsupportedLanguageError):
        service.get_collector(language)


# ----- Whole projects -----


def test_extract_project_parses_each_file_once(
    tmp_path: Path, parser_service: ParserService, service: RelationshipExtractionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    write(tmp_path, "src/models/user.py", "class User:\n    def save(self): pass\n")
    write(
        tmp_path,
        "src/auth.py",
        "from models.user import User\n\ndef login():\n    user = User()\n    user.save()\n",
    )
    write(tmp_path, "web/api.ts", 'import { get } from "./http";\nexport function load() { get(); }\n')
    write(tmp_path, "web/http.ts", "export function get() {}\n")
    parsed: list[str] = []
    original = parser_service.parse_file

    def counting_parse_file(root: Path, path: str, language: Language | str | None = None):  # type: ignore[no-untyped-def]
        parsed.append(path)
        return original(root, path, language)

    monkeypatch.setattr(parser_service, "parse_file", counting_parse_file)

    report = service.extract_project(PROJECT_ID, tmp_path, scan_directory(tmp_path, MAX_FILE_BYTES).files)

    assert sorted(parsed) == ["src/auth.py", "src/models/user.py", "web/api.ts", "web/http.ts"]
    assert report.failures == []
    assert len(report.entities) == 9  # 4 files, User, save, login, load, get
    assert edges(report) >= {
        ("src/auth.py", "IMPORTS", "src/models/user.py"),
        ("src/auth.py:login", "CALLS", "src/models/user.py:User.save"),
        ("web/api.ts", "IMPORTS", "web/http.ts"),
        ("web/api.ts:load", "CALLS", "web/http.ts:get"),
    }
    assert report.relationship_counts == {"CALLS": 3, "DEPENDS_ON": 2, "IMPORTS": 2}


def test_unreadable_file_is_a_failure_and_others_continue(
    tmp_path: Path, service: RelationshipExtractionService
) -> None:
    write(tmp_path, "a.py", "def f(): pass\n")
    write(tmp_path, "b.py", "from a import f\nf()\n")
    files = scan_directory(tmp_path, MAX_FILE_BYTES).files
    (tmp_path / "a.py").unlink()  # removed between scanning and parsing

    report = service.extract_project(PROJECT_ID, tmp_path, files)

    assert [failure.path for failure in report.failures] == ["a.py"]
    assert [file.path for file in report.extraction.files] == ["b.py"]
    assert unresolved(report) >= {("b.py", "IMPORTS", "a", "external")}


def test_collector_error_keeps_the_file_entities(
    tmp_path: Path, service: RelationshipExtractionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "a.py", "def f(): pass\n")

    def broken_visit(*_args: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(PythonReferenceCollector, "visit", broken_visit)

    report = service.extract_project(PROJECT_ID, tmp_path, scan_directory(tmp_path, MAX_FILE_BYTES).files)

    assert report.failures == []
    assert [entity.name for entity in report.entities] == ["a.py", "f"]
    assert report.relationships == []


# ----- IDs and duplicates -----


PROJECT = {
    "user.py": "class User:\n    def save(self): pass\n",
    "auth.py": "from user import User\n\ndef login():\n    user = User()\n    user.save()\n",
}


def test_relationship_ids_are_readable_and_deterministic() -> None:
    first, second = analyze(PROJECT), analyze(PROJECT)

    assert first.relationships == second.relationships
    call = next(r for r in first.relationships if r.target_id == f"{PROJECT_ID}:user.py:User.save")
    assert call.id == f"CALLS:{PROJECT_ID}:auth.py:login->{PROJECT_ID}:user.py:User.save"
    assert (call.file_path, call.line, call.column) == ("auth.py", 5, 5)


def test_repeated_references_give_one_relationship() -> None:
    report = analyze(
        {
            **PROJECT,
            "auth.py": "from user import User\nfrom user import User\n\ndef login():\n"
            + "    user = User()\n    user.save()\n" * 3,
        }
    )

    ids = [r.id for r in report.relationships]
    assert len(ids) == len(set(ids))
    assert len(report.of_type(CALLS)) == 2
    assert len(report.of_type(IMPORTS)) == 1
    assert report.relationships[0].line == 1  # the first occurrence is kept


def test_repeated_unresolved_references_are_listed_once() -> None:
    report = analyze({"a.py": "print(1)\nprint(2)\n"})

    assert len(report.unresolved) == 1
    assert report.unresolved_counts == {"not_found": 1}


# ----- DEPENDS_ON -----


def test_depends_on_needs_real_use_not_just_an_import() -> None:
    report = analyze(
        {
            "user.py": "class User: pass\n",
            "unused.py": "from user import User\n",
            "used.py": "from user import User\n\nclass Admin(User): pass\n",
            "local.py": "def a(): pass\n\ndef b():\n    a()\n",
        }
    )

    assert edges(report, DEPENDS_ON) == {("used.py", "DEPENDS_ON", "user.py")}


# ----- Broken and empty files -----


def test_syntax_errors_keep_valid_regions_and_invent_nothing() -> None:
    report = analyze(
        {
            "a.py": """\
def helper(): pass

def good():
    helper()

def broken(:
    helper()

def also_good():
    helper(
""",
        }
    )

    # Tree-sitter recovers `def broken(:` as a function (Phase 4 extracts it too), but the
    # unfinished call `helper(` ends up in an ERROR node and is ignored.
    assert edges(report, CALLS) == {
        ("a.py:good", "CALLS", "a.py:helper"),
        ("a.py:broken", "CALLS", "a.py:helper"),
    }


def test_broken_class_header_gives_no_inheritance() -> None:
    report = analyze(
        {"base.ts": "export class Base {}\n", "a.ts": 'import { Base } from "./base";\nclass A extends {}\n'}
    )

    assert edges(report, RelationshipType.INHERITS) == set()


@pytest.mark.parametrize("path", ["empty.py", "Empty.java", "empty.js", "empty.ts"])
def test_empty_files_have_no_relationships(path: str) -> None:
    report = analyze({path: ""})

    assert report.relationships == []
    assert report.unresolved == []
    assert [entity.type.value for entity in report.entities] == ["file"]
