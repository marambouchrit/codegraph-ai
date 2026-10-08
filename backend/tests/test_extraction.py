"""Entity extraction: a source file gives its classes, functions and methods."""

from app.extraction.models import EntityType, FileEntities
from tests.helpers import FILES, PROJECT_ID, extraction_service, parser_service


def extract(path: str, source: str) -> FileEntities:
    return extraction_service.extract(parser_service.parse_source(source.encode(), path), PROJECT_ID)


def outline(result: FileEntities) -> list[tuple[str, str]]:
    """(type, qualified name) of every entity except the file itself, in source order."""
    return [(entity.type.value, entity.qualified_name) for entity in result.entities[1:]]


def test_a_python_file_gives_its_classes_methods_and_functions() -> None:
    result = extract("auth/service.py", FILES["auth/service.py"])

    assert outline(result) == [
        ("class", "AuthService"),
        ("method", "AuthService.login"),
        ("method", "AuthService.create_token"),
        ("function", "verify_password"),
    ]


def test_each_entity_knows_its_parent_and_its_lines() -> None:
    result = extract("auth/service.py", FILES["auth/service.py"])
    by_name = {entity.qualified_name: entity for entity in result.entities}
    file, service, login = result.file, by_name["AuthService"], by_name["AuthService.login"]

    assert file.type == EntityType.FILE and file.parent_id is None
    assert service.parent_id == file.id  # the class is in the file
    assert login.parent_id == service.id  # the method is in the class
    assert (login.start_line, login.end_line) == (14, 20)


def test_an_entity_id_is_project_file_and_qualified_name() -> None:
    result = extract("auth/service.py", FILES["auth/service.py"])

    assert [entity.id for entity in result.entities[:3]] == [
        f"{PROJECT_ID}:auth/service.py",
        f"{PROJECT_ID}:auth/service.py:AuthService",
        f"{PROJECT_ID}:auth/service.py:AuthService.login",
    ]


def test_a_javascript_file_gives_functions_arrow_functions_and_classes() -> None:
    source = """export function load(id) {
  return get(id);
}

export const save = (item) => {
  return post(item);
};

class Store {
  add(item) {
    this.items.push(item);
  }
}
"""
    assert outline(extract("src/store.js", source)) == [
        ("function", "load"),
        ("function", "save"),
        ("class", "Store"),
        ("method", "Store.add"),
    ]
