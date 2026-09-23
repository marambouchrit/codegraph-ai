"""Tests for the individual language parsers and syntax-error detection."""

import pytest

from app.ingestion.languages import Language
from app.parsing.base import (
    LanguageParser,
    SyntaxErrorInfo,
    SyntaxErrorKind,
    find_syntax_errors,
)
from app.parsing.java_parser import JavaParser
from app.parsing.javascript_parser import JavaScriptParser
from app.parsing.python_parser import PythonParser
from app.parsing.typescript_parser import TSX_GRAMMAR, TYPESCRIPT_GRAMMAR, TypeScriptParser

VALID_PYTHON = b'''\
import os


class Greeter:
    def __init__(self, name: str) -> None:
        self.name = name

    def greet(self) -> str:
        return f"Hello, {self.name}!"


def main():
    print(Greeter(os.getenv("USER", "world")).greet())
'''

VALID_JAVA = b"""\
package com.example;

import java.util.List;

public class UserService implements Service {
    private final List<String> users;

    public UserService(List<String> users) {
        this.users = users;
    }

    @Override
    public int count() {
        return users.size();
    }
}
"""

VALID_JAVASCRIPT = b"""\
import { readFile } from "node:fs/promises";

export class Cache {
  #items = new Map();

  get(key) {
    return this.#items.get(key) ?? null;
  }
}

export const load = async (path) => JSON.parse(await readFile(path, "utf8"));
"""

VALID_JSX = b"""\
export function Title({ text }) {
  return <h1 className="title">{text}</h1>;
}
"""

VALID_TYPESCRIPT = b"""\
export interface User {
  id: number;
  name: string;
}

type Id = User["id"];

export class UserRepository<T extends User> {
  private readonly users = new Map<Id, T>();

  find(id: Id): T | undefined {
    return this.users.get(id);
  }
}

const legacyCast = <number>someValue;
"""

VALID_TSX = b"""\
type Props = { text: string };

export const Title = ({ text }: Props): JSX.Element => <h1>{text}</h1>;
"""


@pytest.mark.parametrize(
    ("parser", "path", "source", "root_type"),
    [
        (PythonParser(), "app/greeter.py", VALID_PYTHON, "module"),
        (JavaParser(), "src/UserService.java", VALID_JAVA, "program"),
        (JavaScriptParser(), "src/cache.js", VALID_JAVASCRIPT, "program"),
        (JavaScriptParser(), "src/Title.jsx", VALID_JSX, "program"),
        (TypeScriptParser(), "src/users.ts", VALID_TYPESCRIPT, "program"),
        (TypeScriptParser(), "src/Title.tsx", VALID_TSX, "program"),
    ],
)
def test_parses_valid_source(
    parser: LanguageParser, path: str, source: bytes, root_type: str
) -> None:
    result = parser.parse(source, path)

    assert result.path == path
    assert result.language == parser.language
    assert result.source == source
    assert result.has_syntax_errors is False
    assert result.syntax_errors == []
    assert result.root_node.type == root_type
    assert result.root_info.type == root_type
    assert result.root_info.child_count > 0
    assert result.root_info.start_line == 1
    assert result.root_info.end_byte == len(source)


def test_python_tree_contains_expected_nodes() -> None:
    result = PythonParser().parse(VALID_PYTHON, "greeter.py")

    top_level_types = [child.type for child in result.root_node.children]

    assert top_level_types == ["import_statement", "class_definition", "function_definition"]
    class_node = result.root_node.children[1]
    assert class_node.child_by_field_name("name").text == b"Greeter"


def test_java_tree_contains_class_declaration() -> None:
    result = JavaParser().parse(VALID_JAVA, "UserService.java")

    top_level_types = [child.type for child in result.root_node.children]

    assert top_level_types == ["package_declaration", "import_declaration", "class_declaration"]


@pytest.mark.parametrize(
    ("parser", "path", "source"),
    [
        (PythonParser(), "broken.py", b"def broken(:\n    return 1\n"),
        (JavaParser(), "Broken.java", b"public class Broken {\n    void run( {\n    }\n}\n"),
        (JavaScriptParser(), "broken.js", b"function broken() {\n  const = 5;\n}\n"),
        (TypeScriptParser(), "broken.ts", b"let value: = 3;\ninterface {\n"),
    ],
)
def test_detects_syntax_errors_without_raising(
    parser: LanguageParser, path: str, source: bytes
) -> None:
    result = parser.parse(source, path)

    assert result.has_syntax_errors is True
    assert result.syntax_errors, "at least one syntax error should be reported"
    # Tree-sitter still returns a full tree, so later phases can use the valid parts.
    assert result.root_node is not None
    assert result.root_info.end_byte == len(source)


def test_reports_missing_token_with_position() -> None:
    result = JavaParser().parse(b"class A {\n  int x = 1\n}\n", "A.java")

    assert result.syntax_errors == [
        SyntaxErrorInfo(
            kind=SyntaxErrorKind.MISSING,
            node_type=";",
            start_line=2,
            start_column=12,
            end_line=2,
            end_column=12,
        )
    ]
    assert result.syntax_errors[0].message == "Missing ';' at line 2"


def test_reports_error_nodes_in_source_order() -> None:
    source = b"def ok():\n    pass\n\nx = = 1\n\ny = ) 2\n"

    errors = PythonParser().parse(source, "errors.py").syntax_errors

    assert [error.kind for error in errors] == [SyntaxErrorKind.ERROR, SyntaxErrorKind.ERROR]
    assert [error.start_line for error in errors] == [4, 6]
    assert errors[0].message == "Invalid syntax at line 4"


def test_number_of_reported_errors_is_limited() -> None:
    source = b"".join(b"x = = %d\n" % i for i in range(20))
    root = PythonParser().parse(source, "many.py").root_node

    assert len(find_syntax_errors(root, limit=5)) == 5


@pytest.mark.parametrize(
    ("parser", "root_type"),
    [
        (PythonParser(), "module"),
        (JavaParser(), "program"),
        (JavaScriptParser(), "program"),
        (TypeScriptParser(), "program"),
    ],
)
def test_empty_source_gives_empty_tree_without_errors(
    parser: LanguageParser, root_type: str
) -> None:
    result = parser.parse(b"", "empty")

    assert result.has_syntax_errors is False
    assert result.root_info.type == root_type
    assert result.root_info.child_count == 0
    assert result.root_info.end_byte == 0


def test_whitespace_and_comments_only_file_has_no_errors() -> None:
    result = PythonParser().parse(b"\n\n# just a comment\n   \n", "comments.py")

    assert result.has_syntax_errors is False


def test_handles_byte_order_mark_and_invalid_utf8() -> None:
    assert PythonParser().parse(b"\xef\xbb\xbfx = 1\r\n", "bom.py").has_syntax_errors is False
    assert PythonParser().parse(b'x = "caf\xe9"\n', "latin1.py").has_syntax_errors is False


def test_typescript_uses_tsx_grammar_only_for_tsx_files() -> None:
    parser = TypeScriptParser()

    assert parser.grammar_for("a.ts") is TYPESCRIPT_GRAMMAR
    assert parser.grammar_for("a.mts") is TYPESCRIPT_GRAMMAR
    assert parser.grammar_for("Component.tsx") is TSX_GRAMMAR
    assert parser.grammar_for("Component.TSX") is TSX_GRAMMAR


def test_jsx_in_plain_typescript_file_is_a_syntax_error() -> None:
    # JSX is only valid in .tsx files, exactly like the TypeScript compiler.
    parser = TypeScriptParser()

    assert parser.parse(VALID_TSX, "Title.ts").has_syntax_errors is True
    assert parser.parse(VALID_TSX, "Title.tsx").has_syntax_errors is False


@pytest.mark.parametrize(
    ("parser", "language"),
    [
        (PythonParser(), Language.PYTHON),
        (JavaParser(), Language.JAVA),
        (JavaScriptParser(), Language.JAVASCRIPT),
        (TypeScriptParser(), Language.TYPESCRIPT),
    ],
)
def test_each_parser_declares_its_language(parser: LanguageParser, language: Language) -> None:
    assert parser.language == language
