"""Tests for the language extractors: which entities are found, with which parent."""

import pytest

from app.extraction.models import Entity, EntityType, FileEntities
from app.extraction.service import EntityExtractionService
from app.parsing.service import ParserService

PROJECT_ID = "0" * 32
CLASS, INTERFACE = EntityType.CLASS, EntityType.INTERFACE
FUNCTION, METHOD = EntityType.FUNCTION, EntityType.METHOD

parser_service = ParserService(max_file_bytes=1024 * 1024)
extraction_service = EntityExtractionService(parser_service)


def extract(source: bytes, path: str) -> FileEntities:
    return extraction_service.extract(parser_service.parse_source(source, path), PROJECT_ID)


def outline(result: FileEntities) -> list[tuple[EntityType, str]]:
    """(type, qualified name) of every entity except the file itself, in source order."""
    return [(entity.type, entity.qualified_name) for entity in result.entities[1:]]


def by_name(result: FileEntities, qualified_name: str) -> Entity:
    return next(e for e in result.entities if e.qualified_name == qualified_name)


# ----- Python -----


def test_python_class_method_and_function() -> None:
    source = b"""\
class User:
    def login(self):
        pass

def create_user():
    pass
"""
    result = extract(source, "src/models/user.py")

    assert outline(result) == [
        (CLASS, "User"),
        (METHOD, "User.login"),
        (FUNCTION, "create_user"),
    ]
    user = by_name(result, "User")
    assert by_name(result, "User.login").parent_id == user.id
    assert user.parent_id == result.file.id
    assert by_name(result, "create_user").parent_id == result.file.id


def test_python_nested_and_decorated_definitions() -> None:
    source = b"""\
@dataclass
class Order:
    class Meta:
        ordering = ["id"]

    @property
    def total(self):
        def add(a, b):
            return a + b
        return add(1, 2)

    async def refresh(self):
        pass

    if DEBUG:
        def debug(self):
            pass


async def main():
    helper = lambda: None
"""
    result = extract(source, "order.py")

    assert outline(result) == [
        (CLASS, "Order"),
        (CLASS, "Order.Meta"),
        (METHOD, "Order.total"),
        (FUNCTION, "Order.total.add"),  # a function inside a method is not a method
        (METHOD, "Order.refresh"),
        (METHOD, "Order.debug"),  # still in the class body, even inside an `if`
        (FUNCTION, "main"),  # lambdas have no name and are not entities
    ]


def test_python_location_includes_decorators() -> None:
    source = b"@app.get('/users')\n@cache\ndef list_users():\n    return []\n"

    entity = by_name(extract(source, "api.py"), "list_users")

    assert (entity.start_line, entity.start_column) == (1, 1)
    assert (entity.end_line, entity.end_column) == (4, 14)


# ----- Java -----


def test_java_class_interface_and_methods() -> None:
    source = b"""\
package com.example;

public class User implements Authenticatable {
    private String name;

    public User(String name) { this.name = name; }

    public void login() {
    }
}

interface Authenticatable {
    void login();
}
"""
    result = extract(source, "src/main/java/com/example/User.java")

    assert outline(result) == [
        (CLASS, "User"),
        (METHOD, "User.User"),  # constructor
        (METHOD, "User.login"),
        (INTERFACE, "Authenticatable"),
        (METHOD, "Authenticatable.login"),
    ]
    assert by_name(result, "User.login").parent_id == by_name(result, "User").id


def test_java_enums_records_annotations_and_inner_classes() -> None:
    source = b"""\
enum Color { RED, GREEN; Color next() { return RED; } }
record Point(int x, int y) { int sum() { return x + y; } }
@interface Audited { String value(); }
class Outer {
    static class Inner { void run() {} }
    void start() {
        Runnable task = new Runnable() { public void run() {} };
    }
}
"""
    assert outline(extract(source, "Types.java")) == [
        (CLASS, "Color"),
        (METHOD, "Color.next"),
        (CLASS, "Point"),
        (METHOD, "Point.sum"),
        (INTERFACE, "Audited"),
        (METHOD, "Audited.value"),
        (CLASS, "Outer"),
        (CLASS, "Outer.Inner"),
        (METHOD, "Outer.Inner.run"),
        (METHOD, "Outer.start"),
        # The anonymous Runnable's run() is not listed: anonymous classes have no name.
    ]


# ----- JavaScript -----


def test_javascript_class_method_and_function() -> None:
    source = b"""\
class User {
    login() {}
}

function createUser() {}
"""
    result = extract(source, "src/user.js")

    assert outline(result) == [
        (CLASS, "User"),
        (METHOD, "User.login"),
        (FUNCTION, "createUser"),
    ]
    assert by_name(result, "User.login").parent_id == by_name(result, "User").id


def test_javascript_common_function_forms() -> None:
    source = b"""\
export class Store extends Base {
  #cache = new Map();
  onChange = () => {};
  config = { run() {} };
  static create() {}
  get size() { return 0; }
  #reset() {}
}
function* ids() {}
async function load() {}
const arrow = async () => {};
var legacy = function () {};
export const exported = () => {};
const Model = class {};
const { a, b } = source;
const settings = { save() {} };
export default function () {}
[1, 2].map(function (n) { function inner() {} return n; });
"""
    assert outline(extract(source, "store.mjs")) == [
        (CLASS, "Store"),
        (METHOD, "Store.onChange"),
        (METHOD, "Store.create"),
        (METHOD, "Store.size"),
        (METHOD, "Store.#reset"),
        (FUNCTION, "ids"),
        (FUNCTION, "load"),
        (FUNCTION, "arrow"),
        (FUNCTION, "legacy"),
        (FUNCTION, "exported"),
        (CLASS, "Model"),
        (FUNCTION, "inner"),  # declared inside an anonymous callback
    ]


def test_jsx_file() -> None:
    source = b"export function Title({ text }) {\n  return <h1 name={text}>{text}</h1>;\n}\n"

    assert outline(extract(source, "Title.jsx")) == [(FUNCTION, "Title")]


# ----- TypeScript -----


def test_typescript_interface_class_method_and_function() -> None:
    source = b"""\
interface User {
    name: string;
}

class UserService {
    login(): void {}
}

function createUser() {}
"""
    result = extract(source, "src/users.ts")

    assert outline(result) == [
        (INTERFACE, "User"),
        (CLASS, "UserService"),
        (METHOD, "UserService.login"),
        (FUNCTION, "createUser"),
    ]
    assert by_name(result, "UserService.login").parent_id == by_name(result, "UserService").id


def test_typescript_specific_declarations() -> None:
    source = b"""\
export interface Repository<T> {
  find(id: number): T;
  options: { retry(): void };
}
abstract class Base {
  abstract run(): void;
}
export class UserRepository extends Base implements Repository<User> {
  private handle = (): void => {};
  run(): void {}
  find(id: string): User;
  find(id: any): User { return this.users[id]; }
}
function parse(value: string): number;
function parse(value: any): number { return 0; }
declare function external(): void;
namespace Utils { export function helper() {} }
enum Color { Red }
type Handler = { handle(): void };
export default class {}
"""
    assert outline(extract(source, "repo.ts")) == [
        (INTERFACE, "Repository"),
        (METHOD, "Repository.find"),
        (CLASS, "Base"),
        (METHOD, "Base.run"),
        (CLASS, "UserRepository"),
        (METHOD, "UserRepository.handle"),
        (METHOD, "UserRepository.run"),
        (METHOD, "UserRepository.find"),  # the implementation, not the overload signature
        (FUNCTION, "parse"),
        (FUNCTION, "helper"),
    ]


def test_tsx_file() -> None:
    source = b"""\
interface Props { text: string }
export const App = ({ text }: Props) => <div>{text}</div>;
export class Page extends Component<Props> {
  render() { return <App text="hi" />; }
}
"""
    result = extract(source, "App.tsx")

    assert result.has_syntax_errors is False
    assert outline(result) == [
        (INTERFACE, "Props"),
        (FUNCTION, "App"),
        (CLASS, "Page"),
        (METHOD, "Page.render"),
    ]


def test_generic_cast_in_ts_file_still_uses_typescript_grammar() -> None:
    result = extract(b"function f() { return <number>value; }\n", "cast.ts")

    assert result.has_syntax_errors is False
    assert outline(result) == [(FUNCTION, "f")]


# ----- The FILE entity and positions -----


def test_file_entity_describes_the_whole_file() -> None:
    source = b"class User:\n    pass\n"

    file = extract(source, "src/models/user.py").file

    assert file.type == EntityType.FILE
    assert file.name == "user.py"
    assert file.qualified_name == "src/models/user.py"
    assert file.file_path == "src/models/user.py"
    assert file.parent_id is None
    assert (file.start_line, file.start_column) == (1, 1)
    assert file.end_line == 3  # the position after the final newline


def test_positions_are_one_based() -> None:
    source = b"class A {\n    void run() {\n    }\n}\n"

    method = by_name(extract(source, "A.java"), "A.run")

    assert (method.start_line, method.start_column) == (2, 5)
    assert (method.end_line, method.end_column) == (3, 6)


def test_every_entity_shares_the_file_path_and_language() -> None:
    result = extract(b"class A:\n    def b(self):\n        pass\n", "pkg/a.py")

    assert {entity.file_path for entity in result.entities} == {"pkg/a.py"}
    assert {entity.language for entity in result.entities} == {result.language}


# ----- Empty files, comments, syntax errors -----


@pytest.mark.parametrize("path", ["empty.py", "Empty.java", "empty.js", "empty.ts", "empty.tsx"])
def test_empty_file_has_only_the_file_entity(path: str) -> None:
    result = extract(b"", path)

    assert [entity.type for entity in result.entities] == [EntityType.FILE]


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("notes.py", b"# class Fake:\n#     def nope(self): pass\n'''def not_code(): pass'''\n"),
        ("Notes.java", b"// class Fake {}\n/* interface Nope { void x(); } */\n"),
        ("notes.js", b"// function fake() {}\n/* class Nope {} */\nconst s = 'function f() {}';\n"),
        ("notes.ts", b"/** interface Fake {} */\n// class Nope {}\n"),
    ],
)
def test_comments_and_strings_do_not_create_entities(path: str, source: bytes) -> None:
    assert outline(extract(source, path)) == []


@pytest.mark.parametrize(
    ("path", "source", "expected"),
    [
        (
            "broken.py",
            b"class User:\n    def login(self):\n        pass\n\nx = = 1\n\ndef create_user():\n"
            b"    pass\n",
            [(CLASS, "User"), (METHOD, "User.login"), (FUNCTION, "create_user")],
        ),
        (
            "Broken.java",
            b"public class User {\n    public void login() {\n        int x = 1\n    }\n"
            b"    public void logout() {}\n}\n",
            [(CLASS, "User"), (METHOD, "User.login"), (METHOD, "User.logout")],
        ),
        (
            "broken.js",
            b"class User {\n  login() {}\n}\nconst = 5;\nfunction createUser() {}\n",
            [(CLASS, "User"), (METHOD, "User.login"), (FUNCTION, "createUser")],
        ),
        (
            "broken.ts",
            b"interface User { name: string; }\nlet value: = 3;\nclass UserService {\n"
            b"  login(): void {}\n}\nfunction createUser() {}\n",
            [
                (INTERFACE, "User"),
                (CLASS, "UserService"),
                (METHOD, "UserService.login"),
                (FUNCTION, "createUser"),
            ],
        ),
    ],
)
def test_valid_entities_are_extracted_from_files_with_syntax_errors(
    path: str, source: bytes, expected: list[tuple[EntityType, str]]
) -> None:
    result = extract(source, path)

    assert result.has_syntax_errors is True
    assert outline(result) == expected


def test_definitions_inside_error_regions_are_not_trusted() -> None:
    # "class :" has no name. Tree-sitter wraps it in an ERROR node, where the method
    # would look like a module-level function; it must not be reported as one.
    source = b"class :\n    def orphan(self):\n        pass\n\ndef ok():\n    pass\n"

    assert outline(extract(source, "broken.py")) == [(FUNCTION, "ok")]


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("nameless.py", b"def (:\n    pass\n"),
        ("nameless.js", b"function ( {\n"),
        ("Nameless.java", b"class {\n  void run() {}\n}\n"),
    ],
)
def test_definitions_without_a_name_are_skipped(path: str, source: bytes) -> None:
    assert outline(extract(source, path)) == []
