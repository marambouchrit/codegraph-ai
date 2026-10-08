"""Relationship extraction: calls, imports and inheritance, resolved across files."""

from tests.helpers import FILES, analyze, edges

LOGIN = "auth/service.py:AuthService.login"


def test_calls_are_resolved_across_files_and_inside_a_file() -> None:
    found = edges(analyze(FILES))

    # `repository.find_user(...)`: the variable's class is in another file.
    assert (LOGIN, "CALLS", "repository/user.py:UserRepository.find_user") in found
    # `verify_password(...)`: a function of the same file.
    assert (LOGIN, "CALLS", "auth/service.py:verify_password") in found
    # `self.create_token(...)`: a method of the same class.
    assert (LOGIN, "CALLS", "auth/service.py:AuthService.create_token") in found


def test_imports_link_files() -> None:
    found = edges(analyze(FILES))

    assert ("auth/service.py", "IMPORTS", "repository/user.py") in found
    assert ("repository/user.py", "IMPORTS", "db/database.py") in found


def test_inheritance_links_a_class_to_its_parent() -> None:
    child, parent = "repository/user.py:CachedUserRepository", "repository/user.py:UserRepository"

    assert (child, "INHERITS", parent) in edges(analyze(FILES))


def test_a_file_depends_on_the_files_whose_code_it_uses() -> None:
    found = edges(analyze(FILES))

    assert ("auth/service.py", "DEPENDS_ON", "repository/user.py") in found
    # Nothing links the charts to the rest of the project.
    assert not any("reports/charts.py" in (source, target) for source, _, target in found)


def test_external_libraries_are_reported_as_unresolved_not_as_relationships() -> None:
    report = analyze(FILES)

    # `import jwt` and `jwt.encode(...)`: the library is not part of the project.
    assert {(u.target_name, u.reason.value) for u in report.unresolved} >= {
        ("jwt", "external"),
        ("jwt.encode", "external"),
    }
    assert not any("jwt" in target for _, _, target in edges(report))
