"""Reference resolution: finding the right entity, or refusing to guess."""

import pytest

from app.ingestion.languages import Language
from app.relationships.models import RelationshipType, UnresolvedReason
from app.relationships.modules import ModuleIndex
from tests.relationship_helpers import analyze, edges, unresolved

CALLS, IMPORTS = RelationshipType.CALLS, RelationshipType.IMPORTS
EXTERNAL, NOT_FOUND = UnresolvedReason.EXTERNAL, UnresolvedReason.NOT_FOUND
AMBIGUOUS = UnresolvedReason.AMBIGUOUS


# ----- Module lookup -----


def python_index(*paths: str) -> ModuleIndex:
    return ModuleIndex([(path, Language.PYTHON) for path in paths], {})


def script_index(*paths: str) -> ModuleIndex:
    return ModuleIndex([(path, Language.TYPESCRIPT) for path in paths], {})


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        (".user", "src/models/user.py"),
        ("..models.user", "src/models/user.py"),
        (".", "src/models/__init__.py"),
        ("...x", NOT_FOUND),  # above the project root
        ("models.user", "src/models/user.py"),  # "src/" is a source root of the importer
        ("models", "src/models/__init__.py"),  # a package wins over a module
        ("os", EXTERNAL),  # never matches tools/os.py: tools/ is not an ancestor of the importer
        ("requests", EXTERNAL),
    ],
)
def test_python_module_lookup(module: str, expected: str) -> None:
    index = python_index(
        "src/models/__init__.py", "src/models/user.py", "src/models.py", "tools/os.py"
    )
    assert index.python_module("src/models/admin.py", module) == expected


def test_python_module_found_under_two_source_roots_is_ambiguous() -> None:
    index = python_index("app/config.py", "backend/app/config.py")

    assert index.python_module("backend/app/main.py", "app.config") == AMBIGUOUS
    assert index.python_module("app/main.py", "app.config") == "app/config.py"


@pytest.mark.parametrize(
    ("specifier", "expected"),
    [
        ("./models/User", "src/models/User.ts"),
        ("./models/User.js", "src/models/User.ts"),  # TypeScript ESM style
        ("./models", "src/models/index.ts"),
        ("../lib/util", "lib/util.js"),
        ("./view", "src/view.tsx"),
        ("react", EXTERNAL),
        ("@scope/pkg", EXTERNAL),
        ("./styles.css", EXTERNAL),
        ("./missing", NOT_FOUND),
        ("../../outside", NOT_FOUND),
    ],
)
def test_script_module_lookup(specifier: str, expected: str) -> None:
    index = script_index(
        "src/models/User.ts", "src/models/index.ts", "lib/util.js", "src/view.tsx", "src/view.js"
    )
    assert index.script_module("src/app.ts", specifier) == expected


def test_java_packages_come_from_package_declarations() -> None:
    index = ModuleIndex(
        [("a/User.java", Language.JAVA), ("b/Main.java", Language.JAVA)],
        {"a/User.java": "com.example", "b/Main.java": None},
    )

    assert index.java_package("com.example") == ["a/User.java"]
    assert index.java_package("") == ["b/Main.java"]  # the default package
    assert not index.is_java_package("com")


# ----- Duplicate and ambiguous names -----


def test_duplicate_class_names_resolve_through_imports() -> None:
    report = analyze(
        {
            "src/models/user.py": "class User:\n    def save(self): pass\n",
            "src/admin/user.py": "class User:\n    def save(self): pass\n",
            "src/services/auth.py": "from admin.user import User\n\ndef run():\n    User().save()\n",
            "src/services/profile.py": "from ..models.user import User\n\ndef run():\n    User().save()\n",
        }
    )

    assert edges(report, CALLS) >= {
        ("src/services/auth.py:run", "CALLS", "src/admin/user.py:User"),
        ("src/services/profile.py:run", "CALLS", "src/models/user.py:User"),
    }
    assert ("src/services/auth.py:run", "CALLS", "src/models/user.py:User") not in edges(report)


def test_no_guessing_from_a_name_defined_elsewhere() -> None:
    # User exists in the project, but main.py never imports it.
    report = analyze({"models.py": "class User: pass\n", "main.py": "User()\n"})

    assert edges(report) == set()
    assert unresolved(report) == {("main.py", "CALLS", "User", "not_found")}


def test_name_defined_twice_in_a_file_is_ambiguous() -> None:
    report = analyze({"a.py": "def f(): pass\ndef f(): pass\nf()\n"})

    assert edges(report, CALLS) == set()
    assert unresolved(report) == {("a.py", "CALLS", "f", "ambiguous")}


def test_same_type_in_two_files_of_one_java_package_is_ambiguous() -> None:
    report = analyze(
        {
            "a/User.java": "package app; class User {}",
            "b/User.java": "package app; class User {}",
            "Main.java": "package app; class Main { void run() { new User(); } }",
        }
    )

    assert unresolved(report) == {("Main.java:Main.run", "CALLS", "User", "ambiguous")}


def test_ambiguous_module_is_not_imported() -> None:
    report = analyze(
        {
            "app/config.py": "",
            "backend/app/config.py": "",
            "backend/app/main.py": "import app.config\n",
        }
    )

    assert edges(report, IMPORTS) == set()
    assert unresolved(report) == {("backend/app/main.py", "IMPORTS", "app.config", "ambiguous")}


# ----- Variables, aliases and receivers -----


def test_variable_with_conflicting_types_is_unknown() -> None:
    report = analyze(
        {
            "a.py": """\
class A:
    def run(self): pass

class B:
    def run(self): pass

def main():
    x = A()
    x = B()
    x.run()
"""
        }
    )

    assert ("a.py:main", "CALLS", "a.py:A.run") not in edges(report)
    assert ("a.py:main", "CALLS", "x.run", "unknown_receiver") in unresolved(report)


def test_variable_assigned_from_a_function_call_is_not_a_type() -> None:
    report = analyze(
        {"a.py": "class User:\n    def save(self): pass\n\ndef make(): pass\n\nu = make()\nu.save()\n"}
    )

    assert ("a.py", "CALLS", "a.py:User.save") not in edges(report)
    assert ("a.py", "CALLS", "u.save", "unknown_receiver") in unresolved(report)


def test_variable_type_cycles_do_not_hang() -> None:
    report = analyze({"a.py": "def f():\n    a = b.x()\n    b = a.y()\n    a.z()\n"})

    assert edges(report, CALLS) == set()


def test_re_export_cycles_do_not_hang() -> None:
    report = analyze(
        {
            "a.js": 'export { User } from "./b";\nexport * from "./b";\n',
            "b.js": 'export { User } from "./a";\nexport * from "./a";\n',
            "main.js": 'import { User, Other } from "./a";\nUser();\nOther();\n',
        }
    )

    assert edges(report, CALLS) == set()


def test_self_call_outside_a_class_is_not_resolved() -> None:
    report = analyze({"a.py": "def save(): pass\n\ndef run(self):\n    self.save()\n"})

    assert edges(report, CALLS) == set()


# ----- Source entity -----


def test_relationship_source_is_the_innermost_entity() -> None:
    report = analyze(
        {
            "a.py": """\
def helper(): pass

class User:
    helper()

    def login(self):
        helper()

    class Meta:
        def build(self):
            helper()
"""
        }
    )

    assert edges(report, CALLS) == {
        ("a.py:User", "CALLS", "a.py:helper"),
        ("a.py:User.login", "CALLS", "a.py:helper"),
        ("a.py:User.Meta.build", "CALLS", "a.py:helper"),
    }
